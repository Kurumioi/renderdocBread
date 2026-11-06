---
name: renderdoc-explain-event
description: Explain a RenderDoc EID, pass, or frame from captured evidence; create clickable frame rendering diagrams in Bread, investigate missing output, or organize passes.
metadata:
  replaylab-category: event-pipeline
  replaylab-version: "31"
---

# Explain captured events

The Agent chooses the scope, tools and presentation for the current capture;
never inherit another capture's EIDs or conclusions.
No required phase prefix.

For annotated/chat questions, read [visual-explanations.md](references/visual-explanations.md)
to choose images, code, local graphs and interactive meshes according to the user's
actual purpose, including vertex/compute debugging and Unity restoration.

For a local object/color/algorithm/effect question, use `publish_visual_reply` for
chat-local images, comparisons and a small multi-pass diagram with RT thumbnails,
calculation notes and evidenced arrows. Its expanded viewer is independent. Never
replace the global Frame Rendering Flow or its capture sidecar for a local explanation.
Show important algorithms as language-tagged code blocks or saved-source code cards,
with exact Shader Workspace line links. Use `get_cbuffer_parameter_link` for reflected
parameter/component links. Keep annotation coordinates tied to their output EID/RT and
display orientation. A flipped earlier HDR target is not the same image basis as the
final presentation. Verify the full and ROI pictures actually contain the marked target;
never interpret a background ROI as evidence that an effect is inactive. Prefer observing
the annotated final output; map coordinates explicitly before using a different RT.
Establish a cause with `run_shader_effect_experiment`
where supported: same output event/region, unchanged-source baseline control, stable
A/B/A, physical ABI checks and confirmed restoration. Publish its exact image pair
and experimentReceipt for a verified slider. Different EIDs only compare stages.
Rank the main observed causes; briefly mention a relevant disabled/low-impact
mechanism with evidence, without presenting it as this frame's active contribution.
Material/specular color, lighting and post effects can overlap; revise a hypothesis
when depth, meshes, resource usage, mathematics or controlled results contradict it.

Only for an explicitly requested whole-frame diagram, use `prepare_frame_render_flow` and `get_frame_render_flow` for the bound outline,
then `publish_frame_render_flow(publication_scope='frame')` for Bread's Frame Rendering Flow window. Read
[references/frame-rendering-flow.md](references/frame-rendering-flow.md) for
grouping, images and navigation, and
[references/algorithm-investigation.md](references/algorithm-investigation.md)
for tracing key results and composition. Read
[module discovery](references/module-discovery.md) to understand the rendering path before grouping,
and [render-path review](references/render-path-review.md) to test that explanation.
For new analysis or an explicit redraw, use `prepare_frame_render_flow(reanalyze=True)` and
build a new draft from raw evidence; old stages are only a later comparison.
Recheck important conclusions and dependencies, not just flagged gaps or layout;
re-publishing the baseline is not reanalysis.
Follow [reanalysis](references/reanalysis.md) for a fresh assessment and dependency disputes.
Use section="proposals" and section="quality" for the new draft.
Reuse verified static bindings, source and PNGs first; static evidence cannot answer everything.
For Vulkan indirect arguments, `inspect_event_api_parameters` can read the exact
pre-action argument buffer when a single command lacks structured decoding; the
prior replay event is restored. A generic current-state buffer read is different
evidence. A resolved zero workload is an observed no-op: explain its place in the
sequence without claiming an executed contribution, and continue analysis of the
other stages. Retain unknown multi-command child selection as a precise limit,
not a reason to repeat an unchanged full-frame investigation.
An empty pipeline read is not new evidence: retain valid bindings and change the
failed route; never call reused evidence a new validation.
Choose modules, EIDs and relevant RTs from this capture's execution order and
resource flow. Historical EIDs and example techniques are not required targets
or a module checklist. Trace key output versions backward through actual Shader
reads and effective writes, then forward to their downstream use. Respect
subresources, partial writes and history boundaries; cache shared dependencies.
Record sources, calculations and integration for verification, not UI detail panels.
Nodes contain a short plain-language summary, without Inputs/Calculation/Outputs
labels or calculation-details sections. The main sequence follows execution;
a horizontal processGraph shows verified cross-RT handoffs, branches and merges, with a texture
per node. Single-draw arithmetic, parallel MRTs and repeated snapshots are not chains.
Utility-only stages need no invented Shader algorithm tables.
The outline is not semantic analysis. Explain techniques and contributions,
not draw batches. Resolve core purposes during construction; range coverage
does not establish understanding. `get_frame_render_flow(section="candidates")`
keeps the source grouping available after drafts. Continue independent analysis
while recovering a save/display failure. `quality.analysisComplete=false` remains
unfinished; preserve real evidence limits.

Actually view the important RT image content, not just its path or JSON receipt.
Cross-check visual clues with relevant mesh shape/layout, VS deformation,
culling/depth/stencil/blend state, Shader mathematics and resource versions.
Asset names are clues, not proof. Choose the evidence needed for the question.
Read [visual evidence](references/visual-evidence.md) for images, exposure and comparisons.
Keep important MRTs together, fold secondary textures and show channels in a popup.

Use read-only Shader source/disassembly, without creating editable workspaces;
do not switch to Shader organization to satisfy a gate.

For subtle composition changes, prefer a draggable before/after comparison with
matched display mapping. Other comparisons and animations are optional.
Declare channelGroups by purpose with short names and explanations below;
publication pre-generates and persists them.
Name stages by function and textures by their meaning, never by
EID/ResourceId. Use evidenced technical terms with plain-language explanations.
Captions explain the texture's use and meaningful channel groups
(often RGB plus A), without repeating format/size or evidence disclaimers.
Resolve unclear channels through actual consumers; reuse their investigation.
RGB previews stay opaque, normally at 0–1; remap only unreadable views. Verify the final scene
and UI composition rather than showing an isolated UI layer as the final frame.

Use `inspect_event` for one EID. Check persistence separately from window display.
Pass labels remain AIProvisional with publish_to_ui=false; the semantic pass panel is retired.

Separate facts, inferred purpose and unresolved meaning. Bound outputs are
candidates, not an `observed-contributor`. Raw device depth is not linear depth;
equal PNGs do not prove equal resource bytes. Slots, colors or names alone
do not establish GBuffer channel meaning.

Review the displayed rendering explanation, not only coverage and delivery.
Use render-path-review.md to investigate omissions, revise and review again;
reuse evidence in `record_task_review`. Keep limits in the review, not every card.
A useful partial result states material gaps once; never claim unfinished work complete.
