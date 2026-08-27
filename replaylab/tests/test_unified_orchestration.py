from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

from replaylab_agent.errors import ConfigurationError
from replaylab_agent.orchestration import SkillResult
from replaylab_agent.profiles import Profile, ProfileStore
from replaylab_agent.runtime import AgentRunResult, AgentRuntime
from replaylab_agent.events import TaskEventWriter
from replaylab_agent.skills import Skill, SkillCatalog


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
FIXTURES = Path(__file__).parent / "fixtures"


def _store(tmp_path: Path) -> ProfileStore:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "managed",
            "custom-acp",
            model="model-a",
            models=["model-a", "model-b"],
            default_model="model-a",
            command=["fake-acp"],
            options={"allow_unsandboxed_agent": True},
            probed_capabilities=["tools"],
        )
    )
    return store


class _FakeExternalRuntime(AgentRuntime):
    def __init__(self, *args: Any, planner_answers: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.planner_answers = list(planner_answers)
        self.calls: list[dict[str, Any]] = []

    async def _run_external(
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
        self.calls.append(
            {
                "planning": planning_turn,
                "model": profile.model,
                "skill": skill_name,
                "context": context,
                "intent": intent,
                "capture": capture_path,
                "instance": instance_id,
                "prompt": prompt,
                "prior_completed_tools": prior_completed_tools,
            }
        )
        if planning_turn:
            return AgentRunResult(
                writer.task_id,
                profile.id,
                profile.provider.title,
                self.planner_answers.pop(0),
            )
        if not skill_name:
            return AgentRunResult(
                writer.task_id,
                profile.id,
                profile.provider.title,
                "general RenderDoc help",
            )
        skill = self.skills.get(skill_name)
        completed = list(sorted(skill.required_all_tools))
        if skill.required_any_tools and not set(completed).intersection(skill.required_any_tools):
            completed.append(sorted(skill.required_any_tools)[0])
        filler = sorted(skill.allowed_tools)[0]
        while len(completed) < skill.minimum_successful_tools:
            completed.append(filler)
        self._validate_skill_evidence(skill, completed)
        return AgentRunResult(
            writer.task_id,
            profile.id,
            profile.provider.title,
            f"evidence from {skill_name}",
            evidence_tools=tuple(completed),
        )


@pytest.mark.asyncio
async def test_capture_aware_external_chat_uses_zero_tool_plan_then_skill_tasks(
    tmp_path,
) -> None:
    plan = {
        "request_summary": "Explain event and resources",
        "tasks": [
            {
                "id": "task-1",
                "summary": "Explain selected event",
                "skill": "renderdoc-explain-event",
                "depends_on": [],
            },
            {
                "id": "task-2",
                "summary": "Inspect bound resources",
                "skill": "renderdoc-resource-analysis",
                "depends_on": [],
            },
        ],
    }
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=[json.dumps(plan)],
        event_root=tmp_path / "events",
    )
    result = await runtime.run(
        "explain this draw and its resources",
        profile_id="managed",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
        model_override="model-b",
        task_id="unified-external",
    )

    planning_calls = [call for call in runtime.calls if call["planning"]]
    specialist_calls = [call for call in runtime.calls if not call["planning"]]
    assert len(planning_calls) == 1
    assert planning_calls[0]["skill"] == ""
    assert planning_calls[0]["context"] == {}
    assert planning_calls[0]["intent"] is None
    assert planning_calls[0]["capture"] == ""
    assert planning_calls[0]["instance"] == ""
    assert all(call["model"] == "model-b" for call in runtime.calls)
    assert {call["skill"] for call in specialist_calls} == {
        "renderdoc-explain-event",
        "renderdoc-resource-analysis",
    }
    assert result.plan is not None
    assert all(isinstance(item, SkillResult) for item in result.skill_results)
    assert result.synthesis is not None
    assert result.synthesis.evidence_validated is True


@pytest.mark.asyncio
async def test_identity_chat_with_ambient_capture_bypasses_planner_and_evidence_tools(
    tmp_path,
) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=[],
        event_root=tmp_path / "events",
    )
    result = await runtime.run(
        "你是什么AI？",
        profile_id="managed",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
        task_id="identity-general-chat",
    )
    assert result.text == "general RenderDoc help"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["planning"] is False
    assert runtime.calls[0]["skill"] == ""
    assert runtime.calls[0]["context"] == {}
    assert runtime.calls[0]["capture"] == ""
    assert runtime.calls[0]["instance"] == ""


