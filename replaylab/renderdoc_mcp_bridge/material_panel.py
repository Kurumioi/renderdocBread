"""ReplayLab native material-panel model and MiniQt presentation.

The C++ BufferViewer supplies a selection from its current reflected variable
model.  This module never accepts a byte offset from an MCP/model caller: the
selection is immediately checked against a fresh bridge-owned CBuffer snapshot,
and subsequent edits use only the leaf ID issued by that snapshot.

The controller is deliberately independent from RenderDoc and Qt so its physical
identity, packing, cumulative-patch, revision, and rollback rules can be tested
with fixed data.  ``MiniQtMaterialPanel`` is the small qrenderdoc UI wrapper.

Python 3.6 compatibility is required by the bundled qrenderdoc runtime.
"""

from __future__ import absolute_import

import base64
import collections
import hashlib
import math
import struct


SELECTION_SCHEMA = "renderdoc-ultra-buffer-variable-selection/v1"
SNAPSHOT_SCHEMA = "renderdoc-ultra-event-cbuffer-snapshot/v1"
OVERRIDE_SCHEMA = "renderdoc-ultra-event-cbuffer-override/v1"
_SHA256_CHARS = set("0123456789abcdef")


class MaterialPanelError(RuntimeError):
    pass


def _integer(value, label, minimum=0, maximum=0xFFFFFFFFFFFFFFFF):
    if isinstance(value, bool):
        raise MaterialPanelError("%s must be an integer" % label)
    try:
        result = int(value)
    except Exception:
        raise MaterialPanelError("%s must be an integer" % label)
    if result < minimum or result > maximum:
        raise MaterialPanelError(
            "%s must be between %d and %d" % (label, minimum, maximum))
    return result


def _text(value, label, maximum=4096):
    result = str(value or "")
    if len(result) > maximum:
        raise MaterialPanelError("%s is too long" % label)
    return result


def _hex(value, label, length):
    result = _text(value, label, length).strip().lower()
    if len(result) != length or any(char not in _SHA256_CHARS for char in result):
        raise MaterialPanelError("%s must be %d lowercase hex characters" % (label, length))
    return result


def _sha256(value, label):
    return _hex(value, label, 64)


def _canonical_base_type(value):
    token = str(value or "").strip().replace("_", "").replace(" ", "")
    token = token.split(".")[-1].lower()
    result = {
        "float": "Float",
        "sint": "SInt",
        "int": "SInt",
        "uint": "UInt",
    }.get(token)
    if not result:
        raise MaterialPanelError("Only Float, SInt, and UInt parameters are supported")
    return result


def native_selection_from_callback(data, resource_id_int, normalise_stage):
    """Convert only qrenderdoc's native callback payload to a strict selection.

    This function is intentionally not an MCP method.  ``resource_id_int`` and
    ``normalise_stage`` are supplied by the bridge, so RenderDoc wrapper objects
    are canonicalised by the same fixed code as every replay mutation.
    """
    if not isinstance(data, dict):
        raise MaterialPanelError("The BufferViewer callback did not provide an identity object")
    if str(data.get("schema", "")) != SELECTION_SCHEMA:
        raise MaterialPanelError("Unsupported BufferViewer variable-selection schema")

    stage = str(normalise_stage(data.get("stageName", "")) or "")
    if stage not in ("vertex", "hull", "domain", "geometry", "fragment", "compute",
                     "task", "mesh"):
        raise MaterialPanelError("The selected Shader stage is not supported by materials")

    shader_resource_id = _integer(
        resource_id_int(data.get("shaderResourceId")), "shader resource ID", 1)
    buffer_resource_id = _integer(
        resource_id_int(data.get("bufferResourceId")), "buffer resource ID", 1)
    result = {
        "schema": SELECTION_SCHEMA,
        "eventId": _integer(data.get("eventId"), "event ID", 1, 0xFFFFFFFF),
        "stage": stage,
        "blockIndex": _integer(data.get("blockIndex"), "reflection block index", 0, 16383),
        "arrayIndex": _integer(data.get("arrayIndex"), "binding array index", 0, 0),
        "physicalSlot": _integer(data.get("physicalSlot"), "CBuffer slot", 0, 0xFFFFFFFF),
        "registerSpace": _integer(data.get("registerSpace"), "register space", 0, 0xFFFFFFFF),
        "blockName": _text(data.get("blockName"), "constant-block name"),
        "entryPoint": _text(data.get("entryPoint"), "Shader entry point"),
        "shaderResourceId": shader_resource_id,
        "bufferResourceId": buffer_resource_id,
        "bufferByteOffset": _integer(
            data.get("bufferByteOffset"), "bound buffer byte offset", 0, 0xFFFFFFFFFFFFFFFF),
        "bufferByteSize": _integer(
            data.get("bufferByteSize"), "bound buffer byte size", 16, 64 * 1024),
        "fieldName": _text(data.get("fieldName"), "reflected field name"),
        "fieldByteOffset": _integer(
            data.get("fieldByteOffset"), "reflected field byte offset", 0, 64 * 1024 - 4),
        "fieldBaseType": _canonical_base_type(data.get("fieldBaseType")),
        "fieldComponents": _integer(data.get("fieldComponents"), "field components", 1, 4),
    }
    if result["bufferByteOffset"] % 16 or result["bufferByteSize"] % 16:
        raise MaterialPanelError("The native CBuffer window is not 16-byte aligned")
    if result["fieldByteOffset"] + result["fieldComponents"] * 4 > result["bufferByteSize"]:
        raise MaterialPanelError("The selected reflected field lies outside its bound window")
    return result


