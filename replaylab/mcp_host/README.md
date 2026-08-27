# ReplayLab MCP Server

**English** | [中文](README_CN.md)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for RenderDocUltra — enabling AI agents to analyze GPU captures, inspect rendering state, extract shader evidence, use host-registered external shader tools, auto-match textures, and profile performance. Decompiled source is a candidate representation, not guaranteed original HLSL/GLSL.

This directory contains ReplayLab's Python 3.10+ sidecar, not a qrenderdoc
plugin. RenderDocUltra owns the UI and lifecycle in native C++ and loads its
first-party Python bridge from the fixed application path
`integrations/replaylab/python/renderdoc_mcp_bridge`. The bridge is selected by
source/build integration, calls `start`/`stop` directly, and is never discovered
through `extension.json` or a per-user extension directory.

## Features (111 Tools)

### 📦 Capture Management
| Tool | Description |
|------|-------------|
| `load_capture` | Load a `.rdc` capture file for analysis |
| `close_capture` | Close a loaded capture |
| `list_captures` | List all loaded captures |
| `switch_capture` | Switch between multiple loaded captures |
| `launch_renderdoc` | 🆕 **Launch RenderDoc and open a .rdc file**: auto-finds qrenderdoc, starts it, waits for MCP bridge ready |
| `debug_vulkan_bindings` | 🆕 **Debug Vulkan descriptor bindings**: dump raw descriptor set data for diagnosing texture export issues |

### 📊 Frame & API Info
| Tool | Description |
|------|-------------|
| `get_api_properties` | Graphics API type, renderer, degradation status |
| `get_frame_info` | Frame number, timing, draw/dispatch/call statistics |

### 🎨 Actions (Draw Calls)
| Tool | Description |
|------|-------------|
| `get_actions` | List all draw calls, dispatches, and markers (hierarchical) |
| `set_event` | Move replay to a specific event for state inspection |

### 🗂️ Resource Inspection
| Tool | Description |
|------|-------------|
| `get_textures` | List all textures (dimensions, format, mips, array size) |
| `get_buffers` | List all buffers (size, creation flags) |
| `get_resources` | List ALL resources (textures, buffers, shaders, pipelines...) |
| `get_resource_usage` | Track how a resource is used across the frame |

### 🔧 Pipeline State
| Tool | Description |
|------|-------------|
| `get_pipeline_state` | Bound shaders, render targets, depth target, viewport |

### 💎 Shader Inspection
| Tool | Description |
|------|-------------|
| `get_shader_reflection` | Inputs, outputs, constant buffers, samplers, resources |
| `disassemble_shader` | Get shader disassembly (DXBC, SPIR-V, GLSL, etc.) |
| `get_all_disassembly_targets` | List available disassembly languages |

### 🔮 Shader Reverse-Engineering *(NEW)*
| Tool | Description |
|------|-------------|
| `reverse_shader` | **One-stop shader inspection**: RenderDoc-provided disassembly/embedded source when available, reflection, **live cbuffer values**, bound textures with auto-role detection, and samplers; it does not guarantee HLSL/GLSL decompilation |
| `get_shader_source` | Return embedded source or a high-level representation only when RenderDoc exposes one; an empty result is possible |

