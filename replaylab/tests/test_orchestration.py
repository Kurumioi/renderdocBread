from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from replaylab_agent.errors import PolicyError
from replaylab_agent.orchestration import (
    BoundedSupervisorSpecialistReviewer,
    SpecialistDescriptor,
    SkillResult,
)
from replaylab_agent.planning import PlannedTask, TaskPlan
from replaylab_agent.skills import SkillCatalog


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"


def _plan() -> TaskPlan:
    return TaskPlan(
        request_summary="Inspect shader, then explain its event",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Inspect shader",
                skill="renderdoc-shader-workbench",
            ),
            PlannedTask(
                id="task-2",
                summary="Explain event",
                skill="renderdoc-explain-event",
                depends_on=["task-1"],
            ),
        ],
    )


def test_reviewer_accepts_exact_evidence_validated_result_set() -> None:
    strategy = BoundedSupervisorSpecialistReviewer()
    results = [
        SkillResult(
            task_id="task-1",
            skill="renderdoc-shader-workbench",
            summary="Inspect shader",
            answer="shader evidence",
            completed_tools=("inspect_shader",),
            evidence_validated=True,
        ),
        SkillResult(
            task_id="task-2",
            skill="renderdoc-explain-event",
            summary="Explain event",
            answer="event evidence",
            completed_tools=("inspect_event",),
            evidence_validated=True,
        ),
    ]
    synthesis = strategy.review_and_synthesize(_plan(), results)
    assert synthesis.accepted_task_ids == ("task-1", "task-2")
    assert "## Inspect shader" in synthesis.answer
    assert "event evidence" in synthesis.answer
    assert synthesis.evidence_validated is True


def test_reviewer_rejects_missing_or_identity_mismatched_specialist() -> None:
    strategy = BoundedSupervisorSpecialistReviewer()
    first = SkillResult(
        task_id="task-1",
        skill="renderdoc-shader-workbench",
        summary="Inspect shader",
        answer="shader evidence",
        evidence_validated=True,
    )
    with pytest.raises(PolicyError, match="missing specialist"):
        strategy.review_and_synthesize(_plan(), [first])

    wrong = SkillResult(
        task_id="task-2",
        skill="renderdoc-shader-workbench",
        summary="Explain event",
        answer="wrong specialist",
        evidence_validated=True,
    )
    with pytest.raises(PolicyError, match="identity does not match"):
        strategy.review_and_synthesize(_plan(), [first, wrong])


def test_default_strategy_parallelism_remains_host_policy_owned() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    strategy = BoundedSupervisorSpecialistReviewer()
    assert not strategy.can_run_parallel(_plan(), catalog)


def test_shader_specialist_has_isolated_provider_neutral_contract() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    strategy = BoundedSupervisorSpecialistReviewer()
    descriptor = strategy.specialist_descriptor(
        catalog.get("renderdoc-shader-workbench")
    )

    assert descriptor.skill == "renderdoc-shader-workbench"
    assert descriptor.role == "shader-lifecycle-specialist"
    assert descriptor.context_policy == "isolated-per-task"
    assert descriptor.reasoning_hint == "high"
    assert "shader" in (descriptor.model_hint or "")
    assert {field.name for field in descriptor.input_schema} >= {
        "request",
        "frozen_shader_identity",
        "capture_evidence",
    }
    assert {field.name for field in descriptor.output_schema} == {
        "bound_identity",
        "observed_evidence",
        "inferences",
        "generated_hlsl",
        "algorithm_explanation",
        "validation_report",
        "effect_and_recovery_report",
        "project_library_receipt",
        "unknowns",
    }
    rendered = descriptor.render_prompt_contract()
    assert "matching rollback route" in rendered
    assert "project registration" in rendered
    lowered = rendered.lower()
    assert "codex" not in lowered
    assert "anthropic" not in lowered
    assert "gemini" not in lowered


