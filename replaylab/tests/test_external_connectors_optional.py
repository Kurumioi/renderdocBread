from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from replaylab_agent.errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
)
from replaylab_agent.profiles import Profile, ProfileStore
from replaylab_agent.runtime import AgentRuntime
from replaylab_agent.skills import SkillCatalog


FIXTURES = Path(__file__).parent / "fixtures"


def _acp_capture_kwargs(tmp_path: Path, name: str) -> dict[str, object]:
    return {
        "instance_id": "test-qrenderdoc-instance",
        "registry_path": (tmp_path / f"{name}-registry.json").resolve(),
        "audit_file": (tmp_path / f"{name}-audit.jsonl").resolve(),
    }


@pytest.mark.asyncio
async def test_codex_app_server_lifecycle_and_skill_input(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    skill = tmp_path / "SKILL.md"
    skill.write_text("test", encoding="utf-8")
    deltas: list[str] = []
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        result = await connector.run(
            "explain",
            skill_name="renderdoc-explain-event",
            skill_path=skill,
            on_delta=deltas.append,
        )
    finally:
        await connector.close()
    assert result.text == "evidence answer"
    assert result.thread_id == "thread-test"
    assert result.turn_id == "turn-test"
    assert deltas == ["evidence "]


@pytest.mark.asyncio
async def test_codex_app_server_routes_dynamic_tools_to_the_host(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    calls: list[tuple[str, dict[str, object], str]] = []

    async def handle(name: str, arguments: dict[str, object], call_id: str) -> object:
        calls.append((name, arguments, call_id))
        return {"eventId": arguments["event_id"], "inspected": True}

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "dynamic-tool"],
        cwd=tmp_path,
        expected_tools={"inspect_event"},
        dynamic_tools=(
            {
                "type": "function",
                "name": "inspect_event",
                "description": "Inspect one event",
                "inputSchema": {
                    "type": "object",
                    "properties": {"event_id": {"type": "integer"}},
                    "required": ["event_id"],
                    "additionalProperties": False,
                },
            },
        ),
        dynamic_tool_handler=handle,
        timeout=5,
    )

    result = await connector.run("inspect the event")

    assert result.text == "dynamic evidence answer"
    assert calls == [("inspect_event", {"event_id": 7}, "dynamic-call-1")]
    assert not connector._transport._server_request_tasks


def _dynamic_tool_definition() -> dict[str, object]:
    return {
        "type": "function",
        "name": "inspect_event",
        "description": "Inspect one event",
        "inputSchema": {
            "type": "object",
            "properties": {"event_id": {"type": "integer"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    }


@pytest.mark.asyncio
async def test_codex_dynamic_tool_request_can_precede_turn_start_response(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    calls: list[str] = []

    async def handle(name: str, arguments: dict[str, object], call_id: str) -> object:
        calls.append(call_id)
        return {"eventId": arguments["event_id"], "tool": name}

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "dynamic-tool-prestart"],
        cwd=tmp_path,
        expected_tools={"inspect_event"},
        dynamic_tools=(_dynamic_tool_definition(),),
        dynamic_tool_handler=handle,
        timeout=5,
    )

    result = await connector.run("inspect the event")

    assert result.text == "dynamic evidence answer"
    assert calls == ["dynamic-call-1"]
    assert not connector._transport._server_request_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, invalid_json",
    [("dynamic-tool-failure", False), ("dynamic-tool-invalid-json", True)],
)
async def test_codex_dynamic_tool_failures_use_a_normal_tool_response(
    tmp_path, mode: str, invalid_json: bool
) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    async def handle(name: str, arguments: dict[str, object], call_id: str) -> object:
        del name, arguments, call_id
        if invalid_json:
            return {"invalid": float("nan")}
        raise RuntimeError("private bridge failure")

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        expected_tools={"inspect_event"},
        dynamic_tools=(_dynamic_tool_definition(),),
        dynamic_tool_handler=handle,
        timeout=5,
    )

    result = await connector.run("inspect the event")

    assert result.text == "dynamic evidence answer"


