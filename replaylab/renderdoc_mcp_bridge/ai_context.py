"""Deterministic RenderDoc UI context and user-intent protocol.

This module deliberately has no RenderDoc, Qt, MCP, AI SDK, network, or secret
dependencies.  The built-in qrenderdoc integration supplies observed facts,
and an external companion claims explicit UI intents.  Keeping this boundary
small makes it possible to audit exactly what was selected when a user clicked
an AI command.

The implementation is compatible with the Python 3.6 runtime embedded by the
supported RenderDoc build.
"""

from __future__ import absolute_import

import copy
import hashlib
import json
import re
import threading
import time
import uuid


CONTEXT_SCHEMA = "renderdoc-ultra-current-context/v1"
INTENT_SCHEMA = "renderdoc-ultra-ui-intent/v1"
INTENT_BATCH_SCHEMA = "renderdoc-ultra-ui-intent-batch/v1"
SCHEMA_VERSION = 1

INTENT_EXPLAIN_EVENT = "explain-current-event"
INTENT_REVERSE_EDIT_SHADER = "reverse-edit-selected-shader"
INTENT_INSPECT_PIXEL = "inspect-picked-pixel"

ALLOWED_INTENTS = (
    INTENT_EXPLAIN_EVENT,
    INTENT_REVERSE_EDIT_SHADER,
    INTENT_INSPECT_PIXEL,
)

