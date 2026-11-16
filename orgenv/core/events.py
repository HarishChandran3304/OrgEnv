"""Append-only transition records."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["succeeded", "invalid", "unauthorized", "failed", "skipped", "scheduled"]


@dataclass(frozen=True)
class StateChange:
    path: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "before": deepcopy(self.before), "after": deepcopy(self.after)}


@dataclass(frozen=True)
class TransitionEvent:
    sequence: int
    action: dict[str, Any]
    outcome: Outcome
    reason: str
    state_changes: tuple[StateChange, ...]
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    kind: Literal["action", "scheduled", "message", "policy", "query"] = "action"
    sim_time: float = 0.0
    event_id: str | None = None
    event_type: str | None = None
    event_payload: dict[str, Any] | None = None
    event_order: int | None = None
    random_samples: tuple[dict[str, Any], ...] = ()
    scheduled_events: tuple[dict[str, Any], ...] = ()
    seed: int | None = None
    runtime_before: dict[str, Any] = field(default_factory=dict)
    runtime_after: dict[str, Any] = field(default_factory=dict)
    query_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": deepcopy(self.action),
            "outcome": self.outcome,
            "reason": self.reason,
            "state_changes": [change.to_dict() for change in self.state_changes],
            "state_before": deepcopy(self.state_before),
            "state_after": deepcopy(self.state_after),
            "kind": self.kind,
            "sim_time": self.sim_time,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_payload": deepcopy(self.event_payload),
            "event_order": self.event_order,
            "random_samples": deepcopy(list(self.random_samples)),
            "scheduled_events": deepcopy(list(self.scheduled_events)),
            "seed": self.seed,
            "runtime_before": deepcopy(self.runtime_before),
            "runtime_after": deepcopy(self.runtime_after),
            "query_result": deepcopy(self.query_result),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransitionEvent":
        return cls(
            sequence=value["sequence"],
            action=deepcopy(value.get("action", {})),
            outcome=value["outcome"],
            reason=value.get("reason", ""),
            state_changes=tuple(StateChange(**change) for change in value.get("state_changes", [])),
            state_before=deepcopy(value.get("state_before", {})),
            state_after=deepcopy(value.get("state_after", {})),
            kind=value.get("kind", "action"),
            sim_time=float(value.get("sim_time", 0.0)),
            event_id=value.get("event_id"),
            event_type=value.get("event_type"),
            event_payload=deepcopy(value.get("event_payload")),
            event_order=value.get("event_order"),
            random_samples=tuple(deepcopy(value.get("random_samples", []))),
            scheduled_events=tuple(deepcopy(value.get("scheduled_events", []))),
            seed=value.get("seed"),
            runtime_before=deepcopy(value.get("runtime_before", {})),
            runtime_after=deepcopy(value.get("runtime_after", {})),
            query_result=deepcopy(value.get("query_result")),
        )


class EventLog:
    def __init__(self) -> None:
        self._events: list[TransitionEvent] = []

    def append(self, event: TransitionEvent) -> None:
        if event.sequence != len(self._events) + 1:
            raise ValueError("event sequence must be append-only and contiguous")
        self._events.append(deepcopy(event))

    def clear(self) -> None:
        self._events.clear()

    @property
    def events(self) -> tuple[TransitionEvent, ...]:
        return tuple(deepcopy(self._events))

    def as_dicts(self) -> tuple[dict[str, Any], ...]:
        return tuple(event.to_dict() for event in self._events)
