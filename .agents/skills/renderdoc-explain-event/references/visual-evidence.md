# Event visual-evidence contract

Use this contract whenever an Event/Pipeline analysis needs an RT or DSV image,
an isolated channel, a depth display range, a GBuffer explanation, or a rendered
flow diagram. The visual is explanatory evidence derived from capture facts; it
does not replace the underlying resource, state, Shader, or contribution
receipts.

## Decide whether to capture visuals

Capture visual evidence only when at least one trigger is true:

- the user explicitly asks to see, save, compare, or diagram it;
- a missing-output or actual-contribution question materially needs an output
  observation or a bounded before/after comparison;
- a packed GBuffer, depth attachment, or individual channel must be inspected
  to resolve a material semantic question; or
- representative pass boundaries need an input/output comparison.

Do not dump every draw or every attachment. For a whole frame, select bounded
representatives at meaningful pass boundaries. Begin with the combined colour
view and add individual channels only when format, write mask, Shader evidence,
or the user's question shows that a channel may carry distinct information.
Record why every extra view was selected. Absence of a trigger is a valid
`not-applicable` outcome, not an invitation to create files.

The task's shared MCP call budget and each tool's artifact limits apply.
The texture Host checks `budgets.maxTextureByteSize` (256 MiB),
`maxSubresourcePixels` (16 Mi pixels), and `maxPngByteSize` (128 MiB) per view;
use the returned budget and skip receipt. The combined `rgba` overview is a
useful starting point when interpreting an unfamiliar RTV, but is not a Host
prerequisite for an explicitly requested individual channel. Raw DSV depth is
independent of colour views. Stop when the requested conclusion has enough
evidence.

Discover unknown EID, binding and subresource dimensions with authorized reads,
then call the supported tool with that exact view. Preserve each requested
output in the final report: R and G views are distinct requests. Match the
receipt's capture/session, EID, slot, mip, slice, sample, channel and display
range to the request. For diagrams, match kind, EID scope, action bound and
theme. A receipt for a different view is useful evidence but does not fulfill
the requested artifact.

Reuse matching successful artifact receipts across follow-up turns. The graph
publisher can return `reused=true` for identical content; this is a valid
receipt, not a failed duplicate. Texture export allocates a new managed request
directory, so repeating it creates another artifact. Do not repeat a completed
export merely to recover a path or improve a report.

## Runtime-contained RT and DSV artifacts

- Use `capture_event_texture_evidence` for a runtime-contained Event view. Never
  expose the legacy arbitrary-path `save_texture` operation to the child Agent,
  never invent an output path, and never use a screenshot as source-byte proof.
- Use `attachment_kind=rtv` with `channel=rgba/r/g/b/a`, or
  `attachment_kind=dsv`, `attachment_index=0`, `channel=depth`. Select
  `range_mode=native`, `explicit`, or `subresource-minmax`. Only `explicit`
  accepts `black_point` and `white_point`. These are actual tool parameters;
  inspect bindings before choosing an attachment or subresource.
- Bind the request to the frozen capture session and exact positive EID. The
  Host must replay that EID, confirm that each requested resource is actually
  bound at the claimed RTV slot or DSV binding, validate the exact
  mip/slice/sample/plane, save the view, restore the original EID, and return a
  strict receipt. A capture/session drift or binding mismatch fails closed.
- A receipt must distinguish a display-derived PNG from a lossless texture
  export, include resource/binding/subresource/source format and dimensions,
  channel and range mapping, MIME type, byte size, SHA-256, capture session,
  EID, restoration result, and any skipped/budget receipt. Do not describe a
  display PNG as the captured texture's original bytes.
- The Host owns a contained artifact root, unique names, no-overwrite and
  atomic publication. The model must not choose paths or derive paths from
  capture-controlled names. Reject traversal, symlink/reparse escape, partial
  publication, invalid hashes, and unsafe size/count/pixel budgets.
- MSAA sample or resolve behavior must be explicit. Never silently turn a
  requested sample into a resolve. Cube, array, 3D, stencil, and typeless cases
  remain unknown or unsupported unless the Host proves their exact handling.
- A before/after comparison requires observed executable timeline boundaries
  and the same resource/subresource. Equal hashes of complete raw resource
  bytes can establish unchanged bytes; equal display PNGs only establish an
  unchanged display under the same format, channel and range mapping. Equal
  bounded samples do not establish unchanged full resources; a differing raw
  sample can establish a change at that point. A bound output or declared write
  alone remains an `output-affecting-candidate`, not an observed contributor.

## Colour and individual-channel views

- `rgba` uses the combined display. `r`, `g`, `b`, and `a` are deterministic
  single-channel grayscale views of the exact same subresource. Preserve the
  channel selection and black/white mapping in the receipt.
- Prefer the channels enabled by the attachment write mask and PS output
  signature. A disabled write channel must not be described as updated by this
  draw. A zero write mask, absent PS output, discard uncertainty, or unknown
  raster applicability blocks a positive update claim.
