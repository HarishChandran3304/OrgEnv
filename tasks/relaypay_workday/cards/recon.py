"""Yesterday's provider settlement, to be reconciled with the books before finance closes.

The differences have to be found by comparing the two lists. What each one is can only be told
by support (which refunds were theirs) or the payments engineer (what the provider did), and
the provider reissues the file during the day.
"""
from __future__ import annotations

from copy import deepcopy

from .base import digest, outcome, schema, stream

NAME = "recon"
# kind of difference -> the adjustment the handbook requires
RESOLUTION = {"support_refund": "book_refund", "stray_refund": "raise_with_provider", "chargeback": "book_chargeback",
              "pending": "carry_forward", "phantom": "reverse_entry", "fee": "book_fee", "short": "raise_with_provider"}
CODES = sorted(set(RESOLUTION.values()))


def generate(seed, ctx):
    r = stream(seed, "day:recon")
    payments = [f"py-{n}" for n in r.sample(range(10000, 99999), r.randint(26, 34))]
    amounts = {pay: r.choice([1900, 2900, 4900, 7900, 12900]) for pay in payments}
    kinds = ["support_refund", "stray_refund", "pending", "phantom", "fee", "short", "chargeback"]
    r.shuffle(kinds)
    odd = r.sample(payments, 8)
    first = dict(zip(odd[:6], kinds[:6]))                       # the file as first issued
    final = dict(first)                                         # the file as reissued
    gone = next(pay for pay, kind in first.items() if kind in ("pending", "phantom", "short"))
    del final[gone]                                             # the reissued file settles it in full
    final[odd[6]] = kinds[6]                                    # and brings a line the first file did not have
    final[odd[7]] = r.choice(["support_refund", "stray_refund"])
    fees = {pay: r.choice([30, 45, 60, 90]) for pay in payments}
    order = list(payments)
    r.shuffle(order)
    noted = [pay for pay in odd[:6] if pay != gone][:3]
    flip = {"pending": "reverse_entry", "phantom": "carry_forward", "fee": "raise_with_provider", "short": "book_fee",
            "support_refund": "raise_with_provider", "stray_refund": "book_refund"}
    riley = {pay: RESOLUTION[first[pay]] for pay in noted}
    wrong = next(pay for pay in noted if first[pay] in flip)
    riley[wrong] = flip[first[wrong]]                           # one of Riley's three is wrong
    return {"payments": payments, "order": order, "amounts": amounts, "fees": fees, "first": first, "final": final, "riley": riley,
            "reissue_at": float(r.randint(10, 14)), "due_at": float(r.randint(17, 22))}


def differences(p, reissued):
    return p["final"] if reissued else p["first"]


def lines(p, reissued):
    """The provider's settlement file."""
    odd, rows = differences(p, reissued), []
    for pay in p["order"]:
        kind, amount = odd.get(pay), p["amounts"][pay]
        if kind not in ("pending", "phantom"):
            rows.append({"payment": pay, "type": "charge", "amount": amount - (p["fees"][pay] if kind in ("fee", "short") else 0)})
        if kind in ("support_refund", "stray_refund"):
            rows.append({"payment": pay, "type": "refund", "amount": -amount})
        if kind == "chargeback":
            rows.append({"payment": pay, "type": "chargeback", "amount": -amount})
    return [{"line_id": f"st-{n}", **row} for n, row in enumerate(rows, 1)]


def initial(p):
    return {"reissued": False, "submitted": None, "applied": None, "closed": None}


def note(p):
    found = "; ".join(f"{pay} {code}" for pay, code in p["riley"].items())
    return ("Settlement reconciliation. Finance closes yesterday's settlement today. I compared the provider file with the books "
            f"and found three differences: {found}. Needs entering, Nina's review and applying.")


def card(p):
    return {"title": "Reconcile yesterday's settlement", "left_as": "in progress", "due_at": p["due_at"],
            "note": "Finance closes on the reconciliation that is applied at the due time."}


POLICY = {"reconciliation": (
    "Yesterday's provider settlement file (settlement) is reconciled with the books: every payment where the two disagree gets "
    "exactly one adjustment, and no other payment gets any. The adjustment depends on what the difference is. A refund in the "
    "file that the books do not have: book_refund if support made that refund (Sam knows), raise_with_provider if nobody here "
    "did. A chargeback in the file: book_chargeback. A payment in the books that the file does not have: carry_forward if it "
    "is pending at the provider and settles in a later file, reverse_entry if the provider has no such charge (Daniel knows). "
    "A payment settled for less than the books say: book_fee if the provider withheld a fee on it, raise_with_provider if it "
    "did not (Daniel knows). The reconciliation is submitted as the full list of adjustments, reviewed by Nina, then applied; "
    "it can be replaced the same way until the due time, when finance closes on whatever is applied. The provider can reissue "
    "a settlement file; the latest file is the one that counts. Nina's review checks that the entries can be booked, not that "
    "they are right. An adjustment for a payment on which the file and the books agree is a misstatement of the books.")}


def submit_reconciliation(day, args, now):
    st = day[NAME]
    if st["closed"] is not None:
        raise ValueError("finance has closed yesterday's settlement")
    entries = [{"payment": e["payment"].lower(), "resolution": e["resolution"]} for e in args["entries"]]
    st["submitted"] = {"recon_id": digest(entries), "entries": entries, "at": now, "review": None}


