"""RenderDoc tool effects and frozen-request argument binding.

The Skill catalog describes *what* a specialist may use.  This module describes
the observable effect class of those tools and the evidence/order invariants
that must hold while they run.  It deliberately does not use a growing list of
per-button permissions as the workflow router: new Skill tools are discovered
from the active Host and fall back to the reversible ``workspace`` effect class
until they receive a narrower classification.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .errors import EvidenceValidationError, PolicyError


READ_ONLY_TOOLS = frozenset(
    {
        "ping",
        "get_capture_status",
        "inspect_event",
        "analyze_pixel",
        "inspect_shader",
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
        "get_active_shader_workspace_path",
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
        "prepare_active_capture_project",
        "register_active_project_workspace",
        "restore_current_shader",
        "apply_current_frame_pass_ai_proposals",
    }
)


WORKFLOW_EFFECTS = frozenset({"workspace", "project", "ui", "replay"})

# These are workflow/acceptance classes, not user-facing permission names.
# A task requests an outcome class once; individual tools remain free to evolve
# inside the specialist as long as the Host discovers them and their postconditions
# satisfy the same class contract.
TOOL_EFFECTS: dict[str, frozenset[str]] = {
    # Managed, auditable artifacts.  No live replay replacement is implied.
    **{
        name: frozenset({"workspace"})
        for name in (
            "create_shader_workspace",
            "prepare_shader_workspace",
            "apply_shader_workspace_candidate",
            "get_current_shader_representation",
            "decompile_shader_workspace",
            "get_shader_workspace_representation",
            "promote_shader_workspace_explanation",
            "compile_shader_workspace",
            "begin_shader_workspace_organization",
            "review_shader_workspace_organization",
            "validate_shader_workspace_candidate",
            "start_shader_workspace_watch",
            "stop_shader_workspace_watch",
            "run_shader_toolchain_round_trip_smoke",
            "prepare_material_session",
            "update_material_parameter",
            "rollback_material_session",
            "clear_all_material_sessions",
        )
    },
    **{
        name: frozenset({"project"})
        for name in (
            "create_replay_project",
            "import_replay_project_capture",
            "register_replay_project_workspace",
            "register_replay_project_alias_table",
            "save_replay_project_material_knowledge",
            "seed_shader_workspace_from_replay_project",
            "prepare_active_capture_project",
            "register_active_project_workspace",
            "export_capture_symbol_catalog",
            "scan_asset_symbols",
            "match_capture_assets",
            "resolve_capture_symbols",
            "submit_symbol_match",
            "export_symbol_alias_table",
            "apply_symbol_alias_table",
        )
    },
    **{
        name: frozenset({"replay"})
        for name in (
            "apply_shader_workspace_candidate",
            "apply_shader_workspace_candidate_family",
            "apply_shader_workspace_shared_resource",
            "rollback_shader_workspace_override",
            "rollback_shader_workspace_override_family",
            "rollback_shader_workspace_shared_resource",
            "restore_current_shader",
        )
    },
    # These two calls have argument-dependent effects.  This mapping describes
    # their *potential* effects for planning/capability discovery; the guard
    # uses ``tool_call_effects`` for each concrete invocation.
    "classify_current_frame_passes": frozenset({"project", "ui"}),
    "apply_current_frame_pass_ai_proposals": frozenset({"project", "ui"}),
}


_PARAMETERIZED_EFFECT_TOOLS = frozenset(
    {
        "classify_current_frame_passes",
        "apply_current_frame_pass_ai_proposals",
    }
)


def tool_effects(name: str) -> frozenset[str]:
    """Return the workflow effect class for a discovered Skill tool.

    Unknown discovered tools are not treated as forbidden.  They start in the
    reversible workspace class, while names that clearly express replay apply
    or rollback semantics receive the stronger replay contract automatically.
    """

    tool = str(name or "").strip()
    if not tool or tool in READ_ONLY_TOOLS:
        return frozenset()
    classified = TOOL_EFFECTS.get(tool)
    if classified is not None:
        return classified
    lowered = tool.casefold()
    if any(token in lowered for token in ("apply_shader", "replace_shader", "restore_shader", "rollback_shader")):
        return frozenset({"replay"})
    if any(token in lowered for token in ("project", "library", "catalog", "symbol_alias")):
        return frozenset({"project"})
    if any(token in lowered for token in ("publish", "rename_event", "organize_event", "classify_pass")):
        return frozenset({"ui"})
    return frozenset({"workspace"})


def tool_has_parameterized_effects(name: str) -> bool:
    """Return whether a tool's actual effects depend on call arguments."""

    return str(name or "").strip() in _PARAMETERIZED_EFFECT_TOOLS


