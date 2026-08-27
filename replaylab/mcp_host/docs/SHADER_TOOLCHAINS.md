# Shader toolchain routing

The toolchain layer selects hash-pinned external shader operations by detected
container, exact stage, requested editable language, and target graphics API.
It does not download binaries, search `PATH`, or accept commands from MCP/AI
requests.

## Non-negotiable limit

No compiler or decompiler can guarantee recovery of the original HLSL/GLSL
from arbitrary optimized shader bytecode. Compilation normally destroys
comments, macros, source file boundaries, many names, high-level control-flow
choices, and sometimes API-specific type distinctions. A high-level result is
therefore labelled `reconstructed-source`.

A candidate is not eligible for application merely because it decompiled and
compiled. The required evidence chain is:

1. detect the exact input container and executable stage;
2. choose exactly one trusted route in each direction (ambiguity fails closed);
3. reconstruct/edit source;
4. compile back to the **original container format** using the exact target
   profile encoded in the captured executable token;
5. run configured format validators where available;
6. run RenderDoc reflection and require complete ABI equality;
7. optionally compare rendered output, while treating a visual match as extra
   evidence rather than a proof for all inputs.

The round-trip smoke report explicitly keeps
`semanticEquivalenceVerified=false` and `replayAbiValidated=false`.

## Two deliberately separate lanes

- The **explanation lane** is one-way and must always remain useful even when
  no high-level reconstruction exists. FXC `/dumpbin`, DXC `-dumpbin`,
  `spirv-dis`, or RenderDoc's native disassembly/IR provide executable
  calculations for inspection and AI annotation. A one-way `dxbc-asm` or
  `dxil-asm` route remains read-only; text alone never authorizes mutation.
- The **candidate lane** starts only from reviewable source or instruction/IR
  assembly that has a real same-format compiler/assembler. When no editable
  format is explicitly requested, fixed program routing prefers HLSL/GLSL and
  falls back to a complete native-IR round trip only when the preferred
  high-level route is statically incomplete during planning, before asking AI
  to construct missing source. If a selected decompiler fails at runtime, the
  program publishes a finite, hash-bound recovery plan. AI may select only an
  opaque candidate index; the server creates a fresh isolated workspace for
  that fixed route, preserves the failed workspace, and never accepts a tool
  path, command, argument vector, or environment from AI. The
  automatic native-IR set includes a hash-pinned 3DMigoto DXBC assembly round
  trip and the
  official DXC `IDxcAssembler` DXIL/LLVM-text-to-container path. It still needs
  format validation plus fresh, API-specific RenderDoc reflection/ABI and live
  identity gates. No route record and no AI response can authorize apply by
  itself. SPIR-V assembly remains outside automatic fallback until the workspace
  path executes its pinned tools and validator; it may only be requested
  explicitly meanwhile.

This means “all recognised formats are explainable” is a realistic goal;
“every explanatory listing is safely editable/applicable” is not.

The default resource-wide replay replacement supports local D3D11/native DXBC
and D3D12/native DXBC or DXIL. D3D11 still rejects dynamic linkage. D3D12 must
use the checked transaction that rebuilds every affected graphics/compute PSO
with its captured root signature before committing the mapping; any failed PSO
aborts the replacement. Event/family Shader overrides remain D3D11-only.
The separate material-session path supports only existing local D3D12
CBV-backed Root/descriptor-table bindings; it does not authorize root-signature
changes, root constants, ray state objects or indirect Root-CBV mutation.
Vulkan descriptor/push-constant/specialization contracts, OpenGL
program-link contracts, ray-library exports/shader-table references and remote
replay remain unsupported; format recognition alone never widens an apply gate.

## Managed startup catalog stability

