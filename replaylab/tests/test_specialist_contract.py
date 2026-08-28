from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from replaylab_agent.errors import PolicyError
from replaylab_agent.orchestration import (
    SpecialistAcceptanceCoverage,
    SpecialistDescriptor,
    SpecialistEvidenceReference,
    SpecialistFinding,
    SpecialistInput,
    SpecialistOutput,
    SpecialistTargetIdentity,
    SkillResult,
)


def _target() -> SpecialistTargetIdentity:
    return SpecialistTargetIdentity(
        capture_session_id="capture-session-1",
        snapshot_id="snapshot-1",
        event_id=9,
    )


def _completed_output() -> SpecialistOutput:
    evidence = SpecialistEvidenceReference(
        ref_id="evidence-pipeline",
        source="tool",
        tool="get_pipeline_state",
        tool_call_id="call-1",
        result_sha256="a" * 64,
        summary="Pipeline state captured for frozen EID 9.",
        target=_target(),
    )
    return SpecialistOutput(
        task_id="task-1",
        skill="renderdoc-explain-event",
        status="completed",
        answer="EID 9 writes one color target.",
        findings=(
            SpecialistFinding(
                finding_id="finding-color-write",
                claim="EID 9 writes one color target.",
                basis="observed",
                target=_target(),
                evidence_refs=(evidence.ref_id,),
                confidence="high",
            ),
        ),
        evidence=(evidence,),
        coverage=(
            SpecialistAcceptanceCoverage(
                criterion_id="criterion-1",
                status="passed",
                evidence_refs=(evidence.ref_id,),
                note="The required Skill evidence was admitted.",
            ),
            SpecialistAcceptanceCoverage(
                criterion_id="criterion-2",
                status="passed",
                evidence_refs=(evidence.ref_id,),
                note="The finding is marked observed and capture state is identified.",
            ),
        ),
        completed_tools=("get_pipeline_state",),
    )


def test_descriptor_validates_machine_readable_input_and_lifecycle_contract() -> None:
    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")
    envelope = SpecialistInput(
        task_id="task-1",
        skill=descriptor.skill,
        payload={
            "request": "Explain frozen EID 9",
            "frozen_context": {"eventId": 9, "snapshotId": "snapshot-1"},
        },
    )

    assert descriptor.validate_input(envelope) is envelope
    assert descriptor.validate_input(envelope.model_dump(mode="json")) == envelope
    assert descriptor.loop_policy.phases == (
        "plan",
        "collect-evidence",
        "synthesize",
        "validate",
    )
    assert descriptor.loop_policy.retry_scope == "failed-criteria-only"
    assert descriptor.lifecycle_policy.session_scope == "one-task"
    assert (
        descriptor.lifecycle_policy.release_condition
        == "host-confirmed-terminal-and-clean"
    )
    rendered = descriptor.render_prompt_contract()
    assert "replaylab.specialist-output/v1" in rendered
    assert "criterion-1" in rendered
    assert "release only after host-confirmed-terminal-and-clean" in rendered


def test_descriptor_rejects_missing_unknown_and_wrongly_typed_input_fields() -> None:
    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")

    with pytest.raises(PolicyError, match="missing required fields: frozen_context"):
        descriptor.validate_input(
            SpecialistInput(
                task_id="task-1",
                skill=descriptor.skill,
                payload={"request": "Explain EID 9"},
            )
        )
    with pytest.raises(PolicyError, match="undeclared fields: ambient_ui_state"):
        descriptor.validate_input(
            SpecialistInput(
                task_id="task-1",
                skill=descriptor.skill,
                payload={
                    "request": "Explain EID 9",
                    "frozen_context": {"eventId": 9},
                    "ambient_ui_state": "must not be read",
                },
            )
        )
    with pytest.raises(PolicyError, match="frozen_context.*object"):
        descriptor.validate_input(
            SpecialistInput(
                task_id="task-1",
                skill=descriptor.skill,
                payload={
                    "request": "Explain EID 9",
                    "frozen_context": "mutable current selection",
                },
            )
        )


