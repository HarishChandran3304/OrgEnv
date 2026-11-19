"""Private reference, alternative and partial repairs for the billing service.

Never shipped to the runtime image or shown to evaluated agents. Used by tests,
negative controls and the offline Oracle runner. The Harbor Oracle embeds its
own copy of the reference repair.
"""
from __future__ import annotations

REFERENCE_KEYS = '''from __future__ import annotations


def payment_key(invoice: dict) -> str:
    """One idempotency key per invoice, stable across retries and restarts."""
    return f"invoice:{invoice['merchant_id']}:{invoice['invoice_id']}"
'''

CHATTY_API = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .keys import payment_key
from .store import BillingStore


def settled_charge(provider, invoice: dict):
    """The provider charge already paying this invoice, if any."""
    charges = provider.search_charges(invoice["merchant_id"], invoice_id=invoice["invoice_id"])
    paid = [c for c in charges if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]
    return paid[0] if paid else None


class PaymentAPI:
    def __init__(self, store: BillingStore, provider):
        self.store, self.provider = store, provider

    def submit(self, invoice_id: str) -> dict:
        invoice = self.store.invoice(invoice_id)
        if invoice["state"] in ("paid", "failed"):
            return {"invoice_id": invoice_id, "state": invoice["state"], "charge_id": invoice["charge_id"]}
        if self.store.attempts_for(invoice_id):
            # Earlier attempts (possibly by an older release with different
            # keys) may have charged without a response. Never charge twice.
            existing = settled_charge(self.provider, invoice)
            if existing:
                self.store.mark_paid(invoice_id, existing["charge_id"])
                return {"invoice_id": invoice_id, "state": "paid", "charge_id": existing["charge_id"]}
        attempt = self.store.next_attempt(invoice_id)
        key = payment_key(invoice)
        # A repeated key replays the original outcome. After a payment made under this
        # key was refunded, or the card was declined under it (and has since been
        # updated), billing again needs a new key generation.
        reversed_charges = [c for c in self.provider.search_charges(invoice["merchant_id"], invoice_id=invoice_id)
                            if c["status"] == "refunded" and str(c["idempotency_key"]).startswith(key)]
        declines = [a for a in self.store.attempts_for(invoice_id)
                    if a["outcome"] == "declined" and str(a["key"]).startswith(key)]
        if reversed_charges or declines:
            key = f"{key}:rebill-{len(reversed_charges) + len(declines)}"
        charge_id = None
        try:
            result = self.provider.charge(
                key, merchant_id=invoice["merchant_id"], invoice_id=invoice_id,
                subscription_id=invoice["subscription_id"], period=invoice["period"], amount=invoice["amount"])
        except ProviderTimeout:
            outcome, state = "timeout", "retry"
        except ProviderError as exc:
            outcome, state = exc.code, "retry"
        else:
            if result["status"] == "succeeded":
                outcome, state, charge_id = "succeeded", "paid", result["charge_id"]
            else:
                outcome, state = "declined", "failed"
        self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome=outcome, charge_id=charge_id)
        if state == "paid":
            self.store.mark_paid(invoice_id, charge_id)
        else:
            self.store.mark(invoice_id, state)
        return {"invoice_id": invoice_id, "state": state, "outcome": outcome, "charge_id": charge_id}
'''

CHATTY_RECONCILE = '''from __future__ import annotations

from .store import BillingStore


def reconcile_pending(store: BillingStore, provider) -> list[dict]:
    """Settle due invoices with earlier attempts from this invoice's own provider charges."""
    changes = []
    for invoice_id in store.due():
        if not store.attempts_for(invoice_id):
            continue
        invoice = store.invoice(invoice_id)
        charges = [c for c in provider.search_charges(invoice["merchant_id"], invoice_id=invoice_id)
                   if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]
        if charges:
            store.mark_paid(invoice_id, charges[0]["charge_id"])
            changes.append({"invoice_id": invoice_id, "charge_id": charges[0]["charge_id"]})
    return changes
