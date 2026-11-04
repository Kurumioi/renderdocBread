"""Vendor-native workspace, MCP, and session hardening."""

from __future__ import annotations

import json
from pathlib import Path

from replaylab_agent.native_vendor import (
    CLAUDE_BUILTIN_TOOLS,
    agent_env,
    copy_claude_skills,
    main_agent_env,
    prepare_workspace,
    runtime_argv,
    session_meta,
    write_claude_mcp_config,
)
from replaylab_agent.profiles import Profile


def test_claude_session_meta_keeps_only_native_skill_and_replaylab_mcp() -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True},
    )

    options = session_meta(profile)["claudeCode"]["options"]

    assert options["tools"] == ["Skill"]
    assert options["allowedTools"] == ["mcp__Bread"]
    assert options["strictMcpConfig"] is True
    assert options["settingSources"] == ["project"]
    assert "Bash" in options["disallowedTools"]
    assert "Agent" in options["disallowedTools"]
    assert "AskUserQuestion" not in options["disallowedTools"]


def test_claude_workspace_has_one_strict_mcp_and_copied_skills(tmp_path: Path) -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True},
    )
    source = tmp_path / "source" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo.\n---\nUse evidence.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    prepare_workspace(profile, workspace)
    write_claude_mcp_config(
        workspace,
        command="replaylab-ai",
        args=("-I", "-m", "replaylab_agent.mcp_proxy"),
        env={"REPLAYLAB_TASK_ID": "task-1"},
    )
    copy_claude_skills(workspace, [("demo", source / "SKILL.md")])

    settings = json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))
    mcp = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["Bread"]
    assert set(settings["permissions"]["deny"]) == set(CLAUDE_BUILTIN_TOOLS) - {
        "Skill",
        "AskUserQuestion",
    }
    assert mcp["type"] == "stdio"
    assert mcp["args"] == ["-I", "-m", "replaylab_agent.mcp_proxy"]
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").is_file()


def test_gemini_and_qwen_workspaces_exclude_ambient_tools(tmp_path: Path) -> None:
    for preset, directory in (("gemini-acp", ".gemini"), ("qwen-acp", ".qwen")):
        workspace = tmp_path / preset
        workspace.mkdir()
        prepare_workspace(
            Profile(preset, preset, options={"allow_unsandboxed_agent": True}),
            workspace,
            ("inspect_event", "report_task_result"),
        )
        payload = json.loads((workspace / directory / "settings.json").read_text(encoding="utf-8"))
        assert payload["tools"]["core"] == []
        assert payload["tools"]["allowed"] == [
            "mcp_Bread_inspect_event",
            "mcp_Bread_report_task_result",
        ]
        assert payload["mcp"] == {"allowed": ["Bread"], "excluded": []}
        assert "run_shell_command" in payload["tools"]["exclude"]
        assert "invoke_agent" in payload["tools"]["exclude"]


def test_vendor_runtime_argv_applies_launch_scoped_tool_filters(tmp_path: Path) -> None:
    copilot = Profile(
        "copilot", "copilot-acp", options={"allow_unsandboxed_agent": True}
    )
    assert runtime_argv(
        copilot,
        ["copilot", "--acp"],
        tmp_path,
        ("report_task_result", "inspect_event"),
    ) == [
        "copilot",
        "--acp",
        "--disable-builtin-mcps",
        "--available-tools=Bread(inspect_event),Bread(report_task_result)",
        "--allow-tool=Bread(inspect_event),Bread(report_task_result)",
    ]

    gemini = Profile(
        "gemini", "gemini-acp", options={"allow_unsandboxed_agent": True}
    )
    assert runtime_argv(
        gemini,
        ["gemini", "--acp"],
        tmp_path,
        ("inspect_event",),
    )[-2:] == [
        "--allowed-mcp-server-names=Bread",
        "--allowed-tools=mcp_Bread_inspect_event",
    ]


def test_kimi_uses_a_per_turn_exact_tool_agent(tmp_path: Path) -> None:
    profile = Profile(
        "kimi", "kimi-acp", options={"allow_unsandboxed_agent": True}
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepare_workspace(profile, workspace, ("inspect_event", "report_task_result"))

    agent = workspace / ".kimi-code" / "replaylab-agent.md"
    text = agent.read_text(encoding="utf-8")
    assert "description: Bread AI task-scoped RenderDoc analysis agent" in text
    assert "description: ReplayLab task-scoped" not in text
    assert 'mcp__Bread__inspect_event' in text
    assert 'mcp__Bread__report_task_result' in text
    assert "subagents: []" in text
    assert "Read" not in text
    assert runtime_argv(profile, ["kimi", "acp"], workspace) == [
        "kimi",
        "--agent-file",
        str(agent),
        "acp",
    ]


def test_claude_environment_bounds_thinking_and_mcp_timeout() -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True, "thinking_budget_tokens": 12345},
    )

    environment = main_agent_env(profile, 90.0)

    assert environment["MAX_THINKING_TOKENS"] == "12345"
    assert environment["MAX_MCP_OUTPUT_TOKENS"] == "64000"
    assert environment["MCP_TIMEOUT"] == "60000"
    assert environment["MCP_TOOL_TIMEOUT"] == "90000"
    assert agent_env(Profile("codex", "codex")) == {}


def test_vendors_without_session_meta_get_none() -> None:
    for preset in ("codex", "cursor-acp", "gemini-acp", "qwen-acp"):
        assert session_meta(Profile(preset, preset)) is None
