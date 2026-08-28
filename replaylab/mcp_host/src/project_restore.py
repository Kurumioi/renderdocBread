"""Persistent-host orchestration for safe ReplayLab project restoration.

The RDC section consumed here is only a disposable routing cache.  Every
Shader is first matched by a bounded, zero-EID target-capture resource index,
then passes the ordinary single-anchor live ABI/apply transaction. Every material value is
rebound through a new capture-local material session and an exact reflected
block/typed-parameter check.

This module intentionally exposes synchronous functions.  The persistent Host
may run them in its existing worker pool; progress callbacks execute on that
same worker thread and must be forwarded to UI code by the Host.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .projects import MAX_CAPTURE_CATALOG_BYTES, ReplayProjectError, ReplayProjectService
from .rdc_project_index import (
    RDC_PROJECT_INDEX_VERSION,
    RdcProjectIndexError,
    encode_index,
    new_index,
    parse_index,
    select_project_binding,
    upsert_project_binding,
)
from .shader_workspace.io import is_within
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
_FAST_INDEX_POLL_SECONDS = 0.025
_FAST_INDEX_TIMEOUT_SECONDS = 15.0


class CatalogPreparationRequired(RuntimeError):
    """The explicit full-capture project index is not available."""

    def __init__(
        self,
        message: str,
        reason_id: str = CATALOG_INDEX_REQUIRED_REASON_ID,
    ) -> None:
        super().__init__(message)
        self.reason_id = str(reason_id or CATALOG_INDEX_REQUIRED_REASON_ID)


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

    def emit(
        self,
        phase: str,
        message: str,
        *,
        current: int = 0,
        total: int = 0,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        event = {
            "schema": RESTORE_PROGRESS_SCHEMA,
            "schemaVersion": 1,
            "operationId": self.operation_id,
            "phase": _text(phase, "progress.phase", 128),
            "current": _integer(current, "progress.current"),
            "total": _integer(total, "progress.total"),
            "message": _text(message, "progress.message", 1024),
            "detail": copy.deepcopy(dict(detail or {})),
        }
        if len(event["phase"].encode("utf-8")) > 128:
            raise RuntimeError("progress.phase exceeds 128 UTF-8 bytes")
        if len(event["message"].encode("utf-8")) > 1024:
            raise RuntimeError("progress.message exceeds 1024 UTF-8 bytes")
        if event["total"] and event["current"] > event["total"]:
            raise RuntimeError("progress.current exceeds progress.total")
        if self.callback is None:
            return
        try:
            self.callback(copy.deepcopy(event))
        except Exception as exc:
            warning = "progress callback failed: %s: %s" % (type(exc).__name__, exc)
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
    if response.get("sectionVersion") != RDC_PROJECT_INDEX_VERSION:
        raise RuntimeError("RDC project-index section version is unsupported")
    document = response.get("document")
    if not isinstance(document, Mapping):
        raise RuntimeError("RDC project-index wrapper omitted its document")
    # Canonical encoding performs the strict field/limit/seal validation even
    # though the Bridge has already parsed the transport JSON.
    clean = parse_index(encode_index(document))
    return response, clean


def _project_binding_state(
    replay: ReplayProjectService, binding: Mapping[str, Any]
) -> dict[str, Any]:
    state = replay.restore_knowledge_snapshot(
        str(binding.get("captureId", "")), str(binding.get("catalogSha256", ""))
    )
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
    fields = (
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "codeSha256",
        "interfaceHash",
        "layoutHash",
        "interfaceComplete",
    )
    matches = [item for item in shaders if isinstance(item, Mapping) and all(item.get(field) == identity.get(field) for field in fields)]
    if len(matches) != 1:
        raise RuntimeError("RDC Shader association differs from the exact project catalog")
    expected_events = list(matches[0].get("eventIds", []) or [])
    if list(identity.get("eventIds", []) or []) != expected_events or not expected_events:
        raise RuntimeError("RDC Shader anchor events differ from the exact project catalog")
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
    if (
        not isinstance(status, dict)
        or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"
        or _path_key(status.get("workspacePath", "")) != _path_key(workspace)
        or status.get("workspaceId") != managed.get("workspaceId")
        or not isinstance(capture, Mapping)
        or not isinstance(shader, Mapping)
        or not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or capture.get("captureId") != binding_state.get("captureId")
        or capture.get("catalogSha256") != binding_state.get("catalogSha256")
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
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("RDC Shader association references absent project knowledge")
    if (
        knowledge.get("sourceSha256") != record.get("sourceSha256")
        or knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
    ):
        raise RuntimeError("RDC Shader association differs from project knowledge")
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
        or identity.get("interfaceHash", "") != computed_interface
        or identity.get("layoutHash", "") != computed_layout
        or (identity.get("interfaceComplete") is True) != computed_complete
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
    return {
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


def _resource_index_job(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    capture_id: str,
    evidence_sha256: str,
    api: str,
    shaders: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    request = {
        "expected_capture_path": str(capture_path),
        "expected_capture_session_id": capture_session_id,
        "expected_capture_id": capture_id,
        "expected_identity_evidence_sha256": evidence_sha256,
        "expected_api": api,
        "expected_shaders": [copy.deepcopy(dict(item)) for item in shaders],
    }
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
    deadline = time.monotonic() + _FAST_INDEX_TIMEOUT_SECONDS
    request_sha256 = ""
    while True:
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
            reason_id = (
                "shader-resource-index-capture-changed"
                if "capture changed" in str(exc).lower()
                else "shader-resource-index-unavailable"
            )
            raise CatalogPreparationRequired(
                "could not poll the bounded Shader resource index: %s" % exc,
                reason_id,
            ) from exc

    result = snapshot.get("result")
    reason_id = str(result.get("reasonId", "") or "") if isinstance(result, Mapping) else ""
    capability = result.get("capability") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema") != "replaylab-project-shader-resource-index/v1"
        or result.get("schemaVersion") != 1
        or result.get("captureSessionId") != capture_session_id
        or _path_key(result.get("capturePath", "")) != _path_key(capture_path)
        or result.get("captureId") != capture_id
        or result.get("identityEvidenceSha256") != evidence_sha256
        or result.get("requestSha256") != request_sha256
        or not isinstance(capability, Mapping)
        or capability.get("eventTraversalSupported") is not False
        or capability.get("anchorPreflightRequired") is not True
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
            or asset.get("contentMatchMode")
            != "api-format-interface-layout-owner-rid"
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
) -> list[dict[str, Any]]:
    """Match cached assets by resource bytes/format without traversing EIDs."""

    if not validated:
        raise RuntimeError("cached project binding has no Shader workspaces")
    if len(validated) > _MAX_FAST_PROJECT_SHADERS:
        raise CatalogPreparationRequired(
            "cached project binding exceeds the fast Shader resource-index limit",
            "shader-resource-index-shader-limit",
        )
    groups: dict[
        tuple[str, str, str, str, str],
        list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]],
    ] = {}
    for record, unused_workspace, status in validated:
        projection = host._workspace_catalog_binding_projection(status)
        group_key = (
            projection["captureId"],
            _path_key(projection["capturePath"]),
            str(projection["api"]),
            _path_key(projection["catalogPath"]),
            str(projection["catalogSha256"]),
        )
        groups.setdefault(group_key, []).append((record, status, projection))

    reports: list[dict[str, Any]] = []
    for group_key, entries in groups.items():
        first_status = entries[0][1]
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
        expected_shaders: list[dict[str, Any]] = []
        expected_owner_keys: set[tuple[Any, ...]] = set()
        total_blocks = 0
        for record, unused_status, projection in entries:
            # The inexpensive projection proves every workspace points at the
            # same exact catalog bytes.  The one full scope parse above then
            # proves each selected anchor is actually present in those bytes.
            current_key = (
                projection["captureId"],
                _path_key(projection["capturePath"]),
                str(projection["api"]),
                _path_key(projection["catalogPath"]),
                str(projection["catalogSha256"]),
            )
            if current_key != group_key:
                raise RuntimeError("cached workspaces changed catalog grouping")
            identity = record["identity"]
            event_id = min(int(value) for value in identity["eventIds"])
            if (event_id, projection["shaderKey"]) not in use_tuples:
                raise RuntimeError(
                    "cached Shader anchor is absent from the exact workspace catalog"
                )
            expected = _expected_resource_shader(
                catalog, identity, api, event_id, catalog_shader_index
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
        reports.append(
            _resource_index_job(
                host,
                capture_path,
                capture_session_id,
                group_key[0],
                evidence_sha256,
                api,
                expected_shaders,
            )
        )
    return reports


def _apply_workspace(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    capture_session_id: str,
) -> dict[str, Any]:
    wrapped = _tool_json(
        host.apply_shader_workspace_shared_resource(
            str(workspace), expected_revision, expected_candidate_raw_sha256
        ),
        "apply Shader workspace",
        schema="renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
    )
    binding = wrapped.get("binding")
    replacement = wrapped.get("replacement")
    if (
        wrapped.get("operation") != "apply-shared-resource"
        or wrapped.get("applied") is not True
        or wrapped.get("scope") != "captured-resource"
        or not isinstance(binding, Mapping)
        or binding.get("workspaceId") != status.get("workspaceId")
        or binding.get("captureId") != status.get("capture", {}).get("captureId")
        or binding.get("revision") != expected_revision
        or binding.get("candidateRawSha256") != expected_candidate_raw_sha256
        or not isinstance(replacement, Mapping)
        or replacement.get("workspaceId") != status.get("workspaceId")
        or replacement.get("captureSessionId") != capture_session_id
        or replacement.get("candidateRawSha256") != expected_candidate_raw_sha256
        or replacement.get("resourceScoped") is not True
        or replacement.get("originalShaderPreserved") is not True
    ):
        raise RuntimeError("Shader apply returned invalid live ABI/replacement evidence")
    return wrapped


def _normal_shader_identity(shader: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "resourceId": _integer(shader.get("resourceId"), "shader.resourceId", 1),
        "stage": _text(shader.get("stage"), "shader.stage", 32),
        "entryPoint": _text(
            shader.get("entryPoint", ""), "shader.entryPoint", 512, allow_empty=True
        ),
        "rawSha256": _hex(shader.get("rawSha256"), 64, "shader.rawSha256"),
        "codeSha256": str(shader.get("codeSha256", "") or ""),
        "interfaceHash": str(shader.get("interfaceHash", "") or ""),
        "layoutHash": str(shader.get("layoutHash", "") or ""),
        "interfaceComplete": shader.get("interfaceComplete") is True,
        "eventIds": sorted(
            {
                _integer(value, "shader.eventId", 1)
                for value in list(shader.get("eventIds", []) or [])
            }
        ),
    }
    for field in ("codeSha256", "interfaceHash", "layoutHash"):
        if identity[field]:
            _hex(identity[field], 64, "shader.%s" % field)
    if not identity["eventIds"]:
        raise RuntimeError("Shader has no live anchor event")
    return identity


def _shader_record_from_status(
    project_root: Path,
    identity: Mapping[str, Any],
    knowledge_id: str,
    workspace: Path,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    source = status.get("source")
    latest = status.get("latestCompiled")
    if not isinstance(source, Mapping) or not isinstance(latest, Mapping):
        raise RuntimeError("applied workspace omitted source/candidate state")
    relative = workspace.relative_to(project_root).as_posix()
    return {
        "identity": copy.deepcopy(dict(identity)),
        "knowledgeId": _hex(knowledge_id, 32, "workspace knowledgeId"),
        "managedWorkspace": {
            "workspaceId": _hex(status.get("workspaceId"), 32, "workspaceId"),
            "path": relative,
        },
        "candidateRevision": _integer(
            latest.get("revision"), "candidateRevision", 1
        ),
        "candidateRawSha256": _hex(
            latest.get("rawSha256"), 64, "candidateRawSha256"
        ),
        "sourceSha256": _hex(
            source.get("currentSha256"), 64, "sourceSha256"
        ),
    }


def _knowledge_matches_shader(
    knowledge: Mapping[str, Any], identity: Mapping[str, Any]
) -> bool:
    if (
        knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
    ):
        return False
    if knowledge.get("rawSha256") == identity.get("rawSha256"):
        return True
    return bool(
        knowledge.get("codeSha256")
        and knowledge.get("codeSha256") == identity.get("codeSha256")
        and knowledge.get("interfaceComplete") is True
        and identity.get("interfaceComplete") is True
        and knowledge.get("interfaceHash") == identity.get("interfaceHash")
        and knowledge.get("layoutHash", "") == identity.get("layoutHash", "")
    )


def _restore_material(
    host: Any,
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    project_workspace_id: str,
    identity: Mapping[str, Any],
    material_record: Mapping[str, Any],
    capture_session_id: str,
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
    if event_id not in identity.get("eventIds", []):
        raise RuntimeError("RDC material event is not a Shader anchor")
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


def _binding_document(
    active: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    shader_keys = {
        (item["identity"]["resourceId"], item["identity"]["stage"])
        for item in shader_records
    }
    materials = [
        copy.deepcopy(dict(item))
        for item in material_records
        if (item.get("shaderResourceId"), item.get("stage")) in shader_keys
    ]
    return {
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


def _restore_from_cache(
    host: Any,
    replay: ReplayProjectService,
    active: dict[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    binding: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    progress: _Progress,
    shader_summary: dict[str, Any],
    material_summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = list(binding.get("shaders", []) or [])
    seen_workspaces: set[str] = set()
    validated: list[tuple[Mapping[str, Any], Path, dict[str, Any]]] = []
    for index, record in enumerate(records, 1):
        workspace_id = str(record.get("managedWorkspace", {}).get("workspaceId", ""))
        if workspace_id in seen_workspaces:
            raise RuntimeError("RDC index maps one managed workspace more than once")
        seen_workspaces.add(workspace_id)
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

    _match_workspace_resources(
        host, capture_path, capture_session_id, validated
    )

    successful_shader_records: list[dict[str, Any]] = []
    for index, (record, workspace, status) in enumerate(validated, 1):
        shader_summary["attempted"] += 1
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
            applied = _apply_workspace(
                host,
                workspace,
                status,
                expected_revision=int(record["candidateRevision"]),
                expected_candidate_raw_sha256=str(record["candidateRawSha256"]),
                capture_session_id=capture_session_id,
            )
            shader_summary["applied"] += 1
            item = {
                "status": "applied",
                "source": "rdc-index",
                "workspacePath": str(workspace),
                "workspaceId": status["workspaceId"],
                "resourceId": record["identity"]["resourceId"],
                "stage": record["identity"]["stage"],
                "replacementId": applied["replacement"]["replacementId"],
            }
            shader_summary["items"].append(item)
            successful_shader_records.append(copy.deepcopy(dict(record)))
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

    shader_identities = {
        (item["identity"]["resourceId"], item["identity"]["stage"]): item["identity"]
        for item in records
    }
    successful_material_records: list[dict[str, Any]] = []
    material_records = list(binding.get("materials", []) or [])
    for index, record in enumerate(material_records, 1):
        material_summary["attempted"] += 1
        progress.emit(
            "material-restore",
            "Restoring cached CBuffer %d/%d" % (index, len(material_records)),
            current=index,
            total=len(material_records),
            detail={
                "eventId": record.get("eventId"),
                "stage": record.get("stage"),
                "constantBlockPath": record.get("path"),
            },
        )
        try:
            identity = shader_identities.get(
                (record.get("shaderResourceId"), record.get("stage"))
            )
            if identity is None:
                raise RuntimeError("material association has no Shader identity")
            item, clean_record = _restore_material(
                host,
                replay,
                binding_state,
                str(active["workspaceId"]),
                identity,
                record,
                capture_session_id,
            )
            material_summary["restored"] += 1
            material_summary["sessionIds"].append(item["sessionId"])
            material_summary["items"].append(item)
            successful_material_records.append(clean_record)
        except Exception as exc:
            material_summary["failed"] += 1
            material_summary["items"].append(
                {
                    "status": "failed",
                    "source": "rdc-index",
                    "knowledgeId": record.get("knowledgeId"),
                    "eventId": record.get("eventId"),
                    "stage": record.get("stage"),
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )
    return successful_shader_records, successful_material_records


def restore_active_capture_project(
    project_root: str | Path,
    capture_path: str | Path,
    capture_session_id: str | None = None,
    progress_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Restore project Shader/CBuffer knowledge into the active local capture.

    The function is synchronous and safe to call from a persistent-host worker
    thread. A valid RDC cache avoids full-frame export. A stale or missing
    cache never falls back to an implicit full-frame scan; building the complete
    project index is a separate, explicit background operation.
    """

    operation_id = uuid.uuid4().hex
    warnings: list[str] = []
    progress = _Progress(operation_id, progress_callback, warnings)
    result: dict[str, Any] = {
        "schema": RESTORE_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "completed": False,
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
        "warnings": warnings,
    }
    with _RESTORE_GUARD:
        previous_restore_state = automatic_project_sync_suppressed()
        _RESTORE_THREAD_STATE.active = True
        try:
            host = _host_module()
            progress.emit("validate", "Validating active project and capture")
            active, unused_context, project, replay_path, live_session = (
                _validate_capture_and_project(
                    host, project_root, capture_path, capture_session_id
                )
            )
            result["capture"].update({"path": str(Path(capture_path).resolve()), "sessionId": live_session})
            result["project"].update(
                {
                    "root": str(project),
                    "workspaceId": active["workspaceId"],
                    "replayProjectId": active["replayProject"]["projectId"],
                }
            )
            replay = ReplayProjectService(replay_path)
            index_transport: dict[str, Any] | None = None
            index_document: dict[str, Any] | None = None
            selected: dict[str, Any] | None = None
            binding_state: dict[str, Any] | None = None
            action_reason_id = CATALOG_INDEX_REQUIRED_REASON_ID
            progress.emit("index-read", "Reading RDC project association cache")
            try:
                index_transport, index_document = _read_rdc_index(
                    host, Path(capture_path).resolve(), live_session
                )
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
                        binding_state = _project_binding_state(replay, selected)
                        result["cache"] = {
                            "hit": True,
                            "reason": "exact-project-content-binding",
                        }
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
                        progress,
                        result["shaders"],
                        result["materials"],
                    )
                except Exception as exc:
                    # Cache validation failed before it became apply authority.
                    # Do not trust the association and do not replace it with
                    # an implicit full-frame preparation.
                    result["cache"] = {
                        "hit": False,
                        "reason": "cache-live-validation-failed: %s: %s"
                        % (type(exc).__name__, exc),
                    }
                    shader_records = []
                    material_records = []
                    if isinstance(exc, CatalogPreparationRequired):
                        action_reason_id = exc.reason_id
                    selected = None
                    binding_state = None
            else:
                shader_records = []
                material_records = []

            if selected is None or binding_state is None:
                progress.emit(
                    "index-required",
                    "RDC cache unavailable; full Shader index must be built explicitly",
                )
                raise CatalogPreparationRequired(
                    "the full Shader index is unavailable; opening an RDC no longer "
                    "starts an implicit full-frame scan",
                    action_reason_id,
                )

            result["capture"]["captureId"] = binding_state["captureId"]
            host._require_same_active_project(active)
            if shader_records:
                omitted_materials = len(material_records) - len(
                    _binding_document(active, binding_state, shader_records, material_records)["materials"]
                )
                if omitted_materials:
                    warnings.append(
                        "%d restored material association(s) could not be indexed because "
                        "the RDC v1 codec requires an associated managed Shader workspace"
                        % omitted_materials
                    )
                binding = _binding_document(
                    active, binding_state, shader_records, material_records
                )
                progress.emit("index-write", "Writing updated RDC project association cache")
                if index_transport is None:
                    warning = "RDC project index was not read successfully; refusing to overwrite it"
                    result["index"]["warning"] = warning
                    warnings.append(warning)
                else:
                    written, warning, unused_index = _write_index_best_effort(
                        host,
                        index_document,
                        binding,
                        Path(capture_path).resolve(),
                        live_session,
                    )
                    result["index"]["written"] = written
                    if warning:
                        result["index"]["warning"] = warning
                        warnings.append("RDC index write warning: " + warning)
            elif result["materials"]["restored"]:
                warning = (
                    "restored material sessions were not indexed because RDC index v1 "
                    "requires an associated managed Shader workspace"
                )
                result["index"]["warning"] = warning
                warnings.append(warning)

            result["completed"] = True
            progress.emit(
                "complete",
                "ReplayLab project restore completed",
                detail={
                    "shaderApplied": result["shaders"]["applied"],
                    "materialRestored": result["materials"]["restored"],
                    "cacheHit": result["cache"]["hit"],
                },
            )
        except CatalogPreparationRequired as exc:
            # This is an expected fail-closed product state, not a restore
            # fault. Keep the transport successful and expose stable protocol
            # values instead of leaking a Python exception class to qrenderdoc.
            result["outcome"] = "action-required"
            result["reasonId"] = exc.reason_id
        except Exception as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            result["errorType"] = type(exc).__name__
            progress.emit(
                "failed",
                "ReplayLab project restore failed",
                detail={"error": result["error"]},
            )
        finally:
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
        and item.get("originalResourceId") == status.get("shader", {}).get("resourceId")
        and item.get("resourceScoped") is True
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
) -> tuple[bool, str]:
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
            shader_record["identity"]["rawSha256"],
        )
        shaders = [
            item
            for item in shaders
            if (
                item["identity"]["resourceId"],
                item["identity"]["stage"],
                item["identity"]["rawSha256"],
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
    binding = _binding_document(active, binding_state, shaders, materials)
    if material_record is not None and material_record not in binding["materials"]:
        return False, (
            "project committed, but RDC index v1 cannot encode material knowledge "
            "without an associated managed Shader workspace"
        )
    written, warning, unused_index = _write_index_best_effort(
        host, base, binding, capture_path, capture_session_id
    )
    return written, warning


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
        _live_replacement(
            host, status, session_id, replacement, revision, candidate_raw
        )
        active_service = host._active_project_service()
        with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            host._require_same_active_project(active)
            committed = active_service.register_workspace(status)
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
        )
        written, warning = _sync_index_binding(
            host,
            active,
            replay,
            binding_state,
            capture_path,
            session_id,
            shader_record=shader_record,
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
