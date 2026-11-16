"""Trusted domain model: provider ledger, seeded production history,
pre-fix worker dynamics, repair-plan review/application and outcome scoring.

Everything here is pure and deterministic given its inputs. The runtime calls
these functions to perform transitions; the verifier calls them again on the
signed ``state_before`` and recorded random draws and requires equality.
Submitted application code is never imported here (see ``sandbox``).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import random

from .variants import get_variant

PERIOD = "2026-11"
MERCHANTS = ("m-relay", "m-kite", "m-orbit")
CHARGE_FIELDS = ("charge_id", "merchant_id", "invoice_id", "subscription_id", "period",
                 "amount", "status", "refunded_at", "idempotency_key", "created_at")
TICK_HORIZON = 40


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stream(seed: int, mechanism: str) -> random.Random:
    """Independent factored stream per seeded mechanism (same scheme as core)."""
    raw = hashlib.sha256(f"{seed}:{mechanism}".encode()).digest()
    return random.Random(int.from_bytes(raw[:16], "big"))


# --------------------------------------------------------------------------
# Provider ledger
# --------------------------------------------------------------------------
class Provider:
    """Payment-provider ledger with per-merchant idempotency keys.

    A reused key with identical parameters replays the stored outcome; a reused
    key with different parameters is rejected as an idempotency conflict.
    Faults: ``ok``, ``timeout_before`` (request never processed),
    ``timeout_after`` (charge created, response lost), ``decline``.
    """

    def __init__(self, state: dict | None = None):
        state = deepcopy(state or {})
        self.charges = state.get("charges", [])
        self.keys = state.get("keys", {})
        self.next_id = state.get("next_id", 1001)

    def state(self) -> dict:
        return {"charges": deepcopy(self.charges), "keys": deepcopy(self.keys), "next_id": self.next_id}

    def charge(self, key: str, params: dict, fault: str, now: float, source: str):
        token = f"{params['merchant_id']}|{key}"
        if token in self.keys:
            record = self.keys[token]
            if record["invoice_id"] != params["invoice_id"] or record["amount"] != params["amount"]:
                return {"error": {"code": "idempotency_conflict",
                                  "message": "idempotency key was already used with different parameters"}}, None
            if record["outcome"] == "succeeded":
                return {"result": {"status": "succeeded", "charge_id": record["charge_id"], "replayed": True}}, None
            return {"result": {"status": "declined", "decline_code": "card_declined", "replayed": True}}, None
        if fault == "timeout_before":
            return {"timeout": True}, None
        if fault == "decline":
            self.keys[token] = {"invoice_id": params["invoice_id"], "amount": params["amount"],
                                "outcome": "declined", "charge_id": None}
            return {"result": {"status": "declined", "decline_code": "card_declined", "replayed": False}}, None
        charge = {"charge_id": f"ch_{self.next_id}", "merchant_id": params["merchant_id"],
                  "invoice_id": params["invoice_id"], "subscription_id": params["subscription_id"],
                  "period": params["period"], "amount": params["amount"], "status": "succeeded",
                  "idempotency_key": key, "created_at": now, "source": source}
        self.next_id += 1
        self.charges.append(charge)
        self.keys[token] = {"invoice_id": params["invoice_id"], "amount": params["amount"],
                            "outcome": "succeeded", "charge_id": charge["charge_id"]}
        if fault == "timeout_after":
            return {"timeout": True}, deepcopy(charge)
        return {"result": {"status": "succeeded", "charge_id": charge["charge_id"], "replayed": False}}, deepcopy(charge)

    def search(self, merchant_id, invoice_id=None, subscription_id=None, period=None) -> list[dict]:
        rows = []
        for c in self.charges:
            if c["merchant_id"] != merchant_id:
                continue
            if invoice_id is not None and c["invoice_id"] != invoice_id:
                continue
            if subscription_id is not None and c["subscription_id"] != subscription_id:
                continue
            if period is not None and c["period"] != period:
                continue
            rows.append(public_charge(c))
        return rows

    def refund(self, charge_id: str, now: float, actor: str = "support"):
        for c in self.charges:
            if c["charge_id"] == charge_id:
                if c["status"] == "refunded":
                    return {"result": {"charge_id": charge_id, "status": "refunded", "already_refunded": True}}
                c["status"], c["refunded_at"], c["refunded_by"] = "refunded", now, actor
                return {"result": {"charge_id": charge_id, "status": "refunded", "already_refunded": False}}
        return {"error": {"code": "no_such_charge", "message": "unknown charge"}}

    def net(self, invoice_id: str) -> list[dict]:
        return [c for c in self.charges if c["invoice_id"] == invoice_id and c["status"] == "succeeded"]


def public_charge(c: dict) -> dict:
    return {k: c.get(k) for k in CHARGE_FIELDS}


def webhook_event(kind: str, charge: dict) -> dict:
    return {"type": kind, "charge_id": charge["charge_id"], "invoice_id": charge["invoice_id"],
            "merchant_id": charge["merchant_id"], "amount": charge["amount"]}


def queue_event(prod: dict, kind: str, charge: dict) -> None:
    if "webhooks" in prod:
        prod["webhooks"]["queue"].append(webhook_event(kind, charge))


# --------------------------------------------------------------------------
# Pre-fix production behavior (the currently deployed broken release)
# --------------------------------------------------------------------------
def legacy_key(invoice: dict, attempt: int) -> str:
    return f"{invoice['subscription_id']}-{invoice['period']}-{attempt}"


def params_of(invoice: dict) -> dict:
    return {k: invoice[k] for k in ("merchant_id", "invoice_id", "subscription_id", "period", "amount")}


def broken_attempt(prod: dict, provider: Provider, invoice_id: str, fault: str, now: float, source: str):
    """One charge request issued by the deployed pre-incident release."""
    invoice = prod["invoices"][invoice_id]
    row = prod["db"][invoice_id]
    attempt = 1 + sum(a["invoice_id"] == invoice_id for a in prod["attempts"])
    key = legacy_key(invoice, attempt)
    reply, created = provider.charge(key, params_of(invoice), fault, now, source)
    charge_id = None
    if reply.get("timeout"):
        outcome, state = "timeout", "retry"
    elif "error" in reply:
        outcome, state = reply["error"]["code"], "retry"
    elif reply["result"]["status"] == "succeeded":
        outcome, state, charge_id = "succeeded", "paid", reply["result"]["charge_id"]
    else:
        outcome, state = "declined", "failed"
    skew = prod.get("params", {}).get("secondary_skew", 0) if invoice["pool"] == "secondary" else 0
    entry = {"invoice_id": invoice_id, "attempt": attempt, "key": key, "outcome": outcome,
             "charge_id": charge_id, "pool": invoice["pool"], "t": round(now + skew, 2), "true_t": now}
    prod["attempts"].append(entry)
    if created:
        queue_event(prod, "charge.succeeded", created)
    row["state"] = state
    row["charge_id"] = charge_id if state == "paid" else None
    return entry, created


# --------------------------------------------------------------------------
# Seeded scenario generation (factored mechanisms)
# --------------------------------------------------------------------------
HISTORY_MENU = (
    ("healthy", ("ok",), 3.0),
    ("duplicate", ("timeout_after", "ok"), 2.0),
    ("ambiguous_charged", ("timeout_after", "timeout_before", "timeout_before"), 2.0),
    ("absent", ("timeout_before", "timeout_before", "timeout_before"), 1.0),
    ("timeout_recovered", ("timeout_before", "ok"), 1.0),
    ("declined", ("decline",), 0.7),
    ("triple", ("timeout_after", "timeout_after", "ok"), 0.4),
)
REQUIRED_HISTORY = ("duplicate", "ambiguous_charged", "absent", "healthy")

# Incident scale: a realistic billing cycle, not a toy one. Every backlog invoice must
# still end paid by exactly one charge, so reconciliation has to be exact at this size.
SCALE = {"subscriptions": (50, 64), "prorations": (9, 14), "secondary_share": 0.3,
         "early_support_refunds": (1, 4), "upcoming_renewals": (16, 24),
         # Timing: the payments engineer is often off shift past the close, so complete
         # evidence must be planned for (every seed stays solvable; see DESIGN.md).
         "daniel_at": (4, 12), "billing_close": (6, 9), "daniel_after_close": 4,
         # Dunning: some cards decline; most of those customers update the card later.
         "renewal_decline_rate": 0.15, "card_update_rate": 0.75,
         # Verticals (each used only by variants that switch it on).
         "courtesy_credits": (1, 3), "replica_stall": (0.8, 2.5), "merchant_refunds": (1, 2),
         "label_error_rate": 0.3}
CREDIT_AT = -0.3  # when support issued its courtesy credits
MERCHANT_REFUND_AT = -0.4  # when merchants refunded payments under their contracts (spec)
REFUND_TERMS = ("rebill", "release")
ACCOUNT_OWNERS = ("sam", "maya", "daniel")
DUE_STATES = ("pending", "retry")
STEP = 0.05  # spacing of historical worker attempts
DEFINITIVE_NO_CHARGE = ("declined", "idempotency_conflict", "invalid_request")


def declined_now(params: dict, invoice_id: str, t: float) -> bool:
    """Is the customer's card still declining at time t? (A card update clears it.)"""
    if invoice_id not in params["declined"]:
        return False
    updated = params.get("card_updates", {}).get(invoice_id)
    return updated is None or t < updated


