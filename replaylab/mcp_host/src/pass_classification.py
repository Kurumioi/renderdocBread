"""Deterministic RenderDoc action/pass classification and project knowledge.

The module deliberately has no RenderDoc dependency.  The bridge/host supplies
captured action, pipeline and shader-catalog facts; this module turns those
facts into a foldable tree.  Semantic suggestions from a model are carried in
an isolated ``AIProvisional`` overlay and never change the fixed result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shader_workspace.io import atomic_write_json

CLASSIFICATION_SCHEMA = "replaylab-pass-classification/v1"
CLASSIFICATION_VERSION = 1
KNOWLEDGE_SCHEMA = "replaylab-project-pass-knowledge/v1"
KNOWLEDGE_VERSION = 1

MAX_ACTIONS = 100_000
MAX_PROPOSALS = 100_000
MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_KNOWLEDGE_BYTES = 16 * 1024 * 1024
MAX_TEXT = 4096

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

CATEGORY_ORDER = (
    "Depth",
    "Shadow",
    "BasePass",
    "GBuffer",
    "Lighting",
    "Transparent",
    "Post",
    "UI",
    "Compute",
    "Utility",
    "Unknown",
)
CATEGORIES = frozenset(CATEGORY_ORDER)


# Stable values from RenderDoc's ActionFlags enum.  Symbolic flag names are
# accepted too, so this stays testable without importing renderdoc.dll.
FLAG_CLEAR = 0x0001
FLAG_DRAW = 0x0002
FLAG_DISPATCH = 0x0004
FLAG_MESH_DISPATCH = 0x0008
FLAG_CMD_LIST = 0x0010
FLAG_SET_MARKER = 0x0020
FLAG_PUSH_MARKER = 0x0040
FLAG_POP_MARKER = 0x0080
FLAG_PRESENT = 0x0100
FLAG_MULTI_ACTION = 0x0200
FLAG_COPY = 0x0400
FLAG_RESOLVE = 0x0800
FLAG_GEN_MIPS = 0x1000
FLAG_PASS_BOUNDARY = 0x2000
FLAG_DISPATCH_RAY = 0x4000
FLAG_BUILD_ACCELERATION_STRUCTURE = 0x8000
FLAG_INDIRECT = 0x080000

GRAPHICS_MASK = FLAG_DRAW | FLAG_MESH_DISPATCH
COMPUTE_MASK = FLAG_DISPATCH | FLAG_DISPATCH_RAY
UTILITY_MASK = (
    FLAG_CLEAR
    | FLAG_COPY
    | FLAG_RESOLVE
    | FLAG_GEN_MIPS
    | FLAG_PRESENT
    | FLAG_BUILD_ACCELERATION_STRUCTURE
)
MARKER_MASK = (
    FLAG_CMD_LIST
    | FLAG_SET_MARKER
    | FLAG_PUSH_MARKER
    | FLAG_POP_MARKER
    | FLAG_PASS_BOUNDARY
)


SIGNALS: dict[str, tuple[str, ...]] = {
    "Depth": (
        "depth prepass",
        "depthprepass",
        "pre depth",
        "predepth",
        "z prepass",
        "zprepass",
        "depth only",
        "depthonly",
        "early z",
        "earlyz",
        "hi z",
        "hiz",
    ),
    "Shadow": (
        "shadow",
        "shadowmap",
        "shadow map",
        "shadowdepth",
        "shadow depth",
        "csm",
        "vsm",
        "virtual shadow",
        "sun shadow",
    ),
    "BasePass": (
        "base pass",
        "basepass",
        "forward base",
        "forwardbase",
        "forward opaque",
        "forwardopaque",
        "opaque",
    ),
    "GBuffer": (
        "gbuffer",
        "g buffer",
        "deferred base",
        "deferredbase",
        "deferred geometry",
        "deferredgeometry",
    ),
    "Lighting": (
        "lighting",
        "light pass",
        "lightpass",
        "deferred light",
        "deferredlight",
        "light accumulation",
        "lightaccumulation",
        "ssao",
        "ambient occlusion",
    ),
    "Transparent": (
        "transparent",
        "translucent",
        "translucency",
        "alpha blend",
        "alphablend",
        "particle",
        "water",
    ),
    "Post": (
        "post process",
        "postprocess",
        "post fx",
        "postfx",
        "tonemap",
        "tone map",
        "bloom",
        "taa",
        "fxaa",
        "upscale",
        "upsample",
        "depth of field",
        "dof",
        "motion blur",
        "motionblur",
        "color grade",
        "colorgrade",
    ),
    "UI": (
        "ui",
        "hud",
        "slate",
        "imgui",
        "overlay",
        "canvas",
        "user interface",
    ),
}


class PassClassificationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        raise PassClassificationError("value is not canonical JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bounded_text(value: Any, field: str, *, allow_empty: bool = True) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        value = str(value)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > MAX_TEXT or "\x00" in result:
        raise PassClassificationError(f"{field} is invalid")
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if SHA256_RE.fullmatch(text) else ""


def _marker_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:128]:
        if isinstance(item, Mapping):
            text = item.get("name", item.get("label", ""))
        else:
            text = item
        name = _bounded_text(text, "markerPath item")
        if name:
            result.append(name)
    return result


def _normal_search_text(value: str) -> tuple[str, str, frozenset[str]]:
    # Preserve word boundaries for short tokens such as UI/CSM, while also
    # supporting common concatenated marker names such as ShadowDepths.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    normal = re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()
    words = frozenset(normal.split())
    return normal, "".join(normal.split()), words


def _match_signals(value: str) -> dict[str, list[str]]:
    normal, compact, words = _normal_search_text(value)
    matched: dict[str, list[str]] = {}
    if not normal:
        return matched
    for category, tokens in SIGNALS.items():
        hits: list[str] = []
        for token in tokens:
            token_normal, token_compact, token_words = _normal_search_text(token)
            if not token_normal:
                continue
            if len(token_words) == 1 and len(token_compact) <= 3:
                found = token_compact in words
            else:
                found = token_normal in normal or token_compact in compact
            if found and token not in hits:
                hits.append(token)
        if hits:
            matched[category] = hits
    return matched


def _flag_names(action: Mapping[str, Any]) -> frozenset[str]:
    raw = action.get("flagNames", action.get("flagsText", []))
    if isinstance(raw, str):
        raw = re.split(r"[,|+ ]+", raw)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(re.sub(r"[^a-z]", "", str(item).lower()) for item in raw)


def _has_named_flag(names: frozenset[str], *values: str) -> bool:
    wanted = {re.sub(r"[^a-z]", "", value.lower()) for value in values}
    return bool(names & wanted)


def _action_kind(action: Mapping[str, Any]) -> str:
    flags = _safe_int(action.get("flags"), 0)
    names = _flag_names(action)
    if flags & GRAPHICS_MASK or _has_named_flag(names, "Drawcall", "MeshDispatch"):
        return "graphics"
    if flags & COMPUTE_MASK or _has_named_flag(names, "Dispatch", "DispatchRay"):
        return "compute"
    if flags & UTILITY_MASK or _has_named_flag(
        names, "Clear", "Copy", "Resolve", "GenMips", "Present", "BuildAccStruct"
    ):
        return "utility"
    if flags & FLAG_MULTI_ACTION or _has_named_flag(names, "MultiAction"):
        return "container"
    if flags & MARKER_MASK or _has_named_flag(
        names, "CmdList", "SetMarker", "PushMarker", "PopMarker", "PassBoundary"
    ):
        return "marker"
    return "unknown"


def _is_indirect(action: Mapping[str, Any]) -> bool:
    flags = _safe_int(action.get("flags"), 0)
    if flags & FLAG_INDIRECT or _has_named_flag(_flag_names(action), "Indirect"):
        return True
    text = " ".join(
        [str(action.get("name", "")), *_marker_names(action.get("markerPath"))]
    )
    _, compact, _ = _normal_search_text(text)
    return (
        "executeindirect" in compact
        or "drawindirect" in compact
        or "dispatchindirect" in compact
    )


def _indirect_container_name(action: Mapping[str, Any]) -> str:
    candidates = [*_marker_names(action.get("markerPath")), str(action.get("name", ""))]
    for candidate in reversed(candidates):
        normal, compact, _ = _normal_search_text(candidate)
        if "executeindirect" in compact or "multi draw" in normal:
            return _bounded_text(candidate, "indirect container")
    return "ExecuteIndirect" if _is_indirect(action) else ""


def _normalise_actions(raw: Any) -> tuple[list[dict[str, Any]], int]:
    if isinstance(raw, Mapping):
        raw = raw.get("actions", [])
    if not isinstance(raw, (list, tuple)):
        raise PassClassificationError("actions must be a list or get_draw_calls result")

    flattened: list[dict[str, Any]] = []

    def walk(
        values: Sequence[Any],
        inherited_markers: Sequence[str] = (),
        parent_event_id: int | None = None,
    ) -> None:
        for raw_action in values:
            if not isinstance(raw_action, Mapping):
                raise PassClassificationError("every action must be an object")
            if len(flattened) >= MAX_ACTIONS:
                raise PassClassificationError("action count exceeds the safety limit")
            action = dict(raw_action)
            event_id = _safe_int(action.get("eventId", action.get("event_id")), -1)
            if event_id < 0:
                raise PassClassificationError("action eventId must be non-negative")
            name = _bounded_text(action.get("name", ""), "action.name")
            children = action.get("children", [])
            if children is None:
                children = []
            if not isinstance(children, (list, tuple)):
                raise PassClassificationError("action.children must be a list")
            marker_path = _marker_names(action.get("markerPath")) or list(
                inherited_markers
            )
            action.update(
                {
                    "eventId": event_id,
                    "name": name or f"Action {event_id}",
                    "markerPath": marker_path,
                    "_hasChildren": bool(children),
                    "_parentEventId": parent_event_id,
                }
            )
            action.pop("children", None)
            flattened.append(action)
            if children:
                child_markers = marker_path
                if not child_markers or child_markers[-1] != action["name"]:
                    child_markers = [*child_markers, action["name"]]
                walk(children, child_markers, event_id)

    walk(raw)

    # RenderDoc normally gives unique EIDs.  Some adapters provide both a
    # nested tree and a flattened list; collapse those duplicates and prefer
    # the executable child over a marker/container copy.
    by_event: dict[int, dict[str, Any]] = {}
    duplicates = 0
    rank = {
        "graphics": 5,
        "compute": 5,
        "utility": 4,
        "container": 3,
        "marker": 2,
        "unknown": 1,
    }
    zero_event: list[dict[str, Any]] = []
    for action in flattened:
        event_id = action["eventId"]
        if event_id == 0:
            zero_event.append(action)
            continue
        previous = by_event.get(event_id)
        if previous is None:
            by_event[event_id] = action
            continue
        duplicates += 1
        current_rank = rank[_action_kind(action)]
        previous_rank = rank[_action_kind(previous)]
        selected, other = (
            (action, previous) if current_rank > previous_rank else (previous, action)
        )
        if not selected.get("markerPath") and other.get("markerPath"):
            selected["markerPath"] = list(other["markerPath"])
        selected["_deduplicatedCopies"] = (
            _safe_int(selected.get("_deduplicatedCopies"), 0) + 1
        )
        by_event[event_id] = selected
    result = sorted([*by_event.values(), *zero_event], key=lambda item: item["eventId"])
    return result, duplicates


def _event_index(value: Any) -> dict[int, Mapping[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        if "eventId" in value or "event_id" in value:
            event_id = _safe_int(value.get("eventId", value.get("event_id")), -1)
            return {event_id: value} if event_id >= 0 else {}
        result: dict[int, Mapping[str, Any]] = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                event_id = _safe_int(item.get("eventId", item.get("event_id", key)), -1)
                if event_id >= 0:
                    result[event_id] = item
        return result
    if isinstance(value, (list, tuple)):
        result = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            event_id = _safe_int(item.get("eventId", item.get("event_id")), -1)
            if event_id >= 0:
                result[event_id] = item
        return result
    raise PassClassificationError("pipeline states must be event-indexed objects")


def _shader_display_name(value: Mapping[str, Any]) -> str:
    for field in ("displayName", "name", "debugName", "entryPoint", "entry"):
        text = _bounded_text(value.get(field, ""), "shader name")
        if text:
            return text
    alias = value.get("alias")
    if isinstance(alias, Mapping):
        for field in ("displayName", "alias", "canonicalPath"):
            text = _bounded_text(alias.get(field, ""), "shader alias")
            if text:
                return text
    return ""


def _catalog_index(
    catalog: Any,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Mapping[str, Any]]]:
    if catalog is None:
        return {}, {}
    if not isinstance(catalog, Mapping):
        raise PassClassificationError("shader_catalog must be an object")
    shaders = catalog.get("shaders", [])
    uses = catalog.get("uses", [])
    if not isinstance(shaders, list) or not isinstance(uses, list):
        raise PassClassificationError("shader_catalog shaders/uses must be lists")
    lookup: dict[str, Mapping[str, Any]] = {}
    resource_lookup: dict[tuple[int, str], Mapping[str, Any]] = {}
    for shader in shaders:
        if not isinstance(shader, Mapping):
            continue
        key = str(shader.get("shaderKey", "") or "")
        if key:
            lookup[key] = shader
        resource = _safe_int(
            shader.get("shaderResourceId", shader.get("resourceId")), 0
        )
        stage = str(shader.get("stage", "") or "").strip().lower()
        if resource and stage:
            resource_lookup[(resource, stage)] = shader
    by_event: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for use in uses:
        if not isinstance(use, Mapping):
            continue
        event_id = _safe_int(use.get("eventId"), -1)
        if event_id < 0:
            continue
        merged = dict(use)
        shader = lookup.get(str(use.get("shaderKey", "") or ""))
        if shader is None:
            resource = _safe_int(use.get("resourceId", use.get("shaderResourceId")), 0)
            stage = str(use.get("stage", "") or "").strip().lower()
            shader = resource_lookup.get((resource, stage))
        if shader is not None:
            for key in (
                "rawSha256",
                "codeSha256",
                "interfaceHash",
                "layoutHash",
                "displayName",
                "alias",
                "entryPoint",
                "entry",
            ):
                if not merged.get(key) and shader.get(key) is not None:
                    merged[key] = shader.get(key)
        by_event[event_id].append(merged)
    return dict(by_event), lookup


def _resource_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:4096]:
        if isinstance(item, Mapping):
            resource = _safe_int(
                item.get("resourceId", item.get("resource", item.get("id"))), 0
            )
            entry = {
                "resourceId": resource,
                "name": _bounded_text(
                    item.get("displayName", item.get("name", "")), "resource name"
                ),
                "format": _bounded_text(item.get("format", ""), "resource format"),
            }
        else:
            entry = {"resourceId": _safe_int(item, 0), "name": "", "format": ""}
        if entry["resourceId"] or entry["name"] or entry["format"]:
            result.append(entry)
    return result


def _mapping_path(value: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for component in path:
            if not isinstance(current, Mapping) or component not in current:
                current = None
                break
            current = current[component]
        if current is not None:
            return current
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normal = value.strip().lower()
        if normal in ("true", "enabled", "yes", "1"):
            return True
        if normal in ("false", "disabled", "no", "0"):
            return False
    return None


def _pipeline_bool(pipeline: Mapping[str, Any], name: str) -> bool | None:
    paths: dict[str, tuple[tuple[str, ...], ...]] = {
        "blendEnabled": (
            ("blendEnabled",),
            ("blend", "enabled"),
            ("blendState", "enabled"),
        ),
        "depthTestEnabled": (
            ("depthTestEnabled",),
            ("depth", "testEnabled"),
            ("depthState", "testEnabled"),
            ("depthStencilState", "depthEnable"),
        ),
        "depthWriteEnabled": (
            ("depthWriteEnabled",),
            ("depth", "writeEnabled"),
            ("depthState", "writeEnabled"),
            ("depthStencilState", "depthWrite"),
        ),
    }
    direct = _optional_bool(_mapping_path(pipeline, *paths[name]))
    if direct is not None:
        return direct
    if name == "blendEnabled":
        targets = _mapping_path(
            pipeline, ("blendState", "targets"), ("blend", "targets")
        )
        if isinstance(targets, (list, tuple)):
            values = [
                _optional_bool(item.get("enabled", item.get("blendEnable")))
                for item in targets
                if isinstance(item, Mapping)
            ]
            if any(value is True for value in values):
                return True
            if values and all(value is False for value in values):
                return False
    return None


def _action_feature(
    action: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    catalog_uses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    render_targets = _resource_list(
        pipeline.get("renderTargets", action.get("outputs", []))
    )
    depth_raw = pipeline.get("depthTarget")
    if isinstance(depth_raw, Mapping) or depth_raw:
        depth = _resource_list([depth_raw])
    else:
        depth = []

    read_resources = _resource_list(
        pipeline.get("readResources", pipeline.get("reads", []))
    )
    write_resources = _resource_list(
        pipeline.get("writeResources", pipeline.get("writes", render_targets))
    )

    shaders: list[dict[str, Any]] = []
    pipeline_shaders = pipeline.get("shaders", {})
    if isinstance(pipeline_shaders, Mapping):
        for stage, value in pipeline_shaders.items():
            if not isinstance(value, Mapping):
                continue
            shaders.append(
                {
                    "stage": str(stage).strip().lower(),
                    "resourceId": _safe_int(
                        value.get("shaderId", value.get("resourceId")), 0
                    ),
                    "rawSha256": _sha(value.get("rawSha256")),
                    "codeSha256": _sha(value.get("codeSha256")),
                    "name": _shader_display_name(value),
                }
            )
    for use in catalog_uses:
        stage = str(use.get("stage", "") or "").strip().lower()
        resource = _safe_int(use.get("resourceId", use.get("shaderResourceId")), 0)
        raw = _sha(use.get("rawSha256"))
        match = next(
            (
                shader
                for shader in shaders
                if shader["stage"] == stage
                and (
                    (resource and shader["resourceId"] == resource)
                    or (raw and shader["rawSha256"] == raw)
                )
            ),
            None,
        )
        if match is None:
            match = {
                "stage": stage,
                "resourceId": resource,
                "rawSha256": raw,
                "codeSha256": _sha(use.get("codeSha256")),
                "name": _shader_display_name(use),
            }
            shaders.append(match)
        else:
            if raw:
                match["rawSha256"] = raw
            code = _sha(use.get("codeSha256"))
            if code:
                match["codeSha256"] = code
            display = _shader_display_name(use)
            if display:
                match["name"] = display

    unique_shaders: dict[tuple[str, int, str], dict[str, Any]] = {}
    for shader in shaders:
        key = (shader["stage"], shader["resourceId"], shader["rawSha256"])
        unique_shaders[key] = shader
    shaders = sorted(
        unique_shaders.values(),
        key=lambda item: (item["stage"], item["resourceId"], item["rawSha256"]),
    )

    num_indices = _safe_int(action.get("numIndices"), 0)
    num_vertices = _safe_int(action.get("numVertices", action.get("vertexCount")), 0)
    explicit_fullscreen = _optional_bool(
        pipeline.get("fullscreen", pipeline.get("isFullscreen"))
    )
    triangle_candidate = num_indices == 3 or num_vertices == 3
    fullscreen = explicit_fullscreen is True or (
        explicit_fullscreen is None and triangle_candidate and not depth
    )

    return {
        "eventId": action["eventId"],
        "actionKind": _action_kind(action),
        "name": action["name"],
        "markerPath": list(action.get("markerPath", [])),
        "flags": _safe_int(action.get("flags"), 0),
        "indirect": _is_indirect(action),
        "indirectContainerName": _indirect_container_name(action),
        "renderTargets": render_targets,
        "depthTargets": depth,
        "readResources": read_resources,
        "writeResources": write_resources,
        "blendEnabled": _pipeline_bool(pipeline, "blendEnabled"),
        "depthTestEnabled": _pipeline_bool(pipeline, "depthTestEnabled"),
        "depthWriteEnabled": _pipeline_bool(pipeline, "depthWriteEnabled"),
        "fullscreen": fullscreen,
        "fullscreenExplicit": explicit_fullscreen is not None,
        "shaders": shaders,
        "numIndices": num_indices,
        "numVertices": num_vertices,
        "lateFrameFraction": pipeline.get("lateFrameFraction"),
    }


def _pass_fingerprint(feature: Mapping[str, Any]) -> str:
    shaders = [
        {"stage": item["stage"], "rawSha256": item["rawSha256"]}
        for item in feature.get("shaders", [])
        if item.get("rawSha256")
    ]
    if not shaders:
        return ""
    payload = {
        "actionKind": feature.get("actionKind"),
        "shaders": shaders,
        "renderTargetCount": len(feature.get("renderTargets", [])),
        "hasDepthTarget": bool(feature.get("depthTargets")),
        "targetFormats": sorted(
            item["format"]
            for item in feature.get("renderTargets", [])
            if item.get("format")
        ),
        "depthFormats": sorted(
            item["format"]
            for item in feature.get("depthTargets", [])
            if item.get("format")
        ),
        "blendEnabled": feature.get("blendEnabled"),
        "depthTestEnabled": feature.get("depthTestEnabled"),
        "depthWriteEnabled": feature.get("depthWriteEnabled"),
        # Resource IDs are capture-local.  Counts describe the portable shape
        # without pretending that two numeric IDs match across frames.
        "readResourceCount": len(feature.get("readResources", [])),
        "writeResourceCount": len(feature.get("writeResources", [])),
    }
    return _canonical_sha256(payload)


def _evidence(source: str, code: str, detail: str, weight: float) -> dict[str, Any]:
    return {
        "source": source,
        "code": code,
        "detail": detail,
        "weight": round(float(weight), 4),
        "authority": "FixedProgram",
    }


def _add_score(
    scores: dict[str, float],
    evidence: dict[str, list[dict[str, Any]]],
    category: str,
    weight: float,
    item: dict[str, Any],
) -> None:
    if category not in CATEGORIES or category == "Unknown":
        return
    weight = max(0.0, min(float(weight), 1.0))
    scores[category] = 1.0 - (1.0 - scores.get(category, 0.0)) * (1.0 - weight)
    evidence.setdefault(category, []).append(item)


def _subcategory(category: str, text: str, feature: Mapping[str, Any]) -> str:
    normal, compact, words = _normal_search_text(text)
    if category == "Depth":
        if "hiz" in compact:
            return "HiZ"
        if "prepass" in compact or "predepth" in compact or "earlyz" in compact:
            return "DepthPrepass"
        return "DepthOnly"
    if category == "Shadow":
        if "virtualshadow" in compact or "vsm" in words:
            return "VirtualShadowMap"
        if "csm" in words or "cascade" in normal:
            return "CascadedShadow"
        return "ShadowMap"
    if category == "BasePass":
        return "ForwardOpaque" if "forward" in normal else "OpaqueBasePass"
    if category == "GBuffer":
        return "DeferredGeometry"
    if category == "Lighting":
        if "ssao" in words or "ambient occlusion" in normal:
            return "AmbientOcclusion"
        return "DeferredLighting" if "deferred" in normal else "Lighting"
    if category == "Transparent":
        if "particle" in compact:
            return "Particles"
        if "water" in words:
            return "Water"
        return "Translucency"
    if category == "Post":
        tags = (
            ("tonemap", "ToneMap"),
            ("bloom", "Bloom"),
            ("taa", "TAA"),
            ("fxaa", "FXAA"),
            ("upscale", "Upscale"),
            ("upsample", "Upscale"),
            ("depthoffield", "DepthOfField"),
            ("dof", "DepthOfField"),
            ("motionblur", "MotionBlur"),
            ("colorgrade", "ColorGrading"),
        )
        for token, label in tags:
            if token in compact:
                return label
        return "PostProcess"
    if category == "UI":
        if "imgui" in compact:
            return "ImGui"
        if "slate" in compact:
            return "Slate"
        if "hud" in words:
            return "HUD"
        return "UI"
    if category == "Compute":
        if feature.get("flags", 0) & FLAG_DISPATCH_RAY:
            return "RayDispatch"
        if feature.get("flags", 0) & FLAG_MESH_DISPATCH:
            return "MeshDispatch"
        return "ComputeDispatch"
    if category == "Utility":
        flags = _safe_int(feature.get("flags"), 0)
        for mask, label in (
            (FLAG_CLEAR, "Clear"),
            (FLAG_COPY, "Copy"),
            (FLAG_RESOLVE, "Resolve"),
            (FLAG_GEN_MIPS, "GenerateMips"),
            (FLAG_BUILD_ACCELERATION_STRUCTURE, "BuildAccelerationStructure"),
            (FLAG_PRESENT, "Present"),
        ):
            if flags & mask:
                return label
        return "IndirectContainer" if feature.get("indirect") else "Utility"
    return "Unclassified"


def _knowledge_records(value: Any) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PassClassificationError("project_knowledge must be an object")
    if (
        value.get("schema") != KNOWLEDGE_SCHEMA
        or value.get("schemaVersion") != KNOWLEDGE_VERSION
    ):
        raise PassClassificationError(
            "project pass knowledge schema/version is unsupported"
        )
    result: dict[str, Mapping[str, Any]] = {}
    records = value.get("records", [])
    if not isinstance(records, list):
        raise PassClassificationError("project pass knowledge records must be a list")
    for record in records:
        if not isinstance(record, Mapping):
            continue
        fingerprint = _sha(record.get("passFingerprint"))
        authority = str(record.get("authority", ""))
        if (
            fingerprint
            and record.get("reusable") is True
            and record.get("category") in CATEGORIES - {"Unknown"}
            and authority in ("FixedProgram", "AssetVerified")
        ):
            result[fingerprint] = record
    return result


def _classify_feature(
    feature: Mapping[str, Any],
    project_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    kind = str(feature.get("actionKind", "unknown"))
    scores: dict[str, float] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    exact_classification = False

    if kind == "compute":
        _add_score(
            scores,
            evidence,
            "Compute",
            1.0,
            _evidence(
                "actionFlags",
                "dispatch",
                "ActionFlags identifies a Dispatch/DispatchRay",
                1.0,
            ),
        )
        exact_classification = True
    elif kind == "utility":
        _add_score(
            scores,
            evidence,
            "Utility",
            1.0,
            _evidence(
                "actionFlags",
                "utility-action",
                "ActionFlags identifies a non-draw GPU action",
                1.0,
            ),
        )
        exact_classification = True
    elif kind == "container":
        _add_score(
            scores,
            evidence,
            "Utility",
            1.0,
            _evidence(
                "actionFlags",
                "multi-action-container",
                "MultiAction is a container, not an additional draw",
                1.0,
            ),
        )
        exact_classification = True

    source_texts: list[tuple[str, str, float]] = []
    marker_text = " / ".join(feature.get("markerPath", []))
    if marker_text:
        source_texts.append(("markerPath", marker_text, 0.84))
    source_texts.append(("actionName", str(feature.get("name", "")), 0.72))
    shader_text = " / ".join(
        item.get("name", "") for item in feature.get("shaders", []) if item.get("name")
    )
    if shader_text:
        source_texts.append(("shaderName", shader_text, 0.66))
    target_text = " / ".join(
        item.get("name", "")
        for item in [
            *feature.get("renderTargets", []),
            *feature.get("depthTargets", []),
        ]
        if item.get("name")
    )
    if target_text:
        source_texts.append(("targetName", target_text, 0.72))

    signal_categories: set[str] = set()
    # A Dispatch remains in the Compute top-level category.  Its semantic role
    # is still reported below, but marker text cannot turn it into a verified
    # graphics pass.
    if kind not in ("compute", "utility", "container"):
        for source, text, weight in source_texts:
            for category, matches in _match_signals(text).items():
                signal_categories.add(category)
                _add_score(
                    scores,
                    evidence,
                    category,
                    weight,
                    _evidence(
                        source,
                        "semantic-token",
                        f"{source} matched {', '.join(matches[:4])}",
                        weight,
                    ),
                )

    render_target_count = len(feature.get("renderTargets", []))
    has_depth = bool(feature.get("depthTargets"))
    blend = feature.get("blendEnabled")
    depth_write = feature.get("depthWriteEnabled")
    depth_test = feature.get("depthTestEnabled")
    if kind == "graphics":
        if has_depth and render_target_count == 0:
            if "Shadow" in signal_categories:
                _add_score(
                    scores,
                    evidence,
                    "Shadow",
                    0.78,
                    _evidence(
                        "pipelineState",
                        "depth-only-target",
                        "Depth target is bound with no color target",
                        0.78,
                    ),
                )
            else:
                _add_score(
                    scores,
                    evidence,
                    "Depth",
                    0.96,
                    _evidence(
                        "pipelineState",
                        "depth-only-target",
                        "Depth target is bound with no color target",
                        0.96,
                    ),
                )
                exact_classification = True
        if render_target_count >= 3:
            _add_score(
                scores,
                evidence,
                "GBuffer",
                0.68,
                _evidence(
                    "pipelineState",
                    "multiple-color-targets",
                    f"{render_target_count} color targets are bound",
                    0.68,
                ),
            )
        if blend is True:
            _add_score(
                scores,
                evidence,
                "Transparent",
                0.76,
                _evidence(
                    "blendState",
                    "blend-enabled",
                    "At least one color target has blending enabled",
                    0.76,
                ),
            )
            # Blending alone says translucent composition, not whether the
            # layer is world transparency or UI.  An independent UI name/
            # marker plus disabled depth makes the latter the stronger fixed
            # explanation.
            if "UI" in signal_categories and depth_test is False:
                _add_score(
                    scores,
                    evidence,
                    "UI",
                    0.68,
                    _evidence(
                        "pipelineState",
                        "ui-blend-no-depth",
                        "UI semantic token is corroborated by blending with depth testing disabled",
                        0.68,
                    ),
                )
        if render_target_count and depth_write is True and blend is False:
            _add_score(
                scores,
                evidence,
                "BasePass",
                0.50,
                _evidence(
                    "depthState",
                    "opaque-depth-write",
                    "Color output writes depth with blending disabled",
                    0.50,
                ),
            )
        if (
            feature.get("fullscreen")
            and render_target_count
            and depth_write is not True
        ):
            _add_score(
                scores,
                evidence,
                "Post",
                0.48 if not feature.get("fullscreenExplicit") else 0.62,
                _evidence(
                    "geometry",
                    "fullscreen-draw",
                    "Pipeline/geometry describes a fullscreen draw without depth writes",
                    0.48 if not feature.get("fullscreenExplicit") else 0.62,
                ),
            )
        if (
            feature.get("fullscreen")
            and render_target_count == 1
            and len(feature.get("readResources", [])) >= 3
        ):
            _add_score(
                scores,
                evidence,
                "Lighting",
                0.56,
                _evidence(
                    "resourceFlow",
                    "many-inputs-one-output",
                    "Fullscreen draw reads at least three resources into one target",
                    0.56,
                ),
            )
        late_fraction = feature.get("lateFrameFraction")
        if (
            blend is True
            and depth_test is False
            and isinstance(late_fraction, (int, float))
            and not isinstance(late_fraction, bool)
            and float(late_fraction) >= 0.85
        ):
            _add_score(
                scores,
                evidence,
                "UI",
                0.42,
                _evidence(
                    "eventOrder",
                    "late-blended-no-depth",
                    "Late blended draw has depth testing disabled",
                    0.42,
                ),
            )

    fingerprint = _pass_fingerprint(feature)
    knowledge = project_records.get(fingerprint) if fingerprint else None
    if knowledge is not None and kind not in ("utility", "container"):
        authority = str(knowledge.get("authority"))
        category = str(knowledge.get("category"))
        weight = 0.97 if authority == "AssetVerified" else 0.82
        _add_score(
            scores,
            evidence,
            category,
            weight,
            _evidence(
                "projectKnowledge",
                "exact-pass-fingerprint",
                "Exact portable pass fingerprint matched "
                f"{_safe_int(knowledge.get('captureCount'), 1)} prior capture(s)",
                weight,
            ),
        )
        if authority == "AssetVerified":
            exact_classification = True

    ranked = sorted(
        ((category, score) for category, score in scores.items()),
        key=lambda pair: (-pair[1], CATEGORY_ORDER.index(pair[0])),
    )
    if not ranked:
        category = "Unknown"
        score = 0.0
        selected_evidence = [
            _evidence(
                "classifier",
                "insufficient-fixed-evidence",
                "No fixed marker, shader, target, state or project evidence selected a pass",
                0.0,
            )
        ]
        alternatives: list[dict[str, Any]] = []
    else:
        category, score = ranked[0]
        selected_evidence = evidence.get(category, [])
        alternatives = [
            {"category": other, "score": round(other_score, 4)}
            for other, other_score in ranked[1:]
            if other_score >= 0.25
        ][:4]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - runner_up
    if exact_classification and score >= 0.95 and margin >= 0.10:
        confidence = "confirmed"
    elif score >= 0.64 and margin >= 0.10:
        confidence = "probable"
    else:
        confidence = "candidate"

    combined_text = " / ".join(
        text for unused_source, text, unused_weight in source_texts
    )
    semantic_role = ""
    if category == "Compute":
        semantic_candidates: list[tuple[str, int]] = []
        for unused_source, text, unused_weight in source_texts:
            for role, hits in _match_signals(text).items():
                semantic_candidates.append((role, len(hits)))
        if semantic_candidates:
            semantic_role = min(
                semantic_candidates,
                key=lambda pair: (-pair[1], CATEGORY_ORDER.index(pair[0])),
            )[0]

    limitations: list[str] = []
    if feature.get("indirect"):
        limitations.append(
            "Indirect arguments are GPU-authored; the container is folded, but every concrete child action remains inspectable."
        )
    if category not in ("Compute", "Utility") and confidence != "confirmed":
        limitations.append(
            "Pass semantics are inferred from fixed evidence, not engine-authored annotations."
        )

    result = {
        "eventId": feature["eventId"],
        "name": feature["name"],
        "markerPath": list(feature.get("markerPath", [])),
        "category": category,
        "subcategory": _subcategory(category, combined_text, feature),
        "confidence": confidence,
        "confidenceScore": round(score, 4),
        "authority": "FixedProgram",
        "semanticVerified": bool(
            knowledge is not None
            and knowledge.get("authority") == "AssetVerified"
            and category == knowledge.get("category")
        ),
        "evidence": selected_evidence,
        "alternatives": alternatives,
        "actionKind": kind,
        "indirect": bool(feature.get("indirect")),
        "indirectContainerName": feature.get("indirectContainerName", ""),
        "passFingerprint": fingerprint,
        "shaders": [dict(item) for item in feature.get("shaders", [])],
        "targets": {
            "renderTargets": [dict(item) for item in feature.get("renderTargets", [])],
            "depthTargets": [dict(item) for item in feature.get("depthTargets", [])],
        },
        "stateSummary": {
            "blendEnabled": blend,
            "depthTestEnabled": depth_test,
            "depthWriteEnabled": depth_write,
            "readResourceCount": len(feature.get("readResources", [])),
            "writeResourceCount": len(feature.get("writeResources", [])),
            "fullscreen": bool(feature.get("fullscreen")),
        },
        "limitations": limitations,
    }
    if semantic_role:
        result["semanticRole"] = semantic_role
    return result


def _container_record(action: Mapping[str, Any]) -> dict[str, Any]:
    name = _indirect_container_name(action) or action.get("name", "ExecuteIndirect")
    return {
        "eventId": action["eventId"],
        "name": action["name"],
        "label": name,
        "markerPath": list(action.get("markerPath", [])),
        "category": "Utility",
        "subcategory": "IndirectContainer",
        "confidence": "confirmed",
        "confidenceScore": 1.0,
        "authority": "FixedProgram",
        "semanticVerified": False,
        "evidence": [
            _evidence(
                "actionFlags",
                "multi-action-container",
                "ExecuteIndirect/MultiAction parent is represented as a foldable container",
                1.0,
            )
        ],
        "containerOnly": True,
        "indirect": True,
    }


def _tree_id(*parts: Any) -> str:
    label = ":".join(str(part) for part in parts)
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:20]


def _build_tree(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, dict[tuple[str, str], list[Mapping[str, Any]]]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    for item in items:
        marker_path = item.get("markerPath", [])
        if item.get("indirect"):
            group_kind = "indirect-container"
            group_label = str(item.get("indirectContainerName") or "ExecuteIndirect")
        elif marker_path:
            group_kind = "marker"
            group_label = str(marker_path[-1])
        else:
            group_kind = "ungrouped"
            group_label = "Ungrouped"
        grouped[str(item["category"])][str(item["subcategory"])][
            (group_kind, group_label)
        ].append(item)

    tree: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        if category not in grouped:
            continue
        category_node = {
            "id": _tree_id("category", category),
            "nodeType": "category",
            "label": category,
            "count": 0,
            "children": [],
        }
        for subcategory in sorted(grouped[category]):
            subcategory_node = {
                "id": _tree_id("subcategory", category, subcategory),
                "nodeType": "subcategory",
                "label": subcategory,
                "count": 0,
                "children": [],
            }
            groups = grouped[category][subcategory]
            for (group_kind, group_label), group_items in sorted(
                groups.items(), key=lambda pair: (pair[0][0], pair[0][1].lower())
            ):
                leaves = [
                    {
                        "id": _tree_id("action", item["eventId"]),
                        "nodeType": "action",
                        "eventId": item["eventId"],
                        "label": item["name"],
                        "confidence": item["confidence"],
                        "indirect": item.get("indirect", False),
                    }
                    for item in sorted(group_items, key=lambda value: value["eventId"])
                ]
                group_node = {
                    "id": _tree_id(
                        "group", category, subcategory, group_kind, group_label
                    ),
                    "nodeType": "container"
                    if group_kind == "indirect-container"
                    else "markerGroup",
                    "groupKind": group_kind,
                    "label": group_label,
                    "count": len(leaves),
                    "foldByDefault": group_kind == "indirect-container",
                    "children": leaves,
                }
                subcategory_node["children"].append(group_node)
                subcategory_node["count"] += len(leaves)
            category_node["children"].append(subcategory_node)
            category_node["count"] += subcategory_node["count"]
        tree.append(category_node)
    return tree


def apply_ai_semantic_proposals(
    classification: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Attach review-only semantic proposals without changing the fixed tree."""

    if classification.get("schema") != CLASSIFICATION_SCHEMA:
        raise PassClassificationError("classification schema is unsupported")
    result = deepcopy(dict(classification))
    if not proposals:
        result["provisionalOverlayCount"] = 0
        return result
    if not isinstance(proposals, (list, tuple)) or len(proposals) > MAX_PROPOSALS:
        raise PassClassificationError("AI proposals must be a bounded list")
    items = {
        _safe_int(item.get("eventId"), -1): item
        for item in result.get("items", [])
        if isinstance(item, dict)
    }
    seen: set[int] = set()
    count = 0
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            raise PassClassificationError(f"AI proposal {index} must be an object")
        event_id = _safe_int(proposal.get("eventId"), -1)
        if event_id not in items:
            raise PassClassificationError(
                f"AI proposal references unknown eventId {event_id}"
            )
        if event_id in seen:
            raise PassClassificationError(
                f"AI proposals contain duplicate eventId {event_id}"
            )
        seen.add(event_id)
        proposed_category = _bounded_text(
            proposal.get("category", ""), "AI proposal category"
        )
        if proposed_category and proposed_category not in CATEGORIES:
            raise PassClassificationError("AI proposal category is unsupported")
        overlay = {
            "authority": "AIProvisional",
            "verified": False,
            "applied": False,
            "reviewRequired": True,
            "physicalEvidence": False,
            "proposedCategory": proposed_category,
            "proposedSubcategory": _bounded_text(
                proposal.get("subcategory", ""), "AI proposal subcategory"
            ),
            "displayLabel": _bounded_text(
                proposal.get("displayLabel", proposal.get("label", "")),
                "AI proposal displayLabel",
            ),
            "rationale": _bounded_text(
                proposal.get("rationale", ""), "AI proposal rationale"
            ),
        }
        items[event_id]["semanticProposal"] = overlay
        count += 1
    result["provisionalOverlayCount"] = count
    result["policy"]["aiCanChangeFixedTree"] = False
    result["policy"]["aiProposalAuthority"] = "AIProvisional"
    return result


