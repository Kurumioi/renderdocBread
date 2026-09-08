"""Host-owned structured task report — layer 2 of output consistency.

Every backend (the deepagents main Agent and each vendor-native subscription
Agent) hands in its answer through the same ``report_task_result`` tool. The
tool's arguments are a fixed form
(``replaylab-task-report/v1``); what the panel renders is that form, and the
model's prose is only the narration around it.

Acceptance is deterministic and factual — never a judgement of quality:

* schema (pydantic): every field, type and bound;
* ``observed`` findings must cite a tool that this task really called
  (main Agent or any child);
* artifacts must exist on disk and their status must be consistent with the
  report's ``status``.

A rejected report comes back with located reasons, targets and examples.
Schema and factual errors never become accepted by spending the retry budget.
Warnings which the Host can conservatively repair are accepted immediately.

Layer 3 (``ReportGate.verify``) runs when an Agent wants to end its turn: a
turn that used Host tools but handed in no accepted form is not
published yet — the engine sends the Host's verdict back as a follow-up turn
(bounded by ``REPLAYLAB_VERIFY_MAX_ROUNDS``) and only then publishes whatever
it has. Every engine calls the same two methods, so the policy is identical
for deepagents, Claude Code, Codex and the model API.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .review_contract import (
    DEFAULT_MAX_ROUNDS, MAX_REVIEW_ROUNDS, build_verdict, max_review_rounds, reason,
)

REPORT_SCHEMA = "replaylab-task-report/v1"
REPORT_STATE_SCHEMA = "replaylab-task-report-state/v1"
REPORT_TOOL = "report_task_result"
ROUNDS_ENV = "REPLAYLAB_REPORT_MAX_ROUNDS"
DEFAULT_VERIFY_ROUNDS = 2
VERIFY_ROUNDS_ENV = "REPLAYLAB_VERIFY_MAX_ROUNDS"
TASK_ID_ENV = "REPLAYLAB_TASK_ID"
TASK_EVENT_ROOT_ENV = "REPLAYLAB_TASK_EVENT_ROOT"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SIDECAR_BYTES = 2 * 1024 * 1024

ArtifactKind = Literal[
    "shader-workspace", "flow-graph", "analysis", "export", "project-registration", "event-report", "other"
]
ArtifactStatus = Literal["accepted", "soft-accepted", "pending", "failed"]
ReportStatus = Literal["completed", "partial", "failed"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    tool: str = Field(min_length=1, max_length=96)
    values: str = Field(default="", max_length=600)


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    claim: str = Field(min_length=1, max_length=1200)
    kind: Literal["observed", "inferred"] = "observed"
    confidence: Literal["high", "medium", "low", ""] = ""
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)


class Artifact(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    kind: ArtifactKind = "other"
    path: str = Field(min_length=1, max_length=1024)
    status: ArtifactStatus = "accepted"
    note: str = Field(default="", max_length=600)


class TaskReport(BaseModel):
    """What the model fills in; the Host adds ``schema``/``softAccepted`` on publish."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=4000)
    status: ReportStatus = "completed"
    findings: list[Finding] = Field(default_factory=list, max_length=24)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=16)
    unknowns: list[str] = Field(default_factory=list, max_length=12)
    next_steps: list[str] = Field(default_factory=list, max_length=8)

    def event_payload(self, *, soft_accepted: bool = False, issues: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        payload = {
            "schema": REPORT_SCHEMA,
            "summary": self.summary,
            "status": self.status,
            "findings": [finding.model_dump() for finding in self.findings],
            "artifacts": [artifact.model_dump() for artifact in self.artifacts],
            "unknowns": list(self.unknowns),
            "nextSteps": list(self.next_steps),
            "softAccepted": soft_accepted,
        }
        if issues:
            payload["issues"] = [dict(issue) for issue in issues]
        return payload

    @classmethod
    def from_event_payload(cls, payload: Mapping[str, Any]) -> "TaskReport":
        values = dict(payload)
        if "next_steps" not in values and "nextSteps" in values:
            values["next_steps"] = values["nextSteps"]
        return cls.model_validate(values)


REPORT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-6 sentences for the user: conclusion first."},
        "status": {"type": "string", "enum": ["completed", "partial", "failed"]},
        "findings": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "kind": {"type": "string", "enum": ["observed", "inferred"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {"tool": {"type": "string"}, "values": {"type": "string"}},
                            "required": ["tool"],
                        },
                    },
                },
                "required": ["claim", "kind"],
            },
        },
        "artifacts": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["shader-workspace", "flow-graph", "analysis", "export", "project-registration", "event-report", "other"],
                    },
                    "path": {"type": "string"},
                    "status": {"type": "string", "enum": ["accepted", "soft-accepted", "pending", "failed"]},
                    "note": {"type": "string"},
                },
                "required": ["kind", "path", "status"],
            },
        },
        "unknowns": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "next_steps": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
    },
    "required": ["summary", "status"],
}

