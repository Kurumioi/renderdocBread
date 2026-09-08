"""Safe, minimal client for ``codex app-server``.

The connector intentionally delegates authentication to the official Codex
process.  It never reads Codex/ChatGPT token files, browser cookies, or any
other authentication cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..conversation import (
    MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES,
    MAX_CONVERSATION_ATTACHMENTS_PER_TURN,
    ProviderBinaryAttachment,
    ProviderPathAttachment,
)
from ..errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
    ProviderCapabilityError,
)

DeltaCallback = Callable[[str], Any]
ActivityCallback = Callable[[str, float], Any]
TimelineCallback = Callable[[Mapping[str, Any]], Any]
ReasoningCallback = Callable[[Mapping[str, Any]], Any]
AssistantMessageCallback = Callable[[Mapping[str, Any]], Any]
_MAX_JSONL_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_EXTERNAL_RESPONSE_CHARS = 1024 * 1024
MAX_EXTERNAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_NOTIFICATION_QUEUE = 256
_MAX_SERVER_REQUEST_TASKS = 16
_MCP_STARTUP_TIMEOUT_SECONDS = 60.0
_MCP_STARTUP_POLL_SECONDS = 0.25
_MAX_TIMELINE_ITEMS = 128
_MAX_TIMELINE_ID_CHARS = 256
_MAX_TIMELINE_TEXT_CHARS = 4096
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
_READ_ONLY_PERMISSION_PROFILE = ":read-only"
_PERMISSION_PROFILE_CAPABILITY = "codex-app-server.permission-profiles-v2"
_PERMISSION_PROTOCOL_UNSUPPORTED = (
    "codex-app-server.permission-profile-contract-unsupported"
)
_READ_ONLY_PROFILE_UNAVAILABLE = "codex-app-server.read-only-profile-unavailable"
_READ_SCOPE_UNVERIFIED = "codex-app-server.read-scope-unverified"
_ATTACHMENT_ROOT_RE = re.compile(r"^\.replaylab-attachments-[0-9a-f]{32}$")
_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _valid_request_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(2**63) <= value < 2**63
    return isinstance(value, str)


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


def _argument_names(value: Any) -> tuple[str, ...]:
    """Return bounded top-level MCP argument names without inspecting values."""

    if not isinstance(value, Mapping):
        return ()
    names: list[str] = []
    for raw_name in value:
        if len(names) >= 16:
            break
        if not isinstance(raw_name, str):
            continue
        name = _redact_timeline_text(raw_name, limit=96).strip()
        if name:
            names.append(name)
    return tuple(sorted(set(names), key=str.casefold))


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


def _reasoning_content_parts(value: Any) -> tuple[tuple[int, str], ...]:
    """Extract protocol-defined plaintext parts without serialising provider objects."""

    if isinstance(value, str):
        return ((0, value),)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    parts: list[tuple[int, str]] = []
    for index, part in enumerate(value):
        if index >= 256:
            break
        if isinstance(part, str):
            parts.append((index, part))
        elif isinstance(part, Mapping):
            part_type = part.get("type")
            text = part.get("text")
            if part_type in {"reasoning_text", "summary_text", "text", None} and isinstance(
                text, str
            ):
                parts.append((index, text))
    return tuple(parts)


def _opaque_reasoning_metadata(item: Mapping[str, Any]) -> tuple[int, str]:
    """Describe an opaque reasoning blob without retaining or exposing its contents."""

    for name in ("encrypted_content", "encryptedContent", "signature"):
        value = item.get(name)
        if isinstance(value, str) and value:
            encoded = value.encode("utf-8", errors="backslashreplace")
            return len(encoded), hashlib.sha256(encoded).hexdigest()
    return 0, ""


@dataclass(slots=True)
class _TimelineState:
    reasoning: dict[str, str] = field(default_factory=dict)
    reasoning_streams: dict[tuple[str, int], str] = field(default_factory=dict)
    raw_reasoning: dict[tuple[str, int], str] = field(default_factory=dict)
    opaque_reasoning: dict[str, tuple[int, str]] = field(default_factory=dict)
    raw_reasoning_deltas: set[tuple[str, int]] = field(default_factory=set)
    raw_reasoning_emitted: set[tuple[str, int]] = field(default_factory=set)
    summary_reasoning_emitted: set[tuple[str, int]] = field(default_factory=set)
    reasoning_items_seen: set[str] = field(default_factory=set)
    reasoning_items_completed: set[str] = field(default_factory=set)
    reasoning_observed: bool = False
    plans: dict[str, str] = field(default_factory=dict)
    agent_message_phases: dict[str, str] = field(default_factory=dict)
    commentary: dict[str, str] = field(default_factory=dict)
    commentary_emitted: set[str] = field(default_factory=set)

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

    def remember_reasoning_item(self, item_id: str) -> bool:
        self.reasoning_observed = True
        if item_id in self.reasoning_items_seen:
            return True
        if len(self.reasoning_items_seen) >= _MAX_TIMELINE_ITEMS:
            return False
        self.reasoning_items_seen.add(item_id)
        return True

    def remember_reasoning_key(
        self, target: set[tuple[str, int]], key: tuple[str, int]
    ) -> bool:
        if key in target:
            return True
        if len(target) >= _MAX_TIMELINE_ITEMS:
            return False
        target.add(key)
        return True

    def append_reasoning(
        self,
        target: dict[tuple[str, int], str],
        key: tuple[str, int],
        delta: str,
        limit: int,
    ) -> str:
        if key not in target and len(target) >= _MAX_TIMELINE_ITEMS:
            return ""
        current = target.get(key, "")
        remaining = max(0, limit - len(current))
        if remaining:
            current += delta[:remaining]
            target[key] = current
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
        "agentMessage",
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

    if kind == "agentMessage":
        phase = item.get("phase")
        if phase not in {"commentary", "final_answer"}:
            return None
        state.put(state.agent_message_phases, activity_id, phase)
        if phase != "commentary":
            return None
        text = item.get("text")
        if isinstance(text, str) and text:
            state.put(
                state.commentary,
                activity_id,
                _redact_timeline_text(text, limit=_MAX_TIMELINE_TEXT_CHARS),
            )
        else:
            text = state.commentary.get(activity_id, "")
        event = _timeline_event(
            activity_id=activity_id,
            kind="progressNote",
            title=(
                "Progress update"
                if lifecycle != "completed"
                else "Progress update completed"
            ),
            detail=str(text or ""),
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
            duration_ms=duration_ms,
        )
        event["publicDetail"] = event["activityDetail"]
        return event

    if kind == "commandExecution":
        details = []
        exit_code = item.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            details.append(f"Exit code: {exit_code}")
        title = "Running command" if lifecycle != "completed" else "Command completed"
        if status == "failed":
            title = "Command failed"
        event = _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title=title,
            detail="\n".join(details),
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
            duration_ms=duration_ms,
        )
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            event["exitCode"] = exit_code
        return event

    if kind == "mcpToolCall":
        server = _redact_timeline_text(item.get("server"), limit=128)
        tool = _redact_timeline_text(item.get("tool"), limit=256)
        target = ".".join(part for part in (server, tool) if part) or "MCP tool"
        argument_names = _argument_names(item.get("arguments"))
        detail = "Arguments: " + ", ".join(argument_names) if argument_names else ""
        if lifecycle == "completed" and item.get("error") is not None:
            status = "failed"
        title = f"Calling {target}" if lifecycle != "completed" else f"Called {target}"
        if status == "failed":
            title = f"Call failed: {target}"
        event = _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title=title,
            detail=detail,
            status=status,
            lifecycle=lifecycle,
            elapsed=elapsed,
            duration_ms=duration_ms,
        )
        event["activityTarget"] = target
        event["argumentNames"] = list(argument_names)
        return event

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
        return _timeline_event(
            activity_id=activity_id,
            kind=kind,
            title="Viewing image" if lifecycle != "completed" else "Viewed image",
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
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
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
            raise ConnectorError(
                "cannot start Codex app-server; verify the configured executable and "
                "connection settings"
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
        """Decline approval/elicitation and reject every other Host callback."""

        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str) or not method or not _valid_request_id(request_id):
            raise ConnectorError("Codex app-server emitted an invalid server-request envelope")
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
            self._put_priority_notification(
                _DeniedServerRequest(
                    PolicyError(
                        f"Codex requested unsupported host interaction '{method}'"
                    )
                )
            )
            response = {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "ReplayLab does not permit server-initiated interaction",
                },
            }
        # A well-formed ambient command/file/permission/elicitation request is
        # answered with an explicit decline. The provider can continue through
        # its task-scoped ReplayLab MCP. Any other Host callback is a fatal
        # isolation violation and is also queued above.
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
        expected_mcp_servers: set[str] | frozenset[str] = frozenset(),
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
            raw_root = Path(root)
            try:
                resolved = raw_root.resolve(strict=True)
                details = raw_root.lstat()
            except OSError as exc:
                raise ConfigurationError(
                    f"Codex readable root does not exist: {raw_root}"
                ) from exc
            # A file is allowed only for an exact current-turn path reference;
            # accepting arbitrary files here would accidentally widen the
            # provider's workspace.  Canonical identity and link/reparse checks
            # remain enforced by ProviderPathAttachment before run().
            is_reparse = bool(getattr(details, "st_file_attributes", 0) & 0x400)
            if (
                os.path.normcase(os.path.normpath(str(raw_root)))
                != os.path.normcase(os.path.normpath(str(resolved)))
                or raw_root.is_symlink()
                or is_reparse
                or not (resolved.is_dir() or resolved.is_file())
            ):
                raise ConfigurationError(
                    f"Codex readable root must be a canonical directory or exact file: {raw_root}"
                )
            if resolved not in resolved_roots:
                resolved_roots.append(resolved)
        if self.cwd not in resolved_roots:
            resolved_roots.insert(0, self.cwd)
        self.readable_roots = tuple(resolved_roots)
        if any(not isinstance(value, str) or not value.strip() for value in expected_mcp_servers):
            raise ConfigurationError("Codex expected MCP server names must be non-empty strings")
        self.expected_mcp_servers = frozenset(
            value.strip().lower() for value in expected_mcp_servers
        )
        if timeout <= 0:
            raise ConfigurationError("Codex app-server timeout must be positive")
        self.timeout = float(timeout)
        if heartbeat_interval <= 0 or heartbeat_interval > 60:
            raise ConfigurationError(
                "Codex app-server heartbeat interval must be between 0 and 60 seconds"
            )
        self.heartbeat_interval = float(heartbeat_interval)
        self._transport = _JsonlProcess(
            self.command,
            cwd=self.cwd,
            env=env,
        )
        self._initialized = False
        self._run_lock = asyncio.Lock()

    async def __aenter__(self) -> CodexAppServerConnector:
        # Construction and context entry are deliberately lazy; run() starts the
        # process only when the selected backend is actually used.
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._transport.close()
        self._initialized = False

    def _materialize_attachment_inputs(
        self, attachments: Sequence[ProviderBinaryAttachment]
    ) -> tuple[Path | None, list[dict[str, Any]]]:
        """Write validated bytes below one generated, turn-local directory."""

        values = tuple(attachments)
        if not values:
            return None, []
        if len(values) > MAX_CONVERSATION_ATTACHMENTS_PER_TURN:
            raise ConfigurationError("Codex attachment count exceeds the per-turn limit")
        if sum(len(attachment.data) for attachment in values) > (
            MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES
        ):
            raise ConfigurationError("Codex attachments exceed the 8 MiB per-turn limit")
        root = self.cwd / f".replaylab-attachments-{uuid.uuid4().hex}"
        if root.parent != self.cwd or root.exists():
            raise ConfigurationError("Codex attachment workspace identity is invalid")
        try:
            root.mkdir(mode=0o700)
            inputs: list[dict[str, Any]] = []
            for index, attachment in enumerate(values):
                suffix = _IMAGE_SUFFIXES.get(attachment.media_type, ".bin")
                path = root / f"{index:02d}-{attachment.id[:64]}{suffix}"
                if path.parent != root or path.exists():
                    raise ConfigurationError("Codex attachment path is invalid")
                with path.open("xb") as stream:
                    stream.write(attachment.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Windows maps a missing write bit to the read-only file
                # attribute, which prevents shutil.rmtree from removing the
                # generated turn directory. The directory is already private
                # and random; retain the stricter mode where POSIX permissions
                # have their intended meaning.
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        path.chmod(0o400)
                if attachment.kind == "image":
                    inputs.append({"type": "localImage", "path": str(path)})
                else:
                    inputs.append(
                        {
                            "type": "mention",
                            "name": attachment.title,
                            "path": str(path),
                        }
                    )
            return root, inputs
        except BaseException:
            with contextlib.suppress(OSError):
                shutil.rmtree(root)
            raise

    @staticmethod
    def _path_attachment_inputs(
        attachments: Sequence[ProviderPathAttachment],
    ) -> list[dict[str, Any]]:
        """Expose current-turn files as exact Codex mentions, never as prompt text.

        A path reference is Host-admitted and revalidated immediately before it
        crosses the provider boundary.  The provider receives the original
        canonical file path only in the typed mention input; ACP and model
        adapters intentionally never call this path.
        """

        inputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        if len(tuple(attachments)) > MAX_CONVERSATION_ATTACHMENTS_PER_TURN:
            raise ConfigurationError("Codex path attachment count exceeds the per-turn limit")
        for attachment in tuple(attachments):
            if attachment.id in seen:
                continue
            attachment.revalidate()
            seen.add(attachment.id)
            inputs.append(
                {
                    "type": "mention",
                    "name": attachment.title,
                    "path": str(attachment.path),
                }
            )
        return inputs

    def _cleanup_attachment_root(self, root: Path | None) -> bool:
        if root is None:
            return True
        path = Path(root)
        if (
            not path.is_absolute()
            or path.parent != self.cwd
            or _ATTACHMENT_ROOT_RE.fullmatch(path.name) is None
        ):
            return False
        if not path.exists():
            return True
        if path.is_symlink() or not path.is_dir():
            return False
        try:
            shutil.rmtree(path)
        except OSError:
            return False
        return not path.exists()

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
        # Current app-server gates ``runtimeWorkspaceRoots`` behind the
        # experimental capability. ReplayLab always supplies and verifies those
        # roots, including capture-free turns without a ReplayLab MCP server.
        params["capabilities"] = {"experimentalApi": True}
        try:
            await self._transport.request(
                "initialize",
                params,
                timeout=min(self.timeout, 30.0),
            )
        except _AppServerRequestError as exc:
            if self._is_permission_protocol_incompatibility(exc):
                raise self._permission_capability_error(
                    "The configured Codex app-server cannot negotiate runtime workspace roots.",
                    error_code=_PERMISSION_PROTOCOL_UNSUPPORTED,
                ) from exc
            raise
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
        attachments: Sequence[ProviderBinaryAttachment] = (),
        path_attachments: Sequence[ProviderPathAttachment] = (),
        skill_name: str | None = None,
        skill_path: Path | str | None = None,
        on_delta: DeltaCallback | None = None,
        on_activity: ActivityCallback | None = None,
        on_timeline: TimelineCallback | None = None,
        on_reasoning: ReasoningCallback | None = None,
        on_assistant_message: AssistantMessageCallback | None = None,
    ) -> ExternalAgentResult:
        if not prompt.strip():
            raise ConfigurationError("Codex prompt must not be empty")
        if len(tuple(attachments)) + len(tuple(path_attachments)) > MAX_CONVERSATION_ATTACHMENTS_PER_TURN:
            raise ConfigurationError("Codex attachment count exceeds the per-turn limit")
        attachment_ids = [item.id for item in (*tuple(attachments), *tuple(path_attachments))]
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ConfigurationError("Codex attachment ids must be unique")
        attachment_root: Path | None = None
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
                await self._require_read_only_permission_profile()
                runtime_roots = self._runtime_workspace_roots()
                thread_params: dict[str, Any] = {
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    "serviceName": "replaylab",
                    "ephemeral": True,
                    "permissions": _READ_ONLY_PERMISSION_PROFILE,
                    "runtimeWorkspaceRoots": runtime_roots,
                }
                if model:
                    thread_params["model"] = model
                thread_result = await self._start_thread(thread_params)
                self._verify_thread_read_scope(thread_result, runtime_roots)
                thread = thread_result.get("thread")
                raw_thread_id = thread.get("id") if isinstance(thread, dict) else None
                if not isinstance(raw_thread_id, str) or not raw_thread_id:
                    raise ConnectorError("Codex app-server thread/start returned no thread id")
                thread_id = raw_thread_id
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
                attachment_root, attachment_inputs = self._materialize_attachment_inputs(
                    attachments
                )
                inputs: list[dict[str, Any]] = [
                    {"type": "text", "text": text},
                    *attachment_inputs,
                    *self._path_attachment_inputs(path_attachments),
                ]
                if skill_name or skill_path:
                    if not skill_name or not skill_path:
                        raise ConfigurationError("Codex skill invocation requires both name and path")
                    resolved_skill = Path(skill_path).resolve()
                    if not resolved_skill.is_file():
                        raise ConfigurationError(f"Codex skill file does not exist: {resolved_skill}")
                    marker = f"${skill_name}"
                    if not text.lstrip().startswith(marker):
                        text = f"{marker} {text}"
                        inputs[0] = {"type": "text", "text": text}
                    inputs.append(
                        {"type": "skill", "name": skill_name, "path": str(resolved_skill)}
                    )
                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": inputs,
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    # Repeat the already-verified built-in profile and exact roots
                    # so a turn cannot inherit a later ambient configuration
                    # change. ``permissions`` and ``sandboxPolicy`` are mutually
                    # exclusive in the current protocol.
                    "permissions": _READ_ONLY_PERMISSION_PROFILE,
                    "runtimeWorkspaceRoots": runtime_roots,
                    # Request the most useful public fallback when the selected
                    # model does not expose plaintext reasoning content.
                    "summary": "detailed",
                }
                if model:
                    turn_params["model"] = model
                turn_result = await self._start_turn(turn_params)
                turn = turn_result.get("turn")
                raw_turn_id = turn.get("id") if isinstance(turn, dict) else None
                if not isinstance(raw_turn_id, str) or not raw_turn_id:
                    raise ConnectorError("Codex app-server turn/start returned no turn id")
                turn_id = raw_turn_id

                active_reasoning: dict[str, dict[str, Any]] = {}

                async def tracked_reasoning(event: Mapping[str, Any]) -> None:
                    reasoning_id = event.get("reasoningId")
                    key = reasoning_id if isinstance(reasoning_id, str) else ""
                    lifecycle = str(event.get("lifecycle", "")).casefold()
                    if key and lifecycle not in {
                        "completed",
                        "failed",
                        "cancelled",
                        "canceled",
                        "truncated",
                        "unavailable",
                    }:
                        active_reasoning[key] = dict(event)
                    await _emit_reasoning(on_reasoning, event)
                    if key and lifecycle in {
                        "completed",
                        "failed",
                        "cancelled",
                        "canceled",
                        "truncated",
                        "unavailable",
                    }:
                        active_reasoning.pop(key, None)

                try:
                    answer = await self._collect_turn(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        on_delta=on_delta,
                        on_activity=on_activity,
                        on_timeline=on_timeline,
                        on_reasoning=tracked_reasoning if on_reasoning is not None else None,
                        on_assistant_message=on_assistant_message,
                    )
                    # A path can be replaced while Codex is producing its
                    # answer.  Revalidate after the terminal provider event as
                    # well as before constructing the mention; otherwise a
                    # result could be accepted for bytes different from the
                    # Host-admitted identity.
                    for attachment in tuple(path_attachments):
                        attachment.revalidate()
                except BaseException as exc:
                    terminal_lifecycle = (
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                    )
                    for active in tuple(active_reasoning.values()):
                        terminal = dict(active)
                        terminal.update(
                            {
                                "text": "",
                                "reset": False,
                                "lifecycle": terminal_lifecycle,
                            }
                        )
                        with contextlib.suppress(Exception):
                            await _emit_reasoning(on_reasoning, terminal)
                    with contextlib.suppress(Exception):
                        await self._transport.request(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                            timeout=2.0,
                        )
                    raise
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
                try:
                    await self.close()
                finally:
                    # Attachment bytes have their own lifecycle: a transport-close
                    # failure must not skip deletion in direct connector callers.
                    cleanup_ok = self._cleanup_attachment_root(attachment_root)
                    if not cleanup_ok and sys.exc_info()[0] is None:
                        raise LifecycleCleanupError(
                            "Codex attachment workspace cleanup failed"
                        )

    async def _start_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start one thread under the current verifiable permission contract."""

        try:
            return await self._transport.request(
                "thread/start", dict(params), timeout=min(self.timeout, 60.0)
            )
        except _AppServerRequestError as exc:
            if self._is_permission_protocol_incompatibility(exc):
                raise self._permission_capability_error(
                    "The configured Codex app-server does not support ReplayLab's "
                    "scoped read-only permission contract.",
                    error_code=_PERMISSION_PROTOCOL_UNSUPPORTED,
                ) from exc
            raise

    async def _start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start one turn without weakening its verified permission profile."""

        try:
            return await self._transport.request(
                "turn/start", params, timeout=min(self.timeout, 60.0)
            )
        except _AppServerRequestError as exc:
            if self._is_permission_protocol_incompatibility(exc):
                raise self._permission_capability_error(
                    "The configured Codex app-server does not support ReplayLab's "
                    "scoped read-only permission contract.",
                    error_code=_PERMISSION_PROTOCOL_UNSUPPORTED,
                ) from exc
            raise

    def _runtime_workspace_roots(self) -> list[str]:
        """Return the exact canonical roots admitted by the Host for this run."""

        return [str(root) for root in self.readable_roots]

    @staticmethod
    def _runtime_root_key(value: Any) -> str | None:
        if not isinstance(value, str) or not value or "\0" in value:
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        return os.path.normcase(os.path.normpath(str(path)))

    @staticmethod
    def _permission_capability_error(
        message: str, *, error_code: str
    ) -> ProviderCapabilityError:
        return ProviderCapabilityError(
            message,
            error_code=error_code,
            capability=_PERMISSION_PROFILE_CAPABILITY,
        )

    @staticmethod
    def _is_permission_protocol_incompatibility(exc: _AppServerRequestError) -> bool:
        message = exc.server_message.casefold()
        if exc.method == "initialize" and exc.code in {-32600, -32602}:
            return "experimentalapi" in message or "capabilities" in message
        if exc.method == "permissionProfile/list" and exc.code in {-32601, -32600, -32602}:
            return True
        if exc.method not in {"thread/start", "turn/start"} or exc.code not in {
            -32600,
            -32602,
        }:
            return False
        fields = (
            "permissions",
            "permissionprofile",
            "permission profile",
            "runtimeworkspaceroots",
            "runtime workspace roots",
            "experimentalapi",
            "readonly.access",
        )
        return any(field in message for field in fields)

    async def _require_read_only_permission_profile(self) -> None:
        """Require the built-in read-only profile before starting any thread."""

        cursor: str | None = None
        seen_ids: set[str] = set()
        selected_allowed: bool | None = None
        for _page in range(100):
            params: dict[str, Any] = {"cwd": str(self.cwd), "limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                result = await self._transport.request(
                    "permissionProfile/list", params, timeout=min(self.timeout, 30.0)
                )
            except _AppServerRequestError as exc:
                if self._is_permission_protocol_incompatibility(exc):
                    raise self._permission_capability_error(
                        "The configured Codex app-server cannot enumerate permission profiles.",
                        error_code=_PERMISSION_PROTOCOL_UNSUPPORTED,
                    ) from exc
                raise
            data = result.get("data")
            if not isinstance(data, list):
                raise ConnectorError(
                    "Codex permission profile inventory returned an invalid data array"
                )
            for item in data:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not isinstance(item.get("allowed"), bool)
                ):
                    raise ConnectorError(
                        "Codex permission profile inventory contained an invalid entry"
                    )
                profile_id = item["id"]
                if not profile_id or profile_id in seen_ids:
                    raise ConnectorError(
                        "Codex permission profile inventory contained a duplicate/empty id"
                    )
                seen_ids.add(profile_id)
                if profile_id == _READ_ONLY_PERMISSION_PROFILE:
                    selected_allowed = item["allowed"]
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise ConnectorError(
                    "Codex permission profile inventory returned an invalid cursor"
                )
            cursor = next_cursor
        else:
            raise ConnectorError(
                "Codex permission profile inventory exceeded the pagination safety bound"
            )
        if selected_allowed is not True:
            raise self._permission_capability_error(
                "The built-in Codex read-only permission profile is unavailable.",
                error_code=_READ_ONLY_PROFILE_UNAVAILABLE,
            )

    def _verify_thread_read_scope(
        self, result: Mapping[str, Any], expected_roots: Sequence[str]
    ) -> None:
        """Verify that app-server applied the exact requested profile and roots."""

        active = result.get("activePermissionProfile")
        roots = result.get("runtimeWorkspaceRoots")
        sandbox = result.get("sandbox")
        expected_keys = tuple(self._runtime_root_key(root) for root in expected_roots)
        actual_keys = (
            tuple(self._runtime_root_key(root) for root in roots)
            if isinstance(roots, list)
            else ()
        )
        verified = all(
            (
                isinstance(active, dict),
                isinstance(active, dict)
                and active.get("id") == _READ_ONLY_PERMISSION_PROFILE,
                isinstance(active, dict) and active.get("extends") is None,
                bool(expected_keys) and None not in expected_keys,
                actual_keys == expected_keys,
                isinstance(sandbox, dict),
                isinstance(sandbox, dict) and sandbox.get("type") == "readOnly",
                isinstance(sandbox, dict)
                and sandbox.get("networkAccess", False) is False,
                result.get("approvalPolicy") == "never",
            )
        )
        if not verified:
            raise self._permission_capability_error(
                "Codex app-server did not verify ReplayLab's exact read-only roots.",
                error_code=_READ_SCOPE_UNVERIFIED,
            )

    async def _list_thread_mcp_servers(
        self, thread_id: str, *, deadline: float
    ) -> dict[str, dict[str, Any]]:
        """Read one complete, bounded MCP inventory for a loaded thread."""

        loop = asyncio.get_running_loop()
        cursor: str | None = None
        entries: dict[str, dict[str, Any]] = {}
        for _page in range(100):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ConnectorError(
                    "Codex app-server timed out while reading task-scoped MCP status"
                )
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "detail": "toolsAndAuthOnly",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._transport.request(
                "mcpServerStatus/list", params, timeout=min(remaining, 60.0)
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

        return entries

    async def _verify_tool_isolation(self, thread_id: str) -> None:
        """Wait for exactly the task MCP set, then reject ambient tools and Apps."""

        loop = asyncio.get_running_loop()
        startup_timeout = min(self.timeout, _MCP_STARTUP_TIMEOUT_SECONDS)
        deadline = loop.time() + startup_timeout
        waiting = [
            f"{name} (missing)" for name in sorted(self.expected_mcp_servers)
        ]
        while True:
            try:
                entries = await self._list_thread_mcp_servers(
                    thread_id, deadline=deadline
                )
            except ConnectorError as exc:
                rendered_error = str(exc).casefold()
                if "timed out" in rendered_error and (
                    "mcpserverstatus/list" in rendered_error
                    or "task-scoped mcp status" in rendered_error
                ):
                    raise ConnectorError(
                        "Codex app-server timed out waiting for the task-scoped MCP "
                        "server(s): "
                        + ", ".join(waiting)
                    ) from exc
                raise
            actual_servers = frozenset(entries)
            unexpected = actual_servers - self.expected_mcp_servers
            if unexpected:
                if not self.expected_mcp_servers:
                    raise PolicyError(
                        "Codex ReplayLab threads must not load MCP servers; observed: "
                        + ", ".join(sorted(entries))
                    )
                raise PolicyError(
                    "Codex ReplayLab thread loaded an MCP server outside its task scope; "
                    "expected "
                    + ", ".join(sorted(self.expected_mcp_servers))
                    + "; observed "
                    + ", ".join(sorted(entries))
                )

            waiting = [
                f"{name} (missing)"
                for name in sorted(self.expected_mcp_servers - actual_servers)
            ]
            terminal: list[str] = []
            for name in sorted(self.expected_mcp_servers & actual_servers):
                entry = entries[name]
                runtime_status = entry.get("runtimeStatus")
                tools = entry.get("tools")
                if runtime_status == "connected":
                    if not isinstance(tools, dict):
                        raise ConnectorError(
                            "Codex MCP inventory returned an invalid tool inventory for " + name
                        )
                    if not tools:
                        raise ConnectorError(
                            "Codex app-server did not initialize the task-scoped MCP "
                            f"server(s): {name} (empty tool inventory)"
                        )
                    continue
                if runtime_status in {
                    "authenticationRequired",
                    "failed",
                    "cancelled",
                    "disabled",
                }:
                    terminal.append(f"{name} (runtimeStatus={runtime_status})")
                    continue
                if runtime_status is None:
                    waiting.append(f"{name} (runtimeStatus=unknown)")
                    continue
                if runtime_status in {"notStarted", "starting"}:
                    waiting.append(f"{name} (runtimeStatus={runtime_status})")
                    continue
                raise ConnectorError(
                    "Codex MCP inventory returned an unknown runtime status for "
                    f"{name}: {runtime_status!r}"
                )

            if terminal:
                raise ConnectorError(
                    "Codex app-server did not initialize the task-scoped MCP server(s): "
                    + ", ".join(terminal)
                )
            if not waiting:
                break

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ConnectorError(
                    "Codex app-server timed out waiting for the task-scoped MCP server(s): "
                    + ", ".join(waiting)
                )
            await asyncio.sleep(min(_MCP_STARTUP_POLL_SECONDS, remaining))

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
        on_reasoning: ReasoningCallback | None = None,
        on_assistant_message: AssistantMessageCallback | None = None,
    ) -> str:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout
        streamed: list[str] = []
        streamed_chars = 0
        streamed_bytes = 0
        final_message = ""
        last_provider_notice = ""
        timeline_state = _TimelineState()

        async def emit_reasoning_event(
            *,
            item_id: str,
            channel: str,
            index: int = 0,
            text: str = "",
            reset: bool = False,
            lifecycle: str = "updated",
            availability: str = "available",
            opaque_present: bool = False,
            opaque_bytes: int = 0,
            opaque_sha256: str = "",
        ) -> None:
            event: dict[str, Any] = {
                "source": "codex-app-server",
                "channel": channel,
                "itemId": item_id,
                # JSON tuple encoding is unambiguous even when provider item ids
                # contain separators such as colons.
                "reasoningId": json.dumps(
                    [item_id, channel, index], ensure_ascii=False, separators=(",", ":")
                ),
                "contentIndex": index if channel == "raw" else 0,
                "summaryIndex": index if channel == "summary" else 0,
                "contentFormat": "text/plain",
                "text": text,
                "reset": bool(reset),
                "lifecycle": lifecycle,
                "availability": availability,
                "threadId": _item_id(thread_id),
                "turnId": _item_id(turn_id),
            }
            if opaque_present:
                # Provider ciphertext/signatures are continuity material, not
                # readable chain-of-thought. Keep only bounded metadata so they
                # cannot be rendered, logged as prose, or replayed into prompts.
                event["opaquePresent"] = True
                event["opaqueBytes"] = max(0, int(opaque_bytes))
                if opaque_sha256:
                    event["opaqueSha256"] = opaque_sha256
            await _emit_reasoning(on_reasoning, event)

        async def emit_commentary_event(
            *, item_id: str, text: str, reset: bool, lifecycle: str
        ) -> None:
            if on_assistant_message is None or not item_id or not text:
                return
            # Only provider-declared commentary is admitted to this private local-observability
            # callback. Redact common credentials before the text becomes a durable TaskEvent.
            safe_text = _redact_timeline_text(text, limit=_MAX_TIMELINE_TEXT_CHARS)
            if not safe_text:
                return
            await _emit_assistant_message(
                on_assistant_message,
                {
                    "source": "codex-app-server",
                    "channel": "message",
                    "phase": "commentary",
                    "itemId": item_id,
                    "text": safe_text,
                    "reset": bool(reset),
                    "lifecycle": lifecycle,
                    "threadId": _item_id(thread_id),
                    "turnId": _item_id(turn_id),
                },
            )

        async def emit_reasoning_item(
            item: Mapping[str, Any], lifecycle: str
        ) -> None:
            item_id = _item_id(item.get("id"))
            if not item_id or item.get("type") != "reasoning":
                return
            tracked_item = timeline_state.remember_reasoning_item(item_id)
            if lifecycle == "completed" and tracked_item:
                timeline_state.reasoning_items_completed.add(item_id)

            raw_parts = _reasoning_content_parts(item.get("content"))
            raw_keys: list[tuple[str, int]] = []
            for index, value in raw_parts:
                key = (item_id, index)
                raw_keys.append(key)
                previous = timeline_state.raw_reasoning.get(key, "")
                already_emitted = key in timeline_state.raw_reasoning_emitted
                # A completed item is authoritative. If a provider omitted,
                # truncated, or transformed streamed deltas, reset to the exact
                # final snapshot instead of leaving a stale reconstruction.
                reset = not already_emitted or previous != value
                delta = value if reset else ""
                if key in timeline_state.raw_reasoning or len(
                    timeline_state.raw_reasoning
                ) < _MAX_TIMELINE_ITEMS:
                    timeline_state.raw_reasoning[key] = value
                tracked_key = timeline_state.remember_reasoning_key(
                    timeline_state.raw_reasoning_emitted, key
                )
                await emit_reasoning_event(
                    item_id=item_id,
                    channel="raw",
                    index=index,
                    text=delta,
                    reset=reset,
                    lifecycle=lifecycle if tracked_key else "truncated",
                    availability="available" if tracked_key else "state-limit",
                )

            emitted_keys = tuple(sorted(
                key
                for key in timeline_state.raw_reasoning_emitted
                if key[0] == item_id
            ))
            if lifecycle == "completed":
                # Some app-server versions stream deltas but omit ``content``
                # from the completed item. Explicitly close every such stream.
                for _, index in emitted_keys:
                    if (item_id, index) not in raw_keys:
                        await emit_reasoning_event(
                            item_id=item_id,
                            channel="raw",
                            index=index,
                            lifecycle="completed",
                        )

            opaque_bytes, opaque_sha256 = _opaque_reasoning_metadata(item)
            if opaque_bytes:
                opaque_metadata = (opaque_bytes, opaque_sha256)
                previous_opaque = timeline_state.opaque_reasoning.get(item_id)
                if item_id in timeline_state.opaque_reasoning or len(
                    timeline_state.opaque_reasoning
                ) < _MAX_TIMELINE_ITEMS:
                    timeline_state.opaque_reasoning[item_id] = opaque_metadata
                await emit_reasoning_event(
                    item_id=item_id,
                    channel="opaque",
                    text="",
                    reset=opaque_metadata != previous_opaque,
                    lifecycle=lifecycle,
                    availability="encrypted-only",
                    opaque_present=True,
                    opaque_bytes=opaque_bytes,
                    opaque_sha256=opaque_sha256,
                )
            if lifecycle == "completed" and not raw_keys and not emitted_keys:
                await emit_reasoning_event(
                    item_id=item_id,
                    channel="raw",
                    lifecycle=lifecycle,
                    availability="encrypted-only" if opaque_bytes else "not-emitted",
                )

            summary_parts = _reasoning_content_parts(item.get("summary"))
            summary_keys: list[tuple[str, int]] = []
            for index, value in summary_parts:
                key = (item_id, index)
                summary_keys.append(key)
                previous = timeline_state.reasoning_streams.get(key, "")
                already_emitted = key in timeline_state.summary_reasoning_emitted
                reset = not already_emitted or previous != value
                if key in timeline_state.reasoning_streams or len(
                    timeline_state.reasoning_streams
                ) < _MAX_TIMELINE_ITEMS:
                    timeline_state.reasoning_streams[key] = value
                tracked_key = timeline_state.remember_reasoning_key(
                    timeline_state.summary_reasoning_emitted, key
                )
                await emit_reasoning_event(
                    item_id=item_id,
                    channel="summary",
                    index=index,
                    text=value if reset else "",
                    reset=reset,
                    lifecycle=lifecycle if tracked_key else "truncated",
                    availability=(
                        "available"
                        if value and tracked_key
                        else "state-limit" if not tracked_key else "not-emitted"
                    ),
                )
            if lifecycle == "completed":
                emitted_summary_keys = tuple(sorted(
                    key
                    for key in timeline_state.summary_reasoning_emitted
                    if key[0] == item_id
                ))
                for _, index in emitted_summary_keys:
                    if (item_id, index) not in summary_keys:
                        await emit_reasoning_event(
                            item_id=item_id,
                            channel="summary",
                            index=index,
                            lifecycle="completed",
                        )
            if (
                lifecycle == "started"
                and not raw_parts
                and not opaque_bytes
                and not summary_parts
            ):
                # An empty reasoning item still proves that the provider began
                # a reasoning phase. Surface that liveness immediately instead
                # of leaving the monitor indistinguishable from a stalled turn.
                await emit_reasoning_event(
                    item_id=item_id,
                    channel="raw",
                    lifecycle="started",
                    availability="pending",
                )

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

            if method == "item/reasoning/textDelta":
                item_id = _item_id(params.get("itemId"))
                delta = params.get("delta")
                raw_index = params.get("contentIndex", 0)
                if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                    raw_index = 0
                raw_index = max(0, min(raw_index, 255))
                if item_id and isinstance(delta, str) and delta:
                    timeline_state.remember_reasoning_item(item_id)
                    key = (item_id, raw_index)
                    tracked = timeline_state.remember_reasoning_key(
                        timeline_state.raw_reasoning_deltas, key
                    ) and timeline_state.remember_reasoning_key(
                        timeline_state.raw_reasoning_emitted, key
                    )
                    timeline_state.append_reasoning(
                        timeline_state.raw_reasoning,
                        key,
                        delta,
                        MAX_EXTERNAL_RESPONSE_CHARS,
                    )
                    await emit_reasoning_event(
                        item_id=item_id,
                        channel="raw",
                        index=raw_index,
                        text=delta,
                        lifecycle="updated" if tracked else "truncated",
                        availability="available" if tracked else "state-limit",
                    )
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

            if method == "item/reasoning/summaryPartAdded":
                item_id = _item_id(params.get("itemId"))
                summary_index = params.get("summaryIndex", 0)
                if isinstance(summary_index, bool) or not isinstance(summary_index, int):
                    summary_index = 0
                summary_index = max(0, min(summary_index, 255))
                if item_id:
                    timeline_state.remember_reasoning_item(item_id)
                    key = (item_id, summary_index)
                    tracked = timeline_state.remember_reasoning_key(
                        timeline_state.summary_reasoning_emitted, key
                    )
                    await emit_reasoning_event(
                        item_id=item_id,
                        channel="summary",
                        index=summary_index,
                        lifecycle="started" if tracked else "truncated",
                        availability="available" if tracked else "state-limit",
                    )
                continue

            if method == "item/reasoning/summaryTextDelta":
                item_id = _item_id(params.get("itemId"))
                delta = params.get("delta")
                summary_index = params.get("summaryIndex", 0)
                if isinstance(summary_index, bool) or not isinstance(summary_index, int):
                    summary_index = 0
                summary_index = max(0, min(summary_index, 255))
                if item_id and isinstance(delta, str) and delta:
                    timeline_state.remember_reasoning_item(item_id)
                    key = (item_id, summary_index)
                    tracked = timeline_state.remember_reasoning_key(
                        timeline_state.summary_reasoning_emitted, key
                    )
                    timeline_state.append_reasoning(
                        timeline_state.reasoning_streams,
                        key,
                        delta,
                        _MAX_REASONING_SUMMARY_CHARS,
                    )
                    await emit_reasoning_event(
                        item_id=item_id,
                        channel="summary",
                        index=summary_index,
                        text=delta,
                        lifecycle="updated" if tracked else "truncated",
                        availability="available" if tracked else "state-limit",
                    )
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
                # Command text, working directory, and stdout/stderr are private
                # execution details. The lifecycle item still exposes the action
                # category, completion status, and bounded exit code.
                continue

            if method == "item/started":
                item = params.get("item")
                if isinstance(item, Mapping):
                    if item.get("type") == "reasoning":
                        await emit_reasoning_item(item, "started")
                    timeline = _item_timeline_event(
                        item,
                        lifecycle="started",
                        elapsed=elapsed,
                        state=timeline_state,
                    )
                    if timeline is not None:
                        await _emit_timeline(on_timeline, timeline)
                    if item.get("type") == "agentMessage" and item.get("phase") == "commentary":
                        item_id = _item_id(item.get("id"))
                        candidate = item.get("text")
                        if item_id and isinstance(candidate, str) and candidate:
                            await emit_commentary_event(
                                item_id=item_id,
                                text=candidate,
                                reset=True,
                                lifecycle="started",
                            )
                            timeline_state.commentary_emitted.add(item_id)
                continue

            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str) and delta:
                    streamed_chars, streamed_bytes = checked_response_size(
                        delta, streamed_chars, streamed_bytes
                    )
                    item_id = _item_id(params.get("itemId"))
                    raw_phase = params.get("phase")
                    if raw_phase in {"commentary", "final_answer"} and item_id:
                        phase = raw_phase
                        timeline_state.put(timeline_state.agent_message_phases, item_id, phase)
                    elif raw_phase is None and item_id:
                        phase = timeline_state.agent_message_phases.get(item_id, "")
                    else:
                        # An explicit unrecognised phase must not inherit a prior
                        # public phase for the same item id.
                        phase = ""
                    if phase == "commentary" and item_id:
                        commentary = timeline_state.append(
                            timeline_state.commentary,
                            item_id,
                            delta,
                            _MAX_TIMELINE_TEXT_CHARS,
                        )
                        if commentary:
                            progress = _timeline_event(
                                activity_id=item_id,
                                kind="progressNote",
                                title="Progress update",
                                detail=_redact_timeline_text(
                                    commentary, limit=_MAX_TIMELINE_TEXT_CHARS
                                ),
                                status="running",
                                lifecycle="updated",
                                elapsed=elapsed,
                            )
                            progress["publicDetail"] = progress["activityDetail"]
                            await _emit_timeline(on_timeline, progress)
                        await emit_commentary_event(
                            item_id=item_id,
                            text=delta,
                            reset=False,
                            lifecycle="updated",
                        )
                        if on_assistant_message is not None:
                            timeline_state.commentary_emitted.add(item_id)
                    elif phase == "final_answer":
                        streamed.append(delta)
                        await _emit_delta(on_delta, delta)
                    # Unknown phases remain outside both the public timeline and
                    # answer stream. Their size still counts against the provider
                    # response bound above.
                continue
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, Mapping):
                    completed_item_id = _item_id(item.get("id"))
                    previous_commentary = timeline_state.commentary.get(
                        completed_item_id, ""
                    )
                    commentary_was_emitted = (
                        completed_item_id in timeline_state.commentary_emitted
                    )
                    if item.get("type") == "reasoning":
                        await emit_reasoning_item(item, "completed")
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
                            if item.get("phase") == "commentary" and completed_item_id:
                                safe_candidate = _redact_timeline_text(
                                    candidate, limit=_MAX_TIMELINE_TEXT_CHARS
                                )
                                safe_previous = _redact_timeline_text(
                                    previous_commentary,
                                    limit=_MAX_TIMELINE_TEXT_CHARS,
                                )
                                if safe_candidate and (
                                    not commentary_was_emitted
                                    or (
                                        len(candidate) <= _MAX_TIMELINE_TEXT_CHARS
                                        and safe_candidate != safe_previous
                                    )
                                ):
                                    await emit_commentary_event(
                                        item_id=completed_item_id,
                                        text=candidate,
                                        reset=True,
                                        lifecycle="completed",
                                    )
                            elif item.get("phase") == "final_answer":
                                final_message = candidate
                                # A completed final answer is authoritative over
                                # streamed fragments. Commentary and unknown phases
                                # never become fallback answer text.
                                streamed.clear()
                    if completed_item_id:
                        timeline_state.reasoning.pop(completed_item_id, None)
                        for key in tuple(timeline_state.reasoning_streams):
                            if key[0] == completed_item_id:
                                timeline_state.reasoning_streams.pop(key, None)
                        for key in tuple(timeline_state.raw_reasoning):
                            if key[0] == completed_item_id:
                                timeline_state.raw_reasoning.pop(key, None)
                        timeline_state.plans.pop(completed_item_id, None)
                        timeline_state.agent_message_phases.pop(completed_item_id, None)
                        timeline_state.commentary.pop(completed_item_id, None)
                        timeline_state.commentary_emitted.discard(completed_item_id)
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
            if not timeline_state.reasoning_observed:
                await emit_reasoning_event(
                    item_id=_item_id(turn_id) or "turn",
                    channel="raw",
                    lifecycle="unavailable",
                    availability="not-emitted",
                )
            else:
                for item_id in sorted(
                    timeline_state.reasoning_items_seen
                    - timeline_state.reasoning_items_completed
                ):
                    raw_keys = sorted(
                        key
                        for key in timeline_state.raw_reasoning_emitted
                        if key[0] == item_id
                    )
                    summary_keys = sorted(
                        key
                        for key in timeline_state.summary_reasoning_emitted
                        if key[0] == item_id
                    )
                    if raw_keys:
                        for _, index in raw_keys:
                            await emit_reasoning_event(
                                item_id=item_id,
                                channel="raw",
                                index=index,
                                lifecycle="completed",
                            )
                    else:
                        opaque_bytes, opaque_sha256 = timeline_state.opaque_reasoning.get(
                            item_id, (0, "")
                        )
                        await emit_reasoning_event(
                            item_id=item_id,
                            channel="raw",
                            lifecycle="unavailable",
                            availability=(
                                "encrypted-only" if opaque_bytes else "not-emitted"
                            ),
                            opaque_present=bool(opaque_bytes),
                            opaque_bytes=opaque_bytes,
                            opaque_sha256=opaque_sha256,
                        )
                    for _, index in summary_keys:
                        await emit_reasoning_event(
                            item_id=item_id,
                            channel="summary",
                            index=index,
                            lifecycle="completed",
                        )
            return final_message or "".join(streamed)


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


async def _emit_reasoning(
    callback: ReasoningCallback | None, event: Mapping[str, Any]
) -> None:
    if callback is None:
        return
    # Raw reasoning is an explicit, separate local-observability channel. It
    # must never be folded into the public activity timeline or answer stream.
    result = callback(dict(event))
    if inspect.isawaitable(result):
        await result


async def _emit_assistant_message(
    callback: AssistantMessageCallback | None, event: Mapping[str, Any]
) -> None:
    if callback is None:
        return
    # This callback carries only a bounded, redacted, provider-declared commentary message. It is
    # separate from final-answer deltas, reasoning, tool data, and the public progress timeline.
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