def classify_passes(
    actions: Any,
    *,
    pipeline_states: Any = None,
    shader_catalog: Mapping[str, Any] | None = None,
    project_knowledge: Mapping[str, Any] | None = None,
    ai_proposals: Sequence[Mapping[str, Any]] | None = None,
    capture_id: str = "",
) -> dict[str, Any]:
    """Classify RenderDoc actions into a deterministic, foldable pass tree.

    ``actions`` accepts either the list returned by the bridge or the complete
    ``get_draw_calls`` result.  ``pipeline_states`` may be a list or a mapping
    keyed by EventId.  Missing evidence degrades confidence; it is never
    invented.
    """

    capture_digest = _sha(capture_id)
    if capture_id and not capture_digest:
        raise PassClassificationError("capture_id must be a lowercase SHA-256")
    normalised, duplicate_count = _normalise_actions(actions)
    pipelines = _event_index(pipeline_states)
    uses_by_event, _ = _catalog_index(shader_catalog)
    project_records = _knowledge_records(project_knowledge)

    items: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for action in normalised:
        kind = _action_kind(action)
        is_indirect_container = kind == "container" or (
            action.get("_hasChildren")
            and _is_indirect(action)
            and kind not in ("graphics", "compute")
        )
        if is_indirect_container:
            containers.append(_container_record(action))
            continue
        if kind == "marker":
            omitted.append(
                {
                    "eventId": action["eventId"],
                    "name": action["name"],
                    "reason": "marker-used-as-tree-context",
                }
            )
            continue
        feature = _action_feature(
            action,
            pipelines.get(action["eventId"], {}),
            uses_by_event.get(action["eventId"], []),
        )
        items.append(_classify_feature(feature, project_records))

    result: dict[str, Any] = {
        "schema": CLASSIFICATION_SCHEMA,
        "schemaVersion": CLASSIFICATION_VERSION,
        "captureId": capture_digest,
        "generatedAt": _utc_now(),
        "items": items,
        "containers": containers,
        "tree": _build_tree(items),
        "omittedEvents": omitted,
        "summary": {
            "inputCount": len(normalised),
            "actionCount": len(items),
            "containerCount": len(containers),
            "markerContextCount": len(omitted),
            "duplicateCount": duplicate_count,
            "categoryCounts": dict(Counter(item["category"] for item in items)),
        },
        "policy": {
            "classificationAuthority": "FixedProgram",
            "aiCanChangeFixedTree": False,
            "aiProposalAuthority": "AIProvisional",
            "eventIdsAreCaptureLocal": True,
            "resourceIdsAreCaptureLocal": True,
            "projectReuseRequiresExactPassFingerprint": True,
            "executeIndirectParentsAreContainers": True,
            "concreteIndirectChildrenRemainVisible": True,
        },
    }
    return apply_ai_semantic_proposals(result, ai_proposals)


