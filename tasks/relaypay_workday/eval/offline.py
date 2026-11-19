"""Offline drivers: run the Harbor Oracle (or a scripted policy) against EpisodeV3 in-process."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tasks.relaypay_workday.task import EpisodeV3
from tasks.relaypay_workday.verifier import seal, verify_envelope

ROOT = Path(__file__).resolve().parents[3]
KEY = b"v3-offline"


def load_oracle():
    spec = importlib.util.spec_from_file_location("oracle", ROOT / "tasks/relaypay_workday/harbor/solution/oracle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transport(episode):
    def request(path, data=None):
        if path == "/catalog":
            return episode.catalog()
        if path == "/observe":
            return episode.observe()
        if path == "/tool":
            return json.loads(json.dumps(episode.invoke(data["tool"], data["args"])))
        raise ValueError(path)
    return request


def verify(episode):
    return verify_envelope(seal(episode.evidence(), KEY), KEY)


def run_oracle(seed, variant="dynamic", log=None):
    module = load_oracle()
    episode = EpisodeV3(seed, variant=variant)
    oracle = module.AdaptiveOracle(transport(episode), log=log)
    error = None
    try:
        oracle.run()
    except module.OracleError as exc:
        error = str(exc)
    return episode, oracle, error