ReplayLab workspaces pin the exact bytes of both managed config files. Host
startup therefore stages a newly discovered registry/catalog pair before it
decides whether to replace the active pair. Launching the same packaged tools
from another portable or installed directory does not by itself rotate those
bytes: the Host loads both pairs through the normal strict validators, freshly
hashes every executable and fixed file, and compares a path-independent
operation identity. It preserves the old managed pair only when all executable
and dependency SHA-256 values and filenames, side-by-side directory
relationships, argument templates, placeholders, timeouts, operation sets,
routes, validators and capability metadata are identical. The generated status
uses `source=existing-managed-config-relocated-copy` and retains the old
`managedConfig` hashes, so an already authorised workspace keeps its exact
catalog binding.

This is deliberately not a general toolchain-rebinding shortcut. Every file
named by the old registry must still exist as a regular non-symlink file and
pass its original hash. If an old file is missing, any byte/name/layout/command
field changed, a route/provider was added or removed, or comparison is
ambiguous, startup performs the normal managed `startup-refresh`. Existing
workspaces then fail closed against the changed catalog. Use
`migrate_shader_workspace_toolchain` to create a revision-zero successor and,
for durable auto-apply consent, explicitly move the authorization with
`migrate_shader_workspace_live_edit_authorization`. ReplayLab never silently
rewrites a workspace manifest or treats same tool bytes as Shader semantic
equivalence.

## Current route matrix

This matrix describes routes the local provider graph can generate when their
tools are explicitly packaged or registered. A particular ReplayLab bundle may
contain only a subset. The generated `shader-toolchain-status.json` is the
authoritative inventory for that installation; an absent binary means the
corresponding route is unavailable, not downloaded on demand.

| Input | Representation/edit path | Compile-back path | Status and boundary |
| --- | --- | --- | --- |
| DXBC | FXC `/dumpbin` → DXBC assembly | none | Official, non-empty explanation lane; Microsoft supplies no general SM4/5 DXBC assembler |
| DXBC | 3DMigoto 1.3.16 `-d -V` → Flugan DXBC assembly | 3DMigoto `-a --copy-reflection <exact captured baseline>` → DXBC | Native instruction-edit fallback when the preferred HLSL lane is incomplete, or an explicit lane; adapter verifies the pinned tool and baseline hashes, refuses derived-output overwrite, and keeps replay ABI/semantic-equivalence gates separate |
| DXBC | HLSLDecompiler → HLSL | pinned `D3DCompiler_47` adapter (preferred) or FXC → DXBC | Community SM4/SM5 reconstruction; exact `SHDR/SHEX` profile; fresh D3D11 or checked D3D12 shared-resource ABI/apply gate still required |
| DXBC | compatible dxil-spirv build with upstream dxbc-spirv → SPIR-V → SPIRV-Cross HLSL | pinned `D3DCompiler_47` adapter (preferred) or FXC → DXBC | Optional community fallback; file name/hash registration is not feature proof, so validated execution and the untouched reverse-compile probe must succeed; DirectX→Vulkan descriptor semantics can change |
| DXIL | locally source-built RenoDX `decomp.exe` → HLSL | DXC → DXIL | Preferred SM6 reconstructed-source route when packaged. The adapter binds the exact captured entry/signature and rejects unresolved row packing. Names and source structure are not original; untouched compilation, full ABI, exact DXIL CBuffer access, and replay gates remain mandatory |
| DXIL | DXC `-dumpbin` → DXIL/LLVM text | official DXC `IDxcAssembler::AssembleToContainer` → DXIL | Automatic native-IR fallback when no DXIL→HLSL round trip exists and the assembler adapter is locally packaged; it does not recover HLSL, and every candidate still requires exact profile/container checks plus fresh RenderDoc ABI and driver acceptance |
| DXIL | dxil-spirv → SPIR-V → SPIRV-Cross HLSL | DXC → DXIL | Community translation fallback, not an algorithm-equivalence guarantee. Descriptor/resource/indexing semantics may change across the DirectX→Vulkan→HLSL bridge; exact `DxilProgramHeader.ProgramVersion`, untouched compilation, full ABI/access checks, and checked D3D12 all-affected-PSO rebuild are required |
| SPIR-V | SPIRV-Cross → GLSL | glslang → SPIR-V | Reconstructed Vulkan GLSL; exact entry/stage; explicit trusted Vulkan target environment required |
| SPIR-V | SPIRV-Cross → HLSL | DXC → SPIR-V | Listed diagnostically, but automatic round-trip fails closed because an exact HLSL target profile cannot be physically derived from SPIR-V |
| SPIR-V | `spirv-dis` → SPIR-V assembly | `spirv-as` → SPIR-V | IR editing rather than high-level recovery; assembly is not validation |
| OpenGLSPIRV | SPIRV-Cross without Vulkan semantics → GLSL | glslang `-G --target-env opengl` → OpenGLSPIRV | Capture encoding is required; binary magic alone cannot distinguish it from Vulkan SPIR-V |
| OpenGLSPIRV | `spirv-dis` → OpenGLSPIRVAsm | `spirv-as` → OpenGLSPIRV | Physically shared syntax, logically separate route; validate with `spirv-val --target-env opengl` |
| HLSL | source | pinned `D3DCompiler_47` adapter/FXC for DXBC; DXC for DXIL | Native compile, but interface/ABI still depends on the fixed compiler policy and exact target profile |
| GLSL | source | glslang | Separate `-V` Vulkan and `-G` OpenGL SPIR-V candidates; never cross the environments implicitly |

