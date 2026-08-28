"""
File-based IPC client for communicating with RenderDocUltra's built-in bridge.

Protocol (v2):
  1. Atomically publish requests/<request-id>.json
  2. Extension atomically claims and executes that request
  3. Extension writes responses/<same-name>.json
  4. We verify both the file name and response id, then delete our response

ReplayLab v2 bridge instances live under
``%LOCALAPPDATA%/ReplayLab/ipc-v2/instances/<instance-id>``. The client binds
each call to the most recently interacted-with live qrenderdoc instance, or to
``REPLAYLAB_INSTANCE_ID`` when the process owner explicitly selects one. The
legacy global mailbox remains a fallback during migration.
"""

import contextvars
import json
import os
import time
import tempfile
import threading
import uuid
import logging
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger("renderdoc-mcp")

LEGACY_IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp_ipc")
_local_root = os.environ.get("LOCALAPPDATA", "").strip() or tempfile.gettempdir()
IPC_ROOT = os.path.join(_local_root, "ReplayLab", "ipc-v2")
INSTANCE_ROOT = os.path.join(IPC_ROOT, "instances")
# Public legacy constants are retained for compatibility and for isolated
# protocol tests that patch one mailbox directly.
IPC_DIR = LEGACY_IPC_DIR
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
READY_FILE = os.path.join(IPC_DIR, "ready")
REQUEST_DIR = os.path.join(IPC_DIR, "requests")
RESPONSE_DIR = os.path.join(IPC_DIR, "responses")
_LEGACY_REQUEST_DIR = REQUEST_DIR
_LEGACY_RESPONSE_DIR = RESPONSE_DIR
_CALL_BINDING: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "replaylab_direct_tool_call_binding", default=None
)


@contextmanager
def bind_direct_tool_call(instance_id: str, capture_session_id: str):
    """Pin all nested IPC calls made by one in-process AgentHost tool.

    The external MCP process continues to use its ordinary constructor/env
    binding.  This context is only set by AgentHost's direct high-level
    workflow dispatcher and is local to that worker thread/context.
    """

    instance = str(instance_id or "").strip()
    session = str(capture_session_id or "").strip().lower()
    if not instance or instance != instance_id or "\0" in instance or len(instance) > 512:
        raise ValueError("direct ReplayLab tool requires a pinned qrenderdoc instance")
    if session and (
        len(session) != 32
        or any(character not in "0123456789abcdef" for character in session)
    ):
        raise ValueError("direct ReplayLab tool capture session is invalid")
    token = _CALL_BINDING.set((instance, session))
    try:
        yield
    finally:
        _CALL_BINDING.reset(token)


