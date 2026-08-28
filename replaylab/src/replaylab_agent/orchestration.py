"""Provider-neutral contracts for bounded ReplayLab orchestration.

The selected model or managed Agent may propose a plan and produce specialist
answers, but ReplayLab owns scheduling, evidence admission, and final review.
This keeps the model-API and subscription paths on the same host-enforced
contract without enabling a vendor's ambient multi-agent implementation.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import PolicyError
from .planning import TaskPlan
from .skills import Skill, SkillCatalog


_AcceptanceCriterion = Annotated[str, Field(min_length=1, max_length=500)]
_TaskId = Annotated[
    str, Field(pattern=r"^(?:task-[1-3]|stage-[1-6]-task-[1-3])$")
]
_NonEmptyText = Annotated[str, Field(min_length=1, max_length=4000)]


class SpecialistLoopPolicy(BaseModel):
    """Host-visible limits for a specialist's private evidence loop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phases: tuple[
        Literal["plan", "collect-evidence", "synthesize", "validate"], ...
    ] = (
        "plan",
        "collect-evidence",
        "synthesize",
        "validate",
    )
    max_attempts: int = Field(default=2, ge=1, le=3)
    retry_scope: Literal["failed-criteria-only"] = "failed-criteria-only"
    host_validation_required: Literal[True] = True
    terminal_statuses: tuple[Literal["completed", "blocked", "failed"], ...] = (
        "completed",
        "blocked",
        "failed",
    )


class SpecialistLifecyclePolicy(BaseModel):
    """Ownership and release rules for one isolated specialist session."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_scope: Literal["one-task"] = "one-task"
    context_retention: Literal["until-terminal"] = "until-terminal"
    cancellation_policy: Literal["cancel-and-confirm-cleanup"] = (
        "cancel-and-confirm-cleanup"
    )
    release_condition: Literal["host-confirmed-terminal-and-clean"] = (
        "host-confirmed-terminal-and-clean"
    )


class SpecialistTargetIdentity(BaseModel):
    """Immutable identity anchors used by a finding or evidence reference.

    The schema intentionally supports several RenderDoc evidence domains without
    treating the mutable UI selection as identity. At least one capture, event,
    resource, shader, or snapshot anchor is mandatory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capture_session_id: str | None = Field(default=None, min_length=1, max_length=256)
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    event_id: int | None = Field(default=None, ge=0)
    shader_stage: str | None = Field(default=None, min_length=1, max_length=32)
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)
    entry_point: str | None = Field(default=None, min_length=1, max_length=256)
    raw_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    subresource: str | None = Field(default=None, min_length=1, max_length=256)
    pixel: tuple[int, int] | None = None

    @model_validator(mode="after")
    def _require_stable_anchor(self) -> "SpecialistTargetIdentity":
        if not any(
            (
                self.capture_session_id,
                self.snapshot_id,
                self.event_id is not None,
                self.resource_id,
                self.raw_sha256,
            )
        ):
            raise ValueError("specialist target identity requires a stable anchor")
        if self.pixel is not None and any(value < 0 for value in self.pixel):
            raise ValueError("pixel coordinates must be non-negative")
        return self


class SpecialistEvidenceReference(BaseModel):
    """A bounded reference to evidence admitted by the host."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref_id: str = Field(pattern=r"^evidence-[a-z0-9][a-z0-9-]{0,62}$")
    source: Literal["tool", "prerequisite", "host-audit"]
    summary: str = Field(min_length=1, max_length=2000)
    target: SpecialistTargetIdentity
    tool: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$", max_length=128
    )
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def _validate_source_fields(self) -> "SpecialistEvidenceReference":
        if self.source == "tool" and not self.tool:
            raise ValueError("tool evidence requires a tool name")
        if self.source != "tool" and self.tool is not None:
            raise ValueError("only tool evidence may declare a tool name")
        return self


class SpecialistFinding(BaseModel):
    """One evidence-addressable claim produced by a specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_id: str = Field(pattern=r"^finding-[a-z0-9][a-z0-9-]{0,62}$")
    claim: str = Field(min_length=1, max_length=8000)
    basis: Literal["observed", "inferred"]
    target: SpecialistTargetIdentity
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=40)
    confidence: Literal["low", "medium", "high"]
    limitations: tuple[_NonEmptyText, ...] = Field(default_factory=tuple, max_length=20)