DXC provides DXIL disassembly (`IDxcCompiler3::Disassemble` / `dxc -dumpbin`),
not a general DXIL-to-HLSL decompiler. ReplayLab can separately register the
community RenoDX SM6 decompiler and the `dxil-spirv -> SPIRV-Cross` bridge, but
keeps both labelled reconstructed/best-effort and independently gated. Mesa NIR is an internal optimizing IR
used inside Mesa compiler stacks, not a portable original-source recovery CLI,
so it is not advertised as an HLSL decompiler route.

## Exact compiler profile

The workspace and round-trip runner never substitute a fixed `ps_5_0`,
`vs_5_0`, or stage-based guess:

- DXBC reads `VersionToken` only from a complete tokenized program header in
  the single `SHDR`/`SHEX` executable chunk. The chunk must contain both the
  version and `Length` DWORDs, be DWORD-aligned, and have
  `Length * 4 == chunk size`; a plausible first DWORD in truncated or
  contradictory bytes is rejected. SM 4.0, 4.1, 5.0, and 5.1 are supported.
  A container with an `Aon9` part fails closed: its ordinary-looking `*_4_0`
  token does not unambiguously recover the original
  `*_4_0_level_9_x` compiler target.
- DXIL reads `DxilProgramHeader.ProgramVersion` only after validating the full
  `DxilProgramHeader` and `DxilBitcodeHeader`: the part is at least 24 bytes,
  `SizeInUint32` exactly equals the part size, the inner magic is `DXIL`, the
  non-empty bitcode range starts after its header and stays in bounds, and its
  padded end agrees with both other size declarations. Truncated, overlapping,
  overflow-like, extra whole-DWORD trailing data, and contradictory headers
  fail closed.
- Stage and profile are cross-checked against the capture stage.
- DXIL library/ray kinds require multi-entry/export handling and currently fail
  closed instead of being miscompiled as one entry point.

The exact value is stored in `manifest.json.targetProfile`, re-derived whenever
the workspace is loaded, and supplied to a compiler only through the read-only
`{target_profile}` placeholder. A caller cannot override a DirectX token with a
different profile, and a non-DirectX input cannot manufacture one from its
stage name.

