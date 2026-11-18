"""Turn a saved trial folder into the published form under traces/.

python -m tasks.midnight_billing_cascade.eval.export_trace RUN_DIR OUT_DIR

RUN_DIR holds focal-rollout.json and reward-details.json (written by run_trial).
OUT_DIR receives:
  trace.json       one record per model call: reasoning summary, tool call, result, token usage
  reward.json      the verifier's full output
  rollout.json.gz  the complete record, including the exact prompt of every call

Every model call resends the whole history, so the complete record is tens of megabytes of
repeated text; trace.json keeps each piece once.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

JOB = re.compile(r"(?P<model>.+?)-(?P<effort>low|medium|high|xhigh|max|default)-(?P<variant>[a-z]+)-(?P<seed>\d+)(?:-r\d+)?$")


def reasoning(metadata):
    found = (metadata or {}).get("reasoning") or {}
    summary = found.get("summary") or []
    return [s if isinstance(s, str) else s.get("text", "") for s in summary] if isinstance(summary, list) else [str(summary)]


def export(run: Path, out: Path):
    rollout = json.loads((run / "focal-rollout.json").read_text())
    reward = json.loads((run / "reward-details.json").read_text())
    job = JOB.match(run.name)
    steps, results = [], iter(rollout["history"])
    for n, call in enumerate(rollout["decisions"]):
        meta = call.get("metadata") or {}
        usage = meta.get("usage") or {}
        step = {"call": n, "reasoning_summary": reasoning(meta), "response": call.get("response"),
                "validation": call.get("validation"),
                "usage": {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                    "cache_creation_input_tokens") if k in usage}}
        if call.get("validation") in ("valid", "tool_rejected", "invalid_decision"):
            step["result"] = next(results)["result"]
        elif "error_type" in call:
            step["error_type"] = call["error_type"]
        steps.append(step)
    first = rollout["decisions"][0]
    prompt = [json.loads(line) for line in first["request"]["user"].split("\n")]
    if len(prompt) == 1:  # runs recorded before the line-per-part layout: one JSON object
        head, observation = prompt[0], prompt[0]["observation"]
    else:
        head, observation = prompt[0], prompt[-1]["observation"]
    settings = {k: v for k, v in (first.get("metadata") or {}).get("settings", {}).items()}
    trace = {"model": rollout["model"], "variant": job and job["variant"], "seed": job and int(job["seed"]),
             "reward": reward.get("reward"), "resolved": reward.get("resolved"),
             "stop_reason": rollout["stop_reason"], "totals": rollout["totals"], "settings": settings,
             "system_prompt": first["request"]["system"], "instruction": rollout["instruction"],
             "first_observation": observation, "tool_catalog": head["catalog"], "steps": steps}
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace.json").write_text(json.dumps(trace, indent=1))
    (out / "reward.json").write_text(json.dumps(reward, indent=1))
    with gzip.GzipFile(out / "rollout.json.gz", "wb", mtime=0) as f:
        f.write(json.dumps(rollout).encode())
    print(f"{run.name}: {len(steps)} model calls, reward {reward.get('reward')}, resolved {reward.get('resolved')}")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