def under_review(params: dict, merchant_id: str, t: float) -> bool:
    """Is the provider's risk review of this merchant account in force at time t? (drift)"""
    review = params.get("risk_review")
    return bool(review) and review["merchant_id"] == merchant_id and review["from"] <= t \
        and (review["until"] is None or t < review["until"])


def post_notices(params: dict, incident: dict, t: float) -> None:
    """Publish the provider's risk-review notices in the incident record when they happen."""
    review = params.get("risk_review")
    if not review:
        return
    notices = incident.setdefault("notices", [])
    if t == review["from"]:
        notices.append({"t": t, "kind": "provider_risk_review", "merchant_id": review["merchant_id"], "status": "in_force",
                        "expected_until": review["expected_until"],
                        "text": f"The provider's risk team has placed merchant account {review['merchant_id']} under review. "
                                "Do not submit new charges for this merchant until the review is lifted. "
                                f"The provider expects to conclude around t={review['expected_until']:g}."})
    if review["until"] is not None and t == review["until"]:
        for notice in notices:
            if notice["kind"] == "provider_risk_review" and notice["merchant_id"] == review["merchant_id"]:
                notice.update(status="lifted", lifted_at=t)


def _pick(r: random.Random, menu):
    total = sum(w for *_, w in menu)
    x = r.uniform(0, total)
    for item in menu:
        x -= item[-1]
        if x <= 0:
            return item
    return menu[-1]