@pytest.mark.asyncio
async def test_native_conversation_envelope_routes_only_final_identity_request_as_general(
    tmp_path,
) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=[],
        event_root=tmp_path / "events",
    )
    envelope = (
        "Continue this RenderDoc AI conversation. Treat the final USER block as the new "
        "request; earlier blocks are conversation history, not tool evidence.\n\n"
        "USER:\nExplain RenderDoc EID 7\n\n"
        "ASSISTANT:\nEarlier answer\n\n"
        "USER:\n你是什么ai"
    )
    result = await runtime.run(
        envelope,
        profile_id="managed",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )
    assert result.text == "general RenderDoc help"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["planning"] is False
    assert runtime.calls[0]["skill"] == ""


def test_arbitrary_user_label_does_not_trigger_native_envelope_extraction() -> None:
    from replaylab_agent.runtime import _is_general_chat_request

    assert _is_general_chat_request("USER:\n你是什么ai") is False


def test_external_tool_failure_summary_never_echoes_upstream_secret() -> None:
    secret = "sk-secret-that-must-not-be-rendered"

    assert (
        AgentRuntime._safe_external_tool_failure(
            f"ConfigurationError: upstream returned bearer {secret}"
        )
        == "tool configuration or upstream execution failed"
    )
    assert secret not in AgentRuntime._safe_external_tool_failure(
        f"UnexpectedVendorError: response contained {secret}"
    )


@pytest.mark.asyncio
async def test_external_chat_rejects_model_outside_profile_picker(tmp_path) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=[],
        event_root=tmp_path / "events",
    )
    with pytest.raises(ConfigurationError, match="not enabled for profile"):
        await runtime.run(
            "explain this draw",
            profile_id="managed",
            context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
            capture_path="C:/capture.rdc",
            instance_id="qrenderdoc-1",
            model_override="unknown-model",
        )
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_explicit_button_skill_bypasses_planner_and_runs_directly(tmp_path) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=[],
        event_root=tmp_path / "events",
    )
    result = await runtime.run(
        "Explain the exact selected event",
        profile_id="managed",
        skill_name="renderdoc-explain-event",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )
    assert result.text == "evidence from renderdoc-explain-event"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["planning"] is False
    assert runtime.calls[0]["skill"] == "renderdoc-explain-event"


@pytest.mark.asyncio
async def test_real_codex_adapter_planning_turn_has_verified_empty_tool_inventory(
    tmp_path, monkeypatch,
) -> None:
    import replaylab_agent.runtime as runtime_module

    fake_argv = [
        sys.executable,
        str(FIXTURES / "fake_codex_app_server.py"),
        "app-server",
    ]
    monkeypatch.setattr(
        runtime_module, "resolved_agent_argv", lambda profile: list(fake_argv)
    )
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "codex-test",
            "codex",
            # Known presets ignore stored command overrides in production.  The
            # resolver seam above supplies the test fixture without weakening
            # that catalog-command invariant.
            command=list(fake_argv),
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    profile = runtime._profile_with_model(runtime._profile("codex-test"), "")
    writer = TaskEventWriter(tmp_path / "events", task_id="zero-tool-plan")
    result = await runtime._run_external(
        profile,
        "Return a strict plan",
        "",
        {},
        writer,
        None,
        "",
        "",
        planning_turn=True,
    )
    assert result.text == "evidence answer"
    assert result.evidence_tools == ()
    # Planning deltas and provider prose stay hidden from the user-facing task stream.
    assert not (tmp_path / "events" / "zero-tool-plan.jsonl").exists()


