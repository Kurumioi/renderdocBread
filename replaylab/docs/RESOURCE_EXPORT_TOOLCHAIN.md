# Resource export toolchain

ReplayLab resource export uses RenderDoc as the only capture-reading layer and
publishes a versioned evidence package. The current fixed portable scene is a
glTF 2.0 JSON document plus its binary buffer. GLB and FBX are optional future
provider derivatives because neither container can represent every captured
vertex input, descriptor view, sampler, CBuffer, Shader, or pass dependency.

## Responsibility split

Fixed Host and bridge code owns capture identity, buffer reads and decoding,
raw fallbacks, DDS extraction, hashing, path containment, coordinate math,
container packing, internal validation, and truthful external-provider status. The Event
analysis child loads `renderdoc-explain-event` to identify producer/consumer EIDs
and pass dependencies. The Shader analysis child loads
`renderdoc-shader-workbench` to reconstruct algorithms and ABI-bound source. The
export child loads `renderdoc-resource-export` to select semantic mappings and
target compromises from that admitted evidence. These are ordinary bounded child
Agents with task-relevant Skills, not persistent domain roles.

No graphics API implies an application's object-space basis. A D3D capture is
not sufficient evidence to flip X. Every conversion therefore carries an
explicit source-to-target matrix, unit scale, UV policy, winding action, normal
inverse-transpose rule, and tangent-handedness rule.

## Provider registry

Providers are process-owner configuration, never model-selected executable
paths or commands. A missing provider is reported as unavailable; ReplayLab
does not download one while processing a capture. The Host-managed operation
root may be fixed with `RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT`; the model can
receive its resolved value for artifact handoff, but cannot choose or override
it. Provider configuration is not inherited by vendor Agent processes.

