from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from src.active_projects import ActiveReplayProjectService
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import capture_identity


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


def _dxbc(code: bytes = b"active project shader") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(program)) + program
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


def _interface() -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


class _CatalogIPC:
    def __init__(self, root: Path):
        self.root = root
        self.blob = _dxbc()
        fingerprint = fingerprint_bytes(self.blob)
        assert fingerprint is not None
        self.shader = {
            "shaderResourceId": 902,
            "resourceId": 902,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": "blobs/%s.bin" % fingerprint.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            **_interface(),
        }
        self.shader["shaderKey"] = f"902:fragment:main:{fingerprint.raw_sha256}"
        self.use = {
            "eventId": 142,
            "stage": "fragment",
            "entryPoint": "main",
            "resourceId": 902,
            "rawSha256": fingerprint.raw_sha256,
            "shaderKey": self.shader["shaderKey"],
            "constantBlocks": [],
        }
        base = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/frame.rdc",
            "api": "D3D11",
            "gameId": "example-game",
            "buildId": "build-1",
            "platform": "windows-x64",
            "shaders": [dict(self.shader)],
            "uses": [dict(self.use)],
        }
        self.capture_id = capture_identity(base, [self.shader])
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "snapshotId": "f" * 64,
            "capture": {
                "loaded": True,
                "captureSessionId": "a" * 32,
                "captureId": self.capture_id,
                "path": "D:/captures/frame.rdc",
                "api": "D3D11",
                "local": True,
            },
            "events": {"currentEventId": 142, "selectedEventId": 142},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": 902,
                        "entryPoint": "main",
                        "rawSha256": fingerprint.raw_sha256,
                    }
                ],
                "focused": None,
                "focusIsExplicit": False,
            },
            "texture": {"available": False},
        }
        self.export_revision = 1
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method: str, params: dict | None = None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params, timeout))
        if method == "get_current_renderdoc_context":
            return copy.deepcopy(self.context)
        if method == "export_shader_catalog":
            self.assert_full_export(params)
            output = Path(params["output_dir"])
            (output / "blobs").mkdir(parents=True, exist_ok=True)
            raw_path = output / self.shader["rawPath"]
            raw_path.write_bytes(self.blob)
            value = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "capturePath": "D:/captures/frame.rdc",
                "api": "D3D11",
                "gameId": "example-game",
                "buildId": "build-1",
                "platform": "windows-x64",
                "shaders": [dict(self.shader)],
                "uses": [dict(self.use)],
                "exportDiagnostics": {
                    "reflectionRevision": self.export_revision
                },
            }
            path = output / "shader_catalog.v1.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return {
                "schema": value["schema"],
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "catalogPath": str(path),
            }
        if method == "load_symbol_aliases":
            return {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "confirmedOnly": True,
                "counts": {"activeValidatedAliases": 0},
                "applied": [],
            }
        raise AssertionError("unexpected IPC method %s" % method)

    def assert_full_export(self, params: dict) -> None:
        if params.get("event_ids") != []:
            raise AssertionError("active capture preparation must export the full frame")


class _FakeSymbolService:
    scan_calls = 0

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()

    @classmethod
    def for_asset_root(cls, asset_root, database_path=None):
        return cls(Path(database_path or (Path(asset_root) / "symbols.sqlite3")))

    def scan(self, asset_root, build_id="", **unused_kwargs):
        type(self).scan_calls += 1
        return {
            "schema": "renderdoc-ultra-asset-scan/v1",
            "scanId": "1" * 24,
            "assetRoot": str(Path(asset_root).resolve()),
            "buildId": build_id,
        }

    def run(self, asset_root, catalog_path, build_id="", **unused_kwargs):
        scan = self.scan(asset_root, build_id=build_id)
        return {"scan": scan, "match": self.match(catalog_path, scan["scanId"])}

    def match(self, catalog_path, scan_id):
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        return {
            "schema": "renderdoc-ultra-symbol-match/v1",
            "captureId": catalog["captureId"],
            "scanId": scan_id,
            "matches": [],
        }

    def export_alias_table(
        self,
        capture_id,
        output_path,
        verified_only=True,
        scan_id="",
        catalog_sha256="",
    ):
        assert verified_only is True
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": "renderdoc-ultra-aliases/v1",
            "schemaVersion": 1,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "verifiedOnly": True,
            "aliases": [],
            "shaders": [],
            "symbolRules": [],
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return {
            "schema": value["schema"],
            "schemaVersion": 1,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "path": str(path),
            "aliasCount": 0,
            "shaderCount": 0,
            "verifiedOnly": True,
        }

    def unresolved(self, capture_id):
        return {"captureId": capture_id, "count": 0, "items": []}


class ActiveProjectMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = ActiveReplayProjectService(
            self.root / "config" / "active-project.json",
            self.root / "projects",
        )
        self.ipc = _CatalogIPC(self.root)
        self.old_service = self.server._ACTIVE_PROJECT_SERVICE
        self.old_ipc = self.server.ipc
        self.old_route_binding = self.server._active_shader_route_binding_sha256
        self.server._ACTIVE_PROJECT_SERVICE = self.service
        self.server.ipc = self.ipc
        # This unit suite exercises active-project identity and path binding,
        # not the machine-global installed tool registry.  Isolate it from a
        # developer's previously generated LocalAppData configuration while
        # retaining a strict, deterministic route-binding identity.
        self.server._active_shader_route_binding_sha256 = lambda: "e" * 64
        self.server._ACTIVE_SYMBOL_SCAN_CACHE.clear()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        _FakeSymbolService.scan_calls = 0

    def tearDown(self):
        self.server._ACTIVE_PROJECT_SERVICE = self.old_service
        self.server.ipc = self.old_ipc
        self.server._active_shader_route_binding_sha256 = self.old_route_binding
        self.server._ACTIVE_SYMBOL_SCAN_CACHE.clear()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        self.temporary.cleanup()

    def _json(self, value: str) -> dict:
        result = json.loads(value)
        self.assertNotIn("error", result, result)
        self.assertIs(result["applied"], False)
        return result

    def _bound_with_assets(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        configured = self._json(
            self.server.configure_active_replay_project(
                "example-game",
                "build-1",
                "windows-x64",
                "d3d11",
            )
        )
        self.assertTrue(configured["project"]["scopeConfigured"])
        assets = self.root / "unpacked"
        assets.mkdir()
        (assets / "main.hlsl").write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        self._json(self.server.configure_active_project_asset_root(str(assets)))

    def test_process_fixed_default_paths_and_tool_signatures(self):
        local = self.root / "LocalAppData"
        state, projects = self.server._resolve_active_project_paths(
            {"LOCALAPPDATA": str(local)}
        )
        self.assertEqual(
            state,
            (local / "ReplayLab" / "config" / "active-project.json").resolve(),
        )
        self.assertEqual(projects, (local / "ReplayLab" / "projects").resolve())

        custom_state = self.root / "owner" / "selection.json"
        custom_home = self.root / "owner" / "projects"
        state, projects = self.server._resolve_active_project_paths(
            {
                "LOCALAPPDATA": str(local),
                self.server.ACTIVE_PROJECT_STATE_ENV: str(custom_state),
                self.server.ACTIVE_PROJECTS_HOME_ENV: str(custom_home),
            }
        )
        self.assertEqual(state, custom_state.resolve())
        self.assertEqual(projects, custom_home.resolve())

        expected = {
            "ensure_active_replay_project",
            "get_active_replay_project_status",
            "get_active_capture_catalog_binding_status",
            "configure_active_replay_project",
            "configure_active_project_asset_root",
            "activate_active_replay_project",
            "prepare_active_capture_project",
            "get_active_shader_workspace_path",
            "register_active_project_workspace",
            "get_active_project_future_frame_plan",
            "seed_active_shader_workspace",
        }
        for name in expected:
            parameters = inspect.signature(getattr(self.server, name)).parameters
            self.assertNotIn("projects_home", parameters)
            self.assertNotIn("state_path", parameters)
        self.assertEqual(
            list(
                inspect.signature(
                    self.server.prepare_active_capture_project
                ).parameters
            ),
            [],
        )

    def test_pending_project_stages_full_capture_and_blocks_physical_reuse(self):
        ensured = self._json(self.server.ensure_active_replay_project("Example Game"))
        self.assertEqual(ensured["project"]["state"], "pending-scope")
        prepared = self._json(self.server.prepare_active_capture_project())
        self.assertEqual(
            prepared["schema"], "replaylab-active-capture-prepare/v1"
        )
        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(prepared["stagedCatalog"]["captureId"], self.ipc.capture_id)
        self.assertFalse(prepared["stagedCatalog"]["physicalKnowledgeImportAllowed"])
        self.assertTrue(prepared["reusePlan"]["blocked"])
        self.assertFalse(prepared["symbolResolution"]["attempted"])
        self.assertFalse(prepared["policy"]["aiProvisionalAutoApply"])
        self.assertTrue(prepared["captureExport"]["temporaryExportRemoved"])
        self.assertTrue(Path(prepared["captureExport"]["catalogPath"]).is_file())
        export_calls = [item for item in self.ipc.calls if item[0] == "export_shader_catalog"]
        self.assertEqual(len(export_calls), 1)
        self.assertEqual(export_calls[0][1]["event_ids"], [])
        self.assertEqual(
            export_calls[0][2],
            self.server.ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(export_calls[0][2], 900.0)
        catalog_root = Path(prepared["project"]["paths"]["captureCatalogs"])
        self.assertFalse(any(item.name.startswith(".current-") for item in catalog_root.iterdir()))

    def test_internal_prepare_passes_only_token_bound_progress_parameters(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        token = "d" * 32

        prepared = self._json(
            self.server._prepare_active_capture_project(progress_token=token)
        )

        self.assertEqual(prepared["capture"]["captureSessionId"], "a" * 32)
        export = next(
            params
            for method, params, unused_timeout in self.ipc.calls
            if method == "export_shader_catalog"
        )
        self.assertEqual(export["progress_token"], token)
        self.assertEqual(export["progress_capture_session_id"], "a" * 32)
        self.assertNotIn("progress_path", export)

    def test_progress_sidecar_reader_binds_instance_token_and_capture_session(self):
        instances = self.root / "ipc-v2" / "instances"
        instance = instances / ("123-%s" % ("b" * 32))
        requests = instance / "requests"
        responses = instance / "responses"
        requests.mkdir(parents=True)
        responses.mkdir()

        class Selector:
            @staticmethod
            def _instance_mailbox():
                return str(requests), str(responses)

        old_ipc = self.server.ipc
        self.server.ipc = Selector()
        try:
            with mock.patch.object(self.server, "INSTANCE_ROOT", str(instances)):
                channel = self.server._shader_catalog_progress_channel("a" * 32)
                path = Path(channel["path"])
                value = {
                    "schema": self.server.SHADER_CATALOG_PROGRESS_SCHEMA,
                    "schemaVersion": 1,
                    "operationToken": channel["operationToken"],
                    "instanceId": channel["instanceId"],
                    "captureSessionId": channel["captureSessionId"],
                    "phase": "export-shader-bindings",
                    "eventId": 42,
                    "current": 2,
                    "total": 9,
                    "updatedUnixMs": 123456,
                }
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(
                    self.server._read_shader_catalog_progress(channel), value
                )

                changed = dict(value)
                changed["captureSessionId"] = "c" * 32
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "binding"):
                    self.server._read_shader_catalog_progress(channel)

                self.server._cleanup_shader_catalog_progress(channel)
                self.assertFalse(path.exists())
        finally:
            self.server.ipc = old_ipc

    def test_new_capture_without_live_id_uses_exported_canonical_identity(self):
        self.ipc.context["capture"]["captureId"] = ""
        self._json(self.server.ensure_active_replay_project("Example Game"))

        prepared = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(prepared["stagedCatalog"]["captureId"], self.ipc.capture_id)
        export = next(
            params
            for method, params, unused_timeout in self.ipc.calls
            if method == "export_shader_catalog"
        )
        self.assertEqual(export["event_ids"], [])
        self.assertNotIn("capture_id", export)
        self.assertNotIn("expected_capture_id", export)
        selected = self.server._active_capture_catalog_selection(142, "fragment")
        self.assertIsNotNone(selected)
        path_result = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertNotIn("error", path_result, path_result)
        self.assertEqual(path_result["captureId"], self.ipc.capture_id)

    def test_bound_symbol_resolution_keeps_canonical_id_when_live_id_stays_empty(self):
        self._bound_with_assets()
        self.ipc.context["capture"]["captureId"] = ""

        with mock.patch.object(self.server, "SymbolService", _FakeSymbolService):
            prepared = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertTrue(prepared["symbolResolution"]["attempted"])
        self.assertEqual(
            prepared["symbolResolution"]["match"]["captureId"],
            self.ipc.capture_id,
        )

    def test_nonempty_filtered_catalog_id_may_differ_from_full_export(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))

        result = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(result["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(result["stagedCatalog"]["captureId"], self.ipc.capture_id)
        self.assertEqual(
            self.ipc.context["capture"]["captureId"],
            "0" * 64,
        )

    def test_same_session_shader_selection_reuses_staged_full_catalog(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        exports_before = len(
            [call for call in self.ipc.calls if call[0] == "export_shader_catalog"]
        )

        reused = self.server._active_capture_catalog_selection(142, "fragment")

        self.assertIsNotNone(reused)
        exported, catalog_path, selected, binding_sha256 = reused
        self.assertTrue(exported["reusedActiveProjectCatalog"])
        self.assertFalse(exported["exportPerformed"])
        self.assertEqual(exported["captureId"], self.ipc.capture_id)
        self.assertEqual(Path(catalog_path), Path(prepared["stagedCatalog"]["catalogPath"]))
        self.assertEqual(selected.key.catalog_key, self.ipc.shader["shaderKey"])
        self.assertRegex(binding_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "export_shader_catalog"]),
            exports_before,
        )

    def test_public_prepare_rebinds_same_capture_id_to_new_catalog_revision(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))
        unbound = self._json(
            self.server.get_active_capture_catalog_binding_status()
        )
        self.assertFalse(unbound["bound"])

        first = self._json(self.server.prepare_active_capture_project())
        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(first["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(second["capture"]["captureId"], self.ipc.capture_id)
        self.assertNotEqual(
            first["stagedCatalog"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertNotEqual(
            first["stagedCatalog"]["catalogPath"],
            second["stagedCatalog"]["catalogPath"],
        )
        self.assertTrue(Path(first["stagedCatalog"]["catalogPath"]).is_file())
        self.assertTrue(Path(second["stagedCatalog"]["catalogPath"]).is_file())

        status = self._json(
            self.server.get_active_capture_catalog_binding_status()
        )
        self.assertTrue(status["bound"])
        self.assertEqual(
            status["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertEqual(
            status["catalogPath"], second["stagedCatalog"]["catalogPath"]
        )
        reused = self.server._active_capture_catalog_selection(142, "fragment")
        self.assertIsNotNone(reused)
        self.assertEqual(Path(reused[1]), Path(status["catalogPath"]))

    def test_bound_prepare_imports_two_revisions_without_capture_id_conflict(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        configured = self._json(
            self.server.configure_active_replay_project(
                "example-game",
                "build-1",
                "windows-x64",
                "d3d11",
            )
        )
        self.assertTrue(configured["project"]["scopeConfigured"])

        first = self._json(self.server.prepare_active_capture_project())
        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(first["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(second["capture"]["captureId"], self.ipc.capture_id)
        self.assertNotEqual(
            first["stagedCatalog"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertTrue(second["reusePlan"]["catalogRevisionAdded"])
        self.assertTrue(second["reusePlan"]["activeRevisionChanged"])
        replay = self.service.replay_service().status()
        self.assertEqual(replay["captureCount"], 1)
        self.assertEqual(replay["captureRevisionCount"], 2)
        self.assertEqual(
            second["reusePlan"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )

    def test_managed_workspace_path_is_keyed_by_exact_catalog_revision(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        first = self._json(self.server.prepare_active_capture_project())
        path_a = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )

        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())
        path_b = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )

        self.assertEqual(path_a["captureId"], path_b["captureId"])
        self.assertEqual(path_a["rawSha256"], path_b["rawSha256"])
        self.assertEqual(
            path_a["catalogSha256"], first["stagedCatalog"]["catalogSha256"]
        )
        self.assertEqual(
            path_b["catalogSha256"], second["stagedCatalog"]["catalogSha256"]
        )
        self.assertNotEqual(path_a["catalogSha256"], path_b["catalogSha256"])
        self.assertNotEqual(path_a["workspacePath"], path_b["workspacePath"])
        self.assertRegex(path_a["routeBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            path_a["routeBindingSha256"], path_b["routeBindingSha256"]
        )
        self.assertEqual(path_a["workspaceLayout"], "content-addressed-v2")
        self.assertEqual(path_b["workspaceLayout"], "content-addressed-v2")
        self.assertRegex(path_a["workspaceBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(path_b["workspaceBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            Path(path_a["workspacePath"]).name,
            path_a["workspaceBindingSha256"],
        )
        self.assertEqual(
            Path(path_b["workspacePath"]).name,
            path_b["workspaceBindingSha256"],
        )

    def test_tampered_full_catalog_binding_fails_without_subset_fallback(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        catalog = Path(prepared["stagedCatalog"]["catalogPath"])
        catalog.write_bytes(catalog.read_bytes() + b"\n")
        exports_before = len(
            [call for call in self.ipc.calls if call[0] == "export_shader_catalog"]
        )

        with self.assertRaisesRegex(RuntimeError, "path/hash validation"):
            self.server._active_capture_catalog_selection(142, "fragment")

        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "export_shader_catalog"]),
            exports_before,
        )

    def test_oversized_full_catalog_binding_fails_before_hashing(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        catalog = Path(prepared["stagedCatalog"]["catalogPath"])
        with catalog.open("r+b") as stream:
            stream.truncate(self.server.MAX_ACTIVE_CAPTURE_CATALOG_BYTES + 1)

        with mock.patch.object(
            self.server,
            "_file_sha256",
            side_effect=AssertionError("unbounded hash helper must not be used"),
        ):
            with self.assertRaisesRegex(RuntimeError, "size is invalid"):
                self.server._active_capture_catalog_selection(142, "fragment")

    def test_prepare_rejects_live_path_and_api_mismatch_before_staging(self):
        cases = (
            ("path", "D:/captures/other.rdc", "catalog path differs"),
            ("api", "Vulkan", "catalog API differs"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                self.ipc.context["capture"]["captureId"] = ""
                self.ipc.context["capture"][field] = value
                status = self._json(
                    self.server.ensure_active_replay_project("Example Game")
                )["project"]

                result = json.loads(self.server.prepare_active_capture_project())

                self.assertIn(message, result["error"])
                self.assertFalse(
                    (
                        Path(status["paths"]["captureCatalogs"])
                        / self.ipc.capture_id
                    ).exists()
                )
                self.ipc.context["capture"][field] = (
                    "D:/captures/frame.rdc" if field == "path" else "D3D11"
                )

    def test_prepare_rejects_capture_session_change_before_staging(self):
        self.ipc.context["capture"]["captureId"] = ""
        status = self._json(
            self.server.ensure_active_replay_project("Example Game")
        )["project"]
        original_call = self.ipc.call

        def change_session_after_export(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog":
                self.ipc.context["capture"]["captureSessionId"] = "b" * 32
            return result

        with mock.patch.object(
            self.ipc, "call", side_effect=change_session_after_export
        ):
            result = json.loads(self.server.prepare_active_capture_project())

        self.assertIn("capture/session changed", result["error"])
        self.assertFalse(
            (Path(status["paths"]["captureCatalogs"]) / self.ipc.capture_id).exists()
        )

    def test_bound_project_scans_once_per_session_and_registers_verified_aliases(self):
        self._bound_with_assets()
        original_discover = self.server.discover_recovery_assets
        original_report = self.server._active_asset_recovery_report
        guard_available: list[bool] = []

        def assert_guard_available(function, *args, **kwargs):
            acquired: list[bool] = []

            def probe():
                value = self.server._ACTIVE_SYMBOL_SCAN_GUARD.acquire(timeout=1.0)
                acquired.append(value)
                if value:
                    self.server._ACTIVE_SYMBOL_SCAN_GUARD.release()

            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(timeout=2.0)
            guard_available.append(acquired == [True])
            return function(*args, **kwargs)

        with mock.patch.object(
            self.server, "SymbolService", _FakeSymbolService
        ), mock.patch.object(
            self.server,
            "discover_recovery_assets",
            side_effect=lambda *args, **kwargs: assert_guard_available(
                original_discover, *args, **kwargs
            ),
        ), mock.patch.object(
            self.server,
            "_active_asset_recovery_report",
            side_effect=lambda *args, **kwargs: assert_guard_available(
                original_report, *args, **kwargs
            ),
        ):
            first = self._json(self.server.prepare_active_capture_project())
            self.ipc.export_revision = 2
            second = self._json(self.server.prepare_active_capture_project())
        self.assertTrue(guard_available)
        self.assertTrue(all(guard_available))
        self.assertEqual(_FakeSymbolService.scan_calls, 1)
        self.assertTrue(first["symbolResolution"]["attempted"])
        self.assertFalse(first["symbolResolution"]["scanCacheHit"])
        self.assertTrue(second["symbolResolution"]["scanCacheHit"])
        self.assertTrue(first["symbolResolution"]["aliasTable"]["verifiedOnly"])
        self.assertEqual(
            first["symbolResolution"]["aliasTable"]["catalogSha256"],
            first["stagedCatalog"]["catalogSha256"],
        )
        self.assertEqual(
            second["symbolResolution"]["aliasTable"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertNotEqual(
            first["symbolResolution"]["aliasTable"]["path"],
            second["symbolResolution"]["aliasTable"]["path"],
        )
        self.assertIs(first["symbolResolution"]["projectRegistration"]["applied"], False)
        self.assertTrue(
            first["symbolResolution"]["renderDocAliasOverlay"]["confirmedOnly"]
        )
        self.assertFalse(first["symbolResolution"]["policy"]["aiProvisionalAutoApply"])
        recovery = first["symbolResolution"]["assetShaderRecovery"]
        self.assertEqual(recovery["schema"], "replaylab-active-asset-shader-recovery/v1")
        self.assertEqual(recovery["inventory"]["candidateCount"], 1)
        self.assertEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["relativePath"],
            "main.hlsl",
        )
        self.assertGreaterEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["assessment"][
                "recoveryLevel"
            ],
            2,
        )
        self.assertFalse(recovery["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(recovery["policy"]["applyAuthorized"])
        self.assertEqual(recovery["sourcePreviews"][0]["candidateId"], recovery[
            "boundShaders"
        ][0]["topCandidates"][0]["candidateId"])
        preview = recovery["sourcePreviews"][0]
        self.assertEqual(
            hashlib.sha256(preview["sourcePreview"].encode("utf-8")).hexdigest(),
            preview["sourcePreviewSha256"],
        )
        self.assertTrue(recovery["sourcePreviewPolicy"]["fullFileHashRevalidated"])
        self.assertIn("activeProjectPolicy", first["reusePlan"])
        future = self._json(self.server.get_active_project_future_frame_plan())
        self.assertEqual(future["captureId"], self.ipc.capture_id)
        self.assertFalse(future["plan"]["blocked"])
        self.assertFalse(future["plan"]["policy"]["aiProvisionalAutoApply"])

    def test_workspace_path_uses_live_capture_stage_resource_and_hash(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        result = self._json(
            self.server.get_active_shader_workspace_path(142, "pixel")
        )
        self.assertEqual(result["captureId"], self.ipc.capture_id)
        self.assertEqual(result["captureSessionId"], "a" * 32)
        self.assertEqual(result["stage"], "fragment")
        self.assertEqual(result["shaderResourceId"], 902)
        workspace = Path(result["workspacePath"])
        self.assertFalse(workspace.exists())
        self.assertEqual(result["workspaceLayout"], "content-addressed-v2")
        self.assertEqual(workspace.parent.name, "v2")
        self.assertEqual(workspace.name, result["workspaceBindingSha256"])
        repeated = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertEqual(repeated["workspacePath"], result["workspacePath"])

        error = json.loads(self.server.get_active_shader_workspace_path(999, "fragment"))
        self.assertEqual(error["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("current event", error["error"])
        self.assertFalse(error["applied"])

    def test_explicit_asset_recovery_inspection_is_read_only(self):
        assets = self.root / "shader-assets"
        assets.mkdir()
        (assets / "surface.frag").write_text(
            "#version 450\nlayout(location=0) out vec4 color;\n"
            "void main() { color = vec4(1.0); }\n",
            encoding="utf-8",
        )
        result = json.loads(
            self.server.inspect_asset_shader_recovery(
                str(assets),
                json.dumps({"stage": "fragment", "entryPoint": "main"}),
                max_files=10,
                max_file_size_mb=1,
                max_total_read_mb=2,
                max_candidates=10,
            )
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["schema"], "renderdoc-ultra-recovery-assets/v1")
        self.assertEqual(result["candidateCount"], 1)
        self.assertEqual(result["candidates"][0]["relativePath"], "surface.frag")
        self.assertFalse(result["policy"]["assetCodeExecuted"])
        self.assertFalse(result["policy"]["compilerInvoked"])
        self.assertFalse(result["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(result["policy"]["applyAuthorized"])

    def test_source_preview_rejects_parent_reparse_even_with_same_bytes(self):
        assets = self.root / "preview-assets"
        original = assets / "shader-dir"
        replacement = assets / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        source = "float4 main() : SV_Target { return 1; }\n"
        (original / "main.hlsl").write_text(source, encoding="utf-8")
        (replacement / "main.hlsl").write_text(source, encoding="utf-8")
        inventory = self.server.discover_recovery_assets(assets)
        candidate = next(
            item
            for item in inventory["candidates"]
            if item["relativePath"] == "shader-dir/main.hlsl"
        )
        original.rename(assets / "held-original")
        redirected = assets / "shader-dir"
        try:
            if os.name == "nt":
                import _winapi

                _winapi.CreateJunction(str(replacement), str(redirected))
                self.assertFalse(redirected.is_symlink())
            else:
                os.symlink(replacement, redirected, target_is_directory=True)
        except (ImportError, OSError, NotImplementedError):
            self.skipTest("reparse/symlink creation is unavailable")

        preview = self.server._active_recovery_source_preview(
            assets, candidate, 1024
        )
        self.assertIsNone(preview)

    def test_explicit_recovery_limits_reject_values_above_server_hard_caps(self):
        assets = self.root / "shader-assets"
        assets.mkdir()
        cases = (
            {"max_files": self.server._RECOVERY_INSPECT_MAX_FILES + 1},
            {
                "max_file_size_mb": (
                    self.server._RECOVERY_INSPECT_MAX_FILE_SIZE_MB + 1
                )
            },
            {
                "max_total_read_mb": (
                    self.server._RECOVERY_INSPECT_MAX_TOTAL_READ_MB + 1
                )
            },
            {
                "max_candidates": (
                    self.server._RECOVERY_INSPECT_MAX_CANDIDATES + 1
                )
            },
        )
        for override in cases:
            arguments = {
                "asset_root": str(assets),
                "max_files": 1,
                "max_file_size_mb": 1,
                "max_total_read_mb": 1,
                "max_candidates": 1,
                **override,
            }
            result = json.loads(
                self.server.inspect_asset_shader_recovery(**arguments)
            )
            self.assertEqual(result["type"], "ValueError")
            self.assertIn("fixed hard cap", result["error"])

    def test_legacy_scan_defaults_recovery_inventory_off(self):
        parameter = inspect.signature(self.server.scan_asset_symbols).parameters[
            "include_recovery_candidates"
        ]
        self.assertIs(parameter.default, False)

    def test_recommended_one_shot_returns_bounded_shader_recovery(self):
        assets = self.root / "one-shot-assets"
        assets.mkdir()
        (assets / "main.hlsl").write_text(
            "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
        )
        workspace = self.root / "one-shot-workspace"
        with mock.patch.object(self.server, "SymbolService", _FakeSymbolService):
            result = json.loads(
                self.server.resolve_capture_symbols(
                    str(assets),
                    output_dir=str(workspace),
                    apply_verified=False,
                )
            )
        self.assertNotIn("error", result, result)
        recovery = result["assetShaderRecovery"]
        self.assertEqual(
            recovery["schema"], "replaylab-active-asset-shader-recovery/v1"
        )
        self.assertEqual(recovery["inventory"]["candidateCount"], 1)
        self.assertEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["relativePath"],
            "main.hlsl",
        )
        self.assertLessEqual(
            len(json.dumps(recovery, ensure_ascii=False).encode("utf-8")),
            recovery["reportPolicy"]["maximumJsonUtf8Bytes"],
        )
        self.assertFalse(recovery["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(recovery["policy"]["applyAuthorized"])

    def test_active_symbol_scan_cache_is_bounded_lru_with_ttl(self):
        def value(index: int) -> dict:
            return {
                "scan": {"scanId": str(index)},
                "databasePath": "db",
                "recoveryInventory": {"candidates": []},
            }

        keys = [
            ("workspace", "session-%d" % index, "asset", "build")
            for index in range(self.server._ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES + 1)
        ]
        with mock.patch.object(self.server.time, "monotonic", return_value=0.0):
            for index, key in enumerate(keys):
                self.server._active_symbol_scan_cache_put_if_absent(key, value(index))
        self.assertEqual(
            len(self.server._ACTIVE_SYMBOL_SCAN_CACHE),
            self.server._ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES,
        )
        self.assertNotIn(keys[0], self.server._ACTIVE_SYMBOL_SCAN_CACHE)
        with mock.patch.object(
            self.server.time,
            "monotonic",
            return_value=self.server._ACTIVE_SYMBOL_SCAN_CACHE_TTL_SECONDS + 1,
        ):
            self.assertIsNone(
                self.server._active_symbol_scan_cache_get(keys[-1], "db")
            )
        self.assertFalse(self.server._ACTIVE_SYMBOL_SCAN_CACHE)

    def test_activation_rejects_unregistered_project_root(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        outside = self.root / "outside-project"
        outside.mkdir()
        result = json.loads(self.server.activate_active_replay_project(str(outside)))
        self.assertEqual(result["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("not registered", result["error"])
        self.assertFalse(result["applied"])


class ActiveWorkspaceWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "projects" / "p" / "workspaces" / "shaders" / "w"
        self.workspace.mkdir(parents=True)
        self.capture_id = "c" * 64
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": "d" * 32,
                "path": "D:/captures/frame.rdc",
                "api": "D3D11",
            },
            "events": {"currentEventId": 7},
            "shaders": {"bound": []},
        }

        class IPC:
            def __init__(inner):
                inner.context = self.context

            def call(inner, method, params=None, timeout=None):
                if method != "get_current_renderdoc_context":
                    raise AssertionError(method)
                return copy.deepcopy(inner.context)

        class Active:
            def __init__(inner):
                inner.registered = None
                inner.seeded = None

            def status(inner):
                return {
                    "paths": {
                        "shaderWorkspaces": str(
                            self.root / "projects" / "p" / "workspaces" / "shaders"
                        )
                    }
                }

            def pin_active_project(inner, *unused_args, **unused_kwargs):
                return nullcontext()

            def register_workspace(inner, status):
                inner.registered = status
                return {"knowledge": {"knowledgeId": "e" * 32}, "applied": False}

            def seed_workspace(inner, knowledge_id, status):
                inner.seeded = (knowledge_id, status)
                return {"sourceSha256": "f" * 64, "applied": False}

        class Workspaces:
            def __init__(inner):
                inner.calls = 0

            def status(inner, workspace_path):
                inner.calls += 1
                modified = inner.calls > 1
                return {
                    "schema": "renderdoc-ultra-shader-workspace-status/v1",
                    "workspacePath": workspace_path,
                    "capture": {"captureId": self.capture_id},
                    "source": {
                        "currentSha256": "f" * 64,
                        "modifiedSinceDecompile": modified,
                    },
                }

        self.active = Active()
        self.workspaces = Workspaces()
        self.old_service = self.server._ACTIVE_PROJECT_SERVICE
        self.old_ipc = self.server.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active
        self.server.ipc = IPC()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()

    def tearDown(self):
        self.server._ACTIVE_PROJECT_SERVICE = self.old_service
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        self.temporary.cleanup()

    def test_register_and_seed_are_capture_bound_and_never_apply(self):
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            registered = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
            self.assertNotIn("error", registered)
            self.assertFalse(registered["applied"])
            self.assertEqual(registered["captureSessionId"], "d" * 32)
            seeded = json.loads(
                self.server.seed_active_shader_workspace(
                    "e" * 32, str(self.workspace)
                )
            )
        self.assertNotIn("error", seeded)
        self.assertFalse(seeded["applied"])
        self.assertTrue(
            seeded["destinationStatus"]["source"]["modifiedSinceDecompile"]
        )
        self.assertEqual(self.active.seeded[0], "e" * 32)


if __name__ == "__main__":
    unittest.main()