def _decode_raw_base64(value, expected_size):
    if not isinstance(value, str):
        raise MaterialPanelError("CBuffer snapshot raw bytes are missing")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        raise MaterialPanelError("CBuffer snapshot raw bytes are not canonical base64")
    if base64.b64encode(decoded) != encoded or len(decoded) != expected_size:
        raise MaterialPanelError("CBuffer snapshot raw bytes have the wrong size")
    return bytes(decoded)


def _leaf_values(raw, leaf):
    start = leaf["byteOffset"]
    end = start + leaf["byteSize"]
    data = raw[start:end]
    fmt = {"Float": "f", "SInt": "i", "UInt": "I"}[leaf["baseType"]]
    return list(struct.unpack("<%d%s" % (leaf["components"], fmt), data))


def _pack_values(leaf, value):
    components = leaf["components"]
    if isinstance(value, (list, tuple)):
        values = list(value)
    elif components == 1:
        values = [value]
    else:
        raise MaterialPanelError("A vector parameter requires %d components" % components)
    if len(values) != components:
        raise MaterialPanelError("Expected %d components, received %d" % (components, len(values)))

    base_type = leaf["baseType"]
    packed_values = []
    for item in values:
        if isinstance(item, bool):
            raise MaterialPanelError("Boolean values are not valid for this reflected type")
        if base_type == "Float":
            try:
                number = float(item)
            except Exception:
                raise MaterialPanelError("Float parameters require finite numbers")
            if not math.isfinite(number):
                raise MaterialPanelError("NaN and infinity are not valid material values")
            packed_values.append(number)
        else:
            if isinstance(item, float) and not item.is_integer():
                raise MaterialPanelError("Integer parameters require whole numbers")
            try:
                number = int(item)
            except Exception:
                raise MaterialPanelError("Integer parameters require whole numbers")
            minimum, maximum = ((-0x80000000, 0x7FFFFFFF)
                                if base_type == "SInt" else (0, 0xFFFFFFFF))
            if number < minimum or number > maximum:
                raise MaterialPanelError("Integer value is outside the reflected 32-bit range")
            packed_values.append(number)

    fmt = {"Float": "f", "SInt": "i", "UInt": "I"}[base_type]
    try:
        packed = struct.pack("<%d%s" % (components, fmt), *packed_values)
    except (OverflowError, struct.error):
        raise MaterialPanelError("Value cannot be represented by the reflected 32-bit type")
    if base_type == "Float":
        unpacked = struct.unpack("<%df" % components, packed)
        if any(not math.isfinite(number) for number in unpacked):
            raise MaterialPanelError("Float value overflows finite 32-bit storage")
    return packed


def parse_parameter_text(text, base_type, components):
    pieces = [piece.strip() for piece in str(text or "").split(",")]
    if len(pieces) != components or any(piece == "" for piece in pieces):
        raise MaterialPanelError("请输入 %d 个以逗号分隔的分量" % components)
    if base_type == "Float":
        try:
            return [float(piece) for piece in pieces]
        except Exception:
            raise MaterialPanelError("浮点参数需要有效数字")
    try:
        return [int(piece, 10) for piece in pieces]
    except Exception:
        raise MaterialPanelError("整数参数需要十进制整数")


def _stage_abbreviation(stage):
    return {
        "vertex": "VS", "hull": "HS", "domain": "DS", "geometry": "GS",
        "fragment": "PS", "compute": "CS", "task": "AS", "mesh": "MS",
    }.get(stage, stage.upper())


def _context_api_token(value):
    token = str(value or "").strip()
    canonical = token.lower().replace("graphicsapi.", "").replace("direct3d", "d3d")
    return token, canonical