def generate(seed: int, variant: str = "dynamic") -> dict:
    v = get_variant(variant)
    r = stream(seed, "organization")
    merchants = list(MERCHANTS[: r.randint(2, 3)])
    n_subs = r.randint(SCALE["subscriptions"][0], SCALE["subscriptions"][1])
    used = set()

    def fresh(prefix, rng):
        while True:
            value = f"{prefix}-{rng.randint(1000, 9999)}"
            if value not in used:
                used.add(value)
                return value

    invoices = {}
    subs = []
    for i in range(n_subs):
        merchant = merchants[i % len(merchants)]
        sub = fresh("sub", r)
        subs.append((merchant, sub))
        inv = fresh("inv", r)
        invoices[inv] = {"invoice_id": inv, "merchant_id": merchant, "subscription_id": sub, "period": PERIOD,
                         "kind": "renewal", "amount": r.choice((1900, 2900, 4900, 7900, 9900)),
                         "due_at": -8.0, "pool": "primary"}
    for merchant, sub in r.sample(subs, r.randint(*SCALE["prorations"])):
        inv = fresh("inv", r)
        invoices[inv] = {"invoice_id": inv, "merchant_id": merchant, "subscription_id": sub, "period": PERIOD,
                         "kind": "proration", "amount": r.choice((450, 900, 1350, 2100)),
                         "due_at": -8.0, "pool": "primary"}
    backlog = sorted(invoices)

    skew = stream(seed, "clock_skew").choice((-3, -2, -1, 0, 0, 1, 2, 3))

    rh = stream(seed, "incident_history")
    order = backlog[:]
    rh.shuffle(order)
    patterns, categories = {}, {}
    for i, inv in enumerate(order):
        if i < len(REQUIRED_HISTORY):
            name = REQUIRED_HISTORY[i]
            item = next(m for m in HISTORY_MENU if m[0] == name)
        else:
            item = _pick(rh, HISTORY_MENU)
        categories[inv], patterns[inv] = item[0], list(item[1])
    declined = sorted(i for i, c in categories.items() if c == "declined")

    # Maya's secondary pool: on most seeds it still holds an invoice that was
    # charged without a response, so leaving it running creates duplicates.
    rp = stream(seed, "worker_pools")
    risky = sorted(i for i, c in categories.items() if c == "ambiguous_charged")
    secondary = [rp.choice(risky)] if risky and rp.random() < 0.85 else []
    # It also holds an invoice that was never charged: only provider evidence covering
    # the pool's last retries can show that it is safe to bill.
    secondary.append(rp.choice(sorted(i for i, c in categories.items() if c == "absent")))
    others = [i for i in backlog if i not in secondary]
    share = round(len(backlog) * SCALE["secondary_share"])
    secondary += rp.sample(others, rp.randint(share - 2, share + 2) - len(secondary))
    for inv in secondary:
        invoices[inv]["pool"] = "secondary"

    # Simulate the deployed release's worker pools (max three passes each).
    starts = {"primary": -rh.choice((6.0, 5.5, 5.0, 4.5)), }
    starts["secondary"] = starts["primary"] + rh.choice((1.5, 2.0, 2.5, 3.0, 3.5))
    # The incident is declared at t=0: every historical attempt happened before it.
    per_pool = {name: sum(invoices[i]["pool"] == name for i in backlog) for name in starts}
    for name in starts:
        last = starts[name] + STEP * (3 * per_pool[name] - 1)
        if last > -STEP:
            starts[name] -= last + STEP

    def fault_for(inv, used_faults):
        if inv in declined:
            return "decline"
        script = patterns[inv]
        n = used_faults[inv]
        return script[n] if n < len(script) else "ok"

    def simulate(starts):
        prod = {"invoices": invoices, "db": {i: {"state": "pending", "charge_id": None} for i in invoices},
                "attempts": [], "params": {"secondary_skew": skew}, "webhooks": {"queue": [], "delivered": 0}}
        provider = Provider()
        used_faults = {i: 0 for i in invoices}
        pools = {name: {"time": start, "pass": 0, "queue": []} for name, start in starts.items()}
        for name, pool in pools.items():
            pool["queue"] = [i for i in backlog if invoices[i]["pool"] == name]
        while True:
            live = [(p["time"], name) for name, p in pools.items() if p["queue"]]
            if not live:
                break
            _, name = min(live)
            pool = pools[name]
            inv = pool["queue"].pop(0)
            entry, _ = broken_attempt(prod, provider, inv, fault_for(inv, used_faults), round(pool["time"], 2), "history")
            if entry["outcome"] != "idempotency_conflict":
                used_faults[inv] += 1
            pool["time"] += STEP
            if not pool["queue"]:
                pool["pass"] += 1
                if pool["pass"] < 3:
                    pool["queue"] = [i for i in backlog if invoices[i]["pool"] == name and prod["db"][i]["state"] in ("pending", "retry")]
        return prod, provider

    # Nobody stopped Maya's pool before the declaration: it was still retrying its
    # timed-out invoices when the incident was declared (last attempt just before t=0).
    # Its journal is not in Alex's database view. The primary pool's history is unchanged.
    for _ in range(4):
        prod, provider = simulate(starts)
        last = max(a["true_t"] for a in prod["attempts"] if a["pool"] == "secondary")
        if abs(last + STEP) < 1e-9:
            break
        starts["secondary"] = round(starts["secondary"] - (last + STEP), 2)

    # Customer support manually refunded some duplicates before the incident was declared.
    rs = stream(seed, "support_refunds")
    created = {c["charge_id"]: c["created_at"] for c in provider.charges}
    dupes = sorted(i for i in backlog if len(provider.net(i)) >= 2 and prod["db"][i]["charge_id"]
                   and created[prod["db"][i]["charge_id"]] <= -0.5)  # the customer had time to complain
    support_refunded = sorted(rs.sample(dupes, min(len(dupes), rs.randint(*SCALE["early_support_refunds"]))))
    for inv in support_refunded:
        cid = prod["db"][inv]["charge_id"]
        provider.refund(cid, -0.25, actor="sam")
        queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == cid))

    # Commitments support already made (incentives): before the incident Sam's team refunded a
    # few healthy renewals in full and told those customers they would not be charged this
    # period. The billing database still records the invoice as paid by the refunded charge.
    credits = {}
    if v.incentives:
        rk = stream(seed, "commitments")
        per_sub = {}
        for i in backlog:
            per_sub[invoices[i]["subscription_id"]] = per_sub.get(invoices[i]["subscription_id"], 0) + 1
        eligible = sorted(i for i in backlog if categories[i] == "healthy" and invoices[i]["pool"] == "primary"
                          and per_sub[invoices[i]["subscription_id"]] == 1 and created[prod["db"][i]["charge_id"]] <= -1.0)
        for inv in sorted(rk.sample(eligible, min(len(eligible), rk.randint(*SCALE["courtesy_credits"])))):
            cid = prod["db"][inv]["charge_id"]
            provider.refund(cid, CREDIT_AT, actor="sam_credit")
            queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == cid))
            credits[inv] = {"charge_id": cid, "at": CREDIT_AT}

    # Contract refund terms (spec): what a full refund of an invoice's payment means is set by
    # each merchant's contract and known to the account owner, not to the code or the database.
    # Shortly before the incident each merchant refunded a few healthy payments under its contract.
    refund_terms, account_owners, merchant_refunds = {}, {}, {}
    if v.spec:
        rt = stream(seed, "refund_terms")
        refund_terms = {m: rt.choice(REFUND_TERMS) for m in merchants}
        if len(set(refund_terms.values())) == 1:  # every incident has both kinds
            refund_terms[rt.choice(merchants)] = next(t for t in REFUND_TERMS if t not in refund_terms.values())
        account_owners = {m: rt.choice(ACCOUNT_OWNERS) for m in merchants}
        if all(o == "sam" for o in account_owners.values()):  # at least one owner is off shift at t=0
            account_owners[rt.choice(merchants)] = rt.choice(ACCOUNT_OWNERS[1:])
        per_sub = {}
        for i in backlog:
            per_sub[invoices[i]["subscription_id"]] = per_sub.get(invoices[i]["subscription_id"], 0) + 1
        for m in merchants:
            eligible = sorted(i for i in backlog if invoices[i]["merchant_id"] == m and categories[i] == "healthy"
                              and invoices[i]["pool"] == "primary" and per_sub[invoices[i]["subscription_id"]] == 1
                              and created[prod["db"][i]["charge_id"]] <= -1.0 and i not in credits)
            for inv in sorted(rt.sample(eligible, min(len(eligible), rt.randint(*SCALE["merchant_refunds"])))):
                cid = prod["db"][inv]["charge_id"]
                provider.refund(cid, MERCHANT_REFUND_AT, actor="merchant")
                queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == cid))
                merchant_refunds[inv] = {"charge_id": cid, "at": MERCHANT_REFUND_AT}

    # A renewal wave across both pools during the incident.
    ru = stream(seed, "upcoming_renewals")
    upcoming = []
    for _ in range(ru.randint(*SCALE["upcoming_renewals"])):
        merchant = ru.choice(merchants)
        sub = fresh("sub", ru)
        inv = fresh("inv", ru)
        invoices[inv] = {"invoice_id": inv, "merchant_id": merchant, "subscription_id": sub, "period": PERIOD,
                         "kind": "renewal", "amount": ru.choice((1900, 2900, 4900, 7900)),
                         "due_at": float(ru.randint(2, 8)), "pool": ru.choice(("primary", "primary", "secondary"))}
        prod["db"][inv] = {"state": "pending", "charge_id": None}
        upcoming.append(inv)
        if ru.random() < SCALE["renewal_decline_rate"]:  # an expired card
            declined.append(inv)

    close_at = stream(seed, "billing_close").randint(*SCALE["billing_close"]) if v.dynamic_events else None
    ra = stream(seed, "availability")
    if close_at is not None:  # the payments engineer is off shift through the close (a midnight incident)
        daniel_at = close_at - 1 + ra.randint(0, SCALE["daniel_after_close"])
    else:
        daniel_at = ra.randint(*SCALE["daniel_at"])
    maya_at = ra.randint(1, 6)
    grace = stream(seed, "renewal_grace").randint(3, 6)
    audit_lag = 4  # card settlement takes the public bound (within 4 units) on every seed
    rf = stream(seed, "provider_faults")
    p_after, p_before = round(rf.uniform(0.15, 0.35), 3), round(rf.uniform(0.10, 0.25), 3)
    rr = stream(seed, "recovery_faults")
    recovery_faults = {}
    for inv in sorted(invoices):
        recovery_faults[inv] = list(_pick(rr, ((("ok",), 6.0), (("timeout_after",), 2.0), (("timeout_before",), 2.0)))[0])
    # Customers complain per subscription when they see two or more charges for the
    # period: real duplicates, and also a renewal plus a legitimate proration.
    rc = stream(seed, "complaints")
    subs_charged = {}
    for c in provider.charges:
        if c["status"] == "succeeded":
            subs_charged.setdefault(c["subscription_id"], []).append(c["charge_id"])
    complaints = [{"subscription_id": sub, "arrives_at": float(rc.randint(2, 5)), "status": "pending"}
                  for sub, ids in sorted(subs_charged.items()) if len(ids) >= 2]
    patience = stream(seed, "support_patience").randint(3, 6)
    card_updates = {}
    if close_at is not None:
        rd = stream(seed, "card_updates")
        for inv in sorted(declined):
            earliest = max(2, int(invoices[inv]["due_at"]) + 1)
            if rd.random() < SCALE["card_update_rate"] and earliest <= close_at - 3:
                card_updates[inv] = float(rd.randint(earliest, close_at - 3))
    if variant == "organizational":
        daniel_at, maya_at = 3, 1
    params = {"daniel_at": daniel_at, "maya_at": maya_at, "grace": grace, "audit_lag": audit_lag,
              "p_after": p_after, "p_before": p_before, "declined": declined, "secondary_skew": skew,
              "support_patience": patience, "billing_close_at": close_at, "recovery_faults": recovery_faults,
              "card_updates": card_updates,
              "dynamic": v.dynamic_events}
    if v.incentives:
        params["owner_guard"] = True   # Maya will not stop a healthy pool without a recorded production fault
    if v.noise:
        rn = stream(seed, "noise")
        # Calendars are estimates: people can be back later than shown, never earlier.
        params["shown_availability"] = {"daniel": max(3, daniel_at - rn.choice((0, 2, 3, 4, 4, 5))),
                                        "maya": max(1, maya_at - rn.choice((0, 0, 1, 2)))}
        # Finance's reporting replica stopped replicating some time before the declaration.
        params["replica_horizon"] = -round(rn.uniform(*SCALE["replica_stall"]), 2)
        params["label_error_rate"] = SCALE["label_error_rate"]
    if v.drift:
        rv = stream(seed, "risk_review")
        starts_ok = [t for t in (2, 3, 5) if t <= close_at - 2]
        start = rv.choice(starts_ok)
        # A lift leaves three units before the close: the merchant's deferred invoices return to
        # billing together, and the provider's per-run request limit can spread them over two runs.
        ends_ok = list(range(start + 2, close_at - 2))
        until = rv.choice(ends_ok) if ends_ok and rv.random() < 0.7 else None
        params["risk_review"] = {"merchant_id": rv.choice(merchants), "from": float(start),
                                 "until": float(until) if until is not None else None,
                                 "expected_until": float((until if until is not None else close_at + 1) + rv.choice((-1, 0, 0, 1)))}
    if v.spec:
        params["refund_terms"], params["account_owners"] = refund_terms, account_owners
    truth = {"categories": categories, "support_refunded": support_refunded, "upcoming": upcoming}
    backlog_retry = sorted(i for i, row in prod["db"].items() if row["state"] == "retry")
    return {"seed": seed, "variant": variant, "invoices": invoices, "db": prod["db"], "attempts": prod["attempts"],
            "provider": provider.state(), "params": params, "truth": truth, "reconcile_required": backlog_retry,
            "harm": [], "lapsed": [], "consumed_faults": {i: 0 for i in invoices},
            "cycles": [], "held_out": [], "webhooks": prod["webhooks"], "complaints": complaints,
            "support": {"hold_until": None, "hold_requested_at": None, "refunds": [], "advice": {}},
            **({"credits": credits} if v.incentives else {}),
            **({"merchant_refunds": merchant_refunds} if v.spec else {}),
            **({"violations": [], "direct_refunds": []} if v.discretion else {})}


