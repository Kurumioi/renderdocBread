"""Safe, minimal client for ``codex app-server``.

The connector intentionally delegates authentication to the official Codex
process.  It never reads Codex/ChatGPT token files, browser cookies, or any
other authentication cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import AuthenticationError, ConfigurationError, ConnectorError, PolicyError

DeltaCallback = Callable[[str], Any]
ActivityCallback = Callable[[str, float], Any]
TimelineCallback = Callable[[Mapping[str, Any]], Any]
DynamicToolHandler = Callable[[str, dict[str, Any], str], Awaitable[Any]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
_MAX_JSONL_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_EXTERNAL_RESPONSE_CHARS = 1024 * 1024
MAX_EXTERNAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_NOTIFICATION_QUEUE = 256
_MAX_SERVER_REQUEST_TASKS = 16
_MAX_DYNAMIC_TOOL_CALLS = 1024
_MAX_TIMELINE_ITEMS = 128
_MAX_TIMELINE_ID_CHARS = 256
_MAX_TIMELINE_TEXT_CHARS = 4096
_MAX_COMMAND_CHARS = 2048
_MAX_COMMAND_OUTPUT_CHARS = 8192
_MAX_COMMAND_OUTPUT_RAW_CHARS = 16 * 1024
_MAX_VISIBLE_COMMAND_OUTPUT_CHARS = 1200
_MAX_REASONING_SUMMARY_CHARS = 8192
_MAX_PLAN_CHARS = 8192
_MAX_PLAN_STEPS = 32
_MAX_PLAN_STEP_CHARS = 512
_MAX_STRUCTURED_DETAIL_CHARS = 4096
_SENSITIVE_NAME = (
    r"(?:api[_-]?key|key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|cookie|"
    r"credential|pass(?:word|wd)?|secret|token)"
)
_QUOTED_SENSITIVE_RE = re.compile(
    rf"(?i)([\"'](?:[A-Za-z0-9]+[_.-])*{_SENSITIVE_NAME}"
    r"(?:[_.-][A-Za-z0-9]+)*[\"']\s*:\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*')"
)
_ASSIGNED_SENSITIVE_RE = re.compile(
    rf"(?i)((?:--)?(?<![A-Za-z0-9]){_SENSITIVE_NAME}(?![A-Za-z0-9])\s*(?:=|:)\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&|]+)"
)
_SPACED_SENSITIVE_RE = re.compile(
    rf"(?i)((?:--)?(?<![A-Za-z0-9]){_SENSITIVE_NAME}(?![A-Za-z0-9])\s+)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&|]+)"
)
_AUTH_SCHEME_RE = re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+")
_SENSITIVE_KEY_RE = re.compile(_SENSITIVE_NAME, re.IGNORECASE)
_TOKEN_SHAPE_RE = re.compile(
    r"(?i)\b(?:sk|rk|pk|ghp|github_pat|xox[baprs])-[_A-Za-z0-9-]{8,}\b"
)
_DYNAMIC_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DYNAMIC_TOOL_SPEC_KEYS = frozenset(
    {"type", "name", "description", "inputSchema", "deferLoading"}
)
_TOOL_PHASE_IDLE = "idle"
_TOOL_PHASE_STARTING = "starting"
_TOOL_PHASE_RUNNING = "running"
_TOOL_PHASE_CLOSING = "closing"


def _valid_request_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(2**63) <= value < 2**63
    return isinstance(value, str)


def _validated_dynamic_tool_specs(
    values: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return a detached, JSON-safe function-only dynamic-tool inventory."""

    result: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
            raise ConfigurationError("Codex dynamic tool definitions must be JSON objects")
        unexpected = set(raw) - _DYNAMIC_TOOL_SPEC_KEYS
        if unexpected:
            raise ConfigurationError(
                "Codex dynamic tool definition contains unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        if raw.get("type") != "function":
            raise ConfigurationError(
                "ReplayLab supports only top-level Codex function dynamic tools"
            )
        name = raw.get("name")
        description = raw.get("description")
        input_schema = raw.get("inputSchema")
        if not isinstance(name, str) or _DYNAMIC_TOOL_NAME_RE.fullmatch(name) is None:
            raise ConfigurationError("Codex dynamic tool name is invalid")
        if not isinstance(description, str) or "\0" in description:
            raise ConfigurationError("Codex dynamic tool description must be a string")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise ConfigurationError(
                "Codex dynamic tool inputSchema must describe an object"
            )
        if "deferLoading" in raw and not isinstance(raw["deferLoading"], bool):
            raise ConfigurationError("Codex dynamic tool deferLoading must be boolean")
        try:
            encoded = json.dumps(
                dict(raw),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            normalized = json.loads(encoded)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ConfigurationError("Codex dynamic tool definition is not strict JSON") from exc
        if len(encoded.encode("utf-8")) > _MAX_JSONL_MESSAGE_BYTES:
            raise ConfigurationError("Codex dynamic tool definition exceeds the JSONL bound")
        result.append(normalized)
    return tuple(result)


def checked_response_size(text: str, chars: int = 0, encoded_bytes: int = 0) -> tuple[int, int]:
    """Apply one cumulative response budget shared by every external-Agent adapter."""

    next_chars = chars + len(text)
    next_bytes = encoded_bytes + len(text.encode("utf-8"))
    if next_chars > MAX_EXTERNAL_RESPONSE_CHARS or next_bytes > MAX_EXTERNAL_RESPONSE_BYTES:
        raise ConnectorError(
            "external Agent response exceeded the 1 Mi-character / 4 MiB safety bound"
        )
    return next_chars, next_bytes


def _redact_timeline_text(value: Any, *, limit: int = _MAX_TIMELINE_TEXT_CHARS) -> str:
    """Return a display-only, bounded string with common credentials removed.

    App-server activity is provider-controlled data.  It must be safe before it
    reaches either callbacks or the durable TaskEvent log.  This intentionally
    redacts both named assignments/CLI flags and common standalone token shapes.
    """

    if value is None:
        return ""
    text = str(value).replace("\0", "�")
    # Bound the provider-controlled input before the regular expressions run.
    # Leave a small suffix marker inside the same hard display limit.
    truncated = len(text) > limit
    if truncated:
        text = text[: max(0, limit - 14)]
    text = _AUTH_SCHEME_RE.sub(r"\1[REDACTED]", text)
    text = _QUOTED_SENSITIVE_RE.sub(r"\1\"[REDACTED]\"", text)
    text = _ASSIGNED_SENSITIVE_RE.sub(r"\1[REDACTED]", text)
    text = _SPACED_SENSITIVE_RE.sub(r"\1[REDACTED]", text)
    text = _TOKEN_SHAPE_RE.sub("[REDACTED]", text)
    if truncated:
        text += "… [truncated]"
    return text[:limit]


def _safe_structured_detail(value: Any) -> str:
    """Serialize one provider detail into a single bounded redacted string."""

    def clean(item: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return "…"
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(item.items()):
                if index >= 16:
                    result["…"] = "truncated"
                    break
                key = _redact_timeline_text(raw_key, limit=128)
                if _SENSITIVE_KEY_RE.search(key):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = clean(raw_value, depth + 1)
            return result
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            values = []
            for index, child in enumerate(item):
                if index >= 16:
                    break
                values.append(clean(child, depth + 1))
            if len(item) > 16:
                values.append("…")
            return values
        if isinstance(item, str):
            return _redact_timeline_text(item, limit=1024)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _redact_timeline_text(item, limit=1024)

    try:
        rendered = json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = _redact_timeline_text(value, limit=_MAX_STRUCTURED_DETAIL_CHARS)
    return _redact_timeline_text(rendered, limit=_MAX_STRUCTURED_DETAIL_CHARS)


def _compact_structured_detail(value: Any) -> str:
    """Render provider arguments as a short, readable parameter list.

    The activity timeline is an audit summary, not a JSON inspector.  Preserve
    useful top-level scalar values while representing nested payloads by shape
    so a large RenderDoc tool result cannot dominate the conversation.
    """

    def scalar(item: Any) -> str:
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, (int, float)):
            return str(item)
        if isinstance(item, str):
            return _redact_timeline_text(item, limit=180).replace("\n", " ")
        if isinstance(item, Mapping):
            names = [
                _redact_timeline_text(key, limit=48)
                for key in list(item.keys())[:6]
            ]
            suffix = ", …" if len(item) > 6 else ""
            return "{" + ", ".join(names) + suffix + "}"
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return f"[{len(item)} items]"
        return _redact_timeline_text(item, limit=180).replace("\n", " ")

    if isinstance(value, Mapping):
        lines: list[str] = []
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= 12:
                lines.append("… additional parameters hidden")
                break
            key = _redact_timeline_text(raw_key, limit=96)
            rendered = "[REDACTED]" if _SENSITIVE_KEY_RE.search(key) else scalar(raw_value)
            lines.append(f"{key}: {rendered}")
        return _redact_timeline_text("\n".join(lines), limit=1200)
    return _redact_timeline_text(scalar(value), limit=1200)


def _compact_result_detail(value: Any) -> str:
    """Describe an MCP result without embedding its potentially huge payload."""

    if isinstance(value, Mapping):
        status = value.get("status")
        ok = value.get("ok")
        if isinstance(status, str) and status.strip():
            return "status: " + _redact_timeline_text(status, limit=96)
        if isinstance(ok, bool):
            return "status: " + ("ok" if ok else "failed")
        fields = [
            _redact_timeline_text(key, limit=48)
            for key in list(value.keys())[:8]
            if not _SENSITIVE_KEY_RE.search(str(key))
        ]
        if fields:
            return "fields: " + ", ".join(fields)
        return "structured result received"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"{len(value)} result item(s) received"
    if value is None:
        return "completed without a result payload"
    return _redact_timeline_text(value, limit=240).replace("\n", " ")


def _normalise_activity_status(value: Any, lifecycle: str) -> str:
    status = _redact_timeline_text(value, limit=64).strip().casefold()
    if status in {"completed", "success", "succeeded"}:
        return "completed"
    if status in {"failed", "declined", "cancelled", "canceled", "error"}:
        return "failed"
    if lifecycle == "completed":
        return "completed"
    return "running"


def _item_id(value: Any) -> str:
    return _redact_timeline_text(value, limit=_MAX_TIMELINE_ID_CHARS)


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts: list[str] = []
        size = 0
        for part in value:
            text = str(part)
            parts.append(text[: max(0, _MAX_COMMAND_CHARS - size)])
            size += len(text) + 1
            if size >= _MAX_COMMAND_CHARS:
                break
        rendered = " ".join(parts)
    else:
        rendered = ""
    return _redact_timeline_text(rendered, limit=_MAX_COMMAND_CHARS)


def _summary_text(value: Any) -> str:
    """Extract only the public reasoning summary field, never raw content."""

    if isinstance(value, str):
        return value
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ""
    parts: list[str] = []
    for index, part in enumerate(value):
        if index >= 32:
            break
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping):
            # Official summary parts may be represented as {text: ...}.  Do
            # not fall back to generic serialization: adjacent fields could be
            # raw reasoning content on a future or malformed provider.
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


@dataclass(slots=True)
class _TimelineState:
    reasoning: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    command_cwds: dict[str, str] = field(default_factory=dict)
    command_output: dict[str, str] = field(default_factory=dict)
    command_output_truncated: set[str] = field(default_factory=set)
    plans: dict[str, str] = field(default_factory=dict)

    def put(self, target: dict[str, str], item_id: str, value: str) -> None:
        if item_id in target or len(target) < _MAX_TIMELINE_ITEMS:
            target[item_id] = value

    def append(self, target: dict[str, str], item_id: str, delta: str, limit: int) -> str:
        if item_id not in target and len(target) >= _MAX_TIMELINE_ITEMS:
            return ""
        current = target.get(item_id, "")
        remaining = max(0, limit - len(current))
        if remaining:
            current += delta[:remaining]
            target[item_id] = current
        return current


def _timeline_event(
    *,
    activity_id: str,
    kind: str,
    title: str,
    detail: str = "",
    status: str = "running",
    lifecycle: str = "updated",
    elapsed: float = 0.0,
    duration_ms: Any = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "activityId": _item_id(activity_id),
        "activityKind": _redact_timeline_text(kind, limit=64),
        "activityTitle": _redact_timeline_text(title, limit=256),
        "activityDetail": _redact_timeline_text(detail, limit=_MAX_TIMELINE_TEXT_CHARS),
        "status": status if status in {"running", "completed", "failed"} else "running",
        "lifecycle": lifecycle,
        "elapsedSeconds": max(0, int(elapsed)),
    }
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        try:
            bounded_duration = int(duration_ms)
        except (OverflowError, ValueError):
            bounded_duration = 0
        event["durationMs"] = max(
            0, min(bounded_duration, 7 * 24 * 60 * 60 * 1000)
        )
    return event


def _item_timeline_event(
    item: Mapping[str, Any],
    *,
    lifecycle: str,
    elapsed: float,
    state: _TimelineState,
) -> dict[str, Any] | None:
    """Convert explicitly supported public ThreadItem fields for display."""

    kind = _redact_timeline_text(item.get("type"), limit=64)
    if kind not in {
        "commandExecution",
        "mcpToolCall",
        "plan",
        "reasoning",
        "webSearch",
        "imageView",
        "contextCompaction",
    }:
        return None
    activity_id = _item_id(item.get("id", ""))
    if not activity_id:
        # Official item lifecycle events always carry an id.  Ignoring a
        # malformed item avoids accidentally merging unrelated work into one
        # unstable UI row.
        return None
    status = _normalise_activity_status(item.get("status"), lifecycle)
    duration_ms = item.get("durationMs")

    if kind == "commandExecution":
        command = _command_text(item.get("command"))
        cwd = _redact_timeline_text(item.get("cwd"), limit=1024)
        if command:
            state.put(state.commands, activity_id, command)
        else:
            command = state.commands.get(activity_id, "")
        if cwd:
            state.put(state.command_cwds, activity_id, cwd)
        else:
            cwd = state.command_cwds.get(activity_id, "")
        output = ""
        if lifecycle == "completed":
            output = _redact_timeline_text(
                item.get("aggregatedOutput", ""), limit=_MAX_VISIBLE_COMMAND_OUTPUT_CHARS
            )
        if not output:
            output = _redact_timeline_text(
                state.command_output.get(activity_id, ""),
                limit=_MAX_VISIBLE_COMMAND_OUTPUT_CHARS,
            )
        details = []
        if command:
            details.append(f"Command: {command}")
        if cwd:
            details.append(f"Working directory: {cwd}")
        if output:
            details.append(f"Output:\n{output}")
        exit_code = item.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            details.append(f"Exit code: {exit_code}")
        title = "Running command" if lifecycle != "completed" else "Command completed"
        if status == "failed":
            title = "Command failed"
        if command:
            title += f": {command[:120]}"
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title=title,
            detail="\n".join(details),
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
            duration_ms=duration_ms,
        )

    if kind == "mcpToolCall":
        server = _redact_timeline_text(item.get("server"), limit=128)
        tool = _redact_timeline_text(item.get("tool"), limit=256)
        target = ".".join(part for part in (server, tool) if part) or "MCP tool"
        details = []
        if "arguments" in item:
            arguments = _compact_structured_detail(item.get("arguments"))
            if arguments:
                details.append(f"Parameters:\n{arguments}")
        if lifecycle == "completed" and "result" in item:
            details.append(f"Result:\n{_compact_result_detail(item.get('result'))}")
        if lifecycle == "completed" and item.get("error") is not None:
            details.append(
                "Error: "
                + _redact_timeline_text(item.get("error"), limit=_MAX_STRUCTURED_DETAIL_CHARS)
            )
        title = f"Calling {target}" if lifecycle != "completed" else f"Called {target}"
        if status == "failed":
            title = f"Call failed: {target}"
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title=title,
            detail="\n".join(details),
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
            duration_ms=duration_ms,
        )

    if kind == "plan":
        text = _redact_timeline_text(item.get("text"), limit=_MAX_PLAN_CHARS)
        if text:
            state.put(state.plans, activity_id, text)
        else:
            text = state.plans.get(activity_id, "")
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title="Planning" if lifecycle != "completed" else "Plan completed",
            detail=text,
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
        )

    if kind == "reasoning":
        # This is the sole completed-item reasoning field we inspect.  In
        # particular, item["content"] is raw reasoning and is never read.
        summary = _summary_text(item.get("summary"))
        if summary:
            summary = _redact_timeline_text(summary, limit=_MAX_REASONING_SUMMARY_CHARS)
            state.put(state.reasoning, activity_id, summary)
        else:
            summary = state.reasoning.get(activity_id, "")
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title="Reasoning" if lifecycle != "completed" else "Reasoning completed",
            detail=summary,
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
        )

    if kind == "webSearch":
        query = _redact_timeline_text(item.get("query"), limit=2048)
        action = _safe_structured_detail(item.get("action")) if "action" in item else ""
        detail = query
        if action:
            detail += ("\n" if detail else "") + f"Action: {action}"
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title="Searching the web" if lifecycle != "completed" else "Web search completed",
            detail=detail,
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
        )

    if kind == "imageView":
        path = _redact_timeline_text(item.get("path"), limit=2048)
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title="Viewing image" if lifecycle != "completed" else "Viewed image",
            detail=path,
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
        )

    return _timeline_event(
        activity_id=activity_id,
        kind=kind,
        title=(
            "Compacting conversation context"
            if lifecycle != "completed"
            else "Conversation context compacted"
        ),
        status=status,
        lifecycle=lifecycle,
        elapsed=elapsed,
    )


