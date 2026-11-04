"""Assemble an allowlisted, relocatable release without writing to the live application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"__pycache__", ".git", ".venv", "obj", ".uex-cache", ".uex-analysis",
             ".pytest_cache", "cache", "logs", "setup", "profiles", "config", "src"}
SKIP_SUFFIXES = {".pdb", ".lib", ".exp", ".ilk", ".pyc", ".log", ".dmp", ".rdc"}
PRIVATE = {"profiles.json", "keys.json", "agent-tools.json", "UI.config", "mcp.json",
           "profiles.local.json", "agent-tools.local.json", ".env", "deployed.json"}
ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\\s]+\\)")
UEX_NATIVE_FILES = {"Detex.dll", "oodle-data-shared.dll", "zlib-ng2.dll"}
UEX_NATIVE_PREFIX = "replaylab/tools/unpackers/uex/.uex-cache/"


def uex_native_input():
    from restore_dependencies import DEPENDENCIES, load_lock, verify_archive
    record = next(r for r in load_lock()["archives"] if r["id"] == "uex-native")
    if {r["path"] for r in record["files"]} != UEX_NATIVE_FILES:
        raise ValueError("Unexpected UEX native dependency inventory")
    return record, verify_archive(DEPENDENCIES, record)


def stage_uex_natives(root):
    # These three pinned SDK DLLs are dependencies, despite their legacy cache
    # directory name. No generated cache/profile contents enter the package.
    record, archive = uex_native_input()
    with zipfile.ZipFile(archive) as source:
        for item in record["files"]:
            path = child(root, UEX_NATIVE_PREFIX + item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read(item["path"]))
    write_json(root / "replaylab/tools/unpackers/uex/native-dependencies.json", record)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_files(root):
    # Office can create Python caches during its execution smoke. They are
    # runtime scratch, not distributable inputs or required dependencies.
    capsule = root / "replaylab/tools/capabilities"
    metadata = capsule / "bread-capabilities.json"
    physical = None
    if metadata.is_file():
        value = read_json(metadata)
        if value.get("componentArchives"):
            physical = {r["path"] for r in value["physicalFiles"]} | {"bread-capabilities.json"}
    return [p for p in sorted(root.rglob("*")) if p.is_file()
            and "__pycache__" not in p.parts and p.suffix.lower() not in {".pyc", ".pyo"}
            and (physical is None or not p.is_relative_to(capsule) or p.relative_to(capsule).as_posix() in physical)]


def child(root, relative):
    path = (root / relative).resolve()
    if path == root.resolve() or root.resolve() not in path.parents:
        raise ValueError(f"Package path escapes its root: {relative}")
    return path


def copy_tree(source, target, *, excluded=()):
    if not source.is_dir():
        return
    for current, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in excluded)
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"Release inputs cannot contain reparse points: {path}")
        for name in sorted(files):
            path = Path(current) / name
            if (path.suffix.lower() in SKIP_SUFFIXES or name in PRIVATE or
                    name.startswith((".replaylab-", "profiles.", "keys."))):
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def normalize_receipts(root):
    manifest_path = root / "replaylab/replaylab-runtime-manifest.json"
    manifest = read_json(manifest_path)
    if isinstance(manifest.get("shaderTools"), dict):
        manifest["shaderTools"].pop("path", None)  # build-work diagnostic, never a runtime input
    write_json(manifest_path, manifest)
    tool_manifest = root / "replaylab/tools/unpackers/manifest.json"
    if tool_manifest.is_file():
        value = read_json(tool_manifest)
        for kind, tool in value.get("tools", {}).items():
            program = tool.get("program", "")
            if ABSOLUTE.search(program):
                tool["program"] = "%BREAD_TOOLS%/unpackers/" + kind + "/" + re.split(r"[\\/]", program)[-1]
        write_json(tool_manifest, value)


def audit(root, config):
    errors = [f"Missing {name}" for name in config["requiredFiles"] if not child(root, name).is_file()]
    native_record, _ = uex_native_input()
    native_inputs = {UEX_NATIVE_PREFIX + r["path"]: r for r in native_record["files"]}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        unity_runtime = relative.startswith("replaylab/tools/unity-mcp/runtime/")
        capability_runtime = relative.startswith("replaylab/tools/capabilities/")
        pinned_native = relative in native_inputs
        if pinned_native:
            expected = native_inputs[relative]
            if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
                errors.append(f"UEX native dependency differs: {relative}")
        if not capability_runtime and (path.name in PRIVATE or (not unity_runtime and not pinned_native and any(part in SKIP_DIRS for part in parts))):
            errors.append(f"Private/cache file: {relative}")
        if not capability_runtime and path.suffix.lower() in {".json", ".ini", ".cfg", ".config"} and path.stat().st_size < 4 * 1024**2:
            if ABSOLUTE.search(path.read_text(encoding="utf-8-sig", errors="replace")):
                errors.append(f"Machine-specific path in {relative}")
    manifest = read_json(root / "replaylab/replaylab-runtime-manifest.json")
    from runtime_inputs import fingerprint
    if manifest.get("sourceInputsSha256") != fingerprint():
        errors.append("Frozen runtime is from different source/skill inputs; rebuild the runtime before release")
    for record in manifest["files"]:
        path = child(root / "replaylab", record["path"])
        if not path.is_file() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            errors.append(f"Runtime hash mismatch: {record['path']}")
    runtime_config = read_json(root / "replaylab/tools/unpackers/uex/uex.runtimeconfig.json")
    if "framework" in runtime_config["runtimeOptions"] or "frameworks" in runtime_config["runtimeOptions"]:
        errors.append("UEX requires a machine-installed .NET runtime; publish it self-contained")
    expected = sorted(p.parent.name for p in (REPO / ".agents/skills").glob("*/SKILL.md"))
    shipped = sorted(p.parent.name for p in (root / "replaylab/skills").glob("*/SKILL.md"))
    if shipped != expected:
        errors.append("Bundled skill folders do not match the repository")
    distribution = read_json(root / "replaylab/bread-distribution.json") if (root / "replaylab/bread-distribution.json").is_file() else {}
    if distribution.get("profile") == "core":
        for relative in ("replaylab/tools/capabilities", "replaylab/portable-skills"):
            if (root / relative).exists():
                errors.append("Unrequested extension in core release: " + relative)
    else:
        from prepare_capabilities_bundle import verify
        verify(root / "replaylab/tools/capabilities")
    if errors:
        raise ValueError("Release audit failed:\n" + "\n".join(errors))


def smoke(root):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("REPLAYLAB_", "BREAD_", "QT_", "PYTHON", "CODEX_", "SKY_", "NODE_REPL_", "BROWSER_USE_", "CUA_")) or key in {"VIRTUAL_ENV", "NODE_PATH", "NODE_OPTIONS"}:
            env.pop(key, None)
    validation=root.parent/"validation-state"
    for name,relative in {"LOCALAPPDATA":"local","APPDATA":"roaming","USERPROFILE":"user",
        "RENDERDOC_CONFIG_DIR":"ui","REPLAYLAB_RUNTIME_DIR":"mcp-runtime",
        "REPLAYLAB_INTERNAL_RUNTIME_DIR":"internal-runtime","REPLAYLAB_CONFIG_DIR":"config"}.items():
        private=validation/relative;private.mkdir(parents=True,exist_ok=True);env[name]=str(private)
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    env["HOME"] = env["USERPROFILE"]
    env["DOTNET_ROOT"] = str(root / "missing-system-dotnet")
    env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    commands = [
        [root / "RenderDocBread.exe", "--version"],
        [root / "replaylab/replaylab-ai.exe", "-I", "-m", "replaylab_agent.selftest", "--external-mcp-import-smoke"],
        [root / "replaylab/tools/unpackers/uex/uex.exe", "--help"],
        [root / "replaylab/tools/unpackers/assetstudio/AssetStudioModCLI.exe", "--help"],
        [root / "replaylab/tools/unity-mcp/runtime/python.exe", "-I", "-B",
         root / "replaylab/tools/unity-mcp/runtime/bread_unity_entry.py", "--help"],
    ]
    capability_file = root / "replaylab/tools/capabilities/bread-capabilities.json"
    if capability_file.is_file() and read_json(capability_file).get("features", {}).get("portableTools", True):
        commands.append([root / "replaylab/replaylab-ai.exe", "-I", "-m", "replaylab_agent.portable_tools", "--verify-execution"])
    checked = []
    for command in commands:
        result = subprocess.run([str(item) for item in command], cwd=root, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=300 if "--verify-execution" in command else 120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError(f"Relocation smoke failed: {command[0].name}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
        print(f"Relocation smoke passed: {command[0].name}", flush=True)
        if "--verify-execution" in command:
            details = json.loads(result.stdout)
            if details.get("ok") is not True or not isinstance(details.get("checks"), list):
                raise ValueError("Portable execution smoke returned no checked capabilities")
            checked.extend(details["checks"])
        else:
            checked.append({"RenderDocBread.exe": "native-version", "replaylab-ai.exe": "frozen-mcp-import",
                "uex.exe": "self-contained-uex", "AssetStudioModCLI.exe": "assetstudio", "python.exe": "bundled-unity-provider-cli"}[command[0].name])
    return checked


def verify_compilers(root):
    manifest = read_json(Path(__file__).with_name("shader_dxc_manifest.json"))
    bundle = root / "replaylab/tools/dxc"
    binaries = {"dxc.exe", "dxcompiler.dll", "dxil.dll"}
    for name, expected in manifest["platforms"]["windows-x64"]["fileSha256"].items():
        path = (bundle / "bin" if name in binaries else bundle / "licenses" / manifest["release"]) / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Pinned DXC file mismatch: {name}")
    subprocess.run([sys.executable, str(Path(__file__).with_name("fetch_shader_dxc.py")),
                    "--verify-executable", str(bundle / "bin/dxc.exe")], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.run([sys.executable, str(REPO / "util/shader_language_server/deploy_provider.py"),
                    "--application", str(root), "--verify-only"], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.run([sys.executable, str(Path(__file__).with_name("verify_uex_natives.py")),
                    str(root / UEX_NATIVE_PREFIX)], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def assemble(application, gui, runtime, uex, crt, destination, config, audit_only=False, unity_mcp=None, capabilities=None):
    profile = read_json(capabilities / "bread-capabilities.json").get("distributionProfile", "extended") if capabilities else "core"
    destination.mkdir(parents=True, exist_ok=True)
    # Unique, disposable staging only. Never clean, deploy to, or rename the source application.
    with tempfile.TemporaryDirectory(prefix="RenderDocBread-release-") as work:
        root = Path(work) / "RenderDocBread 中文路径"
        root.mkdir()
        for name in config["rootFiles"]:
            fresh = gui.parent / name
            # Native builds put the new core, injection library and helper executables beside
            # the new GUI. Dependencies absent there come from the read-only source application.
            source = fresh if fresh.is_file() else child(application, name)
            if source.is_file():
                shutil.copy2(source, root / name)
        for name in config["directories"]:
            copy_tree(child(application, name), root / name)
        shutil.copy2(gui, root / "RenderDocBread.exe")
        # Never copy unknown installed extensions into a core distribution.
        for name in config["toolDirectories"]:
            copy_tree(child(application / "replaylab/tools", name), root / "replaylab/tools" / name)
        for name in config["toolFiles"]:
            source = child(application / "replaylab/tools", name)
            if source.is_file():
                target = root / "replaylab/tools" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if unity_mcp:
            # The provider needs its own stdlib/CLI modules, including cli/config packages.
            # It was already assembled and hash-verified by prepare_unity_bundle.py.
            from prepare_unity_bundle import verify
            verify(unity_mcp)
            shutil.copytree(unity_mcp, root / "replaylab/tools/unity-mcp", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        copy_tree(application / "integrations/shader-language-server", root / "integrations/shader-language-server")
        copy_tree(REPO / "replaylab/renderdoc_mcp_bridge", root / "integrations/replaylab/python/renderdoc_mcp_bridge")
        copy_tree(REPO / ".agents/skills", root / "replaylab/skills")
        write_json(root / "replaylab/bread-distribution.json", {
            "schema": "bread-distribution/v1", "profile": profile})
        if capabilities:
            if profile != "rdc":
                copy_tree(REPO / "replaylab/portable_skills", root / "replaylab/portable-skills")
            from prepare_capabilities_bundle import stage
            stage(capabilities, root / "replaylab/tools/capabilities")
            from capability_archives import compact_staging
            if profile != "rdc":
                compact_staging(root / "replaylab/tools/capabilities")
        copy_tree(REPO / "replaylab/unity", root / "replaylab/unity")
        for name in ("replaylab-ai.exe", "BreadMCP.exe", "ReplayLabInternal.exe", "ReplayLabShaderTools.exe", "replaylab-runtime-manifest.json"):
            shutil.copy2(runtime / name, root / "replaylab" / name)
        if uex:
            # Start with the official unpacker files (native codecs/licenses), then overlay the
            # self-contained publish of the same patched source. Personal tool state is excluded.
            copy_tree(uex, root / "replaylab/tools/unpackers/uex")
            stage_uex_natives(root)
        uex_target = root / "replaylab/tools/unpackers/uex"
        uex_source = REPO / "replaylab/third_party/unpackers/_src/uex"
        for relative, name in (("LICENSE", "LICENSE.uex.txt"),
                               ("deps/CUE4Parse/LICENSE", "LICENSE.CUE4Parse.txt"),
                               ("deps/CUE4Parse/NOTICE", "NOTICE.CUE4Parse.txt")):
            if not (uex_target / name).is_file():
                shutil.copy2(uex_source / relative, uex_target / name)
        for path in crt.glob("*.dll"):
            shutil.copy2(path, root / path.name)
            if path.name.lower() == "vcruntime140.dll":
                # Native codecs are loaded by uex.exe, whose DLL search root
                # is its own directory rather than the GUI's parent directory.
                shutil.copy2(path, uex_target / path.name)
        shutil.copy2(REPO / "LICENSE.md", root / "LICENSE.md")
        for relative in ("qrenderdoc/3rdparty/qt/LICENSE", "qrenderdoc/3rdparty/python/LICENSE"):
            source = REPO / relative
            target = root / "licenses" / source.parent.name / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # Preserve the corresponding source and upstream build instructions of
        # the bundled GPL decompiler, alongside its original COPYING notices.
        if (root / "replaylab/tools/community-hlsl-decompiler/cmd_Decompiler.exe").is_file():
            from restore_dependencies import DEPENDENCIES, load_lock, verify_archive
            source_record = next(r for r in load_lock()["archives"] if r["id"] == "3dmigoto-source")
            decompiler_source = verify_archive(DEPENDENCIES, source_record)
            source_target = root / "licenses/3dmigoto"
            source_target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(decompiler_source, source_target / decompiler_source.name)
        shutil.copy2(Path(__file__).with_name("PORTABLE_README.txt"), root / "README.txt")
        (root / "RenderDocBread.portable").write_text("renderdocbread-portable/v1\n", encoding="ascii")
        (root / "qt.conf").write_text("[Paths]\nPlugins=qtplugins\n", encoding="ascii")
        normalize_receipts(root)
        audit(root, config)
        # Provider and DXC verification are build-side checks against staged bytes only.
        verify_compilers(root)
        checked = smoke(root) or []
        audit(root, config)  # tools must not leave caches or machine-specific configuration
        records = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": digest(p)}
                   for p in release_files(root)]
        write_json(root / "RenderDocBread.manifest.json", {"schema": "renderdocbread-release/v1",
                   "product": config["product"], "platform": config["platform"],
                   "breadVersion": read_json(root / "replaylab/replaylab-runtime-manifest.json").get("version"),
                   "profile": profile,
                   "createdUtc": datetime.now(timezone.utc).isoformat(), "files": records})
        audit_receipt = destination / ("RenderDocBread-audit-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        write_json(audit_receipt, {"schema": "bread-package-audit/v1", "mode": "audit-only" if audit_only else "package",
            "product": config["product"], "platform": config["platform"], "profile": profile,
            "breadVersion": read_json(root / "replaylab/replaylab-runtime-manifest.json").get("version"),
            "fileCount": len(records), "requiredFileCount": len(config.get("requiredFiles", [])),
            "sourceInputsSha256": read_json(root / "replaylab/replaylab-runtime-manifest.json").get("sourceInputsSha256"),
            "checks": checked, "passed": True})
        print(f"Release audit receipt: {audit_receipt}", flush=True)
        if audit_only:
            print(f"Release audit passed ({len(records)} files)")
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = destination / f"RenderDocBread-{stamp}-win-x64.zip"
        pending = archive.with_suffix(".zip.partial")
        with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for path in release_files(root):
                relative = path.relative_to(root).as_posix()
                output.write(path, "RenderDocBread/" + relative,
                             compress_type=zipfile.ZIP_STORED if relative.startswith(
                                 "replaylab/tools/capabilities/bundles/") else zipfile.ZIP_DEFLATED)
        os.replace(pending, archive)
        archive.with_suffix(".zip.sha256").write_text(digest(archive) + "  " + archive.name + "\n", encoding="ascii")
        print(f"Created {archive}", flush=True)
        return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("application", "gui", "runtime", "crt", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--uex", type=Path)
    parser.add_argument("--unity-mcp", type=Path, required=True)
    parser.add_argument("--capabilities", type=Path, help="Verified skills/runtime snapshot; defaults to the configured standard bundle")
    parser.add_argument("--minimal", action="store_true", help="Explicitly omit standard workflow skills/tools")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("RenderDocBread.package.json"))
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if not args.capabilities and not args.minimal:
        from restore_dependencies import standard_capabilities
        args.capabilities = standard_capabilities(read_json(args.config))
    assemble(args.application.resolve(), args.gui.resolve(), args.runtime.resolve(),
             args.uex.resolve() if args.uex else None, args.crt.resolve(), args.destination.resolve(),
             read_json(args.config), args.audit_only, args.unity_mcp.resolve(), args.capabilities.resolve() if args.capabilities else None)


if __name__ == "__main__":
    main()