_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _operation_lock(root: Path) -> Iterator[None]:
    path = root / ".pass-knowledge-operation.lock"
    token = uuid.uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        raise PassClassificationError(
            "pass knowledge is locked; a stale lock must be reviewed manually"
        ) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if path.read_text(encoding="ascii") == token:
                path.unlink()
        except OSError:
            pass


class PassKnowledgeStore:
    """Content-bound pass observations stored beside a ReplayProjectService project.

    The store is intentionally a separate artifact so the current project v1
    manifest does not need a backwards-incompatible key.  It validates the
    owning project ID/scope on every read and never promotes AI overlays into
    reusable records.
    """

    def __init__(self, project_path: str | Path):
        self.root = Path(project_path).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / "knowledge" / "passes" / "pass_knowledge.v1.json"

    def _project(self) -> dict[str, Any]:
        manifest = self.root / "project.json"
        if (
            self.root.is_symlink()
            or not self.root.is_dir()
            or manifest.is_symlink()
            or not manifest.is_file()
        ):
            raise PassClassificationError(
                "project path must contain a regular project.json"
            )
        if manifest.stat().st_size <= 0 or manifest.stat().st_size > MAX_PROJECT_BYTES:
            raise PassClassificationError("project manifest size is invalid")
        try:
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PassClassificationError(
                "project manifest is not valid UTF-8 JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "renderdoc-ultra-replay-project/v1"
            or value.get("schemaVersion") != 1
        ):
            raise PassClassificationError(
                "unsupported ReplayLab project schema/version"
            )
        project_id = str(value.get("projectId", ""))
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise PassClassificationError("projectId is invalid")
        if not isinstance(value.get("scope"), dict) or not isinstance(
            value.get("captures"), list
        ):
            raise PassClassificationError("project scope/captures are invalid")
        return value

    @staticmethod
    def _empty(project: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "schemaVersion": KNOWLEDGE_VERSION,
            "projectId": project["projectId"],
            "scopeSha256": _canonical_sha256(project["scope"]),
            "updatedAt": _utc_now(),
            "captureObservations": [],
            "records": [],
            "provisionalOverlays": [],
            "policy": {
                "eventIdsReusableAcrossFrames": False,
                "resourceIdsReusableAcrossFrames": False,
                "exactPassFingerprintRequired": True,
                "aiProposalsReusableAuthority": False,
                "aiProposalsApplied": False,
            },
        }

    def load(self) -> dict[str, Any]:
        project = self._project()
        if not self.path.exists():
            return self._empty(project)
        if self.path.is_symlink() or not self.path.is_file():
            raise PassClassificationError(
                "pass knowledge must be a regular non-symlink file"
            )
        if (
            self.path.stat().st_size <= 0
            or self.path.stat().st_size > MAX_KNOWLEDGE_BYTES
        ):
            raise PassClassificationError("pass knowledge size is invalid")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PassClassificationError(
                "pass knowledge is not valid UTF-8 JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != KNOWLEDGE_SCHEMA
            or value.get("schemaVersion") != KNOWLEDGE_VERSION
            or value.get("projectId") != project["projectId"]
            or value.get("scopeSha256") != _canonical_sha256(project["scope"])
        ):
            raise PassClassificationError(
                "pass knowledge does not belong to this exact project scope"
            )
        for key in ("captureObservations", "records", "provisionalOverlays"):
            if not isinstance(value.get(key), list):
                raise PassClassificationError(f"pass knowledge {key} is invalid")
        # Reuse the classifier validator to ensure an altered record cannot
        # silently become classification authority.
        _knowledge_records(value)
        return value

    @staticmethod
    def _observations(
        classification: Mapping[str, Any], capture_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        overlays: list[dict[str, Any]] = []
        for item in classification.get("items", []):
            if not isinstance(item, Mapping):
                continue
            fingerprint = _sha(item.get("passFingerprint"))
            category = str(item.get("category", ""))
            if not fingerprint or category not in CATEGORIES - {"Unknown"}:
                continue
            grouped[fingerprint].append(item)
            proposal = item.get("semanticProposal")
            if isinstance(proposal, Mapping):
                if (
                    proposal.get("authority") != "AIProvisional"
                    or proposal.get("verified") is not False
                ):
                    raise PassClassificationError(
                        "semantic proposal authority was altered"
                    )
                overlays.append(
                    {
                        "captureId": capture_id,
                        "eventId": _safe_int(item.get("eventId"), 0),
                        "passFingerprint": fingerprint,
                        "authority": "AIProvisional",
                        "verified": False,
                        "applied": False,
                        "reviewRequired": True,
                        "proposedCategory": _bounded_text(
                            proposal.get("proposedCategory", ""), "proposedCategory"
                        ),
                        "proposedSubcategory": _bounded_text(
                            proposal.get("proposedSubcategory", ""),
                            "proposedSubcategory",
                        ),
                        "displayLabel": _bounded_text(
                            proposal.get("displayLabel", ""), "displayLabel"
                        ),
                        "rationale": _bounded_text(
                            proposal.get("rationale", ""), "rationale"
                        ),
                    }
                )
        observations: list[dict[str, Any]] = []
        for fingerprint, items in sorted(grouped.items()):
            categories = Counter(str(item["category"]) for item in items)
            subcategories = Counter(str(item["subcategory"]) for item in items)
            evidence_codes = sorted(
                {
                    str(entry.get("code"))
                    for item in items
                    for entry in item.get("evidence", [])
                    if isinstance(entry, Mapping)
                    and entry.get("authority") == "FixedProgram"
                }
            )
            shader_keys = sorted(
                {
                    f"{shader.get('stage', '')}:{shader.get('rawSha256', '')}"
                    for item in items
                    for shader in item.get("shaders", [])
                    if isinstance(shader, Mapping) and _sha(shader.get("rawSha256"))
                }
            )
            observations.append(
                {
                    "captureId": capture_id,
                    "passFingerprint": fingerprint,
                    "categoryCounts": dict(sorted(categories.items())),
                    "subcategoryCounts": dict(sorted(subcategories.items())),
                    "eventCount": len(items),
                    "shaderKeys": shader_keys,
                    "evidenceCodes": evidence_codes,
                    "authority": "FixedProgram",
                    "semanticVerified": False,
                }
            )
        return observations, overlays

    @staticmethod
    def _records(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for observation in observations:
            fingerprint = _sha(observation.get("passFingerprint"))
            if fingerprint:
                grouped[fingerprint].append(observation)
        records: list[dict[str, Any]] = []
        for fingerprint, values in sorted(grouped.items()):
            category_counts: Counter[str] = Counter()
            subcategory_counts: Counter[str] = Counter()
            evidence_codes: set[str] = set()
            shader_keys: set[str] = set()
            for value in values:
                category_counts.update(
                    {
                        str(key): _safe_int(count, 0)
                        for key, count in dict(value.get("categoryCounts", {})).items()
                        if key in CATEGORIES - {"Unknown"} and _safe_int(count, 0) > 0
                    }
                )
                subcategory_counts.update(
                    {
                        str(key): _safe_int(count, 0)
                        for key, count in dict(
                            value.get("subcategoryCounts", {})
                        ).items()
                        if _safe_int(count, 0) > 0
                    }
                )
                evidence_codes.update(
                    str(item) for item in value.get("evidenceCodes", [])
                )
                shader_keys.update(str(item) for item in value.get("shaderKeys", []))
            reusable = len(category_counts) == 1
            category = next(iter(category_counts)) if reusable else "Unknown"
            if reusable and subcategory_counts:
                matching = [(name, count) for name, count in subcategory_counts.items()]
                subcategory = min(matching, key=lambda pair: (-pair[1], pair[0]))[0]
            else:
                subcategory = "Conflicting" if not reusable else "Unclassified"
            records.append(
                {
                    "passFingerprint": fingerprint,
                    "category": category,
                    "subcategory": subcategory,
                    "categoryCounts": dict(sorted(category_counts.items())),
                    "captureCount": len(
                        {str(item.get("captureId")) for item in values}
                    ),
                    "observationCount": sum(
                        _safe_int(item.get("eventCount"), 0) for item in values
                    ),
                    "shaderKeys": sorted(shader_keys),
                    "evidenceCodes": sorted(evidence_codes),
                    "authority": "FixedProgram",
                    "semanticVerified": False,
                    "reusable": reusable,
                    "conflict": not reusable,
                }
            )
        return records

    def save(
        self,
        classification: Mapping[str, Any],
        *,
        capture_id: str = "",
    ) -> dict[str, Any]:
        if (
            classification.get("schema") != CLASSIFICATION_SCHEMA
            or classification.get("schemaVersion") != CLASSIFICATION_VERSION
        ):
            raise PassClassificationError(
                "classification must come from classify_passes"
            )
        digest = _sha(capture_id or classification.get("captureId"))
        if not digest:
            raise PassClassificationError("capture_id must be a lowercase SHA-256")
        project = self._project()
        imported = {
            str(item.get("captureId", ""))
            for item in project.get("captures", [])
            if isinstance(item, Mapping)
        }
        if digest not in imported:
            raise PassClassificationError(
                "capture_id is not imported into this project"
            )
        observations, overlays = self._observations(classification, digest)
        with _store_lock(self.root), _operation_lock(self.root):
            value = self.load()
            value["captureObservations"] = [
                item
                for item in value["captureObservations"]
                if item.get("captureId") != digest
            ] + observations
            value["captureObservations"].sort(
                key=lambda item: (
                    item.get("passFingerprint", ""),
                    item.get("captureId", ""),
                )
            )
            value["provisionalOverlays"] = [
                item
                for item in value["provisionalOverlays"]
                if item.get("captureId") != digest
            ] + overlays
            value["provisionalOverlays"].sort(
                key=lambda item: (
                    item.get("captureId", ""),
                    _safe_int(item.get("eventId"), 0),
                )
            )
            value["records"] = self._records(value["captureObservations"])
            value["updatedAt"] = _utc_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, value)
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "schemaVersion": KNOWLEDGE_VERSION,
            "projectId": value["projectId"],
            "path": str(self.path),
            "captureId": digest,
            "captureObservationCount": len(observations),
            "recordCount": len(value["records"]),
            "reusableRecordCount": sum(
                1 for item in value["records"] if item["reusable"]
            ),
            "provisionalOverlayCount": len(overlays),
            "aiProposalApplied": False,
        }
