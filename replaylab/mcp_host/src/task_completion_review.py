"""Agent task assessments bound to this execution and observed work receipts.

The Host validates shape and freshness only. It does not score answers, decide
whether evidence is sufficient, or scan/hash artifacts to prove completion.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

REVIEW_SCHEMA = "bread-task-review/v1"
STATE_SCHEMA = "bread-task-review-state/v1"
MAX_IDENTITIES = 4096
REVIEW_TOOLS = frozenset({"record_task_review", "record_shader_organization_review"})
TASK_REVIEW_TOOL_DESCRIPTION = (
    "Record the main Agent's self-review against the current user's requirements, after the last meaningful operation. "
    "Use existing evidence; this needs no preparatory tool or new validation. For an ordinary answer one requirement "
    "and its explanation suffice. Each criterion has requirement, status (satisfied/unmet/blocked), and evidence strings. "
    "Use complete only when every requirement is satisfied and no findings/actions/blocker remain; use needs_work "
    "with findings and next_actions then continue, or blocked with the concrete reason you cannot proceed. "
    "The Host binds this assessment to the current task execution and work version but does not judge answer quality. "
    "Subagents provide evidence to the main Agent and must not record the whole-task verdict."
    " If only a real user decision remains, save concrete evidence, use blocked with decision_request "
    "(question/options/evidence), and present that question. This ends automatic recovery without claiming completion."
)
TASK_REVIEW_TOOL_SCHEMA = {
    "type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["needs_work", "complete", "blocked"]},
        "criteria": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
            "type": "object", "properties": {
                "requirement": {"type": "string", "minLength": 1, "maxLength": 2000},
                "status": {"type": "string", "enum": ["satisfied", "unmet", "blocked"]},
                "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
            }, "required": ["requirement", "status", "evidence"], "additionalProperties": False,
        }},
        "findings": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "next_actions": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "blocker": {"type": "string", "maxLength": 4000, "default": ""},
        "decision_request": {"type": ["object","null"], "properties": {
            "question": {"type":"string","minLength":1,"maxLength":2000},
            "options": {"type":"array","minItems":2,"maxItems":3,"items":{"type":"string","minLength":1,"maxLength":512}},
            "evidence": {"type":"array","minItems":1,"maxItems":16,"items":{"type":"string","minLength":1,"maxLength":2000}},
        },"required":["question","options","evidence"],"additionalProperties":False},
    }, "required": ["verdict", "criteria", "findings", "next_actions"], "additionalProperties": False,
}
_VOLATILE = frozenset({"updatedunixns", "timestampunixns", "timestamp", "elapsedms", "durationms",
    "elapsed", "duration", "elapsedseconds", "durationseconds", "elapsedus", "durationus",
    "startedunixns", "completedunixns", "finishedunixns", "createdunixns", "latencyms", "walltimems",
    "createdat", "updatedat", "completedat", "startedat", "finishedat", "sampledat",
    "pollafterms", "requestid", "activityid", "operationid", "jobid", "generatedat"})


def _stable(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<nested>"
    if isinstance(value, Mapping):
        return {str(key): _stable(item, depth + 1) for key, item in list(value.items())[:1024]
                if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in _VOLATILE}
    if isinstance(value, (list, tuple)):
        return [_stable(item, depth + 1) for item in value[:1024]]
    if isinstance(value, str):
        return {"sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(), "chars": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def activity_identity(tool: str, result: Any, arguments: Mapping[str, Any] | None = None) -> str:
    """Digest observed facts without persisting source text, arguments or secrets."""
    if isinstance(result, Mapping) and result.get("schema") == "bread-unity-restoration/v1":
        from .unity_restoration_requirements import stable_activity
        result = stable_activity(result)
    value = {"tool": tool, "arguments": _stable(arguments or {}), "result": _stable(result)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def work_fingerprint(identities: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(identities))).encode("ascii")).hexdigest()


def make_review(state: Mapping[str, Any], verdict: str, criteria: list[dict[str, str]],
                findings: list[str], next_actions: list[str], blocker: str = "", decision_request: dict | None = None) -> dict[str, Any]:
    if verdict not in {"needs_work", "complete", "blocked"}:
        raise ValueError("verdict must be needs_work, complete or blocked")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 16:
        raise ValueError("criteria must contain 1-16 requirements from the current user request")
    for row in criteria:
        if not isinstance(row, Mapping) or set(row) != {"requirement", "status", "evidence"}:
            raise ValueError("Each criterion must contain requirement, status and evidence")
        if row["status"] not in {"satisfied", "unmet", "blocked"}:
            raise ValueError("criterion status must be satisfied, unmet or blocked")
        for key, limit in (("requirement", 2000), ("evidence", 4000)):
            if not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > limit:
                raise ValueError("Each requirement and evidence must be nonempty bounded text")
    for values in (findings, next_actions):
        if not isinstance(values, list) or len(values) > 32 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("findings and next_actions must contain at most 32 nonempty short strings")
    if not isinstance(blocker, str) or len(blocker) > 4000:
        raise ValueError("blocker must be a short explanation")
    if verdict == "complete" and (any(row["status"] != "satisfied" for row in criteria)
                                   or findings or next_actions or blocker.strip()):
        raise ValueError("complete requires all criteria satisfied with no unresolved findings, actions or blocker")
    if verdict == "needs_work" and (not findings or not next_actions):
        raise ValueError("needs_work requires concrete findings and next_actions")
    if verdict == "blocked" and not blocker.strip():
        raise ValueError("blocked requires the actual reason work cannot continue")
    if decision_request is not None:
        if (verdict!="blocked" or not isinstance(decision_request,dict) or
                set(decision_request)!={"question","options","evidence"}):
            raise ValueError("A pending user decision requires blocked and question/options/evidence")
        question=decision_request["question"];options=decision_request["options"];evidence=decision_request["evidence"]
        if (not isinstance(question,str) or not question.strip() or len(question)>2000 or
                not isinstance(options,list) or not 2<=len(options)<=3 or
                not isinstance(evidence,list) or not 1<=len(evidence)<=16 or
                any(not isinstance(value,str) or not value.strip() or len(value)>512 for value in options) or
                any(not isinstance(value,str) or not value.strip() or len(value)>2000 for value in evidence)):
            raise ValueError("Supply a short user question, 2-3 choices and concrete saved evidence")
    return {"schema": REVIEW_SCHEMA, "taskId": state["taskId"], "scopeId": state["scopeId"],
            "requestFingerprint": state["requestFingerprint"], "workRevision": state["workRevision"],
            "workFingerprint": work_fingerprint(state.get("identities", [])), "verdict": verdict,
            "criteria": [dict(row) for row in criteria], "findings": list(findings),
            "nextActions": list(next_actions), "blocker": blocker,
            "author": "agent", "hostSemanticApproval": False,
            **({"decisionRequest":json.loads(json.dumps(decision_request))} if decision_request is not None else {})}


def state_view(state: Mapping[str, Any]) -> dict[str, Any]:
    result = {"schema": STATE_SCHEMA, **{key: state[key] for key in
              ("taskId", "scopeId", "requestFingerprint", "workRevision")},
              "workFingerprint": work_fingerprint(state.get("identities", [])),
              "review": None, "fresh": False,
              "unityRestorations": [*state.get("unityRestorations", {}).values(),
                                    *state.get("unityFunctionalImports", {}).values()],
              "progressTrackingSaturated": state.get("progressTrackingSaturated") is True}
    if isinstance(state.get("reviewError"), str):
        result["reviewError"] = state["reviewError"][:600]
    review = state.get("review")
    if isinstance(review, Mapping):
        try:
            reconstructed = make_review(state, review.get("verdict"), review.get("criteria"),
                review.get("findings"), review.get("nextActions"), review.get("blocker", ""), review.get("decisionRequest"))
            identity = ("schema", "taskId", "scopeId", "requestFingerprint", "author", "hostSemanticApproval")
            if all(review.get(key) == reconstructed[key] for key in identity):
                result["review"] = json.loads(json.dumps(review))
                result["fresh"] = (type(review.get("workRevision")) is int
                    and review["workRevision"] == state["workRevision"]
                    and review.get("workFingerprint") == result["workFingerprint"])
        except (ValueError, KeyError, TypeError):
            pass
    return result
