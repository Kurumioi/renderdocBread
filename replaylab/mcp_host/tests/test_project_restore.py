from __future__ import annotations

import copy
import contextlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from src import project_restore
from src.rdc_project_index import new_index, upsert_project_binding
from src.rdc_shader_inventory_cache import (
    build_shader_inventory_cache_document,
    load_shader_inventory_cache,
)


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self, *unused_args, **unused_kwargs):
        return lambda function: function

    def run(self, **unused_kwargs):
        pass


def _load_server():
    try:
        return importlib.import_module("src.server")
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
    mcp_package = types.ModuleType("mcp")
    mcp_package.__path__ = []
    mcp_server = types.ModuleType("mcp.server")
    mcp_server.__path__ = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    with mock.patch.dict(
        sys.modules,
        {
            "mcp": mcp_package,
            "mcp.server": mcp_server,
            "mcp.server.fastmcp": fastmcp,
        },
    ):
        return importlib.import_module("src.server")


server = _load_server()


class _ActiveService:
    def __init__(self, status: dict):
        self._status = status
        self.aliases_directory = (
            Path(str(status["projectRoot"])).resolve() / "shared" / "aliases"
        )
        self.aliases_directory.mkdir(parents=True, exist_ok=True)
        self.pin_depth = 0
        self.blocked_switches = 0

    def status(self) -> dict:
        return copy.deepcopy(self._status)

    def shared_dictionary_directory(self, kind: str) -> Path:
        if kind != "aliases":
            raise AssertionError("unexpected shared dictionary kind")
        return self.aliases_directory

    @contextlib.contextmanager
    def pin_active_project(self, workspace_id: str, project_root: Path):
        if (
            workspace_id != self._status["workspaceId"]
            or Path(project_root).resolve()
            != Path(self._status["projectRoot"]).resolve()
        ):
            raise RuntimeError("active project changed before the operation")
        self.pin_depth += 1
        try:
            yield
        finally:
            self.pin_depth -= 1
            if (
                workspace_id != self._status["workspaceId"]
                or Path(project_root).resolve()
                != Path(self._status["projectRoot"]).resolve()
            ):
                raise RuntimeError("active project changed during the operation")


class _WorkspaceService:
    def __init__(self, status: dict):
        self._status = status
        self.status_calls = 0
        self.import_calls = 0
        self.import_handler = None
        self.extra_statuses: list[dict] = []

    def status(self, workspace_path: str) -> dict:
        self.status_calls += 1
        for status in [self._status, *self.extra_statuses]:
            if Path(workspace_path).resolve() == Path(status["workspacePath"]).resolve():
                return copy.deepcopy(status)
        raise AssertionError("unexpected workspace path")

    def import_project_candidate(self, workspace_path: str, candidate_plan: dict) -> dict:
        self.import_calls += 1
        if self.import_handler is None:
            raise AssertionError("compiled project candidate was not configured")
        return self.import_handler(workspace_path, candidate_plan)


