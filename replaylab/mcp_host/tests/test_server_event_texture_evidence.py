from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from unittest import mock

from src import server


SESSION = "a" * 32


def _context(session: str = SESSION) -> dict:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "schemaVersion": 1,
        "capture": {
            "loaded": True,
            "captureId": "",
            "captureSessionId": session,
            "path": "C:/captures/frame.rdc",
            "api": "D3D12",
        },
        "events": {"currentEventId": 7, "selectedEventId": 7},
    }


class EvidenceIPC:
    def __init__(
        self,
        *,
        skip_reason: str | None = None,
        outside_path: Path | None = None,
        wrong_hash: bool = False,
        race_session: str | None = None,
    ) -> None:
        self.skip_reason = skip_reason
        self.outside_path = outside_path
        self.wrong_hash = wrong_hash
        self.race_session = race_session
        self.context_calls = 0
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method, params=None, timeout=None):
        payload = dict(params or {})
        self.calls.append((method, payload, timeout))
        if method == "get_current_renderdoc_context":
            self.context_calls += 1
            session = (
                self.race_session
                if self.race_session and self.context_calls > 1
                else SESSION
            )
            return copy.deepcopy(_context(session))
        if method != "capture_event_texture_evidence":
            raise AssertionError("unexpected IPC method: %s" % method)

        request_dir = Path(payload["artifact_dir"])
        attachment = {
            "kind": payload["attachment_kind"],
            "index": payload["attachment_index"],
            "resourceId": 101,
            "mip": payload["mip"],
            "slice": payload["slice"],
            "sample": payload["sample"],
            "viewWidth": 64,
            "viewHeight": 32,
            "format": "RGBA16_FLOAT",
            "textureByteSize": (
                256 * 1024 * 1024 + 1
                if self.skip_reason == "source-size-budget-exceeded"
                else 1024 * 1024
            ),
            "sampleCount": 2,
            "descriptor": {
                "firstMip": payload["mip"],
                "numMips": 1,
                "firstSlice": payload["slice"],
                "numSlices": 1,
            },
        }
        is_minmax = payload["range_mode"] == "subresource-minmax"
        is_flat = self.skip_reason == "subresource-range-flat"
        display = {
            "channel": payload["channel"],
            "channelExtract": {
                "rgba": -1,
                "r": 0,
                "g": 1,
                "b": 2,
                "a": 3,
                "depth": 0,
            }[payload["channel"]],
            "rangeMode": payload["range_mode"],
            "blackPoint": (
                payload["black_point"]
                if payload["range_mode"] == "explicit"
                else (0.5 if is_flat else (0.2 if is_minmax else 0.0))
            ),
            "whitePoint": (
                payload["white_point"]
                if payload["range_mode"] == "explicit"
                else (0.5 if is_flat else (0.8 if is_minmax else 1.0))
            ),
            "subresourceMin": (
                [0.5, 0.5, 0.5, 0.5]
                if is_flat
                else ([0.2, 0.3, 0.4, 0.5] if is_minmax else None)
            ),
            "subresourceMax": (
                [0.5, 0.5, 0.5, 0.5]
                if is_flat
                else ([0.8, 0.7, 0.6, 0.5] if is_minmax else None)
            ),
            "depthInterpretation": (
                "raw-resource-component-0"
                if payload["channel"] == "depth"
                else None
            ),
        }
        if self.skip_reason:
            artifact = {
                "status": "skipped",
                "skipReason": self.skip_reason,
                "attachment": attachment,
                "display": display,
                "output": None,
            }
        else:
            filename = server._event_texture_evidence_filename(
                payload["event_id"],
                payload["attachment_kind"],
                payload["attachment_index"],
                payload["mip"],
                payload["slice"],
                payload["sample"],
                payload["channel"],
            )
            output_path = self.outside_path or (request_dir / filename)
            data = b"\x89PNG\r\n\x1a\nmanaged-evidence"
            output_path.write_bytes(data)
            artifact = {
                "status": "saved",
                "skipReason": None,
                "attachment": attachment,
                "display": display,
                "output": {
                    "path": str(output_path),
                    "mimeType": "image/png",
                    "byteSize": len(data),
                    "sha256": (
                        "0" * 64
                        if self.wrong_hash
                        else hashlib.sha256(data).hexdigest()
                    ),
                    "noOverwrite": True,
                },
            }
        return {
            "schema": "replaylab-event-texture-evidence/v1",
            "schemaVersion": 1,
            "requestId": payload["request_id"],
            "captureSessionId": payload["expected_capture_session_id"],
            "eventId": payload["event_id"],
            "restoration": {"originalEventId": 7, "restored": True},
            "exactEventBinding": True,
            "replayMutation": False,
            "budgets": {
                "maxTextureByteSize": 256 * 1024 * 1024,
                "maxSubresourcePixels": 16 * 1024 * 1024,
                "maxPngByteSize": 128 * 1024 * 1024,
            },
            "artifact": artifact,
        }


