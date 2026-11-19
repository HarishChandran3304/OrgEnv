from __future__ import annotations

from .store import BillingStore


def reconcile_pending(store: BillingStore, provider) -> list[dict]:
    """Settle due invoices whose latest attempt timed out, using provider records."""
    changes = []
    for invoice_id in store.due():
        attempts = store.attempts_for(invoice_id)
        if not attempts or attempts[-1]["outcome"] != "timeout":
            continue
        invoice = store.invoice(invoice_id)
        charges = provider.search_charges(
            invoice["merchant_id"], subscription_id=invoice["subscription_id"], period=invoice["period"])
        if charges:
            store.mark_paid(invoice_id, charges[0]["charge_id"])
            changes.append({"invoice_id": invoice_id, "charge_id": charges[0]["charge_id"]})
    return changes
