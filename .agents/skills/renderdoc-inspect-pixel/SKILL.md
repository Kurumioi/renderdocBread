---
name: renderdoc-inspect-pixel
description: Inspect and explain an exact picked RenderDoc texture pixel. Use for wrong color, missing object, overdraw evidence, and the Inspect Picked Pixel button while binding texture, subresource, coordinate, sample, and cutoff event.
metadata:
  replaylab-category: pixel-texture
  replaylab-read-only: "true"
  replaylab-min-successful-tools: "2"
  replaylab-required-all-tools: get_texture_info inspect_pixel_object
  replaylab-version: "1"
allowed-tools: get_texture_info get_texture_minmax inspect_pixel_object
---

# Inspect a picked pixel

1. Require the texture, subresource, coordinates, and event from the frozen UI intent. Reject an ambiguous request.
2. Confirm texture metadata before inspecting the exact frozen texture, mip, slice, sample, coordinate, and current-event cutoff.
3. Use `inspect_pixel_object`; explain confirmed writes, geometry evidence, direct writes, later overwrites, and uncertainty without inventing an engine object ID.
4. Keep resource IDs, coordinates, array slice, mip, and sample explicit in the answer.
5. Report facts, likely cause, and the smallest next check. Full pixel history and shader invocation debugging remain unavailable until their Host APIs accept all frozen identities explicitly. This skill is read-only.
