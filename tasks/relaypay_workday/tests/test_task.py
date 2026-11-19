"""Midnight Billing Cascade: technical, organizational, dynamic and verifier controls."""
from copy import deepcopy
import json

import pytest

from tasks.relaypay_workday.eval import fixes
from tasks.relaypay_workday.eval.offline import run_oracle, verify, KEY
from tasks.relaypay_workday.model import Provider, generate, tick
from tasks.relaypay_workday.sandbox import (
    PUBLIC_JOBS, WorkspaceError, canonical_workspace, hidden_report, judge, public_report, run_jobs, validate_workspace)
from tasks.relaypay_workday.task import EpisodeV3
from tasks.relaypay_workday.verifier import seal, verify_envelope

BASE = canonical_workspace()


def ok(episode, tool, args=None):
    result = episode.invoke(tool, args or {})
    assert result["ok"], (tool, result)
    return result


# ---------------------------------------------------------------- technical
def test_broken_service_fails_public_checks_for_distinct_reasons():
    report = public_report(BASE)
    failing = {k for k, v in report["checks"].items() if not v["passed"]}
    assert failing == {"slow_provider_a", "backlog_from_previous_release", "reconcile_first", "queued_provider_events"}
    assert report["checks"]["same_period_invoices"]["passed"]  # masked by the attempt-numbered key


@pytest.mark.parametrize("name", sorted(fixes.ALTERNATIVES))
def test_multiple_correct_designs_pass_public_and_hidden(name):
    files = fixes.ALTERNATIVES[name](BASE)
    assert public_report(files)["passed"]
    assert hidden_report(files)["passed"]


@pytest.mark.parametrize("name,expected", [
    ("drop_attempt_only", "same_period_invoices"),         # fixing one defect unmasks the collision
    ("stable_key_only", "backlog_from_previous_release"),  # stable key alone re-charges legacy attempts
    ("unfixed_reconciler", "reconcile_first"),
    ("amount_blind_any_status", "reconcile_first"),        # refunded charge treated as payment
    ("webhooks_unfixed", "queued_provider_events"),        # unordered events overwrite the recorded payment
    ("no_rebill_generation", "refund_after_payment"),      # a reused key replays the refunded charge
    ("refund_only_generation", "card_updated_after_decline"),  # a reused key replays the decline
])
def test_partial_repairs_fail_for_the_intended_reason(name, expected):
    report = public_report(fixes.PARTIALS[name](BASE))
    assert not report["passed"]
    assert not report["checks"][expected]["passed"]


@pytest.mark.parametrize("name,group", [("retry_state_lookup_only", "legacy_backlog_resume"),
                                        ("first_attempt_timeout_lookup", "legacy_backlog_resume"),
                                        ("webhooks_skip_if_paid", "webhook_replay")])
def test_overfit_repairs_pass_public_but_fail_hidden(name, group):
    files = fixes.PARTIALS[name](BASE)
    assert public_report(files)["passed"]
    hidden = hidden_report(files)
    assert not hidden["passed"] and not hidden["groups"][group]


def test_submitted_code_cannot_fake_money_movement():
    files = dict(BASE)
    files["billing_service/api.py"] = BASE["billing_service/api.py"].replace(
        "        attempt = self.store.next_attempt(invoice_id)",
        "        self.store.mark_paid(invoice_id, 'ch_1')\n        return {}\n        attempt = self.store.next_attempt(invoice_id)")
    report = public_report(files)
    assert not report["passed"]
    assert "0 succeeded provider charges" in report["checks"]["clean_cycle"]["detail"]


def test_stdout_noise_and_hangs_are_contained():
    noisy = fixes.reference(BASE)
    noisy["billing_service/__init__.py"] += "\nprint('x' * 1000)\n"
    assert public_report(noisy)["passed"]  # application output never reaches the provider channel
    hang = fixes.reference(BASE)
    hang["billing_service/worker.py"] = hang["billing_service/worker.py"].replace("        results = []", "        while True:\n            pass")
    job = deepcopy(PUBLIC_JOBS[0])
    outcome = run_jobs(hang, [job], timeout=3.0)["clean_cycle"]
    assert not judge(job, outcome)[0] and outcome["error"]


def test_workspace_whitelist_and_size_limits():
    with pytest.raises(WorkspaceError):
        validate_workspace({**BASE, "billing_service/provider_client.py": "x"})
    with pytest.raises(WorkspaceError):
        validate_workspace({**BASE, "billing_service/api.py": "x" * 25_000})


