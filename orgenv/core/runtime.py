"""Context supplied to handlers that use Commit 2 runtime features."""

from __future__ import annotations

from typing import Any, Callable

from .randomness import RandomSource
from .scheduler import EventPrecondition, ScheduledEvent


class ExecutionContext:
    def __init__(
        self,
        now: float,
        random_source: RandomSource,
        schedule_fn: Callable[[float, str, dict[str, Any], EventPrecondition | None], ScheduledEvent],
    ) -> None:
        self.now = now
        self.random = random_source
        self._schedule_fn = schedule_fn
        self.scheduled_events: list[dict[str, Any]] = []

    def schedule(
        self,
        event_type: str,
        payload: dict[str, Any],
        delay: float = 0.0,
        precondition: EventPrecondition | None = None,
    ) -> str:
        event = self._schedule_fn(delay, event_type, payload, precondition)
        self.scheduled_events.append(event.to_dict())
        return event.event_id