@pytest.mark.asyncio
async def test_codex_close_cancels_and_joins_pending_server_request_tasks(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    started = asyncio.Event()
    finished = asyncio.Event()

    async def handle(name: str, arguments: dict[str, object], call_id: str) -> object:
        del name, arguments, call_id
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "dynamic-tool-block"],
        cwd=tmp_path,
        expected_tools={"inspect_event"},
        dynamic_tools=(_dynamic_tool_definition(),),
        dynamic_tool_handler=handle,
        timeout=5,
    )
    run_task = asyncio.create_task(connector.run("inspect the event"))
    await asyncio.wait_for(started.wait(), timeout=3)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert finished.is_set()
    assert not connector._transport._server_request_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["dynamic-tool-wrong-turn", "dynamic-tool-wrong-type", "dynamic-tool-duplicate"],
)
async def test_codex_dynamic_tool_scope_and_call_ids_fail_closed(tmp_path, mode: str) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    async def handle(name: str, arguments: dict[str, object], call_id: str) -> object:
        del name, call_id
        return {"eventId": arguments["event_id"]}

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        expected_tools={"inspect_event"},
        dynamic_tools=(_dynamic_tool_definition(),),
        dynamic_tool_handler=handle,
        timeout=5,
    )

    with pytest.raises(PolicyError, match="active turn grant|repeated"):
        await connector.run("inspect the event")


@pytest.mark.parametrize(
    "definition",
    [
        {
            "type": "namespace",
            "name": "inspect_event",
            "description": "bad namespace",
            "tools": [],
        },
        {
            "type": "function",
            "name": "invalid.name",
            "description": "bad name",
            "inputSchema": {"type": "object"},
        },
        {
            "type": "function",
            "name": "inspect_event",
            "description": "bad schema",
            "inputSchema": {"type": "string"},
        },
    ],
)
def test_codex_dynamic_tool_definitions_fail_closed(tmp_path, definition) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    with pytest.raises(ConfigurationError, match="dynamic tool|function"):
        CodexAppServerConnector(
            [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
            cwd=tmp_path,
            expected_tools={"inspect_event"},
            dynamic_tools=(definition,),
            dynamic_tool_handler=lambda *_args: None,
        )


@pytest.mark.asyncio
async def test_codex_app_server_streams_bounded_public_activity_without_raw_reasoning(
    tmp_path,
) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    timeline: list[dict[str, object]] = []
    connector = CodexAppServerConnector(
        [
            sys.executable,
            str(FIXTURES / "fake_codex_app_server.py"),
            "activity-events",
        ],
        cwd=tmp_path,
        timeout=5,
    )
    result = await connector.run("explain", on_timeline=lambda event: timeline.append(dict(event)))

    assert result.text == "evidence answer"
    assert {
        "plan",
        "reasoning",
        "commandExecution",
        "mcpToolCall",
        "webSearch",
        "imageView",
        "contextCompaction",
    } <= {event["activityKind"] for event in timeline}
    assert all(
        {"activityId", "activityKind", "activityTitle", "activityDetail", "status"}
        <= event.keys()
        for event in timeline
    )
    assert any(event["activityId"] == "turn-plan:turn-test" for event in timeline)
    assert [
        event["lifecycle"]
        for event in timeline
        if event["activityId"] == "command-1"
    ] == ["started", "updated", "updated", "completed"]
    assert sum(event["activityKind"] == "reasoning" for event in timeline) == 4

    completed_mcp = next(
        event
        for event in timeline
        if event["activityId"] == "mcp-1" and event["lifecycle"] == "completed"
    )
    assert "Parameters:\neventId: 7" in completed_mcp["activityDetail"]
    assert "Result:\nstatus: ok" in completed_mcp["activityDetail"]
    assert '{"eventId"' not in completed_mcp["activityDetail"]

    rendered = json.dumps(timeline, ensure_ascii=False)
    assert "Checking the selected draw" in rendered
    assert "Public reasoning summary complete" in rendered
    for forbidden in (
        "RAW_REASONING_STARTED_MUST_NOT_APPEAR",
        "RAW_REASONING_DELTA_MUST_NOT_APPEAR",
        "RAW_REASONING_COMPLETED_MUST_NOT_APPEAR",
        "raw-secret",
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
        assert forbidden not in rendered
    assert "[REDACTED]" in rendered
    assert all(len(str(event["activityDetail"])) <= 4096 for event in timeline)

    command_complete = next(
        event
        for event in timeline
        if event["activityId"] == "command-1" and event["lifecycle"] == "completed"
    )
    assert command_complete["durationMs"] == 23
    assert "final output ok" in str(command_complete["activityDetail"])
    mcp_complete = next(
        event
        for event in timeline
        if event["activityId"] == "mcp-1" and event["lifecycle"] == "completed"
    )
    assert "eventId: 7" in str(mcp_complete["activityDetail"])
    assert mcp_complete["durationMs"] == 11


@pytest.mark.asyncio
async def test_codex_app_server_negotiates_kebab_case_thread_sandbox_once(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [
            sys.executable,
            str(FIXTURES / "fake_codex_app_server.py"),
            "kebab-sandbox",
        ],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        result = await connector.run("explain")
    finally:
        await connector.close()
    assert result.text == "evidence answer"


@pytest.mark.asyncio
async def test_codex_app_server_negotiates_removed_turn_access_once(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [
            sys.executable,
            str(FIXTURES / "fake_codex_app_server.py"),
            "modern-turn-sandbox",
        ],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        result = await connector.run("explain")
    finally:
        await connector.close()
    assert result.text == "evidence answer"


@pytest.mark.asyncio
async def test_codex_app_server_surfaces_retry_and_heartbeat_without_losing_success(
    tmp_path,
) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    activity: list[tuple[str, float]] = []
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "slow-status"],
        cwd=tmp_path,
        timeout=5,
        heartbeat_interval=0.01,
    )
    result = await connector.run(
        "explain",
        on_activity=lambda status, elapsed: activity.append((status, elapsed)),
    )
    assert result.text == "evidence answer"
    assert any("timed out" in status for status, _elapsed in activity)
    assert any(status == "waiting for model response" for status, _elapsed in activity)


@pytest.mark.asyncio
async def test_codex_app_server_creates_isolated_home_only_when_started(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    home = (tmp_path / "managed-codex-home").resolve()
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
        }
        | {"CODEX_HOME": str(home)},
    )
    assert not home.exists()
    try:
        account = await connector.account_read()
    finally:
        await connector.close()
    assert home.is_dir()
    assert account["account"]["planType"] == "plus"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["signed-out", "signed-out-false", "signed-out-no-flag"])
