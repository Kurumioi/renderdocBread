from __future__ import annotations

import copy
import contextlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from src import project_restore
from src.rdc_project_index import new_index, upsert_project_binding


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self, *unused_args, **unused_kwargs):
        return lambda function: function

    def run(self, **unused_kwargs):
        pass


def _load_server():
    try:
        return importlib.import_module("src.server")
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
    mcp_package = types.ModuleType("mcp")
    mcp_package.__path__ = []
    mcp_server = types.ModuleType("mcp.server")
    mcp_server.__path__ = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    with mock.patch.dict(
        sys.modules,
        {
            "mcp": mcp_package,
            "mcp.server": mcp_server,
            "mcp.server.fastmcp": fastmcp,
        },
    ):
        return importlib.import_module("src.server")


server = _load_server()


class _ActiveService:
    def __init__(self, status: dict):
        self._status = status

    def status(self) -> dict:
        return copy.deepcopy(self._status)


class _WorkspaceService:
    def __init__(self, status: dict):
        self._status = status
        self.status_calls = 0

    def status(self, workspace_path: str) -> dict:
        self.status_calls += 1
        if Path(workspace_path).resolve() != Path(self._status["workspacePath"]).resolve():
            raise AssertionError("unexpected workspace path")
        return copy.deepcopy(self._status)


class _ReplayService:
    def __init__(self, state: dict):
        self.state = state
        self.snapshot_calls = 0

    def restore_knowledge_snapshot(self, capture_id: str, catalog_sha256: str) -> dict:
        self.snapshot_calls += 1
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected catalogSha256")
        return copy.deepcopy(self.state)


class _IPC:
    def __init__(self, capture_path: Path, session_id: str, index: dict):
        self.capture_path = capture_path
        self.session_id = session_id
        self.index = index
        self.calls: list[tuple[str, dict]] = []
        self.write_error: Exception | None = None
        self.bind_error: Exception | None = None

    def call(self, method: str, params: dict | None = None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params))
        if method == "read_rdc_project_index":
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": 1,
                "present": True,
                "encodedSize": 1,
                "encodedSha256": "a" * 64,
                "document": copy.deepcopy(self.index),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        if method == "bind_rdc_project_shader_catalog":
            if self.bind_error is not None:
                raise self.bind_error
            return {
                "schema": "replaylab-rdc-project-shader-rebind/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "identityEvidenceSha256": params[
                    "expected_identity_evidence_sha256"
                ],
                "verifiedAnchorCount": len(params["anchors"]),
                "anchors": copy.deepcopy(params["anchors"]),
                "fullFrameTraversal": False,
                "physicalApplyAuthority": False,
            }
        if method == "write_rdc_project_index":
            if self.write_error is not None:
                raise self.write_error
            self.index = copy.deepcopy(params["document"])
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": 1,
                "present": True,
                "written": True,
                "physicalApplyAuthority": False,
                "document": copy.deepcopy(params["document"]),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        raise AssertionError("unexpected IPC method: %s" % method)


