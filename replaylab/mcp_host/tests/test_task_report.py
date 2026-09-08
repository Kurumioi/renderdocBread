from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from src.task_report import (
    REPORT_SCHEMA,
    REPORT_STATE_SCHEMA,
    TaskReportStore,
    ReportGate,
    clear_task_report_sidecars,
    instrument_task_tool,
    read_task_report_sidecars,
    task_report_path,
    task_report_state_path,
)


def _report(**overrides):
    value = {
        "summary": "EID 42 uses the expected pipeline.",
        "status": "completed",
        "findings": [
            {
                "claim": "The draw is indexed.",
                "kind": "observed",
                "evidence": [{"tool": "inspect_event", "values": "eventId=42"}],
            }
        ],
        "artifacts": [],
        "unknowns": [],
        "next_steps": [],
    }
    value.update(overrides)
    return value


def test_store_records_tools_and_writes_only_an_accepted_report(tmp_path) -> None:
    store = TaskReportStore("task-1", tmp_path)
    store.record_tool("inspect_event")

    report, state = read_task_report_sidecars(tmp_path, "task-1")
    assert report is None
    assert state is not None
    assert state["schema"] == REPORT_STATE_SCHEMA
    assert state["toolsUsed"] == ["inspect_event"]

    result = store.submit(_report())
    assert result["accepted"] is True
    report, state = read_task_report_sidecars(tmp_path, "task-1")
    assert report is not None and report["schema"] == REPORT_SCHEMA
    assert report["taskId"] == "task-1"
    assert report["findings"][0]["evidence"][0]["tool"] == "inspect_event"
    assert state is not None and state["accepted"] is True


def test_rejected_report_records_reasons_without_report_sidecar(tmp_path) -> None:
    store = TaskReportStore("task-2", tmp_path)
    result = store.submit(_report())
    assert result["accepted"] is False
    assert result["reasons"][0]["code"] == "evidence-tool-not-called"
    assert not task_report_path(tmp_path, "task-2").exists()
    state = read_task_report_sidecars(tmp_path, "task-2")[1]
    assert state is not None
    assert state["rejections"] == 1
    assert state["lastReasons"][0]["location"].endswith(".tool")


def test_third_factual_rejection_is_exhausted_without_false_publication(tmp_path) -> None:
    store = TaskReportStore("task-3", tmp_path, environ={})
    for _ in range(2):
        assert store.submit(_report())["accepted"] is False
    result = store.submit(_report())
    assert result["accepted"] is False
    assert result["softAccepted"] is False
    assert result["budgetExhausted"] is True
    report = read_task_report_sidecars(tmp_path, "task-3")[0]
    assert report is None
    assert store.submit(_report())["round"] == 3


def test_instrumentation_preserves_sync_and_async_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REPLAYLAB_TASK_ID", "task-4")
    monkeypatch.setenv("REPLAYLAB_TASK_EVENT_ROOT", str(tmp_path))

    def inspect_event() -> str:
        return "sync"

    async def get_pipeline_state() -> str:
        return "async"

    assert instrument_task_tool(inspect_event)() == "sync"
    assert asyncio.run(instrument_task_tool(get_pipeline_state)()) == "async"
    state = read_task_report_sidecars(tmp_path, "task-4")[1]
    assert state is not None
    assert state["toolsUsed"] == ["get_pipeline_state", "inspect_event"]


def test_sidecar_paths_are_exact_and_clear_is_task_scoped(tmp_path) -> None:
    store = TaskReportStore("task-5", tmp_path)
    store.record_tool("inspect_event")
    store.submit(_report())
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    assert task_report_path(tmp_path, "task-5").name == "task-5.report.json"
    assert task_report_state_path(tmp_path, "task-5").name == "task-5.report-state.json"
    clear_task_report_sidecars(tmp_path, "task-5")
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not task_report_path(tmp_path, "task-5").exists()
    assert not task_report_state_path(tmp_path, "task-5").exists()
    with pytest.raises(ValueError):
        task_report_path(tmp_path, "../escape")


def test_schema_attempts_survive_store_restart_and_exhaust_without_acceptance(tmp_path):
    first = TaskReportStore("restart", tmp_path, environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "2"})
    first.record_result("inspect_event", {"eventId": 42})
    verdict = first.submit({"summary": "", "status": "not-a-status"})
    assert verdict["round"] == 1 and verdict["reasons"][0]["severity"] == "blocker"
    restarted = TaskReportStore("restart", tmp_path, environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "9"})
    assert restarted.gate.limit == 2 and restarted.tools_used() == ("inspect_event",)
    assert restarted.submit({"summary": ""})["budgetExhausted"] is True
    again = TaskReportStore("restart", tmp_path)
    assert again.submit(_report())["accepted"] is False
    assert again.gate.attempts == 2
    assert read_task_report_sidecars(tmp_path, "restart")[0] is None


def test_warning_is_conservatively_repaired_and_accepted_without_retry(tmp_path):
    store = TaskReportStore("warning", tmp_path)
    verdict = store.submit(_report(findings=[{"claim": "Possibly a lighting pass.", "kind": "inferred"}]))
    assert verdict["accepted"] is True and verdict["softAccepted"] is False
    assert verdict["round"] == 1 and verdict["schema"] == "replaylab-review-verdict/v1"
    assert verdict["reasons"][0]["severity"] == "warning"
    assert all(verdict["reasons"][0][key] for key in ("direction", "target", "example"))
    report, state = read_task_report_sidecars(tmp_path, "warning")
    assert report["findings"][0]["confidence"] == "low"
    assert state["rejections"] == 0
    assert store.gate.terminal_status("completed", worked=True) == "completed"


