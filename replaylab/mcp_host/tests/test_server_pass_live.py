from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self):
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


class _PassIPC:
    def __init__(self, capture_id: str, session_id: str):
        self.capture_id = capture_id
        self.session_id = session_id
        self.calls = []

    def call(self, method, params=None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params, timeout))
        if method == "get_pass_classification_inputs":
            return {
                "schema": "replaylab-pass-classification-inputs/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "actions": [
                    {
                        "eventId": 10,
                        "name": "BasePass Opaque",
                        "flags": 0x0002,
                        "numIndices": 3,
                        "numInstances": 1,
                        "markerPath": ["Scene", "BasePass"],
                    }
                ],
                "pipelineStates": {
                    "10": {
                        "shaders": {
                            "fragment": {
                                "shaderId": 77,
                                "entryPoint": "main",
                                "rawSha256": "c" * 64,
                            }
                        },
                        "renderTargets": [
                            {"resourceId": 4, "name": "SceneColor", "format": "RGBA8"}
                        ],
                        "depthTarget": {
                            "resourceId": 5,
                            "name": "SceneDepth",
                            "format": "D32",
                        },
                        "depthTestEnabled": True,
                        "depthWriteEnabled": True,
                    }
                },
                "summary": {
                    "actionCount": 1,
                    "pipelineStateCount": 1,
                    "singleReplayThreadTransaction": True,
                    "truncated": False,
                },
            }
        if method == "load_pass_classification":
            classification = params["classification"]
            return {
                "schema": "replaylab-pass-panel-load/v1",
                "schemaVersion": 1,
                "captureId": classification["captureId"],
                "captureSessionId": self.session_id,
                "panel": {"visible": True},
                "loaded": True,
                "replayMutation": False,
                "classificationAuthority": "FixedProgram",
                "aiProposalAuthority": "AIProvisional",
            }
        raise AssertionError("unexpected IPC method %s" % method)


class _PassStore:
    instances = []

    def __init__(self, project_path):
        self.project_path = Path(project_path).resolve()
        self.saved = []
        type(self).instances.append(self)

    def load(self):
        return {
            "schema": "replaylab-project-pass-knowledge/v1",
            "schemaVersion": 1,
            "records": [],
        }

    def save(self, classification, *, capture_id=""):
        self.saved.append((copy.deepcopy(classification), capture_id))
        return {
            "schema": "replaylab-project-pass-knowledge/v1",
            "schemaVersion": 1,
            "path": str(self.project_path / "knowledge" / "passes.json"),
            "captureId": capture_id,
            "provisionalOverlayCount": classification.get(
                "provisionalOverlayCount", 0
            ),
        }


class ServerPassClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.capture_id = "a" * 64
        self.session_id = "b" * 32
        self.workspace_id = "d" * 32
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "path": str(self.root / "capture.rdc"),
                "api": "GraphicsAPI.D3D12",
            },
            "events": {"currentEventId": 10},
            "shaders": {"bound": []},
        }
        self.catalog_root = self.root / "catalogs"
        self.catalog = self.catalog_root / self.capture_id / "catalog.json"
        self.catalog.parent.mkdir(parents=True)
        self.catalog_value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "shaders": [],
            "uses": [],
        }
        self.catalog.write_text(json.dumps(self.catalog_value), encoding="utf-8")
        self.replay_project = self.root / "replay-project"
        self.replay_project.mkdir()
        self.prepared = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogPath": str(self.catalog),
                "catalogSha256": hashlib.sha256(self.catalog.read_bytes()).hexdigest(),
            },
            "project": {
                "workspaceId": self.workspace_id,
                "scopeConfigured": True,
                "paths": {
                    "captureCatalogs": str(self.catalog_root),
                    "replayProject": str(self.replay_project),
                },
                "replayProject": {"projectPath": str(self.replay_project)},
            },
            "applied": False,
        }
        self.ipc = _PassIPC(self.capture_id, self.session_id)
        self.active = mock.Mock()
        self.active.status.return_value = copy.deepcopy(self.prepared["project"])
        self.old_ipc = self.server.ipc
        self.old_active = self.server._ACTIVE_PROJECT_SERVICE
        self.server.ipc = self.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active
        self.server._PASS_CLASSIFICATION_CACHE.clear()
        _PassStore.instances.clear()
        self.patches = [
            mock.patch.object(
                self.server,
                "_active_live_context",
                side_effect=lambda **unused: copy.deepcopy(self.context),
            ),
            mock.patch.object(
                self.server,
                "prepare_active_capture_project",
                return_value=json.dumps(self.prepared),
            ),
            mock.patch.object(self.server, "PassKnowledgeStore", _PassStore),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.old_active
        self.server._PASS_CLASSIFICATION_CACHE.clear()
        self.temporary.cleanup()

    def _fixed(self):
        result = json.loads(self.server.classify_current_frame_passes())
        self.assertNotIn("error", result, result)
        return result

    def test_fixed_classification_is_capture_bound_persisted_and_published(self):
        result = self._fixed()
        self.assertEqual(result["operation"], "classify-fixed")
        self.assertEqual(result["classification"]["captureId"], self.capture_id)
        self.assertEqual(result["classification"]["summary"]["actionCount"], 1)
        self.assertEqual(len(result["classificationId"]), 32)
        self.assertTrue(result["projectKnowledge"]["persisted"])
        self.assertTrue(result["panel"]["published"])
        self.assertEqual(len(_PassStore.instances), 1)
        self.assertEqual(_PassStore.instances[0].saved[0][1], self.capture_id)
        inputs_call = next(
            item for item in self.ipc.calls if item[0] == "get_pass_classification_inputs"
        )
        self.assertEqual(inputs_call[1]["expected_capture_id"], self.capture_id)
        self.assertEqual(
            inputs_call[1]["expected_capture_session_id"], self.session_id
        )
        panel_call = next(
            item for item in self.ipc.calls if item[0] == "load_pass_classification"
        )
        self.assertEqual(
            panel_call[1]["expected_capture_session_id"], self.session_id
        )

    def test_ai_overlay_uses_only_current_server_cached_fixed_result(self):
        fixed = self._fixed()
        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                self.capture_id,
                self.session_id,
                [
                    {
                        "eventId": 10,
                        "category": "BasePass",
                        "subcategory": "Opaque",
                        "displayLabel": "Main opaque objects",
                        "rationale": "Asset-verified material naming suggests BasePass",
                    }
                ],
            )
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["operation"], "attach-ai-provisional")
        proposal = result["classification"]["items"][0]["semanticProposal"]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertEqual(
            result["fixedClassificationSha256"],
            fixed["fixedClassificationSha256"],
        )
        self.assertEqual(len(_PassStore.instances), 2)
        self.assertEqual(_PassStore.instances[-1].saved[-1][0]["items"][0]["category"],
                         fixed["classification"]["items"][0]["category"])

    def test_ai_overlay_rejects_physical_fields_and_changed_session(self):
        fixed = self._fixed()
        forbidden = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                self.capture_id,
                self.session_id,
                [{"eventId": 10, "category": "BasePass", "byteOffset": 16}],
            )
        )
        self.assertIn("forbidden fields", forbidden["error"])
        self.context["capture"]["captureSessionId"] = "e" * 32
        stale = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                self.capture_id,
                self.session_id,
                [{"eventId": 10, "category": "BasePass"}],
            )
        )
        self.assertIn("capture/session changed", stale["error"])


class _LiveManager:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _result(live_edit_id="f" * 32):
        return {
            "schema": "replaylab-shader-live-edit/v1",
            "schemaVersion": 1,
            "liveEditId": live_edit_id,
            "state": "running",
            "rollback": None,
        }

    def start(self, workspace_path, debounce_ms):
        self.calls.append(("start", workspace_path, debounce_ms))
        return self._result()

    def poll(self, live_edit_id):
        self.calls.append(("poll", live_edit_id))
        return self._result(live_edit_id)

    def stop(self, live_edit_id):
        self.calls.append(("stop", live_edit_id))
        value = self._result(live_edit_id)
        value["state"] = "stopped"
        return value

    def list(self):
        self.calls.append(("list",))
        return {
            "persistenceEnabled": True,
            "sessions": [self._result()],
            "authorizations": [],
            "resumeResults": [],
        }

    def migrate_authorization(self, source_workspace, workspace, source_sha256):
        self.calls.append(
            ("migrate-authorization", source_workspace, workspace, source_sha256)
        )
        return {
            "authorizationMigrated": True,
            "authorizationId": "e" * 32,
            "sourceWorkspacePath": source_workspace,
            "workspacePath": workspace,
            "sourceSha256": source_sha256,
            "watchStarted": False,
            "restartResumeAuthorized": True,
            "newSaveRequired": True,
            "semanticEquivalenceVerified": False,
        }


class ServerLiveEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.manager = _LiveManager()
        self.old_manager = self.server._SHADER_LIVE_EDIT_MANAGER
        self.server._SHADER_LIVE_EDIT_MANAGER = self.manager

    def tearDown(self):
        self.server._SHADER_LIVE_EDIT_MANAGER = self.old_manager

    def test_start_poll_stop_expose_shared_resource_policy(self):
        started = json.loads(
            self.server.start_shader_workspace_live_edit("D:/workspace", 450)
        )
        self.assertEqual(started["operation"], "start")
        self.assertTrue(started["backgroundAutoApplyAuthorized"])
        self.assertEqual(started["applyScope"], "captured-shader-resource")
        self.assertTrue(started["policy"]["distinctResourceIdsNeverMerged"])
        self.assertTrue(
            started["policy"]["failedSavePreservesLastGoodReplacement"]
        )
        token = started["result"]["liveEditId"]
        self.assertEqual(
            json.loads(self.server.poll_shader_workspace_live_edit(token))["operation"],
            "poll",
        )
        stopped = json.loads(self.server.stop_shader_workspace_live_edit(token))
        self.assertEqual(stopped["result"]["state"], "stopped")
        self.assertFalse(stopped["backgroundAutoApplyAuthorized"])
        self.assertEqual(
            self.manager.calls,
            [
                ("start", "D:/workspace", 450),
                ("poll", token),
                ("stop", token),
            ],
        )

    def test_list_exposes_persistent_sessions(self):
        listed = json.loads(self.server.list_shader_workspace_live_edits())
        self.assertEqual(listed["operation"], "list")
        self.assertTrue(listed["result"]["persistenceEnabled"])
        self.assertEqual(self.manager.calls, [("list",)])

    def test_explicit_authorization_migration_is_exposed_without_applying(self):
        source_hash = "a" * 64
        migrated = json.loads(
            self.server.migrate_shader_workspace_live_edit_authorization(
                "D:/old-workspace",
                "D:/new-workspace",
                source_hash,
            )
        )
        self.assertEqual(migrated["operation"], "migrate-authorization")
        self.assertTrue(migrated["result"]["authorizationMigrated"])
        self.assertFalse(migrated["result"]["watchStarted"])
        self.assertFalse(migrated["appliedByThisCall"])
        self.assertEqual(
            self.manager.calls,
            [
                (
                    "migrate-authorization",
                    "D:/old-workspace",
                    "D:/new-workspace",
                    source_hash,
                )
            ],
        )

    def test_apply_callback_parses_success_and_raises_error_envelope(self):
        success = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "applied": False,
            "binding": {"revision": 2, "candidateRawSha256": "a" * 64},
            "validation": {"replayAbiValidated": False},
        }
        with mock.patch.object(
            self.server,
            "apply_shader_workspace_shared_resource",
            return_value=json.dumps(success),
        ) as apply:
            result = self.server._shader_live_edit_apply_candidate(
                "D:/workspace", 2, "a" * 64
            )
        self.assertEqual(result, success)
        apply.assert_called_once_with(
            "D:/workspace",
            expected_revision=2,
            expected_candidate_raw_sha256="a" * 64,
        )
        with mock.patch.object(
            self.server,
            "apply_shader_workspace_shared_resource",
            return_value=json.dumps({"error": "ABI changed", "type": "RuntimeError"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "ABI changed"):
                self.server._shader_live_edit_apply_candidate(
                    "D:/workspace", 3, "b" * 64
                )

    def test_later_live_save_uses_initial_abi_lease_and_one_bridge_apply(self):
        workspace = "D:/workspace-live-abi-lease"
        capture_id = "c" * 64
        session_id = "8" * 32
        workspace_id = "b" * 32
        baseline_hash = "a" * 64
        candidate_hash = "d" * 64
        baseline_abi = "9" * 64
        lease_request = "e" * 64
        lease_report = "f" * 64
        current_request = "7" * 64
        old_replacement = "1" * 32
        new_replacement = "2" * 32
        source_hash = "3" * 64
        request = {
            "requestId": current_request,
            "binding": {
                "workspaceId": workspace_id,
                "captureId": capture_id,
                "revision": 4,
                "candidateRawSha256": candidate_hash,
                "baselineAbiSha256": baseline_abi,
                "stage": "fragment",
                "entryPoint": "main",
                "api": "d3d12",
                "encoding": "dxil",
            },
            "params": {
                "candidate_path": "D:/fixed/candidate.dxil",
                "expected_sha256": candidate_hash,
                "stage": "fragment",
                "entry_point": "main",
                "encoding": "dxil",
            },
        }
        status = {
            "workspacePath": workspace,
            "workspaceId": workspace_id,
            "capture": {
                "captureId": capture_id,
                "catalogPath": "D:/fixed/catalog.json",
            },
            "shader": {
                "shaderKey": "77:fragment:main:%s" % baseline_hash,
                "resourceId": 77,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "revision": 4,
            "source": {"currentSha256": source_hash},
            "representation": {
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "baseline": {"rawSha256": baseline_hash},
            "latestCompiled": {
                "revision": 4,
                "sourceSha256": source_hash,
                "rawSha256": candidate_hash,
            },
        }

        class Service:
            def status(inner_self, _workspace):
                return copy.deepcopy(status)

            def candidate_reflection_request(inner_self, _workspace):
                return copy.deepcopy(request)

        class Bridge:
            def __init__(inner_self):
                inner_self.calls = []

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, copy.deepcopy(params), timeout))
                self.assertEqual(method, "apply_shader_candidate_shared_resource")
                self.assertEqual(
                    params["abi_validation_mode"],
                    "inline-physical-with-initial-lease",
                )
                self.assertEqual(params["abi_report_sha256"], lease_report)
                self.assertEqual(params["abi_request_id"], current_request)
                self.assertEqual(params["abi_lease_revision"], 3)
                return {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": new_replacement,
                    "captureId": capture_id,
                    "captureSessionId": session_id,
                    "workspaceId": workspace_id,
                    "revision": 4,
                    "anchorEventId": 321,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "api": "d3d12",
                    "encoding": "dxil",
                    "originalResourceId": 77,
                    "originalRawSha256": baseline_hash,
                    "candidateRawSha256": candidate_hash,
                    "physicalAbiSha256": baseline_abi,
                    "shaderFamilyId": "6" * 64,
                    "targetResourceId": 101,
                    "abiRequestId": current_request,
                    "abiReportSha256": lease_report,
                    "resourceScoped": True,
                    "eventScoped": False,
                    "allOriginalResourceUsesAffected": True,
                    "configured": True,
                    "originalShaderPreserved": True,
                    "singleOwner": True,
                    "checkedDerivedPipelineRebuild": True,
                    "capturedRootSignaturesReused": True,
                    "idempotent": False,
                    "metadataOnly": False,
                    "targetBuiltOnce": True,
                    "targetReused": False,
                    "supersededReplacementId": old_replacement,
                    "supersededTargetFreed": True,
                    "abiValidationMode": (
                        "inline-physical-with-initial-lease"
                    ),
                    "preliminaryReflectionSkipped": True,
                    "freshPhysicalAbiValidationPerformed": True,
                    "abiLeaseRequestId": lease_request,
                    "abiLeaseReportSha256": lease_report,
                    "abiLeaseCandidateRawSha256": "4" * 64,
                    "abiLeaseRevision": 3,
                }

        lease = {
            "replacementId": old_replacement,
            "captureId": capture_id,
            "captureSessionId": session_id,
            "workspaceId": workspace_id,
            "workspacePath": workspace,
            "revision": 3,
            "stage": "fragment",
            "originalResourceId": 77,
            "candidateRawSha256": "4" * 64,
            "abiLeaseRequestId": lease_request,
            "abiLeaseReportSha256": lease_report,
            "abiLeaseCandidateRawSha256": "4" * 64,
            "abiLeaseRevision": 3,
            "physicalAbiSha256": baseline_abi,
        }
        bridge = Bridge()
        with self.server._SHADER_OVERRIDE_GUARD:
            previous_capture_key = self.server._SHADER_OVERRIDE_CAPTURE_KEY
            previous_records = copy.deepcopy(
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS
            )
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
                old_replacement
            ] = copy.deepcopy(lease)
            self.server._SHADER_OVERRIDE_CAPTURE_KEY = session_id
        try:
            with mock.patch.object(
                self.server, "_shader_workspace_service", return_value=Service()
            ), mock.patch.object(
                self.server, "_ensure_workspace_capture_catalog_identity",
                return_value={"captureSessionId": session_id},
            ), mock.patch.object(
                self.server, "list_exact_shader_family_uses", return_value=[321]
            ), mock.patch.object(
                self.server,
                "_shader_family_event_bindings",
                return_value=[{
                    "eventId": 321,
                    "originalResourceId": 77,
                    "originalRawSha256": baseline_hash,
                    "physicalAbiSha256": baseline_abi,
                }],
            ), mock.patch.object(
                self.server, "ipc", bridge
            ), mock.patch.object(
                self.server,
                "apply_shader_workspace_shared_resource",
                side_effect=AssertionError("ordinary duplicate ABI pass was used"),
            ):
                result = self.server._shader_live_edit_apply_candidate(
                    workspace, 4, candidate_hash
                )
            self.assertTrue(result["applied"])
            self.assertTrue(result["validation"]["replayAbiValidated"])
            self.assertTrue(
                result["validation"]["preliminaryReplayReflectionSkipped"]
            )
            self.assertTrue(
                result["validation"]["freshPhysicalValidationPerformed"]
            )
            self.assertEqual(len(bridge.calls), 1)
            with self.server._SHADER_OVERRIDE_GUARD:
                remembered = self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
                    new_replacement
                ]
            self.assertEqual(remembered["abiLeaseRequestId"], lease_request)
            self.assertEqual(
                remembered["abiLeaseReportSha256"], lease_report
            )
        finally:
            with self.server._SHADER_OVERRIDE_GUARD:
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS.update(
                    previous_records
                )
                self.server._SHADER_OVERRIDE_CAPTURE_KEY = previous_capture_key


class ServerRestoreCurrentShaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.capture_id = "1" * 64
        self.session_id = "2" * 32
        self.original_raw = "3" * 64
        self.candidate_raw = "4" * 64
        self.workspace_id = "5" * 32
        self.replacement_id = "6" * 32
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "events": {"currentEventId": 22},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": 77,
                        "entryPoint": "main",
                        "rawSha256": self.candidate_raw,
                    }
                ]
            },
        }
        self.shader_root = self.root / "workspaces"
        self.workspace = (
            self.shader_root
            / self.capture_id
            / "fragment"
            / ("rid-77-%s" % self.original_raw)
        )
        self.workspace.mkdir(parents=True)

        class Active:
            def status(inner):
                return {"paths": {"shaderWorkspaces": str(self.shader_root)}}

            def shader_workspace_path(inner, **unused):
                return self.workspace

        class Workspaces:
            def status(inner, path):
                return {
                    "schema": "renderdoc-ultra-shader-workspace-status/v1",
                    "workspacePath": str(self.workspace),
                    "workspaceId": self.workspace_id,
                    "revision": 3,
                    "capture": {"captureId": self.capture_id},
                    "shader": {
                        "stage": "fragment",
                        "resourceId": 77,
                        "entryPoint": "main",
                        "rawSha256": self.original_raw,
                    },
                }

        self.active = Active()
        self.workspaces = Workspaces()
        self.replacements = [
            {
                "replacementId": self.replacement_id,
                "workspaceId": self.workspace_id,
                "revision": 2,
                "anchorEventId": 22,
                "stage": "fragment",
                "entryPoint": "main",
                "api": "d3d11",
                "encoding": "dxbc",
                "originalResourceId": 77,
                "originalRawSha256": self.original_raw,
                "candidateRawSha256": self.candidate_raw,
                "targetResourceId": 88,
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "originalShaderPreserved": True,
            }
        ]

        class IPC:
            def __init__(inner):
                inner.calls = []

            def call(inner, method, params=None, timeout=None):
                inner.calls.append((method, copy.deepcopy(params or {}), timeout))
                if method != "get_shared_shader_replacements":
                    raise AssertionError(method)
                return {
                    "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                    "schemaVersion": 1,
                    "captureId": self.capture_id,
                    "captureSessionId": self.session_id,
                    "replacements": copy.deepcopy(self.replacements),
                    "count": len(self.replacements),
                    "originalShadersPreserved": True,
                    "replayMutation": False,
                }

        self.ipc = IPC()
        self.old_ipc = self.server.ipc
        self.old_active = self.server._ACTIVE_PROJECT_SERVICE
        self.server.ipc = self.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active
        rollback = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "rollback-shared-resource",
            "workspaceId": self.workspace_id,
            "captureId": self.capture_id,
            "replacement": {
                "replacementId": self.replacement_id,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "workspaceId": self.workspace_id,
                "originalResourceId": 77,
                "cleared": True,
                "originalShaderPreserved": True,
            },
            "applied": False,
        }
        self.patches = [
            mock.patch.object(
                self.server,
                "_active_live_context",
                side_effect=lambda **unused: copy.deepcopy(self.context),
            ),
            mock.patch.object(
                self.server,
                "_shader_workspace_service",
                return_value=self.workspaces,
            ),
            mock.patch.object(
                self.server,
                "rollback_shader_workspace_shared_resource",
                return_value=json.dumps(rollback),
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.old_active
        self.temporary.cleanup()

    def test_restore_uses_bridge_metadata_and_deterministic_workspace(self):
        result = json.loads(self.server.restore_current_shader("pixel"))
        self.assertNotIn("error", result, result)
        self.assertTrue(result["restored"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["replacementId"], self.replacement_id)
        self.assertFalse(result["policy"]["hostMemoryRequired"])
        query = self.ipc.calls[0]
        self.assertEqual(query[1]["expected_capture_id"], self.capture_id)
        self.assertEqual(
            query[1]["expected_capture_session_id"], self.session_id
        )

    def test_no_current_replacement_is_an_explicit_noop(self):
        self.replacements.clear()
        result = json.loads(self.server.restore_current_shader())
        self.assertFalse(result["restored"])
        self.assertFalse(result["applied"])
        self.assertIn("no bridge-owned", result["reason"])

    def test_workspace_identity_mismatch_blocks_rollback(self):
        self.replacements[0]["workspaceId"] = "7" * 32
        result = json.loads(self.server.restore_current_shader())
        self.assertFalse(result["restored"])
        self.assertIn("does not match", result["error"])


class ServerOutputFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def test_fingerprint_is_capture_session_bound_and_validated(self):
        session = "b" * 32
        calls = []

        class IPC:
            def call(self, method, params=None, timeout=None):
                calls.append((method, copy.deepcopy(params or {}), timeout))
                if method == "get_capture_status":
                    return {
                        "loaded": True,
                        "captureSessionId": session,
                        "capturePath": "D:/capture.rdc",
                    }
                if method == "fingerprint_event_outputs":
                    return {
                        "schema": "replaylab-event-output-fingerprint/v1",
                        "schemaVersion": 1,
                        "captureSessionId": session,
                        "eventId": 12892,
                        "gridSize": 2,
                        "outputs": [
                            {
                                "kind": "color",
                                "index": 0,
                                "resourceId": 7,
                                "textureByteSize": 16,
                                "fullHashByteLimit": 32 * 1024 * 1024,
                                "rawHashStatus": "hashed",
                                "rawByteSize": 16,
                                "rawSha256": "c" * 64,
                                "samples": [
                                    {"x": 0, "y": 0, "value": [0.0, 0.0, 0.0, 1.0]}
                                ],
                            }
                        ],
                        "observationalOnly": True,
                        "semanticEquivalenceVerified": False,
                    }
                raise AssertionError(method)

        previous = self.server.ipc
        self.server.ipc = IPC()
        try:
            result = json.loads(self.server.fingerprint_event_outputs(12892, 2))
        finally:
            self.server.ipc = previous
        self.assertNotIn("error", result, result)
        self.assertEqual(result["captureSessionId"], session)
        self.assertEqual(
            result["validationScope"],
            "single-captured-event-output-observation",
        )
        self.assertEqual(calls[1][1]["expected_capture_session_id"], session)
        self.assertFalse(result["semanticEquivalenceVerified"])


if __name__ == "__main__":
    unittest.main()
