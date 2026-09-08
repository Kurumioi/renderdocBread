"""Resolution helpers for the co-packaged ReplayLab MCP Host."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .errors import ConfigurationError


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def find_replaylab_mcp(explicit: Path | str | None = None) -> Path:
    """Resolve the existing MCP Host without starting it."""

    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    configured = os.environ.get("REPLAYLAB_MCP_HOST", "").strip()
    if configured:
        candidates.append(configured)
    development_host = repository_root() / "x64" / "Development" / "replaylab"
    candidates.extend(
        [
            str(development_host / "ReplayLabMCP.unicode.exe"),
            str(development_host / "ReplayLabMCP.exe"),
        ]
    )
    on_path = (
        shutil.which("ReplayLabMCP.unicode.exe")
        or shutil.which("ReplayLabMCP.exe")
        or shutil.which("replaylab-mcp")
    )
    if on_path:
        candidates.append(on_path)
    checked: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        checked.append(str(path))
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    raise ConfigurationError(
        "ReplayLabMCP host was not found; set REPLAYLAB_MCP_HOST. Checked: "
        + ", ".join(checked)
    )


__all__ = ["find_replaylab_mcp", "repository_root"]
