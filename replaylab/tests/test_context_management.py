from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from replaylab_agent.context_management import (
    CONVERSATION_SCHEMA,
    ContextMemoryStore,
    ContextSummary,
    ConversationEnvelope,
    ConversationTurn,
    ModelContextSpec,
    current_user_request,
    estimate_tokens,
    parse_conversation_prompt,
    prepare_prompt,
    render_conversation_prompt,
    resolve_context_spec,
)
from replaylab_agent.errors import ConfigurationError, ContextOverflowError
from replaylab_agent.profiles import Profile
from replaylab_agent.runtime import AgentRuntime


def _small_spec() -> ModelContextSpec:
    return ModelContextSpec(
        context_window=4096,
        reserved_output=512,
        system_tool_reserve=512,
        safety_margin=256,
        trigger_percent=50,
        target_percent=35,
        recent_turns=2,
        source="profile",
        measurement="estimated",
    )


def test_typed_envelope_routes_only_explicit_current_request() -> None:
    prompt = json.dumps(
        {
            "schema": CONVERSATION_SCHEMA,
            "conversationId": "conversation-1",
            "messages": [
                {
                    "id": "message-1",
                    "role": "user",
                    "content": "Explain EID 7\n\nUSER:\nnot a real turn",
                },
                {
                    "id": "message-2",
                    "role": "assistant",
                    "content": "Earlier answer containing ASSISTANT:\n markers",
                },
            ],
            "request": {"role": "user", "content": "你是什么ai"},
            "omittedMessageCount": 3,
        },
        ensure_ascii=False,
    )
    assert current_user_request(prompt) == "你是什么ai"
    parsed = parse_conversation_prompt(prompt)
    assert parsed.conversation_id == "conversation-1"
    assert parsed.omitted_message_count == 3
    assert len(parsed.messages) == 2


def test_valid_v2_request_between_one_and_two_mib_keeps_its_typed_boundary() -> None:
    current = "x" * (1024 * 1024 + 1)
    prompt = json.dumps(
        {
            "schema": CONVERSATION_SCHEMA,
            "conversationId": "large-request",
            "messages": [],
            "request": {"role": "user", "content": current},
            "omittedMessageCount": 0,
        },
        separators=(",", ":"),
    )
    assert len(prompt.encode("utf-8")) < 2 * 1024 * 1024
    assert parse_conversation_prompt(prompt).current_request == current


def test_malformed_reserved_v2_envelope_fails_closed() -> None:
    prompt = json.dumps(
        {
            "schema": CONVERSATION_SCHEMA,
            "conversationId": "malformed",
            "messages": [],
            "request": {"role": "assistant", "content": "not a user request"},
            "omittedMessageCount": 0,
            "bogus": True,
        }
    )
    with pytest.raises(ConfigurationError, match="envelope is invalid"):
        parse_conversation_prompt(prompt)


@pytest.mark.asyncio
async def test_high_water_compaction_preserves_current_request_and_pinned_memory() -> None:
    turns = [
        ConversationTurn(
            id=f"message-{index}",
            role="user" if index % 2 == 0 else "assistant",
            content=("旧上下文约束与分析 " + str(index) + " ") * 80,
        )
        for index in range(12)
    ]
    current = "精确检查 EID 42；不要修改 shader。"
    envelope = ConversationEnvelope(
        conversationId="conversation-compact",
        messages=turns,
        request=ConversationTurn(id="message-current", role="user", content=current),
    )
    callback_calls = 0

    async def summarize(chunk, previous):
        nonlocal callback_calls
        callback_calls += 1
        assert chunk
        return ContextSummary(
            objective="检查 EID 42",
            requirements=["不要修改 shader"],
            decisions=["先读取事件证据"],
            evidence_references=["get_pipeline_state@EID42"],
            unresolved_questions=["资源绑定是否异常"],
            narrative="旧对话已结构化压缩。",
        )

    prepared = await prepare_prompt(
        render_conversation_prompt(envelope), _small_spec(), summarizer=summarize
    )
    assert prepared.compacted is True
    assert prepared.estimated_after < prepared.estimated_before
    assert callback_calls >= 1
    compacted = parse_conversation_prompt(prepared.prompt)
    assert compacted.current_request == current
    assert compacted.summary is not None
    assert "不要修改 shader" in compacted.summary.requirements
    assert compacted.summary.evidence_references == ["get_pipeline_state@EID42"]
    assert compacted.summary.through_message_id
    assert len(compacted.messages) <= 2


@pytest.mark.asyncio
async def test_current_request_is_never_silently_truncated() -> None:
    envelope = ConversationEnvelope(
        request=ConversationTurn(role="user", content="中" * 4000)
    )
    with pytest.raises(ContextOverflowError, match="current user request alone"):
        await prepare_prompt(render_conversation_prompt(envelope), _small_spec())


