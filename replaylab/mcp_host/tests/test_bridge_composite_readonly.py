from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_composite_readonly_contract", BRIDGE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


class CompositeReadonlyBridgeTests(unittest.TestCase):
    def test_composite_methods_are_registered(self) -> None:
        bridge = load_bridge()

        for name in ("inspect_event", "analyze_pixel", "inspect_shader"):
            self.assertIn(name, bridge.METHODS)

    def test_inspect_event_restores_the_visible_event_through_request_tracking(self) -> None:
        bridge = load_bridge()

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()
        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
        )
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None
        bridge._replay = lambda callback: callback(controller)
        bridge._draw_call_details_on_replay = lambda ctrl, event_id: {
            "eventId": event_id,
        }
        bridge._pipeline_state_on_replay = lambda ctrl: {"pipeline": True}

        response = bridge.handle_request(
            {"id": 3, "method": "inspect_event", "params": {"event_id": 41}}
        )

        self.assertNotIn("error", response)
        self.assertEqual([(41, True), (7, True)], controller.events)

    def test_inspect_event_collects_draw_and_pipeline_in_one_replay(self) -> None:
        bridge = load_bridge()
        replay_calls = []

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._replay = replay
        bridge._draw_call_details_on_replay = lambda ctrl, event_id: {
            "eventId": event_id,
            "draw": True,
        }
        bridge._pipeline_state_on_replay = lambda ctrl: {"pipeline": True}

        result = bridge._inspect_event({"event_id": 41})

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True)], controller.events)
        self.assertEqual("renderdoc-internal-inspect-event/v1", result["schema"])
        self.assertEqual({"eventId": 41, "draw": True}, result["drawCall"])
        self.assertEqual({"pipeline": True}, result["pipeline"])

    def test_analyze_pixel_collects_all_evidence_and_restores_in_one_replay(self) -> None:
        bridge = load_bridge()
        replay_calls = []
        helper_calls = []

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
        )
        bridge._replay = replay
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None

        def texture_info(ctrl, resource_id):
            helper_calls.append(("texture", ctrl, resource_id))
            return {"id": resource_id}

        def texture_minmax(ctrl, resource_id):
            helper_calls.append(("minmax", ctrl, resource_id))
            return {"min": 0, "max": 1}

        def inspect(ctrl, texture_id, x, y, mip, array_slice, sample,
                    prefer_geometry, cutoff_event_id):
            helper_calls.append(
                (
                    "inspection", ctrl, texture_id, x, y, mip, array_slice,
                    sample, prefer_geometry, cutoff_event_id,
                )
            )
            return {"pixel": [x, y]}

        bridge._texture_info_on_replay = texture_info
        bridge._texture_minmax_on_replay = texture_minmax
        bridge._inspect_pixel_object_on_replay = inspect

        response = bridge.handle_request(
            {
                "id": 5,
                "method": "analyze_pixel",
                "params": {
                    "texture_id": 99,
                    "x": 12,
                    "y": 34,
                    "event_id": 41,
                    "mip": 1,
                    "slice": 2,
                    "sample": 3,
                    "prefer_geometry": False,
                },
            }
        )
        result = response["result"]

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True), (7, True)], controller.events)
        self.assertEqual(["texture", "minmax", "inspection"], [c[0] for c in helper_calls])
        self.assertEqual("renderdoc-internal-analyze-pixel/v1", result["schema"])
        self.assertEqual({"id": 99}, result["texture"])
        self.assertEqual({"min": 0, "max": 1}, result["minMax"])
        self.assertEqual(
            {"originalEventId": 7, "frozenEventId": 41, "restored": True},
            result["inspection"]["eventRestore"],
        )

    def test_inspect_shader_runs_shader_info_once_without_reverse_duplication(self) -> None:
        bridge = load_bridge()
        calls = []

        def shader_info(params):
            calls.append(params)
            return {"disassembly": {"DXIL": "shader"}}

        bridge._get_shader_info = shader_info
        bridge._reverse_shader = lambda params: self.fail(
            "inspect_shader must not repeat reconstruction through reverse_shader"
        )

        result = bridge._inspect_shader({"event_id": 41, "stage": "pixel"})

        self.assertEqual([{"event_id": 41, "stage": "fragment"}], calls)
        self.assertEqual("renderdoc-internal-inspect-shader/v1", result["schema"])
        self.assertEqual("fragment", result["stage"])
        self.assertEqual({"disassembly": {"DXIL": "shader"}}, result["shader"])
        self.assertNotIn("reconstructionEvidence", result)

    def test_analyze_pixel_restores_event_when_inspection_fails(self) -> None:
        bridge = load_bridge()

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()
        replay_calls = []

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
        )
        bridge._replay = replay
        bridge._texture_info_on_replay = lambda ctrl, resource_id: {"id": resource_id}
        bridge._texture_minmax_on_replay = lambda ctrl, resource_id: {}

        def fail_inspection(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("pixel history failed")

        bridge._inspect_pixel_object_on_replay = fail_inspection

        with self.assertRaisesRegex(RuntimeError, "pixel history failed"):
            bridge._analyze_pixel(
                {"texture_id": 99, "x": 1, "y": 2, "event_id": 41}
            )

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True), (7, True)], controller.events)

    def test_composite_method_keeps_capture_session_guard(self) -> None:
        bridge = load_bridge()
        calls = []
        bridge._capture_session_id = "a" * 32
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None
        bridge.METHODS["inspect_event"] = lambda params: calls.append(params)

        response = bridge.handle_request(
            {
                "id": 17,
                "method": "inspect_event",
                "params": {
                    "event_id": 41,
                    "__replaylab_expected_capture_session_id": "b" * 32,
                },
            }
        )

        self.assertEqual([], calls)
        self.assertEqual(17, response["id"])
        self.assertIn("error", response)
        self.assertIn("capture session changed", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
