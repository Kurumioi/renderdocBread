---
name: renderdoc-shader-workbench
description: Reconstruct, explain, organize, edit, compile, and replace a capture-bound RenderDoc shader. Use for readable HLSL/GLSL/native IR, ABI, bounded output comparison, project reuse, editable Shader Workspace, external CBuffer aliases, and Analyze Shader with RenderDoc AI.
metadata:
  replaylab-category: shader
  replaylab-version: "21"
---

# Shader workbench

You reconstruct or explain ONE capture-bound shader with the ReplayLab MCP
tools. Fixed Host programs perform reconstruction, compilation, ABI checks
and bounded output comparisons. Prefer available native compilable source,
then registered tools that directly reconstruct usable source. Bytecode does
not imply ABI repair: only a concrete failed interface check requires repair.
You organize, explain and publish an editable project result. Do not
hand-translate DXIL/DXBC into HLSL.

## 0. Decide the outcome first

- **Explain only** (the user asks what the shader does, its bindings or data
  flow, and did not ask for source/reconstruction/edit/replace): use
  `inspect_shader` and `get_current_shader_representation` (read-only) and
  report. Do not create a workspace.
- **Reconstruct / decompile / rebuild / readable HLSL** includes organized
  HLSL, analysis, a generated flow chart, project registration, and opening
  the Shader Workspace for editing, saving, compiling and replacement.
  Continue through §1–§2 unless the user explicitly asks for raw output only.
  This uses `workspace`, `project` and `ui`; review also requires `replay`
  because the Host may temporarily compare output and restore replay state.
  Respect the granted effect ceiling; never use another tool to evade it.
- **Existing user-edited workspace**: go directly to §4. Intentional user
  changes do not require another AI organization or equivalence review.
- The normal reconstruction request includes a working editable/save/compile/
  replace session: complete registration, initial checked shared-resource
  apply and live-edit start below. Explain-only or explicitly export-only
  requests do not start that session. Temporary comparison alone is never
  authority for unrelated replacement scopes or CBuffer value edits.

## 1. Reconstruction recipe (project, workspace and replay effects)

Every value you pass must come from a tool result of *this* task. Never guess,
retype or "correct" a hash or resource id by hand; copy it.

1. `get_current_renderdoc_context()` — confirms the capture, the current EID
   and the bound shaders. If the task names a stage/resource, use that; if two
   bound shaders both match the request, stop and report the two candidates
   (the main Agent asks the user).
2. `get_active_shader_workspace_path(event_id, stage)` — prepares only this
   current event/stage when no covering catalog exists, then returns the
   Host-managed workspace path **and the exact identity**: `captureId`,
   `captureSessionId`, `eventId`, `stage`, `entryPoint`, `shaderResourceId`,
   `rawSha256`, `workspacePath`. This is the only source of truth for the
   next call. A new capture with an empty UI `captureId` is supported directly.
   This step has `project` and temporary `replay` effects. It returns explicit
   `catalogScope`, `fullFrame` and covered event/stage fields; a one-event
   catalog never proves knowledge about the rest of the frame. Do not call
   `prepare_active_capture_project` as a prerequisite for ordinary shader
   reconstruction: that separate operation scans the entire frame. Reuse an
   existing full catalog when the Host already has one. If the current event
   changed before bootstrap, use the complete Host-returned frozen tuple only:
   `expected_resource_id`, `expected_entry_point`, `expected_raw_sha256`, and
   `expected_capture_session_id`. All four are required together; a validated
   inventory restore uses the same path for its representative EID. The Host
   checks that tuple against the one-event export and preserves the current UI
   event. If that evidence is unavailable, report the changed context; do not
   guess another EID or silently start a full-frame scan. The path is inside the replay project
   (`…\ReplayLab Project\workspaces\shaders\v2\<hash>`); never invent another
   path, never use your own working directory, never call
   `create_replay_project` or `create_shader_workspace`.
2b. **Asset memory before decompiling** (only when
   `get_active_unpacked_asset_index_status()` reports a configured root):
   call `retrieve_shader_asset_candidates(event_id, stage)` now. A remembered
   resolution, or a `clear leader` with strong `valueEvidence` (distinctive
   constant-buffer numbers such as 12.354754 shared with the asset's
   parameter data, especially adjacent runs), is submitted with
   `submit_shader_asset_resolution` right away; the Host then names CBuffer
   members from that material family's remembered rules during step 3's
   fixed repair, so the decompiled source already carries real names. If the
   set is ambiguous, defer the decision to step 4(b) after reading the code.
   `ready:false` with `indexing.status="running"` means the first-use index
   of a large tree is still building in the background (in qrenderdoc's
   persistent service, so it survives this task and checkpoints every ~90 s):
   do not wait — go on with steps 3–5 and call retrieval again before step
   4(b)/step 7 naming (after `retryAfterSeconds`). A result with
   `partialCatalog` comes from a checkpoint: use it now (partial evidence
   beats none) and retry once more before organizing. The asset line is never
   abandoned because indexing was running at the first call; "indexing still
   running" in your final report is only acceptable when you retried at the
   naming step and it was still incomplete.
   Read `limitations` in the result: it says what the asset tree could not
   provide (packed/encrypted files, compressed shader archives, deferred
   data) so a missing match is explained rather than guessed around.