REPORT_TOOL_DESCRIPTION = (
    "Hand in the task result as a fixed form BEFORE your final message. The panel renders this form; "
    "your prose is only narration. `findings[].kind=observed` needs `evidence[].tool` naming a tool "
    "this task really called (yours or a child's) and `values` with the key numbers; `inferred` needs "
    "`confidence`. `artifacts[]` lists files/workspaces the Host produced, with their gate status. "
    "`status`: completed = everything asked for is done and accepted; partial = something is missing "
    "(say what in `unknowns`); failed = nothing usable. The Host checks the form and answers with "
    "a replaylab-review-verdict/v1 verdict. Fix only blocker reasons while roundsLeft > 0; "
    "warnings need no resubmission. When budgetExhausted is true, stop retrying and preserve "
    "the unresolved result honestly. A new session does not reset the task budget."
)


def _bounded_env_int(name: str, default: int, environ: Mapping[str, str] | None, *, low: int, high: int) -> int:
    value = str((environ if environ is not None else os.environ).get(name, "") or "").strip()
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(low, min(parsed, high))


def max_rounds(environ: Mapping[str, str] | None = None) -> int:
    """Maximum report submissions, including invalid schemas; never a truth bypass."""

    return max_review_rounds("task-report", environ=environ, legacy_env=ROUNDS_ENV)


def verify_rounds(environ: Mapping[str, str] | None = None) -> int:
    """How many layer-3 follow-up turns may run before publishing (0 disables it)."""

    return _bounded_env_int(VERIFY_ROUNDS_ENV, DEFAULT_VERIFY_ROUNDS, environ, low=0, high=5)


