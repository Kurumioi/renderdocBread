from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
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
            "renderdoc_mcp_bridge_event_texture_evidence_contract", BRIDGE_PATH
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


class _TextureSave:
    def __init__(self) -> None:
        self.resourceId = None
        self.typeCast = None
        self.destType = None
        self.mip = -1
        self.slice = types.SimpleNamespace(sliceIndex=-1)
        self.sample = types.SimpleNamespace(mapToArray=True, sampleIndex=-1)
        self.comp = types.SimpleNamespace(blackPoint=0.0, whitePoint=1.0)
        self.channelExtract = -1


class _Controller:
    def __init__(self, current, *, texture_byte_size=1024 * 1024) -> None:
        self.current = current
        self.events = []
        self.saved = []
        self.texture_byte_size = texture_byte_size
        self.color = types.SimpleNamespace(
            resource=101,
            firstMip=1,
            numMips=1,
            firstSlice=2,
            numSlices=1,
        )
        self.depth = types.SimpleNamespace(
            resource=202,
            firstMip=0,
            numMips=1,
            firstSlice=0,
            numSlices=1,
        )

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))
        self.current[0] = event_id

    def GetRootActions(self):
        return [types.SimpleNamespace(eventId=41, children=[])]

    def GetPipelineState(self):
        return types.SimpleNamespace(
            GetOutputTargets=lambda: [self.color],
            GetDepthTarget=lambda: self.depth,
        )

    def GetTextures(self):
        return [
            types.SimpleNamespace(
                resourceId=101,
                width=128,
                height=64,
                depth=1,
                arraysize=4,
                mips=4,
                msSamp=2,
                byteSize=self.texture_byte_size,
                format=types.SimpleNamespace(name="RGBA16_FLOAT", compCount=4),
            ),
            types.SimpleNamespace(
                resourceId=202,
                width=128,
                height=64,
                depth=1,
                arraysize=1,
                mips=1,
                msSamp=1,
                byteSize=self.texture_byte_size,
                format=types.SimpleNamespace(name="D32_FLOAT", compCount=1),
            ),
        ]

    def GetMinMax(self, resource, subresource, cast):
        return (
            types.SimpleNamespace(floatValue=[0.2, 0.1, 0.3, 0.4]),
            types.SimpleNamespace(floatValue=[0.8, 0.9, 0.7, 1.0]),
        )

    def SaveTexture(self, save, path):
        self.saved.append((save, path))
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"evidence")
        return types.SimpleNamespace(code="succeeded")


