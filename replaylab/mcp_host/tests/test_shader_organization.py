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
from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)


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
            self.service.latest["abiGate"] = {
                "status": "compatible",
                "requestId": request_id,
                "reportSha256": report_sha256,
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
        return self.manager.begin(
            str(self.root),
            {
                "schema": "test-semantic-context/v1",
                "textures": [{"slot": 0, "role": "base-color"}],
            },
        )

    def test_exact_code_identity_promotes_readable_proposal(self) -> None:
        opened = self._begin()
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
            "# Algorithm\n\nReturns one render target.\n", encoding="utf-8"
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
            if revision == 3:
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
