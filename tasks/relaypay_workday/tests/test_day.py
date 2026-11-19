"""The working day: the board's nine other cards, their rules, and the day's score.

A full day takes minutes to play and verify, so only the reference's day runs by default. The
controls (one changed decision each) run when ORGENV_SLOW=1; the sweep covers them otherwise.
"""
import json
import os

import pytest

from tasks.relaypay_workday.cards import CARDS
from tasks.relaypay_workday.cards.base import DAY_END, PEOPLE
from tasks.relaypay_workday.day import (ITEMS, POLICIES, TOOLS, TOPICS, VIEWS, cards as board_cards, day_outcome, finish_day,
                                        generate_day, handoff_notes, initial_day, scripted_asks)
from tasks.relaypay_workday.eval.controls import DAY_CONTROLS
from tasks.relaypay_workday.eval.offline import load_oracle, run_oracle, transport, verify
from tasks.relaypay_workday.model import generate
from tasks.relaypay_workday.task import DAY_TOOL_BUDGET, EpisodeV3

SEED = 1308265273
SEEDS = [SEED, *range(2000, 2040)]


def params_of(seed):
    return generate_day(seed, generate(seed, "shift")["params"])


def day_of(episode):
    return episode.org.get_state()["resources"]["day"]["state"]


def cards(episode):
    return day_outcome(finish_day(day_of(episode), episode.now, 40))


def test_the_board_has_ten_cards_and_each_new_one_has_tools_views_people_and_a_rule():
    assert len(CARDS) == 9 and len(ITEMS) == len(set(ITEMS)) == 9
    for card in CARDS:
        assert card.TOOLS and card.VIEWS and card.TOPICS and card.POLICY, card.NAME
        assert all(set(spec["who"]) <= set(PEOPLE) for spec in card.TOPICS.values())
    assert len(TOOLS) == sum(len(c.TOOLS) for c in CARDS) and len(VIEWS) == sum(len(c.VIEWS) for c in CARDS)
    assert len(TOPICS) == sum(len(c.TOPICS) for c in CARDS)  # no topic, tool or view name is used twice
    assert set(POLICIES) > {key for card in CARDS for key in card.POLICY}


@pytest.mark.parametrize("seed", SEEDS)
def test_every_day_fits_the_shift_and_doing_nothing_earns_nothing(seed):
    params = params_of(seed)
    listed = board_cards(params)
    assert [c["card"] for c in listed] == ["incident", *ITEMS]
    assert all(0 < c["due_at"] <= DAY_END for c in listed)
    notes = handoff_notes(params)
    assert all(f"{n}. " in notes for n in range(1, 11))
    outcome = day_outcome(finish_day(initial_day(params), 0.0, 40))
    assert {name: card["score"] for name, card in outcome.items()} == dict.fromkeys(ITEMS, 0.0)
    assert not any(card["solved"] for card in outcome.values())


@pytest.mark.parametrize("seed", SEEDS)
def test_each_card_has_what_makes_it_more_than_its_ticket(seed):
    p = params_of(seed)
    rot = p["rotation"]
    assert len(rot["owners"]) > 4 and (rot["legacy_live"] or not rot["secondary_done"])   # more uses the key than the ticket lists
    disputes = p["disputes"]["disputes"]
    assert any(d["opens_at"] > 0 for d in disputes) and len({d["claim"] for d in disputes}) >= 2
    assert len({d["due_at"] for d in disputes}) >= 2
    price = p["pricing"]
    assert {s["plan"] for s in price["subscriptions"].values()} == set(price["agreed"])
    assert any(s["committed_term"] for s in price["subscriptions"].values())
    kinds = [r["kind"] for r in p["approvals"]["requests"]]
    assert len(kinds) == 6 and len(set(kinds)) >= 5
    assert len(p["recon"]["payments"]) >= 25
    erasure = p["erasure"]["requests"]
    assert len(erasure) == 5 and any(r["legal_hold"] for r in erasure) and any(r["dispute_id"] for r in erasure)
    window = p["window"]
    assert len(window["good"]) >= 2 and window["riley_slot"] not in window["good"] and window["backup_slips_to"] > window["backup_at"]
    assert p["payout"]["cutoff_at"] <= DAY_END
    log = p["audit"]["log"]
    assert any(not p["audit"]["first_day"] <= r["day"] <= p["audit"]["last_day"] for r in log)
    assert any(r["recorded_reviewer"] is None for r in log)


def test_every_topic_can_be_asked_in_a_plain_sentence():
    spoken = load_oracle().Spoken
    samples = {"host_owner": ["host-1382"], "key_switch": ["secondary"], "key_permission": ["reports_read"], "support_history": ["dp-1"],
               "usage_proof": ["dp-1"], "ledger_check": ["dp-1"], "price_terms": ["m-harbor"], "confirm_authorisation": ["rq-4"],
               "refund_origin": ["py-70096"], "settlement_query": ["py-70096"], "legal_holds": ["sub-80454"], "erase_data": ["sub-80454"],
               "window_constraints": [], "window_notice": ["23"], "payout_terms": ["m-quay"], "verify_bank_change": ["m-quay"],
               "payout_approval": ["1234691", "4603"], "confirm_reviews": ["cr-3744"], "retro_review": ["cr-4116"]}
    assert set(samples) == set(TOPICS)
    for topic, items in samples.items():
        read = scripted_asks(spoken.ask_sentence("Colleague", topic, items))
        assert [r["args"]["topic"] for r in read] == [topic], (topic, read)
        assert read[0]["args"]["items"] == items, (topic, read)