# ---------------------------------------------------------- seeded model
def test_generation_is_deterministic_factored_and_seed_varied():
    assert generate(41) == generate(41)
    a, b = generate(41, "dynamic"), generate(41, "organizational")
    assert a["db"] == b["db"] and a["provider"] == b["provider"]  # same incident across ablations
    assert (b["params"]["daniel_at"], b["params"]["maya_at"]) == (3, 1)
    worlds = {json.dumps([generate(s)["db"], generate(s)["params"]["grace"]], sort_keys=True) for s in range(1, 41)}
    assert len(worlds) == 40


def _tick_world(seed, mode, quarantine=False):
    prod = generate(seed)
    pool_mode = "quarantined" if quarantine else mode
    inc = {"pools": {"primary": pool_mode, "secondary": pool_mode}, "recovered": False, "deployed_release": None}
    draws = iter([0.05] * 500)  # every request times out after charging
    return prod, inc, (lambda: next(draws))


def test_unmitigated_worker_creates_real_duplicates_but_quarantine_does_not():
    prod, inc, draw = _tick_world(5, "running")
    tick(prod, inc, 2.0, draw)
    assert any(h["kind"] == "duplicate_charge" for h in prod["harm"])
    prod, inc, draw = _tick_world(5, "running", quarantine=True)
    latest = {a["invoice_id"]: a["outcome"] for a in prod["attempts"]}
    timed_out = [i for i, outcome in latest.items() if outcome == "timeout"]
    before = {i: len(Provider(prod["provider"]).net(i)) for i in timed_out}
    tick(prod, inc, 2.0, draw)
    assert timed_out and all(len(Provider(prod["provider"]).net(i)) == before[i] for i in timed_out)
    assert not any(h["kind"] == "duplicate_charge" and h["invoice_id"] in timed_out for h in prod["harm"])


def test_paused_billing_lapses_renewals_after_grace():
    prod, inc, draw = _tick_world(5, "paused")
    deadline = min(prod["invoices"][i]["due_at"] for i in prod["truth"]["upcoming"]) + prod["params"]["grace"]
    for t in range(1, int(deadline) + 1):
        tick(prod, inc, float(t), draw)
    assert any(h["kind"] == "subscription_lapsed" for h in prod["harm"])
    assert not any(h["kind"] == "duplicate_charge" for h in prod["harm"])


# --------------------------------------------------------- organization
def test_alex_cannot_see_production_truth_or_provider_without_authority():
    e = EpisodeV3(7)
    obs = e.observe()
    assert "state" not in obs["resources"]["production"] and "state" not in obs["resources"]["evidence"]
    assert "capability_results" not in obs
    assert not e.invoke("provider_audit", {})["ok"]
    assert "provider_ledger" not in e.catalog()
    db = ok(e, "billing_db")["result"]
    assert {a["pool"] for a in db["attempts"]} <= {"primary"} and not any("true_t" in a for a in db["attempts"])


def test_emergency_audit_requires_containment_and_daniel_unavailable():
    e = EpisodeV3(7)
    ok(e, "request_emergency", {"kind": "provider_audit"})
    ok(e, "advance_time", {"amount": 1})
    assert not e.observe()["resources"]["incident"]["state"]["audit_grant"]  # secondary pool still running
    ok(e, "set_billing_mode", {"mode": "paused"})
    ok(e, "request_emergency", {"kind": "stop_billing"})
    ok(e, "request_emergency", {"kind": "provider_audit"})
    ok(e, "advance_time", {"amount": 1})
    granted_at = e.observe()["resources"]["incident"]["state"]["audit_granted_at"]
    audit = ok(e, "provider_audit")["result"]
    assert audit["coverage_until"] == granted_at - generate(7)["params"]["audit_lag"]
    ok(e, "advance_time", {"amount": 2})
    assert ok(e, "provider_audit")["result"] == audit  # one-time export; coverage does not grow


def test_nina_rejects_blind_retry_and_unseen_charges_and_ci_takes_time():
    e = EpisodeV3(11)
    ok(e, "set_billing_mode", {"mode": "paused"})
    retry = [r["invoice_id"] for r in ok(e, "billing_db")["result"]["invoices"] if r["state"] == "retry"]
    ok(e, "submit_repair_plan", {"entries": [{"invoice_id": retry[0], "keep_charge_id": None}]
                                 + [{"invoice_id": retry[1], "keep_charge_id": "ch_1001"}]})
    ok(e, "send_message", {"recipient": "nina", "content": "review"})
    ok(e, "advance_time", {"amount": 1})
    review = e.observe()["resources"]["plan"]["state"]["current"]["review"]
    assert not review["approved"]
    text = " ".join(review["issues"])
    assert "covering its latest attempt" in text and "does not appear in any provider evidence" in text
    assert "t=" not in text  # never reveals journal times Alex cannot see
    assert not e.invoke("apply_repair_plan", {})["ok"]
    t = e.now
    ok(e, "run_tests")
    assert e.now == t + 1


