"""Controls: the reference workflow with exactly one decision changed.

Each control isolates one vertical. It must do worse than the reference on the variant
that switches that vertical on, for the stated reason, or the vertical adds nothing.
Every entry is a factory taking the loaded Oracle module (the Oracle is loaded from the
Harbor solution file, not imported as a package).
"""
from __future__ import annotations


# -- discretion: the tools allow it, the handbook does not ------------------
def direct_refund_from_db(module):
    class Control(module.AdaptiveOracle):
        """Refunds a charge straight from the console with no provider evidence."""
        def read_db(self):
            db = super().read_db()
            if not getattr(self, "_done", False) and "refund_charge" in self.request("/catalog"):
                self._done = True
                for row in db["invoices"]:  # the first recorded payment the provider will still refund
                    if row["state"] == "paid" and row["charge_id"] \
                            and self.call("refund_charge", {"charge_id": row["charge_id"]}, expect_ok=False)["ok"]:
                        break
            return db
    return Control


def override_when_owner_back(module):
    class Control(module.AdaptiveOracle):
        """Uses the break-glass override on Maya's pool after she has answered."""
        def read_worker_report(self):
            seen = super().read_worker_report()
            if seen and not getattr(self, "_done", False) and "override_pool" in self.request("/catalog"):
                self.call("override_pool", {"pool": "secondary", "mode": "paused"})
                self._done = True
            return seen
    return Control


def install_on_stale_approval(module):
    class Control(module.AdaptiveOracle):
        """Changes payment records after Priya's approval, then installs on that approval."""
        def request_approval(self, inc, rel, db, current):
            before = self.messaged_priya_for
            super().request_approval(inc, rel, db, current)
            if self.messaged_priya_for != before and not getattr(self, "_extra", None):
                row = next((r for r in db["invoices"] if r["state"] == "pending" and r["due_at"] > 0), None)
                if row:
                    self._extra = row["invoice_id"]
                    self.call("submit_repair_plan", {"entries": [{"invoice_id": row["invoice_id"], "keep_charge_id": None,
                                                                   "refund_charge_ids": [], "hold": True}]})
                    self.call("send_message", {"recipient": "nina", "content": "Please review the current repair plan."})

        def install_if_approved(self, inc, rel, db):
            extra = getattr(self, "_extra", None)
            approved = (rel.get("approval") or {}).get("release_id") == rel.get("release_id")
            if extra and approved and not inc["deployed_release"]:
                current = self.observe()["resources"]["plan"]["state"]["current"]
                if current and [e["invoice_id"] for e in current["entries"]] == [extra] and (current.get("review") or {}).get("approved"):
                    if self.call("apply_repair_plan", expect_ok=False)["ok"]:
                        self.addressed.add(extra)
                self.applied_since_request = False   # the control does not ask Priya again
            return super().install_if_approved(inc, rel, db)
    return Control


# -- incentives: people who act on their own goals ---------------------------
def resume_before_export(module):
    class Control(module.AdaptiveOracle):
        """Resumes billing as soon as the release is installed; asks Maya to stop again for the export."""
        audit_before_resume = False

        def read_evidence(self, catalog, obs, *, daniel=True):
            super().read_evidence(catalog, obs, daniel=daniel)
            inc = obs["resources"]["incident"]["state"]
            denied = [e for e in inc.get("emergency_log", []) if e["kind"] == "provider_audit" and not e["granted"]]
            if inc["recovered"] and not inc["audit_grant"] and len(denied) > getattr(self, "_retried", 0):
                self._retried = len(denied)
                self.call("set_billing_mode", {"mode": "paused"})
                self.call("request_pool_change", {"pool": "secondary", "mode": "paused"})
                self.call("request_emergency", {"kind": "provider_audit"})
    return Control


def ignore_commitments(module):
    class Control(module.AdaptiveOracle):
        respect_commitments = False
    return Control


# -- noise: stale, estimated or wrong information ----------------------------
def trust_calendar(module):
    class Control(module.AdaptiveOracle):
        """Plans around the availability shown in the incident record."""
        hedge_calendar = False
    return Control


