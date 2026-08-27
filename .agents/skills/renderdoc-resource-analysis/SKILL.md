---
name: renderdoc-resource-analysis
description: Inspect RenderDoc texture/buffer metadata, resource bindings, and producer/consumer relationships. Use for missing resources, wrong texture bindings, suspicious geometry metadata, or finding events by resource without unbounded raw-data reads.
metadata:
  replaylab-category: resources
  replaylab-read-only: "true"
  replaylab-min-successful-tools: "1"
  replaylab-required-any-tools: get_textures get_buffers get_resources get_texture_info get_bound_textures find_draws_by_texture find_draws_by_shader find_draws_by_resource
  replaylab-version: "1"
allowed-tools: get_textures get_buffers get_resources get_texture_info get_bound_textures find_draws_by_texture find_draws_by_shader find_draws_by_resource
---

# Resource analysis

1. Preserve the exact resource ID, subresource, byte range, format, and event scope in every inspection.
2. Use producer/consumer searches before assuming a resource is stale or uninitialized.
3. For geometry, correlate resource metadata and producer/consumer evidence; request a separately bounded raw-data workflow if bytes are essential.
4. For textures, distinguish allocation format, view format, subresource, and shader interpretation.
5. Raw texture/buffer payload reads are deliberately unavailable in this skill. This skill never mutates capture state.
