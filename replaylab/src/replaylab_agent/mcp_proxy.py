"""A small official-SDK MCP proxy that exposes one Skill allow-list.

This process is primarily used by ACP agents, whose session API accepts an MCP
stdio server but does not itself provide per-tool filtering.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

from . import __version__
from .errors import ConfigurationError, PolicyError
from .mcp_tools import evidence_digest, tool_result_error, verify_live_tool_scope
from .policy import WORKFLOW_EFFECTS, ToolCallScope, WorkflowGuard, tool_effects
from .process_env import host_process_environment


_INSTANCE_ENV = "REPLAYLAB_INSTANCE_ID"
_REGISTRY_ENV = "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY"
_SCOPE_ENV = "REPLAYLAB_TOOL_SCOPE"
_AUDIT_ENV = "REPLAYLAB_TOOL_AUDIT"


async def serve_filtered_proxy(
    upstream_command: Path | str,
    upstream_args: Sequence[str],
    allowed_tools: set[str] | frozenset[str],
    *,
    allow_empty: bool = False,
    host_env: dict[str, str] | None = None,
    scope: ToolCallScope | None = None,
    audit_file: Path | None = None,
    requested_effects: set[str] | frozenset[str] | None = None,
) -> None:
    allowed = frozenset(name.strip() for name in allowed_tools if name.strip())
    if not allowed and not allow_empty:
        raise ConfigurationError("the filtered MCP proxy requires at least one allowed tool")
    workflow_guard = WorkflowGuard(
        (
            requested_effects
            if requested_effects is not None
            else {
                effect
                for tool in allowed
                for effect in tool_effects(tool)
            }
        )
    )
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise ConfigurationError(
            "filtered external-Agent MCP support requires the 'mcp' package; "
            "install replaylab-agenthost[acp]"
        ) from exc

    async def run_server(server: Server) -> None:
        options = InitializationOptions(
            server_name="replaylab-skill-filter",
            server_version=__version__,
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
            instructions="Only tools authorized by the active ReplayLab Agent Skill.",
        )
        async with stdio_server() as (client_read, client_write):
            await server.run(client_read, client_write, options)

    # Codex loads configured MCP servers outside ReplayLab turns as well.  An
    # inherited allow-env with no grant must remain a true zero-tool server and
    # must not even launch the upstream Host (whose legacy startup performs
    # live-edit recovery).
    if not allowed:
        server = Server(
            "ReplayLab Skill Filter",
            version=__version__,
            instructions="No ReplayLab tools are granted for this process.",
        )

        @server.list_tools()
        async def list_empty_tools():
            return []

        @server.call_tool()
        async def reject_empty_call(name: str, arguments: dict):
            del arguments
            raise ConfigurationError(f"tool '{name}' is outside the active Skill grant")

        await run_server(server)
        return

    upstream = StdioServerParameters(
        command=str(upstream_command),
        args=[str(value) for value in upstream_args],
        env=host_process_environment(host_env),
    )
    async with stdio_client(upstream) as (upstream_read, upstream_write):
        async with ClientSession(upstream_read, upstream_write) as session:
            await session.initialize()
            listed = await session.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            available = frozenset(allowed & set(by_name))
            server = Server(
                "ReplayLab Skill Filter",
                version=__version__,
                instructions=(
                    "Task-scoped ReplayLab tools discovered from the live first-party Host. "
                    "A missing optional tool is omitted; requested workflow outcomes still "
                    "require Host terminal acceptance."
                ),
            )

            @server.list_tools()
            async def list_tools():
                return [by_name[name] for name in sorted(available)]

            @server.call_tool()
            async def call_tool(name: str, arguments: dict):
                if name not in available:
                    raise ConfigurationError(
                        f"tool '{name}' is not available in this task's live Host manifest"
                    )
                return await _call_upstream_tool(
                    session.call_tool,
                    name,
                    arguments,
                    scope=scope,
                    audit_file=audit_file,
                    workflow_guard=workflow_guard,
                )

            try:
                await run_server(server)
            finally:
                # The parent AgentHost cannot share this in-process guard with
                # an ACP child. Emit one Host-owned terminal receipt after the
                # client session closes so a text answer cannot hide an
                # uncertain mutation or an incomplete requested effect.
                terminal_error = ""
                try:
                    workflow_guard.validate_completion()
                except Exception as exc:
                    terminal_error = type(exc).__name__
                _append_workflow_terminal(
                    audit_file,
                    workflow_guard,
                    terminal_error=terminal_error,
                )


async def _call_upstream_tool(
    call_tool,
    name: str,
    arguments: dict,
    *,
    scope: ToolCallScope | None,
    audit_file: Path | None,
    workflow_guard: WorkflowGuard | None = None,
):
    """Run one filtered tool call and preserve failed-call evidence.

    MCP servers encode handler exceptions for the model, so without this
    record a scope precheck, upstream exception, or postcheck failure is later
    indistinguishable from a model that never attempted a tool.  Re-raise the
    original exception unchanged after best-effort auditing.
    """

    failure = ""
    tool_call_id = "acp-" + uuid.uuid4().hex
    arguments_sha256 = evidence_digest(arguments)
    result_sha256 = ""
    try:
        if scope is not None:
            scope.validate(name, arguments)
            await verify_live_tool_scope(call_tool, scope)
        if workflow_guard is not None:
            workflow_guard.check(name, arguments)
        result = await call_tool(name, arguments)
        result_sha256 = evidence_digest(result)
        error = tool_result_error(result)
        if error:
            failure = error
            raise ConfigurationError(
                f"ReplayLab MCP tool '{name}' returned an error envelope ({error})"
            )
        if scope is not None:
            await verify_live_tool_scope(call_tool, scope)
        if workflow_guard is not None:
            workflow_guard.record_result(name, arguments, result)
    except (asyncio.CancelledError, Exception) as exc:
        if workflow_guard is not None and not isinstance(exc, PolicyError):
            workflow_guard.record_failure(name)
        if not failure:
            detail = " ".join(str(exc).split())
            failure = type(exc).__name__ + (f": {detail}" if detail else "")
            failure = failure[:2048]
        try:
            _append_audit(
                audit_file,
                name,
                "failed",
                failure,
                tool_call_id=tool_call_id,
                arguments_sha256=arguments_sha256,
                result_sha256=result_sha256 or evidence_digest(type(exc).__name__),
            )
        except ConfigurationError:
            # The tool/scope failure is the primary error.  Do not replace it
            # with a secondary audit-write failure.
            pass
        raise
    _append_audit(
        audit_file,
        name,
        "completed",
        "",
        tool_call_id=tool_call_id,
        arguments_sha256=arguments_sha256,
        result_sha256=result_sha256,
    )
    return result


def _append_audit(
    path: Path | None,
    tool: str,
    status: str,
    error: str,
    *,
    tool_call_id: str = "",
    arguments_sha256: str = "",
    result_sha256: str = "",
) -> None:
    call_id = tool_call_id or ("acp-" + uuid.uuid4().hex)
    _append_audit_record(
        path,
        {
            "tool": tool,
            "status": status,
            "error": error,
            "toolCallId": call_id,
            "argumentsSha256": arguments_sha256 or evidence_digest({}),
            "resultSha256": result_sha256 or evidence_digest(error or status),
        },
    )


def _append_workflow_terminal(
    path: Path | None,
    guard: WorkflowGuard,
    *,
    terminal_error: str,
) -> None:
    _append_audit_record(
        path,
        {
            "event": "workflow-terminal",
            "status": "failed" if terminal_error else "completed",
            "requestedEffects": sorted(guard.requested_effects),
            "completedEffects": sorted(guard.completed_effects),
            "completedTools": list(guard.completed),
            "mutationUncertain": guard.mutation_uncertain,
            "replayPostconditionObserved": guard.replay_postcondition_observed,
            "errorType": terminal_error,
        },
    )


def _append_audit_record(path: Path | None, value: dict[str, object]) -> None:
    if path is None:
        return
    record = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(record + "\n")
            stream.flush()
    except OSError as exc:
        raise ConfigurationError("cannot write ReplayLab MCP audit evidence") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ReplayLab filtered MCP stdio proxy")
    parser.add_argument("--host", required=True, type=Path)
    parser.add_argument("--host-arg", action="append", default=[])
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument(
        "--effect",
        action="append",
        default=None,
        help="semantic workflow effect requested by the Host for this task",
    )
    parser.add_argument(
        "--allow-env",
        default="",
        help="read a JSON array of tool names from this inherited environment variable",
    )
    parser.add_argument(
        "--host-args-env",
        default="",
        help="read a JSON array of upstream host arguments from this environment variable",
    )
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--registry-path", default="")
    parser.add_argument("--scope-json", default="")
    parser.add_argument(
        "--scope-env",
        default=_SCOPE_ENV,
        help="read a compact frozen tool scope from this environment variable",
    )
    parser.add_argument("--audit-file", type=Path, default=None)
    return parser


def _allowed_from_args(values: Sequence[str], environment_name: str) -> frozenset[str]:
    result = {str(value).strip() for value in values if str(value).strip()}
    if environment_name:
        raw = os.environ.get(environment_name, "")
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{environment_name} must contain a JSON array of tool names"
                ) from exc
            if not isinstance(decoded, list) or any(
                not isinstance(value, str) or not value.strip() for value in decoded
            ):
                raise ConfigurationError(
                    f"{environment_name} must contain a JSON array of non-empty tool names"
                )
            result.update(value.strip() for value in decoded)
    if not result and not environment_name:
        raise ConfigurationError("provide --allow or --allow-env")
    return frozenset(result)


def _host_args_from_args(values: Sequence[str], environment_name: str) -> tuple[str, ...]:
    decoded: object = None
    if environment_name:
        raw = os.environ.get(environment_name, "")
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{environment_name} must contain a JSON array of host arguments"
                ) from exc
    selected: Sequence[object]
    if decoded is not None:
        if not isinstance(decoded, list):
            raise ConfigurationError(
                f"{environment_name} must contain a JSON array of host arguments"
            )
        selected = decoded
    else:
        selected = values or ("stdio",)
    if (
        len(selected) > 32
        or any(
            not isinstance(value, str)
            or not value
            or "\0" in value
            or len(value) > 8192
            for value in selected
        )
    ):
        raise ConfigurationError("ReplayLab MCP host arguments are invalid")
    return tuple(str(value) for value in selected)


def _workflow_effects_from_args(values: Sequence[str]) -> frozenset[str]:
    effects = frozenset(str(value).strip() for value in values if str(value).strip())
    unknown = effects - WORKFLOW_EFFECTS
    if unknown:
        raise ConfigurationError(
            "unknown ReplayLab workflow effect(s): " + ", ".join(sorted(unknown))
        )
    if "replay" in effects and "workspace" not in effects:
        raise ConfigurationError("replay workflow effect requires workspace")
    return effects


def main(argv: Sequence[str] | None = None) -> int:
    # The MCP SDK owns stdio framing, but the interpreter still chooses the
    # redirected stream encoding. Make the contract explicit for direct
    # invocations that did not inherit ReplayLab's process environment.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        allowed = _allowed_from_args(args.allow, args.allow_env)
        requested_effects = (
            None
            if args.effect is None
            else _workflow_effects_from_args(args.effect)
        )
        host_args = _host_args_from_args(args.host_arg, args.host_args_env)
        if allowed:
            instance_id = str(args.instance_id or os.environ.get(_INSTANCE_ENV, "")).strip()
            registry_raw = str(
                args.registry_path or os.environ.get(_REGISTRY_ENV, "")
            ).strip()
            if not instance_id or "\0" in instance_id or len(instance_id) > 512:
                raise ConfigurationError("a valid pinned qrenderdoc instance id is required")
            registry_path = Path(registry_raw).expanduser()
            if (
                not registry_raw
                or not registry_path.is_absolute()
                or registry_path.exists()
            ):
                raise ConfigurationError(
                    "a fresh, absolute, nonexistent live-edit registry path is required"
                )
            scope_raw = str(
                args.scope_json
                or (os.environ.get(args.scope_env, "") if args.scope_env else "")
            )
            scope = ToolCallScope.from_json(scope_raw)
            audit_file = args.audit_file
            if audit_file is None:
                audit_raw = os.environ.get(_AUDIT_ENV, "").strip()
                audit_file = Path(audit_raw) if audit_raw else None
            if audit_file is not None and not audit_file.is_absolute():
                raise ConfigurationError("ReplayLab MCP audit path must be absolute")
            host_env = {
                _INSTANCE_ENV: instance_id,
                _REGISTRY_ENV: str(registry_path),
            }
        else:
            scope = ToolCallScope()
            audit_file = None
            host_env = None
        asyncio.run(
            serve_filtered_proxy(
                args.host,
                host_args,
                allowed,
                allow_empty=bool(args.allow_env),
                host_env=host_env,
                scope=scope,
                audit_file=audit_file,
                requested_effects=requested_effects,
            )
        )
    except (ConfigurationError, OSError) as exc:
        logging.error("filtered MCP proxy failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