def validate_report(
    arguments: Mapping[str, Any],
    *,
    tools_used: Iterable[str],
    artifact_exists: Callable[[str], bool] | None = None,
    artifact_paths: Iterable[str] | None = None,
) -> tuple[TaskReport | None, list[dict[str, str]]]:
    """Schema + factual checks. Returns (report or None, reasons)."""

    try:
        report = TaskReport.model_validate(dict(arguments))
    except ValidationError as exc:
        reasons = []
        for error in exc.errors()[:8]:
            location = ".".join(str(item) for item in error.get("loc", ())) or "report"
            reasons.append(
                reason(
                    "schema",
                    str(error.get("msg", "invalid value")),
                    location=location,
                    direction="Fill this field with the type and bounds the tool schema states.",
                    target="A report matching the fixed tool schema.",
                    example='{"summary":"Work is incomplete.","status":"partial","unknowns":["State the missing result."]}',
                )
            )
        return None, reasons

    used = {str(name).strip() for name in tools_used if str(name).strip()}
    exists = artifact_exists or _path_exists
    observed_paths = {_artifact_path_key(path) for path in artifact_paths} if artifact_paths is not None else None
    reasons: list[dict[str, str]] = []

    for index, finding in enumerate(report.findings):
        where = f"findings[{index}]"
        if finding.kind == "observed":
            if not finding.evidence:
                reasons.append(
                    reason(
                        "observed-without-evidence",
                        "an observed finding must cite the tool call that produced it",
                        location=f"{where}.evidence",
                        direction="Add {tool, values} for the call this value came from, or mark the finding `inferred` with a confidence.",
                        target="Observed claims cite actual Host evidence; hypotheses are labelled inferred.",
                        example='{"kind":"inferred","confidence":"low","claim":"A hypothesis, not an observed result."}',
                    )
                )
            for j, item in enumerate(finding.evidence):
                if item.tool not in used:
                    reasons.append(
                        reason(
                            "evidence-tool-not-called",
                            f"tool '{item.tool}' was not called in this task",
                            location=f"{where}.evidence[{j}].tool",
                            direction="Name a tool that this task (or one of its child tasks) actually called: "
                            + (", ".join(sorted(used)[:12]) if used else "no RenderDoc tool was called in this task")
                            + ". If the value was not observed, mark the finding `inferred`.",
                            target="Evidence names a tool called in this task.",
                            example=json.dumps({"tool": next(iter(sorted(used)), "<actual Host tool>"), "values": "<exact returned values>"}),
                        )
                    )
        elif not finding.confidence:
            finding.confidence = "low"
            reasons.append(
                reason(
                    "inferred-without-confidence",
                    "an inferred finding needs a confidence",
                    location=f"{where}.confidence",
                    direction="The Host conservatively filled confidence=low; no resubmission is needed.",
                    severity="warning",
                    target="Inference uncertainty is explicit.",
                    example='{"kind":"inferred","confidence":"low"}',
                )
            )

    for index, artifact in enumerate(report.artifacts):
        where = f"artifacts[{index}]"
        if not exists(artifact.path):
            reasons.append(
                reason(
                    "artifact-missing",
                    f"'{artifact.path}' does not exist",
                    location=f"{where}.path",
                    direction="Use the exact path a Host tool returned (workspacePath, artifactPath, exportPath); do not invent or shorten it. Drop the entry if nothing was produced.",
                    target="Every listed artifact exists at its exact Host-returned path.",
                    example='{"artifacts":[],"status":"partial","unknowns":["The requested artifact was not produced."]}',
                )
            )
        elif observed_paths is not None and _artifact_path_key(artifact.path) not in observed_paths:
            reasons.append(reason(
                "artifact-not-observed", "this existing path was not returned by a Host tool in this task",
                location=f"{where}.path",
                direction="Use an exact artifact/workspace path from this task's Host results, or omit this artifact.",
                target="An existing artifact with actual task provenance, not an arbitrary local file.",
                example='{"artifacts":[],"status":"partial","unknowns":["No Host-produced artifact was established."]}',
            ))

    unfinished = [a for a in report.artifacts if a.status in {"pending", "failed"}]
    if report.status == "completed" and unfinished:
        reasons.append(
            reason(
                "status-contradicts-artifacts",
                f"{len(unfinished)} artifact(s) are pending/failed but status is completed",
                location="status",
                direction="Set status to `partial` (and list what is missing in `unknowns`) or finish the artifact through its Host tool first.",
                target="Completed means no listed artifact is pending or failed.",
                example='{"status":"partial","unknowns":["The artifact is still pending."]}',
            )
        )
    if report.status == "failed" and any(a.status in {"accepted", "soft-accepted"} for a in report.artifacts):
        reasons.append(
            reason(
                "status-contradicts-artifacts",
                "accepted artifacts exist but status is failed",
                location="status",
                direction="Use `partial` when something usable was produced.",
                target="Preserve usable results without claiming complete success.",
                example='{"status":"partial","unknowns":["Some requested work remains."]}',
            )
        )
    return report, reasons


def _path_exists(path: str) -> bool:
    try:
        return Path(path).expanduser().exists()
    except (OSError, ValueError):
        return False


def _artifact_path_key(path: str) -> str:
    try:
        return os.path.normcase(str(Path(path).expanduser().resolve()))
    except (OSError, ValueError):
        return ""


