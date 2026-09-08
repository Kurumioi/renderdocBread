from __future__ import annotations

import importlib.util
import ast
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "cbuffer_alias_identity.py"
    spec = importlib.util.spec_from_file_location("tested_cbuffer_alias_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_top_members_survive_ambiguous_physical_abi_projection():
    block = SimpleNamespace(name="cbuffer0", fixedBindNumber=3,
                            fixedBindSetOrSpace=2, byteSize=44,
                            variables=[SimpleNamespace(name="unknown", byteOffset=0)])
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]),
        lambda variables: [{"path": "unknown", "absoluteByteOffset": 0, "byteSize": 44}],
        lambda fields, size: False,
    )[0]
    assert result["block"] == "cbuffer0"
    assert result["members"] == [{"name": "unknown", "offset": 0}]
    assert (result["bind"], result["space"]) == (3, 2)
    assert result["memberMappingAvailable"] is False
    assert result["leaves"] == []


def test_only_proven_scalar_or_vector_leaves_are_exposed_for_offset_mapping():
    block = SimpleNamespace(name="Constants", fixedBindNumber=0,
                            fixedBindSetOrSpace=0, byteSize=64,
                            variables=[SimpleNamespace(name="nested", byteOffset=0)])
    fields = [{"path": "nested", "members": [
        {"path": "nested.tint", "absoluteByteOffset": 4, "byteSize": 16, "elements": 1},
        {"path": "nested.array", "absoluteByteOffset": 32, "byteSize": 32, "elements": 2},
        {"path": "nested.structArray", "elements": 2, "members": [
            {"path": "nested.structArray.scalar", "absoluteByteOffset": 48,
             "byteSize": 4, "elements": 1}]},
    ]}]
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]), lambda variables: fields,
        lambda fields, size: True,
    )[0]
    assert result["members"] == [{"name": "nested", "offset": 0}]
    assert result["leaves"] == [{"path": "nested.tint", "offset": 4, "byteSize": 16}]


def _wrapper(monkeypatch, *, owned=False):
    bridge_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
    tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                and item.name == "_get_shader_cbuffer_alias_identity")
    package = "tested_cbuffer_alias_wrapper"
    monkeypatch.setitem(sys.modules, package, types.ModuleType(package))
    monkeypatch.setitem(sys.modules, package + ".cbuffer_alias_identity", _module())
    original = SimpleNamespace(constantBlocks=[SimpleNamespace(
        name="cbuffer0", fixedBindNumber=0, fixedBindSetOrSpace=0, byteSize=44,
        variables=[SimpleNamespace(name="unknown", byteOffset=0)])])
    record = {"captureId": "a" * 64, "captureSessionId": "b" * 32,
              "originalResource": 17, "capturedOriginalReflection": original}
    restored, checks = [], []
    namespace = {
        "__package__": package,
        "_require_capture_identity": lambda value: value,
        "_capture_session_id": "b" * 32,
        "_shader_override_events": lambda params: [{
            "eventId": params["event_id"],
            "expectedOriginalResourceId": params["expected_original_resource_id"],
            "expectedOriginalSha256": params["expected_original_sha256"]}],
        "_normalise_stage_name": lambda value: value,
        "_available_shader_stages": lambda values: [("fragment", "ps")],
        "_shared_shader_replacements": {17: record} if owned else {},
        "_event_shader_overrides": {},
        "_invoke_on_ui_sync": lambda callback: callback(),
        "_context_resource_ids": lambda: {17: 17},
        "_require_shared_shader_ui_owner": lambda resource, owner: checks.append((resource, owner)),
        "_request_visible_event_id": lambda default: 99,
        "_shader_api_token": lambda ctrl: "d3d12",
        "_shader_constant_fields": lambda variables: [{"path": "unknown", "byteSize": 44}],
        "_constant_field_annotations_are_complete": lambda fields, size: False,
        "_set_frame_event": lambda ctrl, event, force: restored.append(event),
        "_replay": lambda callback: callback(object()),
    }

    def validate(ctrl, event, stage, stage_name, entry, **kwargs):
        assert (event["eventId"], stage, stage_name, entry) == (12, "ps", "fragment", "MainPS")
        assert kwargs["require_d3d11_state"] is False
        assert kwargs["owned_shared_replacement"] is (record if owned else None)
        return {"reflection": original}

    namespace["_validate_live_shader_event"] = validate
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge_path), "exec"), namespace)
    params = {"expected_capture_id": "a" * 64, "expected_capture_session_id": "b" * 32,
              "event_id": 12, "stage": "fragment", "entry_point": "MainPS",
              "expected_original_resource_id": 17, "expected_original_sha256": "c" * 64}
    return namespace, params, restored, checks


@pytest.mark.parametrize("owned", [False, True])
def test_wrapper_reads_exact_original_identity_and_restores_event(monkeypatch, owned):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=owned)
    result = namespace["_get_shader_cbuffer_alias_identity"](params)
    assert result["captureId"] == "a" * 64
    assert result["captureSessionId"] == "b" * 32
    assert (result["eventId"], result["stage"], result["entryPoint"], result["shaderResourceId"]) == (12, "fragment", "MainPS", 17)
    assert result["rawSha256"] == "c" * 64
    assert result["blocks"][0]["members"] == [{"name": "unknown", "offset": 0}]
    assert result["blocks"][0]["memberMappingAvailable"] is False
    assert result["blocks"][0]["leaves"] == []
    assert restored == [99]
    assert len(checks) == 1


def test_wrapper_rejects_stale_shared_owner_before_using_its_reflection(monkeypatch):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=True)
    namespace["_shared_shader_replacements"][17]["captureSessionId"] = "d" * 32
    with pytest.raises(ValueError, match="different capture session"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == []
    assert checks == []


def test_wrapper_rejects_capture_change_during_lookup_and_restores_event(monkeypatch):
    namespace, params, restored, unused_checks = _wrapper(monkeypatch)
    validate = namespace["_validate_live_shader_event"]

    def changed(*args, **kwargs):
        result = validate(*args, **kwargs)
        namespace["_capture_session_id"] = "d" * 32
        return result

    namespace["_validate_live_shader_event"] = changed
    with pytest.raises(RuntimeError, match="capture changed during"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == [99]
