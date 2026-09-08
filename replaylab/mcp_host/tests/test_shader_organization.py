from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from src.shader_live_edit import (
    SHARED_APPLY_SCHEMA,
    SHARED_ROLLBACK_TOOL,
    ShaderLiveEditManager,
)
from src.shader_workspace import ShaderWorkspaceWatchManager
from src.shader_workspace import organization as organization_module
from src.shader_workspace.leases import WorkspaceSourceTransactionLease
from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)
from src.shader_workspace.templates import analysis_template, render_analysis_fields


def _valid_analysis() -> str:
    value = analysis_template()
    value.update(overview="Returns one render target.", computation=["The entry point returns its computed color."],
                 outputs=["Declared color output."], observedFacts=[{"claim": "The source has a pixel entry point.", "evidence": "source:MainPS"}], inferences=[])
    text, issues = render_analysis_fields(value)
    assert not issues
    return text


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def _compatible_result(root: Path, revision: int, raw_sha256: str) -> dict:
    replacement_id = "%032x" % (0x2000 + revision)
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": True},
        "scope": "captured-resource",
        "applied": True,
        "replacement": {
            "replacementId": replacement_id,
            "revision": revision,
            "candidateRawSha256": raw_sha256,
            "originalResourceId": 17,
            "resourceScoped": True,
            "eventScoped": False,
            "allOriginalResourceUsesAffected": True,
            "originalShaderPreserved": True,
        },
        "rollback": {
            "tool": SHARED_ROLLBACK_TOOL,
            "workspacePath": str(root),
            "replacementId": replacement_id,
        },
    }


def _rejected_result(revision: int, raw_sha256: str) -> dict:
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": False},
        "applied": False,
        "reason": "candidate ABI is not exactly compatible",
    }