class SpecialistAcceptanceCoverage(BaseModel):
    """Machine-readable coverage for one descriptor acceptance criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    criterion_id: str = Field(pattern=r"^criterion-[1-9][0-9]?$")
    status: Literal["passed", "not-met", "not-applicable"]
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=40)
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _require_pass_evidence(self) -> "SpecialistAcceptanceCoverage":
        if self.status == "passed" and not self.evidence_refs:
            raise ValueError("passed acceptance coverage requires evidence references")
        return self


class SpecialistInput(BaseModel):
    """Provider-neutral, machine-checkable specialist task envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["replaylab.specialist-input/v1"] = (
        "replaylab.specialist-input/v1"
    )
    task_id: _TaskId
    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    payload: dict[str, Any]


class SpecialistOutput(BaseModel):
    """Evidence-grounded structured output owned by a specialist.

    This structure does not itself admit evidence. The host must additionally
    call :meth:`SpecialistDescriptor.validate_output` with the evidence IDs it
    actually admitted before setting ``SkillResult.evidence_validated``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["replaylab.specialist-output/v1"] = (
        "replaylab.specialist-output/v1"
    )
    task_id: _TaskId
    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    status: Literal["completed", "blocked", "failed"]
    answer: str = Field(min_length=1, max_length=1024 * 1024)
    findings: tuple[SpecialistFinding, ...] = Field(default_factory=tuple, max_length=100)
    evidence: tuple[SpecialistEvidenceReference, ...] = Field(
        default_factory=tuple, max_length=100
    )
    unknowns: tuple[_NonEmptyText, ...] = Field(default_factory=tuple, max_length=40)
    coverage: tuple[SpecialistAcceptanceCoverage, ...] = Field(
        default_factory=tuple, max_length=40
    )
    completed_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=40)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_internal_references(self) -> "SpecialistOutput":
        evidence_ids = [item.ref_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("specialist evidence reference IDs must be unique")
        finding_ids = [item.finding_id for item in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("specialist finding IDs must be unique")
        criterion_ids = [item.criterion_id for item in self.coverage]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("specialist acceptance criterion IDs must be unique")

        known_refs = set(evidence_ids)
        referenced = {
            ref_id for finding in self.findings for ref_id in finding.evidence_refs
        } | {
            ref_id for coverage in self.coverage for ref_id in coverage.evidence_refs
        }
        unknown_refs = referenced - known_refs
        if unknown_refs:
            raise ValueError(
                "specialist output references unknown evidence: "
                + ", ".join(sorted(unknown_refs))
            )

        completed_tools = set(self.completed_tools)
        evidence_tools = {
            item.tool for item in self.evidence if item.source == "tool" and item.tool
        }
        if evidence_tools - completed_tools:
            raise ValueError(
                "tool evidence was not declared in completed_tools: "
                + ", ".join(sorted(evidence_tools - completed_tools))
            )
        if self.status == "completed" and not self.findings:
            raise ValueError("completed specialist output requires at least one finding")
        if self.status in {"blocked", "failed"} and not self.unknowns:
            raise ValueError(f"{self.status} specialist output requires unknowns")
        return self


class SpecialistField(BaseModel):
    """One provider-neutral field in a specialist's prompt contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    value_type: Literal[
        "any",
        "string",
        "string-list",
        "object",
        "array",
        "boolean",
        "integer",
        "number",
    ] = "any"


