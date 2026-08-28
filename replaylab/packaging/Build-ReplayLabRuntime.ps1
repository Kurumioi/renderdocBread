[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$OutputDirectory = "",
    [string]$WorkDirectory = "",
    [switch]$IncludeLiteLLM,
    [switch]$KeepBuildWork,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$runtimeManifestName = "replaylab-runtime-manifest.json"
$runtimeManifestSchema = "replaylab-runtime-output/v1"
$buildSessionMarkerName = ".replaylab-build-session.json"
$buildSessionSchema = "replaylab-pyinstaller-session/v1"
$ownedRuntimeFiles = @(
    "replaylab-ai.exe",
    "ReplayLabInternal.exe",
    "ReplayLabMCP.exe",
    $runtimeManifestName
)

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar)
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    return (Get-NormalizedPath $Left).Equals(
        (Get-NormalizedPath $Right), [StringComparison]::OrdinalIgnoreCase)
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [switch]$AllowSame
    )
    $base = Get-NormalizedPath $BasePath
    $target = Get-NormalizedPath $TargetPath
    if ($AllowSame -and $target.Equals($base, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $base + [IO.Path]::DirectorySeparatorChar
    return $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $target = Get-NormalizedPath $TargetPath
    if (-not (Test-PathWithin $BasePath $target)) {
        throw "$Role must be a child of the ReplayLab source root: $target"
    }
    return $target
}

function Assert-NoReparseComponents {
    param(
        [Parameter(Mandatory = $true)][string]$AbsolutePath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = [IO.Path]::GetFullPath($AbsolutePath)
    $filesystemRoot = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($filesystemRoot)) {
        throw "$Role must have a filesystem root: $AbsolutePath"
    }
    $relative = $fullPath.Substring($filesystemRoot.Length)
    $parts = $relative.Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries)
    $current = $filesystemRoot
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            continue
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Role must not pass through a symlink, junction, or reparse point: $current"
        }
    }
    return $fullPath
}

function Assert-RegularFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = Assert-NoReparseComponents ([IO.Path]::GetFullPath($Path)) $Role
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item -isnot [IO.FileInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Role must be an ordinary file: $fullPath"
    }
    return $fullPath
}

function Assert-GeneratedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$ReplayLabRoot,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][string[]]$ProtectedRoots
    )
    $safePath = Assert-PathWithin $ReplayLabRoot $Candidate $Role
    Assert-NoReparseComponents $safePath $Role | Out-Null
    foreach ($protectedRoot in $ProtectedRoots) {
        if (Test-PathWithin $protectedRoot $safePath -AllowSame) {
            throw "$Role must not overlap source or packaging files: $safePath"
        }
    }
    if (Test-Path -LiteralPath $safePath) {
        $item = Get-Item -LiteralPath $safePath -Force -ErrorAction Stop
        if (-not $item.PSIsContainer) {
            throw "$Role must be a directory: $safePath"
        }
    }
    return $safePath
}

function Assert-OutputDirectorySafe {
    param(
        [Parameter(Mandatory = $true)][string]$OutputRoot,
        [Parameter(Mandatory = $true)][string[]]$OwnedFiles
    )
    if (-not (Test-Path -LiteralPath $OutputRoot)) {
        return
    }
    $children = @(Get-ChildItem -LiteralPath $OutputRoot -Force)
    if ($children.Count -eq 0) {
        return
    }

    $manifestPath = Join-Path $OutputRoot $runtimeManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Existing non-empty OutputDirectory is not a marked ReplayLab runtime output: $OutputRoot"
    }
    $manifestPath = Assert-RegularFile $manifestPath "ReplayLab runtime output marker"
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Existing ReplayLab runtime output marker is invalid: $manifestPath"
    }
    if ([string]$manifest.schema -ne $runtimeManifestSchema) {
        throw "Existing ReplayLab runtime output marker has an unsupported schema: $manifestPath"
    }

    $allowed = @{}
    foreach ($name in $OwnedFiles) {
        $allowed[$name.ToLowerInvariant()] = $true
    }
    foreach ($nameValue in @($manifest.ownedFiles)) {
        $name = [string]$nameValue
        if ([string]::IsNullOrWhiteSpace($name) -or
            $name -ne [IO.Path]::GetFileName($name) -or
            -not $allowed.ContainsKey($name.ToLowerInvariant())) {
            throw "ReplayLab runtime output marker contains an unsafe owned file: $name"
        }
    }
    foreach ($ownedName in $OwnedFiles) {
        $ownedPath = Join-Path $OutputRoot $ownedName
        if (Test-Path -LiteralPath $ownedPath) {
            Assert-RegularFile $ownedPath "ReplayLab-owned runtime file '$ownedName'" | Out-Null
        }
    }
}

