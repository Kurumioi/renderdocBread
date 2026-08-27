"""Portable ReplayLab MCP entry point and qrenderdoc-owned loopback sidecar.

qrenderdoc loads a first-party bridge from its fixed built-in integration
directory. The bridge runs in embedded Python 3.6 and deliberately remains a
thin replay/UI adapter. This module is packaged as ``ReplayLabMCP.exe`` with a
Python 3.10+ runtime and supports two transports:

``stdio``
    Spawned and owned by an MCP client such as Codex.
``serve`` / ``ensure``
    A qrenderdoc-owned, bearer-protected streamable-HTTP endpoint bound only to
    loopback. Multiple qrenderdoc processes register owner PID files and the
    host exits after the last owner disappears.

No model SDK, ChatGPT credential, Codex token, or OpenAI API key is read here.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
import ctypes
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .console_events import emit_console_event
from .version import SERVICE_VERSION


SERVICE_NAME = "ReplayLab"
DEFAULT_PORT = 49643
MAX_PORT_SCAN = 16
OWNER_IDLE_GRACE_SECONDS = 15.0
OWNER_POLL_SECONDS = 1.0
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
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
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_INFORMATION_CLASS_POWER_THROTTLING = 4


def _configure_utf8_stdio() -> None:
    """Make every host entry path deterministic on Windows ANSI locales.

    The packaged entry point already applies this before importing the host,
    but ``python -m src.replaylab_host`` and unit/adapter entry paths call this
    module directly.  Repeating the idempotent configuration here prevents a
    Chinese path in a traceback, project result, or MCP diagnostic from being
    encoded as CP936 before a UTF-8 client sees it.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                # StringIO/test embedders and already-detached streams need no
                # encoding change; the binary packaged streams do.
                pass


class _ProcessPowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("ControlMask", ctypes.c_uint32),
        ("StateMask", ctypes.c_uint32),
    ]


