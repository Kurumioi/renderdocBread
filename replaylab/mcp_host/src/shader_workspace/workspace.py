"""Deterministic external decompile/edit/compile workspace lifecycle."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from contextlib import ExitStack
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .abi import (
    ShaderAbi,
    build_candidate_reflection_request,
    parse_shader_abi,
    validate_candidate_reflection_response,
)
from .adapters.dxil_access_signature import (
    DxilAccessSignatureError,
    DxilCBufferAccessSignature,
    extract_dxil_cbuffer_access_signature,
)
from .binary_formats import (
    api_semantic_format,
    executable_format,
    fingerprint_shader_executable,
)
from .catalog import (
    ABI_STATUS_CAPTURED_COMPLETE,
    ABI_STATUS_DXIL_ACCESS_REQUIRED,
    ABI_STATUS_UNAVAILABLE,
    MAX_CATALOG_BYTES,
    ShaderKey,
    load_selected_shader,
)
from .errors import (
    AbiValidationError,
    CatalogValidationError,
    ManifestValidationError,
    ShaderWorkspaceError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
)
from .io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .leases import WorkspaceSourceTransactionLease
from .profiles import extract_target_profile
from .representations import (
    bounded_diagnostic,
    load_fallback_representation,
    publish_fallback_representation,
    publish_tool_explanatory_representation,
)
from .diagnostics import classify_shader_failure
from .tools import ToolOperation, ToolRegistry, run_operation
from .toolchains import (
    BINARY_FORMATS,
    TEXT_FORMATS,
    RouteCapability,
    ToolchainCatalog,
    detect_shader_format,
    normalize_format,
)

WORKSPACE_SCHEMA = "renderdoc-ultra-shader-workspace/v1"
WORKSPACE_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_COMPILED_BYTES = 512 * 1024 * 1024
MAX_ABI_REPORT_BYTES = 16 * 1024 * 1024
ABI_REPORT_SCHEMA = "renderdoc-ultra-shader-abi-report/v1"
ABI_REPORT_VERSION = 1
ROUTE_PLAN_SCHEMA = "renderdoc-ultra-shader-workspace-route-plan/v1"
ROUTE_PLAN_VERSION = 1
TOOLCHAIN_MIGRATION_SCHEMA = "renderdoc-ultra-shader-toolchain-migration/v1"
TOOLCHAIN_MIGRATION_VERSION = 1
LIVE_EDIT_AUTHORIZATION_MIGRATION_EVIDENCE_SCHEMA = (
    "renderdoc-ultra-shader-live-edit-authorization-migration-evidence/v1"
)
LIVE_EDIT_AUTHORIZATION_MIGRATION_EVIDENCE_VERSION = 1
INVALIDATED_CANDIDATE_SUMMARY_SCHEMA = (
    "renderdoc-ultra-invalidated-shader-candidate-summary/v1"
)
INVALIDATED_CANDIDATE_SUMMARY_VERSION = 1
DXIL_DISASSEMBLY_EVIDENCE_SCHEMA = (
    "renderdoc-ultra-shader-workspace-dxil-disassembly-evidence/v1"
)
RECONSTRUCTION_FIDELITY_SCHEMA = (
    "renderdoc-ultra-shader-reconstruction-fidelity/v1"
)
DXIL_DISASSEMBLY_EVIDENCE_VERSION = 1
DXIL_DISASSEMBLY_RECORD_SCHEMA = (
    "renderdoc-ultra-shader-workspace-dxil-disassembly-record/v1"
)
DXIL_DISASSEMBLY_RECORD_VERSION = 1
_DXIL_DISASSEMBLY_RECORD_FIELDS = frozenset(
    {
        "schema",
        "schemaVersion",
        "evidenceRole",
        "workspacePath",
        "workspaceId",
        "captureId",
        "revision",
        "routePlanSha256",
        "routeCatalogPath",
        "routeCatalogSha256",
        "toolRegistryPath",
        "toolRegistrySha256",
        "routeBinding",
        "operation",
        "toolId",
        "toolConfigDigest",
        "executableSha256",
        "fixedFileHashes",
        "command",
        "jobPath",
        "stdoutPath",
        "stdoutSha256",
        "stderrPath",
        "stderrSha256",
        "returnCode",
        "timedOut",
        "launchError",
        "stdoutTruncated",
        "stderrTruncated",
        "inputArtifactPath",
        "inputFormat",
        "inputPath",
        "inputSha256",
        "inputByteSize",
        "inputDetection",
        "outputFormat",
        "outputPath",
        "outputSha256",
        "outputByteSize",
        "outputDetection",
        "status",
    }
)
_FORMAT_SUFFIX = {
    "dxbc": ".dxbc",
    "dxil": ".dxil",
    "spirv": ".spv",
    "opengl-spirv": ".opengl.spv",
    "dxbc-asm": ".dxbc.txt",
    "dxil-asm": ".ll",
    "spirv-asm": ".spvasm",
    "opengl-spirv-asm": ".opengl.spvasm",
    "hlsl": ".hlsl",
    "glsl": ".glsl",
    "essl": ".essl",
    "msl": ".metal",
    "wgsl": ".wgsl",
    "slang": ".slang",
}
_EDITABLE_SOURCE_NAMES = {
    "hlsl": "shader.hlsl",
    "glsl": "shader.glsl",
    "essl": "shader.essl",
    "msl": "shader.metal",
    "wgsl": "shader.wgsl",
    "slang": "shader.slang",
    "dxbc-asm": "shader.dxbc.asm",
    "dxil-asm": "shader.dxil.ll",
    "spirv-asm": "shader.spvasm",
    "opengl-spirv-asm": "shader.opengl.spvasm",
}

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}

# Windows editors, indexers and real-time scanners can briefly retain a handle
# to a newly written directory or manifest.  Only the documented sharing/access
# failures are retried, and the complete wait remains bounded so a real ACL or
# provenance error is still reported promptly.
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})
_TRANSIENT_FILE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)


def _is_transient_windows_file_error(error: BaseException) -> bool:
    if os.name != "nt" or not isinstance(error, OSError):
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in _WINDOWS_TRANSIENT_FILE_ERRORS:
        return True
    return winerror is None and getattr(error, "errno", None) in {
        errno.EACCES,
        errno.EBUSY,
    }


def _replace_revision_directory(staging: Path, destination: Path) -> None:
    """Publish one new revision directory with bounded Windows retries."""

    for attempt in range(len(_TRANSIENT_FILE_RETRY_DELAYS) + 1):
        if destination.exists() or destination.is_symlink():
            raise ManifestValidationError(
                "revision artifact already exists: %s" % destination
            )
        try:
            staging.replace(destination)
            return
        except OSError as exc:
            if (
                not _is_transient_windows_file_error(exc)
                or attempt >= len(_TRANSIENT_FILE_RETRY_DELAYS)
            ):
                raise
            time.sleep(_TRANSIENT_FILE_RETRY_DELAYS[attempt])


def _write_manifest_with_retry(path: Path, manifest: Mapping[str, Any]) -> None:
    """Atomically write a manifest through the shared bounded retry layer."""

    # ``atomic_write_json`` owns the one bounded retry budget for destination
    # replacement.  Wrapping it in a second retry loop would multiply both the
    # attempts and latency when access is persistently denied.
    atomic_write_json(path, manifest)


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class _WorkspaceFileLock:
    """Cross-process fail-closed lock with exact token ownership.

    A crashed owner intentionally leaves the lock behind. This class never
    guesses that a lock is stale; a user must inspect/remove it explicitly.
    """

    def __init__(self, root: Path):
        self.path = root / ".workspace-operation.lock"
        self.token = uuid.uuid4().hex
        self._owned = False

    def __enter__(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise ManifestValidationError(
                "workspace is locked by another process; the lock is never "
                "assumed stale: %s" % self.path
            ) from exc
        try:
            payload = (self.token + "\n").encode("ascii")
            os.write(descriptor, payload)
            os.fsync(descriptor)
            self._owned = True
        finally:
            os.close(descriptor)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._owned:
            return False
        try:
            if self.path.read_text(encoding="ascii") == self.token + "\n":
                self.path.unlink()
        except OSError:
            # Losing/removing somebody else's lock would be less safe than
            # leaving our own token for explicit inspection.
            pass
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not is_within(root, resolved):
        raise ManifestValidationError("workspace artifact escapes the workspace")
    return resolved.relative_to(root).as_posix()


def _editable_format(manifest: Mapping[str, Any]) -> str:
    """Return the immutable routed text format; legacy workspaces are HLSL."""

    plan = manifest.get("routePlan")
    if plan is None:
        return "hlsl"
    if not isinstance(plan, Mapping):
        raise ManifestValidationError("workspace route plan is invalid")
    editable = normalize_format(plan.get("editableFormat"))
    if editable not in TEXT_FORMATS:
        raise ManifestValidationError("workspace editable format must be textual")
    return editable


def _source_path(root: Path, manifest: Mapping[str, Any]) -> Path:
    editable = _editable_format(manifest)
    name = _EDITABLE_SOURCE_NAMES.get(editable)
    if name is None:
        raise ManifestValidationError(
            "workspace editable format has no fixed source filename: %s" % editable
        )
    return root / "source" / name


def _revision_source_path(
    revision_directory: Path, manifest: Mapping[str, Any]
) -> Path:
    return revision_directory / _EDITABLE_SOURCE_NAMES[_editable_format(manifest)]


def _source_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise ToolExecutionError("shader source does not exist: %s" % path)
    size = path.stat().st_size
    if size <= 0:
        raise ToolExecutionError("shader source is empty")
    if size > MAX_SOURCE_BYTES:
        raise ToolExecutionError("shader source exceeds %d bytes" % MAX_SOURCE_BYTES)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("shader source is not valid UTF-8") from exc
    if "\x00" in text:
        raise ToolExecutionError("shader source contains NUL characters")
    return raw


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _catalog_file_sha256(catalog_path: str | Path) -> tuple[Path, str]:
    """Hash one stable, bounded, non-symlink capture catalog.

    Catalog contents are physical workspace provenance.  Hashing therefore
    rejects redirected files and checks the size both around the descriptor
    read and again through the original directory entry.  Callers hash once
    before and once after catalog selection to close the mutable-catalog race.
    """

    unresolved = Path(catalog_path).expanduser()
    try:
        entry_before = unresolved.lstat()
    except OSError as exc:
        raise CatalogValidationError(
            "capture catalog cannot be inspected: %s" % unresolved
        ) from exc
    if stat.S_ISLNK(entry_before.st_mode):
        raise CatalogValidationError("capture catalog must not be a symlink")
    if not stat.S_ISREG(entry_before.st_mode):
        raise CatalogValidationError("capture catalog is not a regular file")
    if entry_before.st_size > MAX_CATALOG_BYTES:
        raise CatalogValidationError(
            "capture catalog exceeds %d bytes" % MAX_CATALOG_BYTES
        )
    try:
        canonical = unresolved.resolve(strict=True)
        digest = hashlib.sha256()
        total = 0
        with canonical.open("rb") as stream:
            descriptor_before = os.fstat(stream.fileno())
            if not stat.S_ISREG(descriptor_before.st_mode):
                raise CatalogValidationError(
                    "capture catalog is not a regular file"
                )
            if descriptor_before.st_size > MAX_CATALOG_BYTES:
                raise CatalogValidationError(
                    "capture catalog exceeds %d bytes" % MAX_CATALOG_BYTES
                )
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_CATALOG_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CATALOG_BYTES:
                    raise CatalogValidationError(
                        "capture catalog exceeds %d bytes" % MAX_CATALOG_BYTES
                    )
                digest.update(chunk)
            descriptor_after = os.fstat(stream.fileno())
        entry_after = unresolved.lstat()
    except CatalogValidationError:
        raise
    except OSError as exc:
        raise CatalogValidationError(
            "capture catalog cannot be read: %s" % unresolved
        ) from exc
    if (
        stat.S_ISLNK(entry_after.st_mode)
        or not stat.S_ISREG(entry_after.st_mode)
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_after.st_size != total
        or entry_before.st_size != entry_after.st_size
        or entry_after.st_size != total
    ):
        raise CatalogValidationError("capture catalog size changed while hashing")
    return canonical, digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _physical_target_profile(
    blob: bytes,
    stage: str,
    shader_format: str,
    capture_api: str,
) -> dict[str, Any]:
    """Return exact DirectX profile data or a capture-bound SPIR-V descriptor."""

    if shader_format in {"dxbc", "dxil"}:
        return extract_target_profile(blob, stage).as_dict()
    if shader_format not in {"spirv", "opengl-spirv"}:
        raise ToolchainRoutingError(
            "no physical target-profile adapter is registered for %s" % shader_format
        )
    semantic = api_semantic_format(capture_api, shader_format)
    if semantic != shader_format:
        raise ToolchainRoutingError(
            "SPIR-V semantic environment contradicts the capture API"
        )
    api_token = str(capture_api or "").strip().casefold().replace(" ", "")
    if api_token.startswith("graphicsapi."):
        api_token = api_token[len("graphicsapi.") :]
    api_token = {"vk": "vulkan", "gl": "opengl"}.get(api_token, api_token)
    expected_api = "opengl" if shader_format == "opengl-spirv" else "vulkan"
    if api_token != expected_api:
        raise ToolchainRoutingError(
            "SPIR-V workspace requires capture-declared %s environment" % expected_api
        )
    return {
        "profile": shader_format,
        "stage": stage,
        "major": None,
        "minor": None,
        "programKind": None,
        "programKindName": stage,
        "containerFormat": shader_format,
        "apiEnvironment": expected_api,
        "profileSource": "capture-api-plus-validated-spirv-module",
    }


def _workspace_creation_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the immutable physical identity shared by recovery attempts."""

    capture = manifest.get("capture")
    shader = manifest.get("shader")
    baseline = manifest.get("baseline")
    target_profile = manifest.get("targetProfile")
    if not all(
        isinstance(value, Mapping)
        for value in (capture, shader, baseline, target_profile)
    ):
        raise ManifestValidationError("workspace creation identity is incomplete")
    return {
        "catalogPath": capture.get("catalogPath"),
        "catalogSha256": capture.get("catalogSha256"),
        "captureId": capture.get("captureId"),
        "capturePath": capture.get("capturePath"),
        "api": capture.get("api"),
        "shader": dict(shader),
        "stage": manifest.get("stage"),
        "entryPoint": manifest.get("entryPoint"),
        "targetProfile": dict(target_profile),
        "baseline": {
            "rawSha256": baseline.get("rawSha256"),
            "codeSha256": baseline.get("codeSha256"),
            "byteSize": baseline.get("byteSize"),
            "kind": baseline.get("kind"),
            "abiStatus": baseline.get("abiStatus"),
            "abiSha256": baseline.get("abiSha256"),
        },
    }


def _operation_binding(operation: ToolOperation) -> dict[str, Any]:
    """Return the complete immutable operation identity used by a route hop."""

    return {
        "toolId": operation.tool_id,
        "operation": operation.operation,
        "configDigest": operation.config_digest,
        "executableSha256": operation.executable_sha256,
        "fixedFileHashes": operation.fixed_file_hashes_dict(),
        "placeholders": sorted(operation.placeholders),
        "timeoutSeconds": operation.timeout_seconds,
    }


def _initial_abi_gate_status(manifest: Mapping[str, Any]) -> str:
    capture = manifest.get("capture")
    api = (
        str(capture.get("api", ""))
        if isinstance(capture, Mapping)
        else ""
    ).strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    if api not in {"d3d11", "direct3d11", "d3d12", "direct3d12"}:
        return "blocked-apply-backend-unsupported"
    baseline = manifest.get("baseline")
    if not isinstance(baseline, Mapping) or baseline.get("abiStatus") not in {
        ABI_STATUS_CAPTURED_COMPLETE,
        ABI_STATUS_DXIL_ACCESS_REQUIRED,
    }:
        return "blocked-baseline-interface-unavailable"
    interface = baseline.get("compiledInterface")
    if (
        isinstance(interface, Mapping)
        and interface.get("dynamicLinkageInterfaceCount") != 0
    ):
        return "blocked-d3d11-dynamic-linkage"
    return "pending-replay-reflection"


