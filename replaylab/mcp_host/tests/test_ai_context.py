from __future__ import annotations

import copy
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "ai_context.py"
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("renderdoc_ultra_ai_context", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ai_context = _load_module()


def _facts(focus_source="explicit-ui-button"):
    return {
        "contextGeneration": 19,
        "capture": {
            "loaded": True,
            "captureSessionId": "a" * 32,
            "captureId": "b" * 64,
            "path": r"D:\captures\frame.rdc",
            "api": "D3D11",
            "local": True,
        },
        "events": {"currentEventId": 142, "selectedEventId": 140},
        "action": {
            "eventId": 142,
            "actionId": 77,
            "name": "DrawIndexed(240)",
            "flags": "Drawcall|Indexed",
            "markerPath": [
                {
                    "eventId": 100,
                    "actionId": 30,
                    "name": "Opaque",
                    "flags": "PushMarker",
                },
                {
                    "eventId": 130,
                    "actionId": 65,
                    "name": "Character",
                    "flags": "PushMarker",
                },
            ],
        },
        "boundShaders": [
            {
                "stage": "pixel",
                "resourceId": 902,
                "entryPoint": "main",
                "rawSha256": "2" * 64,
                "displayName": "CharacterPS",
            },
            {
                "stage": "vertex",
                "resourceId": 901,
                "entryPoint": "main",
                "rawSha256": "1" * 64,
                "displayName": "CharacterVS",
            },
        ],
        "focusStage": "fragment",
        "focusSource": focus_source,
        "texture": {
            "resourceId": 444,
            "name": "HDRColor",
            "subresource": {"mip": 2, "slice": 3, "sample": 1},
            "pickedPixel": {"x": 320, "y": 180},
        },
        "warnings": ["z warning", "a warning", "a warning"],
    }


class CurrentContextTests(unittest.TestCase):
    def test_snapshot_is_canonical_content_addressed_and_complete(self):
        first = ai_context.build_context(_facts())
        reordered = _facts()
        reordered["boundShaders"].reverse()
        reordered["warnings"].reverse()
        second = ai_context.build_context(reordered)

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], ai_context.CONTEXT_SCHEMA)
        self.assertEqual(first["contextGeneration"], 19)
        self.assertEqual(first["events"]["currentEventId"], 142)
        self.assertEqual(first["events"]["selectedEventId"], 140)
        self.assertTrue(first["events"]["selectionDiffersFromReplay"])
        self.assertEqual(
            [item["name"] for item in first["action"]["markerPath"]],
            ["Opaque", "Character"],
        )
        self.assertEqual(first["shaders"]["focused"]["stage"], "fragment")
        self.assertTrue(first["shaders"]["focusIsExplicit"])
        self.assertEqual(first["texture"]["pickedPixel"], {"x": 320, "y": 180})
        self.assertEqual(
            first["texture"]["subresource"], {"mip": 2, "slice": 3, "sample": 1}
        )
        self.assertTrue(first["capabilities"]["canReverseEditFocusedShader"])
        self.assertTrue(first["capabilities"]["canInspectPickedPixel"])
        self.assertEqual(ai_context.validate_context(first), first)

    def test_mutating_a_snapshot_breaks_its_content_hash(self):
        snapshot = ai_context.build_context(_facts())
        snapshot["events"]["currentEventId"] = 999
        with self.assertRaisesRegex(
            ai_context.ContextProtocolError, "content hash does not match"
        ):
            ai_context.validate_context(snapshot)

    def test_loaded_capture_requires_session_identity(self):
        facts = _facts()
        facts["capture"]["captureSessionId"] = ""
        with self.assertRaisesRegex(
            ai_context.ContextProtocolError, "requires a 32-character"
        ):
            ai_context.build_context(facts)

    def test_missing_texture_selection_is_explicit_not_fabricated(self):
        facts = _facts()
        facts["texture"] = {"resourceId": 0}
        snapshot = ai_context.build_context(facts)
        self.assertFalse(snapshot["texture"]["available"])
        self.assertIsNone(snapshot["focusedResource"])
        self.assertFalse(snapshot["capabilities"]["canInspectPickedPixel"])

    def test_default_shader_focus_cannot_masquerade_as_ui_selection(self):
        snapshot = ai_context.build_context(_facts("deterministic-default"))
        self.assertEqual(snapshot["shaders"]["focused"]["stage"], "fragment")
        self.assertFalse(snapshot["shaders"]["focusIsExplicit"])
        queue = ai_context.IntentQueue(id_factory=lambda: "intent-default")
        with self.assertRaisesRegex(
            ai_context.ContextProtocolError, "explicitly stage-scoped"
        ):
            queue.enqueue(ai_context.INTENT_REVERSE_EDIT_SHADER, snapshot)


