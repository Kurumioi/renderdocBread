"""Bounded whole-output observations for shader organization (Python 3.6).

This is captured-input evidence, never a proof for unobserved inputs. The
comparison executes in one replay callback and restores the previous mapping
before returning; it does not publish a replacement or update UI ownership.
"""
import hashlib
import math
import struct
import time


TOLERANCES = {"floatAbsolute": 0.0001, "floatRelative": 0.001,
              "halfFloatAbsolute": 1.0 / 1024.0,
              "depthAbsolute": 0.000001, "integerExact": True,
              "normalizedQuantizationSteps": 1}
OUTPUT_LIMIT = 32 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024


def _enum(value):
    raw = "" if value is None else value
    name = getattr(raw, "name", None)
    return str(name if name is not None else raw).strip().rsplit(".", 1)[-1].lower()


def compare_bytes(before, after, descriptor):
    """Compare every stored component, without diluting errors in an average."""
    if not before or len(before) != len(after):
        raise ValueError("output bytes are empty or changed size")
    exact = before == after
    kind = _enum(descriptor["componentType"])
    width = descriptor["componentByteWidth"]
    count = descriptor["componentCount"]
    result = {"byteExact": exact, "byteSize": len(before),
              "baselineSha256": hashlib.sha256(before).hexdigest(),
              "candidateSha256": hashlib.sha256(after).hexdigest(),
              "maximumAbsoluteError": 0.0, "failedComponents": 0,
              "comparedComponents": 0, "accepted": exact}
    formats = {"float": {2: "e", 4: "f"}, "depth": {4: "f"},
               "uint": {1: "B", 2: "H", 4: "I"},
               "sint": {1: "b", 2: "h", 4: "i"},
               "unorm": {1: "B", 2: "H"},
               "unormsrgb": {1: "B", 2: "H"},
               "snorm": {1: "b", 2: "h"}}
    fmt = formats.get(kind, {}).get(width)
    regular = _enum(descriptor["formatType"]) == "regular" and count in (1, 2, 3, 4)
    if not regular or not fmt:
        if exact:
            # Exact storage equality is still useful for packed formats, but
            # do not pretend those bytes were decoded into numeric components.
            result["comparedComponents"] = len(before)
            result["comparisonKind"] = "byte-exact"
            return result
        raise ValueError("changed packed/compressed output has no numeric comparator")
    if len(before) != descriptor["width"] * descriptor["height"] * count * width:
        raise ValueError("output format or row layout is unsupported")
    result["comparisonKind"] = "numeric-components"
    normalized = kind in ("unorm", "unormsrgb", "snorm")
    scale = float((1 << (width * 8 - (1 if kind == "snorm" else 0))) - 1) if normalized else 1.0
    depth = descriptor["kind"] == "depth" or kind == "depth"
    absolute = TOLERANCES["depthAbsolute"] if depth else TOLERANCES["floatAbsolute"]
    if not depth and kind == "float" and width == 2:
        # One binary16 storage quantum near unit magnitude. Cancellation and
        # different legal round-trip instruction order can otherwise reject
        # visually negligible half-target changes under the float32 floor.
        absolute = TOLERANCES["halfFloatAbsolute"]
    relative = 0.0 if depth else TOLERANCES["floatRelative"]
    if normalized and not depth:
        # Color quantization must not relax the absolute depth limit.
        absolute = 1.0 / scale
        relative = 0.0
    if kind in ("uint", "sint"):
        absolute = relative = 0.0
    if exact and kind not in ("float", "depth"):
        result["comparedComponents"] = len(before) // width
        return result
    for left, right in zip(struct.iter_unpack("<" + fmt, before), struct.iter_unpack("<" + fmt, after)):
        a, b = left[0] / scale, right[0] / scale
        if kind == "snorm":
            a, b = max(-1.0, a), max(-1.0, b)
        result["comparedComponents"] += 1
        if not math.isfinite(a) or not math.isfinite(b):
            result["failedComponents"] += 1
            continue
        error = abs(a - b)
        result["maximumAbsoluteError"] = max(result["maximumAbsoluteError"], error)
        if error > absolute + relative * max(abs(a), abs(b)) + 1e-15:
            result["failedComponents"] += 1
    result["accepted"] = result["failedComponents"] == 0
    return result