3. `prepare_shader_workspace(event_id, stage, workspace_path,
   editable_format="hlsl", expected_resource_id=shaderResourceId,
   expected_entry_point=entryPoint, expected_raw_sha256=rawSha256,
   expected_capture_id=captureId,
   expected_capture_session_id=captureSessionId, admit_existing=true)` —
   all seven identity arguments are mandatory (the Host refuses a partial
   set). Pass `stage` exactly as step 2 returned it. The Host uses the available
   source or registered reconstruction route and creates `source/shader.hlsl`.
   A successful tool route needs no speculative ABI repair or AI-generated
   numerical tests. `admit_existing=true` re-opens a
   workspace that already exists for this shader instead of failing.
   If the result names a runtime route failure, call
   `try_shader_workspace_recovery_candidate` with one still-untried
   registered pair listed in that result, or `get_shader_workspace_representation`
   with `retry_decompile=true`; do not repeat the identical call.
   **Route memory.** `prepare_shader_workspace`, `create_shader_workspace` and
   `plan_shader_recovery` return `routeMemory`. When `routeMemory.applied` is
   true the Host already promoted the lane that worked (or skipped one that
   failed) for this game's shaders of the same encoding/stage/profile, so the
   primary route is the remembered one: do not try the catalog default "just
   in case". Every validated `apply_shader_workspace_shared_resource` and every
   rejected decompile is recorded automatically in the project
   (`shared/dictionaries/shaders/route-memory.json`); you never write it.
   **Location.** With an active project every workspace lives under its
   Shader workspace root: use the path from `get_active_shader_workspace_path`,
   put recovery attempts next to their source workspace, and never create a
   workspace under `%LOCALAPPDATA%` or elsewhere — the Host now refuses it and
   names the right place, because such a workspace can never be registered.
   If status reports `toolchainStatus="refresh-required"`, use its exact
   `requiredAction` arguments with `migrate_shader_workspace_toolchain` and
   `in_place=true`, naming the same existing source and target workspace. The
   Host preserves source and workspace identity, archives old tool provenance,
   and clears old candidate/ABI authority. Compile and validate the preserved
   source with the new binding; a tool upgrade does not require new AI
   organization. Use a separate copy migration only when an archive/successor
   is explicitly needed. When a legacy token-only
   `.workspace-watch.lock` blocks that migration, ask the user whether to take
   it over. Only after the user explicitly chooses migration, repeat that one
   call with `recover_legacy_source_lease=true`; the Host archives the old lock
   and still refuses malformed locks or source-transaction residue. Never
   delete or edit the lock yourself.
   Before invoking a decompiler, the Host now imports a unique active,
   raw-exact project source whose target profile and physical ABI match this
   capture. `projectSourceReuse` and `source.projectSource` identify that
   existing project artifact; no fresh decompiler output is needed. Preserve
   an existing user's edits. The imported source carries project provenance,
   not old replay authorization. The manual `seed_active_shader_workspace`
   tool remains for explicit legacy workflows; do not call it again after a
   successful direct project import.
