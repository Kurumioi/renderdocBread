[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InternalHost,
    [Parameter(Mandatory = $true)][string]$McpHost,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Use a fresh lifetime probe directory.' }
$probeRoot = [IO.Path]::GetFullPath($OutputDirectory)
$null = New-Item -ItemType Directory -Path $probeRoot
$savedEnvironment = @{}
foreach ($key in @('LOCALAPPDATA', 'REPLAYLAB_INSTANCE_ID', 'RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY')) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$records = @()
try {
    foreach ($entry in @(
        @{name='internal'; exe=$InternalHost; state='internal-host.json'},
        @{name='mcp'; exe=$McpHost; state='host.json'})) {
        $directory = Join-Path $probeRoot $entry.name
        $null = New-Item -ItemType Directory -Path $directory
        $runtime = Join-Path $directory 'runtime'
        [Environment]::SetEnvironmentVariable('LOCALAPPDATA', (Join-Path $directory 'local'), 'Process')
        [Environment]::SetEnvironmentVariable('REPLAYLAB_INSTANCE_ID', 'packaging-detached-lifetime', 'Process')
        [Environment]::SetEnvironmentVariable('RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY', (Join-Path $directory 'live-edit.json'), 'Process')
        $executable = (Resolve-Path -LiteralPath $entry.exe).Path
        $child = $null
        $launcher = $null
        $checksComplete = $false
        $record = [ordered]@{host=$entry.name; executable=$executable; launcherExited=$false; resourcesPresent=$false; childExited=$false; passed=$false}
        try {
            $launcher = Start-Process -FilePath $executable -ArgumentList @(
                'ensure', '--runtime-dir', ('"' + $runtime + '"'), '--owner-pid', [string]$PID
            ) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $directory 'ensure.json') -RedirectStandardError (Join-Path $directory 'ensure-stderr.log')
            # Cache the launcher's handle immediately, exactly as done for $child
            # below. Without a cached handle, a launcher that exits quickly reads
            # back ExitCode as $null, and '$null -ne 0' would misreport a
            # successful ensure as 'Frozen ensure failed.'
            $null = $launcher.Handle
            if (-not $launcher.WaitForExit(60000)) { throw 'Frozen ensure did not exit within 60 seconds.' }
            if ($null -eq $launcher.ExitCode) { throw 'Frozen ensure exit code could not be read (handle lost).' }
            $record.launcherExited = $true
            $record.launcherExitCode = $launcher.ExitCode
            if ($launcher.ExitCode -ne 0) { throw 'Frozen ensure failed.' }
            $state = Get-Content -LiteralPath (Join-Path $runtime $entry.state) -Raw | ConvertFrom-Json
            $child = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
            $null = $child.Handle
            $record.childPid = $child.Id
            # Inspect the running child's loaded Python DLL, not an archive in
            # the executable. Its actual extraction directory must survive the
            # ensure bootloader's exit, or later codec/import requests fail.
            $python = @($child.Modules | Where-Object { $_.ModuleName -match '^python[0-9]{2,}\.dll$' })
            if ($python.Count -ne 1) { throw 'Expected one loaded Python runtime in the detached Host.' }
            $bundle = Split-Path -Parent $python[0].FileName
            $record.bundlePath = $bundle
            $library = Join-Path $bundle 'base_library.zip'
            $record.resourcesPresent = Test-Path -LiteralPath $library -PathType Leaf
            if (-not $record.resourcesPresent) { throw 'Detached Host lost base_library.zip after ensure exited.' }
            # Windows may retain the already-open ZIP/DLLs while deleting an
            # extension that has not yet been imported. IDNA also needs this
            # lazy Unicode database; checking the ZIP alone misses that case.
            $record.unicodeDatabasePresent = Test-Path -LiteralPath (Join-Path $bundle 'unicodedata.pyd') -PathType Leaf
            if (-not $record.unicodeDatabasePresent) { throw 'Detached Host lost unicodedata.pyd after ensure exited.' }
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive = [IO.Compression.ZipFile]::OpenRead($library)
            try {
                if ($null -eq $archive.GetEntry('encodings/idna.pyc')) { throw 'Detached Host encoding archive lacks IDNA.' }
                $record.idnaPresent = $true
            } finally { $archive.Dispose() }
            $checksComplete = $true
        } finally {
            # This unique probe directory is the only service we own. Request
            # its normal shutdown even if the missing-resource assertion fails.
            if (-not (Test-Path -LiteralPath $runtime)) {
                $null = New-Item -ItemType Directory -Path $runtime
            }
            [IO.File]::WriteAllText((Join-Path $runtime 'shutdown.request'), 'packaging lifetime probe complete')
            if ($null -eq $child) {
                $statePath = Join-Path $runtime $entry.state
                if (Test-Path -LiteralPath $statePath) {
                    $ownedState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
                    $child = Get-Process -Id ([int]$ownedState.pid) -ErrorAction SilentlyContinue
                    if ($null -ne $child) { $null = $child.Handle }
                }
            }
            if ($null -ne $child) {
                $record.childExited = $child.WaitForExit(20000)
                if ($record.childExited) { $record.childExitCode = $child.ExitCode }
            }
            if ($null -ne $launcher -and -not $launcher.HasExited) {
                $record.launcherExited = $launcher.WaitForExit(10000)
            }
            $record.passed = $checksComplete -and $record.childExited -and $record.childExitCode -eq 0
            $records += $record
            $records | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $probeRoot 'result.json') -Encoding utf8
        }
        if (-not $record.passed) { throw 'Detached Host did not shut down normally with exit code zero.' }
    }
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
}
Write-Output 'Both frozen Hosts retained their runtime after ensure exited and shut down normally.'