def _reconstruction_fidelity_status(
    manifest: Mapping[str, Any],
    source_record: Mapping[str, Any] | None,
    source_result: Mapping[str, Any],
    latest_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify the untouched route round trip against captured executable code.

    This is deliberately narrower than semantic equivalence. ``codeSha256``
    compares executable container chunks, while a fresh replay ABI result only
    proves that one current candidate has the same physical interface. The two
    facts stay separate so ABI success cannot be mistaken for recovery of the
    captured program.
    """

    baseline = manifest.get("baseline")
    captured_code_sha = (
        baseline.get("codeSha256") if isinstance(baseline, Mapping) else None
    )
    baseline_compile = (
        source_record.get("baselineCompile")
        if isinstance(source_record, Mapping)
        and source_record.get("origin") == "trusted-decompile-route"
        else None
    )
    available = bool(
        source_result.get("baselineCompileVerified") is True
        and isinstance(baseline_compile, Mapping)
    )
    if not available:
        return {
            "schema": RECONSTRUCTION_FIDELITY_SCHEMA,
            "scope": "captured-baseline-vs-untouched-route-round-trip",
            "available": False,
            "baselineCompileVerified": False,
            "capturedCodeSha256": captured_code_sha,
            "reconstructedCodeSha256": None,
            "codeSha256Equal": None,
            "currentSourceMatchesUntouchedReconstruction": None,
            "currentCandidateMatchesUntouchedReconstruction": None,
            "currentCandidateReplayAbiCompatible": None,
            "classification": "not-measured",
            "semanticEquivalenceVerified": False,
            "note": (
                "No verified untouched route round-trip artifact is available; "
                "raw container identity is not used as a substitute."
            ),
        }

    reconstructed_code_sha = baseline_compile.get("codeSha256")
    code_equal = reconstructed_code_sha == captured_code_sha
    untouched_source_sha = source_result.get("decompiledSha256")
    current_source_matches = bool(
        untouched_source_sha
        and source_result.get("currentSha256") == untouched_source_sha
    )
    current_candidate_matches = bool(
        current_source_matches
        and isinstance(latest_result, Mapping)
        and latest_result.get("sourceSha256") == untouched_source_sha
        and latest_result.get("codeSha256") == reconstructed_code_sha
    )
    abi_gate = (
        latest_result.get("abiGate")
        if isinstance(latest_result, Mapping)
        else None
    )
    abi_gate_binding_valid = bool(
        isinstance(abi_gate, Mapping)
        and abi_gate.get("status") == "compatible"
        and all(
            isinstance(abi_gate.get(key), str)
            and len(abi_gate.get(key)) == 64
            and all(
                character in "0123456789abcdef"
                for character in abi_gate.get(key)
            )
            for key in ("requestId", "reportSha256")
        )
    )
    current_candidate_abi_compatible = bool(
        current_candidate_matches
        and latest_result.get("requiresReplayAbiValidation") is False
        and latest_result.get("replayAbiValidated") is True
        and latest_result.get("abiCompatible") is True
        and abi_gate_binding_valid
    )
    if code_equal:
        classification = "exact-executable-code-match"
    elif current_candidate_abi_compatible:
        classification = "abi-compatible-executable-code-mismatch"
    else:
        classification = "executable-code-mismatch"

    return {
        "schema": RECONSTRUCTION_FIDELITY_SCHEMA,
        "scope": "captured-baseline-vs-untouched-route-round-trip",
        "available": True,
        "baselineCompileVerified": True,
        "capturedCodeSha256": captured_code_sha,
        "reconstructedCodeSha256": reconstructed_code_sha,
        "codeSha256Equal": code_equal,
        "currentSourceMatchesUntouchedReconstruction": current_source_matches,
        "currentCandidateMatchesUntouchedReconstruction": current_candidate_matches,
        "currentCandidateReplayAbiCompatible": current_candidate_abi_compatible,
        "classification": classification,
        "semanticEquivalenceVerified": False,
        "note": (
            "Executable code identity is measured against the captured baseline. "
            "Fresh ABI compatibility is reported independently and never proves "
            "semantic equivalence; raw container SHA-256 is intentionally ignored."
        ),
    }


class ShaderWorkspaceService:
    """Create and operate isolated workspaces using a fixed local tool registry."""

    def __init__(
        self,
        tool_config_path: str | Path,
        route_config_path: str | Path | None = None,
    ):
        self.registry = ToolRegistry(tool_config_path)
        self.route_config_path = (
            Path(route_config_path).expanduser().resolve()
            if route_config_path is not None
            else None
        )
        self._process_catalog_lock = threading.RLock()
        self._process_catalog_cache: ToolchainCatalog | None = None
        self._dxil_access_cache_lock = threading.RLock()
        self._dxil_access_signature_cache: OrderedDict[
            str, DxilCBufferAccessSignature | None
        ] = OrderedDict()
        self._tool_config_sha256 = _file_sha256(self.registry.path)
        self._route_config_sha256 = (
            _file_sha256(self.route_config_path)
            if self.route_config_path is not None
            else None
        )

    def _process_catalog(self) -> ToolchainCatalog:
        """Return one immutable catalog for this exact service/config snapshot.

        A workspace status used to construct ``ToolchainCatalog`` repeatedly;
        that also rebuilt ``ToolRegistry`` and re-hashed every large compiler
        binary and companion DLL. The two small config files are still hashed
        on every lookup. Any change fails this service closed, while the server
        factory can create a new fully verified service for the new snapshot.
        Tool launches retain their own immediate executable/fixed-file hashes.
        """

        if self.route_config_path is None:
            raise ToolchainRoutingError(
                "automatic workspace routing requires the process-owned "
                "shader toolchain catalog"
            )
        with self._process_catalog_lock:
            if _file_sha256(self.registry.path) != self._tool_config_sha256:
                raise ToolConfigurationError(
                    "shader tool registry bytes changed after service creation"
                )
            if _file_sha256(self.route_config_path) != self._route_config_sha256:
                raise ToolConfigurationError(
                    "shader route catalog bytes changed after service creation"
                )
            if self._process_catalog_cache is None:
                catalog = ToolchainCatalog(
                    self.registry.path,
                    self.route_config_path,
                    registry=self.registry,
                )
                if (
                    _file_sha256(self.registry.path) != self._tool_config_sha256
                    or _file_sha256(self.route_config_path)
                    != self._route_config_sha256
                ):
                    raise ToolConfigurationError(
                        "shader tool configuration changed while loading catalog"
                    )
                self._process_catalog_cache = catalog
            return self._process_catalog_cache

    def _catalog_for_create(
        self, supplied: ToolchainCatalog | None
    ) -> ToolchainCatalog:
        catalog = supplied
        if catalog is None:
            if self.route_config_path is None:
                raise ToolchainRoutingError(
                    "automatic workspace routing requires the process-owned "
                    "shader toolchain catalog"
                )
            catalog = self._process_catalog()
        if catalog.registry.path != self.registry.path:
            raise ToolConfigurationError(
                "toolchain catalog is not bound to this workspace tool registry"
            )
        if (
            self.route_config_path is not None
            and catalog.path != self.route_config_path
        ):
            raise ToolConfigurationError(
                "toolchain catalog path differs from the process-owned route catalog"
            )
        return catalog

    @staticmethod
    def _routes_from_selection(
        catalog: ToolchainCatalog,
        selection: Mapping[str, Any],
        direction: str,
        *,
        allow_empty: bool = False,
    ) -> tuple[RouteCapability, ...]:
        field = "%sRouteIds" % direction
        values = selection.get(field)
        if (
            not isinstance(values, list)
            or (not values and not allow_empty)
            or len(values) > 4
        ):
            raise ToolchainRoutingError(
                "toolchain selection %s must contain %s route IDs"
                % (field, "0..4" if allow_empty else "1..4")
            )
        by_id = {route.route_id: route for route in catalog.routes()}
        result = []
        for value in values:
            if not isinstance(value, str) or value not in by_id:
                raise ToolchainRoutingError(
                    "toolchain selection references an unknown route ID"
                )
            result.append(by_id[value])
        return tuple(result)

    def _route_plan(
        self,
        catalog: ToolchainCatalog,
        selection: Mapping[str, Any],
        original_format: str,
        stage: str,
    ) -> dict[str, Any]:
        if not isinstance(selection, Mapping):
            raise ToolchainRoutingError("toolchain selection must be an object")
        editable = normalize_format(selection.get("editableFormat"))
        if editable not in TEXT_FORMATS or editable not in _EDITABLE_SOURCE_NAMES:
            raise ToolchainRoutingError(
                "ShaderWorkspace has no fixed editable source path for %s" % editable
            )
        if normalize_format(selection.get("inputFormat")) != original_format:
            raise ToolchainRoutingError(
                "toolchain selection input format contradicts the captured shader"
            )
        if str(selection.get("stage", "")) != stage:
            raise ToolchainRoutingError(
                "toolchain selection stage contradicts the captured shader"
            )
        mode = str(selection.get("mode", "") or "").strip()
        forward = self._routes_from_selection(
            catalog, selection, "decompile", allow_empty=True
        )
        reverse = self._routes_from_selection(
            catalog, selection, "compile", allow_empty=True
        )
        inferred_mode = (
            "round-trip"
            if forward and reverse
            else "compile-only-recovery"
            if reverse
            else "evidence-only"
        )
        if mode and mode != inferred_mode:
            raise ToolchainRoutingError(
                "toolchain recovery mode contradicts its bound route paths"
            )
        def bind_path(
            routes: Sequence[RouteCapability], source: str, target: str
        ) -> list[dict[str, Any]]:
            if not routes:
                return []
            current = source
            records = []
            for index, route in enumerate(routes):
                if route.input_format != current:
                    raise ToolchainRoutingError(
                        "toolchain selection route path is not contiguous"
                    )
                if not route.supports_stage(stage):
                    raise ToolchainRoutingError(
                        "toolchain selection route does not support the shader stage"
                    )
                operation = catalog.registry.operation(
                    route.tool_id, route.operation
                )
                integrity = catalog.registry.operation_integrity(
                    route.tool_id, route.operation
                )
                if not integrity.get("available"):
                    raise ToolExecutionError(
                        "tool integrity failed while binding route %s: %s"
                        % (route.route_id, integrity.get("integrityError"))
                    )
                records.append(
                    {
                        "hop": index + 1,
                        "capability": route.as_dict(),
                        "operation": _operation_binding(operation),
                    }
                )
                current = route.output_format
            if current != target:
                raise ToolchainRoutingError(
                    "toolchain selection does not reach its declared target format"
                )
            return records

        plan = {
            "schema": ROUTE_PLAN_SCHEMA,
            "schemaVersion": ROUTE_PLAN_VERSION,
            "routeCatalogPath": str(catalog.path),
            "routeCatalogSha256": _file_sha256(catalog.path),
            "toolRegistryPath": str(catalog.registry.path),
            "toolRegistrySha256": catalog.tool_registry_sha256,
            "inputFormat": original_format,
            "editableFormat": editable,
            "stage": stage,
            "decompile": bind_path(forward, original_format, editable),
            "compile": bind_path(reverse, editable, original_format),
        }
        plan["planSha256"] = _canonical_sha256(plan)
        return plan

    def create(
        self,
        catalog_path: str | Path,
        shader_key: ShaderKey | Mapping[str, Any] | str,
        workspace_path: str | Path,
        decompiler_tool_id: str | None = None,
        compiler_tool_id: str | None = None,
        *,
        toolchain_catalog: ToolchainCatalog | None = None,
        toolchain_selection: Mapping[str, Any] | None = None,
        editable_format: str = "",
        allow_reconstructed: bool = True,
        _expected_creation_identity: Mapping[str, Any] | None = None,
    ) -> dict:
        """Create a new workspace from one physically verified catalog blob."""

        canonical_catalog, catalog_sha256 = _catalog_file_sha256(catalog_path)
        selected = load_selected_shader(canonical_catalog, shader_key)
        canonical_catalog_after, catalog_sha256_after = _catalog_file_sha256(
            canonical_catalog
        )
        if (
            canonical_catalog_after != canonical_catalog
            or catalog_sha256_after != catalog_sha256
        ):
            raise CatalogValidationError(
                "capture catalog changed while selecting the Shader"
            )
        original_format = selected.encoding
        target_profile = _physical_target_profile(
            selected.blob,
            selected.key.stage,
            original_format,
            selected.api,
        )
        automatic_editable_selection = not bool(str(editable_format or "").strip())
        requested_editable_format = normalize_format(
            editable_format
            or ("hlsl" if original_format in {"dxbc", "dxil"} else "glsl")
        )
        if requested_editable_format not in TEXT_FORMATS:
            raise ToolchainRoutingError("workspace editable format must be textual")
        legacy_requested = bool(decompiler_tool_id or compiler_tool_id)
        if legacy_requested and not (decompiler_tool_id and compiler_tool_id):
            raise ToolConfigurationError(
                "legacy workspace creation requires both decompiler and compiler IDs"
            )
        if legacy_requested and (
            toolchain_catalog is not None or toolchain_selection is not None
        ):
            raise ToolConfigurationError(
                "legacy tool IDs and a toolchain selection are mutually exclusive"
            )
        if legacy_requested and requested_editable_format != "hlsl":
            raise ToolConfigurationError(
                "legacy single-tool workspaces are HLSL-only; use a routed "
                "toolchain for instruction assembly"
            )
        route_plan = None
        recovery_plan: dict[str, Any] | None = None
        decompiler = None
        compiler = None
        if legacy_requested:
            decompiler = self.registry.operation(
                str(decompiler_tool_id), "decompile"
            )
            compiler = self.registry.operation(str(compiler_tool_id), "compile")
            if "target_profile" not in compiler.placeholders:
                raise ToolConfigurationError(
                    "workspace HLSL compiler must consume {target_profile}; fixed or "
                    "stage-guessed profiles can change the captured shader ABI"
                )
        else:
            catalog = self._catalog_for_create(toolchain_catalog)
            if toolchain_selection is None:
                if automatic_editable_selection:
                    selection = catalog.plan_automatic_recovery(
                        original_format,
                        selected.key.stage,
                        capture_encoding=original_format,
                        target_api=selected.api,
                        allow_reconstructed=allow_reconstructed,
                    )
                else:
                    selection = catalog.plan_recovery(
                        original_format,
                        selected.key.stage,
                        editable_format=requested_editable_format,
                        capture_encoding=original_format,
                        target_api=selected.api,
                        allow_reconstructed=allow_reconstructed,
                    )
            else:
                selection = toolchain_selection
            route_plan = self._route_plan(
                catalog, selection, original_format, selected.key.stage
            )
            selection_mode = (
                "round-trip"
                if route_plan["decompile"] and route_plan["compile"]
                else "compile-only-recovery"
                if route_plan["compile"]
                else "evidence-only"
            )
            recovery_plan = {
                "schema": "renderdoc-ultra-shader-workspace-recovery/v1",
                "schemaVersion": 1,
                "mode": selection_mode,
                "diagnostics": list(selection.get("diagnostics", [])),
                "allowedActions": list(selection.get("allowedActions", [])),
                "decompileCandidates": list(
                    selection.get("decompileCandidates", [])
                ),
                "compileCandidates": list(selection.get("compileCandidates", [])),
                "configuredButUnavailable": dict(
                    selection.get("configuredButUnavailable", {})
                ),
                "aiPolicy": dict(selection.get("aiPolicy", {})),
                "sourceConstructionEligible": bool(route_plan["compile"]),
                "assetSourceSeedEligibleAfterVerification": bool(
                    route_plan["compile"]
                ),
                "directReplayApplyAuthorized": False,
                "semanticEquivalenceVerified": False,
            }
            recovery_plan["planSha256"] = _canonical_sha256(recovery_plan)
        target = Path(workspace_path).expanduser().resolve()
        if target.exists():
            raise ShaderWorkspaceError("workspace path already exists: %s" % target)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=".%s.staging-" % target.name, dir=str(target.parent)
            )
        )
        try:
            (staging / "baseline").mkdir()
            (staging / "source").mkdir()
            (staging / "jobs").mkdir()
            (staging / "revisions").mkdir()
            baseline_path = staging / "baseline" / "shader.bin"
            atomic_write_bytes(baseline_path, selected.blob)
            now = _utc_now()
            baseline = {
                "path": "baseline/shader.bin",
                "rawSha256": selected.container.raw_sha256,
                "codeSha256": selected.container.code_sha256,
                "byteSize": selected.container.byte_size,
                "kind": selected.container.kind,
                "revision": 0,
                "validationScope": "captured-baseline",
                "requiresReplayAbiValidation": False,
                "abiStatus": selected.abi_status,
                "abiSha256": selected.abi.sha256 if selected.abi is not None else None,
                "compiledInterface": (
                    selected.abi.to_dict() if selected.abi is not None else None
                ),
            }
            manifest = {
                "schema": WORKSPACE_SCHEMA,
                "schemaVersion": WORKSPACE_VERSION,
                "workspaceId": uuid.uuid4().hex,
                "createdAt": now,
                "updatedAt": now,
                "capture": {
                    "captureId": selected.capture_id,
                    "capturePath": selected.capture_path,
                    "api": selected.api,
                    "catalogPath": str(selected.catalog_path),
                    "catalogSha256": catalog_sha256,
                },
                "shader": selected.key.as_dict(),
                "stage": selected.key.stage,
                "entryPoint": selected.key.entry_point,
                "targetProfile": target_profile,
                "baseline": baseline,
                "tools": (
                    {
                        "decompiler": {
                            "toolId": decompiler.tool_id,
                            "configDigest": decompiler.config_digest,
                            "executableSha256": decompiler.executable_sha256,
                            "fixedFileHashes": decompiler.fixed_file_hashes_dict(),
                        },
                        "compiler": {
                            "toolId": compiler.tool_id,
                            "configDigest": compiler.config_digest,
                            "executableSha256": compiler.executable_sha256,
                            "fixedFileHashes": compiler.fixed_file_hashes_dict(),
                        },
                    }
                    if route_plan is None
                    else {}
                ),
                "routePlan": route_plan,
                "recoveryPlan": recovery_plan,
                "revision": 0,
                # lastGood is deliberately the captured baseline until a future
                # replay/ABI validation API explicitly promotes a candidate.
                "lastGood": dict(baseline),
                "latestCompiled": None,
                "source": None,
                "history": [],
            }
            if (
                _expected_creation_identity is not None
                and _workspace_creation_identity(manifest)
                != dict(_expected_creation_identity)
            ):
                raise ToolchainRoutingError(
                    "recovery attempt resolved a different capture/shader identity"
                )
            atomic_write_json(staging / "manifest.json", manifest)
            # Validate the complete staged workspace, including a fresh catalog
            # provenance read, before its directory can become visible. This
            # makes a catalog swap fail without publishing a target workspace.
            staged_manifest = self._load_manifest(staging)
            if (
                _expected_creation_identity is not None
                and _workspace_creation_identity(staged_manifest)
                != dict(_expected_creation_identity)
            ):
                raise ToolchainRoutingError(
                    "recovery attempt staged a different capture/shader identity"
                )
            staging.replace(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {
            "workspacePath": str(target),
            "manifestPath": str(target / "manifest.json"),
            "workspaceId": manifest["workspaceId"],
            "captureId": selected.capture_id,
            "catalogSha256": catalog_sha256,
            "shader": selected.key.as_dict(),
            "baselineRawSha256": selected.container.raw_sha256,
            "baselineKind": selected.container.kind,
            "baselineAbiStatus": baseline["abiStatus"],
            "baselineAbiSha256": baseline["abiSha256"],
            "targetProfile": target_profile,
            "routePlan": route_plan,
            "recoveryPlan": recovery_plan,
            "revision": 0,
            "lastGoodRevision": 0,
            "requiresReplayAbiValidation": False,
        }

    def create_recovery_attempt(
        self,
        source_workspace_path: str | Path,
        workspace_path: str | Path,
        *,
        expected_recovery_plan_sha256: str,
        expected_source_workspace_id: str,
        expected_baseline_raw_sha256: str,
        expected_capture_id: str,
        decompile_candidate_index: int | None,
        compile_candidate_index: int = 0,
    ) -> dict:
        """Create a new workspace bound to one finite recovery-plan candidate.

        Candidate indices refer only to the immutable, program-generated lists
        in the source workspace. No route ID, command, executable, argument, or
        environment value can be supplied by the caller. A new workspace keeps
        failed attempts and their provenance isolated.
        """

        if decompile_candidate_index is not None and (
            isinstance(decompile_candidate_index, bool)
            or not isinstance(decompile_candidate_index, int)
            or decompile_candidate_index < 0
        ):
            raise ToolchainRoutingError(
                "decompile_candidate_index must be null or a non-negative integer"
            )
        if (
            isinstance(compile_candidate_index, bool)
            or not isinstance(compile_candidate_index, int)
            or compile_candidate_index < 0
        ):
            raise ToolchainRoutingError(
                "compile_candidate_index must be a non-negative integer"
            )
        source_root = self._workspace_root(source_workspace_path)
        expected_plan_hash = str(expected_recovery_plan_sha256 or "").lower()
        if len(expected_plan_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_plan_hash
        ):
            raise ToolchainRoutingError(
                "expected_recovery_plan_sha256 must be a lowercase SHA-256"
            )
        source_workspace_id = str(expected_source_workspace_id or "").lower()
        if len(source_workspace_id) != 32 or any(
            character not in "0123456789abcdef" for character in source_workspace_id
        ):
            raise ToolchainRoutingError(
                "expected_source_workspace_id must be a lowercase 32-hex identity"
            )
        expected_baseline_hash = str(expected_baseline_raw_sha256 or "").lower()
        expected_capture_hash = str(expected_capture_id or "").lower()
        for label, digest in (
            ("expected_baseline_raw_sha256", expected_baseline_hash),
            ("expected_capture_id", expected_capture_hash),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ToolchainRoutingError("%s must be a lowercase SHA-256" % label)
        target = Path(workspace_path).expanduser().resolve()
        if target == source_root:
            raise ShaderWorkspaceError("a recovery attempt requires a new workspace")
        if is_within(source_root, target):
            raise ShaderWorkspaceError(
                "a recovery attempt workspace must stay outside the failed source "
                "workspace"
            )
        with _lock_for(source_root), _WorkspaceFileLock(source_root):
            source_manifest = self._load_manifest(source_root)
            route_plan = source_manifest.get("routePlan")
            recovery_plan = source_manifest.get("recoveryPlan")
            if not isinstance(route_plan, Mapping) or not isinstance(
                recovery_plan, Mapping
            ):
                raise ToolchainRoutingError(
                    "source workspace has no program-generated recovery plan"
                )
            if recovery_plan.get("planSha256") != expected_plan_hash:
                raise ToolchainRoutingError(
                    "source recovery plan changed before candidate selection"
                )
            if (
                source_manifest.get("workspaceId") != source_workspace_id
                or source_manifest.get("baseline", {}).get("rawSha256")
                != expected_baseline_hash
                or source_manifest.get("capture", {}).get("captureId")
                != expected_capture_hash
            ):
                raise ToolchainRoutingError(
                    "source workspace/capture/baseline identity changed before selection"
                )
            decompile_candidates = recovery_plan.get("decompileCandidates")
            compile_candidates = recovery_plan.get("compileCandidates")
            if not isinstance(decompile_candidates, list):
                raise ToolchainRoutingError("decompile recovery candidates are invalid")
            if decompile_candidates:
                if (
                    decompile_candidate_index is None
                    or decompile_candidate_index >= len(decompile_candidates)
                ):
                    raise ToolchainRoutingError(
                        "decompile candidate index is outside the fixed recovery plan"
                    )
                selected_decompile = list(
                    decompile_candidates[decompile_candidate_index]
                )
            else:
                if decompile_candidate_index is not None:
                    raise ToolchainRoutingError(
                        "this recovery plan has no decompile candidate"
                    )
                selected_decompile = []
            if not isinstance(compile_candidates, list):
                raise ToolchainRoutingError("compile recovery candidates are invalid")
            if compile_candidates:
                if compile_candidate_index >= len(compile_candidates):
                    raise ToolchainRoutingError(
                        "compile candidate index is outside the fixed recovery plan"
                    )
                selected_compile = list(compile_candidates[compile_candidate_index])
            else:
                if compile_candidate_index != 0:
                    raise ToolchainRoutingError(
                        "this recovery plan has no compile candidate"
                    )
                selected_compile = []
            catalog_path = str(source_manifest["capture"]["catalogPath"])
            shader_key = str(source_manifest["shader"]["shaderKey"])
            input_format = str(route_plan["inputFormat"])
            editable_format = str(route_plan["editableFormat"])
            stage = str(source_manifest["stage"])
            target_api = str(source_manifest["capture"].get("api", ""))
            source_evidence = {
                "sourceWorkspacePath": str(source_root),
                "sourceWorkspaceId": source_manifest["workspaceId"],
                "sourceRecoveryPlanSha256": recovery_plan["planSha256"],
                "decompileCandidateIndex": decompile_candidate_index,
                "compileCandidateIndex": (
                    compile_candidate_index if compile_candidates else None
                ),
                "decompileRouteIds": selected_decompile,
                "compileRouteIds": selected_compile,
            }
            expected_creation_identity = _workspace_creation_identity(source_manifest)
            if expected_creation_identity.get("catalogSha256") is None:
                # Legacy v1 manifests predate exact revision binding. Upgrade
                # only this recovery comparison with a freshly bounded hash;
                # the source manifest itself remains portable and untouched.
                unused_catalog_path, legacy_catalog_sha256 = (
                    _catalog_file_sha256(catalog_path)
                )
                expected_creation_identity["catalogSha256"] = (
                    legacy_catalog_sha256
                )

        catalog = self._catalog_for_create(None)
        fresh = catalog.plan_recovery(
            input_format,
            stage,
            editable_format=editable_format,
            capture_encoding=input_format,
            target_api=target_api,
            allow_reconstructed=True,
        )
        if selected_decompile and selected_decompile not in fresh.get(
            "decompileCandidates", []
        ):
            raise ToolchainRoutingError(
                "selected decompile candidate is no longer available with fixed integrity"
            )
        if selected_compile and selected_compile not in fresh.get(
            "compileCandidates", []
        ):
            raise ToolchainRoutingError(
                "selected compile candidate is no longer available with fixed integrity"
            )
        selection = dict(fresh)
        selection.update(
            {
                "decompileRouteIds": selected_decompile,
                "compileRouteIds": selected_compile,
                "mode": (
                    "round-trip"
                    if selected_decompile and selected_compile
                    else "compile-only-recovery"
                    if selected_compile
                    else "evidence-only"
                ),
            }
        )
        created = self.create(
            catalog_path,
            shader_key,
            target,
            toolchain_catalog=catalog,
            toolchain_selection=selection,
            editable_format=editable_format,
            _expected_creation_identity=expected_creation_identity,
        )
        target_root = self._workspace_root(target)
        with _lock_for(target_root), _WorkspaceFileLock(target_root):
            target_manifest = self._load_manifest(target_root)
            attempt_evidence = {
                "operation": "recovery-candidate-attempt",
                "revision": 0,
                "status": "selected",
                **source_evidence,
                "targetWorkspaceId": target_manifest["workspaceId"],
                "targetRoutePlanSha256": target_manifest["routePlan"]["planSha256"],
            }
            self._append_history(target_root, target_manifest, attempt_evidence)
        result = dict(created)
        result["recoveryAttempt"] = attempt_evidence
        return result

    @staticmethod
    def _route_upgrade_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Return only the route identity an upgrade is allowed to preserve."""

        def path_identity(name: str) -> list[dict[str, str]]:
            values = plan.get(name)
            if not isinstance(values, list):
                raise ManifestValidationError("workspace route path is invalid")
            result = []
            for hop in values:
                capability = hop.get("capability") if isinstance(hop, Mapping) else None
                if not isinstance(capability, Mapping):
                    raise ManifestValidationError(
                        "workspace route capability is invalid"
                    )
                result.append(
                    {
                        "routeId": str(capability.get("routeId", "")),
                        "inputFormat": str(capability.get("inputFormat", "")),
                        "outputFormat": str(capability.get("outputFormat", "")),
                    }
                )
            return result

        return {
            "inputFormat": str(plan.get("inputFormat", "")),
            "editableFormat": str(plan.get("editableFormat", "")),
            "stage": str(plan.get("stage", "")),
            "decompile": path_identity("decompile"),
            "compile": path_identity("compile"),
        }

    @staticmethod
    def _candidate_migration_summary(
        candidate: Any,
    ) -> dict[str, Any] | None:
        if candidate is None:
            return None
        candidate_mapping = candidate if isinstance(candidate, Mapping) else {}
        gate = candidate_mapping.get("abiGate")

        def declared_sha(field: str) -> str | None:
            value = candidate_mapping.get(field)
            if (
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            ):
                return value
            return None

        declared_revision = candidate_mapping.get("revision")
        if (
            isinstance(declared_revision, bool)
            or not isinstance(declared_revision, int)
            or declared_revision < 0
        ):
            declared_revision = None
        gate_status = gate.get("status") if isinstance(gate, Mapping) else None
        if not isinstance(gate_status, str) or len(gate_status) > 128:
            gate_status = None
        if isinstance(candidate, Mapping):
            metadata_shape = "object"
        elif isinstance(candidate, list):
            metadata_shape = "array"
        elif isinstance(candidate, bool):
            metadata_shape = "boolean"
        elif isinstance(candidate, (int, float)):
            metadata_shape = "number"
        elif isinstance(candidate, str):
            metadata_shape = "string"
        else:
            metadata_shape = "unknown"
        return {
            "schema": INVALIDATED_CANDIDATE_SUMMARY_SCHEMA,
            "schemaVersion": INVALIDATED_CANDIDATE_SUMMARY_VERSION,
            "disposition": "invalidated-not-imported",
            "metadataShape": metadata_shape,
            "metadataSha256": _canonical_sha256(candidate),
            "declaredRevision": declared_revision,
            "declaredRoutePlanSha256": declared_sha("routePlanSha256"),
            "declaredSourceSha256": declared_sha("sourceSha256"),
            "declaredRawSha256": declared_sha("rawSha256"),
            "declaredCodeSha256": declared_sha("codeSha256"),
            "declaredAbiGateStatus": gate_status,
            "trusted": False,
            "imported": False,
        }

    def _validate_toolchain_migration_source_record(
        self, manifest: Mapping[str, Any], source_record: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        migration = source_record.get("migration")
        required = {
            "schema",
            "schemaVersion",
            "at",
            "sourceWorkspaceId",
            "sourceWorkspacePath",
            "sourceCaptureId",
            "sourceRevision",
            "sourceRoutePlanSha256",
            "targetRoutePlanSha256",
            "routeIdentity",
            "preservedSourceSha256",
            "sourceMetadataSha256",
            "invalidatedSourceCandidate",
            "sourceCandidatesImported",
            "semanticEquivalenceVerified",
            "recordSha256",
        }
        if not isinstance(migration, Mapping) or set(migration) != required:
            raise ManifestValidationError(
                "toolchain-migrated source evidence is incomplete"
            )
        unsigned = dict(migration)
        declared_hash = unsigned.pop("recordSha256")
        plan = manifest.get("routePlan")
        if not isinstance(plan, Mapping):
            raise ManifestValidationError(
                "toolchain-migrated source has no routed target plan"
            )
        candidate = migration.get("invalidatedSourceCandidate")
        legacy_candidate_fields = {
            "revision",
            "routePlanSha256",
            "sourceSha256",
            "rawSha256",
            "codeSha256",
            "abiGateStatus",
        }
        invalidated_candidate_fields = {
            "schema",
            "schemaVersion",
            "disposition",
            "metadataShape",
            "metadataSha256",
            "declaredRevision",
            "declaredRoutePlanSha256",
            "declaredSourceSha256",
            "declaredRawSha256",
            "declaredCodeSha256",
            "declaredAbiGateStatus",
            "trusted",
            "imported",
        }
        if candidate is not None and not isinstance(candidate, Mapping):
            raise ManifestValidationError(
                "toolchain migration candidate archive is invalid"
            )
        candidate_fields = (
            frozenset(candidate) if isinstance(candidate, Mapping) else frozenset()
        )
        if candidate is not None and candidate_fields not in {
            frozenset(legacy_candidate_fields),
            frozenset(invalidated_candidate_fields),
        }:
            raise ManifestValidationError(
                "toolchain migration candidate archive is invalid"
            )

        def valid_sha(value: Any) -> bool:
            return bool(
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            )

        try:
            source_revision = int(migration.get("sourceRevision"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestValidationError(
                "toolchain migration source revision is invalid"
            ) from exc
        expected_routes = [
            hop["capability"]["routeId"] for hop in plan.get("decompile", [])
        ]
        source_workspace_id = migration.get("sourceWorkspaceId")
        if (
            migration.get("schema") != TOOLCHAIN_MIGRATION_SCHEMA
            or migration.get("schemaVersion") != TOOLCHAIN_MIGRATION_VERSION
            or declared_hash != _canonical_sha256(unsigned)
            or not isinstance(migration.get("at"), str)
            or not isinstance(migration.get("sourceWorkspacePath"), str)
            or not migration.get("sourceWorkspacePath")
            or not isinstance(source_workspace_id, str)
            or len(source_workspace_id) != 32
            or any(
                character not in "0123456789abcdef"
                for character in source_workspace_id
            )
            or not valid_sha(migration.get("sourceCaptureId"))
            or source_revision < 0
            or not valid_sha(migration.get("sourceRoutePlanSha256"))
            or not valid_sha(migration.get("targetRoutePlanSha256"))
            or migration.get("sourceRoutePlanSha256")
            == migration.get("targetRoutePlanSha256")
            or migration.get("targetRoutePlanSha256") != plan.get("planSha256")
            or migration.get("routeIdentity") != self._route_upgrade_identity(plan)
            or migration.get("preservedSourceSha256") != source_record.get("sha256")
            or not valid_sha(migration.get("sourceMetadataSha256"))
            or migration.get("sourceCandidatesImported") is not False
            or migration.get("semanticEquivalenceVerified") is not False
            or source_record.get("routePlanSha256") != plan.get("planSha256")
            or source_record.get("decompileRouteIds") != expected_routes
            or source_record.get("baselineCompileVerified") is not False
            or source_record.get("baselineCompile") is not None
        ):
            raise ManifestValidationError(
                "toolchain-migrated source evidence/hash binding mismatch"
            )
        if candidate is not None and candidate_fields == frozenset(
            legacy_candidate_fields
        ):
            if (
                candidate.get("revision") != source_revision
                or candidate.get("routePlanSha256")
                != migration.get("sourceRoutePlanSha256")
                or not all(
                    valid_sha(candidate.get(field))
                    for field in ("sourceSha256", "rawSha256", "codeSha256")
                )
            ):
                raise ManifestValidationError(
                    "toolchain migration candidate archive binding mismatch"
                )
        elif candidate is not None:
            declared_revision = candidate.get("declaredRevision")
            declared_status = candidate.get("declaredAbiGateStatus")
            declared_digests = (
                candidate.get("declaredRoutePlanSha256"),
                candidate.get("declaredSourceSha256"),
                candidate.get("declaredRawSha256"),
                candidate.get("declaredCodeSha256"),
            )
            if (
                candidate.get("schema")
                != INVALIDATED_CANDIDATE_SUMMARY_SCHEMA
                or candidate.get("schemaVersion")
                != INVALIDATED_CANDIDATE_SUMMARY_VERSION
                or candidate.get("disposition") != "invalidated-not-imported"
                or candidate.get("metadataShape")
                not in {"object", "array", "boolean", "number", "string", "unknown"}
                or not valid_sha(candidate.get("metadataSha256"))
                or (
                    declared_revision is not None
                    and (
                        isinstance(declared_revision, bool)
                        or not isinstance(declared_revision, int)
                        or declared_revision < 0
                    )
                )
                or any(
                    value is not None and not valid_sha(value)
                    for value in declared_digests
                )
                or (
                    declared_status is not None
                    and (
                        not isinstance(declared_status, str)
                        or len(declared_status) > 128
                    )
                )
                or candidate.get("trusted") is not False
                or candidate.get("imported") is not False
            ):
                raise ManifestValidationError(
                    "toolchain migration invalidated candidate summary mismatch"
                )
        return migration

    def _source_for_toolchain_migration(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> tuple[Path, bytes, dict[str, Any]]:
        source_record = manifest.get("source")
        if not isinstance(source_record, dict):
            raise ToolchainRoutingError(
                "toolchain migration requires tracked executable-editable source"
            )
        source_path = _source_path(root, manifest)
        tracked_path = self._artifact(root, source_record.get("path"))
        if tracked_path != source_path.resolve() or not is_within(root, tracked_path):
            raise ManifestValidationError("workspace source path is invalid")
        source = _source_bytes(tracked_path)
        tracked_hash = source_record.get("sha256")
        if (
            not isinstance(tracked_hash, str)
            or len(tracked_hash) != 64
            or any(character not in "0123456789abcdef" for character in tracked_hash)
            or source_record.get("encoding") != "utf-8"
            or source_record.get("lane") != "executable-editable"
            or source_record.get("compileEligible") is not True
            or source_record.get("semanticEquivalenceVerified") is not False
            or source_record.get("baselineRawSha256")
            != manifest.get("baseline", {}).get("rawSha256")
        ):
            raise ManifestValidationError("workspace source metadata mismatch")
        origin = source_record.get("origin")
        plan = manifest.get("routePlan")
        if not isinstance(plan, Mapping):
            raise ToolchainRoutingError(
                "legacy single-tool workspaces cannot be route-migrated"
            )
        if origin == "trusted-decompile-route":
            expected_routes = [
                hop["capability"]["routeId"] for hop in plan.get("decompile", [])
            ]
            jobs = source_record.get("producedByJobs")
            if (
                source_record.get("routePlanSha256") != plan.get("planSha256")
                or source_record.get("decompileRouteIds") != expected_routes
                or not isinstance(jobs, list)
                or len(jobs) != len(expected_routes)
            ):
                raise ManifestValidationError(
                    "workspace routed source provenance mismatch"
                )
            for relative_job in jobs:
                self._job_directory(root, relative_job)
        elif origin == "explicit-explanatory-promotion":
            promotion = source_record.get("promotion")
            if not isinstance(promotion, Mapping) or set(promotion) != {
                "at",
                "representationLane",
                "representationSha256",
                "reviewedSourcePath",
                "reviewedSourceSha256",
                "semanticEquivalenceAcknowledgedUnverified",
            }:
                raise ManifestValidationError(
                    "promoted source evidence is incomplete"
                )
            representation = load_fallback_representation(
                root,
                expected_baseline_path=self._artifact(
                    root, manifest["baseline"]["path"]
                ),
                expected_baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                expected_shader=manifest["shader"],
                expected_stage=manifest["stage"],
                expected_entry_point=manifest["entryPoint"],
                expected_route_plan=plan,
                expected_history=manifest.get("history", ()),
                expected_revision=manifest.get("revision"),
            )
            reviewed_path = self._artifact(
                root, promotion.get("reviewedSourcePath")
            )
            if (
                representation is None
                or promotion.get("representationLane")
                != representation.get("lane")
                or promotion.get("representationSha256")
                != representation.get("sha256")
                or promotion.get("reviewedSourceSha256") != tracked_hash
                or promotion.get("reviewedSourceSha256")
                != hashlib.sha256(_source_bytes(reviewed_path)).hexdigest()
                or promotion.get("semanticEquivalenceAcknowledgedUnverified")
                is not True
            ):
                raise ManifestValidationError(
                    "promoted source evidence/hash binding mismatch"
                )
        elif origin == "toolchain-migration-preserved-source":
            self._validate_toolchain_migration_source_record(
                manifest, source_record
            )
        else:
            raise ToolchainRoutingError(
                "workspace source origin is not eligible for routed migration"
            )
        return tracked_path, source, source_record

    def migrate_toolchain(
        self,
        source_workspace_path: str | Path,
        workspace_path: str | Path,
        *,
        expected_route_plan_sha256: str,
        expected_source_sha256: str,
        host_instance_id: str | None = None,
    ) -> dict:
        """Migrate source to current tools without importing old candidates.

        The source workspace is retained as an archive. The target starts at
        revision zero and binds only the same route IDs, formats, and stage to
        the process-owned catalog/registry. Source bytes are copied exactly;
        semantic equivalence remains explicitly unverified. Old candidate,
        revision-artifact, and ABI-report evidence is not imported or trusted;
        it is reduced to a bounded invalidation summary.

        ``host_instance_id`` is internal process-owner evidence, never request
        data.  When present it enables only the shared lease implementation's
        strict different-generation orphan recovery path.
        """

        expected_plan_hash = str(expected_route_plan_sha256 or "").lower()
        expected_source_hash = str(expected_source_sha256 or "").lower()
        for label, digest in (
            ("expected_route_plan_sha256", expected_plan_hash),
            ("expected_source_sha256", expected_source_hash),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ToolchainRoutingError("%s must be a lowercase SHA-256" % label)
        source_root = self._workspace_root(source_workspace_path)
        target = Path(workspace_path).expanduser().resolve()
        if target == source_root or is_within(source_root, target):
            raise ShaderWorkspaceError(
                "a toolchain migration target must stay outside its source workspace"
            )
        if target.exists() or target.is_symlink():
            raise ShaderWorkspaceError(
                "workspace path already exists: %s" % target
            )

        host_owner = str(host_instance_id or "").strip()
        staging: Path | None = None
        with (
            WorkspaceSourceTransactionLease(
                source_root,
                owner_instance_id=host_owner or None,
                recover_orphaned=bool(host_owner),
            ),
            _lock_for(source_root),
            _WorkspaceFileLock(source_root),
        ):
            source_manifest = self._load_manifest(
                source_root,
                allow_stale_route_bindings=True,
                validate_latest_candidate=False,
            )
            old_plan = source_manifest.get("routePlan")
            if not isinstance(old_plan, Mapping):
                raise ToolchainRoutingError(
                    "legacy single-tool workspaces cannot be route-migrated"
                )
            if old_plan.get("planSha256") != expected_plan_hash:
                raise ToolchainRoutingError(
                    "source route plan changed before toolchain migration"
                )
            source_path, source, source_record = self._source_for_toolchain_migration(
                source_root, source_manifest
            )
            source_hash = hashlib.sha256(source).hexdigest()
            if source_hash != expected_source_hash:
                raise ToolchainRoutingError(
                    "source bytes changed before toolchain migration"
                )

            old_identity = self._route_upgrade_identity(old_plan)
            selection = {
                "inputFormat": old_identity["inputFormat"],
                "editableFormat": old_identity["editableFormat"],
                "stage": old_identity["stage"],
                "decompileRouteIds": [
                    item["routeId"] for item in old_identity["decompile"]
                ],
                "compileRouteIds": [
                    item["routeId"] for item in old_identity["compile"]
                ],
                "mode": (
                    "round-trip"
                    if old_identity["decompile"] and old_identity["compile"]
                    else "compile-only-recovery"
                    if old_identity["compile"]
                    else "evidence-only"
                ),
            }
            catalog = self._catalog_for_create(None)
            new_plan = self._route_plan(
                catalog,
                selection,
                old_identity["inputFormat"],
                old_identity["stage"],
            )
            if self._route_upgrade_identity(new_plan) != old_identity:
                raise ToolchainRoutingError(
                    "current toolchain changed a route ID, format, or shader stage"
                )
            if new_plan.get("planSha256") == old_plan.get("planSha256"):
                raise ToolchainRoutingError(
                    "workspace route plan is already bound to the current toolchain"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.parent / (
                ".%s.toolchain-migration-%s" % (target.name, uuid.uuid4().hex)
            )
            expected_creation_identity = _workspace_creation_identity(source_manifest)
            try:
                self.create(
                    source_manifest["capture"]["catalogPath"],
                    source_manifest["shader"],
                    staging,
                    toolchain_catalog=catalog,
                    toolchain_selection=selection,
                    editable_format=old_identity["editableFormat"],
                    _expected_creation_identity=expected_creation_identity,
                )
                target_manifest = self._load_manifest(staging)
                target_plan = target_manifest.get("routePlan")
                if (
                    not isinstance(target_plan, Mapping)
                    or self._route_upgrade_identity(target_plan) != old_identity
                ):
                    raise ToolchainRoutingError(
                        "migrated workspace route identity changed during creation"
                    )
                now = _utc_now()
                candidate_summary = self._candidate_migration_summary(
                    source_manifest.get("latestCompiled")
                )
                migration = {
                    "schema": TOOLCHAIN_MIGRATION_SCHEMA,
                    "schemaVersion": TOOLCHAIN_MIGRATION_VERSION,
                    "at": now,
                    "sourceWorkspaceId": source_manifest["workspaceId"],
                    "sourceWorkspacePath": str(source_root),
                    "sourceCaptureId": source_manifest["capture"]["captureId"],
                    "sourceRevision": source_manifest["revision"],
                    "sourceRoutePlanSha256": old_plan["planSha256"],
                    "targetRoutePlanSha256": target_plan["planSha256"],
                    "routeIdentity": old_identity,
                    "preservedSourceSha256": source_hash,
                    "sourceMetadataSha256": _canonical_sha256(source_record),
                    "invalidatedSourceCandidate": candidate_summary,
                    "sourceCandidatesImported": False,
                    "semanticEquivalenceVerified": False,
                }
                migration["recordSha256"] = _canonical_sha256(migration)
                destination = _source_path(staging, target_manifest)
                atomic_write_bytes(destination, source)
                target_manifest["source"] = {
                    "path": _relative(staging, destination),
                    "sha256": source_hash,
                    "encoding": "utf-8",
                    "lane": "executable-editable",
                    "origin": "toolchain-migration-preserved-source",
                    "compileEligible": True,
                    "semanticEquivalenceVerified": False,
                    "routePlanSha256": target_plan["planSha256"],
                    "decompileRouteIds": [
                        item["routeId"] for item in old_identity["decompile"]
                    ],
                    "baselineCompileVerified": False,
                    "baselineCompile": None,
                    "baselineRawSha256": target_manifest["baseline"]["rawSha256"],
                    "migration": migration,
                }
                target_manifest["history"].append(
                    {
                        "operation": "toolchain-route-migration",
                        "revision": 0,
                        "status": "source-preserved-candidates-invalidated",
                        "migration": migration,
                    }
                )
                target_manifest["updatedAt"] = now
                atomic_write_json(staging / "manifest.json", target_manifest)
                migrated_status = self.status(staging)
                if (
                    migrated_status["source"]["currentSha256"] != source_hash
                    or migrated_status["latestCompiled"] is not None
                    or migrated_status["revision"] != 0
                ):
                    raise ManifestValidationError(
                        "migrated workspace published unexpected candidate state"
                    )
                if hashlib.sha256(_source_bytes(source_path)).hexdigest() != source_hash:
                    raise ToolchainRoutingError(
                        "source bytes changed while toolchain migration was running"
                    )
                if target.exists() or target.is_symlink():
                    raise ShaderWorkspaceError(
                        "workspace path appeared during migration: %s" % target
                    )
                staging.replace(target)
                staging = None
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)

        return {
            "schema": TOOLCHAIN_MIGRATION_SCHEMA,
            "schemaVersion": TOOLCHAIN_MIGRATION_VERSION,
            "sourceWorkspacePath": str(source_root),
            "workspacePath": str(target),
            "sourcePath": str(_source_path(target, target_manifest)),
            "sourceSha256": source_hash,
            "fromRoutePlanSha256": old_plan["planSha256"],
            "routePlanSha256": target_plan["planSha256"],
            "routeIdentity": old_identity,
            "sourceWorkspaceRevision": source_manifest["revision"],
            "revision": 0,
            "latestCandidateImported": False,
            "invalidatedSourceCandidate": candidate_summary,
            "sourceWorkspaceDisposition": "preserved-archive",
            "semanticEquivalenceVerified": False,
        }

    @staticmethod
    def _live_edit_identity_from_manifest(
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project the immutable capture/Shader facts used by durable consent."""

        capture = manifest.get("capture")
        shader = manifest.get("shader")
        if not isinstance(capture, Mapping) or not isinstance(shader, Mapping):
            raise ManifestValidationError(
                "toolchain migration workspace identity is incomplete"
            )
        return {
            "workspaceId": manifest.get("workspaceId"),
            "captureId": capture.get("captureId"),
            "capturePath": capture.get("capturePath"),
            "shaderKey": shader.get("shaderKey"),
            "shaderRawSha256": shader.get("rawSha256"),
            "resourceId": shader.get("resourceId"),
            "stage": manifest.get("stage"),
            "entryPoint": manifest.get("entryPoint"),
        }

    def live_edit_authorization_migration_evidence(
        self,
        source_workspace_path: str | Path,
        workspace_path: str | Path,
        *,
        expected_source_sha256: str,
    ) -> dict[str, Any]:
        """Verify one exact workspace-migration edge for durable live-edit consent.

        This is deliberately read-only.  It accepts a stale *route* binding only
        for the archived predecessor, then proves that the current workspace was
        created by :meth:`migrate_toolchain`, that both source files still contain
        the exact copied bytes, and that every immutable capture/Shader fact is
        unchanged.  Candidate, ABI and replay-apply evidence is never imported.
        """

        expected_hash = str(expected_source_sha256 or "").lower()
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise ToolchainRoutingError(
                "expected_source_sha256 must be a lowercase SHA-256"
            )
        source_root = self._workspace_root(source_workspace_path)
        target_root = self._workspace_root(workspace_path)
        if source_root == target_root:
            raise ShaderWorkspaceError(
                "live-edit authorization migration requires two workspaces"
            )

        # A deterministic acquisition order prevents two simultaneous migration
        # checks from deadlocking on opposite source/target pairs.  The shared
        # source lease also excludes a running watcher or organizer transaction.
        roots = sorted(
            (source_root, target_root),
            key=lambda item: os.path.normcase(os.path.normpath(str(item))),
        )
        with ExitStack() as stack:
            for root in roots:
                stack.enter_context(WorkspaceSourceTransactionLease(root))
                stack.enter_context(_lock_for(root))
                stack.enter_context(_WorkspaceFileLock(root))

            source_manifest = self._load_manifest(
                source_root,
                allow_stale_route_bindings=True,
                validate_latest_candidate=False,
            )
            target_manifest = self._load_manifest(target_root)
            _, source_bytes, source_record = self._source_for_toolchain_migration(
                source_root, source_manifest
            )
            _, target_bytes, target_source_record = (
                self._source_for_toolchain_migration(target_root, target_manifest)
            )
            migration = target_source_record.get("migration")
            if not isinstance(migration, Mapping):
                raise ManifestValidationError(
                    "target workspace is not a toolchain migration"
                )

            source_hash = hashlib.sha256(source_bytes).hexdigest()
            target_hash = hashlib.sha256(target_bytes).hexdigest()
            try:
                declared_source_root = Path(
                    str(migration.get("sourceWorkspacePath", ""))
                ).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ManifestValidationError(
                    "toolchain migration predecessor path is unavailable"
                ) from exc
            source_plan = source_manifest.get("routePlan")
            source_capture = source_manifest.get("capture")
            if not isinstance(source_plan, Mapping) or not isinstance(
                source_capture, Mapping
            ):
                raise ManifestValidationError(
                    "toolchain migration predecessor identity is incomplete"
                )
            if (
                source_hash != expected_hash
                or target_hash != expected_hash
                or target_source_record.get("sha256") != expected_hash
                or migration.get("preservedSourceSha256") != expected_hash
            ):
                raise ToolchainRoutingError(
                    "source bytes changed across live-edit authorization migration"
                )
            if (
                os.path.normcase(os.path.normpath(str(declared_source_root)))
                != os.path.normcase(os.path.normpath(str(source_root)))
                or migration.get("sourceWorkspaceId")
                != source_manifest.get("workspaceId")
                or migration.get("sourceCaptureId")
                != source_capture.get("captureId")
                or migration.get("sourceRevision")
                != source_manifest.get("revision")
                or migration.get("sourceRoutePlanSha256")
                != source_plan.get("planSha256")
                or migration.get("sourceMetadataSha256")
                != _canonical_sha256(source_record)
                or _workspace_creation_identity(source_manifest)
                != _workspace_creation_identity(target_manifest)
            ):
                raise ManifestValidationError(
                    "toolchain migration does not directly bind this predecessor"
                )

            result = {
                "schema": LIVE_EDIT_AUTHORIZATION_MIGRATION_EVIDENCE_SCHEMA,
                "schemaVersion": LIVE_EDIT_AUTHORIZATION_MIGRATION_EVIDENCE_VERSION,
                "sourceWorkspacePath": str(source_root),
                "workspacePath": str(target_root),
                "sourceSha256": expected_hash,
                "sourceWorkspaceIdentity": self._live_edit_identity_from_manifest(
                    source_manifest
                ),
                "workspaceIdentity": self._live_edit_identity_from_manifest(
                    target_manifest
                ),
                "migrationRecordSha256": migration.get("recordSha256"),
                "sourceCandidatesImported": False,
                "semanticEquivalenceVerified": False,
            }
            result["evidenceSha256"] = _canonical_sha256(result)
            return result

    def _bound_route_operation(self, hop: Mapping[str, Any]) -> ToolOperation:
        capability = hop.get("capability")
        binding = hop.get("operation")
        if not isinstance(capability, Mapping) or not isinstance(binding, Mapping):
            raise ManifestValidationError("workspace route hop binding is invalid")
        operation = self.registry.operation(
            str(capability.get("toolId", "")),
            str(capability.get("operation", "")),
        )
        if binding != _operation_binding(operation):
            raise ToolConfigurationError(
                "workspace route operation changed after workspace creation"
            )
        return operation

    def _run_route_chain(
        self,
        root: Path,
        manifest: dict,
        direction: str,
        revision: int,
        initial_bytes: bytes,
        baseline_path: Path,
        history: dict,
    ) -> tuple[bytes, list[dict[str, Any]]]:
        plan = manifest.get("routePlan")
        if not isinstance(plan, Mapping):
            raise ManifestValidationError("workspace route plan is missing")
        hops = plan.get(direction)
        if not isinstance(hops, list) or not hops:
            raise ManifestValidationError("workspace route chain is missing")
        current = initial_bytes
        provenance: list[dict[str, Any]] = []
        for index, hop in enumerate(hops):
            if not isinstance(hop, Mapping):
                raise ManifestValidationError("workspace route hop is invalid")
            capability = hop.get("capability")
            if not isinstance(capability, Mapping):
                raise ManifestValidationError("workspace route capability is invalid")
            input_format = normalize_format(capability.get("inputFormat"))
            output_format = normalize_format(capability.get("outputFormat"))
            operation = self._bound_route_operation(hop)
            job_dir = self._new_job_path(
                root, "%s-h%02d" % (direction, index + 1), revision
            )
            job_dir.mkdir(parents=False, exist_ok=False)
            input_path = job_dir / ("input" + _FORMAT_SUFFIX[input_format])
            output_path = job_dir / ("output" + _FORMAT_SUFFIX[output_format])
            atomic_write_bytes(input_path, current)
            input_detection = detect_shader_format(current, input_format)
            values = self._template_values(
                root,
                manifest,
                revision,
                job_dir,
                input_path,
                output_path,
                baseline_path,
            )
            result = run_operation(operation, values, job_dir)
            record = self._history_entry(root, operation, result, revision)
            record.update(
                {
                    "hop": index + 1,
                    "routeBinding": dict(hop),
                    "inputFormat": input_format,
                    "outputFormat": output_format,
                    "inputPath": _relative(root, input_path),
                    "inputSha256": hashlib.sha256(current).hexdigest(),
                    "inputDetection": input_detection.as_dict(),
                }
            )
            provenance.append(record)
            history["hops"] = provenance
            try:
                self._require_process_success(result)
                self._require_job_output(job_dir, output_path, "route hop")
                size = output_path.stat().st_size
                limit = MAX_SOURCE_BYTES if output_format in TEXT_FORMATS else MAX_COMPILED_BYTES
                if size <= 0 or size > limit:
                    raise ToolExecutionError(
                        "route %s output must contain 1..%d bytes"
                        % (capability.get("routeId"), limit)
                    )
                output = output_path.read_bytes()
                # Declared route metadata is not evidence. Outputs must be
                # independently recognizable as the route's advertised format.
                detection = detect_shader_format(output, output_format)
                if detection.format != output_format:
                    raise ToolExecutionError(
                        "route %s declared %s but output detected as %s"
                        % (
                            capability.get("routeId"),
                            output_format,
                            detection.format or "unknown",
                        )
                    )
                if output_format in TEXT_FORMATS:
                    try:
                        text = output.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise ToolExecutionError(
                            "route produced non-UTF-8 editable text"
                        ) from exc
                    if "\x00" in text:
                        raise ToolExecutionError(
                            "route produced editable text containing NUL"
                        )
                record.update(
                    {
                        "status": "succeeded",
                        "outputPath": _relative(root, output_path),
                        "outputSha256": hashlib.sha256(output).hexdigest(),
                        "outputDetection": detection.as_dict(),
                    }
                )
                current = output
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc)})
                raise
        return current, provenance

    @staticmethod
    def _needs_dedicated_dxil_disassembly_evidence(
        manifest: Mapping[str, Any],
    ) -> bool:
        """Return whether a reconstructed-HLSL round trip needs physical IR.

        Native DXIL-ASM workspaces already carry stronger hash-adjacent
        disassembly/assembly provenance in their ordinary route hops.  The
        independent lane exists specifically so a DXIL -> HLSL -> DXIL route
        can prove ambiguous CBuffer accesses without treating reconstructed
        HLSL as ABI evidence.
        """

        baseline = manifest.get("baseline")
        plan = manifest.get("routePlan")
        return bool(
            isinstance(baseline, Mapping)
            and str(baseline.get("kind", "")).upper() == "DXIL"
            and baseline.get("abiStatus") == ABI_STATUS_DXIL_ACCESS_REQUIRED
            and isinstance(plan, Mapping)
            and plan.get("inputFormat") == "dxil"
            and plan.get("editableFormat") == "hlsl"
            and isinstance(plan.get("decompile"), list)
            and bool(plan.get("decompile"))
            and isinstance(plan.get("compile"), list)
            and bool(plan.get("compile"))
        )

    def _dedicated_dxil_disassembly_binding(
        self, manifest: Mapping[str, Any]
    ) -> tuple[ToolchainCatalog, RouteCapability, ToolOperation, dict[str, Any]]:
        """Bind one unique integrity-valid direct DXIL disassembler.

        Production catalogs expose this capability through DXC ``-dumpbin``.
        Selection is capability based so hermetic test tools can exercise the
        same fixed-program trust boundary. Equal-priority direct producers are
        deliberately ambiguous and therefore unavailable as evidence.
        """

        if self.route_config_path is None:
            raise ToolchainRoutingError(
                "dedicated DXIL disassembly requires the process-owned route catalog"
            )
        try:
            self._validate_route_plan(manifest, "dxil")
        except (ManifestValidationError, ToolConfigurationError) as exc:
            raise ToolchainRoutingError(
                f"workspace route plan cannot bind dedicated DXIL disassembly: {exc}"
            ) from exc
        catalog = self._process_catalog()
        candidates = tuple(
            route
            for route in catalog.matching_routes(
                "dxil",
                "dxil-asm",
                str(manifest.get("stage", "")),
                allow_reconstructed=False,
                require_integrity=True,
            )
            if route.operation == "decompile"
            and route.fidelity == "ir-disassembly"
        )
        if not candidates:
            raise ToolchainRoutingError(
                "no integrity-valid direct DXIL -> DXIL-ASM disassembler is configured"
            )
        best_priority = min(route.priority for route in candidates)
        best = tuple(
            route for route in candidates if route.priority == best_priority
        )
        if len(best) != 1:
            raise ToolchainRoutingError(
                "dedicated DXIL disassembly route is ambiguous: "
                + ", ".join(sorted(route.route_id for route in best))
            )
        route = best[0]
        operation = self.registry.operation(route.tool_id, route.operation)
        binding = {
            "hop": 1,
            "capability": route.as_dict(),
            "operation": _operation_binding(operation),
        }
        return catalog, route, operation, binding

    def _record_dedicated_dxil_disassembly(
        self,
        manifest: dict,
        role: str,
        record: Mapping[str, Any],
    ) -> None:
        if role not in {"baseline", "candidate"}:
            raise ManifestValidationError("DXIL disassembly evidence role is invalid")
        if (
            set(record) != _DXIL_DISASSEMBLY_RECORD_FIELDS
            or record.get("status") != "succeeded"
            or record.get("evidenceRole") != role
        ):
            raise ManifestValidationError(
                "successful DXIL disassembly evidence record is invalid"
            )
        evidence = manifest.get("dxilDisassemblyEvidence")
        if evidence is None:
            value: dict[str, Any] = {
                "schema": DXIL_DISASSEMBLY_EVIDENCE_SCHEMA,
                "schemaVersion": DXIL_DISASSEMBLY_EVIDENCE_VERSION,
                "baseline": None,
                "candidate": None,
            }
        elif (
            isinstance(evidence, Mapping)
            and set(evidence)
            == {"schema", "schemaVersion", "baseline", "candidate"}
            and evidence.get("schema") == DXIL_DISASSEMBLY_EVIDENCE_SCHEMA
            and evidence.get("schemaVersion")
            == DXIL_DISASSEMBLY_EVIDENCE_VERSION
        ):
            value = dict(evidence)
        else:
            # Never repair or overwrite malformed provenance while performing
            # an unrelated compile/decompile operation.
            raise ManifestValidationError(
                "workspace DXIL disassembly evidence metadata is invalid"
            )
        value[role] = dict(record)
        manifest["dxilDisassemblyEvidence"] = value

    def _attempt_dedicated_dxil_disassembly(
        self,
        root: Path,
        manifest: dict,
        *,
        role: str,
        revision: int,
        input_bytes: bytes,
        input_artifact_path: Path,
        history: dict,
    ) -> dict[str, Any] | None:
        """Create a separate fail-closed physical-DXIL evidence job.

        Failure is retained in operation history, while the editable source or
        compiled container can still be inspected.  No successful evidence is
        published, so ambiguous ABI validation remains closed later.
        """

        history_key = f"{role}DxilDisassemblyEvidence"
        attempt: dict[str, Any] = {
            "status": "not-started",
            "evidenceRole": role,
            "revision": revision,
        }
        history[history_key] = attempt
        try:
            if role not in {"baseline", "candidate"}:
                raise ManifestValidationError(
                    "DXIL disassembly evidence role is invalid"
                )
            input_digest = hashlib.sha256(input_bytes).hexdigest()
            if not input_bytes or len(input_bytes) > MAX_COMPILED_BYTES:
                raise ToolExecutionError(
                    "DXIL disassembly evidence input size is invalid"
                )
            catalog, route, operation, route_binding = (
                self._dedicated_dxil_disassembly_binding(manifest)
            )
            job_dir = self._new_job_path(
                root, f"{role}-dxil-disassembly", revision
            )
            job_dir.mkdir(parents=False, exist_ok=False)
            input_path = job_dir / "input.dxil"
            output_path = job_dir / "output.ll"
            atomic_write_bytes(input_path, input_bytes)
            input_detection = detect_shader_format(input_bytes, "dxil")
            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            values = self._template_values(
                root,
                manifest,
                revision,
                job_dir,
                input_path,
                output_path,
                baseline_path,
            )
            result = run_operation(operation, values, job_dir)
            record = self._history_entry(root, operation, result, revision)
            record.update(
                {
                    "schema": DXIL_DISASSEMBLY_RECORD_SCHEMA,
                    "schemaVersion": DXIL_DISASSEMBLY_RECORD_VERSION,
                    "evidenceRole": role,
                    "workspacePath": str(root.resolve()),
                    "workspaceId": manifest.get("workspaceId"),
                    "captureId": manifest.get("capture", {}).get("captureId"),
                    "routePlanSha256": manifest["routePlan"]["planSha256"],
                    "routeCatalogPath": str(catalog.path),
                    "routeCatalogSha256": _file_sha256(catalog.path),
                    "toolRegistryPath": str(catalog.registry.path),
                    "toolRegistrySha256": catalog.tool_registry_sha256,
                    "routeBinding": route_binding,
                    "command": list(result.command),
                    "stdoutSha256": hashlib.sha256(
                        result.stdout_path.read_bytes()
                    ).hexdigest(),
                    "stderrSha256": hashlib.sha256(
                        result.stderr_path.read_bytes()
                    ).hexdigest(),
                    "inputArtifactPath": _relative(root, input_artifact_path),
                    "inputFormat": route.input_format,
                    "inputPath": _relative(root, input_path),
                    "inputSha256": input_digest,
                    "inputByteSize": len(input_bytes),
                    "inputDetection": input_detection.as_dict(),
                    "outputFormat": route.output_format,
                }
            )
            history[history_key] = record
            self._require_process_success(result)
            self._require_job_output(job_dir, output_path, "DXIL disassembler")
            output_size = output_path.stat().st_size
            if output_size <= 0 or output_size > MAX_SOURCE_BYTES:
                raise ToolExecutionError(
                    f"DXIL disassembly evidence must contain 1..{MAX_SOURCE_BYTES} bytes"
                )
            output = output_path.read_bytes()
            output_detection = detect_shader_format(output, "dxil-asm")
            try:
                text = output.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ToolExecutionError(
                    "DXIL disassembly evidence is not strict UTF-8"
                ) from exc
            if "\x00" in text:
                raise ToolExecutionError(
                    "DXIL disassembly evidence contains NUL"
                )
            record.update(
                {
                    "outputPath": _relative(root, output_path),
                    "outputSha256": hashlib.sha256(output).hexdigest(),
                    "outputByteSize": len(output),
                    "outputDetection": output_detection.as_dict(),
                    "status": "succeeded",
                }
            )
            self._record_dedicated_dxil_disassembly(manifest, role, record)
            return record
        except (
            ShaderWorkspaceError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            failed = history.get(history_key)
            if isinstance(failed, dict):
                failed.update({"status": "failed", "error": str(exc)})
            else:
                history[history_key] = {
                    "status": "failed",
                    "evidenceRole": role,
                    "revision": revision,
                    "error": str(exc),
                }
            return None

    def _decompile_route_chain(
        self, root: Path, manifest: dict, destination: Path
    ) -> dict:
        revision = int(manifest["revision"])
        baseline_path = self._artifact(root, manifest["baseline"]["path"])
        history = {
            "operation": "decompile-route-chain",
            "revision": revision,
            "routePlanSha256": manifest["routePlan"]["planSha256"],
            "hops": [],
            "baselineCompileProbe": {
                "status": "not-started",
                "hops": [],
            },
        }
        try:
            source, hops = self._run_route_chain(
                root,
                manifest,
                "decompile",
                revision,
                baseline_path.read_bytes(),
                baseline_path,
                history,
            )
            if not manifest["routePlan"].get("compile"):
                history["baselineCompileProbe"] = {
                    "status": "not-applicable-evidence-only",
                    "hops": [],
                }
                representation = publish_tool_explanatory_representation(
                    root,
                    baseline_path=baseline_path,
                    baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                    shader=manifest["shader"],
                    stage=manifest["stage"],
                    entry_point=manifest["entryPoint"],
                    payload=source,
                    route_ids=[
                        item["routeBinding"]["capability"]["routeId"]
                        for item in hops
                    ],
                    job_paths=[item["jobPath"] for item in hops],
                    diagnostics=[
                        {
                            "code": "one-way-tool-decompile",
                            "message": (
                                "The fixed decompile route has no reverse compiler; "
                                "its output is explanatory and cannot compile/apply."
                            ),
                        }
                    ],
                )
                history.update(
                    {
                        "status": "succeeded-explanatory",
                        "outputSha256": hashlib.sha256(source).hexdigest(),
                        "publishedRepresentationPath": _relative(
                            root, Path(representation["path"])
                        ),
                        "representationSha256": representation["sha256"],
                    }
                )
                self._append_history(root, manifest, history)
                return {
                    "workspacePath": str(root),
                    "representationPath": representation["path"],
                    "representationSha256": representation["sha256"],
                    "lane": "explanatory-ir",
                    "compileEligible": False,
                    "applyEligible": False,
                    "jobPath": str(root / hops[-1]["jobPath"]),
                    "jobPaths": [str(root / item["jobPath"]) for item in hops],
                    "routeIds": [
                        item["routeBinding"]["capability"]["routeId"]
                        for item in hops
                    ],
                    "toolIds": [item["toolId"] for item in hops],
                    "revision": revision,
                    "requiresReverseCompilerForExecutableEditing": True,
                }
            # A route that merely emits HLSL has not yet proved that its output
            # belongs in the executable-editable lane.  Before publishing the
            # file, compile the untouched reconstruction through the exact
            # reverse chain and verify the resulting physical container and
            # target profile.  This is deliberately only a baseline compiler
            # probe: replay reflection/ABI equality and semantic equivalence
            # remain separate, mandatory gates for apply.
            probe_history = {
                "operation": "decompiler-baseline-compile-probe",
                "revision": revision,
                "routePlanSha256": manifest["routePlan"]["planSha256"],
                "hops": [],
            }
            history["baselineCompileProbe"] = probe_history
            round_trip, compile_hops = self._run_route_chain(
                root,
                manifest,
                "compile",
                revision,
                source,
                baseline_path,
                probe_history,
            )
            round_trip_container = self._validated_compiled_container(
                round_trip,
                manifest,
                "untouched decompiler baseline compile probe",
            )
            probe_history.update(
                {
                    "status": "succeeded",
                    "outputSha256": hashlib.sha256(round_trip).hexdigest(),
                    "outputCodeSha256": round_trip_container.code_sha256,
                    "outputByteSize": len(round_trip),
                    "outputKind": round_trip_container.kind,
                }
            )
            if self._needs_dedicated_dxil_disassembly_evidence(manifest):
                self._attempt_dedicated_dxil_disassembly(
                    root,
                    manifest,
                    role="baseline",
                    revision=0,
                    input_bytes=baseline_path.read_bytes(),
                    input_artifact_path=baseline_path,
                    history=history,
                )
            # Publication is deliberately last: a failed intermediate/final hop
            # or failed baseline compile leaves only isolated job evidence and
            # no executable-editable source.
            source_path = destination
            atomic_write_bytes(source_path, source)
            source_hash = hashlib.sha256(source).hexdigest()
            history.update(
                {
                    "status": "succeeded",
                    "publishedSourcePath": _relative(root, source_path),
                    "outputSha256": source_hash,
                }
            )
            manifest["source"] = {
                "path": _relative(root, source_path),
                "sha256": source_hash,
                "encoding": "utf-8",
                "lane": "executable-editable",
                "origin": "trusted-decompile-route",
                "compileEligible": True,
                "semanticEquivalenceVerified": False,
                "producedByJobs": [item["jobPath"] for item in hops],
                "routePlanSha256": manifest["routePlan"]["planSha256"],
                "decompileRouteIds": [
                    item["routeBinding"]["capability"]["routeId"]
                    for item in hops
                ],
                "baselineCompileVerified": True,
                "baselineCompile": {
                    "routeIds": [
                        item["routeBinding"]["capability"]["routeId"]
                        for item in compile_hops
                    ],
                    "producedByJobs": [item["jobPath"] for item in compile_hops],
                    "blobPath": compile_hops[-1]["outputPath"],
                    "rawSha256": hashlib.sha256(round_trip).hexdigest(),
                    "codeSha256": round_trip_container.code_sha256,
                    "byteSize": len(round_trip),
                    "kind": round_trip_container.kind,
                    "targetProfile": manifest["targetProfile"],
                    "replayAbiValidated": False,
                    "semanticEquivalenceVerified": False,
                },
                "baselineRawSha256": manifest["baseline"]["rawSha256"],
            }
            self._append_history(root, manifest, history)
        except Exception as exc:
            probe = history.get("baselineCompileProbe")
            if isinstance(probe, dict) and probe.get("status") != "succeeded":
                probe.update({"status": "failed", "error": str(exc)})
            history.update({"status": "failed", "error": str(exc)})
            self._append_history(root, manifest, history)
            raise
        return {
            "workspacePath": str(root),
            "sourcePath": str(destination),
            "sourceSha256": source_hash,
            "jobPath": str(root / hops[-1]["jobPath"]),
            "jobPaths": [str(root / item["jobPath"]) for item in hops],
            "routeIds": [
                item["routeBinding"]["capability"]["routeId"] for item in hops
            ],
            "toolIds": [item["toolId"] for item in hops],
            "baselineCompileVerified": True,
            "baselineCompileJobPath": str(root / compile_hops[-1]["jobPath"]),
            "baselineCompileJobPaths": [
                str(root / item["jobPath"]) for item in compile_hops
            ],
            "baselineCompileRouteIds": [
                item["routeBinding"]["capability"]["routeId"]
                for item in compile_hops
            ],
            "baselineCompileRawSha256": hashlib.sha256(round_trip).hexdigest(),
            "revision": revision,
            "requiresReplayAbiValidation": True,
        }

    def _validated_compiled_container(
        self, compiled: bytes, manifest: Mapping[str, Any], label: str
    ):
        plan = manifest.get("routePlan")
        expected_format = (
            str(plan.get("inputFormat", ""))
            if isinstance(plan, Mapping)
            else str(manifest.get("targetProfile", {}).get("containerFormat", ""))
        )
        container = fingerprint_shader_executable(
            compiled, label, format_hint=expected_format or None
        )
        baseline_kind = manifest.get("baseline", {}).get("kind")
        if container is None or container.byte_size != len(compiled):
            raise ToolExecutionError(
                "compiler output is not exactly one %s"
                % (
                    "valid DXBC/DXIL container"
                    if expected_format in {"dxbc", "dxil"}
                    else "structurally valid executable for the captured shader format"
                )
            )
        if container.kind != baseline_kind:
            raise ToolExecutionError(
                "compile route changed the captured container format from %s to %s"
                % (baseline_kind, container.kind)
            )
        capture = manifest.get("capture")
        capture_api = capture.get("api", "") if isinstance(capture, Mapping) else ""
        try:
            physical_profile = _physical_target_profile(
                compiled,
                str(manifest.get("stage", "")),
                executable_format(container),
                str(capture_api),
            )
        except ToolchainRoutingError as exc:
            raise ToolExecutionError(
                "compiled target profile cannot be read from executable bytes: %s"
                % exc
            ) from exc
        if physical_profile != manifest.get("targetProfile"):
            raise ToolExecutionError(
                "compiled executable target profile differs from the captured "
                "physical target profile"
            )
        return container

    def _compile_route_chain(
        self, root: Path, manifest: dict, source: bytes
    ) -> dict:
        self._ensure_dedicated_baseline_dxil_disassembly(root, manifest)
        # Baseline refresh above is an independently committed transaction.
        # Everything after this snapshot belongs to exactly one candidate
        # revision and must either publish together or leave the previous
        # candidate/evidence authoritative.
        manifest_before_candidate = deepcopy(manifest)
        next_revision = int(manifest["revision"]) + 1
        baseline_path = self._artifact(root, manifest["baseline"]["path"])
        revision_dir = root / "revisions" / ("%04d" % next_revision)
        revision_published = False
        history = {
            "operation": "compile-route-chain",
            "revision": next_revision,
            "routePlanSha256": manifest["routePlan"]["planSha256"],
            "hops": [],
        }
        try:
            compiled, hops = self._run_route_chain(
                root,
                manifest,
                "compile",
                next_revision,
                source,
                baseline_path,
                history,
            )
            container = self._validated_compiled_container(
                compiled, manifest, "workspace routed compiler output"
            )
            if revision_dir.exists():
                raise ManifestValidationError(
                    "revision artifact already exists: %s" % revision_dir
                )
            if self._needs_dedicated_dxil_disassembly_evidence(manifest):
                self._attempt_dedicated_dxil_disassembly(
                    root,
                    manifest,
                    role="candidate",
                    revision=next_revision,
                    input_bytes=compiled,
                    input_artifact_path=revision_dir / "shader.bin",
                    history=history,
                )
            revision_staging = (
                root
                / "revisions"
                / (".%04d.staging-%s" % (next_revision, uuid.uuid4().hex))
            )
            revision_staging.mkdir(parents=False, exist_ok=False)
            try:
                atomic_write_bytes(revision_staging / "shader.bin", compiled)
                staged_revision_source = _revision_source_path(
                    revision_staging, manifest
                )
                atomic_write_bytes(staged_revision_source, source)
                _replace_revision_directory(revision_staging, revision_dir)
                revision_published = True
            except Exception:
                shutil.rmtree(revision_staging, ignore_errors=True)
                raise
            revision_blob = revision_dir / "shader.bin"
            revision_source = _revision_source_path(revision_dir, manifest)
            source_hash = hashlib.sha256(source).hexdigest()
            compiled_hash = hashlib.sha256(compiled).hexdigest()
            candidate = {
                "revision": next_revision,
                "blobPath": _relative(root, revision_blob),
                "sourcePath": _relative(root, revision_source),
                "sourceSha256": source_hash,
                "rawSha256": compiled_hash,
                "codeSha256": container.code_sha256,
                "byteSize": len(compiled),
                "kind": container.kind,
                "routePlanSha256": manifest["routePlan"]["planSha256"],
                "compileRouteIds": [
                    item["routeBinding"]["capability"]["routeId"]
                    for item in hops
                ],
                "compileHops": hops,
                "validationScope": "container-structure-only",
                "requiresReplayAbiValidation": True,
                "replayAbiValidated": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
                "abiGate": {
                    "status": _initial_abi_gate_status(manifest),
                    "baselineAbiSha256": manifest["baseline"].get("abiSha256"),
                },
            }
            history.update(
                {
                    "status": "succeeded",
                    "outputSha256": compiled_hash,
                    "candidate": candidate,
                }
            )
            manifest["revision"] = next_revision
            manifest["latestCompiled"] = candidate
            self._append_history(root, manifest, history)
        except Exception as exc:
            # `_attempt_dedicated_dxil_disassembly` intentionally records its
            # successful job into the candidate transaction before directory
            # publication.  Restoring the complete pre-candidate snapshot is
            # essential: otherwise a failed revision N+1 can overwrite the
            # active DXIL evidence for the still-good revision N and make its
            # immutable ABI report impossible to revalidate.
            manifest.clear()
            manifest.update(manifest_before_candidate)
            rollback_error: str | None = None
            if revision_published and revision_dir.is_dir() and not revision_dir.is_symlink():
                try:
                    shutil.rmtree(revision_dir)
                except OSError as cleanup_exc:
                    rollback_error = str(cleanup_exc)
            history.update({"status": "failed", "error": str(exc)})
            if rollback_error is not None:
                history["rollbackError"] = rollback_error
            self._append_history(root, manifest, history)
            raise
        return {
            "workspacePath": str(root),
            "revision": next_revision,
            "sourceSha256": source_hash,
            "candidateBlobPath": str(revision_blob),
            "rawSha256": compiled_hash,
            "codeSha256": container.code_sha256,
            "kind": container.kind,
            "jobPath": str(root / hops[-1]["jobPath"]),
            "jobPaths": [str(root / item["jobPath"]) for item in hops],
            "routeIds": [
                item["routeBinding"]["capability"]["routeId"] for item in hops
            ],
            "toolIds": [item["toolId"] for item in hops],
            "lastGoodRevision": manifest["lastGood"]["revision"],
            "requiresReplayAbiValidation": True,
            "replayAbiValidated": False,
        }

    def decompile(self, workspace_path: str | Path) -> dict:
        root = self._workspace_root(workspace_path)
        with _lock_for(root), _WorkspaceFileLock(root):
            manifest = self._load_manifest(root)
            destination = _source_path(root, manifest)
            if destination.exists() or destination.is_symlink():
                raise ToolExecutionError(
                    "refusing to overwrite existing shader source; compile/archive it "
                    "or create a new workspace"
                )
            if manifest.get("routePlan") is not None:
                if not manifest["routePlan"].get("decompile"):
                    error = ToolchainRoutingError(
                        "workspace has no executable decompile route; use its "
                        "hash-bound explanatory representation and, when the fixed "
                        "compile route is present, promote reviewed provisional source"
                    )
                    diagnostic = classify_shader_failure(error, phase="decompile")
                    self._append_history(
                        root,
                        manifest,
                        {
                            "operation": "decompile-recovery-decision",
                            "revision": int(manifest["revision"]),
                            "status": "not-routable",
                            "diagnostic": diagnostic,
                            "recoveryPlanSha256": (
                                manifest.get("recoveryPlan") or {}
                            ).get("planSha256"),
                        },
                    )
                    raise error
                return self._decompile_route_chain(root, manifest, destination)
            operation = self._bound_operation(manifest, "decompiler", "decompile")
            revision = int(manifest["revision"])
            job_dir = self._new_job_path(root, "decompile", revision)
            job_dir.mkdir(parents=False, exist_ok=False)
            input_path = job_dir / "input.bin"
            output_path = job_dir / "decompiled.hlsl"
            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            atomic_write_bytes(input_path, baseline_path.read_bytes())
            values = self._template_values(
                root, manifest, revision, job_dir, input_path, output_path, input_path
            )
            result = run_operation(operation, values, job_dir)
            history = self._history_entry(root, operation, result, revision)
            try:
                self._require_process_success(result)
                self._require_job_output(job_dir, output_path, "decompiler")
                source = _source_bytes(output_path)
                atomic_write_bytes(destination, source)
                history.update(
                    {
                        "status": "succeeded",
                        "outputPath": _relative(root, output_path),
                        "outputSha256": hashlib.sha256(source).hexdigest(),
                        "publishedSourcePath": _relative(root, destination),
                    }
                )
                manifest["source"] = {
                    "path": _relative(root, destination),
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "encoding": "utf-8",
                    "lane": "executable-editable",
                    "origin": "trusted-decompiler",
                    "compileEligible": True,
                    "semanticEquivalenceVerified": False,
                    "producedByJob": _relative(root, job_dir),
                    "baselineRawSha256": manifest["baseline"]["rawSha256"],
                }
                self._append_history(root, manifest, history)
            except Exception as exc:
                history.update({"status": "failed", "error": str(exc)})
                self._append_history(root, manifest, history)
                raise
            return {
                "workspacePath": str(root),
                "sourcePath": str(destination),
                "sourceSha256": hashlib.sha256(source).hexdigest(),
                "jobPath": str(job_dir),
                "stdoutPath": str(result.stdout_path),
                "stderrPath": str(result.stderr_path),
                "toolId": operation.tool_id,
                "revision": revision,
                "requiresReplayAbiValidation": True,
            }

    def representation(
        self,
        workspace_path: str | Path,
        *,
        bridge_response: Mapping[str, Any] | None = None,
        bridge_event_id: int | None = None,
        retry_decompile: bool = False,
    ) -> dict:
        """Return a non-empty, provenance-bound representation of the shader.

        A successfully reconstructed HLSL file occupies the executable-editable
        lane.  If that route fails, RenderDoc disassembly is published as
        explanatory-ir.  When even RenderDoc has no usable target, a bounded
        byte view plus the complete hash-verified baseline remains available in
        the binary-only lane.  Neither fallback can be compiled or applied.
        """

        root = self._workspace_root(workspace_path)
        status = self.status(root)
        current = status.get("representation")
        if isinstance(current, Mapping) and current.get("lane") == "executable-editable":
            return dict(current)
        if (
            isinstance(current, Mapping)
            and current.get("present") is True
            and not retry_decompile
            and bridge_response is None
        ):
            return dict(current)

        diagnostics: list[dict[str, str]] = []
        fallback_already_exists = bool(
            isinstance(current, Mapping)
            and current.get("lane") in {"explanatory-ir", "binary-only"}
        )
        if fallback_already_exists:
            existing_diagnostics = current.get("diagnostics")
            if isinstance(existing_diagnostics, list):
                diagnostics.extend(
                    item for item in existing_diagnostics if isinstance(item, Mapping)
                )
        if not (fallback_already_exists and bridge_response is not None):
            try:
                decompiled = self.decompile(root)
            except (
                ToolExecutionError,
                ToolConfigurationError,
                ToolchainRoutingError,
            ) as exc:
                classified = classify_shader_failure(exc, phase="decompile")
                diagnostics.append(
                    bounded_diagnostic("external-decompile-failed", exc)
                )
                diagnostics.append(
                    bounded_diagnostic(
                        "recovery-category:%s" % classified["category"],
                        ", ".join(classified["allowedActions"]),
                    )
                )
            else:
                refreshed = self.status(root)
                representation = refreshed.get("representation")
                if not isinstance(representation, Mapping):
                    raise ManifestValidationError(
                        "decompile succeeded without an executable representation"
                    )
                result = dict(representation)
                result["decompileResult"] = decompiled
                # Preserve the historical decompile response fields for existing
                # Companion clients while publishing the explicit lane contract.
                result.update(decompiled)
                return result

        with _lock_for(root), _WorkspaceFileLock(root):
            manifest = self._load_manifest(root)
            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            return publish_fallback_representation(
                root,
                baseline_path=baseline_path,
                baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                shader=manifest["shader"],
                stage=manifest["stage"],
                entry_point=manifest["entryPoint"],
                target_profile=str(manifest["targetProfile"]["profile"]),
                bridge_response=bridge_response,
                bridge_event_id=bridge_event_id,
                diagnostics=diagnostics,
            )

    def promote_explanatory_source(
        self,
        workspace_path: str | Path,
        *,
        expected_representation_sha256: str,
        expected_source_sha256: str,
        acknowledge_semantic_equivalence_unverified: bool,
    ) -> dict:
        """Promote a reviewed fixed-language candidate into the source lane.

        Promotion is an explicit transaction, not a claim of equivalence.  It
        copies only a hash-bound fixed file and still leaves compilation, exact
        replay ABI validation, and replay apply as separate later operations.
        """

        if acknowledge_semantic_equivalence_unverified is not True:
            raise ToolExecutionError(
                "promotion requires explicit acknowledgement that semantic "
                "equivalence is unverified"
            )
        root = self._workspace_root(workspace_path)
        with _lock_for(root), _WorkspaceFileLock(root):
            manifest = self._load_manifest(root)
            editable = _editable_format(manifest)
            if editable not in TEXT_FORMATS or editable not in _EDITABLE_SOURCE_NAMES:
                raise ToolExecutionError(
                    "explanatory source promotion requires a registered editable "
                    "text-format compile route"
                )
            plan = manifest.get("routePlan")
            if isinstance(plan, Mapping) and not plan.get("compile"):
                raise ToolExecutionError(
                    "workspace has no fixed compiler route; provisional source "
                    "may be kept as explanation but cannot enter the executable lane"
                )
            destination = _source_path(root, manifest)
            if manifest.get("source") is not None or destination.exists() or destination.is_symlink():
                raise ToolExecutionError("workspace already contains a tracked executable source")
            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            representation = load_fallback_representation(
                root,
                expected_baseline_path=baseline_path,
                expected_baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                expected_shader=manifest["shader"],
                expected_stage=manifest["stage"],
                expected_entry_point=manifest["entryPoint"],
                expected_route_plan=manifest.get("routePlan"),
                expected_history=manifest.get("history", ()),
                expected_revision=manifest.get("revision"),
            )
            if representation is None:
                raise ToolExecutionError("publish an explanatory/binary representation first")
            if representation.get("sha256") != str(expected_representation_sha256).lower():
                raise ToolExecutionError("representation changed before promotion")
            suffix = Path(_EDITABLE_SOURCE_NAMES[editable]).suffix
            candidate_path = root / "explanation" / ("promoted" + suffix)
            explanation_root = (root / "explanation").resolve()
            candidate = candidate_path.resolve()
            if (
                not is_within(explanation_root, candidate)
                or not candidate.is_file()
                or candidate.is_symlink()
            ):
                raise ToolExecutionError(
                    "write the reviewed candidate to %s first"
                    % candidate_path.relative_to(root).as_posix()
                )
            source = _source_bytes(candidate)
            source_hash = hashlib.sha256(source).hexdigest()
            if source_hash != str(expected_source_sha256).lower():
                raise ToolExecutionError("promoted HLSL hash does not match the reviewed content")
            if destination.parent.is_symlink() or destination.is_symlink():
                raise ToolExecutionError("workspace executable source path is redirected")
            atomic_write_bytes(destination, source)
            now = _utc_now()
            manifest["source"] = {
                "path": _relative(root, destination),
                "sha256": source_hash,
                "encoding": "utf-8",
                "lane": "executable-editable",
                "origin": "explicit-explanatory-promotion",
                "compileEligible": True,
                "semanticEquivalenceVerified": False,
                "baselineRawSha256": manifest["baseline"]["rawSha256"],
                "promotion": {
                    "at": now,
                    "representationLane": representation["lane"],
                    "representationSha256": representation["sha256"],
                    "reviewedSourcePath": _relative(root, candidate),
                    "reviewedSourceSha256": source_hash,
                    "semanticEquivalenceAcknowledgedUnverified": True,
                },
            }
            self._append_history(
                root,
                manifest,
                {
                    "operation": "promote-explanatory-source",
                    "revision": manifest["revision"],
                    "status": "succeeded",
                    "representationLane": representation["lane"],
                    "representationSha256": representation["sha256"],
                    "sourceSha256": source_hash,
                    "semanticEquivalenceVerified": False,
                },
            )
            return {
                "workspacePath": str(root),
                "sourcePath": str(destination),
                "sourceSha256": source_hash,
                "lane": "executable-editable",
                "compileEligible": True,
                "compiled": False,
                "applied": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
                "requiresCompile": True,
                "requiresReplayAbiValidation": True,
                "requiresExplicitApplyAuthorization": True,
            }

    def compile(self, workspace_path: str | Path) -> dict:
        root = self._workspace_root(workspace_path)
        with _lock_for(root), _WorkspaceFileLock(root):
            try:
                manifest = self._load_manifest(root)
            except ManifestValidationError as exc:
                manifest = self._load_manifest_recovering_interrupted_compile(
                    root, exc
                )
            source_record = manifest.get("source")
            if (
                not isinstance(source_record, Mapping)
                or source_record.get("lane") != "executable-editable"
                or source_record.get("compileEligible") is not True
                or source_record.get("semanticEquivalenceVerified") is not False
            ):
                raise ToolExecutionError(
                    "only the explicit executable-editable source lane may be compiled; "
                    "explanatory IR and binary views must be promoted first"
                )
            source_path = _source_path(root, manifest)
            if not is_within(root, source_path.resolve()):
                raise ToolExecutionError("shader source escapes the workspace")
            source = _source_bytes(source_path)
            if manifest.get("routePlan") is not None:
                return self._compile_route_chain(root, manifest, source)
            operation = self._bound_operation(manifest, "compiler", "compile")
            next_revision = int(manifest["revision"]) + 1
            job_dir = self._new_job_path(root, "compile", next_revision)
            job_dir.mkdir(parents=False, exist_ok=False)
            input_path = job_dir / "input.hlsl"
            output_path = job_dir / "compiled.bin"
            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            atomic_write_bytes(input_path, source)
            atomic_write_bytes(job_dir / "baseline.bin", baseline_path.read_bytes())
            job_baseline = job_dir / "baseline.bin"
            values = self._template_values(
                root,
                manifest,
                next_revision,
                job_dir,
                input_path,
                output_path,
                job_baseline,
            )
            result = run_operation(operation, values, job_dir)
            history = self._history_entry(root, operation, result, next_revision)
            try:
                self._require_process_success(result)
                self._require_job_output(job_dir, output_path, "compiler")
                output_size = output_path.stat().st_size
                if output_size <= 0:
                    raise ToolExecutionError("compiler produced a zero-byte output")
                if output_size > MAX_COMPILED_BYTES:
                    raise ToolExecutionError(
                        "compiler output exceeds %d bytes" % MAX_COMPILED_BYTES
                    )
                compiled = output_path.read_bytes()
                container = self._validated_compiled_container(
                    compiled, manifest, str(output_path)
                )
                revision_dir = root / "revisions" / ("%04d" % next_revision)
                if revision_dir.exists():
                    raise ManifestValidationError(
                        "revision artifact already exists: %s" % revision_dir
                    )
                revision_staging = (
                    root
                    / "revisions"
                    / (".%04d.staging-%s" % (next_revision, uuid.uuid4().hex))
                )
                revision_staging.mkdir(parents=False, exist_ok=False)
                try:
                    atomic_write_bytes(revision_staging / "shader.bin", compiled)
                    staged_revision_source = _revision_source_path(
                        revision_staging, manifest
                    )
                    atomic_write_bytes(staged_revision_source, source)
                    revision_staging.replace(revision_dir)
                except Exception:
                    shutil.rmtree(revision_staging, ignore_errors=True)
                    raise
                revision_blob = revision_dir / "shader.bin"
                revision_source = _revision_source_path(revision_dir, manifest)
                candidate = {
                    "revision": next_revision,
                    "blobPath": _relative(root, revision_blob),
                    "sourcePath": _relative(root, revision_source),
                    "sourceSha256": hashlib.sha256(source).hexdigest(),
                    "rawSha256": hashlib.sha256(compiled).hexdigest(),
                    "codeSha256": container.code_sha256,
                    "byteSize": len(compiled),
                    "kind": container.kind,
                    "toolId": operation.tool_id,
                    "toolConfigDigest": operation.config_digest,
                    "executableSha256": operation.executable_sha256,
                    "fixedFileHashes": operation.fixed_file_hashes_dict(),
                    "validationScope": "container-structure-only",
                    "requiresReplayAbiValidation": True,
                    "replayAbiValidated": False,
                    "abiCompatible": False,
                    "semanticEquivalenceVerified": False,
                    "abiGate": {
                        "status": _initial_abi_gate_status(manifest),
                        "baselineAbiSha256": manifest["baseline"].get(
                            "abiSha256"
                        ),
                    },
                }
                history.update(
                    {
                        "status": "succeeded",
                        "outputPath": _relative(root, output_path),
                        "candidate": candidate,
                    }
                )
                manifest["revision"] = next_revision
                manifest["latestCompiled"] = candidate
                self._append_history(root, manifest, history)
            except Exception as exc:
                history.update({"status": "failed", "error": str(exc)})
                self._append_history(root, manifest, history)
                raise
            return {
                "workspacePath": str(root),
                "revision": next_revision,
                "sourceSha256": candidate["sourceSha256"],
                "candidateBlobPath": str(revision_blob),
                "rawSha256": candidate["rawSha256"],
                "codeSha256": candidate["codeSha256"],
                "kind": candidate["kind"],
                "jobPath": str(job_dir),
                "stdoutPath": str(result.stdout_path),
                "stderrPath": str(result.stderr_path),
                "lastGoodRevision": manifest["lastGood"]["revision"],
                "requiresReplayAbiValidation": True,
                "replayAbiValidated": False,
            }

    def candidate_reflection_request(self, workspace_path: str | Path) -> dict:
        """Return the exact current-candidate request for the RenderDoc bridge.

        No shader is replaced here.  The caller must send ``result["params"]``
        to the bridge's ``reflect_shader_candidate`` RPC and pass the result to
        :meth:`validate_candidate_reflection`.
        """

        root = self._workspace_root(workspace_path)
        with _lock_for(root), _WorkspaceFileLock(root):
            manifest = self._load_manifest(root)
            baseline_abi = self._manifest_baseline_abi(manifest)
            if baseline_abi is None:
                raise AbiValidationError(
                    "the capture catalog has no complete baseline reflection; "
                    "candidate ABI validation is fail-closed"
                )
            return self._candidate_reflection_request_from_manifest(
                root, manifest, baseline_abi
            )

    def validate_candidate_reflection(
        self, workspace_path: str | Path, response: Mapping[str, Any]
    ) -> dict:
        """Validate one bridge reflection and promote only an exact ABI match.

        An incompatible but well-formed reflection returns a bounded difference
        report and leaves the candidate pending.  Malformed or incomplete
        bridge evidence raises :class:`AbiValidationError`.
        """

        root = self._workspace_root(workspace_path)
        with _lock_for(root), _WorkspaceFileLock(root):
            manifest = self._load_manifest(root)
            baseline_abi = self._manifest_baseline_abi(manifest)
            if baseline_abi is None:
                raise AbiValidationError(
                    "the capture catalog has no complete baseline reflection; "
                    "candidate ABI validation is fail-closed"
                )
            request = self._candidate_reflection_request_from_manifest(
                root, manifest, baseline_abi
            )
            self._ensure_dedicated_baseline_dxil_disassembly(root, manifest)
            baseline_access, candidate_access = self._dxil_access_evidence(
                root, manifest
            )
            candidate_abi, comparison, evidence = (
                validate_candidate_reflection_response(
                    request,
                    response,
                    baseline_abi,
                    baseline_dxil_access=baseline_access,
                    candidate_dxil_access=candidate_access,
                )
            )
            result = {
                "workspacePath": str(root),
                "revision": manifest["revision"],
                "requestId": request["requestId"],
                **comparison.to_dict(),
                "requiresReplayAbiValidation": not comparison.compatible,
                "replayAbiValidated": comparison.compatible,
                "applied": False,
            }
            if not comparison.compatible:
                # A mismatch is not persisted as success and never changes the
                # candidate's fail-closed pending state.
                return result

            candidate = manifest.get("latestCompiled")
            if not isinstance(candidate, dict):
                raise ManifestValidationError("latestCompiled is missing")
            existing_gate = candidate.get("abiGate")
            if (
                isinstance(existing_gate, dict)
                and existing_gate.get("status") == "compatible"
            ):
                # Apply must obtain fresh replay reflection every time, but it
                # need not rewrite the immutable first-success audit report.
                # The response above was fully revalidated before reaching
                # this idempotent branch.
                result["abiReportPath"] = str(
                    self._artifact(root, existing_gate.get("reportPath"))
                )
                result["abiReportSha256"] = existing_gate.get("reportSha256")
                result["alreadyPersisted"] = True
                return result
            revision_dir = root / "revisions" / ("%04d" % manifest["revision"])
            report_path = revision_dir / "abi-report.json"
            if report_path.exists() or report_path.is_symlink():
                raise ManifestValidationError(
                    "ABI report already exists for this revision"
                )
            report = {
                "schema": ABI_REPORT_SCHEMA,
                "schemaVersion": ABI_REPORT_VERSION,
                "workspaceId": manifest.get("workspaceId"),
                "captureId": manifest["capture"].get("captureId"),
                "revision": manifest["revision"],
                "request": request,
                "response": dict(response),
                "evidence": evidence,
                "baselineInterface": baseline_abi.to_dict(),
                "candidateInterface": candidate_abi.to_dict(),
                "comparison": comparison.to_dict(),
            }
            try:
                report_payload_size = len(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise AbiValidationError(
                    "candidate reflection response is not JSON serializable"
                ) from exc
            if report_payload_size > MAX_ABI_REPORT_BYTES:
                raise AbiValidationError(
                    "candidate ABI report exceeds %d bytes"
                    % MAX_ABI_REPORT_BYTES
                )
            atomic_write_json(report_path, report)
            report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
            candidate["validationScope"] = "replay-reflection-abi-compatible"
            candidate["requiresReplayAbiValidation"] = False
            candidate["replayAbiValidated"] = True
            candidate["abiCompatible"] = True
            # ABI equality proves the callable/binding contract only.  It does
            # not prove that changed calculations are semantically equivalent
            # for all inputs.
            candidate["semanticEquivalenceVerified"] = False
            candidate["abiGate"] = {
                "status": "compatible",
                "baselineAbiSha256": baseline_abi.sha256,
                "candidateAbiSha256": candidate_abi.sha256,
                "requestId": request["requestId"],
                "reportPath": _relative(root, report_path),
                "reportSha256": report_hash,
            }
            self._append_history(
                root,
                manifest,
                {
                    "operation": "abi-validate",
                    "revision": manifest["revision"],
                    "status": "succeeded",
                    "requestId": request["requestId"],
                    "reportPath": _relative(root, report_path),
                    "reportSha256": report_hash,
                    "baselineAbiSha256": baseline_abi.sha256,
                    "candidateAbiSha256": candidate_abi.sha256,
                },
            )
            result["abiReportPath"] = str(report_path)
            result["abiReportSha256"] = report_hash
            return result

    def status(self, workspace_path: str | Path) -> dict:
        """Validate and report a workspace without acquiring its operation lock."""

        root = self._workspace_root(workspace_path)
        try:
            manifest = self._load_manifest(root)
        except ManifestValidationError as original_error:
            # Ordinary status remains lock-free. Only the exact historical
            # interrupted-publication defect enters this rare repair path, and
            # the cross-process operation lock prevents racing a live compile.
            with _lock_for(root), _WorkspaceFileLock(root):
                manifest = self._load_manifest_recovering_interrupted_compile(
                    root, original_error
                )
        # Status also verifies that the process-owned registry still resolves to
        # the exact tool definitions bound when this workspace was created.
        if manifest.get("routePlan") is None:
            self._bound_operation(manifest, "decompiler", "decompile")
            self._bound_operation(manifest, "compiler", "compile")

        source_path = _source_path(root, manifest)
        source_record = manifest.get("source")
        source_result = {
            "present": False,
            "path": str(source_path),
            "format": _editable_format(manifest),
            "currentSha256": None,
            "decompiledSha256": None,
            "modifiedSinceDecompile": False,
            "lane": None,
            "origin": None,
            "compileEligible": False,
            "baselineCompileVerified": False,
            "semanticEquivalenceVerified": False,
            "toolchainMigration": None,
        }
        if source_record is None:
            if source_path.exists() or source_path.is_symlink():
                raise ManifestValidationError(
                    "untracked shader source exists in the workspace"
                )
        else:
            if not isinstance(source_record, dict):
                raise ManifestValidationError("workspace source metadata is invalid")
            tracked_path = self._artifact(root, source_record.get("path"))
            if tracked_path != source_path.resolve() or not is_within(root, tracked_path):
                raise ManifestValidationError("workspace source path is invalid")
            try:
                source = _source_bytes(tracked_path)
            except ToolExecutionError as exc:
                raise ManifestValidationError(str(exc)) from exc
            decompiled_hash = source_record.get("sha256")
            current_hash = hashlib.sha256(source).hexdigest()
            if (
                not isinstance(decompiled_hash, str)
                or len(decompiled_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in decompiled_hash
                )
                or source_record.get("encoding") != "utf-8"
                or source_record.get("lane") != "executable-editable"
                or source_record.get("compileEligible") is not True
                or source_record.get("semanticEquivalenceVerified") is not False
                or source_record.get("baselineRawSha256")
                != manifest["baseline"]["rawSha256"]
            ):
                raise ManifestValidationError("workspace source metadata mismatch")
            origin = source_record.get("origin")
            if origin not in {
                "trusted-decompiler",
                "trusted-decompile-route",
                "explicit-explanatory-promotion",
                "toolchain-migration-preserved-source",
            }:
                raise ManifestValidationError("workspace source origin is invalid")
            plan = manifest.get("routePlan")
            baseline_compile_verified = False
            migration_record: Mapping[str, Any] | None = None
            if origin == "trusted-decompile-route":
                if plan is None:
                    raise ManifestValidationError(
                        "routed source is missing its immutable route plan"
                    )
                expected_routes = [
                    hop["capability"]["routeId"]
                    for hop in plan["decompile"]
                ]
                produced_jobs = source_record.get("producedByJobs")
                if (
                    source_record.get("routePlanSha256")
                    != plan.get("planSha256")
                    or source_record.get("decompileRouteIds") != expected_routes
                    or not isinstance(produced_jobs, list)
                    or len(produced_jobs) != len(expected_routes)
                ):
                    raise ManifestValidationError(
                        "workspace routed source provenance mismatch"
                    )
                for relative_job in produced_jobs:
                    self._job_directory(root, relative_job)
                baseline_compile = source_record.get("baselineCompile")
                if baseline_compile is not None:
                    if (
                        source_record.get("baselineCompileVerified") is not True
                        or not isinstance(baseline_compile, Mapping)
                        or set(baseline_compile)
                        != {
                            "routeIds",
                            "producedByJobs",
                            "blobPath",
                            "rawSha256",
                            "codeSha256",
                            "byteSize",
                            "kind",
                            "targetProfile",
                            "replayAbiValidated",
                            "semanticEquivalenceVerified",
                        }
                    ):
                        raise ManifestValidationError(
                            "workspace baseline compile provenance is incomplete"
                        )
                    expected_compile_routes = [
                        hop["capability"]["routeId"] for hop in plan["compile"]
                    ]
                    compile_jobs = baseline_compile.get("producedByJobs")
                    if (
                        baseline_compile.get("routeIds") != expected_compile_routes
                        or not isinstance(compile_jobs, list)
                        or len(compile_jobs) != len(expected_compile_routes)
                        or baseline_compile.get("targetProfile")
                        != manifest.get("targetProfile")
                        or baseline_compile.get("replayAbiValidated") is not False
                        or baseline_compile.get("semanticEquivalenceVerified") is not False
                    ):
                        raise ManifestValidationError(
                            "workspace baseline compile route/profile binding mismatch"
                        )
                    for relative_job in compile_jobs:
                        self._job_directory(root, relative_job)
                    probe_blob = self._artifact(
                        root, baseline_compile.get("blobPath")
                    )
                    compiled_probe = probe_blob.read_bytes()
                    try:
                        probe_container = self._validated_compiled_container(
                            compiled_probe,
                            manifest,
                            "workspace decompiler baseline compile probe",
                        )
                    except ToolExecutionError as exc:
                        raise ManifestValidationError(str(exc)) from exc
                    try:
                        declared_probe_size = int(baseline_compile.get("byteSize"))
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ManifestValidationError(
                            "workspace baseline compile size is invalid"
                        ) from exc
                    if (
                        declared_probe_size != len(compiled_probe)
                        or baseline_compile.get("rawSha256")
                        != hashlib.sha256(compiled_probe).hexdigest()
                        or baseline_compile.get("codeSha256")
                        != probe_container.code_sha256
                        or baseline_compile.get("kind") != probe_container.kind
                    ):
                        raise ManifestValidationError(
                            "workspace baseline compile artifact/hash mismatch"
                        )
                    baseline_compile_verified = True
                elif (
                    source_record.get("baselineCompileVerified") is not None
                    and source_record.get("baselineCompileVerified") is not False
                ):
                    raise ManifestValidationError(
                        "workspace baseline compile verification flag is invalid"
                    )
            elif origin == "toolchain-migration-preserved-source":
                migration_record = self._validate_toolchain_migration_source_record(
                    manifest, source_record
                )
            elif origin == "trusted-decompiler":
                if plan is not None:
                    raise ManifestValidationError(
                        "legacy decompiler source contradicts the route plan"
                    )
                self._job_directory(root, source_record.get("producedByJob"))
            else:
                promotion = source_record.get("promotion")
                if not isinstance(promotion, Mapping) or set(promotion) != {
                    "at",
                    "representationLane",
                    "representationSha256",
                    "reviewedSourcePath",
                    "reviewedSourceSha256",
                    "semanticEquivalenceAcknowledgedUnverified",
                }:
                    raise ManifestValidationError(
                        "promoted source evidence is incomplete"
                    )
                baseline_path = self._artifact(root, manifest["baseline"]["path"])
                representation = load_fallback_representation(
                    root,
                    expected_baseline_path=baseline_path,
                    expected_baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                    expected_shader=manifest["shader"],
                    expected_stage=manifest["stage"],
                    expected_entry_point=manifest["entryPoint"],
                    expected_route_plan=manifest.get("routePlan"),
                    expected_history=manifest.get("history", ()),
                    expected_revision=manifest.get("revision"),
                )
                reviewed_path = self._artifact(root, promotion.get("reviewedSourcePath"))
                if (
                    representation is None
                    or promotion.get("representationLane") != representation.get("lane")
                    or promotion.get("representationSha256") != representation.get("sha256")
                    or promotion.get("reviewedSourceSha256") != current_hash
                    or promotion.get("reviewedSourceSha256")
                    != hashlib.sha256(_source_bytes(reviewed_path)).hexdigest()
                    or promotion.get("semanticEquivalenceAcknowledgedUnverified") is not True
                ):
                    raise ManifestValidationError(
                        "promoted source evidence/hash binding mismatch"
                    )
            source_result = {
                "present": True,
                "path": str(tracked_path),
                "format": _editable_format(manifest),
                "currentSha256": current_hash,
                "decompiledSha256": decompiled_hash,
                "modifiedSinceDecompile": current_hash != decompiled_hash,
                "lane": "executable-editable",
                "origin": origin,
                "compileEligible": True,
                "baselineCompileVerified": baseline_compile_verified,
                "semanticEquivalenceVerified": False,
                "toolchainMigration": (
                    dict(migration_record)
                    if origin == "toolchain-migration-preserved-source"
                    else None
                ),
            }

        latest = manifest.get("latestCompiled")
        latest_result = None
        if isinstance(latest, dict):
            latest_result = dict(latest)
            latest_result["candidateBlobPath"] = str(
                self._artifact(root, latest["blobPath"])
            )
            latest_result["applied"] = False

        if source_result["present"]:
            current_matches_candidate = bool(
                latest_result
                and latest_result.get("sourceSha256")
                == source_result["currentSha256"]
            )
            editable_format = _editable_format(manifest)
            if source_result["origin"] == "explicit-explanatory-promotion":
                representation_kind = "promoted-explanatory-hlsl"
                semantic_diagnostic = (
                    "Promoted explanatory HLSL, successful compilation, and ABI "
                    "equality do not prove identical calculations for all inputs."
                )
            elif editable_format == "hlsl":
                representation_kind = "reconstructed-hlsl"
                semantic_diagnostic = (
                    "Source reconstruction, successful compilation, and ABI equality "
                    "do not prove identical calculations for all shader inputs."
                )
            elif editable_format == "dxbc-asm":
                representation_kind = "editable-dxbc-assembly"
                semantic_diagnostic = (
                    "DXBC assembly is executable instruction text, not HLSL or recovered "
                    "source. Assembly and ABI equality do not prove semantic equivalence."
                )
            else:
                representation_kind = "editable-%s" % editable_format
                semantic_diagnostic = (
                    "Text round-trip and ABI equality do not prove semantic equivalence."
                )
            representation_result = {
                "present": True,
                "lane": "executable-editable",
                "kind": representation_kind,
                "format": editable_format,
                "path": source_result["path"],
                "sha256": source_result["currentSha256"],
                "compileEligible": True,
                "applyEligible": False,
                "baselineCompileVerified": source_result[
                    "baselineCompileVerified"
                ],
                "abiCompatible": bool(
                    current_matches_candidate
                    and latest_result.get("abiCompatible") is True
                ),
                "semanticEquivalenceVerified": False,
                "diagnostics": [
                    bounded_diagnostic(
                        "semantic-equivalence-unverified",
                        semantic_diagnostic,
                    )
                ],
                "provenance": {
                    "kind": "fixed-program",
                    "origin": source_result["origin"],
                    "baselineRawSha256": manifest["baseline"]["rawSha256"],
                    "baselineCompileVerified": source_result[
                        "baselineCompileVerified"
                    ],
                },
            }
        else:
            fallback = load_fallback_representation(
                root,
                expected_baseline_path=self._artifact(root, manifest["baseline"]["path"]),
                expected_baseline_raw_sha256=manifest["baseline"]["rawSha256"],
                expected_shader=manifest["shader"],
                expected_stage=manifest["stage"],
                expected_entry_point=manifest["entryPoint"],
                expected_route_plan=manifest.get("routePlan"),
                expected_history=manifest.get("history", ()),
                expected_revision=manifest.get("revision"),
            )
            representation_result = fallback or {
                "present": False,
                "lane": None,
                "kind": None,
                "path": None,
                "sha256": None,
                "compileEligible": False,
                "applyEligible": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
                "diagnostics": [],
                "provenance": None,
            }

        reconstruction_fidelity = _reconstruction_fidelity_status(
            manifest,
            source_record if isinstance(source_record, Mapping) else None,
            source_result,
            latest_result,
        )

        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(root),
            "manifestPath": str(root / "manifest.json"),
            "workspaceId": manifest.get("workspaceId"),
            "capture": manifest.get("capture"),
            "shader": manifest.get("shader"),
            "stage": manifest.get("stage"),
            "entryPoint": manifest.get("entryPoint"),
            "targetProfile": manifest.get("targetProfile"),
            "routePlan": manifest.get("routePlan"),
            "recoveryPlan": manifest.get("recoveryPlan"),
            "revision": manifest["revision"],
            "source": source_result,
            "representation": representation_result,
            "reconstructionFidelity": reconstruction_fidelity,
            "baseline": manifest["baseline"],
            "lastGood": manifest["lastGood"],
            "latestCompiled": latest_result,
            "historyCount": len(manifest["history"]),
            "workspaceLocked": (root / ".workspace-operation.lock").exists(),
            "requiresReplayAbiValidation": bool(
                latest_result
                and latest_result.get("requiresReplayAbiValidation") is True
            ),
            "replayAbiValidated": bool(
                latest_result and latest_result.get("replayAbiValidated") is True
            ),
            "abiCompatible": bool(
                latest_result and latest_result.get("abiCompatible") is True
            ),
            "semanticEquivalenceVerified": False,
            "applied": False,
        }

    def _workspace_root(self, workspace_path: str | Path) -> Path:
        root = Path(workspace_path).expanduser().resolve()
        if not root.is_dir():
            raise ManifestValidationError("workspace is not a directory: %s" % root)
        return root

    def _validate_route_plan(
        self,
        manifest: Mapping[str, Any],
        original_format: str,
        *,
        require_process_binding: bool = True,
    ) -> None:
        plan = manifest.get("routePlan")
        if plan is None:
            tools = manifest.get("tools")
            if not isinstance(tools, Mapping) or set(tools) != {
                "decompiler",
                "compiler",
            }:
                raise ManifestValidationError(
                    "legacy workspace must bind exactly one decompiler and compiler"
                )
            return
        if not isinstance(plan, Mapping):
            raise ManifestValidationError("workspace routePlan must be an object")
        required = {
            "schema",
            "schemaVersion",
            "routeCatalogPath",
            "routeCatalogSha256",
            "toolRegistryPath",
            "toolRegistrySha256",
            "inputFormat",
            "editableFormat",
            "stage",
            "decompile",
            "compile",
            "planSha256",
        }
        if set(plan) != required:
            raise ManifestValidationError("workspace routePlan fields are invalid")
        if (
            plan.get("schema") != ROUTE_PLAN_SCHEMA
            or plan.get("schemaVersion") != ROUTE_PLAN_VERSION
        ):
            raise ManifestValidationError("unsupported workspace route plan")
        unsigned = dict(plan)
        declared_plan_hash = unsigned.pop("planSha256")
        if declared_plan_hash != _canonical_sha256(unsigned):
            raise ManifestValidationError("workspace route plan hash mismatch")
        for field in (
            "routeCatalogSha256",
            "toolRegistrySha256",
            "planSha256",
        ):
            digest = plan.get(field)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ManifestValidationError(
                    "workspace route plan digest is invalid"
                )
        if not all(
            isinstance(plan.get(field), str) and bool(plan.get(field))
            for field in ("routeCatalogPath", "toolRegistryPath")
        ):
            raise ManifestValidationError("workspace route plan path is invalid")
        try:
            editable_format = normalize_format(plan.get("editableFormat"))
        except ToolchainRoutingError as exc:
            raise ManifestValidationError(
                "workspace route plan editable format is invalid"
            ) from exc
        if (
            plan.get("inputFormat") != original_format
            or plan.get("editableFormat") != editable_format
            or editable_format not in TEXT_FORMATS
            or plan.get("stage") != manifest.get("stage")
        ):
            raise ManifestValidationError(
                "workspace route plan format/stage binding mismatch"
            )
        if manifest.get("tools") != {}:
            raise ManifestValidationError(
                "routed workspace must not contain legacy single-tool bindings"
            )
        catalog = None
        routes: dict[str, RouteCapability] = {}
        if require_process_binding:
            if self.route_config_path is None:
                raise ManifestValidationError(
                    "routed workspace requires the process-owned route catalog"
                )
            route_path = Path(
                str(plan.get("routeCatalogPath", ""))
            ).expanduser().resolve()
            if route_path != self.route_config_path:
                raise ManifestValidationError(
                    "workspace route catalog differs from the process-owned path"
                )
            if plan.get("routeCatalogSha256") != _file_sha256(route_path):
                raise ManifestValidationError(
                    "workspace route catalog bytes changed after creation"
                )
            if str(plan.get("toolRegistryPath", "")) != str(self.registry.path):
                raise ManifestValidationError(
                    "workspace route plan tool registry path mismatch"
                )
            catalog = self._process_catalog()
            if plan.get("toolRegistrySha256") != catalog.tool_registry_sha256:
                raise ManifestValidationError(
                    "workspace route plan tool registry hash mismatch"
                )
            routes = {route.route_id: route for route in catalog.routes()}

        decompile_values = plan.get("decompile")
        compile_values = plan.get("compile")
        if not isinstance(decompile_values, list) or not isinstance(
            compile_values, list
        ):
            raise ManifestValidationError("workspace route paths must be arrays")
        if len(decompile_values) > 4 or len(compile_values) > 4:
            raise ManifestValidationError(
                "workspace route paths may contain at most 4 hops"
            )
        def validate_path(
            name: str, source: str, target: str, *, allow_empty: bool
        ) -> None:
            values = plan.get(name)
            if not isinstance(values, list) or (not values and not allow_empty):
                raise ManifestValidationError(
                    "workspace %s route path has an invalid hop count" % name
                )
            if not values:
                return
            current = source
            for index, hop in enumerate(values):
                if not isinstance(hop, Mapping) or set(hop) != {
                    "hop",
                    "capability",
                    "operation",
                }:
                    raise ManifestValidationError("workspace route hop is invalid")
                capability = hop.get("capability")
                if not isinstance(capability, Mapping):
                    raise ManifestValidationError(
                        "workspace route capability is invalid"
                    )
                capability_fields = {
                    "routeId",
                    "toolId",
                    "operation",
                    "toolConfigDigest",
                    "inputFormat",
                    "outputFormat",
                    "stages",
                    "fidelity",
                    "priority",
                    "implementation",
                    "sourceUrl",
                    "license",
                    "caveats",
                    "requiresReplayAbiValidation",
                }
                operation_binding = hop.get("operation")
                operation_fields = {
                    "toolId",
                    "operation",
                    "configDigest",
                    "executableSha256",
                    "fixedFileHashes",
                    "placeholders",
                    "timeoutSeconds",
                }
                route_id = capability.get("routeId")
                stages = capability.get("stages")
                fixed_hashes = (
                    operation_binding.get("fixedFileHashes")
                    if isinstance(operation_binding, Mapping)
                    else None
                )
                placeholders = (
                    operation_binding.get("placeholders")
                    if isinstance(operation_binding, Mapping)
                    else None
                )
                if not isinstance(operation_binding, Mapping):
                    raise ManifestValidationError(
                        "workspace route operation binding is invalid"
                    )
                try:
                    capability_input = normalize_format(
                        capability.get("inputFormat")
                    )
                    capability_output = normalize_format(
                        capability.get("outputFormat")
                    )
                    timeout = float(operation_binding.get("timeoutSeconds"))
                except (ToolchainRoutingError, TypeError, ValueError, OverflowError) as exc:
                    raise ManifestValidationError(
                        "workspace route hop binding is invalid"
                    ) from exc
                if (
                    set(capability) != capability_fields
                    or not isinstance(route_id, str)
                    or not route_id
                    or capability.get("inputFormat") != capability_input
                    or capability.get("outputFormat") != capability_output
                    or not isinstance(stages, list)
                    or not stages
                    or any(not isinstance(item, str) for item in stages)
                    or (
                        "*" not in stages
                        and str(manifest.get("stage", "")) not in stages
                    )
                    or set(operation_binding) != operation_fields
                    or operation_binding.get("toolId") != capability.get("toolId")
                    or operation_binding.get("operation")
                    != capability.get("operation")
                    or operation_binding.get("configDigest")
                    != capability.get("toolConfigDigest")
                    or not isinstance(fixed_hashes, Mapping)
                    or any(
                        not isinstance(path, str)
                        or not isinstance(digest, str)
                        or len(digest) != 64
                        or any(ch not in "0123456789abcdef" for ch in digest)
                        for path, digest in fixed_hashes.items()
                    )
                    or not isinstance(placeholders, list)
                    or any(not isinstance(item, str) for item in placeholders)
                    or timeout <= 0
                    or not isinstance(
                        capability.get("requiresReplayAbiValidation"), bool
                    )
                    or capability_input != current
                ):
                    raise ManifestValidationError(
                        "workspace route hop binding is inconsistent"
                    )
                for digest in (
                    capability.get("toolConfigDigest"),
                    operation_binding.get("configDigest"),
                    operation_binding.get("executableSha256"),
                ):
                    if (
                        not isinstance(digest, str)
                        or len(digest) != 64
                        or any(ch not in "0123456789abcdef" for ch in digest)
                    ):
                        raise ManifestValidationError(
                            "workspace route hop digest is invalid"
                        )
                route = routes.get(route_id) if require_process_binding else None
                if require_process_binding and (
                    route is None or capability != route.as_dict()
                ):
                    raise ManifestValidationError(
                        "workspace route capability changed after creation"
                    )
                if (
                    hop.get("hop") != index + 1
                    or (
                        require_process_binding
                        and hop.get("operation")
                        != _operation_binding(
                            self.registry.operation(route.tool_id, route.operation)
                        )
                    )
                ):
                    raise ManifestValidationError(
                        "workspace route hop binding is inconsistent"
                    )
                current = capability_output
            if current != target:
                raise ManifestValidationError(
                    "workspace route path does not reach its target format"
                )

        validate_path(
            "decompile", original_format, editable_format, allow_empty=True
        )
        validate_path("compile", editable_format, original_format, allow_empty=True)

    def _validate_recovery_plan(self, manifest: Mapping[str, Any]) -> None:
        """Validate immutable program/AI recovery boundaries for new workspaces."""

        recovery = manifest.get("recoveryPlan")
        # Workspaces created before recovery planning remain valid. Their route
        # plan still enforces the original strict round-trip contract.
        if recovery is None:
            return
        if not isinstance(recovery, Mapping):
            raise ManifestValidationError("workspace recoveryPlan must be an object")
        required = {
            "schema",
            "schemaVersion",
            "mode",
            "diagnostics",
            "allowedActions",
            "decompileCandidates",
            "compileCandidates",
            "configuredButUnavailable",
            "aiPolicy",
            "sourceConstructionEligible",
            "assetSourceSeedEligibleAfterVerification",
            "directReplayApplyAuthorized",
            "semanticEquivalenceVerified",
            "planSha256",
        }
        if set(recovery) != required:
            raise ManifestValidationError("workspace recoveryPlan fields are invalid")
        if (
            recovery.get("schema")
            != "renderdoc-ultra-shader-workspace-recovery/v1"
            or recovery.get("schemaVersion") != 1
        ):
            raise ManifestValidationError("unsupported workspace recovery plan")
        unsigned = dict(recovery)
        declared_hash = unsigned.pop("planSha256")
        if declared_hash != _canonical_sha256(unsigned):
            raise ManifestValidationError("workspace recovery plan hash mismatch")
        route = manifest.get("routePlan")
        if not isinstance(route, Mapping):
            raise ManifestValidationError(
                "workspace recovery plan requires a fixed routed workspace"
            )
        decompile = route.get("decompile")
        compile_path = route.get("compile")
        if not isinstance(decompile, list) or not isinstance(compile_path, list):
            raise ManifestValidationError("workspace recovery route paths are invalid")
        expected_mode = (
            "round-trip"
            if decompile and compile_path
            else "compile-only-recovery"
            if compile_path
            else "evidence-only"
        )
        if recovery.get("mode") != expected_mode:
            raise ManifestValidationError(
                "workspace recovery mode contradicts its fixed route plan"
            )
        if (
            recovery.get("sourceConstructionEligible") is not bool(compile_path)
            or recovery.get("assetSourceSeedEligibleAfterVerification")
            is not bool(compile_path)
            or recovery.get("directReplayApplyAuthorized") is not False
            or recovery.get("semanticEquivalenceVerified") is not False
        ):
            raise ManifestValidationError("workspace recovery safety policy is invalid")
        for field in ("diagnostics", "allowedActions"):
            values = recovery.get(field)
            if not isinstance(values, list) or len(values) > 128 or any(
                not isinstance(item, Mapping) for item in values
            ):
                raise ManifestValidationError(
                    "workspace recovery %s is invalid" % field
                )
        for field in ("decompileCandidates", "compileCandidates"):
            candidates = recovery.get(field)
            if not isinstance(candidates, list) or len(candidates) > 128:
                raise ManifestValidationError(
                    "workspace recovery %s is invalid" % field
                )
            if any(
                not isinstance(path, list)
                or len(path) > 4
                or any(not isinstance(route_id, str) for route_id in path)
                for path in candidates
            ):
                raise ManifestValidationError(
                    "workspace recovery route candidates are invalid"
                )
        if not isinstance(recovery.get("configuredButUnavailable"), Mapping) or not isinstance(
            recovery.get("aiPolicy"), Mapping
        ):
            raise ManifestValidationError("workspace recovery policy metadata is invalid")

    def _load_manifest(
        self,
        root: Path,
        *,
        allow_stale_route_bindings: bool = False,
        validate_latest_candidate: bool = True,
        allow_interrupted_candidate_recovery: bool = False,
    ) -> dict:
        if not validate_latest_candidate and not (
            allow_stale_route_bindings or allow_interrupted_candidate_recovery
        ):
            raise ManifestValidationError(
                "candidate validation may be relaxed only for stale-route migration "
                "or strict interrupted-publication recovery"
            )
        try:
            manifest = read_json_object(
                root / "manifest.json", MAX_MANIFEST_BYTES, "workspace manifest"
            )
        except ValueError as exc:
            raise ManifestValidationError(str(exc)) from exc
        if (
            manifest.get("schema") != WORKSPACE_SCHEMA
            or manifest.get("schemaVersion") != WORKSPACE_VERSION
        ):
            raise ManifestValidationError(
                "unsupported workspace manifest schema/version"
            )
        if not isinstance(manifest.get("tools"), dict):
            raise ManifestValidationError("workspace tools are missing")
        if not isinstance(manifest.get("baseline"), dict):
            raise ManifestValidationError("workspace baseline is missing")
        try:
            revision = int(manifest.get("revision"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestValidationError("workspace revision is invalid") from exc
        if revision < 0:
            raise ManifestValidationError("workspace revision is invalid")
        manifest["revision"] = revision
        baseline_path = self._artifact(root, manifest["baseline"].get("path"))
        baseline = baseline_path.read_bytes()
        baseline_hash = hashlib.sha256(baseline).hexdigest()
        if baseline_hash != manifest["baseline"].get("rawSha256"):
            raise ManifestValidationError("workspace baseline hash mismatch")
        capture = manifest.get("capture")
        capture_api = capture.get("api", "") if isinstance(capture, Mapping) else ""
        target = manifest.get("targetProfile")
        target_format = (
            str(target.get("containerFormat", ""))
            if isinstance(target, Mapping)
            else ""
        )
        container = fingerprint_shader_executable(
            baseline,
            str(baseline_path),
            format_hint=target_format or None,
        )
        if container is None or container.byte_size != len(baseline):
            raise ManifestValidationError(
                "workspace baseline is not a valid supported shader executable"
            )
        baseline_metadata = manifest["baseline"]
        try:
            baseline_size = int(baseline_metadata.get("byteSize"))
            baseline_revision = int(baseline_metadata.get("revision"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestValidationError(
                "workspace baseline metadata is invalid"
            ) from exc
        if (
            baseline_size != len(baseline)
            or baseline_revision != 0
            or baseline_metadata.get("codeSha256") != container.code_sha256
            or baseline_metadata.get("kind") != container.kind
            or baseline_metadata.get("validationScope") != "captured-baseline"
            or baseline_metadata.get("requiresReplayAbiValidation") is not False
        ):
            raise ManifestValidationError("workspace baseline metadata mismatch")
        shader = manifest.get("shader")
        if not isinstance(shader, dict):
            raise ManifestValidationError("workspace shader key is missing")
        key = ShaderKey.from_value(shader)
        if key.raw_sha256 != baseline_hash:
            raise ManifestValidationError("shader key and baseline hash differ")
        if (
            manifest.get("stage") != key.stage
            or manifest.get("entryPoint") != key.entry_point
        ):
            raise ManifestValidationError(
                "workspace stage/entryPoint does not match shader key"
            )
        try:
            exact_profile = _physical_target_profile(
                baseline,
                key.stage,
                executable_format(container),
                str(capture_api),
            )
        except ToolchainRoutingError as exc:
            raise ManifestValidationError(
                "workspace target profile cannot be verified: %s" % exc
            ) from exc
        if manifest.get("targetProfile") != exact_profile:
            raise ManifestValidationError(
                "workspace targetProfile does not match the executable token"
            )
        self._validate_route_plan(
            manifest,
            exact_profile["containerFormat"],
            require_process_binding=not allow_stale_route_bindings,
        )
        self._validate_recovery_plan(manifest)
        baseline_abi = self._manifest_baseline_abi(manifest)
        self._validate_catalog_provenance(
            manifest, key, baseline, baseline_metadata, container, baseline_abi
        )
        if not isinstance(manifest.get("history"), list):
            raise ManifestValidationError("workspace history must be an array")
        if manifest.get("lastGood") != baseline_metadata:
            raise ManifestValidationError(
                "lastGood must remain the captured baseline until replay validation"
            )
        if validate_latest_candidate:
            self._validate_latest_candidate(root, manifest, revision, baseline_abi)
        return manifest

    @staticmethod
    def _failed_revision_publish_matches(
        root: Path,
        entry: Mapping[str, Any],
        failed_revision: int,
        active_record: Mapping[str, Any],
    ) -> bool:
        """Recognise only the legacy Windows staging-rename failure shape."""

        if (
            entry.get("operation") != "compile-route-chain"
            or entry.get("status") != "failed"
            or entry.get("revision") != failed_revision
            or entry.get("candidateDxilDisassemblyEvidence") != active_record
            or "candidate" in entry
        ):
            return False
        error = str(entry.get("error", ""))
        if not any(
            token in error
            for token in ("[WinError 5]", "[WinError 32]", "[WinError 33]")
        ):
            return False
        normalised = error.replace("\\", "/").casefold()
        # OSError renders filenames through repr(), so Windows separators may
        # appear doubled in the diagnostic even though the bound Path is not.
        while "//" in normalised:
            normalised = normalised.replace("//", "/")
        revisions = str((root / "revisions").resolve()).replace("\\", "/").casefold()
        staging_fragment = f"{revisions}/.{failed_revision:04d}.staging-"
        destination = f"{revisions}/{failed_revision:04d}"
        return (
            staging_fragment in normalised
            and destination in normalised
            and not (root / "revisions" / f"{failed_revision:04d}").exists()
        )

    def _recover_interrupted_candidate_dxil_evidence(
        self, root: Path, manifest: dict
    ) -> bool:
        """Repair only the pre-transaction DXIL evidence overwrite defect.

        Historical manifests could publish candidate DXIL-disassembly metadata
        before the revision directory rename.  A transient Windows rename
        failure then left a compatible revision N and its immutable ABI report
        paired with evidence for failed revision N+1.  Recovery accepts no
        evidence from the failed revision.  It restores the unique record from
        revision N's successful compile history and then runs the complete
        candidate, fixed-tool, artifact, access-signature and ABI-report
        validation before writing anything.
        """

        revision = manifest.get("revision")
        candidate = manifest.get("latestCompiled")
        collection = manifest.get("dxilDisassemblyEvidence")
        history = manifest.get("history")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(candidate, Mapping)
            or candidate.get("revision") != revision
            or not isinstance(candidate.get("abiGate"), Mapping)
            or candidate["abiGate"].get("status") != "compatible"
            or not isinstance(collection, Mapping)
            or set(collection)
            != {"schema", "schemaVersion", "baseline", "candidate"}
            or collection.get("schema") != DXIL_DISASSEMBLY_EVIDENCE_SCHEMA
            or collection.get("schemaVersion")
            != DXIL_DISASSEMBLY_EVIDENCE_VERSION
            or not isinstance(history, list)
        ):
            return False
        active_record = collection.get("candidate")
        if not isinstance(active_record, Mapping):
            return False
        failed_revision = revision + 1
        if (
            active_record.get("revision") != failed_revision
            or active_record.get("evidenceRole") != "candidate"
            or active_record.get("status") != "succeeded"
            or active_record.get("inputArtifactPath")
            != f"revisions/{failed_revision:04d}/shader.bin"
        ):
            return False

        expected_pending_candidate = deepcopy(dict(candidate))
        expected_pending_candidate.update(
            {
                "validationScope": "container-structure-only",
                "requiresReplayAbiValidation": True,
                "replayAbiValidated": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
                "abiGate": {
                    "status": _initial_abi_gate_status(manifest),
                    "baselineAbiSha256": manifest.get("baseline", {}).get(
                        "abiSha256"
                    ),
                },
            }
        )
        successful: list[tuple[int, Mapping[str, Any]]] = []
        failed: list[tuple[int, Mapping[str, Any]]] = []
        for index, entry in enumerate(history):
            if not isinstance(entry, Mapping):
                continue
            if (
                entry.get("operation") == "compile-route-chain"
                and entry.get("status") == "succeeded"
                and entry.get("revision") == revision
                and entry.get("routePlanSha256")
                == manifest.get("routePlan", {}).get("planSha256")
                and entry.get("outputSha256") == candidate.get("rawSha256")
                and entry.get("candidate") == expected_pending_candidate
                and isinstance(
                    entry.get("candidateDxilDisassemblyEvidence"), Mapping
                )
            ):
                successful.append((index, entry))
            if self._failed_revision_publish_matches(
                root, entry, failed_revision, active_record
            ):
                failed.append((index, entry))
        if len(successful) != 1 or len(failed) != 1:
            return False
        success_index, success_entry = successful[0]
        failed_index, failed_entry = failed[0]
        if success_index >= failed_index or any(
            isinstance(entry, Mapping)
            and entry.get("operation") in {"compile-route-chain", "abi-validate"}
            for entry in history[failed_index + 1 :]
        ):
            return False

        restored_record = success_entry["candidateDxilDisassemblyEvidence"]
        repaired = deepcopy(manifest)
        repaired["dxilDisassemblyEvidence"]["candidate"] = deepcopy(
            restored_record
        )
        baseline_abi = self._manifest_baseline_abi(repaired)
        # This replays every immutable check, including the fixed route/tool
        # hashes, job-local bytes/command, physical access signature and the
        # existing ABI report's exact comparison evidence.
        self._validate_latest_candidate(root, repaired, revision, baseline_abi)
        if self._dedicated_dxil_asm_evidence(
            root, repaired, "candidate"
        ) is None:
            return False

        manifest.clear()
        manifest.update(repaired)
        self._append_history(
            root,
            manifest,
            {
                "operation": "interrupted-compile-evidence-recovery",
                "revision": revision,
                "status": "succeeded",
                "restoredCandidateRawSha256": candidate.get("rawSha256"),
                "restoredEvidenceJobPath": restored_record.get("jobPath"),
                "discardedFailedRevision": failed_revision,
                "discardedEvidenceJobPath": active_record.get("jobPath"),
                "failure": str(failed_entry.get("error", "")),
            },
        )
        return True

    def _load_manifest_recovering_interrupted_compile(
        self, root: Path, original_error: ManifestValidationError
    ) -> dict:
        try:
            manifest = self._load_manifest(
                root,
                validate_latest_candidate=False,
                allow_interrupted_candidate_recovery=True,
            )
            if not self._recover_interrupted_candidate_dxil_evidence(
                root, manifest
            ):
                raise original_error
            return self._load_manifest(root)
        except ManifestValidationError:
            raise original_error

    def _manifest_baseline_abi(self, manifest: Mapping[str, Any]) -> ShaderAbi | None:
        baseline = manifest.get("baseline")
        if not isinstance(baseline, Mapping):
            raise ManifestValidationError("workspace baseline is missing")
        status = baseline.get("abiStatus")
        interface = baseline.get("compiledInterface")
        digest = baseline.get("abiSha256")
        if status == ABI_STATUS_UNAVAILABLE:
            if interface is not None or digest is not None:
                raise ManifestValidationError(
                    "unavailable baseline ABI must not contain interface data"
                )
            return None
        if status not in {
            ABI_STATUS_CAPTURED_COMPLETE,
            ABI_STATUS_DXIL_ACCESS_REQUIRED,
        }:
            raise ManifestValidationError("workspace baseline ABI status is invalid")
        try:
            abi = parse_shader_abi(
                interface,
                expected_stage=str(manifest.get("stage", "")),
                expected_entry_point=str(manifest.get("entryPoint", "")),
            )
        except AbiValidationError as exc:
            raise ManifestValidationError(
                "workspace baseline ABI is invalid: %s" % exc
            ) from exc
        if digest != abi.sha256:
            raise ManifestValidationError("workspace baseline ABI hash mismatch")
        if status == ABI_STATUS_DXIL_ACCESS_REQUIRED:
            if str(baseline.get("kind", "")).upper() != "DXIL":
                raise ManifestValidationError(
                    "DXIL access-required baseline must contain DXIL bytes"
                )
            blocks = abi.constant_blocks
            if (
                not any(
                    block.layout_completeness == "ambiguous" for block in blocks
                )
                or any(
                    block.layout_completeness not in {"complete", "ambiguous"}
                    or block.byte_size <= 0
                    or (
                        block.layout_completeness == "ambiguous"
                        and block.array_size != 1
                    )
                    for block in blocks
                )
            ):
                raise ManifestValidationError(
                    "DXIL access-required baseline is not ambiguity-only"
                )
        return abi

    def _validate_catalog_provenance(
        self,
        manifest: dict,
        key: ShaderKey,
        baseline: bytes,
        baseline_metadata: Mapping[str, Any],
        baseline_container,
        baseline_abi: ShaderAbi | None,
    ) -> None:
        """Re-resolve the captured shader and bind it to this workspace.

        The workspace baseline is only a local copy.  Every operation must
        still prove that the original catalog selects the same capture,
        shader identity, and physical shader bytes that were accepted by
        ``create``.  This deliberately makes a missing or rewritten catalog a
        hard failure instead of silently trusting mutable manifest fields.
        """

        capture = manifest.get("capture")
        if not isinstance(capture, dict):
            raise ManifestValidationError("workspace capture provenance is missing")
        catalog_path = capture.get("catalogPath")
        if not isinstance(catalog_path, str) or not catalog_path:
            raise ManifestValidationError(
                "workspace capture catalogPath is invalid"
            )
        declared_catalog_sha256 = capture.get("catalogSha256")
        if declared_catalog_sha256 is not None and (
            not isinstance(declared_catalog_sha256, str)
            or len(declared_catalog_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in declared_catalog_sha256
            )
        ):
            raise ManifestValidationError(
                "workspace capture catalogSha256 is invalid"
            )
        try:
            canonical_before, catalog_sha256_before = _catalog_file_sha256(
                catalog_path
            )
            selected = load_selected_shader(canonical_before, key)
            canonical_after, catalog_sha256_after = _catalog_file_sha256(
                canonical_before
            )
        except (CatalogValidationError, OSError) as exc:
            raise ManifestValidationError(
                "workspace capture catalog provenance cannot be verified: %s" % exc
            ) from exc

        if (
            canonical_before != canonical_after
            or catalog_sha256_before != catalog_sha256_after
        ):
            raise ManifestValidationError(
                "workspace capture catalog changed during provenance validation"
            )
        if (
            declared_catalog_sha256 is not None
            and catalog_sha256_before != declared_catalog_sha256
        ):
            raise ManifestValidationError(
                "workspace capture catalogSha256 no longer matches the capture catalog"
            )

        if catalog_path != str(selected.catalog_path):
            raise ManifestValidationError(
                "workspace capture catalogPath does not match its canonical path"
            )
        if capture.get("captureId") != selected.capture_id:
            raise ManifestValidationError(
                "workspace captureId no longer matches the capture catalog"
            )
        if (
            capture.get("capturePath") != selected.capture_path
            or capture.get("api") != selected.api
        ):
            raise ManifestValidationError(
                "workspace capture metadata no longer matches the capture catalog"
            )
        if selected.key != key:
            raise ManifestValidationError(
                "workspace shader identity no longer matches the capture catalog"
            )
        if selected.blob != baseline:
            raise ManifestValidationError(
                "workspace baseline bytes no longer match the capture catalog"
            )
        if (
            selected.container.raw_sha256 != baseline_metadata.get("rawSha256")
            or selected.container.code_sha256
            != baseline_metadata.get("codeSha256")
            or selected.container.raw_sha256 != baseline_container.raw_sha256
            or selected.container.code_sha256 != baseline_container.code_sha256
            or selected.container.byte_size != baseline_container.byte_size
            or selected.container.kind != baseline_container.kind
        ):
            raise ManifestValidationError(
                "workspace baseline fingerprint no longer matches the capture catalog"
            )
        if (selected.abi is None) != (baseline_abi is None):
            raise ManifestValidationError(
                "workspace baseline ABI availability no longer matches the capture catalog"
            )
        if selected.abi_status != baseline_metadata.get("abiStatus"):
            raise ManifestValidationError(
                "workspace baseline ABI status no longer matches the capture catalog"
            )
        if (
            selected.abi is not None
            and baseline_abi is not None
            and selected.abi.sha256 != baseline_abi.sha256
        ):
            raise ManifestValidationError(
                "workspace baseline ABI no longer matches the capture catalog"
            )

    def _validate_latest_candidate(
        self,
        root: Path,
        manifest: dict,
        revision: int,
        baseline_abi: ShaderAbi | None,
    ) -> None:
        candidate = manifest.get("latestCompiled")
        if revision == 0:
            if candidate is not None:
                raise ManifestValidationError(
                    "revision 0 workspace must not contain latestCompiled"
                )
            return
        if not isinstance(candidate, dict):
            raise ManifestValidationError("latestCompiled is missing")
        try:
            candidate_revision = int(candidate.get("revision"))
            candidate_size = int(candidate.get("byteSize"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManifestValidationError("latestCompiled metadata is invalid") from exc
        if candidate_revision != revision:
            raise ManifestValidationError("latestCompiled revision mismatch")
        blob_path = self._artifact(root, candidate.get("blobPath"))
        source_path = self._artifact(root, candidate.get("sourcePath"))
        compiled = blob_path.read_bytes()
        try:
            source = _source_bytes(source_path)
        except ToolExecutionError as exc:
            raise ManifestValidationError(str(exc)) from exc
        target = manifest.get("targetProfile")
        target_format = (
            str(target.get("containerFormat", ""))
            if isinstance(target, Mapping)
            else ""
        )
        container = fingerprint_shader_executable(
            compiled, str(blob_path), format_hint=target_format or None
        )
        if container is None or container.byte_size != len(compiled):
            raise ManifestValidationError(
                "latestCompiled is not a valid supported shader executable"
            )
        capture = manifest.get("capture")
        capture_api = capture.get("api", "") if isinstance(capture, Mapping) else ""
        try:
            candidate_profile = _physical_target_profile(
                compiled,
                str(manifest.get("stage", "")),
                executable_format(container),
                str(capture_api),
            )
        except ToolchainRoutingError as exc:
            raise ManifestValidationError(
                "latestCompiled target profile is invalid: %s" % exc
            ) from exc
        if (
            candidate_profile != manifest.get("targetProfile")
            or container.kind != manifest.get("baseline", {}).get("kind")
        ):
            raise ManifestValidationError(
                "latestCompiled physical container/profile differs from baseline"
            )
        abi_gate = candidate.get("abiGate")
        if not isinstance(abi_gate, dict):
            raise ManifestValidationError("latestCompiled ABI gate metadata is missing")
        gate_status = abi_gate.get("status")
        expected_baseline_digest = (
            baseline_abi.sha256 if baseline_abi is not None else None
        )
        if abi_gate.get("baselineAbiSha256") != expected_baseline_digest:
            raise ManifestValidationError(
                "latestCompiled baseline ABI binding mismatch"
            )
        if gate_status == "pending-replay-reflection":
            if baseline_abi is None or set(abi_gate) != {
                "status",
                "baselineAbiSha256",
            }:
                raise ManifestValidationError(
                    "latestCompiled pending ABI gate metadata is invalid"
                )
            expected_validation = (
                "container-structure-only",
                True,
                False,
                False,
            )
        elif gate_status == "blocked-baseline-interface-unavailable":
            if baseline_abi is not None or set(abi_gate) != {
                "status",
                "baselineAbiSha256",
            }:
                raise ManifestValidationError(
                    "latestCompiled blocked ABI gate metadata is invalid"
                )
            expected_validation = (
                "container-structure-only",
                True,
                False,
                False,
            )
        elif gate_status == "blocked-apply-backend-unsupported":
            if set(abi_gate) != {"status", "baselineAbiSha256"}:
                raise ManifestValidationError(
                    "latestCompiled unsupported-backend gate metadata is invalid"
                )
            expected_validation = (
                "container-structure-only",
                True,
                False,
                False,
            )
        elif gate_status == "blocked-d3d11-dynamic-linkage":
            if (
                baseline_abi is None
                or baseline_abi.dynamic_linkage_interface_count == 0
                or set(abi_gate)
                != {"status", "baselineAbiSha256"}
            ):
                raise ManifestValidationError(
                    "latestCompiled dynamic-linkage ABI gate metadata is invalid"
                )
            expected_validation = (
                "container-structure-only",
                True,
                False,
                False,
            )
        elif gate_status == "compatible":
            if baseline_abi is None:
                raise ManifestValidationError(
                    "latestCompiled cannot be ABI-compatible without a baseline ABI"
                )
            self._validate_abi_report(
                root, manifest, candidate, abi_gate, baseline_abi
            )
            expected_validation = (
                "replay-reflection-abi-compatible",
                False,
                True,
                True,
            )
        else:
            raise ManifestValidationError("latestCompiled ABI gate status is invalid")
        if (
            candidate_size != len(compiled)
            or candidate.get("rawSha256") != hashlib.sha256(compiled).hexdigest()
            or candidate.get("codeSha256") != container.code_sha256
            or candidate.get("kind") != container.kind
            or candidate.get("sourceSha256") != hashlib.sha256(source).hexdigest()
            or candidate.get("validationScope") != expected_validation[0]
            or candidate.get("requiresReplayAbiValidation")
            is not expected_validation[1]
            or candidate.get("replayAbiValidated") is not expected_validation[2]
            or candidate.get("abiCompatible") is not expected_validation[3]
            or candidate.get("semanticEquivalenceVerified") is not False
        ):
            raise ManifestValidationError("latestCompiled metadata mismatch")
        if manifest.get("routePlan") is not None:
            self._validate_candidate_route_provenance(
                root, manifest, candidate, source, compiled
            )
        else:
            compiler_binding = manifest["tools"].get("compiler")
            if not isinstance(compiler_binding, dict) or any(
                (
                    candidate.get("toolId") != compiler_binding.get("toolId"),
                    candidate.get("toolConfigDigest")
                    != compiler_binding.get("configDigest"),
                    candidate.get("executableSha256")
                    != compiler_binding.get("executableSha256"),
                    candidate.get("fixedFileHashes")
                    != compiler_binding.get("fixedFileHashes"),
                )
            ):
                raise ManifestValidationError(
                    "latestCompiled tool does not match workspace compiler binding"
                )

    def _validate_candidate_route_provenance(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
        source: bytes,
        compiled: bytes,
    ) -> None:
        plan = manifest.get("routePlan")
        if not isinstance(plan, Mapping):
            raise ManifestValidationError("latestCompiled route plan is missing")
        expected_hops = plan.get("compile")
        actual_hops = candidate.get("compileHops")
        expected_route_ids = [
            hop["capability"]["routeId"] for hop in expected_hops
        ]
        if (
            candidate.get("routePlanSha256") != plan.get("planSha256")
            or candidate.get("compileRouteIds") != expected_route_ids
            or not isinstance(actual_hops, list)
            or len(actual_hops) != len(expected_hops)
        ):
            raise ManifestValidationError(
                "latestCompiled route-chain binding mismatch"
            )
        previous_hash = hashlib.sha256(source).hexdigest()
        for index, (expected, actual) in enumerate(
            zip(expected_hops, actual_hops)
        ):
            if not isinstance(actual, Mapping):
                raise ManifestValidationError(
                    "latestCompiled route-hop provenance is invalid"
                )
            capability = expected["capability"]
            operation = expected["operation"]
            required_equal = {
                "hop": index + 1,
                "routeBinding": expected,
                "inputFormat": capability["inputFormat"],
                "outputFormat": capability["outputFormat"],
                "toolId": operation["toolId"],
                "toolConfigDigest": operation["configDigest"],
                "executableSha256": operation["executableSha256"],
                "fixedFileHashes": operation["fixedFileHashes"],
                "status": "succeeded",
            }
            if any(actual.get(key) != value for key, value in required_equal.items()):
                raise ManifestValidationError(
                    "latestCompiled route-hop provenance binding mismatch"
                )
            job_dir = self._job_directory(root, actual.get("jobPath"))
            input_path = self._artifact(root, actual.get("inputPath"))
            output_path = self._artifact(root, actual.get("outputPath"))
            stdout_path = self._artifact(root, actual.get("stdoutPath"))
            stderr_path = self._artifact(root, actual.get("stderrPath"))
            for path in (input_path, output_path, stdout_path, stderr_path):
                if not is_within(job_dir, path):
                    raise ManifestValidationError(
                        "latestCompiled route-hop artifact escapes its job"
                    )
            input_bytes = input_path.read_bytes()
            output_bytes = output_path.read_bytes()
            input_hash = hashlib.sha256(input_bytes).hexdigest()
            output_hash = hashlib.sha256(output_bytes).hexdigest()
            input_detection = detect_shader_format(
                input_bytes, capability["inputFormat"]
            ).as_dict()
            output_detection = detect_shader_format(
                output_bytes, capability["outputFormat"]
            ).as_dict()
            if (
                actual.get("inputSha256") != input_hash
                or input_hash != previous_hash
                or actual.get("outputSha256") != output_hash
                or actual.get("inputDetection") != input_detection
                or actual.get("outputDetection") != output_detection
            ):
                raise ManifestValidationError(
                    "latestCompiled route-hop artifact/hash mismatch"
                )
            previous_hash = output_hash
        if previous_hash != hashlib.sha256(compiled).hexdigest():
            raise ManifestValidationError(
                "latestCompiled final route output does not match candidate bytes"
            )

    def _candidate_reflection_request_from_manifest(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        baseline_abi: ShaderAbi,
    ) -> dict:
        candidate = manifest.get("latestCompiled")
        if not isinstance(candidate, Mapping):
            raise AbiValidationError(
                "compile a candidate before requesting replay reflection"
            )
        gate = candidate.get("abiGate")
        if not isinstance(gate, Mapping) or gate.get("status") not in {
            "pending-replay-reflection",
            "compatible",
        }:
            raise AbiValidationError(
                "the current candidate is not eligible for replay ABI reflection"
            )
        baseline = manifest.get("baseline")
        capture = manifest.get("capture")
        if not isinstance(baseline, Mapping) or not isinstance(capture, Mapping):
            raise ManifestValidationError("workspace provenance is incomplete")
        api_token = str(capture.get("api", "")).strip().lower().replace(
            "graphicsapi.", ""
        )
        if api_token == "direct3d11":
            api_token = "d3d11"
        elif api_token == "direct3d12":
            api_token = "d3d12"
        baseline_kind = str(baseline.get("kind", "")).upper()
        if api_token == "d3d11" and baseline_kind != "DXBC":
            raise AbiValidationError("D3D11 ABI validation accepts native DXBC only")
        if api_token == "d3d12" and baseline_kind not in {"DXBC", "DXIL"}:
            raise AbiValidationError("D3D12 ABI validation accepts native DXBC/DXIL only")
        if api_token not in {"d3d11", "d3d12"}:
            raise AbiValidationError("replay ABI validation supports D3D11/D3D12 only")
        if api_token == "d3d11" and baseline_abi.dynamic_linkage_interface_count != 0:
            raise AbiValidationError(
                "D3D11 dynamic-linkage interfaces are not supported by this ABI gate"
            )
        return build_candidate_reflection_request(
            workspace_id=str(manifest.get("workspaceId", "")),
            capture_id=str(capture.get("captureId", "")),
            api=str(capture.get("api", "")),
            candidate_path=self._artifact(root, candidate.get("blobPath")),
            candidate_sha256=str(candidate.get("rawSha256", "")),
            candidate_byte_size=candidate.get("byteSize"),
            candidate_kind=str(candidate.get("kind", "")),
            revision=manifest.get("revision"),
            stage=str(manifest.get("stage", "")),
            entry_point=str(manifest.get("entryPoint", "")),
            baseline_abi_sha256=baseline_abi.sha256,
            target_profile=str(manifest.get("targetProfile", {}).get("profile", "")),
        )

    def _validate_abi_report(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
        gate: Mapping[str, Any],
        baseline_abi: ShaderAbi,
    ) -> None:
        required_gate_keys = {
            "status",
            "baselineAbiSha256",
            "candidateAbiSha256",
            "requestId",
            "reportPath",
            "reportSha256",
        }
        if set(gate) != required_gate_keys:
            raise ManifestValidationError(
                "latestCompiled compatible ABI gate metadata is invalid"
            )
        report_path = self._artifact(root, gate.get("reportPath"))
        if report_path.stat().st_size > MAX_ABI_REPORT_BYTES:
            raise ManifestValidationError("candidate ABI report exceeds its size limit")
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if report_hash != gate.get("reportSha256"):
            raise ManifestValidationError("candidate ABI report hash mismatch")
        try:
            report = read_json_object(
                report_path, MAX_ABI_REPORT_BYTES, "candidate ABI report"
            )
        except ValueError as exc:
            raise ManifestValidationError(str(exc)) from exc
        if (
            report.get("schema") != ABI_REPORT_SCHEMA
            or report.get("schemaVersion") != ABI_REPORT_VERSION
            or report.get("workspaceId") != manifest.get("workspaceId")
            or report.get("captureId") != manifest["capture"].get("captureId")
            or report.get("revision") != manifest.get("revision")
        ):
            raise ManifestValidationError("candidate ABI report binding mismatch")
        request = self._candidate_reflection_request_from_manifest(
            root, manifest, baseline_abi
        )
        if report.get("request") != request or gate.get("requestId") != request.get(
            "requestId"
        ):
            raise ManifestValidationError("candidate ABI report request mismatch")
        try:
            baseline_access, candidate_access = self._dxil_access_evidence(
                root, manifest
            )
            reflected_abi, comparison, evidence = (
                validate_candidate_reflection_response(
                    request,
                    report.get("response"),
                    baseline_abi,
                    baseline_dxil_access=baseline_access,
                    candidate_dxil_access=candidate_access,
                )
            )
            reported_baseline = parse_shader_abi(
                report.get("baselineInterface"),
                expected_stage=baseline_abi.stage,
                expected_entry_point=baseline_abi.entry_point,
            )
            reported_candidate = parse_shader_abi(
                report.get("candidateInterface"),
                expected_stage=baseline_abi.stage,
                expected_entry_point=baseline_abi.entry_point,
            )
        except AbiValidationError as exc:
            raise ManifestValidationError(
                "candidate ABI report evidence is invalid: %s" % exc
            ) from exc
        if (
            not comparison.compatible
            or reported_baseline != baseline_abi
            or reported_candidate != reflected_abi
            or report.get("comparison") != comparison.to_dict()
            or report.get("evidence") != evidence
            or gate.get("candidateAbiSha256") != reflected_abi.sha256
        ):
            raise ManifestValidationError(
                "candidate ABI report does not prove exact compatibility"
            )

    def _regular_route_artifact(
        self, root: Path, job: Path, relative_value: Any, label: str
    ) -> Path:
        if not isinstance(relative_value, str) or not relative_value:
            raise ManifestValidationError("%s path is invalid" % label)
        unresolved = root / relative_value
        if unresolved.is_symlink():
            raise ManifestValidationError("%s is redirected" % label)
        path = unresolved.resolve()
        if not is_within(job, path) or not path.is_file():
            raise ManifestValidationError("%s escapes its fixed route job" % label)
        return path

    def _baseline_dxil_asm_evidence(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> tuple[bytes, str] | None:
        """Re-bind a baseline DXIL-ASM artifact to its fixed decompile chain."""

        plan = manifest.get("routePlan")
        source = manifest.get("source")
        if not isinstance(plan, Mapping) or not isinstance(source, Mapping):
            return None
        expected_hops = plan.get("decompile")
        if source.get("origin") != "trusted-decompile-route" or not isinstance(
            expected_hops, list
        ):
            return None
        produced_jobs = source.get("producedByJobs")
        if not isinstance(produced_jobs, list) or len(produced_jobs) != len(
            expected_hops
        ):
            return None
        matching: Mapping[str, Any] | None = None
        history_values = manifest.get("history")
        if not isinstance(history_values, list):
            return None
        for entry in reversed(history_values):
            if (
                isinstance(entry, Mapping)
                and entry.get("operation") == "decompile-route-chain"
                and entry.get("status") == "succeeded"
                and entry.get("routePlanSha256") == plan.get("planSha256")
                and entry.get("publishedSourcePath") == source.get("path")
                and entry.get("outputSha256") == source.get("sha256")
            ):
                matching = entry
                break
        if matching is None:
            return None
        actual_hops = matching.get("hops")
        if not isinstance(actual_hops, list) or len(actual_hops) != len(
            expected_hops
        ):
            return None
        baseline_record = manifest.get("baseline")
        if not isinstance(baseline_record, Mapping):
            return None
        baseline_hash = str(baseline_record.get("rawSha256", ""))
        try:
            baseline_bytes = self._artifact(
                root, baseline_record.get("path")
            ).read_bytes()
        except ManifestValidationError:
            return None
        if hashlib.sha256(baseline_bytes).hexdigest() != baseline_hash:
            return None
        previous_hash = baseline_hash
        matches: list[tuple[bytes, str]] = []
        for index, (expected, actual, relative_job) in enumerate(
            zip(expected_hops, actual_hops, produced_jobs, strict=True)
        ):
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
                return None
            capability = expected.get("capability")
            operation = expected.get("operation")
            if not isinstance(capability, Mapping) or not isinstance(
                operation, Mapping
            ):
                return None
            required_equal = {
                "hop": index + 1,
                "routeBinding": expected,
                "inputFormat": capability.get("inputFormat"),
                "outputFormat": capability.get("outputFormat"),
                "toolId": operation.get("toolId"),
                "toolConfigDigest": operation.get("configDigest"),
                "executableSha256": operation.get("executableSha256"),
                "fixedFileHashes": operation.get("fixedFileHashes"),
                "status": "succeeded",
                "jobPath": relative_job,
            }
            if any(actual.get(key) != value for key, value in required_equal.items()):
                return None
            job = self._job_directory(root, relative_job)
            input_path = self._regular_route_artifact(
                root, job, actual.get("inputPath"), "baseline route input"
            )
            output_path = self._regular_route_artifact(
                root, job, actual.get("outputPath"), "baseline route output"
            )
            input_bytes = input_path.read_bytes()
            output_bytes = output_path.read_bytes()
            input_hash = hashlib.sha256(input_bytes).hexdigest()
            output_hash = hashlib.sha256(output_bytes).hexdigest()
            if (
                input_hash != previous_hash
                or actual.get("inputSha256") != input_hash
                or actual.get("outputSha256") != output_hash
                or actual.get("inputDetection")
                != detect_shader_format(
                    input_bytes, str(capability.get("inputFormat"))
                ).as_dict()
                or actual.get("outputDetection")
                != detect_shader_format(
                    output_bytes, str(capability.get("outputFormat"))
                ).as_dict()
            ):
                return None
            if (
                capability.get("inputFormat") == "dxil"
                and capability.get("outputFormat") == "dxil-asm"
                and input_hash == baseline_hash
                and input_bytes == baseline_bytes
            ):
                matches.append((output_bytes, output_hash))
            previous_hash = output_hash
        if previous_hash != source.get("sha256"):
            return None
        # More than one direct conversion is ambiguous provenance: never pick
        # whichever .ll happened to appear last in a multi-hop history.
        return matches[0] if len(matches) == 1 else None

    def _candidate_dxil_asm_evidence(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> tuple[bytes, str] | None:
        """Select DXIL-ASM bytes already hash-bound to the compiled candidate."""

        plan = manifest.get("routePlan")
        candidate = manifest.get("latestCompiled")
        if not isinstance(plan, Mapping) or not isinstance(candidate, Mapping):
            return None
        expected_hops = plan.get("compile")
        actual_hops = candidate.get("compileHops")
        if not isinstance(expected_hops, list) or not isinstance(actual_hops, list):
            return None
        if (
            len(expected_hops) != len(actual_hops)
            or candidate.get("routePlanSha256") != plan.get("planSha256")
        ):
            return None
        try:
            source_bytes = self._artifact(
                root, candidate.get("sourcePath")
            ).read_bytes()
            candidate_bytes = self._artifact(
                root, candidate.get("blobPath")
            ).read_bytes()
        except ManifestValidationError:
            return None
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
        if (
            source_hash != candidate.get("sourceSha256")
            or candidate_hash != candidate.get("rawSha256")
        ):
            return None
        previous_hash = source_hash
        matches: list[tuple[bytes, str]] = []
        for index, (expected, actual) in enumerate(
            zip(expected_hops, actual_hops, strict=True)
        ):
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
                return None
            capability = expected.get("capability")
            operation = expected.get("operation")
            if not isinstance(capability, Mapping) or not isinstance(
                operation, Mapping
            ):
                return None
            required_equal = {
                "hop": index + 1,
                "routeBinding": expected,
                "inputFormat": capability.get("inputFormat"),
                "outputFormat": capability.get("outputFormat"),
                "toolId": operation.get("toolId"),
                "toolConfigDigest": operation.get("configDigest"),
                "executableSha256": operation.get("executableSha256"),
                "fixedFileHashes": operation.get("fixedFileHashes"),
                "status": "succeeded",
            }
            if any(actual.get(key) != value for key, value in required_equal.items()):
                return None
            try:
                job = self._job_directory(root, actual.get("jobPath"))
                input_path = self._regular_route_artifact(
                    root, job, actual.get("inputPath"), "candidate route input"
                )
                output_path = self._regular_route_artifact(
                    root, job, actual.get("outputPath"), "candidate route output"
                )
            except ManifestValidationError:
                return None
            input_bytes = input_path.read_bytes()
            output_bytes = output_path.read_bytes()
            input_hash = hashlib.sha256(input_bytes).hexdigest()
            output_hash = hashlib.sha256(output_bytes).hexdigest()
            if (
                input_hash != previous_hash
                or actual.get("inputSha256") != input_hash
                or actual.get("outputSha256") != output_hash
                or actual.get("inputDetection")
                != detect_shader_format(
                    input_bytes, str(capability.get("inputFormat"))
                ).as_dict()
                or actual.get("outputDetection")
                != detect_shader_format(
                    output_bytes, str(capability.get("outputFormat"))
                ).as_dict()
            ):
                return None
            if (
                capability.get("inputFormat") == "dxil-asm"
                and capability.get("outputFormat") == "dxil"
                and output_hash == candidate_hash
                and output_bytes == candidate_bytes
            ):
                matches.append((input_bytes, input_hash))
            previous_hash = output_hash
        if previous_hash != candidate_hash:
            return None
        return matches[0] if len(matches) == 1 else None

    def _dedicated_dxil_asm_evidence(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        role: str,
    ) -> tuple[bytes, str] | None:
        """Re-bind one independently executed DXC-style disassembly job.

        This path is intentionally separate from the editable route. Every
        mutable claim is re-derived from the process-owned catalogs, current
        pinned tool/dependency bytes, the workspace/revision owner artifact,
        the fixed job command, and job-local input/output bytes.
        """

        try:
            if (
                role not in {"baseline", "candidate"}
                or not self._needs_dedicated_dxil_disassembly_evidence(manifest)
            ):
                return None
            collection = manifest.get("dxilDisassemblyEvidence")
            if (
                not isinstance(collection, Mapping)
                or set(collection)
                != {"schema", "schemaVersion", "baseline", "candidate"}
                or collection.get("schema")
                != DXIL_DISASSEMBLY_EVIDENCE_SCHEMA
                or collection.get("schemaVersion")
                != DXIL_DISASSEMBLY_EVIDENCE_VERSION
            ):
                return None
            record = collection.get(role)
            if (
                not isinstance(record, Mapping)
                or set(record) != _DXIL_DISASSEMBLY_RECORD_FIELDS
                or record.get("schema") != DXIL_DISASSEMBLY_RECORD_SCHEMA
                or record.get("schemaVersion")
                != DXIL_DISASSEMBLY_RECORD_VERSION
                or record.get("evidenceRole") != role
                or record.get("status") != "succeeded"
            ):
                return None

            catalog, _route, operation, route_binding = (
                self._dedicated_dxil_disassembly_binding(manifest)
            )
            plan = manifest.get("routePlan")
            capture = manifest.get("capture")
            if not isinstance(plan, Mapping) or not isinstance(capture, Mapping):
                return None
            if role == "baseline":
                owner_record = manifest.get("baseline")
                expected_revision = 0
                expected_history_operations = {
                    "decompile-route-chain",
                    "baseline-dxil-disassembly-refresh",
                }
                history_key = "baselineDxilDisassemblyEvidence"
            else:
                owner_record = manifest.get("latestCompiled")
                expected_revision = int(manifest.get("revision"))
                expected_history_operations = {"compile-route-chain"}
                history_key = "candidateDxilDisassemblyEvidence"
            if not isinstance(owner_record, Mapping):
                return None
            owner_path = self._artifact(
                root,
                owner_record.get("path")
                if role == "baseline"
                else owner_record.get("blobPath"),
            )
            owner_bytes = owner_path.read_bytes()
            owner_digest = hashlib.sha256(owner_bytes).hexdigest()
            try:
                owner_size = int(owner_record.get("byteSize"))
                owner_revision = int(owner_record.get("revision"))
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                owner_digest != owner_record.get("rawSha256")
                or owner_size != len(owner_bytes)
                or owner_revision != expected_revision
            ):
                return None

            required_record_values = {
                "evidenceRole": role,
                "workspacePath": str(root.resolve()),
                "workspaceId": manifest.get("workspaceId"),
                "captureId": capture.get("captureId"),
                "revision": expected_revision,
                "routePlanSha256": plan.get("planSha256"),
                "routeCatalogPath": str(catalog.path),
                "routeCatalogSha256": _file_sha256(catalog.path),
                "toolRegistryPath": str(catalog.registry.path),
                "toolRegistrySha256": catalog.tool_registry_sha256,
                "routeBinding": route_binding,
                "operation": operation.operation,
                "toolId": operation.tool_id,
                "toolConfigDigest": operation.config_digest,
                "executableSha256": operation.executable_sha256,
                "fixedFileHashes": operation.fixed_file_hashes_dict(),
                "inputArtifactPath": _relative(root, owner_path),
                "inputFormat": "dxil",
                "inputSha256": owner_digest,
                "inputByteSize": len(owner_bytes),
                "outputFormat": "dxil-asm",
                "returnCode": 0,
                "timedOut": False,
                "launchError": None,
                "status": "succeeded",
            }
            if any(
                record.get(key) != value
                for key, value in required_record_values.items()
            ):
                return None

            job = self._job_directory(root, record.get("jobPath"))
            input_path = self._regular_route_artifact(
                root, job, record.get("inputPath"), "DXIL evidence input"
            )
            output_path = self._regular_route_artifact(
                root, job, record.get("outputPath"), "DXIL evidence output"
            )
            stdout_path = self._regular_route_artifact(
                root, job, record.get("stdoutPath"), "DXIL evidence stdout"
            )
            stderr_path = self._regular_route_artifact(
                root, job, record.get("stderrPath"), "DXIL evidence stderr"
            )
            input_payload = input_path.read_bytes()
            output_payload = output_path.read_bytes()
            output_digest = hashlib.sha256(output_payload).hexdigest()
            if (
                input_payload != owner_bytes
                or hashlib.sha256(input_payload).hexdigest() != owner_digest
                or record.get("inputDetection")
                != detect_shader_format(input_payload, "dxil").as_dict()
                or record.get("outputSha256") != output_digest
                or record.get("outputByteSize") != len(output_payload)
                or record.get("outputDetection")
                != detect_shader_format(output_payload, "dxil-asm").as_dict()
                or record.get("stdoutSha256")
                != hashlib.sha256(stdout_path.read_bytes()).hexdigest()
                or record.get("stderrSha256")
                != hashlib.sha256(stderr_path.read_bytes()).hexdigest()
            ):
                return None
            text = output_payload.decode("utf-8", errors="strict")
            if not text or "\x00" in text:
                return None

            baseline_path = self._artifact(root, manifest["baseline"]["path"])
            values = self._template_values(
                root,
                dict(manifest),
                expected_revision,
                job,
                input_path,
                output_path,
                baseline_path,
            )
            expected_command = operation.expand(values)
            if record.get("command") != expected_command:
                return None

            job_metadata_path = job / "job.json"
            if job_metadata_path.is_symlink() or not job_metadata_path.is_file():
                return None
            job_metadata = read_json_object(
                job_metadata_path,
                MAX_MANIFEST_BYTES,
                "DXIL disassembly evidence job metadata",
            )
            required_job_fields = {
                "schema",
                "schemaVersion",
                "toolId",
                "operation",
                "toolConfigDigest",
                "executableSha256",
                "fixedFileHashes",
                "command",
                "cwd",
                "environmentKeys",
                "timeoutSeconds",
                "status",
                "returnCode",
                "launchError",
                "stdoutTruncated",
                "stderrTruncated",
                "stdoutPath",
                "stderrPath",
            }
            if (
                set(job_metadata) != required_job_fields
                or job_metadata.get("schema") != "renderdoc-ultra-shader-job/v1"
                or job_metadata.get("schemaVersion") != 1
                or job_metadata.get("toolId") != operation.tool_id
                or job_metadata.get("operation") != operation.operation
                or job_metadata.get("toolConfigDigest")
                != operation.config_digest
                or job_metadata.get("executableSha256")
                != operation.executable_sha256
                or job_metadata.get("fixedFileHashes")
                != operation.fixed_file_hashes_dict()
                or job_metadata.get("command") != expected_command
                or job_metadata.get("cwd") != str(job)
                or not isinstance(job_metadata.get("environmentKeys"), list)
                or any(
                    not isinstance(item, str)
                    for item in job_metadata.get("environmentKeys", [])
                )
                or job_metadata.get("timeoutSeconds")
                != operation.timeout_seconds
                or job_metadata.get("status") != "succeeded"
                or job_metadata.get("returnCode") != 0
                or job_metadata.get("launchError") is not None
                or job_metadata.get("stdoutTruncated")
                is not record.get("stdoutTruncated")
                or job_metadata.get("stderrTruncated")
                is not record.get("stderrTruncated")
                or job_metadata.get("stdoutPath") != "stdout.log"
                or job_metadata.get("stderrPath") != "stderr.log"
            ):
                return None

            history_values = manifest.get("history")
            if not isinstance(history_values, list):
                return None
            history_matches = []
            for entry in history_values:
                if not isinstance(entry, Mapping):
                    continue
                expected_output_hash = (
                    manifest.get("source", {}).get("sha256")
                    if role == "baseline"
                    else owner_digest
                )
                if (
                    entry.get("operation") in expected_history_operations
                    and entry.get("status") == "succeeded"
                    and entry.get("revision") == expected_revision
                    and entry.get("routePlanSha256") == plan.get("planSha256")
                    and entry.get("outputSha256") == expected_output_hash
                    and entry.get(history_key) == record
                ):
                    history_matches.append(entry)
            if len(history_matches) != 1:
                return None
            return output_payload, output_digest
        except (
            ShaderWorkspaceError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            # This method produces proof, never a best-effort guess. A malformed
            # record, missing artifact, changed dependency, or parsing failure
            # is uniformly represented as absence of evidence.
            return None

    def _ensure_dedicated_baseline_dxil_disassembly(
        self, root: Path, manifest: dict
    ) -> dict[str, Any] | None:
        """Lazily regenerate baseline access evidence with current fixed tools.

        Migration deliberately does not copy path-bound evidence from the old
        workspace. Running the disassembler only after the target has its final
        path keeps its workspace, command, job, catalog, registry, and fixed-file
        bindings truthful. An unavailable refresh remains explicit absence of
        proof: compilation may still produce an inspectable candidate, while
        the later ambiguous-layout ABI gate stays fail-closed.
        """

        if not self._needs_dedicated_dxil_disassembly_evidence(manifest):
            return None
        existing = self._dedicated_dxil_asm_evidence(root, manifest, "baseline")
        if existing is not None:
            collection = manifest.get("dxilDisassemblyEvidence")
            if isinstance(collection, Mapping):
                record = collection.get("baseline")
                if isinstance(record, Mapping):
                    return dict(record)
            raise ManifestValidationError(
                "validated baseline DXIL evidence has no bound record"
            )

        baseline_record = manifest.get("baseline")
        source_record = manifest.get("source")
        plan = manifest.get("routePlan")
        if (
            not isinstance(baseline_record, Mapping)
            or not isinstance(source_record, Mapping)
            or not isinstance(plan, Mapping)
        ):
            raise ManifestValidationError(
                "baseline DXIL evidence refresh requires baseline/source/route provenance"
            )
        baseline_path = self._artifact(root, baseline_record.get("path"))
        baseline_bytes = baseline_path.read_bytes()
        history = {
            "operation": "baseline-dxil-disassembly-refresh",
            "revision": 0,
            "routePlanSha256": plan.get("planSha256"),
            "outputSha256": source_record.get("sha256"),
            "status": "not-started",
        }
        record = self._attempt_dedicated_dxil_disassembly(
            root,
            manifest,
            role="baseline",
            revision=0,
            input_bytes=baseline_bytes,
            input_artifact_path=baseline_path,
            history=history,
        )
        if record is None:
            history["status"] = "failed"
            self._append_history(root, manifest, history)
            return None
        history["status"] = "succeeded"
        self._append_history(root, manifest, history)
        rebound = self._dedicated_dxil_asm_evidence(root, manifest, "baseline")
        if rebound is None:
            raise ManifestValidationError(
                "regenerated baseline DXIL disassembly evidence did not re-bind"
            )
        return record

    def _dxil_access_evidence(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> tuple[
        DxilCBufferAccessSignature | None,
        DxilCBufferAccessSignature | None,
    ]:
        """Extract only from immutable, fixed-route and hash-verified artifacts."""

        baseline_record = manifest.get("baseline")
        if not isinstance(baseline_record, Mapping) or str(
            baseline_record.get("kind", "")
        ).upper() != "DXIL":
            return None, None
        # Keep the original native-IR hash adjacency as the strongest source.
        # Reconstructed-HLSL routes have no such hop, so only then fall back to
        # the separate, fully rebound DXC disassembly jobs above.
        baseline_source = self._baseline_dxil_asm_evidence(root, manifest)
        if baseline_source is None:
            baseline_source = self._dedicated_dxil_asm_evidence(
                root, manifest, "baseline"
            )
        candidate_source = self._candidate_dxil_asm_evidence(root, manifest)
        if candidate_source is None:
            candidate_source = self._dedicated_dxil_asm_evidence(
                root, manifest, "candidate"
            )

        def extract(
            bound: tuple[bytes, str] | None,
        ) -> DxilCBufferAccessSignature | None:
            if bound is None:
                return None
            payload, digest = bound
            # The evidence rebinding above freshly reads the job artifact and
            # derives its declared digest on every validation.  Recheck that
            # byte identity before consulting the parse cache so cache hits do
            # not replace any filesystem/hash gate.  Parsing large DXIL text is
            # CPU-heavy and the exact same baseline/candidate pair is otherwise
            # parsed repeatedly by status/request/apply checks for one save.
            if hashlib.sha256(payload).hexdigest() != digest:
                return None
            with self._dxil_access_cache_lock:
                if digest in self._dxil_access_signature_cache:
                    cached = self._dxil_access_signature_cache[digest]
                    self._dxil_access_signature_cache.move_to_end(digest)
                    return cached
            try:
                parsed = extract_dxil_cbuffer_access_signature(
                    payload, expected_sha256=digest
                )
            except DxilAccessSignatureError:
                # Malformed, dynamic, or incomplete IR is absence of proof, not
                # a reason to weaken the ordinary ambiguous-layout rejection.
                parsed = None
            with self._dxil_access_cache_lock:
                existing = self._dxil_access_signature_cache.get(digest)
                if digest in self._dxil_access_signature_cache:
                    self._dxil_access_signature_cache.move_to_end(digest)
                    return existing
                self._dxil_access_signature_cache[digest] = parsed
                while len(self._dxil_access_signature_cache) > 32:
                    self._dxil_access_signature_cache.popitem(last=False)
            return parsed

        return extract(baseline_source), extract(candidate_source)

    def _artifact(self, root: Path, relative_value: Any) -> Path:
        if not isinstance(relative_value, str) or not relative_value:
            raise ManifestValidationError("workspace artifact path is invalid")
        candidate = (root / relative_value).resolve()
        if not is_within(root, candidate) or not candidate.is_file():
            raise ManifestValidationError(
                "workspace artifact is missing or escapes workspace"
            )
        return candidate

    def _job_directory(self, root: Path, relative_value: Any) -> Path:
        if not isinstance(relative_value, str) or not relative_value:
            raise ManifestValidationError("workspace job path is invalid")
        candidate = (root / relative_value).resolve()
        jobs_root = (root / "jobs").resolve()
        if (
            not is_within(jobs_root, candidate)
            or candidate == jobs_root
            or not candidate.is_dir()
            or candidate.is_symlink()
        ):
            raise ManifestValidationError(
                "workspace job directory is missing or escapes jobs"
            )
        return candidate

    def _bound_operation(
        self, manifest: dict, binding_name: str, operation_name: str
    ) -> ToolOperation:
        binding = manifest["tools"].get(binding_name)
        if not isinstance(binding, dict):
            raise ManifestValidationError(
                "workspace %s tool binding is missing" % binding_name
            )
        tool_id = binding.get("toolId")
        operation = self.registry.operation(tool_id, operation_name)
        if binding.get("configDigest") != operation.config_digest:
            raise ToolConfigurationError(
                "tool %s configuration changed after workspace creation; "
                "create a new workspace" % tool_id
            )
        if binding.get("executableSha256") != operation.executable_sha256:
            raise ToolConfigurationError(
                "tool %s executable hash differs from the workspace binding" % tool_id
            )
        if binding.get("fixedFileHashes") != operation.fixed_file_hashes_dict():
            raise ToolConfigurationError(
                "tool %s fixed-file hashes differ from the workspace binding" % tool_id
            )
        return operation

    def _new_job_path(self, root: Path, operation: str, revision: int) -> Path:
        return root / "jobs" / ("%s-r%04d-%s" % (operation, revision, uuid.uuid4().hex))

    def _template_values(
        self,
        root: Path,
        manifest: dict,
        revision: int,
        job_dir: Path,
        input_path: Path,
        output_path: Path,
        baseline_path: Path,
    ) -> dict[str, str]:
        shader = manifest["shader"]
        return {
            "input": str(input_path),
            "output": str(output_path),
            "baseline": str(baseline_path),
            "job_dir": str(job_dir),
            "workspace_dir": str(root),
            "stage": str(manifest["stage"]),
            "entry_point": str(manifest["entryPoint"]),
            "revision": str(revision),
            "capture_id": str(manifest["capture"]["captureId"]),
            "raw_sha256": str(shader["rawSha256"]),
            "target_profile": str(manifest["targetProfile"]["profile"]),
            "target_api": str(manifest["capture"]["api"]),
        }

    def _require_process_success(self, result) -> None:
        if result.launch_error:
            raise ToolExecutionError(
                "external tool could not be launched: %s; see %s"
                % (result.launch_error, result.job_dir)
            )
        if result.timed_out:
            raise ToolExecutionError("external tool timed out; see %s" % result.job_dir)
        if result.return_code != 0:
            raise ToolExecutionError(
                "external tool exited with code %s; see %s"
                % (result.return_code, result.job_dir)
            )

    def _require_job_output(
        self, job_dir: Path, output_path: Path, producer: str
    ) -> None:
        if not output_path.is_file():
            raise ToolExecutionError("%s did not create its output file" % producer)
        if not is_within(job_dir.resolve(), output_path.resolve()):
            raise ToolExecutionError("%s output escapes its job directory" % producer)

    def _history_entry(
        self, root: Path, operation: ToolOperation, result, revision: int
    ) -> dict:
        return {
            "operation": operation.operation,
            "revision": revision,
            "toolId": operation.tool_id,
            "toolConfigDigest": operation.config_digest,
            "executableSha256": operation.executable_sha256,
            "fixedFileHashes": operation.fixed_file_hashes_dict(),
            "jobPath": _relative(root, result.job_dir),
            "stdoutPath": _relative(root, result.stdout_path),
            "stderrPath": _relative(root, result.stderr_path),
            "returnCode": result.return_code,
            "timedOut": result.timed_out,
            "launchError": result.launch_error,
            "stdoutTruncated": result.stdout_truncated,
            "stderrTruncated": result.stderr_truncated,
        }

    def _append_history(self, root: Path, manifest: dict, history: dict) -> None:
        previous_updated_at = manifest.get("updatedAt")
        manifest["history"].append(history)
        manifest["updatedAt"] = _utc_now()
        try:
            _write_manifest_with_retry(root / "manifest.json", manifest)
        except Exception:
            # Keep the caller's in-memory transaction aligned with the bytes
            # that remain on disk.  In particular, a failed success-manifest
            # write must not be appended a second time as a failure record.
            if manifest["history"] and manifest["history"][-1] is history:
                manifest["history"].pop()
            if previous_updated_at is None:
                manifest.pop("updatedAt", None)
            else:
                manifest["updatedAt"] = previous_updated_at
            raise
