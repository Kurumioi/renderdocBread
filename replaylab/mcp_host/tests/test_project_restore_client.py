import ast
import copy
import hashlib
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


def _desired_head(active_revision=7, **extra):
    semantic = {
        "knowledgeContentSha256": "5" * 64,
        "knowledgeIdsSha256": "6" * 64,
        "artifactRefsSha256": "7" * 64,
        "referencedHeadsSha256": "8" * 64,
        "complete": True,
    }
    value = {
        "activeKnowledgeRevision": active_revision,
        **semantic,
        "headSha256": hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
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
        "restoreMode": "full-inventory" if completed else "unresolved",
        "desiredHead": None,
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
        "inventoryCache": {
            "hit": False, "written": False, "reason": "not-read",
        },
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
        "realizedReceipts": {
            "loadedFromIndex": 0,
            "validatedForRouting": 0,
            "produced": 0,
            "adoptedCurrentSession": 0,
            "mutationsCreated": 0,
            "idempotentApplyResponses": 0,
            "persisted": 0,
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
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, value = reply
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
                # A newer Host advertises additional route families; clients
                # require the ones they use and ignore the rest.
                "capabilities": [
                    "project-service", "project-restore",
                    "shader-source-save", "shader-live-edit", "asset-index"],
            },
            separators=(",", ":"),
        ).encode("utf-8")