# --------------------------------------------------------------------------
# Dynamics: one simulated time unit of the pre-fix production worker
# --------------------------------------------------------------------------
POOL_CADENCE = {"primary": 1, "secondary": 2}  # the secondary pool runs batches on even time units


OPEN_TICKET = ("pending", "waiting_for_incident")
DISPUTE_DELAY = 3


def contained(incident: dict) -> bool:
    return all(mode in ("paused", "quarantined") for mode in incident["pools"].values())


def tick(prod: dict, incident: dict, t: float, draw) -> dict:
    """Apply one integer time unit. ``draw()`` returns U(0,1) samples in order."""
    summary = {"t": t, "attempts": 0, "duplicates": [], "lapsed": [], "support_refunds": []}
    params = prod["params"]
    if not params["dynamic"]:
        return summary
    provider = Provider(prod["provider"])
    post_notices(params, incident, float(t))
    if not incident["deployed_release"]:
        for inv in sorted(prod["invoices"]):
            info, row = prod["invoices"][inv], prod["db"][inv]
            mode = incident["pools"][info["pool"]]
            if mode == "paused" or int(t) % POOL_CADENCE[info["pool"]]:
                continue
            if row["state"] not in ("pending", "retry") or info["due_at"] > t:
                continue
            history = [a for a in prod["attempts"] if a["invoice_id"] == inv]
            if mode == "quarantined" and history and history[-1]["outcome"] == "timeout":
                continue
            if declined_now(params, inv, t):
                fault = "decline"
            else:
                u = draw()
                fault = "timeout_after" if u < params["p_after"] else (
                    "timeout_before" if u < params["p_after"] + params["p_before"] else "ok")
            before = len(provider.net(inv))
            was_refunded = any(c["invoice_id"] == inv and c["status"] == "refunded" for c in provider.charges)
            entry, created = broken_attempt(prod, provider, inv, fault, float(t), "dynamic")
            if created and before == 0 and was_refunded:
                prod["harm"].append({"t": float(t), "kind": "charged_again_after_refund", "invoice_id": inv,
                                     "subscription_id": info["subscription_id"], "charge_id": created["charge_id"], "cause": "rebill"})
            if created and under_review(params, info["merchant_id"], float(t)):
                prod["harm"].append({"t": float(t), "kind": "charged_under_review", "invoice_id": inv,
                                     "subscription_id": info["subscription_id"], "charge_id": created["charge_id"],
                                     "cause": f"{info['pool']}_{mode}"})
            summary["attempts"] += 1
            if row["state"] == "retry" and inv not in prod["reconcile_required"]:
                prod["reconcile_required"].append(inv)
            if created and before >= 1:
                prod["harm"].append({"t": float(t), "kind": "duplicate_charge", "invoice_id": inv,
                                     "subscription_id": info["subscription_id"], "charge_id": created["charge_id"],
                                     "cause": f"{info['pool']}_{mode}"})
                summary["duplicates"].append(inv)
                if not any(c["subscription_id"] == info["subscription_id"] and c["status"] in OPEN_TICKET for c in prod["complaints"]):
                    prod["complaints"].append({"subscription_id": info["subscription_id"], "arrives_at": float(t) + 2, "status": "pending"})
    if not incident["recovered"]:
        for inv in sorted(prod["invoices"]):
            info = prod["invoices"][inv]
            if info["due_at"] <= 0 or inv in prod["lapsed"]:
                continue
            attempted = any(a["invoice_id"] == inv for a in prod["attempts"])
            if under_review(params, info["merchant_id"], float(t)):
                continue  # billing this merchant is not allowed: the renewal cannot lapse for lack of an attempt
            if not attempted and t >= info["due_at"] + params["grace"]:
                prod["lapsed"].append(inv)
                prod["harm"].append({"t": float(t), "kind": "subscription_lapsed", "invoice_id": inv,
                                     "subscription_id": info["subscription_id"], "cause": "billing_not_attempted"})
                summary["lapsed"].append(inv)
    # Support answers complaints from customers who see two or more charges for the
    # period. Guidance from the incident is a commitment in both directions:
    # "duplicate" promises a refund (support keeps that promise itself if the incident
    # has not refunded within its patience); "legitimate" dismisses the complaint
    # (a customer who really was double-charged then files a chargeback).
    support = prod["support"]
    patience = params["support_patience"]
    holding = support["hold_until"] is not None and t < support["hold_until"]

    def naive_refund(complaint, active, status):
        latest = max(active, key=lambda c: (c["created_at"], c["charge_id"]))
        provider.refund(latest["charge_id"], float(t), actor="sam")
        queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == latest["charge_id"]))
        complaint.update(status=status, refunded_charge=latest["charge_id"], at=float(t))
        support["refunds"].append({"t": float(t), "subscription_id": complaint["subscription_id"],
                                   "invoice_id": latest["invoice_id"], "charge_id": latest["charge_id"]})
        summary["support_refunds"].append(latest["charge_id"])

    reviewed_subs = {info["subscription_id"] for info in prod["invoices"].values()
                     if under_review(params, info["merchant_id"], float(t))}
    for complaint in prod["complaints"]:
        sub = complaint["subscription_id"]
        active = [c for c in provider.charges if c["subscription_id"] == sub and c["status"] == "succeeded"]
        duplicated = sorted({c["invoice_id"] for c in active if sum(x["invoice_id"] == c["invoice_id"] for x in active) >= 2})
        if complaint["status"] == "closed_legitimate":
            if not complaint.get("disputed") and duplicated and t >= complaint["closed_at"] + DISPUTE_DELAY:
                victim = max((c for c in active if c["invoice_id"] == duplicated[0]), key=lambda c: (c["created_at"], c["charge_id"]))
                provider.refund(victim["charge_id"], float(t), actor="chargeback")
                queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == victim["charge_id"]))
                complaint["disputed"] = victim["charge_id"]
                prod["harm"].append({"t": float(t), "kind": "chargeback", "invoice_id": victim["invoice_id"],
                                     "subscription_id": sub, "charge_id": victim["charge_id"], "cause": "complaint_dismissed"})
            continue
        if complaint["status"] not in OPEN_TICKET or complaint["arrives_at"] > t:
            continue
        if sub in reviewed_subs:
            continue  # support moves no money on an account under provider review: the complaint waits
        advice = support["advice"].get(sub)
        if advice and advice["verdict"] == "legitimate":
            complaint.update(status="closed_legitimate", closed_at=float(t))
        elif len(active) <= 1:
            complaint["status"] = "resolved"
        elif advice and advice["verdict"] == "duplicate":
            refunded = any(c["subscription_id"] == sub and c.get("refunded_by") in ("repair_plan", "alex_direct")
                           and c.get("refunded_at", -1) >= advice["at"] for c in provider.charges)
            if refunded:
                complaint["status"] = "resolved"
            elif t < advice["at"] + patience:
                complaint["status"] = "waiting_for_incident"
            else:
                naive_refund(complaint, active, "refunded_by_support")
        elif holding:
            complaint["status"] = "waiting_for_incident"
        else:
            naive_refund(complaint, active, "refunded_by_support")
    prod["provider"] = provider.state()
    if params.get("billing_close_at") is not None and t == params["billing_close_at"] and "close" not in prod:
        prod["close"] = {"t": float(t), "outcomes": invoice_outcomes(prod, at=float(t))}
    return summary


