from __future__ import annotations

from src.shader_workspace import (
    AbiValidationError,
    ManifestValidationError,
    ShaderOrganizationError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
    classify_shader_failure,
)


def test_stale_workspace_toolchain_requests_fixed_migration_not_catalog_reexport() -> None:
    result = classify_shader_failure(
        ManifestValidationError(
            "workspace route catalog bytes changed after creation"
        ),
        phase="watch-compile",
    )
    assert result["category"] == "workspace-toolchain-upgrade-required"
    assert result["retryable"] is True
    assert "migrate-source-to-current-fixed-toolchain" in result["allowedActions"]
    assert result["aiPolicy"]["mayAuthorProvisionalSource"] is False


def test_route_failure_offers_only_bounded_recovery_actions() -> None:
    result = classify_shader_failure(
        ToolchainRoutingError("no integrity-valid dxil -> hlsl route"),
        phase="decompile",
    )
    assert result["category"] == "route-unavailable"
    assert "construct-source-if-compile-route-exists" in result["allowedActions"]
    assert result["aiPolicy"]["mayAuthorProvisionalSource"] is True
    assert result["aiPolicy"]["maySupplyCommandOrExecutable"] is False
    assert result["aiPolicy"]["mayAuthorizeApply"] is False


def test_integrity_and_malformed_output_are_not_delegated_to_ai() -> None:
    integrity = classify_shader_failure(
        ToolConfigurationError("executable SHA-256 integrity mismatch"),
        phase="route-bind",
    )
    assert integrity["category"] == "tool-integrity-or-configuration"
    assert integrity["aiPolicy"]["mayAuthorProvisionalSource"] is False

    malformed = classify_shader_failure(
        ToolExecutionError("route declared hlsl but output detected as unknown"),
        phase="decompile",
    )
    assert malformed["category"] == "malformed-tool-output"
    assert malformed["allowedActions"] == [
        "preserve-evidence",
        "try-registered-alternate-route",
    ]


def test_abi_mismatch_keeps_apply_blocked() -> None:
    result = classify_shader_failure(
        AbiValidationError("candidate ABI resource binding mismatch"),
        phase="abi-validate",
    )
    assert result["category"] == "abi-mismatch"
    assert "keep-apply-blocked" in result["allowedActions"]
    assert result["aiPolicy"]["maySupplyPhysicalAbiFacts"] is False


def test_tool_rejection_that_mentions_interface_still_allows_another_route() -> None:
    result = classify_shader_failure(
        ToolExecutionError(
            "compiler process exited with code 1: reconstructed interface was rejected"
        ),
        phase="baseline-compile-probe",
    )
    assert result["phase"] == "baseline-compile-probe"
    assert result["category"] == "tool-rejected-input"
    assert "try-registered-alternate-route" in result["allowedActions"]


def test_organization_session_sha_tamper_is_evidence_not_tool_integrity() -> None:
    result = classify_shader_failure(
        ShaderOrganizationError("baseline code SHA-256 is invalid"),
        phase="organization-review",
    )
    assert result["category"] == "organization-evidence-invalid"
    assert result["aiPolicy"]["mayRefineUnknownCategory"] is False
    assert result["aiPolicy"]["mayAuthorProvisionalSource"] is False


def test_organization_conflict_and_proposal_have_stable_categories() -> None:
    conflict = classify_shader_failure(
        ShaderOrganizationError(
            "RenderDoc capture session changed while collecting organization textures"
        ),
        phase="organization-begin",
    )
    assert conflict["category"] == "organization-conflict"
    assert conflict["retryable"] is True

    proposal = classify_shader_failure(
        ShaderOrganizationError("organization flowchart is not plain safe Mermaid"),
        phase="organization-review",
    )
    assert proposal["category"] == "organization-proposal-invalid"
    assert proposal["aiPolicy"]["mayAuthorProvisionalSource"] is True


def test_organization_compile_and_abi_preserve_typed_cause() -> None:
    direct_compile = classify_shader_failure(
        ToolExecutionError("compiler process exited with code 1"),
        phase="organization-begin",
    )
    assert direct_compile["category"] == "organization-compile-failed"
    assert direct_compile["exceptionType"] == "ToolExecutionError"

    direct_abi = classify_shader_failure(
        AbiValidationError("candidate ABI resource binding mismatch"),
        phase="organization-begin",
    )
    assert direct_abi["category"] == "organization-abi-failed"

    try:
        raise ToolExecutionError("compiler process exited with code 1")
    except ToolExecutionError as cause:
        try:
            raise ShaderOrganizationError(
                "organization review failed; baseline source restored"
            ) from cause
        except ShaderOrganizationError as wrapped_compile:
            compile_result = classify_shader_failure(
                wrapped_compile, phase="organization-review"
            )

    assert compile_result["category"] == "organization-compile-failed"
    assert compile_result["cause"]["category"] == "tool-rejected-input"
    assert compile_result["cause"]["exceptionType"] == "ToolExecutionError"

    try:
        raise AbiValidationError("candidate ABI resource binding mismatch")
    except AbiValidationError as cause:
        try:
            raise ShaderOrganizationError(
                "organization review failed; baseline source restored"
            ) from cause
        except ShaderOrganizationError as wrapped_abi:
            abi_result = classify_shader_failure(
                wrapped_abi, phase="organization-review"
            )

    assert abi_result["category"] == "organization-abi-failed"
    assert abi_result["cause"]["category"] == "abi-mismatch"
    assert "keep-live-edit-blocked" in abi_result["allowedActions"]
