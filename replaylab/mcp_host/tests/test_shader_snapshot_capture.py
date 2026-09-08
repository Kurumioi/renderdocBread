from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from test_shader_console import BRIDGE_PATH, MODULE_PATH, ShaderConsoleFixture, console


class OtherShaderFixture(ShaderConsoleFixture):
    AUTHORIZATION_ID = "1" * 32
    WORKSPACE_ID = "2" * 32
    LIVE_ID = "3" * 32
    WATCH_ID = "4" * 32


def _read_document(fixture, binding):
    return console.load_authorized_source_document(
        str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
        capture_binding=binding)


@pytest.mark.parametrize("binding", [None, {}, {"loaded": False}])
def test_without_current_capture_never_reads_historical_workspace(tmp_path, binding):
    fixture = ShaderConsoleFixture(tmp_path)
    with mock.patch.object(console, "_read_json", side_effect=AssertionError("disk history read")), \
            mock.patch.object(console, "_workspace_model", side_effect=AssertionError("model read")), \
            mock.patch.object(console, "_load_generic_console_entries", return_value={
                "entries": [], "errors": [], "watchedPaths": []}):
        snapshot = console.load_console_snapshot(str(fixture.registry), binding)
    assert snapshot["captureBinding"] is None
    assert snapshot["workspaces"] == []
    with mock.patch.object(console, "_authorized_workspace", side_effect=AssertionError("source read")):
        with pytest.raises(console.ShaderConsoleError, match="capture"):
            _read_document(fixture, binding)


@pytest.mark.parametrize("field,value", [
    ("instanceId", "5678-other"),
    ("captureSessionId", "9" * 32),
    ("capturePath", "C:/captures/other.rdc"),
])
def test_other_instance_session_or_capture_is_never_visible(tmp_path, field, value):
    fixture = ShaderConsoleFixture(tmp_path)
    fixture.write_live()
    fixture.write_watch()
    binding = dict(fixture.capture_binding, **{field: value})
    snapshot = console.load_console_snapshot(str(fixture.registry), binding)
    assert snapshot["workspaces"] == []
    with pytest.raises(console.ShaderConsoleError, match="capture"):
        _read_document(fixture, binding)


def test_legacy_audit_without_capture_binding_cannot_authorize_current_panel(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    fixture.write_live(captureBinding=None)
    fixture.write_watch()
    assert console.load_console_snapshot(
        str(fixture.registry), fixture.capture_binding)["workspaces"] == []
    with pytest.raises(console.ShaderConsoleError, match="capture session"):
        _read_document(fixture, fixture.capture_binding)


def test_current_scoped_stopped_source_is_readable_but_not_auto_apply_ready(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    snapshot = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    workspace, = snapshot["workspaces"]
    assert workspace["captureBinding"] == fixture.capture_binding
    assert workspace["autoApplyReady"] is False
    document = _read_document(fixture, fixture.capture_binding)
    assert document["captureBinding"] == fixture.capture_binding
    assert document["liveEditId"] == fixture.LIVE_ID
    assert document["watchId"] == fixture.WATCH_ID
    assert "writeGrant" not in document


def test_capture_binding_changed_during_source_read_rejects_late_document(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    original_read = console._read_file_bytes

    def read_then_change_scope(path, *args, **kwargs):
        result = original_read(path, *args, **kwargs)
        if console._same_path(path, str(fixture.source)):
            fixture.write_live(captureBinding=dict(
                fixture.capture_binding, captureSessionId="9" * 32))
        return result

    with mock.patch.object(console, "_read_file_bytes", side_effect=read_then_change_scope):
        with pytest.raises(console.ShaderConsoleError, match="capture session"):
            _read_document(fixture, fixture.capture_binding)


def _combine_registry(first, second, *, second_rid=None):
    registry = json.loads(first.registry.read_bytes())
    entry = json.loads(second.registry.read_bytes())["entries"][0]
    entry["authorizedAt"] = "2026-09-06T00:00:00Z"
    if second_rid is not None:
        identity = entry["workspaceIdentity"]
        identity["resourceId"] = second_rid
        identity["shaderKey"] = "fragment:%s:MainPS" % second_rid
        entry["workspaceIdentitySha256"] = console._canonical_sha256(identity)
        manifest_path = second.workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["shader"]["resourceId"] = second_rid
        second._write(manifest_path, manifest)
    registry["entries"].append(entry)
    first._write(first.registry, registry)


def test_same_shader_current_running_source_beats_newer_stopped_history(tmp_path):
    first = ShaderConsoleFixture(tmp_path / "first")
    second = OtherShaderFixture(tmp_path / "second")
    first.write_live()
    first.write_watch()
    _combine_registry(first, second)
    snapshot = console.load_console_snapshot(str(first.registry), first.capture_binding)
    workspace, = snapshot["workspaces"]
    assert workspace["workspaceId"] == first.WORKSPACE_ID
    assert workspace["autoApplyReady"] is True
    assert second.source.is_file()


def test_same_shader_latest_explicit_authorization_wins_when_both_running(tmp_path):
    first = ShaderConsoleFixture(tmp_path / "first")
    second = OtherShaderFixture(tmp_path / "second")
    for fixture in (first, second):
        fixture.write_live()
        fixture.write_watch()
    _combine_registry(first, second)
    snapshot = console.load_console_snapshot(str(first.registry), first.capture_binding)
    workspace, = snapshot["workspaces"]
    assert workspace["workspaceId"] == second.WORKSPACE_ID
    assert first.source.is_file()


def test_identical_shader_bytes_at_different_resource_ids_remain_distinct(tmp_path):
    first = ShaderConsoleFixture(tmp_path / "first")
    second = OtherShaderFixture(tmp_path / "second")
    _combine_registry(first, second, second_rid=102)
    snapshot = console.load_console_snapshot(str(first.registry), first.capture_binding)
    assert {item["resourceId"] for item in snapshot["workspaces"]} == {101, 102}


def test_current_shader_in_full_and_bounded_catalogs_has_one_editor(tmp_path):
    class BoundedCatalogShader(OtherShaderFixture):
        CAPTURE_ID = "f" * 64

    first = ShaderConsoleFixture(tmp_path / "full")
    second = BoundedCatalogShader(tmp_path / "bounded")
    for fixture in (first, second):
        fixture.write_live()
        fixture.write_watch()
    _combine_registry(first, second)
    snapshot = console.load_console_snapshot(str(first.registry), first.capture_binding)
    workspace, = snapshot["workspaces"]
    assert workspace["workspaceId"] == second.WORKSPACE_ID
    assert workspace["captureId"] == second.CAPTURE_ID
    assert first.source.is_file() and second.source.is_file()


def _bridge_scope_functions():
    tree = ast.parse(BRIDGE_PATH.read_text(encoding="utf-8"))
    names = {"_shader_workspace_capture_binding", "_notify_shader_capture_changed"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    lifecycle = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef) and node.name == "CaptureLifecycleViewer")
    nodes.extend(copy.deepcopy(node) for node in lifecycle.body
                 if isinstance(node, ast.FunctionDef) and node.name in {
                     "OnCaptureLoaded", "OnCaptureClosed"})
    namespace = {"_shader_console_protocol": console}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(BRIDGE_PATH), "exec"), namespace)
    return namespace