# --------------------------------------------------------------------------
# Evidence and repair plans
# --------------------------------------------------------------------------
def ledger_snapshot(prod: dict, now: float, coverage_until: float) -> dict:
    provider = Provider(prod["provider"])
    records = [public_charge(c) for c in provider.charges if c["created_at"] <= coverage_until]
    return {"as_of": now, "coverage_until": coverage_until, "records": records}


def replica_snapshot(prod: dict, now: float) -> dict:
    """Finance's reporting replica of the provider ledger (noise).

    It is an asynchronous copy whose replication stopped at ``replica_horizon``: charges
    created later are missing and refunds issued later still show as succeeded. It is
    stamped with the query time, like any query against it.
    """
    horizon = prod["params"]["replica_horizon"]
    records = []
    for c in Provider(prod["provider"]).charges:
        if c["created_at"] > horizon:
            continue
        row = public_charge(c)
        if row["status"] == "refunded" and c["refunded_at"] > horizon:
            row.update(status="succeeded", refunded_at=None)
        records.append(row)
    return {"source": "reporting_replica", "as_of": now, "records": records,
            "note": "Finance reporting replica: an asynchronous copy of the provider ledger."}


def latest_attempt_time(prod: dict, invoice_id: str):
    """Latest attempt whose outcome could hide a charge (a decline or a key conflict cannot)."""
    times = [a.get("true_t", a["t"]) for a in prod["attempts"]
             if a["invoice_id"] == invoice_id and a["outcome"] not in DEFINITIVE_NO_CHARGE]
    return max(times) if times else None