def test_event_specialist_has_flow_ui_and_shader_delegation_contract() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    descriptor = BoundedSupervisorSpecialistReviewer().specialist_descriptor(
        catalog.get("renderdoc-explain-event")
    )

    assert descriptor.role == "event-pipeline-specialist"
    assert {field.name for field in descriptor.output_schema} == {
        "bound_scope",
        "event_findings",
        "resource_flow_graph",
        "shader_delegation_requests",
        "ui_organization",
        "unknowns",
    }
    rendered = descriptor.render_prompt_contract()
    assert "AIProvisional" in rendered
    assert "flow-graph edge" in rendered
    assert "shader-specialist" in rendered
    assert "cannot rewrite native RenderDoc Event Browser names" in rendered


def test_runtime_rejects_descriptor_that_retargets_selected_skill(tmp_path) -> None:
    from replaylab_agent.profiles import ProfileStore
    from replaylab_agent.runtime import AgentRuntime

    class RetargetingStrategy(BoundedSupervisorSpecialistReviewer):
        def specialist_descriptor(self, skill):
            del skill
            return SpecialistDescriptor.generic("renderdoc-performance")

    catalog = SkillCatalog([SKILL_ROOT]).load()
    runtime = AgentRuntime(
        ProfileStore(tmp_path / "profiles.json"),
        catalog,
        orchestration_strategy=RetargetingStrategy(),
    )
    with pytest.raises(PolicyError, match="identity does not match"):
        runtime._specialist_descriptor(catalog.get("renderdoc-shader-workbench"))


def test_shader_contract_is_embedded_in_the_shared_skill_prompt(tmp_path) -> None:
    from replaylab_agent.profiles import ProfileStore
    from replaylab_agent.runtime import AgentRuntime

    catalog = SkillCatalog([SKILL_ROOT]).load()
    runtime = AgentRuntime(ProfileStore(tmp_path / "profiles.json"), catalog)
    skill = catalog.get("renderdoc-shader-workbench")
    descriptor = runtime._specialist_descriptor(skill)
    prompt = runtime._skill_system_prompt(
        skill,
        frozenset({"get_shader_info", "reverse_shader"}),
        frozenset(),
        descriptor,
    )

    assert "shader-lifecycle-specialist" in prompt
    assert "Context policy: isolated-per-task" in prompt
    assert "Generated HLSL" in prompt
    assert "Selected skill (renderdoc-shader-workbench)" in prompt


def test_runtime_host_policy_can_prevent_strategy_from_expanding_parallelism(tmp_path) -> None:
    from replaylab_agent.profiles import ProfileStore
    from replaylab_agent.runtime import AgentRuntime

    class OvereagerStrategy(BoundedSupervisorSpecialistReviewer):
        def can_run_parallel(self, plan, catalog):
            del plan, catalog
            return True

    dependent = TaskPlan(
        request_summary="Sequential evidence",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event",
                skill="renderdoc-explain-event",
            ),
            PlannedTask(
                id="task-2",
                summary="Inspect shader",
                skill="renderdoc-shader-workbench",
                depends_on=["task-1"],
            ),
        ],
    )
    runtime = AgentRuntime(
        ProfileStore(tmp_path / "profiles.json"),
        SkillCatalog([SKILL_ROOT]).load(),
        orchestration_strategy=OvereagerStrategy(),
    )
    assert runtime._can_run_parallel(dependent) is False


@pytest.mark.asyncio
async def test_parallel_specialist_failure_cancels_siblings() -> None:
    from replaylab_agent.runtime import AgentRuntime

    sibling_cancelled = asyncio.Event()

    async def fail():
        await asyncio.sleep(0)
        raise RuntimeError("specialist failed")

    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    with pytest.raises(RuntimeError, match="specialist failed"):
        await AgentRuntime._gather_specialists(fail(), wait_forever())
    assert sibling_cancelled.is_set()


def test_dependency_answers_are_bounded_before_specialist_delegation() -> None:
    from replaylab_agent.runtime import AgentRuntime

    task = PlannedTask(
        id="task-2",
        summary="Use prerequisite evidence",
        skill="renderdoc-explain-event",
        depends_on=["task-1"],
    )
    rendered = AgentRuntime._dependency_context(
        task,
        {"task-1": "x" * (AgentRuntime._DEPENDENCY_CONTEXT_MAX_CHARS + 1)},
    )
    assert "Prerequisite answer text truncated" in rendered
    assert len(rendered) < AgentRuntime._DEPENDENCY_CONTEXT_MAX_CHARS + 256
