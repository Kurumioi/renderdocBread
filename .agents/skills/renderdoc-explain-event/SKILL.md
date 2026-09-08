---
name: renderdoc-explain-event
description: Analyze a RenderDoc EID, draw/dispatch, pass, or whole frame from capture evidence. Use for event behavior, pipeline input/output, pass flow diagrams, semantic EID labels, foldable pass organization, missing output, and the Explain Current Event button.
metadata:
  replaylab-category: event-pipeline
  replaylab-version: "3"
  replaylab-required-references: "references/visual-evidence.md references/workflow-acceptance.md"
---

# Explain an EID, pass, or frame

Own event/pipeline analysis at the user's requested scope. Single-EID questions
stay narrow; pass/frame requests may inspect relevant actions and publish the
existing capture-bound ReplayLab pass tree.

The mandatory `replaylab-core` supplies evidence, templates, errors, bounded
review and reporting rules. The loaded references add only Event procedures:

- `references/visual-evidence.md`: RT/DSV views, channels, depth, GBuffer
  semantics and the fixed Host renderer.
- `references/workflow-acceptance.md`: scope-specific completion criteria.

## Execution role and scope

In API mode a generic child Agent completes its bounded Event outcome and
returns findings to the main Agent. If shader interpretation needs a different
domain, return the exact EID/stage/shader and question for the main Agent's
shader task. Children do not spawn another Agent or dynamically switch Skills.
A native Agent works directly with its granted Host tools and loaded procedures.

Choose useful reads, representative EIDs and safe recovery yourself. The normal
`plan -> act -> observe -> validate -> replan` reasoning is the selected
Agent's responsibility, not a second Host workflow. Tool scope, effect grants
and budgets remain binding. Any route example is advisory telemetry only.

Resolve the requested stage from explicit wording, the frozen UI intent, then
actually bound compatible stages. If one candidate remains, select it and
continue. Never add an absent stage or resource to make a list of options.
Only a material unresolved choice belongs to the user; an API child reports
it to the main Agent, and a native Agent uses its provided question interface.

## Evidence and identity

- Treat ReplayLab as an evidence-producing replay debugger. Fixed Host/bridge
  code owns capture identity, physical pipeline/resource facts, actual-work
  accounting, pass classification, and UI mutation. The child Agent owns
  explanations, secondary grouping, and explicitly `AIProvisional` semantic
  proposals only.
- Use the frozen context supplied by the task or immutable native intent.
  If a free-form capture-dependent task arrives without frozen context, request
  Host resolution through `get_current_renderdoc_context` before collecting
  evidence. Resolve deictic words such as "current" or "this" and a Shader
  stage in this order: explicit request wording, a stage-scoped frozen UI
  intent, then the sole compatible bound stage. Ask only when multiple
  compatible stages remain. Never replace a frozen scope with a later UI
  selection or ask the user to repeat an already resolved EID.
- Bind every claim to the frozen capture session and exact EID or bounded EID
  set. Never silently follow a later UI selection.
- Use draw/dispatch details, pipeline state, bound resources, action hierarchy,
  and debug evidence to establish behavior. Names and shader semantics are not
  physical evidence by themselves.
- Separate observed facts, program-derived classification, and AI semantic
  proposals. Mark uncertainty explicitly and never invent a missing input,
  output, resource dependency, pass purpose, or algorithm.

## Analysis flow

The following entries are an evidence playbook, not a required phase prefix or
tool sequence. Enter only the entries that advance an open outcome; combine,
reorder, revisit, or skip them as observed facts justify. Preserve the concrete
causal rules inside an entry (for example, a comparison needs compatible
before/after evidence), but do not treat the numbering as Host admission.

1. **Choose scope.** Resolve single EID, pass/marker subtree, selected range, or
   whole frame from frozen intent and capture context. Inspect first when a
   bounded read can disambiguate. If one compatible scope remains, select it.
   Escalate only when multiple materially different, evidence-backed scopes
   remain and the choice changes the requested outcome.