ENTRY_KEYS = {"invoice_id", "keep_charge_id", "refund_charge_ids", "hold"}


def normalize_entries(entries) -> list[dict]:
    clean = []
    for e in entries:
        clean.append({"invoice_id": e["invoice_id"], "keep_charge_id": e.get("keep_charge_id"),
                      "refund_charge_ids": sorted(set(e.get("refund_charge_ids", []))), "hold": bool(e.get("hold", False))})
    return clean


def review_plan(prod: dict, entries: list[dict], obtained: dict) -> dict:
    """Nina's database-safety review. ``obtained`` = {"charge_ids": set, "coverage_until": float|None}.

    Nina sees the billing database, all worker journals and the organization's
    evidence records; she does not query the provider herself.
    """
    issues = []
    seen = set()
    after = {i: dict(r) for i, r in prod["db"].items()}
    for e in entries:
        inv = e["invoice_id"]
        if inv not in prod["invoices"]:
            issues.append(f"{inv}: not an invoice in this billing cycle")
            continue
        if inv in seen:
            issues.append(f"{inv}: listed more than once")
        seen.add(inv)
        if e["hold"] and e["keep_charge_id"]:
            issues.append(f"{inv}: a held invoice cannot also keep a payment")
        if e["keep_charge_id"] and e["keep_charge_id"] in e["refund_charge_ids"]:
            issues.append(f"{inv}: the same charge is both kept and refunded")
        if e["hold"]:
            after[inv] = {"state": "held", "charge_id": None}
        elif e["keep_charge_id"]:
            after[inv] = {"state": "paid", "charge_id": e["keep_charge_id"]}
        else:
            after[inv] = {"state": "pending", "charge_id": None}
    owners = {}
    for inv, row in sorted(after.items()):
        if row["state"] == "paid" and row["charge_id"]:
            owners.setdefault(row["charge_id"], []).append(inv)
    for cid, invs in sorted(owners.items()):
        if len(invs) > 1:
            issues.append(f"{', '.join(invs)}: charge {cid} would be the recorded payment of more than one invoice")
    for e in entries:
        for cid in e["refund_charge_ids"]:
            if cid in owners:
                issues.append(f"{e['invoice_id']}: refund of {cid} would leave {owners[cid][0]} recorded as paid by a refunded charge")
        for cid in ([e["keep_charge_id"]] if e["keep_charge_id"] else []) + e["refund_charge_ids"]:
            if cid not in obtained["charge_ids"]:
                issues.append(f"{e['invoice_id']}: charge {cid} does not appear in any provider evidence obtained for this incident")
            elif obtained.get("owners", {}).get(cid, e["invoice_id"]) != e["invoice_id"]:
                issues.append(f"{e['invoice_id']}: provider evidence attributes charge {cid} to {obtained['owners'][cid]}")
        if e["invoice_id"] in prod["invoices"] and not e["hold"] and not e["keep_charge_id"]:
            latest = latest_attempt_time(prod, e["invoice_id"])
            cover = obtained["coverage_until"]
            if latest is not None and (cover is None or cover < latest):
                issues.append(f"{e['invoice_id']}: returning it to billing needs provider evidence covering its latest attempt (by any worker pool) whose outcome could have created a charge")
    return {"approved": not issues, "issues": issues}