@pytest.mark.asyncio
async def test_real_codex_adapter_persists_each_public_activity_as_bounded_running_event(
    tmp_path, monkeypatch,
) -> None:
    import replaylab_agent.runtime as runtime_module

    fake_argv = [
        sys.executable,
        str(FIXTURES / "fake_codex_app_server.py"),
        "activity-events",
        "app-server",
    ]
    monkeypatch.setattr(
        runtime_module, "resolved_agent_argv", lambda profile: list(fake_argv)
    )
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("codex-activity", "codex", command=list(fake_argv)))
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    profile = runtime._profile_with_model(runtime._profile("codex-activity"), "")
    writer = TaskEventWriter(tmp_path / "events", task_id="codex-public-activity")

    result = await runtime._run_external(
        profile,
        "Return a strict plan",
        "",
        {},
        writer,
        None,
        "",
        "",
        planning_turn=True,
    )

    assert result.text == "evidence answer"
    payloads = [
        json.loads(line)
        for line in writer.path.read_text(encoding="utf-8").splitlines()
    ]
    activities = [event for event in payloads if event["kind"] == "running"]
    assert activities
    for event in activities:
        assert {
            "activityId",
            "activityKind",
            "activityTitle",
            "activityDetail",
            "status",
        } <= event["data"].keys()
        assert len(event["data"]["activityDetail"]) <= 4096
    assert {event["data"]["activityKind"] for event in activities} >= {
        "plan",
        "reasoning",
        "commandExecution",
        "mcpToolCall",
        "webSearch",
        "imageView",
        "contextCompaction",
    }
    rendered = json.dumps(payloads, ensure_ascii=False)
    assert "Checking the selected draw" in rendered
    assert "RAW_REASONING" not in rendered
    for secret in (
        "summary-secret",
        "cmd-secret",
        "hunter2",
        "env-secret",
        "auth-secret",
        "stream-secret",
        "final-output-secret",
        "mcp-secret",
        "camel-secret-value",
        "mcp-result-secret",
        "plan-secret",
    ):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_external_capture_execution_cannot_bypass_explicit_skill_plan(tmp_path) -> None:
    runtime = AgentRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        event_root=tmp_path / "events",
    )
    profile = runtime._profile_with_model(runtime._profile("managed"), "")
    with pytest.raises(ConfigurationError, match="explicit planned Skill"):
        await runtime._run_external(
            profile,
            "bypass planner",
            "",
            {"eventId": 9},
            TaskEventWriter(tmp_path / "events", task_id="no-union"),
            None,
            "C:/capture.rdc",
            "qrenderdoc-1",
        )


@pytest.mark.asyncio
async def test_external_planner_retries_twice_then_uses_one_safe_fallback(tmp_path) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=["not json", "```json\n{}\n```"],
        event_root=tmp_path / "events",
    )
    result = await runtime.run(
        "反编译当前 shader",
        profile_id="managed",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
        task_id="fallback-external",
    )
    assert len([call for call in runtime.calls if call["planning"]]) == 2
    assert result.plan is not None
    assert [task.skill for task in result.plan.tasks] == ["renderdoc-shader-workbench"]
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "fallback-external.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    accepted_plan = next(
        event for event in payloads if event["kind"] == "planning" and event["data"].get("tasks")
    )
    assert accepted_plan["data"]["fallback"] is True
    assert accepted_plan["data"]["plannerAttempts"] == 2


