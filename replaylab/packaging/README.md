# ReplayLab runtime packaging

`Build-ReplayLabRuntime.ps1` produces four Python 3.12 executables with separate
product boundaries:

- `replaylab-ai.exe` — qrenderdoc's AgentHost with deepagents model-API execution,
  vendor-native ACP/Codex subscription execution, profile/credential handling,
  bundled RenderDoc Skills, and task-event projection
- `ReplayLabInternal.exe` — qrenderdoc-owned, bearer-protected loopback service
  exposing only bounded ProjectService and project-restore HTTP operations
- `ReplayLabMCP.exe` — first-party MCP Host used through a filtered task-level
  stdio route by deepagents children and vendor-native ACP/Codex tasks, and by
  explicit external Codex/IDE/automation clients.
- `ReplayLabShaderTools.exe` — independent fixed shader adapters, reused unchanged
  when their build inputs are unchanged.

The AgentHost and MCP onefile builds both embed the audited
`src/unpacked_asset_jobs.py` source as the fixed
`replaylab_helpers/unpacked_asset_jobs.py` data helper. This is intentionally a
source data file: an independently configured Python provider must be able to
execute the Host-managed Blender continuation driver after PyInstaller extracts
either onefile runtime. This covers AgentHost-managed continuations and stdio
MCP execution. Runtime resolution is confined to the
absolute `_MEIPASS` root and the selected helper is SHA-256-bound into
continuation receipts.

The default output is `replaylab/output/replaylab/`. The qrenderdoc build owns
deployment of the embedded Python bridge, so this runtime builder does not
create an extension manifest or copy a plugin directory.

Prepare one 64-bit Python environment with both projects' build dependencies:

```powershell
$python = "D:\path\to\python.exe"
& $python -m pip install -e ".\replaylab[all]" -e ".\replaylab\mcp_host[bundle]"
./replaylab/packaging/Build-ReplayLabRuntime.ps1 -PythonExecutable $python
```

The build probe and frozen self-test require the installed DeepAgents version
to match the exact pin in `replaylab/pyproject.toml`. The AgentHost embeds that
dependency manifest, and each successful output records the verified framework
version alongside the runtime files' SHA-256 hashes.
The Windows build also starts each frozen Host through its real `ensure` command
in an isolated runtime directory. After the launcher exits, its detached service
must retain its own readable Python archive and stop normally. Long-lived frozen
children use `PYINSTALLER_RESET_ENVIRONMENT=1` so the launcher's onefile cleanup
cannot delete files needed by later imports, HTTP requests or editor saves.
Rebuilds replace only the five marker-owned files; an unmarked,
non-empty output directory is rejected and unrelated entries are never
recursively deleted.

Deploy a verified runtime into an existing qrenderdoc application layout with
the explicit deployment command:

```powershell
./replaylab/packaging/Deploy-ReplayLabRuntime.ps1 `
    -ApplicationDirectory ./x64/Development
```

The Visual Studio project exposes the same explicit step after building the runtime:

```powershell
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:DeployReplayLabRuntime `
    /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
```

CMake builds expose `deploy-replaylab-runtime`. Neither target is part of the ordinary incremental
`Build`; deployment must happen while qrenderdoc and both sidecars are stopped.

The command reads `replaylab/output/replaylab/` by default and always targets
only `<ApplicationDirectory>/replaylab/`. The application directory must contain an
ordinary qrenderdoc application executable (`qrenderdoc.exe` upstream or this
tree's `q_render_soco_doc.exe`). The source manifest schema, version, exact
owned filenames, sizes, and SHA-256 hashes are validated before any target
mutation; source and target paths may not contain reparse points.

An empty target is marked on first deployment. A non-empty target without that
deployment marker is rejected. To adopt a legacy manual deployment, use
`-AdoptExistingVerifiedRuntime`; adoption succeeds only when both canonical
sidecars, AgentHost, and the build manifest already match the verified runtime output
byte-for-byte (size and SHA-256). Other files are preserved. A mismatched legacy
directory still fails closed and must be reviewed manually.

Later deployments replace only the four runtime executables, build manifest, and
deployment marker. Any unrelated entries are preserved and are never
recursively removed. The marker enters an `incomplete` state only after every
new file has been staged and verified, so an interrupted commit is explicitly
owned and retryable; it changes to `complete` after final hash verification. If
a runtime file is active or locked, deployment fails with a stop-and-retry
message and does not terminate any process. Runtime deployment remains
deliberately separate from the C++ build so rebuilding qrenderdoc cannot
overwrite a sidecar serving an active analysis session. The private and MCP
hosts use different state files, bearer tokens, ports, protocols, and routes;
neither lifecycle command can adopt or release the other's owners.

`ReplayLabShaderTools.exe` is an independent fixed adapter runner. It contains
only the statically resolved adapter import closure, without importing the MCP
Host, server, or workspace package initialization. Its six `adapter-*` commands
preserve the existing fixed argument contracts. The builder smoke-tests every
command with `--help` before using the packaged Hosts.

The runner cache lives at `<WorkDirectory>/shader-tools-cache/`. Its key hashes
the entry point, adapter sources and transitive local imports, the independent
build implementation, Python executable/DLLs/stdlib/site startup scripts, and
the installed PyInstaller implementation, bootloader, dependencies and hook
providers. Host/Agent/report source changes do not enter that key. Cache hits
verify the complete runner's size and SHA-256 and copy exactly those bytes;
actual input changes rebuild it. Tampered or incomplete cache records fail
closed. A bounded process lock prevents concurrent cache generation, and input
hashes are checked again before committing a newly built result.

The runtime manifest includes all four executables. A deployment marker with
the exact former three-executable ownership list can add an absent shader
runner during an ordinary verified deployment. An existing unowned runner is
rejected. Compiler/adapter binaries and fixed arguments remain hash-bound by
the workspace tool registry; this split does not relax tool integrity checks.

The fast checks do not run PyInstaller:

```powershell
./replaylab/packaging/tests/Test-ReplayLabPackaging.ps1
./replaylab/packaging/tests/Test-ReplayLabRuntimeDeployment.ps1
```
