"""``busy``: colleagues need things from Alex during the shift.

They post asks in the incident record, each due one time unit later, and Alex is on call
until the billing close. The switch is off in every earlier variant. ``busy`` turns only it
on; ``workday`` is ``sealed`` plus ``busy``.
"""
import json
from pathlib import Path

import pytest

from tasks.midnight_billing_cascade.eval.controls import CONTROLS
from tasks.midnight_billing_cascade.eval.offline import load_oracle, run_oracle, transport, verify
from tasks.midnight_billing_cascade.model import ASK_WINDOW, ASKS, generate
from tasks.midnight_billing_cascade.task import EpisodeV3
from tasks.midnight_billing_cascade.variants import VARIANTS

SEED = 505673846
FROZEN = json.loads((Path(__file__).parents[1] / "eval/seeds.json").read_text())


def ok(episode, tool, args=None):
    result = episode.invoke(tool, args or {})
    assert result["ok"], (tool, result)
    return result


def production(episode):
    return episode.org.get_state()["resources"]["production"]["state"]


def asks_seen(episode):
    return episode.observe()["resources"]["incident"]["state"].get("asks", [])


def control(name, seed, variant):
    episode = EpisodeV3(seed, variant=variant)
    try:
        CONTROLS[name](load_oracle())(transport(episode)).run()
    except Exception:
        pass  # a control failing mid-way is an outcome
    return verify(episode)


def test_the_switch_is_off_everywhere_else_and_leaves_the_incident_itself_unchanged():
    assert [name for name, v in VARIANTS.items() if v.busy] == ["busy", "workday"]
    for variant, base in (("busy", "dynamic"), ("workday", "sealed")):
        world, plain = generate(SEED, variant), generate(SEED, base)
        assert world["invoices"] == plain["invoices"] and world["attempts"] == plain["attempts"]
        assert world["complaints"] == plain["complaints"] and world["provider"] == plain["provider"]
        assert {k: world["params"][k] for k in plain["params"]} == plain["params"]
        assert "asks" not in plain["params"] and "ask_answers" not in plain
    plain = EpisodeV3(SEED, variant="sealed")
    assert "answer_ask" not in plain.catalog() and "asks" not in plain.observe()["public"]["policies"]
    assert verify(plain)["scorecard"]["asks"] == 0 and verify(plain)["checks"]["asks_answered"]


@pytest.mark.parametrize("seed", FROZEN["development"] + FROZEN["unseen"])
def test_every_incident_has_several_asks_spread_over_the_shift(seed):
    params = generate(seed, "workday")["params"]
    asks = params["asks"]
    assert 4 <= len(asks) <= 6 and len({a["at"] for a in asks}) == len(asks)
    assert all(1 <= a["at"] < params["billing_close_at"] and a["due_at"] == a["at"] + ASK_WINDOW for a in asks)
    assert all(a["kind"] in ASKS for a in asks) and len({a["kind"] for a in asks}) >= 4


def test_an_ask_appears_when_it_is_made_and_is_judged_on_the_figure_at_the_time_of_the_answer():
    first = generate(SEED, "busy")["params"]["asks"][0]
    episode = EpisodeV3(SEED, variant="busy")
    assert "answer_ask" in episode.catalog() and "asks" in episode.observe()["public"]["policies"]
    assert not asks_seen(episode)
    ok(episode, "advance_time", {"amount": first["at"]})
    (ask,) = asks_seen(episode)
    assert ask["ask_id"] == first["ask_id"] and ask["from"] == ASKS[first["kind"]][0] and ask["question"] == ASKS[first["kind"]][1]
    assert ask["asked_at"] == first["at"] and ask["due_at"] == first["due_at"] and ask["answered_at"] is None
    assert not episode.invoke("answer_ask", {"ask_id": "ask-99", "value": 1})["ok"]
    ok(episode, "answer_ask", {"ask_id": ask["ask_id"], "value": 99999})  # on time, wrong figure
    assert asks_seen(episode)[0]["answered_at"] == first["at"]
    assert "correct" not in asks_seen(episode)[0]  # Alex is not told the verdict
    assert not episode.invoke("answer_ask", {"ask_id": ask["ask_id"], "value": 0})["ok"]  # an answer stands
    recorded = production(episode)["ask_answers"][ask["ask_id"]]
    assert recorded["on_time"] and not recorded["correct"]
    summary = verify(episode)["asks"]
    assert summary["wrong"] == [ask["ask_id"]] and summary["answered_correctly_on_time"] == 0


def test_an_answer_after_the_due_time_is_late_even_if_it_is_right():
    first = generate(SEED, "busy")["params"]["asks"][0]
    episode = EpisodeV3(SEED, variant="busy")
    ok(episode, "advance_time", {"amount": first["at"] + ASK_WINDOW})
    kind = first["kind"]
    truth = {"pools_running": lambda: sum(m == "running" for m in episode.observe()["resources"]["incident"]["state"]["pools"].values()),
             "unpaid_invoices": lambda: sum(r["state"] != "paid" for r in ok(episode, "billing_db")["result"]["invoices"]),
             "open_complaints": lambda: sum(t["status"] in ("pending", "waiting_for_incident") for t in ok(episode, "customer_reports")["result"]["tickets"]),
             "plans_applied": lambda: len(episode.observe()["resources"]["plan"]["state"]["applied"]),
             "release_installed": lambda: int(bool(episode.observe()["resources"]["incident"]["state"]["deployed_release"]))}[kind]()
    ok(episode, "answer_ask", {"ask_id": first["ask_id"], "value": truth})  # exactly at the due time: on time
    assert production(episode)["ask_answers"][first["ask_id"]] == {"at": first["due_at"], "value": truth, "on_time": True, "correct": True}
    late = EpisodeV3(SEED, variant="busy")
    ok(late, "advance_time", {"amount": first["at"] + ASK_WINDOW + 1})
    ok(late, "answer_ask", {"ask_id": first["ask_id"], "value": 0})
    assert not production(late)["ask_answers"][first["ask_id"]]["on_time"]
    assert first["ask_id"] in verify(late)["asks"]["late"]


def test_the_reference_answers_every_ask_and_stays_until_the_close():
    episode, _, error = run_oracle(SEED, "workday")
    result = verify(episode)
    assert error is None and result["resolved"] and len(episode.calls) <= 120
    card = result["scorecard"]
    assert card["solved"] and card["asks_answered"] == card["asks"] >= 4
    assert episode.now >= production(episode)["params"]["billing_close_at"]


@pytest.mark.parametrize("name", ["ignore_asks", "answer_asks_at_the_end"])
def test_working_the_incident_without_keeping_up_with_colleagues_is_not_a_complete_response(name):
    result = control(name, SEED, "workday")
    card = result["scorecard"]
    assert not result["resolved"] and not result["checks"]["asks_answered"] and card["asks_answered"] < card["asks"]
    # Everything else about the job is done: the asks are what is missing.
    assert card["completion"] == 1.0 and card["rules_broken"] == 0 and result["checks"]["invoice_integrity"]
