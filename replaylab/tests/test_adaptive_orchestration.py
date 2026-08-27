from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from replaylab_agent.adaptive_orchestration import (
    AdaptiveSupervisorWorkflow,
    BlockedDecision,
    DispatchNextStageDecision,
    FinishDecision,
    GoalSpec,
    InitialAskUserDecision,
    InitialSupervisorProposal,
    SpecialistSession,
    StageExecution,
    StageRecord,
    supervisor_review_prompt,
)
from replaylab_agent.errors import (
    ConnectorError,
    EvidenceValidationError,
    LifecycleCleanupError,
    PolicyError,
)
from replaylab_agent.events import TaskEventWriter
from replaylab_agent.orchestration import SkillResult
from replaylab_agent.planning import PlannedTask, TaskPlan
from replaylab_agent.profiles import Profile, ProfileStore
from replaylab_agent.runtime import AgentRunResult, AgentRuntime
from replaylab_agent.skills import SkillCatalog


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"


def _result(task: PlannedTask, answer: str) -> SkillResult:
    return SkillResult(
        task_id=task.id,
        skill=task.skill,
        summary=task.summary,
        answer=answer,
        completed_tools=("get_pipeline_state",),
        evidence_validated=True,
    )


def test_openai_chat_override_does_not_enable_responses_only_context_features() -> None:
    profile = Profile(
        "openai-chat",
        "openai",
        "gpt-test",
        options={"api": "chat", "context_window_tokens": 32768},
    )
    assert AgentRuntime._provider_context_capabilities(profile) == []
    assert AgentRuntime._context_usage_limit_options(profile) == {}


def test_anthropic_proxy_does_not_receive_official_context_management_fields() -> None:
    profile = Profile(
        "anthropic-proxy",
        "anthropic",
        "claude-compatible",
        base_url="https://anthropic-proxy.example/v1",
        options={"context_window_tokens": 200_000},
    )
    assert AgentRuntime._uses_official_anthropic(profile) is False
    assert AgentRuntime._provider_context_capabilities(profile) == []
    assert AgentRuntime._context_usage_limit_options(profile) == {}


@pytest.mark.asyncio
async def test_langgraph_supervisor_dispatches_second_stage_then_finishes() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    first = TaskPlan(
        request_summary="Inspect the event",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain the selected event",
                skill="renderdoc-explain-event",
            )
        ],
    )
    second = TaskPlan(
        request_summary="Inspect resources after event evidence",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Inspect resources referenced by the event evidence",
                skill="renderdoc-resource-analysis",
            )
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(
            objective="Explain the event and find suspicious resources",
            success_criteria=["Both claims use admitted evidence"],
        ),
        stage_objective="Establish event evidence",
        plan=first,
    )
    executed: list[tuple[int, int]] = []

    async def execute(stage_index, objective, plan, prior_stages):
        del objective
        executed.append((stage_index, len(prior_stages)))
        result = _result(plan.tasks[0], f"evidence from stage {stage_index}")
        return StageExecution(
            results=(result,),
            sessions=(
                SpecialistSession.accepted(
                    stage_index,
                    result,
                    attempts=2 if stage_index == 1 else 1,
                    feedback=("missing evidence corrected",) if stage_index == 1 else (),
                ),
            ),
        )

    async def decide(state):
        if state["stage_index"] == 1:
            return DispatchNextStageDecision(
                reason="Event evidence identifies resources that need a separate bounded check.",
                stage_objective="Inspect the referenced resources",
                plan=second,
            )
        return FinishDecision(
            reason="The goal and both evidence domains are complete.",
            final_answer="Two-stage evidence-grounded answer",
        )

    report = await AdaptiveSupervisorWorkflow(
        catalog, execute, decide, max_stages=3, max_total_tasks=4
    ).run(proposal)
    assert executed == [(1, 0), (2, 1)]
    assert len(report.stages) == 2
    assert report.final_answer == "Two-stage evidence-grounded answer"
    assert report.total_tasks == 2
    assert report.stages[0].sessions[0].attempts == 2
    assert all(
        session.released
        for stage in report.stages
        for session in stage.sessions
    )