class _Host:
    def __init__(
        self,
        active: dict,
        workspace_status: dict,
        capture_path: Path,
        session_id: str,
        index: dict,
        identity_evidence: dict,
    ):
        self.active_service = _ActiveService(active)
        self.workspace_service = _WorkspaceService(workspace_status)
        self.ipc = _IPC(capture_path, session_id, index)
        self.capture_path = capture_path
        self.session_id = session_id
        self.identity_evidence = identity_evidence
        self.scope_calls = 0
        self.projection_calls = 0
        self.apply_calls = 0
        self.prepare_calls = 0

    def _active_project_service(self):
        return self.active_service

    def _shader_workspace_service(self):
        return self.workspace_service

    def _active_live_context(self, allow_missing_capture_id=False):
        return {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "path": str(self.capture_path),
                "captureSessionId": self.session_id,
                "captureId": "",
                "api": "D3D11",
            },
        }

    def _require_same_active_project(self, active):
        if active["workspaceId"] != self.active_service._status["workspaceId"]:
            raise AssertionError("active project changed")
        return copy.deepcopy(active)

    def _workspace_catalog_binding_projection(self, status):
        self.projection_calls += 1
        return {
            "workspaceId": status["workspaceId"],
            "captureId": status["capture"]["captureId"],
            "capturePath": status["capture"]["capturePath"],
            "api": status["capture"]["api"],
            "catalogPath": status["capture"]["catalogPath"],
            "catalogSha256": status["capture"]["catalogSha256"],
            "shaderKey": status["shader"]["shaderKey"],
            "stage": status["stage"],
            "entryPoint": status["entryPoint"],
            "resourceId": status["shader"]["resourceId"],
            "rawSha256": status["shader"]["rawSha256"],
        }

    def _workspace_catalog_rebind_scope(self, status):
        self.scope_calls += 1
        return {"identityEvidence": copy.deepcopy(self.identity_evidence)}

    def _canonical_json_sha256(self, value, label):
        return project_restore._canonical_sha256(value)

    def apply_shader_workspace_shared_resource(
        self, workspace_path, expected_revision, expected_candidate_raw_sha256
    ):
        self.apply_calls += 1
        response = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "binding": {
                "workspaceId": self.workspace_service._status["workspaceId"],
                "captureId": self.workspace_service._status["capture"]["captureId"],
                "revision": expected_revision,
                "candidateRawSha256": expected_candidate_raw_sha256,
            },
            "validation": {"replayAbiValidated": True},
            "replacement": {
                "replacementId": "9" * 32,
                "workspaceId": self.workspace_service._status["workspaceId"],
                "captureSessionId": self.session_id,
                "candidateRawSha256": expected_candidate_raw_sha256,
                "resourceScoped": True,
                "originalShaderPreserved": True,
            },
            "scope": "captured-resource",
            "applied": True,
        }
        return json.dumps(response)

    def prepare_active_capture_project(self):
        self.prepare_calls += 1
        return json.dumps({"error": "fallback deliberately stopped by test"})


class ProjectRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.replay_path = self.project / ".replaylab" / "replay-project"
        self.shader_root = self.project / "workspaces" / "shaders"
        self.workspace = self.shader_root / "cached"
        self.workspace.mkdir(parents=True)
        self.replay_path.mkdir(parents=True)
        self.capture_path = self.root / "frame.rdc"
        self.capture_path.write_bytes(b"rdc")
        self.catalog_path = self.project / "captures" / "catalog.json"
        self.catalog_path.parent.mkdir(parents=True)
        self.catalog_path.write_text("{}", encoding="utf-8")

        self.project_id = "1" * 32
        self.active_workspace_id = "2" * 32
        self.capture_id = "3" * 64
        self.catalog_sha = "4" * 64
        self.knowledge_sha = "5" * 64
        self.scope_sha = "6" * 64
        self.matcher_sha = "7" * 64
        self.managed_workspace_id = "8" * 32
        self.knowledge_id = "a" * 32
        self.raw_sha = "b" * 64
        self.code_sha = "c" * 64
        self.interface_sha = "d" * 64
        self.layout_sha = "e" * 64
        self.candidate_sha = "f" * 64
        self.source_sha = "0" * 64
        self.session_id = "9" * 32
        self.shader_key = "7:fragment:main:%s" % self.raw_sha
        self.identity = {
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "interfaceComplete": True,
            "eventIds": [9],
        }
        self.shader_record = {
            "identity": copy.deepcopy(self.identity),
            "knowledgeId": self.knowledge_id,
            "managedWorkspace": {
                "workspaceId": self.managed_workspace_id,
                "path": self.workspace.relative_to(self.project).as_posix(),
            },
            "candidateRevision": 3,
            "candidateRawSha256": self.candidate_sha,
            "sourceSha256": self.source_sha,
        }
        self.binding = {
            "projectId": self.project_id,
            "workspaceId": self.active_workspace_id,
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "shaders": [copy.deepcopy(self.shader_record)],
            "materials": [],
        }
        self.index = upsert_project_binding(new_index(), self.binding)
        self.active = {
            "workspaceId": self.active_workspace_id,
            "scopeConfigured": True,
            "projectRoot": str(self.project),
            "paths": {
                "replayProject": str(self.replay_path),
                "shaderWorkspaces": str(self.shader_root),
            },
            "replayProject": {
                "projectId": self.project_id,
                "projectPath": str(self.replay_path),
            },
        }
        self.workspace_status = {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(self.workspace),
            "workspaceId": self.managed_workspace_id,
            "capture": {
                "captureId": self.capture_id,
                "capturePath": str(self.capture_path),
                "api": "D3D11",
                "catalogPath": str(self.catalog_path),
                "catalogSha256": self.catalog_sha,
            },
            "shader": {
                "shaderKey": self.shader_key,
                "resourceId": 7,
                "rawSha256": self.raw_sha,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "revision": 3,
            "source": {
                "present": True,
                "currentSha256": self.source_sha,
            },
            "latestCompiled": {
                "revision": 3,
                "sourceSha256": self.source_sha,
                "rawSha256": self.candidate_sha,
            },
        }
        self.state = {
            "projectId": self.project_id,
            "projectPath": str(self.replay_path),
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "capture": {"shaders": [copy.deepcopy(self.identity)]},
            "workspaces": {
                self.knowledge_id: {
                    "knowledgeId": self.knowledge_id,
                    "sourceSha256": self.source_sha,
                    "stage": "fragment",
                    "entryPoint": "main",
                }
            },
            "materials": {},
        }
        self.identity_evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.shader_key],
            "useTuples": [[9, self.shader_key]],
        }

    def tearDown(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp.cleanup()

    def _run(self, *, write_error=None, bind_error=None):
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index,
            self.identity_evidence,
        )
        host.ipc.write_error = write_error
        host.ipc.bind_error = bind_error
        replay = _ReplayService(self.state)
        progress = []
        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=replay
        ):
            result = project_restore.restore_active_capture_project(
                self.project,
                self.capture_path,
                self.session_id,
                progress.append,
            )
        return result, host, replay, progress

    def test_full_prepare_polls_token_bound_shader_export_progress(self) -> None:
        events = []
        warnings = []
        active = {
            "workspaceId": self.active_workspace_id,
            "projectRoot": str(self.project),
        }
        prepared = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "project": copy.deepcopy(active),
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
            },
            "reusePlan": {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "matches": [],
            },
        }

        class Host:
            cleaned = False
            prepare_thread = ""
            read_calls = 0

            @staticmethod
            def _shader_catalog_progress_channel(capture_session_id):
                return {
                    "operationToken": "d" * 32,
                    "captureSessionId": capture_session_id,
                }

            def _prepare_active_capture_project(self, token):
                self.prepare_thread = project_restore.threading.current_thread().name
                self.assert_token = token
                time.sleep(0.08)
                return json.dumps(prepared)

            def _read_shader_catalog_progress(self, channel):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise RuntimeError("transient atomic replace race")
                return {
                    "eventId": 77,
                    "current": 3,
                    "total": 5,
                }

            def _cleanup_shader_catalog_progress(self, channel):
                self.cleaned = True

            @staticmethod
            def _require_same_active_project(unused_active):
                return None

        host = Host()
        progress = project_restore._Progress("e" * 32, events.append, warnings)

        unused_prepared, staged, reuse = project_restore._prepare_response(
            host, active, self.session_id, progress
        )

        self.assertEqual(staged["captureId"], self.capture_id)
        self.assertEqual(reuse["matches"], [])
        self.assertEqual(host.assert_token, "d" * 32)
        self.assertEqual(host.prepare_thread, "replaylab-shader-catalog-prepare")
        self.assertTrue(host.cleaned)
        self.assertGreaterEqual(host.read_calls, 2)
        self.assertTrue(any("progress unavailable" in item for item in warnings))
        self.assertEqual(events[-1]["phase"], "export-shader-bindings")
        self.assertEqual((events[-1]["current"], events[-1]["total"]), (3, 5))
        self.assertIn("EID 77", events[-1]["message"])

    def test_progress_channel_setup_failure_falls_back_to_plain_prepare(self) -> None:
        warnings = []
        active = {
            "workspaceId": self.active_workspace_id,
            "projectRoot": str(self.project),
        }
        prepared = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "project": copy.deepcopy(active),
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
            },
            "reusePlan": {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "matches": [],
            },
        }

        class Host:
            plain_calls = 0
            internal_calls = 0

            @staticmethod
            def _shader_catalog_progress_channel(unused_capture_session_id):
                raise OSError("sidecar unavailable")

            def _prepare_active_capture_project(self, unused_token):
                self.internal_calls += 1
                raise AssertionError("token prepare must not run after setup failure")

            @staticmethod
            def _read_shader_catalog_progress(unused_channel):
                raise AssertionError("progress must not be read")

            @staticmethod
            def _cleanup_shader_catalog_progress(unused_channel):
                raise AssertionError("absent channel must not be cleaned")

            def prepare_active_capture_project(self):
                self.plain_calls += 1
                return json.dumps(prepared)

            @staticmethod
            def _require_same_active_project(unused_active):
                return None

        host = Host()
        progress = project_restore._Progress("e" * 32, None, warnings)

        unused_prepared, staged, reuse = project_restore._prepare_response(
            host, active, self.session_id, progress
        )

        self.assertEqual(staged["captureId"], self.capture_id)
        self.assertEqual(reuse["matches"], [])
        self.assertEqual(host.plain_calls, 1)
        self.assertEqual(host.internal_calls, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("progress setup failed", warnings[0])

    def test_exact_cache_hit_batches_anchor_and_never_prepares_full_frame(self) -> None:
        result, host, replay, progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.scope_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.snapshot_calls, 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(methods.count("bind_rdc_project_shader_catalog"), 1)
        self.assertNotIn("export_shader_catalog", methods)
        self.assertNotIn("prepare", [event["phase"] for event in progress])
        self.assertTrue(all(len(event["message"].encode("utf-8")) <= 1024 for event in progress))

    def test_read_only_rdc_does_not_reverse_successful_restore(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            write_error=PermissionError("read only")
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["index"]["written"])
        self.assertIn("read only", result["index"]["warning"])
        self.assertEqual(host.prepare_calls, 0)

    def test_cached_anchors_share_one_catalog_parse_and_batch_at_256(self) -> None:
        shader_keys = []
        validated = []
        for number in range(257):
            resource_id = number + 1
            event_id = number + 10
            shader_key = "%d:fragment:main:%s" % (resource_id, self.raw_sha)
            shader_keys.append(shader_key)
            identity = {
                "resourceId": resource_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.raw_sha,
                "eventIds": [event_id],
            }
            status = copy.deepcopy(self.workspace_status)
            status["workspaceId"] = "%032x" % (number + 1)
            status["shader"]["resourceId"] = resource_id
            status["shader"]["shaderKey"] = shader_key
            validated.append(
                (
                    {"identity": identity},
                    self.workspace,
                    status,
                )
            )
        evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": list(range(10, 267)),
                "stages": ["fragment"],
            },
            "shaderKeys": shader_keys,
            "useTuples": [
                [number + 10, shader_keys[number]] for number in range(257)
            ],
        }
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index,
            evidence,
        )

        reports = project_restore._bind_workspace_anchors(
            host,
            self.capture_path,
            self.session_id,
            validated,
        )

        bind_calls = [
            params
            for method, params in host.ipc.calls
            if method == "bind_rdc_project_shader_catalog"
        ]
        self.assertEqual(host.scope_calls, 1)
        self.assertEqual(host.projection_calls, 257)
        self.assertEqual([len(call["anchors"]) for call in bind_calls], [256, 1])
        self.assertEqual(len(reports), 2)

    def test_failed_live_anchor_is_never_applied_and_enters_fallback(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            bind_error=RuntimeError("anchor changed")
        )

        self.assertFalse(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertIn("cache-live-validation-failed", result["cache"]["reason"])
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.prepare_calls, 1)

    def test_live_edit_lease_result_runs_shader_project_sync_hook(self) -> None:
        applied = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "applied": True,
            "replacement": {"replacementId": "1" * 32},
        }
        sync = {"attempted": True, "succeeded": True}
        with mock.patch.object(
            server,
            "_apply_shader_workspace_shared_resource_with_live_abi_lease",
            return_value=copy.deepcopy(applied),
        ), mock.patch.object(
            server, "_best_effort_project_shader_sync", return_value=sync
        ) as hook:
            result = server._shader_live_edit_apply_candidate(
                "managed-workspace", 4, "a" * 64
            )

        self.assertEqual(result["projectSync"], sync)
        hook.assert_called_once_with(
            "managed-workspace", 4, "a" * 64, mock.ANY
        )

    def test_material_update_runs_project_sync_hook_after_live_success(self) -> None:
        updated = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "operation": "update",
            "sessionId": "1" * 32,
            "updatedParameterId": "2" * 32,
            "applied": True,
        }

        class Manager:
            def update(self, session_id, parameter, value):
                return copy.deepcopy(updated)

        sync = {"attempted": True, "succeeded": True}
        with mock.patch.object(
            server, "_material_session_manager", return_value=Manager()
        ), mock.patch.object(
            server, "_best_effort_project_material_sync", return_value=sync
        ) as hook:
            result = json.loads(
                server.update_material_parameter("1" * 32, "2" * 32, 0.5)
            )

        self.assertEqual(result["projectSync"], sync)
        hook.assert_called_once_with("1" * 32, updated)

    def test_material_sync_accumulates_live_session_leaves_and_rdc_hint(self) -> None:
        parameter_a = "a" * 32
        parameter_b = "b" * 32
        material_session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "eventId": 9,
            "stage": "fragment",
            "shaderResourceId": 7,
            "constantBlockIndex": 0,
            "constantBlockPath": "cb0",
            "layoutSha256": self.layout_sha,
            "parameters": [
                {
                    "parameterId": parameter_a,
                    "path": "cb0.A",
                    "baseType": "Float",
                    "components": 1,
                    "value": 1.0,
                },
                {
                    "parameterId": parameter_b,
                    "path": "cb0.B",
                    "baseType": "Float",
                    "components": 1,
                    "value": 2.0,
                },
            ],
        }

        class Active:
            def __init__(self, owner):
                self.owner = owner
                self.saved = []
                self.durable_ids = set()

            def status(self):
                return copy.deepcopy(owner.active)

            def pin_active_project(self, unused_workspace_id, unused_project_root):
                return contextlib.nullcontext()

            def save_material_knowledge(self, session, parameter_ids):
                self.saved.append(list(parameter_ids))
                self.durable_ids.update(parameter_ids)
                return {
                    "knowledge": {
                        "knowledgeId": "%032x" % len(self.saved),
                        "parameters": [
                            copy.deepcopy(parameter)
                            for parameter in session["parameters"]
                            if parameter["parameterId"] in self.durable_ids
                        ],
                    }
                }

        owner = self
        active_service = Active(self)

        class Host:
            def get_material_session_status(self, session_id):
                self.last_session_id = session_id
                return json.dumps(copy.deepcopy(material_session))

            def _active_project_service(self):
                return active_service

            def _require_same_active_project(self, active):
                return copy.deepcopy(active)

            def _active_live_context(self, allow_missing_capture_id=False):
                return {
                    "capture": {
                        "path": str(owner.capture_path),
                        "captureSessionId": owner.session_id,
                    }
                }

        class Replay:
            def restore_knowledge_snapshot(self, capture_id, catalog_sha256):
                self.args = (capture_id, catalog_sha256)
                return {"projectId": owner.project_id}

        material_records = []

        def sync_index(
            unused_host,
            unused_active,
            unused_replay,
            unused_binding_state,
            unused_capture_path,
            unused_capture_session_id,
            **records,
        ):
            material_records.append(copy.deepcopy(records["material_record"]))
            return True, ""

        with mock.patch.object(project_restore, "_host_module", return_value=Host()), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=Replay()
        ), mock.patch.object(project_restore, "_sync_index_binding", side_effect=sync_index):
            first = project_restore.sync_active_material_session(
                self.session_id, [parameter_a]
            )
            # Host restart/eviction removes only the optimisation. The durable
            # project head returned by save remains complete and authoritative.
            project_restore._MATERIAL_SYNC_PARAMETERS.clear()
            second = project_restore.sync_active_material_session(
                self.session_id, [parameter_b]
            )

        self.assertEqual(first["parameterIds"], [parameter_a])
        self.assertEqual(second["parameterIds"], [parameter_a, parameter_b])
        self.assertEqual(
            active_service.saved,
            [[parameter_a], [parameter_b]],
        )
        self.assertEqual(
            material_records[-1]["parameters"],
            [{"parameterId": parameter_a}, {"parameterId": parameter_b}],
        )


if __name__ == "__main__":
    unittest.main()