class ReportGate:
    """One deterministic gate; exhausting retries never validates a false claim."""

    def __init__(self, *, tools_used: Callable[[], Iterable[str]],
                 artifact_exists: Callable[[str], bool] | None = None,
                 artifact_paths: Callable[[], Iterable[str]] | None = None,
                 limit: int | None = None, verify_limit: int | None = None) -> None:
        self._tools_used = tools_used
        self._artifact_exists = artifact_exists
        self._artifact_paths = artifact_paths
        self.limit = max(1, min(int(limit), MAX_REVIEW_ROUNDS)) if limit is not None else max_rounds()
        self.verify_limit = max(0, min(int(verify_limit), 5)) if verify_limit is not None else verify_rounds()
        self.attempts = 0
        self.rejections = 0
        self.report: TaskReport | None = None
        self.soft_accepted = False
        self.issues: list[dict[str, str]] = []
        self.last_reasons: list[dict[str, str]] = []
        self.follow_ups = 0
        self.verification_reasons: list[dict[str, str]] = []

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.limit

    def _verdict(self, accepted: bool) -> dict[str, Any]:
        if accepted:
            next_step = "Report accepted. End your turn with a short message; no wording revision is required."
        elif self.exhausted:
            next_step = "Report submission budget exhausted. Stop resubmitting or opening a new task/session to retry. Keep usable artifacts and report the unresolved blockers honestly; this report was not accepted."
        else:
            next_step = f"Fix only the listed blockers and call {REPORT_TOOL} again ({self.limit - self.attempts} submission(s) left)."
        return build_verdict(
            accepted=accepted, level="report-valid" if accepted else "report-invalid",
            round_used=self.attempts, round_limit=self.limit,
            reasons=self.issues if accepted else self.last_reasons,
            exhausted=self.exhausted, next_step=next_step,
        )

    def submit(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.exhausted:
            if self.report is not None and dict(arguments) == self.report.model_dump():
                return self._verdict(True)
            self.report = None
            if not any(item.get("severity", "blocker") == "blocker" for item in self.last_reasons):
                self.last_reasons = [reason(
                    "report-budget-exhausted", "no report submissions remain", location="report",
                    direction="Stop retrying; preserve usable artifacts and state the unresolved result.",
                    target="A bounded task without unsupported completion claims.",
                    example="The report was not accepted; existing artifacts remain available.",
                )]
            return self._verdict(False)
        self.attempts += 1
        report, reasons = validate_report(
            arguments, tools_used=self._tools_used(), artifact_exists=self._artifact_exists,
            artifact_paths=self._artifact_paths() if self._artifact_paths is not None else None,
        )
        self.last_reasons = list(reasons)
        blockers = [item for item in reasons if item.get("severity", "blocker") == "blocker"]
        if report is not None and not blockers:
            self.report, self.soft_accepted, self.issues = report, False, list(reasons)
            self.verification_reasons = []
            return self._verdict(True)
        self.rejections += 1
        self.report, self.soft_accepted, self.issues = None, False, []
        return self._verdict(False)

    def terminal_status(self, fallback: str, *, worked: bool = False) -> str:
        if fallback in {"needs-user-input", "failed"}:
            return fallback
        if self.report is None:
            return "partial" if worked or self.last_reasons or self.verification_reasons else fallback
        if self.report.status == "failed":
            return "failed"
        if self.report.status == "partial" or self.soft_accepted or fallback == "partial":
            return "partial"
        return "completed"

    def event_payload(self) -> dict[str, Any] | None:
        if self.report is None:
            return None
        return self.report.event_payload(soft_accepted=self.soft_accepted, issues=self.issues)

    def adopt_sidecars(self, report_payload: Mapping[str, Any] | None,
                      state_payload: Mapping[str, Any] | None) -> None:
        state = dict(state_payload or {})
        rejections = state.get("rejections", 0)
        if isinstance(rejections, int) and not isinstance(rejections, bool):
            self.rejections = max(self.rejections, rejections)
        attempts = state.get("attempts", rejections)
        if isinstance(attempts, int) and not isinstance(attempts, bool):
            self.attempts = max(self.attempts, attempts)
        limit = state.get("maxRounds")
        if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= MAX_REVIEW_ROUNDS:
            self.limit = limit
        follow_ups = state.get("followUps", 0)
        if isinstance(follow_ups, int) and not isinstance(follow_ups, bool):
            self.follow_ups = max(self.follow_ups, follow_ups)
        verify_limit = state.get("maxFollowUps")
        if isinstance(verify_limit, int) and not isinstance(verify_limit, bool) and 0 <= verify_limit <= 5:
            self.verify_limit = verify_limit
        verification_reasons = state.get("verificationReasons", [])
        if isinstance(verification_reasons, list):
            self.verification_reasons = [dict(item) for item in verification_reasons if isinstance(item, Mapping)]
        reasons = state.get("lastReasons", [])
        if isinstance(reasons, list):
            self.last_reasons = [dict(item) for item in reasons if isinstance(item, Mapping)]
        self.report, self.soft_accepted, self.issues = None, False, []
        if report_payload is None or state.get("accepted") is False:
            return
        try:
            report = TaskReport.from_event_payload(report_payload)
        except ValidationError:
            return
        # Older runtimes could soft-publish factual errors. Never inherit that
        # acceptance into a restarted task, even if its JSON schema is valid.
        checked, current_issues = validate_report(
            report.model_dump(), tools_used=self._tools_used(), artifact_exists=self._artifact_exists,
            artifact_paths=self._artifact_paths() if self._artifact_paths is not None else None,
        )
        if checked is None or any(item.get("severity", "blocker") == "blocker" for item in current_issues):
            self.last_reasons = current_issues
            return
        self.report = checked
        issues = report_payload.get("issues", [])
        self.issues = [dict(item) for item in issues if isinstance(item, Mapping) and item.get("severity") == "warning"] if isinstance(issues, list) else []
        self.issues.extend(current_issues)
        self.verification_reasons = []

    def verify(self, *, worked: bool) -> list[dict[str, str]]:
        if self.verify_limit <= 0 or not worked or self.report is not None:
            return []
        if self.last_reasons:
            directions = "; ".join(
                f"{item.get('location', 'report')}: {item.get('direction', '')}"
                for item in self.last_reasons if item.get("direction")
            )
            return [reason(
                "report-rejected", f"the last {REPORT_TOOL} form has unresolved blockers",
                location="report", direction=f"Fix only these fields: {directions}",
                target="A factual report consistent with actual tool evidence and artifact state.",
                example='{"summary":"Some requested work remains.","status":"partial","unknowns":["Describe what is missing."]}',
            ), *self.last_reasons]
        return [reason(
            "report-missing", f"this task ran Host tools but no {REPORT_TOOL} form was handed in",
            location="report", direction=f"Use the retained Host observations to call {REPORT_TOOL}; do not repeat completed mutations.",
            target="One accepted report for the work already performed.",
            example='{"summary":"Describe the actual result.","status":"partial","findings":[],"unknowns":["State any unresolved work."]}',
        )]

    def request_follow_up(self, reasons: Sequence[Mapping[str, str]]) -> bool:
        self.verification_reasons = [dict(item) for item in reasons]
        if self.exhausted or self.follow_ups >= self.verify_limit:
            return False
        self.follow_ups += 1
        return True

    def verification_payload(self) -> dict[str, Any] | None:
        if not self.follow_ups and not self.verification_reasons and not (self.report is None and self.last_reasons):
            return None
        return {
            "accepted": self.report is not None,
            "followUps": self.follow_ups, "maxFollowUps": self.verify_limit,
            "reportAttempts": self.attempts, "maxReportAttempts": self.limit,
            "budgetExhausted": self.exhausted or (self.report is None and self.follow_ups >= self.verify_limit),
            "reasons": [dict(item) for item in (self.verification_reasons or self.last_reasons if self.report is None else [])],
        }


def task_report_path(root: Path | str, task_id: str) -> Path:
    return _sidecar_path(root, task_id, ".report.json")


def task_report_state_path(root: Path | str, task_id: str) -> Path:
    return _sidecar_path(root, task_id, ".report-state.json")


def _sidecar_path(root: Path | str, task_id: str, suffix: str) -> Path:
    task_id = str(task_id or "")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id contains unsafe path characters")
    return Path(root) / f"{task_id}{suffix}"


def clear_task_report_sidecars(root: Path | str, task_id: str) -> None:
    """Explicit maintenance cleanup; never call when restarting an engine run."""

    for path in (task_report_path(root, task_id), task_report_state_path(root, task_id)):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_SIDECAR_BYTES:
        raise ValueError("task report sidecar exceeds the safety bound")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_sidecar(path: Path, schema: str, task_id: str) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SIDECAR_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != schema
        or value.get("taskId") != task_id
    ):
        return None
    return value