| Purpose | Preferred provider | Role and boundary |
| --- | --- | --- |
| Capture evidence | [RenderDoc Python API](https://github.com/baldurk/renderdoc) | Mesh/pipeline/resource/Shader reflection and replay data. The official mesh decoder is a reference, not complete support for packed formats. |
| Portable scene | [glTF 2.0](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html) | Canonical exchange scene; `_CUSTOM` attributes and `extras` are used only where valid. Exact capture-only facts stay in the sidecar. |
| glTF validation | [Khronos glTF Validator](https://github.com/KhronosGroup/glTF-Validator) | `RENDERDOC_ULTRA_GLTF_VALIDATOR` names the process-owner configured absolute executable. The Host runs the official CLI with resource validation and admits only a non-truncated JSON report with zero errors. |
| SPIR-V validation/conversion | [SPIRV-Tools](https://github.com/KhronosGroup/SPIRV-Tools) and [SPIRV-Cross](https://github.com/KhronosGroup/SPIRV-Cross) | Validate SPIR-V first, then cross-compile/reflection where the observed encoding supports it. |
| HLSL compilation | [DirectX Shader Compiler](https://github.com/microsoft/DirectXShaderCompiler) | Compiles reconstructed HLSL to DXIL/SPIR-V; it is not a general DXBC/DXIL decompiler. |
| Multi-target source | [Slang](https://github.com/shader-slang/slang) | Optional compiler for an already reconstructed, semantically reviewed source lane. |
| DDS processing | [DirectXTex](https://github.com/microsoft/DirectXTex) | Optional registered derivative conversion. Original DDS and resource/view metadata remain authoritative. |
| Unity | [Unity URP custom Shader guidance](https://docs.unity3d.com/6000.0/Documentation/Manual/urp/urp-shaders/birp-urp-custom-shader-upgrade-guide.html) | `RENDERDOC_ULTRA_UNITY_VALIDATOR` optionally names a process-owner configured adapter. It must import the package, compile the Shader, validate the scene, and write the strict Host-bound receipt described below. |
| Blender | [Blender glTF importer](https://docs.blender.org/manual/en/latest/addons/import_export/scene_gltf2.html) | `RENDERDOC_ULTRA_BLENDER` names Blender. The Host runs it in background/factory-startup mode with auto-execution disabled and a Host-generated importer/material/scene validator. A `.blend` in the validation area is evidence only after every required check passes. |
| FBX derivative | Autodesk FBX SDK, Blender CLI, or `ufbx-write` | Optional only. The receipt must name the configured provider and preserve anything it cannot encode in the sidecar. |

SPIRV-Tools, SPIRV-Cross, Slang, and the Khronos validator use permissive
licenses; DXC is LLVM-derived; DirectXTex and RenderDoc are MIT. Blender is run
as a separate GPL program, not linked into ReplayLab. Autodesk FBX SDK remains
an optional system dependency subject to its own EULA. Provider packaging must
review the exact upstream release and license rather than relying on this
summary.

## Package shape

The managed package contains the portable target plus immutable evidence:

```text
<managed-root>/operations/<operation-id>/
  operation.json
  ticket.json
  capture/
    manifest.json
    mesh/*
    textures/*.dds
    shaders/*
    cbuffers/*
  target/
    asset.gltf
    asset.bin
    transfer-spec.json
    package-manifest.json
    source/*                   # immutable copy of capture evidence
    UnityURP/*                 # when requested
    Blender/*                  # when requested
  validation/<validation-request-sha256>/
    request.json
    receipt.json
    gltf/*                     # Khronos report and bounded logs
    blender/*                  # Host script, receipt, logs, artifacts
    unity/*                    # strict request, receipt, logs, artifacts
```

Every captured or packaged payload is recorded relative to its managed capture
or target root with a byte count and SHA-256. The target manifest records
preserved, substituted, unsupported, and omitted data separately. A validator
that did not run is `unavailable`, never a pass. Validation output is outside
`target/`, so providers can create reports and derivatives without modifying the
immutable package they inspect.

## Target-validation lifecycle

After `finalize_resource_export`, call the effectful
`run_resource_export_target_validation(operation_id)` tool. The caller supplies
only the opaque operation ID. Executable paths, package paths, scripts,
arguments, timeouts, log locations, and output locations all come from fixed
Host configuration. The Host launches providers without a shell, caps runtime
and log size, and re-hashes the complete target package after the providers
return.

The validation request binds the operation-request SHA-256, immutable package-
manifest SHA-256, finalize SHA-256, exact requested targets, and every provider
executable SHA-256. Its own SHA-256 is the validation cache key. A repeat call
revalidates and reuses the matching receipt; changing a provider or package
produces a different request or fails integrity validation.

`validate_resource_export(operation_id)` remains read-only. It never launches a
program and never upgrades provider detection to success. It recomputes package
hashes, loads only the exact cached receipt, verifies every recorded artifact,
and sets `targetValidationComplete=true` only when every requested layer has
genuinely passed. A missing or not-yet-run provider leaves completion false. A
provider failure or malformed/tampered receipt makes validation invalid.

### Provider receipt protocol

Blender receipts use
`replaylab-resource-export-blender-provider-receipt/v1`; Unity receipts use
`replaylab-resource-export-unity-provider-receipt/v1`. Both echo the exact
operation ID, validation-request SHA-256, operation-request SHA-256, package-
manifest SHA-256, and provider-executable SHA-256. They also include a real
provider version, boolean required checks, and a complete relative-path artifact
manifest with byte counts and SHA-256 values. The Host rejects redirected,
missing, extra, duplicate, out-of-root, oversized, or hash-mismatched artifacts.

The fixed Blender route requires glTF import, mesh integrity, Principled
material-node construction, image color-space setup, and scene-link integrity.
It currently fails closed when capture material CBuffer mapping, a custom Shader
strategy, or texture-node reconstruction cannot be proven. The scaffold is not
promoted to a validated `.blend` merely because Blender saved a file. The
optional Unity provider must independently pass mesh import, material import,
texture import, Shader compilation, and scene integrity. Its request contains
only Host-derived managed paths and relative output names.

## Current fixed scope

The P1 bridge exports conventional raster draw input (`VSIn`) for `view=0`.
Indexed state comes from RenderDoc action flags, not a possibly stale index
buffer; base vertex, first vertex, instance offset, and instance divisor are
handled explicitly. DDS export preserves all known MSAA samples as array slices.

Packed/special vertex formats retain exact raw bytes and remain canonical-
unsupported. Primitive restart currently retains source indices but blocks glTF
finalization until deterministic primitive splitting is implemented. `view>0`,
post-transform/mesh-shader geometry, authored skeletons/animation, and raw
push/root/inline CBuffer bytes are explicit unsupported or unavailable states.
Buffer-backed CBuffer bytes remain authoritative; the legacy live-value tree is
convenience evidence and may truncate wide vectors or omit trailing zero lanes.

`validate_resource_export` separates internal capture/package checks from
external glTF, Unity, and Blender validation. Unity and Blender package outputs
remain scaffolds until the corresponding target program produces an admitted
receipt; a generated bundle alone is not a `.blend` or Unity import-success
claim.

## Fidelity profiles

- `exact-capture/pass-bundle`: preserve multiple EIDs, MRT/GBuffer resources,
  and separate Shader stages/passes.
- `portable-forward`: retain the draw-local material/surface calculation and
  replace ordinary camera, light, shadow, fog, and environment services with
  target-native equivalents.
- `bake-fallback`: bake a bounded dependency only when the user accepts losing
  its dynamic behavior.

Single-frame input-assembler data does not prove skeleton hierarchy or
animation. Post-VS/GS/mesh output is a posed or baked fallback and must not be
reported as the original authored rig.
