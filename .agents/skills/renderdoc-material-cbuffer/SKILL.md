---
name: renderdoc-material-cbuffer
description: Analyze material-related pipeline bindings and bound textures at a RenderDoc event. Use for wrong shader/texture/slot diagnoses; reflected CBuffer leaf values are not available in the current read-only Host contract.
metadata:
  replaylab-category: material-cbuffer
  replaylab-read-only: "false"
  replaylab-min-successful-tools: "2"
  replaylab-required-all-tools: get_pipeline_state get_bound_textures
  replaylab-version: "1"
allowed-tools: get_pipeline_state get_bound_textures
---

# Material binding analysis

1. Resolve the exact event, shader stage, binding slot/register space, and texture identity.
2. Compare pipeline binding evidence with the expected material role; do not claim to have read constant-buffer bytes or reflected leaf values.
3. Do not prepare a material session or write/rollback parameters in this v1 skill. Chat alone is never authorization.
4. If a diagnosis needs CBuffer values, report that the current Host lacks a bounded read-only value tool and request that capability rather than guessing.
5. End with a bounded binding diagnosis and leave replay state unchanged.
