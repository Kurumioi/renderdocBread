import hashlib
import os
import zipfile

import pytest

import file_hashes
from package_portable import zip_compress_type


def test_hash_reuse_and_same_size_edit_with_restored_mtime(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"original")
    calls = []
    original = hashlib.file_digest
    def measured(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(hashlib, "file_digest", measured)
    stamp = path.stat()
    first = file_hashes.digest(path)
    file_hashes.warm_digests([path])
    assert file_hashes.digest(path) == first and len(calls) == 1
    path.write_bytes(b"modified")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert file_hashes.digest(path) != first and len(calls) == 2


def test_parallel_hash_failure_is_not_silenced(tmp_path):
    with pytest.raises(FileNotFoundError):
        file_hashes.warm_digests([tmp_path / "absent"])


@pytest.mark.parametrize("path,size,method", [
    ("replaylab/replaylab-ai.exe", 20 * 1024**2, zipfile.ZIP_STORED),
    ("replaylab/ReplayLabInternal.exe", 10, zipfile.ZIP_DEFLATED),
    ("replaylab/tools/other.exe", 20 * 1024**2, zipfile.ZIP_DEFLATED),
    ("RenderDocBread.exe", 40 * 1024**2, zipfile.ZIP_DEFLATED),
    ("python36.zip", 10, zipfile.ZIP_STORED),
    ("licenses/NOTICE.txt", 10, zipfile.ZIP_DEFLATED),
    ("assets/image.PNG", 10, zipfile.ZIP_STORED),
    ("replaylab/tools/capabilities/bundles/unknown", 10, zipfile.ZIP_STORED),
])
def test_zip_policy(path, size, method):
    assert zip_compress_type(path, size) == method