@pytest.mark.parametrize("failure", ["tool", "path", "status", "schema"])
def test_blockers_never_become_accepted_at_zero_rounds_left(tmp_path, failure):
    artifact = tmp_path / "existing.txt"
    artifact.write_text("actual artifact", encoding="utf-8")
    report = _report()
    if failure == "path":
        report["artifacts"] = [{"path": str(tmp_path / "invented"), "status": "accepted"}]
    elif failure == "status":
        report["artifacts"] = [{"path": str(artifact), "status": "pending"}]
    elif failure == "schema":
        report["summary"] = ""
    gate = ReportGate(tools_used=lambda: () if failure == "tool" else ("inspect_event",), limit=1)
    verdict = gate.submit(report)
    assert verdict["accepted"] is False and verdict["softAccepted"] is False
    assert verdict["budgetExhausted"] is True and verdict["roundsLeft"] == 0
    assert "Stop" in verdict["nextStep"]
    assert gate.request_follow_up(gate.verify(worked=True)) is False


def test_host_facts_are_bounded_redacted_and_merge_across_stores(tmp_path):
    first = TaskReportStore("facts", tmp_path)
    second = TaskReportStore("facts", tmp_path)
    for i in range(35):
        (first if i % 2 else second).record_result(f"tool_{i}", {
            "eventId": i, "access_token": "NEVER-EXPOSE-ME", "hlsl": "x" * 50000,
            "workspacePath": "D:/project/workspace", "status": "pending",
        })
    restarted = TaskReportStore("facts", tmp_path)
    assert len(restarted.tools_used()) == 35
    state = read_task_report_sidecars(tmp_path, "facts")[1]
    assert len(state["toolFacts"]) == 24
    assert all(len(item["summary"]) <= 1800 for item in state["toolFacts"])
    assert "NEVER-EXPOSE-ME" not in task_report_state_path(tmp_path, "facts").read_text(encoding="utf-8")
    assert "D:/project/workspace" in state["toolFacts"][-1]["summary"]


def test_corrupt_sidecar_cannot_reset_review_budget(tmp_path):
    TaskReportStore("corrupt", tmp_path).submit({"summary": ""})
    task_report_state_path(tmp_path, "corrupt").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to reset"):
        TaskReportStore("corrupt", tmp_path)


def test_existing_unobserved_artifact_is_not_accepted_as_host_output(tmp_path):
    path = tmp_path / "unrelated.txt"
    path.write_text("existing but unrelated", encoding="utf-8")
    store = TaskReportStore("artifact-provenance", tmp_path)
    store.record_tool("inspect_event")
    form = _report(artifacts=[{"path": str(path), "status": "accepted"}])
    rejected = store.submit(form)
    assert rejected["reasons"][0]["code"] == "artifact-not-observed"
    store.record_result("export_event", {"artifactPath": str(path)})
    assert store.submit(form)["accepted"] is True


def test_follow_up_budget_survives_restart_and_state_owns_report(tmp_path):
    first = TaskReportStore("followup", tmp_path, environ={"REPLAYLAB_VERIFY_MAX_ROUNDS": "1"})
    first.record_tool("inspect_event")
    assert first.request_follow_up(first.gate.verify(worked=True)) is True
    restarted = TaskReportStore("followup", tmp_path, environ={"REPLAYLAB_VERIFY_MAX_ROUNDS": "5"})
    assert restarted.gate.follow_ups == 1 and restarted.gate.verify_limit == 1
    assert restarted.request_follow_up(restarted.gate.verify(worked=True)) is False
    assert restarted.submit(_report())["accepted"] is True
    task_report_path(tmp_path, "followup").write_text("interrupted mirror update", encoding="utf-8")
    again = TaskReportStore("followup", tmp_path)
    assert again.gate.report is not None and again.gate.attempts == 1
    assert again.gate.terminal_status("failed", worked=True) == "failed"
    assert ReportGate(tools_used=lambda: ()).terminal_status("failed", worked=True) == "failed"


def test_free_text_fact_credentials_are_redacted(tmp_path):
    store = TaskReportStore("redaction", tmp_path)
    store.record_result("inspect_event", {"error": "https://api/x?token=URLSECRET&n=3 api_key=KEYSECRET password='PASS SECRET'"}, failed=True)
    state_text = task_report_state_path(tmp_path, "redaction").read_text(encoding="utf-8")
    assert all(secret not in state_text for secret in ("URLSECRET", "KEYSECRET", "PASS SECRET"))


def test_concurrent_host_processes_preserve_facts_and_one_bounded_budget(tmp_path):
    script = """
import sys
from src.task_report import TaskReportStore
store = TaskReportStore('concurrent', sys.argv[1], environ={'REPLAYLAB_REVIEW_MAX_ROUNDS': '3'})
for index in range(12):
    store.record_result(sys.argv[2] + str(index), {'eventId': index})
    store.submit({'summary': ''})
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), prefix],
                                 cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for prefix in ("first_", "second_")]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
    final = TaskReportStore("concurrent", tmp_path)
    assert len(final.tools_used()) == 24
    assert final.gate.attempts == 3 and final.gate.report is None