async def test_codex_app_server_signed_out_fails_before_thread_start(tmp_path, mode) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=5,
    )
    with pytest.raises(AuthenticationError, match="Settings > AI"):
        await asyncio.wait_for(connector.run("do not send"), timeout=5)


@pytest.mark.asyncio
async def test_codex_account_model_and_managed_login_surface(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        account = await connector.account_read()
        assert account["account"]["planType"] == "plus"
        models = await connector.list_models()
        assert [item["id"] for item in models] == ["codex-test"]

        browser = await connector.login_start(flow="browser")
        assert browser["authUrl"].startswith("https://")
        completed = await connector.wait_login(browser["loginId"], timeout=5)
        assert completed["success"] is True

        device = await connector.login_start(flow="device")
        assert device["userCode"] == "TEST-CODE"
        completed = await connector.wait_login(device["loginId"], timeout=5)
        assert completed["success"] is True
        await connector.logout()
    finally:
        await connector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["login-race", "login-update-first"])
async def test_codex_first_browser_login_waits_for_account_then_lists_models(
    tmp_path, mode
) -> None:
    """One Connect must close login completion/account persistence races."""

    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [
            sys.executable,
            str(FIXTURES / "fake_codex_app_server.py"),
            mode,
        ],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        initial = await connector.account_read()
        assert initial["account"] is None

        browser = await connector.login_start(flow="browser")
        completed = await connector.wait_login(browser["loginId"], timeout=5)
        assert completed["success"] is True

        # wait_login does not release its operation lock until account/read is
        # authoritative, so the same connection can immediately discover its
        # models and complete the provider test.
        account = await connector.account_read()
        assert account["account"]["planType"] == "plus"
        models = await connector.list_models()
        assert [item["id"] for item in models] == ["codex-test"]
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_codex_app_server_approval_is_denied(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "approval"],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        with pytest.raises(PolicyError, match="fail-closed"):
            await connector.run("try a command")
    finally:
        await connector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, message",
    [
        ("ambient-instructions", "instruction sources"),
        ("extra-mcp", "must not load MCP servers"),
        ("wrong-tools", "must not load MCP servers"),
        ("callable-app", "Apps must be disabled"),
    ],
)
async def test_codex_ambient_capabilities_fail_closed(tmp_path, mode, message) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=5,
    )
    with pytest.raises(PolicyError, match=message):
        await connector.run("explain")