function Resolve-PythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Candidate)
    $resolved = $Candidate
    if (-not [IO.Path]::IsPathRooted($resolved)) {
        $command = Get-Command $resolved -CommandType Application -ErrorAction Stop
        $resolved = $command.Source
    }
    return Assert-RegularFile ([IO.Path]::GetFullPath($resolved)) "PythonExecutable"
}

function Get-BuildProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$AgentSource,
        [Parameter(Mandatory = $true)][string]$McpSource,
        [switch]$WithLiteLLM
    )
    $probeCode = @'
import importlib.util
import json
import struct
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
required = [
    "PyInstaller", "mcp", "uvicorn", "starlette", "fastmcp", "httpx",
    "pydantic", "pydantic_ai", "yaml", "langgraph", "genai_prices", "acp",
    "src.server", "src.ipc_client",
]
if sys.argv[3] == "1":
    required.append("litellm")
missing = [name for name in required if importlib.util.find_spec(name) is None]
pyinstaller_version = ""
if "PyInstaller" not in missing:
    import PyInstaller
    pyinstaller_version = PyInstaller.__version__
from replaylab_agent import __version__ as agent_version
from src.version import SERVICE_VERSION as mcp_version
print(json.dumps({
    "python": list(sys.version_info[:3]),
    "pointerBits": struct.calcsize("P") * 8,
    "pyinstaller": pyinstaller_version,
    "agentVersion": agent_version,
    "mcpVersion": mcp_version,
    "missing": missing,
}))
'@
    $liteFlag = "0"
    if ($WithLiteLLM) {
        $liteFlag = "1"
    }
    $probeOutput = & $Python -c $probeCode $AgentSource $McpSource $liteFlag
    if ($LASTEXITCODE -ne 0) {
        throw "Python build-environment probe failed."
    }
    try {
        return (($probeOutput | Out-String) | ConvertFrom-Json)
    }
    catch {
        throw "Python build-environment probe returned invalid JSON."
    }
}

