from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from replaylab_agent.agent_context import child_tool_grant
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.cli import default_skill_root
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.policy import READ_ONLY_TOOLS
from replaylab_agent.skills import INTENT_SKILLS, SkillCatalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
MCP_SERVER = REPOSITORY_ROOT / "replaylab" / "mcp_host" / "src" / "server.py"
PACKAGING_SCRIPT = (
    REPOSITORY_ROOT / "replaylab" / "packaging" / "Build-ReplayLabRuntime.ps1"
)


def _write_test_skill(root: Path, name: str, instructions: str = "Use evidence.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test procedure for {name}.\n"
        "---\n\n"
        f"{instructions}\n",
        encoding="utf-8",
    )
    return path


def _declared_mcp_tools() -> set[str]:
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                names.add(node.name)
                break
    return names


def test_strict_skill_load_still_rejects_one_invalid_sibling(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken-skill\ndescription: [not valid yaml\n---\n"
        "UNTRUSTED-CONTENT-MUST-NOT-LEAK",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid skill frontmatter"):
        SkillCatalog([root]).load()


def test_runtime_skill_load_quarantines_bad_item_and_keeps_valid_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill", "Retain the exact receipt.")
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken-skill\ndescription: [not valid yaml\n---\n"
        "UNTRUSTED-CONTENT-MUST-NOT-LEAK",
        encoding="utf-8",
    )

    catalog = SkillCatalog([root]).load_runtime()

    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.get("valid-skill").instructions == "Retain the exact receipt."
    assert catalog.quarantined_skill_ids == {"broken-skill"}
    assert catalog.quarantine_count == 1
    assert [item.code for item in catalog.quarantine_diagnostics] == [
        "skill-definition-invalid"
    ]
    diagnostic_text = repr(
        [item.as_dict() for item in catalog.quarantine_diagnostics]
    )
    assert "UNTRUSTED-CONTENT-MUST-NOT-LEAK" not in diagnostic_text
    assert str(tmp_path) not in diagnostic_text
    with pytest.raises(
        ConfigurationError,
        match="packaged definition was quarantined",
    ):
        catalog.get("broken-skill")


def test_runtime_skill_load_quarantines_unreadable_item_without_exception_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    _write_test_skill(root, "unreadable-skill")
    original_read = SkillCatalog._read

    def controlled_read(path: Path):
        if path.parent.name == "unreadable-skill":
            raise OSError("SECRET-OS-DIAGNOSTIC")
        return original_read(path)

    monkeypatch.setattr(SkillCatalog, "_read", staticmethod(controlled_read))

    catalog = SkillCatalog([root]).load_runtime()

    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"unreadable-skill"}
    assert catalog.quarantine_diagnostics[0].code == "skill-file-unreadable"
    assert "SECRET-OS-DIAGNOSTIC" not in repr(catalog.quarantine_diagnostics)


def test_runtime_skill_load_quarantines_duplicate_identity_as_a_whole(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_test_skill(first, "duplicate-skill", "First body must not win.")
    _write_test_skill(second, "duplicate-skill", "Second body must not win.")

    with pytest.raises(ConfigurationError, match="duplicate skill name"):
        SkillCatalog([first, second]).load()

    catalog = SkillCatalog([first, second]).load_runtime()

    assert "duplicate-skill" not in catalog.skills
    assert catalog.quarantined_skill_ids == {"duplicate-skill"}
    assert [item.code for item in catalog.quarantine_diagnostics] == [
        "duplicate-skill-name"
    ]


def test_runtime_skill_quarantine_diagnostics_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for index in range(70):
        skill_dir = root / f"bad-skill-{index}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("invalid", encoding="utf-8")

    catalog = SkillCatalog([root]).load_runtime()

    assert catalog.quarantine_count == 70
    assert len(catalog.quarantine_diagnostics) == 64
    assert not catalog.skills

    summary = catalog.quarantine_summary()
    assert summary.as_dict()["schema"] == "replaylab-skill-quarantine-summary/v1"
    assert summary.as_dict()["quarantineCount"] == 70
    assert summary.as_dict()["truncated"] is True
    assert len(summary.as_dict()["diagnostics"]) == 64
    assert str(tmp_path) not in repr(summary.as_dict())


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            "  replaylab-category: Shader Workbench\n",
            "invalid replaylab-category",
        ),
        (
            "  replaylab-registry-version: zero\n",
            "invalid replaylab-registry-version",
        ),
        (
            "  replaylab-registry-virtual: true\n",
            "must not define replaylab-registry-virtual",
        ),
    ],
)
def test_strict_and_runtime_load_share_metadata_semantic_validation(
    tmp_path: Path,
    metadata: str,
    message: str,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    broken = root / "semantic-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\n"
        "name: semantic-skill\n"
        "description: Metadata validation test.\n"
        "metadata:\n"
        f"{metadata}"
        "---\n\n"
        "Use exact evidence.\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        SkillCatalog([root]).load()

    catalog = SkillCatalog([root]).load_runtime()
    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"semantic-skill"}
    assert catalog.quarantine_summary().as_dict()["diagnostics"] == [
        {
            "code": "skill-definition-invalid",
            "rootIndex": 0,
            "entryIndex": 0,
            "skillId": "semantic-skill",
        }
    ]


