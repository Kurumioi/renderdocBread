"""Persistent-host orchestration for safe ReplayLab project restoration.

The RDC section consumed here is only a disposable routing cache.  Every
Shader is first matched by a bounded, zero-EID target-capture resource index,
then passes the ordinary single-anchor live ABI/apply transaction. CBuffer
names and field aliases are restored from exact ABI/layout knowledge, while
numeric values and material snapshots remain explicit current-EID operations.

This module intentionally exposes synchronous functions. The persistent Host
runs them in its worker pool. Most progress originates on that worker; bounded
legacy export telemetry may arrive on a sidecar reader thread, so `_Progress`
serializes callbacks before the Host forwards them to UI code.
"""

from __future__ import annotations

import contextlib
import base64
import copy
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .projects import MAX_CAPTURE_CATALOG_BYTES, ReplayProjectError, ReplayProjectService
from . import rdc_project_index as _rdc_project_index
from .rdc_project_index import (
    RDC_PROJECT_INDEX_VERSION,
    RDC_PROJECT_INDEX_V2_VERSION,
    RdcProjectIndexError,
    encode_index,
    new_index,
    parse_index,
    select_project_binding,
    upsert_project_binding,
)
from .rdc_shader_inventory_cache import (
    RdcShaderInventoryCacheError,
    load_shader_inventory_cache,
    write_shader_inventory_cache,
)
from .shader_workspace.io import atomic_write_bytes, is_within
from .symbols.scanner import interface_hash, interface_is_complete, layout_hash


RESTORE_SCHEMA = "replaylab-project-restore/v1"
RESTORE_PROGRESS_SCHEMA = "replaylab-project-restore-progress/v1"
SHADER_SYNC_SCHEMA = "replaylab-project-shader-sync/v1"
MATERIAL_SYNC_SCHEMA = "replaylab-project-material-sync/v1"

_RESTORE_GUARD = threading.RLock()
_RESTORE_THREAD_STATE = threading.local()
_MAX_TRACKED_MATERIAL_SYNC_SESSIONS = 128
_MATERIAL_SYNC_PARAMETERS: dict[str, dict[str, Any]] = {}
_SHA256_CHARS = frozenset("0123456789abcdef")
_HEX32_CHARS = _SHA256_CHARS
CATALOG_INDEX_REQUIRED_REASON_ID = "catalog-index-required"
_MAX_FAST_PROJECT_SHADERS = 256
_MAX_TARGET_RAW_SHADER_HASHES = 16
_MAX_FAST_PROJECT_BLOCKS = 4096
_MAX_FAST_CATALOG_SHADERS = 65536
_MAX_FAST_EVIDENCE_USES = 65536
_MAX_FAST_CATALOG_BYTES = 16 * 1024 * 1024
_MAX_FAST_REFLECTION_DEPTH = 24
_MAX_FAST_REFLECTION_FIELDS = 4096
_MAX_FAST_REFLECTION_SIGNATURES = 2048
_MAX_FAST_REFLECTION_BINDINGS = 4096
_MAX_FAST_REFLECTION_SAMPLERS = 2048
_MAX_FAST_REFLECTION_STRING_BYTES = 1024
_MAX_FAST_REFLECTION_TOTAL_STRING_BYTES = 1024 * 1024
_MAX_RESTORE_ALIAS_TARGETS = 256
_MAX_RESTORE_ALIAS_CANDIDATES = 4096
_MAX_RESTORE_ALIASES = 65536
_FAST_INDEX_POLL_SECONDS = 0.025
_FAST_INDEX_TIMEOUT_SECONDS = 15.0


def _is_transient_bridge_poll_timeout(error: BaseException) -> bool:
    """Recognise a mailbox wait delayed by foreground RenderDoc work."""

    message = str(error or "").strip()
    return (
        message.startswith("Timeout (")
        and "Make sure RenderDoc is running with capture loaded" in message
    )


_PREPARE_PROGRESS_MESSAGES = {
    "prepare-export-start": "Starting full-capture Shader export",
    "prepare-export-wait": "Waiting for full-capture Shader export",
    "export-shader-bindings": "Exporting Shader bindings",
    "prepare-export-validate": "Validating the exported Shader catalog",
    "prepare-project-import": "Importing the Shader catalog into the project",
    "prepare-reuse-plan": "Matching project Shader and CBuffer knowledge",
    "prepare-catalog-register": "Registering the active capture catalog",
    "prepare-symbol-resolve": "Resolving project Shader and CBuffer aliases",
    "prepare-complete": "Current capture project preparation completed",
}
_PREPARE_PROGRESS_RANKS = {
    "prepare-export-start": 0,
    "prepare-export-wait": 0,
    "export-shader-bindings": 1,
    "prepare-export-validate": 2,
    "prepare-project-import": 3,
    "prepare-reuse-plan": 4,
    "prepare-catalog-register": 5,
    "prepare-symbol-resolve": 6,
    "prepare-complete": 7,
}


class CatalogPreparationRequired(RuntimeError):
    """The explicit full-capture project index is not available."""

    def __init__(
        self,
        message: str,
        reason_id: str = CATALOG_INDEX_REQUIRED_REASON_ID,
    ) -> None:
        super().__init__(message)
        self.reason_id = str(reason_id or CATALOG_INDEX_REQUIRED_REASON_ID)


class RestoreContextChanged(RuntimeError):
    """The active project/capture lifecycle changed during restoration."""


class RestoreRollbackFailed(RuntimeError):
    """A live Shader replacement could not be transactionally rolled back."""


class RestoreControlStop(RuntimeError):
    """Base class for cooperative cancellation/deadline termination."""


class RestoreCancelled(RestoreControlStop):
    """The owning client or Host requested cooperative cancellation."""


class RestoreDeadlineExceeded(RestoreControlStop):
    """The server-owned monotonic restore budget elapsed."""


def _restore_control_checkpoint(
    cancel_event: Any,
    deadline_monotonic: float | None,
    phase: str,
) -> None:
    """Stop only at transaction-safe boundaries between blocking operations."""

    if cancel_event is not None and cancel_event.is_set():
        raise RestoreCancelled(
            "ReplayLab project restore was cancelled at %s" % phase
        )
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise RestoreDeadlineExceeded(
            "ReplayLab project restore exceeded its server deadline at %s" % phase
        )


def _host_module() -> Any:
    # Lazy import avoids a server -> project_restore -> server cycle when the
    # persistent host imports this module during process start.
    from . import server

    return server


def automatic_project_sync_suppressed() -> bool:
    """Return whether the current worker is already restoring project state."""

    return bool(getattr(_RESTORE_THREAD_STATE, "active", False))


def _material_sync_context(
    session: Mapping[str, Any], project_id: str, project_workspace_id: str
) -> str:
    """Bind accumulated leaf IDs to one exact project/session/block lifecycle."""

    return _canonical_sha256(
        {
            "projectId": _hex(project_id, 32, "material projectId"),
            "projectWorkspaceId": _hex(
                project_workspace_id, 32, "material project workspaceId"
            ),
            "captureId": _hex(session.get("captureId"), 64, "material captureId"),
            "captureSessionId": _hex(
                session.get("captureSessionId"), 32, "material captureSessionId"
            ),
            "eventId": _integer(session.get("eventId"), "material eventId", 1),
            "stage": _text(str(session.get("stage", "")), "material stage", 32),
            "shaderResourceId": _integer(
                session.get("shaderResourceId"), "material shaderResourceId", 1
            ),
            "constantBlockIndex": _integer(
                session.get("constantBlockIndex"), "material constantBlockIndex"
            ),
            "constantBlockPath": _text(
                str(session.get("constantBlockPath", "")),
                "material constantBlockPath",
                2048,
            ),
            "layoutSha256": _hex(
                session.get("layoutSha256"), 64, "material layoutSha256"
            ),
        }
    )


def _remember_material_sync_parameters(
    session_id: str,
    session: Mapping[str, Any],
    project_id: str,
    project_workspace_id: str,
    parameter_ids: Sequence[str],
) -> list[str]:
    """Return the deterministic union of leaves changed in one live session.

    Material knowledge stays append-only, but every newly committed head must
    describe the complete set of edits still represented by the current live
    override.  The context digest prevents a stale/foreign session entry from
    contributing IDs, and the bounded table cannot grow with Host uptime.
    """

    token = _hex(session_id, 32, "material sessionId")
    parameters = list(session.get("parameters", []) or [])
    if not parameters or len(parameters) > 256:
        raise RuntimeError("material session parameters are invalid")
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, Mapping):
            raise RuntimeError("material session parameter[%d] is invalid" % index)
        parameter_id = _hex(
            parameter.get("parameterId"),
            32,
            "material session parameterId",
        )
        if parameter_id in seen:
            raise RuntimeError("material session contains duplicate parameter IDs")
        seen.add(parameter_id)
        ordered_ids.append(parameter_id)
    requested = {
        _hex(value, 32, "material parameterId") for value in parameter_ids
    }
    if not requested or not requested.issubset(seen):
        raise RuntimeError("material parameter IDs are absent from the live session")
    context = _material_sync_context(session, project_id, project_workspace_id)
    previous = _MATERIAL_SYNC_PARAMETERS.pop(token, None)
    if isinstance(previous, Mapping) and previous.get("contextSha256") == context:
        requested.update(list(previous.get("parameterIds", []) or []))
    accumulated = [parameter_id for parameter_id in ordered_ids if parameter_id in requested]
    _MATERIAL_SYNC_PARAMETERS[token] = {
        "contextSha256": context,
        "parameterIds": tuple(accumulated),
    }
    while len(_MATERIAL_SYNC_PARAMETERS) > _MAX_TRACKED_MATERIAL_SYNC_SESSIONS:
        oldest = next(iter(_MATERIAL_SYNC_PARAMETERS))
        _MATERIAL_SYNC_PARAMETERS.pop(oldest, None)
    return accumulated


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _hex(value: Any, length: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) != length or any(character not in _SHA256_CHARS for character in text):
        raise RuntimeError("%s must be %d lowercase hexadecimal characters" % (label, length))
    return text


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError("%s must be an integer >= %d" % (label, minimum))
    return value