@pytest.mark.asyncio
async def test_external_fallback_routes_only_the_typed_current_request(tmp_path) -> None:
    runtime = _FakeExternalRuntime(
        _store(tmp_path),
        SkillCatalog([SKILL_ROOT]).load(),
        planner_answers=["not json", "still not json"],
        event_root=tmp_path / "events",
    )
    prompt = json.dumps(
        {
            "schema": "replaylab-conversation/v2",
            "conversationId": "fallback-route",
            "messages": [
                {
                    "id": "old-user",
                    "role": "user",
                    "content": "Please reverse this shader",
                }
            ],
            "request": {"role": "user", "content": "分析当前事件的性能瓶颈"},
            "omittedMessageCount": 0,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = await runtime.run(
        prompt,
        profile_id="managed",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )
    assert result.plan is not None
    assert [task.skill for task in result.plan.tasks] == ["renderdoc-performance"]


@pytest.mark.asyncio
async def test_model_api_plan_uses_same_skill_result_and_reviewer_contract(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

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
        SkillCatalog([SKILL_ROOT]).load(),
        event_root=tmp_path / "events",
    )
    plan = {
        "request_summary": "Explain the selected event",
        "tasks": [
            {
                "id": "task-1",
                "summary": "Explain event evidence",
                "skill": "renderdoc-explain-event",
                "depends_on": [],
            }
        ],
    }

    class FakePlannerAgent:
        def __init__(self, model, *, output_type, **kwargs):
            del model, kwargs
            assert output_type is not str

        async def run(self, prompt, **kwargs):
            del prompt, kwargs
            from replaylab_agent.planning import TaskPlan

            return SimpleNamespace(output=TaskPlan.model_validate(plan))

    async def fake_skill(self, profile, prompt, skill, context, writer, intent, **kwargs):
        del self, profile, prompt, context, writer, intent, kwargs
        assert skill.name == "renderdoc-explain-event"
        return SimpleNamespace(
            text="API evidence answer",
            completed_tools=("get_draw_call_details", "get_pipeline_state"),
        )

    monkeypatch.setattr(pydantic_ai, "Agent", FakePlannerAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())
    runtime._run_model_skill = MethodType(fake_skill, runtime)
    result = await runtime._run_model_plan(
        runtime._profile_with_model(runtime._profile("api"), ""),
        "explain EID 9",
        {"eventId": 9},
        TaskEventWriter(tmp_path / "events", task_id="api-plan"),
        None,
        "C:/capture.rdc",
        "qrenderdoc-1",
    )
    assert result.text == "API evidence answer"
    assert result.plan is not None
    assert len(result.skill_results) == 1
    assert result.skill_results[0].evidence_validated is True
    assert result.synthesis is not None
    assert result.synthesis.strategy == "bounded-supervisor-specialist-reviewer/v1"


@pytest.mark.asyncio
async def test_model_plan_emits_isolated_parallel_progress_and_safe_text_streams(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors
    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
        ThinkingPart,
        ThinkingPartDelta,
    )
    from replaylab_agent.planning import TaskPlan

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            probed_capabilities=["tools", "json-schema"],
        )
    )
    catalog = SkillCatalog([]).load()
    catalog.skills = {
        "stream-one": Skill(
            "stream-one",
            "First streaming specialist",
            tmp_path / "stream-one" / "SKILL.md",
            "Return the first bounded answer.",
            frozenset({"get_pipeline_state"}),
            {
                "replaylab-read-only": "true",
                "replaylab-category": "pipeline",
            },
        ),
        "stream-two": Skill(
            "stream-two",
            "Second streaming specialist",
            tmp_path / "stream-two" / "SKILL.md",
            "Return the second bounded answer.",
            frozenset({"get_pipeline_state"}),
            {
                "replaylab-read-only": "true",
                "replaylab-category": "resource",
            },
        ),
    }
    runtime = AgentRuntime(store, catalog, event_root=tmp_path / "events")
    plan = TaskPlan.model_validate(
        {
            "request_summary": "Run two safe specialists",
            "tasks": [
                {
                    "id": "task-1",
                    "summary": "First bounded check",
                    "skill": "stream-one",
                    "depends_on": [],
                },
                {
                    "id": "task-2",
                    "summary": "Second bounded check",
                    "skill": "stream-two",
                    "depends_on": [],
                },
            ],
        }
    )

    class FakeAgent:
        def __init__(self, model, *, output_type, system_prompt="", **kwargs):
            del model, kwargs
            self.output_type = output_type
            self.system_prompt = system_prompt

        async def run(self, prompt, **kwargs):
            del prompt
            if self.output_type is not str:
                # Structured planner output must never be connected to the user-facing
                # provider text stream.
                assert "event_stream_handler" not in kwargs
                return SimpleNamespace(output=plan)

            handler = kwargs["event_stream_handler"]
            skill_name = "stream-one" if "stream-one" in self.system_prompt else "stream-two"

            async def events():
                yield PartStartEvent(index=0, part=ThinkingPart("hidden provider reasoning"))
                yield PartDeltaEvent(
                    index=0,
                    delta=ThinkingPartDelta(content_delta="hidden reasoning delta"),
                )
                yield PartStartEvent(index=1, part=TextPart(f"{skill_name}: "))
                # Make both specialists overlap so context isolation is exercised.
                await asyncio.sleep(0)
                yield PartDeltaEvent(index=1, delta=TextPartDelta("answer"))

            await handler(None, events())
            return SimpleNamespace(output=f"{skill_name}: answer")

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())

    result = await runtime.run(
        "inspect two independent areas",
        profile_id="api",
        context={"capture": {"path": "C:/capture.rdc"}, "eventId": 9},
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
        task_id="parallel-model-progress",
    )
    assert result.plan is not None
    assert result.plan.can_run_parallel(catalog) is True

    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "parallel-model-progress.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    accepted_plan = next(
        event
        for event in payloads
        if event["kind"] == "planning" and event["data"].get("tasks")
    )
    assert [task["id"] for task in accepted_plan["data"]["tasks"]] == [
        "task-1",
        "task-2",
    ]

    progress = [
        event
        for event in payloads
        if event["kind"] == "running" and event["data"].get("phase")
    ]
    assert [(event["data"]["phase"], event["data"]["status"]) for event in progress[:2]] == [
        ("planner", "started"),
        ("planner", "completed"),
    ]
    assert [(event["data"]["phase"], event["data"]["status"]) for event in progress[-2:]] == [
        ("reviewer", "started"),
        ("reviewer", "completed"),
    ]
    specialist_progress = [
        event for event in progress if event["data"]["phase"] == "specialist"
    ]
    expected_identity = {"task-1": "stream-one", "task-2": "stream-two"}
    assert {
        (event["data"]["planTaskId"], event["data"]["status"])
        for event in specialist_progress
    } == {
        ("task-1", "started"),
        ("task-1", "completed"),
        ("task-2", "started"),
        ("task-2", "completed"),
    }
    for event in progress:
        assert {
            "phase",
            "status",
            "planTaskId",
            "skill",
            "progressCurrent",
            "progressTotal",
            "activitySummary",
            "strategy",
        } <= event["data"].keys()
    for event in specialist_progress:
        assert expected_identity[event["data"]["planTaskId"]] == event["data"]["skill"]

    provider = [
        event
        for event in payloads
        if event["kind"] == "message-delta"
        and event["data"].get("stream") == "provider"
    ]
    assert {event["data"]["planTaskId"] for event in provider} == {"task-1", "task-2"}
    for task_id, skill_name in expected_identity.items():
        text = "".join(
            event["text"]
            for event in provider
            if event["data"]["planTaskId"] == task_id
        )
        assert text == f"{skill_name}: answer"
        assert all(
            event["data"]["skill"] == skill_name
            for event in provider
            if event["data"]["planTaskId"] == task_id
        )
    assert "hidden provider reasoning" not in "".join(
        event["text"] for event in payloads if event["kind"] == "message-delta"
    )

    reviewer_complete_index = max(
        index
        for index, event in enumerate(payloads)
        if event["kind"] == "running"
        and event["data"].get("phase") == "reviewer"
        and event["data"].get("status") == "completed"
    )
    final_index, final = next(
        (index, event)
        for index, event in enumerate(payloads)
        if event["kind"] == "message-delta"
        and event["data"].get("stream") == "final"
    )
    assert final_index > reviewer_complete_index
    assert final["data"] == {"stream": "final", "reset": True}
    assert payloads[-1]["kind"] == "completed"
