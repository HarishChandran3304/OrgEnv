"""The working day: the board's four other cards, their rules, and the day's score."""
import json

import pytest

from tasks.relaypay_workday.day import PLANS, day_outcome, finish_day, generate_day, handoff_notes, initial_day, proper
from tasks.relaypay_workday.eval.controls import CONTROLS
from tasks.relaypay_workday.eval.offline import load_oracle, run_oracle, transport, verify
from tasks.relaypay_workday.model import generate
from tasks.relaypay_workday.task import EpisodeV3

SEED = 1308265273
SEEDS = [SEED, *range(2000, 2030)]


def day_of(episode):
    return episode.org.get_state()["resources"]["day"]["state"]


def cards(episode):
    return verify(episode)["day"]["cards"]


@pytest.mark.parametrize("seed", SEEDS)
def test_every_day_is_solvable_and_rileys_notes_are_partly_wrong(seed):
    incident = generate(seed, "shift")["params"]
    params = generate_day(seed, incident)
    rot, disp, price = params["rotation"], params["disputes"], params["pricing"]
    assert not (rot["secondary_done"] and not rot["legacy_live"])           # the rotation note is wrong about something
    assert rot["due_at"] > incident["daniel_at"] + 2 and rot["due_at"] > incident["billing_close_at"] + 2
    assert any(d["noted"] == "contest" and d["kind"] == "refunded" for d in disp["disputes"])  # a stale, dangerous recommendation
    assert any(d["kind"] == "valid" for d in disp["disputes"]) and disp["respond_by"] > incident["maya_at"] + 2
    assert price["draft"] != price["agreed"] and price["exceptions"]
    assert price["amend_at"] > incident["daniel_at"] and price["runs_at"] - price["amend_at"] >= 6
    assert sorted(set(price["subscriptions"].values())) == sorted(PLANS)
    requests = params["approvals"]["requests"]
    assert sum(proper(r) for r in requests) == 2 and len(requests) == 4 and all(r["due_at"] <= 36 for r in requests)
    notes = handoff_notes(params)
    assert "should be done" in notes and all(d["dispute_id"] in notes for d in disp["disputes"])


def test_doing_nothing_on_the_board_earns_nothing_for_its_cards():
    episode = EpisodeV3(SEED, variant="shift")
    outcome = day_outcome(finish_day(day_of(episode), 0.0, 40))
    assert {name: card["score"] for name, card in outcome.items()} == dict.fromkeys(outcome, 0.0)
    assert not any(card["solved"] for card in outcome.values())


def test_revoking_while_something_still_uses_the_old_key_is_an_outage():
    episode = EpisodeV3(SEED, variant="shift")
    result = episode.invoke("revoke_old_key", {})
    assert result["ok"] and "not checked" in result["note"]
    card = cards(episode)["rotation"]
    assert not card["solved"] and card["score"] == 0.0 and "still used" in card["breaches"][0]
    assert not episode.invoke("switch_key", {"consumer": "primary"})["ok"]   # too late to switch
    assert not episode.invoke("switch_key", {"consumer": "secondary"})["ok"]  # never Alex's to switch


def test_a_key_change_inside_the_freeze_is_a_breach_even_if_everything_else_is_right():
    episode = EpisodeV3(SEED, variant="shift")
    close = day_of(episode)["params"]["close"]
    episode.invoke("advance_time", {"amount": close})
    episode.invoke("switch_key", {"consumer": "primary"})
    assert "freeze" in cards(episode)["rotation"]["breaches"][0]


def test_disputes_are_answered_once_and_a_contest_needs_the_reference_maya_gives():
    episode = EpisodeV3(SEED, variant="shift")
    disputes = day_of(episode)["params"]["disputes"]["disputes"]
    valid = next(d for d in disputes if d["kind"] == "valid")
    refunded = next(d for d in disputes if d["kind"] == "refunded")
    assert not episode.invoke("respond_dispute", {"dispute_id": valid["dispute_id"], "response": "contest"})["ok"]  # no reference
    assert episode.invoke("respond_dispute", {"dispute_id": valid["dispute_id"], "response": "contest", "usage_ref": valid["usage_ref"].upper()})["ok"]
    assert not episode.invoke("respond_dispute", {"dispute_id": valid["dispute_id"], "response": "accept"})["ok"]   # an answer stands
    assert cards(episode)["disputes"]["breaches"] == []
    episode.invoke("respond_dispute", {"dispute_id": refunded["dispute_id"], "response": "contest", "usage_ref": "ul-1"})
    card = cards(episode)["disputes"]
    assert card["score"] == 0.0 and refunded["dispute_id"] in card["breaches"][0]