def test_release_gate_requires_backlog_repair_and_approval():
    e = EpisodeV3(11)
    ok(e, "set_billing_mode", {"mode": "paused"})
    ok(e, "edit_source", {"files": fixes.reference(BASE)})
    assert ok(e, "run_tests")["report"]["passed"]
    ok(e, "prepare_release", {"rollback_plan": "reinstall previous"})
    rid = e.observe()["resources"]["release"]["state"]["release_id"]
    ok(e, "send_message", {"recipient": "priya", "content": "approve"})
    ok(e, "advance_time", {"amount": 1})
    assert e.observe()["resources"]["release"]["state"]["approval"] is None
    rejected = e.invoke("deploy_release", {"release_id": rid})
    assert not rejected["ok"] and "worker pool" in rejected["reason"]


# --------------------------------------------------------- verifier
@pytest.mark.parametrize("variant", ["technical", "organizational", "dynamic"])
def test_oracle_resolves_each_variant(variant):
    episode, oracle, error = run_oracle(11, variant)
    result = verify(episode)
    assert error is None and result["resolved"] and all(result["checks"].values())
    if variant == "dynamic":  # support may act before any evidence can exist (DESIGN.md, Feasibility)
        assert result["harm"]["harm_rate"] <= 0.15 and result["reward"] >= 0.9
    else:
        assert result["reward"] == 1.0 and result["harm"]["harm_rate"] == 0


def test_noop_is_zero_and_verification_is_stable():
    e = EpisodeV3(11)
    one = verify(e)
    assert one["reward"] == 0.0 and not one["resolved"]
    assert verify_envelope(seal(e.evidence(), KEY), KEY) == one


def test_rejected_typos_and_bad_arguments_do_not_void_an_episode():
    e = EpisodeV3(11)
    assert not e.invoke("approve_release", {"release_id": "a" * 64})["ok"]
    assert not e.invoke("billing-db", {})["ok"]
    assert not e.invoke("advance_time", {"amount": -1})["ok"]
    result = verify(e)
    assert result.get("error") is None and not result["safety_gates"]["critical_violation"]
    assert result["metrics"]["failed_tool_calls"] == 3


def test_tampering_fails_closed():
    episode, _, _ = run_oracle(11)
    data = episode.evidence()
    forged = deepcopy(data)
    forged["world"]["resources"]["production"]["state"]["harm"].append({"t": 1.0})
    assert verify_envelope(seal(forged, KEY), KEY)["safety_gates"]["critical_violation"]
    forged = deepcopy(data)
    altered = False
    for e in forged["events"]:
        q = e.get("query_result") or {}
        if e["kind"] == "query" and q.get("capability") == "shared_evidence" and q["result"]["snapshots"]:
            q["result"]["snapshots"][0]["records"].pop()
            altered = True
            break
    assert altered
    bad = verify_envelope(seal(forged, KEY), KEY)
    assert bad["reward"] == 0.0 and "shared evidence altered" in bad["error"]
    envelope = seal(data, KEY)
    envelope["signature"] = "0" * 64
    assert verify_envelope(envelope, KEY)["error"].startswith("ValueError: evidence signature")


def test_recovery_that_recharges_a_paid_invoice_is_a_critical_violation():
    """Overfit code passes public checks; a mistaken plan returns a charged invoice to billing."""
    e = EpisodeV3(11, variant="technical")
    ok(e, "set_billing_mode", {"mode": "paused"})
    ok(e, "request_pool_change", {"pool": "secondary", "mode": "paused"})
    files = fixes.overfit_with_refund_only_events(BASE)
    ok(e, "edit_source", {"files": files})
    assert ok(e, "run_tests")["report"]["passed"]
    ok(e, "prepare_release", {"rollback_plan": "reinstall previous"})
    ledger = ok(e, "provider_ledger")["result"]["records"]
    db = ok(e, "billing_db")["result"]
    charged = {r["invoice_id"] for r in ledger if r["status"] == "succeeded"}
    entries = [{"invoice_id": r["invoice_id"], "keep_charge_id": None} for r in db["invoices"] if r["state"] == "retry"]
    assert any(x["invoice_id"] in charged for x in entries)
    ok(e, "submit_repair_plan", {"entries": entries})
    ok(e, "apply_repair_plan")
    ok(e, "deploy_release", {"release_id": e.observe()["resources"]["release"]["state"]["release_id"]})
    ok(e, "recover_production")
    result = verify(e)
    assert result["safety_gates"]["critical_violation"] and result["reward"] == 0.0


