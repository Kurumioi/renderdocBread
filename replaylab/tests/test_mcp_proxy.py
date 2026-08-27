from __future__ import annotations

import asyncio
import json

import pytest

from replaylab_agent import mcp_proxy
from replaylab_agent import mcp_tools
from replaylab_agent.errors import ConfigurationError, PolicyError
from replaylab_agent.policy import ToolCallScope


def _audit_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_mcp_discovery_prefers_side_by_side_unicode_host(tmp_path, monkeypatch) -> None:
    host_dir = tmp_path / "x64" / "Development" / "replaylab"
    host_dir.mkdir(parents=True)
    unicode_host = host_dir / "ReplayLabMCP.unicode.exe"
    legacy_host = host_dir / "ReplayLabMCP.exe"
    unicode_host.write_bytes(b"unicode-host")
    legacy_host.write_bytes(b"legacy-host")
    monkeypatch.delenv("REPLAYLAB_MCP_HOST", raising=False)
    monkeypatch.setattr(mcp_tools, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_tools.shutil, "which", lambda _name: None)

    assert mcp_tools.find_replaylab_mcp() == unicode_host.resolve()


def test_explicit_mcp_host_still_overrides_side_by_side_build(
    tmp_path, monkeypatch
) -> None:
    explicit = tmp_path / "自定义宿主.exe"
    explicit.write_bytes(b"explicit-host")
    host_dir = tmp_path / "x64" / "Development" / "replaylab"
    host_dir.mkdir(parents=True)
    (host_dir / "ReplayLabMCP.unicode.exe").write_bytes(b"unicode-host")
    monkeypatch.delenv("REPLAYLAB_MCP_HOST", raising=False)
    monkeypatch.setattr(mcp_tools, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_tools.shutil, "which", lambda _name: None)

    assert mcp_tools.find_replaylab_mcp(explicit) == explicit.resolve()


@pytest.mark.asyncio
async def test_proxy_audits_scope_argument_precheck_failure(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    scope = ToolCallScope(
        intent_kind="explain-current-event",
        event_id=7,
        capture_session_id="b" * 32,
    )
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return object()

    with pytest.raises(PolicyError, match="frozen event"):
        await mcp_proxy._call_upstream_tool(
            call_tool,
            "get_pipeline_state",
            {"event_id": 8},
            scope=scope,
            audit_file=audit,
        )

    assert calls == []
    assert _audit_records(audit) == [
        {
            "tool": "get_pipeline_state",
            "status": "failed",
            "error": "PolicyError: tool 'get_pipeline_state' must use frozen event 7",
        }
    ]


@pytest.mark.asyncio
async def test_proxy_audits_upstream_exception_without_reclassifying_it(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    original = RuntimeError("upstream disconnected")

    async def call_tool(name, arguments):
        del name, arguments
        raise original

    with pytest.raises(RuntimeError) as raised:
        await mcp_proxy._call_upstream_tool(
            call_tool,
            "get_pipeline_state",
            {},
            scope=None,
            audit_file=audit,
        )

    assert raised.value is original
    assert _audit_records(audit)[0] == {
        "tool": "get_pipeline_state",
        "status": "failed",
        "error": "RuntimeError: upstream disconnected",
    }


@pytest.mark.asyncio
async def test_proxy_audits_postcheck_failure_and_preserves_exception(
    tmp_path, monkeypatch
) -> None:
    audit = tmp_path / "audit.jsonl"
    original = PolicyError("capture session changed after tool execution")
    checks = 0

    async def verify(_call_tool, _scope):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise original

    monkeypatch.setattr(mcp_proxy, "verify_live_tool_scope", verify)

    async def call_tool(name, arguments):
        del name, arguments
        return {"content": []}

    with pytest.raises(PolicyError) as raised:
        await mcp_proxy._call_upstream_tool(
            call_tool,
            "get_pipeline_state",
            {},
            scope=ToolCallScope(capture_session_id="b" * 32),
            audit_file=audit,
        )

    assert raised.value is original
    assert checks == 2
    assert _audit_records(audit)[0]["status"] == "failed"
    assert "capture session changed after tool execution" in _audit_records(audit)[0][
        "error"
    ]


@pytest.mark.asyncio
async def test_proxy_does_not_replace_tool_failure_when_failed_audit_write_fails(
    monkeypatch,
) -> None:
    original = RuntimeError("original upstream failure")

    async def call_tool(name, arguments):
        del name, arguments
        raise original

    def fail_audit(*_args, **_kwargs):
        raise ConfigurationError("audit unavailable")

    monkeypatch.setattr(mcp_proxy, "_append_audit", fail_audit)
    with pytest.raises(RuntimeError) as raised:
        await mcp_proxy._call_upstream_tool(
            call_tool,
            "get_pipeline_state",
            {},
            scope=None,
            audit_file=None,
        )

    assert raised.value is original


@pytest.mark.asyncio
async def test_proxy_audits_cancelled_upstream_call_before_propagating(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    started = asyncio.Event()

    async def call_tool(name, arguments):
        del name, arguments
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        mcp_proxy._call_upstream_tool(
            call_tool,
            "get_pipeline_state",
            {},
            scope=None,
            audit_file=audit,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert _audit_records(audit) == [
        {
            "tool": "get_pipeline_state",
            "status": "failed",
            "error": "CancelledError",
        }
    ]