SHADER_STAGE_ORDER = (
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

_STAGE_ALIASES = {
    "vs": "vertex",
    "hs": "hull",
    "tess_ctrl": "hull",
    "ds": "domain",
    "tess_eval": "domain",
    "gs": "geometry",
    "ps": "fragment",
    "pixel": "fragment",
    "fs": "fragment",
    "cs": "compute",
    "as": "task",
    "amplification": "task",
    "ms": "mesh",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


class ContextProtocolError(ValueError):
    """Raised when observed UI facts do not satisfy the public protocol."""


def _text(value, label, maximum=4096):
    if value is None:
        return ""
    value = str(value)
    if len(value) > maximum:
        raise ContextProtocolError("%s exceeds %d characters" % (label, maximum))
    return value


def _integer(value, label, minimum=0, maximum=0xFFFFFFFFFFFFFFFF):
    try:
        value = int(value)
    except Exception:
        raise ContextProtocolError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise ContextProtocolError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def normalise_stage(value):
    value = _text(value, "shader stage", 64).strip().lower()
    value = value.replace("shaderstage.", "")
    value = _STAGE_ALIASES.get(value, value)
    if value and value not in SHADER_STAGE_ORDER:
        raise ContextProtocolError("unsupported shader stage: %s" % value)
    return value


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_hash(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalise_capture(value):
    value = value if isinstance(value, dict) else {}
    loaded = bool(value.get("loaded", False))
    session_id = _text(
        value.get("captureSessionId", ""), "capture session ID", 64
    ).lower()
    if loaded and not _SESSION_RE.match(session_id):
        raise ContextProtocolError(
            "a loaded capture requires a 32-character capture session ID"
        )
    capture_id = _text(value.get("captureId", ""), "capture ID", 128).lower()
    if capture_id and not _SHA256_RE.match(capture_id):
        raise ContextProtocolError("capture ID must be an SHA-256 digest when present")
    return {
        "loaded": loaded,
        "captureSessionId": session_id,
        "captureId": capture_id,
        "path": _text(value.get("path", ""), "capture path", 32768),
        "api": _text(value.get("api", ""), "capture API", 128),
        "local": bool(value.get("local", False)),
    }


def _normalise_events(value, loaded):
    value = value if isinstance(value, dict) else {}
    current = _integer(value.get("currentEventId", 0), "current event ID")
    selected = _integer(value.get("selectedEventId", 0), "selected event ID")
    if not loaded and (current or selected):
        raise ContextProtocolError("an unloaded capture cannot have selected events")
    return {
        "currentEventId": current,
        "selectedEventId": selected,
        "selectionDiffersFromReplay": bool(selected != current),
    }


def _normalise_action_item(value, prefix):
    if not isinstance(value, dict):
        raise ContextProtocolError("%s must be an object" % prefix)
    return {
        "eventId": _integer(value.get("eventId", 0), prefix + " event ID"),
        "actionId": _integer(value.get("actionId", 0), prefix + " action ID"),
        "name": _text(value.get("name", ""), prefix + " name", 4096),
        "flags": _text(value.get("flags", ""), prefix + " flags", 1024),
    }


def _normalise_action(value, events):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContextProtocolError("action must be an object")
    action = _normalise_action_item(value, "action")
    marker_path = value.get("markerPath", [])
    if not isinstance(marker_path, list) or len(marker_path) > 128:
        raise ContextProtocolError("marker path must contain at most 128 items")
    action["markerPath"] = [
        _normalise_action_item(item, "marker path item") for item in marker_path
    ]
    action["matchesCurrentEvent"] = bool(
        action["eventId"] == events["currentEventId"]
    )
    return action


def _normalise_shader(value):
    if not isinstance(value, dict):
        raise ContextProtocolError("shader entries must be objects")
    stage = normalise_stage(value.get("stage", ""))
    if not stage:
        raise ContextProtocolError("shader stage is required")
    resource_id = _integer(
        value.get("resourceId", value.get("shaderResourceId", 0)),
        "shader resource ID",
        1,
    )
    raw_hash = _text(value.get("rawSha256", ""), "shader raw SHA-256", 64).lower()
    if raw_hash and not _SHA256_RE.match(raw_hash):
        raise ContextProtocolError("shader raw SHA-256 is invalid")
    return {
        "stage": stage,
        "resourceId": resource_id,
        "entryPoint": _text(value.get("entryPoint", ""), "shader entry point", 4096),
        "rawSha256": raw_hash,
        "exactRawBytesAvailable": bool(raw_hash),
        "displayName": _text(value.get("displayName", ""), "shader display name", 4096),
    }


def _normalise_shaders(values, focus_stage, focus_source):
    if values is None:
        values = []
    if not isinstance(values, list) or len(values) > len(SHADER_STAGE_ORDER):
        raise ContextProtocolError("bound shaders must be a bounded list")
    shaders = [_normalise_shader(value) for value in values]
    shaders.sort(
        key=lambda shader: (
            SHADER_STAGE_ORDER.index(shader["stage"]),
            shader["resourceId"],
            shader["entryPoint"],
            shader["rawSha256"],
        )
    )
    seen_stages = set()
    for shader in shaders:
        if shader["stage"] in seen_stages:
            raise ContextProtocolError(
                "more than one bound shader was reported for stage %s" % shader["stage"]
            )
        seen_stages.add(shader["stage"])

    focus_stage = normalise_stage(focus_stage)
    focus_source = _text(focus_source, "shader focus source", 64).strip().lower()
    if focus_source not in ("", "explicit-ui-button", "deterministic-default"):
        raise ContextProtocolError("unsupported shader focus source")
    focused = None
    if focus_stage:
        for shader in shaders:
            if shader["stage"] == focus_stage:
                focused = copy.deepcopy(shader)
                break
    if focus_stage and focused is None:
        focus_source = ""
    return {
        "bound": shaders,
        "focused": focused,
        "focusSource": focus_source if focused is not None else "none",
        "focusIsExplicit": bool(focused is not None and focus_source == "explicit-ui-button"),
    }


def _normalise_texture(value):
    if value is None:
        return {
            "available": False,
            "resourceId": 0,
            "name": "",
            "subresource": None,
            "pickedPixel": None,
        }
    if not isinstance(value, dict):
        raise ContextProtocolError("texture context must be an object")
    resource_id = _integer(value.get("resourceId", 0), "texture resource ID")
    if resource_id == 0:
        return {
            "available": False,
            "resourceId": 0,
            "name": "",
            "subresource": None,
            "pickedPixel": None,
        }
    sub = value.get("subresource", {})
    if not isinstance(sub, dict):
        raise ContextProtocolError("texture subresource must be an object")
    pixel = value.get("pickedPixel")
    normalised_pixel = None
    if pixel is not None:
        if not isinstance(pixel, dict):
            raise ContextProtocolError("picked pixel must be an object")
        x = _integer(pixel.get("x", -1), "picked pixel x", -1, 0x7FFFFFFF)
        y = _integer(pixel.get("y", -1), "picked pixel y", -1, 0x7FFFFFFF)
        if x >= 0 and y >= 0:
            normalised_pixel = {"x": x, "y": y}
    return {
        "available": True,
        "resourceId": resource_id,
        "name": _text(value.get("name", ""), "texture name", 4096),
        "subresource": {
            "mip": _integer(sub.get("mip", 0), "texture mip"),
            "slice": _integer(sub.get("slice", 0), "texture slice"),
            "sample": _integer(sub.get("sample", 0), "texture sample"),
        },
        "pickedPixel": normalised_pixel,
    }


def build_context(facts):
    """Validate observed facts and return a canonical, content-addressed snapshot."""
    if not isinstance(facts, dict):
        raise ContextProtocolError("context facts must be an object")
    capture = _normalise_capture(facts.get("capture"))
    events = _normalise_events(facts.get("events"), capture["loaded"])
    action = _normalise_action(facts.get("action"), events)
    shaders = _normalise_shaders(
        facts.get("boundShaders", []),
        facts.get("focusStage", ""),
        facts.get("focusSource", ""),
    )
    texture = _normalise_texture(facts.get("texture"))
    warnings = facts.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 64:
        raise ContextProtocolError("warnings must contain at most 64 strings")
    warnings = sorted(set(_text(item, "context warning", 4096) for item in warnings))

    payload = {
        "schema": CONTEXT_SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "contextGeneration": _integer(
            facts.get("contextGeneration", 0), "context generation"
        ),
        "capture": capture,
        "events": events,
        "action": action,
        "shaders": shaders,
        "texture": texture,
        "focusedResource": (
            {
                "kind": "texture",
                "resourceId": texture["resourceId"],
                "name": texture["name"],
            }
            if texture["available"]
            else None
        ),
        "warnings": warnings,
        "capabilities": {
            "canExplainCurrentEvent": bool(
                capture["loaded"] and events["currentEventId"] > 0
            ),
            "canReverseEditFocusedShader": bool(
                shaders["focused"] is not None
                and shaders["focused"]["exactRawBytesAvailable"]
            ),
            "canInspectPickedPixel": bool(
                texture["available"] and texture["pickedPixel"] is not None
            ),
        },
    }
    payload["snapshotId"] = _content_hash(payload)
    return payload


def validate_context(snapshot):
    """Reject mutated snapshots before they are attached to a UI intent."""
    if not isinstance(snapshot, dict):
        raise ContextProtocolError("context snapshot must be an object")
    supplied = _text(snapshot.get("snapshotId", ""), "context snapshot ID", 64).lower()
    if not _SHA256_RE.match(supplied):
        raise ContextProtocolError("context snapshot ID is invalid")
    body = copy.deepcopy(snapshot)
    body.pop("snapshotId", None)
    if body.get("schema") != CONTEXT_SCHEMA or body.get("schemaVersion") != SCHEMA_VERSION:
        raise ContextProtocolError("unsupported context schema")
    if _content_hash(body) != supplied:
        raise ContextProtocolError("context snapshot content hash does not match")
    return copy.deepcopy(snapshot)


def _intent_arguments(kind, context):
    events = context["events"]
    if kind == INTENT_EXPLAIN_EVENT:
        if not context["capabilities"]["canExplainCurrentEvent"]:
            raise ContextProtocolError("there is no current event to explain")
        return {
            "eventId": events["currentEventId"],
            "selectedEventId": events["selectedEventId"],
        }
    if kind == INTENT_REVERSE_EDIT_SHADER:
        shader = context["shaders"]["focused"]
        if not context["shaders"]["focusIsExplicit"]:
            raise ContextProtocolError(
                "reverse/edit requires an explicitly stage-scoped RenderDoc UI command"
            )
        if not shader or not shader["exactRawBytesAvailable"]:
            raise ContextProtocolError("the focused shader has no exact captured bytes")
        return {
            "eventId": events["currentEventId"],
            "stage": shader["stage"],
            "shaderResourceId": shader["resourceId"],
            "entryPoint": shader["entryPoint"],
            "rawSha256": shader["rawSha256"],
        }
    if kind == INTENT_INSPECT_PIXEL:
        texture = context["texture"]
        if not context["capabilities"]["canInspectPickedPixel"]:
            raise ContextProtocolError("there is no picked texture pixel to inspect")
        return {
            "eventId": events["currentEventId"],
            "textureResourceId": texture["resourceId"],
            "subresource": copy.deepcopy(texture["subresource"]),
            "pixel": copy.deepcopy(texture["pickedPixel"]),
        }
    raise ContextProtocolError("unsupported UI intent: %s" % kind)


def _recommended_workflow(kind):
    """Describe bounded authority granted by one explicit RenderDoc UI click.

    The click authorises the operation named by the button, not an open-ended
    model session. The Reverse/Edit button explicitly authorises a bounded
    live-edit lease for this exact Shader identity; each saved candidate still
    needs a fresh compile, ABI check and live-capture check before a
    sharing-preserving replacement. Keeping the allow-list in the signed intent
    prevents the model's own tool choice from expanding that scope.
    """
    if kind == INTENT_EXPLAIN_EVENT:
        steps = [
            {"tool": "get_draw_call_details", "argumentSource": "intent.eventId"},
            {"tool": "get_pipeline_state", "argumentSource": "intent.eventId"},
        ]
        companion_configuration = []
        allowed_operations = ["read-capture", "explain-event"]
    elif kind == INTENT_REVERSE_EDIT_SHADER:
        steps = [
            {
                "tool": "reverse_shader",
                "argumentSource": "intent.eventId + intent.stage",
            },
            {
                "tool": "prepare_shader_workspace",
                "argumentSource": "intent identity + companion-owned configuration",
            },
            {
                "tool": "start_shader_workspace_live_edit",
                "argumentSource": "validated executable workspace",
            },
        ]
        companion_configuration = [
            "workspace_root",
            "registered_shader_toolchain",
            "active_replay_project",
        ]
        allowed_operations = [
            "read-capture",
            "synchronise-active-project",
            "resolve-project-symbols",
            "export-shader",
            "create-workspace",
            "decompile-shader",
            "organise-source-with-ai",
            "persist-project-knowledge",
            "open-external-editor",
            "watch-and-compile-saves",
            "apply-compatible-shared-resource-saves",
        ]
    elif kind == INTENT_INSPECT_PIXEL:
        steps = [
            {
                "tool": "inspect_pixel_object",
                "argumentSource": "intent texture/subresource/pixel",
            }
        ]
        companion_configuration = []
        allowed_operations = ["read-capture", "inspect-picked-pixel"]
    else:
        raise ContextProtocolError("unsupported UI intent: %s" % kind)
    return {
        # The allow-list is an actual, bounded grant from the explicit UI
        # click. The ordered steps remain suggestions; a companion may use a
        # different deterministic sequence as long as every operation is in
        # allowedOperations and the independent mutation gates still hold.
        "advisoryOnly": False,
        "suggestedStepsAdvisory": True,
        "automaticExecutionAuthorized": True,
        "authorizationSource": "explicit-renderdoc-ui-click",
        "allowedOperations": allowed_operations,
        "replayMutationAuthorized": False,
        "arbitraryFileMutationAuthorized": False,
        "requiresFreshContextValidationBeforeMutation": True,
        "steps": steps,
        "requiredCompanionConfiguration": companion_configuration,
    }


class IntentQueue(object):
    """Bounded at-least-once queue for explicit RenderDoc UI commands.

    A claim carries a random lease token.  Only the holder of the live token can
    complete the intent; expired claims return to the queue.  The queue is
    process-local by design and is cleared when the integration stops.
    """

    def __init__(self, maximum=256, clock=None, id_factory=None):
        self.maximum = _integer(maximum, "maximum intent count", 1, 4096)
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._records = []
        self._sequence = 0
        self._lock = threading.RLock()

    def _new_id(self):
        value = _text(self._id_factory(), "generated intent ID", 128).lower()
        if not value or any(record["intentId"] == value for record in self._records):
            raise ContextProtocolError("generated intent ID is empty or duplicated")
        return value

    def _expire_claims(self, now):
        for record in self._records:
            if record["state"] == "claimed" and record["leaseExpiresAt"] <= now:
                record["state"] = "queued"
                record["claimedBy"] = ""
                record["leaseToken"] = ""
                record["leaseExpiresAt"] = 0.0

    def _make_room(self):
        while len(self._records) >= self.maximum:
            removable = None
            for index, record in enumerate(self._records):
                if record["state"] in ("completed", "failed", "rejected", "cancelled"):
                    removable = index
                    break
            if removable is None:
                raise ContextProtocolError("the RenderDoc UI intent queue is full")
            del self._records[removable]

    def enqueue(self, kind, context):
        kind = _text(kind, "intent kind", 128).strip().lower()
        if kind not in ALLOWED_INTENTS:
            raise ContextProtocolError("unsupported UI intent: %s" % kind)
        context = validate_context(context)
        arguments = _intent_arguments(kind, context)
        with self._lock:
            self._make_room()
            self._sequence += 1
            record = {
                "schema": INTENT_SCHEMA,
                "schemaVersion": SCHEMA_VERSION,
                "intentId": self._new_id(),
                "sequence": self._sequence,
                "kind": kind,
                "origin": "renderdoc-ui",
                "state": "queued",
                "contextSnapshotId": context["snapshotId"],
                "context": context,
                "arguments": arguments,
                "recommendedWorkflow": _recommended_workflow(kind),
                "attempts": 0,
                "claimedBy": "",
                "leaseToken": "",
                "leaseExpiresAt": 0.0,
                "outcomeSummary": "",
            }
            self._records.append(record)
            return copy.deepcopy(record)

    def claim(self, consumer_id, maximum=1, lease_seconds=30):
        consumer_id = _text(consumer_id, "intent consumer ID", 128).strip()
        if not consumer_id:
            raise ContextProtocolError("intent consumer ID is required")
        maximum = _integer(maximum, "claim count", 1, 16)
        lease_seconds = _integer(lease_seconds, "lease seconds", 5, 300)
        now = float(self._clock())
        claimed = []
        with self._lock:
            self._expire_claims(now)
            for record in self._records:
                if record["state"] != "queued":
                    continue
                record["state"] = "claimed"
                record["attempts"] += 1
                record["claimedBy"] = consumer_id
                record["leaseToken"] = self._new_id()
                record["leaseExpiresAt"] = now + lease_seconds
                claimed.append(copy.deepcopy(record))
                if len(claimed) >= maximum:
                    break
        return {
            "schema": INTENT_BATCH_SCHEMA,
            "schemaVersion": SCHEMA_VERSION,
            "consumerId": consumer_id,
            "intents": claimed,
        }

    def complete(self, intent_id, lease_token, outcome, summary=""):
        intent_id = _text(intent_id, "intent ID", 128).lower()
        lease_token = _text(lease_token, "intent lease token", 128).lower()
        outcome = _text(outcome, "intent outcome", 32).strip().lower()
        summary = _text(summary, "intent outcome summary", 4096)
        if outcome not in ("completed", "failed", "rejected"):
            raise ContextProtocolError("unsupported intent outcome")
        now = float(self._clock())
        with self._lock:
            self._expire_claims(now)
            for record in self._records:
                if record["intentId"] != intent_id:
                    continue
                if record["state"] != "claimed":
                    raise ContextProtocolError("the intent does not have a live claim")
                if record["leaseToken"] != lease_token:
                    raise ContextProtocolError("the intent lease token does not match")
                record["state"] = outcome
                record["outcomeSummary"] = summary
                record["leaseToken"] = ""
                record["leaseExpiresAt"] = 0.0
                return copy.deepcopy(record)
        raise ContextProtocolError("unknown intent ID")

    def renew(self, intent_id, lease_token, lease_seconds=30):
        """Extend only the caller's still-live claim.

        Long decompilation/model operations can exceed the original lease. A
        renewal never revives an expired claim and never changes its owner or
        token, so another consumer cannot steal or resurrect work by guessing
        an intent ID.
        """
        intent_id = _text(intent_id, "intent ID", 128).lower()
        lease_token = _text(lease_token, "intent lease token", 128).lower()
        lease_seconds = _integer(lease_seconds, "lease seconds", 5, 300)
        now = float(self._clock())
        with self._lock:
            self._expire_claims(now)
            for record in self._records:
                if record["intentId"] != intent_id:
                    continue
                if record["state"] != "claimed":
                    raise ContextProtocolError("the intent does not have a live claim")
                if record["leaseToken"] != lease_token:
                    raise ContextProtocolError("the intent lease token does not match")
                record["leaseExpiresAt"] = now + lease_seconds
                return copy.deepcopy(record)
        raise ContextProtocolError("unknown intent ID")

    def status(self, intent_id):
        intent_id = _text(intent_id, "intent ID", 128).lower()
        now = float(self._clock())
        with self._lock:
            self._expire_claims(now)
            for record in self._records:
                if record["intentId"] == intent_id:
                    return copy.deepcopy(record)
        raise ContextProtocolError("unknown intent ID")

    def cancel_for_capture_change(self, active_capture_session_id):
        active_capture_session_id = _text(
            active_capture_session_id, "active capture session ID", 64
        ).lower()
        cancelled = 0
        with self._lock:
            for record in self._records:
                if record["state"] not in ("queued", "claimed"):
                    continue
                record_session = record["context"]["capture"]["captureSessionId"]
                if not active_capture_session_id or record_session != active_capture_session_id:
                    record["state"] = "cancelled"
                    record["claimedBy"] = ""
                    record["leaseToken"] = ""
                    record["leaseExpiresAt"] = 0.0
                    record["outcomeSummary"] = "capture session changed"
                    cancelled += 1
        return cancelled


__all__ = [
    "ALLOWED_INTENTS",
    "CONTEXT_SCHEMA",
    "ContextProtocolError",
    "INTENT_BATCH_SCHEMA",
    "INTENT_EXPLAIN_EVENT",
    "INTENT_INSPECT_PIXEL",
    "INTENT_REVERSE_EDIT_SHADER",
    "INTENT_SCHEMA",
    "IntentQueue",
    "build_context",
    "normalise_stage",
    "validate_context",
]
