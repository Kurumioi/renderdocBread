"""The task-scoped MCP proxy enforces deterministic authority boundaries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent import mcp_proxy
from replaylab_agent.errors import ConfigurationError, PolicyError
from replaylab_agent.policy import (
    EFFECTFUL_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_EFFECTS,
    tool_call_effects,
    tool_effects,
    validate_registered_tool_effect_manifest,
)
from replaylab_agent.tool_scope import ToolCallScope


def _args(host: Path, registry: Path, *extra: str):
    return mcp_proxy._parser().parse_args(
        [
            "--host",
            str(host),
            "--instance-id",
            "instance-1",
            "--registry-path",
            str(registry),
            *extra,
        ]
    )


def test_effect_manifest_is_declarative_not_a_workflow_state_machine() -> None:
    assert tool_effects("inspect_event") == frozenset()
    assert tool_effects("compile_shader_workspace") == frozenset({"workspace"})
    assert tool_effects("apply_shader_workspace_candidate") == frozenset({"replay"})
    assert tool_effects("get_active_replay_project_status") == frozenset()
    assert "get_active_shader_workspace_path" not in READ_ONLY_TOOLS
    assert "get_active_shader_workspace_path" in EFFECTFUL_TOOLS
    assert tool_effects("get_active_shader_workspace_path") == frozenset({"project", "replay"})
    assert tool_effects("start_shader_workspace_live_edit") == frozenset(
        {"workspace", "replay"}
    )
    assert tool_effects("review_shader_workspace_organization") == frozenset(
        {"workspace", "replay"}
    )
    assert tool_effects("register_active_project_workspace") == frozenset(
        {"project", "ui"}
    )
    assert tool_call_effects(
        "classify_current_frame_passes",
        {"publish_to_ui": True, "persist_project": False},
    ) == frozenset({"ui"})


def test_effect_manifest_exactly_matches_the_live_first_party_host() -> None:
    from src import server

    registered = frozenset(
        kwargs.get("name") or function.__name__
        for function, _args, kwargs in server.mcp._registrations
    )

    assert validate_registered_tool_effect_manifest(registered) == registered
    assert READ_ONLY_TOOLS | EFFECTFUL_TOOLS == registered
    assert frozenset(TOOL_EFFECTS) == EFFECTFUL_TOOLS
    with pytest.raises(PolicyError, match="not registered"):
        validate_registered_tool_effect_manifest(registered - {"inspect_event"})
    with pytest.raises(PolicyError, match="lack explicit effect declarations"):
        validate_registered_tool_effect_manifest(registered | {"new_host_tool"})


def test_proxy_diagnostic_unwraps_task_group_leaf_causes() -> None:
    error = ExceptionGroup(
        "task group",
        [ConfigurationError("ReplayLab MCP Host is missing allowed tool: stale_tool")],
    )

    assert mcp_proxy._exception_detail(error) == (
        "ReplayLab MCP Host is missing allowed tool: stale_tool"
    )


@pytest.mark.asyncio
async def test_proxy_exposes_the_exact_live_host_catalog(tmp_path: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    allowed = READ_ONLY_TOOLS | EFFECTFUL_TOOLS
    environment = dict(os.environ)
    environment["REPLAYLAB_TEST_ALLOWED_TOOLS"] = json.dumps(sorted(allowed))
    environment["REPLAYLAB_TASK_ID"] = "proxy-invalid-report"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    environment["REPLAYLAB_REVIEW_MAX_ROUNDS"] = "2"
    registry = (tmp_path / "live-edit-registry.json").resolve()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "replaylab_agent.mcp_proxy",
            "--host",
            sys.executable,
            "--host-arg=-m",
            "--host-arg=src.server",
            "--allow-env",
            "REPLAYLAB_TEST_ALLOWED_TOOLS",
            "--effect",
            "workspace",
            "--effect",
            "project",
            "--effect",
            "ui",
            "--effect",
            "replay",
            "--instance-id",
            "proxy-integration-test",
            "--registry-path",
            str(registry),
            "--max-tool-calls",
            "2",
        ],
        env=environment,
    )

    # MCP captures its default stderr at import time. Earlier capture tests may
    # have supplied a stream without fileno; a real subprocess needs a real file.
    with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listing = await session.list_tools()
                for attempt in (1, 2):
                    invalid = await session.call_tool("report_task_result", {"status": "impossible", "findings": "invalid"})
                    assert not invalid.isError, invalid.content
                    verdict = json.loads(invalid.content[0].text)
                    assert verdict["schema"] == "replaylab-review-verdict/v1"
                    assert verdict["round"] == attempt and verdict["accepted"] is False
                    assert verdict["reasons"][0]["code"] == "schema"
                assert verdict["budgetExhausted"] is True

    tools = {tool.name: tool for tool in listing.tools}
    assert set(tools) == allowed
    read_only = tools["inspect_event"].annotations
    assert read_only is not None
    assert read_only.readOnlyHint is True
    assert read_only.destructiveHint is False
    assert read_only.idempotentHint is True
    assert read_only.openWorldHint is False
    effectful = tools["compile_shader_workspace"].annotations
    assert effectful is not None
    assert effectful.readOnlyHint is False
    assert effectful.destructiveHint is True
    assert effectful.idempotentHint is False
    assert effectful.openWorldHint is False


def test_filter_requires_explicit_allow_list_and_fresh_registry(tmp_path: Path) -> None:
    host = (tmp_path / "ReplayLabMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()

    with pytest.raises(ConfigurationError, match="explicit tool allow-list"):
        mcp_proxy._validate_configuration(_args(host, registry))

    registry.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="fresh absolute"):
        mcp_proxy._validate_configuration(
            _args(host, registry, "--allow", "inspect_event")
        )


def test_filter_rejects_allow_list_above_connection_effect_ceiling(tmp_path: Path) -> None:
    host = (tmp_path / "ReplayLabMCP.exe").resolve()
    host.write_bytes(b"host")

    with pytest.raises(ConfigurationError, match="exceeds the effect ceiling"):
        mcp_proxy._validate_configuration(
            _args(
                host,
                (tmp_path / "registry.json").resolve(),
                "--allow",
                "prepare_shader_workspace",
            )
        )


def test_replay_effect_requires_workspace_and_budget_is_bounded(tmp_path: Path) -> None:
    host = (tmp_path / "ReplayLabMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()

    with pytest.raises(ConfigurationError, match="replay effect requires workspace"):
        mcp_proxy._validate_configuration(
            _args(host, registry, "--allow", "inspect_event", "--effect", "replay")
        )
    with pytest.raises(ConfigurationError, match="call budget"):
        mcp_proxy._validate_configuration(
            _args(
                host,
                registry,
                "--allow",
                "inspect_event",
                "--max-tool-calls",
                "0",
            )
        )


def test_shader_review_requires_declared_temporary_replay_effect(tmp_path: Path) -> None:
    host = (tmp_path / "ReplayLabMCP.exe").resolve()
    host.write_bytes(b"host")
    with pytest.raises(ConfigurationError, match="exceeds the effect ceiling"):
        mcp_proxy._validate_configuration(
            _args(
                host, (tmp_path / "registry.json").resolve(),
                "--allow", "review_shader_workspace_organization",
                "--effect", "workspace",
            )
        )


def test_shader_catalog_bootstrap_cannot_enter_through_read_only_scope(tmp_path: Path) -> None:
    host = (tmp_path / "ReplayLabMCP.exe").resolve()
    host.write_bytes(b"host")
    with pytest.raises(ConfigurationError, match="exceeds the effect ceiling"):
        mcp_proxy._validate_configuration(_args(
            host, (tmp_path / "registry.json").resolve(),
            "--allow", "get_active_shader_workspace_path",
        ))


def test_frozen_native_ui_scope_rejects_conflicting_tool_arguments() -> None:
    scope = ToolCallScope.from_request(
        {
            "capture": {"captureSessionId": "a" * 32, "path": "D:/capture.rdc"},
            "events": {"currentEventId": 11329},
        },
        {"kind": "explain-current-event", "arguments": {"eventId": 11329}},
    )

    scope.validate("inspect_event", {"event_id": 11329})
    with pytest.raises(PolicyError, match="frozen UI target"):
        scope.validate("inspect_event", {"event_id": 7})
    assert ToolCallScope.from_json(scope.to_json()) == scope
