---
name: renderdoc-resource-export
description: Export an explicitly bound RenderDoc draw or resource set as a portable, validated mesh, texture, material, CBuffer, and shader package for Unity or Blender. Use when capture data must be reconstructed outside RenderDoc with preserved vertex inputs, sampling/color-space metadata, coordinate conversion, and target-specific rendering compromises.
metadata:
  replaylab-category: resource-export
  replaylab-version: "3"
  replaylab-required-references: "references/source-evidence.md references/target-profiles.md references/package-and-validation.md references/workflow-acceptance.md"
---

# RenderDoc resource export

The mandatory `replaylab-core` owns templates, evidence, review severity,
correction budgets and reporting. Main/child wording below describes API mode;
the vendor-native Agent performs the same bounded work directly and uses its
own supplied question interface. Requirement lists are reasoning aids, not an
extra runtime ledger or a replacement for `report_task_result`.

Own the bounded transfer of capture-bound draw resources into a portable target
package. Preserve observed source data and its interpretation, adapt only the
parts required by the selected target, and prove the resulting package can be
consumed. An export request is not permission to widen the EID scope, write to
an arbitrary path, install a converter, or silently approximate missing data.

This Skill is an adaptive playbook and acceptance contract, not an executable
workflow. The modes, stages, routes, and tool names below are useful options.
The ordinary child Agent may inspect, skip, reorder, combine, revisit, or
replace them when live evidence and registered capabilities support a better
path. Only real data and transaction dependencies are ordered. The Host freezes
identity, authority, effect ceilings, budgets, and receipts. Choose only among
the live Host's supported routes and parameters; fixed tool constraints remain
binding. A failed
tool or rejected receipt is an observation for
`plan -> act -> observe -> validate -> replan`, not task completion.

Before acting, read all four mandatory references completely:

- `references/source-evidence.md` defines the source identity, mesh, texture,
  sampler, CBuffer, shader, and dependency evidence needed before conversion.
- `references/target-profiles.md` defines target contracts, coordinate and
  color-space handling, Unity URP and Blender adaptation, and decisions that
  require user authority.
- `references/package-and-validation.md` defines managed artifacts, manifests,
  receipts, atomic publication, and target validation.
- `references/workflow-acceptance.md` defines the corrective loop, requirement
  states, yield rules, and completion gate.

These references are part of this skill's contract. Do not report completion
from a plausible-looking FBX, material, or shader alone.

## Operating modes

Choose the narrowest mode that satisfies the request. Do not silently turn an
inspection or planning request into a workspace mutation.

- **Inspect and plan:** bind the capture and EID, inventory source evidence,
  report target feasibility and unresolved choices, and create no package.
- **Geometry transfer:** export the requested draw's topology, indices, vertex
  and instance inputs, transforms, and a manifest describing every preserved or
  unsupported attribute.
- **Material transfer:** export requested textures, sampling/color-space
  metadata, CBuffer-derived parameters, and target material assignments.
- **Shader transfer:** consume capture-bound shader evidence and an admitted
  shader route or Shader child receipt, preserve the core observed algorithm, and
  adapt it to the selected target profile.
- **Complete reconstruction:** combine the applicable modes into one validated,
  immutable package. Include only explicitly bound EIDs and approved dependent
  passes.
- **Batch reconstruction:** accept only a bounded explicit EID/resource list;
  ask the main Agent to partition one immutable child operation per exact draw,
  deduplicate admitted Event/Shader/unpacked-asset evidence by result digest,
  and return per-item plus aggregate status. Do not widen a query into a frame.

## Authority and scope

Treat the Host request as the effect ceiling. Bind every operation to the exact
capture/session, EID set, source resource identities, target profile, dependency
policy, coordinate profile, and requested output classes.

The ordinary child Agent chooses among admitted, functionally equivalent
converters and validators, and may use a newly discovered registered capability
inside the same effect ceiling. Return a decision request to the main Agent
when a choice changes visible semantics, output scope, external side effects, target
render architecture, or which information is discarded; the main Agent decides
whether the user must be asked. In particular, do not choose among deferred
to forward merging, multiple shader/pass export, MRT reconstruction, or removal
of cross-draw dependencies without explicit authority.

Use only registered providers visible in the live capability catalog; a
capability query such as `get_resource_export_capabilities` is one way to
inspect that catalog. Never download, install, or run an unregistered converter
at execution time. Missing capabilities trigger another safe registered route,
a precise bounded unknown, or a material follow-up; they do not justify an
improvised command or silent lossy fallback.