class MaterialPanelController(object):
    """Own UI-selected, event-scoped local D3D11/D3D12 material sessions."""

    def __init__(self, snapshot_fn, apply_fn, rollback_fn, context_fn):
        self._snapshot = snapshot_fn
        self._apply = apply_fn
        self._rollback = rollback_fn
        self._context = context_fn
        self._sessions = collections.OrderedDict()
        self._physical_keys = {}

    def _current_context(self):
        value = self._context()
        if not isinstance(value, dict):
            raise MaterialPanelError("ReplayLab current context is unavailable")
        capture = value.get("capture") if isinstance(value.get("capture"), dict) else {}
        events = value.get("events") if isinstance(value.get("events"), dict) else {}
        if not capture.get("loaded"):
            raise MaterialPanelError("No RenderDoc capture is loaded")
        api_name, api_token = _context_api_token(capture.get("api"))
        if not api_token:
            raise MaterialPanelError(
                "The capture API is unavailable; ReplayLab material editing cannot be enabled "
                "safely")
        if api_token not in ("d3d11", "d3d12"):
            raise MaterialPanelError(
                "Current capture API is %s; event-scoped material editing currently supports "
                "D3D11 and D3D12 only" % api_name)
        if capture.get("local") is not True:
            raise MaterialPanelError(
                "This capture is using remote replay; material editing requires local replay")
        if not str(capture.get("captureId", "") or ""):
            raise MaterialPanelError(
                "The capture has no ReplayLab catalog identity yet; run the project's initial "
                "capture analysis before exposing material parameters")
        capture_id = _sha256(capture.get("captureId"), "capture ID")
        capture_session_id = _hex(
            capture.get("captureSessionId"), "capture session ID", 32)
        return {
            "captureId": capture_id,
            "captureSessionId": capture_session_id,
            "eventId": _integer(events.get("currentEventId"), "current event ID", 1),
            "api": "D3D12" if api_token == "d3d12" else "D3D11",
        }

    @staticmethod
    def _normalise_field(value):
        if not isinstance(value, dict):
            raise MaterialPanelError("CBuffer snapshot contains an invalid field")
        leaf = {
            "leafId": _hex(value.get("leafId"), "CBuffer leaf ID", 32),
            "path": _text(value.get("path"), "CBuffer field path"),
            "displayName": _text(
                value.get("displayName", value.get("path")), "CBuffer display name"),
            "byteOffset": _integer(value.get("byteOffset"), "field byte offset", 0, 64 * 1024),
            "byteSize": _integer(value.get("byteSize"), "field byte size", 4, 16),
            "baseType": _canonical_base_type(value.get("baseType")),
            "components": _integer(value.get("components"), "field components", 1, 4),
            "editable": bool(value.get("editable", False)),
        }
        if not leaf["path"] or leaf["byteSize"] != leaf["components"] * 4:
            raise MaterialPanelError("CBuffer snapshot field layout is inconsistent")
        if not leaf["editable"]:
            raise MaterialPanelError("CBuffer snapshot issued a non-editable field")
        return leaf

    def _release_unadopted_snapshot(self, snapshot):
        try:
            self._rollback({
                "snapshot_id": snapshot["snapshotId"],
                "override_id": "",
                "expected_capture_id": snapshot["captureId"],
                "expected_capture_session_id": snapshot["captureSessionId"],
            })
        except Exception:
            pass

    def expose(self, selection):
        if not isinstance(selection, dict) or selection.get("schema") != SELECTION_SCHEMA:
            raise MaterialPanelError("Only a native BufferViewer variable selection can be exposed")
        current = self._current_context()
        if selection["eventId"] != current["eventId"]:
            raise MaterialPanelError("The BufferViewer selection is stale; select the draw again")

        physical_key = (
            current["captureSessionId"], selection["eventId"], selection["stage"],
            selection["physicalSlot"], selection["registerSpace"],
            selection["shaderResourceId"],
            selection["bufferResourceId"], selection["bufferByteOffset"],
            selection["bufferByteSize"],
        )
        session_id = self._physical_keys.get(physical_key)
        session = self._sessions.get(session_id) if session_id else None

        if session is None:
            snapshot = self._snapshot({
                "event_id": selection["eventId"],
                "block_index": selection["blockIndex"],
                "stage": selection["stage"],
                "expected_capture_id": current["captureId"],
            })
            if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
                raise MaterialPanelError("ReplayLab returned an invalid CBuffer snapshot")
            try:
                checks = (
                    ("captureId", _sha256(snapshot.get("captureId"), "snapshot capture ID"),
                     current["captureId"]),
                    ("captureSessionId", _hex(snapshot.get("captureSessionId"),
                                              "snapshot capture session ID", 32),
                     current["captureSessionId"]),
                    ("eventId", _integer(snapshot.get("eventId"), "snapshot event ID", 1),
                     selection["eventId"]),
                    ("stage", str(snapshot.get("stage", "")), selection["stage"]),
                    ("blockIndex", _integer(snapshot.get("blockIndex"),
                                            "snapshot block index"), selection["blockIndex"]),
                    ("slot", _integer(snapshot.get("slot"), "snapshot slot", 0, 0xFFFFFFFF),
                     selection["physicalSlot"]),
                    ("registerSpace", _integer(snapshot.get("registerSpace", 0),
                                                "snapshot register space", 0, 0xFFFFFFFF),
                     selection["registerSpace"]),
                    ("shaderResourceId", _integer(snapshot.get("shaderResourceId"),
                                                  "snapshot shader resource ID", 1),
                     selection["shaderResourceId"]),
                    ("bufferResourceId", _integer(snapshot.get("bufferResourceId"),
                                                  "snapshot buffer resource ID", 1),
                     selection["bufferResourceId"]),
                    ("byteOffset", _integer(snapshot.get("byteOffset"),
                                            "snapshot byte offset"),
                     selection["bufferByteOffset"]),
                    ("byteSize", _integer(snapshot.get("byteSize"),
                                          "snapshot byte size", 16, 64 * 1024),
                     selection["bufferByteSize"]),
                )
                for label, observed, expected in checks:
                    if observed != expected:
                        raise MaterialPanelError(
                            "The live CBuffer %s no longer matches the native selection" % label)

                valid_size = _integer(
                    snapshot.get("validByteSize"), "valid CBuffer byte size", 16, 64 * 1024)
                baseline = _decode_raw_base64(snapshot.get("rawBase64"), valid_size)
                raw_sha = _sha256(snapshot.get("rawSha256"), "snapshot raw hash")
                if hashlib.sha256(baseline).hexdigest() != raw_sha:
                    raise MaterialPanelError("CBuffer snapshot bytes do not match their hash")
                fields = [self._normalise_field(field) for field in snapshot.get("fields", [])]
                fields_by_id = dict((field["leafId"], field) for field in fields)
                if len(fields_by_id) != len(fields):
                    raise MaterialPanelError("CBuffer snapshot contains duplicate leaf IDs")

                session_id = _hex(snapshot.get("snapshotId"), "snapshot ID", 32)
                session = {
                    "sessionId": session_id,
                    "snapshotId": session_id,
                    "captureId": current["captureId"],
                    "captureSessionId": current["captureSessionId"],
                    "api": current["api"],
                    "eventId": selection["eventId"],
                    "stage": selection["stage"],
                    "blockIndex": selection["blockIndex"],
                    "constantBlockPath": _text(
                        snapshot.get("constantBlockPath"), "constant-block path"),
                    "constantBlockDisplayName": _text(
                        snapshot.get("constantBlockDisplayName",
                                     snapshot.get("constantBlockPath")),
                        "constant-block display name"),
                    "slot": selection["physicalSlot"],
                    "registerSpace": selection["registerSpace"],
                    "shaderResourceId": selection["shaderResourceId"],
                    "bufferResourceId": selection["bufferResourceId"],
                    "byteOffset": selection["bufferByteOffset"],
                    "byteSize": selection["bufferByteSize"],
                    "validByteSize": valid_size,
                    "baseline": baseline,
                    "baselineRawSha256": raw_sha,
                    "currentRawSha256": raw_sha,
                    "layoutSha256": _sha256(snapshot.get("layoutSha256"),
                                            "snapshot layout hash"),
                    "revision": 0,
                    "overrideId": "",
                    "fieldsById": fields_by_id,
                    "exposed": collections.OrderedDict(),
                    "patches": collections.OrderedDict(),
                    "physicalKey": physical_key,
                }
            except Exception:
                self._release_unadopted_snapshot(snapshot)
                raise
            self._sessions[session_id] = session
            self._physical_keys[physical_key] = session_id
        elif session["blockIndex"] != selection["blockIndex"]:
            raise MaterialPanelError("The physical CBuffer slot resolves to a different block")

        candidates = []
        for field in session["fieldsById"].values():
            if (field["byteOffset"] == selection["fieldByteOffset"] and
                    field["byteSize"] == selection["fieldComponents"] * 4 and
                    field["baseType"] == selection["fieldBaseType"] and
                    field["components"] == selection["fieldComponents"]):
                candidates.append(field)
        if len(candidates) != 1:
            if not session["exposed"] and not session["patches"]:
                self.rollback(session["sessionId"])
            raise MaterialPanelError(
                "The selected UI variable is not one unique editable leaf in live reflection")

        leaf = candidates[0]
        session["exposed"][leaf["leafId"]] = True
        return self.status(session["sessionId"])

    @staticmethod
    def _rebuilt_bytes(session, patches):
        data = bytearray(session["baseline"])
        for leaf_id, packed in patches.items():
            leaf = session["fieldsById"][leaf_id]
            start = leaf["byteOffset"]
            data[start:start + leaf["byteSize"]] = packed
        return bytes(data)

    def update(self, session_id, parameter_id, value):
        session_id = _hex(session_id, "material session ID", 32)
        parameter_id = _hex(parameter_id, "material parameter ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        current = self._current_context()
        if (current["captureSessionId"] != session["captureSessionId"] or
                current["captureId"] != session["captureId"]):
            raise MaterialPanelError("The material session belongs to an expired capture")
        if current["eventId"] != session["eventId"]:
            raise MaterialPanelError(
                "Select EID %d before editing this event-scoped material" % session["eventId"])
        if parameter_id not in session["exposed"]:
            raise MaterialPanelError("The parameter was not exposed by a native UI selection")
        leaf = session["fieldsById"].get(parameter_id)
        if leaf is None:
            raise MaterialPanelError("The reflected parameter no longer exists")

        packed = _pack_values(leaf, value)
        patches = collections.OrderedDict(session["patches"])
        patches[parameter_id] = packed
        rebuilt = self._rebuilt_bytes(session, patches)
        data_sha = hashlib.sha256(rebuilt).hexdigest()
        patch_values = []
        for leaf_id, leaf_packed in sorted(
                patches.items(), key=lambda item: session["fieldsById"][item[0]]["byteOffset"]):
            patch_values.append({
                "leafId": leaf_id,
                "packedBase64": base64.b64encode(leaf_packed).decode("ascii"),
            })

        response = self._apply({
            "snapshot_id": session["snapshotId"],
            "expected_capture_id": session["captureId"],
            "expected_capture_session_id": session["captureSessionId"],
            "expected_raw_sha256": session["currentRawSha256"],
            "expected_layout_sha256": session["layoutSha256"],
            "expected_revision": session["revision"],
            "new_revision": session["revision"] + 1,
            "expected_override_id": session["overrideId"],
            "data_base64": base64.b64encode(rebuilt).decode("ascii"),
            "data_sha256": data_sha,
            "patches": patch_values,
        })
        if not isinstance(response, dict) or response.get("schema") != OVERRIDE_SCHEMA:
            raise MaterialPanelError("ReplayLab returned an invalid CBuffer override response")
        if (_hex(response.get("snapshotId"), "override snapshot ID", 32) !=
                session["snapshotId"] or
                _integer(response.get("revision"), "override revision") != session["revision"] + 1 or
                _sha256(response.get("currentRawSha256"), "override raw hash") != data_sha or
                not response.get("configured") or not response.get("appliedToCurrentEvent")):
            raise MaterialPanelError("CBuffer override response failed its identity check")

        session["patches"] = patches
        session["currentRawSha256"] = data_sha
        session["revision"] += 1
        session["overrideId"] = _hex(response.get("overrideId"), "override ID", 32)
        return self.status(session_id)

    def update_text(self, session_id, parameter_id, text):
        session = self._sessions.get(session_id)
        if session is None or parameter_id not in session.get("exposed", {}):
            raise MaterialPanelError("Unknown or unexposed material parameter")
        leaf = session["fieldsById"][parameter_id]
        return self.update(
            session_id, parameter_id,
            parse_parameter_text(text, leaf["baseType"], leaf["components"]))

    def _parameter_public(self, session, leaf_id):
        leaf = session["fieldsById"][leaf_id]
        current = self._rebuilt_bytes(session, session["patches"])
        absolute_offset = session["byteOffset"] + leaf["byteOffset"]
        physical = ("EID %d · %s b%d space%d · block[%d] · window +0x%X · "
                    "field +0x%X (buffer +0x%X) · shader %d · buffer %d" % (
                        session["eventId"], _stage_abbreviation(session["stage"]),
                        session["slot"], session["registerSpace"], session["blockIndex"],
                        session["byteOffset"],
                        leaf["byteOffset"], absolute_offset, session["shaderResourceId"],
                        session["bufferResourceId"]))
        return {
            "parameterId": leaf_id,
            "capturedName": leaf["path"],
            "displayName": leaf["displayName"],
            "baseType": leaf["baseType"],
            "components": leaf["components"],
            "byteOffset": leaf["byteOffset"],
            "byteSize": leaf["byteSize"],
            "value": _leaf_values(current, leaf),
            "physicalIdentity": physical,
        }

    def status(self, session_id):
        session_id = _hex(session_id, "material session ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        return {
            "schema": "replaylab-native-material-panel-session/v1",
            "sessionId": session_id,
            "captureId": session["captureId"],
            "captureSessionId": session["captureSessionId"],
            "eventId": session["eventId"],
            "stage": session["stage"],
            "blockIndex": session["blockIndex"],
            "constantBlockPath": session["constantBlockPath"],
            "constantBlockDisplayName": session["constantBlockDisplayName"],
            "slot": session["slot"],
            "registerSpace": session["registerSpace"],
            "shaderResourceId": session["shaderResourceId"],
            "bufferResourceId": session["bufferResourceId"],
            "bufferByteOffset": session["byteOffset"],
            "bufferByteSize": session["byteSize"],
            "revision": session["revision"],
            "overrideId": session["overrideId"],
            "configured": bool(session["overrideId"]),
            "scope": "exact-event",
            "api": session["api"],
            "parameters": [self._parameter_public(session, leaf_id)
                           for leaf_id in session["exposed"]],
        }

    def list_status(self):
        return [self.status(session_id) for session_id in self._sessions]

    def rollback(self, session_id):
        session_id = _hex(session_id, "material session ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        response = self._rollback({
            "snapshot_id": session["snapshotId"],
            "override_id": session["overrideId"],
            "expected_capture_id": session["captureId"],
            "expected_capture_session_id": session["captureSessionId"],
        })
        if not isinstance(response, dict) or response.get("schema") != OVERRIDE_SCHEMA:
            raise MaterialPanelError("ReplayLab returned an invalid rollback response")
        if not response.get("snapshotReleased"):
            raise MaterialPanelError("ReplayLab did not release the CBuffer snapshot")
        self._sessions.pop(session_id, None)
        self._physical_keys.pop(session["physicalKey"], None)
        return response

    def rollback_all(self):
        results = []
        errors = []
        for session_id in list(self._sessions.keys()):
            try:
                results.append(self.rollback(session_id))
            except Exception as exc:
                errors.append("%s: %s" % (session_id, str(exc)))
        if errors:
            raise MaterialPanelError("; ".join(errors))
        return results

    def forget_all(self):
        """Forget UI state after the bridge has invalidated the capture lifecycle."""
        self._sessions.clear()
        self._physical_keys.clear()


