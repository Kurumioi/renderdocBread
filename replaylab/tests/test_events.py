from __future__ import annotations

import json
import os
import time

import pytest

from replaylab_agent.events import TASK_EVENT_SCHEMA, TaskEventWriter
from replaylab_agent.runtime import AgentRuntime


def test_task_events_are_append_only_jsonl_with_monotonic_sequence(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="task-abc")

    first = writer.emit(
        "running",
        backend="model",
        profile="local",
        summary="解释当前事件",
    )
    second = writer.emit("message-delta", text="证据片段")

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    payloads = [json.loads(line) for line in lines]
    assert first.sequence == 1
    assert second.sequence == 2
    assert [item["sequence"] for item in payloads] == [1, 2]
    assert all(item["schema"] == TASK_EVENT_SCHEMA for item in payloads)
    assert all(item["taskId"] == "task-abc" for item in payloads)
    assert payloads[0]["summary"] == "解释当前事件"
    assert payloads[1]["text"] == "证据片段"
    assert all(isinstance(item["timestampUnixMs"], int) for item in payloads)


def test_task_events_resume_sequence_and_redact_sensitive_data(tmp_path) -> None:
    first = TaskEventWriter(tmp_path, task_id="resume-task")
    first.emit("running", data={"api_key": "must-not-appear", "safe": 1})
    second = TaskEventWriter(tmp_path, task_id="resume-task")
    event = second.emit("completed")

    encoded = second.path.read_text(encoding="utf-8")
    payload = json.loads(encoded.splitlines()[0])
    assert event.sequence == 2
    assert "must-not-appear" not in encoded
    assert payload["data"] == {"api_key": "[REDACTED]", "safe": 1}


def test_task_event_failure_keeps_chinese_and_escapes_malformed_unicode(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="unicode-failure")
    writer.emit(
        "failed",
        summary="无法读取 C:/捕获/测试.rdc " + "\ud800",
        data={"errorType": "UnicodeDecodeError"},
    )

    raw = writer.path.read_bytes()
    raw.decode("utf-8", errors="strict")
    payload = json.loads(raw)
    assert "C:/捕获/测试.rdc" in payload["summary"]
    assert payload["summary"].endswith("\ud800")


def test_task_events_resume_after_a_large_last_record(tmp_path) -> None:
    first = TaskEventWriter(tmp_path, task_id="large-tail")
    first.emit("message-delta", text="证据" * (512 * 1024))

    second = TaskEventWriter(tmp_path, task_id="large-tail")
    terminal = second.emit("completed", summary="done")

    assert terminal.sequence == 2
    payloads = [
        json.loads(line)
        for line in second.path.read_text(encoding="utf-8").splitlines()
    ]
    assert [payload["sequence"] for payload in payloads] == [1, 2]


@pytest.mark.parametrize("damaged_tail", [b'{"schema":', b"{not-json}\n"])
def test_task_events_resume_after_invalid_or_partial_tail(tmp_path, damaged_tail) -> None:
    first = TaskEventWriter(tmp_path, task_id="damaged-tail")
    first.emit("running")
    with first.path.open("ab") as stream:
        stream.write(damaged_tail)

    second = TaskEventWriter(tmp_path, task_id="damaged-tail")
    terminal = second.emit("completed", summary="recovered")

    assert terminal.sequence == 2
    valid_payloads = []
    for line in second.path.read_bytes().splitlines():
        try:
            valid_payloads.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    assert [payload["sequence"] for payload in valid_payloads] == [1, 2]


def test_final_task_event_stream_is_complete_and_each_jsonl_record_is_bounded(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="bounded-final")
    # A control character takes six JSON bytes, so two-plus chunks exercise the largest encoded
    # line shape while remaining cheap enough for a unit test.
    text = "\0" * (AgentRuntime._EVENT_TEXT_CHUNK_CHARS * 2 + 1)

    AgentRuntime._emit_text_chunks(
        writer,
        "local",
        text,
        stream="final",
        reset=True,
    )
    writer.emit("completed", profile="local", summary=text[:500])

    lines = writer.path.read_bytes().splitlines()
    assert all(len(line) < 64 * 1024 for line in lines)
    payloads = [json.loads(line) for line in lines]
    deltas = [payload for payload in payloads if payload["kind"] == "message-delta"]
    assert "".join(payload["text"] for payload in deltas) == text
    assert deltas[0]["data"] == {"stream": "final", "reset": True}
    assert all(payload["data"]["reset"] is False for payload in deltas[1:])
    assert payloads[-1]["kind"] == "completed"
    assert payloads[-1]["summary"] == text[:500]
    assert payloads[-1]["text"] == ""


def test_task_event_writer_prunes_only_stale_safe_jsonl_children(tmp_path) -> None:
    stale = tmp_path / "stale-task.jsonl"
    recent = tmp_path / "recent-task.jsonl"
    unrelated = tmp_path / "notes.txt"
    stale.write_text("old", encoding="utf-8")
    recent.write_text("new", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))

    TaskEventWriter(tmp_path, task_id="current-task")

    assert not stale.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_runtime_prunes_stale_host_isolation_directories(tmp_path) -> None:
    event_root = tmp_path / "ai-tasks"
    parent = tmp_path / "host-isolation"
    stale = parent / ("a" * 32)
    unrelated = parent / "do-not-delete"
    stale.mkdir(parents=True)
    unrelated.mkdir()
    (stale / "tool-audit.jsonl").write_text("old", encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(stale, (old, old))

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.event_root = event_root
    _environment, registry, _audit = runtime._new_host_isolation("instance-1")

    assert not stale.exists()
    assert unrelated.exists()
    assert registry.parent.exists()