def test_descriptor_accepts_only_host_admitted_evidence_and_complete_coverage() -> None:
    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")
    output = _completed_output()

    assert descriptor.validate_output(
        output,
        expected_task_id="task-1",
        admitted_evidence_refs=frozenset({"evidence-pipeline"}),
        admitted_tools=frozenset({"get_pipeline_state"}),
    ) is output
    assert descriptor.validate_output(
        output.model_dump(mode="json"),
        expected_task_id="task-1",
        admitted_evidence_refs=frozenset({"evidence-pipeline"}),
        admitted_tools=frozenset({"get_pipeline_state"}),
    ) == output

    with pytest.raises(PolicyError, match="evidence not admitted"):
        descriptor.validate_output(
            output,
            admitted_evidence_refs=frozenset(),
            admitted_tools=frozenset({"get_pipeline_state"}),
        )
    with pytest.raises(PolicyError, match="tools not admitted"):
        descriptor.validate_output(
            output,
            admitted_evidence_refs=frozenset({"evidence-pipeline"}),
            admitted_tools=frozenset(),
        )


def test_completed_output_cannot_skip_descriptor_acceptance_criteria() -> None:
    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")
    output = _completed_output().model_copy(
        update={"coverage": _completed_output().coverage[:1]}
    )

    with pytest.raises(PolicyError, match="missing acceptance coverage: criterion-2"):
        descriptor.validate_output(output)


def test_structured_output_rejects_dangling_evidence_references() -> None:
    with pytest.raises(ValidationError, match="unknown evidence"):
        SpecialistOutput(
            task_id="task-1",
            skill="renderdoc-explain-event",
            status="completed",
            answer="Unsupported claim",
            findings=(
                SpecialistFinding(
                    finding_id="finding-unsupported",
                    claim="Unsupported claim",
                    basis="inferred",
                    target=_target(),
                    evidence_refs=("evidence-missing",),
                    confidence="low",
                ),
            ),
        )


def test_skill_result_remains_legacy_compatible_and_checks_structured_identity() -> None:
    legacy = SkillResult(
        task_id="task-1",
        skill="renderdoc-explain-event",
        summary="Explain EID 9",
        answer="Legacy provider answer",
        completed_tools=("get_pipeline_state",),
        evidence_validated=True,
    )
    assert legacy.structured_output is None

    output = _completed_output()
    structured = SkillResult(
        task_id="task-1",
        skill=output.skill,
        summary="Explain EID 9",
        answer=output.answer,
        completed_tools=output.completed_tools,
        evidence_validated=True,
        structured_output=output,
    )
    assert structured.structured_output is output

    with pytest.raises(ValidationError, match="answer does not match"):
        SkillResult(
            task_id="task-1",
            skill=output.skill,
            summary="Explain EID 9",
            answer="Different legacy answer",
            completed_tools=output.completed_tools,
            evidence_validated=True,
            structured_output=output,
        )


