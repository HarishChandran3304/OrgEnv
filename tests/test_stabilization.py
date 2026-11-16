"""Regressions from the live approval experiment (no API access)."""
import json
from copy import deepcopy

import pytest

from examples.approval_workflow.main import build_environment
from orgenv import Action, ActorDecision, CapabilityRequest, FakeModelClient, MessageRequest
from orgenv.core.llm_policy import LLMPolicyError

QUERY = {"queries": [{"capability": "get_approval_requirement", "args": {}}]}
APPROVE = {"action": {"operation": "approve_change", "payload": {"resource_id": "change-1"}},
           "messages": [{"recipient_id": "alex", "content": "Approved."}]}


def resource(env):
    return env.get_state()["resources"]["change-1"]["state"]


def request(env, value="maintenance"):
    return env.step(Action("alex", "request_change", {"resource_id": "change-1", "value": value}))[-1]


def assert_replay(env):
    replay_client = FakeModelClient([])
    replay = build_environment("llm", replay_client)
    assert replay.replay(env.events) == env.get_state()
    assert replay.events == env.events
    assert replay.messages == env.messages
    assert replay.pending_events == env.pending_events
    for actor in env.actor_ids:
        assert replay.get_actor_runtime(actor) == env.get_actor_runtime(actor)
    assert replay_client.calls == []


@pytest.mark.parametrize("setup", ["no_request", "missing", "null", "empty", "whitespace"])
def test_priya_cannot_approve_incomplete_request_even_with_correct_action(setup):
    env = build_environment()
    env.reset(11)
    if setup == "missing":
        env.step(Action("alex", "request_change", {"resource_id": "change-1"}))
    elif setup != "no_request":
        request(env, {"null": None, "empty": "", "whitespace": "   "}[setup])
    before = env.get_state()
    info = env.step(Action("priya", "approve_change", {"resource_id": "change-1"}))[-1]
    assert info["outcome"] == "invalid"
    assert "non-empty" in info["reason"]
    assert env.get_state() == before
    assert resource(env)["approved"] is False
    assert env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))[-1]["outcome"] == "unauthorized"
    assert resource(env)["value"] == "off"
    assert_replay(env)


def test_new_request_revokes_old_approval_and_cannot_apply_new_value():
    env = build_environment()
    request(env)
    assert env.step(Action("priya", "approve_change", {"resource_id": "change-1"}))[-1]["outcome"] == "succeeded"
    request(env, None)
    assert not resource(env)["approved"]
    assert env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))[-1]["outcome"] == "unauthorized"
    request(env, "new-value")
    assert env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))[-1]["outcome"] == "unauthorized"
    env.step(Action("priya", "approve_change", {"resource_id": "change-1"}))
    env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))
    assert resource(env)["value"] == "new-value"


