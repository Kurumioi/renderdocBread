"""Read-only filesystem Skill catalog used by both ReplayLab engines.

Agent topology belongs to the selected framework/provider and tool authority
belongs to the task-scoped ReplayLab MCP server. This module deliberately does
not support runtime Agent templates, Skill overlays, or external MCP
registrations: keeping those here would create a second, ineffective source of
truth beside deepagents and the vendor-native Agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import ConfigurationError
from .skills import Skill, SkillCatalog, SkillQuarantineSummary

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{1,63}")
_CJK_RE = re.compile(r"[一-鿿]")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "what",
        "why",
        "how",
        "please",
        "help",
        "me",
        "it",
        "is",
        "are",
        "of",
        "to",
        "in",
        "on",
        "an",
        "a",
        "be",
        "can",
        "you",
        "your",
        "my",
        "our",
    }
)


def relevance_terms(text: str) -> frozenset[str]:
    lowered = str(text or "").casefold()
    terms = {term for term in _TOKEN_RE.findall(lowered) if term not in _STOP}
    cjk = _CJK_RE.findall(lowered)
    terms.update("".join(pair) for pair in zip(cjk, cjk[1:]))
    return frozenset(list(terms)[:128])


def _score(terms: Iterable[str], fields: Iterable[tuple[str, int]]) -> int:
    score = 0
    for value, weight in fields:
        haystack = str(value or "").casefold()
        score += sum(weight for term in terms if term in haystack)
    return score


@dataclass(frozen=True, slots=True)
class SkillInfo:
    id: str
    title: str
    description: str
    category: str
    instructions: str
    path: Path
    always: bool = False

    def metadata(self) -> dict[str, str]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
        }


@dataclass
class CatalogView:
    skills: dict[str, SkillInfo] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    _quarantine: SkillQuarantineSummary = field(
        default_factory=lambda: SkillQuarantineSummary(0, ()),
        repr=False,
    )

    @classmethod
    def load(cls, *, skill_roots: Iterable[Path | str]) -> "CatalogView":
        catalog = SkillCatalog([Path(root) for root in skill_roots]).load_runtime()
        view = cls(_quarantine=catalog.quarantine_summary())
        for diagnostic in catalog.quarantine_diagnostics:
            detail = (
                f"skill quarantined: {diagnostic.code} "
                f"(root {diagnostic.root_index}, entry {diagnostic.entry_index})"
            )
            if diagnostic.skill_id:
                detail += f" [{diagnostic.skill_id}]"
            view.diagnostics.append(detail)
        for skill in catalog.skills.values():
            view.skills[skill.name] = _skill_from_filesystem(skill)
        return view

    def skill(self, skill_id: str) -> SkillInfo:
        try:
            return self.skills[skill_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown skill '{skill_id}'") from exc

    def rank_skills(self, text: str, *, limit: int = 16) -> list[SkillInfo]:
        terms = relevance_terms(text)
        scored = [
            (
                -_score(
                    terms,
                    (
                        (skill.id, 16),
                        (skill.title, 12),
                        (skill.description, 8),
                        (skill.instructions, 1),
                    ),
                ),
                skill.id,
                skill,
            )
            for skill in self.skills.values()
            if not skill.always
        ]
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def standing_skills(self) -> list[SkillInfo]:
        # Optional domain Skills may be quarantined independently. Losing the
        # shared acceptance contract must stop execution before an Agent runs.
        core = self.skills.get("replaylab-core")
        if core is None or not core.always or not core.instructions.strip():
            raise ConfigurationError(
                "Required replaylab-core Skill is missing, invalid, or not always loaded. "
                "Restore the bundled replaylab-core/SKILL.md with "
                'replaylab-always: "true" before starting an Agent task.'
            )
        return sorted(
            (skill for skill in self.skills.values() if skill.always),
            key=lambda skill: skill.id,
        )

    def quarantine_summary(self) -> SkillQuarantineSummary:
        return self._quarantine


def _skill_from_filesystem(skill: Skill) -> SkillInfo:
    return SkillInfo(
        id=skill.name,
        title=skill.name.replace("-", " ").title(),
        description=skill.description,
        category=skill.category,
        instructions=skill.instructions,
        path=skill.path,
        always=str(skill.metadata.get("replaylab-always", "")).strip().lower()
        in {"true", "1", "yes"},
    )


__all__ = ["CatalogView", "SkillInfo", "relevance_terms"]