def _capture(root: Path, ipc: EvidenceIPC, **overrides) -> dict:
    arguments = {
        "event_id": 41,
        "attachment_kind": "rtv",
        "attachment_index": 0,
        "channel": "r",
        "mip": 1,
        "slice": 2,
        "sample": 1,
        "range_mode": "native",
    }
    arguments.update(overrides)
    with (
        mock.patch.object(server, "ipc", ipc),
        mock.patch.object(server, "_EVENT_TEXTURE_EVIDENCE_ROOT", root),
    ):
        return json.loads(server.capture_event_texture_evidence(**arguments))


def test_public_tool_has_no_output_path_and_returns_verified_manifest(tmp_path: Path) -> None:
    signature = inspect.signature(server.capture_event_texture_evidence)
    assert "output_path" not in signature.parameters
    assert "artifact_dir" not in signature.parameters

    ipc = EvidenceIPC()
    result = _capture(tmp_path / "managed", ipc)

    assert result["schema"] == "replaylab-event-texture-evidence-host/v1"
    assert result["status"] == "saved"
    assert result["artifact"]["output"]["mimeType"] == "image/png"
    output_path = Path(result["artifact"]["output"]["path"])
    manifest_path = Path(result["manifest"]["path"])
    assert output_path.is_file()
    assert manifest_path.is_file()
    assert output_path.parent == manifest_path.parent
    assert result["manifest"]["sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact"]["output"]["path"] == str(output_path)
    assert manifest["policy"]["callerSuppliedPath"] is False

    bridge_call = next(call for call in ipc.calls if call[0] == "capture_event_texture_evidence")
    assert bridge_call[1]["expected_capture_session_id"] == SESSION
    assert bridge_call[1]["event_id"] == 41
    assert Path(bridge_call[1]["artifact_dir"]).parent == (tmp_path / "managed")
    assert bridge_call[2] == 300.0


def test_budget_skip_still_gets_a_hashed_manifest(tmp_path: Path) -> None:
    ipc = EvidenceIPC(skip_reason="source-size-budget-exceeded")
    result = _capture(tmp_path / "managed", ipc)

    assert result["status"] == "skipped"
    assert result["artifact"]["output"] is None
    assert result["artifact"]["skipReason"] == "source-size-budget-exceeded"
    request_dir = Path(result["manifest"]["path"]).parent
    assert sorted(path.name for path in request_dir.iterdir()) == ["manifest.json"]


def test_flat_minmax_skip_is_preserved_in_manifest(tmp_path: Path) -> None:
    ipc = EvidenceIPC(skip_reason="subresource-range-flat")
    result = _capture(
        tmp_path / "managed", ipc, range_mode="subresource-minmax"
    )

    assert result["status"] == "skipped"
    assert result["artifact"]["skipReason"] == "subresource-range-flat"
    assert result["artifact"]["display"]["blackPoint"] == 0.5
    assert Path(result["manifest"]["path"]).is_file()


def test_outside_bridge_path_is_rejected_and_never_exposed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    ipc = EvidenceIPC(outside_path=outside)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "unsafe texture evidence path" in result["error"]
    assert outside.is_file()
    assert list((tmp_path / "managed").iterdir()) == []


def test_hash_mismatch_fails_closed_and_cleans_owned_request(tmp_path: Path) -> None:
    ipc = EvidenceIPC(wrong_hash=True)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "integrity data" in result["error"]
    assert list((tmp_path / "managed").iterdir()) == []


def test_capture_session_race_fails_closed(tmp_path: Path) -> None:
    ipc = EvidenceIPC(race_session="c" * 32)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "capture/session changed" in result["error"]
    assert list((tmp_path / "managed").iterdir()) == []


def test_scoped_capture_session_mismatch_fails_before_export(tmp_path: Path) -> None:
    ipc = EvidenceIPC()
    with mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", "c" * 32):
        result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "scoped capture session changed" in result["error"]
    assert all(method != "capture_event_texture_evidence" for method, _, _ in ipc.calls)


def test_non_directory_managed_root_fails_before_bridge_export(tmp_path: Path) -> None:
    root = tmp_path / "managed-root"
    root.write_text("not a directory", encoding="utf-8")
    ipc = EvidenceIPC()
    result = _capture(root, ipc)

    assert result["type"] == "RuntimeError"
    assert "root" in result["error"]
    assert all(method != "capture_event_texture_evidence" for method, _, _ in ipc.calls)


def test_each_request_uses_a_unique_no_overwrite_directory(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    first = _capture(root, EvidenceIPC())
    second = _capture(root, EvidenceIPC())

    first_dir = Path(first["manifest"]["path"]).parent
    second_dir = Path(second["manifest"]["path"]).parent
    assert first_dir != second_dir
    assert first_dir.is_dir()
    assert second_dir.is_dir()


def test_depth_contract_rejects_colour_and_out_of_range_values_before_io(
    tmp_path: Path,
) -> None:
    ipc = EvidenceIPC()
    colour_result = _capture(
        tmp_path / "managed-colour",
        ipc,
        attachment_kind="dsv",
        channel="r",
        mip=0,
        slice=0,
        sample=0,
    )
    assert colour_result["type"] == "ValueError"

    range_result = _capture(
        tmp_path / "managed-range",
        ipc,
        attachment_kind="dsv",
        channel="depth",
        mip=0,
        slice=0,
        sample=0,
        range_mode="explicit",
        black_point=-0.1,
        white_point=1.0,
    )
    assert range_result["type"] == "ValueError"
    assert ipc.calls == []
