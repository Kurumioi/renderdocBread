"""IDE-style workspace for reviewed, organised and shared ReplayLab HLSL.

The panel deliberately has no independent filesystem write authority. Its tree
and documents come only from live-edit records bound to the current capture.
The project library is a backend matching source. Every save goes to the authenticated
singleton Host, where the exact in-memory live-edit job owns the source CAS,
compile, ABI validation, RenderDoc replacement and last-good state.
There is no direct-disk fallback when that Host is unavailable.

This module remains Python 3.6/PySide2 compatible for qrenderdoc's embedded
interpreter. It is named ``project_*.py`` so all user-visible text is extracted
into qrenderdoc's ordinary Qt translation catalog.
"""

from __future__ import print_function

import hashlib
import importlib.util
import json
import os
import queue
import re
import threading
import time
import uuid


try:
    from . import shader_console as _shader_console_protocol
    from . import shader_editor_client as _shader_save_client_protocol
    from . import project_shader_flow_graph as _shader_flow_graph
    from . import project_shader_flow_hierarchy as _shader_flow_hierarchy
except (ImportError, ValueError):
    _shader_console_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_console_protocol",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "shader_console.py"))
    if _shader_console_spec is None or _shader_console_spec.loader is None:
        raise ImportError("Unable to load the ReplayLab Shader Console protocol")
    _shader_console_protocol = importlib.util.module_from_spec(_shader_console_spec)
    _shader_console_spec.loader.exec_module(_shader_console_protocol)
    _shader_client_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_save_client",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "shader_editor_client.py"))
    if _shader_client_spec is None or _shader_client_spec.loader is None:
        raise ImportError("Unable to load the ReplayLab Shader save client")
    _shader_save_client_protocol = importlib.util.module_from_spec(
        _shader_client_spec)
    _shader_client_spec.loader.exec_module(_shader_save_client_protocol)
    _shader_flow_graph_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_flow_graph",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "project_shader_flow_graph.py"))
    if (_shader_flow_graph_spec is None or
            _shader_flow_graph_spec.loader is None):
        raise ImportError("Unable to load the ReplayLab Shader flow graph")
    _shader_flow_graph = importlib.util.module_from_spec(
        _shader_flow_graph_spec)
    _shader_flow_graph_spec.loader.exec_module(_shader_flow_graph)
    _hierarchy_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_flow_hierarchy",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "project_shader_flow_hierarchy.py"))
    _shader_flow_hierarchy = importlib.util.module_from_spec(_hierarchy_spec)
    _hierarchy_spec.loader.exec_module(_shader_flow_hierarchy)


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - contract tests use the non-Qt surface
    QtCore = None
    QtGui = None
    QtWidgets = None
    HAS_QT = False


POLL_INTERVAL_MS = 2000
FILESYSTEM_COALESCE_MS = 40
SAVE_RESULT_POLL_MS = 30
# Live compile-check fires only after editing pauses for this long, so a
# continuous typist triggers at most one background compile per pause.
LIVE_CHECK_DEBOUNCE_MS = 600
# The instant local analysis (no compiler) runs shortly after each pause.
LOCAL_ANALYSIS_DEBOUNCE_MS = 150
SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10.0
MAX_OPEN_DOCUMENTS = 32
SETTINGS_KEY = "ReplayLab/ShaderWorkspace/state-v1"
TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")

STAGE_ABBREVIATIONS = {
    "vertex": "VS",
    "hull": "HS",
    "tess_control": "HS",
    "domain": "DS",
    "tess_eval": "DS",
    "geometry": "GS",
    "fragment": "PS",
    "pixel": "PS",
    "compute": "CS",
    "task": "TS",
    "amplification": "AS",
    "mesh": "MS",
}


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))


def _format_message(translated, *arguments):
    values = tuple(str(value) for value in arguments)

    def _replace(match):
        index = int(match.group(1))
        return values[index - 1] if 0 < index <= len(values) else match.group(0)

    return re.sub(r"%([1-9][0-9]*)", _replace, str(translated))


def _latest_event(events, names):
    selected = None
    accepted = set(names)
    for event in events or []:
        if isinstance(event, dict) and event.get("type") in accepted:
            if selected is None or int(event.get("sequence", 0)) > int(
                    selected.get("sequence", 0)):
                selected = event
    return selected


def _classify_commit_unknown(base_hash, submitted_hash, disk_hash):
    """Classify an observed disk hash after an uncertain Host save."""

    if disk_hash == submitted_hash:
        return "committed"
    if disk_hash == base_hash:
        return "pending"
    return "external"


def _document_read_identity(document):
    """UI-owned CAS identity, without copying or reading the native buffer."""
    if document is None:
        return None
    return (document.get("documentToken"), document.get("textRevision", 0),
            document.get("rawSha256"), document.get("sourcePath"),
            bool(document.get("saveInFlight")), bool(document.get("commitUnknown")),
            (document.get("pendingSave") or {}).get("saveToken"))


def _paths_signature(paths):
    values = []
    for path in paths:
        try:
            info = os.stat(path)
            values.append((path, getattr(info, "st_mtime_ns", int(
                info.st_mtime * 1000000000)), int(info.st_size), int(info.st_mode)))
        except OSError:
            values.append((path, -1, -1, -1))
    return tuple(values)


# Which capture events (EIDs) bind each workspace's shader. Resolved on the
# file worker from the workspace manifest's catalogPath -> catalog "uses"
# (each use = eventId + the ShaderKey fields), grouped by resourceId, which is
# unique per capture. The catalog can be large and rarely changes, so it is
# cached by (path, mtime, size) across the 2 s snapshot polls.
_EVENT_ID_CACHE = {}
_EVENT_ID_CACHE_LOCK = threading.Lock()
_MAX_EVENT_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_EVENT_CATALOG_BYTES = 64 * 1024 * 1024
_MAX_EVENT_IDS_PER_SHADER = 4096


def _read_bounded_json(path, maximum):
    """Read one JSON object with a size cap; any problem yields None."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size <= 0 or size > maximum:
            return None
        with open(path, "rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            return None
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _catalog_event_ids_by_resource(catalog_path):
    """Map shader resourceId -> sorted event IDs for one capture catalog (cached)."""
    try:
        info = os.stat(catalog_path)
        stamp = (int(getattr(info, "st_mtime_ns", 0) or 0), int(info.st_size))
    except OSError:
        return {}
    with _EVENT_ID_CACHE_LOCK:
        cached = _EVENT_ID_CACHE.get(catalog_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    catalog = _read_bounded_json(catalog_path, _MAX_EVENT_CATALOG_BYTES)
    by_resource = {}
    uses = catalog.get("uses") if catalog else None
    if isinstance(uses, list):
        for use in uses:
            if not isinstance(use, dict):
                continue
            resource = use.get("resourceId", use.get("shaderResourceId"))
            event = use.get("eventId")
            if (isinstance(resource, bool) or not isinstance(resource, int) or
                    isinstance(event, bool) or not isinstance(event, int) or
                    resource <= 0 or event <= 0):
                continue
            bucket = by_resource.setdefault(resource, set())
            if len(bucket) < _MAX_EVENT_IDS_PER_SHADER:
                bucket.add(event)
    result = dict((key, sorted(value)) for key, value in by_resource.items())
    with _EVENT_ID_CACHE_LOCK:
        _EVENT_ID_CACHE[catalog_path] = (stamp, result)
        # Bound the cache to the catalogs still referenced recently.
        if len(_EVENT_ID_CACHE) > 32:
            for stale in list(_EVENT_ID_CACHE)[:-32]:
                _EVENT_ID_CACHE.pop(stale, None)
    return result


def _workspace_event_ids(workspaces):
    """Return {workspaceId: [eventId, ...]} for the eligible workspaces.

    Never raises: a workspace whose manifest or catalog cannot be read simply
    has no event list (the tree then shows none rather than a wrong one).
    """
    result = {}
    for workspace in workspaces:
        workspace_id = str(workspace.get("workspaceId", "") or "")
        workspace_path = str(workspace.get("workspacePath", "") or "")
        if not workspace_id or not workspace_path:
            continue
        manifest = _read_bounded_json(
            os.path.join(workspace_path, "manifest.json"), _MAX_EVENT_MANIFEST_BYTES)
        if not manifest:
            continue
        capture = manifest.get("capture")
        shader = manifest.get("shader")
        catalog_path = capture.get("catalogPath") if isinstance(capture, dict) else None
        resource = shader.get("resourceId") if isinstance(shader, dict) else None
        if resource is None:
            resource = workspace.get("resourceId")
        if (not isinstance(catalog_path, str) or not catalog_path or
                isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0):
            continue
        events = _catalog_event_ids_by_resource(catalog_path).get(resource)
        if events:
            result[workspace_id] = list(events)
    return result


def _build_workspace_snapshot(request):
    """Filesystem/protocol work only. Never capture a panel, editor or Qt object."""
    try:
        with _shader_console_protocol.cancellable_console_reads(request["stop"]):
            return _build_workspace_snapshot_files(request)
    except _shader_console_protocol.ShaderConsoleReadCancelled:
        return {"cancelled": True}


def _build_workspace_snapshot_files(request):
    """Build one cancellable batch on the executor's file worker."""
    stop = request["stop"]
    if stop.is_set():
        return {"cancelled": True}
    signature = _paths_signature(request["watchedPaths"])
    if not request["force"] and signature == request["previousSignature"]:
        return {"unchanged": True}
    snapshot = _shader_console_protocol.load_console_snapshot(
        request["registryPath"], capture_binding=request["captureBinding"])
    if stop.is_set():
        return {"cancelled": True}
    eligible = [item for item in snapshot.get("workspaces", [])
                if item.get("editorEligible") is True]
    requested_ids = request["documentIds"]
    selected = sorted(eligible, key=lambda item:
                      str(item["workspaceId"]) not in requested_ids)
    sources = {}
    errors = {}
    for workspace in selected[:MAX_OPEN_DOCUMENTS]:
        if stop.is_set():
            return {"cancelled": True}
        key = str(workspace["workspaceId"])
        try:
            opened = _shader_console_protocol.load_authorized_source_document(
                request["registryPath"], workspace.get("authorizationId", ""), key,
                capture_binding=request["captureBinding"])
            if (opened.get("captureBinding") != request["captureBinding"] or
                    opened.get("workspaceId") != key or
                    opened.get("authorizationId") != workspace.get("authorizationId") or
                    opened.get("sourcePath") != workspace.get("sourcePath") or
                    opened.get("liveEditId") != (workspace.get("liveEdit") or {}).get("liveEditId") or
                    opened.get("watchId") != (workspace.get("watch") or {}).get("watchId")):
                raise ValueError("Shader workspace source session changed during refresh")
            sources[key] = opened
        except Exception as exc:
            errors[key] = str(exc)[:2048]
    paths = list(dict.fromkeys(path for path in (
        [request["registryPath"], request["projectSelectionPath"]] +
        list(snapshot.get("watchedPaths", []))) if path))
    # Keep the signature taken before the source reads. Changes during a slow
    # read force the next poll to revalidate rather than becoming a new baseline.
    after = _paths_signature(paths)
    stale_sources = []
    by_path = dict((item[0], item) for item in after)
    for key, opened in list(sources.items()):
        info = by_path.get(opened["sourcePath"])
        if info is None:
            info = _paths_signature([opened["sourcePath"]])[0]
        if info[1:3] != (opened.get("modifiedNs"), opened.get("size")):
            stale_sources.append(key)
            del sources[key]
    watched = [path for path in paths if os.path.isfile(path) or os.path.isdir(path)]
    try:
        event_ids = _workspace_event_ids(eligible)
    except Exception:  # never let the EID decoration break the snapshot
        event_ids = {}
    return {"snapshot": snapshot, "sources": sources, "sourceErrors": errors,
            "eventIds": event_ids,
            "watchedPaths": paths, "existingPaths": watched,
            "signature": after if not errors and not stale_sources and
            signature == _paths_signature(request["watchedPaths"])
            else None, "staleSources": stale_sources}


def _new_snapshot_executor():
    try:
        from .project_launcher import AsyncProjectExecutor
    except (ImportError, ValueError):
        spec = importlib.util.spec_from_file_location(
            "_replaylab_shader_workspace_project_executor", os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "project_launcher.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        AsyncProjectExecutor = module.AsyncProjectExecutor
    return AsyncProjectExecutor(1)


def _save_worker_loop(save_client, task_queue, result_queue, stop_event):
    """Run complete Host save calls without ever touching a Qt object."""

    while True:
        task = task_queue.get()
        if task is None:
            return
        if stop_event.is_set():
            continue
        pending_request = getattr(save_client, "request_pending", None)
        while callable(pending_request):
            try:
                if not pending_request():
                    break
            except Exception:
                break
            if stop_event.wait(0.05):
                return
        outcome = {"task": task}
        try:
            outcome["result"] = save_client.save(
                task["liveEditId"], task["authorizationId"],
                task["workspaceId"], task["baseHash"], task["sourceText"])
            outcome["kind"] = "saved"
        except Exception as exc:
            if isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveConflict):
                outcome["kind"] = "conflict"
            elif isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveUncertain):
                outcome["kind"] = "unknown"
            else:
                outcome["kind"] = "error"
            outcome["error"] = str(exc)
        result_queue.put(outcome)


# ---------------------------------------------------------------------------
# Instant local HLSL analysis (IDE-style, no compiler round trip)
#
# Modern shader IDEs surface most mistakes immediately from a local pass over
# the buffer — a symbol/reference check — and only use the compiler as the
# slower ground truth. This pass targets the mistakes editing produces most
# often in organized decompiled HLSL (self-contained, macro-free): using an
# identifier that is declared nowhere in the file (a deleted declaration, a
# typo). It is deliberately scope-insensitive: a name declared anywhere counts
# as declared, which trades a few missed cases for essentially no false
# positives; the compile-check still catches the rest.
# ---------------------------------------------------------------------------
_HLSL_KEYWORDS = frozenset("""
if else for while do switch case default break continue return discard true false
in out inout const static uniform precise nointerpolation linear centroid noperspective
sample groupshared shared extern volatile row_major column_major snorm unorm
globallycoherent export inline struct class interface cbuffer tbuffer typedef enum
namespace register packoffset void this template operator sizeof typename
""".split())
_HLSL_TYPE_ROOTS = ("float", "int", "uint", "bool", "half", "double", "min16float",
                    "min10float", "min16int", "min12int", "min16uint", "int64_t",
                    "uint64_t", "int16_t", "uint16_t", "float16_t", "float32_t",
                    "float64_t", "dword")
