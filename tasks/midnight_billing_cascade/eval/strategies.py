"""Controlled, fully authorized strategy comparisons.

Each strategy is the reference workflow with exactly one decision changed, so
reward and outcome differences are attributable to that decision.
Run: python -m tasks.midnight_billing_cascade.eval.strategies SEEDS VARIANTS OUT
"""
from __future__ import annotations

import json
import sys

from tasks.midnight_billing_cascade.task import EpisodeV3
from tasks.midnight_billing_cascade.eval.offline import load_oracle, transport, verify

oracle = load_oracle()
Base = oracle.AdaptiveOracle


class WaitForDaniel(Base):
    def choose_route(self, *args):
        return "daniel"


class EmergencyWhenever(Base):
    def choose_route(self, earliest_lapse, daniel_route, daniel_unavailable):
        return "emergency" if daniel_unavailable else "daniel"


class EmergencyNoFinalPass(EmergencyWhenever):
    final_pass = False


class PrimaryOnlyContainment(Base):
    contain_secondary = False   # the on-call kill switch only


class LateSecondaryContainment(Base):
    """Pauses the primary at once; contains the secondary pool only after investigating (CI run)."""
    contain_secondary = False

    def run(self):
        original = self.repair_files

        def repair_then_contain(files):
            out = original(files)
            avail = self.observe()["resources"]["incident"]["state"].get("availability", {})
            self.contain_secondary = True
            self.call("request_emergency" if float(avail.get("maya", 0)) > self.now(self.observe()) + 1 else "request_pool_change",
                      {"kind": "stop_billing"} if float(avail.get("maya", 0)) > self.now(self.observe()) + 1 else {"pool": "secondary", "mode": "paused"})
            return out
        self.repair_files = repair_then_contain
        return super().run()


class NoSupportHold(Base):
    support_hold = False


class NoSupportCoordination(Base):
    support_hold = False
    support_advice = False


class NoHoldStalePlans(Base):
    """No hold, and never re-reads support's refund log (plans built on the last provider export)."""
    support_hold = False

    def read_support(self, catalog):
        return set()


class TrustSkewedJournal(Base):
    trust_skewed_journal = True


class LateContainment(Base):
    contain_first = False      # fixes code first; billing keeps running during CI


class QuarantineBridgeWait(WaitForDaniel):
    use_quarantine = True      # run both pools under Maya's quarantine while waiting for Daniel


STRATEGIES = {"adaptive_reference": Base, "wait_for_daniel": WaitForDaniel, "emergency_whenever": EmergencyWhenever,
              "emergency_no_final_pass": EmergencyNoFinalPass, "primary_only_containment": PrimaryOnlyContainment,
              "late_secondary_containment": LateSecondaryContainment, "no_support_hold": NoSupportHold, "no_support_coordination": NoSupportCoordination,
              "no_hold_stale_plans": NoHoldStalePlans, "trust_skewed_journal": TrustSkewedJournal,
              "late_containment": LateContainment, "quarantine_bridge_wait": QuarantineBridgeWait}


def run(strategy, seed, variant):
    episode = EpisodeV3(seed, variant=variant)
    policy = STRATEGIES[strategy](transport(episode))
    error = None
    try:
        policy.run()
    except Exception as exc:  # strategy failures are outcomes, not crashes
        error = f"{type(exc).__name__}: {exc}"[:300]
    r = verify(episode)
    return {"strategy": strategy, "seed": seed, "variant": variant, "reward": r["reward"], "resolved": r["resolved"],
            "harm_rate": r.get("harm", {}).get("harm_rate"), "harm_events": r.get("harm", {}).get("events"),
            "sim_time": episode.now, "completion_time": r.get("metrics", {}).get("completion_time"),
            "tool_calls": len(episode.calls), "messages_sent": r.get("metrics", {}).get("messages_sent_by_alex"),
            "failed_checks": [k for k, v in r.get("checks", {}).items() if not v], "invoice_failures": r.get("invoice_failures"),
            "decisions": [d for d in policy.decisions if d["decision"] in ("route", "resume_under_quarantine")],
            "policy_error": error, "verifier_error": r.get("error")}


def main():
    seeds = [int(s) for s in sys.argv[1].split(",")]
    variants = sys.argv[2].split(",")
    rows = [run(name, seed, variant) for variant in variants for seed in seeds for name in STRATEGIES]
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w") as fh:
            json.dump(rows, fh, indent=1)
    for r in rows:
        print(f"{r['variant']:14s} {r['seed']:>11} {r['strategy']:24s} reward={r['reward']:<7.4g} resolved={str(r['resolved']):5s} "
              f"harm={r['harm_rate'] if r['harm_rate'] is not None else '-':<6.3} t={r['sim_time']:<5} calls={r['tool_calls']:<3} "
              f"{r['harm_events'] or ''} {r['failed_checks'] or ''} {r['policy_error'] or ''}")


if __name__ == "__main__":
    main()