4. `get_shader_workspace_status(workspace_path)` — confirms `source.present`,
   `source.format`, `source.lane` (`executable-editable` is the lane that can
   compile and publish). `read_shader_workspace_file(workspace_path,
   "source/shader.hlsl")` shows the decompiler output; use `offset`/`limit`
   for long files instead of asking for the whole thing.
   **Check the declarations against the capture before the first compile.**
   `source.hlslCBufferAbiRepair` says what the Host's fixed repair already
   did: `status="applied"` with `repairedBlocks` (cbuffers rewritten to the
   captured size/offsets) and `interfaceRepair.status` (`applied`, or
   `not-applied` with the exact declaration it could not admit in `reason`).
   When the interface repair is `not-applied`, or the record is missing for a
   DXIL→SPIR-V→HLSL route, compare `SPIRV_Cross_Input`/`Output`, every
   `cbuffer` and every resource declaration with `baseline.compiledInterface`
   (row order = `regIndex` order, types/widths, `byteSize`, member offsets,
   `space:bind`) and submit the corrected file through
   `repair_shader_workspace_source_abi` right away — one round trip instead of
   compile → ABI failure → repair.
   **Evidence order for declarations, names and repairs.** Read the cheap
   evidence first, then repair — never the other way round:
   (a) the captured reflection (`baseline.compiledInterface`) is the physical
   truth and the final judge for registers, offsets, sizes and types;
   (b) the project's unpacked assets, when
   `get_active_unpacked_asset_index_status()` says a root is indexed, are the
   most *complete* description — the original declarations including members
   the code never reads, real names, struct shapes, semantics. Two channels,
   both read before authoring any repair or rename:
   **Retrieval (no hash needed, any engine):**
   `retrieve_shader_asset_candidates(event_id, stage, pass_hint, mesh_hint)`
   ranks materials / material instances / shader sources from the catalog by
   evidence that survives shipping: texture *pixels* (content identity),
   constant-buffer *values*, parameter *structure* (cbuffer sizes, texture
   counts, remembered same-shape resolutions), draw *geometry* (skinned /
   static / instanced, index counts, `usedByMeshes`) and *code logic*
   (discard ↔ masked blend mode, sample counts). Texture and resource names
   are used too but only as reference — shipped captures usually have none,
   and a name-only match never exceeds 0.6. Each candidate carries
   `matchedTokens`, `consistency` checks, `facets`, a `document` (real
   parameter names, referenced textures, parent material, instances, meshes
   using it, shader types, defines) and `why`. `retrieval.debug` says per
   channel what the Host could and could not observe and lists `nextSteps`;
   read it when the leader is weak and adjust: give `extra_tokens` from the
   code, `pass_hint`/`mesh_hint`, query the catalog directly, or call again
   after `prepare_shader_workspace` so code hints exist. `ready:false` means
   no asset root is configured — continue without asset evidence. A
   remembered decision (`memory.resolved`)
   comes back first — use it, do not search again. No up-front indexing is
   required: the first call indexes the configured root at header depth and
   hydrates the candidates it needs (`indexing` in the result says what
   happened). Otherwise decide from the evidence: a `clear leader` whose
   checks agree is accepted; an ambiguous set is compared by parent chain,
   texture set and shader types (read the candidate with
   `resolve_active_unpacked_asset` only when the document is not enough).
   **Content identity is the strongest cheap signal.** `facets.textureIdentity`
   / `signals.textureIdentity` come from the Host hashing a small mip of each
   bound texture (RenderDoc converts any GPU format) against fingerprints of
   the unpacked textures, tolerant to resize, mirroring and rotation (the
   `transform` field says which); `same-image` means those pixels are that
   asset. Every material referencing an identified texture is a candidate,
   and the identified texture's own relations lead to more names. A
   `flat`/uniform texture and a snapshot the Bridge could not take are
   reported with a reason, not silently dropped.
   **Value evidence outranks names.** `valueEvidence` / `facets.values`
   lists constant-buffer lane values found verbatim in the candidate's
   serialized parameters (`laneRefs` says which cbuffer lane), how many are
   rare in this catalog, and `adjacentRuns` where consecutive lanes matched
   consecutive asset values — three complex numbers in a row is effectively
   identity even when no texture name matched. From a value-identified
   asset, follow its relations (parent material, instances, textures) for
   more names; the same holds for a texture identified by content.
   These Host signals are supplements that save tokens; they do not replace
   your own search. When they are weak or absent, query the catalog yourself
   (`query_active_unpacked_assets`, `resolve_active_unpacked_asset`,
   `read_recovery_regular_file` on a candidate) by the names, parameters,
   parent chain and shader types you can see, and state which signal your
   decision rests on.
   **Partial matches are evidence, not failures.** Every candidate carries
   `confidence` and `facets` (`textures.explained/unexplained`,
   `shaderTypes.agrees/disagrees`, `names.available`, `values`): when the textures
   facet is high but a shader type disagrees, keep the texture/parameter
   names as reference with that facet's confidence and say so; when only the
   parent material matched, use the parent's names; never throw the whole
   match away because one facet is unexplained, and never promote a weak
   facet to certainty. Then call `submit_shader_asset_resolution(event_id,
   stage, asset_id, confidence, rationale, facets_json)` — several positives
   are allowed (instance + parent + shared function), `reject=true` for a
   wrong leader — so this game build never repeats the search for that shader
   or shape. The document's names are the reference for CBuffer member and
   texture naming in step 7, validated against how the code uses each value,
   and the facet confidence travels into your inferred-name confidence.
   **Names you confirm become rules.** When the shader has a submitted asset
   resolution, every CBuffer member rename you apply with
   `rename_shader_workspace_symbols` is recorded (`memberRules` in its
   result) for that material family and offset; the next shader of the same
   family — another instance, another capture — is decompiled with those
   names already in place and keeps physical lane names only where no rule
   exists. So resolve the asset before renaming, and rename members you are
   confident about even when other members stay unknown.
   **Identity/layout evidence:** `query_active_shader_asset_evidence(event_id,
   stage)` when the catalog indexed shader binaries. A candidate whose
   `identityRelation.physicalIdentityProgramVerified` is true is the same
   program: if a readable source sits with it, base the ABI repair (and the
   readable version) on that source — adapt only the entry point/profile the
   workspace records — and submit it through
   `repair_shader_workspace_source_abi`; the Host's compile + fresh reflection
   confirms it. A candidate with only `layoutHashEqual` lends its block/member
   declarations and names for the blocks whose `space:bind`, `byteSize` and
   offsets agree with (a); anything that disagrees marks the root as another
   game/version for that shader and is dropped, not forced;
   (c) the decompiled code shows what is actually used and is where every
   inferred name is validated. When no asset root is configured, (a) + (c)
   is the normal, complete path.
   If `abiValidationStatus="incompatible"`, read the persisted
   `lastAbiAttempt.comparison` and its report: this exact candidate was already
   rejected. Do not mistake its unauthorized pending `abiGate` for an untried
   candidate. `source-changed` means the failure belongs to the older compiled
   source; compile the edited source and obtain fresh ABI evidence.
