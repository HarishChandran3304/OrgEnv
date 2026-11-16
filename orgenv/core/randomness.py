"""Seeded mechanism-isolated random streams."""

from __future__ import annotations

import hashlib
import random
from typing import Any


class RandomSource:
    def __init__(self, manager: "RandomManager", samples: list[dict[str, Any]]) -> None:
        self._manager = manager
        self._samples = samples

    def uniform(self, mechanism: str, low: float, high: float) -> float:
        value = self._manager._stream(mechanism).uniform(low, high)
        self._samples.append({"mechanism": mechanism, "method": "uniform", "args": [low, high], "value": value})
        return value

    def integer(self, mechanism: str, low: int, high: int) -> int:
        value = self._manager._stream(mechanism).randint(low, high)
        self._samples.append({"mechanism": mechanism, "method": "integer", "args": [low, high], "value": value})
        return value


class RandomManager:
    """Independent streams are derived from the root seed and mechanism name."""

    def __init__(self) -> None:
        self._seed: int = 0
        self._streams: dict[str, random.Random] = {}

    def reset(self, seed: int | None) -> None:
        self._seed = 0 if seed is None else int(seed)
        self._streams.clear()

    def _stream(self, mechanism: str) -> random.Random:
        if not isinstance(mechanism, str) or not mechanism.strip():
            raise ValueError("random mechanism must be a non-empty string")
        if mechanism not in self._streams:
            digest = hashlib.sha256(f"{self._seed}:{mechanism}".encode("utf-8")).digest()
            self._streams[mechanism] = random.Random(int.from_bytes(digest[:16], "big"))
        return self._streams[mechanism]

    def source(self, samples: list[dict[str, Any]]) -> RandomSource:
        return RandomSource(self, samples)

    @property
    def seed(self) -> int:
        return self._seed

