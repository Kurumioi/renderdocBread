"""Agent Skills compatible discovery with ReplayLab policy metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    instructions: str
    allowed_tools: frozenset[str]
    metadata: dict[str, str]

    @property
    def category(self) -> str:
        return self.metadata.get("replaylab-category", "other")

    @property
    def read_only(self) -> bool:
        return self.metadata.get("replaylab-read-only", "false").lower() == "true"

    @property
    def enabled(self) -> bool:
        return not bool(self.metadata.get("replaylab-disabled-until", "").strip())

    @property
    def minimum_successful_tools(self) -> int:
        raw = self.metadata.get("replaylab-min-successful-tools", "0")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"skill '{self.name}' has an invalid replaylab-min-successful-tools value"
            ) from exc
        if not 0 <= value <= 20:
            raise ConfigurationError(
                f"skill '{self.name}' replaylab-min-successful-tools must be between 0 and 20"
            )
        return value

    @property
    def required_all_tools(self) -> frozenset[str]:
        return frozenset(
            value
            for value in self.metadata.get("replaylab-required-all-tools", "").split()
            if value
        )

    @property
    def required_any_tools(self) -> frozenset[str]:
        return frozenset(
            value
            for value in self.metadata.get("replaylab-required-any-tools", "").split()
            if value
        )


class SkillCatalog:
    def __init__(self, roots: list[Path | str]) -> None:
        self.roots = [Path(root) for root in roots]
        self.skills: dict[str, Skill] = {}

    def load(self) -> "SkillCatalog":
        discovered: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for file in sorted(root.glob("*/SKILL.md")):
                skill = self._read(file)
                # Validate machine-readable policy metadata at catalog load time.
                skill.minimum_successful_tools
                required = skill.required_all_tools | skill.required_any_tools
                if required - skill.allowed_tools:
                    raise ConfigurationError(
                        f"skill '{skill.name}' evidence tools are outside allowed-tools"
                    )
                if skill.name in discovered:
                    raise ConfigurationError(f"duplicate skill name: {skill.name}")
                discovered[skill.name] = skill
        self.skills = discovered
        return self

    def get(self, name: str) -> Skill:
        try:
            skill = self.skills[name]
        except KeyError as exc:
            raise ConfigurationError(f"unknown ReplayLab skill: {name}") from exc
        if not skill.enabled:
            reason = skill.metadata.get("replaylab-disabled-until", "future support")
            raise ConfigurationError(
                f"ReplayLab skill '{name}' is disabled until {reason}"
            )
        return skill

    def summaries(self) -> str:
        return "\n".join(
            f"- {skill.name} [{skill.category}]: {skill.description}"
            for skill in sorted(self.skills.values(), key=lambda item: item.name)
            if skill.enabled
        )

    @staticmethod
    def _read(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ConfigurationError(f"skill frontmatter is missing: {path}")
        try:
            _, header, body = text.split("---", 2)
            value = yaml.safe_load(header) or {}
        except (ValueError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"invalid skill frontmatter in {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigurationError(f"skill frontmatter must be a mapping: {path}")
        name = str(value.get("name", ""))
        description = str(value.get("description", "")).strip()
        if not _NAME_RE.fullmatch(name) or path.parent.name != name:
            raise ConfigurationError(f"skill name must match its directory: {path}")
        if not description or len(description) > 1024:
            raise ConfigurationError(f"skill description is empty or too long: {path}")
        raw_metadata = value.get("metadata", {}) or {}
        if not isinstance(raw_metadata, dict):
            raise ConfigurationError(f"skill metadata must be a mapping: {path}")
        metadata = {str(key): str(item) for key, item in raw_metadata.items()}
        allowed = frozenset(str(value.get("allowed-tools", "")).split())
        return Skill(name, description, path.resolve(), body.strip(), allowed, metadata)


INTENT_SKILLS = {
    "explain-current-event": "renderdoc-explain-event",
    "inspect-picked-pixel": "renderdoc-inspect-pixel",
    "reverse-edit-selected-shader": "renderdoc-shader-workbench",
}