class _Clock(object):
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class _Ids(object):
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return "token-%d" % self.value


class IntentQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.ids = _Ids()
        self.queue = ai_context.IntentQueue(
            maximum=4, clock=self.clock, id_factory=self.ids
        )
        self.context = ai_context.build_context(_facts())

    def test_explicit_shader_intent_freezes_exact_shader_identity(self):
        record = self.queue.enqueue(
            ai_context.INTENT_REVERSE_EDIT_SHADER, self.context
        )
        self.context["shaders"]["focused"]["resourceId"] = 999999
        self.assertEqual(record["origin"], "renderdoc-ui")
        self.assertEqual(record["arguments"]["eventId"], 142)
        self.assertEqual(record["arguments"]["stage"], "fragment")
        self.assertEqual(record["arguments"]["shaderResourceId"], 902)
        self.assertEqual(record["arguments"]["rawSha256"], "2" * 64)
        self.assertTrue(
            record["recommendedWorkflow"]["automaticExecutionAuthorized"]
        )
        self.assertFalse(record["recommendedWorkflow"]["advisoryOnly"])
        self.assertTrue(
            record["recommendedWorkflow"]["suggestedStepsAdvisory"]
        )
        self.assertFalse(
            record["recommendedWorkflow"]["replayMutationAuthorized"]
        )
        self.assertIn(
            "decompile-shader",
            record["recommendedWorkflow"]["allowedOperations"],
        )
        self.assertIn(
            "watch-and-compile-saves",
            record["recommendedWorkflow"]["allowedOperations"],
        )
        self.assertIn(
            "synchronise-active-project",
            record["recommendedWorkflow"]["allowedOperations"],
        )
        self.assertIn(
            "resolve-project-symbols",
            record["recommendedWorkflow"]["allowedOperations"],
        )
        self.assertIn(
            "persist-project-knowledge",
            record["recommendedWorkflow"]["allowedOperations"],
        )
        self.assertEqual(
            record["recommendedWorkflow"]["steps"][0]["tool"], "reverse_shader"
        )
        self.assertEqual(record["context"]["shaders"]["focused"]["resourceId"], 902)

    def test_claim_is_leased_retried_and_only_live_holder_completes(self):
        queued = self.queue.enqueue(ai_context.INTENT_EXPLAIN_EVENT, self.context)
        first = self.queue.claim("companion-a", maximum=1, lease_seconds=5)
        first_record = first["intents"][0]
        self.assertEqual(first_record["intentId"], queued["intentId"])
        self.assertEqual(first_record["attempts"], 1)
        self.assertEqual(self.queue.claim("companion-b")["intents"], [])

        self.clock.value += 6
        second = self.queue.claim("companion-b", maximum=1, lease_seconds=30)
        second_record = second["intents"][0]
        self.assertEqual(second_record["attempts"], 2)
        with self.assertRaisesRegex(ai_context.ContextProtocolError, "token does not match"):
            self.queue.complete(
                queued["intentId"], first_record["leaseToken"], "completed"
            )

        completed = self.queue.complete(
            queued["intentId"],
            second_record["leaseToken"],
            "completed",
            "Explained by the external companion",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["leaseToken"], "")

    def test_live_holder_can_renew_but_expired_or_wrong_token_cannot(self):
        queued = self.queue.enqueue(ai_context.INTENT_EXPLAIN_EVENT, self.context)
        claimed = self.queue.claim(
            "companion-a", maximum=1, lease_seconds=5
        )["intents"][0]
        original_expiry = claimed["leaseExpiresAt"]
        self.clock.value += 4
        renewed = self.queue.renew(
            queued["intentId"], claimed["leaseToken"], lease_seconds=30
        )
        self.assertGreater(renewed["leaseExpiresAt"], original_expiry)
        with self.assertRaisesRegex(
            ai_context.ContextProtocolError, "token does not match"
        ):
            self.queue.renew(queued["intentId"], "wrong-token", 30)
        self.clock.value = renewed["leaseExpiresAt"] + 1
        with self.assertRaisesRegex(
            ai_context.ContextProtocolError, "does not have a live claim"
        ):
            self.queue.renew(
                queued["intentId"], claimed["leaseToken"], lease_seconds=30
            )

    def test_capture_change_cancels_stale_unprocessed_intents(self):
        queued = self.queue.enqueue(ai_context.INTENT_INSPECT_PIXEL, self.context)
        cancelled = self.queue.cancel_for_capture_change("c" * 32)
        self.assertEqual(cancelled, 1)
        status = self.queue.status(queued["intentId"])
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["outcomeSummary"], "capture session changed")

    def test_bounded_queue_never_drops_live_work(self):
        queue = ai_context.IntentQueue(
            maximum=1, clock=self.clock, id_factory=self.ids
        )
        queue.enqueue(ai_context.INTENT_EXPLAIN_EVENT, self.context)
        with self.assertRaisesRegex(ai_context.ContextProtocolError, "queue is full"):
            queue.enqueue(ai_context.INTENT_INSPECT_PIXEL, self.context)

    def test_completed_tombstone_can_be_evicted_for_new_work(self):
        queue = ai_context.IntentQueue(
            maximum=1, clock=self.clock, id_factory=self.ids
        )
        old = queue.enqueue(ai_context.INTENT_EXPLAIN_EVENT, self.context)
        claim = queue.claim("companion", lease_seconds=30)["intents"][0]
        queue.complete(old["intentId"], claim["leaseToken"], "completed")
        new = queue.enqueue(ai_context.INTENT_INSPECT_PIXEL, self.context)
        self.assertNotEqual(old["intentId"], new["intentId"])
        with self.assertRaisesRegex(ai_context.ContextProtocolError, "unknown intent"):
            queue.status(old["intentId"])


