# Workflow and acceptance

## Requirement ledger

Track each requested outcome as `required`, `accepted`, `open`, `yielded`,
`failed`, or evidence-backed `not-applicable`. Required outcomes are sticky: a
later fallback cannot erase an explicit report, exact asset, relationship,
batch, Blender, or validation requirement.

Typical criteria include:

- active-project/source scope and effect authority frozen;
- provider and capability route admitted;
- compatible complete index or concrete Strong Inventory selected;
- exact asset set and coherent variants resolved;
- dependency and relationship closure complete or bounded unknowns recorded;
- requested report or Blender jobs terminal exactly once;
- portable manifests, hashes, and per-mode validation accepted;
- capture-correlation evidence returned in a child receipt for the correct
  main-Agent-scheduled downstream child; and
- no unresolved ambiguity, uncertain effect, or pending main-Agent decision.

## Autonomous corrective loop

After every tool result, compare the receipts with the ledger and choose the
lowest-effect action with positive information gain. Reuse compatible index
snapshots, resolved assets, relationships, jobs, and artifacts. Replan only the
failed/open criteria and keep accepted work immutable.

Retry only when the Host proves non-dispatch/read-only zero effect or provides
an idempotent continuation for the same request. Do not retry an uncertain
index mutation, report, extraction, assembly, or overwrite. A provider error,
unsupported format, stale snapshot, truncated Inventory, ambiguous asset,
incompatible Skeleton, or validator failure are distinct states and require
different corrections.

A fallback is acceptable only when it still satisfies the frozen outcome and
is recorded. In particular, do not silently substitute:

- Moderate/partial Inventory for required Strong reporting;
- filename inference for an exact relationship;
- GLB for required `.uemodel` fidelity;
- separate or automatically weighted rigs for a required shared native rig;
- an approximate cooked material for the original Unreal Shader graph; or
- an unpacked match for a capture-bound Unity/Blender reconstruction package.

## Batch behavior

Freeze the batch manifest before effectful work. Each item records the exact
asset identity, dependencies, selected provider route, operation/job identity,
state, artifacts, hashes, warnings, and validation. Deduplicate shared
dependencies and previously accepted items by stable identity.

Independent items may complete separately. A partial failure does not invalidate
accepted child artifacts, but the aggregate batch remains partial until every
required item is accepted or explicitly removed by a new main-Agent-authorized
scope. Resume only failed/open items; never restart successful jobs. Preserve
per-item failure reasons and aggregate coverage/truncation.

## Main Agent decision receipt

The ordinary child Agent returns a decision receipt rather than guessing when
the remaining decision changes scope, coherent variant, report completeness,
desired material/rig fidelity, target deliverable, external effect, or accepted
data loss. The receipt includes:

- the frozen project, snapshot/Inventory, asset set, and requested outcome;
- accepted evidence, receipts, and artifacts;
- the exact open criterion and why evidence cannot decide it;
- decision owner (`main Agent` for an in-scope route choice, `user` for new
  authority or a material preference);
- the smallest Host-eligible options and their risk/fidelity/cost differences;
- one evidence-backed recommendation when possible; and
- the exact continuation point without relying on hidden provider state.

Do not ask the user directly. The main Agent decides whether a user question is
necessary and partitions any Event, Shader, capture-export, or unpacked-asset
follow-up.

## Completion gate

Complete only when the immutable source/index identities and coverage are
known, every required outcome is accepted, all effectful jobs have terminal
receipts, every artifact is hash-bound to its source set, critical validators
pass, and remaining limitations are honest bounded unknowns that do not violate
the request. A generated file, plausible preview, running job, missing
completion manifest, partial Inventory, ambiguous candidate, or pending yield
cannot satisfy completion.