def _outputs(bridge, ctrl):
    rd = bridge.rd
    pipeline = ctrl.GetPipelineState()
    textures = {bridge._resource_id_int(t.resourceId): t for t in ctrl.GetTextures()}
    outputs, total, materialized_total = [], 0, 0
    targets = [("color", i, t) for i, t in enumerate(pipeline.GetOutputTargets() or [])]
    targets.append(("depth", 0, pipeline.GetDepthTarget()))
    for kind, index, target in targets:
        rid = bridge._resource_id_int(getattr(target, "resource", None))
        if not rid:
            continue
        texture = textures.get(rid)
        if texture is None or int(texture.msSamp) != 1 or int(texture.depth) != 1:
            raise ValueError("numeric observation requires single-sample 2D outputs")
        size = int(texture.byteSize)
        if size <= 0 or size > OUTPUT_LIMIT or total + size > TOTAL_LIMIT:
            raise ValueError("output exceeds the bounded numeric observation budget")
        total += size
        mip, array_slice = int(target.firstMip), int(target.firstSlice)
        array_size = int(getattr(texture, "arraysize", 1))
        num_slices = getattr(target, "numSlices", None)
        if (
            mip < 0 or mip >= int(getattr(texture, "mips", 1))
            or array_slice < 0 or array_slice >= array_size
            or (num_slices is None and array_size != 1)
            or (num_slices is not None and int(num_slices) != 1)
        ):
            raise ValueError("layered output observation is unsupported")
        # D3D resources can be typeless while the bound RTV/DSV supplies the
        # actual component interpretation. Readback contains that view's raw
        # storage; no shader or image conversion is requested here.
        view_format = getattr(target, "format", None)
        fmt = (view_format if view_format is not None
               and _enum(view_format.type) != "undefined"
               and _enum(view_format.compType) != "typeless"
               else texture.format)
        descriptor = {"kind": kind, "index": index, "resourceId": rid,
                      "mip": mip, "slice": array_slice,
                      "width": max(1, int(texture.width) >> mip),
                      "height": max(1, int(texture.height) >> mip),
                      "format": bridge._fmt_str(fmt), "formatType": _enum(fmt.type),
                      "componentType": _enum(fmt.compType),
                      "componentByteWidth": int(fmt.compByteWidth),
                      "componentCount": int(fmt.compCount)}
        raw = bytes(ctrl.GetTextureData(target.resource, rd.Subresource(mip, array_slice, 0)))
        materialized_total += len(raw)
        if not raw or len(raw) > OUTPUT_LIMIT or materialized_total > TOTAL_LIMIT:
            raise ValueError("materialized output exceeds observation budget")
        outputs.append((descriptor, raw))
    if not outputs or not any(d["kind"] == "color" for d, unused in outputs):
        raise ValueError("numeric observation requires at least one color output")
    return outputs


