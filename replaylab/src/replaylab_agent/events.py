"""Unified event stream consumed by the native qrenderdoc Assistant panel."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

TASK_EVENT_SCHEMA = "replaylab-ai-task-event/v1"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TAIL_READ_BYTES = 64 * 1024
_MAX_LAST_EVENT_BYTES = 16 * 1024 * 1024
_ARTIFACT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_PRUNE_ENTRIES = 4096
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token)", re.IGNORECASE
)
TaskEventKind = Literal[
    "queued",
    "planning",
    "running",
    "message-delta",
    "tool-started",
    "tool-completed",
    "approval-required",
    "completed",
    "failed",
    "cancelled",
]


def default_task_event_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path(tempfile.gettempdir())
    return root / "ReplayLab" / "ai-tasks"


def _prune_stale_task_events(root: Path, *, now: float | None = None) -> None:
    """Best-effort bounded retention for task transcripts.

    Only direct children whose names already satisfy the task-id contract are
    considered.  This keeps cleanup from following attacker-created directory
    trees or touching unrelated files in the ReplayLab data directory.
    """

    if not root.is_dir():
        return
    cutoff = (time.time() if now is None else now) - _ARTIFACT_RETENTION_SECONDS
    try:
        entries = list(root.iterdir())[:_MAX_PRUNE_ENTRIES]
    except OSError:
        return
    for entry in entries:
        if entry.suffix != ".jsonl" or not _TASK_ID_RE.fullmatch(entry.stem):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            # Retention must never make an AI request fail. A later task will
            # retry deletion after transient sharing/permission errors clear.
            continue


@dataclass(slots=True)
class TaskEvent:
    task_id: str
    kind: TaskEventKind
    sequence: int
    timestamp_ms: int
    backend: str = ""
    profile: str = ""
    summary: str = ""
    text: str = ""
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    schema: str = TASK_EVENT_SCHEMA

    def to_wire(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "schema": value.pop("schema"),
            "taskId": value.pop("task_id"),
            "kind": value.pop("kind"),
            "sequence": value.pop("sequence"),
            "timestampUnixMs": value.pop("timestamp_ms"),
            **value,
        }


class TaskEventWriter:
    """Append-only JSONL with one file per task and defensive key redaction."""

    def __init__(self, root: Path | str | None = None, task_id: str = "") -> None:
        self.root = Path(root) if root else default_task_event_root()
        self.task_id = task_id or uuid.uuid4().hex
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id contains unsafe path characters")
        _prune_stale_task_events(self.root)
        self._sequence = self._last_sequence()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.root / f"{self.task_id}.jsonl"

    def emit(self, kind: TaskEventKind, **values: Any) -> TaskEvent:
        with self._lock:
            self._sequence += 1
            if "data" in values:
                values["data"] = _redact(values["data"])
            event = TaskEvent(
                task_id=self.task_id,
                kind=kind,
                sequence=self._sequence,
                timestamp_ms=int(time.time() * 1000),
                **values,
            )
            self.root.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                event.to_wire(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8", errors="backslashreplace")
            with self.path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                end = stream.tell()
                separator = b""
                if end:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        # A killed writer may leave an incomplete JSON object at EOF. Keep
                        # that fragment on its own corrupt line so this event remains a
                        # parseable JSONL record and future writers can resume its sequence.
                        # This also completes a lone CR into a CRLF boundary.
                        separator = b"\n"
                stream.write(separator + encoded + b"\n")
                stream.flush()
            return event

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                position = stream.tell()
                suffix = b""
                scanned = 0
                while position > 0:
                    count = min(position, _TAIL_READ_BYTES)
                    position -= count
                    stream.seek(position)
                    suffix = stream.read(count) + suffix
                    scanned += count
                    lines = suffix.split(b"\n")
                    suffix = lines.pop(0)
                    for line in reversed(lines):
                        sequence = self._sequence_from_line(line.rstrip(b"\r"))
                        if sequence is not None:
                            return sequence
                    if scanned >= _MAX_LAST_EVENT_BYTES and position > 0:
                        raise ValueError("last task event exceeds the safety bound")
                sequence = self._sequence_from_line(suffix.rstrip(b"\r"))
                return sequence if sequence is not None else 0
        except OSError:
            return 0

    def _sequence_from_line(self, line: bytes) -> int | None:
        if not line:
            return None
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("schema") != TASK_EVENT_SCHEMA or value.get("taskId") != self.task_id:
            return None
        sequence = value.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return None
        return sequence


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY_RE.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value
