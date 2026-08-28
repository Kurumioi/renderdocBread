# ReplayLab RenderDoc skills

These are the canonical specialist Skills for ReplayLab AgentHost and Codex
app-server. The catalog intentionally contains only two broad, composable
workflows:

- `renderdoc-explain-event` owns EID, draw/dispatch, pipeline, pass-tree, and
  frame-flow analysis.
- `renderdoc-shader-workbench` owns shader identity, reconstruction, readable
  HLSL, algorithm analysis, compile/ABI validation, replay replacement,
  rollback, and project-library registration.

`allowed-tools` is a discoverable workflow inventory, not a command to invoke
every tool and not blanket mutation authority. Runtime availability is decided
from the current Host/tool manifest. Missing optional tools should narrow the
workflow or produce a precise follow-up instead of blocking unrelated analysis.
Effectful steps are constrained by target identity, preflight, validation,
postcondition, and rollback evidence. A specialist may report completion only
for stages whose evidence and acceptance criteria actually passed.