5. `compile_shader_workspace(workspace_path)` only if the current source has
   no matching compiled candidate. Then call
   `validate_shader_workspace_candidate(workspace_path)` only if that candidate
   lacks a compatible ABI gate. Receipts must match the current source,
   candidate and revision. The begin/review Host reuses valid evidence; do not
   repeatedly compile an unchanged baseline or add redundant validation calls.
   **Existing project source:** when `source.projectSource` identifies the
   Host-validated project artifact, skip steps 6–9 (new AI organization and
   duplicate publication). Complete the current compile/ABI checks here,
   then call `apply_shader_workspace_shared_resource` and
   `start_shader_workspace_live_edit` as described in §4. The existing project
   library supplies its analysis/graph; the live editor uses the current
   workspace source. Do not begin a new organization session merely to reopen,
   read, reuse or edit an already published source. Intentional user edits
   still need compilation and current ABI safety, never a new equivalence
   review against the original.
   `abiCompatible=false` while the gate status is
   `pending-replay-reflection` means **not yet validated**, not a proven ABI
   mismatch. A compile failure is a tool diagnostic: read its text and fix
   only what it names. After validation, a non-empty ABI difference list with
   `compatible=false` is a proven safety failure, not another pending state:
   do not weaken the gate, do not claim the source is replaceable, and do not
   proceed to organization/publication with that candidate. **Repair it
   yourself, in this same turn, without asking the user what to do.** The
   diagnostic's `allowedActions` contains `repair-source-within-captured-abi`
   and `sourceRepairTool` names the channel:
   `repair_shader_workspace_source_abi(workspace_path, expected_source_sha256,
   source, acknowledge_semantic_equivalence_unverified=true)`. Read the whole
   current source (`read_shader_workspace_file`), the captured interface
   (`baseline.compiledInterface`: `inputSignature` rows with `regIndex`,
   `regChannelMask`, `compType`, `compCount`, `semanticIdxName`,
   `systemValue`; `constantBlocks` with `byteSize`, member offsets;
   `readOnlyResources`), the Host's own attempt in
   `source.hlslCBufferAbiRepair.reason`, and `lastAbiAttempt.comparison`.
   Typical fixes: re-declare `SPIRV_Cross_Input` in the captured row order
   with the captured semantics (`TEXCOORDn` rows map to `regIndex n`,
   `bool … : SV_IsFrontFace` reflects as one `uint`), add unused captured
   inputs (`SV_IsFrontFace`, padding rows) instead of dropping them, pad a
   cbuffer to the captured `byteSize` (e.g. 44 → 48 with a trailing
   `float pad;` or a `float4` in place of `float3`) while keeping every
   member offset, and keep register/space, array sizes and interpolation.
   Send the complete repaired file with the hash of the source you read. The
   Host compile-checks it, compiles and replay-validates it, keeps it only
   when the fresh reflection matches, and otherwise restores the previous
   bytes and returns the diagnostics plus `whatToFix` — one entry per
   mismatch with `where` (semantic / register / `cbuffer register(bN, spaceM)
   @offset` / `tN spaceM`), `why` (captured vs candidate) and `how` (the HLSL
   change). The same list arrives as `explanation` from
   `validate_shader_workspace_candidate` and as `abiExplanation` /
   `whatToFix` in an `organization-abi-failed` error. Work through every
   entry, then submit the next correction. Never change the computation; only the interface
   declarations, padding and the accesses they force. Retry up to three
   submissions; each must address the returned differences. When accepted,
   continue immediately with step 6 (`begin_shader_workspace_organization`).
   Only if three attempts still fail, finish with
   `STATUS: PARTIAL - ABI incompatible` and name the remaining differences.

For the qrenderdoc **Analyze selected Shader with AI** intent and ordinary
requests to decompile, reconstruct, or make a shader readable, continue to
§2: the useful product result is the organized/commented source, analysis,
generated flow graph, project registration and the editable workspace. Stop here only when the user
explicitly asks for the untouched/raw decompiler output without organization,
documentation, or publication; then report the workspace path, source path,
and compile/ABI receipts.

## 2. Organization and publication (the readable version + flow chart)

This is the only step where you author text, and it is what the Shader
Workspace panel in RenderDoc displays.

