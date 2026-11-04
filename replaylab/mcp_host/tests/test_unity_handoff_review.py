from src.unity_handoff_review import review_handoff


def delivery():
    return dict(dualViewFinalOutputs=True,resizeAndTemporalChecks=True,assetLayoutReviewed=True,
                temporaryDiagnosticsRemoved=True,remainingIndependentWorkComplete=True)


def effect(**kwargs):
    return dict(name="TAA",state="unresolved",explanation="Previous-frame producer not reconstructed",
                dependencyKind="history",**kwargs)


def attempts():
    return [dict(effect="TAA",strategy=name,inputFingerprint=name,materialProgress=False)
            for name in ["trace-producer","port-reprojection","test-disocclusion"]]


def codes(result):return {row["code"] for row in result["findings"]}


def test_history_alone_is_not_proof_that_runtime_reconstruction_is_impossible():
    result=review_handoff([effect()],[],[],delivery())
    assert not result["userDecisionSuggested"]
    assert "runtime-history-route-not-investigated" in codes(result)


def test_no_progress_triggers_internal_replanning_without_a_user_budget_gate():
    result=review_handoff([effect(runtimeHistoryInvestigated=True)],[],attempts(),delivery())
    assert not result["userDecisionSuggested"] and result["reassessmentSuggested"]
    assert result["nextAction"]=="reassess-and-continue-repairs"
    assert result["suggestedOptions"]==[] and result["diagnosticsOnly"]
    assert not any("disable" in value.lower() or "accept" in value.lower() for value in result["suggestedOptions"])
    assert "complete" not in result and "blocked" not in result


def exhausted(tmp_path):
    proof=tmp_path/"attempted-routes.json";proof.write_text("retained diagnosis")
    return dict(status="exhausted-supported-recovery",remainingFeasibleRoutes=[],
        attemptedAlternatives=["trace and port original producer","test reprojection/rejection under fixed inputs"],
        reason="Original external state remains unavailable after tested runtime alternatives",evidencePaths=[str(proof)])


def test_independent_repairs_finish_before_asking_about_a_real_limit(tmp_path):
    state=delivery();state["remainingIndependentWorkComplete"]=False
    result=review_handoff([effect(resolutionAssessment=exhausted(tmp_path))],[],attempts(),state)
    assert result["nextAction"]=="continue-independent-work"


def test_repeating_identical_work_does_not_create_progress_but_real_progress_resets_stall():
    work=[attempts()[0]]*3
    first=review_handoff([effect()],[],work,delivery())
    assert not first["userDecisionSuggested"] and "unchanged-attempt-repeated" in codes(first)
    work.append(dict(effect="TAA",strategy="trace-producer",inputFingerprint="new-producer",materialProgress=True))
    improved=review_handoff([effect()],[],work,delivery())
    assert not improved["userDecisionSuggested"] and not improved["noProgressEffects"]


def test_disabling_only_unity_is_not_a_matching_remaining_effect_baseline(tmp_path):
    image=tmp_path/"reference.r32f";image.write_bytes(b"pixel")
    effects=[dict(name="lighting",state="matched"),dict(name="TAA",state="disabled",explanation="User chose to omit it",
                                                        userDecision=dict(accepted=True))]
    comparison=dict(referenceOrigin="rdc-original",matched=True,sameInputs=True,
        referenceEffects=["lighting","TAA"],renderedEffects=["lighting"],disabledEffects=["TAA"],evidencePaths=[str(image)])
    result=review_handoff(effects,[comparison],[],delivery())
    assert "remaining-effects-matched-baseline-missing" in codes(result)
    comparison["referenceEffects"]=["lighting"]
    result=review_handoff(effects,[comparison],[],delivery())
    assert result["matchedRemainingEffectsBaselineObserved"]
    assert not result["userDecisionSuggested"] and "delivery-effect-disabled" in codes(result)
    assert result["temporaryBypassIsDeliveryOption"] is False


def test_approximation_or_disable_needs_an_annotated_user_decision():
    for state in ["approximation","disabled"]:
        result=review_handoff([dict(name="TAA",state=state,explanation="Measured visual difference")],[],[],delivery())
        assert result["unapprovedTradeoffs"]==["TAA"] and not result["userDecisionSuggested"]


def test_acceptance_option_requires_exhausted_recovery_and_restored_matching_remainder(tmp_path):
    diagnosis=dict(schema="bread-unity-difference-localization/v1",isolation=[dict(effect="TAA",
        observedConsistentRemainderAndEffectDifference=True,remainderScope="full-frame-remainder",diagnosticStateRestored=True)])
    result=review_handoff([effect(resolutionAssessment=exhausted(tmp_path))],[],attempts(),delivery(),diagnosis=diagnosis)
    assert result["isolatedDifferenceEffects"]==["TAA"]
    assert "effect retained" in result["suggestedOptions"][1]
    assert not any("disable" in option.lower() for option in result["suggestedOptions"])
    diagnosis["isolation"][0]["diagnosticStateRestored"]=False
    assert not review_handoff([effect()],[],attempts(),delivery(),diagnosis=diagnosis)["isolatedDifferenceEffects"]


def test_slow_progress_still_reaches_a_finite_total_checkpoint_and_new_window_preserves_history():
    work=[dict(effect="TAA",strategy="repair",inputFingerprint=str(index),materialProgress=True,budgetWindowId="initial")
          for index in range(12)]
    result=review_handoff([effect()],[],work,delivery())
    assert not result["userDecisionSuggested"] and result["checkpointEffects"]==["TAA"]
    assert result["nextAction"]=="reassess-and-continue-repairs"
    assert not result["noProgressEffects"]
    resumed=review_handoff([effect(budgetWindowId="user-extended")],[],work,delivery())
    assert not resumed["userDecisionSuggested"] and len(work)==12


def test_diagnostic_bypass_cannot_be_left_in_a_delivered_effect():
    result=review_handoff([dict(name="TAA",state="matched",diagnosticBypassUsed=True)],[],[],delivery())
    assert "diagnostic-bypass-not-restored" in codes(result)


def test_an_untried_feasible_route_overrides_an_exhaustion_claim(tmp_path):
    assessment=exhausted(tmp_path);assessment["remainingFeasibleRoutes"]=["Inspect raw constant-buffer bindings through the alternate reader"]
    result=review_handoff([effect(resolutionAssessment=assessment)],[],attempts(),delivery())
    assert not result["userDecisionSuggested"]
    assert result["nextAction"]=="execute-remaining-feasible-recovery"


def test_a_concrete_unresolved_limit_with_saved_attempts_can_ask_user(tmp_path):
    result=review_handoff([effect(resolutionAssessment=exhausted(tmp_path))],[],attempts(),delivery())
    assert result["userDecisionSuggested"] and result["nextAction"]=="save-checkpoint-and-ask-user"
