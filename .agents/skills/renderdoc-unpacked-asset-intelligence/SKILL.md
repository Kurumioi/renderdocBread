---
name: renderdoc-unpacked-asset-intelligence
description: Index, relate, report, and export explicitly authorized unpacked project assets. Use for active-project asset catalogs, capture-to-unpacked matching, Strong UEX Inventory reports, or exact cooked UE SkeletalMesh delivery to Blender; use renderdoc-resource-export for capture-bound reconstruction packages.
metadata:
  replaylab-category: unpacked-asset-intelligence
  replaylab-version: "6"
  replaylab-required-references: "references/inventory-and-relationships.md references/report-semantics.md references/uex-blender-delivery.md references/workflow-acceptance.md"
---

# RenderDoc unpacked asset intelligence

The mandatory `replaylab-core` owns templates, evidence, review severity,
correction budgets and reporting. Main/child wording below describes API mode;
the vendor-native Agent performs the same bounded work directly and uses its
own supplied question interface. Requirement lists are reasoning aids, not an
extra runtime ledger or a replacement for `report_task_result`.

Own durable knowledge about unpacked assets admitted by the active ReplayLab
project: inventory them, maintain evidence-backed relationships, answer bounded
asset queries, produce reproducible distribution reports, and deliver explicitly
selected cooked UE SkeletalMesh assets through the registered UEX-to-Blender
route. Reuse persisted observations instead of repeatedly walking unpacked trees.

This Skill is an adaptive playbook and acceptance contract, not an executable
pipeline. Its modes, stages, route names, and tool calls are optional guidance.
The ordinary child Agent may inspect, skip, reorder, combine, revisit, or
replace them when current evidence supports a better registered path. Only
genuine identity, data, and transaction dependencies are ordered. The Host
freezes project/asset identity, authority, effects, budgets, and receipts.
Choose only among the live Host's supported providers, routes and parameters;
fixed tool constraints remain binding. Failures remain typed observations in the
`plan -> act -> observe -> validate -> replan` loop.

Before acting, read all four mandatory references completely:

- `references/inventory-and-relationships.md` defines project scope, stable
  identities, incremental indexing, relationships, capture correlation, and
  cross-Skill child-receipt reuse.
- `references/report-semantics.md` defines the Strong Inventory gate,
  Role/Role+Weapon aggregation, distinct-Skeleton bone counts, animation and
  texture statistics, scene grouping, P90, and the portable report contract.
- `references/uex-blender-delivery.md` defines exact asset selection, the UEX
  bundle contract, `.uemodel`/GLB choice, shared rigs, material approximation,
  LOD layout, and Blender validation.
- `references/workflow-acceptance.md` defines the autonomous corrective loop,
  batch behavior, main-Agent decision rules, and completion gate.

These references are normative. A directory listing, plausible filename match,
partial `.blend`, or unverified spreadsheet is not a completed result.

## Ownership and routing

The main Agent is the workflow manager. It freezes the user goal, active
project, requested asset set, target deliverables, effect authority, and
cross-Skill schedule. An ordinary child Agent running this Skill executes only
its assigned asset stage and returns a child receipt with reusable evidence or
verified artifacts to the main Agent.

- The ordinary child Agent running this Skill owns active-project unpacked-asset
  inventory, relationships, statistics, exact UEX asset resolution, UEX report
  jobs, and UEX Blender bundle/assembly jobs for its assigned task.
- A child receipt from `renderdoc-explain-event` carries EID, pass, and frame-flow
  evidence.
- A child receipt from `renderdoc-shader-workbench` carries Shader algorithm,
  ABI, and readable Shader reconstruction evidence.
- A main-Agent-scheduled child running `renderdoc-resource-export` performs
  capture-bound mesh, texture, material, CBuffer, and Shader reconstruction into
  Unity or Blender packages. The current child may supply matching unpacked
  sources through its receipt, but must not imitate capture extraction, target
  adaptation, or final validation assigned to that later child.

