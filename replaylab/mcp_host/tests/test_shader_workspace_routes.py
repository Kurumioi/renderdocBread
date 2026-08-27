from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest

from src.shader_workspace import (
    ManifestValidationError,
    ShaderKey,
    ShaderWorkspaceError,
    ShaderWorkspaceService,
    ToolchainCatalog,
    ToolchainRoutingError,
    ToolExecutionError,
)
from src.shader_workspace.catalog import capture_identity
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.leases import (
    SOURCE_TRANSACTION_BACKUP_FILE,
    SOURCE_TRANSACTION_JOURNAL_FILE,
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry
from src.shader_workspace.watcher import ShaderWorkspaceWatchManager
from src.shader_workspace.workspace import _reconstruction_fidelity_status


def make_container(chunks: list[tuple[bytes, bytes]]) -> bytes:
    header_size = 32 + 4 * len(chunks)
    offsets = []
    encoded = []
    cursor = header_size
    for fourcc, payload in chunks:
        value = fourcc + struct.pack("<I", len(payload)) + payload
        offsets.append(cursor)
        encoded.append(value)
        cursor += len(value)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", value) for value in offsets)
        + b"".join(encoded)
    )


def make_dxil() -> bytes:
    bitcode = b"BC\xc0\xdefixed-dxil-test-bitcode"
    unpadded_size = 24 + len(bitcode)
    padded_size = (unpadded_size + 3) & ~3
    # ProgramVersion 0x60 is pixel/fragment Shader Model 6.0.
    program = (
        struct.pack(
            "<6I",
            0x00000060,
            padded_size // 4,
            0x4C495844,
            0x00000100,
            16,
            len(bitcode),
        )
        + bitcode
        + b"\x00" * (padded_size - unpadded_size)
    )
    return make_container([(b"DXIL", program)])


FAKE_ROUTE_TOOL = r'''from pathlib import Path
import shutil
import struct
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
if mode in {"dxil-to-spirv", "hlsl-to-spirv"}:
    output_path.write_bytes(struct.pack("<5I", 0x07230203, 0x00010000, 0, 1, 0))
elif mode == "dxil-to-asm":
    output_path.write_text(
        'target triple = "dxil-ms-dx"\n; fixed native IR fixture\n',
        encoding="utf-8",
    )
elif mode == "asm-to-dxil":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "spirv-to-hlsl":
    output_path.write_text(
        "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
        encoding="utf-8",
    )
elif mode == "spirv-to-hlsl-fail":
    raise SystemExit(19)
elif mode == "spirv-to-dxil":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "spirv-to-dxil-fail":
    raise SystemExit(23)
else:
    raise SystemExit(3)
'''


