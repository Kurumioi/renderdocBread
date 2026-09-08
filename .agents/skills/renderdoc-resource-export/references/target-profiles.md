# Target profiles and transfer policy

A target profile is an explicit, hash-bound input to the export. It determines
representation and adaptation; it does not change the observed source facts.

## Target profile contract

Record:

- target family and version when relevant;
- rendering pipeline and shader backend;
- mesh container and any required sidecar or importer;
- coordinate handedness, up axis, forward axis, unit scale, UV convention, and
  front-face winding;
- texture container, compression policy, and color-management convention;
- material/shader representation;
- dependency policy;
- requested output and validation classes.

If the user requests Unity without naming a render pipeline, Unity URP is the
default profile. Record that default in the request and report material
features that cannot be represented faithfully. Do not silently choose among
forward, deferred/MRT, custom render feature, or multiple-pass output when that
choice changes the reconstructed effect.

For Blender, select an explicit renderer/profile when the distinction matters.
Use a native material-node graph for representable operations. Preserve custom
or irreducible core calculations in an admitted target-specific node group or
shader representation, and record renderer limitations.

## Container policy

Use the selected container for the channels it can represent faithfully. A
package manifest and lossless sidecar are required whenever source information
has no standard container representation.

- Standard geometry channels may be placed in FBX or glTF according to the
  target importer.
- Additional UVs, colors, skin data, instance inputs, and custom attributes
  must be mapped to supported custom channels or preserved in a sidecar with
  source layout and destination reconstruction instructions.
- Procedural, tessellated, geometry-stage, or mesh-stage output must be labeled
  as baked/post-transform geometry when exported that way.
- Draw arguments, original buffer ranges, source formats, and hashes remain in
  the manifest even if the target container normalizes or repacks them.

Do not describe a package as lossless merely because its visible default view
looks similar.

## Coordinate conversion

Represent source-to-target conversion as an explicit matrix and profile, not as
a hard-coded axis negation. Apply it consistently to geometry, instances,
skeletons, bind poses, animation-related transforms, cameras included by scope,
and shader-space assumptions.

- Transform positions by the full affine transform.
- Transform directions by its linear component and renormalize when needed.
- Transform normals by the inverse transpose of the linear component.
- Transform tangent direction consistently and adjust tangent handedness when
  the orientation determinant changes.
- Reverse triangle winding exactly when the conversion changes orientation.
- Preserve index/topology correspondence after winding conversion.
- Convert matrices with the documented source/target multiplication and storage
  convention; do not confuse matrix layout with vector convention.
- Apply unit scale exactly once.
- Flip a UV axis only when required by the target texture/sampling convention,
  and keep texture transforms and shader sampling consistent.

The conversion receipt must include source and target axes, handedness, unit
scale, transform matrix, determinant sign, winding action, normal rule, tangent
rule, and UV rule. Validation must test these as a single invariant set.

## Texture and color-space mapping

Map the observed view and sampling behavior to target import settings:

- sRGB-decoded color data becomes an sRGB/color texture when the target offers
  the same decode.
- linear data, normals, masks, depth, data LUTs, and integer content use the
  corresponding non-color/data setting.
- Preserve channel swizzles, normal-map convention, alpha meaning, sampler
  address/filter state, mip policy, and comparison behavior in material or
  importer metadata.
- If the target cannot express a sampler per texture use, duplicate the target
  asset or record the shared-sampler compromise rather than silently selecting
  one use.

Do not decide color space from an extension, filename suffix, or visual guess.

## Unity URP adaptation

Prefer target-native URP facilities for ordinary engine services such as
object/camera transforms, shadows, fog, ambient/environment lighting, light
lists, and standard depth conventions. Preserve the selected draw's distinctive
material and surface calculations rather than reproducing capture-global
plumbing unnecessarily.

Generate the smallest suitable target representation:

- use URP Lit or Unlit material properties when the observed calculation maps
  without losing the requested core effect;
- use ShaderLab/HLSL for custom core calculations;
- set surface type, blend factors, alpha clipping, culling, depth write/test,
  queue, render tags, and exposed parameters from evidence;
- map system CBuffer values to Unity built-ins and per-material values to Unity
  material properties;
- include importer metadata/scripts only when needed to reconstruct streams,
  textures, sampler settings, or materials deterministically.

For a GBuffer or deferred-lighting dependency, present bounded alternatives:

1. combine the observed material and lighting contribution into a forward URP
   shader;
2. export separate material and lighting passes with an admitted URP renderer
   feature or MRT plan;
3. replace ordinary lighting with URP-native lighting while retaining the core
   material algorithm;
4. omit the dependency and label the visual loss.

Do not choose among materially different alternatives without authority.

## Blender adaptation

Create a deterministic mesh/material setup for the selected Blender target:

- map standard material operations to native nodes and group related custom
  calculations into clearly named node groups;
- set each image texture's color-space mode from sampling evidence;
- preserve texture coordinate set, mapping transforms, channel extraction,
  normal-map convention, alpha/blend mode, culling, and displacement policy;
- map object/view/system values to Blender-native coordinates or drivers where
  meaningful, not captured constants;
- record differences between Eevee and Cycles when they affect the result.

If a shader operation cannot be represented by the selected Blender material
system, retain the source evidence and state the smallest explicit compromise.
Do not invent an equivalent based only on a visual label.

## Deterministic operations versus child-Agent judgment

Deterministic tools should decode buffers, repack formats, transform coordinate
data, export textures, construct known target templates, assemble manifests,
hash artifacts, and run target validators.

The ordinary child Agent decides source semantics, required dependency scope,
which core algorithm must survive, whether a target-native replacement is
equivalent, and which unresolved alternatives require a main-Agent decision.
A deterministic operation must not hide a semantic decision behind an `auto`
option.