@pytest.mark.asyncio
async def test_denied_next_stage_is_blocked_not_falsely_completed() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    plan = TaskPlan(
        request_summary="Inspect",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event",
                skill="renderdoc-explain-event",
            )
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Inspect until complete"),
        stage_objective="First stage",
        plan=plan,
    )

    async def execute(stage_index, objective, current_plan, prior):
        del objective, prior
        result = _result(current_plan.tasks[0], "partial evidence")
        return StageExecution(
            results=(result,),
            sessions=(SpecialistSession.accepted(stage_index, result),),
        )

    async def decide(state):
        del state
        return DispatchNextStageDecision(
            reason="More work remains",
            stage_objective="Second stage",
            plan=plan,
        )

    report = await AdaptiveSupervisorWorkflow(
        catalog, execute, decide, max_stages=1, max_total_tasks=3
    ).run(proposal)
    assert report.terminal_status == "blocked"
    assert "partial" in report.final_answer
    assert "maximum stage budget" in report.final_answer


@pytest.mark.asyncio
async def test_initial_ask_user_finishes_without_dispatching_a_specialist() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    calls = 0

    async def execute(*args):
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected specialist dispatch: {args}")

    async def decide(state):
        raise AssertionError(f"unexpected supervisor review: {state}")

    initial = InitialAskUserDecision(
        goal=GoalSpec(
            objective="Inspect the exact pixel the user means",
            ambiguities=["No picked pixel coordinate is available"],
        ),
        reason="A coordinate is required before a pixel-specific inspection.",
        question="Which texture pixel should I inspect?",
    )
    report = await AdaptiveSupervisorWorkflow(catalog, execute, decide).run(initial)
    assert calls == 0
    assert report.stages == ()
    assert report.total_tasks == 0
    assert report.terminal_status == "needs-user-input"
    assert report.final_answer == "Which texture pixel should I inspect?"


@pytest.mark.asyncio
async def test_partial_stage_receipts_reach_supervisor_and_cannot_finish_as_complete() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    plan = TaskPlan(
        request_summary="Inspect event and resource",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event",
                skill="renderdoc-explain-event",
            ),
            PlannedTask(
                id="task-2",
                summary="Inspect resource",
                skill="renderdoc-resource-analysis",
            ),
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Inspect both domains"),
        stage_objective="Run independent specialists",
        plan=plan,
    )
    observed = {}

    async def execute(stage_index, objective, current_plan, prior):
        del objective, prior
        accepted = _result(current_plan.tasks[0], "admitted event evidence")
        return StageExecution(
            results=(accepted,),
            sessions=(
                SpecialistSession.accepted(stage_index, accepted),
                SpecialistSession.ended(
                    stage_index,
                    "task-2",
                    "renderdoc-resource-analysis",
                    status="failed",
                    reason="provider timeout after cleanup",
                ),
            ),
        )

    async def decide(state):
        observed["state"] = state
        # Even a mistaken finish proposal may not turn a failed dispatched task
        # into a successful workflow terminal state.
        return FinishDecision(reason="mistakenly enough", final_answer="partial")

    report = await AdaptiveSupervisorWorkflow(catalog, execute, decide).run(proposal)
    assert len(observed["state"]["stages"][0].results) == 1
    assert [item.status for item in observed["state"]["stages"][0].sessions] == [
        "accepted",
        "failed",
    ]
    assert report.total_tasks == 2
    assert report.terminal_status == "blocked"
    assert "provider timeout" in report.final_answer


@pytest.mark.asyncio
async def test_failed_stage_can_replan_and_finish_after_corrective_stage_is_accepted() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    first = TaskPlan(
        request_summary="Inspect event",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event through the first evidence path",
                skill="renderdoc-explain-event",
            )
        ],
    )
    corrective = TaskPlan(
        request_summary="Recover with resource evidence",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Inspect the event's bound resource as corrective evidence",
                skill="renderdoc-resource-analysis",
            )
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Explain the event with admitted evidence"),
        stage_objective="Try the primary evidence path",
        plan=first,
    )

    async def execute(stage_index, objective, current_plan, prior):
        del objective, prior
        task = current_plan.tasks[0]
        if stage_index == 1:
            return StageExecution(
                results=(),
                sessions=(
                    SpecialistSession.ended(
                        stage_index,
                        task.id,
                        task.skill,
                        status="failed",
                        reason="primary evidence path was unavailable",
                    ),
                ),
            )
        result = _result(task, "corrective admitted evidence")
        return StageExecution(
            results=(result,),
            sessions=(SpecialistSession.accepted(stage_index, result),),
        )

    async def decide(state):
        if state["stage_index"] == 1:
            return DispatchNextStageDecision(
                reason="A distinct corrective evidence path is available.",
                stage_objective="Run the corrective evidence path",
                plan=corrective,
            )
        return FinishDecision(
            reason="The corrective stage passed host admission.",
            final_answer="Recovered evidence-grounded answer",
        )

    report = await AdaptiveSupervisorWorkflow(
        catalog, execute, decide, max_stages=2, max_total_tasks=2
    ).run(proposal)
    assert [stage.sessions[0].status for stage in report.stages] == [
        "failed",
        "accepted",
    ]
    assert report.terminal_status == "completed"
    assert report.final_answer == "Recovered evidence-grounded answer"


