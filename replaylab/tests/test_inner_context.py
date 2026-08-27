from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import (
    LoadCapabilityCallPart,
    LoadCapabilityReturnPart,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from replaylab_agent.context_management import ModelContextSpec
from replaylab_agent.errors import ContextOverflowError
from replaylab_agent.inner_context import SpecialistHistoryCompactor, _compactable_block
from replaylab_agent.mcp_tools import ToolEvidenceRecord, evidence_digest


def _spec() -> ModelContextSpec:
    return ModelContextSpec(
        context_window=4096,
        reserved_output=512,
        system_tool_reserve=512,
        safety_margin=256,
        trigger_percent=50,
        target_percent=20,
        recent_turns=2,
        source="profile",
        measurement="estimated",
    )


def _exchange(index: int, content: str) -> list[object]:
    call_id = f"call-{index}"
    return [
        ModelResponse(
            parts=[
                ToolCallPart(
                    f"get_evidence_{index}",
                    {"eventId": index},
                    tool_call_id=call_id,
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    f"get_evidence_{index}",
                    content,
                    tool_call_id=call_id,
                )
            ]
        ),
    ]


def _record(index: int, content: object, *, call_id: str | None = None) -> ToolEvidenceRecord:
    return ToolEvidenceRecord(
        tool_call_id=call_id or f"call-{index}",
        tool=f"get_evidence_{index}",
        arguments_sha256=evidence_digest({"eventId": index}),
        result_sha256=evidence_digest(content),
        outcome="completed",
    )


@pytest.mark.asyncio
async def test_specialist_history_compacts_complete_tool_exchanges_atomically() -> None:
    events = []
    records = (
        _record(1, "old-a" * 5000),
        _record(2, "old-b" * 5000),
        _record(3, "newest evidence"),
    )
    compactor = SpecialistHistoryCompactor(
        _spec(), on_compaction=lambda generation, before, after: events.append(
            (generation, before, after)
        ), evidence_records=lambda: records
    )
    initial = ModelRequest(parts=[UserPromptPart("Inspect EID 9")])
    messages = [
        initial,
        *_exchange(1, "old-a" * 5000),
        *_exchange(2, "old-b" * 5000),
        *_exchange(3, "newest evidence"),
    ]

    compacted = await compactor(messages)

    assert compacted[0] is initial
    assert compactor.generation == 1
    assert events and events[0][2] < events[0][1]
    compacted_memory = compacted[1].parts[0].content
    assert "ReplayLab specialist context compaction/v1" in compacted_memory
    assert "completed_tools=" in compacted_memory
    assert "ledger_sha256=" in compacted_memory
    assert "latest_tool=" in compacted_memory
    # The newest response/request pair remains intact and in order.
    assert compacted[-2] is messages[-2]
    assert compacted[-1] is messages[-1]
    assert compacted[-2].parts[0].tool_call_id == compacted[-1].parts[0].tool_call_id
    # No response containing a tool call is ever retained without its following return.
    for index, message in enumerate(compacted):
        if isinstance(message, ModelResponse) and message.parts:
            assert index + 1 < len(compacted)
            assert isinstance(compacted[index + 1], ModelRequest)
            assert message.parts[0].tool_call_id == compacted[index + 1].parts[0].tool_call_id


@pytest.mark.asyncio
async def test_specialist_history_fails_when_live_atomic_exchange_cannot_fit() -> None:
    records = (
        _record(1, "old" * 5000),
        _record(2, "current" * 10000),
    )
    compactor = SpecialistHistoryCompactor(
        _spec(), evidence_records=lambda: records
    )
    messages = [
        ModelRequest(parts=[UserPromptPart("Inspect")]),
        *_exchange(1, "old" * 5000),
        *_exchange(2, "current" * 10000),
    ]

    with pytest.raises(ContextOverflowError, match="newest complete tool exchange"):
        await compactor(messages)


@pytest.mark.asyncio
async def test_tight_window_shrinks_manifest_without_forgetting_tool_progress() -> None:
    old_content = "old-result-" * 50
    newest_content = "new-result-" * 50
    records = (_record(1, old_content), _record(2, newest_content))
    compactor = SpecialistHistoryCompactor(
        _spec(), evidence_records=lambda: records
    )
    messages = [
        ModelRequest(parts=[UserPromptPart("Inspect EID 9 " * 12)]),
        *_exchange(1, old_content),
        *_exchange(2, newest_content),
    ]

    compacted = await compactor(messages)

    memory = compacted[1].parts[0].content
    assert "completed_tools=get_evidence_1,get_evidence_2" in memory
    assert "ledger_sha256=" in memory
    assert "latest_tool=get_evidence_1" in memory
    assert compactor.generation == 1


@pytest.mark.asyncio
async def test_unknown_or_plain_retry_block_is_pinned_fail_closed() -> None:
    compactor = SpecialistHistoryCompactor(_spec())
    call = ToolCallPart("get_evidence", {}, tool_call_id="call-pinned")
    pinned_response = ModelResponse(parts=[call])
    # A retry without a tool identity is model/user feedback, not a tool return.
    pinned_request = ModelRequest(parts=[RetryPromptPart("try another approach")])
    messages = [
        ModelRequest(parts=[UserPromptPart("Inspect")]),
        pinned_response,
        pinned_request,
        *_exchange(2, "newest" * 10000),
    ]

    with pytest.raises(ContextOverflowError, match="pinned or unknown"):
        await compactor(messages)


def test_capability_subclasses_duplicate_ids_and_digest_mismatch_are_pinned() -> None:
    capability_call = LoadCapabilityCallPart(tool_call_id="capability-1")
    capability_return = LoadCapabilityReturnPart(
        tool_call_id="capability-1",
        content={"capability": "example"},
    )
    assert not _compactable_block(
        [
            ModelResponse(parts=[capability_call]),
            ModelRequest(parts=[capability_return]),
        ],
        (),
    )

    duplicate_call = ToolCallPart(
        "get_evidence_1", {"eventId": 1}, tool_call_id="duplicate"
    )
    duplicate_return = ToolReturnPart(
        "get_evidence_1", "result", tool_call_id="duplicate"
    )
    duplicate_block = [
        ModelResponse(parts=[duplicate_call, duplicate_call]),
        ModelRequest(parts=[duplicate_return, duplicate_return]),
    ]
    duplicate_record = _record(1, "result", call_id="duplicate")
    assert not _compactable_block(duplicate_block, (duplicate_record,))

    ordinary = _exchange(1, "result")
    wrong_result = _record(1, "tampered-result")
    assert not _compactable_block(ordinary, (wrong_result,))
    # Even two individually valid-looking rows for one identity are ambiguous.
    assert not _compactable_block(ordinary, (_record(1, "result"), _record(1, "result")))


@pytest.mark.parametrize("outcome", ["failed", "denied", "interrupted"])
def test_non_successful_tool_returns_remain_verbatim(outcome: str) -> None:
    call = ToolCallPart(
        "get_evidence_1", {"eventId": 1}, tool_call_id="call-1"
    )
    returned = ToolReturnPart(
        "get_evidence_1",
        "failure detail",
        tool_call_id="call-1",
        outcome=outcome,
    )
    record = ToolEvidenceRecord(
        tool_call_id="call-1",
        tool="get_evidence_1",
        arguments_sha256=evidence_digest({"eventId": 1}),
        result_sha256=evidence_digest("failure detail"),
        outcome="failed",
    )
    assert not _compactable_block(
        [ModelResponse(parts=[call]), ModelRequest(parts=[returned])],
        (record,),
    )


@pytest.mark.asyncio
async def test_mcp_toolset_cleanup_has_an_independent_deadline(monkeypatch) -> None:
    import replaylab_agent.mcp_tools as mcp_tools

    cleanup_cancelled = asyncio.Event()

    async def hanging_exit() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_cancelled.set()

    monkeypatch.setattr(mcp_tools, "_MCP_CLEANUP_TIMEOUT", 0.02)
    with pytest.raises(
        mcp_tools.LifecycleCleanupError, match="cleanup was not confirmed"
    ):
        await mcp_tools._bounded_toolset_exit(hanging_exit())
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)