@dataclass(slots=True)
class ExternalAgentResult:
    """Result shared by external-agent connectors."""

    text: str
    backend: str
    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class _TransportClosed:
    def __init__(self, error: ConnectorError) -> None:
        self.error = error


class _DeniedServerRequest:
    def __init__(self, error: PolicyError) -> None:
        self.error = error


class _AppServerRequestError(ConnectorError):
    """Structured JSON-RPC error used for narrowly bounded dialect fallback."""

    def __init__(self, method: str, code: Any, server_message: str) -> None:
        self.method = method
        self.code = code
        self.server_message = server_message
        super().__init__(
            f"Codex app-server {method} failed ({code}) {server_message}"
        )


class _JsonlProcess:
    """Small JSON-RPC-without-jsonrpc-header transport used by app-server."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.server_request_handler = server_request_handler
        self.process: asyncio.subprocess.Process | None = None
        self.notifications: asyncio.Queue[dict[str, Any] | _TransportClosed | _DeniedServerRequest] = (
            asyncio.Queue(maxsize=_MAX_NOTIFICATION_QUEUE)
        )
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        # A prior one-shot run can leave its terminal transport marker queued.
        # Each new process gets an independent notification stream.
        self.notifications = asyncio.Queue(maxsize=_MAX_NOTIFICATION_QUEUE)
        self._ensure_codex_home()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_JSONL_MESSAGE_BYTES + 1,
            )
        except (FileNotFoundError, OSError) as exc:
            rendered = " ".join(self.command)
            raise ConnectorError(
                f"cannot start Codex app-server ({rendered}): {exc}; "
                "configure an explicit Codex executable/command"
            ) from exc
        if self.process.stdin is None or self.process.stdout is None:
            await self.close()
            raise ConnectorError("Codex app-server did not expose stdio pipes")
        self._reader_task = asyncio.create_task(self._read_stdout(), name="replaylab-codex-stdout")
        if self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._discard_stderr(), name="replaylab-codex-stderr"
            )

    def _ensure_codex_home(self) -> None:
        """Create the caller-selected Codex state root at the spawn boundary.

        ReplayLab supplies a profile-scoped ``CODEX_HOME`` so Codex does not
        import the user's global AGENTS.md, MCP servers, Apps, hooks or memory.
        Environment construction remains side-effect free; the directory is
        materialised only when an app-server process is actually launched.
        """

        if self.env is None:
            return
        raw_home = self.env.get("CODEX_HOME", "")
        if not raw_home:
            return
        if "\0" in raw_home:
            raise ConfigurationError("Codex state root contains an invalid NUL")
        home = Path(raw_home)
        if not home.is_absolute():
            raise ConfigurationError("Codex state root must be absolute")
        try:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            if home.is_symlink() or not home.is_dir():
                raise ConfigurationError("Codex state root must be a real directory")
            # POSIX honours this directly.  On Windows the profile-scoped path
            # lives below the user's LocalAppData ACL, and chmod would only
            # toggle the read-only attribute rather than strengthen its ACL.
            if os.name != "nt":
                home.chmod(0o700)
        except ConfigurationError:
            raise
        except OSError as exc:
            raise ConfigurationError(f"cannot prepare Codex state root: {exc}") from exc

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        await self.start()
        deadline = asyncio.get_running_loop().time() + timeout
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await asyncio.wait_for(
                self._send({"method": method, "id": request_id, "params": params}),
                timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
            )
            response = await asyncio.wait_for(
                future,
                timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
            )
        except asyncio.TimeoutError as exc:
            raise ConnectorError(f"Codex app-server timed out during {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        error = response.get("error")
        if error is not None:
            if isinstance(error, dict):
                code = error.get("code", "?")
                message = str(error.get("message", "unknown error"))
                raise _AppServerRequestError(method, code, message)
            else:
                raise ConnectorError(f"Codex app-server {method} failed: {error}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ConnectorError(f"Codex app-server returned an invalid {method} response")
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.start()
        try:
            await asyncio.wait_for(
                self._send({"method": method, "params": params}), timeout=30.0
            )
        except asyncio.TimeoutError as exc:
            raise ConnectorError(f"Codex app-server timed out sending {method}") from exc

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ConnectorError("Codex app-server is not running")
        encoded = (
            json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        payload = encoded.encode("utf-8")
        if len(payload) > _MAX_JSONL_MESSAGE_BYTES:
            raise ConnectorError("Codex app-server request exceeds the 4 MiB JSONL bound")
        try:
            async with self._write_lock:
                process.stdin.write(payload)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise ConnectorError("Codex app-server stdio connection closed while writing") from exc

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        failure: ConnectorError | None = None
        try:
            while True:
                try:
                    line = await self.process.stdout.readline()
                except ValueError as exc:
                    failure = ConnectorError(
                        "Codex app-server emitted a JSONL message over the 4 MiB bound"
                    )
                    failure.__cause__ = exc
                    break
                if not line:
                    break
                if len(line) > _MAX_JSONL_MESSAGE_BYTES:
                    failure = ConnectorError(
                        "Codex app-server emitted a JSONL message over the 4 MiB bound"
                    )
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    failure = ConnectorError("Codex app-server emitted invalid JSONL on stdout")
                    failure.__cause__ = exc
                    break
                if not isinstance(message, dict):
                    failure = ConnectorError("Codex app-server emitted a non-object JSONL message")
                    break
                if "method" in message and "id" in message:
                    method = message.get("method")
                    if not isinstance(method, str) or not method or not _valid_request_id(
                        message.get("id")
                    ):
                        failure = ConnectorError(
                            "Codex app-server emitted an invalid server-request envelope"
                        )
                        break
                    if len(self._server_request_tasks) >= _MAX_SERVER_REQUEST_TASKS:
                        failure = ConnectorError(
                            "Codex app-server exceeded the concurrent server-request bound"
                        )
                        break
                    task = asyncio.create_task(
                        self._handle_server_request(message),
                        name="replaylab-codex-server-request",
                    )
                    self._server_request_tasks.add(task)
                    task.add_done_callback(self._server_request_finished)
                    # Let the task perform its initial policy checks before the
                    # reader consumes a later terminal notification. It remains
                    # independent as soon as it reaches real tool work or I/O.
                    await asyncio.sleep(0)
                    continue
                if "id" in message:
                    request_id = message.get("id")
                    future = self._pending.get(request_id) if type(request_id) is int else None
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if "method" in message:
                    await self.notifications.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = ConnectorError(f"Codex app-server reader failed: {exc}")
        finally:
            if failure is None:
                code = self.process.returncode
                if code is None:
                    with contextlib.suppress(Exception):
                        code = await self.process.wait()
                failure = ConnectorError(f"Codex app-server exited unexpectedly (code {code})")
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            self._put_priority_notification(_TransportClosed(failure))

    def _put_priority_notification(
        self, value: _TransportClosed | _DeniedServerRequest
    ) -> None:
        """Queue a terminal marker without allowing a full activity queue to hide it."""

        try:
            self.notifications.put_nowait(value)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.notifications.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.notifications.put_nowait(value)

    def _server_request_finished(self, task: asyncio.Task[None]) -> None:
        self._server_request_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if error is None:
            return
        failure = ConnectorError(
            "Codex app-server server-request handler failed: "
            + _redact_timeline_text(type(error).__name__, limit=128)
        )
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(failure)
        self._put_priority_notification(_TransportClosed(failure))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """Handle the one host-owned tool callback and deny every other request."""

        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str) or not method or not _valid_request_id(request_id):
            raise ConnectorError("Codex app-server emitted an invalid server-request envelope")
        if method == "item/tool/call" and self.server_request_handler is not None:
            params = message.get("params")
            if not isinstance(params, dict):
                denial = _DeniedServerRequest(
                    PolicyError("Codex sent malformed dynamic tool parameters")
                )
                self._put_priority_notification(denial)
                await asyncio.wait_for(
                    self._send(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32602,
                                "message": "invalid dynamic tool request",
                            },
                        }
                    ),
                    timeout=5.0,
                )
                return
            try:
                result = await self.server_request_handler(method, params)
            except PolicyError as exc:
                self._put_priority_notification(_DeniedServerRequest(exc))
                await asyncio.wait_for(
                    self._send(
                        {
                            "id": request_id,
                            "error": {"code": -32602, "message": "dynamic tool denied"},
                        }
                    ),
                    timeout=5.0,
                )
                return
            except Exception as exc:
                failure = _redact_timeline_text(type(exc).__name__, limit=128)
                result = {
                    "success": False,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": json.dumps(
                                {"error": "internal RenderDoc tool failed", "type": failure},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ],
                }
            await asyncio.wait_for(
                self._send({"id": request_id, "result": result}), timeout=5.0
            )
            return
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            response: dict[str, Any] = {"id": request_id, "result": {"decision": "decline"}}
        elif method == "item/permissions/requestApproval":
            response = {
                "id": request_id,
                "result": {"permissions": [], "scope": "turn"},
            }
        elif method == "mcpServer/elicitation/request":
            response = {
                "id": request_id,
                "result": {"action": "decline", "content": None},
            }
        else:
            response = {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "ReplayLab does not permit server-initiated interaction",
                },
            }
        self._put_priority_notification(
            _DeniedServerRequest(
                PolicyError(
                    f"Codex requested '{method}', but the headless ReplayLab connector "
                    "denied it (fail-closed)"
                )
            )
        )
        await asyncio.wait_for(self._send(response), timeout=5.0)

    async def _discard_stderr(self) -> None:
        # Drain diagnostics to prevent a full pipe from blocking the process.  Do
        # not retain them because third-party diagnostics may contain secrets.
        assert self.process is not None and self.process.stderr is not None
        try:
            while await self.process.stderr.read(8192):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=0.25)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
        tasks = tuple(
            task for task in (self._reader_task, self._stderr_task) if task is not None
        )
        if tasks:
            # Let both pipe readers observe EOF before cancelling them.  In
            # particular, cancelling immediately after Process.wait() leaves
            # Proactor pipe transports alive until after asyncio.run() closes
            # the loop on Python 3.10/Windows.
            _done, pending = await asyncio.wait(tasks, timeout=0.5)
            for task in pending:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        server_request_tasks = tuple(self._server_request_tasks)
        for task in server_request_tasks:
            task.cancel()
        if server_request_tasks:
            await asyncio.gather(*server_request_tasks, return_exceptions=True)
        self._server_request_tasks.clear()
        # asyncio.subprocess does not expose a public transport close method.
        # Closing the private BaseSubprocessTransport while the loop is alive is
        # the only reliable cleanup for Python 3.10's Windows Proactor loop.
        process_transport = getattr(process, "_transport", None)
        if process_transport is not None:
            with contextlib.suppress(Exception):
                process_transport.close()
            await asyncio.sleep(0)
        self.process = None
        self._reader_task = None
        self._stderr_task = None


class CodexAppServerConnector:
    """One-connection Codex app-server adapter with fail-closed permissions."""

    def __init__(
        self,
        command: Sequence[str] | str = ("codex", "app-server"),
        *,
        cwd: Path | str | None = None,
        readable_roots: Sequence[Path | str] | None = None,
        expected_tools: set[str] | frozenset[str] = frozenset(),
        dynamic_tools: Sequence[Mapping[str, Any]] = (),
        dynamic_tool_handler: DynamicToolHandler | None = None,
        timeout: float = 300.0,
        heartbeat_interval: float = 10.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(command, str):
            command = (command,)
        self.command = tuple(str(value) for value in command)
        if not self.command or any(not value or "\0" in value for value in self.command):
            raise ConfigurationError("Codex app-server command must be a non-empty argv sequence")
        self.cwd = Path(cwd or Path.cwd()).resolve()
        if not self.cwd.is_dir():
            raise ConfigurationError(f"Codex working directory does not exist: {self.cwd}")
        roots = readable_roots if readable_roots is not None else (self.cwd,)
        resolved_roots: list[Path] = []
        for root in roots:
            resolved = Path(root).resolve()
            if not resolved.is_dir():
                raise ConfigurationError(f"Codex readable root does not exist: {resolved}")
            if resolved not in resolved_roots:
                resolved_roots.append(resolved)
        if self.cwd not in resolved_roots:
            resolved_roots.insert(0, self.cwd)
        self.readable_roots = tuple(resolved_roots)
        if any(not isinstance(value, str) for value in expected_tools):
            raise ConfigurationError("Codex expected tool names must be strings")
        self.expected_tools = frozenset(value for value in expected_tools)
        if any(
            _DYNAMIC_TOOL_NAME_RE.fullmatch(value) is None for value in self.expected_tools
        ):
            raise ConfigurationError("Codex expected tool name is invalid")
        self.dynamic_tools = _validated_dynamic_tool_specs(dynamic_tools)
        dynamic_names = frozenset(
            str(value.get("name", "")).strip() for value in self.dynamic_tools
        )
        if (
            len(dynamic_names) != len(self.dynamic_tools)
            or "" in dynamic_names
            or dynamic_names != self.expected_tools
        ):
            raise ConfigurationError(
                "Codex dynamic tool definitions must exactly match the active grant"
            )
        if dynamic_tool_handler is not None and not callable(dynamic_tool_handler):
            raise ConfigurationError("Codex internal tool handler must be callable")
        if self.expected_tools and dynamic_tool_handler is None:
            raise ConfigurationError("Codex capture tools require an internal tool handler")
        self.dynamic_tool_handler = dynamic_tool_handler
        if timeout <= 0:
            raise ConfigurationError("Codex app-server timeout must be positive")
        self.timeout = float(timeout)
        if heartbeat_interval <= 0 or heartbeat_interval > 60:
            raise ConfigurationError(
                "Codex app-server heartbeat interval must be between 0 and 60 seconds"
            )
        self.heartbeat_interval = float(heartbeat_interval)
        self._active_thread_id = ""
        self._active_turn_id = ""
        self._tool_phase = _TOOL_PHASE_IDLE
        self._seen_dynamic_call_ids: set[str] = set()
        self._turn_ready = asyncio.Event()
        self._transport = _JsonlProcess(
            self.command,
            cwd=self.cwd,
            env=env,
            server_request_handler=self._handle_server_request,
        )
        self._initialized = False
        self._run_lock = asyncio.Lock()
        # App-server releases have used two serialisations for ThreadStart's
        # SandboxMode. Prefer the established camelCase value and negotiate the
        # newer kebab-case value only after the server explicitly identifies
        # that exact enum mismatch.
        self._thread_sandbox_mode = "readOnly"
        self._turn_restricted_access = True

    async def __aenter__(self) -> CodexAppServerConnector:
        # Construction and context entry are deliberately lazy; run() starts the
        # process only when the selected backend is actually used.
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self._tool_phase = _TOOL_PHASE_CLOSING
        # Wake a tool request that arrived while turn/start was still resolving.
        # It will observe the closing phase and fail without touching RenderDoc.
        self._turn_ready.set()
        await self._transport.close()
        self._initialized = False
        self._active_thread_id = ""
        self._active_turn_id = ""
        self._seen_dynamic_call_ids.clear()
        self._tool_phase = _TOOL_PHASE_IDLE
        self._turn_ready.clear()

    async def _handle_server_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method != "item/tool/call" or self.dynamic_tool_handler is None:
            raise PolicyError(f"Codex requested unsupported host interaction '{method}'")
        if self._tool_phase == _TOOL_PHASE_STARTING:
            try:
                await asyncio.wait_for(self._turn_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError as exc:
                raise PolicyError("Codex requested a dynamic tool before the turn was ready") from exc
        if self._tool_phase != _TOOL_PHASE_RUNNING:
            raise PolicyError("Codex requested a dynamic tool outside an active turn")
        required = ("threadId", "turnId", "callId", "tool", "arguments")
        if any(key not in params for key in required):
            raise PolicyError("Codex dynamic tool call omitted required fields")
        thread_id = params["threadId"]
        turn_id = params["turnId"]
        call_id = params["callId"]
        tool = params["tool"]
        namespace = params.get("namespace")
        arguments = params["arguments"]
        if (
            not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or not isinstance(call_id, str)
            or not isinstance(tool, str)
            or thread_id != self._active_thread_id
            or turn_id != self._active_turn_id
            or not call_id
            or len(call_id) > 256
            or namespace is not None
            or tool not in self.expected_tools
        ):
            raise PolicyError("Codex dynamic tool call did not match the active turn grant")
        if call_id in self._seen_dynamic_call_ids:
            raise PolicyError("Codex repeated a dynamic tool call id")
        if len(self._seen_dynamic_call_ids) >= _MAX_DYNAMIC_TOOL_CALLS:
            raise PolicyError("Codex exceeded the dynamic tool-call bound")
        self._seen_dynamic_call_ids.add(call_id)
        if not isinstance(arguments, dict):
            return self._dynamic_tool_failure("ToolInputError")
        try:
            value = await self.dynamic_tool_handler(tool, arguments, call_id)
        except Exception as exc:
            return self._dynamic_tool_failure(type(exc).__name__)
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            return self._dynamic_tool_failure(type(exc).__name__)
        if len(text) > MAX_EXTERNAL_RESPONSE_CHARS or len(text.encode("utf-8")) > 3 * 1024 * 1024:
            return self._dynamic_tool_failure("ResultTooLarge")
        return {
            "success": True,
            "contentItems": [{"type": "inputText", "text": text}],
        }

    @staticmethod
    def _dynamic_tool_failure(error_type: str) -> dict[str, Any]:
        failure = _redact_timeline_text(error_type, limit=128) or "ToolError"
        return {
            "success": False,
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps(
                        {"error": "internal RenderDoc tool failed", "type": failure},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                }
            ],
        }

    async def _initialize(self) -> None:
        if self._initialized:
            return
        params: dict[str, Any] = {
            "clientInfo": {
                "name": "replaylab",
                "title": "ReplayLab for RenderDoc",
                "version": __version__,
            }
        }
        if self.dynamic_tools:
            # Codex app-server gates thread/start.dynamicTools behind this
            # capability. General-help turns do not opt into the experimental API.
            params["capabilities"] = {"experimentalApi": True}
        await self._transport.request(
            "initialize",
            params,
            timeout=min(self.timeout, 30.0),
        )
        await self._transport.notify("initialized", {})
        self._initialized = True

    async def account_read(self, *, refresh_token: bool = False) -> dict[str, Any]:
        """Return Codex-managed account state without reading token files."""

        async with self._run_lock:
            await self._initialize()
            return await self._transport.request(
                "account/read",
                {"refreshToken": bool(refresh_token)},
                timeout=min(self.timeout, 30.0),
            )

    async def list_models(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Return the bounded, paginated app-server model picker catalog."""

        async with self._run_lock:
            await self._initialize()
            cursor: str | None = None
            models: list[dict[str, Any]] = []
            seen_cursors: set[str] = set()
            for _ in range(10):
                params: dict[str, Any] = {
                    "limit": 100,
                    "includeHidden": bool(include_hidden),
                }
                if cursor:
                    params["cursor"] = cursor
                result = await self._transport.request(
                    "model/list", params, timeout=min(self.timeout, 30.0)
                )
                data = result.get("data", [])
                if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                    raise ConnectorError("Codex app-server returned invalid model/list data")
                for item in data:
                    model_id = item.get("id", item.get("model", ""))
                    if not isinstance(model_id, str) or not model_id.strip():
                        continue
                    if len(models) >= 500:
                        raise ConnectorError("Codex app-server model/list exceeded 500 entries")
                    models.append(dict(item))
                raw_cursor = result.get("nextCursor")
                if raw_cursor is None or raw_cursor == "":
                    return models
                if not isinstance(raw_cursor, str) or raw_cursor in seen_cursors:
                    raise ConnectorError("Codex app-server returned an invalid model/list cursor")
                seen_cursors.add(raw_cursor)
                cursor = raw_cursor
            raise ConnectorError("Codex app-server model/list exceeded 10 pages")

    async def login_start(self, *, flow: str = "browser") -> dict[str, Any]:
        """Start an official managed ChatGPT login on this live app-server."""

        if flow == "browser":
            # Let app-server use its local callback success page.  The hosted
            # success page is intended for branded clients and can hand the
            # browser back to a Codex/ChatGPT application route.  ReplayLab is
            # an embedded client, so that redirect is both surprising and can
            # make it look as if the login belongs to a newly opened chat.
            params: dict[str, Any] = {"type": "chatgpt"}
        elif flow == "device":
            params = {"type": "chatgptDeviceCode"}
        else:
            raise ConfigurationError("Codex login flow must be 'browser' or 'device'")
        await self._run_lock.acquire()
        try:
            await self._initialize()
            result = await self._transport.request(
                "account/login/start", params, timeout=min(self.timeout, 30.0)
            )
            login_id = result.get("loginId")
            if not isinstance(login_id, str) or not login_id:
                raise ConnectorError("Codex app-server login/start returned no login id")
            url_field = "authUrl" if flow == "browser" else "verificationUrl"
            url = result.get(url_field)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ConnectorError(
                    f"Codex app-server login/start returned an invalid {url_field}"
                )
            return dict(result)
        except Exception:
            self._run_lock.release()
            raise

    async def wait_login(self, login_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Wait for one correlated login to become visible through ``account/read``.

        App-server's managed-login terminal sequence contains two notifications:
        ``account/login/completed`` for the matching ``loginId`` and
        ``account/updated`` for the resulting auth mode.  Releases can emit the
        two notifications close together (and not necessarily in the order a
        client happens to dequeue them).  Returning on the first notification
        races the persisted account state and makes a first connection appear
        to fail even though a second connection succeeds.

        Keep the login lock until the matching successful completion has also
        become observable via the official ``account/read`` endpoint.  The
        bounded account probe is also a compatibility fallback for app-server
        releases that omit or reorder ``account/updated``.
        """

        if not self._run_lock.locked():
            raise ConfigurationError("wait_login requires a login started by this connector")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout or self.timeout)
        completion: dict[str, Any] | None = None
        account_update: dict[str, Any] | None = None
        update_grace_deadline: float | None = None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if completion is not None and completion.get("success") is True:
                        raise ConnectorError(
                            "Codex login completed, but the authenticated account state "
                            "did not become available before the timeout"
                        )
                    raise ConnectorError("Codex app-server login timed out")

                # A successful login notification is necessary but not itself
                # sufficient: confirm the account through the supported API
                # before releasing the connector to model/list or a test turn.
                if completion is not None and completion.get("success") is True:
                    # Prefer consuming the paired official account/updated
                    # terminal notification.  Bound this grace period so a
                    # release that omits it still progresses via account/read.
                    if (
                        account_update is None
                        and update_grace_deadline is not None
                        and loop.time() < update_grace_deadline
                    ):
                        notification_timeout = min(
                            remaining, update_grace_deadline - loop.time()
                        )
                    else:
                        notification_timeout = 0.0

                else:
                    notification_timeout = remaining

                if notification_timeout > 0:
                    try:
                        message = await asyncio.wait_for(
                            self._transport.notifications.get(),
                            timeout=notification_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        if completion is None or completion.get("success") is not True:
                            raise ConnectorError("Codex app-server login timed out") from exc
                        # The bounded account/updated grace period elapsed.
                        message = None
                    if message is not None:
                        if isinstance(message, _TransportClosed):
                            raise message.error
                        if isinstance(message, _DeniedServerRequest):
                            raise message.error

                        method = message.get("method")
                        if method == "account/updated":
                            params = message.get("params")
                            if not isinstance(params, dict):
                                raise ConnectorError(
                                    "Codex app-server returned invalid account update data"
                                )
                            # Do not treat this uncorrelated notification as
                            # proof of success.  It is only a readiness signal;
                            # account/read is authoritative after the matching
                            # login completion.
                            if params.get("authMode"):
                                account_update = dict(params)
                            continue
                        if method != "account/login/completed":
                            continue
                        params = message.get("params")
                        if not isinstance(params, dict):
                            raise ConnectorError(
                                "Codex app-server returned invalid login completion data"
                            )
                        if params.get("loginId") != login_id:
                            continue
                        completion = dict(params)
                        if completion.get("success") is not True:
                            return completion
                        update_grace_deadline = min(deadline, loop.time() + 0.5)
                        continue

                if completion is not None and completion.get("success") is True:
                    account_result = await self._transport.request(
                        "account/read",
                        {"refreshToken": False},
                        timeout=min(remaining, 5.0),
                    )
                    account = account_result.get("account")
                    if isinstance(account, dict) and account:
                        return completion

                    # Give account/updated a short opportunity to arrive before
                    # the next bounded account/read probe.  This avoids a busy
                    # loop while remaining compatible with a missing update.
                    retry_delay = min(max(0.0, deadline - loop.time()), 0.25)
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
        finally:
            if self._run_lock.locked():
                self._run_lock.release()

    async def cancel_login(self, login_id: str) -> dict[str, Any]:
        """Cancel a managed login on the same live app-server process."""

        if not isinstance(login_id, str) or not login_id:
            raise ConfigurationError("Codex login id is required")
        await self._initialize()
        return await self._transport.request(
            "account/login/cancel",
            {"loginId": login_id},
            timeout=min(self.timeout, 30.0),
        )

    async def logout(self) -> dict[str, Any]:
        async with self._run_lock:
            await self._initialize()
            return await self._transport.request(
                "account/logout", {}, timeout=min(self.timeout, 30.0)
            )

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        skill_name: str | None = None,
        skill_path: Path | str | None = None,
        on_delta: DeltaCallback | None = None,
        on_activity: ActivityCallback | None = None,
        on_timeline: TimelineCallback | None = None,
    ) -> ExternalAgentResult:
        if not prompt.strip():
            raise ConfigurationError("Codex prompt must not be empty")
        async with self._run_lock:
            try:
                await self._initialize()
                account_state = await self._transport.request(
                    "account/read",
                    {"refreshToken": False},
                    timeout=min(self.timeout, 30.0),
                )
                if account_state.get("account") is None:
                    raise AuthenticationError(
                        "This RenderDoc Codex profile is not signed in. Open Settings > AI, "
                        "edit the connection, then select Connect / Sign in."
                    )
                thread_params: dict[str, Any] = {
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    "serviceName": "replaylab",
                    "ephemeral": True,
                }
                if self.dynamic_tools:
                    thread_params["dynamicTools"] = list(self.dynamic_tools)
                if model:
                    thread_params["model"] = model
                thread_result = await self._start_thread(thread_params)
                thread = thread_result.get("thread")
                raw_thread_id = thread.get("id") if isinstance(thread, dict) else None
                if not isinstance(raw_thread_id, str) or not raw_thread_id:
                    raise ConnectorError("Codex app-server thread/start returned no thread id")
                thread_id = raw_thread_id
                self._active_thread_id = thread_id
                instruction_sources: list[Any] = []
                if isinstance(thread, dict):
                    raw_sources = thread.get("instructionSources", [])
                    if raw_sources is not None:
                        if not isinstance(raw_sources, list):
                            raise ConnectorError(
                                "Codex thread/start returned invalid instructionSources"
                            )
                        instruction_sources.extend(raw_sources)
                raw_top_sources = thread_result.get("instructionSources", [])
                if raw_top_sources is not None:
                    if not isinstance(raw_top_sources, list):
                        raise ConnectorError(
                            "Codex thread/start returned invalid instructionSources"
                        )
                    instruction_sources.extend(raw_top_sources)
                if instruction_sources:
                    raise PolicyError(
                        "Codex ReplayLab threads must not load ambient instruction sources"
                    )
                await self._verify_tool_isolation(thread_id)

                text = prompt
                inputs: list[dict[str, Any]] = []
                if skill_name or skill_path:
                    if not skill_name or not skill_path:
                        raise ConfigurationError("Codex skill invocation requires both name and path")
                    resolved_skill = Path(skill_path).resolve()
                    if not resolved_skill.is_file():
                        raise ConfigurationError(f"Codex skill file does not exist: {resolved_skill}")
                    marker = f"${skill_name}"
                    if not text.lstrip().startswith(marker):
                        text = f"{marker} {text}"
                    inputs.append(
                        {"type": "skill", "name": skill_name, "path": str(resolved_skill)}
                    )
                inputs.insert(0, {"type": "text", "text": text})
                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": inputs,
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    "sandboxPolicy": self._turn_sandbox_policy(),
                }
                if model:
                    turn_params["model"] = model
                self._active_turn_id = ""
                self._seen_dynamic_call_ids.clear()
                self._tool_phase = _TOOL_PHASE_STARTING
                self._turn_ready.clear()
                try:
                    turn_result = await self._start_turn(turn_params)
                except BaseException:
                    self._tool_phase = _TOOL_PHASE_CLOSING
                    self._turn_ready.set()
                    raise
                turn = turn_result.get("turn")
                raw_turn_id = turn.get("id") if isinstance(turn, dict) else None
                if not isinstance(raw_turn_id, str) or not raw_turn_id:
                    self._tool_phase = _TOOL_PHASE_CLOSING
                    self._turn_ready.set()
                    raise ConnectorError("Codex app-server turn/start returned no turn id")
                turn_id = raw_turn_id
                self._active_turn_id = turn_id
                self._tool_phase = _TOOL_PHASE_RUNNING
                self._turn_ready.set()

                try:
                    answer = await self._collect_turn(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        on_delta=on_delta,
                        on_activity=on_activity,
                        on_timeline=on_timeline,
                    )
                except BaseException:
                    self._tool_phase = _TOOL_PHASE_CLOSING
                    self._turn_ready.set()
                    with contextlib.suppress(Exception):
                        await self._transport.request(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                            timeout=2.0,
                        )
                    raise
                finally:
                    self._tool_phase = _TOOL_PHASE_CLOSING
                    self._turn_ready.set()
                return ExternalAgentResult(
                    text=answer,
                    backend="codex-app-server",
                    session_id=thread_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
            finally:
                # Runtime creates a connector per request. Closing here prevents
                # orphaned app-server processes while preserving lazy startup.
                await self.close()

    async def _start_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start one thread with a single exact SandboxMode dialect fallback."""

        request_params = dict(params)
        request_params["sandbox"] = self._thread_sandbox_mode
        try:
            return await self._transport.request(
                "thread/start", request_params, timeout=min(self.timeout, 60.0)
            )
        except _AppServerRequestError as exc:
            message = exc.server_message.casefold()
            dialect_mismatch = (
                exc.method == "thread/start"
                and exc.code == -32600
                and self._thread_sandbox_mode == "readOnly"
                and "unknown variant" in message
                and "readonly" in message
                and "read-only" in message
            )
            if not dialect_mismatch:
                raise
        self._thread_sandbox_mode = "read-only"
        request_params["sandbox"] = self._thread_sandbox_mode
        return await self._transport.request(
            "thread/start", request_params, timeout=min(self.timeout, 60.0)
        )

    def _turn_sandbox_policy(self) -> dict[str, Any]:
        policy: dict[str, Any] = {"type": "readOnly"}
        if self._turn_restricted_access:
            policy["access"] = {
                "type": "restricted",
                "includePlatformDefaults": False,
                "readableRoots": [str(root) for root in self.readable_roots],
            }
        else:
            # Newer app-server builds removed the legacy restricted-access
            # extension while retaining the documented readOnly policy.
            policy["networkAccess"] = False
        return policy

    async def _start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start one turn with a single exact legacy-access fallback."""

        try:
            return await self._transport.request(
                "turn/start", params, timeout=min(self.timeout, 60.0)
            )
        except _AppServerRequestError as exc:
            message = exc.server_message.casefold()
            access_removed = (
                exc.method == "turn/start"
                and exc.code == -32600
                and self._turn_restricted_access
                and "readonly.access is no longer supported" in message
                and "permissionprofile" in message
            )
            if not access_removed:
                raise
        self._turn_restricted_access = False
        retry_params = dict(params)
        retry_params["sandboxPolicy"] = self._turn_sandbox_policy()
        return await self._transport.request(
            "turn/start", retry_params, timeout=min(self.timeout, 60.0)
        )

    async def _verify_tool_isolation(self, thread_id: str) -> None:
        """Fail closed if user-global MCP servers or Apps leak into this thread."""

        cursor: str | None = None
        entries: dict[str, dict[str, Any]] = {}
        for _page in range(100):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "detail": "toolsAndAuthOnly",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._transport.request(
                "mcpServerStatus/list", params, timeout=min(self.timeout, 60.0)
            )
            data = result.get("data")
            if not isinstance(data, list):
                raise ConnectorError("Codex MCP inventory returned an invalid data array")
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ConnectorError("Codex MCP inventory contained an invalid server entry")
                name = item["name"].strip().lower()
                if not name or name in entries:
                    raise ConnectorError("Codex MCP inventory contained a duplicate/empty name")
                entries[name] = item
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ConnectorError("Codex MCP inventory returned an invalid cursor")
            cursor = next_cursor
        else:
            raise ConnectorError("Codex MCP inventory exceeded the pagination safety bound")
        if entries:
            raise PolicyError(
                "Codex ReplayLab threads must not load MCP servers; observed: "
                + ", ".join(sorted(entries))
            )

        installed = await self._transport.request(
            "app/installed",
            {"threadId": thread_id, "forceRefresh": False},
            timeout=min(self.timeout, 60.0),
        )
        apps = installed.get("apps")
        if not isinstance(apps, list):
            raise ConnectorError("Codex app inventory returned an invalid apps array")
        callable_apps = sorted(
            str(item.get("id", ""))
            for item in apps
            if isinstance(item, dict) and item.get("callable") is True
        )
        if callable_apps:
            raise PolicyError(
                "Codex Apps must be disabled for ReplayLab isolation; callable apps: "
                + ", ".join(callable_apps)
            )

    async def _collect_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        on_delta: DeltaCallback | None,
        on_activity: ActivityCallback | None,
        on_timeline: TimelineCallback | None,
    ) -> str:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout
        streamed: list[str] = []
        streamed_chars = 0
        streamed_bytes = 0
        last_message = ""
        final_message = ""
        last_provider_notice = ""
        timeline_state = _TimelineState()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                suffix = f"; last provider status: {last_provider_notice}" if last_provider_notice else ""
                raise ConnectorError(
                    f"Codex app-server turn timed out after {self.timeout:.0f}s{suffix}"
                )
            try:
                event = await asyncio.wait_for(
                    self._transport.notifications.get(),
                    min(remaining, self.heartbeat_interval),
                )
            except asyncio.TimeoutError as exc:
                elapsed = loop.time() - started
                if loop.time() >= deadline:
                    suffix = (
                        f"; last provider status: {last_provider_notice}"
                        if last_provider_notice
                        else ""
                    )
                    raise ConnectorError(
                        f"Codex app-server turn timed out after {self.timeout:.0f}s{suffix}"
                    ) from exc
                await _emit_activity(on_activity, "waiting for model response", elapsed)
                continue
            if isinstance(event, _TransportClosed):
                raise event.error
            if isinstance(event, _DeniedServerRequest):
                raise event.error
            method = str(event.get("method", ""))
            params = event.get("params")
            if not isinstance(params, dict):
                continue
            if method in {"error", "warning"}:
                # App-server reports retryable transport failures as notifications and
                # later emits the authoritative turn/completed terminal state.  Surface
                # a bounded, non-secret status instead of either dropping it or treating
                # a retry as final failure.
                last_provider_notice = _provider_notice(method, params)
                await _emit_activity(
                    on_activity,
                    last_provider_notice,
                    loop.time() - started,
                )
                continue
            event_turn_id = str(params.get("turnId", ""))
            event_thread_id = str(params.get("threadId", ""))
            if event_turn_id and event_turn_id != turn_id:
                continue
            if event_thread_id and event_thread_id != thread_id:
                continue

            elapsed = loop.time() - started

            # Raw chain-of-thought is deliberately outside ReplayLab's UI and
            # persistence contract.  Do not even pass it through the generic
            # sanitizers: only the documented public summary stream below is
            # eligible for display.
            if method == "item/reasoning/textDelta":
                continue

            if method == "turn/plan/updated":
                raw_plan = params.get("plan")
                if isinstance(raw_plan, list):
                    lines: list[str] = []
                    explanation = _redact_timeline_text(
                        params.get("explanation"), limit=2048
                    )
                    if explanation:
                        lines.append(explanation)
                    for raw_step in raw_plan[:_MAX_PLAN_STEPS]:
                        if not isinstance(raw_step, Mapping):
                            continue
                        step = _redact_timeline_text(
                            raw_step.get("step"), limit=_MAX_PLAN_STEP_CHARS
                        )
                        step_status = _redact_timeline_text(
                            raw_step.get("status", "pending"), limit=32
                        )
                        if step:
                            lines.append(f"[{step_status}] {step}")
                    if len(raw_plan) > _MAX_PLAN_STEPS:
                        lines.append("… [plan truncated]")
                    await _emit_timeline(
                        on_timeline,
                        _timeline_event(
                            activity_id=f"turn-plan:{turn_id}",
                            kind="plan",
                            title="Plan updated",
                            detail="\n".join(lines),
                            status="running",
                            lifecycle="updated",
                            elapsed=elapsed,
                        ),
                    )
                continue

            if method == "item/reasoning/summaryTextDelta":
                item_id = _item_id(params.get("itemId"))
                delta = params.get("delta")
                if item_id and isinstance(delta, str) and delta:
                    summary = timeline_state.append(
                        timeline_state.reasoning,
                        item_id,
                        delta,
                        _MAX_REASONING_SUMMARY_CHARS,
                    )
                    if summary:
                        await _emit_timeline(
                            on_timeline,
                            _timeline_event(
                                activity_id=item_id,
                                kind="reasoning",
                                title="Reasoning",
                                detail=_redact_timeline_text(
                                    summary, limit=_MAX_REASONING_SUMMARY_CHARS
                                ),
                                status="running",
                                lifecycle="updated",
                                elapsed=elapsed,
                            ),
                        )
                continue

            if method == "item/plan/delta":
                item_id = _item_id(params.get("itemId"))
                delta = params.get("delta")
                if item_id and isinstance(delta, str) and delta:
                    plan_text = timeline_state.append(
                        timeline_state.plans, item_id, delta, _MAX_PLAN_CHARS
                    )
                    if plan_text:
                        await _emit_timeline(
                            on_timeline,
                            _timeline_event(
                                activity_id=item_id,
                                kind="plan",
                                title="Planning",
                                detail=_redact_timeline_text(
                                    plan_text, limit=_MAX_PLAN_CHARS
                                ),
                                status="running",
                                lifecycle="updated",
                                elapsed=elapsed,
                            ),
                        )
                continue

            if method == "item/commandExecution/outputDelta":
                item_id = _item_id(params.get("itemId"))
                delta = params.get("delta")
                if item_id and isinstance(delta, str) and delta:
                    prior = timeline_state.command_output.get(item_id, "")
                    output = timeline_state.append(
                        timeline_state.command_output,
                        item_id,
                        delta,
                        _MAX_COMMAND_OUTPUT_RAW_CHARS,
                    )
                    if len(prior) + len(delta) > _MAX_COMMAND_OUTPUT_RAW_CHARS:
                        timeline_state.command_output_truncated.add(item_id)
                    if output:
                        detail = _redact_timeline_text(
                            output, limit=_MAX_VISIBLE_COMMAND_OUTPUT_CHARS
                        )
                        if (
                            item_id in timeline_state.command_output_truncated
                            and "[truncated]" not in detail
                        ):
                            detail = (
                                detail[: max(0, _MAX_VISIBLE_COMMAND_OUTPUT_CHARS - 14)]
                                + "… [truncated]"
                            )
                        detail_parts: list[str] = []
                        command = timeline_state.commands.get(item_id, "")
                        cwd = timeline_state.command_cwds.get(item_id, "")
                        if command:
                            detail_parts.append(f"Command: {command}")
                        if cwd:
                            detail_parts.append(f"Working directory: {cwd}")
                        detail_parts.append(f"Output:\n{detail}")
                        await _emit_timeline(
                            on_timeline,
                            _timeline_event(
                                activity_id=item_id,
                                kind="commandExecution",
                                title="Command output",
                                detail="\n".join(detail_parts),
                                status="running",
                                lifecycle="updated",
                                elapsed=elapsed,
                            ),
                        )
                continue

            if method == "item/started":
                item = params.get("item")
                if isinstance(item, Mapping):
                    timeline = _item_timeline_event(
                        item,
                        lifecycle="started",
                        elapsed=elapsed,
                        state=timeline_state,
                    )
                    if timeline is not None:
                        await _emit_timeline(on_timeline, timeline)
                continue

            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str) and delta:
                    streamed_chars, streamed_bytes = checked_response_size(
                        delta, streamed_chars, streamed_bytes
                    )
                    streamed.append(delta)
                    await _emit_delta(on_delta, delta)
                continue
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, Mapping):
                    timeline = _item_timeline_event(
                        item,
                        lifecycle="completed",
                        elapsed=elapsed,
                        state=timeline_state,
                    )
                    if timeline is not None:
                        await _emit_timeline(on_timeline, timeline)
                    if item.get("type") == "agentMessage":
                        candidate = item.get("text")
                        if isinstance(candidate, str):
                            checked_response_size(candidate)
                            last_message = candidate
                            # A completed agentMessage is authoritative over streamed fragments.
                            # Drop the fallback accumulator so a provider cannot retain two copies.
                            streamed.clear()
                            if item.get("phase") == "final_answer":
                                final_message = candidate
                    completed_item_id = _item_id(item.get("id"))
                    if completed_item_id:
                        timeline_state.reasoning.pop(completed_item_id, None)
                        timeline_state.commands.pop(completed_item_id, None)
                        timeline_state.command_cwds.pop(completed_item_id, None)
                        timeline_state.command_output.pop(completed_item_id, None)
                        timeline_state.command_output_truncated.discard(completed_item_id)
                        timeline_state.plans.pop(completed_item_id, None)
                continue
            if method != "turn/completed":
                continue
            completed_turn = params.get("turn")
            if not isinstance(completed_turn, dict):
                raise ConnectorError("Codex app-server emitted an invalid turn/completed event")
            completed_id = str(completed_turn.get("id", ""))
            if completed_id and completed_id != turn_id:
                continue
            status = str(completed_turn.get("status", ""))
            if status != "completed":
                error = completed_turn.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message", status or "failed"))
                else:
                    message = status or "failed"
                raise ConnectorError(f"Codex turn did not complete: {message}")
            return final_message or last_message or "".join(streamed)


async def _emit_delta(callback: DeltaCallback | None, text: str) -> None:
    if callback is None:
        return
    result = callback(text)
    if inspect.isawaitable(result):
        await result


async def _emit_activity(
    callback: ActivityCallback | None, status: str, elapsed: float
) -> None:
    if callback is None:
        return
    result = callback(status, max(0.0, float(elapsed)))
    if inspect.isawaitable(result):
        await result


async def _emit_timeline(
    callback: TimelineCallback | None, event: Mapping[str, Any]
) -> None:
    if callback is None:
        return
    # Give callbacks their own shallow object so they cannot mutate connector
    # state retained for later deltas or authoritative completion items.
    result = callback(dict(event))
    if inspect.isawaitable(result):
        await result


def _provider_notice(method: str, params: Mapping[str, Any]) -> str:
    """Map provider-controlled diagnostics to small, non-secret UI statuses."""

    candidates = (params.get("message"), params.get("error"))
    rendered = " ".join(str(value) for value in candidates if value is not None).casefold()
    if "timed out" in rendered or "timeout" in rendered:
        return "model transport timed out; Codex is retrying"
    if "disconnect" in rendered or "connection" in rendered:
        return "model transport disconnected; Codex is retrying"
    if method == "warning":
        return "Codex reported a recoverable warning; waiting for completion"
    return "Codex reported a provider error; waiting for the terminal turn status"
