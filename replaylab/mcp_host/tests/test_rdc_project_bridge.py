import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

from src.rdc_project_index import new_index


REPOSITORY = Path(__file__).resolve().parents[2]


class _Properties:
    def __init__(self):
        self.name = ""
        self.type = None
        self.version = 0
        self.flags = None
        self.uncompressedSize = 0


class _WriteResult:
    message = ""

    @staticmethod
    def OK():
        return True


class _CaptureAccess:
    def __init__(self):
        self.sections = []

    def GetSectionCount(self):
        return len(self.sections)

    def GetSectionProperties(self, index):
        return self.sections[index][0]

    def GetSectionContents(self, index):
        return self.sections[index][1]

    def WriteSection(self, properties, contents):
        properties.uncompressedSize = len(contents)
        record = (properties, bytes(contents))
        for index, (current, unused) in enumerate(self.sections):
            del unused
            if current.name == properties.name:
                self.sections[index] = record
                break
        else:
            self.sections.append(record)
        return _WriteResult()


class _Pipeline:
    def __init__(self, resource_id):
        self.resource_id = resource_id

    def GetShader(self, unused_stage):
        del unused_stage
        return self.resource_id

    @staticmethod
    def GetShaderPipelineObject():
        return 99


class _Controller:
    def __init__(self, raw, *, api="D3D11", encoding="DXBC", blocks=None):
        self.raw = raw
        self.api = api
        self.encoding = encoding
        self.events = []
        self.entry = types.SimpleNamespace(name="main", stage="fragment")
        self.resource = types.SimpleNamespace(type="shader", resourceId=7)
        self.resources = [self.resource]
        self.reflection = types.SimpleNamespace(
            entryPoint="main",
            stage="fragment",
            encoding=encoding,
            rawBytes=self.raw,
            inputSignature=[],
            outputSignature=[],
            readOnlyResources=[],
            readWriteResources=[],
            samplers=[],
            constantBlocks=list(blocks or []),
            interfaces=[],
            outputTopology="",
            dispatchThreadsDimension=[],
        )

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))

    @staticmethod
    def GetPipelineState():
        return _Pipeline(7)

    def GetAPIProperties(self):
        return types.SimpleNamespace(pipelineType=self.api)

    def GetResources(self):
        return list(self.resources)

    def GetShaderEntryPoints(self, unused_resource):
        del unused_resource
        return [self.entry]

    def GetShader(self, unused_pipeline, unused_resource, unused_entry):
        del unused_pipeline, unused_resource, unused_entry
        return self.reflection


def _constant_block(name="PerMaterial", bind=0, space=0, byte_size=16):
    return types.SimpleNamespace(
        name=name,
        fixedBindNumber=bind,
        fixedBindSetOrSpace=space,
        bindArraySize=1,
        byteSize=byte_size,
        bufferBacked=True,
        inlineDataBytes=False,
        compileConstants=False,
        variables=[],
    )


class _Replay:
    def __init__(self, access, controller):
        self.access = access
        self.controller = controller

    def GetCaptureAccess(self):
        return self.access

    def GetCaptureFile(self):
        return self.access

    def BlockInvoke(self, callback):
        callback(self.controller)

    def AsyncInvoke(self, callback):
        callback(self.controller)


class _Context:
    def __init__(self, path, replay):
        self.path = str(path)
        self.replay = replay

    @staticmethod
    def IsCaptureLoaded():
        return True

    def GetCaptureFilename(self):
        return self.path

    def Replay(self):
        return self.replay

    @staticmethod
    def CurEvent():
        return 5


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.SectionProperties = _Properties
    fake_renderdoc.SectionType = types.SimpleNamespace(Unknown="unknown")
    fake_renderdoc.SectionFlags = types.SimpleNamespace(
        ZstdCompressed="zstd"
    )
    fake_renderdoc.ShaderStage = types.SimpleNamespace(Fragment="fragment")
    fake_renderdoc.ResourceType = types.SimpleNamespace(Shader="shader")
    fake_renderdoc.ResourceId = lambda: 0
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_rdc_project_test", path
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc


class ReplayLabRdcProjectBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.capture_path = Path(self.temporary.name) / "frame.rdc"
        self.capture_path.write_bytes(b"capture")
        self.access = _CaptureAccess()
        self.controller = _Controller(
            b"captured-shader", blocks=[_constant_block()]
        )
        replay = _Replay(self.access, self.controller)
        self.bridge._ctx = _Context(self.capture_path, replay)
        self.bridge._capture_session_id = "1" * 32
        self.bridge._known_catalog_capture_ids = {}
        self.bridge._known_catalog_identity_pairs = set()
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()

    def tearDown(self):
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        self.bridge._ctx = None
        self.temporary.cleanup()

    def _expected_shader(self):
        reflection = self.controller.reflection
        compiled = self.bridge._compiled_shader_interface(reflection)
        compiled["stage"] = self.bridge._normalise_stage_name(reflection.stage)
        compiled["entryPoint"] = reflection.entryPoint
        compiled["encoding"] = self.bridge._project_shader_index_encoding(
            reflection.encoding
        )
        interface_sha, layout_sha = self.bridge._project_symbol_hashes(compiled)
        blocks = [
            self.bridge._project_shader_index_block(
                block, index, compiled["stage"]
            )
            for index, block in enumerate(reflection.constantBlocks)
        ]
        return {
            "resourceId": 7,
            "stage": compiled["stage"],
            "entryPoint": reflection.entryPoint,
            "encoding": compiled["encoding"],
            "rawSha256": hashlib.sha256(reflection.rawBytes).hexdigest(),
            "interfaceHash": interface_sha,
            "layoutHash": layout_sha,
            "interfaceComplete": bool(compiled["interfaceComplete"]),
            "constantBlocks": blocks,
            "anchorEventId": 10,
        }

    def _index_request(self, shader=None):
        return {
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "expected_capture_id": "2" * 64,
            "expected_identity_evidence_sha256": "3" * 64,
            "expected_api": self.controller.api,
            "expected_shaders": [shader or self._expected_shader()],
        }

    def _begin(self, shader=None):
        return self.bridge._begin_rdc_project_shader_resource_index(
            self._index_request(shader)
        )

    def _commit(self, snapshot):
        return self.bridge._commit_rdc_project_shader_resource_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "job_id": snapshot["jobId"],
            "expected_capture_id": "2" * 64,
            "expected_identity_evidence_sha256": "3" * 64,
            "live_identity_sha256": snapshot["result"]["liveIdentitySha256"],
        })

    def test_named_unknown_section_round_trips_exact_sealed_document(self):
        missing = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })
        self.assertFalse(missing["present"])

        document = new_index()
        written = self.bridge._write_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "document": document,
        })
        self.assertTrue(written["written"])
        self.assertFalse(written["physicalApplyAuthority"])
        self.assertEqual(document, written["document"])
        self.assertEqual("unknown", self.access.sections[0][0].type)
        self.assertEqual("zstd", self.access.sections[0][0].flags)

        read = self.bridge._read_rdc_project_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
        })
        self.assertEqual(document, read["document"])
        self.assertEqual(written["encodedSha256"], read["encodedSha256"])

    def test_duplicate_named_sections_and_non_finite_json_fail_closed(self):
        document = new_index()
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        for unused in range(2):
            del unused
            properties = _Properties()
            properties.name = self.bridge.REPLAYLAB_RDC_PROJECT_INDEX_SECTION
            properties.version = 1
            properties.uncompressedSize = len(payload)
            self.access.sections.append((properties, payload))
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.bridge._read_rdc_project_index({})

        self.access.sections.pop()
        invalid = payload.replace(b'"bindings":[]', b'"bindings":[],"x":NaN')
        self.access.sections[0] = (self.access.sections[0][0], invalid)
        self.access.sections[0][0].uncompressedSize = len(invalid)
        with self.assertRaisesRegex(RuntimeError, "strict UTF-8 JSON"):
            self.bridge._read_rdc_project_index({})

    def test_resource_index_matches_complete_content_and_never_sets_event(self):
        snapshot = self._begin()

        self.assertEqual(snapshot["status"], "completed")
        result = snapshot["result"]
        self.assertTrue(result["matched"])
        self.assertEqual(result["eventTraversalCount"], 0)
        self.assertFalse(result["fullFrameTraversal"])
        self.assertFalse(result["physicalApplyAuthority"])
        self.assertEqual(result["resourceOwnershipScope"], "target-capture-session")
        self.assertFalse(result["crossCaptureResourceIdReuse"])
        self.assertEqual(self.controller.events, [])
        asset = result["assets"][0]
        self.assertEqual(asset["ownerResourceId"], 7)
        self.assertEqual(asset["ownerCaptureSessionId"], "1" * 32)
        self.assertTrue(asset["physicalAbiValidated"])
        self.assertTrue(asset["constantBlocksMatched"])
        self.assertEqual(asset["constantBlocks"], self._expected_shader()["constantBlocks"])

        lease = self._commit(snapshot)
        self.assertTrue(lease["committed"])
        self.assertFalse(lease["physicalApplyAuthority"])
        self.assertIn(
            ("2" * 64, "3" * 64), self.bridge._known_catalog_identity_pairs
        )
        self.assertEqual(self.controller.events, [])

    def test_begin_and_commit_are_idempotent(self):
        first = self._begin()
        second = self._begin()
        self.assertEqual(first["jobId"], second["jobId"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

        first_lease = self._commit(first)
        second_lease = self._commit(first)
        self.assertFalse(first_lease["idempotent"])
        self.assertTrue(second_lease["idempotent"])
        self.assertEqual(self.controller.events, [])

    def test_forged_hash_and_extra_or_missing_blocks_fail_closed(self):
        variants = []
        forged = self._expected_shader()
        forged["interfaceHash"] = "4" * 64
        variants.append(forged)

        missing = self._expected_shader()
        missing["constantBlocks"] = []
        variants.append(missing)

        extra = self._expected_shader()
        duplicate = dict(extra["constantBlocks"][0])
        duplicate.update({"index": 1, "path": "Unexpected", "bind": 1})
        extra["constantBlocks"] = [extra["constantBlocks"][0], duplicate]
        variants.append(extra)

        for shader in variants:
            with self.subTest(shader=shader):
                with self.bridge._project_shader_index_jobs_guard:
                    self.bridge._project_shader_index_jobs.clear()
                snapshot = self._begin(shader)
                self.assertEqual(snapshot["status"], "completed")
                self.assertFalse(snapshot["result"]["matched"])
                self.assertEqual(
                    snapshot["result"]["reasonId"],
                    "shader-resource-index-mismatch",
                )
                self.assertEqual(self.controller.events, [])
        self.assertEqual(set(), self.bridge._known_catalog_identity_pairs)

    def test_glsl_is_a_supported_content_addressed_format(self):
        self.controller.api = "OpenGL"
        self.controller.encoding = "GLSL"
        self.controller.reflection.encoding = "GLSL"

        snapshot = self._begin()

        self.assertTrue(snapshot["result"]["matched"])
        self.assertEqual(snapshot["result"]["assets"][0]["encoding"], "glsl")
        self.assertEqual(self.controller.events, [])

    def test_vulkan_null_pipeline_proves_module_only_and_requires_preflight(self):
        self.controller.api = "Vulkan"
        self.controller.encoding = "SPIRV"
        self.controller.reflection.encoding = "SPIRV"

        snapshot = self._begin()

        self.assertTrue(snapshot["result"]["matched"])
        asset = snapshot["result"]["assets"][0]
        self.assertEqual(
            asset["contentMatchMode"],
            "module-sha256-owner-rid-anchor-preflight",
        )
        self.assertFalse(asset["physicalAbiValidated"])
        self.assertEqual(asset["physicalAbiSha256"], "")
        self.assertTrue(asset["requiresAnchorPreflight"])
        self.assertFalse(snapshot["result"]["physicalApplyAuthority"])
        self.assertEqual(self.controller.events, [])

    def test_same_content_under_another_rid_never_inherits_owner_authority(self):
        expected = self._expected_shader()
        expected["resourceId"] = 8
        self.controller.resources = [
            self.controller.resource,
            types.SimpleNamespace(type="shader", resourceId=8),
        ]

        snapshot = self._begin(expected)

        # The fake controller deliberately returns the same module for both RIDs;
        # the result must still retain exactly the requested target-RDC owner.
        self.assertTrue(snapshot["result"]["matched"])
        asset = snapshot["result"]["assets"][0]
        self.assertEqual(asset["resourceId"], 8)
        self.assertEqual(asset["ownerResourceId"], 8)
        self.assertFalse(asset["crossCaptureResourceIdReuse"])

    def test_job_timeout_ttl_and_capture_change_fail_closed(self):
        replay = self.bridge._ctx.replay
        replay.AsyncInvoke = lambda unused_callback: None
        queued = self._begin()
        job = self.bridge._project_shader_index_jobs[queued["jobId"]]
        job["createdMonotonic"] -= (
            self.bridge.PROJECT_SHADER_INDEX_JOB_TIMEOUT_SECONDS + 1.0
        )
        timed_out = self.bridge._poll_rdc_project_shader_resource_index({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "job_id": queued["jobId"],
        })
        self.assertEqual(timed_out["status"], "failed")
        self.assertIn("timed out", timed_out["error"])

        job["updatedMonotonic"] -= (
            self.bridge.PROJECT_SHADER_INDEX_JOB_TTL_SECONDS + 1.0
        )
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.bridge._poll_rdc_project_shader_resource_index({
                "expected_capture_path": str(self.capture_path),
                "expected_capture_session_id": "1" * 32,
                "job_id": queued["jobId"],
            })

        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        replay.AsyncInvoke = lambda callback: callback(self.controller)
        completed = self._begin()
        self.bridge._capture_session_id = "6" * 32
        with self.assertRaisesRegex(RuntimeError, "capture session changed"):
            self._commit(completed)

    def test_request_and_reflection_budgets_fail_before_event_traversal(self):
        expected = self._expected_shader()
        request = self._index_request(expected)
        request["expected_shaders"] = [
            dict(request["expected_shaders"][0], resourceId=index + 1)
            for index in range(self.bridge.MAX_PROJECT_SHADER_INDEX_EXPECTED + 1)
        ]
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            self.bridge._begin_rdc_project_shader_resource_index(request)

        leaf_type = types.SimpleNamespace(
            name="float", baseType="float", members=[], elements=1
        )
        child = types.SimpleNamespace(name="leaf", byteOffset=0, type=leaf_type)
        for depth in range(
            self.bridge.MAX_PROJECT_SHADER_INDEX_REFLECTION_DEPTH + 2
        ):
            nested_type = types.SimpleNamespace(
                name="Nested%d" % depth,
                baseType="struct",
                members=[child],
                elements=1,
            )
            child = types.SimpleNamespace(
                name="field%d" % depth, byteOffset=0, type=nested_type
            )
        self.controller.reflection.constantBlocks = [
            types.SimpleNamespace(
                name="Deep",
                fixedBindNumber=0,
                fixedBindSetOrSpace=0,
                bindArraySize=1,
                byteSize=16,
                bufferBacked=True,
                inlineDataBytes=False,
                compileConstants=False,
                variables=[child],
            )
        ]
        deep_expected = dict(expected)
        deep_expected["constantBlocks"] = []
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        snapshot = self._begin(deep_expected)
        self.assertFalse(snapshot["result"]["matched"])
        self.assertEqual(
            snapshot["result"]["reasonId"],
            "shader-resource-index-reflection-depth-limit",
        )
        self.assertEqual(self.controller.events, [])

        self.controller.reflection.constantBlocks = [_constant_block()]
        signature = types.SimpleNamespace(
            varName="value",
            semanticName="TEXCOORD",
            semanticIdxName="TEXCOORD0",
            semanticIndex=0,
            regIndex=0,
            systemValue="",
            varType="float",
            compType="float",
            compCount=4,
            stream=0,
            regChannelMask=15,
            channelUsedMask=15,
            perPrimitiveRate=False,
            needSemanticIndex=False,
        )
        self.controller.reflection.inputSignature = [
            signature
        ] * (self.bridge.MAX_PROJECT_SHADER_INDEX_SIGNATURES + 1)
        with self.bridge._project_shader_index_jobs_guard:
            self.bridge._project_shader_index_jobs.clear()
        snapshot = self._begin(expected)
        self.assertFalse(snapshot["result"]["matched"])
        self.assertEqual(
            snapshot["result"]["reasonId"],
            "shader-resource-index-reflection-signature-limit",
        )
        self.assertEqual(self.controller.events, [])


if __name__ == "__main__":
    unittest.main()