6. `begin_shader_workspace_organization(workspace_path, event_id)` — opens a
   session (needs the baseline compile + ABI from step 5) and returns
   `organizationId` and the proposal/document paths. If it answers
   `organization-abi-failed`, the session never opened: go back to the step 5
   repair channel (`repair_shader_workspace_source_abi`), then call begin
   again for a fresh `organizationId`; a stale id answers "never opened". Read
   `semanticContext` in the result (bound textures, cbuffers, pipeline facts)
   and the baseline source (`read_shader_workspace_file`).
   `semanticContext.cbufferAliases.blocks[].user` lists display names the
   user already gave CBuffer fields in RenderDoc's constant-buffer panel;
   block aliases are also present. Preserve user names and their priority.
   A display alias may contain spaces or Chinese text; it need not be a legal
   HLSL identifier. Keep that user label while choosing a valid source symbol.
7. **Organize the actual algorithm, not just its presentation.** Start with
   `get_shader_workspace_outline(workspace_path)`: declarations with
   packoffsets and registers, every function with line range/calls/callers/
   texture sample sites, where each cbuffer member and resource is used, and
   how much is still anonymous. Then take slices only —
   `get_shader_workspace_outline(function=<name>)` or `lines=<a>-<b>`. Never
   read a large `source/shader.hlsl` end-to-end in `read_shader_workspace_file`
   chunks, and never hand Host workspace files to vendor sub-agents, grep or
   shell tools: every read goes through these Host tools (they are cheaper
   and carry provenance). Name in this order: cbuffer members and resources
   (asset evidence first — §step 4), the entry function, then helpers by
   caller order. Read the
   Host `organizationContract` and `flowSpecTemplate.computationRanges`.
   **You never rewrite the file yourself.** The proposal is built from
   Host-applied operations that compose in a fixed order — renames
   (`rename_shader_workspace_symbols`), structural operations
   (`restructure_shader_workspace_source`), comments — and every operation is
   compiled before it is kept, so a 300 KB shader is organized in hundreds of
   small, cheap, verified steps instead of one impossible rewrite. The
   procedure for a long decompiler body is iterative:
   (a) **Segment.** From the outline and `computationRanges`, walk the entry
   body top to bottom and cut it at real computation boundaries — input
   unpacking, UV/coordinate derivation, each texture sampling group, normal or
   tangent reconstruction, each material/lighting term, fog/exposure/tone
   steps, output packing. For each range call
   `restructure_shader_workspace_source(operation="extract", start_line,
   end_line, name, comment)` with a provisional but descriptive name. The Host
   infers inputs/inout/out/return from data flow; read `lastReceipt`: many
   parameters or several `outParams` mean the range mixes two computations —
   `undo` and cut smaller. Refusals name the reason (a `return` inside, an
   escaping `break`, a cut through if/else or a multi-line statement): move
   the boundary, do not fight it. Line numbers shift after every accepted
   operation; take them from the outline of `proposalSourcePath`, never from
   memory.
   As you extract, record what you learned while the slice is still in
   context: `restructure_shader_workspace_source(operation="note",
   name=<function>, comment=<one sentence: what it computes, from what, for
   whom>)`. This is how a large refactor stays tractable without a large
   context — the same way coding agents refactor big repositories: an index
   instead of the whole tree, small verified edits, and an external ledger of
   what each unit does. The Host keeps that ledger for you: every result
   carries `ledger`, one row per function with your note, size, origin
   (which range of which caller it came from), inputs/inouts/outputs, calls
   and callers, cbuffer members/resources touched, remaining anonymous
   locals, heuristic `flags` and a status (`provisional` → `noted` →
   `reviewed`). It survives the turn inside the session.
   (b) **Consolidate from the ledger, not from the code.** When the whole
   entry body is segmented, call `operation="list"` and read only the ledger
   (a few hundred tokens). For every row ask: is it one responsibility a
   graphics programmer would name (`ComputeDiffuse`, `SampleNormalMap`,
   `ApplyFog`)? Two rows with the same purpose or one calling the other with
   most of its outputs → merge (`remove` the smaller, or extract a wider
   range); a row with many parameters, many globals or two verbs in its note
   → split (another `extract` inside it, re-reading only that function's
   slice); placeholder names → `rename_function`; does the entry function's
   `calls` list now read as the recipe? Apply the decisions, refresh notes,
   and repeat the ledger review two or three times; stop when no row is
   flagged and every note states what the function computes. Passes cost
   operations, not review rounds.
   (c) **Name inside.** Rename remaining anonymous temporaries per function
   (`rename_shader_workspace_symbols`, batches by function), then add a
   one-line header comment per function (`comments=`) and section comments
   inside any function that legitimately stays long.
   Do not impose a lighting pipeline on a shader that has no lighting. A
   simple or already structured shader needs no artificial split.
   Add section comments inside a necessarily long function. Keep compact
   expressions readable; merge single-use temporaries only when evaluation
   order, branch conditions, derivatives, resource access and precision remain
   justified. Replace arithmetic patterns with floor/smoothstep/dot/cross or
   matrix operations only when types, domain and operation equivalence support
   that identity; a familiar-looking expression is insufficient evidence.
   Preserve uncertainty with neutral descriptive names, not invented meaning.
   Review **every executable function**, including helpers and computation
   structs: no remaining `_2323`, `temp2323`, or `frontier_phi_*` locals/shared
   fields. Unknown business meaning is not a reason to keep an anonymous
   temporary: `radialDistance`, `packedWord`, `clampedMask`, or
   `viewRegister12X` can describe the observed operation without guessing its
   game-specific purpose. Do not rename protected physical ABI declarations
   merely to satisfy a style metric. Names that collide may use a meaningful
   component/call-site suffix; never just copy the old decompiler number.
   Numbered `Stage1/Stage2` chunks cut by line count are incomplete. Determine
   real calculation boundaries and repeated pure operations, then extract
   cohesive helpers. Inspect all long functions for meaningful internal
   sections and explain them briefly at those boundaries. Preserve the input
   operations where a high-level identity or reassociation is unproven.
   The Host's `organizationContract.quality` lists all functions, anonymous
   values, mechanical chunks and dense expressions. Address these as coherent
   batches within the same bounded review session; metrics are quality checks,
   not a numerical or universal semantic proof.
   Keep shared math helpers in the managed HLSL document for this transaction;
   do not create an untracked math.hlsl include or guess an engine include.

   **Consult the project's unpacked source assets for real names first.**
   You always analyse the shader code itself to infer names and structure.
   A configured unpacked-asset root does not replace that analysis — it is a
   strong *reference* you cross-check against, because the configured root can
   be wrong (another game, or another version of this one), and a coincidental
   layout match must not silently rename members with names that contradict
   what the code actually does.

   First check whether this project has a configured unpacked-asset root:
   `get_active_unpacked_asset_index_status()`. **If it is not configured or
   not indexed, name every CBuffer/member from your own analysis of the shader
   code plus reflection and `semanticContext`, as below — the asset root is
   optional and its absence is normal.** If it IS ready, use the matched source
   as reference on top of your code analysis:

   1. Call `query_active_shader_asset_evidence(event_id, stage)` for THIS event
      and read `relationView.assetCandidates[]`. When you need to search or
      confirm the underlying assets/materials more deeply (to judge whether the
      configured root really is this game/version), use the dedicated
      `renderdoc-unpacked-asset-intelligence` skill to read and resolve them;
      do not re-implement that flow here.
   2. For a candidate whose `identityRelation.physicalIdentityProgramVerified`
      is true, or whose `cbufferEvidence.layoutRelation.layoutHashEqual` is
      true, its `cbufferEvidence.asset.blocks` carry the source's real block
      and member names in byte-offset order, lined up with the decompiled
      CBuffer members in the same block (same space/bind). Treat these as a
      high-value proposal, not automatic truth: **validate each proposed name
      against what the code does at that member** (how it is read, combined, and
      output). Apply a source name only when the code's use is consistent with
      it; if the source name plainly contradicts the observed operation, the
      match is likely the wrong game/version for that field — keep your
      code-analysis name and note the mismatch instead. Apply the accepted
      names through the rename tool below so they publish as CBuffer display
      aliases (e.g. a decompiled `materialRegister16W` becomes the validated
      source field name at that offset).
   3. Read `materialEvidence.inferredHints` (material/parameter names from the
      matched material) to strengthen both your naming AND your organization of
      the shader — they hint at the shader's purpose and at members the source
      did not name. Treat all of this as inference to be checked, not fact, and
      label confidence.

   Never apply a source name to a member whose offset/type does not line up,
   never override a name the user already set in the panel, and when no
   candidate is layout-verified do not guess from a weak match — fall back to
   your code analysis and neutral names below. This step is read-only evidence;
   it scans nothing and applies nothing by itself.

   **Apply supported names and external aliases before writing the rewrite.**
   `rename_shader_workspace_symbols(workspace_path, organization_id,
   renames={"cb0": "MaterialCB", "v0": "tintAndAlpha", "t0": "tAlbedo", …},
   comments={"MainPS": "Entry point: …", "applyTint": "…"})`. Take the new
   names from reflection and the `semanticContext` (cbuffer field meanings,
   bound texture slots); use neutral expression/data-role names where evidence
   does not support specific business meaning. The Host applies the renames at the token
   level, so the result is *rename-only equivalent* by construction — this
   can skip numerical replay when compile and ABI checks pass. That fast
   correctness path does not make a long, otherwise unchanged decompiler body
   a completed algorithm organization. The Host returns an explicit incomplete
   organization reason for that case; do not call comments-only work complete.
   This tool regenerates from the session baseline, so calling it after manual
   proposal restructuring would replace that rewrite. Finish these aliases
   first, then author the complete structured proposal.
   You may call it repeatedly; maps accumulate. Inferred CBuffer block and
   member names must also become external display aliases through this
   existing tool; do not create another alias store or modify reflection.
   The tool publishes aliases in RenderDoc's constant-buffer panel
   for every shader and capture that binds a block with the same name and
   layout (`cbufferAliases` in the result); names the user already set there
   are kept (`keptUser`) — do not fight them, mention them.
   Check that returned aliases target the captured block name/layout, not only
   the generated HLSL name. A store write under another layout key is not proof
   that the original CBuffer panel can display it. Do not invent offsets or
   map ambiguous overlapping fields. Label inferred meanings with confidence.
   Then write documents with `write_shader_workspace_organization_file(
   workspace_path, organization_id, kind, content)`:
   - `kind="analysis"`: fill the exact JSON `analysisTemplate` from begin.
     Fill `overview`, `inputsResources`, `computation`, `outputs`,
     `observedFacts` (`claim` + `evidence`) and `inferences` (`claim` +
     `confidence`: high/medium/low). The Host renders the fixed six Markdown
     sections. Do not submit Markdown, HTML, headings, or placeholder bodies.
   - `kind="module_descriptions"`: fill `moduleDescriptionsTemplate` with its
     exact `schema`, `schemaVersion`, and `modules` array of
     `{ "name": "MainPS", "description": "one line" }` records. Names must
     exist in the proposal; the Host supplies actual graph structure.
   - `kind="flow_spec"`: fill the `flowSpecTemplate` from the begin result —
     `inputs` (from `allowedInputs`), source-declared `resources`, `outputs`,
     and ordered `stages` (`id`, `label` ≤ 72 chars, `function`,
     `sourceRange: {startLine,endLine}`, `reads`, `writes`, `next`,
     `inferred: true`). A node is a real calculation section, not a parameter,
     resource declaration or one node per temporary. Ranges must enclose
     complete executable statements in the current proposal's function body.
     Use local intermediate values for data edges; `Function.return` denotes
     that helper's actual return value and may be read only where it is called.
     Order producers before consumers from top to bottom. Several stages may
     describe cohesive ranges of one long function. CBuffer field reads must occur in
     `allowedFields`; listing an invented name cannot authorize it. The Host
     validates every name against the shader and
     renders the diagram in the fixed ReplayLab style; rejected specs come
     back with the offending stage and the fix. Do not submit hand-written
     Mermaid or style overrides; old diagrams remain available for reading.
   - `kind="proposal"`: code restructuring beyond names and
     comments. Preserve computations and interfaces; never invent samples,
     branches, resources or plausible algorithms. Work in a few meaningful
     batches containing a complete function or computation stage. Combine
     small changes; never review every few lines. Write the complete proposal
     and matching documents, then review once per batch. After acceptance a
     further planned batch may begin from that accepted source; never use a
     new session to evade an exhausted review budget. Omit a rewrite only when
     the existing source already expresses a readable algorithm or the user
     explicitly requested untouched output. An unresolved rewrite remains
     partial; a preserved safe baseline is useful but is not the requested
     completed algorithm organization.
