from __future__ import annotations

import hashlib
import enum
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace as NS
from unittest import mock

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_validation.py"
spec = importlib.util.spec_from_file_location("replaylab_shader_numeric_validation", MODULE_PATH)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def descriptor(kind="Float", width=4, count=1, pixels=1, output_kind="color", format_type="Regular"):
    return {"componentType": kind, "componentByteWidth": width,
            "componentCount": count, "width": pixels, "height": 1,
            "kind": output_kind, "formatType": format_type}


def packed(fmt, *values):
    return struct.pack("<" + fmt * len(values), *values)


def test_half_target_allows_one_unit_scale_storage_quantum_but_not_larger_changes():
    desc = descriptor("Float", width=2)
    assert validation.compare_bytes(packed("e", 0), packed("e", 0.0009), desc)["accepted"]
    assert not validation.compare_bytes(packed("e", 0), packed("e", 0.002), desc)["accepted"]
    assert not validation.compare_bytes(packed("f", 0), packed("f", 0.0009), descriptor())["accepted"]


@pytest.mark.parametrize("kind,format_type", [
    ("Float", "Regular"), ("CompType.Float", "ResourceFormatType.Regular"),
])
def test_float_tolerance_accepts_rounding_but_rejects_any_bad_component(kind, format_type):
    desc = descriptor(kind, count=4, format_type=format_type)
    before = packed("f", 1, 10, 0, -100)
    rounded = packed("f", 1.0005, 10.005, 0.00005, -100.05)
    metrics = validation.compare_bytes(before, rounded, desc)
    assert metrics["accepted"] and metrics["comparedComponents"] == 4
    assert not metrics["byteExact"]
    wrong = packed("f", 1.0005, 10.005, 0.01, -100.05)
    metrics = validation.compare_bytes(before, wrong, desc)
    assert not metrics["accepted"] and metrics["failedComponents"] == 1


@pytest.mark.parametrize("kind,fmt,width", [
    ("UNorm", "B", 1), ("CompType.UNormSRGB", "B", 1),
    ("UNorm", "H", 2), ("SNorm", "h", 2),
])
def test_normalized_tolerance_is_exactly_one_stored_quantization_step(kind, fmt, width):
    desc = descriptor(kind, width=width)
    assert validation.compare_bytes(packed(fmt, 40), packed(fmt, 41), desc)["accepted"]
    assert not validation.compare_bytes(packed(fmt, 40), packed(fmt, 42), desc)["accepted"]


def test_snorm_negative_endpoint_aliases_are_equivalent():
    assert validation.compare_bytes(packed("b", -128), packed("b", -127), descriptor("SNorm", width=1))["accepted"]


@pytest.mark.parametrize("kind,fmt,maximum", [("UInt", "I", 0xFFFFFFFF), ("SInt", "i", 0x7FFFFFFF)])
def test_integer_outputs_require_exact_values_even_near_largest_integer(kind, fmt, maximum):
    metrics = validation.compare_bytes(packed(fmt, maximum), packed(fmt, maximum - 1), descriptor(kind))
    assert not metrics["accepted"] and metrics["maximumAbsoluteError"] == 1


def test_depth_uses_absolute_tolerance_without_relative_slack():
    desc = descriptor("Depth", output_kind="depth")
    assert validation.compare_bytes(packed("f", 0.5), packed("f", 0.5000005), desc)["accepted"]
    assert not validation.compare_bytes(packed("f", 0.5), packed("f", 0.50001), desc)["accepted"]


