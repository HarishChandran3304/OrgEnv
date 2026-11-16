from __future__ import annotations

import json

import pytest

from orgenv import (
    Action,
    Actor,
    ActorProfile,
    ActorSpec,
    CapabilityDefinition,
    CapabilityRequest,
    FakeModelClient,
    LLMActorPolicy,
    LLMPolicyError,
    ModelClientError,
    OrgEnv,
    RolePolicy,
    TaskDefinition,
    WorldState,
)


def llm_environment(client: FakeModelClient) -> OrgEnv:
    state = WorldState(
        actors={"priya": Actor("priya", "approver")},
        task_data={"public": {"process": "approval"}},
    )

    def requirement(world: WorldState, actor_id: str, args: dict) -> dict:
        return {"required": "a requested value"}

    def approve(world: WorldState, action: Action) -> None:
        world.task_data["approved"] = True

    profile = ActorProfile(
        role="approver",
        responsibilities=("review changes",),
        behavioral_instructions="Use authorized tools and communicate decisions clearly.",
        priorities=("safety", "accuracy"),
    )
    policy = LLMActorPolicy(
        actor_id="priya",
        profile=profile,
        client=client,
        model="fake-model",
        available_operations=("approve",),
        available_capabilities=("get_requirement",),
    )
    task = TaskDefinition(initial_state=state)
    task.register_operation("approve", approve)
    task.register_capability(
        CapabilityDefinition("get_requirement", requirement, policy=RolePolicy("get_requirement", {"approver"}))
    )
    task.register_actor(ActorSpec("priya", policy=policy, profile=profile))
    task.add_policy(RolePolicy("approve", {"approver"}))
    return OrgEnv(task)


def test_llm_policy_parses_structured_decisions_and_includes_profile() -> None:
    client = FakeModelClient(
        [
            {"queries": [{"capability": "get_requirement", "args": {}}], "messages": [], "action": None},
            {"queries": [], "messages": [], "action": None},
        ]
    )
    env = llm_environment(client)
    env.reset()
    result = env.run_actor("priya")
    assert result is None
    assert len(client.calls) == 2
    assert "authorized tools" in client.calls[0]["system"]
    assert "approver" in client.calls[0]["system"]
    assert env.actor_observation("priya")["capability_results"][0]["ok"] is True
    assert env.get_actor_runtime("priya")["decision_records"][0]["validation"]["ok"] is True


def test_llm_policy_queries_then_executes_authorized_action() -> None:
    client = FakeModelClient(
        [
            {"queries": [{"capability": "get_requirement", "args": {}}], "messages": [], "action": None},
            {"queries": [], "messages": [], "action": {"operation": "approve", "payload": {}}},
        ]
    )
    env = llm_environment(client)
    env.reset()
    result = env.run_actor("priya")
    assert result["outcome"] == "succeeded"
    assert env.get_state()["task_data"]["approved"] is True
    assert len(client.calls) == 2
    assert [record["kind"] for record in env.get_actor_runtime("priya")["decision_records"]] == ["llm_decision", "llm_decision"]


def test_llm_policy_rejects_malformed_or_unsupported_output() -> None:
    policy = LLMActorPolicy(
        actor_id="a",
        profile=ActorProfile("role"),
        client=FakeModelClient({"action": {"operation": "unknown", "payload": {}}}),
        available_operations=("known",),
    )
    with pytest.raises(LLMPolicyError):
        policy.decide({"public": {}})
    assert policy.last_record["validation"]["ok"] is False

    with pytest.raises(LLMPolicyError):
        policy.parse_decision({"action": {"operation": "known", "actor_id": "other", "payload": {}}})


def test_model_failure_does_not_mutate_environment() -> None:
    class FailingClient:
        def generate_json(self, **kwargs):
            raise ModelClientError("offline")

    state = WorldState(actors={"a": Actor("a", "operator")})
    policy = LLMActorPolicy(actor_id="a", profile=ActorProfile("operator"), client=FailingClient())
    task = TaskDefinition(initial_state=state)
    task.register_actor(ActorSpec("a", policy=policy))
    env = OrgEnv(task)
    env.reset()
    before = env.get_state()
    result = env.run_actor("a")
    assert result["outcome"] == "policy_error"
    assert env.get_state() == before
    assert env.get_actor_runtime("a")["decision_records"][0]["validation"]["ok"] is False


def test_llm_action_still_hits_existing_authorization() -> None:
    client = FakeModelClient(
        [{"queries": [], "messages": [], "action": {"operation": "approve", "payload": {}}}]
    )
    env = llm_environment(client)
    env.task.policies = [RolePolicy("approve", {"different-role"})]
    env.reset()
    result = env.run_actor("priya")
    assert result["outcome"] == "unauthorized"
    assert env.get_state().get("approved") is None


def test_replay_does_not_call_llm() -> None:
    source_client = FakeModelClient(
        [
            {"queries": [{"capability": "get_requirement", "args": {}}], "messages": [], "action": None},
            {"queries": [], "messages": [], "action": {"operation": "approve", "payload": {}}},
        ]
    )
    source = llm_environment(source_client)
    source.reset(seed=7)
    source.run_actor("priya")
    records = source.events
    replay_client = FakeModelClient([])
    replay = llm_environment(replay_client)
    assert replay.replay(records) == source.get_state()
    assert replay_client.calls == []
    assert replay.get_actor_runtime("priya")["decision_records"] == source.get_actor_runtime("priya")["decision_records"]