def disable_windows_execution_speed_throttling(
    kernel32: Any | None = None,
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Keep the host's watcher/apply workers responsive in the background.

    Windows may opt a background/minimised process into execution-speed power
    throttling. Setting the documented control bit while clearing its state bit
    explicitly opts this process out. This is process-local, does not alter a
    system power plan, and is reported rather than treated as an apply-safety
    boundary on platforms where the API is unavailable.

    ``kernel32`` and ``platform_name`` are injectable only for deterministic
    unit tests; production callers provide neither.
    """

    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return {
            "supported": False,
            "executionSpeedThrottlingDisabled": False,
            "reason": "not-windows",
        }
    try:
        api = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.argtypes = []
        api.GetCurrentProcess.restype = ctypes.c_void_p
        api.SetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        api.SetProcessInformation.restype = ctypes.c_int
        state = _ProcessPowerThrottlingState(
            Version=PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0,
        )
        succeeded = bool(
            api.SetProcessInformation(
                api.GetCurrentProcess(),
                PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
        report = {
            "supported": True,
            "executionSpeedThrottlingDisabled": succeeded,
            "processInformationClass": PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
            "controlMask": PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            "stateMask": 0,
        }
        if not succeeded:
            report["errorCode"] = int(ctypes.get_last_error())
        return report
    except Exception as error:
        return {
            "supported": False,
            "executionSpeedThrottlingDisabled": False,
            "reason": "%s: %s" % (type(error).__name__, error),
        }


def _prepare_shader_tools() -> None:
    """Discover/hash-pin process-owner tools before importing the MCP server."""
    try:
        from .replaylab_tools import configure_environment

        report = configure_environment(Path(sys.executable).resolve().parent)
        if report.get("error"):
            print("ReplayLab shader tools: %s" % report["error"], file=sys.stderr)
    except Exception as error:
        # Capture inspection remains useful without an external compiler. Tool
        # operations will expose the precise missing/invalid registry error.
        print(
            "ReplayLab shader tool discovery failed: %s: %s"
            % (type(error).__name__, error),
            file=sys.stderr,
        )


def runtime_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    configured = str(env.get("REPLAYLAB_RUNTIME_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return (Path(local).expanduser() / "ReplayLab" / "runtime").resolve()
    user_suffix = str(getattr(os, "getuid", lambda: os.getpid())())
    return (Path(tempfile.gettempdir()) / ("ReplayLab-" + user_suffix)).resolve()


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
    from .active_projects import ActiveReplayProjectService

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


def _paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "owners": root / "owners",
        "token": root / "host.token",
        "state": root / "host.json",
        "shutdown": root / "shutdown.request",
    }


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


def _load_or_create_token(root: Path) -> str:
    paths = _paths(root)
    _secure_directory(root)
    try:
        token = paths["token"].read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if TOKEN_PATTERN.fullmatch(token):
        return token

    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(str(paths["token"]), flags, 0o600)
    except FileExistsError:
        token = paths["token"].read_text(encoding="ascii").strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise RuntimeError("ReplayLab token file is corrupt")
        return token
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        paths["token"].chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token


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


def _owner_path(root: Path, pid: int) -> Path:
    if int(pid) <= 0:
        raise ValueError("owner PID must be positive")
    return _paths(root)["owners"] / ("%d.json" % int(pid))


def register_owner(root: Path, pid: int) -> Path:
    owner = _owner_path(root, pid)
    _atomic_json(
        owner,
        {
            "schema": "replaylab-host-owner/v1",
            "pid": int(pid),
            "registeredUnixMs": int(time.time() * 1000),
        },
    )
    return owner


def release_owner(root: Path, pid: int) -> bool:
    owner = _owner_path(root, pid)
    try:
        owner.unlink()
        return True
    except FileNotFoundError:
        return False


def live_owners(root: Path) -> list[int]:
    directory = _paths(root)["owners"]
    _secure_directory(directory)
    result: list[int] = []
    for owner in directory.glob("*.json"):
        try:
            pid = int(owner.stem)
        except ValueError:
            try:
                owner.unlink()
            except OSError:
                pass
            continue
        value = _load_json(owner)
        if value is None or int(value.get("pid", -1)) != pid or not _process_alive(pid):
            try:
                owner.unlink()
            except OSError:
                pass
            continue
        result.append(pid)
    return sorted(set(result))


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def _state_port(root: Path) -> int | None:
    state = _load_json(_paths(root)["state"])
    try:
        port = int((state or {}).get("port", 0))
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def choose_port(root: Path, requested: int | None = None) -> int:
    configured = requested
    if configured is None:
        raw = os.environ.get("REPLAYLAB_MCP_PORT", "").strip()
        if raw:
            configured = int(raw)
    if configured is not None:
        if not 1024 <= int(configured) <= 65535:
            raise ValueError("ReplayLab port must be between 1024 and 65535")
        return int(configured)
    previous = _state_port(root)
    candidates = ([previous] if previous is not None else []) + list(
        range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN)
    )
    for candidate in dict.fromkeys(candidates):
        if _port_is_free(candidate):
            return candidate
    raise RuntimeError("No free ReplayLab loopback port is available")


def health(port: int, timeout: float = 0.75) -> dict[str, Any] | None:
    request = Request("http://127.0.0.1:%d/health" % int(port), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read(32 * 1024).decode("utf-8"))
    except (OSError, HTTPError, URLError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("service") != SERVICE_NAME:
        return None
    return value


def _self_command(arguments: Sequence[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "src.replaylab_host",
        *arguments,
    ]


def _spawn_detached(arguments: Sequence[str]) -> None:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
            | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            | int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        )
    subprocess.Popen(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
        cwd=str(Path(sys.executable).resolve().parent),
        shell=False,
    )


def ensure(root: Path, owner_pid: int, port: int | None = None) -> dict[str, Any]:
    _secure_directory(root)
    register_owner(root, owner_pid)
    selected_port = port or _state_port(root)
    current = health(selected_port) if selected_port else None
    if current is not None:
        if current.get("version") != SERVICE_VERSION:
            raise RuntimeError(
                "A different ReplayLab host version is already running; close all "
                "qrenderdoc instances before upgrading"
            )
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "started": False,
            "port": int(selected_port),
            "endpoint": "http://127.0.0.1:%d/mcp" % int(selected_port),
        }

    selected_port = choose_port(root, port)
    command = _self_command(
        [
            "serve",
            "--runtime-dir",
            str(root),
            "--port",
            str(selected_port),
            "--owner-pid",
            str(owner_pid),
        ]
    )
    _spawn_detached(command)
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        current = health(selected_port)
        if current is not None:
            return {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "started": True,
                "port": selected_port,
                "endpoint": "http://127.0.0.1:%d/mcp" % selected_port,
            }
        time.sleep(0.1)
    raise RuntimeError("ReplayLab host did not become healthy within 12 seconds")


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
    completed_monotonic: float | None = None
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
    current = value.get("current")
    total = value.get("total")
    for item, label in ((current, "progress.current"), (total, "progress.total")):
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item > MAX_PROJECT_RESTORE_PROGRESS_COUNT
        ):
            raise ValueError("%s must be a bounded non-negative integer" % label)
    clean = {
        "phase": phase,
        "message": message,
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
) -> dict[str, Any]:
    """Import the restore implementation only inside the background worker."""

    from .project_restore import restore_active_capture_project

    return restore_active_capture_project(
        project_root=project_root,
        capture_path=capture_path,
        capture_session_id=capture_session_id,
        progress_callback=progress_callback,
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
        self._monotonic = monotonic
        self._unix_time = unix_time
        self._lock = threading.Lock()
        self._jobs: dict[str, _ProjectRestoreJob] = {}
        self._active_by_key: dict[tuple[str, str, str], str] = {}
        self._closed = False
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
            key=lambda job: (float(job.completed_monotonic), job.job_id),
        )
        excess = max(0, len(remaining) - self._max_history_jobs)
        expired.update(job.job_id for job in remaining[:excess])
        for job_id in expired:
            self._jobs.pop(job_id, None)

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
                "current": 0,
                "total": 0,
            }
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in self._ACTIVE_STATUSES:
                return
            if job.latest_sequence >= 0x7FFFFFFF:
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
            job.status = "running"
            job.updated_unix_ms = self._unix_ms()
            inputs = (job.project_root, job.capture_path, job.capture_session_id)
        try:
            value = self._runner(
                inputs[0],
                inputs[1],
                inputs[2],
                progress_callback=lambda event: self._append_progress(job_id, event),
            )
            result = _bounded_restore_json_object(
                value, "project restore result", MAX_PROJECT_RESTORE_RESULT_BYTES
            )
        except BaseException as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.status = "failed"
                job.error = _bounded_restore_error(exc)
                job.completed_monotonic = self._monotonic()
                job.updated_unix_ms = self._unix_ms()
                if self._active_by_key.get(job.key) == job_id:
                    self._active_by_key.pop(job.key, None)
                self._cleanup_locked(job.completed_monotonic)
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "completed"
            job.result = result
            job.completed_monotonic = self._monotonic()
            job.updated_unix_ms = self._unix_ms()
            if self._active_by_key.get(job.key) == job_id:
                self._active_by_key.pop(job.key, None)
            self._cleanup_locked(job.completed_monotonic)

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
            self._cleanup_locked(self._monotonic())
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return self._snapshot_locked(job, after)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
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


class BearerAuthApp:
    """Small ASGI gate that leaves only ``/health`` unauthenticated."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = ("Bearer " + token).encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") != "/health":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _serve(root: Path, port: int, owner_pid: int | None = None) -> int:
    # Heavy MCP/ASGI imports occur only in the packaged Python 3.10+ host.
    import uvicorn
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if not 1024 <= int(port) <= 65535:
        raise ValueError("ReplayLab port must be between 1024 and 65535")
    power_policy = disable_windows_execution_speed_throttling()
    if (
        power_policy.get("supported") is True
        and power_policy.get("executionSpeedThrottlingDisabled") is not True
    ):
        emit_console_event(
            "host",
            "warning",
            "Windows 后台执行速度限制未能关闭，文件监听延迟可能增加。",
            phase="lifecycle",
            code="host.background-throttling-warning",
            context={"operation": "serve"},
        )
        print(
            "ReplayLab background execution-speed throttling could not be disabled "
            "(Win32 error %s)" % power_policy.get("errorCode", "unknown"),
            file=sys.stderr,
        )
    _secure_directory(root)
    paths = _paths(root)
    token = _load_or_create_token(root)
    if owner_pid is not None:
        register_owner(root, owner_pid)
    try:
        paths["shutdown"].unlink()
    except FileNotFoundError:
        pass

    # Process-owned generation marker for recoverable workspace leases.  It is
    # deliberately replaced for every singleton host generation and cannot be
    # supplied by an MCP request.
    os.environ["REPLAYLAB_HOST_INSTANCE_ID"] = secrets.token_hex(16)
    _prepare_shader_tools()
    from .server import _shader_live_edit_manager, mcp

    # A qrender-owned host may be restarted independently of the editor.  The
    # durable, capture-bound authorisations are resumed before the HTTP endpoint
    # becomes reachable so a later file save cannot race manager construction.
    _shader_live_edit_manager().resume_persistent()

    async def health_endpoint(_request: StarletteRequest) -> JSONResponse:
        return JSONResponse(
            {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "transport": "streamable-http",
                "pid": os.getpid(),
                "backgroundExecution": power_policy,
            }
        )

    restore_jobs = ProjectRestoreJobRegistry()

    async def project_restore_post_endpoint(
        request: StarletteRequest,
    ) -> JSONResponse:
        status_code, value = await project_restore_post_response(request, restore_jobs)
        return JSONResponse(value, status_code=status_code)

    async def project_restore_get_endpoint(
        request: StarletteRequest,
    ) -> JSONResponse:
        status_code, value = project_restore_get_response(
            request.path_params.get("job_id"),
            request.query_params.get("after", "0"),
            restore_jobs,
        )
        return JSONResponse(value, status_code=status_code)

    app = mcp.streamable_http_app()
    app.routes.insert(
        0,
        Route(
            "/replaylab/project-restore/{job_id}",
            project_restore_get_endpoint,
            methods=["GET"],
        ),
    )
    app.routes.insert(
        0,
        Route(
            "/replaylab/project-restore",
            project_restore_post_endpoint,
            methods=["POST"],
        ),
    )
    app.routes.insert(0, Route("/health", health_endpoint, methods=["GET"]))
    protected = BearerAuthApp(app, token)
    started_ms = int(time.time() * 1000)
    empty_since: float | None = None
    server_box: dict[str, Any] = {}

    def publish_state() -> None:
        _atomic_json(
            paths["state"],
            {
                "schema": "replaylab-host/v1",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": int(port),
                "endpoint": "http://127.0.0.1:%d/mcp" % int(port),
                "tokenFile": str(paths["token"]),
                "startedUnixMs": started_ms,
                "heartbeatUnixMs": int(time.time() * 1000),
                "ownerPids": live_owners(root),
                "backgroundExecution": power_policy,
            },
        )

    async def monitor() -> None:
        """Run owner lifecycle checks on uvicorn's own event loop.

        A PyInstaller-frozen Windows executable can receive a console control
        interruption while ``threading.Thread.start()`` waits for its bootstrap
        event. Keeping the lifecycle callback on uvicorn's loop avoids that
        packaging-specific failure and also avoids a second mutable execution
        context around ``server.should_exit``.
        """

        nonlocal empty_since
        server = server_box.get("server")
        owners = live_owners(root)
        if paths["shutdown"].exists():
            if server is not None:
                server.should_exit = True
        elif owners:
            empty_since = None
        elif empty_since is None:
            empty_since = time.monotonic()
        elif time.monotonic() - empty_since >= OWNER_IDLE_GRACE_SECONDS:
            if server is not None:
                server.should_exit = True
        publish_state()

    config = uvicorn.Config(
        protected,
        host="127.0.0.1",
        port=int(port),
        log_level="warning",
        access_log=False,
        server_header=False,
        callback_notify=monitor,
        timeout_notify=max(1, int(OWNER_POLL_SECONDS)),
    )
    server = uvicorn.Server(config)
    server_box["server"] = server

    publish_state()
    emit_console_event(
        "host",
        "info",
        "ReplayLab Host 已完成初始化并开始监听。",
        phase="lifecycle",
        code="host.ready",
        context={"operation": "serve"},
    )
    try:
        server.run()
    finally:
        server.should_exit = True
        restore_jobs.shutdown(wait=True)
        state = _load_json(paths["state"])
        if state is not None and int(state.get("pid", -1)) == os.getpid():
            try:
                paths["state"].unlink()
            except FileNotFoundError:
                pass
        try:
            paths["shutdown"].unlink()
        except FileNotFoundError:
            pass
    return 0


