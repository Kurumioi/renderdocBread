"""Provider-neutral contracts for bounded ReplayLab orchestration.

The selected model or managed Agent may propose a plan and produce specialist
answers, but ReplayLab owns scheduling, evidence admission, and final review.
This keeps the model-API and subscription paths on the same host-enforced
contract without enabling a vendor's ambient multi-agent implementation.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .errors import PolicyError
from .planning import TaskPlan
from .skills import Skill, SkillCatalog


_AcceptanceCriterion = Annotated[str, Field(min_length=1, max_length=500)]


class SpecialistField(BaseModel):
    """One provider-neutral field in a specialist's prompt contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=500)
    required: bool = True


class SpecialistDescriptor(BaseModel):
    """Advisory role and I/O contract for one isolated Skill specialist.

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
                ),
                SpecialistField(
                    name="frozen_context",
                    description="Capture-bound context supplied by ReplayLab as untrusted data.",
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
                ),
                SpecialistField(
                    name="unknowns",
                    description="Material uncertainty or missing evidence, when any remains.",
                    required=False,
                ),
            ),
            acceptance_criteria=(
                "Meet the selected Skill's host-enforced required-tool evidence contract.",
                "Keep observed evidence separate from hypotheses and leave capture state unchanged.",
            ),
        )

    def render_prompt_contract(self) -> str:
        """Render the structured contract for API and external Agent prompts."""

        def fields(values: tuple[SpecialistField, ...]) -> str:
            return "\n".join(
                f"- {item.name} ({'required' if item.required else 'optional'}): "
                f"{item.description}"
                for item in values
            )

        hints = []
        if self.model_hint:
            hints.append(f"Model capability hint: {self.model_hint}.")
        if self.reasoning_hint:
            hints.append(f"Reasoning hint: {self.reasoning_hint}.")
        hint_text = "\n".join(hints)
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria)
        return (
            f"Specialist role: {self.role}\n"
            f"Context policy: {self.context_policy}\n"
            + (hint_text + "\n" if hint_text else "")
            + f"Role instructions: {self.system_instructions}\n\n"
            f"Input schema:\n{fields(self.input_schema)}\n\n"
            f"Output schema:\n{fields(self.output_schema)}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )


_SHADER_SPECIALIST = SpecialistDescriptor(
    skill="renderdoc-shader-workbench",
    role="shader-reconstruction-specialist",
    system_instructions=(
        "Act as a shader ABI, intermediate-representation, and source-reconstruction "
        "specialist. Establish the exact shader identity before analysis; reconstruct from "
        "reflection and disassembly without presenting generated source as an original or "
        "validated decompilation. Report conflicts and unknowns instead of smoothing them over."
    ),
    model_hint="code-capable model with strong shader, compiler IR, and data-flow reasoning",
    reasoning_hint="high",
    input_schema=(
        SpecialistField(
            name="request",
            description="The original user goal and bounded shader subtask.",
        ),
        SpecialistField(
            name="frozen_shader_identity",
            description="Capture session, EID, stage, resource, entry point, and raw SHA-256.",
        ),
        SpecialistField(
            name="capture_evidence",
            description="Reflection, disassembly, bindings, and values returned by allowed tools.",
        ),
        SpecialistField(
            name="prerequisite_evidence",
            description="Validated dependency-task answers, when declared by the plan.",
            required=False,
        ),
    ),
    output_schema=(
        SpecialistField(
            name="bound_identity",
            description="The exact shader identity used for every conclusion.",
        ),
        SpecialistField(
            name="observed_evidence",
            description="Reflection, ABI, disassembly, control/data flow, and interface facts.",
        ),
        SpecialistField(
            name="inferences",
            description="Semantic hypotheses explicitly separated from captured facts.",
        ),
        SpecialistField(
            name="generated_reconstruction",
            description="Optional high-level pseudocode/source labelled as generated and unvalidated.",
            required=False,
        ),
        SpecialistField(
            name="consistency_report",
            description="ABI/interface/control-flow checks, conflicts, confidence, and unknowns.",
        ),
    ),
    acceptance_criteria=(
        "Both get_shader_info and reverse_shader must complete for the bound EID and stage.",
        "Reflection/ABI facts, inferred semantics, and generated source are visibly distinct.",
        "Generated declarations and interfaces are checked against captured bindings and layout.",
        "Unsupported constructs, ambiguity, conflicts, and confidence are reported explicitly.",
        "No compile, workspace, apply, rollback, export, or other mutation is attempted in v1.",
    ),
)


class SkillResult(BaseModel):
    """One specialist answer admitted only after deterministic evidence checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(pattern=r"^(?:task-[1-3]|stage-[1-6]-task-[1-3])$")
    skill: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=1024 * 1024)
    completed_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=40)
    evidence_validated: Literal[True]


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
    "SpecialistDescriptor",
    "SpecialistField",
    "SkillResult",
    "SynthesisResult",
]