### 🧪 External Shader Workspace
| Tool | Description |
|------|-------------|
| `get_shader_toolchain_capabilities` | Freshly hash-check configured shader routes/validators and return the diagnostic matrix |
| `plan_shader_toolchain_route` | Detect the input and uniquely plan binary → editable text → original binary format; ambiguity fails closed |
| `plan_shader_recovery` | Classify a failure by fixed detect/decompile/compile/ABI phase and expose only hash-bound untried route pairs for AI selection |
| `run_shader_toolchain_round_trip_smoke` | Execute the fixed route, exact physical target profile, format checks, and configured validators without applying to RenderDoc |
| `get_current_shader_representation` | Persist a non-empty, hash-bound current-event explanation for any Shader API RenderDoc can reflect; explanatory/binary lanes can never compile or apply |
| `prepare_shader_workspace` | Export one event/stage, require a unique physical shader, create a workspace, and run a host-registered decompiler |
| `create_shader_workspace` | Create a workspace from an existing catalog and exact `shaderKey` |
| `decompile_shader_workspace` | Run the workspace-bound decompiler once without overwriting edited source |
| `get_shader_workspace_representation` | Return executable routed text (prefer HLSL/GLSL; select a unique direct DXBC/DXIL native-IR round trip only when that high-level lane is statically incomplete), otherwise materialize RenderDoc IR or an exact binary fallback instead of a blank editor; runtime failure does not yet switch formats, and SPIR-V assembly stays outside automatic fallback until workspace tool/validator integration |
| `promote_shader_workspace_explanation` | Explicitly promote a hash-reviewed `explanation/promoted.hlsl`; still requires compile, ABI validation, and apply authorization |
| `compile_shader_workspace` | Compile/assemble the route-bound edited text to an auditable candidate; **never applies it to RenderDoc** |
| `get_shader_workspace_status` | Validate hashes and report source/revision/candidate state |
| `begin_shader_workspace_organization` | Compile and freshly ABI-check untouched HLSL, preserve the decompiler bytes, then create fixed proposal/analysis/flowchart files that only AI may edit |
| `review_shader_workspace_organization` | Compile and freshly ABI-check the proposal; promote it only when executable `codeSha256` exactly matches the same-route baseline, otherwise restore baseline and retain provisional evidence |
| `validate_shader_workspace_candidate` | Freshly reflect an exact supported candidate (D3D11/DXBC or D3D12/DXBC|DXIL) and require a complete ABI match |
| `apply_shader_workspace_candidate` | Revalidate ABI plus live capture/event/original bytes, then apply an event-scoped D3D11 override |
| `rollback_shader_workspace_override` | Clear one event-scoped override and free its owned target shader |
| `list_shader_workspace_family_uses` | Page through trusted events using the exact captured Shader RID and ABI |
| `apply_shader_workspace_candidate_family` | Transactionally apply one shared target to events using the same captured Shader RID |
| `rollback_shader_workspace_override_family` | Transactionally clear family references and free the target only after its final reference |
| `apply_shader_workspace_shared_resource` | Default edit scope: build once and replace the original Shader RID for every captured use |
| `rollback_shader_workspace_shared_resource` | Remove/unregister the owned resource replacement and free only its generated target |
| `start_shader_workspace_watch` | Watch only the manifest-bound source (`shader.hlsl`, `shader.dxbc.asm`, or `shader.dxil.ll`); debounce changes and compile unapplied candidates |
| `poll_shader_workspace_watch` | Read bounded watcher/audit state and latest-content convergence |
| `stop_shader_workspace_watch` | Stop scheduling new compiles; an active compiler is allowed to finish safely |
| `start_shader_workspace_live_edit` | Start the stable-save → compile → fresh ABI → shared-Shader replacement loop; failure preserves the last good replacement |
| `poll_shader_workspace_live_edit` | Return bounded save/compile/ABI/apply/recovery telemetry and the last-good replacement identity |
| `stop_shader_workspace_live_edit` | Stop processing new saves and optionally roll back the replacement owned by that session |

Tool commands and paths come only from the process-owned registry documented in
[External Shader Workspace](docs/SHADER_WORKSPACE.md). Compilation and watching
remain unapplied; only an explicit or process-owner-authorized supported apply
can mutate replay after a fresh strict ABI/live-event gate. The default shared-
resource tool supports local D3D11/native DXBC and D3D12/native DXBC or DXIL,
and keeps one captured Shader RID shared across all draws. Before D3D12 commits
the replacement, it rebuilds and validates every affected graphics/compute PSO
with its captured root signature; any failure leaves the mapping unchanged.
Event/family Shader overrides remain D3D11-only. Shadow-CBuffer/material
sessions support local D3D11 and supported local D3D12 root/table CBVs.
Distinct original RIDs and foreign Editor replacements are never
silently merged or taken over. D3D11 dynamic linkage, D3D12 root constants,
ray state objects and indirect root-CBV mutation, Vulkan/OpenGL override and
remote replay remain fail-closed.
The watcher accepts no command, executable, arguments, registry, or environment.
Exact-profile extraction, fail-closed DXBC/DXIL/SPIR-V routing, trusted config
generation, validators, and round-trip smoke reports are documented in
[Shader Toolchain Routing](docs/SHADER_TOOLCHAINS.md). No route promises recovery
of the original high-level source. An optional pinned portable
`d3dcompiler_47.dll` adapter can compile existing HLSL to DXBC without an SDK or
`fxc.exe` on the destination machine; it provides no decompiler, includes, or
ABI exemption.

