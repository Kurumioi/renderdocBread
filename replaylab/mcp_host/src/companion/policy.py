"""Programmatic tool exposure and mutation gates for the companion.

Prompt text is not an authorisation boundary.  Tools are exposed from explicit
sets and every call is checked again immediately before dispatch to MCP.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Tools in this set do not intentionally change capture, replay or local files.
# Context/intent queue operations are omitted because the companion owns them;
# the model never needs to claim or complete its own work.
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
        # Raw texture/buffer downloads are intentionally not model-facing.
        # Their size can be capture-controlled; use bounded metadata/debug or
        # an explicitly authorised export workflow instead.
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


# These operations have effects.  A chat turn receives none of them unless the
# user supplies an exact /allow list.  Some are invoked directly by a bounded
# RenderDoc UI intent, under the narrower policy in engine.py.
EFFECTFUL_TOOLS = frozenset(
    {
        "open_capture",
        "launch_renderdoc",
        # list_captures accepts an arbitrary directory, while
        # identify_drawcalls writes annotated images/output_dir.
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
        # Validation persists an ABI report and workspace manifest/history.
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
        # The smoke operation executes tools and writes a new output directory.
        "run_shader_toolchain_round_trip_smoke",
        "create_replay_project",
        "import_replay_project_capture",
        "register_replay_project_workspace",
        "register_replay_project_alias_table",
        "save_replay_project_material_knowledge",
        "seed_shader_workspace_from_replay_project",
    }
)

SHADER_APPLY_TOOLS = frozenset(
    {
        "apply_shader_workspace_candidate",
        "apply_shader_workspace_candidate_family",
        "apply_shader_workspace_shared_resource",
    }
)

INTERNAL_TOOLS = frozenset(
    {
        "get_current_renderdoc_context",
        "claim_renderdoc_ui_intents",
        "complete_renderdoc_ui_intent",
        "renew_renderdoc_ui_intent",
        # The model selects only a program-listed candidate index. Companion
        # itself supplies the source/target workspace and exact identity hashes.
        "try_shader_workspace_recovery_candidate",
    }
)

EXPLAIN_INTENT_TOOLS = frozenset(
    {
        "get_draw_call_details",
        "get_pipeline_state",
        "get_shader_info",
        "get_bound_textures",
        "find_draws_by_resource",
        "get_debug_messages",
    }
)

PIXEL_INTENT_TOOLS = frozenset(
    {
        "inspect_pixel_object",
        "pixel_history",
        "get_texture_info",
        "get_draw_call_details",
        "get_pipeline_state",
        "find_draws_by_resource",
    }
)

REVERSE_INTENT_MODEL_TOOLS = frozenset(
    {
        "get_draw_call_details",
        "get_pipeline_state",
        "get_shader_info",
        "get_bound_textures",
        "list_shader_workspace_family_uses",
    }
)


class PolicyError(RuntimeError):
    """A requested operation is not authorised by the current turn."""


def _normalise_workspace(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(str(Path(text).expanduser().resolve()))


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


_WORKSPACE_PATH_TOOLS = frozenset(
    {
        "get_shader_workspace_status",
        "list_shader_workspace_family_uses",
        "create_shader_workspace",
        "prepare_shader_workspace",
        "try_shader_workspace_recovery_candidate",
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
        "start_shader_workspace_watch",
    }
)


_PROJECT_TOOL_PATH_ARGUMENTS = {
    "create_replay_project": ("project_path",),
    "get_replay_project_status": ("project_path",),
    "import_replay_project_capture": ("project_path", "catalog_path"),
    "register_replay_project_workspace": ("project_path", "workspace_path"),
    "register_replay_project_alias_table": ("project_path", "alias_table_path"),
    "save_replay_project_material_knowledge": ("project_path",),
    "get_replay_project_reuse_plan": ("project_path",),
    "seed_shader_workspace_from_replay_project": (
        "project_path",
        "workspace_path",
    ),
}


@dataclass
class TurnPolicy:
    """Exact tools and same-turn validation evidence for one model turn."""

    exposed_tools: frozenset[str]
    authorised_effects: frozenset[str] = frozenset()
    readable_roots: tuple[str, ...] = ()
    project_roots: tuple[str, ...] = ()
    _validated_workspaces: set[str] = field(default_factory=set)

    @classmethod
    def for_chat(
        cls,
        authorised_effects: Iterable[str] = (),
        readable_roots: Iterable[str] = (),
        project_roots: Iterable[str] = (),
    ) -> "TurnPolicy":
        requested = frozenset(str(name).strip() for name in authorised_effects)
        unknown = requested - EFFECTFUL_TOOLS
        if unknown:
            raise PolicyError(
                "unknown or non-effectful /allow tool(s): %s"
                % ", ".join(sorted(unknown))
            )
        return cls(
            exposed_tools=READ_ONLY_TOOLS | requested,
            authorised_effects=requested,
            readable_roots=tuple(
                root
                for root in (_normalise_workspace(value) for value in readable_roots)
                if root
            ),
            project_roots=tuple(
                root
                for root in (_normalise_workspace(value) for value in project_roots)
                if root
            ),
        )

    @classmethod
    def read_only(
        cls,
        tools: Iterable[str],
        readable_roots: Iterable[str] = (),
    ) -> "TurnPolicy":
        requested = frozenset(tools)
        if requested - READ_ONLY_TOOLS:
            raise PolicyError("read-only policy contains an effectful tool")
        return cls(
            exposed_tools=requested,
            readable_roots=tuple(
                root
                for root in (_normalise_workspace(value) for value in readable_roots)
                if root
            ),
        )

    def check(self, name: str, arguments: dict[str, Any]) -> None:
        if name not in self.exposed_tools:
            raise PolicyError("tool is not exposed in this turn: %s" % name)
        if name in EFFECTFUL_TOOLS and name not in self.authorised_effects:
            raise PolicyError("tool has no explicit user authorisation: %s" % name)
        if name in _WORKSPACE_PATH_TOOLS:
            workspace = _normalise_workspace(arguments.get("workspace_path"))
            if not workspace or not any(
                workspace == root or workspace.startswith(root + os.sep)
                for root in self.readable_roots
            ):
                raise PolicyError(
                    "%s workspace_path must stay inside the companion-configured "
                    "workspace root" % name
                )
        if name == "try_shader_workspace_recovery_candidate":
            source_workspace = _normalise_workspace(
                arguments.get("source_workspace_path")
            )
            target_workspace = _normalise_workspace(arguments.get("workspace_path"))
            if not source_workspace or not any(
                source_workspace == root
                or source_workspace.startswith(root + os.sep)
                for root in self.readable_roots
            ):
                raise PolicyError(
                    "try_shader_workspace_recovery_candidate source_workspace_path "
                    "must stay inside the companion-configured workspace root"
                )
            if source_workspace == target_workspace:
                raise PolicyError("shader recovery requires a distinct new workspace")
            if target_workspace.startswith(source_workspace + os.sep):
                raise PolicyError(
                    "shader recovery target must stay outside the failed source "
                    "workspace"
                )
        for field_name in _PROJECT_TOOL_PATH_ARGUMENTS.get(name, ()):
            path = _normalise_workspace(arguments.get(field_name))
            allowed_roots = (
                (self.project_roots or self.readable_roots)
                if field_name == "project_path"
                else self.readable_roots + self.project_roots
            )
            if not path or not any(
                path == root or path.startswith(root + os.sep)
                for root in allowed_roots
            ):
                raise PolicyError(
                    "%s %s must stay inside a companion-configured workspace/project root"
                    % (name, field_name)
                )
        if name in {
            "plan_shader_toolchain_route",
            "plan_shader_recovery",
            "run_shader_toolchain_round_trip_smoke",
        }:
            paths = [arguments.get("shader_path")]
            if name == "run_shader_toolchain_round_trip_smoke":
                paths.append(arguments.get("output_directory"))
            for value in paths:
                path = _normalise_workspace(value)
                if not path or not any(
                    path == root or path.startswith(root + os.sep)
                    for root in self.readable_roots
                ):
                    raise PolicyError(
                        "%s paths must stay inside a companion-configured workspace root"
                        % name
                    )
        if name in SHADER_APPLY_TOOLS:
            workspace = _normalise_workspace(arguments.get("workspace_path"))
            if not workspace or workspace not in self._validated_workspaces:
                raise PolicyError(
                    "shader apply requires a successful, fresh, same-turn "
                    "validate_shader_workspace_candidate call"
                )

    def record_result(
        self, name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        if name != "validate_shader_workspace_candidate":
            return
        decoded = _decode_json(result)
        validation = decoded.get("validation") if isinstance(decoded, dict) else None
        if (
            isinstance(decoded, dict)
            and "error" not in decoded
            and isinstance(validation, dict)
            and validation.get("replayAbiValidated") is True
        ):
            workspace = _normalise_workspace(arguments.get("workspace_path"))
            if workspace:
                self._validated_workspaces.add(workspace)


def parse_authorized_message(line: str) -> tuple[str, frozenset[str]]:
    """Parse ``/allow exact_tool[,tool] | message`` without semantic guessing."""

    value = str(line or "").strip()
    if not value.startswith("/allow"):
        return value, frozenset()
    payload = value[len("/allow") :].strip()
    if "|" not in payload:
        raise PolicyError(
            "use /allow tool_name[,tool_name] | your request (authorisation "
            "lasts for this turn only)"
        )
    names_text, message = payload.split("|", 1)
    names = frozenset(name.strip() for name in names_text.split(",") if name.strip())
    if not names or not message.strip():
        raise PolicyError("/allow requires at least one exact tool and a message")
    unknown = names - EFFECTFUL_TOOLS
    if unknown:
        raise PolicyError(
            "unknown or non-effectful /allow tool(s): %s"
            % ", ".join(sorted(unknown))
        )
    return message.strip(), names
