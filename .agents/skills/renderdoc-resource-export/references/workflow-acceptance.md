# Workflow and acceptance

Drive the export from explicit requirements and evidence. A tool call, a file,
or a visually plausible target scene is progress, not completion.

## Requirement states

Track each applicable requirement as one of:

- `satisfied`: admitted evidence proves the criterion;
- `bounded-unknown`: the uncertainty is identified, localized, and preserved in
  the deliverable without invalidating the authorized result;
- `requires-followup`: an allowed next action can resolve it;
- `blocked`: required authority, evidence, provider, or safe postcondition is
  unavailable;
- `not-applicable`: the requirement is outside the admitted request, with a
  recorded reason.

Explicit user requirements and evidence-triggered requirements are sticky.
They remain in the ledger until satisfied, explicitly removed by the user, or
reported as bounded unknown/blocked in the final result. A later narrow tool
result cannot silently erase them.

## Phases and exit criteria

### 1. Bind and plan

Bind the capture/session, EID set, target profile, output classes, dependency
policy, coordinate profile, effect ceiling, and request hash.

Exit when the exact target is unambiguous and the chosen route uses only
Host-admitted capabilities. Yield before mutation if target family, render
architecture, dependency policy, or output scope requires user authority.

### 2. Collect source evidence

Collect event, frame-flow when needed, shader, mesh, texture/sampler, CBuffer,
and resource evidence according to `source-evidence.md`.

Exit when every requested output has an observed or derived source, or a named
bounded unknown. Conflicting evidence must be resolved or yielded; it cannot be
converted into an assumption.

### 3. Select transfer policy

Choose destination channels, target material/shader representation, system
value mappings, coordinate conversion, and treatment of cross-draw
dependencies according to `target-profiles.md`.

Exit when all deterministic transformations are specified and every semantic
compromise has the required authority.

### 4. Prepare

Create the Host-managed staging artifact for the exact admitted request.

Exit when the preparation receipt covers all requested source resources,
records converter identities and hashes, and reports no untracked effect or
unresolved transformation failure.

### 5. Finalize

Assemble the manifest and package, run built-in structural checks, and publish
atomically without overwrite.

Exit when every artifact has a relative path, size, hash, source mapping, and
immutable package identity.

### 6. Validate

Run applicable structural, semantic, target, and replay-postcondition checks
from `package-and-validation.md`.

Exit when each required acceptance criterion has admitted evidence and the
RenderDoc event is restored. A validator that was requested but unavailable is
not a pass.

### 7. Present

Report the package, target profile, source scope, hashes, mappings,
substitutions, omitted dependencies, validation results, and bounded unknowns.
Do not obscure compromises behind a generic success summary.

## Acceptance criteria

Use stable criterion IDs in coverage and retry only those that fail:

- `export-source-identity`: capture/session/EID/resource/shader identity is
  exact and unchanged.
- `export-vertex-interface`: all observed vertex and instance inputs have an
  exact source layout and destination or sidecar mapping.
- `export-topology`: topology, indices, base vertex, draw range, and instance
  semantics are preserved or explicitly bounded.
- `export-texture-sampling`: texture subresource, source/view format, channel
  mapping, color space, sampler, and mip behavior are preserved.
- `export-cbuffer-material`: typed CBuffer layout and live values are mapped as
  material, system, or unknown without baking system state.
- `export-shader-provenance`: target shader artifacts cite the exact admitted
  shader evidence, route, source hash, and recorded adaptation.
- `export-coordinate-transform`: axes, handedness, scale, winding, normals,
  tangents, UVs, instances, and skeletal transforms are internally consistent.
- `export-dependencies`: every cross-draw/pass dependency is included,
  substituted, dropped, or bounded according to the approved policy.
- `export-package-integrity`: manifest, relative paths, hashes, sizes,
  references, atomic publication, and non-overwrite checks pass.
- `export-target-validation`: every requested target importer/compiler/headless
  validator passes for the selected profile.
- `export-replay-restored`: the original RenderDoc capture and event state is
  restored and confirmed.

Additional route-specific criteria may be added, but these IDs must not be
renamed or treated as satisfied by prose alone.

## Corrective loop

Follow `plan -> collect evidence -> synthesize -> validate -> replan` and the
Host retry budget. By default, make no more than two corrective passes after
the initial attempt.

- Preserve passed criteria and admitted evidence.
- Re-run only the smallest failed transformation or validator group.
- Never repeat an already completed publication effect.
- Use only a pre-admitted finite fallback route.
- Stop early when the same failure cannot be changed by an allowed action.
- Keep failed attempts linked by request hash and immutable attempt ID.

Tool success does not waive a failed criterion. Conversely, one failed optional
artifact must not erase independent criteria that already passed.

## Yield, block, and cancellation

Yield only from a safe boundary with admitted evidence. State the exact
question, mutually exclusive options, recommendation and rationale, authority
needed, affected criteria, remaining effects, and what has already passed.

Use user authority for choices that change target, scope, visible material
semantics, dependency architecture, destructive overwrite behavior, or an
external side effect. The main Agent owns an in-scope technical route decision
among admitted equivalent providers and decides whether a user question is
required.

Do not yield while publication, replay restoration, or another mutation is
uncertain. Cancel and confirm cleanup first. Block when required evidence,
authority, safe containment, event restoration, or target capability remains
unavailable after bounded recovery.

## Completion gate

Completion requires all applicable criteria to be `satisfied`, except bounded
unknowns explicitly allowed by the request. It also requires:

- at least one successful admitted tool result;
- findings tied to admitted evidence;
- a complete package/effect receipt for every mutation;
- no outstanding user-authority decision;
- no uncertain filesystem or replay effect;
- an explicit list of preserved, substituted, unsupported, and omitted data.

Inspection-only mode may complete without a package only when the request did
not ask for exported artifacts. Otherwise, a plan, manifest draft, generated
source, unvalidated package, or successful converter exit code is incomplete.