def apply_plan(prod: dict, entries: list[dict], now: float) -> list[dict]:
    provider = Provider(prod["provider"])
    log = []
    for e in entries:
        for cid in e["refund_charge_ids"]:
            outcome = provider.refund(cid, now, actor="repair_plan")
            log.append({"invoice_id": e["invoice_id"], "refund": cid, **outcome})
            if not outcome.get("result", {}).get("already_refunded", True):
                queue_event(prod, "charge.refunded", next(c for c in provider.charges if c["charge_id"] == cid))
        row = prod["db"][e["invoice_id"]]
        if e["hold"]:
            row.update(state="held", charge_id=None)
        elif e["keep_charge_id"]:
            row.update(state="paid", charge_id=e["keep_charge_id"])
        else:
            row.update(state="pending", charge_id=None)
    prod["provider"] = provider.state()
    return log


# --------------------------------------------------------------------------
# Outcome evaluation
# --------------------------------------------------------------------------
def invoice_outcomes(prod: dict, at: float) -> dict:
    """Judge every invoice at time ``at``; a declined card is excused only if not updated by then."""
    provider = Provider(prod["provider"])
    result = {}
    for inv, info in sorted(prod["invoices"].items()):
        net = [c for c in provider.net(inv) if c["merchant_id"] == info["merchant_id"]]
        row = prod["db"][inv]
        review = prod["params"].get("risk_review") or {}
        frozen = under_review(prod["params"], info["merchant_id"], at)
        term = prod["params"].get("refund_terms", {}).get(info["merchant_id"])
        refunded = [c for c in provider.charges if c["invoice_id"] == inv and c["status"] == "refunded"]
        if inv in prod.get("credits", {}):
            # Support credited this invoice in full and promised no charge this period.
            # (Under a release contract the refund already ended the invoice: any unbilled end state counts.)
            ok = not net and (row["state"] == "held" or (term == "release" and row["state"] not in ("paid",) + DUE_STATES))
            reason = "" if ok else ("credited invoice was charged" if net else "credited invoice is not held out of billing")
        elif frozen and any(c["created_at"] >= review["from"] for c in net):
            ok, reason = False, "charged while the merchant account was under provider review"
        elif term == "release" and billed_after_full_refund(provider, inv):
            ok, reason = False, "billed again after a refund released the customer (contract refund terms)"
        elif term == "release" and refunded and not net:
            # The merchant's contract releases the customer on a full refund: the invoice ends unpaid.
            ok = row["state"] not in ("paid",) + DUE_STATES
            reason = "" if ok else "released invoice is still recorded as paid or due"
        elif frozen and not net and not declined_now(prod["params"], inv, at):
            ok = row["state"] != "paid"   # billing it was not allowed: excused while the review is in force
            reason = "" if ok else "billing database records a payment that does not exist"
        elif declined_now(prod["params"], inv, at):
            ok = not net and row["state"] != "paid"
            reason = "" if ok else "declined invoice was charged or recorded as paid"
        elif row["state"] == "held":
            ok, reason = False, "invoice is still held out of billing"
        elif len(net) != 1:
            ok, reason = False, f"{len(net)} net provider charges"
        elif net[0]["amount"] != info["amount"]:
            ok, reason = False, "charged amount differs from invoice"
        elif row["state"] != "paid" or row["charge_id"] != net[0]["charge_id"]:
            ok, reason = False, "billing database does not record the provider payment"
        else:
            ok, reason = True, ""
        result[inv] = {"ok": ok, "reason": reason, "net_charges": len(net)}
    return result


