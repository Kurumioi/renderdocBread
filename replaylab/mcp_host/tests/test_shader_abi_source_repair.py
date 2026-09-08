from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)
from tests.test_shader_organization import _FakeWorkspaceService


class _CheckingService(_FakeWorkspaceService):
    """Fake service with the Host's compile-only check hook."""

    def __init__(self, root: Path, source: bytes) -> None:
        super().__init__(root, source)
        self.check_calls: list[str] = []

    def compile_check(self, workspace_path: str, text: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        self.check_calls.append(text)
        if "syntax-error" in text:
            return {"ok": False, "diagnostics": [
                {"line": 1, "column": 3, "severity": "error", "message": "expected ';'"}]}
        return {"ok": True, "diagnostics": []}


class AbiSourceRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        patch = mock.patch.dict(os.environ, {
            "REPLAYLAB_CONFIG_DIR": str(Path(self.temporary.name) / "host-config")})
        patch.start()
        self.addCleanup(patch.stop)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.original = b"struct SPIRV_Cross_Input { float4 c : TEXCOORD0; };\nfloat4 MainPS() : SV_Target { return 1.0; }\n"
        self.service = _CheckingService(self.root, self.original)
        self.compatible_when = b"abi-fixed"

        def validate(workspace_path: str, revision: int, raw_sha256: str) -> dict:
            source = self.service.source_path.read_bytes()
            compatible = self.compatible_when in source
            request_id = hashlib.sha256(("r:%d%s" % (revision, raw_sha256)).encode()).hexdigest()
            report = hashlib.sha256(("p:%d%s" % (revision, raw_sha256)).encode()).hexdigest()
            latest = self.service.latest
            latest["abiCompatible"] = compatible
            latest["requiresReplayAbiValidation"] = False
            latest["replayAbiValidated"] = True
            latest["validationScope"] = "replay-reflection-abi-compatible"
            latest["semanticEquivalenceVerified"] = False
            latest["abiGate"] = {
                "status": "compatible" if compatible else "incompatible",
                "requestId": request_id, "reportSha256": report,
                "baselineAbiSha256": "a" * 64, "candidateAbiSha256": "b" * 64}
            return {"workspacePath": str(self.root.resolve()), "revision": revision,
                    "requestId": request_id, "compatible": compatible,
                    "requiresReplayAbiValidation": False, "replayAbiValidated": True,
                    "abiReportSha256": report}

        self.manager = ShaderOrganizationManager(lambda: self.service, validate)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sha(self, value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def test_rejected_repair_restores_previous_bytes_and_reports_evidence(self) -> None:
        attempt = self.original.replace(b"float4 c", b"float4 c2")
        result = self.manager.repair_baseline_source(
            str(self.root), self._sha(self.original), attempt.decode(),
            acknowledge_semantic_equivalence_unverified=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["phase"], "compile-or-replay-abi")
        self.assertTrue(result["sourceRestored"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertEqual(result["currentSourceSha256"], self._sha(self.original))
        self.assertEqual(result["nextTool"], "repair_shader_workspace_source_abi")
        self.assertIn("failure", result)
        receipt = json.loads(Path(result["receiptPath"]).read_text("utf-8"))
        self.assertFalse(receipt["accepted"])
        # The untouched decompiler output was preserved before the first attempt.
        self.assertEqual((self.root / "ai" / "decompiler-original.hlsl").read_bytes(), self.original)

    def test_compile_check_failure_never_touches_the_source(self) -> None:
        attempt = self.original + b"syntax-error\n"
        result = self.manager.repair_baseline_source(
            str(self.root), self._sha(self.original), attempt.decode(),
            acknowledge_semantic_equivalence_unverified=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["phase"], "compile-check")
        self.assertEqual(result["diagnostics"][0]["message"], "expected ';'")
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertEqual(self.service.revision, 0)

    def test_accepted_repair_becomes_the_new_baseline_and_begin_succeeds(self) -> None:
        attempt = self.original.replace(b"float4 c", b"float4 c /*abi-fixed*/")
        result = self.manager.repair_baseline_source(
            str(self.root), self._sha(self.original), attempt.decode(),
            acknowledge_semantic_equivalence_unverified=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["nextTool"], "begin_shader_workspace_organization")
        self.assertEqual(self.service.source_path.read_bytes(), attempt)
        self.assertEqual(result["currentSourceSha256"], self._sha(attempt))
        self.assertEqual(result["abiGate"]["status"], "compatible")
        # Stale hash / identical source are refused before anything happens.
        with self.assertRaisesRegex(ShaderOrganizationError, "changed before the ABI repair"):
            self.manager.repair_baseline_source(
                str(self.root), self._sha(self.original), attempt.decode() + "\n",
                acknowledge_semantic_equivalence_unverified=True)
        with self.assertRaisesRegex(ShaderOrganizationError, "identical"):
            self.manager.repair_baseline_source(
                str(self.root), self._sha(attempt), attempt.decode(),
                acknowledge_semantic_equivalence_unverified=True)
        with self.assertRaisesRegex(ShaderOrganizationError, "acknowledging"):
            self.manager.repair_baseline_source(
                str(self.root), self._sha(attempt), attempt.decode() + "\n",
                acknowledge_semantic_equivalence_unverified=False)
        opened = self.manager.begin(str(self.root), {"schema": "test-semantic-context/v1"})
        self.assertEqual(len(str(opened.get("organizationId", ""))), 32, opened)

    def test_failed_begin_session_gets_an_actionable_message(self) -> None:
        session_dir = self.root / "ai" / "organization-sessions" / ("c" * 32)
        session_dir.mkdir(parents=True)
        (session_dir / "failure.json").write_text(json.dumps({
            "schema": "renderdoc-ai-shader-organization-failure/v1",
            "phase": "baseline-compile-abi"}), "utf-8")
        with self.assertRaisesRegex(ShaderOrganizationError, "never opened.*baseline-compile-abi"):
            self.manager.rename(str(self.root), "c" * 32, {"a": "b"})


if __name__ == "__main__":
    unittest.main()