- Plain channel extraction does not decode octahedral normals, packed material
  bits, sRGB intent, signed encodings, or engine-specific compression. Such
  meanings require validated Shader logic; otherwise show raw channel values
  and state the unknown encoding.

## Depth range

- The minimum version displays raw device depth only. Do not call it linear,
  view-space, eye-space, or world-space depth unless a separate validated
  reconstruction establishes the projection matrix, API clip convention,
  near/far interpretation, and reversed-Z behavior.
- Request `range_mode=subresource-minmax` for Host-computed bounds on the exact
  EID, resource, mip, slice and sample being displayed. Never reuse a base mip's
  range for a different subresource. Preserve the returned range evidence and
  exact black/white mapping; the tool has no caller-defined plane/type-cast field.
- Explicit bounds must be finite and strictly increasing. For a real depth
  attachment, raw display bounds must remain within `[0, 1]`. Reject NaN,
  infinity, an inverted range, or an unproved convention. If min equals max,
  preserve the Host's `subresource-range-flat` skip receipt; do not manufacture
  contrast.
- A frozen Texture Viewer range is usable only when the frozen viewer context
  matches the same resource and subresource. A later UI change cannot alter the
  request. Do not claim percentile clipping because the current toolset has no
  verified percentile/histogram contract.
- Treat stencil or ambiguous depth/stencil planes as unsupported until a
  dedicated, tested plane contract exists.

## GBuffer attachment report

Treat “three or more MRTs” and resource names as candidate signals only. Never
assign `Albedo`, `Normal`, `Roughness`, `Metallic`, `MaterialID`, motion, or any
other semantic from slot number, name, format, or convention alone.

For every relevant attachment report:

- RTV slot, resource identity, format, exact subresource, sample count, blend
  and write channels;
- matching PS output register and component masks when reflection exposes them;
- producer EIDs, downstream consumers, and exact resource-flow evidence;
- combined and selected channel artifact IDs, when visual evidence was
  triggered;
- each proposed channel meaning with `observed` or `inferred`, confidence,
  Shader-workbench evidence references, and explicit unknowns.

Group Shader analysis by exact PS identity. Use Host shader observations, or
in API mode ask the main Agent for a bounded shader-domain task, to determine
how output registers and components are written or packed. Ask a concrete
output-packing or GBuffer question; add discard coverage, depth or blending
only when that separate question materially affects the conclusion. Use the
returned shader evidence before claiming a semantic label. If reflection or
Shader evidence is absent, keep the attachment as `RT<n>` plus physical facts
and mark its meaning unknown. Explain depth as a separate DSV attachment, not
as another colour GBuffer target.

## Rendered flow diagrams

There are three distinct views; select the smallest set that answers the user:

1. `resource-flow` — producer/consumer EIDs and definite resource handoffs.
2. `pipeline-stage` — only the API stages that current action/pipeline evidence
   proves were actually bound or involved, retaining compact identifiers such
   as `IA`, `VS`, `PS`, `OM`, or corresponding Vulkan/OpenGL abbreviations. Do
   not add `CS` to a graphics draw; show it only for an observed dispatch or an
   actually bound participating compute stage.
3. `pass-overview` — category to pass to representative EIDs, with bounded
   GBuffer attachment summaries when applicable.

Use `render_current_frame_flow_diagram` within the granted `workspace` effect.
Select `resource-flow` or `pass-overview` with `event_id=0` for a bounded frame
flow, or `pipeline-stage` with one positive exact `event_id` for its
participating API stages. Keep the default `max_actions=8000`; raise it only
for a necessary larger inventory, never above the supported 100000 actions.
The tool has no output-path or graph-IR parameter and fixes the theme to
`evidence-neutral-v1`. The model does not submit free CSS, Mermaid
initialization, `style`, `class`, HTML, URLs, or click actions. Graph facts and
edges come from Host capture observations. The renderer returns an SVG receipt with graph
kind, capture/session scope, source and graph-IR digests, artifact/manifest
paths and SHA-256 values, node/edge counts, omitted counts, and theme. Cite that
receipt and surface the managed artifact path in the result.

If the renderer or managed export is unavailable after safe bounded retries,
preserve the accepted graph facts and return the compact chronological table.
Mark only the requested
rendered artifact as unavailable. Do not turn a presentation failure into a
claim that the Event has no analyzable evidence, and do not ask the user to pick
invented alternative stages or resources.

Use left-to-right chronology by default. Keep exact EIDs visible. Use short
labels, a legend, shapes/borders/line patterns as well as colour, and sufficient
light/dark contrast so colour is never the only meaning. Preserve the existing
40-node/64-edge evidence bound and return an explicit collapsed/truncation
receipt. Unsafe capture text is escaped by the Host. Use the supported renderer
and fixed theme; do not substitute a generic visualization service.
