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
    def __init__(self, raw):
        self.raw = raw
        self.events = []

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))

    @staticmethod
    def GetPipelineState():
        return _Pipeline(7)

    @staticmethod
    def GetShaderEntryPoints(unused_resource):
        del unused_resource
        return [types.SimpleNamespace(name="main")]

    def GetShader(self, unused_pipeline, unused_resource, unused_entry):
        del unused_pipeline, unused_resource, unused_entry
        return types.SimpleNamespace(entryPoint="main", rawBytes=self.raw)


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
        self.controller = _Controller(b"captured-shader")
        replay = _Replay(self.access, self.controller)
        self.bridge._ctx = _Context(self.capture_path, replay)
        self.bridge._capture_session_id = "1" * 32
        self.bridge._known_catalog_capture_ids = {}
        self.bridge._known_catalog_identity_pairs = set()

    def tearDown(self):
        self.bridge._ctx = None
        self.temporary.cleanup()

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

    def test_cached_shader_binding_uses_targeted_live_raw_anchor_only(self):
        raw_sha = hashlib.sha256(b"captured-shader").hexdigest()
        capture_id = "2" * 64
        evidence = "3" * 64
        result = self.bridge._bind_rdc_project_shader_catalog({
            "expected_capture_path": str(self.capture_path),
            "expected_capture_session_id": "1" * 32,
            "expected_capture_id": capture_id,
            "expected_identity_evidence_sha256": evidence,
            "anchors": [{
                "eventId": 10,
                "stage": "fragment",
                "resourceId": 7,
                "entryPoint": "main",
                "rawSha256": raw_sha,
            }],
        })
        self.assertFalse(result["fullFrameTraversal"])
        self.assertFalse(result["physicalApplyAuthority"])
        self.assertEqual([(10, True), (5, True)], self.controller.events)
        self.assertIn((capture_id, evidence),
                      self.bridge._known_catalog_identity_pairs)

    def test_changed_live_shader_does_not_publish_cached_catalog_identity(self):
        with self.assertRaisesRegex(RuntimeError, "physical bytecode"):
            self.bridge._bind_rdc_project_shader_catalog({
                "expected_capture_session_id": "1" * 32,
                "expected_capture_id": "4" * 64,
                "expected_identity_evidence_sha256": "5" * 64,
                "anchors": [{
                    "eventId": 10,
                    "stage": "fragment",
                    "resourceId": 7,
                    "entryPoint": "main",
                    "rawSha256": hashlib.sha256(b"old-shader").hexdigest(),
                }],
            })
        self.assertEqual(set(), self.bridge._known_catalog_identity_pairs)


if __name__ == "__main__":
    unittest.main()
