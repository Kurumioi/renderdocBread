from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

from src.shader_workspace import (
    ToolConfigurationError,
    ToolchainCatalog,
    ToolchainRoutingError,
    detect_shader_format,
    generate_official_toolchain_configs,
    extract_target_profile,
)
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.adapters.spirv_cross_adapter import (
    derive_spirv_cross_arguments,
)
from src.shader_workspace.toolchains import route_record, toolchain_document, validator_record
from src.shader_workspace.tools import ToolRegistry


def make_container_from_chunks(chunks: list[tuple[bytes, bytes]]) -> bytes:
    header_size = 32 + 4 * len(chunks)
    offsets: list[int] = []
    encoded_chunks: list[bytes] = []
    cursor = header_size
    for fourcc, payload in chunks:
        encoded = fourcc + struct.pack("<I", len(payload)) + payload
        offsets.append(cursor)
        encoded_chunks.append(encoded)
        cursor += len(encoded)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", value) for value in offsets)
        + b"".join(encoded_chunks)
    )


def make_container(fourcc: bytes = b"SHDR", code: bytes | None = None) -> bytes:
    if code is None:
        code = (
            struct.pack("<I", 0x00000060) + b"instructions"
            if fourcc == b"DXIL"
            else make_dxbc_program(0x00000050, b"instructions")
        )
    return make_container_from_chunks([(fourcc, code)])


