import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
from capability_archives import pack
from prepare_capabilities_bundle import verify, stage
from test_finalize_capabilities_archive import base_release


def test_compressed_bundle_keeps_all_dependencies_and_shortens_plugin_roots(tmp_path):
    _, source, metadata = base_release(tmp_path)
    plugin = "plugins/vendor/example/123456/skills/example/SKILL.md"
    path = source / plugin; path.parent.mkdir(parents=True); path.write_text("skill")
    import hashlib
    metadata["files"].append({"path": plugin, "size": 5, "sha256": hashlib.sha256(b"skill").hexdigest()})
    metadata["plugins"] = [{"id": "example@vendor", "path": "plugins/vendor/example/123456"}]
    metadata["skillRoots"] = ["plugins/vendor/example/123456/skills"]
    target = tmp_path / "compressed"
    compact = pack(metadata, lambda name: (source / name).open("rb"), target)
    assert compact["plugins"][0]["path"] == "p/example"
    assert (target / "p/example/skills/example/SKILL.md").read_text() == "skill"
    assert compact["skillRoots"] == ["p/example/skills"]
    assert not (target / "python").exists()
    assert (target / "bundles/python.zip").is_file()
    assert verify(target)["componentArchives"] == compact["componentArchives"]
    stage(target, tmp_path / "staged")
    assert verify(tmp_path / "staged")["physicalFiles"] == compact["physicalFiles"]