def serve(root: Path, port: int, owner_pid: int | None = None) -> int:
    """Run the host while publishing non-authoritative lifecycle telemetry."""

    emit_console_event(
        "host",
        "info",
        "ReplayLab Host 正在启动。",
        phase="lifecycle",
        code="host.starting",
        context={"operation": "serve"},
    )
    try:
        return _serve(root, port, owner_pid)
    except Exception as exc:
        emit_console_event(
            "host",
            "error",
            "ReplayLab Host 运行失败：%s" % str(exc)[:2048],
            phase="lifecycle",
            code="host.failed",
            context={"operation": "serve"},
        )
        raise
    finally:
        emit_console_event(
            "host",
            "info",
            "ReplayLab Host 已停止。",
            phase="lifecycle",
            code="host.stopped",
            context={"operation": "serve"},
        )


def status(root: Path) -> dict[str, Any]:
    state = _load_json(_paths(root)["state"])
    port = _state_port(root)
    current = health(port) if port else None
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "running": current is not None,
        "port": port,
        "endpoint": ("http://127.0.0.1:%d/mcp" % port) if port else None,
        "owners": live_owners(root),
        "state": state,
        # Deliberately disclose only the path, never the bearer secret.
        "tokenFile": str(_paths(root)["token"]),
    }