class _ReplayService:
    def __init__(self, state: dict):
        self.state = state
        self.snapshot_calls = 0
        self.inventory_snapshot_calls = 0
        self.reuse_plan_calls = 0
        self.reuse_plan_response: dict | None = None
        self.materialize_calls = 0
        self.materialize_response: dict | None = None
        self.inventory_snapshot_handler = None
        self.inventory_seed_handler = None
        self.inventory_register_handler = None
        self.inventory_register_calls = 0
        self.last_inventory_materialization = None

    def restorable_shader_raw_hashes(self):
        return sorted({item["rawSha256"] for item in self.state["workspaces"].values()})

    def restore_knowledge_snapshot(self, capture_id: str, catalog_sha256: str) -> dict:
        self.snapshot_calls += 1
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected catalogSha256")
        return copy.deepcopy(self.state)

    def reuse_plan(self, capture_id: str, catalog_sha256: str) -> dict:
        self.reuse_plan_calls += 1
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected catalogSha256")
        if self.reuse_plan_response is not None:
            return copy.deepcopy(self.reuse_plan_response)
        result = {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": self.state["projectId"],
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "matchedShaderCount": 0,
            "matches": [],
        }
        return result

    def reuse_snapshot_for_shader_inventory(self, inventory: dict) -> dict:
        if self.inventory_snapshot_handler is None:
            raise AssertionError("Shader inventory snapshot was not configured")
        self.inventory_snapshot_calls += 1
        return self.inventory_snapshot_handler(inventory, None)

    def validate_shader_inventory_snapshot(
        self, inventory: dict, binding: dict
    ) -> dict:
        if self.inventory_snapshot_handler is None:
            raise AssertionError("Shader inventory snapshot was not configured")
        return self.inventory_snapshot_handler(inventory, binding)

    def seed_workspace_from_inventory(
        self,
        knowledge_id: str,
        destination_status: dict,
        target_shader: dict,
        inventory: dict,
        expected_binding: dict,
    ) -> dict:
        if self.inventory_seed_handler is None:
            raise AssertionError("Shader inventory source seed was not configured")
        return self.inventory_seed_handler(
            knowledge_id,
            destination_status,
            target_shader,
            inventory,
            expected_binding,
        )

    def register_workspace_from_inventory(
        self,
        status: dict,
        target_shader: dict,
        inventory: dict,
        expected_binding: dict,
    ) -> dict:
        self.inventory_register_calls += 1
        if self.inventory_register_handler is None:
            raise AssertionError("Shader inventory publication was not configured")
        return self.inventory_register_handler(
            status, target_shader, inventory, expected_binding
        )

    def materialize_alias_candidates(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        unused_shader: dict,
        unused_candidates: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected alias captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected alias catalogSha256")
        if self.materialize_response is None:
            raise AssertionError("alias materialization was not configured")
        return copy.deepcopy(self.materialize_response)

    def materialize_alias_batch(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        targets: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected alias captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected alias catalogSha256")
        if self.materialize_response is None or len(targets) != 1:
            raise AssertionError("alias batch materialization was not configured")
        materialized = copy.deepcopy(self.materialize_response)
        return {
            "schema": "renderdoc-ultra-project-alias-materialization-batch/v1",
            "schemaVersion": 1,
            "projectId": project_id,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "targetCount": 1,
            "candidateCount": len(targets[0]["candidates"]),
            "aliasCount": materialized["aliasCount"],
            "materializations": [materialized],
            "displayAliasesOnly": True,
            "applied": False,
            "projectChangedDuringBatch": False,
        }

    def materialize_alias_batch_from_inventory(
        self,
        project_id: str,
        inventory: dict,
        expected_binding: dict,
        targets: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if self.materialize_response is None or len(targets) != 1:
            raise AssertionError("inventory alias batch was not configured")
        capture_id = expected_binding["captureId"]
        catalog_sha256 = expected_binding["catalogSha256"]
        materialized = copy.deepcopy(self.materialize_response)
        materialized["captureId"] = capture_id
        materialized["catalogSha256"] = catalog_sha256
        materialized["inventoryOnly"] = True
        materialized["aliasTable"]["captureId"] = capture_id
        materialized["aliasTable"]["catalogSha256"] = catalog_sha256
        for alias in materialized["aliasTable"]["aliases"]:
            alias["captureId"] = capture_id
        encoded = (
            json.dumps(
                materialized["aliasTable"],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        materialized["jsonBytes"] = encoded
        materialized["sha256"] = project_restore.hashlib.sha256(encoded).hexdigest()
        result = {
            "schema": "renderdoc-ultra-project-alias-materialization-batch/v1",
            "schemaVersion": 1,
            "projectId": project_id,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "targetCount": 1,
            "candidateCount": len(targets[0]["candidates"]),
            "aliasCount": materialized["aliasCount"],
            "materializations": [materialized],
            "displayAliasesOnly": True,
            "inventoryOnly": True,
            "applied": False,
            "projectChangedDuringBatch": False,
        }
        self.last_inventory_materialization = copy.deepcopy(result)
        return result


class _IPC:
    def __init__(self, capture_path: Path, session_id: str, index: dict | None):
        self.capture_path = capture_path
        self.session_id = session_id
        self.index = index
        self.calls: list[tuple[str, dict]] = []
        self.write_error: Exception | None = None
        self.bind_error: Exception | None = None
        self.resource_index_snapshot: dict | None = None
        self.inventory_assets: list[dict] = []
        self.inventory_unindexed_resource_ids: list[int] = []
        self.inventory_result_mutator = None
        self.expected_result_mutator = None
        self.alias_load_error: Exception | None = None
        self.alias_load_error_after_commit: Exception | None = None
        self.alias_response_mutator = None
        self.alias_transaction: dict | None = None
        self.alias_last_rollback: dict | None = None
        self.alias_rollback_calls = 0
        self.shader_operations: dict[str, dict] = {}
        self.shader_operation_recovery_calls = 0
        self.shared_replacements: list[dict] = []
        self.section_version = 1
        self.begin_reports_running = False
        self.poll_errors: list[Exception] = []

    def _begin_resource_index_snapshot(self) -> dict:
        if self.resource_index_snapshot is None:
            raise AssertionError("resource-index job was not created")
        completed = copy.deepcopy(self.resource_index_snapshot)
        if not self.begin_reports_running:
            return completed
        self.begin_reports_running = False
        running = copy.deepcopy(completed)
        running["status"] = "running"
        running.pop("result", None)
        return running

    def call(self, method: str, params: dict | None = None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params))
        if method == "read_rdc_project_index":
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": self.section_version,
                "present": self.index is not None,
                "encodedSize": 1,
                "encodedSha256": "a" * 64,
                "document": copy.deepcopy(self.index),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        if method == "begin_rdc_project_shader_resource_index":
            if self.bind_error is not None:
                raise self.bind_error
            api = project_restore._asset_api(params["expected_api"])
            inventory_only = params.get("inventory_all_shaders") is True
            if inventory_only:
                assets = copy.deepcopy(self.inventory_assets)
                targets = params.get("target_raw_sha256s")
                filtered = []
                if targets is not None:
                    filtered = sorted({asset["resourceId"] for asset in assets if asset["rawSha256"] not in targets})
                    assets = [asset for asset in assets if asset["rawSha256"] in targets]
                inventory_material = {
                    "api": api,
                    "shaders": [
                        {
                            "resourceId": asset["resourceId"],
                            "stableKeySha256": asset["stableKeySha256"],
                            "anchorEventId": asset["anchorEventId"],
                            "constantBlocks": asset["constantBlocks"],
                        }
                        for asset in assets
                    ],
                }
                resource_inventory_sha = project_restore._canonical_sha256(
                    inventory_material
                )
                stable_resource_set_sha = project_restore._canonical_sha256(
                    sorted({asset["stableKeySha256"] for asset in assets})
                )
                live_identity_sha = project_restore._canonical_sha256(
                    {
                        "api": api,
                        "shaders": [
                            {
                                "resourceId": asset["resourceId"],
                                "stableKeySha256": asset["stableKeySha256"],
                                "stableKeyComplete": asset["stableKeyComplete"],
                                "anchorEventId": asset["anchorEventId"],
                            }
                            for asset in assets
                        ],
                    }
                )
                result = {
                    "schema": "replaylab-project-shader-resource-index/v2",
                    "schemaVersion": 2,
                    "capturePath": str(self.capture_path),
                    "captureId": params["expected_capture_id"],
                    "captureSessionId": self.session_id,
                    "identityEvidenceSha256": params[
                        "expected_identity_evidence_sha256"
                    ],
                    "requestSha256": "c" * 64,
                    "inventoryOnly": True,
                    "inventoryComplete": True,
                    "leaseEligible": False,
                    "matched": False,
                    "capability": {
                        "supported": True,
                        "api": api,
                        "eventTraversalSupported": False,
                        "fullFrameTraversalSupported": False,
                        "anchorPreflightRequired": True,
                    },
                    "assets": assets,
                    "rawBytecodeBlobs": [],
                    "uniqueShaderResourceCount": (
                        len({asset["resourceId"] for asset in assets}) +
                        len(self.inventory_unindexed_resource_ids)
                    ),
                    "processedShaderResourceCount": (
                        len({asset["resourceId"] for asset in assets}) +
                        len(self.inventory_unindexed_resource_ids)
                    ),
                    "indexedShaderResourceCount": len(
                        {asset["resourceId"] for asset in assets}
                    ),
                    "unindexedShaderResourceCount": len(
                        self.inventory_unindexed_resource_ids
                    ),
                    "unindexedShaderResourceIds": copy.deepcopy(
                        self.inventory_unindexed_resource_ids
                    ),
                    "matchedShaderCount": len(assets),
                    "representativeEidCount": len(assets),
                    "symbolSnapshotPublished": True,
                    "symbolSnapshotShaderCount": len(assets),
                    "symbolSnapshotUseCount": len(assets),
                    "symbolSnapshotCaptureId": stable_resource_set_sha,
                    "resourceInventorySha256": resource_inventory_sha,
                    "stableResourceSetSha256": stable_resource_set_sha,
                    "liveIdentitySha256": live_identity_sha,
                    "eventTraversalCount": 0,
                    "fullFrameTraversal": False,
                    "gpuReplayMutation": False,
                    "physicalApplyAuthority": False,
                    "anchorPerShaderMaximum": 1,
                    "resourceOwnershipScope": "target-capture-session",
                    "crossCaptureResourceIdReuse": False,
                }
                if targets is not None:
                    observed = sorted({asset["rawSha256"] for asset in assets})
                    result.update(inventoryScope="target-raw-sha256", inventoryComplete=False,
                                  rawHashPurpose="captured-executable-identity", semanticEquivalenceVerified=False,
                                  targetRawSha256s=targets, scanComplete=True,
                                  targetInventoryComplete=not self.inventory_unindexed_resource_ids,
                                  rawIdentitySearchComplete=not self.inventory_unindexed_resource_ids,
                                  rawIdentityUnavailableShaderResourceIds=list(self.inventory_unindexed_resource_ids),
                                  filteredOutShaderResourceCount=len(filtered), filteredOutShaderResourceIds=filtered,
                                  targetMatchedShaderResourceIds=sorted({asset["resourceId"] for asset in assets}),
                                  observedTargetRawSha256s=observed, unobservedTargetRawSha256s=sorted(set(targets) - set(observed)),
                                  symbolSnapshotPublished=False, symbolSnapshotShaderCount=0,
                                  symbolSnapshotUseCount=0, symbolSnapshotCaptureId="")
                    result["uniqueShaderResourceCount"] += len(filtered)
                    result["processedShaderResourceCount"] += len(filtered)
                if self.inventory_result_mutator is not None:
                    self.inventory_result_mutator(result)
                self.resource_index_snapshot = {
                    "schema": "replaylab-project-shader-resource-index-job/v1",
                    "schemaVersion": 1,
                    "jobId": "d" * 32,
                    "status": "completed",
                    "captureSessionId": self.session_id,
                    "requestSha256": "c" * 64,
                    "idempotent": False,
                    "committed": False,
                    "result": result,
                }
                return self._begin_resource_index_snapshot()
            assets = []
            for shader in params["expected_shaders"]:
                asset = copy.deepcopy(shader)
                asset.update(
                    {
                        "ownerResourceId": shader["resourceId"],
                        "ownerCaptureSessionId": self.session_id,
                        "resourceOwnershipScope": "target-capture-session",
                        "crossCaptureResourceIdReuse": False,
                        "api": api,
                        "physicalAbiSha256": (
                            "" if api == "vulkan" else "f" * 64
                        ),
                        "physicalAbiValidated": api != "vulkan",
                        "requiresAnchorPreflight": True,
                        "constantBlocksMatched": True,
                        "interfaceMatched": True,
                        "contentMatchMode": (
                            "module-sha256-owner-rid-anchor-preflight"
                            if api == "vulkan"
                            else "stable-key-owner-rid"
                        ),
                    }
                )
                assets.append(asset)
            result = {
                "schema": "replaylab-project-shader-resource-index/v2",
                "schemaVersion": 2,
                "capturePath": str(self.capture_path),
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "identityEvidenceSha256": params[
                    "expected_identity_evidence_sha256"
                ],
                "matched": True,
                "reasonId": "",
                "requestSha256": "c" * 64,
                "inventoryOnly": False,
                "inventoryComplete": False,
                "leaseEligible": True,
                "capability": {
                    "supported": True,
                    "api": api,
                    "eventTraversalSupported": False,
                    "fullFrameTraversalSupported": False,
                    "physicalAbiFromNullPipeline": api != "vulkan",
                    "anchorPreflightRequired": True,
                },
                "assets": assets,
                "matchedShaderCount": len(assets),
                "expectedShaderCount": len(assets),
                "liveIdentitySha256": "e" * 64,
                "eventTraversalCount": 0,
                "fullFrameTraversal": False,
                "gpuReplayMutation": False,
                "physicalApplyAuthority": False,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
            }
            if self.expected_result_mutator is not None:
                self.expected_result_mutator(result)
            self.resource_index_snapshot = {
                "schema": "replaylab-project-shader-resource-index-job/v1",
                "schemaVersion": 1,
                "jobId": "d" * 32,
                "status": "completed",
                "captureSessionId": self.session_id,
                "requestSha256": "c" * 64,
                "idempotent": False,
                "committed": False,
                "result": result,
            }
            return self._begin_resource_index_snapshot()
        if method == "poll_rdc_project_shader_resource_index":
            if self.resource_index_snapshot is None:
                raise AssertionError("resource-index job was not started")
            if self.poll_errors:
                raise self.poll_errors.pop(0)
            return copy.deepcopy(self.resource_index_snapshot)
        if method == "commit_rdc_project_shader_resource_index":
            return {
                "schema": "replaylab-project-shader-resource-index-lease/v1",
                "schemaVersion": 1,
                "jobId": params["job_id"],
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "identityEvidenceSha256": params[
                    "expected_identity_evidence_sha256"
                ],
                "liveIdentitySha256": params["live_identity_sha256"],
                "committed": True,
                "idempotent": False,
                "eventTraversalCount": 0,
                "gpuReplayMutation": False,
                "physicalApplyAuthority": False,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
            }
        if method == "get_shared_shader_replacements":
            return {
                "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "replacements": copy.deepcopy(self.shared_replacements),
                "count": len(self.shared_replacements),
                "mutationEpoch": len(self.shared_replacements),
                "originalShadersPreserved": True,
                "replayMutation": False,
            }
        if method == "write_rdc_project_index":
            if self.write_error is not None:
                raise self.write_error
            self.index = copy.deepcopy(params["document"])
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": 1,
                "present": True,
                "written": True,
                "physicalApplyAuthority": False,
                "document": copy.deepcopy(params["document"]),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        if method == "load_symbol_aliases":
            if self.alias_load_error is not None:
                raise self.alias_load_error
            encoded = Path(params["path"]).read_bytes()
            if project_restore.hashlib.sha256(encoded).hexdigest() != params[
                "expected_sha256"
            ]:
                raise AssertionError("alias digest mismatch")
            table = json.loads(encoded.decode("utf-8"))
            aliases = table["aliases"]
            operation_id = params["operation_id"]
            self.alias_transaction = {
                "transactionId": operation_id,
                "captureId": table["captureId"],
                "captureSessionId": self.session_id,
                "manifestSha256": params["expected_sha256"],
                "state": "committed",
            }
            response = {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "captureId": table["captureId"],
                "captureSessionId": self.session_id,
                "operationId": operation_id,
                "transactionId": operation_id,
                "confirmedOnly": params["confirmed_only"],
                "strictAtomic": params["strict_atomic"],
                "committed": True,
                "recoveryRequired": False,
                "rollbackAvailable": True,
                "recoveryLedgerPublished": True,
                "counts": {
                    "manifestAliases": len(aliases),
                    "activeValidatedAliases": len(aliases),
                    "skipped": 0,
                    "conflicts": 0,
                },
            }
            if self.alias_load_error_after_commit is not None:
                raise self.alias_load_error_after_commit
            if self.alias_response_mutator is not None:
                self.alias_response_mutator(response)
            return response
        if method == "get_symbol_alias_transaction":
            transaction = self.alias_transaction
            if (
                transaction is not None
                and transaction["transactionId"] == params["transaction_id"]
            ):
                return {
                    "schema": "renderdoc-ultra-alias-transaction-status/v1",
                    "schemaVersion": 1,
                    "captureId": transaction["captureId"],
                    "captureSessionId": transaction["captureSessionId"],
                    "transactionId": transaction["transactionId"],
                    "state": transaction["state"],
                    "committed": transaction["state"] == "committed",
                    "mutationPresent": True,
                    "recoveryRequired": True,
                    "rollbackAvailable": True,
                    "manifestSha256": transaction["manifestSha256"],
                    "replayMutation": False,
                }
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": params["expected_capture_session_id"],
                "transactionId": "",
                "state": "absent",
                "committed": False,
                "mutationPresent": False,
                "recoveryRequired": False,
                "rollbackAvailable": False,
                "manifestSha256": "",
                "replayMutation": False,
            }
        if method == "rollback_symbol_aliases":
            self.alias_rollback_calls += 1
            transaction_id = params["expected_transaction_id"]
            if (
                self.alias_transaction is not None
                and self.alias_transaction["transactionId"] != transaction_id
            ):
                raise AssertionError("alias rollback transaction mismatch")
            status = (
                "restored" if self.alias_transaction is not None else "already-restored"
            )
            self.alias_transaction = None
            receipt = {
                "schema": "renderdoc-ultra-alias-rollback/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "transactionId": transaction_id,
                "status": status,
                "restored": True,
                "alreadyRestored": status == "already-restored",
                "verified": True,
                "strictReceipt": True,
                "captureTokenVerified": True,
                "originalNamesVerified": True,
                "physicalAtomic": False,
                "recoveryRequired": False,
                "overlayRestored": True,
                "renameLedgerRestored": True,
                "resourceCount": 1,
                "failures": [],
            }
            self.alias_last_rollback = copy.deepcopy(receipt)
            return receipt
        if method == "recover_shader_candidate_shared_resource_operation":
            self.shader_operation_recovery_calls += 1
            operation_id = params["operation_id"]
            outcome = self.shader_operations.get(operation_id)
            if outcome is None:
                status = "absent"
                replacement_id = ""
                known = False
                created = False
                rollback = None
            else:
                status = "recovered"
                replacement_id = outcome["replacementId"]
                known = True
                created = True
                rollback = {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": replacement_id,
                    "captureId": params["expected_capture_id"],
                    "captureSessionId": params["expected_capture_session_id"],
                    "workspaceId": params["workspace_id"],
                    "resourceScoped": True,
                    "eventScoped": False,
                    "cleared": True,
                    "originalShaderPreserved": True,
                    "targetFreed": True,
                }
                self.shader_operations.pop(operation_id, None)
            return {
                "schema": "renderdoc-ultra-shared-shader-operation-recovery/v1",
                "schemaVersion": 1,
                "operationId": operation_id,
                "captureId": params["expected_capture_id"],
                "captureSessionId": params["expected_capture_session_id"],
                "workspaceId": params["workspace_id"],
                "status": status,
                "operationKnown": known,
                "operationCreatedReplacement": created,
                "replacementId": replacement_id,
                "mutationPresent": False,
                "recovered": True,
                "originalShaderPreserved": True,
                "strictReceipt": True,
                "rollback": rollback,
            }
        raise AssertionError("unexpected IPC method: %s" % method)


class _Host:
    def __init__(
        self,
        active: dict,
        workspace_status: dict,
        capture_path: Path,
        session_id: str,
        index: dict,
        identity_evidence: dict,
    ):
        self.active_service = _ActiveService(active)
        self.workspace_service = _WorkspaceService(workspace_status)
        self.ipc = _IPC(capture_path, session_id, index)
        self.ipc.instance_id = "test-instance"
        self.capture_path = capture_path
        self.session_id = session_id
        self.identity_evidence = identity_evidence
        self.scope_calls = 0
        self.projection_calls = 0
        self.apply_calls = 0
        self.prepare_calls = 0
        self.workspace_prepare_calls = 0
        self.workspace_rebind_calls = 0
        self.seed_calls = 0
        self.compile_calls = 0
        self.prepared_response: dict | None = None
        self.prepare_progress_events: list[dict] = []
        self.target_workspace: Path | None = None
        self.target_status: dict | None = None
        self.material_session: dict | None = None
        self.material_prepare_calls = 0
        self.material_update_calls = 0
        self.material_update_error: Exception | None = None
        self.material_rollback_calls = 0
        self.compile_error: Exception | None = None
        self.switch_project_on_compile = False
        self.switch_project_on_apply = False
        self.shader_rollback_calls = 0
        self.shader_rollback_error: Exception | None = None
        self.live_context_calls = 0
        self.switch_project_after_material_success = False
        self.switch_on_live_context_call: int | None = None
        self.require_alias_before_apply = False
        self.apply_response_mutator = None
        self.apply_error_after_commit: Exception | None = None
        self.apply_error_workspace_ids: set[str] = set()
        self.last_apply_response: dict | None = None
        self.edit_start_calls = []
        self.edit_start_error = None

    def _active_project_service(self):
        return self.active_service

    def _shader_workspace_service(self):
        return self.workspace_service

    def start_shader_workspace_live_edit(self, workspace_path):
        if self.active_service.pin_depth:
            raise AssertionError("editor start must run after the project pin is released")
        self.edit_start_calls.append(workspace_path)
        if self.edit_start_error:
            raise self.edit_start_error
        status = self._status_for_path(workspace_path)
        return json.dumps({
            "schema": "replaylab-shader-live-edit-mcp/v1", "schemaVersion": 1,
            "operation": "start", "appliedByThisCall": False,
            "backgroundAutoApplyAuthorized": True,
            "result": {"schema": "replaylab-shader-live-edit/v1", "state": "running",
                "workspacePath": workspace_path, "liveEditId": "a" * 32, "watchId": "b" * 32,
                "captureBinding": {"instanceId": "test-instance", "captureSessionId": self.session_id,
                                   "capturePath": str(self.capture_path)},
                "watch": {"state": "running", "caughtUp": True, "observedSourceSha256": status["source"]["currentSha256"]},
                "latestHandledRevision": status["latestCompiled"]["revision"],
                "latestHandledCandidateRawSha256": status["latestCompiled"]["rawSha256"]}})

    def list_shader_workspace_live_edits(self):
        return json.dumps({"schema": "replaylab-shader-live-edit-mcp/v1", "schemaVersion": 1,
            "operation": "list", "appliedByThisCall": False, "backgroundAutoApplyAuthorized": True,
            "result": {"sessions": []}})

    def _active_live_context(self, allow_missing_capture_id=False):
        self.live_context_calls += 1
        if self.live_context_calls == self.switch_on_live_context_call:
            self.active_service._status["workspaceId"] = "4" * 32
        return {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "path": str(self.capture_path),
                "captureSessionId": self.session_id,
                "captureId": "",
                "api": "D3D11",
            },
        }

    def _require_same_active_project(self, active):
        if active["workspaceId"] != self.active_service._status["workspaceId"]:
            raise AssertionError("active project changed")
        return copy.deepcopy(active)

    def _workspace_catalog_binding_projection(self, status):
        self.projection_calls += 1
        return {
            "workspaceId": status["workspaceId"],
            "captureId": status["capture"]["captureId"],
            "capturePath": status["capture"]["capturePath"],
            "api": status["capture"]["api"],
            "catalogPath": status["capture"]["catalogPath"],
            "catalogSha256": status["capture"]["catalogSha256"],
            "shaderKey": status["shader"]["shaderKey"],
            "stage": status["stage"],
            "entryPoint": status["entryPoint"],
            "resourceId": status["shader"]["resourceId"],
            "rawSha256": status["shader"]["rawSha256"],
        }

    def _workspace_catalog_rebind_scope(self, status):
        self.scope_calls += 1
        return {"identityEvidence": copy.deepcopy(self.identity_evidence)}

    def _canonical_json_sha256(self, value, label):
        return project_restore._canonical_sha256(value)

    def apply_shader_workspace_shared_resource(
        self,
        workspace_path,
        expected_revision,
        expected_candidate_raw_sha256,
        operation_id,
        expected_anchor_event_id=None,
    ):
        if self.require_alias_before_apply and not any(
            method == "load_symbol_aliases" for method, unused in self.ipc.calls
        ):
            raise AssertionError("Shader apply ran before the alias overlay was restored")
        self.apply_calls += 1
        status = self._status_for_path(workspace_path)
        if expected_anchor_event_id != 9:
            raise AssertionError(
                "restore did not pass its frozen representative EID to apply"
            )
        if status["workspaceId"] in self.apply_error_workspace_ids:
            raise RuntimeError("selected cached Shader apply failed")
        candidate_abi = getattr(self, "candidate_abi_sha256", status["baseline"]["abiSha256"])
        report_path = "revisions/%04d/abi-report.json" % expected_revision
        status["latestCompiled"]["abiGate"] = {
            "status": "compatible", "baselineAbiSha256": status["baseline"]["abiSha256"],
            "candidateAbiSha256": candidate_abi, "requestId": "1" * 64,
            "reportSha256": "3" * 64, "reportPath": report_path,
        }
        response = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "operationId": operation_id,
            "binding": {
                "workspaceId": status["workspaceId"],
                "captureId": status["capture"]["captureId"],
                "revision": expected_revision,
                "candidateRawSha256": expected_candidate_raw_sha256,
                "candidateByteSize": status["latestCompiled"]["byteSize"],
                "baselineAbiSha256": status["baseline"]["abiSha256"],
                "stage": status["stage"],
                "entryPoint": status["entryPoint"],
                "api": "d3d11",
                "encoding": "dxbc",
                "targetProfile": status["targetProfile"]["profile"],
            },
            "validation": {
                "workspacePath": str(Path(workspace_path).resolve()),
                "revision": expected_revision,
                "requestId": "1" * 64,
                "compatible": True,
                "baselineAbiSha256": status["baseline"]["abiSha256"],
                "candidateAbiSha256": candidate_abi,
                "differences": [],
                "differencesTruncated": False,
                "requiresReplayAbiValidation": False,
                "replayAbiValidated": True,
                "abiReportSha256": "3" * 64,
                "abiReportPath": str(Path(workspace_path).resolve() / report_path),
                "applied": False,
            },
            "replacement": {
                "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                "schemaVersion": 1,
                "captureId": status["capture"]["captureId"],
                "replacementId": "9" * 32,
                "operationId": operation_id,
                "operationCreatedReplacement": True,
                "workspaceId": status["workspaceId"],
                "captureSessionId": self.session_id,
                "revision": expected_revision,
                "anchorEventId": expected_anchor_event_id,
                "stage": status["stage"],
                "entryPoint": status["entryPoint"],
                "api": "d3d11",
                "encoding": "dxbc",
                "originalResourceId": status["shader"]["resourceId"],
                "originalRawSha256": status.get("baseline", {}).get(
                    "rawSha256", status["shader"]["rawSha256"]
                ),
                "candidateRawSha256": expected_candidate_raw_sha256,
                "physicalAbiSha256": status["baseline"]["abiSha256"],
                "abiRequestId": "1" * 64,
                "abiReportSha256": "3" * 64,
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "configured": True,
                "originalShaderPreserved": True,
                "singleOwner": True,
            },
            "scope": "captured-resource",
            "applied": True,
            "rollback": {
                "tool": "rollback_shader_workspace_shared_resource",
                "workspacePath": str(Path(workspace_path).resolve()),
                "replacementId": "9" * 32,
            },
        }
        self.ipc.shader_operations[operation_id] = {
            "replacementId": "9" * 32,
            "workspaceId": status["workspaceId"],
        }
        if self.apply_error_after_commit is not None:
            raise self.apply_error_after_commit
        if self.apply_response_mutator is not None:
            self.apply_response_mutator(response)
        self.last_apply_response = copy.deepcopy(response)
        if self.switch_project_on_apply:
            self.active_service._status["workspaceId"] = "4" * 32
        return json.dumps(response)

    def _status_for_path(self, workspace_path):
        if (
            self.target_workspace is not None
            and Path(workspace_path).resolve() == self.target_workspace.resolve()
            and self.target_status is not None
        ):
            return self.target_status
        if Path(workspace_path).resolve() == Path(
            self.workspace_service._status["workspacePath"]
        ).resolve():
            return self.workspace_service._status
        for status in self.workspace_service.extra_statuses:
            if Path(workspace_path).resolve() == Path(status["workspacePath"]).resolve():
                return status
        raise AssertionError("unexpected workspace path")

    def _prepare_active_capture_project(self, unused_progress_token=None):
        self.prepare_calls += 1
        if self.prepared_response is None:
            return json.dumps({"error": "fallback deliberately stopped by test"})
        return json.dumps(copy.deepcopy(self.prepared_response))

    def _prepare_active_capture_project_with_progress(
        self, progress_token, callback
    ):
        if len(progress_token) != 32:
            raise AssertionError("unexpected restore progress token")
        for event in self.prepare_progress_events:
            callback(copy.deepcopy(event))
        return self._prepare_active_capture_project(progress_token)

    def prepare_active_capture_project(self):
        return self._prepare_active_capture_project()

    def get_active_shader_workspace_path(self, event_id, stage, **expected):
        if self.target_workspace is None or self.target_status is None:
            raise AssertionError("reuse target was not configured")
        if expected != {
            "expected_resource_id": self.target_status["shader"]["resourceId"],
            "expected_entry_point": self.target_status["entryPoint"],
            "expected_raw_sha256": self.target_status["shader"]["rawSha256"],
            "expected_capture_session_id": self.session_id,
        }:
            raise AssertionError("restore omitted its complete frozen Shader identity")
        return json.dumps(
            {
                "schema": "replaylab-active-shader-workspace-path/v1",
                "schemaVersion": 1,
                "captureId": self.target_status["capture"]["captureId"],
                "captureSessionId": self.session_id,
                "eventId": event_id,
                "stage": stage,
                "entryPoint": self.target_status["entryPoint"],
                "shaderResourceId": self.target_status["shader"]["resourceId"],
                "rawSha256": self.target_status["shader"]["rawSha256"],
                "catalogSha256": self.target_status["capture"]["catalogSha256"],
                "workspacePath": str(self.target_workspace),
                "applied": False,
            }
        )

    def prepare_shader_workspace(
        self,
        event_id,
        stage,
        workspace_path,
        *,
        expected_resource_id=None,
        expected_entry_point="",
        expected_raw_sha256="",
        expected_capture_id="",
        expected_capture_session_id="",
        admit_existing=False,
    ):
        if self.target_workspace is None or Path(workspace_path).resolve() != self.target_workspace:
            raise AssertionError("unexpected reuse workspace")
        self.workspace_prepare_calls += 1
        existed = self.target_workspace.exists()
        if admit_existing:
            self.workspace_rebind_calls += 1
        selected_keys = [
            shader_key
            for selected_event_id, shader_key in self.identity_evidence["useTuples"]
            if selected_event_id == event_id
        ]
        status = self.target_status
        if (
            existed is not admit_existing
            or status is None
            or selected_keys != [status["shader"]["shaderKey"]]
            or stage != status["stage"]
            or expected_resource_id != status["shader"]["resourceId"]
            or expected_entry_point != status["entryPoint"]
            or expected_raw_sha256 != status["shader"]["rawSha256"]
            or expected_capture_id != status["capture"]["captureId"]
            or expected_capture_session_id != self.session_id
        ):
            return json.dumps(
                {"error": "workspace does not match the frozen representative EID"}
            )
        if not existed:
            self.target_workspace.mkdir(parents=True, exist_ok=False)
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "prepare",
                "result": {
                    "workspacePath": str(self.target_workspace),
                    "binding": {
                        "workspacePath": str(self.target_workspace),
                        "workspaceId": status["workspaceId"],
                        "captureId": status["capture"]["captureId"],
                        "captureSessionId": self.session_id,
                        "eventId": event_id,
                        "stage": status["stage"],
                        "entryPoint": status["entryPoint"],
                        "originalResourceId": status["shader"]["resourceId"],
                        "originalRawSha256": status["shader"]["rawSha256"],
                    },
                    "created": {
                        "admittedExisting": existed,
                        "sourcePreserved": existed,
                    },
                },
            }
        )

    def get_shader_workspace_status(self, workspace_path):
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "status",
                "result": copy.deepcopy(self._status_for_path(workspace_path)),
            }
        )

    def seed_active_shader_workspace(self, knowledge_id, workspace_path):
        status = self._status_for_path(workspace_path)
        self.seed_calls += 1
        status["source"].update(
            {
                "currentSha256": "0" * 64,
                "modifiedSinceDecompile": True,
            }
        )
        return json.dumps(
            {
                "schema": "replaylab-active-workspace-seed/v1",
                "schemaVersion": 1,
                "captureId": status["capture"]["captureId"],
                "captureSessionId": self.session_id,
                "workspacePath": str(Path(workspace_path).resolve()),
                "knowledgeId": knowledge_id,
                "applied": False,
            }
        )

    def compile_shader_workspace(self, workspace_path):
        status = self._status_for_path(workspace_path)
        self.compile_calls += 1
        if self.compile_error is not None:
            raise self.compile_error
        status["revision"] = getattr(self, "compile_revision", 1)
        status["latestCompiled"] = {
            "revision": status["revision"],
            "sourceSha256": status["source"]["currentSha256"],
            "rawSha256": "f" * 64,
            "byteSize": 4,
            "kind": "DXBC",
        }
        if self.switch_project_on_compile:
            if self.active_service.pin_depth:
                self.active_service.blocked_switches += 1
            else:
                self.active_service._status["workspaceId"] = "4" * 32
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "compile",
                "result": copy.deepcopy(status),
            }
        )

    def prepare_material_session(self, *unused_args):
        self.material_prepare_calls += 1
        if self.material_session is None:
            raise AssertionError("material reuse was not configured")
        return json.dumps(copy.deepcopy(self.material_session))

    def update_material_parameters(self, session_id, updates):
        if self.material_session is None or session_id != self.material_session["sessionId"]:
            raise AssertionError("unexpected material session")
        self.material_update_calls += 1
        if self.material_update_error is not None:
            raise self.material_update_error
        result = copy.deepcopy(self.material_session)
        result.update(
            {
                "operation": "update-many",
                "updatedParameterIds": [item["parameter"] for item in updates],
                "updatedParameterCount": len(updates),
                "applied": True,
            }
        )
        if self.switch_project_after_material_success:
            # _restore_material performs one immediate context validation after
            # this update. Switch on the following caller-level validation so
            # the successful session enters the global rollback ledger first.
            self.switch_on_live_context_call = self.live_context_calls + 2
        return json.dumps(result)

    def rollback_material_session(self, session_id):
        self.material_rollback_calls += 1
        session = self.material_session or {
            "captureId": "3" * 64,
            "captureSessionId": self.session_id,
        }
        return json.dumps(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "schemaVersion": 1,
                "operation": "rollback",
                "sessionId": session_id,
                "captureId": session["captureId"],
                "captureSessionId": session["captureSessionId"],
                "applied": False,
                "snapshotReleased": True,
            }
        )

    def rollback_shader_workspace_shared_resource(
        self, workspace_path, replacement_id
    ):
        self.shader_rollback_calls += 1
        if self.shader_rollback_error is not None:
            raise self.shader_rollback_error
        status = self._status_for_path(workspace_path)
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                "operation": "rollback-shared-resource",
                "workspaceId": status["workspaceId"],
                "captureId": status["capture"]["captureId"],
                "replacement": {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": replacement_id,
                    "captureId": status["capture"]["captureId"],
                    "captureSessionId": self.session_id,
                    "workspaceId": status["workspaceId"],
                    "resourceScoped": True,
                    "eventScoped": False,
                    "cleared": True,
                    "originalShaderPreserved": True,
                    "targetFreed": True,
                },
                "applied": False,
            }
        )