_HLSL_OBJECT_TYPES = frozenset("""
matrix vector string Buffer RWBuffer StructuredBuffer RWStructuredBuffer
AppendStructuredBuffer ConsumeStructuredBuffer ByteAddressBuffer RWByteAddressBuffer
ConstantBuffer TextureBuffer Texture1D Texture1DArray Texture2D Texture2DArray
Texture2DMS Texture2DMSArray Texture3D TextureCube TextureCubeArray RWTexture1D
RWTexture1DArray RWTexture2D RWTexture2DArray RWTexture3D RasterizerOrderedBuffer
RasterizerOrderedTexture2D SamplerState SamplerComparisonState sampler sampler1D
sampler2D sampler3D samplerCUBE InputPatch OutputPatch TriangleStream LineStream
PointStream RaytracingAccelerationStructure RayDesc FeedbackTexture2D
""".split())
_HLSL_INTRINSICS = frozenset("""
abs acos all any asdouble asfloat asfloat16 asint asint16 asuint asuint16 atan atan2
ceil clamp clip cos cosh countbits cross D3DCOLORtoUBYTE4 ddx ddx_coarse ddx_fine ddy
ddy_coarse ddy_fine degrees determinant distance dot dot2add dot4add_i8packed
dot4add_u8packed dst exp exp2 f16tof32 f32tof16 faceforward firstbithigh firstbitlow
floor fma fmod frac frexp fwidth isfinite isinf isnan ldexp length lerp lit log log10
log2 mad max min modf msad4 mul noise normalize pow radians rcp reflect refract
reversebits round rsqrt saturate sign sin sincos sinh smoothstep sqrt step tan tanh
transpose trunc tex1D tex2D tex3D texCUBE tex2Dlod tex2Dbias tex2Dproj tex2Dgrad
GroupMemoryBarrier GroupMemoryBarrierWithGroupSync DeviceMemoryBarrier
DeviceMemoryBarrierWithGroupSync AllMemoryBarrier AllMemoryBarrierWithGroupSync
InterlockedAdd InterlockedAnd InterlockedOr InterlockedXor InterlockedMin InterlockedMax
InterlockedExchange InterlockedCompareExchange InterlockedCompareStore
WaveActiveSum WaveActiveAllTrue WaveActiveAnyTrue WaveActiveAllEqual WaveGetLaneIndex
WaveGetLaneCount WaveIsFirstLane WaveReadLaneAt WaveReadLaneFirst WaveActiveBallot
WaveActiveCountBits WaveActiveMin WaveActiveMax WaveActiveProduct WaveActiveBitAnd
WaveActiveBitOr WaveActiveBitXor WavePrefixSum WavePrefixProduct WavePrefixCountBits
WaveMatch WaveMultiPrefixSum QuadReadAcrossX QuadReadAcrossY QuadReadAcrossDiagonal
QuadReadLaneAt QuadAny QuadAll EvaluateAttributeAtSample EvaluateAttributeCentroid
EvaluateAttributeSnapped GetRenderTargetSampleCount GetRenderTargetSamplePosition
CheckAccessFullyMapped abort errorf printf NonUniformResourceIndex pack_s8 pack_u8
pack_clamp_s8 pack_clamp_u8 unpack_s8s16 unpack_s8s32 unpack_u8u16 unpack_u8u32
IsHelperLane select and or not TraceRay ReportHit IgnoreHit AcceptHitAndEndSearch
CallShader DispatchMesh SetMeshOutputCounts GetAttributeAtVertex
""".split())
_HLSL_DECL_MODIFIERS = frozenset("""
const static in out inout uniform precise nointerpolation linear centroid noperspective
sample groupshared shared extern volatile row_major column_major snorm unorm
globallycoherent export inline unsigned signed
""".split())
# Not preceded by an identifier/number character or "." — this drops number
# suffixes (0.5f, 1e5f, 0x1F) and member accesses (.xyz, .Sample) at once.
_HLSL_IDENT = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_]*")
_HLSL_TYPE_SHAPE = re.compile(r"^(?:%s)(?:[1-4](?:x[1-4])?)?$" % "|".join(_HLSL_TYPE_ROOTS))
_HLSL_MAX_LOCAL_DIAGNOSTICS = 60


def _hlsl_blank_noncode(text):
    """Replace comments, string/char literals and preprocessor lines with spaces.

    Lengths and newlines are preserved so every offset still maps to the
    original buffer. Returns (blanked_text, defined_macro_names).
    """
    out = list(text)
    n = len(text)
    defines = set()
    i = 0
    line_start = True
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = i
            while j < n and text[j] != "\n":
                out[j] = " "; j += 1
            i = j; continue
        if c == "/" and nxt == "*":
            j = i + 2
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                if text[j] != "\n":
                    out[j] = " "
                j += 1
            end = min(n, j + 2)
            for k in range(i, end):
                if text[k] != "\n":
                    out[k] = " "
            i = end; line_start = False; continue
        if c in "\"'":
            quote = c; j = i + 1
            while j < n and text[j] != quote and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            end = min(n, j + 1)
            for k in range(i, end):
                if text[k] != "\n":
                    out[k] = " "
            i = end; line_start = False; continue
        if c == "#" and line_start:
            j = i
            # A preprocessor line may continue with a trailing backslash.
            while j < n:
                if text[j] == "\n" and not (j > 0 and text[j - 1] == "\\"):
                    break
                j += 1
            directive = text[i:j]
            m = re.match(r"#\s*define\s+([A-Za-z_]\w*)", directive)
            if m:
                defines.add(m.group(1))
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j; continue
        if c == "\n":
            line_start = True
        elif not c.isspace():
            line_start = False
        i += 1
    return "".join(out), defines


def _hlsl_is_type_word(word, user_types):
    return (word in user_types or word in _HLSL_OBJECT_TYPES or
            _HLSL_TYPE_SHAPE.match(word) is not None or word == "void")


def hlsl_local_diagnostics(text):
    """Return instant diagnostics for identifiers used but declared nowhere."""
    if not isinstance(text, str) or not text.strip():
        return []
    code, defines = _hlsl_blank_noncode(text)
    tokens = [(m.group(0), m.start(), m.end()) for m in _HLSL_IDENT.finditer(code)]
    if not tokens:
        return []

    def prev_sig(pos):
        """Previous non-space character before offset pos, and its index."""
        k = pos - 1
        while k >= 0 and code[k].isspace():
            k -= 1
        return (code[k] if k >= 0 else ""), k

    def next_sig(pos):
        k = pos
        while k < len(code) and code[k].isspace():
            k += 1
        return (code[k] if k < len(code) else ""), k

    # Pass 1 — user type names: struct/class/interface/cbuffer X, typedef ... X;
    user_types = set()
    for index, (word, start, end) in enumerate(tokens):
        if word in ("struct", "class", "interface", "cbuffer", "tbuffer") and index + 1 < len(tokens):
            nxt = tokens[index + 1]
            if nxt[1] > end and next_sig(end)[1] == nxt[1]:
                user_types.add(nxt[0])
        elif word == "typedef":
            # typedef <type...> NAME ;  -> the identifier right before ';'
            semi = code.find(";", end)
            if semi > 0:
                seg = code[end:semi]
                names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", seg)
                if names:
                    user_types.add(names[-1])

    # Pass 2 — declared names: "<type> [modifiers] NAME" (+ "type<...> NAME"),
    # comma-continued declarations at the same nesting depth, and macros.
    declared = set(defines)
    depth_at = []  # paren depth per token
    depth = 0
    pos = 0
    for word, start, end in tokens:
        depth += code.count("(", pos, start) - code.count(")", pos, start)
        depth_at.append(depth)
        pos = start
    decl_depth = None  # paren depth of the active declaration statement, if any
    for index, (word, start, end) in enumerate(tokens):
        before, before_idx = prev_sig(start)
        after, after_idx = next_sig(end)
        # A declaration statement ends at ';' or '{' or when leaving its depth.
        if decl_depth is not None:
            between = code[tokens[index - 1][2]:start] if index else ""
            if ";" in between or "{" in between or "}" in between or depth_at[index] < decl_depth:
                decl_depth = None
        if word in _HLSL_DECL_MODIFIERS or word in ("register", "packoffset"):
            continue
        if _hlsl_is_type_word(word, user_types):
            # Find the declared name: skip template args <...> and modifiers.
            j = index + 1
            scan_pos = end
            nxt_char, nxt_idx = next_sig(end)
            if nxt_char == "<":
                close = code.find(">", nxt_idx)
                if close < 0:
                    continue
                scan_pos = close + 1
                while j < len(tokens) and tokens[j][1] < scan_pos:
                    j += 1
            while j < len(tokens) and tokens[j][0] in _HLSL_DECL_MODIFIERS:
                j += 1
            if j < len(tokens):
                name, nstart, nend = tokens[j]
                gap = code[scan_pos:nstart]
                if gap.strip() == "" and not _hlsl_is_type_word(name, user_types) and name not in _HLSL_KEYWORDS:
                    # "float4 foo" / "float4 Foo(" / "Texture2D<float4> tex"
                    # but not "float4(x)" (constructor) — the gap must be pure space
                    declared.add(name)
                    decl_depth = depth_at[j]
            continue
        if decl_depth is not None and before == "," and depth_at[index] == decl_depth:
            # "float a, b, c;" / "int x = 1, y = 2;"
            if after in ("=", ",", ";", "[", ":"):
                declared.add(word)
            continue

    # Pass 3 — uses that are declared nowhere.
    diagnostics = []
    reported = set()
    line_starts = [0]
    for m in re.finditer("\n", code):
        line_starts.append(m.end())
    import bisect
    for index, (word, start, end) in enumerate(tokens):
        if (word in declared or word in _HLSL_KEYWORDS or word in _HLSL_INTRINSICS or
                word in _HLSL_DECL_MODIFIERS or _hlsl_is_type_word(word, user_types)):
            continue
        before, before_idx = prev_sig(start)
        # Member access / method call / scope: .xyz  .Sample(  A::b
        if before == "." or (before == ":" and before_idx > 0 and code[before_idx - 1] == ":"):
            continue
        # Semantic after ':' (param/return/field semantics) up to , ) ; { =
        if before == ":":
            continue
        # Inside register(...) / packoffset(...)
        open_paren = code.rfind("(", 0, start)
        if open_paren >= 0 and code.find(")", open_paren, start) < 0:
            head = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code[max(0, open_paren - 24):open_paren])
            if head and head[-1] in ("register", "packoffset"):
                continue
        # Inside a statement-level [attribute]
        open_br = code.rfind("[", 0, start)
        if open_br >= 0 and code.find("]", open_br, start) < 0:
            b2, _ = prev_sig(open_br)
            if b2 in ("", ";", "{", "}", "]"):
                continue
        # Labels "case FOO:" are rare in HLSL; treat identifiers followed by ':'
        # at statement start as semantics-like and skip.
        if before in ("", ";", "{", "}") and next_sig(end)[0] == ":":
            continue
        line = bisect.bisect_right(line_starts, start)  # 1-based
        column = start - line_starts[line - 1] + 1
        key = (word, line)
        if key in reported:
            continue
        reported.add(key)
        diagnostics.append({
            "line": line, "column": column, "length": end - start,
            "severity": "error",
            "text": "Undefined identifier '%s'" % word,
            "source": "local",
        })
        if len(diagnostics) >= _HLSL_MAX_LOCAL_DIAGNOSTICS:
            break
    return diagnostics


def _analysis_worker_loop(task_queue, result_queue, stop_event):
    """Run the instant local HLSL analysis off the UI thread (latest wins)."""

    while True:
        task = task_queue.get()
        if task is None:
            return
        # Coalesce: only the newest buffer matters while the user types.
        while True:
            try:
                newer = task_queue.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return
            task = newer
        if stop_event.is_set():
            continue
        try:
            diagnostics = hlsl_local_diagnostics(task.get("sourceText", ""))
        except Exception:
            diagnostics = []
        result_queue.put({"task": task, "diagnostics": diagnostics})


def _clamp_diagnostics_to_text(diagnostics, text):
    """Pull end-of-line diagnostics back onto the last token of their line.

    Compilers report "expected ';'" just past the last token, i.e. on the line
    break, where an underline has no glyph to sit on. Like other IDEs, anchor
    such a diagnostic to the last token so the squiggle is visible. Uses the
    exact text that was compiled, so columns map 1:1.
    """
    if not diagnostics or not isinstance(text, str):
        return diagnostics
    lines = text.split("\n")
    result = []
    for item in diagnostics:
        entry = dict(item)
        try:
            line = int(entry.get("line", 0) or 0)
            column = int(entry.get("column", 0) or 0)
        except (TypeError, ValueError):
            result.append(entry)
            continue
        if 1 <= line <= len(lines):
            content = lines[line - 1].rstrip("\r")
            stripped = content.rstrip()
            if stripped and column > len(stripped):
                end = len(stripped)
                start = end
                while start > 0 and end - start < 40 and (stripped[start - 1].isalnum() or stripped[start - 1] in "_)](."):
                    start -= 1
                if start == end:
                    start = end - 1
                entry["column"] = start + 1
                entry["length"] = max(1, end - start)
        result.append(entry)
    return result


def _check_worker_loop(save_client, task_queue, result_queue, stop_event):
    """Run Host compile-checks (unsaved-buffer diagnostics) off the UI thread.

    A check never writes, publishes, or applies, so a failure is only ever
    reported as an error outcome and never fabricates editor squiggles.
    """

    while True:
        task = task_queue.get()
        if task is None:
            return
        if stop_event.is_set():
            continue
        pending_request = getattr(save_client, "request_pending", None)
        while callable(pending_request):
            try:
                if not pending_request():
                    break
            except Exception:
                break
            if stop_event.wait(0.05):
                return
        outcome = {"task": task}
        try:
            outcome["result"] = save_client.check(
                task["liveEditId"], task["authorizationId"],
                task["workspaceId"], task["sourceText"])
            outcome["kind"] = "checked"
        except Exception as exc:
            outcome["kind"] = "error"
            outcome["error"] = str(exc)
        result_queue.put(outcome)


def _join_save_worker(worker, timeout_seconds, request_pending):
    """Bound shutdown latency and prove that the Python worker has stopped."""

    if worker is None:
        return True
    if worker is threading.current_thread():
        raise RuntimeError(
            "ReplayLab Shader Workspace save worker cannot join itself")
    timeout = max(0.0, float(timeout_seconds))
    worker.join(timeout)
    if not worker.is_alive():
        return True
    try:
        transport_pending = bool(request_pending())
    except Exception:
        transport_pending = True
    # i18n-ignore: developer-only shutdown diagnostic printed by the bridge.
    raise RuntimeError(
        "ReplayLab Shader Workspace save worker did not stop within %.3f seconds "
        "(Host request pending: %s)" %
        (timeout, "yes" if transport_pending else "no"))


