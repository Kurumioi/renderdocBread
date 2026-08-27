from __future__ import annotations

import asyncio
import queue
from types import SimpleNamespace

import pytest

from replaylab_agent.profiles import ProfileStore


class _BlockingControlStream:
    def __init__(self) -> None:
        self._lines: queue.Queue[bytes] = queue.Queue()

    def readline(self, _limit: int) -> bytes:
        return self._lines.get(timeout=2)

    def send(self, line: bytes) -> None:
        self._lines.put(line)


@pytest.mark.asyncio
async def test_worker_survives_one_intent_failure(monkeypatch, tmp_path, capsys) -> None:
    import replaylab_agent.cli as cli

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            self.calls = 0

        async def process_one_intent(self, **kwargs):
            assert kwargs["instance_id"] == "pinned-qrenderdoc"
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider turn failed")
            return None

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0.2,
        mcp_host=None,
        profile="",
        instance_id="pinned-qrenderdoc",
    )
    assert await cli._worker(args, ProfileStore(tmp_path / "profiles.json")) == 0
    assert "provider turn failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_inflight_intent(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_one_intent(self, **kwargs):
            assert kwargs["instance_id"] == "pinned-qrenderdoc"
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0,
        mcp_host=None,
        profile="",
        instance_id="pinned-qrenderdoc",
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        cli._worker(
            args,
            ProfileStore(tmp_path / "profiles.json"),
            shutdown_event=shutdown,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    shutdown.set()

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert cancelled.is_set()


def test_worker_control_stdin_flag_is_explicit() -> None:
    import replaylab_agent.cli as cli

    args = cli._parser().parse_args(
        ["worker", "--control-stdin", "--start-gated"]
    )
    assert args.control_stdin is True
    assert args.start_gated is True


@pytest.mark.asyncio
async def test_start_gated_worker_does_not_claim_until_start_then_accepts_shutdown(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    claimed = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_one_intent(self, **kwargs):
            claimed.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    control = _BlockingControlStream()
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=control))
    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0,
        mcp_host=None,
        profile="",
        instance_id="pinned-qrenderdoc",
        control_stdin=True,
        start_gated=True,
    )

    task = asyncio.create_task(
        cli._worker_main(args, ProfileStore(tmp_path / "profiles.json"))
    )
    await asyncio.sleep(0.05)
    assert not claimed.is_set()

    control.send(b"start\n")
    await asyncio.wait_for(claimed.wait(), timeout=1)
    control.send(b"shutdown\n")

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_input", [b"", b"unknown\n", b"x" * 4097])
async def test_start_gated_worker_fails_closed_before_claim(
    monkeypatch, tmp_path, terminal_input
) -> None:
    import replaylab_agent.cli as cli

    constructed = 0

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal constructed
            constructed += 1

        async def process_one_intent(self, **kwargs):
            raise AssertionError("a gated worker must not claim before start")

    control = _BlockingControlStream()
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=control))
    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0,
        mcp_host=None,
        profile="",
        instance_id="pinned-qrenderdoc",
        control_stdin=True,
        start_gated=True,
    )

    task = asyncio.create_task(
        cli._worker_main(args, ProfileStore(tmp_path / "profiles.json"))
    )
    control.send(terminal_input)

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert constructed == 0


@pytest.mark.asyncio
async def test_non_gated_control_reader_continues_until_shutdown(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    claimed = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_one_intent(self, **kwargs):
            claimed.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    control = _BlockingControlStream()
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=control))
    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0,
        mcp_host=None,
        profile="",
        instance_id="pinned-qrenderdoc",
        control_stdin=True,
        start_gated=False,
    )

    task = asyncio.create_task(
        cli._worker_main(args, ProfileStore(tmp_path / "profiles.json"))
    )
    await asyncio.wait_for(claimed.wait(), timeout=1)
    control.send(b"start\n")
    await asyncio.sleep(0.05)
    assert not task.done()
    control.send(b"shutdown\n")

    assert await asyncio.wait_for(task, timeout=1) == 0
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_start_gated_requires_control_stdin(tmp_path) -> None:
    import replaylab_agent.cli as cli

    args = SimpleNamespace(
        poll_seconds=0.1,
        idle_exit_seconds=0,
        mcp_host=None,
        profile="",
        instance_id="",
        control_stdin=False,
        start_gated=True,
    )
    with pytest.raises(ValueError, match="requires --control-stdin"):
        await cli._worker_main(args, ProfileStore(tmp_path / "profiles.json"))
