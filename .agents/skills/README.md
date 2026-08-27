# ReplayLab RenderDoc skills

These are the canonical Agent Skills for both ReplayLab AgentHost and Codex
app-server. They organize stable ReplayLab MCP operations by user workflow,
not one skill per low-level tool.

The seven catalog domains are event/pipeline, pixel/texture, shader, material
bindings, performance, resource analysis, and disabled export/interoperability.
`allowed-tools`
is declarative input, while AgentHost independently intersects it with the
authoritative read-only Host policy and enforces required evidence tools.
Export/effectful tools remain unavailable until qrenderdoc issues a native,
nonce-bound capability ticket; skill text is never mutation authorization.