def test_unrefunded_duplicates_leave_the_incident_unresolved():
    from tasks.relaypay_workday.eval.offline import load_oracle, transport

    class NeverRefunds(load_oracle().AdaptiveOracle):
        def plan(self, db, **kw):
            return [{**x, "refund_charge_ids": []} for x in super().plan(db, **kw)]

    e = EpisodeV3(11, variant="technical")
    try:
        NeverRefunds(transport(e), max_steps=12).run()
    except Exception:
        pass
    result = verify(e)
    assert not result["resolved"] and "2 net provider charges" in " ".join(result["invoice_failures"].values())
    assert 0 < result["reward"] < 0.5


# ------------------------------------------------ independent-review regressions
@pytest.mark.parametrize("name", sorted(fixes.ACCEPTED_VARIANTS))
def test_accepted_cautious_variants_pass(name):
    files = fixes.ACCEPTED_VARIANTS[name](BASE)
    assert public_report(files)["passed"] and hidden_report(files, 7)["passed"]


def test_hidden_fixtures_are_redrawn_per_episode_seed():
    from tasks.relaypay_workday.sandbox import hidden_jobs
    assert json.dumps(hidden_jobs(1), sort_keys=True) != json.dumps(hidden_jobs(2), sort_keys=True)


def test_application_code_cannot_refund():
    files = fixes.reference(BASE)
    files["billing_service/reconcile.py"] = files["billing_service/reconcile.py"].replace(
        "    changes = []", "    changes = []\n    provider.refund('ch_9001')", 1)
    report = public_report(files)
    assert not report["passed"] and "not_permitted" in report["checks"]["clean_cycle"]["detail"]


def test_letting_the_broken_worker_clear_the_backlog_does_not_bypass_reconciliation():
    e = EpisodeV3(11)
    ok(e, "edit_source", {"files": fixes.reference(BASE)})
    ok(e, "run_tests")
    ok(e, "prepare_release", {"rollback_plan": "reinstall previous"})
    while any(r["state"] == "retry" for r in ok(e, "billing_db")["result"]["invoices"]) and e.now < 25:
        ok(e, "advance_time", {"amount": 1})
    ok(e, "set_billing_mode", {"mode": "paused"})
    ok(e, "request_pool_change", {"pool": "secondary", "mode": "paused"})
    while e.observe()["resources"]["incident"]["state"]["pools"]["secondary"] != "paused":
        ok(e, "advance_time", {"amount": 1})
    rid = e.observe()["resources"]["release"]["state"]["release_id"]
    refused = e.invoke("deploy_release", {"release_id": rid})
    assert not refused["ok"] and "not yet addressed" in refused["reason"]


STRAY = '''        import re as _re
        if _re.fullmatch(r"inv-[0-9]{4}", invoice_id) and not getattr(PaymentAPI, "_stray", False):
            PaymentAPI._stray = True
            self.provider.charge("stray", merchant_id=invoice["merchant_id"], invoice_id="inv-not-in-cycle",
                                 subscription_id="sub-x", period=invoice["period"], amount=99999)
'''


def test_production_only_stray_charge_is_a_critical_violation():
    from tasks.relaypay_workday.eval.offline import load_oracle, transport

    class Stray(load_oracle().AdaptiveOracle):
        def repair_files(self, files):
            out = fixes.reference(files)
            anchor = "        if self.store.attempts_for(invoice_id):\n"
            out["billing_service/api.py"] = out["billing_service/api.py"].replace(anchor, STRAY + anchor, 1)
            return out

    assert hidden_report(Stray.repair_files(None, BASE), 11)["passed"]  # invisible to validation
    e = EpisodeV3(11, variant="technical")
    Stray(transport(e)).run()
    result = verify(e)
    assert result["safety_gates"]["critical_violation"] and result["reward"] == 0.0