8. `review_shader_workspace_organization(workspace_path, organization_id)` —
   reuses the validated baseline and compiles the proposal when necessary.
   `exact-code` and `rename-only` are deterministic fast checks. A proposal
   with structural operations usually compiles to the same executable code
   (`exact-code`) because the compiler inlines the helpers; when it does not,
   the Host measures it with the replay comparison below. A different
   code hash is not itself a failure: substantial code changes may receive
   `observational-pass` from one Host-run temporary replay comparison using
   the same captured event inputs. The Host uses fixed absolute/relative
   tolerances for floating-point outputs, stricter depth tolerances and exact
   integer comparison, then restores replay state. Do not spend model tokens
   generating test vectors or recheck unchanged code. This covers measured
   outputs only, not all inputs or universal semantic equivalence.
   It promotes the source on acceptance and
   publishes `ai/flow-graph.json` + `ai/flowchart.generated.mmd`. Read
   `verdict`: `reasons[]` name the code, location, blocker/warning severity,
   direction, target and small example. `roundsLeft` is the remaining budget
   (default 3 rounds, configurable
   by the user). Rejection keeps the same session open: correct its proposal
   and review the same `organizationId`. Unsupported output sampling is not
   a pass; use a proven rename-only proposal where possible. When the budget
   is spent, only warning-level proposal rejection may publish the compiled,
   ABI-validated baseline with a safe Host summary and source-derived diagram
   (`softAccepted`,
   `decision="soft-accepted-baseline"`); the failed rewrite stays provisional.
   Schema, source identity, ABI and other blockers never become accepted or
   soft accepted because the budget ran out. Build long documents in pieces:
   `write_shader_workspace_organization_file(kind, content, mode="draft")`
   merges a partial JSON object (only the fields you are adding; `modules`
   and `stages` merge by name/id) over the previous draft, validates the whole
   and returns **every** issue with its location — nothing is counted. Repeat
   with just the flagged fields until `complete=true`, then publish with
   `mode="final"` and empty `content`. Never rewrite a document from scratch
   because one field was rejected. Template/format rejections of
   `write_shader_workspace_organization_file` (`level="template-rejected"`)
   are counted separately (`verdict.templateErrors`) and do not consume
   content rounds: fix exactly the listed fields (the message names missing
   and unexpected keys) and resubmit. When any verdict carries
   `budgetExhausted=true`, stop: do not write, rename or review again, and do
   not open another session. If it also carries `userDecision.required=true`
   this is the one gate you must put to the user — ask exactly its `question`
   with its `options` through your ask mechanism (this is the explicit
   exception to the no-questions rule); when the user answers, call
   `decide_shader_organization_review_budget(workspace_path, decision,
   user_confirmation=<their words>)` and continue: `extend` reopens up to 3
   rounds, `publish-baseline` makes the next `review` complete the session as
   a user-authorized baseline publication (registration and the editor then
   proceed), `stop` leaves everything as is. Register a returned safe
   baseline only when the Host explicitly soft accepts it.
