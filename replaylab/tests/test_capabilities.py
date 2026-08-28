from __future__ import annotations

from pathlib import Path

import pytest

import replaylab_agent.capabilities as capabilities
from replaylab_agent.adaptive_orchestration import (
    ClarificationOption,
    GoalSpec,
    InitialAskUserDecision,
    InitialBlockedDecision,
    InitialSupervisorProposal,
    admit_initial_decision,
)
from replaylab_agent.capabilities import build_capability_matrix, profile_tool_support
from replaylab_agent.planning import PlannedTask, TaskPlan
from replaylab_agent.profiles import Profile
from replaylab_agent.skills import Skill, SkillCatalog


def _catalog(
    name: str = "renderdoc-shader-workbench",
    *,
    tools: set[str] | frozenset[str] = frozenset({"inspect_shader"}),
    category: str = "shader",
) -> SkillCatalog:
    catalog = SkillCatalog([])
    skill = Skill(
        name=name,
        description=f"Test capability for {name}",
        path=Path(f"/{name}/SKILL.md"),
        instructions="Use only admitted evidence.",
        allowed_tools=frozenset(tools),
        metadata={
            "replaylab-category": category,
            "replaylab-read-only": "false",
        },
    )
    catalog.skills = {name: skill}
    return catalog


def _goal(objective: str = "Explain the selected EID") -> GoalSpec:
    return GoalSpec(
        objective=objective,
        deliverables=["A grounded answer"],
        constraints=["Do not invent capture evidence"],
        success_criteria=["Every capture claim has admitted evidence"],
    )


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (Profile("unprobed", "openai", "model"), "unknown"),
        (
            Profile(
                "probed-supported",
                "openai",
                "model",
                probed_capabilities=["text", "tools"],
            ),
            "supported",
        ),
        (
            Profile(
                "probed-unsupported",
                "openai",
                "model",
                probed_capabilities=["text"],
            ),
            "unsupported",
        ),
        (Profile("explicit-supported", "openai", "model", options={"supports_tools": True}), "supported"),
        (Profile("agent-contract", "codex", "agent"), "supported"),
        (Profile("agent-disabled", "codex", "agent", options={"supports_tools": False}), "unsupported"),
    ],
)
def test_profile_tool_support_is_tri_state_and_does_not_deny_unprobed_models(
    profile: Profile, expected: str
) -> None:
    assert profile_tool_support(profile) == expected


def test_unknown_model_and_partial_transport_remain_attemptable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_shader"})
    )
    catalog = _catalog(
        tools={"inspect_shader", "decompile_shader_workspace"}
    )

    matrix = build_capability_matrix(
        Profile("unprobed", "openai", "model"),
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    )
    shader = matrix.skill("renderdoc-shader-workbench")

    assert matrix.tool_support == "unknown"
    assert matrix.hard_blockers == ()
    assert matrix.has_attemptable_specialist
    assert shader is not None
    assert shader.state == "conditional"
    assert shader.attemptable
    assert shader.internal_tools == ("inspect_shader",)
    assert shader.unavailable_tools == ("decompile_shader_workspace",)
    assert shader.can_attempt_effects([])
    assert not shader.can_attempt_effects(["workspace"])

    model_view = matrix.as_dict()
    assert "sidecar_path" not in model_view
    assert "sidecarPath" not in model_view
    assert model_view["skills"][0]["attemptable"] is True


def test_effect_outcomes_are_available_only_when_a_transport_can_attempt_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_shader"})
    )
    catalog = _catalog(
        tools={
            "inspect_shader",
            "decompile_shader_workspace",
            "apply_shader_workspace_candidate",
        }
    )
    profile = Profile(
        "tool-model", "openai", "model", options={"supports_tools": True}
    )

    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_shader"})
    )
    direct_only = build_capability_matrix(
        profile,
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    ).skill("renderdoc-shader-workbench")
    assert direct_only is not None
    assert direct_only.effect_classes == ("replay", "workspace")
    assert direct_only.available_effect_classes == ()
    assert direct_only.unavailable_effect_classes == ("replay", "workspace")

    monkeypatch.setattr(
        capabilities,
        "available_internal_tools",
        lambda: frozenset(
            {
                "inspect_shader",
                "decompile_shader_workspace",
                "apply_shader_workspace_candidate",
            }
        ),
    )
    with_effect_tools = build_capability_matrix(
        profile,
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    ).skill("renderdoc-shader-workbench")
    assert with_effect_tools is not None
    assert with_effect_tools.state == "ready"
    assert with_effect_tools.available_effect_classes == ("replay", "workspace")
    assert with_effect_tools.unavailable_effect_classes == ()
    assert with_effect_tools.can_attempt_effects(["workspace", "replay"])


