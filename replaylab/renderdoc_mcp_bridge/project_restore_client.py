"""Bounded loopback client for ReplayLab's persistent project-restore jobs.

This module runs in qrenderdoc's embedded Python 3.6.  It deliberately talks
only to the singleton host state published under ReplayLab's local runtime
directory, reconstructs the loopback URL from the validated port, and obtains
the bearer secret from the separate owner-readable token file.
"""

from __future__ import absolute_import, print_function

import json
import os
import re
import tempfile
import time

try:
    from urllib.error import HTTPError, URLError
    from urllib.parse import quote
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover - Python 3 is required by qrenderdoc.
    raise


RESTORE_JOB_SCHEMA = "replaylab-project-restore-job/v1"
RESTORE_JOB_PATH = "/replaylab/project-restore"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 1024
MAX_PROGRESS_EVENTS = 256
MAX_PROGRESS_COUNT = 1000000000
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ReplayLabRestoreClientError(RuntimeError):
    pass


def runtime_root(environment=None):
    """Resolve the exact directory used by ``replaylab_host.runtime_root``."""

    env = os.environ if environment is None else environment
    configured = str(env.get("REPLAYLAB_RUNTIME_DIR", "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return os.path.abspath(os.path.join(
            os.path.expanduser(local), "ReplayLab", "runtime"))
    getuid = getattr(os, "getuid", None)
    suffix = str(getuid() if callable(getuid) else os.getpid())
    return os.path.abspath(os.path.join(
        tempfile.gettempdir(), "ReplayLab-" + suffix))


def _read_bounded(path, maximum):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ReplayLabRestoreClientError("ReplayLab host state is unavailable")
    try:
        size = int(os.path.getsize(path))
    except (OSError, TypeError, ValueError):
        raise ReplayLabRestoreClientError("ReplayLab host state is unavailable")
    if size <= 0 or size > maximum:
        raise ReplayLabRestoreClientError("ReplayLab host state is invalid")
    try:
        with open(path, "rb") as stream:
            value = stream.read(maximum + 1)
    except OSError as exc:
        raise ReplayLabRestoreClientError(
            "ReplayLab host state could not be read: %s" % str(exc))
    if not value or len(value) > maximum:
        raise ReplayLabRestoreClientError("ReplayLab host state is invalid")
    return value


def _strict_json_bytes(payload, label):
    def _reject_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def _reject_constant(value):
        raise ValueError("non-finite number: %s" % value)

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ReplayLabRestoreClientError(
            "%s returned invalid JSON: %s" % (label, str(exc)))
    if not isinstance(value, dict):
        raise ReplayLabRestoreClientError("%s returned a non-object" % label)
    return value


def _clean_path(value, label, require_rdc=False):
    if not isinstance(value, str):
        raise ReplayLabRestoreClientError("%s must be text" % label)
    text = value.strip()
    if not text or len(text) > 32768 or "\x00" in text:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    path = os.path.abspath(os.path.expanduser(text))
    if require_rdc and not path.lower().endswith(".rdc"):
        raise ReplayLabRestoreClientError("capture path must reference an RDC file")
    return path


def _normalise_progress(value, previous_sequence):
    if not isinstance(value, dict):
        raise ReplayLabRestoreClientError("ReplayLab restore progress is malformed")
    try:
        sequence = int(value.get("sequence"))
        current = int(value.get("current", 0))
        total = int(value.get("total", 0))
    except (TypeError, ValueError, OverflowError):
        raise ReplayLabRestoreClientError("ReplayLab restore progress is malformed")
    phase = str(value.get("phase", "") or "").strip()
    message = str(value.get("message", "") or "").strip()
    if (sequence <= previous_sequence or sequence > 0x7FFFFFFF or
            current < 0 or total < 0 or
            current > MAX_PROGRESS_COUNT or total > MAX_PROGRESS_COUNT or
            (not phase and not message) or len(phase) > 128 or
            len(message) > 1024 or "\x00" in phase or "\x00" in message):
        raise ReplayLabRestoreClientError("ReplayLab restore progress is malformed")
    if total:
        current = min(current, total)
    else:
        current = 0
    return {
        "sequence": sequence,
        "phase": phase,
        "message": message,
        "current": current,
        "total": total,
    }


