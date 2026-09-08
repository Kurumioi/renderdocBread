from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaylab_agent.conversation import (
    AttachmentContext,
    ConversationAttachment,
    ConversationEnvelope,
    ProviderPathAttachment,
    prepare_provider_conversation_input,
    render_conversation_prompt,
)


def test_cpp_shader_selection_context_accepts_opaque_resource_and_stage_relation() -> None:
    context = AttachmentContext.model_validate(
        {
            "schema": "replaylab-attachment-context/v1",
            "origin": {
                "kind": "shader-disassembly-selection",
                "panel": "shader-viewer",
                "sourceId": "shader-source-" + "a" * 64,
                "sourceName": "Disassembly",
                "range": {"lineStart": 1, "columnStart": 1, "lineEnd": 2, "columnEnd": 3},
            },
            "capture": {"eventId": 7, "selectedEventId": 7},
            "shader": {"stage": "PS", "resourceId": "ResourceId::99"},
            "relations": [
                {
                    "id": "shader-binding-" + "b" * 64,
                    "kind": "bound-at-event",
                    "targetId": "ResourceId::pipe",
                    "usage": "PS",
                }
            ],
        }
    )
    assert context.shader is not None
    assert context.shader.resource_id == "ResourceId::99"
    assert context.relations[0].usage == "PS"


def test_path_reference_is_host_pinned_and_hidden_from_non_codex_projection(tmp_path: Path) -> None:
    path = tmp_path / "selected.hlsl"
    path.write_text("float4 main() : SV_Target { return 1; }", encoding="utf-8")
    details = path.stat()
    attachment = {
        "id": "selected",
        "kind": "file-ref",
        "title": "selected.hlsl",
        "mediaType": "text/x-hlsl",
        "source": "file-picker",
        "encoding": "path",
        "content": str(path),
        "originalBytes": details.st_size,
        "modifiedUnixMs": details.st_mtime_ns // 1_000_000,
        "truncated": False,
        "trust": "untrusted",
    }
    envelope = ConversationEnvelope.model_validate(
        {"schema": "replaylab-conversation/v5", "request": {"role": "user", "content": "Inspect it", "attachments": [attachment]}}
    )
    prompt = render_conversation_prompt(envelope)
    provider = prepare_provider_conversation_input(prompt, provider_name="ACP")
    assert str(path) not in provider.text
    assert json.loads(provider.text)["conversation"]["request"]["attachments"][0]["delivery"] == "metadata-only-unsupported"
    pinned = ProviderPathAttachment.from_conversation_attachment(envelope.request.attachments[0])
    assert pinned.path == path.resolve()
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        pinned.revalidate()