def test_bridge_capture_provider_and_lifecycle_notify_loaded_scope_then_none(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    binding = fixture.capture_binding
    loaded = [True]
    events = []
    namespace = _bridge_scope_functions()
    namespace.update({
        "_ctx": SimpleNamespace(IsCaptureLoaded=lambda: loaded[0],
                                GetCaptureFilename=lambda: binding["capturePath"]),
        "INSTANCE_ID": binding["instanceId"],
        "_capture_session_id": binding["captureSessionId"],
        "_active_capture_token": "old-token",
        "_shader_workspace_ui": SimpleNamespace(capture_changed=lambda value: events.append(("workspace", value))),
        "_shader_console_ui": SimpleNamespace(capture_changed=lambda value: events.append(("console", value))),
        "_material_panel_ui": None, "_material_panel_controller": None, "_pass_panel_ui": None,
        "_abandon_project_launcher_restore": lambda: None,
        "_reset_symbol_state": lambda **kwargs: namespace.update(_capture_session_id="9" * 32),
        "_current_capture_token": lambda: "new-token",
        "_publish_instance_manifest": lambda **kwargs: events.append(("published", namespace["_active_capture_token"])),
        "_publish_console_event": lambda *args, **kwargs: None,
        "_cancel_project_auto_activation": lambda: None,
        "_schedule_project_auto_activation": lambda: events.append(("scheduled", namespace["_shader_workspace_capture_binding"]())),
    })
    assert namespace["_shader_workspace_capture_binding"]() == binding
    fake_self = SimpleNamespace(_remember_current_events=lambda: None)
    namespace["OnCaptureLoaded"](fake_self)
    expected = dict(binding, captureSessionId="9" * 32)
    assert events == [("published", "new-token"), ("workspace", expected), ("console", expected), ("scheduled", expected)]
    # Qt may still report IsCaptureLoaded during the close callback. The active
    # token is invalidated before notification, so neither provider can reopen it.
    namespace["OnCaptureClosed"](fake_self)
    assert events[-3:] == [("published", None), ("workspace", None), ("console", None)]
    assert namespace["_shader_workspace_capture_binding"]() is None
    namespace["_active_capture_token"] = "unused"
    loaded[0] = False
    assert namespace["_shader_workspace_capture_binding"]() is None


def test_bridge_registers_both_panels_with_live_capture_provider():
    tree = ast.parse(BRIDGE_PATH.read_text(encoding="utf-8"))
    for name in ("_register_shader_workspace_ui", "_register_shader_console_ui"):
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        assert any(isinstance(node, ast.keyword) and node.arg == "capture_binding_provider" and
                   isinstance(node.value, ast.Name) and node.value.id == "_shader_workspace_capture_binding"
                   for node in ast.walk(function))


@pytest.mark.parametrize("change_during_read", [False, True])
def test_console_source_dialog_never_opens_in_a_changed_capture(tmp_path, change_during_read):
    fixture = ShaderConsoleFixture(tmp_path)
    binding = [fixture.capture_binding if change_during_read else None]
    calls = []
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "_open_location")

    def read_dialog(*args):
        calls.append("read")
        binding[0] = None
        return SimpleNamespace(close=lambda: calls.append("close"),
                               deleteLater=lambda: calls.append("delete"))

    namespace = dict(vars(console), SourceLocationDialog=read_dialog)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                 str(MODULE_PATH), "exec"), namespace)
    workspace, = console.load_console_snapshot(
        str(fixture.registry), fixture.capture_binding)["workspaces"]
    fake_panel = SimpleNamespace(
        _workspace_for_entry=lambda entry: workspace,
        _current_capture_binding=lambda: binding[0], top=None)
    namespace["_open_location"](fake_panel, {"sourceFile": str(fixture.source), "line": 1})
    assert calls == (["read", "close", "delete"] if change_during_read else [])
