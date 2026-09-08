"""No native library is imported; every controller operation below is a Python fake."""
import copy
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eid_portable_forward_edit_profile import (EditProbe, compare_configuration, mutation_issues,
                                               navigation, shared_identity, shared_member_offset,
                                               values_from_hex)


VALUES = [struct.pack("<4f", *v) for v in ((0.8, 0.1, 0.3, 1), (0.1, 0.85, 0.2, 1))]
EVENTS = [31, 35, 39, 43]


def witness(event, edited=False):
    return {"buffer": {"patchHex": "ab" if edited else "00"}, "output": {
        "eventId": event, "pixels": [{"bytes": 4, "sha256": "edit" if edited else "original"}],
        "regions": [{"eventId": e, "uintBits": [e + (100 if edited else 0), 0, 0, 1]} for e in EVENTS]}}


class ConfigurationTests(unittest.TestCase):
    def test_values_are_two_distinct_exact_finite_float4_blocks(self):
        self.assertEqual(values_from_hex(",".join(value.hex() for value in VALUES)), VALUES)
        for text in ("00,01", VALUES[0].hex(), VALUES[0].hex() + "," + VALUES[0].hex(),
                     VALUES[0].hex() + "," + struct.pack("<4f", float("nan"), 0, 0, 1).hex()):
            with self.assertRaises(ValueError):
                values_from_hex(text)

    def test_sharing_uses_resource_and_exact_absolute_member_offset(self):
        rows = [{"buffer": 12, "absoluteOffset": 256, "bytes": 16} for _ in EVENTS]
        self.assertEqual(shared_identity(rows), (12, 256, 16))
        for key, value in (("buffer", 13), ("absoluteOffset", 272), ("bytes", 32)):
            changed = copy.deepcopy(rows)
            changed[2][key] = value
            with self.assertRaises(ValueError):
                shared_identity(changed)
        with self.assertRaises(ValueError):
            shared_identity(rows[:3])

    def test_reflection_in_bounds_does_not_authorize_a_truncated_bound_cb_view(self):
        self.assertEqual(shared_member_offset(1024, 272, 256, 272), 1280)
        # Same underlying buffer and reflected layout, but the active descriptor stops early.
        for view_size in (0, 16, 256, 271):
            with self.subTest(view_size=view_size), self.assertRaises(ValueError):
                shared_member_offset(1024, view_size, 256, 272)
        self.assertEqual(shared_member_offset(256, 16, 0, 16), 256)
        self.assertEqual(shared_member_offset(256, (1 << 64) - 1, 0, 16), 256)
        for values in ((0, 1024, 256, 271), (0, -1, 0, 16),
                       ((1 << 64) - 8, 16, 0, 16), (0, 16, -1, 16)):
            with self.assertRaises(ValueError):
                shared_member_offset(*values)

    def test_native_setter_requires_exact_generation_and_no_old_cursor_hit(self):
        row = {"api": "ReplayController", "op": "shared_buffer_patch", "event": 35,
               "generation": 2, "bytes": 16, "ranges": 1}
        self.assertFalse(mutation_issues([row], 35, 1, 2, 16))
        for records, before, after in (([], 1, 2), ([row], 2, 2),
                                      ([dict(row, generation=3)], 1, 2),
                                      ([dict(row, event=43)], 1, 2),
                                      ([dict(row, bytes=32)], 1, 2),
                                      ([row, row], 1, 2),
                                      ([row, {"api": "D3D11LiveForward", "hit": 1}], 1, 2)):
            self.assertTrue(mutation_issues(records, 35, before, after, 16))
        undo = dict(row, ranges=0, bytes=0)
        self.assertFalse(mutation_issues([undo], 35, 1, 2, 0))

    def data(self, edited=True):
        baseline = {event: witness(event) for event in EVENTS}
        references = {event: [witness(event, edited), witness(event, edited)] for event in EVENTS}
        observed = [{"eventId": event, "witness": witness(event, edited)} for event in EVENTS]
        return baseline, references, observed

    def test_all_four_shared_draws_must_change_and_match_forced_outputs(self):
        baseline, refs, visits = self.data()
        rows, issues = compare_configuration(visits, refs, baseline, EVENTS, witness(35, True))
        self.assertFalse(issues)
        self.assertTrue(all(row["ownRegionChanged"] for row in rows))
        refs[39] = [copy.deepcopy(baseline[39]) for _ in range(2)]
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])

    def test_stale_immediate_image_cannot_be_hidden_by_later_good_navigation(self):
        baseline, refs, visits = self.data()
        stale = witness(35, False)
        stale["buffer"] = copy.deepcopy(witness(35, True)["buffer"])
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS, stale)[1])

    def test_undo_requires_original_bytes_and_full_output(self):
        baseline, refs, visits = self.data(edited=False)
        self.assertFalse(compare_configuration(visits, refs, baseline, EVENTS,
                                               require_original=True)[1])
        for values in refs.values():
            for value in values:
                value["output"]["pixels"][0]["sha256"] = "stale unbound texture"
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS, require_original=True)[1])

    def test_unstable_reference_and_zero_output_effect_never_pass(self):
        baseline, refs, visits = self.data()
        refs[35][1]["buffer"]["patchHex"] = "ff"
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])
        baseline, refs, visits = self.data(False)
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])

    def test_post_edit_navigation_starts_at_fixed_B_then_exercises_all_draws(self):
        self.assertEqual(navigation(EVENTS)[0], ("postEditBD", 43))
        self.assertEqual([row[1] for row in navigation(EVENTS)], [43, 31, 35, 43, 31, 39, 43])


