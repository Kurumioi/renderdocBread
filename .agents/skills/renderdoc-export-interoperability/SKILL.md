---
name: renderdoc-export-interoperability
description: Define a bounded RenderDoc export plan with explicit artifacts and destinations. Actual writes are disabled until qrenderdoc issues a native nonce-bound capability ticket.
metadata:
  replaylab-category: export
  replaylab-read-only: "false"
  replaylab-min-successful-tools: "1"
  replaylab-version: "1"
  replaylab-disabled-until: native-capability-ticket
allowed-tools: save_texture export_drawcall export_to_unity
---

# Export and interoperability

1. Require an explicit artifact and destination. Never invent or broaden an output directory.
2. Resolve the exact event/resource/subresource from frozen context and show the planned files.
3. Treat every filesystem write as approval-required even when capture inspection is authorized.
4. Do not invoke export tools in v1; the runtime withholds them until native capability tickets and destination validation exist.
5. Report the blocked plan without deleting or overwriting any user file.