def compare_candidates(bridge, params):
    """Execute one isolated, restored A/B observation using exact native blobs."""
    started = time.monotonic()
    rd = bridge.rd
    capture_id = bridge._require_capture_identity(params.get("expected_capture_id"))
    session = params.get("expected_capture_session_id")
    if not session or session != bridge._capture_session_id:
        raise ValueError("capture session changed before shader observation")
    if bridge._normalise_stage_name(params.get("stage")) != "fragment":
        raise ValueError("numeric organization observation currently supports fragment shaders")
    events = bridge._shader_override_events(params)
    if len(events) != 1:
        raise ValueError("numeric observation requires one exact event")
    event = events[0]
    rid = event["expectedOriginalResourceId"]
    if any(r.get("expectedOriginalResourceId") == rid for r in bridge._event_shader_overrides.values()):
        raise ValueError("event overrides must be resolved before numeric observation")
    existing = bridge._shared_shader_replacements.get(rid)
    original = existing.get("originalResource") if existing else bridge._invoke_on_ui_sync(lambda: bridge._context_resource_ids().get(rid))
    if original is None:
        raise ValueError("captured shader is unavailable")
    bridge._require_shared_shader_ui_owner(original, existing)
    entry = str(params.get("entry_point", ""))
    stage = dict(bridge._available_shader_stages(["fragment"]))["fragment"]
    encoding_name = str(params.get("encoding", "")).lower()
    encoding_attr = {"dxil": "DXIL", "dxbc": "DXBC", "spirv": "SPIRV", "opengl-spirv": "OpenGLSPIRV"}.get(encoding_name)
    if not encoding_attr or not entry:
        raise ValueError("native candidate encoding and entry point are required")
    encoding = getattr(rd.ShaderEncoding, encoding_attr)
    candidates = []
    for key in ("baseline", "candidate"):
        item = params[key]
        unused, blob, digest = bridge._read_shader_candidate({"candidate_path": item["path"], "expected_sha256": item["rawSha256"]})
        candidates.append((blob, digest))
    visible_event = bridge._request_visible_event_id(0)

    def run(ctrl):
        targets, mutated, restored = [], False, False
        api = bridge._shader_api_token(ctrl)
        try:
            live = bridge._validate_live_shader_event(ctrl, event, stage, "fragment", entry,
                require_d3d11_state=(api == "d3d11"), owned_shared_replacement=existing)
            if list(getattr(live["reflection"], "readWriteResources", []) or []):
                raise ValueError("UAV side effects require a dedicated validation fixture")
            for blob, digest in candidates:
                # Native DXIL creation may deduplicate an exact original to
                # its captured ResourceId. Observe that baseline directly;
                # never replace a shader with itself or free the original.
                if digest == event.get("expectedOriginalSha256"):
                    targets.append(None)
                    continue
                target, messages = ctrl.BuildTargetShader(entry, encoding, blob, rd.ShaderCompileFlags(), stage)
                if not bridge._resource_id_int(target):
                    raise RuntimeError("BuildTargetShader failed: %s" % messages)
                targets.append(target)
                target_entry = bridge._exact_shader_entry(list(ctrl.GetShaderEntryPoints(target)), entry, "fragment")
                if target_entry is None:
                    raise ValueError("candidate entry is ambiguous")
                reflection = ctrl.GetShader(rd.ResourceId(), target, target_entry)
                if not reflection or hashlib.sha256(bridge._shader_raw_bytes(reflection)).hexdigest() != digest:
                    raise ValueError("built candidate identity changed")
                if list(getattr(reflection, "readWriteResources", []) or []):
                    raise ValueError("candidate introduces UAV side effects")
                if encoding_name == "dxil":
                    compatible = bridge._dxil_ambiguous_interface_compatible(ctrl, live["pipelineResource"], live["reflection"], reflection,
                        baseline_access=live.get("dxilAccessSignature"), baseline_layout_evidence=live.get("dxilLayoutEvidence"))
                else:
                    compatible = bridge._physical_shader_interfaces_compatible(live["reflection"], reflection)
                if not compatible:
                    raise ValueError("numeric observation cannot bypass a physical ABI mismatch")
            observations = []
            for target in targets:
                mutated = True
                if target is None:
                    bridge._remove_shared_shader_resource(ctrl, api, original)
                else:
                    bridge._replace_shared_shader_resource(ctrl, api, original, target)
                ctrl.ClearReplayCache()
                bridge._set_frame_event(ctrl, event["eventId"], True)
                observations.append(_outputs(bridge, ctrl))
            if len(observations[0]) != len(observations[1]):
                raise ValueError("output attachment count changed")
            metrics = []
            for (left_desc, left), (right_desc, right) in zip(*observations):
                if left_desc != right_desc:
                    changed = ["%s: %r -> %r" % (key, left_desc.get(key), right_desc.get(key))
                               for key in sorted(set(left_desc) | set(right_desc))
                               if left_desc.get(key) != right_desc.get(key)]
                    raise ValueError("output attachment identity changed (" + "; ".join(changed) + ")")
                item = compare_bytes(left, right, left_desc)
                item["output"] = left_desc
                metrics.append(item)
            if session != bridge._capture_session_id:
                raise RuntimeError("capture session changed during shader observation")
            result = {"captureId": capture_id, "captureSessionId": session,
                      "eventId": event["eventId"], "accepted": all(m["accepted"] for m in metrics),
                      "metrics": {"outputs": metrics, "coverage": "all-stored-output-data"},
                      "tolerances": dict(TOLERANCES),
                      "sampleCount": sum(m["comparedComponents"] for m in metrics),
                      "semanticEquivalenceVerified": False}
        except ValueError as error:
            result = {"captureId": capture_id, "captureSessionId": session,
                      "eventId": event["eventId"], "accepted": False,
                      "reason": str(error), "sampleCount": 0,
                      "metrics": {"outputs": [], "coverage": "unsupported"},
                      "tolerances": dict(TOLERANCES),
                      "semanticEquivalenceVerified": False}
        finally:
            try:
                if mutated:
                    if existing:
                        bridge._replace_shared_shader_resource(ctrl, api, original, existing["targetResource"])
                    else:
                        bridge._remove_shared_shader_resource(ctrl, api, original)
                    ctrl.ClearReplayCache()
                bridge._set_frame_event(ctrl, visible_event, True)
                restored = True
            finally:
                if restored:
                    for target in targets:
                        if target is not None:
                            ctrl.FreeTargetResource(target)
                # If restoration failed, retain targets referenced by replay;
                # the failed RPC cannot produce acceptable review evidence.
        result["replayRestored"] = True
        result["durationMilliseconds"] = round((time.monotonic() - started) * 1000)
        return result

    return bridge._replay(run)
