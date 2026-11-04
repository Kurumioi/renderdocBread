import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("cap_bundle", Path(__file__).parents[1] / "prepare_capabilities_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def test_plugin_dependency_copy_preserves_code_but_excludes_accounts(tmp_path):
    source = tmp_path / "source"
    names = ["docs/API.md", "src/runner.py", "LICENSE", "config/defaults.json", "auth.json", ".mcp.json",
             "logs/private.log", "__pycache__/x.pyc"]
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample")
    output = tmp_path / "bundle"
    bundle.copy_code(source, output)
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()) == [
        "LICENSE", "config/defaults.json", "docs/API.md", "src/runner.py"]


def test_sealed_bundle_detects_unrecorded_files(tmp_path):
    bundle.seal(tmp_path, {"schema": "bread-portable-capabilities/v1"})
    (tmp_path / "auth.json").write_text("private")
    with pytest.raises(ValueError, match="inventory"):
        bundle.verify(tmp_path)
