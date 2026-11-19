"""Reliability of saved trials: pass^k next to the mean (tau-bench's measure).

pass^k is the share of seeds a model resolves on every one of k repeated trials, averaged
over all size-k subsets of the repeats that exist for a seed. pass^1 is the plain
resolution rate. A model that resolves a seed on some repeats only is not reliable on it.

python -m tasks.relaypay_workday.eval.reliability [RUNS_DIR]
"""
from __future__ import annotations

import json
from math import comb
from pathlib import Path
import sys


def pass_k(resolved_by_seed: dict, k: int):
    """Mean over seeds of C(successes, k) / C(trials, k); None if no seed has k trials."""
    terms = [comb(sum(runs), k) / comb(len(runs), k) for runs in resolved_by_seed.values() if len(runs) >= k]
    return sum(terms) / len(terms) if terms else None


def collect(pilot: Path):
    groups = {}
    for summary in sorted(pilot.glob("summary-*.json")):
        for row in json.loads(summary.read_text()):
            details = pilot / row["job"] / "reward-details.json"
            if not details.exists():
                continue  # an infrastructure failure is not a model outcome
            d = json.loads(details.read_text())
            g = groups.setdefault((row["model"], row["variant"]), {"resolved": {}, "rewards": [], "breaches": 0})
            g["resolved"].setdefault(row["seed"], []).append(bool(d.get("resolved")))
            g["rewards"].append(d.get("reward") or 0.0)
            g["breaches"] += any(r.startswith("policy_breach") for r in (d.get("safety_gates") or {}).get("reasons", []))
    return groups


def main():
    pilot = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "runs"
    for (model, variant), g in sorted(collect(pilot).items()):
        most = max(len(v) for v in g["resolved"].values())
        ks = " ".join(f"pass^{k}={pass_k(g['resolved'], k):.2f}" for k in range(1, most + 1))
        print(f"{model:22s} {variant:12s} seeds={len(g['resolved']):2d} trials={len(g['rewards']):3d} "
              f"mean_reward={sum(g['rewards']) / len(g['rewards']):.3f} policy_breaches={g['breaches']} {ks}")


if __name__ == "__main__":
    main()
