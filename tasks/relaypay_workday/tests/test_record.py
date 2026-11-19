"""The compact signed record rebuilds the full record exactly and is verified in that form."""
import json

from tasks.relaypay_workday.eval.offline import KEY
from tasks.relaypay_workday.record import diff, patch, unpack
from tasks.relaypay_workday.task import EpisodeV3
from tasks.relaypay_workday.verifier import seal, verify_envelope


def short_day():
    episode = EpisodeV3(1308265273, variant="shift")
    episode.invoke("send_message", {"recipient": "jordan", "content": "Jordan, please order an emergency stop of all billing."})
    episode.invoke("set_billing_mode", {"mode": "paused"})
    episode.invoke("advance_time", {"amount": 3})
    episode.invoke("billing_db", {})
    episode.invoke("switch_key", {"consumer": "primary"})
    return episode


def test_a_patch_rebuilds_the_new_value_and_shares_what_did_not_change():
    old = {"a": [1, 2], "b": {"x": 1, "y": [1]}, "c": "gone", "d": 1}
    new = {"a": [1, 2, 3], "b": {"x": 2, "y": [1]}, "d": 1.0, "e": None}
    change = diff(old, new)
    rebuilt = patch(old, change)
    assert rebuilt == new and type(rebuilt["d"]) is float and rebuilt["b"]["y"] is old["b"]["y"]
    assert diff(new, new) is None and patch(old, None) is old


def test_the_packed_record_is_much_smaller_and_unpacks_to_the_same_events():
    episode = short_day()
    full = json.loads(json.dumps(episode.evidence()))
    packed = json.loads(json.dumps(episode.packed_evidence()))
    assert unpack(packed["events"]) == full["events"]
    assert len(json.dumps(packed)) * 5 < len(json.dumps(full))
    episode.invoke("advance_time", {"amount": 1})  # packing continues from where it stopped
    assert unpack(json.loads(json.dumps(episode.packed_evidence()["events"]))) == json.loads(json.dumps(episode.evidence()["events"]))


def test_the_verifier_gives_the_same_verdict_on_both_forms_and_rejects_an_edited_one():
    episode = short_day()
    full = verify_envelope(seal(episode.evidence(), KEY), KEY)
    envelope = json.loads(json.dumps(seal(episode.packed_evidence(), KEY)))
    packed = verify_envelope(envelope, KEY)
    assert packed.get("error") is None and packed == full
    edited = json.loads(json.dumps(envelope))
    edited["payload"]["events"][-1]["state_after"] = None
    assert "signature" in verify_envelope(edited, KEY)["error"]
    resigned = seal(edited["payload"], KEY)  # even with the key, an event that no longer follows from the last is caught
    assert "mismatch" in verify_envelope(resigned, KEY)["error"]
