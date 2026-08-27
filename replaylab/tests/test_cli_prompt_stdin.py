from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from replaylab_agent.profiles import Profile, ProfileStore


@pytest.mark.asyncio
async def test_run_reads_prompt_from_stdin_without_command_line_value(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))
    observed: dict[str, str] = {}

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, prompt, **kwargs):
            observed["prompt"] = prompt
            return SimpleNamespace(
                task_id="task",
                profile="local",
                executor="RenderDoc Agent",
                text="ok",
                plan=None,
            )

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    prompt = "解释当前中文着色器"
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(prompt.encode("utf-8"))),
    )
    args = SimpleNamespace(
        mcp_host=None,
        context_file=None,
        live_context=False,
        instance_id="",
        prompt=None,
        prompt_stdin=True,
        profile="local",
        skill="",
        task_id="task",
        json=True,
    )
    assert await cli._run_once(args, store) == 0
    assert observed["prompt"] == prompt
