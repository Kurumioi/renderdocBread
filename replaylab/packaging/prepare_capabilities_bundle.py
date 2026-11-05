"""Snapshot explicitly selected plugin code and dependency trees, never account state.

Only this build-time importer knows source machine paths. Runtime manifests use
paths relative to the bundle. Rebuilding an existing snapshot needs no Codex install.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import zipfile
from build_fs import long_path

REPO = Path(__file__).resolve().parents[2]
PRIVATE = {"auth.json", "config.toml", "profiles.json", "keys.json", "agent-tools.json",
           ".env", "history.jsonl", "state.db", "cookies", "login data"}
SKIP = {"__pycache__", ".git", ".pytest_cache", "logs", "sessions", "node_cache"}


def sha(path):
    path = long_path(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_code(source: Path, target: Path):
    """Keep package subdirectories (including src/config), licenses and native DLLs."""
    source, target = long_path(source), long_path(target)
    if source.is_symlink() or source.is_junction():
        raise ValueError("Capability input cannot be a link")
    for directory, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP)
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise ValueError("Capability input contains a link: " + str(path))
        for name in files:
            if name.lower() in PRIVATE or name.endswith((".pyc", ".log", ".rdc", ".dmp")):
                continue
            # These generated files contain native pipe ids and host account
            # paths. Preserve their API descriptions, never their live settings.
            if name == ".mcp.json":
                continue
            path = Path(directory) / name
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def seal(root: Path, metadata: dict):
    root = long_path(root)
    records = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                "sha256": sha(p)} for p in sorted(root.rglob("*"))
               if p.is_file() and p.name != "bread-capabilities.json" and "__pycache__" not in p.parts]
    metadata["files"] = records
    (root / "bread-capabilities.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify(root: Path):
    root = long_path(root)
    metadata = json.loads((root / "bread-capabilities.json").read_text(encoding="utf-8"))
    if metadata.get("schema") != "bread-portable-capabilities/v1":
        raise ValueError("Unknown capability bundle schema")
    compressed = bool(metadata.get("componentArchives"))
    records = metadata.get("physicalFiles", []) if compressed else metadata["files"]
    expected = {record["path"] for record in records}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
              and p.name != "bread-capabilities.json" and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    if compressed:
        # Expanded runtimes and OS-lock files are execution scratch; only the
        # sealed physical records are distributable inputs.
        from capability_archives import COMPONENTS
        actual = {p for p in actual if p in expected or p.split("/")[0] not in COMPONENTS | {".runtime-state"}}
    if expected != actual:
        raise ValueError("Capability file inventory differs from its sealed manifest")
    for record in records:
        path = (root / record["path"]).resolve()
        if path.name.lower() in PRIVATE or path.name == ".mcp.json":
            raise ValueError("Account state or live connection settings cannot be bundled")
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha(path) != record["sha256"]:
            raise ValueError("Capability file mismatch: " + record["path"])
    logical = {record["path"] for record in metadata["files"]}
    if compressed:
        from capability_archives import COMPONENTS
        for component in metadata["componentArchives"]:
            if component["id"] not in COMPONENTS or component["path"] not in expected:
                raise ValueError("Unknown compressed runtime component")
            with zipfile.ZipFile(root / component["path"]) as archive:
                contents = [item.filename for item in archive.infolist() if not item.is_dir()]
                wanted = {p for p in logical if p.startswith(component["id"] + "/")}
                if len(contents) != len(wanted) or set(contents) != wanted:
                    raise ValueError("Compressed runtime inventory differs")
    dependencies = () if metadata.get("distributionProfile") == "rdc" else ("python/python.exe", "node/bin/node.exe", "python/Lib/site-packages/docx/__init__.py",
                 "python/Lib/site-packages/openpyxl/__init__.py", "python/Lib/site-packages/pptx/__init__.py",
                 "python/Lib/site-packages/pypdf/__init__.py")
    if metadata.get("distributionProfile") == "rdc":
        if metadata.get("features", {}).get("portableTools") is not False or metadata.get("componentArchives"):
            raise ValueError("RDC skills-only profile cannot declare portable runtimes")
    for name in dependencies:
        if not (name in logical if compressed else (root / name).is_file()):
            raise ValueError("Missing portable dependency: " + name)
    return metadata


def stage(source: Path, target: Path):
    """Copy the verified payload, omitting host-specific installer wrappers.

    Leave the snapshot untouched. Bread invokes Python entry points with -m;
    wheel-generated Scripts launchers and Playwright installation links retain
    build-machine paths and are not runtime inputs.
    """
    metadata = verify(source)
    excluded = ("python/Scripts/", "browsers/.links/")
    selected = [record for record in metadata.get("physicalFiles", metadata["files"]) if not record["path"].startswith(excluded)]
    if target.exists() and any(target.iterdir()):
        raise ValueError("Capability staging requires an empty directory")
    target.mkdir(parents=True, exist_ok=True)
    for record in selected:
        destination = target / record["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / record["path"], destination)
    metadata = {**metadata, "physicalFiles" if metadata.get("componentArchives") else "files": selected,
                "excludedBuildOnly": list(excluded)}
    (target / "bread-capabilities.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify(target)


def import_desktop(root: Path, desktop: Path, dependencies: Path):
    if root.exists() and any(root.iterdir()):
        raise ValueError("Import requires a new empty output directory; existing snapshots are preserved")
    sys.path.insert(0, str(REPO / "replaylab/src"))
    from replaylab_agent.desktop_capabilities import discover_desktop_capabilities
    capabilities = discover_desktop_capabilities(desktop)
    root.mkdir(parents=True, exist_ok=True)
    plugins = []
    skill_roots = []
    for identifier, source in capabilities.plugins.items():
        name, marketplace = identifier.rsplit("@", 1)
        relative = Path("plugins") / marketplace / name / source.name
        copy_code(source, root / relative)
        manifest = json.loads((source / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        declared = manifest.get("skills", "./skills")
        for item in [declared] if isinstance(declared, str) else declared or []:
            candidate = (source / item).resolve()
            if candidate.is_relative_to(source.resolve()) and candidate.is_dir():
                skill_roots.append((relative / candidate.relative_to(source)).as_posix())
        plugins.append({"id": identifier, "version": source.name, "path": relative.as_posix(),
                        "license": manifest.get("license", "see-original-notices"),
                        "activation": "shared-adapter" if name in {"documents", "pdf", "spreadsheets", "presentations"}
                        else "requires-live-provider-service-or-shared-adapter"})
    if (desktop / "skills").is_dir():
        copy_code(desktop / "skills", root / "custom-skills")
        skill_roots.append("custom-skills")
    for name in ("python", "node", "native"):
        copy_code(dependencies / name, root / name)
    # No discovered MCP env/header/URL is serialized. Service templates describe
    # a dependency; credentials and transient pipes are supplied only at runtime.
    metadata = {"schema": "bread-portable-capabilities/v1", "plugins": plugins,
                "skillRoots": skill_roots, "sharedAdapter": "bread-portable",
                "providerServices": {"codex_app": {"requires": "CODEX_APP_TOOLS_PIPE_PATH"},
                                     "cua_repl": {"requires": "SKY_CUA_NATIVE_PIPE_DIRECTORY"}},
                "credentialsIncluded": False}
    seal(root, metadata)
    return verify(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--desktop", type=Path)
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument("--provider-runtime", type=Path)
    parser.add_argument("--bread-skills", type=Path, help="Archive personal Bread skill overrides without replacing newer defaults")
    parser.add_argument("--office-msi", type=Path)
    parser.add_argument("--seal", action="store_true", help="Seal after adding audited runtime dependencies")
    args = parser.parse_args()
    args.output = long_path(args.output)
    if args.desktop and args.dependencies:
        metadata = import_desktop(args.output.resolve(), args.desktop.resolve(), args.dependencies.resolve())
    else:
        if args.bread_skills:
            copy_code(args.bread_skills, args.output / "user-skill-snapshots")
        if args.provider_runtime:
            copy_code(args.provider_runtime, args.output / "provider-runtime")
        if args.office_msi:
            # Verified, pinned official administrative image: no system install,
            # registry integration, file associations or recipient dependency.
            if sha(args.office_msi) != "ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a":
                raise ValueError("Pinned LibreOffice 26.8.2 MSI hash mismatch")
            with tempfile.TemporaryDirectory(prefix="bread-office-extract-") as temp:
                subprocess.run(["msiexec.exe", "/a", str(args.office_msi), "/qn", "TARGETDIR=" + temp],
                               check=True, timeout=300, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                programs = list(Path(temp).rglob("program/soffice.exe"))
                if len(programs) != 1:
                    raise ValueError("Office image has no unique program directory")
                copy_code(programs[0].parent.parent, args.output / "office")
        if args.seal:
            metadata = json.loads((args.output / "bread-capabilities.json").read_text(encoding="utf-8"))
            adapters = Path(__file__).with_name("capability_adapters")
            if adapters.is_dir():
                copy_code(adapters, args.output / "bread-adapters")
                if "bread-adapters" not in metadata["skillRoots"]:
                    metadata["skillRoots"].append("bread-adapters")
            if (args.output / "user-skill-snapshots").is_dir():
                metadata["userSkillSnapshots"] = {"path": "user-skill-snapshots", "activation": "optional-user-override",
                    "note": "Preserved personal copies; current bundled defaults remain active on a new installation."}
            seal(args.output, metadata)
        metadata = verify(args.output)
    print(json.dumps({"plugins": len(metadata["plugins"]), "files": len(metadata["files"]),
                      "bytes": sum(item["size"] for item in metadata["files"])}, indent=2))


if __name__ == "__main__":
    main()
