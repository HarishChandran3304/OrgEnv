"""Sweeps: the Oracle or a named control on many seeds and variants, in parallel.

python -m tasks.relaypay_workday.eval.sweep POLICY VARIANTS SEEDS [OUT]
  POLICY   oracle | a control name from tasks.relaypay_workday.eval.controls.CONTROLS
  SEEDS    development | frozen | 2000-2059 | comma list
"""
from __future__ import annotations

import json
from multiprocessing import Pool
from pathlib import Path
import sys

HERE = Path(__file__).parent


def one(job):
    policy, variant, seed = job
    from tasks.relaypay_workday.eval.offline import load_oracle, transport, verify
    from tasks.relaypay_workday.task import EpisodeV3
    module = load_oracle()
    if policy == "oracle":
        cls = module.AdaptiveOracle
    else:
        from tasks.relaypay_workday.eval.controls import CONTROLS
        cls = CONTROLS[policy](module)
    episode = EpisodeV3(seed, variant=variant)
    error = None
    try:
        cls(transport(episode)).run()
    except Exception as exc:  # a control failing is an outcome
        error = f"{type(exc).__name__}: {exc}"[:200]
    r = verify(episode)
    harm = r.get("harm", {})
    return {"policy": policy, "variant": variant, "seed": seed, "reward": r["reward"], "reward_graded": r.get("reward_graded"), "resolved": r["resolved"],
            "critical": r["safety_gates"]["reasons"], "harm_rate": harm.get("harm_rate"), "harm_events": harm.get("events"),
            "failed_checks": [k for k, v in r.get("checks", {}).items() if not v],
            "invoice_failures": len(r.get("invoice_failures") or {}), "tool_calls": len(episode.calls), "sim_time": episode.now,
            "cards": (r.get("scorecard") or {}).get("cards_detail"),
            "policy_error": error, "verifier_error": r.get("error")}


def seeds_of(text):
    if text in ("frozen", "development"):
        s = json.loads((HERE / "seeds.json").read_text())
        return s["development"] + (s["unseen"] if text == "frozen" else [])
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in text.split(",")]


def main():
    policy, variants, seeds = sys.argv[1], sys.argv[2].split(","), seeds_of(sys.argv[3])
    jobs = [(policy, v, s) for v in variants for s in seeds]
    with Pool(min(10, len(jobs))) as pool:  # a day's run and its verification hold under 1 GB
        rows = pool.map(one, jobs)
    for v in variants:
        sub = [r for r in rows if r["variant"] == v]
        n = len(sub)
        resolved = sum(r["resolved"] for r in sub)
        zero = sum(bool(r["critical"]) for r in sub)
        mean = sum(r["reward"] for r in sub) / n
        harm = sum((r["harm_rate"] or 0) for r in sub) / n
        print(f"{policy:28s} {v:14s} n={n:3d} resolved={resolved:3d} critical={zero:3d} mean_reward={mean:.3f} mean_graded={sum((r["reward_graded"] or 0) for r in sub) / n:.3f} mean_harm={harm:.3f} "
              f"min_reward={min(r['reward'] for r in sub):.3f} max_calls={max(r['tool_calls'] for r in sub)}")
        if sub[0].get("cards"):  # a working day: how each card went
            print("   " + "  ".join(f"{card} {sum(r['cards'][card]['solved'] for r in sub)}/{n} ({sum(r['cards'][card]['score'] for r in sub) / n:.2f})"
                                    for card in sub[0]["cards"]))
        for r in sub:
            if (policy == "oracle" and not r["resolved"]) or r["verifier_error"]:
                print("   !!", json.dumps(r)[:600])
    if len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
