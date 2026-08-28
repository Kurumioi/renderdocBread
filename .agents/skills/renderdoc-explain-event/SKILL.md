---
name: renderdoc-explain-event
description: Analyze a RenderDoc EID, draw/dispatch, pass, or whole frame from capture evidence. Use for event behavior, pipeline input/output, pass flow diagrams, semantic EID labels, foldable pass organization, missing output, and the Explain Current Event button.
metadata:
  replaylab-category: event-pipeline
  replaylab-read-only: "false"
  replaylab-min-successful-tools: "1"
  replaylab-version: "1"
allowed-tools: inspect_event analyze_pixel get_draw_calls get_frame_summary get_draw_call_details get_pipeline_state get_bound_textures find_draws_by_shader find_draws_by_texture find_draws_by_resource get_textures get_buffers get_resources get_texture_info get_texture_minmax inspect_pixel_object enumerate_counters fetch_counters get_debug_messages get_action_timings classify_current_frame_passes apply_current_frame_pass_ai_proposals
---

# Explain an EID, pass, or frame

Own event/pipeline analysis at the scope requested by the user. A single-EID
question should stay narrow. A pass/frame organization request may inspect all
relevant actions and publish the existing capture-bound ReplayLab pass tree.

## Evidence and identity

- Bind every claim to the frozen capture session and exact EID or bounded EID
  set. Never silently follow a later UI selection.
- Use draw/dispatch details, pipeline state, bound resources, action hierarchy,
  and debug evidence to establish behavior. Names and shader semantics are not
  physical evidence by themselves.
- Separate observed facts, program-derived classification, and AI semantic
  proposals. Mark uncertainty explicitly and never invent a missing input,
  output, resource dependency, pass purpose, or algorithm.

## Analysis flow

1. **Choose scope.** Resolve single EID, pass/marker subtree, selected range, or
   whole frame. If multiple materially different scopes remain plausible, ask
   the supervisor for a structured user clarification.
2. **Collect structure.** For an EID, use `inspect_event`. For a pass/frame,
   enumerate the bounded action hierarchy. Use `classify_current_frame_passes`
   only when the task declares the `project` effect, because the current Host
   always stages the capture catalog before fixed classification. Pass
   `publish_to_ui` and `persist_project` explicitly from the task's declared
   `ui` and persistence outcomes; never rely on their effectful defaults.
   For an explicit picked-pixel native task, use the single bundled
   `analyze_pixel` transaction and stay bound to its frozen texture,
   subresource, coordinate, sample, and cutoff EID.
3. **Trace each relevant draw/dispatch.** Record action type, topology/counts,
   pipeline stages, render/depth targets, resources read/written, and observable
   stage interfaces. State what is unknown when the Host does not expose a
   value.
4. **Infer behavior cautiously.** Derive behavior from inputs, outputs, pipeline
   state, and validated shader evidence. If shader semantics are material but
   unavailable, return a bounded shader-specialist request containing EID,
   stage, shader identity, and the exact question. Consume only validated
   `renderdoc-shader-workbench` prerequisite results; do not imitate or invent a
   hidden shader specialist.
5. **Build the flow.** Emit a Mermaid or compact text graph whose nodes name
   exact EIDs/passes and whose edges cite an observed resource or stage handoff.
   Do not draw an edge solely because two events are adjacent.
6. **Organize the UI when requested.** Start from the fixed-program
   classification. Attach semantic labels only through
   `apply_current_frame_pass_ai_proposals`; these remain `AIProvisional` and
   must preserve the fixed tree, capture identity, and review status. Pass
   `publish_to_ui` and `persist_project` explicitly, and use the exact
   `classificationId` returned in this task. The
   existing tool publishes ReplayLab's foldable semantic-pass panel; it does
   not rewrite native RenderDoc Event Browser names or captured marker data.

## Result contract

Report the bound capture/EID scope, pass summary, per-event input/output and
behavior findings, resource-flow graph, shader delegation results or requests,
UI organization receipt, and unknowns. Every semantic label and graph edge must
name its supporting EID/tool evidence and confidence.

Completion requires exact scope, admitted capture evidence, traceable
conclusions, and no contradicted graph edge or label. For UI organization it
also requires the fixed classification identity and publication result; never
claim native EID/Marker renaming until a real Host tool returns that evidence.