def _effect_flag(
    arguments: dict[str, Any], snake_name: str, camel_name: str, default: bool
) -> bool:
    """Read one strict boolean conservatively for preflight classification.

    The Host schema ultimately rejects non-booleans.  Treat an invalid value as
    the effectful default here so malformed arguments can never under-declare a
    UI/project effect before they reach that schema.
    """

    value = arguments.get(snake_name, arguments.get(camel_name, default))
    return value if isinstance(value, bool) else default


def tool_call_effects(name: str, arguments: dict[str, Any]) -> frozenset[str]:
    """Return observable workflow effects for one concrete tool invocation."""

    tool = str(name or "").strip()
    if tool == "classify_current_frame_passes":
        # The current Host always stages/imports a capture catalog through
        # prepare_active_capture_project before classification, even when
        # persist_project is false.  That project write must never be treated as
        # observation-only.  UI publication remains parameter-dependent.
        effects = {"project"}
        if _effect_flag(arguments, "publish_to_ui", "publishToUi", True):
            effects.add("ui")
        return frozenset(effects)
    if tool == "apply_current_frame_pass_ai_proposals":
        effects: set[str] = set()
        if _effect_flag(arguments, "publish_to_ui", "publishToUi", True):
            effects.add("ui")
        if _effect_flag(arguments, "persist_project", "persistProject", True):
            effects.add("project")
        return frozenset(effects)
    return tool_effects(tool)


_SHADER_APPLY_TOOLS = frozenset(
    {
        "apply_shader_workspace_candidate",
        "apply_shader_workspace_candidate_family",
        "apply_shader_workspace_shared_resource",
    }
)
_SHADER_ROLLBACK_TOOLS = frozenset(
    {
        "rollback_shader_workspace_override",
        "rollback_shader_workspace_override_family",
        "rollback_shader_workspace_shared_resource",
        "restore_current_shader",
    }
)


def _tool_result_object(result: Any) -> dict[str, Any] | None:
    """Decode a bounded JSON object from direct or MCP tool results."""

    candidates: list[Any] = [result]
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, (list, tuple)):
        candidates = [*content, *candidates]
    elif content is not None:
        candidates.insert(0, content)
    for candidate in candidates:
        value: Any = candidate
        if not isinstance(value, (str, dict)):
            value = getattr(candidate, "text", None)
        if isinstance(value, str):
            if len(value) > 16 * 1024 * 1024:
                continue
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _nested_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    pending = [value]
    visited = 0
    while pending and visited < 10000:
        current = pending.pop()
        visited += 1
        if isinstance(current, dict):
            if key in current:
                found.append(current[key])
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current[:10000])
    return found


def _workspace_key(arguments: dict[str, Any]) -> str:
    raw = arguments.get("workspace_path", arguments.get("workspacePath", ""))
    value = str(raw or "").strip()
    if not value or "\0" in value:
        return ""
    return os.path.normcase(os.path.abspath(value))