def test_supervisor_review_budget_is_shared_and_session_receipt_is_stage_bound() -> None:
    task_a = PlannedTask(
        id="task-1",
        summary="A",
        skill="renderdoc-explain-event",
    )
    task_b = PlannedTask(
        id="task-2",
        summary="B",
        skill="renderdoc-resource-analysis",
    )
    plan = TaskPlan(request_summary="review", tasks=[task_a, task_b])
    result_a = _result(task_a, "a" * 10000)
    result_b = _result(task_b, "b" * 10000)
    stage = StageRecord(
        stage_id="stage-1",
        objective="review both",
        plan=plan,
        results=(result_a, result_b),
        sessions=(
            SpecialistSession.accepted(1, result_a),
            SpecialistSession.accepted(1, result_b),
        ),
    )
    payload = json.loads(
        supervisor_review_prompt(
            {
                "goal": GoalSpec(objective="review both"),
                "stages": [stage],
                "stage_index": 1,
                "total_tasks": 2,
                "max_stages": 3,
                "max_total_tasks": 4,
            },
            answer_char_budget=8192,
        )
    )
    answers = [item["answer"] for item in payload["completedStages"][0]["results"]]
    assert [len(answer) for answer in answers] == [4096, 4096]
    assert payload["hostBudget"] == {
        "maxStages": 3,
        "maxTotalTasks": 4,
        "remainingStages": 2,
        "remainingTaskSlots": 2,
        "maxTasksInNextStage": 2,
    }

    wrong_stage = StageExecution(
        results=(result_a, result_b),
        sessions=(
            SpecialistSession.accepted(2, result_a),
            SpecialistSession.accepted(1, result_b),
        ),
    )
    with pytest.raises(PolicyError, match="failed admission"):
        AdaptiveSupervisorWorkflow._validate_stage_execution(2, plan, wrong_stage, ())


@pytest.mark.asyncio
async def test_model_runtime_uses_v2_proposal_and_main_agent_replans(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

    catalog = SkillCatalog([SKILL_ROOT]).load()
    first = TaskPlan(
        request_summary="Explain event first",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event evidence",
                skill="renderdoc-explain-event",
            )
        ],
    )
    second = TaskPlan(
        request_summary="Inspect resources next",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Inspect resource evidence",
                skill="renderdoc-resource-analysis",
            )
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Explain the event and related resources"),
        stage_objective="Establish event facts",
        plan=first,
    )
    review_count = 0

    class FakeAgent:
        def __init__(self, model, *, output_type, system_prompt="", **kwargs):
            del model, kwargs
            self.output_type = output_type
            self.system_prompt = system_prompt

        async def run(self, prompt, **kwargs):
            nonlocal review_count
            del prompt, kwargs
            if "reviewing host-admitted specialist results" not in self.system_prompt:
                return SimpleNamespace(output=proposal)
            review_count += 1
            if review_count == 1:
                return SimpleNamespace(
                    output=DispatchNextStageDecision(
                        reason="The first result points to a distinct resource question.",
                        stage_objective="Inspect resources",
                        plan=second,
                    )
                )
            return SimpleNamespace(
                output=FinishDecision(
                    reason="All accepted evidence is sufficient.",
                    final_answer="Final adaptive answer",
                )
            )

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )

    async def fake_skill(self, profile, prompt, skill, context, writer, intent, **kwargs):
        del self, profile, prompt, context, writer, intent, kwargs
        return SimpleNamespace(
            text=f"accepted {skill.name}",
            completed_tools=("get_pipeline_state",),
            attempts=1,
            validation_feedback=(),
            context_generation=0,
        )

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())
    runtime._run_model_skill = MethodType(fake_skill, runtime)
    result = await runtime._run_model_plan(
        runtime._profile("api"),
        "Explain EID 9 and related resources",
        {"eventId": 9},
        TaskEventWriter(tmp_path / "events", task_id="adaptive-model"),
        None,
        "C:/capture.rdc",
        "qrenderdoc-1",
    )
    assert result.text == "Final adaptive answer"
    assert result.workflow is not None
    assert len(result.workflow.stages) == 2
    assert [item.task_id for item in result.skill_results] == [
        "stage-1-task-1",
        "stage-2-task-1",
    ]
    assert result.synthesis is not None
    assert result.synthesis.strategy == "adaptive-supervisor-specialists/langgraph-v2"


