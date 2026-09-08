from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.agent_contracts import RunResult
from replaylab_agent.profiles import Profile, ProfileStore


def _store(tmp_path: Path) -> ProfileStore:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))
    return store


def _args(**overrides):
    values = {
        "mcp_host": None,
        "context_file": None,
        "live_context": False,
        "instance_id": "",
        "prompt": "wait",
        "prompt_stdin": False,
        "profile": "local",
        "model": "",
        "skill": "",
        "intent_kind": "",
        "task_id": "task",
        "cancel_file": None,
        "engine_config": None,
        "json": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result() -> RunResult:
    return RunResult(
        task_id="task",
        profile_id="local",
        executor="Ollama",
        text="ok",
        terminal_status="completed",
    )


def _install_engine(monkeypatch, implementation):
    import replaylab_agent.cli as cli

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def run(self, request):
            return await implementation(request)

    monkeypatch.setattr(cli, "DeepAgentsEngine", FakeEngine)


@pytest.mark.asyncio
async def test_run_cancel_file_cancels_engine_before_native_force_kill(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(_request):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _install_engine(monkeypatch, run)
    cancel_file = tmp_path / "cancel.signal"
    cancel_file.write_bytes(b"")
    operation = asyncio.create_task(
        cli._run_once(_args(cancel_file=cancel_file), _store(tmp_path))
    )
    await started.wait()
    cancel_file.write_text("cancel\n", encoding="utf-8")

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cancelled.is_set()
    assert not cancel_file.exists()


@pytest.mark.asyncio
async def test_missing_cancel_file_does_not_cancel_new_engine(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(_request):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _install_engine(monkeypatch, run)
    cancel_file = tmp_path / "not-visible-yet.signal"
    operation = asyncio.create_task(
        cli._run_once(_args(cancel_file=cancel_file), _store(tmp_path))
    )
    await started.wait()
    await asyncio.sleep(0.15)
    assert not operation.done()
    assert not cancelled.is_set()

    cancel_file.write_text("cancel\n", encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cancelled.is_set()
    assert not cancel_file.exists()


@pytest.mark.asyncio
async def test_cancel_watcher_ignores_transient_error_and_oversized_payload(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    cancel_file = tmp_path / "cancel.signal"
    cancel_file.write_bytes(b"")
    real_open = Path.open
    failed_once = False

    def flaky_open(path, *args, **kwargs):
        nonlocal failed_once
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == cancel_file and mode == "rb" and not failed_once:
            failed_once = True
            raise PermissionError("temporary sharing violation")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    waiter = asyncio.create_task(cli._wait_for_run_cancellation(cancel_file))
    await asyncio.sleep(0.15)
    assert failed_once
    assert not waiter.done()

    with real_open(cancel_file, "wb") as signal:
        signal.write(b"not-a-cancel-token" * 8)
    await asyncio.sleep(0.15)
    assert not waiter.done()

    with real_open(cancel_file, "wb") as signal:
        signal.write(b"cancel\n")
    await asyncio.wait_for(waiter, timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_watcher_failure_does_not_cancel_engine(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    completed = asyncio.Event()

    async def run(_request):
        await asyncio.sleep(0.05)
        completed.set()
        return _result()

    async def broken_cancel_watcher(_path):
        raise RuntimeError("watcher bug")

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(cli, "_wait_for_run_cancellation", broken_cancel_watcher)
    assert (
        await cli._run_once(
            _args(cancel_file=tmp_path / "cancel.signal"), _store(tmp_path)
        )
        == 0
    )
    assert completed.is_set()


@pytest.mark.asyncio
async def test_run_reads_prompt_from_stdin(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    prompt = "解释当前中文着色器"
    monkeypatch.setattr(
        cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(prompt.encode("utf-8")))
    )
    assert (
        await cli._run_once(
            _args(prompt=None, prompt_stdin=True), _store(tmp_path)
        )
        == 0
    )
    assert observed["request"].prompt == prompt


@pytest.mark.asyncio
async def test_direct_action_becomes_scoped_intent(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    args = _args(
        prompt="inspect",
        skill="renderdoc-explain-event",
        intent_kind="inspect-picked-pixel",
    )
    assert await cli._run_once(args, _store(tmp_path)) == 0
    request = observed["request"]
    assert request.intent == {"kind": "inspect-picked-pixel", "arguments": {}}
    assert request.skill_name == "renderdoc-explain-event"


@pytest.mark.asyncio
async def test_explain_event_intent_keeps_agent_skill_choice_adaptive(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    assert (
        await cli._run_once(
            _args(prompt="explain", intent_kind="explain-current-event"),
            _store(tmp_path),
        )
        == 0
    )
    request = observed["request"]
    assert request.skill_name == ""
    assert request.intent == {"kind": "explain-current-event", "arguments": {}}