def test_codex_process_hardening_disables_ambient_agent_features() -> None:
    argv = AgentRuntime._harden_codex_argv(["codex", "app-server"])
    rendered = " ".join(argv)
    for override in (
        'model_provider="replaylab_openai_subscription"',
        "model_providers.replaylab_openai_subscription.supports_websockets=false",
        "features.apps=false",
        "features.plugins=false",
        "features.remote_plugin=false",
        "features.unbounded_connection_retries=false",
        "features.shell_tool=false",
        "features.unified_exec=false",
        "features.hooks=false",
        "features.multi_agent=false",
        "agents.enabled=false",
        "features.goals=false",
        'web_search="disabled"',
        'history.persistence="none"',
    ):
        assert override in rendered
    assert "mcp_servers.replaylab" not in rendered
    assert "replaylab_agent.mcp_proxy" not in rendered
    assert "REPLAYLAB_ALLOWED_TOOLS" not in rendered


@pytest.mark.asyncio
async def test_codex_jsonl_accepts_bounded_messages_over_64_kib(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "large-line"],
        cwd=tmp_path,
        timeout=5,
    )
    result = await connector.run("explain")
    assert result.text == "x" * 100_000


@pytest.mark.asyncio
async def test_codex_many_small_deltas_hit_cumulative_response_bound(tmp_path) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "many-deltas"],
        cwd=tmp_path,
        timeout=5,
    )
    with pytest.raises(ConnectorError, match="response exceeded"):
        await connector.run("explain")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, message",
    [
        ("exit-initialize", "exited unexpectedly.*17"),
        ("invalid-jsonl", "invalid JSONL"),
        ("invalid-utf8", "invalid JSONL"),
    ],
)
async def test_codex_transport_failures_are_bounded_and_close_owned_process(
    tmp_path, mode, message
) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=2,
        heartbeat_interval=0.05,
    )
    with pytest.raises(ConnectorError, match=message):
        await asyncio.wait_for(connector.run("explain"), timeout=4)

    assert connector._transport.process is None
    assert connector._transport._reader_task is None
    assert connector._transport._stderr_task is None


@pytest.mark.asyncio
async def test_codex_hung_turn_times_out_interrupts_and_closes_owned_process(
    tmp_path,
) -> None:
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "hang-turn"],
        cwd=tmp_path,
        timeout=0.2,
        heartbeat_interval=0.05,
    )
    with pytest.raises(ConnectorError, match="turn timed out"):
        await asyncio.wait_for(connector.run("explain"), timeout=4)

    assert connector._transport.process is None


@pytest.mark.asyncio
async def test_acp_provider_exit_is_reported_without_hanging(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector

    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "exit", "no-mcp"],
        cwd=tmp_path,
        replaylab_mcp_executable=None,
        allowed_tools=frozenset(),
        timeout=2,
    )
    with pytest.raises(ConnectorError, match="ACP agent failed"):
        await asyncio.wait_for(connector.run("hello"), timeout=4)


