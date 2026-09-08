"""AgentHost view of the Host-owned task-report contract."""

from __future__ import annotations

import sys
from pathlib import Path


try:
    from src.task_report import *  # type: ignore[import-not-found]  # noqa: F401,F403
    from src.task_report import __all__
except ModuleNotFoundError as error:
    # Editable source checkout: ReplayLabMCP is a sibling project. Frozen
    # AgentHost already receives this import root from the packaging script.
    mcp_root = Path(__file__).resolve().parents[2] / "mcp_host"
    if not (mcp_root / "src" / "task_report.py").is_file():
        raise error
    sys.path.insert(0, str(mcp_root))
    from src.task_report import *  # type: ignore[import-not-found,no-redef]  # noqa: F401,F403,E402
    from src.task_report import __all__  # type: ignore[import-not-found,no-redef]  # noqa: E402
