"""Fail-closed cross-process leases shared by Shader source transactions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone

SOURCE_TRANSACTION_JOURNAL_FILE = ".workspace-source-transaction.json"
SOURCE_TRANSACTION_BACKUP_FILE = ".workspace-source-transaction.backup"
LEASE_SCHEMA = "replaylab-workspace-source-lease/v2"
LEASE_VERSION = 2
MAX_LEASE_BYTES = 4096
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class WorkspaceSourceLeaseError(RuntimeError):
    """The workspace source is already owned by another transaction."""


class WorkspaceSourceTransactionLease:
    """Token-owned lease for watcher and organization source transactions.

    The historical filename is retained so existing watcher crash residue keeps
    exactly the same fail-closed meaning. A missing owner process is never
    inferred from the token; only the owner holding the exact token may remove
    it.
    """

    FILE_NAME = ".workspace-watch.lock"

    def __init__(
        self,
        root: Path,
        *,
        owner_instance_id: str | None = None,
        recover_orphaned: bool = False,
    ):
        self.root = root
        self.path = root / self.FILE_NAME
        self.token = uuid.uuid4().hex
        instance = str(owner_instance_id or "").strip().lower()
        if instance and _TOKEN.fullmatch(instance) is None:
            raise WorkspaceSourceLeaseError("source lease owner instance ID is invalid")
        if recover_orphaned and not instance:
            raise WorkspaceSourceLeaseError(
                "orphan lease recovery requires a process-owned host instance ID"
            )
        self.owner_instance_id = instance
        self.recover_orphaned = bool(recover_orphaned)
        self._owned = False
        self._payload = self._lease_payload()

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        recovered = False
        while True:
            try:
                self._acquire_new()
                return
            except FileExistsError as exc:
                if (
                    recovered
                    or not self.recover_orphaned
                    or not self._archive_orphaned_host_lease()
                ):
                    raise WorkspaceSourceLeaseError(
                        "workspace already has a source transaction lease; the lease "
                        "is never assumed stale: %s" % self.path
                    ) from exc
                recovered = True

    def _lease_payload(self) -> bytes:
        if not self.owner_instance_id:
            return (self.token + "\n").encode("ascii")
        value = {
            "schema": LEASE_SCHEMA,
            "schemaVersion": LEASE_VERSION,
            "token": self.token,
            "ownerInstanceId": self.owner_instance_id,
            "ownerPid": os.getpid(),
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _acquire_new(self) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, self._payload)
            os.fsync(descriptor)
            self._owned = True
        finally:
            os.close(descriptor)

    def _archive_orphaned_host_lease(self) -> bool:
        """Archive a lease from a different singleton-host generation.

        This path is deliberately unavailable to ordinary callers and legacy
        token-only locks.  The ReplayLab singleton host supplies a random
        generation ID, so a different well-formed owner ID is evidence that
        the previous singleton generation ended.  Source transaction residue
        still blocks recovery.
        """

        if not self.owner_instance_id:
            return False
        if any(
            (self.root / name).exists() or (self.root / name).is_symlink()
            for name in (
                SOURCE_TRANSACTION_JOURNAL_FILE,
                SOURCE_TRANSACTION_BACKUP_FILE,
            )
        ):
            return False
        try:
            if self.path.is_symlink() or not self.path.is_file():
                return False
            before = self.path.stat(follow_symlinks=False)
            if before.st_size <= 0 or before.st_size > MAX_LEASE_BYTES:
                return False
            raw = self.path.read_bytes()
            after = self.path.stat(follow_symlinks=False)
            if (
                len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return False
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if not isinstance(value, dict) or set(value) != {
                "schema",
                "schemaVersion",
                "token",
                "ownerInstanceId",
                "ownerPid",
                "createdAt",
            }:
                return False
            previous_owner = str(value.get("ownerInstanceId", ""))
            previous_token = str(value.get("token", ""))
            if (
                value.get("schema") != LEASE_SCHEMA
                or value.get("schemaVersion") != LEASE_VERSION
                or _TOKEN.fullmatch(previous_owner) is None
                or _TOKEN.fullmatch(previous_token) is None
                or previous_owner == self.owner_instance_id
            ):
                return False
            watch_dir = self.root / "watch"
            if watch_dir.exists() and (watch_dir.is_symlink() or not watch_dir.is_dir()):
                return False
            watch_dir.mkdir(parents=False, exist_ok=True)
            if watch_dir.resolve() != watch_dir or self.root.resolve() not in watch_dir.parents:
                return False
            archive = watch_dir / (
                "orphaned-lease-%s-%s.json" % (previous_owner, previous_token)
            )
            if archive.exists() or archive.is_symlink():
                return False
            # Recheck the directory entry immediately before the recoverable
            # move so a replaced owner file is never archived by mistake.
            final = self.path.stat(follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
            ):
                return False
            self.path.replace(archive)
            return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False

    def release(self) -> bool:
        """Remove the lease only when the on-disk token is still exactly ours."""

        if not self._owned:
            return False
        removed = False
        try:
            # An interrupted in-place source CAS leaves a bounded backup and
            # journal. Preserve the lease as crash residue until those files
            # are explicitly inspected/recovered; never let a watcher compile
            # potentially partial source bytes.
            if any(
                (self.root / name).exists() or (self.root / name).is_symlink()
                for name in (
                    SOURCE_TRANSACTION_JOURNAL_FILE,
                    SOURCE_TRANSACTION_BACKUP_FILE,
                )
            ):
                return False
            if self.path.is_symlink():
                return False
            metadata = self.path.stat(follow_symlinks=False)
            expected = self._payload
            if metadata.st_size != len(expected):
                return False
            with self.path.open("rb") as stream:
                actual = stream.read(len(expected) + 1)
            if actual == expected:
                self.path.unlink()
                removed = True
        except OSError:
            # A missing, replaced or unreadable lease is never guessed to be
            # ours. Retaining it is safer than deleting another owner's token.
            pass
        finally:
            self._owned = False
        return removed

    def __enter__(self) -> "WorkspaceSourceTransactionLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False


__all__ = [
    "SOURCE_TRANSACTION_BACKUP_FILE",
    "SOURCE_TRANSACTION_JOURNAL_FILE",
    "WorkspaceSourceLeaseError",
    "WorkspaceSourceTransactionLease",
]