@pytest.mark.asyncio
async def test_acp_process_cleanup_timeout_is_a_fatal_lifecycle_error(monkeypatch) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    cleanup_cancelled = asyncio.Event()

    class HangingManager:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *args):
            del args
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    monkeypatch.setattr(acp_connector, "_ACP_CLEANUP_TIMEOUT", 0.02)
    with pytest.raises(LifecycleCleanupError, match="cleanup was not confirmed"):
        async with acp_connector._managed_agent_process(
            HangingManager(), "test ACP"
        ):
            pass
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_acp_capture_tools_use_one_task_filtered_mcp_proxy(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector
    from replaylab_agent.mcp_tools import find_replaylab_mcp

    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "filtered"],
        cwd=tmp_path,
        replaylab_mcp_executable=find_replaylab_mcp(),
        allowed_tools=frozenset({"get_pipeline_state"}),
        **_acp_capture_kwargs(tmp_path, "stream"),
        timeout=10,
    )
    result = await connector.run("hello")
    assert result.text == "ACP: hello"


@pytest.mark.asyncio
async def test_acp_general_chat_starts_without_renderdoc_mcp(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector
    from replaylab_agent.mcp_tools import find_replaylab_mcp

    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "no-mcp"],
        cwd=tmp_path,
        replaylab_mcp_executable=None,
        allowed_tools=frozenset(),
        timeout=10,
    )
    result = await connector.run("hello")
    assert result.text == "ACP: hello"