'''

# Alternative design: never retry an ambiguous attempt inline. Ambiguous
# invoices are parked as "unknown" and only the reconciler decides whether they
# were charged; confirmed-unpaid invoices are re-queued.
HOLD_API = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .keys import payment_key
from .reconcile import own_charges


class PaymentAPI:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider

    def submit(self, invoice_id):
        inv = self.store.invoice(invoice_id)
        if inv["state"] in ("paid", "failed", "unknown"):
            return {"invoice_id": invoice_id, "state": inv["state"]}
        history = self.store.attempts_for(invoice_id)
        if inv["state"] != "confirmed_unpaid" and any(a["outcome"] != "declined" for a in history):
            self.store.mark(invoice_id, "unknown")
            return {"invoice_id": invoice_id, "state": "unknown"}
        attempt = self.store.next_attempt(invoice_id)
        key = payment_key(inv)
        reversed_charges = []
        if history:
            try:
                reversed_charges = [c for c in own_charges(self.store, self.provider, inv)
                                    if c["status"] == "refunded" and str(c["idempotency_key"]).startswith(key)]
            except (ProviderTimeout, ProviderError):
                return {"invoice_id": invoice_id, "state": inv["state"]}
        declines = [a for a in history if a["outcome"] == "declined" and str(a["key"]).startswith(key)]
        if reversed_charges or declines:
            key = f"{key}:rebill-{len(reversed_charges) + len(declines)}"
        try:
            res = self.provider.charge(key, merchant_id=inv["merchant_id"], invoice_id=invoice_id,
                                       subscription_id=inv["subscription_id"], period=inv["period"], amount=inv["amount"])
        except ProviderTimeout:
            self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome="timeout")
            self.store.mark(invoice_id, "unknown")
            return {"invoice_id": invoice_id, "state": "unknown"}
        except ProviderError as exc:
            self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome=exc.code)
            self.store.mark(invoice_id, "unknown")
            return {"invoice_id": invoice_id, "state": "unknown"}
        if res["status"] == "succeeded":
            self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome="succeeded", charge_id=res["charge_id"])
            self.store.mark_paid(invoice_id, res["charge_id"])
            return {"invoice_id": invoice_id, "state": "paid"}
        self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome="declined")
        self.store.mark(invoice_id, "failed")
        return {"invoice_id": invoice_id, "state": "failed"}
'''

HOLD_STORE_DUE = 'DUE_STATES = ("pending", "retry", "confirmed_unpaid")'

HOLD_RECONCILE = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout


def merchant_ledger(store, provider, merchant_id, refresh=False):
    cache = store.__dict__.setdefault("_ledger", {})
    if refresh or merchant_id not in cache:
        cache[merchant_id] = provider.search_charges(merchant_id)
    return cache[merchant_id]


def own_charges(store, provider, inv):
    return [c for c in merchant_ledger(store, provider, inv["merchant_id"]) if c["invoice_id"] == inv["invoice_id"]]


def reconcile_pending(store, provider):
    changes = []
    refreshed = set()
    for invoice_id, inv in sorted(store.invoices.items()):
        if inv["state"] not in ("unknown", "retry"):
            continue
        try:  # the reconciler decides from a fresh ledger (one request per merchant per pass)
            ledger = merchant_ledger(store, provider, inv["merchant_id"], refresh=inv["merchant_id"] not in refreshed)
        except (ProviderError, ProviderTimeout):
            continue
        refreshed.add(inv["merchant_id"])
        own = [c for c in ledger if c["invoice_id"] == invoice_id and c["status"] == "succeeded"]
        if own:
            store.mark_paid(invoice_id, own[0]["charge_id"])
        else:
            store.mark(invoice_id, "confirmed_unpaid")
        changes.append(invoice_id)
    return changes
'''

HOLD_KEYS = '''from __future__ import annotations


