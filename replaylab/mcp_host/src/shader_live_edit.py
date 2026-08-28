"""Persistent compile/validate/apply sessions for a Shader workspace.

The low-level workspace watcher intentionally compiles only.  This module adds
one explicitly-authorised host-side monitor which may pass a newly compiled
candidate to the existing shared-resource apply transaction.  It never accepts
an executable, compiler argument, ResourceId, event set, or ABI claim from the
caller: those remain owned by the workspace and replay validators.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shader_workspace.io import atomic_write_json, is_within, read_json_object

LIVE_EDIT_SCHEMA = "replaylab-shader-live-edit/v1"
LIVE_EDIT_VERSION = 1
MAX_ACTIVE_LIVE_EDITS = 8
MAX_RETAINED_LIVE_EDITS = 32
MAX_AUDIT_EVENTS = 128
POLL_SECONDS = 0.05
DEFAULT_DEBOUNCE_MS = 100
JOIN_SECONDS = 3.0
MIN_POLL_SECONDS = 0.001
MAX_POLL_SECONDS = 10.0
MIN_JOIN_SECONDS = 0.05
MAX_JOIN_SECONDS = 30.0
LIVE_EDIT_REGISTRY_SCHEMA = "replaylab-shader-live-edit-registry/v1"
LIVE_EDIT_REGISTRY_VERSION = 1
MAX_LIVE_EDIT_REGISTRY_BYTES = 1024 * 1024

SHARED_APPLY_SCHEMA = "renderdoc-ultra-shader-shared-resource-apply-mcp/v1"
SHARED_ROLLBACK_TOOL = "rollback_shader_workspace_shared_resource"
AUTHORIZATION_MIGRATION_EVIDENCE_SCHEMA = (
    "renderdoc-ultra-shader-live-edit-authorization-migration-evidence/v1"
)

_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ShaderLiveEditError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded(value: Any, maximum: int = 2048) -> str:
    text = str(value)
    return text if len(text) <= maximum else text[:maximum] + "...[truncated]"


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShaderLiveEditError("%s must be a positive integer" % label)
    return value


def _duration(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShaderLiveEditError("%s must be a finite number" % label)
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ShaderLiveEditError(
            "%s must be between %.3f and %.3f seconds" % (label, minimum, maximum)
        )
    return result


def _workspace_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _persistent_identity(value: Any) -> dict[str, Any]:
    """Validate the small, capture-bound identity stored for auto-resume.

    The callback that produces this value is process-owned.  Persisting the
    identity prevents a later directory at the same path from inheriting an
    old user's auto-apply authorisation.
    """

    if not isinstance(value, dict):
        raise ShaderLiveEditError("workspace identity callback returned a non-object")
    required = {
        "workspaceId",
        "captureId",
        "capturePath",
        "shaderKey",
        "shaderRawSha256",
        "resourceId",
        "stage",
        "entryPoint",
    }
    if set(value) != required:
        raise ShaderLiveEditError("workspace identity callback returned invalid fields")
    workspace_id = str(value.get("workspaceId", ""))
    capture_id = str(value.get("captureId", ""))
    capture_path = str(value.get("capturePath", ""))
    shader_key = str(value.get("shaderKey", ""))
    shader_raw_sha256 = str(value.get("shaderRawSha256", ""))
    stage = str(value.get("stage", ""))
    entry_point = str(value.get("entryPoint", ""))
    resource_id = value.get("resourceId")
    if _TOKEN.fullmatch(workspace_id) is None:
        raise ShaderLiveEditError("workspace identity has an invalid workspaceId")
    if _SHA256.fullmatch(capture_id) is None:
        raise ShaderLiveEditError("workspace identity has an invalid captureId")
    if not capture_path or len(capture_path) > 32768:
        raise ShaderLiveEditError("workspace identity has an invalid capturePath")
    if not shader_key or len(shader_key) > 4096:
        raise ShaderLiveEditError("workspace identity has an invalid shaderKey")
    if _SHA256.fullmatch(shader_raw_sha256) is None:
        raise ShaderLiveEditError("workspace identity has an invalid shader hash")
    if isinstance(resource_id, bool) or not isinstance(resource_id, int) or resource_id <= 0:
        raise ShaderLiveEditError("workspace identity has an invalid ResourceId")
    if not stage or len(stage) > 64 or not entry_point or len(entry_point) > 512:
        raise ShaderLiveEditError("workspace identity has an invalid stage/entry point")
    return {
        "workspaceId": workspace_id,
        "captureId": capture_id,
        "capturePath": capture_path,
        "shaderKey": shader_key,
        "shaderRawSha256": shader_raw_sha256,
        "resourceId": resource_id,
        "stage": stage,
        "entryPoint": entry_point,
    }


def _physical_shader_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Remove only the workspace generation from a validated identity."""

    normalized = _persistent_identity(identity)
    return {
        key: value
        for key, value in normalized.items()
        if key != "workspaceId"
    }


