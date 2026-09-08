"""Task-local process context and deterministic tool grants."""

from __future__ import annotations

import contextlib
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError
from .events import default_task_event_root
from .policy import EFFECTFUL_TOOLS, READ_ONLY_TOOLS, tool_effects
from .process_env import external_agent_environment, resolved_agent_argv
from .profiles import Profile
from .tool_scope import ToolCallScope


@dataclass(slots=True)
class ProviderContext:
    profile: Profile
    model: str
    instance_id: str
    context: dict[str, Any]
    intent: dict[str, Any] | None
    mcp_executable: Path | None = None
    workspace_base: Path | None = None
    log_root: Path | None = None
    main_timeout: float = 3600.0
    max_tool_calls: int = 480
    task_id: str = ""

    @property
    def protocol(self) -> str:
        return str(self.profile.provider.protocol)

    def workspace_root(self) -> Path:
        base = self.workspace_base or (
            default_task_event_root().parent / "external-agent-workspace"
        )
        base.mkdir(parents=True, exist_ok=True)
        if base.is_symlink():
            raise ConfigurationError("the external Agent workspace root cannot be a link")
        return base.resolve()

    def new_turn_workspace(self) -> tuple[Path, Path]:
        root = self.workspace_root()
        turn_root = root / f"native-{uuid.uuid4().hex}"
        turn_root.mkdir(mode=0o700)
        workspace = turn_root / (
            "cursor-workspace" if self.profile.preset == "cursor-acp" else "agent-workspace"
        )
        workspace.mkdir(mode=0o700)
        if self.profile.preset == "cursor-acp":
            from .process_env import ensure_cursor_profile_config, ensure_cursor_workspace

            ensure_cursor_profile_config(self.profile)
            ensure_cursor_workspace(workspace)
        return workspace, turn_root

    @staticmethod
    def cleanup_turn_workspace(turn_root: Path | None) -> None:
        if turn_root is None:
            return
        with contextlib.suppress(Exception):
            resolved = turn_root.resolve(strict=True)
            if (
                re.fullmatch(r"native-[0-9a-f]{32}", resolved.name) is not None
                and not resolved.is_symlink()
            ):
                shutil.rmtree(resolved)

    def agent_argv(self) -> list[str]:
        return resolved_agent_argv(self.profile)

    def agent_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return external_agent_environment(self.profile, dict(extra or {}))

    def scope(self) -> ToolCallScope:
        return ToolCallScope.from_request(self.context, self.intent)


def normalized_effects(effects: Sequence[str]) -> frozenset[str]:
    granted = {str(item).strip() for item in effects if str(item).strip()}
    if "replay" in granted:
        granted.add("workspace")
    return frozenset(granted)


def child_tool_grant(effects: Sequence[str]) -> frozenset[str]:
    """Expose read-only tools plus effectful tools inside the connection ceiling."""

    granted = normalized_effects(effects)
    allowed = set(READ_ONLY_TOOLS)
    for name in EFFECTFUL_TOOLS:
        if tool_effects(name).issubset(granted):
            allowed.add(name)
    return frozenset(allowed)


_CODEX_FORBIDDEN_ARGS = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
    }
)
_CODEX_CONFIG_OVERRIDES = (
    'model_provider="replaylab_openai_subscription"',
    'model_providers.replaylab_openai_subscription.name="ReplayLab OpenAI subscription"',
    "model_providers.replaylab_openai_subscription.requires_openai_auth=true",
    "model_providers.replaylab_openai_subscription.supports_websockets=false",
    'model_providers.replaylab_openai_subscription.wire_api="responses"',
    "model_providers.replaylab_openai_subscription.request_max_retries=2",
    "model_providers.replaylab_openai_subscription.stream_max_retries=2",
    "model_providers.replaylab_openai_subscription.stream_idle_timeout_ms=90000",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.recommended_plugins=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.in_app_chat=false",
    "features.in_app_dictation=false",
    "features.in_app_local_automation=false",
    "features.in_app_updates=false",
    "features.image_generation=false",
    "features.shell_snapshot=false",
    "features.skill_search=false",
    "features.tool_suggest=false",
    "features.workspace_dependencies=false",
    "features.unbounded_connection_retries=false",
    "apps._default.enabled=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.skill_mcp_dependency_install=false",
    "features.hooks=false",
    "features.multi_agent=false",
    "agents.enabled=false",
    "features.goals=false",
    "features.memories=false",
    "project_doc_max_bytes=0",
    "project_doc_fallback_filenames=[]",
    "tools.view_image=false",
    'web_search="disabled"',
    'history.persistence="none"',
    "allow_login_shell=false",
)


def harden_codex_argv(argv: Sequence[str]) -> list[str]:
    """Disable ambient Codex capabilities before adding the one ReplayLab MCP."""

    values = list(argv)
    if any(value in _CODEX_FORBIDDEN_ARGS for value in values[1:]):
        raise ConfigurationError(
            "Codex profile command may not override ReplayLab's security configuration"
        )
    try:
        index = values.index("app-server")
    except ValueError as exc:
        raise ConfigurationError(
            "Codex profile command must contain the app-server subcommand"
        ) from exc
    hardened = values[:index]
    for value in _CODEX_CONFIG_OVERRIDES:
        hardened.extend(("-c", value))
    hardened.extend(values[index:])
    return hardened


__all__ = [
    "ProviderContext",
    "child_tool_grant",
    "harden_codex_argv",
    "normalized_effects",
]
