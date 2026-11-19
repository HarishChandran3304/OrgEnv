"""Replay a saved rollout's tool calls, unchanged, on other variants of the same seed.

This is not a model trial: the model would act differently with different observations.
It answers one question only: does this exact action sequence still work when a vertical
is switched on? Calls the new variant rejects are skipped, as the runtime would.

python -m tasks.relaypay_workday.eval.replay_trace ROLLOUT.json SEED VARIANT[,VARIANT...]
"""
from __future__ import annotations

import json
import sys

from tasks.relaypay_workday.eval.offline import verify
from tasks.relaypay_workday.task import EpisodeV3


def replay(history, seed, variant):
    episode = EpisodeV3(seed, variant=variant)
    rejected = []
    for step in history:
        decision = step["decision"]
        if not isinstance(decision, dict) or "tool" not in decision:
            continue
        result = episode.invoke(decision["tool"], decision.get("args", {}))
        if not result["ok"] and step["result"].get("ok"):
            rejected.append((episode.now, decision["tool"], str(result.get("error") or result.get("reason") or "")[:110]))
    return episode, rejected


def main():
    history = json.loads(open(sys.argv[1]).read())["history"]
    seed = int(sys.argv[2])
    for variant in sys.argv[3].split(","):
        episode, rejected = replay(history, seed, variant)
        r = verify(episode)
        inc = episode.org.get_state()["resources"]["incident"]["state"]
        print(json.dumps({"variant": variant, "reward": round(r["reward"], 3), "reward_graded": round(r["reward_graded"], 3), "resolved": r["resolved"],
                          "breaches": r["safety_gates"]["reasons"], "harm": r["harm"]["events"],
                          "invoice_failures": len(r["invoice_failures"]), "audit_granted": inc["audit_grant"],
                          "pool_declines": inc.get("pool_declines", []), "newly_rejected": rejected[:6],
                          "violations": r.get("policy_violations")}))


if __name__ == "__main__":
    main()