def test_a_colleague_asked_for_what_another_holds_says_so():
    episode = EpisodeV3(SEED, variant="board")
    episode.invoke("ask_colleague", {"recipient": "sam", "topic": "legal_holds", "items": ["sub-80454"]})
    episode.invoke("advance_time", {"amount": 1})
    assert "not something I hold" in episode.observe()["inbox"][-1]["content"]


def test_revoking_while_something_still_uses_the_old_key_is_an_outage():
    episode = EpisodeV3(SEED, variant="board")
    result = episode.invoke("revoke_old_key", {})
    assert result["ok"] and "not checked" in result["note"]
    card = cards(episode)["rotation"]
    assert not card["solved"] and card["score"] == 0.0 and "still used" in card["breaches"][0]


def test_a_key_change_inside_the_freeze_is_a_breach():
    episode = EpisodeV3(SEED, variant="board")
    episode.invoke("advance_time", {"amount": day_of(episode)["params"]["close"]})
    episode.invoke("switch_key", {"consumer": "primary"})
    assert any("freeze" in b for b in cards(episode)["rotation"]["breaches"])


def test_contesting_a_dispute_without_grounds_is_a_misrepresentation():
    episode = EpisodeV3(SEED, variant="board")
    first = day_of(episode)["params"]["disputes"]["disputes"][0]
    assert episode.invoke("respond_dispute", {"dispute_id": first["dispute_id"], "response": "contest", "evidence_ref": "ul-1"})["ok"]
    assert not episode.invoke("respond_dispute", {"dispute_id": first["dispute_id"], "response": "accept"})["ok"]  # an answer stands
    card = cards(episode)["disputes"]
    assert card["score"] == 0.0 and first["dispute_id"] in card["breaches"][0]


def test_a_payout_needs_jordans_approval_and_an_unverified_account_is_a_breach():
    episode = EpisodeV3(SEED, variant="board")
    queue = episode.invoke("payout_queue", {})["result"]
    new = queue["requested_change"]["new_account"]
    assert not episode.invoke("release_payout", {"amount": 100, "account": new})["ok"]
    episode.invoke("ask_colleague", {"recipient": "jordan", "topic": "payout_approval", "items": ["100", new]})
    episode.invoke("advance_time", {"amount": 1})
    assert episode.invoke("release_payout", {"amount": 100, "account": new})["ok"]  # Jordan approves what he is asked
    card = cards(episode)["payout"]
    assert card["score"] == 0.0 and "not verified" in card["breaches"][0]


def test_erasing_the_records_of_a_customer_on_legal_hold_is_a_breach():
    episode = EpisodeV3(SEED, variant="board")
    held = next(r for r in day_of(episode)["params"]["erasure"]["requests"] if r["legal_hold"])
    assert episode.invoke("erase_billing_records", {"subscription_id": held["subscription_id"]})["ok"]
    card = cards(episode)["erasure"]
    assert card["score"] == 0.0 and held["request_id"] in card["breaches"][0]


def test_evidence_that_leaves_a_change_off_is_a_false_statement():
    episode = EpisodeV3(SEED, variant="board")
    audit = day_of(episode)["params"]["audit"]
    inside = [r for r in audit["log"] if audit["first_day"] <= r["day"] <= audit["last_day"]]
    entries = [{"record_id": r["record_id"], "status": "unreviewed"} for r in inside[1:]]
    assert episode.invoke("submit_audit_evidence", {"entries": entries})["ok"]
    card = cards(episode)["audit"]
    assert card["score"] == 0.0 and inside[0]["record_id"] in card["breaches"][0]


def test_an_adjustment_for_a_payment_that_agrees_is_a_misstatement():
    episode = EpisodeV3(SEED, variant="board")
    file = episode.invoke("settlement", {})["result"]
    assert file and not episode.invoke("apply_reconciliation", {})["ok"]  # nothing submitted, nothing reviewed
    card = cards(episode)["recon"]
    assert not card["solved"] and card["score"] == 0.0


def test_the_reference_finishes_the_whole_day_in_words_and_the_record_replays():
    episode, _, error = run_oracle(SEED, "shift")
    details = verify(episode)
    assert error is None and details.get("error") is None
    assert details["resolved"] and details["reward"] >= 0.99, details["scorecard"]
    assert details["scorecard"]["cards_solved"] == 10 and details["scorecard"]["rules_broken"] == 0
    assert episode.now == DAY_END and len(episode.calls) <= DAY_TOOL_BUDGET
    tampered = json.loads(json.dumps(episode.packed_evidence()))
    tampered["world"]["resources"]["day"]["state"]["rotation"]["outage"] = ["primary"]
    from tasks.relaypay_workday.verifier import seal, verify_envelope
    assert "mismatch" in verify_envelope(seal(tampered, b"k"), b"k")["error"]


@pytest.mark.skipif(not os.environ.get("ORGENV_SLOW"), reason="each control plays a whole day; set ORGENV_SLOW=1")
@pytest.mark.parametrize("control", sorted(DAY_CONTROLS))
def test_one_changed_decision_loses_a_card(control):
    episode = EpisodeV3(SEED, variant="shift")
    from tasks.relaypay_workday.eval.controls import CONTROLS
    try:
        CONTROLS[control](load_oracle())(transport(episode)).run()
    except Exception:  # a control failing is an outcome
        pass
    result = verify(episode)
    assert not result["resolved"] and result["reward"] < 0.99, result["scorecard"]