def test_acp_uses_one_task_filtered_sidecar_for_the_complete_skill_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_shader"})
    )
    monkeypatch.setattr(
        capabilities, "find_replaylab_mcp", lambda _value=None: Path("C:/ReplayLab/host.exe")
    )
    catalog = _catalog(
        tools={"inspect_shader", "decompile_shader_workspace"}
    )

    matrix = build_capability_matrix(
        Profile("tool-model", "openai", "model", options={"supports_tools": True}),
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
        enable_sidecar=True,
    )
    shader = matrix.skill("renderdoc-shader-workbench")

    assert matrix.sidecar_transport_state == "ready"
    assert matrix.hard_blockers == ()
    assert shader is not None
    assert shader.state == "ready"
    assert shader.internal_tools == ()
    assert shader.sidecar_tools == (
        "decompile_shader_workspace",
        "inspect_shader",
    )
    assert shader.available_effect_classes == ("workspace",)
    assert shader.can_attempt_effects(["workspace"])


def test_only_host_facts_become_hard_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_event"})
    )
    catalog = _catalog(
        "renderdoc-explain-event", tools={"inspect_event"}, category="event-pipeline"
    )

    matrix = build_capability_matrix(
        Profile(
            "no-tools",
            "openai",
            "model",
            options={"supports_tools": False},
        ),
        catalog,
        {"capture": {"loaded": False}},
        "",
    )

    assert set(matrix.hard_blockers) == {
        "profile-tools-unsupported",
        "capture-not-loaded",
        "instance-not-pinned",
    }
    assert matrix.internal_tool_service_ready
    event = matrix.skill("renderdoc-explain-event")
    assert event is not None and event.state == "unavailable"


@pytest.mark.parametrize("model_action", ["blocked", "ask_user"])
def test_host_reroutes_model_authored_permission_dead_ends_to_specialist(
    monkeypatch: pytest.MonkeyPatch, model_action: str
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_event"})
    )
    catalog = _catalog(
        "renderdoc-explain-event", tools={"inspect_event"}, category="event-pipeline"
    )
    matrix = build_capability_matrix(
        Profile("unprobed", "openai", "model"),
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    )
    if model_action == "blocked":
        decision = InitialBlockedDecision(
            goal=_goal(),
            reason="I do not have permission to inspect the event.",
            answer="Enable the tool first.",
            blocker_codes=[],
        )
    else:
        decision = InitialAskUserDecision(
            goal=_goal(),
            reason="The capability grant is unavailable.",
            question="Would you like to enable the tool permission?",
            options=[
                ClarificationOption(
                    id="enable_tool",
                    label="Enable tool",
                    description="Enable a permission before inspecting the EID.",
                ),
                ClarificationOption(
                    id="stop",
                    label="Stop",
                    description="Do not inspect the selected EID.",
                ),
            ],
        )

    admitted = admit_initial_decision(
        decision,
        request="Explain this EID",
        context={"capture": {"loaded": True}},
        catalog=catalog,
        matrix=matrix,
    )

    assert isinstance(admitted, InitialSupervisorProposal)
    assert admitted.plan.tasks[0].skill == "renderdoc-explain-event"
    assert admitted.plan.tasks[0].effects == []


def test_host_accepts_only_a_model_blocker_code_verified_by_the_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_event"})
    )
    catalog = _catalog(
        "renderdoc-explain-event", tools={"inspect_event"}, category="event-pipeline"
    )
    matrix = build_capability_matrix(
        Profile(
            "no-tools",
            "openai",
            "model",
            options={"supports_tools": False},
        ),
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    )
    decision = InitialBlockedDecision(
        goal=_goal(),
        reason="The selected profile explicitly reports that tools are unsupported.",
        answer="This capture-specific inspection cannot run with the selected profile.",
        blocker_codes=["profile-tools-unsupported"],
    )

    admitted = admit_initial_decision(
        decision,
        request="Explain this EID",
        context={"capture": {"loaded": True}},
        catalog=catalog,
        matrix=matrix,
    )

    assert admitted is decision


def test_host_admits_a_planned_effect_only_when_the_effect_route_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(
        tools={"inspect_shader", "decompile_shader_workspace"}
    )
    profile = Profile(
        "tool-model", "openai", "model", options={"supports_tools": True}
    )
    proposal = InitialSupervisorProposal(
        goal=_goal("Generate readable HLSL for the selected shader"),
        stage_objective="Create a managed HLSL reconstruction",
        plan=TaskPlan(
            request_summary="Generate readable HLSL",
            tasks=[
                PlannedTask(
                    id="task-1",
                    summary="Decompile into a managed workspace",
                    skill="renderdoc-shader-workbench",
                    effects=["workspace"],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        capabilities, "available_internal_tools", lambda: frozenset({"inspect_shader"})
    )
    inspection_only = build_capability_matrix(
        profile,
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    )

    rejected = admit_initial_decision(
        proposal,
        request="Generate readable HLSL for this shader",
        context={"capture": {"loaded": True}},
        catalog=catalog,
        matrix=inspection_only,
    )
    assert isinstance(rejected, InitialAskUserDecision)

    monkeypatch.setattr(
        capabilities,
        "available_internal_tools",
        lambda: frozenset({"inspect_shader", "decompile_shader_workspace"}),
    )
    effect_ready = build_capability_matrix(
        profile,
        catalog,
        {"capture": {"loaded": True}},
        "qrenderdoc-instance",
    )

    admitted = admit_initial_decision(
        proposal,
        request="Generate readable HLSL for this shader",
        context={"capture": {"loaded": True}},
        catalog=catalog,
        matrix=effect_ready,
    )
    assert admitted is proposal
