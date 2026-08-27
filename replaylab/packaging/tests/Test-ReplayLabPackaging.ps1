[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

$testsRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$packagingRoot = [IO.Path]::GetFullPath((Split-Path -Parent $testsRoot))
$replayLabRoot = [IO.Path]::GetFullPath((Split-Path -Parent $packagingRoot))
$buildScript = Join-Path $packagingRoot "Build-ReplayLabRuntime.ps1"

$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile(
    $buildScript, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw ("Build script has PowerShell parser errors: " +
        (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

foreach ($entryName in @("agent_host_entry.py", "mcp_host_entry.py")) {
    $entryPath = Join-Path $packagingRoot $entryName
    Assert-True (Test-Path -LiteralPath $entryPath -PathType Leaf) "Missing entry point: $entryName"
}

$source = Get-Content -LiteralPath $buildScript -Raw
Assert-True ($source.Contains('$PythonExecutable "replaylab-ai"')) `
    "Build script must name the AgentHost executable replaylab-ai."
Assert-True ($source.Contains('$PythonExecutable "ReplayLabMCP"')) `
    "Build script must name the MCP executable ReplayLabMCP."
$legacyManifestToken = "extension" + ".json"
Assert-True (-not $source.Contains($legacyManifestToken)) `
    "Runtime packaging must not copy or generate a Python extension manifest."

$validationOutput = Join-Path $replayLabRoot "output\packaging-validation-only"
$validationWork = Join-Path $replayLabRoot "output\.packaging-validation-work"
$result = & $buildScript -ValidateOnly -OutputDirectory $validationOutput `
    -WorkDirectory $validationWork
Assert-True ($result.Status -eq "valid") "ValidateOnly did not report a valid layout."
Assert-True (-not (Test-Path -LiteralPath $validationOutput)) `
    "ValidateOnly must not create the output directory."
Assert-True (-not (Test-Path -LiteralPath $validationWork)) `
    "ValidateOnly must not create the work directory."

$unsafeRejected = $false
try {
    & $buildScript -ValidateOnly -OutputDirectory $replayLabRoot `
        -WorkDirectory $validationWork | Out-Null
}
catch {
    $unsafeRejected = $true
}
Assert-True $unsafeRejected "ValidateOnly must reject the ReplayLab source root as output."

Write-Host "ReplayLab packaging static and parameter-safety tests passed."
