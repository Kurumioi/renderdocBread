# UEX asset report semantics

## Inventory quality gate

Generate statistics only from a persisted UEX Inventory. Do not reopen paks or
infer missing cooked values while reporting. Require discovery confidence
`Strong`, then select a concrete Inventory whose own metadata is `Strong`,
complete, and not truncated. Prefer the newest acceptable snapshot, but bind
the job to its exact Inventory id so a newer Moderate snapshot cannot be chosen
implicitly.

Partial or non-Strong reporting is allowed only after the user explicitly
accepts an incomplete result. Preserve all resulting gaps and warnings. Missing
or unavailable cooked index, vertex, bone, animation, or texture metadata stays
null/`missing`; it is never converted to zero.

## Project grouping rules

Validate the project specification against actual Inventory paths before
aggregation. Do not apply one game's folder conventions to another project.

- Exclude presentation duplicates such as `Default/Show` only through an
  explicit narrow rule.
- Retain the authoritative `Combat` assembly.
- Classify other unmatched valid states as `Additional` so they remain
  auditable.
- Map components to `Role`, `Weapon`, or independent categories such as `Pet`,
  `Prop`, `Summon`, or vehicle.
- Scope scene patterns narrowly and exclude collision, proxy, placeholder,
  test, cinematic, and other technical content.

## Character, LOD, and Skeleton aggregation

A row represents one logical character assembly rather than one Unreal asset.
`Role` combines every mesh assigned to the Role component for the same
character, variant, and state. `Role+Weapon` is the union of those Role and
Weapon meshes. Other components stay independent unless the project spec
explicitly combines them.

Sum triangles and vertices across included meshes. At requested LOD `n`, a
component without exact LOD `n` may use its last native LOD at or below `n`; if
no effective LOD exists, the value is unavailable.

Never sum bone counts per submesh. For each requested LOD:

1. group skeletal meshes by exact Skeleton reference;
2. for each shared Skeleton, take the maximum active-bone count among its
   included meshes; and
3. sum those maxima only across distinct Skeletons.

Reference-bone counts follow the same distinct-Skeleton rule.

## Animations and textures

Associate animations through exact Skeleton references, not filename or folder
proximity. Count clips and sum only available durations. Histogram clips by the
configured frame-count bins; clips with missing frame counts remain in the
total but not the histogram. `Core` is an explicit project-spec path marker and
may be compared with All. Curves, notifies, physics, and montage semantics are
outside this report.

Resolve textures through mesh -> material/material instance -> parent material
-> texture references. Deduplicate exact texture assets before grouping by
width x height. Resolution and texture counts are in scope; material slots,
Shader fidelity, compression quality, and estimated memory are not silently
derived.

## Scene categories and P90

Only assets within the configured scene scope are candidates. Apply global
exclusions first, then ordered category rules; send unmatched in-scope meshes
to the configured fallback, normally `Other`. Common categories such as large
buildings and vegetation must be defined by project rules rather than guessed
from one filename.

Scene distributions use available LOD0 triangle counts. Compute P90 by nearest
rank: sort the `N` available values ascending and select the one-based item at
`ceil(0.90 * N)`. P90 is neither a mean, maximum, nor budget. A model with
missing LOD0 remains in the category model total but is omitted from the
histogram and P90 population with a warning.

## Portable output and validation

One report job produces:

- `asset-report.html`, self-contained with no CDN;
- `asset-report.json`, the complete renderer-independent model;
- `asset-summary.csv`, the flat review/BI extract; and
- `asset-report-manifest.json`, written last as the completion marker.

The JSON, HTML, and completion manifest must directly identify the same selected
Inventory. The flat CSV need not duplicate that metadata when the manifest
binds its exact name, byte length, and SHA-256 into the same completed bundle.
Verify grouping diagnostics, missing value counts, coverage/truncation, output
hashes, and Inventory identity before delivery. An HTML file without the other
artifacts and completion manifest is not a successful report.