For a mixed request, return the smallest evidence bundle that lets the main
Agent schedule the next ordinary child Agent. Do not call another child
implicitly, ask the user directly, broaden the asset set, or claim the overall
workflow is complete. If the transport exposes no typed yield field, preserve
the decision as an open requirement instead of inventing a schema field.

## Authority and evidence boundaries

Operate only on source roots, profiles, inventories, and output locations
admitted by the active project and Host. A free-form path from model text is not
authority. Never widen one character, folder, EID, or selected asset into a
full-game scan or export. Only process assets the user is authorized to inspect.

Use only providers in the registered live capability catalog; capability calls
such as `get_unpacked_asset_index_capabilities` and
`get_unpacked_asset_provider_capabilities` are examples for inspecting it. Do
not download, install, or execute an unregistered parser, UEX build, Blender
add-on, or converter.
Never expose AES keys, profile secrets, pak contents outside the admitted
artifact root, or unsanitized proprietary data in logs or reports.

Treat observed metadata, deterministic relationships, heuristic candidates,
and AI interpretation as separate evidence classes. Preserve null/`missing`
for unavailable cooked data: zero is a measured value and must never be used as
a substitute. A filename, suffix, nearby folder, or matching bone name is a
search hint rather than proof.

## Operating modes

Choose the narrowest mode that meets the assigned outcome:

- **Catalog status/query:** inspect existing project knowledge and answer a
  bounded query without rescanning or mutating the project.
- **Incremental indexing:** index only admitted roots and requested asset kinds,
  persist stable identities and relationships, and retain truncation/staleness
  receipts. The on-demand scan runs in qrenderdoc's persistent service
  (`ReplayLabInternal`) and checkpoints roughly every 90 seconds, so it
  survives the end of a task and a later task joins or resumes it instead of
  starting over. `get_active_unpacked_asset_index_status` reports
  `complete=false` plus `progress` for a checkpointed catalog and
  `backgroundJob` for the running scan; a checkpoint is queryable and its
  candidates are valid partial evidence (`partialCatalog` in retrieval
  results). Never start a second scan while `backgroundJob.status="running"`.
- **Capture correlation:** rank unpacked candidates against admitted
  capture/Shader/resource evidence and return exact, ambiguous, and unresolved
  matches without exporting the capture. For "which asset is this shader?"
  use `retrieve_shader_asset_candidates(event_id, stage, …)`: it is a
  retriever (bound-texture names, resource names, pass/mesh hints → weighted
  tokens → ranked documents with consistency checks), not a hash gate, so it
  works for engines whose shader binaries are compressed or absent from the
  tree (Unreal `.ushaderbytecode`, for example). The catalog is generic
  first: every small file of any engine gets the same content probe —
  readable identifiers and paths, distinctive numbers, embedded images
  (fingerprinted), embedded shader containers, and name mentions resolved
  to other files as `mentions-asset` relations — so retrieval works on an
  unrecognized engine. A recognized engine only adds precision on top
  (Unreal today: exact import tables, mounts, classes, shader types, mip
  tables for texture fingerprints); `coverage.engine` says which layer
  applied. Record the decision with `submit_shader_asset_resolution` (or
  `reject=true`) — it is remembered per game build by shader hash and by
  structural shape, and `retrieve_…` returns that memory first. Hash-level
  identity (`query_active_shader_asset_evidence`) remains the strongest
  signal when the catalog did index shader binaries.
- **Distribution report:** generate the portable Strong-Inventory report with
  the project grouping specification and fixed statistical semantics.
- **UEX Blender delivery:** resolve exact cooked SkeletalMesh paths, export one
  self-contained bundle per selected asset, assemble compatible parts, and
  validate the final `.blend`.
- **Batch asset work:** resolve a bounded explicit set, deduplicate shared
  dependencies, run independent jobs once, and report per-item state plus the
  aggregate outcome.

## Optional stage and route map