def test_skill_file_and_parsed_body_limits_are_strict_or_quarantined(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    oversized_body = "x" * (128 * 1024 + 1)
    _write_test_skill(root, "oversized-body", oversized_body)
    oversized_file = root / "oversized-file"
    oversized_file.mkdir(parents=True)
    (oversized_file / "SKILL.md").write_bytes(b"x" * (256 * 1024 + 1))

    with pytest.raises(ConfigurationError, match="128 Ki characters"):
        SkillCatalog([root]).load()

    catalog = SkillCatalog([root]).load_runtime()
    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"oversized-body", "oversized-file"}
    assert catalog.quarantine_count == 2
    assert {
        item.code for item in catalog.quarantine_diagnostics
    } == {"skill-definition-invalid"}


def test_strict_skill_load_rejects_oversized_main_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    oversized = root / "oversized-file"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_bytes(b"x" * (256 * 1024 + 1))

    with pytest.raises(ConfigurationError, match="skill file exceeds 256 KiB"):
        SkillCatalog([root]).load()


def _host_projected_catalog() -> SkillCatalog:
    """Load pure procedure bundles; live tools are queried separately."""

    return SkillCatalog([SKILL_ROOT]).load()


def test_repository_skills_are_pure_procedure_and_evidence_contracts() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert set(catalog.skills) == {
        "renderdoc-explain-event",
        "renderdoc-resource-export",
        "renderdoc-shader-workbench",
        "renderdoc-unpacked-asset-intelligence",
        # Mandatory standard loaded into every API/native execution role.
        "replaylab-core",
    }
    assert catalog.get("replaylab-core").metadata.get("replaylab-always") == "true"
    for skill in catalog.skills.values():
        assert skill.path.is_absolute()
        assert skill.instructions
        assert not hasattr(skill, "allowed_tools")
        assert not hasattr(skill, "read_only")
        assert "allowed-tools" not in skill.metadata
        assert "replaylab-read-only" not in skill.metadata
        assert "replaylab-min-successful-tools" not in skill.metadata
        assert skill.category != "other"
    catalog = _host_projected_catalog()
    shader = catalog.skills["renderdoc-shader-workbench"]
    event = catalog.skills["renderdoc-explain-event"]
    exporter = catalog.skills["renderdoc-resource-export"]
    unpacked = catalog.skills["renderdoc-unpacked-asset-intelligence"]
    for skill in (shader, event, exporter, unpacked):
        assert not hasattr(skill, "minimum_successful_tools")
        assert not hasattr(skill, "required_all_tools")
        assert not hasattr(skill, "required_any_tools")
    live_tools = _declared_mcp_tools()
    assert {
        "inspect_shader",
        "compile_shader_workspace",
        "try_shader_workspace_recovery_candidate",
        "validate_shader_workspace_candidate",
        "apply_shader_workspace_shared_resource",
        "rollback_shader_workspace_shared_resource",
        "restore_current_shader",
        "register_active_project_workspace",
        "get_current_shader_representation",
        "prepare_material_session",
        "update_material_parameter",
        "rollback_material_session",
        "clear_all_material_sessions",
        "apply_symbol_alias_table",
        "rollback_symbol_alias_table",
        "get_symbol_match_cases",
        "get_symbol_match_evidence",
        "list_symbol_matches",
        "submit_symbol_match",
    } <= live_tools
    assert "export_symbol_alias_table" in live_tools
    assert {
        "inspect_event",
        "analyze_current_frame_flow",
        "analyze_pixel",
        "capture_event_texture_evidence",
        "render_current_frame_flow_diagram",
        "classify_current_frame_passes",
        "apply_current_frame_pass_ai_proposals",
    } <= live_tools
    assert {
        "get_resource_export_capabilities",
        "inspect_resource_export",
        "prepare_resource_export",
        "finalize_resource_export",
        "get_resource_export_status",
        "run_resource_export_target_validation",
        "validate_resource_export",
    } <= live_tools
    assert {
        "export_drawcall",
        "export_to_unity",
        "save_texture",
    }.isdisjoint(live_tools)
    assert {
        "get_unpacked_asset_index_capabilities",
        "get_active_unpacked_asset_index_status",
        "index_active_unpacked_assets",
        "query_active_unpacked_assets",
        "resolve_active_unpacked_asset",
        "get_unpacked_asset_provider_capabilities",
        "get_unpacked_asset_provider_decisions",
        "query_unpacked_asset_inventory_assets",
        "start_unpacked_asset_report",
        "start_unpacked_asset_blender_export",
        "continue_unpacked_asset_blender_export",
        "get_unpacked_asset_job_status",
        "prepare_active_capture_project",
        "query_active_shader_asset_evidence",
        "inspect_resource_export",
    } <= live_tools
    assert {
        "export_drawcall",
        "export_to_unity",
        "save_texture",
    }.isdisjoint(live_tools)
    # Shader Skill v6+: a concrete tool recipe (identity values copied from
    # tool results, Host-applied renames, verdict-driven review, soft accept).
    normalized_shader = " ".join(shader.instructions.lower().split())
    assert "explain only" in normalized_shader
    assert "seven identity" in normalized_shader
    assert "try_shader_workspace_recovery_candidate" in normalized_shader
    assert "rename-only equivalent" in normalized_shader
    assert "flowspectemplate" in normalized_shader
    assert "roundsleft" in normalized_shader
    assert "softaccepted" in normalized_shader
    assert "register_active_project_workspace" in normalized_shader
    assert "observational-pass" in normalized_shader
    assert "fixed absolute/relative" in normalized_shader
    assert "complete function or computation stage" in normalized_shader
    assert "never review every few lines" in normalized_shader
    assert "same `organizationid`" in normalized_shader
    assert "soft-accepted-baseline" in normalized_shader
    assert "block and member names" in normalized_shader
    assert "external display aliases" in normalized_shader
    assert "intentional user edits" in normalized_shader
    assert "do not repeat organization review" in normalized_shader
    assert "registration alone can leave the panel read-only" in normalized_shader
    assert "apply_shader_workspace_shared_resource" in normalized_shader
    assert "start_shader_workspace_live_edit" in normalized_shader
    assert "captured block name/layout" in normalized_shader
    assert "native compilable source" in normalized_shader
    assert "only a concrete failed interface check requires repair" in normalized_shader
    assert "never call `create_replay_project`" in normalized_shader or "create_replay_project" in normalized_shader
    assert "rollback" in normalized_shader
    assert "aiprovisional" in event.instructions.lower()
    assert "actual-work" in event.instructions.lower()
    assert "observed-contributor" in event.instructions.lower()
    assert "executiontimeline" in event.instructions.lower()
    assert "max_actions=8000" in event.instructions.lower()
    assert "representative shader" in event.instructions.lower()
    assert "40-node/64-edge" in event.instructions.lower()
    assert "event visual-evidence contract" in event.instructions.lower()
    assert "event evidence and acceptance" in event.instructions.lower()
    assert "raw device depth" in event.instructions.lower()
    assert "gbuffer attachment report" in event.instructions.lower()
    assert "replaylab-core" in event.instructions.lower()
    assert "replaylab-event-route-decision" not in event.instructions.lower()
    assert "branch/assistance ledger" not in event.instructions.lower()
    assert "not a required phase prefix" in event.instructions.lower()
    assert "advisory telemetry only" in event.instructions.lower()
    assert "publish_to_ui=true" in event.instructions.lower()
    assert "glTF".lower() in exporter.instructions.lower()
    assert "never infer" in exporter.instructions.lower()
    assert "host-managed" in exporter.instructions.lower()
    assert "inverse-transpose" in exporter.instructions.lower()
    assert "strong inventory" in unpacked.instructions.lower()
    assert "stable observations" in unpacked.instructions.lower()
    assert "heuristic" in unpacked.instructions.lower()
    assert "exact cooked virtual skeletalmesh paths" in unpacked.instructions.lower()
    assert "analyze_current_frame_flow" in READ_ONLY_TOOLS
    assert "inspect_resource_export" in READ_ONLY_TOOLS


