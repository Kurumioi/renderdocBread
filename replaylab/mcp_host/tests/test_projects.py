from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from src import projects as projects_module
from src.projects import ReplayProjectError, ReplayProjectService
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, capture_identity


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


def _complete_interface(output_topology: str = "TriangleList") -> dict:
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
        "outputTopology": output_topology,
        "dynamicLinkageInterfaceCount": 0,
    }


class ReplayProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        ReplayProjectService.create(
            self.project,
            "multi-frame",
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family="d3d11",
        )
        self.service = ReplayProjectService(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_project_derives_active_knowledge_without_read_side_effects(self) -> None:
        manifest_path = self.project / "project.json"
        created = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            created["activeKnowledge"],
            {"revision": 0, "workspaces": {}, "materials": {}},
        )

        catalog, shader, capture_id = self._catalog(
            "legacy-active", _dxbc(), resource_id=91
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "legacy-active-workspace",
            capture_id,
            shader,
            "// legacy active source\n",
        )
        registered = self.service.register_workspace(status)
        legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy.pop("activeKnowledge")
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        before = manifest_path.read_bytes()

        self.service.status()

        self.assertEqual(manifest_path.read_bytes(), before)
        repeated = self.service.register_workspace(status)
        self.assertTrue(repeated["idempotent"])
        migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
        token = registered["knowledge"]["knowledgeId"]
        self.assertEqual(migrated["activeKnowledge"]["revision"], 2)
        self.assertEqual(migrated["activeKnowledge"]["workspaces"], {token: 2})
        self.assertEqual(migrated["activeKnowledge"]["materials"], {})

    def test_explicit_active_knowledge_corruption_fails_closed(self) -> None:
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["activeKnowledge"] = {
            "revision": 1,
            "workspaces": {"f" * 32: 1},
            "materials": {},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            ReplayProjectError, "activeKnowledge workspaces index is incomplete"
        ):
            self.service.status()

    def _catalog(
        self,
        label: str,
        blob: bytes,
        *,
        resource_id: int,
        topology: str = "TriangleList",
    ) -> tuple[Path, dict, str]:
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(blob_path),
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            **_complete_interface(topology),
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        capture_id = capture_identity(value, [shader])
        value["captureId"] = capture_id
        catalog_path = directory / "catalog.json"
        catalog_path.write_text(json.dumps(value), encoding="utf-8")
        return catalog_path, shader, capture_id

    def _catalog_revision(
        self,
        source_catalog: Path,
        label: str,
        *,
        field_base_type: str,
        indent: int | None = None,
    ) -> Path:
        value = json.loads(source_catalog.read_text(encoding="utf-8"))
        source_blob = Path(value["shaders"][0]["rawPath"])
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(source_blob.read_bytes())
        value["shaders"][0]["rawPath"] = str(blob_path)
        value["shaders"][0]["constantBuffers"][0]["fields"][0][
            "baseType"
        ] = field_base_type
        path = directory / "catalog.json"
        path.write_text(json.dumps(value, indent=indent), encoding="utf-8")
        return path

    def _workspace_status(
        self,
        label: str,
        capture_id: str,
        shader: dict,
        source_text: str,
    ) -> dict:
        workspace = self.root / label
        (workspace / "source").mkdir(parents=True)
        source_path = workspace / "source" / "shader.hlsl"
        source_path.write_text(source_text, encoding="utf-8")
        (workspace / "ai").mkdir()
        (workspace / "ai" / "analysis.md").write_text(
            "AI explanation only; verify against reflection.\n", encoding="utf-8"
        )
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

    def test_cross_frame_reuse_seeds_source_but_never_reuses_live_state(self) -> None:
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_b = _dxbc(b"same program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "frame-a", blob_a, resource_id=101
        )
        catalog_b, shader_b, capture_b = self._catalog(
            "frame-b", blob_b, resource_id=909
        )
        self.assertNotEqual(shader_a["rawSha256"], shader_b["rawSha256"])
        self.assertEqual(shader_a["codeSha256"], shader_b["codeSha256"])

        imported_a = self.service.import_capture(catalog_a)
        self.assertEqual(imported_a["matchedShaderCount"], 0)
        status_a = self._workspace_status(
            "workspace-a", capture_a, shader_a, "float4 main() : SV_Target { return 1; }\n"
        )
        workspace = self.service.register_workspace(status_a)

        alias_path = self.root / "aliases.json"
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
        material = self.service.save_material_knowledge(
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
        self.assertFalse(material["applied"])

        plan = self.service.import_capture(catalog_b)
        self.assertEqual(plan["matchedShaderCount"], 1)
        match = plan["matches"][0]
        self.assertEqual(
            match["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(
            match["workspaceCandidates"][0]["analysisCandidate"][
                "explanatoryOnly"
            ]
        )
        self.assertFalse(
            match["workspaceCandidates"][0]["analysisCandidate"][
                "physicalEvidence"
            ]
        )
        self.assertEqual(
            match["aliasCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(
            match["materialCandidates"][0][
                "materialLayoutLiveRevalidationRequired"
            ]
        )
        self.assertFalse(plan["policy"]["autoApplyAuthorized"])
        self.assertTrue(
            plan["policy"]["liveAbiRevalidationRequiredBeforeShaderApply"]
        )
        self.assertTrue(plan["policy"]["liveMaterialLayoutRevalidationRequired"])

        status_b = self._workspace_status(
            "workspace-b", capture_b, shader_b, "// fresh decompile\n"
        )
        seeded = self.service.seed_workspace(
            workspace["knowledge"]["knowledgeId"], status_b
        )
        self.assertFalse(seeded["compiled"])
        self.assertFalse(seeded["applied"])
        self.assertTrue(seeded["requiresCompile"])
        self.assertTrue(seeded["requiresLiveAbiRevalidation"])
        self.assertEqual(
            Path(status_b["source"]["path"]).read_text(encoding="utf-8"),
            "float4 main() : SV_Target { return 1; }\n",
        )

    def test_code_match_requires_complete_identical_interface_and_layout(self) -> None:
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_c = _dxbc(b"same program", b"frame-c reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "frame-a", blob_a, resource_id=1
        )
        catalog_c, unused_shader_c, unused_capture_c = self._catalog(
            "frame-c", blob_c, resource_id=2, topology="LineList"
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status(
            "workspace-a", capture_a, shader_a, "// edited source\n"
        )
        self.service.register_workspace(status)
        plan = self.service.import_capture(catalog_c)
        self.assertEqual(plan["matchedShaderCount"], 0)

    def test_exact_catalog_and_knowledge_cache_skips_full_correspondence_rebuild(self) -> None:
        blob_a = _dxbc(b"same cached program", b"frame-a reflection")
        blob_b = _dxbc(b"same cached program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "cache-frame-a", blob_a, resource_id=201
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "cache-frame-b", blob_b, resource_id=202
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "cache-workspace-a", capture_a, shader_a, "// cached source a\n"
            )
        )

        first = self.service.import_capture(catalog_b)
        self.assertFalse(first["derivedCache"]["hit"])
        self.assertTrue(first["derivedCache"]["stored"])
        self.assertEqual(first["matchedShaderCount"], 1)
        cache_path = Path(first["derivedCache"]["path"])
        self.assertTrue(cache_path.is_file())
        self.assertTrue(cache_path.is_relative_to(self.project))
        cache_value = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cache_value["catalogSha256"], first["catalogSha256"]
        )
        self.assertEqual(
            cache_value["knowledgeContentSha256"],
            first["derivedCache"]["knowledgeContentSha256"],
        )
        unsigned = {
            key: value for key, value in cache_value.items() if key != "sealSha256"
        }
        self.assertEqual(
            cache_value["sealSha256"],
            hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )

        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            side_effect=AssertionError("exact cache hit rebuilt all correspondences"),
        ):
            repeated = self.service.reuse_plan(
                capture_b, first["catalogSha256"]
            )
        self.assertTrue(repeated["derivedCache"]["hit"])
        self.assertEqual(
            repeated["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertFalse(repeated["derivedCache"]["physicalGateBypassed"])

    def test_reuse_cache_invalidates_on_knowledge_or_catalog_content_change(self) -> None:
        blob_a = _dxbc(b"same invalidation program", b"frame-a reflection")
        blob_b = _dxbc(b"same invalidation program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "invalidate-frame-a", blob_a, resource_id=301
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "invalidate-frame-b", blob_b, resource_id=302
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "invalidate-workspace-a",
                capture_a,
                shader_a,
                "// invalidation source a\n",
            )
        )
        initial = self.service.import_capture(catalog_b)
        initial_cache = initial["derivedCache"]

        latest_workspace = self.service.register_workspace(
            self._workspace_status(
                "invalidate-workspace-b",
                capture_a,
                shader_a,
                "// invalidation source b\n",
            )
        )
        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            wraps=self.service._build_reuse_mappings,
        ) as rebuilt:
            knowledge_changed = self.service.reuse_plan(
                capture_b, initial["catalogSha256"]
            )
        self.assertEqual(rebuilt.call_count, 1)
        self.assertFalse(knowledge_changed["derivedCache"]["hit"])
        self.assertNotEqual(
            knowledge_changed["derivedCache"]["knowledgeContentSha256"],
            initial_cache["knowledgeContentSha256"],
        )
        self.assertNotEqual(
            knowledge_changed["derivedCache"]["path"], initial_cache["path"]
        )
        workspace_candidates = knowledge_changed["matches"][0]["workspaceCandidates"]
        self.assertEqual(len(workspace_candidates), 1)
        self.assertEqual(
            workspace_candidates[0]["knowledgeId"],
            latest_workspace["knowledge"]["knowledgeId"],
        )
        self.assertTrue(workspace_candidates[0]["activeCandidate"])
        self.assertEqual(workspace_candidates[0]["supersededCandidateCount"], 1)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["workspaces"]), 2)

        changed_catalog = json.loads(catalog_b.read_text(encoding="utf-8"))
        changed_catalog["exportDiagnostics"] = {"revision": 2}
        catalog_b.write_text(json.dumps(changed_catalog), encoding="utf-8")
        catalog_changed = self.service.import_capture(catalog_b)
        self.assertEqual(catalog_changed["captureId"], capture_b)
        self.assertNotEqual(
            catalog_changed["catalogSha256"], knowledge_changed["catalogSha256"]
        )
        self.assertFalse(catalog_changed["derivedCache"]["hit"])
        self.assertNotEqual(
            catalog_changed["derivedCache"]["path"],
            knowledge_changed["derivedCache"]["path"],
        )

    def test_historical_workspace_reactivation_controls_head_and_digest(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "workspace-reactivation", _dxbc(), resource_id=311
        )
        imported = self.service.import_capture(catalog)
        status_a = self._workspace_status(
            "workspace-reactivation-a",
            capture_id,
            shader,
            "// source A\n",
        )
        status_b = self._workspace_status(
            "workspace-reactivation-b",
            capture_id,
            shader,
            "// source B\n",
        )
        record_a = self.service.register_workspace(status_a)["knowledge"]
        record_b = self.service.register_workspace(status_b)["knowledge"]
        before = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        before_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            before_plan["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_b["knowledgeId"],
        )

        repeated = self.service.register_workspace(status_a)
        self.assertTrue(repeated["idempotent"])
        after = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        after_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertNotEqual(
            before["knowledgeContentSha256"], after["knowledgeContentSha256"]
        )
        self.assertFalse(after_plan["derivedCache"]["hit"])
        self.assertEqual(
            after_plan["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_a["knowledgeId"],
        )

        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["workspaces"]), 2)
        activations = manifest["activeKnowledge"]["workspaces"]
        self.assertGreater(
            activations[record_a["knowledgeId"]],
            activations[record_b["knowledgeId"]],
        )
        manifest["knowledge"]["workspaces"].reverse()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        reordered = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            reordered["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_a["knowledgeId"],
        )

    def test_restore_binding_is_capture_scoped_and_hashes_sources_once(self) -> None:
        shared_source = _dxbc(b"scoped shared program", b"source reflection")
        shared_target = _dxbc(b"scoped shared program", b"target reflection")
        unrelated_blob = _dxbc(b"unrelated program", b"other reflection")
        source_catalog, source_shader, source_capture = self._catalog(
            "restore-scope-source", shared_source, resource_id=601
        )
        target_catalog, unused_target_shader, target_capture = self._catalog(
            "restore-scope-target", shared_target, resource_id=602
        )
        unrelated_catalog, unrelated_shader, unrelated_capture = self._catalog(
            "restore-scope-unrelated", unrelated_blob, resource_id=603
        )
        self.service.import_capture(source_catalog)
        target_import = self.service.import_capture(target_catalog)
        self.service.import_capture(unrelated_catalog)

        relevant_workspace = self.service.register_workspace(
            self._workspace_status(
                "restore-scope-workspace",
                source_capture,
                source_shader,
                "float4 main() : SV_Target { return 1; }\n",
            )
        )["knowledge"]
        relevant_parameter_id = "1" * 32
        relevant_material = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": source_capture,
                "captureSessionId": "2" * 32,
                "shaderResourceId": 601,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "3" * 64,
                "parameters": [
                    {
                        "parameterId": relevant_parameter_id,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [1.0, 0.5, 0.25, 1.0],
                    }
                ],
            },
            [relevant_parameter_id],
        )["knowledge"]

        before = self.service.restore_binding_state(
            target_capture, target_import["catalogSha256"]
        )
        self.assertRegex(before["knowledgeContentSha256"], r"^[0-9a-f]{64}$")
        relevant_source = (
            self.project / relevant_workspace["sourcePath"]
        ).resolve()
        with mock.patch.object(
            projects_module,
            "_file_bytes",
            wraps=projects_module._file_bytes,
        ) as file_reads:
            snapshot = self.service.restore_knowledge_snapshot(
                target_capture, target_import["catalogSha256"]
            )
        source_reads = [
            call
            for call in file_reads.call_args_list
            if call.args and Path(call.args[0]).resolve() == relevant_source
        ]
        self.assertEqual(len(source_reads), 1)
        self.assertEqual(
            set(snapshot["workspaces"]), {relevant_workspace["knowledgeId"]}
        )
        self.assertEqual(
            set(snapshot["materials"]), {relevant_material["knowledgeId"]}
        )

        original_source = relevant_source.read_bytes()
        try:
            relevant_source.write_bytes(original_source + b"// tampered\n")
            with self.assertRaisesRegex(
                ReplayProjectError, "workspace source hash mismatch"
            ):
                self.service.restore_knowledge_snapshot(
                    target_capture, target_import["catalogSha256"]
                )
        finally:
            relevant_source.write_bytes(original_source)

        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        tampered_manifest = json.loads(original_manifest.decode("utf-8"))
        tampered_material = next(
            item
            for item in tampered_manifest["knowledge"]["materials"]
            if item["knowledgeId"] == relevant_material["knowledgeId"]
        )
        tampered_material["parameters"][0]["value"][0] = 0.0
        try:
            manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ReplayProjectError, "material knowledgeId does not match"
            ):
                self.service.restore_knowledge_snapshot(
                    target_capture, target_import["catalogSha256"]
                )
        finally:
            manifest_path.write_bytes(original_manifest)

        unrelated_workspace = self.service.register_workspace(
            self._workspace_status(
                "restore-scope-unrelated-workspace",
                unrelated_capture,
                unrelated_shader,
                "float4 main() : SV_Target { return 0; }\n",
            )
        )["knowledge"]
        unrelated_parameter_id = "4" * 32
        unrelated_material = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": unrelated_capture,
                "captureSessionId": "5" * 32,
                "shaderResourceId": 603,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "6" * 64,
                "parameters": [
                    {
                        "parameterId": unrelated_parameter_id,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [0.0, 0.0, 0.0, 1.0],
                    }
                ],
            },
            [unrelated_parameter_id],
        )["knowledge"]

        after = self.service.restore_knowledge_snapshot(
            target_capture, target_import["catalogSha256"]
        )
        self.assertEqual(
            after["knowledgeContentSha256"], before["knowledgeContentSha256"]
        )
        self.assertNotIn(unrelated_workspace["knowledgeId"], after["workspaces"])
        self.assertNotIn(unrelated_material["knowledgeId"], after["materials"])

        self.service.register_workspace(
            self._workspace_status(
                "restore-scope-new-relevant-workspace",
                source_capture,
                source_shader,
                "float4 main() : SV_Target { return 0.5; }\n",
            )
        )
        changed = self.service.restore_binding_state(
            target_capture, target_import["catalogSha256"]
        )
        self.assertNotEqual(
            changed["knowledgeContentSha256"], before["knowledgeContentSha256"]
        )

    def test_reuse_plan_selects_latest_material_head_per_cbuffer(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "material-head", _dxbc(), resource_id=351
        )
        imported = self.service.import_capture(catalog)
        parameter_a = "a" * 32
        parameter_b = "b" * 32
        session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "captureId": capture_id,
            "captureSessionId": "c" * 32,
            "shaderResourceId": shader["shaderResourceId"],
            "stage": shader["stage"],
            "constantBlockPath": "cb0",
            "layoutSha256": "d" * 64,
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
        first = self.service.save_material_knowledge(session, [parameter_a])
        # Simulate a new Host/session whose in-memory dirty-leaf accumulator
        # knows only B. The project must merge durable head A under its lock.
        latest = self.service.save_material_knowledge(session, [parameter_b])
        self.assertEqual(
            [item["parameterId"] for item in latest["knowledge"]["parameters"]],
            [parameter_a, parameter_b],
        )

        plan = self.service.reuse_plan(capture_id, imported["catalogSha256"])
        candidates = plan["matches"][0]["materialCandidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["knowledgeId"], latest["knowledge"]["knowledgeId"]
        )
        self.assertTrue(candidates[0]["activeCandidate"])
        self.assertEqual(candidates[0]["supersededCandidateCount"], 1)
        self.assertEqual(candidates[0]["constantBlockPath"], "cb0")
        self.assertTrue(plan["policy"]["appendOnlyKnowledgeHistoryPreserved"])
        self.assertTrue(plan["policy"]["newestValidActiveHeadOnly"])
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["materials"]), 2)
        self.assertEqual(
            manifest["knowledge"]["materials"][0]["knowledgeId"],
            first["knowledge"]["knowledgeId"],
        )

        before_revert = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        session["parameters"][0]["value"] = 3.0
        changed = self.service.save_material_knowledge(session, [parameter_a])
        self.assertEqual(len(changed["knowledge"]["parameters"]), 2)
        session["parameters"][0]["value"] = 1.0
        reverted = self.service.save_material_knowledge(session, [parameter_a])
        self.assertTrue(reverted["idempotent"])
        self.assertEqual(
            reverted["knowledge"]["knowledgeId"], latest["knowledge"]["knowledgeId"]
        )
        after_revert = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        self.assertNotEqual(
            before_revert["knowledgeContentSha256"],
            after_revert["knowledgeContentSha256"],
        )
        reverted_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            reverted_plan["matches"][0]["materialCandidates"][0]["knowledgeId"],
            latest["knowledge"]["knowledgeId"],
        )
        final_manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(final_manifest["knowledge"]["materials"]), 3)
        activations = final_manifest["activeKnowledge"]["materials"]
        self.assertGreater(
            activations[latest["knowledge"]["knowledgeId"]],
            activations[changed["knowledge"]["knowledgeId"]],
        )

    def test_resealed_cache_cannot_bypass_interface_layout_match_gate(self) -> None:
        blob_a = _dxbc(b"same gated program", b"frame-a reflection")
        blob_b = _dxbc(b"same gated program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "gate-frame-a", blob_a, resource_id=401
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "gate-frame-b", blob_b, resource_id=402
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "gate-workspace-a", capture_a, shader_a, "// gated source\n"
            )
        )
        first = self.service.import_capture(catalog_b)
        self.assertEqual(
            first["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        cache_path = Path(first["derivedCache"]["path"])
        cache_value = json.loads(cache_path.read_text(encoding="utf-8"))
        cache_value["mappings"][0]["workspaces"][0]["matchMode"] = "raw-exact"
        unsigned = {
            key: value for key, value in cache_value.items() if key != "sealSha256"
        }
        cache_value["sealSha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        cache_path.write_text(json.dumps(cache_value), encoding="utf-8")

        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            wraps=self.service._build_reuse_mappings,
        ) as rebuilt:
            recovered = self.service.reuse_plan(capture_b, first["catalogSha256"])
        self.assertEqual(rebuilt.call_count, 1)
        self.assertFalse(recovered["derivedCache"]["hit"])
        self.assertEqual(
            recovered["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(recovered["policy"]["liveAbiRevalidationRequiredBeforeShaderApply"])
        self.assertFalse(recovered["policy"]["autoApplyAuthorized"])

    def test_same_capture_id_tracks_catalog_revisions_and_selects_latest(self) -> None:
        catalog, unused_shader, capture_id = self._catalog(
            "same-capture-revisions", _dxbc(), resource_id=3
        )
        first = self.service.import_capture(catalog)
        self.assertFalse(first["catalogRevisionKnown"])

        changed = json.loads(catalog.read_text(encoding="utf-8"))
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")
        second = self.service.import_capture(catalog)

        self.assertEqual(second["captureId"], capture_id)
        self.assertFalse(second["idempotent"])
        self.assertFalse(second["catalogRevisionKnown"])
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        status = self.service.status()
        self.assertEqual(status["captureCount"], 1)
        self.assertEqual(status["captureRevisionCount"], 2)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revisions = manifest["captures"][0]["catalogRevisions"]
        self.assertEqual(len(revisions), 2)
        self.assertEqual(
            {item["catalogSha256"] for item in revisions},
            {first["catalogSha256"], second["catalogSha256"]},
        )

        repeated = self.service.import_capture(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertTrue(repeated["catalogRevisionKnown"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)

    def test_legacy_single_catalog_manifest_remains_readable_and_idempotent(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "legacy-single-catalog", _dxbc(), resource_id=4
        )
        self.service.import_capture(catalog)
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captures"][0].pop("catalogRevisions")
        manifest["captures"][0].pop("activeCatalogSha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertEqual(self.service.status()["captureRevisionCount"], 1)
        repeated = self.service.import_capture(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertTrue(repeated["catalogRevisionKnown"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 1)

    def test_revision_history_is_owned_exact_and_historical_reimport_keeps_head(self) -> None:
        catalog_a, shader_a, capture_id = self._catalog(
            "revision-owned-a", _dxbc(), resource_id=5
        )
        catalog_b = self._catalog_revision(
            catalog_a,
            "revision-owned-b",
            field_base_type="UInt",
            indent=2,
        )
        catalog_b_equivalent = self._catalog_revision(
            catalog_a,
            "revision-owned-b-equivalent",
            field_base_type="UInt",
            indent=4,
        )

        first = self.service.import_capture(catalog_a)
        second = self.service.import_capture(catalog_b)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        self.assertTrue(second["catalogRevisionAdded"])
        self.assertTrue(second["activeRevisionChanged"])

        equivalent = self.service.import_capture(catalog_b_equivalent)
        self.assertTrue(equivalent["idempotent"])
        self.assertFalse(equivalent["catalogRevisionAdded"])
        self.assertEqual(equivalent["catalogSha256"], second["catalogSha256"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)

        historical = self.service.import_capture(catalog_a)
        self.assertTrue(historical["idempotent"])
        self.assertEqual(historical["catalogSha256"], first["catalogSha256"])
        self.assertEqual(
            historical["activeCatalogSha256"], second["catalogSha256"]
        )
        self.assertFalse(historical["activeRevisionChanged"])
        self.assertEqual(
            self.service.reuse_plan(capture_id, first["catalogSha256"])[
                "catalogSha256"
            ],
            first["catalogSha256"],
        )

        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        capture = manifest["captures"][0]
        self.assertEqual(capture["captureId"], capture_id)
        self.assertEqual(capture["catalogSha256"], second["catalogSha256"])
        self.assertEqual(capture["activeCatalogSha256"], second["catalogSha256"])
        self.assertEqual(len(capture["catalogRevisions"]), 2)
        owned_paths = [
            self.project / item["catalogPath"]
            for item in capture["catalogRevisions"]
        ]
        self.assertTrue(all(path.is_file() for path in owned_paths))
        self.assertTrue(all(path.is_relative_to(self.project) for path in owned_paths))

        # External import roots are origins, not retained authority.
        catalog_a.write_text("{}", encoding="utf-8")
        catalog_b.unlink()
        catalog_b_equivalent.unlink()
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)
        self.assertEqual(shader_a["rawSha256"], capture["shaders"][0]["rawSha256"])

    def test_project_owned_catalog_revision_hash_tamper_fails_closed(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "revision-catalog-tamper", _dxbc(), resource_id=51
        )
        self.service.import_capture(catalog)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revision = manifest["captures"][0]["catalogRevisions"][0]
        owned_catalog = self.project / revision["catalogPath"]
        owned_catalog.write_bytes(owned_catalog.read_bytes() + b" \n")

        with self.assertRaisesRegex(ReplayProjectError, "catalog hash mismatch"):
            self.service.status()

    def test_active_catalog_revision_mirror_tamper_fails_closed(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "revision-mirror-tamper", _dxbc(), resource_id=52
        )
        self.service.import_capture(catalog)
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captures"][0]["importedAt"] = "2000-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ReplayProjectError, "mirror is inconsistent"):
            self.service.status()

    def test_workspace_registration_uses_exact_catalog_revision_not_head(self) -> None:
        catalog_a, shader_a, capture_id = self._catalog(
            "workspace-revision-a", _dxbc(), resource_id=6
        )
        first = self.service.import_capture(catalog_a)
        catalog_b = self._catalog_revision(
            catalog_a,
            "workspace-revision-b",
            field_base_type="UInt",
        )
        second = self.service.import_capture(catalog_b)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])

        status = self._workspace_status(
            "workspace-revision-source",
            capture_id,
            shader_a,
            "float4 main() : SV_Target { return 1; }\n",
        )
        status["capture"].update(
            {
                "catalogPath": str(catalog_a),
                "capturePath": str(self.root / "workspace-revision-a" / "workspace-revision-a.rdc"),
                "api": "D3D11",
            }
        )
        registered = self.service.register_workspace(status)
        self.assertEqual(
            registered["knowledge"]["originCatalogSha256"],
            first["catalogSha256"],
        )
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revisions = manifest["captures"][0]["catalogRevisions"]
        revision_a = next(
            item for item in revisions if item["catalogSha256"] == first["catalogSha256"]
        )
        revision_b = next(
            item for item in revisions if item["catalogSha256"] == second["catalogSha256"]
        )
        self.assertEqual(
            registered["knowledge"]["layoutHash"],
            revision_a["shaders"][0]["layoutHash"],
        )
        self.assertNotEqual(
            revision_a["shaders"][0]["layoutHash"],
            revision_b["shaders"][0]["layoutHash"],
        )

        ambiguous = self._workspace_status(
            "workspace-revision-ambiguous",
            capture_id,
            shader_a,
            "float4 main() : SV_Target { return 0; }\n",
        )
        with self.assertRaisesRegex(ReplayProjectError, "revision is ambiguous"):
            self.service.register_workspace(ambiguous)

    def test_internal_workspace_artifact_is_hash_verified(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-a", blob, resource_id=11
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "workspace-a", capture_id, shader, "// trusted seed\n"
        )
        registered = self.service.register_workspace(status)
        source = self.project / registered["knowledge"]["sourcePath"]
        source.write_text("// tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "hash mismatch"):
            self.service.status()

    def test_project_rejects_capture_from_another_api_scope(self) -> None:
        blob = _dxbc()
        catalog, unused_shader, unused_capture = self._catalog(
            "frame-vulkan", blob, resource_id=12
        )
        value = json.loads(catalog.read_text(encoding="utf-8"))
        value["api"] = "Vulkan"
        value["captureId"] = capture_identity(value, value["shaders"])
        catalog.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "API conflicts"):
            self.service.import_capture(catalog)

    def test_project_rejects_instruction_assembly_as_hlsl_knowledge(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-assembly", blob, resource_id=13
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "workspace-assembly", capture_id, shader, "dcl_globalFlags refactoringAllowed\n"
        )
        status["source"]["format"] = "dxbc-asm"
        Path(status["source"]["path"]).rename(
            Path(status["source"]["path"]).with_name("shader.dxbc.asm")
        )
        status["source"]["path"] = str(
            Path(status["source"]["path"]).with_name("shader.dxbc.asm")
        )
        with self.assertRaisesRegex(ReplayProjectError, "HLSL"):
            self.service.register_workspace(status)

    def test_project_rejects_seeding_into_instruction_assembly_workspace(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-hlsl", blob, resource_id=14
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "workspace-hlsl", capture_id, shader, "float4 main() : SV_Target { return 1; }\n"
            )
        )
        destination = self._workspace_status(
            "workspace-assembly-destination",
            capture_id,
            shader,
            "dcl_globalFlags refactoringAllowed\n",
        )
        destination["source"]["format"] = "dxbc-asm"
        with self.assertRaisesRegex(ReplayProjectError, "HLSL"):
            self.service.seed_workspace(
                registered["knowledge"]["knowledgeId"], destination
            )


if __name__ == "__main__":
    unittest.main()
