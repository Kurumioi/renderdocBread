"""Small task-scoped stdio filter in front of ReplayLabMCP.

The filter owns no workflow, receipt, recovery, or planning state. It exposes
the explicit tool allow-list, enforces the connection effect ceiling and call
budget, checks arguments against an optional frozen native-UI target, and
forwards the call to the first-party Host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import ConfigurationError, PolicyError
from .policy import (
    WORKFLOW_EFFECTS,
    tool_call_effects,
    tool_effects,
    validate_registered_tool_effect_manifest,
)
from .process_env import host_process_environment
from .agent_report import REPORT_TOOL, TaskReportStore
from .tool_scope import ToolCallScope


_INSTANCE_ENV = "REPLAYLAB_INSTANCE_ID"
_REGISTRY_ENV = "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY"
_TASK_ENV_NAMES = (
    "REPLAYLAB_TASK_ID",
    "REPLAYLAB_TASK_EVENT_ROOT",
    "REPLAYLAB_REPORT_MAX_ROUNDS",
    "REPLAYLAB_REVIEW_MAX_ROUNDS",
    "REPLAYLAB_VERIFY_MAX_ROUNDS",
)
_MAX_PROXY_TOOL_CALLS = 1024
_MAX_ERROR_DETAIL_CHARS = 4096


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replaylab-agent-mcp-filter",
        description="Expose one task-scoped subset of ReplayLab MCP tools",
    )
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--host-arg", action="append", default=[])
    parser.add_argument("--host-args-env", default="")
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--allow-env", default="")
    parser.add_argument("--effect", action="append", default=[])
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--registry-path", default="")
    parser.add_argument("--scope-json", default="")
    parser.add_argument("--max-tool-calls", type=int, default=480)
    return parser


def _exception_detail(error: BaseException) -> str:
    """Expose bounded leaf causes hidden by AnyIO task-group wrappers."""

    pending: list[BaseException] = [error]
    details: list[str] = []
    while pending and len(details) < 8:
        current = pending.pop()
        nested = getattr(current, "exceptions", ())
        if isinstance(nested, tuple) and nested:
            pending.extend(reversed(nested))
            continue
        detail = (str(current).strip() or type(current).__name__).replace("\0", "�")
        if detail not in details:
            details.append(detail[:1024])
    rendered = " | ".join(details) or type(error).__name__
    return rendered[:_MAX_ERROR_DETAIL_CHARS]


def _json_string_list(raw: str, label: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{label} must contain a JSON string array") from exc
    if (
        not isinstance(value, list)
        or len(value) > _MAX_PROXY_TOOL_CALLS
        or any(
            not isinstance(item, str)
            or not item.strip()
            or "\0" in item
            or len(item) > 8192
            for item in value
        )
    ):
        raise ConfigurationError(f"{label} must contain a bounded JSON string array")
    return tuple(item.strip() for item in value)


def _selected_values(
    direct: Sequence[str], environment_name: str, label: str
) -> frozenset[str]:
    values = {str(item).strip() for item in direct if str(item).strip()}
    if environment_name:
        raw = os.environ.get(environment_name, "")
        if raw:
            values.update(_json_string_list(raw, environment_name))
    if not values:
        raise ConfigurationError(f"provide an explicit {label}")
    return frozenset(values)


def _host_arguments(direct: Sequence[str], environment_name: str) -> tuple[str, ...]:
    if environment_name and os.environ.get(environment_name, ""):
        return _json_string_list(os.environ[environment_name], environment_name)
    values = tuple(str(item) for item in direct) or ("stdio",)
    if len(values) > 32 or any(not item or "\0" in item or len(item) > 8192 for item in values):
        raise ConfigurationError("ReplayLab MCP Host arguments are invalid")
    return values


def _validate_configuration(args: argparse.Namespace) -> tuple[
    Path,
    tuple[str, ...],
    frozenset[str],
    frozenset[str],
    ToolCallScope,
    dict[str, str],
]:
    host = args.host.expanduser()
    if not host.is_absolute() or not host.is_file() or host.is_symlink():
        raise ConfigurationError("ReplayLab MCP Host must be an absolute regular file")
    host = host.resolve()
    allowed = _selected_values(args.allow, args.allow_env, "tool allow-list")
    effects = frozenset(str(item).strip() for item in args.effect if str(item).strip())
    unknown = effects - WORKFLOW_EFFECTS
    if unknown:
        raise ConfigurationError(
            "unknown ReplayLab effect class(es): " + ", ".join(sorted(unknown))
        )
    if "replay" in effects and "workspace" not in effects:
        raise ConfigurationError("the replay effect requires workspace")
    excessive = {
        name: tool_effects(name) - effects
        for name in allowed
        if tool_effects(name) - effects
    }
    if excessive:
        raise ConfigurationError(
            "tool allow-list exceeds the effect ceiling: "
            + ", ".join(sorted(excessive))
        )
    if not 1 <= args.max_tool_calls <= _MAX_PROXY_TOOL_CALLS:
        raise ConfigurationError(
            f"ReplayLab MCP call budget must be between 1 and {_MAX_PROXY_TOOL_CALLS}"
        )
    scope = ToolCallScope.from_json(str(args.scope_json or ""))
    instance_id = str(args.instance_id or os.environ.get(_INSTANCE_ENV, "")).strip()
    registry_raw = str(args.registry_path or os.environ.get(_REGISTRY_ENV, "")).strip()
    if not instance_id or "\0" in instance_id or len(instance_id) > 512:
        raise ConfigurationError("a valid pinned qrenderdoc instance id is required")
    registry = Path(registry_raw).expanduser()
    if (
        not registry_raw
        or not registry.is_absolute()
        or registry.exists()
        or registry.is_symlink()
    ):
        raise ConfigurationError(
            "a fresh absolute live-edit registry path is required"
        )
    extra = {_INSTANCE_ENV: instance_id, _REGISTRY_ENV: str(registry)}
    for name in _TASK_ENV_NAMES:
        value = os.environ.get(name, "")
        if value:
            extra[name] = value
    return (
        host,
        _host_arguments(args.host_arg, args.host_args_env),
        allowed,
        effects,
        scope,
        host_process_environment(extra),
    )


async def serve_filtered_proxy(
    host: Path,
    host_args: Sequence[str],
    allowed_tools: frozenset[str],
    *,
    effects: frozenset[str],
    scope: ToolCallScope,
    host_env: dict[str, str],
    max_tool_calls: int,
) -> None:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise ConfigurationError(
            "the ReplayLab MCP filter requires the MCP 1.x runtime"
        ) from exc

    async with stdio_client(
        StdioServerParameters(
            command=str(host),
            args=list(host_args),
            env=host_env,
        )
    ) as streams:
        async with ClientSession(*streams) as upstream:
            await upstream.initialize()
            listing = await upstream.list_tools()
            upstream_by_name = {
                str(tool.name): tool for tool in list(getattr(listing, "tools", ()) or ())
            }
            validate_registered_tool_effect_manifest(upstream_by_name)
            missing = allowed_tools - set(upstream_by_name)
            if missing:
                raise ConfigurationError(
                    "ReplayLab MCP Host is missing allowed tool(s): "
                    + ", ".join(sorted(missing))
                )
            task_tools = {}
            for name in allowed_tools:
                tool = upstream_by_name[name]
                declared_effects = tool_effects(name)
                read_only = not declared_effects
                existing = getattr(tool, "annotations", None)
                task_tools[name] = tool.model_copy(
                    update={
                        "annotations": ToolAnnotations(
                            title=getattr(existing, "title", None),
                            readOnlyHint=read_only,
                            destructiveHint=not read_only,
                            idempotentHint=read_only,
                            openWorldHint=False,
                        )
                    }
                )
            server = Server(
                "ReplayLab Task Filter",
                version=__version__,
                instructions="Only tools authorized by this ReplayLab connection are exposed.",
            )
            lock = asyncio.Lock()
            calls = 0

            @server.list_tools()
            async def list_tools() -> list[Any]:
                return [task_tools[name] for name in sorted(allowed_tools)]

            # Count malformed calls too. Ordinary argument validation remains
            # in the upstream Host; report validation belongs to its shared
            # persistent gate so schema failures consume report rounds.
            @server.call_tool(validate_input=False)
            async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
                nonlocal calls
                if name not in allowed_tools:
                    raise PolicyError(f"tool '{name}' is outside the active task grant")
                if not isinstance(arguments, dict):
                    raise PolicyError("ReplayLab MCP tool arguments must be an object")
                actual_effects = tool_call_effects(name, arguments)
                if not actual_effects.issubset(effects):
                    raise PolicyError(
                        f"tool '{name}' exceeds the active connection effect ceiling"
                    )
                scope.validate(name, arguments)
                async with lock:
                    if calls >= max_tool_calls:
                        raise PolicyError("ReplayLab MCP task exceeded its tool-call budget")
                    calls += 1
                if name == REPORT_TOOL:
                    from mcp.types import CallToolResult, TextContent
                    verdict = TaskReportStore.from_environment(host_env).submit(arguments)
                    encoded = json.dumps(verdict, ensure_ascii=False)
                    return CallToolResult(content=[TextContent(type="text", text=encoded)],
                                          structuredContent={"result": encoded}, isError=False)
                return await upstream.call_tool(name, arguments)

            options = InitializationOptions(
                server_name="replaylab-task-filter",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions="Only tools authorized by this ReplayLab connection are exposed.",
            )
            async with stdio_server() as downstream:
                await server.run(*downstream, options)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass
    logging.basicConfig(level=logging.WARNING)
    try:
        args = _parser().parse_args(argv)
        host, host_args, allowed, effects, scope, host_env = _validate_configuration(args)
        asyncio.run(
            serve_filtered_proxy(
                host,
                host_args,
                allowed,
                effects=effects,
                scope=scope,
                host_env=host_env,
                max_tool_calls=args.max_tool_calls,
            )
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error(
            "ReplayLab MCP filter failed: %s", _exception_detail(exc)
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "serve_filtered_proxy"]
