"""Background unpacked-asset indexing that outlives one AI task.

The catalog scan is triggered on first need (never up front).  A task-scoped
MCP process dies with its task, so the job itself must live in the process
qrenderdoc keeps alive — ``ReplayLabInternal`` — and the scan must leave
checkpoints (``AssetCatalogService.scan(checkpoint_seconds=...)``) so even a
killed job resumes instead of restarting.

This module is the process-local job table used by *whichever* process runs
the scan: the internal host through its ``/replaylab/asset-index`` route, or
the MCP process as a fallback when qrenderdoc's internal service is not
reachable (source runs, tests).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import threading
import time
from typing import Any

from .asset_catalog import AssetCatalogService

ASSET_INDEX_OPERATION_SCHEMA = "replaylab-internal-asset-index/v1"
CHECKPOINT_SECONDS = 90.0

_JOBS: dict[str, dict[str, Any]] = {}
_GUARD = threading.Lock()


def _public(job: Mapping[str, Any]) -> dict[str, Any]:
    started = float(job.get("startedAt") or 0.0)
    finished = job.get("finishedAt")
    return {
        "status": job.get("status"),
        "depth": job.get("depth"),
        "owner": job.get("owner"),
        "elapsedSeconds": round((float(finished) if finished else time.monotonic()) - started, 1) if started else 0.0,
        "progress": dict(job.get("progress") or {}),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def ensure_index_job(
    database_path: Path,
    asset_root: Path,
    build_id: str,
    *,
    depth: str = "headers",
    owner: str = "",
) -> dict[str, Any]:
    """Start (or join) the background scan for this catalog and return its state."""

    key = str(database_path)
    with _GUARD:
        job = _JOBS.get(key)
        if job is not None and job.get("status") == "running":
            return _public(job)
        job = {"status": "running", "startedAt": time.monotonic(), "progress": {}, "result": None,
               "error": None, "depth": depth, "owner": owner}
        _JOBS[key] = job

        def _progress(update: Mapping[str, Any]) -> None:
            job["progress"] = dict(update)

        def _run() -> None:
            try:
                job["result"] = AssetCatalogService(database_path).scan(
                    asset_root, build_id=build_id, scan_mode="incremental", depth=depth,
                    progress=_progress, checkpoint_seconds=CHECKPOINT_SECONDS)
                job["status"] = "done"
            except Exception as error:  # surfaced through status()
                job["error"] = str(error)[:400]
                job["status"] = "failed"
            job["finishedAt"] = time.monotonic()

        threading.Thread(target=_run, name="replaylab-asset-index", daemon=True).start()
        return _public(job)


def index_job_status(database_path: Path) -> dict[str, Any] | None:
    with _GUARD:
        job = _JOBS.get(str(database_path))
        return _public(job) if job is not None else None


def wait_for_index_job(database_path: Path, budget_seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, budget_seconds)
    while True:
        state = index_job_status(database_path)
        if state is None or state["status"] != "running" or time.monotonic() >= deadline:
            return state
        time.sleep(0.25)


__all__ = [
    "ASSET_INDEX_OPERATION_SCHEMA",
    "CHECKPOINT_SECONDS",
    "ensure_index_job",
    "index_job_status",
    "wait_for_index_job",
]
