"""Transactional organization of reconstructed HLSL with bounded evidence.

The model is allowed to edit only a proposal copy.  Fixed code compiles and
ABI-validates the untouched source first, then the proposal, and promotes the
proposal after exact code identity, declared rename-only proof, or a Host-run
output comparison within explicit tolerances, together with current replay
ABI validation. Output comparisons cover the tested capture only, not every
possible input; none of these checks recovers the game's original source.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any
import uuid

from . import aliases as cbuffer_aliases
from .equivalence import (
    ANALYSIS_TEMPLATE,
    EquivalenceError,
    ReviewRounds,
    apply_renames,
    build_verdict,
    declared_symbols,
    differences_to_reasons,
    flow_spec_template,
    flow_value_bindings,
    insert_function_comments,
    mermaid_from_flow_graph,
    mermaid_from_flow_spec,
    reason as verdict_reason,
    rename_only_equivalent,
    validate_flow_spec,
    validate_renames,
)
from .io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .publication_receipts import CompletedShaderReviews
from .templates import (analysis_template, render_analysis_fields, analysis_markdown_issues,
                        organization_contract, organization_structure_issues)
from .flow_graph import _function_records as _function_records_for_template
from .flow_graph import (
    MAX_FLOW_GRAPH_BYTES,
    MAX_MODULE_DESCRIPTIONS_BYTES,
    ShaderFlowGraphError,
    build_shader_flow_graph,
    load_module_descriptions,
    module_description_template,
    validate_shader_flow_graph,
)
from .leases import (
    SOURCE_TRANSACTION_BACKUP_FILE,
    SOURCE_TRANSACTION_JOURNAL_FILE,
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)


ORGANIZATION_SESSION_SCHEMA = "renderdoc-ai-shader-organization-session/v1"
ORGANIZATION_GATE_SCHEMA = "renderdoc-ai-shader-organization-gate/v1"
ORGANIZATION_POLICY_VERSION = "renderdoc-ai-organize-workspace/v1"
ORGANIZATION_OBSERVATION_SCHEMA = "renderdoc-ai-shader-organization-observation/v1"

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_ANALYSIS_BYTES = 256 * 1024
MAX_FLOWCHART_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_MERMAID = re.compile(r"^\s*(?:flowchart|graph)\s+(?:TD|TB|BT|RL|LR)\b")
_UNSAFE_HLSL_DIRECTIVE = re.compile(
    r"^[ \t]*#[ \t]*(?:include|line)\b", re.IGNORECASE | re.MULTILINE
)
_MERMAID_CLICK = re.compile(r"^[ \t]*click\b", re.IGNORECASE | re.MULTILINE)
_MERMAID_URL = re.compile(
    r"(?:https?://|javascript:|data:|file:|mailto:|www\.)", re.IGNORECASE
)
_MERMAID_HTML = re.compile(r"<\s*/?\s*[a-z!][^>]*>", re.IGNORECASE)
_MERMAID_INIT = re.compile(r"%%\s*\{", re.IGNORECASE)


class ShaderOrganizationError(RuntimeError):
    """A proposal could not pass the bounded organization transaction."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShaderOrganizationError("organization evidence is not JSON serializable") from exc


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShaderOrganizationError(
            "organization session is not JSON serializable"
        ) from exc


