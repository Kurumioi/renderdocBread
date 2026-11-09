#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][version]$InstalledVersion,
    [Parameter(Mandatory = $true)][version]$TargetVersion,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$Sha256
)

$ErrorActionPreference = 'Stop'
$stage = [IO.Path]::GetFullPath($PSScriptRoot)
$app = [IO.Path]::GetDirectoryName($stage)
$restartDirectory = (Get-Location).ProviderPath
$exe = Join-Path $app 'RenderDocBread.exe'
$archivePath = Join-Path $stage 'update.zip'
$payload = Join-Path $stage 'payload'
$backup = Join-Path $stage 'backup'
$log = Join-Path $stage 'update-error.txt'
$manifestName = 'RenderDocBread.manifest.json'
$zipPrefix = 'RenderDocBread/'

function Assert-RegularPath([string]$path) {
    try {
        $attributes = [IO.File]::GetAttributes($path)
    } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return
    }
    if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Redirected update path: $path"
    }
}

function Resolve-PackagePath([string]$root, [string]$relative) {
    # Block traversal, alternate streams, Windows device names, and path aliases.
    foreach ($part in $relative.Split('/')) {
        if ($part -match '(^$|[\\:\x00-\x1f*?"<>|]|[. ]$|^ |^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$))') {
            throw "Invalid package path: $relative"
        }
        $root = [IO.Path]::Combine($root, $part)
        Assert-RegularPath $root
    }
    return $root
}

function Read-Manifest([string]$path, [version]$version) {
    $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -cne 'renderdocbread-release/v1' -or
        $manifest.product -cne 'RenderDocBread' -or $manifest.platform -cne 'windows-x64' -or
        $manifest.profile -cne 'standard' -or $manifest.breadVersion -cne $version.ToString()) {
        throw "Unexpected package manifest: $path"
    }
    $files = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($record in $manifest.files) {
        if (!$record.path -or $record.path -eq $manifestName -or $record.sha256 -notmatch '^[a-f0-9]{64}$' -or
            $record.size -lt 0) {
            throw "Invalid package manifest record: $($record.path)"
        }
        $files.Add([string]$record.path, $record)
    }
    foreach ($required in @('RenderDocBread.exe', 'RenderDocBread.portable', 'renderdoc.dll')) {
        if (!$files.ContainsKey($required)) { throw "Missing package file: $required" }
    }
    return ,$files
}

function Get-StreamHash([IO.Stream]$stream) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '')
    } finally {
        $hasher.Dispose()
    }
}

function Test-PackageFile([string]$path, $record) {
    $file = [IO.FileInfo]::new($path)
    if (!$file.Exists -or $file.Length -ne $record.size) { return $false }
    $stream = $file.OpenRead()
    try {
        return (Get-StreamHash $stream) -eq $record.sha256
    } finally {
        $stream.Dispose()
    }
}

$journal = [Collections.Generic.List[object]]::new()
function Move-UpdateFile([string]$from, [string]$to) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($to))
    try {
        [IO.File]::Move($from, $to)
    } catch {
        throw "Could not move '$from' to '$to': $($_.Exception.Message)"
    }
    $journal.Add(@{ From = $from; To = $to })
}

