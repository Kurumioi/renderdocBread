"""ReplayLab MCP discovery and per-skill filtering for ACP/external Agents.

Model API and Codex dynamicTools turns execute the same contracts through the
in-process InternalToolService. ACP v1 has no equivalent Host callback, so this
module gives that provider exactly one task-filtered ReplayLabMCP adapter instead
of handing it every RenderDoc operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import json
import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigurationError, LifecycleCleanupError, PolicyError
from .events import TaskEventWriter
from .policy import ToolCallScope, WorkflowGuard
from .process_env import host_process_environment


_MCP_CLEANUP_TIMEOUT = 2.0


async def _bounded_toolset_exit(awaitable: Any) -> bool | None:
    """Bound MCP teardown independently from cancellation of the Agent run."""

    cleanup_task: asyncio.Future[Any] | None = None
    try:
        cleanup_task = asyncio.ensure_future(awaitable)
        return await asyncio.wait_for(
            asyncio.shield(cleanup_task), timeout=_MCP_CLEANUP_TIMEOUT
        )
    except LifecycleCleanupError:
        raise
    except (asyncio.TimeoutError, asyncio.CancelledError) as cleanup_error:
        if cleanup_task is not None:
            cleanup_task.cancel()

            def consume(task: asyncio.Future[Any]) -> None:
                with contextlib.suppress(BaseException):
                    task.exception()

            cleanup_task.add_done_callback(consume)
        raise LifecycleCleanupError(
            "ReplayLab MCP specialist transport cleanup was not confirmed within the deadline"
        ) from cleanup_error
    except Exception as cleanup_error:
        raise LifecycleCleanupError(
            "ReplayLab MCP specialist transport cleanup failed"
        ) from cleanup_error


@dataclass(slots=True)
class ToolAudit:
    """In-memory evidence used by the host's minimum Skill acceptance gate."""

    started: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    evidence: list["ToolEvidenceRecord"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolEvidenceRecord:
    """Bounded host-owned digest for context compaction and acceptance receipts."""

    tool_call_id: str
    tool: str
    arguments_sha256: str
    result_sha256: str
    outcome: Literal["completed", "failed"]


def evidence_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    except (TypeError, ValueError, OverflowError):
        encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def find_replaylab_mcp(explicit: Path | str | None = None) -> Path:
    """Resolve the existing MCP host without starting it."""

    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    configured = os.environ.get("REPLAYLAB_MCP_HOST", "").strip()
    if configured:
        candidates.append(configured)
    development_host = repository_root() / "x64" / "Development" / "replaylab"
    # A running Windows executable cannot be atomically replaced.  Prefer the
    # side-by-side Unicode build so existing Codex/qrenderdoc sessions may keep
    # their old image mapped while every new Agent task gets the fixed host.
    candidates.extend(
        [
            str(development_host / "ReplayLabMCP.unicode.exe"),
            str(development_host / "ReplayLabMCP.exe"),
        ]
    )
    on_path = (
        shutil.which("ReplayLabMCP.unicode.exe")
        or shutil.which("ReplayLabMCP.exe")
        or shutil.which("replaylab-mcp")
    )
    if on_path:
        candidates.append(on_path)

    checked: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        checked.append(str(path))
        if path.is_file():
            return path.resolve()
    raise ConfigurationError(
        "ReplayLabMCP host was not found; set REPLAYLAB_MCP_HOST. Checked: "
        + ", ".join(checked)
    )


def build_mcp_toolset(
    allowed_tools: set[str] | frozenset[str],
    *,
    writer: TaskEventWriter | None = None,
    executable: Path | str | None = None,
    host_args: Sequence[str] = ("stdio",),
    host_env: Mapping[str, str] | None = None,
    scope: ToolCallScope | None = None,
    audit: ToolAudit | None = None,
    plan_task_id: str = "",
    skill_name: str = "",
    workflow_guard: WorkflowGuard | None = None,
) -> Any:
    """Build a lazy stdio MCP toolset filtered to one skill's grants."""

    allowed = frozenset(name.strip() for name in allowed_tools if name.strip())
    if not allowed:
        raise ConfigurationError("an Agent skill must grant at least one ReplayLab MCP tool")
    host = find_replaylab_mcp(executable)
    arguments = tuple(str(value) for value in host_args)
    if not arguments or any(not value or "\0" in value for value in arguments):
        raise ConfigurationError("ReplayLab MCP host arguments are invalid")
    selected_env = dict(host_env or {})
    instance_id = str(selected_env.get("REPLAYLAB_INSTANCE_ID", "")).strip()
    registry_raw = str(
        selected_env.get("RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY", "")
    ).strip()
    registry = Path(registry_raw).expanduser()
    if not instance_id or "\0" in instance_id or len(instance_id) > 512:
        raise ConfigurationError("ReplayLab MCP requires a pinned qrenderdoc instance id")
    if not registry_raw or not registry.is_absolute() or registry.exists():
        raise ConfigurationError(
            "ReplayLab MCP requires a fresh, absolute, nonexistent live-edit registry path"
        )
    try:
        from fastmcp.client.transports import StdioTransport
        from pydantic_ai.mcp import MCPToolset
        from pydantic_ai.toolsets import WrapperToolset
    except ImportError as exc:
        raise ConfigurationError(
            "model Agent support is not installed; install replaylab-agenthost[model]"
        ) from exc

    async def process_tool_call(ctx: Any, call_tool: Any, name: str, tool_args: dict[str, Any]) -> Any:
        if name not in allowed:
            # Filtering should make this unreachable, but keep a second gate at
            # execution time in case a provider replays a stale tool call.
            raise PolicyError(f"tool '{name}' is outside the active skill grant")
        if scope is not None:
            scope.validate(name, tool_args)
        if workflow_guard is not None:
            workflow_guard.check(name, tool_args)
        if audit is not None:
            audit.started.append(name)
        tool_call_id = str(getattr(ctx, "tool_call_id", "") or "")[:256]
        arguments_digest = evidence_digest(tool_args)
        call_activity_id = (
            f"{plan_task_id or skill_name or 'tool'}:{name}:{uuid.uuid4().hex[:12]}"
        )[:512]
        if writer:
            event_data: dict[str, Any] = {
                "argumentNames": sorted(str(key) for key in tool_args),
                "activityId": call_activity_id,
            }
            if plan_task_id:
                event_data["planTaskId"] = plan_task_id
            if skill_name:
                event_data["skill"] = skill_name
            writer.emit(
                "tool-started",
                tool=name,
                data=event_data,
            )
        try:
            if scope is not None:
                await verify_live_tool_scope(call_tool, scope)
            result = await call_tool(name, tool_args)
            error = tool_result_error(result)
            if error:
                raise ConfigurationError(
                    f"ReplayLab MCP tool '{name}' returned an error envelope ({error})"
                )
            if scope is not None:
                await verify_live_tool_scope(call_tool, scope)
            if workflow_guard is not None:
                workflow_guard.record_result(name, tool_args, result)
        except Exception as exc:
            if workflow_guard is not None and not isinstance(exc, PolicyError):
                workflow_guard.record_failure(name)
            if audit is not None:
                audit.failed.append(name)
                audit.evidence.append(
                    ToolEvidenceRecord(
                        tool_call_id=tool_call_id,
                        tool=name,
                        arguments_sha256=arguments_digest,
                        result_sha256=evidence_digest(type(exc).__name__),
                        outcome="failed",
                    )
                )
            if writer:
                event_data = {
                    "status": "failed",
                    "errorType": type(exc).__name__,
                    "activityId": call_activity_id,
                }
                if plan_task_id:
                    event_data["planTaskId"] = plan_task_id
                if skill_name:
                    event_data["skill"] = skill_name
                writer.emit(
                    "tool-completed",
                    tool=name,
                    summary="tool failed",
                    data=event_data,
                )
            raise
        if audit is not None:
            audit.completed.append(name)
            audit.evidence.append(
                ToolEvidenceRecord(
                    tool_call_id=tool_call_id,
                    tool=name,
                    arguments_sha256=arguments_digest,
                    result_sha256=evidence_digest(result),
                    outcome="completed",
                )
            )
        if writer:
            event_data = {
                "status": "completed",
                "activityId": call_activity_id,
            }
            if plan_task_id:
                event_data["planTaskId"] = plan_task_id
            if skill_name:
                event_data["skill"] = skill_name
            writer.emit("tool-completed", tool=name, data=event_data)
        return result

    toolset = MCPToolset(
        StdioTransport(
            command=str(host),
            args=list(arguments),
            env=host_process_environment(selected_env),
        ),
        process_tool_call=process_tool_call,
        tool_error_behavior="failed",
    )

    class _LifecycleGuardToolset(WrapperToolset):
        """Make an uncertain MCP child shutdown distinguishable from task failure."""

        async def __aexit__(self, *args: Any) -> bool | None:
            return await _bounded_toolset_exit(super().__aexit__(*args))

    filtered = toolset.filtered(lambda _ctx, definition: definition.name in allowed)
    return _LifecycleGuardToolset(filtered)


def tool_result_error(result: Any) -> str:
    """Return a bounded failure kind for MCP or Host JSON error envelopes."""

    if getattr(result, "is_error", False) is True:
        return "mcp-is-error"
    if isinstance(result, dict) and result.get("isError") is True:
        return "mcp-is-error"

    candidates: list[Any] = [result]
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, (list, tuple)):
        candidates.extend(content)
    elif content is not None:
        candidates.append(content)

    for candidate in candidates:
        value: Any = candidate
        if not isinstance(value, (str, dict)):
            value = getattr(candidate, "text", None)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or len(stripped) > 16 * 1024 * 1024:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict) and value.get("error") not in (None, "", False, {}):
            return "host-error-envelope"
    return ""


