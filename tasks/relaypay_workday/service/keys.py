from __future__ import annotations


def payment_key(invoice: dict, attempt: int) -> str:
    """Idempotency key sent to the payment provider for one charge request."""
    return f"{invoice['subscription_id']}-{invoice['period']}-{attempt}"
