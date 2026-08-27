from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from replaylab_agent.planning import (
    PlannedTask,
    TaskPlan,
    external_planner_prompt,
    parse_external_plan,
    safe_fallback_plan,
    validate_plan,
)
from replaylab_agent.skills import SkillCatalog


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"


def _catalog() -> SkillCatalog:
    return SkillCatalog([SKILL_ROOT]).load()


def _task(task_id: str, skill: str, *, depends_on: list[str] | None = None) -> PlannedTask:
    return PlannedTask(
        id=task_id,
        summary=f"Run {skill}",
        skill=skill,
        depends_on=depends_on or [],
    )


def test_plan_accepts_only_backwards_dependencies_and_known_skills() -> None:
    plan = TaskPlan(
        request_summary="Explain an event, then inspect its resources",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task(
                "task-2",
                "renderdoc-resource-analysis",
                depends_on=["task-1"],
            ),
        ],
    )

    assert validate_plan(plan, _catalog()) is plan

    with pytest.raises(ValidationError, match="dependencies must refer to earlier tasks"):
        TaskPlan(
            request_summary="Invalid forward dependency",
            tasks=[
                _task("task-1", "renderdoc-explain-event", depends_on=["task-2"]),
                _task("task-2", "renderdoc-resource-analysis"),
            ],
        )


def test_plan_is_bounded_to_three_tasks() -> None:
    with pytest.raises(ValidationError):
        TaskPlan(
            request_summary="Too many tasks",
            tasks=[
                _task("task-1", "renderdoc-explain-event"),
                _task("task-2", "renderdoc-resource-analysis"),
                _task("task-3", "renderdoc-performance"),
                _task("task-1", "renderdoc-inspect-pixel"),
            ],
        )

    with pytest.raises(ValidationError, match="consecutive and ordered"):
        TaskPlan(
            request_summary="Skipped task id",
            tasks=[_task("task-2", "renderdoc-explain-event")],
        )


def test_parallelism_requires_independent_read_only_distinct_categories() -> None:
    catalog = _catalog()
    safe = TaskPlan(
        request_summary="Independent evidence collection",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-resource-analysis"),
        ],
    )
    same_category = TaskPlan(
        request_summary="Same category",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-explain-event"),
        ],
    )
    duplicate_category_among_three = TaskPlan(
        request_summary="Three tasks with one duplicate category",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-resource-analysis"),
            _task("task-3", "renderdoc-explain-event"),
        ],
    )
    shader_read_only = TaskPlan(
        request_summary="Contains the read-only shader reconstruction skill",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-shader-workbench"),
        ],
    )
    dependent = TaskPlan(
        request_summary="Sequential evidence",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task(
                "task-2",
                "renderdoc-resource-analysis",
                depends_on=["task-1"],
            ),
        ],
    )

    assert safe.can_run_parallel(catalog)
    assert not same_category.can_run_parallel(catalog)
    assert not duplicate_category_among_three.can_run_parallel(catalog)
    assert shader_read_only.can_run_parallel(catalog)
    assert not dependent.can_run_parallel(catalog)

    # Preserve the generic effectful-work invariant without misclassifying the
    # current Shader Skill, whose v1 tool grant is entirely read-only.
    effectful_catalog = _catalog()
    shader = effectful_catalog.skills["renderdoc-shader-workbench"]
    effectful_catalog.skills[shader.name] = replace(
        shader,
        metadata={**shader.metadata, "replaylab-read-only": "false"},
    )
    assert not shader_read_only.can_run_parallel(effectful_catalog)


def test_external_plan_parser_is_strict_and_catalog_bound() -> None:
    catalog = _catalog()
    raw = (
        '{"request_summary":"Explain the selected event",'
        '"tasks":[{"id":"task-1","summary":"Inspect event evidence",'
        '"skill":"renderdoc-explain-event","depends_on":[]}]}'
    )
    plan = parse_external_plan(raw, catalog)
    assert plan.tasks[0].skill == "renderdoc-explain-event"

    with pytest.raises(ValueError, match="exactly one JSON object"):
        parse_external_plan(f"```json\n{raw}\n```", catalog)
    with pytest.raises(ValueError, match="TaskPlan contract"):
        parse_external_plan(raw.replace("renderdoc-explain-event", "invented-skill"), catalog)


def test_external_planner_prompt_explicitly_has_no_tools_and_one_json_schema() -> None:
    prompt = external_planner_prompt("explain EID 7", _catalog())
    assert "Planning has no RenderDoc tools" in prompt
    assert "Return exactly one JSON object" in prompt
    assert '"maxItems":3' in prompt
    assert "explain EID 7" in prompt


@pytest.mark.parametrize(
    "user_prompt, expected_skill",
    [
        ("反编译当前 shader", "renderdoc-shader-workbench"),
        ("why is this pixel the wrong color", "renderdoc-inspect-pixel"),
        ("find the GPU performance bottleneck", "renderdoc-performance"),
        ("explain this EID", "renderdoc-explain-event"),
    ],
)
def test_safe_fallback_plan_is_one_bounded_specialist(user_prompt, expected_skill) -> None:
    plan = safe_fallback_plan(user_prompt, _catalog())
    assert len(plan.tasks) == 1
    assert plan.tasks[0].skill == expected_skill
