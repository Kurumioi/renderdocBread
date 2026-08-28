from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from replaylab_agent.context_management import (
    CONVERSATION_SCHEMA,
    ClarificationResponse,
    ContextMemoryStore,
    ContextSummary,
    ConversationEnvelope,
    ConversationTurn,
    MemoryFact,
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


def _context_snapshot(session_id: str, generation: int = 0, **extra):
    snapshot = {
        "schema": "renderdoc-ultra-current-context/v1",
        "schemaVersion": 1,
        "contextGeneration": generation,
        "capture": {"loaded": True, "captureSessionId": session_id},
        **extra,
    }
    snapshot["snapshotId"] = hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return snapshot


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
            content=("旧上下文约束与分析 " + str(index) + " ") * 80
            + (" get_pipeline_state@EID42" if index == 1 else ""),
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
async def test_wire_ceiling_does_not_shrink_the_largest_supported_model_window() -> None:
    envelope = ConversationEnvelope(
        conversationId="wire-high-water",
        messages=[
            ConversationTurn(
                id="large-old-turn",
                role="user",
                content="x" * (8 * 1024 * 1024),
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
        trigger_percent=100,
        target_percent=25,
        recent_turns=0,
        source="profile",
        measurement="estimated",
    )
    assert spec.proactive_trigger_tokens == spec.trigger_tokens
    assert estimate_tokens(prompt) >= spec.trigger_tokens
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


def test_codex_model_uses_openai_catalog_window_and_new_default_policy() -> None:
    profile = Profile(
        "subscription",
        "codex",
        "gpt-5.6-sol",
    )
    spec = resolve_context_spec(profile)
    assert spec.context_window >= 1_000_000
    assert spec.context_window > 32768
    assert spec.source == "model-catalog"
    assert spec.trigger_percent == 100
    assert spec.target_percent == 25
    assert spec.trigger_tokens == spec.input_capacity


def test_unknown_model_requires_a_verified_window_instead_of_implicit_32k() -> None:
    profile = Profile("unknown", "ollama", "not-in-the-model-catalog")
    with pytest.raises(ConfigurationError, match="verified context_window_tokens"):
        resolve_context_spec(profile)


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
            content=("历史事实 " + str(index)) * 100
            + (" get_pipeline_state@EID42" if index == 1 else ""),
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


def test_same_key_user_correction_supersedes_older_memory_fact() -> None:
    old = ContextSummary(
        memoryFacts=[
            MemoryFact(
                key="shader-edit-permission",
                category="requirement",
                value="Do not edit the shader",
                sourceMessageIds=["message-1"],
                sourceRole="user",
                importance=100,
                createdUnixMs=1,
                lastConfirmedUnixMs=1,
            )
        ]
    )
    newer = ContextSummary(
        requirements=["Editing the shader is now allowed"],
        memoryFacts=[
            MemoryFact(
                key="shader-edit-permission",
                category="requirement",
                value="Editing the shader is now allowed",
                sourceMessageIds=["message-2"],
                sourceRole="user",
                importance=100,
                createdUnixMs=2,
                lastConfirmedUnixMs=2,
            )
        ],
    )
    merged = old.merged_with(newer, digest="c" * 64)
    active = [fact for fact in merged.memory_facts if fact.status == "active"]
    superseded = [fact for fact in merged.memory_facts if fact.status == "superseded"]
    assert [fact.value for fact in active] == ["Editing the shader is now allowed"]
    assert [fact.value for fact in superseded] == ["Do not edit the shader"]
    assert superseded[0].superseded_by == active[0].id


@pytest.mark.asyncio
async def test_capture_change_expires_capture_facts_but_keeps_general_requirements() -> None:
    capture_a = "a" * 32
    capture_b = "b" * 32
    summary = ContextSummary(
        requirements=["Answer in Chinese"],
        evidence_references=["get_pipeline_state@EID42"],
        captureSessionId=capture_a,
        memoryFacts=[
            MemoryFact(
                key="language",
                category="requirement",
                value="Answer in Chinese",
                sourceMessageIds=["message-1"],
                sourceRole="user",
                importance=100,
            ),
            MemoryFact(
                key="pipeline-evidence",
                category="evidence",
                value="get_pipeline_state@EID42",
                sourceMessageIds=["message-2"],
                sourceRole="assistant",
                importance=90,
                scope="capture",
                captureSessionId=capture_a,
            ),
        ],
    )
    envelope = ConversationEnvelope(
        messages=[],
        request=ConversationTurn(role="user", content="continue"),
        summary=summary,
    )
    prepared = await prepare_prompt(
        render_conversation_prompt(envelope),
        _small_spec(),
        capture_session_id=capture_b,
    )
    parsed = parse_conversation_prompt(prepared.prompt)
    assert prepared.compacted is True
    assert parsed.summary is not None
    assert parsed.summary.capture_session_id == capture_b
    assert parsed.summary.requirements == ["Answer in Chinese"]
    assert parsed.summary.evidence_references == []
    evidence = [
        fact for fact in parsed.summary.memory_facts if fact.category == "evidence"
    ]
    assert len(evidence) == 1
    assert evidence[0].status == "expired"


@pytest.mark.asyncio
async def test_recent_raw_history_uses_a_token_budget_and_stays_a_suffix() -> None:
    spec = _small_spec().model_copy(
        update={"recent_turns": 6, "recent_token_percent": 10}
    )
    turns = [
        ConversationTurn(id="old-1", role="user", content="small old context"),
        ConversationTurn(id="old-2", role="assistant", content="small old answer"),
        ConversationTurn(id="newest", role="assistant", content="x" * 5000),
    ]
    envelope = ConversationEnvelope(
        messages=turns,
        request=ConversationTurn(role="user", content="current request"),
    )
    prepared = await prepare_prompt(render_conversation_prompt(envelope), spec)
    parsed = parse_conversation_prompt(prepared.prompt)
    assert prepared.compacted is True
    assert parsed.current_request == "current request"
    assert parsed.messages == []


@pytest.mark.asyncio
async def test_memory_budget_prefers_important_active_facts_and_expires_stale_facts() -> None:
    facts = [
        MemoryFact(
            category="conversation_note",
            value=(f"low-value-note-{index} " + "x" * 450),
            sourceMessageIds=[f"message-{index}"],
            sourceRole="assistant",
            importance=5,
        )
        for index in range(60)
    ]
    facts.extend(
        [
            MemoryFact(
                key="hard-constraint",
                category="requirement",
                value="Never invent RenderDoc evidence",
                sourceMessageIds=["message-important"],
                sourceRole="user",
                importance=100,
            ),
            MemoryFact(
                key="stale-assumption",
                category="assumption",
                value="This assumption has expired",
                sourceMessageIds=["message-stale"],
                sourceRole="assistant",
                importance=99,
                validUntilUnixMs=1,
            ),
        ]
    )
    envelope = ConversationEnvelope(
        request=ConversationTurn(role="user", content="continue"),
        summary=ContextSummary(memoryFacts=facts),
    )
    prepared = await prepare_prompt(render_conversation_prompt(envelope), _small_spec())
    parsed = parse_conversation_prompt(prepared.prompt)
    assert parsed.summary is not None
    active_values = {
        fact.value for fact in parsed.summary.memory_facts if fact.status == "active"
    }
    assert "Never invent RenderDoc evidence" in active_values
    assert "This assumption has expired" not in active_values


@pytest.mark.asyncio
async def test_clarification_metadata_survives_compaction_byte_for_byte() -> None:
    origin = _context_snapshot("d" * 32, 1)
    current = _context_snapshot("d" * 32, 2)
    envelope = ConversationEnvelope.model_validate(
        {
            "schema": CONVERSATION_SCHEMA,
            "messages": [
                {"id": f"old-{index}", "role": "user", "content": "旧消息" * 500}
                for index in range(6)
            ],
            "request": {"role": "user", "content": "使用原始快照"},
            "clarificationResponse": {
                "schema": "replaylab-clarification-response/v1",
                "clarificationId": "clarification-1",
                "optionIds": ["origin", "include-current"],
                "otherText": "Use the original EID but compare the current shader.",
            },
            "contextCandidates": {
                "schema": "replaylab-context-candidates/v1",
                "origin": {
                    "contextInstanceId": "origin-1",
                    "snapshot": origin,
                },
                "current": {
                    "contextInstanceId": "current-1",
                    "snapshot": current,
                },
            },
        }
    )
    prepared = await prepare_prompt(
        render_conversation_prompt(envelope), _small_spec()
    )
    parsed = parse_conversation_prompt(prepared.prompt)
    assert prepared.clarification_response == envelope.clarification_response
    assert prepared.context_candidates == envelope.context_candidates
    assert prepared.context_candidates is not None
    assert prepared.context_candidates.origin is not None
    assert prepared.context_candidates.current is not None
    assert parsed.clarification_response == envelope.clarification_response
    assert parsed.context_candidates is None


def test_clarification_response_requires_a_stable_non_empty_selection() -> None:
    with pytest.raises(ValueError, match="requires optionIds or otherText"):
        ClarificationResponse(
            clarificationId="clarification-1",
            optionIds=[],
            otherText="",
        )
    with pytest.raises(ValueError, match="must be unique"):
        ClarificationResponse(
            clarificationId="clarification-1",
            optionIds=["same", "same"],
        )


def test_context_candidates_reject_tampering_oversize_and_empty_payloads() -> None:
    tampered = _context_snapshot("e" * 32)
    tampered["contextGeneration"] = 99
    with pytest.raises(ValueError, match="content hash does not match"):
        ConversationEnvelope.model_validate(
            {
                "request": {"role": "user", "content": "choose"},
                "contextCandidates": {
                    "schema": "replaylab-context-candidates/v1",
                    "origin": {
                        "contextInstanceId": "origin-1",
                        "snapshot": tampered,
                    },
                },
            }
        )

    oversized = _context_snapshot("e" * 32, padding="x" * (512 * 1024))
    with pytest.raises(ValueError, match="exceeds 512 KiB"):
        ConversationEnvelope.model_validate(
            {
                "request": {"role": "user", "content": "choose"},
                "contextCandidates": {
                    "schema": "replaylab-context-candidates/v1",
                    "origin": {
                        "contextInstanceId": "origin-1",
                        "snapshot": oversized,
                    },
                },
            }
        )

    with pytest.raises(ValueError, match="requires origin or current"):
        ConversationEnvelope.model_validate(
            {
                "request": {"role": "user", "content": "choose"},
                "contextCandidates": {
                    "schema": "replaylab-context-candidates/v1"
                },
            }
        )
