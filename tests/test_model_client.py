"""Exercise the real adapter request construction without network access."""

import json
import sys
from types import SimpleNamespace

import pytest

from orgenv import ModelClientError, OpenAIModelClient


def adapter(monkeypatch, response_text):
    requests = []

    def create(**request):
        requests.append(request)
        # Reproduce the provider's JSON-mode guard. Checking only instructions
        # would miss the live regression when observations lack the word JSON.
        messages = request["input"]
        content = messages if isinstance(messages, str) else " ".join(m["content"] for m in messages)
        if "json" not in content.lower():
            raise ValueError("Response input messages must contain the word 'json'")
        return SimpleNamespace(output_text=response_text)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create))
    ))
    return OpenAIModelClient(api_key="test-only"), requests


def test_json_mode_request_accepts_observation_without_json_instruction(monkeypatch):
    client, requests = adapter(monkeypatch, '{"action": null, "messages": [], "queries": []}')
    observation = json.dumps({"actor_id": "priya", "inbox": [{"content": "Please approve."}]})
    assert "json" not in observation.lower()
    result = client.generate_json(model="gpt-4.1-mini", system="Return JSON only.",
                                  user=observation, temperature=0.0)
    assert result == {"action": None, "messages": [], "queries": []}
    request = requests[0]
    assert request["instructions"] == "Return JSON only."
    assert request["input"][-1] == {"role": "user", "content": observation}
    assert request["input"][0]["role"] == "developer"
    assert request["model"] == "gpt-4.1-mini" and request["temperature"] == 0.0
    assert request["text"] == {"format": {"type": "json_object"}}


@pytest.mark.parametrize("response_text", ["not JSON", "[]", "null"])
def test_adapter_still_rejects_invalid_or_non_object_outputs(monkeypatch, response_text):
    client, requests = adapter(monkeypatch, response_text)
    with pytest.raises(ModelClientError):
        client.generate_json(model="gpt-4.1-mini", system="Decide.", user="{}", temperature=0)
    assert len(requests) == 1
