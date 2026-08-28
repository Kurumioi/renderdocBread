"""ReplayLab Console for qrenderdoc.

The console combines a fixed, append-only ReplayLab event journal with the
authorised shader watcher/live-edit audit trail.  Its trust roots are a fixed
per-user journal directory and the process-owned persistent shader
authorisation registry; paths found only in compiler text are never opened.
The module does not invoke a model, a shell, a compiler, or a replay mutation.

This file is intentionally Python 3.6 and PySide2 compatible because it is
imported by qrenderdoc's embedded interpreter.  The parsing/projection helpers
remain usable without Qt so they can be contract-tested independently.
"""

from __future__ import print_function

import ctypes
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - ordinary unit tests intentionally lack Qt
    QtCore = None
    QtGui = None
    QtWidgets = None
    HAS_QT = False


REGISTRY_SCHEMA = "replaylab-shader-live-edit-registry/v1"
WORKSPACE_SCHEMA = "renderdoc-ultra-shader-workspace/v1"
WATCH_SCHEMA = "renderdoc-ultra-shader-watch/v1"
LIVE_EDIT_SCHEMA = "replaylab-shader-live-edit/v1"
ORGANIZATION_GATE_SCHEMA = "renderdoc-ai-shader-organization-gate/v1"
ORGANIZATION_POLICY_VERSION = "renderdoc-ai-organize-workspace/v1"
CONSOLE_SCHEMA = "replaylab-console/v1"
JOURNAL_EVENT_SCHEMA = "replaylab-console-event/v1"

MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_LOG_BYTES = 1024 * 1024
MAX_AUTHORIZATIONS = 256
MAX_EVENTS = 2048
MAX_CONSOLE_ENTRIES = 4096
MAX_TEXT = 16384
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_LINE_BYTES = 32 * 1024
MAX_JOURNAL_READ_FILES = 8
MAX_JOURNAL_READ_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_READ_BYTES_PER_FILE = 2 * 1024 * 1024
MAX_JOURNAL_SEGMENTS_PER_SESSION = 4
MAX_JOURNAL_RETENTION_FILES = 32
MAX_JOURNAL_RETENTION_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_DIRECTORY_ENTRIES = 512
JOURNAL_RETENTION_CHECK_INTERVAL = 128
JOURNAL_RETENTION_MIN_INTERVAL_SECONDS = 60.0

CONSOLE_SEVERITIES = ("info", "warning", "error")
CONSOLE_MODULES = (
    "system", "bridge", "host", "project", "capture", "pass", "material",
    "shader", "replay", "toolchain",
)
CONSOLE_CONTEXT_FIELDS = (
    "operation", "captureId", "captureSessionId", "eventId", "resourceId",
    "stage", "passId", "projectId", "workspaceId",
)

TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")
EXACT_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_EXTENSION_RE = re.compile(
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese)$",
    re.IGNORECASE,
)
HLSL_SOURCE_EXTENSION_RE = re.compile(r"\.(?:hlsl|hlsli|fx|fxh)$", re.IGNORECASE)
JOURNAL_FILE_RE = re.compile(
    r"^events\.(?P<role>[a-z][a-z0-9_-]{0,31})\."
    r"(?P<pid>[0-9]{1,10})\.(?P<session>[0-9a-f]{32})\."
    r"(?P<segment>[0-9]{4})\.jsonl$"
)

# DXC/D3DCompiler commonly use either ``file:line:column: error`` or
# ``file(line,column): error``.  The suffix-qualified file group prevents a
# Windows drive colon from being mistaken for the line separator.
COLON_DIAGNOSTIC_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:[\\/]|/)?[^\r\n]*?"
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese))"
    r":(?P<line>[0-9]+)(?::(?P<column>[0-9]+))?\s*:\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\b"
    r"(?P<message>[^\r\n]*)",
    re.IGNORECASE,
)
PAREN_DIAGNOSTIC_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:[\\/]|/)?[^\r\n]*?"
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese))"
    r"\((?P<line>[0-9]+)(?:\s*,\s*(?P<column>[0-9]+))?\)\s*:\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\b"
    r"(?P<message>[^\r\n]*)",
    re.IGNORECASE,
)

# These two fixed host diagnostics mean that an apply job lost its exact
# source/candidate binding because a newer save arrived.  They are expected
# latest-wins queue behaviour, not a compiler, ABI, or RenderDoc mutation
# failure.  Keep this list deliberately narrow so every other applyFailed
# remains a real error in the console.
SUPERSEDED_APPLY_FAILURE_FRAGMENTS = (
    "shader source changed after the candidate compile",
    "workspace no longer contains the expected saved candidate",
    "workspace no longer contains expected saved candidate",
)


class ShaderConsoleError(RuntimeError):
    pass


class ShaderConsoleConflictError(ShaderConsoleError):
    """The authorized source changed after the editor's expected hash."""

    pass


def _canonical_sha256(value):
    """Match the Host registry's stable identity digest exactly."""
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ShaderConsoleError(
            "live-edit authorization identity is not canonical JSON: %s" %
            _bounded(exc, 512))
    return hashlib.sha256(payload).hexdigest()


def default_registry_path(local_app_data=None):
    root = str(local_app_data or os.environ.get("LOCALAPPDATA", "") or "").strip()
    if not root:
        return ""
    return os.path.abspath(os.path.join(
        root, "ReplayLab", "config", "shader-live-edit-authorizations.v1.json"))


def default_console_journal_directory(local_app_data=None):
    """Return the one fixed per-user directory accepted by the console UI."""
    root = str(local_app_data or os.environ.get("LOCALAPPDATA", "") or "").strip()
    if not root:
        return ""
    return os.path.abspath(os.path.join(root, "ReplayLab", "console-events"))


def _bounded(value, maximum=MAX_TEXT):
    text = str(value or "")
    if "\x00" in text:
        text = text.replace("\x00", "")
    if len(text) > maximum:
        return text[:maximum] + "..."
    return text


def normalise_console_severity(value):
    """Collapse progress/success/note into Unity's ordinary message class."""
    severity = str(value or "info").strip().lower()
    if severity in ("warning", "warn"):
        return "warning"
    if severity in ("error", "fatal", "failure", "failed"):
        return "error"
    return "info"


def _console_module(value):
    module = str(value or "system").strip().lower()
    if module not in CONSOLE_MODULES:
        raise ShaderConsoleError("console module is not registered")
    return module


def _console_context(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ShaderConsoleError("console event context must be an object")
    clean = {}
    for key in CONSOLE_CONTEXT_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ShaderConsoleError("console event context field is invalid")
        clean[key] = _bounded(item, 1024)
    return clean


def _utc_now_text(unix_ms=None):
    value = int(unix_ms if unix_ms is not None else time.time() * 1000)
    seconds = value // 1000
    milliseconds = value % 1000
    return "%s.%03dZ" % (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)), milliseconds)


_JOURNAL_LOCK = threading.RLock()
_JOURNAL_SESSION_ID = uuid.uuid4().hex
_JOURNAL_SEQUENCE = 0
_JOURNAL_SEGMENT = 0
_JOURNAL_RETENTION_COUNTER = 0
_JOURNAL_LAST_RETENTION_MONOTONIC = 0.0


def _journal_filename(directory, role, segment):
    return os.path.join(directory, "events.%s.%d.%s.%04d.jsonl" % (
        role, os.getpid(), _JOURNAL_SESSION_ID, segment))


def _fixed_journal_directory(directory):
    """Reject every spool path except ReplayLab's exact per-user directory."""
    expected = default_console_journal_directory()
    if not expected or os.path.normcase(os.path.abspath(str(directory))) != os.path.normcase(
            os.path.abspath(expected)):
        raise ShaderConsoleError("ReplayLab console journal directory is not fixed")
    return os.path.abspath(expected)


def _regular_journal_file(path):
    """Accept one ordinary, single-link file without following redirections."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if (not stat.S_ISREG(info.st_mode) or os.path.islink(path) or
            bool(getattr(info, "st_file_attributes", 0) & 0x400)):
        return False
    # A hard link could make appending to the journal modify an unrelated file.
    # Fresh ReplayLab journal files always have exactly one directory entry.
    return int(getattr(info, "st_nlink", 1) or 1) == 1


def _journal_candidates(directory):
    """Enumerate only exact, bounded, non-linked ReplayLab journal segments."""
    root = _fixed_journal_directory(directory)
    if not os.path.isdir(root) or _is_reparse(root):
        return []
    values = []
    try:
        iterator = os.scandir(root)
    except OSError:
        return []
    try:
        for index, directory_entry in enumerate(iterator):
            if index >= MAX_JOURNAL_DIRECTORY_ENTRIES:
                break
            name = directory_entry.name
            match = JOURNAL_FILE_RE.fullmatch(name)
            if match is None:
                continue
            path = os.path.join(root, name)
            if not _regular_journal_file(path):
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            size = int(info.st_size)
            if size < 0 or size > MAX_JOURNAL_BYTES:
                continue
            values.append({
                "mtime": float(info.st_mtime),
                "name": name,
                "path": path,
                "match": match,
                "size": size,
            })
    finally:
        iterator.close()
    return values


def _enforce_console_journal_retention(directory, protected_path=""):
    """Bound only the generic Console spool; shader/security audits are untouched."""
    root = _fixed_journal_directory(directory)
    protected = os.path.normcase(os.path.abspath(protected_path)) if protected_path else ""
    candidates = _journal_candidates(root)
    removals = set()

    own = [item for item in candidates if (
        item["match"].group("session") == _JOURNAL_SESSION_ID and
        int(item["match"].group("pid")) == os.getpid())]
    own.sort(key=lambda item: int(item["match"].group("segment")), reverse=True)
    for item in own[MAX_JOURNAL_SEGMENTS_PER_SESSION:]:
        removals.add(item["path"])

    retained = [item for item in candidates if item["path"] not in removals]
    retained.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    kept_bytes = 0
    for index, item in enumerate(retained):
        if (index >= MAX_JOURNAL_RETENTION_FILES or
                kept_bytes + item["size"] > MAX_JOURNAL_RETENTION_BYTES):
            removals.add(item["path"])
        else:
            kept_bytes += item["size"]

    removed = 0
    for path in sorted(removals):
        if protected and os.path.normcase(os.path.abspath(path)) == protected:
            continue
        # Revalidate immediately before deletion. Never delete an invalid name,
        # directory, symlink, reparse point, or hard link after a directory race.
        name = os.path.basename(path)
        if (JOURNAL_FILE_RE.fullmatch(name) is None or
                os.path.dirname(os.path.abspath(path)) != root or
                not _regular_journal_file(path)):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            # A live producer may still own the segment. Retention is best effort
            # and a later bounded check will retry without disrupting publishing.
            pass
    return removed


def _maybe_enforce_console_journal_retention(directory, protected_path="",
                                             force=False):
    """Throttle reader-side retention while keeping a fixed safety bound."""
    global _JOURNAL_LAST_RETENTION_MONOTONIC
    now = time.monotonic()
    with _JOURNAL_LOCK:
        if (not force and _JOURNAL_LAST_RETENTION_MONOTONIC > 0 and
                now - _JOURNAL_LAST_RETENTION_MONOTONIC <
                JOURNAL_RETENTION_MIN_INTERVAL_SECONDS):
            return 0
        removed = _enforce_console_journal_retention(
            directory, protected_path=protected_path)
        _JOURNAL_LAST_RETENTION_MONOTONIC = now
        return removed


def _ensure_fixed_journal_directory(directory):
    directory = _fixed_journal_directory(directory)
    parent = os.path.dirname(directory)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    if os.path.exists(parent) and _is_reparse(parent):
        raise ShaderConsoleError("ReplayLab console journal parent is redirected")
    if not os.path.isdir(directory):
        os.makedirs(directory)
    if _is_reparse(directory):
        raise ShaderConsoleError("ReplayLab console journal directory is redirected")


def publish_console_event(module, severity, message, phase="", code="",
                          context=None, process_role="bridge"):
    """Append one trusted fixed-program event; no caller-controlled path exists.

    The API deliberately accepts only display scalars and a small context
    allow-list. It cannot carry a command, executable, argument list or source
    path. Each process owns its journal segment, so publishers never replace or
    delete another process's audit evidence.
    """
    global _JOURNAL_SEQUENCE, _JOURNAL_SEGMENT, _JOURNAL_RETENTION_COUNTER
    module_value = _console_module(module)
    supplied_severity = str(severity or "info").strip().lower()
    if supplied_severity not in (
            "info", "progress", "success", "note", "warning", "warn",
            "error", "fatal", "failure", "failed"):
        raise ShaderConsoleError("console event severity is invalid")
    severity_value = normalise_console_severity(severity)
    role_value = str(process_role or "bridge").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role_value) is None:
        raise ShaderConsoleError("console process role is invalid")
    message_value = _bounded(message)
    if not message_value:
        raise ShaderConsoleError("console event message is empty")
    phase_value = _bounded(phase or module_value, 128)
    code_value = _bounded(code, 256)
    context_value = _console_context(context)
    directory = default_console_journal_directory()
    with _JOURNAL_LOCK:
        _ensure_fixed_journal_directory(directory)
        _JOURNAL_SEQUENCE += 1
        unix_ms = int(time.time() * 1000)
        event = {
            "schema": JOURNAL_EVENT_SCHEMA,
            "schemaVersion": 1,
            "sessionId": _JOURNAL_SESSION_ID,
            "sequence": _JOURNAL_SEQUENCE,
            "unixMs": unix_ms,
            "at": _utc_now_text(unix_ms),
            "processRole": role_value,
            "processId": os.getpid(),
            "module": module_value,
            "severity": severity_value,
            "phase": phase_value,
            "code": code_value,
            "message": message_value,
            "context": context_value,
        }
        encoded = (json.dumps(
            event, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise ShaderConsoleError("console event exceeds its bounded size")
        path = _journal_filename(directory, role_value, _JOURNAL_SEGMENT)
        try:
            if os.path.exists(path) and not _regular_journal_file(path):
                raise ShaderConsoleError(
                    "console event journal is not an ordinary private file")
            if (os.path.isfile(path) and
                    os.path.getsize(path) + len(encoded) > MAX_JOURNAL_BYTES):
                _JOURNAL_SEGMENT += 1
                path = _journal_filename(directory, role_value, _JOURNAL_SEGMENT)
                if os.path.exists(path) and not _regular_journal_file(path):
                    raise ShaderConsoleError(
                        "console event journal segment is redirected")
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError("console event journal made no write progress")
                    offset += written
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ShaderConsoleError(
                "could not append ReplayLab console event: %s" % _bounded(exc, 512))
        _JOURNAL_RETENTION_COUNTER += 1
        if (_JOURNAL_RETENTION_COUNTER == 1 or
                _JOURNAL_RETENTION_COUNTER % JOURNAL_RETENTION_CHECK_INTERVAL == 0):
            _maybe_enforce_console_journal_retention(
                directory, protected_path=path, force=True)
    return dict(event)


def _positive_integer(value, default=0, maximum=0x7FFFFFFF):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0 or value > maximum:
        return default
    return value


def _is_reparse(path):
    """Reject symlinks and Windows reparse points before following them."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if os.path.islink(path):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _same_path(first, second):
    return _normal_path(first) == _normal_path(second)


