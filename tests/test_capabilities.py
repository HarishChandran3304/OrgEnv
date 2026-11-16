from __future__ import annotations

import pytest

from orgenv import (
    Action,
    Actor,
    ActorDecision,
    ActorSpec,
    CapabilityDefinition,
    CapabilityRequest,
    DeterministicPolicy,
    OrgEnv,
    Resource,
    RolePolicy,
    TaskDefinition,
    WorldState,
)


def make_capability_env(policy=None) -> OrgEnv:
    state = WorldState(
        actors={"alex": Actor("alex", "engineer"), "priya": Actor("priya", "approver")},
        resources={"service": Resource("service", "service", state={"health": "green", "secret": "internal"})},
        task_data={"public": {"name": "example"}},
    )

    def get_health(world: WorldState, actor_id: str, args: dict) -> dict:
        return {"service": args["service"], "health": world.get_resource("service").state["health"]}

    def validate(args):
        if set(args) != {"service"} or args["service"] != "payments":
            raise ValueError("service must be payments")
        return args

    task = TaskDefinition(initial_state=state)
    task.register_capability(
        CapabilityDefinition(
            "get_service_health",
            get_health,
            validator=validate,
            policy=RolePolicy("get_service_health", {"approver"}),
        )
    )
    task.register_actor(ActorSpec("alex"))
    task.register_actor(ActorSpec("priya", policy=policy))
    return OrgEnv(task)


def test_registration_invocation_and_detached_result() -> None:
    env = make_capability_env()
    env.reset()
    result = env.query("priya", "get_service_health", {"service": "payments"})
    assert result["ok"] is True
    assert result["result"] == {"service": "payments", "health": "green"}
    result["result"]["health"] = "tampered"
    assert env.get_state()["resources"]["service"]["state"]["health"] == "green"
    assert env.actor_observation("alex")["capability_results"] == []
    assert env.actor_observation("priya")["capability_results"][0]["result"]["health"] == "green"


def test_unknown_invalid_and_unauthorized_queries_are_safe() -> None:
    env = make_capability_env()
    env.reset()
    unknown = env.query("priya", "missing", {})
    invalid = env.query("priya", "get_service_health", {"service": "other"})
    denied = env.query("alex", "get_service_health", {"service": "payments"})
    assert unknown["error"]["code"] == "unknown_capability"
    assert invalid["error"]["code"] == "invalid_arguments"
    assert denied["error"]["code"] == "unauthorized"
    assert "secret" not in denied["error"]["message"]
    assert env.get_state()["resources"]["service"]["state"]["secret"] == "internal"


def test_handler_runs_on_copy_and_query_is_recorded() -> None:
    env = make_capability_env()

    def mutating_read(world: WorldState, actor_id: str, args: dict) -> dict:
        world.get_resource("service").state["health"] = "mutated-copy"
        return {"health": world.get_resource("service").state["health"]}

    env.register_capability("mutating_read", mutating_read, policy=lambda state, actor_id, args: True)
    env.reset()
    result = env.query("priya", "mutating_read", {})
    assert result["result"]["health"] == "mutated-copy"
    assert env.get_state()["resources"]["service"]["state"]["health"] == "green"
    query_events = [event for event in env.events if event["kind"] == "query"]
    assert len(query_events) == 1
    assert query_events[0]["action"]["payload"]["actor_id"] == "priya"
    assert query_events[0]["query_result"]["ok"] is True


def test_policy_can_query_then_use_result_to_act() -> None:
    seen = []

    def priya_policy(observation):
        seen.append(observation)
        if not observation["capability_results"]:
            return ActorDecision(queries=(CapabilityRequest("get_service_health", {"service": "payments"}),))
        return ActorDecision(action=Action("priya", "approve", {}))

    env = make_capability_env(DeterministicPolicy(priya_policy))
    env.register_operation("approve", lambda world, action: world.task_data.update({"approved": True}))
    env.add_policy(RolePolicy("approve", {"approver"}))
    env.reset()
    result = env.run_actor("priya")
    assert result["outcome"] == "succeeded"
    assert env.get_state()["task_data"]["approved"] is True
    assert len(seen) == 2
    assert seen[1]["capability_results"][0]["result"]["health"] == "green"


def test_query_replay_and_reset() -> None:
    env = make_capability_env()
    env.reset(seed=12)
    env.query("priya", "get_service_health", {"service": "payments"})
    source_events = env.events
    source_state = env.get_state()
    replay = make_capability_env()
    assert replay.replay(source_events) == source_state
    assert replay.actor_observation("priya")["capability_results"] == env.actor_observation("priya")["capability_results"]
    env.reset(seed=12)
    assert env.events == ()
    assert env.actor_observation("priya")["capability_results"] == []


def test_capability_name_must_be_unique() -> None:
    env = make_capability_env()
    with pytest.raises(ValueError):
        env.register_capability("get_service_health", lambda state, actor_id, args: {})
