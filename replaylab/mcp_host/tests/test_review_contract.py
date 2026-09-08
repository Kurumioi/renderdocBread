import json

import pytest

from src.review_contract import build_verdict, max_review_rounds, reason


def test_report_and_artifact_share_configuration_and_bounds(tmp_path):
    assert max_review_rounds(environ={}) == 3
    assert max_review_rounds(environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "99"}) == 10
    assert max_review_rounds(environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "-2"}) == 1
    assert max_review_rounds(environ={"REPLAYLAB_REVIEW_MAX_ROUNDS": "invalid"}) == 3
    values = {"REPLAYLAB_REVIEW_MAX_ROUNDS": "4", "REPLAYLAB_REPORT_MAX_ROUNDS": "8"}
    assert max_review_rounds("task-report", environ=values, legacy_env="REPLAYLAB_REPORT_MAX_ROUNDS") == 4
    config = tmp_path / "ReplayLab" / "config" / "review.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"maxRounds": 5}), encoding="utf-8")
    assert max_review_rounds("task-report", environ={"LOCALAPPDATA": str(tmp_path)}) == 5
    assert max_review_rounds("shader-organization", environ={"LOCALAPPDATA": str(tmp_path)}) == 5


def test_blockers_cannot_be_wrapped_as_accepted_or_soft_accepted():
    blocker = reason("artifact-missing", "The path does not exist", location="artifact.path",
                     direction="Use the actual output path", target="Existing artifact", example="Omit an artifact that was not produced")
    for accepted, soft in ((True, False), (False, True)):
        with pytest.raises(ValueError, match="blocking"):
            build_verdict(accepted=accepted, soft_accepted=soft, level="test",
                          round_used=3, round_limit=3, reasons=[blocker])
    warning = {**blocker, "severity": "warning"}
    result = build_verdict(accepted=True, level="report-valid", round_used=1, round_limit=3, reasons=[warning])
    assert result["schema"] == "replaylab-review-verdict/v1"
    assert result["roundsLeft"] == 2 and result["budgetExhausted"] is False
