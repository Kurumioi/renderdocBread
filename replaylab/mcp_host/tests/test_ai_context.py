from __future__ import annotations

import copy
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
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


def _qt5_json_number_roundtrip(value):
    """Model QJsonValue's double-backed integer storage without Qt bindings."""
    if isinstance(value, dict):
        return {
            key: _qt5_json_number_roundtrip(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_qt5_json_number_roundtrip(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(float(value))
    return value


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
        self.assertEqual(first["shaders"]["focused"]["resourceId"], "902")
        self.assertTrue(first["events"]["selectionDiffersFromReplay"])
        self.assertEqual(
            [item["name"] for item in first["action"]["markerPath"]],
            ["Opaque", "Character"],
        )
        self.assertEqual(first["shaders"]["focused"]["stage"], "fragment")
        self.assertTrue(first["shaders"]["focusIsExplicit"])
        self.assertEqual(first["texture"]["pickedPixel"], {"x": 320, "y": 180})
        self.assertEqual(first["texture"]["resourceId"], "444")
        self.assertEqual(
            first["texture"]["subresource"], {"mip": 2, "slice": 3, "sample": 1}
        )
        self.assertTrue(first["capabilities"]["canReverseEditFocusedShader"])
        self.assertTrue(first["capabilities"]["canInspectPickedPixel"])
        self.assertEqual(ai_context.validate_context(first), first)

    def test_64_bit_resource_ids_survive_qt5_json_roundtrip_and_hash_validation(self):
        facts = _facts()
        facts["boundShaders"][0]["resourceId"] = 1000000000000000628
        facts["boundShaders"][1]["resourceId"] = 1000000000000000627
        facts["texture"]["resourceId"] = 1000000000000000774

        snapshot = ai_context.build_context(facts)
        roundtripped = _qt5_json_number_roundtrip(copy.deepcopy(snapshot))

        self.assertEqual(roundtripped, snapshot)
        self.assertEqual(
            [item["resourceId"] for item in snapshot["shaders"]["bound"]],
            ["1000000000000000627", "1000000000000000628"],
        )
        self.assertEqual(snapshot["texture"]["resourceId"], "1000000000000000774")
        self.assertEqual(ai_context.validate_context(roundtripped), snapshot)

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

    def test_shader_candidates_are_filtered_by_current_action_compatibility(self):
        stages = [
            "vertex", "hull", "domain", "geometry", "fragment", "compute",
            "task", "mesh", "raygen", "intersection", "anyhit",
            "closesthit", "miss", "callable",
        ]
        cases = (
            (
                "ActionFlags.Instanced|Indexed|Drawcall",
                ["vertex", "hull", "domain", "geometry", "fragment"],
            ),
            ("ActionFlags.Dispatch", ["compute"]),
            ("ActionFlags.MeshDispatch", ["fragment", "task", "mesh"]),
            (
                "ActionFlags.DispatchRay",
                [
                    "raygen", "intersection", "anyhit", "closesthit", "miss",
                    "callable",
                ],
            ),
            ("ActionFlags.Copy", []),
            ("ActionFlags.Unknown", []),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                facts = _facts()
                facts["action"]["flags"] = flags
                facts["boundShaders"] = [
                    {
                        "stage": stage,
                        "resourceId": 1000 + index,
                        "entryPoint": stage + "_main",
                        "rawSha256": ("%x" % (index + 1)) * 64,
                    }
                    for index, stage in enumerate(stages)
                ]
                facts["focusStage"] = "compute"

                snapshot = ai_context.build_context(facts)

                self.assertEqual(
                    [item["stage"] for item in snapshot["shaders"]["bound"]],
                    expected,
                )
                self.assertEqual(
                    set(snapshot["shaders"]["compatibleStages"]), set(expected)
                )
                self.assertEqual(
                    snapshot["shaders"]["compatibilitySource"],
                    "current-action-flags",
                )
                self.assertEqual(
                    snapshot["shaders"]["focused"] is not None,
                    flags == "ActionFlags.Dispatch",
                )

    def test_draw_context_drops_a_retained_material_setup_compute_shader(self):
        facts = _facts()
        facts["boundShaders"][0]["entryPoint"] = "MainPS"
        facts["boundShaders"][1]["entryPoint"] = "Main"
        facts["boundShaders"].append(
            {
                "stage": "compute",
                "resourceId": 977,
                "entryPoint": "MaterialSetupCS",
                "rawSha256": "3" * 64,
            }
        )

        snapshot = ai_context.build_context(facts)

        self.assertEqual(
            [item["entryPoint"] for item in snapshot["shaders"]["bound"]],
            ["Main", "MainPS"],
        )
        self.assertNotIn(
            "MaterialSetupCS",
            [item["entryPoint"] for item in snapshot["shaders"]["bound"]],
        )

    def test_mutually_exclusive_action_families_fail_closed(self):
        for flags in (
            "ActionFlags.Drawcall|ActionFlags.Dispatch",
            "ActionFlags.MeshDispatch|ActionFlags.DispatchRay",
            0x0002 | 0x0004,
        ):
            with self.subTest(flags=flags):
                facts = _facts()
                facts["action"]["flags"] = flags

                snapshot = ai_context.build_context(facts)

                self.assertEqual(snapshot["shaders"]["bound"], [])
                self.assertEqual(snapshot["shaders"]["compatibleStages"], [])
                self.assertIsNone(snapshot["shaders"]["focused"])

    def test_non_current_action_cannot_supply_shader_candidates(self):
        facts = _facts()
        facts["action"]["eventId"] = facts["events"]["currentEventId"] - 1

        snapshot = ai_context.build_context(facts)

        self.assertEqual(snapshot["shaders"]["bound"], [])
        self.assertEqual(snapshot["shaders"]["compatibleStages"], [])
        self.assertIsNone(snapshot["shaders"]["focused"])


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

    def test_external_mcp_exposes_context_but_not_internal_intent_queue(self):
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
        self.assertIn("get_current_renderdoc_context", tools)
        self.assertTrue(
            {
                "enqueue_renderdoc_ui_intent",
                "claim_renderdoc_ui_intents",
                "renew_renderdoc_ui_intent",
                "complete_renderdoc_ui_intent",
                "get_renderdoc_ui_intent_status",
            }.isdisjoint(tools)
        )

    def test_qrenderdoc_buttons_start_direct_tasks_without_external_intent_queue(self):
        source_root = REPOSITORY.parent
        assistant = (
            source_root / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
        ).read_text(encoding="utf-8")
        integration = (
            source_root / "qrenderdoc" / "Code" / "ReplayLab" /
            "ReplayLabIntegration.cpp"
        ).read_text(encoding="utf-8")
        combined = assistant + "\n" + integration
        for marker in (
            "QueueIntent(",
            "queueNativeIntent",
            "enqueue_native_intent",
            "claim_renderdoc_ui_intents",
            "Codex Desktop",
        ):
            self.assertNotIn(marker, combined)
        self.assertIn("StartCaptureSkill(", assistant)
        self.assertIn('lit("codex-app-server")', assistant)
        self.assertIn("CurrentContextJSON", integration)

    def test_editing_queued_clarification_rebinds_context_and_drops_option_claims(self):
        source_root = REPOSITORY.parent
        assistant = (
            source_root / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
        ).read_text(encoding="utf-8")
        start = assistant.index("void AIAssistant::editQueuedChat")
        end = assistant.index("void AIAssistant::deleteQueuedChat", start)
        edit = assistant[start:end]

        self.assertIn("updated.clarificationResponse.optionIds.clear();", edit)
        self.assertIn("updated.clarificationResponse.otherText = edited;", edit)
        self.assertIn(
            "updated.clarificationResponse.currentContextSnapshot.clear();",
            edit,
        )
        self.assertIn(
            "updated.clarificationResponse.currentContextInstanceId.clear();",
            edit,
        )
        self.assertIn("findBridgeBinding(", edit)
        self.assertIn("updated.requestedEventId = qint64(m_Ctx.CurEvent());", edit)
        self.assertIn(
            "updated.requestedSelectedEventId = qint64(m_Ctx.CurSelectedEvent());",
            edit,
        )
        self.assertIn("advanceChatContextFreeze();", edit)
        self.assertNotIn("freezeChatContext", edit)
        self.assertNotIn("CurrentContextJSON", edit)


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
        self.pending = []
        self.block_invocations = 0

    def BlockInvoke(self, callback):
        self.block_invocations += 1
        callback(self.controller)

    def AsyncInvoke(self, *args):
        callback = args[-1]
        self.pending.append(callback)

    def run_next(self):
        callback = self.pending.pop(0)
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
        self.current_event = 142
        self.selected_event = 140
        self.replay = _Replay(controller)

    def IsCaptureLoaded(self):
        return True

    def IsCaptureLocal(self):
        return True

    def GetCaptureFilename(self):
        return r"D:\captures\frame.rdc"

    def CurEvent(self):
        return self.current_event

    def CurSelectedEvent(self):
        return self.selected_event

    def GetAction(self, event_id):
        return self.action if event_id == 142 else None

    def CurAction(self):
        return self.action

    def APIProps(self):
        return types.SimpleNamespace(pipelineType="D3D11")

    def Replay(self):
        return self.replay

    def CurPipelineState(self):
        return self.controller.pipeline

    def GetTextureViewer(self):
        return self.texture_viewer

    def GetResourceName(self, resource):
        return "HDRColor"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vertex-enum",
        Hull="hull-enum",
        Domain="domain-enum",
        Geometry="geometry-enum",
        Fragment="fragment-enum",
        Compute="compute-enum",
        Task="task-enum",
        Mesh="mesh-enum",
        RayGen="raygen-enum",
        Intersection="intersection-enum",
        AnyHit="anyhit-enum",
        ClosestHit="closesthit-enum",
        Miss="miss-enum",
        Callable="callable-enum",
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
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.bridge.IPC_DIR = os.path.join(
            self._temp.name, "%d-%s" % (os.getpid(), "c" * 32))
        self.bridge.INSTANCE_FILE = os.path.join(
            self.bridge.IPC_DIR, "instance.json")
        marker = _Action(100, 30, "Opaque", "PushMarker")
        action = _Action(142, 77, "DrawIndexed(240)", "Drawcall", marker)
        pipeline = _Pipeline(
            {
                "vertex-enum": _ResourceId(901),
                "fragment-enum": _ResourceId(902),
                # D3D12 keeps graphics and compute state independently. This
                # stale CS must not become a candidate for the current Draw.
                "compute-enum": _ResourceId(903),
            }
        )
        self.controller = _Controller(pipeline)
        self.bridge._ctx = _CaptureContext(self.controller, action)
        self.bridge._capture_session_id = "a" * 32
        self.bridge._active_capture_token = self.bridge._current_capture_token()
        self.bridge._symbol_capture_snapshot["captureId"] = "b" * 64
        self.bridge._ai_context_generation = 0
        self.bridge._ai_last_observation_id = ""

    @unittest.skipUnless(os.name == "nt", "Win32 long-path regression")
    def test_atomic_write_supports_catalog_blob_paths_beyond_max_path(self):
        directory = Path(self._temp.name)
        index = 0
        while len(str(directory / (("a" * 64) + ".bin.tmp.1234.5678"))) < 280:
            directory /= ("segment-%02d-" % index) + ("x" * 28)
            index += 1
        target = directory / (("a" * 64) + ".bin")

        self.bridge._atomic_write_bytes(str(target), b"dxil")

        self.assertGreater(len(str(target)), 260)
        self.assertEqual(target.read_bytes(), b"dxil")

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
        self.assertEqual(first["texture"]["resourceId"], "444")
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
        self.assertEqual(clicked["shaders"]["focused"]["resourceId"], "902")
        self.assertTrue(clicked["shaders"]["focusIsExplicit"])

    def test_bridge_exposes_context_without_legacy_intent_queue(self):
        self.assertIn("get_current_renderdoc_context", self.bridge.METHODS)
        self.assertTrue(
            {
                "enqueue_renderdoc_ui_intent",
                "claim_renderdoc_ui_intents",
                "renew_renderdoc_ui_intent",
                "complete_renderdoc_ui_intent",
                "get_renderdoc_ui_intent_status",
            }.isdisjoint(self.bridge.METHODS)
        )

    def test_native_integration_exposes_bound_stages_and_direct_context(self):
        self.assertEqual(
            self.bridge.available_native_shader_stages(), "vertex,fragment"
        )
        context = json.loads(self.bridge.current_native_context_json("fragment"))
        self.assertEqual(context["shaders"]["focused"]["resourceId"], "902")
        self.assertTrue(context["shaders"]["focusIsExplicit"])

    def test_async_native_context_freeze_queues_without_blocking_and_publishes_file(self):
        replay = self.bridge._ctx.replay

        begin = json.loads(self.bridge.begin_native_context_freeze_json("fragment"))

        self.assertEqual(begin["schema"], "replaylab-current-context-freeze-job/v1")
        self.assertEqual(begin["status"], "queued")
        self.assertEqual(begin["captureSessionId"], "a" * 32)
        self.assertEqual(begin["currentEventId"], 142)
        self.assertEqual(begin["selectedEventId"], 140)
        self.assertEqual(replay.block_invocations, 0)
        self.assertEqual(len(replay.pending), 1)
        self.assertFalse(os.path.exists(begin["resultPath"]))

        replay.run_next()

        terminal = json.loads(Path(begin["resultPath"]).read_text("utf-8"))
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["jobId"], begin["jobId"])
        self.assertEqual(terminal["selectedEventId"], 140)
        self.assertEqual(terminal["context"]["events"]["currentEventId"], 142)
        self.assertEqual(
            json.loads(self.bridge.poll_native_context_freeze_json(begin["jobId"])),
            terminal,
        )

    def test_delayed_begin_binds_expected_events_and_omits_later_texture(self):
        self.bridge._ctx.current_event = 99
        self.bridge._ctx.selected_event = 98

        begin = json.loads(self.bridge.begin_native_context_freeze_json(
            "fragment", "142", "140", "a" * 32
        ))
        self.bridge._ctx.replay.run_next()
        terminal = json.loads(Path(begin["resultPath"]).read_text("utf-8"))
        context = terminal["context"]

        self.assertEqual(context["events"]["currentEventId"], 142)
        self.assertEqual(context["events"]["selectedEventId"], 140)
        self.assertFalse(context["texture"]["available"])
        self.assertTrue(any(
            "Texture Viewer context was omitted" in warning
            for warning in context["warnings"]
        ))
        self.assertEqual(
            self.controller.events[-2:], [(142, True), (99, True)]
        )

    def test_delayed_begin_rejects_session_mismatch_and_invalid_action(self):
        with self.assertRaisesRegex(RuntimeError, "capture session changed"):
            self.bridge.begin_native_context_freeze_json(
                "", "142", "140", "d" * 32
            )
        with self.assertRaisesRegex(ValueError, "valid action"):
            self.bridge.begin_native_context_freeze_json(
                "", "141", "141", "a" * 32
            )
        with self.assertRaisesRegex(ValueError, "positive decimal uint32"):
            self.bridge.begin_native_context_freeze_json(
                "", "0142", "140", "a" * 32
            )

    def test_delayed_begin_preserves_zero_selected_event(self):
        self.bridge._ctx.current_event = 99
        self.bridge._ctx.selected_event = 0
        begin = json.loads(self.bridge.begin_native_context_freeze_json(
            "", "142", "0", "a" * 32
        ))
        self.assertEqual(begin["selectedEventId"], 0)
        self.bridge._ctx.replay.run_next()
        terminal = json.loads(Path(begin["resultPath"]).read_text("utf-8"))
        self.assertEqual(terminal["context"]["events"]["selectedEventId"], 0)

    def test_async_native_context_freeze_reports_capture_change_failure(self):
        begin = json.loads(self.bridge.begin_native_context_freeze_json(""))
        self.bridge._capture_session_id = "d" * 32

        self.bridge._ctx.replay.run_next()

        terminal = json.loads(Path(begin["resultPath"]).read_text("utf-8"))
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("capture changed", terminal["error"])
        self.assertNotIn("context", terminal)

    def test_async_failure_restores_replay_event_and_publishes_once(self):
        self.bridge._ctx.current_event = 99
        self.bridge._ctx.selected_event = 98
        begin = json.loads(self.bridge.begin_native_context_freeze_json(
            "", "142", "140", "a" * 32
        ))
        writes = []
        original_write = self.bridge._atomic_write_bytes

        def recording_write(path, data):
            writes.append((path, data))
            original_write(path, data)

        def fail_stage(*_args, **_kwargs):
            raise RuntimeError("synthetic stage failure")

        self.bridge._atomic_write_bytes = recording_write
        self.bridge._bound_shader_stage_context = fail_stage
        self.bridge._ctx.replay.run_next()

        terminal = json.loads(Path(begin["resultPath"]).read_text("utf-8"))
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("synthetic stage failure", terminal["error"])
        self.assertEqual(
            self.controller.events[-2:], [(142, True), (99, True)]
        )
        self.assertEqual(len(writes), 1)

    def test_abandon_is_idempotent_and_callback_cannot_overwrite_terminal(self):
        begin = json.loads(self.bridge.begin_native_context_freeze_json(""))

        first = json.loads(
            self.bridge.abandon_native_context_freeze_json(begin["jobId"])
        )
        second = json.loads(
            self.bridge.abandon_native_context_freeze_json(begin["jobId"])
        )
        before = Path(begin["resultPath"]).read_bytes()
        self.bridge._ctx.replay.run_next()

        self.assertEqual(first["status"], "abandoned")
        self.assertEqual(second, first)
        self.assertEqual(Path(begin["resultPath"]).read_bytes(), before)

    def test_timeout_publishes_terminal_file_and_late_callback_cannot_overwrite(self):
        begin = json.loads(self.bridge.begin_native_context_freeze_json(""))

        self.bridge._timeout_current_context_freeze_job(begin["jobId"])
        before = Path(begin["resultPath"]).read_bytes()
        terminal = json.loads(before.decode("utf-8"))
        self.bridge._ctx.replay.run_next()

        self.assertEqual(terminal["status"], "failed")
        self.assertIn("timed out", terminal["error"])
        self.assertEqual(Path(begin["resultPath"]).read_bytes(), before)

    def test_instance_manifest_exposes_only_current_capture_session_id(self):
        self.bridge._publish_instance_manifest()
        manifest = json.loads(Path(self.bridge.INSTANCE_FILE).read_text("utf-8"))
        self.assertEqual(manifest["captureSessionId"], "a" * 32)

        self.bridge._ctx = None
        self.bridge._publish_instance_manifest()
        manifest = json.loads(Path(self.bridge.INSTANCE_FILE).read_text("utf-8"))
        self.assertEqual(manifest["captureSessionId"], "")

    def test_bridge_action_kind_filters_draw_compute_mesh_and_ray_stages(self):
        cases = (
            (
                "Drawcall",
                {
                    "vertex-enum": _ResourceId(901),
                    "fragment-enum": _ResourceId(902),
                    "compute-enum": _ResourceId(903),
                },
                ["vertex", "fragment"],
            ),
            (
                "Dispatch",
                {
                    "vertex-enum": _ResourceId(901),
                    "fragment-enum": _ResourceId(902),
                    "compute-enum": _ResourceId(903),
                },
                ["compute"],
            ),
            (
                "MeshDispatch",
                {
                    "vertex-enum": _ResourceId(901),
                    "fragment-enum": _ResourceId(902),
                    "compute-enum": _ResourceId(903),
                    "task-enum": _ResourceId(904),
                    "mesh-enum": _ResourceId(905),
                },
                ["fragment", "task", "mesh"],
            ),
            (
                "DispatchRay",
                {
                    "compute-enum": _ResourceId(903),
                    "raygen-enum": _ResourceId(906),
                    "intersection-enum": _ResourceId(907),
                    "anyhit-enum": _ResourceId(908),
                    "closesthit-enum": _ResourceId(909),
                    "miss-enum": _ResourceId(910),
                    "callable-enum": _ResourceId(911),
                },
                [
                    "raygen", "intersection", "anyhit", "closesthit", "miss",
                    "callable",
                ],
            ),
        )
        for flags, stage_values, expected in cases:
            with self.subTest(flags=flags):
                self.bridge._ctx.action.flags = flags
                self.controller.pipeline.stage_values = stage_values
                self.bridge._ai_context_generation = 0
                self.bridge._ai_last_observation_id = ""

                context = self.bridge._build_current_renderdoc_context()

                self.assertEqual(
                    [item["stage"] for item in context["shaders"]["bound"]],
                    expected,
                )
                self.assertEqual(
                    self.bridge.available_native_shader_stages(),
                    ",".join(expected),
                )

    def test_bridge_unknown_or_non_work_action_fails_closed(self):
        self.bridge._ctx.action.flags = "Copy"

        context = self.bridge._build_current_renderdoc_context()

        self.assertEqual(context["shaders"]["bound"], [])
        self.assertEqual(self.bridge.available_native_shader_stages(), "")


if __name__ == "__main__":
    unittest.main()
