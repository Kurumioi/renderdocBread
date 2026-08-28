from __future__ import annotations

import pytest

from replaylab_agent.errors import EvidenceValidationError, PolicyError
from replaylab_agent.policy import ToolCallScope, WorkflowGuard


CAPTURE_CONTEXT = {
    "capture": {"loaded": True, "captureSessionId": "b" * 32, "path": "C:/x.rdc"}
}


def test_button_scope_requires_complete_frozen_identity() -> None:
    with pytest.raises(PolicyError, match="missing its frozen event"):
        ToolCallScope.from_request(
            CAPTURE_CONTEXT, {"kind": "explain-current-event", "arguments": {}}
        )
    with pytest.raises(PolicyError, match="pixel intent is missing"):
        ToolCallScope.from_request(
            {**CAPTURE_CONTEXT, "events": {"currentEventId": 9}},
            {"kind": "inspect-picked-pixel", "arguments": {"eventId": 9}},
        )


def test_unknown_native_intent_keeps_capture_guard_without_an_enum_dead_end() -> None:
    scope = ToolCallScope.from_request(
        {**CAPTURE_CONTEXT, "events": {"currentEventId": 17}},
        {"kind": "future-renderdoc-inspection", "arguments": {"eventId": 17}},
    )

    assert scope.intent_kind == "future-renderdoc-inspection"
    assert not scope.exact_target
    scope.validate("get_pipeline_state", {"event_id": 29})
    scope.validate_live_context(CAPTURE_CONTEXT)


def test_message_scope_freezes_capture_but_keeps_selection_as_an_evidence_anchor() -> None:
    scope = ToolCallScope.from_request(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 21},
            "texture": {
                "resourceId": 42,
                "pickedPixel": {"x": 3, "y": 4},
                "subresource": {"mip": 1, "slice": 2, "sample": 0},
            },
            "shaders": {
                "focused": {
                    "stage": "pixel",
                    "resourceId": 77,
                    "entryPoint": "main",
                    "rawSha256": "a" * 64,
                }
            },
        },
        None,
    )

    assert scope.intent_kind == ""
    scope.validate("get_pipeline_state", {"event_id": 21})
    scope.validate("get_texture_info", {"resource_id": 42})
    scope.validate("reverse_shader", {"event_id": 21, "stage": "fragment"})

    # A general Event/Pipeline task may inspect a bounded pass or frame. The
    # submitted selection stays available to the specialist, but it is not a
    # per-tool permission lock.
    scope.validate("get_pipeline_state", {"event_id": 22})
    scope.validate("get_texture_info", {"resource_id": 99})
    scope.validate("reverse_shader", {"event_id": 22, "stage": "vertex"})

    # Moving the UI after submission is allowed. Tool arguments remain bound to
    # the frozen message snapshot while the live capture session stays the same.
    scope.validate_live_context(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 99},
            "texture": {"resourceId": 99},
        }
    )


def test_pixel_scope_binds_event_resource_coordinate_and_sample() -> None:
    scope = ToolCallScope.from_request(
        CAPTURE_CONTEXT,
        {
            "kind": "inspect-picked-pixel",
            "arguments": {
                "eventId": 9,
                "textureResourceId": 42,
                "pixel": {"x": 3, "y": 4},
                "subresource": {"mip": 1, "slice": 2, "sample": 0},
            },
        },
    )
    scope.validate(
        "debug_pixel", {"event_id": 9, "x": 3, "y": 4, "sample": 0}
    )
    scope.validate(
        "inspect_pixel_object",
        {
            "event_id": 9,
            "texture_id": 42,
            "x": 3,
            "y": 4,
            "mip": 1,
            "slice": 2,
            "sample": 0,
        },
    )
    scope.validate_live_context(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 9},
            "texture": {
                "resourceId": 42,
                "pickedPixel": {"x": 3, "y": 4},
                "subresource": {"mip": 1, "slice": 2, "sample": 0},
            },
        }
    )
    with pytest.raises(PolicyError, match="frozen event"):
        scope.validate(
            "debug_pixel", {"event_id": 10, "x": 3, "y": 4, "sample": 0}
        )
    with pytest.raises(PolicyError, match="frozen resource"):
        scope.validate("get_texture_info", {"resource_id": 99})
    with pytest.raises(PolicyError, match="frozen event"):
        scope.validate(
            "inspect_pixel_object",
            {
                "event_id": 10,
                "texture_id": 42,
                "x": 3,
                "y": 4,
                "mip": 1,
                "slice": 2,
                "sample": 0,
            },
        )
    # Navigation is not a capture swap and must not invalidate frozen work.
    scope.validate_live_context(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 100},
            "texture": {"resourceId": 99},
        }
    )


def test_shader_scope_requires_exact_raw_identity_and_binds_stage() -> None:
    intent = {
        "kind": "reverse-edit-selected-shader",
        "arguments": {
            "eventId": 12,
            "stage": "pixel",
            "shaderResourceId": 77,
            "entryPoint": "main",
            "rawSha256": "a" * 64,
        },
    }
    scope = ToolCallScope.from_request(CAPTURE_CONTEXT, intent)
    scope.validate("reverse_shader", {"event_id": 12, "stage": "fragment"})
    scope.validate_live_context(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 12},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": 77,
                        "entryPoint": "main",
                        "rawSha256": "a" * 64,
                    }
                ]
            },
        }
    )
    with pytest.raises(PolicyError, match="shader stage"):
        scope.validate("reverse_shader", {"event_id": 12, "stage": "vertex"})
    scope.validate_live_context(
        {
            **CAPTURE_CONTEXT,
            "events": {"currentEventId": 99},
            "shaders": {"bound": []},
        }
    )


