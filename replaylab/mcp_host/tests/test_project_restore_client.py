import ast
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "renderdoc_mcp_bridge"
    / "project_restore_client.py"
)
SPEC = importlib.util.spec_from_file_location(
    "replaylab_project_restore_client_test", MODULE_PATH
)
client_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(client_module)


def _job(status, progress=None, **extra):
    value = {
        "schema": client_module.RESTORE_JOB_SCHEMA,
        "schemaVersion": 1,
        "jobId": "a" * 32,
        "status": status,
        "latestSequence": max(
            [item["sequence"] for item in (progress or [])] or [0]
        ),
        "progress": list(progress or []),
        "progressTruncated": False,
    }
    value.update(extra)
    return value


def _restore_result(project="D:/project", capture="D:/capture.rdc",
                    session="f" * 32, completed=True, **extra):
    value = {
        "schema": "replaylab-project-restore/v1",
        "schemaVersion": 1,
        "operationId": "1" * 32,
        "completed": completed,
        "capture": {
            "path": os.path.abspath(capture),
            "sessionId": session,
            "captureId": "2" * 64,
        },
        "project": {
            "root": os.path.abspath(project),
            "workspaceId": "3" * 32,
            "replayProjectId": "4" * 32,
        },
        "cache": {"hit": False, "reason": "index-absent"},
        "shaders": {
            "attempted": 0, "applied": 0, "skipped": 0, "failed": 0,
            "items": [],
        },
        "materials": {
            "attempted": 0, "restored": 0, "skipped": 0, "failed": 0,
            "sessionIds": [], "items": [],
        },
        "index": {
            "read": True, "present": False, "written": False,
            "warning": None,
        },
        "warnings": [],
    }
    value.update(extra)
    return value


class _Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, port, token, method, path, body, timeout,
                 cancel_event, generation):
        self.calls.append((
            port, token, method, path, body, timeout, cancel_event, generation))
        status, value = self.replies.pop(0)
        payload = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, separators=(",", ":")).encode("utf-8")
        )
        return status, payload


class _HealthTransport:
    def __init__(self, pid=None, version=client_module.EXPECTED_HOST_VERSION,
                 status=200, host_instance_id="5" * 32,
                 protocol_version=(
                     client_module.INTERNAL_HOST_STATE_PROTOCOL_VERSION)):
        self.pid = os.getpid() if pid is None else pid
        self.version = version
        self.status = status
        self.host_instance_id = host_instance_id
        self.protocol_version = protocol_version
        self.calls = []

    def __call__(self, port, timeout, cancel_event, generation):
        del cancel_event, generation
        self.calls.append((port, timeout))
        return self.status, json.dumps(
            {
                "service": "ReplayLabInternal",
                "version": self.version,
                "transport": "internal-http",
                "pid": self.pid,
                "hostInstanceId": self.host_instance_id,
                "hostStateProtocolVersion": self.protocol_version,
                "capabilities": [
                    "project-service", "project-restore",
                    "shader-source-save"],
            },
            separators=(",", ":"),
        ).encode("utf-8")