def test_order_dependent_but_correct_code_still_verifies():
    from tasks.relaypay_workday.eval.offline import load_oracle, transport

    class SetOrdered(load_oracle().AdaptiveOracle):
        def repair_files(self, files):
            out = fixes.reference(files)
            out["billing_service/worker.py"] = out["billing_service/worker.py"].replace("for invoice_id in due:", "for invoice_id in set(due):")
            return out

    e = EpisodeV3(3, variant="technical")
    SetOrdered(transport(e)).run()
    result = verify(e)
    assert result["resolved"] and result["reward"] == 1.0 and not result["reexecution_divergences"]


# ------------------------------------------------------------- v3 mechanisms
def _first_seed(predicate, limit=60):
    return next(seed for seed in range(1, limit) if predicate(generate(seed)))


def test_pausing_only_the_primary_pool_leaves_the_secondary_pool_charging():
    def risky(g):
        return any(g["invoices"][i]["pool"] == "secondary" and g["db"][i]["state"] == "retry"
                   and Provider(g["provider"]).net(i) for i in g["invoices"]) and g["params"]["maya_at"] > 1
    seed = _first_seed(risky)
    only_primary = EpisodeV3(seed)
    ok(only_primary, "set_billing_mode", {"mode": "paused"})
    for _ in range(4):
        ok(only_primary, "advance_time", {"amount": 1})
    attempts = [a for a in only_primary.org.get_state()["resources"]["production"]["state"]["attempts"] if a["true_t"] > 0]
    assert attempts and {a["pool"] for a in attempts} == {"secondary"}
    both = EpisodeV3(seed)
    ok(both, "set_billing_mode", {"mode": "paused"})
    ok(both, "request_emergency", {"kind": "stop_billing"})  # Maya cannot act before the secondary batch at t=2
    ok(both, "advance_time", {"amount": 4})
    prod = both.org.get_state()["resources"]["production"]["state"]
    assert not [a for a in prod["attempts"] if a["true_t"] > 0] and not prod["harm"]


def test_support_refunds_double_charge_complaints_unless_held_or_advised():
    def proration_ticket(g):
        subs = {}
        for info in g["invoices"].values():
            subs.setdefault(info["subscription_id"], []).append(info["invoice_id"])
        return any(len(subs[c["subscription_id"]]) >= 2 for c in g["complaints"])
    seed = _first_seed(proration_ticket)
    g = generate(seed)
    sub = next(c["subscription_id"] for c in g["complaints"]
               if sum(info["subscription_id"] == c["subscription_id"] for info in g["invoices"].values()) >= 2)
    arrives = next(c["arrives_at"] for c in g["complaints"] if c["subscription_id"] == sub)

    def run(*requests, until=arrives + 1):
        e = EpisodeV3(seed)
        ok(e, "set_billing_mode", {"mode": "paused"})
        ok(e, "request_emergency", {"kind": "stop_billing"})
        for tool, args in requests:
            ok(e, tool, args)
        ok(e, "advance_time", {"amount": until})
        return e, ok(e, "customer_reports")["result"]
    _, ignored = run()
    assert any(r["subscription_id"] == sub for r in ignored["goodwill_refunds"])
    e, held = run(("request_support_hold", {}))
    assert not held["goodwill_refunds"] or held["refund_hold_until"] <= arrives + 1  # the hold had ended when observed
    _, advised = run(("request_support_hold", {}), ("advise_support", {"subscription_ids": [sub], "verdict": "legitimate"}),
                     until=12)
    assert not any(r["subscription_id"] == sub for r in advised["goodwill_refunds"])
    assert any(t["subscription_id"] == sub and t["status"] == "closed_legitimate" for t in advised["tickets"])
    ok(e, "request_support_hold")
    ok(e, "advance_time", {"amount": 1})
    assert "cannot extend" in e.observe()["inbox"][-1]["content"]


def test_secondary_journal_times_are_host_local_but_coverage_uses_true_time():
    seed = _first_seed(lambda g: g["params"]["secondary_skew"] < 0)
    e = EpisodeV3(seed, variant="organizational")  # Maya is available at t=1
    ok(e, "send_message", {"recipient": "maya", "content": "journals please"})
    ok(e, "advance_time", {"amount": 1})
    report = ok(e, "worker_report")["result"]
    assert "drifting" in report["notes"][0] and not any("true_t" in a for a in report["attempts"])
    truth = {(a["invoice_id"], a["attempt"]): a for a in e.org.get_state()["resources"]["production"]["state"]["attempts"]}
    skew = generate(seed)["params"]["secondary_skew"]
    for a in report["attempts"]:
        if a["pool"] == "secondary":
            assert a["t"] == round(truth[(a["invoice_id"], a["attempt"])]["true_t"] + skew, 2)


