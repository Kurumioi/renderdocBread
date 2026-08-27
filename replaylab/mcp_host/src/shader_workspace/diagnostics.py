"""Deterministic shader recovery failure taxonomy.

The classifier intentionally runs before AI.  It turns bounded exception text
and known exception types into a stable category plus a finite set of safe next
actions.  AI may explain an ``unknown`` result or choose among emitted actions;
it does not invent executables, command lines, physical ABI facts, or apply
authorization.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import (
    AbiValidationError,
    CatalogValidationError,
    ManifestValidationError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
)


DIAGNOSTIC_SCHEMA = "renderdoc-ultra-shader-recovery-diagnostic/v1"
_MAX_MESSAGE = 4096


def _bounded_message(error: BaseException | str) -> str:
    text = str(error).replace("\x00", "�")
    return text if len(text) <= _MAX_MESSAGE else text[:_MAX_MESSAGE] + "…"


def _contains(message: str, *patterns: str) -> bool:
    return any(re.search(pattern, message, flags=re.IGNORECASE) for pattern in patterns)


def _is_organization_error(error: BaseException | str) -> bool:
    if not isinstance(error, BaseException):
        return False
    return any(
        base.__name__ == "ShaderOrganizationError"
        and base.__module__.endswith("shader_workspace.organization")
        for base in type(error).__mro__
    )


def _direct_cause(error: BaseException | str) -> BaseException | None:
    if not isinstance(error, BaseException):
        return None
    cause = error.__cause__
    if cause is None and not error.__suppress_context__:
        cause = error.__context__
    return cause if isinstance(cause, BaseException) and cause is not error else None


def _classify_organization_failure(
    error: BaseException, message: str
) -> tuple[str, bool, list[str], bool]:
    """Classify the fixed organization transaction before generic text rules."""

    cause = _direct_cause(error)
    if _contains(
        message,
        r"capture session changed",
        r"active renderdoc capture does not match",
        r"source changed after",
        r"source changed concurrently",
        r"workspace (?:source |)binding changed",
        r"workspace source hash changed",
        r"not awaiting (?:proposal )?review",
    ):
        return (
            "organization-conflict",
            True,
            ["preserve-evidence", "refresh-workspace-state", "restart-organization-session"],
            False,
        )
    if _contains(
        message,
        r"organized hlsl proposal",
        r"organization analysis",
        r"organization flowchart",
        r"plain safe mermaid",
        r"analysis must not embed code fences",
        r"proposal source path",
    ):
        return (
            "organization-proposal-invalid",
            True,
            ["preserve-evidence", "repair-organization-proposal", "review-again"],
            True,
        )
    if isinstance(cause, AbiValidationError) or _contains(
        message,
        r"abi validation",
        r"replay abi",
        r"shader reflection",
        r"reflection evidence",
        r"compiled interface",
    ):
        return (
            "organization-abi-failed",
            False,
            [
                "preserve-evidence",
                "repair-source-within-captured-abi",
                "keep-live-edit-blocked",
            ],
            True,
        )
    if isinstance(cause, (ToolConfigurationError, ToolExecutionError)) or _contains(
        message,
        r"compile returned",
        r"compile was not bound",
        r"compiler produced",
        r"produced unstable .*executable code",
    ):
        return (
            "organization-compile-failed",
            False,
            [
                "preserve-evidence",
                "inspect-fixed-compiler-diagnostic",
                "repair-organization-proposal",
                "keep-live-edit-blocked",
            ],
            True,
        )
    # Session identity, hashes, paths, baseline provenance and all other
    # unrecognised organization invariants fail as fixed-program evidence.
    return (
        "organization-evidence-invalid",
        False,
        ["preserve-evidence", "discard-organization-session", "restart-organization-session"],
        False,
    )


def classify_shader_failure(
    error: BaseException | str,
    *,
    phase: str = "unknown",
) -> dict[str, Any]:
    """Return a stable recovery diagnostic for one failed operation."""

    message = _bounded_message(error)
    kind = type(error).__name__ if isinstance(error, BaseException) else "Diagnostic"
    organization_phase = str(phase or "").startswith("organization-")
    category = "unknown"
    retryable = False
    actions: list[str] = ["preserve-evidence", "request-ai-explanation"]
    ai_may_author_source = False

    if isinstance(error, ManifestValidationError) and _contains(
        message,
        r"route catalog bytes changed after creation",
        r"route plan tool registry hash mismatch",
        r"route capability changed after creation",
        r"route hop binding is inconsistent",
    ):
        category = "workspace-toolchain-upgrade-required"
        retryable = True
        actions = [
            "preserve-evidence",
            "migrate-source-to-current-fixed-toolchain",
            "compile-and-revalidate-in-migrated-workspace",
            "restart-persistent-live-edit-on-migrated-workspace",
        ]
    elif isinstance(error, (CatalogValidationError, ManifestValidationError)):
        category = "provenance-invalid"
        actions = ["preserve-evidence", "re-export-capture-catalog"]
    elif isinstance(error, ToolchainRoutingError):
        if _contains(message, r"unsupported shader format", r"unsupported target api"):
            category = "unsupported-format-or-api"
            actions = [
                "preserve-evidence",
                "inspect-native-ir",
                "install-or-register-format-provider",
            ]
        elif _contains(message, r"ambiguous", r"equal.*route", r"choose an explicit"):
            category = "route-ambiguous"
            actions = ["preserve-evidence", "choose-registered-route-id"]
        else:
            category = "route-unavailable"
            actions = [
                "preserve-evidence",
                "inspect-native-ir",
                "try-registered-alternate-route",
                "construct-source-if-compile-route-exists",
                "install-or-register-format-provider",
            ]
            ai_may_author_source = True
    elif _is_organization_error(error):
        category, retryable, actions, ai_may_author_source = (
            _classify_organization_failure(error, message)
        )
    elif organization_phase and isinstance(
        error, (ToolConfigurationError, ToolExecutionError)
    ):
        category = "organization-compile-failed"
        actions = [
            "preserve-evidence",
            "inspect-fixed-compiler-diagnostic",
            "repair-organization-proposal",
            "keep-live-edit-blocked",
        ]
        ai_may_author_source = isinstance(error, ToolExecutionError) and _contains(
            message, r"exited with code", r"process.*failed"
        )
    elif organization_phase and (
        isinstance(error, AbiValidationError)
        or _contains(message, r"abi", r"reflection", r"compiled interface")
    ):
        category = "organization-abi-failed"
        actions = [
            "preserve-evidence",
            "repair-source-within-captured-abi",
            "keep-live-edit-blocked",
        ]
        ai_may_author_source = True
    elif isinstance(error, ToolConfigurationError) or _contains(
        message, r"integrity", r"sha-?256", r"fixed file", r"registry.*changed"
    ):
        category = "tool-integrity-or-configuration"
        actions = ["preserve-evidence", "repair-process-owned-tool-registration"]
    elif isinstance(error, AbiValidationError) or (
        not isinstance(error, ToolExecutionError)
        and _contains(message, r"abi", r"reflection", r"interface")
    ):
        if _contains(message, r"unavailable", r"incomplete", r"opaque"):
            category = "baseline-interface-unavailable"
            actions = [
                "preserve-evidence",
                "recover-physical-interface",
                "keep-apply-blocked",
            ]
        else:
            category = "abi-mismatch"
            actions = [
                "preserve-evidence",
                "repair-source-within-captured-abi",
                "keep-apply-blocked",
            ]
            ai_may_author_source = True
    elif isinstance(error, ToolExecutionError):
        if _contains(message, r"timed out"):
            category = "tool-timeout"
            retryable = True
            actions = ["preserve-evidence", "try-registered-alternate-route"]
        elif _contains(message, r"launch", r"not found", r"cannot start"):
            category = "tool-launch-failed"
            actions = [
                "preserve-evidence",
                "try-registered-alternate-route",
                "repair-process-owned-tool-registration",
            ]
        elif _contains(message, r"exited with code", r"process.*failed"):
            category = "tool-rejected-input"
            actions = [
                "preserve-evidence",
                "try-registered-alternate-route",
                "repair-provisional-source-if-compile-route-exists",
            ]
            ai_may_author_source = True
        elif _contains(
            message,
            r"declared .* but output detected",
            r"non-utf",
            r"containing nul",
            r"did not create",
            r"not exactly one valid",
        ):
            category = "malformed-tool-output"
            actions = ["preserve-evidence", "try-registered-alternate-route"]
        elif _contains(message, r"root signature", r"rootsignature"):
            category = "root-signature-contract"
            actions = [
                "preserve-evidence",
                "reuse-captured-pipeline-root-signature",
                "keep-apply-blocked",
            ]
        else:
            category = "tool-execution-failed"
            actions = [
                "preserve-evidence",
                "try-registered-alternate-route",
                "request-ai-diagnostic-review",
            ]

    result = {
        "schema": DIAGNOSTIC_SCHEMA,
        "schemaVersion": 1,
        "phase": str(phase or "unknown")[:128],
        "category": category,
        "exceptionType": kind,
        "message": message,
        "retryable": retryable,
        "allowedActions": actions,
        "aiPolicy": {
            "mayExplain": True,
            "mayRefineUnknownCategory": category == "unknown",
            "mayChooseOnlyAllowedActions": True,
            "mayAuthorProvisionalSource": ai_may_author_source,
            "maySupplyPhysicalAbiFacts": False,
            "maySupplyCommandOrExecutable": False,
            "mayAuthorizeApply": False,
        },
    }
    cause = _direct_cause(error)
    if cause is not None:
        cause_diagnostic = classify_shader_failure(
            cause,
            phase=("underlying-" + str(phase or "unknown")[:108]),
        )
        result["cause"] = cause_diagnostic
        if (
            category == "organization-compile-failed"
            and cause_diagnostic["category"]
            in {
                "tool-integrity-or-configuration",
                "tool-launch-failed",
                "tool-timeout",
                "malformed-tool-output",
            }
        ):
            result["aiPolicy"]["mayAuthorProvisionalSource"] = False
    return result