def test_event_visual_reference_uses_live_host_contracts_and_receipt_reuse() -> None:
    from src.event_flow_diagram import MAX_EDGES, MAX_NODES

    catalog = SkillCatalog([SKILL_ROOT]).load()
    event = " ".join(catalog.get("renderdoc-explain-event").instructions.lower().split())
    for retired in (
        "event_route.artifactobligations", "logical continuation prefix",
        "canonical request sha-256", "24 unique texture", "three unique rendered",
        "it rejects an exact duplicate", "`flat-range`", "authorized `workspace` phase",
    ):
        assert retired not in event, retired
    for contract in (
        "capture_event_texture_evidence", "range_mode=subresource-minmax",
        "attachment_kind=dsv", "attachment_index=0", "subresource-range-flat",
        "reused=true", "event_id=0", "max_actions=8000",
        f"{MAX_NODES}-node/{MAX_EDGES}-edge", "complete raw resource bytes",
        "equal display pngs only establish an unchanged display",
    ):
        assert contract in event, contract


def test_event_skill_keeps_generic_child_agent_autonomy_and_shader_skill_is_a_recipe() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    event = " ".join(catalog.get("renderdoc-explain-event").instructions.lower().split())
    shader = catalog.get("renderdoc-shader-workbench").instructions.lower()
    core = catalog.get("replaylab-core").instructions.lower()

    assert "generic child agent" in event
    assert "plan -> act -> observe -> validate -> replan" in event
    assert "specialist" not in event
    assert "useful partial result" in event
    assert "bounded unknown" in event or "bounded-unknown" in event
    assert "if one candidate remains, select it" in event
    assert "never offer or analyze `cs`" in event
    assert "uncertain mutation state" in event

    # The shader Skill (v6+) is a concrete tool recipe; the rules every child
    # shares live in the always-loaded core Skill and are referenced, not repeated.
    assert "replaylab-core" in shader
    assert "specialist" not in shader
    assert "prepare_shader_workspace" in shader
    assert "rename_shader_workspace_symbols" in shader
    assert "review_shader_workspace_organization" in shader
    assert "status: done" in shader
    for phrase in ("tool error", "verdict", "observed", "inferred", "generated", "status: done"):
        assert phrase in core, phrase


