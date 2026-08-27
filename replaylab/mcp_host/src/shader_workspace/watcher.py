"""Bounded, auditable save watching for one tracked shader workspace source.

The watcher is deliberately a host-side convenience layer.  It obtains the
fixed, manifest-validated source path from ``ShaderWorkspaceService.status``
and invokes ``ShaderWorkspaceService.compile``.  It never talks to RenderDoc
and never promotes or applies a compiled candidate.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import WorkspaceWatchError
from .diagnostics import classify_shader_failure
from .io import atomic_write_json, is_within
from .leases import WorkspaceSourceLeaseError, WorkspaceSourceTransactionLease
from .workspace import MAX_SOURCE_BYTES, ShaderWorkspaceService

WATCH_SCHEMA = "renderdoc-ultra-shader-watch/v1"
WATCH_VERSION = 1
MIN_DEBOUNCE_MS = 100
MAX_DEBOUNCE_MS = 5000
DEFAULT_DEBOUNCE_MS = 100
MAX_ACTIVE_WATCHERS = 8
MAX_RETAINED_WATCHERS = 32
MAX_AUDIT_EVENTS = 128
STOP_JOIN_SECONDS = 2.0
MAX_TRANSIENT_COMPILE_RETRIES = 2
TRANSIENT_COMPILE_RETRY_DELAYS = (0.10, 0.25)
MAX_COMPILER_DIAGNOSTICS = 128
MAX_COMPILER_DIAGNOSTIC_BYTES = 1024 * 1024
_WATCH_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DXC_COLON_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\s*:?[ \t]*"
    r"(?P<message>.*)$",
    flags=re.IGNORECASE,
)
_DXC_PAREN_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)(?:\s*,\s*(?P<column>\d+))?\)\s*:"
    r"\s*(?P<severity>fatal\s+error|error|warning|note)\b\s*:?[ \t]*"
    r"(?P<message>.*)$",
    flags=re.IGNORECASE,
)
_JOB_HINT = re.compile(r"(?:^|[;\s])see\s+(.+?)\s*$", flags=re.IGNORECASE)


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error if isinstance(next_error, BaseException) else None


def _is_transient_compile_failure(error: BaseException) -> bool:
    """Retry only bounded local Windows sharing/access failures."""

    for item in _exception_chain(error):
        if isinstance(item, OSError):
            if getattr(item, "winerror", None) in {5, 32, 33}:
                return True
            if os.name == "nt" and getattr(item, "winerror", None) is None and getattr(
                item, "errno", None
            ) in {errno.EACCES, errno.EBUSY}:
                return True
        message = str(item)
        if any(token in message for token in ("[WinError 5]", "[WinError 32]", "[WinError 33]")):
            return True
    return False


def _diagnostic_texts(root: Path, error: BaseException) -> list[str]:
    texts = [str(item) for item in _exception_chain(error) if str(item)]
    jobs_root = (root / "jobs").resolve()
    for value in tuple(texts):
        match = _JOB_HINT.search(value)
        if match is None:
            continue
        hinted = Path(match.group(1).strip().strip('"'))
        try:
            job = hinted.resolve(strict=True)
            stderr = job / "stderr.log"
            if (
                not is_within(jobs_root, job)
                or job == jobs_root
                or job.is_symlink()
                or stderr.is_symlink()
                or not stderr.is_file()
                or stderr.stat().st_size > MAX_COMPILER_DIAGNOSTIC_BYTES
            ):
                continue
            texts.append(stderr.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _compiler_diagnostics(
    root: Path, source_path: Path, error: BaseException
) -> list[dict[str, Any]]:
    """Parse common DXC diagnostics into console/link-friendly records."""

    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    jobs_root = (root / "jobs").resolve()
    for text_value in _diagnostic_texts(root, error):
        for raw_line in text_value.splitlines():
            line = raw_line.strip()
            match = _DXC_COLON_DIAGNOSTIC.match(line)
            if match is None:
                match = _DXC_PAREN_DIAGNOSTIC.match(line)
            if match is None:
                continue
            file_value = match.group("file").strip().strip('"')
            try:
                diagnostic_path = Path(file_value).resolve()
                if is_within(jobs_root, diagnostic_path):
                    file_value = str(source_path)
            except OSError:
                pass
            severity = match.group("severity").lower().replace("fatal error", "error")
            message = _bounded_text(match.group("message"), 4096)
            record = {
                "file": file_value,
                "line": int(match.group("line")),
                "column": int(match.group("column") or 1),
                "severity": severity,
                "message": message,
            }
            key = tuple(record.values())
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(record)
            if len(diagnostics) >= MAX_COMPILER_DIAGNOSTICS:
                return diagnostics
    if diagnostics:
        return diagnostics
    return [
        {
            "file": str(source_path),
            "line": None,
            "column": None,
            "severity": "error",
            "message": _bounded_text(error, 4096),
        }
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, maximum: int = 2048) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    return text[:maximum] + "...[truncated]"


def _validate_debounce(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkspaceWatchError("debounce_ms must be an integer")
    if value < MIN_DEBOUNCE_MS or value > MAX_DEBOUNCE_MS:
        raise WorkspaceWatchError(
            "debounce_ms must be between %d and %d"
            % (MIN_DEBOUNCE_MS, MAX_DEBOUNCE_MS)
        )
    return value


def _fixed_source(root: Path, expected_source: Path) -> Path:
    """Return the only path a watcher may observe, rejecting redirection."""

    source_dir = root / "source"
    source = Path(expected_source)
    if not source.is_absolute() or source.parent != source_dir:
        raise WorkspaceWatchError(
            "workspace status source must be one direct child of the source directory"
        )
    if source_dir.is_symlink() or source.is_symlink():
        raise WorkspaceWatchError("workspace source path must not be a symlink")
    try:
        source_dir_resolved = source_dir.resolve(strict=True)
        source_resolved = source.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceWatchError(
            "workspace tracked source is not available: %s" % exc
        ) from exc
    if source_dir_resolved != source_dir or source_resolved != source:
        raise WorkspaceWatchError("workspace source path is redirected")
    if not is_within(root, source_resolved) or not source_resolved.is_file():
        raise WorkspaceWatchError(
            "workspace tracked source is not a regular in-workspace file"
        )
    return source_resolved


def _source_hash(root: Path, expected_source: Path) -> str:
    """Hash one stable UTF-8 source snapshot without accepting another path."""

    source = _fixed_source(root, expected_source)
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceWatchError("workspace shader source is not a regular file")
    if before.st_size <= 0:
        raise WorkspaceWatchError("workspace shader source is empty")
    if before.st_size > MAX_SOURCE_BYTES:
        raise WorkspaceWatchError(
            "workspace shader source exceeds %d bytes" % MAX_SOURCE_BYTES
        )
    with source.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise WorkspaceWatchError(
            "workspace shader source exceeds %d bytes" % MAX_SOURCE_BYTES
        )
    after = source.stat(follow_symlinks=False)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(raw) != after.st_size:
        raise WorkspaceWatchError("workspace shader source changed while being read")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkspaceWatchError(
            "workspace shader source is not valid UTF-8"
        ) from exc
    if "\x00" in text:
        raise WorkspaceWatchError("workspace shader source contains NUL characters")
    return hashlib.sha256(raw).hexdigest()


class _WatchJob:
    def __init__(
        self,
        watch_id: str,
        root: Path,
        source_path: Path,
        debounce_ms: int,
        service: ShaderWorkspaceService,
        initial_hash: str,
        catch_up_reference_hash: str | None,
        catch_up_reference_kind: str | None,
        latest_compiled_hash: str | None,
        latest_candidate_revision: int | None,
        latest_candidate_raw_sha256: str | None,
        audit_path: Path,
        lease: WorkspaceSourceTransactionLease,
        on_finished: Callable[["_WatchJob"], None],
    ):
        self.watch_id = watch_id
        self.root = root
        self.source_path = source_path
        self.debounce_ms = debounce_ms
        self.service = service
        self.audit_path = audit_path
        self._lease = lease
        self._on_finished = on_finished
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="shader-watch-%s" % watch_id[:8],
            daemon=True,
        )
        self._state = "starting"
        self._created_at = _utc_now()
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._initial_hash = initial_hash
        self._observed_hash = initial_hash
        # A pristine workspace establishes the watch baseline and is not
        # compiled merely because watching started.  If a previous candidate
        # exists for different source bytes, however, treating the current
        # file as already attempted would strand a pre-watch editor save
        # forever: no further filesystem change would arrive to wake it.  Seed
        # last-attempted from that stale candidate so the normal debounce loop
        # deterministically catches the current file up.
        self._catch_up_reference_hash = catch_up_reference_hash
        self._catch_up_reference_kind = catch_up_reference_kind
        self._last_attempted_hash = (
            catch_up_reference_hash
            if catch_up_reference_hash and catch_up_reference_hash != initial_hash
            else initial_hash
        )
        self._last_compiled_hash = latest_compiled_hash
        self._latest_candidate_revision = latest_candidate_revision
        self._latest_candidate_raw_sha256 = latest_candidate_raw_sha256
        self._stable_since = time.monotonic()
        self._compile_in_progress = False
        self._pending = False
        self._compile_attempts = 0
        self._compile_successes = 0
        self._compile_failures = 0
        self._transient_retry_hash: str | None = None
        self._transient_retry_count = 0
        self._transient_retry_due = 0.0
        self._last_error: dict[str, Any] | None = None
        self._observation_error_key: tuple[str, str] | None = None
        self._next_sequence = 1
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_AUDIT_EVENTS)
        self._dropped_events = 0

    def start(self) -> None:
        with self._lock:
            catch_up_scheduled = self._last_attempted_hash != self._initial_hash
            self._record_locked(
                "watchCreated",
                sourceSha256=self._initial_hash,
                staleCandidateSourceSha256=(
                    self._last_attempted_hash
                    if catch_up_scheduled
                    and self._catch_up_reference_kind == "latest-compiled"
                    else None
                ),
                catchUpReferenceSourceSha256=(
                    self._last_attempted_hash if catch_up_scheduled else None
                ),
                catchUpReferenceKind=(
                    self._catch_up_reference_kind if catch_up_scheduled else None
                ),
                catchUpCompileScheduled=catch_up_scheduled,
                message=(
                    "current source differs from the last tracked source; a debounced "
                    "catch-up compile is scheduled"
                    if catch_up_scheduled
                    else "initial source hash recorded; no initial compile scheduled"
                ),
            )
        self._thread.start()

    def stop(self) -> dict:
        with self._lock:
            if self._state not in {"stopped", "failed"}:
                self._state = "stopping"
                self._stop.set()
                self._record_locked(
                    "stopRequested",
                    message=(
                        "the active compile, if any, is allowed to finish; no new "
                        "compile will start"
                    ),
                )
        if self._thread is not threading.current_thread():
            self._thread.join(STOP_JOIN_SECONDS)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def is_terminal(self) -> bool:
        with self._lock:
            return self._state in {"stopped", "failed"}

    def _run(self) -> None:
        try:
            with self._lock:
                self._state = "running"
                self._started_at = _utc_now()
                self._record_locked("watchStarted")
            interval = min(0.25, max(0.05, self.debounce_ms / 4000.0))
            while not self._stop.wait(interval):
                self._tick()
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                }
                self._record_locked(
                    "watchFailed",
                    errorType=type(exc).__name__,
                    message=_bounded_text(exc),
                    persist_failure_is_fatal=False,
                )
        finally:
            lease_released = self._lease.release()
            with self._lock:
                if self._state != "failed":
                    self._state = "stopped"
                self._compile_in_progress = False
                self._pending = False
                self._stopped_at = _utc_now()
                self._record_locked(
                    "watchStopped",
                    watchLeaseReleased=lease_released,
                    message=(
                        "cross-process watch lease released"
                        if lease_released
                        else "watch lease token could not be verified; inspect it manually"
                    ),
                    persist_failure_is_fatal=False,
                )
            self._on_finished(self)

    def _tick(self) -> None:
        try:
            current_hash = _source_hash(self.root, self.source_path)
        except Exception as exc:
            with self._lock:
                error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                }
                error_key = (error["type"], error["message"])
                if self._observation_error_key != error_key:
                    self._observation_error_key = error_key
                    self._last_error = error
                    self._record_locked(
                        "observationFailed",
                        errorType=error["type"],
                        message=error["message"],
                    )
            return

        now = time.monotonic()
        target_hash: str | None = None
        with self._lock:
            if self._stop.is_set():
                return
            if self._observation_error_key is not None:
                if self._last_error is not None and (
                    self._last_error.get("type"),
                    self._last_error.get("message"),
                ) == self._observation_error_key:
                    self._last_error = None
                self._observation_error_key = None
                self._record_locked("observationRecovered")
            if current_hash != self._observed_hash:
                previous = self._observed_hash
                self._observed_hash = current_hash
                self._stable_since = now
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._pending = current_hash != self._last_attempted_hash
                self._last_error = None
                self._record_locked(
                    "sourceChanged",
                    previousSha256=previous,
                    sourceSha256=current_hash,
                )
                return
            retrying_same_save = self._transient_retry_hash == current_hash
            self._pending = (
                current_hash != self._last_attempted_hash or retrying_same_save
            )
            if not self._pending:
                return
            if retrying_same_save:
                if now < self._transient_retry_due:
                    return
            elif (now - self._stable_since) * 1000.0 < self.debounce_ms:
                return
            target_hash = current_hash
            self._last_attempted_hash = target_hash
            self._compile_in_progress = True
            self._compile_attempts += 1
            self._record_locked(
                "compileStarted",
                sourceSha256=target_hash,
                attempt=self._compile_attempts,
                transientRetry=(
                    self._transient_retry_count if retrying_same_save else 0
                ),
            )

        try:
            result = self.service.compile(self.root)
            compiled_hash = result.get("sourceSha256")
            revision = result.get("revision")
            candidate_raw_sha256 = result.get("rawSha256")
            if (
                not isinstance(compiled_hash, str)
                or _SHA256.fullmatch(compiled_hash) is None
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid source hash"
                )
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid revision"
                )
            if (
                not isinstance(candidate_raw_sha256, str)
                or _SHA256.fullmatch(candidate_raw_sha256) is None
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid raw SHA-256"
                )
            with self._lock:
                self._compile_successes += 1
                self._last_compiled_hash = compiled_hash
                # The service snapshots the source under its own cross-process
                # operation lock. If an editor saved between our observation
                # and that snapshot, this is the hash that was actually tried.
                self._last_attempted_hash = compiled_hash
                self._latest_candidate_revision = revision
                self._latest_candidate_raw_sha256 = candidate_raw_sha256
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._last_error = None
                self._record_locked(
                    "compileSucceeded",
                    requestedSourceSha256=target_hash,
                    compiledSourceSha256=compiled_hash,
                    revision=self._latest_candidate_revision,
                    candidateRawSha256=candidate_raw_sha256,
                    message=(
                        "candidate remains unapplied and requires replay ABI validation"
                    ),
                )
        except Exception as exc:
            diagnostic = classify_shader_failure(exc, phase="watch-compile")
            diagnostics = _compiler_diagnostics(self.root, self.source_path, exc)
            transient = _is_transient_compile_failure(exc)
            with self._lock:
                self._compile_failures += 1
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                    "diagnostic": diagnostic,
                    "diagnostics": diagnostics,
                }
                self._record_locked(
                    "compileFailed",
                    requestedSourceSha256=target_hash,
                    errorType=type(exc).__name__,
                    message=_bounded_text(exc),
                    diagnostic=diagnostic,
                    diagnostics=diagnostics,
                    transient=transient,
                )
                if (
                    transient
                    and not self._stop.is_set()
                    and self._transient_retry_count
                    < MAX_TRANSIENT_COMPILE_RETRIES
                ):
                    delay = TRANSIENT_COMPILE_RETRY_DELAYS[
                        self._transient_retry_count
                    ]
                    self._transient_retry_count += 1
                    self._transient_retry_hash = target_hash
                    self._transient_retry_due = time.monotonic() + delay
                    self._record_locked(
                        "compileRetryScheduled",
                        sourceSha256=target_hash,
                        retry=self._transient_retry_count,
                        delayMs=int(delay * 1000),
                        reason="transient-windows-file-sharing",
                    )
                else:
                    self._transient_retry_hash = None
                    self._transient_retry_due = 0.0
        finally:
            try:
                after_hash = _source_hash(self.root, self.source_path)
            except Exception:
                after_hash = None
            with self._lock:
                self._compile_in_progress = False
                if after_hash is not None and after_hash != self._observed_hash:
                    previous = self._observed_hash
                    self._observed_hash = after_hash
                    self._stable_since = time.monotonic()
                    self._transient_retry_hash = None
                    self._transient_retry_count = 0
                    self._transient_retry_due = 0.0
                    self._record_locked(
                        "sourceChangedDuringCompile",
                        previousSha256=previous,
                        sourceSha256=after_hash,
                    )
                self._pending = (
                    not self._stop.is_set()
                    and (
                        self._observed_hash != self._last_attempted_hash
                        or self._transient_retry_hash == self._observed_hash
                    )
                )
                self._persist_locked()

    def _record_locked(
        self,
        event_type: str,
        persist_failure_is_fatal: bool = True,
        **details: Any,
    ) -> None:
        if len(self._events) == self._events.maxlen:
            self._dropped_events += 1
        event = {
            "sequence": self._next_sequence,
            "at": _utc_now(),
            "type": event_type,
        }
        self._next_sequence += 1
        event.update(details)
        self._events.append(event)
        try:
            self._persist_locked()
        except Exception as exc:
            if persist_failure_is_fatal:
                raise WorkspaceWatchError(
                    "could not persist watcher audit state: %s" % exc
                ) from exc

    def _persist_locked(self) -> None:
        atomic_write_json(self.audit_path, self._snapshot_locked())

    def _snapshot_locked(self) -> dict:
        caught_up = bool(
            self._last_compiled_hash
            and self._last_compiled_hash == self._observed_hash
            and not self._compile_in_progress
            and not self._pending
        )
        return {
            "schema": WATCH_SCHEMA,
            "schemaVersion": WATCH_VERSION,
            "watchId": self.watch_id,
            "state": self._state,
            "workspacePath": str(self.root),
            "sourcePath": str(self.source_path),
            "auditPath": str(self.audit_path),
            "watchLease": {
                "path": str(self._lease.path),
                "ownedByThisProcess": self._lease.owned,
            },
            "debounceMs": self.debounce_ms,
            "createdAt": self._created_at,
            "startedAt": self._started_at,
            "stoppedAt": self._stopped_at,
            "initialSourceSha256": self._initial_hash,
            "observedSourceSha256": self._observed_hash,
            "lastAttemptedSourceSha256": self._last_attempted_hash,
            "lastCompiledSourceSha256": self._last_compiled_hash,
            "latestCandidateRevision": self._latest_candidate_revision,
            "latestCandidateRawSha256": self._latest_candidate_raw_sha256,
            "compileInProgress": self._compile_in_progress,
            "pending": self._pending,
            "caughtUp": caught_up,
            "compileAttempts": self._compile_attempts,
            "compileSuccesses": self._compile_successes,
            "compileFailures": self._compile_failures,
            "transientRetry": {
                "scheduled": self._transient_retry_hash is not None,
                "sourceSha256": self._transient_retry_hash,
                "retry": self._transient_retry_count,
                "maximumRetries": MAX_TRANSIENT_COMPILE_RETRIES,
            },
            "lastError": self._last_error,
            "droppedAuditEvents": self._dropped_events,
            "events": list(self._events),
            "candidatePolicy": {
                "requiresReplayAbiValidation": True,
                "replayAbiValidated": False,
                "applied": False,
            },
        }


class ShaderWorkspaceWatchManager:
    """Own a bounded number of process-local workspace watcher threads."""

    def __init__(
        self,
        service_factory: Callable[[], ShaderWorkspaceService],
        *,
        owner_instance_id: str | None = None,
    ):
        self._service_factory = service_factory
        self._owner_instance_id = str(owner_instance_id or "").strip().lower()
        if self._owner_instance_id and _WATCH_ID.fullmatch(self._owner_instance_id) is None:
            raise WorkspaceWatchError("watch owner instance ID is invalid")
        self._lock = threading.RLock()
        self._jobs: dict[str, _WatchJob] = {}
        self._active_by_workspace: dict[str, str] = {}

    def start(
        self, workspace_path: str | Path, debounce_ms: int = DEFAULT_DEBOUNCE_MS
    ) -> dict:
        return self._start(workspace_path, debounce_ms, recover_orphaned_lease=False)

    def start_persistent(
        self,
        workspace_path: str | Path,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> dict:
        """Resume a durable session, recovering only a prior host-generation lease."""

        return self._start(
            workspace_path,
            debounce_ms,
            recover_orphaned_lease=bool(self._owner_instance_id),
        )

    def _start(
        self,
        workspace_path: str | Path,
        debounce_ms: int,
        *,
        recover_orphaned_lease: bool,
    ) -> dict:
        debounce = _validate_debounce(debounce_ms)
        service = self._service_factory()
        status_result = service.status(workspace_path)
        root = Path(status_result["workspacePath"]).resolve()
        source_record = status_result.get("source")
        if not isinstance(source_record, dict) or source_record.get("present") is not True:
            raise WorkspaceWatchError(
                "workspace must contain a tracked decompiled source before watching"
            )
        representation = status_result.get("representation")
        if (
            source_record.get("lane") != "executable-editable"
            or not isinstance(representation, dict)
            or representation.get("lane") != "executable-editable"
            or representation.get("compileEligible") is not True
        ):
            raise WorkspaceWatchError(
                "save watching requires the executable-editable shader lane"
            )
        status_source = Path(source_record.get("path", ""))
        source = _fixed_source(root, status_source)
        if status_source.resolve() != source:
            raise WorkspaceWatchError("workspace status returned an unexpected source path")
        initial_hash = _source_hash(root, source)
        latest = status_result.get("latestCompiled")
        latest_hash = latest.get("sourceSha256") if isinstance(latest, dict) else None
        if not isinstance(latest_hash, str) or _SHA256.fullmatch(latest_hash) is None:
            latest_hash = None
        latest_revision = latest.get("revision") if isinstance(latest, dict) else None
        if (
            isinstance(latest_revision, bool)
            or not isinstance(latest_revision, int)
            or latest_revision < 1
        ):
            latest_revision = None
        latest_raw_sha256 = (
            latest.get("rawSha256") if isinstance(latest, dict) else None
        )
        if (
            not isinstance(latest_raw_sha256, str)
            or _SHA256.fullmatch(latest_raw_sha256) is None
        ):
            latest_raw_sha256 = None
        decompiled_hash = source_record.get("decompiledSha256")
        if not isinstance(decompiled_hash, str) or _SHA256.fullmatch(decompiled_hash) is None:
            decompiled_hash = None
        if latest_hash is not None:
            catch_up_reference_hash = latest_hash
            catch_up_reference_kind = "latest-compiled"
        else:
            # The immutable source record is the only evidence that can distinguish a
            # pristine workspace from an edit saved before watching began. Without it,
            # treating the current bytes as a change would also compile every untouched
            # decompile merely because a watcher was opened.
            catch_up_reference_hash = decompiled_hash
            catch_up_reference_kind = (
                "decompiled-source" if decompiled_hash is not None else None
            )
        workspace_key = os.path.normcase(str(root))

        with self._lock:
            self._prune_locked()
            existing_id = self._active_by_workspace.get(workspace_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and not existing.is_terminal():
                    raise WorkspaceWatchError(
                        "workspace already has an active watcher: %s" % existing_id
                    )
                self._active_by_workspace.pop(workspace_key, None)
            active_count = sum(not job.is_terminal() for job in self._jobs.values())
            if active_count >= MAX_ACTIVE_WATCHERS:
                raise WorkspaceWatchError(
                    "active watcher limit reached (%d)" % MAX_ACTIVE_WATCHERS
                )
            watch_dir = root / "watch"
            if watch_dir.is_symlink():
                raise WorkspaceWatchError("workspace watch directory must not be a symlink")
            watch_dir.mkdir(parents=False, exist_ok=True)
            if watch_dir.resolve() != watch_dir or not watch_dir.is_dir():
                raise WorkspaceWatchError("workspace watch directory is redirected")
            watch_id = uuid.uuid4().hex
            audit_path = watch_dir / (watch_id + ".json")
            lease = WorkspaceSourceTransactionLease(
                root,
                owner_instance_id=self._owner_instance_id or None,
                recover_orphaned=recover_orphaned_lease,
            )
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                raise WorkspaceWatchError(
                    "workspace already has a cross-process watch lease; the lease "
                    "is never assumed stale: %s" % lease.path
                ) from exc
            try:
                # The pre-lease status is used only to locate and reserve the
                # workspace. Organization may publish/recover a newer revision
                # before acquisition, so re-resolve every source/candidate
                # field under the shared lease before constructing the job.
                locked_status = service.status(workspace_path)
                locked_root = Path(locked_status["workspacePath"]).resolve()
                locked_source_record = locked_status.get("source")
                locked_representation = locked_status.get("representation")
                if (
                    locked_root != root
                    or not isinstance(locked_source_record, dict)
                    or locked_source_record.get("present") is not True
                    or locked_source_record.get("lane") != "executable-editable"
                    or not isinstance(locked_representation, dict)
                    or locked_representation.get("lane") != "executable-editable"
                    or locked_representation.get("compileEligible") is not True
                ):
                    raise WorkspaceWatchError(
                        "workspace identity/lane changed while acquiring its source lease"
                    )
                locked_status_source = Path(
                    locked_source_record.get("path", "")
                )
                locked_source = _fixed_source(locked_root, locked_status_source)
                if (
                    locked_source != source
                    or locked_status_source.resolve() != locked_source
                ):
                    raise WorkspaceWatchError(
                        "workspace source changed while acquiring its source lease"
                    )
                initial_hash = _source_hash(locked_root, locked_source)
                if locked_source_record.get("currentSha256") != initial_hash:
                    raise WorkspaceWatchError(
                        "workspace source hash changed while watcher start was resolving"
                    )
                latest = locked_status.get("latestCompiled")
                latest_hash = (
                    latest.get("sourceSha256")
                    if isinstance(latest, dict)
                    else None
                )
                if (
                    not isinstance(latest_hash, str)
                    or _SHA256.fullmatch(latest_hash) is None
                ):
                    latest_hash = None
                latest_revision = (
                    latest.get("revision") if isinstance(latest, dict) else None
                )
                if (
                    isinstance(latest_revision, bool)
                    or not isinstance(latest_revision, int)
                    or latest_revision < 1
                ):
                    latest_revision = None
                latest_raw_sha256 = (
                    latest.get("rawSha256") if isinstance(latest, dict) else None
                )
                if (
                    not isinstance(latest_raw_sha256, str)
                    or _SHA256.fullmatch(latest_raw_sha256) is None
                ):
                    latest_raw_sha256 = None
                decompiled_hash = locked_source_record.get("decompiledSha256")
                if (
                    not isinstance(decompiled_hash, str)
                    or _SHA256.fullmatch(decompiled_hash) is None
                ):
                    decompiled_hash = None
                if latest_hash is not None:
                    catch_up_reference_hash = latest_hash
                    catch_up_reference_kind = "latest-compiled"
                else:
                    catch_up_reference_hash = decompiled_hash
                    catch_up_reference_kind = (
                        "decompiled-source"
                        if decompiled_hash is not None
                        else None
                    )
                job = _WatchJob(
                    watch_id,
                    root,
                    source,
                    debounce,
                    service,
                    initial_hash,
                    catch_up_reference_hash,
                    catch_up_reference_kind,
                    latest_hash,
                    latest_revision,
                    latest_raw_sha256,
                    audit_path,
                    lease,
                    self._finished,
                )
                self._jobs[watch_id] = job
                self._active_by_workspace[workspace_key] = watch_id
                job.start()
            except Exception:
                self._jobs.pop(watch_id, None)
                self._active_by_workspace.pop(workspace_key, None)
                lease.release()
                raise
        return job.snapshot()

    def poll(self, watch_id: str) -> dict:
        return self._job(watch_id).snapshot()

    def stop(self, watch_id: str) -> dict:
        return self._job(watch_id).stop()

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.is_terminal():
                job._stop.set()
        deadline = time.monotonic() + STOP_JOIN_SECONDS
        for job in jobs:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            job._thread.join(remaining)

    def _job(self, watch_id: str) -> _WatchJob:
        if not isinstance(watch_id, str) or _WATCH_ID.fullmatch(watch_id) is None:
            raise WorkspaceWatchError("watch_id must be a 32-character lowercase hex ID")
        with self._lock:
            job = self._jobs.get(watch_id)
        if job is None:
            raise WorkspaceWatchError("unknown or expired watch_id")
        return job

    def _finished(self, job: _WatchJob) -> None:
        key = os.path.normcase(str(job.root))
        with self._lock:
            if self._active_by_workspace.get(key) == job.watch_id:
                self._active_by_workspace.pop(key, None)

    def _prune_locked(self) -> None:
        terminal = [
            job for job in self._jobs.values() if job.is_terminal()
        ]
        excess = len(terminal) - MAX_RETAINED_WATCHERS
        if excess <= 0:
            return
        terminal.sort(key=lambda job: job.watch_id)
        for job in terminal[:excess]:
            self._jobs.pop(job.watch_id, None)