class _PersistentLiveEditRegistry:
    """Atomic process-owned authorisation registry used only for restart resume."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema": LIVE_EDIT_REGISTRY_SCHEMA,
            "schemaVersion": LIVE_EDIT_REGISTRY_VERSION,
            "entries": [],
        }

    def _validate_entry(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "authorizationId",
            "workspacePath",
            "workspaceIdentity",
            "workspaceIdentitySha256",
            "debounceMs",
            "authorizedAt",
            "updatedAt",
            "lastLiveEditId",
        }:
            raise ShaderLiveEditError("persistent live-edit registry entry is invalid")
        authorization_id = str(value.get("authorizationId", ""))
        workspace_path = str(value.get("workspacePath", ""))
        identity = _persistent_identity(value.get("workspaceIdentity"))
        identity_hash = str(value.get("workspaceIdentitySha256", ""))
        last_live_edit_id = str(value.get("lastLiveEditId", "") or "")
        if _TOKEN.fullmatch(authorization_id) is None:
            raise ShaderLiveEditError("persistent live-edit authorization ID is invalid")
        if not workspace_path or len(workspace_path) > 32768:
            raise ShaderLiveEditError("persistent live-edit workspace path is invalid")
        if identity_hash != _canonical_sha256(identity):
            raise ShaderLiveEditError("persistent live-edit workspace identity hash mismatch")
        if last_live_edit_id and _TOKEN.fullmatch(last_live_edit_id) is None:
            raise ShaderLiveEditError("persistent live-edit last session ID is invalid")
        debounce = value.get("debounceMs")
        if isinstance(debounce, bool) or not isinstance(debounce, int):
            raise ShaderLiveEditError("persistent live-edit debounce is invalid")
        # The low-level watcher remains the authority for the exact bounds.
        if debounce < 100 or debounce > 5000:
            raise ShaderLiveEditError("persistent live-edit debounce is out of range")
        for field in ("authorizedAt", "updatedAt"):
            timestamp = str(value.get(field, ""))
            if not timestamp or len(timestamp) > 128:
                raise ShaderLiveEditError(
                    "persistent live-edit %s is invalid" % field
                )
        return {
            "authorizationId": authorization_id,
            "workspacePath": workspace_path,
            "workspaceIdentity": identity,
            "workspaceIdentitySha256": identity_hash,
            "debounceMs": debounce,
            "authorizedAt": str(value["authorizedAt"]),
            "updatedAt": str(value["updatedAt"]),
            "lastLiveEditId": last_live_edit_id or None,
        }

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            if self.path.is_symlink():
                raise ShaderLiveEditError("persistent live-edit registry is redirected")
            return self._empty()
        if self.path.is_symlink() or not self.path.is_file():
            raise ShaderLiveEditError("persistent live-edit registry is redirected")
        try:
            value = read_json_object(
                self.path,
                MAX_LIVE_EDIT_REGISTRY_BYTES,
                "persistent live-edit registry",
            )
        except ValueError as exc:
            raise ShaderLiveEditError(str(exc)) from exc
        if set(value) != {"schema", "schemaVersion", "entries"} or (
            value.get("schema") != LIVE_EDIT_REGISTRY_SCHEMA
            or value.get("schemaVersion") != LIVE_EDIT_REGISTRY_VERSION
        ):
            raise ShaderLiveEditError("persistent live-edit registry schema is invalid")
        entries = value.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_ACTIVE_LIVE_EDITS:
            raise ShaderLiveEditError("persistent live-edit registry entries are invalid")
        normalized = [self._validate_entry(entry) for entry in entries]
        keys = [_workspace_key(Path(entry["workspacePath"])) for entry in normalized]
        if len(set(keys)) != len(keys):
            raise ShaderLiveEditError("persistent live-edit registry has duplicate workspaces")
        value["entries"] = normalized
        return value

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._load_locked()["entries"])

    def authorize(
        self,
        workspace: Path,
        identity: dict[str, Any],
        debounce_ms: int,
        *,
        authorization_id: str | None = None,
        last_live_edit_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_identity = _persistent_identity(identity)
        key = _workspace_key(workspace)
        with self._lock:
            registry = self._load_locked()
            entries = registry["entries"]
            now = _utc_now()
            existing = next(
                (
                    entry
                    for entry in entries
                    if _workspace_key(Path(entry["workspacePath"])) == key
                ),
                None,
            )
            if existing is None:
                if len(entries) >= MAX_ACTIVE_LIVE_EDITS:
                    raise ShaderLiveEditError(
                        "persistent live-edit authorization limit reached (%d)"
                        % MAX_ACTIVE_LIVE_EDITS
                    )
                existing = {
                    "authorizationId": authorization_id or uuid.uuid4().hex,
                    "workspacePath": str(workspace),
                    "workspaceIdentity": normalized_identity,
                    "workspaceIdentitySha256": _canonical_sha256(normalized_identity),
                    "debounceMs": debounce_ms,
                    "authorizedAt": now,
                    "updatedAt": now,
                    "lastLiveEditId": last_live_edit_id,
                }
                entries.append(existing)
            else:
                if (
                    authorization_id is not None
                    and existing["authorizationId"] != authorization_id
                ):
                    raise ShaderLiveEditError(
                        "persistent live-edit authorization changed during resume"
                    )
                if existing["workspaceIdentity"] != normalized_identity:
                    raise ShaderLiveEditError(
                        "workspace identity changed; explicit live-edit authorisation is required"
                    )
                existing["debounceMs"] = debounce_ms
                existing["updatedAt"] = now
                existing["lastLiveEditId"] = last_live_edit_id
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.parent.is_symlink() or self.path.parent.resolve() != self.path.parent:
                raise ShaderLiveEditError("persistent live-edit registry directory is redirected")
            atomic_write_json(self.path, registry)
            return copy.deepcopy(existing)

    def transfer(
        self,
        source_workspace: Path,
        workspace: Path,
        source_identity: dict[str, Any],
        identity: dict[str, Any],
        *,
        authorization_id: str,
    ) -> dict[str, Any]:
        """Atomically move one exact authorization to a verified successor."""

        normalized_source = _persistent_identity(source_identity)
        normalized_target = _persistent_identity(identity)
        source_key = _workspace_key(source_workspace)
        target_key = _workspace_key(workspace)
        if source_key == target_key:
            raise ShaderLiveEditError(
                "persistent live-edit authorization migration requires two workspaces"
            )
        if _TOKEN.fullmatch(str(authorization_id or "")) is None:
            raise ShaderLiveEditError(
                "persistent live-edit authorization ID is invalid"
            )
        with self._lock:
            registry = self._load_locked()
            entries = registry["entries"]
            source = next(
                (
                    entry
                    for entry in entries
                    if _workspace_key(Path(entry["workspacePath"])) == source_key
                ),
                None,
            )
            if source is None:
                raise ShaderLiveEditError(
                    "source workspace has no persistent live-edit authorization"
                )
            if (
                source["authorizationId"] != authorization_id
                or source["workspaceIdentity"] != normalized_source
            ):
                raise ShaderLiveEditError(
                    "source live-edit authorization changed during migration"
                )
            if any(
                _workspace_key(Path(entry["workspacePath"])) == target_key
                for entry in entries
            ):
                raise ShaderLiveEditError(
                    "target workspace already has a persistent live-edit authorization"
                )
            source.update(
                {
                    "workspacePath": str(workspace),
                    "workspaceIdentity": normalized_target,
                    "workspaceIdentitySha256": _canonical_sha256(normalized_target),
                    "updatedAt": _utc_now(),
                    "lastLiveEditId": None,
                }
            )
            atomic_write_json(self.path, registry)
            return copy.deepcopy(source)

    def revoke(self, workspace: Path, authorization_id: str | None = None) -> bool:
        key = _workspace_key(workspace)
        with self._lock:
            registry = self._load_locked()
            retained = []
            removed = False
            for entry in registry["entries"]:
                matches = _workspace_key(Path(entry["workspacePath"])) == key
                if matches and (
                    authorization_id is None
                    or entry["authorizationId"] == authorization_id
                ):
                    removed = True
                    continue
                retained.append(entry)
            if removed:
                registry["entries"] = retained
                atomic_write_json(self.path, registry)
            return removed

    def revoke_unique_last_live_edit_id(
        self, live_edit_id: str
    ) -> dict[str, Any] | None:
        """Revoke one orphan authorization by its exact last session ID.

        This recovery path deliberately does not accept a workspace fallback.
        Duplicate session IDs are treated as ambiguous corruption and leave the
        complete registry unchanged.
        """

        if _TOKEN.fullmatch(str(live_edit_id or "")) is None:
            raise ShaderLiveEditError(
                "live_edit_id must be a 32-character lowercase hex ID"
            )
        with self._lock:
            registry = self._load_locked()
            matches = [
                entry
                for entry in registry["entries"]
                if entry["lastLiveEditId"] == live_edit_id
            ]
            if not matches:
                return None
            if len(matches) != 1:
                raise ShaderLiveEditError(
                    "persistent live-edit session ID is ambiguous; no authorization was revoked"
                )
            target = matches[0]
            target_key = _workspace_key(Path(target["workspacePath"]))
            target_authorization_id = target["authorizationId"]
            retained = [
                entry
                for entry in registry["entries"]
                if not (
                    _workspace_key(Path(entry["workspacePath"])) == target_key
                    and entry["authorizationId"] == target_authorization_id
                )
            ]
            if len(retained) != len(registry["entries"]) - 1:
                raise ShaderLiveEditError(
                    "persistent live-edit authorization identity is ambiguous; "
                    "no authorization was revoked"
                )
            registry["entries"] = retained
            atomic_write_json(self.path, registry)
            return copy.deepcopy(target)


def _resolved_workspace(value: str | Path, label: str) -> Path:
    try:
        result = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ShaderLiveEditError("%s is not an available workspace: %s" % (label, exc)) from exc
    if not result.is_dir():
        raise ShaderLiveEditError("%s is not a directory" % label)
    return result


def _validate_watch_baseline(watch: dict[str, Any]) -> None:
    state = str(watch.get("state", ""))
    if state not in {"starting", "running"}:
        raise ShaderLiveEditError("workspace watcher was not active when live-edit started")
    if not isinstance(watch.get("compileInProgress"), bool):
        raise ShaderLiveEditError(
            "workspace watcher returned an invalid initial compileInProgress state"
        )
    revision = watch.get("latestCandidateRevision")
    raw_sha256 = str(watch.get("latestCandidateRawSha256", "") or "")
    if revision is None:
        if raw_sha256:
            raise ShaderLiveEditError(
                "workspace watcher returned candidate bytes without an initial revision"
            )
        return
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ShaderLiveEditError("workspace watcher returned an invalid initial revision")
    if _SHA256.fullmatch(raw_sha256) is None:
        raise ShaderLiveEditError(
            "workspace watcher returned an initial revision without a valid raw SHA-256"
        )


def _watch_counter(watch: dict[str, Any], key: str) -> int:
    """Return one monotonic watcher counter without trusting arbitrary shapes."""

    value = watch.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShaderLiveEditError(
            "workspace watcher returned an invalid %s counter" % key
        )
    return value


def _watch_boolean(watch: dict[str, Any], key: str) -> bool:
    """Read one watcher state flag without accepting truthy substitutes."""

    value = watch.get(key)
    if not isinstance(value, bool):
        raise ShaderLiveEditError(
            "workspace watcher returned an invalid %s state" % key
        )
    return value


def _watch_error(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ShaderLiveEditError("workspace watcher returned an invalid error")
    return {
        "at": _bounded(value.get("at", ""), 128),
        "type": _bounded(value.get("type", "WorkspaceWatchError"), 128),
        "message": _bounded(value.get("message", "workspace compile failed")),
    }


def _watch_summary(watch: dict[str, Any]) -> dict[str, Any]:
    """Project a bounded compile-status view for the live-edit audit."""

    revision = watch.get("latestCandidateRevision")
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0
    ):
        raise ShaderLiveEditError("workspace watcher returned an invalid candidate revision")
    raw_sha256 = str(watch.get("latestCandidateRawSha256", "") or "")
    if revision is None:
        if raw_sha256:
            raise ShaderLiveEditError(
                "workspace watcher returned candidate bytes without a revision"
            )
    elif _SHA256.fullmatch(raw_sha256) is None:
        raise ShaderLiveEditError(
            "workspace watcher returned a candidate without a valid raw SHA-256"
        )

    hashes: dict[str, str] = {}
    for key in (
        "observedSourceSha256",
        "lastAttemptedSourceSha256",
        "lastCompiledSourceSha256",
    ):
        value = str(watch.get(key, "") or "")
        if value and _SHA256.fullmatch(value) is None:
            raise ShaderLiveEditError(
                "workspace watcher returned an invalid %s" % key
            )
        hashes[key] = value

    return {
        "state": str(watch.get("state", "")),
        "compileInProgress": _watch_boolean(watch, "compileInProgress"),
        "pending": _watch_boolean(watch, "pending"),
        "caughtUp": _watch_boolean(watch, "caughtUp"),
        "observedSourceSha256": hashes["observedSourceSha256"],
        "lastAttemptedSourceSha256": hashes["lastAttemptedSourceSha256"],
        "lastCompiledSourceSha256": hashes["lastCompiledSourceSha256"],
        "latestCandidateRevision": revision,
        "latestCandidateRawSha256": raw_sha256,
        "compileAttempts": _watch_counter(watch, "compileAttempts"),
        "compileSuccesses": _watch_counter(watch, "compileSuccesses"),
        "compileFailures": _watch_counter(watch, "compileFailures"),
        "lastError": _watch_error(watch.get("lastError")),
    }


def _latest_watch_compile_failure(watch: dict[str, Any]) -> dict[str, str] | None:
    events = watch.get("events")
    if not isinstance(events, list):
        return _watch_error(watch.get("lastError"))
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        # The watcher keeps bounded history after recovery.  The newest
        # compile outcome, rather than any older failure still in that history,
        # defines the current error state.
        if event.get("type") == "compileSucceeded":
            return None
        if event.get("type") == "compileFailed":
            return {
                "at": _bounded(event.get("at", ""), 128),
                "type": _bounded(event.get("errorType", "WorkspaceWatchError"), 128),
                "message": _bounded(event.get("message", "workspace compile failed")),
            }
    return _watch_error(watch.get("lastError"))


def _most_recent_watch_compile_failure(
    watch: dict[str, Any],
) -> dict[str, str] | None:
    """Return failure detail even when a later compile recovered between polls."""

    events = watch.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if isinstance(event, dict) and event.get("type") == "compileFailed":
                return {
                    "at": _bounded(event.get("at", ""), 128),
                    "type": _bounded(
                        event.get("errorType", "WorkspaceWatchError"), 128
                    ),
                    "message": _bounded(
                        event.get("message", "workspace compile failed")
                    ),
                }
    return _watch_error(watch.get("lastError"))


class _LiveEditJob:
    def __init__(
        self,
        *,
        live_edit_id: str,
        watch_snapshot: dict[str, Any],
        watch_manager: Any,
        apply_candidate: Callable[[str, int, str], dict[str, Any]],
        on_finished: Callable[[_LiveEditJob], None],
        poll_seconds: float,
        join_seconds: float,
    ) -> None:
        self.live_edit_id = live_edit_id
        self.watch_id = str(watch_snapshot["watchId"])
        self.workspace = Path(str(watch_snapshot["workspacePath"])).resolve()
        self.watch_manager = watch_manager
        if not callable(apply_candidate):
            raise ShaderLiveEditError("apply_candidate must be callable")
        self.apply_candidate = apply_candidate
        self.on_finished = on_finished
        self.poll_seconds = poll_seconds
        self.join_seconds = join_seconds
        self._lock = threading.RLock()
        self._apply_condition = threading.Condition(self._lock)
        self._watch_stop_lock = threading.Lock()
        self._watch_stopped = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="shader-live-edit-%s" % live_edit_id[:8],
            daemon=True,
        )
        self._apply_thread = threading.Thread(
            target=self._run_apply_queue,
            name="shader-live-apply-%s" % live_edit_id[:8],
            daemon=True,
        )
        self._state = "starting"
        self._created_at = _utc_now()
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._watch_snapshot = _watch_summary(watch_snapshot)
        initial_revision = watch_snapshot.get("latestCandidateRevision")
        # The watcher thread starts before its initial snapshot is returned. A
        # very fast catch-up compile may therefore finish inside
        # ``watch_manager.start``. Such a candidate was compiled after live-edit
        # authorisation and must remain eligible instead of being mistaken for
        # a pre-session candidate.
        catch_up_compiled_during_start = self._watch_snapshot["compileSuccesses"] > 0
        self._latest_handled_revision = (
            int(initial_revision)
            if isinstance(initial_revision, int)
            and not isinstance(initial_revision, bool)
            and initial_revision > 0
            and not catch_up_compiled_during_start
            else 0
        )
        initial_raw_sha256 = str(
            watch_snapshot.get("latestCandidateRawSha256", "") or ""
        )
        self._latest_handled_raw_sha256 = (
            initial_raw_sha256
            if self._latest_handled_revision > 0
            and _SHA256.fullmatch(initial_raw_sha256)
            else ""
        )
        self._latest_scheduled_revision = self._latest_handled_revision
        self._latest_scheduled_raw_sha256 = self._latest_handled_raw_sha256
        self._pending_apply: tuple[int, str] | None = None
        self._apply_in_progress: tuple[int, str] | None = None
        self._superseded_applies = 0
        self._apply_attempts = 0
        self._apply_successes = 0
        self._apply_reuses = 0
        self._apply_rejections = 0
        self._apply_failures = 0
        self._last_apply: dict[str, Any] | None = None
        self._active_apply: dict[str, Any] | None = None
        self._last_error: dict[str, Any] | None = None
        self._last_compile_error = (
            _latest_watch_compile_failure(watch_snapshot)
            if self._watch_snapshot["compileFailures"] > 0
            else None
        )
        self._last_seen_compile_successes = self._watch_snapshot["compileSuccesses"]
        self._last_seen_compile_failures = self._watch_snapshot["compileFailures"]
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_AUDIT_EVENTS)
        self._next_sequence = 1
        self._dropped_events = 0

        live_dir = self.workspace / "live-edit"
        if live_dir.exists() and (live_dir.is_symlink() or not live_dir.is_dir()):
            raise ShaderLiveEditError("workspace live-edit directory is redirected")
        live_dir.mkdir(parents=False, exist_ok=True)
        if live_dir.resolve() != live_dir or not is_within(self.workspace, live_dir):
            raise ShaderLiveEditError("workspace live-edit directory escapes its workspace")
        self.audit_path = live_dir / (live_edit_id + ".json")
        self._validate_audit_path_locked()

    def start(self) -> None:
        with self._lock:
            self._record_locked(
                "liveEditCreated",
                watchId=self.watch_id,
                autoApplyCompatibleSaves=True,
                applyScope="captured-shader-resource",
                catchUpCandidateAlreadyCompiled=(
                    self._watch_snapshot["compileSuccesses"] > 0
                ),
                message=(
                    "Only candidates compiled after this live-edit session starts are "
                    "eligible. Different candidate bytes require fresh ABI/live replay "
                    "validation; bytes identical to the active validated replacement are reused"
                ),
            )
            if self._last_compile_error is not None:
                self._record_locked(
                    "compileFailed",
                    errorType=self._last_compile_error["type"],
                    message=self._last_compile_error["message"],
                    compileFailures=self._watch_snapshot["compileFailures"],
                )
        self._apply_thread.start()
        try:
            self._thread.start()
        except Exception:
            self._stop.set()
            with self._apply_condition:
                self._apply_condition.notify_all()
            self._apply_thread.join(self.join_seconds)
            raise

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state in {"stopped", "failed"}:
                terminal = True
            else:
                terminal = False
        if terminal:
            self._stop_workspace_watcher()
            return self.snapshot()
        self._stop.set()
        with self._apply_condition:
            self._pending_apply = None
            self._apply_condition.notify_all()
        stop_error: Exception | None = None
        with self._lock:
            self._state = "stopping"
            try:
                self._record_locked("liveEditStopRequested")
            except Exception as exc:
                stop_error = exc
        try:
            self._stop_workspace_watcher()
        except Exception as exc:
            if stop_error is None:
                stop_error = exc
        if self._thread is not threading.current_thread():
            self._thread.join(self.join_seconds)
        if self._thread.is_alive():
            raise ShaderLiveEditError(
                "live-edit worker did not stop within %.3f seconds" % self.join_seconds
            )
        if self._apply_thread.is_alive():
            raise ShaderLiveEditError(
                "live-edit apply worker did not stop within %.3f seconds"
                % self.join_seconds
            )
        if stop_error is not None:
            raise ShaderLiveEditError("could not stop live-edit cleanly: %s" % stop_error)
        return self.snapshot()

    def record_persistent_authorization_revoke_failure(
        self, error: BaseException
    ) -> None:
        """Keep a best-effort audit when durable stop consent was not saved.

        The in-memory monitor is stopped independently by ``stop``.  This
        record must therefore never revive or prolong either worker, and an
        unavailable audit file must not hide the original registry failure.
        """

        message = (
            "persistent live-edit authorization revocation was not confirmed: %s"
            % _bounded(error)
        )
        with self._lock:
            self._last_error = {
                "at": _utc_now(),
                "type": type(error).__name__,
                "message": message,
            }
            self._record_locked(
                "persistentAuthorizationRevokeFailed",
                persist_failure_is_fatal=False,
                errorType=type(error).__name__,
                message=message,
                durableAuthorizationRevoked=False,
                monitoringTerminal=self._state in {"stopped", "failed"},
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def is_terminal(self) -> bool:
        with self._lock:
            return self._state in {"stopped", "failed"}

    def save_source(self, expected_raw_sha256: str, text: str) -> dict[str, Any]:
        """Save through the exact running watcher owned by this live session."""

        with self._lock:
            if self._state != "running" or self._stop.is_set():
                raise ShaderLiveEditError("shader live-edit session is not running")
            result = self.watch_manager.save_source(
                self.watch_id, expected_raw_sha256, text
            )
            if not isinstance(result, dict) or result.get("watchId") != self.watch_id:
                raise ShaderLiveEditError(
                    "workspace watcher returned an invalid editor save result"
                )
            return result

    def _run(self) -> None:
        try:
            with self._lock:
                self._state = "stopping" if self._stop.is_set() else "running"
                self._started_at = _utc_now()
                self._record_locked("liveEditStarted")
            while not self._stop.wait(self.poll_seconds):
                watch = self._checked_watch_snapshot(
                    self.watch_manager.poll(self.watch_id)
                )
                self._observe_watch(watch)
                state = str(watch.get("state", ""))
                if state in {"stopping", "stopped", "failed"}:
                    if state == "failed":
                        detail = watch.get("lastError")
                        raise ShaderLiveEditError(
                            "workspace watcher failed%s"
                            % (": %s" % _bounded(detail) if detail else "")
                        )
                    break
                revision = watch.get("latestCandidateRevision")
                raw_sha256 = str(watch.get("latestCandidateRawSha256", "") or "")
                with self._lock:
                    scheduled_revision = self._latest_scheduled_revision
                    scheduled_raw_sha256 = self._latest_scheduled_raw_sha256
                if (
                    isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and revision == scheduled_revision
                    and scheduled_raw_sha256
                    and _SHA256.fullmatch(raw_sha256)
                    and raw_sha256 != scheduled_raw_sha256
                ):
                    raise ShaderLiveEditError(
                        "workspace watcher changed candidate bytes without advancing revision"
                    )
                if (
                    isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and revision > scheduled_revision
                ):
                    # A save can be observed after candidate N was published but
                    # before N+1 is compiled.  Revision alone would re-apply N
                    # against the newer source.  Only the watcher snapshot which
                    # proves its compiled candidate is caught up to the exact
                    # observed source is eligible for mutation.
                    if (
                        watch.get("compileInProgress") is not False
                        or watch.get("pending") is not False
                        or watch.get("caughtUp") is not True
                    ):
                        continue
                    observed_source_sha256 = str(
                        watch.get("observedSourceSha256", "") or ""
                    )
                    compiled_source_sha256 = str(
                        watch.get("lastCompiledSourceSha256", "") or ""
                    )
                    if (
                        _SHA256.fullmatch(observed_source_sha256) is None
                        or compiled_source_sha256 != observed_source_sha256
                    ):
                        raise ShaderLiveEditError(
                            "workspace watcher claimed caught-up state without an "
                            "exact compiled-source binding"
                        )
                    if _SHA256.fullmatch(raw_sha256) is None:
                        raise ShaderLiveEditError(
                            "workspace watcher published a candidate without a valid raw SHA-256"
                        )
                    self._enqueue_apply(int(revision), raw_sha256)
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded(exc),
                }
                self._record_locked(
                    "liveEditFailed",
                    errorType=type(exc).__name__,
                    message=_bounded(exc),
                    persist_failure_is_fatal=False,
                )
        finally:
            self._stop.set()
            with self._apply_condition:
                self._pending_apply = None
                self._apply_condition.notify_all()
            if self._apply_thread is not threading.current_thread():
                # Match the old single-worker lifetime: an in-flight replay
                # mutation owns the session until it returns. The public stop
                # call remains bounded and may report its existing timeout,
                # while this daemon coordinator finishes safely afterwards.
                self._apply_thread.join()
            with self._lock:
                if self._state != "failed":
                    self._state = "stopped"
                self._stopped_at = _utc_now()
                self._record_locked(
                    "liveEditStopped",
                    message=(
                        "Stopping monitoring does not roll back an already validated replacement"
                    ),
                    persist_failure_is_fatal=False,
                )
            try:
                self._stop_workspace_watcher()
            except Exception as exc:
                with self._lock:
                    if self._last_error is None:
                        self._last_error = {
                            "at": _utc_now(),
                            "type": type(exc).__name__,
                            "message": _bounded(exc),
                        }
                    self._record_locked(
                        "workspaceWatchStopFailed",
                        errorType=type(exc).__name__,
                        message=_bounded(exc),
                        persist_failure_is_fatal=False,
                    )
            try:
                self.on_finished(self)
            except Exception as exc:
                # The worker is already terminal. Do not turn manager bookkeeping
                # failure into an unhandled daemon-thread exception.
                with self._lock:
                    self._last_error = {
                        "at": _utc_now(),
                        "type": type(exc).__name__,
                        "message": _bounded(exc),
                    }

    def _enqueue_apply(self, revision: int, raw_sha256: str) -> None:
        """Publish one caught-up candidate to a single-slot latest-wins queue."""

        with self._apply_condition:
            if self._stop.is_set():
                return
            if revision <= self._latest_scheduled_revision:
                return
            superseded = self._pending_apply
            self._pending_apply = (revision, raw_sha256)
            self._latest_scheduled_revision = revision
            self._latest_scheduled_raw_sha256 = raw_sha256
            if superseded is not None:
                self._superseded_applies += 1
                self._record_locked(
                    "applySuperseded",
                    revision=superseded[0],
                    candidateRawSha256=superseded[1],
                    supersededByRevision=revision,
                    supersededByCandidateRawSha256=raw_sha256,
                )
            self._record_locked(
                "applyQueued",
                revision=revision,
                candidateRawSha256=raw_sha256,
                queuePolicy="latest-wins",
            )
            self._apply_condition.notify_all()

    def _run_apply_queue(self) -> None:
        """Apply serially while the independent monitor keeps polling saves."""

        while True:
            with self._apply_condition:
                self._apply_condition.wait_for(
                    lambda: self._pending_apply is not None or self._stop.is_set()
                )
                if self._stop.is_set():
                    self._pending_apply = None
                    return
                candidate = self._pending_apply
                self._pending_apply = None
                if candidate is None:  # defensive condition invariant
                    continue
                self._apply_in_progress = candidate
            try:
                self._apply(candidate[0], candidate[1])
            except Exception as exc:
                # Audit persistence and internal evidence checks can fail before
                # the callback's own guarded section. Such a failure terminates
                # the authorisation instead of silently losing the apply worker.
                with self._lock:
                    # Persist the terminal snapshot with no phantom in-flight
                    # candidate.  In particular, a durable applyStarted record
                    # may exist even though its mutation was never entered.
                    self._apply_in_progress = None
                    self._state = "failed"
                    self._last_error = {
                        "at": _utc_now(),
                        "type": type(exc).__name__,
                        "message": _bounded(exc),
                    }
                    self._record_locked(
                        "liveEditFailed",
                        errorType=type(exc).__name__,
                        message=_bounded(exc),
                        persist_failure_is_fatal=False,
                    )
                self._stop.set()
                with self._apply_condition:
                    self._pending_apply = None
                    self._apply_condition.notify_all()
                return
            finally:
                with self._apply_condition:
                    self._apply_in_progress = None
                    self._apply_condition.notify_all()

    def _stop_workspace_watcher(self) -> None:
        with self._watch_stop_lock:
            if self._watch_stopped:
                return
            self.watch_manager.stop(self.watch_id)
            self._watch_stopped = True

    def _checked_watch_snapshot(self, watch: Any) -> dict[str, Any]:
        if not isinstance(watch, dict):
            raise ShaderLiveEditError("workspace watcher returned a non-object snapshot")
        if not isinstance(watch.get("watchId"), str) or watch["watchId"] != self.watch_id:
            raise ShaderLiveEditError("workspace watcher identity changed")
        if not isinstance(watch.get("workspacePath"), str):
            raise ShaderLiveEditError("workspace watcher returned an invalid workspace path")
        observed_workspace = _resolved_workspace(
            watch["workspacePath"], "workspace watcher path"
        )
        if _workspace_key(observed_workspace) != _workspace_key(self.workspace):
            raise ShaderLiveEditError("workspace watcher changed its workspace identity")
        state = str(watch.get("state", ""))
        if state not in {"starting", "running", "stopping", "stopped", "failed"}:
            raise ShaderLiveEditError("workspace watcher returned an invalid state")
        if not isinstance(watch.get("compileInProgress"), bool):
            raise ShaderLiveEditError(
                "workspace watcher returned an invalid compileInProgress state"
            )
        return watch

    def _observe_watch(self, watch: dict[str, Any]) -> None:
        summary = _watch_summary(watch)
        with self._lock:
            if summary["compileSuccesses"] < self._last_seen_compile_successes:
                raise ShaderLiveEditError(
                    "workspace watcher compile-success counter moved backwards"
                )
            if summary["compileFailures"] < self._last_seen_compile_failures:
                raise ShaderLiveEditError(
                    "workspace watcher compile-failure counter moved backwards"
                )

            previous_error = self._last_compile_error
            failure_advanced = (
                summary["compileFailures"] > self._last_seen_compile_failures
            )
            success_advanced = (
                summary["compileSuccesses"] > self._last_seen_compile_successes
            )
            self._watch_snapshot = summary

            if failure_advanced:
                self._last_compile_error = (
                    _most_recent_watch_compile_failure(watch)
                    or {
                        "at": _utc_now(),
                        "type": "WorkspaceWatchError",
                        "message": "workspace watcher reported a compile failure",
                    }
                )
                self._record_locked(
                    "compileFailed",
                    errorType=self._last_compile_error["type"],
                    message=self._last_compile_error["message"],
                    compileFailures=summary["compileFailures"],
                    observedSourceSha256=summary["observedSourceSha256"],
                )

            if success_advanced:
                self._last_compile_error = None
                if previous_error is not None or failure_advanced:
                    self._record_locked(
                        "compileRecovered",
                        compileSuccesses=summary["compileSuccesses"],
                        revision=summary["latestCandidateRevision"],
                        candidateRawSha256=summary[
                            "latestCandidateRawSha256"
                        ],
                    )

            self._last_seen_compile_successes = summary["compileSuccesses"]
            self._last_seen_compile_failures = summary["compileFailures"]

    def _apply(self, revision: int, raw_sha256: str) -> None:
        # Mark a candidate handled before invoking the callback. A failed apply
        # stays auditable and is never retried indefinitely without a newer save.
        with self._lock:
            if self._stop.is_set():
                return
            self._latest_handled_revision = revision
            self._latest_handled_raw_sha256 = raw_sha256
            if self._reuse_active_replacement_locked(revision, raw_sha256):
                return
            self._apply_attempts += 1
            # This is the durable pre-mutation barrier.  _record_locked raises
            # after bounded persistence retries, so apply_candidate is never
            # entered unless the exact revision/hash intent is already on disk.
            self._record_locked(
                "applyStarted",
                revision=revision,
                candidateRawSha256=raw_sha256,
                applyScope="captured-shader-resource",
            )
        try:
            result = self.apply_candidate(str(self.workspace), revision, raw_sha256)
            result = self._validated_apply_result(result, revision, raw_sha256)
            applied = result["applied"]
            if applied is True:
                replacement = result["replacement"]
                with self._lock:
                    self._apply_successes += 1
                    self._last_apply = result
                    self._active_apply = result
                    self._last_error = None
                    # Include the queue transition in the same durable snapshot
                    # as the result.  Otherwise the on-disk audit permanently
                    # claims this completed candidate is still in progress.
                    self._apply_in_progress = None
                    self._record_locked(
                        "applySucceeded",
                        revision=revision,
                        candidateRawSha256=raw_sha256,
                        replacementId=replacement.get("replacementId"),
                        originalResourceId=replacement.get("originalResourceId"),
                        originalShaderPreserved=replacement["originalShaderPreserved"],
                        validationScope=result["validation"].get(
                            "validationScope", "full-replay-reflection"
                        ),
                        preliminaryReplayReflectionSkipped=result[
                            "validation"
                        ].get("preliminaryReplayReflectionSkipped", False),
                        freshPhysicalValidationPerformed=result[
                            "validation"
                        ].get("freshPhysicalValidationPerformed", True),
                    )
                return
            if applied is False:
                with self._lock:
                    self._apply_rejections += 1
                    self._last_apply = result
                    self._last_error = None
                    self._apply_in_progress = None
                    self._record_locked(
                        "applyRejected",
                        revision=revision,
                        candidateRawSha256=raw_sha256,
                        reason=_bounded(result.get("reason", "ABI/live replay gate rejected candidate")),
                    )
                return
            raise ShaderLiveEditError("shared-resource apply omitted its applied decision")
        except Exception as exc:
            with self._lock:
                self._apply_failures += 1
                self._apply_in_progress = None
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded(exc),
                }
                self._record_locked(
                    "applyFailed",
                    revision=revision,
                    candidateRawSha256=raw_sha256,
                    errorType=type(exc).__name__,
                    message=_bounded(exc),
                )

    def _reuse_active_replacement_locked(
        self, revision: int, raw_sha256: str
    ) -> bool:
        """Accept an exact byte-identical save without replay work.

        The active replacement exists only after the normal callback returned
        complete ABI, live-capture, replacement and rollback evidence. Reusing
        that same candidate hash therefore does not assert compatibility for
        any new executable bytes and does not call the Bridge at all.
        """

        active = self._active_apply
        replacement = active.get("replacement") if isinstance(active, dict) else None
        if (
            not isinstance(replacement, dict)
            or replacement.get("candidateRawSha256") != raw_sha256
        ):
            return False
        source_revision = _positive_integer(
            replacement.get("revision"), "active replacement revision"
        )
        validation = active.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("replayAbiValidated") is not True
        ):
            raise ShaderLiveEditError(
                "active replacement lost its replay ABI validation evidence"
            )
        reuse = {
            "schema": SHARED_APPLY_SCHEMA,
            "operation": "reuse-active-shared-resource",
            "applied": True,
            "reused": True,
            "mutationPerformed": False,
            "binding": {
                "revision": revision,
                "candidateRawSha256": raw_sha256,
            },
            "validation": {
                "replayAbiValidated": True,
                "performedForRevision": False,
                "evidenceReusedFromRevision": source_revision,
            },
            "replacement": copy.deepcopy(replacement),
            "rollback": copy.deepcopy(active.get("rollback")),
        }
        self._apply_reuses += 1
        self._last_apply = reuse
        self._last_error = None
        self._apply_in_progress = None
        self._record_locked(
            "applyReused",
            revision=revision,
            candidateRawSha256=raw_sha256,
            evidenceReusedFromRevision=source_revision,
            replacementId=replacement.get("replacementId"),
            mutationPerformed=False,
            replayAbiValidationPerformed=False,
        )
        return True

    def _validated_apply_result(
        self, result: Any, revision: int, raw_sha256: str
    ) -> dict[str, Any]:
        """Return bounded evidence for the exact candidate requested by this job."""

        if not isinstance(result, dict):
            raise ShaderLiveEditError("shared-resource apply returned a non-object")
        if result.get("schema") != SHARED_APPLY_SCHEMA:
            raise ShaderLiveEditError("shared-resource apply returned an unexpected schema")
        if result.get("operation") != "apply-shared-resource":
            raise ShaderLiveEditError("shared-resource apply returned an unexpected operation")

        binding = result.get("binding")
        if not isinstance(binding, dict):
            raise ShaderLiveEditError("shared-resource apply omitted candidate binding evidence")
        binding_revision = _positive_integer(
            binding.get("revision"), "shared-resource binding revision"
        )
        if binding_revision != revision:
            raise ShaderLiveEditError("shared-resource apply returned a different candidate revision")
        if binding.get("candidateRawSha256") != raw_sha256:
            raise ShaderLiveEditError("shared-resource apply returned a different candidate hash")

        validation = result.get("validation")
        if not isinstance(validation, dict) or not isinstance(
            validation.get("replayAbiValidated"), bool
        ):
            raise ShaderLiveEditError("shared-resource apply omitted replay ABI evidence")

        applied = result.get("applied")
        if not isinstance(applied, bool):
            raise ShaderLiveEditError("shared-resource apply omitted its applied decision")
        evidence: dict[str, Any] = {
            "schema": SHARED_APPLY_SCHEMA,
            "operation": "apply-shared-resource",
            "applied": applied,
            "binding": {
                "revision": revision,
                "candidateRawSha256": raw_sha256,
            },
            "validation": {
                "replayAbiValidated": validation["replayAbiValidated"],
            },
        }
        optional_validation = {
            "validationScope": str,
            "preliminaryReplayReflectionSkipped": bool,
            "freshPhysicalValidationPerformed": bool,
            "freshLiveCaptureValidationPerformed": bool,
            "abiLeaseRevision": int,
        }
        for key, expected_type in optional_validation.items():
            value = validation.get(key)
            if (
                isinstance(value, expected_type)
                and not (expected_type is int and isinstance(value, bool))
            ):
                evidence["validation"][key] = value

        if applied is False:
            if validation["replayAbiValidated"] is not False:
                raise ShaderLiveEditError(
                    "shared-resource apply rejected a replay-ABI-compatible candidate"
                )
            evidence["reason"] = _bounded(
                result.get("reason", "candidate ABI is not exactly compatible")
            )
            return evidence

        if validation["replayAbiValidated"] is not True:
            raise ShaderLiveEditError(
                "shared-resource apply claimed success without replay ABI validation"
            )
        if result.get("scope") != "captured-resource":
            raise ShaderLiveEditError("shared-resource apply changed its resource scope")

        replacement = result.get("replacement")
        if not isinstance(replacement, dict):
            raise ShaderLiveEditError(
                "successful shared-resource apply omitted replacement evidence"
            )
        replacement_id = replacement.get("replacementId")
        if not isinstance(replacement_id, str) or _TOKEN.fullmatch(replacement_id) is None:
            raise ShaderLiveEditError("shared-resource apply returned an invalid replacement ID")
        replacement_revision = _positive_integer(
            replacement.get("revision"), "shared-resource replacement revision"
        )
        if replacement_revision != revision:
            raise ShaderLiveEditError("shared-resource replacement revision changed")
        if replacement.get("candidateRawSha256") != raw_sha256:
            raise ShaderLiveEditError("shared-resource replacement candidate hash changed")
        original_resource_id = _positive_integer(
            replacement.get("originalResourceId"), "original Shader ResourceId"
        )
        if any(
            (
                replacement.get("resourceScoped") is not True,
                replacement.get("eventScoped") is not False,
                replacement.get("allOriginalResourceUsesAffected") is not True,
                replacement.get("originalShaderPreserved") is not True,
            )
        ):
            raise ShaderLiveEditError(
                "shared-resource replacement did not preserve scope/original Shader evidence"
            )

        rollback = result.get("rollback")
        if not isinstance(rollback, dict):
            raise ShaderLiveEditError("successful shared-resource apply omitted rollback evidence")
        if rollback.get("tool") != SHARED_ROLLBACK_TOOL:
            raise ShaderLiveEditError("shared-resource apply returned an unexpected rollback tool")
        if rollback.get("replacementId") != replacement_id:
            raise ShaderLiveEditError("shared-resource rollback replacement identity changed")
        if not isinstance(rollback.get("workspacePath"), str):
            raise ShaderLiveEditError("shared-resource rollback workspace path is invalid")
        rollback_workspace = _resolved_workspace(
            rollback["workspacePath"], "rollback workspace path"
        )
        if _workspace_key(rollback_workspace) != _workspace_key(self.workspace):
            raise ShaderLiveEditError("shared-resource rollback workspace identity changed")

        evidence.update(
            {
                "scope": "captured-resource",
                "replacement": {
                    "replacementId": replacement_id,
                    "revision": revision,
                    "candidateRawSha256": raw_sha256,
                    "originalResourceId": original_resource_id,
                    "resourceScoped": True,
                    "eventScoped": False,
                    "allOriginalResourceUsesAffected": True,
                    "originalShaderPreserved": True,
                },
                "rollback": {
                    "tool": SHARED_ROLLBACK_TOOL,
                    "workspacePath": str(self.workspace),
                    "replacementId": replacement_id,
                },
            }
        )
        return evidence

    def _record_locked(
        self,
        event_type: str,
        persist_failure_is_fatal: bool = True,
        **details: Any,
    ) -> None:
        if len(self._events) == self._events.maxlen:
            self._dropped_events += 1
        event = {"sequence": self._next_sequence, "at": _utc_now(), "type": event_type}
        self._next_sequence += 1
        event.update(details)
        self._events.append(event)
        try:
            self._validate_audit_path_locked()
            atomic_write_json(self.audit_path, self._snapshot_locked())
        except Exception as exc:
            if persist_failure_is_fatal:
                raise ShaderLiveEditError(
                    "could not persist live-edit audit state: %s" % exc
                ) from exc

    def _validate_audit_path_locked(self) -> None:
        live_dir = self.audit_path.parent if hasattr(self, "audit_path") else self.workspace / "live-edit"
        if live_dir.is_symlink() or not live_dir.is_dir():
            raise ShaderLiveEditError("workspace live-edit directory is redirected")
        try:
            resolved = live_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ShaderLiveEditError("workspace live-edit directory is unavailable: %s" % exc) from exc
        if resolved != live_dir or not is_within(self.workspace, resolved):
            raise ShaderLiveEditError("workspace live-edit directory escapes its workspace")
        if hasattr(self, "audit_path"):
            if self.audit_path.parent != live_dir:
                raise ShaderLiveEditError("live-edit audit path changed parent")
            if self.audit_path.exists() and (
                self.audit_path.is_symlink() or not self.audit_path.is_file()
            ):
                raise ShaderLiveEditError("live-edit audit path is redirected")

    def _snapshot_locked(self) -> dict[str, Any]:
        rollback = None
        if isinstance(self._active_apply, dict):
            rollback = self._active_apply.get("rollback")
        active_replacement = copy.deepcopy(
            self._active_apply.get("replacement")
            if isinstance(self._active_apply, dict)
            else None
        )
        return {
            "schema": LIVE_EDIT_SCHEMA,
            "schemaVersion": LIVE_EDIT_VERSION,
            "liveEditId": self.live_edit_id,
            "state": self._state,
            "workspacePath": str(self.workspace),
            "watchId": self.watch_id,
            "auditPath": str(self.audit_path),
            "createdAt": self._created_at,
            "startedAt": self._started_at,
            "stoppedAt": self._stopped_at,
            "autoApplyCompatibleSaves": True,
            "applyScope": "captured-shader-resource",
            "latestHandledRevision": self._latest_handled_revision,
            "latestHandledCandidateRawSha256": self._latest_handled_raw_sha256,
            "latestScheduledRevision": self._latest_scheduled_revision,
            "latestScheduledCandidateRawSha256": self._latest_scheduled_raw_sha256,
            "pendingApply": (
                {
                    "revision": self._pending_apply[0],
                    "candidateRawSha256": self._pending_apply[1],
                }
                if self._pending_apply is not None
                else None
            ),
            "applyInProgress": (
                {
                    "revision": self._apply_in_progress[0],
                    "candidateRawSha256": self._apply_in_progress[1],
                }
                if self._apply_in_progress is not None
                else None
            ),
            "applyQueuePolicy": "latest-wins",
            "supersededApplies": self._superseded_applies,
            "applyAttempts": self._apply_attempts,
            "applySuccesses": self._apply_successes,
            "applyReuses": self._apply_reuses,
            "applyRejections": self._apply_rejections,
            "applyFailures": self._apply_failures,
            "lastApply": copy.deepcopy(self._last_apply),
            "lastError": copy.deepcopy(self._last_error),
            "lastCompileError": copy.deepcopy(self._last_compile_error),
            "watch": copy.deepcopy(self._watch_snapshot),
            "activeReplacement": active_replacement,
            "lastGoodReplacement": copy.deepcopy(active_replacement),
            "rollback": copy.deepcopy(rollback),
            "droppedAuditEvents": self._dropped_events,
            "events": copy.deepcopy(list(self._events)),
            "policy": {
                "newSaveRequired": True,
                "freshCompileRequired": True,
                "initialFullReplayAbiValidationRequired": True,
                "freshReplayAbiValidationRequired": True,
                "freshPreliminaryReplayReflectionRequired": False,
                "changedBytesUseInlinePhysicalAbiGate": True,
                "freshLiveCaptureValidationRequired": True,
                "identicalActiveCandidateSkipsReplayAbiValidation": True,
                "identicalActiveCandidateSkipsReplayMutation": True,
                "applyQueueLatestWins": True,
                "sharedResourceIdentityPreserved": True,
                "originalShaderPreserved": True,
                "failedSavePreservesLastGoodReplacement": True,
                "aiCanBypassApplyGate": False,
            },
        }


class ShaderLiveEditManager:
    """Own bounded live-edit monitor threads around one watch manager."""

    def __init__(
        self,
        watch_manager: Any,
        apply_candidate: Callable[[str, int, str], dict[str, Any]],
        *,
        poll_seconds: float = POLL_SECONDS,
        join_seconds: float = JOIN_SECONDS,
        registry_path: str | Path | None = None,
        workspace_identity: Callable[[str], dict[str, Any]] | None = None,
        workspace_migration_evidence: (
            Callable[[str, str, str], dict[str, Any]] | None
        ) = None,
    ) -> None:
        self.watch_manager = watch_manager
        if not callable(apply_candidate):
            raise ShaderLiveEditError("apply_candidate must be callable")
        self.apply_candidate = apply_candidate
        self.poll_seconds = _duration(
            poll_seconds, "poll_seconds", MIN_POLL_SECONDS, MAX_POLL_SECONDS
        )
        self.join_seconds = _duration(
            join_seconds, "join_seconds", MIN_JOIN_SECONDS, MAX_JOIN_SECONDS
        )
        if (registry_path is None) != (workspace_identity is None):
            raise ShaderLiveEditError(
                "persistent live edit requires both registry_path and workspace_identity"
            )
        if workspace_identity is not None and not callable(workspace_identity):
            raise ShaderLiveEditError("workspace_identity must be callable")
        if workspace_migration_evidence is not None and not callable(
            workspace_migration_evidence
        ):
            raise ShaderLiveEditError(
                "workspace_migration_evidence must be callable"
            )
        self._registry = (
            _PersistentLiveEditRegistry(registry_path)
            if registry_path is not None
            else None
        )
        self._workspace_identity = workspace_identity
        self._workspace_migration_evidence = workspace_migration_evidence
        self._lock = threading.RLock()
        self._jobs: dict[str, _LiveEditJob] = {}
        self._active_by_workspace: dict[str, str] = {}
        self._starting_workspaces: set[str] = set()
        self._authorization_by_live_edit: dict[str, str] = {}
        self._resume_attempted = False
        self._resume_results: list[dict[str, Any]] = []

    def start(
        self, workspace_path: str | Path, debounce_ms: int = DEFAULT_DEBOUNCE_MS
    ) -> dict[str, Any]:
        return self._start(workspace_path, debounce_ms, persistent_entry=None)

    def _start(
        self,
        workspace_path: str | Path,
        debounce_ms: int,
        *,
        persistent_entry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        requested_root = _resolved_workspace(workspace_path, "workspace_path")
        key = _workspace_key(requested_root)
        with self._lock:
            self._prune_locked()
            existing_id = self._active_by_workspace.get(key)
            if existing_id:
                existing = self._jobs.get(existing_id)
                if existing is not None and not existing.is_terminal():
                    raise ShaderLiveEditError(
                        "workspace already has an active live-edit session: %s" % existing_id
                    )
                self._active_by_workspace.pop(key, None)
            if key in self._starting_workspaces:
                raise ShaderLiveEditError("workspace already has a live-edit start in progress")
            active = sum(not job.is_terminal() for job in self._jobs.values())
            if active + len(self._starting_workspaces) >= MAX_ACTIVE_LIVE_EDITS:
                raise ShaderLiveEditError(
                    "active live-edit limit reached (%d)" % MAX_ACTIVE_LIVE_EDITS
                )
            self._starting_workspaces.add(key)

        watch: dict[str, Any] | None = None
        watch_id = ""
        job: _LiveEditJob | None = None
        authorization: dict[str, Any] | None = None
        authorization_preexisted = persistent_entry is not None
        try:
            if self._registry is not None:
                if self._workspace_identity is None:  # defensive constructor invariant
                    raise ShaderLiveEditError(
                        "persistent live-edit identity callback is unavailable"
                    )
                identity = _persistent_identity(
                    self._workspace_identity(str(requested_root))
                )
                if persistent_entry is not None:
                    expected = self._registry._validate_entry(persistent_entry)
                    if (
                        _workspace_key(Path(expected["workspacePath"])) != key
                        or expected["workspaceIdentity"] != identity
                    ):
                        raise ShaderLiveEditError(
                            "persistent live-edit workspace identity no longer matches"
                        )
                    authorization_id = expected["authorizationId"]
                else:
                    authorization_id = None
                authorization = self._registry.authorize(
                    requested_root,
                    identity,
                    debounce_ms,
                    authorization_id=authorization_id,
                )
            persistent_start = getattr(self.watch_manager, "start_persistent", None)
            if persistent_entry is not None and callable(persistent_start):
                watch_value = persistent_start(workspace_path, debounce_ms)
            else:
                watch_value = self.watch_manager.start(workspace_path, debounce_ms)
            if not isinstance(watch_value, dict):
                raise ShaderLiveEditError("workspace watcher returned a non-object identity")
            watch = copy.deepcopy(watch_value)
            watch_id_value = watch.get("watchId")
            if not isinstance(watch_id_value, str) or _TOKEN.fullmatch(watch_id_value) is None:
                raise ShaderLiveEditError("workspace watcher returned invalid identity")
            watch_id = watch_id_value
            if not isinstance(watch.get("workspacePath"), str):
                raise ShaderLiveEditError("workspace watcher returned an invalid workspace path")
            root = _resolved_workspace(
                watch["workspacePath"], "workspace watcher path"
            )
            if _workspace_key(root) != key:
                raise ShaderLiveEditError("workspace watcher returned a different workspace")
            _validate_watch_baseline(watch)

            live_edit_id = uuid.uuid4().hex
            try:
                job = _LiveEditJob(
                    live_edit_id=live_edit_id,
                    watch_snapshot=watch,
                    watch_manager=self.watch_manager,
                    apply_candidate=self.apply_candidate,
                    on_finished=self._finished,
                    poll_seconds=self.poll_seconds,
                    join_seconds=self.join_seconds,
                )
                with self._lock:
                    self._jobs[live_edit_id] = job
                    self._active_by_workspace[key] = live_edit_id
                    if authorization is not None:
                        self._authorization_by_live_edit[live_edit_id] = str(
                            authorization["authorizationId"]
                        )
                    job.start()
                if self._registry is not None and authorization is not None:
                    authorization = self._registry.authorize(
                        requested_root,
                        authorization["workspaceIdentity"],
                        debounce_ms,
                        authorization_id=authorization["authorizationId"],
                        last_live_edit_id=live_edit_id,
                    )
                return job.snapshot()
            except Exception:
                with self._lock:
                    self._jobs.pop(live_edit_id, None)
                    self._authorization_by_live_edit.pop(live_edit_id, None)
                    if self._active_by_workspace.get(key) == live_edit_id:
                        self._active_by_workspace.pop(key, None)
                raise
        except Exception:
            if _TOKEN.fullmatch(watch_id):
                try:
                    self.watch_manager.stop(watch_id)
                except Exception:
                    pass
            if (
                self._registry is not None
                and authorization is not None
                and not authorization_preexisted
            ):
                try:
                    self._registry.revoke(
                        requested_root, str(authorization["authorizationId"])
                    )
                except Exception:
                    pass
            raise
        finally:
            with self._lock:
                self._starting_workspaces.discard(key)

    def poll(self, live_edit_id: str) -> dict[str, Any]:
        return self._job(live_edit_id).snapshot()

    def save_authorized_source(
        self,
        live_edit_id: str,
        authorization_id: str,
        workspace_id: str,
        expected_raw_sha256: str,
        text: str,
    ) -> dict[str, Any]:
        """Conditionally save while the exact durable live job remains active.

        Holding the manager lock through the job/watcher CAS excludes public
        stop, revoke, prune and shutdown from racing between authorization and
        publication. The watcher job lock independently excludes its terminal
        transition and owns the source transaction lease for the whole CAS.
        """

        if any(
            not isinstance(value, str) or _TOKEN.fullmatch(value) is None
            for value in (live_edit_id, authorization_id, workspace_id)
        ):
            raise ShaderLiveEditError(
                "shader editor live/authorization/workspace identity is invalid"
            )
        with self._lock:
            job = self._jobs.get(live_edit_id)
            if job is None or job.is_terminal():
                raise ShaderLiveEditError(
                    "shader editor live-edit session is not running"
                )
            key = _workspace_key(job.workspace)
            if self._active_by_workspace.get(key) != live_edit_id:
                raise ShaderLiveEditError(
                    "shader editor live-edit session is not active"
                )
            if self._authorization_by_live_edit.get(live_edit_id) != authorization_id:
                raise ShaderLiveEditError(
                    "shader editor authorization does not own this live-edit session"
                )
            if self._registry is None:
                raise ShaderLiveEditError(
                    "persistent shader editor authorization is unavailable"
                )
            matches = [
                entry
                for entry in self._registry.entries()
                if entry["authorizationId"] == authorization_id
                and entry.get("lastLiveEditId") == live_edit_id
                and entry["workspaceIdentity"]["workspaceId"] == workspace_id
                and _workspace_key(Path(entry["workspacePath"])) == key
            ]
            if len(matches) != 1:
                raise ShaderLiveEditError(
                    "shader editor authorization is unavailable or ambiguous"
                )
            result = job.save_source(expected_raw_sha256, text)
            result.update(
                {
                    "liveEditId": live_edit_id,
                    "authorizationId": authorization_id,
                    "workspaceId": workspace_id,
                }
            )
            return result

    def stop(self, live_edit_id: str) -> dict[str, Any]:
        if not isinstance(live_edit_id, str) or not _TOKEN.fullmatch(live_edit_id):
            raise ShaderLiveEditError(
                "live_edit_id must be a 32-character lowercase hex ID"
            )
        with self._lock:
            job = self._jobs.get(live_edit_id)
        if job is None:
            return self._stop_orphaned_persistent_authorization(live_edit_id)
        # Attempt durable revocation first, while always stopping the current
        # in-memory session below.  A persistence error is reported explicitly:
        # it is not safe to claim that restart authorization was removed.
        revoke_error: Exception | None = None
        if self._registry is not None:
            with self._lock:
                authorization_id = self._authorization_by_live_edit.get(live_edit_id)
            try:
                removed = self._registry.revoke(job.workspace, authorization_id)
                if not removed:
                    # ``False`` is safe only when durable state independently
                    # confirms that the workspace has no authorization.  An
                    # authorization-ID mismatch must not be reported as a
                    # successful revoke.
                    matching = [
                        entry
                        for entry in self._registry.entries()
                        if _workspace_key(Path(entry["workspacePath"]))
                        == _workspace_key(job.workspace)
                    ]
                    if matching:
                        raise ShaderLiveEditError(
                            "persistent live-edit authorization was not removed"
                        )
            except Exception as exc:
                revoke_error = exc

        result: dict[str, Any] | None = None
        stop_error: Exception | None = None
        try:
            # A durable-registry error must never leave the current watcher or
            # either in-memory worker running.
            result = job.stop()
        except Exception as exc:
            stop_error = exc

        if revoke_error is None:
            with self._lock:
                self._authorization_by_live_edit.pop(live_edit_id, None)
        else:
            # Keep the authorization ID so an explicit second stop can retry
            # the exact revoke instead of broadening it to the whole workspace.
            job.record_persistent_authorization_revoke_failure(revoke_error)

        if stop_error is not None and revoke_error is not None:
            raise ShaderLiveEditError(
                "persistent live-edit authorization revocation was not confirmed "
                "(%s), and the current live-edit session could not be stopped "
                "cleanly (%s)" % (revoke_error, stop_error)
            ) from stop_error
        if stop_error is not None:
            raise stop_error
        if revoke_error is not None:
            raise ShaderLiveEditError(
                "current live-edit monitoring was stopped, but persistent "
                "authorization revocation was not confirmed; the durable "
                "authorization may still resume after restart: %s" % revoke_error
            ) from revoke_error
        if result is None:  # defensive: exactly one stop result is required
            raise ShaderLiveEditError("live-edit stop returned no result")
        return result

    def _stop_orphaned_persistent_authorization(
        self, live_edit_id: str
    ) -> dict[str, Any]:
        """Revoke a failed-resume authorization with no in-memory job."""

        if self._registry is None:
            raise ShaderLiveEditError("unknown or expired live_edit_id")
        try:
            revoked = self._registry.revoke_unique_last_live_edit_id(live_edit_id)
        except Exception as exc:
            raise ShaderLiveEditError(
                "could not revoke the exact orphaned persistent live-edit "
                "authorization; no authorization was reported revoked: %s" % exc
            ) from exc
        if revoked is None:
            raise ShaderLiveEditError(
                "unknown or expired live_edit_id; no exact persistent authorization matched"
            )
        with self._lock:
            self._authorization_by_live_edit.pop(live_edit_id, None)
        stopped_at = _utc_now()
        return {
            "schema": LIVE_EDIT_SCHEMA,
            "schemaVersion": LIVE_EDIT_VERSION,
            "liveEditId": live_edit_id,
            "state": "stopped",
            "workspacePath": revoked["workspacePath"],
            "watchId": None,
            "stoppedAt": stopped_at,
            "monitoringWasActive": False,
            "orphanedPersistentAuthorization": True,
            "persistentAuthorizationRevoked": True,
            "revokedAuthorizationId": revoked["authorizationId"],
            "rollback": None,
            "message": (
                "No in-memory live-edit job existed; the unique persistent "
                "authorization matching this exact lastLiveEditId was revoked"
            ),
        }

    def migrate_authorization(
        self,
        source_workspace_path: str | Path,
        workspace_path: str | Path,
        expected_source_sha256: str,
    ) -> dict[str, Any]:
        """Move durable consent across one fixed-program toolchain migration.

        This never discovers a successor by scanning and never accepts identity
        fields from the caller.  The process-owned verifier must prove one
        direct migration edge and exact copied source bytes.  A running source
        or target session is rejected so authorization ownership cannot move
        underneath an apply worker.
        """

        if self._registry is None or self._workspace_identity is None:
            raise ShaderLiveEditError("persistent live edit is not enabled")
        if self._workspace_migration_evidence is None:
            raise ShaderLiveEditError(
                "live-edit authorization migration verifier is unavailable"
            )
        expected_hash = str(expected_source_sha256 or "").lower()
        if _SHA256.fullmatch(expected_hash) is None:
            raise ShaderLiveEditError(
                "expected_source_sha256 must be a lowercase SHA-256"
            )
        source_root = _resolved_workspace(
            source_workspace_path, "source_workspace_path"
        )
        target_root = _resolved_workspace(workspace_path, "workspace_path")
        source_key = _workspace_key(source_root)
        target_key = _workspace_key(target_root)
        if source_key == target_key:
            raise ShaderLiveEditError(
                "live-edit authorization migration requires two workspaces"
            )

        reserved = (source_key, target_key)
        with self._lock:
            self._prune_locked()
            for key in reserved:
                active_id = self._active_by_workspace.get(key)
                active = self._jobs.get(active_id) if active_id else None
                if active is not None and not active.is_terminal():
                    raise ShaderLiveEditError(
                        "live-edit authorization cannot migrate while either "
                        "workspace has an active session"
                    )
                if key in self._starting_workspaces:
                    raise ShaderLiveEditError(
                        "live-edit authorization cannot migrate during session start"
                    )
            self._starting_workspaces.update(reserved)

        try:
            source_entry = next(
                (
                    entry
                    for entry in self._registry.entries()
                    if _workspace_key(Path(entry["workspacePath"])) == source_key
                ),
                None,
            )
            if source_entry is None:
                raise ShaderLiveEditError(
                    "source workspace has no persistent live-edit authorization"
                )
            evidence = self._workspace_migration_evidence(
                str(source_root), str(target_root), expected_hash
            )
            if not isinstance(evidence, dict):
                raise ShaderLiveEditError(
                    "workspace migration verifier returned a non-object"
                )
            required = {
                "schema",
                "schemaVersion",
                "sourceWorkspacePath",
                "workspacePath",
                "sourceSha256",
                "sourceWorkspaceIdentity",
                "workspaceIdentity",
                "migrationRecordSha256",
                "sourceCandidatesImported",
                "semanticEquivalenceVerified",
                "evidenceSha256",
            }
            unsigned = dict(evidence)
            declared_evidence_hash = str(unsigned.pop("evidenceSha256", ""))
            try:
                evidence_source_root = Path(
                    str(evidence.get("sourceWorkspacePath", ""))
                ).resolve(strict=True)
                evidence_target_root = Path(
                    str(evidence.get("workspacePath", ""))
                ).resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ShaderLiveEditError(
                    "workspace migration verifier returned invalid paths"
                ) from exc
            source_identity = _persistent_identity(
                evidence.get("sourceWorkspaceIdentity")
            )
            target_identity = _persistent_identity(evidence.get("workspaceIdentity"))
            if (
                set(evidence) != required
                or evidence.get("schema") != AUTHORIZATION_MIGRATION_EVIDENCE_SCHEMA
                or evidence.get("schemaVersion") != 1
                or declared_evidence_hash != _canonical_sha256(unsigned)
                or _workspace_key(evidence_source_root) != source_key
                or _workspace_key(evidence_target_root) != target_key
                or evidence.get("sourceSha256") != expected_hash
                or _SHA256.fullmatch(
                    str(evidence.get("migrationRecordSha256", ""))
                )
                is None
                or evidence.get("sourceCandidatesImported") is not False
                or evidence.get("semanticEquivalenceVerified") is not False
            ):
                raise ShaderLiveEditError(
                    "workspace migration evidence is incomplete or changed"
                )
            if source_entry["workspaceIdentity"] != source_identity:
                raise ShaderLiveEditError(
                    "migration predecessor does not match durable authorization"
                )
            if (
                source_identity["workspaceId"] == target_identity["workspaceId"]
                or _physical_shader_identity(source_identity)
                != _physical_shader_identity(target_identity)
            ):
                raise ShaderLiveEditError(
                    "toolchain migration changed capture or Shader physical identity"
                )
            # Re-resolve the current target through the independent process-owned
            # identity callback before committing the registry CAS.
            if _persistent_identity(
                self._workspace_identity(str(target_root))
            ) != target_identity:
                raise ShaderLiveEditError(
                    "target workspace identity changed after migration verification"
                )
            authorization = self._registry.transfer(
                source_root,
                target_root,
                source_identity,
                target_identity,
                authorization_id=str(source_entry["authorizationId"]),
            )
            return {
                "authorizationMigrated": True,
                "authorizationId": authorization["authorizationId"],
                "sourceWorkspacePath": str(source_root),
                "workspacePath": str(target_root),
                "workspaceIdentity": copy.deepcopy(target_identity),
                "sourceSha256": expected_hash,
                "migrationRecordSha256": evidence["migrationRecordSha256"],
                "debounceMs": authorization["debounceMs"],
                "watchStarted": False,
                "restartResumeAuthorized": True,
                "newSaveRequired": True,
                "semanticEquivalenceVerified": False,
            }
        finally:
            with self._lock:
                for key in reserved:
                    self._starting_workspaces.discard(key)

    def resume_persistent(self) -> dict[str, Any]:
        """Resume every still-authorised workspace once for this host process."""

        if self._registry is None:
            return {"enabled": False, "attempted": False, "results": []}
        with self._lock:
            if self._resume_attempted:
                return {
                    "enabled": True,
                    "attempted": True,
                    "results": copy.deepcopy(self._resume_results),
                }
            self._resume_attempted = True
        results: list[dict[str, Any]] = []
        try:
            entries = self._registry.entries()
        except Exception as exc:
            results.append(
                {
                    "resumed": False,
                    "errorType": type(exc).__name__,
                    "message": _bounded(exc),
                }
            )
            with self._lock:
                self._resume_results = results
            return {"enabled": True, "attempted": True, "results": results}
        for entry in entries:
            workspace_path = str(entry["workspacePath"])
            try:
                started = self._start(
                    workspace_path,
                    int(entry["debounceMs"]),
                    persistent_entry=entry,
                )
                results.append(
                    {
                        "workspacePath": workspace_path,
                        "authorizationId": entry["authorizationId"],
                        "resumed": True,
                        "liveEditId": started["liveEditId"],
                        "watchId": started["watchId"],
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "workspacePath": workspace_path,
                        "authorizationId": entry["authorizationId"],
                        "resumed": False,
                        "errorType": type(exc).__name__,
                        "message": _bounded(exc),
                    }
                )
        with self._lock:
            self._resume_results = copy.deepcopy(results)
        return {"enabled": True, "attempted": True, "results": results}

    def list(self) -> dict[str, Any]:
        with self._lock:
            sessions = [job.snapshot() for job in self._jobs.values()]
            resume_results = copy.deepcopy(self._resume_results)
        authorizations = self._registry.entries() if self._registry is not None else []
        return {
            "persistenceEnabled": self._registry is not None,
            "sessions": sessions,
            "authorizations": authorizations,
            "resumeResults": resume_results,
        }

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.is_terminal():
                job._stop.set()
                try:
                    job._stop_workspace_watcher()
                except Exception:
                    pass
        deadline = time.monotonic() + self.join_seconds
        for job in jobs:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            job._thread.join(remaining)

    def _job(self, live_edit_id: str) -> _LiveEditJob:
        if not isinstance(live_edit_id, str) or not _TOKEN.fullmatch(live_edit_id):
            raise ShaderLiveEditError(
                "live_edit_id must be a 32-character lowercase hex ID"
            )
        with self._lock:
            job = self._jobs.get(live_edit_id)
        if job is None:
            raise ShaderLiveEditError("unknown or expired live_edit_id")
        return job

    def _finished(self, job: _LiveEditJob) -> None:
        key = _workspace_key(job.workspace)
        with self._lock:
            if self._active_by_workspace.get(key) == job.live_edit_id:
                self._active_by_workspace.pop(key, None)

    def _prune_locked(self) -> None:
        terminal = [job for job in self._jobs.values() if job.is_terminal()]
        excess = len(terminal) - MAX_RETAINED_LIVE_EDITS
        if excess <= 0:
            return
        terminal.sort(key=lambda job: job.live_edit_id)
        for job in terminal[:excess]:
            self._jobs.pop(job.live_edit_id, None)


__all__ = [
    "LIVE_EDIT_SCHEMA",
    "LIVE_EDIT_VERSION",
    "LIVE_EDIT_REGISTRY_SCHEMA",
    "LIVE_EDIT_REGISTRY_VERSION",
    "ShaderLiveEditError",
    "ShaderLiveEditManager",
]
