"""Vendor-native ACP/Codex engine behavior and report integration."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from replaylab_agent.acp_session import PromptResult
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_report import REPORT_TOOL, TaskReportStore
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.native_mcp import NativeMCPRoute, build_native_mcp_route, native_codex_argv
from replaylab_agent.profiles import Profile


SKILL_MD = """---
name: renderdoc-explain-event
description: Analyze one RenderDoc event from capture evidence.
metadata:
  replaylab-category: event-pipeline
---
Use inspect_event and cite exact values.
"""

CORE_MD = """---
name: replaylab-core
description: Standing Host evidence rules.
metadata:
  replaylab-category: core
  replaylab-always: "true"
---
Copy identifiers exactly and distinguish observations from inference.
"""


def _catalog(tmp_path: Path) -> CatalogView:
    skills = tmp_path / "skills"
    for name, content in (
        ("renderdoc-explain-event", SKILL_MD),
        ("replaylab-core", CORE_MD),
    ):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return CatalogView.load(skill_roots=[skills])


def _profile(preset: str = "claude-code-acp") -> Profile:
    options = {"allow_unsandboxed_agent": True} if preset.endswith("-acp") else {}
    return Profile(id="native", preset=preset, title="Native", options=options)


def _context() -> dict[str, Any]:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "capture": {
            "loaded": True,
            "path": "D:/capture.rdc",
            "api": "D3D12",
            "captureSessionId": "a" * 32,
        },
        "events": {"currentEventId": 42, "selectedEventId": 42},
        "action": {"eventId": 42, "name": "DrawIndexed"},
        "shaders": {"bound": []},
        "texture": {"available": False},
    }


def _route(workspace: Path, event_root: Path, task_id: str) -> NativeMCPRoute:
    return NativeMCPRoute(
        command="replaylab-ai",
        args=("-m", "replaylab_agent.mcp_proxy"),
        env={
            "REPLAYLAB_TASK_ID": task_id,
            "REPLAYLAB_TASK_EVENT_ROOT": str(event_root),
        },
        allowed_tools=frozenset({"inspect_event", REPORT_TOOL}),
        effects=frozenset(),
        registry_path=workspace / "registry.json",
    )


def _good_report() -> dict[str, Any]:
    return {
        "summary": "EID 42 was inspected.",
        "status": "completed",
        "findings": [
            {
                "claim": "The draw exists.",
                "kind": "observed",
                "evidence": [{"tool": "inspect_event", "values": "eventId=42"}],
            }
        ],
        "artifacts": [],
        "unknowns": [],
        "next_steps": [],
    }


@pytest.mark.asyncio
async def test_acp_native_agent_reuses_session_for_layer3_followup(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    holder: dict[str, Any] = {"sessions": []}

    def fake_route(context, *, workspace, event_root, allowed_effects):
        return _route(workspace, event_root, context.task_id)

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.prompts: list[str] = []
            self.store = TaskReportStore(
                kwargs["mcp_stdio_env"]["REPLAYLAB_TASK_ID"],
                kwargs["mcp_stdio_env"]["REPLAYLAB_TASK_EVENT_ROOT"],
            )
            holder["sessions"].append(self)

        async def __aenter__(self):
            holder["settings"] = (Path(self.kwargs["cwd"]) / ".claude" / "settings.json").is_file()
            holder["mcp_json"] = (Path(self.kwargs["cwd"]) / ".mcp.json").is_file()
            holder["skill"] = (
                Path(self.kwargs["cwd"])
                / ".claude"
                / "skills"
                / "renderdoc-explain-event"
                / "SKILL.md"
            ).is_file()
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            self.prompts.append(text)
            if len(self.prompts) == 1:
                assert "## Loaded Skill: renderdoc-explain-event" in text
                assert "Use inspect_event and cite exact values." in text
                self.store.record_tool("inspect_event")
                answer = "I inspected EID 42."
            else:
                assert "Host verification" in text
                assert self.store.submit(_good_report())["accepted"] is True
                answer = "EID 42 is confirmed."
            callback = kwargs.get("on_delta")
            if callback:
                callback(answer)
            return PromptResult(answer, [answer], "end_turn", 1)

    monkeypatch.setattr(
        "replaylab_agent.native_engine.build_native_mcp_route", fake_route
    )
    monkeypatch.setenv("REPLAYLAB_VERIFY_MAX_ROUNDS", "2")
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root,
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
            heartbeat_seconds=0.05,
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(
                prompt="解释 EID 42",
                profile=_profile(),
                task_id="native-acp",
                context=_context(),
                instance_id="instance-1",
            )
        )
    finally:
        close_logging()

    assert len(holder["sessions"]) == 1
    assert len(holder["sessions"][0].prompts) == 2
    assert holder["settings"] and holder["mcp_json"] and holder["skill"]
    assert result.core == "vendor-native-agent/v1"
    assert result.terminal_status == "completed"
    assert result.report is not None
    assert result.report["findings"][0]["evidence"][0]["tool"] == "inspect_event"


@pytest.mark.asyncio
async def test_native_restart_keeps_exhausted_report_and_followup_budget(tmp_path, monkeypatch):
    event_root = tmp_path / "events"
    task_id = "native-restart"
    existing = TaskReportStore(task_id, event_root, environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "1", "REPLAYLAB_VERIFY_MAX_ROUNDS": "1"})
    existing.record_result("inspect_event", {"eventId": 42})
    assert existing.request_follow_up(existing.gate.verify(worked=True))
    existing.submit({"summary": ""})
    prompts = []

    class FakeSession:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def prompt(self, text, **kwargs):
            prompts.append(text)
            return PromptResult("Unresolved report.", [], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route",
                        lambda context, *, workspace, event_root, allowed_effects: _route(workspace, event_root, context.task_id))
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
                              config=OrchestratorConfig(event_root=event_root, log_root=tmp_path / "logs", workspace_base=tmp_path / "workspaces"),
                              acp_session_factory=FakeSession)
    try:
        result = await engine.run(RunRequest(prompt="Continue existing task.", profile=_profile(), task_id=task_id, context=_context(), instance_id="instance-1"))
    finally:
        close_logging()
    assert len(prompts) == 1 and result.terminal_status == "partial"
    restored = TaskReportStore(task_id, event_root)
    assert restored.gate.attempts == 1 and restored.gate.follow_ups == 1
    assert restored.tools_used() == ("inspect_event",)


@pytest.mark.asyncio
async def test_acp_elicitation_becomes_panel_clarification(tmp_path, monkeypatch) -> None:
    def fake_route(context, *, workspace, event_root, allowed_effects):
        return _route(workspace, event_root, context.task_id)

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            kwargs["on_elicitation"] if "on_elicitation" in kwargs else None
            self.kwargs["on_elicitation"](
                "Which shader?",
                {
                    "sessionId": "s",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "question_0": {
                                "type": "string",
                                "oneOf": [
                                    {"const": "VS", "title": "Vertex shader"},
                                    {"const": "PS", "title": "Pixel shader"},
                                ],
                            }
                        },
                    },
                },
            )
            return PromptResult("waiting", ["waiting"], "end_turn", 0)

    monkeypatch.setattr(
        "replaylab_agent.native_engine.build_native_mcp_route", fake_route
    )
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events",
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(
                prompt="解释着色器",
                profile=_profile(),
                task_id="native-question",
                context=_context(),
                instance_id="instance-1",
            )
        )
    finally:
        close_logging()
    assert result.terminal_status == "needs-user-input"
    assert result.clarification is not None
    assert [item.label for item in result.clarification.options] == [
        "Vertex shader",
        "Pixel shader",
    ]


@pytest.mark.asyncio
async def test_codex_native_agent_uses_new_thread_for_verification(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    store = TaskReportStore("native-codex", event_root)
    holder: dict[str, Any] = {}

    def fake_route(context, *, workspace, event_root, allowed_effects):
        return _route(workspace, event_root, context.task_id)

    class FakeConnector:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            holder["argv"] = list(argv)
            holder["kwargs"] = kwargs
            holder["connector"] = self
            self.prompts: list[str] = []
            self.closed = False

        async def run(self, prompt: str, **kwargs: Any) -> Any:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                store.record_tool("inspect_event")
                store.record_result("inspect_event", {"eventId": 42, "numIndices": 1200})
                store.record_result("apply_shader_workspace_shared_resource", {
                    "applied": True, "workspacePath": "D:/project/shader-workspace"
                })
                bad = _good_report()
                bad["findings"][0]["evidence"][0]["tool"] = "never_called"
                assert store.submit(bad)["accepted"] is False
                text = "The work finished; details were omitted from this answer."
            else:
                assert "1200" in prompt and "D:/project/shader-workspace" in prompt
                assert "report_task_result" in prompt and "evidence-tool-not-called" in prompt
                assert "Standing rules" in prompt and "Do not repeat completed" in prompt
                restarted = TaskReportStore("native-codex", event_root)
                assert restarted.gate.attempts == 1
                assert restarted.submit(_good_report())["accepted"] is True
                text = "EID 42 confirmed."
            return type("Result", (), {"text": text})()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "replaylab_agent.native_engine.build_native_mcp_route", fake_route
    )
    monkeypatch.setattr(
        "replaylab_agent.native_engine.native_codex_argv",
        lambda argv, route: ["codex", "-c", "mcp_servers.ReplayLab={}", "app-server"],
    )
    monkeypatch.setattr(
        ProviderContext,
        "agent_argv",
        lambda self: ["codex", "app-server"],
    )
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root,
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        codex_connector_factory=FakeConnector,
    )
    try:
        result = await engine.run(
            RunRequest(
                prompt="explain EID 42",
                profile=_profile("codex"),
                task_id="native-codex",
                context=_context(),
                instance_id="instance-1",
            )
        )
    finally:
        close_logging()
    connector = holder["connector"]
    assert len(connector.prompts) == 2
    assert "Host verification" in connector.prompts[1]
    assert connector.closed is True
    assert holder["kwargs"]["expected_mcp_servers"] == frozenset({"ReplayLab"})
    assert result.report is not None


def test_native_route_uses_filtered_proxy_and_codex_hardening(tmp_path, monkeypatch) -> None:
    host = tmp_path / "ReplayLabMCP.exe"
    host.write_bytes(b"host")
    profile = _profile("codex")
    context = ProviderContext(
        profile=profile,
        model="",
        instance_id="instance-1",
        context=_context(),
        intent=None,
        mcp_executable=host,
        task_id="route-task",
    )
    route = build_native_mcp_route(
        context,
        workspace=tmp_path,
        event_root=tmp_path / "events",
        allowed_effects=("workspace",),
    )
    assert route is not None
    assert route.args[:3] == ("-I", "-m", "replaylab_agent.mcp_proxy")
    assert "--guard" not in route.args
    assert "--allow" in route.args and REPORT_TOOL in route.args
    assert "--effect" in route.args and "workspace" in route.args
    assert route.env["REPLAYLAB_TASK_ID"] == "route-task"

    argv = native_codex_argv(["codex", "app-server"], route)
    rendered = " ".join(argv)
    assert "mcp_servers.ReplayLab=" in rendered
    assert "features.shell_tool=false" in rendered
    assert "features.apps=false" in rendered
    assert argv[-1] == "app-server"

    task_server = tomllib.loads(route.codex_config_override())["mcp_servers"]["ReplayLab"]
    assert task_server["command"] == route.command
    assert task_server["args"] == list(route.args)
    assert task_server["env"] == route.env
    assert task_server["required"] is True
    assert task_server["startup_timeout_sec"] == 60
    assert task_server["tool_timeout_sec"] == 3600
    assert task_server["default_tools_approval_mode"] == "approve"
    capture_free = " ".join(native_codex_argv(["codex", "app-server"], None))
    assert "default_tools_approval_mode" not in capture_free