function Assert-BuildProbe {
    param(
        [Parameter(Mandatory = $true)][object]$Probe,
        [Parameter(Mandatory = $true)][string]$ReplayLabRoot,
        [Parameter(Mandatory = $true)][string]$McpRoot,
        [switch]$WithLiteLLM
    )
    $version = @($Probe.python)
    if ($version.Count -lt 2 -or [int]$version[0] -ne 3 -or [int]$version[1] -lt 10) {
        throw "ReplayLab runtime packaging requires Python 3.10 or newer."
    }
    if ([int]$Probe.pointerBits -ne 64) {
        throw "ReplayLab Windows runtime packaging requires a 64-bit Python interpreter."
    }
    $missing = @($Probe.missing)
    if ($missing.Count -gt 0) {
        $agentInstall = "${ReplayLabRoot}[all]"
        if ($WithLiteLLM) {
            $agentInstall = "${ReplayLabRoot}[all,litellm]"
        }
        $mcpInstall = "${McpRoot}[bundle]"
        throw ("Runtime build dependencies are missing: {0}. Install them with: `"{1}`" -m pip install -e `"{2}`" -e `"{3}`"" -f `
            ($missing -join ", "), $PythonExecutable, $agentInstall, $mcpInstall)
    }
    $pyInstallerMajor = [int](([string]$Probe.pyinstaller).Split('.')[0])
    if ($pyInstallerMajor -ne 6) {
        throw "ReplayLab runtime packaging requires PyInstaller 6.x; found $($Probe.pyinstaller)."
    }
    if ([string]$Probe.agentVersion -ne [string]$Probe.mcpVersion) {
        throw "AgentHost and ReplayLabMCP versions must match before packaging."
    }
}

function Invoke-PyInstallerBuild {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$EntryPoint,
        [Parameter(Mandatory = $true)][string]$ImportRoot,
        [Parameter(Mandatory = $true)][string]$DistPath,
        [Parameter(Mandatory = $true)][string]$BuildPath,
        [Parameter(Mandatory = $true)][string]$SpecPath,
        [string[]]$AdditionalImportRoots = @(),
        [string[]]$CollectAll = @(),
        [string[]]$CollectSubmodules = @(),
        [string[]]$HiddenImports = @()
    )
    $arguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--noupx",
        "--log-level", "WARN",
        "--name", $Name,
        "--paths", $ImportRoot,
        "--distpath", $DistPath,
        "--workpath", $BuildPath,
        "--specpath", $SpecPath
    )
    foreach ($root in $AdditionalImportRoots) {
        $arguments += @("--paths", $root)
    }
    foreach ($module in $CollectAll) {
        $arguments += @("--collect-all", $module)
    }
    foreach ($module in $CollectSubmodules) {
        $arguments += @("--collect-submodules", $module)
    }
    foreach ($module in $HiddenImports) {
        $arguments += @("--hidden-import", $module)
    }
    $arguments += $EntryPoint

    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed while building $Name."
    }
}

function Remove-ControlledBuildSession {
    param(
        [Parameter(Mandatory = $true)][string]$WorkRoot,
        [Parameter(Mandatory = $true)][string]$SessionRoot,
        [Parameter(Mandatory = $true)][string]$SessionId
    )
    try {
        $safeSession = Assert-PathWithin $WorkRoot $SessionRoot "PyInstaller session"
        Assert-NoReparseComponents $safeSession "PyInstaller session" | Out-Null
        $markerPath = Assert-RegularFile (Join-Path $safeSession $buildSessionMarkerName) `
            "PyInstaller session marker"
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ([string]$marker.schema -ne $buildSessionSchema -or
            [string]$marker.sessionId -ne $SessionId) {
            throw "PyInstaller session marker does not authorise cleanup: $safeSession"
        }
        $reparseItems = @(Get-ChildItem -LiteralPath $safeSession -Recurse -Force |
            Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 })
        if ($reparseItems.Count -gt 0) {
            throw "PyInstaller session contains a reparse point; preserving it for review: $safeSession"
        }
        Remove-Item -LiteralPath $safeSession -Recurse -Force
    }
    catch {
        Write-Warning "Build work was preserved because safe cleanup could not be proven: $($_.Exception.Message)"
    }
}

$packagingRoot = Get-NormalizedPath $PSScriptRoot
$replayLabRoot = Get-NormalizedPath (Split-Path -Parent $packagingRoot)
$agentSource = Join-Path $replayLabRoot "src"
$mcpRoot = Join-Path $replayLabRoot "mcp_host"
$agentEntry = Join-Path $packagingRoot "agent_host_entry.py"
$internalEntry = Join-Path $packagingRoot "internal_host_entry.py"
$mcpEntry = Join-Path $packagingRoot "mcp_host_entry.py"
$protectedRoots = @(
    $agentSource,
    $mcpRoot,
    (Join-Path $replayLabRoot "renderdoc_mcp_bridge"),
    $packagingRoot
)

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $replayLabRoot "output\replaylab"
}
if ([string]::IsNullOrWhiteSpace($WorkDirectory)) {
    $WorkDirectory = Join-Path $replayLabRoot "output\.pyinstaller-work"
}
$OutputDirectory = Assert-GeneratedDirectory $replayLabRoot $OutputDirectory `
    "OutputDirectory" $protectedRoots
$WorkDirectory = Assert-GeneratedDirectory $replayLabRoot $WorkDirectory `
    "WorkDirectory" $protectedRoots
