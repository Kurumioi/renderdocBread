"""Direct-open lifecycle contracts without loading Qt, replay, or a capture."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import threading
import types
import unittest
import uuid


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"


class Signal:
    def __init__(self): self.handlers = []; self.disconnected = []
    def connect(self, callback): self.handlers.append(callback)
    def disconnect(self, callback=None):
        self.disconnected.append(callback)
        if callback is None: self.handlers = []
        else: self.handlers.remove(callback)
    def emit(self):
        for callback in list(self.handlers): callback()


class Timer:
    created = 0
    def __init__(self):
        Timer.created += 1
        self.timeout = Signal(); self.stops = 0
    def setSingleShot(self, value): pass
    def start(self, value=None): pass
    def stop(self): self.stops += 1
    def deleteLater(self): pass


class Executor:
    def __init__(self):
        self.jobs = []; self.cancelled = []; self._shutdown = False
        self._result_timer = Timer(); self.drains = 0
        self._result_timer.timeout.connect(self._drain_results)
    def _drain_results(self): self.drains += 1
    def submit(self, owner, operation, callback): self.jobs.append((owner, operation, callback))
    def cancel_owner(self, owner): self.cancelled.append(owner)


class RestoreClient:
    def __init__(self, **kwargs):
        self.cancelled = False; self.calls = []
        self.progress_callback = None; self.progress_values = []
    def cancel(self): self.cancelled = True
    def restore(self, project, path, session, progress_callback=None):
        self.calls.append((project, path, session))
        if self.cancelled: raise RuntimeError("cancelled")
        self.progress_callback = progress_callback
        for progress in self.progress_values:
            progress_callback(progress)
        return {"completed": True, "editing": {"failed": 0}}


class DirectProjectRestoreTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.abspath("current.rdc")
        self.session = "f" * 32
        self.executor = Executor(); self.messages = []; self.refreshes = []; self.clients = []
        self.executor_creations = []; self.generation_resumes = []
        self.console = []
        self.match = {"activated": True, "matchCount": 1,
                      "project": {"projectRoot": os.path.abspath("project"), "name": "Known"}}
        def client(**kwargs):
            value = RestoreClient(**kwargs); self.clients.append(value); return value
        context = types.SimpleNamespace(IsCaptureLoaded=lambda:True, IsCaptureLoading=lambda:False,
                                        GetCaptureFilename=lambda:self.path)
        protocol = types.SimpleNamespace(
            HAS_QT=True,
            AsyncProjectExecutor=lambda workers:self.executor_creations.append(workers) or self.executor,
            ReplayLabProjectClient=lambda exe: types.SimpleNamespace(match_and_activate_capture=lambda *args:self.match),
            _restore_result_error=lambda value, error:error,
            _restore_status_text=lambda value:value["phase"])
        self.ns = {"os": os, "uuid": uuid, "json": json, "re": re, "threading": threading,
            "HAS_UI": True, "_ctx": context,
            "QtCore": types.SimpleNamespace(QTimer=Timer, QCoreApplication=types.SimpleNamespace(translate=lambda c,s:s)),
            "_project_auto_activation_generation":0, "_project_auto_activation_timer":None,
            "_project_auto_restore_client":None, "_project_auto_restore_result":None, "_capture_session_id":self.session,
            "_project_auto_restore_progress_stop":None,
            "_active_capture_token":("loaded",os.path.normcase(self.path)),
            "_project_async_executor":None, "_project_launcher_protocol":protocol,
            "_project_restore_client_protocol":types.SimpleNamespace(ReplayLabRestoreHostClient=client,
                resume_restore_generation=lambda:self.generation_resumes.append(True)),
            "_consume_preselected_project_capture":lambda path:False,
            "_atomic_write_json":lambda *args:None, "PROJECT_ASSOCIATION_LEASE_FILE":str(uuid.uuid4()) + ".json",
            "_find_replaylab_internal_host":lambda:"ReplayLabInternal.exe",
            "_show_project_background_status":lambda *args:self.messages.append(args),
            "_clear_project_background_status":lambda:None,
            "_consume_project_association_lease":lambda *args:None,
            "_project_ui_format":lambda text,*args:text,
            "_dispose_project_timer":lambda timer:None,
            "_project_preselected_open_depth":0, "_project_preselected_capture_path":"",
            "_shader_workspace_ui":types.SimpleNamespace(refresh=lambda **kwargs:self.refreshes.append(kwargs))}
        tree = ast.parse(BRIDGE.read_text(encoding="utf-8"), feature_version=(3,6))
        funcs = [node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in
                 ("_schedule_project_auto_activation", "_cancel_project_auto_activation", "_ensure_project_async_executor",
                  "_project_auto_restore_diagnostic", "_attach_project_auto_restore_progress",
                  "_stop_project_auto_restore_progress")]
        lifecycle = next(node for statement in tree.body if isinstance(statement, ast.If)
                         for node in statement.body if isinstance(node, ast.ClassDef)
                         and node.name == "CaptureLifecycleViewer")
        funcs.extend(node for node in lifecycle.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("OnCaptureLoaded", "OnCaptureClosed"))
        self.load_order = []
        self.ns.update({
            "_abandon_project_launcher_restore":lambda:self.load_order.append("abandon"),
            "_material_panel_ui":types.SimpleNamespace(capture_changed=lambda:self.load_order.append("material")),
            "_material_panel_controller":None, "_pass_panel_ui":None,
            "_reset_symbol_state":lambda **kwargs:self.load_order.append("symbols"),
            "_current_capture_token":lambda:("loaded", os.path.normcase(self.path)),
            "_publish_instance_manifest":lambda **kwargs:self.load_order.append("manifest"),
            "_shader_workspace_capture_binding":lambda:{"captureSessionId":self.session},
            "_notify_shader_capture_changed":lambda binding:self.load_order.append("notify"),
            "_publish_console_event":lambda *args,**kwargs:(self.load_order.append("console"),self.console.append((args,kwargs))),
        })
        exec(compile(ast.Module(body=funcs,type_ignores=[]),str(BRIDGE),"exec"),self.ns)
        # Reuse the real launcher interpretation: catalog-index-required is an
        # expected action, so this helper intentionally returns no error text.
        launcher_tree = ast.parse(BRIDGE.with_name("project_launcher.py").read_text(encoding="utf-8"))
        helper_ns = {"QtCore":self.ns["QtCore"], "TRANSLATION_CONTEXT":"ReplayLabProjects",
                     "re": re, "MAX_RESTORE_PROGRESS_COUNT": 1000000000,
                     "ProjectLauncherError": RuntimeError}
        helpers = [node for node in launcher_tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("_restore_result_error", "_restore_action_required", "_qt_format",
                                     "_normalise_restore_progress", "_restore_progress_text", "_restore_status_text")]
        exec(compile(ast.Module(body=helpers,type_ignores=[]),str(BRIDGE),"exec"),helper_ns)
        protocol._restore_result_error = helper_ns["_restore_result_error"]
        protocol._normalise_restore_progress = helper_ns["_normalise_restore_progress"]
        self.real_restore_status_text = helper_ns["_restore_status_text"]

    def test_actual_capture_loaded_callback_reaches_restore_after_fresh_symbol_and_ui_binding(self):
        self.ns["_active_capture_token"] = None
        fake_self = types.SimpleNamespace(_remember_current_events=lambda:self.load_order.append("events"))
        self.ns["OnCaptureLoaded"](fake_self)
        self.assertEqual(self.load_order, ["events", "abandon", "material", "symbols", "manifest", "notify", "console"])
        self.assertEqual(self.executor.jobs, [])  # No Host/IPC work inside LoadCapture.
        self.assertEqual(self.clients, [])
        self.assertIsNone(self.ns["_project_async_executor"])
        self.assertEqual(self.ns["_active_capture_token"], ("loaded", os.path.normcase(self.path)))
        self.ns["_project_auto_activation_timer"].timeout.emit()
        self.assertEqual(self.executor_creations, [2])
        self.assertEqual(self.generation_resumes, [True])
        owner, operation, callback = self.executor.jobs[0]
        callback(operation(), "")
        owner, operation, callback = self.executor.jobs[1]
        callback(operation(), "")
        self.assertEqual(self.clients[0].calls, [(self.match["project"]["projectRoot"], self.path, self.session)])
        self.assertEqual(self.refreshes, [{"force": True}])
        self.assertIs(self.ns["_ensure_project_async_executor"](), self.executor)
        self.assertEqual(self.executor_creations, [2])
        self.assertEqual(self.generation_resumes, [True])

    def test_actual_capture_close_cancels_loaded_restore_and_late_result(self):
        fake_self = types.SimpleNamespace(_remember_current_events=lambda:None)
        self.ns["OnCaptureLoaded"](fake_self)
        self.ns["_project_auto_activation_timer"].timeout.emit()
        owner, operation, callback = self.executor.jobs[0]
        callback(operation(), "")
        restore_callback = self.executor.jobs[1][2]
        self.ns["OnCaptureClosed"](fake_self)
        self.assertTrue(self.clients[0].cancelled)
        self.assertIsNone(self.ns["_active_capture_token"])
        restore_callback({"completed": True}, "")
        self.assertEqual(self.refreshes, [])

    def test_capture_loaded_from_explicit_project_launcher_preserves_its_single_restore(self):
        self.ns["_consume_preselected_project_capture"] = lambda path:True
        self.ns["OnCaptureLoaded"](types.SimpleNamespace(_remember_current_events=lambda:None))
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual(self.clients, [])

    def test_delayed_load_timer_does_not_start_work_for_closed_or_loading_capture(self):
        for loading in (False, True):
            with self.subTest(loading=loading):
                self.ns["_ctx"].IsCaptureLoaded = lambda:loading
                self.ns["_ctx"].IsCaptureLoading = lambda:loading
                self.ns["OnCaptureLoaded"](types.SimpleNamespace(_remember_current_events=lambda:None))
                self.ns["_project_auto_activation_timer"].timeout.emit()
                self.assertEqual(self.executor.jobs, [])
                self.assertEqual(self.executor_creations, [])

    def match_current(self):
        self.ns["_schedule_project_auto_activation"]()
        self.ns["_project_auto_activation_timer"].timeout.emit()
        owner, operation, callback = self.executor.jobs[0]
        callback(operation(), "")

    def test_unique_direct_open_runs_existing_restore_and_refreshes_current_editor(self):
        self.match_current()
        self.assertEqual(len(self.executor.jobs), 2)
        owner, operation, callback = self.executor.jobs[1]
        callback(operation(), "")
        self.assertEqual(self.clients[0].calls, [(self.match["project"]["projectRoot"], self.path, self.session)])
        self.assertEqual(self.refreshes, [{"force":True}])
        self.assertIsNone(self.ns["_project_auto_restore_client"])

    def test_ambiguous_or_unmatched_project_never_starts_restore(self):
        self.match = {"activated": False, "matchCount":2, "project":None}
        self.match_current()
        self.assertEqual(len(self.executor.jobs),1)
        self.assertEqual(self.clients,[])

    def test_changed_capture_drops_delayed_match(self):
        self.ns["_schedule_project_auto_activation"]()
        self.ns["_project_auto_activation_timer"].timeout.emit()
        self.ns["_capture_session_id"] = "a" * 32
        owner, operation, callback = self.executor.jobs[0]
        callback(operation(), "")
        self.assertEqual(self.clients,[])

    def test_close_cancels_inflight_client_and_ignores_late_completion(self):
        self.match_current()
        owner, operation, callback = self.executor.jobs[1]
        self.ns["_cancel_project_auto_activation"]()
        self.assertTrue(self.clients[0].cancelled)
        before = list(self.messages)
        callback({"completed":True}, "")
        self.assertEqual(self.messages,before)
        self.assertEqual(self.refreshes,[])
        with self.assertRaisesRegex(RuntimeError,"cancelled"): operation()

    def test_session_change_before_worker_starts_cancels_instead_of_restoring(self):
        self.match_current()
        self.ns["_capture_session_id"] = "a" * 32
        with self.assertRaisesRegex(RuntimeError,"cancelled"): self.executor.jobs[1][1]()
        self.assertTrue(self.clients[0].cancelled)

    def test_explicit_project_open_does_not_duplicate_its_existing_restore(self):
        self.ns["_consume_preselected_project_capture"] = lambda path:True
        self.ns["_schedule_project_auto_activation"]()
        self.assertEqual(self.executor.jobs,[])

    def finish_restore(self, value, error=""):
        self.match_current()
        self.executor.jobs[1][2](value, error)
        return self.ns["_project_auto_restore_result"]

    def test_expected_catalog_action_is_visible_instead_of_reported_complete(self):
        result = self.finish_restore({"completed":False, "outcome":"action-required",
            "reasonId":"catalog-index-required", "operationId":"a"*32,
            "restoreMode":"project-target-inventory"})
        self.assertEqual(result["phase"], "index-required")
        self.assertEqual(result["reasonId"], "catalog-index-required")
        self.assertEqual(self.messages[-1], ("index-required", 10000))
        args, kwargs = self.console[-1]
        self.assertEqual(args[:2], ("project", "warning"))
        self.assertEqual(kwargs["code"], "projectAutoRestoreFinished")
        self.assertEqual(kwargs["context"]["failureCode"], "catalog-index-required")
        self.assertEqual(kwargs["context"]["captureSessionId"], self.session)
        self.assertFalse(json.loads(kwargs["context"]["diagnostics"])["completed"])

    def test_partial_counts_or_editing_failure_do_not_report_complete(self):
        cases = [
            {"shaders":{"attempted":2,"applied":1,"failed":1}},
            {"shaders":{"attempted":1,"applied":0,"skipped":1}},
            {"materials":{"attempted":1,"restored":0}},
            {"editing":{"attempted":1,"enabled":0,"failed":1,
                "items":[{"status":"failed","error":"exact scope changed"}]}},
            {"outcome":"partial"},
        ]
        for value in cases:
            with self.subTest(value=value):
                summary, detail = self.ns["_project_auto_restore_diagnostic"](
                    dict(value,completed=True), "", 1, self.session)
                self.assertEqual(summary["phase"], "failed")
                self.assertTrue(detail)
                self.assertNotEqual(detail, "complete")

    def test_success_retains_bounded_readable_observation_without_authority(self):
        result = self.finish_restore({"completed":True,"operationId":"c"*32,
            "shaders":{"attempted":1,"applied":1,"failed":0,"skipped":0,
                "items":[{"status":"applied","source":"SECRET HLSL"}]},
            "editing":{"attempted":1,"enabled":1,"failed":0},
            "authorization":{"token":"must-not-be-copied"},
            "warnings":["w"*10000]*100,
            "targetLookup":{"targetRawSha256s":["d"*64],"observedTargetRawSha256s":["d"*64],
                "unobservedTargetRawSha256s":[],"rawIdentitySearchComplete":True,"targetInventoryComplete":True}})
        encoded = json.dumps(result)
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(self.messages[-1], ("complete",5000))
        self.assertNotIn("SECRET HLSL", encoded)
        self.assertNotIn("must-not-be-copied", encoded)
        self.assertEqual(len(result["warnings"]), 4)
        self.assertEqual(result["targetLookup"]["observedTargetRawSha256sCount"], 1)
        self.assertLess(len(encoded), 5000)

    def test_failure_diagnostic_redacts_credentials_and_late_result_cannot_replace_observation(self):
        result = self.finish_restore({}, "connect Bearer example-secret token=second-secret")
        self.assertEqual(result["phase"], "failed")
        self.assertNotIn("example-secret", json.dumps(result))
        self.assertNotIn("second-secret", json.dumps(result))
        before = json.dumps(result)
        self.ns["_cancel_project_auto_activation"]()
        self.executor.jobs[1][2]({"completed":True}, "")
        self.assertEqual(json.dumps(self.ns["_project_auto_restore_result"]), before)

    def _start_progress(self):
        self.match_current()
        self.executor.jobs[1][1]()
        self.assertTrue(callable(self.clients[0].progress_callback))
        return self.clients[0].progress_callback, self.executor._result_timer.timeout.handlers[-1]

    def test_worker_progress_is_coalesced_and_only_ui_drain_uses_real_translation_mapping(self):
        self.match_current()
        client = self.clients[0]
        client.progress_values = [
            {"phase":"resource-inventory", "current":1, "total":9, "message":"RAW HOST FIRST"},
            {"phase":"shader-compile", "current":2, "total":3, "message":"RAW HOST SECOND"},
            {"phase":"shader-apply", "current":99, "total":3, "item":"MainPS",
             "message":"RAW HOST FINAL", "opaque":object()},
        ]
        ui_thread = threading.get_ident()
        translated = []

        def ui_only(value):
            self.assertEqual(threading.get_ident(), ui_thread)
            return value

        def translate(context, text):
            ui_only(None)
            translated.append((context, text))
            return "translated<" + text + ">"

        self.ns["_ctx"].IsCaptureLoaded = lambda:ui_only(True)
        self.ns["_ctx"].IsCaptureLoading = lambda:ui_only(False)
        self.ns["_ctx"].GetCaptureFilename = lambda:ui_only(self.path)
        self.ns["QtCore"].QCoreApplication.translate = translate
        formatted = []

        def status_text(value):
            ui_only(None)
            formatted.append(copy.deepcopy(value))
            return self.real_restore_status_text(value)

        self.ns["_project_launcher_protocol"]._restore_status_text = status_text
        before = list(self.messages)
        timers = Timer.created
        errors = []

        def worker():
            try: self.executor.jobs[1][1]()
            except BaseException as error: errors.append(error)

        thread = threading.Thread(target=worker)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.messages, before)
        self.assertEqual(translated, [])
        self.assertEqual(client.calls, [(self.match["project"]["projectRoot"], self.path, self.session)])
        # The slot owns a normalized copy, not a reference to the producer's object.
        client.progress_values[-1]["phase"] = "mutated-after-publish"
        self.executor._result_timer.timeout.emit()
        self.assertEqual(len(self.messages), len(before) + 1)
        self.assertEqual(formatted, [{"phase":"shader-apply", "current":3, "total":3,
                                     "item":"MainPS", "message":""}])
        message = self.messages[-1][0]
        self.assertIn("Validating ABI and applying Shaders", message)
        self.assertIn("3 / 3", message)
        self.assertIn("MainPS", message)
        self.assertNotIn("RAW HOST", message)
        self.assertIn(("ReplayLabProjects", "Validating ABI and applying Shaders"), translated)
        self.executor._result_timer.timeout.emit()
        self.assertEqual(len(self.messages), len(before) + 1)
        self.assertEqual(Timer.created, timers)

    def test_cancel_detaches_only_own_progress_hook_and_rejects_late_publish_and_queued_drain(self):
        publish, queued_drain = self._start_progress()
        self.assertTrue(publish({"phase":"shader-compile"}))
        self.ns["_cancel_project_auto_activation"]()
        before = list(self.messages)
        self.assertFalse(publish({"phase":"shader-apply"}))
        queued_drain()
        self.executor._result_timer.timeout.emit()
        self.assertEqual(self.messages, before)
        self.assertIsNone(self.ns["_project_auto_restore_progress_stop"])
        self.assertEqual(self.executor._result_timer.timeout.handlers, [self.executor._drain_results])
        self.assertNotIn(None, self.executor._result_timer.timeout.disconnected)
        self.assertEqual(self.executor._result_timer.stops, 0)

    def test_terminal_result_clears_pending_progress_before_late_worker_or_ui_delivery(self):
        publish, queued_drain = self._start_progress()
        publish({"phase":"shader-compile"})
        self.executor.jobs[1][2]({"completed":True}, "")
        before = list(self.messages)
        self.assertEqual(before[-1], ("complete", 5000))
        self.assertFalse(publish({"phase":"shader-apply"}))
        queued_drain()
        self.executor._result_timer.timeout.emit()
        self.assertEqual(self.messages, before)
        self.assertIsNone(self.ns["_project_auto_restore_progress_stop"])
        self.assertEqual(self.executor._result_timer.timeout.handlers, [self.executor._drain_results])
        self.assertEqual(self.executor._result_timer.stops, 0)

    def test_old_client_progress_and_queued_completion_cannot_stop_or_overwrite_new_client(self):
        old_publish, old_drain = self._start_progress()
        old_stop = self.ns["_project_auto_restore_progress_stop"]
        next_client = RestoreClient()
        self.ns["_project_auto_restore_client"] = next_client
        new_publish = self.ns["_attach_project_auto_restore_progress"](
            self.executor, next_client, self.ns["_project_auto_activation_generation"],
            self.session, self.path)
        new_stop = self.ns["_project_auto_restore_progress_stop"]
        self.assertFalse(old_publish({"phase":"shader-apply"}))
        self.assertTrue(new_publish({"phase":"shader-compile"}))
        old_stop(); old_drain()
        self.executor.jobs[1][2]({"completed":True}, "")
        self.assertIs(self.ns["_project_auto_restore_progress_stop"], new_stop)
        before = list(self.messages)
        self.executor._result_timer.timeout.emit()
        self.assertEqual(self.messages, before + [("shader-compile",)])
        self.assertIs(self.ns["_project_auto_restore_client"], next_client)
        self.assertEqual(len(self.executor._result_timer.timeout.handlers), 2)

    def test_progress_drain_rejects_changed_scope_loading_path_or_shutdown_and_removes_hook(self):
        self.match_current()
        generation = self.ns["_project_auto_activation_generation"]
        original_context = self.ns["_ctx"]
        protocol = self.ns["_project_restore_client_protocol"]
        for mutation in ("generation", "session", "token", "executor", "shutdown", "closing",
                         "context-missing", "not-loaded", "loading", "path"):
            with self.subTest(mutation=mutation):
                publish = self.ns["_attach_project_auto_restore_progress"](
                    self.executor, self.clients[0], generation, self.session, self.path)
                drain = self.executor._result_timer.timeout.handlers[-1]
                self.assertTrue(publish({"phase":"shader-compile"}))
                before = list(self.messages)
                if mutation == "generation": self.ns["_project_auto_activation_generation"] += 1
                elif mutation == "session": self.ns["_capture_session_id"] = "a" * 32
                elif mutation == "token": self.ns["_active_capture_token"] = None
                elif mutation == "executor": self.ns["_project_async_executor"] = Executor()
                elif mutation == "shutdown": self.executor._shutdown = True
                elif mutation == "closing": protocol._RESTORE_STOPPING = True
                elif mutation == "context-missing": self.ns["_ctx"] = None
                elif mutation == "not-loaded": original_context.IsCaptureLoaded = lambda:False
                elif mutation == "loading": original_context.IsCaptureLoading = lambda:True
                elif mutation == "path": original_context.GetCaptureFilename = lambda:os.path.abspath("other.rdc")
                try:
                    drain()
                    self.assertFalse(publish({"phase":"shader-apply"}))
                    self.assertEqual(self.messages, before)
                    self.assertIsNone(self.ns["_project_auto_restore_progress_stop"])
                    self.assertEqual(self.executor._result_timer.timeout.handlers, [self.executor._drain_results])
                finally:
                    self.ns.update(_project_auto_activation_generation=generation,
                        _capture_session_id=self.session, _active_capture_token=("loaded", os.path.normcase(self.path)),
                        _project_async_executor=self.executor, _ctx=original_context)
                    self.executor._shutdown = False
                    protocol._RESTORE_STOPPING = False
                    original_context.IsCaptureLoaded = lambda:True
                    original_context.IsCaptureLoading = lambda:False
                    original_context.GetCaptureFilename = lambda:self.path

    def test_malformed_progress_does_not_replace_the_latest_valid_slot(self):
        publish, _drain = self._start_progress()
        self.assertTrue(publish({"phase":"shader-compile", "current":1, "total":2}))
        for value in (None, {"phase":"shader-apply", "current":True},
                      {"phase":"shader-apply", "item":object()}, {"phase":"bad\x00phase"}):
            with self.subTest(value=value): self.assertFalse(publish(value))
        before = list(self.messages)
        self.executor._result_timer.timeout.emit()
        self.assertEqual(self.messages, before + [("shader-compile",)])
