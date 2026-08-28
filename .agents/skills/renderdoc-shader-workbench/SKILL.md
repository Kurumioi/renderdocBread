---
name: renderdoc-shader-workbench
description: Reconstruct, explain, edit, validate, replace, and reuse a capture-bound RenderDoc shader. Use for readable HLSL, shader algorithms, ABI/resources, compile failures, safe replay replacement and rollback, project-library reuse, and the Reverse/Edit with AI button.
metadata:
  replaylab-category: shader
  replaylab-read-only: "false"
  replaylab-min-successful-tools: "1"
  replaylab-version: "1"
allowed-tools: inspect_shader get_draw_call_details get_pipeline_state get_shader_info reverse_shader get_bound_textures get_shader_toolchain_capabilities plan_shader_toolchain_route plan_shader_recovery get_shader_workspace_status list_shader_workspace_family_uses get_replay_project_status get_replay_project_reuse_plan prepare_active_capture_project get_active_shader_workspace_path prepare_shader_workspace decompile_shader_workspace get_shader_workspace_representation promote_shader_workspace_explanation begin_shader_workspace_organization review_shader_workspace_organization compile_shader_workspace validate_shader_workspace_candidate apply_shader_workspace_candidate apply_shader_workspace_candidate_family apply_shader_workspace_shared_resource rollback_shader_workspace_override rollback_shader_workspace_override_family rollback_shader_workspace_shared_resource register_active_project_workspace
---

# Shader workbench

Own the lifecycle of one capture-bound shader, from evidence and readable
reconstruction through optional compile, replay replacement, rollback, and
project reuse. Run only the stages needed by the user's goal. An analysis
request does not imply replacement; a replacement request does imply every
preflight and rollback check below.

## Evidence boundary

- Bind the run to the frozen capture session, EID, normalized stage, shader
  resource ID, entry point, and raw SHA-256. Revalidate this identity before
  compile validation, replacement, rollback, or project registration. Stop on
  an identity conflict instead of combining different shaders.
- Treat reflection, disassembly, declared bindings, and tool-returned values as **captured evidence**. Treat semantic names, algorithms, material intent, and source constructs not directly present in that evidence as **inference**.
- Treat any HLSL, pseudocode, renamed variable, reconstructed structure, or
  reconstructed expression you create as **generated reconstruction**. Never
  call it original source. A successful compiler proves only the reported
  compiler/container/ABI checks, not semantic equivalence to unavailable source.
- `inspect_shader` is an evidence bundle, not a guarantee of high-level
  decompilation. Embedded source is original only when the tool explicitly
  proves that provenance; otherwise reconstruct from reflection and
  disassembly.

## Analysis flow

1. **Identity and representation.** Establish API/encoding, stage, entry point,
   raw identity, available source/debug information, reflection completeness,
   and disassembly form. Use `inspect_shader` for capture evidence and the
   toolchain planning/status tools when the requested stage needs an editable
   route.
2. **Reflection and ABI.** Record stage inputs/outputs, semantics or locations, interpolation, resources and access kinds, descriptor/register/space or set/binding, samplers, UAV/storage bindings, constant-buffer members and offsets, array strides, matrix dimensions/major order, specialization/push constants, and thread-group or tessellation metadata when present. Do not invent missing CBuffer leaf values or layouts.
3. **Control and data flow.** Trace observable blocks, branches, loops, calls, phi/select patterns, discard/kill, barriers, derivatives, atomics, texture sample/load/store operations, address calculations, conversions, and output writes. Prefer a compact data-flow account focused on values that reach resources or stage outputs.
4. **Resource and interface mapping.** Map disassembly operands back to reflected resources and stage interfaces. Preserve aliasing, arrays, dynamic indexing, bindless/direct access, precision, signedness, vector width, interpolation, and cross-stage contracts. Flag ambiguous mappings rather than assigning plausible names as facts.
5. **Readable HLSL and algorithm account.** Reconstruct only to the level
   supported by evidence. Preserve ABI declarations and observable behavior
   before improving names, helpers, or layout. Explain the algorithm as traced
   data/control flow, marking every semantic interpretation as an inference
   with confidence.
6. **Source organization.** When requested, create or reuse the exact managed
   workspace, organize generated HLSL through the organization review tools,
   and retain raw-to-readable traceability. Do not edit unmanaged paths.
7. **Compile and consistency validation.** Compile through the configured
   toolchain, then validate declarations, resources, CBuffer layout, stage
   interfaces, entry point, container, and ABI against the capture evidence.
   A failed or unavailable check stays failed/unknown; do not translate it into
   success prose.
8. **Replacement transaction.** Before applying, record the original shader
   identity, candidate hash, exact target scope, applicable rollback tool, and
   validation receipt. Apply only the requested scope. Verify the returned
   replacement identity and post-state; if verification fails after a mutation,
   invoke the matching rollback route and report both outcomes.
9. **Project reuse.** Register generated HLSL in the active project only after
   its identity/provenance and current validation status are recorded. Store it
   as generated project knowledge, never as recovered original source.

## Result contract

Always make the following distinctions visible, using headings or compact labels appropriate to the answer:

- **Bound identity:** event, stage, resource, entry point, and raw hash.
- **Observed evidence:** reflection/ABI plus relevant disassembly, control/data flow, resources, and interfaces.
- **Inferences:** likely semantics or intent, each with an evidence-based confidence level.
- **Generated reconstruction:** optional HLSL/pseudocode in a clearly marked
  block, with ABI-sensitive declarations preserved and provenance stated.
- **Validation:** exact compiler, container, ABI, interface, and consistency
  checks performed, including failures and checks not run.
- **Effects and recovery:** workspace/project artifacts, apply scope,
  candidate/replacement identity, postcondition, and rollback evidence, when
  effectful stages were requested.
- **Consistency and unknowns:** conflicts, missing evidence, and what would be
  needed to raise confidence.

Do not pad a narrow explanation with a full source listing, but never collapse evidence, inference, and generated source into one unlabeled narrative.

## Completion boundary

Do not use tool availability alone as proof of completion. For analysis, require
capture-bound evidence. For generated HLSL, require provenance labels and ABI
consistency review. For compile, require the compiler receipt. For replacement,
require preflight, validation, applied identity, postcondition, and a usable
rollback record. For project publication, require the registered artifact and
its validation/provenance metadata. Return a partial result or precise blocker
when only some stages can be proven.
