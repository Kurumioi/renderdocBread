from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vs",
        Hull="hs",
        Domain="ds",
        Geometry="gs",
        Fragment="ps",
        Compute="cs",
        Task="as",
        Mesh="ms",
    )
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_catalog_rebind_%s" % uuid.uuid4().hex,
            BRIDGE_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc


def _catalog(capture_path):
    raw_bytes = b"fixed captured shader bytes"
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    shader_key = "77:fragment:main:%s" % raw_sha256
    shader_identity = {
        "shaderKey": shader_key,
        "shaderResourceId": 77,
        "resourceId": 77,
        "stage": "fragment",
        "entryPoint": "main",
        "rawSha256": raw_sha256,
    }
    catalog = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "capturePath": capture_path,
        "api": "GraphicsAPI.D3D11",
        "capture": {
            "path": capture_path,
            "api": "GraphicsAPI.D3D11",
            "renderer": "D3D11",
            "frameNumber": 1,
        },
        "selection": {
            "mode": "explicit",
            "eventIds": [321, 322],
            "stages": ["fragment"],
        },
        "shaders": [dict(shader_identity, rawPath="blobs/%s.bin" % raw_sha256)],
        "uses": [dict(shader_identity, eventId=321)],
        "errors": [],
        "counts": {"events": 2, "shaders": 1, "uses": 1, "errors": 0},
    }
    return catalog, {raw_sha256: raw_bytes}


def _change_shader_identity(catalog):
    changed = copy.deepcopy(catalog)
    raw_sha256 = changed["shaders"][0]["rawSha256"]
    shader_key = "77:fragment:alternate:%s" % raw_sha256
    for record in (changed["shaders"][0], changed["uses"][0]):
        record["entryPoint"] = "alternate"
        record["shaderKey"] = shader_key
    return changed