class MiniQtMaterialPanel(object):
    """A portable dock panel using qrenderdoc's MiniQt helper only."""

    def __init__(self, ctx, qrd_module, qt_core, controller, closed_callback=None):
        self.ctx = ctx
        self.qrd = qrd_module
        self.qt_core = qt_core
        self.controller = controller
        self.closed_callback = closed_callback
        self.mqt = ctx.Extensions().GetMiniQtHelper()
        self.top = self.mqt.CreateToplevelWidget(
            "ReplayLab Material Parameters", self._closed)
        self.root = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.top, self.root)
        self.capability = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(
            self.capability,
            "Local D3D11/D3D12 · exact-event scope · reflected float/int/uint scalar/vec1-4")
        self.mqt.AddWidget(self.root, self.capability)
        self.content = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.root, self.content)
        self.status_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(self.status_label, "No exposed parameters")
        self.mqt.AddWidget(self.root, self.status_label)
        self.rollback_all_button = self.mqt.CreateButton(self._rollback_all_clicked)
        self.mqt.SetWidgetText(self.rollback_all_button, "Rollback all material overrides")
        self.mqt.AddWidget(self.root, self.rollback_all_button)
        self._docked = False
        self._building = False
        self._generation = {}
        self._row_status = {}
        self._dynamic_roots = []
        self._alive = True
        self._callback_epoch = 0
        self._closed_notified = False
        self._pending_timers = []

    def _retire_timer(self, timer):
        try:
            self._pending_timers.remove(timer)
        except ValueError:
            pass
        try:
            timer.stop()
        except Exception:
            pass
        try:
            timer.timeout.disconnect()
        except Exception:
            pass
        try:
            timer.deleteLater()
        except Exception:
            pass

    def _cancel_pending_timers(self):
        # QTimer.singleShot retains an opaque Python callback that cannot be
        # cancelled.  Owning each debounce timer explicitly lets shutdown drop
        # every callback before qrenderdoc tears down PySide/Python 3.6.
        for timer in list(self._pending_timers):
            self._retire_timer(timer)
        self._pending_timers = []

    def _schedule_debounced(self, callback, delay_ms):
        timer = self.qt_core.QTimer()
        timer.setSingleShot(True)

        def _fired(selected_timer=timer, selected_callback=callback):
            self._retire_timer(selected_timer)
            selected_callback()

        timer.timeout.connect(_fired)
        self._pending_timers.append(timer)
        timer.start(int(delay_ms))

    def _closed(self, _ctx, _widget, _text):
        # This callback runs from QWidget destruction. Never enter replay or MiniQt here: doing so
        # re-enters Qt while its object tree is being torn down and can crash qrenderdoc. Explicit
        # rollback remains available in the panel and shutdown() performs it before destruction.
        if self.top is None and self._closed_notified:
            return
        self._cancel_pending_timers()
        self._alive = False
        self._callback_epoch += 1
        self._generation.clear()
        self._row_status.clear()
        self._dynamic_roots = []
        self._docked = False
        callback = None
        if self.closed_callback and not self._closed_notified:
            self._closed_notified = True
            callback = self.closed_callback
        # Release borrowed native handles before interpreter finalization.  In
        # particular an undocked panel has no ToolWindowManager owner, so its
        # QWidget would otherwise survive until Qt/Python shutdown ordering is
        # already unsafe.
        self.closed_callback = None
        self.top = None
        self.root = None
        self.capability = None
        self.content = None
        self.status_label = None
        self.rollback_all_button = None
        self.ctx = None
        self.qrd = None
        self.qt_core = None
        self.mqt = None
        self.controller = None
        if callback:
            callback()

    def _require_alive(self):
        if not self._alive or self.top is None:
            raise MaterialPanelError("The material panel has already been closed")

    def show(self):
        self._require_alive()
        if not self._docked:
            ref = None
            dock_ref = self.qrd.DockReference.MainToolArea
            try:
                if self.ctx.HasPipelineViewer():
                    ref = self.ctx.GetPipelineViewer().Widget()
                    dock_ref = self.qrd.DockReference.RightOf
            except Exception:
                ref = None
                dock_ref = self.qrd.DockReference.MainToolArea
            self.ctx.AddDockWindow(self.top, dock_ref, ref, 0.30)
            self._docked = True
        self.ctx.RaiseDockWindow(self.top)

    @staticmethod
    def _format_value(parameter):
        if parameter["baseType"] == "Float":
            return ", ".join("%.9g" % value for value in parameter["value"])
        return ", ".join(str(value) for value in parameter["value"])

    def _set_status(self, text):
        self.mqt.SetWidgetText(self.status_label, str(text))

    def expose(self, selection):
        self._require_alive()
        status = self.controller.expose(selection)
        self.rebuild()
        self.show()
        self._set_status(
            "Exposed %d parameter(s) at EID %d; valid edits replay immediately" %
            (len(status["parameters"]), status["eventId"]))
        return status

    def _destroy_dynamic(self):
        self.mqt.ClearContainedWidgets(self.content)
        for widget in self._dynamic_roots:
            try:
                self.mqt.DestroyWidget(widget)
            except Exception:
                pass
        self._dynamic_roots = []
        self._row_status = {}

    def rebuild(self):
        self._require_alive()
        self._building = True
        try:
            self._destroy_dynamic()
            statuses = self.controller.list_status()
            self.mqt.SetWidgetEnabled(self.rollback_all_button, bool(statuses))
            if not statuses:
                empty = self.mqt.CreateLabel()
                self.mqt.SetWidgetText(
                    empty,
                    "Open a Pipeline State CBuffer, right-click a reflected value, then choose\n"
                    "ReplayLab → Expose as Material Parameter.")
                self.mqt.AddWidget(self.content, empty)
                self._dynamic_roots.append(empty)
                self._set_status(
                    "No active material session; controls remain disabled until a native "
                    "D3D11/D3D12 CBuffer leaf is exposed")
            for session in statuses:
                group = self.mqt.CreateGroupBox(True)
                title = "%s — EID %d %s b%d space%d (block[%d])" % (
                    session["constantBlockDisplayName"] or session["constantBlockPath"],
                    session["eventId"], _stage_abbreviation(session["stage"]),
                    session["slot"], session["registerSpace"], session["blockIndex"])
                self.mqt.SetWidgetText(group, title)
                body = self.mqt.CreateVerticalContainer()
                self.mqt.AddWidget(group, body)
                self.mqt.AddWidget(self.content, group)
                self._dynamic_roots.append(group)

                for parameter in session["parameters"]:
                    label = self.mqt.CreateLabel()
                    self.mqt.SetWidgetText(
                        label, "%s [%s%d]\n%s" % (
                            parameter["displayName"], parameter["baseType"],
                            parameter["components"], parameter["physicalIdentity"]))
                    self.mqt.AddWidget(body, label)

                    row = self.mqt.CreateHorizontalContainer()
                    session_id = session["sessionId"]
                    parameter_id = parameter["parameterId"]

                    def _changed(_ctx, widget, _text_value,
                                 sid=session_id, pid=parameter_id):
                        if self._building or not self._alive:
                            return
                        key = (sid, pid)
                        generation = self._generation.get(key, 0) + 1
                        self._generation[key] = generation
                        callback_epoch = self._callback_epoch

                        def _apply_debounced(expected=generation, selected_key=key,
                                             selected_widget=widget,
                                             expected_epoch=callback_epoch):
                            if (not self._alive or self.top is None or
                                    self._callback_epoch != expected_epoch or
                                    self._generation.get(selected_key) != expected):
                                return
                            self._apply_widget_text(
                                selected_key[0], selected_key[1], selected_widget)

                        self._schedule_debounced(_apply_debounced, 180)

                    textbox = self.mqt.CreateTextBox(True, _changed)
                    self.mqt.SetWidgetText(textbox, self._format_value(parameter))
                    self.mqt.AddWidget(row, textbox)

                    def _apply_clicked(_ctx, _widget, _text_value,
                                       sid=session_id, pid=parameter_id,
                                       selected_widget=textbox):
                        if not self._alive or self.top is None:
                            return
                        self._apply_widget_text(sid, pid, selected_widget)

                    apply_button = self.mqt.CreateButton(_apply_clicked)
                    self.mqt.SetWidgetText(apply_button, "Apply")
                    self.mqt.AddWidget(row, apply_button)
                    self.mqt.AddWidget(body, row)

                    row_status = self.mqt.CreateLabel()
                    self.mqt.SetWidgetText(row_status, "Captured baseline")
                    self.mqt.AddWidget(body, row_status)
                    self._row_status[(session_id, parameter_id)] = row_status

                def _rollback_clicked(_ctx, _widget, _text_value,
                                      sid=session["sessionId"]):
                    if not self._alive or self.top is None:
                        return
                    try:
                        self.controller.rollback(sid)
                        self.rebuild()
                        self._set_status("Rolled back and released material session %s" % sid)
                    except Exception as exc:
                        self._set_status("Rollback failed: %s" % str(exc))

                rollback = self.mqt.CreateButton(_rollback_clicked)
                self.mqt.SetWidgetText(rollback, "Rollback this CBuffer")
                self.mqt.AddWidget(body, rollback)
        finally:
            self._building = False

    def _apply_widget_text(self, session_id, parameter_id, widget):
        if not self._alive or self.top is None:
            return
        try:
            text = self.mqt.GetWidgetText(widget)
            status = self.controller.update_text(session_id, parameter_id, text)
            message = "Applied revision %d; EID %d replayed" % (
                status["revision"], status["eventId"])
            label = self._row_status.get((session_id, parameter_id))
            if label:
                self.mqt.SetWidgetText(label, message)
            self._set_status(message)
        except Exception as exc:
            label = self._row_status.get((session_id, parameter_id))
            message = "Not applied: %s" % str(exc)
            if label:
                self.mqt.SetWidgetText(label, message)
            self._set_status(message)

    def _rollback_all_clicked(self, _ctx, _widget, _text):
        if not self._alive or self.top is None:
            return
        try:
            count = len(self.controller.list_status())
            self.controller.rollback_all()
            self.rebuild()
            self._set_status("Rolled back %d material session(s)" % count)
        except Exception as exc:
            self._set_status("Rollback failed: %s" % str(exc))

    def capture_changed(self):
        self.controller.forget_all()
        if self._alive and self._docked:
            self.rebuild()
            self._set_status("Capture changed; all material-panel identities expired")

    def shutdown(self, rollback=True):
        if not self._alive and self.top is None:
            if self.controller is not None:
                self.controller.forget_all()
            return
        controller = self.controller
        top = self.top
        mqt = self.mqt
        was_docked = self._docked
        self._cancel_pending_timers()
        if rollback and controller is not None:
            try:
                controller.rollback_all()
            except Exception as exc:
                print("[ReplayLab] Material panel shutdown rollback error: %s" % str(exc))
        if controller is not None:
            controller.forget_all()
        self._alive = False
        self._callback_epoch += 1
        self._generation.clear()
        try:
            if top is not None and mqt is not None:
                if was_docked:
                    mqt.CloseToplevelWidget(top)
                else:
                    # The panel is constructed eagerly but may never be docked.
                    # MiniQt CloseToplevelWidget cannot delete that orphan.
                    mqt.DestroyWidget(top)
        except Exception:
            pass
        # DestroyWidget is deferred for the undocked case. Detach borrowed
        # handles synchronously and let the native callback become a no-op.
        self._closed(None, top, "")
