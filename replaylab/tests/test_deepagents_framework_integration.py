from __future__ import annotations

import json
import functools
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

import deepagents
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.deep_engine import DeepAgentsEngine, _langchain_tool
from replaylab_agent.profiles import Profile


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Scripted local model with the tool-binding surface deepagents requires."""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ToolCallingFakeModel":
        del tools, tool_choice, kwargs
        return self


def _message(tool: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": arguments, "id": call_id}],
    )


def _catalog(tmp_path: Path) -> CatalogView:
    skill = tmp_path / "skills" / "renderdoc-explain-event"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: renderdoc-explain-event\n"
        "description: Inspect one RenderDoc event from Host evidence.\n"
        "metadata:\n"
        "  replaylab-category: event-pipeline\n"
        "---\n\n"
        "Call inspect_event and copy the exact EID into the report.\n",
        encoding="utf-8",
    )
    core = tmp_path / "skills" / "replaylab-core"
    core.mkdir()
    canonical = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "replaylab-core" / "SKILL.md"
    (core / "SKILL.md").write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
    return CatalogView.load(skill_roots=[tmp_path / "skills"])


@pytest.mark.asyncio
async def test_real_deepagents_runs_skill_subagent_host_tool_and_report_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPLAYLAB_VERIFY_MAX_ROUNDS", "0")
    inspected: list[int] = []
    task_store = TaskReportStore("framework-integration", tmp_path / "events")

    async def inspect_event(event_id: int) -> str:
        """Return deterministic event evidence."""

        inspected.append(event_id)
        result = json.dumps({"eventId": event_id, "name": "DrawIndexed"})
        task_store.record_tool("inspect_event")
        task_store.record_result("inspect_event", result)
        return result

    host_tool = _langchain_tool(
        "inspect_event",
        inspect_event,
        description="Inspect one event by EID.",
        args_schema={
            "type": "object",
            "properties": {"event_id": {"type": "integer"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    )

    @asynccontextmanager
    async def tool_session(*_args: Any):
        yield {"inspect_event": host_tool}

    report = {
        "summary": "EID 42 is a DrawIndexed event.",
        "status": "completed",
        "findings": [
            {
                "claim": "The selected event is DrawIndexed.",
                "kind": "observed",
                "evidence": [{"tool": "inspect_event", "values": "eventId=42"}],
            }
        ],
        "artifacts": [],
        "unknowns": [],
        "next_steps": [],
    }
    model = ToolCallingFakeModel(
        responses=[
            _message(
                "task",
                {
                    "description": "Inspect EID 42 and report its exact event name.",
                    "subagent_type": "renderdoc-explain-event",
                },
                "task-1",
            ),
            _message("inspect_event", {"event_id": 42}, "inspect-1"),
            AIMessage(content="inspect_event returned eventId=42, name=DrawIndexed.\nSTATUS: DONE"),
            _message("report_task_result", {"status": "invalid", "findings": "wrong-type"}, "invalid-report-1"),
            _message("report_task_result", report, "report-1"),
            AIMessage(content="EID 42 is a DrawIndexed event."),
        ]
    )
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events",
            workspace_base=tmp_path / "workspaces",
            log_root=tmp_path / "logs",
        ),
        deepagents_module=deepagents,
        tool_session_factory=tool_session,
        model_factory=lambda _profile, _model: model,
    )
    request = RunRequest(
        prompt="Explain EID 42.",
        profile=Profile(
            id="offline-deepagents",
            preset="openai",
            model="offline-model",
            api_key_env="UNUSED_OFFLINE_KEY",
        ),
        task_id="framework-integration",
        instance_id="0123456789abcdef0123456789abcdef",
        context={
            "capture": {"loaded": True, "path": "D:/capture.rdc", "api": "D3D12"},
            "events": {"currentEventId": 42, "selectedEventId": 42},
        },
    )

    result = await engine.run(request)

    assert inspected == [42]
    assert result.terminal_status == "completed"
    assert result.report is not None
    assert result.report["schema"] == "replaylab-task-report/v1"
    assert result.report["summary"] == report["summary"]
    assert result.report["findings"][0]["evidence"] == [
        {"tool": "inspect_event", "values": "eventId=42"}
    ]
    assert result.report["softAccepted"] is False
    assert len(result.children) == 1
    assert result.children[0].agent_id == "renderdoc-explain-event"
    assert result.children[0].tools_used == ["inspect_event"]
    assert result.children[0].status == "completed"
    assert TaskReportStore(request.task_id, tmp_path / "events").gate.attempts == 2


@pytest.mark.asyncio
async def test_real_capture_free_general_child_has_core_and_no_main_tools(tmp_path, monkeypatch):
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)
    model = ToolCallingFakeModel(responses=[
        _message("task", {"description": "Explain what a vertex shader does.", "subagent_type": "general-purpose"}, "general-1"),
        AIMessage(content="A vertex shader transforms vertices.\nSTATUS: DONE"),
        AIMessage(content="A vertex shader transforms vertices."),
    ])
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "workspaces", log_root=tmp_path / "logs"),
        deepagents_module=deepagents,
        model_factory=lambda _profile, _model: model,
    )
    request = RunRequest(prompt="Explain vertex shaders.", task_id="no-capture-framework",
                         profile=Profile(id="offline", preset="openai", model="offline-model", api_key_env="UNUSED_OFFLINE_KEY"))
    result = await engine.run(request)
    assert result.terminal_status == "completed"
    assert len(result.children) == 1
    spec = created[0]["subagents"][0]
    assert spec["name"] == "general-purpose"
    assert spec["tools"] == []
    assert "Standing rules: replaylab-core" in spec.get("system_prompt", spec.get("prompt", ""))
    assert result.children[0].tools_used == []
    assert TaskReportStore(request.task_id, tmp_path / "events").tools_used() == ()