class ReplayLabRestoreHostClientTests(unittest.TestCase):
    def test_target_lookup_completion_preserves_partial_scope_and_rejects_false_coverage(self):
        value = _restore_result(restoreMode="project-target-inventory", inventoryScope="target-raw-sha256",
            fullFrame=False, targetLookup={"targetRawSha256s": ["a" * 64],
                "observedTargetRawSha256s": [], "unobservedTargetRawSha256s": ["a" * 64],
                "targetInventoryComplete": True, "rawIdentitySearchComplete": True})
        check = lambda candidate: client_module.ReplayLabRestoreHostClient._result(
            candidate, os.path.abspath("D:/project"), os.path.abspath("D:/capture.rdc"), "f" * 32)
        self.assertFalse(check(value)["fullFrame"])
        variants = []
        for field in ("targetInventoryComplete", "rawIdentitySearchComplete"):
            bad = copy.deepcopy(value); bad["targetLookup"][field] = False; variants.append(bad)
        bad = copy.deepcopy(value); bad["fullFrame"] = True; variants.append(bad)
        bad = copy.deepcopy(value); bad["targetLookup"]["unobservedTargetRawSha256s"] = []; variants.append(bad)
        bad = copy.deepcopy(value); bad["targetLookup"]["targetRawSha256s"] *= 2; variants.append(bad)
        bad = copy.deepcopy(value); bad["inventoryCache"]["written"] = True; variants.append(bad)
        bad = copy.deepcopy(value); bad["targetLookup"]["unknownAuthority"] = True; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad):
                with self.assertRaises(client_module.ReplayLabRestoreClientError): check(bad)

    def test_editor_receipt_requires_one_applied_workspace_and_exact_capture(self):
        value = _restore_result()
        workspace = os.path.abspath("D:/project/workspace")
        value["shaders"].update(attempted=1, applied=1, items=[{
            "status": "applied", "source": "reuse-plan", "workspacePath": workspace,
            "workspaceId": "9" * 32, "resourceId": 7, "stage": "fragment",
            "replacementId": "a" * 32, "knowledgeId": "b" * 32, "idempotentlyAdopted": False}])
        value["realizedReceipts"].update(produced=1, mutationsCreated=1)
        value["editing"] = {"attempted": 1, "enabled": 1, "failed": 0, "items": [{
            "workspaceId": "9" * 32, "workspacePath": workspace, "status": "enabled",
            "liveEditId": "c" * 32, "watchId": "d" * 32,
            "captureBinding": {"instanceId": "1-current", "capturePath": os.path.abspath("D:/capture.rdc"),
                               "captureSessionId": "f" * 32}}]}
        check = lambda candidate: client_module.ReplayLabRestoreHostClient._result(
            candidate, os.path.abspath("D:/project"), os.path.abspath("D:/capture.rdc"), "f" * 32)
        self.assertEqual(check(value)["editing"]["enabled"], 1)
        for mutation in ("session", "workspace", "count", "unknown", "uncommitted"):
            bad = copy.deepcopy(value)
            if mutation == "session": bad["editing"]["items"][0]["captureBinding"]["captureSessionId"] = "e" * 32
            elif mutation == "workspace": bad["editing"]["items"][0]["workspaceId"] = "e" * 32
            elif mutation == "count": bad["editing"]["enabled"] = 0
            elif mutation == "unknown": bad["editing"]["arbitraryCommand"] = "ignored"
            else: bad["completed"] = False
            with self.subTest(mutation=mutation):
                with self.assertRaises(client_module.ReplayLabRestoreClientError): check(bad)
        failed = copy.deepcopy(value)
        failed["editing"].update(enabled=0, failed=1, items=[{
            "workspaceId": "9" * 32, "workspacePath": workspace, "status": "failed", "error": "Editor unavailable"}])
        self.assertTrue(check(failed)["completed"])
        self.assertEqual(check(failed)["shaders"]["applied"], 1)

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

    def test_progress_item_is_bounded_forward_compatible_and_optional(self):
        event = client_module._normalise_progress({
            "sequence": 1,
            "phase": "export-shader-bindings",
            "message": "Exporting Shader bindings",
            "current": 7,
            "total": 12,
            "item": "EID 418",
        }, 0)
        self.assertEqual("EID 418", event["item"])
        legacy = client_module._normalise_progress({
            "sequence": 2,
            "phase": "prepare",
            "message": "",
            "current": 0,
            "total": 0,
        }, 1)
        self.assertEqual("", legacy["item"])
        with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "malformed"):
            client_module._normalise_progress({
                "sequence": 3,
                "phase": "prepare",
                "message": "",
                "current": 0,
                "total": 0,
                "item": "x" * 1025,
            }, 2)

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
            result["restoreMode"] = "exact-rdc-head"
            result["desiredHead"] = _desired_head()
            result["cache"] = {
                "hit": True,
                "reason": "exact-desired-project-head",
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
                    "idempotentlyAdopted": True,
                }],
            }
            result["index"] = {
                "read": True,
                "present": True,
                "written": True,
                "warning": None,
            }
            result["realizedReceipts"] = {
                "loadedFromIndex": 1,
                "validatedForRouting": 1,
                "produced": 1,
                "adoptedCurrentSession": 1,
                "mutationsCreated": 0,
                "idempotentApplyResponses": 0,
                "persisted": 1,
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

    def test_empty_project_fast_path_result_is_strictly_validated(self):
        result = _restore_result()
        result["restoreMode"] = "no-project-knowledge"
        result["capture"]["captureId"] = None
        result["cache"] = {
            "hit": False,
            "reason": "project-has-no-restorable-knowledge",
        }
        result["inventoryCache"] = {
            "hit": False,
            "written": False,
            "reason": "not-required-for-empty-project",
        }
        result["index"] = {
            "read": False,
            "present": False,
            "written": False,
            "warning": None,
        }

        accepted = client_module.ReplayLabRestoreHostClient._result(
            result, "D:/project", "D:/capture.rdc", "f" * 32)

        self.assertEqual("no-project-knowledge", accepted["restoreMode"])
        self.assertIsNone(accepted["capture"]["captureId"])

        invalid = json.loads(json.dumps(result))
        invalid["index"]["read"] = True
        with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"):
            client_module.ReplayLabRestoreHostClient._result(
                invalid, "D:/project", "D:/capture.rdc", "f" * 32)

        invalid = json.loads(json.dumps(result))
        invalid["capture"]["captureId"] = "2" * 64
        with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"):
            client_module.ReplayLabRestoreHostClient._result(
                invalid, "D:/project", "D:/capture.rdc", "f" * 32)

    def test_new_restore_head_and_receipt_fields_fail_closed(self):
        base = _restore_result()
        base["desiredHead"] = _desired_head(active_revision=11)
        accepted = client_module.ReplayLabRestoreHostClient._result(
            base, "D:/project", "D:/capture.rdc", "f" * 32)
        self.assertEqual(11, accepted["desiredHead"]["activeKnowledgeRevision"])

        invalid_results = []

        invalid = _restore_result()
        invalid["restoreMode"] = "background-fast-path"
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["desiredHead"] = _desired_head(headSha256="0" * 64)
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["desiredHead"] = _desired_head(active_revision=True)
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["desiredHead"] = _desired_head()
        invalid["desiredHead"]["unexpected"] = False
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["inventoryCache"] = {
            "hit": True,
            "written": False,
            "reason": "fresh-target-validation",
        }
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["realizedReceipts"]["produced"] = 1
        invalid_results.append(invalid)

        invalid = _restore_result()
        invalid["realizedReceipts"]["unexpected"] = 0
        invalid_results.append(invalid)

        for index, invalid in enumerate(invalid_results):
            with self.subTest(index=index), self.assertRaisesRegex(
                    client_module.ReplayLabRestoreClientError,
                    "invalid result"):
                client_module.ReplayLabRestoreHostClient._result(
                    invalid, "D:/project", "D:/capture.rdc", "f" * 32)

    def test_cache_hit_requires_new_semantic_head_reason_and_receipt_evidence(self):
        result = _restore_result()
        result["restoreMode"] = "exact-rdc-head"
        result["desiredHead"] = _desired_head()
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
                "workspacePath": "D:/project/shader-workspace",
                "workspaceId": "9" * 32,
                "resourceId": 7,
                "stage": "fragment",
                "replacementId": "a" * 32,
                "idempotentlyAdopted": True,
            }],
        }
        result["realizedReceipts"] = {
            "loadedFromIndex": 1,
            "validatedForRouting": 1,
            "produced": 1,
            "adoptedCurrentSession": 1,
            "mutationsCreated": 0,
            "idempotentApplyResponses": 0,
            "persisted": 0,
        }

        with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"):
            client_module.ReplayLabRestoreHostClient._result(
                result, "D:/project", "D:/capture.rdc", "f" * 32)

        result["cache"]["reason"] = "exact-desired-project-head"
        accepted = client_module.ReplayLabRestoreHostClient._result(
            result, "D:/project", "D:/capture.rdc", "f" * 32)
        self.assertTrue(accepted["cache"]["hit"])

        result["shaders"]["items"][0]["idempotentlyAdopted"] = False
        with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "invalid result"):
            client_module.ReplayLabRestoreHostClient._result(
                result, "D:/project", "D:/capture.rdc", "f" * 32)

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

            self.assertEqual(60.0, client.startup_timeout_seconds)

    def test_restore_client_allows_packaged_cold_start(self):
        client = client_module.ReplayLabRestoreHostClient(
            transport=_Transport([]), health_transport=_HealthTransport())

        self.assertEqual(60.0, client.startup_timeout_seconds)

    def test_default_restore_timeout_exceeds_host_deadline_and_cleanup(self):
        client = client_module.ReplayLabRestoreHostClient(
            transport=_Transport([]), health_transport=_HealthTransport()
        )

        self.assertEqual(
            client_module.DEFAULT_PROJECT_RESTORE_CLIENT_TIMEOUT_SECONDS,
            client.timeout_seconds,
        )
        self.assertGreater(
            client.timeout_seconds,
            client_module.PROJECT_RESTORE_DEADLINE_SECONDS
            + client_module.PROJECT_RESTORE_MAX_INFLIGHT_SECONDS
            + client_module.PROJECT_RESTORE_CLEANUP_GRACE_SECONDS,
        )

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

    def test_client_timeout_best_effort_deletes_current_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            ticks = iter([0.0, 0.0, 0.0, 0.0, 2.0])
            transport = _Transport([
                (202, _job("queued")),
                (200, _job("running")),
                (202, {"cancelled": True}),
            ])
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), timeout_seconds=1.0,
                startup_timeout_seconds=0.1,
                transport=transport, health_transport=_HealthTransport(),
                monotonic=lambda: next(ticks), sleep=lambda unused: None,
            )

            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "project restore timed out",
            ):
                client.restore("D:/project", "D:/capture.rdc", "8" * 32)

            self.assertEqual(
                ["POST", "GET", "DELETE"],
                [call[2] for call in transport.calls],
            )
            self.assertEqual(
                "%s/%s" % (client_module.RESTORE_JOB_PATH, "a" * 32),
                transport.calls[-1][3],
            )
            self.assertFalse(transport.calls[-1][6].is_set())
            self.assertIsNone(transport.calls[-1][7])

    @staticmethod
    def _native_timeout(path, method="GET", code=12002):
        original = OSError("native transport diagnostic")
        original.winerror = code
        return client_module.ReplayLabRestoreTransportError(original, method, path)

    def test_known_job_native_timeout_rechecks_same_job_and_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            query = "%s/%s?after=1" % (client_module.RESTORE_JOB_PATH, "a" * 32)
            client, transport, unused_health = self._client(runtime, [
                (202, _job("running", [{"sequence": 1, "phase": "shader-compile",
                    "message": "Compiling", "current": 1, "total": 1}])),
                self._native_timeout(query),
                (200, _job("completed", result=_restore_result(), latestSequence=1)),
            ])
            result = client.restore("D:/project", "D:/capture.rdc", "f" * 32)
            self.assertTrue(result["completed"])
            self.assertEqual(["POST", "GET", "GET"], [call[2] for call in transport.calls])
            self.assertEqual([query, query], [call[3] for call in transport.calls[1:]])
            self.assertEqual(transport.calls[1][0:2], transport.calls[2][0:2])

    def test_only_exact_job_native_timeout_is_retryable(self):
        query = "%s/%s?after=0" % (client_module.RESTORE_JOB_PATH, "a" * 32)
        errors = [
            self._native_timeout(query, code=12029),
            self._native_timeout(query, method="POST"),
            self._native_timeout(query + "0"),
            client_module.ReplayLabRestoreClientError("WinError 12002"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__, facts=vars(error)):
                with tempfile.TemporaryDirectory() as temporary:
                    client, transport, unused = self._client(self._runtime(temporary), [
                        (202, _job("running")), error,
                    ])
                    with self.assertRaises(client_module.ReplayLabRestoreClientError) as caught:
                        client.restore("D:/project", "D:/capture.rdc", "f" * 32)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(["POST", "GET"], [call[2] for call in transport.calls])
        with tempfile.TemporaryDirectory() as temporary:
            uncertain = client_module.ReplayLabRestoreRequestUncertain("POST timed out")
            client, transport, unused = self._client(self._runtime(temporary), [uncertain])
            with self.assertRaises(client_module.ReplayLabRestoreRequestUncertain):
                client.restore("D:/project", "D:/capture.rdc", "f" * 32)
            self.assertEqual(["POST"], [call[2] for call in transport.calls])

    def test_known_job_timeout_retry_stops_on_cancel_or_total_deadline(self):
        query = "%s/%s?after=0" % (client_module.RESTORE_JOB_PATH, "a" * 32)
        for cancel in (True, False):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as temporary:
                client, transport, unused = self._client(self._runtime(temporary), [
                    (202, _job("running")), self._native_timeout(query),
                    (202, {"cancelled": True}),
                ])
                clock = [0.0]
                client.monotonic = lambda: clock[0]
                client.timeout_seconds = 1.0
                def wait(unused):
                    if cancel:
                        client.cancel()
                    else:
                        clock[0] = 2.0
                client.sleep = wait
                with self.assertRaisesRegex(client_module.ReplayLabRestoreClientError,
                                            "cancelled" if cancel else "project restore timed out"):
                    client.restore("D:/project", "D:/capture.rdc", "f" * 32)
                self.assertEqual(["POST", "GET", "DELETE"], [call[2] for call in transport.calls])

    @unittest.skipUnless(os.name == "nt", "WinHTTP is Windows-only")
    def test_real_winhttp_receive_timeout_preserves_native_code_and_request(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        port = listener.getsockname()[1]
        stopped = threading.Event()
        def serve():
            try:
                connection, unused = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                       b"Connection: close\r\n\r\n")
                    # WinHTTP may round its short receive timeout to a whole
                    # second. Keep the peer open past that native boundary.
                    time.sleep(5.0)
            finally:
                listener.close()
                stopped.set()
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        with self.assertRaises(client_module.ReplayLabRestoreTransportError) as caught:
            client_module._winhttp_request_blocking(
                "GET", "/owned-job", port, "", b"", 10.0,
                threading.Event(), self.restore_generation)
        self.assertEqual(12002, caught.exception.winerror)
        self.assertEqual("GET", caught.exception.method)
        self.assertEqual("/owned-job", caught.exception.path)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(stopped.wait(6))

    def test_client_cancel_best_effort_deletes_current_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            transport = _Transport([
                (202, _job("queued")),
                (200, _job("running")),
                (200, {"cancelled": True}),
            ])
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), transport=transport,
                health_transport=_HealthTransport(),
            )
            client.sleep = lambda unused: client.cancel()

            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "cancelled"
            ):
                client.restore("D:/project", "D:/capture.rdc", "9" * 32)

            self.assertEqual(
                ["POST", "GET", "DELETE"],
                [call[2] for call in transport.calls],
            )
            self.assertFalse(transport.calls[-1][6].is_set())
            self.assertIsNone(transport.calls[-1][7])

    def test_delete_failure_does_not_mask_original_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            ticks = iter([0.0, 0.0, 0.0, 0.0, 2.0])

            class DeleteFailingTransport(_Transport):
                def __call__(self, port, token, method, path, body, timeout,
                             cancel_event, generation):
                    if method == "DELETE":
                        self.calls.append((
                            port, token, method, path, body, timeout,
                            cancel_event, generation))
                        raise RuntimeError("cleanup transport failed")
                    return _Transport.__call__(
                        self, port, token, method, path, body, timeout,
                        cancel_event, generation)

            transport = DeleteFailingTransport([
                (202, _job("queued")),
                (200, _job("running")),
            ])
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), timeout_seconds=1.0,
                startup_timeout_seconds=0.1,
                transport=transport, health_transport=_HealthTransport(),
                monotonic=lambda: next(ticks), sleep=lambda unused: None,
            )

            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError,
                "project restore timed out",
            ):
                client.restore("D:/project", "D:/capture.rdc", "a" * 32)

            self.assertEqual("DELETE", transport.calls[-1][2])

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
