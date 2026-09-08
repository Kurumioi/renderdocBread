"""Deterministic effect classes for the first-party ReplayLab tool catalog.

The legacy workflow state machine intentionally does not live here. This
module answers only whether a tool is read-only and which connection-level
effect classes an admitted call may exercise.
"""

from __future__ import annotations

from typing import Any, Iterable

from .errors import PolicyError


READ_ONLY_TOOLS = frozenset(
    {
        "ping",
        "report_task_result",
        "get_capture_status",
        "get_current_renderdoc_context",
        "read_shader_workspace_file",
        "get_shader_workspace_outline",
        "inspect_event",
        "analyze_current_frame_flow",
        "analyze_pixel",
        "inspect_shader",
        "debug_vulkan_bindings",
        "analyze_lighting",
        "get_draw_calls",
        "get_frame_summary",
        "get_draw_call_details",
        "get_pipeline_state",
        "get_bound_textures",
        "find_draws_by_shader",
        "find_draws_by_texture",
        "find_draws_by_resource",
        "get_textures",
        "get_buffers",
        "get_resources",
        "get_texture_info",
        "get_texture_data",
        "get_buffer_contents",
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
        "poll_shader_workspace_live_edit",
        "list_shader_workspace_live_edits",
        "list_shader_workspace_family_uses",
        "get_shader_toolchain_capabilities",
        "plan_shader_toolchain_route",
        "plan_shader_recovery",
        "inspect_asset_shader_recovery",
        "list_material_sessions",
        "get_material_session_status",
        "poll_shader_workspace_watch",
        "get_replay_project_status",
        "get_replay_project_reuse_plan",
        "get_active_replay_project_status",
        "get_active_capture_catalog_binding_status",
        "get_active_project_future_frame_plan",
        "get_resource_export_capabilities",
        "get_resource_export_status",
        "inspect_resource_export",
        "validate_resource_export",
        "query_active_shader_asset_evidence",
        "get_unpacked_asset_index_capabilities",
        "get_active_unpacked_asset_index_status",
        "get_unpacked_asset_provider_capabilities",
        "get_unpacked_asset_provider_decisions",
        "get_unpacked_asset_job_status",
        "query_active_unpacked_assets",
        "query_unpacked_asset_inventory_assets",
        "resolve_active_unpacked_asset",
        "fingerprint_event_outputs",
        "get_post_vs_data",
    }
)


EFFECTFUL_TOOLS = frozenset(
    {
        "open_capture",
        "launch_renderdoc",
        "list_captures",
        "identify_drawcalls",
        "capture_event_texture_evidence",
        "render_current_frame_flow_diagram",
        "classify_current_frame_passes",
        "export_capture_symbol_catalog",
        "scan_asset_symbols",
        "match_capture_assets",
        "resolve_capture_symbols",
        "submit_symbol_match",
        "export_symbol_alias_table",
        "apply_symbol_alias_table",
        "rollback_symbol_alias_table",
        "create_shader_workspace",
        "prepare_shader_workspace",
        "try_shader_workspace_recovery_candidate",
        "get_current_shader_representation",
        "decompile_shader_workspace",
        "get_shader_workspace_representation",
        "promote_shader_workspace_explanation",
        "compile_shader_workspace",
        "migrate_shader_workspace_toolchain",
        "begin_shader_workspace_organization",
        "write_shader_workspace_organization_file",
        "rename_shader_workspace_symbols",
        "restructure_shader_workspace_source",
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
        "update_material_parameters",
        "rollback_material_session",
        "clear_all_material_sessions",
        "start_shader_workspace_watch",
        "stop_shader_workspace_watch",
        "start_shader_workspace_live_edit",
        "migrate_shader_workspace_live_edit_authorization",
        "stop_shader_workspace_live_edit",
        "run_shader_toolchain_round_trip_smoke",
        "create_replay_project",
        "ensure_active_replay_project",
        "activate_active_replay_project",
        "configure_active_replay_project",
        "configure_active_project_asset_root",
        "import_replay_project_capture",
        "register_replay_project_workspace",
        "register_replay_project_alias_table",
        "save_replay_project_material_knowledge",
        "seed_shader_workspace_from_replay_project",
        "seed_active_shader_workspace",
        "prepare_active_capture_project",
        "get_active_shader_workspace_path",
        "register_active_project_workspace",
        "register_active_project_shader_replacement",
        "register_active_project_material_session",
        "restore_current_shader",
        "apply_current_frame_pass_ai_proposals",
        "prepare_resource_export",
        "finalize_resource_export",
        "run_resource_export_target_validation",
        "index_active_unpacked_assets",
        "start_unpacked_asset_report",
        "start_unpacked_asset_blender_export",
        "continue_unpacked_asset_blender_export",
        # AI-authored ABI repair of the workspace source (compile + replay
        # reflection, restored on failure) and the asset-matching memory.
        "repair_shader_workspace_source_abi",
        "retrieve_shader_asset_candidates",
        "submit_shader_asset_resolution",
        "forget_shader_asset_resolution",
        # Applies the user's answer to an exhausted organization review budget.
        "decide_shader_organization_review_budget",
    }
)


