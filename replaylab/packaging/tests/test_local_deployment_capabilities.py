"""Patch deployment reuses only exactly verified dependency trees."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "util/ai"))
import deploy_current_build as deploy
from prepare_capabilities_bundle import seal


def bundle(path, data=b"tool-code"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "tool.bin").write_bytes(data)
    seal(path, {"schema": "bread-portable-capabilities/v1", "distributionProfile": "rdc",
                "features": {"portableTools": False}})


def installed(tmp_path):
    app = tmp_path / "app"
    old = app / "replaylab/tools/capabilities-old"
    bundle(old)
    (app / "replaylab/bread-distribution.json").write_text(json.dumps(
        {"capabilitiesDirectory": "tools/capabilities-old"}), encoding="utf-8")
    source = tmp_path / "source"
    bundle(source)
    return app, old, source


def test_verified_identical_dependency_tree_is_reused(tmp_path):
    app, old, source = installed(tmp_path)
    assert deploy.capabilities_target(app, source, "new") == ("tools/capabilities-old", old, True)
    assert not (app / "replaylab/tools/capabilities-new").exists()


def test_changed_or_damaged_dependencies_get_a_separate_tree(tmp_path):
    app, old, source = installed(tmp_path)
    (old / "tool.bin").write_bytes(b"damaged")
    relative, target, reused = deploy.capabilities_target(app, source, "new")
    assert relative == "tools/capabilities-new" and not reused
    assert target == app / "replaylab/tools/capabilities-new"
    assert (old / "tool.bin").read_bytes() == b"damaged"


def test_unsafe_old_directory_is_never_reused(tmp_path):
    app, old, source = installed(tmp_path)
    (app / "replaylab/bread-distribution.json").write_text(json.dumps(
        {"capabilitiesDirectory": "../../outside"}), encoding="utf-8")
    assert deploy.capabilities_target(app, source, "new")[2] is False
