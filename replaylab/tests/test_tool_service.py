from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

import replaylab_agent.tool_service as tool_service_module
from replaylab_agent.errors import ConfigurationError, PolicyError
from replaylab_agent.policy import ToolCallScope
from replaylab_agent.tool_service import (
    InternalToolService,
    ToolAudit,
    call_dynamic_tool,
    dynamic_tool_specs,
)


class FakeBridge:
    def __init__(self, session_id: str = "a" * 32) -> None:
        self.session_id = session_id
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        instance_id: str = "",
        timeout: float | None = None,
    ) -> Any:
        del timeout
        values = dict(params or {})
        self.calls.append((method, values, instance_id))
        if method == "get_current_renderdoc_context":
            return {
                "capture": {
                    "loaded": True,
                    "captureSessionId": self.session_id,
                }
            }
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            time.sleep(0.01)
            if method == "inspect_event":
                return {
                    "schema": "renderdoc-internal-inspect-event/v1",
                    "eventId": int(values["event_id"]),
                    "drawCall": {},
                    "pipeline": {},
                }
            return {"method": method, "arguments": values}
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.asyncio
async def test_inspect_event_is_one_audited_high_level_transaction() -> None:
    bridge = FakeBridge()
    service = InternalToolService(bridge)
    audit = ToolAudit()
    invocation = service.invocation(
        {"inspect_event"},
        instance_id="qrenderdoc-test",
        scope=ToolCallScope(event_id=17, capture_session_id="a" * 32),
        audit=audit,
    )

    result = await call_dynamic_tool(
        service,
        invocation,
        "inspect_event",
        {"event_id": 17},
        call_id="codex-call-1",
    )

    assert result["schema"] == "renderdoc-internal-inspect-event/v1"
    assert [item[0] for item in bridge.calls] == [
        "get_current_renderdoc_context",
        "inspect_event",
        "get_current_renderdoc_context",
    ]
    assert all(
        item[1]["__replaylab_expected_capture_session_id"] == "a" * 32
        for item in bridge.calls
    )
    assert audit.started == ["inspect_event"]
    assert audit.completed == ["inspect_event"]
    assert audit.failed == []
    assert audit.evidence[0].tool_call_id == "codex-call-1"


@pytest.mark.asyncio
async def test_dynamic_schema_and_scope_fail_closed() -> None:
    service = InternalToolService(FakeBridge())
    invocation = service.invocation(
        {"analyze_pixel"},
        instance_id="qrenderdoc-test",
        scope=ToolCallScope(
            intent_kind="inspect-picked-pixel",
            event_id=9,
            resource_id=42,
            x=3,
            y=4,
            mip=0,
            slice=0,
            sample=0,
            capture_session_id="a" * 32,
        ),
    )
    specs = dynamic_tool_specs(service, invocation)
    assert [item["name"] for item in specs] == ["analyze_pixel"]
    assert set(specs[0]["inputSchema"]["required"]) == {
        "texture_id",
        "x",
        "y",
        "event_id",
    }

    with pytest.raises(PolicyError, match="frozen resource"):
        await call_dynamic_tool(
            service,
            invocation,
            "analyze_pixel",
            {"texture_id": 43, "x": 3, "y": 4, "event_id": 9},
        )
    with pytest.raises(PolicyError, match="arguments are invalid"):
        await call_dynamic_tool(
            service,
            invocation,
            "analyze_pixel",
            {"texture_id": 42, "x": 3, "y": 4, "event_id": 9, "extra": True},
        )


@pytest.mark.asyncio
async def test_parallel_specialists_share_one_serial_replay_service() -> None:
    bridge = FakeBridge()
    service = InternalToolService(bridge)
    first = service.invocation(
        {"inspect_event"},
        instance_id="qrenderdoc-test",
        scope=ToolCallScope(event_id=1),
    )
    second = service.invocation(
        {"inspect_event"},
        instance_id="qrenderdoc-test",
        scope=ToolCallScope(event_id=2),
    )

    await asyncio.gather(
        call_dynamic_tool(service, first, "inspect_event", {"event_id": 1}),
        call_dynamic_tool(service, second, "inspect_event", {"event_id": 2}),
    )

    assert bridge.maximum_active == 1


@pytest.mark.asyncio
async def test_catalog_export_never_treats_empty_scope_as_implicit_full_scan(tmp_path) -> None:
    bridge = FakeBridge()
    service = InternalToolService(bridge, catalog_root=tmp_path)
    kwargs = {
        "instance_id": "qrenderdoc-test",
        "capture_session_id": "a" * 32,
        "output_dir": str(tmp_path),
        "event_ids": [],
    }
    with pytest.raises(PolicyError, match="explicit event IDs"):
        await service.export_shader_catalog(**kwargs)
    with pytest.raises(ConfigurationError, match="chunked background job"):
        await service.export_shader_catalog(**kwargs, allow_full_capture=True)
    assert not any(method == "export_shader_catalog" for method, _, _ in bridge.calls)