def _within(root, path):
    try:
        root_value = _normal_path(root)
        path_value = _normal_path(path)
        return os.path.commonpath((root_value, path_value)) == root_value
    except (OSError, ValueError):
        return False


def _regular_file(path, maximum, field):
    value = os.path.abspath(str(path))
    if not os.path.isfile(value) or _is_reparse(value):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)
    size = os.path.getsize(value)
    if size < 0 or size > maximum:
        raise ShaderConsoleError("%s exceeds its bounded size" % field)
    return value


def _regular_file_in_tree(root, path, maximum, field):
    """Accept a regular file only through ordinary directories under root."""
    root_value = _directory(root, field + " root")
    value = os.path.abspath(str(path))
    if not _within(root_value, value):
        raise ShaderConsoleError("%s escapes its fixed directory" % field)
    current = os.path.dirname(value)
    while not _same_path(current, root_value):
        if not _within(root_value, current) or _is_reparse(current):
            raise ShaderConsoleError("%s directory is redirected" % field)
        parent = os.path.dirname(current)
        if _same_path(parent, current):
            raise ShaderConsoleError("%s directory is invalid" % field)
        current = parent
    return _regular_file(value, maximum, field)


def _validate_windows_file_handle(kernel32, handle, filename, field):
    """Bind a Win32 handle to the exact ordinary file named by the caller."""

    class _FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("attributes", ctypes.c_uint32), ("created", _FileTime),
            ("accessed", _FileTime), ("written", _FileTime),
            ("volumeSerial", ctypes.c_uint32), ("sizeHigh", ctypes.c_uint32),
            ("sizeLow", ctypes.c_uint32), ("links", ctypes.c_uint32),
            ("fileIndexHigh", ctypes.c_uint32),
            ("fileIndexLow", ctypes.c_uint32),
        )

    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation))
    get_information.restype = ctypes.c_int
    information = _ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.attributes & (0x400 | 0x10):  # REPARSE_POINT | DIRECTORY
        raise ShaderConsoleError("%s is unavailable or redirected" % field)

    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)
    get_final_path.restype = ctypes.c_uint32
    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(get_final_path(handle, buffer, capacity, 0))
    if length <= 0 or length >= capacity:
        raise ctypes.WinError(ctypes.get_last_error())
    final_path = buffer.value
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    if (os.path.normcase(os.path.abspath(final_path)) !=
            os.path.normcase(os.path.abspath(filename))):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)


