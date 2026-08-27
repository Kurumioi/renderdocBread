---
name: renderdoc-explain-event
description: Explain the currently selected RenderDoc draw, dispatch, or event using capture evidence. Use for “what does this draw do”, pipeline-state explanations, pass intent, missing output, and the Explain Current Event button.
metadata:
  replaylab-category: event-pipeline
  replaylab-read-only: "true"
  replaylab-min-successful-tools: "2"
  replaylab-required-all-tools: get_draw_call_details get_pipeline_state
  replaylab-version: "1"
allowed-tools: get_draw_call_details get_pipeline_state
---

# Explain a RenderDoc event

1. Treat the UI intent's context snapshot and event ID as authoritative. Do not silently switch events.
2. Read `get_draw_call_details` and `get_pipeline_state` for that exact event.
3. Describe what the event does, which resources and shaders matter, and what evidence supports each conclusion.
4. Separate captured facts from hypotheses. If evidence is insufficient, name the next bounded inspection rather than guessing.
5. End with a compact diagnosis or “no defect established”. Never mutate replay state from this skill.