def payment_key(invoice: dict) -> str:
    return invoice["invoice_id"]
'''


CHATTY_WEBHOOKS = '''from __future__ import annotations

from .store import BillingStore


def apply_webhook_events(store: BillingStore, events: list[dict], provider) -> int:
    """Events arrive unordered and may be stale: re-derive each touched invoice from the provider."""
    touched = []
    for event in events:
        invoice_id = event.get("invoice_id")
        if invoice_id in store.invoices and invoice_id not in touched:
            touched.append(invoice_id)
    for invoice_id in touched:
        row = store.invoice(invoice_id)
        if row["state"] == "failed":
            continue
        own = [c for c in provider.search_charges(row["merchant_id"], invoice_id=invoice_id)
               if c["status"] == "succeeded" and c["amount"] == row["amount"]]
        ids = [c["charge_id"] for c in own]
        if len(own) == 1:
            store.mark_paid(invoice_id, own[0]["charge_id"])
        elif len(own) > 1 and row["charge_id"] not in ids:
            store.mark_paid(invoice_id, ids[0])
        elif not own and row["state"] == "paid":
            store.mark(invoice_id, "retry")
    return len(touched)
'''

# Reference repair (efficient): provider requests are rate limited per merchant and
# scheduler run, so each merchant's ledger is read once per run and refreshed only
# when provider events say it changed. A stale ledger is safe: a retry reuses the
# same idempotency key, so the provider replays instead of charging twice.
REFERENCE_RECONCILE = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .store import BillingStore


def merchant_ledger(store: BillingStore, provider, merchant_id: str, refresh: bool = False) -> list[dict]:
    cache = store.__dict__.setdefault("_ledger", {})
    if refresh or merchant_id not in cache:
        cache[merchant_id] = provider.search_charges(merchant_id)
    return cache[merchant_id]


def own_charges(store: BillingStore, provider, invoice: dict) -> list[dict]:
    return [c for c in merchant_ledger(store, provider, invoice["merchant_id"]) if c["invoice_id"] == invoice["invoice_id"]]


def reconcile_pending(store: BillingStore, provider) -> list[dict]:
    \"\"\"Settle due invoices with earlier attempts from this invoice's own provider charges.\"\"\"
    changes = []
    for invoice_id in store.due():
        if not store.attempts_for(invoice_id):
            continue
        invoice = store.invoice(invoice_id)
        try:
            charges = [c for c in own_charges(store, provider, invoice)
                       if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]
        except (ProviderError, ProviderTimeout):
            continue
        if charges:
            store.mark_paid(invoice_id, charges[0]["charge_id"])
            changes.append({"invoice_id": invoice_id, "charge_id": charges[0]["charge_id"]})
    return changes
'''

REFERENCE_API = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .keys import payment_key
from .reconcile import own_charges
from .store import BillingStore


