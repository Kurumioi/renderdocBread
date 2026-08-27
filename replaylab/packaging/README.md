# ReplayLab runtime packaging

`Build-ReplayLabRuntime.ps1` produces the two Python 3.10+ sidecars used by
qrenderdoc's built-in ReplayLab integration:

- `replaylab-ai.exe` — model/provider and AgentHost process
- `ReplayLabMCP.exe` — capture-analysis MCP host

The default output is `replaylab/output/replaylab/`. The qrenderdoc build owns
deployment of the embedded Python bridge, so this runtime builder does not
create an extension manifest or copy a plugin directory.

Prepare one 64-bit Python environment with both projects' build dependencies:

```powershell
$python = "D:\path\to\python.exe"
& $python -m pip install -e ".\replaylab[all]" -e ".\replaylab\mcp_host[bundle]"
./replaylab/packaging/Build-ReplayLabRuntime.ps1 -PythonExecutable $python
```

Use `-IncludeLiteLLM` only when the separate `litellm` optional dependency is
installed. Each successful output includes a versioned manifest with SHA-256
hashes. Rebuilds replace only the three marker-owned files; an unmarked,
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
executables and the build manifest already match the verified runtime output
byte-for-byte (size and SHA-256). Other files are preserved. A mismatched legacy
directory still fails closed and must be reviewed manually.

Later deployments replace only the two runtime executables, build manifest, and
deployment marker. Any unrelated entries are preserved and are never
recursively removed. The marker enters an `incomplete` state only after every
new file has been staged and verified, so an interrupted commit is explicitly
owned and retryable; it changes to `complete` after final hash verification. If
a runtime file is active or locked, deployment fails with a stop-and-retry
message and does not terminate any process. Runtime deployment remains
deliberately separate from the C++ build so rebuilding qrenderdoc cannot
overwrite a sidecar serving an active analysis session.

The fast checks do not run PyInstaller:

```powershell
./replaylab/packaging/tests/Test-ReplayLabPackaging.ps1
./replaylab/packaging/tests/Test-ReplayLabRuntimeDeployment.ps1
```
