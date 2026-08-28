from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from replaylab_agent.errors import ConnectorError, LifecycleCleanupError
from replaylab_agent.context_management import PreparedPrompt
from replaylab_agent.profiles import ProfileStore
from replaylab_agent.runtime import AgentRunResult, AgentRuntime
from replaylab_agent.events import TaskEventWriter
from replaylab_agent.profiles import Profile
from replaylab_agent.skills import SkillCatalog


def _unknown_model_profile() -> Profile:
    return Profile(
        "local",
        "ollama",
        "test-model",
        options={"context_window_tokens": 65_536},
    )


def _snapshot(session: str, event_id: int) -> dict:
    value = {
        "schema": "renderdoc-ultra-current-context/v1",
        "capture": {
            "loaded": True,
            "captureSessionId": session,
            "path": f"C:/captures/{session}.rdc",
        },
        "events": {"currentEventId": event_id},
    }
    value["snapshotId"] = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return value


class FakePinnedBridge:
    def __init__(self) -> None:
        self.completed: list[tuple[str, str]] = []

    def instances(self):
        return [
            SimpleNamespace(
                id="pinned-qrenderdoc",
                capture_loaded=True,
                capture_path="C:/captures/test.rdc",
            )
        ]

    def claim_intents(self, consumer, **kwargs):
        assert consumer
        assert kwargs["instance_id"] == "pinned-qrenderdoc"
        context = {
            "capture": {
                "loaded": True,
                "captureSessionId": "a" * 32,
                "path": "C:/captures/test.rdc",
            },
            "events": {"currentEventId": 7},
        }
        context["snapshotId"] = hashlib.sha256(
            json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        return [
            {
                "intentId": "intent-cancel",
                "leaseToken": "host-only",
                "kind": "explain-current-event",
                "contextSnapshotId": context["snapshotId"],
                "context": context,
                "arguments": {"eventId": 7},
                "recommendedWorkflow": {},
            }
        ]

    def call(self, method, *, instance_id):
        assert method == "get_current_renderdoc_context"
        assert instance_id == "pinned-qrenderdoc"
        return {
            "capture": {
                "loaded": True,
                "captureSessionId": "a" * 32,
                "path": "C:/captures/test.rdc",
            }
        }

    def renew_intent(self, intent, lease_seconds, *, instance_id):
        assert instance_id == "pinned-qrenderdoc"
        return {}

    def complete_intent(self, intent, *, outcome, summary, instance_id):
        assert instance_id == "pinned-qrenderdoc"
        self.completed.append((outcome, summary))
        return {}


class SlowPreflightBridge(FakePinnedBridge):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = threading.Event()
        self.renewals = 0

    def call(self, method, *, instance_id):
        # Model a bridge preflight slower than the first renewal interval. The
        # worker must keep the claim alive before it starts any model work.
        assert self.renewed.wait(timeout=1)
        return super().call(method, instance_id=instance_id)

    def renew_intent(self, intent, lease_seconds, *, instance_id):
        self.renewals += 1
        self.renewed.set()
        return super().renew_intent(
            intent, lease_seconds, instance_id=instance_id
        )


class MultiInstanceBridge(FakePinnedBridge):
    def instances(self):
        return [
            SimpleNamespace(
                id="other-qrenderdoc",
                capture_loaded=True,
                capture_path="C:/captures/other.rdc",
            ),
            SimpleNamespace(
                id="pinned-qrenderdoc",
                capture_loaded=True,
                capture_path="C:/captures/test.rdc",
            ),
        ]


class FailingFinalizationBridge(FakePinnedBridge):
    def complete_intent(self, intent, *, outcome, summary, instance_id):
        del intent, outcome, summary
        assert instance_id == "pinned-qrenderdoc"
        raise RuntimeError("qrenderdoc 回执失败")


class BlockingFinalizationBridge(FakePinnedBridge):
    def __init__(self) -> None:
        super().__init__()
        self.finalization_started = threading.Event()
        self.release_finalization = threading.Event()

    def complete_intent(self, intent, *, outcome, summary, instance_id):
        del intent, outcome, summary
        assert instance_id == "pinned-qrenderdoc"
        self.finalization_started.set()
        self.release_finalization.wait(timeout=5)
        return {}


@pytest.mark.asyncio
async def test_cancelling_worker_cancels_child_and_completes_pinned_lease(tmp_path) -> None:
    runtime = AgentRuntime(
        ProfileStore(tmp_path / "profiles.json"),
        SkillCatalog([]).load(),
    )
    bridge = FakePinnedBridge()
    started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def fake_run(self, *args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cancelled.set()

    runtime.run = MethodType(fake_run, runtime)
    task = asyncio.create_task(runtime.process_one_intent(bridge=bridge))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child_cancelled.is_set()
    assert bridge.completed == [("failed", "AgentHost task cancelled")]


@pytest.mark.asyncio
async def test_cancellation_is_terminal_even_when_queue_ack_fails(tmp_path) -> None:
    runtime = AgentRuntime(
        ProfileStore(tmp_path / "profiles.json"),
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FailingFinalizationBridge()
    started = asyncio.Event()

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        started.set()
        await asyncio.Event().wait()

    runtime.run = MethodType(fake_run, runtime)
    task = asyncio.create_task(runtime.process_one_intent(bridge=bridge))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["kind"] for item in payloads] == ["queued", "running", "cancelled"]


@pytest.mark.asyncio
async def test_provider_failure_is_terminal_even_when_queue_ack_fails(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FailingFinalizationBridge()

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        raise ConnectorError("模型失败：D:/捕获/测试帧.rdc")

    runtime.run = MethodType(fake_run, runtime)
    with pytest.raises(ConnectorError, match="测试帧"):
        await asyncio.wait_for(runtime.process_one_intent(bridge=bridge), timeout=1)

    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["kind"] for item in payloads] == ["queued", "running", "failed"]
    assert payloads[-1]["summary"] == "模型失败：D:/捕获/测试帧.rdc"
    assert payloads[-1]["data"] == {"errorType": "ConnectorError"}


@pytest.mark.asyncio
async def test_success_remains_completed_when_queue_ack_fails(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FailingFinalizationBridge()

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "成功")

    runtime.run = MethodType(fake_run, runtime)
    result = await asyncio.wait_for(runtime.process_one_intent(bridge=bridge), timeout=1)

    assert result is not None and result.text == "成功"
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["kind"] for item in payloads] == ["queued", "running", "completed"]
    assert payloads[-1]["data"] == {"terminalStatus": "completed"}


@pytest.mark.asyncio
async def test_intent_terminal_event_preserves_adaptive_blocked_status(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FailingFinalizationBridge()

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(
            kwargs["task_id"],
            "local",
            "RenderDoc Agent",
            "The workflow needs a different capture selection.",
            workflow=SimpleNamespace(terminal_status="blocked"),
        )

    runtime.run = MethodType(fake_run, runtime)
    result = await asyncio.wait_for(runtime.process_one_intent(bridge=bridge), timeout=1)

    assert result is not None
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["kind"] for item in payloads] == ["queued", "running", "completed"]
    assert payloads[-1]["data"] == {"terminalStatus": "blocked"}


@pytest.mark.asyncio
async def test_terminal_is_visible_and_blocked_queue_ack_has_hard_deadline(
    tmp_path, monkeypatch
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = BlockingFinalizationBridge()
    monkeypatch.setattr(AgentRuntime, "_BRIDGE_FINALIZATION_TIMEOUT", 0.05)

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    task = asyncio.create_task(runtime.process_one_intent(bridge=bridge))
    try:
        for _ in range(100):
            if bridge.finalization_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert bridge.finalization_started.is_set()
        payloads = [
            json.loads(line)
            for line in (tmp_path / "events" / "intent-cancel.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert payloads[-1]["kind"] == "completed"
        # The bridge call is still blocked, proving the terminal event did not
        # wait for its acknowledgement.
        assert not task.done()
        result = await asyncio.wait_for(task, timeout=0.5)
        assert result is not None and result.text == "done"
    finally:
        bridge.release_finalization.set()


@pytest.mark.asyncio
async def test_cancellation_during_success_ack_does_not_emit_second_terminal(
    tmp_path, monkeypatch
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = BlockingFinalizationBridge()
    monkeypatch.setattr(AgentRuntime, "_BRIDGE_FINALIZATION_TIMEOUT", 1.0)

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    task = asyncio.create_task(runtime.process_one_intent(bridge=bridge))
    try:
        for _ in range(100):
            if bridge.finalization_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert bridge.finalization_started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
    finally:
        bridge.release_finalization.set()

    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    terminal = [
        item for item in payloads if item["kind"] in {"completed", "failed", "cancelled"}
    ]
    assert [item["kind"] for item in terminal] == ["completed"]


@pytest.mark.asyncio
async def test_claim_is_renewed_during_context_preflight(
    tmp_path, monkeypatch
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = SlowPreflightBridge()
    monkeypatch.setattr(
        AgentRuntime,
        "_lease_renew_interval",
        staticmethod(lambda _lease_seconds: 0.01),
    )

    async def fake_run(self, *args, **kwargs):
        assert bridge.renewals >= 1
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    result = await runtime.process_one_intent(bridge=bridge, lease_seconds=5)
    assert result is not None and result.text == "done"
    assert bridge.renewals >= 2  # preflight renewal plus finalization extension


@pytest.mark.asyncio
async def test_explicit_instance_disambiguates_multiple_loaded_qrenderdocs(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = MultiInstanceBridge()

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    result = await runtime.process_one_intent(
        bridge=bridge,
        instance_id="pinned-qrenderdoc",
    )
    assert result is not None and result.text == "done"


@pytest.mark.asyncio
async def test_intent_worker_forwards_selected_model_override(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "local",
            "ollama",
            "model-a",
            models=["model-a", "model-b"],
            default_model="model-a",
            options={"context_window_tokens": 65_536},
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FakePinnedBridge()

    async def fake_run(self, *args, **kwargs):
        assert kwargs["model_override"] == "model-b"
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    result = await runtime.process_one_intent(
        bridge=bridge,
        model_override="model-b",
    )
    assert result is not None and result.text == "done"


def test_workflow_boolean_text_never_unlocks_mutation(tmp_path) -> None:
    # Use a direct minimal Skill value to test the authorization gate without
    # depending on the repository skill path.
    from replaylab_agent.skills import Skill

    skill = Skill(
        "test-skill",
        "test",
        tmp_path / "SKILL.md",
        "test",
        frozenset({"get_pipeline_state", "apply_shader_workspace_candidate"}),
        {},
    )
    allowed, withheld = AgentRuntime._effective_tools(
        skill,
        {"recommendedWorkflow": {"replayMutationAuthorized": "false"}},
    )
    assert allowed == frozenset({"get_pipeline_state"})
    assert withheld == frozenset({"apply_shader_workspace_candidate"})


@pytest.mark.asyncio
async def test_authoritative_terminal_precedes_queue_ack_and_is_monotonic(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    bridge = FakePinnedBridge()

    original_complete = bridge.complete_intent

    def checking_complete(intent, *, outcome, summary, instance_id):
        if outcome == "completed":
            path = runtime.event_root / "intent-cancel.jsonl"
            existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert [item["kind"] for item in existing] == [
                "queued",
                "running",
                "completed",
            ]
        return original_complete(
            intent,
            outcome=outcome,
            summary=summary,
            instance_id=instance_id,
        )

    bridge.complete_intent = checking_complete

    async def fake_run(self, *args, **kwargs):
        writer = TaskEventWriter(self.event_root, task_id=kwargs["task_id"])
        writer.emit("queued", profile="local")
        writer.emit("running", profile="local")
        return AgentRunResult(kwargs["task_id"], "local", "RenderDoc Agent", "done")

    runtime.run = MethodType(fake_run, runtime)
    result = await runtime.process_one_intent(bridge=bridge)
    assert result is not None and result.text == "done"
    payloads = [
        json.loads(line)
        for line in (runtime.event_root / "intent-cancel.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["sequence"] for item in payloads] == [1, 2, 3]
    assert [item["kind"] for item in payloads] == ["queued", "running", "completed"]


@pytest.mark.asyncio
async def test_model_general_chat_needs_no_capture_tools_and_emits_authoritative_final(
    tmp_path,
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )

    async def fake_general(self, profile, prompt, writer):
        assert profile.id == "local"
        assert prompt == "private user question"
        writer.emit("running", profile=profile.id, summary="general help")
        return AgentRunResult(writer.task_id, profile.id, "RenderDoc Agent", "general answer")

    runtime._run_model_general = MethodType(fake_general, runtime)
    result = await runtime.run(
        "private user question",
        profile_id="local",
        task_id="general-chat",
    )
    assert result.text == "general answer"
    payloads = [
        json.loads(line)
        for line in (runtime.event_root / "general-chat.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["kind"] for item in payloads] == [
        "queued",
        "running",
        "message-delta",
        "completed",
    ]
    assert payloads[0]["summary"] == "request queued"
    assert payloads[0]["data"] == {"promptChars": len("private user question"), "attempt": 1}
    assert "attempt" not in payloads[0]
    assert "private user question" not in json.dumps(payloads[0])
    assert payloads[2]["text"] == "general answer"
    assert payloads[2]["data"] == {"stream": "final", "reset": True}


@pytest.mark.asyncio
async def test_model_general_streams_only_visible_text_then_resets_with_final(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors
    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
        ThinkingPart,
        ThinkingPartDelta,
    )

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )

    class FakeGeneralAgent:
        def __init__(self, model, *, output_type, **kwargs):
            del model, kwargs
            assert output_type is str

        async def run(self, prompt, **kwargs):
            assert prompt == "How does capture navigation work?"
            handler = kwargs["event_stream_handler"]

            async def events():
                yield PartStartEvent(index=0, part=ThinkingPart("private analysis"))
                yield PartDeltaEvent(
                    index=0,
                    delta=ThinkingPartDelta(content_delta="private analysis delta"),
                )
                yield PartStartEvent(index=1, part=TextPart("Visible "))
                yield PartDeltaEvent(index=1, delta=TextPartDelta("answer"))

            await handler(None, events())
            return SimpleNamespace(output="Visible answer")

    monkeypatch.setattr(pydantic_ai, "Agent", FakeGeneralAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda profile: object())

    result = await runtime.run(
        "How does capture navigation work?",
        profile_id="local",
        task_id="general-provider-stream",
    )
    assert result.text == "Visible answer"
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "general-provider-stream.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    deltas = [event for event in payloads if event["kind"] == "message-delta"]
    provider = [event for event in deltas if event["data"]["stream"] == "provider"]
    assert [event["text"] for event in provider] == ["Visible ", "answer"]
    assert all(event["data"] == {"stream": "provider", "reset": False} for event in provider)
    assert "private analysis" not in "".join(event["text"] for event in deltas)
    assert deltas[-1]["text"] == "Visible answer"
    assert deltas[-1]["data"] == {"stream": "final", "reset": True}
    assert payloads[-1]["kind"] == "completed"


@pytest.mark.asyncio
async def test_model_stage_timeout_emits_failed_terminal_without_hanging(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai
    import replaylab_agent.connectors.models as model_connectors

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    cancelled = asyncio.Event()

    class HangingAgent:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def run(self, *args, **kwargs):
            del args, kwargs
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(pydantic_ai, "Agent", HangingAgent)
    monkeypatch.setattr(model_connectors, "build_model", lambda _profile: object())
    monkeypatch.setattr(
        AgentRuntime,
        "_stage_timeout",
        staticmethod(lambda _profile, _stage: 0.05),
    )

    with pytest.raises(ConnectorError, match="model chat stage timed out"):
        await asyncio.wait_for(
            runtime.run("hello", profile_id="local", task_id="model-timeout"),
            timeout=1,
        )

    assert cancelled.is_set()
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "model-timeout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert payloads[-1]["kind"] == "failed"
    assert payloads[-1]["data"]["errorType"] == "ConnectorError"


@pytest.mark.asyncio
async def test_external_stage_timeout_emits_failed_terminal_without_hanging(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "managed",
            "custom-acp",
            command=[sys.executable],
            options={
                "allow_unsandboxed_agent": True,
                "context_window_tokens": 65_536,
            },
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    cancelled = asyncio.Event()

    class HangingACPConnector:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def run(self, *args, **kwargs):
            del args, kwargs
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(acp_connector, "ACPConnector", HangingACPConnector)
    monkeypatch.setattr(
        AgentRuntime,
        "_stage_timeout",
        staticmethod(lambda _profile, _stage: 0.05),
    )

    with pytest.raises(ConnectorError, match="ACP agent chat stage timed out"):
        await asyncio.wait_for(
            runtime.run("hello", profile_id="managed", task_id="external-timeout"),
            timeout=1,
        )

    assert cancelled.is_set()
    payloads = [
        json.loads(line)
        for line in (tmp_path / "events" / "external-timeout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert payloads[-1]["kind"] == "failed"
    assert payloads[-1]["data"]["errorType"] == "ConnectorError"


@pytest.mark.asyncio
async def test_cursor_runtime_uses_scoped_config_and_fresh_empty_workspace(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.acp as acp_connector
    import replaylab_agent.runtime as runtime_module

    monkeypatch.setenv("LOCALAPPDATA", str((tmp_path / "local").resolve()))
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "cursor-main",
            "cursor-acp",
            options={
                "allow_unsandboxed_agent": True,
                "context_window_tokens": 65_536,
            },
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    captured: dict[str, object] = {}

    class FakeCursorConnector:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        async def run(self, *args, **kwargs):
            del args, kwargs
            workspace = Path(captured["kwargs"]["cwd"])
            captured["workspace_entries"] = sorted(
                path.name for path in workspace.iterdir()
            )
            captured["workspace_mcp"] = json.loads(
                (workspace / ".cursor" / "mcp.json").read_text(encoding="utf-8")
            )
            return SimpleNamespace(text="Cursor answer")

    monkeypatch.setattr(acp_connector, "ACPConnector", FakeCursorConnector)
    monkeypatch.setattr(
        runtime_module,
        "resolved_agent_argv",
        lambda _profile: [sys.executable, "cursor-acp-fixture"],
    )

    result = await runtime.run(
        "hello",
        profile_id="cursor-main",
        task_id="cursor-isolation",
    )

    assert result.text == "Cursor answer"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["provider_id"] == "cursor-acp"
    workspace = Path(kwargs["cwd"])
    assert workspace.name == "cursor-workspace"
    assert captured["workspace_entries"] == [".cursor"]
    assert captured["workspace_mcp"] == {"mcpServers": {}}
    assert not workspace.parent.exists()
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    config_root = Path(environment["CURSOR_CONFIG_DIR"])
    assert (config_root / "cli-config.json").is_file()
    assert environment["HOME"] == str(config_root)
    assert environment["USERPROFILE"] == str(config_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_external_connector_close_uncertainty_is_fatal_even_after_provider_failure(
    tmp_path, monkeypatch, provider_fails
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "managed",
            "custom-acp",
            command=[sys.executable],
            options={
                "allow_unsandboxed_agent": True,
                "context_window_tokens": 65_536,
            },
        )
    )
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    close_cancelled = asyncio.Event()

    class HangingCloseConnector:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def run(self, *args, **kwargs):
            del args, kwargs
            if provider_fails:
                raise ConnectorError("原始 provider 失败")
            return SimpleNamespace(text="provider answer")

        async def close(self):
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled.set()

    monkeypatch.setattr(acp_connector, "ACPConnector", HangingCloseConnector)
    monkeypatch.setattr(AgentRuntime, "_CONNECTOR_CLEANUP_TIMEOUT", 0.05)

    expected = "cleanup failed or timed out"
    with pytest.raises(LifecycleCleanupError, match=expected):
        await asyncio.wait_for(
            runtime.run(
                "hello",
                profile_id="managed",
                task_id=f"close-timeout-{int(provider_fails)}",
            ),
            timeout=1,
        )

    assert close_cancelled.is_set()
    payloads = [
        json.loads(line)
        for line in (
            tmp_path / "events" / f"close-timeout-{int(provider_fails)}.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert payloads[-1]["kind"] == "failed"
    assert expected in payloads[-1]["summary"]


@pytest.mark.asyncio
async def test_ambiguous_clarification_snapshot_asks_before_compaction(
    tmp_path, monkeypatch
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )

    async def must_not_prepare(*_args, **_kwargs):
        raise AssertionError("ambiguous snapshot must not start compaction")

    monkeypatch.setattr(runtime, "_prepare_context_prompt", must_not_prepare)
    prompt = json.dumps(
        {
            "schema": "replaylab-conversation/v2",
            "request": {"role": "user", "content": "继续处理"},
            "clarificationResponse": {
                "schema": "replaylab-clarification-response/v1",
                "clarificationId": "clarification-1",
                "optionIds": [],
                "otherText": "继续处理",
            },
            "contextCandidates": {
                "schema": "replaylab-context-candidates/v1",
                "origin": {
                    "contextInstanceId": "origin-instance",
                    "snapshot": _snapshot("a" * 32, 7),
                },
                "current": {
                    "contextInstanceId": "current-instance",
                    "snapshot": _snapshot("b" * 32, 9),
                },
            },
        },
        ensure_ascii=False,
    )

    result = await runtime.run(prompt, profile_id="local", task_id="snapshot-ask")

    assert result.workflow is not None
    assert result.workflow.terminal_status == "needs-user-input"
    assert result.workflow.clarification is not None
    assert [option.id for option in result.workflow.clarification.options] == [
        "use_origin_snapshot",
        "use_current_snapshot",
    ]


@pytest.mark.asyncio
async def test_selected_current_snapshot_drives_memory_before_planning(
    tmp_path, monkeypatch
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(_unknown_model_profile())
    runtime = AgentRuntime(
        store,
        SkillCatalog([]).load(),
        event_root=tmp_path / "events",
    )
    origin = _snapshot("a" * 32, 7)
    current = _snapshot("b" * 32, 9)

    async def prepare(_profile, prepared_prompt, _writer, **kwargs):
        assert "contextCandidates" not in prepared_prompt
        assert kwargs["capture_session_id"] == "b" * 32
        return PreparedPrompt(
            prompt=prepared_prompt,
            current_request="使用当前选择",
            estimated_before=10,
            estimated_after=10,
            compacted=False,
        )

    async def plan(
        _profile,
        _prompt,
        selected_context,
        writer,
        _intent,
        _capture_path,
        selected_instance,
    ):
        assert selected_context["events"]["currentEventId"] == 9
        assert selected_instance == "current-instance"
        return AgentRunResult(writer.task_id, "local", "test", "selected-current")

    monkeypatch.setattr(runtime, "_prepare_context_prompt", prepare)
    monkeypatch.setattr(runtime, "_run_model_plan", plan)
    prompt = json.dumps(
        {
            "schema": "replaylab-conversation/v2",
            "request": {"role": "user", "content": "使用当前选择"},
            "clarificationResponse": {
                "schema": "replaylab-clarification-response/v1",
                "clarificationId": "clarification-1",
                "optionIds": ["use_current_snapshot"],
                "otherText": "",
            },
            "contextCandidates": {
                "schema": "replaylab-context-candidates/v1",
                "origin": {
                    "contextInstanceId": "origin-instance",
                    "snapshot": origin,
                },
                "current": {
                    "contextInstanceId": "current-instance",
                    "snapshot": current,
                },
            },
        },
        ensure_ascii=False,
    )

    result = await runtime.run(prompt, profile_id="local", task_id="snapshot-current")
    assert result.text == "selected-current"
