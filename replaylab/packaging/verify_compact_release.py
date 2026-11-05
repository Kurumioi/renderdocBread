"""Relocated execution in a fresh temporary profile, without developer PATH tools."""
import argparse
import json
from pathlib import Path
import tempfile
import zipfile
from build_fs import temporary_directory

from package_portable import audit, digest, read_json, smoke, verify_compilers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = read_json(args.receipt)
    archive = Path(receipt["archive"])
    if digest(archive) != receipt["sha256"]:
        raise ValueError("Release checksum differs")
    with temporary_directory(prefix="Bread-portable-relocated-") as temporary:
        stage = Path(temporary) / "便携测试"
        stage.mkdir()
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                target = (stage / member.filename).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError("Release member escapes its root")
            package.extractall(stage)
        root = stage / "RenderDocBread"
        audit(root, read_json(Path(__file__).with_name("RenderDocBread.package.json")))
        print("Relocated release audit passed", flush=True)
        verify_compilers(root)
        print("Relocated compiler capability checks passed", flush=True)
        checks = smoke(root)
        args.output.write_text(json.dumps({"ok": True, "archive": str(archive),
            "sha256": receipt["sha256"], "freshProfile": True, "developerPathRemoved": True,
            "checks": ["sealed-release-audit", "shader-compiler-capabilities", *checks]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Relocated smoke passed; temporary profile and expanded tools removed", flush=True)


if __name__ == "__main__":
    main()