class SourceBoundaryTests(unittest.TestCase):
    def test_protocol_module_has_no_ai_network_qt_or_renderdoc_import(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "import openai",
            "from openai",
            "import requests",
            "import httpx",
            "import renderdoc",
            "import qrenderdoc",
            "import PySide",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_bridge_and_protocol_remain_python36_parseable(self):
        for path in (MODULE_PATH, BRIDGE_PATH):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )

    def test_mcp_exposes_context_claim_renew_complete_but_not_enqueue(self):
        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        tools = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        self.assertTrue(
            {
                "get_current_renderdoc_context",
                "claim_renderdoc_ui_intents",
                "renew_renderdoc_ui_intent",
                "complete_renderdoc_ui_intent",
                "get_renderdoc_ui_intent_status",
            }.issubset(tools)
        )
        self.assertNotIn("enqueue_renderdoc_ui_intent", tools)


class _ResourceId(object):
    def __init__(self, value):
        self.value = value

    def __int__(self):
        return self.value


class _Entry(object):
    def __init__(self, name):
        self.name = name


class _Pipeline(object):
    def __init__(self, stage_values):
        self.stage_values = stage_values

    def GetShader(self, stage):
        return self.stage_values.get(stage, _ResourceId(0))

    def GetShaderEntryPoint(self, stage):
        return "main"

    def GetShaderPipelineObject(self):
        return _ResourceId(777)


class _Controller(object):
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.events = []

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))

    def GetPipelineState(self):
        return self.pipeline

    def GetShaderEntryPoints(self, resource):
        return [_Entry("main")]

    def GetShader(self, pipeline, resource, entry):
        return types.SimpleNamespace(entryPoint=entry.name, rawBytes=("shader-%d" % int(resource)).encode())


class _Replay(object):
    def __init__(self, controller):
        self.controller = controller

    def BlockInvoke(self, callback):
        callback(self.controller)


class _Action(object):
    def __init__(self, event_id, action_id, name, flags, parent=None):
        self.eventId = event_id
        self.actionId = action_id
        self.customName = name
        self.name = name
        self.flags = flags
        self.parent = parent


class _TextureViewer(object):
    def GetCurrentResource(self):
        return _ResourceId(444)

    def GetSelectedSubresource(self):
        return types.SimpleNamespace(mip=2, slice=3, sample=1)

    def GetPickedLocation(self):
        return (320, 180)


class _CaptureContext(object):
    def __init__(self, controller, action):
        self.controller = controller
        self.action = action
        self.texture_viewer = _TextureViewer()

    def IsCaptureLoaded(self):
        return True

    def IsCaptureLocal(self):
        return True

    def GetCaptureFilename(self):
        return r"D:\captures\frame.rdc"

    def CurEvent(self):
        return 142

    def CurSelectedEvent(self):
        return 140

    def GetAction(self, event_id):
        return self.action if event_id == 142 else None

    def CurAction(self):
        return self.action

    def APIProps(self):
        return types.SimpleNamespace(pipelineType="D3D11")

    def Replay(self):
        return _Replay(self.controller)

    def CurPipelineState(self):
        return self.controller.pipeline

    def GetTextureViewer(self):
        return self.texture_viewer

    def GetResourceName(self, resource):
        return "HDRColor"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vertex-enum", Fragment="fragment-enum", Compute="compute-enum"
    )
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_ai_context_contract", BRIDGE_PATH
        )
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc
    return bridge


