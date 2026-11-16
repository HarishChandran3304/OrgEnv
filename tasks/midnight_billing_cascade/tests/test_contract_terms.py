"""Contract refund terms: part of the technical specification is held by people.

``spec`` turns on contract refund terms: each live merchant's contract says whether a full
refund means "bill again" or "the customer is released". The term is in no file; the deployed
release must apply it. ``linked`` is ``full`` plus ``spec``; no earlier variant changes.
"""
import pytest

from tasks.midnight_billing_cascade.eval.controls import CONTROLS
from tasks.midnight_billing_cascade.eval.offline import load_oracle, run_oracle, transport, verify
from orgenv.core import Action
from tasks.midnight_billing_cascade.model import Provider, generate, invoice_outcomes, rebill_owed
from tasks.midnight_billing_cascade.sandbox import canonical_workspace, hidden_report, public_report, technical_progress
from tasks.midnight_billing_cascade.task import OPERATIONS, EpisodeV3, db_digest
from tasks.midnight_billing_cascade.variants import VARIANTS, VERTICALS

SEED = 505673846
ORACLE = load_oracle()
BASE = canonical_workspace()


def ok(episode, tool, args=None):
    result = episode.invoke(tool, args or {})
    assert result["ok"], (tool, result)
    return result


def state(episode, name):
    return episode.org.get_state()["resources"][name]["state"]


def fixed(release):
    return ORACLE.apply_terms(ORACLE.repair(BASE), release)


def control(name, seed, variant):
    episode = EpisodeV3(seed, variant=variant)
    try:
        CONTROLS[name](ORACLE)(transport(episode)).run()
    except Exception:
        pass
    return verify(episode)


# ------------------------------------------------------------------ the switch
def test_spec_is_a_separate_switch_and_earlier_variants_do_not_change():
    assert VARIANTS["spec"].spec and not any(getattr(VARIANTS["spec"], v) for v in VERTICALS)
    assert VARIANTS["linked"].spec and all(getattr(VARIANTS["linked"], v) for v in VERTICALS)
    assert not any(VARIANTS[v].spec for v in ("technical", "organizational", "dynamic", "full", *VERTICALS))
    base, spec, full = generate(SEED, "dynamic"), generate(SEED, "spec"), generate(SEED, "full")
    assert spec["invoices"] == base["invoices"] and spec["attempts"] == base["attempts"]
    assert {k: spec["params"][k] for k in base["params"]} == base["params"]
    assert "refund_terms" not in full["params"] and "merchant_refunds" not in full
    linked = generate(SEED, "linked")
    assert linked["credits"] == generate(SEED, "incentives")["credits"]
    assert not set(linked["merchant_refunds"]) & set(linked["credits"])


def test_only_spec_variants_show_the_tool_the_policy_the_owners_and_the_extra_scenario():
    full, linked = EpisodeV3(SEED, variant="full"), EpisodeV3(SEED, variant="linked")
    assert "request_contract_terms" not in full.catalog() and "request_contract_terms" in linked.catalog()
    extra = set(linked.observe()["public"]["policies"]) - set(full.observe()["public"]["policies"])
    assert extra == {"refund_terms"}
    assert "account owner" not in str(full.observe()["public"]["roles"])
    assert "contracts" not in full.observe()["resources"] and linked.observe()["resources"]["contracts"]["state"] == {"terms": {}}
    assert "refund_releases_invoice" not in ok(full, "read_public_tests")["result"]["scenarios"]
    assert "refund_releases_invoice" in ok(linked, "read_public_tests")["result"]["scenarios"]
    assert "Contract refund terms" not in ok(full, "read_workspace")["result"]["provider_contract"]
    assert "Contract refund terms" in ok(linked, "read_workspace")["result"]["provider_contract"]


@pytest.mark.parametrize("seed", range(2000, 2030))
def test_every_incident_has_both_terms_an_owner_off_shift_and_refunds_under_each_contract(seed):
    world = generate(seed, "spec")
    terms, owners = world["params"]["refund_terms"], world["params"]["account_owners"]
    assert set(terms) == set(owners) == {i["merchant_id"] for i in world["invoices"].values()}
    assert set(terms.values()) == {"rebill", "release"} and set(owners.values()) - {"sam"}
    provider = Provider(world["provider"])
    for inv in world["merchant_refunds"]:
        assert world["db"][inv]["state"] == "paid" and not provider.net(inv)  # the database still says paid
    # The owners are public (roles); the terms are not.
    public = str(EpisodeV3(seed, variant="spec").observe()["public"])
    assert all(merchant in public for merchant in owners)
    assert "'rebill'" not in public and "'release'" not in public


# ------------------------------------------------------ the technical requirement
def test_validation_requires_the_term_of_the_published_example_merchant():
    plain = ORACLE.repair(BASE)
    assert public_report(plain)["passed"] and not public_report(plain, spec=True)["passed"]
    assert public_report(fixed(set()), spec=True)["passed"]
    assert public_report(fixed(set()))["passed"]  # the terms change nothing for the other scenarios


def test_hidden_validation_covers_this_incidents_live_merchants():
    terms = generate(SEED, "spec")["params"]["refund_terms"]
    release = {m for m, t in terms.items() if t == "release"}
    assert hidden_report(fixed(release), SEED, terms)["passed"]
    never_asked = hidden_report(fixed(set()), SEED, terms)
    guessed = hidden_report(fixed(set(terms)), SEED, terms)
    for report in (never_asked, guessed):
        assert not report["passed"] and [g for g, good in report["groups"].items() if not good] == ["contract_refund_terms"]
    assert "contract_refund_terms" not in hidden_report(fixed(release), SEED)["groups"]  # other variants: unchanged
    assert technical_progress(fixed(release), SEED, terms)[0] == 1.0 > technical_progress(fixed(set()), SEED, terms)[0]


