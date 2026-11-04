import hashlib
import importlib.util
import json
import io
from pathlib import Path
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("restore_dependencies", Path(__file__).parents[1] / "restore_dependencies.py")
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


def sealed(tmp_path, entries=None):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    entries = entries or {"tool/license.txt": b"original notice", "tool/code.py": b"print('tool')\n"}
    archive = inputs / "tools.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name, data in entries.items():
            stream.writestr(name, data)
    record = {"id": "tools", "path": "tools.zip", "size": archive.stat().st_size,
        "sha256": restore.sha(archive), "files": [{"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
            for n, d in entries.items()]}
    return inputs, record


def test_restore_is_reusable_and_preserves_sealed_inputs(tmp_path):
    inputs, record = sealed(tmp_path)
    output = tmp_path / "build"
    result = restore.restore(inputs, output, record)
    original_mtime = (result / "tool/code.py").stat().st_mtime_ns
    assert restore.restore(inputs, output, record) == result
    assert (result / "tool/code.py").stat().st_mtime_ns == original_mtime
    assert restore.sha(inputs / record["path"]) == record["sha256"]
    assert set(p.name for p in inputs.iterdir()) == {"tools.zip"}


def test_modified_output_is_repaired_with_previous_generation_preserved(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (result / "tool/code.py").write_text("damaged")
    assert restore.restore(inputs, tmp_path / "build", record) == result
    assert (result / "tool/code.py").read_bytes() == b"print('tool')\n"
    previous = list(result.parent.glob("*.damaged-*"))
    assert len(previous) == 1 and (previous[0] / "tool/code.py").read_text() == "damaged"


def test_changed_input_cannot_overwrite_working_output(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (inputs / record["path"]).write_bytes(b"wrong input")
    with pytest.raises(ValueError, match="input changed"):
        restore.restore(inputs, tmp_path / "build", record)
    assert (result / "tool/code.py").read_bytes() == b"print('tool')\n"


def test_unlisted_output_files_cannot_enter_a_package(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (result / "unexpected-client.exe").write_bytes(b"unlisted")
    restore.restore(inputs, tmp_path / "build", record)
    assert not (result / "unexpected-client.exe").exists()


@pytest.mark.parametrize("entries", [
    {"../outside": b"x"}, {"/absolute": b"x"}, {"C:/absolute": b"x"},
    {"dir\\file": b"x"}, {"tool/A": b"x", "tool/a": b"y"},
])
def test_unsafe_archive_paths_are_rejected_before_extraction(tmp_path, entries):
    inputs, record = sealed(tmp_path, entries)
    with pytest.raises(ValueError):
        restore.restore(inputs, tmp_path / "build", record)
    assert not (tmp_path / "outside").exists()


def test_lfs_pointer_gives_actionable_diagnostic(tmp_path):
    inputs, record = sealed(tmp_path)
    (inputs / record["path"]).write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10000\n")
    with pytest.raises(ValueError, match="--download"):
        restore.verify_archive(inputs, record)


def downloadable(record):
    return dict(record, downloadUrl="https://github.com/Straw1997/renderdocBread/releases/download/v1.13.121/" + record["path"])


def test_download_verifies_and_atomically_restores_missing_input(tmp_path, monkeypatch):
    inputs, record = sealed(tmp_path)
    data = (inputs / record["path"]).read_bytes()
    (inputs / record["path"]).unlink()
    monkeypatch.setattr(restore, "urlopen", lambda *a, **kw: io.BytesIO(data))
    assert restore.download_archive(inputs, downloadable(record)).read_bytes() == data
    assert not list(inputs.glob(".bread-download-*"))


@pytest.mark.parametrize("corrupt", [b"short", b"x" * 10000])
def test_failed_download_preserves_legacy_pointer_and_cleans_partial(tmp_path, monkeypatch, corrupt):
    inputs, record = sealed(tmp_path)
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10000\n"
    (inputs / record["path"]).write_bytes(pointer)
    monkeypatch.setattr(restore, "urlopen", lambda *a, **kw: io.BytesIO(corrupt))
    with pytest.raises(ValueError):
        restore.download_archive(inputs, downloadable(record))
    assert (inputs / record["path"]).read_bytes() == pointer
    assert not list(inputs.glob(".bread-download-*"))


def test_existing_valid_or_damaged_input_is_never_downloaded_over(tmp_path, monkeypatch):
    inputs, record = sealed(tmp_path)
    def no_network(*a, **kw):
        pytest.fail("Existing archives must not be downloaded over")
    monkeypatch.setattr(restore, "urlopen", no_network)
    assert restore.download_archive(inputs, downloadable(record)) == inputs / record["path"]
    (inputs / record["path"]).write_bytes(b"damaged archive")
    with pytest.raises(ValueError, match="input changed"):
        restore.download_archive(inputs, downloadable(record))
    assert (inputs / record["path"]).read_bytes() == b"damaged archive"


def test_untrusted_download_url_is_rejected_before_network(tmp_path, monkeypatch):
    inputs, record = sealed(tmp_path)
    (inputs / record["path"]).unlink()
    with pytest.raises(ValueError, match="download URL"):
        restore.download_archive(inputs, dict(record, downloadUrl="https://example.invalid/tools.zip"))


def test_extract_cannot_write_into_dependency_inputs(tmp_path):
    inputs, record = sealed(tmp_path)
    with pytest.raises(ValueError, match="inside sealed inputs"):
        restore.restore(inputs, inputs / "build", record)


def test_legacy_explicit_config_remains_usable():
    assert restore.standard_capabilities({"standardCapabilitiesDirectory": "old/bundle"}) == restore.REPO / "old/bundle"
