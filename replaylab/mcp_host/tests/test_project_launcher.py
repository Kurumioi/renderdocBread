import ast
import importlib.util
import tempfile
import threading
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_launcher.py"
SPEC = importlib.util.spec_from_file_location("replaylab_project_launcher_test", MODULE_PATH)
project_launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(project_launcher)


def _class_method_source(class_name, method_name):
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method_node = next(
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    return ast.get_source_segment(source, method_node)


def _project(active=True):
    return {
        "schema": "replaylab-project-workspace/v1",
        "schemaVersion": 1,
        "workspaceId": "1" * 32,
        "name": "Example",
        "state": "bound",
        "projectRoot": str(Path("C:/ReplayLab/projects/example")),
        "scope": {
            "gameId": "game",
            "buildId": "build",
            "platform": "windows-x64",
            "apiFamily": "d3d11",
        },
        "assetRoot": None,
        "active": active,
        "captureLibrary": {
            "schema": "replaylab-project-capture-library/v1",
            "schemaVersion": 1,
            "workspaceId": "1" * 32,
            "count": 1,
            "captures": [
                {
                    "captureRefId": "2" * 64,
                    "path": str(Path("C:/captures/frame.rdc")),
                    "displayName": "frame.rdc",
                    "available": True,
                    "sizeBytes": 123,
                    "modifiedTimeNs": 456,
                    "thumbnail": {"state": "placeholder"},
                    "contentIdentity": {
                        "schema": "replaylab-rdc-content-identity/v1",
                        "algorithm": "sha256-full",
                        "sha256": "4" * 64,
                        "sizeBytes": 123,
                        "modifiedTimeNs": 456,
                        "fileIdentity": "1:2",
                    },
                }
            ],
        },
    }


class ReplayLabProjectLauncherTests(unittest.TestCase):
    def test_main_window_moves_destructive_actions_to_safe_context_menus(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        new_project_init = _class_method_source("NewProjectDialog", "__init__")
        project_menu = _class_method_source(
            "ReplayLabProjectLauncher", "_show_project_context_menu")
        capture_menu = _class_method_source(
            "ReplayLabProjectLauncher", "_show_capture_context_menu")

        self.assertNotIn('QPushButton("Remove Project")', launcher_init)
        self.assertNotIn('QPushButton("Refresh")', launcher_init)
        self.assertNotIn('QPushButton("Remove from Project")', launcher_init)
        self.assertIn('"Remove Selected"', new_project_init)
        self.assertIn("QCoreApplication.translate", new_project_init)

        self.assertIn("self._show_project_context_menu", launcher_init)
        self.assertIn("self._show_capture_context_menu", launcher_init)
        self.assertIn("self.remove_project_shortcut", launcher_init)
        self.assertIn("self.remove_capture_shortcut", launcher_init)
        self.assertEqual(2, launcher_init.count("QtGui.QKeySequence.Delete"))
        self.assertGreaterEqual(
            launcher_init.count("QtCore.Qt.WidgetWithChildrenShortcut"), 2)

        self.assertIn('"Refresh Projects"', project_menu)
        self.assertIn('"Remove Project"', project_menu)
        self.assertIn("QCoreApplication.translate", project_menu)
        self.assertIn("self.project_list.itemAt(position)", project_menu)
        self.assertIn("item is not None", project_menu)
        self.assertIn("self.refresh()", project_menu)
        self.assertIn("self._remove_selected_project()", project_menu)

        self.assertIn('"Open Selected Frame"', capture_menu)
        self.assertIn('"Remove from Project"', capture_menu)
        self.assertIn("QCoreApplication.translate", capture_menu)
        self.assertIn("self.capture_list.itemAt(position)", capture_menu)
        self.assertIn("self.capture_list.clearSelection()", capture_menu)
        self.assertIn("QtCore.QItemSelectionModel.NoUpdate", capture_menu)
        self.assertIn("self.capture_list.selectedItems()", capture_menu)
        self.assertIn("item is not None", capture_menu)
        self.assertIn("self._remove_selected_captures()", capture_menu)

    def test_open_progress_hides_launcher_for_native_load_and_recovers_on_error(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        set_busy = _class_method_source(
            "ReplayLabProjectLauncher", "_set_busy")
        open_capture = _class_method_source(
            "ReplayLabProjectLauncher", "_open_capture_path")

        self.assertIn("QtWidgets.QProgressBar()", launcher_init)
        self.assertIn("self.operation_progress.setRange(0, 0)", launcher_init)
        self.assertIn("self.operation_progress.setVisible(self._busy)", set_busy)
        self.assertLess(
            open_capture.index("self.hide()"),
            open_capture.index("self.open_capture(path)"))
        self.assertLess(
            open_capture.index("self.open_capture(path)"),
            open_capture.index("self.close()"))
        self.assertIn("self.show()", open_capture)
        self.assertIn("self.raise_()", open_capture)
        self.assertIn("self.activateWindow()", open_capture)
        self.assertIn("self._set_busy(False)", open_capture)

    def test_post_load_restore_progress_is_separate_bounded_and_ui_polled(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        open_capture = _class_method_source(
            "ReplayLabProjectLauncher", "_open_capture_path")
        start_restore = _class_method_source(
            "ReplayLabProjectLauncher", "_start_capture_restore")
        restore_finished = _class_method_source(
            "ReplayLabProjectLauncher", "_capture_restore_finished")
        dialog_init = _class_method_source(
            "ReplayLabRestoreProgressDialog", "__init__")
        dialog_start = _class_method_source(
            "ReplayLabRestoreProgressDialog", "start")
        publish_progress = _class_method_source(
            "ReplayLabRestoreProgressDialog", "publish_progress")
        drain_progress = _class_method_source(
            "ReplayLabRestoreProgressDialog", "_drain_progress")
        abandon_restore = _class_method_source(
            "ReplayLabRestoreProgressDialog", "abandon")
        launcher_finished = _class_method_source(
            "ReplayLabProjectLauncher", "_dialog_finished")

        self.assertIn("restore_capture=None", launcher_init)
        self.assertIn("self.restore_capture = restore_capture", launcher_init)
        self.assertLess(
            open_capture.index("self.open_capture(path)"),
            open_capture.index("self._start_capture_restore(project_root, path)"))
        self.assertIn("if self.restore_capture is None", open_capture)
        self.assertIn("open_error = _capture_open_error(opened)", open_capture)
        self.assertIn("ReplayLabRestoreProgressDialog(", start_restore)
        self.assertIn("dialog.start()", start_restore)

        self.assertLess(
            dialog_init.index("root.addWidget(self.progress_bar)"),
            dialog_init.index("root.addWidget(self.status_label)"))
        self.assertIn("self._progress_timer.setInterval(25)", dialog_init)
        self.assertIn("queue.Queue(MAX_RESTORE_PROGRESS_EVENTS)", dialog_init)
        self.assertIn("self.executor.submit(", dialog_start)
        self.assertIn("self.publish_progress", dialog_start)
        self.assertIn("self._progress_events.put_nowait(progress)", publish_progress)
        self.assertIn("self._progress_events.get_nowait()", drain_progress)
        self.assertIn("latest[\"current\"]", drain_progress)
        self.assertIn("_restore_progress_text(latest)", drain_progress)
        self.assertIn("self._progress_timer.stop()", abandon_restore)
        self.assertIn("self.deleteLater()", abandon_restore)
        self.assertIn("restore_dialog.abandon()", launcher_finished)

        self.assertIn("QtWidgets.QMessageBox.warning(", restore_finished)
        self.assertIn("The RDC opened successfully", restore_finished)
        self.assertIn("You can continue using the opened capture", restore_finished)
        self.assertIn("self.close()", restore_finished)

    def test_restore_progress_contract_is_bounded_and_formats_counts(self):
        value = project_launcher._normalise_restore_progress({
            "phase": "hash",
            "message": "Checking shared Shader hashes",
            "current": 7,
            "total": 12,
        })
        self.assertEqual("hash", value["phase"])
        self.assertEqual(7, value["current"])
        self.assertEqual(12, value["total"])
        self.assertEqual(
            "Checking shared Shader hashes (7 / 12)",
            project_launcher._restore_progress_text(value))

        clamped = project_launcher._normalise_restore_progress({
            "phase": "apply", "message": "Applying", "current": 20, "total": 3,
        })
        self.assertEqual(3, clamped["current"])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "must be an object"):
            project_launcher._normalise_restore_progress("not-an-object")
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "counts"):
            project_launcher._normalise_restore_progress({
                "phase": "hash", "message": "Checking", "current": -1, "total": 2,
            })

    def test_explicit_open_and_restore_failures_are_distinguished(self):
        self.assertEqual("", project_launcher._capture_open_error(None))
        self.assertEqual("", project_launcher._capture_open_error({"opened": "frame.rdc"}))
        self.assertIn("not opened", project_launcher._capture_open_error(False))
        self.assertEqual(
            "native load failed",
            project_launcher._capture_open_error({"error": "native load failed"}))
        self.assertEqual(
            "RenderDoc did not finish opening the selected RDC",
            project_launcher._capture_open_error({
                "errorId": "renderdoc-open-incomplete",
            }))
        self.assertEqual(
            "future-open-failure: native diagnostic",
            project_launcher._capture_open_error({
                "errorId": "future-open-failure",
                "error": {"message": "native diagnostic"},
            }))

        self.assertEqual("", project_launcher._restore_result_error({"restored": True}))
        self.assertEqual(
            "restore failed",
            project_launcher._restore_result_error(None, "restore failed"))
        self.assertEqual(
            "hash mismatch",
            project_launcher._restore_result_error({
                "error": {"message": "hash mismatch"},
            }))
        self.assertEqual(
            "The selected RDC is no longer the active RenderDoc capture",
            project_launcher._restore_result_error({
                "errorId": "capture-no-longer-active",
            }))
        self.assertEqual(
            "The active capture changed while ReplayLab restored project data",
            project_launcher._restore_result_error({
                "errorId": "capture-changed-during-restore",
            }))
        self.assertEqual(
            "future-restore-failure",
            project_launcher._restore_result_error({
                "errorId": "future-restore-failure",
            }))
        self.assertEqual(
            "future-restore-failure: restore diagnostic",
            project_launcher._restore_result_error({
                "errorId": "future-restore-failure",
                "error": {"message": "restore diagnostic"},
            }))

    def test_qt_format_does_not_reprocess_dynamic_percent_tokens(self):
        self.assertEqual(
            "A|B%1|C%20",
            project_launcher._qt_format("%1|%2|%3", "A", "B%1", "C%20"))
        self.assertEqual(
            "%4 remains",
            project_launcher._qt_format("%4 remains", "A", "B", "C"))

    def test_thumbnail_reason_ids_localize_stable_outcomes(self):
        unavailable = project_launcher._thumbnail_result(
            "missing", "legacy English", reason_id="rdc-unavailable")
        self.assertEqual(
            "RDC file is unavailable",
            project_launcher._thumbnail_reason_text(unavailable))
        failed = project_launcher._thumbnail_result(
            "error", "legacy English", reason_id="read-failed",
            reason_detail="decoder status")
        self.assertEqual(
            "Embedded thumbnail read failed: decoder status",
            project_launcher._thumbnail_reason_text(failed))
        diagnostic = project_launcher._thumbnail_result(
            "error", "third-party diagnostic")
        self.assertEqual(
            "third-party diagnostic",
            project_launcher._thumbnail_reason_text(diagnostic))
        future = project_launcher._thumbnail_result(
            "error", reason_id="future-thumbnail-failure",
            reason_detail="decoder diagnostic")
        self.assertEqual(
            "future-thumbnail-failure: decoder diagnostic",
            project_launcher._thumbnail_reason_text(future))

    def test_capture_updates_replace_only_the_current_project_snapshot(self):
        current = project_launcher.validate_project(_project(active=True))
        updated = dict(current)
        updated["active"] = False
        updated["captures"] = []

        projects, row = project_launcher.replace_project_snapshot(
            [current], updated)

        self.assertEqual(0, row)
        self.assertTrue(projects[0]["active"])
        self.assertEqual([], projects[0]["captures"])
        self.assertEqual(1, len(current["captures"]))
        self.assertFalse(updated["active"])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "not present"):
            project_launcher.replace_project_snapshot(
                [current], {"workspaceId": "f" * 32})

        add_captures = _class_method_source(
            "ReplayLabProjectLauncher", "_add_captures")
        remove_captures = _class_method_source(
            "ReplayLabProjectLauncher", "_remove_selected_captures")
        self.assertIn("self._apply_updated_project(updated)", add_captures)
        self.assertIn("self._apply_updated_project(updated)", remove_captures)
        self.assertNotIn("self.refresh(", add_captures)
        self.assertNotIn("self.refresh(", remove_captures)

    def test_async_callback_registry_discards_results_after_owner_close(self):
        registry = project_launcher.AsyncCallbackRegistry()
        delivered = []
        first = registry.register("window-a", delivered.append, "first")
        second = registry.register("window-b", delivered.append, "second")
        self.assertEqual(2, len(registry))
        registry.cancel_owner("window-a")
        self.assertIsNone(registry.take(first))
        remaining = registry.take(second)
        self.assertEqual("window-b", remaining[0])
        remaining[1]("safe")
        self.assertEqual(["safe"], delivered)

    def test_startup_gate_skips_headless_loading_and_direct_capture_open(self):
        allowed = project_launcher.launcher_startup_allowed
        self.assertTrue(allowed([]))
        self.assertFalse(allowed(["--unittest", "[eventbrowser]"]))
        self.assertFalse(allowed(["--replayhost=build-machine"]))
        self.assertFalse(allowed(["--targetcontrol=localhost:38920"]))
        self.assertFalse(allowed(["--python=automation.py"]))
        self.assertFalse(allowed(["C:/captures/frame.rdc"]))
        self.assertFalse(allowed([], capture_loaded=True))
        self.assertFalse(allowed([], capture_loading=True))
        self.assertFalse(allowed([], ui_available=False))

    def test_capture_path_auto_association_requires_one_unique_path(self):
        first = project_launcher.validate_project(_project(active=False))
        capture_path = first["captures"][0]["path"]
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path,
                first["captures"][0]["contentIdentity"])["workspaceId"],
        )
        self.assertIsNone(
            project_launcher.unique_project_for_capture(
                [first], "C:/captures/not-registered.rdc",
                first["captures"][0]["contentIdentity"]))
        replaced = dict(first["captures"][0]["contentIdentity"])
        replaced["sha256"] = "5" * 64
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path, replaced)["workspaceId"])
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path, None)["workspaceId"])
        second_value = _project(active=False)
        second_value["workspaceId"] = "3" * 32
        second_value["captureLibrary"]["workspaceId"] = "3" * 32
        second = project_launcher.validate_project(second_value)
        self.assertIsNone(
            project_launcher.unique_project_for_capture(
                [first, second], capture_path,
                first["captures"][0]["contentIdentity"]))

    def test_client_uses_fixed_shell_free_project_commands(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            if arguments == ["project-list"]:
                return {
                    "schema": "replaylab-project-list/v1",
                    "projects": [_project()],
                }
            if arguments[0] == "project-match-capture":
                project = _project()
                return {
                    "schema": "replaylab-capture-project-match/v1",
                    "matchCount": 1,
                    "matches": [{"workspaceId": project["workspaceId"]}],
                    "activated": True,
                    "project": {
                        "workspaceId": project["workspaceId"],
                        "projectRoot": project["projectRoot"],
                        "name": project["name"],
                    },
                }
            if arguments[0] == "project-unregister":
                return {
                    "schema": "replaylab-project-unregister/v1",
                    "removed": True,
                    "projectFilesDeleted": False,
                }
            return _project()

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabMCP.exe", runner=runner)
        projects = client.list_projects()
        self.assertEqual("Example", projects[0]["name"])
        client.create_project(
            "New", "C:/ReplayLab/portable/New",
            capture_files=["C:/captures/a.rdc"])
        self.assertEqual("project-create", calls[1][0])
        self.assertIn("--project-root", calls[1])
        self.assertNotIn("--game-id", calls[1])
        self.assertIn("--capture", calls[1])
        client.register_existing_project("C:/ReplayLab/portable/Existing")
        self.assertEqual("project-register", calls[2][0])
        client.unregister_project("C:/ReplayLab/portable/Existing")
        self.assertEqual("project-unregister", calls[3][0])
        client.activate("C:/ReplayLab/projects/example")
        self.assertEqual("project-activate", calls[4][0])
        client.configure_project(
            "C:/ReplayLab/projects/example",
            game_id="game",
            build_id="build",
            platform="windows-x64",
            api_family="d3d11",
            asset_root="C:/assets",
        )
        self.assertEqual("project-configure", calls[5][0])
        self.assertIn("--asset-root", calls[5])
        client.remove_captures(
            "C:/ReplayLab/projects/example", ["2" * 64])
        self.assertEqual("project-remove-capture", calls[6][0])
        self.assertIn("--capture-ref-id", calls[6])
        match = client.match_and_activate_capture(
            "C:/captures/a.rdc",
            "C:/ReplayLab/ipc-v2/instances/one/lease.json",
            "a" * 32)
        self.assertTrue(match["activated"])
        self.assertEqual("project-match-capture", calls[7][0])
        self.assertIn("--association-lease", calls[7])

    def test_client_exposes_unavailable_registry_records_for_removal(self):
        def runner(arguments):
            self.assertEqual(["project-list"], arguments)
            return {
                "schema": "replaylab-project-list/v1",
                "projects": [],
                "rejected": [{
                    "workspaceId": "a" * 32,
                    "projectRoot": "C:/Moved/Old Project",
                    "name": "Old Project",
                    "reason": "workspace manifest is unavailable",
                }],
            }

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabMCP.exe", runner=runner)
        projects = client.list_projects()
        self.assertEqual(1, len(projects))
        self.assertTrue(projects[0]["unavailable"])
        self.assertEqual([], projects[0]["captures"])
        self.assertIn("manifest", projects[0]["unavailableReason"])

    def test_project_client_localizes_stable_unavailable_reason_id(self):
        def runner(arguments):
            self.assertEqual(["project-list"], arguments)
            return {
                "schema": "replaylab-project-list/v1",
                "projects": [],
                "rejected": [{
                    "workspaceId": "b" * 32,
                    "projectRoot": "C:/Moved/Invalid Project",
                    "name": "Invalid Project",
                    "reasonId": "project-validation-failed",
                    "reasonDetail": "workspace manifest is unavailable",
                }],
            }

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabMCP.exe", runner=runner)
        project = client.list_projects()[0]
        self.assertEqual(
            "Project files are unavailable or invalid: workspace manifest is unavailable",
            project_launcher._unavailable_project_reason_text(project))
        self.assertEqual(
            "project-validation-failed", project["unavailableReasonId"])

    def test_project_client_tracks_and_forgets_host_process(self):
        tracked = []
        forgotten = []
        original_track = project_launcher._track_host_process
        original_forget = project_launcher._forget_host_process
        original_popen = project_launcher.subprocess.Popen

        class _Process(object):
            returncode = 0

            def communicate(self, timeout=None):
                self.timeout = timeout
                return b"{}", b""

        process = _Process()
        project_launcher._track_host_process = tracked.append
        project_launcher._forget_host_process = forgotten.append
        project_launcher.subprocess.Popen = lambda *args, **kwargs: process
        try:
            client = project_launcher.ReplayLabProjectClient(
                str(Path(__file__).resolve()), timeout_seconds=7)
            self.assertEqual({}, client._run_process(["project-list"]))
        finally:
            project_launcher.subprocess.Popen = original_popen
            project_launcher._track_host_process = original_track
            project_launcher._forget_host_process = original_forget

        self.assertEqual([process], tracked)
        self.assertEqual([process], forgotten)
        self.assertEqual(7, process.timeout)

    def test_project_client_bounds_communicate_after_kill(self):
        original_popen = project_launcher.subprocess.Popen

        class _Pipe(object):
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Process(object):
            def __init__(self):
                self.returncode = None
                self.stdout = _Pipe()
                self.stderr = _Pipe()
                self.communicate_timeouts = []
                self.wait_timeouts = []
                self.killed = 0

            def communicate(self, timeout=None):
                self.communicate_timeouts.append(timeout)
                raise project_launcher.subprocess.TimeoutExpired("ReplayLabMCP", timeout)

            def kill(self):
                self.killed += 1

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                raise project_launcher.subprocess.TimeoutExpired("ReplayLabMCP", timeout)

        process = _Process()
        project_launcher.subprocess.Popen = lambda *args, **kwargs: process
        try:
            client = project_launcher.ReplayLabProjectClient(
                str(Path(__file__).resolve()), timeout_seconds=7)
            with self.assertRaisesRegex(
                    project_launcher.ProjectLauncherError,
                    "did not stop after cancellation"):
                client._run_process(["project-list"])
        finally:
            project_launcher.subprocess.Popen = original_popen

        self.assertEqual([7, 5], process.communicate_timeouts)
        self.assertEqual([1], process.wait_timeouts)
        self.assertEqual(1, process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_async_executor_shutdown_uses_only_bounded_wait(self):
        shutdown = _class_method_source("AsyncProjectExecutor", "shutdown")
        self.assertIn("waitForDone(max(1, int(timeout_ms)))", shutdown)
        self.assertIn("activeThreadCount() > 0", shutdown)
        self.assertNotIn("while self.pool.activeThreadCount()", shutdown)
        self.assertNotIn("waitForDone()", shutdown)

    def test_shutdown_cancels_only_live_project_host_children(self):
        class _Process(object):
            def __init__(self, returncode):
                self.returncode = returncode
                self.killed = 0

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed += 1
                self.returncode = -1

        live = _Process(None)
        finished = _Process(0)
        with project_launcher._ACTIVE_HOST_PROCESSES_GUARD:
            project_launcher._ACTIVE_HOST_PROCESSES.update((live, finished))
        try:
            project_launcher.cancel_active_host_processes()
        finally:
            with project_launcher._ACTIVE_HOST_PROCESSES_GUARD:
                project_launcher._ACTIVE_HOST_PROCESSES.discard(live)
                project_launcher._ACTIVE_HOST_PROCESSES.discard(finished)
        self.assertEqual(1, live.killed)
        self.assertEqual(0, finished.killed)

    def test_thumbnail_reader_uses_metadata_handle_without_opening_replay(self):
        calls = []

        class ResultCode:
            Succeeded = object()

        class FileType:
            PNG = object()

        class Thumbnail:
            data = b"png-data"
            width = 128
            height = 72

        class Capture:
            def OpenFile(self, path, filetype, progress):
                calls.append(("OpenFile", path, filetype, progress))
                return ResultCode.Succeeded

            def GetThumbnail(self, filetype, max_edge):
                calls.append(("GetThumbnail", filetype, max_edge))
                return Thumbnail()

            def Shutdown(self):
                calls.append(("Shutdown",))

            def OpenCapture(self, *unused_args):
                raise AssertionError("thumbnail reader must never create replay")

        class FakeRenderDoc:
            @staticmethod
            def OpenCaptureFile():
                calls.append(("OpenCaptureFile",))
                return Capture()

        FakeRenderDoc.ResultCode = ResultCode
        FakeRenderDoc.FileType = FileType

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frame.rdc"
            path.write_bytes(b"rdc")
            reader = project_launcher.RenderDocThumbnailReader(FakeRenderDoc, 256)
            result = reader(str(path))
        self.assertEqual("ready", result["state"])
        self.assertEqual(b"png-data", result["data"])
        self.assertEqual(
            ["OpenCaptureFile", "OpenFile", "GetThumbnail", "Shutdown"],
            [call[0] for call in calls],
        )

    def test_thumbnail_reader_rejects_cross_thread_renderdoc_use(self):
        class FakeRenderDoc:
            pass

        reader = project_launcher.RenderDocThumbnailReader(FakeRenderDoc)
        errors = []

        def invoke():
            try:
                reader("C:/capture.rdc")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join()
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], project_launcher.ThumbnailReadError)
        self.assertIn("owning UI thread", str(errors[0]))

    def test_thumbnail_cache_is_revision_keyed_bounded_and_negative_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.rdc"
            second = root / "b.rdc"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            calls = []

            def loader(path):
                calls.append(path)
                return {
                    "state": "ready", "reason": "", "data": b"1234",
                    "width": 2, "height": 2,
                }

            cache = project_launcher.ThumbnailCache(
                maximum_entries=1, maximum_bytes=8)
            one = cache.get_or_load(str(first), loader)
            hit = cache.get_or_load(str(first), loader)
            self.assertFalse(one["cacheHit"])
            self.assertTrue(hit["cacheHit"])
            self.assertEqual(1, len(calls))
            cache.get_or_load(str(second), loader)
            cache.get_or_load(str(first), loader)
            self.assertEqual(3, len(calls))

            first.write_bytes(b"changed-size")
            revised = cache.get_or_load(str(first), loader)
            self.assertFalse(revised["cacheHit"])
            self.assertEqual(4, len(calls))

            missing = cache.get_or_load(str(root / "missing.rdc"), loader)
            self.assertEqual("missing", missing["state"])
            self.assertEqual(4, len(calls))

    def test_project_scoped_create_omits_user_visible_scope_arguments(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            value = _project()
            value["state"] = "bound"
            value["scope"] = {
                "gameId": "replaylab-project", "buildId": "1" * 32,
                "platform": "", "apiFamily": ""
            }
            return value

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabMCP.exe", runner=runner)
        result = client.create_project("Portable", "C:/Projects/Portable")
        self.assertEqual("bound", result["state"])
        self.assertIn("--project-root", calls[0])
        self.assertNotIn("--game-id", calls[0])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "both be supplied"):
            client.create_project(
                "Partial", "C:/Projects/Partial", game_id="game")

    def test_project_scoped_settings_only_submit_the_asset_root(self):
        values = project_launcher._project_settings_payload(
            True,
            "replaylab-project",
            "1" * 32,
            "windows-x64",
            "d3d12",
            " C:/Assets ",
        )
        self.assertEqual({"asset_root": "C:/Assets"}, values)

        legacy = project_launcher._project_settings_payload(
            False, "game", "build", "windows-x64", "d3d12", "C:/Assets")
        self.assertEqual("game", legacy["game_id"])
        self.assertEqual("windows-x64", legacy["platform"])

    def test_invalid_host_documents_are_rejected_before_ui_use(self):
        value = _project()
        value["captureLibrary"]["workspaceId"] = "3" * 32
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "capture library"):
            project_launcher.validate_project(value)


if __name__ == "__main__":
    unittest.main()