Use the following as a map of possible subgoals, not a checklist. Skip outcomes
already proven by immutable receipts, change tools/providers when diagnostics
justify it, and return to an earlier assumption when validation contradicts it.

1. **Project and capability binding:** bind the active project and frozen
   request, then inspect the relevant index/provider capabilities when they are
   not already known. For UEX work, compare the available Inventory evidence
   and resolve exact cooked identities plus a validated `exportAssetPath`; the
   Agent chooses the suitable provider rather than accepting a Host choice.
2. **Snapshot reuse:** inspect active index status and reuse a compatible
   complete snapshot. Index only when the requested knowledge is absent, stale,
   truncated, or explicitly being refreshed and project mutation is admitted.
3. **Incremental indexing:** when required, index the smallest admitted roots and
   categories. Preserve snapshot/provider/spec identities, coverage, warnings,
   truncation, and content hashes; never report an incomplete scan as complete.
4. **Resolution:** query to narrow candidates and resolve exact assets plus
   transitive relationships. Keep ambiguity open when multiple candidates remain.
5. **Capture correlation:** prepare project-backed capture state only when it is
   explicitly authorized and still required. Consume
   `query_active_shader_asset_evidence` and/or `inspect_resource_export` for the
   frozen capture/EID. Record the evidence intersection and hand the ranked
   match set to the main Agent; do not turn a match into capture-export
   authority.
6. **Report or delivery job:** for a report, require a concrete complete Strong
   Inventory and validated project grouping rules before one
   `start_unpacked_asset_report` call. For a
   Blender deliverable, require exact virtual SkeletalMesh paths and a coherent
   variant before one `start_unpacked_asset_blender_export` call per admitted
   logical job.
7. **Continuation:** observe only the returned jobs until each
   is terminal. Queued or running is not a reason to start a duplicate job.
   When an exact Blender job is terminal `partial`, use only
   `continue_unpacked_asset_blender_export` with a fresh operation ID and the
   returned parent request/result hashes. Do not pass old artifact paths or
   manually resubmit successful assets; the Host derives and retains them.
8. **Acceptance:** validate the mode-specific artifacts and receipts. Replan
   only failed or still-open acceptance criteria, retaining all accepted
   evidence and successful child jobs.

## Batch and corrective behavior

Batch membership must be explicit or deterministically resolved from an
explicit bounded query. Group multipart characters by logical assembly, keep
independent Pet/Prop/Summon assets independent, choose one coherent replacement
variant, and deduplicate materials, textures, Skeletons, and already completed
work by stable identity. Never infer that similarly named assets belong to one
batch.

On a partial batch failure, preserve completed items and retry only failed
items when the Host proves the old effect did not complete or supplies an
idempotent continuation. The managed Blender continuation is that boundary:
it re-hashes parent bundles, retries only failed/open items, and combines old
and new manifests before shared assembly/validation. Do not restart a report or export after an uncertain
mutation. Use a declared fallback only when it preserves the user's required
outcome; GLB, partial Inventory, separate armatures, and a capture-only package
are not silent equivalents to the stricter requested result.

Yield to the main Agent when a remaining choice changes asset scope, coherent
variant, target deliverable, report completeness, approximation, external
effect, or which evidence will be discarded. Include the accepted evidence,
open criterion, decision owner (`main Agent` or `user`), smallest Host-eligible
options, recommendation when evidence supports one, and exact continuation
point. Main-Agent-owned choices already inside frozen authority must not
interrupt the user.

## Completion

Report the active project and immutable index/Inventory snapshot, providers and
capability fingerprints, requested and resolved assets, relationship and match
evidence with confidence, batch item states, generated artifacts and hashes,
validation results, warnings, truncation, approximations, and unresolved
unknowns. A report requires its completion manifest; a Blender result requires
its assembly and validation receipts. Completion is allowed only when every
required criterion in `references/workflow-acceptance.md` is accepted and no
pending main-Agent decision, ambiguous asset, uncertain effect, or critical
validation failure remains.