With no explicit editable format, automatic cross-format fallback occurs only
when the preferred high-level route is statically incomplete during planning.
If that selected route later fails during decompilation or baseline compilation,
the current recovery plan remains in the same format; it does not automatically
create a native-IR workspace. SPIR-V assembly is likewise excluded from
automatic fallback until its pinned workspace tools and validator are executed.

A CBuffer appearing in the reflection UI proves that a binding, total size, or
some display fields are visible; it does not prove complete physical member
offset/type/stride evidence. Missing fields or a reflected type that contradicts
actual DXIL loads is reported as `ambiguous`. ReplayLab then uses the strict
DXIL physical access signature (space/bind, byte ranges, load types and counts)
for ABI alignment; unknown semantic names remain provisional and AI may not
invent members. `get_active_capture_catalog_binding_status` reports the exact
hash-bound full-catalog revision without causing a subset export.

### 🗃️ Multi-capture replay projects

An idle ReplayLab/qrenderdoc launch now opens the `ReplayLab Projects` home
instead of an empty capture window. From there, or later through
`File -> ReplayLab Projects…`, you can create a project at a chosen path, add or
forget an existing portable project without deleting its files, configure its
asset root, add/remove RDC references, and open a frame from its bounded
thumbnail library. Direct `.rdc`, replay-host, target-control,
and other special startup modes are not intercepted.

Project management and capture-path checks run on a bounded background pool;
qrenderdoc remains responsive and late results are discarded after a dialog
closes or the loaded capture changes. Adding an RDC records only its reference
metadata and immediately queues its embedded thumbnail; no full-file identity
hash blocks the operation. Direct opens activate a project only for one unique
registered absolute-path match, while zero or multiple matches keep the current
project. Project creation is staged and atomically published, and a failed
selection commit is rolled back to a hidden quarantine rather than exposing a
half-created project.

| Tool | Description |
|------|-------------|
| `create_replay_project` | Create one explicit game/build/platform/API knowledge boundary |
| `get_replay_project_status` | Hash-validate and summarize the project index |
| `import_replay_project_capture` | Import a strict capture catalog and produce safe cross-frame candidates |
| `register_replay_project_workspace` | Content-address validated HLSL and optional explanatory `ai/analysis.md` |
| `register_replay_project_alias_table` | Retain verified capture-bound aliases as candidates |
| `save_replay_project_material_knowledge` | Retain explicitly selected typed values from a live session |
| `get_replay_project_reuse_plan` | Match by raw hash or code + complete interface + layout |
| `seed_shader_workspace_from_replay_project` | Copy only matched HLSL into a new unedited workspace; never compile/apply |

Projects never reuse an old workspace manifest, ABI report, replay/session
handle or apply authorization. `captureId` identifies a capture-path/API/Shader-
byte family, not every reflection-catalog byte. Each normalized full catalog is
therefore retained as an immutable project-owned `catalogSha256` revision.
Managed workspace paths use `v2/<workspaceBindingSha256>`, a canonical hash of
capture, catalog revision, route binding, stage, resource ID, and raw Shader
identity. `routeBindingSha256` separately binds the exact route catalog, tool
registry, and automatic-format policy. Workspace manifests and registered
aliases/knowledge bind the exact catalog revision; reimporting a historical
revision does not move the active head, and missing revision provenance fails
closed once a family has multiple revisions.
See [Multi-capture replay projects](docs/REPLAY_PROJECTS.md).

### 🤖 External AI Companion

