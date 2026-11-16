"""Local development stand-in for the platform's provider_client module.

Only for experimenting in /workspace. Validation (the provider's test mode) and
production (live merchant accounts) inject the real provider; this fake never times
out unless you configure it to, and like test mode it is not rate limited.
"""
import itertools


class ProviderTimeout(Exception):
    """The request may or may not have been processed by the provider."""


class ProviderError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code = code


class ProviderClient:
    def __init__(self, faults=None):
        self.faults = {k: list(v) for k, v in (faults or {}).items()}  # invoice_id -> ["timeout_after", ...]
        self.charges, self.keys, self._ids = [], {}, itertools.count(1)

    def charge(self, idempotency_key, *, merchant_id, invoice_id, subscription_id, period, amount):
        token = (merchant_id, idempotency_key)
        if token in self.keys:
            rec = self.keys[token]
            if (rec["invoice_id"], rec["amount"]) != (invoice_id, amount):
                raise ProviderError("idempotency_conflict", "key reused with different parameters")
            return {"status": "succeeded", "charge_id": rec["charge_id"], "replayed": True}
        fault = self.faults.get(invoice_id, []).pop(0) if self.faults.get(invoice_id) else "ok"
        if fault == "timeout_before":
            raise ProviderTimeout("timed out")
        charge = {"charge_id": f"ch_dev{next(self._ids)}", "merchant_id": merchant_id, "invoice_id": invoice_id,
                  "subscription_id": subscription_id, "period": period, "amount": amount, "status": "succeeded",
                  "idempotency_key": idempotency_key, "created_at": 0.0}
        self.charges.append(charge)
        self.keys[token] = {"invoice_id": invoice_id, "amount": amount, "charge_id": charge["charge_id"]}
        if fault == "timeout_after":
            raise ProviderTimeout("timed out")
        return {"status": "succeeded", "charge_id": charge["charge_id"], "replayed": False}

    def search_charges(self, merchant_id, *, invoice_id=None, subscription_id=None, period=None):
        return [dict(c) for c in self.charges if c["merchant_id"] == merchant_id
                and (invoice_id is None or c["invoice_id"] == invoice_id)
                and (subscription_id is None or c["subscription_id"] == subscription_id)
                and (period is None or c["period"] == period)]

    def refund(self, charge_id):
        raise ProviderError("not_permitted", "refunds are issued by support or by applying a reviewed repair plan")