def codex_toml(executable: Path) -> str:
    path = str(executable.resolve()).replace("\\", "/").replace("'", "\\'")
    return "\n".join(
        [
            "[mcp_servers.replaylab]",
            "command = '%s'" % path,
            "args = ['stdio']",
            "startup_timeout_sec = 30",
            "tool_timeout_sec = 3600",
            "",
        ]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ReplayLabMCP",
        description="Portable ReplayLab MCP host for qrenderdoc and Codex",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "stdio",
            "serve",
            "ensure",
            "release",
            "status",
            "codex-config",
            "project-list",
            "project-create",
            "project-register",
            "project-unregister",
            "project-activate",
            "project-configure",
            "project-add-capture",
            "project-remove-capture",
            "project-match-capture",
        ),
        default="stdio",
    )
    parser.add_argument("--runtime-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--executable")
    parser.add_argument("--project-root")
    parser.add_argument("--name")
    parser.add_argument("--game-id")
    parser.add_argument("--build-id")
    parser.add_argument("--platform", default="")
    parser.add_argument("--api-family", default="")
    parser.add_argument("--asset-root")
    parser.add_argument("--clear-asset-root", action="store_true")
    parser.add_argument("--capture", action="append", default=[])
    parser.add_argument("--capture-ref-id", action="append", default=[])
    parser.add_argument("--association-lease")
    parser.add_argument("--association-token")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "adapter-hlsl":
        from .shader_workspace.adapters.hlsl_decompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-dxbc-assembly":
        from .shader_workspace.adapters.dxbc_assembly_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-d3dcompile":
        from .shader_workspace.adapters.d3dcompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-dxil-assemble":
        from .shader_workspace.adapters.dxil_assembler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-spirv-cross":
        from .shader_workspace.adapters.spirv_cross_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-renodx":
        from .shader_workspace.adapters.renodx_decompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    arguments = _parser().parse_args(raw_arguments)
    root = (
        Path(arguments.runtime_dir).expanduser().resolve()
        if arguments.runtime_dir
        else runtime_root()
    )
    if arguments.command == "stdio":
        power_policy = disable_windows_execution_speed_throttling()
        if (
            power_policy.get("supported") is True
            and power_policy.get("executionSpeedThrottlingDisabled") is not True
        ):
            emit_console_event(
                "host",
                "warning",
                "Windows 后台执行速度限制未能关闭，文件监听延迟可能增加。",
                phase="lifecycle",
                code="host.background-throttling-warning",
                context={"operation": "stdio"},
            )
            print(
                "ReplayLab background execution-speed throttling could not be disabled "
                "(Win32 error %s)" % power_policy.get("errorCode", "unknown"),
                file=sys.stderr,
            )
        _prepare_shader_tools()
        from .server import main as stdio_main

        stdio_main()
        return 0
    if arguments.command == "serve":
        port = choose_port(root, arguments.port)
        return serve(root, port, arguments.owner_pid)
    if arguments.command == "ensure":
        if not arguments.owner_pid:
            raise SystemExit("ensure requires --owner-pid")
        print(json.dumps(ensure(root, arguments.owner_pid, arguments.port), indent=2))
        return 0
    if arguments.command == "release":
        if not arguments.owner_pid:
            raise SystemExit("release requires --owner-pid")
        print(json.dumps({"released": release_owner(root, arguments.owner_pid)}))
        return 0
    if arguments.command == "status":
        print(json.dumps(status(root), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "codex-config":
        executable = (
            Path(arguments.executable).expanduser().resolve()
            if arguments.executable
            else Path(sys.executable).resolve()
        )
        print(codex_toml(executable), end="")
        return 0
    if arguments.command == "project-list":
        print(
            json.dumps(
                project_service().list_projects(), ensure_ascii=False, indent=2
            )
        )
        return 0
    if arguments.command == "project-create":
        if not arguments.name or not arguments.project_root:
            raise SystemExit("project-create requires --name and --project-root")
        value = project_service().create_project(
            name=arguments.name,
            game_id=arguments.game_id or "",
            build_id=arguments.build_id or "",
            platform=arguments.platform,
            api_family=arguments.api_family,
            asset_root=arguments.asset_root,
            capture_files=arguments.capture,
            project_root=arguments.project_root,
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-register":
        if not arguments.project_root:
            raise SystemExit("project-register requires --project-root")
        value = project_service().register_existing_project(arguments.project_root)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-unregister":
        if not arguments.project_root:
            raise SystemExit("project-unregister requires --project-root")
        value = project_service().unregister_project(arguments.project_root)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-activate":
        if not arguments.project_root:
            raise SystemExit("project-activate requires --project-root")
        value = project_service().activate(arguments.project_root)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-configure":
        if not arguments.project_root:
            raise SystemExit("project-configure requires --project-root")
        if arguments.asset_root and arguments.clear_asset_root:
            raise SystemExit(
                "project-configure cannot combine --asset-root and --clear-asset-root"
            )
        game_id = arguments.game_id or ""
        build_id = arguments.build_id or ""
        if bool(game_id) != bool(build_id):
            raise SystemExit(
                "project-configure requires both --game-id and --build-id, or neither"
            )
        settings: dict[str, Any] = {
            "game_id": game_id,
            "build_id": build_id,
            "platform": arguments.platform,
            "api_family": arguments.api_family,
        }
        if arguments.asset_root is not None:
            settings["asset_root"] = arguments.asset_root
        elif arguments.clear_asset_root:
            settings["asset_root"] = None
        service = project_service()
        current = service.configure_project_settings(
            arguments.project_root, **settings
        )
        print(
            json.dumps(
                service.project_status(current["projectRoot"]),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if arguments.command == "project-add-capture":
        if not arguments.project_root or not arguments.capture:
            raise SystemExit(
                "project-add-capture requires --project-root and at least one --capture"
            )
        value = project_service().add_capture_files(
            arguments.project_root, arguments.capture
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-remove-capture":
        if not arguments.project_root or not arguments.capture_ref_id:
            raise SystemExit(
                "project-remove-capture requires --project-root and at least one --capture-ref-id"
            )
        value = project_service().remove_capture_references(
            arguments.project_root, arguments.capture_ref_id
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "project-match-capture":
        if len(arguments.capture) != 1:
            raise SystemExit(
                "project-match-capture requires exactly one --capture"
            )
        if bool(arguments.association_lease) != bool(arguments.association_token):
            raise SystemExit(
                "project-match-capture requires both association lease arguments, or neither"
            )
        guard = (
            project_association_guard(
                arguments.association_lease,
                arguments.association_token,
                arguments.capture[0],
            )
            if arguments.association_lease else None
        )
        value = project_service().match_and_activate_capture(
            arguments.capture[0], activation_guard=guard
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
