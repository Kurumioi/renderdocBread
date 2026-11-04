import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_compact_release as release


def test_compact_rewrite_removes_old_payload_and_preserves_exact_dependency_inventory(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for source in release.SCOPES:
        folder = repo / source
        folder.mkdir(parents=True)
        (folder / "current.txt").write_text("current source")
    monkeypatch.setattr(release, "REPO", repo)
    monkeypatch.setattr(release, "fingerprint", lambda: "source")
    capsule = tmp_path / "caps"; capsule.mkdir()
    (capsule / "runtime.zip").write_bytes(b"already compressed")
    item = {"path": "plugins/old/data", "size": 3, "sha256": hashlib.sha256(b"old").hexdigest()}
    tools = {"componentArchives": [{"id": "python"}], "files": [dict(item, path="p/new/data")],
             "physicalFiles": [{"path": "runtime.zip"}]}
    (capsule / "bread-capabilities.json").write_text(json.dumps(tools))
    monkeypatch.setattr(release, "verify", lambda path: tools)
    base = tmp_path / "base.zip"
    files = {release.CAPS + item["path"]: b"old", "unchanged.dll": b"native", "replaylab/skills/obsolete.txt": b"obsolete",
             "pymodules/renderdoc.pyd": b"previous native bindings", "renderdoc.dll": b"previous replay core"}
    manifest = {"files": [{"path": p, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for p, v in files.items()]}
    with zipfile.ZipFile(base, "w") as z:
        for p, v in files.items(): z.writestr(release.PREFIX + p, v)
        z.writestr(release.PREFIX + "RenderDocBread.manifest.json", json.dumps(manifest))
        z.writestr(release.PREFIX + release.CAPS + "bread-capabilities.json", json.dumps({"files": [item]}))
    base.with_suffix(".zip.sha256").write_text(release.digest(base))
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "replaylab-runtime-manifest.json").write_text(json.dumps({"sourceInputsSha256": "source", "files": []}))
    gui = tmp_path / "gui.exe"; gui.write_bytes(b"gui")
    bindings = tmp_path / "renderdoc.pyd"; bindings.write_bytes(b"current native bindings")
    core = tmp_path / "renderdoc.dll"; core.write_bytes(b"current replay core")
    output = tmp_path / "result.zip"
    result = release.compose(base, capsule, runtime, gui, output, replay_bindings=bindings, replay_core=core)
    with zipfile.ZipFile(output) as z:
        assert release.PREFIX + release.CAPS + item["path"] not in z.namelist()
        assert z.read(release.PREFIX + "unchanged.dll") == b"native"
        assert z.read(release.PREFIX + "pymodules/renderdoc.pyd") == bindings.read_bytes()
        assert z.read(release.PREFIX + "renderdoc.dll") == core.read_bytes()
        assert not any("obsolete" in name for name in z.namelist())
        assert len(z.namelist()) == len(set(z.namelist()))
        assert b"obsolete" not in output.read_bytes()  # No unreachable superseded local member.
    assert result["dependenciesPreserved"]


def test_core_rewrite_omits_all_optional_bytes_but_keeps_unity_and_core_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "fingerprint", lambda: "source")
    monkeypatch.setattr(release, "verify", lambda *a: (_ for _ in ()).throw(AssertionError("No optional snapshot needed")))
    base = tmp_path / "base.zip"
    unwanted = b"UNWANTED_OFFICE_AND_BROWSER_PAYLOAD"
    files = {release.CAPS + "bundles/office.zip": unwanted,
             "replaylab/tools/unrequested-plugin/bin.exe": unwanted,
             "replaylab/portable-skills/documents/SKILL.md": unwanted,
             "replaylab/tools/unity-mcp/runtime/python.exe": b"unity python",
             "replaylab/tools/unpackers/uex/uex.exe": b"uex"}
    manifest = {"files": [{"path": p, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for p, v in files.items()]}
    with zipfile.ZipFile(base, "w") as z:
        for p, v in files.items(): z.writestr(release.PREFIX + p, v)
        z.writestr(release.PREFIX + "RenderDocBread.manifest.json", json.dumps(manifest))
    base.with_suffix(".zip.sha256").write_text(release.digest(base))
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "replaylab-runtime-manifest.json").write_text(json.dumps({"sourceInputsSha256": "source", "files": []}))
    gui = tmp_path / "gui.exe"; gui.write_bytes(b"gui")
    output = tmp_path / "core.zip"
    result = release.compose(base, None, runtime, gui, output)
    with zipfile.ZipFile(output) as z:
        assert not any(name.startswith(release.PREFIX + release.CAPS) or "/portable-skills/" in name for name in z.namelist())
        assert not any("unrequested-plugin" in name for name in z.namelist())
        assert json.loads(z.read(release.PREFIX + "replaylab/bread-distribution.json"))["profile"] == "core"
        assert z.read(release.PREFIX + "replaylab/tools/unity-mcp/runtime/python.exe") == b"unity python"
        assert z.read(release.PREFIX + "replaylab/tools/unpackers/uex/uex.exe") == b"uex"
        assert any(name.endswith("skills/renderdoc-resource-export/SKILL.md") for name in z.namelist())
    assert unwanted not in output.read_bytes()  # No unreachable local ZIP entries.
    assert result["profile"] == "core" and result["logicalToolFiles"] == 0
