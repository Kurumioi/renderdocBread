"""Shared deterministic feedback and bounded review configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

REVIEW_VERDICT_SCHEMA = "replaylab-review-verdict/v1"
DEFAULT_MAX_ROUNDS = 3
MAX_REVIEW_ROUNDS = 10
REVIEW_ROUNDS_ENV = "REPLAYLAB_REVIEW_MAX_ROUNDS"


def max_review_rounds(
    kind: str = "shader-organization", *, environ: Mapping[str, str] | None = None,
    legacy_env: str | None = None,
) -> int:
    values = os.environ if environ is None else environ
    value: Any = str(values.get(REVIEW_ROUNDS_ENV, "") or "").strip()
    if not value and legacy_env:
        value = str(values.get(legacy_env, "") or "").strip()
    if not value:
        local = str(values.get("LOCALAPPDATA", "") or "").strip()
        if local:
            path = Path(local) / "ReplayLab" / "config" / "review.json"
            try:
                if not path.is_symlink() and path.stat().st_size <= 65536:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        section = payload.get(kind)
                        value = section.get("maxRounds") if isinstance(section, dict) else payload.get("maxRounds")
            except (OSError, UnicodeError, ValueError):
                pass
    try:
        parsed = int(value) if value not in (None, "") and not isinstance(value, bool) else DEFAULT_MAX_ROUNDS
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_ROUNDS
    return max(1, min(parsed, MAX_REVIEW_ROUNDS))


def reason(
    code: str, message: str, *, location: str = "", direction: str = "",
    severity: str = "blocker", target: str = "", example: str = "",
) -> dict[str, str]:
    if severity not in {"blocker", "warning"}:
        raise ValueError("review reason severity must be blocker or warning")
    return {
        "code": code, "message": message, "location": location,
        "direction": direction, "severity": severity,
        "target": target or direction, "example": example,
    }


def build_verdict(
    *, accepted: bool, level: str, round_used: int, round_limit: int,
    reasons: Iterable[Mapping[str, Any]] = (), soft_accepted: bool = False,
    next_step: str = "", exhausted: bool | None = None,
) -> dict[str, Any]:
    issues = [dict(item) for item in reasons]
    for issue in issues:
        issue.setdefault("severity", "blocker")
        issue.setdefault("target", issue.get("direction", ""))
        issue.setdefault("example", "")
        if issue["severity"] not in {"blocker", "warning"}:
            raise ValueError("review reason severity must be blocker or warning")
    if (accepted or soft_accepted) and any(item["severity"] == "blocker" for item in issues):
        raise ValueError("blocking review reasons cannot be accepted or soft-accepted")
    spent = round_used >= round_limit if exhausted is None else bool(exhausted)
    return {
        "schema": REVIEW_VERDICT_SCHEMA,
        "accepted": accepted, "softAccepted": soft_accepted, "level": level,
        "round": round_used, "maxRounds": round_limit,
        "roundsLeft": max(0, round_limit - round_used),
        "budgetExhausted": spent,
        "reasons": issues, "nextStep": next_step,
    }
