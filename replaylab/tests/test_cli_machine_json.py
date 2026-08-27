from __future__ import annotations

import json
from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest

from replaylab_agent.planning import PlannedTask, TaskPlan
from replaylab_agent.profiles import Profile, ProfileStore
from replaylab_agent.runtime import AgentRuntime


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = BytesIO()


class _BinaryStderr:
    def __init__(self) -> None:
        self.buffer = BytesIO()


def _result(text: str, *, plan=None, workflow=None):
    return SimpleNamespace(
        task_id="task-json",
        profile="local",
        executor="RenderDoc Agent",
        text=text,
        plan=plan,
        workflow=workflow,
    )


@pytest.mark.asyncio
async def test_run_json_is_one_compact_utf8_record(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, prompt, **kwargs):
            assert prompt == "解释事件"
            return _result("中文回答 ✓")

    output = _BinaryStdout()
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.sys, "stdout", output)
    args = SimpleNamespace(
        mcp_host=None,
        context_file=None,
        live_context=False,
        instance_id="",
        prompt="解释事件",
        prompt_stdin=False,
        profile="local",
        skill="",
        task_id="task-json",
        json=True,
    )

    assert await cli._run_once(args, store) == 0
    encoded = output.buffer.getvalue()
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert b'\n  "' not in encoded
    assert json.loads(encoded.decode("utf-8"))["text"] == "中文回答 ✓"


@pytest.mark.asyncio
async def test_work_once_json_is_one_compact_utf8_record(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_one_intent(self, **kwargs):
            assert kwargs == {"profile_id": "local", "instance_id": "instance-a"}
            return _result("任务完成")

    output = _BinaryStdout()
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.sys, "stdout", output)
    args = SimpleNamespace(
        mcp_host=None,
        profile="local",
        instance_id="instance-a",
        json=True,
    )

    assert await cli._work_once(args, store) == 0
    encoded = output.buffer.getvalue()
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert b'\n  "' not in encoded
    assert json.loads(encoded.decode("utf-8"))["text"] == "任务完成"


def test_compact_result_envelope_fits_eight_mib_for_legal_worst_case_text() -> None:
    import replaylab_agent.cli as cli

    # JSON uses six ASCII bytes for each embedded NUL ("\\u0000"), making this a worse
    # machine-envelope case than a 4-byte UTF-8 code point at the same one-million-char limit.
    text = "\0" * AgentRuntime._EVENT_TEXT_MAX_CHARS
    assert AgentRuntime._validated_result_text(text, "test response") == text
    plan = TaskPlan(
        request_summary="\0" * 800,
        tasks=[
            PlannedTask(
                id=f"task-{index}",
                summary="\0" * 500,
                skill="s" * 64,
            )
            for index in range(1, 4)
        ],
    )

    encoded = cli._machine_json(cli._result_payload(_result(text, plan=plan))) + b"\n"

    assert len(encoded) > 4 * 1024 * 1024
    assert len(encoded) < 8 * 1024 * 1024
    assert json.loads(encoded)["text"] == text


def test_workflow_receipt_is_bounded_and_excludes_specialist_answers() -> None:
    import replaylab_agent.cli as cli

    secret_answer = "specialist answer must not be duplicated" * 1000
    result = SimpleNamespace(
        task_id="task-1",
        skill="renderdoc-explain-event",
        summary="Explain the event",
        answer=secret_answer,
        completed_tools=("get_pipeline_state", "get_draw_call_details"),
        evidence_validated=True,
    )
    session = SimpleNamespace(
        task_id="task-1",
        agent_id="specialist-0123456789abcdef",
        skill="renderdoc-explain-event",
        status="accepted",
        attempts=2,
        validation_feedback=("corrected evidence",),
        failure_reason="",
        context_generation=1,
        released=True,
    )
    workflow = SimpleNamespace(
        terminal_status="completed",
        strategy="adaptive-supervisor-specialists/langgraph-v2",
        supervisor_decisions=2,
        total_tasks=1,
        goal=SimpleNamespace(objective="Explain the selected event"),
        stages=(
            SimpleNamespace(
                stage_id="stage-1",
                objective="Establish event evidence",
                results=(result,),
                sessions=(session,),
            ),
        ),
    )

    payload = cli._result_payload(_result("final answer", workflow=workflow))
    encoded_receipt = cli._machine_json(payload["workflowReceipt"])

    assert payload["terminalStatus"] == "completed"
    assert payload["workflowReceipt"]["schema"] == "replaylab-workflow-receipt/v1"
    assert payload["workflowReceipt"]["stages"][0]["sessions"][0]["released"] is True
    assert secret_answer not in encoded_receipt.decode("utf-8")
    assert len(encoded_receipt) <= 256 * 1024


def test_terminal_error_is_exact_bounded_utf8_even_under_chinese_locale(
    monkeypatch,
) -> None:
    import replaylab_agent.cli as cli

    output = _BinaryStderr()
    monkeypatch.setattr(cli.sys, "stderr", output)
    detail = "replaylab-ai: 无法读取 D:\\抓帧\\白银之城\\4.rdc"

    cli._print_utf8_error(detail)

    encoded = output.buffer.getvalue()
    assert encoded == (detail + "\n").encode("utf-8")
    assert encoded.decode("utf-8").endswith("白银之城\\4.rdc\n")


def test_terminal_error_text_only_fallback_preserves_chinese(monkeypatch) -> None:
    import replaylab_agent.cli as cli

    output = StringIO()
    monkeypatch.setattr(cli.sys, "stderr", output)
    cli._print_utf8_error("连接失败：中文路径")

    assert output.getvalue() == "连接失败：中文路径\n"


def test_machine_json_preserves_chinese_and_survives_malformed_provider_unicode() -> None:
    import replaylab_agent.cli as cli

    encoded = cli._machine_json(
        {"status": "failed", "detail": "中文路径 D:/捕获/测试.rdc " + "\ud800"}
    )

    encoded.decode("utf-8", errors="strict")
    payload = json.loads(encoded)
    assert "D:/捕获/测试.rdc" in payload["detail"]
    assert payload["detail"].endswith("\ud800")
