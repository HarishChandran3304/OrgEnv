"""A merchant's price change that must be in force, and right, before its renewal batch.

The signed amendment is held by the account owner and is stated as rules, not as a list: which
subscriptions keep their price and which get a discount has to be worked out from the
subscription data. The merchant changes the amendment partway through the day, and a change
only reaches the batch if it is in force early enough.
"""
from __future__ import annotations

from copy import deepcopy

from .base import digest, outcome, schema, stream

NAME = "pricing"
MERCHANT = "m-harbor"
PLANS = {"basic": 1900, "pro": 4900, "team": 12900}
LEAD = 2.0  # a change must be in force this long before the batch to reach it


def generate(seed, ctx):
    r = stream(seed, "day:pricing")
    agreed = {plan: old + r.choice([300, 400, 500, 600, 800, 1000]) for plan, old in PLANS.items()}
    wrong = r.choice(sorted(PLANS))
    cycle = r.randint(20, 40)
    cutoff = cycle - r.randint(4, 8)
    percent = r.choice([10, 20, 25])
    ids = [f"sub-{n}" for n in sorted(r.sample(range(60000, 70000), r.randint(12, 16)))]
    subs = {}
    for k, sub in enumerate(ids):
        old, committed = (k % 4 in (0, 1), k % 4 in (0, 2)) if k < 8 else (r.random() < 0.35, r.random() < 0.4)  # every combination occurs
        subs[sub] = {"plan": sorted(PLANS)[k % 3] if k < 6 else r.choice(sorted(PLANS)),
                     "started_cycle": r.randint(cutoff - 9, cutoff - 1) if old else r.randint(cutoff, cycle - 1), "committed_term": committed}
    plain = [s for s, row in subs.items() if row["started_cycle"] >= cutoff]
    p = {"owner": r.choice(["maya", "daniel"]), "agreed": agreed, "cycle": cycle, "cutoff": cutoff, "percent": percent, "subscriptions": subs,
         "named": sorted(r.sample(plain, 1)), "deferred_plan": r.choice(sorted(PLANS)),
         "amend_at": float(r.randint(13, 18)), "runs_at": float(r.randint(28, 34))}
    # Riley's draft: an earlier version of the amendment, with the discount given to every committed subscription
    # and nobody kept on their old price.
    prices = {**agreed, wrong: agreed[wrong] + r.choice([-200, -100, 100, 200])}
    p["draft"] = {"prices": prices, "overrides": {s: prices[row["plan"]] * (100 - percent) // 100 for s, row in subs.items() if row["committed_term"]}}
    return p


def owed(p, sub, amended):
    row = p["subscriptions"][sub]
    if sub in p["named"] or row["started_cycle"] < p["cutoff"] or (amended and row["plan"] == p["deferred_plan"]):
        return PLANS[row["plan"]]
    new = p["agreed"][row["plan"]]
    return new * (100 - p["percent"]) // 100 if row["committed_term"] else new


def charge_batch(p, table, amended):
    """What the renewal batch charges each subscription, and what the amendment says it owes."""
    rows = {}
    for sub, row in sorted(p["subscriptions"].items()):
        charged = PLANS[row["plan"]] if table is None else table["overrides"].get(sub, table["prices"][row["plan"]])
        rows[sub] = {"plan": row["plan"], "charged": charged, "owed": owed(p, sub, amended)}
    return rows


def in_force_at(st, t):
    """The table the batch would use at ``t``: the latest one in force for at least the lead time."""
    ready = [a for a in st["applied"] if a["at"] <= t - LEAD]
    return ready[-1] if ready else None


def initial(p):
    return {"submitted": None, "applied": [], "amended": False, "batch": None}


def note(p):
    draft = ", ".join(f"{plan} {cents}" for plan, cents in sorted(p["draft"]["prices"].items()))
    return (f"{MERCHANT} price change. Their new prices start with this cycle's renewal batch. I entered a draft from the amendment I "
            f"was sent ({draft}, in cents, with the committed-term discount as overrides; it is in price_table). Needs Nina's "
            "review and applying before the batch.")


def card(p):
    return {"title": f"{MERCHANT} price change", "left_as": "in progress", "due_at": p["runs_at"],
            "note": "Must be in force before the merchant's renewal batch runs."}


POLICY = {"price_changes": (
    "A merchant's prices change only as its signed contract amendment says. The account owner holds the amendment and is told "
    "of any change the merchant makes to it. An amendment states rules; which subscriptions they apply to is worked out from "
    "the subscription data (price_table). A price change is submitted as the full plan price table in cents, plus an override "
    "for every subscription that is to be charged something other than its plan's price in that table. It is reviewed by Nina, "
    "then applied, and can be replaced the same way until the renewal batch runs. The batch charges each subscription from the "
    f"latest table that had been in force for at least {LEAD:g} time units when it runs; a later change does not reach it. A "
    "customer charged more than the amendment allows is harmed; a customer charged less is revenue the merchant loses.")}


def change_id(prices, overrides):
    return digest({"prices": prices, "overrides": dict(sorted(overrides.items()))})


def submit_price_change(day, args, now):
    st = day[NAME]
    if st["batch"] is not None:
        raise ValueError("the renewal batch has run; this cycle's prices can no longer change")
    prices, overrides = dict(args["prices"]), dict(args.get("overrides") or {})
    st["submitted"] = {"change_id": change_id(prices, overrides), "prices": prices, "overrides": overrides, "at": now, "review": None}


def apply_price_change(day, args, now):
    st = day[NAME]
    change = st["submitted"]
    if st["batch"] is not None:
        raise ValueError("the renewal batch has run; this cycle's prices can no longer change")
    if not change or not change["review"] or not change["review"]["approved"]:
        raise ValueError("the submitted price change has not been approved by Nina's review")
    st["applied"].append({k: deepcopy(change[k]) for k in ("change_id", "prices", "overrides")} | {"at": now})


CENTS = {"type": "integer", "minimum": -1000000, "maximum": 10000000}
TOOLS = {
    "submit_price_change": (submit_price_change, f"Replace the submitted price change for {MERCHANT}: the full plan price table in cents and an override (subscription id to cents) for every subscription charged something else. Needs Nina's review before it can be applied.",
                            schema({"prices": {"type": "object", "minProperties": 1, "maxProperties": 8, "additionalProperties": CENTS},
                                    "overrides": {"type": "object", "maxProperties": 100, "additionalProperties": CENTS}}, ("prices",)), False),
    "apply_price_change": (apply_price_change, "Put the submitted, reviewed price change in force (see policies.price_changes for when it reaches the batch).", schema(), False),
}


def price_table(day, now):
    p, st = day["params"][NAME], day[NAME]
    submitted = st["submitted"] and {k: deepcopy(st["submitted"][k]) for k in ("change_id", "prices", "overrides", "at")}
    return {"as_of": now, "merchant_id": MERCHANT, "account_owner": p["owner"], "renewal_batch_at": p["runs_at"], "current_cycle": p["cycle"],
            "current_prices": dict(PLANS), "subscriptions": deepcopy(p["subscriptions"]), "riley_draft": deepcopy(p["draft"]),
            "submitted": submitted, "in_force": deepcopy(st["applied"][-1]) if st["applied"] else None, "batch": deepcopy(st["batch"])}


VIEWS = {"price_table": (price_table, f"Read {MERCHANT}'s plans and subscriptions, Riley's draft, the submitted change and the prices in force.")}

TOPICS = {"price_terms": {"who": ("maya", "daniel"), "about": "what a merchant's signed contract amendment says about its prices (not its refund term or fee). items: the merchant id, such as \"m-harbor\".",
                          "trigger": r"amendment|price", "items": r"\bm-[a-z0-9]+\b"}}


def reply(day, topic, actor, items, now):
    p, st, said = day["params"][NAME], day[NAME], []
    for merchant in dict.fromkeys(items):
        if merchant != MERCHANT:
            said.append(f"I hold no price amendment for {merchant}.")
        elif p["owner"] != actor:
            said.append(f"{MERCHANT} is not my account.")
        else:
            said.append(f"Signed amendment for {MERCHANT}, new plan prices in cents from this cycle: "
                        + ", ".join(f"{plan} {cents}" for plan, cents in sorted(p["agreed"].items()))
                        + f". A subscription that started before cycle {p['cutoff']} keeps its current price, whatever else applies. "
                        f"A subscription on a committed term pays the new price less {p['percent']} percent. "
                        f"{', '.join(p['named'])} keeps its current price by name."
                        + (f" Changed today: the {p['deferred_plan']} plan's change is deferred to the next cycle, so every "
                           f"{p['deferred_plan']} subscription pays its current price this cycle." if st["amended"] else ""))
    return said


def review(p, change):
    """Nina's database-safety review. She does not know the contract."""
    issues = [f"plan {plan} has no price" for plan in sorted(PLANS) if plan not in change["prices"]]
    for plan, cents in sorted(change["prices"].items()):
        if plan not in PLANS:
            issues.append(f"{plan} is not a plan of {MERCHANT}")
        elif cents <= 0:
            issues.append(f"price of {plan} is not positive")
    for sub, cents in sorted(change["overrides"].items()):
        if sub not in p["subscriptions"]:
            issues.append(f"{sub} is not a subscription of {MERCHANT}")
        elif cents <= 0:
            issues.append(f"override for {sub} is not positive")
    return issues


def turn(day, actor, now):
    p, change = day["params"][NAME], day[NAME]["submitted"]
    if actor != "nina" or not change or change["review"] is not None:
        return []
    issues = review(p, change)
    change["review"] = {"change_id": change["change_id"], "approved": not issues, "issues": issues, "at": now}
    return [f"Reviewed price change {change['change_id'][:12]}: it is safe to apply." if not issues else
            f"Price change {change['change_id'][:12]} is not approved: " + " | ".join(issues)]


def tick(day, t):
    p, st = day["params"][NAME], day[NAME]
    if p["amend_at"] == t:
        st["amended"] = True
        day["pings"].append({"ping_id": "note-pricing", "from": p["owner"], "at": t,
                             "text": f"{MERCHANT} has changed its amendment: the {p['deferred_plan']} plan's price change moves to the next "
                                     f"cycle. This cycle every {p['deferred_plan']} subscription pays its current price."})
    if p["runs_at"] == t and st["batch"] is None:
        st["batch"] = {"at": t, "rows": charge_batch(p, in_force_at(st, t), st["amended"])}


def result(day):
    p, st = day["params"][NAME], day[NAME]
    batch = (st["batch"] or {}).get("rows") or charge_batch(p, in_force_at(st, p["runs_at"]), True)
    right = sum(row["charged"] == row["owed"] for row in batch.values())
    # At stake: the subscriptions whose price was meant to change, plus any that ends wrong.
    stake = [s for s, row in batch.items() if row["owed"] != PLANS[row["plan"]] or row["charged"] != row["owed"]]
    good = sum(batch[s]["charged"] == batch[s]["owed"] for s in stake)
    return outcome(right == len(batch), good / len(stake) if stake else 1.0, (), correct=right, subscriptions=len(batch),
                   overcharged=sorted(s for s, row in batch.items() if row["charged"] > row["owed"]))