def _text(value: Any, label: str, maximum: int = 4096, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise RuntimeError("%s must be bounded text" % label)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum:
        raise RuntimeError("%s must be bounded text" % label)
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _asset_api(value: Any) -> str:
    token = str(value or "").strip().lower().rsplit(".", 1)[-1]
    token = token.replace("_", "").replace(" ", "")
    return {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "gl": "opengl",
        "openglcore": "opengl",
    }.get(token, token)


def _asset_encoding(value: Any) -> str:
    token = str(value or "").strip().lower().rsplit(".", 1)[-1]
    token = token.replace("_", "").replace(" ", "").replace("-", "")
    return {
        "directxbytecode": "dxbc",
        "directxil": "dxil",
        "dxilcontainer": "dxil",
        "spv": "spirv",
        "spirvshader": "spirv",
        "openglspirv": "opengl-spirv",
        "glslshader": "glsl",
        "openglglsl": "glsl",
    }.get(token, token)


def _project_asset_encoding(value: Any) -> str:
    """Return the established project-manifest spelling for one encoding.

    The Bridge protocol and the seven-field stable-key material deliberately
    use lowercase tokens, while the persistent project schema has always used
    display spellings such as ``DXIL`` and ``SPIR-V``.  Keep that representation
    boundary explicit so normalising an otherwise exact live inventory cannot
    make its snapshot compare unequal to the request that produced it.
    """

    protocol = _asset_encoding(value)
    display = {
        "dxbc": "DXBC",
        "dxil": "DXIL",
        "spirv": "SPIR-V",
        "opengl-spirv": "OpenGL-SPIR-V",
        "glsl": "GLSL",
        "metallib": "METALLIB",
    }.get(protocol)
    if display is None:
        raise RuntimeError("Shader encoding is unsupported or not canonicalizable")
    return display


def _asset_format_supported(api: str, encoding: str) -> bool:
    return encoding in {
        "d3d11": {"dxbc"},
        "d3d12": {"dxbc", "dxil"},
        "opengl": {"glsl", "spirv", "opengl-spirv"},
        "vulkan": {"spirv"},
    }.get(api, set())


def _reject_json_constant(value: str) -> None:
    raise RuntimeError("tool wrapper contains non-finite JSON: %s" % value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("tool wrapper contains duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _tool_json(
    payload: Any,
    operation: str,
    *,
    schema: str | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise RuntimeError("%s returned a non-JSON wrapper" % operation)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("%s returned invalid JSON" % operation) from exc
    if not isinstance(value, dict):
        raise RuntimeError("%s returned a non-object JSON wrapper" % operation)
    if value.get("error"):
        raise RuntimeError("%s failed: %s" % (operation, value.get("error")))
    if schema is not None and value.get("schema") != schema:
        raise RuntimeError("%s returned an unsupported schema" % operation)
    if schema_version is not None and value.get("schemaVersion") != schema_version:
        raise RuntimeError("%s returned an unsupported schema version" % operation)
    return value


class _Progress:
    def __init__(
        self,
        operation_id: str,
        callback: Callable[[dict[str, Any]], Any] | None,
        warnings: list[str],
    ):
        if callback is not None and not callable(callback):
            raise TypeError("progress_callback must be callable or None")
        self.operation_id = operation_id
        self.callback = callback
        self.warnings = warnings
        # Full-capture export telemetry is forwarded by a sidecar polling
        # thread while the restore worker owns the physical operation.  Keep
        # callbacks serialized and suppress exact duplicates so a slow UI
        # consumer cannot be entered concurrently or flooded by an unchanged
        # observation.
        self._lock = threading.RLock()
        self._last_signature: tuple[Any, ...] | None = None
        self._callback_available = callback is not None

    @staticmethod
    def _item_from_detail(detail: Mapping[str, Any]) -> str:
        for field in (
            "workspacePath",
            "constantBlockPath",
        ):
            value = detail.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        resource_id = detail.get("resourceId")
        stage = detail.get("stage")
        if isinstance(resource_id, int) and not isinstance(resource_id, bool):
            if isinstance(stage, str) and stage.strip():
                return "Shader %d (%s)" % (resource_id, stage.strip())
            return "Shader %d" % resource_id
        event_id = detail.get("eventId")
        if isinstance(event_id, int) and not isinstance(event_id, bool):
            return "EID %d" % event_id
        return ""

    @staticmethod
    def _safe_item(value: Any) -> str:
        """Return display-only item text without gaining failure authority."""

        if not isinstance(value, str):
            return ""
        clean = value.strip()
        if not clean or any(ord(character) < 0x20 for character in clean):
            return ""
        try:
            encoded = clean.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return ""
        if len(encoded) <= 1024:
            return clean
        return encoded[:1024].decode("utf-8", errors="ignore").rstrip()

    def emit(
        self,
        phase: str,
        message: str,
        *,
        current: int = 0,
        total: int = 0,
        item: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        clean_detail = copy.deepcopy(dict(detail or {}))
        clean_item = self._safe_item(item)
        if not clean_item:
            clean_item = self._safe_item(self._item_from_detail(clean_detail))
        event = {
            "schema": RESTORE_PROGRESS_SCHEMA,
            "schemaVersion": 1,
            "operationId": self.operation_id,
            "phase": _text(phase, "progress.phase", 128),
            "current": _integer(current, "progress.current"),
            "total": _integer(total, "progress.total"),
            "message": _text(message, "progress.message", 1024),
            "item": clean_item,
            "detail": clean_detail,
        }
        if len(event["phase"].encode("utf-8")) > 128:
            raise RuntimeError("progress.phase exceeds 128 UTF-8 bytes")
        if len(event["message"].encode("utf-8")) > 1024:
            raise RuntimeError("progress.message exceeds 1024 UTF-8 bytes")
        if len(event["item"].encode("utf-8")) > 1024:
            raise RuntimeError("progress.item exceeds 1024 UTF-8 bytes")
        if event["total"] and event["current"] > event["total"]:
            raise RuntimeError("progress.current exceeds progress.total")
        signature = (
            event["phase"],
            event["message"],
            event["current"],
            event["total"],
            event["item"],
        )
        with self._lock:
            if signature == self._last_signature:
                return
            self._last_signature = signature
            if not self._callback_available or self.callback is None:
                return
            try:
                self.callback(copy.deepcopy(event))
            except Exception as exc:
                # Progress is advisory. Disable a broken consumer after its
                # first failure instead of retrying it for every Shader/EID.
                self._callback_available = False
                warning = "progress callback failed: %s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                if warning not in self.warnings:
                    self.warnings.append(warning)


def _validate_capture_and_project(
    host: Any,
    project_root: str | Path,
    capture_path: str | Path,
    capture_session_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, str]:
    raw_project = Path(project_root).expanduser()
    raw_capture = Path(capture_path).expanduser()
    if not raw_project.is_absolute() or not raw_capture.is_absolute():
        raise ValueError("project_root and capture_path must be absolute")
    if raw_project.exists() and raw_project.is_symlink():
        raise RuntimeError("project_root must not be a symlink")
    if raw_capture.is_symlink() or not raw_capture.is_file() or raw_capture.suffix.lower() != ".rdc":
        raise RuntimeError("capture_path must be an existing regular non-symlink .rdc")
    project = raw_project.resolve()
    capture = raw_capture.resolve()
    active = host._active_project_service().status()
    if (
        not isinstance(active, dict)
        or active.get("scopeConfigured") is not True
        or _path_key(active.get("projectRoot", "")) != _path_key(project)
    ):
        raise RuntimeError("the requested bound project is not the active ReplayLab project")
    workspace_id = _hex(active.get("workspaceId"), 32, "active workspaceId")
    replay_status = active.get("replayProject")
    paths = active.get("paths")
    if not isinstance(replay_status, dict) or not isinstance(paths, dict):
        raise RuntimeError("active project omitted its replay-project identity")
    replay_id = _hex(replay_status.get("projectId"), 32, "replay projectId")
    replay_path = Path(str(paths.get("replayProject", "") or "")).expanduser()
    if (
        replay_path.is_symlink()
        or not replay_path.is_dir()
        or _path_key(replay_path) != _path_key(replay_status.get("projectPath", ""))
    ):
        raise RuntimeError("active replay-project path is invalid")
    context = host._active_live_context(allow_missing_capture_id=True)
    live_capture = context.get("capture")
    if not isinstance(live_capture, dict):
        raise RuntimeError("RenderDoc current context omitted capture identity")
    live_session = _hex(
        live_capture.get("captureSessionId"), 32, "capture.captureSessionId"
    )
    if capture_session_id is not None:
        expected_session = _hex(capture_session_id, 32, "capture_session_id")
        if expected_session != live_session:
            raise RuntimeError("RenderDoc capture session changed before project restore")
    if _path_key(live_capture.get("path", "")) != _path_key(capture):
        raise RuntimeError("RenderDoc loaded capture differs from capture_path")
    # Validate the snapshot once more through the server's race guard when it
    # is available.  Later native calls repeat path/session checks themselves.
    host._require_same_active_project(active)
    return active, context, project, replay_path.resolve(), live_session


def _read_rdc_index(
    host: Any, capture_path: Path, capture_session_id: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    response = host.ipc.call(
        "read_rdc_project_index",
        {
            "expected_capture_path": str(capture_path),
            "expected_capture_session_id": capture_session_id,
        },
        timeout=30.0,
    )
    if (
        not isinstance(response, dict)
        or response.get("schema") != "replaylab-rdc-project-index-transport/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureSessionId") != capture_session_id
        or _path_key(response.get("capturePath", "")) != _path_key(capture_path)
        or not isinstance(response.get("present"), bool)
    ):
        raise RuntimeError("RenderDoc returned an invalid RDC project-index wrapper")
    if response["present"] is False:
        if response.get("document") is not None:
            raise RuntimeError("absent RDC project index carried a document")
        return response, None
    if response.get("sectionVersion") not in (
        1,
        RDC_PROJECT_INDEX_V2_VERSION,
        RDC_PROJECT_INDEX_VERSION,
    ):
        raise RuntimeError("RDC project-index section version is unsupported")
    document = response.get("document")
    if not isinstance(document, Mapping):
        raise RuntimeError("RDC project-index wrapper omitted its document")
    # Canonical encoding performs the strict field/limit/seal validation even
    # though the Bridge has already parsed the transport JSON.
    clean = parse_index(encode_index(document))
    return response, clean


def _inventory_binding_state(
    snapshot: Mapping[str, Any], live_inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """Adapt a sealed project inventory snapshot to the restore read model."""

    if (
        snapshot.get("schema")
        != "renderdoc-ultra-project-shader-inventory-reuse/v1"
        or snapshot.get("schemaVersion") != 1
        or not isinstance(snapshot.get("inventory"), Mapping)
        or not isinstance(snapshot.get("binding"), Mapping)
        or not isinstance(snapshot.get("knowledge"), Mapping)
    ):
        raise RuntimeError("project returned an invalid Shader inventory snapshot")
    binding = snapshot["binding"]
    knowledge = snapshot["knowledge"]
    project_inventory = snapshot["inventory"]
    live_shaders = live_inventory.get("shaders")
    if not isinstance(live_shaders, list):
        raise RuntimeError("live Shader inventory was lost during project matching")
    capture_shaders = [_normal_shader_identity(item) for item in live_shaders]
    capture_shaders.sort(
        key=lambda item: (
            item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]
        )
    )

    raw_workspaces = knowledge.get("workspaces")
    if not isinstance(raw_workspaces, Mapping):
        raise RuntimeError("project Shader inventory snapshot omitted HLSL knowledge")
    workspaces: dict[str, dict[str, Any]] = {}
    for knowledge_id, value in raw_workspaces.items():
        if not isinstance(value, Mapping):
            raise RuntimeError("project Shader artifact is malformed")
        physical = value.get("physicalIdentity")
        source = value.get("source")
        if not isinstance(physical, Mapping) or not isinstance(source, Mapping):
            raise RuntimeError("project Shader artifact omitted physical identity")
        stable_key = _hex(
            physical.get("stableKeySha256"),
            64,
            "project Shader stableKeySha256",
        )
        source_sha256 = _hex(
            source.get("sha256"), 64, "project Shader sourceSha256"
        )
        workspaces[str(knowledge_id)] = {
            "knowledgeId": str(knowledge_id),
            "api": _asset_api(physical.get("api")),
            "encoding": _asset_encoding(physical.get("encoding")),
            "stage": _text(physical.get("stage"), "project Shader stage", 32),
            "entryPoint": _text(
                physical.get("entryPoint", ""),
                "project Shader entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _hex(
                physical.get("rawSha256"), 64, "project Shader rawSha256"
            ),
            "abiHash": _hex(
                physical.get("abiHash"), 64, "project Shader abiHash"
            ),
            "baselineAbiSha256": _hex(
                physical.get("abiHash"), 64, "project Shader abiHash"
            ),
            "layoutHash": _hex(
                physical.get("layoutHash"), 64, "project Shader layoutHash"
            ),
            "stableKeyComplete": value.get("interfaceComplete") is True,
            "stableKeySha256": stable_key,
            "sourceSha256": source_sha256,
            "source": copy.deepcopy(dict(source)),
            "targetProfile": copy.deepcopy(value.get("targetProfile")),
            "compiledCandidate": copy.deepcopy(value.get("compiledCandidate")),
            "compilerBinding": copy.deepcopy(value.get("compilerBinding")),
            "assemblyDisassemblyArtifacts": copy.deepcopy(
                value.get("assemblyDisassemblyArtifacts", [])
            ),
            "cbufferAliasRefs": copy.deepcopy(
                value.get("cbufferAliasRefs", [])
            ),
        }
    raw_materials = knowledge.get("materials")
    if not isinstance(raw_materials, Mapping) or raw_materials:
        raise RuntimeError(
            "resource inventory snapshot attempted event-scoped material reuse"
        )
    return {
        "projectId": binding.get("projectId"),
        "projectPath": snapshot.get("projectPath"),
        "scopeSha256": binding.get("scopeSha256"),
        "captureId": binding.get("captureId"),
        "catalogSha256": binding.get("catalogSha256"),
        "inventorySha256": binding.get("inventorySha256"),
        "knowledgeContentSha256": binding.get("knowledgeContentSha256"),
        "matcherPolicySha256": binding.get("matcherPolicySha256"),
        "capture": {
            "captureId": binding.get("captureId"),
            "catalogSha256": binding.get("catalogSha256"),
            "api": project_inventory.get("capture", {}).get("api"),
            "inventoryOnly": True,
            "shaders": capture_shaders,
        },
        "workspaces": workspaces,
        "materials": {},
        "reusePlan": copy.deepcopy(snapshot.get("reusePlan")),
        "inventoryBinding": copy.deepcopy(dict(binding)),
        "projectInventory": copy.deepcopy(dict(project_inventory)),
    }


def _project_binding_state(
    replay: ReplayProjectService,
    binding: Mapping[str, Any],
    live_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if live_inventory is None:
        state = replay.restore_knowledge_snapshot(
            str(binding.get("captureId", "")),
            str(binding.get("catalogSha256", "")),
        )
    else:
        project_inventory = _project_shader_inventory(live_inventory)
        snapshot = replay.reuse_snapshot_for_shader_inventory(project_inventory)
        state = _inventory_binding_state(snapshot, live_inventory)
    for field in (
        "projectId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        expected_length = 32 if field == "projectId" else 64
        _hex(state.get(field), expected_length, "project binding %s" % field)
        if binding.get(field) != state[field]:
            raise RuntimeError("RDC project binding %s is stale" % field)
    return state


def _capture_shader(
    state: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any]:
    capture = state.get("capture")
    shaders = capture.get("shaders") if isinstance(capture, Mapping) else None
    if not isinstance(shaders, list):
        raise RuntimeError("project binding capture omitted Shader identities")
    wanted = _normal_shader_identity(identity)
    matches: list[dict[str, Any]] = []
    for item in shaders:
        if not isinstance(item, Mapping):
            continue
        try:
            candidate = _normal_shader_identity(item)
        except Exception:
            continue
        if wanted.get("stableKeyComplete") is True:
            if candidate.get("stableKeyComplete") is True:
                matched = bool(
                    candidate.get("stableKeySha256")
                    == wanted.get("stableKeySha256")
                    and candidate.get("resourceId") == wanted.get("resourceId")
                )
            else:
                # Project catalog revisions predate the v2 stable-key fields.
                # They still bind the exact capture-local owner and reflection
                # layout. The strict catalog projection plus the expected-only
                # live job below must reproduce the missing physical ABI/stable
                # key before this compatibility match can reach an apply.
                matched = all(
                    candidate.get(field) == wanted.get(field)
                    for field in (
                        "resourceId",
                        "stage",
                        "entryPoint",
                        "rawSha256",
                        "layoutHash",
                        "anchorEventId",
                    )
                )
        else:
            # Legacy project revisions remain readable as routing hints, but
            # their incomplete keys are never upgraded into executable
            # authority. Keep their comparison exact and capture-local.
            matched = all(
                candidate.get(field) == wanted.get(field)
                for field in (
                    "resourceId",
                    "stage",
                    "entryPoint",
                    "rawSha256",
                    "abiHash",
                    "layoutHash",
                )
            )
        if matched:
            matches.append(dict(item))
    if len(matches) != 1:
        raise RuntimeError("RDC Shader association differs from the exact project catalog")
    expected_anchor = _normal_shader_identity(matches[0]).get("anchorEventId")
    if wanted.get("anchorEventId") != expected_anchor:
        raise RuntimeError(
            "RDC Shader representative EID differs from the project inventory"
        )
    return dict(matches[0])


def _resolve_workspace_path(
    project_root: Path, shader_root: Path, relative_path: str
) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise RuntimeError("managed workspace path is not project-relative")
    cursor = project_root
    for component in pure.parts:
        cursor = cursor / component
        if cursor.exists() and cursor.is_symlink():
            raise RuntimeError("managed workspace path traverses a symlink")
    workspace = project_root.joinpath(*pure.parts).resolve()
    if not workspace.is_dir() or not is_within(shader_root, workspace):
        raise RuntimeError("managed workspace escaped the active Shader workspace root")
    if workspace.relative_to(project_root).as_posix() != relative_path:
        raise RuntimeError("managed workspace path is not canonical")
    return workspace


def _validate_workspace_record(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    binding_state: Mapping[str, Any],
    record: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    identity = record.get("identity")
    managed = record.get("managedWorkspace")
    if not isinstance(identity, Mapping) or not isinstance(managed, Mapping):
        raise RuntimeError("RDC Shader association is incomplete")
    _capture_shader(binding_state, identity)
    shader_root = Path(str(active.get("paths", {}).get("shaderWorkspaces", ""))).resolve()
    if shader_root.is_symlink() or not shader_root.is_dir():
        raise RuntimeError("active Shader workspace root is unavailable")
    workspace = _resolve_workspace_path(
        project_root, shader_root, str(managed.get("path", ""))
    )
    status = host._shader_workspace_service().status(str(workspace))
    capture = status.get("capture") if isinstance(status, Mapping) else None
    shader = status.get("shader") if isinstance(status, Mapping) else None
    source = status.get("source") if isinstance(status, Mapping) else None
    latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
    inventory_binding = bool(
        binding_state.get("capture", {}).get("inventoryOnly")
        if isinstance(binding_state.get("capture"), Mapping)
        else False
    )
    if (
        not isinstance(status, dict)
        or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"
        or _path_key(status.get("workspacePath", "")) != _path_key(workspace)
        or status.get("workspaceId") != managed.get("workspaceId")
        or not isinstance(capture, Mapping)
        or not isinstance(shader, Mapping)
        or not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or (
            not inventory_binding
            and capture.get("captureId") != binding_state.get("captureId")
        )
        or (
            not inventory_binding
            and capture.get("catalogSha256") != binding_state.get("catalogSha256")
        )
        or _path_key(capture.get("capturePath", "")) != _path_key(capture_path)
        or shader.get("resourceId") != identity.get("resourceId")
        or status.get("stage") != identity.get("stage")
        or status.get("entryPoint", "") != identity.get("entryPoint", "")
        or shader.get("rawSha256") != identity.get("rawSha256")
        or source.get("present") is not True
        or source.get("currentSha256") != record.get("sourceSha256")
        or latest.get("revision") != record.get("candidateRevision")
        or latest.get("sourceSha256") != record.get("sourceSha256")
        or latest.get("rawSha256") != record.get("candidateRawSha256")
    ):
        raise RuntimeError("managed Shader workspace changed since the RDC index was written")
    knowledge = binding_state.get("workspaces", {}).get(
        str(record.get("knowledgeId", ""))
    )
    artifact_refs = record.get("artifactRefs")
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("RDC Shader association references absent project knowledge")
    if (
        knowledge.get("sourceSha256") != record.get("sourceSha256")
        or knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
        or not _knowledge_matches_shader(knowledge, identity, executable=True)
    ):
        raise RuntimeError("RDC Shader association differs from project knowledge")
    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        compiled = knowledge.get("compiledCandidate")
        compiler_binding = knowledge.get("compilerBinding")
        target_profile = knowledge.get("targetProfile")
        representations = knowledge.get("assemblyDisassemblyArtifacts", [])
        alias_refs = knowledge.get("cbufferAliasRefs", [])
        expected_artifact_refs = {
            "projectArtifactId": str(record.get("knowledgeId", "")),
            "revision": record.get("candidateRevision"),
            "compiledSha256": (
                compiled.get("rawSha256")
                if isinstance(compiled, Mapping)
                else ""
            ),
            "compileConfigSha256": _canonical_sha256(
                compiler_binding
                if isinstance(compiler_binding, Mapping)
                else target_profile
            ),
            "representationsSha256": (
                _canonical_sha256(representations)
                if isinstance(representations, list) and representations
                else ""
            ),
            "aliasesSha256": (
                _canonical_sha256(alias_refs)
                if isinstance(alias_refs, list) and alias_refs
                else ""
            ),
        }
        if (
            not isinstance(artifact_refs, Mapping)
            or dict(artifact_refs) != expected_artifact_refs
        ):
            raise RuntimeError(
                "RDC Shader artifact references differ from the project library"
            )
    return workspace, status


def _physical_enum(value: Any) -> str:
    return (
        str(value or "").strip().lower().rsplit(".", 1)[-1]
        .replace("_", "")
        .replace(" ", "")
    )


def _physical_base_type(value: Any) -> str:
    token = _physical_enum(value)
    return {
        "float32": "float",
        "single": "float",
        "sint": "int",
        "signedint": "int",
        "int32": "int",
        "unsignedint": "uint",
        "uint32": "uint",
        "float16": "half",
        "min16float": "half",
        "float64": "double",
    }.get(token, token)


def _new_fast_reflection_budget() -> dict[str, int]:
    return {
        "fields": 0,
        "signatures": 0,
        "bindings": 0,
        "samplers": 0,
        "stringBytes": 0,
    }


def _fast_reflection_charge(
    budget: dict[str, int], key: str, maximum: int, reason_id: str, label: str
) -> None:
    budget[key] = int(budget.get(key, 0)) + 1
    if budget[key] > maximum:
        raise CatalogPreparationRequired(
            "Shader catalog exceeds the bounded %s budget" % label, reason_id
        )


def _fast_reflection_text(value: Any, budget: dict[str, int]) -> str:
    text = str(value or "")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise CatalogPreparationRequired(
            "Shader catalog contains invalid Unicode text",
            "shader-resource-index-reflection-string-limit",
        ) from exc
    if size > _MAX_FAST_REFLECTION_STRING_BYTES:
        raise CatalogPreparationRequired(
            "Shader catalog contains an oversized reflection string",
            "shader-resource-index-reflection-string-limit",
        )
    budget["stringBytes"] = int(budget.get("stringBytes", 0)) + size
    if budget["stringBytes"] > _MAX_FAST_REFLECTION_TOTAL_STRING_BYTES:
        raise CatalogPreparationRequired(
            "Shader catalog reflection text exceeds the aggregate budget",
            "shader-resource-index-reflection-string-limit",
        )
    return text


def _validate_fast_reflection_fields(
    values: Any, budget: dict[str, int], depth: int = 0
) -> None:
    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    if not isinstance(values, list):
        return
    for value in values:
        if not isinstance(value, Mapping):
            continue
        _fast_reflection_charge(
            budget,
            "fields",
            _MAX_FAST_REFLECTION_FIELDS,
            "shader-resource-index-reflection-field-limit",
            "field-count",
        )
        for text in (
            value.get("name", ""),
            value.get("path", ""),
            value.get("type", ""),
            value.get("baseType", ""),
        ):
            _fast_reflection_text(text, budget)
        elements = value.get("elements", 1)
        if (
            isinstance(elements, bool)
            or not isinstance(elements, int)
            or elements < 0
            or elements > (1 << 20)
        ):
            raise CatalogPreparationRequired(
                "Shader catalog contains an invalid array element count",
                "shader-resource-index-reflection-field-limit",
            )
        _validate_fast_reflection_fields(
            value.get("members", value.get("fields", [])), budget, depth + 1
        )


def _validate_fast_catalog_shader_budget(shader: Mapping[str, Any]) -> None:
    """Reject pathological reflection before scanner hashing can recurse/expand."""

    budget = _new_fast_reflection_budget()
    for value in (
        shader.get("stage", ""),
        shader.get("entryPoint", shader.get("entry", "")),
        shader.get("encoding", shader.get("binaryFormat", "")),
        shader.get("outputTopology", ""),
    ):
        _fast_reflection_text(value, budget)

    for key in ("inputSignature", "outputSignature"):
        values = shader.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "signatures",
                _MAX_FAST_REFLECTION_SIGNATURES,
                "shader-resource-index-reflection-signature-limit",
                "signature-count",
            )
            for text in (
                value.get("varName", ""),
                value.get("semanticName", value.get("semantic", "")),
                value.get("semanticIdxName", ""),
                value.get("systemValue", ""),
                value.get("compType", value.get("componentType", "")),
            ):
                _fast_reflection_text(text, budget)

    for key in ("readOnlyResources", "readWriteResources"):
        values = shader.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "bindings",
                _MAX_FAST_REFLECTION_BINDINGS,
                "shader-resource-index-reflection-binding-limit",
                "resource-binding-count",
            )
            for text in (
                value.get("name", ""),
                value.get("descriptorType", value.get("resourceType", "")),
                value.get("textureType", ""),
            ):
                _fast_reflection_text(text, budget)
            variable_type = value.get("variableType")
            if isinstance(variable_type, Mapping):
                _fast_reflection_text(variable_type.get("name", ""), budget)
                _fast_reflection_text(variable_type.get("baseType", ""), budget)
                _validate_fast_reflection_fields(
                    variable_type.get("members", variable_type.get("fields", [])),
                    budget,
                )

    samplers = shader.get("samplers", [])
    if isinstance(samplers, list):
        for value in samplers:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "samplers",
                _MAX_FAST_REFLECTION_SAMPLERS,
                "shader-resource-index-reflection-sampler-limit",
                "sampler-count",
            )
            _fast_reflection_text(value.get("name", ""), budget)

    blocks = shader.get("constantBlocks", shader.get("constantBuffers", []))
    if isinstance(blocks, list):
        if len(blocks) > _MAX_FAST_PROJECT_BLOCKS:
            raise CatalogPreparationRequired(
                "Shader catalog exceeds the bounded CBuffer budget",
                "shader-resource-index-block-limit",
            )
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            _fast_reflection_text(block.get("name", ""), budget)
            _validate_fast_reflection_fields(
                block.get("fields", block.get("variables", [])), budget
            )


def _physical_field(
    value: Mapping[str, Any],
    budget: dict[str, int] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    if budget is None:
        budget = _new_fast_reflection_budget()
    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    _fast_reflection_charge(
        budget,
        "fields",
        _MAX_FAST_REFLECTION_FIELDS,
        "shader-resource-index-reflection-field-limit",
        "field-count",
    )
    return {
        "byteOffset": int(value.get("byteOffset", value.get("offset", 0)) or 0),
        "byteSize": int(value.get("byteSize", 0) or 0),
        "baseType": _physical_base_type(value.get("baseType", value.get("type", ""))),
        "rows": int(value.get("rows", 0) or 0),
        "columns": int(value.get("columns", value.get("cols", 0)) or 0),
        "elements": int(value.get("elements", 0) or 0),
        "arrayByteStride": int(
            value.get("arrayByteStride", value.get("arrayStride", 0)) or 0
        ),
        "matrixByteStride": int(
            value.get("matrixByteStride", value.get("matrixStride", 0)) or 0
        ),
        "rowMajor": bool(value.get("rowMajor", False)),
        "flags": int(value.get("flags", 0) or 0),
        "pointerTypeID": int(value.get("pointerTypeID", 0xFFFFFFFF)),
        "bitFieldOffset": int(value.get("bitFieldOffset", 0) or 0),
        "bitFieldSize": int(value.get("bitFieldSize", 0) or 0),
        "members": sorted(
            [
                _physical_field(item, budget, depth + 1)
                for item in list(value.get("members", value.get("fields", [])) or [])
                if isinstance(item, Mapping)
            ],
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
    }


def _catalog_block_projection(
    value: Mapping[str, Any],
    position: int,
    stage: str,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    if budget is None:
        budget = _new_fast_reflection_budget()
    index = _integer(value.get("index", position), "constant block index")
    name = str(value.get("name", "") or "")
    path = name or "cbuffer%d" % index
    space = _integer(
        value.get("space", value.get("registerSpace", 0)), "constant block space"
    )
    bind = _integer(
        value.get("bind", value.get("bindNumber", index)), "constant block bind"
    )
    byte_size = _integer(
        value.get("byteSize", value.get("size", 0)),
        "constant block byteSize",
        1,
    )
    fields = list(value.get("fields", value.get("variables", [])) or [])
    if not all(isinstance(item, Mapping) for item in fields):
        raise RuntimeError("constant block fields are invalid")
    completeness = str(value.get("layoutCompleteness", "") or "").strip().lower()
    if completeness not in {"complete", "partial", "opaque", "ambiguous"}:
        completeness = "complete" if fields else "opaque"
    canonical_block = {
        "index": index,
        "name": name,
        "bind": bind,
        "space": space,
        "bindArraySize": _integer(
            value.get("bindArraySize", value.get("arraySize", 1)),
            "constant block bindArraySize",
            1,
        ),
        "byteSize": byte_size,
        "bufferBacked": bool(value.get("bufferBacked", True)),
        "inlineDataBytes": bool(value.get("inlineDataBytes", False)),
        "compileConstants": bool(value.get("compileConstants", False)),
        "fields": copy.deepcopy(fields),
        "layoutCompleteness": completeness,
    }
    physical_block = {
        "space": space,
        "bind": bind,
        "bindArraySize": canonical_block["bindArraySize"],
        "byteSize": byte_size,
        "bufferBacked": canonical_block["bufferBacked"],
        "inlineDataBytes": canonical_block["inlineDataBytes"],
        "compileConstants": canonical_block["compileConstants"],
        "fields": sorted(
            [_physical_field(item, budget) for item in fields],
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
        "layoutCompleteness": completeness,
    }
    return {
        "space": space,
        "bind": bind,
        "index": index,
        "path": path,
        "byteSize": byte_size,
        "layoutSha256": _canonical_sha256(
            {
                "schema": "renderdoc-ultra-event-cbuffer-layout/v1",
                "stage": stage,
                "block": canonical_block,
            }
        ),
        "abiSha256": _canonical_sha256(physical_block),
    }


def _catalog_inventory_leaf_base_type(value: Any) -> str:
    token = _physical_enum(value)
    return {
        "float": "Float",
        "float32": "Float",
        "single": "Float",
        "sint": "SInt",
        "int": "SInt",
        "int32": "SInt",
        "signedint": "SInt",
        "uint": "UInt",
        "uint32": "UInt",
        "unsignedint": "UInt",
        "double": "Double",
        "float64": "Double",
        "half": "Half",
        "float16": "Half",
        "min16float": "Half",
        "bool": "Bool",
        "boolean": "Bool",
        "slong": "SLong",
        "long": "SLong",
        "int64": "SLong",
        "sint64": "SLong",
        "ulong": "ULong",
        "uint64": "ULong",
        "byte": "Byte",
        "ubyte": "UByte",
        "short": "Short",
        "ushort": "UShort",
    }.get(token, token or "unknown")


def _catalog_inventory_fields(
    values: Any,
    budget: dict[str, int],
    *,
    prefix: str = "",
    parent_offset: int = 0,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Project catalog CBuffer leaves exactly as the live Bridge inventory."""

    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    if not isinstance(values, list) or not all(
        isinstance(value, Mapping) for value in values
    ):
        raise CatalogPreparationRequired(
            "Shader catalog CBuffer fields are malformed",
            "shader-resource-index-interface-mismatch",
        )
    leaves: list[dict[str, Any]] = []
    for position, value in enumerate(values):
        _fast_reflection_charge(
            budget,
            "fields",
            _MAX_FAST_REFLECTION_FIELDS,
            "shader-resource-index-reflection-field-limit",
            "field-count",
        )
        raw_name = str(value.get("name", "") or "")
        name = raw_name or "field%d" % position
        _fast_reflection_text(name, budget)
        canonical_path = name if not prefix else "%s.%s" % (prefix, name)
        _fast_reflection_text(canonical_path, budget)
        local_offset = _integer(
            value.get("byteOffset", value.get("offset", 0)),
            "CBuffer field byteOffset",
        )
        absolute_offset = parent_offset + local_offset
        members = value.get("members", value.get("fields", []))
        if not isinstance(members, list) or not all(
            isinstance(member, Mapping) for member in members
        ):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer members are malformed",
                "shader-resource-index-interface-mismatch",
            )
        if members:
            leaves.extend(
                _catalog_inventory_fields(
                    members,
                    budget,
                    prefix=canonical_path,
                    parent_offset=absolute_offset,
                    depth=depth + 1,
                )
            )
            continue
        row_major = value.get("rowMajor", False)
        if not isinstance(row_major, bool):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer row-major evidence is malformed",
                "shader-resource-index-interface-mismatch",
            )
        leaves.append(
            {
                "canonicalPath": canonical_path,
                "name": name,
                "absoluteByteOffset": absolute_offset,
                "byteSize": _integer(
                    value.get("byteSize", 0), "CBuffer field byteSize", 1
                ),
                "baseType": _catalog_inventory_leaf_base_type(
                    value.get("baseType", value.get("type", ""))
                ),
                "rows": _integer(value.get("rows", 0), "CBuffer field rows"),
                "columns": _integer(
                    value.get("columns", value.get("cols", 0)),
                    "CBuffer field columns",
                ),
                "elements": _integer(
                    value.get("elements", 0), "CBuffer field elements"
                ),
                "arrayByteStride": _integer(
                    value.get("arrayByteStride", value.get("arrayStride", 0)),
                    "CBuffer field arrayByteStride",
                ),
                "matrixByteStride": _integer(
                    value.get("matrixByteStride", value.get("matrixStride", 0)),
                    "CBuffer field matrixByteStride",
                ),
                "rowMajor": row_major,
                "flags": _integer(value.get("flags", 0), "CBuffer field flags"),
                "pointerTypeID": _integer(
                    value.get("pointerTypeID", 0xFFFFFFFF),
                    "CBuffer field pointerTypeID",
                ),
                "bitFieldOffset": _integer(
                    value.get("bitFieldOffset", 0), "CBuffer field bitFieldOffset"
                ),
                "bitFieldSize": _integer(
                    value.get("bitFieldSize", 0), "CBuffer field bitFieldSize"
                ),
            }
        )
    leaves.sort(
        key=lambda item: (
            item["absoluteByteOffset"],
            item["canonicalPath"],
            item["name"],
            item["byteSize"],
            item["baseType"],
            item["rows"],
            item["columns"],
            item["elements"],
            item["arrayByteStride"],
            item["matrixByteStride"],
            item["rowMajor"],
            item["flags"],
            item["pointerTypeID"],
            item["bitFieldOffset"],
            item["bitFieldSize"],
        )
    )
    return leaves


def _strict_fast_catalog(
    catalog_path: str | Path, expected_sha256: str
) -> dict[str, Any]:
    path = Path(catalog_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("active capture catalog is unavailable")
    before = path.stat()
    if before.st_size <= 0:
        raise RuntimeError("active capture catalog is empty")
    if before.st_size > min(MAX_CAPTURE_CATALOG_BYTES, _MAX_FAST_CATALOG_BYTES):
        raise CatalogPreparationRequired(
            "active capture catalog exceeds the bounded resource-index budget",
            "shader-resource-index-catalog-limit",
        )
    raw = path.read_bytes()
    after = path.stat()
    if (
        len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("active capture catalog changed while it was read")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("active capture catalog hash changed")
    try:
        value = json.loads(
            raw.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("active capture catalog is not strict UTF-8 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "renderdoc-ultra-capture-symbols/v1"
        or value.get("schemaVersion") != 1
        or not isinstance(value.get("shaders"), list)
    ):
        raise RuntimeError("active capture catalog schema is unsupported")
    if len(value["shaders"]) > _MAX_FAST_CATALOG_SHADERS:
        raise CatalogPreparationRequired(
            "active capture catalog exceeds the bounded Shader-count budget",
            "shader-resource-index-catalog-shader-limit",
        )
    return value


def _fast_catalog_shader_index(
    catalog: Mapping[str, Any],
) -> dict[tuple[Any, str, str, str], list[Mapping[str, Any]]]:
    result: dict[tuple[Any, str, str, str], list[Mapping[str, Any]]] = {}
    for item in list(catalog.get("shaders", []) or []):
        if not isinstance(item, Mapping):
            continue
        key = (
            item.get("shaderResourceId", item.get("resourceId")),
            str(item.get("stage", "") or ""),
            str(item.get("entryPoint", item.get("entry", "")) or ""),
            str(item.get("rawSha256", "") or "").lower(),
        )
        result.setdefault(key, []).append(item)
    return result


def _expected_resource_shader(
    catalog: Mapping[str, Any],
    identity: Mapping[str, Any],
    api: str,
    anchor_event_id: int,
    shader_index: Mapping[
        tuple[Any, str, str, str], Sequence[Mapping[str, Any]]
    ] | None = None,
    *,
    inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    index = shader_index if shader_index is not None else _fast_catalog_shader_index(catalog)
    key = (
        identity.get("resourceId"),
        str(identity.get("stage", "") or ""),
        str(identity.get("entryPoint", "") or ""),
        str(identity.get("rawSha256", "") or "").lower(),
    )
    matches = list(index.get(key, []))
    if len(matches) != 1:
        raise CatalogPreparationRequired(
            "active catalog has no unique content-addressed Shader record",
            "shader-resource-index-ambiguous" if len(matches) > 1 else
            "shader-resource-index-mismatch",
        )
    shader = dict(matches[0])
    _validate_fast_catalog_shader_budget(shader)
    computed_interface = interface_hash(shader)
    computed_layout = layout_hash(shader)
    computed_complete = interface_is_complete(shader)
    if (
        str(shader.get("interfaceHash", "") or "") not in ("", computed_interface)
        or str(shader.get("layoutHash", "") or "") not in ("", computed_layout)
    ):
        raise CatalogPreparationRequired(
            "Shader interface/layout digest differs from the indexed catalog",
            "shader-resource-index-interface-mismatch",
        )
    encoding = _asset_encoding(
        shader.get("encoding", shader.get("binaryFormat", shader.get("format", "")))
    )
    if not encoding:
        raise CatalogPreparationRequired(
            "Shader format is absent from the indexed catalog",
            "shader-resource-index-format-unsupported",
        )
    if not _asset_format_supported(api, encoding):
        raise CatalogPreparationRequired(
            "Shader format is unsupported for the active capture API",
            "shader-resource-index-format-unsupported",
        )
    blocks_value = shader.get("constantBlocks", shader.get("constantBuffers", []))
    if not isinstance(blocks_value, list):
        raise CatalogPreparationRequired(
            "Shader constant-block set is unavailable",
            "shader-resource-index-interface-mismatch",
        )
    if len(blocks_value) > _MAX_FAST_PROJECT_BLOCKS:
        raise CatalogPreparationRequired(
            "Shader constant-block set exceeds the fast-index limit",
            "shader-resource-index-block-limit",
        )
    projection_budget = _new_fast_reflection_budget()
    blocks = [
        _catalog_block_projection(
            block, index, str(identity["stage"]), projection_budget
        )
        for index, block in enumerate(blocks_value)
        if isinstance(block, Mapping)
    ]
    if len(blocks) != len(blocks_value):
        raise CatalogPreparationRequired(
            "Shader constant-block set is malformed",
            "shader-resource-index-interface-mismatch",
        )
    blocks.sort(
        key=lambda item: (
            item["space"], item["bind"], item["index"], item["path"]
        )
    )
    block_keys = [
        (item["space"], item["bind"], item["index"], item["path"])
        for item in blocks
    ]
    if len(set(block_keys)) != len(block_keys):
        raise CatalogPreparationRequired(
            "Shader constant-block ownership is ambiguous",
            "shader-resource-index-ambiguous",
        )
    expected = {
        "resourceId": _integer(identity.get("resourceId"), "Shader resourceId", 1),
        "stage": str(identity["stage"]),
        "entryPoint": str(identity.get("entryPoint", "") or ""),
        "encoding": encoding,
        "rawSha256": _hex(identity.get("rawSha256"), 64, "Shader rawSha256"),
        "interfaceHash": computed_interface,
        "layoutHash": computed_layout,
        "interfaceComplete": computed_complete,
        "constantBlocks": blocks,
        "anchorEventId": _integer(anchor_event_id, "Shader anchorEventId", 1),
    }
    has_v2_identity = any(
        field in identity
        for field in (
            "api",
            "encoding",
            "abiHash",
            "stableKeyComplete",
            "stableKeySha256",
            "anchorEventId",
        )
    )
    if not has_v2_identity:
        # Legacy v1 identities retain their original reflection fields and may
        # authorise knowledge-only CBuffer rebinding after an exact catalog
        # check. They are never promoted to executable stable-key authority.
        if (
            identity.get("interfaceHash", "") != computed_interface
            or identity.get("layoutHash", "") != computed_layout
            or (identity.get("interfaceComplete") is True) != computed_complete
        ):
            raise CatalogPreparationRequired(
                "legacy Shader interface/layout digest differs from the indexed catalog",
                "shader-resource-index-interface-mismatch",
            )
        return expected

    normal_identity = _normal_shader_identity(identity)
    if normal_identity.get("stableKeyComplete") is not True:
        raise CatalogPreparationRequired(
            "v2 Shader identity has no complete stable key for live CBuffer rebinding",
            "shader-resource-index-key-incomplete",
        )
    if (
        normal_identity.get("api") != api
        or normal_identity.get("encoding") != encoding
        or normal_identity.get("layoutHash") != computed_layout
        or normal_identity.get("anchorEventId") != anchor_event_id
    ):
        raise CatalogPreparationRequired(
            "v2 Shader identity differs from the exact indexed catalog",
            "shader-resource-index-interface-mismatch",
        )
    expected.update(
        {
            "api": normal_identity["api"],
            "abiHash": normal_identity["abiHash"],
            "stableKeyComplete": True,
            "stableKeySha256": normal_identity["stableKeySha256"],
        }
    )
    if inventory is None:
        # This is request material only. It cannot authorise an apply until
        # `_resource_index_job` has freshly reproduced every field from the
        # live target RDC and committed its capture-session lease.
        return expected
    inventory_assets = (
        inventory.get("shaders") if isinstance(inventory, Mapping) else None
    )
    if not isinstance(inventory_assets, list):
        raise CatalogPreparationRequired(
            "v2 Shader identity has no validated live inventory evidence",
            "shader-resource-index-invalid-response",
        )
    identity_fields = (
        "api",
        "encoding",
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "abiHash",
        "layoutHash",
        "stableKeySha256",
        "anchorEventId",
    )
    live_matches: list[Mapping[str, Any]] = []
    for asset in inventory_assets:
        if not isinstance(asset, Mapping):
            continue
        try:
            live_identity = _normal_shader_identity(asset)
        except Exception:
            continue
        if (
            live_identity.get("stableKeyComplete") is True
            and all(
                live_identity.get(field) == normal_identity.get(field)
                for field in identity_fields
            )
        ):
            live_matches.append(asset)
    if len(live_matches) != 1:
        raise CatalogPreparationRequired(
            "v2 Shader identity has no unique exact live inventory match",
            (
                "shader-resource-index-ambiguous"
                if len(live_matches) > 1
                else "shader-resource-index-mismatch"
            ),
        )
    live = live_matches[0]
    inventory_blocks: list[dict[str, Any]] = []
    block_projection_budget = _new_fast_reflection_budget()
    field_projection_budget = _new_fast_reflection_budget()
    for position, raw_block in enumerate(blocks_value):
        core = _catalog_block_projection(
            raw_block,
            position,
            str(identity["stage"]),
            block_projection_budget,
        )
        fields = _catalog_inventory_fields(
            raw_block.get("fields", raw_block.get("variables", [])),
            field_projection_budget,
        )
        if len({field["canonicalPath"] for field in fields}) != len(fields):
            raise CatalogPreparationRequired(
                "Shader catalog contains duplicate CBuffer field paths",
                "shader-resource-index-ambiguous",
            )
        if any(
            field["absoluteByteOffset"] + field["byteSize"] > core["byteSize"]
            for field in fields
        ):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer field exceeds its block",
                "shader-resource-index-interface-mismatch",
            )
        name = str(raw_block.get("name", "") or "").strip()
        inventory_blocks.append(
            {
                **core,
                "name": name or core["path"],
                "fields": fields,
            }
        )
    inventory_blocks.sort(
        key=lambda item: (
            item["space"], item["bind"], item["index"], item["path"]
        )
    )
    if (
        normal_identity.get("api") != api
        or normal_identity.get("encoding") != encoding
        or live.get("exactExecutableMatchEligible") is not True
        or live.get("formatSupported") is not True
        or live.get("identityAmbiguous") is not False
        or live.get("interfaceHash") != computed_interface
        or live.get("interfaceComplete") is not computed_complete
        or live.get("constantBlocks") != inventory_blocks
    ):
        raise CatalogPreparationRequired(
            "v2 Shader ABI/reflection evidence differs from the live inventory",
            "shader-resource-index-interface-mismatch",
        )
    expected["constantBlocks"] = inventory_blocks
    return expected


def _target_shader_inventory_coverage(
    result: Mapping[str, Any], targets: list[str], assets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate filtered coverage without promoting it to a full inventory."""
    if not targets:
        if result.get("inventoryScope", "all-shaders") != "all-shaders":
            raise CatalogPreparationRequired("unexpected filtered Shader inventory", "shader-resource-index-invalid-response")
        return {}

    def resource_ids(field: str) -> list[int]:
        values = result.get(field)
        if not isinstance(values, list) or len(values) > _MAX_FAST_CATALOG_SHADERS:
            raise RuntimeError("target Shader inventory has invalid " + field)
        clean = [_integer(value, field, 1) for value in values]
        if clean != sorted(set(clean)):
            raise RuntimeError("target Shader inventory repeats or reorders " + field)
        return clean

    def hashes(field: str) -> list[str]:
        values = result.get(field)
        if not isinstance(values, list) or len(values) > len(targets):
            raise RuntimeError("target Shader inventory has invalid " + field)
        clean = [_hex(value, 64, field) for value in values]
        if values != clean or clean != sorted(set(clean)) or not set(clean).issubset(targets):
            raise RuntimeError("target Shader inventory changed " + field)
        return clean

    try:
        filtered = resource_ids("filteredOutShaderResourceIds")
        unavailable = resource_ids("rawIdentityUnavailableShaderResourceIds")
        matched = resource_ids("targetMatchedShaderResourceIds")
        unindexed = resource_ids("unindexedShaderResourceIds")
        observed = hashes("observedTargetRawSha256s")
        unobserved = hashes("unobservedTargetRawSha256s")
        indexed = {asset["resourceId"] for asset in assets}
        known = indexed | set(unindexed)
        if (
            result.get("inventoryScope") != "target-raw-sha256"
            or result.get("rawHashPurpose") != "captured-executable-identity"
            or result.get("semanticEquivalenceVerified") is not False
            or result.get("targetRawSha256s") != targets
            or result.get("scanComplete") is not True
            or result.get("inventoryComplete") is not False
            or result.get("targetInventoryComplete") is not (not unavailable and not unindexed)
            or result.get("rawIdentitySearchComplete") is not (not unavailable)
            or result.get("filteredOutShaderResourceCount") != len(filtered)
            or isinstance(result.get("filteredOutShaderResourceCount"), bool)
            or set(filtered) & known
            or not set(unavailable).issubset(known)
            or not set(matched).issubset(known)
            or not indexed.issubset(matched)
            or not set(unindexed).issubset(set(unavailable) | set(matched))
            or unobserved != sorted(set(targets) - set(observed))
            or bool(matched) != bool(observed)
            or any(asset["rawSha256"] not in observed for asset in assets)
        ):
            raise RuntimeError("target Shader inventory scope/counts/identities are inconsistent")
    except (KeyError, RuntimeError) as exc:
        raise CatalogPreparationRequired(str(exc), "shader-resource-index-invalid-response") from exc
    return {key: copy.deepcopy(result[key]) for key in (
        "inventoryScope", "rawHashPurpose", "semanticEquivalenceVerified",
        "targetRawSha256s", "scanComplete", "targetInventoryComplete",
        "rawIdentitySearchComplete", "rawIdentityUnavailableShaderResourceIds",
        "filteredOutShaderResourceCount", "filteredOutShaderResourceIds",
        "targetMatchedShaderResourceIds", "observedTargetRawSha256s", "unobservedTargetRawSha256s",
    )}


def _inventory_shader_resources(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    api: str,
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    *,
    target_raw_sha256s: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Inventory unique Shaders, optionally analyzing only exact raw matches.

    Target mode still reads each physical module's actual bytes, but only a
    matching module receives DXIL/ABI/CBuffer/GetUsage work. Filtered results
    cannot publish a full symbol snapshot or become a complete inventory cache.
    """

    targets: list[str] = []
    if target_raw_sha256s is not None:
        if (not isinstance(target_raw_sha256s, (list, tuple))
                or not 1 <= len(target_raw_sha256s) <= _MAX_TARGET_RAW_SHADER_HASHES):
            raise ValueError("target_raw_sha256s must contain 1..16 exact lowercase SHA-256 strings")
        for value in target_raw_sha256s:
            digest = _hex(value, 64, "target_raw_sha256s item")
            if not isinstance(value, str) or value != digest:
                raise ValueError("target_raw_sha256s must contain exact lowercase SHA-256 strings")
            targets.append(digest)
        targets = sorted(set(targets))

    canonical_api = _asset_api(api)
    if canonical_api not in {"d3d11", "d3d12", "opengl", "vulkan"}:
        raise CatalogPreparationRequired(
            "the active capture API does not support unique Shader indexing",
            "shader-resource-index-api-unsupported",
        )
    request_capture_id = _canonical_sha256(
        {
            "schema": "replaylab-project-shader-resource-inventory-request/v1",
            "capturePath": _path_key(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
            **({"targetRawSha256s": targets} if targets else {}),
        }
    )
    evidence_sha256 = _canonical_sha256(
        {
            "mode": "unique-shader-resources",
            "eventTraversal": False,
            "anchorPerShaderMaximum": 1,
            **({"targetRawSha256s": targets, "inventoryScope": "target-raw-sha256"} if targets else {}),
        }
    )
    request = {
        "expected_capture_path": str(capture_path),
        "expected_capture_session_id": capture_session_id,
        "expected_capture_id": request_capture_id,
        "expected_identity_evidence_sha256": evidence_sha256,
        "expected_api": canonical_api,
        "expected_shaders": [],
        "inventory_all_shaders": True,
        "discover_anchor_eids": True,
    }
    if targets:
        request["target_raw_sha256s"] = targets
    if control_checkpoint is not None:
        control_checkpoint("before unique Shader resource inventory")
    try:
        snapshot = host.ipc.call(
            "begin_rdc_project_shader_resource_index", request, timeout=10.0
        )
    except Exception as exc:
        raise CatalogPreparationRequired(
            "could not start unique Shader resource indexing: %s" % exc,
            "shader-resource-index-unavailable",
        ) from exc
    request_sha256 = ""
    job_id = ""
    deadline = time.monotonic() + max(
        _FAST_INDEX_TIMEOUT_SECONDS, 300.0
    )
    last_progress: tuple[int, int, int] | None = None
    while True:
        if control_checkpoint is not None:
            control_checkpoint("during unique Shader resource inventory")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("schema")
            != "replaylab-project-shader-resource-index-job/v1"
            or snapshot.get("schemaVersion") != 1
            or snapshot.get("captureSessionId") != capture_session_id
        ):
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid unique Shader inventory job",
                "shader-resource-index-invalid-response",
            )
        current_job_id = _hex(
            snapshot.get("jobId"), 32, "Shader inventory jobId"
        )
        current_request_sha256 = _hex(
            snapshot.get("requestSha256"),
            64,
            "Shader inventory requestSha256",
        )
        if job_id and current_job_id != job_id:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader inventory job identity",
                "shader-resource-index-invalid-response",
            )
        if request_sha256 and current_request_sha256 != request_sha256:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader inventory request identity",
                "shader-resource-index-invalid-response",
            )
        job_id = current_job_id
        request_sha256 = current_request_sha256
        observation = snapshot.get("progress")
        if isinstance(observation, Mapping):
            current = observation.get("processedResourceCount", 0)
            total = observation.get("totalResourceCount", 0)
            resource_id = observation.get("resourceId")
            if (
                not isinstance(current, bool)
                and isinstance(current, int)
                and not isinstance(total, bool)
                and isinstance(total, int)
                and 0 <= current <= total <= _MAX_FAST_CATALOG_SHADERS
                and (resource_id is None or (
                    not isinstance(resource_id, bool)
                    and isinstance(resource_id, int)
                    and resource_id > 0
                ))
                and observation.get("eventTraversalCount") == 0
            ):
                signature = (current, total, int(resource_id or 0))
                if signature != last_progress:
                    progress.emit(
                        "resource-inventory",
                        "Indexing unique Shader resources",
                        current=current,
                        total=total,
                        item=(
                            "Shader ResourceId %d" % resource_id
                            if resource_id else ""
                        ),
                        detail={
                            "resourceId": resource_id,
                            "eventTraversalCount": 0,
                            "inspectedEntryPointCount": observation.get(
                                "inspectedEntryPointCount", 0
                            ),
                        },
                    )
                    last_progress = signature
        status = str(snapshot.get("status", "") or "")
        if status == "completed":
            break
        if status == "failed":
            raise CatalogPreparationRequired(
                "unique Shader resource indexing failed: %s"
                % str(snapshot.get("error", "") or "unknown failure"),
                "shader-resource-index-failed",
            )
        if status not in {"queued", "running"}:
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid unique Shader inventory state",
                "shader-resource-index-invalid-response",
            )
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired(
                "unique Shader resource indexing timed out",
                "shader-resource-index-timeout",
            )
        time.sleep(_FAST_INDEX_POLL_SECONDS)
        try:
            snapshot = host.ipc.call(
                "poll_rdc_project_shader_resource_index",
                {
                    "expected_capture_path": str(capture_path),
                    "expected_capture_session_id": capture_session_id,
                    "job_id": job_id,
                },
                timeout=5.0,
            )
        except Exception as exc:
            # EID/pipeline work can briefly delay the GUI-owned IPC poller
            # while the already-started replay job remains healthy. Continue
            # polling that same idempotent job instead of starting a second
            # all-Shader inventory.
            if (
                _is_transient_bridge_poll_timeout(exc)
                and time.monotonic() < deadline
            ):
                continue
            raise CatalogPreparationRequired(
                "could not poll unique Shader resource indexing: %s" % exc,
                "shader-resource-index-unavailable",
            ) from exc

    result = snapshot.get("result")
    capability = result.get("capability") if isinstance(result, Mapping) else None
    assets = result.get("assets") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema")
        != "replaylab-project-shader-resource-index/v2"
        or result.get("schemaVersion") != 2
        or result.get("captureSessionId") != capture_session_id
        or _path_key(result.get("capturePath", "")) != _path_key(capture_path)
        or result.get("captureId") != request_capture_id
        or result.get("identityEvidenceSha256") != evidence_sha256
        or result.get("requestSha256") != request_sha256
        or result.get("inventoryOnly") is not True
        or result.get("inventoryComplete") is not (not targets)
        or result.get("leaseEligible") is not False
        or result.get("eventTraversalCount") != 0
        or result.get("fullFrameTraversal") is not False
        or result.get("gpuReplayMutation") is not False
        or result.get("physicalApplyAuthority") is not False
        or result.get("anchorPerShaderMaximum") != 1
        or result.get("symbolSnapshotPublished") is not (not targets)
        or result.get("resourceOwnershipScope") != "target-capture-session"
        or result.get("crossCaptureResourceIdReuse") is not False
        or not isinstance(capability, Mapping)
        or capability.get("supported") is not True
        or capability.get("api") != canonical_api
        or capability.get("eventTraversalSupported") is not False
        or capability.get("fullFrameTraversalSupported") is not False
        or not isinstance(assets, list)
        or len(assets) > _MAX_FAST_CATALOG_SHADERS
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned invalid zero-EID Shader inventory evidence",
            "shader-resource-index-invalid-response",
        )

    clean_assets: list[dict[str, Any]] = []
    owner_anchors: dict[int, int | None] = {}
    owners: set[tuple[Any, ...]] = set()
    for position, value in enumerate(assets):
        if not isinstance(value, Mapping):
            raise CatalogPreparationRequired(
                "Shader inventory contains a malformed asset",
                "shader-resource-index-invalid-response",
            )
        identity = _normal_shader_identity(value)
        resource_id = identity["resourceId"]
        owner = (
            resource_id,
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        anchor = identity.get("anchorEventId")
        if owner in owners:
            raise CatalogPreparationRequired(
                "Shader inventory contains duplicate resource identities",
                "shader-resource-index-ambiguous",
            )
        if resource_id in owner_anchors and owner_anchors[resource_id] != anchor:
            raise CatalogPreparationRequired(
                "one Shader resource received multiple representative EIDs",
                "shader-resource-index-invalid-response",
            )
        owners.add(owner)
        owner_anchors[resource_id] = anchor
        if (
            value.get("ownerResourceId") != resource_id
            or value.get("ownerCaptureSessionId") != capture_session_id
            or value.get("resourceOwnershipScope")
            != "target-capture-session"
            or value.get("crossCaptureResourceIdReuse") is not False
            or value.get("api") != canonical_api
            or value.get("requiresAnchorPreflight") is not True
            or value.get("exactExecutableMatchEligible")
            is not bool(identity["stableKeyComplete"] and anchor is not None)
            or not isinstance(value.get("constantBlocks"), list)
        ):
            raise CatalogPreparationRequired(
                "Shader inventory changed resource ownership or reuse policy",
                "shader-resource-index-invalid-response",
            )
        blocks: list[dict[str, Any]] = []
        block_keys: set[tuple[Any, ...]] = set()
        for block in value["constantBlocks"]:
            if not isinstance(block, Mapping):
                raise CatalogPreparationRequired(
                    "Shader inventory contains a malformed CBuffer layout",
                    "shader-resource-index-invalid-response",
                )
            raw_fields = block.get("fields")
            if not isinstance(raw_fields, list):
                raise CatalogPreparationRequired(
                    "Shader inventory CBuffer omitted reflected fields",
                    "shader-resource-index-invalid-response",
                )
            fields: list[dict[str, Any]] = []
            field_paths: set[str] = set()
            for raw_field in raw_fields:
                if not isinstance(raw_field, Mapping):
                    raise CatalogPreparationRequired(
                        "Shader inventory contains a malformed CBuffer field",
                        "shader-resource-index-invalid-response",
                    )
                if not isinstance(raw_field.get("rowMajor"), bool):
                    raise CatalogPreparationRequired(
                        "Shader inventory CBuffer row-major evidence is malformed",
                        "shader-resource-index-invalid-response",
                    )
                field = {
                    "canonicalPath": _text(
                        raw_field.get("canonicalPath"),
                        "CBuffer field canonicalPath",
                        2048,
                    ),
                    "name": _text(
                        raw_field.get("name"), "CBuffer field name", 1024
                    ),
                    "absoluteByteOffset": _integer(
                        raw_field.get("absoluteByteOffset"),
                        "CBuffer field absoluteByteOffset",
                    ),
                    "byteSize": _integer(
                        raw_field.get("byteSize"), "CBuffer field byteSize", 1
                    ),
                    "baseType": _text(
                        raw_field.get("baseType"), "CBuffer field baseType", 64
                    ),
                    "rows": _integer(raw_field.get("rows"), "CBuffer field rows"),
                    "columns": _integer(
                        raw_field.get("columns"), "CBuffer field columns"
                    ),
                    "elements": _integer(
                        raw_field.get("elements"), "CBuffer field elements"
                    ),
                    "arrayByteStride": _integer(
                        raw_field.get("arrayByteStride"),
                        "CBuffer field arrayByteStride",
                    ),
                    "matrixByteStride": _integer(
                        raw_field.get("matrixByteStride"),
                        "CBuffer field matrixByteStride",
                    ),
                    "rowMajor": raw_field["rowMajor"],
                    "flags": _integer(
                        raw_field.get("flags"), "CBuffer field flags"
                    ),
                    "pointerTypeID": _integer(
                        raw_field.get("pointerTypeID"),
                        "CBuffer field pointerTypeID",
                    ),
                    "bitFieldOffset": _integer(
                        raw_field.get("bitFieldOffset"),
                        "CBuffer field bitFieldOffset",
                    ),
                    "bitFieldSize": _integer(
                        raw_field.get("bitFieldSize"),
                        "CBuffer field bitFieldSize",
                    ),
                }
                if field["canonicalPath"] in field_paths:
                    raise CatalogPreparationRequired(
                        "Shader inventory contains duplicate CBuffer field paths",
                        "shader-resource-index-ambiguous",
                    )
                field_paths.add(field["canonicalPath"])
                fields.append(field)
            fields.sort(
                key=lambda item: (
                    item["absoluteByteOffset"],
                    item["canonicalPath"],
                    item["name"],
                    item["byteSize"],
                    item["baseType"],
                    item["rows"],
                    item["columns"],
                    item["elements"],
                    item["arrayByteStride"],
                    item["matrixByteStride"],
                    item["rowMajor"],
                    item["flags"],
                    item["pointerTypeID"],
                    item["bitFieldOffset"],
                    item["bitFieldSize"],
                )
            )
            projected = {
                "index": _integer(block.get("index"), "CBuffer index"),
                "path": _text(block.get("path"), "CBuffer path", 2048),
                "name": _text(block.get("name"), "CBuffer name", 1024),
                "space": _integer(block.get("space"), "CBuffer space"),
                "bind": _integer(block.get("bind"), "CBuffer bind"),
                "byteSize": _integer(
                    block.get("byteSize"), "CBuffer byteSize", 1
                ),
                "layoutSha256": _hex(
                    block.get("layoutSha256"), 64, "CBuffer layoutSha256"
                ),
                "abiSha256": _hex(
                    block.get("abiSha256"), 64, "CBuffer abiSha256"
                ),
                "fields": fields,
            }
            if any(
                field["absoluteByteOffset"] + field["byteSize"]
                > projected["byteSize"]
                for field in fields
            ):
                raise CatalogPreparationRequired(
                    "Shader inventory CBuffer field exceeds its block",
                    "shader-resource-index-invalid-response",
                )
            block_key = (
                projected["space"],
                projected["bind"],
                projected["index"],
                projected["path"],
            )
            if block_key in block_keys:
                raise CatalogPreparationRequired(
                    "Shader inventory contains duplicate CBuffer ownership",
                    "shader-resource-index-ambiguous",
                )
            block_keys.add(block_key)
            blocks.append(projected)
        blocks.sort(
            key=lambda item: (
                item["space"], item["bind"], item["index"], item["path"]
            )
        )
        clean_assets.append(
            {
                **identity,
                "interfaceHash": _hex(
                    value.get("interfaceHash"), 64, "Shader interfaceHash"
                ),
                "interfaceComplete": value.get("interfaceComplete") is True,
                "constantBlocks": blocks,
                "rawSize": _integer(value.get("rawSize"), "Shader rawSize"),
                "rawBytecodeIncluded": value.get("rawBytecodeIncluded") is True,
                "formatSupported": value.get("formatSupported") is True,
                "identityAmbiguous": value.get("identityAmbiguous") is True,
                "exactExecutableMatchEligible": (
                    value.get("exactExecutableMatchEligible") is True
                ),
            }
        )

    clean_assets.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["rawSha256"],
        )
    )
    try:
        resource_count = _integer(
            result.get("uniqueShaderResourceCount"),
            "unique Shader resource count",
        )
        processed_resource_count = _integer(
            result.get("processedShaderResourceCount"),
            "processed Shader resource count",
        )
    except RuntimeError as exc:
        raise CatalogPreparationRequired(
            "Shader inventory resource counts are malformed",
            "shader-resource-index-invalid-response",
        ) from exc
    if (
        resource_count > _MAX_FAST_CATALOG_SHADERS
        or processed_resource_count > _MAX_FAST_CATALOG_SHADERS
    ):
        raise CatalogPreparationRequired(
            "Shader inventory exceeds the unique-resource limit",
            "shader-resource-index-invalid-response",
        )
    target_coverage = _target_shader_inventory_coverage(result, targets, clean_assets)
    filtered_count = target_coverage.get("filteredOutShaderResourceCount", 0)
    indexed_fields = (
        "indexedShaderResourceCount",
        "unindexedShaderResourceCount",
        "unindexedShaderResourceIds",
    )
    indexed_field_presence = [field in result for field in indexed_fields]
    if any(indexed_field_presence) and not all(indexed_field_presence):
        raise CatalogPreparationRequired(
            "Shader inventory partially reports unindexed resources",
            "shader-resource-index-invalid-response",
        )
    if all(indexed_field_presence):
        try:
            indexed_resource_count = _integer(
                result.get("indexedShaderResourceCount"),
                "indexed Shader resource count",
            )
            unindexed_resource_count = _integer(
                result.get("unindexedShaderResourceCount"),
                "unindexed Shader resource count",
            )
        except RuntimeError as exc:
            raise CatalogPreparationRequired(
                "Shader inventory has malformed indexed-resource counts",
                "shader-resource-index-invalid-response",
            ) from exc
        raw_unindexed_resource_ids = result.get("unindexedShaderResourceIds")
        if (
            not isinstance(raw_unindexed_resource_ids, list)
            or len(raw_unindexed_resource_ids) > _MAX_FAST_CATALOG_SHADERS
        ):
            raise CatalogPreparationRequired(
                "Shader inventory has a malformed unindexed-resource ledger",
                "shader-resource-index-invalid-response",
            )
        try:
            unindexed_resource_ids = [
                _integer(value, "unindexed Shader ResourceId", 1)
                for value in raw_unindexed_resource_ids
            ]
        except RuntimeError as exc:
            raise CatalogPreparationRequired(
                "Shader inventory has a malformed unindexed ResourceId",
                "shader-resource-index-invalid-response",
            ) from exc
        if (
            unindexed_resource_ids != sorted(set(unindexed_resource_ids))
            or any(value in owner_anchors for value in unindexed_resource_ids)
            or indexed_resource_count != len(owner_anchors)
            or unindexed_resource_count != len(unindexed_resource_ids)
            or resource_count
            != indexed_resource_count + unindexed_resource_count + filtered_count
        ):
            raise CatalogPreparationRequired(
                "Shader inventory unindexed-resource ledger is inconsistent",
                "shader-resource-index-invalid-response",
            )
    else:
        # Backward compatibility for an older v2 Bridge is safe only when it
        # indexed every physical Shader resource. A gap needs the explicit,
        # bounded ResourceId ledger above and may never be inferred.
        indexed_resource_count = len(owner_anchors)
        unindexed_resource_count = 0
        unindexed_resource_ids = []
        if resource_count != indexed_resource_count:
            raise CatalogPreparationRequired(
                "Shader inventory omitted resources without an identity ledger",
                "shader-resource-index-invalid-response",
            )
    inventory_material = {
        "api": canonical_api,
        "shaders": [
            {
                "resourceId": item["resourceId"],
                "stableKeySha256": item["stableKeySha256"],
                "anchorEventId": item["anchorEventId"],
                "constantBlocks": item["constantBlocks"],
            }
            for item in clean_assets
        ],
    }
    inventory_sha256 = _canonical_sha256(inventory_material)
    stable_set_sha256 = _canonical_sha256(
        sorted({item["stableKeySha256"] for item in clean_assets})
    )
    live_sha256 = _canonical_sha256(
        {
            "api": canonical_api,
            "shaders": [
                {
                    "resourceId": item["resourceId"],
                    "stableKeySha256": item["stableKeySha256"],
                    "stableKeyComplete": item["stableKeyComplete"],
                    "anchorEventId": item["anchorEventId"],
                }
                for item in clean_assets
            ],
        }
    )
    if (
        result.get("resourceInventorySha256") != inventory_sha256
        or result.get("stableResourceSetSha256") != stable_set_sha256
        or result.get("liveIdentitySha256") != live_sha256
        or processed_resource_count != resource_count
        or result.get("matchedShaderCount") not in (0, len(clean_assets))
        or result.get("representativeEidCount")
        != sum(value is not None for value in owner_anchors.values())
        or result.get("symbolSnapshotShaderCount") != (0 if targets else len(clean_assets))
        or result.get("symbolSnapshotUseCount")
        != (0 if targets else sum(item["anchorEventId"] is not None for item in clean_assets))
        or result.get("symbolSnapshotCaptureId") != ("" if targets else stable_set_sha256)
    ):
        raise CatalogPreparationRequired(
            "Shader inventory aggregate hashes or counts are inconsistent",
            "shader-resource-index-invalid-response",
        )

    raw_blobs: dict[str, bytes] = {}
    encoded_blobs = result.get("rawBytecodeBlobs", [])
    if not isinstance(encoded_blobs, list):
        raise CatalogPreparationRequired(
            "Shader inventory bytecode table is malformed",
            "shader-resource-index-invalid-response",
        )
    for value in encoded_blobs:
        if (
            not isinstance(value, Mapping)
            or value.get("encoding") != "base64"
        ):
            raise CatalogPreparationRequired(
                "Shader inventory bytecode record is malformed",
                "shader-resource-index-invalid-response",
            )
        digest = _hex(value.get("sha256"), 64, "Shader bytecode sha256")
        size = _integer(value.get("size"), "Shader bytecode size", 1)
        try:
            payload = base64.b64decode(
                str(value.get("data", "") or "").encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise CatalogPreparationRequired(
                "Shader inventory bytecode is not strict base64",
                "shader-resource-index-invalid-response",
            ) from exc
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise CatalogPreparationRequired(
                "Shader inventory bytecode hash or size changed",
                "shader-resource-index-invalid-response",
            )
        if digest in raw_blobs:
            raise CatalogPreparationRequired(
                "Shader inventory repeats one bytecode blob",
                "shader-resource-index-ambiguous",
            )
        raw_blobs[digest] = payload
    for asset in clean_assets:
        if asset["rawBytecodeIncluded"] != (asset["rawSha256"] in raw_blobs):
            raise CatalogPreparationRequired(
                "Shader inventory bytecode inclusion evidence is inconsistent",
                "shader-resource-index-invalid-response",
            )
    progress.emit(
        "resource-inventory",
        "Unique Shader resource inventory completed",
        current=resource_count,
        total=resource_count,
        detail={
            "uniqueShaderResourceCount": resource_count,
            "indexedShaderResourceCount": indexed_resource_count,
            "unindexedShaderResourceCount": unindexed_resource_count,
            "unindexedShaderResourceIds": unindexed_resource_ids,
            "shaderEntryCount": len(clean_assets),
            "representativeEidCount": sum(
                value is not None for value in owner_anchors.values()
            ),
            "eventTraversalCount": 0,
        },
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": stable_set_sha256,
            "catalogSha256": inventory_sha256,
            "inventorySha256": inventory_sha256,
            "capturePath": str(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
        },
        "shaders": clean_assets,
        "rawBytecode": raw_blobs,
        "eventTraversalCount": 0,
        "fullFrameTraversal": False,
        "anchorPerShaderMaximum": 1,
        **target_coverage,
        "bridgeResult": dict(result),
    }


def _inventory_project_shader_targets(
    host: Any, replay: ReplayProjectService, capture_path: Path,
    capture_session_id: str, api: str, progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Index only current project heads, in fixed batches of 16 original hashes."""
    targets = replay.restorable_shader_raw_hashes()
    if not targets or len(targets) > 65536:
        raise CatalogPreparationRequired("project has no bounded original Shader lookup targets", "shader-resource-index-targets-unavailable")
    deadline = time.monotonic() + 300.0
    def checkpoint(phase: str) -> None:
        if control_checkpoint is not None:
            control_checkpoint(phase)
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired("bounded project Shader target lookup timed out", "shader-resource-index-timeout")
    results = []
    for offset in range(0, len(targets), _MAX_TARGET_RAW_SHADER_HASHES):
        checkpoint("before project Shader target batch")
        result = _inventory_shader_resources(
            host, capture_path, capture_session_id, api, progress, checkpoint,
            target_raw_sha256s=targets[offset:offset + _MAX_TARGET_RAW_SHADER_HASHES],
        )
        if (result.get("targetInventoryComplete") is not True
                or result.get("rawIdentitySearchComplete") is not True):
            raise CatalogPreparationRequired(
                "target Shader lookup is incomplete; unobserved targets are unknown, not absent",
                "shader-resource-index-targets-incomplete",
            )
        results.append(result)
    if len(results) == 1:
        return results[0]
    merged = copy.deepcopy(results[0])
    by_target: dict[tuple[int, str], dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    observed: set[str] = set()
    for result in results:
        if any(result["capture"].get(key) != results[0]["capture"].get(key)
               for key in ("capturePath", "captureSessionId", "api")):
            raise RuntimeError("project Shader target batches changed capture identity")
        for shader in result["shaders"]:
            key = (shader["resourceId"], shader["stableKeySha256"])
            if key in by_target and by_target[key] != shader:
                raise RuntimeError("project Shader target changed across lookup batches")
            by_target[key] = shader
        for digest, payload in result.get("rawBytecode", {}).items():
            if digest in blobs and blobs[digest] != payload:
                raise RuntimeError("project Shader target bytes changed across batches")
            blobs[digest] = payload
        observed.update(result["observedTargetRawSha256s"])
    shaders = sorted(by_target.values(), key=lambda item: (item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]))
    digest = _canonical_sha256({"api": _asset_api(api), "shaders": [
        {key: shader[key] for key in ("resourceId", "stableKeySha256", "anchorEventId", "constantBlocks")}
        for shader in shaders]})
    merged["capture"].update(captureId=_canonical_sha256(sorted({item["stableKeySha256"] for item in shaders})),
                             catalogSha256=digest, inventorySha256=digest)
    filtered = set(results[0]["filteredOutShaderResourceIds"])
    for result in results[1:]:
        filtered.intersection_update(result["filteredOutShaderResourceIds"])
    merged.update(shaders=shaders, rawBytecode=blobs, bridgeResult=None,
                  targetRawSha256s=targets, observedTargetRawSha256s=sorted(observed),
                  unobservedTargetRawSha256s=sorted(set(targets) - observed),
                  targetMatchedShaderResourceIds=sorted({item["resourceId"] for item in shaders}),
                  filteredOutShaderResourceIds=sorted(filtered), filteredOutShaderResourceCount=len(filtered),
                  targetBatchCount=len(results))
    return merged


def _project_shader_inventory(inventory: Mapping[str, Any], *, allow_target_lookup: bool = False) -> dict[str, Any]:
    """Project the live Bridge inventory to the strict project-library contract.

    Transport-only data (capture paths/sessions, bytecode response blobs and
    Bridge diagnostics) must never become project matching authority.  The
    project service recomputes the aggregate hashes from this bounded view.
    """

    if (inventory.get("inventoryScope", "all-shaders") != "all-shaders"
            and not (allow_target_lookup and inventory.get("inventoryScope") == "target-raw-sha256"
                     and inventory.get("targetInventoryComplete") is True
                     and inventory.get("rawIdentitySearchComplete") is True)):
        raise RuntimeError("filtered Shader lookup cannot publish a complete project inventory")

    capture = inventory.get("capture")
    shaders = inventory.get("shaders")
    if not isinstance(capture, Mapping) or not isinstance(shaders, list):
        raise RuntimeError("live Shader inventory is incomplete")
    projected: list[dict[str, Any]] = []
    seen_targets: set[tuple[int, str]] = set()
    resource_anchors: dict[int, int | None] = {}
    for position, shader in enumerate(shaders):
        if not isinstance(shader, Mapping):
            raise RuntimeError(
                "live Shader inventory item %d is malformed" % position
            )
        identity = _normal_shader_identity(shader)
        resource_id = identity["resourceId"]
        target_key = (resource_id, identity["stableKeySha256"])
        if target_key in seen_targets:
            raise RuntimeError(
                "live Shader inventory repeats one resource/entry identity"
            )
        seen_targets.add(target_key)
        anchor_event_id = identity.get("anchorEventId")
        if (
            resource_id in resource_anchors
            and resource_anchors[resource_id] != anchor_event_id
        ):
            raise RuntimeError(
                "live Shader inventory assigns conflicting representative EIDs to one resource"
            )
        resource_anchors[resource_id] = anchor_event_id
        raw_blocks = shader.get("constantBlocks")
        if not isinstance(raw_blocks, list):
            raise RuntimeError("live Shader inventory omitted CBuffer layouts")
        blocks: list[dict[str, Any]] = []
        for block_index, block in enumerate(raw_blocks):
            if not isinstance(block, Mapping):
                raise RuntimeError(
                    "live Shader CBuffer %d is malformed" % block_index
                )
            raw_fields = block.get("fields")
            if not isinstance(raw_fields, list) or not all(
                isinstance(value, Mapping) for value in raw_fields
            ):
                raise RuntimeError(
                    "live Shader CBuffer %d omitted reflected fields" % block_index
                )
            blocks.append(
                {
                    "index": _integer(block.get("index"), "CBuffer index"),
                    "path": _text(block.get("path"), "CBuffer path", 2048),
                    "name": _text(block.get("name"), "CBuffer name", 1024),
                    "space": _integer(block.get("space"), "CBuffer space"),
                    "bind": _integer(block.get("bind"), "CBuffer bind"),
                    "byteSize": _integer(
                        block.get("byteSize"), "CBuffer byteSize", 1
                    ),
                    "layoutSha256": _hex(
                        block.get("layoutSha256"), 64, "CBuffer layoutSha256"
                    ),
                    "abiSha256": _hex(
                        block.get("abiSha256"), 64, "CBuffer abiSha256"
                    ),
                    "fields": [copy.deepcopy(dict(value)) for value in raw_fields],
                }
            )
        blocks.sort(
            key=lambda item: (
                item["index"], item["path"], item["space"], item["bind"]
            )
        )
        projected.append(
            {
                "resourceId": resource_id,
                "api": identity["api"],
                # Persistent project manifests use the established display
                # token.  ``stableKeySha256`` above was verified from the
                # lowercase protocol token and is intentionally unchanged.
                "encoding": _project_asset_encoding(identity["encoding"]),
                "rawSha256": identity["rawSha256"],
                "stage": identity["stage"],
                "entryPoint": identity["entryPoint"],
                "abiHash": identity["abiHash"],
                "layoutHash": identity["layoutHash"],
                "interfaceHash": _hex(
                    shader.get("interfaceHash"), 64, "Shader interfaceHash"
                ),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "stableKeySha256": identity["stableKeySha256"],
                "anchorEventId": identity["anchorEventId"],
                "constantBlocks": blocks,
            }
        )
    projected.sort(
        key=lambda item: (
            item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]
        )
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": _hex(
                capture.get("captureId"), 64, "Shader inventory captureId"
            ),
            "catalogSha256": _hex(
                capture.get("catalogSha256"),
                64,
                "Shader inventory catalogSha256",
            ),
            "inventorySha256": _hex(
                capture.get("inventorySha256"),
                64,
                "Shader inventory inventorySha256",
            ),
            "api": _asset_api(capture.get("api")),
        },
        "shaders": projected,
    }


def _shader_inventory_cache_binding(
    replay: ReplayProjectService, active: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the current project/scope binding for a disposable sidecar."""

    status = replay.status()
    replay_project = active.get("replayProject")
    if not isinstance(status, Mapping) or not isinstance(replay_project, Mapping):
        raise RuntimeError("active Replay Project status is unavailable")
    project_id = _hex(
        replay_project.get("projectId"), 32, "active Replay Project projectId"
    )
    if status.get("projectId") != project_id:
        raise RuntimeError("active Replay Project changed before cache access")
    scope = status.get("scope")
    if isinstance(scope, Mapping):
        scope_sha256 = _canonical_sha256(scope)
    else:
        # This compatibility branch is useful to older embedded Hosts which
        # exposed the already-canonical digest but not the scope object.
        scope_sha256 = _hex(
            status.get("scopeSha256"), 64, "Replay Project scopeSha256"
        )
    return project_id, scope_sha256


def _project_has_no_restorable_knowledge(
    replay: ReplayProjectService,
) -> bool:
    """Return true only for a positively identified empty project knowledge set."""

    try:
        status = replay.status()
    except Exception:
        return False
    counts = status.get("knowledgeCounts") if isinstance(status, Mapping) else None
    required = ("workspaces", "materials", "aliases")
    if not isinstance(counts, Mapping) or any(key not in counts for key in required):
        return False
    values: list[int] = []
    for value in counts.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
        values.append(value)
    return bool(values) and all(value == 0 for value in values)


def _runtime_shader_inventory_from_cache_assets(
    assets: Sequence[Mapping[str, Any]],
    capture_path: Path,
    capture_session_id: str,
    api: str,
) -> dict[str, Any]:
    """Adapt sealed routing assets to the existing project-inventory shape.

    The returned ResourceIds and representative EIDs are still provisional.
    Callers must run the bounded target-capture verifier before any alias or
    executable restoration and should then rebuild this value from only the
    verified subset.
    """

    canonical_api = _asset_api(api)
    clean_assets: list[dict[str, Any]] = []
    owners: set[tuple[Any, ...]] = set()
    for position, value in enumerate(assets):
        if not isinstance(value, Mapping):
            raise RuntimeError(
                "cached Shader inventory item %d is malformed" % position
            )
        identity = _normal_shader_identity(value)
        if identity["api"] != canonical_api:
            raise RuntimeError("cached Shader inventory API changed")
        owner = (
            identity["resourceId"],
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        if owner in owners:
            raise RuntimeError("cached Shader inventory repeats one owner")
        owners.add(owner)
        blocks = value.get("constantBlocks")
        if not isinstance(blocks, list):
            raise RuntimeError("cached Shader inventory omitted CBuffer layouts")
        anchor = identity.get("anchorEventId")
        format_supported = value.get("formatSupported") is True
        identity_ambiguous = value.get("identityAmbiguous") is True
        clean_assets.append(
            {
                **identity,
                "interfaceHash": str(value.get("interfaceHash", "") or ""),
                "interfaceComplete": value.get("interfaceComplete") is True,
                "constantBlocks": [
                    copy.deepcopy(dict(block)) for block in blocks
                ],
                "rawSize": _integer(
                    value.get("rawSize"), "cached Shader rawSize"
                ),
                "rawBytecodeIncluded": False,
                "formatSupported": format_supported,
                "identityAmbiguous": identity_ambiguous,
                "exactExecutableMatchEligible": bool(
                    identity.get("stableKeyComplete") is True
                    and anchor is not None
                    and format_supported
                    and not identity_ambiguous
                ),
            }
        )
    clean_assets.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["rawSha256"],
        )
    )
    inventory_material = {
        "api": canonical_api,
        "shaders": [
            {
                "resourceId": item["resourceId"],
                "stableKeySha256": item["stableKeySha256"],
                "anchorEventId": item["anchorEventId"],
                "constantBlocks": item["constantBlocks"],
            }
            for item in clean_assets
        ],
    }
    inventory_sha256 = _canonical_sha256(inventory_material)
    stable_set_sha256 = _canonical_sha256(
        sorted({item["stableKeySha256"] for item in clean_assets})
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": stable_set_sha256,
            "catalogSha256": inventory_sha256,
            "inventorySha256": inventory_sha256,
            "capturePath": str(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
        },
        "shaders": clean_assets,
        "rawBytecode": {},
        "eventTraversalCount": 0,
        "fullFrameTraversal": False,
        "anchorPerShaderMaximum": 1,
        "cacheRoutingOnly": True,
    }


def _runtime_shader_inventory_from_cache(
    document: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
    api: str,
) -> dict[str, Any]:
    if (
        document.get("routingOnly") is not True
        or document.get("freshTargetValidationRequired") is not True
        or document.get("crossCaptureResourceIdReuse") is not False
        or not isinstance(document.get("assets"), list)
    ):
        raise RuntimeError("Shader inventory cache weakened its routing policy")
    return _runtime_shader_inventory_from_cache_assets(
        document["assets"], capture_path, capture_session_id, api
    )


def _cached_inventory_expected_shader(asset: Mapping[str, Any]) -> dict[str, Any]:
    identity = _normal_shader_identity(asset)
    blocks = asset.get("constantBlocks")
    if not isinstance(blocks, list):
        raise RuntimeError("cached Shader route omitted CBuffer layouts")
    return {
        "resourceId": identity["resourceId"],
        "stage": identity["stage"],
        "entryPoint": identity["entryPoint"],
        "api": identity["api"],
        "encoding": identity["encoding"],
        "rawSha256": identity["rawSha256"],
        "abiHash": identity["abiHash"],
        "interfaceHash": _hex(
            asset.get("interfaceHash"), 64, "cached Shader interfaceHash"
        ),
        "layoutHash": identity["layoutHash"],
        "interfaceComplete": asset.get("interfaceComplete") is True,
        "stableKeyComplete": identity.get("stableKeyComplete") is True,
        "stableKeySha256": identity["stableKeySha256"],
        "anchorEventId": _shader_anchor_event_id(identity),
        # The Bridge request deliberately accepts only physical block cores.
        # Names and fields remain sealed sidecar evidence and are admitted only
        # after these hashes have matched fresh target reflection.
        "constantBlocks": [
            {
                "index": _integer(block.get("index"), "cached CBuffer index"),
                "path": _text(block.get("path"), "cached CBuffer path", 2048),
                "space": _integer(block.get("space"), "cached CBuffer space"),
                "bind": _integer(block.get("bind"), "cached CBuffer bind"),
                "byteSize": _integer(
                    block.get("byteSize"), "cached CBuffer byteSize", 1
                ),
                "layoutSha256": _hex(
                    block.get("layoutSha256"),
                    64,
                    "cached CBuffer layoutSha256",
                ),
                "abiSha256": _hex(
                    block.get("abiSha256"), 64, "cached CBuffer abiSha256"
                ),
            }
            for block in blocks
            if isinstance(block, Mapping)
        ],
    }


def _verify_cached_inventory_candidates(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    prepared: Mapping[str, Any],
    provisional_inventory: Mapping[str, Any],
    cache_document: Mapping[str, Any],
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Freshly validate every cached route that the current project may use."""

    plan = prepared.get("reusePlan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    assets = provisional_inventory.get("shaders")
    capture = provisional_inventory.get("capture")
    if (
        not isinstance(matches, list)
        or not isinstance(assets, list)
        or not isinstance(capture, Mapping)
    ):
        raise RuntimeError("cached Shader reuse plan is incomplete")
    if len(matches) > _MAX_FAST_PROJECT_SHADERS:
        raise CatalogPreparationRequired(
            "cached Shader candidates exceed the bounded verifier limit",
            "shader-resource-index-shader-limit",
        )
    api = _asset_api(capture.get("api"))
    if api == "vulkan":
        # A Vulkan null-pipeline route does not provide physical ABI authority;
        # rebuild the current inventory before admitting cached reflected data.
        raise CatalogPreparationRequired(
            "Vulkan cached routes require a fresh complete inventory",
            "shader-resource-index-vulkan-cache-refresh",
        )
    by_target: dict[tuple[Any, str], list[Mapping[str, Any]]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        identity = _normal_shader_identity(asset)
        by_target.setdefault(
            (identity["resourceId"], identity["stableKeySha256"]), []
        ).append(asset)

    selected_assets: list[dict[str, Any]] = []
    expected_shaders: list[dict[str, Any]] = []
    for match in matches:
        shader = match.get("shader") if isinstance(match, Mapping) else None
        if not isinstance(shader, Mapping):
            raise RuntimeError("cached Shader reuse plan contains a malformed match")
        identity = _normal_shader_identity(shader)
        candidates = by_target.get(
            (identity["resourceId"], identity["stableKeySha256"]), []
        )
        if len(candidates) != 1:
            raise CatalogPreparationRequired(
                "cached Shader route is not unique",
                "shader-resource-index-ambiguous"
                if len(candidates) > 1
                else "shader-resource-index-mismatch",
            )
        asset = candidates[0]
        if (
            identity.get("stableKeyComplete") is not True
            or identity.get("anchorEventId") is None
            or asset.get("formatSupported") is not True
            or asset.get("identityAmbiguous") is not False
        ):
            raise CatalogPreparationRequired(
                "cached Shader route has no complete executable identity",
                "shader-resource-index-key-incomplete",
            )
        selected_assets.append(copy.deepcopy(dict(asset)))
        expected_shaders.append(_cached_inventory_expected_shader(asset))

    if expected_shaders:
        expected_sha256 = _canonical_sha256(expected_shaders)
        evidence_sha256 = _canonical_sha256(
            {
                "schema": "replaylab-sidecar-shader-route-validation/v1",
                "cacheSealSha256": cache_document.get("sealSha256"),
                "capturePathSha256": cache_document.get("capturePathSha256"),
                "inventoryAlgorithmVersion": cache_document.get(
                    "inventoryAlgorithmVersion"
                ),
                "matchingPolicyVersion": cache_document.get(
                    "matchingPolicyVersion"
                ),
                "expectedShadersSha256": expected_sha256,
            }
        )
        selected_capture_id = _canonical_sha256(
            sorted(
                {
                    item["stableKeySha256"]
                    for item in expected_shaders
                }
            )
        )
        _resource_index_job(
            host,
            capture_path,
            capture_session_id,
            selected_capture_id,
            evidence_sha256,
            api,
            expected_shaders,
            control_checkpoint,
        )
    return _runtime_shader_inventory_from_cache_assets(
        selected_assets, capture_path, capture_session_id, api
    )


def _resource_index_job(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    capture_id: str,
    evidence_sha256: str,
    api: str,
    shaders: Sequence[Mapping[str, Any]],
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    request = {
        "expected_capture_path": str(capture_path),
        "expected_capture_session_id": capture_session_id,
        "expected_capture_id": capture_id,
        "expected_identity_evidence_sha256": evidence_sha256,
        "expected_api": api,
        "expected_shaders": [copy.deepcopy(dict(item)) for item in shaders],
        "inventory_all_shaders": False,
        "discover_anchor_eids": False,
    }
    if control_checkpoint is not None:
        control_checkpoint("before Shader resource-index start")
    try:
        snapshot = host.ipc.call(
            "begin_rdc_project_shader_resource_index", request, timeout=10.0
        )
    except Exception as exc:
        reason_id = (
            "shader-resource-index-capture-changed"
            if "capture changed" in str(exc).lower()
            else "shader-resource-index-unavailable"
        )
        raise CatalogPreparationRequired(
            "could not start the bounded Shader resource index: %s" % exc,
            reason_id,
        ) from exc
    if control_checkpoint is not None:
        control_checkpoint("after Shader resource-index start")
    deadline = time.monotonic() + _FAST_INDEX_TIMEOUT_SECONDS
    request_sha256 = ""
    while True:
        if control_checkpoint is not None:
            control_checkpoint("during Shader resource-index polling")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("schema")
            != "replaylab-project-shader-resource-index-job/v1"
            or snapshot.get("schemaVersion") != 1
            or snapshot.get("captureSessionId") != capture_session_id
        ):
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid Shader resource-index job",
                "shader-resource-index-invalid-response",
            )
        job_id = _hex(snapshot.get("jobId"), 32, "Shader resource-index jobId")
        current_request_sha256 = _hex(
            snapshot.get("requestSha256"), 64, "Shader resource-index requestSha256"
        )
        if request_sha256 and current_request_sha256 != request_sha256:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader resource-index request identity",
                "shader-resource-index-invalid-response",
            )
        request_sha256 = current_request_sha256
        status = str(snapshot.get("status", "") or "")
        if status == "completed":
            break
        if status == "failed":
            error = str(snapshot.get("error", "") or "")
            reason = (
                "shader-resource-index-timeout"
                if "timed out" in error.lower()
                else "shader-resource-index-failed"
            )
            raise CatalogPreparationRequired(
                "bounded Shader resource indexing failed: %s" % (error or status),
                reason,
            )
        if status not in {"queued", "running"}:
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid Shader resource-index state",
                "shader-resource-index-invalid-response",
            )
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired(
                "bounded Shader resource indexing timed out",
                "shader-resource-index-timeout",
            )
        time.sleep(_FAST_INDEX_POLL_SECONDS)
        try:
            snapshot = host.ipc.call(
                "poll_rdc_project_shader_resource_index",
                {
                    "expected_capture_path": str(capture_path),
                    "expected_capture_session_id": capture_session_id,
                    "job_id": job_id,
                },
                timeout=5.0,
            )
        except Exception as exc:
            if (
                _is_transient_bridge_poll_timeout(exc)
                and time.monotonic() < deadline
            ):
                continue
            reason_id = (
                "shader-resource-index-capture-changed"
                if "capture changed" in str(exc).lower()
                else "shader-resource-index-unavailable"
            )
            raise CatalogPreparationRequired(
                "could not poll the bounded Shader resource index: %s" % exc,
                reason_id,
            ) from exc
        if control_checkpoint is not None:
            control_checkpoint("after Shader resource-index poll")

    result = snapshot.get("result")
    reason_id = str(result.get("reasonId", "") or "") if isinstance(result, Mapping) else ""
    capability = result.get("capability") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema") != "replaylab-project-shader-resource-index/v2"
        or result.get("schemaVersion") != 2
        or result.get("captureSessionId") != capture_session_id
        or _path_key(result.get("capturePath", "")) != _path_key(capture_path)
        or result.get("captureId") != capture_id
        or result.get("identityEvidenceSha256") != evidence_sha256
        or result.get("requestSha256") != request_sha256
        or not isinstance(capability, Mapping)
        or capability.get("eventTraversalSupported") is not False
        or capability.get("fullFrameTraversalSupported") is not False
        or capability.get("anchorPreflightRequired") is not True
        or result.get("inventoryOnly") is not False
        or result.get("leaseEligible") is not True
        or result.get("eventTraversalCount") != 0
        or result.get("fullFrameTraversal") is not False
        or result.get("gpuReplayMutation") is not False
        or result.get("physicalApplyAuthority") is not False
        or result.get("resourceOwnershipScope") != "target-capture-session"
        or result.get("crossCaptureResourceIdReuse") is not False
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned invalid zero-EID Shader index evidence",
            "shader-resource-index-invalid-response",
        )
    if result.get("matched") is not True:
        raise CatalogPreparationRequired(
            "bounded Shader resource index did not uniquely match the project assets",
            reason_id or "shader-resource-index-mismatch",
        )
    if (
        capability.get("supported") is not True
        or capability.get("api") != api
        or capability.get("physicalAbiFromNullPipeline") != (api != "vulkan")
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned inconsistent Shader resource-index capability evidence",
            "shader-resource-index-invalid-response",
        )
    assets = result.get("assets")
    if (
        not isinstance(assets, list)
        or len(assets) != len(shaders)
        or result.get("expectedShaderCount") != len(shaders)
        or result.get("matchedShaderCount") != len(shaders)
    ):
        raise CatalogPreparationRequired(
            "bounded Shader resource index returned an incomplete asset set",
            "shader-resource-index-mismatch",
        )
    expected_by_owner = {
        (
            item["resourceId"], item["stage"], item["entryPoint"],
            item["encoding"], item["rawSha256"]
        ): item
        for item in shaders
    }
    seen_owners: set[tuple[Any, ...]] = set()
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise CatalogPreparationRequired(
                "bounded Shader resource index returned a malformed asset",
                "shader-resource-index-invalid-response",
            )
        owner = (
            asset.get("resourceId"), asset.get("stage"), asset.get("entryPoint"),
            asset.get("encoding"), asset.get("rawSha256")
        )
        expected = expected_by_owner.get(owner)
        if (
            expected is None
            or owner in seen_owners
            or asset.get("ownerResourceId") != expected["resourceId"]
            or asset.get("ownerCaptureSessionId") != capture_session_id
            or asset.get("resourceOwnershipScope") != "target-capture-session"
            or asset.get("crossCaptureResourceIdReuse") is not False
            or asset.get("api") != api
            or asset.get("abiHash") != expected.get("abiHash")
            or asset.get("stableKeyComplete")
            != expected.get("stableKeyComplete")
            or asset.get("stableKeySha256")
            != expected.get("stableKeySha256")
            or asset.get("interfaceHash") != expected["interfaceHash"]
            or asset.get("layoutHash") != expected["layoutHash"]
            or asset.get("interfaceComplete") != expected["interfaceComplete"]
            or asset.get("requiresAnchorPreflight") is not True
            or asset.get("anchorEventId") != expected["anchorEventId"]
        ):
            raise CatalogPreparationRequired(
                "bounded Shader resource index changed asset ownership or ABI identity",
                "shader-resource-index-invalid-response",
            )
        seen_owners.add(owner)
        if api == "vulkan":
            if (
                asset.get("physicalAbiValidated") is not False
                or asset.get("physicalAbiSha256", "") != ""
                or asset.get("contentMatchMode")
                != "module-sha256-owner-rid-anchor-preflight"
            ):
                raise CatalogPreparationRequired(
                    "Vulkan null-pipeline reflection claimed physical ABI authority",
                    "shader-resource-index-invalid-response",
                )
        elif (
            asset.get("physicalAbiValidated") is not True
            or not isinstance(asset.get("physicalAbiSha256"), str)
            or len(asset.get("physicalAbiSha256")) != 64
            or any(
                character not in _SHA256_CHARS
                for character in str(asset.get("physicalAbiSha256", ""))
            )
            or asset.get("constantBlocksMatched") is not True
            or asset.get("interfaceMatched") is not True
            or asset.get("contentMatchMode") != "stable-key-owner-rid"
            or asset.get("constantBlocks") != expected["constantBlocks"]
        ):
            raise CatalogPreparationRequired(
                "bounded Shader resource index did not prove the complete ABI/block set",
                "shader-resource-index-interface-mismatch",
            )
    if seen_owners != set(expected_by_owner):
        raise CatalogPreparationRequired(
            "bounded Shader resource index omitted target-RDC ownership",
            "shader-resource-index-mismatch",
        )
    live_sha256 = _hex(
        result.get("liveIdentitySha256"), 64, "live Shader identitySha256"
    )
    if control_checkpoint is not None:
        control_checkpoint("before Shader resource-index commit")
    try:
        lease = host.ipc.call(
            "commit_rdc_project_shader_resource_index",
            {
                "expected_capture_path": str(capture_path),
                "expected_capture_session_id": capture_session_id,
                "job_id": job_id,
                "expected_capture_id": capture_id,
                "expected_identity_evidence_sha256": evidence_sha256,
                "live_identity_sha256": live_sha256,
            },
            timeout=10.0,
        )
    except Exception as exc:
        reason_id = (
            "shader-resource-index-capture-changed"
            if "capture changed" in str(exc).lower()
            else "shader-resource-index-unavailable"
        )
        raise CatalogPreparationRequired(
            "could not commit the bounded Shader resource index: %s" % exc,
            reason_id,
        ) from exc
    if control_checkpoint is not None:
        control_checkpoint("after Shader resource-index commit")
    if (
        not isinstance(lease, dict)
        or lease.get("schema")
        != "replaylab-project-shader-resource-index-lease/v1"
        or lease.get("schemaVersion") != 1
        or lease.get("jobId") != job_id
        or lease.get("captureId") != capture_id
        or lease.get("captureSessionId") != capture_session_id
        or lease.get("identityEvidenceSha256") != evidence_sha256
        or lease.get("liveIdentitySha256") != live_sha256
        or lease.get("committed") is not True
        or lease.get("eventTraversalCount") != 0
        or lease.get("gpuReplayMutation") is not False
        or lease.get("physicalApplyAuthority") is not False
        or lease.get("resourceOwnershipScope") != "target-capture-session"
        or lease.get("crossCaptureResourceIdReuse") is not False
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned an invalid Shader resource-index lease",
            "shader-resource-index-invalid-response",
        )
    return {"job": dict(snapshot), "result": dict(result), "lease": lease}


def _match_workspace_resources(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    validated: Sequence[tuple[Mapping[str, Any], Path, dict[str, Any]]],
    control_checkpoint: Callable[[str], None] | None = None,
    *,
    inventory: Mapping[str, Any] | None = None,
    progress: _Progress | None = None,
) -> list[dict[str, Any]]:
    """Match cached artifacts to the unique target-capture resource inventory."""

    if not validated:
        raise RuntimeError("cached project binding has no Shader workspaces")
    if len(validated) > _MAX_FAST_PROJECT_SHADERS:
        raise CatalogPreparationRequired(
            "cached project binding exceeds the fast Shader resource-index limit",
            "shader-resource-index-shader-limit",
        )
    if inventory is None:
        groups: dict[
            tuple[str, str, str, str, str],
            list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]],
        ] = {}
        for record, unused_workspace, status in validated:
            if control_checkpoint is not None:
                control_checkpoint("before cached Shader catalog projection")
            projection = host._workspace_catalog_binding_projection(status)
            group_key = (
                str(projection["captureId"]),
                _path_key(projection["capturePath"]),
                str(projection["api"]),
                _path_key(projection["catalogPath"]),
                str(projection["catalogSha256"]),
            )
            groups.setdefault(group_key, []).append((record, status, projection))
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader catalog projection")

        reports: list[dict[str, Any]] = []
        matched_position = 0
        for group_key, entries in groups.items():
            first_status = entries[0][1]
            if control_checkpoint is not None:
                control_checkpoint("before cached Shader catalog identity evidence")
            scope = host._workspace_catalog_rebind_scope(first_status)
            evidence = scope.get("identityEvidence")
            if not isinstance(evidence, Mapping):
                raise RuntimeError("workspace catalog omitted exact identity evidence")
            evidence_sha256 = host._canonical_json_sha256(
                evidence, "RDC cached workspace identity evidence"
            )
            raw_use_tuples = evidence.get("useTuples", [])
            if (
                not isinstance(raw_use_tuples, list)
                or len(raw_use_tuples) > _MAX_FAST_EVIDENCE_USES
            ):
                raise CatalogPreparationRequired(
                    "workspace catalog evidence exceeds the bounded use-index budget",
                    "shader-resource-index-evidence-limit",
                )
            use_tuples = {
                (int(item[0]), str(item[1]))
                for item in raw_use_tuples
                if isinstance(item, list)
                and len(item) == 2
                and not isinstance(item[0], bool)
                and isinstance(item[0], int)
            }
            catalog = _strict_fast_catalog(group_key[3], group_key[4])
            catalog_shader_index = _fast_catalog_shader_index(catalog)
            api = _asset_api(group_key[2])
            capture_record = catalog.get("capture")
            catalog_api = catalog.get("api", "")
            if not catalog_api and isinstance(capture_record, Mapping):
                catalog_api = capture_record.get("api", "")
            if _asset_api(catalog_api) != api:
                raise CatalogPreparationRequired(
                    "active catalog API differs from the managed Shader workspace",
                    "shader-resource-index-api-mismatch",
                )
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader catalog identity evidence")

            expected_shaders: list[dict[str, Any]] = []
            expected_owner_keys: set[tuple[Any, ...]] = set()
            total_blocks = 0
            for record, unused_status, projection in entries:
                current_key = (
                    str(projection["captureId"]),
                    _path_key(projection["capturePath"]),
                    str(projection["api"]),
                    _path_key(projection["catalogPath"]),
                    str(projection["catalogSha256"]),
                )
                if current_key != group_key:
                    raise RuntimeError("cached workspaces changed catalog grouping")
                identity = _normal_shader_identity(record.get("identity", {}))
                event_id = _shader_anchor_event_id(identity)
                if (event_id, str(projection["shaderKey"])) not in use_tuples:
                    raise RuntimeError(
                        "cached Shader anchor is absent from the exact workspace catalog"
                    )
                expected = _expected_resource_shader(
                    catalog,
                    identity,
                    api,
                    event_id,
                    catalog_shader_index,
                )
                owner_key = (
                    expected["resourceId"],
                    expected["stage"],
                    expected["entryPoint"],
                    expected["rawSha256"],
                )
                if owner_key in expected_owner_keys:
                    raise CatalogPreparationRequired(
                        "cached project binding repeats one target-RDC Shader owner",
                        "shader-resource-index-ambiguous",
                    )
                expected_owner_keys.add(owner_key)
                total_blocks += len(expected["constantBlocks"])
                if total_blocks > _MAX_FAST_PROJECT_BLOCKS:
                    raise CatalogPreparationRequired(
                        "cached project binding exceeds the fast CBuffer index limit",
                        "shader-resource-index-block-limit",
                    )
                expected_shaders.append(expected)
                matched_position += 1
                if progress is not None:
                    progress.emit(
                        "resource-match",
                        "Matching exact project Shader artifacts",
                        current=matched_position,
                        total=len(validated),
                        item="Shader ResourceId %d" % identity["resourceId"],
                        detail={
                            "resourceId": identity["resourceId"],
                            "stableKeySha256": identity["stableKeySha256"],
                            "eventTraversalCount": 0,
                        },
                    )

            reports.append(
                _resource_index_job(
                    host,
                    capture_path,
                    capture_session_id,
                    group_key[0],
                    evidence_sha256,
                    api,
                    expected_shaders,
                    control_checkpoint,
                )
            )
        return reports
    capture = inventory.get("capture") if isinstance(inventory, Mapping) else None
    assets = inventory.get("shaders") if isinstance(inventory, Mapping) else None
    if (
        not isinstance(capture, Mapping)
        or capture.get("captureSessionId") != capture_session_id
        or _path_key(capture.get("capturePath", "")) != _path_key(capture_path)
        or inventory.get("eventTraversalCount") != 0
        or inventory.get("fullFrameTraversal") is not False
        or inventory.get("anchorPerShaderMaximum") != 1
        or not isinstance(assets, list)
    ):
        raise CatalogPreparationRequired(
            "the target Shader resource inventory is invalid",
            "shader-resource-index-invalid-response",
        )
    by_owner: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        key = (
            asset.get("resourceId"),
            asset.get("stage"),
            asset.get("entryPoint", ""),
            asset.get("rawSha256"),
        )
        by_owner.setdefault(key, []).append(asset)

    matched_assets: list[dict[str, Any]] = []
    for position, (record, unused_workspace, status) in enumerate(validated, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader stable-key matching")
        identity = _normal_shader_identity(record.get("identity", {}))
        if identity.get("stableKeyComplete") is not True:
            raise CatalogPreparationRequired(
                "the legacy RDC Shader association has no complete stable key",
                "shader-resource-index-key-incomplete",
            )
        owner = (
            identity["resourceId"],
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        candidates = list(by_owner.get(owner, []))
        exact = [
            item
            for item in candidates
            if item.get("stableKeyComplete") is True
            and item.get("stableKeySha256") == identity["stableKeySha256"]
            and item.get("api") == identity["api"]
            and item.get("encoding") == identity["encoding"]
            and item.get("abiHash") == identity["abiHash"]
            and item.get("layoutHash") == identity["layoutHash"]
            and item.get("anchorEventId") == identity.get("anchorEventId")
            and item.get("identityAmbiguous") is False
            and item.get("formatSupported") is True
        ]
        if len(exact) != 1:
            raise CatalogPreparationRequired(
                "the cached Shader association did not exactly match the target resource",
                (
                    "shader-resource-index-ambiguous"
                    if len(exact) > 1
                    else "shader-resource-index-mismatch"
                ),
            )
        target_profile = status.get("targetProfile")
        if (
            not isinstance(target_profile, Mapping)
            or _asset_api(status.get("capture", {}).get("api"))
            != identity["api"]
            or _asset_encoding(status.get("baseline", {}).get("kind"))
            != identity["encoding"]
        ):
            raise CatalogPreparationRequired(
                "the managed Shader workspace API/encoding changed",
                "shader-resource-index-format-unsupported",
            )
        matched_assets.append(dict(exact[0]))
        if progress is not None:
            progress.emit(
                "resource-match",
                "Matching exact project Shader artifacts",
                current=position,
                total=len(validated),
                item="Shader ResourceId %d" % identity["resourceId"],
                detail={
                    "resourceId": identity["resourceId"],
                    "stableKeySha256": identity["stableKeySha256"],
                    "eventTraversalCount": 0,
                },
            )
        if control_checkpoint is not None:
            control_checkpoint("after cached Shader stable-key matching")
    return [
        {
            "schema": "replaylab-project-shader-resource-match/v2",
            "schemaVersion": 2,
            "inventory": inventory,
            "assets": matched_assets,
            "eventTraversalCount": 0,
        }
    ]


def _validate_workspace_apply_response(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    wrapped: Mapping[str, Any],
    *,
    operation_id: str,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    expected_anchor_event_id: int,
    capture_session_id: str,
) -> dict[str, Any]:
    binding = wrapped.get("binding")
    validation = wrapped.get("validation")
    replacement = wrapped.get("replacement")
    rollback = wrapped.get("rollback")
    shader = status.get("shader")
    capture = status.get("capture")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if not all(
        isinstance(value, Mapping)
        for value in (binding, validation, replacement, rollback, shader, capture, baseline, latest, target_profile)
    ):
        raise RuntimeError("Shader apply returned incomplete live ABI/replacement evidence")
    original_raw_sha256 = _hex(
        baseline.get("rawSha256"), 64, "Shader apply baseline.rawSha256"
    )
    baseline_abi_sha256 = _hex(
        baseline.get("abiSha256"), 64, "Shader apply baseline.abiSha256"
    )
    expected_api = _asset_api(capture.get("api"))
    expected_encoding = _asset_encoding(latest.get("kind"))
    expected_profile = _text(
        str(target_profile.get("profile", "")),
        "Shader apply targetProfile.profile",
        64,
    ).lower()
    validation_scope = validation.get("validationScope", "")
    # Project restore calls the public direct-MCP apply route. Lease/live-edit
    # wrappers have a different request binding and are not accepted here.
    ordinary_validation = validation_scope == ""
    request_id = ""
    report_sha256 = ""
    validation_evidence_valid = False
    if ordinary_validation:
        request_id = _hex(
            validation.get("requestId"), 64, "Shader apply validation.requestId"
        )
        report_sha256 = _hex(
            validation.get("abiReportSha256"),
            64,
            "Shader apply validation.abiReportSha256",
        )
        candidate_abi_sha256 = _hex(
            validation.get("candidateAbiSha256"), 64,
            "Shader apply validation.candidateAbiSha256",
        )
        # The canonical status reader revalidates the complete persisted ABI
        # report against its immutable candidate, reflection request and hash.
        # Compatibility may legitimately compare two different ABI digests
        # (for example, recovered CBuffer fields with proven physical access).
        readback = _tool_json(host.get_shader_workspace_status(str(workspace)),
            "read applied Shader ABI report", schema="renderdoc-ultra-shader-workspace-mcp/v1")
        current = readback.get("result") or {}
        current_latest = current.get("latestCompiled") or {}
        current_gate = current_latest.get("abiGate") or {}
        if (readback.get("operation") != "status"
                or current.get("workspaceId") != status.get("workspaceId")
                or current.get("capture") != capture
                or current.get("shader") != shader
                or current.get("baseline") != baseline
                or current.get("stage") != status.get("stage")
                or current.get("entryPoint", "") != status.get("entryPoint", "")
                or current.get("targetProfile") != target_profile
                or current_latest.get("revision") != expected_revision
                or current_latest.get("rawSha256") != expected_candidate_raw_sha256
                or current_latest.get("sourceSha256") != latest.get("sourceSha256")
                or current.get("source", {}).get("currentSha256") != latest.get("sourceSha256")
                or current_gate.get("status") != "compatible"
                or current_gate.get("baselineAbiSha256") != baseline_abi_sha256
                or current_gate.get("candidateAbiSha256") != candidate_abi_sha256
                or current_gate.get("requestId") != request_id
                or current_gate.get("reportSha256") != report_sha256
                or _path_key(workspace / str(current_gate.get("reportPath", "")))
                    != _path_key(validation.get("abiReportPath", ""))):
            raise RuntimeError("Shader apply returned invalid live ABI/replacement evidence: current ABI report differs")
        validation_evidence_valid = bool(
            validation.get("workspacePath") == str(workspace)
            and validation.get("revision") == expected_revision
            and validation.get("baselineAbiSha256") == baseline_abi_sha256
            and isinstance(validation.get("differences"), list)
            and not validation.get("differences")
            and validation.get("differencesTruncated") is False
            and validation.get("applied") is False
        )
    replacement_id = str(replacement.get("replacementId", "") or "")
    anchor_event_id = replacement.get("anchorEventId")
    candidate_byte_size = binding.get("candidateByteSize")
    if (
        wrapped.get("operation") != "apply-shared-resource"
        or wrapped.get("operationId") != operation_id
        or wrapped.get("applied") is not True
        or wrapped.get("scope") != "captured-resource"
        or binding.get("workspaceId") != status.get("workspaceId")
        or binding.get("captureId") != capture.get("captureId")
        or binding.get("revision") != expected_revision
        or binding.get("candidateRawSha256") != expected_candidate_raw_sha256
        or isinstance(candidate_byte_size, bool)
        or not isinstance(candidate_byte_size, int)
        or candidate_byte_size <= 0
        or binding.get("baselineAbiSha256") != baseline_abi_sha256
        or binding.get("stage") != status.get("stage")
        or binding.get("entryPoint", "") != status.get("entryPoint", "")
        or binding.get("api") != expected_api
        or binding.get("encoding") != expected_encoding
        or binding.get("targetProfile") != expected_profile
        or validation.get("compatible") is not True
        or validation.get("requiresReplayAbiValidation") is not False
        or validation.get("replayAbiValidated") is not True
        or not validation_evidence_valid
        or replacement.get("schema")
        != "renderdoc-ultra-shared-shader-replacement/v1"
        or replacement.get("schemaVersion") != 1
        or replacement.get("operationId") != operation_id
        or not isinstance(replacement.get("operationCreatedReplacement"), bool)
        or replacement.get("captureId") != capture.get("captureId")
        or replacement.get("workspaceId") != status.get("workspaceId")
        or replacement.get("captureSessionId") != capture_session_id
        or replacement.get("revision") != expected_revision
        or isinstance(anchor_event_id, bool)
        or not isinstance(anchor_event_id, int)
        or anchor_event_id != expected_anchor_event_id
        or replacement.get("stage") != status.get("stage")
        or replacement.get("entryPoint", "") != status.get("entryPoint", "")
        or replacement.get("api") != expected_api
        or replacement.get("encoding") != expected_encoding
        or replacement.get("originalResourceId") != shader.get("resourceId")
        or replacement.get("originalRawSha256") != original_raw_sha256
        or replacement.get("candidateRawSha256") != expected_candidate_raw_sha256
        or replacement.get("physicalAbiSha256") != baseline_abi_sha256
        or replacement.get("abiRequestId") != request_id
        or replacement.get("abiReportSha256") != report_sha256
        or len(replacement_id) != 32
        or any(character not in _HEX32_CHARS for character in replacement_id)
        or replacement.get("resourceScoped") is not True
        or replacement.get("eventScoped") is not False
        or replacement.get("allOriginalResourceUsesAffected") is not True
        or replacement.get("configured") is not True
        or replacement.get("originalShaderPreserved") is not True
        or replacement.get("singleOwner") is not True
        or rollback.get("tool") != "rollback_shader_workspace_shared_resource"
        or _path_key(rollback.get("workspacePath", "")) != _path_key(workspace)
        or rollback.get("replacementId") != replacement_id
    ):
        raise RuntimeError("Shader apply returned invalid live ABI/replacement evidence")
    return wrapped


def _recover_uncertain_workspace_apply(
    host: Any,
    status: Mapping[str, Any],
    operation_id: str,
    capture_session_id: str,
) -> dict[str, Any]:
    capture_id = _hex(
        status.get("capture", {}).get("captureId"),
        64,
        "uncertain Shader apply captureId",
    )
    workspace_id = _hex(
        status.get("workspaceId"), 32, "uncertain Shader apply workspaceId"
    )
    receipt = host.ipc.call(
        "recover_shader_candidate_shared_resource_operation",
        {
            "operation_id": _hex(
                operation_id, 32, "uncertain Shader apply operationId"
            ),
            "expected_capture_id": capture_id,
            "expected_capture_session_id": _hex(
                capture_session_id,
                32,
                "uncertain Shader apply captureSessionId",
            ),
            "workspace_id": workspace_id,
        },
        timeout=60.0,
    )
    rollback = receipt.get("rollback") if isinstance(receipt, Mapping) else None
    status_value = receipt.get("status") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema")
        != "renderdoc-ultra-shared-shader-operation-recovery/v1"
        or receipt.get("schemaVersion") != 1
        or receipt.get("operationId") != operation_id
        or receipt.get("captureId") != capture_id
        or receipt.get("captureSessionId") != capture_session_id
        or receipt.get("workspaceId") != workspace_id
        or receipt.get("originalShaderPreserved") is not True
        or receipt.get("strictReceipt") is not True
        or receipt.get("recovered") is not True
        or receipt.get("mutationPresent") is not False
        or status_value
        not in {"absent", "idempotent-noop", "recovered", "already-recovered"}
        or not isinstance(receipt.get("operationKnown"), bool)
        or not isinstance(receipt.get("operationCreatedReplacement"), bool)
        or (
            status_value == "recovered"
            and (
                not isinstance(rollback, Mapping)
                or rollback.get("replacementId") != receipt.get("replacementId")
                or rollback.get("captureId") != capture_id
                or rollback.get("captureSessionId") != capture_session_id
                or rollback.get("workspaceId") != workspace_id
                or rollback.get("resourceScoped") is not True
                or rollback.get("eventScoped") is not False
                or rollback.get("cleared") is not True
                or rollback.get("originalShaderPreserved") is not True
            )
        )
        or (status_value != "recovered" and rollback is not None)
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid uncertain Shader operation recovery evidence"
        )
    return dict(receipt)


def _apply_workspace(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    expected_anchor_event_id: int,
    capture_session_id: str,
) -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    try:
        wrapped = _tool_json(
            host.apply_shader_workspace_shared_resource(
                str(workspace),
                expected_revision,
                expected_candidate_raw_sha256,
                operation_id,
                expected_anchor_event_id=expected_anchor_event_id,
            ),
            "apply Shader workspace",
            schema="renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
        )
        return _validate_workspace_apply_response(
            host,
            workspace,
            status,
            wrapped,
            operation_id=operation_id,
            expected_revision=expected_revision,
            expected_candidate_raw_sha256=expected_candidate_raw_sha256,
            expected_anchor_event_id=expected_anchor_event_id,
            capture_session_id=capture_session_id,
        )
    except RestoreRollbackFailed:
        raise
    except Exception as apply_exc:
        try:
            recovery = _recover_uncertain_workspace_apply(
                host, status, operation_id, capture_session_id
            )
        except Exception as recovery_exc:
            raise RestoreRollbackFailed(
                "Shader apply response was uncertain and fail-safe recovery failed: "
                "%s: %s"
                % (type(recovery_exc).__name__, recovery_exc)
            ) from apply_exc
        raise RuntimeError(
            "Shader apply failed or returned invalid evidence; exact operation %s "
            "was fail-safe recovered with status %s (%s: %s)"
            % (
                operation_id,
                recovery.get("status"),
                type(apply_exc).__name__,
                apply_exc,
            )
        ) from apply_exc


def _adopt_exact_live_replacement(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    record: Mapping[str, Any],
    capture_session_id: str,
) -> dict[str, Any] | None:
    """Adopt an already-realized candidate in this exact capture session.

    This is a current-session idempotence optimisation, never persisted apply
    authority.  A replacement from an older session cannot appear in the
    Bridge response because both the request and wrapper are session-bound.
    """

    identity = _normal_shader_identity(record.get("identity", {}))
    managed = record.get("managedWorkspace")
    shader = status.get("shader")
    capture = status.get("capture")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    if not all(
        isinstance(value, Mapping)
        for value in (managed, shader, capture, baseline, latest)
    ):
        raise RuntimeError("live replacement adoption omitted workspace identity")
    workspace_id = _hex(
        managed.get("workspaceId"), 32, "live replacement workspaceId"
    )
    capture_id = _hex(
        capture.get("captureId"), 64, "live replacement captureId"
    )
    revision = _integer(
        record.get("candidateRevision"), "live replacement revision", 1
    )
    candidate_raw = _hex(
        record.get("candidateRawSha256"),
        64,
        "live replacement candidateRawSha256",
    )
    original_raw = _hex(
        baseline.get("rawSha256"), 64, "live replacement originalRawSha256"
    )
    physical_abi = _hex(
        baseline.get("abiSha256"), 64, "live replacement physicalAbiSha256"
    )
    if any(
        (
            identity.get("stableKeyComplete") is not True,
            identity.get("resourceId") != shader.get("resourceId"),
            identity.get("rawSha256") != original_raw,
            identity.get("abiHash") != physical_abi,
            status.get("workspaceId") != workspace_id,
            latest.get("revision") != revision,
            latest.get("rawSha256") != candidate_raw,
        )
    ):
        raise RuntimeError("live replacement adoption differs from desired Shader")
    try:
        response = host.ipc.call(
            "get_shared_shader_replacements",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
            },
            timeout=30.0,
        )
    except Exception:
        # Compatibility with an older Bridge: the ordinary strict apply route
        # remains safe and idempotent on its own.
        return None
    replacements = (
        response.get("replacements") if isinstance(response, Mapping) else None
    )
    if (
        not isinstance(response, Mapping)
        or response.get("schema")
        != "renderdoc-ultra-shared-shader-replacement-list/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureId") != capture_id
        or response.get("captureSessionId") != capture_session_id
        or response.get("originalShadersPreserved") is not True
        or response.get("replayMutation") is not False
        or not isinstance(replacements, list)
        or len(replacements) > _MAX_FAST_CATALOG_SHADERS
        or response.get("count") != len(replacements)
    ):
        # Malformed metadata cannot authorise adoption.  Fall through to the
        # public strict apply transaction instead of trusting any item.
        return None
    expected_api = _asset_api(capture.get("api"))
    expected_encoding = _asset_encoding(latest.get("kind"))
    anchor = _shader_anchor_event_id(identity)
    exact = [
        item
        for item in replacements
        if isinstance(item, Mapping)
        and item.get("workspaceId") == workspace_id
        and item.get("revision") == revision
        and item.get("candidateRawSha256") == candidate_raw
        and item.get("anchorEventId") == anchor
        and item.get("stage") == identity["stage"]
        and item.get("entryPoint", "") == identity["entryPoint"]
        and item.get("api") == expected_api
        and item.get("encoding") == expected_encoding
        and item.get("originalResourceId") == identity["resourceId"]
        and item.get("originalRawSha256") == original_raw
        and item.get("physicalAbiSha256") == physical_abi
        and item.get("resourceScoped") is True
        and item.get("eventScoped") is False
        and item.get("allOriginalResourceUsesAffected") is True
        and item.get("originalShaderPreserved") is True
    ]
    if len(exact) > 1:
        raise RuntimeError("multiple live replacements claim one desired Shader")
    if not exact:
        return None
    replacement_id = str(exact[0].get("replacementId", "") or "")
    if (
        len(replacement_id) != 32
        or any(character not in _HEX32_CHARS for character in replacement_id)
    ):
        return None
    return {
        "schema": "replaylab-project-live-replacement-adoption/v1",
        "schemaVersion": 1,
        "workspacePath": str(workspace),
        "replacement": copy.deepcopy(dict(exact[0])),
        "applied": True,
        "idempotentlyAdopted": True,
        "physicalApplyAuthority": False,
    }


def _rollback_applied_workspace(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    applied: Mapping[str, Any],
    capture_session_id: str,
) -> None:
    replacement = applied.get("replacement")
    if not isinstance(replacement, Mapping):
        raise RestoreRollbackFailed("applied Shader omitted replacement evidence")
    replacement_id = _hex(
        replacement.get("replacementId"), 32, "Shader rollback replacementId"
    )
    rolled_back = _tool_json(
        host.rollback_shader_workspace_shared_resource(
            str(workspace), replacement_id
        ),
        "rollback restored Shader workspace",
        schema="renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
    )
    result = rolled_back.get("replacement")
    if (
        rolled_back.get("operation") != "rollback-shared-resource"
        or rolled_back.get("workspaceId") != status.get("workspaceId")
        or rolled_back.get("captureId") != status.get("capture", {}).get("captureId")
        or rolled_back.get("applied") is not False
        or not isinstance(result, Mapping)
        or result.get("schema")
        != "renderdoc-ultra-shared-shader-replacement/v1"
        or result.get("schemaVersion") != 1
        or result.get("replacementId") != replacement_id
        or result.get("captureId") != status.get("capture", {}).get("captureId")
        or result.get("captureSessionId") != capture_session_id
        or result.get("workspaceId") != status.get("workspaceId")
        or result.get("resourceScoped") is not True
        or result.get("eventScoped") is not False
        or result.get("cleared") is not True
        or result.get("originalShaderPreserved") is not True
        or not isinstance(result.get("targetFreed"), bool)
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid restored Shader rollback evidence"
        )


def _rollback_applied_workspaces(
    host: Any,
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
) -> str:
    failures: list[str] = []
    while applied_workspaces:
        workspace, status, applied, capture_session_id = applied_workspaces.pop()
        try:
            _rollback_applied_workspace(
                host, workspace, status, applied, capture_session_id
            )
        except Exception as exc:
            failures.append(
                "%s: %s" % (type(exc).__name__, exc)
            )
    return "; ".join(failures)[:4096]


def _rollback_restored_material_sessions(
    host: Any,
    restored_sessions: list[tuple[str, str, str]],
) -> str:
    failures: list[str] = []
    while restored_sessions:
        session_id, capture_id, capture_session_id = restored_sessions.pop()
        try:
            rolled_back = _tool_json(
                host.rollback_material_session(session_id),
                "rollback restored material session",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
            if (
                rolled_back.get("operation") != "rollback"
                or rolled_back.get("sessionId") != session_id
                or rolled_back.get("captureId") != capture_id
                or rolled_back.get("captureSessionId") != capture_session_id
                or rolled_back.get("applied") is not False
                or rolled_back.get("snapshotReleased") is not True
            ):
                raise RestoreRollbackFailed(
                    "RenderDoc returned invalid restored material rollback evidence"
                )
        except Exception as exc:
            failures.append("%s: %s" % (type(exc).__name__, exc))
        finally:
            _MATERIAL_SYNC_PARAMETERS.pop(session_id, None)
    return "; ".join(failures)[:4096]


def _normal_shader_identity(shader: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalise one resource identity without promoting EID into its key.

    V2 identities are capture-independent exact keys plus two capture-local
    attachments: ``resourceId`` and an optional representative
    ``anchorEventId``.  The legacy shape remains readable only as incomplete
    routing evidence; it can never authorise executable reuse by itself.
    """

    resource_id = _integer(shader.get("resourceId"), "shader.resourceId", 1)
    stage = _text(shader.get("stage"), "shader.stage", 32)
    entry_point = _text(
        shader.get("entryPoint", ""),
        "shader.entryPoint",
        512,
        allow_empty=True,
    )
    raw_sha256 = _hex(shader.get("rawSha256"), 64, "shader.rawSha256")
    if any(
        field in shader
        for field in (
            "api",
            "encoding",
            "abiHash",
            "stableKeyComplete",
            "stableKeySha256",
            "anchorEventId",
        )
    ):
        api = _asset_api(shader.get("api"))
        encoding = _asset_encoding(shader.get("encoding"))
        abi_hash = str(shader.get("abiHash", "") or "").lower()
        layout_digest = str(shader.get("layoutHash", "") or "").lower()
        for value, label in (
            (abi_hash, "shader.abiHash"),
            (layout_digest, "shader.layoutHash"),
        ):
            if value:
                _hex(value, 64, label)
        complete = shader.get("stableKeyComplete") is True
        if complete and (not api or not encoding or not abi_hash or not layout_digest):
            raise RuntimeError(
                "complete Shader stable key omitted API, encoding, ABI, or layout"
            )
        material = {
            "api": api,
            "encoding": encoding,
            "rawSha256": raw_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "abiHash": abi_hash,
            "layoutHash": layout_digest,
        }
        stable_key = _canonical_sha256(material)
        supplied_key = str(shader.get("stableKeySha256", "") or "").lower()
        if supplied_key:
            _hex(supplied_key, 64, "shader.stableKeySha256")
            if supplied_key != stable_key:
                raise RuntimeError("Shader stable key digest does not match its fields")
        anchor = shader.get("anchorEventId")
        if anchor is not None:
            anchor = _integer(anchor, "shader.anchorEventId", 1)
        return {
            **material,
            "resourceId": resource_id,
            "stableKeyComplete": complete,
            "stableKeySha256": stable_key,
            "anchorEventId": anchor,
        }

    code_sha256 = str(shader.get("codeSha256", "") or "")
    interface_hash = str(shader.get("interfaceHash", "") or "")
    layout_digest = str(shader.get("layoutHash", "") or "")
    for value, label in (
        (code_sha256, "shader.codeSha256"),
        (interface_hash, "shader.interfaceHash"),
        (layout_digest, "shader.layoutHash"),
    ):
        if value:
            _hex(value, 64, label)
    event_ids = sorted(
        {
            _integer(value, "shader.eventId", 1)
            for value in list(shader.get("eventIds", []) or [])
        }
    )
    if not event_ids:
        raise RuntimeError("legacy Shader identity has no representative event")
    legacy_material = {
        "api": "",
        "encoding": "",
        "rawSha256": raw_sha256,
        "stage": stage,
        "entryPoint": entry_point,
        "abiHash": interface_hash,
        "layoutHash": layout_digest,
    }
    return {
        **legacy_material,
        "resourceId": resource_id,
        "stableKeyComplete": False,
        "stableKeySha256": _canonical_sha256(legacy_material),
        "anchorEventId": event_ids[0],
    }


def _shader_anchor_event_id(
    identity: Mapping[str, Any], *, required: bool = True
) -> int | None:
    value = identity.get("anchorEventId")
    if value is None:
        if required:
            raise RuntimeError(
                "Shader has no representative EID for live PSO/ABI validation"
            )
        return None
    return _integer(value, "shader.anchorEventId", 1)


def _shader_record_from_status(
    project_root: Path,
    identity: Mapping[str, Any],
    knowledge_id: str,
    workspace: Path,
    status: Mapping[str, Any],
    project_knowledge: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = status.get("source")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if (
        not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or not isinstance(target_profile, Mapping)
    ):
        raise RuntimeError("applied workspace omitted source/candidate state")
    relative = workspace.relative_to(project_root).as_posix()
    revision = _integer(latest.get("revision"), "candidateRevision", 1)
    candidate_sha256 = _hex(
        latest.get("rawSha256"), 64, "candidateRawSha256"
    )
    compile_config_sha256 = str(
        latest.get("compileConfigSha256", status.get("compileConfigSha256", ""))
        or ""
    )
    if compile_config_sha256:
        _hex(compile_config_sha256, 64, "compileConfigSha256")
    else:
        compile_config_sha256 = _canonical_sha256(target_profile)
    representations_sha256 = str(
        status.get("representationsSha256", "") or ""
    )
    if representations_sha256:
        _hex(representations_sha256, 64, "representationsSha256")
    aliases_sha256 = ""
    if isinstance(project_knowledge, Mapping):
        if project_knowledge.get("sourceSha256") != source.get("currentSha256"):
            raise RuntimeError(
                "project Shader artifact source differs from the applied workspace"
            )
        compiled = project_knowledge.get("compiledCandidate")
        if (
            not isinstance(compiled, Mapping)
            or compiled.get("rawSha256") != candidate_sha256
        ):
            raise RuntimeError(
                "project Shader artifact binary differs from the applied workspace"
            )
        compiler_binding = project_knowledge.get("compilerBinding")
        if isinstance(compiler_binding, Mapping):
            compile_config_sha256 = _canonical_sha256(compiler_binding)
        else:
            compile_config_sha256 = _canonical_sha256(
                project_knowledge.get("targetProfile")
            )
        representations = project_knowledge.get(
            "assemblyDisassemblyArtifacts", []
        )
        if isinstance(representations, list) and representations:
            representations_sha256 = _canonical_sha256(representations)
        alias_refs = project_knowledge.get("cbufferAliasRefs", [])
        if isinstance(alias_refs, list) and alias_refs:
            aliases_sha256 = _canonical_sha256(alias_refs)
    return {
        "identity": copy.deepcopy(dict(identity)),
        "knowledgeId": _hex(knowledge_id, 32, "workspace knowledgeId"),
        "managedWorkspace": {
            "workspaceId": _hex(status.get("workspaceId"), 32, "workspaceId"),
            "path": relative,
        },
        "candidateRevision": revision,
        "candidateRawSha256": candidate_sha256,
        "sourceSha256": _hex(
            source.get("currentSha256"), 64, "sourceSha256"
        ),
        "artifactRefs": {
            "projectArtifactId": _hex(
                knowledge_id, 32, "workspace knowledgeId"
            ),
            "revision": revision,
            "compiledSha256": candidate_sha256,
            "compileConfigSha256": compile_config_sha256,
            "representationsSha256": representations_sha256,
            "aliasesSha256": aliases_sha256,
        },
    }


def _knowledge_matches_shader(
    knowledge: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    executable: bool = False,
) -> bool:
    if (
        knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
        or knowledge.get("rawSha256") != identity.get("rawSha256")
    ):
        return False
    if executable:
        return bool(
            identity.get("stableKeyComplete") is True
            and knowledge.get("stableKeyComplete") is True
            and knowledge.get("stableKeySha256")
            == identity.get("stableKeySha256")
        )
    # Names, aliases and typed CBuffer knowledge are non-executable, but they
    # still require the same original program plus exact ABI/layout evidence.
    knowledge_abi = str(
        knowledge.get("abiHash", knowledge.get("baselineAbiSha256", "")) or ""
    )
    identity_abi = str(identity.get("abiHash", "") or "")
    return bool(
        knowledge.get("layoutHash", "") == identity.get("layoutHash", "")
        and (not knowledge_abi or not identity_abi or knowledge_abi == identity_abi)
    )


def _source_preparation_matches_shader(
    knowledge: Mapping[str, Any], identity: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """A raw-exact source match is not a binary or replay permission."""
    return bool(
        candidate.get("sourcePreparationOnly") is True
        and candidate.get("sourceSeedEligible") is True
        and candidate.get("matchMode") == "raw-exact"
        and candidate.get("binaryCandidateEligible") is False
        and candidate.get("binaryCandidate") is None
        and candidate.get("knowledgeId") == knowledge.get("knowledgeId")
        and candidate.get("sourceSha256") == knowledge.get("sourceSha256")
        and identity.get("anchorEventId") is not None
        and all(knowledge.get(key) == identity.get(key) and identity.get(key)
                for key in ("api", "encoding", "rawSha256", "stage", "abiHash"))
        and knowledge.get("entryPoint", "") == identity.get("entryPoint", "")
        and knowledge.get("baselineAbiSha256") == identity.get("abiHash")
        and knowledge.get("stableKeySha256") == identity.get("stableKeySha256")
    )


def _validate_prepared_project_source(
    status: Mapping[str, Any], knowledge: Mapping[str, Any],
    identity: Mapping[str, Any], binding_state: Mapping[str, Any],
) -> None:
    """Recheck the original project input after normal workspace admission."""
    source = status.get("source") or {}
    origin = source.get("projectSource") or {}
    baseline = status.get("baseline") or {}
    if (
        not isinstance(origin, Mapping)
        or origin.get("schema") != "replaylab-verified-project-shader-source/v1"
        or origin.get("projectId") != binding_state.get("projectId")
        or _path_key(origin.get("projectPath", "")) != _path_key(binding_state.get("projectPath", ""))
        or origin.get("knowledgeId") != knowledge.get("knowledgeId")
        or origin.get("sourceSha256") != knowledge.get("sourceSha256")
        or source.get("currentSha256") != knowledge.get("sourceSha256")
        or origin.get("originalRawSha256") != identity.get("rawSha256")
        or origin.get("baselineAbiSha256") != identity.get("abiHash")
        or baseline.get("abiSha256") != identity.get("abiHash")
        or _asset_encoding(baseline.get("kind")) != identity.get("encoding")
        or _asset_api((status.get("capture") or {}).get("api")) != identity.get("api")
        or origin.get("stage") != identity.get("stage")
        or origin.get("entryPoint", "") != identity.get("entryPoint", "")
        or origin.get("targetProfile") != knowledge.get("targetProfile")
        or status.get("targetProfile") != knowledge.get("targetProfile")
        or origin.get("applied") is not False
        or origin.get("requiresCompile") is not True
        or origin.get("requiresLiveAbiRevalidation") is not True
    ):
        raise RuntimeError("prepared project source differs from the current original Shader or active knowledge")


def _restore_material(
    host: Any,
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    project_workspace_id: str,
    identity: Mapping[str, Any],
    material_record: Mapping[str, Any],
    capture_session_id: str,
    validate_context: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    knowledge = binding_state.get("materials", {}).get(
        str(material_record.get("knowledgeId", ""))
    )
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("RDC material association references absent project knowledge")
    if not _knowledge_matches_shader(knowledge, identity):
        raise RuntimeError("material knowledge no longer matches the current Shader")
    if any(
        (
            material_record.get("shaderResourceId") != identity.get("resourceId"),
            material_record.get("stage") != identity.get("stage"),
            material_record.get("path") != knowledge.get("constantBlockPath"),
            material_record.get("layoutSha256")
            != knowledge.get("constantBlockLayoutSha256"),
        )
    ):
        raise RuntimeError("RDC material association differs from project knowledge")
    event_id = _integer(material_record.get("eventId"), "material.eventId", 1)
    # Material snapshots are event-scoped by design. They need not reuse the
    # representative Shader EID, but the live session below must prove that
    # this exact event binds the indexed physical Shader and CBuffer layout.
    block_index = _integer(material_record.get("blockIndex"), "material.blockIndex")
    session = _tool_json(
        host.prepare_material_session(
            event_id,
            block_index,
            str(identity["stage"]),
            str(binding_state["captureId"]),
        ),
        "prepare material session",
        schema="renderdoc-ultra-material-session/v1",
        schema_version=1,
    )
    session_id = _hex(session.get("sessionId"), 32, "material sessionId")
    try:
        if any(
            (
                session.get("captureId") != binding_state.get("captureId"),
                session.get("captureSessionId") != capture_session_id,
                session.get("eventId") != event_id,
                session.get("stage") != identity.get("stage"),
                session.get("constantBlockIndex") != block_index,
                session.get("constantBlockPath") != knowledge.get("constantBlockPath"),
                session.get("layoutSha256")
                != knowledge.get("constantBlockLayoutSha256"),
                session.get("shaderResourceId") != identity.get("resourceId"),
                not isinstance(session.get("parameters"), list),
            )
        ):
            raise RuntimeError("live material session changed block/layout identity")
        if validate_context is not None:
            validate_context()
        indexed_parameter_ids = {
            str(item.get("parameterId", "") or "")
            for item in list(material_record.get("parameters", []) or [])
            if isinstance(item, Mapping)
        }
        knowledge_parameters = list(knowledge.get("parameters", []) or [])
        if indexed_parameter_ids and indexed_parameter_ids != {
            str(item.get("parameterId", "") or "") for item in knowledge_parameters
        }:
            raise RuntimeError("RDC material parameter set differs from project knowledge")
        live_parameters = list(session["parameters"])
        updates = []
        batch_updates = []
        for saved in knowledge_parameters:
            matches = [
                item
                for item in live_parameters
                if isinstance(item, Mapping)
                and item.get("path") == saved.get("path")
                and item.get("baseType") == saved.get("baseType")
                and item.get("components") == saved.get("components")
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "material parameter %s has no unique live typed match"
                    % saved.get("path")
                )
            live_parameter_id = _hex(
                matches[0].get("parameterId"), 32, "live material parameterId"
            )
            batch_updates.append(
                {
                    "parameter": live_parameter_id,
                    "value": copy.deepcopy(saved.get("value")),
                }
            )
            updates.append(
                {
                    "parameterId": saved["parameterId"],
                    "liveParameterId": live_parameter_id,
                    "path": saved["path"],
                }
            )
        if batch_updates:
            updated = _tool_json(
                host.update_material_parameters(session_id, batch_updates),
                "update material parameters",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
            expected_live_ids = [item["liveParameterId"] for item in updates]
            if any(
                (
                    updated.get("operation") != "update-many",
                    updated.get("sessionId") != session_id,
                    updated.get("captureId") != binding_state.get("captureId"),
                    updated.get("captureSessionId") != capture_session_id,
                    updated.get("layoutSha256")
                    != knowledge.get("constantBlockLayoutSha256"),
                    updated.get("updatedParameterIds") != expected_live_ids,
                    updated.get("updatedParameterCount") != len(expected_live_ids),
                    updated.get("applied") is not True,
                )
            ):
                raise RuntimeError("material batch update returned invalid replay evidence")
            if validate_context is not None:
                validate_context()
        _remember_material_sync_parameters(
            session_id,
            session,
            str(binding_state["projectId"]),
            project_workspace_id,
            [str(item["liveParameterId"]) for item in updates],
        )
        clean_index_record = {
            "knowledgeId": knowledge["knowledgeId"],
            "shaderResourceId": identity["resourceId"],
            "stage": identity["stage"],
            "blockIndex": block_index,
            "path": knowledge["constantBlockPath"],
            "layoutSha256": knowledge["constantBlockLayoutSha256"],
            "eventId": event_id,
            "parameters": [
                {"parameterId": item["parameterId"]}
                for item in knowledge_parameters
            ],
        }
        return (
            {
                "status": "restored",
                "sessionId": session_id,
                "knowledgeId": knowledge["knowledgeId"],
                "eventId": event_id,
                "stage": identity["stage"],
                "blockIndex": block_index,
                "constantBlockPath": knowledge["constantBlockPath"],
                "parameterCount": len(updates),
                "updates": updates,
            },
            clean_index_record,
        )
    except Exception:
        try:
            _tool_json(
                host.rollback_material_session(session_id),
                "rollback material session",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
        except Exception:
            pass
        _MATERIAL_SYNC_PARAMETERS.pop(session_id, None)
        raise


def _write_index_best_effort(
    host: Any,
    base_index: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        index = upsert_project_binding(base_index or new_index(), binding)
        # Validate/canonicalise before crossing into the Bridge.
        index = parse_index(encode_index(index))
        if base_index is not None:
            canonical_base = parse_index(encode_index(base_index))
            if canonical_base == index:
                # Avoid rewriting the RDC section when reopening an unchanged
                # project. Besides saving I/O, this preserves the cheap file
                # observation used by the disposable Shader-inventory sidecar.
                return True, "", index
        response = host.ipc.call(
            "write_rdc_project_index",
            {
                "expected_capture_path": str(capture_path),
                "expected_capture_session_id": capture_session_id,
                "document": index,
            },
            timeout=60.0,
        )
        if (
            not isinstance(response, dict)
            or response.get("schema")
            != "replaylab-rdc-project-index-transport/v1"
            or response.get("schemaVersion") != 1
            or response.get("captureSessionId") != capture_session_id
            or _path_key(response.get("capturePath", "")) != _path_key(capture_path)
            or response.get("present") is not True
            or response.get("written") is not True
            or response.get("physicalApplyAuthority") is not False
            or response.get("document") != index
        ):
            raise RuntimeError("RenderDoc returned invalid RDC index write evidence")
        return True, "", index
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc), None


def _binding_reference_components(
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    """Hash only project heads referenced by this RDC association.

    The project-wide active revision is audit metadata.  It must not evict an
    otherwise exact RDC association when an unrelated project artifact is
    published, so the executable head is derived from the records actually
    named by this binding.
    """

    knowledge_ids = {
        "workspaces": sorted(
            {
                _hex(item.get("knowledgeId"), 32, "workspace knowledgeId")
                for item in shader_records
            }
        ),
        "aliases": [],
        "materials": sorted(
            {
                _hex(item.get("knowledgeId"), 32, "material knowledgeId")
                for item in material_records
            }
        ),
    }
    artifact_refs: list[dict[str, Any]] = []
    for item in shader_records:
        refs = item.get("artifactRefs")
        if not isinstance(refs, Mapping):
            raise RuntimeError("Shader binding omitted project artifact heads")
        artifact_refs.append(
            {
                "knowledgeId": _hex(
                    item.get("knowledgeId"), 32, "workspace knowledgeId"
                ),
                "artifactRefs": copy.deepcopy(dict(refs)),
            }
        )
    artifact_refs.sort(
        key=lambda item: (
            item["knowledgeId"],
            _canonical_sha256(item["artifactRefs"]),
        )
    )
    return _canonical_sha256(knowledge_ids), _canonical_sha256(artifact_refs)


def _binding_active_knowledge_revision(
    binding_state: Mapping[str, Any],
    previous_head: Mapping[str, Any] | None = None,
) -> int:
    raw_revision: Any = binding_state.get("activeKnowledgeRevision")
    inventory_binding = binding_state.get("inventoryBinding")
    if isinstance(inventory_binding, Mapping):
        raw_revision = inventory_binding.get(
            "activeKnowledgeRevision", raw_revision
        )
    if raw_revision is None and isinstance(previous_head, Mapping):
        raw_revision = previous_head.get("activeKnowledgeRevision")
    if raw_revision is None:
        return 0
    return _integer(raw_revision, "activeKnowledgeRevision")


def _desired_project_head(
    binding_state: Mapping[str, Any],
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
    *,
    previous_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    derived_knowledge_ids_sha256, derived_artifact_refs_sha256 = (
        _binding_reference_components(shader_records, material_records)
    )
    knowledge_ids_sha256 = derived_knowledge_ids_sha256
    artifact_refs_sha256 = derived_artifact_refs_sha256
    inventory_binding = binding_state.get("inventoryBinding")
    if isinstance(inventory_binding, Mapping):
        knowledge_ids_sha256 = _hex(
            inventory_binding.get("knowledgeIdsSha256"),
            64,
            "inventoryBinding.knowledgeIdsSha256",
        )
        artifact_refs_sha256 = _hex(
            inventory_binding.get("artifactRefsSha256"),
            64,
            "inventoryBinding.artifactRefsSha256",
        )
    elif isinstance(previous_head, Mapping) and previous_head.get("complete") is True:
        # Full-catalog restore snapshots predate these two explicit hashes.
        # Preserve the last codec-verified values and independently recompute
        # referencedHeadsSha256 below; knowledgeContentSha256 plus strict
        # per-workspace validation still proves currentness.
        knowledge_ids_sha256 = _hex(
            previous_head.get("knowledgeIdsSha256"),
            64,
            "desiredProjectHead.knowledgeIdsSha256",
        )
        artifact_refs_sha256 = _hex(
            previous_head.get("artifactRefsSha256"),
            64,
            "desiredProjectHead.artifactRefsSha256",
        )
    referenced_sha256 = _rdc_project_index.referenced_heads_sha256(
        shader_records, material_records
    )
    return _rdc_project_index.build_desired_project_head(
        active_knowledge_revision=_binding_active_knowledge_revision(
            binding_state, previous_head
        ),
        knowledge_content_sha256=_hex(
            binding_state.get("knowledgeContentSha256"),
            64,
            "knowledgeContentSha256",
        ),
        knowledge_ids_sha256=knowledge_ids_sha256,
        artifact_refs_sha256=artifact_refs_sha256,
        referenced_heads_sha256=referenced_sha256,
        complete=True,
    )


def _realized_receipt_from_record(
    record: Mapping[str, Any],
    physical_abi_sha256: str,
    *,
    abi_report_sha256: str | None = None,
) -> dict[str, Any]:
    managed = record.get("managedWorkspace")
    if not isinstance(managed, Mapping):
        raise RuntimeError("Shader receipt omitted its managed workspace")
    return _rdc_project_index.build_realized_apply_receipt(
        _normal_shader_identity(record.get("identity", {})),
        _hex(managed.get("workspaceId"), 32, "receipt workspaceId"),
        _integer(record.get("candidateRevision"), "receipt candidateRevision", 1),
        _hex(
            record.get("candidateRawSha256"),
            64,
            "receipt candidateRawSha256",
        ),
        _hex(physical_abi_sha256, 64, "receipt physicalAbiSha256"),
        abi_report_sha256=(
            _hex(abi_report_sha256, 64, "receipt abiReportSha256")
            if abi_report_sha256
            else None
        ),
    )


def _realized_receipt_from_apply(
    record: Mapping[str, Any], applied: Mapping[str, Any]
) -> dict[str, Any]:
    replacement = applied.get("replacement")
    if not isinstance(replacement, Mapping):
        raise RuntimeError("Shader apply omitted realized replacement evidence")
    return _realized_receipt_from_record(
        record,
        str(replacement.get("physicalAbiSha256", "")),
        abi_report_sha256=str(replacement.get("abiReportSha256", "") or "")
        or None,
    )


def _normalised_receipts_for_records(
    shader_records: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(receipts) != len(shader_records):
        raise RuntimeError("realized Shader receipt coverage is incomplete")
    expected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in shader_records:
        identity = _normal_shader_identity(record.get("identity", {}))
        managed = record.get("managedWorkspace")
        if not isinstance(managed, Mapping):
            raise RuntimeError("Shader receipt omitted its managed workspace")
        key = (
            identity["stableKeySha256"],
            identity["resourceId"],
            _hex(managed.get("workspaceId"), 32, "receipt workspaceId"),
            _integer(record.get("candidateRevision"), "receipt candidateRevision", 1),
            _hex(
                record.get("candidateRawSha256"),
                64,
                "receipt candidateRawSha256",
            ),
        )
        if key in expected:
            raise RuntimeError("realized Shader receipt target is ambiguous")
        expected[key] = dict(record)

    clean: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw_receipt in receipts:
        if not isinstance(raw_receipt, Mapping):
            raise RuntimeError("realized Shader receipt is malformed")
        key = (
            raw_receipt.get("stableKeySha256"),
            raw_receipt.get("originalResourceId"),
            raw_receipt.get("workspaceId"),
            raw_receipt.get("candidateRevision"),
            raw_receipt.get("candidateRawSha256"),
        )
        record = expected.get(key)
        if record is None or key in seen:
            raise RuntimeError("realized Shader receipt differs from desired binding")
        abi_report = str(raw_receipt.get("abiReportSha256", "") or "") or None
        rebuilt = _realized_receipt_from_record(
            record,
            str(raw_receipt.get("physicalAbiSha256", "")),
            abi_report_sha256=abi_report,
        )
        if rebuilt != dict(raw_receipt):
            raise RuntimeError("realized Shader receipt seal or policy is invalid")
        identity = _normal_shader_identity(record["identity"])
        if rebuilt["physicalAbiSha256"] != identity["abiHash"]:
            raise RuntimeError("realized Shader receipt ABI differs from desired identity")
        seen.add(key)
        clean.append(rebuilt)
    clean.sort(
        key=lambda item: (
            item["originalResourceId"],
            item["stage"],
            item["entryPoint"],
            item["workspaceId"],
        )
    )
    return clean


def _validate_cached_binding_accelerator(
    binding: Mapping[str, Any],
    binding_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    desired = binding.get("desiredProjectHead")
    realized = binding.get("realizedApplySnapshot")
    shaders = list(binding.get("shaders", []) or [])
    materials = list(binding.get("materials", []) or [])
    if (
        not isinstance(desired, Mapping)
        or desired.get("complete") is not True
        or not isinstance(realized, Mapping)
        or realized.get("coverage") != "complete"
        or realized.get("desiredHeadSha256") != desired.get("headSha256")
        or not isinstance(realized.get("receipts"), list)
    ):
        raise RuntimeError("RDC project binding has no complete realized head")
    current = _desired_project_head(
        binding_state, shaders, materials, previous_head=desired
    )
    # activeKnowledgeRevision is audit-only and is deliberately excluded from
    # headSha256 by the codec.  Referenced heads and immutable artifact hashes
    # remain exact.
    if current.get("headSha256") != desired.get("headSha256"):
        raise RuntimeError("RDC desired project head is stale")
    clean_receipts = _normalised_receipts_for_records(
        shaders, list(realized["receipts"])
    )
    return current, clean_receipts


def _revalidate_binding_state_before_commit(
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS-check the referenced project head immediately before RDC write."""

    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        project_inventory = binding_state.get("projectInventory")
        inventory_binding = binding_state.get("inventoryBinding")
        if not isinstance(project_inventory, Mapping) or not isinstance(
            inventory_binding, Mapping
        ):
            raise RuntimeError("inventory project head cannot be revalidated")
        snapshot = replay.validate_shader_inventory_snapshot(
            project_inventory, inventory_binding
        )
        refreshed = _inventory_binding_state(
            snapshot,
            {
                "shaders": copy.deepcopy(
                    list(binding_state.get("capture", {}).get("shaders", []) or [])
                )
            },
        )
    else:
        refreshed = replay.restore_knowledge_snapshot(
            str(binding.get("captureId", "")),
            str(binding.get("catalogSha256", "")),
        )
    for field in (
        "projectId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        if refreshed.get(field) != binding.get(field):
            raise RuntimeError(
                "project referenced head changed before RDC index commit"
            )
    expected_head = _desired_project_head(
        refreshed,
        list(binding.get("shaders", []) or []),
        list(binding.get("materials", []) or []),
        previous_head=(
            binding.get("desiredProjectHead")
            if isinstance(binding.get("desiredProjectHead"), Mapping)
            else None
        ),
    )
    desired_head = binding.get("desiredProjectHead")
    if (
        not isinstance(desired_head, Mapping)
        or expected_head.get("headSha256") != desired_head.get("headSha256")
    ):
        raise RuntimeError("project desired head changed before RDC index commit")
    return refreshed


def _binding_document(
    active: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
    realized_receipts: Sequence[Mapping[str, Any]] = (),
    previous_desired_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # A replay replacement is resource-wide, not entry-point-wide.  Never
    # persist two executable records that could make a later cache hit apply
    # the same physical Shader resource twice (or select an arbitrary entry
    # point).  Multi-entry-point metadata remains in the project inventory;
    # only the one validated executable association is eligible for the RDC
    # cache.
    seen_resource_ids: set[int] = set()
    for item in shader_records:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("identity"), Mapping
        ):
            raise RuntimeError("Shader binding record is malformed")
        resource_id = _normal_shader_identity(item["identity"])["resourceId"]
        if resource_id in seen_resource_ids:
            raise RuntimeError(
                "RDC index cannot map one physical Shader resource more than once"
            )
        seen_resource_ids.add(resource_id)

    shader_keys = {
        (item["identity"]["resourceId"], item["identity"]["stage"])
        for item in shader_records
    }
    materials = [
        copy.deepcopy(dict(item))
        for item in material_records
        if (item.get("shaderResourceId"), item.get("stage")) in shader_keys
    ]
    desired_binding = {
        "projectId": binding_state["projectId"],
        "workspaceId": active["workspaceId"],
        "scopeSha256": binding_state["scopeSha256"],
        "captureId": binding_state["captureId"],
        "catalogSha256": binding_state["catalogSha256"],
        "knowledgeContentSha256": binding_state["knowledgeContentSha256"],
        "matcherPolicySha256": binding_state["matcherPolicySha256"],
        "shaders": [copy.deepcopy(dict(item)) for item in shader_records],
        "materials": materials,
    }
    desired_head = _desired_project_head(
        binding_state,
        desired_binding["shaders"],
        materials,
        previous_head=previous_desired_head,
    )
    if realized_receipts:
        clean_receipts = _normalised_receipts_for_records(
            desired_binding["shaders"], realized_receipts
        )
        coverage = "complete"
    else:
        clean_receipts = []
        coverage = "none"
    realized_snapshot = _rdc_project_index.build_realized_apply_snapshot(
        desired_head, clean_receipts, coverage=coverage
    )
    return _rdc_project_index.build_project_binding(
        desired_binding, desired_head, realized_snapshot
    )


def _require_restore_context(
    host: Any,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
) -> dict[str, Any]:
    """Revalidate the exact project/capture lifecycle after a long operation."""

    current, unused_context, project, unused_replay, session = (
        _validate_capture_and_project(
            host,
            project_root,
            capture_path,
            capture_session_id,
        )
    )
    if (
        project != project_root
        or session != capture_session_id
        or current.get("workspaceId") != active.get("workspaceId")
        or current.get("replayProject", {}).get("projectId")
        != active.get("replayProject", {}).get("projectId")
    ):
        raise RestoreContextChanged(
            "active project or capture changed during project restore"
        )
    return current


def _prepare_response(
    host: Any,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build and strictly bind the full target-capture reuse plan."""

    progress.emit(
        "prepare",
        "Exporting the full Shader catalog for project matching",
    )
    if control_checkpoint is not None:
        control_checkpoint("before full-frame Shader catalog export")
    prepare_with_progress = getattr(
        host, "_prepare_active_capture_project_with_progress", None
    )
    if callable(prepare_with_progress):
        forward_lock = threading.RLock()
        highest_rank = -1
        export_position: tuple[int, int] | None = None

        def forward_export_progress(observation: Any) -> None:
            # Telemetry is display-only. Repeat a strict bounded projection
            # here because high-level worker observations and legacy sidecar
            # polling can arrive from different threads.
            nonlocal highest_rank, export_position
            try:
                if not isinstance(observation, Mapping):
                    return
                schema = observation.get("schema")
                phase = observation.get("phase")
                current = observation.get("current")
                total = observation.get("total")
                event_id = observation.get("eventId", 0)
                updated_unix_ms = observation.get("updatedUnixMs")
                item = observation.get("item", "")
                if (
                    any(
                        isinstance(value, bool) or not isinstance(value, int)
                        for value in (current, total, event_id, updated_unix_ms)
                    )
                    or not 0 <= current <= total <= _MAX_FAST_EVIDENCE_USES
                    or not 0 <= event_id <= 0xFFFFFFFF
                    or updated_unix_ms <= 0
                    or not isinstance(item, str)
                    or "\x00" in item
                    or len(item.encode("utf-8")) > 1024
                ):
                    return
                if schema == "replaylab-shader-catalog-progress/v1":
                    if (
                        phase != "export-shader-bindings"
                        or not 1 <= current <= total
                    ):
                        return
                    message = _PREPARE_PROGRESS_MESSAGES[phase]
                    item = "EID %d" % event_id if event_id else ""
                elif schema == "replaylab-active-capture-prepare-progress/v1":
                    message = observation.get("message")
                    if (
                        observation.get("schemaVersion") != 1
                        or phase not in _PREPARE_PROGRESS_MESSAGES
                        or message != _PREPARE_PROGRESS_MESSAGES[phase]
                    ):
                        return
                    if phase == "export-shader-bindings" and not 1 <= current <= total:
                        return
                else:
                    return
                rank = _PREPARE_PROGRESS_RANKS[phase]
                with forward_lock:
                    if rank < highest_rank:
                        return
                    if phase == "export-shader-bindings":
                        if export_position is not None and (
                            total != export_position[1]
                            or current < export_position[0]
                        ):
                            return
                        export_position = (current, total)
                    highest_rank = max(highest_rank, rank)
                    progress.emit(
                        phase,
                        message,
                        current=current,
                        total=total,
                        item=item,
                        detail={
                            "eventId": event_id,
                            "updatedUnixMs": updated_unix_ms,
                        },
                    )
            except Exception:
                return

        accepts_control = False
        try:
            signature = inspect.signature(prepare_with_progress)
            accepts_control = "control_checkpoint" in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            pass
        if accepts_control:
            raw = prepare_with_progress(
                progress.operation_id,
                forward_export_progress,
                control_checkpoint=control_checkpoint,
            )
        else:
            raw = prepare_with_progress(
                progress.operation_id, forward_export_progress
            )
    else:
        prepare = getattr(host, "_prepare_active_capture_project", None)
        if callable(prepare):
            raw = prepare(progress.operation_id)
        else:
            prepare = getattr(host, "prepare_active_capture_project", None)
            if not callable(prepare):
                raise RuntimeError(
                    "ReplayLab Host has no active-capture preparation route"
                )
            raw = prepare()
    if control_checkpoint is not None:
        control_checkpoint("after full-frame Shader catalog export")
    prepared = _tool_json(
        raw,
        "prepare active capture project",
        schema="replaylab-active-capture-prepare/v1",
        schema_version=1,
    )
    capture = prepared.get("capture")
    project = prepared.get("project")
    staged = prepared.get("stagedCatalog")
    exported = prepared.get("captureExport")
    plan = prepared.get("reusePlan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    if (
        not isinstance(capture, Mapping)
        or not isinstance(project, Mapping)
        or not isinstance(staged, Mapping)
        or not isinstance(exported, Mapping)
        or not isinstance(plan, Mapping)
        or not isinstance(matches, list)
        or len(matches) > _MAX_FAST_CATALOG_SHADERS
        or capture.get("captureSessionId") != capture_session_id
        or project.get("workspaceId") != active.get("workspaceId")
        or project.get("scopeConfigured") is not True
        or _path_key(project.get("projectRoot", "")) != _path_key(project_root)
        or project.get("replayProject", {}).get("projectId")
        != active.get("replayProject", {}).get("projectId")
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("projectId") != active.get("replayProject", {}).get("projectId")
        or plan.get("matchedShaderCount") != len(matches)
        or exported.get("fullFrame") is not True
        or prepared.get("applied") is not False
    ):
        raise RuntimeError("active capture preparation returned invalid project evidence")
    capture_id = _hex(capture.get("captureId"), 64, "prepared captureId")
    catalog_sha256 = _hex(
        staged.get("catalogSha256"), 64, "prepared catalogSha256"
    )
    if any(
        (
            staged.get("captureId") != capture_id,
            exported.get("captureId") != capture_id,
            exported.get("catalogSha256") != catalog_sha256,
            plan.get("captureId") != capture_id,
            plan.get("catalogSha256") != catalog_sha256,
        )
    ):
        raise RuntimeError("active capture preparation changed capture/catalog identity")
    policy = plan.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("rawExactOrCodeInterfaceLayoutExactOnly") is not True
        or policy.get("oldWorkspaceManifestReused") is not False
        or policy.get("oldCaptureAbiReportReused") is not False
        or policy.get("oldMaterialSessionReused") is not False
        or policy.get("liveAbiRevalidationRequiredBeforeShaderApply") is not True
        or policy.get("liveMaterialLayoutRevalidationRequired") is not True
    ):
        raise RuntimeError("project reuse plan weakened the physical validation policy")
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )
    progress.emit(
        "prepare",
        "Full capture catalog prepared; validating project reuse matches",
        current=len(matches),
        total=len(matches),
        detail={"captureId": capture_id, "matchedShaderCount": len(matches)},
    )
    return prepared


def _prepare_inventory_response(
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    inventory: Mapping[str, Any],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Match the unique-resource inventory directly against project artifacts."""

    project_inventory = _project_shader_inventory(inventory, allow_target_lookup=True)
    shaders = project_inventory["shaders"]
    progress.emit(
        "resource-match",
        "Matching exact project Shader artifacts",
        current=0,
        total=len(shaders),
        detail={
            "uniqueShaderResourceCount": len(shaders),
            "eventTraversalCount": 0,
        },
    )
    if control_checkpoint is not None:
        control_checkpoint("before exact project Shader inventory match")
    snapshot = replay.reuse_snapshot_for_shader_inventory(project_inventory)
    if control_checkpoint is not None:
        control_checkpoint("after exact project Shader inventory match")
    plan = snapshot.get("reusePlan") if isinstance(snapshot, Mapping) else None
    binding = snapshot.get("binding") if isinstance(snapshot, Mapping) else None
    knowledge = snapshot.get("knowledge") if isinstance(snapshot, Mapping) else None
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    policy = plan.get("policy") if isinstance(plan, Mapping) else None
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("schema")
        != "renderdoc-ultra-project-shader-inventory-reuse/v1"
        or snapshot.get("schemaVersion") != 1
        or snapshot.get("projectId")
        != active.get("replayProject", {}).get("projectId")
        or snapshot.get("inventory") != project_inventory
        or not isinstance(plan, Mapping)
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("captureId")
        != project_inventory["capture"]["captureId"]
        or plan.get("catalogSha256")
        != project_inventory["capture"]["inventorySha256"]
        or not isinstance(matches, list)
        or plan.get("matchedShaderCount") != len(matches)
        or len(matches) > len(shaders)
        or not isinstance(binding, Mapping)
        or not isinstance(knowledge, Mapping)
        or not isinstance(policy, Mapping)
        or policy.get("inventoryOnly") is not True
        or policy.get("fuzzyExecutableMatchAllowed") is not False
        or policy.get("stableKeyExactRequiredForSourceOrBinary") is not True
        or policy.get("targetAnchorRequiredBeforeExecutableApply") is not True
        or policy.get("binaryExecutableAutoApplyAuthorized") is not False
        or policy.get("inventoryMaterialValueReuseAllowed") is not False
    ):
        raise RuntimeError("project returned an invalid exact Shader inventory plan")

    live_by_target = {
        (item["resourceId"], item["stableKeySha256"]): item
        for item in inventory["shaders"]
    }
    prepared_plan = copy.deepcopy(dict(plan))
    if inventory.get("inventoryScope") == "target-raw-sha256":
        prepared_plan.update(inventoryScope="target-raw-sha256", fullFrame=False,
                             targetRawSha256s=list(inventory["targetRawSha256s"]))
    for match in prepared_plan["matches"]:
        shader = match.get("shader") if isinstance(match, Mapping) else None
        if not isinstance(shader, Mapping):
            raise RuntimeError("project Shader inventory plan contains a malformed match")
        target = live_by_target.get(
            (shader.get("resourceId"), shader.get("stableKeySha256"))
        )
        if target is None:
            raise RuntimeError("project Shader inventory plan changed target identity")
        # Preserve the Bridge's conservative completeness verdict. The project
        # service validates the seven-field key, while only fresh live evidence
        # can authorize an executable replacement.
        match["shader"] = {
            **dict(shader),
            "stableKeyComplete": target.get("stableKeyComplete") is True,
            "anchorEventId": target.get("anchorEventId"),
        }
        if list(match.get("materialCandidates", []) or []):
            raise RuntimeError(
                "project inventory plan included event-scoped material values"
            )
    progress.emit(
        "resource-match",
        "Exact project Shader artifact matching completed",
        current=len(shaders),
        total=len(shaders),
        detail={
            "matchedShaderCount": len(prepared_plan["matches"]),
            "eventTraversalCount": 0,
        },
    )
    return {
        "schema": "replaylab-active-shader-inventory-prepare/v1",
        "schemaVersion": 1,
        "inventory": project_inventory,
        "liveInventory": copy.deepcopy(dict(inventory)),
        "reusePlan": prepared_plan,
        "binding": copy.deepcopy(dict(binding)),
        "knowledge": copy.deepcopy(dict(knowledge)),
        "snapshotSha256": snapshot.get("snapshotSha256"),
        "applied": False,
    }


def _prepared_binding_state(
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    if prepared.get("schema") == "replaylab-active-shader-inventory-prepare/v1":
        project_inventory = prepared.get("inventory")
        live_inventory = prepared.get("liveInventory")
        binding = prepared.get("binding")
        if not all(
            isinstance(value, Mapping)
            for value in (project_inventory, live_inventory, binding)
        ):
            raise RuntimeError("prepared Shader inventory snapshot is incomplete")
        snapshot = replay.validate_shader_inventory_snapshot(
            project_inventory, binding
        )
        state = _inventory_binding_state(snapshot, live_inventory)
        plan = prepared.get("reusePlan")
        if (
            not isinstance(plan, Mapping)
            or state.get("projectId") != plan.get("projectId")
            or state.get("projectId")
            != active.get("replayProject", {}).get("projectId")
            or state.get("captureId") != plan.get("captureId")
            or state.get("catalogSha256") != plan.get("catalogSha256")
        ):
            raise RuntimeError(
                "prepared Shader inventory knowledge differs from its reuse plan"
            )
        return state
    plan = prepared["reusePlan"]
    state = replay.restore_knowledge_snapshot(
        str(plan["captureId"]), str(plan["catalogSha256"])
    )
    if not isinstance(state, dict):
        raise RuntimeError("prepared project returned invalid knowledge state")
    for field in (
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        _hex(state.get(field), 64, "prepared project %s" % field)
    _hex(state.get("projectId"), 32, "prepared projectId")
    if any(
        (
            state.get("projectId") != plan.get("projectId"),
            state.get("projectId")
            != active.get("replayProject", {}).get("projectId"),
            state.get("captureId") != plan.get("captureId"),
            state.get("catalogSha256") != plan.get("catalogSha256"),
            not isinstance(state.get("capture"), Mapping),
            not isinstance(state.get("workspaces"), Mapping),
            not isinstance(state.get("materials"), Mapping),
        )
    ):
        raise RuntimeError("prepared project knowledge differs from the reuse plan")
    return state


def _reuse_workspace_status(
    host: Any,
    active: Mapping[str, Any],
    workspace: Path,
    identity: Mapping[str, Any],
    capture_id: str,
    catalog_sha256: str | None,
) -> dict[str, Any]:
    wrapped = _tool_json(
        host.get_shader_workspace_status(str(workspace)),
        "get target Shader workspace status",
        schema="renderdoc-ultra-shader-workspace-mcp/v1",
    )
    if wrapped.get("operation") != "status" or not isinstance(
        wrapped.get("result"), Mapping
    ):
        raise RuntimeError("target Shader workspace status is invalid")
    status = dict(wrapped["result"])
    capture = status.get("capture")
    shader = status.get("shader")
    baseline = status.get("baseline")
    source = status.get("source")
    shader_root = Path(
        str(active.get("paths", {}).get("shaderWorkspaces", ""))
    ).resolve()
    status_catalog = (
        capture.get("catalogSha256", capture.get("catalogRevisionSha256", ""))
        if isinstance(capture, Mapping)
        else ""
    )
    status_catalog = str(status_catalog or "") or None
    expected_catalog = str(catalog_sha256 or "") or None
    if (
        status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"
        or _path_key(status.get("workspacePath", "")) != _path_key(workspace)
        or workspace.is_symlink()
        or not workspace.is_dir()
        or not shader_root.is_dir()
        or not is_within(shader_root, workspace)
        or not isinstance(capture, Mapping)
        or not isinstance(shader, Mapping)
        or not isinstance(baseline, Mapping)
        or not isinstance(source, Mapping)
        or capture.get("captureId") != capture_id
        or status_catalog != expected_catalog
        or shader.get("resourceId") != identity.get("resourceId")
        or shader.get("rawSha256") != identity.get("rawSha256")
        or baseline.get("rawSha256") != identity.get("rawSha256")
        or status.get("stage") != identity.get("stage")
        or status.get("entryPoint", "") != identity.get("entryPoint", "")
        or source.get("present") is not True
        or source.get("lane") != "executable-editable"
        or source.get("format") != "hlsl"
    ):
        raise RuntimeError("target Shader workspace changed physical identity")
    _hex(status.get("workspaceId"), 32, "target workspaceId")
    _hex(source.get("currentSha256"), 64, "target sourceSha256")
    return status


def _prepare_reuse_workspace(
    host: Any,
    active: Mapping[str, Any],
    identity: Mapping[str, Any],
    capture_session_id: str,
    catalog_sha256: str,
    *,
    expected_anchor_event_id: int,
    inventory_mode: bool = False,
) -> tuple[Path, dict[str, Any], str, str | None]:
    # Freeze the representative EID selected by the current unique-resource
    # inventory. Every later workspace admission and apply receipt must remain
    # bound to this exact anchor.
    event_id = _integer(
        expected_anchor_event_id, "expected Shader anchorEventId", 1
    )
    if event_id != _shader_anchor_event_id(identity):
        raise RuntimeError("target Shader inventory representative EID changed")
    located = _tool_json(
        host.get_active_shader_workspace_path(
            event_id, str(identity["stage"]),
            expected_resource_id=int(identity["resourceId"]),
            expected_entry_point=str(identity.get("entryPoint", "") or ""),
            expected_raw_sha256=str(identity["rawSha256"]),
            expected_capture_session_id=capture_session_id,
        ),
        "resolve target Shader workspace",
        schema="replaylab-active-shader-workspace-path/v1",
        schema_version=1,
    )
    located_catalog = str(located.get("catalogSha256") or "") or None
    if any(
        (
            located.get("captureId") is None,
            located.get("captureSessionId") != capture_session_id,
            located.get("eventId") != event_id,
            located.get("stage") != identity.get("stage"),
            located.get("entryPoint", "") != identity.get("entryPoint", ""),
            located.get("shaderResourceId") != identity.get("resourceId"),
            located.get("rawSha256") != identity.get("rawSha256"),
            (not inventory_mode and located_catalog != catalog_sha256),
            located.get("applied") is not False,
        )
    ):
        raise RuntimeError("target Shader workspace path changed identity")
    capture_id = _hex(located.get("captureId"), 64, "target captureId")
    raw_workspace = Path(str(located.get("workspacePath", ""))).expanduser()
    if not raw_workspace.is_absolute():
        raise RuntimeError("target Shader workspace path is not absolute")
    workspace = raw_workspace.resolve()
    shader_root = Path(
        str(active.get("paths", {}).get("shaderWorkspaces", ""))
    ).resolve()
    if not shader_root.is_dir() or not is_within(shader_root, workspace):
        raise RuntimeError("target Shader workspace escaped the active project")
    workspace_existed = workspace.exists()
    prepared = _tool_json(
        host.prepare_shader_workspace(
            event_id,
            str(identity["stage"]),
            str(workspace),
            expected_resource_id=int(identity["resourceId"]),
            expected_entry_point=str(identity.get("entryPoint", "") or ""),
            expected_raw_sha256=str(identity["rawSha256"]),
            expected_capture_id=capture_id,
            expected_capture_session_id=capture_session_id,
            admit_existing=workspace_existed,
        ),
        "prepare target Shader workspace",
        schema="renderdoc-ultra-shader-workspace-mcp/v1",
    )
    prepared_result = prepared.get("result")
    prepared_binding = (
        prepared_result.get("binding")
        if isinstance(prepared_result, Mapping)
        else None
    )
    prepared_created = (
        prepared_result.get("created")
        if isinstance(prepared_result, Mapping)
        else None
    )
    if (
        prepared.get("operation") != "prepare"
        or not isinstance(prepared_result, Mapping)
        or _path_key(prepared_result.get("workspacePath", ""))
        != _path_key(workspace)
        or not isinstance(prepared_binding, Mapping)
        or _path_key(prepared_binding.get("workspacePath", ""))
        != _path_key(workspace)
        or prepared_binding.get("captureId") != capture_id
        or prepared_binding.get("captureSessionId") != capture_session_id
        or prepared_binding.get("eventId") != event_id
        or prepared_binding.get("stage") != identity.get("stage")
        or prepared_binding.get("entryPoint", "")
        != identity.get("entryPoint", "")
        or prepared_binding.get("originalResourceId")
        != identity.get("resourceId")
        or prepared_binding.get("originalRawSha256")
        != identity.get("rawSha256")
        or not isinstance(prepared_created, Mapping)
        or prepared_created.get("admittedExisting") is not workspace_existed
        or prepared_created.get("sourcePreserved") is not workspace_existed
    ):
        raise RuntimeError(
            "target Shader workspace preparation changed the frozen representative EID"
        )
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        located_catalog,
    )
    return workspace, status, capture_id, located_catalog


def _compile_reuse_workspace(
    host: Any,
    active: Mapping[str, Any],
    workspace: Path,
    status: Mapping[str, Any],
    identity: Mapping[str, Any],
    capture_id: str,
    catalog_sha256: str | None,
    candidate_plan: Mapping[str, Any],
    progress: _Progress,
    position: int,
    total: int,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if control_checkpoint is not None:
        control_checkpoint("before reused Shader candidate status")
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after reused Shader candidate status")
    expected_source_sha256 = _hex(
        candidate_plan.get("sourceSha256"), 64, "reuse sourceSha256"
    )
    source = status.get("source")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if (
        not isinstance(source, Mapping)
        or source.get("currentSha256") != expected_source_sha256
        or not isinstance(target_profile, Mapping)
    ):
        raise RuntimeError("target Shader source/profile changed before candidate reuse")
    binary = candidate_plan.get("binaryCandidate")
    expected_compiler_binding_sha256: str | None = None
    if candidate_plan.get("binaryCandidateEligible") is True:
        source_revision = (
            binary.get("sourceRevision") if isinstance(binary, Mapping) else None
        )
        if (
            not isinstance(binary, Mapping)
            or binary.get("sourceSha256") != expected_source_sha256
            or isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision <= 0
            or binary.get("targetProfile") != target_profile
        ):
            raise RuntimeError(
                "project compiled candidate changed source revision or target profile"
            )
        expected_compiler_binding_sha256 = _hex(
            binary.get("compilerBindingSha256"),
            64,
            "project compiled candidate compilerBindingSha256",
        )
    progress_detail = {
        "resourceId": identity["resourceId"],
        "stage": identity["stage"],
        "knowledgeId": candidate_plan.get("knowledgeId"),
    }
    existing_candidate_current = isinstance(source, Mapping) and isinstance(
        latest, Mapping
    ) and (
        latest.get("sourceSha256") == source.get("currentSha256")
        and latest.get("revision") == status.get("revision")
        and isinstance(latest.get("revision"), int)
        and latest.get("revision") > 0
        and isinstance(latest.get("rawSha256"), str)
    )
    existing_project_candidate_exact = True
    if existing_candidate_current and candidate_plan.get("binaryCandidateEligible") is True:
        # A target workspace can survive within the same project even though its
        # most recent candidate was produced with a different compiler route or
        # flags. Source/profile equality is not enough to bypass the reviewed
        # project artifact: require the immutable binary and its import receipt
        # to match every executable/configuration field before reusing it.
        receipt = latest.get("projectImport")
        expected_code_sha256 = binary.get("codeSha256") if isinstance(binary, Mapping) else None
        existing_project_candidate_exact = bool(
            isinstance(binary, Mapping)
            and isinstance(receipt, Mapping)
            and latest.get("rawSha256") == binary.get("sha256")
            and latest.get("codeSha256") == expected_code_sha256
            and latest.get("byteSize") == binary.get("byteSize")
            and str(latest.get("kind", "")).lower()
            == str(binary.get("kind", "")).lower()
            and receipt.get("artifactSha256") == binary.get("sha256")
            and receipt.get("codeSha256") == expected_code_sha256
            and receipt.get("byteSize") == binary.get("byteSize")
            and str(receipt.get("kind", "")).lower()
            == str(binary.get("kind", "")).lower()
            and receipt.get("sourceSha256") == expected_source_sha256
            and receipt.get("sourceRevision") == binary.get("sourceRevision")
            and receipt.get("targetProfile") == binary.get("targetProfile")
            and receipt.get("compilerBindingSha256")
            == expected_compiler_binding_sha256
            and receipt.get("originValidationEvidenceOnly") is True
            and receipt.get("freshCaptureRebindRequired") is True
            and receipt.get("freshLiveAbiRevalidationRequired") is True
            and receipt.get("applyAuthorization") is False
            and receipt.get("oldReplacementIdReused") is False
            and receipt.get("oldCaptureAbiAuthorityReused") is False
        )
    if (existing_candidate_current and existing_project_candidate_exact
            and candidate_plan.get("sourcePreparationOnly") is not True):
        _hex(latest.get("rawSha256"), 64, "existing candidateRawSha256")
        progress.emit(
            "shader-candidate-load",
            "Reusing the target workspace Shader candidate %d/%d"
            % (position, total),
            current=position,
            total=total,
            detail=progress_detail,
        )
        return dict(status)
    workspace_service = (
        host._shader_workspace_service()
        if hasattr(host, "_shader_workspace_service")
        else None
    )
    importer = getattr(workspace_service, "import_project_candidate", None)
    if (
        candidate_plan.get("binaryCandidateEligible") is True
        and isinstance(binary, Mapping)
        and callable(importer)
    ):
        progress.emit(
            "shader-candidate-load",
            "Loading the verified project Shader candidate %d/%d"
            % (position, total),
            current=position,
            total=total,
            detail=progress_detail,
        )
        try:
            if control_checkpoint is not None:
                control_checkpoint("before project Shader candidate import")
            imported = importer(str(workspace), binary)
            if control_checkpoint is not None:
                control_checkpoint("after project Shader candidate import")
            if (
                not isinstance(imported, Mapping)
                or imported.get("workspacePath") != str(workspace)
                or imported.get("sourceSha256")
                != status.get("source", {}).get("currentSha256")
                or imported.get("compilerSkipped") is not True
                or imported.get("compiledFromProjectArtifact") is not True
                or imported.get("requiresReplayAbiValidation") is not True
                or imported.get("replayAbiValidated") is not False
                or imported.get("applied") is not False
            ):
                raise RuntimeError("project candidate import returned invalid evidence")
            _hex(imported.get("rawSha256"), 64, "imported candidateRawSha256")
            if control_checkpoint is not None:
                control_checkpoint("before imported Shader candidate status")
            after_import = _reuse_workspace_status(
                host,
                active,
                workspace,
                identity,
                capture_id,
                catalog_sha256,
            )
            if control_checkpoint is not None:
                control_checkpoint("after imported Shader candidate status")
            imported_latest = after_import.get("latestCompiled")
            imported_receipt = (
                imported_latest.get("projectImport")
                if isinstance(imported_latest, Mapping)
                else None
            )
            if (
                not isinstance(imported_latest, Mapping)
                or imported_latest.get("rawSha256") != imported.get("rawSha256")
                or imported_latest.get("rawSha256") != binary.get("sha256")
                or imported_latest.get("codeSha256") != binary.get("codeSha256")
                or imported_latest.get("byteSize") != binary.get("byteSize")
                or str(imported_latest.get("kind", "")).lower()
                != str(binary.get("kind", "")).lower()
                or imported_latest.get("sourceSha256")
                != after_import.get("source", {}).get("currentSha256")
                or imported_latest.get("revision") != after_import.get("revision")
                or not isinstance(imported_receipt, Mapping)
                or imported_receipt.get("artifactSha256") != binary.get("sha256")
                or imported_receipt.get("codeSha256") != binary.get("codeSha256")
                or imported_receipt.get("byteSize") != binary.get("byteSize")
                or str(imported_receipt.get("kind", "")).lower()
                != str(binary.get("kind", "")).lower()
                or imported_receipt.get("sourceSha256") != expected_source_sha256
                or imported_receipt.get("sourceRevision")
                != binary.get("sourceRevision")
                or imported_receipt.get("targetProfile")
                != binary.get("targetProfile")
                or imported_receipt.get("compilerBindingSha256")
                != expected_compiler_binding_sha256
                or imported_receipt.get("originValidationEvidenceOnly") is not True
                or imported_receipt.get("freshCaptureRebindRequired") is not True
                or imported_receipt.get("freshLiveAbiRevalidationRequired") is not True
                or imported_receipt.get("applyAuthorization") is not False
                or imported_receipt.get("oldReplacementIdReused") is not False
                or imported_receipt.get("oldCaptureAbiAuthorityReused") is not False
            ):
                raise RuntimeError(
                    "project candidate import did not bind the target workspace"
                )
            return after_import
        except RestoreControlStop:
            raise
        except Exception as exc:
            # A project binary is only a compile optimisation. The managed
            # workspace service imports it transactionally; rejection never
            # weakens the source-compile and fresh-live-ABI fallback.
            progress_detail = {
                **progress_detail,
                "fallbackReason": (
                    "%s: %s" % (type(exc).__name__, exc)
                )[:1024],
            }
    if control_checkpoint is not None:
        control_checkpoint("before fallback Shader compile status")
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after fallback Shader compile status")
    if status.get("source", {}).get("currentSha256") != candidate_plan.get(
        "sourceSha256"
    ):
        raise RuntimeError("target Shader source changed before fallback compile")
    progress.emit(
        "shader-compile",
        "Compiling project HLSL %d/%d" % (position, total),
        current=position,
        total=total,
        detail=progress_detail,
    )
    if control_checkpoint is not None:
        control_checkpoint("before fallback Shader compile")
    before_compile_revision = _integer(status.get("revision"), "pre-compile revision")
    compiled = _tool_json(
        host.compile_shader_workspace(str(workspace)),
        "compile reused Shader source",
        schema="renderdoc-ultra-shader-workspace-mcp/v1",
    )
    if control_checkpoint is not None:
        control_checkpoint("after fallback Shader compile")
    if compiled.get("operation") != "compile":
        raise RuntimeError("reused Shader compile returned invalid evidence")
    if control_checkpoint is not None:
        control_checkpoint("before compiled Shader candidate status")
    after = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after compiled Shader candidate status")
    source = after.get("source")
    latest = after.get("latestCompiled")
    if (
        not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or source.get("currentSha256") != candidate_plan.get("sourceSha256")
        or latest.get("sourceSha256") != source.get("currentSha256")
        or latest.get("revision") != after.get("revision")
        or (candidate_plan.get("sourcePreparationOnly") is True
            and latest.get("revision") != before_compile_revision + 1)
    ):
        raise RuntimeError("reused Shader compile did not publish a bound candidate")
    _hex(latest.get("rawSha256"), 64, "compiled candidateRawSha256")
    return after


def _reuse_material_record(
    prepared: Mapping[str, Any],
    identity_source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _normal_shader_identity(identity_source)
    staged = prepared["stagedCatalog"]
    catalog = _strict_fast_catalog(
        str(staged.get("catalogPath", "")), str(staged["catalogSha256"])
    )
    catalog_api = catalog.get("api", "")
    capture = catalog.get("capture")
    if not catalog_api and isinstance(capture, Mapping):
        catalog_api = capture.get("api", "")
    expected = _expected_resource_shader(
        catalog,
        identity_source,
        _asset_api(catalog_api),
        _shader_anchor_event_id(identity),
        inventory=inventory,
    )
    matches = [
        block
        for block in expected["constantBlocks"]
        if block.get("path") == candidate.get("constantBlockPath")
        and block.get("layoutSha256")
        == candidate.get("constantBlockLayoutSha256")
    ]
    if len(matches) != 1:
        raise RuntimeError("material reuse candidate has no unique target CBuffer layout")
    return {
        "knowledgeId": _hex(
            candidate.get("knowledgeId"), 32, "material knowledgeId"
        ),
        "shaderResourceId": identity["resourceId"],
        "stage": identity["stage"],
        "blockIndex": matches[0]["index"],
        "path": matches[0]["path"],
        "layoutSha256": matches[0]["layoutSha256"],
        "eventId": _shader_anchor_event_id(identity),
        "parameters": [],
    }


def _validate_alias_rollback_receipt(
    receipt: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema") != "renderdoc-ultra-alias-rollback/v1"
        or receipt.get("schemaVersion") != 1
        or receipt.get("captureId") != capture_id
        or receipt.get("captureSessionId") != capture_session_id
        or receipt.get("transactionId") != transaction_id
        or receipt.get("status") not in {"restored", "already-restored"}
        or receipt.get("restored") is not True
        or not isinstance(receipt.get("alreadyRestored"), bool)
        or receipt.get("verified") is not True
        or receipt.get("strictReceipt") is not True
        or receipt.get("captureTokenVerified") is not True
        or receipt.get("originalNamesVerified") is not True
        or receipt.get("recoveryRequired") is not False
        or receipt.get("overlayRestored") is not True
        or receipt.get("renameLedgerRestored") is not True
        or not isinstance(receipt.get("failures"), list)
        or receipt.get("failures")
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid strict alias rollback evidence"
        )
    return dict(receipt)


def _rollback_alias_transaction(
    host: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
) -> dict[str, Any]:
    receipt = host.ipc.call(
        "rollback_symbol_aliases",
        {
            "expected_capture_id": _hex(
                capture_id, 64, "alias rollback captureId"
            ),
            "expected_capture_session_id": _hex(
                capture_session_id, 32, "alias rollback captureSessionId"
            ),
            "expected_transaction_id": _hex(
                transaction_id, 32, "alias rollback transactionId"
            ),
        },
        timeout=60.0,
    )
    return _validate_alias_rollback_receipt(
        receipt,
        capture_id=capture_id,
        capture_session_id=capture_session_id,
        transaction_id=transaction_id,
    )


def _recover_uncertain_alias_load(
    host: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    query_error: Exception | None = None
    try:
        status = host.ipc.call(
            "get_symbol_alias_transaction",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
                "transaction_id": transaction_id,
            },
            timeout=30.0,
        )
        if (
            not isinstance(status, Mapping)
            or status.get("schema")
            != "renderdoc-ultra-alias-transaction-status/v1"
            or status.get("schemaVersion") != 1
            or status.get("captureId") != capture_id
            or status.get("captureSessionId") != capture_session_id
            or status.get("transactionId") not in {"", transaction_id}
            or status.get("state")
            not in {"absent", "pending", "committed", "rolled-back"}
            or not isinstance(status.get("mutationPresent"), bool)
            or not isinstance(status.get("recoveryRequired"), bool)
            or status.get("replayMutation") is not False
            or (
                status.get("state") in {"pending", "committed"}
                and (
                    status.get("transactionId") != transaction_id
                    or status.get("mutationPresent") is not True
                    or status.get("recoveryRequired") is not True
                    or status.get("manifestSha256") != manifest_sha256
                )
            )
            or (
                status.get("state") in {"absent", "rolled-back"}
                and status.get("mutationPresent") is not False
            )
        ):
            raise RuntimeError(
                "RenderDoc returned invalid strict alias transaction status"
            )
        if status.get("mutationPresent") is False:
            return dict(status)
    except Exception as exc:
        query_error = exc

    try:
        return _rollback_alias_transaction(
            host,
            capture_id=capture_id,
            capture_session_id=capture_session_id,
            transaction_id=transaction_id,
        )
    except Exception as rollback_exc:
        diagnostic = (
            "strict alias response recovery failed"
            + (
                "; query failed: %s: %s" % (type(query_error).__name__, query_error)
                if query_error is not None
                else ""
            )
            + "; rollback failed: %s: %s"
            % (type(rollback_exc).__name__, rollback_exc)
        )
        raise RestoreRollbackFailed(diagnostic[:4096]) from rollback_exc


def _rollback_restored_alias_transactions(
    host: Any, restored_alias_transactions: list[dict[str, str]]
) -> str:
    failures: list[str] = []
    retryable: list[dict[str, str]] = []
    while restored_alias_transactions:
        transaction = restored_alias_transactions.pop()
        try:
            _rollback_alias_transaction(
                host,
                capture_id=transaction["captureId"],
                capture_session_id=transaction["captureSessionId"],
                transaction_id=transaction["transactionId"],
            )
        except Exception as exc:
            failures.append("%s: %s" % (type(exc).__name__, exc))
            retryable.append(transaction)
    # Preserve failed entries in their original order. The Bridge retains the
    # same capture-bound ledger, so a later cleanup attempt can only retry the
    # exact transaction rather than losing its recovery authority.
    restored_alias_transactions.extend(reversed(retryable))
    return "; ".join(failures)[:4096]


def _restore_reuse_aliases(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    plan: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    inventory: Mapping[str, Any] | None = None,
    inventory_binding: Mapping[str, Any] | None = None,
    restored_alias_transactions: list[dict[str, str]] | None = None,
) -> None:
    targets: list[Mapping[str, Any]] = []
    candidate_count = 0
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        raw_candidates = match.get("aliasCandidates", [])
        if raw_candidates is None:
            raw_candidates = []
        if not isinstance(raw_candidates, list):
            raise RuntimeError("project alias candidates are not a bounded array")
        if not raw_candidates:
            continue
        if not isinstance(match.get("shader"), Mapping):
            raise RuntimeError("project alias target has no Shader identity")
        candidate_count += len(raw_candidates)
        if candidate_count > _MAX_RESTORE_ALIAS_CANDIDATES:
            raise RuntimeError("project alias candidate restore exceeds the safe limit")
        targets.append(match)
        if len(targets) > _MAX_RESTORE_ALIAS_TARGETS:
            raise RuntimeError("project alias target restore exceeds the safe limit")
    if not targets:
        return
    if control_checkpoint is not None:
        control_checkpoint("before project alias materialization")
    inventory_mode = plan.get("policy", {}).get("inventoryOnly") is True
    materialize_batch = getattr(replay, "materialize_alias_batch", None)
    materialize_inventory_batch = getattr(
        replay, "materialize_alias_batch_from_inventory", None
    )
    materialize = getattr(replay, "materialize_alias_candidates", None)
    if inventory_mode:
        if (
            not callable(materialize_inventory_batch)
            or not isinstance(inventory, Mapping)
            or not isinstance(inventory_binding, Mapping)
        ):
            raise RuntimeError(
                "Replay project has no verified inventory alias materialization route"
            )
    elif not callable(materialize_batch) and not callable(materialize):
        raise RuntimeError("Replay project has no verified alias materialization route")
    progress.emit(
        "alias-restore",
        "Rebinding verified Shader and CBuffer aliases",
        current=0,
        total=len(targets),
    )
    tables: list[dict[str, Any]] = []
    all_aliases: list[dict[str, Any]] = []
    all_shaders: list[dict[str, Any]] = []
    physical_targets: dict[tuple[Any, ...], str] = {}
    project_id = _hex(plan.get("projectId"), 32, "alias projectId")
    capture_id = _hex(plan.get("captureId"), 64, "alias captureId")
    catalog_sha256 = _hex(
        plan.get("catalogSha256"), 64, "alias catalogSha256"
    )
    materialization_targets: list[dict[str, Any]] = []
    inventory_shaders = (
        inventory.get("shaders", []) if isinstance(inventory, Mapping) else []
    )
    for match in targets:
        target_shader: Mapping[str, Any] = match["shader"]
        if inventory_mode:
            exact = [
                value
                for value in inventory_shaders
                if isinstance(value, Mapping)
                and value.get("resourceId") == target_shader.get("resourceId")
                and value.get("stableKeySha256")
                == target_shader.get("stableKeySha256")
            ]
            if len(exact) != 1:
                raise RuntimeError(
                    "project alias target is absent or ambiguous in the live inventory"
                )
            target_shader = exact[0]
        materialization_targets.append(
            {
                "shader": target_shader,
                "candidates": list(match.get("aliasCandidates", []) or []),
            }
        )
    if inventory_mode:
        batch = materialize_inventory_batch(
            project_id,
            inventory,
            inventory_binding,
            materialization_targets,
        )
        materializations = (
            batch.get("materializations") if isinstance(batch, Mapping) else None
        )
    elif callable(materialize_batch):
        batch = materialize_batch(
            project_id,
            capture_id,
            catalog_sha256,
            materialization_targets,
        )
        materializations = (
            batch.get("materializations") if isinstance(batch, Mapping) else None
        )
    else:
        materializations = []
        for match in targets:
            if control_checkpoint is not None:
                control_checkpoint("before project alias target materialization")
            materializations.append(
                materialize(
                    project_id,
                    capture_id,
                    catalog_sha256,
                    match["shader"],
                    list(match.get("aliasCandidates", []) or []),
                )
            )
            if control_checkpoint is not None:
                control_checkpoint("after project alias target materialization")
        batch = None
    if batch is not None and (
        not isinstance(batch, Mapping)
        or batch.get("schema")
        != "renderdoc-ultra-project-alias-materialization-batch/v1"
        or batch.get("schemaVersion") != 1
        or batch.get("projectId") != project_id
        or batch.get("captureId") != capture_id
        or batch.get("catalogSha256") != catalog_sha256
        or batch.get("targetCount") != len(targets)
        or batch.get("candidateCount") != candidate_count
        or batch.get("displayAliasesOnly") is not True
        or batch.get("applied") is not False
        or batch.get("projectChangedDuringBatch") is not False
        or (inventory_mode and batch.get("inventoryOnly") is not True)
        or not isinstance(materializations, list)
        or len(materializations) != len(targets)
    ):
        raise RuntimeError("project alias batch returned invalid evidence")
    if control_checkpoint is not None:
        control_checkpoint("after project alias materialization")
    for position, (match, materialized) in enumerate(
        zip(targets, materializations), 1
    ):
        table = materialized.get("aliasTable") if isinstance(materialized, Mapping) else None
        aliases = table.get("aliases") if isinstance(table, Mapping) else None
        shaders = table.get("shaders") if isinstance(table, Mapping) else None
        raw = materialized.get("jsonBytes") if isinstance(materialized, Mapping) else None
        if (
            not isinstance(materialized, Mapping)
            or materialized.get("schema")
            != "renderdoc-ultra-project-alias-materialization/v1"
            or materialized.get("schemaVersion") != 1
            or materialized.get("projectId") != project_id
            or materialized.get("captureId") != capture_id
            or materialized.get("catalogSha256") != catalog_sha256
            or materialized.get("displayAliasesOnly") is not True
            or (
                inventory_mode
                and materialized.get("inventoryOnly") is not True
            )
            or materialized.get("applied") is not False
            or not isinstance(raw, bytes)
            or hashlib.sha256(raw).hexdigest() != materialized.get("sha256")
            or not isinstance(table, Mapping)
            or table.get("schema") != "renderdoc-ultra-aliases/v1"
            or table.get("schemaVersion") != 1
            or table.get("captureId") != capture_id
            or table.get("catalogSha256") != catalog_sha256
            or table.get("verifiedOnly") is not True
            or not isinstance(aliases, list)
            or not aliases
            or len(aliases) != materialized.get("aliasCount")
            or not isinstance(shaders, list)
        ):
            raise RuntimeError("project alias materialization returned invalid evidence")
        for alias in aliases:
            if not isinstance(alias, Mapping) or alias.get("verified") is not True:
                raise RuntimeError("project alias materialization emitted an unverified alias")
            kind = str(alias.get("bindingKind", "") or "").strip().lower()
            target: tuple[Any, ...] = (
                kind,
                alias.get("shaderResourceId"),
                alias.get("stage"),
                alias.get("entryPoint", ""),
                alias.get("rawSha256", ""),
            )
            if kind in {"constantbuffer", "constantfield"}:
                target += (
                    alias.get("registerSpace", 0),
                    alias.get("bindNumber", 0),
                )
            if kind == "constantfield":
                target += (alias.get("byteOffset", 0),)
            name = str(alias.get("alias", "") or "")
            previous = physical_targets.setdefault(target, name)
            if previous != name:
                raise RuntimeError(
                    "project aliases conflict for one target-capture binding"
                )
            all_aliases.append(copy.deepcopy(dict(alias)))
            if len(all_aliases) > _MAX_RESTORE_ALIASES:
                raise RuntimeError("project alias restore exceeds the loader alias limit")
        all_shaders.extend(copy.deepcopy(list(shaders)))
        tables.append(copy.deepcopy(dict(table)))
        progress.emit(
            "alias-restore",
            "Rebinding verified Shader and CBuffer aliases %d/%d"
            % (position, len(targets)),
            current=position,
            total=len(targets),
            detail={"aliasCount": len(all_aliases)},
        )
    if isinstance(batch, Mapping) and batch.get("aliasCount") != len(all_aliases):
        raise RuntimeError("project alias batch alias count is inconsistent")
    merged = copy.deepcopy(tables[0])
    merged["aliases"] = all_aliases
    merged["shaders"] = all_shaders
    merged["projectMaterialization"] = {
        "schema": "renderdoc-ultra-project-alias-materialization/v1",
        "schemaVersion": 1,
        "projectId": project_id,
        "captureId": capture_id,
        "catalogSha256": catalog_sha256,
        "targetCount": len(targets),
        "displayAliasesOnly": True,
        "captureLocalReplayStateReused": False,
    }
    encoded = (
        json.dumps(
            merged,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if not encoded or len(encoded) > 32 * 1024 * 1024:
        raise RuntimeError("merged project alias table exceeds the loader limit")
    digest = hashlib.sha256(encoded).hexdigest()
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )
    aliases_root = host._active_project_service().shared_dictionary_directory(
        "aliases"
    )
    if aliases_root.is_symlink() or not aliases_root.is_dir():
        raise RuntimeError("active project alias dictionary is unavailable")
    aliases_root = aliases_root.resolve()
    alias_path = (
        aliases_root
        / ("%s-%s.project-reuse.json" % (capture_id[:16], digest))
    ).resolve()
    if not is_within(aliases_root, alias_path) or alias_path.is_symlink():
        raise RuntimeError("capture-local alias path escaped the project")
    atomic_write_bytes(alias_path, encoded)
    if (
        alias_path.is_symlink()
        or not alias_path.is_file()
        or hashlib.sha256(alias_path.read_bytes()).hexdigest() != digest
    ):
        raise RuntimeError("capture-local alias table failed post-write verification")
    if control_checkpoint is not None:
        control_checkpoint("before capture-local alias overlay load")
    progress.emit(
        "resource-anchor",
        "Validating one representative EID per Shader",
        current=0,
        total=len(targets),
        detail={
            "purpose": "Shader and CBuffer alias validation",
            "eventTraversalCount": 0,
        },
    )
    alias_operation_id = uuid.uuid4().hex
    try:
        overlay = host.ipc.call(
            "load_symbol_aliases",
            {
                "path": str(alias_path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
                "operation_id": alias_operation_id,
            },
            timeout=60.0,
        )
        counts = overlay.get("counts") if isinstance(overlay, Mapping) else None
        if (
            not isinstance(overlay, dict)
            or overlay.get("schema") != "renderdoc-ultra-aliases/v1"
            or overlay.get("schemaVersion") != 1
            or overlay.get("captureId") != capture_id
            or overlay.get("captureSessionId") != capture_session_id
            or overlay.get("operationId") != alias_operation_id
            or overlay.get("transactionId") != alias_operation_id
            or overlay.get("confirmedOnly") is not True
            or overlay.get("strictAtomic") is not True
            or overlay.get("committed") is not True
            or overlay.get("recoveryRequired") is not False
            or overlay.get("rollbackAvailable") is not True
            or overlay.get("recoveryLedgerPublished") is not True
            or not isinstance(counts, Mapping)
            or counts.get("manifestAliases") != len(all_aliases)
            or counts.get("activeValidatedAliases") != len(all_aliases)
            or counts.get("skipped") != 0
            or counts.get("conflicts") != 0
        ):
            raise RuntimeError(
                "RenderDoc rejected one or more verified project aliases"
            )
    except Exception as load_exc:
        try:
            recovery = _recover_uncertain_alias_load(
                host,
                capture_id=capture_id,
                capture_session_id=capture_session_id,
                transaction_id=alias_operation_id,
                manifest_sha256=digest,
            )
        except RestoreRollbackFailed:
            raise
        raise RuntimeError(
            "capture-local alias overlay failed or returned invalid evidence; "
            "transaction %s was fail-safe recovered with status %s (%s: %s)"
            % (
                alias_operation_id,
                recovery.get("status"),
                type(load_exc).__name__,
                load_exc,
            )
        ) from load_exc
    if restored_alias_transactions is not None:
        restored_alias_transactions.append(
            {
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "transactionId": alias_operation_id,
            }
        )
    if control_checkpoint is not None:
        control_checkpoint("after capture-local alias overlay load")
    progress.emit(
        "resource-anchor",
        "Representative Shader EID validation completed",
        current=len(targets),
        total=len(targets),
        detail={
            "purpose": "Shader and CBuffer alias validation",
            "eventTraversalCount": 0,
        },
    )
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )


def _restore_reuse_aliases_best_effort(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    plan: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    inventory: Mapping[str, Any] | None = None,
    inventory_binding: Mapping[str, Any] | None = None,
    restored_alias_transactions: list[dict[str, str]] | None = None,
) -> bool:
    """Restore display-only aliases without blocking executable reuse."""

    try:
        _restore_reuse_aliases(
            host,
            replay,
            active,
            project_root,
            capture_path,
            capture_session_id,
            plan,
            matches,
            progress,
            control_checkpoint,
            inventory,
            inventory_binding,
            restored_alias_transactions,
        )
        return True
    except RestoreRollbackFailed:
        raise
    except RestoreControlStop:
        raise
    except Exception as exc:
        # Never downgrade an active capture/project lifecycle failure to a
        # cosmetic warning. Revalidation raises here when the context changed.
        _require_restore_context(
            host, active, project_root, capture_path, capture_session_id
        )
        diagnostic = (
            "Project display aliases were not restored: %s: %s"
            % (type(exc).__name__, exc)
        )[:4096]
        if diagnostic not in progress.warnings:
            progress.warnings.append(diagnostic)
        progress.emit(
            "alias-restore",
            "Project display aliases could not be restored; continuing with Shader and CBuffer reuse",
            detail={"status": "failed", "error": diagnostic[:1024]},
        )
        return False


def _restore_from_reuse_plan(
    host: Any,
    replay: ReplayProjectService,
    active: dict[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    prepared: Mapping[str, Any],
    inventory: Mapping[str, Any],
    progress: _Progress,
    shader_summary: dict[str, Any],
    material_summary: dict[str, Any],
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
    restored_material_sessions: list[tuple[str, str, str]],
    restored_alias_transactions: list[dict[str, str]],
    control_checkpoint: Callable[[str], None] | None = None,
    realized_receipts: list[dict[str, Any]] | None = None,
    receipt_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Materialize target-local workspaces/sessions from an exact reuse plan."""

    if realized_receipts is None:
        realized_receipts = []

    plan = prepared["reusePlan"]
    if control_checkpoint is not None:
        control_checkpoint("before prepared project binding snapshot")
    binding_state = _prepared_binding_state(replay, active, prepared)
    if control_checkpoint is not None:
        control_checkpoint("after prepared project binding snapshot")
    matches = list(plan.get("matches", []) or [])
    normalised_matches: list[
        tuple[
            Mapping[str, Any],
            dict[str, Any],
            list[Mapping[str, Any]],
        ]
    ] = []
    executable_resource_match_counts: dict[int, int] = {}
    for match in matches:
        if not isinstance(match, Mapping) or not isinstance(
            match.get("shader"), Mapping
        ):
            raise RuntimeError("project reuse plan contains an invalid Shader match")
        identity = _normal_shader_identity(match["shader"])
        workspace_candidates = [
            item
            for item in list(match.get("workspaceCandidates", []) or [])
            if isinstance(item, Mapping)
        ]
        active_candidates = [
            item
            for item in workspace_candidates
            if item.get("activeCandidate") is True
        ]
        if not active_candidates and len(workspace_candidates) == 1:
            active_candidates = [workspace_candidates[0]]
        if (
            len(active_candidates) == 1
            and active_candidates[0].get("sourceSeedEligible") is True
        ):
            resource_id = identity["resourceId"]
            executable_resource_match_counts[resource_id] = (
                executable_resource_match_counts.get(resource_id, 0) + 1
            )
        normalised_matches.append((match, identity, active_candidates))
    successful_shader_records: list[dict[str, Any]] = []
    successful_material_records: list[dict[str, Any]] = []
    capture_id = str(plan["captureId"])
    catalog_sha256 = str(plan["catalogSha256"])
    inventory_mode = (
        prepared.get("schema") == "replaylab-active-shader-inventory-prepare/v1"
    )

    _restore_reuse_aliases_best_effort(
        host,
        replay,
        active,
        project_root,
        capture_path,
        capture_session_id,
        plan,
        matches,
        progress,
        control_checkpoint,
        prepared.get("inventory") if inventory_mode else None,
        prepared.get("binding") if inventory_mode else None,
        restored_alias_transactions,
    )

    for position, (match, identity, active_candidates) in enumerate(
        normalised_matches, 1
    ):
        if control_checkpoint is not None:
            control_checkpoint("before project reuse-plan Shader match")
        _capture_shader(binding_state, identity)
        expected_anchor_event_id = _shader_anchor_event_id(identity)
        # A resource-level replacement may run at most once for one physical
        # ResourceId.  Multiple entry points may all keep their project HLSL
        # and CBuffer alias metadata, but two executable candidates for the
        # same module are ambiguous and therefore fail closed as a group.
        executable_conflict = (
            executable_resource_match_counts.get(identity["resourceId"], 0) > 1
        )
        if executable_conflict:
            active_candidates = []
        if len(active_candidates) != 1:
            shader_summary["skipped"] += 1
            shader_summary["items"].append(
                {
                    "status": "skipped",
                    "source": "reuse-plan",
                    "resourceId": identity["resourceId"],
                    "stage": identity["stage"],
                    "reason": (
                        "multiple executable entry points target one physical Shader resource"
                        if executable_conflict
                        else (
                            "project-shader-stable-identity-mismatch"
                            if match.get("sourcePreparationBlockedReason")
                            == "project-shader-stable-identity-mismatch"
                            else "no unique active project HLSL candidate matched"
                        )
                    ),
                }
            )
        else:
            candidate = active_candidates[0]
            source_preparation_only = candidate.get("sourcePreparationOnly") is True
            knowledge_id = _hex(
                candidate.get("knowledgeId"), 32, "workspace knowledgeId"
            )
            knowledge = binding_state.get("workspaces", {}).get(knowledge_id)
            if (
                candidate.get("sourceSeedEligible") is not True
                or identity.get("anchorEventId") is None
                or not isinstance(knowledge, Mapping)
                or (source_preparation_only and (
                    not inventory_mode
                    or plan.get("policy", {}).get("rawExactSourcePreparationAllowed") is not True
                    or plan.get("policy", {}).get("stableKeyExactRequiredForBinary") is not True
                    or not _source_preparation_matches_shader(knowledge, identity, candidate)
                ))
                or (not source_preparation_only and (
                    identity.get("stableKeyComplete") is not True
                    or not _knowledge_matches_shader(knowledge, identity, executable=True)
                ))
                or knowledge.get("sourceSha256") != candidate.get("sourceSha256")
            ):
                shader_summary["skipped"] += 1
                shader_summary["items"].append(
                    {
                        "status": "skipped",
                        "source": "reuse-plan",
                        "resourceId": identity["resourceId"],
                        "stage": identity["stage"],
                        "reason": "matched project HLSL is not executable source-seed knowledge",
                    }
                )
            else:
                shader_summary["attempted"] += 1
                progress.emit(
                    "shader-prepare",
                    "Preparing project HLSL %d/%d" % (position, len(matches)),
                    current=position,
                    total=len(matches),
                    detail={
                        "resourceId": identity["resourceId"],
                        "stage": identity["stage"],
                        "knowledgeId": knowledge_id,
                    },
                )
                try:
                    progress.emit(
                        "resource-anchor",
                        "Validating one representative EID per Shader",
                        current=position,
                        total=len(matches),
                        item="Shader ResourceId %d" % identity["resourceId"],
                        detail={
                            "resourceId": identity["resourceId"],
                            "anchorEventId": expected_anchor_event_id,
                            "eventTraversalCount": 0,
                        },
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("before target Shader workspace preparation")
                    (
                        workspace,
                        status,
                        live_capture_id,
                        live_catalog_sha256,
                    ) = _prepare_reuse_workspace(
                        host,
                        active,
                        identity,
                        capture_session_id,
                        catalog_sha256,
                        expected_anchor_event_id=expected_anchor_event_id,
                        inventory_mode=inventory_mode,
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("after target Shader workspace preparation")
                    if source_preparation_only:
                        # The normal public prepare path must have imported the
                        # exact active project source. Never fall back to an old
                        # seed, reverse-engineer it again, or adopt its grant.
                        _validate_prepared_project_source(status, knowledge, identity, binding_state)
                        _prepared_binding_state(replay, active, prepared)
                    source = status["source"]
                    if source.get("currentSha256") != candidate.get("sourceSha256"):
                        if source.get("modifiedSinceDecompile") is not False:
                            raise RuntimeError(
                                "target Shader workspace contains a different user edit"
                            )
                        if control_checkpoint is not None:
                            control_checkpoint("before target Shader source seed")
                        if inventory_mode:
                            project_inventory = prepared.get("inventory")
                            expected_binding = prepared.get("binding")
                            target_shader = None
                            if isinstance(project_inventory, Mapping):
                                for value in project_inventory.get("shaders", []):
                                    if (
                                        isinstance(value, Mapping)
                                        and value.get("resourceId")
                                        == identity.get("resourceId")
                                        and value.get("stableKeySha256")
                                        == identity.get("stableKeySha256")
                                    ):
                                        if target_shader is not None:
                                            raise RuntimeError(
                                                "target Shader inventory identity is ambiguous"
                                            )
                                        target_shader = value
                            if (
                                target_shader is None
                                or not isinstance(expected_binding, Mapping)
                            ):
                                raise RuntimeError(
                                    "target Shader inventory binding is incomplete"
                                )
                            seeded = replay.seed_workspace_from_inventory(
                                knowledge_id,
                                status,
                                target_shader,
                                project_inventory,
                                expected_binding,
                            )
                            if (
                                not isinstance(seeded, Mapping)
                                or seeded.get("schema")
                                != "renderdoc-ultra-project-workspace-inventory-seed/v1"
                                or seeded.get("schemaVersion") != 1
                                or seeded.get("captureId") != capture_id
                                or seeded.get("catalogSha256") != catalog_sha256
                                or seeded.get("targetResourceId")
                                != identity.get("resourceId")
                                or seeded.get("targetStableKeySha256")
                                != identity.get("stableKeySha256")
                                or seeded.get("workspacePath") != str(workspace)
                                or seeded.get("applied") is not False
                            ):
                                raise RuntimeError(
                                    "target Shader inventory seed changed identity"
                                )
                        else:
                            seeded = _tool_json(
                                host.seed_active_shader_workspace(
                                    knowledge_id, str(workspace)
                                ),
                                "seed target Shader workspace",
                                schema="replaylab-active-workspace-seed/v1",
                                schema_version=1,
                            )
                        if control_checkpoint is not None:
                            control_checkpoint("after target Shader source seed")
                        if not inventory_mode and (
                            seeded.get("captureId") != capture_id
                            or seeded.get("captureSessionId") != capture_session_id
                            or seeded.get("workspacePath") != str(workspace)
                            or seeded.get("applied") is not False
                        ):
                            raise RuntimeError(
                                "target Shader source seed changed capture identity"
                            )
                        status = _reuse_workspace_status(
                            host,
                            active,
                            workspace,
                            identity,
                            live_capture_id,
                            live_catalog_sha256,
                        )
                    if status["source"].get("currentSha256") != candidate.get(
                        "sourceSha256"
                    ):
                        raise RuntimeError("target Shader source seed hash mismatch")
                    _require_restore_context(
                        host,
                        active,
                        project_root,
                        capture_path,
                        capture_session_id,
                    )
                    status = _compile_reuse_workspace(
                        host,
                        active,
                        workspace,
                        status,
                        identity,
                        live_capture_id,
                        live_catalog_sha256,
                        candidate,
                        progress,
                        position,
                        len(matches),
                        control_checkpoint,
                    )
                    latest = status["latestCompiled"]
                    revision = _integer(
                        latest.get("revision"), "candidateRevision", 1
                    )
                    candidate_raw = _hex(
                        latest.get("rawSha256"), 64, "candidateRawSha256"
                    )
                    if source_preparation_only:
                        _validate_prepared_project_source(status, knowledge, identity, binding_state)
                        _prepared_binding_state(replay, active, prepared)
                    _require_restore_context(
                        host,
                        active,
                        project_root,
                        capture_path,
                        capture_session_id,
                    )
                    progress.emit(
                        "shader-apply",
                        "Validating ABI and applying project Shader %d/%d"
                        % (position, len(matches)),
                        current=position,
                        total=len(matches),
                        detail={
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "workspacePath": str(workspace),
                        },
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("before project Shader apply")
                    apply_record = {
                        "identity": copy.deepcopy(identity),
                        "managedWorkspace": {
                            "workspaceId": status["workspaceId"],
                            "path": workspace.relative_to(project_root).as_posix(),
                        },
                        "candidateRevision": revision,
                        "candidateRawSha256": candidate_raw,
                    }
                    applied = None if source_preparation_only else _adopt_exact_live_replacement(
                        host,
                        workspace,
                        status,
                        apply_record,
                        capture_session_id,
                    )
                    adopted = applied is not None
                    if applied is None:
                        applied = _apply_workspace(
                            host,
                            workspace,
                            status,
                            expected_revision=revision,
                            expected_candidate_raw_sha256=candidate_raw,
                            expected_anchor_event_id=expected_anchor_event_id,
                            capture_session_id=capture_session_id,
                        )
                    operation_created = bool(
                        not adopted
                        and applied.get("replacement", {}).get(
                            "operationCreatedReplacement"
                        )
                        is True
                    )
                    try:
                        after = _reuse_workspace_status(
                            host,
                            active,
                            workspace,
                            identity,
                            live_capture_id,
                            live_catalog_sha256,
                        )
                        _require_restore_context(
                            host,
                            active,
                            project_root,
                            capture_path,
                            capture_session_id,
                        )
                        if source_preparation_only:
                            _validate_prepared_project_source(after, knowledge, identity, binding_state)
                            _prepared_binding_state(replay, active, prepared)
                        if inventory_mode and not source_preparation_only:
                            project_inventory = prepared.get("inventory")
                            live_inventory = prepared.get("liveInventory")
                            expected_binding = prepared.get("binding")
                            if not all(
                                isinstance(value, Mapping)
                                for value in (
                                    project_inventory,
                                    live_inventory,
                                    expected_binding,
                                )
                            ):
                                raise RuntimeError(
                                    "project Shader inventory publication binding is incomplete"
                                )
                            exact_targets = [
                                value
                                for value in project_inventory.get("shaders", [])
                                if isinstance(value, Mapping)
                                and value.get("resourceId")
                                == identity.get("resourceId")
                                and value.get("stableKeySha256")
                                == identity.get("stableKeySha256")
                            ]
                            if len(exact_targets) != 1:
                                raise RuntimeError(
                                    "project Shader inventory publication target is ambiguous"
                                )
                            published = replay.register_workspace_from_inventory(
                                after,
                                exact_targets[0],
                                project_inventory,
                                expected_binding,
                            )
                            published_knowledge = (
                                published.get("knowledge")
                                if isinstance(published, Mapping)
                                else None
                            )
                            published_binding = (
                                published.get("binding")
                                if isinstance(published, Mapping)
                                else None
                            )
                            if (
                                not isinstance(published_knowledge, Mapping)
                                or not isinstance(published_binding, Mapping)
                                or published.get("applied") is not False
                            ):
                                raise RuntimeError(
                                    "project rejected the validated Shader artifact publication"
                                )
                            knowledge_id = _hex(
                                published_knowledge.get("knowledgeId"),
                                32,
                                "published workspace knowledgeId",
                            )
                            refreshed = replay.validate_shader_inventory_snapshot(
                                project_inventory, published_binding
                            )
                            binding_state = _inventory_binding_state(
                                refreshed, live_inventory
                            )
                            published_state = binding_state.get(
                                "workspaces", {}
                            ).get(knowledge_id)
                            if (
                                not isinstance(published_state, Mapping)
                                or not _knowledge_matches_shader(
                                    published_state, identity, executable=True
                                )
                                or published_state.get("sourceSha256")
                                != after.get("source", {}).get("currentSha256")
                                or not isinstance(
                                    published_state.get("compiledCandidate"),
                                    Mapping,
                                )
                                or published_state["compiledCandidate"].get(
                                    "rawSha256"
                                )
                                != candidate_raw
                            ):
                                raise RuntimeError(
                                    "published project Shader artifact differs from live validation"
                                )
                            if isinstance(prepared, dict):
                                prepared["binding"] = copy.deepcopy(
                                    dict(published_binding)
                                )
                                prepared["knowledge"] = copy.deepcopy(
                                    dict(refreshed.get("knowledge", {}))
                                )
                        record = _shader_record_from_status(
                            project_root,
                            identity,
                            knowledge_id,
                            workspace,
                            after,
                            (
                                binding_state.get("workspaces", {}).get(
                                    knowledge_id
                                )
                                if inventory_mode and not source_preparation_only
                                else None
                            ),
                        )
                    except Exception:
                        if operation_created:
                            try:
                                _rollback_applied_workspace(
                                    host,
                                    workspace,
                                    status,
                                    applied,
                                    capture_session_id,
                                )
                            except Exception as rollback_exc:
                                raise RestoreRollbackFailed(
                                    "restored Shader post-apply validation failed and rollback failed: %s: %s"
                                    % (type(rollback_exc).__name__, rollback_exc)
                                ) from rollback_exc
                        raise
                    if operation_created:
                        applied_workspaces.append(
                            (
                                workspace,
                                copy.deepcopy(dict(after)),
                                copy.deepcopy(dict(applied)),
                                capture_session_id,
                            )
                        )
                    if source_preparation_only:
                        record["sourcePreparationOnly"] = True
                        # This is the freshly validated current-session apply
                        # observation, not the complete-key executable cache
                        # receipt. It is counted but never persisted in RDC.
                        receipt = {"sourcePreparationOnly": True,
                                   "identity": copy.deepcopy(identity),
                                   "replacement": copy.deepcopy(dict(applied["replacement"]))}
                    else:
                        receipt = _realized_receipt_from_apply(record, applied)
                    realized_receipts.append(receipt)
                    if receipt_stats is not None:
                        receipt_stats["produced"] += 1
                        if adopted:
                            receipt_stats["adoptedCurrentSession"] += 1
                        elif operation_created:
                            receipt_stats["mutationsCreated"] += 1
                        else:
                            receipt_stats["idempotentApplyResponses"] += 1
                    if control_checkpoint is not None:
                        control_checkpoint("after project Shader apply")
                    successful_shader_records.append(record)
                    shader_summary["applied"] += 1
                    shader_summary["items"].append(
                        {
                            "status": "applied",
                            "source": "reuse-plan",
                            "workspacePath": str(workspace),
                            "workspaceId": after["workspaceId"],
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "replacementId": applied["replacement"][
                                "replacementId"
                            ],
                            "idempotentlyAdopted": adopted,
                            "knowledgeId": knowledge_id,
                        }
                    )
                except (
                    RestoreContextChanged,
                    RestoreRollbackFailed,
                    RestoreControlStop,
                ):
                    raise
                except Exception as exc:
                    shader_summary["failed"] += 1
                    shader_summary["items"].append(
                        {
                            "status": "failed",
                            "source": "reuse-plan",
                            "knowledgeId": knowledge_id,
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "error": "%s: %s" % (type(exc).__name__, exc),
                        }
                    )
                _require_restore_context(
                    host,
                    active,
                    project_root,
                    capture_path,
                    capture_session_id,
                )

        # CBuffer names and field aliases are installed by the project alias
        # overlay above.  Event-scoped numeric values/material snapshots are
        # deliberately not restored while opening an RDC: they require the
        # current EID (or an explicit material-snapshot action) on demand.
        if control_checkpoint is not None:
            control_checkpoint("after project reuse-plan Shader match")
    return successful_shader_records, successful_material_records, binding_state


def _legacy_material_shader_identity(
    shader_identities: Mapping[tuple[Any, Any], Sequence[Mapping[str, Any]]],
    record: Mapping[str, Any],
) -> Mapping[str, Any]:
    identity_candidates = shader_identities.get(
        (record.get("shaderResourceId"), record.get("stage"))
    )
    if not identity_candidates:
        raise RuntimeError("material association has no Shader identity")
    if len(identity_candidates) != 1:
        # Legacy material rows predate entryPoint/stable-key binding.  Never
        # guess when one physical resource exposes more than one entry point.
        raise RuntimeError(
            "legacy material association is ambiguous across Shader entry points"
        )
    return identity_candidates[0]


def _restore_from_cache(
    host: Any,
    replay: ReplayProjectService,
    active: dict[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    binding: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
    progress: _Progress,
    shader_summary: dict[str, Any],
    material_summary: dict[str, Any],
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
    restored_material_sessions: list[tuple[str, str, str]],
    restored_alias_transactions: list[dict[str, str]],
    control_checkpoint: Callable[[str], None] | None = None,
    realized_receipts: list[dict[str, Any]] | None = None,
    receipt_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if realized_receipts is None:
        realized_receipts = []
    records = list(binding.get("shaders", []) or [])
    seen_workspaces: set[str] = set()
    seen_resource_ids: set[int] = set()
    preflight_records: list[tuple[Mapping[str, Any], str]] = []
    # Reject an ambiguous legacy/forged cache before validating even the first
    # workspace.  Validation is read-only today, but keeping the entire cache
    # preflight ahead of every external call prevents future validation hooks
    # from creating a partial side effect before the duplicate is discovered.
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(
            record.get("identity"), Mapping
        ):
            raise RuntimeError("RDC index contains a malformed Shader record")
        resource_id = _normal_shader_identity(record["identity"])["resourceId"]
        if resource_id in seen_resource_ids:
            raise RuntimeError(
                "RDC index maps one physical Shader resource more than once"
            )
        seen_resource_ids.add(resource_id)
        workspace_id = str(record.get("managedWorkspace", {}).get("workspaceId", ""))
        if workspace_id in seen_workspaces:
            raise RuntimeError("RDC index maps one managed workspace more than once")
        seen_workspaces.add(workspace_id)
        preflight_records.append((record, workspace_id))

    validated: list[tuple[Mapping[str, Any], Path, dict[str, Any]]] = []
    for index, (record, workspace_id) in enumerate(preflight_records, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader workspace validation")
        progress.emit(
            "cache-resource",
            "Validating cached Shader asset %d/%d" % (index, len(records)),
            current=index,
            total=len(records),
            detail={"workspaceId": workspace_id},
        )
        workspace, status = _validate_workspace_record(
            host,
            replay,
            active,
            project_root,
            capture_path,
            binding_state,
            record,
        )
        validated.append((record, workspace, status))
        if control_checkpoint is not None:
            control_checkpoint("after cached Shader workspace validation")

    _match_workspace_resources(
        host,
        capture_path,
        capture_session_id,
        validated,
        control_checkpoint,
        inventory=inventory,
        progress=progress,
    )
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )

    # The qrenderdoc alias overlay is capture-session-local and is reset every
    # time an RDC is opened. The RDC index deliberately stores routing
    # evidence, not display aliases, so rematerialize the current project's
    # target-bound aliases before any cached Shader replacement is applied.
    if control_checkpoint is not None:
        control_checkpoint("before cached project alias reuse plan")
    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        plan = copy.deepcopy(binding_state.get("reusePlan"))
    else:
        plan = replay.reuse_plan(
            str(binding_state["captureId"]),
            str(binding_state["catalogSha256"]),
        )
    if control_checkpoint is not None:
        control_checkpoint("after cached project alias reuse plan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    if (
        not isinstance(plan, Mapping)
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("projectId") != active.get("replayProject", {}).get("projectId")
        or plan.get("captureId") != binding_state.get("captureId")
        or plan.get("catalogSha256") != binding_state.get("catalogSha256")
        or not isinstance(matches, list)
        or len(matches) > _MAX_FAST_CATALOG_SHADERS
        or plan.get("matchedShaderCount") != len(matches)
    ):
        raise RuntimeError("cached project alias reuse plan changed project binding")
    _restore_reuse_aliases_best_effort(
        host,
        replay,
        active,
        project_root,
        capture_path,
        capture_session_id,
        plan,
        matches,
        progress,
        control_checkpoint,
        binding_state.get("projectInventory"),
        binding_state.get("inventoryBinding"),
        restored_alias_transactions,
    )

    successful_shader_records: list[dict[str, Any]] = []
    for index, (record, workspace, status) in enumerate(validated, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader apply")
        shader_summary["attempted"] += 1
        _require_restore_context(
            host, active, project_root, capture_path, capture_session_id
        )
        progress.emit(
            "shader-apply",
            "Applying cached Shader %d/%d" % (index, len(validated)),
            current=index,
            total=len(validated),
            detail={
                "workspacePath": str(workspace),
                "resourceId": record["identity"]["resourceId"],
                "stage": record["identity"]["stage"],
            },
        )
        try:
            applied = _adopt_exact_live_replacement(
                host, workspace, status, record, capture_session_id
            )
            adopted = applied is not None
            if applied is None:
                applied = _apply_workspace(
                    host,
                    workspace,
                    status,
                    expected_revision=int(record["candidateRevision"]),
                    expected_candidate_raw_sha256=str(record["candidateRawSha256"]),
                    expected_anchor_event_id=_shader_anchor_event_id(
                        _normal_shader_identity(record["identity"])
                    ),
                    capture_session_id=capture_session_id,
                )
            operation_created = bool(
                not adopted
                and applied.get("replacement", {}).get(
                    "operationCreatedReplacement"
                )
                is True
            )
            try:
                _require_restore_context(
                    host, active, project_root, capture_path, capture_session_id
                )
            except Exception:
                if operation_created:
                    try:
                        _rollback_applied_workspace(
                            host,
                            workspace,
                            status,
                            applied,
                            capture_session_id,
                        )
                    except Exception as rollback_exc:
                        raise RestoreRollbackFailed(
                            "cached Shader context changed and rollback failed: %s: %s"
                            % (type(rollback_exc).__name__, rollback_exc)
                        ) from rollback_exc
                raise
            if operation_created:
                applied_workspaces.append(
                    (
                        workspace,
                        copy.deepcopy(dict(status)),
                        copy.deepcopy(dict(applied)),
                        capture_session_id,
                    )
                )
            receipt = _realized_receipt_from_apply(record, applied)
            realized_receipts.append(receipt)
            if receipt_stats is not None:
                receipt_stats["produced"] += 1
                if adopted:
                    receipt_stats["adoptedCurrentSession"] += 1
                elif operation_created:
                    receipt_stats["mutationsCreated"] += 1
                else:
                    receipt_stats["idempotentApplyResponses"] += 1
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader apply")
            shader_summary["applied"] += 1
            item = {
                "status": "applied",
                "source": "rdc-index",
                "workspacePath": str(workspace),
                "workspaceId": status["workspaceId"],
                "resourceId": record["identity"]["resourceId"],
                "stage": record["identity"]["stage"],
                "replacementId": applied["replacement"]["replacementId"],
                "idempotentlyAdopted": adopted,
            }
            shader_summary["items"].append(item)
            successful_shader_records.append(copy.deepcopy(dict(record)))
        except (
            RestoreContextChanged,
            RestoreRollbackFailed,
            RestoreControlStop,
        ):
            raise
        except Exception as exc:
            shader_summary["failed"] += 1
            shader_summary["items"].append(
                {
                    "status": "failed",
                    "source": "rdc-index",
                    "workspacePath": str(workspace),
                    "resourceId": record["identity"]["resourceId"],
                    "stage": record["identity"]["stage"],
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )

    # Old RDC bindings may still carry event-scoped CBuffer value snapshots.
    # They are history/routing hints only: automatic cross-RDC restore loads
    # names and field aliases through the project overlay above, while numeric
    # values require an explicit current-EID/material-snapshot action. Returning
    # an empty list also migrates the next successfully written cache away from
    # the obsolete automatic-value records.
    return successful_shader_records, []


def _enable_restored_shader_editing(
    host: Any, active: Mapping[str, Any], project_root: Path,
    capture_path: Path, capture_session_id: str,
    records: Sequence[Mapping[str, Any]], shader_items: Sequence[Mapping[str, Any]],
    *, cancel_event: Any = None, deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    from .ipc_client import _CALL_BINDING, bind_direct_tool_call
    try:
        previous = _CALL_BINDING.get()
        if previous is not None and previous[1] != capture_session_id:
            raise RuntimeError("restore caller is bound to another capture session")
        instance = previous[0] if previous is not None else getattr(host.ipc, "instance_id", "")
        # The implicit instance is only a candidate. Re-check the requested
        # path/session INSIDE this binding before any start, then retain it for
        # every nested request even when another GUI becomes more active.
        with bind_direct_tool_call(instance, capture_session_id):
            return _enable_restored_shader_editing_bound(
                host, active, project_root, capture_path, capture_session_id,
                records, shader_items, cancel_event=cancel_event,
                deadline_monotonic=deadline_monotonic)
    except Exception as exc:
        items = [{"workspaceId": item["workspaceId"], "workspacePath": item["workspacePath"],
                  "status": "failed", "error": ("%s: %s" % (type(exc).__name__, exc))[:4096]}
                 for item in shader_items if item.get("status") == "applied"]
        return {"attempted": len(items), "enabled": 0, "failed": len(items), "items": items}


def _retire_restored_editor_job(live_id: str, workspace: Path, binding: Mapping[str, Any]) -> str:
    """Retire this Internal worker's exact new job, retaining durable consent."""
    from . import internal_tool_service
    with internal_tool_service._SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = internal_tool_service._SHADER_LIVE_EDIT_MANAGER
    if manager is None:
        return "exact editor owner unavailable; cleanup not confirmed"
    # Same retirement used by expired-capture reconciliation. Public stop also
    # revokes durable user consent, so it is not a cleanup route for restore.
    with manager._lifecycle_lock:
        with manager._lock:
            job = manager._jobs.get(live_id)
        if (job is None or job.session is None or job.session.binding != dict(binding)
                or _path_key(job.workspace) != _path_key(workspace)):
            return "exact editor ownership changed; cleanup not confirmed"
        job.stop()
        with manager._lock:
            key = os.path.normcase(str(workspace.resolve()))
            if manager._active_by_workspace.get(key) == live_id:
                manager._active_by_workspace.pop(key, None)
    return "exact editor job retired; durable authorization preserved"


def _enable_restored_shader_editing_bound(
    host: Any, active: Mapping[str, Any], project_root: Path,
    capture_path: Path, capture_session_id: str,
    records: Sequence[Mapping[str, Any]], shader_items: Sequence[Mapping[str, Any]],
    *, cancel_event: Any = None, deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Authorize editing only after restore has committed and released its leases.

    Restore runs on its dedicated worker; live-start is served by the separate
    HTTP executor. No source transaction or active-project pin may remain held
    while that request acquires the watcher lease. An editor failure is separate
    from the already committed, physically validated replay replacement.
    """
    result: dict[str, Any] = {"attempted": 0, "enabled": 0, "failed": 0, "items": []}
    by_id = {str(record.get("managedWorkspace", {}).get("workspaceId", "")): record
             for record in records}
    for applied in shader_items:
        if applied.get("status") != "applied":
            continue
        result["attempted"] += 1
        item = {"workspaceId": applied["workspaceId"], "workspacePath": applied["workspacePath"]}
        started_live_id = ""
        started_binding = None
        created_editor = False
        try:
            _restore_control_checkpoint(cancel_event, deadline_monotonic, "before restored Shader editor start")
            _require_restore_context(host, active, project_root, capture_path, capture_session_id)
            record = by_id.get(str(applied["workspaceId"]))
            if not isinstance(record, Mapping):
                raise RuntimeError("applied Shader has no committed project record")
            workspace = _resolve_workspace_path(
                project_root, Path(active["paths"]["shaderWorkspaces"]).resolve(),
                str(record["managedWorkspace"]["path"]),
            )
            if _path_key(workspace) != _path_key(applied["workspacePath"]):
                raise RuntimeError("applied Shader workspace path changed before editor start")
            status_response = _tool_json(
                host.get_shader_workspace_status(str(workspace)),
                "read restored Shader before editor start",
                schema="renderdoc-ultra-shader-workspace-mcp/v1",
            )
            if status_response.get("operation") != "status" or not isinstance(status_response.get("result"), Mapping):
                raise RuntimeError("restored Shader status response is invalid")
            status = status_response["result"]
            identity = record["identity"]
            latest = status.get("latestCompiled") or {}
            if (status.get("workspaceId") != applied["workspaceId"]
                    or _path_key(status.get("capture", {}).get("capturePath", "")) != _path_key(capture_path)
                    or status.get("shader", {}).get("resourceId") != identity.get("resourceId")
                    or status.get("shader", {}).get("rawSha256") != identity.get("rawSha256")
                    or status.get("stage") != identity.get("stage")
                    or status.get("entryPoint", "") != identity.get("entryPoint", "")
                    or status.get("source", {}).get("currentSha256") != record.get("sourceSha256")
                    or latest.get("sourceSha256") != record.get("sourceSha256")
                    or latest.get("revision") != record.get("candidateRevision")
                    or latest.get("rawSha256") != record.get("candidateRawSha256")):
                raise RuntimeError("applied Shader source or physical identity changed before editor start")
            from .ipc_client import _CALL_BINDING
            direct_binding = _CALL_BINDING.get()
            expected_instance = direct_binding[0]
            expected_binding = {"instanceId": expected_instance, "captureSessionId": capture_session_id,
                                "capturePath": str(capture_path)}

            def current_editor_id() -> str:
                listing = _tool_json(host.list_shader_workspace_live_edits(),
                    "list restored Shader editors", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
                sessions = (listing.get("result") or {}).get("sessions")
                if (listing.get("operation") != "list" or listing.get("appliedByThisCall") is not False
                        or not isinstance(sessions, list) or len(sessions) > 256):
                    raise RuntimeError("restored Shader editor list is invalid")
                matches = []
                for candidate in sessions:
                    if not isinstance(candidate, Mapping):
                        raise RuntimeError("restored Shader editor list contains an invalid session")
                    scope = candidate.get("captureBinding") or {}
                    if (candidate.get("state") in ("starting", "running")
                            and _path_key(candidate.get("workspacePath", "")) == _path_key(workspace)
                            and scope.get("instanceId") == expected_instance
                            and scope.get("captureSessionId") == capture_session_id
                            and _path_key(scope.get("capturePath", "")) == _path_key(capture_path)):
                        matches.append(_hex(candidate.get("liveEditId"), 32, "existing restored liveEditId"))
                if len(matches) > 1:
                    raise RuntimeError("multiple current editors own the restored workspace")
                return matches[0] if matches else ""

            existing_id = current_editor_id()
            operation = "poll" if existing_id else "start"
            if existing_id:
                wrapped = _tool_json(host.poll_shader_workspace_live_edit(existing_id),
                    "read resumed Shader editor", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
            else:
                try:
                    wrapped = _tool_json(host.start_shader_workspace_live_edit(str(workspace)),
                        "start restored Shader editing", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
                    created_editor = True
                except Exception as start_error:
                    # Owner reconciliation can win between list and start.
                    # Re-read one exact current session; never repeat start.
                    try:
                        existing_id = current_editor_id()
                    except Exception as lookup_error:
                        raise RuntimeError("%s; current editor recovery failed: %s" % (start_error, lookup_error)) from start_error
                    if not existing_id:
                        raise start_error
                    operation = "poll"
                    wrapped = _tool_json(host.poll_shader_workspace_live_edit(existing_id),
                        "read concurrently resumed Shader editor", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
            live = wrapped.get("result") or {}
            started_live_id = _hex(live.get("liveEditId"), 32, "restored liveEditId")
            if existing_id and started_live_id != existing_id:
                raise RuntimeError("resumed Shader editor returned another job")
            started_binding = expected_binding
            poll_deadline = min(time.monotonic() + 10.0, deadline_monotonic or float("inf"))
            while True:
                binding = live.get("captureBinding") or {}
                if (wrapped.get("operation") != operation
                        or wrapped.get("appliedByThisCall") is not False
                        or wrapped.get("backgroundAutoApplyAuthorized") is not True
                        or live.get("schema") != "replaylab-shader-live-edit/v1"
                        or _path_key(live.get("workspacePath", "")) != _path_key(workspace)
                        or binding.get("instanceId") != expected_instance
                        or binding.get("captureSessionId") != capture_session_id
                        or _path_key(binding.get("capturePath", "")) != _path_key(capture_path)):
                    raise RuntimeError("restored Shader editor changed its frozen scope")
                live_id = _hex(live.get("liveEditId"), 32, "restored liveEditId")
                if started_live_id and live_id != started_live_id:
                    raise RuntimeError("restored Shader poll changed its live-edit job")
                started_live_id = live_id
                started_binding = expected_binding
                watch = live.get("watch") or {}
                _restore_control_checkpoint(cancel_event, deadline_monotonic, "waiting for restored Shader editor")
                _require_restore_context(host, active, project_root, capture_path, capture_session_id)
                if (live.get("state") == "running" and watch.get("state") == "running"
                        and watch.get("caughtUp") is True
                        and watch.get("observedSourceSha256") == record.get("sourceSha256")):
                    break
                if live.get("state") not in ("starting", "running") or watch.get("state") not in ("starting", "running"):
                    raise RuntimeError("restored Shader editor stopped before becoming ready")
                if time.monotonic() >= poll_deadline:
                    raise RuntimeError("restored Shader editor did not become ready within ten seconds")
                if cancel_event is not None and callable(getattr(cancel_event, "wait", None)):
                    cancel_event.wait(min(0.1, max(0.0, poll_deadline - time.monotonic())))
                else:
                    time.sleep(min(0.1, max(0.0, poll_deadline - time.monotonic())))
                _restore_control_checkpoint(cancel_event, deadline_monotonic, "before restored Shader editor poll")
                operation = "poll"
                wrapped = _tool_json(host.poll_shader_workspace_live_edit(started_live_id),
                    "poll restored Shader editing", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
                live = wrapped.get("result") or {}
            binding = live.get("captureBinding") or {}
            watch = live.get("watch") or {}
            if (wrapped.get("operation") != operation or wrapped.get("appliedByThisCall") is not False
                    or wrapped.get("backgroundAutoApplyAuthorized") is not True
                    or live.get("schema") != "replaylab-shader-live-edit/v1"
                    or live.get("state") != "running" or watch.get("state") != "running"
                    or watch.get("caughtUp") is not True
                    or _path_key(live.get("workspacePath", "")) != _path_key(workspace)
                    or binding.get("captureSessionId") != capture_session_id
                    or _path_key(binding.get("capturePath", "")) != _path_key(capture_path)
                    or not isinstance(binding.get("instanceId"), str) or not binding["instanceId"]
                    or (expected_instance is not None and binding["instanceId"] != expected_instance)
                    or watch.get("observedSourceSha256") != record.get("sourceSha256")):
                raise RuntimeError("restored Shader editor returned a different capture or source")
            _require_restore_context(host, active, project_root, capture_path, capture_session_id)
            item.update(status="enabled",
                        liveEditId=_hex(live.get("liveEditId"), 32, "restored liveEditId"),
                        watchId=_hex(live.get("watchId"), 32, "restored watchId"),
                        captureBinding=copy.deepcopy(binding))
            result["enabled"] += 1
        except Exception as exc:
            diagnostic = "%s: %s" % (type(exc).__name__, exc)
            if created_editor and started_live_id and started_binding is not None:
                try:
                    diagnostic += "; " + _retire_restored_editor_job(started_live_id, workspace, started_binding)
                except Exception as cleanup_exc:
                    diagnostic += "; exact editor retirement failed: %s" % cleanup_exc
            item.update(status="failed", error=diagnostic[:4096])
            result["failed"] += 1
        result["items"].append(item)
    return result


def restore_active_capture_project(
    project_root: str | Path,
    capture_path: str | Path,
    capture_session_id: str | None = None,
    progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    cancel_event: Any = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Restore project Shader/CBuffer knowledge into the active local capture.

    The function is synchronous and safe to call from a persistent-host worker
    thread. An exact RDC association validates only its bounded Shader set. On
    a miss, a sealed project-side inventory can narrow the same bounded live
    validation; only a first open or stale cache inventories every unique
    Shader resource. No path walks the EID stream, and every cache remains a
    routing hint until fresh target-session ABI and anchor validation succeeds.
    """

    if cancel_event is not None and not callable(
        getattr(cancel_event, "is_set", None)
    ):
        raise TypeError("cancel_event must expose is_set()")
    clean_deadline: float | None = None
    if deadline_monotonic is not None:
        if isinstance(deadline_monotonic, bool) or not isinstance(
            deadline_monotonic, (int, float)
        ):
            raise TypeError("deadline_monotonic must be a finite monotonic timestamp")
        clean_deadline = float(deadline_monotonic)
        if not math.isfinite(clean_deadline) or clean_deadline < 0:
            raise ValueError(
                "deadline_monotonic must be a finite monotonic timestamp"
            )

    def control_checkpoint(phase: str) -> None:
        _restore_control_checkpoint(cancel_event, clean_deadline, phase)

    control_checkpoint("restore start")
    operation_id = uuid.uuid4().hex
    warnings: list[str] = []
    progress = _Progress(operation_id, progress_callback, warnings)
    result: dict[str, Any] = {
        "schema": RESTORE_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "completed": False,
        "restoreMode": "unresolved",
        "desiredHead": None,
        "capture": {
            "path": str(Path(capture_path).expanduser()),
            "sessionId": capture_session_id,
            "captureId": None,
        },
        "project": {
            "root": str(Path(project_root).expanduser()),
            "workspaceId": None,
            "replayProjectId": None,
        },
        "cache": {"hit": False, "reason": "not-read"},
        "inventoryCache": {
            "hit": False,
            "written": False,
            "reason": "not-read",
        },
        "shaders": {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        },
        "materials": {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        },
        "index": {
            "read": False,
            "present": False,
            "written": False,
            "warning": None,
        },
        "realizedReceipts": {
            "loadedFromIndex": 0,
            "validatedForRouting": 0,
            "produced": 0,
            "adoptedCurrentSession": 0,
            "mutationsCreated": 0,
            "idempotentApplyResponses": 0,
            "persisted": 0,
        },
        "warnings": warnings,
    }
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ] = []
    restored_material_sessions: list[tuple[str, str, str]] = []
    restored_alias_transactions: list[dict[str, str]] = []
    realized_receipts: list[dict[str, Any]] = []
    host: Any = None
    pin_context: Any = None
    commit_checkpoint_passed = False
    inventory_cache_binding: tuple[str, str] | None = None
    inventory_cache_write_source: Mapping[str, Any] | None = None

    def release_active_project_pin() -> None:
        nonlocal pin_context
        current = pin_context
        pin_context = None
        if current is not None:
            current.close()

    def rollback_restored_state() -> str:
        failures: list[str] = []
        material_error = _rollback_restored_material_sessions(
            host, restored_material_sessions
        )
        if material_error:
            failures.append("material rollback failed: " + material_error)
        shader_error = _rollback_applied_workspaces(host, applied_workspaces)
        if shader_error:
            failures.append("Shader rollback failed: " + shader_error)
        alias_error = _rollback_restored_alias_transactions(
            host, restored_alias_transactions
        )
        if alias_error:
            failures.append("alias rollback failed: " + alias_error)
        return "; ".join(failures)[:4096]

    with _RESTORE_GUARD:
        previous_restore_state = automatic_project_sync_suppressed()
        _RESTORE_THREAD_STATE.active = True
        try:
            host = _host_module()
            progress.emit("validate", "Validating active project and capture")
            control_checkpoint("before active project and capture validation")
            active, live_context, project, replay_path, live_session = (
                _validate_capture_and_project(
                    host, project_root, capture_path, capture_session_id
                )
            )
            control_checkpoint("after active project and capture validation")
            active_service = host._active_project_service()
            pin = getattr(active_service, "pin_active_project", None)
            if not callable(pin):
                raise RuntimeError(
                    "active project service has no transaction pin route"
                )
            pin_context = contextlib.ExitStack()
            control_checkpoint("before active project transaction pin")
            pin_context.enter_context(pin(active["workspaceId"], project))
            control_checkpoint("after active project transaction pin")
            result["capture"].update({"path": str(Path(capture_path).resolve()), "sessionId": live_session})
            result["project"].update(
                {
                    "root": str(project),
                    "workspaceId": active["workspaceId"],
                    "replayProjectId": active["replayProject"]["projectId"],
                }
            )
            replay = ReplayProjectService(replay_path)
            live_capture = live_context.get("capture")
            if not isinstance(live_capture, Mapping):
                raise RuntimeError("RenderDoc current context omitted capture API")
            if _project_has_no_restorable_knowledge(replay):
                # There is nothing that could be applied, so neither an RDC
                # association lookup nor a 500+ resource inventory can improve
                # the result. Original-resource indexing is created lazily once
                # this project actually owns HLSL/CBuffer/alias knowledge.
                result["restoreMode"] = "no-project-knowledge"
                result["cache"] = {
                    "hit": False,
                    "reason": "project-has-no-restorable-knowledge",
                }
                result["inventoryCache"] = {
                    "hit": False,
                    "written": False,
                    "reason": "not-required-for-empty-project",
                }
                result["capture"]["captureId"] = (
                    str(live_capture.get("captureId", "") or "") or None
                )
                control_checkpoint("before empty project restore completion")
                release_active_project_pin()
                result["completed"] = True
                progress.emit(
                    "complete",
                    "ReplayLab project restore completed",
                    detail={
                        "shaderApplied": 0,
                        "materialRestored": 0,
                        "cacheHit": False,
                        "projectKnowledgeEmpty": True,
                    },
                )
                return result
            inventory: dict[str, Any] | None = None
            index_transport: dict[str, Any] | None = None
            index_document: dict[str, Any] | None = None
            selected: dict[str, Any] | None = None
            binding_state: dict[str, Any] | None = None
            progress.emit("index-read", "Reading RDC project association cache")
            try:
                control_checkpoint("before RDC project index read")
                index_transport, index_document = _read_rdc_index(
                    host, Path(capture_path).resolve(), live_session
                )
                control_checkpoint("after RDC project index read")
                result["index"]["read"] = True
                result["index"]["present"] = index_document is not None
                if index_document is None:
                    result["cache"]["reason"] = "index-absent"
                else:
                    selected = select_project_binding(
                        index_document,
                        active["replayProject"]["projectId"],
                        active["workspaceId"],
                    )
                    if selected is None:
                        result["cache"]["reason"] = "project-binding-absent"
                    elif not selected.get("shaders"):
                        result["cache"]["reason"] = "project-binding-has-no-shaders"
                        selected = None
                    else:
                        control_checkpoint("before cached project binding snapshot")
                        binding_state = _project_binding_state(replay, selected)
                        desired_head, indexed_receipts = (
                            _validate_cached_binding_accelerator(
                                selected, binding_state
                            )
                        )
                        control_checkpoint("after cached project binding snapshot")
                        result["desiredHead"] = copy.deepcopy(desired_head)
                        result["realizedReceipts"]["loadedFromIndex"] = len(
                            selected.get("realizedApplySnapshot", {}).get(
                                "receipts", []
                            )
                        )
                        result["realizedReceipts"]["validatedForRouting"] = len(
                            indexed_receipts
                        )
                        result["restoreMode"] = "exact-rdc-head"
                        result["cache"] = {
                            "hit": True,
                            "reason": "exact-desired-project-head",
                        }
            except RestoreControlStop:
                raise
            except Exception as exc:
                result["cache"]["reason"] = "invalid-or-stale-index: %s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                selected = None
                binding_state = None

            if selected is not None and binding_state is not None:
                progress.emit(
                    "cache-hit",
                    "RDC cache matched; indexing bounded live Shader resources",
                    detail={"shaderCount": len(selected["shaders"])},
                )
                try:
                    shader_records, material_records = _restore_from_cache(
                        host,
                        replay,
                        active,
                        project,
                        Path(capture_path).resolve(),
                        live_session,
                        selected,
                        binding_state,
                        None,
                        progress,
                        result["shaders"],
                        result["materials"],
                        applied_workspaces,
                        restored_material_sessions,
                        restored_alias_transactions,
                        control_checkpoint,
                        realized_receipts,
                        result["realizedReceipts"],
                    )
                except (
                    RestoreContextChanged,
                    RestoreRollbackFailed,
                    RestoreControlStop,
                ):
                    raise
                except Exception as exc:
                    # Cache validation failed before every desired association
                    # could be freshly realized. The cache never has apply
                    # authority; rebuild routing from fresh target evidence.
                    cleanup_error = rollback_restored_state()
                    if cleanup_error:
                        raise RestoreRollbackFailed(
                            "cached restore validation failed and cleanup failed: "
                            + cleanup_error
                        ) from exc
                    result["cache"] = {
                        "hit": False,
                        "reason": "cache-live-validation-failed: %s: %s"
                        % (type(exc).__name__, exc),
                    }
                    shader_records = []
                    material_records = []
                    realized_receipts.clear()
                    for field in (
                        "produced",
                        "adoptedCurrentSession",
                        "mutationsCreated",
                        "idempotentApplyResponses",
                    ):
                        result["realizedReceipts"][field] = 0
                    selected = None
                    binding_state = None
            else:
                shader_records = []
                material_records = []

            if selected is None or binding_state is None:
                fallback_reason = str(result["cache"].get("reason", "cache-miss"))
                capture = Path(capture_path).resolve()
                prepared: dict[str, Any] | None = None
                cache_document: dict[str, Any] | None = None
                try:
                    inventory_cache_binding = _shader_inventory_cache_binding(
                        replay, active
                    )
                except Exception:
                    # The cache is an optional derived accelerator. A legacy
                    # Host/project status without scope metadata keeps the
                    # established full-inventory path without adding noise.
                    inventory_cache_binding = None
                    result["inventoryCache"]["reason"] = "binding-unavailable"

                if inventory_cache_binding is not None:
                    cache_project_id, cache_scope_sha256 = inventory_cache_binding
                    try:
                        control_checkpoint("before Shader inventory cache read")
                        cache_document = load_shader_inventory_cache(
                            replay_path,
                            cache_project_id,
                            cache_scope_sha256,
                            capture,
                        )
                        control_checkpoint("after Shader inventory cache read")
                    except RestoreControlStop:
                        raise
                    except RdcShaderInventoryCacheError as exc:
                        result["inventoryCache"]["reason"] = (
                            "invalid: %s" % exc
                        )[:4096]
                        warnings.append(
                            ("Shader inventory cache was ignored: %s" % exc)[:4096]
                        )
                    except Exception as exc:
                        result["inventoryCache"]["reason"] = (
                            "read-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                    else:
                        if cache_document is None:
                            result["inventoryCache"]["reason"] = (
                                "absent-or-stale"
                            )

                if cache_document is not None:
                    try:
                        provisional_inventory = _runtime_shader_inventory_from_cache(
                            cache_document,
                            capture,
                            live_session,
                            str(live_capture.get("api", "")),
                        )
                        provisional = _prepare_inventory_response(
                            replay,
                            active,
                            provisional_inventory,
                            progress,
                            control_checkpoint,
                        )
                        verified_inventory = _verify_cached_inventory_candidates(
                            host,
                            capture,
                            live_session,
                            provisional,
                            provisional_inventory,
                            cache_document,
                            control_checkpoint,
                        )
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        # Re-plan from only the freshly verified subset. This
                        # prevents the full cached aggregate, unmatched RIDs or
                        # stale fields from entering the final binding state.
                        prepared = _prepare_inventory_response(
                            replay,
                            active,
                            verified_inventory,
                            progress,
                            control_checkpoint,
                        )
                        inventory = verified_inventory
                        inventory_cache_write_source = cache_document
                        result["inventoryCache"] = {
                            "hit": True,
                            "written": False,
                            "reason": "fresh-target-validation",
                        }
                        result["restoreMode"] = "inventory-sidecar"
                    except (
                        RestoreContextChanged,
                        RestoreRollbackFailed,
                        RestoreControlStop,
                    ):
                        raise
                    except CatalogPreparationRequired as exc:
                        # A valid sidecar has already reduced this restore to
                        # bounded fresh-target validation.  If the bridge is
                        # temporarily unavailable or the bounded job times
                        # out, starting a 500+ Shader inventory is both more
                        # expensive and less likely to succeed.  Preserve the
                        # accelerator and fail this background attempt without
                        # monopolising RenderDoc's replay queue.  A genuine
                        # identity/interface mismatch still falls through and
                        # rebuilds the stale sidecar below.
                        if exc.reason_id in {
                            "shader-resource-index-timeout",
                            "shader-resource-index-unavailable",
                            "shader-resource-index-capture-changed",
                        }:
                            raise
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        result["inventoryCache"]["reason"] = (
                            "live-validation-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                        prepared = None
                        inventory = None
                    except Exception as exc:
                        # A target/session change must not be disguised as a
                        # normal cache miss. Otherwise the sidecar is simply
                        # discarded and the complete live inventory rebuilds it.
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        result["inventoryCache"]["reason"] = (
                            "live-validation-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                        prepared = None
                        inventory = None

                if prepared is None:
                    # A failed full-cache validation cannot be re-published
                    # after a narrower lookup has succeeded.
                    inventory_cache_write_source = None
                    result["restoreMode"] = "project-target-inventory"
                    inventory = _inventory_project_shader_targets(
                        host,
                        replay,
                        capture,
                        live_session,
                        str(live_capture.get("api", "")),
                        progress,
                        control_checkpoint,
                    )
                    _require_restore_context(
                        host, active, project, capture, live_session
                    )
                    result["inventoryScope"] = "target-raw-sha256"
                    result["fullFrame"] = False
                    result["targetLookup"] = {key: copy.deepcopy(inventory[key]) for key in (
                        "targetRawSha256s", "observedTargetRawSha256s", "unobservedTargetRawSha256s",
                        "targetInventoryComplete", "rawIdentitySearchComplete")}
                    if not inventory["shaders"]:
                        result["capture"]["captureId"] = inventory["capture"]["captureId"]
                        result["cache"] = {"hit": False, "reason": "project-targets-not-present"}
                        result["inventoryCache"] = {"hit": False, "written": False, "reason": "target-lookup-is-not-a-full-inventory"}
                        control_checkpoint("before completed empty project target lookup")
                        release_active_project_pin()
                        result["completed"] = True
                        progress.emit("complete", "ReplayLab project restore completed", detail={"matchedShaderCount": 0, "fullFrame": False})
                        return result
                    bridge_result = inventory.get("bridgeResult")
                    if (isinstance(bridge_result, Mapping)
                            and inventory.get("inventoryScope", "all-shaders") == "all-shaders"):
                        inventory_cache_write_source = bridge_result
                    if (
                        inventory_cache_binding is not None
                        and inventory_cache_write_source is not None
                    ):
                        cache_project_id, cache_scope_sha256 = (
                            inventory_cache_binding
                        )
                        try:
                            control_checkpoint(
                                "before Shader inventory cache publication"
                            )
                            write_shader_inventory_cache(
                                replay_path,
                                cache_project_id,
                                cache_scope_sha256,
                                capture,
                                inventory_cache_write_source,
                            )
                            control_checkpoint(
                                "after Shader inventory cache publication"
                            )
                            previous_reason = str(
                                result["inventoryCache"].get("reason", "cache-miss")
                            )
                            result["inventoryCache"] = {
                                "hit": False,
                                "written": True,
                                "reason": (
                                    "stored-after-full-inventory:"
                                    + previous_reason
                                )[:4096],
                            }
                        except RestoreControlStop:
                            raise
                        except Exception as exc:
                            result["inventoryCache"]["reason"] = (
                                "write-failed: %s: %s"
                                % (type(exc).__name__, exc)
                            )[:4096]
                            warnings.append(
                                (
                                    "Shader inventory cache could not be written: "
                                    "%s: %s" % (type(exc).__name__, exc)
                                )[:4096]
                            )
                    prepared = _prepare_inventory_response(
                        replay,
                        active,
                        inventory,
                        progress,
                        control_checkpoint,
                    )

                if inventory is None:
                    raise RuntimeError("Shader inventory preparation produced no data")
                shader_records, material_records, binding_state = (
                    _restore_from_reuse_plan(
                        host,
                        replay,
                        active,
                        project,
                        Path(capture_path).resolve(),
                        live_session,
                        prepared,
                        inventory,
                        progress,
                        result["shaders"],
                        result["materials"],
                        applied_workspaces,
                        restored_material_sessions,
                        restored_alias_transactions,
                        control_checkpoint,
                        realized_receipts,
                        result["realizedReceipts"],
                    )
                )
                result["cache"] = {
                    "hit": False,
                    "reason": ("project-reuse-plan:" + fallback_reason)[:4096],
                }

            result["capture"]["captureId"] = binding_state["captureId"]
            _require_restore_context(
                host,
                active,
                project,
                Path(capture_path).resolve(),
                live_session,
            )
            control_checkpoint("after final active restore context validation")
            incomplete_restore = bool(
                result["shaders"]["failed"]
                or result["shaders"]["skipped"]
                or result["shaders"]["applied"]
                != result["shaders"]["attempted"]
                or result["materials"]["failed"]
                or result["materials"]["restored"]
                != result["materials"]["attempted"]
            )
            if incomplete_restore:
                alias_error = _rollback_restored_alias_transactions(
                    host, restored_alias_transactions
                )
                if alias_error:
                    raise RestoreRollbackFailed(
                        "incomplete restore could not roll back its strict alias overlay: "
                        + alias_error
                    )
                warning = (
                    "RDC project association cache was not updated because one or "
                    "more reusable resources were not restored; the previous binding "
                    "was preserved for retry"
                )
                result["index"]["warning"] = warning
                warnings.append(warning)
            elif any(record.get("sourcePreparationOnly") is True for record in shader_records):
                # A partial-reflection source can pass the full current live
                # ABI gate without acquiring complete-index routing authority.
                # Keep the prior whole binding intact for mixed restores too.
                result["cache"]["reason"] = "project-source-preparation:fresh-live-abi-no-executable-index"
                result["index"]["warning"] = (
                    "Project HLSL was compiled and applied with current live ABI validation; "
                    "the previous RDC executable index was preserved because reflected layout knowledge is incomplete"
                )
                warnings.append(result["index"]["warning"])
                control_checkpoint("before completing source-preparation restore")
                _prepared_binding_state(replay, active, prepared)
            elif shader_records:
                binding = _binding_document(
                    active,
                    binding_state,
                    shader_records,
                    material_records,
                    realized_receipts,
                    (
                        selected.get("desiredProjectHead")
                        if isinstance(selected, Mapping)
                        and isinstance(
                            selected.get("desiredProjectHead"), Mapping
                        )
                        else None
                    ),
                )
                result["desiredHead"] = copy.deepcopy(
                    binding["desiredProjectHead"]
                )
                omitted_materials = len(material_records) - len(
                    binding["materials"]
                )
                if omitted_materials:
                    warnings.append(
                        "%d restored material association(s) could not be indexed because "
                        "the RDC project index requires an associated managed Shader workspace"
                        % omitted_materials
                    )
                progress.emit("index-write", "Writing updated RDC project association cache")
                if index_transport is None:
                    warning = "RDC project index was not read successfully; refusing to overwrite it"
                    result["index"]["warning"] = warning
                    warnings.append(warning)
                else:
                    control_checkpoint("before final referenced project head validation")
                    binding_state = _revalidate_binding_state_before_commit(
                        replay, binding_state, binding
                    )
                    control_checkpoint("after final referenced project head validation")
                    control_checkpoint("before RDC project index write")
                    # The RDC write is the final transaction commit operation.
                    # Cancellation racing after this checkpoint must let the
                    # completed restore win; rolling live state back after a
                    # successful write would leave stale routing receipts.
                    commit_checkpoint_passed = True
                    written, warning, unused_index = _write_index_best_effort(
                        host,
                        index_document,
                        binding,
                        Path(capture_path).resolve(),
                        live_session,
                    )
                    result["index"]["written"] = written
                    if written:
                        result["index"]["present"] = True
                        result["realizedReceipts"]["persisted"] = len(
                            realized_receipts
                        )
                    if warning:
                        result["index"]["warning"] = warning
                        warnings.append("RDC index write warning: " + warning)
            elif result["materials"]["restored"]:
                warning = (
                    "restored material sessions were not indexed because the RDC project index "
                    "requires an associated managed Shader workspace"
                )
                result["index"]["warning"] = warning
                warnings.append(warning)

            if (
                result["index"]["written"]
                and inventory_cache_binding is not None
                and inventory_cache_write_source is not None
            ):
                # A real RDC section write changes size/mtime and would make a
                # sidecar sealed before the write immediately stale. Refresh
                # only the cheap file observation; the allow-listed inventory
                # payload is unchanged and remains routing-only.
                cache_project_id, cache_scope_sha256 = inventory_cache_binding
                try:
                    write_shader_inventory_cache(
                        replay_path,
                        cache_project_id,
                        cache_scope_sha256,
                        Path(capture_path).resolve(),
                        inventory_cache_write_source,
                    )
                    result["inventoryCache"]["written"] = True
                except Exception as exc:
                    result["inventoryCache"]["reason"] = (
                        "refresh-failed: %s: %s" % (type(exc).__name__, exc)
                    )[:4096]
                    warnings.append(
                        (
                            "Shader inventory cache observation could not be "
                            "refreshed after the RDC index update: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                    )

            if not commit_checkpoint_passed:
                control_checkpoint("before restored state commit")
            release_active_project_pin()
            result["completed"] = True
            result["editing"] = _enable_restored_shader_editing(
                host, active, project, Path(capture_path).resolve(), live_session,
                shader_records, result["shaders"]["items"],
                cancel_event=cancel_event, deadline_monotonic=clean_deadline,
            )
            progress.emit(
                "complete",
                "ReplayLab project restore completed",
                detail={
                    "shaderApplied": result["shaders"]["applied"],
                    "materialRestored": result["materials"]["restored"],
                    "cacheHit": result["cache"]["hit"],
                },
            )
        except RestoreControlStop as exc:
            # Keep the project pin held while reversing every live mutation.
            # Cancellation/deadline is a terminal Host failure rather than a
            # legacy completed result with ``completed: false``.
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            diagnostic = "%s: %s" % (type(exc).__name__, exc)
            if rollback_error:
                diagnostic += "; restored state cleanup failed: " + rollback_error
            if pin_error:
                diagnostic += "; active project pin release failed: " + pin_error
            progress.emit(
                "failed",
                "ReplayLab project restore was cancelled",
                detail={"error": diagnostic[:4096]},
            )
            if rollback_error or pin_error:
                raise RestoreRollbackFailed(diagnostic[:4096]) from exc
            raise
        except CatalogPreparationRequired as exc:
            # This is an expected fail-closed product state, not a restore
            # fault. Keep the transport successful and expose stable protocol
            # values instead of leaking a Python exception class to qrenderdoc.
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            if rollback_error or pin_error:
                result["error"] = (
                    "%s: %s%s%s"
                    % (
                        type(exc).__name__,
                        exc,
                        "; active project pin release failed: " + pin_error
                        if pin_error else "",
                        "; restored state cleanup failed: " + rollback_error
                        if rollback_error else "",
                    )
                )[:4096]
                result["errorType"] = "RestoreRollbackFailed"
                progress.emit(
                    "failed",
                    "ReplayLab project restore failed and restored state cleanup also failed",
                    detail={"error": result["error"]},
                )
            else:
                result["outcome"] = "action-required"
                result["reasonId"] = exc.reason_id
        except Exception as exc:
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            result["errorType"] = type(exc).__name__
            if rollback_error:
                result["error"] = (
                    result["error"]
                    + "; restored state cleanup failed: "
                    + rollback_error
                )[:4096]
                result["errorType"] = "RestoreRollbackFailed"
            if pin_error:
                result["error"] = (
                    result["error"]
                    + "; active project pin release failed: "
                    + pin_error
                )[:4096]
                if result["errorType"] != "RestoreRollbackFailed":
                    result["errorType"] = "RestoreContextChanged"
            progress.emit(
                "failed",
                "ReplayLab project restore failed",
                detail={"error": result["error"]},
            )
        finally:
            if pin_context is not None:
                release_active_project_pin()
            _RESTORE_THREAD_STATE.active = previous_restore_state
    return result


def _live_replacement(
    host: Any,
    status: Mapping[str, Any],
    capture_session_id: str,
    replacement_id: str,
    revision: int,
    candidate_raw_sha256: str,
) -> dict[str, Any]:
    capture_id = _hex(status.get("capture", {}).get("captureId"), 64, "workspace captureId")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    capture = status.get("capture")
    shader = status.get("shader")
    if not all(
        isinstance(value, Mapping)
        for value in (baseline, latest, capture, shader)
    ):
        raise RuntimeError("workspace omitted live replacement identity")
    original_raw_sha256 = _hex(
        baseline.get("rawSha256"), 64, "workspace baseline.rawSha256"
    )
    physical_abi_sha256 = _hex(
        baseline.get("abiSha256"), 64, "workspace baseline.abiSha256"
    )
    expected_anchor = _shader_anchor_event_id(
        {
            "anchorEventId": status.get(
                "anchorEventId",
                status.get("eventId", status.get("binding", {}).get("eventId")),
            )
        },
        required=False,
    )
    response = host.ipc.call(
        "get_shared_shader_replacements",
        {
            "expected_capture_id": capture_id,
            "expected_capture_session_id": capture_session_id,
        },
        timeout=30.0,
    )
    replacements = response.get("replacements") if isinstance(response, Mapping) else None
    if (
        not isinstance(response, dict)
        or response.get("schema") != "renderdoc-ultra-shared-shader-replacement-list/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureId") != capture_id
        or response.get("captureSessionId") != capture_session_id
        or response.get("originalShadersPreserved") is not True
        or response.get("replayMutation") is not False
        or not isinstance(replacements, list)
        or response.get("count") != len(replacements)
    ):
        raise RuntimeError("RenderDoc returned invalid shared-replacement metadata")
    matches = [
        item
        for item in replacements
        if isinstance(item, Mapping)
        and item.get("replacementId") == replacement_id
        and item.get("workspaceId") == status.get("workspaceId")
        and item.get("revision") == revision
        and item.get("candidateRawSha256") == candidate_raw_sha256
        and item.get("stage") == status.get("stage")
        and item.get("entryPoint", "") == status.get("entryPoint", "")
        and item.get("api") == _asset_api(capture.get("api"))
        and item.get("encoding") == _asset_encoding(latest.get("kind"))
        and item.get("originalResourceId") == shader.get("resourceId")
        and item.get("originalRawSha256") == original_raw_sha256
        and item.get("physicalAbiSha256") == physical_abi_sha256
        and (
            expected_anchor is None
            or item.get("anchorEventId") == expected_anchor
        )
        and item.get("resourceScoped") is True
        and item.get("eventScoped") is False
        and item.get("allOriginalResourceUsesAffected") is True
        and item.get("originalShaderPreserved") is True
    ]
    if len(matches) != 1:
        raise RuntimeError("replacement_id is not the exact applied workspace candidate")
    return dict(matches[0])


def _sync_index_binding(
    host: Any,
    active: Mapping[str, Any],
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
    *,
    shader_record: Mapping[str, Any] | None = None,
    material_record: Mapping[str, Any] | None = None,
    realized_receipt: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    inventory_cache_document: dict[str, Any] | None = None
    inventory_cache_root: Path | None = None
    inventory_cache_warning = ""
    try:
        raw_cache_root = getattr(replay, "root")
        inventory_cache_root = Path(raw_cache_root).resolve()
    except Exception:
        inventory_cache_root = None
    if inventory_cache_root is not None:
        try:
            inventory_cache_document = load_shader_inventory_cache(
                inventory_cache_root,
                _hex(binding_state.get("projectId"), 32, "projectId"),
                _hex(binding_state.get("scopeSha256"), 64, "scopeSha256"),
                capture_path,
            )
        except RdcShaderInventoryCacheError as exc:
            inventory_cache_warning = (
                "Shader inventory cache was ignored after project publication: %s"
                % exc
            )[:4096]
        except Exception:
            # The derived cache cannot make an otherwise valid explicit
            # project publication fail.
            inventory_cache_document = None
    try:
        unused_transport, index = _read_rdc_index(host, capture_path, capture_session_id)
    except Exception as exc:
        return False, "RDC index read failed after project commit: %s: %s" % (
            type(exc).__name__, exc
        )
    base = index or new_index()
    old = select_project_binding(
        base, binding_state["projectId"], active["workspaceId"]
    )
    if old is not None and any(
        old.get(field) != binding_state.get(field)
        for field in (
            "projectId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "matcherPolicySha256",
        )
    ):
        old = None
    shaders = list(old.get("shaders", []) or []) if old else []
    materials = list(old.get("materials", []) or []) if old else []
    old_receipts = (
        list(old.get("realizedApplySnapshot", {}).get("receipts", []) or [])
        if old
        else []
    )
    # Project knowledge is immutable but the manifest can be repaired or
    # replaced outside this process. Preserve only associations that still
    # name exact current records and exact current-catalog Shader identities.
    current_shaders: list[dict[str, Any]] = []
    for item in shaders:
        try:
            knowledge = binding_state.get("workspaces", {}).get(
                item.get("knowledgeId")
            )
            if (
                not isinstance(knowledge, Mapping)
                or knowledge.get("sourceSha256") != item.get("sourceSha256")
            ):
                continue
            _capture_shader(binding_state, item["identity"])
            current_shaders.append(item)
        except Exception:
            continue
    shaders = current_shaders
    current_materials: list[dict[str, Any]] = []
    for item in materials:
        knowledge = binding_state.get("materials", {}).get(item.get("knowledgeId"))
        if (
            isinstance(knowledge, Mapping)
            and knowledge.get("constantBlockPath") == item.get("path")
            and knowledge.get("constantBlockLayoutSha256")
            == item.get("layoutSha256")
        ):
            current_materials.append(item)
    materials = current_materials
    if shader_record is not None:
        key = (
            shader_record["identity"]["resourceId"],
            shader_record["identity"]["stage"],
            shader_record["identity"].get("entryPoint", ""),
            shader_record["identity"]["stableKeySha256"],
        )
        shaders = [
            item
            for item in shaders
            if (
                item["identity"]["resourceId"],
                item["identity"]["stage"],
                item["identity"].get("entryPoint", ""),
                item["identity"]["stableKeySha256"],
            )
            != key
        ]
        shaders.append(copy.deepcopy(dict(shader_record)))
    if material_record is not None:
        key = (
            material_record["shaderResourceId"],
            material_record["stage"],
            material_record["eventId"],
            material_record["blockIndex"],
            material_record["path"],
        )
        materials = [
            item
            for item in materials
            if (
                item["shaderResourceId"],
                item["stage"],
                item["eventId"],
                item["blockIndex"],
                item["path"],
            )
            != key
        ]
        materials.append(copy.deepcopy(dict(material_record)))
    desired_receipt_keys = {
        (
            item["identity"]["stableKeySha256"],
            item["identity"]["resourceId"],
            item["managedWorkspace"]["workspaceId"],
            item["candidateRevision"],
            item["candidateRawSha256"],
        )
        for item in shaders
    }
    receipt_candidates = [
        copy.deepcopy(dict(item))
        for item in old_receipts
        if isinstance(item, Mapping)
        and (
            item.get("stableKeySha256"),
            item.get("originalResourceId"),
            item.get("workspaceId"),
            item.get("candidateRevision"),
            item.get("candidateRawSha256"),
        )
        in desired_receipt_keys
    ]
    if realized_receipt is not None:
        if shader_record is None:
            return False, "realized Shader receipt has no desired Shader record"
        replacement_key = (
            shader_record["identity"]["stableKeySha256"],
            shader_record["identity"]["resourceId"],
            shader_record["managedWorkspace"]["workspaceId"],
            shader_record["candidateRevision"],
            shader_record["candidateRawSha256"],
        )
        receipt_candidates = [
            item
            for item in receipt_candidates
            if (
                item.get("stableKeySha256"),
                item.get("originalResourceId"),
                item.get("workspaceId"),
                item.get("candidateRevision"),
                item.get("candidateRawSha256"),
            )
            != replacement_key
        ]
        receipt_candidates.append(copy.deepcopy(dict(realized_receipt)))
    try:
        complete_receipts = _normalised_receipts_for_records(
            shaders, receipt_candidates
        )
    except Exception:
        # A legacy/partial receipt set cannot be promoted.  Keep the desired
        # project binding but safely downgrade this optional accelerator.
        complete_receipts = []
    binding_state_for_write = dict(binding_state)
    if (
        "activeKnowledgeRevision" not in binding_state_for_write
        and old
        and isinstance(old.get("desiredProjectHead"), Mapping)
    ):
        binding_state_for_write["activeKnowledgeRevision"] = old[
            "desiredProjectHead"
        ].get("activeKnowledgeRevision", 0)
    binding = _binding_document(
        active,
        binding_state_for_write,
        shaders,
        materials,
        complete_receipts,
    )
    if material_record is not None and material_record not in binding["materials"]:
        return False, (
            "project committed, but the RDC project index cannot encode material knowledge "
            "without an associated managed Shader workspace"
        )
    try:
        _revalidate_binding_state_before_commit(
            replay, binding_state_for_write, binding
        )
    except Exception as exc:
        return False, (
            "project referenced head changed before RDC index commit: %s: %s"
            % (type(exc).__name__, exc)
        )[:4096]
    written, warning, unused_index = _write_index_best_effort(
        host, base, binding, capture_path, capture_session_id
    )
    if (
        written
        and inventory_cache_root is not None
        and inventory_cache_document is not None
    ):
        try:
            write_shader_inventory_cache(
                inventory_cache_root,
                _hex(binding_state.get("projectId"), 32, "projectId"),
                _hex(binding_state.get("scopeSha256"), 64, "scopeSha256"),
                capture_path,
                inventory_cache_document,
            )
        except Exception as exc:
            inventory_cache_warning = (
                "Shader inventory cache observation could not be refreshed "
                "after project publication: %s: %s"
                % (type(exc).__name__, exc)
            )[:4096]
    combined_warning = "; ".join(
        item for item in (warning, inventory_cache_warning) if item
    )[:4096]
    return written, combined_warning


def _register_applied_workspace_with_readback(
    project: Any,
    workspace_service: Any,
    workspace: Path,
    expected_status: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a live-applied user revision without re-running AI organization.

    The caller has already verified the exact live replacement and owns the
    active-project pin. A running watcher owns the workspace source lease, so
    taking a second lease here would always reject successful live edits.
    Instead the writer requires the current source to match the immutable
    ABI-validated candidate, and both source/status and project artifacts are
    read back. A concurrent save can never be recorded as the applied version.
    """

    status = workspace_service.status(str(workspace))
    if status != expected_status:
        raise RuntimeError("workspace changed before applied project publication")
    source = status.get("source")
    latest = status.get("latestCompiled")
    if not isinstance(source, Mapping) or not isinstance(latest, Mapping):
        raise RuntimeError("applied workspace omitted source/candidate state")
    gate = latest.get("abiGate")
    if (
        not isinstance(gate, Mapping)
        or gate.get("status") != "compatible"
        or latest.get("replayAbiValidated") is not True
        or latest.get("abiCompatible") is not True
        or latest.get("requiresReplayAbiValidation") is not False
        or latest.get("validationScope") != "replay-reflection-abi-compatible"
        or latest.get("semanticEquivalenceVerified") is not False
        or status.get("replayAbiValidated") is not True
        or status.get("abiCompatible") is not True
        or latest.get("revision") != status.get("revision")
        or latest.get("sourceSha256") != source.get("currentSha256")
    ):
        raise RuntimeError("applied project publication requires current compatible ABI evidence")
    committed = project.register_workspace(status, include_explanatory=False)
    knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("applied project publication omitted workspace knowledge")
    knowledge_id = _hex(knowledge.get("knowledgeId"), 32, "workspace knowledgeId")
    published = project.get_workspace_knowledge(knowledge_id)
    if workspace_service.status(str(workspace)) != status:
        raise RuntimeError("workspace changed during applied project publication")
    if (
        knowledge.get("analysisSha256")
        or committed.get("flowchartArtifacts") != []
        or committed.get("flowGraphArtifacts") != []
    ):
        raise RuntimeError("applied project publication reused stale AI documentation")
    return _host_module()._validated_active_workspace_registration(
        committed, status, published, publication_gate=None
    )


def sync_active_shader_workspace_after_apply(
    workspace_path: str | Path,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    replacement_id: str,
) -> dict[str, Any]:
    """Commit one exactly applied workspace, then refresh the RDC cache."""

    with _RESTORE_GUARD:
        host = _host_module()
        active = host._active_project_service().status()
        workspace = Path(workspace_path).expanduser().resolve()
        managed = host._require_managed_active_workspace(active, str(workspace))
        status = host._shader_workspace_service().status(str(managed))
        revision = _integer(expected_revision, "expected_revision", 1)
        candidate_raw = _hex(
            expected_candidate_raw_sha256, 64, "expected_candidate_raw_sha256"
        )
        replacement = _hex(replacement_id, 32, "replacement_id")
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        source = status.get("source") if isinstance(status, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(source, Mapping)
            or latest.get("revision") != revision
            or latest.get("rawSha256") != candidate_raw
            or latest.get("sourceSha256") != source.get("currentSha256")
        ):
            raise RuntimeError("workspace changed after the applied candidate")
        capture_path = Path(str(status.get("capture", {}).get("capturePath", ""))).resolve()
        live = host._active_live_context(allow_missing_capture_id=True)
        session_id = _hex(
            live.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )
        if _path_key(live.get("capture", {}).get("path", "")) != _path_key(capture_path):
            raise RuntimeError("active capture differs from the applied workspace")
        live_replacement = _live_replacement(
            host, status, session_id, replacement, revision, candidate_raw
        )
        active_service = host._active_project_service()
        with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            host._require_same_active_project(active)
            committed = _register_applied_workspace_with_readback(
                active_service,
                host._shader_workspace_service(),
                managed,
                status,
            )
            knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
            if not isinstance(knowledge, Mapping):
                raise RuntimeError("project workspace registration returned invalid knowledge")
        # Re-read after the durable project commit so a newer save cannot be
        # represented by the old applied revision in the RDC cache.
        after = host._shader_workspace_service().status(str(managed))
        if (
            after.get("revision") != status.get("revision")
            or after.get("source", {}).get("currentSha256")
            != source.get("currentSha256")
            or after.get("latestCompiled", {}).get("rawSha256") != candidate_raw
        ):
            raise RuntimeError("workspace changed while project knowledge was committed")
        live_replacement = _live_replacement(
            host, after, session_id, replacement, revision, candidate_raw
        )
        replay = ReplayProjectService(active["paths"]["replayProject"])
        binding_state = replay.restore_knowledge_snapshot(
            status["capture"]["captureId"], status["capture"].get("catalogSha256", "")
        )
        identity = next(
            (
                _normal_shader_identity(item)
                for item in binding_state["capture"]["shaders"]
                if item.get("resourceId") == status["shader"]["resourceId"]
                and item.get("stage") == status["stage"]
                and item.get("entryPoint", "") == status.get("entryPoint", "")
                and item.get("rawSha256") == status["shader"]["rawSha256"]
            ),
            None,
        )
        if identity is None:
            raise RuntimeError("committed workspace is absent from its project capture")
        shader_record = _shader_record_from_status(
            Path(active["projectRoot"]).resolve(),
            identity,
            str(knowledge["knowledgeId"]),
            managed,
            after,
            project_knowledge=knowledge,
        )
        realized_receipt = _realized_receipt_from_record(
            shader_record,
            str(live_replacement.get("physicalAbiSha256", "")),
            abi_report_sha256=(
                str(live_replacement.get("abiReportSha256", "") or "") or None
            ),
        )
        written, warning = _sync_index_binding(
            host,
            active,
            replay,
            binding_state,
            capture_path,
            session_id,
            shader_record=shader_record,
            realized_receipt=realized_receipt,
        )
        return {
            "schema": SHADER_SYNC_SCHEMA,
            "schemaVersion": 1,
            "workspacePath": str(managed),
            "workspaceId": status["workspaceId"],
            "captureId": status["capture"]["captureId"],
            "captureSessionId": session_id,
            "replacementId": replacement,
            "revision": revision,
            "candidateRawSha256": candidate_raw,
            "projectCommitted": True,
            "knowledge": copy.deepcopy(dict(knowledge)),
            "rdcIndexWritten": written,
            "warning": warning or None,
        }


def sync_active_material_session(
    session_id: str, parameter_ids: Sequence[str]
) -> dict[str, Any]:
    """Commit selected live values, then refresh their RDC association hint."""

    with _RESTORE_GUARD:
        host = _host_module()
        token = _hex(session_id, 32, "session_id")
        requested = [str(value or "").strip().lower() for value in parameter_ids]
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("parameter_ids must be a non-empty unique sequence")
        session = _tool_json(
            host.get_material_session_status(token),
            "get material session status",
            schema="renderdoc-ultra-material-session/v1",
            schema_version=1,
        )
        active_service = host._active_project_service()
        active = active_service.status()
        replay_status = active.get("replayProject") if isinstance(active, Mapping) else None
        if not isinstance(replay_status, Mapping):
            raise RuntimeError("active project omitted its replay-project identity")
        project_id = _hex(replay_status.get("projectId"), 32, "replay projectId")
        with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            host._require_same_active_project(active)
            accumulated = _remember_material_sync_parameters(
                token,
                session,
                project_id,
                str(active["workspaceId"]),
                requested,
            )
            committed = active_service.save_material_knowledge(session, accumulated)
            knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
            if not isinstance(knowledge, Mapping):
                raise RuntimeError("project material save returned invalid knowledge")
            raw_committed_parameters = knowledge.get("parameters")
            if (
                not isinstance(raw_committed_parameters, list)
                or not raw_committed_parameters
                or len(raw_committed_parameters) > 256
            ):
                raise RuntimeError(
                    "project material save omitted its complete parameter head"
                )
            committed_parameter_ids: list[str] = []
            for index, parameter in enumerate(raw_committed_parameters):
                if not isinstance(parameter, Mapping):
                    raise RuntimeError(
                        "project material parameter[%d] is invalid" % index
                    )
                committed_parameter_ids.append(
                    _hex(
                        parameter.get("parameterId"),
                        32,
                        "project material parameterId",
                    )
                )
            if (
                len(set(committed_parameter_ids)) != len(committed_parameter_ids)
                or not set(accumulated).issubset(committed_parameter_ids)
            ):
                raise RuntimeError(
                    "project material save returned an inconsistent parameter head"
                )
            # Durable project state is authoritative. Prime the in-memory table
            # with the complete committed head only as a subsequent-call
            # optimisation; Host restart/eviction cannot affect correctness.
            _remember_material_sync_parameters(
                token,
                session,
                project_id,
                str(active["workspaceId"]),
                committed_parameter_ids,
            )
        # Saving knowledge is the durable transaction.  Only then is the RDC
        # hint refreshed, and failure there is reported as a warning.
        replay = ReplayProjectService(active["paths"]["replayProject"])
        binding_state = replay.restore_knowledge_snapshot(session["captureId"], "")
        live = host._active_live_context(allow_missing_capture_id=True)
        capture_path = Path(str(live.get("capture", {}).get("path", ""))).resolve()
        capture_session_id = _hex(
            live.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )
        if capture_session_id != session.get("captureSessionId"):
            raise RuntimeError("material session belongs to another capture lifecycle")
        material_record = {
            "knowledgeId": knowledge["knowledgeId"],
            "shaderResourceId": session["shaderResourceId"],
            "stage": session["stage"],
            "blockIndex": session["constantBlockIndex"],
            "path": session["constantBlockPath"],
            "layoutSha256": session["layoutSha256"],
            "eventId": session["eventId"],
            "parameters": [
                {"parameterId": parameter_id}
                for parameter_id in committed_parameter_ids
            ],
        }
        written, warning = _sync_index_binding(
            host,
            active,
            replay,
            binding_state,
            capture_path,
            capture_session_id,
            material_record=material_record,
        )
        return {
            "schema": MATERIAL_SYNC_SCHEMA,
            "schemaVersion": 1,
            "sessionId": token,
            "captureId": session["captureId"],
            "captureSessionId": capture_session_id,
            "parameterIds": committed_parameter_ids,
            "projectCommitted": True,
            "knowledge": copy.deepcopy(dict(knowledge)),
            "rdcIndexWritten": written,
            "warning": warning or None,
        }


__all__ = [
    "MATERIAL_SYNC_SCHEMA",
    "RESTORE_PROGRESS_SCHEMA",
    "RESTORE_SCHEMA",
    "SHADER_SYNC_SCHEMA",
    "automatic_project_sync_suppressed",
    "restore_active_capture_project",
    "sync_active_material_session",
    "sync_active_shader_workspace_after_apply",
]