class _FakeWorkspaceService:
    def __init__(self, root: Path, source: bytes) -> None:
        self.root = root.resolve()
        self.source_path = self.root / "source" / "shader.hlsl"
        self.source_path.parent.mkdir(parents=True)
        self.source_path.write_bytes(source)
        self.decompiled_sha = hashlib.sha256(source).hexdigest()
        self.baseline_code_sha = self._code_sha(source)
        self.revision = 0
        self.latest = None

    def _code_sha(self, source: bytes) -> str:
        payload = b"changed-program" if b"semantic-change" in source else b"same-program"
        return hashlib.sha256(payload).hexdigest()

    def compile(self, workspace_path: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        source = self.source_path.read_bytes()
        source_sha = hashlib.sha256(source).hexdigest()
        self.revision += 1
        raw_sha = hashlib.sha256(b"container:" + source).hexdigest()
        code_sha = self._code_sha(source)
        self.latest = {
            "revision": self.revision,
            "sourceSha256": source_sha,
            "rawSha256": raw_sha,
            "codeSha256": code_sha,
            "abiCompatible": False,
            "requiresReplayAbiValidation": True,
            "replayAbiValidated": False,
        }
        return {
            "workspacePath": str(self.root),
            "revision": self.revision,
            "sourceSha256": source_sha,
            "rawSha256": raw_sha,
            "codeSha256": code_sha,
            "kind": "DXIL",
        }

    compile_check_calls: list[str] = []

    def compile_check(self, workspace_path: str, text: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        self.compile_check_calls.append(text)
        if "compile-error" in text:
            return {"ok": False, "diagnostics": [{"message": "synthetic compile error"}]}
        return {"ok": True, "diagnostics": []}

    def status(self, workspace_path: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        source_sha = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        current_source_matches = source_sha == self.decompiled_sha
        candidate_matches = bool(
            current_source_matches
            and self.latest
            and self.latest.get("sourceSha256") == source_sha
            and self.latest.get("codeSha256") == self.baseline_code_sha
        )
        candidate_abi = bool(
            candidate_matches
            and self.latest.get("replayAbiValidated") is True
            and self.latest.get("abiCompatible") is True
        )
        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(self.root),
            "workspaceId": "1" * 32,
            "capture": {"captureId": "2" * 64},
            "shader": {
                "resourceId": 17,
                "rawSha256": "3" * 64,
                "stage": "fragment",
                "entryPoint": "MainPS",
            },
            "stage": "fragment",
            "entryPoint": "MainPS",
            "targetProfile": {"profile": "ps_6_6"},
            "source": {
                "present": True,
                "path": str(self.source_path),
                "format": "hlsl",
                "currentSha256": source_sha,
                "decompiledSha256": self.decompiled_sha,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "representation": {
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "baseline": {
                "rawSha256": "3" * 64,
                "compiledInterface": {},
            },
            "revision": self.revision,
            "latestCompiled": dict(self.latest) if self.latest else None,
            "replayAbiValidated": bool(
                self.latest
                and self.latest.get("replayAbiValidated") is True
            ),
            "abiCompatible": bool(
                self.latest and self.latest.get("abiCompatible") is True
            ),
            "reconstructionFidelity": {
                "schema": "renderdoc-ultra-shader-reconstruction-fidelity/v1",
                "scope": "captured-baseline-vs-untouched-route-round-trip",
                "available": True,
                "baselineCompileVerified": True,
                "capturedCodeSha256": self.baseline_code_sha,
                "reconstructedCodeSha256": self.baseline_code_sha,
                "codeSha256Equal": True,
                "currentSourceMatchesUntouchedReconstruction": (
                    current_source_matches
                ),
                "currentCandidateMatchesUntouchedReconstruction": candidate_matches,
                "currentCandidateReplayAbiCompatible": candidate_abi,
                "classification": "exact-executable-code-match",
                "semanticEquivalenceVerified": False,
                "note": "fixed fake fidelity evidence",
            },
        }


class ShaderOrganizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        config_patch = mock.patch.dict(os.environ, {
            "REPLAYLAB_CONFIG_DIR": str(Path(self.temporary.name) / "host-config")
        })
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.original = b"float4 MainPS() : SV_Target { return 1.0; }\n"
        self.service = _FakeWorkspaceService(self.root, self.original)
        self.live_manager: ShaderLiveEditManager | None = None

        def validate(workspace_path: str, revision: int, raw_sha256: str) -> dict:
            self.assertEqual(Path(workspace_path).resolve(), self.root.resolve())
            self.assertEqual(revision, self.service.revision)
            self.assertEqual(raw_sha256, self.service.latest["rawSha256"])
            request_id = hashlib.sha256(
                ("request:" + str(revision) + raw_sha256).encode("ascii")
            ).hexdigest()
            report_sha256 = hashlib.sha256(
                (str(revision) + raw_sha256).encode("ascii")
            ).hexdigest()
            self.service.latest["abiCompatible"] = True
            self.service.latest["requiresReplayAbiValidation"] = False
            self.service.latest["replayAbiValidated"] = True
            self.service.latest["validationScope"] = (
                "replay-reflection-abi-compatible"
            )
            self.service.latest["semanticEquivalenceVerified"] = False
            self.service.latest["abiGate"] = {
                "status": "compatible",
                "requestId": request_id,
                "reportSha256": report_sha256,
                "baselineAbiSha256": hashlib.sha256(
                    b"organization-test-baseline-abi"
                ).hexdigest(),
                "candidateAbiSha256": hashlib.sha256(
                    ("candidate-abi:" + raw_sha256).encode("ascii")
                ).hexdigest(),
            }
            return {
                "workspacePath": str(self.root.resolve()),
                "revision": revision,
                "requestId": request_id,
                "compatible": True,
                "requiresReplayAbiValidation": False,
                "replayAbiValidated": True,
                "abiReportSha256": report_sha256,
            }

        self.manager = ShaderOrganizationManager(lambda: self.service, validate)

    def tearDown(self) -> None:
        if self.live_manager is not None:
            self.live_manager.shutdown()
        self.temporary.cleanup()

    def _begin(self) -> dict:
        opened = self.manager.begin(
            str(self.root),
            {
                "schema": "test-semantic-context/v1",
                "textures": [{"slot": 0, "role": "base-color"}],
            },
        )
        Path(opened["analysisPath"]).write_text(_valid_analysis(), encoding="utf-8")
        return opened

    def _accepted_review(self) -> tuple[dict, dict]:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// Readability-only rewrite\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        Path(opened["analysisPath"]).write_text(
            _valid_analysis(), encoding="utf-8"
        )
        Path(opened["flowchartPath"]).write_text(
            "flowchart TD\n  A[Input] --> B[Output]\n", encoding="utf-8"
        )
        reviewed = self.manager.review(
            str(self.root), opened["organizationId"]
        )
        self.assertEqual(
            reviewed["gate"]["decision"], "accepted-executable-editable"
        )
        return opened, reviewed

    def test_exact_code_identity_promotes_readable_proposal(self) -> None:
        opened = self._begin()
        description_template = json.loads(
            Path(opened["moduleDescriptionsPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            description_template["schema"],
            "replaylab-shader-module-descriptions/v1",
        )
        self.assertEqual(
            [item["name"] for item in description_template["modules"]],
            ["MainPS"],
        )
        self.assertTrue(
            opened["semanticContext"]["reconstructionFidelity"][
                "currentCandidateReplayAbiCompatible"
            ]
        )
        persisted_context = json.loads(
            Path(opened["semanticContextPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            persisted_context["reconstructionFidelity"],
            opened["semanticContext"]["reconstructionFidelity"],
        )
        proposal = Path(opened["proposalSourcePath"])
        proposal.write_text(
            "// Readable name only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        Path(opened["analysisPath"]).write_text(
            _valid_analysis(), encoding="utf-8"
        )
        Path(opened["flowchartPath"]).write_text(
            "flowchart TD\n  A[Input] --> B[Output]\n", encoding="utf-8"
        )

        reviewed = self.manager.review(
            str(self.root), opened["organizationId"]
        )

        self.assertTrue(reviewed["gate"]["codeSha256Equal"])
        self.assertTrue(reviewed["gate"]["organizedSourcePromoted"])
        self.assertTrue(reviewed["gate"]["safeToStartLiveEdit"])
        self.assertIn(
            "Readable name only",
            self.service.source_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (self.root / "ai" / "decompiler-original.hlsl").read_bytes(),
            self.original,
        )
        self.assertFalse(reviewed["gate"]["semanticEquivalenceVerified"])
        self.assertFalse(reviewed["applied"])
        gate = reviewed["gate"]
        self.assertEqual(gate["workspaceId"], "1" * 32)
        self.assertEqual(gate["captureId"], "2" * 64)
        self.assertEqual(gate["stage"], "fragment")
        self.assertEqual(gate["entryPoint"], "MainPS")
        self.assertTrue(gate["finalBinding"]["complete"])
        self.assertEqual(gate["finalRevision"], reviewed["organizedCompile"]["revision"])
        self.assertEqual(
            gate["finalCandidateRawSha256"],
            reviewed["organizedCompile"]["rawSha256"],
        )
        self.assertEqual(gate["finalCodeSha256"], reviewed["organizedCompile"]["codeSha256"])
        self.assertEqual(gate["finalAbiRequestId"], reviewed["organizedAbi"]["requestId"])
        self.assertEqual(
            gate["finalAbiReportSha256"],
            reviewed["organizedAbi"]["abiReportSha256"],
        )
        self.assertEqual(
            json.loads(Path(reviewed["gatePath"]).read_text(encoding="utf-8")),
            gate,
        )
        flow_graph = reviewed["flowGraph"]
        self.assertEqual(flow_graph["schema"], "replaylab-shader-flow-graph/v1")
        self.assertEqual(flow_graph["workspaceId"], "1" * 32)
        self.assertEqual(flow_graph["sourceSha256"], gate["finalSourceSha256"])
        self.assertEqual(flow_graph["sourceRevision"], gate["finalRevision"])
        self.assertEqual(flow_graph["nodes"][0]["name"], "MainPS")
        self.assertEqual(
            flow_graph["nodes"][0]["source"],
            {
                "workspaceId": "1" * 32,
                "documentId": "1" * 32,
                "path": "source/shader.hlsl",
                "line": 2,
                "column": 31,
            },
        )
        self.assertEqual(flow_graph["graphId"], gate["flowGraphId"])
        self.assertEqual(
            hashlib.sha256(Path(reviewed["flowGraphPath"]).read_bytes()).hexdigest(),
            gate["flowGraphSha256"],
        )

    def test_publication_validator_accepts_exact_completed_session(self) -> None:
        unused_opened, reviewed = self._accepted_review()

        validated = self.manager.validate_publication_gate(
            str(self.root), self.service.status(str(self.root))
        )

        self.assertEqual(validated, reviewed["gate"])

    def _observation(self, unused_workspace: str, baseline: dict, candidate: dict) -> dict:
        return {
            "schema": "renderdoc-ai-shader-organization-observation/v1",
            "scope": "captured-event-output",
            "accepted": True,
            "baselineCandidateRawSha256": baseline["rawSha256"],
            "candidateRawSha256": candidate["rawSha256"],
            "baselineSourceSha256": baseline["sourceSha256"],
            "candidateSourceSha256": candidate["sourceSha256"],
            "metrics": {"maxAbsoluteError": 0.000001},
            "tolerances": {"absolute": 0.00001, "relative": 0.001},
            "sampleCount": 256,
            "semanticEquivalenceVerified": False,
            "replayRestored": True,
        }

    def _changed_proposal(self, opened: dict) -> None:
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\nfloat4 MainPS() : SV_Target { return 1.000001; }\n",
            encoding="utf-8",
        )

    def test_host_measurements_allow_changed_code_without_claiming_semantic_proof(self) -> None:
        self.manager._compare_candidates = mock.Mock(side_effect=self._observation)
        opened = self._begin()
        self._changed_proposal(opened)
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertEqual(reviewed["equivalenceLevel"], "observational-pass")
        self.assertTrue(reviewed["verdict"]["accepted"])
        self.assertFalse(reviewed["gate"]["codeSha256Equal"])
        self.assertFalse(reviewed["gate"]["semanticEquivalenceVerified"])
        self.assertEqual(self.service.revision, 2)
        self.assertEqual(
            self.manager.validate_publication_gate(str(self.root)), reviewed["gate"]
        )
        self.manager._compare_candidates.assert_called_once()

    def _dense_proposal(self, opened: dict, *, anonymous: bool = False) -> None:
        name = "_2323" if anonymous else "accumulatedValue"
        expression = " + ".join(["0.01"] * 100)
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\nfloat4 MainPS() : SV_Target\n{\n"
            f"    float {name} = {expression};\n    return {name};\n}}\n",
            encoding="utf-8",
        )

    def test_readability_warnings_continue_to_measurements_and_survive_publication(self) -> None:
        self.manager._compare_candidates = mock.Mock(side_effect=self._observation)
        opened = self._begin()
        self._dense_proposal(opened)
        reviewed = self.manager.review(str(self.root), opened["organizationId"])

        gate = reviewed["gate"]
        verdict = reviewed["verdict"]
        self.assertTrue(verdict["accepted"])
        self.assertEqual(verdict["level"], "observational-pass")
        self.assertEqual(
            [(item["code"], item["severity"]) for item in verdict["reasons"]],
            [("algorithm-dense-expressions", "warning")],
        )
        self.assertEqual(gate["decision"], "accepted-executable-editable")
        self.assertTrue(gate["baselineCompiledAndAbiValidated"])
        self.assertTrue(gate["organizedCompiledAndAbiValidated"])
        self.assertFalse(gate["codeSha256Equal"])
        self.assertEqual(self.service.revision, 2)
        self.manager._compare_candidates.assert_called_once()
        self.assertEqual(
            self.service.source_path.read_bytes(),
            Path(opened["proposalSourcePath"]).read_bytes(),
        )
        session_dir = Path(opened["sessionPath"]).parent
        for path in (Path(reviewed["gatePath"]), session_dir / "gate.json", Path(opened["sessionPath"])):
            with self.subTest(path=path.name):
                persisted = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["verdict"], verdict)
        rounds = json.loads((self.root / "ai" / "organization-rounds.json").read_text(encoding="utf-8"))
        self.assertTrue(rounds["shader-organization"]["lastAccepted"])
        self.assertEqual(rounds["shader-organization"]["lastReasons"], verdict["reasons"])
        self.assertEqual(self.manager.validate_publication_gate(str(self.root)), gate)

    def test_mixed_readability_blocker_and_warning_reject_before_compile_or_measurements(self) -> None:
        self.manager._compare_candidates = mock.Mock(side_effect=self._observation)
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "3"}):
            opened = self._begin()
            self._dense_proposal(opened, anonymous=True)
            reviewed = self.manager.review(str(self.root), opened["organizationId"])

            self.assertFalse(reviewed["verdict"]["accepted"])
            self.assertEqual(reviewed["gate"]["decision"], "organization-content-rejected")
            self.assertEqual(
                {(item["code"], item["severity"]) for item in reviewed["verdict"]["reasons"]},
                {("algorithm-anonymous-values", "blocker"), ("algorithm-dense-expressions", "warning")},
            )
            self.assertEqual(self.service.revision, 1)
            self.assertEqual(self.service.source_path.read_bytes(), self.original)
            self.manager._compare_candidates.assert_not_called()

            # Correct the actual blocker without removing its warning or
            # opening a replacement session to reset the review budget.
            self._dense_proposal(opened)
            accepted = self.manager.review(str(self.root), opened["organizationId"])
        self.assertTrue(accepted["verdict"]["accepted"])
        self.assertEqual(accepted["verdict"]["round"], 2)
        self.assertEqual(accepted["verdict"]["reasons"][0]["severity"], "warning")
        self.manager._compare_candidates.assert_called_once()

    def _assert_warning_does_not_accept_failed_measurement(self, *, max_rounds: int) -> None:
        def compare(workspace: str, baseline: dict, candidate: dict) -> dict:
            observation = self._observation(workspace, baseline, candidate)
            observation.update(accepted=False, reason="Measured output exceeds the fixed tolerance.")
            observation["metrics"]["maxAbsoluteError"] = 0.5
            return observation

        self.manager._compare_candidates = mock.Mock(side_effect=compare)
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": str(max_rounds)}):
            opened = self._begin()
            self._dense_proposal(opened)
            reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.manager._compare_candidates.assert_called_once()
        self.assertFalse(reviewed["verdict"]["accepted"])
        self.assertFalse(reviewed["gate"]["organizedSourcePromoted"])
        self.assertFalse(reviewed["gate"]["observation"]["accepted"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertEqual(
            {item["code"] for item in reviewed["verdict"]["reasons"]},
            {"algorithm-dense-expressions", "output-comparison"},
        )
        if max_rounds == 1:
            self.assertTrue(reviewed["verdict"]["softAccepted"])
            self.assertEqual(reviewed["gate"]["decision"], "soft-accepted-baseline")
            self.assertEqual(reviewed["gate"]["finalSourceSha256"], self.service.decompiled_sha)
            self.assertEqual(self.manager.validate_publication_gate(str(self.root)), reviewed["gate"])
        else:
            self.assertFalse(reviewed["verdict"]["softAccepted"])
            self.assertEqual(reviewed["gate"]["decision"], "retained-provisional-code-change")
            self.assertFalse(reviewed["gate"]["safeToStartLiveEdit"])

    def test_readability_warning_does_not_override_failed_measurement(self) -> None:
        self._assert_warning_does_not_accept_failed_measurement(max_rounds=3)

    def test_exhausted_warning_review_keeps_baseline_when_measurement_fails(self) -> None:
        self._assert_warning_does_not_accept_failed_measurement(max_rounds=1)

    def test_exact_and_rename_fast_paths_do_not_replay_compare(self) -> None:
        self.manager._compare_candidates = mock.Mock(side_effect=AssertionError("unexpected replay"))
        unused_opened, reviewed = self._accepted_rename_review()
        self.assertEqual(reviewed["equivalenceLevel"], "rename-only")
        self.manager._compare_candidates.assert_not_called()

    def test_observation_cannot_be_forged_by_editing_both_gate_files(self) -> None:
        self.manager._compare_candidates = self._observation
        opened = self._begin()
        self._changed_proposal(opened)
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        gate = copy.deepcopy(reviewed["gate"])
        gate["observation"]["tolerances"]["absolute"] = 1000.0
        Path(reviewed["gatePath"]).write_text(json.dumps(gate), encoding="utf-8")
        (Path(opened["sessionPath"]).parent / "gate.json").write_text(
            json.dumps(gate), encoding="utf-8"
        )
        with self.assertRaisesRegex(ShaderOrganizationError, "sealed session"):
            self.manager.validate_publication_gate(str(self.root))

    def test_observation_rejects_stale_candidate_and_missing_restoration(self) -> None:
        for field, invalid in (("candidateRawSha256", "f" * 64), ("replayRestored", False), ("sampleCount", 0)):
            with self.subTest(field=field):
                def compare(workspace, baseline, candidate):
                    observation = self._observation(workspace, baseline, candidate)
                    observation[field] = invalid
                    return observation

                self.manager._compare_candidates = compare
                opened = self._begin()
                self._changed_proposal(opened)
                with self.assertRaisesRegex(ShaderOrganizationError, "Host output comparison"):
                    self.manager.review(str(self.root), opened["organizationId"])
                self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_rejected_proposal_can_be_fixed_and_reviewed_in_same_session(self) -> None:
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "3"}):
            opened = self._begin()
            self._changed_proposal(opened)
            failed = self.manager.review(str(self.root), opened["organizationId"])
            self.assertFalse(failed["verdict"]["accepted"])
            session = json.loads(Path(opened["sessionPath"]).read_text(encoding="utf-8"))
            self.assertEqual(session["state"], "awaiting-proposal-review")
            self.manager.rename(str(self.root), opened["organizationId"], {}, {"MainPS": "Returns constant color"})
            reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertTrue(reviewed["verdict"]["accepted"])
        self.assertEqual(self.manager.validate_publication_gate(str(self.root)), reviewed["gate"])

    def test_soft_acceptance_publishes_sealed_validated_baseline_only(self) -> None:
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "1"}):
            opened = self._begin()
            self._changed_proposal(opened)
            reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertTrue(reviewed["verdict"]["softAccepted"])
        self.assertFalse(reviewed["verdict"]["accepted"])
        self.assertEqual(reviewed["gate"]["decision"], "soft-accepted-baseline")
        self.assertTrue(reviewed["gate"]["safeToStartLiveEdit"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertEqual(reviewed["gate"]["finalSourceSha256"], self.service.decompiled_sha)
        self.assertEqual(self.manager.validate_publication_gate(str(self.root)), reviewed["gate"])
        with self.assertRaisesRegex(ShaderOrganizationError, "not awaiting review"):
            self.manager.review(str(self.root), opened["organizationId"])
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        self.assertEqual(restarted.validate_publication_gate(str(self.root)), reviewed["gate"])

    def test_current_baseline_and_unchanged_proposal_reuse_compilation(self) -> None:
        self.manager._compile_and_validate(self.service, self.root, self.service.decompiled_sha, "existing baseline")
        self.assertEqual(self.service.revision, 1)
        opened = self._begin()
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertEqual(self.service.revision, 1)
        self.assertTrue(reviewed["organizedCompile"]["reusedCurrentCandidate"])
        self.assertEqual(self.manager.validate_publication_gate(str(self.root)), reviewed["gate"])

    def test_rename_uses_sealed_original_cbuffer_identity_for_external_aliases(self) -> None:
        self.original = b"cbuffer generatedCB : register(b0) { float generated; };\nfloat4 MainPS() : SV_Target { return generated; }\n"
        self.service.source_path.write_bytes(self.original)
        self.service.decompiled_sha = hashlib.sha256(self.original).hexdigest()
        identity = {
            "schema": "replaylab-cbuffer-alias-identity/v1", "captureId": "2" * 64,
            "captureSessionId": "a" * 32, "eventId": 12, "stage": "fragment",
            "shaderResourceId": 17, "rawSha256": "3" * 64,
            "blocks": [{"block": "OriginalCB", "bind": 0, "space": 0,
                        "members": [{"name": "original", "offset": 0}],
                        "leaves": [{"path": "original", "offset": 0, "byteSize": 4}],
                        "memberMappingAvailable": True}],
        }
        self.manager._cbuffer_alias_identity = lambda unused_path: copy.deepcopy(identity)
        with mock.patch.dict(os.environ, {"REPLAYLAB_ALIAS_ROOT": str(self.root / "test-aliases")}):
            opened = self._begin()
            # Editable semantic context is not the authority after begin.
            Path(opened["semanticContextPath"]).write_text("{}", encoding="utf-8")
            renamed = self.manager.rename(str(self.root), opened["organizationId"],
                                          {"generatedCB": "Material", "generated": "albedo"})
        block = renamed["cbufferAliases"]["blocks"][0]
        self.assertEqual(block["block"], "OriginalCB")
        self.assertTrue(block["capturedIdentityMatched"])
        self.assertEqual(block["written"], ["OriginalCB.original"])
        self.assertEqual(block["blockAlias"]["alias"], "Material")

    def test_alias_identity_from_another_captured_shader_is_rejected(self) -> None:
        self.manager._cbuffer_alias_identity = lambda unused: {
            "schema": "replaylab-cbuffer-alias-identity/v1", "captureId": "2" * 64,
            "stage": "fragment", "shaderResourceId": 999, "rawSha256": "3" * 64,
            "blocks": [],
        }
        with self.assertRaisesRegex(ShaderOrganizationError, "CBuffer alias identity is invalid"):
            self._begin()

    def _accepted_rename_review(self) -> tuple[dict, dict]:
        self.original = (
            b"float4 MainPS() : SV_Target { float4 v0 = 1.0; return v0; }\n"
        )
        self.service.source_path.write_bytes(self.original)
        self.service.decompiled_sha = hashlib.sha256(self.original).hexdigest()
        self.service.baseline_code_sha = self.service._code_sha(self.original)
        original_code_sha = self.service._code_sha

        def metadata_sensitive_code_sha(source: bytes) -> str:
            # DXIL can carry resource/local names in executable metadata. Model
            # that change independently of the unchanged mathematical program.
            if b"tintAndAlpha" in source:
                return hashlib.sha256(b"renamed-metadata:" + source).hexdigest()
            return original_code_sha(source)

        with mock.patch.object(
            self.service, "_code_sha", side_effect=metadata_sensitive_code_sha
        ):
            opened = self._begin()
            renamed = self.manager.rename(
                str(self.root), opened["organizationId"], {"v0": "tintAndAlpha"}
            )
            self.assertTrue(renamed["renameOnlyEquivalent"])
            reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertEqual(reviewed["equivalenceLevel"], "rename-only")
        self.assertTrue(reviewed["verdict"]["accepted"])
        self.assertFalse(reviewed["gate"]["codeSha256Equal"])
        return opened, reviewed

    def test_publication_validator_accepts_declared_rename_with_changed_code_hash(self) -> None:
        opened, reviewed = self._accepted_rename_review()
        self.assertIsNone(reviewed["recoveryCompile"])
        self.assertFalse(reviewed["gate"]["rawRecoveryCompiledAndAbiValidated"])
        self.assertFalse(opened["policy"]["exactCodeSha256RequiredForPromotion"])
        with mock.patch.object(
            organization_module,
            "rename_only_equivalent",
            wraps=organization_module.rename_only_equivalent,
        ) as prove:
            validated = self.manager.validate_publication_gate(str(self.root))
        self.assertEqual(validated, reviewed["gate"])
        prove.assert_called_once_with(
            self.original.decode("utf-8"),
            self.service.source_path.read_text(encoding="utf-8"),
            {"v0": "tintAndAlpha"},
        )

    def test_publication_validator_rechecks_rename_source_proof(self) -> None:
        self._accepted_rename_review()
        with mock.patch.object(
            organization_module, "rename_only_equivalent", return_value={"equal": False}
        ), self.assertRaisesRegex(ShaderOrganizationError, "rename-only source proof failed"):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_forged_equivalence_or_rename_map(self) -> None:
        opened, reviewed = self._accepted_rename_review()
        for field, value in (
            ("equivalenceLevel", "exact-code"),
            ("codeSha256Equal", True),
            ("renameMap", {"v0": "differentName"}),
        ):
            with self.subTest(field=field):
                gate = dict(reviewed["gate"])
                gate[field] = value
                Path(reviewed["gatePath"]).write_text(json.dumps(gate), encoding="utf-8")
                (Path(opened["sessionPath"]).parent / "gate.json").write_text(
                    json.dumps(gate), encoding="utf-8"
                )
                with self.assertRaisesRegex(ShaderOrganizationError, "sealed session"):
                    self.manager.validate_publication_gate(str(self.root))

    def test_declared_rename_does_not_accept_changed_arithmetic(self) -> None:
        self.original = (
            b"float4 MainPS() : SV_Target { float4 v0 = 1.0; return v0; }\n"
        )
        self.service.source_path.write_bytes(self.original)
        self.service.decompiled_sha = hashlib.sha256(self.original).hexdigest()
        self.service.baseline_code_sha = self.service._code_sha(self.original)
        opened = self._begin()
        self.manager.rename(
            str(self.root), opened["organizationId"], {"v0": "tintAndAlpha"}
        )
        proposal_path = Path(opened["proposalSourcePath"])
        proposal = proposal_path.read_text(encoding="utf-8")
        proposal_path.write_text(
            "// semantic-change\n" + proposal.replace("1.0", "2.0"), encoding="utf-8"
        )
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertFalse(reviewed["verdict"]["accepted"])
        self.assertEqual(reviewed["equivalenceLevel"], "rejected")
        self.assertTrue(reviewed["gate"]["rawRecoveryCompiledAndAbiValidated"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        with self.assertRaisesRegex(ShaderOrganizationError, "not completed and accepted"):
            self.manager.validate_publication_gate(str(self.root))

    def test_structured_flow_graph_records_calls_ai_descriptions_and_locations(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "float3 ApplyLighting(float3 value) { return value; }\n"
            "float4 MainPS() : SV_Target\n"
            "{\n"
            "  return float4(ApplyLighting(float3(1.0, 1.0, 1.0)), 1.0);\n"
            "}\n",
            encoding="utf-8",
        )
        Path(opened["moduleDescriptionsPath"]).write_text(
            json.dumps(
                {
                    "schema": "replaylab-shader-module-descriptions/v1",
                    "schemaVersion": 1,
                    "modules": [
                        {
                            "name": "ApplyLighting",
                            "description": "Evaluates the reconstructed lighting term.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        reviewed = self.manager.review(str(self.root), opened["organizationId"])

        graph = reviewed["flowGraph"]
        nodes = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(list(nodes), ["MainPS", "ApplyLighting"])
        self.assertEqual(
            nodes["ApplyLighting"]["description"],
            "Evaluates the reconstructed lighting term.",
        )
        self.assertEqual(nodes["ApplyLighting"]["source"]["line"], 1)
        self.assertEqual(nodes["MainPS"]["source"]["line"], 4)
        self.assertEqual(
            graph["edges"],
            [
                {
                    "source": nodes["MainPS"]["id"],
                    "target": nodes["ApplyLighting"]["id"],
                    "kind": "calls",
                }
            ],
        )

    def test_review_publishes_algorithm_stages_in_both_graph_artifacts(self) -> None:
        opened = self._begin()
        proposal = "float4 MainPS() : SV_Target {\n  float4 color = 1.0;\n  return color;\n}\n"
        Path(opened["proposalSourcePath"]).write_text(proposal, encoding="utf-8")
        spec = {"schema": "replaylab-shader-flow-spec/v1", "inputs": [], "resources": [], "outputs": ["SV_Target"],
                "stages": [
                    {"id": "prepare", "label": "Prepare color", "function": "MainPS", "sourceRange": {"startLine": 2, "endLine": 2},
                     "reads": [], "writes": ["color"], "next": ["output"]},
                    {"id": "output", "label": "Write color", "function": "MainPS", "sourceRange": {"startLine": 3, "endLine": 3},
                     "reads": ["color"], "writes": ["SV_Target"], "next": []},
                ]}
        Path(opened["flowSpecPath"]).write_text(json.dumps(spec), encoding="utf-8")
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertTrue(reviewed["verdict"]["accepted"])
        graph = reviewed["flowGraph"]
        self.assertEqual(graph["graphKind"], "algorithm-dataflow")
        self.assertEqual([node["label"] for node in graph["nodes"]], ["Prepare color", "Write color"])
        self.assertEqual(graph["edges"][0]["values"], ["color"])
        self.assertIn("Prepare color", (self.root / "ai" / "flowchart.generated.mmd").read_text(encoding="utf-8"))
        self.manager.validate_publication_gate(str(self.root))

    def test_comment_only_large_body_fails_before_candidate_compile(self) -> None:
        self.original = ("float4 MainPS() : SV_Target {\nfloat4 _1 = 1;\n" + "_1 = _1 + 0;\n" * 180 + "return _1;\n}").encode()
        self.service.source_path.write_bytes(self.original)
        self.service.decompiled_sha = hashlib.sha256(self.original).hexdigest()
        opened = self._begin()
        self.assertTrue(opened["organizationContract"]["needsAlgorithmRestructuring"])
        Path(opened["proposalSourcePath"]).write_bytes(b"// Organized explanation\n" + self.original)
        previous_revision = self.service.revision
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertFalse(reviewed["verdict"]["accepted"])
        self.assertIn("algorithm-organization-incomplete", [reason["code"] for reason in reviewed["verdict"]["reasons"]])
        self.assertEqual(self.service.revision, previous_revision)
        self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_invalid_module_descriptions_fail_before_review_compiler(self) -> None:
        opened = self._begin()
        Path(opened["moduleDescriptionsPath"]).write_text(
            json.dumps(
                {
                    "schema": "replaylab-shader-module-descriptions/v1",
                    "schemaVersion": 1,
                    "modules": [
                        {"name": "MainPS", "description": "unsafe\nmultiline"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        attempts = self.service.revision

        result = self.manager.review(str(self.root), opened["organizationId"])
        self.assertFalse(result["verdict"]["accepted"])
        self.assertEqual(result["verdict"]["reasons"][0]["code"], "module-description-schema")
        self.assertIn("single-line", result["verdict"]["reasons"][0]["message"])

        self.assertEqual(self.service.revision, attempts)
        self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_publication_validator_rejects_flow_graph_tamper(self) -> None:
        unused_opened, reviewed = self._accepted_review()
        graph_path = Path(reviewed["flowGraphPath"])
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["nodes"][0]["description"] = "Tampered explanation."
        graph_path.write_text(json.dumps(graph), encoding="utf-8")

        with self.assertRaisesRegex(
            ShaderOrganizationError, "flow graph content changed"
        ):
            self.manager.validate_publication_gate(
                str(self.root), self.service.status(str(self.root))
            )

    def test_publication_validator_rejects_forged_pending_root_gate(self) -> None:
        self._begin()
        root_gate_path = self.root / "ai" / "organize-code-gate.json"
        forged = json.loads(root_gate_path.read_text(encoding="utf-8"))
        forged.update(
            {
                "decision": "accepted-executable-editable",
                "codeSha256Equal": True,
                "organizedSourcePromoted": True,
                "safeToStartLiveEdit": True,
            }
        )
        root_gate_path.write_text(json.dumps(forged), encoding="utf-8")

        with self.assertRaisesRegex(
            ShaderOrganizationError, "not completed and accepted"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_completed_session_hmac_tamper(self) -> None:
        opened, unused_reviewed = self._accepted_review()
        session_path = Path(opened["sessionPath"])
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["organizedCodeSha256"] = "0" * 64
        session_path.write_text(json.dumps(session), encoding="utf-8")

        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_root_session_gate_split(self) -> None:
        unused_opened, reviewed = self._accepted_review()
        root_gate_path = Path(reviewed["gatePath"])
        root_gate = json.loads(root_gate_path.read_text(encoding="utf-8"))
        root_gate["note"] = "tampered root-only note"
        root_gate_path.write_text(json.dumps(root_gate), encoding="utf-8")

        with self.assertRaisesRegex(
            ShaderOrganizationError, "root and session publication gates differ"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_equal_gates_disconnected_from_session(
        self,
    ) -> None:
        opened, reviewed = self._accepted_review()
        tampered = dict(reviewed["gate"])
        tampered["proposalSourceSha256"] = "0" * 64
        Path(reviewed["gatePath"]).write_text(
            json.dumps(tampered), encoding="utf-8"
        )
        session_gate_path = (
            Path(opened["sessionPath"]).parent / "gate.json"
        )
        session_gate_path.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaisesRegex(
            ShaderOrganizationError, "differs from its sealed session"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_raw_recovery_on_accepted_gate(
        self,
    ) -> None:
        opened, reviewed = self._accepted_review()
        tampered = dict(reviewed["gate"])
        tampered["rawRecoveryCompiledAndAbiValidated"] = True
        Path(reviewed["gatePath"]).write_text(
            json.dumps(tampered), encoding="utf-8"
        )
        (Path(opened["sessionPath"]).parent / "gate.json").write_text(
            json.dumps(tampered), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            ShaderOrganizationError, "not an accepted organize-only review"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_current_code_disconnect(self) -> None:
        self._accepted_review()
        self.service.latest["codeSha256"] = "0" * 64

        with self.assertRaisesRegex(
            ShaderOrganizationError, "stale or internally disconnected"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_publication_validator_rejects_current_source_disconnect(self) -> None:
        self._accepted_review()
        self.service.source_path.write_bytes(self.original + b"// later edit\n")

        with self.assertRaisesRegex(
            ShaderOrganizationError, "source evidence changed"
        ):
            self.manager.validate_publication_gate(str(self.root))

    def test_completed_publication_survives_manager_restart_without_re_review(self) -> None:
        unused_opened, reviewed = self._accepted_review()
        restarted = ShaderOrganizationManager(
            lambda: self.service,
            mock.Mock(side_effect=AssertionError("restart must not repeat ABI review")),
        )
        self.assertNotEqual(self.manager._session_hmac_key, restarted._session_hmac_key)
        self.assertEqual(restarted.validate_publication_gate(str(self.root)), reviewed["gate"])

    def test_unfinished_publication_still_fails_closed_after_restart(self) -> None:
        self._begin()
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            restarted.validate_publication_gate(str(self.root))

    def test_completed_receipt_cannot_authorize_edited_session_after_restart(self) -> None:
        opened, unused_reviewed = self._accepted_review()
        session_path = Path(opened["sessionPath"])
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["organizedCodeSha256"] = "f" * 64
        session_path.write_text(json.dumps(session), encoding="utf-8")
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            restarted.validate_publication_gate(str(self.root))

    def test_completed_receipt_does_not_replace_current_abi_binding_after_restart(self) -> None:
        self._accepted_review()
        self.service.latest["abiGate"]["reportSha256"] = "f" * 64
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        with self.assertRaisesRegex(ShaderOrganizationError, "stale or internally disconnected"):
            restarted.validate_publication_gate(str(self.root))

    def test_missing_completed_receipt_does_not_trust_legacy_disk_records(self) -> None:
        self._accepted_review()
        self.manager._completed_reviews.path.unlink()
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            restarted.validate_publication_gate(str(self.root))
        self.assertFalse(self.manager._completed_reviews.path.exists())

    def test_corrupt_receipt_store_never_trusts_disk_records(self) -> None:
        self._accepted_review()
        self.manager._completed_reviews.path.write_bytes(b"not a database")
        restarted = ShaderOrganizationManager(lambda: self.service, lambda *args: {})
        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            restarted.validate_publication_gate(str(self.root))

    def test_publication_validator_does_not_invert_review_lease_order(self) -> None:
        opened, reviewed = self._accepted_review()
        session_path = Path(opened["sessionPath"]).resolve()
        completed_session = json.loads(session_path.read_text(encoding="utf-8"))
        current_source = self.service.source_path.read_bytes()
        awaiting_session = dict(completed_session)
        awaiting_session["state"] = "awaiting-proposal-review"
        awaiting_session["baselineSourcePath"] = awaiting_session[
            "proposalSourcePath"
        ]
        awaiting_session["baselineSourceSha256"] = hashlib.sha256(
            current_source
        ).hexdigest()
        awaiting_session["baselineCodeSha256"] = self.service._code_sha(
            current_source
        )
        awaiting_session = self.manager._seal_session(awaiting_session)

        real_read_json_object = organization_module.read_json_object
        real_lease_acquire = WorkspaceSourceTransactionLease.acquire
        review_waiting_for_lease = threading.Event()
        release_review = threading.Event()
        publication_done = threading.Event()
        review_errors: list[Exception] = []
        publication_errors: list[Exception] = []
        publication_results: list[dict] = []
        review_thread: threading.Thread

        def thread_specific_read(path, maximum_bytes, description):
            if (
                threading.current_thread() is review_thread
                and Path(path).resolve() == session_path
            ):
                return copy.deepcopy(awaiting_session)
            return real_read_json_object(path, maximum_bytes, description)

        def blocking_review_acquire(lease):
            if threading.current_thread() is review_thread:
                review_waiting_for_lease.set()
                release_review.wait(3.0)
                raise RuntimeError("stop synthetic review lease wait")
            return real_lease_acquire(lease)

        def run_review() -> None:
            try:
                self.manager.review(str(self.root), opened["organizationId"])
            except Exception as exc:
                review_errors.append(exc)

        def run_publication_validation() -> None:
            try:
                publication_results.append(
                    self.manager.validate_publication_gate(str(self.root))
                )
            except Exception as exc:
                publication_errors.append(exc)
            finally:
                publication_done.set()

        publication_lease = WorkspaceSourceTransactionLease(self.root.resolve())
        publication_lease.acquire()
        publication_thread: threading.Thread | None = None
        reached_review_wait = False
        completed_without_guard = False
        try:
            with (
                mock.patch.object(
                    organization_module,
                    "read_json_object",
                    side_effect=thread_specific_read,
                ),
                mock.patch.object(
                    WorkspaceSourceTransactionLease,
                    "acquire",
                    new=blocking_review_acquire,
                ),
            ):
                review_thread = threading.Thread(target=run_review, daemon=True)
                review_thread.start()
                reached_review_wait = review_waiting_for_lease.wait(1.0)
                if reached_review_wait:
                    publication_thread = threading.Thread(
                        target=run_publication_validation, daemon=True
                    )
                    publication_thread.start()
                    completed_without_guard = publication_done.wait(1.0)
                release_review.set()
                review_thread.join(3.0)
                if publication_thread is not None:
                    publication_thread.join(3.0)
        finally:
            release_review.set()
            publication_lease.release()

        self.assertTrue(reached_review_wait)
        self.assertTrue(completed_without_guard)
        self.assertFalse(review_thread.is_alive())
        if publication_thread is not None:
            self.assertFalse(publication_thread.is_alive())
        self.assertEqual(publication_errors, [])
        self.assertEqual(publication_results, [reviewed["gate"]])
        self.assertEqual(len(review_errors), 1)
        self.assertIn("synthetic review lease wait", str(review_errors[0]))

    def test_code_change_is_provisional_and_raw_source_is_revalidated(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\n"
            "float4 MainPS() : SV_Target { return 2.0; }\n",
            encoding="utf-8",
        )
        reviewed = self.manager.review(
            str(self.root), opened["organizationId"]
        )

        self.assertFalse(reviewed["gate"]["codeSha256Equal"])
        self.assertFalse(reviewed["gate"]["organizedSourcePromoted"])
        self.assertFalse(reviewed["gate"]["safeToStartLiveEdit"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertIsNotNone(reviewed["recoveryCompile"])
        self.assertTrue(
            (self.root / "ai" / "organized-provisional.hlsl").is_file()
        )

    def test_concurrent_source_edit_is_not_overwritten(self) -> None:
        opened = self._begin()
        concurrent = self.original + b"// user edit after begin\n"
        self.service.source_path.write_bytes(concurrent)

        with self.assertRaisesRegex(
            ShaderOrganizationError, "source changed after"
        ):
            self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.source_path.read_bytes(), concurrent)

    def test_invalid_mermaid_fails_before_proposal_replaces_source(self) -> None:
        opened = self._begin()
        Path(opened["flowchartPath"]).write_text(
            "not a mermaid graph\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ShaderOrganizationError, "Mermaid"):
            self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_active_watcher_blocks_review_before_source_promotion(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\n"
            "float4 MainPS() : SV_Target { return 2.0; }\n",
            encoding="utf-8",
        )
        watches = ShaderWorkspaceWatchManager(lambda: self.service)
        watch_id = watches.start(self.root, 100)["watchId"]
        try:
            with self.assertRaisesRegex(
                ShaderOrganizationError, "exclusive workspace source transaction"
            ):
                self.manager.review(str(self.root), opened["organizationId"])
            self.assertEqual(self.service.source_path.read_bytes(), self.original)
            self.assertEqual(self.service.revision, 1)
            gate = json.loads(
                (self.root / "ai" / "organize-code-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(gate["safeToStartLiveEdit"])
        finally:
            watches.stop(watch_id)

    def test_active_watcher_blocks_begin_before_any_session_or_compile(self) -> None:
        watches = ShaderWorkspaceWatchManager(lambda: self.service)
        watch_id = watches.start(self.root, 100)["watchId"]
        try:
            with self.assertRaisesRegex(
                ShaderOrganizationError, "organization begin requires the exclusive"
            ):
                self._begin()
            self.assertEqual(self.service.revision, 0)
            self.assertFalse((self.root / "ai").exists())
        finally:
            watches.stop(watch_id)

        # The exact watcher owner released its token, so begin can now acquire
        # the same transaction lease and compile its authoritative baseline.
        opened = self._begin()
        self.assertEqual(self.service.revision, 1)
        self.assertTrue(Path(opened["sessionPath"]).is_file())

    def test_review_lease_blocks_watcher_start_until_final_commit(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        entered = threading.Event()
        release = threading.Event()
        original_validate = self.manager._validate_candidate

        def blocking_validate(workspace_path, revision, raw_sha256):
            if revision == 2:
                entered.set()
                if not release.wait(2.0):
                    raise RuntimeError("test did not release organization review")
            return original_validate(workspace_path, revision, raw_sha256)

        self.manager._validate_candidate = blocking_validate
        results = []
        errors = []

        def run_review():
            try:
                results.append(
                    self.manager.review(str(self.root), opened["organizationId"])
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_review)
        thread.start()
        self.assertTrue(entered.wait(1.0))
        watches = ShaderWorkspaceWatchManager(lambda: self.service)
        try:
            with self.assertRaisesRegex(Exception, "cross-process watch lease"):
                watches.start(self.root, 100)
        finally:
            release.set()
            thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["gate"]["safeToStartLiveEdit"])

    def test_session_baseline_code_tamper_cannot_authorize_proposal(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\n"
            "float4 MainPS() : SV_Target { return 2.0; }\n",
            encoding="utf-8",
        )
        session_path = Path(opened["sessionPath"])
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["baselineCodeSha256"] = self.service._code_sha(
            Path(opened["proposalSourcePath"]).read_bytes()
        )
        session_path.write_text(json.dumps(session), encoding="utf-8")
        attempts = self.service.revision

        with self.assertRaisesRegex(ShaderOrganizationError, "integrity seal"):
            self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.revision, attempts)
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        gate = json.loads(
            (self.root / "ai" / "organize-code-gate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(gate["safeToStartLiveEdit"])

    def test_proposal_directives_are_rejected_before_review_compiler(self) -> None:
        for directive in ('#include "outside.hlsl"', "#line 900"):
            with self.subTest(directive=directive):
                opened = self._begin()
                Path(opened["proposalSourcePath"]).write_text(
                    directive
                    + "\nfloat4 MainPS() : SV_Target { return 1.0; }\n",
                    encoding="utf-8",
                )
                attempts = self.service.revision
                with self.assertRaisesRegex(
                    ShaderOrganizationError, "#include or #line"
                ):
                    self.manager.review(str(self.root), opened["organizationId"])
                self.assertEqual(self.service.revision, attempts)
                self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_mermaid_active_content_is_rejected_before_review_compiler(self) -> None:
        unsafe_values = (
            "flowchart TD\n  A-->B\n  click A https://example.invalid\n",
            "flowchart TD\n  A[https://example.invalid]\n",
            "flowchart TD\n  A[<b>unsafe</b>]\n",
            "%%{init: {'theme':'dark'}}%%\nflowchart TD\n  A-->B\n",
        )
        for flowchart in unsafe_values:
            with self.subTest(flowchart=flowchart):
                opened = self._begin()
                Path(opened["flowchartPath"]).write_text(
                    flowchart, encoding="utf-8"
                )
                attempts = self.service.revision
                with self.assertRaisesRegex(ShaderOrganizationError, "Mermaid"):
                    self.manager.review(str(self.root), opened["organizationId"])
                self.assertEqual(self.service.revision, attempts)

    def test_session_symlink_is_rejected(self) -> None:
        opened = self._begin()
        session_path = Path(opened["sessionPath"])
        external = self.root / "external-session.json"
        external.write_bytes(session_path.read_bytes())
        session_path.unlink()
        try:
            session_path.symlink_to(external)
        except OSError as exc:
            self.skipTest("symlink creation is unavailable: %s" % exc)

        with self.assertRaisesRegex(ValueError, "regular file"):
            self.manager.review(str(self.root), opened["organizationId"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)

    def test_final_session_commit_failure_never_leaves_safe_gate(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        real_atomic_write_json = organization_module.atomic_write_json

        def fail_completed_session(path, value):
            if Path(path).name == "session.json" and value.get("state") == "completed":
                raise OSError("synthetic final session commit failure")
            return real_atomic_write_json(path, value)

        with mock.patch.object(
            organization_module,
            "atomic_write_json",
            side_effect=fail_completed_session,
        ):
            with self.assertRaisesRegex(
                ShaderOrganizationError, "final session commit failure"
            ):
                self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        gate = json.loads(
            (self.root / "ai" / "organize-code-gate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(gate["safeToStartLiveEdit"])
        self.assertTrue(gate["sourceRestored"])
        self.assertTrue(gate["finalBinding"]["complete"])
        self.assertEqual(
            gate["finalBinding"]["sourceSha256"],
            hashlib.sha256(self.original).hexdigest(),
        )

    def test_durable_receipt_write_failure_restores_source_and_blocks_publication(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_bytes(self.original + b"// readable\n")
        with mock.patch.object(self.manager._completed_reviews, "remember",
                               side_effect=OSError("receipt store unavailable")):
            with self.assertRaisesRegex(ShaderOrganizationError, "receipt store unavailable"):
                self.manager.review(str(self.root), opened["organizationId"])
        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        gate = json.loads((self.root / "ai" / "organize-code-gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["safeToStartLiveEdit"])
        self.assertTrue(gate["sourceRestored"])

    def test_concurrent_user_save_during_review_is_not_overwritten(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        concurrent = b"float4 MainPS() : SV_Target { return 9.0; }\n"
        original_validate = self.manager._validate_candidate

        def concurrent_validate(workspace_path, revision, raw_sha256):
            result = original_validate(workspace_path, revision, raw_sha256)
            if self.service.source_path.read_bytes() != self.original:
                self.service.source_path.write_bytes(concurrent)
            return result

        self.manager._validate_candidate = concurrent_validate
        with self.assertRaisesRegex(
            ShaderOrganizationError, "refusing to overwrite newer bytes"
        ):
            self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.source_path.read_bytes(), concurrent)
        gate = json.loads(
            (self.root / "ai" / "organize-code-gate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(gate["safeToStartLiveEdit"])
        self.assertFalse(gate["sourceRestored"])

    @unittest.skipUnless(os.name == "nt", "Win32 share-mode CAS test")
    def test_source_cas_denies_unmanaged_write_in_compare_write_window(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        concurrent = b"float4 MainPS() : SV_Target { return 9.0; }\n"
        real_evidence_write = organization_module._exclusive_durable_write
        blocked_writes = []

        def attempt_unmanaged_write(path, value, label):
            try:
                self.service.source_path.write_bytes(concurrent)
            except OSError as exc:
                blocked_writes.append(exc)
            return real_evidence_write(path, value, label)

        with mock.patch.object(
            organization_module,
            "_exclusive_durable_write",
            side_effect=attempt_unmanaged_write,
        ):
            reviewed = self.manager.review(
                str(self.root), opened["organizationId"]
            )

        self.assertTrue(blocked_writes)
        self.assertTrue(reviewed["gate"]["safeToStartLiveEdit"])
        self.assertNotEqual(self.service.source_path.read_bytes(), concurrent)
        self.assertFalse((self.root / ".workspace-source-transaction.json").exists())
        self.assertFalse((self.root / ".workspace-source-transaction.backup").exists())
        self.assertFalse((self.root / ".workspace-watch.lock").exists())

        # Once the transaction is complete the editor owns its save normally.
        self.service.source_path.write_bytes(concurrent)
        self.assertEqual(self.service.source_path.read_bytes(), concurrent)

    def test_partial_source_write_is_recovered_before_lease_release(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        real_write = organization_module._write_locked_source
        calls = 0

        def fail_first_write(handle, value, label):
            nonlocal calls
            calls += 1
            if calls == 1:
                handle.seek(0)
                handle.write(b"partial")
                handle.truncate(7)
                handle.flush()
                raise OSError("synthetic partial source write")
            return real_write(handle, value, label)

        with mock.patch.object(
            organization_module,
            "_write_locked_source",
            side_effect=fail_first_write,
        ):
            with self.assertRaisesRegex(
                ShaderOrganizationError, "synthetic partial source write"
            ):
                self.manager.review(str(self.root), opened["organizationId"])

        self.assertEqual(self.service.source_path.read_bytes(), self.original)
        self.assertFalse((self.root / ".workspace-source-transaction.json").exists())
        self.assertFalse((self.root / ".workspace-source-transaction.backup").exists())
        self.assertFalse((self.root / ".workspace-watch.lock").exists())

    def test_review_then_live_edit_first_save_and_failure_preserve_last_good(self) -> None:
        opened = self._begin()
        Path(opened["proposalSourcePath"]).write_text(
            "// readable-only\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        reviewed = self.manager.review(str(self.root), opened["organizationId"])
        self.assertTrue(reviewed["gate"]["safeToStartLiveEdit"])
        review_revision = reviewed["organizedCompile"]["revision"]
        calls = []

        def apply_candidate(_workspace, revision, raw_sha256):
            calls.append(revision)
            if revision == review_revision + 2:
                return _rejected_result(revision, raw_sha256)
            return _compatible_result(self.root, revision, raw_sha256)

        self.live_manager = ShaderLiveEditManager(
            ShaderWorkspaceWatchManager(lambda: self.service),
            apply_candidate,
            poll_seconds=0.005,
            join_seconds=0.5,
        )
        started = self.live_manager.start(self.root, 100)
        time.sleep(0.15)
        self.assertEqual(
            self.live_manager.poll(started["liveEditId"])["applyAttempts"], 0
        )

        self.service.source_path.write_text(
            "// readable-only\n// first-save\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        _wait_until(
            lambda: self.live_manager.poll(started["liveEditId"])[
                "applySuccesses"
            ]
            == 1
        )
        successful = self.live_manager.poll(started["liveEditId"])
        last_good = copy.deepcopy(successful["activeReplacement"])
        rollback = copy.deepcopy(successful["rollback"])

        self.service.source_path.write_text(
            "// readable-only\n// rejected-save\n"
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            encoding="utf-8",
        )
        _wait_until(
            lambda: self.live_manager.poll(started["liveEditId"])[
                "applyRejections"
            ]
            == 1
        )
        rejected = self.live_manager.poll(started["liveEditId"])
        self.assertEqual(calls, [review_revision + 1, review_revision + 2])
        self.assertEqual(rejected["activeReplacement"], last_good)
        self.assertEqual(rejected["lastGoodReplacement"], last_good)
        self.assertEqual(rejected["rollback"], rollback)
        self.live_manager.stop(started["liveEditId"])


if __name__ == "__main__":
    unittest.main()


_STRUCTURED = b"""cbuffer View : register(b0)
{
    float Exposure : packoffset(c0.x);
};
static float4 out_color;

void frag_main()
{
    float _10 = 0.5f * Exposure;
    float _11 = _10 * 2.0f;
    float _12 = _11 + 1.0f;
    out_color = float4(_12, _12, _12, 1.0f);
}

float4 MainPS() : SV_Target0
{
    frag_main();
    return out_color;
}
"""


class ShaderRestructureSessionTests(unittest.TestCase):
    """Session-level structural operations on a multi-statement baseline."""

    _begin = ShaderOrganizationTests._begin
    tearDown = ShaderOrganizationTests.tearDown

    def setUp(self) -> None:
        ShaderOrganizationTests.setUp(self)
        self.original = _STRUCTURED
        self.service.source_path.write_bytes(self.original)
        self.service.decompiled_sha = hashlib.sha256(self.original).hexdigest()

    def test_extract_is_sealed_replayed_and_survives_renames(self) -> None:
        opened = self._begin()
        organization = opened["organizationId"]
        result = self.manager.restructure(str(self.root), organization, "extract",
                                          start_line=9, end_line=11, name="ComputeIntensity",
                                          comment="Exposure-scaled intensity.")
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["lastReceipt"]["returns"], "_12")
        self.assertEqual(result["compile"], {"ok": True, "diagnostics": []})
        proposal = Path(result["proposalSourcePath"]).read_text(encoding="utf-8")
        self.assertIn("float ComputeIntensity()", proposal)
        self.assertIn("    float _12 = ComputeIntensity();", proposal)
        names = [f["name"] for f in result["structure"]["functions"]]
        self.assertEqual(names, ["ComputeIntensity", "frag_main", "MainPS"])
        ledger = {row["name"]: row for row in result["ledger"]}
        self.assertEqual(ledger["ComputeIntensity"]["purpose"], "Exposure-scaled intensity.")
        self.assertEqual(ledger["ComputeIntensity"]["outputs"], ["_12"])
        self.assertEqual(ledger["ComputeIntensity"]["globalsUsed"], ["Exposure"])
        self.assertTrue(ledger["ComputeIntensity"]["origin"].startswith("extracted from frag_main"))
        self.assertEqual(ledger["frag_main"]["calls"], ["ComputeIntensity"])
        noted = self.manager.restructure(str(self.root), organization, "note", name="frag_main",
                                         comment="Entry body: computes the intensity and packs the colour.")
        self.assertEqual({row["name"]: row["purpose"] for row in noted["ledger"]}["frag_main"],
                         "Entry body: computes the intensity and packs the colour.")
        listed = self.manager.restructure(str(self.root), organization, "list")
        self.assertEqual(listed["operations"], 1)
        statuses = {row["name"]: row["status"] for row in listed["ledger"]}
        self.assertEqual(statuses["frag_main"], "noted")  # still flagged: anonymous locals remain
        self.assertEqual(statuses["MainPS"], "provisional")
        # renames regenerate through the same pipeline: the extraction is kept
        renamed = self.manager.rename(str(self.root), organization, {"_12": "intensity"})
        self.assertEqual(renamed["structureOps"], 1)
        proposal = Path(renamed["proposalSourcePath"]).read_text(encoding="utf-8")
        self.assertIn("float intensity = ComputeIntensity();", proposal)
        self.assertIn("return intensity;", proposal)
        self.assertFalse(renamed["renameOnlyEquivalent"])
        # rename the extracted function itself
        result = self.manager.restructure(str(self.root), organization, "rename_function",
                                          name="ComputeIntensity", new_name="ExposedIntensity")
        self.assertTrue(result["accepted"])
        proposal = Path(result["proposalSourcePath"]).read_text(encoding="utf-8")
        self.assertIn("float ExposedIntensity()", proposal)
        self.assertNotIn("ComputeIntensity", proposal)
        # undo restores the plain renamed baseline
        result = self.manager.restructure(str(self.root), organization, "undo")
        self.assertEqual(result["operations"], [])
        proposal = Path(result["proposalSourcePath"]).read_text(encoding="utf-8")
        self.assertNotIn("ExposedIntensity", proposal)
        self.assertIn("float intensity = _11 + 1.0f;", proposal)

    def test_rejected_extraction_changes_nothing(self) -> None:
        opened = self._begin()
        organization = opened["organizationId"]
        before = Path(self.root / opened["proposalSourcePath"]).read_bytes() if (self.root / str(opened.get("proposalSourcePath", ""))).exists() else None
        result = self.manager.restructure(str(self.root), organization, "extract",
                                          start_line=17, end_line=18, name="Bad")
        self.assertFalse(result["accepted"])
        self.assertIn("return", result["reason"])
        self.assertFalse(result["counted"])
        session = json.loads((self.root / "ai" / "organization-sessions" / organization / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["structureOps"], [])
        # a compile failure is also a clean rejection
        with mock.patch.object(self.service, "compile_check", return_value={"ok": False, "diagnostics": [{"message": "boom"}]}):
            result = self.manager.restructure(str(self.root), organization, "extract",
                                              start_line=9, end_line=11, name="ComputeIntensity")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["compile"]["diagnostics"][0]["message"], "boom")
        session = json.loads((self.root / "ai" / "organization-sessions" / organization / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["structureOps"], [])
