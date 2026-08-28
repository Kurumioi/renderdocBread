from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from replaylab_agent.errors import PolicyError
from replaylab_agent.planning import (
    PlannedTask,
    TaskPlan,
    external_planner_prompt,
    infer_workflow_effects,
    parse_external_plan,
    planner_instructions,
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
        request_summary="Inspect a shader, then explain its event",
        tasks=[
            _task("task-1", "renderdoc-shader-workbench"),
            _task(
                "task-2",
                "renderdoc-explain-event",
                depends_on=["task-1"],
            ),
        ],
    )

    assert validate_plan(plan, _catalog()) is plan

    with pytest.raises(ValidationError, match="dependencies must refer to earlier tasks"):
        TaskPlan(
            request_summary="Invalid forward dependency",
            tasks=[
                _task("task-1", "renderdoc-shader-workbench", depends_on=["task-2"]),
                _task("task-2", "renderdoc-explain-event"),
            ],
        )


def test_plan_is_bounded_to_three_tasks() -> None:
    with pytest.raises(ValidationError):
        TaskPlan(
            request_summary="Too many tasks",
            tasks=[
                _task("task-1", "renderdoc-explain-event"),
                _task("task-2", "renderdoc-shader-workbench"),
                _task("task-3", "renderdoc-explain-event"),
                _task("task-1", "renderdoc-shader-workbench"),
            ],
        )

    with pytest.raises(ValidationError, match="consecutive and ordered"):
        TaskPlan(
            request_summary="Skipped task id",
            tasks=[_task("task-2", "renderdoc-explain-event")],
        )


def test_parallelism_requires_independent_read_only_distinct_categories() -> None:
    catalog = _catalog()
    independent = TaskPlan(
        request_summary="Independent evidence collection",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-shader-workbench"),
        ],
    )
    same_category = TaskPlan(
        request_summary="Same category",
        tasks=[
            _task("task-1", "renderdoc-explain-event"),
            _task("task-2", "renderdoc-explain-event"),
        ],
    )
    dependent = TaskPlan(
        request_summary="Sequential evidence",
        tasks=[
            _task("task-1", "renderdoc-shader-workbench"),
            _task(
                "task-2",
                "renderdoc-explain-event",
                depends_on=["task-1"],
            ),
        ],
    )

    assert not independent.can_run_parallel(catalog)
    assert not same_category.can_run_parallel(catalog)
    assert not dependent.can_run_parallel(catalog)

    # The host can still prove the generic read-only/distinct-category rule on
    # an explicitly narrowed snapshot of the two workflows.
    from dataclasses import replace

    read_only_catalog = _catalog()
    for name, skill in tuple(read_only_catalog.skills.items()):
        read_only_catalog.skills[name] = replace(
            skill, metadata={**skill.metadata, "replaylab-read-only": "true"}
        )
    assert independent.can_run_parallel(read_only_catalog)


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
        ("why is this pixel the wrong color", "renderdoc-explain-event"),
        ("find the GPU performance bottleneck", "renderdoc-explain-event"),
        ("explain this EID", "renderdoc-explain-event"),
    ],
)
def test_safe_fallback_plan_is_one_bounded_specialist(user_prompt, expected_skill) -> None:
    plan = safe_fallback_plan(user_prompt, _catalog())
    assert len(plan.tasks) == 1
    assert plan.tasks[0].skill == expected_skill


def test_safe_fallback_plan_never_selects_an_arbitrary_default_specialist() -> None:
    with pytest.raises(PolicyError, match="cannot safely infer a specialist"):
        safe_fallback_plan("Please help me understand what is going on", _catalog())


def test_planned_effects_are_semantic_outcomes_with_replay_preflight() -> None:
    task = PlannedTask(
        id="task-1",
        summary="Build and replace the selected shader",
        skill="renderdoc-shader-workbench",
        effects=["workspace", "replay"],
    )
    plan = TaskPlan(request_summary="Replace the shader", tasks=[task])

    assert validate_plan(plan, _catalog()) is plan

    with pytest.raises(ValidationError, match="workflow effects must be unique"):
        PlannedTask(
            id="task-1",
            summary="Duplicate outcome",
            skill="renderdoc-shader-workbench",
            effects=["workspace", "workspace"],
        )
    with pytest.raises(ValidationError, match="replay effect requires workspace"):
        PlannedTask(
            id="task-1",
            summary="Unsafe replacement",
            skill="renderdoc-shader-workbench",
            effects=["replay"],
        )


def test_plan_rejects_an_outcome_the_selected_specialist_does_not_implement() -> None:
    plan = TaskPlan(
        request_summary="Ask the event specialist to build a shader workspace",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Build shader source",
                skill="renderdoc-explain-event",
                effects=["workspace"],
            )
        ],
    )

    with pytest.raises(PolicyError, match="does not implement workflow effect"):
        validate_plan(plan, _catalog())


def test_event_ui_plan_requires_project_staging_effect() -> None:
    plan = TaskPlan(
        request_summary="Publish the foldable pass panel",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Publish pass organization",
                skill="renderdoc-explain-event",
                effects=["ui"],
            )
        ],
    )

    with pytest.raises(PolicyError, match="requires the project effect"):
        validate_plan(plan, _catalog())


@pytest.mark.parametrize(
    ("user_request", "expected"),
    [
        ("解释当前 EID，不修改任何内容", ()),
        ("Do not replace shader; explain it only", ()),
        ("不要替换着色器，只解释算法", ()),
        ("反编译并生成 HLSL", ("workspace",)),
        ("replace shader", ("workspace", "replay")),
        ("重命名 EID 并发布折叠目录", ("project", "ui")),
        (
            "生成 HLSL，编译回去替换着色器，再加入项目库",
            ("workspace", "project", "replay"),
        ),
    ],
)
def test_fallback_effect_inference_requires_explicit_action_language(
    user_request: str, expected: tuple[str, ...]
) -> None:
    assert infer_workflow_effects(user_request) == expected


def test_external_plan_round_trips_effects_and_prompt_calls_them_outcomes() -> None:
    raw = (
        '{"request_summary":"Replace the selected shader",'
        '"tasks":[{"id":"task-1","summary":"Validate and replace it",'
        '"skill":"renderdoc-shader-workbench","depends_on":[],'
        '"effects":["workspace","replay"]}]}'
    )

    plan = parse_external_plan(raw, _catalog())
    instructions = planner_instructions(_catalog())

    assert plan.tasks[0].effects == ["workspace", "replay"]
    assert "semantic outcomes explicitly requested by the user" in instructions
    assert "not exact tool permissions" in instructions
    assert not plan.can_run_parallel(_catalog())