class BridgeContextContractTests(unittest.TestCase):
    def setUp(self):
        self.bridge = _load_bridge()
        marker = _Action(100, 30, "Opaque", "PushMarker")
        action = _Action(142, 77, "DrawIndexed(240)", "Drawcall", marker)
        pipeline = _Pipeline(
            {
                "vertex-enum": _ResourceId(901),
                "fragment-enum": _ResourceId(902),
            }
        )
        self.controller = _Controller(pipeline)
        self.bridge._ctx = _CaptureContext(self.controller, action)
        self.bridge._capture_session_id = "a" * 32
        self.bridge._symbol_capture_snapshot["captureId"] = "b" * 64
        self.bridge._ai_context_generation = 0
        self.bridge._ai_last_observation_id = ""

    def test_bridge_reads_exact_current_event_shader_texture_and_marker_context(self):
        first = self.bridge._build_current_renderdoc_context()
        second = self.bridge._build_current_renderdoc_context()
        self.assertEqual(first, second)
        self.assertGreater(first["contextGeneration"], 0)
        self.assertEqual(first["events"]["currentEventId"], 142)
        self.assertEqual(first["events"]["selectedEventId"], 140)
        self.assertEqual(first["action"]["markerPath"][0]["name"], "Opaque")
        self.assertEqual(
            [shader["stage"] for shader in first["shaders"]["bound"]],
            ["vertex", "fragment"],
        )
        self.assertEqual(
            first["shaders"]["bound"][1]["rawSha256"],
            __import__("hashlib").sha256(b"shader-902").hexdigest(),
        )
        self.assertIsNone(first["shaders"]["focused"])
        self.assertEqual(first["texture"]["resourceId"], 444)
        self.assertEqual(first["texture"]["pickedPixel"], {"x": 320, "y": 180})
        self.assertIn((142, True), self.controller.events)

    def test_only_internal_stage_scoped_ui_path_can_mark_shader_focus_explicit(self):
        untrusted = self.bridge._get_current_renderdoc_context(
            {"focus_stage": "fragment", "focus_source": "explicit-ui-button"}
        )
        self.assertIsNone(untrusted["shaders"]["focused"])

        clicked = self.bridge._build_current_renderdoc_context(
            "fragment", "explicit-ui-button"
        )
        self.assertEqual(clicked["shaders"]["focused"]["resourceId"], 902)
        self.assertTrue(clicked["shaders"]["focusIsExplicit"])

    def test_bridge_exposes_claim_renew_complete_but_no_enqueue_rpc(self):
        self.assertIn("get_current_renderdoc_context", self.bridge.METHODS)
        self.assertIn("claim_renderdoc_ui_intents", self.bridge.METHODS)
        self.assertIn("renew_renderdoc_ui_intent", self.bridge.METHODS)
        self.assertIn("complete_renderdoc_ui_intent", self.bridge.METHODS)
        self.assertNotIn("enqueue_renderdoc_ui_intent", self.bridge.METHODS)

    def test_native_integration_exposes_bound_stages_and_typed_intents(self):
        self.assertEqual(
            self.bridge.available_native_shader_stages(), "vertex,fragment"
        )

        explain_id = self.bridge.enqueue_native_intent("explain-current-event")
        explain = self.bridge._ai_intent_queue.status(explain_id)
        self.assertEqual(explain["kind"], "explain-current-event")
        self.assertEqual(explain["origin"], "renderdoc-ui")
        self.assertEqual(explain["arguments"]["eventId"], 142)
        self.assertIsNone(explain["context"]["shaders"]["focused"])

        shader_id = self.bridge.enqueue_native_intent(
            "reverse-edit-selected-shader", "fragment"
        )
        shader = self.bridge._ai_intent_queue.status(shader_id)
        self.assertEqual(shader["kind"], "reverse-edit-selected-shader")
        self.assertEqual(shader["arguments"]["stage"], "fragment")
        self.assertEqual(shader["arguments"]["shaderResourceId"], 902)
        self.assertTrue(shader["context"]["shaders"]["focusIsExplicit"])

        pixel_id = self.bridge.enqueue_native_intent("inspect-picked-pixel")
        pixel = self.bridge._ai_intent_queue.status(pixel_id)
        self.assertEqual(pixel["kind"], "inspect-picked-pixel")
        self.assertEqual(pixel["arguments"]["textureResourceId"], 444)
        self.assertEqual(pixel["arguments"]["pixel"], {"x": 320, "y": 180})


if __name__ == "__main__":
    unittest.main()
