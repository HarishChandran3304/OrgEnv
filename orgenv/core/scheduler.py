"""Deterministic, in-memory scheduling primitives."""

from __future__ import annotations

import heapq
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import WorldState

EventPrecondition = Callable[[WorldState, "ScheduledEvent"], bool]


@dataclass(frozen=True)
class ScheduledEvent:
    event_id: str
    due_time: float
    order: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    precondition: EventPrecondition | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deepcopy(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "due_time": self.due_time,
            "order": self.order,
            "event_type": self.event_type,
            "payload": deepcopy(self.payload),
        }


class EventScheduler:
    """A stable priority queue ordered by due time, then insertion order."""

    def __init__(self) -> None:
        self._queue: list[tuple[float, int, ScheduledEvent]] = []
        self._next_order = 1

    def clear(self) -> None:
        self._queue.clear()
        self._next_order = 1

    def schedule_at(
        self,
        due_time: float,
        event_type: str,
        payload: dict[str, Any],
        precondition: EventPrecondition | None = None,
    ) -> ScheduledEvent:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event type must be a non-empty string")
        if due_time < 0:
            raise ValueError("event due time must be non-negative")
        order = self._next_order
        self._next_order += 1
        event = ScheduledEvent(f"event-{order}", float(due_time), order, event_type, payload, precondition)
        heapq.heappush(self._queue, (event.due_time, event.order, event))
        return event

    def schedule_after(
        self,
        now: float,
        delay: float,
        event_type: str,
        payload: dict[str, Any],
        precondition: EventPrecondition | None = None,
    ) -> ScheduledEvent:
        if delay < 0:
            raise ValueError("event delay must be non-negative")
        return self.schedule_at(now + float(delay), event_type, payload, precondition)

    def schedule_record(self, value: dict[str, Any], precondition: EventPrecondition | None = None) -> ScheduledEvent:
        """Restore a serialized scheduled item during replay."""
        event = ScheduledEvent(
            value["event_id"],
            float(value["due_time"]),
            int(value["order"]),
            value["event_type"],
            value.get("payload", {}),
            precondition,
        )
        heapq.heappush(self._queue, (event.due_time, event.order, event))
        self._next_order = max(self._next_order, event.order + 1)
        return event

    def remove(self, event_id: str) -> bool:
        for index, (_, _, event) in enumerate(self._queue):
            if event.event_id == event_id:
                self._queue.pop(index)
                heapq.heapify(self._queue)
                return True
        return False

    def pop_due(self, now: float) -> ScheduledEvent | None:
        if self._queue and self._queue[0][0] <= now:
            return heapq.heappop(self._queue)[2]
        return None

    def has_due(self, now: float) -> bool:
        return bool(self._queue and self._queue[0][0] <= now)

    def next_due_time(self) -> float | None:
        return self._queue[0][0] if self._queue else None

    def pending(self) -> tuple[dict[str, Any], ...]:
        return tuple(event.to_dict() for _, _, event in sorted(self._queue))

    def snapshot(self) -> tuple[list[tuple[float, int, ScheduledEvent]], int]:
        return deepcopy(self._queue), self._next_order

    def restore(self, snapshot: tuple[list[tuple[float, int, ScheduledEvent]], int]) -> None:
        self._queue, self._next_order = deepcopy(snapshot)
