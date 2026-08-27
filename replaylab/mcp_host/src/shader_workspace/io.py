"""Small bounded and atomic filesystem helpers used by shader workspaces."""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


# Windows editors, antivirus scanners and indexers can briefly keep a JSON
# destination (or its just-written temporary) open without FILE_SHARE_DELETE.
# Retrying only these well-known sharing/access failures keeps atomic writes
# robust without hiding a real ACL, redirection or disk failure.
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})
_ATOMIC_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)


def _is_transient_windows_file_error(error: BaseException) -> bool:
    if not isinstance(error, OSError):
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in _WINDOWS_TRANSIENT_FILE_ERRORS:
        return True
    return os.name == "nt" and winerror is None and getattr(error, "errno", None) in {
        errno.EACCES,
        errno.EBUSY,
    }


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(len(_ATOMIC_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if (
                not _is_transient_windows_file_error(exc)
                or attempt >= len(_ATOMIC_RETRY_DELAYS)
            ):
                raise
            time.sleep(_ATOMIC_RETRY_DELAYS[attempt])


def _cleanup_temporary_with_retry(temporary: Path) -> None:
    """Best-effort bounded cleanup which never masks the write exception."""

    for attempt in range(len(_ATOMIC_RETRY_DELAYS) + 1):
        try:
            temporary.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if (
                not _is_transient_windows_file_error(exc)
                or attempt >= len(_ATOMIC_RETRY_DELAYS)
            ):
                return
            time.sleep(_ATOMIC_RETRY_DELAYS[attempt])


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, path)
    finally:
        _cleanup_temporary_with_retry(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, payload)


def read_json_object(path: Path, maximum_bytes: int, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("%s is not a regular file: %s" % (description, path))
    before = path.stat()
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise ValueError(
            "%s size must be between 1 and %d bytes: %s"
            % (description, maximum_bytes, path)
        )
    try:
        raw = path.read_bytes()
        after = path.stat()
        if (
            len(raw) != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ValueError("%s changed while it was read: %s" % (description, path))
        text = raw.decode("utf-8-sig", errors="strict")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("%s is not valid UTF-8 JSON: %s" % (description, exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("%s must contain one JSON object" % description)
    return value
