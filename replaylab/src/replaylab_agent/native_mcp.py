"""Task-scoped MCP declarations for vendor-native subscription agents."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .mcp_runtime import find_replaylab_mcp
from .agent_context import ProviderContext, child_tool_grant, harden_codex_argv
from .agent_report import REPORT_TOOL, TASK_EVENT_ROOT_ENV, TASK_ID_ENV


_CODEX_TASK_MCP_STARTUP_TIMEOUT_SECONDS = 60
_CODEX_TASK_MCP_TOOL_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class NativeMCPRoute:
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    allowed_tools: frozenset[str]
    effects: frozenset[str]
    registry_path: Path

    def claude_json(self) -> dict[str, object]:
        return {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
        }

    def codex_config_override(self) -> str:
        command = _toml_string(self.command)
        args = ", ".join(_toml_string(value) for value in self.args)
        environment = ", ".join(
            f"{name} = {_toml_string(value)}" for name, value in sorted(self.env.items())
        )
        # Codex treats unannotated or effectful MCP tools as approval-requiring.
        # This server is not the unrestricted ReplayLab Host: it is a fresh
        # per-task filter with an exact allow-list, effect ceiling, frozen UI
        # scope and call budget.  The initiating ReplayLab intent is therefore
        # the approval boundary, while the filter remains the enforcement
        # boundary.  Keep this trust override scoped to this one inline server.
        return (
            "mcp_servers.ReplayLab={ command = "
            + command
            + ", args = ["
            + args
            + "], env = {"
            + environment
            + "}, required = true, startup_timeout_sec = "
            + str(_CODEX_TASK_MCP_STARTUP_TIMEOUT_SECONDS)
            + ", tool_timeout_sec = "
            + str(_CODEX_TASK_MCP_TOOL_TIMEOUT_SECONDS)
            + ', default_tools_approval_mode = "approve" }'
        )


def build_native_mcp_route(
    context: ProviderContext,
    *,
    workspace: Path,
    event_root: Path,
    allowed_effects: Sequence[str],
) -> NativeMCPRoute | None:
    """Build the one direct ReplayLab MCP route, or none for capture-free chat."""

    if not context.instance_id:
        return None
    effects = frozenset(str(value).strip() for value in allowed_effects if str(value).strip())
    if "replay" in effects:
        effects = effects | {"workspace"}
    allowed_tools = child_tool_grant(tuple(effects)) | {REPORT_TOOL}
    registry = workspace / f"live-edit-registry-{uuid.uuid4().hex}.json"
    host = find_replaylab_mcp(context.mcp_executable)
    arguments: list[str] = [
        "-I",
        "-m",
        "replaylab_agent.mcp_proxy",
        "--host",
        str(host),
        "--host-arg",
        "stdio",
        "--instance-id",
        context.instance_id,
        "--registry-path",
        str(registry),
        "--scope-json",
        context.scope().to_json(),
        "--max-tool-calls",
        str(context.max_tool_calls),
    ]
    for name in sorted(allowed_tools):
        arguments.extend(("--allow", name))
    for effect in sorted(effects):
        arguments.extend(("--effect", effect))
    environment = {
        TASK_ID_ENV: context.task_id,
        TASK_EVENT_ROOT_ENV: str(event_root),
    }
    for variable in ("REPLAYLAB_REPORT_MAX_ROUNDS", "REPLAYLAB_REVIEW_MAX_ROUNDS", "REPLAYLAB_VERIFY_MAX_ROUNDS"):
        report_rounds = os.environ.get(variable, "").strip()
        if report_rounds:
            environment[variable] = report_rounds
    return NativeMCPRoute(
        command=sys.executable,
        args=tuple(arguments),
        env=environment,
        allowed_tools=frozenset(allowed_tools),
        effects=effects,
        registry_path=registry,
    )


def native_codex_argv(argv: Sequence[str], route: NativeMCPRoute | None) -> list[str]:
    hardened = harden_codex_argv(argv)
    if route is None:
        return hardened
    try:
        index = hardened.index("app-server")
    except ValueError:  # harden_codex_argv already reports this; defensive only.
        return hardened
    return [
        *hardened[:index],
        "-c",
        route.codex_config_override(),
        *hardened[index:],
    ]


def _toml_string(value: str) -> str:
    # JSON strings are valid TOML basic strings for the characters used in an
    # argv/environment declaration and correctly escape Windows backslashes.
    return json.dumps(str(value), ensure_ascii=False)


__all__ = ["NativeMCPRoute", "build_native_mcp_route", "native_codex_argv"]
