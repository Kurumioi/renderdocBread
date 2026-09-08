"""Controlled high-level services shared by ReplayLab's private runtime.

This module deliberately contains no MCP transport, model/provider integration,
or desktop-app routing. It owns the bounded project-restore and Shader editor
save contracts consumed by ReplayLabInternal.exe. External adapters may reuse
these data-plane helpers, but cannot turn this module into a general tool
surface.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from .active_projects import ActiveReplayProjectService
from .shader_live_edit import ShaderLiveEditManager, ShaderLiveEditSession


ACTIVE_PROJECT_STATE_ENV = "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"
ACTIVE_PROJECTS_HOME_ENV = "RENDERDOC_ULTRA_PROJECTS_HOME"
PROJECT_ASSOCIATION_LEASE_SCHEMA = "replaylab-project-association-lease/v1"
PROJECT_RESTORE_JOB_SCHEMA = "replaylab-project-restore-job/v1"
PROJECT_RESTORE_JOB_VERSION = 1
PROJECT_RESTORE_SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROJECT_RESTORE_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_PROJECT_RESTORE_REQUEST_BYTES = 64 * 1024
MAX_PROJECT_RESTORE_PATH_BYTES = 32 * 1024
MAX_PROJECT_RESTORE_PROGRESS_EVENTS = 512
MAX_PROJECT_RESTORE_PROGRESS_PAGE = 128
MAX_PROJECT_RESTORE_PROGRESS_BYTES = 16 * 1024
MAX_PROJECT_RESTORE_PROGRESS_COUNT = 1_000_000_000
MAX_PROJECT_RESTORE_RESULT_BYTES = 8 * 1024 * 1024
MAX_PROJECT_RESTORE_ERROR_BYTES = 4096
MAX_PROJECT_RESTORE_ACTIVE_JOBS = 8
MAX_PROJECT_RESTORE_HISTORY_JOBS = 32
PROJECT_RESTORE_HISTORY_TTL_SECONDS = 15 * 60.0
# One restore may spend most of this budget in the full-frame RenderDoc export.
# Cancellation/deadline enforcement is cooperative: a single in-flight IPC is
# allowed to return (or hit its own timeout), then the restore checkpoints and
# rolls back all transaction-ledger state.  The desktop client keeps an
# additional cleanup/transport margin beyond this server-owned deadline.
PROJECT_RESTORE_DEADLINE_SECONDS = 30 * 60.0
PROJECT_RESTORE_CLEANUP_GRACE_SECONDS = 5 * 60.0
PROJECT_RESTORE_CANCELLED_ERROR = "ReplayLab project restore was cancelled"
PROJECT_RESTORE_DEADLINE_ERROR = (
    "ReplayLab project restore exceeded its server deadline"
)
SHADER_EDITOR_SAVE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHADER_EDITOR_SAVE_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_SHADER_EDITOR_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES = MAX_SHADER_EDITOR_SOURCE_BYTES + 64 * 1024
MAX_SHADER_EDITOR_CHECK_DIAGNOSTICS = 128
SHADER_LIVE_EDIT_OPERATION_SCHEMA = "replaylab-internal-shader-live-edit/v1"
MAX_SHADER_LIVE_EDIT_OPERATION_BYTES = 64 * 1024
ASSET_INDEX_OPERATION_SCHEMA = "replaylab-internal-asset-index/v1"
MAX_ASSET_INDEX_OPERATION_BYTES = 4 * 1024
_ASSET_INDEX_DEPTHS = frozenset({"names", "headers", "full"})


def parse_asset_index_operation(payload: bytes) -> dict[str, Any]:
    """``ensure`` (start or join the background scan) / ``status``; no paths accepted.

    The active project, its asset root and the managed catalog path are
    resolved by this process from the project state, never from the request.
    """

    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ASSET_INDEX_OPERATION_BYTES:
        raise ValueError("asset index request has an invalid size")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_duplicate_shader_editor_json_object)
    if not isinstance(value, dict) or set(value) != {"schema", "requestId", "operation", "depth"}:
        raise ValueError("asset index request fields are invalid")
    if value["schema"] != ASSET_INDEX_OPERATION_SCHEMA or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(str(value["requestId"])) is None:
        raise ValueError("asset index request identity is invalid")
    if value["operation"] not in {"ensure", "status"}:
        raise ValueError("asset index operation is invalid")
    if not isinstance(value["depth"], str) or value["depth"] not in _ASSET_INDEX_DEPTHS:
        raise ValueError("asset index depth is invalid")
    return value


def parse_shader_live_edit_operation(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SHADER_LIVE_EDIT_OPERATION_BYTES:
        raise ValueError("Shader live-edit request has an invalid size")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_duplicate_shader_editor_json_object)
    if not isinstance(value, dict) or set(value) != {"schema", "requestId", "operation", "parameters", "binding"}:
        raise ValueError("Shader live-edit request fields are invalid")
    if value["schema"] != SHADER_LIVE_EDIT_OPERATION_SCHEMA or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(str(value["requestId"])) is None:
        raise ValueError("Shader live-edit request identity is invalid")
    fields = {"start": {"workspace_path", "debounce_ms"}, "list": set(),
              "poll": {"live_edit_id"}, "stop": {"live_edit_id"},
              "migrate": {"source_workspace_path", "workspace_path", "expected_source_sha256"}}
    parameters = value["parameters"]
    if value["operation"] not in fields or not isinstance(parameters, dict) or set(parameters) != fields[value["operation"]]:
        raise ValueError("Shader live-edit operation or parameters are invalid")
    for key, item in parameters.items():
        if key == "debounce_ms":
            if isinstance(item, bool) or not isinstance(item, int) or not 100 <= item <= 5000:
                raise ValueError("Shader live-edit debounce is invalid")
        elif key.endswith("_path"):
            if not isinstance(item, str) or not item or len(item) > 32768 or "\0" in item or not Path(item).is_absolute():
                raise ValueError("Shader live-edit workspace path is invalid")
        elif key == "live_edit_id":
            if not isinstance(item, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(item) is None:
                raise ValueError("Shader live-edit ID is invalid")
        elif not isinstance(item, str) or SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(item) is None:
            raise ValueError("Shader live-edit source hash is invalid")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {"instanceId", "captureSessionId", "capturePath"}:
        raise ValueError("Shader live-edit capture binding is invalid")
    if (not isinstance(binding["instanceId"], str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", binding["instanceId"]) is None
            or binding["instanceId"] in {".", ".."}
            or not isinstance(binding["captureSessionId"], str)
            or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(binding["captureSessionId"]) is None
            or not isinstance(binding["capturePath"], str)
            or not binding["capturePath"] or "\0" in binding["capturePath"]
            or not Path(binding["capturePath"]).is_absolute()):
        raise ValueError("Shader live-edit capture binding is invalid")
    return value

def active_project_paths(
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Resolve the exact process-owner project roots used by the MCP server."""

    env = os.environ if environment is None else environment
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        replaylab_root = Path(local).expanduser() / "ReplayLab"
    else:
        xdg = str(env.get("XDG_DATA_HOME", "") or "").strip()
        data_root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        replaylab_root = data_root / "ReplayLab"
    configured_state = str(env.get(ACTIVE_PROJECT_STATE_ENV, "") or "").strip()
    configured_home = str(env.get(ACTIVE_PROJECTS_HOME_ENV, "") or "").strip()
    state_path = (
        Path(configured_state).expanduser()
        if configured_state
        else replaylab_root / "config" / "active-project.json"
    )
    projects_home = (
        Path(configured_home).expanduser()
        if configured_home
        else replaylab_root / "projects"
    )
    return state_path.resolve(), projects_home.resolve()