def _read_file_bytes(path, maximum, field):
    """Read a bounded file without blocking an atomic Windows replacement.

    CPython's ordinary Windows file open does not promise delete sharing. The
    live-edit writer publishes audits with ``os.replace``; a 100ms console
    reader must therefore opt into READ|WRITE|DELETE sharing explicitly.
    """
    filename = _regular_file(path, maximum, field)
    if os.name != "nt":
        try:
            with open(filename, "rb") as stream:
                raw = stream.read(maximum + 1)
        except OSError as exc:
            raise ShaderConsoleError(
                "%s is unavailable: %s" % (field, _bounded(exc, 512)))
        if len(raw) > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        return raw

    handle = None
    close_handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        create_file.restype = ctypes.c_void_p
        get_size = kernel32.GetFileSizeEx
        get_size.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong))
        get_size.restype = ctypes.c_int
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p)
        read_file.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        handle = create_file(
            filename, 0x80000000, 0x1 | 0x2 | 0x4, None, 3,
            0x08000000 | 0x00200000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            raise ctypes.WinError(ctypes.get_last_error())
        _validate_windows_file_handle(kernel32, handle, filename, field)
        size = ctypes.c_longlong()
        if not get_size(handle, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value < 0 or size.value > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        remaining = int(size.value)
        chunks = []
        while remaining > 0:
            request = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(request)
            received = ctypes.c_uint32()
            if not read_file(
                    handle, buffer, request, ctypes.byref(received), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if received.value <= 0:
                break
            chunks.append(buffer.raw[:received.value])
            remaining -= received.value
        if remaining != 0:
            raise ShaderConsoleError("%s changed while it was read" % field)
        return b"".join(chunks)
    except ShaderConsoleError:
        raise
    except (OSError, ValueError) as exc:
        raise ShaderConsoleError(
            "%s is unavailable: %s" % (field, _bounded(exc, 512)))
    finally:
        if close_handle is not None and handle not in (
                None, 0, ctypes.c_void_p(-1).value):
            try:
                close_handle(handle)
            except Exception:
                pass


def _read_file_tail_bytes(path, maximum, tail_maximum, field):
    """Read only the newest bounded tail while preserving complete JSONL rows."""
    filename = _regular_file(path, maximum, field)
    tail_limit = max(1, min(int(tail_maximum), int(maximum)))

    def _complete_tail(raw, wanted_start):
        if wanted_start <= 0:
            return raw
        # The read starts one byte before the requested tail. If that byte is a
        # newline, the requested offset already starts on a record boundary;
        # otherwise discard the one partial record deterministically.
        if raw.startswith(b"\n"):
            return raw[1:]
        boundary = raw.find(b"\n")
        return raw[boundary + 1:] if boundary >= 0 else b""

    if os.name != "nt":
        try:
            with open(filename, "rb") as stream:
                size = int(os.fstat(stream.fileno()).st_size)
                if size < 0 or size > maximum:
                    raise ShaderConsoleError("%s exceeds its bounded size" % field)
                wanted_start = max(0, size - tail_limit)
                read_start = max(0, wanted_start - 1)
                stream.seek(read_start, os.SEEK_SET)
                raw = stream.read(size - read_start)
                return _complete_tail(raw, wanted_start)
        except ShaderConsoleError:
            raise
        except OSError as exc:
            raise ShaderConsoleError(
                "%s is unavailable: %s" % (field, _bounded(exc, 512)))

    handle = None
    close_handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        create_file.restype = ctypes.c_void_p
        get_size = kernel32.GetFileSizeEx
        get_size.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong))
        get_size.restype = ctypes.c_int
        set_pointer = kernel32.SetFilePointerEx
        set_pointer.argtypes = (
            ctypes.c_void_p, ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong), ctypes.c_uint32)
        set_pointer.restype = ctypes.c_int
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p)
        read_file.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        handle = create_file(
            filename, 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x08000000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            raise ctypes.WinError(ctypes.get_last_error())
        size_value = ctypes.c_longlong()
        if not get_size(handle, ctypes.byref(size_value)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = int(size_value.value)
        if size < 0 or size > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        wanted_start = max(0, size - tail_limit)
        read_start = max(0, wanted_start - 1)
        if read_start and not set_pointer(
                handle, read_start, None, 0):  # FILE_BEGIN
            raise ctypes.WinError(ctypes.get_last_error())
        remaining = size - read_start
        chunks = []
        while remaining > 0:
            request = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(request)
            received = ctypes.c_uint32()
            if not read_file(
                    handle, buffer, request, ctypes.byref(received), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if received.value <= 0:
                break
            chunks.append(buffer.raw[:received.value])
            remaining -= received.value
        return _complete_tail(b"".join(chunks), wanted_start)
    except ShaderConsoleError:
        raise
    except (OSError, ValueError) as exc:
        raise ShaderConsoleError(
            "%s is unavailable: %s" % (field, _bounded(exc, 512)))
    finally:
        if close_handle is not None and handle not in (
                None, 0, ctypes.c_void_p(-1).value):
            try:
                close_handle(handle)
            except Exception:
                pass


def _directory(path, field):
    value = os.path.abspath(str(path))
    if not os.path.isdir(value) or _is_reparse(value):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)
    return value


def _read_json(path, maximum, field):
    try:
        raw = _read_file_bytes(path, maximum, field)
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except ShaderConsoleError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ShaderConsoleError("%s is invalid: %s" % (field, _bounded(exc, 512)))
    if not isinstance(value, dict):
        raise ShaderConsoleError("%s must contain one JSON object" % field)
    return value


def _safe_relative(root, relative, field):
    if not isinstance(relative, str) or not relative or len(relative) > 4096:
        raise ShaderConsoleError("%s is invalid" % field)
    if os.path.isabs(relative):
        raise ShaderConsoleError("%s must be workspace-relative" % field)
    candidate = os.path.abspath(os.path.join(root, relative.replace("/", os.sep)))
    if not _within(root, candidate):
        raise ShaderConsoleError("%s escapes its workspace" % field)
    return candidate


def _validate_authorization(value):
    required_fields = {
        "authorizationId", "workspacePath", "workspaceIdentity",
        "workspaceIdentitySha256", "debounceMs", "authorizedAt",
        "updatedAt", "lastLiveEditId",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise ShaderConsoleError("live-edit authorization entry is invalid")
    authorization_id = str(value.get("authorizationId", "") or "").lower()
    live_edit_id = str(value.get("lastLiveEditId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(authorization_id) is None:
        raise ShaderConsoleError("live-edit authorization ID is invalid")
    if live_edit_id and EXACT_TOKEN_RE.fullmatch(live_edit_id) is None:
        raise ShaderConsoleError("live-edit session ID is invalid")
    debounce = value.get("debounceMs")
    if (isinstance(debounce, bool) or not isinstance(debounce, int) or
            debounce < 100 or debounce > 5000):
        raise ShaderConsoleError("live-edit authorization debounce is invalid")
    for field in ("authorizedAt", "updatedAt"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 128:
            raise ShaderConsoleError(
                "live-edit authorization timestamp is invalid")
    workspace_text = value.get("workspacePath")
    if not isinstance(workspace_text, str) or not os.path.isabs(workspace_text):
        raise ShaderConsoleError("authorized workspace path is invalid")
    workspace = _directory(workspace_text, "authorized workspace")
    identity = value.get("workspaceIdentity")
    identity_fields = {
        "workspaceId", "captureId", "capturePath", "shaderKey",
        "shaderRawSha256", "resourceId", "stage", "entryPoint",
    }
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise ShaderConsoleError("authorized workspace identity is invalid")
    identity_sha256 = str(value.get("workspaceIdentitySha256", "") or "").lower()
    if (SHA256_RE.fullmatch(identity_sha256) is None or
            identity_sha256 != _canonical_sha256(identity)):
        raise ShaderConsoleError(
            "authorized workspace identity hash is invalid")
    workspace_id = str(identity.get("workspaceId", "") or "").lower()
    capture_id = str(identity.get("captureId", "") or "").lower()
    raw_sha256 = str(identity.get("shaderRawSha256", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(workspace_id) is None:
        raise ShaderConsoleError("authorized workspace ID is invalid")
    if SHA256_RE.fullmatch(capture_id) is None or SHA256_RE.fullmatch(raw_sha256) is None:
        raise ShaderConsoleError("authorized capture/shader identity is invalid")
    capture_path = identity.get("capturePath")
    shader_key = identity.get("shaderKey")
    if (not isinstance(capture_path, str) or not capture_path or
            len(capture_path) > 32768 or not isinstance(shader_key, str) or
            not shader_key or len(shader_key) > 4096):
        raise ShaderConsoleError(
            "authorized capture/shader locator is invalid")
    resource_id = _positive_integer(identity.get("resourceId"), 0, 0x7FFFFFFFFFFFFFFF)
    if resource_id <= 0:
        raise ShaderConsoleError("authorized Shader ResourceId is invalid")
    stage = str(identity.get("stage", "") or "")
    entry_point = str(identity.get("entryPoint", "") or "")
    if (not stage or len(stage) > 64 or not entry_point or
            len(entry_point) > 512):
        raise ShaderConsoleError(
            "authorized Shader stage/entry point is invalid")
    return {
        "authorizationId": authorization_id,
        "lastLiveEditId": live_edit_id,
        "workspacePath": workspace,
        "workspaceId": workspace_id,
        "captureId": capture_id,
        "shaderRawSha256": raw_sha256,
        "stage": stage.lower(),
        "entryPoint": entry_point,
        "resourceId": resource_id,
        "capturePath": capture_path,
        "shaderKey": shader_key,
        "workspaceIdentitySha256": identity_sha256,
        "debounceMs": debounce,
        "authorizedAt": value["authorizedAt"],
        "updatedAt": value["updatedAt"],
    }


def _read_bounded_text(path, maximum, field):
    try:
        raw = _read_file_bytes(path, maximum, field)
        return raw.decode("utf-8", errors="replace")
    except ShaderConsoleError:
        raise
    except OSError as exc:
        raise ShaderConsoleError("%s is unavailable: %s" % (field, _bounded(exc, 512)))


def _manifest_compile_diagnostics(value, workspace, source_path):
    """Read compiler stderr only through failed, manifest-bound job records."""
    history = value.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_EVENTS:
        return {}
    jobs_root = os.path.join(workspace, "jobs")
    by_source_sha256 = {}
    for record in history:
        if not isinstance(record, dict) or record.get("status") != "failed":
            continue
        operation = str(record.get("operation", "") or "")
        if operation not in ("compile", "compile-route-chain"):
            continue
        hops = record.get("hops") if operation == "compile-route-chain" else [record]
        if not isinstance(hops, list):
            continue
        for hop in hops:
            if not isinstance(hop, dict) or hop.get("status") != "failed":
                continue
            if str(hop.get("operation", "") or "") != "compile":
                continue
            source_sha256 = str(hop.get("inputSha256", "") or "").lower()
            relative = hop.get("stderrPath")
            if SHA256_RE.fullmatch(source_sha256) is None or not isinstance(relative, str):
                continue
            try:
                stderr_path = _safe_relative(workspace, relative, "compiler stderr path")
                if not _within(jobs_root, stderr_path):
                    continue
                stderr = _read_bounded_text(
                    stderr_path, MAX_DIAGNOSTIC_LOG_BYTES, "compiler stderr")
                diagnostics = parse_compiler_diagnostics(
                    stderr, source_path, workspace)
                if diagnostics:
                    by_source_sha256[source_sha256] = diagnostics
            except ShaderConsoleError:
                continue
    return by_source_sha256


def _validate_manifest(workspace, authorization):
    manifest_path = os.path.join(workspace, "manifest.json")
    value = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    if value.get("schema") != WORKSPACE_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader workspace manifest schema is invalid")
    if str(value.get("workspaceId", "")).lower() != authorization["workspaceId"]:
        raise ShaderConsoleError("authorized workspace ID no longer matches its manifest")
    capture = value.get("capture")
    shader = value.get("shader")
    source = value.get("source")
    if not isinstance(capture, dict) or not isinstance(shader, dict) or not isinstance(source, dict):
        raise ShaderConsoleError("shader workspace manifest identity is incomplete")
    expected = authorization
    comparisons = (
        (str(capture.get("captureId", "")).lower(), expected["captureId"]),
        (str(shader.get("rawSha256", "")).lower(), expected["shaderRawSha256"]),
        (str(shader.get("stage", "")).lower(), expected["stage"]),
        (str(shader.get("entryPoint", "")), expected["entryPoint"]),
        (_positive_integer(shader.get("resourceId"), 0, 0x7FFFFFFFFFFFFFFF),
         expected["resourceId"]),
    )
    if any(observed != declared for observed, declared in comparisons):
        raise ShaderConsoleError("authorized shader identity no longer matches its manifest")
    if (source.get("encoding") != "utf-8" or
            source.get("lane") != "executable-editable" or
            source.get("compileEligible") is not True):
        raise ShaderConsoleError(
            "workspace source is not an executable editable UTF-8 representation")
    source_path = _safe_relative(workspace, source.get("path"), "workspace source path")
    source_root = os.path.join(workspace, "source")
    if not _within(source_root, source_path) or SOURCE_EXTENSION_RE.search(source_path) is None:
        raise ShaderConsoleError("workspace source is outside its dedicated source directory")
    source_path = _regular_file_in_tree(
        source_root, source_path, MAX_SOURCE_BYTES, "workspace shader source")
    diagnostics = _manifest_compile_diagnostics(value, workspace, source_path)
    return source_path, diagnostics, manifest_path


def _validate_organization_gate(workspace, authorization, source_path):
    """Prove that the editor source was promoted by an organise-only review.

    The durable live-edit authorization proves consent to compile and apply
    later saves. This separate fixed gate proves that the initial HLSL exposed
    by the code workspace is the reviewed, organised executable-editable lane
    instead of raw decompiler output or an explanatory proposal.
    """
    gate_path = _safe_relative(
        workspace, "ai/organize-code-gate.json", "shader organization gate path")
    _regular_file_in_tree(
        os.path.join(workspace, "ai"), gate_path, MAX_MANIFEST_BYTES,
        "shader organization gate")
    gate = _read_json(
        gate_path, MAX_MANIFEST_BYTES, "shader organization gate")
    if gate.get("schema") != ORGANIZATION_GATE_SCHEMA:
        raise ShaderConsoleError("shader organization gate schema is invalid")
    if (gate.get("schemaVersion") != 1 or
            gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            gate.get("requestedTransformation") != "organize-only" or
            gate.get("decision") != "accepted-executable-editable" or
            gate.get("codeSha256Equal") is not True or
            gate.get("organizedSourcePromoted") is not True or
            gate.get("baselineCompiledAndAbiValidated") is not True or
            gate.get("organizedCompiledAndAbiValidated") is not True or
            gate.get("safeToStartLiveEdit") is not True or
            gate.get("replayApplied") is not False):
        raise ShaderConsoleError(
            "shader organization gate did not promote editable HLSL")
    identity = (
        str(gate.get("workspaceId", "") or "").lower(),
        str(gate.get("captureId", "") or "").lower(),
        str(gate.get("stage", "") or "").lower(),
        str(gate.get("entryPoint", "") or ""),
    )
    expected = (
        authorization["workspaceId"], authorization["captureId"],
        authorization["stage"], authorization["entryPoint"])
    binding = gate.get("finalBinding")
    final_source_sha256 = str(gate.get("finalSourceSha256", "") or "").lower()
    binding_fields = {
        "complete", "sourceSha256", "revision", "candidateRawSha256",
        "codeSha256", "abiRequestId", "abiReportSha256",
    }
    organization_id = str(gate.get("organizationId", "") or "").lower()
    reviewed_at = gate.get("reviewedAt")
    baseline_source = str(gate.get("baselineSourceSha256", "") or "").lower()
    proposal_source = str(gate.get("proposalSourceSha256", "") or "").lower()
    baseline_code = str(gate.get("baselineCodeSha256", "") or "").lower()
    organized_code = str(gate.get("organizedCodeSha256", "") or "").lower()
    final_candidate = str(gate.get("finalCandidateRawSha256", "") or "").lower()
    final_code = str(gate.get("finalCodeSha256", "") or "").lower()
    final_abi_request = str(gate.get("finalAbiRequestId", "") or "").lower()
    final_abi_report = str(gate.get("finalAbiReportSha256", "") or "").lower()
    final_revision = gate.get("finalRevision")
    if (identity != expected or not isinstance(binding, dict) or
            set(binding) != binding_fields or
            binding.get("complete") is not True or
            EXACT_TOKEN_RE.fullmatch(organization_id) is None or
            not isinstance(reviewed_at, str) or not reviewed_at or
            len(reviewed_at) > 128 or
            SHA256_RE.fullmatch(final_source_sha256) is None or
            any(SHA256_RE.fullmatch(item) is None for item in (
                baseline_source, proposal_source, baseline_code,
                organized_code, final_candidate, final_code,
                final_abi_request, final_abi_report)) or
            baseline_code != organized_code or
            isinstance(final_revision, bool) or
            not isinstance(final_revision, int) or final_revision <= 0 or
            str(binding.get("sourceSha256", "") or "").lower() !=
            final_source_sha256 or
            binding.get("revision") != final_revision or
            str(binding.get("candidateRawSha256", "") or "").lower() !=
            final_candidate or
            str(binding.get("codeSha256", "") or "").lower() != final_code or
            str(binding.get("abiRequestId", "") or "").lower() !=
            final_abi_request or
            str(binding.get("abiReportSha256", "") or "").lower() !=
            final_abi_report):
        raise ShaderConsoleError(
            "shader organization gate is not bound to this workspace")

    declared_source = gate.get("executableEditableSourcePath")
    if not isinstance(declared_source, str) or not declared_source:
        raise ShaderConsoleError("shader organization gate source is missing")
    if os.path.isabs(declared_source):
        declared_path = os.path.abspath(declared_source)
        if not _within(workspace, declared_path):
            raise ShaderConsoleError(
                "shader organization gate source escapes its workspace")
    else:
        declared_path = _safe_relative(
            workspace, declared_source, "organized editable source path")
    if not _same_path(declared_path, source_path):
        raise ShaderConsoleError(
            "shader organization gate points at a different source")
    return {
        "path": gate_path,
        "finalSourceSha256": final_source_sha256,
        "finalRevision": final_revision,
        "finalCandidateRawSha256": final_candidate,
        "finalCodeSha256": final_code,
    }


def _authorized_workspace(registry_path, authorization_id, workspace_id):
    """Resolve one exact durable live-edit authorization.

    The editor never accepts a path selected from the tree as authority. Every
    open and save re-opens the fixed registry, validates its bounded schema,
    resolves the two opaque identities, and then revalidates the workspace
    manifest before touching source bytes.
    """
    path = os.path.abspath(str(registry_path or default_registry_path() or ""))
    if not path:
        raise ShaderConsoleError("live-edit authorization registry is unavailable")
    registry = _read_json(path, MAX_REGISTRY_BYTES, "live-edit authorization registry")
    if registry.get("schema") != REGISTRY_SCHEMA or registry.get("schemaVersion") != 1:
        raise ShaderConsoleError("live-edit authorization registry schema is invalid")
    entries = registry.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_AUTHORIZATIONS:
        raise ShaderConsoleError("live-edit authorization registry entries are invalid")

    requested_authorization = str(authorization_id or "").lower()
    requested_workspace = str(workspace_id or "").lower()
    if (TOKEN_RE.fullmatch(requested_authorization) is None or
            TOKEN_RE.fullmatch(requested_workspace) is None):
        raise ShaderConsoleError("shader editor workspace identity is invalid")

    # Select by inert scalar IDs before resolving any workspace path. An old,
    # unrelated authorization whose workspace was deleted must not deny access
    # to the exact requested authorization; only the selected entry gains the
    # authority to touch disk and is then validated in full.
    matching_raw = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        raw_identity = raw.get("workspaceIdentity")
        raw_authorization = str(raw.get("authorizationId", "") or "").lower()
        raw_workspace = (str(raw_identity.get("workspaceId", "") or "").lower()
                         if isinstance(raw_identity, dict) else "")
        if (raw_authorization == requested_authorization and
                raw_workspace == requested_workspace):
            matching_raw.append(raw)
    if len(matching_raw) != 1:
        raise ShaderConsoleError("shader editor workspace authorization is unavailable or ambiguous")

    authorization = _validate_authorization(matching_raw[0])
    source_path, compiler_diagnostics, manifest_path = _validate_manifest(
        authorization["workspacePath"], authorization)
    if HLSL_SOURCE_EXTENSION_RE.search(source_path) is None:
        raise ShaderConsoleError("authorized workspace source is not HLSL")
    organization_gate = _validate_organization_gate(
        authorization["workspacePath"], authorization, source_path)
    return (authorization, source_path, compiler_diagnostics, manifest_path,
            organization_gate)


def _require_running_live_edit(authorization, source_path):
    """Require the exact authorized watcher/live pair to be running."""
    live_edit_id = str(authorization.get("lastLiveEditId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(live_edit_id) is None:
        raise ShaderConsoleError("authorized live-edit session is not running")
    workspace = authorization["workspacePath"]
    live_root = os.path.join(workspace, "live-edit")
    live_path = _regular_file_in_tree(
        live_root, os.path.join(live_root, live_edit_id + ".json"),
        MAX_AUDIT_BYTES, "authorized shader live-edit audit")
    live_value = _read_json(
        live_path, MAX_AUDIT_BYTES, "authorized shader live-edit audit")
    live = _validate_live(live_value, workspace, live_edit_id)
    if live.get("state") != "running":
        raise ShaderConsoleError("authorized live-edit session is not running")
    watch_id = str(live.get("watchId", "") or "").lower()
    watch_root = os.path.join(workspace, "watch")
    watch_path = _regular_file_in_tree(
        watch_root, os.path.join(watch_root, watch_id + ".json"),
        MAX_AUDIT_BYTES, "authorized shader watcher audit")
    watch_value = _read_json(
        watch_path, MAX_AUDIT_BYTES, "authorized shader watcher audit")
    watch = _validate_watch(watch_value, workspace, source_path, watch_id)
    if watch.get("state") != "running":
        raise ShaderConsoleError("authorized shader watcher is not running")
    return live, watch


def load_authorized_source_document(registry_path, authorization_id, workspace_id):
    """Read one editable HLSL document through its durable authorization."""
    authorization, source_path, _diagnostics, _manifest_path, gate = _authorized_workspace(
        registry_path, authorization_id, workspace_id)
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "workspace shader source")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError("workspace shader source is not valid UTF-8: %s" %
                                 _bounded(exc, 512))
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw_sha256 != gate["finalSourceSha256"]:
        _live, watch = _require_running_live_edit(authorization, source_path)
        if (watch.get("initialSourceSha256") != gate["finalSourceSha256"] or
                watch.get("observedSourceSha256") != raw_sha256):
            raise ShaderConsoleError(
                "workspace shader source is not in the authorized organization/edit lineage")
    info = os.stat(source_path)
    return {
        "authorizationId": authorization["authorizationId"],
        "workspaceId": authorization["workspaceId"],
        "workspacePath": authorization["workspacePath"],
        "sourcePath": source_path,
        "fileName": os.path.basename(source_path),
        "stage": authorization["stage"],
        "entryPoint": authorization["entryPoint"],
        "resourceId": authorization["resourceId"],
        "text": text,
        "rawSha256": raw_sha256,
        "size": len(raw),
        "modifiedNs": getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
    }


def save_authorized_source_document(registry_path, authorization_id, workspace_id,
                                    expected_raw_sha256, text):
    """Reject the retired embedded-process write path.

    Shader Workspace saves must cross the authenticated singleton Host and bind
    to its in-memory live-edit/watcher job. Keeping this fail-closed symbol
    makes older integrations receive an explicit error instead of silently
    falling back to an unaudited direct filesystem write.
    """
    del registry_path, authorization_id, workspace_id, expected_raw_sha256, text
    raise ShaderConsoleError(
        "direct shader source saves are disabled; use the running ReplayLab Host")




def _latest_token_json(directory, preferred=""):
    if not os.path.isdir(directory) or _is_reparse(directory):
        return ""
    if preferred and TOKEN_RE.fullmatch(preferred):
        path = os.path.join(directory, preferred + ".json")
        if os.path.isfile(path) and not _is_reparse(path):
            return path
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return ""
    if len(names) > MAX_AUTHORIZATIONS * 4:
        names = names[:MAX_AUTHORIZATIONS * 4]
    for name in names:
        stem, extension = os.path.splitext(name)
        if extension.lower() != ".json" or TOKEN_RE.fullmatch(stem) is None:
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or _is_reparse(path):
            continue
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    return max(candidates)[1] if candidates else ""


def _validate_events(value, field):
    events = value.get("events", [])
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ShaderConsoleError("%s event list is invalid or too large" % field)
    clean = []
    previous = 0
    for raw in events:
        if not isinstance(raw, dict):
            raise ShaderConsoleError("%s contains a non-object event" % field)
        sequence = _positive_integer(raw.get("sequence"), 0)
        event_type = _bounded(raw.get("type", ""), 128)
        if sequence <= previous or not event_type:
            raise ShaderConsoleError("%s event identity/order is invalid" % field)
        previous = sequence
        # Preserve only scalar and fixed diagnostic display fields.  No command,
        # executable, or arguments from an audit become part of the UI model.
        event = {
            "sequence": sequence,
            "at": _bounded(raw.get("at", ""), 128),
            "type": event_type,
            "message": _bounded(raw.get("message", "")),
            "errorType": _bounded(raw.get("errorType", ""), 256),
            "revision": _positive_integer(raw.get("revision"), 0),
            "attempt": _positive_integer(raw.get("attempt"), 0),
            "replacementId": _bounded(raw.get("replacementId", ""), 128),
            "requestedSourceSha256": _bounded(
                raw.get("requestedSourceSha256", ""), 64).lower(),
        }
        diagnostic = raw.get("diagnostic")
        if isinstance(diagnostic, dict):
            event["diagnosticMessage"] = _bounded(diagnostic.get("message", ""))
            event["diagnosticCategory"] = _bounded(diagnostic.get("category", ""), 128)
            event["diagnosticPhase"] = _bounded(diagnostic.get("phase", ""), 128)
        event["diagnostics"] = []
        raw_diagnostics = raw.get("diagnostics")
        if isinstance(raw_diagnostics, list) and len(raw_diagnostics) <= 128:
            for item in raw_diagnostics:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity", "error") or "error").lower()
                if severity not in ("error", "warning", "note"):
                    severity = "error"
                line_value = item.get("line")
                column_value = item.get("column")
                event["diagnostics"].append({
                    "file": _bounded(item.get("file", ""), 4096),
                    "line": _positive_integer(line_value, 0, 100000000),
                    "column": _positive_integer(column_value, 0, 100000000),
                    "severity": severity,
                    "message": _bounded(item.get("message", ""), 4096),
                })
        clean.append(event)
    return clean


def _validate_watch(value, workspace, source_path, expected_watch_id=""):
    if value.get("schema") != WATCH_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader watcher audit schema is invalid")
    watch_id = str(value.get("watchId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(watch_id) is None or (
            expected_watch_id and watch_id != expected_watch_id):
        raise ShaderConsoleError("shader watcher audit identity is invalid")
    if not _same_path(value.get("workspacePath", ""), workspace):
        raise ShaderConsoleError("shader watcher audit changed workspace")
    if not _same_path(value.get("sourcePath", ""), source_path):
        raise ShaderConsoleError("shader watcher audit changed source file")
    initial_source_sha256 = str(
        value.get("initialSourceSha256", "") or "").lower()
    observed_source_sha256 = str(
        value.get("observedSourceSha256", "") or "").lower()
    if (SHA256_RE.fullmatch(initial_source_sha256) is None or
            SHA256_RE.fullmatch(observed_source_sha256) is None):
        raise ShaderConsoleError("shader watcher source lineage is invalid")
    return {
        "watchId": watch_id,
        "state": _bounded(value.get("state", ""), 32),
        "compileInProgress": value.get("compileInProgress") is True,
        "pending": value.get("pending") is True,
        "caughtUp": value.get("caughtUp") is True,
        "initialSourceSha256": initial_source_sha256,
        "observedSourceSha256": observed_source_sha256,
        "lastAttemptedSourceSha256": str(
            value.get("lastAttemptedSourceSha256", "") or "").lower(),
        "lastCompiledSourceSha256": str(
            value.get("lastCompiledSourceSha256", "") or "").lower(),
        "latestCandidateRevision": _positive_integer(
            value.get("latestCandidateRevision"), 0),
        "compileAttempts": _positive_integer(value.get("compileAttempts"), 0),
        "compileSuccesses": _positive_integer(value.get("compileSuccesses"), 0),
        "compileFailures": _positive_integer(value.get("compileFailures"), 0),
        "events": _validate_events(value, "shader watcher audit"),
    }


def _validate_live(value, workspace, expected_live_id=""):
    if value.get("schema") != LIVE_EDIT_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader live-edit audit schema is invalid")
    live_id = str(value.get("liveEditId", "") or "").lower()
    watch_id = str(value.get("watchId", "") or "").lower()
    if (EXACT_TOKEN_RE.fullmatch(live_id) is None or
            EXACT_TOKEN_RE.fullmatch(watch_id) is None):
        raise ShaderConsoleError("shader live-edit audit identity is invalid")
    if expected_live_id and live_id != expected_live_id:
        raise ShaderConsoleError("shader live-edit audit is not the authorized session")
    if not _same_path(value.get("workspacePath", ""), workspace):
        raise ShaderConsoleError("shader live-edit audit changed workspace")
    active = value.get("activeReplacement")
    active_revision = 0
    if isinstance(active, dict):
        active_revision = _positive_integer(active.get("revision"), 0)
    return {
        "liveEditId": live_id,
        "watchId": watch_id,
        "state": _bounded(value.get("state", ""), 32),
        "applyAttempts": _positive_integer(value.get("applyAttempts"), 0),
        "applySuccesses": _positive_integer(value.get("applySuccesses"), 0),
        "applyRejections": _positive_integer(value.get("applyRejections"), 0),
        "applyFailures": _positive_integer(value.get("applyFailures"), 0),
        "applyReuses": _positive_integer(value.get("applyReuses"), 0),
        "applyInProgress": isinstance(value.get("applyInProgress"), dict),
        "pendingApply": isinstance(value.get("pendingApply"), dict),
        "latestHandledRevision": _positive_integer(
            value.get("latestHandledRevision"), 0),
        "latestScheduledRevision": _positive_integer(
            value.get("latestScheduledRevision"), 0),
        "activeRevision": active_revision,
        "events": _validate_events(value, "shader live-edit audit"),
    }


def _authorised_diagnostic_source(reported, default_source, workspace):
    """Map compiler job copies back to the one authorised editable source."""
    reported = str(reported or "").strip().strip('"')
    if not reported:
        return default_source
    source_root = os.path.join(workspace, "source")
    jobs_root = os.path.join(workspace, "jobs")
    candidate = reported
    if not os.path.isabs(candidate):
        candidate = os.path.join(source_root, candidate)
    candidate = os.path.abspath(candidate)
    if _within(source_root, candidate) and os.path.isfile(candidate) and not _is_reparse(candidate):
        return candidate
    if _within(jobs_root, candidate):
        return default_source
    # DXC can report only ``input.hlsl`` even though the fixed runner copied the
    # authorised source into a job-local input file.
    if os.path.basename(reported).lower().startswith("input."):
        return default_source
    return default_source


def parse_compiler_diagnostics(message, default_source_path, workspace_path=None):
    """Parse bounded DXC/D3DCompiler locations without trusting their paths.

    Every returned ``sourceFile`` is either the exact authorised source or a
    regular include under that workspace's dedicated ``source`` directory.
    """
    text = _bounded(message)
    source = os.path.abspath(str(default_source_path))
    workspace = os.path.abspath(str(workspace_path or os.path.dirname(os.path.dirname(source))))
    matches = []
    occupied = []
    for pattern in (PAREN_DIAGNOSTIC_RE, COLON_DIAGNOSTIC_RE):
        for match in pattern.finditer(text):
            span = match.span()
            if any(not (span[1] <= item[0] or span[0] >= item[1]) for item in occupied):
                continue
            occupied.append(span)
            line = _positive_integer(int(match.group("line")), 0, 100000000)
            column_text = match.group("column")
            column = _positive_integer(int(column_text), 0, 100000000) if column_text else 0
            severity = match.group("severity").lower().replace("fatal ", "")
            reported = match.group("file").strip()
            detail = match.group("message").strip()
            detail = re.sub(r"^\s*:\s*", "", detail)
            matches.append({
                "severity": severity,
                "message": _bounded(detail or ("compiler %s" % severity)),
                "sourceFile": _authorised_diagnostic_source(
                    reported, source, workspace),
                "reportedFile": _bounded(reported, 4096),
                "line": line,
                "column": column,
                "offset": span[0],
            })
    matches.sort(key=lambda item: item.pop("offset"))
    return matches


def _entry(origin, event, severity, phase, text, source_path="", line=0,
           column=0, reported_file="", stream_key="", identity_suffix=""):
    sequence = event.get("sequence", 0)
    cursor_sequence = event.get("cursorSequence", sequence)
    stream_value = stream_key or origin
    identity = "%s:%d" % (stream_value, sequence)
    if identity_suffix:
        identity += ":" + str(identity_suffix)
    return {
        "id": identity,
        "origin": origin,
        "streamKey": stream_value,
        "sequence": sequence,
        "cursorSequence": cursor_sequence,
        "at": event.get("at", ""),
        "unixMs": _positive_integer(event.get("unixMs"), 0, 0x7FFFFFFFFFFFFFFF),
        "eventType": event.get("type", ""),
        "severity": normalise_console_severity(severity),
        "phase": phase,
        "text": _bounded(text),
        # A source is locatable only when an actual compiler diagnostic supplied
        # an explicit line. Session/ABI/persistence errors must never become the
        # misleading ``shader.hlsl:1:1`` fallback.
        "sourceFile": source_path if source_path and line > 0 else "",
        "reportedFile": reported_file,
        "line": line if line > 0 else 0,
        "column": column if line > 0 and column > 0 else 0,
        "module": "shader",
    }


def _diagnostic_identity(diagnostic, duplicate_index=0):
    """Return a stable content identity without borrowing audit sequences."""
    payload = json.dumps([
        normalise_console_severity(diagnostic.get("severity", "error")),
        str(diagnostic.get("reportedFile", "") or ""),
        _positive_integer(diagnostic.get("line"), 0, 100000000),
        _positive_integer(diagnostic.get("column"), 0, 100000000),
        str(diagnostic.get("message", "") or ""),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return "diagnostic:%s:%d" % (digest, int(duplicate_index))


def _is_superseded_apply_failure(event):
    """Return true only for the fixed stale-candidate race diagnostics."""
    if not isinstance(event, dict) or event.get("type") != "applyFailed":
        return False
    message = str(event.get("message", "") or "").lower()
    return any(fragment in message for fragment in SUPERSEDED_APPLY_FAILURE_FRAGMENTS)


def console_entries_from_audits(workspace, source_path, watch=None, live=None,
                                compiler_diagnostics=None):
    """Project fixed audit events into a bounded Unity-style console model."""
    entries = []
    workspace_key = _normal_path(workspace)
    watch_stream = "%s:watch:%s" % (
        workspace_key, (watch or {}).get("watchId", "default"))
    live_stream = "%s:live:%s" % (
        workspace_key, (live or {}).get("liveEditId", "default"))
    watch_labels = {
        "watchCreated": ("info", "监听", "Shader 源码监听已创建"),
        "watchStarted": ("success", "监听", "Shader 源码监听已启动"),
        "sourceChanged": ("info", "检测", "检测到 Shader 源码保存"),
        "compileStarted": ("progress", "编译", "正在编译 Shader"),
        "compileSucceeded": ("success", "编译", "Shader 编译成功，等待验证和应用"),
        "compileRetryScheduled": (
            "warning", "编译", "检测到瞬时文件占用，将自动重试本次保存"),
        "sourceChangedDuringCompile": (
            "warning", "检测", "编译期间检测到新的保存，将继续处理最新版本"),
        "observationFailed": ("error", "监听", "读取 Shader 源码失败"),
        "observationRecovered": ("success", "监听", "Shader 源码监听已恢复"),
        "stopRequested": ("warning", "监听", "Shader 源码监听正在停止"),
        "watchStopRequested": ("warning", "监听", "Shader 源码监听正在停止"),
        "watchStopped": ("warning", "监听", "Shader 源码监听已停止"),
        "watchFailed": ("error", "监听", "Shader 源码监听失败"),
    }
    if isinstance(watch, dict):
        for event in watch.get("events", []):
            event_type = event.get("type", "")
            if event_type == "compileFailed":
                message = event.get("message") or event.get("diagnosticMessage") or "Shader 编译失败"
                diagnostics = []
                for structured in event.get("diagnostics", []):
                    diagnostics.append({
                        "severity": structured["severity"],
                        "message": structured["message"] or message,
                        "sourceFile": _authorised_diagnostic_source(
                            structured.get("file", ""), source_path, workspace),
                        "reportedFile": structured.get("file", ""),
                        "line": structured.get("line", 0),
                        "column": structured.get("column", 0),
                    })
                if not diagnostics:
                    diagnostics = parse_compiler_diagnostics(
                        message, source_path, workspace)
                source_sha256 = event.get("requestedSourceSha256", "")
                if (not diagnostics and isinstance(compiler_diagnostics, dict)
                        and SHA256_RE.fullmatch(source_sha256 or "")):
                    diagnostics = compiler_diagnostics.get(source_sha256, [])
                if diagnostics:
                    diagnostic_occurrences = {}
                    for diagnostic in diagnostics:
                        cloned = dict(event)
                        cloned["cursorSequence"] = event.get("sequence", 0)
                        base_identity = _diagnostic_identity(diagnostic, 0)
                        duplicate_index = diagnostic_occurrences.get(base_identity, 0)
                        diagnostic_occurrences[base_identity] = duplicate_index + 1
                        entries.append(_entry(
                            "watch", cloned, diagnostic["severity"], "编译",
                            diagnostic["message"], diagnostic["sourceFile"],
                            diagnostic["line"], diagnostic["column"],
                            diagnostic["reportedFile"], watch_stream,
                            _diagnostic_identity(diagnostic, duplicate_index)))
                else:
                    entries.append(_entry(
                        "watch", event, "error", "编译",
                        "%s: %s" % (event.get("errorType") or "编译错误", message),
                        stream_key=watch_stream))
                continue
            label = watch_labels.get(event_type)
            if label:
                text = label[2]
                if event.get("attempt"):
                    text += "（第 %d 次）" % event["attempt"]
                if event.get("revision"):
                    text += " · revision %d" % event["revision"]
                if event.get("message") and event_type in ("observationFailed",):
                    text += "：" + event["message"]
                entries.append(_entry(
                    "watch", event, label[0], label[1], text,
                    stream_key=watch_stream))

    live_labels = {
        "liveEditCreated": ("info", "实时更新", "实时 Shader 更新会话已创建"),
        "liveEditStarted": ("success", "实时更新", "实时 Shader 更新已启动"),
        "compileRecovered": ("success", "编译", "Shader 编译已从上一次错误恢复"),
        "applyStarted": ("progress", "ABI / 应用", "正在验证 ABI 并应用 Shader"),
        "applyQueued": ("progress", "应用队列", "Shader 已进入实时应用队列"),
        "applySuperseded": (
            "warning", "应用队列", "较旧 Shader 已由更新的保存取代"),
        "applySucceeded": ("success", "ABI / 应用", "Shader 已验证并更新到 RenderDoc"),
        "applyReused": (
            "success", "ABI / 应用", "Shader 字节未变，复用已验证的实时替换"),
        "applyRejected": ("error", "ABI / 应用", "Shader 被 ABI / Replay 门禁拒绝"),
        "applyFailed": ("error", "ABI / 应用", "Shader 应用失败"),
        "liveEditFailed": ("error", "实时更新", "实时 Shader 更新会话失败"),
        "persistentAuthorizationRevokeFailed": (
            "error", "授权", "撤销实时编辑持久授权失败"),
        "workspaceWatchStopFailed": ("error", "监听", "停止源码监听失败"),
        "liveEditStopRequested": ("warning", "实时更新", "实时 Shader 更新正在停止"),
        "liveEditStopped": ("warning", "实时更新", "实时 Shader 更新已停止"),
    }
    if isinstance(live, dict):
        for event in live.get("events", []):
            event_type = event.get("type", "")
            # The watcher event contains richer compiler diagnostics; avoid a
            # duplicate red row when both audits are present.
            if event_type == "compileFailed" and isinstance(watch, dict):
                continue
            label = live_labels.get(event_type)
            if event_type == "compileFailed":
                label = ("error", "编译", "Shader 编译失败")
            elif _is_superseded_apply_failure(event):
                label = (
                    "warning", "应用队列",
                    "较旧 Shader 应用已由新的保存取代")
            if not label:
                continue
            text = label[2]
            if event.get("revision"):
                text += " · revision %d" % event["revision"]
            if event.get("message"):
                text += "：" + event["message"]
            entries.append(_entry(
                "live", event, label[0], label[1], text,
                stream_key=live_stream))

    entries.sort(key=lambda item: (
        item.get("at", ""), 0 if item.get("origin") == "watch" else 1,
        item.get("sequence", 0)))
    return entries[-MAX_CONSOLE_ENTRIES:]


def _validate_journal_event(raw, filename_match, previous_sequence):
    if not isinstance(raw, dict):
        raise ShaderConsoleError("console journal contains a non-object event")
    if raw.get("schema") != JOURNAL_EVENT_SCHEMA or raw.get("schemaVersion") != 1:
        raise ShaderConsoleError("console journal event schema is invalid")
    session_id = str(raw.get("sessionId", "") or "").lower()
    role = str(raw.get("processRole", "") or "").lower()
    process_id = _positive_integer(raw.get("processId"), 0, 0x7FFFFFFF)
    sequence = _positive_integer(raw.get("sequence"), 0, 0x7FFFFFFFFFFFFFFF)
    if (session_id != filename_match.group("session") or
            role != filename_match.group("role") or
            process_id != int(filename_match.group("pid")) or
            sequence <= previous_sequence):
        raise ShaderConsoleError("console journal event identity/order is invalid")
    unix_ms = _positive_integer(raw.get("unixMs"), 0, 0x7FFFFFFFFFFFFFFF)
    message = _bounded(raw.get("message", ""))
    severity = str(raw.get("severity", "") or "").lower()
    if not message or severity not in CONSOLE_SEVERITIES or unix_ms <= 0:
        raise ShaderConsoleError("console journal display fields are invalid")
    module = _console_module(raw.get("module", ""))
    context = _console_context(raw.get("context"))
    stream = "journal:%s:%d:%s" % (role, process_id, session_id)
    return {
        "id": "%s:%d" % (stream, sequence),
        "origin": "journal",
        "streamKey": stream,
        "sequence": sequence,
        "cursorSequence": sequence,
        "at": _bounded(raw.get("at", ""), 128) or _utc_now_text(unix_ms),
        "unixMs": unix_ms,
        "eventType": _bounded(raw.get("code", ""), 256) or "consoleEvent",
        "severity": severity,
        "phase": _bounded(raw.get("phase", ""), 128) or module,
        "text": message,
        "sourceFile": "",
        "reportedFile": "",
        "line": 0,
        "column": 0,
        "module": module,
        "context": context,
        "processRole": role,
        "processId": process_id,
    }


def _load_generic_console_entries():
    """Read the newest strict journals within one global byte/event budget."""
    result = {"entries": [], "errors": [], "watchedPaths": []}
    configured = default_console_journal_directory()
    if not configured:
        return result
    root = os.path.abspath(str(configured))
    if not os.path.isdir(root):
        return result
    if _is_reparse(root):
        result["errors"].append("ReplayLab console journal directory is redirected")
        return result
    try:
        # The UI may be the only long-running reader while a host-only producer
        # appends events. Run the same generic-spool retention at most once per
        # minute; this never touches workspace, authorisation, or safety audits.
        _maybe_enforce_console_journal_retention(root)
        candidates = _journal_candidates(root)
        candidates.sort(
            key=lambda item: (item["mtime"], item["name"]), reverse=True)
        remaining_bytes = MAX_JOURNAL_READ_BYTES
        for item in candidates[:MAX_JOURNAL_READ_FILES]:
            if (remaining_bytes <= 0 or
                    len(result["entries"]) >= MAX_CONSOLE_ENTRIES):
                break
            name = item["name"]
            path = item["path"]
            match = item["match"]
            read_budget = min(
                MAX_JOURNAL_READ_BYTES_PER_FILE, remaining_bytes,
                max(item["size"], 1))
            try:
                raw = _read_file_tail_bytes(
                    path, MAX_JOURNAL_BYTES, read_budget,
                    "console event journal")
                remaining_bytes -= min(item["size"], read_budget)
                lines = raw.splitlines()
                remaining_events = MAX_CONSOLE_ENTRIES - len(result["entries"])
                if len(lines) > remaining_events:
                    lines = lines[-remaining_events:]
                previous = 0
                for index, line in enumerate(lines):
                    if not line.strip():
                        continue
                    if len(line) > MAX_JOURNAL_LINE_BYTES:
                        raise ShaderConsoleError("console journal line exceeds its bounded size")
                    try:
                        value = json.loads(line.decode("utf-8", errors="strict"))
                        event = _validate_journal_event(value, match, previous)
                    except (UnicodeError, ValueError, TypeError) as exc:
                        # A producer may be interrupted during its final append.
                        # Ignore only that incomplete tail; earlier evidence is
                        # immutable and remains visible.
                        if index == len(lines) - 1 and not raw.endswith(b"\n"):
                            break
                        raise ShaderConsoleError(
                            "console journal JSON is invalid: %s" % _bounded(exc, 512))
                    previous = event["sequence"]
                    result["entries"].append(event)
                result["watchedPaths"].append(path)
            except ShaderConsoleError as exc:
                result["errors"].append("%s: %s" % (name, _bounded(exc, 512)))
        result["entries"].sort(key=lambda item: (
            item.get("unixMs", 0), item.get("streamKey", ""),
            item.get("sequence", 0)))
        result["entries"] = result["entries"][-MAX_CONSOLE_ENTRIES:]
        result["watchedPaths"].insert(0, root)
    except (OSError, ShaderConsoleError) as exc:
        result["errors"].append(
            "ReplayLab console journal is unavailable: %s" % _bounded(exc, 512))
    return result


def console_severity_counts(entries):
    counts = {"info": 0, "warning": 0, "error": 0}
    for entry in entries or []:
        severity = normalise_console_severity(
            entry.get("severity", "info") if isinstance(entry, dict) else "info")
        counts[severity] += 1
    return counts


def build_console_clear_cursors(entries):
    """Build a view-only high-water mark; original audit files stay intact."""
    cursors = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        stream = str(entry.get("streamKey", "") or "")
        sequence = _positive_integer(entry.get("cursorSequence"), 0,
                                     0x7FFFFFFFFFFFFFFF)
        if stream and sequence > cursors.get(stream, 0):
            cursors[stream] = sequence
    return cursors


def console_entries_after_clear(entries, cursors):
    cursor_values = cursors if isinstance(cursors, dict) else {}
    visible = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        stream = str(entry.get("streamKey", "") or "")
        sequence = _positive_integer(entry.get("cursorSequence"), 0,
                                     0x7FFFFFFFFFFFFFFF)
        if not stream or sequence > _positive_integer(
                cursor_values.get(stream), 0, 0x7FFFFFFFFFFFFFFF):
            visible.append(entry)
    return visible


def filter_console_entries(entries, enabled=None):
    values = enabled if isinstance(enabled, dict) else {
        "info": True, "warning": True, "error": True}
    return [entry for entry in entries or [] if values.get(
        normalise_console_severity(entry.get("severity", "info")), False)]


def _append_snapshot_error_entries(result):
    for message in result.get("errors", []):
        text_value = _bounded(message, 4096)
        digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        stream = "console-loader:%s" % digest
        result["entries"].append({
            "id": "%s:1" % stream,
            "origin": "console",
            "streamKey": stream,
            "sequence": 1,
            "cursorSequence": 1,
            "at": _utc_now_text(result.get("generatedUnixMs")),
            "unixMs": result.get("generatedUnixMs", 0),
            "eventType": "consoleSourceFailed",
            "severity": "error",
            "phase": "控制台",
            "text": text_value,
            "sourceFile": "",
            "reportedFile": "",
            "line": 0,
            "column": 0,
            "module": "system",
        })


def derive_console_status(watch=None, live=None):
    """Return the current headline/progress from the latest fixed audit state."""
    watch = watch if isinstance(watch, dict) else {}
    live = live if isinstance(live, dict) else {}
    if live.get("state") == "failed":
        return {"text": "实时更新会话失败", "phase": "错误", "percent": 100,
                "busy": False, "severity": "error"}
    if watch.get("compileInProgress"):
        return {"text": "正在编译 Shader", "phase": "编译", "percent": 45,
                "busy": True, "severity": "progress"}
    if watch.get("pending"):
        return {"text": "已检测到保存，等待源码稳定", "phase": "检测", "percent": 20,
                "busy": True, "severity": "progress"}
    combined = []
    for origin, audit in (("watch", watch), ("live", live)):
        for event in audit.get("events", []):
            combined.append((event.get("at", ""), origin, event))
    combined.sort(key=lambda item: item[0])
    latest = combined[-1][2] if combined else {}
    event_type = latest.get("type", "")
    if event_type == "applyStarted":
        return {"text": "正在验证 ABI 并应用 Shader", "phase": "ABI / 应用",
                "percent": 75, "busy": True, "severity": "progress"}
    if event_type in ("applyQueued", "applySuperseded"):
        return {"text": "Shader 已排队，等待实时应用", "phase": "应用队列",
                "percent": 68, "busy": True, "severity": "progress"}
    if _is_superseded_apply_failure(latest):
        revision = _positive_integer(latest.get("revision"), 0)
        return {"text": "较旧 Shader%s 已由新的保存取代" % (
                    " revision %d" % revision if revision else ""),
                "phase": "应用队列", "percent": 68, "busy": False,
                "severity": "warning"}
    if event_type in ("compileFailed", "applyRejected", "applyFailed", "liveEditFailed"):
        return {"text": latest.get("message") or "Shader 更新失败", "phase": "错误",
                "percent": 100, "busy": False, "severity": "error"}
    if event_type == "compileSucceeded":
        return {"text": "编译成功，等待 ABI 验证和应用", "phase": "编译",
                "percent": 65, "busy": True, "severity": "progress"}
    if event_type in ("applySucceeded", "applyReused") or live.get("activeRevision", 0) > 0:
        # A byte-identical save records applyReused for the new revision while
        # intentionally preserving the original active replacement object.
        # For a success event prefer that event, then the live manager's
        # handled revision.  For unrelated later events retain the actual
        # replacement revision so a failed handled revision is never labelled
        # as successfully active.
        if event_type in ("applySucceeded", "applyReused"):
            revision = _positive_integer(latest.get("revision"), 0)
            if not revision:
                revision = live.get("latestHandledRevision", 0)
            if not revision:
                revision = live.get("activeRevision", 0)
        else:
            revision = live.get("activeRevision", 0)
        return {"text": "Shader 已实时更新%s" % (
                    " · revision %d" % revision if revision else ""),
                "phase": "完成", "percent": 100, "busy": False,
                "severity": "success"}
    if live.get("state") == "running":
        return {"text": "实时监听中", "phase": "监听", "percent": 0,
                "busy": False, "severity": "success"}
    return {"text": "等待持久实时编辑会话", "phase": "空闲", "percent": 0,
            "busy": False, "severity": "info"}


def _workspace_model(authorization):
    workspace = authorization["workspacePath"]
    source_path, compiler_diagnostics, manifest_path = _validate_manifest(
        workspace, authorization)
    organization_gate_path = os.path.join(
        workspace, "ai", "organize-code-gate.json")
    editor_eligible = False
    editor_eligibility = "non-hlsl-source"
    if HLSL_SOURCE_EXTENSION_RE.search(source_path) is not None:
        try:
            _validate_organization_gate(workspace, authorization, source_path)
            editor_eligible = True
            editor_eligibility = "organized-executable-editable-hlsl"
        except ShaderConsoleError:
            editor_eligibility = "organization-gate-unavailable"
    live = None
    watch = None
    live_path = _latest_token_json(
        os.path.join(workspace, "live-edit"), authorization["lastLiveEditId"])
    if live_path:
        live_value = _read_json(live_path, MAX_AUDIT_BYTES, "shader live-edit audit")
        live = _validate_live(
            live_value, workspace, authorization["lastLiveEditId"])
    watch_id = live.get("watchId", "") if isinstance(live, dict) else ""
    watch_path = _latest_token_json(os.path.join(workspace, "watch"), watch_id)
    if watch_path:
        watch_value = _read_json(watch_path, MAX_AUDIT_BYTES, "shader watcher audit")
        watch = _validate_watch(watch_value, workspace, source_path, watch_id)
    entries = console_entries_from_audits(
        workspace, source_path, watch=watch, live=live,
        compiler_diagnostics=compiler_diagnostics)
    workspace_key = _normal_path(workspace)
    for entry in entries:
        entry["workspaceKey"] = workspace_key
    return {
        "authorizationId": authorization["authorizationId"],
        "workspaceId": authorization["workspaceId"],
        "workspacePath": workspace,
        "sourcePath": source_path,
        "captureId": authorization["captureId"],
        "stage": authorization["stage"],
        "entryPoint": authorization["entryPoint"],
        "resourceId": authorization["resourceId"],
        "updatedAt": authorization["updatedAt"],
        "editorEligible": editor_eligible,
        "editorEligibility": editor_eligibility,
        "autoApplyReady": bool(
            isinstance(live, dict) and live.get("state") == "running" and
            isinstance(watch, dict) and watch.get("state") == "running"),
        "watch": watch,
        "liveEdit": live,
        "entries": entries,
        "status": derive_console_status(watch=watch, live=live),
        "watchedPaths": [path for path in (
            source_path, manifest_path, organization_gate_path,
            os.path.dirname(organization_gate_path),
            live_path, watch_path, os.path.dirname(live_path) if live_path else "",
            os.path.dirname(watch_path) if watch_path else "") if path],
    }


def load_console_snapshot(registry_path=None):
    """Load the fixed journal plus authorised shader workspace projections."""
    path = os.path.abspath(str(registry_path or default_registry_path() or ""))
    generic = _load_generic_console_entries()
    result = {
        "schema": CONSOLE_SCHEMA,
        "registryPath": path,
        "generatedUnixMs": int(time.time() * 1000),
        "workspaces": [],
        "entries": list(generic["entries"]),
        "errors": list(generic["errors"]),
        "watchedPaths": list(generic["watchedPaths"]),
    }
    if not path or not os.path.isfile(path):
        _append_snapshot_error_entries(result)
        result["counts"] = console_severity_counts(result["entries"])
        return result
    try:
        registry = _read_json(path, MAX_REGISTRY_BYTES, "live-edit authorization registry")
        if registry.get("schema") != REGISTRY_SCHEMA or registry.get("schemaVersion") != 1:
            raise ShaderConsoleError("live-edit authorization registry schema is invalid")
        entries = registry.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_AUTHORIZATIONS:
            raise ShaderConsoleError("live-edit authorization registry entries are invalid")
        seen = set()
        for raw in entries:
            try:
                authorization = _validate_authorization(raw)
                key = _normal_path(authorization["workspacePath"])
                if key in seen:
                    raise ShaderConsoleError("duplicate authorized shader workspace")
                seen.add(key)
                result["workspaces"].append(_workspace_model(authorization))
            except ShaderConsoleError as exc:
                result["errors"].append(_bounded(exc, 1024))
        result["workspaces"].sort(
            key=lambda item: (item.get("updatedAt", ""), item["workspacePath"]),
            reverse=True)
        result["watchedPaths"] = [path, os.path.dirname(path)]
        for workspace in result["workspaces"]:
            result["watchedPaths"].extend(workspace.get("watchedPaths", []))
            result["entries"].extend(workspace.get("entries", []))
    except ShaderConsoleError as exc:
        result["errors"].append(_bounded(exc, 1024))
    # De-duplicate paths while retaining order. They are used only as
    # QFileSystemWatcher inputs, never as executable inputs.
    unique = []
    known = set()
    for item in result["watchedPaths"]:
        key = _normal_path(item)
        if key not in known and os.path.exists(item):
            known.add(key)
            unique.append(item)
    result["watchedPaths"] = unique
    _append_snapshot_error_entries(result)
    result["entries"].sort(key=lambda item: (
        item.get("at", ""), item.get("unixMs", 0), item.get("streamKey", ""),
        item.get("sequence", 0)))
    result["entries"] = result["entries"][-MAX_CONSOLE_ENTRIES:]
    result["counts"] = console_severity_counts(result["entries"])
    return result


if HAS_QT:
    class SourceLocationDialog(QtWidgets.QDialog):
        """Read-only exact-line source view; never writes or launches a command."""

        def __init__(self, source_path, line, column, parent=None):
            super(SourceLocationDialog, self).__init__(parent)
            self.setWindowTitle("ReplayLab Shader Source")
            self.resize(1000, 720)
            layout = QtWidgets.QVBoxLayout(self)
            location = "%s:%d:%d" % (source_path, max(line, 1), max(column, 1))
            label = QtWidgets.QLabel(location, self)
            label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(label)
            self.editor = QtWidgets.QPlainTextEdit(self)
            self.editor.setReadOnly(True)
            self.editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
            self.editor.setFont(font)
            layout.addWidget(self.editor, 1)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Close, parent=self)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            raw = _read_file_bytes(
                source_path, MAX_SOURCE_BYTES, "workspace shader source")
            self.editor.setPlainText(raw.decode("utf-8", errors="replace"))
            block = self.editor.document().findBlockByNumber(max(line, 1) - 1)
            cursor = QtGui.QTextCursor(block)
            if column > 1:
                cursor.movePosition(
                    QtGui.QTextCursor.Right, QtGui.QTextCursor.MoveAnchor,
                    max(column - 1, 0))
            self.editor.setTextCursor(cursor)
            self.editor.centerCursor()
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.cursor = QtGui.QTextCursor(block)
            selection.cursor.clearSelection()
            selection.format.setProperty(
                QtGui.QTextFormat.FullWidthSelection, True)
            selection.format.setBackground(QtGui.QColor(120, 20, 20, 125))
            selection.format.setForeground(QtGui.QColor(255, 230, 230))
            self.editor.setExtraSelections([selection])


    class ReplayLabConsolePanel(object):
        """A dockable, continuously refreshed general ReplayLab console."""

        POLL_INTERVAL_MS = 100
        FILESYSTEM_COALESCE_MS = 20

        def __init__(self, ctx, qrd_module, registry_path=None, closed_callback=None):
            self.ctx = ctx
            self.qrd = qrd_module
            self.registry_path = registry_path or default_registry_path()
            self.closed_callback = closed_callback
            self.top = QtWidgets.QWidget(ctx.GetMainWindow().Widget())
            self.top.setWindowTitle("ReplayLab Console")
            self.top.destroyed.connect(self._closed)
            root = QtWidgets.QVBoxLayout(self.top)
            root.setContentsMargins(5, 5, 5, 5)
            header = QtWidgets.QHBoxLayout()
            self.workspace_combo = QtWidgets.QComboBox(self.top)
            self.workspace_combo.currentIndexChanged.connect(self._render_selected)
            header.addWidget(self.workspace_combo, 1)
            self.refresh_button = QtWidgets.QPushButton("立即刷新", self.top)
            self.refresh_button.clicked.connect(self.refresh)
            header.addWidget(self.refresh_button)
            root.addLayout(header)
            filters = QtWidgets.QHBoxLayout()
            self.info_filter = QtWidgets.QToolButton(self.top)
            self.warning_filter = QtWidgets.QToolButton(self.top)
            self.error_filter = QtWidgets.QToolButton(self.top)
            self._filter_buttons = {
                "info": self.info_filter,
                "warning": self.warning_filter,
                "error": self.error_filter,
            }
            for button in self._filter_buttons.values():
                button.setCheckable(True)
                button.setChecked(True)
                button.toggled.connect(self._render_selected)
                filters.addWidget(button)
            filters.addStretch(1)
            self.clear_button = QtWidgets.QPushButton("清理所有消息", self.top)
            self.clear_button.setToolTip(
                "只清理当前控制台视图；不会删除 ReplayLab 审计、授权或原始日志")
            self.clear_button.clicked.connect(self._clear_messages)
            filters.addWidget(self.clear_button)
            root.addLayout(filters)
            self.status_label = QtWidgets.QLabel("等待 ReplayLab 消息", self.top)
            self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            root.addWidget(self.status_label)
            self.progress = QtWidgets.QProgressBar(self.top)
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            root.addWidget(self.progress)
            self.tree = QtWidgets.QTreeWidget(self.top)
            self.tree.setHeaderLabels(("时间", "模块", "阶段", "信息", "位置"))
            self.tree.setRootIsDecorated(False)
            self.tree.setAlternatingRowColors(True)
            self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.tree.itemSelectionChanged.connect(self._selection_changed)
            self.tree.itemDoubleClicked.connect(self._item_open)
            self.tree.header().setStretchLastSection(False)
            self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
            root.addWidget(self.tree, 3)
            self.detail = QtWidgets.QTextEdit(self.top)
            self.detail.setReadOnly(True)
            self.detail.setMaximumHeight(130)
            root.addWidget(self.detail)
            actions = QtWidgets.QHBoxLayout()
            self.open_button = QtWidgets.QPushButton("定位到源码", self.top)
            self.open_button.setEnabled(False)
            self.open_button.clicked.connect(self._open_selected)
            actions.addWidget(self.open_button)
            actions.addStretch(1)
            root.addLayout(actions)
            self.file_watcher = QtCore.QFileSystemWatcher(self.top)
            self.file_watcher.fileChanged.connect(self._filesystem_changed)
            self.file_watcher.directoryChanged.connect(self._filesystem_changed)
            self.change_timer = QtCore.QTimer(self.top)
            self.change_timer.setSingleShot(True)
            try:
                self.change_timer.setTimerType(QtCore.Qt.PreciseTimer)
            except Exception:
                pass
            self.change_timer.setInterval(self.FILESYSTEM_COALESCE_MS)
            self.change_timer.timeout.connect(self._flush_filesystem_changes)
            self.timer = QtCore.QTimer(self.top)
            try:
                self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            except Exception:
                pass
            self.timer.setInterval(self.POLL_INTERVAL_MS)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()
            self._docked = False
            self._alive = True
            self._snapshot = None
            self._selected_workspace_key = "__all__"
            self._clear_cursors = {}
            self._source_dialogs = []
            self._watched_paths = [self.registry_path]
            self._last_disk_signature = None
            self.refresh(True)

        def _closed(self, _object=None):
            if not self._alive:
                return
            self._alive = False
            try:
                self.timer.stop()
            except Exception:
                pass
            try:
                self.change_timer.stop()
            except Exception:
                pass
            try:
                self.file_watcher.removePaths(self.file_watcher.files())
                self.file_watcher.removePaths(self.file_watcher.directories())
            except Exception:
                pass
            callback = self.closed_callback
            self.closed_callback = None
            self._source_dialogs = []
            self.top = None
            if callback:
                callback()

        def show(self):
            if not self._alive or self.top is None:
                raise ShaderConsoleError("ReplayLab Console has already been closed")
            if not self._docked:
                reference = None
                dock_reference = self.qrd.DockReference.MainToolArea
                try:
                    if self.ctx.HasPipelineViewer():
                        reference = self.ctx.GetPipelineViewer().Widget()
                        dock_reference = self.qrd.DockReference.BottomOf
                except Exception:
                    reference = None
                    dock_reference = self.qrd.DockReference.MainToolArea
                self.ctx.AddDockWindow(self.top, dock_reference, reference, 0.28)
                self._docked = True
            self.ctx.RaiseDockWindow(self.top)

        def shutdown(self):
            if not self._alive:
                return
            try:
                self.timer.stop()
            except Exception:
                pass
            for dialog in list(self._source_dialogs):
                try:
                    dialog.close()
                except Exception:
                    pass
            self._source_dialogs = []
            widget = self.top
            self._closed()
            if widget is not None:
                try:
                    widget.close()
                    widget.deleteLater()
                except Exception:
                    pass

        def _filesystem_changed(self, _path):
            # Atomic audit replacement can remove a QFileSystemWatcher file
            # subscription. Coalesce a save's file+directory burst into one
            # short deferred refresh and then rebuild every subscription.
            if not self.change_timer.isActive():
                self.change_timer.start(self.FILESYSTEM_COALESCE_MS)

        def _flush_filesystem_changes(self):
            self.refresh(True)

        def _disk_signature(self):
            values = []
            for path in self._watched_paths:
                try:
                    info = os.stat(path)
                    values.append((
                        _normal_path(path), getattr(info, "st_mtime_ns", 0),
                        info.st_size, info.st_mode))
                except OSError:
                    values.append((_normal_path(path), -1, -1, -1))
            return tuple(values)

        def _reset_watched_paths(self, paths):
            current = self.file_watcher.files() + self.file_watcher.directories()
            if current:
                try:
                    self.file_watcher.removePaths(current)
                except Exception:
                    pass
            files = []
            directories = []
            for path in paths:
                if os.path.isfile(path):
                    files.append(path)
                elif os.path.isdir(path):
                    directories.append(path)
            if files:
                self.file_watcher.addPaths(files)
            if directories:
                self.file_watcher.addPaths(directories)

        def refresh(self, force=False):
            if not self._alive:
                return
            before = self._disk_signature()
            if not force and before == self._last_disk_signature:
                return
            snapshot = load_console_snapshot(self.registry_path)
            previous_key = self._selected_workspace_key
            self._snapshot = snapshot
            self.workspace_combo.blockSignals(True)
            self.workspace_combo.clear()
            selected_index = 0
            self.workspace_combo.addItem("全部 ReplayLab 消息", "__all__")
            for index, workspace in enumerate(snapshot["workspaces"]):
                key = _normal_path(workspace["workspacePath"])
                label = "%s %s · Shader %d · %s" % (
                    workspace["stage"], workspace["entryPoint"],
                    workspace["resourceId"], os.path.basename(workspace["workspacePath"]))
                self.workspace_combo.addItem(label, key)
                if key == previous_key:
                    selected_index = index + 1
            self.workspace_combo.setCurrentIndex(selected_index)
            self._selected_workspace_key = str(
                self.workspace_combo.itemData(selected_index) or "__all__")
            self.workspace_combo.blockSignals(False)
            self._watched_paths = snapshot.get("watchedPaths", []) or [self.registry_path]
            self._reset_watched_paths(self._watched_paths)
            self._last_disk_signature = self._disk_signature()
            self._render_selected()

        def _selected_workspace(self):
            if not self._snapshot:
                return None
            index = self.workspace_combo.currentIndex()
            values = self._snapshot.get("workspaces", [])
            if index <= 0 or index - 1 >= len(values):
                return None
            return values[index - 1]

        def _scope_entries(self):
            if not self._snapshot:
                return []
            workspace = self._selected_workspace()
            if workspace is None:
                return self._snapshot.get("entries", [])
            return workspace.get("entries", [])

        def _clear_messages(self, _checked=False):
            if not self._snapshot:
                return
            # Unity-style Clear is deliberately a view watermark. Original
            # security audits, authorisations and append-only journals remain.
            all_entries = list(self._snapshot.get("entries", []))
            for workspace in self._snapshot.get("workspaces", []):
                all_entries.extend(workspace.get("entries", []))
            latest = build_console_clear_cursors(all_entries)
            for stream, sequence in latest.items():
                if sequence > self._clear_cursors.get(stream, 0):
                    self._clear_cursors[stream] = sequence
            self._render_selected()

        def _enabled_severities(self):
            return dict((severity, button.isChecked())
                        for severity, button in self._filter_buttons.items())

        def _set_filter_counts(self, counts):
            self.info_filter.setText("普通 (%d)" % counts.get("info", 0))
            self.warning_filter.setText("警告 (%d)" % counts.get("warning", 0))
            self.error_filter.setText("错误 (%d)" % counts.get("error", 0))
            self.warning_filter.setStyleSheet("color: #e6b84a;")
            self.error_filter.setStyleSheet("color: #ff5f5f;")

        def _render_selected(self, _index=None):
            if not self._alive:
                return
            workspace = self._selected_workspace()
            self.tree.clear()
            self.detail.clear()
            self.open_button.setEnabled(False)
            entries = console_entries_after_clear(
                self._scope_entries(), self._clear_cursors)
            counts = console_severity_counts(entries)
            self._set_filter_counts(counts)
            visible = filter_console_entries(entries, self._enabled_severities())
            if workspace is None:
                self._selected_workspace_key = "__all__"
                errors = (self._snapshot or {}).get("errors", [])
                if errors:
                    self.status_label.setText(
                        "ReplayLab Console · %s" % errors[0])
                    self.status_label.setStyleSheet("color: #ff6666;")
                else:
                    self.status_label.setText(
                        "ReplayLab Console · 普通 %d · 警告 %d · 错误 %d" % (
                            counts["info"], counts["warning"], counts["error"]))
                    self.status_label.setStyleSheet("")
                self.progress.setValue(0)
            else:
                self._selected_workspace_key = _normal_path(workspace["workspacePath"])
                status = workspace["status"]
                self.status_label.setText("%s · %s" % (status["phase"], status["text"]))
                color = {
                    "error": "#ff6666", "success": "#79d279",
                    "progress": "#66b3ff", "warning": "#ffcc66",
                }.get(status["severity"], "")
                self.status_label.setStyleSheet(
                    "color: %s; font-weight: 600;" % color if color else "")
                self.progress.setValue(status["percent"])
            severity_colors = {
                "error": QtGui.QColor("#ff5f5f"),
                "warning": QtGui.QColor("#e6b84a"),
            }
            for entry in visible:
                location = ""
                if entry.get("sourceFile") and entry.get("line"):
                    location = "%s:%d%s" % (
                        os.path.basename(entry["sourceFile"]), entry["line"],
                        ":%d" % entry["column"] if entry.get("column") else "")
                item = QtWidgets.QTreeWidgetItem((
                    entry.get("at", "").replace("T", " ").replace("Z", ""),
                    entry.get("module", "system"), entry.get("phase", ""),
                    entry.get("text", ""), location))
                item.setData(0, QtCore.Qt.UserRole, entry)
                item.setToolTip(3, entry.get("text", ""))
                brush_color = severity_colors.get(entry.get("severity"))
                if brush_color is not None:
                    brush = QtGui.QBrush(brush_color)
                    for column in range(5):
                        item.setForeground(column, brush)
                self.tree.addTopLevelItem(item)
            if self.tree.topLevelItemCount():
                latest = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
                self.tree.setCurrentItem(latest)
                self.tree.scrollToItem(latest)

        def _selection_changed(self):
            items = self.tree.selectedItems()
            if not items:
                self.open_button.setEnabled(False)
                self.detail.clear()
                return
            entry = items[0].data(0, QtCore.Qt.UserRole)
            if not isinstance(entry, dict):
                return
            location = ""
            if entry.get("sourceFile") and entry.get("line"):
                location = "\n%s:%d:%d" % (
                    entry["sourceFile"], entry["line"],
                    entry.get("column") or 1)
            self.detail.setPlainText("[%s/%s/%s] %s%s" % (
                entry.get("severity", "info"), entry.get("module", "system"),
                entry.get("phase", ""), entry.get("text", ""), location))
            self.open_button.setEnabled(bool(
                entry.get("sourceFile") and entry.get("line")))

        def _item_open(self, item, _column):
            entry = item.data(0, QtCore.Qt.UserRole)
            self._open_location(entry)

        def _open_selected(self):
            items = self.tree.selectedItems()
            if items:
                self._open_location(items[0].data(0, QtCore.Qt.UserRole))

        def _workspace_for_entry(self, entry):
            key = str(entry.get("workspaceKey", "") or "")
            if not key:
                return None
            for workspace in (self._snapshot or {}).get("workspaces", []):
                if _normal_path(workspace.get("workspacePath", "")) == key:
                    return workspace
            return None

        def _open_location(self, entry):
            if not isinstance(entry, dict):
                return
            workspace = self._workspace_for_entry(entry)
            if (not workspace or not entry.get("sourceFile") or
                    not entry.get("line")):
                return
            source_path = entry["sourceFile"]
            # Re-check the fixed authorization boundary at click time. Compiler
            # text can never redirect this button to another file.
            source_root = os.path.join(workspace["workspacePath"], "source")
            if (not _within(source_root, source_path) or not os.path.isfile(source_path)
                    or _is_reparse(source_path)):
                QtWidgets.QMessageBox.critical(
                    self.top, "ReplayLab Console",
                    "源码路径已改变或离开授权工作区，已拒绝打开。")
                return
            try:
                dialog = SourceLocationDialog(
                    source_path, entry.get("line") or 1,
                    entry.get("column") or 1, self.top)
                dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
                dialog.destroyed.connect(
                    lambda _object=None, selected=dialog: self._forget_dialog(selected))
                self._source_dialogs.append(dialog)
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            except Exception as exc:
                QtWidgets.QMessageBox.critical(
                    self.top, "ReplayLab Console", _bounded(exc, 2048))

        def _forget_dialog(self, dialog):
            try:
                self._source_dialogs.remove(dialog)
            except ValueError:
                pass

    # Compatibility surface for older installed bridge registrations.
    ShaderConsolePanel = ReplayLabConsolePanel

else:
    class ReplayLabConsolePanel(object):  # pragma: no cover - defensive import surface
        def __init__(self, *args, **kwargs):
            raise ShaderConsoleError("PySide2 is unavailable for ReplayLab Console")

    ShaderConsolePanel = ReplayLabConsolePanel