@pytest.mark.asyncio
async def test_external_agent_runtime_general_chat_needs_no_capture_instance(tmp_path) -> None:
    pytest.importorskip("acp")

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "managed-chat",
            "custom-acp",
            command=[sys.executable, str(FIXTURES / "fake_acp_agent.py"), "no-mcp"],
            options={
                "allow_unsandboxed_agent": True,
                "context_window_tokens": 65_536,
            },
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    result = await runtime.run("hello", profile_id="managed-chat", task_id="managed-general")
    assert result.text.startswith("ACP: Answer as RenderDoc AI general help.")
    payloads = [
        json.loads(line)
        for line in (runtime.event_root / "managed-general.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert payloads[-2]["data"] == {"stream": "final", "reset": True}
    assert payloads[-1]["kind"] == "completed"
    turn_workspace = tmp_path / "external-agent-workspace"
    assert not list(turn_workspace.glob("turn-*"))
    assert not list(turn_workspace.glob(".delete-turn-*"))


@pytest.mark.asyncio
async def test_external_agent_runtime_cleans_turn_workspace_after_failure(tmp_path) -> None:
    pytest.importorskip("acp")

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "managed-failure",
            "custom-acp",
            command=[
                sys.executable,
                str(FIXTURES / "fake_acp_agent.py"),
                "refusal",
                "no-mcp",
            ],
            options={
                "allow_unsandboxed_agent": True,
                "context_window_tokens": 65_536,
            },
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )

    with pytest.raises(ConnectorError, match="refusal"):
        await runtime.run(
            "hello", profile_id="managed-failure", task_id="managed-failure"
        )

    turn_workspace = tmp_path / "external-agent-workspace"
    assert not list(turn_workspace.glob("turn-*"))
    assert not list(turn_workspace.glob(".delete-turn-*"))


def test_external_agent_workspace_reaps_only_owned_stale_turns(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    root = runtime._external_agent_workspace()
    stale = root / ("turn-" + "a" * 32)
    fresh = root / ("turn-" + "b" * 32)
    unrelated = root / "user-directory"
    stale.mkdir()
    fresh.mkdir()
    unrelated.mkdir()
    (stale / "audit.jsonl").write_text("old", encoding="utf-8")
    old = time.time() - runtime._EXTERNAL_TURN_STALE_SECONDS - 60
    os.utime(stale, (old, old))

    assert runtime._external_agent_workspace() == root

    assert not stale.exists()
    assert fresh.is_dir()
    assert unrelated.is_dir()
    assert runtime._cleanup_external_agent_turn_workspace(fresh)


def test_external_agent_turn_creation_cleans_after_cursor_setup_failure(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.runtime as runtime_module

    store = ProfileStore(tmp_path / "profiles.json")
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    profile = Profile(
        "cursor-cleanup",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )

    def fail_setup(_profile):
        raise ConfigurationError("managed Cursor setup failed")

    monkeypatch.setattr(runtime_module, "ensure_cursor_profile_config", fail_setup)

    with pytest.raises(ConfigurationError, match="managed Cursor setup failed"):
        runtime._external_agent_turn_workspace(profile)

    turn_workspace = tmp_path / "external-agent-workspace"
    assert not list(turn_workspace.glob("turn-*"))
    assert not list(turn_workspace.glob(".delete-turn-*"))


@pytest.mark.asyncio
async def test_acp_rejects_an_mcp_executable_without_task_tools(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector
    from replaylab_agent.mcp_tools import find_replaylab_mcp

    with pytest.raises(ConfigurationError, match="without any task tools"):
        ACPConnector(
            [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "no-mcp"],
            cwd=tmp_path,
            replaylab_mcp_executable=find_replaylab_mcp(),
            allowed_tools=frozenset(),
            timeout=10,
        )


@pytest.mark.asyncio
async def test_acp_non_terminal_stop_reason_is_not_success(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector
    from replaylab_agent.mcp_tools import find_replaylab_mcp

    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "refusal", "no-mcp"],
        cwd=tmp_path,
        allowed_tools=frozenset(),
        timeout=10,
    )
    with pytest.raises(ConnectorError, match="refusal"):
        await connector.run("hello")


@pytest.mark.asyncio
async def test_acp_many_small_deltas_hit_cumulative_response_bound(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector
    from replaylab_agent.mcp_tools import find_replaylab_mcp

    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "many-deltas", "no-mcp"],
        cwd=tmp_path,
        allowed_tools=frozenset(),
        timeout=10,
    )
    with pytest.raises(ConnectorError, match="response exceeded"):
        await connector.run("hello")


def test_external_agent_environment_is_minimal_and_explicit(monkeypatch) -> None:
    from replaylab_agent.process_env import host_process_environment

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-external-agent")
    monkeypatch.setenv("KIMI_MEMBERSHIP_KEY", "explicit-only")
    profile = Profile(
        "agent-env-test",
        "custom-acp",
        command=[sys.executable],
        agent_env=["KIMI_MEMBERSHIP_KEY"],
    )
    environment = AgentRuntime._external_agent_env(profile)
    assert "OPENAI_API_KEY" not in environment
    assert environment["KIMI_MEMBERSHIP_KEY"] == "explicit-only"
    assert "OPENAI_API_KEY" not in host_process_environment()


def test_model_context_does_not_receive_intent_lease() -> None:
    prompt = AgentRuntime._with_context(
        "explain",
        {"eventId": 7},
        {
            "intentId": "intent-1",
            "kind": "explain-current-event",
            "context": {"secretDuplicate": True},
            "arguments": {"eventId": 7, "nested": {"lease_token": "also-private"}},
            "recommendedWorkflow": {"allowedOperations": ["read-capture"]},
            "leaseToken": "never-send-this",
            "claimedBy": "host-private",
            "leaseExpiresAt": 999,
        },
    )
    assert "intent-1" in prompt
    assert "never-send-this" not in prompt
    assert "also-private" not in prompt
    assert "host-private" not in prompt
    assert "secretDuplicate" not in prompt


@pytest.mark.asyncio
async def test_acp_client_file_and_terminal_capabilities_fail_closed() -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import _ACPClientState, _load_acp_sdk, _make_client

    client = _make_client(_load_acp_sdk(), _ACPClientState(on_delta=None))
    with pytest.raises(PolicyError, match="read a local file"):
        await client.read_text_file("session", "C:/secret.txt")
    with pytest.raises(PolicyError, match="create a terminal"):
        await client.create_terminal("session", "powershell")


@pytest.mark.asyncio
async def test_cursor_acp_permission_requests_are_cancelled_without_trusting_labels(
    tmp_path,
) -> None:
    pytest.importorskip("acp")
    from acp import schema
    from replaylab_agent.connectors.acp import (
        _ACPClientState,
        _load_acp_sdk,
        _make_client,
    )

    audit = tmp_path / "permission-audit.jsonl"
    state = _ACPClientState(
        on_delta=None,
        provider_id="cursor-acp",
        session_id="session-1",
        permission_audit_file=audit,
    )
    client = _make_client(_load_acp_sdk(), state)
    response = await client.request_permission(
        "session-1",
        schema.ToolCallUpdate(
            toolCallId="call-1",
            title="Mcp(ReplayLab:get_pipeline_state)",
            kind="other",
            rawInput={"server": "ReplayLab", "tool": "get_pipeline_state"},
        ),
        [
            schema.PermissionOption(
                optionId="allow-always",
                name="Allow always",
                kind="allow_always",
            ),
            schema.PermissionOption(
                optionId="reject-once",
                name="Reject",
                kind="reject_once",
            ),
        ],
    )

    payload = response.model_dump(by_alias=True) if hasattr(response, "model_dump") else response
    assert payload["outcome"] == {"outcome": "cancelled"}
    assert state.policy_error is None
    assert state.cursor_permission_denials == 1
    audit_payload = json.loads(audit.read_text(encoding="utf-8"))
    assert audit_payload == {
        "event": "permissionDenied",
        "provider": "cursor-acp",
        "sequence": 1,
    }
    assert "get_pipeline_state" not in audit.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cursor_acp_permission_validation_is_bounded_and_session_bound() -> None:
    pytest.importorskip("acp")
    from acp import schema
    from replaylab_agent.connectors.acp import (
        _ACPClientState,
        _load_acp_sdk,
        _make_client,
    )

    state = _ACPClientState(
        on_delta=None,
        provider_id="cursor-acp",
        session_id="session-1",
    )
    client = _make_client(_load_acp_sdk(), state)
    tool_call = schema.ToolCallUpdate(toolCallId="call-1", title="Read", kind="read")
    options = [
        schema.PermissionOption(
            optionId="reject-once", name="Reject", kind="reject_once"
        )
    ]

    await client.request_permission("other-session", tool_call, options)
    assert state.policy_error is not None
    assert "session mismatch" in str(state.policy_error)
    assert state.abort_event.is_set()

    state = _ACPClientState(
        on_delta=None,
        provider_id="cursor-acp",
        session_id="session-1",
    )
    client = _make_client(_load_acp_sdk(), state)
    await client.request_permission("session-1", tool_call, options)
    assert state.policy_error is None
    await client.request_permission("session-1", tool_call, options)
    assert state.policy_error is not None
    assert "duplicate tool-call id" in str(state.policy_error)

    state = _ACPClientState(
        on_delta=None,
        provider_id="cursor-acp",
        session_id="session-1",
    )
    client = _make_client(_load_acp_sdk(), state)
    for index in range(17):
        await client.request_permission(
            "session-1",
            schema.ToolCallUpdate(
                toolCallId=f"bounded-{index}", title="Denied", kind="read"
            ),
            options,
        )
    assert state.cursor_permission_denials == 17
    assert state.policy_error is not None
    assert "bounded permission-denial limit" in str(state.policy_error)
    assert state.abort_event.is_set()


@pytest.mark.asyncio
async def test_cursor_acp_known_extensions_are_noninteractive_and_unknowns_fail_closed() -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import (
        _ACPClientState,
        _load_acp_sdk,
        _make_client,
    )

    state = _ACPClientState(on_delta=None, provider_id="cursor-acp")
    client = _make_client(_load_acp_sdk(), state)
    assert await client.ext_method("cursor/ask_question", {"private": "ignored"}) == {
        "outcome": {
            "outcome": "skipped",
            "reason": "ReplayLab does not provide interactive answers during this automated turn.",
        }
    }
    assert await client.ext_method("cursor/create_plan", {"private": "ignored"}) == {
        "outcome": {
            "outcome": "rejected",
            "reason": "ReplayLab does not approve external execution plans; continue with read-only analysis.",
        }
    }
    for method in (
        "cursor/update_todos",
        "cursor/task",
        "cursor/generate_image",
    ):
        await client.ext_notification(method, {"private": "ignored"})
    assert state.policy_error is None

    await client.ext_notification("cursor/unknown", {})
    assert state.policy_error is not None
    assert state.abort_event.is_set()

    generic_state = _ACPClientState(on_delta=None)
    generic_client = _make_client(_load_acp_sdk(), generic_state)
    await generic_client.request_permission("session", {}, [])
    assert generic_state.policy_error is not None
    assert generic_state.abort_event.is_set()
