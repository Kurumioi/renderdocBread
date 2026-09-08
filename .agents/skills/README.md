# ReplayLab RenderDoc Skills

These filesystem Skills are the current bundled procedures and shared output
standard. API mode uses the pinned GitHub `deepagents` framework with a main
Agent and bounded domain children. ACP mode uses the vendor's own single Agent;
Codex app-server is the other supported native transport.

- `replaylab-core` is mandatory for API main Agents, API children and every
  native Agent, including follow-up threads. It defines Host templates,
  evidence labels, acceptance, bounded corrections and reports. It is marked
  `replaylab-always: "true"` and is never a selectable domain. A missing or
  invalid core stops execution; it cannot be silently quarantined and skipped.
- `renderdoc-explain-event` covers EIDs, draw/dispatch, pipeline, passes and
  frame-flow analysis.
- `renderdoc-shader-workbench` covers reconstruction, readable HLSL, analysis,
  numerical comparison, compile/ABI checks, replacement, external CBuffer
  aliases, editable workspaces and project reuse.
- `renderdoc-resource-export` covers capture-bound mesh, texture, sampler,
  material, CBuffer and shader export into managed Unity/Blender packages.
- `renderdoc-unpacked-asset-intelligence` covers authorized project asset
  catalogs, Strong UEX Inventory reports, capture matching and cooked UE
  SkeletalMesh/Blender delivery.

The runtime catalog selects domain procedures from their metadata. Core rules
are injected in full independently of that selection. Invalid optional domain
Skills are quarantined individually. Skills do not define Agent topology, MCP
registration, tool allowlists or effect authority. The task-scoped Host tool
inventory and user authorization remain authoritative.

Models fill the typed templates returned by Host tools; Host renderers own
diagram/document style and deterministic checks own acceptance. Domain Skills
describe their procedure and tool-specific fields without duplicating the
shared standard. The contract unifies output structure and evidence quality;
it does not promise identical wording or semantic conclusions across models.

Corrections are bounded by Host verdicts. Blocking errors remain rejected when
the budget is spent; minor issues can pass with explicit limitations. A useful
partial result is valid when the evidence supports only part of the request.
Keep completed effects and receipts, and inspect uncertain mutations before
retrying. Ask the user only when an unresolved choice materially changes the
target, scope or authorized effect.

The old runtime Agent registry, virtual configuration Skill, per-child Skill
switch loop and custom planner/reviewer are not part of this architecture.
Historical migration documents describe their removal, not active extension
points. Change provider configuration through the current Settings/profile
interfaces, not an obsolete Agent-owned configuration workflow.
