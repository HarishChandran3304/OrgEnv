"""A compact form of the signed record.

Every event carries the whole world before and after it, which is what lets the verifier
re-execute each transition on its recorded ``state_before``. Over a working day that is
hundreds of megabytes, rewritten after every tool call. The runtime therefore signs and stores
the record with each event's states written as a change from the state before it; the verifier
checks the signature on that form and then rebuilds the full record, exactly, before it
verifies anything. Nothing is dropped: an event's ``state_changes`` are stored unless they are
what the engine derives from the two states.
"""
from __future__ import annotations

from orgenv.core.engine import _state_changes

STATES = ("state_before", "state_after", "runtime_before", "runtime_after")


def diff(old, new):
    """A patch that turns ``old`` into ``new``; ``None`` if they are equal."""
    if type(old) is type(new) and old == new:
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        changed = {key: diff(old[key], value) if key in old else {"r": value} for key, value in new.items()
                   if key not in old or type(old[key]) is not type(value) or old[key] != value}
        return {"d": changed, "x": [key for key in old if key not in new]}
    if isinstance(old, list) and isinstance(new, list) and len(new) >= len(old) and new[:len(old)] == old:
        return {"a": new[len(old):]}
    return {"r": new}


def patch(old, change):
    """Apply a patch from ``diff``. Unchanged parts are shared with ``old``, never copied."""
    if change is None:
        return old
    if "r" in change:
        return change["r"]
    if "a" in change:
        return old + change["a"]
    new = {key: value for key, value in old.items() if key not in change["x"]}
    for key, sub in change["d"].items():
        new[key] = patch(old.get(key), sub)
    return new


def _derived(before, after):
    return [change.to_dict() for change in _state_changes(before, after)]


class Packer:
    """Packs events one at a time, so each call only pays for the events it added."""

    def __init__(self):
        self.rows = []
        self.last = {"state_after": None, "runtime_after": None}

    def add(self, event):
        row = {key: value for key, value in event.items() if key not in STATES and key != "state_changes"}
        row["has"] = [key for key in STATES if key in event]
        row["state_before"] = diff(self.last["state_after"], event["state_before"])
        row["state_after"] = diff(event["state_before"], event["state_after"])
        if "runtime_before" in event:
            row["runtime_before"] = diff(self.last["runtime_after"], event["runtime_before"])
        if "runtime_after" in event:
            row["runtime_after"] = diff(event.get("runtime_before", self.last["runtime_after"]), event["runtime_after"])
            self.last["runtime_after"] = event["runtime_after"]
        if event.get("state_changes") != _derived(event["state_before"], event["state_after"]):
            row["state_changes"] = event["state_changes"]
        self.last["state_after"] = event["state_after"]
        self.rows.append(row)


def unpack(rows):
    """The full events, rebuilt in order. Consecutive events share whatever did not change."""
    events, state, runtime = [], None, None
    for row in rows:
        event = {key: value for key, value in row.items() if key not in STATES and key not in ("has", "state_changes")}
        before = patch(state, row["state_before"])
        state = patch(before, row["state_after"])
        event["state_before"], event["state_after"] = before, state
        if "runtime_before" in row["has"]:
            event["runtime_before"] = patch(runtime, row["runtime_before"])
        if "runtime_after" in row["has"]:
            runtime = patch(event.get("runtime_before", runtime), row["runtime_after"])
            event["runtime_after"] = runtime
        event["state_changes"] = row["state_changes"] if "state_changes" in row else _derived(before, state)
        events.append(event)
    return events