class ProjectRestoreTests(unittest.TestCase):
    def test_product_target_lookup_batches_only_project_original_hashes(self):
        targets = sorted({self.raw_sha, *("%064x" % index for index in range(1, 18))})
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=targets):
            result, host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(result["completed"], result)
        begins = [params for method, params in host.ipc.calls if method == "begin_rdc_project_shader_resource_index"]
        self.assertEqual(len(begins), 2)
        self.assertEqual([digest for params in begins for digest in params["target_raw_sha256s"]], targets)
        self.assertTrue(all(0 < len(params["target_raw_sha256s"]) <= 16 for params in begins))
        self.assertEqual(result["targetLookup"]["observedTargetRawSha256s"], [self.raw_sha])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["fullFrame"])

    def test_product_target_no_match_is_bounded_and_does_not_prepare_source(self):
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=["0" * 64]):
            result, host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["cache"]["reason"], "project-targets-not-present")
        self.assertEqual(result["targetLookup"]["unobservedTargetRawSha256s"], ["0" * 64])
        self.assertFalse(result["fullFrame"])
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 0)

    def test_committed_restore_starts_current_editor_after_releasing_project_pin(self):
        result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["editing"]["enabled"], 1, result["editing"])
        self.assertEqual(host.edit_start_calls, [str(self.workspace)])
        self.assertEqual(result["editing"]["items"][0]["captureBinding"]["captureSessionId"], self.session_id)

    def test_restored_editor_polls_starting_on_one_bound_gui_even_if_global_selection_changes(self):
        from src.ipc_client import _CALL_BINDING
        original_start = _Host.start_shader_workspace_live_edit
        polls = []
        def start(host, workspace):
            self.assertEqual(_CALL_BINDING.get(), ("test-instance", self.session_id))
            response = json.loads(original_start(host, workspace))
            host.ready_editor_response = copy.deepcopy(response)
            host.ipc.instance_id = "other-active-gui"
            response["result"]["state"] = "starting"
            response["result"]["watch"]["state"] = "starting"
            return json.dumps(response)
        def poll(host, live_id):
            polls.append((_CALL_BINDING.get(), live_id))
            response = copy.deepcopy(host.ready_editor_response)
            response["operation"] = "poll"
            return json.dumps(response)
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", start), mock.patch.object(
                _Host, "poll_shader_workspace_live_edit", poll, create=True):
            result, unused_host, unused_replay, unused_progress = self._run()
        self.assertEqual(result["editing"]["enabled"], 1, result["editing"])
        self.assertEqual(polls, [(("test-instance", self.session_id), "a" * 32)])
        self.assertIsNone(_CALL_BINDING.get())

    def test_wrong_candidate_gui_is_rejected_inside_binding_before_editor_start(self):
        from src.ipc_client import _CALL_BINDING
        result, host, unused_replay, unused_progress = self._run()
        host.edit_start_calls.clear()
        host.ipc.instance_id = "wrong-gui"
        original_context = host._active_live_context
        def context(**kwargs):
            value = original_context(**kwargs)
            if _CALL_BINDING.get() == ("wrong-gui", self.session_id):
                value["capture"]["captureSessionId"] = "e" * 32
            return value
        host._active_live_context = context
        editing = project_restore._enable_restored_shader_editing(
            host, self.active, self.project, self.capture_path, self.session_id,
            self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["failed"], 1)
        self.assertEqual(host.edit_start_calls, [])

    def test_owner_resumed_editor_before_restore_is_reused_without_new_grant_or_revision_gate(self):
        from src.ipc_client import _CALL_BINDING
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        # The owner resumed before restore recompiled/applied the same source.
        # Its watcher cursor remains older; it is not evidence for that apply.
        resumed["result"]["latestHandledRevision"] = 0
        resumed["result"]["latestHandledCandidateRawSha256"] = "1" * 64
        host.edit_start_calls.clear()
        listing = json.loads(host.list_shader_workspace_live_edits())
        listing["result"]["sessions"] = [resumed["result"]]
        polls = []
        def poll(live_id):
            polls.append((_CALL_BINDING.get(), live_id))
            value = copy.deepcopy(resumed); value["operation"] = "poll"
            return json.dumps(value)
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", poll, create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit") as start, \
                mock.patch.object(project_restore, "_retire_restored_editor_job") as retire:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["enabled"], 1, editing)
        self.assertEqual(polls, [(("test-instance", self.session_id), "a" * 32)])
        start.assert_not_called(); retire.assert_not_called()

    def test_existing_editor_that_fails_poll_is_not_retired_or_reauthorized(self):
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        listing = json.loads(host.list_shader_workspace_live_edits())
        listing["result"]["sessions"] = [resumed["result"]]
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", side_effect=RuntimeError("owner unavailable"), create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit") as start, \
                mock.patch.object(project_restore, "_retire_restored_editor_job") as retire:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["failed"], 1)
        start.assert_not_called(); retire.assert_not_called()

    def test_owner_resume_between_list_and_start_is_reused_with_one_start_attempt(self):
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        empty = json.loads(host.list_shader_workspace_live_edits())
        listing = copy.deepcopy(empty); listing["result"]["sessions"] = [resumed["result"]]
        resumed["operation"] = "poll"
        with mock.patch.object(host, "list_shader_workspace_live_edits", side_effect=[json.dumps(empty), json.dumps(listing)]), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", return_value=json.dumps(resumed), create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit", side_effect=RuntimeError("already active")) as start:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["enabled"], 1, editing)
        self.assertEqual(start.call_count, 1)

    def test_exact_editor_retirement_preserves_durable_consent_and_other_scope(self):
        from src import internal_tool_service
        binding = {"instanceId": "test-instance", "captureSessionId": self.session_id,
                   "capturePath": str(self.capture_path)}
        stopped = []
        job = types.SimpleNamespace(workspace=self.workspace, session=types.SimpleNamespace(binding=binding),
                                    stop=lambda:stopped.append("a" * 32))
        key = project_restore._path_key(self.workspace)
        manager = types.SimpleNamespace(_lifecycle_lock=threading.RLock(), _lock=threading.RLock(),
            _jobs={"a" * 32:job}, _active_by_workspace={key:"a" * 32}, _registry=mock.Mock())
        with mock.patch.object(internal_tool_service, "_SHADER_LIVE_EDIT_MANAGER", manager):
            wrong = dict(binding, captureSessionId="e" * 32)
            self.assertIn("not confirmed", project_restore._retire_restored_editor_job("a" * 32, self.workspace, wrong))
            self.assertEqual(stopped, [])
            self.assertIn("retired", project_restore._retire_restored_editor_job("a" * 32, self.workspace, binding))
        self.assertEqual(stopped, ["a" * 32])
        self.assertEqual(manager._active_by_workspace, {})
        manager._registry.revoke.assert_not_called()

    def test_post_start_failed_watch_retires_only_returned_owned_job(self):
        original_start = _Host.start_shader_workspace_live_edit
        def start(host, workspace):
            response = json.loads(original_start(host, workspace))
            response["result"]["watch"]["state"] = "failed"
            return json.dumps(response)
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", start), mock.patch.object(
                project_restore, "_retire_restored_editor_job", return_value="retired") as retire:
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["failed"], 1)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(retire.call_args.args[0], "a" * 32)

    def test_product_restore_results_pass_the_real_embedded_client_contract(self):
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("product_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        for target_mode in (False, True):
            with self.subTest(target_mode=target_mode):
                result, unused_host, unused_replay, unused_progress = self._run(
                    index_present=not target_mode, auto_prepare=target_mode)
                accepted = client.ReplayLabRestoreHostClient._result(
                    result, str(self.project), str(self.capture_path), self.session_id)
                self.assertEqual(accepted["editing"]["enabled"], 1, accepted["editing"])
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=["0" * 64]):
            result, unused_host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)["completed"])

    def test_editor_failure_preserves_committed_replacement_and_reports_separate_failure(self):
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", side_effect=RuntimeError("editor unavailable")):
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(result["editing"]["failed"], 1)
        self.assertIn("editor unavailable", result["editing"]["items"][0]["error"])

    def test_editor_start_rejects_changed_source_and_capture_without_reusing_old_grant(self):
        result, host, unused_replay, unused_progress = self._run()
        applied = result["shaders"]["items"]
        host.edit_start_calls.clear()
        for mutation in ("source", "resource", "capture", "cancel"):
            with self.subTest(mutation=mutation):
                host.edit_start_calls.clear()
                original = copy.deepcopy(host.workspace_service._status)
                event = threading.Event()
                if mutation == "source":
                    host.workspace_service._status["source"]["currentSha256"] = "9" * 64
                elif mutation == "resource":
                    host.workspace_service._status["shader"]["resourceId"] += 1
                elif mutation == "capture":
                    host.session_id = "e" * 32
                else:
                    event.set()
                editing = project_restore._enable_restored_shader_editing(
                    host, self.active, self.project, self.capture_path, self.session_id,
                    self.binding["shaders"], applied, cancel_event=event)
                self.assertEqual(editing["failed"], 1)
                self.assertEqual(editing["enabled"], 0)
                self.assertEqual(host.edit_start_calls, [])
                host.workspace_service._status = original
                host.session_id = self.session_id

    def setUp(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.replay_path = self.project / ".replaylab" / "replay-project"
        self.shader_root = self.project / "workspaces" / "shaders"
        self.workspace = self.shader_root / "cached"
        self.workspace.mkdir(parents=True)
        self.replay_path.mkdir(parents=True)
        self.capture_path = self.root / "frame.rdc"
        self.capture_path.write_bytes(b"rdc")
        self.catalog_path = self.project / "captures" / "catalog.json"
        self.catalog_path.parent.mkdir(parents=True)

        self.project_id = "1" * 32
        self.active_workspace_id = "2" * 32
        self.capture_id = "3" * 64
        self.knowledge_sha = "5" * 64
        self.scope_sha = "6" * 64
        self.matcher_sha = "7" * 64
        self.managed_workspace_id = "8" * 32
        self.knowledge_id = "a" * 32
        self.raw_sha = "b" * 64
        self.code_sha = "c" * 64
        self.candidate_sha = "f" * 64
        self.live_abi_sha = "f" * 64
        self.source_sha = "0" * 64
        self.session_id = "9" * 32
        constant_block = {
            "index": 0,
            "name": "PerMaterial",
            "bind": 0,
            "space": 0,
            "bindArraySize": 1,
            "byteSize": 16,
            "bufferBacked": True,
            "inlineDataBytes": False,
            "compileConstants": False,
            "fields": [],
            "layoutCompleteness": "opaque",
        }
        shader_catalog_record = {
            "shaderResourceId": 7,
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "encoding": "ShaderEncoding.DXBC",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceComplete": True,
            "inputSignature": [],
            "outputSignature": [],
            "readOnlyResources": [],
            "readWriteResources": [],
            "samplers": [],
            "constantBlocks": [constant_block],
            "threadGroupSize": [],
            "outputTopology": "",
            "dynamicLinkageInterfaceCount": 0,
        }
        self.interface_sha = project_restore.interface_hash(shader_catalog_record)
        self.layout_sha = project_restore.layout_hash(shader_catalog_record)
        shader_catalog_record["interfaceHash"] = self.interface_sha
        shader_catalog_record["layoutHash"] = self.layout_sha
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "api": "GraphicsAPI.D3D11",
            "capturePath": str(self.capture_path),
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9],
                "stages": ["fragment"],
            },
            "shaders": [shader_catalog_record],
            "uses": [
                {
                    "shaderResourceId": 7,
                    "resourceId": 7,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "shaderKey": "7:fragment:main:%s" % self.raw_sha,
                    "eventId": 9,
                }
            ],
        }
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.shader_key = "7:fragment:main:%s" % self.raw_sha
        self.stable_key_sha = project_restore._canonical_sha256(
            {
                "api": "d3d11",
                "encoding": "dxbc",
                "rawSha256": self.raw_sha,
                "stage": "fragment",
                "entryPoint": "main",
                "abiHash": self.live_abi_sha,
                "layoutHash": self.layout_sha,
            }
        )
        self.target_profile = {
            "kind": "DXBC",
            "stage": "fragment",
            "profile": "ps_5_0",
        }
        self.identity = {
            "api": "d3d11",
            "encoding": "dxbc",
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "abiHash": self.live_abi_sha,
            "layoutHash": self.layout_sha,
            "stableKeyComplete": True,
            "stableKeySha256": self.stable_key_sha,
            "anchorEventId": 9,
        }
        self.shader_record = {
            "identity": copy.deepcopy(self.identity),
            "knowledgeId": self.knowledge_id,
            "managedWorkspace": {
                "workspaceId": self.managed_workspace_id,
                "path": self.workspace.relative_to(self.project).as_posix(),
            },
            "candidateRevision": 3,
            "candidateRawSha256": self.candidate_sha,
            "sourceSha256": self.source_sha,
            "artifactRefs": {
                "projectArtifactId": self.knowledge_id,
                "revision": 3,
                "compiledSha256": self.candidate_sha,
                "compileConfigSha256": project_restore._canonical_sha256(
                    self.target_profile
                ),
                "representationsSha256": "",
                "aliasesSha256": "",
            },
        }
        self.binding = {
            "projectId": self.project_id,
            "workspaceId": self.active_workspace_id,
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "shaders": [copy.deepcopy(self.shader_record)],
            "materials": [],
        }
        self.index = None
        self.active = {
            "workspaceId": self.active_workspace_id,
            "scopeConfigured": True,
            "projectRoot": str(self.project),
            "paths": {
                "replayProject": str(self.replay_path),
                "shaderWorkspaces": str(self.shader_root),
            },
            "replayProject": {
                "projectId": self.project_id,
                "projectPath": str(self.replay_path),
            },
        }
        self.workspace_status = {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(self.workspace),
            "workspaceId": self.managed_workspace_id,
            "capture": {
                "captureId": self.capture_id,
                "capturePath": str(self.capture_path),
                "api": "D3D11",
                "catalogPath": str(self.catalog_path),
                "catalogSha256": self.catalog_sha,
            },
            "shader": {
                "shaderKey": self.shader_key,
                "resourceId": 7,
                "rawSha256": self.raw_sha,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "targetProfile": copy.deepcopy(self.target_profile),
            "revision": 3,
            "source": {
                "present": True,
                "currentSha256": self.source_sha,
            },
            "latestCompiled": {
                "revision": 3,
                "sourceSha256": self.source_sha,
                "rawSha256": self.candidate_sha,
                "byteSize": 4,
                "kind": "DXBC",
            },
            "baseline": {
                "rawSha256": self.raw_sha,
                "kind": "DXBC",
                "abiSha256": self.live_abi_sha,
            },
        }
        self.state = {
            "projectId": self.project_id,
            "projectPath": str(self.replay_path),
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "capture": {"shaders": [copy.deepcopy(self.identity)]},
            "workspaces": {
                self.knowledge_id: {
                    "knowledgeId": self.knowledge_id,
                    "sourceSha256": self.source_sha,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "stableKeyComplete": True,
                    "stableKeySha256": self.stable_key_sha,
                }
            },
            "materials": {},
        }
        self.identity_evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.shader_key],
            "useTuples": [[9, self.shader_key]],
        }
        self.material_layout_sha = project_restore._catalog_block_projection(
            constant_block, 0, "fragment"
        )["layoutSha256"]
        self._refresh_exact_binding([self.shader_record])

    def _refresh_exact_binding(self, shader_records, material_records=None):
        records = [copy.deepcopy(item) for item in shader_records]
        materials = [copy.deepcopy(item) for item in (material_records or [])]
        receipts = [
            project_restore._realized_receipt_from_record(
                item,
                item["identity"]["abiHash"],
                abi_report_sha256="3" * 64,
            )
            for item in records
        ]
        self.binding = project_restore._binding_document(
            self.active,
            self.state,
            records,
            materials,
            receipts,
        )
        self.index = upsert_project_binding(new_index(), self.binding)

    def _live_replacement_record(self, **updates):
        replacement = {
            "replacementId": "e" * 32,
            "operationId": "d" * 32,
            "workspaceId": self.managed_workspace_id,
            "revision": 3,
            "anchorEventId": 9,
            "stage": "fragment",
            "entryPoint": "main",
            "api": "d3d11",
            "encoding": "dxbc",
            "originalResourceId": 7,
            "originalRawSha256": self.raw_sha,
            "candidateRawSha256": self.candidate_sha,
            "physicalAbiSha256": self.live_abi_sha,
            "resourceScoped": True,
            "eventScoped": False,
            "allOriginalResourceUsesAffected": True,
            "originalShaderPreserved": True,
        }
        replacement.update(updates)
        return replacement

    @staticmethod
    def _reseal_index_document(document):
        clean = copy.deepcopy(document)
        clean["sealSha256"] = project_restore._canonical_sha256(
            {key: value for key, value in clean.items() if key != "sealSha256"}
        )
        return clean

    def _shader_inventory_sidecar_document(self) -> dict:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        source_block = catalog["shaders"][0]["constantBlocks"][0]
        block = {
            **project_restore._catalog_block_projection(
                source_block, 0, "fragment"
            ),
            "name": str(source_block.get("name", "") or ""),
            "fields": [],
        }
        material = {
            "api": "d3d11",
            "encoding": "dxbc",
            "rawSha256": self.raw_sha,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": self.live_abi_sha,
            "layoutHash": self.layout_sha,
        }
        return build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha,
            self.capture_path,
            {
                "assets": [
                    {
                        **self.identity,
                        "rawSize": 4,
                        "interfaceHash": self.interface_sha,
                        "interfaceComplete": True,
                        "interfaceEvidence": "null-pipeline-reflection",
                        "physicalAbiSha256": self.live_abi_sha,
                        "physicalAbiValidated": True,
                        "stableKeyMaterial": material,
                        "identityAmbiguous": False,
                        "formatSupported": True,
                        "anchorSource": "resource-usage-index",
                        "constantBlocks": [block],
                    }
                ],
                "coverage": {"inventoryComplete": True},
            },
        )

    def tearDown(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp.cleanup()

    def _configure_reuse_plan(
        self,
        host,
        *,
        with_material=False,
        with_binary=False,
        binary_import_error=None,
        binary_import_source_change=None,
        preexisting_binary_profile_mismatch=False,
        preexisting_binary_compiler_mismatch=False,
        precreate_target_workspace=False,
    ):
        target_workspace = self.shader_root / "restored"
        target_status = copy.deepcopy(self.workspace_status)
        target_status.update(
            {
                "workspacePath": str(target_workspace),
                "workspaceId": "d" * 32,
                "revision": 0,
                "latestCompiled": None,
                "baseline": {
                    "rawSha256": self.raw_sha,
                    "kind": "DXBC",
                    "abiSha256": self.live_abi_sha,
                },
            }
        )
        target_status["source"] = {
            "present": True,
            "currentSha256": "e" * 64,
            "modifiedSinceDecompile": False,
            "lane": "executable-editable",
            "format": "hlsl",
        }
        host.target_workspace = target_workspace
        host.target_status = target_status
        if precreate_target_workspace:
            target_workspace.mkdir(parents=True, exist_ok=False)
        match = {
            "shader": copy.deepcopy(self.identity),
            "workspaceCandidates": [
                {
                    "knowledgeId": self.knowledge_id,
                    "matchMode": "raw-exact",
                    "sourceSha256": self.source_sha,
                    "sourceSeedEligible": True,
                    "activeCandidate": True,
                    "stableKeyComplete": True,
                    "stableKeySha256": self.stable_key_sha,
                }
            ],
            "aliasCandidates": [],
            "materialCandidates": [],
        }
        if with_binary:
            binary_path = self.replay_path / "artifacts" / ("f" * 64 + ".dxbc")
            match["workspaceCandidates"][0].update(
                {
                    "binaryCandidateEligible": True,
                    "binaryCandidate": {
                        "artifactPath": str(binary_path),
                        "sha256": "f" * 64,
                        "codeSha256": "c" * 64,
                        "byteSize": 4,
                        "kind": "dxbc",
                        "sourceSha256": self.source_sha,
                        "sourceRevision": 1,
                        "targetProfile": copy.deepcopy(
                            target_status["targetProfile"]
                        ),
                        "compilerBindingSha256": "d" * 64,
                        "candidateOnly": True,
                        "contentAddressed": True,
                        "integrityVerified": True,
                    },
                }
            )

            def project_import_receipt(source_sha256):
                return {
                    "artifactSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "sourceSha256": source_sha256,
                    "sourceRevision": 1,
                    "targetProfile": copy.deepcopy(target_status["targetProfile"]),
                    "compilerBindingSha256": "d" * 64,
                    "originValidationEvidenceOnly": True,
                    "freshCaptureRebindRequired": True,
                    "freshLiveAbiRevalidationRequired": True,
                    "applyAuthorization": False,
                    "oldReplacementIdReused": False,
                    "oldCaptureAbiAuthorityReused": False,
                }

            def import_candidate(workspace_path, unused_candidate):
                if binary_import_source_change is not None:
                    target_status["source"][
                        "currentSha256"
                    ] = binary_import_source_change
                if binary_import_error is not None:
                    raise binary_import_error
                project_import = project_import_receipt(
                    target_status["source"]["currentSha256"]
                )
                target_status["revision"] = 1
                target_status["latestCompiled"] = {
                    "revision": 1,
                    "sourceSha256": target_status["source"]["currentSha256"],
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": project_import,
                }
                return {
                    "workspacePath": workspace_path,
                    "revision": 1,
                    "sourceSha256": target_status["source"]["currentSha256"],
                    "candidateBlobPath": str(binary_path),
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "kind": "DXBC",
                    "projectImport": project_import,
                    "compilerSkipped": True,
                    "compiledFromProjectArtifact": True,
                    "requiresReplayAbiValidation": True,
                    "replayAbiValidated": False,
                    "applied": False,
                }

            host.workspace_service.import_handler = import_candidate
            if preexisting_binary_profile_mismatch:
                stale_receipt = project_import_receipt(self.source_sha)
                stale_receipt["targetProfile"]["profile"] = "ps_4_0"
                target_status["revision"] = 2
                target_status["latestCompiled"] = {
                    "revision": 2,
                    "sourceSha256": self.source_sha,
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": stale_receipt,
                }
            elif preexisting_binary_compiler_mismatch:
                stale_receipt = project_import_receipt(self.source_sha)
                stale_receipt["compilerBindingSha256"] = "e" * 64
                target_status["revision"] = 2
                target_status["latestCompiled"] = {
                    "revision": 2,
                    "sourceSha256": self.source_sha,
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": stale_receipt,
                }
        if with_material:
            material_knowledge_id = "6" * 32
            saved_parameter_id = "7" * 32
            live_parameter_id = "8" * 32
            self.state["materials"][material_knowledge_id] = {
                "knowledgeId": material_knowledge_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.raw_sha,
                "codeSha256": self.code_sha,
                "interfaceComplete": True,
                "interfaceHash": self.interface_sha,
                "layoutHash": self.layout_sha,
                "constantBlockPath": "PerMaterial",
                "constantBlockLayoutSha256": self.material_layout_sha,
                "parameters": [
                    {
                        "parameterId": saved_parameter_id,
                        "path": "PerMaterial.Roughness",
                        "baseType": "Float",
                        "components": 1,
                        "value": 0.35,
                    }
                ],
            }
            match["materialCandidates"] = [
                {
                    "knowledgeId": material_knowledge_id,
                    "matchMode": "raw-exact",
                    "parameterCount": 1,
                    "constantBlockPath": "PerMaterial",
                    "constantBlockLayoutSha256": self.material_layout_sha,
                    "activeCandidate": True,
                }
            ]
            host.material_session = {
                "schema": "renderdoc-ultra-material-session/v1",
                "schemaVersion": 1,
                "sessionId": "5" * 32,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "eventId": 9,
                "stage": "fragment",
                "shaderResourceId": 7,
                "constantBlockIndex": 0,
                "constantBlockPath": "PerMaterial",
                "layoutSha256": self.material_layout_sha,
                "parameters": [
                    {
                        "parameterId": live_parameter_id,
                        "path": "PerMaterial.Roughness",
                        "baseType": "Float",
                        "components": 1,
                    }
                ],
            }
        plan = {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": self.project_id,
            "projectPath": str(self.replay_path),
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "matchedShaderCount": 1,
            "matches": [match],
            "policy": {
                "rawExactOrCodeInterfaceLayoutExactOnly": True,
                "oldWorkspaceManifestReused": False,
                "oldCaptureAbiReportReused": False,
                "oldMaterialSessionReused": False,
                "liveAbiRevalidationRequiredBeforeShaderApply": True,
                "liveMaterialLayoutRevalidationRequired": True,
            },
        }
        host.prepared_response = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "project": copy.deepcopy(self.active),
            "captureExport": {
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "fullFrame": True,
            },
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogPath": str(self.catalog_path),
                "catalogSha256": self.catalog_sha,
            },
            "reusePlan": plan,
            "applied": False,
        }

    def _configure_alias_reuse(self, host, replay, *, prepared=False):
        alias_candidate = {
            "knowledgeId": "4" * 32,
            "groupIndex": 0,
            "matchMode": "raw-exact",
            "activeCandidate": True,
        }
        if prepared:
            plan = host.prepared_response["reusePlan"]
            plan["matches"][0]["aliasCandidates"] = [alias_candidate]
        else:
            plan = {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "matchedShaderCount": 1,
                "matches": [
                    {
                        "shader": copy.deepcopy(self.identity),
                        "workspaceCandidates": [],
                        "aliasCandidates": [alias_candidate],
                        "materialCandidates": [],
                    }
                ],
            }
            replay.reuse_plan_response = plan
        alias = {
            "captureId": self.capture_id,
            "shaderResourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "bindingKind": "shader",
            "registerSpace": 0,
            "bindNumber": 0,
            "byteOffset": 0,
            "canonicalPath": "",
            "alias": "MainSurfacePS",
            "originalName": "",
            "provenance": "program",
            "confidence": 1.0,
            "verified": True,
            "metadata": {"programVerified": True},
        }
        alias_table = {
            "schema": "renderdoc-ultra-aliases/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "captureCatalog": str(self.catalog_path),
            "verifiedOnly": True,
            "aliases": [alias],
            "shaders": [
                {
                    "shaderResourceId": 7,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "codeSha256": self.code_sha,
                    "aliases": [alias],
                    "verified": True,
                }
            ],
            "symbolRules": [],
        }
        encoded = (
            json.dumps(alias_table, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        replay.materialize_response = {
            "schema": "renderdoc-ultra-project-alias-materialization/v1",
            "schemaVersion": 1,
            "projectId": self.project_id,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "shader": copy.deepcopy(self.identity),
            "aliasCount": 1,
            "aliasTable": alias_table,
            "jsonBytes": encoded,
            "sha256": project_restore.hashlib.sha256(encoded).hexdigest(),
            "displayAliasesOnly": True,
            "applied": False,
        }

    def _add_second_cached_shader(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        second_catalog_shader = copy.deepcopy(catalog["shaders"][0])
        second_catalog_shader.update({"shaderResourceId": 8, "resourceId": 8})
        catalog["shaders"].append(second_catalog_shader)
        second_shader_key = "8:fragment:main:%s" % self.raw_sha
        second_use = copy.deepcopy(catalog["uses"][0])
        second_use.update(
            {
                "shaderResourceId": 8,
                "resourceId": 8,
                "shaderKey": second_shader_key,
                "eventId": 10,
            }
        )
        catalog["uses"].append(second_use)
        catalog["selection"]["eventIds"] = [9, 10]
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.workspace_status["capture"]["catalogSha256"] = self.catalog_sha
        self.state["catalogSha256"] = self.catalog_sha
        second_identity = copy.deepcopy(self.identity)
        second_identity.update({"resourceId": 8, "anchorEventId": 10})
        second_knowledge_id = "b" * 32
        self.state["capture"]["shaders"].append(copy.deepcopy(second_identity))
        self.state["workspaces"][second_knowledge_id] = {
            "knowledgeId": second_knowledge_id,
            "sourceSha256": self.source_sha,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "stableKeyComplete": True,
            "stableKeySha256": self.stable_key_sha,
        }
        second_workspace = self.shader_root / "cached-second"
        second_workspace.mkdir()
        second_status = copy.deepcopy(self.workspace_status)
        second_status.update(
            {
                "workspacePath": str(second_workspace),
                "workspaceId": "e" * 32,
            }
        )
        second_status["shader"].update(
            {"shaderKey": second_shader_key, "resourceId": 8}
        )
        second_record = copy.deepcopy(self.shader_record)
        second_record.update(
            {
                "identity": second_identity,
                "knowledgeId": second_knowledge_id,
                "managedWorkspace": {
                    "workspaceId": "e" * 32,
                    "path": second_workspace.relative_to(self.project).as_posix(),
                },
                "artifactRefs": {
                    **second_record["artifactRefs"],
                    "projectArtifactId": second_knowledge_id,
                },
            }
        )
        self._refresh_exact_binding(
            [*self.binding["shaders"], second_record],
            self.binding["materials"],
        )
        self.identity_evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9, 10],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.shader_key, second_shader_key],
            "useTuples": [[9, self.shader_key], [10, second_shader_key]],
        }
        return second_status

    def _configure_inventory_replay(self, replay, host, *, index_present):
        # Exact RDC bindings now validate project knowledge before any live
        # inventory exists. The first inventory snapshot is therefore always
        # the miss/stale fallback for the current resource set.
        cache_snapshot_pending = False
        last_snapshot: dict | None = None

        def snapshot(inventory, expected_binding):
            nonlocal cache_snapshot_pending, last_snapshot
            if expected_binding is not None:
                if (
                    last_snapshot is None
                    or last_snapshot["inventory"] != inventory
                    or last_snapshot["binding"] != expected_binding
                ):
                    raise AssertionError("Shader inventory binding changed")
                return copy.deepcopy(last_snapshot)

            cache_lookup = cache_snapshot_pending
            cache_snapshot_pending = False
            project_capture = inventory["capture"]
            knowledge = {"workspaces": {}, "aliases": [], "materials": {}}
            for knowledge_id, record in self.state["workspaces"].items():
                targets = [
                    shader
                    for shader in inventory["shaders"]
                    if shader["stage"] == record["stage"]
                    and shader.get("entryPoint", "")
                    == record.get("entryPoint", "")
                    and shader["rawSha256"] == record["rawSha256"]
                    and shader["stableKeySha256"]
                    == record["stableKeySha256"]
                ]
                if not targets:
                    continue
                target = targets[0]
                knowledge["workspaces"][knowledge_id] = {
                    "physicalIdentity": {
                        field: target[field]
                        for field in (
                            "api",
                            "encoding",
                            "rawSha256",
                            "stage",
                            "entryPoint",
                            "abiHash",
                            "layoutHash",
                            "stableKeySha256",
                        )
                    },
                    "interfaceComplete": target["interfaceComplete"],
                    "source": {
                        "kind": "verified-hlsl",
                        "knowledgeId": knowledge_id,
                        "artifactPath": str(
                            self.replay_path / "artifacts" / (knowledge_id + ".hlsl")
                        ),
                        "sha256": record["sourceSha256"],
                        "byteSize": 4,
                    },
                    "targetProfile": copy.deepcopy(
                        self.workspace_status["targetProfile"]
                    ),
                    "compiledCandidate": {
                        "rawSha256": self.candidate_sha,
                    },
                    "compilerBinding": None,
                    "assemblyDisassemblyArtifacts": [],
                }

            if cache_lookup:
                capture_id = self.state["captureId"]
                catalog_sha256 = self.state["catalogSha256"]
            else:
                capture_id = project_capture["captureId"]
                catalog_sha256 = project_capture["inventorySha256"]
            knowledge_ids = {
                "workspaces": sorted(knowledge["workspaces"]),
                "aliases": [],
                "materials": [],
            }
            binding = {
                "schema": "renderdoc-ultra-project-shader-inventory-binding/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "scopeSha256": self.scope_sha,
                "captureId": capture_id,
                "catalogSha256": catalog_sha256,
                "inventorySha256": project_capture["inventorySha256"],
                "knowledgeContentSha256": self.knowledge_sha,
                "matcherPolicySha256": self.matcher_sha,
                "activeKnowledgeRevision": 1,
                "knowledgeIds": knowledge_ids,
                "knowledgeIdsSha256": project_restore._canonical_sha256(
                    knowledge_ids
                ),
                "artifactRefs": [],
                "artifactRefsSha256": project_restore._canonical_sha256([]),
            }
            binding["bindingSha256"] = project_restore._canonical_sha256(binding)

            configured_plan = (
                copy.deepcopy(host.prepared_response["reusePlan"])
                if host.prepared_response is not None
                else copy.deepcopy(replay.reuse_plan_response)
            )
            matches = []
            if configured_plan is not None:
                for match in configured_plan["matches"]:
                    target = next(
                        (
                            shader
                            for shader in inventory["shaders"]
                            if shader["resourceId"]
                            == match["shader"]["resourceId"]
                            and shader["stableKeySha256"]
                            == match["shader"]["stableKeySha256"]
                        ),
                        None,
                    )
                    if target is None:
                        continue
                    match["shader"] = copy.deepcopy(target)
                    match["materialCandidates"] = []
                    matches.append(match)
            policy = {
                "inventoryOnly": True,
                "fuzzyExecutableMatchAllowed": False,
                "stableKeyExactRequiredForSourceOrBinary": True,
                "rawExactSourcePreparationAllowed": True,
                "stableKeyExactRequiredForBinary": True,
                "targetAnchorRequiredBeforeExecutableApply": True,
                "binaryExecutableAutoApplyAuthorized": False,
                "inventoryMaterialValueReuseAllowed": False,
            }
            plan = {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": (
                    capture_id if cache_lookup else project_capture["captureId"]
                ),
                "catalogSha256": (
                    catalog_sha256
                    if cache_lookup
                    else project_capture["inventorySha256"]
                ),
                "matchedShaderCount": len(matches),
                "matches": matches,
                "policy": policy,
            }
            snapshot_value = {
                "schema": "renderdoc-ultra-project-shader-inventory-reuse/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "projectPath": str(self.replay_path),
                "inventory": copy.deepcopy(inventory),
                "reusePlan": plan,
                "binding": binding,
                "knowledge": knowledge,
            }
            snapshot_value["snapshotSha256"] = project_restore._canonical_sha256(
                {"binding": binding, "knowledge": knowledge}
            )
            last_snapshot = copy.deepcopy(snapshot_value)
            replay.last_inventory_snapshot = copy.deepcopy(snapshot_value)
            return snapshot_value

        def seed(knowledge_id, destination_status, target_shader, inventory, binding):
            del destination_status, binding
            record = self.state["workspaces"][knowledge_id]
            if target_shader["stableKeySha256"] != record["stableKeySha256"]:
                raise AssertionError("Shader inventory stable key changed")
            host.seed_calls += 1
            host.target_status["source"].update(
                {
                    "currentSha256": record["sourceSha256"],
                    "modifiedSinceDecompile": True,
                }
            )
            return {
                "schema": "renderdoc-ultra-project-workspace-inventory-seed/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": inventory["capture"]["captureId"],
                "catalogSha256": inventory["capture"]["inventorySha256"],
                "knowledgeId": knowledge_id,
                "targetResourceId": target_shader["resourceId"],
                "targetStableKeySha256": target_shader["stableKeySha256"],
                "workspacePath": str(host.target_workspace),
                "sourceSha256": record["sourceSha256"],
                "applied": False,
            }

        def register(status, target_shader, inventory, expected_binding):
            nonlocal last_snapshot
            if (
                last_snapshot is None
                or last_snapshot["inventory"] != inventory
                or last_snapshot["binding"] != expected_binding
            ):
                raise AssertionError("Shader inventory publication binding changed")
            matches = [
                (knowledge_id, value)
                for knowledge_id, value in last_snapshot["knowledge"][
                    "workspaces"
                ].items()
                if value["physicalIdentity"]["stableKeySha256"]
                == target_shader["stableKeySha256"]
            ]
            if len(matches) != 1:
                raise AssertionError("Shader inventory publication target changed")
            knowledge_id, artifact = matches[0]
            latest = status.get("latestCompiled")
            if not isinstance(latest, dict):
                raise AssertionError("validated Shader candidate is absent")
            artifact["compiledCandidate"] = {
                "rawSha256": latest["rawSha256"],
                "sourceSha256": latest["sourceSha256"],
                "sourceRevision": latest["revision"],
                "targetProfile": copy.deepcopy(status["targetProfile"]),
            }
            last_snapshot["snapshotSha256"] = project_restore._canonical_sha256(
                {
                    "binding": last_snapshot["binding"],
                    "knowledge": last_snapshot["knowledge"],
                }
            )
            return {
                "knowledge": {"knowledgeId": knowledge_id},
                "idempotent": False,
                "applied": False,
                "binding": copy.deepcopy(last_snapshot["binding"]),
                "snapshotSha256": last_snapshot["snapshotSha256"],
            }

        replay.inventory_snapshot_handler = snapshot
        replay.inventory_seed_handler = seed
        replay.inventory_register_handler = register

    def _run(
        self,
        *,
        write_error=None,
        bind_error=None,
        index_present=True,
        auto_prepare=False,
        with_material=False,
        with_binary=False,
        compile_error=None,
        switch_project_on_compile=False,
        switch_project_on_apply=False,
        with_aliases=False,
        alias_load_error=None,
        alias_load_error_after_commit=None,
        alias_response_mutator=None,
        require_alias_before_apply=False,
        apply_response_mutator=None,
        apply_error_after_commit=None,
        binary_import_error=None,
        binary_import_source_change=None,
        preexisting_binary_profile_mismatch=False,
        preexisting_binary_compiler_mismatch=False,
        precreate_target_workspace=False,
        extra_workspace_statuses=None,
        apply_error_workspace_ids=None,
        inventory_asset_mutator=None,
        inventory_unindexed_resource_ids=None,
        inventory_result_mutator=None,
        expected_result_mutator=None,
        prepare_progress_events=None,
        material_update_error=None,
        shader_rollback_error=None,
        switch_project_after_material_success=False,
        shared_replacements=None,
        inventory_register_error=None,
        rdc_section_version=1,
        cancel_on_progress_phase=None,
        deadline_monotonic=None,
        return_control_error=False,
        resource_index_begin_running=False,
        resource_index_poll_errors=None,
        source_preparation_only=False,
        source_preparation_mutator=None,
    ):
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index if index_present else None,
            self.identity_evidence,
        )
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog_shaders = {
            shader["resourceId"]: shader for shader in catalog["shaders"]
        }
        for identity in self.state["capture"]["shaders"]:
            catalog_shader = catalog_shaders[identity["resourceId"]]
            blocks = [
                {
                    **project_restore._catalog_block_projection(
                        block, index, identity["stage"]
                    ),
                    "name": str(block.get("name", "") or ""),
                    "fields": [],
                }
                for index, block in enumerate(catalog_shader["constantBlocks"])
            ]
            asset = {
                **copy.deepcopy(identity),
                "stableKeyMaterial": {
                    field: identity[field]
                    for field in (
                        "api",
                        "encoding",
                        "rawSha256",
                        "stage",
                        "entryPoint",
                        "abiHash",
                        "layoutHash",
                    )
                },
                "ownerResourceId": identity["resourceId"],
                "ownerCaptureSessionId": self.session_id,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
                "interfaceHash": self.interface_sha,
                "interfaceComplete": True,
                "interfaceEvidence": "null-pipeline-reflection",
                "constantBlocks": blocks,
                "rawSize": 4,
                "rawBytecodeIncluded": False,
                "formatSupported": True,
                "identityAmbiguous": False,
                "physicalAbiSha256": self.live_abi_sha,
                "physicalAbiValidated": True,
                "requiresAnchorPreflight": True,
                "anchorSource": "resource-usage-index",
                "exactExecutableMatchEligible": True,
            }
            if inventory_asset_mutator is not None:
                inventory_asset_mutator(asset)
            if source_preparation_only:
                asset.update(interfaceComplete=False, stableKeyComplete=False,
                             exactExecutableMatchEligible=False)
            host.ipc.inventory_assets.append(asset)
        host.ipc.inventory_unindexed_resource_ids = list(
            inventory_unindexed_resource_ids or []
        )
        host.ipc.inventory_result_mutator = inventory_result_mutator
        host.ipc.expected_result_mutator = expected_result_mutator
        host.ipc.shared_replacements = copy.deepcopy(shared_replacements or [])
        host.ipc.section_version = rdc_section_version
        host.ipc.begin_reports_running = resource_index_begin_running
        host.ipc.poll_errors = list(resource_index_poll_errors or [])
        if auto_prepare:
            self._configure_reuse_plan(
                host,
                with_material=with_material,
                with_binary=with_binary,
                binary_import_error=binary_import_error,
                binary_import_source_change=binary_import_source_change,
                preexisting_binary_profile_mismatch=(
                    preexisting_binary_profile_mismatch
                ),
                preexisting_binary_compiler_mismatch=(
                    preexisting_binary_compiler_mismatch
                ),
                precreate_target_workspace=precreate_target_workspace,
            )
        host.compile_error = compile_error
        host.switch_project_on_compile = switch_project_on_compile
        host.switch_project_on_apply = switch_project_on_apply
        host.ipc.write_error = write_error
        host.ipc.bind_error = bind_error
        replay = _ReplayService(self.state)
        self._configure_inventory_replay(
            replay, host, index_present=index_present
        )
        if source_preparation_only:
            candidate = host.prepared_response["reusePlan"]["matches"][0]["workspaceCandidates"][0]
            candidate.update(sourcePreparationOnly=True, binaryCandidateEligible=False, binaryCandidate=None)
            host.target_status["source"].update({
                "currentSha256": self.source_sha,
                "projectSource": {
                    "schema":"replaylab-verified-project-shader-source/v1",
                    "projectId":self.project_id,"projectPath":str(self.replay_path),
                    "knowledgeId":self.knowledge_id,"sourceSha256":self.source_sha,
                    "originalRawSha256":self.raw_sha,"baselineAbiSha256":self.live_abi_sha,
                    "stage":self.identity["stage"],"entryPoint":self.identity["entryPoint"],
                    "targetProfile":copy.deepcopy(host.target_status["targetProfile"]),
                    "applied":False,"requiresCompile":True,"requiresLiveAbiRevalidation":True,
                },
            })
            if source_preparation_mutator is not None:
                source_preparation_mutator(host, replay)
        if inventory_register_error is not None:
            def reject_inventory_publication(*unused_args, **unused_kwargs):
                raise inventory_register_error

            replay.inventory_register_handler = reject_inventory_publication
        if with_aliases:
            self._configure_alias_reuse(host, replay, prepared=auto_prepare)
        host.ipc.alias_load_error = alias_load_error
        host.ipc.alias_load_error_after_commit = alias_load_error_after_commit
        host.ipc.alias_response_mutator = alias_response_mutator
        host.require_alias_before_apply = require_alias_before_apply
        host.apply_response_mutator = apply_response_mutator
        host.apply_error_after_commit = apply_error_after_commit
        host.workspace_service.extra_statuses = list(extra_workspace_statuses or [])
        host.apply_error_workspace_ids = set(apply_error_workspace_ids or [])
        host.prepare_progress_events = list(prepare_progress_events or [])
        host.material_update_error = material_update_error
        host.shader_rollback_error = shader_rollback_error
        host.switch_project_after_material_success = (
            switch_project_after_material_success
        )
        progress = []
        cancel_event = (
            threading.Event() if cancel_on_progress_phase is not None else None
        )

        def record_progress(event):
            progress.append(event)
            if (
                cancel_event is not None
                and event.get("phase") == cancel_on_progress_phase
            ):
                cancel_event.set()

        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=replay
        ):
            try:
                result = project_restore.restore_active_capture_project(
                    self.project,
                    self.capture_path,
                    self.session_id,
                    record_progress,
                    cancel_event=cancel_event,
                    deadline_monotonic=deadline_monotonic,
                )
            except project_restore.RestoreControlStop as exc:
                if not return_control_error:
                    raise
                result = exc
        return result, host, replay, progress

    def test_progress_item_clips_long_unicode_without_interrupting_callback(self) -> None:
        events = []
        warnings = []
        progress = project_restore._Progress("a" * 32, events.append, warnings)

        progress.emit(
            "shader-apply",
            "Applying Shader",
            current=1,
            total=1,
            item="目录/" + ("着" * 2000) + "/shader.hlsl",
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(len(events[0]["item"].encode("utf-8")), 1024)
        self.assertTrue(events[0]["item"].startswith("目录/"))
        self.assertEqual(warnings, [])

    def test_empty_project_never_starts_shader_inventory(self) -> None:
        empty_status = {
            "projectId": self.project_id,
            "knowledgeCounts": {
                "workspaces": 0,
                "materials": 0,
                "aliases": 0,
            },
        }
        with mock.patch.object(
            _ReplayService, "status", return_value=empty_status, create=True
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertEqual(result["restoreMode"], "no-project-knowledge")
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(result["materials"]["attempted"], 0)
        self.assertEqual(
            result["inventoryCache"]["reason"],
            "not-required-for-empty-project",
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("read_rdc_project_index", methods)
        self.assertNotIn("begin_rdc_project_shader_resource_index", methods)
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_exact_cache_hit_indexes_resources_and_never_prepares_full_frame(self) -> None:
        result, host, replay, progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.scope_calls, 1)
        self.assertEqual(host.projection_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.snapshot_calls, 2)
        self.assertEqual(replay.inventory_snapshot_calls, 0)
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["desiredHead"]["headSha256"], self.binding["desiredProjectHead"]["headSha256"])
        self.assertEqual(result["realizedReceipts"]["validatedForRouting"], 1)
        self.assertEqual(result["realizedReceipts"]["persisted"], 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 1
        )
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 1
        )
        self.assertNotIn("bind_rdc_project_shader_catalog", methods)
        self.assertNotIn("export_shader_catalog", methods)
        begin = next(
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        )
        self.assertFalse(begin["inventory_all_shaders"])
        self.assertFalse(begin["discover_anchor_eids"])
        self.assertEqual(len(begin["expected_shaders"]), 1)
        expected = host.ipc.resource_index_snapshot["result"]["assets"][0]
        self.assertEqual(expected, {
            **begin["expected_shaders"][0],
            "ownerResourceId": 7,
            "ownerCaptureSessionId": self.session_id,
            "resourceOwnershipScope": "target-capture-session",
            "crossCaptureResourceIdReuse": False,
            "api": "d3d11",
            "physicalAbiSha256": "f" * 64,
            "physicalAbiValidated": True,
            "requiresAnchorPreflight": True,
            "constantBlocksMatched": True,
            "interfaceMatched": True,
            "contentMatchMode": "stable-key-owner-rid",
        })
        self.assertEqual(expected["encoding"], "dxbc")
        self.assertEqual(expected["abiHash"], self.live_abi_sha)
        self.assertTrue(expected["stableKeyComplete"])
        self.assertEqual(expected["stableKeySha256"], self.stable_key_sha)
        self.assertEqual(expected["interfaceHash"], self.interface_sha)
        self.assertEqual(expected["layoutHash"], self.layout_sha)
        self.assertTrue(expected["interfaceComplete"])
        self.assertEqual(
            set(expected["constantBlocks"][0]),
            {
                "space",
                "bind",
                "index",
                "path",
                "byteSize",
                "layoutSha256",
                "abiSha256",
            },
        )
        phases = [event["phase"] for event in progress]
        self.assertNotIn("resource-inventory", phases)
        self.assertNotIn("prepare", phases)
        replacement = host.last_apply_response["replacement"]
        self.assertTrue(replacement["resourceScoped"])
        self.assertFalse(replacement["eventScoped"])
        self.assertTrue(replacement["allOriginalResourceUsesAffected"])
        self.assertTrue(replacement["originalShaderPreserved"])
        self.assertTrue(all(len(event["message"].encode("utf-8")) <= 1024 for event in progress))

    def test_foreground_poll_timeout_retries_the_same_shader_index_job(self) -> None:
        transient = RuntimeError(
            "Timeout (5s). Make sure RenderDoc is running with capture loaded. "
            "Check Tools > ReplayLab/RenderDoc MCP > Status."
        )
        result, host, unused_replay, progress = self._run(
            resource_index_begin_running=True,
            resource_index_poll_errors=[transient],
        )

        self.assertTrue(result["completed"])
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 1
        )
        self.assertEqual(
            methods.count("poll_rdc_project_shader_resource_index"), 2
        )
        begin = next(
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        )
        self.assertFalse(begin["inventory_all_shaders"])
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_same_capture_session_adopts_exact_live_replacement_without_mutation(
        self,
    ) -> None:
        result, host, unused_replay, unused_progress = self._run(
            shared_replacements=[self._live_replacement_record()]
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertTrue(result["shaders"]["items"][0]["idempotentlyAdopted"])
        self.assertEqual(result["realizedReceipts"]["adoptedCurrentSession"], 1)
        self.assertEqual(result["realizedReceipts"]["mutationsCreated"], 0)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(methods.count("get_shared_shader_replacements"), 1)
        receipt = host.ipc.index["bindings"][0]["realizedApplySnapshot"][
            "receipts"
        ][0]
        self.assertNotIn("replacementId", receipt)
        self.assertNotIn("captureSessionId", receipt)
        self.assertNotIn("operationId", receipt)
        self.assertNotIn("targetResourceId", receipt)
        self.assertTrue(receipt["routingOnly"])
        self.assertFalse(receipt["physicalApplyAuthority"])

    def test_v2_rdc_section_transport_remains_readable_after_v3_codec(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            rdc_section_version=2
        )

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(host.apply_calls, 1)

    def test_unrelated_active_revision_audit_does_not_invalidate_desired_head(
        self,
    ) -> None:
        self.binding["desiredProjectHead"]["activeKnowledgeRevision"] = 41
        self.index = upsert_project_binding(new_index(), self.binding)

        result, host, unused_replay, unused_progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["desiredHead"]["activeKnowledgeRevision"], 41)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.apply_calls, 1)

    def test_new_capture_session_never_uses_persisted_receipt_as_apply_authority(
        self,
    ) -> None:
        self.session_id = "4" * 32

        result, host, unused_replay, unused_progress = self._run()

        self.assertTrue(result["completed"])
        self.assertEqual(result["realizedReceipts"]["validatedForRouting"], 1)
        self.assertEqual(result["realizedReceipts"]["adoptedCurrentSession"], 0)
        self.assertEqual(host.apply_calls, 1)

    def test_tampered_or_abi_mismatched_receipt_falls_back_before_apply(self) -> None:
        for label, field in (
            ("candidate-tamper", "candidateRawSha256"),
            ("abi-mismatch", "physicalAbiSha256"),
        ):
            with self.subTest(label=label):
                forged = copy.deepcopy(self.index)
                receipt = forged["bindings"][0]["realizedApplySnapshot"][
                    "receipts"
                ][0]
                receipt[field] = "4" * 64
                receipt["receiptSha256"] = project_restore._canonical_sha256(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receiptSha256"
                    }
                )
                snapshot = forged["bindings"][0]["realizedApplySnapshot"]
                snapshot["receiptsSha256"] = project_restore._canonical_sha256(
                    snapshot["receipts"]
                )
                self.index = self._reseal_index_document(forged)

                result, host, unused_replay, progress = self._run(
                    auto_prepare=True
                )

                self.assertTrue(result["completed"])
                self.assertFalse(result["cache"]["hit"])
                self.assertEqual(result["restoreMode"], "project-target-inventory")
                self.assertFalse(result["fullFrame"])
                self.assertEqual(result["shaders"]["applied"], 1)
                self.assertEqual(host.apply_calls, 1)
                begin = next(
                    params
                    for method, params in host.ipc.calls
                    if method == "begin_rdc_project_shader_resource_index"
                )
                self.assertTrue(begin["inventory_all_shaders"])
                self.assertIn(
                    "resource-inventory", [event["phase"] for event in progress]
                )
                self._refresh_exact_binding([self.shader_record])

    def test_index_miss_uses_sealed_inventory_sidecar_and_only_fresh_targets(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "write_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ) as cache_write:
            result, host, replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertTrue(result["inventoryCache"]["hit"])
        self.assertTrue(result["inventoryCache"]["written"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 2)
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertFalse(begins[0]["inventory_all_shaders"])
        self.assertFalse(begins[0]["discover_anchor_eids"])
        self.assertEqual(len(begins[0]["expected_shaders"]), 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 1
        )
        self.assertIn("write_rdc_project_index", methods)
        self.assertEqual(cache_write.call_count, 1)
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_first_project_target_lookup_never_publishes_a_full_sidecar(self) -> None:
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["inventoryCache"]["hit"])
        self.assertFalse(result["inventoryCache"]["written"])
        document = load_shader_inventory_cache(
            self.replay_path,
            self.project_id,
            self.scope_sha,
            self.capture_path,
        )
        self.assertIsNone(document)
        self.assertEqual(result["inventoryScope"], "target-raw-sha256")
        self.assertFalse(result["fullFrame"])
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertTrue(begins[0]["inventory_all_shaders"])
        self.assertEqual(begins[0]["target_raw_sha256s"], [self.raw_sha])
        self.assertIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

        # With no RDC association, a second run stays a narrow target lookup;
        # a subset is never promoted into the full-inventory sidecar cache.
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ):
            second, second_host, unused_replay, second_progress = self._run(
                index_present=False,
                auto_prepare=True,
            )
        self.assertTrue(second["completed"])
        self.assertFalse(second["inventoryCache"]["hit"])
        second_begins = [
            params
            for method, params in second_host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(second_begins), 1)
        self.assertEqual(second_begins[0]["target_raw_sha256s"], [self.raw_sha])
        self.assertEqual(second_begins[0]["expected_shaders"], [])
        self.assertIn(
            "resource-inventory",
            [event["phase"] for event in second_progress],
        )

    def test_sidecar_target_mismatch_falls_back_before_apply(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "_resource_index_job",
            side_effect=project_restore.CatalogPreparationRequired(
                "fresh ABI changed", "shader-resource-index-interface-mismatch"
            ),
        ), mock.patch.object(
            project_restore,
            "write_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["inventoryCache"]["hit"])
        self.assertIn(
            "live-validation-failed",
            result["inventoryCache"]["reason"],
        )
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 1)
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertTrue(begins[0]["inventory_all_shaders"])
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertLess(
            phases.index("resource-inventory"), phases.index("shader-apply")
        )

    def test_sidecar_poll_failure_never_expands_to_full_inventory(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "_resource_index_job",
            side_effect=project_restore.CatalogPreparationRequired(
                "bounded bridge poll timed out",
                "shader-resource-index-timeout",
            ),
        ), mock.patch.object(
            project_restore, "_inventory_shader_resources"
        ) as full_inventory:
            result, unused_host, unused_replay, unused_progress = self._run(
                index_present=False, auto_prepare=True
            )

        full_inventory.assert_not_called()
        self.assertFalse(result["completed"])
        self.assertEqual(result["outcome"], "action-required")
        self.assertEqual(
            result["reasonId"], "shader-resource-index-timeout"
        )

    def test_v2_cache_identity_accepts_legacy_project_catalog_only_before_live_job(
        self,
    ) -> None:
        legacy = {
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "interfaceComplete": True,
            "eventIds": [9],
        }

        matched = project_restore._capture_shader(
            {"capture": {"shaders": [legacy]}}, self.identity
        )

        self.assertEqual(matched, legacy)

    def test_cache_hit_ignores_legacy_material_values_and_rewrites_empty_materials(
        self,
    ) -> None:
        material_records = [
            {
                "knowledgeId": "6" * 32,
                "shaderResourceId": 7,
                "stage": "fragment",
                "path": "PerMaterial",
                "layoutSha256": self.material_layout_sha,
                "eventId": 9,
                "blockIndex": 0,
                "parameters": [{"parameterId": "7" * 32}],
            }
        ]
        self._refresh_exact_binding(self.binding["shaders"], material_records)

        result, host, unused_replay, progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(
            result["materials"],
            {
                "attempted": 0,
                "restored": 0,
                "skipped": 0,
                "failed": 0,
                "sessionIds": [],
                "items": [],
            },
        )
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertNotIn("material-restore", [event["phase"] for event in progress])
        self.assertEqual(host.ipc.index["bindings"][0]["materials"], [])

    def test_cached_duplicate_resource_is_rejected_before_workspace_validation(
        self,
    ) -> None:
        duplicate = copy.deepcopy(self.shader_record)
        duplicate["managedWorkspace"] = {
            "workspaceId": "e" * 32,
            "path": "workspaces/shaders/duplicate",
        }
        binding = copy.deepcopy(self.binding)
        binding["shaders"].append(duplicate)

        with mock.patch.object(
            project_restore, "_validate_workspace_record"
        ) as validate_workspace:
            with self.assertRaisesRegex(
                RuntimeError,
                "one physical Shader resource more than once",
            ):
                project_restore._restore_from_cache(
                    object(),
                    object(),
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    binding,
                    {},
                    {},
                    mock.Mock(),
                    {"attempted": 0},
                    {"attempted": 0},
                    [],
                    [],
                    [],
                )

        validate_workspace.assert_not_called()

    def test_binding_document_rejects_two_executables_for_one_resource(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "one physical Shader resource more than once",
        ):
            project_restore._binding_document(
                self.active,
                {
                    "projectId": self.project_id,
                    "scopeSha256": self.scope_sha,
                    "captureId": self.capture_id,
                    "catalogSha256": self.catalog_sha,
                    "knowledgeContentSha256": self.knowledge_sha,
                    "matcherPolicySha256": self.matcher_sha,
                },
                [self.shader_record, copy.deepcopy(self.shader_record)],
                [],
            )

    def test_project_inventory_is_canonical_for_real_project_service(self) -> None:
        result, host, replay, unused_progress = self._run(
            index_present=False, auto_prepare=True
        )

        self.assertTrue(result["completed"])
        projected = replay.last_inventory_snapshot["inventory"]
        real_project = self.root / "canonical-replay-project"
        project_restore.ReplayProjectService.create(
            real_project,
            name="canonical inventory",
            game_id="game",
            build_id="build",
            api_family="d3d11",
        )
        snapshot = project_restore.ReplayProjectService(
            real_project
        ).reuse_snapshot_for_shader_inventory(projected)

        self.assertEqual(snapshot["inventory"], projected)
        self.assertEqual(snapshot["inventory"]["shaders"][0]["encoding"], "DXBC")
        self.assertEqual(
            snapshot["inventory"]["shaders"][0]["stableKeySha256"],
            self.stable_key_sha,
        )

        # Regress the real failure mode: Bridge protocol encoding is lowercase
        # ``dxil`` while the persistent project contract canonicalises it to
        # ``DXIL``.  The strict response equality check must still pass without
        # weakening the seven-field stable key.
        dxil_stable_key = project_restore._canonical_sha256(
            {
                "api": "d3d12",
                "encoding": "dxil",
                "rawSha256": self.raw_sha,
                "stage": "fragment",
                "entryPoint": "main",
                "abiHash": self.live_abi_sha,
                "layoutHash": self.layout_sha,
            }
        )
        host.ipc.inventory_assets[0].update(
            {
                "api": "d3d12",
                "encoding": "dxil",
                "stableKeySha256": dxil_stable_key,
            }
        )
        host.ipc.resource_index_snapshot = None
        live_dxil = project_restore._inventory_shader_resources(
            host,
            self.capture_path,
            self.session_id,
            "d3d12",
            project_restore._Progress("3" * 32, None, []),
        )
        dxil_project = self.root / "canonical-dxil-replay-project"
        dxil_status = project_restore.ReplayProjectService.create(
            dxil_project,
            name="canonical DXIL inventory",
            game_id="game",
            build_id="build",
            api_family="d3d12",
        )
        prepared = project_restore._prepare_inventory_response(
            project_restore.ReplayProjectService(dxil_project),
            {"replayProject": {"projectId": dxil_status["projectId"]}},
            live_dxil,
            project_restore._Progress("4" * 32, None, []),
        )

        self.assertEqual(prepared["inventory"]["shaders"][0]["encoding"], "DXIL")
        self.assertEqual(
            prepared["inventory"]["shaders"][0]["stableKeySha256"],
            dxil_stable_key,
        )
        self.assertEqual(prepared["reusePlan"]["matches"], [])

    def test_unknown_raw_identity_does_not_claim_project_targets_absent(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[8]
        )

        self.assertFalse(result["completed"])
        self.assertEqual(result["reasonId"], "shader-resource-index-targets-incomplete")
        self.assertEqual(result["shaders"]["applied"], 0)
        inventory = host.ipc.resource_index_snapshot["result"]
        self.assertEqual(inventory["uniqueShaderResourceCount"], 2)
        self.assertEqual(inventory["indexedShaderResourceCount"], 1)
        self.assertEqual(inventory["unindexedShaderResourceCount"], 1)
        self.assertEqual(inventory["unindexedShaderResourceIds"], [8])
        completed = [
            event
            for event in progress
            if event["phase"] == "resource-inventory"
            and event["message"] == "Unique Shader resource inventory completed"
        ][-1]
        self.assertEqual((completed["current"], completed["total"]), (2, 2))
        self.assertEqual(completed["detail"]["indexedShaderResourceCount"], 1)
        self.assertEqual(completed["detail"]["unindexedShaderResourceCount"], 1)

    def test_unindexed_shader_ledger_fails_closed_when_resource_is_also_indexed(
        self,
    ) -> None:
        result, unused_host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[7]
        )

        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )
        self.assertFalse(result["completed"])

    def test_legacy_v2_inventory_without_ledger_remains_compatible_when_complete(
        self,
    ) -> None:
        def omit_ledger(result):
            result.pop("indexedShaderResourceCount")
            result.pop("unindexedShaderResourceCount")
            result.pop("unindexedShaderResourceIds")

        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_result_mutator=omit_ledger
        )

        # The old unfiltered API still accepts complete v2 evidence; the new
        # target API requires its explicit coverage ledger instead.
        self.assertFalse(result["completed"])
        inventory = project_restore._inventory_shader_resources(
            host, self.capture_path, self.session_id, "D3D11",
            project_restore._Progress("a" * 32, None, []))
        self.assertEqual(len(inventory["shaders"]), 1)

    def test_legacy_v2_inventory_without_ledger_rejects_unreported_gap(self) -> None:
        def omit_ledger(result):
            result.pop("indexedShaderResourceCount")
            result.pop("unindexedShaderResourceCount")
            result.pop("unindexedShaderResourceIds")

        result, unused_host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[8],
            inventory_result_mutator=omit_ledger,
        )

        self.assertFalse(result["completed"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )

    def test_malformed_unindexed_shader_ledgers_have_one_fail_closed_reason(
        self,
    ) -> None:
        def partial(result):
            result.pop("unindexedShaderResourceIds")

        def non_list(result):
            result["unindexedShaderResourceIds"] = "8"

        def invalid_resource_id(result):
            result["uniqueShaderResourceCount"] = 2
            result["processedShaderResourceCount"] = 2
            result["unindexedShaderResourceCount"] = 1
            result["unindexedShaderResourceIds"] = [0]

        def duplicate_resource_id(result):
            result["uniqueShaderResourceCount"] = 3
            result["processedShaderResourceCount"] = 3
            result["unindexedShaderResourceCount"] = 2
            result["unindexedShaderResourceIds"] = [8, 8]

        for label, mutate in {
            "partial": partial,
            "non-list": non_list,
            "invalid-resource-id": invalid_resource_id,
            "duplicate-resource-id": duplicate_resource_id,
        }.items():
            with self.subTest(label=label):
                result, unused_host, unused_replay, unused_progress = self._run(
                    index_present=False,
                    auto_prepare=True,
                    inventory_result_mutator=mutate
                )
                self.assertFalse(result["completed"])
                self.assertEqual(
                    result["reasonId"],
                    "shader-resource-index-invalid-response",
                )

    def test_physical_shader_resource_count_obeys_inventory_limit(self) -> None:
        with mock.patch.object(project_restore, "_MAX_FAST_CATALOG_SHADERS", 1):
            result, unused_host, unused_replay, unused_progress = self._run(
                index_present=False,
                auto_prepare=True,
                inventory_unindexed_resource_ids=[8]
            )

        self.assertFalse(result["completed"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )

    def test_cache_hit_reloads_project_alias_overlay_before_shader_apply(self) -> None:
        result, host, replay, progress = self._run(
            with_aliases=True,
            require_alias_before_apply=True,
        )

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(replay.reuse_plan_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 0)
        self.assertEqual(replay.materialize_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("load_symbol_aliases", methods)
        load = next(
            params
            for method, params in host.ipc.calls
            if method == "load_symbol_aliases"
        )
        self.assertTrue(load["confirmed_only"])
        self.assertTrue(load["strict_atomic"])
        self.assertTrue(Path(load["path"]).is_file())
        phases = [event["phase"] for event in progress]
        self.assertIn("alias-restore", phases)
        self.assertLess(phases.index("alias-restore"), phases.index("shader-apply"))

    def test_apply_rejects_forged_abi_and_replacement_identity_without_reindexing(self) -> None:
        mutations = {
            "abi-pending": lambda response: response["validation"].update(
                {
                    "compatible": False,
                    "requiresReplayAbiValidation": True,
                    "replayAbiValidated": False,
                }
            ),
            "foreign-resource": lambda response: response["replacement"].update(
                {"originalResourceId": 404}
            ),
            "foreign-capture": lambda response: response["replacement"].update(
                {"captureId": "4" * 64}
            ),
            "foreign-anchor": lambda response: response["replacement"].update(
                {"anchorEventId": 77}
            ),
            "invalid-id": lambda response: response["replacement"].update(
                {"replacementId": "not-a-replacement"}
            ),
            "foreign-stage": lambda response: response["binding"].update(
                {"stage": "vertex"}
            ),
            "baseline-abi-mismatch": lambda response: response["binding"].update(
                {"baselineAbiSha256": "4" * 64}
            ),
            "candidate-abi-mismatch": lambda response: response[
                "validation"
            ].update({"candidateAbiSha256": "4" * 64}),
            "candidate-bytes-mismatch": lambda response: response["binding"].update(
                {"candidateRawSha256": "4" * 64}),
            "report-path-mismatch": lambda response: response["validation"].update(
                {"abiReportPath": str(self.workspace_status["workspacePath"]) + "/other-report.json"}),
            "reported-digest-mismatch": lambda response: (
                response["validation"].update({"abiReportSha256": "4" * 64}),
                response["replacement"].update({"abiReportSha256": "4" * 64})),
            "request-cross-binding": lambda response: response[
                "replacement"
            ].update({"abiRequestId": "4" * 64}),
            "report-cross-binding": lambda response: response[
                "replacement"
            ].update({"abiReportSha256": "4" * 64}),
            "physical-abi-mismatch": lambda response: response[
                "replacement"
            ].update({"physicalAbiSha256": "4" * 64}),
            "foreign-rollback": lambda response: response["rollback"].update(
                {"replacementId": "4" * 32}
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                original_index = copy.deepcopy(self.index)
                result, host, unused_replay, progress = self._run(
                    apply_response_mutator=mutation
                )

                self.assertTrue(result["completed"])
                self.assertEqual(result["shaders"]["applied"], 0)
                self.assertEqual(result["shaders"]["failed"], 1)
                self.assertIn(
                    "invalid live ABI/replacement evidence",
                    result["shaders"]["items"][0]["error"],
                )
                self.assertFalse(result["index"]["written"])
                self.assertEqual(host.ipc.index, original_index)
                methods = [method for method, unused in host.ipc.calls]
                self.assertNotIn("write_rdc_project_index", methods)
                self.assertEqual(progress[-1]["phase"], "complete")

    def test_partial_cache_apply_preserves_full_binding_for_retry(self) -> None:
        second_status = self._add_second_cached_shader()
        original_index = copy.deepcopy(self.index)

        result, host, replay, progress = self._run(
            extra_workspace_statuses=[second_status],
            apply_error_workspace_ids={second_status["workspaceId"]},
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["attempted"], 2)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertFalse(result["index"]["written"])
        self.assertEqual(result["realizedReceipts"]["persisted"], 0)
        self.assertIn("preserved for retry", result["index"]["warning"])
        self.assertEqual(host.ipc.index, original_index)
        self.assertEqual(
            host.ipc.index["bindings"][0]["realizedApplySnapshot"],
            original_index["bindings"][0]["realizedApplySnapshot"],
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("write_rdc_project_index", methods)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_alias_load_failure_is_reported_without_blocking_shader_restore(self) -> None:
        result, host, replay, progress = self._run(
            with_aliases=True,
            alias_load_error=RuntimeError("alias overlay rejected"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(replay.materialize_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertTrue(any("alias overlay rejected" in item for item in result["warnings"]))
        methods = [method for method, unused in host.ipc.calls]
        # The recovered binding is byte-for-byte identical, so reopening must
        # not rewrite the RDC section merely because best-effort alias loading
        # reported a display-only warning.
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("alias-restore", phases)
        self.assertNotIn("prepare", phases)
        self.assertEqual(phases[-1], "complete")
        failed_alias = [
            event
            for event in progress
            if event["phase"] == "alias-restore"
            and event["detail"].get("status") == "failed"
        ]
        self.assertEqual(len(failed_alias), 1)

    def test_alias_response_lost_after_commit_is_strictly_recovered(self) -> None:
        result, host, unused_replay, progress = self._run(
            with_aliases=True,
            alias_load_error_after_commit=TimeoutError(
                "alias response was lost after commit"
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertTrue(
            any("fail-safe recovered" in warning for warning in result["warnings"])
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("get_symbol_alias_transaction", methods)
        self.assertIn("rollback_symbol_aliases", methods)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_alias_corrupt_commit_response_is_strictly_recovered(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            with_aliases=True,
            alias_response_mutator=lambda response: response.update(
                {"transactionId": "4" * 32}
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertTrue(
            any("invalid evidence" in warning for warning in result["warnings"])
        )

    def test_alias_commit_then_cancel_rolls_back_total_restore_ledger(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_aliases=True,
            cancel_on_progress_phase="resource-anchor",
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreCancelled)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertEqual(host.active_service.pin_depth, 0)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_alias_commit_then_later_failure_rolls_back_with_shader(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_aliases=True,
            switch_project_on_apply=True,
        )

        self.assertFalse(result["completed"])
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_shader_response_lost_after_mutation_uses_operation_recovery(self) -> None:
        original_index = copy.deepcopy(self.index)
        result, host, unused_replay, progress = self._run(
            apply_error_after_commit=TimeoutError(
                "shared Shader apply response was lost"
            )
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.ipc.shader_operation_recovery_calls, 1)
        self.assertEqual(host.ipc.shader_operations, {})
        self.assertFalse(result["index"]["written"])
        self.assertEqual(host.ipc.index, original_index)
        self.assertIn(
            "fail-safe recovered", result["shaders"]["items"][0]["error"]
        )
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_shader_corrupt_response_rolls_back_by_operation_not_forged_id(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            apply_response_mutator=lambda response: response["replacement"].update(
                {"replacementId": "not-the-real-replacement"}
            )
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.ipc.shader_operation_recovery_calls, 1)
        self.assertEqual(host.ipc.shader_operations, {})
        recovery = next(
            params
            for method, params in host.ipc.calls
            if method == "recover_shader_candidate_shared_resource_operation"
        )
        self.assertEqual(len(recovery["operation_id"]), 32)

    def test_inventory_restore_does_not_trust_legacy_catalog_for_material_values(
        self,
    ) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["shaders"][0]["interfaceHash"] = "d" * 64
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.binding["catalogSha256"] = self.catalog_sha
        self.index = upsert_project_binding(new_index(), self.binding)
        self.workspace_status["capture"]["catalogSha256"] = self.catalog_sha
        self.state["catalogSha256"] = self.catalog_sha

        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertIsNotNone(host.ipc.index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertNotIn("prepare", phases)
        self.assertNotIn("material-restore", phases)
        self.assertEqual(phases[-1], "complete")
        self.assertIn("index-write", phases)

    def test_legacy_material_projection_requires_exact_catalog_reflection(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        legacy_identity = {
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "interfaceComplete": True,
            "eventIds": [9],
        }

        expected = project_restore._expected_resource_shader(
            catalog, legacy_identity, "d3d11", 9
        )

        self.assertEqual(expected["interfaceHash"], self.interface_sha)
        self.assertEqual(expected["layoutHash"], self.layout_sha)
        forged = copy.deepcopy(legacy_identity)
        forged["interfaceHash"] = "d" * 64
        with self.assertRaisesRegex(
            project_restore.CatalogPreparationRequired,
            "legacy Shader interface/layout digest differs",
        ):
            project_restore._expected_resource_shader(
                catalog, forged, "d3d11", 9
            )

    def test_legacy_material_projection_rejects_multi_entrypoint_resource(self) -> None:
        identities = {
            (7, "fragment"): [
                {**copy.deepcopy(self.identity), "entryPoint": "main"},
                {**copy.deepcopy(self.identity), "entryPoint": "shadow"},
            ]
        }

        with self.assertRaisesRegex(
            RuntimeError, "ambiguous across Shader entry points"
        ):
            project_restore._legacy_material_shader_identity(
                identities,
                {"shaderResourceId": 7, "stage": "fragment"},
            )

    def test_v2_material_projection_requires_exact_live_inventory_reflection(
        self,
    ) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        raw_block = catalog["shaders"][0]["constantBlocks"][0]
        live_block = {
            **project_restore._catalog_block_projection(
                raw_block, 0, "fragment"
            ),
            "name": "PerMaterial",
            "fields": [],
        }
        inventory = {
            "shaders": [
                {
                    **copy.deepcopy(self.identity),
                    "interfaceHash": self.interface_sha,
                    "interfaceComplete": True,
                    "constantBlocks": [live_block],
                    "formatSupported": True,
                    "identityAmbiguous": False,
                    "exactExecutableMatchEligible": True,
                }
            ]
        }

        expected = project_restore._expected_resource_shader(
            catalog,
            self.identity,
            "d3d11",
            9,
            inventory=inventory,
        )

        self.assertEqual(expected["constantBlocks"], [live_block])
        tampered = copy.deepcopy(inventory)
        tampered["shaders"][0]["constantBlocks"][0]["abiSha256"] = "d" * 64
        with self.assertRaisesRegex(
            project_restore.CatalogPreparationRequired,
            "ABI/reflection evidence differs",
        ):
            project_restore._expected_resource_shader(
                catalog,
                self.identity,
                "d3d11",
                9,
                inventory=tampered,
            )

    def test_read_only_rdc_does_not_reverse_successful_restore(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            write_error=PermissionError("read only"),
            index_present=False,
            auto_prepare=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["index"]["written"])
        self.assertIn("read only", result["index"]["warning"])
        self.assertEqual(host.prepare_calls, 0)

    def test_absent_index_inventories_reuses_hlsl_and_writes_local_index(self) -> None:
        result, host, replay, progress = self._run(
            index_present=False, auto_prepare=True
        )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertEqual(
            result["cache"]["reason"], "project-reuse-plan:index-absent"
        )
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertTrue(result["index"]["written"])
        self.assertTrue(result["index"]["present"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.seed_calls, 1)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.snapshot_calls, 0)
        self.assertEqual(replay.inventory_snapshot_calls, 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods,
            [
                "read_rdc_project_index",
                "begin_rdc_project_shader_resource_index",
                "get_shared_shader_replacements",
                "write_rdc_project_index",
            ],
        )
        phases = [event["phase"] for event in progress]
        for phase in (
            "resource-inventory",
            "resource-match",
            "shader-prepare",
            "resource-anchor",
            "shader-compile",
            "shader-apply",
            "index-write",
            "complete",
        ):
            self.assertIn(phase, phases)
        self.assertNotIn("index-required", phases)
        self.assertNotIn("failed", phases)
        self.assertNotIn("prepare", phases)
        self.assertLess(phases.index("resource-match"), phases.index("shader-prepare"))
        self.assertLess(phases.index("shader-compile"), phases.index("shader-apply"))
        self.assertLess(phases.index("shader-apply"), phases.index("index-write"))

    def test_incomplete_reflection_source_recompiles_applies_and_opens_without_republication(self):
        def existing_candidate(host, replay):
            # Actual 6.rdc recovered HLSL ABI digest differs from its baseline.
            host.candidate_abi_sha256 = "2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b"
            host.target_status["revision"] = 1
            host.target_status["latestCompiled"] = {
                "revision":1,"sourceSha256":self.source_sha,"rawSha256":"f"*64,
                "kind":"DXBC","byteSize":4}
            host.compile_revision = 2
        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=existing_candidate,
            inventory_register_error=AssertionError("source reuse must not register again"))
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"],1, result)
        self.assertEqual(result["editing"]["enabled"],1, result)
        self.assertEqual(result["realizedReceipts"]["produced"],1)
        self.assertEqual(result["realizedReceipts"]["persisted"],0)
        self.assertFalse(result["index"]["written"])
        self.assertIn("fresh-live-abi-no-executable-index",result["cache"]["reason"])
        self.assertEqual(host.target_status["revision"],2)
        self.assertEqual(host.compile_calls,1)
        self.assertEqual(host.apply_calls,1)
        self.assertNotEqual(host.last_apply_response["validation"]["candidateAbiSha256"],
                            host.last_apply_response["validation"]["baselineAbiSha256"])
        self.assertEqual(host.seed_calls,0)
        self.assertEqual(host.workspace_service.import_calls,0)
        self.assertEqual(replay.inventory_register_calls,0)
        self.assertEqual(host.target_status["source"]["projectSource"]["knowledgeId"],self.knowledge_id)
        self.assertNotIn("get_shared_shader_replacements",[method for method,unused in host.ipc.calls])
        self.assertNotIn("write_rdc_project_index",[method for method,unused in host.ipc.calls])
        self.assertFalse(replay.last_inventory_snapshot["knowledge"]["workspaces"][self.knowledge_id]["interfaceComplete"])
        # Exercise the real GUI client contract: an observed successful apply
        # need not publish a fictitiously complete executable RDC receipt.
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("source_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        accepted = client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)
        self.assertEqual(accepted["editing"]["enabled"], 1)

    def test_actual_capture_distinct_compatible_abi_digests_bind_current_report(self):
        # Values from the accepted 6.rdc public apply receipt, 2026-09-06.
        status = copy.deepcopy(self.workspace_status)
        status["baseline"]["abiSha256"] = "b6020009d362764b604f9abd81b0e1880ecfcca4d997816d632ccd7845a2219e"
        host = _Host(self.active, status, self.capture_path, self.session_id,
                     None, self.identity_evidence)
        host.candidate_abi_sha256 = "2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b"
        workspace = Path(status["workspacePath"])
        wrapped = json.loads(host.apply_shader_workspace_shared_resource(
            str(workspace), status["revision"], self.candidate_sha, "7" * 32,
            expected_anchor_event_id=9))
        checked = project_restore._validate_workspace_apply_response(
            host, workspace, status, wrapped, operation_id="7" * 32,
            expected_revision=status["revision"], expected_candidate_raw_sha256=self.candidate_sha,
            expected_anchor_event_id=9, capture_session_id=self.session_id)
        self.assertIs(checked, wrapped)
        self.assertNotEqual(checked["validation"]["candidateAbiSha256"],
                            checked["validation"]["baselineAbiSha256"])

    def test_source_preparation_checks_original_source_profile_abi_and_project_reference(self):
        for field, replacement in (("knowledgeId","7"*32),("sourceSha256","a"*64),
                                   ("baselineAbiSha256","a"*64),("originalRawSha256","a"*64),
                                   ("projectId","a"*32),("entryPoint","different"),
                                   ("targetProfile",{"profile":"ps_4_0"})):
            with self.subTest(field=field):
                result, host, unused, unused_progress = self._run(
                    index_present=False,auto_prepare=True,source_preparation_only=True,
                    source_preparation_mutator=lambda host,replay:
                        host.target_status["source"]["projectSource"].update({field:replacement}))
                self.assertEqual(result["shaders"]["applied"],0,result)
                self.assertEqual(host.compile_calls,0)
                self.assertEqual(host.apply_calls,0)
                self.assertEqual(host.edit_start_calls,[])

    def test_source_preparation_keeps_compile_and_live_abi_gates(self):
        cases = (
            {"compile_error":RuntimeError("compiler rejected source")},
            {"apply_response_mutator":lambda response:response["validation"].update(compatible=False)},
            {"apply_response_mutator":lambda response:response["replacement"].update(captureSessionId="e"*32)},
        )
        for options in cases:
            with self.subTest(options=options):
                result,host,replay,unused = self._run(
                    index_present=False,auto_prepare=True,source_preparation_only=True,**options)
                self.assertEqual(result["shaders"]["applied"],0,result)
                self.assertEqual(result["shaders"]["failed"],1,result)
                self.assertEqual(host.edit_start_calls,[])
                self.assertEqual(replay.inventory_register_calls,0)
                self.assertFalse(result["index"]["written"])

    def test_source_preparation_revalidates_project_head_before_apply(self):
        def change_head(host,replay):
            original = replay.inventory_snapshot_handler
            checks = []
            def snapshot(inventory,binding):
                if binding is not None:
                    checks.append(True)
                    if len(checks) >= 3:
                        raise RuntimeError("active project knowledge head changed")
                return original(inventory,binding)
            replay.inventory_snapshot_handler = snapshot
        result,host,unused,unused_progress = self._run(
            index_present=False,auto_prepare=True,source_preparation_only=True,
            source_preparation_mutator=change_head)
        self.assertEqual(result["shaders"]["applied"],0,result)
        self.assertEqual(host.compile_calls,1)
        self.assertEqual(host.apply_calls,0)
        self.assertEqual(host.edit_start_calls,[])

    def test_source_preparation_stable_identity_block_is_reported_without_work(self):
        def block_candidate(host, replay):
            match = host.prepared_response["reusePlan"]["matches"][0]
            match["workspaceCandidates"] = []
            match["sourcePreparationBlockedReason"] = "project-shader-stable-identity-mismatch"
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=block_candidate)
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(result["shaders"]["skipped"], 1)
        self.assertEqual(result["shaders"]["items"][0]["reason"],
                         "project-shader-stable-identity-mismatch")
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.edit_start_calls, [])

    def test_source_preparation_does_not_accept_binary_authority(self):
        def allow_binary(host, replay):
            candidate = host.prepared_response["reusePlan"]["matches"][0]["workspaceCandidates"][0]
            candidate["binaryCandidateEligible"] = True
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=allow_binary)
        self.assertEqual(result["shaders"]["skipped"], 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.edit_start_calls, [])

    def test_two_executable_entries_for_one_resource_fail_closed_as_a_group(
        self,
    ) -> None:
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            None,
            self.identity_evidence,
        )
        self._configure_reuse_plan(host)
        duplicate = copy.deepcopy(
            host.prepared_response["reusePlan"]["matches"][0]
        )
        host.prepared_response["reusePlan"]["matches"].append(duplicate)
        host.prepared_response["reusePlan"]["matchedShaderCount"] = 2
        replay = _ReplayService(self.state)
        shaders = {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        materials = {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        }

        with mock.patch.object(
            project_restore,
            "_restore_reuse_aliases_best_effort",
            return_value=True,
        ):
            records, material_records, unused_state = (
                project_restore._restore_from_reuse_plan(
                    host,
                    replay,
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    host.prepared_response,
                    {},
                    project_restore._Progress("1" * 32, None, []),
                    shaders,
                    materials,
                    [],
                    [],
                    [],
                )
            )

        self.assertEqual(records, [])
        self.assertEqual(material_records, [])
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(shaders["attempted"], 0)
        self.assertEqual(shaders["applied"], 0)
        self.assertEqual(shaders["skipped"], 2)
        self.assertTrue(
            all(
                "multiple executable entry points" in item["reason"]
                for item in shaders["items"]
            )
        )

    def test_one_executable_entry_for_one_resource_applies_exactly_once(
        self,
    ) -> None:
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            None,
            self.identity_evidence,
        )
        self._configure_reuse_plan(host)
        metadata_only = copy.deepcopy(
            host.prepared_response["reusePlan"]["matches"][0]
        )
        metadata_only["workspaceCandidates"] = []
        host.prepared_response["reusePlan"]["matches"].append(metadata_only)
        host.prepared_response["reusePlan"]["matchedShaderCount"] = 2
        replay = _ReplayService(self.state)
        shaders = {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        materials = {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        }
        applied_workspaces = []

        with mock.patch.object(
            project_restore,
            "_restore_reuse_aliases_best_effort",
            return_value=True,
        ):
            records, material_records, unused_state = (
                project_restore._restore_from_reuse_plan(
                    host,
                    replay,
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    host.prepared_response,
                    {},
                    project_restore._Progress("2" * 32, None, []),
                    shaders,
                    materials,
                    applied_workspaces,
                    [],
                    [],
                )
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(material_records, [])
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(len(applied_workspaces), 1)
        self.assertEqual(shaders["attempted"], 1)
        self.assertEqual(shaders["applied"], 1)
        self.assertEqual(shaders["skipped"], 1)

    def test_existing_reuse_workspace_is_readmitted_for_frozen_inventory_anchor(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.workspace_rebind_calls, 1)
        self.assertEqual(host.apply_calls, 1)

    def test_existing_reuse_workspace_rejects_historical_anchor_before_apply(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
            inventory_asset_mutator=lambda asset: asset.update(
                {"anchorEventId": 10}
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn(
            "frozen representative EID",
            result["shaders"]["items"][0]["error"],
        )
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.workspace_rebind_calls, 1)
        self.assertEqual(host.apply_calls, 0)
        self.assertFalse(result["index"]["written"])

    def test_inventory_restore_never_invokes_full_catalog_export_progress(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            prepare_progress_events=[
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "wrong-phase",
                    "current": 1,
                    "total": 2,
                    "eventId": 8,
                    "updatedUnixMs": 100,
                },
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "export-shader-bindings",
                    "current": 1,
                    "total": 2,
                    "eventId": 9,
                    "updatedUnixMs": 101,
                },
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "export-shader-bindings",
                    "current": 2,
                    "total": 2,
                    "eventId": 10,
                    "updatedUnixMs": 102,
                },
            ],
        )

        self.assertTrue(result["completed"])
        export_events = [
            event
            for event in progress
            if event["phase"] == "export-shader-bindings"
        ]
        self.assertEqual(export_events, [])
        self.assertEqual(host.prepare_calls, 0)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)

    def test_absent_index_does_not_import_event_material_values(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["attempted"], 0)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["sessionIds"], [])
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        phases = [event["phase"] for event in progress]
        self.assertNotIn("material-restore", phases)
        selected = host.ipc.index["bindings"][0]
        self.assertEqual(selected["materials"], [])

    def test_inventory_restore_never_invokes_event_material_update_route(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            material_update_error=RuntimeError("material update rejected"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertIsNotNone(host.ipc.index)
        self.assertIsNone(result["index"]["warning"])
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("write_rdc_project_index", methods)
        self.assertNotIn("material-restore", [event["phase"] for event in progress])
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_verified_compiled_candidate_skips_compile_but_still_live_applies(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertTrue(result["index"]["written"])
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-candidate-load", phases)
        self.assertNotIn("shader-compile", phases)
        self.assertLess(
            phases.index("shader-candidate-load"), phases.index("shader-apply")
        )

    def test_same_hlsl_candidate_with_different_compile_profile_is_reimported(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            preexisting_binary_profile_mismatch=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(
            host.target_status["latestCompiled"]["projectImport"]["targetProfile"],
            host.target_status["targetProfile"],
        )

    def test_same_binary_with_different_compiler_flags_binding_is_reimported(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            preexisting_binary_compiler_mismatch=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(
            host.target_status["latestCompiled"]["projectImport"][
                "compilerBindingSha256"
            ],
            "d" * 64,
        )

    def test_binary_import_fallback_refuses_concurrently_changed_source(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            binary_import_error=RuntimeError("candidate import rejected"),
            binary_import_source_change="1" * 64,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn(
            "source changed before fallback compile",
            result["shaders"]["items"][0]["error"],
        )
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertIsNone(host.ipc.index)
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-candidate-load", phases)
        self.assertNotIn("shader-compile", phases)

    def test_stale_index_reinventories_and_replaces_only_after_success(self) -> None:
        self.state["knowledgeContentSha256"] = "4" * 64

        result, host, unused_replay, progress = self._run(auto_prepare=True)

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertIn("project-reuse-plan:invalid-or-stale-index", result["cache"]["reason"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertTrue(result["index"]["written"])
        selected = host.ipc.index["bindings"][0]
        self.assertEqual(selected["knowledgeContentSha256"], self.knowledge_sha)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)
        self.assertIn("index-write", phases)

    def test_inventory_publication_cas_conflict_preserves_old_rdc_receipts(
        self,
    ) -> None:
        original_index = copy.deepcopy(self.index)
        self.state["knowledgeContentSha256"] = "4" * 64

        result, host, unused_replay, progress = self._run(
            auto_prepare=True,
            inventory_register_error=RuntimeError(
                "shader inventory project binding is stale or changed"
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertFalse(result["index"]["written"])
        self.assertEqual(result["realizedReceipts"]["persisted"], 0)
        self.assertEqual(host.ipc.index, original_index)
        self.assertNotIn(
            "write_rdc_project_index",
            [method for method, unused in host.ipc.calls],
        )
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_reuse_compile_failure_never_overwrites_absent_index(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            compile_error=RuntimeError("compiler unavailable"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn("compiler unavailable", result["shaders"]["items"][0]["error"])
        self.assertFalse(result["index"]["written"])
        self.assertIsNone(host.ipc.index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-compile", phases)
        self.assertNotIn("index-write", phases)

    def test_project_pin_blocks_activation_during_compile_until_restore_finishes(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_compile=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.active_service.blocked_switches, 1)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_project_switch_during_reuse_apply_rolls_back_live_replacement(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_apply=True,
        )

        self.assertFalse(result["completed"])
        self.assertIn("active project changed", result["error"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_post_apply_rollback_failure_is_a_fatal_restore_error(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_apply=True,
            shader_rollback_error=RuntimeError("rollback route unavailable"),
        )

        self.assertFalse(result["completed"])
        self.assertEqual(result["errorType"], "RestoreRollbackFailed")
        self.assertIn("rollback failed", result["error"])
        self.assertIn("rollback route unavailable", result["error"])
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_inventory_restore_does_not_create_material_rollback_ledger(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            switch_project_after_material_success=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertNotIn("5" * 32, project_restore._MATERIAL_SYNC_PARAMETERS)
        self.assertIsNotNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_running_cancel_rolls_back_shader_without_material_ledger(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            cancel_on_progress_phase="index-write",
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreCancelled)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertEqual(host.active_service.pin_depth, 0)
        self.assertNotIn("5" * 32, project_restore._MATERIAL_SYNC_PARAMETERS)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_expired_server_deadline_stops_before_host_work(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            deadline_monotonic=time.monotonic() - 1.0,
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreDeadlineExceeded)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.ipc.calls, [])
        self.assertEqual(progress, [])

    def test_project_switch_during_cached_apply_rolls_back_and_never_prepares(self) -> None:
        original_index = copy.deepcopy(self.index)
        result, host, unused_replay, progress = self._run(
            switch_project_on_apply=True
        )

        self.assertFalse(result["completed"])
        self.assertIn("active project or capture changed", result["error"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.ipc.index, original_index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_cached_resource_index_rejects_more_than_256_without_ipc(self) -> None:
        shader_keys = []
        validated = []
        for number in range(257):
            resource_id = number + 1
            event_id = number + 10
            shader_key = "%d:fragment:main:%s" % (resource_id, self.raw_sha)
            shader_keys.append(shader_key)
            identity = {
                "resourceId": resource_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.raw_sha,
                "eventIds": [event_id],
            }
            status = copy.deepcopy(self.workspace_status)
            status["workspaceId"] = "%032x" % (number + 1)
            status["shader"]["resourceId"] = resource_id
            status["shader"]["shaderKey"] = shader_key
            validated.append(
                (
                    {"identity": identity},
                    self.workspace,
                    status,
                )
            )
        evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": list(range(10, 267)),
                "stages": ["fragment"],
            },
            "shaderKeys": shader_keys,
            "useTuples": [
                [number + 10, shader_keys[number]] for number in range(257)
            ],
        }
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index,
            evidence,
        )

        with self.assertRaises(project_restore.CatalogPreparationRequired) as raised:
            project_restore._match_workspace_resources(
                host,
                self.capture_path,
                self.session_id,
                validated,
            )

        self.assertEqual(
            raised.exception.reason_id, "shader-resource-index-shader-limit"
        )
        self.assertEqual(host.scope_calls, 0)
        self.assertEqual(host.projection_calls, 0)
        self.assertEqual(host.ipc.calls, [])

    def test_failed_live_anchor_preserves_index_without_full_prepare(self) -> None:
        original_index = copy.deepcopy(self.index)

        def change_expected_anchor(result):
            result["assets"][0]["anchorEventId"] = 10

        result, host, replay, progress = self._run(
            expected_result_mutator=change_expected_anchor,
            inventory_asset_mutator=lambda asset: asset.update(
                {"anchorEventId": 10}
            )
        )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertIn("cache-live-validation-failed", result["cache"]["reason"])
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(replay.snapshot_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 1)
        self.assertEqual(host.ipc.index, original_index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 2
        )
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 0
        )
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertFalse(begins[0]["inventory_all_shaders"])
        self.assertTrue(begins[1]["inventory_all_shaders"])
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)
        self.assertEqual(phases[-1], "complete")

    def test_live_edit_apply_has_no_implicit_project_sync(self) -> None:
        applied = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "applied": True,
            "replacement": {"replacementId": "1" * 32},
        }
        with mock.patch.object(
            server,
            "_apply_shader_workspace_shared_resource_with_live_abi_lease",
            return_value=copy.deepcopy(applied),
        ):
            result = server._shader_live_edit_apply_candidate(
                "managed-workspace", 4, "a" * 64
            )

        self.assertNotIn("projectSync", result)

    def test_material_update_has_no_implicit_project_sync(self) -> None:
        updated = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "operation": "update",
            "sessionId": "1" * 32,
            "updatedParameterId": "2" * 32,
            "applied": True,
        }

        class Manager:
            def update(self, session_id, parameter, value):
                return copy.deepcopy(updated)

        with mock.patch.object(
            server, "_material_session_manager", return_value=Manager()
        ):
            result = json.loads(
                server.update_material_parameter("1" * 32, "2" * 32, 0.5)
            )

        self.assertNotIn("projectSync", result)

    def test_shader_project_sync_requires_explicit_registration_tool(self) -> None:
        synced = {
            "schema": "replaylab-project-shader-sync/v1",
            "schemaVersion": 1,
            "projectCommitted": True,
        }
        with mock.patch.object(
            project_restore,
            "sync_active_shader_workspace_after_apply",
            return_value=copy.deepcopy(synced),
        ) as sync:
            result = json.loads(
                server.register_active_project_shader_replacement(
                    "managed-workspace", 4, "a" * 64, "1" * 32
                )
            )

        self.assertEqual(result, synced)
        sync.assert_called_once_with(
            "managed-workspace",
            expected_revision=4,
            expected_candidate_raw_sha256="a" * 64,
            replacement_id="1" * 32,
        )

    def test_material_project_sync_requires_explicit_registration_tool(self) -> None:
        synced = {
            "schema": "replaylab-project-material-sync/v1",
            "schemaVersion": 1,
            "projectCommitted": True,
        }
        with mock.patch.object(
            project_restore,
            "sync_active_material_session",
            return_value=copy.deepcopy(synced),
        ) as sync:
            result = json.loads(
                server.register_active_project_material_session(
                    "1" * 32, ["2" * 32]
                )
            )

        self.assertEqual(result, synced)
        sync.assert_called_once_with("1" * 32, ["2" * 32])

    def test_project_material_restore_uses_one_validated_batch_update(self) -> None:
        saved_a = "1" * 32
        saved_b = "2" * 32
        live_a = "3" * 32
        live_b = "4" * 32
        session_id = "5" * 32
        knowledge_id = "6" * 32
        layout_sha = "7" * 64
        knowledge = {
            "knowledgeId": knowledge_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": "",
            "interfaceComplete": True,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "constantBlockPath": "PerMaterial",
            "constantBlockLayoutSha256": layout_sha,
            "parameters": [
                {
                    "parameterId": saved_a,
                    "path": "PerMaterial.Roughness",
                    "baseType": "Float",
                    "components": 1,
                    "value": 0.25,
                },
                {
                    "parameterId": saved_b,
                    "path": "PerMaterial.Tint",
                    "baseType": "Float",
                    "components": 3,
                    "value": [0.1, 0.2, 0.3],
                },
            ],
        }
        identity = copy.deepcopy(self.identity)
        binding_state = {
            "projectId": self.project_id,
            "captureId": self.capture_id,
            "materials": {knowledge_id: knowledge},
        }
        material_record = {
            "knowledgeId": knowledge_id,
            "shaderResourceId": 7,
            "stage": "fragment",
            "path": "PerMaterial",
            "layoutSha256": layout_sha,
            "eventId": 9,
            "blockIndex": 0,
            "parameters": [
                {"parameterId": saved_a},
                {"parameterId": saved_b},
            ],
        }
        session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "sessionId": session_id,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "eventId": 9,
            "stage": "fragment",
            "shaderResourceId": 7,
            "constantBlockIndex": 0,
            "constantBlockPath": "PerMaterial",
            "layoutSha256": layout_sha,
            "parameters": [
                {
                    "parameterId": live_a,
                    "path": "PerMaterial.Roughness",
                    "baseType": "Float",
                    "components": 1,
                },
                {
                    "parameterId": live_b,
                    "path": "PerMaterial.Tint",
                    "baseType": "Float",
                    "components": 3,
                },
            ],
        }

        class Host:
            def __init__(self):
                self.batch_calls = []
                self.rollback_calls = 0

            @staticmethod
            def prepare_material_session(*unused_args):
                return json.dumps(session)

            def update_material_parameters(self, requested_session, updates):
                self.batch_calls.append((requested_session, copy.deepcopy(updates)))
                response = copy.deepcopy(session)
                response.update(
                    {
                        "operation": "update-many",
                        "updatedParameterIds": [live_a, live_b],
                        "updatedParameterCount": 2,
                        "applied": True,
                    }
                )
                return json.dumps(response)

            @staticmethod
            def update_material_parameter(*unused_args):
                raise AssertionError("project restore must not update leaves individually")

            def rollback_material_session(self, unused_session_id):
                self.rollback_calls += 1
                return json.dumps(
                    {
                        "schema": "renderdoc-ultra-material-session/v1",
                        "schemaVersion": 1,
                    }
                )

        host = Host()
        restored, unused_index = project_restore._restore_material(
            host,
            None,
            binding_state,
            self.active_workspace_id,
            identity,
            material_record,
            self.session_id,
        )

        self.assertEqual(restored["parameterCount"], 2)
        self.assertEqual(
            host.batch_calls,
            [
                (
                    session_id,
                    [
                        {"parameter": live_a, "value": 0.25},
                        {"parameter": live_b, "value": [0.1, 0.2, 0.3]},
                    ],
                )
            ],
        )
        self.assertEqual(host.rollback_calls, 0)

    def test_material_sync_accumulates_live_session_leaves_and_rdc_hint(self) -> None:
        parameter_a = "a" * 32
        parameter_b = "b" * 32
        material_session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "eventId": 9,
            "stage": "fragment",
            "shaderResourceId": 7,
            "constantBlockIndex": 0,
            "constantBlockPath": "cb0",
            "layoutSha256": self.layout_sha,
            "parameters": [
                {
                    "parameterId": parameter_a,
                    "path": "cb0.A",
                    "baseType": "Float",
                    "components": 1,
                    "value": 1.0,
                },
                {
                    "parameterId": parameter_b,
                    "path": "cb0.B",
                    "baseType": "Float",
                    "components": 1,
                    "value": 2.0,
                },
            ],
        }

        class Active:
            def __init__(self, owner):
                self.owner = owner
                self.saved = []
                self.durable_ids = set()

            def status(self):
                return copy.deepcopy(owner.active)

            def pin_active_project(self, unused_workspace_id, unused_project_root):
                return contextlib.nullcontext()

            def save_material_knowledge(self, session, parameter_ids):
                self.saved.append(list(parameter_ids))
                self.durable_ids.update(parameter_ids)
                return {
                    "knowledge": {
                        "knowledgeId": "%032x" % len(self.saved),
                        "parameters": [
                            copy.deepcopy(parameter)
                            for parameter in session["parameters"]
                            if parameter["parameterId"] in self.durable_ids
                        ],
                    }
                }

        owner = self
        active_service = Active(self)

        class Host:
            def get_material_session_status(self, session_id):
                self.last_session_id = session_id
                return json.dumps(copy.deepcopy(material_session))

            def _active_project_service(self):
                return active_service

            def _require_same_active_project(self, active):
                return copy.deepcopy(active)

            def _active_live_context(self, allow_missing_capture_id=False):
                return {
                    "capture": {
                        "path": str(owner.capture_path),
                        "captureSessionId": owner.session_id,
                    }
                }

        class Replay:
            def restore_knowledge_snapshot(self, capture_id, catalog_sha256):
                self.args = (capture_id, catalog_sha256)
                return {"projectId": owner.project_id}

        material_records = []

        def sync_index(
            unused_host,
            unused_active,
            unused_replay,
            unused_binding_state,
            unused_capture_path,
            unused_capture_session_id,
            **records,
        ):
            material_records.append(copy.deepcopy(records["material_record"]))
            return True, ""

        with mock.patch.object(project_restore, "_host_module", return_value=Host()), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=Replay()
        ), mock.patch.object(project_restore, "_sync_index_binding", side_effect=sync_index):
            first = project_restore.sync_active_material_session(
                self.session_id, [parameter_a]
            )
            # Host restart/eviction removes only the optimisation. The durable
            # project head returned by save remains complete and authoritative.
            project_restore._MATERIAL_SYNC_PARAMETERS.clear()
            second = project_restore.sync_active_material_session(
                self.session_id, [parameter_b]
            )

        self.assertEqual(first["parameterIds"], [parameter_a])
        self.assertEqual(second["parameterIds"], [parameter_a, parameter_b])
        self.assertEqual(
            active_service.saved,
            [[parameter_a], [parameter_b]],
        )
        self.assertEqual(
            material_records[-1]["parameters"],
            [{"parameterId": parameter_a}, {"parameterId": parameter_b}],
        )


if __name__ == "__main__":
    unittest.main()