class ReplayLabRestoreHostClient(object):
    """Start and poll one authenticated restore job on the persistent host."""

    def __init__(self, runtime_directory=None, timeout_seconds=1800,
                 startup_timeout_seconds=15, poll_seconds=0.1,
                 opener=None, sleep=None, monotonic=None):
        self.runtime_directory = os.path.abspath(
            runtime_directory or runtime_root())
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.startup_timeout_seconds = max(
            0.1, float(startup_timeout_seconds))
        self.poll_seconds = min(1.0, max(0.01, float(poll_seconds)))
        self.opener = opener or urlopen
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic

    def _credentials(self, deadline):
        state_path = os.path.join(self.runtime_directory, "host.json")
        token_path = os.path.join(self.runtime_directory, "host.token")
        last_error = None
        while self.monotonic() < deadline:
            try:
                state = _strict_json_bytes(
                    _read_bounded(state_path, MAX_STATE_BYTES),
                    "ReplayLab host state")
                if (state.get("schema") != "replaylab-host/v1" or
                        state.get("service") != "ReplayLab" or
                        str(state.get("host", "") or "") != "127.0.0.1"):
                    raise ReplayLabRestoreClientError(
                        "ReplayLab host state has an invalid identity")
                try:
                    port = int(state.get("port"))
                except (TypeError, ValueError, OverflowError):
                    raise ReplayLabRestoreClientError(
                        "ReplayLab host state has an invalid port")
                if port < 1024 or port > 65535:
                    raise ReplayLabRestoreClientError(
                        "ReplayLab host state has an invalid port")
                published_token = str(state.get("tokenFile", "") or "").strip()
                if (published_token and
                        os.path.normcase(os.path.abspath(published_token)) !=
                        os.path.normcase(os.path.abspath(token_path))):
                    raise ReplayLabRestoreClientError(
                        "ReplayLab host token path is inconsistent")
                token = _read_bounded(token_path, MAX_TOKEN_BYTES).decode(
                    "ascii").strip()
                if TOKEN_PATTERN.fullmatch(token) is None:
                    raise ReplayLabRestoreClientError(
                        "ReplayLab host token is invalid")
                return port, token
            except (ReplayLabRestoreClientError, UnicodeDecodeError) as exc:
                last_error = exc
                self.sleep(min(self.poll_seconds, 0.1))
        raise ReplayLabRestoreClientError(
            "ReplayLab persistent host did not become ready: %s" %
            str(last_error or "host state is unavailable"))

    def _request(self, port, token, method, path, payload=None):
        body = None
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_STATE_BYTES:
                raise ReplayLabRestoreClientError(
                    "ReplayLab restore request is oversized")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            "http://127.0.0.1:%d%s" % (int(port), path),
            data=body, headers=headers, method=method)
        try:
            response = self.opener(request, timeout=15.0)
            try:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            try:
                detail = exc.read(4097).decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise ReplayLabRestoreClientError(
                "ReplayLab host rejected the restore request (HTTP %s): %s" %
                (getattr(exc, "code", "error"), detail[:4096]))
        except (URLError, OSError) as exc:
            raise ReplayLabRestoreClientError(
                "ReplayLab host connection failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned an invalid response size")
        return _strict_json_bytes(response_body, "ReplayLab host")

    @staticmethod
    def _snapshot(value, expected_job_id=None):
        if not isinstance(value, dict) or value.get("schema") != RESTORE_JOB_SCHEMA:
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned an invalid restore job")
        job_id = str(value.get("jobId", "") or "").strip().lower()
        status = str(value.get("status", "") or "").strip().lower()
        if (JOB_ID_PATTERN.fullmatch(job_id) is None or
                status not in ("queued", "running", "completed", "failed") or
                (expected_job_id is not None and job_id != expected_job_id)):
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned an invalid restore job")
        progress = value.get("progress", [])
        if not isinstance(progress, list) or len(progress) > MAX_PROGRESS_EVENTS:
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned invalid restore progress")
        try:
            latest_sequence = int(value.get("latestSequence", 0))
        except (TypeError, ValueError, OverflowError):
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned invalid restore progress")
        if (latest_sequence < 0 or latest_sequence > 0x7FFFFFFF or
                not isinstance(value.get("progressTruncated", False), bool)):
            raise ReplayLabRestoreClientError(
                "ReplayLab host returned invalid restore progress")
        return job_id, status, progress, latest_sequence

    def restore(self, project_root, capture_path, capture_session_id,
                progress_callback=None):
        project = _clean_path(project_root, "project root")
        capture = _clean_path(capture_path, "capture path", require_rdc=True)
        session = str(capture_session_id or "").strip().lower()
        if SESSION_PATTERN.fullmatch(session) is None:
            raise ReplayLabRestoreClientError(
                "capture session ID is invalid")
        if progress_callback is not None and not callable(progress_callback):
            raise ReplayLabRestoreClientError(
                "progress callback must be callable")

        startup_deadline = self.monotonic() + self.startup_timeout_seconds
        port, token = self._credentials(startup_deadline)
        started = self._request(port, token, "POST", RESTORE_JOB_PATH, {
            "projectRoot": project,
            "capturePath": capture,
            "captureSessionId": session,
        })
        job_id, unused_status, unused_progress, unused_latest = self._snapshot(
            started)
        del unused_status, unused_progress, unused_latest

        sequence = 0
        deadline = self.monotonic() + self.timeout_seconds
        while self.monotonic() < deadline:
            query = "%s/%s?after=%d" % (
                RESTORE_JOB_PATH, quote(job_id, safe=""), sequence)
            snapshot = self._request(port, token, "GET", query)
            current_job_id, status, events, latest_sequence = self._snapshot(
                snapshot, job_id)
            del current_job_id
            for raw_event in events:
                event = _normalise_progress(raw_event, sequence)
                sequence = event["sequence"]
                if progress_callback is not None:
                    try:
                        progress_callback({
                            "phase": event["phase"],
                            "message": event["message"],
                            "current": event["current"],
                            "total": event["total"],
                        })
                    except Exception:
                        # Progress is display-only and cannot cancel or weaken a
                        # physical restore transaction.
                        pass
            # A terminal snapshot can still contain only the next page from a
            # bounded progress ring. Drain every available newer page before
            # handing the final result to the dialog.
            if status in ("completed", "failed") and sequence < latest_sequence:
                continue
            if status == "completed":
                result = snapshot.get("result")
                if not isinstance(result, dict):
                    raise ReplayLabRestoreClientError(
                        "ReplayLab restore completed without a result")
                return result
            if status == "failed":
                error = str(snapshot.get("error", "") or "").strip()
                raise ReplayLabRestoreClientError(
                    error[:4096] or "ReplayLab project restore failed")
            self.sleep(self.poll_seconds)
        raise ReplayLabRestoreClientError(
            "ReplayLab project restore timed out")


__all__ = [
    "RESTORE_JOB_SCHEMA",
    "ReplayLabRestoreClientError",
    "ReplayLabRestoreHostClient",
    "runtime_root",
]
