[CmdletBinding()]
param(
    [ValidateSet("Development", "Release")]
    [string]$Configuration = "Development",
    [string]$SourceRoot = "",
    [string]$TargetRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ReplayLabRelativePath {
    param([string]$BasePath, [string]$TargetPath)
    # Uri.MakeRelativeUri is available in Windows PowerShell 5 and avoids the
    # PowerShell 7-only Path.GetRelativePath API.
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $targetFull = [IO.Path]::GetFullPath($TargetPath)
    $baseUri = New-Object Uri($baseFull)
    $targetUri = New-Object Uri($targetFull)
    return [Uri]::UnescapeDataString(
        $baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

function Assert-ReplayLabChildPath {
    param([string]$BasePath, [string]$TargetPath)
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $targetFull = [IO.Path]::GetFullPath($TargetPath)
    if (-not $targetFull.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase))
    {
        throw "Refusing to use a path outside the ReplayLab integration root: $targetFull"
    }
    return $targetFull
}

function Assert-ReplayLabNoReparseComponents {
    param(
        [Parameter(Mandatory = $true)][string]$AbsolutePath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = [IO.Path]::GetFullPath($AbsolutePath)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($root))
    {
        throw "$Role must have a filesystem root: $AbsolutePath"
    }
    $components = $fullPath.Substring($root.Length).Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries)
    $current = $root
    foreach ($componentName in $components)
    {
        $current = Join-Path $current $componentName
        if (-not (Test-Path -LiteralPath $current))
        {
            continue
        }
        $component = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($component.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        {
            throw "$Role must not pass through a symlink, junction, or other reparse point: $current"
        }
    }
    return $fullPath
}

function Get-ReplayLabIntegrationSourceFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safeRoot = Assert-ReplayLabNoReparseComponents $Root "ReplayLab integration source"
    $seen = @{}
    $files = @()
    foreach ($sourceFile in Get-ChildItem -LiteralPath $safeRoot -File -Recurse -Force)
    {
        $relativePath = Get-ReplayLabRelativePath $safeRoot $sourceFile.FullName
        $segments = @($relativePath.Split(
            [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
            [StringSplitOptions]::RemoveEmptyEntries))
        if ($segments -contains "__pycache__")
        {
            continue
        }
        $isPython = $sourceFile.Extension -eq ".py"
        $isRootMetadata = $relativePath -eq "integration.json"
        if (-not ($isPython -or $isRootMetadata))
        {
            continue
        }
        if ([IO.Path]::IsPathRooted($relativePath) -or
            @($segments | Where-Object { $_ -eq "." -or $_ -eq ".." }).Count -gt 0)
        {
            throw "Unsafe ReplayLab integration source path: $relativePath"
        }
        $safeSource = Assert-ReplayLabChildPath $safeRoot $sourceFile.FullName
        Assert-ReplayLabNoReparseComponents $safeSource (
            "ReplayLab integration source '$relativePath'") | Out-Null
        $key = $relativePath.ToLowerInvariant()
        if ($seen.ContainsKey($key))
        {
            throw "Duplicate ReplayLab integration source path: $relativePath"
        }
        $seen[$key] = $true
        $files += [pscustomobject]@{
            SourcePath = $safeSource
            RelativePath = $relativePath
        }
    }
    return @($files | Sort-Object RelativePath)
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
if ([string]::IsNullOrWhiteSpace($SourceRoot))
{
    $SourceRoot = Join-Path $repositoryRoot "replaylab/renderdoc_mcp_bridge"
}
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container))
{
    throw "Canonical ReplayLab built-in integration source is unavailable: $SourceRoot"
}

$requiredFiles = @(
    "__init__.py",
    "ai_context.py",
    "dxil_access_signature.py",
    "material_panel.py",
    "pass_panel.py",
    "project_launcher.py",
    "project_restore_client.py",
    "project_shader_workspace.py",
    "shader_console.py",
    "shader_editor_client.py",
    "integration.json"
)
foreach ($requiredFile in $requiredFiles)
{
    $requiredPath = Join-Path $SourceRoot $requiredFile
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf))
    {
        throw "Required ReplayLab integration source is missing: $requiredPath"
    }
}

$running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -in @("qrenderdoc", "q_render_soco_doc")
})
if ($running.Count -gt 0)
{
    throw "Close qrenderdoc before deploying its built-in ReplayLab integration. Running PID(s): $($running.Id -join ', ')"
}

$sourceFiles = @(Get-ReplayLabIntegrationSourceFiles $SourceRoot)
if ($sourceFiles.Count -lt $requiredFiles.Count)
{
    throw "ReplayLab integration source file discovery returned an incomplete set."
}

if ([string]::IsNullOrWhiteSpace($TargetRoot))
{
    $TargetRoot = Join-Path $repositoryRoot (
        "x64/{0}/integrations/replaylab/python/renderdoc_mcp_bridge" -f $Configuration)
}
$target = [System.IO.Path]::GetFullPath($TargetRoot)

New-Item -ItemType Directory -Path $target -Force | Out-Null
Assert-ReplayLabNoReparseComponents $target "ReplayLab integration deployment target" | Out-Null
foreach ($sourceFile in $sourceFiles)
{
    $deployed = Assert-ReplayLabChildPath $target (
        Join-Path $target $sourceFile.RelativePath)
    $parentDirectory = [IO.Path]::GetDirectoryName($deployed)
    if (-not $parentDirectory.Equals(
            [IO.Path]::GetFullPath($target).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase))
    {
        $parentDirectory = Assert-ReplayLabChildPath $target $parentDirectory
    }
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    Assert-ReplayLabNoReparseComponents $parentDirectory (
        "ReplayLab integration deployment parent '$($sourceFile.RelativePath)'") | Out-Null
    Copy-Item -LiteralPath $sourceFile.SourcePath -Destination $deployed -Force
}
foreach ($sourceFile in $sourceFiles)
{
    $deployed = Assert-ReplayLabChildPath $target (
        Join-Path $target $sourceFile.RelativePath)
    $sourceHash = (Get-FileHash -LiteralPath $sourceFile.SourcePath -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath $deployed -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash)
    {
        throw "ReplayLab integration deployment hash mismatch: $deployed"
    }
}
Write-Host "Deployed and verified $($sourceFiles.Count) ReplayLab built-in integration files: $target"
