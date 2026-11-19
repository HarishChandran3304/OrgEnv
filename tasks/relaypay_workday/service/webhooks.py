from __future__ import annotations

from .store import BillingStore


def apply_webhook_events(store: BillingStore, events: list[dict], provider) -> int:
    """Apply provider events (charge.succeeded / charge.refunded) to invoice rows."""
    applied = 0
    for event in events:
        invoice_id = event.get("invoice_id")
        if invoice_id not in store.invoices:
            continue
        row = store.invoice(invoice_id)
        if event["type"] == "charge.succeeded":
            store.mark_paid(invoice_id, event["charge_id"])
        elif event["type"] == "charge.refunded" and row["charge_id"] == event["charge_id"]:
            # The recorded payment was reversed: bill the invoice again.
            store.mark(invoice_id, "retry")
        applied += 1
    return applied
