"""Shared layer-3 report follow-ups for both current Agent engines."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_report import REPORT_TOOL, ReportGate, verify_rounds
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.profiles import Profile

from tests.test_deep_engine import (
    CORE_MD,
    SHADER_MD,
    SKILL_MD,
    FakeDeepagents,
    FakeToolSession,
    Msg,
    _context,
    _envelope,
    ev,
)


def _valid_report() -> dict[str, Any]:
    return {
        "summary": "EID 13353 draws 1200 indices.",
        "status": "completed",
        "findings": [
            {
                "claim": "The draw has 1200 indices.",
                "kind": "observed",
                "evidence": [
                    {"tool": "inspect_event", "values": "numIndices=1200"}
                ],
            }
        ],
        "artifacts": [],
        "unknowns": [],
        "next_steps": [],
    }


def _read_events(root: Path, task_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (root / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _child_events(answer: str = "First answer.") -> list[dict[str, Any]]:
    return [
        ev(
            "on_tool_start",
            "task",
            "task-1",
            data={
                "input": {
                    "description": "Inspect EID 13353",
                    "subagent_type": "general-purpose",
                }
            },
        ),
        ev(
            "on_tool_start",
            "inspect_event",
            "tool-1",
            parents=["task-1"],
            data={"input": {"event_id": 13353}},
        ),
        ev(
            "on_tool_end",
            "inspect_event",
            "tool-1",
            parents=["task-1"],
            data={"output": Msg('{"numIndices":1200}')},
        ),
        ev(
            "on_tool_end",
            "task",
            "task-1",
            data={"output": Msg("child result: 1200 indices\nSTATUS: DONE")},
        ),
        ev(
            "on_chat_model_end",
            "model",
            "main-1",
            data={"output": Msg(answer)},
        ),
    ]


class ReportVerificationConfigTests(unittest.TestCase):
    def test_verify_rounds_is_bounded_and_zero_disables_it(self) -> None:
        self.assertEqual(verify_rounds({}), 2)
        self.assertEqual(verify_rounds({"REPLAYLAB_VERIFY_MAX_ROUNDS": "0"}), 0)
        self.assertEqual(verify_rounds({"REPLAYLAB_VERIFY_MAX_ROUNDS": "99"}), 5)
        self.assertEqual(verify_rounds({"REPLAYLAB_VERIFY_MAX_ROUNDS": "invalid"}), 2)

    def test_gate_returns_cell_directions_and_spends_a_bounded_budget(self) -> None:
        gate = ReportGate(tools_used=lambda: {"inspect_event"}, verify_limit=1)
        rejected = _valid_report()
        rejected["findings"][0]["evidence"][0]["tool"] = "never_called"
        verdict = gate.submit(rejected)
        self.assertFalse(verdict["accepted"])

        reasons = gate.verify(worked=True)
        self.assertEqual(reasons[0]["code"], "report-rejected")
        self.assertIn("findings[0].evidence[0].tool", reasons[0]["direction"])
        self.assertTrue(gate.request_follow_up(reasons))
        self.assertFalse(gate.request_follow_up(reasons))


class DeepVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        skills = root / "skills"
        for name, contents in (
            ("renderdoc-explain-event", SKILL_MD),
            ("renderdoc-shader-workbench", SHADER_MD),
            ("replaylab-core", CORE_MD),
        ):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(contents, encoding="utf-8")
        self.catalog = CatalogView.load(skill_roots=[skills])
        self.events_root = root / "ai-tasks"
        self.config = OrchestratorConfig(
            event_root=self.events_root,
            log_root=root / "logs",
            heartbeat_seconds=0.05,
        )
        self.tool_holder: dict[str, Any] = {}

    def tearDown(self) -> None:
        close_logging()
        self.tmp.cleanup()

    def _request(self, task_id: str) -> RunRequest:
        return RunRequest(
            prompt=_envelope("解释当前事件"),
            profile=Profile(id="m", preset="openai", model="gpt-x"),
            task_id=task_id,
            context=_context(),
            instance_id="inst",
        )

    async def _run(
        self, task_id: str, script: Any, *, rounds: int
    ) -> tuple[Any, FakeDeepagents]:
        fake = FakeDeepagents(script)
        engine = DeepAgentsEngine(
            catalog=self.catalog,
            config=self.config,
            deepagents_module=fake,
            tool_session_factory=lambda request, config, event_root: FakeToolSession(self.tool_holder, request, event_root),
            model_factory=lambda profile, model: f"model:{model or profile.model}",
        )
        with mock.patch.dict(
            os.environ, {"REPLAYLAB_VERIFY_MAX_ROUNDS": str(rounds)}
        ):
            result = await engine.run(self._request(task_id))
        return result, fake

    async def test_missing_report_gets_follow_up_and_accepts_without_rerunning_child(
        self,
    ) -> None:
        prompts: list[str] = []
        accepted: list[dict[str, Any]] = []

        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            prompts.append(record["payload"]["messages"][0]["content"])
            if len(prompts) == 1:
                return _child_events()
            report = next(
                tool for tool in record["tools"] if tool.name == REPORT_TOOL
            ).coroutine
            accepted.append(json.loads(await report(**_valid_report())))
            return [
                ev(
                    "on_chat_model_end",
                    "model",
                    "main-2",
                    data={"output": Msg("Form submitted.")},
                )
            ]

        result, _fake = await self._run("t-verify-ok", script, rounds=2)

        self.assertTrue(accepted[0]["accepted"])
        self.assertEqual(len(result.children), 1)
        self.assertEqual(len(prompts), 2)
        self.assertIn("report-missing", prompts[1])
        self.assertIn("child result: 1200 indices", prompts[1])
        self.assertIn("inspect_event", prompts[1])
        final = _read_events(self.events_root, "t-verify-ok")[-1]
        self.assertTrue(final["data"]["verification"]["accepted"])
        self.assertEqual(final["data"]["verification"]["followUps"], 1)
        self.assertEqual(final["data"]["verification"]["reasons"], [])

    async def test_rejected_report_direction_reaches_follow_up(self) -> None:
        prompts: list[str] = []
        rejected: list[dict[str, Any]] = []

        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            prompts.append(record["payload"]["messages"][0]["content"])
            if len(prompts) == 1:
                report = next(
                    tool for tool in record["tools"] if tool.name == REPORT_TOOL
                ).coroutine
                bad = _valid_report()
                bad["findings"][0]["evidence"][0]["tool"] = "never_called"
                rejected.append(json.loads(await report(**bad)))
                return _child_events()
            return [
                ev(
                    "on_chat_model_end",
                    "model",
                    "main-2",
                    data={"output": Msg("No valid form.")},
                )
            ]

        result, _fake = await self._run("t-verify-rejected", script, rounds=1)

        self.assertFalse(rejected[0]["accepted"])
        self.assertIn("report-rejected", prompts[1])
        self.assertIn("findings[0].evidence[0].tool", prompts[1])
        self.assertNotEqual(result.terminal_status, "failed")
        verification = _read_events(
            self.events_root, "t-verify-rejected"
        )[-1]["data"]["verification"]
        self.assertFalse(verification["accepted"])
        self.assertEqual(verification["followUps"], 1)
        self.assertEqual(verification["reasons"][0]["code"], "report-rejected")

    async def test_budget_exhaustion_is_recorded_without_failing_run(self) -> None:
        calls = 0

        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            return (
                _child_events()
                if calls == 1
                else [
                    ev(
                        "on_chat_model_end",
                        "model",
                        "main-2",
                        data={"output": Msg("Still useful.")},
                    )
                ]
            )

        result, _fake = await self._run("t-verify-budget", script, rounds=1)

        self.assertEqual(calls, 2)
        self.assertNotEqual(result.terminal_status, "failed")
        verification = _read_events(self.events_root, "t-verify-budget")[-1][
            "data"
        ]["verification"]
        self.assertEqual(verification["followUps"], 1)
        self.assertFalse(verification["accepted"])
        self.assertEqual(verification["reasons"][0]["code"], "report-missing")

    async def test_zero_rounds_disables_follow_up(self) -> None:
        calls = 0

        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            return _child_events("One-turn answer.")

        result, _fake = await self._run("t-verify-off", script, rounds=0)

        self.assertEqual(calls, 1)
        self.assertNotEqual(result.terminal_status, "failed")
        self.assertNotIn(
            "verification", _read_events(self.events_root, "t-verify-off")[-1]["data"]
        )


if __name__ == "__main__":
    unittest.main()