def billed_after_full_refund(provider: Provider, invoice_id: str) -> bool:
    """Was a charge created for this invoice after every earlier charge had been refunded?"""
    charges = sorted((c for c in provider.charges if c["invoice_id"] == invoice_id), key=lambda c: (c["created_at"], c["charge_id"]))
    for i, c in enumerate(charges[1:], start=1):
        if all(e["status"] == "refunded" and e["refunded_at"] <= c["created_at"] for e in charges[:i]):
            return True
    return False


def rebill_owed(prod: dict, invoice_id: str) -> bool:
    """Under a ``rebill`` contract a merchant's own refund leaves the invoice owed: billing it
    again is the contract working, not a customer charged again after a refund (spec)."""
    info = prod["invoices"].get(invoice_id)
    if not info or prod["params"].get("refund_terms", {}).get(info["merchant_id"]) != "rebill":
        return False
    refunds = sorted((c for c in prod["provider"]["charges"] if c["invoice_id"] == invoice_id and c["status"] == "refunded"),
                     key=lambda c: (c["refunded_at"], c["charge_id"]))
    return bool(refunds) and refunds[-1].get("refunded_by") == "merchant"


def incident_invoices(prod: dict) -> list[str]:
    """Invoices that needed reconciliation at any point (initial or dynamic)."""
    initial = prod["truth"]["categories"]
    affected = {i for i, c in initial.items() if c not in ("healthy", "timeout_recovered", "declined")}
    affected |= {h["invoice_id"] for h in prod["harm"]}
    affected |= {a["invoice_id"] for a in prod["attempts"] if a.get("true_t", a["t"]) > 0
                 and a["outcome"] not in ("succeeded",) + DEFINITIVE_NO_CHARGE}  # a decline is not an incident effect
    return sorted(affected)


def harm_summary(prod: dict) -> dict:
    subs = {info["subscription_id"] for info in prod["invoices"].values()}
    harmed = {h["subscription_id"] for h in prod["harm"]} & subs
    kinds = {}
    for h in prod["harm"]:
        kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
    return {"at_risk_subscriptions": len(subs), "harmed_subscriptions": len(harmed),
            "harm_rate": len(harmed) / len(subs), "events": kinds,
            "recovery_harmful_charges": sum(h["cause"] == "recovery" and h["kind"] in ("duplicate_charge", "invalid_charge")
                                            for h in prod["harm"])}