The portable `D3DCompiler_47` route is compile-only. Its hash-pinned adapter
loads one explicitly registered absolute `d3dcompiler_47.dll` path with
`D3DCompile`, passes only `{input}`, `{output}`, `{entry_point}` and
`{target_profile}`, and fixes both compiler flag words to zero. It supplies no
macro table or include handler, so `#include` is intentionally unsupported and
fails closed. If FXC is also registered, the portable route has the unique
better priority; callers never choose between equal routes. This adapter does
not reconstruct HLSL and does not make FXC/D3DCompiler disassembly editable.
Every output still has to pass container/profile checks and the full replay ABI
gate before it can be applied.

SPIRV-Cross is never invoked directly by a generated registry. Its bundled,
hash-pinned adapter receives the workspace's physical entry point, stage,
target profile, and capture API. It always supplies `--entry` and `--stage`;
for HLSL it derives `--shader-model` from the exact profile (for example,
`ps_6_7` becomes `67`) and explicitly preserves the selected entry name.
Missing facts, a profile/stage contradiction, a non-DirectX HLSL target, or an
unrepresentable profile fail before SPIRV-Cross starts. This prevents its
upstream defaults (first module entry point and HLSL Shader Model 3.0) from
silently selecting the wrong program.

## Explicit SPIR-V environment

The SPIR-V binary header identifies a SPIR-V version, but it does not prove the
consumer API environment or Vulkan version/extension contract. A generic
capture API label such as `Vulkan` is also insufficient. Generated configs
therefore have no inferred/default validation environment:

- `--spirv-target-env` is mandatory when `glslang` or `spirv-val` is supplied;
- accepted generated Vulkan environments are currently `vulkan1.0`,
  `vulkan1.1`, `vulkan1.2`, and `vulkan1.3`;
- the exact literal is bound into glslang, DXC SPIR-V, and `spirv-val`
  operations (`spirv-val --target-env ...`), so the validator can never fall
  back to its generic Universal default;
- if the capture/tool owner cannot establish the intended environment, the
  SPIR-V compile/validation route must not be configured. No version is guessed
  from the module header.

This environment choice is validation context, not semantic-equivalence or
RenderDoc replay-ABI evidence.

RenderDoc also exposes distinct `ShaderEncoding::SPIRV` and
`ShaderEncoding::OpenGLSPIRV` values. The word stream itself is physically
indistinguishable, so an OpenGL route is generated only with the explicit
`--enable-opengl-spirv` operator choice and is selected only when capture
metadata says `GraphicsAPI.OpenGL` plus `OpenGLSPIRV`. The corresponding fixed
commands use SPIRV-Cross without `--vulkan-semantics`, glslang `-G
--target-env opengl`, and `spirv-val --target-env opengl`.

## Generate trusted local configs

Supply absolute paths to binaries installed or built by the local operator:

```powershell
python -m src.shader_workspace.toolchain_config `
  --tools-output D:\RenderDocUltra\shader-tools.v1.json `
  --routes-output D:\RenderDocUltra\shader-toolchains.v1.json `
  --d3dcompiler 'C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll' `
  --fxc 'C:\Program Files (x86)\Windows Kits\10\bin\...\x64\fxc.exe' `
  --dxc D:\VulkanSDK\Bin\dxc.exe `
  --renodx-decompiler D:\ShaderTools\decomp.exe `
  --spirv-cross D:\VulkanSDK\Bin\spirv-cross.exe `
  --glslang D:\VulkanSDK\Bin\glslangValidator.exe `
  --spirv-dis D:\VulkanSDK\Bin\spirv-dis.exe `
  --spirv-as D:\VulkanSDK\Bin\spirv-as.exe `
  --spirv-val D:\VulkanSDK\Bin\spirv-val.exe `
  --spirv-target-env vulkan1.1 `
  --enable-opengl-spirv `
  --hlsl-decompiler D:\ShaderTools\CMD_Decompiler.exe `
  --dxil-spirv D:\ShaderTools\dxil-spirv.exe