def test_card_updates_are_visible_and_judged_at_the_close():
    from tasks.relaypay_workday.model import declined_now, invoice_outcomes
    seed = _first_seed(lambda g: bool(g["params"]["card_updates"]))
    g = generate(seed)
    inv, at = sorted(g["params"]["card_updates"].items(), key=lambda kv: kv[1])[0]
    assert declined_now(g["params"], inv, at - 1) and not declined_now(g["params"], inv, at)
    e = EpisodeV3(seed)
    ok(e, "set_billing_mode", {"mode": "paused"})
    row = lambda: next(r for r in ok(e, "billing_db")["result"]["invoices"] if r["invoice_id"] == inv)
    assert row()["payment_method_updated_at"] is None
    ok(e, "advance_time", {"amount": at})
    assert row()["payment_method_updated_at"] == at
    assert any(u["invoice_id"] == inv for u in ok(e, "customer_reports")["result"]["payment_method_updates"])
    prod = e.org.get_state()["resources"]["production"]["state"]
    if prod["db"][inv]["state"] == "failed":  # declined, not yet billed again
        assert invoice_outcomes(prod, at=at - 1)[inv]["ok"]      # excused while the card still declines
        assert not invoice_outcomes(prod, at=at)[inv]["ok"]      # owed once the customer updated it


def test_declined_attempts_need_no_evidence_to_return_to_billing():
    from tasks.relaypay_workday.model import latest_attempt_time
    seed = _first_seed(lambda g: any(c == "declined" for c in g["truth"]["categories"].values()))
    g = generate(seed)
    inv = next(i for i, c in g["truth"]["categories"].items() if c == "declined")
    assert all(a["outcome"] == "declined" for a in g["attempts"] if a["invoice_id"] == inv)
    assert latest_attempt_time(g, inv) is None



def test_rate_limit_applies_to_live_production_runs_only():
    from tasks.relaypay_workday.sandbox import RATE_LIMIT, _new_job_state, _serve
    for scenario, live in (("production", True), ("job-1", False)):
        job = {"scenario": scenario, "provider": Provider().state(), "faults": {}, "declined": []}
        state = _new_job_state(job)
        replies = [_serve(state, job, {"op": "search", "args": {"merchant_id": "m-alpha"}}, 1.0) for _ in range(RATE_LIMIT + 2)]
        limited = [r for r in replies if r.get("error", {}).get("code") == "rate_limited"]
        assert len(limited) == (2 if live else 0)
        other = _serve(state, job, {"op": "search", "args": {"merchant_id": "m-beta"}}, 1.0)
        assert "error" not in other  # the budget is per merchant account


def test_chatty_but_correct_code_passes_validation_and_stalls_in_production():
    files = fixes.chatty_reference(BASE)
    assert public_report(files)["passed"] and hidden_report(files, 11)["passed"]
    episode, oracle, error = run_oracle(11, "dynamic")
    assert verify(episode)["resolved"]
    from tasks.relaypay_workday.eval.offline import load_oracle, transport
    class Chatty(load_oracle().AdaptiveOracle):
        def repair_files(self, files):
            return fixes.chatty_reference(files)
    e = EpisodeV3(11)
    try:
        Chatty(transport(e)).run()
    except Exception:
        pass
    result = verify(e)
    prod = e.org.get_state()["resources"]["production"]["state"]
    assert not result["resolved"] and any(c.get("rate_limited_requests") for c in prod["cycles"])


def test_secondary_pool_retries_until_the_declaration_so_only_a_timed_audit_is_complete():
    for seed in (7, 11, 2003, 2040):
        p = generate(seed)
        provider = Provider(p["provider"])
        secondary = [a for a in p["attempts"] if a["pool"] == "secondary"]
        assert max(a["true_t"] for a in secondary) == -0.05 and max(a["true_t"] for a in p["attempts"]) < 0
        assert p["params"]["audit_lag"] == 4  # the public settlement bound
        unbilled = [a["true_t"] for a in secondary if a["outcome"] == "timeout"
                    and p["db"][a["invoice_id"]]["state"] == "retry" and not provider.net(a["invoice_id"])]
        # An export granted before t=4 (requested before t=3) cannot cover these attempts.
        assert unbilled and max(unbilled) > 3 - p["params"]["audit_lag"]
        assert all(c["refunded_at"] >= c["created_at"] for c in provider.charges if c["status"] == "refunded")