# ------------------------------------------------------------------ asking people
def owned_by(world, person):
    return sorted(m for m, o in world["params"]["account_owners"].items() if o == person)


def test_an_owner_answers_only_for_their_own_accounts():
    seed = next(s for s in range(2000, 2100) if owned_by(generate(s, "spec"), "sam") and owned_by(generate(s, "spec"), "maya"))
    world, e = generate(seed, "spec"), EpisodeV3(seed, variant="spec")
    mine, hers = owned_by(world, "sam")[0], owned_by(world, "maya")[0]
    ok(e, "request_contract_terms", {"recipient": "sam", "merchant_id": mine})
    ok(e, "request_contract_terms", {"recipient": "sam", "merchant_id": hers})
    ok(e, "advance_time", {"amount": 1.0})
    terms = state(e, "contracts")["terms"]
    refunded = sorted(i for i in world["merchant_refunds"] if world["invoices"][i]["merchant_id"] == mine)
    assert terms == {mine: {"refund_term": world["params"]["refund_terms"][mine], "refunded_this_period": refunded, "from": "sam", "at": 1.0}}
    assert any(f"{hers} is not my account" in m["content"] for m in e.observe()["inbox"])
    assert not state(e, "incident")["contract_requests"]


def test_jordan_reads_a_contract_only_while_its_owner_is_away():
    seed = next(s for s in range(2000, 2100) if generate(s, "spec")["params"]["maya_at"] >= 3 and owned_by(generate(s, "spec"), "maya"))
    world, e = generate(seed, "spec"), EpisodeV3(seed, variant="spec")
    merchant = owned_by(world, "maya")[0]
    ok(e, "request_contract_terms", {"recipient": "jordan", "merchant_id": merchant})
    ok(e, "advance_time", {"amount": 1.0})
    assert state(e, "contracts")["terms"][merchant]["from"] == "jordan"
    late = EpisodeV3(seed, variant="spec")
    ok(late, "advance_time", {"amount": float(world["params"]["maya_at"])})
    ok(late, "request_contract_terms", {"recipient": "jordan", "merchant_id": merchant})
    ok(late, "advance_time", {"amount": 1.0})
    assert not state(late, "contracts")["terms"]
    assert any("is available; ask them" in m["content"] for m in late.observe()["inbox"])
    ok(late, "request_contract_terms", {"recipient": "maya", "merchant_id": merchant})
    ok(late, "advance_time", {"amount": 1.0})
    assert state(late, "contracts")["terms"][merchant]["refund_term"] == world["params"]["refund_terms"][merchant]


# --------------------------------------------------------------------- outcomes
def test_a_released_invoice_ends_unpaid_and_a_rebill_contract_rebill_is_not_harm():
    world = generate(SEED, "spec")
    terms = world["params"]["refund_terms"]
    by_term = {terms[world["invoices"][inv]["merchant_id"]]: inv for inv in world["merchant_refunds"]}
    released, owed = by_term["release"], by_term["rebill"]
    assert not invoice_outcomes(world, at=6.0)[released]["ok"]                 # still recorded as paid
    world["db"][released] = {"state": "released", "charge_id": None}
    assert invoice_outcomes(world, at=6.0)[released]["ok"]
    assert rebill_owed(world, owed) and not rebill_owed(world, released)
    provider = Provider(world["provider"])
    for inv in (released, owed):
        info = world["invoices"][inv]
        reply, created = provider.charge(f"again-{inv}", {k: info[k] for k in ("merchant_id", "invoice_id", "subscription_id", "period", "amount")}, "ok", 4.0, "test")
        world["db"][inv] = {"state": "paid", "charge_id": created["charge_id"]}
    world["provider"] = provider.state()
    outcomes = invoice_outcomes(world, at=6.0)
    assert outcomes[owed]["ok"] and "billed again after a refund released" in outcomes[released]["reason"]


def test_the_tie_break_is_part_of_the_handlers_the_verifier_replays():
    e = EpisodeV3(SEED, variant="discretion")
    w = e.org._state
    inc, release = w.get_resource("incident").state, w.get_resource("release").state
    release["approval"] = {"release_id": "r", "at": e.now, "db_digest": db_digest(w.get_resource("production").state), "pools": dict(inc["pools"])}
    inc["pool_requests"].append({"pool": "secondary", "mode": "paused", "requested_at": e.now - 1})
    OPERATIONS["update_worker_report"][0](e, w, Action("maya", "update_worker_report", {}))
    assert inc["pools"]["secondary"] == "paused" and release["approval"]["pools"] == inc["pools"]


# ------------------------------------------------------------------ solvability
@pytest.mark.parametrize("variant", ["spec", "linked"])
def test_oracle_asks_first_and_resolves(variant):
    episode, oracle, error = run_oracle(SEED, variant)
    result = verify(episode)
    assert error is None and result["resolved"] and not result["policy_violations"], result
    assert result["reward"] >= 0.95 and len(episode.calls) <= 120
    assert set(state(episode, "contracts")["terms"]) == set(generate(SEED, variant)["params"]["refund_terms"])
    asked = [i for i, c in enumerate(episode.calls) if c["tool"] == "request_contract_terms"]
    edited = [i for i, c in enumerate(episode.calls) if c["tool"] == "edit_source"]
    assert asked and max(asked) < min(edited)


@pytest.mark.parametrize("name", ["never_ask_terms", "assume_release_terms"])
def test_code_written_without_asking_is_not_valid_for_production(name):
    result = control(name, SEED, "spec")
    assert not result["resolved"] and not result["checks"]["payment_retry_behavior"]
    assert result["components"]["technical_progress"] < 1.0
