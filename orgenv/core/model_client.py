"""Minimal model-client boundary used by :class:`LLMActorPolicy`."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping, Protocol


class ModelClientError(RuntimeError):
    """A model request failed or returned unusable data."""


class ModelClient(Protocol):
    def generate_json(self, *, model: str, system: str, user: str, temperature: float) -> Mapping[str, Any] | str: ...


class FakeModelClient:
    """Deterministic client for offline tests and examples."""

    def __init__(self, responses: list[Mapping[str, Any] | str] | Mapping[str, Any] | str) -> None:
        if isinstance(responses, list):
            self._responses = [deepcopy(response) for response in responses]
        else:
            self._responses = [deepcopy(responses)]
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, *, model: str, system: str, user: str, temperature: float) -> Mapping[str, Any] | str:
        self.calls.append({"model": model, "system": system, "user": user, "temperature": temperature})
        if not self._responses:
            raise ModelClientError("fake model has no response left")
        return deepcopy(self._responses.pop(0))


class OpenAIModelClient:
    """Small OpenAI SDK adapter; the SDK is imported only when instantiated."""

    def __init__(self, api_key: str | None = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional integration
            raise ModelClientError("install the optional 'openai' package to use OpenAIModelClient") from exc
        self._client = OpenAI(api_key=api_key)

    def generate_json(self, *, model: str, system: str, user: str, temperature: float) -> Mapping[str, Any] | str:
        try:
            response = self._client.responses.create(
                model=model,
                temperature=temperature,
                instructions=system,
                # JSON mode checks the input messages for an explicit JSON
                # instruction; the separate `instructions` field is not enough.
                # Keep the actor observation unchanged and at user priority.
                input=[
                    {"role": "developer", "content": "Return a JSON object."},
                    {"role": "user", "content": user},
                ],
                text={"format": {"type": "json_object"}},
            )
            text = response.output_text
            parsed = json.loads(text)
            if not isinstance(parsed, Mapping):
                raise ModelClientError("model JSON response must be an object")
            return parsed
        except ModelClientError:
            raise
        except Exception as exc:
            raise ModelClientError(f"model request failed: {exc}") from exc
