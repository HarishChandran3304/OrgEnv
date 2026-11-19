from __future__ import annotations

from copy import deepcopy


class BillingStore:
    """Invoice rows and the payment-attempt journal (the billing database)."""

    DUE_STATES = ("pending", "retry")

    def __init__(self, invoices: list[dict], attempts: list[dict] | None = None):
        self.invoices = {}
        for row in deepcopy(invoices):
            row.setdefault("state", "pending")
            row.setdefault("charge_id", None)
            self.invoices[row["invoice_id"]] = row
        self.attempts = deepcopy(list(attempts or []))

    def invoice(self, invoice_id: str) -> dict:
        return self.invoices[invoice_id]

    def due(self) -> list[str]:
        return sorted(i for i, row in self.invoices.items() if row["state"] in self.DUE_STATES)

    def attempts_for(self, invoice_id: str) -> list[dict]:
        return [a for a in self.attempts if a["invoice_id"] == invoice_id]

    def next_attempt(self, invoice_id: str) -> int:
        return len(self.attempts_for(invoice_id)) + 1

    def record_attempt(self, invoice_id: str, *, attempt: int, key: str, outcome: str,
                       charge_id: str | None = None) -> dict:
        row = {"invoice_id": invoice_id, "attempt": attempt, "key": key,
               "outcome": outcome, "charge_id": charge_id}
        self.attempts.append(row)
        return dict(row)

    def mark_paid(self, invoice_id: str, charge_id: str) -> None:
        row = self.invoice(invoice_id)
        row["state"], row["charge_id"] = "paid", charge_id

    def mark(self, invoice_id: str, state: str) -> None:
        self.invoice(invoice_id)["state"] = state

    def snapshot(self) -> dict:
        return {"invoices": deepcopy(self.invoices), "attempts": deepcopy(self.attempts)}
