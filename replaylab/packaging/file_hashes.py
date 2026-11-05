"""Process-local, change-aware hashes shared by package audits and manifests."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
from threading import RLock

from build_fs import long_path

_cache = {}
_lock = RLock()

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ("creation", "access", "write", "change")] + [("attributes", wintypes.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]


def _change_time(path):
    # Windows stat().st_ctime is creation time in CPython 3.12, not NTFS
    # ChangeTime. Query the latter so restoring LastWriteTime cannot hide edits.
    handle = _kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileBasicInfo()
        if not _kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.change
    finally:
        _kernel.CloseHandle(handle)


def identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            _change_time(path) if os.name == "nt" else info.st_ctime_ns)


def digest(path):
    path = long_path(path)
    before = identity(path)
    key = os.path.normcase(str(path))
    with _lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == before:
        return cached[1]
    with path.open("rb") as stream:
        value = hashlib.file_digest(stream, "sha256").hexdigest()
    if identity(path) != before:
        raise ValueError(f"File changed while hashing: {path}")
    with _lock:
        _cache[key] = (before, value)
    return value


def warm_digests(paths, *, fresh=False):
    paths = list(paths)
    if fresh:
        with _lock:
            for path in paths:
                _cache.pop(os.path.normcase(str(long_path(path))), None)
    # Bound concurrent disk readers even on large build machines.
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        for _ in pool.map(digest, paths):
            pass  # Iteration propagates worker failures before the audit begins.
