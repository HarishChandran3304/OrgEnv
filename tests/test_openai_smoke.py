"""Opt-in real-model smoke test.

Run explicitly with ``ORGENV_RUN_LLM_SMOKE=1 OPENAI_API_KEY=... pytest ...``.
"""

from __future__ import annotations

import os

import pytest

from orgenv import ActorProfile, LLMActorPolicy, OpenAIModelClient


@pytest.mark.skipif(
    not (os.getenv("ORGENV_RUN_LLM_SMOKE") == "1" and os.getenv("OPENAI_API_KEY")),
    reason="real OpenAI smoke test is opt-in",
)
def test_openai_model_client_returns_a_structured_decision() -> None:  # pragma: no cover - external API
    policy = LLMActorPolicy(
        actor_id="smoke",
        profile=ActorProfile("tester", behavioral_instructions="Take no action."),
        client=OpenAIModelClient(),
        model=os.getenv("ORGENV_LLM_MODEL", "gpt-4.1-mini"),
    )
    decision = policy.decide({"public": {"instruction": "Take no action."}, "inbox": []})
    assert decision.action is None
