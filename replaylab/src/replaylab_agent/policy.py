"""Authoritative ReplayLab MCP policy and frozen-intent argument binding.

The tool classification mirrors the policy shipped by the current
ReplayLabMCP host.  Unknown tools fail closed: a name is model-callable only
when it is explicitly present in ``READ_ONLY_TOOLS``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from .errors import PolicyError


READ_ONLY_TOOLS = frozenset(
    {
        "ping",
        "get_capture_status",
        "debug_vulkan_bindings",
        "analyze_lighting",
        "get_draw_calls",
        "get_frame_summary",
        "get_draw_call_details",
        "get_pipeline_state",
        "get_shader_info",
        "reverse_shader",
        "get_bound_textures",
        "find_draws_by_shader",
        "find_draws_by_texture",
        "find_draws_by_resource",
        "get_textures",
        "get_buffers",
        "get_resources",
        "get_texture_info",
        "pick_pixel",
        "get_texture_minmax",
        "pixel_history",
        "inspect_pixel_object",
        "debug_pixel",
        "debug_vertex",
        "enumerate_counters",
        "fetch_counters",
        "get_debug_messages",
        "get_action_timings",
        "get_symbol_match_cases",
        "get_symbol_match_evidence",
        "list_symbol_matches",
        "get_shader_workspace_status",
        "list_shader_workspace_family_uses",
        "get_shader_toolchain_capabilities",
        "plan_shader_toolchain_route",
        "plan_shader_recovery",
        "list_material_sessions",
        "get_material_session_status",
        "poll_shader_workspace_watch",
        "get_replay_project_status",
        "get_replay_project_reuse_plan",
        "get_renderdoc_ui_intent_status",
    }
)


EFFECTFUL_TOOLS = frozenset(
    {
        "open_capture",
        "launch_renderdoc",
        "list_captures",
        "identify_drawcalls",
        "save_texture",
        "export_drawcall",
        "export_to_unity",
        "export_capture_symbol_catalog",
        "scan_asset_symbols",
        "match_capture_assets",
        "resolve_capture_symbols",
        "submit_symbol_match",
        "export_symbol_alias_table",
        "apply_symbol_alias_table",
        "create_shader_workspace",
        "prepare_shader_workspace",
        "get_current_shader_representation",
        "decompile_shader_workspace",
        "get_shader_workspace_representation",
        "promote_shader_workspace_explanation",
        "compile_shader_workspace",
        "begin_shader_workspace_organization",
        "review_shader_workspace_organization",
        "validate_shader_workspace_candidate",
        "apply_shader_workspace_candidate",
        "apply_shader_workspace_candidate_family",
        "apply_shader_workspace_shared_resource",
        "rollback_shader_workspace_override",
        "rollback_shader_workspace_override_family",
        "rollback_shader_workspace_shared_resource",
        "prepare_material_session",
        "update_material_parameter",
        "rollback_material_session",
        "clear_all_material_sessions",
        "start_shader_workspace_watch",
        "stop_shader_workspace_watch",
        "run_shader_toolchain_round_trip_smoke",
        "create_replay_project",
        "import_replay_project_capture",
        "register_replay_project_workspace",
        "register_replay_project_alias_table",
        "save_replay_project_material_knowledge",
        "seed_shader_workspace_from_replay_project",
    }
)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _stage(value: Any) -> str:
    stage = str(value or "").strip().lower().replace("shaderstage.", "")
    return {
        "vs": "vertex",
        "ps": "fragment",
        "pixel": "fragment",
        "fs": "fragment",
        "cs": "compute",
        "hs": "hull",
        "ds": "domain",
        "gs": "geometry",
    }.get(stage, stage)


@dataclass(frozen=True, slots=True)
class ToolCallScope:
    """Exact identifiers frozen by a native qrenderdoc button intent.

    General chat/analysis has an empty ``intent_kind`` and is capture-bounded
    only.  Button intents additionally bind every relevant tool argument so a
    model cannot silently inspect another event, shader, resource, or texel.
    """

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
        shaders = _mapping(context.get("shaders"))
        focused_shader = _mapping(shaders.get("focused"))

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
            if kind == "reverse-edit-selected-shader":
                resource_id = _positive_int(focused_shader.get("resourceId"))
            else:
                resource_id = _positive_int(_first(texture, "resourceId", "resource_id"))
        stage = _stage(
            _first(arguments, "stage", "shaderStage")
            or focused_shader.get("stage")
            or context.get("focusStage", "")
        )
        x = _nonnegative_int(_first(arguments, "x", "pixelX"))
        y = _nonnegative_int(_first(arguments, "y", "pixelY"))
        if x is None:
            x = _nonnegative_int(picked.get("x"))
        if y is None:
            y = _nonnegative_int(picked.get("y"))
        scope = cls(
            intent_kind=kind,
            event_id=event_id,
            resource_id=resource_id,
            stage=stage,
            x=x,
            y=y,
            mip=_nonnegative_int(_first(arguments, "mip", "mipLevel"))
            if _first(arguments, "mip", "mipLevel") is not None
            else _nonnegative_int(subresource.get("mip")),
            slice=_nonnegative_int(_first(arguments, "slice", "arraySlice"))
            if _first(arguments, "slice", "arraySlice") is not None
            else _nonnegative_int(subresource.get("slice")),
            sample=_nonnegative_int(_first(arguments, "sample", "sampleIndex"))
            if _first(arguments, "sample", "sampleIndex") is not None
            else _nonnegative_int(subresource.get("sample")),
            entry_point=str(
                _first(arguments, "entryPoint", "entry_point")
                or focused_shader.get("entryPoint", "")
            ).strip(),
            raw_sha256=str(
                _first(arguments, "rawSha256", "raw_sha256")
                or focused_shader.get("rawSha256", "")
            ).strip().lower(),
            capture_session_id=str(capture.get("captureSessionId", "") or "")
            .strip()
            .lower(),
            capture_id=str(capture.get("captureId", "") or "").strip().lower(),
            capture_path=str(capture.get("path", "") or "").strip(),
        )
        scope.require_complete()
        return scope

    def require_complete(self) -> None:
        if not self.intent_kind:
            return
        if (
            len(self.capture_session_id) != 32
            or any(
                character not in "0123456789abcdef"
                for character in self.capture_session_id
            )
        ):
            raise PolicyError("UI intent is missing its exact capture session identity")
        if self.intent_kind == "explain-current-event":
            if self.event_id is None:
                raise PolicyError("explain intent is missing its frozen event id")
            return
        if self.intent_kind == "inspect-picked-pixel":
            if any(
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
            return
        if self.intent_kind == "reverse-edit-selected-shader":
            if (
                self.event_id is None
                or self.resource_id is None
                or not self.stage
                or not self.entry_point
                or len(self.raw_sha256) != 64
                or any(character not in "0123456789abcdef" for character in self.raw_sha256)
            ):
                raise PolicyError(
                    "shader intent is missing its exact event/stage/resource/entry/raw identity"
                )
            return
        raise PolicyError(f"unsupported ReplayLab UI intent scope: {self.intent_kind}")

    def validate_live_context(self, context: dict[str, Any]) -> None:
        if not self.capture_session_id:
            return
        capture = _mapping(context.get("capture"))
        if capture.get("loaded") is not True:
            raise PolicyError("the pinned qrenderdoc capture is no longer loaded")
        observed_session = str(capture.get("captureSessionId", "") or "").strip().lower()
        if observed_session != self.capture_session_id:
            raise PolicyError("the pinned qrenderdoc capture session changed during the tool call")
        observed_id = str(capture.get("captureId", "") or "").strip().lower()
        if self.capture_id and observed_id != self.capture_id:
            raise PolicyError("the pinned qrenderdoc capture identity changed during the tool call")
        # ``captureSessionId`` is the native, high-entropy identity minted for
        # this exact loaded capture.  The external Windows MCP bridge can
        # return a lossy ANSI rendering of a non-ASCII capture path even while
        # it is attached to that same session.  Keep the path as diagnostic
        # context, but never let that presentation-only value override a
        # matching session id.  Capture/session ids and the intent-specific
        # event/resource guards below remain authoritative.
        if self.intent_kind == "inspect-picked-pixel":
            events = _mapping(context.get("events"))
            texture = _mapping(context.get("texture"))
            picked = _mapping(texture.get("pickedPixel"))
            subresource = _mapping(texture.get("subresource"))
            if _positive_int(events.get("currentEventId")) != self.event_id:
                raise PolicyError("the frozen pixel cutoff event changed during the tool call")
            if _positive_int(texture.get("resourceId")) != self.resource_id:
                raise PolicyError("the frozen texture selection changed during the tool call")
            for name, expected, observed in (
                ("x", self.x, picked.get("x")),
                ("y", self.y, picked.get("y")),
                ("mip", self.mip, subresource.get("mip")),
                ("slice", self.slice, subresource.get("slice")),
                ("sample", self.sample, subresource.get("sample")),
            ):
                if _nonnegative_int(observed) != expected:
                    raise PolicyError(
                        f"the frozen pixel {name} changed during the tool call"
                    )
        if self.intent_kind == "reverse-edit-selected-shader":
            events = _mapping(context.get("events"))
            shaders = _mapping(context.get("shaders"))
            bound = shaders.get("bound", [])
            if _positive_int(events.get("currentEventId")) != self.event_id:
                raise PolicyError("the frozen shader event changed during the tool call")
            if not isinstance(bound, list):
                raise PolicyError("live qrenderdoc shader bindings are invalid")
            matches = [
                value
                for value in bound
                if isinstance(value, dict) and _stage(value.get("stage")) == self.stage
            ]
            if len(matches) != 1:
                raise PolicyError("the frozen shader stage is no longer bound exactly once")
            observed = matches[0]
            if (
                _positive_int(observed.get("resourceId")) != self.resource_id
                or str(observed.get("entryPoint", "") or "").strip() != self.entry_point
                or str(observed.get("rawSha256", "") or "").strip().lower()
                != self.raw_sha256
            ):
                raise PolicyError(
                    "the frozen shader resource/entry/raw identity changed during the tool call"
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
        allowed = {field.name for field in fields(cls)}
        if set(decoded) - allowed:
            raise PolicyError("ReplayLab tool scope contains unknown fields")
        try:
            scope = cls(**decoded)
        except TypeError as exc:
            raise PolicyError("ReplayLab tool scope has invalid field types") from exc
        scope.require_complete()
        return scope

    def validate(self, tool: str, arguments: dict[str, Any]) -> None:
        if not self.intent_kind:
            return
        if not isinstance(arguments, dict):
            raise PolicyError("ReplayLab MCP tool arguments must be an object")

        event_tools = {
            "get_draw_call_details",
            "get_pipeline_state",
            "get_shader_info",
            "reverse_shader",
            "get_bound_textures",
            "debug_pixel",
        }
        resource_tools = {
            "get_texture_info",
            "get_texture_minmax",
            "pixel_history",
            "inspect_pixel_object",
        }
        if tool in event_tools and self.event_id is not None:
            actual = _positive_int(_first(arguments, "event_id", "eventId"))
            if actual != self.event_id:
                raise PolicyError(
                    f"tool '{tool}' must use frozen event {self.event_id}"
                )
        if tool in resource_tools and self.resource_id is not None:
            actual = _positive_int(
                _first(arguments, "resource_id", "resourceId", "texture_id", "textureId")
            )
            if actual != self.resource_id:
                raise PolicyError(
                    f"tool '{tool}' must use frozen resource {self.resource_id}"
                )
        if tool in {"pixel_history", "inspect_pixel_object", "debug_pixel"}:
            for name, expected in (("x", self.x), ("y", self.y)):
                if expected is not None and _nonnegative_int(arguments.get(name)) != expected:
                    raise PolicyError(
                        f"tool '{tool}' must use frozen pixel {name}={expected}"
                    )
        if tool == "inspect_pixel_object":
            for name, expected in (
                ("mip", self.mip),
                ("slice", self.slice),
                ("sample", self.sample),
            ):
                if expected is not None and _nonnegative_int(arguments.get(name)) != expected:
                    raise PolicyError(
                        f"tool '{tool}' must use frozen subresource {name}={expected}"
                    )
        if tool == "debug_pixel" and self.sample is not None:
            if _nonnegative_int(arguments.get("sample")) != self.sample:
                raise PolicyError(
                    f"tool '{tool}' must use frozen sample {self.sample}"
                )
        if tool in {
            "get_shader_info",
            "reverse_shader",
            "get_bound_textures",
            "get_shader_toolchain_capabilities",
        } and self.stage:
            actual = _stage(arguments.get("stage", ""))
            if actual != self.stage:
                raise PolicyError(f"tool '{tool}' must use frozen shader stage {self.stage}")


__all__ = ["EFFECTFUL_TOOLS", "READ_ONLY_TOOLS", "ToolCallScope"]
