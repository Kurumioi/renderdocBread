"""Host-owned receipts for completed shader reviews, independent of a process.

The model may edit workspace proposal files, but no tool exposes this store.
Only a completed Host review inserts a receipt. Its digest identifies the
exact accepted record; it is not a claim that different shaders are equivalent.
Current source, graph, candidate and replay ABI are still checked by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _default_path() -> Path:
    configured = os.environ.get("REPLAYLAB_CONFIG_DIR", "").strip()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    root = (Path(configured).expanduser() if configured else
            Path(local) / "ReplayLab" / "config" if local else
            Path.home() / ".config" / "ReplayLab")
    return root / "shader-acceptance" / "completed-reviews.sqlite3"


class CompletedShaderReviews:
    """Remember accepted snapshots outside every AI-editable workspace tree."""

    def __init__(self, path: Path | None = None) -> None:
        # Configuration belongs to the process owner, never an MCP argument.
        self.path = Path(path) if path is not None else _default_path()

    @staticmethod
    def _identity(session: Mapping[str, Any]) -> tuple[str, str, str]:
        raw = json.dumps(dict(session), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return (hashlib.sha256(raw).hexdigest(),
                str(Path(str(session["workspacePath"])).resolve()),
                str(session["organizationId"]))

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        path = self.path.absolute()
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("shader acceptance store must not use redirected paths")
        if not create and not path.is_file():
            return None
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=ro"),
            uri=True, timeout=5.0,
        )
        if create:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS accepted_reviews ("
                "digest TEXT PRIMARY KEY, workspace TEXT NOT NULL, "
                "organization TEXT NOT NULL, decision TEXT NOT NULL, "
                "accepted_at TEXT NOT NULL)"
            )
        return connection

    def remember(self, session: Mapping[str, Any]) -> None:
        if (session.get("state") != "completed" or session.get("decision") not in
                ("accepted-executable-editable", "soft-accepted-baseline")):
            raise ValueError("only completed accepted shader reviews can be retained")
        digest, workspace, organization = self._identity(session)
        connection = self._connect(create=True)
        assert connection is not None
        try:
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO accepted_reviews VALUES (?, ?, ?, ?, ?)",
                    (digest, workspace, organization, session["decision"],
                     str(session["completedAt"])),
                )
        finally:
            connection.close()

    def contains(self, session: Mapping[str, Any]) -> bool:
        if (session.get("state") != "completed" or session.get("decision") not in
                ("accepted-executable-editable", "soft-accepted-baseline")):
            return False
        try:
            digest, workspace, organization = self._identity(session)
            connection = self._connect(create=False)
            if connection is None:
                return False
            try:
                return connection.execute(
                    "SELECT 1 FROM accepted_reviews WHERE digest=? AND workspace=? "
                    "AND organization=? AND decision=?",
                    (digest, workspace, organization, session["decision"]),
                ).fetchone() is not None
            finally:
                connection.close()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            # Missing/corrupt local provenance never converts disk claims into
            # accepted work. It also never creates a fresh store during a read.
            return False