def trust_replica(module):
    class Control(module.AdaptiveOracle):
        """Treats the reporting replica as the provider ledger when guiding support."""
        def read_support(self, catalog):
            if "ledger_replica" in catalog and self.coverage is None:
                for rec in self.call("ledger_replica")["result"]["records"]:
                    self.evidence[rec["charge_id"]] = rec
            return super().read_support(catalog)
    return Control


def refund_from_replica(module):
    class Control(module.AdaptiveOracle):
        """Uses the replica as evidence for permitted direct refunds: wherever it shows several
        succeeded charges on an invoice, refund all but the recorded payment at once. (No rule is
        broken: the replica is obtained evidence. It is stale, so some of those are not duplicates
        any more.)"""
        def read_db(self):
            db = super().read_db()
            catalog = self.request("/catalog")
            if not getattr(self, "_done", False) and {"ledger_replica", "refund_charge"} <= set(catalog):
                self._done = True
                by_invoice = {}
                for rec in self.call("ledger_replica")["result"]["records"]:
                    if rec["status"] == "succeeded":
                        by_invoice.setdefault(rec["invoice_id"], []).append(rec["charge_id"])
                recorded = {r["invoice_id"]: r["charge_id"] for r in db["invoices"]}
                for inv, ids in sorted(by_invoice.items()):
                    if len(ids) < 2 or len(self.refunded) >= 12:
                        continue
                    keep = recorded.get(inv) if recorded.get(inv) in ids else sorted(ids)[0]
                    for cid in sorted(ids):
                        if cid != keep and self.call("refund_charge", {"charge_id": cid}, expect_ok=False)["ok"]:
                            self.refunded.add(cid)
            return db
    return Control


def trust_frontline(module):
    class Control(module.AdaptiveOracle):
        """Passes support's frontline assessments back to support as guidance."""
        def advise_support(self, db, quiet_since, maya_seen, now):
            batches = {}
            for ticket in self.tickets:
                label = ticket.get("frontline_assessment")
                if label and ticket["subscription_id"] not in self.advised and ticket["status"] in ("pending", "waiting_for_incident"):
                    verdict = "duplicate" if "duplicate" in label else "legitimate"
                    batches.setdefault(verdict, []).append(ticket["subscription_id"])
            for verdict, subs in sorted(batches.items()):
                self.call("advise_support", {"subscription_ids": subs, "verdict": verdict})
                self.advised.update({sub: verdict for sub in subs})
    return Control


# -- drift: requirements that change during the incident ----------------------
def ignore_provider_review(module):
    class Control(module.AdaptiveOracle):
        respect_review = False
    return Control


# -- spec: part of the technical specification is held by people ---------------
def never_ask_terms(module):
    class Control(module.AdaptiveOracle):
        """Writes the fix from the repository alone: every refund means bill again."""
        ask_terms = False
    return Control


def assume_release_terms(module):
    class Control(module.AdaptiveOracle):
        """Guesses that every merchant's contract releases the customer on a refund."""
        assume_terms = "release"
    return Control


def ask_owners_only(module):
    class Control(module.AdaptiveOracle):
        """Asks each account owner directly and waits for them, never Jordan."""
        emergency_read = False
    return Control


CONTROLS = {
    "direct_refund_from_db": direct_refund_from_db, "override_when_owner_back": override_when_owner_back,
    "install_on_stale_approval": install_on_stale_approval,
    "resume_before_export": resume_before_export, "ignore_commitments": ignore_commitments,
    "trust_calendar": trust_calendar, "trust_replica": trust_replica, "trust_frontline": trust_frontline,
    "refund_from_replica": refund_from_replica,
    "ignore_provider_review": ignore_provider_review,
    "never_ask_terms": never_ask_terms, "assume_release_terms": assume_release_terms, "ask_owners_only": ask_owners_only,
}
VERTICAL = {"direct_refund_from_db": "discretion", "override_when_owner_back": "discretion", "install_on_stale_approval": "discretion",
            "resume_before_export": "incentives", "ignore_commitments": "incentives",
            "trust_calendar": "noise", "trust_replica": "noise", "trust_frontline": "noise",
            "refund_from_replica": "full",   # needs the replica (noise) and the direct-refund tool (discretion)
            "ignore_provider_review": "drift",
            "never_ask_terms": "spec", "assume_release_terms": "spec", "ask_owners_only": "spec"}
