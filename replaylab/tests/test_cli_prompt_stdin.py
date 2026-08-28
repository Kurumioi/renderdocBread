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
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, prompt, **kwargs):
            observed["prompt"] = prompt
            observed["kwargs"] = kwargs
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


@pytest.mark.asyncio
async def test_direct_native_action_is_forwarded_as_a_scoped_internal_intent(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, prompt, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                task_id="task",
                profile="local",
                executor="RenderDoc Agent",
                text="ok",
                plan=None,
            )

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    args = SimpleNamespace(
        mcp_host=None,
        context_file=None,
        live_context=False,
        instance_id="",
        prompt="inspect",
        prompt_stdin=False,
        profile="local",
        skill="renderdoc-explain-event",
        intent_kind="inspect-picked-pixel",
        task_id="task",
        json=True,
    )

    assert await cli._run_once(args, store) == 0
    assert observed["intent"] == {
        "kind": "inspect-picked-pixel",
        "arguments": {},
    }