Install `pip install -e ".[companion]"`, set `OPENAI_API_KEY` only in the
sidecar shell, then run `renderdoc-ai-companion gui --workspace-root <dir>`.
This standalone host currently uses an OpenAI Platform API key; a ChatGPT/Codex
subscription is available only through supported ChatGPT/Codex clients
configured to use this local MCP, not as a credential for this executable.
See the official [Codex authentication](https://learn.chatgpt.com/docs/auth)
and [local MCP/STDIO](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
documentation.
It snapshots the current RenderDoc context before each chat turn, automatically
claims/renews RenderDoc button intents, and can prepare one shared shader
workspace through the trusted automatic multi-hop route. Optional source
organization, IDE save watching, explicit `/apply-last` and `/rollback-last`,
and separately opt-in compatible-save auto-apply remain behind
compile/ABI/live gates. Optional `--project-root`/`--project` mode imports each
new capture session and may seed only a uniquely matched fresh workspace;
creating a project also requires explicit game/build IDs and never authorizes
Apply. See
[External AI Companion Host](docs/AI_COMPANION_HOST.md).

### 🎯 Auto Texture-Shader Binding *(NEW)*
| Tool | Description |
|------|-------------|
| `get_bound_textures` | **Auto-maps** shader texture slots → actual textures with **role inference** (albedo, normal, roughness, metallic, AO, emissive, etc.) |
| `get_bound_samplers` | Sampler state per shader slot (filter, address mode) |

### 📋 Constant Buffer Values *(NEW)*
| Tool | Description |
|------|-------------|
| `get_cbuffer_contents` | Read **live runtime values** of shader constant buffers (floats, ints, matrices) |

### 🎚️ Event-scoped D3D11/D3D12 Material Parameters
| Tool | Description |
|------|-------------|
| `prepare_material_session` | Snapshot one exact reflected CBuffer binding, preserving ring-buffer offset and its complete valid window |
| `list_material_sessions` | List sessions that remain valid for the active capture lifecycle |
| `get_material_session_status` | Read parameter IDs/types/values, hashes, and replay revision |
| `update_material_parameter` | Accept only a typed value for one issued parameter ID/name, rebuild the full shadow buffer, and replay the event |
| `rollback_material_session` | Clear the event-scoped shadow binding and release its snapshot |
| `clear_all_material_sessions` | Explicitly clear every shadow/snapshot in the active capture after a host crash or restart |

MCP callers cannot submit offsets, types, registers, or raw buffer bytes. See
[Event-scoped D3D11/D3D12 Material Sessions](docs/MATERIAL_SESSIONS.md) for the exact
trust boundary and current limitations.

qrenderdoc now includes a native MVP: open a Pipeline State CBuffer variable
view, right-click a supported field, and choose
`ReplayLab -> Expose as Material Parameter`. The dock shows the complete
physical identity, coalesces typed scalar/vector text edits before replaying the
exact event, and provides per-CBuffer/all-session rollback. Semantic sliders and
colour pickers are not implemented yet. D3D12 root constants, ray state
objects, indirect root-CBV mutation, and remote replay remain fail-closed.

### 📤 One-Shot DrawCall Export *(NEW)*
| Tool | Description |
|------|-------------|
| `export_drawcall` | Export **everything** about a draw call: shaders (all disassembly formats + source), all bound textures as PNG (named by role), cbuffer values, pipeline state |
| `export_to_unity` | 🆕 **Export Unity-ready assets**: mesh as .obj + .fbx (with correct coordinate system conversion, normals/UVs), texture binding info (use `save_texture` to export textures), Unity material JSON, C# editor import script |

### 🔍 Smart Identification
| Tool | Description |
|------|-------------|
| `identify_drawcalls` | 🆕 **Identify what each DrawCall renders**: per-action RT thumbnails, shader grouping, bound texture info, screen-space bounding box and coverage |
| `analyze_lighting` | 🆕 **Analyze lighting setup**: detect PBR/Blinn-Phong model, extract cbuffer light params, identify shadow maps and environment cubemaps |

### 🖼️ Texture Operations
| Tool | Description |
|------|-------------|
| `pick_pixel` | Read RGBA value at a specific pixel |
| `get_texture_minmax` | Get min/max pixel values in a texture |
| `save_texture` | Export texture to PNG/JPG/BMP/TGA/HDR/EXR/DDS |

### 📝 Buffer Data
| Tool | Description |
|------|-------------|
| `get_buffer_data` | Read raw buffer contents (hex + base64) |

### 🔍 Pixel History
| Tool | Description |
|------|-------------|
| `pixel_history` | Pixel modifications through the currently selected event (not automatically the whole frame) |

### 🐛 Shader Debugging
| Tool | Description |
|------|-------------|
| `debug_pixel` | Step-by-step pixel shader execution trace |
| `debug_vertex` | Step-by-step vertex shader execution trace |

### ⚡ Performance Counters
| Tool | Description |
|------|-------------|
| `enumerate_counters` | List available GPU performance counters |
| `fetch_counters` | Fetch values for specific counters |

### 📐 Mesh Data
| Tool | Description |
|------|-------------|
| `get_post_vs_data` | Post-vertex-shader mesh output info |

### 🚨 Debug Messages
| Tool | Description |
|------|-------------|
| `get_debug_messages` | API validation errors/warnings from the driver |

## Prerequisites

1. **RenderDocUltra v1.44** (the built-in integration declares this minimum)
2. **Python 3.10+** for the external MCP/Companion process; it does not need to
   match RenderDoc's embedded Python because communication uses file IPC
3. Optional OpenAI companion dependencies via `pip install -e ".[companion]"`

## Installation

```bash
cd replaylab/mcp_host
pip install -e .
```

## Configuration

Set the `RENDERDOC_MODULE_PATH` environment variable to the directory containing the RenderDoc Python module:

```bash
# Windows (typical RenderDoc installation)
set RENDERDOC_MODULE_PATH=C:\Program Files\RenderDoc

# Linux
export RENDERDOC_MODULE_PATH=/usr/lib/renderdoc
```

### MCP Client Configuration

Add to your MCP config (e.g. `~/.workbuddy/mcp.json`):

```json
{
  "mcpServers": {
    "replaylab": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/renderdocUltra/replaylab/mcp_host",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

Or using the installed script:

```json
{
  "mcpServers": {
    "replaylab": {
      "command": "renderdoc-mcp",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

## Usage Examples

### Basic workflow
```
1. load_capture("path/to/capture.rdc")     → Load a capture
2. get_actions()                             → See all draw calls
3. set_event(142)                            → Move to draw call #142
4. get_pipeline_state()                      → See what shaders/targets are bound
5. get_textures()                            → List all textures
6. pick_pixel(texture_id=5, x=100, y=200)   → Read pixel value
7. pixel_history(texture_id=3, x=50, y=50)  → See how a pixel was modified
8. save_texture(texture_id=5, output_path="rt0.png")  → Export texture
```

### 🔮 Shader reverse-engineering (NEW)
```
1. set_event(142)                            → Position at the draw call
2. reverse_shader(142, "fragment")           → Inspect the pixel shader:
                                                - Available source/disassembly (high-level source is not guaranteed)
                                                - All bound textures with auto-detected roles
                                                - Live constant buffer values (matrices, colors, etc.)
                                                - Full reflection data
3. get_shader_source(142, "vertex")          → Get source/high-level text if RenderDoc exposes it
4. get_bound_textures(142, "fragment")       → See: slot 0 = albedo, slot 1 = normal, slot 2 = PBR map...
5. get_cbuffer_contents(142, "fragment", 0)  → Read material parameters (color, roughness, metallic values)
```

### 📤 One-shot drawcall export (NEW)
```
1. export_drawcall(142, "C:/output/drawcall_142")
   → Creates a folder with:
      fragment_HLSL.txt                      (only when a high-level target is available)
      fragment_DXBC.txt                      (raw bytecode disassembly)
      vertex_HLSL.txt                        (only when a high-level target is available)
      fragment_slot0__MainTex_albedo.png     (diffuse texture)
      fragment_slot1__BumpMap_normal.png     (normal map)
      fragment_slot2__MetallicMap_metallic.png
      fragment_slot3__OcclusionMap_ao.png
      ... plus constant buffer values and pipeline state in the JSON result
```

### 🎯 Texture role auto-detection
The `get_bound_textures` and `reverse_shader` tools automatically infer texture roles
from shader variable names and texture formats:

| Detected Keywords | Inferred Role |
|-------------------|---------------|
| `_MainTex`, `diffuse`, `baseColor`, `albedo` | `albedo` |
| `_BumpMap`, `normalMap`, `_NRM` | `normal` |
| `_MetallicMap`, `metalness` | `metallic` |
| `_Roughness`, `smoothness` | `roughness` |
| `_OcclusionMap`, `ao` | `ambient_occlusion` |
| `_EmissionMap`, `emissive`, `glow` | `emissive` |
| BC5/RG format textures | `normal` (format heuristic) |
| BC6H/HDR format textures | `environment` (format heuristic) |

### Performance analysis
```
1. enumerate_counters()                      → See available counters
2. fetch_counters("1,2,3")                   → Get counter values per draw call
```

## Architecture

```
[AI / IDE] ←stdio/HTTP→ [ReplayLab sidecar] ←file IPC→ [qrenderdoc built-in integration]
                           Python 3.10+                     native C++ lifecycle +
                          111 MCP tools                     embedded Python bridge
                           ipc_client.py                    pyrenderdoc API
```

```
replaylab/
├── renderdoc_mcp_bridge/ # First-party embedded-Python bridge source
│   ├── integration.json  # Built-in integration metadata
│   └── __init__.py       # start/stop + typed native-intent API
└── mcp_host/
    ├── pyproject.toml          # Project config + dependencies
    ├── README.md               # English docs
    ├── README_CN.md            # Chinese docs
    └── src/
        ├── __init__.py
        ├── server.py           # MCP tool definitions (111 tools, FastMCP)
        ├── rd_wrapper.py       # RenderDoc API wrapper (local mode, backup)
        └── ipc_client.py       # File IPC client for RenderDoc bridge

Build output:
  <qrenderdoc-app>/integrations/replaylab/python/renderdoc_mcp_bridge/
```

- **`server.py`** — Registers 111 MCP tools, communicates via stdio with AI clients
- **`ipc_client.py`** — File-based IPC to bridge Python 3.10+ ↔ RenderDoc's Python 3.6
- **`rd_wrapper.py`** — Direct RenderDoc API wrapper (backup, for same-Python scenarios)
- **Built-in bridge** — Runs inside RenderDoc, captures UI context/intents, and executes bounded GPU replay calls via IPC

## Notes

- **Multi-API reads and writes**: Existing analysis paths have been tested with D3D11 ✅ and Vulkan/ANGLE ✅. D3D12/OpenGL read fallbacks are not a validation claim. Default shared Shader replacement supports local D3D11/DXBC and checked D3D12/DXBC|DXIL; event/family Shader overrides remain D3D11-only. CBuffer/material mutation supports local D3D11 and bounded local D3D12 CBV paths; other APIs, remote replay, root constants/ray state objects, and indirect root-CBV mutation fail closed.
- **Tessellation shaders**: Hull/Domain shader stages fully supported for D3D11/D3D12
- Supports **multiple simultaneous captures** — load several and switch between them
- All resource IDs are numeric integers for easy passing between tools
- Texture save supports PNG, JPG, BMP, TGA, HDR, EXR, and DDS formats
- Shader debugging returns step-by-step variable traces (limited to 50 steps for output size)
- Buffer data is returned as both hex and base64 for flexibility
- The server communicates with RenderDoc via **file IPC** — no Python version conflicts
- Shader inspection returns only representations actually available from RenderDoc. Editable HLSL requires a user-registered external decompiler; reconstructed source is not the original source.
- Texture role inference covers major game engine naming conventions
- **Model export quality** (v1.5.0): DX11 left-hand → right-hand coordinate system conversion (X-axis flip + winding reversal), `baseVertex` + `indexOffset` correct application, index remapping for precise vertex extraction, packed normal detection (auto-discard encoded tangent frames), UV flip control

## License

MIT
