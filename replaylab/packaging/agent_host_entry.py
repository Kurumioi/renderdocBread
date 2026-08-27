"""PyInstaller entry point for the ReplayLab AgentHost runtime."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Sequence


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

    # A frozen AgentHost cannot derive the source repository from __file__.
    # Prefer its co-packaged MCP host while preserving an explicit operator
    # override supplied by qrenderdoc or the environment.
    if getattr(sys, "frozen", False) and not os.environ.get("REPLAYLAB_MCP_HOST", "").strip():
        sibling = Path(sys.executable).resolve().parent / "ReplayLabMCP.exe"
        if sibling.is_file():
            os.environ["REPLAYLAB_MCP_HOST"] = str(sibling)


def _dispatch(argv: Sequence[str]) -> int:
    """Support the two isolated child modules spawned by AgentHost itself."""

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