```

The generator:

- performs no network access or binary discovery;
- hashes every executable;
- pins `d3dcompiler_47.dll` together with the fixed ctypes adapter host/script;
  this compile-only route needs neither `fxc.exe` nor a Windows SDK installation
  on the destination machine when the DLL is lawfully packaged;
- registers official FXC/DXC dump operations as explanation-only routes;
- automatically pins sibling `dxcompiler.dll` and `dxil.dll` when present;
- pins a locally supplied RenoDX `decomp.exe` together with its side-by-side
  `dxcompiler.dll` and the fixed entry/signature adapter;
- pins the HLSLDecompiler adapter and `CMD_Decompiler.exe` together;
- pins the SPIRV-Cross adapter and executable together; callers supply only
  physical workspace facts, never free-form SPIRV-Cross arguments;
- accepts repeated `--pin PROVIDER=ABSOLUTE_PATH` for additional DLL/data
  dependencies;
- binds every route to the exact operation digest and the route catalog to the
  exact tool-registry file hash;
- refuses to overwrite configs unless `--force` is explicit.

Dynamic dependencies loaded from system directories cannot all be proven by
this registry. Pin local companion DLLs and install tools only from a trusted
source.

Start the MCP server with both process-owner-controlled paths. MCP callers
cannot override either path:

```powershell
$env:RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG = 'D:\RenderDocUltra\shader-tools.v1.json'
$env:RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG = 'D:\RenderDocUltra\shader-toolchains.v1.json'
python -m src.server
```

## Deterministic API

```python
from src.shader_workspace import ToolchainCatalog

catalog = ToolchainCatalog(tool_config, route_config)

selection = catalog.select_round_trip_for_file(
    captured_shader,
    stage="fragment",
    target_api="d3d11",
)

matrix = catalog.diagnostic_matrix("fragment")

