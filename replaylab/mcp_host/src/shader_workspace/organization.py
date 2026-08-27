"""Transactional, code-hash-gated organization of reconstructed HLSL.

The model is allowed to edit only a proposal copy.  Fixed code compiles and
ABI-validates the untouched source first, then the proposal, and promotes the
proposal only when both compiler runs produce the same executable-code hash.
This proves that the readability-only rewrite did not change the compiler's
executable program relative to the reconstructed baseline.  It deliberately
does not claim that the reconstructed baseline is the game's original source
or semantically identical to the captured shader for every possible input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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

from .io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .leases import (
    SOURCE_TRANSACTION_BACKUP_FILE,
    SOURCE_TRANSACTION_JOURNAL_FILE,
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)


ORGANIZATION_SESSION_SCHEMA = "renderdoc-ai-shader-organization-session/v1"
ORGANIZATION_GATE_SCHEMA = "renderdoc-ai-shader-organization-gate/v1"
ORGANIZATION_POLICY_VERSION = "renderdoc-ai-organize-workspace/v1"

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


class ShaderOrganizationManager:
    """Own bounded proposal files and the compile/ABI/code identity gate."""

    def __init__(
        self,
        service_factory: Callable[[], Any],
        validate_candidate: Callable[[str, int, str], Mapping[str, Any]],
    ) -> None:
        if not callable(service_factory) or not callable(validate_candidate):
            raise TypeError("organization callbacks must be callable")
        self._service_factory = service_factory
        self._validate_candidate = validate_candidate
        self._guard = threading.RLock()
        # Session files live below an AI-editable tree. Seal every fixed field
        # with a process-owned key so proposal edits cannot rewrite baseline
        # code/hash/identity authority. A server restart intentionally makes an
        # unfinished session fail closed instead of trusting disk-only claims.
        self._session_hmac_key = secrets.token_bytes(32)

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
        if _json_size(context) > MAX_CONTEXT_BYTES:
            raise ShaderOrganizationError("semantic context exceeds its fixed size limit")
        _validate_source_directives(
            source_bytes.decode("utf-8", errors="strict"),
            "organization baseline HLSL",
        )

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
            context_path = session_dir / "semantic-context.json"
            atomic_write_bytes(baseline_path, source_bytes)
            atomic_write_bytes(proposal_path, source_bytes)
            atomic_write_bytes(
                analysis_path,
                (
                    "# Shader algorithm analysis\n\n"
                    "Replace this template with evidence-bound analysis. Mark inferred "
                    "resource or parameter meanings as hypotheses.\n"
                ).encode("utf-8"),
            )
            atomic_write_bytes(
                flowchart_path,
                b"flowchart TD\n  A[Captured shader] --> B[Reconstructed HLSL]\n  B --> C[Readable proposal]\n",
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

    def review(self, workspace_path: str, organization_id: str) -> dict[str, Any]:
        """Compile a fixed proposal and promote it only on exact code identity."""

        with self._guard:
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = service.status(workspace_path)
            root, source_path, current_bytes, current_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            session_path = session_dir / "session.json"
            session = read_json_object(
                session_path, MAX_CONTEXT_BYTES, "organization session"
            )
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
            if "```" in analysis:
                raise ShaderOrganizationError("organization analysis must not embed code fences")
            _validate_safe_mermaid(flowchart)

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

                # Disk session fields are HMAC-sealed, but the fixed compiler is
                # still the authority. Recompile and freshly ABI-validate the
                # untouched baseline immediately before the proposal.
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
                # Human-readable analysis remains useful even when the HLSL
                # proposal is retained only as provisional explanatory text.
                atomic_write_bytes(root / "ai" / "analysis.md", analysis_bytes)
                atomic_write_bytes(root / "ai" / "flowchart.mmd", flowchart_bytes)
                if code_equal:
                    decision = "accepted-executable-editable"
                    promoted = True
                    atomic_write_bytes(session_dir / "organized.hlsl", proposal_bytes)
                    atomic_write_bytes(root / "ai" / "organized.hlsl", proposal_bytes)
                else:
                    decision = "retained-provisional-code-change"
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

                final_source_sha = proposal_sha if code_equal else baseline_sha
                final_compile = organized_compile if code_equal else recovery_compile
                final_validation = (
                    organized_validation if code_equal else recovery_validation
                )
                final_binding = self._candidate_binding(
                    final_source_sha, final_compile, final_validation
                )

                gate = {
                    "schema": ORGANIZATION_GATE_SCHEMA,
                    "schemaVersion": 1,
                    "policyVersion": ORGANIZATION_POLICY_VERSION,
                    "organizationId": organization_id,
                    "reviewedAt": _utc_now(),
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
                    "decision": decision,
                    "organizedSourcePromoted": promoted,
                    "executableEditableSourcePath": str(source_path),
                    "proposalSourcePath": str(proposal_path),
                    "baselineCompiledAndAbiValidated": True,
                    "organizedCompiledAndAbiValidated": True,
                    "rawRecoveryCompiledAndAbiValidated": not code_equal,
                    "semanticEquivalenceVerified": False,
                    "replayApplied": False,
                    "safeToStartLiveEdit": code_equal,
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
                    "note": (
                        "Code identity is proven only against the reconstructed HLSL "
                        "baseline compiled by the same fixed route; original captured "
                        "shader semantic equivalence remains unverified."
                    ),
                }
                atomic_write_json(session_dir / "organized-compile.json", organized_compile)
                atomic_write_json(session_dir / "organized-abi.json", organized_validation)
                if recovery_compile is not None:
                    atomic_write_json(session_dir / "recovery-compile.json", recovery_compile)
                    atomic_write_json(session_dir / "recovery-abi.json", recovery_validation)
                self._require_final_binding(service, root, session, final_binding)
                completed = dict(session)
                completed["state"] = "completed"
                completed["completedAt"] = gate["reviewedAt"]
                completed["decision"] = decision
                completed["proposalSourceSha256"] = proposal_sha
                completed["organizedCodeSha256"] = organized_compile["codeSha256"]
                completed["organizedSourcePromoted"] = promoted
                completed["finalBinding"] = final_binding
                completed = self._seal_session(completed)
                # Commit all supporting evidence and the final sealed session
                # before publishing either gate. The root gate is the final
                # filesystem operation on the success path, so safe=true can
                # never precede session commit.
                atomic_write_json(session_path, completed)
                atomic_write_json(session_dir / "gate.json", gate)
                self._require_final_binding(service, root, session, final_binding)
                self._publish_root_gate(root, gate)
                return {
                    "schema": "renderdoc-ai-shader-organization-review/v1",
                    "schemaVersion": 1,
                    "workspacePath": str(root),
                    "organizationId": organization_id,
                    "sourcePath": str(source_path),
                    "analysisPath": str(root / "ai" / "analysis.md"),
                    "flowchartPath": str(root / "ai" / "flowchart.mmd"),
                    "gatePath": str(root / "ai" / "organize-code-gate.json"),
                    "gate": gate,
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

    def _compile_and_validate(
        self,
        service: Any,
        root: Path,
        expected_source_sha: str,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
            raise ShaderOrganizationError("%s did not pass fresh replay ABI validation" % label)
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
        if (
            _SHA256.fullmatch(supplied_hmac) is None
            or not hmac.compare_digest(supplied_hmac, self._session_hmac(session))
        ):
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
            "semanticContextPath": str(root / str(session["semanticContextPath"])),
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
                "exactCodeSha256RequiredForPromotion": True,
                "abiEqualityAloneIsNotSemanticProof": True,
                "semanticEquivalenceToCapturedOriginalVerified": False,
                "replayMutation": False,
            },
        }


__all__ = [
    "ORGANIZATION_GATE_SCHEMA",
    "ORGANIZATION_POLICY_VERSION",
    "ORGANIZATION_SESSION_SCHEMA",
    "ShaderOrganizationError",
    "ShaderOrganizationManager",
]