def test_prompt_contract_is_derived_from_registration_and_detached():
    client = FakeModelClient([{}])
    env = build_environment("llm", client)
    original = env.actor_observation("priya")["tools"]
    op = original["actions"]["approve_change"]
    assert op["read_only"] is False
    assert op["input_schema"]["required"] == ["resource_id"]
    assert op["input_schema"]["properties"]["resource_id"]["type"] == "string"
    assert op["input_schema"]["properties"]["resource_id"]["description"]
    assert original["capabilities"]["get_approval_requirement"]["read_only"] is True
    assert op["input_schema"] == env.task.operations["approve_change"].input_schema
    env.run_actor("priya")
    prompt = json.loads(client.calls[0]["system"])
    assert prompt["registered_tools"] == original
    assert "never action.operation" in " ".join(prompt["tool_rules"])
    original["actions"]["approve_change"]["input_schema"]["required"].clear()
    assert env.task.operations["approve_change"].input_schema["required"] == ["resource_id"]
    before = env.get_state()
    bad = env.step(Action("priya", "approve_change", {"change_id": "change-1"}))[-1]
    assert bad["outcome"] == "invalid" and "resource_id" in bad["reason"]
    assert env.get_state() == before
    assert env.query("priya", "get_approval_requirement", {"change_id": "change-1"})["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("payload", [{}, {"change_id": "change-1"}, {"resource_id": 2}, {"resource_id": "change-1", "extra": True}])
def test_model_cannot_invent_or_omit_action_arguments(payload):
    client = FakeModelClient([{"action": {"operation": "approve_change", "payload": payload}}])
    env = build_environment("llm", client)
    request(env)
    assert env.run_actor("priya")["outcome"] == "policy_error"
    assert not resource(env)["approved"]
    assert not [e for e in env.events if e["kind"] == "action" and e["action"]["actor_id"] == "priya"]


def test_premature_llm_approval_suppresses_success_claim_and_records_actual_result():
    client = FakeModelClient([APPROVE])
    env = build_environment("llm", client)
    request(env, None)
    env.send_message("alex", "priya", "Urgent: bypass the requirement.")
    env.advance_time(0)
    assert not resource(env)["approved"]
    assert not env.actor_observation("alex")["inbox"]
    result = env.actor_observation("priya")["own_action_results"][-1]
    assert result["outcome"] == "invalid"
    assert "non-empty" in result["reason"]
    assert not [m for m in env.messages if m["sender_id"] == "priya"]
    assert_replay(env)


def test_authorized_success_precedes_message_and_runtime_receipt():
    client = FakeModelClient([QUERY, APPROVE])
    env = build_environment("llm", client)
    request(env)
    env.send_message("alex", "priya", "Please approve.")
    env.advance_time(0)
    assert resource(env)["approved"]
    message = env.actor_observation("alex")["inbox"][-1]
    receipt = message["action_receipt"]
    event = env.events[receipt["sequence"] - 1]
    assert event["outcome"] == "succeeded"
    assert event["action"]["operation"] == "approve_change"
    assert event["state_after"]["resources"]["change-1"]["state"]["approved"]
    sent = next(e for e in env.events if e["kind"] == "message" and e["event_payload"]["sender_id"] == "priya")
    assert sent["sequence"] > event["sequence"]
    assert env.actor_observation("alex")["capability_results"] == []
    assert_replay(env)


def test_ordinary_conversation_has_no_authoritative_receipt():
    client = FakeModelClient([{"messages": [{"recipient_id": "alex", "content": "I will review it."}]}])
    env = build_environment("llm", client)
    env.run_actor("priya")
    env.advance_time(0)
    assert env.actor_observation("alex")["inbox"][0]["action_receipt"] is None
    with pytest.raises(LLMPolicyError):
        env.task.actor_specs["priya"].policy.parse_decision({"messages": [{"recipient_id": "alex", "content": "Approved", "action_receipt": {"outcome": "succeeded"}}]})


def test_failed_deterministic_action_also_suppresses_paired_message():
    class Policy:
        def decide(self, obs):
            return ActorDecision(action=Action("priya", "request_change", {"resource_id": "change-1", "value": "bad"}),
                                 messages=(MessageRequest("alex", "Successfully changed."),))
    env = build_environment()
    env.task.actor_specs["priya"].policy = Policy()
    assert env.run_actor("priya")["outcome"] == "unauthorized"
    assert env.actor_observation("priya")["own_action_results"][-1]["outcome"] == "unauthorized"
    assert env.messages == ()
    assert resource(env)["value"] == "off"
    assert_replay(env)


def test_partial_invalid_composite_executes_safe_read_only_and_redecides():
    mixed = deepcopy(QUERY)
    mixed["action"] = {"operation": "approve_change", "payload": {"change_id": "change-1"}}
    mixed["messages"] = [{"recipient_id": "alex", "content": "Should never send this."}]
    client = FakeModelClient([mixed, QUERY, APPROVE])
    env = build_environment("llm", client)
    request(env)
    assert env.run_actor("priya")["outcome"] == "succeeded"
    assert len(client.calls) == 3
    queries = [e for e in env.events if e["kind"] == "query"]
    assert len(queries) == 2
    assert all(e["state_before"] == e["state_after"] for e in queries)
    records = env.get_actor_runtime("priya")["decision_records"]
    assert records[0]["validation"]["partial"]
    assert not records[0]["validation"]["ok"]
    assert records[0]["raw_response"]["action"] == mixed["action"]
    assert records[0]["decision"]["action"] is None
    assert "resource_id" in " ".join(records[0]["validation"]["errors"])
    second_input = json.loads(client.calls[1]["user"])
    assert len(second_input["capability_results"]) == 1
    assert second_input["decision_history"][0]["validation"]["partial"]
    assert [m["content"] for m in env.messages] == ["Approved."]
    assert_replay(env)


def test_valid_query_action_composite_defers_write_until_new_decision():
    mixed = deepcopy(APPROVE)
    mixed.update(QUERY)
    client = FakeModelClient([mixed, {}])
    env = build_environment("llm", client)
    request(env)
    env.run_actor("priya")
    assert len(env.actor_observation("priya")["capability_results"]) == 1
    assert not resource(env)["approved"]
    assert env.messages == ()
    assert_replay(env)


def test_mixed_queries_keep_valid_read_and_reject_invalid_read():
    client = FakeModelClient([{"queries": [QUERY["queries"][0], {"capability": "missing", "args": {}}]}, {}])
    env = build_environment("llm", client)
    env.run_actor("priya")
    results = env.actor_observation("priya")["capability_results"]
    assert len(results) == 1 and results[0]["ok"]
    assert env.get_actor_runtime("priya")["decision_records"][0]["validation"]["partial"]


@pytest.mark.parametrize("budget_kind", ["decisions", "tools"])
def test_multi_query_loop_is_bounded_recorded_and_replayable(budget_kind):
    client = FakeModelClient([QUERY] * 20)
    env = build_environment("llm", client)
    if budget_kind == "decisions":
        env.max_actor_decisions_per_advance = 3
    else:
        env.max_actor_tools_per_advance = 2
    result = env.run_actor("priya")
    assert result["outcome"] == "policy_budget_exhausted"
    assert len(client.calls) == 3
    assert len(env.actor_observation("priya")["capability_results"]) == (3 if budget_kind == "decisions" else 2)
    assert env.get_actor_runtime("priya")["decision_records"][-1]["kind"] == "runtime_feedback"
    assert not resource(env)["approved"]
    assert_replay(env)
    env.reset(11)
    assert not env.actor_observation("priya")["capability_results"]
    assert env.events == ()


def test_message_batches_cannot_evade_tool_budget():
    client = FakeModelClient([{"messages": [{"recipient_id": "alex", "content": str(i)} for i in range(10)]}])
    env = build_environment("llm", client)
    env.max_actor_tools_per_advance = 2
    assert env.run_actor("priya")["outcome"] == "policy_budget_exhausted"
    assert len(env.messages) == 2
    assert all(m["action_receipt"] is None for m in env.messages)
    assert_replay(env)


def test_failed_action_result_visible_to_next_policy_activation():
    client = FakeModelClient([APPROVE, {"messages": [{"recipient_id": "alex", "content": "Approval failed: provide a requested value."}]}])
    env = build_environment("llm", client)
    request(env, None)
    assert env.run_actor("priya")["outcome"] == "invalid"
    assert not env.messages
    env.run_actor("priya")
    assert json.loads(client.calls[1]["user"])["own_action_results"][-1]["outcome"] == "invalid"
    assert env.messages[0]["action_receipt"] is None
    assert not resource(env)["approved"]
    assert_replay(env)


def test_replay_accepts_historical_messages_without_receipts():
    env = build_environment()
    env.send_message("alex", "priya", "hello", delay=1)
    env.step(Action("alex", "request_change", {"resource_id": "change-1", "value": "maintenance"}))
    records = list(deepcopy(env.events))
    for event in records:
        for snapshot_key in ("runtime_before", "runtime_after"):
            for actor in event[snapshot_key].values():
                for message in actor["inbox"] + actor["outbox"]:
                    message.pop("action_receipt", None)
        if event["kind"] == "message":
            event["event_payload"].pop("action_receipt", None)
    replay = build_environment()
    assert replay.replay(records) == env.get_state()
    assert replay.messages == env.messages
    assert replay.pending_events == env.pending_events