report = catalog.round_trip_smoke(
    captured_shader,
    editable_format=selection["editableFormat"],
    output_directory=smoke_directory,
    stage="fragment",
    entry_point="main",
    target_api="d3d11",
)
```

`select_round_trip_for_file` returns `why`, full capabilities, source URLs,
licenses, caveats, exact target profile, and both route/tool ID sequences. If
two equally ranked route combinations exist, it rejects auto-selection. An
advanced caller can inspect `matching_routes` and choose an explicit route ID.

The same operations are exposed through three bounded MCP tools:

- `get_shader_toolchain_capabilities(stage)` only re-hashes and reports the
  matrix;
- `plan_shader_toolchain_route(shader_path, stage, ...)` detects and selects a
  unique complete round trip without running tools;
- `run_shader_toolchain_round_trip_smoke(shader_path, output_directory, stage,
  editable_format, ...)` executes only the configured, digest-bound route. A
  route using the SPIRV-Cross adapter additionally requires the capture's
  target API; it has no executable, command, argument-list, config-path, or
  target-profile parameter.

## Primary sources

- [RenderDoc `ShaderEncoding`](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/api/replay/replay_enums.h)
  defines distinct Vulkan `SPIRV`/`SPIRVAsm` and
  `OpenGLSPIRV`/`OpenGLSPIRVAsm` encodings and separate known-tool mappings.
- [Microsoft DirectXShaderCompiler](https://github.com/microsoft/DirectXShaderCompiler)
  documents HLSL→DXIL/SPIR-V compilation and the compiler/disassembler/
  validator APIs.
- [DXC `DxilContainer.h`](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/DxilContainer/DxilContainer.h)
  defines the DXIL program-version fields used for exact profile extraction.
- [Microsoft FXC documentation](https://learn.microsoft.com/windows/win32/direct3dtools/dx-graphics-tools-fxc)
  documents `/T`, `/E`, `/Fo`, `/Fc`, and `/dumpbin` for legacy HLSL
  compilation/disassembly.
- [Microsoft `D3DCompile`](https://learn.microsoft.com/windows/win32/api/d3dcompiler/nf-d3dcompiler-d3dcompile)
  defines the fixed in-process HLSL-to-bytecode call used by the portable
  adapter. ReplayLab passes null macro/include pointers and fixed zero flags;
  redistribution rights still depend on the DLL package actually supplied by
  the bundle builder.
- [Khronos SPIRV-Cross](https://github.com/KhronosGroup/SPIRV-Cross) describes
  SPIR-V reconstruction to HLSL/GLSL and explicitly notes incomplete obscure
  feature coverage. Its [`main.cpp`](https://github.com/KhronosGroup/SPIRV-Cross/blob/main/main.cpp)
  documents that entry selection otherwise defaults to the first module entry,
  implements `--entry`/`--stage`, and enables the emitted HLSL entry name after
  an explicit rename. Its
  [`spirv_hlsl.hpp`](https://github.com/KhronosGroup/SPIRV-Cross/blob/main/spirv_hlsl.hpp)
  options default to Shader Model 3.0 unless `--shader-model` is supplied.
- [Khronos glslang](https://github.com/KhronosGroup/glslang) is the reference
  GLSL front end/SPIR-V generator. Its HLSL front end is deprecated as of April
  2026, so generated configs do not use it for HLSL.
- [Khronos SPIRV-Tools](https://github.com/KhronosGroup/SPIRV-Tools) provides
  `spirv-dis`, `spirv-as`, and `spirv-val`; upstream states validator coverage
  is incomplete/one-sided. The upstream
  [`spirv-val` CLI source](https://github.com/KhronosGroup/SPIRV-Tools/blob/main/tools/val/val.cpp)
  defines `--target-env`; generated configs always supply it explicitly.
- [3DMigoto 1.3.16 `cmd_Decompiler` source](https://github.com/bo3b/3Dmigoto/blob/cbc4993/HLSLDecompiler/cmd_Decompiler/cmd_Decompiler.cpp)
  defines `.hlsl` for `-D`, `.asm` for `-d`, and `.shdr` for `-a`. The HLSL
  adapter uses `-D -V --stop-on-failure`. The assembly adapter uses
  `-d -V --stop-on-failure`, whose validation reassembles the untouched listing
  and compares DXBC sections, then uses `-a --copy-reflection` with a
  job-local snapshot whose SHA-256 equals the captured baseline. In 1.3.16 the
  post-`-a` validation branch is still a TODO, so the adapter deliberately does
  not claim `-V` validates edited assembly. Successful assembly is an
  executable candidate, not HLSL recovery or semantic-equivalence proof. The
  3DMigoto source is GPL-3.0; verify the license/provenance of the supplied
  binary.
- [dxil-spirv](https://github.com/HansKristian-Work/dxil-spirv) is an MIT
  community DirectX→SPIR-V translation project used by D3D translation stacks.
  Its [`dxil_spirv.cpp` CLI source](https://github.com/HansKristian-Work/dxil-spirv/blob/master/dxil_spirv.cpp)
  defines the configured input, `--output`, `--validate`, and `--entry`
  arguments. Current upstream builds integrate `dxbc-spirv`; the same upstream
  executable's
  [`dxil_spirv_c.cpp` parser](https://github.com/HansKristian-Work/dxil-spirv/blob/master/dxil_spirv_c.cpp)
  dispatches legacy DXBC blobs through `parseDXBCBinary`. This justifies a
  conditional DXBC→SPIR-V route for compatible builds; it does not prove that
  an arbitrary local executable with that name contains the integration. The
  generated registry pins its exact hash, execution uses validation, output is
  independently detected as SPIR-V, and editable HLSL is withheld unless the
  untouched reverse compile succeeds. None of those checks establish source or
  semantic equivalence, and this is not a Microsoft-supported recovery path.
- [Mesa NIR documentation](https://docs.mesa3d.org/nir/index.html) describes NIR
  as Mesa's internal optimizing compiler IR.