class PaymentAPI:
    def __init__(self, store: BillingStore, provider):
        self.store, self.provider = store, provider

    def submit(self, invoice_id: str) -> dict:
        invoice = self.store.invoice(invoice_id)
        if invoice["state"] in ("paid", "failed"):
            return {"invoice_id": invoice_id, "state": invoice["state"], "charge_id": invoice["charge_id"]}
        attempt = self.store.next_attempt(invoice_id)
        key = payment_key(invoice)
        charges = []
        if attempt > 1:  # only an invoice with history can have provider charges already
            try:
                charges = own_charges(self.store, self.provider, invoice)
            except (ProviderTimeout, ProviderError):
                self.store.mark(invoice_id, "retry")
                return {"invoice_id": invoice_id, "state": "retry", "outcome": "lookup_unavailable"}
        if self.store.attempts_for(invoice_id):
            # Earlier attempts (possibly by an older release with different
            # keys) may have charged without a response. Never charge twice.
            paid = [c for c in charges if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]
            if paid:
                self.store.mark_paid(invoice_id, paid[0]["charge_id"])
                return {"invoice_id": invoice_id, "state": "paid", "charge_id": paid[0]["charge_id"]}
        reversed_charges = [c for c in charges if c["status"] == "refunded" and str(c["idempotency_key"]).startswith(key)]
        # A repeated key replays the original outcome. After a payment made under this
        # key was refunded, or the card was declined under it (and has since been
        # updated), billing again needs a new key generation.
        declines = [a for a in self.store.attempts_for(invoice_id)
                    if a["outcome"] == "declined" and str(a["key"]).startswith(key)]
        if reversed_charges or declines:
            key = f"{key}:rebill-{len(reversed_charges) + len(declines)}"
        charge_id = None
        try:
            result = self.provider.charge(
                key, merchant_id=invoice["merchant_id"], invoice_id=invoice_id,
                subscription_id=invoice["subscription_id"], period=invoice["period"], amount=invoice["amount"])
        except ProviderTimeout:
            outcome, state = "timeout", "retry"
        except ProviderError as exc:
            outcome, state = exc.code, "retry"
        else:
            if result["status"] == "succeeded":
                outcome, state, charge_id = "succeeded", "paid", result["charge_id"]
            else:
                outcome, state = "declined", "failed"
        self.store.record_attempt(invoice_id, attempt=attempt, key=key, outcome=outcome, charge_id=charge_id)
        if state == "paid":
            self.store.mark_paid(invoice_id, charge_id)
        else:
            self.store.mark(invoice_id, state)
        return {"invoice_id": invoice_id, "state": state, "outcome": outcome, "charge_id": charge_id}
'''

REFERENCE_WEBHOOKS = '''from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .reconcile import merchant_ledger
from .store import BillingStore


def apply_webhook_events(store: BillingStore, events: list[dict], provider) -> int:
    \"\"\"Events arrive unordered and may be stale: re-derive each touched invoice from a
    fresh read of its merchant's ledger (one request per merchant).\"\"\"
    touched = []
    for event in events:
        invoice_id = event.get("invoice_id")
        if invoice_id in store.invoices and invoice_id not in touched:
            touched.append(invoice_id)
    ledgers = {}
    for merchant in sorted({store.invoice(i)["merchant_id"] for i in touched}):
        try:
            ledgers[merchant] = merchant_ledger(store, provider, merchant, refresh=True)
        except (ProviderError, ProviderTimeout):
            ledgers[merchant] = None  # events are delivered again next cycle
    for invoice_id in touched:
        row = store.invoice(invoice_id)
        ledger = ledgers.get(row["merchant_id"])
        if row["state"] == "failed" or ledger is None:
            continue
        own = [c for c in ledger if c["invoice_id"] == invoice_id and c["status"] == "succeeded" and c["amount"] == row["amount"]]
        ids = [c["charge_id"] for c in own]
        if len(own) == 1:
            store.mark_paid(invoice_id, own[0]["charge_id"])
        elif len(own) > 1 and row["charge_id"] not in ids:
            store.mark_paid(invoice_id, ids[0])
        elif not own and row["state"] == "paid":
            store.mark(invoice_id, "retry")
    return len(touched)
'''


WEBHOOKS_SKIP_IF_PAID = '''from __future__ import annotations


def apply_webhook_events(store, events, provider):
    """Only fills in unpaid invoices from success events; re-bills reversed payments."""
    for event in events:
        invoice_id = event.get("invoice_id")
        if invoice_id not in store.invoices:
            continue
        row = store.invoice(invoice_id)
        if event["type"] == "charge.succeeded" and row["state"] != "paid":
            store.mark_paid(invoice_id, event["charge_id"])
        elif event["type"] == "charge.refunded" and row["charge_id"] == event["charge_id"]:
            store.mark(invoice_id, "retry")
    return len(events)
