from __future__ import annotations

import ast
import copy
import importlib.util
import queue
import sys
import threading
import time
import unittest
import types
from pathlib import Path
from unittest import mock

import pytest


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = REPLAYLAB_ROOT.parent
INTEGRATION_ROOT = REPLAYLAB_ROOT / "renderdoc_mcp_bridge"
MODULE_PATH = INTEGRATION_ROOT / "project_shader_workspace.py"
BRIDGE_PATH = INTEGRATION_ROOT / "__init__.py"
I18N_EXTRACTOR_PATH = REPOSITORY_ROOT / "util" / "i18n" / (
    "extract_replaylab_translations.py"
)

SHADER_CONSOLE_APIS = {
    "cancellable_console_reads",
    "load_console_snapshot",
    "load_authorized_source_document",
    "normalize_capture_binding",
}


def _parse(path):
    return ast.parse(
        path.read_text(encoding="utf-8"),
        filename=str(path),
        feature_version=(3, 6),
    )


def _function(tree, name):
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one function named %s" % name)
    return matches[0]


def _method(tree, name):
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one method named %s" % name)
    return matches[0]


def _attribute_calls(node):
    return {
        item.func.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }


def _name_calls(node):
    return {
        item.func.id
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }


def _load_without_qt():
    spec = importlib.util.spec_from_file_location(
        "replaylab_project_shader_workspace_no_qt", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not create the shader-workspace module spec")
    module = importlib.util.module_from_spec(spec)
    without_qt = {
        "PySide2": None,
        "PySide2.QtCore": None,
        "PySide2.QtGui": None,
        "PySide2.QtWidgets": None,
    }
    with mock.patch.dict(sys.modules, without_qt):
        spec.loader.exec_module(module)
    return module


def _load_i18n_extractor():
    spec = importlib.util.spec_from_file_location(
        "shader_workspace_i18n_extractor", I18N_EXTRACTOR_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not create the ReplayLab i18n extractor spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShaderWorkspaceModuleContractTests(unittest.TestCase):
    def test_compiler_locations_are_bound_to_the_saved_source_revision(self):
        method = _method(_parse(MODULE_PATH), "_workspace_problems")
        namespace = {"_latest_event": _load_without_qt()._latest_event}
        tree = ast.Module(body=[method], type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), "exec"), namespace)
        problems = namespace["_workspace_problems"]
        workspace = {
            "watch": {"events": [{"type": "compileFailed", "sequence": 4,
                                   "requestedSourceSha256": "old", "message": "Old compile failed"}]},
            "entries": [{"cursorSequence": 4, "severity": "error", "line": 11, "text": "Bad old line"}],
        }
        self.assertEqual(problems(None, workspace, "new"), [])
        self.assertEqual(problems(None, workspace, "old")[0]["line"], 11)
        workspace["watch"]["events"].append({"type": "compileSucceeded", "sequence": 5})
        self.assertEqual(problems(None, workspace, "old"), [])

    def test_module_remains_python_36_parseable(self):
        _parse(MODULE_PATH)

    def test_module_imports_without_qt_and_exposes_defensive_panel(self):
        module = _load_without_qt()
        self.assertFalse(module.HAS_QT)
        self.assertTrue(hasattr(module, "ReplayLabShaderWorkspacePanel"))
        with self.assertRaisesRegex(Exception, "PySide2|Qt"):
            module.ReplayLabShaderWorkspacePanel(None, None)

    def test_module_has_no_process_execution_surface(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "QProcess"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_dock_close_and_first_tab_have_explicit_lifecycle_hooks(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("@QtCore.Slot(result=bool)\n        def checkAllowClose", source)
        self.assertIn("self._tab_changed(index)", source)
        refresh = _method(_parse(MODULE_PATH), "refresh")
        self.assertIn("watchedPaths", {node.value for node in ast.walk(refresh)
                                      if isinstance(node, ast.Constant) and isinstance(node.value, str)})
        self.assertIn("capture_changed", _attribute_calls(refresh))

    def test_tab_order_and_dirty_state_follow_the_native_editor(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("self._reordering_tabs = True", source)
        self.assertIn(
            "item for item in self._tab_workspace_ids() if item in pinned",
            source,
        )
        self.assertIn("editor.SetTextChangedCallback(", source)
        self.assertIn("editor.SetSavePoint()", source)
        self.assertIn('document.get("baselineEditorText", "")', source)
        self.assertIn('document["editor"].GetText() != baseline', source)

    def test_host_save_runs_off_ui_and_results_are_drained_on_ui_timer(self):
        tree = _parse(MODULE_PATH)
        worker = _function(tree, "_save_worker_loop")
        save_method = _method(tree, "_save_document")
        drain_method = _method(tree, "_drain_save_results")
        self.assertIn("save", _attribute_calls(worker))
        self.assertNotIn("save", _attribute_calls(save_method))
        self.assertIn("put", _attribute_calls(save_method))
        self.assertIn("get_nowait", _attribute_calls(drain_method))
        worker_names = {
            node.id for node in ast.walk(worker) if isinstance(node, ast.Name)
        }
        self.assertFalse({"QtCore", "QtGui", "QtWidgets"} & worker_names)

        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("threading.Thread(", source)
        self.assertIn("target=_save_worker_loop", source)
        self.assertIn('getattr(save_client, "request_pending", None)', source)
        self.assertIn("stop_event.wait(0.05)", source)
        self.assertIn(
            "self.save_result_timer.timeout.connect(self._drain_save_results)",
            source,
        )
        self.assertIn("if not self.save_result_timer.isActive():", source)
        self.assertIn("self.save_result_timer.stop()", source)
        self.assertIn('document.get("documentToken")', source)
        self.assertIn('pending.get("saveToken")', source)
        self.assertIn("self._stop_save_worker()", source)

    def test_save_worker_join_is_bounded_and_reports_pending_transport(self):
        module = _load_without_qt()
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.daemon = True
        worker.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                    RuntimeError, "Host request pending: yes"):
                module._join_save_worker(worker, 0.02, lambda: True)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_save_worker_join_confirms_a_finished_worker(self):
        module = _load_without_qt()
        worker = threading.Thread(target=lambda: None)
        worker.start()
        self.assertTrue(
            module._join_save_worker(worker, 0.5, lambda: False))
        self.assertFalse(worker.is_alive())

    def test_panel_shutdown_gates_ui_cleanup_on_worker_join(self):
        tree = _parse(MODULE_PATH)
        stop = _method(tree, "_stop_save_worker")
        shutdown = _method(tree, "shutdown")
        destroyed = _method(tree, "_destroyed")
        confirm_close = _method(tree, "_confirm_panel_close")
        self.assertIn("join", _attribute_calls(_function(
            tree, "_join_save_worker")))
        self.assertIn("is_alive", _attribute_calls(_function(
            tree, "_join_save_worker")))
        self.assertIn("_save_transport_pending", {
            node.attr for node in ast.walk(stop)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("stop", _attribute_calls(shutdown))
        self.assertIn("close", _attribute_calls(shutdown))
        self.assertIn("deleteLater", _attribute_calls(shutdown))
        shutdown_source = ast.get_source_segment(
            MODULE_PATH.read_text(encoding="utf-8"), shutdown)
        self.assertIsNotNone(shutdown_source)
        self.assertLess(
            shutdown_source.index("self._stop_save_worker()"),
            shutdown_source.index("widget.close()"),
        )
        self.assertIn("_stop_save_worker", _attribute_calls(confirm_close))
        self.assertIn("_shutting_down", {
            node.attr for node in ast.walk(destroyed)
            if isinstance(node, ast.Attribute)
        })

    def test_commit_unknown_hash_model_is_exact_and_preserves_editor_buffer(self):
        module = _load_without_qt()
        self.assertEqual(
            "committed", module._classify_commit_unknown("base", "sent", "sent")
        )
        self.assertEqual(
            "pending", module._classify_commit_unknown("base", "sent", "base")
        )
        self.assertEqual(
            "external", module._classify_commit_unknown("base", "sent", "other")
        )

        tree = _parse(MODULE_PATH)
        promote = _method(tree, "_promote_submitted_save")
        self.assertNotIn("setPlainText", _attribute_calls(promote))
        source = MODULE_PATH.read_text(encoding="utf-8")
        for field in (
            '"baseHash"', '"submittedHash"', '"textRevision"',
            '"submittedEditorText"',
        ):
            self.assertIn(field, source)
        self.assertIn('document["rawSha256"] = task["submittedHash"]', source)
        self.assertIn('document["baselineEditorText"] = task[', source)
        self.assertIn('document["conflict"] = True', source)
        self.assertIn("self._save_transport_pending()", source)
        self.assertIn("self._resolve_uncommitted_save(document)", source)

    def test_checkable_button_connections_select_bool_overload(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "self.files_button.clicked[bool].connect(self._toggle_file_tree)",
            source)
        self.assertIn(
            "self.flow_button.clicked[bool].connect(self._toggle_flow_graph)",
            source)

    def test_worker_classifies_uncertain_result_without_touching_qt(self):
        module = _load_without_qt()
        task = {
            "liveEditId": "a" * 32,
            "authorizationId": "b" * 32,
            "workspaceId": "c" * 32,
            "baseHash": "d" * 64,
            "sourceText": "float4 main() : SV_Target { return 0; }",
        }

        class UncertainClient:
            def save(self, *unused_arguments):
                raise module._shader_save_client_protocol.ReplayLabShaderSaveUncertain(
                    "timeout"
                )

        tasks = queue.Queue()
        results = queue.Queue()
        tasks.put(task)
        tasks.put(None)
        module._save_worker_loop(
            UncertainClient(), tasks, results, threading.Event()
        )
        outcome = results.get_nowait()
        self.assertEqual("unknown", outcome["kind"])
        self.assertIs(task, outcome["task"])

    def test_ui_reads_through_console_and_saves_only_through_host_client(self):
        tree = _parse(MODULE_PATH)

        module_aliases = set()
        direct_api_aliases = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = str(node.module or "")
            if module_name.endswith("shader_console"):
                for alias in node.names:
                    if alias.name in SHADER_CONSOLE_APIS:
                        direct_api_aliases[alias.asname or alias.name] = alias.name
            for alias in node.names:
                if alias.name == "shader_console":
                    module_aliases.add(alias.asname or alias.name)

        # Direct-file import fallback modules conventionally include
        # ``shader_console`` in their private protocol variable name.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and "shader_console" in node.id.lower():
                module_aliases.add(node.id)

        used_apis = set()
        unexpected_module_calls = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id in direct_api_aliases:
                used_apis.add(direct_api_aliases[function.id])
            elif (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in module_aliases
            ):
                if function.attr in SHADER_CONSOLE_APIS:
                    used_apis.add(function.attr)
                else:
                    unexpected_module_calls.add(function.attr)

        self.assertEqual(SHADER_CONSOLE_APIS, used_apis)
        self.assertEqual(set(), unexpected_module_calls)
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("save_client.save(", source)
        self.assertNotIn("save_authorized_source_document(", source)
        self.assertIn("There is no direct-disk fallback", source)

        # The panel receives validated projections/documents from shader_console;
        # it must not grow a second filesystem data path around those contracts.
        direct_io = {
            "open", "listdir", "scandir", "walk", "glob", "rglob",
            "read_text", "read_bytes", "write_text", "write_bytes",
            "QFile", "QSaveFile",
        }
        called = _name_calls(tree) | _attribute_calls(tree)
        self.assertEqual(set(), called & direct_io)

    def test_project_library_is_not_a_workspace_panel_source_or_navigation_path(self):
        tree = _parse(MODULE_PATH)
        source = MODULE_PATH.read_text(encoding="utf-8")
        refresh = _method(tree, "refresh")
        opener = _method(tree, "_open_workspace")
        disk_refresh = _method(tree, "_refresh_document_from_disk")
        reload_current = _method(tree, "_reload_current")
        saver = _method(tree, "_save_document")

        for method in (refresh, opener, disk_refresh, reload_current):
            self.assertNotIn("load_project_shader_library_snapshot", _attribute_calls(method))
            self.assertNotIn("load_project_shader_source_document", _attribute_calls(method))
        self.assertIn("SetReadOnly", _attribute_calls(opener))
        self.assertIn("SetReadOnly", _attribute_calls(disk_refresh))
        saver_source = ast.get_source_segment(source, saver)
        self.assertIsNotNone(saver_source)
        self.assertIn('document.get("readOnly")', saver_source)
        self.assertLess(
            saver_source.index('document.get("readOnly")'),
            saver_source.index("self._save_tasks.put(task)"),
        )
        self.assertNotIn("Project Library", {node.value for node in ast.walk(tree)
                                             if isinstance(node, ast.Constant) and isinstance(node.value, str)})
        self.assertNotIn("_project_documents", {node.attr for node in ast.walk(tree)
                                                if isinstance(node, ast.Attribute)})

    def test_file_navigator_defaults_hidden_and_flow_graph_follows_tabs(self):
        tree = _parse(MODULE_PATH)
        source = MODULE_PATH.read_text(encoding="utf-8")
        constructors = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ]
        constructor = max(constructors, key=lambda node: len(list(ast.walk(node))))
        tab_changed = _method(tree, "_tab_changed")
        graph_sync = _method(tree, "_sync_flow_graph")
        graph_navigation = _method(tree, "_flow_graph_node_activated")

        self.assertIn('state.get("filesVisible") is True', source)
        self.assertIn('state.get("flowVisible") is not False', source)
        self.assertIn(
            "self.files_button.clicked[bool].connect(self._toggle_file_tree)",
            source)
        self.assertIn(
            "self.flow_button.clicked[bool].connect(self._toggle_flow_graph)",
            source)
        self.assertIn('QtGui.QKeySequence("Ctrl+B")', source)
        constructor_source = ast.get_source_segment(source, constructor)
        self.assertIsNotNone(constructor_source)
        self.assertLess(
            constructor_source.index("header.addWidget(self.files_button)"),
            constructor_source.index("header.addWidget(title)"),
        )
        self.assertIn("ShaderFlowGraphPanel", _attribute_calls(constructor))
        self.assertIn("nodeActivated", {
            node.attr for node in ast.walk(constructor)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("_sync_flow_graph", {
            node.attr for node in ast.walk(tab_changed)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("set_graph", _attribute_calls(graph_sync))
        self.assertIn("clear_graph", _attribute_calls(graph_sync))
        self.assertIn("_anchor_graph", _attribute_calls(graph_sync))
        self.assertNotIn("stale", {keyword.arg for node in ast.walk(graph_sync)
                                  if isinstance(node, ast.Call) for keyword in node.keywords})

        navigation_calls = _attribute_calls(graph_navigation)
        self.assertIn("GoToLine", navigation_calls)
        self.assertNotIn("open", navigation_calls)
        self.assertIn("_current_capture_binding", navigation_calls)

    def test_project_module_is_discovered_and_clean_under_i18n_gate(self):
        self.assertTrue(MODULE_PATH.match("project_*.py"))
        self.assertIn(MODULE_PATH, set(INTEGRATION_ROOT.rglob("project_*.py")))

        extractor = _load_i18n_extractor()
        messages, errors = extractor.extract(MODULE_PATH)
        self.assertEqual([], errors)
        self.assertTrue(messages)
        self.assertEqual(
            {"ReplayLabProjects"},
            {context for context, unused_source in messages},
        )


class ShaderWorkspaceBridgeContractTests(unittest.TestCase):
    def test_bridge_registers_unregisters_and_shows_workspace_by_default(self):
        tree = _parse(BRIDGE_PATH)
        register = _function(tree, "_register_shader_workspace_ui")
        unregister = _function(tree, "_unregister_shader_workspace_ui")
        start = _function(tree, "start")
        stop = _function(tree, "stop")

        module_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        self.assertIn("_shader_workspace_ui", module_names)
        self.assertIn("_shader_workspace_callbacks", module_names)

        register_attributes = _attribute_calls(register)
        self.assertIn("ReplayLabShaderWorkspacePanel", register_attributes)
        self.assertIn("RegisterWindowMenu", register_attributes)
        self.assertIn("_show", _name_calls(register))

        self.assertIn("_register_shader_workspace_ui", _name_calls(start))
        self.assertIn(
            "_unregister_shader_workspace_ui",
            {
                node.id
                for node in ast.walk(stop)
                if isinstance(node, ast.Name)
            },
        )
        self.assertIn("shutdown", _attribute_calls(unregister))
        unregister_source = ast.get_source_segment(
            BRIDGE_PATH.read_text(encoding="utf-8"), unregister)
        self.assertIsNotNone(unregister_source)
        self.assertLess(
            unregister_source.index("panel.shutdown()"),
            unregister_source.index("_shader_workspace_ui = None"),
        )


class _Tabs:
    def __init__(self):
        self.ids = []
        self.selected = -1
        self.blocked = False

    def count(self):
        return len(self.ids)

    def removeTab(self, index):
        self.ids.pop(index)

    def blockSignals(self, blocked):
        self.blocked = blocked

    def setCurrentIndex(self, index):
        self.selected = index


class _DeferredExecutor:
    def __init__(self):
        self.jobs = []
        self.cancelled = set()

    def submit(self, owner, operation, callback):
        self.jobs.append((owner, operation, callback))

    def cancel_owner(self, owner):
        self.cancelled.add(owner)


def _finish_refresh(panel):
    owner, operation, callback = panel._snapshot_executor.jobs.pop(0)
    result = operation()
    if owner not in panel._snapshot_executor.cancelled:
        callback(result, "")
    return result


def _scoped_panel(tmp_path, binding):
    """Execute production panel methods with offline native-widget doubles."""
    module = _load_without_qt()
    namespace = dict(vars(module))
    namespace["QtCore"] = types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda _context, text: text))
    namespace["QtWidgets"] = types.SimpleNamespace(QMessageBox=mock.Mock())
    names = ("_current_capture_binding", "_can_resume_document", "capture_changed", "refresh", "_document_model",
             "_open_workspace", "_save_document", "_confirm_panel_close", "_snapshot_ready",
             "_apply_workspace_snapshot", "_cached_source_document", "_cancel_snapshot_refresh",
             "_filesystem_changed", "_reset_watched_paths")
    tree = _parse(MODULE_PATH)
    methods = ast.Module(body=[copy.deepcopy(_method(tree, name)) for name in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(methods), str(MODULE_PATH), "exec"), namespace)
    panel_type = type("ScopedPanelHarness", (), {name: namespace[name] for name in names})
    panel = panel_type()
    provider = {"binding": binding}
    panel._capture_binding_provider = lambda: provider["binding"]
    panel._capture_binding = panel._current_capture_binding()
    panel._alive = True
    panel._shutting_down = False
    panel._snapshot_executor = _DeferredExecutor()
    panel._owns_snapshot_executor = False
    panel._snapshot_owner = "panel-test"
    panel._snapshot_generation = 0
    panel._snapshot_running = False
    panel._snapshot_again = False
    panel._snapshot_stop = threading.Event()
    panel._source_documents = {}
    panel._source_request_states = {}
    panel._pending_open = {}
    panel._pending_reload = {}
    panel._last_disk_signature = None
    panel._tree_signature = None
    panel._flow_graph_signature = None
    panel._restored = False
    panel._snapshot = None
    panel._workspaces = {}
    panel._documents = {}
    panel._tree_items = {}
    panel._pinned = []
    panel._restore_open = []
    panel._restore_active = ""
    panel.registry_path = str(tmp_path / "registry.json")
    panel.project_selection_path = str(tmp_path / "active-project.json")
    panel.tab_bar = _Tabs()
    panel.file_tree = mock.Mock()
    panel.flow_graph = mock.Mock()
    panel.editor_stack = mock.Mock()
    panel.ctx = mock.Mock()
    panel.empty_page = object()
    panel.top = object()
    panel.status_label = mock.Mock()
    panel._save_tasks = queue.Queue()
    panel._watched_paths = [panel.registry_path, panel.project_selection_path]
    panel.change_timer = mock.Mock()
    panel._refresh_current_view = mock.Mock()
    panel._render_tree = mock.Mock()
    panel._refresh_document_from_disk = mock.Mock()
    panel._reset_watched_paths = mock.Mock()
    panel._tab_changed = mock.Mock()
    panel._tab_index = lambda key: panel.tab_bar.ids.index(key) if key in panel.tab_bar.ids else -1

    def opened(key, **_kwargs):
        panel.tab_bar.ids.append(key)
        return True

    panel._open_workspace = mock.Mock(side_effect=opened)

    def source_document(_registry, authorization_id, key, capture_binding=None):
        snapshot = getattr(module._shader_console_protocol.load_console_snapshot, "return_value", {})
        workspace = next(item for item in snapshot.get("workspaces", [])
                         if item["workspaceId"] == key)
        source = Path(workspace.setdefault("sourcePath", str(tmp_path / (key + ".hlsl"))))
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_text("float4 MainPS() { return 0; }", encoding="utf-8")
        info = source.stat()
        return {"workspaceId": key, "authorizationId": authorization_id,
                "captureBinding": capture_binding, "sourcePath": str(source),
                "text": source.read_text(encoding="utf-8"), "rawSha256": "3" * 64,
                "modifiedNs": info.st_mtime_ns, "size": info.st_size,
                "liveEditId": (workspace.get("liveEdit") or {}).get("liveEditId"),
                "watchId": (workspace.get("watch") or {}).get("watchId")}

    module._shader_console_protocol.load_authorized_source_document = source_document
    return panel, provider, module._shader_console_protocol, panel_type


def _capture_binding(tmp_path, session="1" * 32):
    return {"instanceId": "offline-gui", "captureSessionId": session,
            "capturePath": str(tmp_path / "capture.rdc")}


def test_refresh_returns_before_blocked_loader_and_only_ui_callback_publishes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    panel, unused_provider, protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    ui_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()

    def load(*unused, **unused_keywords):
        assert threading.get_ident() != ui_thread
        entered.set()
        assert release.wait(5)
        return {"workspaces": [], "watchedPaths": []}

    with mock.patch.object(protocol, "load_console_snapshot", side_effect=load) as loader:
        panel.refresh(True)
        loader.assert_not_called()
        owner, operation, callback = panel._snapshot_executor.jobs.pop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(operation)
            try:
                assert entered.wait(5)
                assert panel._snapshot is None
                panel._render_tree.assert_not_called()
            finally:
                release.set()
            result = pending.result(timeout=5)
        assert panel._snapshot is None
        callback(result, "")
    assert panel._snapshot == {"workspaces": [], "watchedPaths": []}


def test_forced_refreshes_coalesce_without_starving_current_result(tmp_path):
    panel, unused_provider, protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    with mock.patch.object(protocol, "load_console_snapshot", return_value={
            "workspaces": [], "watchedPaths": []}):
        panel.refresh(True)
        for unused in range(100):
            panel.refresh(True)
        assert len(panel._snapshot_executor.jobs) == 1
        _finish_refresh(panel)
    assert panel._snapshot is not None
    assert len(panel._snapshot_executor.jobs) == 1


@pytest.mark.parametrize("change", ["textRevision", "documentToken", "rawSha256", "saveInFlight"])
def test_late_source_cache_never_overwrites_new_edit_or_save_state(tmp_path, change):
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = {"documentToken": 1, "textRevision": 0, "rawSha256": "a" * 64}
    panel._documents["shader"] = document
    panel._workspaces["shader"] = {"authorizationId": "b" * 32, "liveEdit": {"liveEditId": "live"}}
    panel._source_documents["shader"] = {
        "captureBinding": panel._capture_binding, "authorizationId": "b" * 32, "liveEditId": "live"}
    panel._source_request_states["shader"] = (
        1, 0, "a" * 64, None, False, False, None)
    assert panel._cached_source_document("shader") is not None
    document[change] = True if change == "saveInFlight" else "changed"
    assert panel._cached_source_document("shader") is None


def _dirty_document(panel, key="old-workspace"):
    editor = mock.Mock()
    editor.GetText.return_value = "unsaved HLSL buffer"
    document = {"workspaceId": key, "editor": editor, "dirty": True,
                "available": True, "captureBinding": panel._capture_binding,
                "saveQueued": True, "documentToken": 7}
    panel._documents[key] = document
    panel._workspaces[key] = {"workspaceId": key, "autoApplyReady": True}
    panel.tab_bar.ids.append(key)
    return document


def test_no_capture_refresh_never_reads_old_registry_or_restores_project_documents(tmp_path):
    panel, _provider, protocol, _type = _scoped_panel(tmp_path, None)
    panel._pinned = ["historical-library", "historical-workspace"]
    panel._restore_open = list(panel._pinned)
    with mock.patch.object(protocol, "_load_generic_console_entries", return_value={
            "entries": [], "errors": [], "watchedPaths": []}), \
            mock.patch.object(protocol, "_read_json", side_effect=AssertionError("unscoped registry read")), \
            mock.patch.object(protocol, "load_project_shader_library_snapshot") as library:
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel._snapshot["captureBinding"] is None
    assert panel._snapshot["workspaces"] == []
    assert panel._workspaces == {}
    assert panel.tab_bar.count() == 0
    panel._open_workspace.assert_not_called()
    library.assert_not_called()


def test_capture_changed_hides_old_tabs_disables_native_navigation_and_keeps_dirty_buffer(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = None
    panel.capture_changed(None)
    assert panel.tab_bar.count() == 0 and not panel.tab_bar.blocked
    assert panel._workspaces == {} and panel._document_model(document["workspaceId"]) is None
    assert panel._documents[document["workspaceId"]] is document
    assert document["dirty"] is True and document["available"] is False
    assert document["saveQueued"] is False
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    document["editor"].SetText.assert_not_called()
    document["editor"].SetReadOnly.assert_called_with(True)
    document["editor"].SetNavigationEnabled.assert_called_with(False)
    panel.editor_stack.setCurrentWidget.assert_called_with(panel.empty_page)
    panel.flow_graph.clear_graph.assert_called_with(graph_key="shader-workspace-empty")


def test_snapshot_returning_after_capture_switch_is_not_published(tmp_path):
    first = _capture_binding(tmp_path)
    second = _capture_binding(tmp_path, "2" * 32)
    panel, provider, protocol, _type = _scoped_panel(tmp_path, first)
    document = _dirty_document(panel)

    def late_snapshot(*_args, **kwargs):
        assert kwargs["capture_binding"] == panel._capture_binding
        provider["binding"] = second
        return {"workspaces": [{"workspaceId": "old-result", "editorEligible": True}]}

    with mock.patch.object(protocol, "load_console_snapshot", side_effect=late_snapshot):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel._snapshot is None and panel._workspaces == {}
    assert panel._capture_binding == protocol.normalize_capture_binding(second)
    assert panel.tab_bar.count() == 0
    assert document["dirty"] is True and not document["available"]
    panel._render_tree.assert_not_called()
    panel._open_workspace.assert_not_called()


def test_authorized_document_returning_after_capture_close_never_creates_native_editor(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel._workspaces["current"] = {"workspaceId": "current", "authorizationId": "a" * 32}

    def late_document(*_args, **kwargs):
        assert kwargs["capture_binding"] == panel._capture_binding
        provider["binding"] = None
        return {"sourceText": "old HLSL"}

    with mock.patch.object(protocol, "load_authorized_source_document", side_effect=late_document):
        assert panel_type._open_workspace(panel, "current", quiet=True) is False
    assert panel._documents == {} and panel.tab_bar.count() == 0
    panel.ctx.CreateShaderCodeEditor.assert_not_called()


def test_restore_tabs_only_uses_current_capture_authorized_projection(tmp_path):
    panel, _provider, protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    old = _dirty_document(panel)
    panel._restore_open = ["old-workspace", "current"]
    panel._pinned = ["historical-library"]
    snapshot = {"workspaces": [
        {"workspaceId": "current", "authorizationId": "a" * 32, "editorEligible": True},
        {"workspaceId": "rejected", "editorEligible": False}], "watchedPaths": []}
    with mock.patch.object(protocol, "load_console_snapshot", return_value=snapshot) as loader:
        panel.refresh(force=True)
        _finish_refresh(panel)
    loader.assert_called_once_with(panel.registry_path, capture_binding=panel._capture_binding)
    assert set(panel._workspaces) == {"current"}
    assert panel.tab_bar.ids == ["current"]
    assert [call.args[0] for call in panel._open_workspace.call_args_list] == ["current"]
    assert old["dirty"] is True and old["available"] is False
    old["editor"].SetNavigationEnabled.assert_called_with(False)


def test_save_rejects_changed_capture_binding_even_before_the_next_ui_refresh(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = _capture_binding(tmp_path, "2" * 32)
    assert panel._save_document(document["workspaceId"]) is False
    assert panel._save_tasks.empty()
    assert document["dirty"] is True
    document["editor"].SetSavePoint.assert_not_called()


def test_closing_panel_still_prompts_for_dirty_buffers_hidden_by_capture_change(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = None
    panel.capture_changed(None)
    panel._native_editors_released = False
    panel._shutting_down = False
    panel._confirm_close_document = mock.Mock(return_value=False)
    panel._release_native_editors = mock.Mock()
    assert panel._confirm_panel_close() is False
    panel._confirm_close_document.assert_called_once_with(document)
    panel._release_native_editors.assert_not_called()


def _reopen_dirty_capture(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    document["sourcePath"] = str(tmp_path / "managed" / "source.hlsl")
    document["authorizationId"] = "old-authorization"
    panel._restore_open = [document["workspaceId"]]
    provider["binding"] = None
    panel.capture_changed(None)
    provider["binding"] = _capture_binding(tmp_path, "2" * 32)
    workspace = {"workspaceId": document["workspaceId"], "editorEligible": True,
                 "authorizationId": "fresh-authorization", "autoApplyReady": True,
                 "sourcePath": document["sourcePath"],
                 "liveEdit": {"liveEditId": "fresh-live-edit"}}

    def insert_tab(key, _document):
        panel.tab_bar.ids.append(key)
        return len(panel.tab_bar.ids) - 1

    panel._insert_tab = mock.Mock(side_effect=insert_tab)
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    return panel, provider, protocol, document, workspace


def test_same_capture_and_managed_source_resume_dirty_buffer_with_fresh_host_authorization(tmp_path):
    panel, _provider, protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    snapshot = {"workspaces": [workspace], "watchedPaths": []}
    with mock.patch.object(protocol, "load_console_snapshot", return_value=snapshot):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel.tab_bar.ids == [document["workspaceId"]]
    assert document["available"] is True and document["dirty"] is True
    assert document["captureBinding"] == panel._current_capture_binding()
    assert document["authorizationId"] == "fresh-authorization"
    document["editor"].SetNavigationEnabled.assert_called_with(True)
    document["editor"].SetText.assert_not_called()
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    panel.ctx.CreateShaderCodeEditor.assert_not_called()

    panel._document_source_text = lambda item: item["editor"].GetText()
    panel._next_save_token = 0
    panel._sync_document_dirty = mock.Mock()
    panel.save_result_timer = mock.Mock()
    panel._save_state = mock.Mock()
    assert panel._save_document(document["workspaceId"]) is True
    task = panel._save_tasks.get_nowait()
    assert task["sourceText"] == "unsaved HLSL buffer"
    assert task["authorizationId"] == "fresh-authorization"
    assert task["liveEditId"] == "fresh-live-edit"


@pytest.mark.parametrize("mismatch", ["capture-path", "source-path", "save-in-flight", "commit-unknown"])
def test_reopening_cannot_attach_dirty_buffer_to_another_source_or_pending_save(tmp_path, mismatch):
    panel, provider, protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    original_binding = copy.deepcopy(document["captureBinding"])
    if mismatch == "capture-path":
        provider["binding"]["capturePath"] = str(tmp_path / "different.rdc")
    elif mismatch == "source-path":
        workspace["sourcePath"] = str(tmp_path / "another" / "source.hlsl")
    elif mismatch == "save-in-flight":
        document["saveInFlight"] = True
    else:
        document["commitUnknown"] = True
    with mock.patch.object(protocol, "load_console_snapshot", return_value={
            "workspaces": [workspace], "watchedPaths": []}):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel.tab_bar.count() == 0
    assert panel._documents[document["workspaceId"]] is document
    assert document["available"] is False and document["dirty"] is True
    assert document["captureBinding"] == original_binding
    assert document["authorizationId"] == "old-authorization"
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    document["editor"].SetReadOnly.assert_called_with(True)
    document["editor"].SetNavigationEnabled.assert_called_with(False)
    document["editor"].SetText.assert_not_called()
    panel._insert_tab.assert_not_called()
    panel._refresh_document_from_disk.assert_not_called()
    assert panel._save_document(document["workspaceId"]) is False
    assert panel._save_tasks.empty()


if __name__ == "__main__":
    unittest.main()
