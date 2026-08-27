from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import struct
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from src.active_projects import ActiveReplayProjectService
from src.shader_workspace import CatalogValidationError, select_unique_shader_for_use
from src.shader_workspace.catalog import ShaderKey, capture_identity
from src.shader_workspace.leases import WorkspaceSourceTransactionLease
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry


def make_dxbc(instructions: bytes = b"mcp shader instructions") -> bytes:
    body = instructions + b"\x00" * (-len(instructions) % 4)
    code = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(code)) + code
    header_size = 36
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", header_size + len(chunk))
        + struct.pack("<I", 1)
        + struct.pack("<I", header_size)
        + chunk
    )


FAKE_TOOL = r"""from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
if mode == "decompile":
    stage, entry = sys.argv[4], sys.argv[5]
    output_path.write_text(
        "// prepared %s/%s\nfloat4 %s() : SV_Target { return 1; }\n"
        % (stage, entry, entry or "main"),
        encoding="utf-8",
    )
elif mode == "compile":
    shutil.copyfile(Path(sys.argv[4]), output_path)
else:
    raise SystemExit(3)
"""


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self):
        return lambda function: function

    def run(self, **unused_kwargs):
        pass


def load_server_module():
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


class _CatalogBridge:
    def __init__(self, blob: bytes, key: ShaderKey):
        self.blob = blob
        self.key = key
        self.calls: list[tuple[str, dict]] = []
        self.disassembly = {"Native": "mov r0, r0\nret"}
        self.context: dict | None = None
        self.capture_session_id = "8" * 32
        self.known_catalog_capture_ids: set[str] = set()
        self.known_catalog_identity_pairs: dict[str, str] = {}
        self.export_capture_id_override: str | None = None
        self.all_draw_dispatch_event_ids = [321]
        self.export_use_event_id_override: int | None = None

    def call(self, method: str, params: dict, timeout: float | None = None):
        self.calls.append((method, dict(params)))
        if method == "get_current_renderdoc_context":
            if self.context is None:
                raise AssertionError("current context was not configured")
            return copy.deepcopy(self.context)
        if method == "reverse_shader":
            return {
                "shaderId": self.key.resource_id,
                "stage": params["stage"],
                "entryPoint": self.key.entry_point,
                "rawSha256": self.key.raw_sha256,
                "disassembly": dict(self.disassembly),
            }
        if method == "get_capture_status":
            return {
                "loaded": True,
                "api": "GraphicsAPI.D3D11",
                "captureId": (
                    sorted(self.known_catalog_capture_ids)[-1]
                    if self.known_catalog_capture_ids
                    else ""
                ),
                "capturePath": "D:/captures/frame.rdc",
                "captureSessionId": self.capture_session_id,
                "knownCatalogCaptureIds": sorted(
                    self.known_catalog_capture_ids
                ),
                "knownCatalogIdentityPairs": [
                    {
                        "captureId": capture_id,
                        "identityEvidenceSha256": (
                            self.known_catalog_identity_pairs[capture_id]
                        ),
                    }
                    for capture_id in sorted(self.known_catalog_capture_ids)
                    if capture_id in self.known_catalog_identity_pairs
                ],
            }
        if method != "export_shader_catalog":
            raise AssertionError("unexpected bridge call: %s" % method)
        output_dir = Path(params["output_dir"])
        requested_event_ids = params.get("event_ids")
        event_ids = list(
            requested_event_ids
            if requested_event_ids is not None
            else self.all_draw_dispatch_event_ids
        )
        blob_dir = output_dir / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        (blob_dir / (self.key.raw_sha256 + ".bin")).write_bytes(self.blob)
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": self.key.raw_sha256,
        }
        use = {
            "eventId": (
                self.export_use_event_id_override
                if self.export_use_event_id_override is not None
                else event_ids[0]
            ),
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entryPoint": self.key.entry_point,
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/frame.rdc",
            "api": "D3D11",
            "selection": {
                "mode": (
                    "explicit"
                    if requested_event_ids is not None
                    else "all_draw_dispatch"
                ),
                "eventIds": event_ids,
                "stages": list(params["stages"]),
            },
            "shaders": [shader],
            "uses": [use],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        identity_evidence = {
            "selection": copy.deepcopy(catalog["selection"]),
            "shaderKeys": [self.key.catalog_key],
            "useTuples": [[use["eventId"], self.key.catalog_key]],
        }
        identity_evidence_sha256 = hashlib.sha256(
            json.dumps(
                identity_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        expected_capture_id = str(params.get("expected_capture_id", "") or "")
        expected_evidence_sha256 = str(
            params.get("expected_identity_evidence_sha256", "") or ""
        )
        if expected_capture_id:
            if catalog["captureId"] != expected_capture_id:
                raise RuntimeError(
                    "Live Shader catalog did not match expected_capture_id"
                )
            if identity_evidence_sha256 != expected_evidence_sha256:
                raise RuntimeError(
                    "Live Shader catalog selection/Shader/use evidence changed"
                )
        catalog_path = output_dir / "shader_catalog.v1.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        returned_capture_id = (
            self.export_capture_id_override or catalog["captureId"]
        )
        if self.export_capture_id_override is None:
            self.known_catalog_capture_ids.add(catalog["captureId"])
            self.known_catalog_identity_pairs[catalog["captureId"]] = (
                identity_evidence_sha256
            )
        result = {
            "schema": catalog["schema"],
            "schemaVersion": 1,
            "captureId": returned_capture_id,
            "catalogPath": str(catalog_path),
            "counts": {"uniqueShaders": 1},
        }
        if expected_capture_id:
            result["identityEvidenceSha256"] = identity_evidence_sha256
            result["expectedIdentityValidatedBeforeCache"] = True
        return result


class ShaderWorkspaceMcpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server_module()

    def setUp(self) -> None:
        temp_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temp_root))
        self.root = Path(self.temporary.name)
        self.script = self.root / "fixed_shader_tool.py"
        self.script.write_text(FAKE_TOOL, encoding="utf-8")
        executable = Path(sys.executable).resolve()
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        script_hash = hashlib.sha256(self.script.read_bytes()).hexdigest()
        fixed_files = {str(self.script.resolve()): script_hash}
        self.config = self.root / "shader-tools.v1.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-tools/v1",
                    "schemaVersion": 1,
                    "tools": {
                        "test-decompiler": {
                            "executable": str(executable),
                            "executableSha256": executable_hash,
                            "fixedFileHashes": fixed_files,
                            "decompile": {
                                "args": [
                                    str(self.script.resolve()),
                                    "decompile",
                                    "{input}",
                                    "{output}",
                                    "{stage}",
                                    "{entry_point}",
                                ],
                                "timeoutSeconds": 10,
                            },
                        },
                        "test-compiler": {
                            "executable": str(executable),
                            "executableSha256": executable_hash,
                            "fixedFileHashes": fixed_files,
                            "compile": {
                                "args": [
                                    str(self.script.resolve()),
                                    "compile",
                                    "{input}",
                                    "{output}",
                                    "{baseline}",
                                    "{target_profile}",
                                ],
                                "timeoutSeconds": 10,
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        registry = ToolRegistry(self.config)
        self.toolchains_config = self.root / "shader-toolchains.v1.json"
        self.toolchains_config.write_text(
            json.dumps(
                toolchain_document(
                    self.config,
                    [
                        route_record(
                            registry,
                            route_id="test-dxbc-hlsl",
                            tool_id="test-decompiler",
                            operation="decompile",
                            input_format="dxbc",
                            output_format="hlsl",
                            stages=["fragment"],
                            fidelity="reconstructed-source",
                            priority=10,
                            implementation="test decompiler",
                            source_url="https://example.invalid/test-decompiler",
                            caveats=["Fixture source is reconstructed."],
                        ),
                        route_record(
                            registry,
                            route_id="test-hlsl-dxbc",
                            tool_id="test-compiler",
                            operation="compile",
                            input_format="hlsl",
                            output_format="dxbc",
                            stages=["fragment"],
                            fidelity="native-compile",
                            priority=10,
                            implementation="test compiler",
                            source_url="https://example.invalid/test-compiler",
                        ),
                    ],
                )
            ),
            encoding="utf-8",
        )
        self.config_binding = mock.patch.object(
            self.server,
            "_SHADER_TOOLS_CONFIG_PATH",
            self.config.resolve(),
        )
        self.config_binding.start()
        self.toolchains_config_binding = mock.patch.object(
            self.server,
            "_SHADER_TOOLCHAINS_CONFIG_PATH",
            self.toolchains_config.resolve(),
        )
        self.toolchains_config_binding.start()
        self.original_watch_manager = self.server._SHADER_WORKSPACE_WATCH_MANAGER
        self.server._SHADER_WORKSPACE_WATCH_MANAGER = None
        self.blob = make_dxbc()
        self.key = ShaderKey(
            77,
            "fragment",
            "main",
            hashlib.sha256(self.blob).hexdigest(),
        )
        self.bridge = _CatalogBridge(self.blob, self.key)
        self.original_ipc = self.server.ipc
        self.original_representation_root = self.server._SHADER_REPRESENTATIONS_ROOT
        self.server._SHADER_REPRESENTATIONS_ROOT = self.root / "representations"
        self.server.ipc = self.bridge

    def tearDown(self) -> None:
        manager = self.server._SHADER_WORKSPACE_WATCH_MANAGER
        if manager is not None:
            manager.shutdown()
        self.server._SHADER_WORKSPACE_WATCH_MANAGER = self.original_watch_manager
        self.server.ipc = self.original_ipc
        self.server._SHADER_REPRESENTATIONS_ROOT = self.original_representation_root
        self.toolchains_config_binding.stop()
        self.config_binding.stop()
        self.temporary.cleanup()

    def test_workspace_response_reports_checked_d3d12_dxil_shared_scope(self) -> None:
        response = json.loads(
            self.server._shader_workspace_response(
                "compile",
                {
                    "workspacePath": str(self.root / "dxil-workspace"),
                    "kind": "DXIL",
                    "requiresReplayAbiValidation": True,
                },
            )
        )
        replacement = response["renderDocReplacement"]
        self.assertTrue(replacement["supportedByTheseTools"])
        self.assertEqual(
            replacement["supportedCaptureScope"],
            {
                "apis": ["D3D11", "D3D12"],
                "containerKinds": ["DXBC", "DXIL"],
                "combinations": [
                    {"api": "D3D11", "containerKinds": ["DXBC"]},
                    {"api": "D3D12", "containerKinds": ["DXBC", "DXIL"]},
                ],
            },
        )
        # No capture API was supplied in this synthetic response, so capability is not guessed.
        self.assertEqual(replacement["candidateScopeStatus"], "not-evaluated")
        self.assertIn("D3D12", replacement["reason"])

    def test_prepare_edit_compile_status_never_applies_candidate(self) -> None:
        workspace = self.root / "workspace"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        self.assertEqual(prepared["operation"], "prepare")
        self.assertFalse(prepared["candidateState"]["present"])
        self.assertFalse(prepared["renderDocReplacement"]["attempted"])
        self.assertEqual(
            self.bridge.calls,
            [
                (
                    "export_shader_catalog",
                    {
                        "output_dir": self.bridge.calls[0][1]["output_dir"],
                        "event_ids": [321],
                        "stages": ["fragment"],
                    },
                )
            ],
        )
        source = workspace / "source" / "shader.hlsl"
        self.assertIn("prepared fragment/main", source.read_text(encoding="utf-8"))
        source.write_text(
            source.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8"
        )

        compiled = json.loads(self.server.compile_shader_workspace(str(workspace)))
        self.assertNotIn("error", compiled)
        self.assertTrue(compiled["candidateState"]["present"])
        self.assertTrue(
            compiled["candidateState"]["requiresReplayAbiValidation"]
        )
        self.assertFalse(compiled["candidateState"]["replayAbiValidated"])
        self.assertFalse(compiled["candidateState"]["applied"])
        self.assertTrue(compiled["renderDocReplacement"]["supportedByTheseTools"])
        self.assertFalse(
            compiled["renderDocReplacement"]["supportedByThisOperation"]
        )
        self.assertEqual(len(self.bridge.calls), 1)

        lock_path = workspace / ".workspace-operation.lock"
        lock_path.write_text("another-process-token\n", encoding="ascii")
        status = json.loads(self.server.get_shader_workspace_status(str(workspace)))
        self.assertNotIn("error", status)
        self.assertTrue(status["result"]["source"]["modifiedSinceDecompile"])
        self.assertTrue(status["candidateState"]["present"])
        self.assertTrue(status["candidateState"]["requiresReplayAbiValidation"])
        self.assertTrue(status["result"]["workspaceLocked"])
        self.assertEqual(
            lock_path.read_text(encoding="ascii"), "another-process-token\n"
        )
        self.assertFalse(status["result"]["applied"])

    def test_restart_rebinds_exact_workspace_catalog_without_new_workspace(self) -> None:
        workspace = self.root / "workspace-rebind"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        capture_id = prepared["result"]["created"]["captureId"]
        manifest_before = (workspace / "manifest.json").read_bytes()
        self.bridge.known_catalog_capture_ids.clear()
        self.bridge.calls.clear()

        rebound = self.server._ensure_workspace_capture_catalog_identity(
            str(workspace)
        )
        self.assertTrue(rebound["rebound"])
        self.assertEqual(rebound["state"], "rebound-from-workspace-catalog")
        self.assertEqual(rebound["captureId"], capture_id)
        self.assertEqual(rebound["selectedEventId"], 321)
        self.assertEqual(rebound["stage"], "fragment")
        self.assertFalse(rebound["workspaceCreated"])
        self.assertFalse(rebound["candidateCompiled"])
        self.assertFalse(rebound["candidateApplied"])
        self.assertFalse(rebound["previousCandidateRetried"])
        self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
        export_calls = [
            call for call in self.bridge.calls
            if call[0] == "export_shader_catalog"
        ]
        self.assertEqual(len(export_calls), 1)
        self.assertEqual(export_calls[0][1]["event_ids"], [321])
        self.assertEqual(export_calls[0][1]["stages"], ["fragment"])
        self.assertIn(capture_id, self.bridge.known_catalog_capture_ids)
        self.assertEqual(
            list(self.server._SHADER_REPRESENTATIONS_ROOT.glob("_catalog-rebind-*")),
            [],
        )

        self.bridge.calls.clear()
        already_known = self.server._ensure_workspace_capture_catalog_identity(
            str(workspace)
        )
        self.assertFalse(already_known["rebound"])
        self.assertEqual(already_known["state"], "already-known")
        self.assertEqual(
            [call[0] for call in self.bridge.calls], ["get_capture_status"]
        )

    def test_restart_fast_path_requires_exact_catalog_evidence_pair(self) -> None:
        workspace = self.root / "workspace-rebind-exact-pair"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        capture_id = prepared["result"]["created"]["captureId"]
        workspace_evidence_sha256 = self.bridge.known_catalog_identity_pairs[
            capture_id
        ]
        different_evidence = {
            "selection": {
                "mode": "explicit",
                "eventIds": [321, 322],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.key.catalog_key],
            "useTuples": [[322, self.key.catalog_key]],
        }
        different_evidence_sha256 = hashlib.sha256(
            json.dumps(
                different_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(
            different_evidence_sha256, workspace_evidence_sha256
        )
        # The live Bridge knows the same captureId, but only for a different
        # exact selection/use binding. That is not a valid fast-path hit.
        self.bridge.known_catalog_identity_pairs[capture_id] = (
            different_evidence_sha256
        )
        self.bridge.calls.clear()

        rebound = self.server._ensure_workspace_capture_catalog_identity(
            str(workspace)
        )

        self.assertTrue(rebound["rebound"])
        self.assertEqual(rebound["state"], "rebound-from-workspace-catalog")
        export_calls = [
            params
            for method, params in self.bridge.calls
            if method == "export_shader_catalog"
        ]
        self.assertEqual(len(export_calls), 1)
        self.assertEqual(
            export_calls[0]["expected_capture_id"], capture_id
        )
        self.assertEqual(
            export_calls[0]["expected_identity_evidence_sha256"],
            workspace_evidence_sha256,
        )
        self.assertEqual(
            self.bridge.known_catalog_identity_pairs[capture_id],
            workspace_evidence_sha256,
        )

    def test_failed_expected_reexport_does_not_enable_next_fast_path(self) -> None:
        workspace = self.root / "workspace-rebind-failed-expected"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        capture_id = prepared["result"]["created"]["captureId"]
        expected_evidence_sha256 = self.bridge.known_catalog_identity_pairs[
            capture_id
        ]
        stale_evidence_sha256 = "f" * 64
        self.assertNotEqual(stale_evidence_sha256, expected_evidence_sha256)
        self.bridge.known_catalog_identity_pairs[capture_id] = (
            stale_evidence_sha256
        )
        self.bridge.export_use_event_id_override = 322
        self.bridge.calls.clear()

        with self.assertRaisesRegex(
            RuntimeError, "selection/Shader/use evidence changed"
        ):
            self.server._ensure_workspace_capture_catalog_identity(str(workspace))
        self.assertEqual(
            self.bridge.known_catalog_identity_pairs[capture_id],
            stale_evidence_sha256,
        )

        self.bridge.export_use_event_id_override = None
        rebound = self.server._ensure_workspace_capture_catalog_identity(
            str(workspace)
        )

        self.assertTrue(rebound["rebound"])
        self.assertEqual(
            len(
                [
                    call
                    for call in self.bridge.calls
                    if call[0] == "export_shader_catalog"
                ]
            ),
            2,
        )
        self.assertEqual(
            self.bridge.known_catalog_identity_pairs[capture_id],
            expected_evidence_sha256,
        )

    def test_restart_rebind_rejects_hashless_workspace_catalog(self) -> None:
        workspace = self.root / "workspace-rebind-hashless"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        status = self.server._shader_workspace_service().status(str(workspace))
        status["capture"].pop("catalogSha256")
        self.bridge.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "catalogSha256"):
            self.server._ensure_workspace_capture_catalog_identity(
                str(workspace), status=status
            )

        self.assertEqual(self.bridge.calls, [])

    def test_restart_rebind_rejects_a_different_live_catalog_identity(self) -> None:
        workspace = self.root / "workspace-rebind-mismatch"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                321,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        manifest_before = (workspace / "manifest.json").read_bytes()
        self.bridge.known_catalog_capture_ids.clear()
        self.bridge.export_capture_id_override = "f" * 64

        with self.assertRaisesRegex(
            RuntimeError, "did not reproduce the workspace captureId"
        ):
            self.server._ensure_workspace_capture_catalog_identity(str(workspace))
        self.assertEqual(self.bridge.known_catalog_capture_ids, set())
        self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(
            list(self.server._SHADER_REPRESENTATIONS_ROOT.glob("_catalog-rebind-*")),
            [],
        )

    def test_restart_rebind_replays_all_draw_dispatch_selection_exactly(self) -> None:
        catalog_dir = self.root / "all-draw-dispatch-catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "stages": ["fragment"],
            },
        )
        workspace = self.root / "workspace-rebind-all"
        created = json.loads(
            self.server.create_shader_workspace(
                exported["catalogPath"],
                self.key.catalog_key,
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", created)
        self.bridge.known_catalog_capture_ids.clear()
        self.bridge.calls.clear()

        rebound = self.server._ensure_workspace_capture_catalog_identity(
            str(workspace)
        )
        self.assertTrue(rebound["rebound"])
        export_calls = [
            call for call in self.bridge.calls
            if call[0] == "export_shader_catalog"
        ]
        self.assertEqual(len(export_calls), 1)
        self.assertNotIn("event_ids", export_calls[0][1])
        self.assertEqual(export_calls[0][1]["stages"], ["fragment"])

    def test_restart_rebind_rejects_changed_event_shader_use_tuple(self) -> None:
        catalog_dir = self.root / "multi-event-catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
            },
        )
        workspace = self.root / "workspace-rebind-use-change"
        created = json.loads(
            self.server.create_shader_workspace(
                exported["catalogPath"],
                self.key.catalog_key,
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", created)
        self.bridge.known_catalog_capture_ids.clear()
        self.bridge.export_use_event_id_override = 322

        with self.assertRaisesRegex(
            RuntimeError, "selection/Shader/use evidence changed"
        ):
            self.server._ensure_workspace_capture_catalog_identity(str(workspace))
        self.assertEqual(
            list(self.server._SHADER_REPRESENTATIONS_ROOT.glob("_catalog-rebind-*")),
            [],
        )

    def test_fresh_validation_rebinds_once_before_first_reflection_attempt(self) -> None:
        capture_id = "c" * 64
        candidate_hash = "d" * 64
        source_hash = "1" * 64
        request_id = "e" * 64
        status = {
            "workspaceId": "b" * 32,
            "capture": {"captureId": capture_id},
            "revision": 4,
            "source": {"currentSha256": source_hash},
            "representation": {
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "latestCompiled": {
                "revision": 4,
                "sourceSha256": source_hash,
                "rawSha256": candidate_hash,
            },
        }
        request = {
            "requestId": request_id,
            "binding": {
                "workspaceId": "b" * 32,
                "captureId": capture_id,
                "revision": 4,
                "candidateRawSha256": candidate_hash,
            },
            "params": {"candidate_path": "fixed.bin"},
        }
        order = []

        class Service:
            def status(inner_self, _workspace):
                return copy.deepcopy(status)

            def candidate_reflection_request(inner_self, _workspace):
                return copy.deepcopy(request)

            def validate_candidate_reflection(inner_self, _workspace, _response):
                return {"requestId": request_id, "replayAbiValidated": True}

        class Bridge:
            def call(inner_self, method, params, timeout=None):
                self.assertEqual(method, "reflect_shader_candidate")
                self.assertEqual(params["expected_capture_id"], capture_id)
                order.append("reflect")
                return {
                    "captureId": capture_id,
                    "captureSessionId": "8" * 32,
                }

        def ensure(_workspace, *, status=None):
            self.assertIsNotNone(status)
            order.append("rebind")
            return {"rebound": True, "previousCandidateRetried": False}

        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=Service()
        ), mock.patch.object(self.server, "ipc", Bridge()), mock.patch.object(
            self.server,
            "_ensure_workspace_capture_catalog_identity",
            side_effect=ensure,
        ) as rebind:
            unused_service, after, reflected_request, validated = (
                self.server._fresh_shader_candidate_validation(
                    "fixed-workspace", 4, candidate_hash
                )
            )
        self.assertEqual(order, ["rebind", "reflect"])
        self.assertEqual(rebind.call_count, 1)
        self.assertEqual(after["revision"], 4)
        self.assertEqual(reflected_request, request)
        self.assertTrue(validated["replayAbiValidated"])

    def test_rebind_identity_keeps_hashless_nonselected_shader_facts(self) -> None:
        hashless_key = "88:vertex:main:no-raw-bytes"
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "selection": {
                "mode": "explicit",
                "eventIds": [321],
                "stages": ["vertex", "fragment"],
            },
            "shaders": [
                {
                    "shaderKey": hashless_key,
                    "resourceId": 88,
                    "stage": "vertex",
                    "entryPoint": "main",
                    "rawSha256": None,
                },
                {
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                },
            ],
            "uses": [
                {
                    "eventId": 321,
                    "shaderKey": hashless_key,
                    "resourceId": 88,
                    "stage": "vertex",
                    "entryPoint": "main",
                    "rawSha256": None,
                },
                {
                    "eventId": 321,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                },
            ],
        }
        evidence = self.server._catalog_rebind_identity_evidence(
            catalog, "fixture catalog"
        )
        self.assertEqual(
            evidence["shaderKeys"],
            sorted([hashless_key, self.key.catalog_key]),
        )
        self.assertEqual(len(evidence["useTuples"]), 2)

    def test_toolchain_migration_mcp_preserves_source_and_invalidates_candidate(
        self,
    ) -> None:
        catalog_dir = self.root / "migration-catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "event_ids": [321],
                "stages": ["fragment"],
            },
        )
        source_workspace = self.root / "workspace-before-migration"
        created = json.loads(
            self.server.create_shader_workspace(
                exported["catalogPath"],
                self.key.catalog_key,
                str(source_workspace),
            )
        )
        self.assertNotIn("error", created)
        decompiled = json.loads(
            self.server.decompile_shader_workspace(str(source_workspace))
        )
        self.assertNotIn("error", decompiled)
        source_path = Path(decompiled["result"]["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// exact migration edit\n")
        compiled = json.loads(
            self.server.compile_shader_workspace(str(source_workspace))
        )
        self.assertNotIn("error", compiled)
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest = (source_workspace / "manifest.json").read_bytes()

        upgraded_catalog = json.loads(
            self.toolchains_config.read_text(encoding="utf-8")
        )
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        self.toolchains_config.write_text(
            json.dumps(upgraded_catalog), encoding="utf-8"
        )

        target_workspace = self.root / "workspace-after-migration"
        previous_host_lease = WorkspaceSourceTransactionLease(
            source_workspace, owner_instance_id="1" * 32
        )
        previous_host_lease.acquire()
        previous_lease = json.loads(
            (source_workspace / ".workspace-watch.lock").read_text(encoding="utf-8")
        )
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("REPLAYLAB_HOST_INSTANCE_ID", None)
            blocked = json.loads(
                self.server.migrate_shader_workspace_toolchain(
                    str(source_workspace),
                    str(target_workspace),
                    created["result"]["routePlan"]["planSha256"],
                    source_hash,
                )
            )
        self.assertEqual(blocked["type"], "WorkspaceSourceLeaseError")
        self.assertFalse(target_workspace.exists())
        self.assertEqual(
            json.loads(
                (source_workspace / ".workspace-watch.lock").read_text(
                    encoding="utf-8"
                )
            ),
            previous_lease,
        )
        with mock.patch.dict(
            os.environ, {"REPLAYLAB_HOST_INSTANCE_ID": "2" * 32}
        ):
            migrated = json.loads(
                self.server.migrate_shader_workspace_toolchain(
                    str(source_workspace),
                    str(target_workspace),
                    created["result"]["routePlan"]["planSha256"],
                    source_hash,
                )
            )
        self.assertNotIn("error", migrated)
        self.assertEqual(migrated["operation"], "toolchain-migrate")
        self.assertFalse(migrated["candidateState"]["present"])
        self.assertFalse(migrated["result"]["latestCandidateImported"])
        self.assertFalse(migrated["result"]["semanticEquivalenceVerified"])
        self.assertEqual(
            (source_workspace / "manifest.json").read_bytes(), source_manifest
        )
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            (target_workspace / "source" / "shader.hlsl").read_bytes(),
            source_bytes,
        )
        self.assertFalse((source_workspace / ".workspace-watch.lock").exists())
        archived_lease = source_workspace / "watch" / (
            "orphaned-lease-%s-%s.json"
            % (previous_lease["ownerInstanceId"], previous_lease["token"])
        )
        self.assertEqual(
            json.loads(archived_lease.read_text(encoding="utf-8")),
            previous_lease,
        )

        status = json.loads(
            self.server.get_shader_workspace_status(str(target_workspace))
        )
        self.assertNotIn("error", status)
        self.assertEqual(status["result"]["revision"], 0)
        self.assertIsNone(status["result"]["latestCompiled"])
        migration = status["result"]["source"]["toolchainMigration"]
        self.assertFalse(migration["sourceCandidatesImported"])
        self.assertFalse(migration["semanticEquivalenceVerified"])
        self.assertEqual(
            migration["invalidatedSourceCandidate"]["declaredRevision"], 1
        )

    def test_active_full_catalog_identity_survives_workspace_prepare(self) -> None:
        second_blob = make_dxbc(b"second shader")
        second_key = ShaderKey(
            88,
            "vertex",
            "main",
            hashlib.sha256(second_blob).hexdigest(),
        )
        full_export = self.root / "full-export"
        blobs = full_export / "blobs"
        blobs.mkdir(parents=True)
        (blobs / (self.key.raw_sha256 + ".bin")).write_bytes(self.blob)
        (blobs / (second_key.raw_sha256 + ".bin")).write_bytes(second_blob)

        def shader_record(key: ShaderKey, blob: bytes) -> dict:
            return {
                "shaderKey": key.catalog_key,
                "resourceId": key.resource_id,
                "shaderResourceId": key.resource_id,
                "stage": key.stage,
                "entryPoint": key.entry_point,
                "rawPath": f"blobs/{key.raw_sha256}.bin",
                "rawSize": len(blob),
                "rawSha256": key.raw_sha256,
            }

        full_catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/frame.rdc",
            "api": "D3D11",
            "shaders": [
                shader_record(self.key, self.blob),
                shader_record(second_key, second_blob),
            ],
            "uses": [
                {
                    "eventId": 321,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                },
                {
                    "eventId": 322,
                    "shaderKey": second_key.catalog_key,
                    "resourceId": second_key.resource_id,
                    "stage": second_key.stage,
                    "entryPoint": second_key.entry_point,
                    "rawSha256": second_key.raw_sha256,
                },
            ],
        }
        full_id = capture_identity(full_catalog, [self.key, second_key])
        subset_id = capture_identity(full_catalog, [self.key])
        self.assertNotEqual(full_id, subset_id)
        full_catalog["captureId"] = full_id

        active = ActiveReplayProjectService(
            self.root / "active-config" / "selection.json",
            self.root / "active-projects",
        )
        active.ensure_pending("Catalog identity integration")
        self.bridge.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureSessionId": "a" * 32,
                "captureId": "",
                "path": "D:/captures/frame.rdc",
                "api": "D3D11",
                "local": True,
            },
            "events": {"currentEventId": 321, "selectedEventId": 321},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": self.key.resource_id,
                        "entryPoint": self.key.entry_point,
                        "rawSha256": self.key.raw_sha256,
                    }
                ]
            },
        }
        original_bridge_call = self.bridge.call

        def bridge_call(method: str, params: dict, timeout=None):
            if method != "export_shader_catalog" or params.get("event_ids") != []:
                return original_bridge_call(method, params, timeout)
            self.bridge.calls.append((method, dict(params)))
            output = Path(params["output_dir"])
            output_blobs = output / "blobs"
            output_blobs.mkdir(parents=True, exist_ok=True)
            (output_blobs / (self.key.raw_sha256 + ".bin")).write_bytes(self.blob)
            (output_blobs / (second_key.raw_sha256 + ".bin")).write_bytes(second_blob)
            path = output / "shader_catalog.v1.json"
            path.write_text(json.dumps(full_catalog), encoding="utf-8")
            return {
                "schema": full_catalog["schema"],
                "schemaVersion": 1,
                "captureId": full_id,
                "catalogPath": str(path),
                "counts": {"uniqueShaders": 2},
            }

        old_active = self.server._ACTIVE_PROJECT_SERVICE
        self.server._ACTIVE_PROJECT_SERVICE = active
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        try:
            with mock.patch.object(self.bridge, "call", side_effect=bridge_call):
                active_prepared = json.loads(
                    self.server.prepare_active_capture_project()
                )
                self.assertNotIn("error", active_prepared, active_prepared)
                self.assertEqual(active_prepared["capture"]["captureId"], full_id)
                path_result = json.loads(
                    self.server.get_active_shader_workspace_path(321, "fragment")
                )
                self.assertNotIn("error", path_result, path_result)
                self.assertEqual(path_result["captureId"], full_id)
                self.assertEqual(
                    path_result["catalogSha256"],
                    active_prepared["stagedCatalog"]["catalogSha256"],
                )
                workspace = Path(path_result["workspacePath"])
                self.assertFalse(workspace.exists())
                self.assertEqual(
                    path_result["workspaceLayout"], "content-addressed-v2"
                )
                self.assertEqual(workspace.parent.name, "v2")
                self.assertEqual(
                    workspace.name, path_result["workspaceBindingSha256"]
                )
                exports_before = len(
                    [
                        call
                        for call in self.bridge.calls
                        if call[0] == "export_shader_catalog"
                    ]
                )

                bypass = json.loads(
                    self.server.prepare_shader_workspace(
                        321,
                        "fragment",
                        str(workspace),
                        "test-decompiler",
                        "test-compiler",
                        str(self.root / "explicit-subset"),
                    )
                )
                self.assertIn("cannot bypass", bypass["error"])
                self.assertFalse(workspace.exists())

                race_workspace = workspace.with_name(workspace.name + "-binding-race")
                workspace_service = self.server._shader_workspace_service()
                original_create = workspace_service.create
                with self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
                    original_binding = copy.deepcopy(
                        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS["a" * 32]
                    )

                def create_then_replace_binding(*args, **kwargs):
                    result = original_create(*args, **kwargs)
                    replacement = copy.deepcopy(original_binding)
                    replacement["captureId"] = "e" * 64
                    with self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
                        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS["a" * 32] = (
                            replacement
                        )
                    return result

                try:
                    with mock.patch.object(
                        self.server,
                        "_shader_workspace_service",
                        return_value=workspace_service,
                    ), mock.patch.object(
                        workspace_service,
                        "create",
                        side_effect=create_then_replace_binding,
                    ):
                        raced = json.loads(
                            self.server.prepare_shader_workspace(
                                321,
                                "fragment",
                                str(race_workspace),
                                "test-decompiler",
                                "test-compiler",
                            )
                        )
                    self.assertIn("binding changed", raced["error"])
                finally:
                    with self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
                        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS["a" * 32] = (
                            original_binding
                        )

                prepared = json.loads(
                    self.server.prepare_shader_workspace(
                        321,
                        "fragment",
                        str(workspace),
                        "test-decompiler",
                        "test-compiler",
                    )
                )
                self.assertNotIn("error", prepared, prepared)
                self.assertTrue(
                    prepared["result"]["catalogExport"][
                        "reusedActiveProjectCatalog"
                    ]
                )
                status = json.loads(
                    self.server.get_shader_workspace_status(str(workspace))
                )["result"]
                self.assertEqual(status["capture"]["captureId"], full_id)
                self.assertEqual(
                    status["capture"]["catalogSha256"],
                    active_prepared["stagedCatalog"]["catalogSha256"],
                )
                self.assertEqual(
                    len(
                        [
                            call
                            for call in self.bridge.calls
                            if call[0] == "export_shader_catalog"
                        ]
                    ),
                    exports_before,
                )
        finally:
            self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
            self.server._ACTIVE_PROJECT_SERVICE = old_active

    def test_current_event_representation_covers_non_workspace_shader_formats(self) -> None:
        represented = json.loads(
            self.server.get_current_shader_representation(777, "fragment")
        )
        self.assertNotIn("error", represented)
        self.assertEqual(represented["lane"], "explanatory-ir")
        self.assertFalse(represented["compileEligible"])
        self.assertFalse(represented["applyEligible"])
        self.assertFalse(represented["abiCompatible"])
        self.assertFalse(represented["semanticEquivalenceVerified"])
        self.assertTrue(represented["rawBytesAvailable"])
        self.assertEqual(Path(represented["rawBinaryPath"]).read_bytes(), self.blob)
        artifact = Path(represented["path"])
        self.assertTrue(artifact.is_file())
        self.assertGreater(artifact.stat().st_size, 0)
        self.assertIn("mov r0, r0", artifact.read_text(encoding="utf-8"))
        self.assertTrue(
            artifact.resolve().is_relative_to(
                self.server._SHADER_REPRESENTATIONS_ROOT.resolve()
            )
        )
        self.assertEqual(
            list(self.server._SHADER_REPRESENTATIONS_ROOT.glob("_catalog-*")), []
        )

        self.bridge.disassembly = {}
        binary_only = json.loads(
            self.server.get_current_shader_representation(778, "fragment")
        )
        self.assertNotIn("error", binary_only)
        self.assertEqual(binary_only["lane"], "binary-only")
        self.assertGreater(Path(binary_only["path"]).stat().st_size, 0)
        self.assertTrue(
            any(
                item["code"] == "no-textual-representation"
                for item in binary_only["diagnostics"]
            )
        )
        self.assertEqual(
            list(self.server._SHADER_REPRESENTATIONS_ROOT.glob("_catalog-*")), []
        )

    def test_prepare_without_tool_ids_uses_server_owned_route_catalog(self) -> None:
        workspace = self.root / "workspace-auto-route"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                322,
                "fragment",
                str(workspace),
            )
        )
        self.assertNotIn("error", prepared)
        plan = prepared["result"]["created"]["routePlan"]
        self.assertEqual(
            [item["capability"]["routeId"] for item in plan["decompile"]],
            ["test-dxbc-hlsl"],
        )
        self.assertEqual(
            [item["capability"]["routeId"] for item in plan["compile"]],
            ["test-hlsl-dxbc"],
        )
        self.assertEqual(
            prepared["result"]["decompiled"]["routeIds"],
            ["test-dxbc-hlsl"],
        )

    def test_toolchain_mcp_plans_and_smokes_without_caller_commands(self) -> None:
        shader_path = self.root / "captured.dxbc"
        shader_path.write_bytes(self.blob)

        capabilities = json.loads(
            self.server.get_shader_toolchain_capabilities("fragment")
        )
        self.assertNotIn("error", capabilities)
        self.assertTrue(
            any(row["status"] == "ready" for row in capabilities["matrix"])
        )

        planned = json.loads(
            self.server.plan_shader_toolchain_route(
                str(shader_path),
                "fragment",
                target_api="d3d11",
            )
        )
        self.assertNotIn("error", planned)
        self.assertEqual(planned["decompileRouteIds"], ["test-dxbc-hlsl"])
        self.assertEqual(planned["compileRouteIds"], ["test-hlsl-dxbc"])
        self.assertEqual(planned["targetProfile"]["profile"], "ps_5_0")

        smoke = json.loads(
            self.server.run_shader_toolchain_round_trip_smoke(
                str(shader_path),
                str(self.root / "smoke"),
                "fragment",
                "hlsl",
            )
        )
        self.assertIsNone(smoke["error"])
        self.assertTrue(smoke["formatSmokePassed"])
        self.assertFalse(smoke["semanticEquivalenceVerified"])
        self.assertFalse(smoke["replayAbiValidated"])
        self.assertEqual(smoke["targetProfileDetails"]["profile"], "ps_5_0")

    def test_save_watch_compiles_changed_source_without_renderdoc_apply(self) -> None:
        workspace = self.root / "watched-workspace"
        prepared = json.loads(
            self.server.prepare_shader_workspace(
                322,
                "fragment",
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", prepared)
        started = json.loads(
            self.server.start_shader_workspace_watch(str(workspace), 100)
        )
        self.assertNotIn("error", started)
        watch_id = started["result"]["watchId"]
        source = workspace / "source" / "shader.hlsl"
        edited = source.read_text(encoding="utf-8") + "// saved from IDE\n"
        source.write_text(edited, encoding="utf-8")
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        deadline = time.monotonic() + 5.0
        polled = None
        while time.monotonic() < deadline:
            polled = json.loads(
                self.server.poll_shader_workspace_watch(watch_id)
            )
            if polled["result"]["caughtUp"]:
                break
            time.sleep(0.02)
        self.assertIsNotNone(polled)
        self.assertTrue(polled["result"]["caughtUp"], polled)
        self.assertEqual(
            polled["result"]["lastCompiledSourceSha256"], expected_hash
        )
        self.assertEqual(polled["result"]["latestCandidateRevision"], 1)
        self.assertFalse(polled["result"]["candidatePolicy"]["applied"])
        self.assertFalse(polled["renderDocReplacement"]["attempted"])
        self.assertEqual(len(self.bridge.calls), 1)
        revision_source = workspace / "revisions" / "0001" / "shader.hlsl"
        self.assertEqual(revision_source.read_text(encoding="utf-8"), edited)
        audit = json.loads(
            Path(polled["result"]["auditPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(audit["lastCompiledSourceSha256"], expected_hash)

        stopped = json.loads(self.server.stop_shader_workspace_watch(watch_id))
        self.assertEqual(stopped["result"]["state"], "stopped")
        invalid = json.loads(
            self.server.start_shader_workspace_watch(str(workspace), 50)
        )
        self.assertEqual(invalid["type"], "WorkspaceWatchError")

    def test_abi_validate_apply_and_rollback_use_capture_bound_rpc_chain(self) -> None:
        capture_id = "c" * 64
        candidate_hash = "d" * 64
        baseline_hash = "a" * 64
        request_id = "e" * 64
        report_hash = "f" * 64
        workspace_id = "b" * 32
        shader_key = "77:fragment:main:%s" % baseline_hash
        candidate_path = str((self.root / "trusted-candidate.dxbc").resolve())
        request = {
            "schema": "renderdoc-ultra-shader-reflection-request/v1",
            "schemaVersion": 1,
            "requestId": request_id,
            "method": "reflect_shader_candidate",
            "binding": {
                "workspaceId": workspace_id,
                "captureId": capture_id,
                "revision": 2,
                "candidateRawSha256": candidate_hash,
                "candidateByteSize": 128,
                "baselineAbiSha256": "9" * 64,
                "stage": "fragment",
                "entryPoint": "main",
                "api": "d3d11",
                "encoding": "dxbc",
                "targetProfile": "ps_5_0",
            },
            "params": {
                "candidate_path": candidate_path,
                "expected_sha256": candidate_hash,
                "stage": "fragment",
                "entry_point": "main",
                "encoding": "dxbc",
                "target_profile": "ps_5_0",
            },
            "requiredReplaySequence": [],
        }
        status = {
            "workspacePath": str((self.root / "workspace").resolve()),
            "workspaceId": workspace_id,
            "capture": {
                "captureId": capture_id,
                "catalogPath": str(self.root / "catalog.json"),
                "capturePath": "D:/captures/frame.rdc",
                "api": "D3D11",
            },
            "shader": {
                "shaderKey": shader_key,
                "resourceId": 77,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": baseline_hash,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "revision": 2,
            "source": {
                "present": True,
                "currentSha256": "1" * 64,
                "lane": "executable-editable",
            },
            "representation": {
                "present": True,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "baseline": {"rawSha256": baseline_hash, "kind": "DXBC"},
            "latestCompiled": {
                "revision": 2,
                "sourceSha256": "1" * 64,
                "rawSha256": candidate_hash,
                "abiGate": {
                    "status": "compatible",
                    "requestId": request_id,
                    "reportSha256": report_hash,
                },
            },
        }
        validation = {
            "revision": 2,
            "requestId": request_id,
            "compatible": True,
            "requiresReplayAbiValidation": False,
            "replayAbiValidated": True,
            "abiReportPath": str(self.root / "abi-report.json"),
            "abiReportSha256": report_hash,
            "applied": False,
        }

        class FakeService:
            def __init__(inner_self):
                inner_self.calls = []

            def status(inner_self, workspace_path):
                inner_self.calls.append("status")
                return json.loads(json.dumps(status))

            def candidate_reflection_request(inner_self, workspace_path):
                inner_self.calls.append("request")
                return json.loads(json.dumps(request))

            def validate_candidate_reflection(inner_self, workspace_path, response):
                inner_self.calls.append("validate")
                self.assertEqual(response["captureId"], capture_id)
                return dict(validation)

        class ApplyBridge:
            def __init__(inner_self):
                inner_self.calls = []
                inner_self.change_source_during_shared_apply = False

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, dict(params), timeout))
                if method == "reflect_shader_candidate":
                    self.assertEqual(params["expected_capture_id"], capture_id)
                    return {
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                    }
                if method == "apply_shader_candidate_override":
                    self.assertEqual(params["event_id"], 321)
                    self.assertEqual(params["expected_original_sha256"], baseline_hash)
                    return {
                        "schema": "renderdoc-ultra-event-shader-override/v1",
                        "schemaVersion": 1,
                        "overrideId": "1" * 32,
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "workspaceId": workspace_id,
                        "revision": 2,
                        "eventId": 321,
                        "stage": "fragment",
                        "entryPoint": "main",
                        "originalResourceId": 77,
                        "originalRawSha256": baseline_hash,
                        "candidateRawSha256": candidate_hash,
                        "targetResourceId": 99,
                        "shaderFamilyId": "5" * 64,
                        "targetReferenceCount": 1,
                        "idempotent": False,
                        "abiRequestId": request_id,
                        "abiReportSha256": report_hash,
                        "eventScoped": True,
                        "configured": True,
                        "appliedToCurrentEvent": True,
                        "originalShaderPreserved": True,
                        "supersededOverrideId": None,
                        "supersededTargetFreed": True,
                    }
                if method == "apply_shader_candidate_overrides":
                    self.assertEqual(
                        [item["event_id"] for item in params["events"]],
                        [321, 322],
                    )
                    overrides = []
                    for index, event_id in enumerate((321, 322), start=2):
                        overrides.append(
                            {
                                "schema": "renderdoc-ultra-event-shader-override/v1",
                                "schemaVersion": 1,
                                "overrideId": str(index) * 32,
                                "captureId": capture_id,
                                "captureSessionId": "8" * 32,
                                "workspaceId": workspace_id,
                                "revision": 2,
                                "eventId": event_id,
                                "stage": "fragment",
                                "entryPoint": "main",
                                "originalResourceId": 77,
                                "originalRawSha256": baseline_hash,
                                "candidateRawSha256": candidate_hash,
                                "targetResourceId": 100,
                                "shaderFamilyId": "6" * 64,
                                "targetReferenceCount": 2,
                                "idempotent": False,
                                "abiRequestId": request_id,
                                "abiReportSha256": report_hash,
                                "eventScoped": True,
                                "configured": True,
                                "appliedToCurrentEvent": True,
                                "originalShaderPreserved": True,
                                "supersededOverrideId": None,
                                "supersededTargetFreed": True,
                            }
                        )
                    return {
                        "schema": "renderdoc-ultra-event-shader-override-batch/v1",
                        "schemaVersion": 1,
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "shaderFamilyId": "6" * 64,
                        "candidateRawSha256": candidate_hash,
                        "targetResourceId": 100,
                        "transactional": True,
                        "originalShadersPreserved": True,
                        "overrides": overrides,
                    }
                if method == "apply_shader_candidate_shared_resource":
                    self.assertEqual(params["event_id"], 321)
                    self.assertEqual(params["expected_original_resource_id"], 77)
                    if inner_self.change_source_during_shared_apply:
                        status["source"]["currentSha256"] = "2" * 64
                    return {
                        "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                        "schemaVersion": 1,
                        "replacementId": "7" * 32,
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "workspaceId": workspace_id,
                        "revision": 2,
                        "anchorEventId": 321,
                        "stage": "fragment",
                        "entryPoint": "main",
                        "api": "d3d11",
                        "encoding": "dxbc",
                        "originalResourceId": 77,
                        "originalRawSha256": baseline_hash,
                        "candidateRawSha256": candidate_hash,
                        "physicalAbiSha256": "9" * 64,
                        "shaderFamilyId": "7" * 64,
                        "targetResourceId": 101,
                        "abiRequestId": request_id,
                        "abiReportSha256": report_hash,
                        "resourceScoped": True,
                        "eventScoped": False,
                        "allOriginalResourceUsesAffected": True,
                        "configured": True,
                        "originalShaderPreserved": True,
                        "singleOwner": True,
                        "checkedDerivedPipelineRebuild": False,
                        "capturedRootSignaturesReused": False,
                        "idempotent": False,
                        "metadataOnly": False,
                        "targetBuiltOnce": True,
                        "targetReused": False,
                        "supersededReplacementId": None,
                        "supersededTargetFreed": True,
                    }
                if method == "get_capture_status":
                    return {
                        "loaded": True,
                        "api": "GraphicsAPI.D3D11",
                        "captureId": capture_id,
                        "capturePath": "D:/captures/frame.rdc",
                        "captureSessionId": "8" * 32,
                        "knownCatalogCaptureIds": [capture_id],
                    }
                if method == "rollback_shader_candidate_override":
                    return {
                        "schema": "renderdoc-ultra-event-shader-override/v1",
                        "schemaVersion": 1,
                        "overrideId": params["override_id"],
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "eventId": 321,
                        "stage": "fragment",
                        "candidateRawSha256": candidate_hash,
                        "cleared": True,
                        "targetFreed": True,
                        "clearWarning": "",
                    }
                if method == "rollback_shader_candidate_overrides":
                    return {
                        "schema": "renderdoc-ultra-event-shader-rollback-batch/v1",
                        "schemaVersion": 1,
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "transactional": True,
                        "originalShadersPreserved": True,
                        "overrides": [
                            {
                                "schema": "renderdoc-ultra-event-shader-override/v1",
                                "schemaVersion": 1,
                                "overrideId": token,
                                "captureId": capture_id,
                                "captureSessionId": "8" * 32,
                                "eventId": event_id,
                                "stage": "fragment",
                                "candidateRawSha256": candidate_hash,
                                "cleared": True,
                                "targetFreed": index == 1,
                                "originalShaderPreserved": True,
                            }
                            for index, (token, event_id) in enumerate(
                                zip(params["override_ids"], (321, 322))
                            )
                        ],
                    }
                if method == "rollback_shader_candidate_shared_resource":
                    return {
                        "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                        "schemaVersion": 1,
                        "replacementId": params["replacement_id"],
                        "captureId": capture_id,
                        "captureSessionId": "8" * 32,
                        "workspaceId": workspace_id,
                        "originalResourceId": 77,
                        "targetResourceId": 101,
                        "resourceScoped": True,
                        "eventScoped": False,
                        "cleared": True,
                        "targetFreed": True,
                        "originalShaderPreserved": True,
                        "freeWarning": "",
                    }
                raise AssertionError("unexpected bridge method: %s" % method)

        service = FakeService()
        bridge = ApplyBridge()
        selected = types.SimpleNamespace(
            key=types.SimpleNamespace(
                catalog_key=shader_key,
                resource_id=77,
                stage="fragment",
                entry_point="main",
                raw_sha256=baseline_hash,
            ),
            abi=types.SimpleNamespace(sha256="9" * 64),
        )
        with self.server._SHADER_OVERRIDE_GUARD:
            self.server._SHADER_OVERRIDE_RECORDS.clear()
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
            self.server._SHADER_OVERRIDE_CAPTURE_KEY = ""
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=service
        ), mock.patch.object(self.server, "ipc", bridge), mock.patch.object(
            self.server,
            "select_unique_shader_for_use",
            return_value=selected,
        ), mock.patch.object(
            self.server,
            "list_exact_shader_family_uses",
            return_value=[321, 322],
        ), mock.patch.object(
            self.server,
            "_ensure_workspace_capture_catalog_identity",
            return_value={
                "state": "already-known",
                "rebound": False,
                "workspaceCreated": False,
                "candidateCompiled": False,
                "candidateApplied": False,
                "previousCandidateRetried": False,
            },
        ):
            validated = json.loads(
                self.server.validate_shader_workspace_candidate(
                    status["workspacePath"]
                )
            )
            self.assertTrue(validated["validation"]["replayAbiValidated"])
            self.assertFalse(validated["renderDocReplacement"]["attempted"])

            applied = json.loads(
                self.server.apply_shader_workspace_candidate(
                    status["workspacePath"], 321
                )
            )
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["override"]["eventScoped"])
            self.assertEqual(
                applied["rollback"]["tool"],
                "rollback_shader_workspace_override",
            )

            rolled_back = json.loads(
                self.server.rollback_shader_workspace_override(
                    status["workspacePath"], applied["override"]["overrideId"]
                )
            )
            self.assertFalse(rolled_back["applied"])
            self.assertTrue(rolled_back["override"]["targetFreed"])

            listed = json.loads(
                self.server.list_shader_workspace_family_uses(
                    status["workspacePath"]
                )
            )
            self.assertEqual(listed["eventIds"], [321, 322])
            self.assertFalse(listed["hasMore"])

            family = json.loads(
                self.server.apply_shader_workspace_candidate_family(
                    status["workspacePath"]
                )
            )
            self.assertTrue(family["applied"])
            self.assertEqual(family["scope"], "all-exact-uses")
            self.assertTrue(family["family"]["transactional"])
            self.assertEqual(
                {item["targetResourceId"] for item in family["family"]["overrides"]},
                {100},
            )
            family_rollback = json.loads(
                self.server.rollback_shader_workspace_override_family(
                    status["workspacePath"], family["rollback"]["overrideIds"]
                )
            )
            self.assertFalse(family_rollback["applied"])
            self.assertTrue(family_rollback["family"]["transactional"])

            shared = json.loads(
                self.server.apply_shader_workspace_shared_resource(
                    status["workspacePath"]
                )
            )
            self.assertTrue(shared["applied"])
            self.assertEqual(shared["scope"], "captured-resource")
            self.assertTrue(shared["replacement"]["resourceScoped"])
            self.assertFalse(shared["replacement"]["eventScoped"])
            self.assertEqual(
                shared["rollback"]["tool"],
                "rollback_shader_workspace_shared_resource",
            )
            shared_rollback = json.loads(
                self.server.rollback_shader_workspace_shared_resource(
                    status["workspacePath"],
                    shared["replacement"]["replacementId"],
                )
            )
            self.assertFalse(shared_rollback["applied"])
            self.assertTrue(shared_rollback["replacement"]["targetFreed"])

            exact_validation = json.loads(
                self.server.validate_shader_workspace_candidate(
                    status["workspacePath"], 2, candidate_hash
                )
            )
            self.assertTrue(
                exact_validation["validation"]["replayAbiValidated"]
            )
            shared_mutations_before_race = sum(
                call[0] == "apply_shader_candidate_shared_resource"
                for call in bridge.calls
            )
            status["revision"] = 3
            status["latestCompiled"]["revision"] = 3
            status["latestCompiled"]["rawSha256"] = "0" * 64
            raced = json.loads(
                self.server.apply_shader_workspace_shared_resource(
                    status["workspacePath"], 2, candidate_hash
                )
            )
            self.assertIn("expected saved candidate", raced["error"])
            self.assertEqual(
                sum(
                    call[0] == "apply_shader_candidate_shared_resource"
                    for call in bridge.calls
                ),
                shared_mutations_before_race,
            )
            status["revision"] = 2
            status["latestCompiled"]["revision"] = 2
            status["latestCompiled"]["rawSha256"] = candidate_hash

            # A save that lands while the Bridge mutation is running makes the
            # just-created replacement stale. The host must detect the changed
            # canonical source after the call and roll back that owned result.
            bridge.change_source_during_shared_apply = True
            raced_during_apply = json.loads(
                self.server.apply_shader_workspace_shared_resource(
                    status["workspacePath"], 2, candidate_hash
                )
            )
            self.assertIn("source changed", raced_during_apply["error"])
            self.assertEqual(
                sum(
                    call[0] == "rollback_shader_candidate_shared_resource"
                    for call in bridge.calls
                ),
                2,
            )
            bridge.change_source_during_shared_apply = False
            status["source"]["currentSha256"] = "1" * 64

            partial_binding = json.loads(
                self.server.validate_shader_workspace_candidate(
                    status["workspacePath"], 2
                )
            )
            self.assertIn("must be supplied together", partial_binding["error"])

            with mock.patch.object(
                self.server,
                "list_exact_shader_family_uses",
                return_value=list(range(1, 258)),
            ):
                oversized = json.loads(
                    self.server.apply_shader_workspace_candidate_family(
                        status["workspacePath"]
                    )
                )
            self.assertIn("257 exact uses", oversized["error"])
            self.assertIn("explicit subset", oversized["error"])

        methods = [call[0] for call in bridge.calls]
        self.assertEqual(methods.count("reflect_shader_candidate"), 7)
        self.assertEqual(methods.count("apply_shader_candidate_override"), 1)
        self.assertEqual(methods.count("rollback_shader_candidate_override"), 1)
        self.assertEqual(methods.count("apply_shader_candidate_overrides"), 1)
        self.assertEqual(methods.count("rollback_shader_candidate_overrides"), 1)
        self.assertEqual(
            methods.count("apply_shader_candidate_shared_resource"), 2
        )
        self.assertEqual(
            methods.count("rollback_shader_candidate_shared_resource"), 2
        )
        self.assertNotIn("ReplaceResource", methods)
        self.assertNotIn("replace_shader", methods)

    def test_create_uses_exact_key_and_host_owned_registry_path(self) -> None:
        catalog_dir = self.root / "catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "event_ids": [9],
                "stages": ["fragment"],
            },
        )
        workspace = self.root / "created-only"
        created = json.loads(
            self.server.create_shader_workspace(
                exported["catalogPath"],
                self.key.catalog_key,
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertNotIn("error", created)
        self.assertEqual(created["operation"], "create")
        self.assertFalse(created["candidateState"]["present"])
        self.assertEqual(
            self.server._shader_tools_config_path(), self.config.resolve()
        )
        self.assertEqual(
            self.server._resolve_shader_tools_config_path(
                {
                    "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG": str(self.config),
                    "LOCALAPPDATA": str(self.root / "ignored"),
                }
            ),
            self.config.resolve(),
        )

        self.assertEqual(
            self.server._resolve_shader_tools_config_path(
                {
                    "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG": "",
                    "LOCALAPPDATA": str(self.root / "LocalAppData"),
                }
            ),
            (self.root / "LocalAppData" / "RenderDocUltra" / "shader-tools.v1.json").resolve(),
        )

    def test_prepare_selector_fails_closed_on_two_event_stage_shaders(self) -> None:
        catalog_dir = self.root / "ambiguous-catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "event_ids": [41],
                "stages": ["fragment"],
            },
        )
        catalog_path = Path(exported["catalogPath"])
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        second_blob = make_dxbc(b"second shader")
        second_key = ShaderKey(
            78,
            "fragment",
            "main2",
            hashlib.sha256(second_blob).hexdigest(),
        )
        (catalog_dir / "blobs" / (second_key.raw_sha256 + ".bin")).write_bytes(
            second_blob
        )
        catalog["shaders"].append(
            {
                "shaderKey": second_key.catalog_key,
                "resourceId": second_key.resource_id,
                "stage": second_key.stage,
                "entryPoint": second_key.entry_point,
                "rawPath": "blobs/%s.bin" % second_key.raw_sha256,
                "rawSize": len(second_blob),
                "rawSha256": second_key.raw_sha256,
            }
        )
        catalog["uses"].append(
            {
                "eventId": 41,
                "shaderKey": second_key.catalog_key,
                "resourceId": second_key.resource_id,
                "stage": second_key.stage,
                "entryPoint": second_key.entry_point,
                "rawSha256": second_key.raw_sha256,
            }
        )
        catalog["captureId"] = capture_identity(catalog, [self.key, second_key])
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        with self.assertRaisesRegex(CatalogValidationError, "exactly one"):
            select_unique_shader_for_use(catalog_path, 41, "pixel")

    def test_registry_requires_explicit_fixed_file_hash_object(self) -> None:
        catalog_dir = self.root / "fixed-files-required-catalog"
        exported = self.bridge.call(
            "export_shader_catalog",
            {
                "output_dir": str(catalog_dir),
                "event_ids": [50],
                "stages": ["fragment"],
            },
        )
        registry = json.loads(self.config.read_text(encoding="utf-8"))
        del registry["tools"]["test-decompiler"]["fixedFileHashes"]
        self.config.write_text(json.dumps(registry), encoding="utf-8")
        workspace = self.root / "must-not-be-created"
        result = json.loads(
            self.server.create_shader_workspace(
                exported["catalogPath"],
                self.key.catalog_key,
                str(workspace),
                "test-decompiler",
                "test-compiler",
            )
        )
        self.assertEqual(result["type"], "ToolConfigurationError")
        self.assertIn("fixedFileHashes is required", result["error"])
        self.assertFalse(workspace.exists())

    def test_alias_apply_reexports_from_database_and_binds_exact_file_digest(self) -> None:
        database = self.root / "symbols.sqlite3"
        capture_id = "capture-program-verified"

        class FakeService:
            def __init__(inner_self, database_path):
                inner_self.database_path = Path(database_path).resolve()

            def export_alias_table(
                inner_self,
                supplied_capture_id,
                output_path,
                verified_only=True,
                scan_id="",
            ):
                self.assertEqual(supplied_capture_id, capture_id)
                self.assertTrue(verified_only)
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                value = {
                    "schema": "renderdoc-ultra-aliases/v1",
                    "schemaVersion": 1,
                    "captureId": supplied_capture_id,
                    "verifiedOnly": True,
                    "aliases": [],
                }
                path.write_text(json.dumps(value), encoding="utf-8")
                return {"path": str(path), "captureId": supplied_capture_id}

        class AliasBridge:
            def __init__(inner_self):
                inner_self.calls = []

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, dict(params), timeout))
                return {"activeValidatedAliases": 0}

        bridge = AliasBridge()
        with mock.patch.object(self.server, "SymbolService", FakeService), mock.patch.object(
            self.server, "ipc", bridge
        ):
            result = json.loads(
                self.server.apply_symbol_alias_table(
                    str(database), capture_id, scan_id="trusted-scan"
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(len(bridge.calls), 1)
        method, params, timeout = bridge.calls[0]
        self.assertEqual(method, "load_symbol_aliases")
        self.assertEqual(timeout, 60.0)
        self.assertTrue(params["confirmed_only"])
        self.assertEqual(
            params["expected_sha256"],
            hashlib.sha256(Path(params["path"]).read_bytes()).hexdigest(),
        )
        self.assertNotEqual(Path(params["path"]), database)

    def test_organization_semantic_context_is_capture_session_bound(self) -> None:
        capture_id = "a" * 64
        capture_session_id = "b" * 32
        raw_sha256 = "c" * 64
        status = {
            "workspaceId": "d" * 32,
            "capture": {
                "captureId": capture_id,
                "catalogPath": str(self.root / "catalog.json"),
            },
            "shader": {
                "resourceId": 71,
                "stage": "fragment",
                "entryPoint": "MainPS",
            },
            "stage": "fragment",
            "entryPoint": "MainPS",
            "targetProfile": {"profile": "ps_6_6"},
            "baseline": {
                "rawSha256": raw_sha256,
                "compiledInterface": {},
            },
            "reconstructionFidelity": {
                "schema": "renderdoc-ultra-shader-reconstruction-fidelity/v1",
                "available": True,
                "codeSha256Equal": False,
                "classification": "abi-compatible-executable-code-mismatch",
                "semanticEquivalenceVerified": False,
            },
        }
        current_context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
            },
        }

        class FakeService:
            def status(inner_self, workspace_path):
                self.assertEqual(workspace_path, str(self.root))
                return copy.deepcopy(status)

        class ContextBridge:
            def __init__(inner_self):
                inner_self.calls = []

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, dict(params), timeout))
                if method == "get_current_renderdoc_context":
                    return copy.deepcopy(current_context)
                if method == "get_bound_textures":
                    return [{"slot": 0, "resourceId": 17}]
                if method == "get_pipeline_state":
                    return {"shaders": {"fragment": {"shaderId": 71}}}
                raise AssertionError("unexpected bridge method: %s" % method)

        selected = types.SimpleNamespace(
            key=types.SimpleNamespace(
                resource_id=71,
                stage="fragment",
                entry_point="MainPS",
                raw_sha256=raw_sha256,
            )
        )
        bridge = ContextBridge()
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=FakeService()
        ), mock.patch.object(self.server, "ipc", bridge), mock.patch.object(
            self.server, "select_unique_shader_for_use", return_value=selected
        ):
            result = self.server._shader_organization_semantic_context(
                str(self.root), 12892
            )

        self.assertEqual(
            [call[0] for call in bridge.calls],
            [
                "get_current_renderdoc_context",
                "get_bound_textures",
                "get_current_renderdoc_context",
                "get_pipeline_state",
                "get_current_renderdoc_context",
            ],
        )
        live = result["liveBindingEvidence"]
        self.assertTrue(live["available"])
        self.assertEqual(live["captureId"], capture_id)
        self.assertEqual(live["captureSessionId"], capture_session_id)
        self.assertEqual(
            result["reconstructionFidelity"]["classification"],
            "abi-compatible-executable-code-mismatch",
        )

    def test_organization_semantic_context_discards_capture_session_drift(self) -> None:
        capture_id = "a" * 64
        status = {
            "workspaceId": "d" * 32,
            "capture": {
                "captureId": capture_id,
                "catalogPath": str(self.root / "catalog.json"),
            },
            "shader": {
                "resourceId": 71,
                "stage": "fragment",
                "entryPoint": "MainPS",
            },
            "stage": "fragment",
            "entryPoint": "MainPS",
            "targetProfile": {"profile": "ps_6_6"},
            "baseline": {
                "rawSha256": "c" * 64,
                "compiledInterface": {},
            },
            "reconstructionFidelity": {
                "schema": "renderdoc-ultra-shader-reconstruction-fidelity/v1",
                "available": True,
                "codeSha256Equal": False,
                "classification": "abi-compatible-executable-code-mismatch",
                "semanticEquivalenceVerified": False,
            },
        }
        sessions = iter(("b" * 32, "e" * 32))

        class FakeService:
            def status(inner_self, unused_workspace_path):
                return copy.deepcopy(status)

        class DriftingBridge:
            def __init__(inner_self):
                inner_self.calls = []

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, dict(params), timeout))
                if method == "get_current_renderdoc_context":
                    return {
                        "schema": "renderdoc-ultra-current-context/v1",
                        "schemaVersion": 1,
                        "capture": {
                            "loaded": True,
                            "captureId": capture_id,
                            "captureSessionId": next(sessions),
                        },
                    }
                if method == "get_bound_textures":
                    return []
                if method == "get_pipeline_state":
                    raise AssertionError("pipeline evidence must not be queried after drift")
                raise AssertionError("unexpected bridge method: %s" % method)

        selected = types.SimpleNamespace(
            key=types.SimpleNamespace(
                resource_id=71,
                stage="fragment",
                entry_point="MainPS",
                raw_sha256="c" * 64,
            )
        )
        bridge = DriftingBridge()
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=FakeService()
        ), mock.patch.object(self.server, "ipc", bridge), mock.patch.object(
            self.server, "select_unique_shader_for_use", return_value=selected
        ):
            with self.assertRaisesRegex(
                self.server.ShaderOrganizationError, "capture session changed"
            ):
                self.server._shader_organization_semantic_context(str(self.root), 12892)

        self.assertEqual(
            [call[0] for call in bridge.calls],
            [
                "get_current_renderdoc_context",
                "get_bound_textures",
                "get_current_renderdoc_context",
            ],
        )

    def test_qrenderdoc_discovery_rejects_non_qrenderdoc_executables(self) -> None:
        arbitrary = self.root / "python.exe"
        arbitrary.write_bytes(b"not qrenderdoc")
        self.assertEqual(
            self.server._validated_qrenderdoc_candidate(str(arbitrary)), ""
        )

        qrenderdoc = self.root / "qrenderdoc.exe"
        qrenderdoc.write_bytes(b"test executable identity")
        self.assertEqual(
            self.server._validated_qrenderdoc_candidate(str(qrenderdoc)),
            str(qrenderdoc.resolve()),
        )
        self.assertEqual(
            self.server._validated_qrenderdoc_candidate(str(self.root)),
            str(qrenderdoc.resolve()),
        )


if __name__ == "__main__":
    unittest.main()