9. With `project` and `ui`: `register_active_project_workspace(workspace_path)`
   publishes the reviewed HLSL, analysis and flow graph into the replay
   project and shows its Shader Workspace. Registration alone can leave the
   panel read-only; it is not the end of editable delivery. For the normal
   editable/save/compile/replace request, follow §4: call
   `apply_shader_workspace_shared_resource` for the exact current candidate,
   then `start_shader_workspace_live_edit` with the Host-returned identity and
   authorization values. Persistent authorization slots are reclaimed by the
   Host automatically (dead workspaces, older attempts of the same shader,
   least-recent other captures — reported as `reclaimedAuthorizations`); do
   not list/stop old sessions yourself to free a slot. Read back live-session
   and panel state. The main
   editor must contain the actual canonical compile source, not an alias view.
   Other RDCs in the same project may reuse the
   stored HLSL through Host matching; each target still needs its own current
   interface/candidate binding before replacement. Verify registration and
   panel-open status; HLSL existing on disk alone is not completed delivery.
   Automatic project restoration searches only original raw identities of
   current project knowledge heads, in bounded batches. A target lookup is
   not a full-frame catalog; unavailable raw bytes leave absence unknown.
   Selected-Shader requests need only their one event/stage catalog and never
   require a full-frame preparation pass.