def test_scope_rejects_capture_swap_between_tool_calls() -> None:
    scope = ToolCallScope.from_request(
        CAPTURE_CONTEXT,
        {"kind": "explain-current-event", "arguments": {"eventId": 7}},
    )
    scope.validate_live_context(CAPTURE_CONTEXT)
    with pytest.raises(PolicyError, match="capture session changed"):
        scope.validate_live_context(
            {
                "capture": {
                    "loaded": True,
                    "captureSessionId": "c" * 32,
                    "path": "C:/other.rdc",
                }
            }
        )


def test_scope_accepts_lossy_unicode_path_for_the_same_capture_session() -> None:
    frozen = {
        "capture": {
            "loaded": True,
            "captureSessionId": "d" * 32,
            "captureId": "capture-6",
            "path": "D:/Study/抓帧/白银之城/6.rdc",
        }
    }
    scope = ToolCallScope.from_request(
        frozen,
        {"kind": "explain-current-event", "arguments": {"eventId": 13185}},
    )

    scope.validate_live_context(
        {
            "capture": {
                "loaded": True,
                "captureSessionId": "d" * 32,
                "captureId": "capture-6",
                "path": "D:/Study/ץ֡/����֮��/6.rdc",
            }
        }
    )

    with pytest.raises(PolicyError, match="capture session changed"):
        scope.validate_live_context(
            {
                "capture": {
                    "loaded": True,
                    "captureSessionId": "e" * 32,
                    "captureId": "capture-6",
                    "path": "D:/Study/抓帧/白银之城/6.rdc",
                }
            }
        )


def test_workflow_guard_uses_effect_outcomes_not_button_permissions(tmp_path) -> None:
    guard = WorkflowGuard({"workspace", "replay"})
    workspace = str(tmp_path / "shader-workspace")
    apply_args = {"workspace_path": workspace, "event_id": 12}

    with pytest.raises(PolicyError, match="validate.*first"):
        guard.check("apply_shader_workspace_candidate", apply_args)

    validate_args = {"workspace_path": workspace}
    guard.check("validate_shader_workspace_candidate", validate_args)
    guard.record_result(
        "validate_shader_workspace_candidate",
        validate_args,
        {"validation": {"replayAbiValidated": True}},
    )
    guard.check("apply_shader_workspace_candidate", apply_args)
    guard.record_result(
        "apply_shader_workspace_candidate",
        apply_args,
        {"applied": True, "validation": {"replayAbiValidated": True}},
    )
    guard.validate_completion()


def test_workflow_guard_requires_effect_postconditions_and_recovery(tmp_path) -> None:
    guard = WorkflowGuard({"workspace", "replay"})
    workspace = str(tmp_path / "shader-workspace")
    validate_args = {"workspace_path": workspace}
    apply_args = {"workspace_path": workspace, "event_id": 12}
    guard.record_result(
        "validate_shader_workspace_candidate",
        validate_args,
        {"validation": {"replayAbiValidated": True}},
    )
    guard.record_failure("apply_shader_workspace_candidate")
    with pytest.raises(EvidenceValidationError, match="rollback"):
        guard.validate_completion()

    rollback_args = {"workspace_path": workspace, "replacement_id": "receipt"}
    guard.check("rollback_shader_workspace_override", rollback_args)
    guard.record_result(
        "rollback_shader_workspace_override",
        rollback_args,
        {"operation": "rollback", "restored": True},
    )
    guard.validate_completion()


def test_pass_proposals_must_reference_this_tasks_fixed_classification() -> None:
    guard = WorkflowGuard({"project", "ui"})
    proposal_args = {
        "classification_id": "classification-1",
        "expected_capture_id": "a" * 64,
        "expected_capture_session_id": "b" * 32,
        "proposals": [],
        "publish_to_ui": True,
        "persist_project": False,
    }
    with pytest.raises(PolicyError, match="classificationId"):
        guard.check("apply_current_frame_pass_ai_proposals", proposal_args)

    guard.record_result(
        "classify_current_frame_passes",
        {"publish_to_ui": False, "persist_project": False},
        {
            "classificationId": "classification-1",
            "panel": {"publishRequested": False, "published": False},
            "projectKnowledge": {"persistRequested": False},
        },
    )
    with pytest.raises(EvidenceValidationError, match="ui"):
        guard.validate_completion()
    guard.check("apply_current_frame_pass_ai_proposals", proposal_args)
    with pytest.raises(PolicyError, match="does not match"):
        guard.record_result(
            "apply_current_frame_pass_ai_proposals",
            proposal_args,
            {
                "classificationId": "classification-2",
                "panel": {"publishRequested": True, "published": True},
                "projectKnowledge": {"persistRequested": False},
            },
        )
    guard.record_result(
        "apply_current_frame_pass_ai_proposals",
        proposal_args,
        {
            "classificationId": "classification-1",
            "panel": {"publishRequested": True, "published": True},
            "projectKnowledge": {"persistRequested": False},
        },
    )
    guard.validate_completion()


def test_fixed_pass_classification_cannot_bypass_project_effect() -> None:
    guard = WorkflowGuard()
    with pytest.raises(PolicyError, match="project"):
        guard.check(
            "classify_current_frame_passes",
            {"publish_to_ui": False, "persist_project": False},
        )