if ((Test-PathWithin $OutputDirectory $WorkDirectory -AllowSame) -or
    (Test-PathWithin $WorkDirectory $OutputDirectory -AllowSame)) {
    throw "OutputDirectory and WorkDirectory must not overlap."
}

foreach ($sourceFile in @(
    (Join-Path $replayLabRoot "pyproject.toml"),
    (Join-Path $mcpRoot "pyproject.toml"),
    (Join-Path $agentSource "replaylab_agent\cli.py"),
    (Join-Path $mcpRoot "src\internal_host.py"),
    (Join-Path $mcpRoot "src\internal_tool_service.py"),
    (Join-Path $mcpRoot "src\replaylab_host.py"),
    $agentEntry,
    $internalEntry,
    $mcpEntry
)) {
    Assert-RegularFile $sourceFile "ReplayLab runtime packaging source" | Out-Null
}
Assert-OutputDirectorySafe $OutputDirectory $ownedRuntimeFiles

if ($ValidateOnly) {
    [pscustomobject]@{
        Status = "valid"
        OutputDirectory = $OutputDirectory
        WorkDirectory = $WorkDirectory
        AgentEntry = $agentEntry
        InternalEntry = $internalEntry
        McpEntry = $mcpEntry
    }
    return
}

if ($env:OS -ne "Windows_NT") {
    throw "ReplayLab runtime packaging currently produces Windows executables and must run on Windows."
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $replayLabRoot ".venv\Scripts\python.exe"
}
$PythonExecutable = Resolve-PythonExecutable $PythonExecutable
$probe = Get-BuildProbe $PythonExecutable $agentSource $mcpRoot -WithLiteLLM:$IncludeLiteLLM
Assert-BuildProbe $probe $replayLabRoot $mcpRoot -WithLiteLLM:$IncludeLiteLLM