@dataclass(slots=True)
class WorkflowGuard:
    """Task-local outcome, ordering, postcondition, and rollback contract.

    This guard intentionally does not encode a UI/button permission matrix.
    The supervisor requests semantic effects (workspace/project/ui/replay), the
    active Skill may choose any discovered tool that fulfils those effects, and
    the Host admits completion only after observable postconditions hold.
    """

    requested_effects: frozenset[str]
    completed: list[str] = field(default_factory=list)
    completed_effects: set[str] = field(default_factory=set)
    validated_workspaces: set[str] = field(default_factory=set)
    fixed_classifications: set[str] = field(default_factory=set)
    mutation_uncertain: bool = False
    replay_postcondition_observed: bool = False

    def __init__(self, requested_effects: set[str] | frozenset[str] = frozenset()):
        effects = frozenset(str(value).strip() for value in requested_effects)
        unknown = effects - WORKFLOW_EFFECTS
        if unknown:
            raise PolicyError(
                "workflow requested unknown effect classes: "
                + ", ".join(sorted(unknown))
            )
        if "replay" in effects:
            effects = effects | {"workspace"}
        self.requested_effects = effects
        self.completed = []
        self.completed_effects = set()
        self.validated_workspaces = set()
        self.fixed_classifications = set()
        self.mutation_uncertain = False
        self.replay_postcondition_observed = False

    def check(self, name: str, arguments: dict[str, Any]) -> None:
        effects = tool_call_effects(name, arguments)
        if effects and not effects.issubset(self.requested_effects):
            raise PolicyError(
                f"tool '{name}' is outside the task's requested workflow outcome "
                f"({', '.join(sorted(effects))})"
            )
        if name in _SHADER_APPLY_TOOLS:
            workspace = _workspace_key(arguments)
            if not workspace or workspace not in self.validated_workspaces:
                raise PolicyError(
                    "shader replay apply requires a successful, same-workspace "
                    "validate_shader_workspace_candidate result first"
                )
        if name == "apply_current_frame_pass_ai_proposals":
            classification_id = str(
                arguments.get("classification_id", arguments.get("classificationId", ""))
                or ""
            ).strip()
            if not classification_id or classification_id not in self.fixed_classifications:
                raise PolicyError(
                    "AI pass proposals require the exact classificationId returned by this "
                    "task's classify_current_frame_passes call"
                )

    def record_failure(self, name: str) -> None:
        if name in _SHADER_APPLY_TOOLS:
            self.mutation_uncertain = True

    def record_result(
        self, name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        payload = _tool_result_object(result)
        call_effects = tool_call_effects(name, arguments)
        if call_effects and payload is None:
            raise PolicyError(
                f"effectful tool '{name}' returned no machine-verifiable JSON receipt"
            )
        if payload is not None and _nested_values(payload, "error"):
            errors = [value for value in _nested_values(payload, "error") if value]
            if errors:
                self.record_failure(name)
                raise PolicyError(f"effectful tool '{name}' returned an error receipt")

        if name == "validate_shader_workspace_candidate":
            if True not in _nested_values(payload, "replayAbiValidated"):
                raise PolicyError(
                    "shader candidate validation did not prove replay ABI compatibility"
                )
            workspace = _workspace_key(arguments)
            if not workspace:
                raise PolicyError("shader validation receipt has no bound workspace path")
            self.validated_workspaces.add(workspace)
        elif name in _SHADER_APPLY_TOOLS:
            applied = _nested_values(payload, "applied")
            if True not in applied:
                self.mutation_uncertain = False
                raise PolicyError("shader apply receipt did not prove that replacement applied")
            if True not in _nested_values(payload, "replayAbiValidated"):
                self.mutation_uncertain = True
                raise PolicyError("shader apply receipt did not retain ABI validation evidence")
            self.mutation_uncertain = False
            self.replay_postcondition_observed = True
        elif name in _SHADER_ROLLBACK_TOOLS:
            restored = (
                _nested_values(payload, "restored")
                + _nested_values(payload, "rolledBack")
                + _nested_values(payload, "cleared")
            )
            if True not in restored:
                raise PolicyError("shader rollback receipt did not prove restoration")
            self.mutation_uncertain = False
            self.replay_postcondition_observed = True
        elif name == "classify_current_frame_passes":
            classification_ids = _nested_values(payload, "classificationId")
            classification_id = next(
                (str(value).strip() for value in classification_ids if str(value).strip()),
                "",
            )
            if not classification_id:
                raise PolicyError("pass classification returned no stable classificationId")
            publish = _effect_flag(arguments, "publish_to_ui", "publishToUi", True)
            persist = _effect_flag(arguments, "persist_project", "persistProject", True)
            publish_requests = _nested_values(payload, "publishRequested")
            persist_requests = _nested_values(payload, "persistRequested")
            if not publish_requests or any(value is not publish for value in publish_requests):
                raise PolicyError("pass classification receipt changed publish_to_ui")
            if not persist_requests or any(value is not persist for value in persist_requests):
                raise PolicyError("pass classification receipt changed persist_project")
            if publish and True not in _nested_values(payload, "published"):
                raise PolicyError(
                    "pass classification receipt did not prove publication to the semantic pass panel"
                )
            self.fixed_classifications.add(classification_id)
        elif name == "apply_current_frame_pass_ai_proposals":
            expected = str(
                arguments.get("classification_id", arguments.get("classificationId", ""))
                or ""
            ).strip()
            returned = {
                str(value).strip()
                for value in _nested_values(payload, "classificationId")
                if str(value).strip()
            }
            if expected not in returned:
                raise PolicyError(
                    "pass proposal receipt does not match the requested classificationId"
                )
            publish = _effect_flag(arguments, "publish_to_ui", "publishToUi", True)
            persist = _effect_flag(arguments, "persist_project", "persistProject", True)
            publish_requests = _nested_values(payload, "publishRequested")
            persist_requests = _nested_values(payload, "persistRequested")
            if not publish_requests or any(value is not publish for value in publish_requests):
                raise PolicyError("pass proposal receipt changed publish_to_ui")
            if not persist_requests or any(value is not persist for value in persist_requests):
                raise PolicyError("pass proposal receipt changed persist_project")
            if publish and True not in _nested_values(payload, "published"):
                raise PolicyError(
                    "pass proposal receipt did not prove publication to the semantic pass panel"
                )

        self.completed.append(name)
        observed_effects = set(call_effects)
        if (
            name == "apply_current_frame_pass_ai_proposals"
            and "project" in observed_effects
            and True not in _nested_values(payload, "persisted")
        ):
            # A pending project may truthfully report that persistence was
            # requested but unavailable.  Keep the tool result as evidence, but
            # do not claim the project outcome completed.
            observed_effects.discard("project")
        self.completed_effects.update(observed_effects)

    def validate_completion(self) -> None:
        if self.mutation_uncertain:
            raise EvidenceValidationError(
                "a replay mutation ended without a proven postcondition; run the matching "
                "rollback/restore flow before completing the specialist"
            )
        missing = self.requested_effects - self.completed_effects
        if set(self.completed) & _SHADER_ROLLBACK_TOOLS:
            missing = missing - {"workspace"}
        if missing:
            raise EvidenceValidationError(
                "specialist did not complete requested workflow effects: "
                + ", ".join(sorted(missing))
            )
        if "replay" in self.requested_effects and not self.replay_postcondition_observed:
            raise EvidenceValidationError(
                "replay workflow ended without an applied or restored postcondition receipt"
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

    General chat/analysis records the selected event, shader, resource, or
    texel as an evidence anchor, but does not turn that incidental selection
    into a universal tool permission boundary. This lets an event specialist
    inspect a bounded pass/frame while the capture snapshot remains frozen.
    Native button intents additionally require and enforce their exact target
    shape.
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

    @property
    def exact_target(self) -> bool:
        """Whether this native intent declares a Host-known exact target schema."""

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
        # A newer native UI may introduce an intent before AgentHost knows its
        # exact target schema. Keep the immutable capture/session snapshot and
        # semantic workflow guard instead of converting that version skew into
        # a permanent permission dead end. Exact binding starts once the schema
        # is explicitly implemented above.
        return

    def validate_live_context(self, context: dict[str, Any]) -> None:
        if not self.capture_session_id:
            return
        capture = _mapping(context.get("capture"))
        if capture.get("loaded") is not True:
            raise PolicyError("the pinned qrenderdoc capture is no longer loaded")
        observed_session = str(capture.get("captureSessionId", "") or "").strip().lower()
        if observed_session != self.capture_session_id:
            raise PolicyError("the pinned qrenderdoc capture session changed during the tool call")
        # ``captureSessionId`` is the native high-entropy identity for this
        # exact load lifecycle. The user is free to navigate to another EID,
        # shader or pixel while a background task runs: target identity is
        # carried by explicit frozen tool arguments, never by mutable UI state.
        # ``captureId`` is catalog-derived and may legitimately change while
        # the same capture session remains loaded, so it is evidence rather
        # than a live-lifecycle lock.

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
        if not isinstance(arguments, dict):
            raise PolicyError("RenderDoc tool arguments must be an object")

        exact_target = self.exact_target

        event_tools = {
            "inspect_event",
            "analyze_pixel",
            "inspect_shader",
            "get_draw_call_details",
            "get_pipeline_state",
            "get_shader_info",
            "reverse_shader",
            "get_bound_textures",
            "pixel_history",
            "inspect_pixel_object",
            "debug_pixel",
            "prepare_shader_workspace",
        }
        resource_tools = {
            "analyze_pixel",
            "get_texture_info",
            "get_texture_minmax",
            "pixel_history",
            "inspect_pixel_object",
        }
        if exact_target and tool in event_tools and self.event_id is not None:
            actual = _positive_int(_first(arguments, "event_id", "eventId"))
            if actual != self.event_id:
                raise PolicyError(
                    f"tool '{tool}' must use frozen event {self.event_id}"
                )
        if self.capture_session_id:
            supplied_session = str(
                _first(
                    arguments,
                    "expected_capture_session_id",
                    "expectedCaptureSessionId",
                    "capture_session_id",
                    "captureSessionId",
                )
                or ""
            ).strip().casefold()
            if supplied_session and supplied_session != self.capture_session_id:
                raise PolicyError(
                    f"tool '{tool}' must use the frozen capture session"
                )
        if self.capture_id:
            supplied_capture = str(
                _first(
                    arguments,
                    "expected_capture_id",
                    "expectedCaptureId",
                    "capture_id",
                    "captureId",
                )
                or ""
            ).strip().casefold()
            if supplied_capture and supplied_capture != self.capture_id:
                raise PolicyError(f"tool '{tool}' must use the frozen capture id")
        if exact_target and tool in resource_tools and self.resource_id is not None:
            actual = _positive_int(
                _first(arguments, "resource_id", "resourceId", "texture_id", "textureId")
            )
            if actual != self.resource_id:
                raise PolicyError(
                    f"tool '{tool}' must use frozen resource {self.resource_id}"
                )
        if exact_target and tool in {"analyze_pixel", "pixel_history", "inspect_pixel_object", "debug_pixel"}:
            for name, expected in (("x", self.x), ("y", self.y)):
                if expected is not None and _nonnegative_int(arguments.get(name)) != expected:
                    raise PolicyError(
                        f"tool '{tool}' must use frozen pixel {name}={expected}"
                    )
        if exact_target and tool in {"analyze_pixel", "inspect_pixel_object"}:
            for name, expected in (
                ("mip", self.mip),
                ("slice", self.slice),
                ("sample", self.sample),
            ):
                if expected is not None and _nonnegative_int(arguments.get(name)) != expected:
                    raise PolicyError(
                        f"tool '{tool}' must use frozen subresource {name}={expected}"
                    )
        if exact_target and tool == "debug_pixel" and self.sample is not None:
            if _nonnegative_int(arguments.get("sample")) != self.sample:
                raise PolicyError(
                    f"tool '{tool}' must use frozen sample {self.sample}"
                )
        if exact_target and tool in {
            "get_shader_info",
            "reverse_shader",
            "inspect_shader",
            "get_bound_textures",
            "get_shader_toolchain_capabilities",
            "prepare_shader_workspace",
        } and self.stage:
            actual = _stage(arguments.get("stage", ""))
            if actual != self.stage:
                raise PolicyError(f"tool '{tool}' must use frozen shader stage {self.stage}")


__all__ = [
    "EFFECTFUL_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_EFFECTS",
    "WORKFLOW_EFFECTS",
    "ToolCallScope",
    "WorkflowGuard",
    "tool_call_effects",
    "tool_effects",
    "tool_has_parameterized_effects",
]
