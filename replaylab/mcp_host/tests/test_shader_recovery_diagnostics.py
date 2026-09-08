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


def test_unknown_failure_requests_diagnostic_replan_not_explanation_only() -> None:
    diagnostic = classify_shader_failure(
        RuntimeError("unclassified workspace preparation failure"),
        phase="workspace-prepare",
    )

    assert diagnostic["category"] == "unknown"
    assert diagnostic["retryable"] is False
    assert diagnostic["allowedActions"] == [
        "preserve-evidence",
        "request-ai-diagnostic-review",
    ]


def test_abi_mismatch_keeps_apply_blocked() -> None:
    result = classify_shader_failure(
        AbiValidationError("candidate ABI resource binding mismatch"),
        phase="abi-validate",
    )
    assert result["category"] == "abi-mismatch"
    assert "keep-apply-blocked" in result["allowedActions"]
    assert result["aiPolicy"]["maySupplyPhysicalAbiFacts"] is False


def test_replay_backend_and_ray_boundaries_have_distinct_finite_actions() -> None:
    backend = classify_shader_failure(
        RuntimeError(
            "Checked Vulkan Shader replacement failed: derived pipeline rebuild failed"
        ),
        phase="live-edit-apply",
    )
    assert backend["category"] == "backend-replacement-rejected"
    assert backend["retryable"] is False
    assert "keep-last-good-replacement" in backend["allowedActions"]
    assert "require-new-compiled-save" in backend["allowedActions"]

    ray = classify_shader_failure(
        RuntimeError("Ray shader replacement needs state-object shader-group and SBT evidence"),
        phase="live-edit-apply",
    )
    assert ray["category"] == "ray-library-replacement-unavailable"
    assert "preserve-compiled-source-or-ir" in ray["allowedActions"]
    assert "keep-apply-blocked" in ray["allowedActions"]


def test_capture_and_replacement_ownership_conflicts_are_not_blind_retries() -> None:
    capture = classify_shader_failure(
        RuntimeError("active capture session changed before Shader apply"),
        phase="live-edit-apply",
    )
    assert capture["category"] == "capture-or-shader-identity-conflict"
    assert capture["retryable"] is False
    assert "rebind-exact-capture-and-shader" in capture["allowedActions"]

    owner = classify_shader_failure(
        RuntimeError("replacement is no longer owned by this workspace"),
        phase="live-edit-apply",
    )
    assert owner["category"] == "replacement-ownership-conflict"
    assert "use-owner-specific-rollback" in owner["allowedActions"]


def test_workspace_prepare_failures_keep_stable_safe_recovery_categories() -> None:
    path = classify_shader_failure(
        RuntimeError("workspace path already exists: C:/private/path"),
        phase="workspace-prepare",
    )
    assert path["category"] == "workspace-path-conflict"
    assert path["retryable"] is True
    assert "use-new-isolated-workspace" in path["allowedActions"]

    catalog = classify_shader_failure(
        RuntimeError("RenderDoc bridge did not return catalogPath"),
        phase="workspace-prepare",
    )
    assert catalog["category"] == "capture-catalog-export-failed"
    assert catalog["retryable"] is True
    assert "re-export-capture-catalog" in catalog["allowedActions"]

    long_catalog_path = classify_shader_failure(
        RuntimeError(
            "[Errno 2] No such file or directory: "
            "C:/Users/test/AppData/Local/ReplayLab/external-agent-workspace/"
            "turn-abc/agent-workspace/.renderdoc-ultra-catalogs/mainps/"
            "blobs/" + ("a" * 64) + ".bin.tmp.1234.5678"
        ),
        phase="workspace-prepare",
    )
    assert long_catalog_path["category"] == "workspace-path-conflict"
    assert long_catalog_path["retryable"] is True
    assert "resolve-active-managed-workspace-path" in long_catalog_path[
        "allowedActions"
    ]

    identity = classify_shader_failure(
        RuntimeError(
            "selected Shader entry point does not match expected_entry_point"
        ),
        phase="workspace-prepare",
    )
    assert identity["category"] == "capture-or-shader-identity-conflict"
    assert identity["retryable"] is False
    assert "rebind-exact-capture-and-shader" in identity["allowedActions"]


def test_workspace_prepare_managed_path_violations_fail_closed() -> None:
    messages = (
        "workspace_path is not the active project's deterministic path",
        "deterministic Shader workspace escaped the active project",
        "existing workspace is outside the active project workspace root",
    )
    for message in messages:
        result = classify_shader_failure(
            RuntimeError(message),
            phase="workspace-prepare",
        )
        assert result["category"] == "workspace-path-policy-violation"
        assert result["retryable"] is False
        assert "keep-workspace-write-blocked" in result["allowedActions"]
        assert "use-new-isolated-workspace" not in result["allowedActions"]

    ordinary_collision = classify_shader_failure(
        RuntimeError("workspace path already exists: C:/private/path"),
        phase="workspace-prepare",
    )
    assert ordinary_collision["category"] == "workspace-path-conflict"
    assert ordinary_collision["retryable"] is True


def test_workspace_prepare_provider_stderr_cannot_spoof_identity_conflict() -> None:
    result = classify_shader_failure(
        ToolExecutionError(
            "provider process exited with code 1: selected Shader bytes do not "
            "match expected_raw_sha256"
        ),
        phase="workspace-prepare",
    )
    assert result["category"] == "tool-rejected-input"
    assert result["category"] != "capture-or-shader-identity-conflict"
    assert "try-registered-alternate-route" in result["allowedActions"]


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


def test_baseline_compile_probe_prefers_an_alternate_compiler_pair() -> None:
    result = classify_shader_failure(
        ToolExecutionError(
            "untouched reconstructed-source baseline compile probe failed: "
            "compiler process exited with code 1"
        ),
        phase="recovery-decompile-or-baseline-compile-probe",
    )
    assert result["category"] == "baseline-compile-probe-failed"
    assert result["retryable"] is True
    assert (
        "try-same-decompiler-with-registered-alternate-compiler"
        in result["allowedActions"]
    )
    assert "keep-apply-blocked" in result["allowedActions"]


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