def read_task_report_sidecars(
    root: Path | str,
    task_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    state = _read_sidecar(
        task_report_state_path(root, task_id), REPORT_STATE_SCHEMA, task_id
    )
    # A single atomic state commit owns both budget and accepted report. The
    # separate report file is a compatibility mirror and may lag after a crash.
    if state is not None and "report" in state:
        report = state["report"] if isinstance(state["report"], dict) else None
    else:
        report = _read_sidecar(task_report_path(root, task_id), REPORT_SCHEMA, task_id)
    return report, state


MAX_TOOL_FACTS = 24
MAX_TOOL_FACT_CHARS = 1800
_SECRET_KEY = re.compile(r"token|secret|password|authorization|credential|cookie|api.?key", re.I)
_LARGE_BODY_KEYS = {"bytecode", "base64", "sourcetext", "sourcecode", "hlsl", "blob", "bytes"}


def _fact_projection(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<nested value omitted>"
    if isinstance(value, Mapping):
        result = {}
        # Identifiers and verdicts survive even when the tool returns a large
        # source document. The excerpt is evidence data, never instructions.
        priority = {"schema", "error", "accepted", "compatible", "status", "workspacePath", "captureId", "eventId", "resourceId", "shaderResourceId", "path", "result", "verdict"}
        entries = sorted(value.items(), key=lambda item: str(item[0]) not in priority)
        for key, child in entries[:24]:
            name = str(key)
            if _SECRET_KEY.search(name):
                result[name] = "<redacted>"
            elif name.lower() in _LARGE_BODY_KEYS and isinstance(child, (str, bytes, list)):
                result[name] = "<large source/binary omitted>"
            else:
                result[name] = _fact_projection(child, depth + 1)
        if len(entries) > 24:
            result["_omittedKeys"] = len(entries) - 24
        return result
    if isinstance(value, (list, tuple)):
        return [_fact_projection(item, depth + 1) for item in value[:8]] + (["<further items omitted>"] if len(value) > 8 else [])
    if isinstance(value, str):
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer <redacted>", value)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "<redacted>", text)
        text = re.sub(r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|password|secret|authorization)\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&;,]+)", r"\1<redacted>", text)
        return text[:1024] + ("<truncated>" if len(text) > 1024 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<non-text result>"


def tool_fact(name: str, result: Any, *, failed: bool = False) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            pass
    if isinstance(result, Mapping) and (result.get("error") or result.get("isError") is True):
        failed = True
    projected = _fact_projection(result)
    text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"), default=str)
    return {"tool": name, "outcome": "error" if failed else "returned",
            "summary": text[:MAX_TOOL_FACT_CHARS], "truncated": len(text) > MAX_TOOL_FACT_CHARS}


def _returned_artifact_paths(value: Any, depth: int = 0) -> set[str]:
    if isinstance(value, str) and depth == 0:
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return set()
    result: set[str] = set()
    if depth > 8:
        return result
    if isinstance(value, Mapping):
        for key, child in list(value.items())[:128]:
            if _SECRET_KEY.search(str(key)):
                continue
            if str(key) in {"path", "workspacePath", "artifactPath", "analysisPath", "flowchartPath", "sourcePath", "exportPath", "outputPath", "projectPath"} and isinstance(child, str) and len(child) <= 1024:
                result.add(child)
            elif isinstance(child, (Mapping, list, tuple)):
                result.update(_returned_artifact_paths(child, depth + 1))
    elif isinstance(value, (list, tuple)):
        for child in value[:128]:
            result.update(_returned_artifact_paths(child, depth + 1))
    return result


@contextmanager
def _task_state_lock(root: Path, task_id: str):
    root.mkdir(parents=True, exist_ok=True)
    path = _sidecar_path(root, task_id, ".report-state.lock")
    if path.is_symlink():
        raise ValueError("task report lock must not be redirected")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 5.0
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("task report state is busy; no review budget was reset")
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class TaskReportStore:
    """Shared task state survives Host restarts and vendor continuation threads."""

    def __init__(self, task_id: str, event_root: Path | str | None, *, environ: Mapping[str, str] | None = None) -> None:
        self.task_id = str(task_id or "").strip()
        self.event_root = Path(event_root) if event_root else None
        self.enabled = bool(self.event_root and _TASK_ID_RE.fullmatch(self.task_id))
        self._lock = threading.RLock()
        self._tools_used: set[str] = set()
        self._facts: list[dict[str, Any]] = []
        self._artifact_paths: set[str] = set()
        self.gate = ReportGate(tools_used=self.tools_used, artifact_paths=lambda: self._artifact_paths,
                               limit=max_rounds(environ), verify_limit=verify_rounds(environ))
        if self.enabled:
            with self._lock, _task_state_lock(self.event_root, self.task_id):
                self._hydrate()

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "TaskReportStore":
        values = environ if environ is not None else os.environ
        return cls(values.get(TASK_ID_ENV, ""), values.get(TASK_EVENT_ROOT_ENV, ""), environ=values)

    def tools_used(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tools_used))

    def _hydrate(self) -> None:
        report, state = read_task_report_sidecars(self.event_root, self.task_id)
        if state is None:
            state_path = task_report_state_path(self.event_root, self.task_id)
            if state_path.exists() or state_path.is_symlink():
                raise ValueError("task report state is invalid; refusing to reset its review budget")
            return
        self._tools_used.update(str(item) for item in state.get("toolsUsed", []) if isinstance(item, str) and item)
        self._artifact_paths.update(item for item in state.get("artifactPaths", []) if isinstance(item, str) and len(item) <= 1024)
        raw_facts = state.get("toolFacts", [])
        self._facts = [dict(item) for item in raw_facts if isinstance(item, Mapping) and isinstance(item.get("summary"), str) and isinstance(item.get("tool"), str)][-MAX_TOOL_FACTS:] if isinstance(raw_facts, list) else []
        self.gate.adopt_sidecars(report, state)

    def sync(self) -> None:
        if self.enabled:
            with self._lock, _task_state_lock(self.event_root, self.task_id):
                self._hydrate()

    def record_tool(self, name: str) -> None:
        name = str(name or "").strip()
        if not self.enabled or not name or name == REPORT_TOOL:
            return
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._tools_used.add(name)
            self._write_state()

    def record_result(self, name: str, result: Any, *, failed: bool = False) -> None:
        if not self.enabled or not name or name == REPORT_TOOL:
            return
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._tools_used.add(name)
            self._artifact_paths.update(_returned_artifact_paths(result))
            self._facts.append(tool_fact(name, result, failed=failed))
            self._facts = self._facts[-MAX_TOOL_FACTS:]
            self._write_state()

    def submit(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return build_verdict(
                accepted=False, level="report-invalid", round_used=0, round_limit=0,
                reasons=[reason("task-context-missing", "the Host has no task context", location="taskId",
                                direction="Start this MCP server through AgentHost for the active task.",
                                target="A report bound to one actual task.", example="Use the task-scoped ReplayLab MCP connection.")],
                next_step="Report submission is unavailable without a task context.",
            )
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            result = self.gate.submit(arguments)
            self._facts.append(tool_fact(REPORT_TOOL, result))
            self._facts = self._facts[-MAX_TOOL_FACTS:]
            self._write_state()
            if result.get("accepted") is True:
                payload = self.gate.event_payload() or {}
                _atomic_write_json(task_report_path(self.event_root, self.task_id), {**payload, "taskId": self.task_id})
            else:
                path = task_report_path(self.event_root, self.task_id)
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            return result

    def request_follow_up(self, reasons: Sequence[Mapping[str, str]]) -> bool:
        if not self.enabled:
            return False
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            allowed = self.gate.request_follow_up(reasons)
            self._write_state()
            return allowed

    def _write_state(self) -> None:
        if not self.enabled or self.event_root is None:
            return
        _atomic_write_json(task_report_state_path(self.event_root, self.task_id), {
            "schema": REPORT_STATE_SCHEMA, "taskId": self.task_id,
            "accepted": self.gate.report is not None, "softAccepted": self.gate.soft_accepted,
            "attempts": self.gate.attempts, "maxRounds": self.gate.limit,
            "budgetExhausted": self.gate.exhausted, "rejections": self.gate.rejections,
            "followUps": self.gate.follow_ups, "maxFollowUps": self.gate.verify_limit,
            "verificationReasons": self.gate.verification_reasons,
            "report": ({**self.gate.event_payload(), "taskId": self.task_id} if self.gate.report is not None else None),
            "lastReasons": [dict(item) for item in self.gate.last_reasons],
            "toolsUsed": sorted(self._tools_used), "toolFacts": self._facts,
            "artifactPaths": sorted(self._artifact_paths)[:128],
        })


_STORE_LOCK = threading.Lock()
_STORE_KEY: tuple[str, str] | None = None
_STORE: TaskReportStore | None = None


def current_task_report_store(
    environ: Mapping[str, str] | None = None,
) -> TaskReportStore:
    global _STORE_KEY, _STORE
    values = environ if environ is not None else os.environ
    key = (
        str(values.get(TASK_ID_ENV, "")),
        str(values.get(TASK_EVENT_ROOT_ENV, "")),
    )
    with _STORE_LOCK:
        if _STORE is None or _STORE_KEY != key:
            _STORE_KEY = key
            _STORE = TaskReportStore.from_environment(values)
        return _STORE


def instrument_task_tool(function: Callable[..., Any]) -> Callable[..., Any]:
    """Record every actual MCP call while preserving FastMCP introspection."""

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            store = current_task_report_store()
            store.record_tool(function.__name__)
            try:
                result = await function(*args, **kwargs)
            except Exception as error:
                store.record_result(function.__name__, str(error), failed=True)
                raise
            store.record_result(function.__name__, result)
            return result

        return async_wrapper

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        store = current_task_report_store()
        store.record_tool(function.__name__)
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            store.record_result(function.__name__, str(error), failed=True)
            raise
        store.record_result(function.__name__, result)
        return result

    return wrapper


__all__ = [
    "Artifact",
    "Evidence",
    "Finding",
    "REPORT_SCHEMA",
    "REPORT_STATE_SCHEMA",
    "REPORT_TOOL",
    "REPORT_TOOL_DESCRIPTION",
    "REPORT_TOOL_SCHEMA",
    "ReportGate",
    "TASK_EVENT_ROOT_ENV",
    "TASK_ID_ENV",
    "TaskReport",
    "TaskReportStore",
    "clear_task_report_sidecars",
    "current_task_report_store",
    "instrument_task_tool",
    "max_rounds",
    "read_task_report_sidecars",
    "reason",
    "task_report_path",
    "task_report_state_path",
    "tool_fact",
    "validate_report",
    "verify_rounds",
]
