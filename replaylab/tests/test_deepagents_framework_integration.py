from __future__ import annotations

import json
import functools
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

import deepagents
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.deep_engine import BreadMCPTools, DeepAgentsEngine, _langchain_tool
from replaylab_agent.profiles import Profile
from replaylab_agent.skill_gateway import SkillGateway


@pytest.fixture(autouse=True)
def completed_general_review_for_framework_regressions(monkeypatch):
    """Keep these Skill/Shader/framework tests independent of generic review policy."""
    from replaylab_agent.task_review import completion_review_state
    def reviewed(store, tracker):
        store.record_task_review("complete", [{"requirement": "Framework regression outcome", "status": "satisfied", "evidence": "Scripted framework outcome"}], [], [])
        return completion_review_state(store, tracker)
    monkeypatch.setattr("replaylab_agent.deep_engine.completion_review_state", reviewed)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_review", [True, False])
async def test_real_api_shader_review_continues_checkpoint_without_replaying_tools(tmp_path, monkeypatch, complete_review):
    row = {"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
           "saved": True, "fresh": True, "review": {}}
    calls = []
    workspace = {}
    original_create = deepagents.create_deep_agent
    @functools.wraps(original_create)
    def create(*args, **kwargs):
        workspace["path"] = kwargs["backend"].cwd
        return original_create(*args, **kwargs)
    monkeypatch.setattr(deepagents, "create_deep_agent", create)

    async def inspect_event(event_id: int) -> str:
        calls.append(event_id)
        if not complete_review:
            (workspace["path"] / "draft.hlsl").write_text("float4 RecoverableColor;", encoding="utf-8")
        if len(calls) == 2 and complete_review:
            row["review"] = {"verdict": "complete"}
        return json.dumps({"eventId": event_id})

    tool = _langchain_tool("inspect_event", inspect_event, description="Inspect an event.",
        args_schema={"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]})

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": tool}

    model = ToolCallingFakeModel(responses=[
        _message("inspect_event", {"event_id": 1}, "before-review"), AIMessage(content="Organization done."),
        _message("inspect_event", {"event_id": 2}, "during-review"), AIMessage(content="Reviewed conclusion."),
    ])
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", lambda store: {"targets": [row]})
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Organize the Shader", task_id="api-review", instance_id="offline",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert calls == [1, 2], result.error
    assert result.terminal_status == ("completed" if complete_review else "partial"), result.error
    final_context = model.seen_messages[-1]
    assert sum(isinstance(message, ToolMessage) and message.tool_call_id == "before-review" for message in final_context) == 1
    assert any(isinstance(message, HumanMessage) and "explicit Agent self-review" in str(message.content) for message in final_context)
    if complete_review:
        assert result.text == "Reviewed conclusion."
    else:
        assert "no new progress" in result.text
        assert "draft recovery" in result.text
        assert list((tmp_path / "turns" / "recovered-shader-drafts").glob("*/manifest.json"))
    events = [json.loads(line) for line in (tmp_path / "events" / "api-review.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = [event for event in events if event["kind"] == "completed"]
    final = [event for event in events if event["kind"] == "message-delta" and event["data"].get("stream") == "final"]
    assert len(terminal) == 1 and sum(event["data"].get("reset") is True for event in final) == 1
    assert "".join(event["text"] for event in final) == result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher", [False, True])
async def test_real_api_complete_shader_file_survives_framework_eviction(tmp_path, monkeypatch, dispatcher):
    import hashlib
    source = "float4 MeaningfulColor;\n" * 5000
    raw = source.encode("utf-8")
    text = json.dumps({"schema": "renderdoc-ai-shader-workspace-file/v1", "mode": "complete",
        "offset": 0, "text": source, "chars": len(source), "totalChars": len(source), "sizeBytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True},
        "truncated": False, "applied": False})

    calls = []
    async def read_shader_workspace_file(workspace_path: str, mode: str) -> str:
        calls.append(mode)
        return text if mode == "complete" else "Small independent observation"

    tool = _langchain_tool("read_shader_workspace_file", read_shader_workspace_file, description="Read Shader source.",
        args_schema={"type": "object", "properties": {"workspace_path": {"type": "string"}, "mode": {"type": "string"}},
                     "required": ["workspace_path", "mode"]})

    @asynccontextmanager
    async def session(*args):
        yield {"read_shader_workspace_file": tool}

    args = {"workspace_path": "shader", "mode": "complete"}
    responses = [
        _message("invoke_bread_tool" if dispatcher else "read_shader_workspace_file",
                 {"name": "read_shader_workspace_file", "arguments": args} if dispatcher else args, "complete-source"),
    ]
    if dispatcher:
        responses.extend(_message("read_shader_workspace_file", {"workspace_path": "shader", "mode": "range"}, f"small-{index}") for index in range(24))
    responses.extend([AIMessage(content="Read the entire source."), AIMessage(content="The earlier complete source remains available.")])
    review_reads = []
    def review_state(store):
        review_reads.append(True)
        return {"targets": [{"workspacePath": "shader", "changed": True, "saved": True, "fresh": True,
                             "review": {} if len(review_reads) == 1 else {"verdict": "complete"}}]}
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", review_state)
    model = ToolCallingFakeModel(responses=responses)
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Read this Shader source", task_id="fullfile-api", instance_id="offline",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "completed", result.error
    message = next(item for item in model.seen_messages[-1] if isinstance(item, ToolMessage) and item.tool_call_id == "complete-source")
    assert json.loads(message.content)["text"] == source
    assert calls.count("complete") == 1
    assert len(calls) == (25 if dispatcher else 1)
    assert len(review_reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_error", [False, True])
@pytest.mark.parametrize("review_complete", [False, True])
async def test_api_review_never_appends_turn_to_unsettled_checkpoint(tmp_path, monkeypatch, checkpoint_error, review_complete):
    from types import SimpleNamespace
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def create(*args, **kwargs):
        graph = original_create(*args, **kwargs)

        class UnsettledGraph:
            def astream_events(self, *args, **kwargs):
                return graph.astream_events(*args, **kwargs)

            async def aget_state(self, config):
                if checkpoint_error:
                    raise OSError("checkpoint unavailable")
                return SimpleNamespace(next=("tools",))

        return UnsettledGraph()

    monkeypatch.setattr(deepagents, "create_deep_agent", create)
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", lambda store: {"targets": [{"workspacePath": "shader",
        "changed": True, "saved": True, "fresh": True, "review": {"verdict": "complete"} if review_complete else {}}]})
    model = ToolCallingFakeModel(responses=[AIMessage(content="Done")])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Organize the Shader", task_id="unsafe-checkpoint",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "partial", result.error
    assert "Some operations are still running" in result.text
    assert len(model.seen_messages) == 1


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Scripted local model with the tool-binding surface deepagents requires."""

    bound_tool_names: list[tuple[str, ...]] = Field(default_factory=list)
    seen_messages: list[list[Any]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ToolCallingFakeModel":
        del tool_choice, kwargs
        self.bound_tool_names.append(tuple(
            tool.get("name", "") if isinstance(tool, dict) else tool.name
            for tool in tools
        ))
        return self

    def _generate(self, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


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
async def test_real_deepagents_runs_skill_subagent_host_tool_without_report_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPLAYLAB_VERIFY_MAX_ROUNDS", "1")
    inspected: list[int] = []
    loaded_skills: list[str] = []
    original_load = SkillGateway.load

    def record_load(self: SkillGateway, skill_id: str, section: str = "", offset: int = 0):
        loaded_skills.append(skill_id)
        return original_load(self, skill_id, section, offset)

    monkeypatch.setattr(SkillGateway, "load", record_load)
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

    model = ToolCallingFakeModel(
        responses=[
            _message(
                "load_bread_skill",
                {"skill_id": "renderdoc-explain-event", "reason": "Explain the selected event."},
                "skill-1",
            ),
            _message(
                "task",
                {
                    "description": "Inspect EID 42 and report its exact event name.",
                    "subagent_type": "general-purpose",
                },
                "task-1",
            ),
            _message(
                "list_bread_capabilities",
                {"tool": "inspect_event"},
                "inspect-contract-1",
            ),
            _message(
                "invoke_bread_tool",
                {"name": "inspect_event", "arguments": {"event_id": 42}},
                "inspect-1",
            ),
            AIMessage(content="inspect_event returned eventId=42, name=DrawIndexed.\nSTATUS: DONE"),
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
    assert loaded_skills == ["renderdoc-explain-event"]
    assert result.terminal_status == "completed"
    assert len(result.children) == 1
    assert result.children[0].agent_id == "general-purpose"
    assert result.children[0].tools_used == ["inspect_event"]
    assert result.children[0].status == "completed"


@pytest.mark.asyncio
async def test_real_api_child_reloads_run_loaded_skill_missing_own_context(tmp_path, monkeypatch):
    """Default isolated children receive their description, not the parent's Skill text."""

    from replaylab_agent.agent_prompts import SKILL_TASK_GUIDANCE

    catalog = _catalog(tmp_path)
    skill_id = "renderdoc-explain-event"
    full_entry = catalog.skill(skill_id).instructions.strip()
    description = "Review EID 42 using the applicable event Skill."
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)

    @asynccontextmanager
    async def offline_tool_session(*_args):
        yield {}

    model = ToolCallingFakeModel(responses=[
        _message("load_bread_skill", {"skill_id": skill_id, "reason": "Prepare event analysis."}, "main-skill"),
        _message("task", {"description": description, "subagent_type": "general-purpose"}, "delegate"),
        _message("list_bread_capabilities", {}, "child-directory"),
        _message("load_bread_skill", {"skill_id": skill_id, "reason": "My context has no Skill body."}, "child-skill"),
        AIMessage(content="I received the applicable Skill in my own context."),
        AIMessage(content="The delegated review received its own Skill instructions."),
    ])
    engine = DeepAgentsEngine(
        catalog=catalog,
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "workspaces", log_root=tmp_path / "logs"),
        deepagents_module=deepagents,
        tool_session_factory=offline_tool_session,
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(
        prompt="Review EID 42.", task_id="isolated-child-skill-context", instance_id="offline-instance",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY"),
    ))
    assert result.terminal_status == "completed", result.error
    assert len(result.children) == 1
    child_spec = created[0]["subagents"][0]
    assert child_spec.get("mode") != "fork"
    assert full_entry not in child_spec["system_prompt"]
    assert SKILL_TASK_GUIDANCE in created[0]["system_prompt"]
    assert SKILL_TASK_GUIDANCE in child_spec["system_prompt"]
    assert "your own current context" in child_spec["system_prompt"]

    child_batches = [messages for messages in model.seen_messages
                     if any(isinstance(message, HumanMessage) and message.content == description for message in messages)]
    assert child_batches
    assert not any(full_entry in str(message.content) for message in child_batches[0])
    assert not any(isinstance(message, ToolMessage) and message.tool_call_id == "main-skill"
                   for messages in child_batches for message in messages)
    child_messages = {message.tool_call_id: message for messages in child_batches for message in messages
                      if isinstance(message, ToolMessage)}
    directory = json.loads(child_messages["child-directory"].content)
    assert directory["loadedSkills"] == [skill_id]
    assert next(row for row in directory["skills"] if row["id"] == skill_id)["loaded"] is True
    assert "instructions" not in directory
    assert full_entry not in child_messages["child-directory"].content
    child_loaded = json.loads(child_messages["child-skill"].content)
    assert child_loaded["alreadyLoaded"] is True
    assert child_loaded["instructions"] == full_entry
    assert child_loaded["readReceipt"]["entryComplete"] is True
    main_loaded = next(message for messages in model.seen_messages for message in messages
                       if isinstance(message, ToolMessage) and message.tool_call_id == "main-skill")
    assert json.loads(main_loaded.content)["alreadyLoaded"] is False
    assert json.loads(main_loaded.content)["readReceipt"]["instructionsSha256"] == child_loaded["readReceipt"]["instructionsSha256"]
    assert TaskReportStore("isolated-child-skill-context", tmp_path / "events").tools_used() == ()


