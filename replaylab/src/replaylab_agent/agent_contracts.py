"""Small run contracts shared by the API and vendor-native engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_protocol import (
    CHILD_EFFECTS,
    ChildResult,
    ClarificationRequest,
)
from .profiles import Profile


@dataclass(slots=True)
class OrchestratorConfig:
    """Host-owned limits; no planning or review state machine lives here."""

    main_timeout: float = 3600.0
    allowed_effects: frozenset[str] = frozenset(CHILD_EFFECTS)
    event_root: Path | None = None
    log_root: Path | None = None
    workspace_base: Path | None = None
    max_tool_calls: int = 480
    heartbeat_seconds: float = 10.0


@dataclass(slots=True)
class RunRequest:
    prompt: str
    profile: Profile
    task_id: str
    model: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    instance_id: str = ""
    capture_path: str = ""
    intent: dict[str, Any] | None = None
    skill_name: str = ""
    mcp_executable: Path | None = None


@dataclass(slots=True)
class RunResult:
    task_id: str
    profile_id: str
    executor: str
    text: str
    terminal_status: str
    clarification: ClarificationRequest | None = None
    children: list[ChildResult] = field(default_factory=list)
    error: str = ""
    report: dict[str, Any] | None = None
    core: str = "deepagents/v1"

    def payload(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "profile": self.profile_id,
            "executor": self.executor,
            "engine": self.core,
            "text": self.text,
            "terminalStatus": self.terminal_status,
            "clarification": (
                self.clarification.event_payload() if self.clarification else None
            ),
            "report": self.report,
            "children": [child.model_dump() for child in self.children],
            "error": self.error,
        }


__all__ = ["OrchestratorConfig", "RunRequest", "RunResult"]
