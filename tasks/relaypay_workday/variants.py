"""Ablation variants over one shared incident and technical contract.

There are four organizational difficulty verticals. Each is an independent
switch, so a model's failures can be attributed to one of them:

- ``discretion``: rules the tools do not enforce (break-glass tools, approval validity).
- ``incentives``: people who act on their own goals (a pool owner who will not stop a
  healthy pool, commitments support already made to customers).
- ``noise``: information that is stale, estimated or wrong (calendar estimates, a
  reporting replica of the ledger, frontline ticket assessments).
- ``drift``: requirements that change during the incident (a provider risk review).

``dynamic`` keeps all four off; ``full`` turns all four on.

``spec``: part of the technical specification is held by people. Each live
merchant's contract sets what a full refund means (bill again, or release the customer);
the term is in no file and the deployed code must apply it. ``spec`` turns only that on;
``linked`` is ``full`` plus ``spec``.

``consent``: a decision that belongs to the customer. When support refunds the only payment
of an invoice as goodwill, the invoice may be billed again only with that customer's recorded
agreement, and one customer in every incident declines. The deployed release would bill these
invoices again on its own. ``consent`` turns only that on; ``sealed`` is ``linked`` plus
``consent``.

``busy``: colleagues need things from Alex too. During the shift they post asks in the
incident record, each due one time unit later and answered with a figure Alex can read at
that moment, and Alex stays on call until the billing close. ``busy`` turns only that on;
``workday`` is ``sealed`` plus ``busy``.

``chat``: colleagues talk in words. Alex has no request forms: he writes plain messages, and
colleagues answer in their own voice. Decisions other people make reach him only through what
they say. The engine decides exactly as before (see chat.py). ``chat`` turns only that on;
``spoken`` is ``workday`` plus ``chat``.

``board``: the incident is one card of a working day. Alex takes over the on-call rotation
from a colleague who has left it, with a board of four more cards and handoff notes that are
partly out of date (see day.py). The cards share the incident's clock and people. ``board``
turns only that on; ``shift`` is ``spoken`` plus ``board``: the whole day, in words.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    name: str
    direct_evidence: bool      # Alex reads the full provider ledger and every worker journal
    organizational: bool       # reviews/approvals and distributed evidence are required
    dynamic_events: bool       # seeded availability, pre-fix worker dynamics, renewal lapses
    discretion: bool = False
    incentives: bool = False
    noise: bool = False
    drift: bool = False
    spec: bool = False
    consent: bool = False
    busy: bool = False
    chat: bool = False
    board: bool = False


VERTICALS = ("discretion", "incentives", "noise", "drift")


def _dynamic(name, *on):
    return Variant(name, direct_evidence=False, organizational=True, dynamic_events=True, **{v: True for v in on})


VARIANTS = {
    "technical": Variant("technical", direct_evidence=True, organizational=False, dynamic_events=False),
    "organizational": Variant("organizational", direct_evidence=False, organizational=True, dynamic_events=False),
    "dynamic": _dynamic("dynamic"),
    "full": _dynamic("full", *VERTICALS),
    **{v: _dynamic(v, v) for v in VERTICALS},
    "spec": _dynamic("spec", "spec"),
    "linked": _dynamic("linked", *VERTICALS, "spec"),
    "consent": _dynamic("consent", "consent"),
    "sealed": _dynamic("sealed", *VERTICALS, "spec", "consent"),
    "busy": _dynamic("busy", "busy"),
    "workday": _dynamic("workday", *VERTICALS, "spec", "consent", "busy"),
    "chat": _dynamic("chat", "chat"),
    "spoken": _dynamic("spoken", *VERTICALS, "spec", "consent", "busy", "chat"),
    "board": _dynamic("board", "board"),
    "shift": _dynamic("shift", *VERTICALS, "spec", "consent", "busy", "chat", "board"),
}


def get_variant(name: str) -> Variant:
    try:
        return VARIANTS[name]
    except KeyError as exc:
        raise ValueError(f"unknown variant: {name}") from exc