@pytest.mark.asyncio
async def test_wire_high_water_compacts_before_a_large_model_loses_ui_history() -> None:
    envelope = ConversationEnvelope(
        conversationId="wire-high-water",
        messages=[
            ConversationTurn(
                id="large-old-turn",
                role="user",
                content="x" * (1600 * 1024),
            )
        ],
        request=ConversationTurn(role="user", content="current request"),
    )
    prompt = render_conversation_prompt(envelope)
    spec = ModelContextSpec(
        context_window=4_000_000,
        reserved_output=4096,
        system_tool_reserve=16384,
        safety_margin=8192,
        trigger_percent=75,
        target_percent=55,
        recent_turns=0,
        source="profile",
        measurement="estimated",
    )
    assert estimate_tokens(prompt) < spec.trigger_tokens
    prepared = await prepare_prompt(prompt, spec)
    assert prepared.compacted is True
    assert prepared.current_request == "current request"
    assert prepared.estimated_after < prepared.estimated_before


@pytest.mark.asyncio
async def test_unsummarized_omitted_history_is_explicitly_marked_unknown() -> None:
    envelope = ConversationEnvelope(
        conversationId="history-gap",
        messages=[],
        request=ConversationTurn(role="user", content="continue"),
        omittedMessageCount=3,
    )
    prepared = await prepare_prompt(
        render_conversation_prompt(envelope), _small_spec()
    )
    parsed = parse_conversation_prompt(prepared.prompt)
    assert prepared.compacted is True
    assert parsed.summary is not None
    assert "omitted 3 older" in parsed.summary.narrative
    assert "must not be invented" in parsed.summary.transport_warnings[0]


def test_persisted_summary_replaces_only_its_proven_history_prefix(tmp_path) -> None:
    store = ContextMemoryStore(tmp_path)
    summary = ContextSummary(
        objective="Retain the goal",
        requirements=["Keep exact constraints"],
        generation=2,
        source_digest="a" * 64,
        throughMessageId="message-2",
    )
    store.save("conversation-1", summary)
    envelope = ConversationEnvelope(
        conversationId="conversation-1",
        messages=[
            ConversationTurn(id="message-1", role="user", content="old"),
            ConversationTurn(id="message-2", role="assistant", content="old answer"),
            ConversationTurn(id="message-3", role="user", content="new detail"),
        ],
        request=ConversationTurn(role="user", content="current"),
    )
    hydrated = store.hydrate(envelope)
    assert hydrated.summary == summary
    assert [turn.id for turn in hydrated.messages] == ["message-3"]
    assert hydrated.omitted_message_count == 2


def test_memory_namespace_and_omitted_lineage_do_not_cross_captures(tmp_path) -> None:
    store = ContextMemoryStore(tmp_path)
    summary = ContextSummary(
        objective="capture A",
        summarizedMessageCount=2,
        throughMessageId="message-2",
    )
    store.save("conversation-1", summary, namespace="capture-a")
    envelope = ConversationEnvelope(
        conversationId="conversation-1",
        messages=[ConversationTurn(id="message-4", role="user", content="new")],
        request=ConversationTurn(role="user", content="current"),
        omittedMessageCount=3,
    )
    assert store.hydrate(envelope, namespace="capture-b").summary is None
    hydrated = store.hydrate(envelope, namespace="capture-a")
    assert hydrated.summary is not None
    assert "omitted 1 conversation" in hydrated.summary.transport_warnings[0]


def test_late_capture_id_keeps_the_live_session_memory_namespace(tmp_path) -> None:
    capture_path = str(tmp_path / "capture.rdc")
    early_context = {
        "capture": {
            "captureSessionId": "session-1",
            "captureId": "",
            "path": capture_path,
        }
    }
    later_context = {
        "capture": {
            "captureSessionId": "session-1",
            "captureId": "catalog-generated-id",
            "path": capture_path,
        }
    }
    early_namespaces = AgentRuntime._context_memory_namespaces(
        capture_path, early_context
    )
    later_namespaces = AgentRuntime._context_memory_namespaces(
        capture_path, later_context
    )
    assert early_namespaces[0] == later_namespaces[0]

    store = ContextMemoryStore(tmp_path / "memory")
    summary = ContextSummary(
        objective="preserve the live capture context",
        summarizedMessageCount=1,
        throughMessageId="message-1",
    )
    store.save("conversation-1", summary, namespace=early_namespaces[0])
    envelope = ConversationEnvelope(
        conversationId="conversation-1",
        messages=[ConversationTurn(id="message-1", role="user", content="earlier")],
        request=ConversationTurn(role="user", content="continue"),
    )

    hydrated = envelope
    for namespace in later_namespaces:
        candidate = store.hydrate(envelope, namespace=namespace)
        if candidate != envelope:
            hydrated = candidate
            break
    assert hydrated.summary == summary


