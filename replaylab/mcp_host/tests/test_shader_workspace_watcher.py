from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import ShaderWorkspaceWatchManager, WorkspaceWatchError
from src.shader_workspace.leases import WorkspaceSourceTransactionLease


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _FakeWorkspaceService:
    def __init__(
        self,
        root: Path,
        block_first: bool = False,
        fail_first: bool = False,
        fail_attempt: int = 0,
    ):
        self.root = root.resolve()
        self.block_first = block_first
        self.fail_attempt = fail_attempt or (1 if fail_first else 0)
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.compile_hashes: list[str] = []
        self.latest: dict | None = None
        self.decompiled_hash = _sha((self.root / "source" / "shader.hlsl").read_bytes())

    def status(self, workspace_path) -> dict:
        root = Path(workspace_path).resolve()
        if root != self.root:
            raise RuntimeError("unexpected workspace")
        source = root / "source" / "shader.hlsl"
        current = _sha(source.read_bytes())
        return {
            "workspacePath": str(root),
            "source": {
                "present": True,
                "path": str(source),
                "currentSha256": current,
                "decompiledSha256": self.decompiled_hash,
                "lane": "executable-editable",
            },
            "representation": {
                "present": True,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "latestCompiled": dict(self.latest) if self.latest else None,
        }

    def compile(self, workspace_path) -> dict:
        root = Path(workspace_path).resolve()
        source = (root / "source" / "shader.hlsl").read_bytes()
        source_hash = _sha(source)
        attempt = len(self.compile_hashes) + 1
        self.compile_hashes.append(source_hash)
        if attempt == 1:
            self.first_started.set()
            if self.block_first and not self.release_first.wait(3.0):
                raise RuntimeError("test compile release timed out")
        if attempt == self.fail_attempt:
            raise RuntimeError("intentional compiler failure")
        candidate_hash = _sha(b"compiled:" + source)
        self.latest = {
            "sourceSha256": source_hash,
            "revision": attempt,
            "rawSha256": candidate_hash,
        }
        return {
            "revision": attempt,
            "sourceSha256": source_hash,
            "rawSha256": candidate_hash,
        }


class ShaderWorkspaceWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temp_root))
        self.root = Path(self.temporary.name).resolve()
        (self.root / "source").mkdir()
        self.source = self.root / "source" / "shader.hlsl"
        self.initial = b"float4 main() : SV_Target { return 0; }\n"
        self.source.write_bytes(self.initial)
        self.managers: list[ShaderWorkspaceWatchManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self.temporary.cleanup()

    def _manager(self, service: _FakeWorkspaceService) -> ShaderWorkspaceWatchManager:
        manager = ShaderWorkspaceWatchManager(lambda: service)
        self.managers.append(manager)
        return manager

    def _wait_for(self, manager, watch_id, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = manager.poll(watch_id)
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail("watcher condition was not reached: %r" % manager.poll(watch_id))

    def test_debounce_and_latest_saved_revision_wins(self) -> None:
        service = _FakeWorkspaceService(self.root, block_first=True)
        manager = self._manager(service)
        started = manager.start(self.root, 100)
        watch_id = started["watchId"]

        time.sleep(0.18)
        self.assertEqual(service.compile_hashes, [])

        first = b"float4 main() : SV_Target { return 1; }\n"
        newest = b"float4 main() : SV_Target { return 2; }\n"
        self.source.write_bytes(first)
        self.assertTrue(service.first_started.wait(3.0))
        self.source.write_bytes(newest)
        service.release_first.set()

        caught_up = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 2,
        )
        self.assertEqual(service.compile_hashes, [_sha(first), _sha(newest)])
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(newest))
        self.assertEqual(caught_up["observedSourceSha256"], _sha(newest))
        self.assertEqual(caught_up["latestCandidateRevision"], 2)
        self.assertEqual(
            caught_up["latestCandidateRawSha256"],
            _sha(b"compiled:" + newest),
        )
        self.assertFalse(caught_up["candidatePolicy"]["applied"])
        self.assertTrue(caught_up["candidatePolicy"]["requiresReplayAbiValidation"])
        event_types = [event["type"] for event in caught_up["events"]]
        self.assertIn("sourceChangedDuringCompile", event_types)
        self.assertEqual(event_types.count("compileSucceeded"), 2)

        audit = json.loads(Path(caught_up["auditPath"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["watchId"], watch_id)
        self.assertEqual(audit["lastCompiledSourceSha256"], _sha(newest))
        stopped = manager.stop(watch_id)
        self.assertEqual(stopped["state"], "stopped")

    def test_explanatory_lane_is_never_watchable(self) -> None:
        class _ExplanatoryService(_FakeWorkspaceService):
            def status(self, workspace_path) -> dict:
                result = super().status(workspace_path)
                result["source"]["lane"] = "explanatory-ir"
                result["representation"].update(
                    {"lane": "explanatory-ir", "compileEligible": False}
                )
                return result

        service = _ExplanatoryService(self.root)
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "executable-editable"):
            manager.start(self.root, 100)
        self.assertEqual(service.compile_hashes, [])

    def test_compile_failure_is_audited_and_new_hash_can_recover(self) -> None:
        service = _FakeWorkspaceService(self.root, fail_first=True)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        failed_source = b"float4 main() : SV_Target { return 3; }\n"
        self.source.write_bytes(failed_source)
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        self.assertEqual(failed["lastError"]["type"], "RuntimeError")
        time.sleep(0.2)
        self.assertEqual(len(service.compile_hashes), 1)

        recovered_source = b"float4 main() : SV_Target { return 4; }\n"
        self.source.write_bytes(recovered_source)
        recovered = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(recovered["compileAttempts"], 2)
        self.assertEqual(recovered["compileFailures"], 1)
        self.assertEqual(recovered["lastCompiledSourceSha256"], _sha(recovered_source))
        self.assertIsNone(recovered["lastError"])
        event_types = [event["type"] for event in recovered["events"]]
        self.assertIn("compileFailed", event_types)
        self.assertIn("compileSucceeded", event_types)
        manager.stop(watch_id)

    def test_transient_windows_file_failure_retries_the_same_save(self) -> None:
        class _TransientService(_FakeWorkspaceService):
            def compile(inner_self, workspace_path) -> dict:
                if not inner_self.compile_hashes:
                    source = (
                        Path(workspace_path) / "source" / "shader.hlsl"
                    ).read_bytes()
                    inner_self.compile_hashes.append(_sha(source))
                    error = PermissionError(13, "revision publish denied")
                    error.winerror = 5
                    raise error
                return super().compile(workspace_path)

        service = _TransientService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        edited = b"float4 main() : SV_Target { return 31; }\n"
        self.source.write_bytes(edited)

        recovered = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"]
            and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited), _sha(edited)])
        self.assertEqual(recovered["compileAttempts"], 2)
        self.assertEqual(recovered["compileFailures"], 1)
        self.assertFalse(recovered["transientRetry"]["scheduled"])
        event_types = [event["type"] for event in recovered["events"]]
        self.assertIn("compileRetryScheduled", event_types)
        manager.stop(watch_id)

    def test_transient_winerror5_persisting_watcher_audit_keeps_watching(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        real_replace = os.replace
        denial_count = 0

        def deny_watcher_audit_once(temporary, destination):
            nonlocal denial_count
            if Path(destination).parent == self.root / "watch" and denial_count == 0:
                denial_count += 1
                error = PermissionError(13, "Access is denied", str(temporary))
                error.winerror = 5
                error.filename2 = str(destination)
                raise error
            return real_replace(temporary, destination)

        with mock.patch(
            "src.shader_workspace.io.os.replace", side_effect=deny_watcher_audit_once
        ), mock.patch("src.shader_workspace.io.time.sleep", return_value=None):
            started = manager.start(self.root, 100)
            edited = b"float4 main() : SV_Target { return 32; }\n"
            self.source.write_bytes(edited)
            recovered = self._wait_for(
                manager,
                started["watchId"],
                lambda result: result["caughtUp"]
                and result["compileSuccesses"] == 1,
            )

        persisted = json.loads(
            Path(recovered["auditPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(denial_count, 1)
        self.assertEqual(recovered["state"], "running")
        self.assertEqual(persisted["compileSuccesses"], 1)
        self.assertEqual(list((self.root / "watch").glob(".*.tmp-*")), [])
        manager.stop(started["watchId"])

    def test_compile_failure_publishes_dxc_line_diagnostics(self) -> None:
        class _DiagnosticService(_FakeWorkspaceService):
            def compile(inner_self, workspace_path) -> dict:
                root = Path(workspace_path)
                source = (root / "source" / "shader.hlsl").read_bytes()
                inner_self.compile_hashes.append(_sha(source))
                job = root / "jobs" / "compile-r0001-diagnostic"
                job.mkdir(parents=True)
                compiler_input = job / "input.hlsl"
                compiler_input.write_bytes(source)
                (job / "stderr.log").write_text(
                    f"{compiler_input}:12:7: error: undeclared identifier 'bad'\n"
                    f"{compiler_input}(14,3): warning X3206: implicit truncation\n",
                    encoding="utf-8",
                )
                raise RuntimeError(f"external tool exited with code 1; see {job}")

        service = _DiagnosticService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        self.source.write_bytes(
            b"float4 main() : SV_Target { return bad; }\n"
        )
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        diagnostics = failed["lastError"]["diagnostics"]
        self.assertEqual(
            [
                {
                    "file": str(self.source),
                    "line": 12,
                    "column": 7,
                    "severity": "error",
                    "message": "undeclared identifier 'bad'",
                },
                {
                    "file": str(self.source),
                    "line": 14,
                    "column": 3,
                    "severity": "warning",
                    "message": "X3206: implicit truncation",
                },
            ],
            diagnostics,
        )
        failed_event = next(
            event
            for event in reversed(failed["events"])
            if event["type"] == "compileFailed"
        )
        self.assertEqual(failed_event["diagnostics"], diagnostics)
        manager.stop(watch_id)

    def test_atomic_replace_save_is_debounced_and_compiled_once(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]

        edited = b"float4 main() : SV_Target { return 9; }\n"
        temporary_source = self.source.with_name("shader.hlsl.ide-save")
        temporary_source.write_bytes(edited)
        temporary_source.replace(self.source)

        caught_up = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited)])
        self.assertEqual(caught_up["observedSourceSha256"], _sha(edited))
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(edited))
        manager.stop(watch_id)

    def test_start_catches_up_source_that_differs_from_existing_candidate(self) -> None:
        service = _FakeWorkspaceService(self.root)
        stale_source = b"float4 main() : SV_Target { return 7; }\n"
        service.latest = {
            "sourceSha256": _sha(stale_source),
            "revision": 4,
            "rawSha256": _sha(b"compiled:" + stale_source),
        }
        current = b"float4 main() : SV_Target { return 8; }\n"
        self.source.write_bytes(current)

        manager = self._manager(service)
        started = manager.start(self.root, 100)
        created = started["events"][0]
        self.assertTrue(created["catchUpCompileScheduled"])
        self.assertEqual(created["staleCandidateSourceSha256"], _sha(stale_source))

        caught_up = self._wait_for(
            manager,
            started["watchId"],
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(current)])
        self.assertEqual(caught_up["observedSourceSha256"], _sha(current))
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(current))
        manager.stop(started["watchId"])

    def test_start_reloads_latest_candidate_after_acquiring_source_lease(self) -> None:
        class _RevisionChangesBeforeLeaseService(_FakeWorkspaceService):
            def __init__(inner_self, root):
                super().__init__(root)
                inner_self.status_calls = 0
                inner_self.latest = {
                    "sourceSha256": _sha(self.initial),
                    "revision": 4,
                    "rawSha256": "4" * 64,
                }

            def status(inner_self, workspace_path):
                result = super().status(workspace_path)
                inner_self.status_calls += 1
                if inner_self.status_calls == 1:
                    # Simulate an organization recovery publishing a newer
                    # candidate after preflight but before watcher lease entry.
                    inner_self.latest = {
                        "sourceSha256": _sha(self.initial),
                        "revision": 5,
                        "rawSha256": "5" * 64,
                    }
                return result

        service = _RevisionChangesBeforeLeaseService(self.root)
        manager = self._manager(service)
        started = manager.start(self.root, 100)
        self.assertGreaterEqual(service.status_calls, 2)
        self.assertEqual(started["latestCandidateRevision"], 5)
        self.assertEqual(started["latestCandidateRawSha256"], "5" * 64)
        self.assertTrue(started["caughtUp"])
        self.assertFalse(started["events"][0]["catchUpCompileScheduled"])
        manager.stop(started["watchId"])

    def test_start_catches_up_edit_saved_before_first_candidate(self) -> None:
        service = _FakeWorkspaceService(self.root)
        edited = b"float4 main() : SV_Target { return 11; }\n"
        self.source.write_bytes(edited)

        manager = self._manager(service)
        started = manager.start(self.root, 100)
        created = started["events"][0]
        self.assertTrue(created["catchUpCompileScheduled"])
        self.assertEqual(created["catchUpReferenceKind"], "decompiled-source")
        self.assertEqual(created["catchUpReferenceSourceSha256"], _sha(self.initial))
        self.assertIsNone(created["staleCandidateSourceSha256"])

        caught_up = self._wait_for(
            manager,
            started["watchId"],
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited)])
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(edited))
        manager.stop(started["watchId"])

    def test_failed_new_save_preserves_previous_candidate_as_last_good(self) -> None:
        service = _FakeWorkspaceService(self.root, fail_attempt=2)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]

        good = b"float4 main() : SV_Target { return 5; }\n"
        self.source.write_bytes(good)
        previous = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        previous_raw = _sha(b"compiled:" + good)
        self.assertEqual(previous["latestCandidateRevision"], 1)
        self.assertEqual(previous["latestCandidateRawSha256"], previous_raw)

        invalid = b"float4 main() : SV_Target { syntax error }\n"
        self.source.write_bytes(invalid)
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        self.assertEqual(failed["latestCandidateRevision"], 1)
        self.assertEqual(failed["latestCandidateRawSha256"], previous_raw)
        self.assertEqual(failed["lastCompiledSourceSha256"], _sha(good))
        self.assertEqual(failed["observedSourceSha256"], _sha(invalid))
        self.assertFalse(failed["caughtUp"])
        self.assertEqual(service.latest["revision"], 1)
        self.assertEqual(service.latest["rawSha256"], previous_raw)
        time.sleep(0.2)
        self.assertEqual(len(service.compile_hashes), 2)
        manager.stop(watch_id)

    def test_explanatory_lane_cannot_start_executable_save_watcher(self) -> None:
        service = _FakeWorkspaceService(self.root)
        original_status = service.status

        def explanatory_status(workspace_path):
            status = original_status(workspace_path)
            status["source"]["lane"] = "explanatory-ir"
            status["representation"]["lane"] = "explanatory-ir"
            status["representation"]["compileEligible"] = False
            return status

        service.status = explanatory_status
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "executable-editable"):
            manager.start(self.root, 100)

    def test_contract_rejects_invalid_debounce_duplicate_and_watch_id(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "integer"):
            manager.start(self.root, True)
        with self.assertRaisesRegex(WorkspaceWatchError, "between"):
            manager.start(self.root, 99)
        watch_id = manager.start(self.root, 100)["watchId"]
        with self.assertRaisesRegex(WorkspaceWatchError, "active watcher"):
            manager.start(self.root, 100)
        with self.assertRaisesRegex(WorkspaceWatchError, "lowercase hex"):
            manager.poll("../../not-a-watch")
        manager.stop(watch_id)

    def test_cross_process_lease_is_fail_closed_and_token_owned(self) -> None:
        first_service = _FakeWorkspaceService(self.root)
        second_service = _FakeWorkspaceService(self.root)
        first = self._manager(first_service)
        second = self._manager(second_service)
        watch_id = first.start(self.root, 100)["watchId"]
        lease_path = self.root / ".workspace-watch.lock"
        owner_token = lease_path.read_bytes()
        self.assertEqual(len(owner_token), 33)

        with self.assertRaisesRegex(WorkspaceWatchError, "cross-process watch lease"):
            second.start(self.root, 100)
        self.assertEqual(lease_path.read_bytes(), owner_token)
        stopped = first.stop(watch_id)
        self.assertTrue(stopped["events"][-1]["watchLeaseReleased"])
        self.assertFalse(lease_path.exists())

        second_id = second.start(self.root, 100)["watchId"]
        lease_path.write_text("another-process-token\n", encoding="ascii")
        retained = second.stop(second_id)
        self.assertFalse(retained["events"][-1]["watchLeaseReleased"])
        self.assertEqual(
            lease_path.read_text(encoding="ascii"), "another-process-token\n"
        )
        lease_path.unlink()

    def test_preexisting_crash_lease_is_never_stolen(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        lease_path = self.root / ".workspace-watch.lock"
        lease_path.write_text("crashed-owner-token\n", encoding="ascii")
        with self.assertRaisesRegex(WorkspaceWatchError, "never assumed stale"):
            manager.start(self.root, 100)
        self.assertEqual(
            lease_path.read_text(encoding="ascii"), "crashed-owner-token\n"
        )
        lease_path.unlink()

    def test_persistent_host_resume_archives_only_a_different_v2_owner_lease(self) -> None:
        previous = WorkspaceSourceTransactionLease(
            self.root, owner_instance_id="1" * 32
        )
        previous.acquire()
        lease_path = self.root / ".workspace-watch.lock"
        old_payload = json.loads(lease_path.read_text(encoding="utf-8"))

        current = WorkspaceSourceTransactionLease(
            self.root,
            owner_instance_id="2" * 32,
            recover_orphaned=True,
        )
        current.acquire()
        new_payload = json.loads(lease_path.read_text(encoding="utf-8"))
        self.assertEqual(new_payload["ownerInstanceId"], "2" * 32)
        archive = self.root / "watch" / (
            "orphaned-lease-%s-%s.json"
            % (old_payload["ownerInstanceId"], old_payload["token"])
        )
        self.assertTrue(archive.is_file())
        self.assertEqual(
            json.loads(archive.read_text(encoding="utf-8")), old_payload
        )
        self.assertTrue(current.release())

    def test_persistent_host_resume_never_steals_same_generation_lease(self) -> None:
        previous = WorkspaceSourceTransactionLease(
            self.root, owner_instance_id="3" * 32
        )
        previous.acquire()
        contender = WorkspaceSourceTransactionLease(
            self.root,
            owner_instance_id="3" * 32,
            recover_orphaned=True,
        )
        with self.assertRaisesRegex(WorkspaceWatchError, "never assumed stale"):
            # WatchManager wraps this error in production; use the same public
            # exception text contract for the lower-level lease here.
            try:
                contender.acquire()
            except Exception as exc:
                raise WorkspaceWatchError(str(exc)) from exc
        self.assertTrue(previous.release())


if __name__ == "__main__":
    unittest.main()