def project_service(environment: Mapping[str, str] | None = None):
    state_path, projects_home = active_project_paths(environment)
    return ActiveReplayProjectService(state_path, projects_home)


def project_association_guard(
    lease_path: str, lease_token: str, expected_capture_path: str
):
    """Return a fail-closed live-capture lease checker for qrenderdoc."""

    token = str(lease_token or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("project association lease token is invalid")
    raw = Path(str(lease_path or "")).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("project association lease path is invalid")
    lease = raw.resolve()
    expected_capture = os.path.normcase(
        str(Path(expected_capture_path).expanduser().resolve())
    )
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    boundary = (
        Path(local).expanduser() / "ReplayLab" / "ipc-v2" / "instances"
        if local else Path(tempfile.gettempdir()) / "ReplayLab" / "ipc-v2" / "instances"
    ).resolve()
    try:
        if os.path.commonpath((str(boundary), str(lease))) != str(boundary):
            raise ValueError("project association lease escapes ReplayLab IPC")
    except ValueError as exc:
        raise ValueError("project association lease escapes ReplayLab IPC") from exc

    def _guard() -> bool:
        if lease.is_symlink() or not lease.is_file():
            return False
        try:
            if lease.stat().st_size > 16 * 1024:
                return False
            value = json.loads(lease.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        try:
            lease_capture = os.path.normcase(
                str(Path(value.get("capturePath", "")).expanduser().resolve())
            )
        except (OSError, TypeError, ValueError):
            return False
        return bool(
            isinstance(value, dict)
            and value.get("schema") == PROJECT_ASSOCIATION_LEASE_SCHEMA
            and hmac.compare_digest(str(value.get("token", "")), token)
            and value.get("active") is True
            and lease_capture == expected_capture
        )

    return _guard

def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _secure_directory(path.parent)
    temporary = path.with_name(
        "%s.tmp.%d.%d" % (path.name, os.getpid(), time.time_ns())
    )
    # Manifests cross qrenderdoc, Python and occasionally legacy Windows
    # launchers.  ASCII JSON escapes remove the last code-page-sensitive wire
    # boundary while json.load reconstructs the exact Unicode paths.
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None

def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a reliable existence probe in a frozen
        # Windows application. In particular, CPython can leave WinError 87 set
        # while returning from the builtin, which surfaces as ``SystemError``
        # under PyInstaller. Query a non-destructive process handle instead.
        try:
            import ctypes

            process_query_limited_information = 0x1000
            synchronize = 0x00100000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(
                process_query_limited_information | synchronize, False, int(pid)
            )
            if not handle:
                # Access denied proves that a process owns the PID even though
                # this user cannot query it. Other failures mean no live owner.
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_uint32(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Owner liveness is a shutdown/lifetime boundary. If the native
            # probe itself is unavailable, do not keep a privileged host alive
            # indefinitely on an unverifiable PID.
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # A process owned by another security principal is still alive; owner
        # files are in this user's private runtime directory, so fail closed.
        return True
    except (OSError, ProcessLookupError):
        return False


class ProjectRestoreRequestError(ValueError):
    """A bounded project-restore HTTP request failed validation."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = int(status_code)


class ProjectRestoreCapacityError(RuntimeError):
    """The bounded restore queue cannot accept another distinct active key."""


@dataclass
class _ProjectRestoreJob:
    job_id: str
    key: tuple[str, str, str]
    project_root: str
    capture_path: str
    capture_session_id: str
    status: str
    created_monotonic: float
    created_unix_ms: int
    updated_unix_ms: int
    latest_sequence: int
    progress: deque[dict[str, Any]]
    cancel_event: threading.Event
    deadline_monotonic: float
    completed_monotonic: float | None = None
    completed_order: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


def _bounded_restore_text(
    value: Any,
    label: str,
    maximum_bytes: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ProjectRestoreRequestError("%s must be a string" % label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProjectRestoreRequestError("%s must be valid UTF-8" % label) from exc
    if len(encoded) > maximum_bytes:
        raise ProjectRestoreRequestError("%s exceeds its byte limit" % label)
    if any(ord(character) < 0x20 for character in value):
        raise ProjectRestoreRequestError("%s contains control characters" % label)
    return value


def _canonical_restore_path(value: Any, label: str, *, directory: bool) -> str:
    text = _bounded_restore_text(value, label, MAX_PROJECT_RESTORE_PATH_BYTES)
    raw = Path(text)
    if not raw.is_absolute():
        raise ProjectRestoreRequestError("%s must be an absolute path" % label)
    if raw.is_symlink():
        raise ProjectRestoreRequestError("%s must not be a symlink" % label)
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRestoreRequestError("%s does not exist" % label) from exc
    lexical = Path(os.path.abspath(str(raw)))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise ProjectRestoreRequestError("%s must not be redirected or aliased" % label)
    if directory:
        if not resolved.is_dir():
            raise ProjectRestoreRequestError("%s must be a directory" % label)
    else:
        if not resolved.is_file():
            raise ProjectRestoreRequestError("%s must be a regular file" % label)
        if resolved.suffix.casefold() != ".rdc":
            raise ProjectRestoreRequestError("capturePath must name an .rdc file")
    return str(resolved)


def _duplicate_restore_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectRestoreRequestError(
                "project restore request contains duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def _reject_restore_json_constant(value: str) -> None:
    raise ProjectRestoreRequestError(
        "project restore request contains non-finite JSON: %s" % value
    )


def parse_project_restore_request(payload: bytes | bytearray | memoryview) -> dict[str, str]:
    """Parse the exact, bounded POST body and bind it to local physical paths."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProjectRestoreRequestError("project restore body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ProjectRestoreRequestError("project restore body is empty")
    if len(raw) > MAX_PROJECT_RESTORE_REQUEST_BYTES:
        raise ProjectRestoreRequestError(
            "project restore body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_restore_json_object,
            parse_constant=_reject_restore_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise ProjectRestoreRequestError(
            "project restore body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProjectRestoreRequestError("project restore body is invalid JSON") from exc
    expected = {"projectRoot", "capturePath", "captureSessionId"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ProjectRestoreRequestError("project restore fields are invalid")
    session_id = value["captureSessionId"]
    if (
        not isinstance(session_id, str)
        or PROJECT_RESTORE_SESSION_PATTERN.fullmatch(session_id) is None
    ):
        raise ProjectRestoreRequestError(
            "captureSessionId must be 32 lowercase hexadecimal characters"
        )
    return {
        "projectRoot": _canonical_restore_path(
            value["projectRoot"], "projectRoot", directory=True
        ),
        "capturePath": _canonical_restore_path(
            value["capturePath"], "capturePath", directory=False
        ),
        "captureSessionId": session_id,
    }


async def _read_project_restore_body(request: Any) -> bytes:
    """Read a Starlette-like request without buffering an unbounded body."""

    headers = getattr(request, "headers", {})
    supplied_length = headers.get("content-length") if hasattr(headers, "get") else None
    if supplied_length not in (None, ""):
        try:
            declared = int(supplied_length)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProjectRestoreRequestError("content-length is invalid") from exc
        if declared < 0:
            raise ProjectRestoreRequestError("content-length is invalid")
        if declared > MAX_PROJECT_RESTORE_REQUEST_BYTES:
            raise ProjectRestoreRequestError(
                "project restore body exceeds its byte limit", status_code=413
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > MAX_PROJECT_RESTORE_REQUEST_BYTES:
            raise ProjectRestoreRequestError(
                "project restore body exceeds its byte limit", status_code=413
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _bounded_restore_json_object(
    value: Any, label: str, maximum_bytes: int
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be an object" % label)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("%s must be finite JSON" % label) from exc
    if len(encoded) > maximum_bytes:
        raise ValueError("%s exceeds its byte limit" % label)
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("%s must be an object" % label)
    return decoded


def _bounded_restore_error(error: BaseException) -> str:
    text = "%s: %s" % (type(error).__name__, error)
    text = "".join(character if ord(character) >= 0x20 else " " for character in text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_PROJECT_RESTORE_ERROR_BYTES:
        return encoded.decode("utf-8")
    suffix = b"..."
    clipped = encoded[: MAX_PROJECT_RESTORE_ERROR_BYTES - len(suffix)]
    return clipped.decode("utf-8", errors="ignore") + suffix.decode("ascii")


def _normalise_restore_progress(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("restore progress must be an object")
    phase = _bounded_restore_text(value.get("phase"), "progress.phase", 128)
    message = _bounded_restore_text(
        value.get("message"), "progress.message", 1024, allow_empty=True
    )
    item = _bounded_restore_text(
        value.get("item", ""), "progress.item", 1024, allow_empty=True
    )
    current = value.get("current")
    total = value.get("total")
    for count, label in ((current, "progress.current"), (total, "progress.total")):
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > MAX_PROJECT_RESTORE_PROGRESS_COUNT
        ):
            raise ValueError("%s must be a bounded non-negative integer" % label)
    clean = {
        "phase": phase,
        "message": message,
        "item": item,
        "current": current,
        "total": total,
    }
    _bounded_restore_json_object(clean, "restore progress", MAX_PROJECT_RESTORE_PROGRESS_BYTES)
    return clean


def _run_project_restore(
    project_root: str,
    capture_path: str,
    capture_session_id: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Import the restore implementation only inside the background worker."""

    from .project_restore import restore_active_capture_project

    return restore_active_capture_project(
        project_root=project_root,
        capture_path=capture_path,
        capture_session_id=capture_session_id,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        deadline_monotonic=deadline_monotonic,
    )


def _runner_accepts_restore_control(runner: Callable[..., Any]) -> bool:
    """Return whether a custom runner accepts the v2 cooperative controls.

    Existing tests and embedders may supply the historical callback-only
    runner.  Inspecting once avoids catching a TypeError raised *inside* the
    runner and accidentally retrying physical work.
    """

    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    return all(
        name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for name in ("cancel_event", "deadline_monotonic")
    )


class ProjectRestoreJobRegistry:
    """Thread-safe, bounded single-worker registry for project restore jobs."""

    _ACTIVE_STATUSES = frozenset(("queued", "running"))
    _TERMINAL_STATUSES = frozenset(("completed", "failed"))

    def __init__(
        self,
        *,
        runner: Callable[..., dict[str, Any]] | None = None,
        max_active_jobs: int = MAX_PROJECT_RESTORE_ACTIVE_JOBS,
        max_history_jobs: int = MAX_PROJECT_RESTORE_HISTORY_JOBS,
        history_ttl_seconds: float = PROJECT_RESTORE_HISTORY_TTL_SECONDS,
        max_progress_events: int = MAX_PROJECT_RESTORE_PROGRESS_EVENTS,
        progress_page_size: int = MAX_PROJECT_RESTORE_PROGRESS_PAGE,
        deadline_seconds: float = PROJECT_RESTORE_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        unix_time: Callable[[], float] = time.time,
    ):
        if (
            isinstance(max_active_jobs, bool)
            or int(max_active_jobs) <= 0
            or isinstance(max_history_jobs, bool)
            or int(max_history_jobs) <= 0
            or float(history_ttl_seconds) <= 0
            or isinstance(max_progress_events, bool)
            or int(max_progress_events) <= 0
            or isinstance(progress_page_size, bool)
            or int(progress_page_size) <= 0
            or isinstance(deadline_seconds, bool)
            or not math.isfinite(float(deadline_seconds))
            or float(deadline_seconds) <= 0
        ):
            raise ValueError("project restore registry bounds must be positive")
        self._runner = runner or _run_project_restore
        self._max_active_jobs = int(max_active_jobs)
        self._max_history_jobs = int(max_history_jobs)
        self._history_ttl_seconds = float(history_ttl_seconds)
        self._max_progress_events = int(max_progress_events)
        self._progress_page_size = min(
            int(progress_page_size), self._max_progress_events
        )
        self._deadline_seconds = float(deadline_seconds)
        self._monotonic = monotonic
        self._unix_time = unix_time
        self._lock = threading.Lock()
        self._jobs: dict[str, _ProjectRestoreJob] = {}
        self._active_by_key: dict[tuple[str, str, str], str] = {}
        self._next_completed_order = 0
        self._closed = False
        self._runner_accepts_control = (
            runner is None or _runner_accepts_restore_control(self._runner)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="replaylab-project-restore"
        )

    def _unix_ms(self) -> int:
        return int(self._unix_time() * 1000)

    def _cleanup_locked(self, now: float) -> None:
        terminal = [
            job
            for job in self._jobs.values()
            if job.status in self._TERMINAL_STATUSES
            and job.completed_monotonic is not None
        ]
        expired = {
            job.job_id
            for job in terminal
            if now - float(job.completed_monotonic) >= self._history_ttl_seconds
        }
        remaining = sorted(
            (job for job in terminal if job.job_id not in expired),
            key=lambda job: int(job.completed_order or 0),
        )
        excess = max(0, len(remaining) - self._max_history_jobs)
        expired.update(job.job_id for job in remaining[:excess])
        for job_id in expired:
            self._jobs.pop(job_id, None)

    def _fail_locked(
        self, job: _ProjectRestoreJob, error: str, now: float
    ) -> None:
        if job.status in self._TERMINAL_STATUSES:
            return
        job.status = "failed"
        job.error = str(error or "project restore failed")[
            :MAX_PROJECT_RESTORE_ERROR_BYTES
        ]
        job.completed_monotonic = now
        self._next_completed_order += 1
        job.completed_order = self._next_completed_order
        job.updated_unix_ms = self._unix_ms()
        if self._active_by_key.get(job.key) == job.job_id:
            self._active_by_key.pop(job.key, None)

    def _expire_queued_locked(self, now: float) -> None:
        for job in list(self._jobs.values()):
            if job.status == "queued" and now >= job.deadline_monotonic:
                job.cancel_event.set()
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)

    def _snapshot_locked(self, job: _ProjectRestoreJob, after: int) -> dict[str, Any]:
        progress = [
            copy.deepcopy(event)
            for event in job.progress
            if event["sequence"] > after
        ][: self._progress_page_size]
        oldest = job.progress[0]["sequence"] if job.progress else job.latest_sequence + 1
        value: dict[str, Any] = {
            "schema": PROJECT_RESTORE_JOB_SCHEMA,
            "schemaVersion": PROJECT_RESTORE_JOB_VERSION,
            "jobId": job.job_id,
            "status": job.status,
            "latestSequence": job.latest_sequence,
            "progress": progress,
            "progressTruncated": bool(job.progress and after < oldest - 1),
        }
        if job.status == "completed":
            value["result"] = copy.deepcopy(job.result or {})
        elif job.status == "failed":
            value["error"] = str(job.error or "project restore failed")
        return value

    def submit(self, request: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
        expected = {"projectRoot", "capturePath", "captureSessionId"}
        if not isinstance(request, Mapping) or set(request) != expected:
            raise ProjectRestoreRequestError("project restore fields are invalid")
        project_root = str(request["projectRoot"])
        capture_path = str(request["capturePath"])
        capture_session_id = str(request["captureSessionId"])
        key = (
            os.path.normcase(project_root),
            os.path.normcase(capture_path),
            capture_session_id,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("project restore registry is closed")
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            active_id = self._active_by_key.get(key)
            active = self._jobs.get(active_id) if active_id is not None else None
            if active is not None and active.status in self._ACTIVE_STATUSES:
                return self._snapshot_locked(active, 0), True
            active_count = sum(
                job.status in self._ACTIVE_STATUSES for job in self._jobs.values()
            )
            if active_count >= self._max_active_jobs:
                raise ProjectRestoreCapacityError("project restore queue is full")
            job_id = secrets.token_hex(16)
            while job_id in self._jobs:
                job_id = secrets.token_hex(16)
            unix_ms = self._unix_ms()
            job = _ProjectRestoreJob(
                job_id=job_id,
                key=key,
                project_root=project_root,
                capture_path=capture_path,
                capture_session_id=capture_session_id,
                status="queued",
                created_monotonic=now,
                created_unix_ms=unix_ms,
                updated_unix_ms=unix_ms,
                latest_sequence=0,
                progress=deque(maxlen=self._max_progress_events),
                cancel_event=threading.Event(),
                deadline_monotonic=now + self._deadline_seconds,
            )
            self._jobs[job_id] = job
            self._active_by_key[key] = job_id
            try:
                self._executor.submit(self._execute, job_id)
            except BaseException:
                self._active_by_key.pop(key, None)
                self._jobs.pop(job_id, None)
                raise
            return self._snapshot_locked(job, 0), False

    def _append_progress(self, job_id: str, value: Any) -> None:
        try:
            clean = _normalise_restore_progress(value)
        except (TypeError, ValueError, ProjectRestoreRequestError):
            clean = {
                "phase": "progress-warning",
                "message": "Ignored malformed project restore progress.",
                "item": "",
                "current": 0,
                "total": 0,
            }
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in self._ACTIVE_STATUSES:
                return
            if job.latest_sequence >= 0x7FFFFFFF:
                return
            if job.progress and all(
                job.progress[-1].get(field) == clean.get(field)
                for field in ("phase", "message", "item", "current", "total")
            ):
                return
            job.latest_sequence += 1
            event = {"sequence": job.latest_sequence, **clean}
            job.progress.append(event)
            job.updated_unix_ms = self._unix_ms()

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                return
            now = self._monotonic()
            if job.cancel_event.is_set():
                self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                self._cleanup_locked(now)
                return
            if now >= job.deadline_monotonic:
                job.cancel_event.set()
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                self._cleanup_locked(now)
                return
            job.status = "running"
            job.updated_unix_ms = self._unix_ms()
            inputs = (job.project_root, job.capture_path, job.capture_session_id)
            cancel_event = job.cancel_event
            deadline_monotonic = job.deadline_monotonic
        try:
            runner_kwargs: dict[str, Any] = {
                "progress_callback": lambda event: self._append_progress(
                    job_id, event
                )
            }
            if self._runner_accepts_control:
                runner_kwargs.update(
                    {
                        "cancel_event": cancel_event,
                        "deadline_monotonic": deadline_monotonic,
                    }
                )
            value = self._runner(inputs[0], inputs[1], inputs[2], **runner_kwargs)
            result = _bounded_restore_json_object(
                value, "project restore result", MAX_PROJECT_RESTORE_RESULT_BYTES
            )
        except BaseException as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                now = self._monotonic()
                exception_name = type(exc).__name__
                if exception_name == "RestoreCancelled":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                elif exception_name == "RestoreDeadlineExceeded":
                    self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                else:
                    # In particular, never hide RestoreRollbackFailed merely
                    # because its triggering cancellation Event remains set:
                    # that diagnostic means live mutation may still exist.
                    self._fail_locked(job, _bounded_restore_error(exc), now)
                self._cleanup_locked(now)
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            now = self._monotonic()
            # ``completed: true`` is the restore transaction's commit marker:
            # its final checkpoint ran while the project pin and rollback
            # ledgers were still owned, then the pin was released.  A DELETE
            # racing after that return must not relabel committed live state as
            # failed, because no worker remains that could roll it back.
            committed = result.get("completed") is True
            if not committed and job.cancel_event.is_set():
                self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                self._cleanup_locked(now)
                return
            if not committed and now >= job.deadline_monotonic:
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                self._cleanup_locked(now)
                return
            job.status = "completed"
            job.result = result
            job.completed_monotonic = now
            self._next_completed_order += 1
            job.completed_order = self._next_completed_order
            job.updated_unix_ms = self._unix_ms()
            if self._active_by_key.get(job.key) == job_id:
                self._active_by_key.pop(job.key, None)
            self._cleanup_locked(job.completed_monotonic)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        if (
            not isinstance(job_id, str)
            or PROJECT_RESTORE_JOB_ID_PATTERN.fullmatch(job_id) is None
        ):
            raise ProjectRestoreRequestError("project restore job id is invalid")
        with self._lock:
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in self._ACTIVE_STATUSES:
                job.cancel_event.set()
                job.updated_unix_ms = self._unix_ms()
                if job.status == "queued":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
            return self._snapshot_locked(job, 0)

    def get(self, job_id: str, *, after: int = 0) -> dict[str, Any] | None:
        if (
            not isinstance(job_id, str)
            or PROJECT_RESTORE_JOB_ID_PATTERN.fullmatch(job_id) is None
        ):
            raise ProjectRestoreRequestError("project restore job id is invalid")
        if (
            isinstance(after, bool)
            or not isinstance(after, int)
            or after < 0
            or after > (1 << 63) - 1
        ):
            raise ProjectRestoreRequestError("after must be a non-negative integer")
        with self._lock:
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return self._snapshot_locked(job, after)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = self._monotonic()
            for job in list(self._jobs.values()):
                if job.status not in self._ACTIVE_STATUSES:
                    continue
                job.cancel_event.set()
                if job.status == "queued":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
        self._executor.shutdown(wait=wait, cancel_futures=True)


def _parse_project_restore_after(value: Any) -> int:
    token = str(value if value is not None else "0")
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", token):
        raise ProjectRestoreRequestError("after must be a non-negative integer")
    result = int(token)
    if result > (1 << 63) - 1:
        raise ProjectRestoreRequestError("after must be a bounded non-negative integer")
    return result


async def project_restore_post_response(
    request: Any, registry: ProjectRestoreJobRegistry
) -> tuple[int, dict[str, Any]]:
    """Purely testable POST endpoint adapter returning ``(status, JSON)``."""

    try:
        value = parse_project_restore_request(await _read_project_restore_body(request))
        snapshot, deduplicated = registry.submit(value)
        return (200 if deduplicated else 202), snapshot
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except ProjectRestoreCapacityError as exc:
        return 429, {"error": str(exc)[:2048]}
    except RuntimeError as exc:
        return 503, {"error": str(exc)[:2048]}


def project_restore_get_response(
    job_id: Any,
    after: Any,
    registry: ProjectRestoreJobRegistry,
) -> tuple[int, dict[str, Any]]:
    """Purely testable GET endpoint adapter returning ``(status, JSON)``."""

    try:
        snapshot = registry.get(str(job_id), after=_parse_project_restore_after(after))
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    if snapshot is None:
        return 404, {"error": "project restore job was not found"}
    return 200, snapshot


def project_restore_delete_response(
    job_id: Any, registry: ProjectRestoreJobRegistry
) -> tuple[int, dict[str, Any]]:
    """Request cooperative cancellation and return the bounded job snapshot."""

    try:
        snapshot = registry.cancel(str(job_id))
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    if snapshot is None:
        return 404, {"error": "project restore job was not found"}
    # Queued cancellation is terminal immediately. A running job acknowledges
    # the request and transitions to failed at its next safe checkpoint.
    return (202 if snapshot.get("status") == "running" else 200), snapshot


class ShaderEditorSaveRequestError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _duplicate_shader_editor_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShaderEditorSaveRequestError(
                "shader editor save contains a duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def parse_shader_editor_save_request(
    payload: bytes | bytearray | memoryview,
) -> dict[str, str]:
    """Parse one exact, bounded save request without accepting path authority."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ShaderEditorSaveRequestError("shader editor save body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ShaderEditorSaveRequestError("shader editor save body is empty")
    if len(raw) > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor save body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_shader_editor_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ShaderEditorSaveRequestError(
                    "shader editor save contains non-finite JSON: %s" % token
                )
            ),
        )
    except UnicodeDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor save body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor save body is invalid JSON"
        ) from exc
    expected_fields = {
        "liveEditId", "authorizationId", "workspaceId",
        "expectedRawSha256", "text",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ShaderEditorSaveRequestError("shader editor save fields are invalid")
    for field in ("liveEditId", "authorizationId", "workspaceId"):
        token = value.get(field)
        if (not isinstance(token, str) or
                SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(token) is None):
            raise ShaderEditorSaveRequestError(
                "%s must be 32 lowercase hexadecimal characters" % field
            )
    expected_sha256 = value.get("expectedRawSha256")
    if (not isinstance(expected_sha256, str) or
            SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(expected_sha256) is None):
        raise ShaderEditorSaveRequestError(
            "expectedRawSha256 must be 64 lowercase hexadecimal characters"
        )
    text = value.get("text")
    if not isinstance(text, str) or not text:
        raise ShaderEditorSaveRequestError("shader editor source must not be empty")
    if "\x00" in text:
        raise ShaderEditorSaveRequestError(
            "shader editor source contains NUL characters"
        )
    try:
        source_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor source is not valid UTF-8"
        ) from exc
    if source_size > MAX_SHADER_EDITOR_SOURCE_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor source exceeds its byte limit", status_code=413
        )
    return value


def parse_shader_editor_check_request(
    payload: bytes | bytearray | memoryview,
) -> dict[str, str]:
    """Parse one bounded compile-check request (unsaved-buffer diagnostics).

    A check is read-only: it needs the live/authorization/workspace identity
    and the buffer text, but no ``expectedRawSha256`` because it never writes
    the source and so has no on-disk revision to guard against.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ShaderEditorSaveRequestError("shader editor check body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ShaderEditorSaveRequestError("shader editor check body is empty")
    if len(raw) > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor check body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_shader_editor_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ShaderEditorSaveRequestError(
                    "shader editor check contains non-finite JSON: %s" % token
                )
            ),
        )
    except UnicodeDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor check body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor check body is invalid JSON"
        ) from exc
    expected_fields = {"liveEditId", "authorizationId", "workspaceId", "text"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ShaderEditorSaveRequestError("shader editor check fields are invalid")
    for field in ("liveEditId", "authorizationId", "workspaceId"):
        token = value.get(field)
        if (not isinstance(token, str) or
                SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(token) is None):
            raise ShaderEditorSaveRequestError(
                "%s must be 32 lowercase hexadecimal characters" % field
            )
    text = value.get("text")
    if not isinstance(text, str) or not text:
        raise ShaderEditorSaveRequestError("shader editor source must not be empty")
    if "\x00" in text:
        raise ShaderEditorSaveRequestError(
            "shader editor source contains NUL characters"
        )
    try:
        source_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor source is not valid UTF-8"
        ) from exc
    if source_size > MAX_SHADER_EDITOR_SOURCE_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor source exceeds its byte limit", status_code=413
        )
    return value


async def _read_shader_editor_save_body(request: Any) -> bytes:
    headers = getattr(request, "headers", {})
    supplied_length = headers.get("content-length") if hasattr(headers, "get") else None
    if supplied_length not in (None, ""):
        try:
            declared = int(supplied_length)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ShaderEditorSaveRequestError("content-length is invalid") from exc
        if declared < 0:
            raise ShaderEditorSaveRequestError("content-length is invalid")
        if declared > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
            raise ShaderEditorSaveRequestError(
                "shader editor save body exceeds its byte limit", status_code=413
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        value = chunk if isinstance(chunk, bytes) else bytes(chunk)
        total += len(value)
        if total > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
            raise ShaderEditorSaveRequestError(
                "shader editor save body exceeds its byte limit", status_code=413
            )
        chunks.append(value)
    return b"".join(chunks)


def shader_editor_save_response(
    payload: bytes | bytearray | memoryview, manager: Any
) -> tuple[int, dict[str, Any]]:
    """Execute a save only through an in-memory Host-owned live-edit job."""

    try:
        value = parse_shader_editor_save_request(payload)
        result = manager.save_authorized_source(
            value["liveEditId"],
            value["authorizationId"],
            value["workspaceId"],
            value["expectedRawSha256"],
            value["text"],
        )
        if not isinstance(result, dict):
            raise RuntimeError("shader editor save returned a non-object result")
        watch_id = str(result.get("watchId", "") or "")
        saved_sha256 = str(result.get("rawSha256", "") or "")
        changed = result.get("changed")
        compile_scheduled = result.get("compileScheduled")
        size = result.get("size")
        modified_ns = result.get("modifiedNs")
        if (
            SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(watch_id) is None
            or SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(saved_sha256) is None
            or not isinstance(changed, bool)
            or not isinstance(compile_scheduled, bool)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_SHADER_EDITOR_SOURCE_BYTES
            or isinstance(modified_ns, bool)
            or not isinstance(modified_ns, int)
            or modified_ns <= 0
        ):
            raise RuntimeError("shader editor save returned an invalid result")
        return 200, {
            "schema": "replaylab-shader-source-save/v1",
            "schemaVersion": 1,
            "liveEditId": value["liveEditId"],
            "authorizationId": value["authorizationId"],
            "workspaceId": value["workspaceId"],
            "watchId": watch_id,
            "savedRawSha256": saved_sha256,
            "size": size,
            "modifiedNs": modified_ns,
            "changed": changed,
            "compileScheduled": compile_scheduled,
        }
    except ShaderEditorSaveRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except Exception as exc:
        from .shader_workspace.errors import WorkspaceWatchConflictError

        if isinstance(exc, WorkspaceWatchConflictError):
            return 409, {"error": str(exc)[:2048], "conflict": True}
        # Unknown/expired IDs, an inactive job, or a stopped watcher are all
        # safe client conflicts. Internal persistence/I/O faults remain 503 so
        # the editor does not claim that the source was published.
        if type(exc).__name__ in {"ShaderLiveEditError", "WorkspaceWatchError"}:
            return 409, {"error": str(exc)[:2048]}
        return 503, {"error": ("%s: %s" % (type(exc).__name__, exc))[:2048]}


def shader_editor_check_response(
    payload: bytes | bytearray | memoryview, manager: Any
) -> tuple[int, dict[str, Any]]:
    """Compile an unsaved buffer through the Host-owned live-edit job only.

    Read-only counterpart to :func:`shader_editor_save_response`: it returns
    compiler diagnostics for the current buffer without writing, publishing,
    or applying anything. An ``ok`` of ``None`` means the buffer could not be
    checked live (e.g. a route-plan workspace or an unavailable compiler); the
    editor then keeps whatever it already shows rather than inventing errors.
    """

    try:
        value = parse_shader_editor_check_request(payload)
        result = manager.check_authorized_source(
            value["liveEditId"],
            value["authorizationId"],
            value["workspaceId"],
            value["text"],
        )
        if not isinstance(result, dict):
            raise RuntimeError("shader editor check returned a non-object result")
        ok = result.get("ok")
        if ok is not None and not isinstance(ok, bool):
            raise RuntimeError("shader editor check returned an invalid ok flag")
        raw_diagnostics = result.get("diagnostics") or []
        if not isinstance(raw_diagnostics, list):
            raise RuntimeError("shader editor check returned invalid diagnostics")
        diagnostics: list[dict[str, Any]] = []
        for item in raw_diagnostics[:MAX_SHADER_EDITOR_CHECK_DIAGNOSTICS]:
            if not isinstance(item, dict):
                continue
            try:
                line = int(item.get("line", 0) or 0)
            except (TypeError, ValueError):
                line = 0
            try:
                column = int(item.get("column", 0) or 0)
            except (TypeError, ValueError):
                column = 0
            severity = str(item.get("severity", "error") or "error")
            if severity not in ("error", "warning", "information", "hint"):
                severity = "error"
            message = str(item.get("message", "") or "")[:4096]
            diagnostics.append({
                "line": max(0, line),
                "column": max(0, column),
                "severity": severity,
                "message": message,
            })
        return 200, {
            "schema": "replaylab-shader-source-check/v1",
            "schemaVersion": 1,
            "liveEditId": value["liveEditId"],
            "authorizationId": value["authorizationId"],
            "workspaceId": value["workspaceId"],
            "ok": ok,
            "diagnostics": diagnostics,
            "reason": str(result.get("reason") or "")[:512],
        }
    except ShaderEditorSaveRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except Exception as exc:
        # A check never publishes, so every failure is a safe client-side
        # conflict or an internal fault; neither must be shown as a code error.
        if type(exc).__name__ in {"ShaderLiveEditError", "WorkspaceWatchError"}:
            return 409, {"error": str(exc)[:2048]}
        return 503, {"error": ("%s: %s" % (type(exc).__name__, exc))[:2048]}


_SHADER_LIVE_EDIT_MANAGER: ShaderLiveEditManager | None = None
_SHADER_LIVE_EDIT_MANAGER_GUARD = threading.Lock()
_SHADER_LIVE_EDIT_BINDINGS: dict[str, ShaderLiveEditSession] = {}
_SHADER_LIVE_EDIT_BINDING_GUARD = threading.Lock()


def _shader_capture_mailbox(directory: Path) -> dict[str, Any] | None:
    path = directory / "instance.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def _shader_capture_session_state(binding: dict[str, str], pid: int) -> str:
    """Distinguish a closed capture from an unavailable but possibly live owner.

    A stalled UI heartbeat is not evidence that another GUI may take over.
    Physical process/session facts are read locally; no startup UI RPC occurs.
    """
    from .ipc_client import INSTANCE_ROOT, IPCClient

    if not IPCClient._process_alive(pid):
        return "expired"
    try:
        value = _shader_capture_mailbox(Path(INSTANCE_ROOT) / binding["instanceId"])
        if (not isinstance(value, dict) or value.get("pid") != pid
                or value.get("instanceId") != binding["instanceId"]):
            return "unavailable"
        if value.get("stopped") is True or value.get("captureLoaded") is False:
            return "expired"
        session = value.get("captureSessionId")
        capture_path = value.get("capturePath")
        if (value.get("captureLoaded") is not True or not isinstance(session, str)
                or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                or not isinstance(capture_path, str) or not capture_path):
            return "unavailable"
        if (session != binding["captureSessionId"]
                or os.path.normcase(str(Path(capture_path).resolve()))
                != os.path.normcase(str(Path(binding["capturePath"]).resolve()))):
            return "expired"
        return "current"
    except (OSError, TypeError, ValueError):
        return "unavailable"


def _freeze_shader_live_session(instance: str, session: str, pid: int,
                                capture_path: str) -> ShaderLiveEditSession:
    from .ipc_client import bind_direct_tool_call

    binding = {"instanceId": instance, "captureSessionId": session,
               "capturePath": capture_path}

    def state() -> str:
        return _shader_capture_session_state(binding, pid)

    def apply_candidate(workspace_path: str, revision: int, candidate_sha256: str) -> dict[str, Any]:
        from . import server

        if state() != "current":
            raise RuntimeError("Shader live-edit capture session is no longer current")
        # This closure belongs to one job. A later job for the same workspace
        # cannot retarget an old in-flight worker or inherit its ABI lease.
        with bind_direct_tool_call(instance, session):
            return server._shader_live_edit_apply_candidate(workspace_path, revision, candidate_sha256)

    return ShaderLiveEditSession(binding, state, apply_candidate)


def _bound_shader_workspace_identity(workspace_path: str) -> dict[str, Any]:
    from . import server
    from .ipc_client import _CALL_BINDING, bind_direct_tool_call

    status = server._shader_workspace_service().status(workspace_path)
    identity = server._shader_live_edit_workspace_identity(workspace_path, status=status)
    from .ipc_client import INSTANCE_ROOT, IPCClient

    # Discovery only chooses a unique live mailbox. A new job must then prove
    # the workspace's exact catalog before it publishes any watcher or editor
    # authority. Automatic resume runs on the healthy Host's owner worker, not
    # its startup/heartbeat thread, so that preflight may safely wait for Qt.
    direct = _CALL_BINDING.get()
    directories = ([Path(INSTANCE_ROOT) / direct[0]] if direct is not None
                   else list(Path(INSTANCE_ROOT).glob("*"))[:1024])
    matches = []
    expected_path = os.path.normcase(str(Path(identity["capturePath"]).resolve()))
    for directory in directories:
        manifest = IPCClient._manifest(str(directory))
        if not manifest or manifest.get("captureLoaded") is not True:
            continue
        session = manifest.get("captureSessionId", "")
        if (not isinstance(session, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                or (direct is not None and session != direct[1])):
            continue
        if os.path.normcase(str(Path(manifest.get("capturePath", "")).resolve())) == expected_path:
            matches.append((directory.name, session, manifest.get("pid")))
    if len(matches) != 1:
        raise RuntimeError("Shader live-edit requires one matching live qrenderdoc capture")
    instance, session, pid = matches[0]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Shader live-edit mailbox has an invalid process identity")
    frozen = _freeze_shader_live_session(instance, session, pid, str(identity["capturePath"]))
    if frozen.state() != "current":
        raise RuntimeError("Shader live-edit capture changed while binding its job")
    with bind_direct_tool_call(instance, session):
        context = server._workspace_active_capture_context(workspace_path, status=status)
    capture = context.get("capture") if isinstance(context, dict) else None
    if (
        not isinstance(capture, dict)
        or capture.get("captureId") != identity["captureId"]
        or capture.get("captureSessionId") != session
        or not isinstance(capture.get("path"), str)
        or os.path.normcase(str(Path(capture["path"]).resolve())) != expected_path
        or frozen.state() != "current"
    ):
        raise RuntimeError("Shader live-edit capture changed during catalog identity validation")
    with _SHADER_LIVE_EDIT_BINDING_GUARD:
        _SHADER_LIVE_EDIT_BINDINGS[os.path.normcase(str(Path(workspace_path).resolve()))] = frozen
    return identity


def _bound_shader_session(workspace_path: str) -> ShaderLiveEditSession:
    with _SHADER_LIVE_EDIT_BINDING_GUARD:
        binding = _SHADER_LIVE_EDIT_BINDINGS.get(os.path.normcase(str(Path(workspace_path).resolve())))
    if binding is None:
        raise RuntimeError("Shader live-edit has no bound qrenderdoc capture session")
    return binding


def shader_live_edit_manager() -> ShaderLiveEditManager:
    """Return the one bounded internal Shader live-edit manager.

    The neutral service owns manager lifetime.  The implementation functions
    are imported explicitly from the transport-free high-level tool module;
    server.py defers all FastMCP imports/construction until an external
    transport calls run() or streamable_http_app().
    """

    global _SHADER_LIVE_EDIT_MANAGER
    with _SHADER_LIVE_EDIT_MANAGER_GUARD:
        if _SHADER_LIVE_EDIT_MANAGER is None:
            from .server import (
                _SHADER_LIVE_EDIT_REGISTRY_PATH,
                _shader_live_edit_workspace_migration_evidence,
                _shader_workspace_watch_manager,
                _shader_live_edit_apply_candidate,
            )

            _SHADER_LIVE_EDIT_MANAGER = ShaderLiveEditManager(
                _shader_workspace_watch_manager(),
                _shader_live_edit_apply_candidate,
                registry_path=_SHADER_LIVE_EDIT_REGISTRY_PATH,
                workspace_identity=_bound_shader_workspace_identity,
                bind_session=_bound_shader_session,
                workspace_migration_evidence=(
                    _shader_live_edit_workspace_migration_evidence
                ),
            )
        return _SHADER_LIVE_EDIT_MANAGER


def shutdown_shader_live_edits() -> None:
    with _SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = _SHADER_LIVE_EDIT_MANAGER
    if manager is not None:
        manager.shutdown()


class InternalToolService:
    """Closed process-owned facade for qrenderdoc's high-level operations."""

    def __init__(
        self,
        *,
        project: Any | None = None,
        restore_jobs: ProjectRestoreJobRegistry | None = None,
        shader_manager: Any | None = None,
    ):
        self.project = project or project_service()
        self.restore_jobs = restore_jobs or ProjectRestoreJobRegistry()
        self.shader_manager = shader_manager
        self._shader_refresh_guard = threading.Lock()

    def start(self) -> dict[str, Any]:
        if self.shader_manager is None:
            self.shader_manager = shader_live_edit_manager()
        # Publication of health must precede capture resume. The existing
        # registered-owner tick handles cold start and GUI reopen alike.
        return {"enabled": True, "attempted": False, "resumeDeferredToOwnerTick": True}

    def save_shader_source(
        self, payload: bytes | bytearray | memoryview
    ) -> tuple[int, dict[str, Any]]:
        if self.shader_manager is None:
            raise RuntimeError("internal Shader service has not started")
        return shader_editor_save_response(payload, self.shader_manager)

    def check_shader_source(
        self, payload: bytes | bytearray | memoryview
    ) -> tuple[int, dict[str, Any]]:
        if self.shader_manager is None:
            raise RuntimeError("internal Shader service has not started")
        return shader_editor_check_response(payload, self.shader_manager)

    def refresh_shader_sessions(self, owner_pids: list[int]) -> dict[str, Any]:
        """Reconcile registered GUI captures during the existing owner tick.

        This reads at most 1024 mailboxes and never calls back into the UI.
        Multiple live GUIs with the same capture are ambiguous; only an
        explicit current-capture operation may choose between them.
        """
        from .ipc_client import INSTANCE_ROOT, IPCClient, bind_direct_tool_call

        reconcile = getattr(self.shader_manager, "reconcile_capture", None)
        if not callable(reconcile) or not self._shader_refresh_guard.acquire(blocking=False):
            return {"attempted": False, "results": []}
        try:
            owners = {pid for pid in owner_pids if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0}
            seen_owners: set[int] = set()
            unavailable_owner = False
            by_capture: dict[str, list[dict[str, str]]] = {}
            directories = []
            for directory in Path(INSTANCE_ROOT).glob("*"):
                if len(directories) >= 1024:
                    return {"attempted": False, "results": [], "reason": "mailbox scan limit reached"}
                directories.append(directory)
            for directory in directories:
                # Include a physically live but busy GUI in uniqueness. The
                # ordinary IPC discovery filters old heartbeats, which would
                # incorrectly erase a still-active competing capture here.
                manifest = _shader_capture_mailbox(directory)
                if (not manifest or manifest.get("pid") not in owners
                        or manifest.get("instanceId") != directory.name):
                    continue
                seen_owners.add(manifest["pid"])
                if manifest.get("stopped") is True or manifest.get("captureLoaded") is False:
                    continue
                session = manifest.get("captureSessionId")
                path = manifest.get("capturePath")
                if (not isinstance(session, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                        or not isinstance(path, str) or not path or not Path(path).is_absolute()):
                    unavailable_owner = True
                    continue
                scope = {"instanceId": directory.name, "captureSessionId": session, "capturePath": path}
                state = _shader_capture_session_state(scope, int(manifest["pid"]))
                if state != "current":
                    unavailable_owner |= state == "unavailable"
                    continue
                by_capture.setdefault(os.path.normcase(str(Path(path).resolve())), []).append(scope)
            if unavailable_owner or any(IPCClient._process_alive(pid) for pid in owners - seen_owners):
                return {"attempted": False, "results": [], "reason": "registered GUI mailbox is unavailable"}
            results = []
            for candidates in by_capture.values():
                if len(candidates) != 1:
                    continue
                scope = candidates[0]
                if IPCClient._manifest(str(Path(INSTANCE_ROOT) / scope["instanceId"])) is None:
                    continue
                try:
                    with bind_direct_tool_call(scope["instanceId"], scope["captureSessionId"]):
                        result = reconcile(scope)
                    results.append({"binding": scope, "result": result})
                except Exception as exc:
                    results.append({"binding": scope, "errorType": type(exc).__name__, "message": str(exc)[:2048]})
            return {"attempted": True, "results": results}
        finally:
            self._shader_refresh_guard.release()

    def asset_index_operation(self, payload: bytes) -> tuple[int, dict[str, Any]]:
        """Run the project asset scan in this long-lived process (see asset_index_jobs)."""

        from . import server
        from .asset_index_jobs import ensure_index_job, index_job_status

        try:
            value = parse_asset_index_operation(payload)
            unused_service, status, asset_root, database_path = server._active_asset_catalog_context(
                require_catalog=False, create_dictionary=(value["operation"] == "ensure"))
            build_id = str(status.get("scope", {}).get("buildId", "") or "")
            if value["operation"] == "ensure":
                job = ensure_index_job(database_path, asset_root, build_id, depth=value["depth"], owner="ReplayLabInternal")
            else:
                job = index_job_status(database_path)
            catalog = None
            try:
                if database_path.is_file():
                    catalog = server.AssetCatalogService(database_path).status()
            except Exception as exc:  # noqa: BLE001 - a half-written catalog is reported, not fatal
                catalog = {"ready": False, "reason": str(exc)[:200]}
            return 200, {"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": value["requestId"],
                         "result": {"job": job, "catalog": catalog, "databasePath": str(database_path),
                                    "assetRoot": str(asset_root), "buildId": build_id}}
        except (ValueError, UnicodeError) as exc:
            return 400, {"error": str(exc)[:2048]}
        except Exception as exc:
            return 409, {"error": str(exc)[:2048]}

    def shader_live_edit_operation(self, payload: bytes) -> tuple[int, dict[str, Any]]:
        from . import server
        from .ipc_client import bind_direct_tool_call

        try:
            value = parse_shader_live_edit_operation(payload)
            if self.shader_manager is None:
                raise RuntimeError("internal Shader service has not started")
            binding = value["binding"]
            with bind_direct_tool_call(binding["instanceId"], binding["captureSessionId"]):
                context = server._active_live_context(allow_missing_capture_id=True)
                capture = context["capture"]
                if (capture["captureSessionId"] != binding["captureSessionId"]
                        or os.path.normcase(str(Path(capture["path"]).resolve())) != os.path.normcase(str(Path(binding["capturePath"]).resolve()))):
                    raise RuntimeError("Shader live-edit capture session changed")
                operation = value["operation"]
                if operation == "list":
                    reconcile = getattr(self.shader_manager, "reconcile_capture", None)
                    if callable(reconcile):
                        reconcile(binding)
                elif operation in {"poll", "stop"}:
                    require_binding = getattr(self.shader_manager, "require_capture_binding", None)
                    if callable(require_binding):
                        require_binding(value["parameters"]["live_edit_id"], binding)
                method = {"start": self.shader_manager.start, "list": self.shader_manager.list,
                          "poll": self.shader_manager.poll, "stop": self.shader_manager.stop,
                          "migrate": self.shader_manager.migrate_authorization}[operation]
                result = method(**value["parameters"])
                if operation == "list" and isinstance(result, dict):
                    # Retained historical jobs and a still-live foreign GUI
                    # are not active editor authority for this caller.
                    result["sessions"] = [item for item in result.get("sessions", [])
                                          if item.get("captureBinding") == binding]
            if not isinstance(result, dict):
                raise RuntimeError("Shader live-edit operation returned invalid state")
            return 200, {"schema": SHADER_LIVE_EDIT_OPERATION_SCHEMA, "requestId": value["requestId"],
                         "binding": binding, "result": result}
        except (ValueError, UnicodeError) as exc:
            return 400, {"error": str(exc)[:2048]}
        except Exception as exc:
            return 409, {"error": str(exc)[:2048]}

    def shutdown(self) -> None:
        self.restore_jobs.shutdown(wait=True)
        shutdown_shader_live_edits()
