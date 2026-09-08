"""Every execution role must load the same mandatory Host acceptance contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from replaylab_agent.agent_prompts import (
    deep_child_system_prompt,
    deep_main_system_prompt,
    native_agent_system_prompt,
)
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.errors import ConfigurationError


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
ROLES = ("api-main", "api-child", "api-general", "claude-acp", "codex", "other-acp")


def _prompt(catalog: CatalogView, role: str) -> str:
    skill = catalog.skill("renderdoc-shader-workbench")
    if role == "api-main":
        return deep_main_system_prompt(
            catalog=catalog, request="Explain this shader", context=None,
            response_language="zh_CN", subagents=[(skill.id, skill.description)],
        )
    if role.startswith("api-"):
        return deep_child_system_prompt(
            catalog=catalog, skill=skill if role == "api-child" else None,
            context=None, effects=(),
        )
    return native_agent_system_prompt(
        catalog=catalog, request="Explain this shader", context=None,
        response_language="zh_CN", tool_names=("inspect_shader",),
        protocol=role, effects=(), loaded_skill=skill,
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_role_gets_complete_single_core_independent_of_domain(role: str) -> None:
    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    core = catalog.skill("replaylab-core")
    prompt = _prompt(catalog, role)
    assert prompt.count(core.instructions.strip()) == 1
    assert core not in catalog.rank_skills("review acceptance template", limit=100)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("fault", ("missing", "disabled", "empty"))
def test_missing_standard_stops_every_prompt_path(role: str, fault: str) -> None:
    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    core = catalog.skill("replaylab-core")
    if fault == "missing":
        del catalog.skills[core.id]
    elif fault == "disabled":
        catalog.skills[core.id] = replace(core, always=False)
    else:
        catalog.skills[core.id] = replace(core, instructions=" ")
    with pytest.raises(ConfigurationError, match="Required replaylab-core"):
        _prompt(catalog, role)


def test_quarantined_core_cannot_silently_disappear_at_runtime(tmp_path: Path) -> None:
    core_dir = tmp_path / "replaylab-core"
    core_dir.mkdir()
    (core_dir / "SKILL.md").write_text("---\nname: [broken YAML\n---\n", encoding="utf-8")
    catalog = CatalogView.load(skill_roots=[tmp_path])
    assert catalog.diagnostics
    with pytest.raises(ConfigurationError, match="Required replaylab-core"):
        catalog.standing_skills()
