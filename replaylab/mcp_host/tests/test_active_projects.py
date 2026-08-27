from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import unittest
from unittest import mock

from src.active_projects import (
    ActiveProjectError,
    ActiveReplayProjectService,
    CAPTURE_LIBRARY_SCHEMA,
    FUTURE_FRAME_PLAN_SCHEMA,
    PROJECT_WORKSPACE_SCHEMA,
)
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, capture_identity


def _canonical_hash(value: dict, field: str) -> str:
    material = dict(value)
    material.pop(field, None)
    payload = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dxbc(code: bytes = b"compiled instructions", metadata: bytes = b"") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunks = [b"SHDR" + struct.pack("<I", len(program)) + program]
    if metadata:
        chunks.append(b"RDEF" + struct.pack("<I", len(metadata)) + metadata)
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", offset) for offset in offsets)
        + b"".join(chunks)
    )


def _complete_interface() -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [
            {
                "name": "PerMaterial",
                "space": 0,
                "bind": 0,
                "arraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": "BaseColor",
                        "byteOffset": 0,
                        "byteSize": 16,
                        "baseType": "Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0,
                        "bitFieldOffset": 0,
                        "bitFieldSize": 0,
                        "members": [],
                    }
                ],
            }
        ],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


class ActiveReplayProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects_home = self.root / "projects"
        self.state_path = self.root / "state" / "active-project.json"
        self.service = ActiveReplayProjectService(
            self.state_path, self.projects_home
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _pending(self) -> dict:
        return self.service.ensure_pending("Example Game Project")

    def _bound(self, *, api_family: str = "d3d11") -> dict:
        self._pending()
        return self.service.configure_scope(
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family=api_family,
        )

    def _catalog(
        self,
        label: str,
        blob: bytes,
        *,
        resource_id: int,
        include_scope: bool = True,
    ) -> tuple[Path, dict, str]:
        directory = self.root / "catalog-sources" / label
        directory.mkdir(parents=True)
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": "shader.dxbc",
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            **_complete_interface(),
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        if include_scope:
            value.update(
                {
                    "gameId": "example-game",
                    "buildId": "build-1",
                    "platform": "windows-x64",
                }
            )
        capture_id = capture_identity(value, [shader])
        value["captureId"] = capture_id
        path = directory / "catalog.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        returned_shader = dict(shader)
        returned_shader["rawPath"] = str(blob_path)
        return path, returned_shader, capture_id

    def _workspace_status(
        self,
        label: str,
        capture_id: str,
        shader: dict,
        *,
        flowchart: bytes | None = b"flowchart TD\n  A[Input] --> B[Color]\n",
    ) -> dict:
        workspace = self.root / "source-workspaces" / label
        (workspace / "source").mkdir(parents=True)
        source_path = workspace / "source" / "shader.hlsl"
        source_path.write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        (workspace / "ai").mkdir()
        (workspace / "ai" / "analysis.md").write_text(
            "Explanatory analysis only.\n", encoding="utf-8"
        )
        if flowchart is not None:
            (workspace / "ai" / "flowchart.mmd").write_bytes(flowchart)
        manifest_path = workspace / "manifest.json"
        manifest_path.write_text('{"validated":true}\n', encoding="utf-8")
        fingerprint = fingerprint_bytes(Path(shader["rawPath"]).read_bytes())
        self.assertIsNotNone(fingerprint)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(workspace),
            "manifestPath": str(manifest_path),
            "workspaceId": hashlib.md5(label.encode("utf-8")).hexdigest(),
            "capture": {"captureId": capture_id},
            "shader": {
                "resourceId": shader["shaderResourceId"],
                "rawSha256": fingerprint.raw_sha256,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "targetProfile": {
                "kind": "DXBC",
                "stage": "fragment",
                "profile": "ps_5_0",
            },
            "source": {
                "present": True,
                "path": str(source_path),
                "format": "hlsl",
                "lane": "executable-editable",
                "currentSha256": source_hash,
                "decompiledSha256": source_hash,
                "modifiedSinceDecompile": False,
            },
            "baseline": {
                "rawSha256": fingerprint.raw_sha256,
                "codeSha256": fingerprint.code_sha256,
                "abiSha256": "a" * 64,
            },
        }

    def test_first_use_creates_persistent_pending_workspace(self) -> None:
        first = self._pending()
        second = self.service.ensure_pending("Ignored New Name")

        self.assertEqual(first["schema"], PROJECT_WORKSPACE_SCHEMA)
        self.assertEqual(first["workspaceId"], second["workspaceId"])
        self.assertEqual(first["projectRoot"], second["projectRoot"])
        self.assertEqual(first["state"], "pending-scope")
        self.assertFalse(first["scopeConfigured"])
        self.assertFalse(first["policy"]["physicalKnowledgeReuseAllowed"])
        self.assertFalse(first["policy"]["crossGameAutomaticPhysicalReuse"])
        self.assertFalse(first["policy"]["aiProvisionalAutoApply"])
        for key in (
            "captureCatalogs",
            "shaderWorkspaces",
            "sharedDictionaries",
            "sharedExplanatory",
        ):
            self.assertTrue(Path(first["paths"][key]).is_dir())
        self.assertTrue(self.state_path.is_file())

    def test_launcher_project_library_lists_activates_and_tracks_rdc_references(self) -> None:
        captures = self.root / "external-captures"
        captures.mkdir()
        frame_a = captures / "frame-a.rdc"
        frame_b = captures / "frame-b.RDC"
        frame_a.write_bytes(b"rdc-a")
        frame_b.write_bytes(b"rdc-b")
        assets = self.root / "unpacked-assets"
        assets.mkdir()

        created = self.service.create_project(
            name="Example Launcher Project",
            game_id="example-game",
            build_id="shipping-42",
            platform="windows-x64",
            api_family="d3d11",
            asset_root=assets,
            capture_files=[frame_a],
        )
        self.assertEqual("bound", created["state"])
        self.assertEqual(str(assets.resolve()), created["assetRoot"])
        self.assertEqual(CAPTURE_LIBRARY_SCHEMA, created["captureLibrary"]["schema"])
        self.assertEqual(1, created["captureLibrary"]["count"])
        self.assertTrue(created["captureLibrary"]["captures"][0]["available"])
        self.assertEqual(
            "embedded-lazy",
            created["captureLibrary"]["captures"][0]["thumbnail"]["state"],
        )

        updated = self.service.add_capture_files(
            created["projectRoot"], [frame_a, frame_b]
        )
        self.assertEqual(2, updated["captureLibrary"]["count"])
        listed = self.service.list_projects()
        self.assertEqual("replaylab-project-list/v1", listed["schema"])
        self.assertEqual(1, listed["count"])
        self.assertTrue(listed["projects"][0]["active"])
        self.assertEqual(created["workspaceId"], listed["projects"][0]["workspaceId"])

        frame_b.unlink()
        refreshed = self.service.project_status(created["projectRoot"])
        availability = {
            Path(item["path"]).name: item["available"]
            for item in refreshed["captureLibrary"]["captures"]
        }
        self.assertEqual({"frame-a.rdc": True, "frame-b.RDC": False}, availability)

    def test_custom_project_path_registers_and_unregister_keeps_every_file(self) -> None:
        project_root = self.root / "shared-location" / "项目 A"
        capture = self.root / "external.rdc"
        capture.write_bytes(b"portable capture reference")
        with mock.patch(
            "src.active_projects._capture_content_identity",
            side_effect=AssertionError("adding a reference must not hash the RDC"),
        ):
            created = self.service.create_project(
                name="Portable Project",
                game_id="",
                build_id="",
                project_root=project_root,
                capture_files=[capture],
            )
            self.service.add_capture_files(created["projectRoot"], [capture])

        manifest_path = project_root / ".replaylab" / "workspace.json"
        manifest_before = manifest_path.read_bytes()
        removed = self.service.unregister_project(created["projectRoot"])
        self.assertTrue(removed["removed"])
        self.assertFalse(removed["projectFilesDeleted"])
        self.assertTrue(project_root.is_dir())
        self.assertEqual(manifest_before, manifest_path.read_bytes())
        self.assertTrue(capture.is_file())
        self.assertFalse(self.state_path.exists())
        self.assertEqual(0, self.service.list_projects()["count"])

        restored = self.service.register_existing_project(project_root)
        self.assertEqual(created["workspaceId"], restored["workspaceId"])
        self.assertEqual(1, self.service.list_projects()["count"])

    def test_moved_project_stays_removable_and_can_be_registered_at_new_path(self) -> None:
        original_root = self.root / "portable" / "before-move"
        moved_root = self.root / "portable" / "after-move"
        created = self.service.create_project(
            name="Moved Project",
            game_id="",
            build_id="",
            project_root=original_root,
        )
        original_root.rename(moved_root)

        listed = self.service.list_projects()
        self.assertEqual(0, listed["count"])
        self.assertEqual(1, len(listed["rejected"]))
        self.assertEqual(str(original_root.resolve()), listed["rejected"][0]["projectRoot"])
        self.assertEqual(
            "project-validation-failed", listed["rejected"][0]["reasonId"])
        self.assertEqual(
            listed["rejected"][0]["reason"],
            listed["rejected"][0]["reasonDetail"],
        )

        removed = self.service.unregister_project(original_root)
        self.assertFalse(removed["projectFilesDeleted"])
        self.assertTrue(moved_root.is_dir())
        restored = self.service.register_existing_project(moved_root)
        self.assertEqual(created["workspaceId"], restored["workspaceId"])
        self.assertEqual(1, self.service.list_projects()["count"])

    def test_remove_capture_references_supports_missing_files_without_deleting_rdc(self) -> None:
        first = self.root / "first.rdc"
        second = self.root / "second.rdc"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        project = self.service.create_project(
            name="Capture Removal",
            game_id="",
            build_id="",
            capture_files=[first, second],
        )
        records = {
            Path(record["path"]).name: record["captureRefId"]
            for record in project["captureLibrary"]["captures"]
        }
        second.unlink()
        updated = self.service.remove_capture_references(
            project["projectRoot"], [records["first.rdc"], records["second.rdc"]]
        )
        self.assertEqual(0, updated["captureLibrary"]["count"])
        self.assertEqual(b"first", first.read_bytes())
        self.assertFalse(second.exists())

    def test_missing_registry_bootstraps_legacy_projects_once(self) -> None:
        project = self.service.create_pending(
            self.projects_home / "legacy-project", "Legacy Project"
        )
        self.service.registry_path.unlink()
        migrated = ActiveReplayProjectService(self.state_path, self.projects_home)
        listed = migrated.list_projects()
        self.assertEqual(1, listed["count"])
        self.assertEqual(project["workspaceId"], listed["projects"][0]["workspaceId"])
        migrated.unregister_project(project["projectRoot"])
        self.assertEqual(0, migrated.list_projects()["count"])
        self.assertTrue(Path(project["projectRoot"]).is_dir())

    def test_launcher_project_uses_private_workspace_scope_and_validates_rdc(self) -> None:
        pending = self.service.create_project(
            name="Project Scoped Launcher", game_id="", build_id=""
        )
        self.assertEqual("bound", pending["state"])
        self.assertTrue(pending["scopeConfigured"])
        self.assertEqual("replaylab-project", pending["scope"]["gameId"])
        self.assertEqual(pending["workspaceId"], pending["scope"]["buildId"])
        with self.assertRaisesRegex(ActiveProjectError, "both be supplied"):
            self.service.create_project(
                name="Partial Scope", game_id="game", build_id=""
            )
        bad = self.root / "not-a-capture.txt"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, r"\.rdc"):
            self.service.create_project(
                name="Bad Project",
                game_id="game",
                build_id="build",
                capture_files=[bad],
            )
        self.assertEqual(1, len(list(self.projects_home.iterdir())))

    def test_capture_auto_match_uses_one_unambiguous_registered_path(self) -> None:
        capture = self.root / "replaceable.rdc"
        capture.write_bytes(b"capture-one")
        first = self.service.create_project(
            name="First Game", game_id="game-a", build_id="build-a",
            capture_files=[capture],
        )
        second = self.service.create_project(
            name="Second Game", game_id="game-b", build_id="build-b"
        )

        matched = self.service.match_and_activate_capture(capture)
        self.assertTrue(matched["activated"])
        self.assertFalse(matched["contentIdentityCacheHit"])
        self.assertEqual("unique-normalised-path", matched["associationMode"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        old_stat = capture.stat()
        capture.write_bytes(b"capture-two")
        os.utime(
            capture,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1000000000),
        )
        self.service.activate(second["projectRoot"])
        replaced = self.service.match_and_activate_capture(capture)
        self.assertTrue(replaced["activated"])
        self.assertEqual(1, replaced["matchCount"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        self.service.activate(second["projectRoot"])
        expired = self.service.match_and_activate_capture(
            capture, activation_guard=lambda: False
        )
        self.assertEqual(1, expired["matchCount"])
        self.assertFalse(expired["activated"])
        self.assertEqual(second["workspaceId"], self.service.status()["workspaceId"])

        self.service.add_capture_files(second["projectRoot"], [capture])
        self.service.activate(second["projectRoot"])
        ambiguous = self.service.match_and_activate_capture(capture)
        self.assertFalse(ambiguous["activated"])
        self.assertEqual(2, ambiguous["matchCount"])
        self.assertEqual(second["workspaceId"], self.service.status()["workspaceId"])

    def test_legacy_path_only_capture_is_compatible_with_path_association(self) -> None:
        capture = self.root / "legacy.rdc"
        capture.write_bytes(b"legacy")
        project = self.service.create_project(
            name="Legacy", game_id="game", build_id="build",
            capture_files=[capture],
        )
        library_path = Path(project["projectRoot"]) / ".replaylab" / "capture-library.json"
        library = json.loads(library_path.read_text(encoding="utf-8"))
        library["schemaVersion"] = 1
        library["captures"][0].pop("contentIdentity")
        library["librarySha256"] = _canonical_hash(library, "librarySha256")
        library_path.write_text(json.dumps(library), encoding="utf-8")

        self.service.create_project(
            name="Other", game_id="other", build_id="other"
        )
        result = self.service.match_and_activate_capture(capture)
        self.assertTrue(result["activated"])
        self.assertEqual(1, result["matchCount"])
        self.assertEqual(project["workspaceId"], self.service.status()["workspaceId"])
        upgraded = self.service.add_capture_files(project["projectRoot"], [capture])
        self.assertEqual(2, upgraded["captureLibrary"]["schemaVersion"])
        self.assertIsNone(
            upgraded["captureLibrary"]["captures"][0]["contentIdentity"]
        )
        self.assertTrue(self.service.match_and_activate_capture(capture)["activated"])

    def test_create_project_rolls_back_publication_and_active_selection(self) -> None:
        existing = self.service.create_project(
            name="Existing", game_id="game", build_id="build"
        )
        before_directories = {item.name for item in self.projects_home.iterdir()}
        original_write = self.service._write_selection

        def fail_new_selection(root: Path, workspace_id: str) -> None:
            if workspace_id != existing["workspaceId"]:
                raise ActiveProjectError("simulated selection publish failure")
            original_write(root, workspace_id)

        with mock.patch.object(
            self.service, "_write_selection", side_effect=fail_new_selection
        ):
            with self.assertRaisesRegex(ActiveProjectError, "simulated"):
                self.service.create_project(
                    name="Must Roll Back", game_id="new-game", build_id="new-build"
                )
        self.assertEqual(
            before_directories, {item.name for item in self.projects_home.iterdir()}
        )
        self.assertEqual(existing["workspaceId"], self.service.status()["workspaceId"])

        original_status = self.service._project_status

        def fail_after_selection(project_root: str | Path):
            if Path(project_root).name.startswith("late-failure"):
                raise ActiveProjectError("simulated post-selection validation failure")
            return original_status(project_root)

        with mock.patch.object(
            self.service, "_project_status", side_effect=fail_after_selection
        ):
            with self.assertRaisesRegex(ActiveProjectError, "post-selection"):
                self.service.create_project(
                    name="Late Failure", game_id="new-game", build_id="new-build"
                )
        self.assertEqual(
            before_directories, {item.name for item in self.projects_home.iterdir()}
        )
        self.assertEqual(existing["workspaceId"], self.service.status()["workspaceId"])

    def test_failed_create_quarantine_is_never_listed_if_cleanup_fails(self) -> None:
        existing = self.service.create_project(
            name="Existing Visible", game_id="game", build_id="build"
        )
        original_status = self.service._project_status
        original_rmtree = shutil.rmtree

        def fail_after_selection(project_root: str | Path):
            if Path(project_root).name.startswith("quarantine-me"):
                raise ActiveProjectError("simulated final validation failure")
            return original_status(project_root)

        def leave_quarantine(path, *args, **kwargs):
            if ".failed-" in Path(path).name:
                return None
            return original_rmtree(path, *args, **kwargs)

        with mock.patch.object(
            self.service, "_project_status", side_effect=fail_after_selection
        ), mock.patch("src.active_projects.shutil.rmtree", side_effect=leave_quarantine):
            with self.assertRaisesRegex(ActiveProjectError, "final validation"):
                self.service.create_project(
                    name="Quarantine Me", game_id="new", build_id="new"
                )
        hidden = [
            item for item in self.projects_home.iterdir()
            if item.name.startswith(".quarantine-me.failed-")
        ]
        self.assertEqual(1, len(hidden))
        listed = self.service.list_projects()
        self.assertEqual(1, listed["count"])
        self.assertEqual(existing["workspaceId"], listed["projects"][0]["workspaceId"])
        original_rmtree(hidden[0])

    def test_project_settings_serialise_with_pinned_active_operation(self) -> None:
        active = self.service.create_project(
            name="Pending Settings", game_id="", build_id=""
        )
        assets = self.root / "serial-assets"
        assets.mkdir()
        started = threading.Event()
        finished = threading.Event()

        def configure() -> None:
            started.set()
            self.service.configure_project_settings(
                active["projectRoot"], asset_root=assets
            )
            finished.set()

        worker = threading.Thread(target=configure, daemon=True)
        with self.service.pin_active_project(
            active["workspaceId"], active["projectRoot"]
        ):
            worker.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(finished.wait(0.1))
            self.assertIsNone(self.service.status()["assetRoot"])
        self.assertTrue(finished.wait(2.0))
        worker.join(timeout=1.0)
        self.assertEqual(str(assets.resolve()), self.service.status()["assetRoot"])

    def test_explicit_project_settings_are_atomic_and_never_switch_active(self) -> None:
        first = self.service.create_project(
            name="First", game_id="game-a", build_id="build-a"
        )
        second = self.service.create_pending(
            self.projects_home / "second-pending", "Second"
        )
        self.service.activate(first["projectRoot"])
        missing_assets = self.root / "missing-assets"

        with self.assertRaisesRegex(ActiveProjectError, "asset root"):
            self.service.configure_project_settings(
                second["projectRoot"],
                game_id="game-b",
                build_id="build-b",
                asset_root=missing_assets,
            )
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])
        unchanged = self.service.project_status(second["projectRoot"])
        self.assertEqual("pending-scope", unchanged["state"])
        self.assertFalse(Path(unchanged["paths"]["replayProject"]).exists())

        assets = self.root / "second-assets"
        assets.mkdir()
        configured = self.service.configure_project_settings(
            second["projectRoot"],
            game_id="game-b",
            build_id="build-b",
            platform="windows-x64",
            api_family="d3d11",
            asset_root=assets,
        )
        self.assertEqual("bound", configured["state"])
        self.assertEqual(str(assets.resolve()), configured["assetRoot"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        with self.assertRaisesRegex(ActiveProjectError, "scope is immutable"):
            self.service.configure_project_settings(
                second["projectRoot"],
                game_id="game-b",
                build_id="different-build",
            )
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

    def test_project_pin_blocks_activation_until_operation_finishes(self) -> None:
        first = self._pending()
        second = self.service.create_pending(
            self.projects_home / "second-project", "Second Project"
        )
        self.service.activate(first["projectRoot"])
        started = threading.Event()
        finished = threading.Event()

        def switch_project() -> None:
            started.set()
            self.service.activate(second["projectRoot"])
            finished.set()

        worker = threading.Thread(target=switch_project, daemon=True)
        with self.service.pin_active_project(
            first["workspaceId"], first["projectRoot"]
        ):
            worker.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(finished.wait(0.1))
            self.assertEqual(self.service.status()["workspaceId"], first["workspaceId"])
        self.assertTrue(finished.wait(2.0))
        worker.join(timeout=1.0)
        self.assertEqual(self.service.status()["workspaceId"], second["workspaceId"])

    def test_pending_project_blocks_physical_reuse_but_can_stage_catalog(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "pending-frame", _dxbc(), resource_id=1, include_scope=False
        )
        staged = self.service.stage_capture_catalog(catalog)
        self.assertEqual(staged["captureId"], capture_id)
        self.assertFalse(staged["physicalKnowledgeImportAllowed"])
        self.assertTrue(Path(staged["catalogPath"]).is_file())

        plan = self.service.future_frame_plan()
        self.assertEqual(plan["schema"], FUTURE_FRAME_PLAN_SCHEMA)
        self.assertTrue(plan["blocked"])
        self.assertEqual(plan["preparationSteps"], [])
        with self.assertRaisesRegex(ActiveProjectError, "scope is not configured"):
            self.service.import_capture(catalog)

    def test_staging_enforces_expected_live_catalog_binding_before_publish(self) -> None:
        self._pending()
        cases = (
            {
                "label": "wrong-id",
                "expected_capture_id": "0" * 64,
                "expected_capture_path": None,
                "expected_api": None,
                "message": "expected exported identity",
            },
            {
                "label": "wrong-path",
                "expected_capture_id": None,
                "expected_capture_path": str(self.root / "other.rdc"),
                "expected_api": None,
                "message": "path differs",
            },
            {
                "label": "wrong-api",
                "expected_capture_id": None,
                "expected_capture_path": None,
                "expected_api": "Vulkan",
                "message": "API differs",
            },
        )
        for index, case in enumerate(cases, start=1):
            with self.subTest(case=case["label"]):
                catalog, unused_shader, capture_id = self._catalog(
                    str(case["label"]), _dxbc(bytes([index])), resource_id=index
                )
                kwargs = {
                    key: case[key]
                    for key in (
                        "expected_capture_id",
                        "expected_capture_path",
                        "expected_api",
                    )
                    if case[key] is not None
                }
                with self.assertRaisesRegex(ActiveProjectError, str(case["message"])):
                    self.service.stage_capture_catalog(catalog, **kwargs)
                self.assertFalse(
                    self.service.capture_catalog_path(capture_id).parent.exists()
                )

    def test_scope_binding_is_immutable_and_wraps_replay_project(self) -> None:
        bound = self._bound()
        self.assertEqual(bound["state"], "bound")
        self.assertEqual(bound["scope"]["gameId"], "example-game")
        self.assertEqual(bound["scope"]["apiFamily"], "d3d11")
        replay_root = Path(bound["paths"]["replayProject"])
        self.assertTrue((replay_root / "project.json").is_file())
        self.assertEqual(self.service.replay_service().status()["scope"], bound["scope"])

        idempotent = self.service.configure_scope(
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family="Direct3D 11",
        )
        self.assertEqual(idempotent["workspaceId"], bound["workspaceId"])
        with self.assertRaisesRegex(ActiveProjectError, "scope is immutable"):
            self.service.configure_scope(
                game_id="another-game",
                build_id="build-1",
                platform="windows-x64",
                api_family="d3d11",
            )

    def test_capture_import_is_content_addressed_and_api_can_be_pinned(self) -> None:
        self._bound(api_family="")
        catalog, unused_shader, capture_id = self._catalog(
            "frame-a", _dxbc(), resource_id=11
        )
        first = self.service.import_capture(catalog)
        staged_path = Path(first["stagedCatalog"]["catalogPath"])
        staged_value = json.loads(staged_path.read_text(encoding="utf-8"))
        staged_blob = staged_path.parent / staged_value["shaders"][0]["rawPath"]
        self.assertTrue(staged_blob.is_file())
        self.assertTrue(staged_blob.resolve().is_relative_to(staged_path.parent.resolve()))
        self.assertEqual(first["reusePlan"]["captureId"], capture_id)
        self.assertTrue(first["physicalKnowledgeImported"])
        self.assertFalse(first["autoApplied"])
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "d3d11")

        second = self.service.import_capture(catalog)
        self.assertTrue(second["stagedCatalog"]["idempotent"])
        self.assertTrue(second["reusePlan"]["idempotent"])

    def test_same_capture_id_preserves_distinct_catalog_revisions(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "catalog-revisions", _dxbc(), resource_id=111
        )

        first = self.service.stage_capture_catalog(catalog)
        first_path = Path(first["catalogPath"])
        first_bytes = first_path.read_bytes()

        changed = json.loads(catalog.read_text(encoding="utf-8"))
        # captureId deliberately excludes reflection/use/diagnostic payloads.
        # A corrected exporter may therefore produce another exact catalog
        # revision for the same Shader-set identity.
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")

        second = self.service.stage_capture_catalog(catalog)
        second_path = Path(second["catalogPath"])
        self.assertEqual(second["captureId"], capture_id)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(
            second["catalogRevisionSha256"], second["catalogSha256"]
        )
        self.assertEqual(second_path.parent.name, second["catalogSha256"])

        repeated = self.service.stage_capture_catalog(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["catalogPath"], second["catalogPath"])

    def test_pending_catalog_revision_limit_fails_before_publish(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "catalog-revision-limit", _dxbc(), resource_id=112
        )
        first = self.service.stage_capture_catalog(catalog)
        changed = json.loads(catalog.read_text(encoding="utf-8"))
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")

        with mock.patch(
            "src.active_projects.MAX_STAGED_CATALOG_REVISIONS", 1
        ):
            with self.assertRaisesRegex(ActiveProjectError, "revision limit"):
                self.service.stage_capture_catalog(catalog)

        expected_payload = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(expected_payload["captureId"], capture_id)
        self.assertTrue(Path(first["catalogPath"]).is_file())

    def test_first_capture_scope_pin_recovers_after_snapshot_update_crash(self) -> None:
        bound = self._bound(api_family="")
        catalog, unused_shader, unused_capture = self._catalog(
            "scope-recovery", _dxbc(), resource_id=12
        )
        staged = self.service.stage_capture_catalog(catalog)

        # Simulate a process stopping after the authoritative replay project
        # imported the capture, but before the active workspace copied the API
        # refinement into its portable configuration snapshot.
        from src.projects import ReplayProjectService

        ReplayProjectService(bound["paths"]["replayProject"]).import_capture(
            staged["catalogPath"]
        )
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "")

        recovered = self.service.import_capture(catalog)
        self.assertTrue(recovered["reusePlan"]["idempotent"])
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "d3d11")

    def test_deterministic_paths_preserve_distinct_shader_resource_ids(self) -> None:
        status = self._pending()
        capture = "1" * 64
        raw = "2" * 64
        path_a = self.service.shader_workspace_path(
            capture_id=capture,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
            create=True,
        )
        path_b = self.service.shader_workspace_path(
            capture_id=capture,
            stage="Pixel Shader",
            shader_resource_id=20,
            raw_sha256=raw,
            create=True,
        )
        self.assertNotEqual(path_a, path_b)
        self.assertTrue(path_a.is_dir())
        self.assertTrue(path_b.is_dir())
        revision_a = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        revision_b = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="4" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        self.assertNotEqual(revision_a, revision_b)
        self.assertEqual(revision_a.parent.name, "v2")
        self.assertEqual(revision_b.parent.name, "v2")
        self.assertRegex(revision_a.name, r"^[0-9a-f]{64}$")
        self.assertRegex(revision_b.name, r"^[0-9a-f]{64}$")
        route_a = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            route_binding_sha256="5" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        route_b = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            route_binding_sha256="6" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        self.assertNotEqual(route_a, route_b)
        self.assertEqual(route_a.parent.name, "v2")
        self.assertEqual(route_b.parent.name, "v2")
        self.assertRegex(route_a.name, r"^[0-9a-f]{64}$")
        self.assertRegex(route_b.name, r"^[0-9a-f]{64}$")
        if os.name == "nt":
            longest_fixed_job_output = (
                route_a
                / "jobs"
                / ("decompile-h99-r9999-" + ("f" * 32))
                / "output.dxil.ll"
            )
            self.assertLess(len(str(longest_fixed_job_output)), 248)
        self.assertEqual(
            route_a,
            self.service.shader_workspace_path(
                capture_id=capture,
                catalog_sha256="3" * 64,
                route_binding_sha256="5" * 64,
                stage="Pixel Shader",
                shader_resource_id=10,
                raw_sha256=raw,
            ),
        )
        self.assertTrue(
            path_a.resolve().is_relative_to(
                Path(status["paths"]["shaderWorkspaces"]).resolve()
            )
        )
        self.assertEqual(
            self.service.shared_dictionary_directory("aliases"),
            self.service.shared_dictionary_directory("aliases"),
        )
        with self.assertRaisesRegex(ActiveProjectError, "unknown shared"):
            self.service.shared_dictionary_directory("../outside")

    def test_asset_root_is_explicit_read_only_and_boundary_checked(self) -> None:
        status = self._pending()
        asset_root = self.root / "unpacked-game"
        (asset_root / "Shaders").mkdir(parents=True)
        asset_file = asset_root / "Shaders" / "base.bin"
        asset_file.write_bytes(b"asset")

        configured = self.service.configure_asset_root(asset_root)
        self.assertEqual(Path(configured["assetRoot"]), asset_root.resolve())
        self.assertEqual(
            self.service.resolve_asset_path("Shaders/base.bin"), asset_file.resolve()
        )
        with self.assertRaisesRegex(ActiveProjectError, "safe relative"):
            self.service.resolve_asset_path("../outside.bin")
        with self.assertRaisesRegex(ActiveProjectError, "must not contain"):
            self.service.configure_asset_root(self.root)
        self.assertTrue(
            Path(status["projectRoot"]).resolve().is_relative_to(
                self.projects_home.resolve()
            )
        )

    def test_active_selection_and_workspace_hashes_detect_tampering(self) -> None:
        status = self._pending()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["projectRoot"] = str(self.root / "outside")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "selection hash mismatch"):
            self.service.status()

        # Restore a valid selection, then tamper with the portable manifest.
        self.service.activate(status["projectRoot"])
        manifest_path = Path(status["paths"]["workspaceManifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = "tampered"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "manifest hash mismatch"):
            self.service.status()

    def test_rehashed_layout_escape_is_still_rejected(self) -> None:
        status = self._pending()
        manifest_path = Path(status["paths"]["workspaceManifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["layout"]["sharedDictionaries"] = "../../outside"
        manifest["configSha256"] = _canonical_hash(manifest, "configSha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "layout is invalid"):
            self.service.status()

    def test_external_project_must_be_registered_before_activation(self) -> None:
        other_home = self.root / "other-projects"
        other = ActiveReplayProjectService(
            self.root / "other-state.json", other_home
        )
        outside = other.create_pending(other_home / "outside")
        with self.assertRaisesRegex(ActiveProjectError, "not registered"):
            self.service.activate(outside["projectRoot"])
        registered = self.service.register_existing_project(outside["projectRoot"])
        activated = self.service.activate(registered["projectRoot"])
        self.assertEqual(outside["workspaceId"], activated["workspaceId"])

    def test_activate_validates_each_project_snapshot_once(self) -> None:
        from src.projects import ReplayProjectService

        project = self._bound()
        original_read_workspace = self.service._read_workspace
        original_replay_status = ReplayProjectService.status
        calls = {"workspace": 0, "replayStatus": 0}

        def read_workspace_once(*args, **kwargs):
            calls["workspace"] += 1
            return original_read_workspace(*args, **kwargs)

        def replay_status_once(instance, *args, **kwargs):
            calls["replayStatus"] += 1
            return original_replay_status(instance, *args, **kwargs)

        with mock.patch.object(
            self.service, "_read_workspace", side_effect=read_workspace_once
        ), mock.patch.object(
            ReplayProjectService,
            "status",
            autospec=True,
            side_effect=replay_status_once,
        ):
            activated = self.service.activate(project["projectRoot"])

        self.assertEqual({"workspace": 1, "replayStatus": 1}, calls)
        self.assertEqual(project["workspaceId"], activated["workspaceId"])
        self.assertIn("captureLibrary", activated)
        self.assertIsNotNone(activated["replayProject"])

    def test_future_frame_plan_includes_verified_candidates_and_flowchart(self) -> None:
        self._bound()
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_b = _dxbc(b"same program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "reuse-a", blob_a, resource_id=101
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "reuse-b", blob_b, resource_id=909
        )
        self.service.import_capture(catalog_a)
        workspace_status = self._workspace_status(
            "workspace-a", capture_a, shader_a
        )
        registered = self.service.register_workspace(workspace_status)
        self.assertEqual(len(registered["flowchartArtifacts"]), 1)
        self.assertFalse(
            registered["flowchartArtifacts"][0]["policy"]["physicalEvidence"]
        )

        alias_path = self.root / "verified-aliases.json"
        alias_path.write_text(
            json.dumps(
                {
                    "schema": ALIAS_SCHEMA,
                    "schemaVersion": ALIAS_VERSION,
                    "verifiedOnly": True,
                    "captureId": capture_a,
                    "aliases": [
                        {
                            "verified": True,
                            "captureId": capture_a,
                            "shaderResourceId": 101,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "rawSha256": shader_a["rawSha256"],
                            "codeSha256": shader_a["codeSha256"],
                            "bindingKind": "constantBufferVariable",
                            "registerSpace": 0,
                            "bindNumber": 0,
                            "byteOffset": 0,
                            "canonicalPath": "cb0._child0",
                            "alias": "BaseColor",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.service.register_alias_table(alias_path)
        self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": capture_a,
                "captureSessionId": "b" * 32,
                "shaderResourceId": 101,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "c" * 64,
                "parameters": [
                    {
                        "parameterId": "d" * 32,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [1.0, 0.5, 0.25, 1.0],
                    }
                ],
            },
            ["d" * 32],
        )

        imported = self.service.import_capture(catalog_b)
        plan = imported["reusePlan"]
        self.assertEqual(plan["matchedShaderCount"], 1)
        match = plan["matches"][0]
        source = match["workspaceCandidates"][0]
        self.assertTrue(source["sourceSeedEligible"])
        self.assertEqual(len(source["flowchartCandidates"]), 1)
        flowchart = source["flowchartCandidates"][0]
        self.assertTrue(Path(flowchart["path"]).is_file())
        self.assertTrue(flowchart["explanatoryOnly"])
        self.assertFalse(flowchart["physicalEvidence"])
        self.assertFalse(flowchart["applyAuthorization"])
        self.assertEqual(len(match["aliasCandidates"]), 1)
        self.assertEqual(len(match["materialCandidates"]), 1)
        kinds = {item["kind"] for item in plan["preparationSteps"]}
        self.assertEqual(
            kinds,
            {
                "seed-reviewed-hlsl",
                "load-verified-alias-candidate",
                "list-material-value-candidate",
            },
        )
        self.assertFalse(plan["activeProjectPolicy"]["physicalMutationAutoApply"])
        self.assertFalse(plan["activeProjectPolicy"]["aiProvisionalAutoApply"])

        fetched = self.service.future_frame_plan(capture_b)
        self.assertFalse(fetched["blocked"])
        self.assertEqual(len(fetched["preparationSteps"]), 3)

    def test_invalid_flowchart_is_rejected_before_workspace_registration(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "bad-flowchart", _dxbc(), resource_id=77
        )
        self.service.import_capture(catalog)
        workspace_status = self._workspace_status(
            "workspace-bad-flowchart",
            capture_id,
            shader,
            flowchart=b"flowchart TD\x00A-->B\n",
        )
        with self.assertRaisesRegex(ActiveProjectError, "NUL"):
            self.service.register_workspace(workspace_status)
        self.assertEqual(
            self.service.replay_service().status()["knowledgeCounts"]["workspaces"],
            0,
        )

    def test_ai_provisional_alias_cannot_enter_active_project(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "provisional", _dxbc(), resource_id=88
        )
        self.service.import_capture(catalog)
        alias_path = self.root / "provisional-alias.json"
        alias_path.write_text(
            json.dumps(
                {
                    "schema": ALIAS_SCHEMA,
                    "schemaVersion": ALIAS_VERSION,
                    "verifiedOnly": False,
                    "captureId": capture_id,
                    "aliases": [
                        {
                            "verified": False,
                            "status": "AIProvisional",
                            "captureId": capture_id,
                            "shaderResourceId": 88,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "rawSha256": shader["rawSha256"],
                            "bindingKind": "constantBufferVariable",
                            "registerSpace": 0,
                            "bindNumber": 0,
                            "byteOffset": 0,
                            "canonicalPath": "cb0._child0",
                            "alias": "MaybeRoughness",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ActiveProjectError, "verified exported"):
            self.service.register_alias_table(alias_path)

    def test_content_addressed_flowchart_is_hash_verified_on_load(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "flowchart-tamper", _dxbc(), resource_id=99
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status("workspace-flowchart", capture_id, shader)
        )
        record = registered["flowchartArtifacts"][0]
        project_root = Path(self.service.status()["projectRoot"])
        artifact = project_root / record["artifactPath"]
        artifact.write_text("flowchart TD\n  X-->Y\n", encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "artifact hash mismatch"):
            self.service.status()


if __name__ == "__main__":
    unittest.main()
