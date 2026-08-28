from __future__ import annotations

import ast
import importlib.util
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = REPLAYLAB_ROOT.parent
INTEGRATION_ROOT = REPLAYLAB_ROOT / "renderdoc_mcp_bridge"
MODULE_PATH = INTEGRATION_ROOT / "project_shader_workspace.py"
BRIDGE_PATH = INTEGRATION_ROOT / "__init__.py"
I18N_EXTRACTOR_PATH = REPOSITORY_ROOT / "util" / "i18n" / (
    "extract_replaylab_translations.py"
)

SHADER_CONSOLE_APIS = {
    "load_console_snapshot",
    "load_authorized_source_document",
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
        self.assertIn("[self.registry_path] + list(snapshot.get", source)

    def test_tab_order_and_dirty_state_follow_the_qt_widgets(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("self._reordering_tabs = True", source)
        self.assertIn(
            "item for item in self._tab_workspace_ids() if item in pinned",
            source,
        )
        self.assertIn("editor.document().modificationChanged.connect", source)
        self.assertIn("editor.textChanged.connect", source)
        self.assertIn('document.get("baselineEditorText", "")', source)
        self.assertIn('document["editor"].toPlainText() != baseline', source)

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


if __name__ == "__main__":
    unittest.main()