class BridgeCatalogRebindTests(unittest.TestCase):
    def _configure(self, module, catalog, blobs):
        capture_path = catalog["capturePath"]
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 9,
            GetCaptureFilename=lambda: capture_path,
        )
        module._replay = lambda unused_callback: (
            copy.deepcopy(catalog), dict(blobs))

    def _seed_state(self, module):
        old_capture_id = "a" * 64
        old_capture_path = os.path.normcase(os.path.abspath("D:/captures/old.rdc"))
        module._symbol_capture_snapshot = {
            "captureId": old_capture_id,
            "capturePath": "D:/captures/old.rdc",
            "shaderKeys": {(12, "vertex", "main", "b" * 64)},
            "usesByKey": {(12, "vertex", "main", "b" * 64): [7]},
        }
        module._known_catalog_capture_ids = {old_capture_id: old_capture_path}
        module._known_catalog_identity_pairs = {(old_capture_id, "c" * 64)}
        module._symbol_alias_overlay = {
            "schema": module.SYMBOL_ALIAS_SCHEMA,
            "schemaVersion": module.SYMBOL_ALIAS_VERSION,
            "captureId": old_capture_id,
            "sourcePath": "D:/aliases.json",
            "confirmedOnly": True,
            "allAliases": [{"displayName": "old alias"}],
            "aliases": [{"displayName": "old alias"}],
        }
        module._symbol_resource_renames = {
            12: {"name": "CapturedName", "hadCustomName": True}
        }
        module._symbol_rename_capture_token = ("loaded", old_capture_path)
        return {
            "snapshot": copy.deepcopy(module._symbol_capture_snapshot),
            "known": copy.deepcopy(module._known_catalog_capture_ids),
            "pairs": copy.deepcopy(module._known_catalog_identity_pairs),
            "overlay": copy.deepcopy(module._symbol_alias_overlay),
            "renames": copy.deepcopy(module._symbol_resource_renames),
            "token": module._symbol_rename_capture_token,
        }

    def _assert_state_unchanged(self, module, original):
        self.assertEqual(module._symbol_capture_snapshot, original["snapshot"])
        self.assertEqual(module._known_catalog_capture_ids, original["known"])
        self.assertEqual(module._known_catalog_identity_pairs, original["pairs"])
        self.assertEqual(module._symbol_alias_overlay, original["overlay"])
        self.assertEqual(module._symbol_resource_renames, original["renames"])
        self.assertEqual(module._symbol_rename_capture_token, original["token"])

    def test_expected_mismatches_never_publish_or_write_catalog_identity(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            base_catalog, blobs = _catalog("D:/captures/current.rdc")
            expected_capture_id = module._catalog_capture_id(base_catalog)
            expected_evidence = module._catalog_identity_evidence_sha256(base_catalog)

            use_changed = copy.deepcopy(base_catalog)
            use_changed["uses"][0]["eventId"] = 322
            selection_changed = copy.deepcopy(base_catalog)
            selection_changed["selection"]["eventIds"] = [322, 321]
            cases = (
                (
                    "capture",
                    base_catalog,
                    "f" * 64,
                    expected_evidence,
                    "expected_capture_id",
                ),
                (
                    "use",
                    use_changed,
                    expected_capture_id,
                    expected_evidence,
                    "selection/Shader/use evidence changed",
                ),
                (
                    "selection",
                    selection_changed,
                    expected_capture_id,
                    expected_evidence,
                    "selection/Shader/use evidence changed",
                ),
                (
                    "shader",
                    _change_shader_identity(base_catalog),
                    expected_capture_id,
                    expected_evidence,
                    "expected_capture_id",
                ),
            )
            for name, live_catalog, wanted_capture_id, wanted_evidence, error in cases:
                with self.subTest(name=name):
                    original = self._seed_state(module)
                    self._configure(module, live_catalog, blobs)
                    output_dir = Path(temporary) / name / "export"
                    with self.assertRaisesRegex(RuntimeError, error):
                        module._export_shader_catalog({
                            "output_dir": str(output_dir),
                            "event_ids": [321, 322],
                            "stages": ["fragment"],
                            "expected_capture_id": wanted_capture_id,
                            "expected_identity_evidence_sha256": wanted_evidence,
                        })
                    self._assert_state_unchanged(module, original)
                    self.assertNotIn(
                        wanted_capture_id, module._known_catalog_capture_ids)
                    self.assertNotIn(
                        (wanted_capture_id, wanted_evidence),
                        module._known_catalog_identity_pairs,
                    )
                    with self.assertRaisesRegex(
                            RuntimeError, "no bridge catalog identity"):
                        module._require_capture_identity(wanted_capture_id)
                    self.assertFalse(output_dir.exists())

    def test_matching_expected_identity_is_published_before_files_are_returned(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            evidence_sha256 = module._catalog_identity_evidence_sha256(catalog)
            self._configure(module, catalog, blobs)
            original = self._seed_state(module)
            output_dir = Path(temporary) / "expected"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
                "expected_capture_id": capture_id,
                "expected_identity_evidence_sha256": evidence_sha256,
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(result["identityEvidenceSha256"], evidence_sha256)
            self.assertIs(result["expectedIdentityValidatedBeforeCache"], True)
            self.assertIn(capture_id, module._known_catalog_capture_ids)
            self.assertIn(
                (capture_id, evidence_sha256),
                module._known_catalog_identity_pairs,
            )
            # Restart rebinding is identity-only. It must not switch the active
            # symbol catalog or discard aliases selected by the user.
            self.assertEqual(
                module._symbol_capture_snapshot, original["snapshot"])
            self.assertEqual(module._symbol_alias_overlay, original["overlay"])
            self.assertEqual(module._symbol_resource_renames, original["renames"])
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())
            self.assertEqual(len(list((output_dir / "blobs").glob("*.bin"))), 1)

    def test_export_without_expected_identity_remains_backward_compatible(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            self._configure(module, catalog, blobs)
            module._symbol_capture_snapshot = {
                "captureId": "",
                "capturePath": "",
                "shaderKeys": set(),
                "usesByKey": {},
            }
            module._known_catalog_capture_ids = {}
            module._known_catalog_identity_pairs = set()
            output_dir = Path(temporary) / "ordinary"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(
                result["identityEvidenceSha256"],
                module._catalog_identity_evidence_sha256(catalog),
            )
            self.assertNotIn("expectedIdentityValidatedBeforeCache", result)
            self.assertIn(capture_id, module._known_catalog_capture_ids)
            self.assertIn(
                (capture_id, result["identityEvidenceSha256"]),
                module._known_catalog_identity_pairs,
            )
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())

    def test_full_export_reuses_reflection_but_keeps_per_event_uses_and_bindings(self):
        module = _load_bridge()
        capture_path = "D:/captures/repeated-shader.rdc"
        shader_resource_id = 77
        pipeline_resource_id = 901
        entry = types.SimpleNamespace(name="main")
        block = types.SimpleNamespace(
            name="PerDraw",
            fixedBindNumber=2,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=64,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=[],
        )
        reflection = types.SimpleNamespace(
            entryPoint="main",
            encoding="dxbc",
            rawBytes=b"one exact shader used by two events",
            constantBlocks=[block],
            inputSignature=[],
            outputSignature=[],
            readOnlyResources=[],
            readWriteResources=[],
            samplers=[],
            interfaces=[],
        )

        class Pipeline:
            def __init__(self, controller):
                self.controller = controller
                self.constant_block_calls = []

            def GetShader(self, unused_stage):
                return shader_resource_id

            def GetShaderEntryPoint(self, unused_stage):
                return "main"

            def GetShaderPipelineObject(self):
                return pipeline_resource_id

            def GetConstantBlock(self, stage, block_index, array_index):
                self.constant_block_calls.append(
                    (self.controller.event_id, stage, block_index, array_index))
                return types.SimpleNamespace(descriptor=types.SimpleNamespace(
                    resource=1000 + self.controller.event_id,
                    byteOffset=self.controller.event_id * 16,
                    byteSize=64,
                ))

        class Controller:
            def __init__(self):
                self.event_id = 0
                self.entry_point_calls = 0
                self.reflection_calls = 0
                self.pipeline = Pipeline(self)

            def GetRootActions(self):
                raise AssertionError(
                    "explicit representative-EID export must not scan actions"
                )

            def GetAPIProperties(self):
                return types.SimpleNamespace(
                    pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12")

            def GetFrameInfo(self):
                return types.SimpleNamespace(frameNumber=1)

            def SetFrameEvent(self, event_id, unused_force):
                self.event_id = event_id

            def GetPipelineState(self):
                return self.pipeline

            def GetShaderEntryPoints(self, shader_id):
                self.assert_shader(shader_id)
                self.entry_point_calls += 1
                return [entry]

            def GetShader(self, pipeline_id, shader_id, selected_entry):
                if pipeline_id != pipeline_resource_id:
                    raise AssertionError("unexpected pipeline")
                self.assert_shader(shader_id)
                if selected_entry is not entry:
                    raise AssertionError("unexpected entry")
                self.reflection_calls += 1
                return reflection

            @staticmethod
            def assert_shader(shader_id):
                if shader_id != shader_resource_id:
                    raise AssertionError("unexpected shader")

        controller = Controller()
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 9,
            GetCaptureFilename=lambda: capture_path,
        )
        module._replay = lambda callback: callback(controller)
        module._known_catalog_capture_ids = {}
        module._known_catalog_identity_pairs = set()
        module._symbol_capture_snapshot = {
            "captureId": "",
            "capturePath": "",
            "shaderKeys": set(),
            "usesByKey": {},
        }

        original_layout = module._constant_block_layout
        layout_calls = []

        def counting_layout(constant_block):
            layout_calls.append(constant_block)
            return original_layout(constant_block)

        module._constant_block_layout = counting_layout
        with tempfile.TemporaryDirectory() as temporary:
            progress_root = Path(temporary) / "instance"
            progress_root.mkdir()
            module.IPC_DIR = str(progress_root)
            module.INSTANCE_ID = "123-%s" % ("d" * 32)
            module._capture_session_id = "e" * 32
            output_dir = Path(temporary) / "cached-reflection"
            module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [101, 202],
                "stages": ["fragment"],
                "progress_token": "f" * 32,
                "progress_capture_session_id": "e" * 32,
            })
            catalog = json.loads(
                (output_dir / "shader_catalog.v1.json").read_text(
                    encoding="utf-8"))
            progress = json.loads(
                (progress_root / (
                    "shader-catalog-progress-%s.json" % ("f" * 32)
                )).read_text(encoding="utf-8")
            )

        self.assertEqual(controller.entry_point_calls, 1)
        self.assertEqual(controller.reflection_calls, 1)
        self.assertEqual(layout_calls, [block])
        self.assertEqual(
            [call[0] for call in controller.pipeline.constant_block_calls],
            [101, 202],
        )
        self.assertEqual(len(catalog["shaders"]), 1)
        self.assertEqual([use["eventId"] for use in catalog["uses"]], [101, 202])
        self.assertEqual(
            [use["constantBlocks"][0]["binding"]["resourceId"]
             for use in catalog["uses"]],
            [1101, 1202],
        )
        self.assertEqual(progress["operationToken"], "f" * 32)
        self.assertEqual(progress["captureSessionId"], "e" * 32)
        self.assertEqual(progress["instanceId"], "123-%s" % ("d" * 32))
        self.assertEqual(progress["eventId"], 202)
        self.assertEqual((progress["current"], progress["total"]), (2, 2))

    def test_progress_binding_rejects_invalid_token_or_capture_session(self):
        module = _load_bridge()
        module._capture_session_id = "a" * 32
        with self.assertRaisesRegex(ValueError, "progress_token"):
            module._shader_catalog_progress_binding(
                {
                    "progress_token": "../escape",
                    "progress_capture_session_id": "a" * 32,
                }
            )
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            module._shader_catalog_progress_binding(
                {
                    "progress_token": "b" * 32,
                    "progress_capture_session_id": "c" * 32,
                }
            )

    def test_ordinary_all_draw_export_allows_no_events_or_shaders(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            capture_path = "D:/captures/empty.rdc"
            catalog = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "capturePath": capture_path,
                "api": "GraphicsAPI.D3D11",
                "capture": {
                    "path": capture_path,
                    "api": "GraphicsAPI.D3D11",
                    "renderer": "D3D11",
                    "frameNumber": 1,
                },
                "selection": {
                    "mode": "all_draw_dispatch",
                    "eventIds": [],
                    "stages": ["fragment"],
                },
                "shaders": [],
                "uses": [],
                "errors": [],
                "counts": {
                    "events": 0,
                    "shaderUses": 0,
                    "uniqueShaders": 0,
                    "uniqueBlobs": 0,
                    "errors": 0,
                },
            }
            capture_id = module._catalog_capture_id(catalog)
            evidence_sha256 = module._catalog_identity_evidence_sha256(catalog)
            self._configure(module, catalog, {})
            module._symbol_capture_snapshot = {
                "captureId": "",
                "capturePath": "",
                "shaderKeys": set(),
                "usesByKey": {},
            }
            module._known_catalog_capture_ids = {}
            module._known_catalog_identity_pairs = set()
            output_dir = Path(temporary) / "empty"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "stages": ["fragment"],
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(result["identityEvidenceSha256"], evidence_sha256)
            self.assertEqual(result["counts"]["uniqueShaders"], 0)
            self.assertIn(
                (capture_id, evidence_sha256),
                module._known_catalog_identity_pairs,
            )
            self.assertEqual(module._symbol_capture_snapshot["shaderKeys"], set())
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())

    def test_cache_capacity_failure_is_transactional(self):
        module = _load_bridge()
        catalog, unused_blobs = _catalog("D:/captures/current.rdc")
        catalog["captureId"] = module._catalog_capture_id(catalog)
        original = self._seed_state(module)
        module._known_catalog_capture_ids = {
            "%064x" % index: "D:/captures/known.rdc"
            for index in range(module.MAX_KNOWN_CATALOG_CAPTURE_IDS)
        }
        original["known"] = copy.deepcopy(module._known_catalog_capture_ids)

        with self.assertRaisesRegex(RuntimeError, "Too many catalog identities"):
            module._cache_symbol_catalog(catalog)

        self._assert_state_unchanged(module, original)

    def test_exact_pair_capacity_failure_is_transactional_and_identity_only(self):
        module = _load_bridge()
        catalog, unused_blobs = _catalog("D:/captures/current.rdc")
        capture_id = module._catalog_capture_id(catalog)
        catalog["captureId"] = capture_id
        original = self._seed_state(module)
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(catalog["capturePath"]))
        }
        module._known_catalog_identity_pairs = {
            (capture_id, "%064x" % index)
            for index in range(module.MAX_KNOWN_CATALOG_IDENTITY_PAIRS)
        }
        original["known"] = copy.deepcopy(module._known_catalog_capture_ids)
        original["pairs"] = copy.deepcopy(module._known_catalog_identity_pairs)

        with self.assertRaisesRegex(RuntimeError, "Too many exact catalog selections"):
            module._cache_catalog_identity_pair(catalog)

        self._assert_state_unchanged(module, original)

    def test_capture_status_exposes_bounded_exact_identity_pairs(self):
        module = _load_bridge()
        capture_path = "D:/captures/current.rdc"
        capture_id = "d" * 64
        evidence_sha256 = "e" * 64
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            GetCaptureFilename=lambda: capture_path,
        )
        module._capture_session_id = "7" * 32
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(capture_path))
        }
        module._known_catalog_identity_pairs = {
            (capture_id, evidence_sha256)
        }
        module._symbol_capture_snapshot = {
            "captureId": "",
            "capturePath": "",
            "shaderKeys": set(),
            "usesByKey": {},
        }
        controller = types.SimpleNamespace(
            GetAPIProperties=lambda: types.SimpleNamespace(
                pipelineType="GraphicsAPI.D3D11", localRenderer="D3D11"
            ),
            GetFrameInfo=lambda: types.SimpleNamespace(frameNumber=1),
        )
        module._replay = lambda callback: callback(controller)

        status = module._get_capture_status({})

        self.assertEqual(status["knownCatalogCaptureIds"], [capture_id])
        self.assertEqual(
            status["knownCatalogIdentityPairs"],
            [{
                "captureId": capture_id,
                "identityEvidenceSha256": evidence_sha256,
            }],
        )


if __name__ == "__main__":
    unittest.main()
