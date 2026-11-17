"""Model clients for trials: bounded requests, usage reporting and per-episode call records."""
from __future__ import annotations

from copy import deepcopy

from orgenv import ModelClientError, OpenAIModelClient


class TrialOpenAIClient(OpenAIModelClient):
    """Existing SDK/client boundary with bounded requests and usage reporting.

    No server-side tools, conversation IDs, retries, fallback models or previous
    thread history. Each call receives only the supplied actor-specific input.
    """

    def __init__(self, api_key=None, *, max_output_tokens=4096):
        super().__init__(api_key=api_key)
        self._client = self._client.with_options(timeout=45, max_retries=0)
        self.max_output_tokens = max_output_tokens
        self.last_metadata = None

    def generate_json(self, *, model, system, user, temperature):
        settings = {"model": model, "max_output_tokens": self.max_output_tokens,
                    "text": {"format": {"type": "json_object"}}, "store": False}
        # The existing older-model configuration accepts temperature. For newer
        # reasoning models leave generation controls at provider defaults; never
        # retry with a different model or silently change parameters on failure.
        if model.startswith("gpt-4"):
            settings["temperature"] = temperature
        self.last_metadata = {"settings": deepcopy(settings), "usage": None, "cost_usd": None,
                              "cost_note": "Responses usage is not an invoice; no pricing estimate configured."}
        try:
            response = self._client.responses.create(
                **settings, instructions=system,
                input=[{"role": "developer", "content": "Return a JSON object."},
                       {"role": "user", "content": user}],
            )
            self.last_metadata.update(
                response_id=response.id, returned_model=response.model, status=response.status,
                usage=response.usage.model_dump(mode="json") if response.usage else None,
                reasoning=response.reasoning.model_dump(mode="json") if response.reasoning else None,
            )
            if response.status != "completed":
                raise ModelClientError("model response did not complete")
            # Parsing belongs to the actor adapter, so malformed outputs remain
            # visible as decision-validation errors rather than API errors.
            return response.output_text
        except ModelClientError:
            raise
        except Exception as exc:
            self.last_metadata["error_type"] = type(exc).__name__
            raise ModelClientError("model API request failed; see recorded error type") from None

    def close(self):
        self._client.close()


class RecordingClient:
    """Per-episode, per-actor hard call cap and operator-only model records."""

    def __init__(self, delegate, limit):
        self.delegate = delegate
        self.limit = limit
        self.calls = []
        self.blocked_calls = 0

    def generate_json(self, **request):
        if len(self.calls) >= self.limit:
            self.blocked_calls += 1
            raise ModelClientError("episode model-call budget exhausted")
        record = {"request": deepcopy(request)}
        self.calls.append(record)
        try:
            raw = self.delegate.generate_json(**request)
            record["response"] = deepcopy(raw)
            return raw
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            # Provider exception text can contain credentials or request data.
            raise ModelClientError("model call failed; inspect operator error_type") from None
        finally:
            record["metadata"] = deepcopy(getattr(self.delegate, "last_metadata", None))

    def totals(self):
        usages = [c.get("metadata", {}).get("usage") for c in self.calls if c.get("metadata")]
        known = [u for u in usages if u is not None]
        return {"calls": len(self.calls), "blocked_calls": self.blocked_calls,
                "usage_reported_calls": len(known),
                "input_tokens": sum(u["input_tokens"] for u in known) if known else None,
                "output_tokens": sum(u["output_tokens"] for u in known) if known else None,
                "total_tokens": sum(u["total_tokens"] for u in known) if known else None,
                # Of input_tokens: read from the provider's prompt cache, and written to it (where reported).
                "cache_read_input_tokens": sum(u.get("cache_read_input_tokens") or 0 for u in known) if known else None,
                "cache_creation_input_tokens": sum(u.get("cache_creation_input_tokens") or 0 for u in known) if known else None,
                "cost_usd": None}