@pytest.mark.asyncio
async def test_model_runtime_consumes_and_host_validates_structured_receipt(
    tmp_path, monkeypatch
) -> None:
    import pydantic_ai

    import replaylab_agent.tool_service as tool_service
    from replaylab_agent.events import TaskEventWriter
    from replaylab_agent.tool_service import ToolEvidenceRecord
    from replaylab_agent.profiles import Profile, ProfileStore
    from replaylab_agent.runtime import AgentRuntime
    from replaylab_agent.skills import SkillCatalog

    skill_root = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    catalog = SkillCatalog([skill_root]).load()
    skill = catalog.get("renderdoc-explain-event")
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "api",
            "ollama",
            "model-a",
            options={"context_window_tokens": 65_536},
            probed_capabilities=["tools", "json-schema"],
        )
    )
    runtime = AgentRuntime(
        store,
        catalog,
        event_root=tmp_path / "events",
        context_memory_root=tmp_path / "memory",
    )

    records = (
        ToolEvidenceRecord(
            "call-inspect-event",
            "inspect_event",
            "1" * 64,
            "2" * 64,
            "completed",
        ),
    )
    target = SpecialistTargetIdentity(
        capture_session_id="a" * 32,
        snapshot_id="b" * 64,
        event_id=9,
    )
    evidence = tuple(
        SpecialistEvidenceReference(
            ref_id=f"evidence-{index}",
            source="tool",
            tool=record.tool,
            tool_call_id=record.tool_call_id,
            result_sha256=record.result_sha256,
            summary=f"Host tool evidence from {record.tool}.",
            target=target,
        )
        for index, record in enumerate(records, start=1)
    )
    output = SpecialistOutput(
        task_id="task-1",
        skill=skill.name,
        status="completed",
        answer="The frozen event is a draw using one graphics pipeline.",
        findings=(
            SpecialistFinding(
                finding_id="finding-draw",
                claim="The frozen event is a draw using one graphics pipeline.",
                basis="observed",
                target=target,
                evidence_refs=tuple(item.ref_id for item in evidence),
                confidence="high",
            ),
        ),
        evidence=evidence,
        coverage=tuple(
            SpecialistAcceptanceCoverage(
                criterion_id=f"criterion-{index}",
                status="passed",
                evidence_refs=tuple(item.ref_id for item in evidence),
                note="Covered by the host-admitted event evidence.",
            )
            for index in range(1, 8)
        ),
        completed_tools=tuple(record.tool for record in records),
        details={
            "bound_scope": {
                "captureSessionId": "a" * 32,
                "eventIds": [9],
            },
            "event_findings": [
                {
                    "eventId": 9,
                    "basis": "observed",
                    "behavior": "draw using one graphics pipeline",
                }
            ],
            "resource_flow_graph": "flowchart LR\n  E9[\"EID 9 draw\"]",
        },
    )
    captured: dict[str, object] = {}

    def fake_toolset(_service, invocation):
        captured["audit"] = invocation.audit
        return object()

    class FakeResult:
        def __init__(self) -> None:
            self.output = output

    class FakeAgent:
        def __init__(self, _model, *, output_type, **_kwargs) -> None:
            assert output_type is SpecialistOutput

        async def run(self, _prompt, **_kwargs):
            assert "event_stream_handler" not in _kwargs
            audit = captured["audit"]
            audit.completed.extend(record.tool for record in records)
            audit.evidence.extend(records)
            return FakeResult()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(tool_service, "build_internal_toolset", fake_toolset)

    execution = await runtime._run_model_skill(
        runtime._profile("api"),
        "Explain frozen EID 9",
        skill,
        {
            "capture": {
                "path": "C:/capture.rdc",
                "captureSessionId": "a" * 32,
            },
            "events": {"currentEventId": 9},
            "snapshotId": "b" * 64,
        },
        TaskEventWriter(tmp_path / "events", task_id="structured-specialist"),
        None,
        model=object(),
        capture_path="C:/capture.rdc",
        instance_id="qrenderdoc-1",
    )

    assert execution.text == output.answer
    assert execution.structured_output == output
    receipt = SkillResult(
        task_id="task-1",
        skill=skill.name,
        summary="Explain frozen EID 9",
        answer=execution.text,
        completed_tools=execution.completed_tools,
        evidence_validated=True,
        structured_output=execution.structured_output,
    )
    assert receipt.structured_output is not None
    assert receipt.structured_output.coverage[0].status == "passed"


def test_runtime_rejects_structured_receipt_with_forged_tool_digest() -> None:
    from replaylab_agent.errors import EvidenceValidationError
    from replaylab_agent.mcp_tools import ToolEvidenceRecord
    from replaylab_agent.runtime import AgentRuntime

    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")
    output = _completed_output()
    record = ToolEvidenceRecord(
        "call-1",
        "get_pipeline_state",
        "a" * 64,
        "b" * 64,
        "completed",
    )

    with pytest.raises(EvidenceValidationError, match="does not match.*host audit"):
        AgentRuntime._validate_structured_specialist_output(
            descriptor,
            output,
            expected_task_id="task-1",
            completed_tools=output.completed_tools,
            evidence_records=(record,),
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"task_id": "task-2"}, "task identity"),
        ({"skill": "renderdoc-performance"}, "Skill identity"),
    ],
)
def test_runtime_rejects_structured_receipt_identity_drift(updates, message) -> None:
    from replaylab_agent.errors import EvidenceValidationError
    from replaylab_agent.mcp_tools import ToolEvidenceRecord
    from replaylab_agent.runtime import AgentRuntime

    descriptor = SpecialistDescriptor.generic("renderdoc-explain-event")
    output = _completed_output().model_copy(update=updates)
    record = ToolEvidenceRecord(
        "call-1",
        "get_pipeline_state",
        "f" * 64,
        "a" * 64,
        "completed",
    )

    with pytest.raises(EvidenceValidationError, match=message):
        AgentRuntime._validate_structured_specialist_output(
            descriptor,
            output,
            expected_task_id="task-1",
            completed_tools=output.completed_tools,
            evidence_records=(record,),
        )


