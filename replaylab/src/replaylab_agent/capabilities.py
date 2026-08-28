"""Runtime capability discovery for supervisor routing and specialist admission.

The matrix is descriptive rather than an authorization list.  It lets the
supervisor distinguish a genuinely unavailable execution path from one that
is merely conditional on probing a model. The
actual tool transport still validates names, arguments, capture identity and
workflow postconditions immediately before execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .mcp_tools import find_replaylab_mcp
from .policy import READ_ONLY_TOOLS, tool_effects
from .profiles import Profile
from .skills import Skill, SkillCatalog
from .tool_service import available_internal_tools


CapabilityState = Literal["ready", "conditional", "unavailable"]
ToolSupportState = Literal["supported", "unknown", "unsupported"]


@dataclass(frozen=True, slots=True)
class SkillCapability:
    name: str
    state: CapabilityState
    internal_tools: tuple[str, ...]
    sidecar_tools: tuple[str, ...]
    unavailable_tools: tuple[str, ...]
    effect_classes: tuple[str, ...]
    available_effect_classes: tuple[str, ...]
    unavailable_effect_classes: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def attemptable(self) -> bool:
        return self.state != "unavailable"

    def can_attempt_effects(self, effects: tuple[str, ...] | list[str]) -> bool:
        return self.attemptable and set(effects).issubset(self.available_effect_classes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "attemptable": self.attemptable,
            "internalTools": list(self.internal_tools),
            "sidecarTools": list(self.sidecar_tools),
            "unavailableTools": list(self.unavailable_tools),
            "effectClasses": list(self.effect_classes),
            "availableEffectClasses": list(self.available_effect_classes),
            "unavailableEffectClasses": list(self.unavailable_effect_classes),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    tool_support: ToolSupportState
    capture_loaded: bool
    instance_pinned: bool
    internal_tool_service_ready: bool
    sidecar_transport_state: CapabilityState
    skills: tuple[SkillCapability, ...]
    hard_blockers: tuple[str, ...]

    def skill(self, name: str) -> SkillCapability | None:
        return next((item for item in self.skills if item.name == name), None)

    @property
    def has_attemptable_specialist(self) -> bool:
        return any(item.attemptable for item in self.skills)

    def as_dict(self) -> dict[str, Any]:
        # Paths are host diagnostics and are intentionally omitted from the
        # model-facing form.  The supervisor needs readiness, not installation
        # details that could be echoed to a user.
        return {
            "schema": "replaylab-capability-matrix/v3",
            "toolSupport": self.tool_support,
            "captureLoaded": self.capture_loaded,
            "instancePinned": self.instance_pinned,
            "internalToolService": (
                "ready" if self.internal_tool_service_ready else "unavailable"
            ),
            "sidecarTransport": self.sidecar_transport_state,
            "skills": [item.as_dict() for item in self.skills],
            "hardBlockers": list(self.hard_blockers),
            "routingRule": (
                "ready and conditional Skills are attemptable; only the Host may confirm a "
                "hard blocker after dispatch validation"
            ),
        }


def profile_tool_support(profile: Profile) -> ToolSupportState:
    """Return a tri-state instead of treating an unprobed model as denied."""

    explicit = profile.options.get("supports_tools")
    if explicit is False:
        return "unsupported"
    if explicit is True or profile.provider.kind == "agent":
        return "supported"
    if "tools" in profile.capabilities:
        return "supported"
    if profile.probed_capabilities is not None:
        return "unsupported"
    return "unknown"


def _capture_loaded(context: dict[str, Any], capture_path: str = "") -> bool:
    capture = context.get("capture")
    if isinstance(capture, dict):
        loaded = capture.get("loaded")
        if isinstance(loaded, bool):
            return loaded
        if str(capture.get("path", "") or "").strip():
            return True
    return bool(str(capture_path or "").strip())


def _sidecar_path(explicit: Path | str | None) -> str:
    try:
        return str(find_replaylab_mcp(explicit))
    except Exception:
        return ""


def _skill_capability(
    skill: Skill,
    *,
    tool_support: ToolSupportState,
    capture_loaded: bool,
    instance_pinned: bool,
    sidecar_path: str,
    sidecar_only: bool,
) -> SkillCapability:
    internal_contracts = available_internal_tools()
    if sidecar_only:
        # ACP v1 accepts MCP servers but has no Codex-style dynamicTools Host
        # callback.  Its complete task grant therefore uses the filtered
        # external adapter even though the same contracts are implemented by
        # InternalToolService for model API and Codex profiles.
        internal = ()
        sidecar = tuple(sorted(skill.allowed_tools)) if sidecar_path else ()
        unresolved = () if sidecar_path else tuple(sorted(skill.allowed_tools))
    else:
        internal = tuple(sorted(skill.allowed_tools & internal_contracts))
        remaining = tuple(sorted(skill.allowed_tools - set(internal)))
        sidecar = remaining if sidecar_path else ()
        unresolved = () if sidecar_path else remaining
    effects = tuple(
        sorted({effect for name in skill.allowed_tools for effect in tool_effects(name)})
    )
    available_tools = set(internal) | set(sidecar)
    available_effects = tuple(
        sorted({effect for name in available_tools for effect in tool_effects(name)})
    )
    unavailable_effects = tuple(sorted(set(effects) - set(available_effects)))
    reasons: list[str] = []
    if tool_support == "unsupported":
        reasons.append("selected profile explicitly reports no tool calling")
    elif tool_support == "unknown":
        reasons.append("tool support will be probed by attempting the selected provider")
    if not capture_loaded:
        reasons.append("no RenderDoc capture is loaded")
    if not instance_pinned:
        reasons.append("no qrenderdoc instance is pinned")
    if sidecar:
        reasons.append("ACP will use one task-filtered bundled MCP adapter")
    elif unresolved:
        reasons.append("no active transport currently exposes some workflow tools")

    if tool_support == "unsupported" or not capture_loaded or not instance_pinned:
        state: CapabilityState = "unavailable"
    elif not internal and unresolved:
        state = "unavailable"
    elif tool_support == "unknown" or unresolved:
        state = "conditional"
    else:
        state = "ready"
    return SkillCapability(
        name=skill.name,
        state=state,
        internal_tools=internal,
        sidecar_tools=sidecar,
        unavailable_tools=unresolved,
        effect_classes=effects,
        available_effect_classes=available_effects,
        unavailable_effect_classes=unavailable_effects,
        reasons=tuple(reasons),
    )


def build_capability_matrix(
    profile: Profile,
    catalog: SkillCatalog,
    context: dict[str, Any],
    instance_id: str,
    *,
    capture_path: str = "",
    enable_sidecar: bool = False,
    mcp_executable: Path | str | None = None,
) -> CapabilityMatrix:
    support = profile_tool_support(profile)
    loaded = _capture_loaded(context, capture_path)
    pinned = bool(str(instance_id or "").strip())
    internal_ready = bool(available_internal_tools())
    sidecar = _sidecar_path(mcp_executable) if enable_sidecar else ""
    skills = tuple(
        _skill_capability(
            skill,
            tool_support=support,
            capture_loaded=loaded,
            instance_pinned=pinned,
            sidecar_path=sidecar,
            sidecar_only=enable_sidecar,
        )
        for skill in sorted(catalog.skills.values(), key=lambda item: item.name)
        if skill.enabled
    )
    blockers: list[str] = []
    if support == "unsupported":
        blockers.append("profile-tools-unsupported")
    if not loaded:
        blockers.append("capture-not-loaded")
    if not pinned:
        blockers.append("instance-not-pinned")
    if not blockers and skills and not any(item.attemptable for item in skills):
        blockers.append("no-specialist-transport")
    return CapabilityMatrix(
        tool_support=support,
        capture_loaded=loaded,
        instance_pinned=pinned,
        internal_tool_service_ready=internal_ready,
        sidecar_transport_state="ready" if sidecar else "unavailable",
        skills=skills,
        hard_blockers=tuple(dict.fromkeys(blockers)),
    )


def read_only_tool(name: str) -> bool:
    """Compatibility helper used by routing tests and diagnostics."""

    return name in READ_ONLY_TOOLS or name in available_internal_tools()


__all__ = [
    "CapabilityMatrix",
    "SkillCapability",
    "build_capability_matrix",
    "profile_tool_support",
    "read_only_tool",
]