@pytest.mark.asyncio
async def test_real_capture_free_general_child_has_no_preloaded_skill_or_main_tools(tmp_path, monkeypatch):
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
    assert "Standing rules" not in spec.get("system_prompt", spec.get("prompt", ""))
    assert result.children[0].tools_used == []
    assert TaskReportStore(request.task_id, tmp_path / "events").tools_used() == ()


def _host_file_helpers():
    """Load the actual Host file admission implementation without its runtime."""

    path = Path(__file__).resolve().parents[1] / "mcp_host" / "src" / "agent_workspace_files.py"
    spec = importlib.util.spec_from_file_location("offline_host_agent_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_api_framework_edits_host_copy_and_imports_same_physical_root(tmp_path, monkeypatch):
    helpers = _host_file_helpers()
    original = "float4 Shade(float3 normalWS) { return float4(normalWS, 1); }\n"
    revised = "float4 Shade(float3 normalWS) { return float4(normalWS * 0.5 + 0.5, 1); }\n"
    user_source = tmp_path / "user-project" / "source" / "shader.hlsl"
    user_source.parent.mkdir(parents=True)
    user_source.write_text(original, encoding="utf-8")
    delivered = tmp_path / "host-saved-source.hlsl"
    observed = {}
    model = ToolCallingFakeModel(responses=[])
    # This fixture uses the real turn workspace lifecycle and Host file helper,
    # without starting a RenderDoc process or granting access to user roots.
    monkeypatch.setattr("replaylab_agent.deep_engine.build_native_mcp_route", lambda *_a, **_k: None)

    class Session(BreadMCPTools):
        async def __aenter__(self):
            await super().__aenter__()
            assert self.workspace_root is not None
            observed["root"] = self.workspace_root
            monkeypatch.setenv(helpers.AGENT_WORKSPACE_ROOT_ENV, str(self.workspace_root))
            agent_file = helpers.copy_text_to_agent_workspace("proposal.hlsl", user_source.read_bytes())
            observed["file"] = agent_file

            async def read_shader_workspace_file(workspace_path: str, relative_path: str) -> str:
                """Export a managed Shader source into the current Agent directory."""
                assert workspace_path == "managed-workspace"
                assert relative_path == "source/shader.hlsl"
                return json.dumps({"agentFile": agent_file, "applied": False})

            async def write_shader_workspace_organization_file(organization_id: str, source_file: str) -> str:
                """Import an Agent-owned proposal through Host physical file admission."""
                assert organization_id == "test-organization"
                assert Path(source_file).parent == self.workspace_root
                text, receipt = helpers.read_agent_text_file(source_file)
                assert text == revised
                delivered.write_text(text, encoding="utf-8")
                observed["import"] = receipt
                return json.dumps({"sourceSha256": receipt["sha256"], "saved": True, "applied": False})

            model.responses = [
                _message("read_shader_workspace_file", {"workspace_path": "managed-workspace", "relative_path": "source/shader.hlsl"}, "export"),
                _message("task", {"description": "Read and revise /proposal.hlsl; report the edit.", "subagent_type": "general-purpose"}, "edit-child"),
                _message("read_file", {"file_path": agent_file["virtualPath"]}, "child-read"),
                _message("edit_file", {"file_path": agent_file["virtualPath"], "old_string": original.strip(), "new_string": revised.strip()}, "child-edit"),
                AIMessage(content="Working proposal revised. Host import remains required.\nSTATUS: DONE"),
                _message("read_file", {"file_path": agent_file["virtualPath"]}, "main-read"),
                _message("write_shader_workspace_organization_file", {"organization_id": "test-organization", "source_file": agent_file["path"]}, "import"),
                AIMessage(content="Host saved the revised Source; replay was not applied."),
            ]
            return {function.__name__: _langchain_tool(function.__name__, function)
                    for function in (read_shader_workspace_file, write_shader_workspace_organization_file)}

    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        tool_session_factory=lambda request, config, event_root: Session(request, config, event_root),
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(
        prompt="Reorganize this Shader.", task_id="physical-api-import", instance_id="offline-instance",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY"),
    ))
    assert result.terminal_status == "completed", result.error
    assert delivered.read_text(encoding="utf-8") == revised
    assert user_source.read_text(encoding="utf-8") == original
    assert observed["import"]["path"] == observed["file"]["path"]
    assert not observed["root"].parent.exists()
    tool_messages = {message.tool_call_id: message for messages in model.seen_messages
                     for message in messages if isinstance(message, ToolMessage)}
    assert original.strip() in str(tool_messages["child-read"].content)
    assert revised.strip() in str(tool_messages["main-read"].content)
    assert all("execute" not in names for names in model.bound_tool_names)


