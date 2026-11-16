"""Monotonic simulated time."""

from __future__ import annotations

import math


class SimulationClock:
    """A clock advanced explicitly by the simulation, never by wall time."""

    def __init__(self, start: float = 0.0) -> None:
        if not math.isfinite(start):
            raise ValueError("clock start must be finite")
        self._time = float(start)

    @property
    def now(self) -> float:
        return self._time

    def reset(self, start: float = 0.0) -> None:
        if not math.isfinite(start):
            raise ValueError("clock start must be finite")
        self._time = float(start)

    def advance(self, amount: float) -> float:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("time advance must be a non-negative number")
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("time advance must be a non-negative finite number")
        self._time += float(amount)
        return self._time

    def advance_to(self, target: float) -> float:
        if not math.isfinite(target) or target < self._time:
            raise ValueError("clock target must be finite and monotonic")
        self._time = float(target)
        return self._time

    def set_for_replay(self, value: float) -> None:
        """Set time while reconstructing a previously recorded run."""
        if not math.isfinite(value) or value < self._time:
            raise ValueError("replay time must be finite and monotonic")
        self._time = float(value)