@pytest.mark.asyncio
async def test_model_runtime_returns_failed_and_dependency_blocked_receipts_to_supervisor(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

    catalog = SkillCatalog([SKILL_ROOT]).load()
    plan = TaskPlan(
        request_summary="Inspect event then its resource",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event",
                skill="renderdoc-explain-event",
            ),
            PlannedTask(
                id="task-2",
                summary="Inspect dependent resource",
                skill="renderdoc-resource-analysis",
                depends_on=["task-1"],
            ),
        ],
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Inspect event and dependent resource"),
        stage_objective="Collect dependent evidence",
        plan=plan,
    )
    class FakeAgent:
        def __init__(self, model, *, output_type, system_prompt="", **kwargs):
            del model, output_type, kwargs
            self.system_prompt = system_prompt

        async def run(self, prompt, **kwargs):
            del prompt, kwargs
            if "reviewing host-admitted specialist results" not in self.system_prompt:
                return SimpleNamespace(output=proposal)
            return SimpleNamespace(
                output=BlockedDecision(
                    reason="The prerequisite specialist failed.",
                    answer="Unable to complete the dependent inspection.",
                )
            )

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )
    calls = []

    async def failing_skill(self, profile, prompt, skill, context, writer, intent, **kwargs):
        del self, profile, prompt, context, writer, intent, kwargs
        calls.append(skill.name)
        raise ConnectorError("provider failed after owned cleanup")

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())
    runtime._run_model_skill = MethodType(failing_skill, runtime)

    result = await runtime._run_model_plan(
        runtime._profile("api"),
        "Inspect EID 9 and its dependent resource",
        {"eventId": 9},
        TaskEventWriter(tmp_path / "events", task_id="partial-model"),
        None,
        "C:/capture.rdc",
        "qrenderdoc-1",
    )

    assert calls == ["renderdoc-explain-event"]
    assert result.workflow is not None
    assert result.workflow.terminal_status == "blocked"
    assert result.workflow.stages[0].results == ()
    assert [session.status for session in result.workflow.stages[0].sessions] == [
        "failed",
        "blocked",
    ]
    assert result.workflow.stages[0].sessions[1].attempts == 0


@pytest.mark.asyncio
async def test_legacy_model_plan_failure_returns_blocked_workflow_receipt(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

    catalog = SkillCatalog([SKILL_ROOT]).load()
    plan = TaskPlan(
        request_summary="Inspect event",
        tasks=[
            PlannedTask(
                id="task-1",
                summary="Explain event",
                skill="renderdoc-explain-event",
            )
        ],
    )

    class FakePlannerAgent:
        def __init__(self, model, *, output_type, **kwargs):
            del model, output_type, kwargs

        async def run(self, prompt, **kwargs):
            del prompt, kwargs
            return SimpleNamespace(output=plan)

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )

    async def failing_skill(self, *args, **kwargs):
        del self, args
        kwargs["lifecycle_state"]["context_generation"] = 3
        raise ConnectorError("specialist provider unavailable")

    monkeypatch.setattr(pydantic_ai, "Agent", FakePlannerAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())
    runtime._run_model_skill = MethodType(failing_skill, runtime)

    result = await runtime._run_model_plan(
        runtime._profile("api"),
        "Inspect EID 9",
        {"eventId": 9},
        TaskEventWriter(tmp_path / "events", task_id="legacy-partial-model"),
        None,
        "C:/capture.rdc",
        "qrenderdoc-1",
    )

    assert result.workflow is not None
    assert result.workflow.strategy == runtime.orchestration_strategy.name
    assert result.workflow.terminal_status == "blocked"
    assert result.synthesis is None
    assert result.workflow.stages[0].sessions[0].status == "failed"
    assert result.workflow.stages[0].sessions[0].context_generation == 3