def tool_result_json_object(result: Any) -> dict[str, Any] | None:
    """Decode the first JSON object returned as MCP text content."""

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    candidates: list[Any] = []
    if isinstance(content, (list, tuple)):
        candidates.extend(content)
    elif content is not None:
        candidates.append(content)
    if not candidates or not (isinstance(result, dict) and "content" in result):
        candidates.append(result)
    for candidate in candidates:
        value: Any = candidate
        if not isinstance(value, (str, dict)):
            value = getattr(candidate, "text", None)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    return None


async def verify_live_tool_scope(call_tool: Any, scope: ToolCallScope) -> None:
    """Check capture identity immediately before/after a scoped tool call."""

    if not scope.capture_session_id:
        return
    result = await call_tool("get_current_renderdoc_context", {})
    error = tool_result_error(result)
    if error:
        raise ConfigurationError(
            f"cannot verify the pinned qrenderdoc capture ({error})"
        )
    context = tool_result_json_object(result)
    if context is None:
        raise ConfigurationError("qrenderdoc capture verification returned no JSON object")
    scope.validate_live_context(context)


__all__ = [
    "ToolAudit",
    "ToolEvidenceRecord",
    "build_mcp_toolset",
    "evidence_digest",
    "find_replaylab_mcp",
    "repository_root",
    "tool_result_error",
    "tool_result_json_object",
    "verify_live_tool_scope",
]