2. **Collect a useful composite when it has the best evidence gain.** For a
   single EID, `inspect_event` is often useful; for a whole frame, pass, marker
   subtree, or broad EID range, one bounded `analyze_current_frame_flow` call is
   often more efficient than reconstructing the frame from unrelated point
   queries. These are suggestions, not mandatory first calls. Keep
   its default `max_actions=8000`; raise it deliberately, never above 100000,
   only when the requested scope requires that cost. Treat its strict
   `executionTimeline` as the canonical chronological order for all admitted
   positive EIDs, including Marker/container context, and use
   `actionExecutionTimeline` for the Draw/Dispatch/Utility-only order. Treat its
   items, workload, I/O, draw state, participation flags, and resource edges as
   an inventory, not as permission to invent semantics. Honor its action and byte
   `truncated`/omitted receipts. Use targeted reads afterwards only for material
   gaps or representative events. For an explicit picked-pixel native task, use
   the single bundled `analyze_pixel` transaction and stay bound to its frozen
   texture, subresource, coordinate, sample, and cutoff EID.
3. **Classify actual work conservatively.** Give every relevant EID exactly one
   `actual-work` state:
   - `observed-contributor` only when admitted evidence proves that the action
     changed or contributed to a resource or pixel, such as pixel history, a
     before/after resource difference, or a contribution counter. A bound
     output, declared write, or draw/dispatch opcode alone is insufficient;
   - `output-affecting-candidate` when an issued non-zero direct action (or an
     indirect action whose arguments remain unresolved) has a bound output or
     declared write, or when an issued copy/clear/resolve has a destination,
     but no observation proves contribution; and
   - `not-established` when neither condition is proven. This includes markers,
     state-only actions, zero-sized work, and any action whose contribution is
     unknown. Say `non-contributing` only when explicit negative evidence proves
     that stronger claim.
   Preserve the composite participation evidence (`issuedAction`,
   `outputAffectingCandidate`, and `observedContributor`) behind the state. Never
   promote a candidate to an observed contributor from names, adjacency, or a
   plausible shader purpose.
4. **Trace actual draw/dispatch workload in EID order.** For every included
   action record the action kind; direct/indirect, indexed/instanced, vertex or
   index counts, dispatch dimensions, and topology when exposed; stage and
   shader identities; vertex/index/indirect inputs and shader-visible CBV, SRV,
   UAV, sampler, texture, and buffer inputs; RTV, DSV, UAV/storage, copy, and
   present outputs; formats/subresources; and the viewport/scissor, raster,
   cull, depth/stencil, blend, write-mask, sample, and attachment state that can
   change the result. Distinguish absent, disabled, and not exposed. Keep the
   chronological timeline intact even when also grouping repeated EIDs under a
   pass, marker, material, or shader summary.
5. **Build resource flow, not adjacency.** A producer-to-consumer edge requires
   an observed resource identity and compatible write/read or stage handoff;
   include producer EID, consumer EID, resource ID and subresource/range when
   exposed. Preserve external inputs, terminal outputs, read-modify-write and
   ambiguous aliasing as explicit boundary or unknown records in the
   chronological table/unknowns; the E/P graph grammar does not invent boundary
   nodes for them. Never create a
   dependency edge merely because EIDs are consecutive, share a label, or bind
   similarly. Report composite truncation before drawing conclusions about the
   omitted tail.
6. **Apply representative Shader analysis.** When semantics decide pass
   purpose, discard/output behavior, resource meaning or missing output, inspect
   representative shaders with the granted Host tools. In API mode request
   any additional shader-domain work through the main Agent. Group repeated
   uses by exact shader identity/stage and materially different workload,
   resource or state. Pass the representative EID, covered EIDs, bound stage,
   resource identity and one concrete question, such as output packing or
   discard coverage. Rewording the same question is duplicate work. Integrate
   the returned observations and keep unsupported meanings unknown.
7. **Build safe, bounded flow views.** Use
   `render_current_frame_flow_diagram` and its fixed Host templates for
   requested diagrams. The Host derives graph facts from the selected capture
   scope; the model supplies the supported scope/kind parameters, not raw
   Mermaid or its own graph grammar. Follow `references/visual-evidence.md`.
   Only definite resource handoffs become dependency arrows. When a rendered
   artifact is unavailable, preserve useful facts in a chronological table and
   mark that presentation outcome unmet; do not invent a substitute renderer.
