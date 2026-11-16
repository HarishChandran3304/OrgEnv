from __future__ import annotations

import pytest

from orgenv import (
    Action,
    Actor,
    ActorDecision,
    ActorProcessingLimitError,
    ActorSpec,
    DeterministicPolicy,
    MessageRequest,
    OrgEnv,
    Resource,
    RolePolicy,
    TaskDefinition,
    WorldState,
)


def actor_task(
    *,
    alice_policy=None,
    bob_policy=None,
    bob_role="approver",
    max_actor_actions=100,
) -> OrgEnv:
    state = WorldState(
        actors={"alice": Actor("alice", "requester"), "bob": Actor("bob", bob_role)},
        resources={"r": Resource("r", "request", owner_id="alice", state={"approved": False, "value": "old"})},
        task_data={"public": {"department": "operations"}},
    )

    def approve(world: WorldState, action: Action) -> None:
        world.get_resource("r").state["approved"] = True

    def set_value(world: WorldState, action: Action) -> None:
        world.get_resource("r").state["value"] = action.payload["value"]

    task = TaskDefinition(initial_state=state)
    task.register_operation("approve", approve)
    task.register_operation("set_value", set_value)
    task.add_policy(RolePolicy("approve", {"approver"}))
    task.add_policy(RolePolicy("set_value", {"requester"}))
    task.register_actor(ActorSpec("alice", policy=alice_policy, visible_resource_ids=frozenset({"r"})))
    task.register_actor(
        ActorSpec(
            "bob",
            policy=bob_policy,
            private_knowledge={"approval_rule": "Check the request before approving."},
            visible_resource_ids=frozenset({"r"}),
        )
    )
    env = OrgEnv(task, max_actor_actions_per_advance=max_actor_actions)
    return env


def test_actor_observations_are_isolated_and_detached() -> None:
    env = actor_task()
    env.reset()
    alice = env.actor_observation("alice")
    bob = env.actor_observation("bob")
    assert "approval_rule" not in alice["private_knowledge"]
    assert bob["private_knowledge"]["approval_rule"]
    alice["resources"]["r"]["state"]["approved"] = True
    alice["private_knowledge"]["new_secret"] = True
    assert env.get_state()["resources"]["r"]["state"]["approved"] is False
    assert "new_secret" not in env.actor_observation("alice")["private_knowledge"]


def test_delayed_messages_deliver_in_timestamp_and_insertion_order() -> None:
    env = actor_task()
    env.reset()
    first = env.send_message("alice", "bob", "first", delay=2)
    second = env.send_message("alice", "bob", "second", delay=2)
    assert first != second
    assert env.get_actor_runtime("bob")["inbox"] == []
    env.advance_time(1)
    assert env.get_actor_runtime("bob")["inbox"] == []
    env.advance_time(1)
    inbox = env.get_actor_runtime("bob")["inbox"]
    assert [message["content"] for message in inbox] == ["first", "second"]
    assert all(message["status"] == "delivered" for message in inbox)


def test_policy_receives_observation_and_can_send_bound_message_and_action() -> None:
    seen = []

    def bob_decide(observation):
        seen.append(observation)
        if observation["inbox"]:
            return ActorDecision(
                action=Action("bob", "approve", {"resource_id": "r"}),
                messages=(MessageRequest("alice", "approved"),),
            )
        return None

    env = actor_task(bob_policy=DeterministicPolicy(bob_decide))
    env.reset()
    env.send_message("alice", "bob", "please approve", delay=1)
    env.advance_time(1)
    assert seen and "approval_rule" in seen[0]["private_knowledge"]
    assert env.get_state()["resources"]["r"]["state"]["approved"] is True
    assert env.get_actor_runtime("alice")["inbox"][0]["sender_id"] == "bob"
    assert env.get_actor_runtime("bob")["outbox"][0]["sender_id"] == "bob"


def test_direct_unauthorized_action_and_policy_impersonation_are_rejected() -> None:
    env = actor_task(bob_policy=DeterministicPolicy(lambda observation: Action("alice", "set_value", {"value": "bad"})))
    env.reset()
    _, _, _, _, info = env.step(Action("bob", "set_value", {"value": "bad"}))
    assert info["outcome"] == "unauthorized"
    env.run_actor("bob")
    result = env.get_actor_runtime("bob")["action_results"][0]
    assert result["outcome"] == "invalid"
    assert env.get_state()["resources"]["r"]["state"]["value"] == "old"
    with pytest.raises(KeyError):
        env.send_message("alice", "missing", "no")


def test_autonomous_response_loop_is_bounded() -> None:
    always_reply = DeterministicPolicy(lambda observation: ActorDecision(messages=(MessageRequest("alice", "loop"),)))
    env = actor_task(bob_policy=always_reply, alice_policy=always_reply, max_actor_actions=3)
    env.reset()
    env.send_message("alice", "bob", "start")
    with pytest.raises(ActorProcessingLimitError):
        env.advance_time(0)
    assert len(env.events) >= 3


def test_actor_reset_reproducibility_and_replay() -> None:
    policy = DeterministicPolicy(
        lambda observation: ActorDecision(messages=(MessageRequest("alice", "done"),)) if observation["inbox"] else None
    )
    env = actor_task(bob_policy=policy)
    env.reset(seed=5)
    env.send_message("alice", "bob", "request", delay=1)
    env.advance_time(1)
    source_events = env.events
    source_state = env.get_state()
    source_runtime = env.get_actor_runtime("alice")
    replay = actor_task(bob_policy=policy)
    assert replay.replay(source_events) == source_state
    assert replay.get_actor_runtime("alice") == source_runtime
    assert replay.messages == env.messages
    env.reset(seed=5)
    assert env.events == () and env.get_actor_runtime("bob")["inbox"] == []