def test_a_price_change_needs_ninas_review_and_the_batch_charges_what_is_in_force():
    episode = EpisodeV3(SEED, variant="board")
    params = day_of(episode)["params"]["pricing"]
    prices = dict(params["agreed"])
    assert episode.invoke("submit_price_change", {"prices": prices, "exceptions": params["exceptions"]})["ok"]
    assert not episode.invoke("apply_price_change", {})["ok"]
    episode.invoke("send_message", {"recipient": "nina", "content": "Please review the submitted price change."})
    episode.invoke("advance_time", {"amount": 1})
    assert "safe to apply" in episode.observe()["inbox"][-1]["content"]
    assert episode.invoke("apply_price_change", {})["ok"]
    card = cards(episode)["pricing"]   # right when applied, but the merchant changes the amendment later
    assert not card["solved"] and 0 < card["score"] < 1 and card["detail"]["overcharged"]
    episode.invoke("advance_time", {"amount": params["runs_at"] - 1})
    assert not episode.invoke("submit_price_change", {"prices": prices})["ok"]  # the batch has run


def test_nina_rejects_a_price_table_that_is_unsafe_but_does_not_know_the_contract():
    episode = EpisodeV3(SEED, variant="board")
    episode.invoke("submit_price_change", {"prices": {"basic": 2000, "pro": -5, "gold": 9000}, "exceptions": ["sub-1"]})
    episode.invoke("send_message", {"recipient": "nina", "content": "Please review the submitted price change."})
    episode.invoke("advance_time", {"amount": 1})
    said = episode.observe()["inbox"][-1]["content"]
    assert "not approved" in said and "team has no price" in said and "gold is not a plan" in said and "sub-1 is not a subscription" in said
    episode.invoke("submit_price_change", {"prices": {"basic": 1, "pro": 1, "team": 1}})  # absurd, but safe for the database
    episode.invoke("send_message", {"recipient": "nina", "content": "Please review the submitted price change."})
    episode.invoke("advance_time", {"amount": 1})
    assert "safe to apply" in episode.observe()["inbox"][-1]["content"]


def test_on_call_requests_arrive_through_the_day_and_expire():
    episode = EpisodeV3(SEED, variant="shift")
    requests = day_of(episode)["params"]["approvals"]["requests"]
    first = requests[0]
    assert episode.invoke("oncall_queue", {})["result"]["requests"] == []
    assert not episode.invoke("decide_request", {"request_id": first["request_id"], "decision": "decline"})["ok"]  # not asked yet
    episode.invoke("advance_time", {"amount": first["at"]})
    asked = next(m for m in episode.observe()["inbox"] if m["message_id"] == first["request_id"])
    assert asked["sender_id"] == first["from"] and first["request_id"] in asked["content"]
    listed = episode.invoke("oncall_queue", {})["result"]["requests"][0]
    assert ("change" in listed) != ("access" in listed) and listed["decision"] is None
    episode.invoke("advance_time", {"amount": 3})
    late = episode.invoke("decide_request", {"request_id": first["request_id"], "decision": "decline"})
    assert not late["ok"] and "expired" in json.dumps(late)


def test_the_reference_finishes_the_whole_day_in_words_and_the_record_replays():
    episode, _, error = run_oracle(SEED, "shift")
    details = verify(episode)
    assert error is None and details.get("error") is None
    assert details["resolved"] and details["reward"] >= 0.99, details["scorecard"]
    assert details["scorecard"]["cards_solved"] == 5 and details["scorecard"]["rules_broken"] == 0
    assert episode.now == 36.0 and len(episode.calls) <= 600
    tampered = json.loads(json.dumps(episode.evidence()))
    tampered["world"]["resources"]["day"]["state"]["rotation"]["outage"] = []
    tampered["world"]["resources"]["day"]["state"]["approvals"]["decisions"] = {}
    from tasks.relaypay_workday.verifier import seal, verify_envelope
    assert "mismatch" in verify_envelope(seal(tampered, b"k"), b"k")["error"]


@pytest.mark.parametrize("control,card", [
    ("trust_riley_rotation", "rotation"), ("trust_riley_disputes", "disputes"), ("trust_riley_prices", "pricing"),
    ("ignore_amendment", "pricing"), ("approve_whoever_asks", "approvals"), ("leave_after_incident", "approvals"),
])
def test_one_changed_decision_loses_its_card_and_only_that_card(control, card):
    module = load_oracle()
    episode = EpisodeV3(SEED, variant="shift")
    CONTROLS[control](module)(transport(episode)).run()
    result = verify(episode)["day"]["cards"]
    assert not result[card]["solved"], result[card]
    if control != "leave_after_incident":  # leaving early loses every card still open
        assert all(c["solved"] for name, c in result.items() if name != card), {n: c["solved"] for n, c in result.items()}
