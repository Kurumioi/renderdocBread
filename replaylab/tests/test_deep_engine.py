"""DeepAgentsEngine projected through a scripted deepagents event stream."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from replaylab_agent import deep_engine
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.profiles import Profile


SKILL_MD = """---
name: renderdoc-explain-event
description: Analyze one RenderDoc event from capture evidence.
metadata:
  replaylab-category: event-pipeline
---
Use inspect_event and cite exact values.
"""

SHADER_MD = """---
name: renderdoc-shader-workbench
description: Reconstruct one capture-bound shader.
metadata:
  replaylab-category: shader-workbench
---
Use inspect_shader first. Prepare and review the workspace.
"""

CORE_MD = """---
name: replaylab-core
description: Standing Host evidence rules.
metadata:
  replaylab-category: core
  replaylab-always: "true"
---
Copy identifiers exactly. Mark observed and inferred facts. Call report_task_result.
"""


def _context() -> dict[str, Any]:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "capture": {
            "loaded": True,
            "path": "D:/capture.rdc",
            "api": "D3D12",
            "captureSessionId": "a" * 32,
        },
        "events": {"currentEventId": 13353, "selectedEventId": 13353},
        "action": {"eventId": 13353, "name": "DrawIndexed"},
        "shaders": {"bound": []},
        "texture": {"available": False},
    }


def _envelope(text: str, history: list[tuple[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "schema": "replaylab-conversation/v5",
            "messages": [
                {"role": role, "content": content}
                for role, content in (history or [])
            ],
            "request": {"role": "user", "content": text},
        },
        ensure_ascii=False,
    )


class Msg:
    def __init__(self, content: Any, tool_calls: list[Any] | None = None, **extra: Any) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.additional_kwargs = extra


class FakeDeepagents:
    """Record create_deep_agent inputs and replay one scripted event stream."""

    class SubAgent:
        __annotations__ = {
            "name": str,
            "description": str,
            "system_prompt": str,
            "tools": list,
        }

    def __init__(self, script: Any) -> None:
        self.script = script
        self.created: list[dict[str, Any]] = []

    def create_deep_agent(
        self,
        *,
        model: Any,
        tools: list[Any],
        subagents: list[dict[str, Any]],
        system_prompt: str,
    ) -> Any:
        record = {
            "model": model,
            "tools": tools,
            "subagents": subagents,
            "system_prompt": system_prompt,
        }
        self.created.append(record)
        script = self.script

        class Agent:
            async def astream_events(self, payload: Any, *, config: Any, version: str):
                record["payload"] = payload
                record["config"] = config
                record["version"] = version
                for event in await script(record):
                    # A framework event alone is not Host evidence. Execute
                    # the fixture Host tool so its task ledger records reality.
                    if event["event"] == "on_tool_start" and event["name"] != "task":
                        known = {tool.name: tool for subagent in subagents for tool in subagent["tools"]}
                        actual = known.get(event["name"])
                        if actual is not None:
                            await actual.coroutine(**event.get("data", {}).get("input", {}))
                    yield event

        return Agent()


def ev(
    kind: str,
    name: str,
    run_id: str,
    *,
    parents: list[str] = (),
    data: Any = None,
) -> dict[str, Any]:
    return {
        "event": kind,
        "name": name,
        "run_id": run_id,
        "parent_ids": list(parents),
        "data": data or {},
    }


class FakeToolSession:
    def __init__(self, holder: dict[str, Any], request: Any, event_root: Path) -> None:
        self.holder = holder
        self.store = TaskReportStore(request.task_id, event_root)

    async def __aenter__(self) -> dict[str, Any]:
        async def inspect_event(event_id: int) -> str:
            """Inspect one exact RenderDoc event."""
            if event_id < 1:
                return "ERROR: event_id must be positive"
            return json.dumps({"eventId": event_id})

        async def inspect_shader(event_id: int = 1) -> str:
            """Inspect one capture-bound shader."""
            return json.dumps({"eventId": event_id})

        async def prepare_shader_workspace(workspace_path: str = "") -> str:
            """Prepare one shader workspace."""
            return json.dumps({"workspace": workspace_path})

        async def apply_shader_workspace_candidate(workspace_path: str = "") -> str:
            """Apply one reviewed shader workspace."""
            return json.dumps({"workspace": workspace_path})

        def observed(function):
            @functools.wraps(function)
            async def call(**arguments):
                self.store.record_tool(function.__name__)
                result = await function(**arguments)
                self.store.record_result(function.__name__, result)
                return result
            return call

        tools = {
            function.__name__: deep_engine._langchain_tool(function.__name__, observed(function))
            for function in (
                inspect_event,
                inspect_shader,
                prepare_shader_workspace,
                apply_shader_workspace_candidate,
            )
        }
        self.holder["tools"] = tools
        return tools

    async def __aexit__(self, *_args: Any) -> None:
        self.holder["closed"] = True


# unittest's isolated loop keeps the scripted async generators deterministic on Windows.
class DeepEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._verify_patch = mock.patch.dict(
            os.environ, {"REPLAYLAB_VERIFY_MAX_ROUNDS": "0"}
        )
        self._verify_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        skills = root / "skills"
        for name, text in (
            ("renderdoc-explain-event", SKILL_MD),
            ("renderdoc-shader-workbench", SHADER_MD),
            ("replaylab-core", CORE_MD),
        ):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(text, encoding="utf-8")
        self.catalog = CatalogView.load(skill_roots=[skills])
        self.events_root = root / "ai-tasks"
        self.config = OrchestratorConfig(
            event_root=self.events_root,
            log_root=root / "logs",
            heartbeat_seconds=0.05,
        )
        self.tool_holder: dict[str, Any] = {}

    def tearDown(self) -> None:
        self._verify_patch.stop()
        close_logging()
        self.tmp.cleanup()

    def _engine(self, script: Any) -> tuple[DeepAgentsEngine, FakeDeepagents]:
        fake = FakeDeepagents(script)
        engine = DeepAgentsEngine(
            catalog=self.catalog,
            config=self.config,
            deepagents_module=fake,
            tool_session_factory=lambda request, config, event_root: FakeToolSession(self.tool_holder, request, event_root),
            model_factory=lambda profile, model: f"model:{model or profile.model}",
        )
        return engine, fake

    def _request(self, text: str, **kwargs: Any) -> RunRequest:
        return RunRequest(
            prompt=_envelope(text),
            profile=Profile(id="m", preset="openai", model="gpt-x"),
            task_id=kwargs.pop("task_id", "t-deep"),
            context=_context(),
            instance_id="inst",
            **kwargs,
        )

    def test_subagent_effects_preserve_configured_replay_and_respect_ceiling(self) -> None:
        engine, unused_fake = self._engine(None)
        self.assertIn("replay", engine.subagent_effects())
        self.config.allowed_effects = frozenset({"workspace", "project"})
        self.assertEqual(engine.subagent_effects(), ("project", "workspace"))
        self.config.allowed_effects = frozenset()
        self.assertEqual(engine.subagent_effects(), ())

    def _log(self, task_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (self.events_root / f"{task_id}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    async def test_subagents_come_from_skills_with_effect_ceilinged_tools(self) -> None:
        self.config.allowed_effects = frozenset({"workspace", "project", "ui"})
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_chat_model_stream", "m", "r1", data={"chunk": Msg("直接回答")}),
                ev("on_chat_model_end", "m", "r1", data={"output": Msg("直接回答")}),
            ]

        engine, fake = self._engine(script)
        result = await engine.run(self._request("你是什么模型"))

        self.assertEqual(result.terminal_status, "completed")
        created = fake.created[0]
        self.assertEqual(created["model"], "model:gpt-x")
        self.assertEqual([tool.name for tool in created["tools"]], ["ask_user", "report_task_result"])
        names = [item["name"] for item in created["subagents"]]
        self.assertIn("renderdoc-explain-event", names)
        self.assertIn("renderdoc-shader-workbench", names)
        self.assertNotIn("replaylab-core", names)
        self.assertEqual(names[-1], "general-purpose")
        shader = next(item for item in created["subagents"] if item["name"] == "renderdoc-shader-workbench")
        general = created["subagents"][-1]
        self.assertIn("Standing rules: replaylab-core", shader["system_prompt"])
        self.assertIn("Granted effect classes: project, ui, workspace", shader["system_prompt"])
        self.assertIn("READ-ONLY", general["system_prompt"])
        self.assertIn("prepare_shader_workspace", {tool.name for tool in shader["tools"]})
        self.assertNotIn("apply_shader_workspace_candidate", {tool.name for tool in shader["tools"]})
        self.assertEqual({tool.name for tool in general["tools"]}, {"inspect_event", "inspect_shader"})
        self.assertEqual(created["version"], "v2")
        self.assertTrue(self.tool_holder["closed"])

    async def test_no_capture_means_no_mcp_session_or_subagents(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("hello")})]

        engine, fake = self._engine(script)
        request = self._request("hi")
        request.instance_id = ""
        request.context = {}
        result = await engine.run(request)

        self.assertEqual(result.text, "hello")
        general = fake.created[0]["subagents"]
        self.assertEqual([item["name"] for item in general], ["general-purpose"])
        self.assertEqual(general[0]["tools"], [])
        self.assertIn("Standing rules: replaylab-core", general[0]["system_prompt"])
        self.assertNotIn("tools", self.tool_holder)

    async def test_report_checks_child_tools_and_lands_in_terminal_event(self) -> None:
        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            report = next(tool for tool in record["tools"] if tool.name == "report_task_result").coroutine
            record["report"] = report
            return [
                ev("on_tool_start", "task", "task-1", data={"input": {"description": "look", "subagent_type": "general-purpose"}}),
                ev("on_tool_start", "inspect_event", "tool-1", parents=["task-1"], data={"input": {"event_id": 13353}}),
                ev("on_tool_end", "inspect_event", "tool-1", parents=["task-1"], data={"output": "{}"}),
                ev("on_tool_end", "task", "task-1", data={"output": Msg("STATUS: DONE")}),
                ev("on_chat_model_end", "m", "r9", data={"output": Msg("Done")}),
            ]

        engine, fake = self._engine(script)
        original = fake.create_deep_agent

        def create(**kwargs: Any) -> Any:
            agent = original(**kwargs)
            stream = agent.astream_events

            async def wrapped(payload: Any, *, config: Any, version: str):
                async for event in stream(payload, config=config, version=version):
                    yield event
                    if event["event"] == "on_tool_end" and event["name"] == "task":
                        report = fake.created[-1]["report"]
                        rejected = json.loads(
                            await report(
                                summary="Draw inspected.",
                                findings=[{"claim": "x", "kind": "observed", "evidence": [{"tool": "get_draw_calls"}]}],
                            )
                        )
                        accepted = json.loads(
                            await report(
                                summary="Draw inspected.",
                                findings=[{"claim": "x", "kind": "observed", "evidence": [{"tool": "inspect_event", "values": "eventId=13353"}]}],
                            )
                        )
                        fake.created[-1]["report_calls"] = [rejected, accepted]

            agent.astream_events = wrapped
            return agent

        fake.create_deep_agent = create  # type: ignore[method-assign]
        result = await engine.run(self._request("解释事件", task_id="t-report"))

        rejected, accepted = fake.created[0]["report_calls"]
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reasons"][0]["code"], "evidence-tool-not-called")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(result.report["schema"], "replaylab-task-report/v1")
        self.assertEqual(self._log("t-report")[-1]["data"]["report"]["summary"], "Draw inspected.")

    async def test_event_mapping_tracks_children_tools_reasoning_and_final_text(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_tool_start", "task", "task-1", data={"input": {"description": "Inspect", "subagent_type": "renderdoc-explain-event"}}),
                ev("on_chat_model_stream", "m", "r2", parents=["task-1"], data={"chunk": Msg([{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Looking"}])}),
                ev("on_tool_start", "inspect_event", "tool-1", parents=["task-1", "r2"], data={"input": {"event_id": 13353}}),
                ev("on_tool_end", "inspect_event", "tool-1", parents=["task-1", "r2"], data={"output": Msg('{"eventId":13353}')}),
                ev("on_tool_end", "task", "task-1", data={"output": Msg("Findings\nSTATUS: DONE")}),
                ev("on_chat_model_end", "m", "r3", data={"output": Msg("The draw renders 1200 vertices.")}),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("解释事件", task_id="t-child"))

        self.assertEqual(result.text, "The draw renders 1200 vertices.")
        self.assertEqual(len(result.children), 1)
        child = result.children[0]
        self.assertEqual(child.agent_id, "renderdoc-explain-event")
        self.assertEqual(child.tools_used, ["inspect_event"])
        kinds = [item["kind"] for item in self._log("t-child")]
        self.assertIn("reasoning-delta", kinds)
        self.assertIn("tool-started", kinds)
        self.assertIn("tool-completed", kinds)

    async def test_restart_preserves_gate_budget_and_virtual_events_are_not_host_evidence(self) -> None:
        from replaylab_agent.agent_report import TaskReportStore

        store = TaskReportStore("t-restart", self.config.event_root, environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "1", "REPLAYLAB_VERIFY_MAX_ROUNDS": "1"})
        store.record_tool("inspect_event")
        self.assertTrue(store.request_follow_up(store.gate.verify(worked=True)))
        store.submit({"summary": ""})

        async def script(_record):
            return [
                ev("on_tool_start", "task", "child", data={"input": {"description": "Read", "subagent_type": "general-purpose"}}),
                ev("on_tool_start", "read_file", "virtual", parents=["child"], data={"input": {"file_path": "/scratch.txt"}}),
                ev("on_tool_end", "read_file", "virtual", parents=["child"], data={"output": Msg("virtual data")}),
                ev("on_tool_end", "task", "child", data={"output": Msg("STATUS: DONE")}),
                ev("on_chat_model_end", "m", "final", data={"output": Msg("Unresolved report.")}),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("continue", task_id="t-restart"))
        restored = TaskReportStore("t-restart", self.config.event_root)
        self.assertEqual(restored.gate.attempts, 1)
        self.assertEqual(restored.gate.follow_ups, 1)
        self.assertEqual(restored.tools_used(), ("inspect_event",))
        self.assertEqual(result.terminal_status, "partial")

    async def test_ask_user_ends_with_needs_user_input(self) -> None:
        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            ask = next(tool for tool in record["tools"] if tool.name == "ask_user").coroutine
            record["first"] = await ask(
                question="Which shader?",
                options=[{"id": "vs", "label": "MainVS"}, {"id": "ps", "label": "MainPS"}],
            )
            record["second"] = await ask(
                question="Again?",
                options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            )
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("pick")})]

        engine, fake = self._engine(script)
        result = await engine.run(self._request("decompile", task_id="t-ask"))

        self.assertIn("Question recorded", fake.created[0]["first"])
        self.assertTrue(fake.created[0]["second"].startswith("ERROR:"))
        self.assertEqual(result.terminal_status, "needs-user-input")
        self.assertEqual(result.text, "Which shader?")

    async def test_clarification_answer_and_intent_reach_prompt(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("ok")})]

        engine, fake = self._engine(script)
        envelope = json.loads(_envelope("continue", [("user", "decompile"), ("assistant", "Which shader?")]))
        envelope["clarificationResponse"] = {
            "clarificationId": "q-1",
            "optionIds": ["ps"],
            "otherText": "",
        }
        request = self._request(
            "x",
            task_id="t-answer",
            intent={"kind": "analyze-shader", "arguments": {}},
            skill_name="renderdoc-shader-workbench",
        )
        request.prompt = json.dumps(envelope, ensure_ascii=False)
        await engine.run(request)
        prompt = fake.created[0]["payload"]["messages"][0]["content"]
        self.assertIn("replaylab-provider-conversation/v1", prompt)
        self.assertIn("decompile", prompt)
        self.assertIn("selected option(s) = [ps]", prompt)
        self.assertIn("analyze-shader", prompt)

    async def test_framework_failure_and_timeout_are_failed_runs(self) -> None:
        async def fail(_record: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("model refused: invalid api key")

        engine, _fake = self._engine(fail)
        result = await engine.run(self._request("hi", task_id="t-err"))
        self.assertEqual(result.terminal_status, "failed")
        self.assertIn("invalid api key", result.error)

        async def hang(_record: dict[str, Any]) -> list[dict[str, Any]]:
            await asyncio.sleep(5)
            return []

        self.config.main_timeout = 0.05
        engine, _fake = self._engine(hang)
        result = await engine.run(self._request("hi", task_id="t-timeout"))
        self.assertEqual(result.terminal_status, "failed")
        self.assertIn("exceeded", result.error)

    def test_missing_dependency_message_matches_required_runtime(self) -> None:
        with mock.patch.dict(sys.modules, {"deepagents": None}):
            with self.assertRaises(Exception) as raised:
                deep_engine.import_deepagents()
        self.assertIn("required deepagents runtime is missing", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