$sessionId = [Guid]::NewGuid().ToString("N")
$sessionRoot = Join-Path $WorkDirectory ("session-" + $sessionId)
if (-not (Test-Path -LiteralPath $WorkDirectory)) {
    New-Item -ItemType Directory -Path $WorkDirectory | Out-Null
}
Assert-NoReparseComponents $WorkDirectory "WorkDirectory" | Out-Null
New-Item -ItemType Directory -Path $sessionRoot -Force:$false | Out-Null
Assert-NoReparseComponents $sessionRoot "PyInstaller session" | Out-Null
@{
    schema = $buildSessionSchema
    sessionId = $sessionId
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $sessionRoot $buildSessionMarkerName) `
    -Encoding UTF8

try {
    $agentDist = Join-Path $sessionRoot "agent\dist"
    $agentBuild = Join-Path $sessionRoot "agent\build"
    $agentSpec = Join-Path $sessionRoot "agent\spec"
    $mcpDist = Join-Path $sessionRoot "mcp\dist"
    $mcpBuild = Join-Path $sessionRoot "mcp\build"
    $mcpSpec = Join-Path $sessionRoot "mcp\spec"
    $internalDist = Join-Path $sessionRoot "internal\dist"
    $internalBuild = Join-Path $sessionRoot "internal\build"
    $internalSpec = Join-Path $sessionRoot "internal\spec"
    New-Item -ItemType Directory -Path `
        $agentDist, $agentBuild, $agentSpec, `
        $internalDist, $internalBuild, $internalSpec, `
        $mcpDist, $mcpBuild, $mcpSpec | Out-Null

    $agentCollectAll = @("pydantic_ai", "mcp", "genai_prices")
    if ($IncludeLiteLLM) {
        $agentCollectAll += "litellm"
    }
    Invoke-PyInstallerBuild $PythonExecutable "replaylab-ai" $agentEntry $agentSource `
        $agentDist $agentBuild $agentSpec `
        -AdditionalImportRoots @($mcpRoot) `
        -CollectAll $agentCollectAll `
        -CollectSubmodules @("replaylab_agent", "langgraph", "acp", "fastmcp", "src") `
        -HiddenImports @("replaylab_agent.mcp_proxy", "replaylab_agent.selftest")

    Invoke-PyInstallerBuild $PythonExecutable "ReplayLabMCP" $mcpEntry $mcpRoot `
        $mcpDist $mcpBuild $mcpSpec `
        -CollectAll @("mcp") `
        -CollectSubmodules @("src", "uvicorn", "starlette")

    Invoke-PyInstallerBuild $PythonExecutable "ReplayLabInternal" $internalEntry $mcpRoot `
        $internalDist $internalBuild $internalSpec `
        -CollectSubmodules @("src", "uvicorn", "starlette")

    $builtAgent = Assert-RegularFile (Join-Path $agentDist "replaylab-ai.exe") `
        "built AgentHost executable"
    $builtMcp = Assert-RegularFile (Join-Path $mcpDist "ReplayLabMCP.exe") `
        "built MCP Host executable"
    $builtInternal = Assert-RegularFile `
        (Join-Path $internalDist "ReplayLabInternal.exe") `
        "built internal Host executable"

    & $builtAgent --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its --help smoke test."
    }
    & $builtAgent -I -m replaylab_agent.mcp_proxy --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its frozen MCP proxy dispatch smoke test."
    }
    & $builtMcp --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged ReplayLabMCP.exe failed its --help smoke test."
    }
    & $builtInternal --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged ReplayLabInternal.exe failed its --help smoke test."
    }

    if (-not (Test-Path -LiteralPath $OutputDirectory)) {
        New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
    }
    Assert-NoReparseComponents $OutputDirectory "OutputDirectory" | Out-Null
    Assert-OutputDirectorySafe $OutputDirectory $ownedRuntimeFiles

    Copy-Item -LiteralPath $builtAgent `
        -Destination (Join-Path $OutputDirectory "replaylab-ai.exe") -Force
    Copy-Item -LiteralPath $builtMcp `
        -Destination (Join-Path $OutputDirectory "ReplayLabMCP.exe") -Force
    Copy-Item -LiteralPath $builtInternal `
        -Destination (Join-Path $OutputDirectory "ReplayLabInternal.exe") -Force

    $fileRecords = @()
    foreach ($name in @(
        "replaylab-ai.exe", "ReplayLabInternal.exe", "ReplayLabMCP.exe")) {
        $path = Assert-RegularFile (Join-Path $OutputDirectory $name) `
            "packaged ReplayLab runtime file '$name'"
        $file = Get-Item -LiteralPath $path
        $fileRecords += [ordered]@{
            path = $name
            size = [int64]$file.Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $manifest = [ordered]@{
        schema = $runtimeManifestSchema
        version = [string]$probe.agentVersion
        generatedUtc = [DateTime]::UtcNow.ToString("o")
        python = (@($probe.python) -join ".")
        pyinstaller = [string]$probe.pyinstaller
        ownedFiles = $ownedRuntimeFiles
        files = $fileRecords
    }
    $stagedManifest = Join-Path $sessionRoot $runtimeManifestName
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stagedManifest -Encoding UTF8
    Copy-Item -LiteralPath $stagedManifest `
        -Destination (Join-Path $OutputDirectory $runtimeManifestName) -Force

    Write-Host "ReplayLab runtime $($probe.agentVersion) created at $OutputDirectory"
}
finally {
    if ($KeepBuildWork) {
        Write-Host "PyInstaller work retained at $sessionRoot"
    }
    else {
        Remove-ControlledBuildSession $WorkDirectory $sessionRoot $sessionId
    }
}