@pytest.mark.asyncio
async def test_catalog_export_is_pinned_bounded_and_agent_owned(tmp_path) -> None:
    bridge = FakeBridge()
    service = InternalToolService(bridge, catalog_root=tmp_path)
    output = tmp_path / "capture-a"
    base = {
        "instance_id": "qrenderdoc-test",
        "capture_session_id": "a" * 32,
        "output_dir": str(output),
    }

    with pytest.raises(ConfigurationError, match="pinned qrenderdoc"):
        await service.export_shader_catalog(
            **{**base, "instance_id": ""}, event_ids=[1]
        )
    with pytest.raises(PolicyError, match="exact capture session"):
        await service.export_shader_catalog(
            **{**base, "capture_session_id": "not-a-session"}, event_ids=[1]
        )
    with pytest.raises(PolicyError, match="at most 64"):
        await service.export_shader_catalog(**base, event_ids=list(range(1, 66)))
    with pytest.raises(PolicyError, match="unique"):
        await service.export_shader_catalog(**base, event_ids=[1, 1])
    with pytest.raises(PolicyError, match="positive integers"):
        await service.export_shader_catalog(**base, event_ids=[0])
    with pytest.raises(PolicyError, match="catalog root"):
        await service.export_shader_catalog(
            **{**base, "output_dir": str(tmp_path.parent / "outside")},
            event_ids=[1],
        )

    result = await service.export_shader_catalog(**base, event_ids=[2, 7])
    assert result["method"] == "export_shader_catalog"
    export_call = next(item for item in bridge.calls if item[0] == "export_shader_catalog")
    assert export_call[1]["event_ids"] == [2, 7]
    assert export_call[1]["__replaylab_expected_capture_session_id"] == "a" * 32
    assert export_call[2] == "qrenderdoc-test"


class BlockingBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.actual_calls = 0

    def call(self, method, params=None, *, instance_id="", timeout=None):
        if method == "get_current_renderdoc_context":
            return super().call(
                method, params, instance_id=instance_id, timeout=timeout
            )
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.actual_calls += 1
            ordinal = self.actual_calls
        try:
            self.calls.append((method, dict(params or {}), instance_id))
            if ordinal == 1:
                self.first_started.set()
                assert self.release_first.wait(2.0)
            return {"method": method}
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.asyncio
async def test_cancelled_bridge_thread_keeps_shared_replay_lock_until_it_stops() -> None:
    bridge = BlockingBridge()
    service = InternalToolService(bridge)
    first = service.invocation(
        {"inspect_event"}, instance_id="qrenderdoc-test", scope=ToolCallScope(event_id=1)
    )
    second = service.invocation(
        {"inspect_event"}, instance_id="qrenderdoc-test", scope=ToolCallScope(event_id=2)
    )
    first_task = asyncio.create_task(
        call_dynamic_tool(service, first, "inspect_event", {"event_id": 1})
    )
    assert await asyncio.to_thread(bridge.first_started.wait, 1.0)
    first_task.cancel()
    second_task = asyncio.create_task(
        call_dynamic_tool(service, second, "inspect_event", {"event_id": 2})
    )
    await asyncio.sleep(0.05)
    assert bridge.actual_calls == 1
    bridge.release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await second_task
    assert bridge.maximum_active == 1


@pytest.mark.asyncio
async def test_workflow_contract_runs_in_process_with_host_scope_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_shader_workspace_status(workspace_path: str) -> str:
        """Return one verified Shader workspace status."""

        raise AssertionError("the schema source must never execute directly")

    calls: list[tuple[str, dict[str, Any], str, str]] = []

    monkeypatch.setattr(
        tool_service_module,
        "_direct_workflow_function",
        lambda name: get_shader_workspace_status,
    )

    def invoke(
        name: str,
        arguments: dict[str, Any],
        instance_id: str,
        capture_session_id: str,
    ) -> dict[str, Any]:
        calls.append((name, dict(arguments), instance_id, capture_session_id))
        return json.loads(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-workspace/v1",
                    "operation": "status",
                    "workspacePath": arguments["workspace_path"],
                }
            )
        )

    monkeypatch.setattr(tool_service_module, "_invoke_direct_workflow", invoke)

    bridge = FakeBridge()
    service = InternalToolService(bridge)
    audit = ToolAudit()
    invocation = service.invocation(
        {"get_shader_workspace_status"},
        instance_id="qrenderdoc-pinned",
        scope=ToolCallScope(capture_session_id="a" * 32),
        audit=audit,
    )

    specs = dynamic_tool_specs(service, invocation)
    assert [item["name"] for item in specs] == ["get_shader_workspace_status"]
    assert specs[0]["inputSchema"]["required"] == ["workspace_path"]
    result = await call_dynamic_tool(
        service,
        invocation,
        "get_shader_workspace_status",
        {"workspace_path": "D:/ReplayLab/workspace"},
        call_id="direct-workflow-1",
    )

    assert result["operation"] == "status"
    assert calls == [
        (
            "get_shader_workspace_status",
            {"workspace_path": "D:/ReplayLab/workspace"},
            "qrenderdoc-pinned",
            "a" * 32,
        )
    ]
    assert [item[0] for item in bridge.calls] == [
        "get_current_renderdoc_context",
        "get_current_renderdoc_context",
    ]
    assert audit.completed == ["get_shader_workspace_status"]
    assert audit.evidence[0].tool_call_id == "direct-workflow-1"