class ShaderWorkspaceRouteChainTests(unittest.TestCase):
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
        self.script = self.root / "route_tool.py"
        self.script.write_text(FAKE_ROUTE_TOOL, encoding="utf-8")
        executable = Path(sys.executable).resolve()
        base = {
            "executable": str(executable),
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "fixedFileHashes": {
                str(self.script.resolve()): hashlib.sha256(
                    self.script.read_bytes()
                ).hexdigest()
            },
        }

        def operation(name: str, operation_name: str, args: list[str]) -> dict:
            record = dict(base)
            record[operation_name] = {"args": args, "timeoutSeconds": 10}
            return record

        script = str(self.script.resolve())
        self.tools_path = self.root / "shader-tools.v1.json"
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {
                    "dxil-spv": operation(
                        "dxil-spv",
                        "transform",
                        [script, "dxil-to-spirv", "{input}", "{output}"],
                    ),
                    "spv-hlsl": operation(
                        "spv-hlsl",
                        "decompile",
                        [script, "spirv-to-hlsl", "{input}", "{output}"],
                    ),
                    "spv-hlsl-fail": operation(
                        "spv-hlsl-fail",
                        "decompile",
                        [script, "spirv-to-hlsl-fail", "{input}", "{output}"],
                    ),
                    "hlsl-spv": operation(
                        "hlsl-spv",
                        "compile",
                        [
                            script,
                            "hlsl-to-spirv",
                            "{input}",
                            "{output}",
                            "{target_profile}",
                        ],
                    ),
                    "spv-dxil": operation(
                        "spv-dxil",
                        "transform",
                        [
                            script,
                            "spirv-to-dxil",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                    "spv-dxil-fail": operation(
                        "spv-dxil-fail",
                        "transform",
                        [
                            script,
                            "spirv-to-dxil-fail",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                    "dxil-asm": operation(
                        "dxil-asm",
                        "decompile",
                        [script, "dxil-to-asm", "{input}", "{output}"],
                    ),
                    "asm-dxil": operation(
                        "asm-dxil",
                        "compile",
                        [
                            script,
                            "asm-to-dxil",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                },
            },
        )
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="test-dxil-spirv",
                tool_id="dxil-spv",
                operation="transform",
                input_format="dxil",
                output_format="spirv",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixture DXIL transform",
                source_url="https://example.invalid/dxil-spv",
            ),
            route_record(
                registry,
                route_id="test-spirv-hlsl",
                tool_id="spv-hlsl",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=10,
                implementation="fixture source recovery",
                source_url="https://example.invalid/spv-hlsl",
                caveats=["Fixture HLSL is reconstructed."],
            ),
            route_record(
                registry,
                route_id="test-spirv-hlsl-fail",
                tool_id="spv-hlsl-fail",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=500,
                implementation="fixture failing recovery",
                source_url="https://example.invalid/spv-hlsl-fail",
                caveats=["This route deliberately fails."],
            ),
            route_record(
                registry,
                route_id="test-hlsl-spirv",
                tool_id="hlsl-spv",
                operation="compile",
                input_format="hlsl",
                output_format="spirv",
                stages=["fragment"],
                fidelity="native-compile",
                priority=10,
                implementation="fixture HLSL compiler",
                source_url="https://example.invalid/hlsl-spv",
            ),
            route_record(
                registry,
                route_id="test-spirv-dxil",
                tool_id="spv-dxil",
                operation="transform",
                input_format="spirv",
                output_format="dxil",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixture DXIL packer",
                source_url="https://example.invalid/spv-dxil",
            ),
            route_record(
                registry,
                route_id="test-spirv-dxil-fail",
                tool_id="spv-dxil-fail",
                operation="transform",
                input_format="spirv",
                output_format="dxil",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=500,
                implementation="fixture failing DXIL packer",
                source_url="https://example.invalid/spv-dxil-fail",
            ),
            route_record(
                registry,
                route_id="test-dxil-native-disassembly",
                tool_id="dxil-asm",
                operation="decompile",
                input_format="dxil",
                output_format="dxil-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixture DXIL native disassembly",
                source_url="https://example.invalid/dxil-asm",
            ),
            route_record(
                registry,
                route_id="test-dxil-native-assembly",
                tool_id="asm-dxil",
                operation="compile",
                input_format="dxil-asm",
                output_format="dxil",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture DXIL native assembly",
                source_url="https://example.invalid/asm-dxil",
            ),
        ]
        self.routes_path = self.root / "shader-toolchains.v1.json"
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        self.blob = make_dxil()
        self.key = ShaderKey(
            91,
            "fragment",
            "main",
            hashlib.sha256(self.blob).hexdigest(),
        )
        catalog_dir = self.root / "capture-catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (self.key.raw_sha256 + ".bin")).write_bytes(
            self.blob
        )
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/dxil-frame.rdc",
            "api": "D3D12",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 44,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> ShaderWorkspaceService:
        return ShaderWorkspaceService(self.tools_path, self.routes_path)

    def test_dxil_two_hop_workspace_binds_and_executes_both_directions(self) -> None:
        workspace = self.root / "workspace-two-hop"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, workspace
        )
        self.assertEqual(
            [item["capability"]["routeId"] for item in created["routePlan"]["decompile"]],
            ["test-dxil-spirv", "test-spirv-hlsl"],
        )
        self.assertEqual(
            [item["capability"]["routeId"] for item in created["routePlan"]["compile"]],
            ["test-hlsl-spirv", "test-spirv-dxil"],
        )
        self.assertEqual(
            created["routePlan"]["routeCatalogSha256"],
            hashlib.sha256(self.routes_path.read_bytes()).hexdigest(),
        )

        decompiled = self.service().decompile(workspace)
        self.assertEqual(len(decompiled["jobPaths"]), 2)
        self.assertTrue(decompiled["baselineCompileVerified"])
        self.assertEqual(len(decompiled["baselineCompileJobPaths"]), 2)
        self.assertEqual(
            decompiled["routeIds"], ["test-dxil-spirv", "test-spirv-hlsl"]
        )
        source = Path(decompiled["sourcePath"])
        self.assertIn("SV_Target", source.read_text(encoding="utf-8"))
        untouched_status = self.service().status(workspace)
        untouched_fidelity = untouched_status["reconstructionFidelity"]
        self.assertTrue(untouched_fidelity["available"])
        self.assertTrue(untouched_fidelity["baselineCompileVerified"])
        self.assertTrue(untouched_fidelity["codeSha256Equal"])
        self.assertTrue(
            untouched_fidelity["currentSourceMatchesUntouchedReconstruction"]
        )
        self.assertFalse(
            untouched_fidelity["currentCandidateMatchesUntouchedReconstruction"]
        )
        self.assertFalse(
            untouched_fidelity["currentCandidateReplayAbiCompatible"]
        )
        self.assertEqual(
            untouched_fidelity["classification"],
            "exact-executable-code-match",
        )
        self.assertFalse(untouched_fidelity["semanticEquivalenceVerified"])
        source.write_text(
            source.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8"
        )

        compiled = self.service().compile(workspace)
        self.assertEqual(len(compiled["jobPaths"]), 2)
        self.assertEqual(
            compiled["routeIds"], ["test-hlsl-spirv", "test-spirv-dxil"]
        )
        self.assertEqual(Path(compiled["candidateBlobPath"]).read_bytes(), self.blob)
        status = ShaderWorkspaceService(
            self.tools_path, self.routes_path
        ).status(workspace)
        self.assertTrue(status["source"]["baselineCompileVerified"])
        self.assertTrue(status["representation"]["baselineCompileVerified"])
        self.assertEqual(status["targetProfile"]["profile"], "ps_6_0")
        self.assertEqual(
            status["latestCompiled"]["compileRouteIds"],
            ["test-hlsl-spirv", "test-spirv-dxil"],
        )
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        # This fixture has no captured ABI, so a physical access proof would
        # have no safe consumer. Ordinary HLSL routing remains unchanged and
        # does not manufacture a DXIL disassembly evidence claim.
        self.assertEqual("unavailable", manifest["baseline"]["abiStatus"])
        self.assertNotIn("dxilDisassemblyEvidence", manifest)
        self.assertNotIn(
            "baselineDxilDisassemblyEvidence",
            next(
                entry
                for entry in manifest["history"]
                if entry.get("operation") == "decompile-route-chain"
            ),
        )

    def test_reconstruction_fidelity_exposes_abi_compatible_code_mismatch(self) -> None:
        captured_code = "a" * 64
        reconstructed_code = "b" * 64
        source_sha = "c" * 64
        manifest = {"baseline": {"codeSha256": captured_code}}
        source_record = {
            "origin": "trusted-decompile-route",
            "baselineCompile": {
                "codeSha256": reconstructed_code,
                # Raw identity deliberately does not participate in this gate.
                "rawSha256": "d" * 64,
            },
        }
        source_result = {
            "baselineCompileVerified": True,
            "decompiledSha256": source_sha,
            "currentSha256": source_sha,
        }
        latest = {
            "sourceSha256": source_sha,
            "rawSha256": "e" * 64,
            "codeSha256": reconstructed_code,
            "requiresReplayAbiValidation": False,
            "replayAbiValidated": True,
            "abiCompatible": True,
            "abiGate": {
                "status": "compatible",
                "requestId": "1" * 64,
                "reportSha256": "2" * 64,
            },
        }

        fidelity = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )

        self.assertTrue(fidelity["available"])
        self.assertFalse(fidelity["codeSha256Equal"])
        self.assertTrue(fidelity["currentCandidateReplayAbiCompatible"])
        self.assertEqual(
            fidelity["classification"],
            "abi-compatible-executable-code-mismatch",
        )
        self.assertFalse(fidelity["semanticEquivalenceVerified"])
        latest["rawSha256"] = "f" * 64
        self.assertEqual(
            _reconstruction_fidelity_status(
                manifest, source_record, source_result, latest
            )["classification"],
            "abi-compatible-executable-code-mismatch",
        )
        latest["requiresReplayAbiValidation"] = True
        pending = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )
        self.assertFalse(pending["currentCandidateReplayAbiCompatible"])
        self.assertEqual(pending["classification"], "executable-code-mismatch")
        latest["requiresReplayAbiValidation"] = False
        latest["abiGate"]["reportSha256"] = "not-a-bound-report"
        unbound = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )
        self.assertFalse(unbound["currentCandidateReplayAbiCompatible"])
        self.assertEqual(unbound["classification"], "executable-code-mismatch")

    def test_failed_second_hop_never_publishes_source(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selection = catalog.select_round_trip(
            "dxil", "fragment", editable_format="hlsl", target_api="d3d12"
        )
        selection["decompileRouteIds"] = [
            "test-dxil-spirv",
            "test-spirv-hlsl-fail",
        ]
        workspace = self.root / "workspace-failed-hop"
        self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            toolchain_catalog=catalog,
            toolchain_selection=selection,
        )
        with self.assertRaises(ToolExecutionError):
            self.service().decompile(workspace)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["history"][-1]["status"], "failed")
        self.assertEqual(
            [item["status"] for item in manifest["history"][-1]["hops"]],
            ["succeeded", "failed"],
        )

    def test_default_recovers_to_complete_native_ir_before_compile_only_hlsl(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        document["routes"] = [
            route
            for route in document["routes"]
            if route["routeId"]
            not in {
                "test-dxil-spirv",
                "test-spirv-hlsl",
                "test-spirv-hlsl-fail",
            }
        ]
        atomic_write_json(self.routes_path, document)

        service = self.service()
        automatic_workspace = self.root / "workspace-automatic-native-ir"
        automatic = service.create(
            self.catalog_path, self.key.catalog_key, automatic_workspace
        )
        self.assertEqual("dxil-asm", automatic["routePlan"]["editableFormat"])
        self.assertEqual("round-trip", automatic["recoveryPlan"]["mode"])
        self.assertEqual(
            "automatic-native-ir-round-trip-fallback",
            automatic["recoveryPlan"]["diagnostics"][0]["category"],
        )
        decompiled = service.decompile(automatic_workspace)
        self.assertTrue(Path(decompiled["sourcePath"]).name.endswith(".ll"))
        self.assertTrue(decompiled["baselineCompileVerified"])
        self.assertEqual(
            "executable-editable",
            service.status(automatic_workspace)["representation"]["lane"],
        )

        explicit_workspace = self.root / "workspace-explicit-hlsl"
        explicit = service.create(
            self.catalog_path,
            self.key.catalog_key,
            explicit_workspace,
            editable_format="hlsl",
        )
        self.assertEqual("hlsl", explicit["routePlan"]["editableFormat"])
        self.assertEqual("compile-only-recovery", explicit["recoveryPlan"]["mode"])
        self.assertEqual([], explicit["routePlan"]["decompile"])

    def test_failed_untouched_baseline_compile_never_publishes_source(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selection = catalog.select_round_trip(
            "dxil", "fragment", editable_format="hlsl", target_api="d3d12"
        )
        selection["compileRouteIds"] = [
            "test-hlsl-spirv",
            "test-spirv-dxil-fail",
        ]
        workspace = self.root / "workspace-baseline-compile-failed"
        self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            toolchain_catalog=catalog,
            toolchain_selection=selection,
        )
        with self.assertRaises(ToolExecutionError):
            self.service().decompile(workspace)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        history = manifest["history"][-1]
        self.assertEqual(history["status"], "failed")
        self.assertEqual([item["status"] for item in history["hops"]], ["succeeded", "succeeded"])
        probe = history["baselineCompileProbe"]
        self.assertEqual(probe["status"], "failed")
        self.assertEqual(
            [item["status"] for item in probe["hops"]],
            ["succeeded", "failed"],
        )

    def test_compile_only_recovery_workspace_promotes_hash_bound_source(self) -> None:
        routes = json.loads(self.routes_path.read_text(encoding="utf-8"))
        routes["routes"] = [
            route
            for route in routes["routes"]
            if route["inputFormat"] == "hlsl"
            or (route["inputFormat"] == "spirv" and route["outputFormat"] == "dxil")
        ]
        atomic_write_json(self.routes_path, routes)

        workspace = self.root / "workspace-compile-only"
        service = self.service()
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        self.assertEqual("compile-only-recovery", created["recoveryPlan"]["mode"])
        self.assertEqual([], created["routePlan"]["decompile"])
        self.assertEqual(
            ["test-hlsl-spirv", "test-spirv-dxil"],
            [
                item["capability"]["routeId"]
                for item in created["routePlan"]["compile"]
            ],
        )

        represented = service.representation(workspace)
        self.assertEqual("binary-only", represented["lane"])
        candidate = workspace / "explanation" / "promoted.hlsl"
        source = b"float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n"
        candidate.write_bytes(source)
        promoted = service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=represented["sha256"],
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
            acknowledge_semantic_equivalence_unverified=True,
        )
        self.assertEqual("executable-editable", promoted["lane"])
        compiled = service.compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())
        status = service.status(workspace)
        self.assertTrue(status["source"]["compileEligible"])
        self.assertFalse(status["semanticEquivalenceVerified"])
        self.assertFalse(status["applied"])

    def test_compile_only_recovery_promotes_instruction_assembly(self) -> None:
        registry = ToolRegistry(self.tools_path)
        original_routes = json.loads(
            self.routes_path.read_text(encoding="utf-8")
        )["routes"]
        routes = [
            route_record(
                registry,
                route_id="test-dxil-assembly-spirv",
                tool_id="hlsl-spv",
                operation="compile",
                input_format="dxil-asm",
                output_format="spirv",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture DXIL assembly compiler",
                source_url="https://example.invalid/dxil-assembly",
            ),
            next(
                route
                for route in original_routes
                if route["routeId"] == "test-spirv-dxil"
            ),
        ]
        atomic_write_json(
            self.routes_path,
            toolchain_document(self.tools_path, routes, []),
        )
        workspace = self.root / "workspace-compile-only-assembly"
        service = self.service()
        created = service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxil-asm",
        )
        self.assertEqual("compile-only-recovery", created["recoveryPlan"]["mode"])
        self.assertTrue(created["recoveryPlan"]["sourceConstructionEligible"])
        compiler_attempt = service.create_recovery_attempt(
            workspace,
            self.root / "workspace-compile-only-assembly-attempt",
            expected_recovery_plan_sha256=created["recoveryPlan"]["planSha256"],
            expected_source_workspace_id=created["workspaceId"],
            expected_baseline_raw_sha256=created["baselineRawSha256"],
            expected_capture_id=created["captureId"],
            decompile_candidate_index=None,
            compile_candidate_index=0,
        )
        self.assertEqual([], compiler_attempt["routePlan"]["decompile"])
        self.assertTrue(compiler_attempt["routePlan"]["compile"])
        represented = service.representation(workspace)
        source = b"target triple = \"dxil-ms-dx\"\n; reviewed fixture IR\n"
        candidate = workspace / "explanation" / "promoted.ll"
        candidate.write_bytes(source)
        promoted = service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=represented["sha256"],
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
            acknowledge_semantic_equivalence_unverified=True,
        )
        self.assertTrue(Path(promoted["sourcePath"]).name.endswith(".ll"))
        self.assertEqual("dxil-asm", service.status(workspace)["source"]["format"])
        compiled = service.compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())

    def test_one_way_decompiler_publishes_explanatory_tool_ir(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        document["routes"] = [
            route
            for route in document["routes"]
            if route["routeId"] in {"test-dxil-spirv", "test-spirv-hlsl"}
        ]
        atomic_write_json(self.routes_path, document)

        workspace = self.root / "workspace-one-way-explanation"
        service = self.service()
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        self.assertEqual("evidence-only", created["recoveryPlan"]["mode"])
        decompiler_action = next(
            action
            for action in created["recoveryPlan"]["allowedActions"]
            if action["id"] == "run-registered-decompiler"
        )
        self.assertFalse(decompiler_action["requiresCompileRoute"])
        self.assertFalse(decompiler_action["producesExecutableSource"])
        self.assertTrue(decompiler_action["producesExplanatoryIr"])
        self.assertEqual([], created["routePlan"]["compile"])
        self.assertEqual(
            ["test-dxil-spirv", "test-spirv-hlsl"],
            [
                item["capability"]["routeId"]
                for item in created["routePlan"]["decompile"]
            ],
        )

        result = service.decompile(workspace)
        self.assertEqual("explanatory-ir", result["lane"])
        self.assertFalse(result["compileEligible"])
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        representation = service.status(workspace)["representation"]
        self.assertEqual("trusted-tool-one-way-decompile", representation["kind"])
        self.assertFalse(representation["compileEligible"])
        self.assertEqual(
            ["test-dxil-spirv", "test-spirv-hlsl"],
            representation["provenance"]["routeIds"],
        )
        self.assertIn(
            "SV_Target", Path(representation["path"]).read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(ToolExecutionError, "executable-editable"):
            service.compile(workspace)

        descriptor_path = workspace / "explanation" / "representation.json"
        original = json.loads(descriptor_path.read_text(encoding="utf-8"))
        forged_records = []
        forged_kind = json.loads(json.dumps(original))
        forged_kind["kind"] = "forged-claim"
        forged_records.append(forged_kind)
        forged_route = json.loads(json.dumps(original))
        forged_route["provenance"]["routeIds"] = ["not-a-real-route"]
        forged_records.append(forged_route)
        forged_job = json.loads(json.dumps(original))
        forged_job["provenance"]["producedByJobs"] = ["jobs/forged"] * 2
        forged_records.append(forged_job)
        for forged in forged_records:
            atomic_write_json(descriptor_path, forged)
            with self.assertRaises(ManifestValidationError):
                service.status(workspace)
        atomic_write_json(descriptor_path, original)

        manifest_path = workspace / "manifest.json"
        manifest_original = json.loads(manifest_path.read_text(encoding="utf-8"))
        forged_manifest = json.loads(json.dumps(manifest_original))
        forged_hop = forged_manifest["history"][-1]["hops"][0]
        forged_hop["toolId"] = "forged-tool"
        forged_hop["toolConfigDigest"] = "0" * 64
        forged_hop["routeBinding"]["operation"] = {"forged": True}
        atomic_write_json(manifest_path, forged_manifest)
        with self.assertRaises(ManifestValidationError):
            service.status(workspace)
        atomic_write_json(manifest_path, manifest_original)

    def test_recovery_attempt_selects_only_a_listed_alternative_in_new_workspace(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in document["routes"]:
            if route["routeId"] == "test-spirv-hlsl-fail":
                route["priority"] = 1
            elif route["routeId"] == "test-spirv-hlsl":
                route["priority"] = 20
        atomic_write_json(self.routes_path, document)

        service = self.service()
        source_workspace = self.root / "workspace-failed-primary"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        candidates = created["recoveryPlan"]["decompileCandidates"]
        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual("test-spirv-hlsl-fail", candidates[0][-1])
        failed_representation = service.representation(source_workspace)
        self.assertEqual("binary-only", failed_representation["lane"])

        attempt_workspace = self.root / "workspace-alternative-attempt"
        attempted = service.create_recovery_attempt(
            source_workspace,
            attempt_workspace,
            expected_recovery_plan_sha256=created["recoveryPlan"]["planSha256"],
            expected_source_workspace_id=created["workspaceId"],
            expected_baseline_raw_sha256=created["baselineRawSha256"],
            expected_capture_id=created["captureId"],
            decompile_candidate_index=1,
        )
        self.assertEqual(
            candidates[1],
            [
                hop["capability"]["routeId"]
                for hop in attempted["routePlan"]["decompile"]
            ],
        )
        self.assertEqual(
            source_workspace.resolve(),
            Path(
                attempted["recoveryAttempt"]["sourceWorkspacePath"]
            ).resolve(),
        )
        recovered = service.representation(attempt_workspace)
        self.assertEqual("executable-editable", recovered["lane"])
        status = json.loads(
            (attempt_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "recovery-candidate-attempt", status["history"][0]["operation"]
        )

        with self.assertRaisesRegex(
            ToolchainRoutingError, "outside the fixed recovery plan"
        ):
            service.create_recovery_attempt(
                source_workspace,
                self.root / "workspace-invalid-attempt",
                expected_recovery_plan_sha256=created["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=999,
            )
        with self.assertRaisesRegex(ToolchainRoutingError, "plan changed"):
            service.create_recovery_attempt(
                source_workspace,
                self.root / "workspace-stale-plan-attempt",
                expected_recovery_plan_sha256="0" * 64,
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=1,
            )

        nested_attempt = source_workspace / "nested-recovery-attempt"
        with self.assertRaisesRegex(
            ShaderWorkspaceError, "outside the failed source workspace"
        ):
            service.create_recovery_attempt(
                source_workspace,
                nested_attempt,
                expected_recovery_plan_sha256=created["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=1,
            )
        self.assertFalse(nested_attempt.exists())

    def test_recovery_attempt_catalog_swap_never_publishes_target(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in document["routes"]:
            if route["routeId"] == "test-spirv-hlsl-fail":
                route["priority"] = 1
            elif route["routeId"] == "test-spirv-hlsl":
                route["priority"] = 20
        atomic_write_json(self.routes_path, document)

        service = self.service()
        source_workspace = self.root / "workspace-catalog-swap-source"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        original_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        original_catalog_for_create = service._catalog_for_create
        swapped = False

        def swap_capture_before_fresh_plan(supplied):
            nonlocal swapped
            if not swapped:
                swapped = True
                replacement = json.loads(json.dumps(original_catalog))
                replacement["capturePath"] = "D:/captures/different-frame.rdc"
                replacement["captureId"] = capture_identity(replacement, [self.key])
                atomic_write_json(self.catalog_path, replacement)
            return original_catalog_for_create(supplied)

        service._catalog_for_create = swap_capture_before_fresh_plan
        target = self.root / "workspace-catalog-swap-target"
        try:
            with self.assertRaisesRegex(
                ToolchainRoutingError, "different capture/shader identity"
            ):
                service.create_recovery_attempt(
                    source_workspace,
                    target,
                    expected_recovery_plan_sha256=created["recoveryPlan"][
                        "planSha256"
                    ],
                    expected_source_workspace_id=created["workspaceId"],
                    expected_baseline_raw_sha256=created["baselineRawSha256"],
                    expected_capture_id=created["captureId"],
                    decompile_candidate_index=1,
                )
        finally:
            atomic_write_json(self.catalog_path, original_catalog)
        self.assertFalse(target.exists())

    def test_route_catalog_reload_and_manifest_chain_tamper_fail_closed(self) -> None:
        workspace = self.root / "workspace-tamper"
        self.service().create(self.catalog_path, self.key.catalog_key, workspace)
        ShaderWorkspaceService(self.tools_path, self.routes_path).status(workspace)

        original_catalog = self.routes_path.read_bytes()
        self.routes_path.write_bytes(original_catalog + b"\n")
        with self.assertRaises(ManifestValidationError):
            self.service().status(workspace)
        self.routes_path.write_bytes(original_catalog)

        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["routePlan"]["compile"][0]["capability"]["routeId"] = (
            "test-spirv-dxil"
        )
        atomic_write_json(manifest_path, manifest)
        with self.assertRaises(ManifestValidationError):
            self.service().status(workspace)

    def test_toolchain_upgrade_migrates_exact_source_without_old_candidate(self) -> None:
        source_workspace = self.root / "workspace-before-toolchain-upgrade"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// preserved edit\n")
        compiled = self.service().compile(source_workspace)
        self.assertEqual(compiled["revision"], 1)
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest_path = source_workspace / "manifest.json"
        source_manifest_before = source_manifest_path.read_bytes()

        upgraded_registry_document = json.loads(
            self.tools_path.read_text(encoding="utf-8")
        )
        upgraded_registry_document["tools"]["hlsl-spv"]["compile"][
            "timeoutSeconds"
        ] = 11
        atomic_write_json(self.tools_path, upgraded_registry_document)
        upgraded_registry = ToolRegistry(self.tools_path)

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        upgraded_catalog["toolRegistrySha256"] = hashlib.sha256(
            self.tools_path.read_bytes()
        ).hexdigest()
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
            if route["toolId"] == "hlsl-spv":
                route["toolConfigDigest"] = upgraded_registry.operation(
                    route["toolId"], route["operation"]
                ).config_digest
        atomic_write_json(self.routes_path, upgraded_catalog)
        with self.assertRaisesRegex(
            ManifestValidationError, "route catalog bytes changed"
        ):
            self.service().status(source_workspace)

        target_workspace = self.root / "workspace-after-toolchain-upgrade"
        migrated = self.service().migrate_toolchain(
            source_workspace,
            target_workspace,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=source_hash,
        )

        self.assertEqual(migrated["revision"], 0)
        self.assertFalse(migrated["latestCandidateImported"])
        self.assertFalse(migrated["semanticEquivalenceVerified"])
        self.assertEqual(
            migrated["sourceWorkspaceDisposition"], "preserved-archive"
        )
        self.assertEqual(source_manifest_path.read_bytes(), source_manifest_before)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        migrated_source = target_workspace / "source" / "shader.hlsl"
        self.assertEqual(migrated_source.read_bytes(), source_bytes)

        transfer_evidence = (
            self.service().live_edit_authorization_migration_evidence(
                source_workspace,
                target_workspace,
                expected_source_sha256=source_hash,
            )
        )
        self.assertEqual(transfer_evidence["sourceSha256"], source_hash)
        self.assertEqual(
            transfer_evidence["sourceWorkspaceIdentity"]["workspaceId"],
            created["workspaceId"],
        )
        self.assertNotEqual(
            transfer_evidence["workspaceIdentity"]["workspaceId"],
            created["workspaceId"],
        )
        self.assertEqual(
            {
                key: value
                for key, value in transfer_evidence[
                    "sourceWorkspaceIdentity"
                ].items()
                if key != "workspaceId"
            },
            {
                key: value
                for key, value in transfer_evidence["workspaceIdentity"].items()
                if key != "workspaceId"
            },
        )
        self.assertFalse(transfer_evidence["sourceCandidatesImported"])
        self.assertFalse(transfer_evidence["semanticEquivalenceVerified"])

        migrated_source.write_bytes(source_bytes + b"// unbound target edit\n")
        with self.assertRaisesRegex(
            ToolchainRoutingError, "source bytes changed"
        ):
            self.service().live_edit_authorization_migration_evidence(
                source_workspace,
                target_workspace,
                expected_source_sha256=source_hash,
            )
        migrated_source.write_bytes(source_bytes)

        status = self.service().status(target_workspace)
        self.assertEqual(status["revision"], 0)
        self.assertIsNone(status["latestCompiled"])
        self.assertEqual(
            status["source"]["origin"],
            "toolchain-migration-preserved-source",
        )
        self.assertFalse(status["source"]["baselineCompileVerified"])
        self.assertFalse(status["reconstructionFidelity"]["available"])
        target_manifest = json.loads(
            (target_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        migration = target_manifest["source"]["migration"]
        self.assertFalse(migration["sourceCandidatesImported"])
        self.assertFalse(migration["semanticEquivalenceVerified"])
        self.assertEqual(
            migration["invalidatedSourceCandidate"]["declaredRevision"], 1
        )
        self.assertEqual(
            migration["invalidatedSourceCandidate"]["declaredRawSha256"],
            compiled["rawSha256"],
        )
        self.assertFalse(migration["invalidatedSourceCandidate"]["trusted"])
        self.assertFalse(migration["invalidatedSourceCandidate"]["imported"])
        self.assertEqual(status["source"]["toolchainMigration"], migration)
        self.assertEqual(list((target_workspace / "revisions").iterdir()), [])
        self.assertNotEqual(
            target_manifest["routePlan"]["planSha256"],
            created["routePlan"]["planSha256"],
        )
        self.assertEqual(
            target_manifest["routePlan"]["routeCatalogSha256"],
            hashlib.sha256(self.routes_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            target_manifest["routePlan"]["toolRegistrySha256"],
            hashlib.sha256(self.tools_path.read_bytes()).hexdigest(),
        )
        new_plan_sha256 = target_manifest["routePlan"]["planSha256"]
        newly_compiled = self.service().compile(target_workspace)
        self.assertEqual(
            newly_compiled["revision"], 1
        )
        self.assertEqual(
            self.service().status(target_workspace)["latestCompiled"][
                "routePlanSha256"
            ],
            new_plan_sha256,
        )

    def test_toolchain_migration_rejects_route_or_source_identity_change(self) -> None:
        source_workspace = self.root / "workspace-migration-rejected"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest = (source_workspace / "manifest.json").read_bytes()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        upgraded_catalog["routes"] = [
            route
            for route in upgraded_catalog["routes"]
            if route["routeId"] != "test-hlsl-spirv"
        ]
        atomic_write_json(self.routes_path, upgraded_catalog)
        target = self.root / "workspace-migration-route-rejected"
        with self.assertRaisesRegex(
            ToolchainRoutingError, "unknown route ID"
        ):
            self.service().migrate_toolchain(
                source_workspace,
                target,
                expected_route_plan_sha256=created["routePlan"]["planSha256"],
                expected_source_sha256=source_hash,
            )
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            (source_workspace / "manifest.json").read_bytes(), source_manifest
        )

        with self.assertRaisesRegex(
            ToolchainRoutingError, "source bytes changed"
        ):
            self.service().migrate_toolchain(
                source_workspace,
                self.root / "workspace-migration-source-rejected",
                expected_route_plan_sha256=created["routePlan"]["planSha256"],
                expected_source_sha256="0" * 64,
            )

    def test_toolchain_migration_invalidates_incomplete_old_abi_report(self) -> None:
        # Import locally so unittest discovery of this module does not collect
        # the support module's TestCase classes a second time.
        from tests.test_shader_abi import complete_interface, reflection_response

        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        interface = complete_interface()
        interface["encoding"] = "ShaderEncoding.DXIL"
        catalog["shaders"][0].update(interface)
        atomic_write_json(self.catalog_path, catalog)

        service = self.service()
        source_workspace = self.root / "workspace-invalid-old-abi-candidate"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = service.decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// preserve me\n")
        compiled = service.compile(source_workspace)
        response = reflection_response(
            interface,
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(
            source_workspace, response
        )
        self.assertTrue(validated["replayAbiValidated"])

        report_path = Path(validated["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["response"]["compiledInterface"]["interfaceComplete"] = False
        atomic_write_json(report_path, report)
        manifest_path = source_workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"]["abiGate"]["reportSha256"] = hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest()
        atomic_write_json(manifest_path, manifest)
        invalid_manifest_bytes = manifest_path.read_bytes()
        invalid_report_bytes = report_path.read_bytes()
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()

        with self.assertRaisesRegex(
            ManifestValidationError,
            "candidate ABI report evidence is invalid: .*interfaceComplete must be true",
        ):
            service.status(source_workspace)

        upgraded_catalog = json.loads(
            self.routes_path.read_text(encoding="utf-8")
        )
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        target_workspace = self.root / "workspace-after-invalid-candidate"
        # A ShaderWorkspaceService is bound to the exact process-owned tool and
        # route config snapshot that existed when it was constructed.  The
        # production server factory replaces that service after either small
        # config changes, so exercise migration through the same fresh snapshot
        # instead of asking the stale instance to accept changed catalog bytes.
        migration_service = self.service()
        migrated = migration_service.migrate_toolchain(
            source_workspace,
            target_workspace,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=source_hash,
        )

        self.assertEqual(migrated["revision"], 0)
        self.assertFalse(migrated["latestCandidateImported"])
        self.assertEqual(manifest_path.read_bytes(), invalid_manifest_bytes)
        self.assertEqual(report_path.read_bytes(), invalid_report_bytes)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            (target_workspace / "source" / "shader.hlsl").read_bytes(),
            source_bytes,
        )
        status = migration_service.status(target_workspace)
        self.assertEqual(status["revision"], 0)
        self.assertIsNone(status["latestCompiled"])
        self.assertEqual(list((target_workspace / "revisions").iterdir()), [])
        summary = status["source"]["toolchainMigration"][
            "invalidatedSourceCandidate"
        ]
        self.assertEqual(summary["declaredAbiGateStatus"], "compatible")
        self.assertFalse(summary["trusted"])
        self.assertFalse(summary["imported"])
        self.assertFalse(status["semanticEquivalenceVerified"])

    def test_toolchain_migration_respects_shared_source_lease(self) -> None:
        source_workspace = self.root / "workspace-migration-leased"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        target = self.root / "workspace-migration-while-watched"
        lease = WorkspaceSourceTransactionLease(source_workspace)
        lease.acquire()
        try:
            with self.assertRaisesRegex(
                WorkspaceSourceLeaseError, "source transaction lease"
            ):
                self.service().migrate_toolchain(
                    source_workspace,
                    target,
                    expected_route_plan_sha256=created["routePlan"]["planSha256"],
                    expected_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                )
        finally:
            self.assertTrue(lease.release())
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), source_bytes)

    def test_toolchain_migration_host_recovery_rejects_unsafe_leases(self) -> None:
        source_workspace = self.root / "workspace-migration-unsafe-leases"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        current_owner = "2" * 32
        lease_path = source_workspace / WorkspaceSourceTransactionLease.FILE_NAME

        def assert_blocked(label: str, *, host_instance_id: str | None) -> None:
            target = self.root / ("workspace-migration-blocked-" + label)
            lease_before = lease_path.read_bytes()
            with self.assertRaisesRegex(
                WorkspaceSourceLeaseError, "source transaction lease"
            ):
                self.service().migrate_toolchain(
                    source_workspace,
                    target,
                    expected_route_plan_sha256=created["routePlan"]["planSha256"],
                    expected_source_sha256=source_hash,
                    host_instance_id=host_instance_id,
                )
            self.assertEqual(lease_path.read_bytes(), lease_before)
            self.assertFalse(target.exists())

        different_host = WorkspaceSourceTransactionLease(
            source_workspace, owner_instance_id="1" * 32
        )
        different_host.acquire()
        assert_blocked("no-host-instance", host_instance_id=None)
        self.assertTrue(different_host.release())

        same_host = WorkspaceSourceTransactionLease(
            source_workspace, owner_instance_id=current_owner
        )
        same_host.acquire()
        assert_blocked("same-host-instance", host_instance_id=current_owner)
        self.assertTrue(same_host.release())

        legacy = WorkspaceSourceTransactionLease(source_workspace)
        legacy.acquire()
        assert_blocked("legacy-token", host_instance_id=current_owner)
        self.assertTrue(legacy.release())

        lease_path.write_bytes(b"{malformed-json\n")
        assert_blocked("malformed-lock", host_instance_id=current_owner)
        lease_path.unlink()

        for residue_name, label in (
            (SOURCE_TRANSACTION_JOURNAL_FILE, "transaction-journal"),
            (SOURCE_TRANSACTION_BACKUP_FILE, "transaction-backup"),
        ):
            previous = WorkspaceSourceTransactionLease(
                source_workspace, owner_instance_id="1" * 32
            )
            previous.acquire()
            residue = source_workspace / residue_name
            residue.write_bytes(b"transaction residue")
            assert_blocked(label, host_instance_id=current_owner)
            residue.unlink()
            self.assertTrue(previous.release())

        self.assertEqual(source_path.read_bytes(), source_bytes)


DXBC_ASSEMBLY_ROUTE_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
if mode == "disassemble":
    output_path.write_text(
        "ps_5_0\n"
        "dcl_globalFlags refactoringAllowed\n"
        "dcl_output o0.xyzw\n"
        "mov o0.xyzw, l(1.000000, 0.000000, 0.000000, 1.000000)\n"
        "ret\n",
        encoding="utf-8",
    )
elif mode == "assemble":
    shutil.copyfile(Path(sys.argv[4]), output_path)
else:
    raise SystemExit(9)
'''


class DxbcAssemblyWorkspaceTests(unittest.TestCase):
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
        self.script = self.root / "dxbc_assembly_route_tool.py"
        self.script.write_text(DXBC_ASSEMBLY_ROUTE_TOOL, encoding="utf-8")
        executable = Path(sys.executable).resolve()
        identity = {
            "executable": str(executable),
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "fixedFileHashes": {
                str(self.script.resolve()): hashlib.sha256(
                    self.script.read_bytes()
                ).hexdigest()
            },
            "decompile": {
                "args": [
                    str(self.script.resolve()),
                    "disassemble",
                    "{input}",
                    "{output}",
                    "{raw_sha256}",
                ],
                "timeoutSeconds": 10,
            },
            "compile": {
                "args": [
                    str(self.script.resolve()),
                    "assemble",
                    "{input}",
                    "{output}",
                    "{baseline}",
                    "{raw_sha256}",
                ],
                "timeoutSeconds": 10,
            },
        }
        self.tools_path = self.root / "shader-tools.v1.json"
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {"dxbc-assembly": identity},
            },
        )
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="test-dxbc-disassemble",
                tool_id="dxbc-assembly",
                operation="decompile",
                input_format="dxbc",
                output_format="dxbc-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixture instruction disassembler",
                source_url="https://example.invalid/dxbc-disassemble",
            ),
            route_record(
                registry,
                route_id="test-dxbc-assemble",
                tool_id="dxbc-assembly",
                operation="compile",
                input_format="dxbc-asm",
                output_format="dxbc",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture instruction assembler",
                source_url="https://example.invalid/dxbc-assemble",
            ),
        ]
        self.routes_path = self.root / "shader-toolchains.v1.json"
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        program = struct.pack("<2I", 0x00000050, 3) + b"\x00\x00\x00\x00"
        self.blob = make_container([(b"SHDR", program)])
        self.key = ShaderKey(
            207,
            "fragment",
            "main",
            hashlib.sha256(self.blob).hexdigest(),
        )
        catalog_dir = self.root / "capture-catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (self.key.raw_sha256 + ".bin")).write_bytes(
            self.blob
        )
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/dxbc-frame.rdc",
            "api": "D3D11",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 81,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> ShaderWorkspaceService:
        return ShaderWorkspaceService(self.tools_path, self.routes_path)

    def test_explicit_assembly_round_trip_uses_distinct_source_and_revision_names(self) -> None:
        workspace = self.root / "workspace-dxbc-assembly"
        automatic = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            self.root / "workspace-default-hlsl",
        )
        self.assertEqual("round-trip", automatic["recoveryPlan"]["mode"])
        self.assertEqual("dxbc-asm", automatic["routePlan"]["editableFormat"])
        evidence = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            self.root / "workspace-explicit-hlsl",
            editable_format="hlsl",
        )
        self.assertEqual("evidence-only", evidence["recoveryPlan"]["mode"])
        self.assertEqual([], evidence["routePlan"]["decompile"])
        self.assertEqual([], evidence["routePlan"]["compile"])
        created = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        self.assertEqual("dxbc-asm", created["routePlan"]["editableFormat"])
        decompiled = self.service().decompile(workspace)
        source = Path(decompiled["sourcePath"])
        self.assertEqual("shader.dxbc.asm", source.name)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        source.write_text(
            source.read_text(encoding="utf-8").replace("1.000000", "0.500000"),
            encoding="utf-8",
        )
        compiled = self.service().compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())
        revision_source = workspace / "revisions" / "0001" / "shader.dxbc.asm"
        self.assertTrue(revision_source.is_file())
        status = self.service().status(workspace)
        self.assertEqual("dxbc-asm", status["source"]["format"])
        self.assertEqual("editable-dxbc-assembly", status["representation"]["kind"])
        self.assertFalse(status["representation"]["semanticEquivalenceVerified"])

    def test_save_watcher_tracks_assembly_source_and_compiles_candidate(self) -> None:
        workspace = self.root / "workspace-dxbc-assembly-watch"
        service = self.service()
        service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        source = Path(service.decompile(workspace)["sourcePath"])
        manager = ShaderWorkspaceWatchManager(self.service)
        try:
            started = manager.start(workspace, debounce_ms=100)
            self.assertEqual(source, Path(started["sourcePath"]))
            source.write_text(
                source.read_text(encoding="utf-8") + "// assembly edit\n",
                encoding="utf-8",
            )
            deadline = time.monotonic() + 5.0
            snapshot = started
            while time.monotonic() < deadline:
                snapshot = manager.poll(started["watchId"])
                if snapshot["compileSuccesses"] >= 1:
                    break
                time.sleep(0.05)
            self.assertEqual(1, snapshot["compileSuccesses"])
            self.assertEqual(0, snapshot["compileFailures"])
            self.assertEqual(1, snapshot["latestCandidateRevision"])
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