@pytest.mark.asyncio
async def test_uncertain_specialist_cleanup_is_fatal_and_never_gets_released_receipt(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

    catalog = SkillCatalog([SKILL_ROOT]).load()
    task = PlannedTask(
        id="task-1",
        summary="Explain event",
        skill="renderdoc-explain-event",
    )
    proposal = InitialSupervisorProposal(
        goal=GoalSpec(objective="Inspect event"),
        stage_objective="Collect event evidence",
        plan=TaskPlan(request_summary="Inspect event", tasks=[task]),
    )

    class FakePlannerAgent:
        def __init__(self, model, *, output_type, **kwargs):
            del model, output_type, kwargs

        async def run(self, prompt, **kwargs):
            del prompt, kwargs
            return SimpleNamespace(output=proposal)

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(store, catalog, event_root=tmp_path / "events")

    async def uncertain_cleanup(self, *args, **kwargs):
        del self, args, kwargs
        raise LifecycleCleanupError("specialist child cleanup was not confirmed")

    monkeypatch.setattr(pydantic_ai, "Agent", FakePlannerAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())
    runtime._run_model_skill = MethodType(uncertain_cleanup, runtime)

    with pytest.raises(LifecycleCleanupError, match="cleanup was not confirmed"):
        await runtime._run_model_plan(
            runtime._profile("api"),
            "Inspect EID 9",
            {"eventId": 9},
            TaskEventWriter(tmp_path / "events", task_id="fatal-cleanup-model"),
            None,
            "C:/capture.rdc",
            "qrenderdoc-1",
        )


@pytest.mark.asyncio
async def test_model_specialist_keeps_history_until_host_evidence_accepts(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.mcp_tools as mcp_tools

    catalog = SkillCatalog([SKILL_ROOT]).load()
    skill = catalog.get("renderdoc-explain-event")
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            options={"max_specialist_attempts": 2},
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )
    captured = {}

    def fake_toolset(allowed_tools, **kwargs):
        del allowed_tools
        captured["audit"] = kwargs["audit"]
        return object()

    calls: list[dict] = []

    class FakeResult:
        def __init__(self, output):
            self.output = output

        def all_messages(self):
            return ["retained-specialist-history"]

    class FakeAgent:
        def __init__(self, model, **kwargs):
            del model, kwargs

        async def run(self, prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            if len(calls) == 2:
                captured["audit"].completed.extend(
                    ["get_draw_call_details", "get_pipeline_state"]
                )
            return FakeResult(f"attempt {len(calls)}")

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(mcp_tools, "build_mcp_toolset", fake_toolset)
    result = await runtime._run_model_skill(
        runtime._profile("api"),
        "Explain EID 9",
        skill,
        {"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        TaskEventWriter(tmp_path / "events", task_id="specialist-retry"),
        None,
        model=object(),
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )
    assert result.text == "attempt 2"
    assert result.attempts == 2
    assert result.validation_feedback
    assert len(calls) == 2
    assert calls[1]["message_history"] == ["retained-specialist-history"]
    assert "Host validation feedback" in calls[1]["prompt"]
    assert set(result.completed_tools) == {
        "get_draw_call_details",
        "get_pipeline_state",
    }


@pytest.mark.asyncio
async def test_explicit_external_specialist_retries_as_one_logical_session(
    tmp_path,
) -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "external",
            "custom-acp",
            command=["fake-acp"],
            options={
                "allow_unsandboxed_agent": True,
                "max_specialist_attempts": 2,
            },
            probed_capabilities=["tools"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )
    calls: list[dict] = []

    async def fake_external(
        self,
        profile,
        prompt,
        skill_name,
        context,
        writer,
        intent,
        capture_path,
        instance_id,
        *,
        planning_turn=False,
        prior_completed_tools=(),
    ):
        del self, context, intent, capture_path, instance_id, planning_turn
        calls.append(
            {
                "prompt": prompt,
                "prior": prior_completed_tools,
            }
        )
        if len(calls) == 1:
            raise EvidenceValidationError(
                "get_pipeline_state is still required",
                partial_text="first partial answer",
                completed_tools=("get_draw_call_details",),
            )
        return AgentRunResult(
            writer.task_id,
            profile.id,
            profile.provider.title,
            "accepted external answer",
            evidence_tools=("get_draw_call_details", "get_pipeline_state"),
        )

    runtime._run_external = MethodType(fake_external, runtime)
    result = await runtime.run(
        "Explain EID 9",
        profile_id="external",
        skill_name="renderdoc-explain-event",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )
    assert result.text == "accepted external answer"
    assert len(calls) == 2
    assert calls[1]["prior"] == ("get_draw_call_details",)
    assert "first partial answer" in calls[1]["prompt"]
    assert "same logical specialist session" in calls[1]["prompt"]
