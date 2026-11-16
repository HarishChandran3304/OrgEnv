from __future__ import annotations

import pytest

from orgenv import Action, Actor, OrgEnv, Resource, TaskDefinition, WorldState
from orgenv.core.engine import EventProcessingLimitError, ReplayError


def runtime_task() -> TaskDefinition:
    state = WorldState(
        actors={"a": Actor("a", "operator")},
        resources={"r": Resource("r", "counter", owner_id="a", state={"value": 0})},
        task_data={"order": [], "samples": []},
    )

    def sample(world: WorldState, action: Action, context) -> None:
        world.task_data["samples"].append(
            {
                "a": context.random.integer("mechanism_a", 1, 100),
                "b": context.random.integer("mechanism_b", 1, 100),
            }
        )

    def schedule_marker(world: WorldState, action: Action, context) -> None:
        context.schedule("mark", {"label": action.payload["label"]}, delay=action.payload.get("delay", 1))

    def mark(world: WorldState, event, context) -> None:
        world.task_data["order"].append(event.payload["label"])
        world.get_resource("r").state["value"] += 1
        if event.payload.get("chain"):
            context.schedule("mark", {"label": event.payload["chain"]}, delay=0)
        if event.payload.get("repeat"):
            context.schedule("mark", {"label": event.payload["label"], "repeat": True}, delay=0)

    def fail(world: WorldState, event) -> None:
        world.get_resource("r").state["value"] = 99
        raise RuntimeError("event failed deliberately")

    task = TaskDefinition(initial_state=state, operations={"sample": sample, "schedule": schedule_marker})
    task.register_event_handler("mark", mark)
    task.register_event_handler("fail", fail)
    return task


def test_clock_advance_is_explicit_monotonic_and_resettable() -> None:
    env = OrgEnv(runtime_task())
    env.reset(seed=2)
    assert env.current_time == 0.0
    env.advance_time(2.5)
    assert env.current_time == 2.5
    with pytest.raises(ValueError):
        env.advance_time(-1)
    env.reset(seed=2)
    assert env.current_time == 0.0 and env.pending_events == () and env.events == ()


def test_same_timestamp_order_and_chained_events_are_deterministic() -> None:
    env = OrgEnv(runtime_task())
    env.reset()
    env.schedule_event("mark", {"label": "first", "chain": "child"}, delay=2)
    env.schedule_event("mark", {"label": "second"}, delay=2)
    processed = env.advance_time(2)
    assert [record["event_payload"]["label"] for record in processed] == ["first", "second", "child"]
    assert env.get_state()["task_data"]["order"] == ["first", "second", "child"]
    assert [record["sequence"] for record in env.events] == [1, 2, 3]


def test_large_advance_executes_events_at_their_due_times() -> None:
    env = OrgEnv(runtime_task())
    env.reset()
    env.schedule_event("mark", {"label": "due-2"}, delay=2)
    env.schedule_event("mark", {"label": "due-5"}, delay=5)
    processed = env.advance_time(5)
    assert [record["sim_time"] for record in processed] == [2.0, 5.0]
    assert env.current_time == 5.0


def test_precondition_and_handler_failure_are_recorded_without_state_mutation() -> None:
    env = OrgEnv(runtime_task())
    env.reset()
    env.schedule_event("mark", {"label": "blocked"}, delay=1, precondition=lambda state, event: False)
    env.schedule_event("fail", {}, delay=1)
    before = env.get_state()
    processed = env.advance_time(1)
    assert [record["outcome"] for record in processed] == ["skipped", "failed"]
    assert env.get_state() == before
    assert "precondition" in processed[0]["reason"]
    assert "deliberately" in processed[1]["reason"]


def test_event_processing_bound_stops_zero_delay_loops() -> None:
    env = OrgEnv(runtime_task(), max_events_per_advance=3)
    env.reset()
    env.schedule_event("mark", {"label": "loop", "repeat": True})
    with pytest.raises(EventProcessingLimitError):
        env.advance_time(0)
    assert len(env.events) == 3
    assert env.pending_events


def test_seeded_randomness_repeats_and_streams_are_isolated() -> None:
    left, right = OrgEnv(runtime_task()), OrgEnv(runtime_task())
    left.reset(seed=44)
    right.reset(seed=44)
    action = Action("a", "sample", {})
    left.step(action)
    right.step(action)
    assert left.get_state() == right.get_state()
    assert left.events == right.events

    only_a = OrgEnv(runtime_task())
    only_a.reset(seed=44)
    only_a.task.operations["sample"] = lambda world, action, context: world.task_data["samples"].append(
        {"a": context.random.integer("mechanism_a", 1, 100)}
    )
    only_a.step(action)
    assert left.get_state()["task_data"]["samples"][0]["a"] == only_a.get_state()["task_data"]["samples"][0]["a"]
    assert left.events[0]["random_samples"][0]["mechanism"] == "mechanism_a"


def test_replay_uses_recorded_state_and_detects_tampering() -> None:
    source = OrgEnv(runtime_task())
    source.reset(seed=10)
    source.step(Action("a", "schedule", {"label": "done", "delay": 2}))
    source.advance_time(2)
    records = source.events
    replay = OrgEnv(runtime_task())
    assert replay.replay(records) == source.get_state()
    assert replay.events == records

    pending_source = OrgEnv(runtime_task())
    pending_source.reset(seed=10)
    pending_source.step(Action("a", "schedule", {"label": "future", "delay": 4}))
    pending_replay = OrgEnv(runtime_task())
    pending_replay.replay(pending_source.events)
    assert pending_replay.pending_events == pending_source.pending_events

    tampered = [dict(record) for record in records]
    tampered[0]["state_after"] = dict(tampered[0]["state_after"], task_data={"order": ["tampered"], "samples": []})
    with pytest.raises(ReplayError):
        OrgEnv(runtime_task()).replay(tampered)


def test_reset_clears_scheduled_work_and_random_streams() -> None:
    env = OrgEnv(runtime_task())
    env.reset(seed=99)
    env.step(Action("a", "schedule", {"label": "discarded", "delay": 5}))
    env.reset(seed=99)
    assert env.pending_events == ()
    env.step(Action("a", "sample", {}))
    first = env.get_state()
    env.reset(seed=99)
    env.step(Action("a", "sample", {}))
    assert env.get_state() == first