def test_runtime_selects_native_schema_only_for_confirmed_profile_capability() -> None:
    from replaylab_agent.profiles import Profile
    from replaylab_agent.runtime import AgentRuntime

    text_profile = Profile(
        "text", "ollama", "model-a", probed_capabilities=["tools"]
    )
    schema_profile = Profile(
        "schema",
        "ollama",
        "model-a",
        probed_capabilities=["tools", "json-schema"],
    )

    assert AgentRuntime._specialist_output_type(text_profile) is str
    assert AgentRuntime._specialist_output_type(schema_profile) is SpecialistOutput


def test_runtime_maps_noncompleted_receipt_to_evidence_validation() -> None:
    from replaylab_agent.errors import EvidenceValidationError
    from replaylab_agent.runtime import AgentRuntime

    output = SpecialistOutput(
        task_id="task-1",
        skill="renderdoc-explain-event",
        status="blocked",
        answer="The required event evidence was unavailable.",
        unknowns=("The frozen capture no longer exposes the event.",),
    )

    with pytest.raises(EvidenceValidationError, match="terminal status 'blocked'"):
        AgentRuntime._validate_structured_specialist_output(
            SpecialistDescriptor.generic(output.skill),
            output,
            expected_task_id="task-1",
            completed_tools=(),
            evidence_records=(),
        )


def test_runtime_rejects_receipt_target_that_conflicts_with_frozen_event() -> None:
    from replaylab_agent.errors import EvidenceValidationError
    from replaylab_agent.mcp_tools import ToolEvidenceRecord
    from replaylab_agent.policy import ToolCallScope
    from replaylab_agent.runtime import AgentRuntime

    output = _completed_output()
    record = ToolEvidenceRecord(
        "call-1",
        "get_pipeline_state",
        "f" * 64,
        "a" * 64,
        "completed",
    )

    with pytest.raises(EvidenceValidationError, match="event_id 9 != 10"):
        AgentRuntime._validate_structured_specialist_output(
            SpecialistDescriptor.generic(output.skill),
            output,
            expected_task_id="task-1",
            completed_tools=output.completed_tools,
            evidence_records=(record,),
            scope=ToolCallScope(intent_kind="explain-current-event", event_id=10),
        )


def test_runtime_allows_a_general_frame_receipt_to_cover_other_frozen_capture_events() -> None:
    from replaylab_agent.policy import ToolCallScope
    from replaylab_agent.runtime import AgentRuntime

    target = _completed_output().findings[0].target
    assert target.event_id == 9
    assert (
        AgentRuntime._specialist_target_scope_conflict(
            target,
            ToolCallScope(event_id=10),
        )
        == ""
    )


def test_stage_task_identity_rebinding_keeps_skill_result_receipt_consistent() -> None:
    from replaylab_agent.runtime import AgentRuntime

    stage_output = _completed_output().model_copy(
        update={"task_id": "stage-1-task-1"}
    )
    task_output = AgentRuntime._rebind_specialist_output_task(stage_output, "task-1")
    assert task_output is not None
    task_result = SkillResult(
        task_id="task-1",
        skill=task_output.skill,
        summary="Explain event",
        answer=task_output.answer,
        completed_tools=task_output.completed_tools,
        evidence_validated=True,
        structured_output=task_output,
    )

    flattened_output = AgentRuntime._rebind_specialist_output_task(
        task_result.structured_output, "stage-1-task-1"
    )
    flattened_result = SkillResult(
        task_id="stage-1-task-1",
        skill=task_result.skill,
        summary=task_result.summary,
        answer=task_result.answer,
        completed_tools=task_result.completed_tools,
        evidence_validated=True,
        structured_output=flattened_output,
    )
    assert flattened_result.structured_output is not None
    assert flattened_result.structured_output.task_id == flattened_result.task_id