class IPCClient:
    """Communicates with RenderDocUltra's built-in bridge via file IPC."""

    def __init__(self, timeout: float = 30.0, instance_id: str | None = None):
        self.timeout = timeout
        self.instance_id = (
            str(instance_id or os.environ.get("REPLAYLAB_INSTANCE_ID", "")).strip()
        )
        self._explicit_instance = bool(self.instance_id)
        # Keep calls ordered inside one MCP process. The v2 protocol also uses
        # per-request files, so independent processes cannot delete or consume
        # each other's response.
        self._mailbox_lock = threading.Lock()

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            # ``os.kill(pid, 0)`` can surface WinError 87 as ``SystemError`` in
            # a PyInstaller-frozen process. The packaged stdio MCP uses this
            # path to select a live qrenderdoc mailbox, so query a read-only
            # process handle just like the ReplayLab owner monitor does.
            try:
                import ctypes

                process_query_limited_information = 0x1000
                synchronize = 0x00100000
                still_active = 259
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = [
                    ctypes.c_uint32,
                    ctypes.c_int,
                    ctypes.c_uint32,
                ]
                kernel32.OpenProcess.restype = ctypes.c_void_p
                kernel32.GetExitCodeProcess.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                kernel32.GetExitCodeProcess.restype = ctypes.c_int
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int
                handle = kernel32.OpenProcess(
                    process_query_limited_information | synchronize,
                    False,
                    int(pid),
                )
                if not handle:
                    return ctypes.get_last_error() == 5
                try:
                    exit_code = ctypes.c_uint32(0)
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return int(exit_code.value) == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _manifest(directory: str) -> dict | None:
        path = os.path.join(directory, "instance.json")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        try:
            heartbeat = int(value.get("heartbeatUnixMs", 0))
            pid = int(value.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if value.get("stopped") or heartbeat <= 0 or pid <= 0:
            return None
        # Five missed one-second heartbeats is enough to avoid routing a replay
        # command to a dead or blocked qrenderdoc integration.
        if int(time.time() * 1000) - heartbeat > 5000:
            return None
        if not IPCClient._process_alive(pid):
            return None
        return value

    def _instance_mailbox(self) -> tuple[str, str] | None:
        binding = _CALL_BINDING.get()
        if binding is not None:
            instance_id, _capture_session_id = binding
            directory = os.path.join(INSTANCE_ROOT, instance_id)
            manifest = self._manifest(directory)
            if manifest is None:
                raise RuntimeError(
                    "Requested ReplayLab qrenderdoc instance is not alive: %s"
                    % instance_id
                )
            return (
                os.path.join(directory, "requests"),
                os.path.join(directory, "responses"),
            )

        candidates: list[tuple[tuple[int, int, int], str, dict]] = []
        if self._explicit_instance:
            directory = os.path.join(INSTANCE_ROOT, self.instance_id)
            manifest = self._manifest(directory)
            if manifest is None:
                raise RuntimeError(
                    "Requested ReplayLab qrenderdoc instance is not alive: %s"
                    % self.instance_id
                )
            candidates.append(((0, 0, 0), directory, manifest))
        else:
            # An implicitly selected instance is only the result of the last
            # lookup, not a binding. Re-scan on every call so a newly opened
            # capture can take precedence over an older capture-less window.
            try:
                names = os.listdir(INSTANCE_ROOT)
            except OSError:
                names = []
            for name in names:
                directory = os.path.join(INSTANCE_ROOT, name)
                if not os.path.isdir(directory):
                    continue
                manifest = self._manifest(directory)
                if manifest is None:
                    continue
                try:
                    score = (
                        int(bool(manifest.get("captureLoaded"))),
                        int(manifest.get("lastActivityUnixMs", 0)),
                        int(manifest.get("startedUnixMs", 0)),
                    )
                except (TypeError, ValueError):
                    continue
                candidates.append((score, directory, manifest))
        if not candidates:
            if not self._explicit_instance:
                self.instance_id = ""
            return None
        _score, directory, manifest = max(candidates, key=lambda item: item[0])
        self.instance_id = str(manifest.get("instanceId", ""))
        return (
            os.path.join(directory, "requests"),
            os.path.join(directory, "responses"),
        )

    def _mailbox(self) -> tuple[str, str]:
        # Unit tests and compatibility embedders may explicitly replace the
        # public mailbox constants with an isolated directory.
        if REQUEST_DIR != _LEGACY_REQUEST_DIR or RESPONSE_DIR != _LEGACY_RESPONSE_DIR:
            return REQUEST_DIR, RESPONSE_DIR
        selected = self._instance_mailbox()
        if selected is not None:
            return selected
        return REQUEST_DIR, RESPONSE_DIR

    def is_bridge_alive(self) -> bool:
        try:
            resp = self.call("ping", {}, timeout=3.0)
            return resp is not None and "status" in resp
        except Exception:
            return False

    def call(self, method: str, params: dict, timeout: Optional[float] = None) -> Any:
        values = dict(params or {})
        binding = _CALL_BINDING.get()
        if binding is not None and binding[1]:
            expected = values.get("__replaylab_expected_capture_session_id")
            if expected not in (None, "", binding[1]):
                raise RuntimeError(
                    "ReplayLab direct tool attempted to change its frozen capture session"
                )
            values["__replaylab_expected_capture_session_id"] = binding[1]
        with self._mailbox_lock:
            return self._call_locked(method, values, timeout)

    def _call_locked(
        self, method: str, params: dict, timeout: Optional[float] = None
    ) -> Any:
        if timeout is None:
            timeout = self.timeout

        request_dir, response_dir = self._mailbox()
        os.makedirs(request_dir, exist_ok=True)
        os.makedirs(response_dir, exist_ok=True)

        # Build request
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "method": method, "params": params}
        request_json = json.dumps(request, ensure_ascii=False, default=str)
        file_name = request_id + ".json"
        request_path = os.path.join(request_dir, file_name)
        response_path = os.path.join(response_dir, file_name)
        temporary_path = request_path + ".tmp.%d" % os.getpid()

        # A request only becomes visible after the complete UTF-8 document has
        # been flushed and atomically renamed into the queue.
        with open(temporary_path, "w", encoding="utf-8") as f:
            f.write(request_json)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, request_path)

        # Wait for our matching response. Cleanup is deliberately scoped to
        # this UUID, including protocol errors and bridge-side failures.
        try:
            start = time.time()
            while time.time() - start < timeout:
                if os.path.exists(response_path):
                    time.sleep(0.05)
                    try:
                        with open(response_path, "r", encoding="utf-8") as f:
                            response = json.load(f)

                        response_id = response.get("id")
                        if response_id != request_id:
                            raise RuntimeError(
                                "Bridge protocol error: response id %r does not match %r"
                                % (response_id, request_id)
                            )

                        if "error" in response:
                            err = response["error"]
                            raise RuntimeError("Bridge error [%s]: %s" % (
                                err.get("code", "?"), err.get("message", str(err))))
                        return response.get("result", response)
                    except json.JSONDecodeError:
                        time.sleep(0.1)
                        continue
                time.sleep(0.1)

            raise RuntimeError(
                "Timeout (%.0fs). Make sure RenderDoc is running with capture loaded. "
                "Check Tools > ReplayLab/RenderDoc MCP > Status." % timeout)
        finally:
            # Never touch another process's request/response or the legacy
            # global mailbox.
            for owned_path in (request_path, response_path, temporary_path):
                try:
                    if os.path.exists(owned_path):
                        os.remove(owned_path)
                except OSError:
                    pass