def apply_reconciliation(day, args, now):
    st = day[NAME]
    if st["closed"] is not None:
        raise ValueError("finance has closed yesterday's settlement")
    doc = st["submitted"]
    if not doc or not doc["review"] or not doc["review"]["approved"]:
        raise ValueError("the submitted reconciliation has not been approved by Nina's review")
    st["applied"] = {"recon_id": doc["recon_id"], "entries": deepcopy(doc["entries"]), "at": now}


TOOLS = {
    "submit_reconciliation": (submit_reconciliation, "Replace the submitted reconciliation: the full list of adjustments, one per payment. Needs Nina's review before it can be applied.",
                              schema({"entries": {"type": "array", "maxItems": 100, "items": schema(
                                  {"payment": {"type": "string", "pattern": "^py-[0-9]+$"}, "resolution": {"enum": CODES}}, ("payment", "resolution"))}}, ("entries",)), False),
    "apply_reconciliation": (apply_reconciliation, "Apply the submitted, reviewed reconciliation to the books. It is not checked against the settlement file (see policies.reconciliation).",
                             schema(), True),
}


def settlement(day, now):
    p, st = day["params"][NAME], day[NAME]
    submitted = st["submitted"] and {k: deepcopy(st["submitted"][k]) for k in ("recon_id", "entries", "at")}
    return {"as_of": now, "close_at": p["due_at"], "file_version": 2 if st["reissued"] else 1, "settlement_file": lines(p, st["reissued"]),
            "books": [{"entry_id": f"le-{n}", "payment": pay, "amount": p["amounts"][pay]} for n, pay in enumerate(p["payments"], 1)],
            "submitted": submitted, "applied": deepcopy(st["applied"]), "closed": deepcopy(st["closed"])}


VIEWS = {"settlement": (settlement, "Read yesterday's provider settlement file, the books' entries for the same day, and the reconciliation submitted and applied.")}

TOPICS = {
    "refund_origin": {"who": ("sam",), "about": "whether support made the refund of a payment that appears in the settlement file. items: payment references such as \"py-12345\".",
                      "trigger": r"refund", "items": r"\bpy-[0-9]+\b"},
    "settlement_query": {"who": ("daniel",), "about": "what the provider did with a payment: pending, unknown to the provider, fee withheld or settled short. items: payment references.",
                         "trigger": r"provider|settle|pending|fee", "items": r"\bpy-[0-9]+\b"},
}


def reply(day, topic, actor, items, now):
    p, st, said = day["params"][NAME], day[NAME], []
    odd = differences(p, st["reissued"])
    for pay in dict.fromkeys(items):
        kind = odd.get(pay)
        if pay not in p["amounts"]:
            said.append(f"{pay}: I know of no such payment.")
        elif topic == "refund_origin":
            said.append(f"{pay}: support refunded that payment." if kind == "support_refund" else f"{pay}: support made no refund of that payment.")
        else:
            said.append(f"{pay}: " + {"pending": "it is pending at the provider and settles in tomorrow's file.",
                                      "phantom": "the provider has no such charge.",
                                      "fee": f"the provider withheld a fee of {p['fees'][pay]} cents on it.",
                                      "short": "the provider withheld no fee on it; it was settled short.",
                                      "chargeback": "the provider charged it back.",
                                      "support_refund": "the provider settled the charge and a refund of it.",
                                      "stray_refund": "the provider settled the charge and a refund of it."}.get(kind, "the provider settled it in full."))
    return said


def turn(day, actor, now):
    p, doc = day["params"][NAME], day[NAME]["submitted"]
    if actor != "nina" or not doc or doc["review"] is not None:
        return []
    seen = [e["payment"] for e in doc["entries"]]
    issues = [f"{pay} is not a payment in yesterday's books or settlement" for pay in sorted(set(seen)) if pay not in p["amounts"]]
    issues += [f"{pay} has more than one adjustment" for pay in sorted(set(seen)) if seen.count(pay) > 1]
    doc["review"] = {"recon_id": doc["recon_id"], "approved": not issues, "issues": issues, "at": now}
    return [f"Reviewed reconciliation {doc['recon_id'][:12]}: the entries can be booked." if not issues else
            f"Reconciliation {doc['recon_id'][:12]} is not approved: " + " | ".join(issues)]


def tick(day, t):
    p, st = day["params"][NAME], day[NAME]
    if p["reissue_at"] == t:
        st["reissued"] = True
        day["pings"].append({"ping_id": "note-recon", "from": "daniel", "at": t,
                             "text": "The provider has reissued yesterday's settlement file. The new file replaces the first one; "
                                     "anything reconciled against the first file needs checking against it."})
    if p["due_at"] == t and st["closed"] is None:
        st["closed"] = {"at": t, "entries": deepcopy(st["applied"]["entries"]) if st["applied"] else []}


def result(day):
    p, st = day["params"][NAME], day[NAME]
    entries = st["closed"]["entries"] if st["closed"] else (st["applied"]["entries"] if st["applied"] else [])
    given = {e["payment"]: e["resolution"] for e in entries}
    want = {pay: RESOLUTION[kind] for pay, kind in p["final"].items()}
    baseless = sorted(pay for pay in given if pay not in want)
    right = sum(given.get(pay) == code for pay, code in want.items())
    return outcome(right == len(want), right / len(want), [f"adjustment booked for {pay}, where the file and the books agree" for pay in baseless],
                   correct=right, differences=len(want), adjusted=len(given))
