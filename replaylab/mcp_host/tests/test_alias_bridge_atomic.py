from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vs",
        Hull="hs",
        Domain="ds",
        Geometry="gs",
        Fragment="ps",
        Compute="cs",
        Task="as",
        Mesh="ms",
    )
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_alias_atomic_%s" % uuid.uuid4().hex,
            BRIDGE_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc


class _Context:
    def __init__(self):
        self.capture_path = "D:/captures/current.rdc"
        self.captured_names = {1: "CapturedOne", 2: "CapturedTwo"}
        self.names = {
            1: (True, "OldAliasOne"),
            2: (True, "CapturedTwo"),
        }
        self.fail_once_for = None
        self.fail_on_calls = set()
        self.per_resource_calls = {}
        self.set_calls = []
        self.before_set = None

    def IsCaptureLoaded(self):
        return True

    def GetCaptureFilename(self):
        return self.capture_path

    def HasResourceCustomName(self, resource_id):
        return self.names[int(resource_id)][0]

    def GetResourceNameUnsuffixed(self, resource_id):
        return self.names[int(resource_id)][1]

    def SetResourceCustomName(self, resource_id, name):
        resource_id = int(resource_id)
        if self.before_set is not None:
            self.before_set(resource_id, name)
        self.set_calls.append((resource_id, name))
        call_number = self.per_resource_calls.get(resource_id, 0) + 1
        self.per_resource_calls[resource_id] = call_number
        if self.fail_once_for == resource_id:
            self.fail_once_for = None
            raise RuntimeError("injected rename failure")
        if (resource_id, call_number) in self.fail_on_calls:
            raise RuntimeError("injected scheduled rename failure")
        if name:
            self.names[resource_id] = (True, str(name))
        else:
            self.names[resource_id] = (False, self.captured_names[resource_id])


class AliasBridgeAtomicTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_bridge()
        self.context = _Context()
        self.module._ctx = self.context
        self.module._capture_session_id = "b" * 32
        self.raw_sha256 = hashlib.sha256(b"captured shader").hexdigest()
        self.capture_id = "a" * 64
        self.shader_keys = {
            (1, "fragment", "main", self.raw_sha256),
            (2, "fragment", "main", self.raw_sha256),
        }
        self.module._symbol_capture_snapshot = {
            "captureId": self.capture_id,
            "capturePath": "D:/captures/current.rdc",
            "shaderKeys": set(self.shader_keys),
            "usesByKey": {},
        }
        self.module._revalidate_symbol_shader_keys = lambda keys: (set(keys), {})
        self.module._context_resource_ids = lambda: {1: 1, 2: 2}
        self.module._symbol_alias_overlay = {
            "schema": self.module.SYMBOL_ALIAS_SCHEMA,
            "schemaVersion": self.module.SYMBOL_ALIAS_VERSION,
            "captureId": self.capture_id,
            "sourcePath": "D:/aliases-old.json",
            "confirmedOnly": True,
            "allAliases": [{"alias": "OldAliasOne"}],
            "aliases": [{"alias": "OldAliasOne"}],
        }
        self.module._symbol_resource_renames = {
            1: {"hadCustomName": True, "name": "CapturedOne"}
        }
        self.module._symbol_rename_capture_token = (
            self.module._current_capture_token()
        )
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary.cleanup()

    def _alias(self, resource_id, name, *, verified=True):
        return {
            "verified": verified,
            "metadata": {"programVerified": True},
            "provenance": "program",
            "captureId": self.capture_id,
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha256,
            "bindingKind": "shader",
            "alias": name,
        }

    def _write(self, aliases):
        path = Path(self.temporary.name) / "aliases.json"
        raw = json.dumps(
            {
                "schema": self.module.SYMBOL_ALIAS_SCHEMA,
                "schemaVersion": self.module.SYMBOL_ALIAS_VERSION,
                "captureId": self.capture_id,
                "aliases": aliases,
            },
            sort_keys=True,
        ).encode("utf-8")
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_strict_rename_failure_restores_old_names_and_overlay(self):
        path, digest = self._write(
            [self._alias(1, "NewAliasOne"), self._alias(2, "NewAliasTwo")]
        )
        old_overlay = copy.deepcopy(self.module._symbol_alias_overlay)
        old_renames = copy.deepcopy(self.module._symbol_resource_renames)
        old_token = self.module._symbol_rename_capture_token
        old_names = copy.deepcopy(self.context.names)
        self.context.fail_once_for = 2

        with self.assertRaisesRegex(RuntimeError, "verified immediate rollback"):
            self.module._load_symbol_aliases(
                {
                    "path": str(path),
                    "confirmed_only": True,
                    "expected_sha256": digest,
                    "strict_atomic": True,
                }
            )

        self.assertEqual(self.context.names, old_names)
        self.assertEqual(self.module._symbol_alias_overlay, old_overlay)
        self.assertEqual(self.module._symbol_resource_renames, old_renames)
        self.assertEqual(self.module._symbol_rename_capture_token, old_token)
        self.assertIsNone(self.module._symbol_alias_recovery_ledger)
        receipt = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(receipt["status"], "already-restored")
        self.assertTrue(receipt["verified"])

    def test_strict_skips_fail_before_any_overlay_or_name_mutation(self):
        path, digest = self._write(
            [self._alias(1, "NewAliasOne"), self._alias(2, "Ignored", verified=False)]
        )
        old_overlay = copy.deepcopy(self.module._symbol_alias_overlay)
        old_names = copy.deepcopy(self.context.names)

        with self.assertRaisesRegex(ValueError, "Strict atomic alias load rejected"):
            self.module._load_symbol_aliases(
                {
                    "path": str(path),
                    "confirmed_only": True,
                    "expected_sha256": digest,
                    "strict_atomic": True,
                }
            )

        self.assertFalse(self.context.set_calls)
        self.assertEqual(self.context.names, old_names)
        self.assertEqual(self.module._symbol_alias_overlay, old_overlay)

        conflict_path, conflict_digest = self._write(
            [self._alias(1, "NameA"), self._alias(1, "NameB")]
        )
        with self.assertRaisesRegex(ValueError, "1 conflicts"):
            self.module._load_symbol_aliases(
                {
                    "path": str(conflict_path),
                    "confirmed_only": True,
                    "expected_sha256": conflict_digest,
                    "strict_atomic": True,
                }
            )
        self.assertFalse(self.context.set_calls)
        self.assertEqual(self.context.names, old_names)
        self.assertEqual(self.module._symbol_alias_overlay, old_overlay)

    def test_strict_success_commits_once_and_legacy_default_remains_best_effort(self):
        path, digest = self._write(
            [self._alias(1, "NewAliasOne"), self._alias(2, "NewAliasTwo")]
        )
        result = self.module._load_symbol_aliases(
            {
                "path": str(path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
            }
        )
        self.assertTrue(result["strictAtomic"])
        self.assertFalse(result["physicalAtomic"])
        self.assertEqual(result["atomicity"], "complete-or-recovery-required")
        self.assertTrue(result["rollbackAvailable"])
        self.assertFalse(result["recoveryRequired"])
        self.assertEqual(result["captureSessionId"], "b" * 32)
        self.assertEqual(len(result["transactionId"]), 32)
        self.assertTrue(result["committed"])
        self.assertEqual(result["counts"]["skipped"], 0)
        self.assertEqual(self.context.names[1], (True, "NewAliasOne"))
        self.assertEqual(self.context.names[2], (True, "NewAliasTwo"))
        self.assertEqual(len(self.module._symbol_alias_overlay["aliases"]), 2)

        rollback = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(rollback["status"], "restored")
        self.assertTrue(rollback["strictReceipt"])
        self.assertFalse(rollback["physicalAtomic"])
        self.assertEqual(self.context.names[1], (True, "OldAliasOne"))
        self.assertEqual(self.context.names[2], (True, "CapturedTwo"))

        legacy_path, legacy_digest = self._write(
            [self._alias(1, "LegacyAlias"), self._alias(2, "Ignored", verified=False)]
        )
        legacy = self.module._load_symbol_aliases(
            {
                "path": str(legacy_path),
                "confirmed_only": True,
                "expected_sha256": legacy_digest,
            }
        )
        self.assertFalse(legacy["strictAtomic"])
        self.assertTrue(legacy["committed"])
        self.assertEqual(legacy["counts"]["skipped"], 1)
        self.assertEqual(self.context.names[1], (True, "LegacyAlias"))

    def test_operation_token_queries_lost_commit_and_binds_strict_rollback(self):
        path, digest = self._write([self._alias(1, "NewAliasOne")])
        operation_id = "d" * 32
        result = self.module._load_symbol_aliases(
            {
                "path": str(path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
                "operation_id": operation_id,
            }
        )
        self.assertEqual(result["operationId"], operation_id)
        self.assertEqual(result["transactionId"], operation_id)

        status = self.module._get_symbol_alias_transaction(
            {
                "expected_capture_id": self.capture_id,
                "expected_capture_session_id": "b" * 32,
                "transaction_id": operation_id,
            }
        )
        self.assertEqual(status["state"], "committed")
        self.assertTrue(status["mutationPresent"])
        self.assertTrue(status["recoveryRequired"])
        self.assertEqual(status["manifestSha256"], digest)

        with self.assertRaisesRegex(RuntimeError, "another transaction"):
            self.module._rollback_symbol_aliases(
                {
                    "expected_capture_id": self.capture_id,
                    "expected_transaction_id": "e" * 32,
                }
            )
        self.assertIsNotNone(self.module._symbol_alias_recovery_ledger)

        receipt = self.module._rollback_symbol_aliases(
            {
                "expected_capture_id": self.capture_id,
                "expected_transaction_id": operation_id,
            }
        )
        self.assertEqual(receipt["transactionId"], operation_id)
        self.assertEqual(receipt["status"], "restored")
        after = self.module._get_symbol_alias_transaction(
            {
                "expected_capture_id": self.capture_id,
                "expected_capture_session_id": "b" * 32,
                "transaction_id": operation_id,
            }
        )
        self.assertEqual(after["state"], "rolled-back")
        self.assertFalse(after["mutationPresent"])

    def test_forward_and_immediate_rollback_failure_retains_retryable_ledger(self):
        path, digest = self._write(
            [self._alias(1, "NewAliasOne"), self._alias(2, "NewAliasTwo")]
        )
        old_overlay = self.module._symbol_alias_overlay
        old_renames = copy.deepcopy(self.module._symbol_resource_renames)
        # Resource 2 fails during forward. Resource 1 then fails during the
        # immediate full rollback, leaving a physical partial rename.
        self.context.fail_on_calls = {(2, 1), (1, 2)}

        with self.assertRaisesRegex(RuntimeError, "recovery remains required"):
            self.module._load_symbol_aliases(
                {
                    "path": str(path),
                    "confirmed_only": True,
                    "expected_sha256": digest,
                    "strict_atomic": True,
                }
            )

        ledger = self.module._symbol_alias_recovery_ledger
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["state"], "pending")
        self.assertIs(self.module._symbol_alias_overlay, old_overlay)
        self.assertEqual(self.module._symbol_resource_renames, old_renames)
        self.assertEqual(self.context.names[1], (True, "NewAliasOne"))

        receipt = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(receipt["status"], "restored")
        self.assertEqual(receipt["resourceCount"], 2)
        self.assertIsNone(self.module._symbol_alias_recovery_ledger)
        self.assertEqual(self.context.names[1], (True, "OldAliasOne"))
        self.assertEqual(self.context.names[2], (True, "CapturedTwo"))

    def test_ledger_is_published_before_first_physical_rename(self):
        path, digest = self._write([self._alias(1, "NewAliasOne")])
        observed_ledgers = []
        self.context.before_set = lambda unused_id, unused_name: observed_ledgers.append((
            self.module._symbol_alias_recovery_ledger is not None,
            self.module._symbol_alias_recovery_ledger.get("state"),
        ))

        self.module._load_symbol_aliases(
            {
                "path": str(path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
            }
        )

        self.assertTrue(observed_ledgers)
        self.assertTrue(all(published for published, unused_state in observed_ledgers))
        self.assertEqual(observed_ledgers[0][1], "pending")

    def test_cross_capture_rollback_is_rejected_without_clearing_ledger(self):
        path, digest = self._write([self._alias(1, "NewAliasOne")])
        self.module._load_symbol_aliases(
            {
                "path": str(path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
            }
        )
        ledger = self.module._symbol_alias_recovery_ledger
        self.module._capture_session_id = "c" * 32

        with self.assertRaisesRegex(RuntimeError, "another capture session"):
            self.module._rollback_symbol_aliases(
                {"expected_capture_id": self.capture_id}
            )
        self.assertIs(self.module._symbol_alias_recovery_ledger, ledger)

        self.module._capture_session_id = "b" * 32
        self.context.capture_path = "D:/captures/other.rdc"
        with self.assertRaisesRegex(RuntimeError, "capture token"):
            self.module._rollback_symbol_aliases(
                {"expected_capture_id": self.capture_id}
            )
        self.assertIs(self.module._symbol_alias_recovery_ledger, ledger)

        self.context.capture_path = "D:/captures/current.rdc"
        receipt = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(receipt["status"], "restored")
        retry = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(retry["status"], "already-restored")

    def test_explicit_rollback_failure_retains_ledger_for_retry(self):
        path, digest = self._write([self._alias(1, "NewAliasOne")])
        self.module._load_symbol_aliases(
            {
                "path": str(path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
            }
        )
        ledger = self.module._symbol_alias_recovery_ledger
        committed_overlay = self.module._symbol_alias_overlay
        self.context.fail_on_calls.add((1, 2))

        with self.assertRaisesRegex(RuntimeError, "recovery remains required"):
            self.module._rollback_symbol_aliases(
                {"expected_capture_id": self.capture_id}
            )
        self.assertIs(self.module._symbol_alias_recovery_ledger, ledger)
        self.assertIs(self.module._symbol_alias_overlay, committed_overlay)

        receipt = self.module._rollback_symbol_aliases(
            {"expected_capture_id": self.capture_id}
        )
        self.assertEqual(receipt["status"], "restored")
        self.assertEqual(self.context.names[1], (True, "OldAliasOne"))
        self.assertIsNot(self.module._symbol_alias_overlay, committed_overlay)


if __name__ == "__main__":
    unittest.main()
