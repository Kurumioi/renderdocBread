"""Provider-neutral context compaction for a specialist's inner tool loop.

Pydantic AI runs this processor immediately before every model request.  A
model response containing tool calls and the following request containing
their returns are treated as one atomic block: ReplayLab keeps or removes the
whole block, never an orphan call/return half.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .context_management import ModelContextSpec, estimate_tokens
from .errors import ContextOverflowError


_COMPACTION_MARKER = "[ReplayLab specialist context compaction/v1]"


def _message_tokens(messages: Sequence[Any]) -> int:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    encoded = ModelMessagesTypeAdapter.dump_json(list(messages))
    # UTF-8 byte count is a deliberately conservative provider-neutral upper
    # bound for byte-fallback tokenizers. This inner-loop guard values safety
    # over squeezing an unknown model's advertised window.
    return max(
        estimate_tokens(encoded.decode("utf-8", errors="replace")), len(encoded)
    )


def _atomic_blocks(messages: Sequence[Any]) -> tuple[list[Any], list[list[Any]]]:
    """Return an immutable first request plus response/request atomic blocks."""

    from pydantic_ai.messages import ModelRequest, ModelResponse

    values = list(messages)
    if not values:
        return [], []
    prefix = [values[0]] if isinstance(values[0], ModelRequest) else []
    index = len(prefix)
    blocks: list[list[Any]] = []
    while index < len(values):
        message = values[index]
        if (
            isinstance(message, ModelResponse)
            and index + 1 < len(values)
            and isinstance(values[index + 1], ModelRequest)
        ):
            blocks.append([message, values[index + 1]])
            index += 2
        else:
            blocks.append([message])
            index += 1
    return prefix, blocks


def _safe_content(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _verified_tool_exchange(
    block: Sequence[Any], evidence_records: Sequence[Any]
) -> list[tuple[Any, Any, Any]] | None:
    """Return exact regular call/return/ledger triples, or pin fail-closed."""

    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from .mcp_tools import evidence_digest

    if (
        len(block) != 2
        or type(block[0]) is not ModelResponse
        or type(block[1]) is not ModelRequest
    ):
        return None
    response, request = block
    if any(
        type(part) not in (ToolCallPart, TextPart, ThinkingPart)
        for part in response.parts
    ):
        return None
    calls = [
        part
        for part in response.parts
        if type(part) is ToolCallPart
    ]
    if not calls:
        return None
    if any(type(part) is not ToolReturnPart for part in request.parts):
        # Failed/retry, user, native capability, and future message shapes stay
        # verbatim so the specialist cannot forget an unsuccessful operation.
        return None
    returns = list(request.parts)
    call_ids = [str(part.tool_call_id) for part in calls]
    return_ids = [str(part.tool_call_id) for part in returns]
    if (
        any(not value for value in call_ids)
        or len(set(call_ids)) != len(call_ids)
        or len(set(return_ids)) != len(return_ids)
        or set(call_ids) != set(return_ids)
    ):
        return None
    return_by_id = {str(part.tool_call_id): part for part in returns}
    triples: list[tuple[Any, Any, Any]] = []
    for call in calls:
        returned = return_by_id[str(call.tool_call_id)]
        if (
            call.tool_name != returned.tool_name
            or returned.outcome != "success"
        ):
            return None
        matching = [
            record
            for record in evidence_records
            if str(getattr(record, "tool_call_id", "")) == str(call.tool_call_id)
            and str(getattr(record, "tool", "")) == call.tool_name
        ]
        # Reused ids, failed audit rows, and absent audit rows are ambiguous.
        if len(matching) != 1 or getattr(matching[0], "outcome", "") != "completed":
            return None
        record = matching[0]
        try:
            call_arguments = call.args_as_dict()
        except (TypeError, ValueError):
            return None
        if (
            evidence_digest(call_arguments)
            != str(getattr(record, "arguments_sha256", ""))
            or evidence_digest(returned.content)
            != str(getattr(record, "result_sha256", ""))
        ):
            return None
        triples.append((call, returned, record))
    return triples


def _compactable_block(
    block: Sequence[Any], evidence_records: Sequence[Any] = ()
) -> bool:
    """Return whether a block is a fully understood, host-proven exchange."""

    from pydantic_ai.messages import ModelRequest, UserPromptPart

    if len(block) == 1 and type(block[0]) is ModelRequest:
        message = block[0]
        return bool(
            isinstance(message.metadata, dict)
            and message.metadata.get("replaylabContextCompaction") is True
            and all(type(part) is UserPromptPart for part in message.parts)
        )
    return _verified_tool_exchange(block, evidence_records) is not None


def _bounded_excerpt(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    marker = f" …[middle compacted; sha256={digest}]… "
    if limit <= len(marker) + 16:
        return marker[:limit]
    left = (limit - len(marker)) // 2
    return value[:left] + marker + value[-(limit - len(marker) - left) :]


def _compaction_manifest(
    blocks: Sequence[Sequence[Any]],
    evidence_records: Sequence[Any],
    max_chars: int = 1024,
) -> str:
    """Keep a compact, non-empty progress ledger even under tight budgets."""

    from pydantic_ai.messages import ModelRequest, UserPromptPart

    completed = [
        record
        for record in evidence_records
        if getattr(record, "outcome", "") == "completed"
    ]
    ledger_rows = [
        {
            "callId": str(getattr(record, "tool_call_id", "")),
            "tool": str(getattr(record, "tool", "")),
            "argumentsSha256": str(getattr(record, "arguments_sha256", "")),
            "resultSha256": str(getattr(record, "result_sha256", "")),
        }
        for record in completed
    ]
    ledger_sha = hashlib.sha256(
        _safe_content(ledger_rows).encode("utf-8", errors="replace")
    ).hexdigest()
    tool_names = list(
        dict.fromkeys(row["tool"] for row in ledger_rows if row["tool"])
    )
    rendered_tools = ",".join(tool_names)

    latest: tuple[Any, Any, Any] | None = None
    prior_memory = ""
    for block in blocks:
        verified = _verified_tool_exchange(block, evidence_records)
        if verified:
            latest = verified[-1]
        for message in block:
            if (
                type(message) is ModelRequest
                and isinstance(message.metadata, dict)
                and message.metadata.get("replaylabContextCompaction") is True
            ):
                for part in message.parts:
                    if type(part) is UserPromptPart:
                        prior_memory = _safe_content(part.content)

    limit = max(384, int(max_chars))
    lines = [
        _COMPACTION_MARKER,
        f"completed_calls={len(completed)}; ledger_sha256={ledger_sha}",
    ]
    latest_content = ""
    if latest is not None:
        call, returned, record = latest
        lines.append(
            f"latest_tool={call.tool_name[:64]}; result_sha256="
            f"{getattr(record, 'result_sha256', '')}"
        )
        latest_content = _safe_content(returned.content)

    def append_if_fit(value: str) -> None:
        if len("\n".join([*lines, value])) <= limit:
            lines.append(value)

    append_if_fit("completed_tools=" + (rendered_tools or "(none)"))
    if latest is not None:
        append_if_fit(
            "latest_arguments_sha256="
            + str(getattr(latest[2], "arguments_sha256", ""))
        )
    append_if_fit("Older host-verified tool exchanges were removed atomically.")

    core = "\n".join(lines)
    remaining = limit - len(core) - 18
    if remaining > 32 and latest_content:
        core += "\nlatest_excerpt=" + _bounded_excerpt(latest_content, remaining)
    elif remaining > 32 and prior_memory:
        core += "\nprior_memory=" + _bounded_excerpt(prior_memory, remaining)
    return core[:limit]


def _compaction_summary(
    blocks: Sequence[Sequence[Any]],
    max_chars: int,
    evidence_records: Sequence[Any] = (),
) -> str:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    facts: list[tuple[str, str]] = []
    for block in blocks:
        verified = _verified_tool_exchange(block, evidence_records)
        if verified is not None:
            for call, returned, record in verified:
                facts.append(
                    (
                        f"admitted tool call {call.tool_name}",
                        "arguments_sha256="
                        + str(getattr(record, "arguments_sha256", "")),
                    )
                )
                facts.append(
                    (
                        f"admitted tool return {returned.tool_name} ({returned.outcome})",
                        "result_sha256="
                        + str(getattr(record, "result_sha256", ""))
                        + "; excerpt="
                        + _safe_content(returned.content),
                    )
                )
        for message in block:
            if type(message) is ModelResponse:
                for part in message.parts:
                    if type(part) is TextPart and part.content.strip():
                        facts.append(("specialist note", part.content.strip()))
            elif type(message) is ModelRequest:
                if isinstance(message.metadata, dict) and message.metadata.get(
                    "replaylabContextCompaction"
                ):
                    for part in message.parts:
                        if type(part) is UserPromptPart:
                            facts.append(("earlier compacted context", _safe_content(part.content)))

    header = _compaction_manifest(blocks, evidence_records, min(max_chars, 1024))
    header += (
        "\nAdditional bounded notes follow; the host's separate audit remains authoritative.\n"
    )
    if not facts or len(header) >= max_chars:
        return header
    available = max(0, max_chars - len(header) - len(facts) * 12)
    share = max(32, available // len(facts))
    lines: list[str] = []
    for label, value in facts:
        line = f"- {label}: {_bounded_excerpt(value, share)}"
        if len(header) + sum(len(item) + 1 for item in lines) + len(line) > max_chars:
            break
        lines.append(line)
    return header + "\n".join(lines)


@dataclass(slots=True)
class SpecialistHistoryCompactor:
    """A deterministic Pydantic AI ProcessHistory callback with host budgets."""

    spec: ModelContextSpec
    on_compaction: Callable[[int, int, int], None] | None = None
    evidence_records: Callable[[], Sequence[Any]] | None = None
    generation: int = 0

    async def __call__(self, messages: list[Any]) -> list[Any]:
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        before = _message_tokens(messages)
        if before <= self.spec.trigger_tokens:
            return messages

        prefix, blocks = _atomic_blocks(messages)
        # The first request and the newest response/request block are live
        # context. If there is nothing older, there is no safe unit to remove.
        if len(blocks) < 2:
            if before > self.spec.input_capacity:
                raise ContextOverflowError(
                    "the specialist's immutable request and newest tool exchange exceed the "
                    "selected model context window"
                )
            return messages

        # Only remove a contiguous, fully understood prefix. An unknown/native
        # part pins itself and everything newer so ordering and semantics stay
        # intact. The newest live block is always pinned.
        evidence_records = self.evidence_records() if self.evidence_records else ()
        removable_count = 0
        for block in blocks[:-1]:
            if not _compactable_block(block, evidence_records):
                break
            removable_count += 1
        if removable_count == 0:
            if before > self.spec.input_capacity:
                raise ContextOverflowError(
                    "the specialist history contains a pinned or unknown message block that "
                    "cannot fit the selected model context window"
                )
            return messages

        kept: list[list[Any]] = list(blocks[removable_count:])
        dropped: list[list[Any]] = list(blocks[:removable_count])
        for block in reversed(blocks[:removable_count]):
            candidate_dropped = dropped[:-1]
            candidate_summary: list[Any] = []
            if candidate_dropped:
                candidate_summary = [
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                _compaction_manifest(
                                    candidate_dropped,
                                    evidence_records,
                                    max(512, min(1024, self.spec.target_tokens // 3)),
                                )
                            )
                        ],
                        metadata={"replaylabContextCompaction": True},
                    )
                ]
            candidate_blocks = [block, *kept]
            candidate = (
                prefix
                + candidate_summary
                + [item for group in candidate_blocks for item in group]
            )
            if _message_tokens(candidate) <= self.spec.target_tokens:
                kept.insert(0, block)
                dropped.pop()
            else:
                break

        if not dropped:
            return messages
        summary_chars = max(512, min(64 * 1024, self.spec.target_tokens // 2))
        summary = ModelRequest(
            parts=[
                UserPromptPart(
                    _compaction_summary(
                        dropped,
                        summary_chars,
                        evidence_records,
                    )
                )
            ],
            metadata={"replaylabContextCompaction": True},
        )
        compacted = prefix + [summary] + [item for group in kept for item in group]

        # Prefer the newest complete exchange over detailed excerpts, but never
        # replace older progress with an empty "omitted" marker. The compact
        # manifest retains completed tool names plus a host-ledger digest and
        # the latest verified result identity.
        if _message_tokens(compacted) > self.spec.target_tokens:
            live = [item for group in kept for item in group]
            requested = max(384, min(1024, self.spec.input_capacity // 3))
            limits = list(
                dict.fromkeys(
                    [requested, 896, 768, 640, 512, 448, 384]
                )
            )
            within_capacity: list[Any] | None = None
            last_candidate: list[Any] = compacted
            for limit in (value for value in limits if value <= requested):
                summary = ModelRequest(
                    parts=[
                        UserPromptPart(
                            _compaction_manifest(
                                dropped,
                                evidence_records,
                                limit,
                            )
                        )
                    ],
                    metadata={"replaylabContextCompaction": True},
                )
                candidate = prefix + [summary] + live
                last_candidate = candidate
                candidate_tokens = _message_tokens(candidate)
                if candidate_tokens <= self.spec.target_tokens:
                    within_capacity = candidate
                    break
                if candidate_tokens <= self.spec.input_capacity:
                    within_capacity = candidate
            compacted = within_capacity or last_candidate

        after = _message_tokens(compacted)
        if after > self.spec.input_capacity:
            raise ContextOverflowError(
                "the specialist's immutable request and newest complete tool exchange exceed "
                "the selected model context window after atomic compaction"
            )
        self.generation = min(10000, self.generation + 1)
        if self.on_compaction is not None:
            self.on_compaction(self.generation, before, after)
        return compacted


__all__ = ["SpecialistHistoryCompactor"]