def test_runtime_packaging_embeds_the_canonical_workflow_skill_sources() -> None:
    script = PACKAGING_SCRIPT.read_text(encoding="utf-8")

    assert '$skillRoot = Join-Path $repositoryRoot ".agents\\skills"' in script
    for relative_path in (
        "renderdoc-explain-event\\SKILL.md",
        "renderdoc-explain-event\\references\\visual-evidence.md",
        "renderdoc-explain-event\\references\\workflow-acceptance.md",
        "renderdoc-shader-workbench\\SKILL.md",
    ):
        assert f'(Join-Path $skillRoot "{relative_path}")' in script


def test_required_skill_references_are_contained_and_host_loaded(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "bounded-skill"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    (references / "contract.md").write_text(
        "# Required contract\n\nEvidence must be exact.", encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bounded-skill\n"
        "description: Test required reference loading.\n"
        "metadata:\n"
        "  replaylab-required-references: references/contract.md\n"
        "---\n\n"
        "# Main contract\n",
        encoding="utf-8",
    )

    skill = SkillCatalog([skill_root]).load().get("bounded-skill")

    assert "# Main contract" in skill.instructions
    assert "## Mandatory reference: references/contract.md" in skill.instructions
    assert "Evidence must be exact." in skill.instructions


def test_required_skill_reference_rejects_parent_escape(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "bounded-skill"
    skill_dir.mkdir(parents=True)
    (skill_root / "outside.md").write_text("outside", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bounded-skill\n"
        "description: Test required reference containment.\n"
        "metadata:\n"
        "  replaylab-required-references: ../outside.md\n"
        "---\n\n"
        "# Main contract\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid required reference"):
        SkillCatalog([skill_root]).load()


def test_frozen_runtime_loads_skills_from_pyinstaller_bundle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert default_skill_root() == tmp_path / ".agents" / "skills"


def test_every_native_ui_intent_maps_to_a_loadable_skill() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert INTENT_SKILLS == {
        "explain-current-event": "renderdoc-explain-event",
        "inspect-picked-pixel": "renderdoc-explain-event",
        "reverse-edit-selected-shader": "renderdoc-shader-workbench",
    }
    assert all(catalog.get(skill_name) for skill_name in INTENT_SKILLS.values())


def test_every_workflow_skill_uses_evidence_and_effect_acceptance_not_rigid_required_tools() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for skill in catalog.skills.values():
        assert not hasattr(skill, "minimum_successful_tools")
        assert not hasattr(skill, "required_all_tools")
        assert not hasattr(skill, "required_any_tools")
        lowered = skill.instructions.lower()
        assert "evidence" in lowered
        # every Skill either states its own completion contract or defers to
        # the core Skill's STATUS line
        assert "completion" in lowered or "status: done" in lowered or "replaylab-core" in lowered


def test_retired_skill_tool_metadata_is_non_authoritative_compatibility_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "legacy-hints"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: legacy-hints\n"
        "description: Legacy execution metadata must be ignored.\n"
        "metadata:\n"
        "  replaylab-min-successful-tools: nope\n"
        "  replaylab-required-all-tools: inspect_shader BadTool\n"
        "  replaylab-required-any-tools: inspect_event inspect_event\n"
        "---\n\n"
        "# Adaptive guidance\n\nUse evidence and completion outcomes.\n",
        encoding="utf-8",
    )

    skill = SkillCatalog([root]).load().get("legacy-hints")

    assert skill.instructions
    assert not hasattr(skill, "minimum_successful_tools")
    assert not hasattr(skill, "required_all_tools")
    assert not hasattr(skill, "required_any_tools")


def test_removed_legacy_skills_are_not_discoverable() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for name in (
        "renderdoc-export-interoperability",
        "renderdoc-inspect-pixel",
        "renderdoc-material-cbuffer",
        "renderdoc-performance",
    ):
        with pytest.raises(ConfigurationError, match="unknown ReplayLab skill"):
            catalog.get(name)


def test_connection_effects_not_skill_text_control_child_tool_grants() -> None:
    read_only = child_tool_grant(())
    workspace = child_tool_grant(("workspace",))
    replay = child_tool_grant(("replay",))

    assert {"inspect_event", "inspect_shader"} <= read_only <= READ_ONLY_TOOLS
    assert "compile_shader_workspace" not in read_only
    assert "compile_shader_workspace" in workspace
    assert "apply_shader_workspace_candidate" not in workspace
    assert "apply_shader_workspace_candidate" in replay
    assert "update_material_parameter" in replay


def test_live_host_catalog_never_depends_on_skill_advertised_tools() -> None:
    catalog = _host_projected_catalog()
    external = _declared_mcp_tools()

    for skill in catalog.skills.values():
        assert not hasattr(skill, "allowed_tools")
    assert {"inspect_event", "inspect_shader", "report_task_result"} <= external