if HAS_QT:
    class _WorkspaceRoot(QtWidgets.QWidget):
        def __init__(self, owner):
            super(_WorkspaceRoot, self).__init__()
            self.owner = owner

        @QtCore.Slot(result=bool)
        def checkAllowClose(self):
            # ToolWindowManager queries this exact Qt meta-method before it
            # removes and directly deletes a dock tab; that path does not send
            # QWidget.closeEvent.
            return self.owner is None or self.owner._confirm_panel_close()

        def closeEvent(self, event):
            if self.checkAllowClose():
                event.accept()
                super(_WorkspaceRoot, self).closeEvent(event)
            else:
                event.ignore()


    def _pin_icon(palette, pinned):
        pixmap = QtGui.QPixmap(16, 16)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.translate(8.0, 7.5)
        color = palette.color(QtGui.QPalette.ButtonText)
        color.setAlpha(235 if pinned else 175)
        painter.setPen(QtGui.QPen(
            color, 1.8 if pinned else 1.5, QtCore.Qt.SolidLine,
            QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        if pinned:
            fill = QtGui.QColor(color)
            fill.setAlpha(55)
            painter.setBrush(fill)
        else:
            painter.setBrush(QtCore.Qt.NoBrush)
        body = QtGui.QPainterPath()
        body.moveTo(-4.5, -5.0)
        body.lineTo(4.5, -5.0)
        body.lineTo(2.7, -1.7)
        body.lineTo(2.1, 1.0)
        body.lineTo(-2.1, 1.0)
        body.lineTo(-2.7, -1.7)
        body.closeSubpath()
        painter.drawPath(body)
        painter.drawLine(QtCore.QPointF(0.0, 1.0), QtCore.QPointF(0.0, 5.5))
        painter.drawPoint(QtCore.QPointF(0.0, 6.0))
        painter.end()
        return QtGui.QIcon(pixmap)


    class _DocumentTabBar(QtWidgets.QTabBar):
        pinRequested = QtCore.Signal(str)
        closeDocumentRequested = QtCore.Signal(str)
        closeOthersRequested = QtCore.Signal(str)

        def __init__(self, parent=None):
            super(_DocumentTabBar, self).__init__(parent)
            self.setDocumentMode(True)
            self.setMovable(True)
            self.setTabsClosable(True)
            self.setElideMode(QtCore.Qt.ElideMiddle)
            self.setExpanding(False)
            self.tabCloseRequested.connect(self._close_index)
            self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._context_menu)

        def _close_index(self, index):
            workspace_id = str(self.tabData(index) or "")
            if workspace_id:
                self.closeDocumentRequested.emit(workspace_id)

        def configure_pin_button(self, index, workspace_id, pinned):
            button = QtWidgets.QToolButton(self)
            button.setAutoRaise(True)
            button.setFixedSize(20, 20)
            button.setIcon(_pin_icon(self.palette(), pinned))
            if pinned:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Unpin tab"))
            else:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Pin tab"))
            button.clicked.connect(
                lambda _checked=False, value=workspace_id: self.pinRequested.emit(value))
            self.setTabButton(index, QtWidgets.QTabBar.LeftSide, button)

        def _context_menu(self, point):
            index = self.tabAt(point)
            if index < 0:
                return
            workspace_id = str(self.tabData(index) or "")
            menu = QtWidgets.QMenu(self)
            close_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close tab"))
            others_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close other tabs"))
            menu.addSeparator()
            pin_button = self.tabButton(index, QtWidgets.QTabBar.LeftSide)
            pin_action = menu.addAction(
                pin_button.toolTip() if pin_button is not None else
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Pin tab"))
            selected = menu.exec_(self.mapToGlobal(point))
            if selected is close_action:
                self.closeDocumentRequested.emit(workspace_id)
            elif selected is others_action:
                self.closeOthersRequested.emit(workspace_id)
            elif selected is pin_action:
                self.pinRequested.emit(workspace_id)

        def mouseReleaseEvent(self, event):
            if event.button() == QtCore.Qt.MiddleButton:
                index = self.tabAt(event.pos())
                if index >= 0:
                    self._close_index(index)
                    event.accept()
                    return
            super(_DocumentTabBar, self).mouseReleaseEvent(event)


    class ReplayLabShaderWorkspacePanel(object):
        """Docked tree, multi-document HLSL editor and compile/apply status."""

        def __init__(self, ctx, qrd, closed_callback=None, registry_path=None,
                     save_client=None, project_selection_path=None,
                     capture_binding_provider=None, async_executor=None):
            self.ctx = ctx
            self._capture_binding_provider = capture_binding_provider
            self._capture_binding = None
            self.qrd = qrd
            self.closed_callback = closed_callback
            self.registry_path = str(registry_path or "")
            self.project_selection_path = str(project_selection_path or "")
            self._save_client = (
                save_client or
                _shader_save_client_protocol.ReplayLabShaderSaveClient())
            self._save_tasks = queue.Queue()
            self._save_results = queue.Queue()
            self._save_stop = threading.Event()
            self._save_worker = None
            self._save_worker_stopped = False
            self._check_tasks = queue.Queue()
            self._check_results = queue.Queue()
            self._check_stop = threading.Event()
            self._check_worker = None
            self._check_worker_stopped = False
            self._next_check_token = 0
            # Latest live compile-check per workspace, keyed to the editor
            # text revision it describes so a newer edit never keeps a stale
            # squiggle pinned to code that has since moved.
            self._live_check = {}
            self._live_check_logged = set()
            self._analysis_tasks = queue.Queue()
            self._analysis_results = queue.Queue()
            self._analysis_stop = threading.Event()
            self._analysis_worker = None
            # {workspaceId: {textRevision, diagnostics}} from the instant local pass.
            self._local_analysis = {}
            # {workspaceId: [eventId, ...]} — which capture events bind each shader.
            self._workspace_event_ids = {}
            # Live frame-wide usage from RenderDoc (GetUsage) for the OPEN capture:
            # authoritative and complete (a replaced shader keeps its original id
            # in the frame record), whereas a one-event catalog only knows one EID.
            self._usage_results = queue.Queue()
            self._usage_cache = {}
            self._usage_pending = set()
            self._next_document_token = 0
            self._next_save_token = 0
            self._alive = True
            self._docked = False
            self._shutting_down = False
            self._native_editors_released = False
            self._snapshot = None
            self._snapshot_executor = async_executor or _new_snapshot_executor()
            self._owns_snapshot_executor = async_executor is None
            self._snapshot_owner = "shader-workspace-" + uuid.uuid4().hex
            self._snapshot_generation = 0
            self._snapshot_running = False
            self._snapshot_again = False
            self._snapshot_stop = threading.Event()
            self._source_documents = {}
            self._source_request_states = {}
            self._pending_open = {}
            self._pending_reload = {}
            self._workspaces = {}
            self._documents = {}
            self._tree_items = {}
            self._watched_paths = [path for path in (
                self.registry_path, self.project_selection_path) if path]
            self._last_disk_signature = None
            self._loading_documents = set()
            self._reordering_tabs = False
            self._restored = False
            self._tree_signature = None
            self._flow_graph_signature = None
            self._settings = QtCore.QSettings()
            state = self._load_state()
            self._pinned = list(state.get("pinned", []))
            self._restore_open = list(state.get("open", []))
            self._restore_active = str(state.get("active", "") or "")
            # Keep the source navigator out of the way until it is requested;
            # the code canvas and per-Shader flow graph are the primary views.
            # Existing state files predate these fields and therefore receive
            # the requested first-run defaults (files hidden, graph visible).
            self._files_visible = state.get("filesVisible") is True
            self._flow_visible = state.get("flowVisible") is not False

            self.top = _WorkspaceRoot(self)
            self.top.setObjectName("replaylabShaderWorkspace")
            self.top.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            self.top.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"))
            self.top.destroyed.connect(self._destroyed)

            outer = QtWidgets.QVBoxLayout(self.top)
            outer.setContentsMargins(4, 4, 4, 4)
            header = QtWidgets.QHBoxLayout()
            self.files_button = QtWidgets.QToolButton(self.top)
            self.files_button.setObjectName(
                "replaylabShaderWorkspaceFilesButton")
            self.files_button.setCheckable(True)
            self.files_button.setChecked(self._files_visible)
            self.files_button.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextBesideIcon)
            self.files_button.setIcon(self.top.style().standardIcon(
                QtWidgets.QStyle.SP_DirIcon))
            self.files_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader files"))
            self.files_button.clicked[bool].connect(self._toggle_file_tree)
            header.addWidget(self.files_button)
            self.flow_button = QtWidgets.QToolButton(self.top)
            self.flow_button.setObjectName(
                "replaylabShaderWorkspaceFlowButton")
            self.flow_button.setCheckable(True)
            self.flow_button.setChecked(self._flow_visible)
            self.flow_button.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextBesideIcon)
            self.flow_button.setIcon(self.top.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogDetailedView))
            self.flow_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Flow graph"))
            self.flow_button.clicked[bool].connect(self._toggle_flow_graph)
            header.addWidget(self.flow_button)
            title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"), self.top)
            title_font = title.font()
            title_font.setBold(True)
            title.setFont(title_font)
            header.addWidget(title)
            self.subtitle = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Reviewed organized HLSL · Ctrl+S saves and automatically compiles/replaces"),
                self.top)
            # ``mid`` becomes nearly indistinguishable from the window color
            # in several dark themes. Use the theme's readable text role for
            # this small explanatory line.
            self.subtitle.setStyleSheet("color: palette(text);")
            header.addWidget(self.subtitle)
            header.addStretch(1)
            self.save_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save"), self.top)
            self.save_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save the current Shader (Ctrl+S)"))
            self.save_button.clicked.connect(self._save_current)
            header.addWidget(self.save_button)
            self.save_all_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Save All"), self.top)
            self.save_all_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save all modified Shader tabs"))
            self.save_all_button.clicked.connect(self._save_all)
            header.addWidget(self.save_all_button)
            self.refresh_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Refresh"), self.top)
            self.refresh_button.clicked.connect(lambda _checked=False: self.refresh(True))
            header.addWidget(self.refresh_button)
            outer.addLayout(header)

            self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self.top)
            self.file_tree = QtWidgets.QTreeWidget(self.main_splitter)
            self.file_tree.setObjectName("replaylabShaderWorkspaceTree")
            self.file_tree.setHeaderLabels((QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader files"),))
            self.file_tree.setMinimumWidth(210)
            self.file_tree.setUniformRowHeights(True)
            self.file_tree.itemClicked.connect(self._tree_item_clicked)
            self.file_tree.itemActivated.connect(self._tree_item_clicked)
            self.file_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.file_tree.customContextMenuRequested.connect(self._tree_context_menu)
            self.main_splitter.addWidget(self.file_tree)

            right = QtWidgets.QWidget(self.main_splitter)
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            self.tab_bar = _DocumentTabBar(right)
            self.tab_bar.currentChanged.connect(self._tab_changed)
            self.tab_bar.tabMoved.connect(self._tab_moved)
            self.tab_bar.pinRequested.connect(self._toggle_pin)
            self.tab_bar.closeDocumentRequested.connect(self._close_document)
            self.tab_bar.closeOthersRequested.connect(self._close_other_documents)
            right_layout.addWidget(self.tab_bar)

            self.editor_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, right)
            self.editor_stack = QtWidgets.QStackedWidget(self.editor_splitter)
            empty = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "No editable HLSL workspaces are available. Use Analyze Shader with RenderDoc AI; a workspace can be created only when a verified editable route is available."),
                self.editor_stack)
            empty.setAlignment(QtCore.Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setMargin(24)
            self.empty_page = empty
            self.editor_stack.addWidget(empty)
            self.editor_splitter.addWidget(self.editor_stack)

            problems_box = QtWidgets.QWidget(self.editor_splitter)
            problems_layout = QtWidgets.QVBoxLayout(problems_box)
            problems_layout.setContentsMargins(0, 0, 0, 0)
            problems_title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Problems"), problems_box)
            problems_title_font = problems_title.font()
            problems_title_font.setBold(True)
            problems_title.setFont(problems_title_font)
            problems_layout.addWidget(problems_title)
            self.problems = QtWidgets.QTreeWidget(problems_box)
            self.problems.setHeaderLabels((
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Line"),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Message"),
            ))
            self.problems.setRootIsDecorated(False)
            self.problems.setAlternatingRowColors(True)
            self.problems.header().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeToContents)
            self.problems.header().setSectionResizeMode(
                1, QtWidgets.QHeaderView.Stretch)
            self.problems.itemActivated.connect(self._problem_activated)
            self.problems.itemDoubleClicked.connect(self._problem_activated)
            problems_layout.addWidget(self.problems)
            self.editor_splitter.addWidget(problems_box)
            self.editor_splitter.setStretchFactor(0, 4)
            self.editor_splitter.setStretchFactor(1, 1)
            self.editor_splitter.setSizes((680, 150))
            right_layout.addWidget(self.editor_splitter)
            self.main_splitter.addWidget(right)

            self.flow_graph = _shader_flow_graph.ShaderFlowGraphPanel(
                self.main_splitter)
            self.flow_graph.setObjectName(
                "replaylabShaderWorkspaceFlowGraph")
            self.flow_graph.setMinimumWidth(300)
            self.flow_graph.nodeActivated.connect(
                self._flow_graph_node_activated)
            self.flow_graph.subgraphRequested.connect(
                self._flow_graph_subgraph_requested)
            self.main_splitter.addWidget(self.flow_graph)
            self.main_splitter.setStretchFactor(0, 0)
            self.main_splitter.setStretchFactor(1, 3)
            self.main_splitter.setStretchFactor(2, 2)
            self.main_splitter.setCollapsible(0, True)
            self.main_splitter.setCollapsible(1, False)
            self.main_splitter.setCollapsible(2, True)
            self.main_splitter.setSizes((250, 820, 480))
            self.file_tree.setVisible(self._files_visible)
            self.flow_graph.setVisible(self._flow_visible)
            self._update_file_button_tooltip()
            self._update_flow_button_tooltip()
            outer.addWidget(self.main_splitter, 1)

            status_row = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Waiting for organized HLSL workspaces"), self.top)
            self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            status_row.addWidget(self.status_label, 1)
            self.reload_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Reload"), self.top)
            self.reload_button.setVisible(False)
            self.reload_button.clicked.connect(self._reload_current)
            status_row.addWidget(self.reload_button)
            outer.addLayout(status_row)

            self.file_watcher = QtCore.QFileSystemWatcher(self.top)
            self.file_watcher.fileChanged.connect(self._filesystem_changed)
            self.file_watcher.directoryChanged.connect(self._filesystem_changed)
            self.change_timer = QtCore.QTimer(self.top)
            self.change_timer.setSingleShot(True)
            self.change_timer.setInterval(FILESYSTEM_COALESCE_MS)
            self.change_timer.timeout.connect(lambda: self.refresh(True))
            self.poll_timer = QtCore.QTimer(self.top)
            self.poll_timer.setInterval(POLL_INTERVAL_MS)
            self.poll_timer.timeout.connect(self.refresh)
            self.poll_timer.start()
            self.save_result_timer = QtCore.QTimer(self.top)
            self.save_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.save_result_timer.timeout.connect(self._drain_save_results)
            self._save_worker = threading.Thread(
                target=_save_worker_loop,
                args=(self._save_client, self._save_tasks, self._save_results,
                      self._save_stop),
                name="ReplayLabShaderWorkspaceSave")
            self._save_worker.daemon = True
            self._save_worker.start()
            self.check_result_timer = QtCore.QTimer(self.top)
            self.check_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.check_result_timer.timeout.connect(self._drain_check_results)
            self.check_result_timer.start()
            self.check_debounce_timer = QtCore.QTimer(self.top)
            self.check_debounce_timer.setSingleShot(True)
            self.check_debounce_timer.setInterval(LIVE_CHECK_DEBOUNCE_MS)
            self.check_debounce_timer.timeout.connect(self._request_live_check)
            self._check_worker = threading.Thread(
                target=_check_worker_loop,
                args=(self._save_client, self._check_tasks, self._check_results,
                      self._check_stop),
                name="ReplayLabShaderWorkspaceCheck")
            self._check_worker.daemon = True
            self._check_worker.start()
            self.analysis_debounce_timer = QtCore.QTimer(self.top)
            self.analysis_debounce_timer.setSingleShot(True)
            self.analysis_debounce_timer.setInterval(LOCAL_ANALYSIS_DEBOUNCE_MS)
            self.analysis_debounce_timer.timeout.connect(self._request_local_analysis)
            self._analysis_worker = threading.Thread(
                target=_analysis_worker_loop,
                args=(self._analysis_tasks, self._analysis_results, self._analysis_stop),
                name="ReplayLabShaderWorkspaceAnalysis")
            self._analysis_worker.daemon = True
            self._analysis_worker.start()

            self.close_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Close, self.top)
            self.close_shortcut.activated.connect(self._close_current)
            self.save_all_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence("Ctrl+Shift+S"), self.top)
            self.save_all_shortcut.activated.connect(self._save_all)
            self.files_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence("Ctrl+B"), self.top)
            self.files_shortcut.activated.connect(self.files_button.click)
            self.refresh(True)

        def _load_state(self):
            raw = self._settings.value(SETTINGS_KEY, "")
            try:
                value = json.loads(str(raw or ""))
            except (TypeError, ValueError):
                return {}
            if not isinstance(value, dict):
                return {}
            clean = {}
            for field in ("open", "pinned"):
                values = value.get(field, [])
                clean[field] = [
                    str(item).lower() for item in values
                    if isinstance(item, str) and TOKEN_RE.fullmatch(item.lower())
                ][:MAX_OPEN_DOCUMENTS]
            active = str(value.get("active", "") or "").lower()
            clean["active"] = active if TOKEN_RE.fullmatch(active) else ""
            clean["filesVisible"] = value.get("filesVisible") is True
            clean["flowVisible"] = value.get("flowVisible") is not False
            return clean

        def _save_state(self):
            if self.tab_bar is None:
                return
            opened = [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]
            active = ""
            if self.tab_bar.currentIndex() >= 0:
                active = str(self.tab_bar.tabData(self.tab_bar.currentIndex()) or "")
            value = {
                "open": opened,
                "pinned": [item for item in self._pinned if item in self._documents],
                "active": active,
                "filesVisible": bool(self._files_visible),
                "flowVisible": bool(self._flow_visible),
            }
            self._settings.setValue(
                SETTINGS_KEY, json.dumps(value, separators=(",", ":")))

        def _update_file_button_tooltip(self):
            if self._files_visible:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Hide Shader files (Ctrl+B)")
            else:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Show Shader files (Ctrl+B)")
            self.files_button.setToolTip(tooltip)

        def _toggle_file_tree(self, visible):
            self._files_visible = bool(visible)
            self.file_tree.setVisible(self._files_visible)
            self.files_button.setChecked(self._files_visible)
            self._update_file_button_tooltip()
            if self._files_visible:
                sizes = self.main_splitter.sizes()
                if sizes and sizes[0] < 180:
                    sizes[0] = 250
                    self.main_splitter.setSizes(sizes)
            self._save_state()

        def _update_flow_button_tooltip(self):
            if self._flow_visible:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Hide flow graph")
            else:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Show flow graph")
            self.flow_button.setToolTip(tooltip)

        def _toggle_flow_graph(self, visible):
            self._flow_visible = bool(visible)
            self.flow_graph.setVisible(self._flow_visible)
            self.flow_button.setChecked(self._flow_visible)
            self._update_flow_button_tooltip()
            if self._flow_visible:
                sizes = self.main_splitter.sizes()
                if len(sizes) >= 3 and sizes[2] < 260:
                    sizes[2] = 420
                    self.main_splitter.setSizes(sizes)
            self._save_state()

        def _add_editor_selection_to_chat(
                self, text, source_path, start_line, end_line,
                workspace_id=""):
            title = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader selection")
            source_name = (
                os.path.basename(source_path) or
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader Workspace"))
            source = _format_message(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Shader Workspace — %1 (lines %2–%3)"),
                source_name, start_line, end_line)

            # i18n-ignore: The following keys and values are the stable structured-attachment
            # protocol. They are not user-visible UI strings. Paths are reduced to a leaf name;
            # the opaque workspace ID is the only Host lookup hint carried into the conversation.
            document = self._documents.get(str(workspace_id or ""))
            workspace = document.get("workspace", {}) if document else {}
            workspace_id = str(
                (document or {}).get("workspaceId", "") or
                workspace.get("workspaceId", "") or
                workspace.get("documentId", ""))
            if not TOKEN_RE.fullmatch(workspace_id):
                workspace_id = hashlib.sha256(
                    ("shader-workspace\n" + source_name).encode("utf-8")).hexdigest()
            context = {
                "schema": "replaylab-attachment-context/v1",
                "origin": {
                    "kind": "shader-workspace",
                    "panel": "shader-workspace",
                    "sourceId": workspace_id,
                    "sourceName": source_name,
                    "range": {
                        "lineStart": int(start_line),
                        "lineEnd": int(end_line),
                    },
                },
            }
            capture_id = str(workspace.get("captureId", "") or "")
            if capture_id:
                context["capture"] = {"captureSessionId": capture_id}
            shader = {
                "stage": str(workspace.get("stage", "") or ""),
                "entryPoint": str(
                    (document or {}).get("entryPoint", "") or
                    workspace.get("entryPoint", "") or ""),
                "rawSha256": str((document or {}).get("rawSha256", "") or ""),
                "workspaceFileId": workspace_id,
            }
            resource_id = workspace.get("resourceId", 0)
            if isinstance(resource_id, int) and resource_id > 0:
                shader["resourceId"] = resource_id
                context["relations"] = [{
                    "id": "workspace-" + workspace_id,
                    "kind": "workspace-copy-of",
                    "targetId": "shader:%d" % resource_id,
                    "usage": "source",
                }]
            context["shader"] = dict(
                (key, value) for key, value in shader.items()
                if value not in ("", 0, None))
            self.ctx.AddTextToAIAssistant(
                title, text, source,
                json.dumps(context, ensure_ascii=False, separators=(",", ":")))

        def _request_local_analysis(self):
            """Queue the instant local analysis of the current buffer."""
            if not self._alive or self.top is None:
                return
            document = self._current_document()
            if document is None or not document.get("available") or not document.get("dirty"):
                return
            try:
                source_text = document["editor"].GetText()
            except Exception:
                return
            self._analysis_tasks.put({
                "workspaceId": document.get("workspaceId"),
                "documentToken": document["documentToken"],
                "textRevision": int(document.get("textRevision", 0)),
                "sourceText": source_text,
            })

        def _drain_analysis_results(self):
            if not self._alive or self.top is None:
                return
            updated = False
            while True:
                try:
                    outcome = self._analysis_results.get_nowait()
                except queue.Empty:
                    break
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or document.get("documentToken") != task.get("documentToken") or
                        int(document.get("textRevision", 0)) != int(task.get("textRevision", 0))):
                    continue
                self._local_analysis[workspace_id] = {
                    "textRevision": int(task.get("textRevision", 0)),
                    "diagnostics": list(outcome.get("diagnostics") or []),
                }
                updated = True
            if updated and self._alive:
                self._refresh_current_view()

        def _request_live_check(self):
            """Queue a compile-check of the current unsaved buffer, if eligible."""
            if not self._alive or self.top is None:
                return
            document = self._current_document()
            if document is None or not document.get("available"):
                return
            if (not document.get("dirty") or document.get("conflict") or
                    document.get("saveInFlight") or document.get("commitUnknown")):
                return
            workspace_id = document.get("workspaceId")
            workspace = self._document_model(workspace_id)
            if not workspace or workspace.get("autoApplyReady") is not True:
                return
            live_edit = workspace.get("liveEdit") or {}
            live_edit_id = str(live_edit.get("liveEditId", "") or "")
            authorization_id = str(document.get("authorizationId", "") or "")
            if not live_edit_id or not authorization_id:
                return
            try:
                source_text = self._document_source_text(document)
            except Exception:
                return
            if not source_text:
                return
            self._next_check_token += 1
            self._check_tasks.put({
                "checkToken": self._next_check_token,
                "workspaceId": workspace_id,
                "documentToken": document["documentToken"],
                "liveEditId": live_edit_id,
                "authorizationId": authorization_id,
                "sourceText": source_text,
                "textRevision": int(document.get("textRevision", 0)),
            })

        def _log_live_check_once(self, message):
            """Print each distinct live-check failure once (developer console)."""
            text = str(message or "")[:512]
            if text in self._live_check_logged:
                return
            self._live_check_logged.add(text)
            # i18n-ignore: developer-only diagnostic printed by the bridge.
            print("[ReplayLab] Shader Workspace %s" % text)

        def _request_live_usage(self, workspaces):
            """Ask RenderDoc which events bind each shader of the open capture.

            Runs GetUsage on the replay thread (cheap: a frame-record lookup),
            never touching Qt there; results come back through a queue drained
            on the UI timer. Cached per (capture, resourceId) until the capture
            changes, so the 2 s snapshot poll does not re-query.
            """
            if not self._alive or self.top is None:
                return
            binding = self._current_capture_binding() or {}
            capture_key = _normal_path(str(binding.get('capturePath', '') or ''))
            if not capture_key:
                return
            wanted = []
            for workspace in workspaces or []:
                resource = workspace.get('resourceId')
                if (isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0 or
                        _normal_path(str(workspace.get('capturePath', '') or '')) != capture_key):
                    continue
                key = (capture_key, resource)
                if key in self._usage_cache or key in self._usage_pending:
                    continue
                wanted.append((key, str(workspace.get('workspaceId', '') or ''), resource))
            if not wanted:
                return
            try:
                by_int = {}
                for resource in self.ctx.GetResources():
                    rid = getattr(resource, 'resourceId', None)
                    try:
                        by_int[int(rid)] = rid
                    except Exception:
                        continue
            except Exception:
                return
            results = self._usage_results
            for key, workspace_id, resource in wanted:
                rid = by_int.get(resource)
                if rid is None:
                    continue
                self._usage_pending.add(key)

                def _query(ctrl, key=key, workspace_id=workspace_id, rid=rid):
                    events = []
                    try:
                        seen = set()
                        for use in ctrl.GetUsage(rid):
                            event = int(getattr(use, 'eventId', 0) or 0)
                            if event > 0 and event not in seen:
                                seen.add(event)
                                events.append(event)
                        events.sort()
                    except Exception:
                        events = None
                    results.put((key, workspace_id, events))

                try:
                    replay = self.ctx.Replay()
                    try:
                        replay.AsyncInvoke('replaylab-shader-usage', _query)
                    except TypeError:
                        replay.AsyncInvoke(_query)
                except Exception:
                    self._usage_pending.discard(key)

        def _drain_usage_results(self):
            if not self._alive or self.top is None:
                return
            changed = False
            while True:
                try:
                    key, workspace_id, events = self._usage_results.get_nowait()
                except queue.Empty:
                    break
                self._usage_pending.discard(key)
                if events is None:
                    continue  # query failed: keep whatever the catalog said
                self._usage_cache[key] = list(events)
                if workspace_id and events and self._workspace_event_ids.get(workspace_id) != list(events):
                    self._workspace_event_ids[workspace_id] = list(events)
                    changed = True
            if changed:
                self._rerender_tree_keep_selection()

        def _rerender_tree_keep_selection(self):
            current = self._current_workspace_id()
            self._render_tree(list(self._workspaces.values()))
            # Rebuilding the tree drops the selection; restore the open tab's item.
            self._tree_signature = None
            item = self._tree_items.get(current) if current else None
            if item is not None:
                self.file_tree.setCurrentItem(item)

        def _drain_check_results(self):
            """Apply live compile-check diagnostics that still match the buffer."""
            self._drain_usage_results()
            self._drain_analysis_results()
            if not self._alive or self.top is None:
                return
            updated = False
            while True:
                try:
                    outcome = self._check_results.get_nowait()
                except queue.Empty:
                    break
                if outcome.get("kind") != "checked":
                    self._log_live_check_once(
                        "live check error: %s" % str(outcome.get("error", "") or ""))
                    continue
                result = outcome.get("result") or {}
                if result.get("ok") is None:
                    self._log_live_check_once(
                        "live check unavailable: %s" % str(result.get("reason", "") or ""))
                    continue
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or document.get("documentToken") !=
                        task.get("documentToken")):
                    continue
                if int(document.get("textRevision", 0)) != int(
                        task.get("textRevision", 0)):
                    continue
                self._live_check[workspace_id] = {
                    "textRevision": int(task.get("textRevision", 0)),
                    "diagnostics": _clamp_diagnostics_to_text(
                        list(result.get("diagnostics") or []), task.get("sourceText", "")),
                    "ok": bool(result.get("ok")),
                }
                updated = True
            if updated and self._alive:
                self._refresh_current_view()

        def _current_problems(self, document, workspace):
            """Choose which diagnostics underline the open document.

            A saved, stable document shows the compiler's post-save
            diagnostics. While the buffer is dirty those saved locations no
            longer match the text, so a fresh live compile-check of the
            unsaved buffer is shown instead -- but only when it describes the
            current edit revision, so a stale squiggle is never pinned to
            code that has since moved.
            """
            if document.get("conflict"):
                return []
            if document.get("dirty"):
                revision = int(document.get("textRevision", 0))
                problems = []
                # Instant local pass (symbol/reference check) appears first;
                # the compiler's own diagnostics for the same revision follow.
                local = self._local_analysis.get(document.get("workspaceId"))
                if local is not None and local.get("textRevision") == revision:
                    problems.extend(local.get("diagnostics") or [])
                live = self._live_check.get(document.get("workspaceId"))
                if live is not None and live.get("textRevision") == revision:
                    seen = set((int(p.get("line", 0) or 0), str(p.get("text", ""))) for p in problems)
                    for item in live.get("diagnostics") or []:
                        key = (int(item.get("line", 0) or 0), str(item.get("text", "")))
                        if key not in seen:
                            problems.append(item)
                return problems
            if document.get("saveInFlight"):
                return []
            return self._workspace_problems(
                workspace or {}, document.get("rawSha256", ""))

        def _stop_save_worker(
                self, timeout_seconds=SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS):
            if not self._check_worker_stopped:
                self._check_worker_stopped = True
                self._check_stop.set()
                self._check_tasks.put(None)
                self._analysis_stop.set()
                self._analysis_tasks.put(None)
            check_worker = self._check_worker
            if (check_worker is not None and
                    check_worker is not threading.current_thread()):
                check_worker.join(min(2.0, max(0.0, float(timeout_seconds))))
                if not check_worker.is_alive():
                    self._check_worker = None
            if self._save_worker_stopped:
                worker = self._save_worker
            else:
                self._save_worker_stopped = True
                self._save_stop.set()
                self._save_tasks.put(None)
                worker = self._save_worker
            stopped = _join_save_worker(
                worker, timeout_seconds, self._save_transport_pending)
            if stopped:
                self._save_worker = None
            return stopped

        def _destroyed(self, _object=None):
            if not self._alive:
                return
            self._alive = False
            self._cancel_snapshot_refresh()
            if not self._shutting_down:
                try:
                    self._stop_save_worker()
                except Exception as exc:
                    # i18n-ignore: developer-only shutdown diagnostic.
                    print("[ReplayLab] Shader Workspace shutdown error: %s" %
                          str(exc))
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
                if getattr(self, "check_result_timer", None) is not None:
                    self.check_result_timer.stop()
                if getattr(self, "check_debounce_timer", None) is not None:
                    self.check_debounce_timer.stop()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.stop()
            except Exception:
                pass
            callback = self.closed_callback
            self.closed_callback = None
            self.top = None
            if callback:
                callback()

        def _release_native_editors(self):
            # QWidget wrappers returned by Widget() are non-owning. Detach them from the stack,
            # release Python callbacks, then ask CaptureContext to destroy the native owner.
            for document in self._documents.values():
                editor = document.get("editor")
                editor_widget = document.get("editorWidget")
                if editor is None:
                    continue
                if editor_widget is not None:
                    self.editor_stack.removeWidget(editor_widget)
                editor.ClearCallbacks()
                self.ctx.DestroyShaderCodeEditor(editor)
                document["editor"] = None
                document["editorWidget"] = None
            self._native_editors_released = True
            self._live_check.clear()
            self._local_analysis.clear()

        def _confirm_panel_close(self):
            if self._native_editors_released:
                return True
            if self._shutting_down:
                self._release_native_editors()
                return True
            for workspace_id, document in list(self._documents.items()):
                if document and document.get("dirty"):
                    if not self._confirm_close_document(document):
                        return False
            self._save_state()
            try:
                # ToolWindowManager can directly delete an accepted dock tab. Prove the Python
                # worker is quiescent before allowing that irreversible ownership transition.
                self._stop_save_worker()
                self._cancel_snapshot_refresh()
            except Exception as exc:
                # i18n-ignore: developer-only shutdown diagnostic; the translated status below is
                # the user-visible close refusal.
                print("[ReplayLab] Shader Workspace close deferred: %s" % str(exc))
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The Shader Workspace is still finishing a save. Wait for it to complete, then close the workspace again."))
                return False
            self._release_native_editors()
            return True

        def show(self):
            if not self._alive or self.top is None:
                raise RuntimeError("ReplayLab Shader Workspace has already been closed")
            if not self._docked:
                self.ctx.AddDockWindow(
                    self.top, self.qrd.DockReference.MainToolArea, None, 0.72)
                self._docked = True
            self.ctx.RaiseDockWindow(self.top)

        def shutdown(self):
            if not self._alive:
                return
            self._shutting_down = True
            self._save_state()
            try:
                self._stop_save_worker()
                self._cancel_snapshot_refresh()
            except Exception:
                # Keep the live widget and its bridge-owned controller reachable so a later stop()
                # can retry after the bounded Host request finishes. Closing here would trigger
                # destroyed/closed_callback and irreversibly lose the only worker reference.
                self._shutting_down = False
                raise
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
                if getattr(self, "check_result_timer", None) is not None:
                    self.check_result_timer.stop()
                if getattr(self, "check_debounce_timer", None) is not None:
                    self.check_debounce_timer.stop()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.stop()
                subscriptions = (
                    self.file_watcher.files() + self.file_watcher.directories())
                if subscriptions:
                    self.file_watcher.removePaths(subscriptions)
            except Exception:
                pass
            widget = self.top
            if widget is not None:
                widget.close()
                widget.deleteLater()

        def _reset_watched_paths(self, paths):
            # Existence/classification is performed by the snapshot worker. Do
            # not repeatedly remove/re-add every subscription on audit writes.
            current = self.file_watcher.files() + self.file_watcher.directories()
            removed = list(set(current) - set(paths))
            added = list(set(paths) - set(current))
            if removed:
                self.file_watcher.removePaths(removed)
            if added:
                self.file_watcher.addPaths(added)

        def _filesystem_changed(self, _path):
            # Audit/heartbeat writes request a subsequent snapshot, but do not
            # invalidate every in-flight read: that would starve a busy shader.
            # Source replacement and authority changes invalidate publication.
            sensitive = {self.registry_path, self.project_selection_path}
            for workspace in self._workspaces.values():
                source_path = workspace.get("sourcePath", "")
                sensitive.update((source_path, os.path.dirname(source_path)))
            if _path in sensitive:
                self._snapshot_generation += 1
                self._source_documents = {}
            if not self.change_timer.isActive():
                self.change_timer.start(FILESYSTEM_COALESCE_MS)

        def _document_model(self, document_id):
            return self._workspaces.get(document_id)

        def _current_capture_binding(self):
            try:
                value = (self._capture_binding_provider()
                         if self._capture_binding_provider is not None else None)
                return _shader_console_protocol.normalize_capture_binding(value)
            except Exception:
                return None

        def _can_resume_document(self, document, workspace, binding):
            previous = document.get('captureBinding') or {}
            if previous == binding:
                return True
            if document.get('saveInFlight') or document.get('commitUnknown'):
                return False
            # Reopening the same capture may restore its draft only after a fresh Host
            # authorization matched this managed source. A different RDC never inherits it.
            return bool(binding and previous.get('capturePath') == binding.get('capturePath') and
                        _normal_path(document.get('sourcePath', '')) ==
                        _normal_path(workspace.get('sourcePath', '')))

        def capture_changed(self, binding=None):
            binding = _shader_console_protocol.normalize_capture_binding(binding)
            if binding == self._capture_binding:
                return
            self._snapshot_stop.set()
            self._snapshot_generation += 1
            self._source_documents = {}
            self._pending_open = {}
            self._pending_reload = {}
            self._capture_binding = binding
            self._usage_cache = {}
            self._usage_pending = set()
            self._last_disk_signature = None
            self._tree_signature = None
            self._flow_graph_signature = None
            self._restored = False
            self._workspaces = {}
            self._tree_items = {}
            self.file_tree.clear()
            # Keep unsaved buffers in memory, but exclude the previous capture from tabs,
            # graph, F12, completion and save actions. Do not discard a user's edits.
            self.tab_bar.blockSignals(True)
            try:
                while self.tab_bar.count():
                    self.tab_bar.removeTab(0)
                for document in self._documents.values():
                    document['available'] = False
                    document['saveQueued'] = False
                    document['editor'].SetReadOnly(True)
                    document['editor'].SetNavigationEnabled(False)
            finally:
                self.tab_bar.blockSignals(False)
            self.editor_stack.setCurrentWidget(self.empty_page)
            self.flow_graph.clear_graph(graph_key='shader-workspace-empty')
            self._refresh_current_view()

        def _cancel_snapshot_refresh(self):
            self._snapshot_generation += 1
            self._snapshot_stop.set()
            self._snapshot_running = False
            self._source_documents = {}
            self._snapshot_again = False
            self._snapshot_executor.cancel_owner(self._snapshot_owner)
            if self._owns_snapshot_executor:
                self._snapshot_executor.shutdown(10000)

        def refresh(self, force=False):
            if not self._alive or self._shutting_down:
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
                force = True
            if self._snapshot_running:
                self._snapshot_again = self._snapshot_again or bool(force)
                return
            identities = dict((key, _document_read_identity(document))
                              for key, document in self._documents.items())
            self._snapshot_stop = threading.Event()
            request = {
                "stop": self._snapshot_stop,
                "generation": self._snapshot_generation,
                "captureBinding": dict(binding) if binding else None,
                "registryPath": self.registry_path,
                "projectSelectionPath": self.project_selection_path,
                "watchedPaths": list(self._watched_paths),
                "previousSignature": self._last_disk_signature,
                "force": bool(force or self._pending_open or self._pending_reload or any(
                    document.get("commitUnknown") for document in self._documents.values())),
                "documentIds": list(dict.fromkeys(
                    list(self._pending_open) + list(self._documents) + self._pinned +
                    self._restore_open)),
                "documentStates": identities,
            }
            self._snapshot_running = True
            self._snapshot_again = False
            # The operation captures only immutable request data. The shared
            # executor retains the UI callback until delivery or cancel_owner.
            self._snapshot_executor.submit(
                self._snapshot_owner, lambda: _build_workspace_snapshot(request),
                lambda result, error: self._snapshot_ready(request, result, error))

        def _snapshot_ready(self, request, result, error):
            self._snapshot_running = False
            if not self._alive or self._shutting_down:
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
            if (request["captureBinding"] != binding or
                    request["generation"] != self._snapshot_generation or
                    request["registryPath"] != self.registry_path or
                    request["projectSelectionPath"] != self.project_selection_path):
                self._snapshot_again = True
            elif not error and result and not result.get("unchanged") and not result.get("cancelled"):
                self._source_documents = result["sources"]
                self._source_request_states = request["documentStates"]
                self._apply_workspace_snapshot(result, binding)
            if self._snapshot_again:
                self._snapshot_again = False
                self.refresh(True)

        def _apply_workspace_snapshot(self, result, binding):
            snapshot = result["snapshot"]
            self._snapshot = snapshot
            previous_ids = set(self._workspaces)
            eligible = [workspace for workspace in snapshot.get('workspaces', [])
                        if workspace.get('editorEligible') is True]
            for workspace in eligible:
                if str(workspace['workspaceId']) in result.get('sourceErrors', {}):
                    workspace['autoApplyReady'] = False
            self._workspaces = {str(workspace['workspaceId']): workspace for workspace in eligible}
            event_ids = result.get("eventIds") or {}
            self._workspace_event_ids = dict(
                (str(key), list(value)) for key, value in event_ids.items()
                if isinstance(value, (list, tuple)))
            signature = tuple((workspace['workspaceId'], workspace.get('stage', ''),
                               workspace.get('entryPoint', ''), workspace.get('sourcePath', ''),
                               tuple(self._workspace_event_ids.get(str(workspace['workspaceId']), ())))
                              for workspace in eligible)
            if signature != self._tree_signature:
                self._tree_signature = signature
                self._render_tree(eligible)
            self._request_live_usage(eligible)
            self._watched_paths = result["watchedPaths"]
            self._reset_watched_paths(result["existingPaths"])
            self._last_disk_signature = result["signature"]
            for workspace_id, document in list(self._documents.items()):
                workspace = self._workspaces.get(workspace_id)
                if workspace is None or not self._can_resume_document(document, workspace, binding):
                    document['available'] = False
                    document['editor'].SetReadOnly(True)
                    document['editor'].SetNavigationEnabled(False)
                    index = self._tab_index(workspace_id)
                    if index >= 0:
                        self.tab_bar.removeTab(index)
                    continue
                document['available'] = True
                document['workspace'] = workspace
                document['authorizationId'] = workspace.get('authorizationId', '')
                document['captureBinding'] = binding
                document['editor'].SetNavigationEnabled(True)
                self._refresh_document_from_disk(workspace_id)
            if not self._restored and binding is not None:
                self._restored = bool(self._source_documents)
                restore_ids = list(dict.fromkeys(self._pinned + self._restore_open))
                for workspace_id in restore_ids[:MAX_OPEN_DOCUMENTS]:
                    if workspace_id in self._source_documents:
                        self._open_workspace(workspace_id, quiet=True, select=False)
                readable = [item for item in eligible if str(item['workspaceId']) in self._source_documents]
                if not self.tab_bar.count() and readable:
                    self._open_workspace(str(readable[0]['workspaceId']), quiet=True, select=False)
                index = self._tab_index(self._restore_active)
                index = index if index >= 0 else (0 if self.tab_bar.count() else -1)
                self.tab_bar.setCurrentIndex(index)
                self._tab_changed(index)
            else:
                added = [str(workspace['workspaceId']) for workspace in eligible
                         if str(workspace['workspaceId']) not in previous_ids and
                         str(workspace['workspaceId']) in self._source_documents]
                if added:
                    self._open_workspace(added[0], quiet=True)
            pending_open = self._pending_open
            self._pending_open = {}
            for workspace_id, options in pending_open.items():
                if workspace_id in self._source_documents:
                    self._open_workspace(workspace_id, **options)
            pending_reload = self._pending_reload
            self._pending_reload = {}
            for workspace_id, identity in pending_reload.items():
                document = self._documents.get(workspace_id)
                opened = self._cached_source_document(workspace_id)
                if (document is not None and opened is not None and
                        identity == _document_read_identity(document)):
                    self._apply_loaded_document(document, opened)
            self._refresh_current_view()

        def _cached_source_document(self, workspace_id):
            workspace = self._workspaces.get(workspace_id)
            opened = self._source_documents.get(workspace_id)
            if (workspace is None or opened is None or
                    self._capture_binding != self._current_capture_binding() or
                    opened.get("captureBinding") != self._capture_binding or
                    opened.get("authorizationId") != workspace.get("authorizationId") or
                    opened.get("liveEditId") != (workspace.get("liveEdit") or {}).get("liveEditId")):
                return None
            document = self._documents.get(workspace_id)
            if document is None and workspace_id in self._source_request_states:
                return None
            if document is not None and self._source_request_states.get(workspace_id) != _document_read_identity(document):
                return None
            return opened

        def _render_tree(self, workspaces):
            self.file_tree.clear()
            self._tree_items = {}
            captures = {}
            stages = {}
            for workspace in workspaces:
                capture_id = str(workspace.get("captureId", "") or "")
                capture_key = capture_id or "unknown"
                capture_item = captures.get(capture_key)
                if capture_item is None:
                    label = _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Capture %1"),
                        capture_id[:12] if capture_id else
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Unknown"))
                    capture_item = QtWidgets.QTreeWidgetItem((label,))
                    capture_item.setToolTip(0, capture_id)
                    captures[capture_key] = capture_item
                    self.file_tree.addTopLevelItem(capture_item)
                stage = str(workspace.get("stage", "") or "").lower()
                stage_key = (capture_key, stage)
                stage_item = stages.get(stage_key)
                if stage_item is None:
                    abbreviation = STAGE_ABBREVIATIONS.get(stage, stage.upper())
                    stage_item = QtWidgets.QTreeWidgetItem((abbreviation,))
                    stages[stage_key] = stage_item
                    capture_item.addChild(stage_item)
                file_name = os.path.basename(str(workspace.get("sourcePath", "") or ""))
                entry_point = str(workspace.get("entryPoint", "") or "")
                workspace_id = str(workspace.get("workspaceId", "") or "")
                events = list(self._workspace_event_ids.get(workspace_id, ()))
                label = "%s · %s" % (entry_point, file_name)
                tooltip = str(workspace.get("sourcePath", "") or "")
                if events:
                    # Show the events that bind this shader inline (first few)
                    # and the complete list in the tooltip; right-click jumps.
                    shown = ", ".join(str(event) for event in events[:4])
                    if len(events) > 4:
                        shown += " +%d" % (len(events) - 4)
                    label += "   [EID %s]" % shown
                    tooltip += "\n" + _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Used by %1 event(s): %2"),
                        str(len(events)),
                        ", ".join(str(event) for event in events[:64]) +
                        (" …" if len(events) > 64 else ""))
                    tooltip += "\n" + QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Right-click to jump to an event")
                leaf = QtWidgets.QTreeWidgetItem((label,))
                leaf.setData(0, QtCore.Qt.UserRole, workspace_id)
                leaf.setToolTip(0, tooltip)
                stage_item.addChild(leaf)
                self._tree_items[workspace_id] = leaf
            self.file_tree.expandAll()

        def _tree_item_clicked(self, item, _column=0):
            workspace_id = str(item.data(0, QtCore.Qt.UserRole) or "")
            if workspace_id:
                self._open_workspace(workspace_id)

        def _tree_context_menu(self, position):
            """Right-click on a shader: list the events that bind it and jump."""
            item = self.file_tree.itemAt(position)
            if item is None:
                return
            workspace_id = str(item.data(0, QtCore.Qt.UserRole) or "")
            if not workspace_id:
                return
            workspace = self._workspaces.get(workspace_id) or {}
            events = list(self._workspace_event_ids.get(workspace_id, ()))
            menu = QtWidgets.QMenu(self.file_tree)
            open_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open Shader"))
            open_action.triggered.connect(
                lambda _checked=False, value=workspace_id: self._open_workspace(value))
            if not events:
                info = menu.addAction(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "No event uses this Shader in the capture catalog"))
                info.setEnabled(False)
            else:
                menu.addSeparator()
                # Jumping only makes sense inside the capture these EIDs belong
                # to; for another open capture the actions stay visible but off.
                binding = self._current_capture_binding() or {}
                same_capture = bool(binding) and (
                    _normal_path(str(workspace.get("capturePath", "") or "")) ==
                    _normal_path(str(binding.get("capturePath", "") or "")))
                header = menu.addAction(_format_message(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Used by %1 event(s)"), str(len(events))))
                header.setEnabled(False)
                for event in events[:48]:
                    action = menu.addAction(_format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Jump to EID %1"), str(event)))
                    action.setEnabled(same_capture)
                    if not same_capture:
                        action.setToolTip(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects",
                            "Open the capture this Shader belongs to first"))
                    action.triggered.connect(
                        lambda _checked=False, value=int(event): self._jump_to_event(value))
                if len(events) > 48:
                    more = menu.addAction(_format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "… and %1 more (see tooltip)"),
                        str(len(events) - 48)))
                    more.setEnabled(False)
            menu.exec_(self.file_tree.viewport().mapToGlobal(position))

        def _jump_to_event(self, event_id):
            """Select an event in RenderDoc (same as clicking it in the event browser)."""
            try:
                event = int(event_id)
            except (TypeError, ValueError):
                return
            if event <= 0 or not self._alive:
                return
            try:
                self.ctx.SetEventID([], event, event)
            except Exception as exc:
                # i18n-ignore: developer-only diagnostic printed by the bridge.
                print("[ReplayLab] Shader Workspace jump to EID %d failed: %s" % (event, str(exc)))

        def _tab_workspace_ids(self):
            return [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]

        def _tab_index(self, workspace_id):
            for index in range(self.tab_bar.count()):
                if str(self.tab_bar.tabData(index) or "") == str(workspace_id or ""):
                    return index
            return -1

        def _tab_title(self, document):
            title = "%s · %s" % (
                document.get("entryPoint", ""), document.get("fileName", ""))
            return title + (" *" if document.get("dirty") else "")

        def _insert_tab(self, workspace_id, document):
            pinned = workspace_id in self._pinned
            insertion = self.tab_bar.count()
            if pinned:
                insertion = sum(
                    1 for item in self._tab_workspace_ids() if item in self._pinned)
            index = self.tab_bar.insertTab(insertion, self._tab_title(document))
            self.tab_bar.setTabData(index, workspace_id)
            self.tab_bar.setTabToolTip(index, document.get("sourcePath", ""))
            self.tab_bar.configure_pin_button(index, workspace_id, pinned)
            return index

        def _open_workspace(self, workspace_id, quiet=False, select=True, navigation=None):
            workspace_id = str(workspace_id or "")
            index = self._tab_index(workspace_id)
            if index >= 0:
                if select:
                    self.tab_bar.setCurrentIndex(index)
                if navigation and workspace_id in self._documents:
                    self._documents[workspace_id]["editor"].GoToLine(*navigation)
                return True
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or self._capture_binding != self._current_capture_binding():
                return False
            document = self._documents.get(workspace_id)
            if document is not None:
                if not self._can_resume_document(document, workspace, self._capture_binding):
                    return False
                document['workspace'] = workspace
                document['available'] = True
                document['captureBinding'] = self._capture_binding
                document['authorizationId'] = workspace.get('authorizationId', '')
                document['editor'].SetNavigationEnabled(True)
                self._refresh_document_from_disk(workspace_id)
                index = self._insert_tab(workspace_id, document)
                if select:
                    self.tab_bar.setCurrentIndex(index)
                    self._tab_changed(index)
                return True
            if len(self._documents) >= MAX_OPEN_DOCUMENTS:
                closed = False
                for candidate in list(self._documents):
                    document = self._documents.get(candidate)
                    if (candidate not in self._pinned and document and
                            not document.get("dirty")):
                        closed = self._close_document(candidate, prompt=False)
                        if closed:
                            break
                if not closed:
                    if not quiet:
                        QtWidgets.QMessageBox.warning(
                            self.top,
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Could not open Shader"),
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "Close an unmodified tab before opening another Shader."))
                    return False
            binding = self._current_capture_binding()
            opened = self._cached_source_document(workspace_id)
            if opened is None:
                self._pending_open[workspace_id] = {
                    "quiet": quiet, "select": select, "navigation": navigation}
                self.refresh(True)
                return False

            editor = self.ctx.CreateShaderCodeEditor(self.editor_stack)
            if editor is None:
                if not quiet:
                    QtWidgets.QMessageBox.warning(
                        self.top,
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Could not open Shader"),
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects",
                            "The native Shader editor is unavailable."))
                return False
            editor_widget = editor.Widget()
            editor_widget.setObjectName(
                "replaylabShaderEditor_%s" % workspace_id)
            source_path = (
                opened.get("sourcePath", "") or
                workspace.get("sourcePath", "") or
                workspace.get("sourceRelativePath", "") or
                opened.get("fileName", "") or
                workspace.get("fileName", ""))
            extension = os.path.splitext(str(source_path or ""))[1].lower()
            language = (
                "glsl" if extension in (".glsl", ".vert", ".frag", ".geom",
                                         ".tesc", ".tese", ".comp") else
                "slang" if extension == ".slang" else "hlsl")
            self._loading_documents.add(workspace_id)
            editor.SetFileName(source_path)
            editor.SetLanguage(language)
            editor.SetText(opened.get("text", ""))
            editor.SetSavePoint()
            self._loading_documents.discard(workspace_id)
            editor.SetTextChangedCallback(
                lambda value=workspace_id: self._document_text_changed(value))
            editor.SetSaveCallback(
                lambda value=workspace_id: self._save_document(value))
            editor.SetAddToChatCallback(
                lambda text, source_path, start_line, end_line,
                value=workspace_id: self._add_editor_selection_to_chat(
                    text, source_path, start_line, end_line, value))
            editor.SetDefinitionCallback(
                lambda file_name, line, column:
                self._definition_requested(file_name, line, column))
            self._next_document_token += 1
            document = {
                "workspaceId": workspace_id,
                "documentKind": "live-edit",
                "readOnly": False,
                "captureBinding": binding,
                "documentToken": self._next_document_token,
                "authorizationId": opened.get("authorizationId", ""),
                "workspace": workspace,
                "sourcePath": opened.get("sourcePath", ""),
                "fileName": opened.get("fileName", ""),
                "entryPoint": opened.get("entryPoint", ""),
                "editor": editor,
                "editorWidget": editor_widget,
                "rawSha256": opened.get("rawSha256", ""),
                "modifiedNs": opened.get("modifiedNs", 0),
                "size": opened.get("size", 0),
                "newline": self._newline_style(opened.get("text", "")),
                "baselineEditorText": editor.GetText(),
                "textRevision": 0,
                "dirty": False,
                "conflict": False,
                "saveInFlight": False,
                "commitUnknown": False,
                "pendingSave": None,
                "saveQueued": False,
                "closeAfterSave": False,
                "available": True,
            }
            self._documents[workspace_id] = document
            self.editor_stack.addWidget(editor_widget)
            index = self._insert_tab(workspace_id, document)
            editor.SetReadOnly(
                workspace.get("autoApplyReady") is not True)
            if select:
                self.tab_bar.setCurrentIndex(index)
                # insertTab makes the first tab current before tabData exists,
                # so its immediate currentChanged callback cannot resolve the
                # document. Activate explicitly even when the index stayed 0.
                self._tab_changed(index)
            self._save_state()
            if navigation:
                editor.GoToLine(*navigation)
            return True

        def _document_text_changed(self, workspace_id):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            document["textRevision"] = int(
                document.get("textRevision", 0)) + 1
            self._sync_document_dirty(document)
            # A live compile-check underlines semantic errors in the
            # unsaved buffer. Restart the debounce so the background
            # compile runs only once the typist pauses.
            if (self._alive and
                    getattr(self, "check_debounce_timer", None) is not None and
                    document is self._current_document()):
                self.check_debounce_timer.start()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.start()

        def _document_changed(self, workspace_id, _modified=None):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            self._sync_document_dirty(document)

        def _definition_requested(self, file_name, line, column):
            """Open only a definition already authorised by this workspace."""

            requested = str(file_name or "")
            requested_key = os.path.normcase(
                os.path.normpath(requested)).replace("\\", "/")

            def _matches(candidate):
                candidate = str(candidate or "")
                if not candidate or not requested_key:
                    return False
                candidate_key = os.path.normcase(
                    os.path.normpath(candidate)).replace("\\", "/")
                if candidate_key == requested_key:
                    return True
                # An authorised model can expose a relative project path while the language
                # server returns an absolute URI. Matching the suffix selects the authorised
                # Host document; it never reads the LSP-provided path directly.
                return (not os.path.isabs(candidate) and
                        requested_key.endswith("/" + candidate_key.lstrip("/")))

            for workspace_id, document in self._documents.items():
                if not document.get("available"):
                    continue
                workspace = document.get("workspace", {})
                candidates = (
                    document.get("sourcePath", ""),
                    document.get("fileName", ""),
                    workspace.get("sourcePath", ""),
                    workspace.get("sourceRelativePath", ""),
                    workspace.get("fileName", ""),
                )
                if any(_matches(candidate) for candidate in candidates):
                    index = self._tab_index(workspace_id)
                    if index >= 0:
                        self.tab_bar.setCurrentIndex(index)
                    document["editor"].GoToLine(
                        max(1, int(line or 1)), max(1, int(column or 1)))
                    return

            models = dict(self._workspaces)
            for workspace_id, workspace in models.items():
                candidates = (
                    workspace.get("sourcePath", ""),
                    workspace.get("sourceRelativePath", ""),
                    workspace.get("fileName", ""),
                )
                if not any(_matches(candidate) for candidate in candidates):
                    continue
                if self._open_workspace(workspace_id):
                    document = self._documents.get(workspace_id)
                    if document:
                        document["editor"].GoToLine(
                            max(1, int(line or 1)), max(1, int(column or 1)))
                    return
                if workspace_id in self._pending_open:
                    self._pending_open[workspace_id]["navigation"] = (
                        max(1, int(line or 1)), max(1, int(column or 1)))
                    return

            self.status_label.setText(_format_message(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Definition is outside the authorized Shader Workspace: %1"),
                os.path.basename(requested) or requested))

        def _sync_document_dirty(self, document):
            if document.get("readOnly"):
                document["dirty"] = False
                self._update_tab(document["workspaceId"])
                if self._current_workspace_id() == document["workspaceId"]:
                    self._refresh_current_view()
                return
            pending = document.get("pendingSave")
            if (pending is not None and
                    (document.get("saveInFlight") or
                     document.get("commitUnknown"))):
                baseline = pending.get("submittedEditorText", "")
            else:
                baseline = document.get("baselineEditorText", "")
            document["dirty"] = (
                document["editor"].GetText() != baseline)
            workspace_id = document["workspaceId"]
            self._update_tab(workspace_id)
            if self._current_workspace_id() == workspace_id:
                self._refresh_current_view()

        def _update_tab(self, workspace_id):
            index = self._tab_index(workspace_id)
            document = self._documents.get(workspace_id)
            if index >= 0 and document:
                self.tab_bar.setTabText(index, self._tab_title(document))
                self.tab_bar.configure_pin_button(
                    index, workspace_id, workspace_id in self._pinned)

        def _current_workspace_id(self):
            index = self.tab_bar.currentIndex()
            return str(self.tab_bar.tabData(index) or "") if index >= 0 else ""

        def _current_document(self):
            return self._documents.get(self._current_workspace_id())

        def _source_hierarchy(self, document):
            revision = (document.get('textRevision', 0), document.get('flowBufferReset', 0))
            if document.get('hierarchyRevision') != revision:
                document['hierarchyRevision'] = revision
                document['hierarchy'] = None
                try:
                    document['hierarchy'] = _shader_flow_hierarchy.SourceFlowHierarchy(
                        str(document['editor'].GetText()), {
                            'workspaceId': document['workspaceId'],
                            'documentId': document['workspaceId'],
                            'path': document['sourcePath'],
                            'sourceRevision': str(revision)})
                except ValueError:
                    # While a brace/signature is half typed, keep bookmarks but disable
                    # expanding an unparseable function. Never fall back to stale line numbers.
                    pass
            return document.get('hierarchy')

        def _function_line_span(self, hierarchy, record):
            start = hierarchy.tokens[record['start']].start()
            end = hierarchy.tokens[record['end']].start()
            return (hierarchy.source.count('\n', 0, start) + 1,
                    hierarchy.source.count('\n', 0, end) + 1)

        def _function_bookmark(self, document, hierarchy, function_id):
            record = hierarchy.functions.get(function_id)
            if record is None:
                return -1
            bookmarks = document.setdefault('flowFunctionBookmarks', {})
            if (function_id not in bookmarks or
                    not document['editor'].GetLineAnchorLine(bookmarks[function_id])):
                position = hierarchy.tokens[record['declaration']].start()
                line = hierarchy.source.count('\n', 0, position) + 1
                # A new projection may bind a recreated declaration. Old graph anchors keep
                # their own invalid handle and must never be redirected to this new marker.
                bookmarks[function_id] = document['editor'].CreateLineAnchor(line)
            return bookmarks[function_id]

        def _bookmarked_function(self, document, hierarchy, marker):
            if hierarchy is None:
                return None
            line = document['editor'].GetLineAnchorLine(marker)
            if not line:
                return None
            matches = [record for record in hierarchy.functions.values() if record is not None and
                       hierarchy.source.count('\n', 0, hierarchy.tokens[record['declaration']].start()) + 1 == line]
            # A bookmark identifies the edited declaration even after a parameter/function rename.
            # Multiple declarations on one line are ambiguous; do not guess between them.
            return matches[0] if len(matches) == 1 else None

        def _prune_flow_anchors(self, document, graph=None, max_anchors=8192):
            def graph_keys(value):
                if not isinstance(value, dict):
                    return set()
                value = value.get('flowGraph', value)
                return {str(node.get('source', {}).get('anchorId'))
                        for node in value.get('nodes', [])[:256]
                        if isinstance(node, dict) and isinstance(node.get('source'), dict)
                        and node['source'].get('anchorId')}

            anchors = document.setdefault('flowAnchors', {})
            bookmarks = document.setdefault('flowFunctionBookmarks', {})
            marker_fields = ('marker', 'functionMarker', 'expandMarker')
            owned = {anchor.get(field, -1) for anchor in anchors.values()
                     for field in marker_fields}
            owned.update(bookmarks.values())
            # The panel owns a bounded, inert history of these exact graph dictionaries.
            # Inspect only issued anchor IDs; neither source paths nor graph labels are authority.
            history = getattr(getattr(self.flow_graph, '_history', None), '_entries', ())
            protected = graph_keys(graph)
            retained = set(protected)
            for index, entry in enumerate(history):
                keys = graph_keys(entry[0])
                retained.update(keys)
                if index == 0:
                    protected.update(keys)  # Keep the top-level algorithm reachable.
            for key in list(anchors):
                if key not in retained:
                    del anchors[key]
            # Unusually large histories must not make every native edit scan unlimited markers.
            # An expired history node reports unavailable; it is never rebound by an old line.
            excess = max(0, len(anchors) - max(1, int(max_anchors)))
            oldest = sorted(anchors, key=lambda key: (
                key in protected, anchors[key].get('sequence', 0)))
            for key in oldest[:excess]:
                del anchors[key]
            referenced = {anchor.get(field, -1) for anchor in anchors.values()
                          for field in marker_fields}
            for function_id, marker in list(bookmarks.items()):
                if marker not in referenced:
                    del bookmarks[function_id]
            referenced.update(bookmarks.values())
            for marker in owned - referenced:
                if marker >= 0:
                    document['editor'].RemoveLineAnchor(marker)

        def _anchor_graph(self, document, graph, hierarchy):
            anchors = document.setdefault('flowAnchors', {})
            local_graph = graph.get('projectionKind') == 'current-source-function'
            for node in graph.get('nodes', [])[:256]:
                location = node.get('source')
                if not isinstance(location, dict):
                    continue
                # Graph locations are navigation metadata, never authority to open a path.
                location['documentId'] = document['workspaceId']
                location['workspaceId'] = document['workspaceId']
                symbol = str(location.get('symbol') or node.get('name') or '')
                key = '%s:%s:%s' % (document['documentToken'],
                                     graph.get('graphId', ''), node.get('id', ''))
                anchored = anchors.get(key)
                if anchored is not None:
                    # Reprojecting an accepted graph after changing tabs must preserve an
                    # edited/renamed declaration's identity. A deleted marker stays invalid.
                    record = self._bookmarked_function(
                        document, hierarchy, anchored['functionMarker'])
                    location['anchorId'] = key
                    if (local_graph and hierarchy and
                            graph.get('sourceSha256') == hierarchy.source_sha256 and
                            (record is None or not document['editor'].GetLineAnchorLine(anchored['marker']))):
                        # This is a freshly parsed local graph, possibly after undo/recreation
                        # produced the exact same text hash. Give it a new opaque identity;
                        # retained history still holds the old, invalid anchor.
                        replacement = hierarchy.functions.get(graph.get('functionId'))
                        if replacement is not None:
                            key += ':new:%s' % (document.get('flowAnchorSequence', 0) + 1)
                            anchored, record = None, replacement
                else:
                    function_id = (graph.get('functionId') if local_graph else
                                   hierarchy.function_identity(symbol)) if hierarchy else None
                    record = hierarchy.functions.get(function_id) if hierarchy else None
                if record is None:
                    node.pop('functionId', None)
                    if anchored is None:
                        location.pop('anchorId', None)
                    continue
                function_id = record['id']
                symbol = record['name']
                if anchored is None:
                    first, last = self._function_line_span(hierarchy, record)
                    line = int(location.get('line', first) or first)
                    # Only a graph derived from this buffer can supply an initial line.
                    # For an older project graph, relocate by the unique function identity.
                    if (graph.get('sourceSha256') != hierarchy.source_sha256 or
                            not first <= line <= last):
                        line = first
                    column = max(1, int(location.get('column', 1) or 1))
                    # A top-level pipeline stage navigates to where it is called
                    # in the entry body (its use), not into its definition. The
                    # functionMarker below still tracks the declaration, so
                    # double-click expansion and rename tracking are unaffected.
                    call_owner_id = None
                    if not local_graph:
                        call = hierarchy.call_site(symbol)
                        if call is not None and call.get('ownerFunctionId'):
                            line = call['line']
                            column = max(1, int(call.get('column', 1) or 1))
                            call_owner_id = call['ownerFunctionId']
                    marker = document['editor'].CreateLineAnchor(line)
                    document['flowAnchorSequence'] = document.get('flowAnchorSequence', 0) + 1
                    anchored = {'marker': marker, 'functionId': function_id,
                                'functionMarker': self._function_bookmark(document, hierarchy, function_id),
                                'expansionKind': 'call' if local_graph else 'stage',
                                'sequence': document['flowAnchorSequence'],
                                'symbol': symbol, 'column': column}
                    if call_owner_id:
                        anchored['callOwnerId'] = call_owner_id
                        anchored['callSymbol'] = symbol
                    anchors[key] = anchored
                location['anchorId'] = key
                location['symbol'] = symbol
                location['functionSignature'] = record['signature'][:1024]
                if not local_graph:
                    node['functionId'] = function_id
            self._prune_flow_anchors(document, graph)
            return graph

        def _sync_flow_graph(self, document=None):
            document = document if document is not None else self._current_document()
            if document is None or not document.get('available'):
                signature = ('shader-workspace-empty',)
                if signature != self._flow_graph_signature:
                    self._flow_graph_signature = signature
                    self.flow_graph.clear_graph(graph_key='shader-workspace-empty')
                return
            workspace = self._document_model(document['workspaceId']) or {}
            graph = workspace.get('flowGraph')
            graph_key = '%s:%s' % (document['workspaceId'],
                                   str((graph or {}).get('graphId') or 'unavailable'))
            # Text edits move native bookmarks. Rebuilding the scene on every keystroke would
            # reset the user's function exploration and detach those bookmarks.
            signature = (graph_key, document['documentToken'], document.get('flowBufferReset', 0))
            if signature == self._flow_graph_signature:
                if not document.get('flowProjectionIncomplete'):
                    return
                if self._source_hierarchy(document) is None:
                    return
            self._flow_graph_signature = signature
            if not isinstance(graph, dict):
                self.flow_graph.clear_graph(graph_key=graph_key)
                return
            hierarchy = self._source_hierarchy(document)
            document['flowProjectionIncomplete'] = hierarchy is None
            projection = hierarchy.project_root(graph) if hierarchy else json.loads(json.dumps(graph))
            self._anchor_graph(document, projection, hierarchy)
            self.flow_graph.set_graph(projection, graph_key=graph_key)
            self._prune_flow_anchors(document, projection)

        def _flow_graph_target_unavailable(self):
            self.status_label.setText(QtCore.QCoreApplication.translate(
                'ReplayLabProjects',
                'The linked calculation was removed or is incomplete. Finish editing the function before navigating.'))

        def _flow_graph_node_activated(self, payload):
            if not isinstance(payload, dict):
                return
            document = self._current_document()
            if (document is None or not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return
            anchor = document.get('flowAnchors', {}).get(str(payload.get('anchorId') or ''))
            if anchor is None:
                self._flow_graph_target_unavailable()
                return
            hierarchy = self._source_hierarchy(document)
            record = self._bookmarked_function(document, hierarchy, anchor['functionMarker'])
            line = document['editor'].GetLineAnchorLine(anchor['marker'])
            if record is None or not line:
                self._flow_graph_target_unavailable()
                return
            call_owner_id = anchor.get('callOwnerId')
            if call_owner_id:
                # A pipeline-stage anchor points at the call site in the entry
                # body. Validate the anchor still sits inside that owner function
                # and still invokes the stage before navigating; otherwise report
                # the target unavailable instead of jumping to unrelated code.
                owner = hierarchy.functions.get(call_owner_id) if hierarchy else None
                if owner is None:
                    self._flow_graph_target_unavailable()
                    return
                first, last = self._function_line_span(hierarchy, owner)
                if (not first <= line <= last or
                        not hierarchy.line_calls(line, anchor.get('callSymbol', record['name']))):
                    self._flow_graph_target_unavailable()
                    return
                document['editor'].GoToLine(line, anchor['column'])
                return
            first, last = self._function_line_span(hierarchy, record)
            if not first <= line <= last:
                self._flow_graph_target_unavailable()
                return
            document['editor'].GoToLine(line, anchor['column'])

        def _flow_graph_subgraph_requested(self, payload):
            document = self._current_document()
            if (not isinstance(payload, dict) or document is None or
                    not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return
            # Only nodes issued by this panel can request an expansion.
            anchor = document.get('flowAnchors', {}).get(str(payload.get('anchorId') or ''))
            if anchor is None:
                self._flow_graph_target_unavailable()
                return
            hierarchy = self._source_hierarchy(document)
            try:
                if hierarchy is None:
                    raise ValueError('Source is incomplete')
                owner = self._bookmarked_function(document, hierarchy, anchor['functionMarker'])
                if owner is None:
                    raise ValueError('Linked function was removed')
                function = owner
                if anchor['expansionKind'] == 'call':
                    line = document['editor'].GetLineAnchorLine(anchor['marker'])
                    current_graph = hierarchy.function_graph(owner['id'])
                    matches = [node for node in current_graph['nodes']
                               if node.get('sourceRange', {}).get('startLine', 0) <= line <=
                               node.get('sourceRange', {}).get('endLine', -1)] if line else []
                    if len(matches) != 1 or not matches[0].get('functionId'):
                        raise ValueError('Linked call was removed or is ambiguous')
                    function = hierarchy.functions.get(matches[0]['functionId'])
                    if function is None:
                        raise ValueError('Linked callee was removed')
                graph = hierarchy.function_graph(function['id'])
            except ValueError:
                self._flow_graph_target_unavailable()
                return
            self._anchor_graph(document, graph, hierarchy)
            self.flow_graph.open_subgraph(
                graph, graph_key='%s:%s' % (document['workspaceId'], graph['graphId']))
            self._prune_flow_anchors(document, graph)

        def _tab_changed(self, index):
            if index >= 0:
                workspace_id = str(self.tab_bar.tabData(index) or "")
                document = self._documents.get(workspace_id)
                if document:
                    self.editor_stack.setCurrentWidget(document["editorWidget"])
                    tree_item = self._tree_items.get(workspace_id)
                    if tree_item is not None:
                        self.file_tree.setCurrentItem(tree_item)
            else:
                self.editor_stack.setCurrentWidget(self.empty_page)
            self._sync_flow_graph()
            self._save_state()
            self._refresh_current_view()

        def _tab_moved(self, _from, _to):
            if self._reordering_tabs:
                return
            # Pinned tabs stay as one stable group at the left edge.
            self._reordering_tabs = True
            try:
                ordered = self._tab_workspace_ids()
                desired = ([item for item in ordered if item in self._pinned] +
                           [item for item in ordered if item not in self._pinned])
                if ordered != desired:
                    current = self._current_workspace_id()
                    for destination, workspace_id in enumerate(desired):
                        source = self._tab_index(workspace_id)
                        if source != destination:
                            self.tab_bar.moveTab(source, destination)
                    self.tab_bar.setCurrentIndex(self._tab_index(current))
                pinned = set(self._pinned)
                self._pinned = [
                    item for item in self._tab_workspace_ids() if item in pinned]
            finally:
                self._reordering_tabs = False
            self._save_state()

        def _toggle_pin(self, workspace_id):
            workspace_id = str(workspace_id or "")
            if workspace_id not in self._documents:
                return
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            else:
                self._pinned.append(workspace_id)
            current = self._current_workspace_id()
            document = self._documents[workspace_id]
            index = self._tab_index(workspace_id)
            self.tab_bar.removeTab(index)
            self._insert_tab(workspace_id, document)
            selected_index = self._tab_index(current)
            self.tab_bar.setCurrentIndex(selected_index)
            self._tab_changed(selected_index)
            self._save_state()

        def _confirm_close_document(self, document):
            if not document.get("dirty"):
                if (document.get("saveInFlight") or
                        document.get("commitUnknown")):
                    document["closeAfterSave"] = True
                    return False
                return True
            message = _format_message(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save changes to %1 before closing?"),
                document.get("fileName", ""))
            box = QtWidgets.QMessageBox(self.top)
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Unsaved Shader"))
            box.setText(message)
            buttons = QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel
            if document.get('available'):
                buttons |= QtWidgets.QMessageBox.Save
            else:
                box.setText(_format_message(QtCore.QCoreApplication.translate(
                    'ReplayLabProjects',
                    'There are unsaved changes to %1 from another capture. Reopen that capture to save, or discard the changes.'),
                    document.get('fileName', '')))
            box.setStandardButtons(buttons)
            answer = box.exec_()
            if answer == QtWidgets.QMessageBox.Save:
                document["closeAfterSave"] = True
                if not self._save_document(document["workspaceId"]):
                    document["closeAfterSave"] = False
                return False
            return answer == QtWidgets.QMessageBox.Discard

        def _close_document(self, workspace_id, prompt=True):
            workspace_id = str(workspace_id or "")
            document = self._documents.get(workspace_id)
            if document is None:
                return True
            if not prompt and (document.get('dirty') or document.get('saveInFlight') or
                               document.get('commitUnknown')):
                return False
            if prompt and not self._confirm_close_document(document):
                return False
            index = self._tab_index(workspace_id)
            if index >= 0:
                self.tab_bar.removeTab(index)
            editor = document["editor"]
            editor_widget = document["editorWidget"]
            self.editor_stack.removeWidget(editor_widget)
            editor.ClearCallbacks()
            self.ctx.DestroyShaderCodeEditor(editor)
            document["editor"] = None
            document["editorWidget"] = None
            self._documents.pop(workspace_id, None)
            self._pending_open.pop(workspace_id, None)
            self._pending_reload.pop(workspace_id, None)
            self._source_documents.pop(workspace_id, None)
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            if not self._documents:
                self.editor_stack.setCurrentWidget(self.empty_page)
            self._save_state()
            self._refresh_current_view()
            return True

        def _close_current(self):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                self._close_document(workspace_id)

        def _close_other_documents(self, workspace_id):
            for candidate in list(self._tab_workspace_ids()):
                if candidate != workspace_id and not self._close_document(candidate):
                    break

        def _save_current(self, _checked=False):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                return self._save_document(workspace_id)
            return True

        def _save_all(self, _checked=False):
            for workspace_id in list(self._tab_workspace_ids()):
                document = self._documents.get(workspace_id)
                if (document and not document.get("readOnly") and
                        document.get("dirty")):
                    if not self._save_document(workspace_id):
                        self.tab_bar.setCurrentIndex(self._tab_index(workspace_id))
                        return False
            return True

        def _save_document(self, workspace_id):
            document = self._documents.get(workspace_id)
            if document is None:
                return False
            if document.get("readOnly"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Project HLSL is read-only; create a managed workspace to edit and replace it."))
                return False
            workspace = self._workspaces.get(workspace_id)
            if (workspace is None or not document.get("available") or
                    document.get("captureBinding") != self._current_capture_binding()):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Workspace authorization is no longer available."))
                return False
            if workspace.get("autoApplyReady") is not True:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Live edit is not running. Restart Analyze Shader with RenderDoc AI before saving."))
                return False
            if document.get("conflict"):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
                return False
            if (document.get("saveInFlight") or
                    document.get("commitUnknown")):
                if document.get("dirty"):
                    document["saveQueued"] = True
                return True
            if not document.get("dirty"):
                return True
            source_text = self._document_source_text(document)
            try:
                submitted_hash = hashlib.sha256(
                    source_text.encode("utf-8", errors="strict")).hexdigest()
            except UnicodeEncodeError as exc:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"), str(exc))
                return False
            live_edit = workspace.get("liveEdit") or {}
            self._next_save_token += 1
            task = {
                "saveToken": self._next_save_token,
                "documentToken": document["documentToken"],
                "workspaceId": workspace_id,
                "liveEditId": str(live_edit.get("liveEditId", "") or ""),
                "authorizationId": document.get("authorizationId", ""),
                "baseHash": document.get("rawSha256", ""),
                "submittedHash": submitted_hash,
                "sourceText": source_text,
                "submittedEditorText": document["editor"].GetText(),
                "textRevision": int(document.get("textRevision", 0)),
            }
            document["pendingSave"] = task
            document["saveInFlight"] = True
            document["commitUnknown"] = False
            document["saveQueued"] = False
            # The submitted undo revision is the provisional Qt clean point.
            # A definite failure restores dirty state; a success (or observed
            # commit after timeout) leaves Undo anchored at the saved revision.
            document["editor"].SetSavePoint()
            self._sync_document_dirty(document)
            self._save_tasks.put(task)
            if not self.save_result_timer.isActive():
                self.save_result_timer.start()
            self._save_state()
            self._refresh_current_view()
            return True

        def _drain_save_results(self):
            if not self._alive or self.top is None:
                return
            refresh_unknown = False
            while True:
                try:
                    outcome = self._save_results.get_nowait()
                except queue.Empty:
                    break
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or
                        document.get("documentToken") !=
                        task.get("documentToken")):
                    continue
                pending = document.get("pendingSave") or {}
                if pending.get("saveToken") != task.get("saveToken"):
                    continue
                document["saveInFlight"] = False
                kind = outcome.get("kind")
                if kind == "saved":
                    result = outcome.get("result") or {}
                    if result.get("rawSha256") != task.get("submittedHash"):
                        self._finish_failed_save(
                            document, outcome.get("error", ""), False)
                    else:
                        self._promote_submitted_save(
                            document, result,
                            result.get("changed") is True, True)
                elif kind == "unknown":
                    document["commitUnknown"] = True
                    self._sync_document_dirty(document)
                    refresh_unknown = True
                else:
                    self._finish_failed_save(
                        document, outcome.get("error", ""),
                        kind == "conflict")
            if refresh_unknown and self._alive:
                self.refresh(True)
            if not any(
                    document.get("saveInFlight")
                    for document in self._documents.values()):
                self.save_result_timer.stop()

        def _finish_failed_save(self, document, message, conflict):
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["conflict"] = bool(conflict)
            self._sync_document_dirty(document)
            document["editor"].SetModified(bool(document.get("dirty")))
            if self._alive and self.top is not None:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    str(message or ""))
            self._save_state()
            self._refresh_current_view()

        def _save_transport_pending(self):
            checker = getattr(self._save_client, "request_pending", None)
            if not callable(checker):
                return False
            try:
                return bool(checker())
            except Exception:
                return True

        def _resolve_uncommitted_save(self, document):
            workspace_id = document["workspaceId"]
            retry = bool(document.get("saveQueued"))
            close_after_save = bool(document.get("closeAfterSave"))
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["conflict"] = False
            self._sync_document_dirty(document)
            document["editor"].SetModified(bool(document.get("dirty")))
            if document.get("dirty") and (retry or close_after_save):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()

        def _promote_submitted_save(self, document, metadata, changed,
                                    continue_queued):
            task = document.get("pendingSave")
            if task is None:
                return
            workspace_id = document["workspaceId"]
            document["rawSha256"] = task["submittedHash"]
            document["baselineEditorText"] = task["submittedEditorText"]
            document["modifiedNs"] = metadata.get("modifiedNs", 0)
            document["size"] = metadata.get("size", 0)
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["conflict"] = False
            document["available"] = True
            queued = bool(document.get("saveQueued"))
            document["saveQueued"] = False
            close_after_save = bool(document.get("closeAfterSave"))
            self._sync_document_dirty(document)
            if not document.get("dirty"):
                document["editor"].SetSavePoint()
            if self._current_workspace_id() == workspace_id:
                if changed:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Saved; waiting for compile and replacement…"))
                else:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No changes to save"))
            self._save_state()
            if (continue_queued and queued and document.get("dirty") and
                    not document.get("conflict")):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()

        @staticmethod
        def _document_source_text(document):
            source_text = document["editor"].GetText()
            newline = document.get("newline", "\n")
            if newline != "\n":
                source_text = source_text.replace("\r\n", "\n").replace(
                    "\r", "\n").replace("\n", newline)
            return source_text

        @staticmethod
        def _newline_style(text):
            value = str(text or "")
            if "\r\n" in value:
                return "\r\n"
            if "\r" in value:
                return "\r"
            return "\n"

        def _refresh_document_from_disk(self, workspace_id):
            document = self._documents.get(workspace_id)
            workspace = self._document_model(workspace_id)
            if document is None or workspace is None:
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
                return
            document["editor"].SetReadOnly(
                workspace.get("autoApplyReady") is not True)
            # The worker result is authoritative while the Host request still
            # has a known outcome. Filesystem notification can arrive first.
            if document.get("saveInFlight"):
                return
            commit_unknown = bool(
                document.get("commitUnknown") and
                document.get("pendingSave") is not None)
            opened = self._cached_source_document(workspace_id)
            if opened is None:
                return
            disk_hash = opened.get("rawSha256")
            if commit_unknown:
                task = document.get("pendingSave") or {}
                classification = _classify_commit_unknown(
                    task.get("baseHash"), task.get("submittedHash"), disk_hash)
                if classification == "committed":
                    self._promote_submitted_save(
                        document, opened,
                        task.get("submittedHash") != task.get("baseHash"), True)
                elif classification == "pending":
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    if self._save_transport_pending():
                        self._sync_document_dirty(document)
                    else:
                        self._resolve_uncommitted_save(document)
                else:
                    document["commitUnknown"] = False
                    document["pendingSave"] = None
                    document["saveQueued"] = False
                    document["closeAfterSave"] = False
                    document["conflict"] = True
                    self._sync_document_dirty(document)
                    document["editor"].SetModified(bool(document.get("dirty")))
                return
            if disk_hash == document.get("rawSha256"):
                document["modifiedNs"] = opened.get("modifiedNs", 0)
                document["size"] = opened.get("size", 0)
                return
            if document.get("dirty"):
                try:
                    editor_hash = hashlib.sha256(
                        self._document_source_text(document).encode(
                            "utf-8", errors="strict")).hexdigest()
                except UnicodeEncodeError:
                    editor_hash = ""
                if editor_hash == disk_hash:
                    document["rawSha256"] = disk_hash
                    document["baselineEditorText"] = (
                        document["editor"].GetText())
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    self._sync_document_dirty(document)
                    document["editor"].SetSavePoint()
                    self._save_state()
                    return
                document["conflict"] = True
                return
            self._apply_loaded_document(document, opened)

        def _apply_loaded_document(self, document, opened):
            workspace_id = document["workspaceId"]
            self._loading_documents.add(workspace_id)
            line = document["editor"].GetCursorLine()
            column = document["editor"].GetCursorColumn()
            document["editor"].SetText(opened.get("text", ""))
            document["flowAnchors"] = {}
            document["flowFunctionBookmarks"] = {}
            document["flowBufferReset"] = int(document.get("flowBufferReset", 0)) + 1
            self._loading_documents.discard(workspace_id)
            document["rawSha256"] = opened.get("rawSha256", "")
            document["modifiedNs"] = opened.get("modifiedNs", 0)
            document["size"] = opened.get("size", 0)
            document["newline"] = self._newline_style(opened.get("text", ""))
            document["baselineEditorText"] = document["editor"].GetText()
            document["dirty"] = False
            document["conflict"] = False
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["available"] = True
            document["editor"].SetSavePoint()
            document["editor"].GoToLine(line, column)
            self._update_tab(workspace_id)

        def _reload_current(self, _checked=False):
            document = self._current_document()
            if document is None:
                return
            if document.get("dirty"):
                answer = QtWidgets.QMessageBox.question(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Reload Shader"),
                    _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Discard local changes and reload %1 from disk?"),
                        document.get("fileName", "")),
                    QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Cancel)
                if answer != QtWidgets.QMessageBox.Discard:
                    return
            workspace = self._document_model(document["workspaceId"])
            if workspace is None:
                return
            self._pending_reload[document["workspaceId"]] = _document_read_identity(document)
            self._source_documents.pop(document["workspaceId"], None)
            self.refresh(True)

        def _workspace_problems(self, workspace, source_sha256=""):
            problems = []
            watch = workspace.get("watch") or {}
            events = watch.get("events", [])
            failed = _latest_event(events, ("compileFailed",))
            succeeded = _latest_event(events, ("compileSucceeded",))
            compile_failure_current = not (
                failed is None or (succeeded is not None and int(
                    succeeded.get("sequence", 0)) > int(failed.get("sequence", 0))))
            if compile_failure_current:
                failed_source = str(failed.get("requestedSourceSha256", "") or "")
                if not (failed_source and source_sha256 and failed_source != source_sha256):
                    sequence = int(failed.get("sequence", 0))
                    values = [
                        entry for entry in workspace.get("entries", [])
                        if (int(entry.get("cursorSequence", 0)) == sequence and
                            entry.get("severity") in ("error", "warning") and
                            int(entry.get("line", 0)) > 0)
                    ]
                    if values:
                        problems.extend(values)
                    else:
                        problems.append({
                            "severity": "error",
                            "line": 0,
                            "column": 0,
                            "text": failed.get("message") or
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Shader compilation failed"),
                        })
            # Compile can succeed while the live replacement still fails: the
            # ABI/live-replay apply gate rejected the candidate, or the apply hit
            # a transport/host error. The concrete reason is otherwise dropped and
            # only the generic "Replacement failed" status line shows. Surface the
            # real reason here (as a non-navigable line-0 diagnostic) so the user
            # can tell an ABI rejection (revert the edit) from a transient apply
            # error (just save again).
            live = workspace.get("liveEdit") or {}
            apply_failed = _latest_event(
                live.get("events", []), ("applyRejected", "applyFailed", "liveEditFailed"))
            apply_ok = _latest_event(
                live.get("events", []), ("applySucceeded", "applyReused"))
            if apply_failed is not None and (apply_ok is None or int(
                    apply_failed.get("sequence", 0)) > int(apply_ok.get("sequence", 0))):
                detail = str(apply_failed.get("reason")
                             or apply_failed.get("message") or "").strip()
                diagnostic = apply_failed.get("diagnostic")
                if isinstance(diagnostic, dict):
                    category = str(diagnostic.get("category", "") or "").strip()
                    if category and category not in detail:
                        detail = ("%s [%s]" % (detail, category)) if detail else category
                if not detail:
                    # Reuse the existing translated status label as a fallback so
                    # this adds no new untranslated UI string.
                    detail = QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Replacement failed; the last good replacement remains active")
                problems.append({
                    "severity": "error",
                    "line": 0,
                    "column": 0,
                    "text": detail,
                })
            return problems

        def _set_editor_diagnostics(self, editor, problems):
            diagnostics = []
            for problem in problems or []:
                line = int(problem.get("line", 0) or 0)
                if line <= 0:
                    continue
                diagnostic = self.qrd.ShaderEditorDiagnostic()
                diagnostic.line = line
                diagnostic.column = max(
                    1, int(problem.get("column", 0) or 1))
                diagnostic.length = max(
                    1, int(problem.get("length", 0) or 1))
                severity = str(problem.get("severity", "error") or "error")
                diagnostic.severity = (
                    self.qrd.ShaderEditorDiagnosticSeverity.Warning
                    if severity == "warning" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Information
                    if severity == "information" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Hint
                    if severity == "hint" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Error)
                diagnostic.message = str(problem.get("text", "") or "")
                # i18n-ignore: stable diagnostic producer metadata, not a UI label.
                diagnostic.source = "ReplayLab Host"
                diagnostics.append(diagnostic)
            editor.SetDiagnostics(diagnostics)

        def _problem_activated(self, item, _column=0):
            line = int(item.data(0, QtCore.Qt.UserRole) or 0)
            column = int(item.data(1, QtCore.Qt.UserRole) or 0)
            document = self._current_document()
            if document and line > 0:
                document["editor"].GoToLine(line, column)

        def _status_text(self, workspace):
            if workspace is None:
                if self._capture_binding is None:
                    return QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Open an RDC capture to load its matching project Shaders")
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for organized HLSL workspaces")
            live = workspace.get("liveEdit") or {}
            watch = workspace.get("watch") or {}
            if workspace.get("autoApplyReady") is not True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Live edit is not running; this Shader is read-only")
            if watch.get("compileInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiling…")
            if live.get("pendingApply") is True or live.get("applyInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Validating ABI and applying…")
            if watch.get("pending") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…")
            watch_failed = _latest_event(watch.get("events", []), ("compileFailed",))
            watch_ok = _latest_event(watch.get("events", []), ("compileSucceeded",))
            if watch_failed is not None and (watch_ok is None or int(
                    watch_failed.get("sequence", 0)) > int(watch_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Compilation failed; the last good replacement remains active")
            apply_failed = _latest_event(
                live.get("events", []), ("applyRejected", "applyFailed", "liveEditFailed"))
            apply_ok = _latest_event(
                live.get("events", []), ("applySucceeded", "applyReused"))
            if apply_failed is not None and (apply_ok is None or int(
                    apply_failed.get("sequence", 0)) > int(apply_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Replacement failed; the last good replacement remains active")
            if int(live.get("activeRevision", 0) or 0) > 0:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiled and applied")
            return QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Watching saves")

        def _refresh_current_view(self):
            document = self._current_document()
            self._sync_flow_graph(document)
            if document is None:
                self.subtitle.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Reviewed organized HLSL · Ctrl+S saves and automatically compiles/replaces"))
                self.save_button.setEnabled(False)
                self.save_all_button.setEnabled(any(
                    item.get("available") and not item.get("readOnly") and item.get("dirty") and
                    not item.get("conflict") and
                    not item.get("saveInFlight") and
                    not item.get("commitUnknown")
                    for item in self._documents.values()))
                self.reload_button.setVisible(False)
                self.problems.clear()
                self.status_label.setText(self._status_text(None))
                return
            workspace = self._document_model(document["workspaceId"])
            self.subtitle.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Reviewed organized HLSL · Ctrl+S saves and automatically compiles/replaces"))
            self.save_button.setEnabled(bool(
                document.get("available") and document.get("dirty") and workspace and
                workspace.get("autoApplyReady") is True and
                not document.get("conflict") and
                not document.get("saveInFlight") and
                not document.get("commitUnknown")))
            self.save_all_button.setEnabled(any(
                item.get("available") and not item.get("readOnly") and item.get("dirty") and
                not item.get("conflict") and
                not item.get("saveInFlight") and
                not item.get("commitUnknown")
                for item in self._documents.values()))
            self.reload_button.setVisible(bool(document.get("conflict")))
            if document.get("conflict"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
            elif (document.get("saveInFlight") or
                  document.get("commitUnknown")):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…"))
            elif document.get("dirty"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Modified"))
            else:
                self.status_label.setText(self._status_text(workspace))

            # Compiler locations describe the saved revision. Keep its console
            # history, but do not underline a newer, unsaved document with it.
            problems = self._current_problems(document, workspace)
            self.problems.clear()
            for problem in problems:
                line = int(problem.get("line", 0) or 0)
                text_value = str(problem.get("text", "") or "")
                item = QtWidgets.QTreeWidgetItem((str(line) if line else "", text_value))
                item.setData(0, QtCore.Qt.UserRole, line)
                item.setData(1, QtCore.Qt.UserRole, int(problem.get("column", 0) or 0))
                if problem.get("severity") == "error":
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor("#e06c75")))
                self.problems.addTopLevelItem(item)
            self._set_editor_diagnostics(document["editor"], problems)
            if not problems:
                placeholder = QtWidgets.QTreeWidgetItem((
                    "", QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No compilation problems")))
                placeholder.setFlags(placeholder.flags() & ~QtCore.Qt.ItemIsSelectable)
                self.problems.addTopLevelItem(placeholder)


else:
    class ReplayLabShaderWorkspacePanel(object):
        def __init__(self, _ctx, _qrd, closed_callback=None, registry_path=None,
                     save_client=None, project_selection_path=None,
                     capture_binding_provider=None):
            del closed_callback, registry_path, save_client, project_selection_path, capture_binding_provider
            raise RuntimeError("PySide2 Qt is required for ReplayLab Shader Workspace")


__all__ = ["HAS_QT", "ReplayLabShaderWorkspacePanel"]
