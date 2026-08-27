---
name: renderdoc-shader-workbench
description: Inspect and reconstruct the shader selected in RenderDoc from capture-bound reflection and disassembly. Use for shader ABI/resource analysis, control/data-flow recovery, source-reconstruction guidance, compile-error explanation, and the Reverse/Edit with AI button; generated source is not original source and live edits remain native-approval gated.
metadata:
  replaylab-category: shader
  replaylab-read-only: "true"
  replaylab-min-successful-tools: "2"
  replaylab-required-all-tools: get_shader_info reverse_shader
  replaylab-version: "1"
allowed-tools: get_shader_info reverse_shader get_shader_toolchain_capabilities
---

# Shader workbench

Recover the most useful high-level explanation or reconstruction that the capture evidence permits. The workflow is staged because shader identity and ABI mistakes invalidate everything downstream, but adapt the depth of each stage to the user's question.

## Evidence boundary

- Bind the run to the frozen capture session, event, normalized stage, shader resource ID, entry point, and raw SHA-256. Confirm that `get_shader_info` and `reverse_shader` describe that same identity. Stop and report an identity conflict instead of combining evidence from different shaders.
- Treat reflection, disassembly, declared bindings, and tool-returned values as **captured evidence**. Treat semantic names, algorithms, material intent, and source constructs not directly present in that evidence as **inference**.
- Treat any HLSL, GLSL, MSL, pseudocode, renamed variable, reconstructed structure, or reconstructed expression that you write as **generated reconstruction**. Never call it original, exact, round-tripped, compiled, or validated unless a future authorized tool actually proves that claim.
- `reverse_shader` is an inspection bundle, not a guarantee of high-level decompilation. Embedded source or a high-level target may be evidence when explicitly returned; otherwise reconstruct from reflection and disassembly.

## Analysis flow

1. **Identity and representation.** Establish API/encoding, stage, entry point, raw identity, available source/debug information, reflection completeness, and disassembly form. Use `get_shader_toolchain_capabilities` only when the user asks about an editable/round-trip route or when the available representation materially affects the reconstruction plan.
2. **Reflection and ABI.** Record stage inputs/outputs, semantics or locations, interpolation, resources and access kinds, descriptor/register/space or set/binding, samplers, UAV/storage bindings, constant-buffer members and offsets, array strides, matrix dimensions/major order, specialization/push constants, and thread-group or tessellation metadata when present. Do not invent missing CBuffer leaf values or layouts.
3. **Control and data flow.** Trace observable blocks, branches, loops, calls, phi/select patterns, discard/kill, barriers, derivatives, atomics, texture sample/load/store operations, address calculations, conversions, and output writes. Prefer a compact data-flow account focused on values that reach resources or stage outputs.
4. **Resource and interface mapping.** Map disassembly operands back to reflected resources and stage interfaces. Preserve aliasing, arrays, dynamic indexing, bindless/direct access, precision, signedness, vector width, interpolation, and cross-stage contracts. Flag ambiguous mappings rather than assigning plausible names as facts.
5. **High-level reconstruction.** Reconstruct only to the level supported by evidence: explanation, structured pseudocode, or source-like code. Preserve ABI declarations and observable behavior before improving names or style. Annotate inferred types, structures, helpers, branch conditions, and semantic intent.
6. **Consistency review.** Check reconstructed declarations against reflection; resource operations against binding/access types; inputs and outputs against stage interfaces; CBuffer access against offsets/strides/matrix order; and reconstructed control flow against disassembly. Report contradictions, unsupported instructions, lost precision, undefined behavior, and optimization artifacts.

## Result contract

Always make the following distinctions visible, using headings or compact labels appropriate to the answer:

- **Bound identity:** event, stage, resource, entry point, and raw hash.
- **Observed evidence:** reflection/ABI plus relevant disassembly, control/data flow, resources, and interfaces.
- **Inferences:** likely semantics or intent, each with an evidence-based confidence level.
- **Generated reconstruction:** optional pseudocode/source in a clearly marked block, with ABI-sensitive declarations preserved. State that it is uncompiled and unvalidated in v1.
- **Consistency and unknowns:** checks performed, conflicts, missing evidence, and what would be needed to raise confidence.

Do not pad a narrow explanation with a full source listing, but never collapse evidence, inference, and generated source into one unlabeled narrative.

## Mutation boundary

This v1 execution path is read-only. Do not create a workspace, invoke a compiler/decompiler, write/export files, apply a shader replacement, validate live replay, or roll back. Chat text and provider approval are not authority. Those actions require a short-lived native qrenderdoc capability ticket bound to a nonce, exact shader identity, operation, arguments, and destination before the corresponding tools may be exposed.
