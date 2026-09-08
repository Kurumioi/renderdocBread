"""Preserve source assets while explicitly replacing their compile binding.

Historical reconstruction commands remain provenance, never an execution
requirement after source has been produced. Refresh imports no candidate or
replay authority and does not change the source document or workspace ID.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from .errors import ManifestValidationError, ToolchainRoutingError, ToolConfigurationError, ToolExecutionError
from .io import atomic_write_bytes

ORIGIN = "toolchain-refreshed-source"
SCHEMA = "renderdoc-ultra-shader-toolchain-refresh/v1"


def _manifest_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ManifestValidationError("toolchain manifest must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ManifestValidationError("toolchain manifest exceeds its size limit")
    return raw


def validate_refresh_source(service: Any, root: Path, manifest: Mapping[str, Any], source: Mapping[str, Any]) -> dict:
    from .workspace import _canonical_sha256, _source_path, _workspace_creation_identity, MAX_MANIFEST_BYTES
    receipt = source.get("toolchainRefresh")
    fields = {"schema", "schemaVersion", "workspaceId", "at", "revision", "fromRoutePlanSha256",
              "toRoutePlanSha256", "preservedSourceSha256", "previousManifestPath", "previousManifestSha256",
              "sourceProvenance", "reconstructionRoutePlan", "candidatesInvalidated", "recordSha256"}
    if not isinstance(receipt, Mapping) or set(receipt) != fields:
        raise ManifestValidationError("toolchain refresh source provenance is incomplete")
    unsigned = dict(receipt)
    digest = unsigned.pop("recordSha256")
    plan = manifest.get("routePlan")
    original_source = receipt.get("sourceProvenance")
    original_plan = receipt.get("reconstructionRoutePlan")
    if (
        receipt.get("schema") != SCHEMA or receipt.get("schemaVersion") != 1
        or receipt.get("workspaceId") != manifest.get("workspaceId")
        or receipt.get("candidatesInvalidated") is not True
        or not isinstance(receipt.get("at"), str)
        or type(receipt.get("revision")) is not int or receipt["revision"] < 0
        or not isinstance(plan, Mapping) or not isinstance(original_plan, Mapping)
        or not isinstance(original_source, Mapping) or original_source.get("origin") == ORIGIN
        or receipt.get("toRoutePlanSha256") != plan.get("planSha256")
        or source.get("routePlanSha256") != plan.get("planSha256")
        or source.get("sha256") != original_source.get("sha256")
        or source.get("path") != original_source.get("path")
        or source.get("projectSource") != original_source.get("projectSource")
        or digest != _canonical_sha256(unsigned)
    ):
        raise ManifestValidationError("toolchain refresh source binding is invalid")
    for key in ("fromRoutePlanSha256", "toRoutePlanSha256", "preservedSourceSha256", "previousManifestSha256"):
        value = receipt.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ManifestValidationError("toolchain refresh digest is invalid")
    declared_archive = root / str(receipt.get("previousManifestPath", ""))
    if any(part.is_symlink() for part in (declared_archive, declared_archive.parent, declared_archive.parent.parent)):
        raise ManifestValidationError("toolchain refresh archive must not be redirected")
    archive = service._artifact(root, receipt.get("previousManifestPath"))
    if archive.parent.parent != root / "toolchain-history" or archive.name != "manifest.json":
        raise ManifestValidationError("toolchain refresh archive path is invalid")
    raw = _manifest_bytes(archive, MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest() != receipt["previousManifestSha256"]:
        raise ManifestValidationError("toolchain refresh archive bytes changed")
    try:
        previous = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ManifestValidationError("toolchain refresh archive is not valid JSON") from exc
    if (
        not isinstance(previous, Mapping)
        or previous.get("workspaceId") != manifest.get("workspaceId")
        or _workspace_creation_identity(previous) != _workspace_creation_identity(manifest)
        or previous.get("revision") != receipt["revision"]
        or previous.get("routePlan", {}).get("planSha256") != receipt["fromRoutePlanSha256"]
    ):
        raise ManifestValidationError("toolchain refresh archive identity changed")
    historical = deepcopy(dict(manifest))
    historical["source"] = deepcopy(dict(original_source))
    historical["routePlan"] = deepcopy(dict(original_plan))
    service._validate_route_plan(historical, str(original_plan.get("inputFormat")), require_process_binding=False)
    service._source_for_toolchain_migration(root, historical)
    if service._artifact(root, source.get("path")) != _source_path(root, manifest).resolve():
        raise ManifestValidationError("toolchain refresh source path changed")
    return dict(receipt)


def refresh_toolchain(service: Any, root: Path, *, expected_plan: str, expected_source: str,
                      host_instance_id: str | None, recover_legacy: bool) -> dict:
    from .workspace import (_WorkspaceFileLock, _lock_for, _source_bytes, _canonical_sha256, MAX_MANIFEST_BYTES,
                            _relative, _utc_now, _write_manifest_with_retry)
    from .leases import WorkspaceSourceTransactionLease
    with WorkspaceSourceTransactionLease(root, owner_instance_id=host_instance_id or None,
                                         recover_orphaned=bool(host_instance_id), recover_legacy=recover_legacy), \
            _lock_for(root), _WorkspaceFileLock(root):
        manifest = service._load_manifest(root, allow_stale_route_bindings=True, validate_latest_candidate=False)
        old_plan = manifest.get("routePlan")
        if not isinstance(old_plan, Mapping) or old_plan.get("planSha256") != expected_plan:
            raise ToolchainRoutingError("source route plan changed before in-place toolchain refresh")
        path, source_bytes, original_record = service._source_for_toolchain_migration(root, manifest)
        if hashlib.sha256(source_bytes).hexdigest() != expected_source:
            raise ToolchainRoutingError("source bytes changed before in-place toolchain refresh")
        identity = service._route_upgrade_identity(old_plan)
        if not identity["compile"]:
            raise ToolchainRoutingError("in-place refresh requires an existing compile route")
        selection = {
            "inputFormat": identity["inputFormat"], "editableFormat": identity["editableFormat"],
            "stage": identity["stage"], "decompileRouteIds": [],
            "compileRouteIds": [hop["routeId"] for hop in identity["compile"]],
            "strictNativeIrValidationRequired": bool(old_plan.get("validators")), "mode": "compile-only-recovery",
        }
        plan = service._route_plan(service._catalog_for_create(None), selection, identity["inputFormat"], identity["stage"])
        new_identity = service._route_upgrade_identity(plan)
        if any(new_identity[key] != identity[key] for key in ("inputFormat", "editableFormat", "stage", "compile")):
            raise ToolchainRoutingError("in-place refresh changed the compile route or source interface")
        if plan["planSha256"] == old_plan["planSha256"]:
            raise ToolchainRoutingError("workspace compile binding is already current")
        # Keep only the first reconstruction provenance, so repeated compiler
        # upgrades do not create an unbounded recursive source-record chain.
        reconstruction = old_plan
        if original_record.get("origin") == ORIGIN:
            prior = validate_refresh_source(service, root, manifest, original_record)
            original_record = dict(prior["sourceProvenance"])
            reconstruction = prior["reconstructionRoutePlan"]
        history_root = root / "toolchain-history"
        if history_root.is_symlink() or (history_root.exists() and not history_root.is_dir()):
            raise ManifestValidationError("toolchain history must be an ordinary workspace directory")
        archive_dir = history_root / uuid.uuid4().hex
        archive_dir.mkdir(parents=True, exist_ok=False)
        archive = archive_dir / "manifest.json"
        old_bytes = _manifest_bytes(root / "manifest.json", MAX_MANIFEST_BYTES)
        if json.loads(old_bytes.decode("utf-8-sig")) != manifest:
            raise ToolchainRoutingError("workspace manifest changed before in-place toolchain refresh")
        atomic_write_bytes(archive, old_bytes)
        receipt = {
            "schema": SCHEMA, "schemaVersion": 1, "workspaceId": manifest["workspaceId"],
            "at": _utc_now(), "revision": manifest["revision"],
            "fromRoutePlanSha256": old_plan["planSha256"], "toRoutePlanSha256": plan["planSha256"],
            "preservedSourceSha256": expected_source, "previousManifestPath": _relative(root, archive),
            "previousManifestSha256": hashlib.sha256(old_bytes).hexdigest(),
            "sourceProvenance": deepcopy(original_record), "reconstructionRoutePlan": deepcopy(reconstruction),
            "candidatesInvalidated": True,
        }
        receipt["recordSha256"] = _canonical_sha256(receipt)
        updated = deepcopy(manifest)
        updated["routePlan"] = plan
        updated["recoveryPlan"] = None
        updated["source"] = {
            "path": original_record["path"], "sha256": original_record["sha256"], "encoding": "utf-8",
            "lane": "executable-editable", "origin": ORIGIN, "compileEligible": True,
            "semanticEquivalenceVerified": False, "routePlanSha256": plan["planSha256"],
            "baselineRawSha256": manifest["baseline"]["rawSha256"], "toolchainRefresh": receipt,
        }
        if "entryPoint" in original_record:
            updated["source"]["entryPoint"] = original_record["entryPoint"]
        if "projectSource" in original_record:
            # Preserve the independently validated project publication route
            # for the editor; a compiler refresh does not demand AI re-review.
            updated["source"]["projectSource"] = deepcopy(original_record["projectSource"])
        updated["latestCompiled"] = None
        updated.pop("lastAbiAttempt", None)
        updated.pop("dxilDisassemblyEvidence", None)
        updated["history"].append({"operation": "toolchain-refresh", "revision": manifest["revision"],
                                   "status": "source-preserved-candidates-invalidated", "refresh": receipt})
        updated["updatedAt"] = receipt["at"]
        service._validate_route_plan(updated, identity["inputFormat"], require_process_binding=True)
        validate_refresh_source(service, root, updated, updated["source"])
        service._validate_latest_candidate(root, updated, updated["revision"], service._manifest_baseline_abi(updated))
        if hashlib.sha256(_source_bytes(path)).hexdigest() != expected_source or (root / "manifest.json").read_bytes() != old_bytes:
            raise ToolchainRoutingError("workspace changed before in-place toolchain refresh commit")
        _write_manifest_with_retry(root / "manifest.json", updated)
    return {"schema": SCHEMA, "schemaVersion": 1, "inPlace": True, "workspacePath": str(root),
            "sourceWorkspacePath": str(root), "sourcePath": str(path), "workspaceId": manifest["workspaceId"],
            "sourceSha256": expected_source, "fromRoutePlanSha256": expected_plan, "routePlanSha256": plan["planSha256"],
            "sourcePreserved": True, "candidatesInvalidated": True, "replayAbiValidated": False,
            "semanticEquivalenceVerified": False, "applied": False, "status": service.status(root)}


def stale_toolchain_status(service: Any, root: Path, original_error: Exception) -> dict:
    """Read independently verified source when only execution tools are stale.

    A second validation verifies capture/catalog, baseline, route snapshot and
    source provenance without resolving historical commands. No old candidate
    becomes authoritative through this path, even if its files are present.
    """
    from .workspace import _editable_format, _reconstruction_fidelity_status
    from .flow_graph import load_workspace_flow_graph_status
    if isinstance(original_error, ToolConfigurationError):
        # A running service cannot silently adopt a rewritten process-owned
        # registry. The factory must construct a new immutable service first.
        raise original_error
    manifest = service._load_manifest(root, allow_stale_route_bindings=True, validate_latest_candidate=False)
    plan = manifest.get("routePlan")
    if not isinstance(plan, Mapping) or not isinstance(manifest.get("source"), Mapping):
        raise original_error
    # Do not mask a corrupt candidate or any unrelated manifest error when the
    # recorded execution route is still available and unchanged.
    try:
        service._validate_route_plan(manifest, str(plan["inputFormat"]), require_process_binding=True)
    except (ManifestValidationError, ToolConfigurationError, ToolExecutionError, ToolchainRoutingError):
        pass
    else:
        raise original_error
    path, data, record = service._source_for_toolchain_migration(root, manifest)
    digest = hashlib.sha256(data).hexdigest()
    source = {"present": True, "path": str(path), "format": _editable_format(manifest),
              "currentSha256": digest, "decompiledSha256": record["sha256"],
              "modifiedSinceDecompile": digest != record["sha256"], "lane": "executable-editable",
              "origin": record["origin"], "compileEligible": False, "baselineCompileVerified": False,
              "semanticEquivalenceVerified": False, "toolchainMigration": None, "hlslCBufferAbiRepair": None}
    return {
        "schema": "renderdoc-ultra-shader-workspace-status/v1", "workspacePath": str(root),
        "manifestPath": str(root / "manifest.json"), "workspaceId": manifest["workspaceId"],
        "capture": manifest["capture"], "shader": manifest["shader"], "stage": manifest["stage"],
        "entryPoint": manifest["entryPoint"], "targetProfile": manifest["targetProfile"], "routePlan": plan,
        "compilerBinding": None, "assemblyDisassemblyArtifacts": [], "recoveryPlan": manifest.get("recoveryPlan"),
        "revision": manifest["revision"], "source": source,
        "representation": {"present": True, "lane": "executable-editable", "kind": source["format"],
                           "path": str(path), "sha256": digest, "compileEligible": False, "applyEligible": False,
                           "abiCompatible": False, "semanticEquivalenceVerified": False,
                           "diagnostics": [str(original_error)], "provenance": {"sourceOrigin": record["origin"]}},
        "reconstructionFidelity": _reconstruction_fidelity_status(manifest, record, source, None),
        "flowGraph": load_workspace_flow_graph_status(root, workspace_id=manifest["workspaceId"],
            source_sha256=digest, source_revision=manifest["revision"], source_path=str(path.relative_to(root)).replace("\\", "/")),
        "baseline": manifest["baseline"], "lastGood": manifest["baseline"], "latestCompiled": None,
        "lastAbiAttempt": None, "abiValidationStatus": "not-compiled", "historyCount": len(manifest["history"]),
        "workspaceLocked": (root / ".workspace-operation.lock").exists(), "requiresReplayAbiValidation": True,
        "replayAbiValidated": False, "abiCompatible": False, "semanticEquivalenceVerified": False, "applied": False,
        "toolchainStatus": "refresh-required", "toolchainDiagnostic": str(original_error),
        "requiredAction": {"tool": "migrate_shader_workspace_toolchain", "in_place": True,
                           "source_workspace_path": str(root), "workspace_path": str(root),
                           "expected_route_plan_sha256": plan["planSha256"], "expected_source_sha256": digest},
    }
