from __future__ import annotations

import pytest

from replaylab_agent.errors import PolicyError
from replaylab_agent.policy import ToolCallScope


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
    with pytest.raises(PolicyError, match="texture selection changed"):
        scope.validate_live_context(
            {
                **CAPTURE_CONTEXT,
                "events": {"currentEventId": 9},
                "texture": {
                    "resourceId": 99,
                    "pickedPixel": {"x": 3, "y": 4},
                    "subresource": {"mip": 1, "slice": 2, "sample": 0},
                },
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
    with pytest.raises(PolicyError, match="resource/entry/raw identity changed"):
        scope.validate_live_context(
            {
                **CAPTURE_CONTEXT,
                "events": {"currentEventId": 12},
                "shaders": {
                    "bound": [
                        {
                            "stage": "fragment",
                            "resourceId": 78,
                            "entryPoint": "main",
                            "rawSha256": "a" * 64,
                        }
                    ]
                },
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
