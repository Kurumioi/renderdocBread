# UEX SkeletalMesh and Blender delivery

## Exact selection and job scope

Require exact cooked virtual SkeletalMesh paths. For a modular character, pass
body, face, hair, clothing, and other required parts as separate selected
assets. Select one coherent replacement variant; do not export every variant or
broaden one selection into a character folder/full-game export.

Use one Host job for the exact admitted logical selection and poll its returned
identity until terminal. A queued or running job is still active. Never start a
duplicate because conversion is slow, and never surface AES keys; only
Host-redacted fingerprints may appear.

## Bundle contract

Each selected asset produces a self-contained directory whose stable entry is
`manifest.json` with schema `uex.blender-bundle.v1`. Preserve at least the exact
`sourceAsset`, mesh name, Skeleton and bone count, source LOD and morph-target
counts, exported meshes by LOD, optional `.uemodel`, material bindings, decoded
textures, and warnings. Every path inside a bundle is relative; reject absolute
paths and `..` traversal.

The aggregate delivery receipt includes an assembly manifest with schema
`uex.blender-assembly.v1`, exact source assets, bundle manifests, geometry
source, final `.blend` and SHA-256, options, validation report, and terminal
status. Keep the first compatible selected bundle as the leader Skeleton.

## Geometry and shared rig

Prefer `.uemodel` through an admitted compatible UEFormat importer with scale
`0.01`, bone reorientation disabled, sockets/collision disabled, and morph
targets enabled when requested. It preserves Unreal-oriented rigs and fuller
skinning. Use GLB only as an explicitly recorded fallback; a visually plausible
rest pose does not prove correct deformation pivots.

Before sharing an armature across modular parts or LODs, compare bone count,
bone names and parents in order, every local rest matrix within tolerance, and
armature object transforms. Then redirect each mesh's Armature modifier and
parent to the leader, preserve native vertex groups/weights, and remove the
redundant armature only after all references move. Fail on incompatibility;
never repair it with automatic weights or a silent partial merge.

Place matching parts in collections ending with `LOD0`, `LOD1`, and so on.
Keep LOD0 visible and hide higher LOD collections by default. Attach all LODs
to the compatible leader armature. These collections are review organization,
not an Unreal LOD asset.

## Material approximation

Treat the bundle's `materials[].baseColor` binding as authoritative when its
confidence is sufficient. Texture suffixes and names are search hints only.
Inspect direct/inherited parameters, material-instance and raw material data,
switches, and warnings before overriding a missing binding.

The Blender material is a review approximation of cooked data:

- `Opaque`: Base Color only;
- `Masked`: Base Color alpha with a conservative threshold where supported;
- `Translucent`: alpha with a transparent surface method;
- `Additive`: transparency plus emission, with the blend difference recorded;
  and
- `Modulate`: transparent approximation with the Unreal compositing difference
  recorded.

Do not invent Base Color for a missing/low-confidence binding. Cooked Shader
bytecode is not the original Unreal graph; custom shading, Substrate, virtual
textures, collections, layered materials, and animated effects remain explicit
manual-reconstruction gaps.

## Validation gate

Treat a nonzero job/validator exit as failure even when a partial `.blend`
exists. The validation artifact must check for at least:

- one or more meshes and armature bones;
- an enabled Armature modifier, weighted vertex groups, and assigned weights on
  every skinned mesh;
- exactly one referenced shared armature for a compatible modular assembly;
- a present and visible LOD0 collection with higher LODs hidden by default; and
- no missing external image referenced by an unpacked material.

Zero decoded images can be valid for an untextured material and is a warning,
not a critical error. Deliver the `.blend`, all immutable bundle manifests, the
assembly manifest, and the validation report together. Claim pose fidelity only
when the rig/deformation checks support it.

For a terminal partial Host receipt, continue only with
`continue_unpacked_asset_blender_export` and its exact parent request/result
hashes. Never pass retained manifest paths or resubmit successful assets. The
Host must re-hash retained bundles, derive failed/open items, and assemble the
original ordered set of retained plus new manifests before this validation
gate can pass. An assembly-only retry with all bundles retained must not invoke
UEX export again.