function Expand-Update {
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $stream = [IO.File]::Open($archivePath, 'Open', 'Read', 'Read')
    try {
        if ((Get-StreamHash $stream) -ne $Sha256) {
            throw 'The downloaded ZIP does not match the GitHub release SHA-256.'
        }
        $stream.Position = 0
        $zip = [IO.Compression.ZipArchive]::new($stream, 'Read')
        try {
            [void][IO.Directory]::CreateDirectory($payload)
            $path = Join-Path $payload $manifestName
            $entry = $zip.GetEntry($zipPrefix + $manifestName)
            if (!$entry) { throw 'The release ZIP is missing the package manifest.' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $path)
            $files = Read-Manifest $path $TargetVersion
            $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            foreach ($entry in $zip.Entries) {
                if (!$entry.FullName.StartsWith($zipPrefix, [StringComparison]::Ordinal)) {
                    throw "Unexpected ZIP entry: $($entry.FullName)"
                }
                $relative = $entry.FullName.Substring($zipPrefix.Length)
                if (!$seen.Add($relative)) { throw "Duplicate ZIP entry: $relative" }
                if ($relative -ceq $manifestName) { continue }
                if (!$files.ContainsKey($relative) -or $entry.Length -ne $files[$relative].size) {
                    throw "Unlisted or invalid ZIP entry: $relative"
                }
                $destination = Resolve-PackagePath $payload $relative
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination)
                if (!(Test-PackageFile $destination $files[$relative])) {
                    throw "Extracted file failed verification: $relative"
                }
            }
            if ($seen.Count -ne $files.Count + 1) { throw 'The release ZIP is incomplete.' }
            return ,$files
        } finally {
            $zip.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

$lock = $null
$failed = $false
$rollbackFailed = $false
$validStage = $false
try {
    if ([IO.Path]::GetFileName($stage) -notmatch '^\.bread-update-[a-z0-9]+$') {
        throw 'Invalid update staging folder.'
    }
    Assert-RegularPath $stage
    Assert-RegularPath $app
    $validStage = $true
    Assert-RegularPath $exe

    $parent = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
    if ($parent -and $parent.Path -eq $exe -and !$parent.WaitForExit(60000)) {
        throw 'RenderDocBread did not exit before the update timeout.'
    }

    $lockPath = Resolve-PackagePath $app '.bread-update.lock'
    $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 1,
        [IO.FileOptions]::DeleteOnClose)
    $oldManifest = Resolve-PackagePath $app $manifestName
    $old = Read-Manifest $oldManifest $InstalledVersion
    if ($TargetVersion -le $InstalledVersion) { throw 'The update must be a newer release.' }
    $new = Expand-Update

    $appPrefix = $app + [IO.Path]::DirectorySeparatorChar
    foreach ($process in Get-Process) {
        $image = $process.Path
        if ($image -and $image.StartsWith($appPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Close other processes from this installation before updating: $image"
        }
    }

    # Only replace files that still match the installed package. Leave other files
    # untouched, including local edits to files removed from the newer release.
    $replace = [Collections.Generic.List[string]]::new()
    foreach ($relative in $new.Keys) {
        $target = Resolve-PackagePath $app $relative
        if (Test-Path -LiteralPath $target) {
            if (!$old.ContainsKey($relative)) { throw "An existing file would be overwritten: $target" }
            if (Test-PackageFile $target $new[$relative]) { continue }
            if (!(Test-PackageFile $target $old[$relative])) {
                throw "An existing file would be overwritten: $target"
            }
        }
        $replace.Add($relative)
    }
    $remove = [Collections.Generic.List[string]]::new()
    foreach ($relative in $old.Keys) {
        if ($new.ContainsKey($relative)) { continue }
        $target = Resolve-PackagePath $app $relative
        if (Test-PackageFile $target $old[$relative]) { $remove.Add($relative) }
    }

    foreach ($relative in $remove) {
        Move-UpdateFile (Join-Path $app $relative) (Join-Path $backup $relative)
    }
    # Record the new version only after the entire payload has been installed.
    $replace.Add($manifestName)
    foreach ($relative in $replace) {
        $target = Join-Path $app $relative
        if ([IO.File]::Exists($target)) { Move-UpdateFile $target (Join-Path $backup $relative) }
        Move-UpdateFile (Join-Path $payload $relative) $target
    }
} catch {
    $failed = $true
    $errors = @($_)
    for ($i = $journal.Count - 1; $i -ge 0; $i--) {
        try {
            [IO.File]::Move($journal[$i].To, $journal[$i].From)
        } catch {
            $rollbackFailed = $true
            $errors += $_
        }
    }
    if ($validStage) {
        $errors | Out-String | Set-Content -LiteralPath $log -Encoding UTF8 -ErrorAction Continue
    }
} finally {
    if ($lock) { $lock.Dispose() }
}

# Keep the backup when rollback was blocked. In every other case remove the
# transient payload and archive. The error log stays available to the user.
if (!$validStage) { exit 1 }
try {
    if (!$failed) {
        Start-Process -FilePath $exe -WorkingDirectory $restartDirectory
        # Qt preserves the read-only resource permissions when it copies this script.
        Remove-Item -LiteralPath $stage -Recurse -Force
        exit 0
    }
    if (!$rollbackFailed) {
        foreach ($directory in @($payload, $backup)) {
            if ([IO.Directory]::Exists($directory)) { Remove-Item -LiteralPath $directory -Recurse -Force }
        }
        [IO.File]::Delete($archivePath)
    }
    if (!$rollbackFailed -and !(Get-Process -Name RenderDocBread -ErrorAction SilentlyContinue |
               Where-Object { $_.Path -eq $exe })) {
        Start-Process -FilePath $exe -WorkingDirectory $restartDirectory -ArgumentList @('--updatefailed', ('"{0}"' -f $log))
    }
} catch {
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding UTF8
    $failed = $true
}
if ($failed) { exit 1 }