def make_dxbc_program(program_version: int, instructions: bytes = b"\x00" * 4) -> bytes:
    padding = b"\x00" * (-len(instructions) % 4)
    body = instructions + padding
    return struct.pack("<2I", program_version, 2 + len(body) // 4) + body


def make_dxil_program(program_version: int, bitcode: bytes = b"BC\xc0\xdefixture") -> bytes:
    bitcode_offset = 16  # relative to DxilBitcodeHeader
    unpadded_size = 24 + len(bitcode)
    padded_size = (unpadded_size + 3) & ~3
    return (
        struct.pack(
            "<6I",
            program_version,
            padded_size // 4,
            0x4C495844,
            0x00000100,
            bitcode_offset,
            len(bitcode),
        )
        + bitcode
        + b"\x00" * (padded_size - unpadded_size)
    )


def replace_dxil_header_field(container: bytes, index: int, value: int) -> bytes:
    changed = bytearray(container)
    chunk_offset = struct.unpack_from("<I", changed, 32)[0]
    struct.pack_into("<I", changed, chunk_offset + 8 + index * 4, value)
    return bytes(changed)


FAKE_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
if mode == "decompile-hlsl":
    output_path.write_text(
        "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
        encoding="utf-8",
    )
elif mode == "compile-baseline":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "validate":
    if input_path.stat().st_size <= 0:
        raise SystemExit(2)
else:
    raise SystemExit("unknown mode")
'''


class ShaderToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temporary_root))
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_tool.py"
        self.script.write_text(FAKE_TOOL, encoding="utf-8")
        self.script_hash = hashlib.sha256(self.script.read_bytes()).hexdigest()
        self.python = Path(sys.executable).resolve()
        self.python_hash = hashlib.sha256(self.python.read_bytes()).hexdigest()
        self.tools_path = self.root / "shader-tools.v1.json"
        self.routes_path = self.root / "shader-toolchains.v1.json"
        self._write_tools()
        self._write_routes()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_tools(self) -> None:
        base = {
            "executable": str(self.python),
            "executableSha256": self.python_hash,
            "fixedFileHashes": {str(self.script.resolve()): self.script_hash},
        }
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {
                    "fake-decompiler": {
                        **base,
                        "decompile": {
                            "args": [
                                str(self.script.resolve()),
                                "decompile-hlsl",
                                "{input}",
                                "{output}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                    "fake-compiler": {
                        **base,
                        "compile": {
                            "args": [
                                str(self.script.resolve()),
                                "compile-baseline",
                                "{input}",
                                "{output}",
                                "{baseline}",
                                "{target_profile}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                    "fake-validator": {
                        **base,
                        "validate": {
                            "args": [
                                str(self.script.resolve()),
                                "validate",
                                "{input}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                },
            },
        )

    def _write_routes(self, ambiguous: bool = False) -> None:
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="fake-dxbc-hlsl",
                tool_id="fake-decompiler",
                operation="decompile",
                input_format="dxbc",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=100,
                implementation="test decompiler",
                source_url="https://example.invalid/decompiler",
                caveats=["Test reconstruction is not original source."],
            ),
            route_record(
                registry,
                route_id="fake-hlsl-dxbc",
                tool_id="fake-compiler",
                operation="compile",
                input_format="hlsl",
                output_format="dxbc",
                stages=["fragment"],
                fidelity="native-compile",
                priority=10,
                implementation="test compiler",
                source_url="https://example.invalid/compiler",
            ),
        ]
        if ambiguous:
            duplicate = dict(routes[0])
            duplicate["routeId"] = "fake-dxbc-hlsl-equal"
            routes.append(duplicate)
        validators = [
            validator_record(
                registry,
                validator_id="fake-dxbc-validator",
                tool_id="fake-validator",
                input_format="dxbc",
                stages=["fragment"],
                priority=10,
                implementation="test validator",
                source_url="https://example.invalid/validator",
            )
        ]
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, validators)
        )

    def test_detects_containers_ir_and_conservative_source_languages(self) -> None:
        self.assertEqual(detect_shader_format(make_container()).format, "dxbc")
        self.assertEqual(detect_shader_format(make_container(b"DXIL")).format, "dxil")
        spirv = struct.pack("<5I", 0x07230203, 0x00010600, 0, 1, 0)
        self.assertEqual(detect_shader_format(spirv).format, "spirv")
        opengl_spirv = detect_shader_format(spirv, "OpenGLSPIRV")
        self.assertEqual(opengl_spirv.format, "opengl-spirv")
        self.assertEqual(
            opengl_spirv.confidence, "declared-semantic-environment"
        )
        self.assertEqual(
            detect_shader_format(b"; SPIR-V\nOpCapability Shader\nOpMemoryModel Logical GLSL450\n").format,
            "spirv-asm",
        )
        self.assertEqual(
            detect_shader_format(
                b"; SPIR-V\nOpCapability Shader\nOpMemoryModel Logical GLSL450\n",
                "OpenGLSPIRVAsm",
            ).format,
            "opengl-spirv-asm",
        )
        self.assertEqual(
            detect_shader_format(b"float4 main() : SV_Target { return 1; }\n").format,
            "hlsl",
        )
        self.assertEqual(
            detect_shader_format(b"#version 450\nvoid main(){gl_Position=vec4(0);}\n").format,
            "glsl",
        )
        self.assertIsNone(detect_shader_format(b"void main() {}\n").format)
        with self.assertRaisesRegex(ToolchainRoutingError, "contradicts"):
            detect_shader_format(make_container(), "spirv")

    def test_exact_target_profile_is_read_from_compiled_tokens_never_stage_guessed(self) -> None:
        cases = (
            (0x00010040, "vs_4_0", "vertex"),
            (0x00000041, "ps_4_1", "fragment"),
            (0x00050050, "cs_5_0", "compute"),
            (0x00020051, "gs_5_1", "geometry"),
        )
        for token, expected, stage in cases:
            with self.subTest(expected=expected):
                profile = extract_target_profile(
                    make_container(code=make_dxbc_program(token, b"code")), stage
                )
                self.assertEqual(profile.profile, expected)
                self.assertFalse(profile.as_dict()["guessed"])
        dxil = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x00000067)), "fragment"
        )
        self.assertEqual(dxil.profile, "ps_6_7")
        task = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x000E0067)),
            "amplification",
        )
        self.assertEqual(task.profile, "as_6_7")
        self.assertEqual(task.stage, "task")
        with self.assertRaisesRegex(ToolchainRoutingError, "contradicts"):
            extract_target_profile(make_container(), "vertex")
        with self.assertRaisesRegex(ToolchainRoutingError, "multi-entry"):
            extract_target_profile(
                make_container(b"DXIL", make_dxil_program(0x00060063)),
                "library",
            )

    def test_dxbc_profile_requires_complete_exact_tokenized_program_header(self) -> None:
        valid = make_container(code=make_dxbc_program(0x00010040, b"code"))
        shex = extract_target_profile(
            make_container(b"SHEX", make_dxbc_program(0x00050050)), "compute"
        )
        self.assertEqual(shex.profile, "cs_5_0")

        chunk_offset = struct.unpack_from("<I", valid, 32)[0]
        short = make_container(code=struct.pack("<I", 0x00010040))
        unaligned = make_container(code=make_dxbc_program(0x00010040) + b"x")
        too_short_length = bytearray(valid)
        struct.pack_into("<I", too_short_length, chunk_offset + 12, 1)
        contradictory_length = bytearray(valid)
        struct.pack_into("<I", contradictory_length, chunk_offset + 12, 2)
        oversized_length = bytearray(valid)
        struct.pack_into("<I", oversized_length, chunk_offset + 12, 0xFFFFFFFF)
        reserved_version = bytearray(valid)
        struct.pack_into("<I", reserved_version, chunk_offset + 8, 0x00010140)

        malformed = (
            ("missing Length", short, "too small"),
            ("non-DWORD chunk", unaligned, "not DWORD-aligned"),
            ("Length below header", bytes(too_short_length), "Length.*smaller"),
            ("contradictory Length", bytes(contradictory_length), "Length.*match"),
            ("hostile Length", bytes(oversized_length), "Length.*match"),
            ("reserved version bits", bytes(reserved_version), "version.*reserved"),
        )
        for label, fixture, message in malformed:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ToolchainRoutingError, message):
                    extract_target_profile(fixture, "vertex")

    def test_dxil_profile_requires_a_complete_consistent_program_header(self) -> None:
        valid = make_container(b"DXIL", make_dxil_program(0x00000067))
        malformed = (
            (
                "truncated headers",
                make_container(b"DXIL", b"\x00" * 23),
                "too small",
            ),
            (
                "reserved ProgramVersion bits",
                replace_dxil_header_field(valid, 0, 0x00000167),
                "ProgramVersion.*reserved",
            ),
            (
                "undersized SizeInUint32",
                replace_dxil_header_field(valid, 1, 5),
                "SizeInUint32.*smaller",
            ),
            (
                "mismatched SizeInUint32",
                replace_dxil_header_field(valid, 1, 6),
                "SizeInUint32.*match",
            ),
            (
                "wrong DXIL magic",
                replace_dxil_header_field(valid, 2, 0x41414141),
                "invalid DXIL magic",
            ),
            (
                "bitcode overlaps header",
                replace_dxil_header_field(valid, 4, 12),
                "BitcodeOffset overlaps",
            ),
            (
                "hostile offset",
                replace_dxil_header_field(valid, 4, 0xFFFFFFFC),
                "BitcodeOffset exceeds",
            ),
            (
                "empty bitcode",
                replace_dxil_header_field(valid, 5, 0),
                "BitcodeSize.*non-zero",
            ),
            (
                "bitcode crosses part end",
                replace_dxil_header_field(valid, 5, 0xFFFFFFFF),
                "bitcode range exceeds",
            ),
            (
                "bitcode and padded program disagree",
                replace_dxil_header_field(valid, 5, len(b"BC\xc0\xdefixture") - 4),
                "inconsistent",
            ),
        )
        for label, fixture, message in malformed:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ToolchainRoutingError, message):
                    extract_target_profile(fixture, "fragment")

    def test_downlevel_aon9_profile_fails_closed_instead_of_guessing_sm40(self) -> None:
        level_9 = make_container_from_chunks(
            [
                (b"SHDR", make_dxbc_program(0x00010040, b"code")),
                (b"Aon9", b"undocumented-downlevel-metadata"),
            ]
        )
        with self.assertRaisesRegex(ToolchainRoutingError, "10Level9/downlevel.*not be guessed"):
            extract_target_profile(level_9, "vertex")

    def test_auto_route_is_explainable_and_fails_closed_on_equal_choices(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selected = catalog.select_round_trip(
            "dxbc",
            "fragment",
            editable_format="hlsl",
            capture_encoding="dxbc",
            target_api="d3d11",
        )
        self.assertEqual(selected["decompilerToolIds"], ["fake-decompiler"])
        self.assertEqual(selected["compilerToolIds"], ["fake-compiler"])
        self.assertTrue(selected["containsReconstructedSource"])
        self.assertFalse(selected["semanticEquivalenceVerified"])
        self.assertIn("No route can guarantee", selected["guarantee"])

        self._write_routes(ambiguous=True)
        ambiguous = ToolchainCatalog(self.tools_path, self.routes_path)
        with self.assertRaisesRegex(ToolchainRoutingError, "ambiguous"):
            ambiguous.select_round_trip("dxbc", "fragment", target_api="d3d11")

        recovery = ambiguous.plan_recovery(
            "dxbc", "fragment", editable_format="hlsl", target_api="d3d11"
        )
        self.assertEqual("round-trip", recovery["mode"])
        self.assertEqual(
            [["fake-dxbc-hlsl"], ["fake-dxbc-hlsl-equal"]],
            recovery["decompileCandidates"],
        )
        self.assertEqual(["fake-dxbc-hlsl"], recovery["decompileRouteIds"])
        self.assertTrue(
            any(
                action["id"] == "retry-registered-decompiler"
                for action in recovery["allowedActions"]
            )
        )

    def test_recovery_plan_keeps_compile_only_and_evidence_only_lanes(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["routes"] = [
            route
            for route in value["routes"]
            if route["routeId"] != "fake-dxbc-hlsl"
        ]
        atomic_write_json(self.routes_path, value)
        compile_only = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        self.assertEqual("compile-only-recovery", compile_only["mode"])
        self.assertEqual([], compile_only["decompileRouteIds"])
        self.assertEqual(["fake-hlsl-dxbc"], compile_only["compileRouteIds"])
        self.assertTrue(compile_only["aiPolicy"]["mayAuthorProvisionalSource"])
        self.assertFalse(compile_only["directReplayApplyAuthorized"])

        value["routes"] = []
        atomic_write_json(self.routes_path, value)
        evidence_only = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        self.assertEqual("evidence-only", evidence_only["mode"])
        self.assertEqual([], evidence_only["compileRouteIds"])
        self.assertFalse(evidence_only["aiPolicy"]["mayAuthorProvisionalSource"])
        self.assertTrue(
            any(
                action["id"] == "install-or-register-format-provider"
                for action in evidence_only["allowedActions"]
            )
        )

    def test_recovery_plan_enumerates_alternate_compiler_pairs(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        compiler = next(
            route
            for route in value["routes"]
            if route["routeId"] == "fake-hlsl-dxbc"
        )
        alternate = json.loads(json.dumps(compiler))
        alternate["routeId"] = "fake-hlsl-dxbc-alternate"
        alternate["priority"] = 20
        value["routes"].append(alternate)
        atomic_write_json(self.routes_path, value)

        plan = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        retry = next(
            action
            for action in plan["allowedActions"]
            if action["id"] == "retry-registered-decompiler"
        )
        self.assertEqual(retry["candidateRoutePaths"], [])
        self.assertEqual(
            retry["candidateRoutePairs"],
            [
                {
                    "decompileCandidateIndex": 0,
                    "compileCandidateIndex": 1,
                    "decompileRouteIds": ["fake-dxbc-hlsl"],
                    "compileRouteIds": ["fake-hlsl-dxbc-alternate"],
                }
            ],
        )

    def test_route_catalog_is_bound_to_exact_registry_and_operation_digest(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["toolRegistrySha256"] = "0" * 64
        atomic_write_json(self.routes_path, value)
        with self.assertRaisesRegex(ToolConfigurationError, "toolRegistrySha256"):
            ToolchainCatalog(self.tools_path, self.routes_path)

        self._write_routes()
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["routes"][0]["toolConfigDigest"] = "0" * 64
        atomic_write_json(self.routes_path, value)
        with self.assertRaisesRegex(ToolConfigurationError, "toolConfigDigest"):
            ToolchainCatalog(self.tools_path, self.routes_path)

    def test_diagnostic_matrix_rehashes_tools_without_executing_them(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        before = catalog.diagnostic_matrix("fragment")
        dxbc_hlsl = next(
            row
            for row in before["matrix"]
            if row["inputFormat"] == "dxbc" and row["outputFormat"] == "hlsl"
        )
        self.assertEqual(dxbc_hlsl["status"], "ready")
        with self.script.open("a", encoding="utf-8") as stream:
            stream.write("\n# tampered\n")
        after = catalog.diagnostic_matrix("fragment")
        dxbc_hlsl = next(
            row
            for row in after["matrix"]
            if row["inputFormat"] == "dxbc" and row["outputFormat"] == "hlsl"
        )
        self.assertEqual(dxbc_hlsl["status"], "integrity-failed")
        self.assertFalse(next(iter(after["operations"]))["available"])

    def test_round_trip_smoke_records_format_evidence_without_claiming_abi(self) -> None:
        shader = self.root / "captured.dxbc"
        shader.write_bytes(make_container())
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        report = catalog.round_trip_smoke(
            shader,
            "hlsl",
            self.root / "smoke",
            "fragment",
        )
        self.assertTrue(report["formatSmokePassed"])
        self.assertTrue(report["byteIdentical"])
        self.assertFalse(report["semanticEquivalenceVerified"])
        self.assertFalse(report["replayAbiValidated"])
        self.assertTrue(report["externalValidationConfigured"])
        self.assertTrue(report["externalValidationPassed"])
        self.assertEqual(len(report["validatorJobs"]), 1)
        self.assertEqual(len(report["jobs"]), 2)
        self.assertEqual(report["targetProfile"], "ps_5_0")
        self.assertTrue((self.root / "smoke" / "smoke-report.json").is_file())

    def test_official_config_generator_hashes_explicit_paths_and_never_downloads(self) -> None:
        generated_root = self.root / "generated"
        generated_root.mkdir()
        spirv_dis = generated_root / "spirv-dis.exe"
        spirv_as = generated_root / "spirv-as.exe"
        spirv_val = generated_root / "spirv-val.exe"
        spirv_dis.write_bytes(b"explicit fake binary A")
        spirv_as.write_bytes(b"explicit fake binary B")
        spirv_val.write_bytes(b"explicit fake binary C")
        result = generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_dis=spirv_dis.resolve(),
            spirv_as=spirv_as.resolve(),
            spirv_val=spirv_val.resolve(),
            spirv_target_env="vulkan1.1",
        )
        self.assertEqual(result["toolCount"], 3)
        self.assertEqual(result["routeCount"], 2)
        self.assertEqual(result["validatorCount"], 1)
        self.assertFalse(result["binaryPathsWereDiscovered"])
        self.assertFalse(result["downloadsPerformed"])
        self.assertEqual(result["spirvTargetEnv"], "vulkan1.1")
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["spirv-tools-val"]["validate"]["args"],
            ["--target-env", "vulkan1.1", "{input}"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        with self.assertRaisesRegex(
            ToolchainRoutingError, "cannot be derived from the module"
        ):
            catalog.select_round_trip(
                "spirv", "fragment", editable_format="spirv-asm"
            )
        selected = catalog.select_round_trip(
            "spirv",
            "fragment",
            editable_format="spirv-asm",
            target_api="vulkan",
        )
        self.assertEqual(selected["targetApi"], "vulkan")
        with self.assertRaisesRegex(ToolConfigurationError, "overwrite"):
            generate_official_toolchain_configs(
                generated_root / "tools.json",
                generated_root / "routes.json",
                spirv_dis=spirv_dis.resolve(),
            )

    def test_spirv_environment_is_explicit_and_invalid_values_fail_closed(self) -> None:
        generated_root = self.root / "spirv-env"
        generated_root.mkdir()
        spirv_val = generated_root / "spirv-val.exe"
        glslang = generated_root / "glslangValidator.exe"
        spirv_val.write_bytes(b"fake spirv-val")
        glslang.write_bytes(b"fake glslang")
        for supplied in ({"spirv_val": spirv_val}, {"glslang": glslang}):
            with self.assertRaisesRegex(ToolConfigurationError, "will not be guessed"):
                generate_official_toolchain_configs(
                    generated_root / ("tools-%s.json" % next(iter(supplied))),
                    generated_root / ("routes-%s.json" % next(iter(supplied))),
                    **supplied,
                )
        with self.assertRaisesRegex(ToolConfigurationError, "unsupported Vulkan"):
            generate_official_toolchain_configs(
                generated_root / "tools-invalid.json",
                generated_root / "routes-invalid.json",
                spirv_val=spirv_val,
                spirv_target_env="opengl4.5",
            )
        generate_official_toolchain_configs(
            generated_root / "tools-valid.json",
            generated_root / "routes-valid.json",
            glslang=glslang,
            spirv_target_env="vulkan1.3",
        )
        valid_tools = json.loads(
            (generated_root / "tools-valid.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            valid_tools["tools"]["glslang-fragment-vulkan"]["compile"]["args"][:3],
            ["-V", "--target-env", "vulkan1.3"],
        )

    def test_spirv_cross_adapter_uses_physical_multi_entry_stage_and_sm6_profile(self) -> None:
        arguments = derive_spirv_cross_arguments(
            source=r"D:\jobs\multi-entry.spv",
            output=r"D:\jobs\selected.hlsl",
            mode="hlsl",
            entry_point="LightingPS",
            stage="fragment",
            target_api="GraphicsAPI.D3D12",
            target_profile="ps_6_7",
        )
        self.assertEqual(
            arguments,
            [
                r"D:\jobs\multi-entry.spv",
                "--entry",
                "LightingPS",
                "--stage",
                "frag",
                "--hlsl",
                "--shader-model",
                "67",
                "--rename-entry-point",
                "LightingPS",
                "LightingPS",
                "frag",
                "--output",
                r"D:\jobs\selected.hlsl",
            ],
        )
        with self.assertRaisesRegex(ValueError, "contradicts physical stage"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="D3D12",
                target_profile="vs_6_7",
            )
        with self.assertRaisesRegex(ValueError, "physical Direct3D API"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="Vulkan",
                target_profile="ps_6_7",
            )
        with self.assertRaisesRegex(ValueError, "contradicts physical D3D11"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="D3D11",
                target_profile="ps_6_7",
            )

    def test_opengl_spirv_routes_never_alias_vulkan_semantics(self) -> None:
        generated_root = self.root / "opengl-spirv"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "spirv-cross.exe",
            "glslangValidator.exe",
            "spirv-dis.exe",
            "spirv-as.exe",
            "spirv-val.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()
        result = generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=binaries["spirv-cross.exe"],
            glslang=binaries["glslangValidator.exe"],
            spirv_dis=binaries["spirv-dis.exe"],
            spirv_as=binaries["spirv-as.exe"],
            spirv_val=binaries["spirv-val.exe"],
            spirv_target_env="vulkan1.2",
            enable_opengl_spirv=True,
        )
        self.assertTrue(result["openGlSpirvEnabled"])
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        selected = catalog.select_round_trip(
            "OpenGLSPIRV",
            "fragment",
            editable_format="glsl",
            capture_encoding="OpenGLSPIRV",
            target_api="GraphicsAPI.OpenGL",
        )
        self.assertEqual(selected["targetApi"], "opengl")
        self.assertEqual(
            selected["decompileRouteIds"],
            ["spirv-cross-opengl-spirv-glsl"],
        )
        self.assertEqual(
            selected["compileRouteIds"],
            ["glslang-glsl-opengl-spirv-fragment"],
        )
        with self.assertRaisesRegex(ToolchainRoutingError, "does not accept"):
            catalog.select_round_trip(
                "OpenGLSPIRV", "fragment", target_api="vulkan"
            )
        with self.assertRaisesRegex(ToolchainRoutingError, "does not accept"):
            catalog.select_round_trip("spirv", "fragment", target_api="opengl")

        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["glslang-fragment-opengl"]["compile"]["args"][:3],
            ["-G", "--target-env", "opengl"],
        )
        self.assertEqual(
            tools["tools"]["spirv-tools-val-opengl"]["validate"]["args"],
            ["--target-env", "opengl", "{input}"],
        )

        arguments = derive_spirv_cross_arguments(
            source="in.opengl.spv",
            output="out.glsl",
            mode="opengl-glsl",
            entry_point="main",
            stage="fragment",
            target_api="OpenGL",
        )
        self.assertNotIn("--vulkan-semantics", arguments)
        with self.assertRaisesRegex(ValueError, "physical OpenGL API"):
            derive_spirv_cross_arguments(
                source="in.opengl.spv",
                output="out.glsl",
                mode="opengl-glsl",
                entry_point="main",
                stage="fragment",
                target_api="Vulkan",
            )

    def test_generated_spirv_cross_routes_pin_adapter_and_physical_placeholders(self) -> None:
        generated_root = self.root / "spirv-cross-adapter"
        generated_root.mkdir()
        spirv_cross = generated_root / "spirv-cross.exe"
        spirv_cross.write_bytes(b"fake spirv-cross")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=spirv_cross,
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        operation = tools["tools"]["spirv-cross-hlsl"]
        args = operation["decompile"]["args"]
        self.assertIn("{entry_point}", args)
        self.assertIn("{stage}", args)
        self.assertIn("{target_profile}", args)
        self.assertIn("{target_api}", args)
        self.assertIn(str(spirv_cross.resolve()), operation["fixedFileHashes"])
        self.assertTrue(
            any(
                path.endswith("spirv_cross_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

    def test_dxc_generator_pins_official_sibling_dlls_when_present(self) -> None:
        generated_root = self.root / "dxc-generated"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        compiler = generated_root / "dxcompiler.dll"
        validator = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc")
        compiler.write_bytes(b"fake compiler dll")
        validator.write_bytes(b"fake validator dll")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            dxc=dxc.resolve(),
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        fixed = tools["tools"]["dxc-exact-profile-dxil"]["fixedFileHashes"]
        self.assertIn(str(compiler.resolve()), fixed)
        self.assertIn(str(validator.resolve()), fixed)
        self.assertNotIn("dxc-exact-profile-spirv", tools["tools"])
        self.assertEqual(
            tools["tools"]["dxc-exact-profile-dxil"]["decompile"]["args"],
            ["-dumpbin", "-Fc", "{output}", "{input}"],
        )
        routes = json.loads(
            (generated_root / "routes.json").read_text(encoding="utf-8")
        )["routes"]
        native_dump = next(
            item for item in routes if item["routeId"] == "dxc-dxil-native-disassembly"
        )
        self.assertEqual(native_dump["outputFormat"], "dxil-asm")
        self.assertEqual(native_dump["fidelity"], "ir-disassembly")

    def test_fxc_generator_exposes_read_only_native_disassembly(self) -> None:
        generated_root = self.root / "fxc-generated"
        generated_root.mkdir()
        fxc = generated_root / "fxc.exe"
        fxc.write_bytes(b"fake fxc")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            fxc=fxc.resolve(),
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["fxc-exact-profile"]["decompile"]["args"],
            ["/nologo", "/dumpbin", "/Fc", "{output}", "{input}"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        route = catalog.matching_routes("dxbc", "dxbc-asm", "fragment")[0]
        self.assertEqual(route.fidelity, "ir-disassembly")
        row = next(
            item
            for item in catalog.diagnostic_matrix("fragment")["matrix"]
            if item["inputFormat"] == "dxbc"
            and item["outputFormat"] == "dxbc-asm"
        )
        self.assertEqual(row["lane"], "explanation")
        self.assertFalse(row["directReplayApplyAuthorized"])

    def test_d3dcompiler_generator_pins_dll_adapter_and_uniquely_beats_fxc(self) -> None:
        generated_root = self.root / "d3dcompiler-generated"
        generated_root.mkdir()
        dll = generated_root / "d3dcompiler_47.dll"
        fxc = generated_root / "fxc.exe"
        dll.write_bytes(b"fake pinned d3dcompiler")
        fxc.write_bytes(b"fake fxc")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            d3dcompiler=dll,
            fxc=fxc,
        )
        tools = json.loads((generated_root / "tools.json").read_text("utf-8"))
        record = tools["tools"]["d3dcompiler47-exact-profile"]
        self.assertEqual(str(Path(sys.executable).resolve()), record["executable"])
        self.assertIn(str(dll.resolve()), record["fixedFileHashes"])
        self.assertTrue(
            any(
                path.endswith("d3dcompiler_adapter.py")
                for path in record["fixedFileHashes"]
            )
        )
        arguments = record["compile"]["args"]
        self.assertIn("{input}", arguments)
        self.assertIn("{output}", arguments)
        self.assertIn("{entry_point}", arguments)
        self.assertIn("{target_profile}", arguments)
        self.assertNotIn("{stage}", arguments)
        self.assertNotIn("{target_api}", arguments)
        self.assertFalse(any("include" in value.casefold() for value in arguments))

        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        selected = catalog.find_path("hlsl", "dxbc", "fragment")
        self.assertEqual(
            [route.route_id for route in selected],
            ["d3dcompiler47-hlsl-dxbc-exact-profile"],
        )
        fxc_route = next(
            route
            for route in catalog.matching_routes("hlsl", "dxbc", "fragment")
            if route.route_id == "fxc-hlsl-dxbc-exact-profile"
        )
        self.assertLess(selected[0].priority, fxc_route.priority)

        wrong_name = generated_root / "compiler.dll"
        wrong_name.write_bytes(b"same API, wrong trusted package identity")
        with self.assertRaisesRegex(ToolConfigurationError, "must be named"):
            generate_official_toolchain_configs(
                generated_root / "wrong-tools.json",
                generated_root / "wrong-routes.json",
                d3dcompiler=wrong_name,
            )

    def test_community_adapters_are_explicit_lossy_routes_with_round_trip_backends(self) -> None:
        generated_root = self.root / "community-generated"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "cmd_Decompiler.exe",
            "dxil-spirv.exe",
            "spirv-cross.exe",
            "fxc.exe",
            "dxc.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            hlsl_decompiler=binaries["cmd_Decompiler.exe"],
            dxil_spirv=binaries["dxil-spirv.exe"],
            spirv_cross=binaries["spirv-cross.exe"],
            fxc=binaries["fxc.exe"],
            dxc=binaries["dxc.exe"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        dxbc = catalog.select_round_trip("dxbc", "fragment", target_api="d3d11")
        self.assertEqual(
            dxbc["decompileRouteIds"],
            ["community-hlsl-decompiler-dxbc-hlsl"],
        )
        self.assertEqual(
            dxbc["compileRouteIds"], ["fxc-hlsl-dxbc-exact-profile"]
        )
        assembly = catalog.select_round_trip(
            "dxbc",
            "fragment",
            editable_format="dxbc-asm",
            target_api="d3d11",
        )
        self.assertEqual(
            assembly["decompileRouteIds"],
            ["community-3dmigoto-dxbc-disassemble"],
        )
        self.assertEqual(
            assembly["compileRouteIds"],
            ["community-3dmigoto-dxbc-assemble"],
        )
        self.assertFalse(assembly["containsReconstructedSource"])
        self.assertFalse(assembly["semanticEquivalenceVerified"])
        dxil = catalog.select_round_trip("dxil", "fragment", target_api="d3d12")
        self.assertEqual(
            dxil["decompileRouteIds"],
            ["community-dxil-spirv", "spirv-cross-spirv-hlsl"],
        )
        self.assertEqual(
            dxil["compileRouteIds"], ["dxc-hlsl-dxil-exact-profile"]
        )
        legacy_dxil_spirv = catalog.matching_routes(
            "dxbc", "spirv", "fragment"
        )
        self.assertEqual(
            [item.route_id for item in legacy_dxil_spirv],
            ["community-dxbc-spirv"],
        )
        self.assertIn(
            "dxbc-spirv integration",
            " ".join(legacy_dxil_spirv[0].caveats),
        )
        self.assertIn(
            "reverse-compile probe",
            " ".join(legacy_dxil_spirv[0].caveats),
        )
        community = {
            item.implementation: item
            for item in catalog.routes()
            if item.implementation in {"3DMigoto cmd_Decompiler", "dxil-spirv"}
        }
        self.assertEqual(set(community), {"3DMigoto cmd_Decompiler", "dxil-spirv"})
        self.assertIn("GPL-3.0", community["3DMigoto cmd_Decompiler"].license_name)
        self.assertEqual(community["dxil-spirv"].license_name, "MIT")
        self.assertTrue(all(item.caveats for item in community.values()))
        generated_tools = json.loads(
            (generated_root / "tools.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            generated_tools["tools"]["dxil-spirv-transform"]["transform"]["args"],
            [
                "{input}",
                "--output",
                "{output}",
                "--validate",
                "--entry",
                "{entry_point}",
            ],
        )
        assembly_tool = generated_tools["tools"]["community-dxbc-assembly"]
        decompile_args = assembly_tool["decompile"]["args"]
        compile_args = assembly_tool["compile"]["args"]
        self.assertIn("--mode", decompile_args)
        self.assertIn("disassemble", decompile_args)
        self.assertIn("{raw_sha256}", decompile_args)
        self.assertIn("--mode", compile_args)
        self.assertIn("assemble", compile_args)
        self.assertIn("--baseline", compile_args)
        self.assertIn("{baseline}", compile_args)
        self.assertNotIn("-V", compile_args)
        self.assertTrue(
            any(
                path.endswith("dxbc_assembly_adapter.py")
                for path in assembly_tool["fixedFileHashes"]
            )
        )
        row = next(
            item
            for item in catalog.diagnostic_matrix("fragment")["matrix"]
            if item["inputFormat"] == "dxbc"
            and item["outputFormat"] == "dxbc-asm"
        )
        self.assertTrue(row["roundTripReady"])
        self.assertEqual("executable-editable", row["lane"])

    def test_renodx_direct_dxil_reconstruction_precedes_spirv_bridge(self) -> None:
        generated_root = self.root / "renodx-generated"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "decomp.exe",
            "dxcompiler.dll",
            "dxil-spirv.exe",
            "spirv-cross.exe",
            "dxc.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()

        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            renodx_decompiler=binaries["decomp.exe"],
            dxil_spirv=binaries["dxil-spirv.exe"],
            spirv_cross=binaries["spirv-cross.exe"],
            dxc=binaries["dxc.exe"],
        )
        registry = json.loads(
            (generated_root / "tools.json").read_text(encoding="utf-8")
        )
        operation = registry["tools"]["community-renodx-dxil-decompiler"]
        self.assertEqual(str(Path(sys.executable).resolve()), operation["executable"])
        arguments = operation["decompile"]["args"]
        self.assertTrue(arguments[0].endswith("renodx_decompiler_adapter.py"))
        self.assertEqual(
            str(binaries["decomp.exe"]), arguments[arguments.index("--tool") + 1]
        )
        self.assertEqual(
            hashlib.sha256(binaries["decomp.exe"].read_bytes()).hexdigest(),
            arguments[arguments.index("--tool-sha256") + 1],
        )
        self.assertEqual("{input}", arguments[arguments.index("--input") + 1])
        self.assertEqual("{output}", arguments[arguments.index("--output") + 1])
        self.assertEqual(
            "{workspace_dir}", arguments[arguments.index("--workspace") + 1]
        )
        self.assertEqual(
            "{raw_sha256}", arguments[arguments.index("--raw-sha256") + 1]
        )
        self.assertEqual(
            "{entry_point}", arguments[arguments.index("--entry") + 1]
        )
        self.assertEqual("{stage}", arguments[arguments.index("--stage") + 1])
        self.assertEqual(
            hashlib.sha256(binaries["decomp.exe"].read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(binaries["decomp.exe"])],
        )
        self.assertEqual(
            hashlib.sha256(binaries["dxcompiler.dll"].read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(binaries["dxcompiler.dll"])],
        )
        self.assertTrue(
            any(
                path.endswith("renodx_decompiler_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        candidates = catalog.candidate_paths("dxil", "hlsl", "fragment")
        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(
            [route.route_id for route in candidates[0]],
            ["community-renodx-dxil-hlsl"],
        )
        self.assertEqual(
            [route.route_id for route in candidates[1]],
            ["community-dxil-spirv", "spirv-cross-spirv-hlsl"],
        )
        selected = catalog.select_round_trip(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual(
            ["community-renodx-dxil-hlsl"],
            selected["decompileRouteIds"],
        )
        direct = candidates[0][0]
        self.assertEqual("reconstructed-source", direct.fidelity)
        self.assertEqual("RenoDX decomp.exe", direct.implementation)
        self.assertIn("MIT", direct.license_name)
        self.assertIn("Third-party/community", " ".join(direct.caveats))
        self.assertIn("not proof of semantic equivalence", " ".join(direct.caveats))

        missing_root = self.root / "renodx-missing-dependency"
        missing_root.mkdir()
        lone_decompiler = missing_root / "decomp.exe"
        lone_decompiler.write_bytes(b"lone provider")
        with self.assertRaisesRegex(
            ToolConfigurationError, "side-by-side dxcompiler.dll"
        ):
            generate_official_toolchain_configs(
                missing_root / "tools.json",
                missing_root / "routes.json",
                renodx_decompiler=lone_decompiler,
            )

    def test_official_dxc_ir_assembler_is_an_executable_round_trip(self) -> None:
        generated_root = self.root / "dxc-assembler-generated"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        dxcompiler = generated_root / "dxcompiler.dll"
        dxil = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc cli")
        dxcompiler.write_bytes(b"fake official dxc runtime")
        dxil.write_bytes(b"fake optional validator runtime")
        tool_path = generated_root / "tools.json"
        route_path = generated_root / "routes.json"
        generate_official_toolchain_configs(
            tool_path,
            route_path,
            dxc=dxc,
        )

        registry = json.loads(tool_path.read_text(encoding="utf-8"))
        operation = registry["tools"]["dxc-official-dxil-assembler"]
        arguments = operation["compile"]["args"]
        self.assertEqual(str(Path(sys.executable).resolve()), operation["executable"])
        self.assertTrue(arguments[0].endswith("dxil_assembler_adapter.py"))
        self.assertEqual(str(dxcompiler.resolve()), arguments[arguments.index("--dll") + 1])
        self.assertEqual(
            hashlib.sha256(dxcompiler.read_bytes()).hexdigest(),
            arguments[arguments.index("--dll-sha256") + 1],
        )
        self.assertEqual("{input}", arguments[arguments.index("--input") + 1])
        self.assertEqual("{output}", arguments[arguments.index("--output") + 1])
        self.assertNotIn("{entry_point}", arguments)
        self.assertNotIn("{target_profile}", arguments)
        self.assertEqual(
            hashlib.sha256(dxcompiler.read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(dxcompiler.resolve())],
        )
        self.assertEqual(
            hashlib.sha256(dxil.read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(dxil.resolve())],
        )
        self.assertTrue(
            any(
                path.endswith("dxil_assembler_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

        catalog = ToolchainCatalog(tool_path, route_path)
        assembly_route = catalog.matching_routes(
            "dxil-asm", "dxil", "fragment"
        )
        self.assertEqual(
            ["dxc-dxil-assemble-to-container"],
            [route.route_id for route in assembly_route],
        )
        self.assertEqual("ir-assembly", assembly_route[0].fidelity)
        self.assertIn("Microsoft DXC", assembly_route[0].implementation)
        self.assertIn("do not prove", " ".join(assembly_route[0].caveats))
        round_trip = catalog.select_round_trip(
            "dxil",
            "fragment",
            editable_format="dxil-asm",
            target_api="d3d12",
        )
        self.assertEqual(
            ["dxc-dxil-native-disassembly"],
            round_trip["decompileRouteIds"],
        )
        self.assertEqual(
            ["dxc-dxil-assemble-to-container"],
            round_trip["compileRouteIds"],
        )
        self.assertFalse(round_trip["containsReconstructedSource"])
        self.assertFalse(round_trip["semanticEquivalenceVerified"])
        automatic = catalog.select_round_trip(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual("dxil-asm", automatic["editableFormat"])
        self.assertEqual(
            ["dxc-dxil-native-disassembly"],
            automatic["decompileRouteIds"],
        )
        recovery = catalog.plan_automatic_recovery(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual("round-trip", recovery["mode"])
        self.assertEqual("dxil-asm", recovery["editableFormat"])
        self.assertEqual(
            "automatic-native-ir-round-trip-fallback",
            recovery["diagnostics"][0]["category"],
        )
        shader_path = generated_root / "captured.dxil"
        shader_path.write_bytes(
            make_container(b"DXIL", make_dxil_program(0x00000060))
        )
        file_recovery = catalog.plan_recovery_for_file(
            shader_path,
            "fragment",
            target_api="d3d12",
        )
        self.assertEqual("round-trip", file_recovery["mode"])
        self.assertEqual("dxil-asm", file_recovery["editableFormat"])

        unavailable = self.root / "dxc-assembler-unavailable"
        unavailable.mkdir()
        lone_dxc = unavailable / "dxc.exe"
        lone_dxc.write_bytes(b"dxc without runtime")
        generate_official_toolchain_configs(
            unavailable / "tools.json",
            unavailable / "routes.json",
            dxc=lone_dxc,
        )
        unavailable_registry = json.loads(
            (unavailable / "tools.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "dxc-official-dxil-assembler", unavailable_registry["tools"]
        )
        unavailable_catalog = ToolchainCatalog(
            unavailable / "tools.json", unavailable / "routes.json"
        )
        self.assertEqual(
            (),
            unavailable_catalog.matching_routes(
                "dxil-asm", "dxil", "fragment"
            ),
        )

    def test_automatic_native_ir_fallback_rejects_multihop_and_ambiguity(self) -> None:
        generated_root = self.root / "dxc-automatic-fallback-safety"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        dxcompiler = generated_root / "dxcompiler.dll"
        dxil = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc cli")
        dxcompiler.write_bytes(b"fake official dxc runtime")
        dxil.write_bytes(b"fake optional validator runtime")
        tool_path = generated_root / "tools.json"
        route_path = generated_root / "routes.json"
        generate_official_toolchain_configs(tool_path, route_path, dxc=dxc)
        original = json.loads(route_path.read_text(encoding="utf-8"))
        dump = next(
            route
            for route in original["routes"]
            if route["routeId"] == "dxc-dxil-native-disassembly"
        )
        assemble = next(
            route
            for route in original["routes"]
            if route["routeId"] == "dxc-dxil-assemble-to-container"
        )

        strange = json.loads(json.dumps(original))
        strange["routes"] = [
            route
            for route in strange["routes"]
            if route["routeId"]
            not in {
                "dxc-dxil-native-disassembly",
                "dxc-dxil-assemble-to-container",
            }
        ]
        # Keep every synthetic edge valid under the route schema while making
        # the apparent round trip depend on unrelated, multi-hop formats.  A
        # decompile operation must start from a binary format and a compile
        # operation must end at one, hence the SPIR-V bridge in each direction.
        for route_id, source, target, template, fidelity in (
            ("strange-dxil-slang", "dxil", "slang", dump, "custom-transform"),
            ("strange-slang-spirv", "slang", "spirv", assemble, "custom-transform"),
            ("strange-spirv-ir", "spirv", "dxil-asm", dump, "ir-disassembly"),
            ("strange-ir-spirv", "dxil-asm", "spirv", assemble, "custom-transform"),
            ("strange-spirv-slang", "spirv", "slang", dump, "custom-transform"),
            ("strange-slang-dxil", "slang", "dxil", assemble, "ir-assembly"),
        ):
            route = json.loads(json.dumps(template))
            route.update(
                {
                    "routeId": route_id,
                    "inputFormat": source,
                    "outputFormat": target,
                    "fidelity": fidelity,
                }
            )
            strange["routes"].append(route)
        strange_path = generated_root / "routes-strange.json"
        atomic_write_json(strange_path, strange)
        strange_plan = ToolchainCatalog(
            tool_path, strange_path
        ).plan_automatic_recovery("dxil", "fragment", target_api="d3d12")
        self.assertEqual("compile-only-recovery", strange_plan["mode"])
        self.assertEqual("hlsl", strange_plan["editableFormat"])

        ambiguous = json.loads(json.dumps(original))
        duplicate = json.loads(json.dumps(dump))
        duplicate["routeId"] = "dxc-dxil-native-disassembly-alternate"
        ambiguous["routes"].append(duplicate)
        ambiguous_path = generated_root / "routes-ambiguous.json"
        atomic_write_json(ambiguous_path, ambiguous)
        ambiguous_plan = ToolchainCatalog(
            tool_path, ambiguous_path
        ).plan_automatic_recovery("dxil", "fragment", target_api="d3d12")
        self.assertEqual("compile-only-recovery", ambiguous_plan["mode"])
        self.assertEqual("hlsl", ambiguous_plan["editableFormat"])

if __name__ == "__main__":
    unittest.main()
