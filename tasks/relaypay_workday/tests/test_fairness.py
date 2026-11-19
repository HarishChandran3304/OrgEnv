"""Fairness rules: what reaches support at t applies at t, and the graded score.

The scheduler orders the billing tick at t before a message delivered at t. Without the
same-instant rule in ``model.tick`` a hold or guidance "delivered at t" silently lost to
the tick at t, although the queue then showed it as received.
"""
from tasks.relaypay_workday.eval.offline import run_oracle, verify
from tasks.relaypay_workday.model import SUPPORT_DELIVERY, generate
from tasks.relaypay_workday.scoring import graded_reward, invoice_share, weighted_reward
from tasks.relaypay_workday.task import EpisodeV3

SEED = 505673846


def ok(episode, tool, args=None):
    result = episode.invoke(tool, args or {})
    assert result["ok"], (tool, result)
    return result


def _proration_complaint():
    """A seed with a customer who complains about two charges that belong to two invoices."""
    def two_invoices(g, sub):
        return sum(info["subscription_id"] == sub for info in g["invoices"].values()) >= 2
    for seed in range(1, 60):
        g = generate(seed)
        for complaint in g["complaints"]:
            if two_invoices(g, complaint["subscription_id"]):
                return seed, complaint["subscription_id"], complaint["arrives_at"], g["params"]["support_patience"]
    raise AssertionError("no seed with a proration complaint")


def _contained(seed, until):
    episode = EpisodeV3(seed)
    ok(episode, "set_billing_mode", {"mode": "paused"})
    ok(episode, "request_emergency", {"kind": "stop_billing"})
    ok(episode, "advance_time", {"amount": until})
    return episode


def _refunded(reports, sub):
    return any(r["subscription_id"] == sub for r in reports["goodwill_refunds"])


def test_unguided_support_refunds_the_complaint_when_it_arrives():
    seed, sub, arrives, _ = _proration_complaint()
    episode = _contained(seed, arrives)
    assert _refunded(ok(episode, "customer_reports")["result"], sub)


def test_guidance_that_reaches_support_at_t_applies_to_the_complaints_answered_at_t():
    seed, sub, arrives, _ = _proration_complaint()
    episode = _contained(seed, arrives - SUPPORT_DELIVERY)
    sent = ok(episode, "advise_support", {"subscription_ids": [sub], "verdict": "legitimate"})
    assert sent["delivery_at"] == arrives  # the delivery time Alex is told is the time it takes effect
    ok(episode, "advance_time", {"amount": SUPPORT_DELIVERY})
    reports = ok(episode, "customer_reports")["result"]
    assert not _refunded(reports, sub)
    assert any(t["subscription_id"] == sub and t["status"] == "closed_legitimate" for t in reports["tickets"])
    assert reports["guidance_received"][sub]["verdict"] == "legitimate"
    assert "Noted guidance" in episode.observe()["inbox"][-1]["content"]


def test_a_hold_that_reaches_support_at_t_covers_the_complaints_answered_at_t():
    seed, sub, arrives, patience = _proration_complaint()
    episode = _contained(seed, arrives - SUPPORT_DELIVERY)
    ok(episode, "request_support_hold")
    ok(episode, "advance_time", {"amount": SUPPORT_DELIVERY})
    reports = ok(episode, "customer_reports")["result"]
    assert not _refunded(reports, sub)
    assert reports["refund_hold_until"] == arrives + patience
    assert "will hold goodwill refunds" in episode.observe()["inbox"][-1]["content"]


def test_guidance_sent_after_the_complaint_was_answered_is_too_late():
    seed, sub, arrives, _ = _proration_complaint()
    episode = _contained(seed, arrives)
    ok(episode, "advise_support", {"subscription_ids": [sub], "verdict": "legitimate"})
    ok(episode, "advance_time", {"amount": SUPPORT_DELIVERY})
    assert _refunded(ok(episode, "customer_reports")["result"], sub)


def test_invoice_share_counts_incident_invoices_and_any_invoice_that_ends_wrong():
    fine, wrong = {"ok": True}, {"ok": False}
    assert invoice_share(["a", "b"], {"a": fine, "b": fine, "healthy": fine}) == 1.0
    assert invoice_share(["a", "b", "c", "d"], {"a": wrong, "b": fine, "c": fine, "d": fine, "healthy": fine}) == 0.75
    # Breaking an invoice the incident never touched counts against the run.
    assert invoice_share(["a", "b", "c", "d"], {"a": fine, "b": fine, "c": fine, "d": fine, "healthy": wrong}) == 0.8
    assert invoice_share([], {"healthy": fine}) == 1.0


def test_graded_reward_differs_from_the_score_only_when_invoices_alone_are_wrong():
    assert graded_reward(1.0, 1.0, 1.0, True, 0.1) == weighted_reward(1.0, 1.0, True, 0.1)       # resolved
    assert graded_reward(1.0, 0.5, 0.9, False, 0.0) == weighted_reward(1.0, 0.5, False, 0.0)    # another check failed
    partial = graded_reward(1.0, 1.0, 0.5, True, 0.0)
    assert weighted_reward(1.0, 1.0, False, 0.0) < partial < weighted_reward(1.0, 1.0, True, 0.0)
    assert graded_reward(1.0, 1.0, 0.5, True, 0.2) < partial                                     # harm still costs


def test_the_verifier_reports_the_graded_score_next_to_an_unchanged_score():
    result = verify(run_oracle(SEED, "linked")[0])
    assert result["resolved"] and result["reward_graded"] == result["reward"]
    assert result["components"]["invoice_share"] == 1.0
    untouched = verify(EpisodeV3(SEED, variant="linked"))
    assert not untouched["resolved"] and untouched["reward"] <= untouched["reward_graded"] < 0.3


def test_the_scorecard_reports_one_meaning_per_number():
    solved = verify(run_oracle(SEED, "linked")[0])["scorecard"]
    assert solved["solved"] and solved["completion"] == 1.0 and solved["rules_broken"] == 0
    assert solved["code_groups_passed"] == solved["code_groups"] > 0 and solved["customers_harmed"] <= solved["customers"]
    untouched = verify(EpisodeV3(SEED, variant="linked"))["scorecard"]
    # Literal counts: some affected invoices end correct with no help, and the shipped code passes one group.
    assert not untouched["solved"] and untouched["completion"] < 0.3
    assert untouched["code_groups_passed"] < untouched["code_groups"] and untouched["customers_harmed"] > 0
