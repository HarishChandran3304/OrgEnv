from __future__ import annotations

from provider_client import ProviderError, ProviderTimeout

from .keys import payment_key
from .store import BillingStore


class PaymentAPI:
    def __init__(self, store: BillingStore, provider):
        self.store, self.provider = store, provider

    def submit(self, invoice_id: str) -> dict:
        invoice = self.store.invoice(invoice_id)
        if invoice["state"] == "paid":
            return {"invoice_id": invoice_id, "state": "paid", "charge_id": invoice["charge_id"]}
        attempt = self.store.next_attempt(invoice_id)
        key = payment_key(invoice, attempt)
        charge_id = None
        try:
            result = self.provider.charge(
                key,
                merchant_id=invoice["merchant_id"],
                invoice_id=invoice_id,
                subscription_id=invoice["subscription_id"],
                period=invoice["period"],
                amount=invoice["amount"],
            )
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