class SpecialistDescriptor(BaseModel):
    """Role, I/O, loop, and lifecycle contract for an isolated specialist.

    The descriptor deliberately contains no provider command or Agent SDK type.
    A provider adapter may use its model/reasoning hints when it can do so
    without overriding the user's selected model. ReplayLab's tool grant and
    deterministic evidence validator remain authoritative either way.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    role: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=96)
    context_policy: Literal["isolated-per-task"] = "isolated-per-task"
    system_instructions: str = Field(min_length=1, max_length=8000)
    model_hint: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning_hint: Literal["low", "medium", "high", "xhigh"] | None = None
    input_schema: tuple[SpecialistField, ...] = Field(min_length=1, max_length=16)
    output_schema: tuple[SpecialistField, ...] = Field(min_length=1, max_length=16)
    acceptance_criteria: tuple[_AcceptanceCriterion, ...] = Field(
        min_length=1, max_length=16
    )
    output_contract: Literal["replaylab.specialist-output/v1"] = (
        "replaylab.specialist-output/v1"
    )
    loop_policy: SpecialistLoopPolicy = Field(default_factory=SpecialistLoopPolicy)
    lifecycle_policy: SpecialistLifecyclePolicy = Field(
        default_factory=SpecialistLifecyclePolicy
    )

    @model_validator(mode="after")
    def _validate_declared_field_names(self) -> "SpecialistDescriptor":
        for label, fields in (
            ("input", self.input_schema),
            ("output", self.output_schema),
        ):
            names = [item.name for item in fields]
            if len(names) != len(set(names)):
                raise ValueError(f"specialist {label} field names must be unique")
        return self

    @classmethod
    def generic(cls, skill_name: str) -> "SpecialistDescriptor":
        return cls(
            skill=skill_name,
            role="renderdoc-skill-specialist",
            system_instructions=(
                "Perform only the selected bounded Skill in a fresh context. Preserve the "
                "host evidence boundary and do not delegate to another Agent."
            ),
            input_schema=(
                SpecialistField(
                    name="request",
                    description="The original request and bounded task summary.",
                    value_type="string",
                ),
                SpecialistField(
                    name="frozen_context",
                    description="Capture-bound context supplied by ReplayLab as untrusted data.",
                    value_type="object",
                ),
                SpecialistField(
                    name="prerequisite_evidence",
                    description="Validated answers from declared dependency tasks, when present.",
                    required=False,
                ),
            ),
            output_schema=(
                SpecialistField(
                    name="answer",
                    description="A concise answer whose factual claims are tied to tool evidence.",
                    value_type="string",
                ),
                SpecialistField(
                    name="unknowns",
                    description="Material uncertainty or missing evidence, when any remains.",
                    required=False,
                    value_type="string-list",
                ),
            ),
            acceptance_criteria=(
                "Meet the selected Skill's host-enforced required-tool evidence contract.",
                "Keep observed evidence separate from hypotheses and leave capture state unchanged.",
            ),
        )

    @staticmethod
    def _matches_value_type(value: Any, value_type: str) -> bool:
        if value_type == "any":
            return True
        if value_type == "string":
            return isinstance(value, str)
        if value_type == "string-list":
            return isinstance(value, (list, tuple)) and all(
                isinstance(item, str) for item in value
            )
        if value_type == "object":
            return isinstance(value, Mapping)
        if value_type == "array":
            return isinstance(value, (list, tuple))
        if value_type == "boolean":
            return isinstance(value, bool)
        if value_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if value_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return False  # pragma: no cover - protected by SpecialistField

    @classmethod
    def _validate_declared_payload(
        cls,
        payload: Mapping[str, Any],
        fields: tuple[SpecialistField, ...],
        *,
        label: str,
    ) -> None:
        declared = {item.name: item for item in fields}
        unknown = set(payload) - set(declared)
        if unknown:
            raise PolicyError(
                f"specialist {label} contains undeclared fields: "
                + ", ".join(sorted(unknown))
            )
        missing = [
            item.name
            for item in fields
            if item.required and item.name not in payload
        ]
        if missing:
            raise PolicyError(
                f"specialist {label} is missing required fields: "
                + ", ".join(missing)
            )
        for name, value in payload.items():
            field = declared[name]
            if not cls._matches_value_type(value, field.value_type):
                raise PolicyError(
                    f"specialist {label} field '{name}' must be {field.value_type}"
                )

    def validate_input(
        self, value: SpecialistInput | Mapping[str, Any]
    ) -> SpecialistInput:
        """Validate one task envelope against this descriptor's declared input."""

        envelope = (
            value
            if isinstance(value, SpecialistInput)
            else SpecialistInput.model_validate_json(json.dumps(value))
        )
        if envelope.skill != self.skill:
            raise PolicyError(
                "specialist input identity does not match descriptor: "
                f"{envelope.skill} != {self.skill}"
            )
        self._validate_declared_payload(
            envelope.payload, self.input_schema, label="input"
        )
        return envelope

    def validate_output(
        self,
        value: SpecialistOutput | Mapping[str, Any],
        *,
        expected_task_id: str | None = None,
        admitted_evidence_refs: frozenset[str] | None = None,
        admitted_tools: frozenset[str] | None = None,
    ) -> SpecialistOutput:
        """Validate structured output and descriptor acceptance coverage.

        ``admitted_evidence_refs`` and ``admitted_tools`` should be supplied by
        the host evidence ledger. Their presence turns model-authored references
        into a deterministic admission check instead of trusting their labels.
        """

        output = (
            value
            if isinstance(value, SpecialistOutput)
            else SpecialistOutput.model_validate_json(json.dumps(value))
        )
        if output.skill != self.skill:
            raise PolicyError(
                "specialist output identity does not match descriptor: "
                f"{output.skill} != {self.skill}"
            )
        if expected_task_id is not None and output.task_id != expected_task_id:
            raise PolicyError(
                "specialist output task identity does not match assignment: "
                f"{output.task_id} != {expected_task_id}"
            )

        reserved = {"answer", "unknowns"} & set(output.details)
        if reserved:
            raise PolicyError(
                "specialist output details duplicate common fields: "
                + ", ".join(sorted(reserved))
            )
        declared_output_names = {item.name for item in self.output_schema}
        semantic_payload: dict[str, Any] = dict(output.details)
        if "answer" in declared_output_names:
            semantic_payload["answer"] = output.answer
        if "unknowns" in declared_output_names and output.unknowns:
            semantic_payload["unknowns"] = output.unknowns
        self._validate_declared_payload(
            semantic_payload, self.output_schema, label="output"
        )

        evidence_ids = {item.ref_id for item in output.evidence}
        if admitted_evidence_refs is not None:
            unadmitted = evidence_ids - admitted_evidence_refs
            if unadmitted:
                raise PolicyError(
                    "specialist output cites evidence not admitted by the host: "
                    + ", ".join(sorted(unadmitted))
                )
        if admitted_tools is not None:
            unadmitted_tools = set(output.completed_tools) - admitted_tools
            if unadmitted_tools:
                raise PolicyError(
                    "specialist output declares tools not admitted by the host: "
                    + ", ".join(sorted(unadmitted_tools))
                )

        expected_criteria = {
            f"criterion-{index}"
            for index in range(1, len(self.acceptance_criteria) + 1)
        }
        coverage = {item.criterion_id: item for item in output.coverage}
        unexpected_criteria = set(coverage) - expected_criteria
        if unexpected_criteria:
            raise PolicyError(
                "specialist output covers unknown acceptance criteria: "
                + ", ".join(sorted(unexpected_criteria))
            )
        if output.status == "completed":
            missing_criteria = expected_criteria - set(coverage)
            if missing_criteria:
                raise PolicyError(
                    "completed specialist output is missing acceptance coverage: "
                    + ", ".join(sorted(missing_criteria))
                )
            not_passed = [
                criterion_id
                for criterion_id, item in coverage.items()
                if item.status != "passed"
            ]
            if not_passed:
                raise PolicyError(
                    "completed specialist output has unmet acceptance criteria: "
                    + ", ".join(sorted(not_passed))
                )
        return output

    def render_prompt_contract(self) -> str:
        """Render the structured contract for API and external Agent prompts."""

        def fields(values: tuple[SpecialistField, ...]) -> str:
            return "\n".join(
                f"- {item.name} ({'required' if item.required else 'optional'}, "
                f"{item.value_type}): "
                f"{item.description}"
                for item in values
            )

        hints = []
        if self.model_hint:
            hints.append(f"Model capability hint: {self.model_hint}.")
        if self.reasoning_hint:
            hints.append(f"Reasoning hint: {self.reasoning_hint}.")
        hint_text = "\n".join(hints)
        criteria = "\n".join(
            f"- criterion-{index}: {item}"
            for index, item in enumerate(self.acceptance_criteria, start=1)
        )
        return (
            f"Specialist role: {self.role}\n"
            f"Context policy: {self.context_policy}\n"
            f"Structured output contract: {self.output_contract}\n"
            f"Private loop: {' -> '.join(self.loop_policy.phases)}; "
            f"at most {self.loop_policy.max_attempts} attempts; retries address only "
            "failed criteria.\n"
            f"Lifecycle: {self.lifecycle_policy.session_scope}; release only after "
            f"{self.lifecycle_policy.release_condition}.\n"
            + (hint_text + "\n" if hint_text else "")
            + f"Role instructions: {self.system_instructions}\n\n"
            "Structured receipt fields: contract_version, task_id, skill, status, answer, "
            "findings, evidence, unknowns, coverage, completed_tools, and details. Each finding "
            "contains finding_id, claim, basis (observed or inferred), target, evidence_refs, "
            "confidence, and limitations. Each evidence item contains ref_id, source, summary, "
            "target, and optional tool/tool_call_id/result_sha256. Each coverage item contains "
            "criterion_id, status, evidence_refs, and note. Output fields named answer or "
            "unknowns use their common receipt fields; put every other domain-specific output "
            "field declared below inside details.\n\n"
            f"Input schema:\n{fields(self.input_schema)}\n\n"
            f"Output schema:\n{fields(self.output_schema)}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )


_EVENT_SPECIALIST = SpecialistDescriptor(
    skill="renderdoc-explain-event",
    role="event-pipeline-specialist",
    system_instructions=(
        "Act as a capture-bound EID, pipeline, pass, and frame-flow specialist. "
        "Choose the narrowest scope that answers the request, derive every behavior label "
        "and graph edge from admitted event/resource/pipeline evidence, and distinguish "
        "fixed-program classification from AIProvisional semantics. When shader semantics "
        "are necessary, consume a declared shader-specialist dependency or return a bounded "
        "delegation request to the supervisor; never manufacture shader behavior. The current "
        "Host can publish ReplayLab's foldable semantic-pass panel but cannot rewrite native "
        "RenderDoc Event Browser names or captured marker hierarchy, so never claim that effect."
    ),
    model_hint="graphics-debugging model with RenderDoc pipeline and resource-flow reasoning",
    reasoning_hint="high",
    input_schema=(
        SpecialistField(
            name="request",
            description="The original user goal and bounded EID/pass/frame subtask.",
            value_type="string",
        ),
        SpecialistField(
            name="frozen_event_scope",
            description=(
                "Capture session, snapshot, exact EID or bounded EID set, and requested scope."
            ),
            value_type="object",
        ),
        SpecialistField(
            name="capture_evidence",
            description="Admitted action, pipeline, resource, debug, and pass-classification evidence.",
            value_type="array",
        ),
        SpecialistField(
            name="requested_effects",
            description="Requested pass-panel publication or AIProvisional semantic overlays.",
            required=False,
            value_type="array",
        ),
        SpecialistField(
            name="prerequisite_evidence",
            description="Validated shader-specialist or earlier event-task results.",
            required=False,
            value_type="array",
        ),
    ),
    output_schema=(
        SpecialistField(
            name="bound_scope",
            description="The exact capture/session and EID/pass/frame scope analyzed.",
            value_type="object",
        ),
        SpecialistField(
            name="event_findings",
            description="Per-EID inputs, outputs, pipeline state, behavior, basis, and confidence.",
            value_type="array",
        ),
        SpecialistField(
            name="resource_flow_graph",
            description="A Mermaid or compact graph with evidence-addressable nodes and edges.",
            value_type="string",
        ),
        SpecialistField(
            name="shader_delegation_requests",
            description="Bounded EID/stage/shader questions still requiring the shader specialist.",
            required=False,
            value_type="array",
        ),
        SpecialistField(
            name="ui_organization",
            description="Fixed classification, provisional-label, panel-publication, and limitation receipt.",
            required=False,
            value_type="object",
        ),
        SpecialistField(
            name="unknowns",
            description="Missing evidence or unresolved behavior that must not be guessed.",
            required=False,
            value_type="string-list",
        ),
    ),
    acceptance_criteria=(
        "The exact capture session and analyzed EID set are bound to admitted Host evidence.",
        "Each relevant draw/dispatch records evidence-backed inputs, outputs, and behavior or an explicit unknown.",
        "Observed facts, fixed-program classification, AIProvisional semantics, and hypotheses remain distinct.",
        "Every flow-graph edge names an observed resource or stage handoff rather than mere event adjacency.",
        "Shader-dependent claims use validated shader-specialist evidence or produce a bounded delegation request.",
        "Any pass-panel publication preserves the fixed classification identity and reports the actual UI receipt.",
        "Native EID names or marker hierarchy are never reported as changed without a real Host mutation receipt.",
    ),
)


_SHADER_SPECIALIST = SpecialistDescriptor(
    skill="renderdoc-shader-workbench",
    role="shader-lifecycle-specialist",
    system_instructions=(
        "Act as a shader ABI, intermediate-representation, HLSL reconstruction, and managed "
        "shader-lifecycle specialist. Establish and revalidate the exact shader identity; "
        "separate captured evidence, inferred semantics, and generated HLSL; run only the "
        "analysis, organization, compile, replacement, rollback, or project-registration stages "
        "requested by the user. An effectful completion requires its preflight, validation, "
        "postcondition, and recovery evidence. Never present generated HLSL as original source "
        "or claim semantic equivalence that the available checks did not prove."
    ),
    model_hint="code-capable model with strong shader, compiler IR, and data-flow reasoning",
    reasoning_hint="high",
    input_schema=(
        SpecialistField(
            name="request",
            description="The original user goal and bounded shader subtask.",
            value_type="string",
        ),
        SpecialistField(
            name="frozen_shader_identity",
            description="Capture session, EID, stage, resource, entry point, and raw SHA-256.",
            value_type="object",
        ),
        SpecialistField(
            name="capture_evidence",
            description="Reflection, disassembly, bindings, and values returned by allowed tools.",
            value_type="array",
        ),
        SpecialistField(
            name="prerequisite_evidence",
            description="Validated dependency-task answers, when declared by the plan.",
            required=False,
            value_type="array",
        ),
        SpecialistField(
            name="requested_stages",
            description="Analysis, HLSL organization, compile, apply, rollback, or project reuse stages.",
            required=False,
            value_type="array",
        ),
    ),
    output_schema=(
        SpecialistField(
            name="bound_identity",
            description="The exact shader identity used for every conclusion.",
            value_type="object",
        ),
        SpecialistField(
            name="observed_evidence",
            description="Reflection, ABI, disassembly, control/data flow, and interface facts.",
            value_type="array",
        ),
        SpecialistField(
            name="inferences",
            description="Semantic hypotheses explicitly separated from captured facts.",
            value_type="array",
        ),
        SpecialistField(
            name="generated_hlsl",
            description="Optional readable HLSL labelled with generated provenance and validation status.",
            required=False,
            value_type="string",
        ),
        SpecialistField(
            name="algorithm_explanation",
            description="Evidence-traced control/data-flow explanation with confidence and limitations.",
            value_type="string",
        ),
        SpecialistField(
            name="validation_report",
            description="Compiler/container/ABI/interface checks run, not run, passed, and failed.",
            value_type="object",
        ),
        SpecialistField(
            name="effect_and_recovery_report",
            description="Optional workspace/apply/postcondition/rollback transaction receipt.",
            required=False,
            value_type="object",
        ),
        SpecialistField(
            name="project_library_receipt",
            description="Optional registered generated artifact, identity, provenance, and validation status.",
            required=False,
            value_type="object",
        ),
        SpecialistField(
            name="unknowns",
            description="Unsupported instructions, ambiguous semantics, or missing validation evidence.",
            required=False,
            value_type="string-list",
        ),
    ),
    acceptance_criteria=(
        "The capture session, EID, stage, shader resource, entry point, and raw hash are bound and revalidated before each requested effectful stage.",
        "Reflection/ABI facts, inferred semantics, generated HLSL, and original-source provenance are visibly distinct.",
        "Generated HLSL declarations, bindings, interfaces, control/data flow, and algorithm claims are checked against admitted capture evidence.",
        "The validation report truthfully records every compiler/container/ABI/interface check as run, not run, passed, or failed.",
        "Any replacement records candidate and original identity, exact scope, successful preflight, validation, postcondition, and a matching rollback route; failed postconditions are rolled back and reported.",
        "Any project registration records the generated artifact, capture/workspace identity, provenance, and validation status without calling it original source.",
        "Unsupported constructs, ambiguity, conflicts, failed checks, and remaining unknowns are reported explicitly rather than smoothed over.",
    ),
)


class SkillResult(BaseModel):
    """One specialist answer admitted only after deterministic evidence checks.

    ``structured_output`` is optional during the v1 migration so existing
    provider adapters remain compatible. When present, its identity, terminal
    status, answer, and tool ledger must agree with the legacy fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: _TaskId
    skill: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=1024 * 1024)
    completed_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=40)
    evidence_validated: Literal[True]
    structured_output: SpecialistOutput | None = None

    @model_validator(mode="after")
    def _validate_structured_compatibility(self) -> "SkillResult":
        output = self.structured_output
        if output is None:
            return self
        if output.task_id != self.task_id or output.skill != self.skill:
            raise ValueError("structured specialist output identity does not match SkillResult")
        if output.status != "completed":
            raise ValueError("an admitted SkillResult requires completed structured output")
        if output.answer != self.answer:
            raise ValueError("structured specialist answer does not match SkillResult")
        if set(output.completed_tools) != set(self.completed_tools):
            raise ValueError("structured specialist tool ledger does not match SkillResult")
        return self


class SynthesisResult(BaseModel):
    """Final answer accepted by the host-side reviewer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    answer: str = Field(min_length=1, max_length=1024 * 1024)
    accepted_task_ids: tuple[str, ...] = Field(min_length=1, max_length=12)
    strategy: str = Field(min_length=1, max_length=128)
    evidence_validated: Literal[True] = True


@runtime_checkable
class OrchestrationStrategy(Protocol):
    """Pluggable scheduling/review seam for future multi-agent topologies."""

    name: str

    def specialist_descriptor(self, skill: Skill) -> SpecialistDescriptor:
        """Return one provider-neutral, isolated role contract for ``skill``."""

    def can_run_parallel(self, plan: TaskPlan, catalog: SkillCatalog) -> bool:
        """Return whether specialists may run concurrently."""

    def review_and_synthesize(
        self, plan: TaskPlan, results: list[SkillResult]
    ) -> SynthesisResult:
        """Reject an incomplete result set or synthesize its accepted answers."""


class BoundedSupervisorSpecialistReviewer:
    """Default deterministic supervisor-specialist-reviewer strategy.

    The supervisor is the bounded :class:`TaskPlan`; each task is executed by a
    temporary skill specialist; this reviewer admits only an exact, evidence-
    validated result set.  A later strategy can add dedicated model-backed
    supervisors or reviewers behind this interface, while the same deterministic
    evidence gate remains mandatory.
    """

    name = "bounded-supervisor-specialist-reviewer/v1"

    def specialist_descriptor(self, skill: Skill) -> SpecialistDescriptor:
        if skill.name == _EVENT_SPECIALIST.skill:
            return _EVENT_SPECIALIST
        if skill.name == _SHADER_SPECIALIST.skill:
            return _SHADER_SPECIALIST
        return SpecialistDescriptor.generic(skill.name)

    def can_run_parallel(self, plan: TaskPlan, catalog: SkillCatalog) -> bool:
        return plan.can_run_parallel(catalog)

    def review_and_synthesize(
        self, plan: TaskPlan, results: list[SkillResult]
    ) -> SynthesisResult:
        expected = {task.id: task for task in plan.tasks}
        observed: dict[str, SkillResult] = {}
        for result in results:
            if result.task_id in observed:
                raise PolicyError(f"duplicate specialist result: {result.task_id}")
            task = expected.get(result.task_id)
            if task is None:
                raise PolicyError(f"unexpected specialist result: {result.task_id}")
            if result.skill != task.skill or result.summary != task.summary:
                raise PolicyError(
                    f"specialist result identity does not match plan task: {result.task_id}"
                )
            # Literal[True] is also enforced by Pydantic. Keep the explicit host
            # check so alternative SkillResult implementations cannot weaken it.
            if result.evidence_validated is not True:
                raise PolicyError(
                    f"specialist result lacks validated evidence: {result.task_id}"
                )
            observed[result.task_id] = result

        missing = [task.id for task in plan.tasks if task.id not in observed]
        if missing:
            raise PolicyError("missing specialist results: " + ", ".join(missing))

        ordered = [observed[task.id] for task in plan.tasks]
        if len(ordered) == 1:
            answer = ordered[0].answer
        else:
            answer = "\n\n".join(
                f"## {result.summary}\n\n{result.answer}" for result in ordered
            )
        return SynthesisResult(
            answer=answer,
            accepted_task_ids=tuple(result.task_id for result in ordered),
            strategy=self.name,
            evidence_validated=True,
        )


__all__ = [
    "BoundedSupervisorSpecialistReviewer",
    "OrchestrationStrategy",
    "SpecialistAcceptanceCoverage",
    "SpecialistDescriptor",
    "SpecialistEvidenceReference",
    "SpecialistField",
    "SpecialistFinding",
    "SpecialistInput",
    "SpecialistLifecyclePolicy",
    "SpecialistLoopPolicy",
    "SpecialistOutput",
    "SpecialistTargetIdentity",
    "SkillResult",
    "SynthesisResult",
]
