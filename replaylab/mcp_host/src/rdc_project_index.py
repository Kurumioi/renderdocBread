"""Strict codec for ReplayLab project-association hints embedded in an RDC.

The document produced here is a disposable candidate index.  It deliberately
does not contain replay handles, absolute paths, compiled-Shader authority, or
captured CBuffer bytes.  Callers must still revalidate every Shader, workspace
artifact, CBuffer layout, and live binding before applying project knowledge.

This module only owns the versioned JSON payload.  Native code remains
responsible for placing the payload in an RDC section transactionally.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any


RDC_PROJECT_INDEX_SECTION_NAME = "replaylab/cache/project-reuse-index"
RDC_PROJECT_INDEX_SCHEMA = "replaylab-rdc-project-reuse-index/v1"
RDC_PROJECT_INDEX_VERSION = 1

MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_PROJECT_BINDINGS = 32
MAX_SHADER_RECORDS = 8192
MAX_MATERIAL_RECORDS = 8192
MAX_EVENT_IDS_PER_SHADER = 4096
MAX_TOTAL_EVENT_IDS = 262144
MAX_MATERIAL_PARAMETERS = 256
MAX_TEXT_BYTES = 4096
MAX_ENTRY_POINT_BYTES = 512
MAX_STAGE_BYTES = 32

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RENDERDOC_STAGES = frozenset(
    (
        "vertex",
        "hull",
        "domain",
        "geometry",
        "fragment",
        "compute",
        "task",
        "mesh",
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
    )
)

_INDEX_FIELDS = frozenset(("schema", "schemaVersion", "bindings", "sealSha256"))
_BINDING_FIELDS = frozenset(
    (
        "projectId",
        "workspaceId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
        "shaders",
        "materials",
    )
)
_SHADER_FIELDS = frozenset(
    (
        "identity",
        "knowledgeId",
        "managedWorkspace",
        "candidateRevision",
        "candidateRawSha256",
        "sourceSha256",
    )
)
_SHADER_IDENTITY_FIELDS = frozenset(
    (
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "codeSha256",
        "interfaceHash",
        "layoutHash",
        "interfaceComplete",
        "eventIds",
    )
)
_MANAGED_WORKSPACE_FIELDS = frozenset(("workspaceId", "path"))
_MATERIAL_FIELDS = frozenset(
    (
        "knowledgeId",
        "shaderResourceId",
        "stage",
        "blockIndex",
        "path",
        "layoutSha256",
        "eventId",
        "parameters",
    )
)
_PARAMETER_ID_FIELDS = frozenset(("parameterId",))
_PARAMETER_TYPED_FIELDS = frozenset(
    (
        "parameterId",
        "path",
        "baseType",
        "components",
        "arrayIndex",
        "byteOffset",
        "byteSize",
    )
)
_MATERIAL_BASE_TYPES = frozenset(("Float", "SInt", "UInt"))


class RdcProjectIndexError(ValueError):
    """Raised when an embedded RDC project index is malformed or stale."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RdcProjectIndexError("RDC project index is not canonical JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise RdcProjectIndexError("RDC project index contains non-finite JSON: %s" % value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RdcProjectIndexError(
                "RDC project index contains duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def _require_fields(value: Any, expected: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise RdcProjectIndexError("%s fields are invalid" % label)
    return value


def _hex32(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX32_RE.fullmatch(value) is None:
        raise RdcProjectIndexError("%s must be 32 lowercase hexadecimal characters" % label)
    return value


def _sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RdcProjectIndexError("%s must be a lowercase SHA-256" % label)
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RdcProjectIndexError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise RdcProjectIndexError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def _text(
    value: Any,
    label: str,
    *,
    maximum_bytes: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise RdcProjectIndexError("%s must be text" % label)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RdcProjectIndexError("%s must be valid UTF-8 text" % label) from exc
    if len(encoded) > maximum_bytes:
        raise RdcProjectIndexError("%s exceeds its UTF-8 byte limit" % label)
    if any(ord(character) < 0x20 for character in value):
        raise RdcProjectIndexError("%s contains control characters" % label)
    return value


def _stage(value: Any, label: str) -> str:
    stage = _text(value, label, maximum_bytes=MAX_STAGE_BYTES)
    if _STAGE_RE.fullmatch(stage) is None or stage not in _RENDERDOC_STAGES:
        raise RdcProjectIndexError("%s is not a supported canonical RenderDoc stage" % label)
    return stage


def _project_relative_path(value: Any, label: str) -> str:
    text = _text(value, label)
    if "\\" in text or ":" in text:
        raise RdcProjectIndexError("%s must use a portable project-relative path" % label)
    windows = PureWindowsPath(text)
    posix = PurePosixPath(text)
    if windows.is_absolute() or windows.drive or posix.is_absolute():
        raise RdcProjectIndexError("%s must be project-relative" % label)
    if text.startswith("/") or text.endswith("/") or "//" in text:
        raise RdcProjectIndexError("%s is not a canonical project-relative path" % label)
    parts = posix.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise RdcProjectIndexError("%s escapes or aliases the project root" % label)
    canonical = "/".join(parts)
    if canonical != text:
        raise RdcProjectIndexError("%s is not a canonical project-relative path" % label)
    return canonical


def _semantic_path(value: Any, label: str) -> str:
    text = _text(value, label, maximum_bytes=2048)
    # These are reflected member/block paths, not filesystem paths.  Still
    # reject absolute-looking values so an index never carries an absolute path.
    if (
        text.startswith(("/", "\\"))
        or PureWindowsPath(text).is_absolute()
        or PureWindowsPath(text).drive
    ):
        raise RdcProjectIndexError("%s must not be an absolute path" % label)
    return text


def _normalise_shader_identity(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _SHADER_IDENTITY_FIELDS, label)
    resource_id = _integer(
        source["resourceId"], "%s.resourceId" % label, minimum=1, maximum=(1 << 64) - 1
    )
    stage = _stage(source["stage"], "%s.stage" % label)
    entry_point = _text(
        source["entryPoint"],
        "%s.entryPoint" % label,
        maximum_bytes=MAX_ENTRY_POINT_BYTES,
        allow_empty=True,
    )
    raw_sha256 = _sha256(source["rawSha256"], "%s.rawSha256" % label)
    code_sha256 = _sha256(
        source["codeSha256"], "%s.codeSha256" % label, allow_empty=True
    )
    interface_hash = _sha256(
        source["interfaceHash"], "%s.interfaceHash" % label, allow_empty=True
    )
    layout_hash = _sha256(
        source["layoutHash"], "%s.layoutHash" % label, allow_empty=True
    )
    interface_complete = source["interfaceComplete"]
    if not isinstance(interface_complete, bool):
        raise RdcProjectIndexError("%s.interfaceComplete must be a boolean" % label)
    if interface_complete and not interface_hash:
        raise RdcProjectIndexError(
            "%s.interfaceHash is required for a complete interface" % label
        )
    raw_events = source["eventIds"]
    if (
        not isinstance(raw_events, list)
        or not raw_events
        or len(raw_events) > MAX_EVENT_IDS_PER_SHADER
    ):
        raise RdcProjectIndexError("%s.eventIds is empty or oversized" % label)
    event_ids = [
        _integer(
            event_id,
            "%s.eventIds[%d]" % (label, index),
            minimum=1,
            maximum=(1 << 32) - 1,
        )
        for index, event_id in enumerate(raw_events)
    ]
    if len(set(event_ids)) != len(event_ids):
        raise RdcProjectIndexError("%s.eventIds contains duplicates" % label)
    event_ids.sort()
    return {
        "resourceId": resource_id,
        "stage": stage,
        "entryPoint": entry_point,
        "rawSha256": raw_sha256,
        "codeSha256": code_sha256,
        "interfaceHash": interface_hash,
        "layoutHash": layout_hash,
        "interfaceComplete": interface_complete,
        "eventIds": event_ids,
    }


def _normalise_shader_record(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _SHADER_FIELDS, label)
    workspace = _require_fields(
        source["managedWorkspace"],
        _MANAGED_WORKSPACE_FIELDS,
        "%s.managedWorkspace" % label,
    )
    return {
        "identity": _normalise_shader_identity(source["identity"], "%s.identity" % label),
        "knowledgeId": _hex32(source["knowledgeId"], "%s.knowledgeId" % label),
        "managedWorkspace": {
            "workspaceId": _hex32(
                workspace["workspaceId"], "%s.managedWorkspace.workspaceId" % label
            ),
            "path": _project_relative_path(
                workspace["path"], "%s.managedWorkspace.path" % label
            ),
        },
        "candidateRevision": _integer(
            source["candidateRevision"],
            "%s.candidateRevision" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "candidateRawSha256": _sha256(
            source["candidateRawSha256"], "%s.candidateRawSha256" % label
        ),
        "sourceSha256": _sha256(source["sourceSha256"], "%s.sourceSha256" % label),
    }


def _normalise_parameter(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcProjectIndexError("%s must be an object" % label)
    fields = frozenset(value)
    if fields == _PARAMETER_ID_FIELDS:
        return {"parameterId": _hex32(value["parameterId"], "%s.parameterId" % label)}
    if fields != _PARAMETER_TYPED_FIELDS:
        raise RdcProjectIndexError("%s fields are invalid" % label)
    base_type = value["baseType"]
    if not isinstance(base_type, str) or base_type not in _MATERIAL_BASE_TYPES:
        raise RdcProjectIndexError("%s.baseType is unsupported" % label)
    components = _integer(
        value["components"], "%s.components" % label, minimum=1, maximum=4
    )
    byte_size = _integer(
        value["byteSize"], "%s.byteSize" % label, minimum=4, maximum=16
    )
    if byte_size != components * 4:
        raise RdcProjectIndexError("%s.byteSize does not match its 32-bit components" % label)
    return {
        "parameterId": _hex32(value["parameterId"], "%s.parameterId" % label),
        "path": _semantic_path(value["path"], "%s.path" % label),
        "baseType": base_type,
        "components": components,
        "arrayIndex": _integer(
            value["arrayIndex"], "%s.arrayIndex" % label, minimum=0, maximum=(1 << 31) - 1
        ),
        "byteOffset": _integer(
            value["byteOffset"], "%s.byteOffset" % label, minimum=0, maximum=(1 << 32) - 1
        ),
        "byteSize": byte_size,
    }


def _normalise_material_record(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _MATERIAL_FIELDS, label)
    raw_parameters = source["parameters"]
    if not isinstance(raw_parameters, list) or len(raw_parameters) > MAX_MATERIAL_PARAMETERS:
        raise RdcProjectIndexError("%s.parameters is invalid or oversized" % label)
    parameters = [
        _normalise_parameter(parameter, "%s.parameters[%d]" % (label, index))
        for index, parameter in enumerate(raw_parameters)
    ]
    parameter_ids = [parameter["parameterId"] for parameter in parameters]
    if len(set(parameter_ids)) != len(parameter_ids):
        raise RdcProjectIndexError("%s.parameters contains duplicate IDs" % label)
    parameters.sort(key=lambda parameter: parameter["parameterId"])
    return {
        "knowledgeId": _hex32(source["knowledgeId"], "%s.knowledgeId" % label),
        "shaderResourceId": _integer(
            source["shaderResourceId"],
            "%s.shaderResourceId" % label,
            minimum=1,
            maximum=(1 << 64) - 1,
        ),
        "stage": _stage(source["stage"], "%s.stage" % label),
        "blockIndex": _integer(
            source["blockIndex"], "%s.blockIndex" % label, minimum=0, maximum=65535
        ),
        "path": _semantic_path(source["path"], "%s.path" % label),
        "layoutSha256": _sha256(source["layoutSha256"], "%s.layoutSha256" % label),
        "eventId": _integer(
            source["eventId"], "%s.eventId" % label, minimum=1, maximum=(1 << 32) - 1
        ),
        "parameters": parameters,
    }


def _shader_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = value["identity"]
    workspace = value["managedWorkspace"]
    return (
        identity["resourceId"],
        identity["stage"],
        identity["entryPoint"],
        identity["rawSha256"],
        value["knowledgeId"],
        workspace["workspaceId"],
        workspace["path"],
        value["candidateRevision"],
    )


def _shader_association_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = value["identity"]
    return (
        identity["resourceId"],
        identity["stage"],
        identity["entryPoint"],
        identity["rawSha256"],
    )


def _material_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["shaderResourceId"],
        value["stage"],
        value["eventId"],
        value["blockIndex"],
        value["path"],
        value["knowledgeId"],
    )


def _material_association_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["shaderResourceId"],
        value["stage"],
        value["eventId"],
        value["blockIndex"],
        value["path"],
    )


def _normalise_binding(value: Any, label: str = "binding") -> dict[str, Any]:
    source = _require_fields(value, _BINDING_FIELDS, label)
    raw_shaders = source["shaders"]
    raw_materials = source["materials"]
    if (
        not isinstance(raw_shaders, list)
        or len(raw_shaders) > MAX_SHADER_RECORDS
    ):
        raise RdcProjectIndexError("%s.shaders is invalid or oversized" % label)
    if not isinstance(raw_materials, list) or len(raw_materials) > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("%s.materials is invalid or oversized" % label)

    shaders = [
        _normalise_shader_record(shader, "%s.shaders[%d]" % (label, index))
        for index, shader in enumerate(raw_shaders)
    ]
    shader_keys = [_shader_association_key(shader) for shader in shaders]
    if len(set(shader_keys)) != len(shader_keys):
        raise RdcProjectIndexError("%s.shaders contains duplicate associations" % label)
    shaders.sort(key=_shader_sort_key)

    materials = [
        _normalise_material_record(material, "%s.materials[%d]" % (label, index))
        for index, material in enumerate(raw_materials)
    ]
    material_keys = [_material_association_key(material) for material in materials]
    if len(set(material_keys)) != len(material_keys):
        raise RdcProjectIndexError("%s.materials contains duplicate associations" % label)
    shader_resources = {
        (shader["identity"]["resourceId"], shader["identity"]["stage"])
        for shader in shaders
    }
    for material in materials:
        if (material["shaderResourceId"], material["stage"]) not in shader_resources:
            raise RdcProjectIndexError(
                "%s.materials references a Shader/stage absent from the binding" % label
            )
    materials.sort(key=_material_sort_key)

    return {
        "projectId": _hex32(source["projectId"], "%s.projectId" % label),
        "workspaceId": _hex32(source["workspaceId"], "%s.workspaceId" % label),
        "scopeSha256": _sha256(source["scopeSha256"], "%s.scopeSha256" % label),
        "captureId": _sha256(source["captureId"], "%s.captureId" % label),
        "catalogSha256": _sha256(source["catalogSha256"], "%s.catalogSha256" % label),
        "knowledgeContentSha256": _sha256(
            source["knowledgeContentSha256"], "%s.knowledgeContentSha256" % label
        ),
        "matcherPolicySha256": _sha256(
            source["matcherPolicySha256"], "%s.matcherPolicySha256" % label
        ),
        "shaders": shaders,
        "materials": materials,
    }


def _binding_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return str(value["projectId"]), str(value["workspaceId"])


def _seal_unsigned(bindings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unsigned = {
        "schema": RDC_PROJECT_INDEX_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_VERSION,
        "bindings": copy.deepcopy(list(bindings)),
    }
    return {**unsigned, "sealSha256": _canonical_sha256(unsigned)}


def _normalise_index(value: Any) -> dict[str, Any]:
    source = _require_fields(value, _INDEX_FIELDS, "RDC project index")
    if (
        not isinstance(source["schema"], str)
        or isinstance(source["schemaVersion"], bool)
        or not isinstance(source["schemaVersion"], int)
        or source["schema"] != RDC_PROJECT_INDEX_SCHEMA
        or source["schemaVersion"] != RDC_PROJECT_INDEX_VERSION
    ):
        raise RdcProjectIndexError("RDC project index schema/version is unsupported")
    raw_bindings = source["bindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index bindings are invalid or oversized")
    bindings = [
        _normalise_binding(binding, "bindings[%d]" % index)
        for index, binding in enumerate(raw_bindings)
    ]
    binding_keys = [_binding_key(binding) for binding in bindings]
    if len(set(binding_keys)) != len(binding_keys):
        raise RdcProjectIndexError("RDC project index contains duplicate project bindings")
    bindings.sort(key=_binding_key)

    shader_count = sum(len(binding["shaders"]) for binding in bindings)
    material_count = sum(len(binding["materials"]) for binding in bindings)
    event_count = sum(
        len(shader["identity"]["eventIds"])
        for binding in bindings
        for shader in binding["shaders"]
    )
    if shader_count > MAX_SHADER_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many Shader records")
    if material_count > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many material records")
    if event_count > MAX_TOTAL_EVENT_IDS:
        raise RdcProjectIndexError("RDC project index has too many event references")

    seal = _sha256(source["sealSha256"], "RDC project index sealSha256")
    unsigned = {
        "schema": RDC_PROJECT_INDEX_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_VERSION,
        "bindings": bindings,
    }
    expected = _canonical_sha256(unsigned)
    if not hmac.compare_digest(seal, expected):
        raise RdcProjectIndexError("RDC project index sealSha256 does not match its contents")
    return {**unsigned, "sealSha256": seal}


def new_index() -> dict[str, Any]:
    """Return a new sealed, empty v1 RDC project index."""

    return _seal_unsigned([])


def parse_index(payload: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Parse, bound, canonicalise, and verify one v1 index payload."""

    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RdcProjectIndexError("RDC project index is not valid UTF-8") from exc
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
    else:
        raise RdcProjectIndexError("RDC project index payload must be bytes or text")
    if not raw or len(raw) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError(
            "RDC project index payload must contain 1..%d bytes" % MAX_INDEX_BYTES
        )
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise RdcProjectIndexError("RDC project index is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RdcProjectIndexError("RDC project index is not valid JSON") from exc
    result = _normalise_index(value)
    if len(_canonical_bytes(result)) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError("RDC project index canonical payload is oversized")
    return copy.deepcopy(result)


def encode_index(index: Mapping[str, Any]) -> bytes:
    """Validate and encode one sealed index as canonical UTF-8 JSON."""

    clean = _normalise_index(index)
    payload = _canonical_bytes(clean)
    if len(payload) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError("RDC project index payload is oversized")
    return payload


def upsert_project_binding(
    index: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    """Insert or replace one exact ``(projectId, workspaceId)`` binding."""

    clean_index = _normalise_index(index)
    clean_binding = _normalise_binding(binding)
    wanted = _binding_key(clean_binding)
    bindings = [
        existing
        for existing in clean_index["bindings"]
        if _binding_key(existing) != wanted
    ]
    if len(bindings) >= MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index binding limit reached")
    bindings.append(clean_binding)
    bindings.sort(key=_binding_key)
    result = _seal_unsigned(bindings)
    # Re-run aggregate limits and the seal check before returning publishable data.
    return _normalise_index(result)


def remove_project_binding(
    index: Mapping[str, Any], project_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Remove one workspace binding, or every binding for one project."""

    clean_index = _normalise_index(index)
    project = _hex32(project_id, "projectId")
    workspace = _hex32(workspace_id, "workspaceId") if workspace_id is not None else None
    bindings = [
        binding
        for binding in clean_index["bindings"]
        if not (
            binding["projectId"] == project
            and (workspace is None or binding["workspaceId"] == workspace)
        )
    ]
    return _normalise_index(_seal_unsigned(bindings))


def binding_knowledge_is_current(
    binding: Mapping[str, Any], knowledge_content_sha256: str
) -> bool:
    """Return whether a binding targets the exact current project knowledge."""

    clean = _normalise_binding(binding)
    expected = _sha256(knowledge_content_sha256, "knowledgeContentSha256")
    return hmac.compare_digest(clean["knowledgeContentSha256"], expected)


def select_project_binding(
    index: Mapping[str, Any],
    project_id: str,
    workspace_id: str,
    *,
    knowledge_content_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Select one binding, returning ``None`` when absent or knowledge-stale."""

    clean_index = _normalise_index(index)
    wanted = (_hex32(project_id, "projectId"), _hex32(workspace_id, "workspaceId"))
    expected_knowledge = (
        _sha256(knowledge_content_sha256, "knowledgeContentSha256")
        if knowledge_content_sha256 is not None
        else None
    )
    for binding in clean_index["bindings"]:
        if _binding_key(binding) != wanted:
            continue
        if expected_knowledge is not None and not hmac.compare_digest(
            binding["knowledgeContentSha256"], expected_knowledge
        ):
            return None
        return copy.deepcopy(binding)
    return None


def new() -> dict[str, Any]:
    """Short public alias for :func:`new_index`."""

    return new_index()


def parse(payload: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Short public alias for :func:`parse_index`."""

    return parse_index(payload)


def encode(index: Mapping[str, Any]) -> bytes:
    """Short public alias for :func:`encode_index`."""

    return encode_index(index)


def upsert(index: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Short public alias for :func:`upsert_project_binding`."""

    return upsert_project_binding(index, binding)


def remove(
    index: Mapping[str, Any], project_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Short public alias for :func:`remove_project_binding`."""

    return remove_project_binding(index, project_id, workspace_id)


def select(
    index: Mapping[str, Any],
    project_id: str,
    workspace_id: str,
    *,
    knowledge_content_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Short public alias for :func:`select_project_binding`."""

    return select_project_binding(
        index,
        project_id,
        workspace_id,
        knowledge_content_sha256=knowledge_content_sha256,
    )


__all__ = [
    "MAX_EVENT_IDS_PER_SHADER",
    "MAX_INDEX_BYTES",
    "MAX_MATERIAL_PARAMETERS",
    "MAX_MATERIAL_RECORDS",
    "MAX_PROJECT_BINDINGS",
    "MAX_SHADER_RECORDS",
    "RDC_PROJECT_INDEX_SCHEMA",
    "RDC_PROJECT_INDEX_SECTION_NAME",
    "RDC_PROJECT_INDEX_VERSION",
    "RdcProjectIndexError",
    "binding_knowledge_is_current",
    "encode",
    "encode_index",
    "new",
    "new_index",
    "parse",
    "parse_index",
    "remove",
    "remove_project_binding",
    "select",
    "select_project_binding",
    "upsert",
    "upsert_project_binding",
]
