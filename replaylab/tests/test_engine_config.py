from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaylab_agent import native_mcp
from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.engine_config import load_engine_config
from replaylab_agent.profiles import Profile


def _config(tmp_path: Path, values: dict):
    path = tmp_path / "agent-engine.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return load_engine_config(path)


@pytest.mark.parametrize("value,expected", [(1, 1), (37, 37), (1024, 1024), (0, 1), (2048, 1024)])
def test_task_tool_budget_is_bounded(tmp_path, value, expected) -> None:
    assert _config(tmp_path, {"max_tool_calls": value}).max_tool_calls == expected


@pytest.mark.parametrize("value", [True, 3.5, "37", None])
def test_task_tool_budget_requires_integer(tmp_path, value) -> None:
    assert _config(tmp_path, {"max_tool_calls": value}).max_tool_calls == 480


def test_removed_child_limits_do_not_change_task_budget(tmp_path) -> None:
    legacy = {"child_timeout": 1, "max_children": 1, "max_parallel": 1, "max_child_tool_calls": 1}
    config = _config(tmp_path, legacy)
    assert config.max_tool_calls == 480
    assert all(not hasattr(config, name) for name in legacy)
    assert _config(tmp_path, {**legacy, "max_tool_calls": 7}).max_tool_calls == 7


@pytest.mark.parametrize("preset", ["openai", "claude-code-acp", "codex-app-server"])
@pytest.mark.parametrize("configured_budget", [None, 7])
def test_api_and_native_routes_forward_the_same_total_budget(tmp_path, monkeypatch, preset, configured_budget) -> None:
    host = tmp_path / "ReplayLabMCP.exe"
    monkeypatch.setattr(native_mcp, "find_replaylab_mcp", lambda _path: host)
    values = {} if configured_budget is None else {"max_tool_calls": configured_budget}
    config = _config(tmp_path, values)
    context = ProviderContext(
        profile=Profile(id="budget", preset=preset, title="Budget test"),
        model="", instance_id="budget-test", context={}, intent=None,
        max_tool_calls=config.max_tool_calls,
    )
    route = native_mcp.build_native_mcp_route(
        context, workspace=tmp_path, event_root=tmp_path / "events", allowed_effects=(),
    )
    assert route is not None
    budget = route.args[route.args.index("--max-tool-calls") + 1]
    assert budget == str(480 if configured_budget is None else configured_budget)
