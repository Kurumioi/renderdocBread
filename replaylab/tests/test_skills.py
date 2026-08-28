from __future__ import annotations

import ast
from pathlib import Path

import pytest

from replaylab_agent.errors import ConfigurationError
from replaylab_agent.policy import READ_ONLY_TOOLS
from replaylab_agent.runtime import AgentRuntime
from replaylab_agent.skills import INTENT_SKILLS, SkillCatalog
from replaylab_agent.tool_service import available_internal_tools


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
MCP_SERVER = REPOSITORY_ROOT / "replaylab" / "mcp_host" / "src" / "server.py"


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


def test_repository_skills_load_with_bounded_tool_sets() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert set(catalog.skills) == {
        "renderdoc-explain-event",
        "renderdoc-shader-workbench",
    }
    for skill in catalog.skills.values():
        assert skill.path.is_absolute()
        assert skill.instructions
        assert skill.allowed_tools
        assert skill.category != "other"
    shader = catalog.skills["renderdoc-shader-workbench"]
    event = catalog.skills["renderdoc-explain-event"]
    assert shader.read_only is False
    assert event.read_only is False
    assert shader.required_all_tools == set()
    assert event.required_all_tools == set()
    assert {
        "inspect_shader",
        "compile_shader_workspace",
        "validate_shader_workspace_candidate",
        "apply_shader_workspace_shared_resource",
        "rollback_shader_workspace_shared_resource",
        "register_active_project_workspace",
    } <= shader.allowed_tools
    assert {
        "inspect_event",
        "analyze_pixel",
        "classify_current_frame_passes",
        "apply_current_frame_pass_ai_proposals",
    } <= event.allowed_tools
    assert "generated hlsl" in shader.instructions.lower()
    assert "original source" in shader.instructions.lower()
    assert "aiprovisional" in event.instructions.lower()


def test_every_native_ui_intent_maps_to_a_loadable_skill() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert INTENT_SKILLS == {
        "explain-current-event": "renderdoc-explain-event",
        "inspect-picked-pixel": "renderdoc-explain-event",
        "reverse-edit-selected-shader": "renderdoc-shader-workbench",
    }
    assert all(catalog.get(skill_name) for skill_name in INTENT_SKILLS.values())


def test_every_specialist_uses_evidence_and_effect_acceptance_not_rigid_required_tools() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for skill in catalog.skills.values():
        assert skill.minimum_successful_tools >= 1
        assert not skill.required_all_tools
        lowered = skill.instructions.lower()
        assert "evidence" in lowered
        assert "completion" in lowered


def test_removed_specialists_are_not_discoverable() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for name in (
        "renderdoc-export-interoperability",
        "renderdoc-inspect-pixel",
        "renderdoc-material-cbuffer",
        "renderdoc-performance",
    ):
        with pytest.raises(ConfigurationError, match="unknown ReplayLab skill"):
            catalog.get(name)


def test_native_capture_intents_keep_bounded_composites_without_locking_shader_reads() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    bounded = {
        "explain-current-event": {"inspect_event"},
        "inspect-picked-pixel": {"analyze_pixel"},
    }
    for intent_kind, tools in bounded.items():
        skill = catalog.get(INTENT_SKILLS[intent_kind])
        effective, withheld = AgentRuntime._effective_tools(
            skill, {"kind": intent_kind}
        )
        assert effective == tools
        assert effective <= READ_ONLY_TOOLS
        assert effective <= available_internal_tools()
        assert withheld == skill.allowed_tools - tools

    shader = catalog.get(INTENT_SKILLS["reverse-edit-selected-shader"])
    effective, withheld = AgentRuntime._effective_tools(
        shader, {"kind": "reverse-edit-selected-shader"}
    )
    assert effective == shader.allowed_tools & READ_ONLY_TOOLS
    assert {"inspect_shader", "reverse_shader", "get_shader_workspace_status"} <= effective
    assert withheld == shader.allowed_tools - effective


def test_general_specialists_expose_all_read_routes_and_withhold_unrequested_effects() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for skill in catalog.skills.values():
        effective, withheld = AgentRuntime._effective_tools(skill, None)
        assert effective
        assert effective == skill.allowed_tools & READ_ONLY_TOOLS
        assert withheld == skill.allowed_tools - effective


def test_specialists_never_advertise_invented_tools() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    internal = set(available_internal_tools())
    external = _declared_mcp_tools()
    for skill in catalog.skills.values():
        assert skill.allowed_tools <= internal
        assert skill.allowed_tools <= external
