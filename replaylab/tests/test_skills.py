from __future__ import annotations

from pathlib import Path

import pytest

from replaylab_agent.errors import ConfigurationError
from replaylab_agent.policy import EFFECTFUL_TOOLS, READ_ONLY_TOOLS
from replaylab_agent.runtime import AgentRuntime
from replaylab_agent.skills import INTENT_SKILLS, SkillCatalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills"


def test_repository_skills_load_with_bounded_tool_sets() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert set(catalog.skills) == {
        "renderdoc-explain-event",
        "renderdoc-export-interoperability",
        "renderdoc-inspect-pixel",
        "renderdoc-material-cbuffer",
        "renderdoc-performance",
        "renderdoc-resource-analysis",
        "renderdoc-shader-workbench",
    }
    for skill in catalog.skills.values():
        assert skill.path.is_absolute()
        assert skill.instructions
        assert skill.allowed_tools
        assert skill.category != "other"
    assert not catalog.skills["renderdoc-export-interoperability"].enabled
    assert "renderdoc-export-interoperability" not in catalog.summaries()
    shader = catalog.skills["renderdoc-shader-workbench"]
    assert shader.read_only is True
    assert shader.required_all_tools == {"get_shader_info", "reverse_shader"}
    assert "generated reconstruction" in shader.instructions.lower()
    assert "nonce" in shader.instructions.lower()
    with pytest.raises(ConfigurationError, match="disabled until"):
        catalog.get("renderdoc-export-interoperability")


def test_every_native_ui_intent_maps_to_a_loadable_skill() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert INTENT_SKILLS == {
        "explain-current-event": "renderdoc-explain-event",
        "inspect-picked-pixel": "renderdoc-inspect-pixel",
        "reverse-edit-selected-shader": "renderdoc-shader-workbench",
    }
    assert all(catalog.get(skill_name) for skill_name in INTENT_SKILLS.values())


def test_every_model_exposed_skill_tool_is_authoritatively_read_only() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for skill in catalog.skills.values():
        if skill.name == "renderdoc-export-interoperability":
            assert skill.allowed_tools <= EFFECTFUL_TOOLS
            with pytest.raises(ConfigurationError, match="native, nonce-bound"):
                AgentRuntime._effective_tools(skill, None)
            continue
        effective, withheld = AgentRuntime._effective_tools(skill, None)
        assert effective
        assert effective <= READ_ONLY_TOOLS
        assert not (effective & EFFECTFUL_TOOLS)
        assert withheld == skill.allowed_tools - effective
        assert skill.minimum_successful_tools >= 1