class ReplayLabRestoreHostClientTests(unittest.TestCase):
    def setUp(self):
        self.restore_generation = client_module.resume_restore_generation()

    def tearDown(self):
        generation = client_module.begin_restore_shutdown()
        client_module.wait_restore_quiescent(generation, 1.0)

    def _runtime(self, root):
        runtime = Path(root) / "runtime"
        runtime.mkdir()
        token_path = runtime / "internal-host.token"
        token_path.write_text("T" * 48, encoding="ascii")
        (runtime / "internal-host.json").write_text(
            json.dumps(
                {
                    "schema": "replaylab-internal-host/v1",
                    "service": "ReplayLabInternal",
                    "version": client_module.EXPECTED_HOST_VERSION,
                    "host": "127.0.0.1",
                    "port": 49643,
                    "endpoint": "http://127.0.0.1:49643/replaylab/internal",
                    "pid": os.getpid(),
                    "heartbeatUnixMs": int(time.time() * 1000),
                    "hostInstanceId": "5" * 32,
                    "hostStateProtocolVersion":
                        client_module.INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                    "tokenFile": str(token_path),
                }
            ),
            encoding="utf-8",
        )
        return runtime

    def _client(self, runtime, replies):
        transport = _Transport(replies)
        health = _HealthTransport()
        return (
            client_module.ReplayLabRestoreHostClient(
                str(runtime), transport=transport, health_transport=health,
                startup_timeout_seconds=0.1, sleep=lambda unused: None,
            ),
            transport,
            health,
        )

    def test_module_is_python36_and_socket_free_winhttp(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertTrue(imported.isdisjoint(
            {"http", "http.client", "urllib", "urllib.request", "socket", "_socket"}
        ))
        self.assertIn("WinHttpOpen", source)
        self.assertIn('"127.0.0.1"', source)
        self.assertIn("WINHTTP_DISABLE_REDIRECTS", source)
        self.assertIn("DISABLE_AUTHENTICATION", source)
        self.assertIn("WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH", source)
        self.assertIn("WINHTTP_CANCEL_SLICE_MS", source)
        self.assertNotIn("ReplayLabProjectRestoreHTTP", source)
        self.assertNotIn("_ACTIVE_REQUEST_THREAD", source)

    def test_restore_starts_authenticated_job_and_streams_ordered_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            capture.write_bytes(b"rdc")
            project.mkdir()
            client, transport, health = self._client(
                runtime,
                [
                    (202, _job("queued")),
                    (
                        200,
                        _job(
                            "running",
                            [
                                {
                                    "sequence": 1,
                                    "phase": "index",
                                    "message": "Reading RDC index",
                                    "current": 0,
                                    "total": 0,
                                },
                                {
                                    "sequence": 2,
                                    "phase": "shader",
                                    "message": "Applying shared Shader",
                                    "current": 1,
                                    "total": 2,
                                },
                            ],
                        ),
                    ),
                    (
                        200,
                        _job(
                            "completed",
                            [
                                {
                                    "sequence": 3,
                                    "phase": "finish",
                                    "message": "Project restore completed",
                                    "current": 1,
                                    "total": 1,
                                }
                            ],
                            result=_restore_result(
                                str(project), str(capture), "b" * 32),
                        ),
                    ),
                ],
            )
            progress = []

            result = client.restore(
                str(project), str(capture), "b" * 32, progress.append
            )

            self.assertTrue(result["completed"])
            self.assertEqual([
                "client-validate",
                "host-state",
                "host-health",
                "restore-submit",
                "restore-queued",
                "index",
                "shader",
                "finish",
                "result-validate",
            ], [
                value["phase"] for value in progress
            ])
            self.assertEqual([(49643, 2.0)], health.calls)
            self.assertEqual("POST", transport.calls[0][2])
            self.assertEqual("GET", transport.calls[1][2])
            self.assertIn("after=0", transport.calls[1][3])
            self.assertIn("after=2", transport.calls[2][3])
            self.assertEqual("T" * 48, transport.calls[0][1])
            payload = json.loads(transport.calls[0][4].decode("utf-8"))
            self.assertEqual(os.path.abspath(str(project)), payload["projectRoot"])
            self.assertEqual(os.path.abspath(str(capture)), payload["capturePath"])
            self.assertEqual("b" * 32, payload["captureSessionId"])

    def test_initial_snapshot_progress_is_forwarded_without_an_extra_poll(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            capture.write_bytes(b"rdc")
            project.mkdir()
            client, transport, _health = self._client(
                runtime,
                [(202, _job(
                    "completed",
                    [{
                        "sequence": 1,
                        "phase": "complete",
                        "message": "Project restore completed",
                        "current": 1,
                        "total": 1,
                    }],
                    result=_restore_result(
                        str(project), str(capture), "4" * 32)))],
            )
            progress = []

            result = client.restore(
                str(project), str(capture), "4" * 32, progress.append)

            self.assertTrue(result["completed"])
            self.assertEqual(1, len(transport.calls))
            self.assertEqual("POST", transport.calls[0][2])
            self.assertEqual([
                "client-validate", "host-state", "host-health",
                "restore-submit", "complete", "result-validate",
            ], [item["phase"] for item in progress])

    def test_running_status_is_visible_before_host_progress_arrives(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            capture.write_bytes(b"rdc")
            project.mkdir()
            client, _transport, _health = self._client(
                runtime,
                [
                    (202, _job("queued")),
                    (200, _job("running")),
                    (200, _job(
                        "completed",
                        result=_restore_result(
                            str(project), str(capture), "6" * 32))),
                ],
            )
            progress = []

            client.restore(
                str(project), str(capture), "6" * 32, progress.append)

            self.assertIn("restore-queued", [
                item["phase"] for item in progress])
            self.assertIn("restore-running", [
                item["phase"] for item in progress])
            self.assertEqual("result-validate", progress[-1]["phase"])

    def test_failed_job_surfaces_bounded_host_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job(
                     "failed", error="ABI mismatch\ncompiler diagnostic"))],
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "ABI mismatch"
            ):
                client.restore(
                    str(Path(temporary) / "project"),
                    str(Path(temporary) / "frame.rdc"),
                    "c" * 32,
                )

    def test_complete_cache_hit_result_accepts_verified_shader_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            workspace = project / "shader-workspace"
            capture.write_bytes(b"rdc")
            workspace.mkdir(parents=True)
            result = _restore_result(
                str(project), str(capture), "8" * 32)
            result["cache"] = {
                "hit": True,
                "reason": "exact-project-content-binding",
            }
            result["shaders"] = {
                "attempted": 1,
                "applied": 1,
                "skipped": 0,
                "failed": 0,
                "items": [{
                    "status": "applied",
                    "source": "rdc-index",
                    "workspacePath": str(workspace),
                    "workspaceId": "9" * 32,
                    "resourceId": 7,
                    "stage": "fragment",
                    "replacementId": "a" * 32,
                }],
            }
            result["index"] = {
                "read": True,
                "present": True,
                "written": True,
                "warning": None,
            }
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=result))],
            )

            restored = client.restore(
                str(project), str(capture), "8" * 32)

            self.assertTrue(restored["completed"])
            self.assertEqual(1, restored["shaders"]["applied"])

    def test_completed_job_preserves_bounded_business_failure_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            failed = _restore_result(completed=False)
            failed["error"] = (
                "RuntimeError: live Shader anchor changed\n"
                "compiler line 2\t(column 4)"
            )
            failed["errorType"] = "RuntimeError"
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=failed))],
            )

            result = client.restore(
                "D:/project", "D:/capture.rdc", "f" * 32)

            self.assertFalse(result["completed"])
            self.assertIn("anchor changed", result["error"])

    def test_completed_job_preserves_catalog_action_required_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            required = _restore_result(
                completed=False,
                outcome="action-required",
                reasonId="catalog-index-required",
            )
            required["capture"]["captureId"] = None
            client, _transport, _health = self._client(
                runtime,
                [
                    (202, _job("queued")),
                    (200, _job("completed", result=required)),
                ],
            )

            result = client.restore(
                "D:/project", "D:/capture.rdc", "f" * 32
            )

            self.assertFalse(result["completed"])
            self.assertEqual(result["outcome"], "action-required")
            self.assertEqual(result["reasonId"], "catalog-index-required")
            self.assertNotIn("errorType", result)

    def test_multiline_restore_diagnostics_preserve_partial_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            workspace = project / "shader-workspace"
            capture.write_bytes(b"rdc")
            workspace.mkdir(parents=True)
            diagnostic = "compile line 1\r\ncompile line 2\t(column 7)"
            result = _restore_result(
                str(project), str(capture), "7" * 32)
            result["cache"]["reason"] = "cache miss:\n" + diagnostic
            result["shaders"] = {
                "attempted": 1,
                "applied": 0,
                "skipped": 0,
                "failed": 1,
                "items": [{
                    "status": "failed",
                    "source": "rdc-index",
                    "workspacePath": str(workspace),
                    "resourceId": 2 ** 40,
                    "stage": "fragment",
                    "error": diagnostic,
                }],
            }
            result["materials"] = {
                "attempted": 1,
                "restored": 0,
                "skipped": 0,
                "failed": 1,
                "sessionIds": [],
                "items": [{
                    "status": "failed",
                    "source": "rdc-index",
                    "knowledgeId": "6" * 32,
                    "eventId": 2 ** 40,
                    "stage": "fragment",
                    "error": diagnostic,
                }],
            }
            result["index"]["warning"] = diagnostic
            result["warnings"] = [diagnostic]
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=result))],
            )

            restored = client.restore(
                str(project), str(capture), "7" * 32)

            self.assertEqual(diagnostic, restored["shaders"]["items"][0]["error"])
            self.assertEqual(diagnostic, restored["materials"]["items"][0]["error"])
            self.assertEqual([diagnostic], restored["warnings"])

    def test_restore_diagnostic_rejects_nul(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            failed = _restore_result(completed=False)
            failed["error"] = "RuntimeError: invalid\x00diagnostic"
            failed["errorType"] = "RuntimeError"
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=failed))],
            )

            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "without a bounded diagnostic",
            ):
                client.restore(
                    "D:/project", "D:/capture.rdc", "f" * 32)

    def test_non_monotonic_progress_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, _transport, _health = self._client(
                runtime,
                [
                    (202, _job("queued")),
                    (
                        200,
                        _job(
                            "running",
                            [
                                {
                                    "sequence": 2, "phase": "one",
                                    "message": "one", "current": 0, "total": 0,
                                },
                                {
                                    "sequence": 2, "phase": "two",
                                    "message": "two", "current": 0, "total": 0,
                                },
                            ],
                        ),
                    ),
                ],
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "progress is malformed",
            ):
                client.restore(
                    str(Path(temporary) / "project"),
                    str(Path(temporary) / "frame.rdc"),
                    "d" * 32,
                )

    def test_health_identity_mismatch_never_sends_bearer(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, [(202, _job("queued"))]
            )
            health.pid = os.getpid() + 1
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "health identity is invalid",
            ):
                client.restore("D:/project", "D:/capture.rdc", "e" * 32)
            self.assertEqual([], transport.calls)

    def test_internal_project_client_allows_packaged_cold_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ReplayLabInternal.exe"
            executable.write_bytes(b"internal-host-placeholder")

            client = client_module.ReplayLabInternalProjectClient(
                str(executable), transport=_Transport([]),
                health_transport=_HealthTransport())

            self.assertEqual(30.0, client.startup_timeout_seconds)

    def test_internal_project_request_owns_the_current_lifecycle_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            executable = Path(temporary) / "ReplayLabInternal.exe"
            executable.write_bytes(b"internal-host-placeholder")
            request_id = "a" * 32
            transport = _Transport([(
                200,
                {
                    "schema": client_module.PROJECT_OPERATION_RESULT_SCHEMA,
                    "requestId": request_id,
                    "result": {
                        "schema": "replaylab-project-list/v1",
                        "projects": [],
                        "rejected": [],
                    },
                },
            )])
            client = client_module.ReplayLabInternalProjectClient(
                str(executable), runtime_directory=str(runtime),
                startup_timeout_seconds=0.1, transport=transport,
                health_transport=_HealthTransport())

            value = client.invoke(
                "list-projects", {}, request_id=request_id)

            self.assertEqual("replaylab-project-list/v1", value["schema"])
            self.assertEqual(self.restore_generation, transport.calls[0][7])
            self.assertNotIn(client, client_module._ACTIVE_CLIENTS)

    def test_internal_project_request_respects_the_shutdown_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ReplayLabInternal.exe"
            executable.write_bytes(b"internal-host-placeholder")
            client = client_module.ReplayLabInternalProjectClient(str(executable))
            client_module.begin_restore_shutdown()

            with self.assertRaisesRegex(
                    client_module.ReplayLabRestoreClientError,
                    "cancelled during shutdown"):
                client.invoke("list-projects", {})

    def test_health_protocol_mismatch_never_sends_bearer(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, [(202, _job("queued"))]
            )
            health.protocol_version = 2
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreHostIncompatible,
                "health contract is incompatible",
            ):
                client.restore("D:/project", "D:/capture.rdc", "1" * 32)
            self.assertEqual([], transport.calls)

    def test_fresh_legacy_host_fails_fast_before_health_or_bearer(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            state_path = runtime / "internal-host.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["version"] = "1.10.1"
            state.pop("hostStateProtocolVersion")
            state.pop("hostInstanceId")
            state_path.write_text(json.dumps(state), encoding="utf-8")
            transport = _Transport([(202, _job("queued"))])
            health = _HealthTransport()
            sleeps = []
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), transport=transport, health_transport=health,
                startup_timeout_seconds=5.0, sleep=sleeps.append,
            )
            progress = []

            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreHostIncompatible,
                "runtime is incompatible",
            ):
                client.restore(
                    "D:/project", "D:/capture.rdc", "2" * 32,
                    progress.append)

            self.assertEqual([], sleeps)
            self.assertEqual([], health.calls)
            self.assertEqual([], transport.calls)
            self.assertEqual(
                ["client-validate", "host-state"],
                [item["phase"] for item in progress])

    def test_redirect_or_invalid_success_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, _transport, _health = self._client(
                runtime, [(307, {"error": "redirects are forbidden"})]
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "HTTP 307"
            ):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)

            contradictory = _restore_result()
            contradictory["shaders"]["attempted"] = 1
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=contradictory))],
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"
            ):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)

            client, _transport, _health = self._client(
                runtime, [(206, _job("queued"))]
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "HTTP 206"
            ):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)

            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result={"completed": True}))],
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"
            ):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)

            invalid_failure = _restore_result(completed=False)
            client, _transport, _health = self._client(
                runtime,
                [(202, _job("queued")),
                 (200, _job("completed", result=invalid_failure))],
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "without a bounded diagnostic",
            ):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)

    def test_stale_host_state_never_reaches_health_or_bearer_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            state_path = runtime / "internal-host.json"
            for overrides in (
                {"pid": 2147483647},
                {"heartbeatUnixMs": int(time.time() * 1000) + 60000},
                {"heartbeatUnixMs": int(time.time() * 1000) - 60000},
            ):
                with self.subTest(overrides=overrides):
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state.update(overrides)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    client, transport, health = self._client(
                        runtime, [(202, _job("queued"))]
                    )
                    with self.assertRaisesRegex(
                        client_module.ReplayLabRestoreClientError,
                        "internal Host did not become ready",
                    ):
                        client.restore(
                            "D:/project", "D:/capture.rdc", "2" * 32
                        )
                    self.assertEqual([], health.calls)
                    self.assertEqual([], transport.calls)
                    state["pid"] = os.getpid()
                    state["heartbeatUnixMs"] = int(time.time() * 1000)
                    state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_bool_progress_fields_and_schema_version_are_rejected(self):
        invalid_jobs = [
            {**_job("queued"), "schemaVersion": True},
            {**_job("queued"), "latestSequence": "0"},
            {key: value for key, value in _job("queued").items()
             if key != "progressTruncated"},
            _job(
                "running",
                [{
                    "sequence": True, "phase": "bad", "message": "bad",
                    "current": 0, "total": 0,
                }],
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            for index, invalid in enumerate(invalid_jobs):
                with self.subTest(index=index):
                    replies = [(202, invalid)] if index < 3 else [
                        (202, _job("queued")), (200, invalid)]
                    client, _transport, _health = self._client(runtime, replies)
                    with self.assertRaises(client_module.ReplayLabRestoreClientError):
                        client.restore(
                            "D:/project", "D:/capture.rdc", "3" * 32
                        )

    def test_cancel_active_restores_interrupts_polling(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            started = threading.Event()
            release = threading.Event()

            class BlockingTransport:
                def __init__(self):
                    self.calls = 0

                def __call__(self, port, token, method, path, body, timeout,
                             cancel_event, generation):
                    del port, token, method, path, body, timeout
                    del cancel_event, generation
                    self.calls += 1
                    if self.calls == 1:
                        return 202, json.dumps(_job("queued")).encode("utf-8")
                    started.set()
                    release.wait(1.0)
                    return 200, json.dumps(_job("running")).encode("utf-8")

            transport = BlockingTransport()
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), transport=transport,
                health_transport=_HealthTransport(),
            )
            outcome = []

            def run_restore():
                try:
                    client.restore("D:/project", "D:/capture.rdc", "1" * 32)
                except Exception as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=run_restore)
            worker.start()
            self.assertTrue(started.wait(1.0))
            client_module.cancel_active_restores()
            release.set()
            worker.join(2.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(outcome))
            self.assertIn("cancelled", str(outcome[0]))

    def test_shutdown_fence_rejects_late_restore_before_any_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, [(202, _job("queued"))]
            )
            generation = client_module.begin_restore_shutdown()
            self.assertTrue(client_module.wait_restore_quiescent(generation, 0.0))
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "cancelled"
            ):
                client.restore("D:/project", "D:/capture.rdc", "6" * 32)
            self.assertEqual([], health.calls)
            self.assertEqual([], transport.calls)

    def test_snapshot_without_cursor_progress_is_rejected_without_poll_storm(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            stalled = _job("running")
            stalled["latestSequence"] = 1
            client, transport, _health = self._client(
                runtime, [(202, _job("queued")), (200, stalled)]
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "invalid restore progress",
            ):
                client.restore("D:/project", "D:/capture.rdc", "7" * 32)
            self.assertEqual(2, len(transport.calls))

    @unittest.skipUnless(os.name == "nt", "WinHTTP is Windows-only")
    def test_real_winhttp_slow_drip_obeys_total_deadline_without_orphan(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        stopped = threading.Event()

        def serve():
            try:
                connection, _address = listener.accept()
                with connection:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 1024\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    for unused in range(1024):
                        del unused
                        connection.sendall(b"x")
                        time.sleep(0.02)
            except OSError:
                pass
            finally:
                listener.close()
                stopped.set()

        server = threading.Thread(target=serve, name="ReplayLabSlowDripTest")
        server.daemon = True
        server.start()
        started = time.monotonic()
        with self.assertRaisesRegex(
            client_module.ReplayLabRestoreClientError, "timed out"
        ):
            client_module._winhttp_request_blocking(
                "GET", "/slow", port, "Accept: application/json\r\n", b"",
                0.2, threading.Event(), self.restore_generation)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertFalse(any(
            thread.name == "ReplayLabProjectRestoreHTTP"
            for thread in threading.enumerate()
        ))
        self.assertTrue(stopped.wait(1.0))

    def test_runtime_root_matches_configured_and_local_appdata_contracts(self):
        self.assertEqual(
            os.path.abspath("C:/custom/replaylab-runtime"),
            client_module.runtime_root(
                {"REPLAYLAB_INTERNAL_RUNTIME_DIR":
                    "C:/custom/replaylab-runtime"}
            ),
        )
        self.assertEqual(
            os.path.abspath(
                "C:/Users/test/AppData/Local/ReplayLab/internal-runtime"),
            client_module.runtime_root(
                {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