class FakeRecorder:
    def __init__(self, controller):
        self.controller = controller

    def measure(self, label, callback, event=None, cleanup=False):
        self.controller.trace.append(label)
        value = callback()
        records = []
        if label.endswith(".setter"):
            records = [{"api": "ReplayController", "op": "shared_buffer_patch", "event": 35,
                        "generation": self.controller.generation, "ranges": 1, "bytes": 16}]
        return value, {"wallMs": 1.0, "timingRecords": records}

    def publish(self):
        pass


class ImmediateSetterTests(unittest.TestCase):
    def make(self, stale=False):
        ctrl = SimpleNamespace(generation=0, value=b"\0" * 16, trace=[], event=35, stale=stale)
        status = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": False, "events": [],
                  "hasInitialCheckpoint": False}
        for field in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                      "builds", "hits", "misses", "invalidations", "rejected"):
            status[field] = 0
        ctrl.GetReplayCheckpointStatus = lambda: json.dumps(status)
        ctrl.GetSharedBufferPatchGeneration = lambda: ctrl.generation
        ctrl.GetCurrentEventID = lambda: ctrl.event
        def set_value(buffer, offset, data):
            ctrl.value, ctrl.generation = data, ctrl.generation + 1
            return ""
        ctrl.SetSharedBufferPatch = set_value
        ctrl.GetBufferData = lambda resource, begin, count: ctrl.value
        ctrl.SetFrameEvent = lambda *args: self.fail("Immediate slider witness must not call SetFrameEvent")
        report = {"issues": [], "mutations": [], "totalReadbackBytes": 0}
        recorder = FakeRecorder(ctrl)
        run = EditProbe(ctrl, None, recorder, report, "D3D11", EVENTS, [], {}, [], 12, 256,
                        (256, 16, 0), 1024, False, 1)
        return run, ctrl, report

    def test_consecutive_setters_read_immediately_at_same_eid(self):
        run, ctrl, report = self.make()
        def output(*args):
            return {"eventId": 35, "pixels": [{"bytes": 4, "sha256": ctrl.value.hex()}], "regions": []}
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            first = run.change("value1", VALUES[0])
            second = run.change("value2", VALUES[1])
        self.assertNotEqual(first, second)
        self.assertEqual(ctrl.event, 35)
        self.assertEqual(ctrl.trace, ["value1.setter", "value1.immediate.readback",
                                      "value2.setter", "value2.immediate.readback"])
        self.assertFalse(report["issues"])

    def test_stale_eid_after_setter_fails_before_any_repair(self):
        run, ctrl, report = self.make()
        original = ctrl.SetSharedBufferPatch
        def stale(*args):
            original(*args)
            ctrl.event = 43
            return ""
        ctrl.SetSharedBufferPatch = stale
        with patch("eid_portable_forward_edit_profile.complete_witness", return_value={
                "eventId": 43, "pixels": [], "regions": []}):
            with self.assertRaises(RuntimeError):
                run.change("value1", VALUES[0])

    def test_enabled_phase_cannot_pass_without_a_positive_native_hit(self):
        run, ctrl, report = self.make()
        run.report.update(visits=[], phases=[])
        run.baseline = {event: witness(event) for event in EVENTS}
        def visit(label, event, force=False, expected=None):
            return {"eventId": event, "witness": witness(event, True),
                    "liveForward": {"matchingHits": 0}}
        run.visit = visit
        run.phase("value", VALUES[0])
        self.assertTrue(any("positive hit requirement failed" in issue for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
