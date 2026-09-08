"""Per-vendor containment for native subscription Agents.

Vendor Agents ship ambient shell, file, web, and delegation tools. ReplayLab
disables those surfaces and exposes only the task-scoped ``ReplayLab`` MCP plus
the vendor's Skill/elicitation primitives. The ACP client also rejects foreign
permission requests and terminal/file callbacks as a fail-closed boundary.

* **Claude Code** (``claude-agent-acp``): ``session/new`` ``_meta.claudeCode``
  options (``tools: []``, ``disallowedTools``, ``allowedTools``,
  ``settingSources``, ``strictMcpConfig``) plus a ``.claude/settings.json`` in
  the fresh workspace as a second layer; thinking via ``MAX_THINKING_TOKENS``.
* **Gemini CLI family** (``gemini-acp``, ``qwen-acp``): workspace
  ``settings.json`` with a built-in-tool gate, exact MCP tool approvals and an
  MCP server allow-list.
* **Cursor**: handled by ``process_env.ensure_cursor_workspace`` (permissions).
* **CodeBuddy**: handled on the preset command line (``--tools ""``).
* **Copilot**: exact ``--available-tools`` / ``--allow-tool`` launch options.
* **Kimi**: a per-turn explicit Agent file containing only the exact task MCP
  tools, selected with ``--agent-file``.
* **custom**: the client-side denial layer alone applies.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import REPLAYLAB_MCP_SERVER_NAME
from .profiles import Profile

# Claude Code's own tools (removed via ``tools: []`` and additionally denied).
CLAUDE_BUILTIN_TOOLS: tuple[str, ...] = (
    "Agent", "Task", "Bash", "BashOutput", "KillShell", "PowerShell", "Read", "Write", "Edit",
    "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "TodoWrite",
    "Skill", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "SandboxNetworkAccess",
)
CLAUDE_NATIVE_TOOLS: tuple[str, ...] = ("Skill", "AskUserQuestion")

# Gemini CLI / Qwen Code built-in tool names (``tools.exclude``).
GEMINI_BUILTIN_TOOLS: tuple[str, ...] = (
    "run_shell_command", "write_file", "replace", "edit", "read_file", "read_many_files",
    "list_directory", "glob", "grep_search", "search_file_content", "google_web_search",
    "web_search", "web_fetch", "write_todos", "save_memory", "ask_user", "invoke_agent",
    "activate_skill", "enter_plan_mode", "exit_plan_mode", "complete_task",
    "get_internal_docs", "list_mcp_resources", "read_mcp_resource", "update_topic",
    "tracker_create_task", "tracker_update_task", "tracker_get_task", "tracker_list_tasks",
    "tracker_add_dependency", "tracker_visualize", "list_background_processes",
    "read_background_output",
)

DEFAULT_THINKING_BUDGET = 8000
_CLAUDE_PRESET = "claude-code-acp"
_GEMINI_FAMILY = {"gemini-acp": ".gemini", "qwen-acp": ".qwen"}


def thinking_budget(profile: Profile) -> int:
    """``thinking_budget_tokens`` profile option; ``0`` switches thinking off."""

    try:
        value = int(profile.options.get("thinking_budget_tokens", DEFAULT_THINKING_BUDGET))
    except (TypeError, ValueError):
        return DEFAULT_THINKING_BUDGET
    return max(0, min(value, 128_000))


def claude_session_meta(setting_sources: Sequence[str] = ("project",)) -> dict[str, Any]:
    """``session/new`` ``_meta`` for ``claude-agent-acp``.

    ``tools: [\"Skill\"]`` leaves only project Skills available;
    ``AskUserQuestion`` is separately bridged by claude-agent-acp when the ACP
    client advertises form elicitation. ``allowedTools`` auto-approves
    the Host MCP server (no permission round-trip), ``settingSources`` keeps the
    user's global ``~/.claude/settings.json`` (hooks, plugins, allow rules) out
    of the session, ``strictMcpConfig`` ignores any other configured MCP server.
    """

    return {
        "claudeCode": {
            "options": {
                "tools": ["Skill"],
                "disallowedTools": [
                    item for item in CLAUDE_BUILTIN_TOOLS if item not in CLAUDE_NATIVE_TOOLS
                ],
                "allowedTools": [f"mcp__{REPLAYLAB_MCP_SERVER_NAME}"],
                "settingSources": list(setting_sources),
                "strictMcpConfig": True,
            }
        },
    }


def session_meta(profile: Profile) -> dict[str, Any] | None:
    """Vendor ``_meta`` for ``session/new`` or ``None`` when unsupported."""

    if profile.preset == _CLAUDE_PRESET:
        return claude_session_meta()
    return None


def write_claude_settings(workspace: Path) -> None:
    """Deny every built-in Claude Code tool in this workspace (project settings)."""

    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "permissions": {
            "allow": [
                "Skill",
                "AskUserQuestion",
                f"mcp__{REPLAYLAB_MCP_SERVER_NAME}",
            ],
            "deny": [
                item for item in CLAUDE_BUILTIN_TOOLS if item not in CLAUDE_NATIVE_TOOLS
            ],
            "defaultMode": "default",
        },
        "includeCoAuthoredBy": False,
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_claude_mcp_config(
    workspace: Path,
    *,
    command: str,
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> None:
    """Pin Claude Code to the one filtered stdio ReplayLab MCP route."""

    payload = {
        "mcpServers": {
            REPLAYLAB_MCP_SERVER_NAME: {
                "type": "stdio",
                "command": str(command),
                "args": [str(value) for value in args],
                "env": {str(key): str(value) for key, value in (env or {}).items()},
            }
        }
    }
    (workspace / ".mcp.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def copy_claude_skills(
    workspace: Path,
    skills: Sequence[tuple[str, Path]],
) -> None:
    """Copy admitted filesystem Skills into Claude's project Skill directory."""

    root = workspace / ".claude" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    for skill_id, skill_file in skills:
        declared = Path(skill_file)
        if declared.is_symlink():
            continue
        source = declared.resolve()
        if source.name != "SKILL.md" or not source.is_file():
            continue
        source_root = source.parent
        try:
            entries = tuple(source_root.rglob("*"))
        except OSError:
            continue
        if any(
            entry.is_symlink()
            or bool(getattr(entry.lstat(), "st_file_attributes", 0) & 0x400)
            for entry in entries
        ):
            raise ValueError(f"Skill '{skill_id}' contains a link or reparse point")
        destination = root / str(skill_id)
        shutil.copytree(source_root, destination, dirs_exist_ok=False, symlinks=False)


def _gemini_mcp_tool_name(tool_name: str) -> str:
    return f"mcp_{REPLAYLAB_MCP_SERVER_NAME}_{tool_name}"


def _kimi_mcp_tool_name(tool_name: str) -> str:
    return f"mcp__{REPLAYLAB_MCP_SERVER_NAME}__{tool_name}"


def write_gemini_settings(
    workspace: Path,
    config_dir: str,
    allowed_tools: Sequence[str] = (),
) -> None:
    """Exclude the built-in tools and trust the ReplayLab MCP server (workspace scope).

    ``mcp.allowed`` contains server names; ``tools.allowed`` contains exact
    fully-qualified tool names. Mixing those namespaces makes the server
    connect but leaves every tool behind a confirmation gate. ``tools.core``
    is empty to suppress built-ins; the explicit MCP entries remain visible on
    Gemini releases whose core allow-list currently also filters MCP tools.

    Both the nested and flat legacy keys are written for the Gemini-derived
    CLIs which still read different generations of the settings schema.
    """

    settings_dir = workspace / config_dir
    settings_dir.mkdir(parents=True, exist_ok=True)
    exclude = list(GEMINI_BUILTIN_TOOLS)
    allowed = [_gemini_mcp_tool_name(name) for name in sorted(set(allowed_tools))]
    payload = {
        "tools": {"core": [], "exclude": exclude, "allowed": allowed},
        "mcp": {"allowed": [REPLAYLAB_MCP_SERVER_NAME], "excluded": []},
        "excludeTools": exclude,
        "allowedTools": allowed,
        "telemetry": {"enabled": False, "logPrompts": False},
        "usageStatisticsEnabled": False,
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_kimi_agent(workspace: Path, allowed_tools: Sequence[str]) -> Path:
    """Create a launch-scoped Kimi Agent whose visible tools are task exact."""

    settings_dir = workspace / ".kimi-code"
    settings_dir.mkdir(parents=True, exist_ok=True)
    target = settings_dir / "replaylab-agent.md"
    tool_lines = [
        f"  - {json.dumps(_kimi_mcp_tool_name(name), ensure_ascii=True)}"
        for name in sorted(set(allowed_tools))
    ]
    tools = "tools: []" if not tool_lines else "tools:\n" + "\n".join(tool_lines)
    target.write_text(
        "---\n"
        "name: replaylab\n"
        "description: ReplayLab task-scoped RenderDoc analysis agent\n"
        + tools
        + "\nsubagents: []\n"
        "---\n\n"
        "${base_prompt}\n",
        encoding="utf-8",
    )
    return target


def prepare_workspace(
    profile: Profile,
    workspace: Path,
    allowed_tools: Sequence[str] = (),
) -> None:
    """Write the vendor's workspace-scoped configuration into a fresh workspace."""

    if profile.preset == _CLAUDE_PRESET:
        write_claude_settings(workspace)
    elif profile.preset in _GEMINI_FAMILY:
        write_gemini_settings(
            workspace,
            _GEMINI_FAMILY[profile.preset],
            allowed_tools,
        )
    elif profile.preset == "kimi-acp":
        write_kimi_agent(workspace, allowed_tools)


def runtime_argv(
    profile: Profile,
    argv: Sequence[str],
    workspace: Path,
    allowed_tools: Sequence[str] = (),
) -> list[str]:
    """Apply launch-only vendor controls which ACP ``session/new`` cannot carry."""

    result = list(argv)
    exact_tools = sorted(set(allowed_tools))
    if profile.preset == "copilot-acp":
        result.append("--disable-builtin-mcps")
        if exact_tools:
            identifiers = ",".join(
                f"{REPLAYLAB_MCP_SERVER_NAME}({name})" for name in exact_tools
            )
            result.extend(
                (
                    f"--available-tools={identifiers}",
                    f"--allow-tool={identifiers}",
                )
            )
    elif profile.preset == "gemini-acp" and exact_tools:
        result.extend(
            (
                f"--allowed-mcp-server-names={REPLAYLAB_MCP_SERVER_NAME}",
                "--allowed-tools="
                + ",".join(_gemini_mcp_tool_name(name) for name in exact_tools),
            )
        )
    elif profile.preset == "kimi-acp":
        agent_file = workspace / ".kimi-code" / "replaylab-agent.md"
        try:
            acp_index = result.index("acp")
        except ValueError:
            acp_index = len(result)
        result[acp_index:acp_index] = ["--agent-file", str(agent_file)]
    return result


def agent_env(profile: Profile) -> dict[str, str]:
    """Extra environment for the agent process (thinking on, no telemetry noise)."""

    env: dict[str, str] = {}
    if profile.preset == _CLAUDE_PRESET:
        budget = thinking_budget(profile)
        if budget > 0:
            env["MAX_THINKING_TOKENS"] = str(budget)
        env["DISABLE_AUTOUPDATER"] = "1"
        env["DISABLE_TELEMETRY"] = "1"
        # Claude Code spills MCP results above this many tokens into a file the
        # Agent then has to Read — impossible without file tools.  The Host caps
        # results at MAX_MODEL_TOOL_RESULT_CHARS (~30k tokens); keep this above it.
        env["MAX_MCP_OUTPUT_TOKENS"] = "64000"
    elif profile.preset in _GEMINI_FAMILY:
        env["NO_COLOR"] = "1"
    elif profile.preset == "kimi-acp":
        env["KIMI_DISABLE_TELEMETRY"] = "1"
    return env


def main_agent_env(profile: Profile, main_timeout: float) -> dict[str, str]:
    """Environment for the long-lived main Agent process.

    Claude Code honours ``MCP_TIMEOUT`` (server start-up) and
    ``MCP_TOOL_TIMEOUT`` (one tool call). Capture export and validation tools can
    legitimately run for many minutes, so the tool timeout follows the main-turn
    budget.
    """

    env = agent_env(profile)
    env["MCP_TIMEOUT"] = "60000"
    env["MCP_TOOL_TIMEOUT"] = str(int(max(main_timeout, 60.0) * 1000))
    return env


def describe(profile: Profile) -> str:
    """One line for the log: which vendor switches apply to this preset."""

    preset = profile.preset
    if preset == _CLAUDE_PRESET:
        return "claude: project Skills + AskUserQuestion elicitation, one strict ReplayLab MCP, thinking via MAX_THINKING_TOKENS"
    if preset in _GEMINI_FAMILY:
        return f"gemini-family: {_GEMINI_FAMILY[preset]}/settings.json built-in gate + exact ReplayLab MCP allow-list"
    if preset == "copilot-acp":
        return "copilot: exact ReplayLab MCP tools via launch-only available/allow lists"
    if preset == "kimi-acp":
        return "kimi: launch-scoped Agent file exposes exact ReplayLab MCP tools only"
    if preset == "cursor-acp":
        return "cursor: .cursor/cli.json permissions"
    if preset == "codebuddy-acp":
        return "codebuddy: --tools \"\" --allowedTools mcp__ReplayLab on the command line"
    return "no vendor switch known; client-side denial layer only"


__all__ = [
    "CLAUDE_BUILTIN_TOOLS",
    "CLAUDE_NATIVE_TOOLS",
    "GEMINI_BUILTIN_TOOLS",
    "agent_env",
    "claude_session_meta",
    "describe",
    "copy_claude_skills",
    "main_agent_env",
    "prepare_workspace",
    "runtime_argv",
    "session_meta",
    "thinking_budget",
    "write_claude_settings",
    "write_claude_mcp_config",
    "write_gemini_settings",
    "write_kimi_agent",
]