## 3. When a tool fails

Follow the standing rules (`replaylab-core`, Tool errors and Autonomy). Never
end the turn with a question such as "how should I proceed?" while an
`allowedActions` entry, a listed recovery candidate, a repair channel or a
review round is still available: pick it and continue. Shader-specific: a runtime
route failure in `prepare_shader_workspace` lists the untried registered
decompiler/compiler pairs — pick one with
`try_shader_workspace_recovery_candidate` instead of repeating the call; a
completed compile with a final `compatible=false` ABI verdict blocks
organization, project registration and replay replacement (it is not success);
   a stale route-catalog/provenance verdict requires the Host's guarded
`migrate_shader_workspace_toolchain(..., in_place=true)`, never a manual
manifest edit; a
`review` rejection names the proposal line or failed output metrics — fix that
complete computation batch or use the proven rename path. If comparison fails
to restore replay, stop and report it; do not mutate uncertain replay state.

## 4. Replacement (replay effect only)

After delivery, intentional user edits need not match the original or the AI
result and do not repeat organization review. Save with the current-source
conflict check, compile the changed code, and validate its resource/interface
compatibility. Hashes identify versions and stale evidence; they do not forbid
adding code. Old diagrams and comparison results can become stale.

Replacement requires the exact current candidate revision/hash from
`get_shader_workspace_status`, fresh ABI evidence, and the requested scope.
`apply_shader_workspace_candidate` (one event) or
`apply_shader_workspace_candidate_family` (every use of the shader); verify
the returned identity and post-state; on any inconsistency roll back with the
matching `rollback_shader_workspace_override*` tool before doing anything
else. For default editable reconstruction, use the checked shared-resource
apply result and then `start_shader_workspace_live_edit`; verify its durable
session, canonical source path and editable panel state with the returned
status/poll tools. Do not report a short-lived Agent-owned watch as a working
persistent editor. Explain-only and explicit export-only requests skip this
step. Keep the last successful version if a new compile or replacement fails.
CBuffer value edits are separate from external display aliases and need their
own requested scope.

## 5. Evidence and provenance rules

`replaylab-core`, Evidence rules, applies. Shader-specific: preserve descriptor/register/
space or set/binding, access kinds, arrays, interpolation, precision,
signedness, vector width, cbuffer offsets/strides, stage interfaces, discard,
derivatives, barriers, atomics and output writes; mark ambiguity instead of
inventing plausible facts. Ray/library readability or compilation never
proves that replay can replace a state object.

## 6. Report

`report_task_result` (and the API child's Markdown to its main Agent) carries
the full record: findings with the producing tool, bound identity
(capture/session, EID, stage, resource, entry point, raw hash), what was
produced (workspace path, source path, compile/ABI receipts, organization
review decision, project registration), inferences with confidence, unknowns,
and `STATUS: DONE` / `STATUS: PARTIAL - <reason>` / `STATUS: FAILED - <reason>`.

The answer the user reads is different: a short conversational summary in
the user's language (`replaylab-core`, The user-facing answer). Say what the
shader does, what was done (decompiled, ABI repaired and how, organized,
registered, editor opened), why anything failed or was left out, and the one
next thing the user may want. No headings, bullet lists, tables, emoji, tool
names or hashes unless the user asks; do not repeat the same fact twice.