@pytest.mark.parametrize("value", [0, 32767, 65534])
def test_d16_unorm_depth_keeps_strict_depth_tolerance(value):
    desc = descriptor("UNorm", width=2, output_kind="depth")
    raw = packed("H", value)
    assert validation.compare_bytes(raw, raw, desc)["accepted"]
    metrics = validation.compare_bytes(raw, packed("H", value + 1), desc)
    assert not metrics["accepted"]
    assert metrics["failedComponents"] == 1
    assert metrics["maximumAbsoluteError"] == pytest.approx(1.0 / 65535)
    assert metrics["maximumAbsoluteError"] > validation.TOLERANCES["depthAbsolute"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_float_outputs_are_rejected_even_when_bytes_are_identical(value):
    raw = packed("f", value)
    metrics = validation.compare_bytes(raw, raw, descriptor())
    assert metrics["byteExact"] and not metrics["accepted"]
    assert metrics["failedComponents"] == 1


@pytest.mark.parametrize("kind,width,fmt", [("Float", 4, "f"), ("UInt", 4, "I"), ("Float", 2, "e")])
def test_exact_regular_output_counts_components_instead_of_pixels(kind, width, fmt):
    raw = packed(fmt, *([1] * 8))
    metrics = validation.compare_bytes(raw, raw, descriptor(kind, width=width, count=4, pixels=2))
    assert metrics["accepted"] and metrics["comparedComponents"] == 8


def test_changed_packed_output_rejects_but_exact_storage_is_recorded_as_bytes():
    desc = descriptor("UNorm", width=0, count=4, format_type="ResourceFormatType.R10G10B10A2")
    raw = b"abcd"
    metrics = validation.compare_bytes(raw, raw, desc)
    assert metrics["accepted"] and metrics["comparisonKind"] == "byte-exact"
    with pytest.raises(ValueError, match="packed/compressed"):
        validation.compare_bytes(raw, b"abce", desc)


@pytest.mark.parametrize("before,after", [(b"", b""), (b"1234", b"12")])
def test_empty_or_size_changed_outputs_are_not_accepted(before, after):
    with pytest.raises(ValueError, match="empty or changed size"):
        validation.compare_bytes(before, after, descriptor())


def test_same_bytes_with_invalid_regular_row_layout_do_not_inflate_coverage():
    with pytest.raises(ValueError, match="row layout"):
        validation.compare_bytes(b"1234", b"1234", descriptor(count=4))


def texture_format(kind="CompType.Float", width=4):
    return NS(type="ResourceFormatType.Regular", compType=kind, compByteWidth=width, compCount=1)


class FakeController:
    def __init__(self):
        self.calls = []
        self.targets = {}
        self.mapping = None
        self.event_id = 17
        self.build_count = 0
        self.build_failure = None
        self.observe_failure = None
        self.observe_count = 0
        self.texture = NS(resourceId=41, msSamp=1, depth=1, byteSize=4,
                          width=1, height=1, mips=1, arraysize=1, format=texture_format())
        self.output = NS(resource=41, firstMip=0, firstSlice=0, numSlices=1, format=texture_format())
        self.outputs = [self.output]
        self.depth = NS(resource=0)
        self.output_values = {201: packed("f", 1), 202: packed("f", 1.0005)}

    def BuildTargetShader(self, entry, encoding, blob, flags, stage):
        self.build_count += 1
        self.calls.append(("build", self.build_count))
        if self.build_failure == self.build_count:
            return 0, "injected compiler failure"
        target = 200 + self.build_count
        self.targets[target] = blob
        return target, ""

    def GetShaderEntryPoints(self, target):
        return [NS(name="MainPS", stage="fragment")]

    def GetShader(self, unused_pipeline, target, entry):
        return NS(raw=self.targets[target], readWriteResources=[])

    def GetPipelineState(self):
        return NS(GetOutputTargets=lambda: self.outputs, GetDepthTarget=lambda: self.depth)

    def GetTextures(self):
        return [self.texture]

    def GetTextureData(self, resource, subresource):
        self.observe_count += 1
        self.calls.append(("observe", self.mapping, subresource))
        if self.observe_failure == self.observe_count:
            raise ValueError("injected unsupported output")
        return self.output_values.get(self.mapping, packed("f", 1))

    def ClearReplayCache(self):
        self.calls.append(("clear",))

    def FreeTargetResource(self, target):
        assert self.mapping != target
        self.calls.append(("free", target))
        del self.targets[target]


class FakeBridge:
    def __init__(self, existing=False):
        self.ctrl = FakeController()
        self.rd = NS(ShaderEncoding=NS(DXIL="DXIL", DXBC="DXBC"),
                     ShaderCompileFlags=lambda: None, ResourceId=lambda: 0,
                     Subresource=lambda mip, slice_id, sample: (mip, slice_id, sample))
        self._capture_session_id = "b" * 32
        self._event_shader_overrides = {}
        self._shared_shader_replacements = ({7: {"originalResource": 7, "targetResource": 999}} if existing else {})
        self.ctrl.mapping = 999 if existing else None
        self.replace_failure = None
        self.restore_failure = False
        self.visible_restore_failure = False
        self.compare_compatible = True
        self.event = {"eventId": 50, "expectedOriginalResourceId": 7}
        self.params = {"expected_capture_id": "a" * 64, "expected_capture_session_id": self._capture_session_id,
                       "stage": "fragment", "encoding": "dxbc", "entry_point": "MainPS"}
        for key, blob in (("baseline", b"baseline binary"), ("candidate", b"candidate binary")):
            self.params[key] = {"path": key, "rawSha256": hashlib.sha256(blob).hexdigest()}

    def _require_capture_identity(self, value):
        return value

    def _normalise_stage_name(self, value):
        return value

    def _shader_override_events(self, params):
        return [self.event]

    def _invoke_on_ui_sync(self, fn):
        return fn()

    def _context_resource_ids(self):
        return {7: 7}

    def _require_shared_shader_ui_owner(self, original, existing):
        return None

    def _available_shader_stages(self, stages):
        return [("fragment", "fragment")]

    def _read_shader_candidate(self, params):
        blob = (b"baseline binary" if params["candidate_path"] == "baseline" else b"candidate binary")
        return None, blob, hashlib.sha256(blob).hexdigest()

    def _request_visible_event_id(self, fallback):
        return 17

    def _shader_api_token(self, ctrl):
        return "d3d12"

    def _validate_live_shader_event(self, ctrl, *args, **kwargs):
        ctrl.event_id = 50
        return {"reflection": NS(readWriteResources=[]), "pipelineResource": 3}

    def _resource_id_int(self, value):
        return int(value or 0)

    def _exact_shader_entry(self, entries, entry, stage):
        return entries[0]

    def _shader_raw_bytes(self, reflection):
        return reflection.raw

    def _physical_shader_interfaces_compatible(self, original, candidate):
        return self.compare_compatible

    def _dxil_ambiguous_interface_compatible(self, *args, **kwargs):
        return self.compare_compatible

    def _replace_shared_shader_resource(self, ctrl, api, original, target):
        ctrl.calls.append(("replace", target))
        ctrl.mapping = target
        if target == self.replace_failure or (target == 999 and self.restore_failure):
            raise RuntimeError("injected replacement failure")

    def _remove_shared_shader_resource(self, ctrl, api, original):
        ctrl.calls.append(("remove", original))
        if self.restore_failure:
            raise RuntimeError("injected rollback failure")
        ctrl.mapping = None

    def _set_frame_event(self, ctrl, event, force):
        ctrl.calls.append(("event", event))
        if event == 17 and self.visible_restore_failure:
            raise RuntimeError("injected visible restore failure")
        ctrl.event_id = event

    def _fmt_str(self, fmt):
        return str(fmt.compType)

    def _replay(self, fn):
        return fn(self.ctrl)


def test_exact_captured_baseline_is_observed_without_building_or_freeing_original():
    bridge = FakeBridge(existing=True)
    bridge.event["expectedOriginalSha256"] = bridge.params["baseline"]["rawSha256"]
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is True
    assert result["replayRestored"] is True
    assert bridge.ctrl.mapping == 999
    assert sum(1 for call in bridge.ctrl.calls if call[0] == "free") == 1
    assert ("remove", 7) in bridge.ctrl.calls


@pytest.mark.parametrize("existing", [False, True])
def test_ab_observation_restores_prior_replacement_and_visible_event(existing):
    bridge = FakeBridge(existing)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["replayRestored"]
    assert result["sampleCount"] == 1
    assert not result["semanticEquivalenceVerified"]
    assert bridge.ctrl.mapping == (999 if existing else None)
    assert bridge.ctrl.event_id == 17 and bridge.ctrl.targets == {}


@pytest.mark.parametrize("failure", [1, 2])
def test_build_failure_restores_visible_event_and_releases_earlier_target(failure):
    bridge = FakeBridge()
    bridge.ctrl.build_failure = failure
    with pytest.raises(RuntimeError, match="BuildTargetShader failed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("failure", [1, 2])
@pytest.mark.parametrize("existing", [False, True])
def test_unsupported_output_returns_rejected_evidence_only_after_restore(failure, existing):
    bridge = FakeBridge(existing)
    bridge.ctrl.observe_failure = failure
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False and result["sampleCount"] == 0
    assert result["replayRestored"] is True
    assert "unsupported output" in result["reason"]
    assert bridge.ctrl.mapping == (999 if existing else None)
    assert bridge.ctrl.event_id == 17 and bridge.ctrl.targets == {}


def test_comparator_exception_restores_replay_before_propagating_error():
    bridge = FakeBridge()
    with mock.patch.object(validation, "compare_bytes", side_effect=RuntimeError("injected comparison failure")):
        with pytest.raises(RuntimeError, match="comparison failure"):
            validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("existing", [False, True])
def test_restore_failure_is_rpc_error_and_does_not_free_possibly_referenced_targets(existing):
    bridge = FakeBridge(existing)
    bridge.restore_failure = True
    bridge.ctrl.observe_failure = 1
    with pytest.raises(RuntimeError, match="failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert len(bridge.ctrl.targets) == 2
    assert not any(call[0] == "free" for call in bridge.ctrl.calls)


def test_visible_event_restore_failure_cannot_claim_replay_restored():
    bridge = FakeBridge()
    bridge.visible_restore_failure = True
    with pytest.raises(RuntimeError, match="visible restore failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert len(bridge.ctrl.targets) == 2


@pytest.mark.parametrize("target", [201, 202])
def test_partial_setter_failure_still_restores_original_mapping(target):
    bridge = FakeBridge()
    bridge.replace_failure = target
    with pytest.raises(RuntimeError, match="replacement failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


def test_physical_abi_mismatch_returns_rejection_without_applying_candidates():
    bridge = FakeBridge()
    bridge.compare_compatible = False
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["sampleCount"] == 0
    assert "physical ABI mismatch" in result["reason"] and result["replayRestored"]
    assert not any(call[0] == "replace" for call in bridge.ctrl.calls)


def test_capture_session_change_during_observation_is_error_after_restore():
    bridge = FakeBridge()
    original = bridge.ctrl.GetTextureData
    def read(resource, subresource):
        bridge._capture_session_id = "c" * 32
        return original(resource, subresource)
    bridge.ctrl.GetTextureData = read
    with pytest.raises(RuntimeError, match="capture session changed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.targets == {}


def test_typeless_resource_uses_bound_view_format_for_numeric_comparison():
    bridge = FakeBridge()
    bridge.ctrl.texture.format = texture_format("CompType.Typeless")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"]
    assert result["metrics"]["outputs"][0]["output"]["componentType"] == "float"


def test_native_integer_enums_preserve_zero_regular_member_and_named_component():
    class ResourceFormatType(enum.IntEnum):
        Regular = 0

    class CompType(enum.IntEnum):
        Float = 1

    bridge = FakeBridge()
    bridge.ctrl.output.format = NS(type=ResourceFormatType.Regular, compType=CompType.Float,
                                  compByteWidth=4, compCount=1)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"]
    output = result["metrics"]["outputs"][0]["output"]
    assert output["formatType"] == "regular" and output["componentType"] == "float"


@pytest.mark.parametrize("attribute,value", [("numSlices", 2), ("firstMip", 1), ("firstSlice", 1)])
def test_layered_or_out_of_range_output_is_unsupported(attribute, value):
    bridge = FakeBridge()
    setattr(bridge.ctrl.output, attribute, value)
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["sampleCount"] == 0 and result["replayRestored"]


def test_legacy_target_without_num_slices_cannot_silently_sample_only_one_array_layer():
    bridge = FakeBridge()
    del bridge.ctrl.output.numSlices
    bridge.ctrl.texture.arraysize = 2
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "layered" in result["reason"]


def test_per_output_and_combined_materialization_budgets_are_enforced():
    bridge = FakeBridge()
    with mock.patch.object(validation, "OUTPUT_LIMIT", 3):
        with pytest.raises(ValueError, match="budget"):
            validation._outputs(bridge, bridge.ctrl)
    bridge.ctrl.outputs = [bridge.ctrl.output, bridge.ctrl.output]
    bridge.ctrl.texture.byteSize = 1  # Actual readback must be independently bounded.
    with mock.patch.object(validation, "TOTAL_LIMIT", 7):
        with pytest.raises(ValueError, match="materialized output"):
            validation._outputs(bridge, bridge.ctrl)