@pytest.mark.asyncio
async def test_capture_free_api_file_tools_confine_reads_and_writes_to_owned_root(tmp_path, monkeypatch):
    outside = tmp_path / "user-source.hlsl"
    outside.write_text("PRIVATE USER SOURCE", encoding="utf-8")
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)
    model = ToolCallingFakeModel(responses=[
        _message("read_file", {"file_path": str(outside)}, "outside-read"),
        _message("edit_file", {"file_path": "../user-source.hlsl", "old_string": "PRIVATE", "new_string": "CHANGED"}, "escape-edit"),
        _message("write_file", {"file_path": "../escaped.txt", "content": "escape"}, "escape-write"),
        _message("write_file", {"file_path": "/notes/owned.txt", "content": "OWNED PHYSICAL FILE"}, "owned-write"),
        _message("read_file", {"file_path": "/notes/owned.txt"}, "owned-read"),
        AIMessage(content="Only the temporary working file was created."),
    ])
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(prompt="Make a temporary note.", task_id="confined-api-files",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "completed", result.error
    messages = {message.tool_call_id: message for batch in model.seen_messages
                for message in batch if isinstance(message, ToolMessage)}
    assert "PRIVATE USER SOURCE" not in str(messages["outside-read"].content)
    assert messages["outside-read"].status == "error"
    assert messages["escape-edit"].status == "error"
    assert messages["escape-write"].status == "error"
    assert "OWNED PHYSICAL FILE" in str(messages["owned-read"].content)
    assert outside.read_text(encoding="utf-8") == "PRIVATE USER SOURCE"
    assert not list(tmp_path.rglob("escaped.txt"))
    backend = created[0]["backend"]
    assert backend.virtual_mode is True
    assert backend.cwd.parent.parent == (tmp_path / "turns").resolve()
    assert (backend.cwd / "notes" / "owned.txt").read_text(encoding="utf-8") == "OWNED PHYSICAL FILE"
    assert "Local task outputs are retained" in result.text
    assert all("execute" not in names for names in model.bound_tool_names)
