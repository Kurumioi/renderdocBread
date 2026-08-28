"""Direct client for the bundled qrenderdoc bridge protocol v2.

The internal AgentHost uses this transport through its controlled ToolService.
ReplayLabMCP is a separate external adapter for IDEs and other out-of-process
clients; it is not part of the internal model-to-RenderDoc call path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BridgeError


def default_ipc_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path(tempfile.gettempdir())
    return root / "ReplayLab" / "ipc-v2"


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a harmless existence probe on every Windows
        # Python build. Query a minimal process handle instead.
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class BridgeInstance:
    id: str
    pid: int
    directory: Path
    protocol_version: int
    bridge_version: str
    heartbeat_ms: int
    activity_ms: int
    capture_loaded: bool
    capture_path: str


class BridgeClient:
    def __init__(
        self,
        ipc_root: Path | str | None = None,
        timeout: float = 30.0,
        heartbeat_ttl: float = 15.0,
    ) -> None:
        self.ipc_root = Path(ipc_root) if ipc_root else default_ipc_root()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.heartbeat_ttl = float(heartbeat_ttl)
        if self.heartbeat_ttl <= 0:
            raise ValueError("heartbeat_ttl must be positive")

    def instances(self, *, live_only: bool = True) -> list[BridgeInstance]:
        instance_root = self.ipc_root / "instances"
        if not instance_root.is_dir():
            return []
        result: list[BridgeInstance] = []
        for directory in instance_root.iterdir():
            manifest_path = directory / "instance.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                pid = int(value.get("pid", 0))
                stopped = bool(value.get("stopped", False))
                heartbeat_ms = int(value.get("heartbeatUnixMs", 0))
                heartbeat_age_ms = int(time.time() * 1000) - heartbeat_ms
                if live_only and (
                    stopped
                    or not _pid_is_live(pid)
                    or heartbeat_ms <= 0
                    or heartbeat_age_ms < -5000
                    or heartbeat_age_ms > int(self.heartbeat_ttl * 1000)
                ):
                    continue
                protocol = int(value.get("protocolVersion", 0))
                if protocol < 2:
                    continue
                result.append(
                    BridgeInstance(
                        id=str(value.get("instanceId", directory.name)),
                        pid=pid,
                        directory=directory,
                        protocol_version=protocol,
                        bridge_version=str(value.get("bridgeVersion", "")),
                        heartbeat_ms=heartbeat_ms,
                        activity_ms=int(value.get("lastActivityUnixMs", 0)),
                        capture_loaded=bool(value.get("captureLoaded", False)),
                        capture_path=str(value.get("capturePath", "")),
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        result.sort(
            key=lambda item: (item.activity_ms, item.heartbeat_ms, item.id), reverse=True
        )
        return result

    def select_instance(self, instance_id: str = "") -> BridgeInstance:
        candidates = self.instances()
        if instance_id:
            for candidate in candidates:
                if candidate.id == instance_id:
                    return candidate
            raise BridgeError(f"qrenderdoc instance is not live: {instance_id}")
        if not candidates:
            raise BridgeError("no live qrenderdoc ReplayLab bridge was found")
        loaded = [candidate for candidate in candidates if candidate.capture_loaded]
        return (loaded or candidates)[0]

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        instance_id: str = "",
        timeout: float | None = None,
    ) -> Any:
        if not method or not isinstance(method, str):
            raise BridgeError("bridge method name is required")
        instance = self.select_instance(instance_id)
        request_dir = instance.directory / "requests"
        response_dir = instance.directory / "responses"
        request_dir.mkdir(parents=True, exist_ok=True)
        response_dir.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        request_path = request_dir / f"{request_id}.json"
        response_path = response_dir / f"{request_id}.json"
        payload = {"id": request_id, "method": method, "params": params or {}}
        self._atomic_json(request_path, payload)
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise BridgeError("bridge timeout must be positive")
        deadline = time.monotonic() + request_timeout
        try:
            while time.monotonic() < deadline:
                if response_path.is_file():
                    try:
                        response = json.loads(response_path.read_text(encoding="utf-8"))
                    except OSError:
                        time.sleep(0.02)
                        continue
                    except UnicodeDecodeError as exc:
                        raise BridgeError("bridge returned a response that is not valid UTF-8") from exc
                    except json.JSONDecodeError as exc:
                        raise BridgeError("bridge returned malformed JSON") from exc
                    if not isinstance(response, dict):
                        raise BridgeError("bridge returned a non-object JSON response")
                    if str(response.get("id", "")) != request_id:
                        raise BridgeError("bridge response id does not match the request")
                    error = response.get("error")
                    if isinstance(error, dict):
                        raise BridgeError(str(error.get("message", "bridge request failed")))
                    return response.get("result")
                time.sleep(0.02)
        finally:
            try:
                response_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise BridgeError(f"bridge request timed out after {request_timeout:.1f}s: {method}")

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise BridgeError(f"cannot write bridge request: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
