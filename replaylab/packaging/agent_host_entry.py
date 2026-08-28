"""PyInstaller entry point for the ReplayLab AgentHost runtime."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Sequence


def _is_regular_unlinked_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(details.st_mode)
        and not (getattr(details, "st_file_attributes", 0) & 0x400)
    )


def _configure_process() -> None:
    """Keep JSON and protocol streams independent of the Windows code page."""

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass

    # A frozen AgentHost cannot derive the installed runtime directory from a
    # source-tree __file__.  ACP specialists spawn their task-filtered proxy
    # through sys.executable, and the parent runtime resolves the upstream MCP
    # host before that spawn.  Pin both paths to this exact co-packaged bundle
    # while preserving an explicit operator/qrenderdoc override.
    if getattr(sys, "frozen", False) and not os.environ.get(
        "REPLAYLAB_MCP_HOST", ""
    ).strip():
        sibling = Path(sys.executable).resolve().parent / "ReplayLabMCP.exe"
        if _is_regular_unlinked_file(sibling):
            os.environ["REPLAYLAB_MCP_HOST"] = str(sibling)


def _dispatch(argv: Sequence[str]) -> int:
    """Support the isolated child modules spawned by AgentHost itself."""

    selected = list(argv)
    if selected[:1] == ["-I"]:
        selected.pop(0)

    if selected[:2] == ["-m", "replaylab_agent.mcp_proxy"]:
        from replaylab_agent.mcp_proxy import main

        return main(selected[2:])
    if selected[:2] == ["-m", "replaylab_agent.selftest"]:
        from replaylab_agent.selftest import _child_entry

        return _child_entry(selected[2:])
    if selected[:1] == ["-m"]:
        raise SystemExit("unsupported internal ReplayLab AgentHost module")

    from replaylab_agent.cli import main

    return main(selected)


if __name__ == "__main__":
    _configure_process()
    raise SystemExit(_dispatch(sys.argv[1:]))