8. **Organize the UI only when requested.** Treat “整理整个 EID 流程”,
   “只看实际工作”, “按 Pass/阶段折叠”, “按 Unity Frame Debugger 整理”, and
   equivalents as explicit requests for the foldable semantic-pass panel. This
   authorizes `ui` publication only. The default panel path is session-bound and
   display-only: it must not stage/import a capture catalog, prepare an active
   project, or write pass knowledge. It does not authorize durable project
   persistence or semantic overlays unless those outcomes were separately
   requested. Read-only frame/pass explanation uses
   `analyze_current_frame_flow` and does not call the effectful classifier; a
   narrow EID explanation remains on `inspect_event`. To create the foldable
   ReplayLab pass panel, the task declares `ui` and calls
   `classify_current_frame_passes` with `publish_to_ui=true` and
   `persist_project=false`. Require `project` and set `persist_project=true`
   only when the user explicitly asks to save the organization as durable
   project knowledge or reuse it across captures. Attach semantic labels only through
   `apply_current_frame_pass_ai_proposals`, using the exact `classificationId`
   returned in this task and explicit `publish_to_ui`/`persist_project` values.
   A display-only classification cannot be upgraded to persistence by a later
   proposal call; start a freshly Host-authorized project-backed classification
   when persistence was newly requested.

   Validate the effect receipt, not just a visible panel. A UI-only result has
   `captureBindingMode=display-session`, `effectMode=display-only`,
   `projectKnowledge.projectPrepared=false`, and
   `panel.publicationMode=display-only`. A successfully saved result has
   `captureBindingMode=project-catalog`, `effectMode=project-persisted`, and
   `panel.publicationMode=project-persisted`. `project-staged` proves that
   authorized preparation occurred but does not satisfy a requested persistence
   outcome; keep that requirement open unless `projectKnowledge.persisted=true`.
   Preserve the fixed category order `Depth`, `Shadow`, `BasePass`, `GBuffer`,
   `Lighting`, `Transparent`, `Post`, `UI`, `Compute`, `Utility`, and `Unknown`,
   together with Marker and `ExecuteIndirect` containers. Keep concrete
   indirect Draw/Dispatch children visible. Never present Marker or bookkeeping
   events as draws, and never delete real Clear, Copy, or Resolve work. The
   native panel projection is bounded to 2500 actions; report its source
   truncation and never persist a truncated classification or overlay as
   reusable project knowledge. These labels remain `AIProvisional` and must
   preserve the fixed tree, capture identity, chronological EID coverage, and
   review status. The tools publish a
   separate foldable semantic-pass panel; they do not rewrite native RenderDoc
   Event Browser names or captured marker data. Reuse bound-project pass
   fingerprints across captures only when fixed evidence agrees;
   `AIProvisional` agreement is never reuse authority.

## Compatibility and completion

- Preserve each requested RT, channel, depth range, diagram and UI/persistence
  outcome. Once observed, record its exact EID, binding/slot, resource,
  subresource, sample and display range. A different channel or range does not
  fulfill that request. Report unavailable requested outputs explicitly.
- Treat absent, disabled, not exposed, unsupported, truncated and tool failure
  as different states. Never infer an API-specific stage from another API.
  Preserve compact `IA`, `VS`, `PS`, `OM`, `CS`, `VTX`, `FS`,
  `FB`, `TS`, `MS` and `AS` identifiers where applicable; never offer or
  analyze `CS` for a graphics draw merely because the API supports compute.
- Reuse successful exports, diagrams, project writes and UI publication.
  Inspect uncertain mutation state before retrying. A useful partial result
  with a bounded-unknown conclusion is valid; a budget ceiling is not proof
  that an omitted requested artifact was completed.
- Stop when the requested outcome has supporting evidence, or when only an
  explicitly named evidence/capability boundary remains. Follow the common
  Host correction budget; do not create a private retry or checkpoint protocol.

## Result contract

Use the role-specific report in `replaylab-core`. Lead with the result and
bound capture/EID scope. Include the relevant chronological EID table,
actual-work state, workload, inputs/outputs/state, resource handoffs,
representative shader findings, artifact/UI receipts, truncation and unknowns.
Use `EID | Marker | Action | Stage | Workload | State | Evidence` for timelines.

Copy Host participation flags and counts without promoting output candidates
to observed contributors. Each semantic label and graph edge cites supporting
EID/tool evidence, with inferred confidence where appropriate. Do not invent
`workflow_progress`, `delegation_requests`, a supervisor directive, or a
separate structured child-report schema: those retired runtime envelopes are
not current tool contracts.

For UI organization, completion also requires the fixed classification
identity and publication receipt. For requested durable saving, a visible
panel is insufficient: require the actual project-persistence outcome.
Apply the evidence criteria in `references/workflow-acceptance.md`; return a
useful partial result when a requested artifact or shader interpretation
remains unresolved.
