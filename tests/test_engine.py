from __future__ import annotations

from copy import deepcopy

import pytest

from orgenv import (
    Action,
    ActionExecutionError,
    Actor,
    ApprovalRequiredPolicy,
    OrgEnv,
    Resource,
    RolePolicy,
    TaskDefinition,
    WorldState,
)


def make_task() -> TaskDefinition:
    state = WorldState(
        actors={
            "a": Actor("a", "requester"),
            "b": Actor("b", "approver"),
            "x": Actor("x", "observer"),
        },
        resources={"r": Resource("r", "change", owner_id="a", state={"value": "old", "approved": False})},
        task_data={"counter": 0},
    )

    def request(world: WorldState, action: Action) -> None:
        resource = world.get_resource(action.payload["resource_id"])
        resource.state["requested_value"] = action.payload["value"]

    def approve(world: WorldState, action: Action) -> None:
        world.get_resource(action.payload["resource_id"]).state["approved"] = True

    def apply(world: WorldState, action: Action) -> None:
        resource = world.get_resource(action.payload["resource_id"])
        resource.state["value"] = resource.state["requested_value"]

    return TaskDefinition(
        initial_state=state,
        operations={"request": request, "approve": approve, "apply": apply},
        policies=[RolePolicy("request", {"requester"}), RolePolicy("approve", {"approver"}), ApprovalRequiredPolicy("apply")],
        success_condition=lambda world: world.get_resource("r").state["value"] == "new",
    )


def test_initialization_lookup_and_reset_restore_state() -> None:
    task = make_task()
    env = OrgEnv(task)
    initial = env.reset(seed=42)
    assert initial["actors"]["a"]["role"] == "requester"
    assert env.get_state()["resources"]["r"]["state"]["value"] == "old"
    env.step(Action("a", "request", {"resource_id": "r", "value": "new"}))
    assert env.get_state()["resources"]["r"]["state"]["requested_value"] == "new"
    reset = env.reset()
    assert reset == initial
    assert env.events == ()
    with pytest.raises(KeyError):
        task.initial_state.get_actor("missing")
    with pytest.raises(KeyError):
        task.initial_state.get_resource("missing")


def test_valid_transition_and_detached_observation() -> None:
    env = OrgEnv(make_task())
    observation = env.reset()
    observation["resources"]["r"]["state"]["value"] = "tampered"
    assert env.get_state()["resources"]["r"]["state"]["value"] == "old"
    _, reward, terminated, truncated, info = env.step(Action("a", "request", {"resource_id": "r", "value": "new"}))
    assert reward == 0.0 and not terminated and not truncated
    assert info["outcome"] == "succeeded"
    assert env.get_state()["resources"]["r"]["state"]["requested_value"] == "new"


def test_mapping_actions_are_coerced_to_the_universal_action() -> None:
    env = OrgEnv(make_task())
    env.reset()
    _, _, _, _, info = env.step({"actor_id": "a", "operation": "request", "payload": {"resource_id": "r", "value": "new"}})
    assert info["outcome"] == "succeeded"


def test_invalid_operation_does_not_mutate_state() -> None:
    env = OrgEnv(make_task())
    env.reset()
    before = env.get_state()
    _, _, _, _, info = env.step(Action("a", "does_not_exist", {}))
    assert info["outcome"] == "invalid"
    assert "unknown operation" in info["reason"]
    assert env.get_state() == before
    assert env.events[-1]["state_changes"] == []


def test_unauthorized_action_does_not_mutate_state() -> None:
    env = OrgEnv(make_task())
    env.reset()
    before = env.get_state()
    _, _, _, _, info = env.step(Action("x", "approve", {"resource_id": "r"}))
    assert info["outcome"] == "unauthorized"
    assert "not authorized" in info["reason"]
    assert env.get_state() == before


def test_successful_authorization_workflow() -> None:
    env = OrgEnv(make_task())
    env.reset()
    env.step(Action("a", "request", {"resource_id": "r", "value": "new"}))
    _, _, _, _, denied = env.step(Action("a", "apply", {"resource_id": "r"}))
    assert denied["outcome"] == "unauthorized"
    env.step(Action("b", "approve", {"resource_id": "r"}))
    _, reward, terminated, _, info = env.step(Action("a", "apply", {"resource_id": "r"}))
    assert info["outcome"] == "succeeded"
    assert reward == 1.0 and terminated
    assert env.get_state()["resources"]["r"]["state"]["value"] == "new"


def test_event_order_and_state_changes_are_inspectable() -> None:
    env = OrgEnv(make_task())
    env.reset()
    env.step(Action("a", "request", {"resource_id": "r", "value": "new"}))
    env.step(Action("a", "apply", {"resource_id": "r"}))
    assert [event["sequence"] for event in env.events] == [1, 2]
    assert env.events[0]["outcome"] == "succeeded"
    assert any(change["path"].endswith("requested_value") for change in env.events[0]["state_changes"])
    assert env.events[1]["outcome"] == "unauthorized"
    assert env.events[1]["state_before"] == env.events[1]["state_after"]


def test_determinism_for_identical_sequences() -> None:
    actions = [
        Action("a", "request", {"resource_id": "r", "value": "new"}),
        Action("a", "apply", {"resource_id": "r"}),
        Action("b", "approve", {"resource_id": "r"}),
        Action("a", "apply", {"resource_id": "r"}),
    ]
    left, right = OrgEnv(make_task()), OrgEnv(make_task())
    left.reset(seed=7)
    right.reset(seed=7)
    left_results = [left.step(action) for action in actions]
    right_results = [right.step(action) for action in actions]
    assert left_results == right_results
    assert left.get_state() == right.get_state()
    assert left.events == right.events


def test_task_specific_registration_without_core_changes() -> None:
    task = make_task()

    def increment(world: WorldState, action: Action) -> None:
        world.task_data["counter"] += action.payload.get("amount", 1)

    task.register_operation("increment", increment)
    env = OrgEnv(task)
    env.reset()
    env.step(Action("a", "increment", {"amount": 3}))
    assert env.get_state()["task_data"]["counter"] == 3


def test_handler_rejection_is_atomic() -> None:
    task = make_task()

    def reject_after_mutation(world: WorldState, action: Action) -> None:
        world.task_data["counter"] = 99
        raise ActionExecutionError("business rule rejected")

    task.register_operation("reject", reject_after_mutation)
    env = OrgEnv(task)
    env.reset()
    before = deepcopy(env.get_state())
    _, _, _, _, info = env.step(Action("a", "reject", {}))
    assert info["outcome"] == "invalid"
    assert env.get_state() == before