WORKFLOW_EFFECTS = frozenset({"workspace", "project", "ui", "replay"})


TOOL_EFFECTS: dict[str, frozenset[str]] = {
    **{
        name: frozenset({"workspace"})
        for name in (
            "create_shader_workspace",
            "prepare_shader_workspace",
            "try_shader_workspace_recovery_candidate",
            "migrate_shader_workspace_toolchain",
            "apply_shader_workspace_candidate",
            "get_current_shader_representation",
            "decompile_shader_workspace",
            "get_shader_workspace_representation",
            "promote_shader_workspace_explanation",
            "compile_shader_workspace",
            "begin_shader_workspace_organization",
            "write_shader_workspace_organization_file",
            "rename_shader_workspace_symbols",
            "restructure_shader_workspace_source",
            "validate_shader_workspace_candidate",
            "start_shader_workspace_watch",
            "stop_shader_workspace_watch",
            "run_shader_toolchain_round_trip_smoke",
            "open_capture",
            "launch_renderdoc",
            "list_captures",
            "identify_drawcalls",
            "capture_event_texture_evidence",
            "render_current_frame_flow_diagram",
            "prepare_material_session",
            "update_material_parameter",
            "rollback_material_session",
            "clear_all_material_sessions",
            "prepare_resource_export",
            "finalize_resource_export",
            "run_resource_export_target_validation",
            "start_unpacked_asset_report",
            "start_unpacked_asset_blender_export",
            "continue_unpacked_asset_blender_export",
        )
    },
    **{
        name: frozenset({"project"})
        for name in (
            "create_replay_project",
            "ensure_active_replay_project",
            "activate_active_replay_project",
            "configure_active_replay_project",
            "configure_active_project_asset_root",
            "import_replay_project_capture",
            "register_replay_project_workspace",
            "register_replay_project_alias_table",
            "save_replay_project_material_knowledge",
            "seed_shader_workspace_from_replay_project",
            "register_active_project_shader_replacement",
            "register_active_project_material_session",
            "export_capture_symbol_catalog",
            "scan_asset_symbols",
            "match_capture_assets",
            "resolve_capture_symbols",
            "submit_symbol_match",
            "export_symbol_alias_table",
            "apply_symbol_alias_table",
            "index_active_unpacked_assets",
            "repair_shader_workspace_source_abi",
            # first use may build/hydrate the project's asset catalog
            "retrieve_shader_asset_candidates",
            "decide_shader_organization_review_budget",
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
    "classify_current_frame_passes": frozenset({"project", "ui"}),
    # Review may temporarily replace candidates to measure output; registration
    # immediately opens the editor dock. Keep these effects visible to routing.
    "review_shader_workspace_organization": frozenset({"workspace", "replay"}),
    "register_active_project_workspace": frozenset({"project", "ui"}),
    "apply_current_frame_pass_ai_proposals": frozenset({"project", "ui"}),
    "prepare_active_capture_project": frozenset({"project", "ui"}),
    "get_active_shader_workspace_path": frozenset({"project", "replay"}),
    "apply_symbol_alias_table": frozenset({"project", "ui"}),
    "rollback_symbol_alias_table": frozenset({"ui"}),
    "update_material_parameter": frozenset({"workspace", "replay"}),
    "update_material_parameters": frozenset({"workspace", "replay"}),
    "rollback_material_session": frozenset({"workspace", "replay"}),
    "clear_all_material_sessions": frozenset({"workspace", "replay"}),
    "seed_active_shader_workspace": frozenset({"workspace"}),
    "submit_shader_asset_resolution": frozenset({"project"}),
    "forget_shader_asset_resolution": frozenset({"project"}),
    "start_shader_workspace_live_edit": frozenset({"workspace", "replay"}),
    "migrate_shader_workspace_live_edit_authorization": frozenset(
        {"workspace", "replay"}
    ),
    "stop_shader_workspace_live_edit": frozenset({"workspace", "replay"}),
}


_PARAMETERIZED_EFFECT_TOOLS = frozenset(
    {"classify_current_frame_passes", "apply_current_frame_pass_ai_proposals"}
)


def tool_effects(name: str) -> frozenset[str]:
    """Return a conservative effect class for one discovered tool."""

    tool = str(name or "").strip()
    if not tool or tool in READ_ONLY_TOOLS:
        return frozenset()
    return TOOL_EFFECTS.get(tool, frozenset({"workspace"}))


def validate_registered_tool_effect_manifest(names: Iterable[str]) -> frozenset[str]:
    """Fail closed when a first-party tool lacks an explicit declaration."""

    registered = frozenset(str(name or "").strip() for name in names)
    if "" in registered:
        raise PolicyError("registered first-party tool manifest contains an empty id")
    explicit_effects = frozenset(TOOL_EFFECTS)
    declared = READ_ONLY_TOOLS | EFFECTFUL_TOOLS | explicit_effects
    conflicts = READ_ONLY_TOOLS & (EFFECTFUL_TOOLS | explicit_effects)
    missing_effects = registered - READ_ONLY_TOOLS - explicit_effects
    stale_declarations = declared - registered
    missing_markers = explicit_effects - EFFECTFUL_TOOLS
    missing_mappings = EFFECTFUL_TOOLS - explicit_effects
    if conflicts:
        raise PolicyError(
            "registered first-party tools have conflicting read-only/effectful declarations: "
            + ", ".join(sorted(conflicts))
        )
    if missing_effects:
        raise PolicyError(
            "registered first-party tools lack explicit effect declarations: "
            + ", ".join(sorted(missing_effects))
        )
    if stale_declarations:
        raise PolicyError(
            "effect manifest declares tools not registered by the first-party Host: "
            + ", ".join(sorted(stale_declarations))
        )
    if missing_markers:
        raise PolicyError(
            "registered first-party tools have effect mappings but no effectful declaration: "
            + ", ".join(sorted(missing_markers))
        )
    if missing_mappings:
        raise PolicyError(
            "registered first-party tools are marked effectful without effect mappings: "
            + ", ".join(sorted(missing_mappings))
        )
    return registered


def tool_effects_are_host_known(name: str) -> bool:
    tool = str(name or "").strip()
    return bool(tool_effects(tool)) and (
        tool in EFFECTFUL_TOOLS or tool in TOOL_EFFECTS
    )


def tool_has_parameterized_effects(name: str) -> bool:
    return str(name or "").strip() in _PARAMETERIZED_EFFECT_TOOLS


def _effect_flag(
    arguments: dict[str, Any], snake_name: str, camel_name: str, default: bool
) -> bool:
    value = arguments.get(snake_name, arguments.get(camel_name, default))
    return value if isinstance(value, bool) else default


def tool_call_effects(name: str, arguments: dict[str, Any]) -> frozenset[str]:
    tool = str(name or "").strip()
    if tool in _PARAMETERIZED_EFFECT_TOOLS:
        effects: set[str] = set()
        if _effect_flag(arguments, "publish_to_ui", "publishToUi", True):
            effects.add("ui")
        if _effect_flag(arguments, "persist_project", "persistProject", False):
            effects.add("project")
        return frozenset(effects)
    return tool_effects(tool)


__all__ = [
    "EFFECTFUL_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_EFFECTS",
    "WORKFLOW_EFFECTS",
    "tool_call_effects",
    "tool_effects",
    "tool_effects_are_host_known",
    "tool_has_parameterized_effects",
    "validate_registered_tool_effect_manifest",
]
