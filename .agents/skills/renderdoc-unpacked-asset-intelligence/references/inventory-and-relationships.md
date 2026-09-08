# Inventory and relationship contract

## Active-project boundary

The active ReplayLab project is the authority boundary. Its Host-admitted
source roots, provider profiles, inventories, capture bindings, and managed
artifact directories define what may be inspected or changed. Never accept an
arbitrary absolute path, traverse above an admitted root, or widen a query into
a provider/profile-wide extraction.

Use a persisted compatible snapshot before scanning. Indexing is a project
effect and requires the assigned workflow to admit that effect. A read-only
query that can be answered from a complete current snapshot must not refresh it
merely to obtain newer timestamps.

## Stable observations

Each asset observation must retain enough provenance to be reproduced:

- active-project and snapshot identity;
- provider/profile and provider-version fingerprint;
- normalized virtual path and asset kind;
- source-relative path where the Host permits it;
- content or provider identity/hash and byte length when exposed;
- source timestamp only as a change hint, never as content identity;
- parsed format/schema version, coverage, warnings, and truncation; and
- observed fields with explicit present, missing, unsupported, or invalid
  state.

Prefer content-addressed observations. An incremental scan may use size and
timestamp to avoid unnecessary parsing only when the Host preserves a content
verification strategy. Changed assets create new current observations. When the
Host exposes historical snapshots, removed assets become tombstoned/stale there.
Otherwise, preserve hash-bound receipts from prior work but report historical
catalog lookup as unavailable; never present a removed asset as current.
Never merge records solely by basename.

## Relationship table

Persist relationships as evidence-bearing edges, not prose-only notes. An edge
contains source asset identity, relation type, target asset identity, provider
and snapshot, evidence references, derivation class, confidence, and freshness.
Useful relation types include:

- mesh uses material, Skeleton, animation, morph target, or LOD source;
- material/material instance inherits a parent and binds textures/parameters;
- Shader or material references a texture, sampler, or parameter collection;
- multipart role contains body, face, hair, clothing, or weapon components;
- capture resource or Shader evidence matches an unpacked candidate; and
- generated report, bundle, assembly, or validation artifact derives from an
  exact asset set and snapshot.

Mark edges as observed, deterministic-derived, or heuristic. Only observed or
deterministic-derived edges may satisfy an exact dependency. Heuristic edges
remain ranked candidates until corroborated. Preserve negative and ambiguous
results in the child receipt so the main Agent does not schedule another child
to repeat the same search without new evidence.

## Query and exact resolution

Query broadly enough to produce candidates, then resolve narrowly. An exact
resolution requires the provider identity and normalized virtual asset path;
content hashes, class, Skeleton, material chain, dimensions, or other fields
strengthen identity but must not be fabricated when unavailable.

Rank capture-to-unpacked candidates from admitted intersections such as exact
content/hash, Shader identity, vertex layout, index/topology counts, texture
dimensions/format, material bindings, CBuffer parameters, and project symbol
evidence. Record which dimensions agreed, disagreed, or were unavailable.
Directory proximity, basename, suffix, and human-readable labels are weak
hints. Never auto-select tied or materially conflicting candidates.

`prepare_active_capture_project` is effectful project preparation. Use it only
when the main Agent assigned persistent capture correlation and the current
capture is not already admitted. `query_active_shader_asset_evidence` supplies
project-backed Shader/asset associations. `inspect_resource_export` supplies
capture-bound mesh, texture, sampler, CBuffer, and Shader evidence. The result
of correlation is an evidence bundle for the main Agent or
`renderdoc-resource-export`, not permission to export the capture.

## Reuse and child receipts

A reusable child receipt names the immutable snapshot, requested query, exact
assets or ranked candidates, dependency closure, relationship edges, evidence
hashes, coverage, warnings, and unresolved ambiguity. The main Agent and
downstream ordinary child Agents must be able to consume it without rescanning
the unpacked roots. If the snapshot is stale relative to a requested
reproducibility boundary, keep the old receipt valid for its original snapshot
and create a new one rather than mutating its meaning.
