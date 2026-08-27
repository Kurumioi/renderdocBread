"""Fail-closed SPIRV-Cross command adapter.

SPIRV-Cross otherwise selects the first module entry point and defaults HLSL
output to Shader Model 3.0.  This adapter accepts only workspace-derived
stage/entry/profile/API facts, cross-checks them, and builds the fixed CLI used
by the hash-pinned tool registry.  It never searches ``PATH`` or invokes a
shell.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


_STAGE_TO_SPIRV_CROSS = {
    "vertex": "vert",
    "hull": "tesc",
    "domain": "tese",
    "geometry": "geom",
    "fragment": "frag",
    "compute": "comp",
    "task": "task",
    "mesh": "mesh",
}
_STAGE_TO_PROFILE_PREFIX = {
    "vertex": "vs",
    "hull": "hs",
    "domain": "ds",
    "geometry": "gs",
    "fragment": "ps",
    "compute": "cs",
    "task": "as",
    "mesh": "ms",
}
_PROFILE_RE = re.compile(r"^(ps|vs|gs|hs|ds|cs|ms|as)_(\d+)_(\d+)$")
_HLSL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalise_api(value: str) -> str:
    api = str(value).strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    return {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
    }.get(api, api)


def derive_spirv_cross_arguments(
    *,
    source: str,
    output: str,
    mode: str,
    entry_point: str,
    stage: str,
    target_api: str,
    target_profile: str = "",
) -> list[str]:
    """Derive one exact SPIRV-Cross argument vector from physical facts."""

    normalised_stage = str(stage).strip().casefold()
    spirv_stage = _STAGE_TO_SPIRV_CROSS.get(normalised_stage)
    if spirv_stage is None:
        raise ValueError("unsupported SPIRV-Cross stage: %s" % stage)
    entry = str(entry_point).strip()
    if not entry or len(entry) > 512 or "\x00" in entry:
        raise ValueError("entry point must be a bounded non-empty string")
    api = _normalise_api(target_api)
    arguments = [
        str(source),
        "--entry",
        entry,
        "--stage",
        spirv_stage,
    ]
    if mode == "hlsl":
        if api not in {"d3d11", "d3d12"}:
            raise ValueError("HLSL reconstruction requires a physical Direct3D API")
        if not _HLSL_IDENTIFIER_RE.fullmatch(entry):
            raise ValueError("HLSL entry point is not a source identifier")
        profile = str(target_profile).strip().casefold()
        match = _PROFILE_RE.fullmatch(profile)
        if match is None:
            raise ValueError("HLSL reconstruction requires an exact target profile")
        expected_prefix = _STAGE_TO_PROFILE_PREFIX[normalised_stage]
        if match.group(1) != expected_prefix:
            raise ValueError(
                "target profile %s contradicts physical stage %s"
                % (profile, normalised_stage)
            )
        major = int(match.group(2))
        minor = int(match.group(3))
        if major < 3 or minor > 9:
            raise ValueError("target profile cannot be represented by SPIRV-Cross")
        if api == "d3d11" and major >= 6:
            raise ValueError("Shader Model 6 profile contradicts physical D3D11 API")
        shader_model = str(major * 10 + minor)
        arguments.extend(
            [
                "--hlsl",
                "--shader-model",
                shader_model,
                # Any explicit rename makes SPIRV-Cross emit the selected
                # physical entry name instead of silently changing it to main.
                "--rename-entry-point",
                entry,
                entry,
                spirv_stage,
            ]
        )
    elif mode == "vulkan-glsl":
        if api != "vulkan":
            raise ValueError("Vulkan GLSL reconstruction requires physical Vulkan API")
        if str(target_profile).strip():
            raise ValueError("Vulkan GLSL reconstruction must not invent a DirectX profile")
        arguments.append("--vulkan-semantics")
    elif mode == "opengl-glsl":
        if api != "opengl":
            raise ValueError("OpenGL GLSL reconstruction requires physical OpenGL API")
        if str(target_profile).strip():
            raise ValueError("OpenGL GLSL reconstruction must not invent a DirectX profile")
        # Absence of --vulkan-semantics is intentional. RenderDoc likewise
        # configures a distinct SPIRV-Cross tool for OpenGL-flavoured SPIR-V.
        # The capture encoding is the authority because the SPIR-V header alone
        # cannot identify the consumer environment.
    else:
        raise ValueError("unsupported SPIRV-Cross adapter mode")
    arguments.extend(["--output", str(output)])
    return arguments


def _regular_file(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("%s must be absolute" % label)
    if path.is_symlink():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    path = path.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--mode", choices=("hlsl", "vulkan-glsl", "opengl-glsl"), required=True
    )
    parser.add_argument("--entry", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--target-api", required=True)
    parser.add_argument("--target-profile", default="")
    parsed = parser.parse_args(argv)

    tool = _regular_file(parsed.tool, "SPIRV-Cross")
    source = _regular_file(parsed.input, "input")
    raw_output = Path(parsed.output)
    if not raw_output.is_absolute():
        raise RuntimeError("output must be absolute")
    if raw_output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    output = raw_output.resolve()
    if output.exists() or output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise RuntimeError("output parent must be a regular directory")

    command = [str(tool)] + derive_spirv_cross_arguments(
        source=str(source),
        output=str(output),
        mode=parsed.mode,
        entry_point=parsed.entry,
        stage=parsed.stage,
        target_api=parsed.target_api,
        target_profile=parsed.target_profile,
    )
    completed = subprocess.run(
        command,
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        env=dict(os.environ),
        check=False,
    )
    if completed.returncode != 0:
        return int(completed.returncode or 1)
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("SPIRV-Cross did not produce its expected output")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
