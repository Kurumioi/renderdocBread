# Package and validation contract

The export is a Host-owned artifact transaction, not a collection of unrelated
file writes. Every preparation, finalization, resume, and validation operation
must use the same admitted request hash.

## Managed artifact lifecycle

`prepare_resource_export` may create a staging area only beneath the
Host-managed artifact root. It must reject absolute paths, parent traversal,
reparse or symlink escapes, stale capture identity, mismatched request hashes,
and collisions with another request.

Preparation should:

- snapshot admitted source and route fingerprints;
- decode and transform only the requested resources;
- write deterministic candidate artifacts into staging;
- record every warning, bounded unknown, and unsupported mapping;
- return a receipt identifying created effects and remaining criteria.

`finalize_resource_export` may run only after the source and transformation
criteria pass. It must:

- assemble the package from admitted staging artifacts;
- generate the manifest and per-file hashes;
- perform built-in structural checks;
- publish atomically to a new immutable destination;
- never overwrite a prior successful or failed artifact;
- return an exact artifact and effect receipt.

`get_resource_export_status` may inspect or resume only the same logical
request. A retry must preserve passed criteria and completed effects, and use a
new attempt identity linked to the original request. It must not retarget a
staging directory after a capture or selection change.

## Package contents

The package manifest is canonical even when the target importer also embeds
metadata. It should contain:

- schema and exporter versions;
- request hash and capture/session/EID identities;
- target, coordinate, dependency, and validation profiles;
- source resource, shader, and admitted prerequisite hashes;
- mesh/topology/index/vertex/instance mappings;
- texture/subresource/view/sampler/color-space mappings;
- CBuffer layouts, classifications, values, and target property mappings;
- shader artifacts, target substitutions, and dependency compromises;
- relative artifact paths, media types, byte sizes, and SHA-256 hashes;
- unsupported fields and bounded unknowns;
- preparation, finalization, and validation receipts.

All artifact paths are relative to the package root. The manifest must preserve
the source-to-destination mapping for data repacked or represented in a sidecar.
Generated importer scripts and target project fragments are artifacts and must
be hashed like meshes, textures, and shaders.

## Required receipts

A valid receipt records:

- logical request and attempt IDs;
- capture/session/EID and selected route fingerprints;
- exact source resource and raw shader hashes;
- converter and validator identities, versions, and registered hashes;
- coordinate conversion profile and transform result;
- every created relative path, byte size, and content hash;
- completed effects and preserved prior effects;
- warnings, failures, bounded unknowns, and remaining criteria;
- current and restored RenderDoc event identity;
- immutable package identity when publication succeeds.

Do not accept a free-form success string or a path alone as a receipt.

## Validation layers

`validate_resource_export` must report each requested layer separately.

### Structural validation

- manifest schema, required entries, relative paths, hashes, and sizes agree;
- mesh indices, vertex counts, stream lengths, topology, skin data, and instance
  counts are internally valid;
- texture dimensions, subresources, encodings, and referenced files agree;
- material, shader, texture, mesh, and importer references resolve;
- no artifact escapes the managed root or mutates an earlier package.

### Semantic and fidelity validation

- every observed vertex input is mapped or explicitly unsupported;
- index/base-vertex and per-instance interpretation match source evidence;
- texture view, swizzle, sRGB/linear interpretation, sampler, and mip behavior
  match the recorded transfer policy;
- CBuffer layout and material/system mapping preserve types and values;
- coordinate conversion, winding, normals, tangents, UVs, instances, and bones
  satisfy one consistent transform receipt;
- shader interface and material properties agree;
- included, substituted, and dropped dependencies match the approved policy.

### Target validation

Use only an admitted non-interactive validator. For Unity, validate importable
mesh/material/texture assets, shader compilation for the selected URP profile,
and importer reconstruction when requested. For Blender, validate headless
mesh import, material/node construction, image color-space settings, and scene
link integrity for the selected renderer.

If no target validator is available, do not claim target validation. Mark it as
an unavailable capability and report which structural and semantic checks did
run. Do not install Unity, Blender, an SDK, or a converter to satisfy the check.

### Replay postcondition

After every operation that changes the current EID or replay state, verify that
the original event and capture context are restored. An uncertain or failed
restoration blocks completion and yielding until resolved or safely cancelled.

## Failure and recovery

Keep failed candidates immutable for diagnosis, but never promote them as the
successful package. Retry only the failed criterion with an admitted fallback
route. Do not repeat a successful external effect or overwrite a previous
attempt.

Do not fall back to `export_drawcall`, `export_to_unity`, `save_texture`, a GUI
automation path, or an invented command. Those paths lack the managed artifact,
exact source, receipt, and target validation guarantees required here.
