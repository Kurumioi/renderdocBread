"""The canonical Host-owned task-report contract and acceptance gate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

from replaylab_agent.agent_report import (
    REPORT_TOOL,
    REPORT_TOOL_SCHEMA,
    ReportGate,
    TaskReportStore,
    max_rounds,
    read_task_report_sidecars,
    validate_report,
)


def _good(**overrides: Any) -> dict[str, Any]:
    report = {
        "summary": "The draw renders 1200 vertices with MainPS.",
        "status": "completed",
        "findings": [
            {
                "claim": "1200 vertices",
                "kind": "observed",
                "evidence": [{"tool": "inspect_event", "values": "numIndices=1200"}],
            },
            {"claim": "likely a skybox pass", "kind": "inferred", "confidence": "low"},
        ],
        "artifacts": [],
        "unknowns": [],
        "next_steps": ["inspect the pixel shader"],
    }
    report.update(overrides)
    return report


def test_schema_errors_name_each_invalid_cell() -> None:
    report, reasons = validate_report({"summary": "", "status": "done"}, tools_used=())

    assert report is None
    assert {item["location"] for item in reasons} >= {"summary", "status"}
    assert all(item["direction"] for item in reasons)


def test_factual_checks_use_real_tools_and_artifact_paths(tmp_path: Path) -> None:
    existing = tmp_path / "shader.hlsl"
    existing.write_text("x", encoding="utf-8")
    report, reasons = validate_report(
        _good(
            findings=[
                {"claim": "a", "kind": "observed"},
                {"claim": "b", "kind": "observed", "evidence": [{"tool": "get_pipeline_state"}]},
                {"claim": "c", "kind": "inferred"},
            ],
            artifacts=[
                {"kind": "shader-workspace", "path": str(existing), "status": "accepted"},
                {"kind": "analysis", "path": str(tmp_path / "missing.md"), "status": "pending"},
            ],
        ),
        tools_used={"inspect_event"},
    )

    assert report is not None
    assert [item["code"] for item in reasons] == [
        "observed-without-evidence",
        "evidence-tool-not-called",
        "inferred-without-confidence",
        "artifact-missing",
        "status-contradicts-artifacts",
    ]
    assert reasons[1]["location"] == "findings[1].evidence[0].tool"
    assert "inspect_event" in reasons[1]["direction"]


def test_report_schema_is_the_single_fixed_form() -> None:
    assert REPORT_TOOL == "report_task_result"
    assert set(REPORT_TOOL_SCHEMA["required"]) == {"summary", "status"}
    assert set(REPORT_TOOL_SCHEMA["properties"]) == {
        "summary",
        "status",
        "findings",
        "artifacts",
        "unknowns",
        "next_steps",
    }


def test_factual_rejections_remain_rejected_after_bounded_rounds() -> None:
    with mock.patch.dict(os.environ, {"REPLAYLAB_REPORT_MAX_ROUNDS": "2"}):
        assert max_rounds() == 2
        gate = ReportGate(tools_used=lambda: set())
        bad = _good(
            findings=[
                {
                    "claim": "x",
                    "kind": "observed",
                    "evidence": [{"tool": "ghost"}],
                }
            ]
        )

        first = gate.submit(bad)
        second = gate.submit(bad)

    assert first["accepted"] is False
    assert first["roundsLeft"] == 1
    assert second["accepted"] is False
    assert second["softAccepted"] is False
    assert second["budgetExhausted"] is True
    assert gate.event_payload() is None
    assert gate.submit(bad)["round"] == 2
    assert gate.terminal_status("completed") == "partial"


def test_schema_failures_never_soft_accept_and_user_input_has_priority() -> None:
    gate = ReportGate(tools_used=lambda: set(), limit=1)

    result = gate.submit({"summary": ""})

    assert result["accepted"] is False
    assert gate.report is None
    assert gate.terminal_status("needs-user-input") == "needs-user-input"


def test_report_status_controls_terminal_status() -> None:
    gate = ReportGate(tools_used=lambda: {"inspect_event"})
    assert gate.submit(_good(status="partial"))["accepted"] is True
    assert gate.terminal_status("completed") == "partial"
    assert gate.terminal_status("needs-user-input") == "needs-user-input"
    assert gate.submit(_good(status="failed"))["accepted"] is True
    assert gate.terminal_status("completed") == "failed"
    missing = ReportGate(tools_used=lambda: {"inspect_event"}, verify_limit=0)
    assert missing.terminal_status("completed", worked=True) == "partial"


def test_task_report_store_persists_one_shared_native_sidecar(tmp_path: Path) -> None:
    store = TaskReportStore("native-task", tmp_path)
    store.record_tool("inspect_event")

    accepted = store.submit(_good())
    report, state = read_task_report_sidecars(tmp_path, "native-task")

    assert accepted["accepted"] is True
    assert report is not None and report["summary"].startswith("The draw")
    assert report["taskId"] == "native-task"
    assert state is not None and state["toolsUsed"] == ["inspect_event"]
    assert state["accepted"] is True