'''


REFUND_ONLY_WEBHOOKS = '''def apply_webhook_events(store, events, provider):
    for event in events:
        invoice_id = event.get("invoice_id")
        if invoice_id in store.invoices and event["type"] == "charge.refunded" and store.invoice(invoice_id)["charge_id"] == event["charge_id"]:
            store.mark(invoice_id, "retry")
    return len(events)
'''


def overfit_with_refund_only_events(files):
    """Passes public validation; in production it re-charges a returned-to-billing invoice
    that already has a provider charge (no lookup outside the retry state, success events ignored)."""
    out = retry_state_lookup_only(files)
    out["billing_service/webhooks.py"] = REFUND_ONLY_WEBHOOKS
    return out


def _with(files, **replacements):
    out = dict(files)
    for name, content in replacements.items():
        out[f"billing_service/{name}.py"] = content
    return out


def reference(files):
    return _with(files, keys=REFERENCE_KEYS, api=REFERENCE_API, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


def chatty_reference(files):
    """Correct on every validation scenario, but searches the provider per invoice and step:
    in production the per-merchant rate limit stalls billing (a production-only root cause)."""
    return _with(files, keys=REFERENCE_KEYS, api=CHATTY_API, reconcile=CHATTY_RECONCILE, webhooks=CHATTY_WEBHOOKS)


def hold_and_reconcile(files):
    store = files["billing_service/store.py"].replace('DUE_STATES = ("pending", "retry")', HOLD_STORE_DUE)
    return _with(files, keys=HOLD_KEYS, api=HOLD_API, reconcile=HOLD_RECONCILE, store=store, webhooks=REFERENCE_WEBHOOKS)


def attempt_key_with_lookup(files):
    """Correct alternative: keeps per-attempt keys but always reconciles before re-charging."""
    keys = REFERENCE_KEYS.replace('def payment_key(invoice: dict) -> str:', 'def payment_key(invoice: dict, attempt: int = 0) -> str:') \
        .replace('return f"invoice:{invoice[\'merchant_id\']}:{invoice[\'invoice_id\']}"',
                 'return f"{invoice[\'invoice_id\']}:attempt-{attempt}"')
    api = REFERENCE_API.replace("key = payment_key(invoice)", "key = payment_key(invoice, attempt)") \
        .replace("        except ProviderTimeout:\n            outcome, state = \"timeout\", \"retry\"",
                 "        except ProviderTimeout:\n            # a new attempt gets a new key: the ledger must be re-read before it\n"
                 "            self.store.__dict__.get(\"_ledger\", {}).pop(invoice[\"merchant_id\"], None)\n"
                 "            outcome, state = \"timeout\", \"retry\"")
    assert api.count("payment_key(invoice, attempt)") == 1 and "_ledger" in api
    return _with(files, keys=keys, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


# ---- partial repairs (expected to fail for a specific reason) -------------
def drop_attempt_only(files):
    keys = files["billing_service/keys.py"].replace("-{attempt}\"", "\"")
    return _with(files, keys=keys)


def stable_key_only(files):
    api = files["billing_service/api.py"].replace("key = payment_key(invoice, attempt)", "key = payment_key(invoice)")
    return _with(files, keys=REFERENCE_KEYS, api=api)


def unfixed_reconciler(files):
    return _with(files, keys=REFERENCE_KEYS, api=CHATTY_API, webhooks=CHATTY_WEBHOOKS)


def no_rebill_generation(files):
    """Stable key with lookup, but re-uses the key after a refund (the provider replays the refunded charge)."""
    api = REFERENCE_API.replace("        if reversed_charges or declines:\n            key = f\"{key}:rebill-{len(reversed_charges) + len(declines)}\"\n", "")
    assert api != REFERENCE_API
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


def refund_only_generation(files):
    """New key generation after refunds only: a declined key replays the decline after a card update."""
    api = REFERENCE_API.replace("        if reversed_charges or declines:\n", "        if reversed_charges:\n")
    assert api != REFERENCE_API
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


def webhooks_unfixed(files):
    """Every other module repaired; the event consumer still overwrites recorded payments."""
    return _with(files, keys=REFERENCE_KEYS, api=REFERENCE_API, reconcile=REFERENCE_RECONCILE)


def webhooks_skip_if_paid(files):
    """Plausible event fix that trusts stale success events for unpaid invoices."""
    return _with(files, keys=REFERENCE_KEYS, api=REFERENCE_API, reconcile=REFERENCE_RECONCILE, webhooks=WEBHOOKS_SKIP_IF_PAID)


def latest_timeout_lookup_only(files):
    """Looks up the provider only when the latest attempt was a timeout."""
    api = REFERENCE_API.replace("if self.store.attempts_for(invoice_id):",
                                "if self.store.attempts_for(invoice_id) and self.store.attempts_for(invoice_id)[-1][\"outcome\"] == \"timeout\":")
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


def amount_blind_any_status(files):
    """Treats any provider record for the invoice (including refunds) as payment."""
    api = REFERENCE_API.replace('paid = [c for c in charges if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]',
                                'paid = list(charges)')
    rec = REFERENCE_RECONCILE.replace('if c["status"] == "succeeded" and c["amount"] == invoice["amount"]]', ']')
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=rec)


def retry_state_lookup_only(files):
    """Overfits the public fixture: only invoices labelled "retry" are looked up."""
    api = REFERENCE_API.replace("if self.store.attempts_for(invoice_id):",
                                "if invoice[\"state\"] == \"retry\" and self.store.attempts_for(invoice_id):")
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


def first_attempt_timeout_lookup(files):
    """Overfits the public fixture: looks up only when the first journal attempt timed out."""
    api = REFERENCE_API.replace("if self.store.attempts_for(invoice_id):",
                                "if self.store.attempts_for(invoice_id) and self.store.attempts_for(invoice_id)[0][\"outcome\"] == \"timeout\":")
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


ALTERNATIVES = {"reference": reference, "hold_and_reconcile": hold_and_reconcile,
                "attempt_key_with_lookup": attempt_key_with_lookup}
PARTIALS = {"drop_attempt_only": drop_attempt_only, "stable_key_only": stable_key_only,
            "unfixed_reconciler": unfixed_reconciler, "amount_blind_any_status": amount_blind_any_status,
            "retry_state_lookup_only": retry_state_lookup_only, "first_attempt_timeout_lookup": first_attempt_timeout_lookup,
            "webhooks_unfixed": webhooks_unfixed, "webhooks_skip_if_paid": webhooks_skip_if_paid,
            "no_rebill_generation": no_rebill_generation, "refund_only_generation": refund_only_generation}
PARK_GUARD = '''        if invoice["state"] == "needs_review":
            return {"invoice_id": invoice_id, "state": "needs_review"}
        try:
            own = [c for c in own_charges(self.store, self.provider, invoice) if c["status"] == "succeeded"]
        except (ProviderTimeout, ProviderError):
            own = []
        if len(own) > 1:
            self.store.mark(invoice_id, "needs_review")
            return {"invoice_id": invoice_id, "state": "needs_review"}
'''


def park_duplicates(files):
    """Cautious design: invoices that already carry several charges are parked for a human."""
    anchor = "        if self.store.attempts_for(invoice_id):\n"
    api = REFERENCE_API.replace(anchor, PARK_GUARD + anchor, 1)
    return _with(files, keys=REFERENCE_KEYS, api=api, reconcile=REFERENCE_RECONCILE, webhooks=REFERENCE_WEBHOOKS)


ACCEPTED_VARIANTS = {"latest_timeout_lookup_only": latest_timeout_lookup_only, "park_duplicates": park_duplicates}
# Correct on every validation scenario but too chatty for the provider's rate limit in production.
PRODUCTION_ONLY_FAILURES = {"chatty_reference": chatty_reference}
