"""Run model trials through Harbor and save every rollout.

python -m tasks.midnight_billing_cascade.eval.package_harbor   # once: vendor the runtime into the Harbor task
set -a; . ./.env; set +a                                       # credentials stay in this host process only
python -m tasks.midnight_billing_cascade.eval.run_trial MODEL VARIANT SEEDS [--parallel N] [--repeat K]

Environment: ORGENV_TRIAL_EFFORT (reasoning effort), ORGENV_TRIAL_PROVIDER (unset: the model
vendor's own API; "bedrock": Amazon Bedrock; "relay": an OpenAI-compatible endpoint given by
RELAY_BASE_URL and RELAY_API_KEY), ORGENV_TRIAL_JOBS (Harbor job directory),
ORGENV_TRIAL_AGENT_TIMEOUT_MULT (wall-clock only).

--repeat K runs every seed K times (job suffix -r1..-rK) so that reliability (pass^k, see
reliability.py) can be reported next to the mean.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1] / "runs"
JOBS = Path(os.environ.get("ORGENV_TRIAL_JOBS", "/tmp/orgenv-trials"))  # set to persistent disk: /tmp is wiped on reboot
# Same patterns as agent.SECRET_RE (kept local: this runner does not import Harbor).
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|ABSK[A-Za-z0-9+/=]{16,}|bedrock-api-key-[A-Za-z0-9+/=_%\-]{16,}"
                       r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


EFFORT = os.environ.get("ORGENV_TRIAL_EFFORT")
PROVIDER = os.environ.get("ORGENV_TRIAL_PROVIDER")
# Wall-clock only: model latency never advances simulated time, so slower models may get longer.
AGENT_TIMEOUT_MULT = float(os.environ.get("ORGENV_TRIAL_AGENT_TIMEOUT_MULT", "1"))


def run(model, variant, seed, repeat=None):
    name = f"{model}-{EFFORT or 'default'}-{variant}-{seed}" + (f"-r{repeat}" if repeat else "")
    env = {**os.environ, "ORGENV_VARIANT_SEED": str(seed), "ORGENV_TASK_VARIANT": variant, "PYTHONPATH": str(ROOT)}
    cmd = ["harbor", "run", "-p", str(ROOT / "tasks/midnight_billing_cascade/harbor"),
           "--agent-import-path", "tasks.midnight_billing_cascade.eval.agent:TrialAgent", "-m", model, *(["--ak", f"effort={EFFORT}"] if EFFORT else []),  # "reasoning_effort" is a reserved Harbor agent option
           *(["--ak", f"provider={PROVIDER}"] if PROVIDER else []),
           *(["--agent-timeout-multiplier", str(AGENT_TIMEOUT_MULT)] if AGENT_TIMEOUT_MULT != 1 else []),
           "-o", str(JOBS), "--job-name", name, "-n", "1", "-r", "0", "-q"]
    try:  # outlive Harbor's own agent timeout (7200 s x multiplier) so its verifier still runs
        proc = subprocess.run(["sg", "docker", "-c", " ".join(shlex.quote(c) for c in cmd)], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=7200 * AGENT_TIMEOUT_MULT + 3600)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "runner_timeout"
    # The trial folder's name depends on the Harbor version (task name or task folder): find it by content.
    trial = next((p for p in sorted((JOBS / name).glob("*")) if p.is_dir() and (p / "result.json").exists()), None)
    dest = OUT / name
    dest.mkdir(parents=True, exist_ok=True)
    for src, target in (("agent/focal-rollout.json", "focal-rollout.json"), ("verifier/reward-details.json", "reward-details.json"),
                        ("result.json", "trial-result.json"), ("exception.txt", "exception.txt")):
        if trial and (trial / src).exists():
            shutil.copy2(trial / src, dest / target)
    for path in dest.iterdir():  # defensive: never persist a credential
        text = path.read_text(errors="replace")
        if SECRET_RE.search(text):
            path.write_text(SECRET_RE.sub("[REDACTED]", text))
    details = json.loads((dest / "reward-details.json").read_text()) if (dest / "reward-details.json").exists() else {}
    record = {"job": name, "model": model, "variant": variant, "seed": seed, "repeat": repeat, "return_code": code,
              "scorecard": details.get("scorecard"),
              "reward": details.get("reward"), "reward_graded": details.get("reward_graded"), "resolved": details.get("resolved"), "error": details.get("error")}
    print(json.dumps(record), flush=True)
    return record


def main():
    model, variant = sys.argv[1], sys.argv[2]
    seeds = [int(s) for s in sys.argv[3].split(",")]
    parallel = int(sys.argv[sys.argv.index("--parallel") + 1]) if "--parallel" in sys.argv else 3
    if PROVIDER == "bedrock":  # explicit credentials only: never fall back to a stray ~/.aws profile
        if not os.environ.get("AWS_REGION") or not (
                os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_ACCESS_KEY_ID")):
            raise SystemExit("Bedrock needs AWS_REGION and AWS_BEARER_TOKEN_BEDROCK (or AWS_ACCESS_KEY_ID) in this process")
    else:
        key = ("RELAY_API_KEY" if PROVIDER == "relay" else
               "ANTHROPIC_API_KEY" if model.startswith("claude-") else "OPENAI_API_KEY")
        if not os.environ.get(key):
            raise SystemExit("model API key is not set in this process")
    repeats = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    jobs = [(s, r if repeats > 1 else None) for s in seeds for r in range(1, repeats + 1)]
    with ThreadPoolExecutor(parallel) as pool:
        records = list(pool.map(lambda job: run(model, variant, *job), jobs))
    (OUT / f"summary-{model}-{variant}.json").write_text(json.dumps(records, indent=1))


if __name__ == "__main__":
    main()