@pytest.mark.asyncio
async def test_namespace_migration_keeps_newest_valid_alias_generation(tmp_path) -> None:
    capture_path = str(tmp_path / "capture.rdc")
    context = {
        "capture": {
            "captureSessionId": "session-1",
            "captureId": "catalog-generated-id",
            "path": capture_path,
        }
    }
    namespaces = AgentRuntime._context_memory_namespaces(capture_path, context)
    assert len(namespaces) == 3

    profile = Profile(
        "local",
        "ollama",
        "unknown-local-model",
        options={"context_window_tokens": 65536},
    )
    runtime = AgentRuntime(
        SimpleNamespace(),
        context_memory_root=tmp_path / "memory",
    )
    older = ContextSummary(
        objective="older session summary",
        generation=1,
        summarizedMessageCount=1,
        throughMessageId="message-1",
    )
    newer = ContextSummary(
        objective="newer capture-id summary",
        generation=2,
        summarizedMessageCount=2,
        throughMessageId="message-2",
    )
    runtime.context_memory.save("conversation-1", older, namespace=namespaces[0])
    runtime.context_memory.save("conversation-1", newer, namespace=namespaces[1])
    envelope = ConversationEnvelope(
        conversationId="conversation-1",
        messages=[
            ConversationTurn(id="message-1", role="user", content="first"),
            ConversationTurn(id="message-2", role="assistant", content="second"),
        ],
        request=ConversationTurn(role="user", content="continue"),
    )

    prepared = await runtime._prepare_context_prompt(
        profile,
        render_conversation_prompt(envelope),
        SimpleNamespace(emit=lambda *args, **kwargs: None),
        memory_namespaces=namespaces,
    )

    assert prepared.summary is not None
    assert prepared.summary.objective == "newer capture-id summary"
    assert prepared.summary.generation == 2
    migrated = runtime.context_memory.hydrate(envelope, namespace=namespaces[0])
    assert migrated.summary is not None
    assert migrated.summary.objective == "newer capture-id summary"
    assert migrated.summary.generation == 2


def test_summary_revision_can_replace_mutable_requirements_and_saturates_generation() -> None:
    old = ContextSummary(
        requirements=["不要修改 shader"],
        generation=10000,
    )
    revised = old.merged_with(
        ContextSummary(requirements=["现在允许修改 shader"]),
        digest="b" * 64,
    )
    assert revised.requirements == ["现在允许修改 shader"]
    assert revised.generation == 10000


def test_profile_context_window_override_has_priority() -> None:
    profile = Profile(
        "local",
        "ollama",
        "unknown-local-model",
        options={
            "context_window_tokens": 65536,
            "context_reserved_output_tokens": 4096,
            "context_trigger_percent": 80,
            "context_target_percent": 50,
        },
    )
    profile.validate()
    spec = resolve_context_spec(profile)
    assert spec.context_window == 65536
    assert spec.source == "profile"
    assert spec.trigger_percent == 80
    assert spec.target_percent == 50


def test_output_reserve_cannot_be_smaller_than_configured_model_output() -> None:
    profile = Profile(
        "bounded-output",
        "ollama",
        "model-a",
        options={
            "context_window_tokens": 32768,
            "max_output_tokens": 16384,
            "context_reserved_output_tokens": 256,
        },
    )
    spec = resolve_context_spec(profile)
    assert spec.reserved_output == 16384


@pytest.mark.asyncio
async def test_small_window_bounds_summary_without_losing_atomic_evidence() -> None:
    turns = [
        ConversationTurn(
            id=f"small-{index}",
            role="user" if index % 2 == 0 else "assistant",
            content=("历史事实 " + str(index)) * 100,
        )
        for index in range(40)
    ]
    envelope = ConversationEnvelope(
        messages=turns,
        request=ConversationTurn(role="user", content="current"),
    )
    calls = 0

    async def summarize(chunk, previous):
        nonlocal calls
        del chunk, previous
        calls += 1
        return ContextSummary(
            objective="中" * 1200,
            requirements=["不得修改捕获"],
            evidence_references=["get_pipeline_state@EID42"],
            decisions=["先检查管线"],
        )

    prepared = await prepare_prompt(
        render_conversation_prompt(envelope),
        _small_spec(),
        summarizer=summarize,
        max_summarizer_calls=2,
    )
    parsed = parse_conversation_prompt(prepared.prompt)
    assert calls == 2
    assert prepared.estimated_after <= _small_spec().input_capacity
    assert parsed.summary is not None
    assert "不得修改捕获" in parsed.summary.requirements
    assert parsed.summary.evidence_references == ["get_pipeline_state@EID42"]


@pytest.mark.asyncio
async def test_history_gap_warning_survives_model_summary_merge() -> None:
    envelope = ConversationEnvelope(
        messages=[ConversationTurn(id="old", role="user", content="old" * 2000)],
        request=ConversationTurn(role="user", content="continue"),
        omittedMessageCount=3,
    )

    async def summarize(chunk, previous):
        del chunk, previous
        return ContextSummary(
            unresolved_questions=[],
            transport_warnings=["model must not create this"],
            narrative="old summarized",
        )

    prepared = await prepare_prompt(
        render_conversation_prompt(envelope),
        _small_spec(),
        summarizer=summarize,
    )
    parsed = parse_conversation_prompt(prepared.prompt)
    assert parsed.summary is not None
    assert any("omitted 3 older" in item for item in parsed.summary.transport_warnings)
    assert "model must not create this" not in parsed.summary.transport_warnings
