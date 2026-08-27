from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

import pytest

from src.shader_workspace import ShaderKey, ShaderWorkspaceService, ToolExecutionError
from src.shader_workspace.binary_formats import fingerprint_shader_executable
from src.shader_workspace.catalog import capture_identity
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry


FAKE_SPIRV_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
source = Path(sys.argv[2])
target = Path(sys.argv[3])
if mode == "decompile":
    target.write_text("#version 450\nlayout(location=0) out vec4 color;\nvoid main(){color=vec4(1);}\n", encoding="utf-8")
elif mode == "compile":
    shutil.copyfile(Path(sys.argv[4]), target)
else:
    raise SystemExit(3)
'''


@pytest.fixture()
def spirv_workspace_fixture(tmp_path: Path) -> dict:
    script = tmp_path / "spirv_tool.py"
    script.write_text(FAKE_SPIRV_TOOL, encoding="utf-8")
    executable = Path(sys.executable).resolve()
    identity = {
        "executable": str(executable),
        "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "fixedFileHashes": {
            str(script.resolve()): hashlib.sha256(script.read_bytes()).hexdigest()
        },
    }
    tools_path = tmp_path / "shader-tools.v1.json"
    atomic_write_json(
        tools_path,
        {
            "schema": "renderdoc-ultra-shader-tools/v1",
            "schemaVersion": 1,
            "tools": {
                "fake-spirv-cross": {
                    **identity,
                    "decompile": {
                        "args": [str(script), "decompile", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-glslang": {
                    **identity,
                    "compile": {
                        "args": [
                            str(script),
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{stage}",
                            "{target_api}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
            },
        },
    )
    registry = ToolRegistry(tools_path)
    routes = [
        route_record(
            registry,
            route_id="fake-spirv-glsl",
            tool_id="fake-spirv-cross",
            operation="decompile",
            input_format="spirv",
            output_format="glsl",
            stages=["fragment"],
            fidelity="reconstructed-source",
            priority=10,
            implementation="test SPIR-V source recovery",
            source_url="https://example.invalid/spirv-cross",
            caveats=["Reconstructed GLSL is not original source."],
        ),
        route_record(
            registry,
            route_id="fake-glsl-spirv",
            tool_id="fake-glslang",
            operation="compile",
            input_format="glsl",
            output_format="spirv",
            stages=["fragment"],
            fidelity="native-compile",
            priority=10,
            implementation="test GLSL compiler",
            source_url="https://example.invalid/glslang",
        ),
    ]
    routes_path = tmp_path / "shader-toolchains.v1.json"
    atomic_write_json(routes_path, toolchain_document(tools_path, routes, []))

    # Structurally valid little-endian SPIR-V 1.0 header plus OpNop.
    blob = struct.pack("<6I", 0x07230203, 0x00010000, 0, 1, 0, 0x00010000)
    key = ShaderKey(501, "fragment", "main", hashlib.sha256(blob).hexdigest())
    catalog_root = tmp_path / "catalog"
    (catalog_root / "blobs").mkdir(parents=True)
    (catalog_root / "blobs" / (key.raw_sha256 + ".bin")).write_bytes(blob)
    shader = {
        "shaderKey": key.catalog_key,
        "resourceId": key.resource_id,
        "shaderResourceId": key.resource_id,
        "stage": key.stage,
        "entryPoint": key.entry_point,
        "encoding": "SPIRV",
        "rawPath": "blobs/%s.bin" % key.raw_sha256,
        "rawSize": len(blob),
        "rawSha256": key.raw_sha256,
        "interfaceComplete": False,
    }
    catalog = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "capturePath": "D:/captures/vulkan-frame.rdc",
        "api": "Vulkan",
        "shaders": [shader],
        "uses": [
            {
                "eventId": 71,
                "shaderKey": key.catalog_key,
                "resourceId": key.resource_id,
                "stage": key.stage,
                "entryPoint": key.entry_point,
                "rawSha256": key.raw_sha256,
            }
        ],
    }
    catalog["captureId"] = capture_identity(catalog, [key])
    catalog_path = catalog_root / "shader_catalog.v1.json"
    atomic_write_json(catalog_path, catalog)
    return {
        "service": ShaderWorkspaceService(tools_path, routes_path),
        "routesPath": routes_path,
        "catalogPath": catalog_path,
        "key": key,
        "blob": blob,
        "root": tmp_path,
    }


def test_spirv_glsl_round_trip_workspace_is_editable_but_apply_blocked(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    workspace = fixture["root"] / "workspace"
    service = fixture["service"]
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["routePlan"]["inputFormat"] == "spirv"
    assert created["routePlan"]["editableFormat"] == "glsl"
    assert created["recoveryPlan"]["mode"] == "round-trip"

    decompiled = service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    assert source_path.name == "shader.glsl"
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8"
    )
    compiled = service.compile(workspace)
    assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]
    status = service.status(workspace)
    assert status["targetProfile"]["apiEnvironment"] == "vulkan"
    assert status["latestCompiled"]["abiGate"]["status"] == (
        "blocked-apply-backend-unsupported"
    )
    assert status["applied"] is False
    with pytest.raises(Exception, match="fail-closed|eligible for replay ABI reflection"):
        service.candidate_reflection_request(workspace)


def test_spirv_compile_only_recovery_promotes_glsl(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    routes_path = fixture["routesPath"]
    routes = json.loads(routes_path.read_text(encoding="utf-8"))
    routes["routes"] = [
        route for route in routes["routes"] if route["inputFormat"] == "glsl"
    ]
    atomic_write_json(routes_path, routes)
    service = ShaderWorkspaceService(fixture["service"].registry.path, routes_path)
    workspace = fixture["root"] / "compile-only"
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["recoveryPlan"]["mode"] == "compile-only-recovery"
    representation = service.representation(workspace)
    candidate = workspace / "explanation" / "promoted.glsl"
    source = b"#version 450\nvoid main(){}\n"
    candidate.write_bytes(source)
    service.promote_explanatory_source(
        workspace,
        expected_representation_sha256=representation["sha256"],
        expected_source_sha256=hashlib.sha256(source).hexdigest(),
        acknowledge_semantic_equivalence_unverified=True,
    )
    compiled = service.compile(workspace)
    assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]


def test_byte_swapped_spirv_fingerprint_is_consistent_with_detection() -> None:
    blob = struct.pack(">6I", 0x07230203, 0x00010000, 0, 1, 0, 0x00010000)
    executable = fingerprint_shader_executable(blob, "swapped fixture")
    assert executable is not None
    assert executable.shader_format == "spirv"
    assert executable.raw_sha256 == hashlib.sha256(blob).hexdigest()


def test_opengl_spirv_round_trip_uses_environment_specific_formats(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    registry = fixture["service"].registry
    routes_path = fixture["routesPath"]
    routes = [
        route_record(
            registry,
            route_id="fake-opengl-spirv-glsl",
            tool_id="fake-spirv-cross",
            operation="decompile",
            input_format="opengl-spirv",
            output_format="glsl",
            stages=["fragment"],
            fidelity="reconstructed-source",
            priority=1,
            implementation="test OpenGL SPIR-V recovery",
            source_url="https://example.invalid/opengl-spirv-cross",
            caveats=["Fixture output is reconstructed GLSL."],
        ),
        route_record(
            registry,
            route_id="fake-glsl-opengl-spirv",
            tool_id="fake-glslang",
            operation="compile",
            input_format="glsl",
            output_format="opengl-spirv",
            stages=["fragment"],
            fidelity="native-compile",
            priority=1,
            implementation="test OpenGL GLSL compiler",
            source_url="https://example.invalid/opengl-glslang",
        ),
    ]
    atomic_write_json(
        routes_path,
        toolchain_document(registry.path, routes, []),
    )
    catalog_path = fixture["catalogPath"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["api"] = "OpenGL"
    catalog["capturePath"] = "D:/captures/opengl-frame.rdc"
    catalog["captureId"] = capture_identity(catalog, [fixture["key"]])
    atomic_write_json(catalog_path, catalog)

    service = ShaderWorkspaceService(registry.path, routes_path)
    workspace = fixture["root"] / "opengl-workspace"
    created = service.create(catalog_path, fixture["key"], workspace)
    assert created["routePlan"]["inputFormat"] == "opengl-spirv"
    assert created["targetProfile"]["apiEnvironment"] == "opengl"
    decompiled = service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "// OpenGL edit\n",
        encoding="utf-8",
    )
    compiled = service.compile(workspace)
    assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]
