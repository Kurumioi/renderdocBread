"""Compact frozen target metadata for task-scoped ReplayLab MCP routes.

This is deliberately not a workflow state machine.  It records the native UI
target, validates its shape, and rejects explicit arguments that contradict
that target.  Tool effects and call counts are enforced independently by the
MCP filter.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from .errors import PolicyError


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 1 <= result <= 0xFFFFFFFFFFFFFFFF else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 0x7FFFFFFF else None


def _stage(value: Any) -> str:
    stage = str(value or "").strip().upper()
    aliases = {
        "VERTEX": "VS",
        "HULL": "HS",
        "DOMAIN": "DS",
        "GEOMETRY": "GS",
        "PIXEL": "PS",
        "FRAGMENT": "PS",
        "COMPUTE": "CS",
        "TASK": "AS",
        "AMPLIFICATION": "AS",
        "MESH": "MS",
    }
    return aliases.get(stage, stage)


@dataclass(frozen=True, slots=True)
class ToolCallScope:
    intent_kind: str = ""
    event_id: int | None = None
    resource_id: int | None = None
    stage: str = ""
    x: int | None = None
    y: int | None = None
    mip: int | None = None
    slice: int | None = None
    sample: int | None = None
    entry_point: str = ""
    raw_sha256: str = ""
    capture_session_id: str = ""
    capture_id: str = ""
    capture_path: str = ""

    @property
    def exact_target(self) -> bool:
        return self.intent_kind in {
            "explain-current-event",
            "inspect-picked-pixel",
            "reverse-edit-selected-shader",
        }

    @classmethod
    def from_request(
        cls, context: dict[str, Any], intent: dict[str, Any] | None
    ) -> "ToolCallScope":
        intent_value = _mapping(intent)
        arguments = _mapping(intent_value.get("arguments"))
        kind = str(intent_value.get("kind", "") or "")
        events = _mapping(context.get("events"))
        action = _mapping(context.get("action"))
        texture = _mapping(context.get("texture"))
        capture = _mapping(context.get("capture"))
        picked = _mapping(arguments.get("pixel")) or _mapping(texture.get("pickedPixel"))
        subresource = _mapping(arguments.get("subresource")) or _mapping(
            texture.get("subresource")
        )
        focused_shader = _mapping(_mapping(context.get("shaders")).get("focused"))

        event_id = _positive_int(_first(arguments, "eventId", "event_id"))
        if event_id is None:
            event_id = _positive_int(
                _first(events, "currentEventId", "selectedEventId", "eventId")
            ) or _positive_int(_first(action, "eventId", "event_id"))
        resource_id = _positive_int(
            _first(
                arguments,
                "resourceId",
                "resource_id",
                "textureId",
                "texture_id",
                "textureResourceId",
                "shaderResourceId",
            )
        )
        if resource_id is None:
            source = focused_shader if kind == "reverse-edit-selected-shader" else texture
            resource_id = _positive_int(_first(source, "resourceId", "resource_id"))
        stage = _stage(
            _first(arguments, "stage", "shaderStage")
            or focused_shader.get("stage")
            or context.get("focusStage")
        )
        x = _nonnegative_int(_first(arguments, "x", "pixelX"))
        y = _nonnegative_int(_first(arguments, "y", "pixelY"))
        if x is None:
            x = _nonnegative_int(picked.get("x"))
        if y is None:
            y = _nonnegative_int(picked.get("y"))
        mip = _nonnegative_int(
            _first(arguments, "mip", "mipLevel")
            if _first(arguments, "mip", "mipLevel") is not None
            else subresource.get("mip")
        )
        slice_index = _nonnegative_int(
            _first(arguments, "slice", "arraySlice")
            if _first(arguments, "slice", "arraySlice") is not None
            else subresource.get("slice")
        )
        sample = _nonnegative_int(
            _first(arguments, "sample", "sampleIndex")
            if _first(arguments, "sample", "sampleIndex") is not None
            else subresource.get("sample")
        )
        entry_point = str(
            _first(arguments, "entryPoint", "entry_point")
            or focused_shader.get("entryPoint", "")
        ).strip()
        raw_sha256 = str(
            _first(arguments, "rawSha256", "raw_sha256")
            or focused_shader.get("rawSha256", "")
        ).strip().lower()

        if kind == "explain-current-event":
            resource_id = None
            stage = entry_point = raw_sha256 = ""
            x = y = mip = slice_index = sample = None
        elif kind == "inspect-picked-pixel":
            stage = entry_point = raw_sha256 = ""
        elif kind == "reverse-edit-selected-shader":
            x = y = mip = slice_index = sample = None

        value = cls(
            intent_kind=kind,
            event_id=event_id,
            resource_id=resource_id,
            stage=stage,
            x=x,
            y=y,
            mip=mip,
            slice=slice_index,
            sample=sample,
            entry_point=entry_point,
            raw_sha256=raw_sha256,
            capture_session_id=str(capture.get("captureSessionId", "") or "")
            .strip()
            .lower(),
            capture_id=str(capture.get("captureId", "") or "").strip().lower(),
            capture_path=str(capture.get("path", "") or "").strip(),
        )
        value.require_complete()
        return value

    def require_complete(self) -> None:
        if not self.intent_kind:
            return
        if not re.fullmatch(r"[0-9a-f]{32}", self.capture_session_id):
            raise PolicyError("UI intent is missing its exact capture session identity")
        if self.intent_kind == "explain-current-event" and self.event_id is None:
            raise PolicyError("explain intent is missing its frozen event id")
        if self.intent_kind == "inspect-picked-pixel" and any(
            value is None
            for value in (
                self.event_id,
                self.resource_id,
                self.x,
                self.y,
                self.mip,
                self.slice,
                self.sample,
            )
        ):
            raise PolicyError(
                "pixel intent is missing its frozen event/resource/pixel/subresource identity"
            )
        if self.intent_kind == "reverse-edit-selected-shader" and (
            self.event_id is None
            or self.resource_id is None
            or not self.stage
            or not self.entry_point
            or re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256) is None
        ):
            raise PolicyError(
                "shader intent is missing its exact event/stage/resource/entry/raw identity"
            )

    def validate(self, tool: str, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise PolicyError("RenderDoc tool arguments must be an object")
        checks = (
            (("event_id", "eventId", "eid"), self.event_id, _positive_int),
            (("resource_id", "resourceId", "texture_id", "textureId"), self.resource_id, _positive_int),
            (("stage", "shaderStage"), self.stage, _stage),
            (("x", "pixelX"), self.x, _nonnegative_int),
            (("y", "pixelY"), self.y, _nonnegative_int),
            (("mip", "mipLevel"), self.mip, _nonnegative_int),
            (("slice", "arraySlice"), self.slice, _nonnegative_int),
            (("sample", "sampleIndex"), self.sample, _nonnegative_int),
        )
        for names, expected, normalize in checks:
            supplied = _first(arguments, *names)
            if self.exact_target and supplied is not None and expected is not None:
                if normalize(supplied) != expected:
                    raise PolicyError(
                        f"tool '{tool}' arguments conflict with the frozen UI target"
                    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "ToolCallScope":
        if not value:
            return cls()
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PolicyError("ReplayLab tool scope is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise PolicyError("ReplayLab tool scope must be a JSON object")
        allowed = {item.name for item in fields(cls)}
        if set(decoded) - allowed:
            raise PolicyError("ReplayLab tool scope contains unknown fields")
        try:
            scope = cls(**decoded)
        except TypeError as exc:
            raise PolicyError("ReplayLab tool scope has invalid field types") from exc
        scope.require_complete()
        return scope


__all__ = ["ToolCallScope"]