class EventTextureEvidenceBridgeTests(unittest.TestCase):
    def _fixture(self, *, texture_byte_size=1024 * 1024):
        bridge = load_bridge()
        bridge.rd.TextureSave = _TextureSave
        bridge.rd.FileType = types.SimpleNamespace(PNG="png")
        bridge.rd.CompType = types.SimpleNamespace(Typeless="typeless")
        bridge.rd.ResultCode = types.SimpleNamespace(Succeeded="succeeded")
        bridge.rd.Subresource = lambda mip, array_slice, sample: (
            mip,
            array_slice,
            sample,
        )
        current = [7]
        controller = _Controller(current, texture_byte_size=texture_byte_size)
        bridge._capture_session_id = "a" * 32
        bridge._ctx = types.SimpleNamespace(CurEvent=lambda: current[0])
        bridge._replay = lambda callback: callback(controller)
        return bridge, controller

    def _params(self, request_dir: Path, **overrides):
        value = {
            "event_id": 41,
            "attachment_kind": "rtv",
            "attachment_index": 0,
            "channel": "r",
            "mip": 1,
            "slice": 2,
            "sample": 1,
            "range_mode": "subresource-minmax",
            "black_point": None,
            "white_point": None,
            "request_id": "b" * 32,
            "artifact_dir": str(request_dir),
            "expected_capture_session_id": "a" * 32,
        }
        value.update(overrides)
        return value

    def test_composite_is_registered_separately_from_legacy_save(self) -> None:
        bridge, unused_controller = self._fixture()
        self.assertIs(
            bridge.METHODS["capture_event_texture_evidence"],
            bridge._capture_event_texture_evidence,
        )
        self.assertIsNot(
            bridge.METHODS["capture_event_texture_evidence"],
            bridge.METHODS["save_texture"],
        )

    def test_saves_exact_bound_channel_range_and_restores_event(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir)
            )

            self.assertEqual(result["schema"], "replaylab-event-texture-evidence/v1")
            self.assertEqual(result["artifact"]["status"], "saved")
            self.assertEqual(controller.events, [(41, True), (7, True)])
            save, temporary_path = controller.saved[0]
            self.assertEqual(save.resourceId, 101)
            self.assertEqual(save.mip, 1)
            self.assertEqual(save.slice.sliceIndex, 2)
            self.assertEqual(save.sample.sampleIndex, 1)
            self.assertEqual(save.channelExtract, 0)
            self.assertEqual(save.comp.blackPoint, 0.2)
            self.assertEqual(save.comp.whitePoint, 0.8)
            self.assertFalse(Path(temporary_path).exists())
            output = result["artifact"]["output"]
            output_path = Path(output["path"])
            self.assertEqual(output_path.parent, request_dir)
            self.assertEqual(output["mimeType"], "image/png")
            self.assertEqual(
                output["sha256"], hashlib.sha256(output_path.read_bytes()).hexdigest()
            )

    def test_depth_is_raw_component_zero_with_explicit_unit_range(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(
                    request_dir,
                    attachment_kind="dsv",
                    channel="depth",
                    mip=0,
                    slice=0,
                    sample=0,
                    range_mode="explicit",
                    black_point=0.25,
                    white_point=0.75,
                )
            )

            save = controller.saved[0][0]
            self.assertEqual(save.resourceId, 202)
            self.assertEqual(save.channelExtract, 0)
            self.assertEqual(save.comp.blackPoint, 0.25)
            self.assertEqual(save.comp.whitePoint, 0.75)
            self.assertEqual(
                result["artifact"]["display"]["depthInterpretation"],
                "raw-resource-component-0",
            )

    def test_unbound_slot_fails_closed_and_still_restores_event(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            with self.assertRaisesRegex(RuntimeError, "RTV slot is not bound"):
                bridge._capture_event_texture_evidence(
                    self._params(request_dir, attachment_index=1)
                )
            self.assertEqual(controller.events, [(41, True), (7, True)])
            self.assertEqual(list(request_dir.iterdir()), [])

    def test_source_budget_returns_skip_receipt_without_saving(self) -> None:
        bridge, controller = self._fixture(
            texture_byte_size=256 * 1024 * 1024 + 1
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir, range_mode="native")
            )

            self.assertEqual(result["artifact"]["status"], "skipped")
            self.assertEqual(
                result["artifact"]["skipReason"],
                "source-size-budget-exceeded",
            )
            self.assertEqual(controller.saved, [])
            self.assertEqual(controller.events, [(41, True), (7, True)])
            self.assertEqual(list(request_dir.iterdir()), [])

    def test_flat_subresource_range_returns_a_verified_skip(self) -> None:
        bridge, controller = self._fixture()
        controller.GetMinMax = lambda resource, subresource, cast: (
            types.SimpleNamespace(floatValue=[0.5, 0.5, 0.5, 0.5]),
            types.SimpleNamespace(floatValue=[0.5, 0.5, 0.5, 0.5]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir)
            )

            self.assertEqual(result["artifact"]["status"], "skipped")
            self.assertEqual(
                result["artifact"]["skipReason"], "subresource-range-flat"
            )
            self.assertEqual(result["artifact"]["display"]["blackPoint"], 0.5)
            self.assertEqual(result["artifact"]["display"]["whitePoint"], 0.5)
            self.assertEqual(controller.saved, [])
            self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_nonempty_host_directory_is_rejected_before_replay(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            (request_dir / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new empty directory"):
                bridge._capture_event_texture_evidence(self._params(request_dir))
            self.assertEqual(controller.events, [])

    def test_depth_range_outside_unit_interval_is_rejected(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            with self.assertRaisesRegex(ValueError, r"within \[0, 1\]"):
                bridge._capture_event_texture_evidence(
                    self._params(
                        request_dir,
                        attachment_kind="dsv",
                        channel="depth",
                        mip=0,
                        slice=0,
                        sample=0,
                        range_mode="explicit",
                        black_point=-0.1,
                        white_point=1.0,
                    )
                )
            self.assertEqual(controller.events, [])


if __name__ == "__main__":
    unittest.main()
