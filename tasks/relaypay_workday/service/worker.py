from __future__ import annotations

from .api import PaymentAPI
from .store import BillingStore


class BillingWorker:
    """Drains due invoices; each pass retries invoices left in a due state."""

    def __init__(self, api: PaymentAPI, store: BillingStore, *, max_passes: int = 3):
        self.api, self.store, self.max_passes = api, store, max_passes

    def drain(self) -> list[dict]:
        results = []
        for _ in range(self.max_passes):
            due = self.store.due()
            if not due:
                break
            for invoice_id in due:
                results.append(self.api.submit(invoice_id))
        return results