def _regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ShaderOrganizationError("%s is missing or redirected" % label)
    before = path.stat()
    if before.st_size <= 0 or before.st_size > maximum:
        raise ShaderOrganizationError("%s has an invalid byte size" % label)
    value = path.read_bytes()
    after = path.stat()
    if (
        len(value) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ShaderOrganizationError("%s changed while it was read" % label)
    return value


def _utf8(path: Path, maximum: int, label: str) -> tuple[str, bytes]:
    value = _regular_bytes(path, maximum, label)
    if b"\x00" in value:
        raise ShaderOrganizationError("%s contains NUL" % label)
    try:
        return value.decode("utf-8", errors="strict"), value
    except UnicodeError as exc:
        raise ShaderOrganizationError("%s is not strict UTF-8" % label) from exc


def _safe_dir(path: Path, parent: Path, label: str, *, create: bool) -> Path:
    if not is_within(parent, path):
        raise ShaderOrganizationError("%s escapes its workspace" % label)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ShaderOrganizationError("%s is redirected" % label)
    elif create:
        path.mkdir(parents=False, exist_ok=False)
    else:
        raise ShaderOrganizationError("%s does not exist" % label)
    if path.resolve() != path:
        raise ShaderOrganizationError("%s is redirected" % label)
    return path


def _hex(value: Any, label: str, length: int = 64) -> str:
    text = str(value or "").strip().lower()
    matcher = _SHA256 if length == 64 else _ID
    if matcher.fullmatch(text) is None:
        raise ShaderOrganizationError("%s is invalid" % label)
    return text


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShaderOrganizationError("%s is invalid" % label)
    return value


def _exclusive_durable_write(path: Path, value: bytes, label: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ShaderOrganizationError("%s already exists or is unavailable" % label) from exc
    try:
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ShaderOrganizationError("%s write stalled" % label)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_locked_source(handle: Any, value: bytes, label: str) -> None:
    handle.seek(0)
    remaining = memoryview(value)
    while remaining:
        written = handle.write(remaining)
        if not isinstance(written, int) or written <= 0:
            raise ShaderOrganizationError("%s write stalled" % label)
        remaining = remaining[written:]
    handle.truncate(len(value))
    handle.flush()
    os.fsync(handle.fileno())


def _remove_source_transaction_evidence(journal: Path, backup: Path) -> None:
    # Journal first: a crash after this unlink still leaves the backup, which
    # independently prevents the shared lease from being released.
    for artifact in (journal, backup):
        if not artifact.exists() and not artifact.is_symlink():
            continue
        if artifact.is_symlink() or not artifact.is_file():
            raise ShaderOrganizationError(
                "source transaction recovery evidence is missing or redirected"
            )
        artifact.unlink()


def _source_compare_and_swap(
    path: Path,
    expected: bytes,
    replacement: bytes,
    label: str,
) -> bool:
    """Conditionally replace source while an OS lock protects compare/write.

    The workspace lease excludes every managed writer.  The source-file lock
    closes the remaining compare/replace window for ordinary Windows editor
    writes (and for cooperating POSIX writers).  Keeping the original handle
    open also prevents a Windows rename-over save; on POSIX an unmanaged
    rename is detected by the post-write inode check without touching its new
    pathname.
    """

    if path.is_symlink() or not path.is_file():
        raise ShaderOrganizationError("%s is missing or redirected" % label)
    try:
        if os.name == "nt":
            # Python's normal CRT open shares write access. Open the pathname
            # with Win32 share-read only so a competing editor cannot truncate,
            # write, delete, or rename it between the comparison and write.
            import ctypes
            from ctypes import wintypes
            import msvcrt

            create_file = ctypes.windll.kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            win_handle = create_file(
                str(path),
                0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
                0x00000001,  # FILE_SHARE_READ
                None,
                3,  # OPEN_EXISTING
                0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if win_handle == invalid_handle:
                raise ctypes.WinError()
            try:
                descriptor = msvcrt.open_osfhandle(
                    int(win_handle), os.O_RDWR | os.O_BINARY
                )
            except Exception:
                ctypes.windll.kernel32.CloseHandle(win_handle)
                raise
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        else:  # pragma: no cover - Windows is the supported ReplayLab host.
            handle = path.open("r+b", buffering=0)
    except OSError as exc:
        raise ShaderOrganizationError("%s could not be opened exclusively" % label) from exc
    locked = False
    windows_lock = os.name == "nt"
    try:
        handle.seek(0)
        if windows_lock:
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, MAX_SOURCE_BYTES)
            except OSError as exc:
                raise ShaderOrganizationError(
                    "%s is being edited by another process" % label
                ) from exc
        else:  # pragma: no cover - Windows is the supported ReplayLab host.
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ShaderOrganizationError(
                    "%s is being edited by another process" % label
                ) from exc
        locked = True

        opened = os.fstat(handle.fileno())
        named = path.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or path.is_symlink()
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            raise ShaderOrganizationError("%s identity changed before CAS" % label)
        handle.seek(0)
        observed = handle.read(MAX_SOURCE_BYTES + 1)
        if len(observed) > MAX_SOURCE_BYTES:
            raise ShaderOrganizationError("%s exceeds its fixed size limit" % label)
        if observed != expected:
            return False

        root = path.parent.parent
        journal = root / SOURCE_TRANSACTION_JOURNAL_FILE
        backup = root / SOURCE_TRANSACTION_BACKUP_FILE
        if any(
            artifact.exists() or artifact.is_symlink()
            for artifact in (journal, backup)
        ):
            raise ShaderOrganizationError(
                "workspace has unresolved source transaction recovery evidence"
            )
        _exclusive_durable_write(backup, expected, "source transaction backup")
        journal_value = _canonical_json(
            {
                "schema": "renderdoc-shader-source-transaction/v1",
                "sourcePath": str(path.relative_to(root)).replace("\\", "/"),
                "expectedSourceSha256": _sha256(expected),
                "replacementSourceSha256": _sha256(replacement),
                "backupFile": SOURCE_TRANSACTION_BACKUP_FILE,
            }
        ) + b"\n"
        try:
            _exclusive_durable_write(
                journal, journal_value, "source transaction journal"
            )
        except Exception:
            try:
                backup.unlink()
            except OSError:
                pass
            raise

        try:
            _write_locked_source(handle, replacement, "%s replacement" % label)
            after_opened = os.fstat(handle.fileno())
            after_named = path.stat()
            handle.seek(0)
            after = handle.read(MAX_SOURCE_BYTES + 1)
            if (
                path.is_symlink()
                or after_opened.st_dev != after_named.st_dev
                or after_opened.st_ino != after_named.st_ino
                or after != replacement
            ):
                raise ShaderOrganizationError(
                    "%s changed concurrently during compare-and-swap" % label
                )
            _remove_source_transaction_evidence(journal, backup)
        except Exception as write_error:
            try:
                _write_locked_source(handle, expected, "%s recovery" % label)
                handle.seek(0)
                restored = handle.read(MAX_SOURCE_BYTES + 1)
                if restored != expected:
                    raise ShaderOrganizationError(
                        "%s could not be restored from its in-memory backup" % label
                    )
                _remove_source_transaction_evidence(journal, backup)
            except Exception as recovery_error:
                # Recovery evidence remains, so WorkspaceSourceTransactionLease
                # deliberately preserves crash residue and every managed start
                # fails closed until an operator inspects/restores the backup.
                raise ShaderOrganizationError(
                    "%s CAS failed (%s); recovery evidence was retained (%s)"
                    % (label, write_error, recovery_error)
                ) from write_error
            raise
        return True
    finally:
        if locked:
            try:
                handle.seek(0)
                if windows_lock:
                    import msvcrt

                    msvcrt.locking(
                        handle.fileno(), msvcrt.LK_UNLCK, MAX_SOURCE_BYTES
                    )
                else:  # pragma: no cover - see acquisition branch above.
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _read_session_file(session_dir: Path, description: str) -> dict:
    """Load session.json, naming a begin that died at its baseline gate."""

    session_path = session_dir / "session.json"
    failure_path = session_dir / "failure.json"
    if not session_path.exists() and failure_path.is_file():
        try:
            failure = read_json_object(failure_path, MAX_CONTEXT_BYTES, "organization failure")
        except ValueError:
            failure = {}
        raise ShaderOrganizationError(
            "organization session %s never opened: begin failed at %s; repair the "
            "workspace source (repair_shader_workspace_source_abi) and call "
            "begin_shader_workspace_organization again for a new organization_id"
            % (session_dir.name, failure.get("phase") or "its baseline gate")
        )
    return read_json_object(session_path, MAX_CONTEXT_BYTES, description)


def _validate_source_directives(text: str, label: str) -> None:
    if _UNSAFE_HLSL_DIRECTIVE.search(text):
        raise ShaderOrganizationError(
            "%s must not contain #include or #line" % label
        )


def _validate_safe_mermaid(text: str) -> None:
    if "```" in text or _MERMAID.match(text) is None:
        raise ShaderOrganizationError("organization flowchart is not plain safe Mermaid")
    if _MERMAID_CLICK.search(text):
        raise ShaderOrganizationError("organization Mermaid must not contain click actions")
    if _MERMAID_URL.search(text):
        raise ShaderOrganizationError("organization Mermaid must not contain URLs")
    if _MERMAID_HTML.search(text):
        raise ShaderOrganizationError("organization Mermaid must not contain HTML")
    if _MERMAID_INIT.search(text):
        raise ShaderOrganizationError(
            "organization Mermaid must not contain init directives"
        )


_FLOWCHART_PLACEHOLDER = (
    b"flowchart TD\n  A[Captured shader] --> B[Reconstructed HLSL]\n  B --> C[Readable proposal]\n"
)



def _waive_blockers(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Downgrade blockers to warnings after the user chose to publish the baseline as-is."""

    waived = []
    for item in reasons:
        record = dict(item)
        if record.get("severity", "blocker") == "blocker":
            record["severity"] = "warning"
            record["waivedByUser"] = True
        waived.append(record)
    return waived


class ShaderOrganizationManager:
    """Own bounded proposal files and the compile/ABI/code identity gate."""

    def __init__(
        self,
        service_factory: Callable[[], Any],
        validate_candidate: Callable[[str, int, str], Mapping[str, Any]],
        compare_candidates: Callable[
            [str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
        ] | None = None,
        cbuffer_alias_identity: Callable[[str], Mapping[str, Any]] | None = None,
        *,
        completed_reviews: CompletedShaderReviews | None = None,
    ) -> None:
        if not callable(service_factory) or not callable(validate_candidate):
            raise TypeError("organization callbacks must be callable")
        if compare_candidates is not None and not callable(compare_candidates):
            raise TypeError("organization comparison callback must be callable")
        if cbuffer_alias_identity is not None and not callable(cbuffer_alias_identity):
            raise TypeError("organization CBuffer identity callback must be callable")
        self._service_factory = service_factory
        self._validate_candidate = validate_candidate
        self._compare_candidates = compare_candidates
        self._cbuffer_alias_identity = cbuffer_alias_identity
        self._guard = threading.RLock()
        # Session files live below an AI-editable tree. Seal every fixed field
        # with a process-owned key so proposal edits cannot rewrite baseline
        # code/hash/identity authority. A server restart intentionally makes an
        # unfinished session fail closed instead of trusting disk-only claims.
        self._session_hmac_key = secrets.token_bytes(32)
        # Completed reviews are source-asset provenance, not process sessions.
        # Their exact accepted snapshot survives a Host restart in a local
        # store that is outside the managed proposal tools' write surface.
        self._completed_reviews = completed_reviews or CompletedShaderReviews()

    def _session_hmac(self, session: Mapping[str, Any]) -> str:
        unsigned = dict(session)
        unsigned.pop("sessionIntegrityHmacSha256", None)
        return hmac.new(
            self._session_hmac_key,
            _canonical_json(unsigned),
            hashlib.sha256,
        ).hexdigest()

    def _seal_session(self, session: Mapping[str, Any]) -> dict[str, Any]:
        sealed = dict(session)
        sealed["sessionIntegrityHmacSha256"] = self._session_hmac(sealed)
        return sealed

    @staticmethod
    def _candidate_binding(
        source_sha256: str,
        compile_result: Mapping[str, Any] | None,
        validation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if compile_result is None or validation is None:
            return {
                "complete": False,
                "sourceSha256": source_sha256,
                "revision": None,
                "candidateRawSha256": "",
                "codeSha256": "",
                "abiRequestId": "",
                "abiReportSha256": "",
            }
        return {
            "complete": True,
            "sourceSha256": _hex(source_sha256, "gate source SHA-256"),
            "revision": _positive_int(
                compile_result.get("revision"), "gate candidate revision"
            ),
            "candidateRawSha256": _hex(
                compile_result.get("rawSha256"), "gate candidate raw SHA-256"
            ),
            "codeSha256": _hex(
                compile_result.get("codeSha256"), "gate code SHA-256"
            ),
            "abiRequestId": _hex(
                validation.get("requestId"), "gate ABI request ID"
            ),
            "abiReportSha256": _hex(
                validation.get("abiReportSha256"), "gate ABI report SHA-256"
            ),
        }

    @staticmethod
    def _inactive_gate(
        *,
        organization_id: str,
        status: Mapping[str, Any],
        decision: str,
        reason: str,
        source_sha256: str,
        compile_result: Mapping[str, Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        source_restored: bool = False,
    ) -> dict[str, Any]:
        binding = ShaderOrganizationManager._candidate_binding(
            source_sha256, compile_result, validation
        )
        return {
            "schema": ORGANIZATION_GATE_SCHEMA,
            "schemaVersion": 1,
            "policyVersion": ORGANIZATION_POLICY_VERSION,
            "organizationId": organization_id,
            "reviewedAt": _utc_now(),
            "requestedTransformation": "organize-only",
            "workspaceId": str(status.get("workspaceId", "")),
            "captureId": str(status.get("capture", {}).get("captureId", "")),
            "stage": str(status.get("stage", "")),
            "entryPoint": str(status.get("entryPoint", "")),
            "decision": decision,
            "reason": str(reason),
            "codeSha256Equal": False,
            "organizedSourcePromoted": False,
            "sourceRestored": bool(source_restored),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
            "safeToStartLiveEdit": False,
            "finalBinding": binding,
        }

    @staticmethod
    def _publish_root_gate(root: Path, gate: Mapping[str, Any]) -> None:
        atomic_write_json(root / "ai" / "organize-code-gate.json", dict(gate))

    def _require_final_binding(
        self,
        service: Any,
        root: Path,
        session: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        status = service.status(str(root))
        observed_root, unused_path, unused_bytes, source_sha = self._workspace_source(
            status
        )
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        abi_gate = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            observed_root != root
            or status.get("workspaceId") != session.get("workspaceId")
            or status.get("capture", {}).get("captureId")
            != session.get("captureId")
            or status.get("stage") != session.get("stage")
            or status.get("entryPoint") != session.get("entryPoint")
            or source_sha != binding.get("sourceSha256")
            or status.get("revision") != binding.get("revision")
            or not isinstance(latest, Mapping)
            or latest.get("sourceSha256") != binding.get("sourceSha256")
            or latest.get("rawSha256") != binding.get("candidateRawSha256")
            or latest.get("codeSha256") != binding.get("codeSha256")
            or latest.get("abiCompatible") is not True
            or not isinstance(abi_gate, Mapping)
            or abi_gate.get("status") != "compatible"
            or abi_gate.get("requestId") != binding.get("abiRequestId")
            or abi_gate.get("reportSha256") != binding.get("abiReportSha256")
        ):
            raise ShaderOrganizationError(
                "organization final source/candidate/ABI binding changed"
            )

    def begin(
        self,
        workspace_path: str,
        semantic_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a fixed proposal copy after compiling/validating raw HLSL."""

        with self._guard:
            service = self._service_factory()
            initial_status = service.status(workspace_path)
            initial_root, unused_path, unused_bytes, unused_sha = (
                self._workspace_source(initial_status)
            )
            lease = WorkspaceSourceTransactionLease(initial_root)
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                raise ShaderOrganizationError(
                    "organization begin requires the exclusive workspace source "
                    "transaction lease; an active watcher/live edit or review, or "
                    "crash residue, must be stopped or inspected first"
                ) from exc
            try:
                # Status/source observed before the lease is only sufficient to
                # locate the workspace lock. All authoritative begin evidence is
                # re-read while watcher/review start is excluded.
                status = service.status(workspace_path)
                root, source_path, source_bytes, source_sha = (
                    self._workspace_source(status)
                )
                if root != initial_root:
                    raise ShaderOrganizationError(
                        "workspace identity changed while organization begin acquired ownership"
                    )
                return self._begin_owned(
                    service,
                    status,
                    root,
                    source_path,
                    source_bytes,
                    source_sha,
                    semantic_context,
                )
            finally:
                lease.release()

    def repair_baseline_source(
        self,
        workspace_path: str,
        expected_source_sha256: str,
        source_text: Any,
        *,
        acknowledge_semantic_equivalence_unverified: bool,
    ) -> dict[str, Any]:
        """Replace the workspace HLSL with an AI-authored captured-ABI repair.

        The decompiled text is compiled and replay-ABI validated first; only a
        candidate whose fresh reflection matches the captured physical ABI is
        kept as the new organization baseline.  Anything else leaves the source
        bytes exactly as they were and returns the compiler/ABI evidence, so an
        Agent can iterate without ever leaving a broken source behind.  The
        byte-for-byte decompiler output is preserved before the first repair.
        """

        if acknowledge_semantic_equivalence_unverified is not True:
            raise ShaderOrganizationError(
                "ABI source repair requires acknowledging that semantic "
                "equivalence stays unverified"
            )
        expected = _hex(expected_source_sha256, "expected source SHA-256")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ShaderOrganizationError("ABI repair source must be non-empty text")
        if "\x00" in source_text:
            raise ShaderOrganizationError("ABI repair source contains NUL characters")
        try:
            replacement = source_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ShaderOrganizationError("ABI repair source is not valid UTF-8") from exc
        if len(replacement) > MAX_SOURCE_BYTES:
            raise ShaderOrganizationError("ABI repair source exceeds its fixed size limit")
        _validate_source_directives(source_text, "ABI repair source")
        replacement_sha = _sha256(replacement)

        with self._guard:
            service = self._service_factory()
            initial_status = service.status(workspace_path)
            initial_root, unused_path, unused_bytes, unused_sha = (
                self._workspace_source(initial_status)
            )
            lease = WorkspaceSourceTransactionLease(initial_root)
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                raise ShaderOrganizationError(
                    "ABI source repair requires the exclusive workspace source "
                    "transaction lease; stop the active watcher/live edit or "
                    "organization review first"
                ) from exc
            try:
                status = service.status(workspace_path)
                root, source_path, source_bytes, source_sha = (
                    self._workspace_source(status)
                )
                if root != initial_root:
                    raise ShaderOrganizationError(
                        "workspace identity changed while ABI repair acquired ownership"
                    )
                if source_sha != expected:
                    raise ShaderOrganizationError(
                        "workspace source changed before the ABI repair; read the "
                        "current source again and resubmit"
                    )
                if replacement == source_bytes:
                    raise ShaderOrganizationError(
                        "ABI repair source is identical to the current workspace source"
                    )
                return self._repair_baseline_owned(
                    service, status, root, source_path, source_bytes, source_sha,
                    replacement, replacement_sha,
                )
            finally:
                lease.release()

    def _repair_baseline_owned(
        self,
        service: Any,
        status: Mapping[str, Any],
        root: Path,
        source_path: Path,
        source_bytes: bytes,
        source_sha: str,
        replacement: bytes,
        replacement_sha: str,
    ) -> dict[str, Any]:
        from .diagnostics import classify_shader_failure

        ai_dir = root / "ai"
        _safe_dir(ai_dir, root, "workspace AI directory", create=True)
        attempts_dir = _safe_dir(
            ai_dir / "abi-repair-attempts", ai_dir, "ABI repair attempts directory",
            create=True,
        )
        # Keep the untouched decompiler output exactly like organization begin
        # does, so a later organization still proves its provenance chain.
        raw_original = ai_dir / "decompiler-original.hlsl"
        decompiled_sha = _hex(
            status["source"].get("decompiledSha256"),
            "workspace decompiler source SHA-256",
        )
        if raw_original.exists() or raw_original.is_symlink():
            preserved = _regular_bytes(
                raw_original, MAX_SOURCE_BYTES, "preserved decompiler source"
            )
            if _sha256(preserved) != decompiled_sha:
                raise ShaderOrganizationError(
                    "preserved decompiler source contradicts workspace provenance"
                )
        elif source_sha == decompiled_sha:
            atomic_write_bytes(raw_original, source_bytes)
        else:
            raise ShaderOrganizationError(
                "edited workspace has no preserved byte-for-byte decompiler source"
            )

        attempt_id = uuid.uuid4().hex
        attempt_source = attempts_dir / (attempt_id + ".hlsl")
        receipt_path = attempts_dir / (attempt_id + ".json")
        atomic_write_bytes(attempt_source, replacement)
        receipt: dict[str, Any] = {
            "schema": "renderdoc-ai-shader-abi-source-repair/v1",
            "schemaVersion": 1,
            "attemptId": attempt_id,
            "at": _utc_now(),
            "workspacePath": str(root),
            "workspaceId": status.get("workspaceId"),
            "captureId": status.get("capture", {}).get("captureId"),
            "stage": status.get("stage"),
            "entryPoint": status.get("entryPoint"),
            "previousSourceSha256": source_sha,
            "decompilerOriginalSha256": decompiled_sha,
            "repairedSourceSha256": replacement_sha,
            "attemptSourcePath": str(attempt_source.relative_to(root)).replace("\\", "/"),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
        }

        # A compile-only check first: a source that does not even compile must
        # never be swapped in, and the Agent gets line-bound diagnostics.
        check = None
        compile_check = getattr(service, "compile_check", None)
        if callable(compile_check):
            try:
                check = compile_check(str(root), replacement.decode("utf-8"))
            except Exception as exc:  # evidence only; the swap below still gates
                check = {"ok": None, "reason": str(exc)[:2000]}
        if isinstance(check, Mapping) and check.get("ok") is False:
            receipt.update({
                "accepted": False,
                "phase": "compile-check",
                "sourceRestored": True,
                "diagnostics": list(check.get("diagnostics") or [])[:128],
            })
            atomic_write_json(receipt_path, receipt)
            return self._repair_result(root, receipt, status, None)

        try:
            swapped = _source_compare_and_swap(
                source_path, source_bytes, replacement, "workspace HLSL"
            )
        except ShaderOrganizationError:
            raise
        if not swapped:
            raise ShaderOrganizationError(
                "workspace shader source changed on disk during the ABI repair"
            )
        failure: dict[str, Any] | None = None
        compile_result: dict[str, Any] | None = None
        validation: dict[str, Any] | None = None
        try:
            compile_result, validation = self._compile_and_validate(
                service, root, replacement_sha, "ABI-repaired source"
            )
        except Exception as exc:
            failure = {
                "type": type(exc).__name__,
                "message": str(exc)[:4000],
                "diagnostic": classify_shader_failure(exc, phase="abi-source-repair"),
            }
        after = None
        try:
            after = service.status(str(root))
        except Exception as exc:
            if failure is None:
                failure = {"type": type(exc).__name__, "message": str(exc)[:4000]}
        latest = after.get("latestCompiled") if isinstance(after, Mapping) else None
        if failure is None and isinstance(latest, Mapping):
            receipt.update({
                "accepted": True,
                "phase": "compiled-and-replay-abi-validated",
                "sourceRestored": False,
                "revision": compile_result.get("revision") if compile_result else None,
                "candidateRawSha256": compile_result.get("rawSha256") if compile_result else None,
                "codeSha256": compile_result.get("codeSha256") if compile_result else None,
                "abiRequestId": validation.get("requestId") if validation else None,
                "abiReportSha256": validation.get("abiReportSha256") if validation else None,
                "abiGate": dict(latest.get("abiGate") or {}),
            })
            atomic_write_json(receipt_path, receipt)
            return self._repair_result(root, receipt, after, None)

        # Fail closed: put the previous bytes back before reporting anything.
        restored = _source_compare_and_swap(
            source_path, replacement, source_bytes, "workspace HLSL"
        )
        if not restored:
            raise ShaderOrganizationError(
                "ABI repair failed and the previous workspace source could not be "
                "restored automatically; inspect %s" % source_path
            )
        evidence: dict[str, Any] = {}
        if isinstance(after, Mapping):
            attempt = after.get("lastAbiAttempt")
            if isinstance(attempt, Mapping):
                evidence["abiComparison"] = attempt.get("comparison")
                # Identity-keyed findings with a concrete HLSL hint each; this
                # is what the next repair attempt should be written against.
                evidence["abiExplanation"] = attempt.get("explanation")
            if isinstance(latest, Mapping):
                evidence["latestCompiled"] = {
                    key: latest.get(key)
                    for key in ("revision", "sourceSha256", "rawSha256", "abiGate")
                }
            evidence["abiValidationStatus"] = after.get("abiValidationStatus")
        receipt.update({
            "accepted": False,
            "phase": "compile-or-replay-abi",
            "sourceRestored": True,
            "failure": failure,
            "evidence": evidence,
        })
        atomic_write_json(receipt_path, receipt)
        return self._repair_result(root, receipt, after or status, failure)

    def _repair_result(
        self,
        root: Path,
        receipt: Mapping[str, Any],
        status: Mapping[str, Any] | None,
        failure: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        accepted = receipt.get("accepted") is True
        result: dict[str, Any] = {
            "schema": "renderdoc-ai-shader-abi-source-repair-result/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "attemptId": receipt.get("attemptId"),
            "accepted": accepted,
            "phase": receipt.get("phase"),
            "sourceRestored": receipt.get("sourceRestored"),
            "currentSourceSha256": (
                receipt.get("repairedSourceSha256") if accepted
                else receipt.get("previousSourceSha256")
            ),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
            "receiptPath": str(root / "ai" / "abi-repair-attempts" / (str(receipt.get("attemptId")) + ".json")),
        }
        if accepted:
            result.update({
                "revision": receipt.get("revision"),
                "candidateRawSha256": receipt.get("candidateRawSha256"),
                "abiGate": receipt.get("abiGate"),
                "nextTool": "begin_shader_workspace_organization",
            })
        else:
            evidence = receipt.get("evidence") or {}
            explanation = evidence.get("abiExplanation") if isinstance(evidence, Mapping) else None
            result.update({
                "diagnostics": receipt.get("diagnostics"),
                "failure": dict(failure) if failure else None,
                "evidence": evidence,
                "whatToFix": (
                    [
                        {"where": item.get("identity"), "why": item.get("message"),
                         "how": item.get("hint")}
                        for item in (explanation.get("findings") or [])
                    ]
                    if isinstance(explanation, Mapping) else None
                ),
                "baselineInterface": (
                    (status or {}).get("baseline", {}).get("compiledInterface")
                    if isinstance(status, Mapping) else None
                ),
                "nextTool": "repair_shader_workspace_source_abi",
            })
        return result

    def _begin_owned(
        self,
        service: Any,
        status: Mapping[str, Any],
        root: Path,
        source_path: Path,
        source_bytes: bytes,
        source_sha: str,
        semantic_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Create begin artifacts while the caller owns the source lease."""

        context = dict(semantic_context or {})
        source_text = source_bytes.decode("utf-8", errors="strict")
        _validate_source_directives(source_text, "organization baseline HLSL")
        # Display names the user (or an earlier AI pass) already gave this
        # shader's cbuffer members in RenderDoc's constant-buffer panel.
        alias_identity = None
        if self._cbuffer_alias_identity is not None:
            alias_identity = self._cbuffer_alias_identity(str(root))
            if (
                not isinstance(alias_identity, Mapping)
                or alias_identity.get("schema") != "replaylab-cbuffer-alias-identity/v1"
                or alias_identity.get("captureId") != status.get("capture", {}).get("captureId")
                or alias_identity.get("stage") != status.get("stage")
                or str(alias_identity.get("shaderResourceId")) != str(status.get("shader", {}).get("resourceId"))
                or alias_identity.get("rawSha256") != status.get("baseline", {}).get("rawSha256")
                or not isinstance(alias_identity.get("blocks"), list)
                or _json_size(alias_identity) > MAX_CONTEXT_BYTES // 2
            ):
                raise ShaderOrganizationError("captured CBuffer alias identity is invalid")
            alias_identity = dict(alias_identity)
            context["cbufferAliasIdentity"] = alias_identity
        context["cbufferAliases"] = cbuffer_aliases.evidence_for_source(source_text, alias_identity)
        if _json_size(context) > MAX_CONTEXT_BYTES:
            raise ShaderOrganizationError("semantic context exceeds its fixed size limit")

        ai_dir = root / "ai"
        # The public caller already owns this re-entrant guard. Keeping the
        # artifact transaction scoped here also makes direct internal use safe.
        with self._guard:
            if ai_dir.exists():
                _safe_dir(ai_dir, root, "workspace AI directory", create=False)
            else:
                ai_dir.mkdir(parents=False, exist_ok=False)
            sessions_dir = ai_dir / "organization-sessions"
            if sessions_dir.exists():
                _safe_dir(sessions_dir, ai_dir, "organization sessions directory", create=False)
            else:
                sessions_dir.mkdir(parents=False, exist_ok=False)

            organization_id = uuid.uuid4().hex
            # Invalidate any older successful root gate before this new review
            # session exposes editable proposal files.
            self._publish_root_gate(
                root,
                self._inactive_gate(
                    organization_id=organization_id,
                    status=status,
                    decision="organization-beginning",
                    reason="proposal review has not completed",
                    source_sha256=source_sha,
                ),
            )

            raw_original = ai_dir / "decompiler-original.hlsl"
            decompiled_sha = _hex(
                status["source"].get("decompiledSha256"),
                "workspace decompiler source SHA-256",
            )
            if raw_original.exists() or raw_original.is_symlink():
                preserved = _regular_bytes(
                    raw_original, MAX_SOURCE_BYTES, "preserved decompiler source"
                )
                if _sha256(preserved) != decompiled_sha:
                    raise ShaderOrganizationError(
                        "preserved decompiler source contradicts workspace provenance"
                    )
            elif source_sha == decompiled_sha:
                atomic_write_bytes(raw_original, source_bytes)
            else:
                raise ShaderOrganizationError(
                    "edited workspace has no preserved byte-for-byte decompiler source"
                )

            session_dir = sessions_dir / organization_id
            session_dir.mkdir(parents=False, exist_ok=False)
            baseline_path = session_dir / "baseline-source.hlsl"
            proposal_path = session_dir / "organized-proposal.hlsl"
            analysis_path = session_dir / "analysis.md"
            flowchart_path = session_dir / "flowchart.mmd"
            module_descriptions_path = session_dir / "module-descriptions.json"
            structured_flow_graph_path = session_dir / "flow-graph.json"
            context_path = session_dir / "semantic-context.json"
            atomic_write_bytes(baseline_path, source_bytes)
            atomic_write_bytes(proposal_path, source_bytes)
            atomic_write_bytes(
                analysis_path,
                ANALYSIS_TEMPLATE.encode("utf-8"),
            )
            atomic_write_bytes(flowchart_path, _FLOWCHART_PLACEHOLDER)
            atomic_write_json(
                module_descriptions_path,
                module_description_template(
                    source_bytes.decode("utf-8", errors="strict")
                ),
            )
            atomic_write_json(context_path, context)

            try:
                baseline_compile, baseline_validation = self._compile_and_validate(
                    service, root, source_sha, "untouched organization baseline"
                )
            except Exception:
                # Proposal files are useful failure evidence, but a failed baseline
                # must never be presented as an editable organization session.
                atomic_write_json(
                    session_dir / "failure.json",
                    {
                        "schema": "renderdoc-ai-shader-organization-failure/v1",
                        "at": _utc_now(),
                        "phase": "baseline-compile-abi",
                        "sourceSha256": source_sha,
                        "sourceRestored": _sha256(source_path.read_bytes()) == source_sha,
                        "applied": False,
                    },
                )
                raise

            # The server collects semantic facts before this transaction. Refresh
            # only the fixed reconstruction-fidelity record after the baseline
            # compile and fresh ABI gate, so AI can distinguish "ABI-compatible"
            # from "same captured executable code". Direct unit/service callers
            # predating this status field retain their supplied context.
            post_baseline_status = service.status(str(root))
            post_root, post_source_path, post_source_bytes, post_source_sha = (
                self._workspace_source(post_baseline_status)
            )
            post_latest = post_baseline_status.get("latestCompiled")
            post_abi_gate = (
                post_latest.get("abiGate")
                if isinstance(post_latest, Mapping)
                else None
            )
            if (
                post_root != root
                or post_source_path != source_path
                or post_source_bytes != source_bytes
                or post_source_sha != source_sha
                or post_baseline_status.get("revision")
                != baseline_compile.get("revision")
                or not isinstance(post_latest, Mapping)
                or post_latest.get("sourceSha256") != source_sha
                or post_latest.get("rawSha256") != baseline_compile.get("rawSha256")
                or post_latest.get("codeSha256") != baseline_compile.get("codeSha256")
                or post_latest.get("abiCompatible") is not True
                or not isinstance(post_abi_gate, Mapping)
                or post_abi_gate.get("status") != "compatible"
                or post_abi_gate.get("requestId")
                != baseline_validation.get("requestId")
                or post_abi_gate.get("reportSha256")
                != baseline_validation.get("abiReportSha256")
            ):
                raise ShaderOrganizationError(
                    "workspace changed while refreshing organization baseline fidelity"
                )
            post_fidelity = post_baseline_status.get("reconstructionFidelity")
            if post_fidelity is not None:
                if (
                    not isinstance(post_fidelity, Mapping)
                    or post_fidelity.get("schema")
                    != "renderdoc-ultra-shader-reconstruction-fidelity/v1"
                    or post_fidelity.get("semanticEquivalenceVerified") is not False
                ):
                    raise ShaderOrganizationError(
                        "post-baseline reconstruction fidelity evidence is invalid"
                    )
                context["reconstructionFidelity"] = dict(post_fidelity)
                if _json_size(context) > MAX_CONTEXT_BYTES:
                    raise ShaderOrganizationError(
                        "semantic context exceeds its fixed size limit"
                    )
                atomic_write_json(context_path, context)

            session = self._seal_session({
                "schema": ORGANIZATION_SESSION_SCHEMA,
                "schemaVersion": 1,
                "policyVersion": ORGANIZATION_POLICY_VERSION,
                "organizationId": organization_id,
                "state": "awaiting-proposal-review",
                "createdAt": _utc_now(),
                "workspacePath": str(root),
                "workspaceId": _hex(status.get("workspaceId"), "workspaceId", 32),
                "captureId": _hex(
                    status.get("capture", {}).get("captureId"), "captureId"
                ),
                "stage": str(status.get("stage", "")),
                "entryPoint": str(status.get("entryPoint", "")),
                "sourcePath": str(source_path.relative_to(root)).replace("\\", "/"),
                "baselineSourcePath": str(baseline_path.relative_to(root)).replace("\\", "/"),
                "proposalSourcePath": str(proposal_path.relative_to(root)).replace("\\", "/"),
                "analysisPath": str(analysis_path.relative_to(root)).replace("\\", "/"),
                "flowchartPath": str(flowchart_path.relative_to(root)).replace("\\", "/"),
                "moduleDescriptionsPath": str(
                    module_descriptions_path.relative_to(root)
                ).replace("\\", "/"),
                "structuredFlowGraphPath": str(
                    structured_flow_graph_path.relative_to(root)
                ).replace("\\", "/"),
                "semanticContextPath": str(context_path.relative_to(root)).replace("\\", "/"),
                "decompilerOriginalPath": str(raw_original.relative_to(root)).replace("\\", "/"),
                "baselineSourceSha256": source_sha,
                "decompilerOriginalSha256": decompiled_sha,
                "baselineRevision": baseline_compile["revision"],
                "baselineCandidateRawSha256": baseline_compile["rawSha256"],
                "baselineCodeSha256": baseline_compile["codeSha256"],
                "baselineAbiRequestId": _hex(
                    baseline_validation.get("requestId"),
                    "baseline ABI request ID",
                ),
                "baselineAbiReportSha256": _hex(
                    baseline_validation.get("abiReportSha256"),
                    "baseline ABI report SHA-256",
                ),
                "baselineCompiledAndAbiValidated": True,
                "semanticEquivalenceVerified": False,
                "replayApplied": False,
                # Host-applied edits (rename_shader_workspace_symbols); a proposal
                # that equals baseline + these renames is accepted without a
                # byte-identical executable (DXIL keeps names in metadata).
                "renameMap": {},
                "commentMap": {},
                "structureOps": [],
                "functionNotes": {},
                "cbufferAliasIdentity": alias_identity,
            })
            atomic_write_json(session_dir / "baseline-compile.json", baseline_compile)
            atomic_write_json(session_dir / "baseline-abi.json", baseline_validation)
            atomic_write_json(session_dir / "session.json", session)
            self._publish_root_gate(
                root,
                self._inactive_gate(
                    organization_id=organization_id,
                    status=status,
                    decision="awaiting-proposal-review",
                    reason="proposal has not passed review",
                    source_sha256=source_sha,
                    compile_result=baseline_compile,
                    validation=baseline_validation,
                    source_restored=True,
                ),
            )
            return self._begin_result(root, session_dir, session, context)

    def rename(
        self,
        workspace_path: str,
        organization_id: str,
        renames: Mapping[str, Any],
        comments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Host-applied renames/comments: regenerate the proposal from the baseline.

        The maps are cumulative for the session and sealed into it, so the
        review can prove rename-only equivalence without trusting the text.
        """

        with self._guard:
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = service.status(workspace_path)
            root, _source_path, _current_bytes, _current_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            session_path = session_dir / "session.json"
            session = _read_session_file(session_dir, "organization session")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") != "awaiting-proposal-review":
                raise ShaderOrganizationError("organization session is not awaiting review")
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_text, _baseline_bytes = _utf8(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            merged_renames = dict(session.get("renameMap") or {})
            merged_renames.update({str(k): str(v) for k, v in dict(renames or {}).items()})
            try:
                clean = validate_renames(baseline_text, merged_renames)
            except EquivalenceError as exc:
                raise ShaderOrganizationError("rename request rejected: %s" % exc) from exc
            merged_comments = dict(session.get("commentMap") or {})
            merged_comments.update(
                {str(k): str(v) for k, v in dict(comments or {}).items() if str(v or "").strip()}
            )
            renamed_comments = {clean.get(k, k): v for k, v in merged_comments.items()}
            structure_ops = list(session.get("structureOps") or [])
            proposal, missing, structure_receipts = self._regenerate_proposal(
                baseline_text, clean, structure_ops, renamed_comments
            )
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source"
            )
            atomic_write_bytes(proposal_path, proposal.encode("utf-8"))
            self._write_alias_view(root, baseline_text, clean, merged_comments, proposal)
            updated = dict(session)
            updated["renameMap"] = clean
            updated["commentMap"] = merged_comments
            updated = self._seal_session(updated)
            atomic_write_json(session_path, updated)
            check = rename_only_equivalent(baseline_text, proposal, clean) if not structure_ops else {"equal": False}
            return {
                "schema": "renderdoc-ai-shader-organization-rename/v1",
                "schemaVersion": 1,
                "workspacePath": str(root),
                "organizationId": organization_id,
                "proposalSourcePath": str(proposal_path),
                "renamesApplied": clean,
                "commentsApplied": sorted(set(renamed_comments) - set(missing)),
                "commentsNotFound": missing,
                "renameOnlyEquivalent": bool(check.get("equal")),
                "structureOps": len(structure_ops),
                "aliasesPath": str(root / "ai" / "aliases.json"),
                "viewPath": str(root / "ai" / "organized.hlsl"),
                "cbufferAliases": self._publish_cbuffer_aliases(
                    baseline_text, clean, session.get("cbufferAliasIdentity")
                ),
                "nextTool": "review_shader_workspace_organization",
                "applied": False,
            }

    @staticmethod
    def _regenerate_proposal(
        baseline_text: str,
        renames: Mapping[str, str],
        structure_ops: Sequence[Mapping[str, Any]],
        comments: Mapping[str, str],
    ) -> tuple[str, list[str], list[dict[str, Any]]]:
        """baseline -> renames -> structural operations -> comments, in that fixed order."""

        from .restructure import RestructureError, apply_operations

        proposal = apply_renames(baseline_text, dict(renames))
        receipts: list[dict[str, Any]] = []
        if structure_ops:
            try:
                proposal, receipts = apply_operations(proposal, structure_ops)
            except RestructureError as exc:
                raise ShaderOrganizationError("structural operations no longer apply: %s" % exc) from exc
        try:
            proposal, missing = insert_function_comments(proposal, dict(comments))
        except EquivalenceError as exc:
            raise ShaderOrganizationError("comment request rejected: %s" % exc) from exc
        return proposal, missing, receipts

    def restructure(
        self,
        workspace_path: str,
        organization_id: str,
        operation: str,
        *,
        start_line: int = 0,
        end_line: int = 0,
        name: str = "",
        new_name: str = "",
        comment: str = "",
    ) -> dict[str, Any]:
        """Host-applied structural edit of the proposal (extract / undo / remove / rename_function / list).

        Operations are sealed into the session and replayed on top of the
        renamed baseline, then the result must compile through the workspace's
        bound route before it is kept.  A rejected operation leaves the
        session exactly as it was.
        """

        from .restructure import RestructureError, structure_report

        with self._guard:
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = service.status(workspace_path)
            root, _source_path, _current_bytes, _current_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            session_path = session_dir / "session.json"
            session = _read_session_file(session_dir, "organization session")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") != "awaiting-proposal-review":
                raise ShaderOrganizationError("organization session is not awaiting review")
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_text, _baseline_bytes = _utf8(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            operation = str(operation or "").strip().lower()
            ops = [dict(item) for item in (session.get("structureOps") or [])]
            comments = dict(session.get("commentMap") or {})
            renames = dict(session.get("renameMap") or {})
            changed = True
            if operation == "extract":
                if not (isinstance(start_line, int) and isinstance(end_line, int)) or start_line <= 0 or end_line < start_line:
                    raise ShaderOrganizationError("extract needs start_line <= end_line (1-based lines of the current proposal)")
                if not str(name or "").strip():
                    raise ShaderOrganizationError("extract needs a function name")
                if len(ops) >= 200:
                    raise ShaderOrganizationError("this session already holds 200 structural operations")
                ops.append({"operation": "extract", "startLine": int(start_line), "endLine": int(end_line),
                            "name": str(name).strip(), "comment": str(comment or "").strip()[:600]})
            elif operation == "undo":
                if not ops:
                    raise ShaderOrganizationError("nothing to undo")
                removed = ops.pop()
                comments.pop(str(removed.get("name", "")), None)
            elif operation == "remove":
                index = next((i for i, item in enumerate(ops) if item.get("name") == name), None)
                if index is None:
                    raise ShaderOrganizationError("no structural operation created %r" % name)
                ops.pop(index)
                comments.pop(str(name), None)
            elif operation == "rename_function":
                index = next((i for i, item in enumerate(ops) if item.get("name") == name), None)
                if index is None:
                    raise ShaderOrganizationError("no extracted function named %r" % name)
                if not str(new_name or "").strip():
                    raise ShaderOrganizationError("rename_function needs new_name")
                ops[index]["name"] = str(new_name).strip()
                if name in comments:
                    comments[str(new_name).strip()] = comments.pop(name)
                if str(comment or "").strip():
                    ops[index]["comment"] = str(comment).strip()[:600]
            elif operation == "note":
                if not str(name or "").strip() or not str(comment or "").strip():
                    raise ShaderOrganizationError("note needs name (a function) and comment (its purpose, one or two sentences)")
                notes = dict(session.get("functionNotes") or {})
                notes[str(name).strip()] = str(comment).strip()[:600]
                session = dict(session)
                session["functionNotes"] = notes
                changed = False
            elif operation == "list":
                changed = False
            else:
                raise ShaderOrganizationError("operation must be one of: extract, undo, remove, rename_function, note, list")

            renamed_comments = {renames.get(k, k): v for k, v in comments.items()}
            try:
                proposal, missing, receipts = self._regenerate_proposal(baseline_text, renames, ops, renamed_comments)
            except ShaderOrganizationError as exc:
                return self._restructure_rejection(root, organization_id, operation, ops, str(exc), None)
            compile_result: dict[str, Any] | None = None
            if changed:
                compile_result = service.compile_check(str(root), proposal)
                if compile_result.get("ok") is False:
                    return self._restructure_rejection(root, organization_id, operation, ops,
                                                       "the result does not compile", compile_result)
                proposal_path = self._session_artifact(
                    root, session, "proposalSourcePath", session_dir, "proposal source"
                )
                atomic_write_bytes(proposal_path, proposal.encode("utf-8"))
                self._write_alias_view(root, baseline_text, renames, comments, proposal)
                updated = dict(session)
                updated["structureOps"] = ops
                updated["commentMap"] = comments
                updated = self._seal_session(updated)
                atomic_write_json(session_path, updated)
            if operation == "note":
                updated = self._seal_session(dict(session))
                atomic_write_json(session_path, updated)
            report = structure_report(proposal, entry_point=str(session.get("entryPoint") or ""))
            ledger = self._function_ledger(session, ops, receipts, report)
            return {
                "schema": "renderdoc-ai-shader-organization-restructure/v1",
                "schemaVersion": 1,
                "workspacePath": str(root),
                "organizationId": organization_id,
                "operation": operation,
                "accepted": True,
                "operations": ops if operation != "list" else len(ops),
                "receipts": receipts if operation != "list" else len(receipts),
                "lastReceipt": receipts[-1] if (receipts and operation == "extract") else None,
                "compile": compile_result,
                "ledger": ledger,
                "structure": report if operation != "list" else {"totals": report["totals"], "anonymity": report["anonymity"], "flagged": report["flagged"]},
                "proposalSourcePath": str(root / str(session["proposalSourcePath"])),
                "commentsNotFound": missing,
                "nextTool": ("get_shader_workspace_outline(relative_path=<proposalSourcePath>) to re-read line numbers, "
                             "then more restructure/rename calls, then review_shader_workspace_organization"),
                "applied": False,
            }

    @staticmethod
    def _function_ledger(
        session: Mapping[str, Any],
        ops: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
        report: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """The compact working memory for the consolidation pass.

        One row per function: Host facts (lines, interface, globals, flags,
        where it came from) plus the AI's own purpose note.  Deciding merges
        and splits from this table costs a few hundred tokens instead of
        re-reading code.
        """

        notes = session.get("functionNotes") if isinstance(session.get("functionNotes"), Mapping) else {}
        comments = session.get("commentMap") if isinstance(session.get("commentMap"), Mapping) else {}
        by_name = {str(item.get("name")): item for item in receipts}
        op_comments = {str(item.get("name")): str(item.get("comment") or "") for item in ops}
        rows = []
        for item in report.get("functions", []):
            name = item["name"]
            receipt = by_name.get(name)
            rows.append({
                "name": name,
                "purpose": str(notes.get(name) or comments.get(name) or op_comments.get(name) or ""),
                "lines": item["lines"],
                "isEntry": item.get("isEntry", False),
                "origin": ("extracted from %s lines %s" % (receipt["enclosingFunction"], receipt["sourceLines"])
                           if receipt else "original"),
                "inputs": receipt["inputs"] if receipt else item.get("parameters", []),
                "inouts": receipt["inouts"] if receipt else [],
                "outputs": ([receipt["returns"]] if receipt and receipt.get("returns") else []) + (receipt["outParams"] if receipt else []),
                "calls": item.get("calls", []),
                "callers": item.get("callers", []),
                "globalsUsed": item.get("globalsUsed", []),
                "anonymousLocals": item.get("anonymousLocals", 0),
                "flags": item.get("flags", []),
                "status": "reviewed" if (name in notes and not item.get("flags")) else ("noted" if name in notes else "provisional"),
            })
        return rows

    @staticmethod
    def _restructure_rejection(root: Path, organization_id: str, operation: str, ops: list[dict[str, Any]],
                               reason: str, compile_result: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "schema": "renderdoc-ai-shader-organization-restructure/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "organizationId": organization_id,
            "operation": operation,
            "accepted": False,
            "reason": reason,
            "compile": dict(compile_result) if compile_result else None,
            "attemptedOperations": ops,
            "counted": False,
            "next": ("adjust the line range/name using the reason (and compiler diagnostics) and call again; "
                     "the session was not changed"),
            "applied": False,
        }

    @staticmethod
    def _publish_cbuffer_aliases(
        baseline_text: str, renames: Mapping[str, str],
        captured_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mirror cbuffer block/member renames into the per-user alias store.

        The RenderDoc constant-buffer panel shows these as display names for
        every shader and capture whose block has the same name and layout.
        User-typed aliases always win; a failure here never fails the rename.
        """

        blocks: list[dict[str, Any]] = []
        published = 0
        kept_user: list[str] = []
        member_renames: list[dict[str, Any]] = []
        for publication in cbuffer_aliases.aliases_from_renames(baseline_text, dict(renames), captured_identity):
            record: dict[str, Any] = {
                "block": publication["block"], "layoutSha256": publication["layoutSha256"] or None,
                "sourceBlock": publication.get("sourceBlock"),
                "bind": publication.get("bind"), "space": publication.get("space"),
                "capturedIdentityMatched": publication.get("capturedIdentityMatched", False),
                "unmapped": publication.get("unmapped", []),
            }
            for entry in publication.get("entries", []):
                if int(entry.get("offset", -1)) >= 0 and int(entry.get("byteSize", 0)) > 0:
                    member_renames.append({
                        "space": publication.get("space"), "bind": publication.get("bind"),
                        "byteOffset": int(entry["offset"]), "byteSize": int(entry["byteSize"]),
                        "name": str(entry["alias"]), "original": str(entry.get("original") or entry.get("path")),
                        "block": publication["block"],
                    })
            if not publication["layoutSha256"]:
                record["error"] = "layout incomplete: member offsets unknown, aliases not published"
                blocks.append(record)
                continue
            try:
                report = cbuffer_aliases.publish_aliases(
                    publication["block"],
                    publication["members"],
                    publication["entries"],
                    source="ai",
                    block_alias=publication.get("blockAlias"),
                )
            except (OSError, ValueError) as exc:
                record["error"] = str(exc)
                blocks.append(record)
                continue
            published += len(report["written"]) + int(report["blockAliasWritten"])
            kept_user.extend(report["keptUser"])
            if report["blockAliasKeptUser"]:
                kept_user.append(publication["block"])
            record.update({
                "written": report["written"],
                "keptUser": report["keptUser"],
                "blockAlias": report["blockAlias"],
                "blockAliasWritten": report["blockAliasWritten"],
                "blockAliasKeptUser": report["blockAliasKeptUser"],
                "rejected": report["rejected"],
                "path": report["path"],
            })
            blocks.append(record)
        return {"published": published, "keptUser": kept_user, "blocks": blocks,
                "memberRenames": member_renames[:512],
                "storeRoot": str(cbuffer_aliases.store_root())}

    def review(self, workspace_path: str, organization_id: str) -> dict[str, Any]:
        """Return bounded content verdicts; physical failures remain exceptions."""
        try:
            return self._review_checked(workspace_path, organization_id)
        except Exception as error:
            issue = verdict_reason("shader-review-blocked", str(error), location="organization",
                direction="Resolve this exact schema, source, compiler, ABI or transaction error before publication. The failed proposal has no apply authority.",
                target="current source/candidate/ABI and returned template contract",
                example="A compatible ABI result must reference the current compiled revision.")
            used, limit = 0, 0
            try:
                with self._guard:
                    status = self._service_factory().status(workspace_path)
                    root, _, _, _ = self._workspace_source(status)
                    rounds = ReviewRounds(root)
                    used, limit = rounds.used, rounds.limit
                    if used < limit:
                        used = rounds.record(accepted=False, reasons=[issue])
            except Exception:
                # Reporting must not weaken or replace the original failure.
                pass
            error.review_verdict = build_verdict(accepted=False, level="blocked", round_used=used,
                                                 round_limit=limit, reasons=[issue],
                                                 workspace_path=str(workspace_path))
            raise

    def _review_checked(self, workspace_path: str, organization_id: str) -> dict[str, Any]:
        """Compile a proposal and compare it using the cheapest sufficient check."""

        with self._guard:
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = service.status(workspace_path)
            root, source_path, current_bytes, current_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            session_path = session_dir / "session.json"
            session = _read_session_file(session_dir, "organization session")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") != "awaiting-proposal-review":
                raise ShaderOrganizationError("organization session is not awaiting review")
            baseline_sha = _hex(
                session.get("baselineSourceSha256"), "baseline source SHA-256"
            )
            if current_sha != baseline_sha:
                raise ShaderOrganizationError(
                    "editable source changed after the organization session began"
                )
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_bytes = _regular_bytes(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            if _sha256(baseline_bytes) != baseline_sha or baseline_bytes != current_bytes:
                raise ShaderOrganizationError("organization baseline source binding changed")
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source"
            )
            proposal_text, proposal_bytes = _utf8(
                proposal_path, MAX_SOURCE_BYTES, "organized HLSL proposal"
            )
            # Reject source-expanding/diagnostic-remapping directives before
            # this review invokes any compiler, including its authoritative
            # baseline recompile.
            _validate_source_directives(proposal_text, "organized HLSL proposal")
            proposal_sha = _sha256(proposal_bytes)
            analysis_path = self._session_artifact(
                root, session, "analysisPath", session_dir, "analysis"
            )
            analysis, analysis_bytes = _utf8(
                analysis_path, MAX_ANALYSIS_BYTES, "organization analysis"
            )
            flowchart_path = self._session_artifact(
                root, session, "flowchartPath", session_dir, "flowchart"
            )
            flowchart, flowchart_bytes = _utf8(
                flowchart_path, MAX_FLOWCHART_BYTES, "organization flowchart"
            )
            module_descriptions_path = self._session_artifact(
                root,
                session,
                "moduleDescriptionsPath",
                session_dir,
                "module descriptions",
            )
            module_reasons: list[dict[str, str]] = []
            try:
                module_descriptions = load_module_descriptions(
                    module_descriptions_path
                )
            except ShaderFlowGraphError as exc:
                module_descriptions = {}
                module_reasons.append(verdict_reason("module-description-schema", str(exc), location="module_descriptions",
                    direction="Use moduleDescriptionsTemplate with schema, schemaVersion and a modules array.", target="moduleDescriptionsTemplate",
                    example='{"schema":"replaylab-shader-module-descriptions/v1","schemaVersion":1,"modules":[{"name":"MainPS","description":"Returns color"}]}'))
            known_functions = {r["name"] for r in _function_records_for_template(proposal_text)}
            rename_map = session.get("renameMap") or {}
            module_descriptions = {rename_map.get(name, name): description for name, description in module_descriptions.items()}
            for name in set(module_descriptions) - known_functions:
                module_reasons.append(verdict_reason("module-description-function", "Description names a function absent from the proposal: " + name,
                    location="module_descriptions.modules", direction="Use only actual proposal function names.", target=", ".join(sorted(known_functions)[:16]), example=next(iter(sorted(known_functions)), "MainPS")))
            structured_flow_graph_path = self._session_artifact(
                root,
                session,
                "structuredFlowGraphPath",
                session_dir,
                "structured flow graph",
            )
            if flowchart_bytes.strip() not in (b"", _FLOWCHART_PLACEHOLDER.strip()):
                _validate_safe_mermaid(flowchart)
            # Historical Mermaid remains in its input file for inspection;
            # newly published diagrams always use the fixed Host renderer.
            generate_flowchart = True
            content_reasons = module_reasons + analysis_markdown_issues(analysis)
            flow_spec_path = session_dir / "flow-spec.json"
            clean_spec: dict[str, Any] = {}
            if flow_spec_path.is_file():
                try:
                    raw_spec = read_json_object(flow_spec_path, MAX_CONTEXT_BYTES, "flow spec")
                    symbols = declared_symbols(proposal_text)
                    values = flow_value_bindings(proposal_text)
                    clean_spec, spec_reasons = validate_flow_spec(
                        raw_spec, functions=[r["name"] for r in _function_records_for_template(proposal_text)],
                        resources=symbols["resources"] + symbols["cbuffers"], outputs=symbols["outputs"],
                        inputs=values["inputs"], fields=values["fields"], source=proposal_text)
                    content_reasons.extend(spec_reasons)
                except (ValueError, ShaderFlowGraphError) as exc:
                    content_reasons.append(verdict_reason("flow-spec-schema", str(exc), location="flow_spec", direction="Fill the exact flowSpecTemplate returned by begin.", example='{"schema":"replaylab-shader-flow-spec/v1", ...}'))
            if not any(reason.get("severity") != "warning" for reason in content_reasons):
                content_reasons.extend(organization_structure_issues(
                    baseline_bytes.decode("utf-8", errors="strict"), proposal_text, clean_spec, rename_map))
            rounds = ReviewRounds(root)
            user_soft_publish = rounds.user_soft_publish_allowed
            if user_soft_publish:
                # The user chose "publish the validated baseline as-is": content
                # blockers become recorded warnings and the review proceeds to
                # the baseline-retaining publication below.
                content_reasons = _waive_blockers(content_reasons)
            if rounds.used >= rounds.limit and not user_soft_publish:
                content_reasons.append(verdict_reason("review-budget-exhausted", "This workspace's automatic review budget is exhausted.", location="organization", direction="Ask the user (verdict.userDecision) instead of retrying; do not open another session to evade the limit."))
            # Readability warnings are feedback, not a substitute for the
            # compile/ABI/equivalence decision. Only blockers stop that review.
            if any(reason.get("severity") != "warning" for reason in content_reasons):
                round_used = rounds.used if rounds.used >= rounds.limit else rounds.record(accepted=False, reasons=content_reasons)
                verdict = build_verdict(accepted=False, level="content-rejected", round_used=round_used, round_limit=rounds.limit, reasons=content_reasons, workspace_path=str(root))
                gate = self._inactive_gate(organization_id=organization_id, status=status,
                    decision="organization-content-rejected", reason="Analysis or flow fields did not pass the template contract.", source_sha256=current_sha)
                gate["verdict"] = verdict
                self._publish_root_gate(root, gate)
                return {"schema": "renderdoc-ai-shader-organization-review/v1", "schemaVersion": 1,
                        "workspacePath": str(root), "organizationId": organization_id,
                        "gate": gate, "verdict": verdict, "applied": False}

            sealed_baseline_code_sha = _hex(
                session.get("baselineCodeSha256"), "baseline code SHA-256"
            )
            lease = WorkspaceSourceTransactionLease(root)
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                self._publish_root_gate(
                    root,
                    self._inactive_gate(
                        organization_id=organization_id,
                        status=status,
                        decision="review-blocked-active-source-transaction",
                        reason=str(exc),
                        source_sha256=current_sha,
                    ),
                )
                raise ShaderOrganizationError(
                    "organization review requires the exclusive workspace source "
                    "transaction lease; an active watcher/live edit or crash residue "
                    "must be stopped or inspected first"
                ) from exc

            source_replaced = False
            review_baseline_compile: dict[str, Any] | None = None
            review_baseline_validation: dict[str, Any] | None = None
            organized_compile: dict[str, Any] | None = None
            organized_validation: dict[str, Any] | None = None
            recovery_compile: dict[str, Any] | None = None
            recovery_validation: dict[str, Any] | None = None
            try:
                # Re-read every workspace/session binding after obtaining the
                # lease. Watcher start uses this same atomic lease, so neither
                # transaction can enter while the other owns source mutation.
                locked_status = service.status(workspace_path)
                locked_root, locked_source_path, locked_bytes, locked_sha = (
                    self._workspace_source(locked_status)
                )
                self._validate_session(
                    session, locked_root, organization_id, locked_status
                )
                if (
                    locked_root != root
                    or locked_source_path != source_path
                    or locked_sha != baseline_sha
                    or locked_bytes != baseline_bytes
                ):
                    raise ShaderOrganizationError(
                        "editable source changed before organization review acquired ownership"
                    )

                # Reuse the Host-validated current baseline while its exact
                # source/candidate/ABI binding remains fresh. Hashes identify
                # that evidence; they do not impose identity on AI edits.
                review_baseline_compile, review_baseline_validation = (
                    self._compile_and_validate(
                        service,
                        root,
                        baseline_sha,
                        "authoritative organization review baseline",
                    )
                )
                baseline_code_sha = review_baseline_compile["codeSha256"]
                if baseline_code_sha != sealed_baseline_code_sha:
                    raise ShaderOrganizationError(
                        "authoritative baseline executable code changed after begin"
                    )

                if not _source_compare_and_swap(
                    source_path,
                    baseline_bytes,
                    proposal_bytes,
                    "workspace HLSL",
                ):
                    raise ShaderOrganizationError(
                        "editable source changed before proposal promotion"
                    )
                source_replaced = True
                organized_compile, organized_validation = self._compile_and_validate(
                    service, root, proposal_sha, "organized HLSL proposal"
                )
                code_equal = organized_compile["codeSha256"] == baseline_code_sha
                # Level B: baseline + declared renames == proposal (names,
                # comments and whitespace only).  DXIL metadata carries names,
                # so this is the level that admits readable resource names.
                equivalence = rename_only_equivalent(
                    baseline_bytes.decode("utf-8", errors="replace"),
                    proposal_text,
                    session.get("renameMap") if isinstance(session.get("renameMap"), Mapping) else {},
                )
                rename_only_equal = bool(equivalence.get("equal"))
                accepted = code_equal or rename_only_equal
                equivalence_level = (
                    "exact-code" if code_equal else ("rename-only" if rename_only_equal else "rejected")
                )
                observation: dict[str, Any] | None = None
                if not accepted and self._compare_candidates is not None:
                    observation_value = self._compare_candidates(
                        str(root), review_baseline_compile, organized_compile
                    )
                    observation = self._validate_observation(
                        observation_value,
                        baseline_source_sha=baseline_sha,
                        candidate_source_sha=proposal_sha,
                        baseline_candidate_sha=review_baseline_compile["rawSha256"],
                        candidate_sha=organized_compile["rawSha256"],
                    )
                    accepted = observation["accepted"]
                    if accepted:
                        equivalence_level = "observational-pass"
                # Keep non-blocking content feedback in the verdict that is
                # sealed with the session and persisted in both gate files.
                review_reasons: list[dict[str, str]] = list(content_reasons)
                if not accepted:
                    if observation is not None:
                        review_reasons.append(verdict_reason(
                            "output-comparison",
                            str(observation.get("reason") or "Captured output comparison did not pass"),
                            location="organized-proposal.hlsl",
                            severity="warning",
                            direction="inspect the comparison metrics and revise one complete function or computation stage before reviewing again",
                        ))
                    else:
                        review_reasons.extend(differences_to_reasons(equivalence.get("differences", [])))
                if not accepted and user_soft_publish:
                    review_reasons = _waive_blockers(review_reasons)
                round_used = rounds.record(accepted=accepted, reasons=review_reasons)
                soft_accept = (not accepted) and (
                    user_soft_publish
                    or (rounds.exhausted(round_used) and not any(r["severity"] == "blocker" for r in review_reasons))
                )
                verdict = build_verdict(
                    accepted=accepted,
                    level=equivalence_level,
                    round_used=round_used,
                    round_limit=rounds.limit,
                    reasons=review_reasons,
                    soft_accepted=soft_accept,
                    workspace_path=str(root),
                )
                if soft_accept and user_soft_publish:
                    verdict["userSoftPublish"] = True
                if accepted:
                    atomic_write_bytes(root / "ai" / "analysis.md", analysis_bytes)
                else:
                    # Proposal explanations are retained in the session, but
                    # cannot describe the different restored baseline as fact.
                    safe_analysis = analysis_template()
                    safe_analysis.update(overview="The compiled, ABI-validated baseline is retained. The AI proposal was not accepted.",
                        computation=["Use the source-derived function graph; proposal computations are unverified."],
                        outputs=declared_symbols(baseline_bytes.decode("utf-8"))["outputs"] or ["No color/depth output semantic was declared."],
                        observedFacts=[{"claim": "The retained baseline passed compilation and replay ABI validation.", "evidence": "baseline ABI request " + review_baseline_validation["requestId"]}], inferences=[])
                    safe_text, safe_issues = render_analysis_fields(safe_analysis)
                    if safe_issues:
                        raise ShaderOrganizationError("Host baseline summary could not be rendered")
                    atomic_write_bytes(root / "ai" / "analysis.md", safe_text.encode("utf-8"))
                if accepted:
                    decision = "accepted-executable-editable"
                    promoted = True
                    atomic_write_bytes(session_dir / "organized.hlsl", proposal_bytes)
                    atomic_write_bytes(root / "ai" / "organized.hlsl", proposal_bytes)
                else:
                    decision = (
                        "soft-accepted-baseline" if soft_accept
                        else "retained-provisional-code-change"
                    )
                    promoted = False
                    atomic_write_bytes(
                        session_dir / "organized-provisional.hlsl", proposal_bytes
                    )
                    atomic_write_bytes(
                        root / "ai" / "organized-provisional.hlsl", proposal_bytes
                    )
                    if not _source_compare_and_swap(
                        source_path,
                        proposal_bytes,
                        baseline_bytes,
                        "workspace HLSL",
                    ):
                        raise ShaderOrganizationError(
                            "editable source changed while the proposal was reviewed"
                        )
                    source_replaced = False
                    recovery_compile, recovery_validation = self._compile_and_validate(
                        service, root, baseline_sha, "organization baseline recovery"
                    )
                    if recovery_compile["codeSha256"] != baseline_code_sha:
                        raise ShaderOrganizationError(
                            "the same compiler produced unstable baseline executable code"
                        )

                final_source_sha = proposal_sha if accepted else baseline_sha
                final_compile = organized_compile if accepted else recovery_compile
                final_validation = (
                    organized_validation if accepted else recovery_validation
                )
                final_binding = self._candidate_binding(
                    final_source_sha, final_compile, final_validation
                )
                reviewed_at = _utc_now()
                final_source_bytes = proposal_bytes if accepted else baseline_bytes
                try:
                    final_source_text = final_source_bytes.decode(
                        "utf-8", errors="strict"
                    )
                    flow_graph = build_shader_flow_graph(
                        final_source_text,
                        workspace_id=session["workspaceId"],
                        source_path=session["sourcePath"],
                        source_sha256=final_source_sha,
                        source_revision=final_binding["revision"],
                        stage=session["stage"],
                        entry_point=session["entryPoint"],
                        generated_at=reviewed_at,
                        origin=(
                            "organized-proposal"
                            if accepted
                            else "recovered-baseline"
                        ),
                        descriptions=module_descriptions if accepted else {},
                        flow_spec=clean_spec if accepted else None,
                    )
                except (UnicodeError, ShaderFlowGraphError) as exc:
                    raise ShaderOrganizationError(
                        "organized HLSL flow graph could not be generated: %s"
                        % exc
                    ) from exc
                root_flow_graph_path = root / "ai" / "flow-graph.json"
                atomic_write_json(structured_flow_graph_path, flow_graph)
                atomic_write_json(root_flow_graph_path, flow_graph)
                generated_mermaid = mermaid_from_flow_graph(flow_graph).encode("utf-8")
                if accepted and clean_spec.get("stages"):
                    atomic_write_json(root / "ai" / "flow-spec.json", clean_spec)
                verdict["flowSpecAccepted"] = accepted and bool(clean_spec.get("stages"))
                atomic_write_bytes(root / "ai" / "flowchart.generated.mmd", generated_mermaid)
                if generate_flowchart:
                    atomic_write_bytes(root / "ai" / "flowchart.mmd", generated_mermaid)
                flow_graph_bytes = _regular_bytes(
                    root_flow_graph_path,
                    MAX_FLOW_GRAPH_BYTES,
                    "published Shader flow graph",
                )
                session_flow_graph_bytes = _regular_bytes(
                    structured_flow_graph_path,
                    MAX_FLOW_GRAPH_BYTES,
                    "organization session Shader flow graph",
                )
                if session_flow_graph_bytes != flow_graph_bytes:
                    raise ShaderOrganizationError(
                        "published and session Shader flow graphs differ"
                    )
                flow_graph_sha256 = _sha256(flow_graph_bytes)

                gate = {
                    "schema": ORGANIZATION_GATE_SCHEMA,
                    "schemaVersion": 1,
                    "policyVersion": ORGANIZATION_POLICY_VERSION,
                    "organizationId": organization_id,
                    "reviewedAt": reviewed_at,
                    "requestedTransformation": "organize-only",
                    "workspaceId": session["workspaceId"],
                    "captureId": session["captureId"],
                    "stage": session["stage"],
                    "entryPoint": session["entryPoint"],
                    "baselineSourceSha256": baseline_sha,
                    "proposalSourceSha256": proposal_sha,
                    "baselineCodeSha256": baseline_code_sha,
                    "organizedCodeSha256": organized_compile["codeSha256"],
                    "baselineReviewRevision": review_baseline_compile["revision"],
                    "baselineReviewCandidateRawSha256": review_baseline_compile[
                        "rawSha256"
                    ],
                    "baselineReviewAbiRequestId": review_baseline_validation[
                        "requestId"
                    ],
                    "baselineReviewAbiReportSha256": review_baseline_validation[
                        "abiReportSha256"
                    ],
                    "codeSha256Equal": code_equal,
                    "equivalenceLevel": equivalence_level,
                    "observation": observation,
                    "renameMap": dict(session.get("renameMap") or {}),
                    "verdict": verdict,
                    "softPublishAllowed": soft_accept,
                    "flowchartGenerated": generate_flowchart,
                    "decision": decision,
                    "organizedSourcePromoted": promoted,
                    "executableEditableSourcePath": str(source_path),
                    "proposalSourcePath": str(proposal_path),
                    "baselineCompiledAndAbiValidated": True,
                    "organizedCompiledAndAbiValidated": True,
                    "rawRecoveryCompiledAndAbiValidated": (
                        recovery_compile is not None and recovery_validation is not None
                    ),
                    "semanticEquivalenceVerified": False,
                    "replayApplied": False,
                    "safeToStartLiveEdit": accepted or soft_accept,
                    "finalBinding": final_binding,
                    "finalSourceSha256": final_binding["sourceSha256"],
                    "finalRevision": final_binding["revision"],
                    "finalCandidateRawSha256": final_binding[
                        "candidateRawSha256"
                    ],
                    "finalCodeSha256": final_binding["codeSha256"],
                    "finalAbiRequestId": final_binding["abiRequestId"],
                    "finalAbiReportSha256": final_binding[
                        "abiReportSha256"
                    ],
                    "flowGraphId": flow_graph["graphId"],
                    "flowGraphSha256": flow_graph_sha256,
                    "flowGraphArtifactPath": str(root_flow_graph_path),
                    "note": (
                        "Exact code and declared renames use static proof; observational-pass "
                        "covers only measured capture outputs within the recorded tolerances. "
                        "Soft acceptance keeps the validated baseline. Semantic equivalence "
                        "for every input remains unverified."
                    ),
                }
                atomic_write_json(session_dir / "organized-compile.json", organized_compile)
                atomic_write_json(session_dir / "organized-abi.json", organized_validation)
                if recovery_compile is not None:
                    atomic_write_json(session_dir / "recovery-compile.json", recovery_compile)
                    atomic_write_json(session_dir / "recovery-abi.json", recovery_validation)
                self._require_final_binding(service, root, session, final_binding)
                completed = dict(session)
                completed["state"] = (
                    "completed" if accepted or soft_accept else "awaiting-proposal-review"
                )
                completed["completedAt"] = gate["reviewedAt"]
                completed["decision"] = decision
                completed["proposalSourceSha256"] = proposal_sha
                completed["organizedCodeSha256"] = organized_compile["codeSha256"]
                completed["codeSha256Equal"] = code_equal
                completed["equivalenceLevel"] = equivalence_level
                completed["observation"] = observation
                completed["softPublishAllowed"] = soft_accept
                completed["verdict"] = verdict
                completed["organizedSourcePromoted"] = promoted
                completed["finalBinding"] = final_binding
                completed["flowGraphId"] = flow_graph["graphId"]
                completed["flowGraphSha256"] = flow_graph_sha256
                completed = self._seal_session(completed)
                # Commit all supporting evidence and the final sealed session
                # before publishing either gate. The root gate is the final
                # filesystem operation on the success path, so safe=true can
                # never precede session commit.
                atomic_write_json(session_path, completed)
                atomic_write_json(session_dir / "gate.json", gate)
                self._require_final_binding(service, root, session, final_binding)
                if accepted or soft_accept:
                    if is_within(root, self._completed_reviews.path.resolve()):
                        raise ShaderOrganizationError(
                            "completed shader review store must be outside the workspace"
                        )
                    self._completed_reviews.remember(completed)
                self._publish_root_gate(root, gate)
                return {
                    "schema": "renderdoc-ai-shader-organization-review/v1",
                    "schemaVersion": 1,
                    "workspacePath": str(root),
                    "organizationId": organization_id,
                    "sourcePath": str(source_path),
                    "analysisPath": str(root / "ai" / "analysis.md"),
                    "flowchartPath": str(root / "ai" / "flowchart.mmd"),
                    "flowGraphPath": str(root_flow_graph_path),
                    "flowGraphSha256": flow_graph_sha256,
                    "flowGraph": flow_graph,
                    "gatePath": str(root / "ai" / "organize-code-gate.json"),
                    "gate": gate,
                    "verdict": verdict,
                    "equivalenceLevel": equivalence_level,
                    "flowchartGeneratedPath": str(root / "ai" / "flowchart.generated.mmd"),
                    "organizedCompile": organized_compile,
                    "organizedAbi": organized_validation,
                    "recoveryCompile": recovery_compile,
                    "recoveryAbi": recovery_validation,
                    "applied": False,
                }
            except Exception as error:
                recovery_error = ""
                restored = False
                failure_compile: dict[str, Any] | None = None
                failure_validation: dict[str, Any] | None = None
                observed_sha = ""
                try:
                    observed = _regular_bytes(
                        source_path, MAX_SOURCE_BYTES, "workspace HLSL recovery"
                    )
                    observed_sha = _sha256(observed)
                    if observed == baseline_bytes:
                        restored = True
                    elif source_replaced and observed == proposal_bytes:
                        restored = _source_compare_and_swap(
                            source_path,
                            proposal_bytes,
                            baseline_bytes,
                            "workspace HLSL recovery",
                        )
                    else:
                        recovery_error = (
                            "source changed concurrently; refusing to overwrite newer bytes"
                        )
                    if restored:
                        failure_compile, failure_validation = self._compile_and_validate(
                            service,
                            root,
                            baseline_sha,
                            "organization exception recovery",
                        )
                        authoritative_code_sha = (
                            review_baseline_compile.get("codeSha256")
                            if isinstance(review_baseline_compile, dict)
                            else sealed_baseline_code_sha
                        )
                        if failure_compile["codeSha256"] != authoritative_code_sha:
                            raise ShaderOrganizationError(
                                "exception recovery produced different executable code"
                            )
                        observed_sha = baseline_sha
                        atomic_write_json(
                            session_dir / "recovery-compile.json", failure_compile
                        )
                        atomic_write_json(
                            session_dir / "recovery-abi.json", failure_validation
                        )
                except Exception as recovery:
                    recovery_error = str(recovery)
                failure = {
                    "schema": "renderdoc-ai-shader-organization-failure/v1",
                    "at": _utc_now(),
                    "phase": "proposal-review",
                    "error": str(error),
                    "recoveryError": recovery_error,
                    "sourceRestored": restored,
                    "semanticEquivalenceVerified": False,
                    "replayApplied": False,
                }
                try:
                    atomic_write_json(session_dir / "failure.json", failure)
                    failure_gate = self._inactive_gate(
                        organization_id=organization_id,
                        status=status,
                        decision="organization-review-failed",
                        reason=str(error),
                        source_sha256=observed_sha or current_sha,
                        compile_result=failure_compile,
                        validation=failure_validation,
                        source_restored=restored,
                    )
                    atomic_write_json(session_dir / "gate.json", failure_gate)
                    self._publish_root_gate(root, failure_gate)
                except Exception as gate_error:
                    if recovery_error:
                        recovery_error += "; gate failure: %s" % gate_error
                    else:
                        recovery_error = "gate failure: %s" % gate_error
                if recovery_error:
                    raise ShaderOrganizationError(
                        "organization review failed (%s); recovery failed (%s)"
                        % (error, recovery_error)
                    ) from error
                raise ShaderOrganizationError(
                    "organization review failed; baseline source restored: %s" % error
                ) from error
            finally:
                lease.release()

    def validate_publication_gate(
        self,
        workspace_path: str,
        status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one trusted organize-only publication gate.

        The root and per-session gate files live below the AI-editable tree and
        are therefore consistency records, not authority by themselves.  The
        completed session must match this process's seal or an exact completed
        review retained by the Host outside the workspace. Callers which use this
        result as a publication precondition must hold the workspace source
        transaction lease across this validation and their publication commit.
        This method deliberately does not acquire ``self._guard``: begin/review
        take that guard before the same source lease, so doing the reverse here
        would create a lock-order cycle.  The HMAC key is immutable after init.
        """

        service = self._service_factory()
        if status is None:
            observed_status = service.status(workspace_path)
        elif isinstance(status, Mapping):
            observed_status = status
        else:
            raise ShaderOrganizationError(
                "organization publication status is invalid"
            )

        root, source_path, source_bytes, source_sha = self._workspace_source(
            observed_status
        )
        if Path(workspace_path).expanduser().resolve() != root:
            raise ShaderOrganizationError(
                "organization publication workspace identity changed"
            )

        ai_dir = _safe_dir(root / "ai", root, "organization AI directory", create=False)
        sessions_dir = _safe_dir(
            ai_dir / "organization-sessions",
            ai_dir,
            "organization sessions directory",
            create=False,
        )
        root_gate = read_json_object(
            ai_dir / "organize-code-gate.json",
            MAX_CONTEXT_BYTES,
            "organization publication root gate",
        )
        if (
            root_gate.get("schema") != ORGANIZATION_GATE_SCHEMA
            or root_gate.get("schemaVersion") != 1
            or root_gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION
        ):
            raise ShaderOrganizationError(
                "organization publication gate schema is invalid"
            )

        organization_id = _hex(
            root_gate.get("organizationId"),
            "organization publication ID",
            32,
        )
        session_dir = _safe_dir(
            sessions_dir / organization_id,
            sessions_dir,
            "organization publication session directory",
            create=False,
        )
        session = _read_session_file(session_dir, "organization publication session")
        self._validate_session(
            session, root, organization_id, observed_status
        )
        soft_publish = session.get("decision") == "soft-accepted-baseline"
        if (
            session.get("state") != "completed"
            or session.get("decision") not in (
                "accepted-executable-editable", "soft-accepted-baseline"
            )
            or session.get("organizedSourcePromoted") is not (not soft_publish)
            or (soft_publish and session.get("softPublishAllowed") is not True)
        ):
            raise ShaderOrganizationError(
                "organization publication session is not completed and accepted"
            )

        session_gate = read_json_object(
            session_dir / "gate.json",
            MAX_CONTEXT_BYTES,
            "organization publication session gate",
        )
        if root_gate != session_gate:
            raise ShaderOrganizationError(
                "organization root and session publication gates differ"
            )

        binding = root_gate.get("finalBinding")
        sealed_binding = session.get("finalBinding")
        binding_fields = {
            "complete",
            "sourceSha256",
            "revision",
            "candidateRawSha256",
            "codeSha256",
            "abiRequestId",
            "abiReportSha256",
        }
        if (
            not isinstance(binding, Mapping)
            or not isinstance(sealed_binding, Mapping)
            or set(binding) != binding_fields
            or set(sealed_binding) != binding_fields
            or dict(binding) != dict(sealed_binding)
            or binding.get("complete") is not True
        ):
            raise ShaderOrganizationError(
                "organization publication final binding is invalid"
            )

        if (
            root_gate.get("requestedTransformation") != "organize-only"
            or root_gate.get("decision") != session.get("decision")
            or root_gate.get("organizedSourcePromoted") is not (not soft_publish)
            or root_gate.get("baselineCompiledAndAbiValidated") is not True
            or root_gate.get("organizedCompiledAndAbiValidated") is not True
            or root_gate.get("rawRecoveryCompiledAndAbiValidated") is not soft_publish
            or root_gate.get("semanticEquivalenceVerified") is not False
            or root_gate.get("replayApplied") is not False
            or root_gate.get("safeToStartLiveEdit") is not True
        ):
            raise ShaderOrganizationError(
                "organization publication gate is not an accepted organize-only review"
            )

        sealed_pairs = (
            ("organizationId", "organizationId"),
            ("workspaceId", "workspaceId"),
            ("captureId", "captureId"),
            ("stage", "stage"),
            ("entryPoint", "entryPoint"),
            ("baselineSourceSha256", "baselineSourceSha256"),
            ("proposalSourceSha256", "proposalSourceSha256"),
            ("baselineCodeSha256", "baselineCodeSha256"),
            ("organizedCodeSha256", "organizedCodeSha256"),
            ("codeSha256Equal", "codeSha256Equal"),
            ("equivalenceLevel", "equivalenceLevel"),
            ("observation", "observation"),
            ("softPublishAllowed", "softPublishAllowed"),
            ("verdict", "verdict"),
            ("renameMap", "renameMap"),
            ("decision", "decision"),
            ("organizedSourcePromoted", "organizedSourcePromoted"),
            ("reviewedAt", "completedAt"),
            ("flowGraphId", "flowGraphId"),
            ("flowGraphSha256", "flowGraphSha256"),
        )
        if any(
            root_gate.get(gate_key) != session.get(session_key)
            for gate_key, session_key in sealed_pairs
        ):
            raise ShaderOrganizationError(
                "organization publication gate differs from its sealed session"
            )

        baseline_source_sha = _hex(
            session.get("baselineSourceSha256"),
            "organization baseline source SHA-256",
        )
        proposal_source_sha = _hex(
            session.get("proposalSourceSha256"),
            "organization proposal source SHA-256",
        )
        baseline_code_sha = _hex(
            session.get("baselineCodeSha256"),
            "organization baseline code SHA-256",
        )
        organized_code_sha = _hex(
            session.get("organizedCodeSha256"),
            "organization organized code SHA-256",
        )
        final_source_sha = _hex(
            binding.get("sourceSha256"),
            "organization final source SHA-256",
        )
        final_candidate_sha = _hex(
            binding.get("candidateRawSha256"),
            "organization final candidate SHA-256",
        )
        final_code_sha = _hex(
            binding.get("codeSha256"),
            "organization final code SHA-256",
        )
        final_request_id = _hex(
            binding.get("abiRequestId"),
            "organization final ABI request ID",
        )
        final_report_sha = _hex(
            binding.get("abiReportSha256"),
            "organization final ABI report SHA-256",
        )
        final_revision = _positive_int(
            binding.get("revision"), "organization final revision"
        )
        flow_graph_id = _hex(
            root_gate.get("flowGraphId"),
            "organization Shader flow graph ID",
            32,
        )
        flow_graph_sha256 = _hex(
            root_gate.get("flowGraphSha256"),
            "organization Shader flow graph SHA-256",
        )

        baseline_path = self._session_artifact(
            root,
            session,
            "baselineSourcePath",
            session_dir,
            "organization publication baseline source",
        )
        proposal_path = self._session_artifact(
            root,
            session,
            "proposalSourcePath",
            session_dir,
            "organization publication proposal source",
        )
        baseline_bytes = _regular_bytes(
            baseline_path,
            MAX_SOURCE_BYTES,
            "organization publication baseline source",
        )
        proposal_bytes = _regular_bytes(
            proposal_path,
            MAX_SOURCE_BYTES,
            "organization publication proposal source",
        )
        declared_source_path = Path(
            str(root_gate.get("executableEditableSourcePath", ""))
        )
        declared_proposal_path = Path(
            str(root_gate.get("proposalSourcePath", ""))
        )
        if not declared_source_path.is_absolute():
            declared_source_path = root / declared_source_path
        if not declared_proposal_path.is_absolute():
            declared_proposal_path = root / declared_proposal_path
        if (
            declared_source_path.is_symlink()
            or declared_proposal_path.is_symlink()
            or declared_source_path.resolve() != source_path
            or declared_proposal_path.resolve() != proposal_path
            or _sha256(baseline_bytes) != baseline_source_sha
            or _sha256(proposal_bytes) != proposal_source_sha
            or (baseline_bytes if soft_publish else proposal_bytes) != source_bytes
        ):
            raise ShaderOrganizationError(
                "organization publication source evidence changed"
            )

        # Code hashes identify evidence. Only the fast exact-code path needs
        # equality; rename proof and Host measurements authorize other paths.
        code_equal = baseline_code_sha == organized_code_sha
        equivalence_level = root_gate.get("equivalenceLevel")
        if (
            root_gate.get("codeSha256Equal") is not code_equal
            or equivalence_level not in (
                "exact-code", "rename-only", "observational-pass", "rejected"
            )
            or (code_equal and equivalence_level != "exact-code")
            or (equivalence_level == "exact-code" and not code_equal)
            or (soft_publish != (equivalence_level == "rejected"))
        ):
            raise ShaderOrganizationError(
                "organization publication equivalence level contradicts its code evidence"
            )
        if equivalence_level == "rename-only":
            try:
                baseline_text = baseline_bytes.decode("utf-8", errors="strict")
                proposal_text = proposal_bytes.decode("utf-8", errors="strict")
                renames = validate_renames(baseline_text, session.get("renameMap"))
                equivalent = rename_only_equivalent(baseline_text, proposal_text, renames)
            except (EquivalenceError, UnicodeDecodeError) as exc:
                raise ShaderOrganizationError(
                    "organization publication rename-only evidence is invalid"
                ) from exc
            if equivalent.get("equal") is not True:
                raise ShaderOrganizationError(
                    "organization publication rename-only source proof failed"
                )
        if equivalence_level == "observational-pass":
            observation = self._validate_observation(
                session.get("observation"),
                baseline_source_sha=baseline_source_sha,
                candidate_source_sha=proposal_source_sha,
                baseline_candidate_sha=root_gate.get("baselineReviewCandidateRawSha256"),
                candidate_sha=final_candidate_sha,
            )
            if observation.get("accepted") is not True:
                raise ShaderOrganizationError(
                    "organization publication lacks accepted Host output comparison"
                )
        verdict = session.get("verdict")
        if not isinstance(verdict, Mapping) or (
            verdict.get("softAccepted") is not soft_publish
            or verdict.get("accepted") is not (not soft_publish)
        ):
            raise ShaderOrganizationError("organization publication verdict is inconsistent")

        declared_flow_graph_path = Path(
            str(root_gate.get("flowGraphArtifactPath", ""))
        )
        if not declared_flow_graph_path.is_absolute():
            declared_flow_graph_path = root / declared_flow_graph_path
        root_flow_graph_path = (root / "ai" / "flow-graph.json").resolve()
        session_flow_graph_path = self._session_artifact(
            root,
            session,
            "structuredFlowGraphPath",
            session_dir,
            "organization publication Shader flow graph",
        )
        if (
            declared_flow_graph_path.is_symlink()
            or declared_flow_graph_path.resolve() != root_flow_graph_path
            or root_flow_graph_path.is_symlink()
            or not is_within(root, root_flow_graph_path)
        ):
            raise ShaderOrganizationError(
                "organization publication Shader flow graph path is invalid"
            )
        root_flow_graph_bytes = _regular_bytes(
            root_flow_graph_path,
            MAX_FLOW_GRAPH_BYTES,
            "organization publication Shader flow graph",
        )
        session_flow_graph_bytes = _regular_bytes(
            session_flow_graph_path,
            MAX_FLOW_GRAPH_BYTES,
            "organization session Shader flow graph",
        )
        if (
            root_flow_graph_bytes != session_flow_graph_bytes
            or _sha256(root_flow_graph_bytes) != flow_graph_sha256
        ):
            raise ShaderOrganizationError(
                "organization publication Shader flow graph content changed"
            )
        try:
            flow_graph_value = read_json_object(
                root_flow_graph_path,
                MAX_FLOW_GRAPH_BYTES,
                "organization publication Shader flow graph",
            )
            validated_flow_graph = validate_shader_flow_graph(
                flow_graph_value,
                expected_workspace_id=str(session["workspaceId"]),
                expected_source_sha256=final_source_sha,
                expected_source_revision=final_revision,
                expected_source_path=str(session["sourcePath"]),
            )
        except (ValueError, ShaderFlowGraphError) as exc:
            raise ShaderOrganizationError(
                "organization publication Shader flow graph is invalid: %s" % exc
            ) from exc
        if validated_flow_graph["graphId"] != flow_graph_id:
            raise ShaderOrganizationError(
                "organization publication Shader flow graph ID changed"
            )

        latest = observed_status.get("latestCompiled")
        abi_gate = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(abi_gate, Mapping)
            or latest.get("replayAbiValidated") is not True
            or latest.get("abiCompatible") is not True
            or latest.get("requiresReplayAbiValidation") is not False
            or latest.get("validationScope")
            != "replay-reflection-abi-compatible"
            or latest.get("semanticEquivalenceVerified") is not False
            or observed_status.get("replayAbiValidated") is not True
            or observed_status.get("abiCompatible") is not True
            or abi_gate.get("status") != "compatible"
        ):
            raise ShaderOrganizationError(
                "organization publication candidate lacks current replay ABI validation"
            )

        latest_source_sha = _hex(
            latest.get("sourceSha256"),
            "organization publication candidate source SHA-256",
        )
        latest_candidate_sha = _hex(
            latest.get("rawSha256"),
            "organization publication candidate SHA-256",
        )
        latest_code_sha = _hex(
            latest.get("codeSha256"),
            "organization publication candidate code SHA-256",
        )
        latest_request_id = _hex(
            abi_gate.get("requestId"),
            "organization publication ABI request ID",
        )
        latest_report_sha = _hex(
            abi_gate.get("reportSha256"),
            "organization publication ABI report SHA-256",
        )
        _hex(
            abi_gate.get("baselineAbiSha256"),
            "organization publication baseline ABI SHA-256",
        )
        _hex(
            abi_gate.get("candidateAbiSha256"),
            "organization publication candidate ABI SHA-256",
        )
        _positive_int(
            root_gate.get("baselineReviewRevision"),
            "organization baseline review revision",
        )
        for field, label in (
            (
                "baselineReviewCandidateRawSha256",
                "organization baseline review candidate SHA-256",
            ),
            (
                "baselineReviewAbiRequestId",
                "organization baseline review ABI request ID",
            ),
            (
                "baselineReviewAbiReportSha256",
                "organization baseline review ABI report SHA-256",
            ),
        ):
            _hex(root_gate.get(field), label)

        gate_binding_projection = {
            "complete": True,
            "sourceSha256": root_gate.get("finalSourceSha256"),
            "revision": root_gate.get("finalRevision"),
            "candidateRawSha256": root_gate.get("finalCandidateRawSha256"),
            "codeSha256": root_gate.get("finalCodeSha256"),
            "abiRequestId": root_gate.get("finalAbiRequestId"),
            "abiReportSha256": root_gate.get("finalAbiReportSha256"),
        }
        if (
            gate_binding_projection != dict(binding)
            or (baseline_source_sha if soft_publish else proposal_source_sha) != final_source_sha
            or final_source_sha != source_sha
            or latest_source_sha != source_sha
            or (baseline_code_sha if soft_publish else organized_code_sha) != final_code_sha
            or latest_code_sha != final_code_sha
            or observed_status.get("revision") != final_revision
            or latest.get("revision") != final_revision
            or latest_candidate_sha != final_candidate_sha
            or latest_request_id != final_request_id
            or latest_report_sha != final_report_sha
        ):
            raise ShaderOrganizationError(
                "organization publication gate is stale or internally disconnected"
            )

        # Re-resolve status through the fixed workspace service immediately
        # before returning.  With the caller-owned source lease this is the
        # final coherent current-source/candidate/ABI publication witness.
        self._require_final_binding(service, root, session, binding)
        return dict(root_gate)

    @staticmethod
    def _validate_observation(
        value: Any,
        *,
        baseline_source_sha: str,
        candidate_source_sha: str,
        baseline_candidate_sha: str,
        candidate_sha: str,
    ) -> dict[str, Any]:
        """Validate Host-only bounded measurements; never a universal proof."""
        if not isinstance(value, Mapping) or _json_size(value) > MAX_CONTEXT_BYTES // 2:
            raise ShaderOrganizationError("Host output comparison is invalid or too large")
        result = dict(value)
        if (
            result.get("schema") != ORGANIZATION_OBSERVATION_SCHEMA
            or result.get("scope") != "captured-event-output"
            or not isinstance(result.get("accepted"), bool)
            or result.get("semanticEquivalenceVerified") is not False
            or result.get("replayRestored") is not True
            or not isinstance(result.get("metrics"), Mapping)
            or not isinstance(result.get("tolerances"), Mapping)
            or isinstance(result.get("sampleCount"), bool)
            or not isinstance(result.get("sampleCount"), int)
            or result["sampleCount"] < (1 if result["accepted"] else 0)
        ):
            raise ShaderOrganizationError("Host output comparison contract is invalid")
        for key, expected in (
            ("baselineSourceSha256", baseline_source_sha),
            ("candidateSourceSha256", candidate_source_sha),
            ("baselineCandidateRawSha256", baseline_candidate_sha),
            ("candidateRawSha256", candidate_sha),
        ):
            if _hex(result.get(key), "comparison " + key) != _hex(expected, "expected " + key):
                raise ShaderOrganizationError("Host output comparison candidate binding changed")
        return result

    def _current_compile_and_validation(
        self, service: Any, root: Path, expected_source_sha: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Reuse only current Host-verified source, compiler and ABI evidence."""
        status = service.status(str(root))
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        abi = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(abi, Mapping)
            or status.get("source", {}).get("currentSha256") != expected_source_sha
            or latest.get("sourceSha256") != expected_source_sha
            or status.get("revision") != latest.get("revision")
            or status.get("abiValidationStatus") in ("incompatible", "source-changed")
            or status.get("replayAbiValidated") is not True
            or status.get("abiCompatible") is not True
            or latest.get("abiCompatible") is not True
            or latest.get("replayAbiValidated") is not True
            or latest.get("requiresReplayAbiValidation") is not False
            or latest.get("semanticEquivalenceVerified") is not False
            or latest.get("validationScope") != "replay-reflection-abi-compatible"
            or abi.get("status") != "compatible"
        ):
            return None
        compile_result = dict(latest)
        compile_result["workspacePath"] = str(root)
        compile_result["reusedCurrentCandidate"] = True
        validation = {
            "workspacePath": str(root),
            "revision": latest["revision"],
            "requestId": abi.get("requestId"),
            "abiReportSha256": abi.get("reportSha256"),
            "compatible": True,
            "replayAbiValidated": True,
            "requiresReplayAbiValidation": False,
            "reusedCurrentEvidence": True,
        }
        binding = self._candidate_binding(expected_source_sha, compile_result, validation)
        self._require_final_binding(service, root, {
            "workspaceId": status.get("workspaceId"),
            "captureId": status.get("capture", {}).get("captureId"),
            "stage": status.get("stage"),
            "entryPoint": status.get("entryPoint"),
        }, binding)
        return compile_result, validation

    def _compile_and_validate(
        self,
        service: Any,
        root: Path,
        expected_source_sha: str,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current = self._current_compile_and_validation(service, root, expected_source_sha)
        if current is not None:
            return current
        compile_result = service.compile(str(root))
        if not isinstance(compile_result, dict):
            raise ShaderOrganizationError("%s compile returned a non-object" % label)
        revision = _positive_int(compile_result.get("revision"), "%s revision" % label)
        raw_sha = _hex(compile_result.get("rawSha256"), "%s raw SHA-256" % label)
        code_sha = _hex(compile_result.get("codeSha256"), "%s code SHA-256" % label)
        if _hex(
            compile_result.get("sourceSha256"), "%s source SHA-256" % label
        ) != expected_source_sha:
            raise ShaderOrganizationError("%s compile was not bound to source bytes" % label)
        validation_value = self._validate_candidate(str(root), revision, raw_sha)
        if not isinstance(validation_value, Mapping):
            raise ShaderOrganizationError("%s ABI validation returned a non-object" % label)
        validation = dict(validation_value)
        request_id = _hex(
            validation.get("requestId"), "%s ABI request ID" % label
        )
        report_sha = _hex(
            validation.get("abiReportSha256"), "%s ABI report SHA-256" % label
        )
        if (
            validation.get("compatible") is not True
            or validation.get("replayAbiValidated") is not True
            or validation.get("requiresReplayAbiValidation") is not False
            or validation.get("revision") != revision
        ):
            raise self._abi_failure_error(service, root, label)
        after = service.status(str(root))
        latest = after.get("latestCompiled") if isinstance(after, dict) else None
        abi_gate = latest.get("abiGate") if isinstance(latest, dict) else None
        if (
            not isinstance(latest, dict)
            or Path(str(after.get("workspacePath", ""))).resolve() != root
            or after.get("revision") != revision
            or after.get("source", {}).get("currentSha256") != expected_source_sha
            or latest.get("sourceSha256") != expected_source_sha
            or latest.get("rawSha256") != raw_sha
            or latest.get("codeSha256") != code_sha
            or latest.get("abiCompatible") is not True
            or not isinstance(abi_gate, Mapping)
            or abi_gate.get("status") != "compatible"
            or abi_gate.get("requestId") != request_id
            or abi_gate.get("reportSha256") != report_sha
        ):
            raise ShaderOrganizationError("%s workspace binding changed" % label)
        return compile_result, validation

    @staticmethod
    def _abi_failure_error(service: Any, root: Path, label: str) -> ShaderOrganizationError:
        """Name the failed ABI facts in the error instead of only the verdict."""

        message = "%s did not pass fresh replay ABI validation" % label
        explanation = None
        try:
            status = service.status(str(root))
            attempt = status.get("lastAbiAttempt") if isinstance(status, Mapping) else None
            explanation = attempt.get("explanation") if isinstance(attempt, Mapping) else None
        except Exception:
            explanation = None
        if isinstance(explanation, Mapping) and explanation.get("findings"):
            findings = list(explanation.get("findings") or [])
            message += ": %s; e.g. %s" % (
                explanation.get("summary"),
                str(findings[0].get("message", ""))[:300],
            )
        error = ShaderOrganizationError(message)
        error.abi_explanation = explanation  # type: ignore[attr-defined]
        return error

    def _workspace_source(
        self, status: Mapping[str, Any]
    ) -> tuple[Path, Path, bytes, str]:
        if status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ShaderOrganizationError("workspace status schema is invalid")
        source = status.get("source")
        representation = status.get("representation")
        if (
            not isinstance(source, Mapping)
            or not isinstance(representation, Mapping)
            or source.get("present") is not True
            or source.get("format") != "hlsl"
            or source.get("lane") != "executable-editable"
            or source.get("compileEligible") is not True
            or representation.get("lane") != "executable-editable"
            or representation.get("compileEligible") is not True
        ):
            raise ShaderOrganizationError(
                "AI organization requires an executable-editable HLSL workspace"
            )
        root = Path(str(status.get("workspacePath", ""))).resolve()
        source_path = Path(str(source.get("path", ""))).resolve()
        if (
            not root.is_dir()
            or root.is_symlink()
            or not is_within(root, source_path)
            or source_path.is_symlink()
            or source_path.name != "shader.hlsl"
        ):
            raise ShaderOrganizationError("workspace source path is invalid or redirected")
        source_bytes = _regular_bytes(source_path, MAX_SOURCE_BYTES, "workspace HLSL")
        source_sha = _sha256(source_bytes)
        if _hex(source.get("currentSha256"), "current source SHA-256") != source_sha:
            raise ShaderOrganizationError("workspace source hash changed")
        source_bytes.decode("utf-8", errors="strict")
        return root, source_path, source_bytes, source_sha

    def _validate_session(
        self,
        session: Mapping[str, Any],
        root: Path,
        organization_id: str,
        status: Mapping[str, Any],
    ) -> None:
        supplied_hmac = str(session.get("sessionIntegrityHmacSha256", "") or "")
        process_sealed = (_SHA256.fullmatch(supplied_hmac) is not None and
                          hmac.compare_digest(supplied_hmac, self._session_hmac(session)))
        if not process_sealed and not self._completed_reviews.contains(session):
            raise ShaderOrganizationError(
                "organization session integrity seal is invalid"
            )
        if (
            session.get("schema") != ORGANIZATION_SESSION_SCHEMA
            or session.get("schemaVersion") != 1
            or session.get("policyVersion") != ORGANIZATION_POLICY_VERSION
            or session.get("organizationId") != organization_id
            or Path(str(session.get("workspacePath", ""))).resolve() != root
            or session.get("workspaceId") != status.get("workspaceId")
            or session.get("captureId") != status.get("capture", {}).get("captureId")
            or session.get("stage") != status.get("stage")
            or session.get("entryPoint") != status.get("entryPoint")
            or session.get("baselineCompiledAndAbiValidated") is not True
            or session.get("semanticEquivalenceVerified") is not False
            or session.get("replayApplied") is not False
        ):
            raise ShaderOrganizationError("organization session identity is invalid")

    def _session_artifact(
        self,
        root: Path,
        session: Mapping[str, Any],
        key: str,
        session_dir: Path,
        label: str,
    ) -> Path:
        value = session.get(key)
        if not isinstance(value, str) or not value or "\\" in value:
            raise ShaderOrganizationError("%s path is invalid" % label)
        path = (root / value).resolve()
        if not is_within(session_dir, path) or path.parent != session_dir:
            raise ShaderOrganizationError("%s path escapes its session" % label)
        return path

    def _flow_spec_template(self, root: Path, session: Mapping[str, Any]) -> dict[str, Any]:
        try:
            source = (root / str(session["sourcePath"])).read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        try:
            functions = [record["name"] for record in _function_records_for_template(source)]
        except Exception:
            functions = []
        return flow_spec_template(source, functions, str(session.get("entryPoint") or ""))

    @staticmethod
    def _write_alias_view(root: Path, baseline_text: str, renames: Mapping[str, str], comments: Mapping[str, str], proposal: str) -> None:
        """``ai/aliases.json`` (display name -> original) and the rendered view ``ai/organized.hlsl``.

        These are review artifacts. Acceptance promotes the actual proposal
        into the canonical editor/compile source; there is no alias-only main
        editor buffer or implicit reverse rewrite.
        """

        aliases = {
            "schema": "replaylab-shader-aliases/v1",
            "schemaVersion": 1,
            "aliases": {new: old for old, new in renames.items()},
            "comments": dict(comments),
            "baselineSourceSha256": _sha256(baseline_text.encode("utf-8")),
            "viewPath": "ai/organized.hlsl",
        }
        atomic_write_json(root / "ai" / "aliases.json", aliases)
        atomic_write_bytes(root / "ai" / "organized.hlsl", proposal.encode("utf-8"))

    def _begin_result(
        self,
        root: Path,
        session_dir: Path,
        session: Mapping[str, Any],
        semantic_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema": "renderdoc-ai-shader-organization-begin/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "organizationId": session["organizationId"],
            "sourcePath": str(root / str(session["sourcePath"])),
            "proposalSourcePath": str(root / str(session["proposalSourcePath"])),
            "analysisPath": str(root / str(session["analysisPath"])),
            "flowchartPath": str(root / str(session["flowchartPath"])),
            "moduleDescriptionsPath": str(
                root / str(session["moduleDescriptionsPath"])
            ),
            "structuredFlowGraphPath": str(
                root / str(session["structuredFlowGraphPath"])
            ),
            "semanticContextPath": str(root / str(session["semanticContextPath"])),
            "flowSpecPath": str(session_dir / "flow-spec.json"),
            "flowSpecTemplate": self._flow_spec_template(root, session),
            "analysisTemplate": analysis_template(),
            "organizationContract": organization_contract(
                (root / str(session["sourcePath"])).read_text(encoding="utf-8")),
            "moduleDescriptionsTemplate": module_description_template(
                (root / str(session["sourcePath"])).read_text(encoding="utf-8")),
            "templateContract": {
                "schema": "replaylab-shader-templates/v1", "rendering": "host-fixed",
                "modelFieldsOnly": True, "customMermaidAccepted": False,
                "reviewVerdictSchema": "replaylab-review-verdict/v1",
                "maxRounds": ReviewRounds(root).limit,
                "blockersMaySoftAccept": False,
                "softAcceptancePublishes": "validated-baseline-and-host-summary",
                "submitTool": "write_shader_workspace_organization_file",
            },
            "aliasesPath": str(root / "ai" / "aliases.json"),
            "sessionPath": str(session_dir / "session.json"),
            "baselineSourceSha256": session["baselineSourceSha256"],
            "baselineCodeSha256": session["baselineCodeSha256"],
            "semanticContext": dict(semantic_context),
            "nextTool": "review_shader_workspace_organization",
            "applied": False,
            "policy": {
                "aiEditsProposalOnly": True,
                "rawDecompilerSourcePreserved": True,
                "baselineCompileAndAbiRequired": True,
                "organizedCompileAndAbiRequired": True,
                "exactCodeSha256RequiredForPromotion": False,
                "acceptedEquivalenceLevels": (
                    ["exact-code", "rename-only", "observational-pass"]
                    if self._compare_candidates is not None
                    else ["exact-code", "rename-only"]
                ),
                "declaredRenameOnlyProofRequiredWhenCodeChanges": self._compare_candidates is None,
                "outputComparisonOnlyForCodeChanges": True,
                "reuseCurrentCompileAndAbiEvidence": True,
                "reviewBatchGuidance": "one complete function or meaningful computation stage; combine small changes",
                "postPublicationUserEditsRequireEquivalence": False,
                "callGraphDerivedFromFinalSource": True,
                "aiMayEditModuleDescriptionsOnly": True,
                "abiEqualityAloneIsNotSemanticProof": True,
                "semanticEquivalenceToCapturedOriginalVerified": False,
                "replayMutation": self._compare_candidates is not None,
                "comparisonReplayRestored": True,
            },
        }


__all__ = [
    "ORGANIZATION_GATE_SCHEMA",
    "ORGANIZATION_POLICY_VERSION",
    "ORGANIZATION_SESSION_SCHEMA",
    "ShaderOrganizationError",
    "ShaderOrganizationManager",
]