## Optional stage and route map

Use this map to reason about remaining outcomes. Skip stages already proven by
accepted receipts, revisit assumptions contradicted by later validation, and
substitute any registered capability that proves the same outcome. The examples
do not require one particular tool sequence.

1. **Target binding:** obtain the active capture/session and requested EID set,
   for example through `get_current_renderdoc_context` when frozen context was
   not already supplied. Bind rather than assume the current UI selection.
2. **Source evidence:** an exact-event composite such as `inspect_event` can
   establish draw, pipeline, resource, and attachment identity. A bounded frame
   composite such as `analyze_current_frame_flow` is useful only when earlier
   or later draws, GBuffer attachments, or pass dependencies can affect the
   requested result.
3. **Shader evidence:** obtain the bound interface, live resources, CBuffers, and
   raw shader identity. When readable reconstruction or target shader
   conversion is required, reuse an accepted Shader result or inspect the live
   Shader toolchain capabilities. The Agent selects the compiler/decompiler and
   representation from actual evidence; a route planner's output is advisory.
4. **Export feasibility:** inspect capabilities and source-export evidence, for
   example with `get_resource_export_capabilities` and
   `inspect_resource_export`. Compare alternatives by outcome coverage,
   fidelity, risk, and validators rather than accepting a Host-ranked route.
5. **Material decisions:** resolve every material target, dependency
   compromise, coordinate profile, and bounded unknown before mutation. Yield
   when the remaining choice needs user authority.
6. **Reversible preparation:** a preparation operation such as
   `prepare_resource_export` may create the exact Host-managed staging artifact
   when needed. It must return a receipt bound to the request hash.
7. **Atomic publication:** finalize only after source and transformation checks
   pass. It must assemble and atomically publish an immutable package without
   overwriting an existing artifact.
8. **Target validation:** when requested, run an available target validator for
   the finalized immutable package and verify structural/semantic evidence
   against the same operation/package/provider identity. Status queries may
   resume or diagnose that same request without duplicating it.
9. **Corrective loop:** replan only failed acceptance criteria. Preserve passed
   criteria, admitted evidence, failed candidate artifacts, and completed effects.

For a batch, never loop hidden free-form filesystem or replay operations inside
one child. The main Agent owns membership and scheduling; each ordinary child
Agent owns one exact export task at a time. Reuse completed idempotent operation receipts, retain
successful packages after a sibling fails, and retry only the failed/unstarted
children whose previous effect is known terminal. Return the continuation set
and immutable result digests so a later stage can resume without repeating the
whole batch.

## Shader and material boundary

An admitted Shader child receipt carries the algorithm, ABI, resource-usage, and
readable-reconstruction evidence. The ordinary child Agent running this Skill
owns transfer policy and target packaging for its exact task.
Never manufacture shader semantics from resource names alone.

Map observed CBuffer values into target material parameters only when their
type, offset, live value, and shader use are supported by evidence. Map camera,
object, time, viewport, and other system values to target-native inputs instead
of baking the captured frame value. Preserve unresolved values as explicit
unknown parameters or sidecar records.

Never infer object-space axes, handedness, or unit scale solely from the
captured graphics API. D3D, Vulkan, and OpenGL clip-space conventions are not
proof of the application's object-space convention; require matrix/shader
evidence or an explicit user-selected source profile.
The fixed coordinate receipt must use an inverse-transpose normal transform,
determinant-aware winding, and a matching tangent-handedness adjustment.

Preserve the core draw-local algorithm. Prefer target-native shadows,
environment lighting, fog, and equivalent engine services when they replace
capture-global dependencies without changing the requested core effect. Record
every substitution and dropped dependency.

## Prohibited legacy shortcuts

Do not call or recommend `export_drawcall`, `export_to_unity`, or
`save_texture`. They are legacy convenience paths and do not satisfy this
child task's exact target, managed artifact, source-fidelity, receipt, or target
validation contract. Do not use them as a fallback after a strict export tool
fails.

## Completion

Report only artifacts admitted by the Host. The final result must identify the
source capture and EIDs, target profile, package manifest and hashes, preserved
and unsupported data, shader/material compromises, coordinate transform,
validation results, and any bounded unknowns. A generated file without this
evidence is incomplete.
