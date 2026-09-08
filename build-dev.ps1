#Requires -Version 5.1
<#
.SYNOPSIS
  One-command Development build for renderdocUltra (q_render_soco_doc.exe)
  + ReplayLab runtime freeze + integration deploy.
.DESCRIPTION
  Locates MSBuild via vswhere, builds Development|x64, then deploys the ReplayLab
  Python integration. Optionally runs the Qt i18n pipeline (lupdate/lrelease/lint)
  with -RunI18n. Run from a normal PowerShell prompt on Windows.

  -OptimizeReplay enables /O2 for the D3D11, D3D12, OpenGL and Vulkan driver
  projects, including their precompiled headers. The shared core keeps its usual flags.

  The Linux sandbox on this machine cannot run MSVC/Qt, so this step runs on Windows.
.EXAMPLE
  .\build-dev.ps1                 # build Development|x64 + deploy
  .\build-dev.ps1 -RunI18n        # also run the i18n update/build/lint gate first
  .\build-dev.ps1 -SkipRuntime    # skip the PyInstaller runtime freeze (faster relink)
#>
[CmdletBinding()]
param(
  [string]$Configuration = 'Development',
  [string]$Platform      = 'x64',
  [int]$MaxCpu           = 2,
  [switch]$RunI18n,
  [string]$QtBin         = 'C:\Qt\5.15.2\msvc2019_64\bin',
  [switch]$SkipDeploy,
  [switch]$SkipRuntime,
  [switch]$OptimizeReplay
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$solutionDir = (Resolve-Path $root).Path.TrimEnd('\') + '\'

function Step($m){ Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Fail($m){ Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# --- locate MSBuild via vswhere ---
Step 'Locating MSBuild'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if(-not (Test-Path $vswhere)){ Fail "vswhere not found at $vswhere - install Visual Studio 2019/2022 with the C++ workload." }
$msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
if(-not $msbuild -or -not (Test-Path $msbuild)){ Fail 'MSBuild.exe not located via vswhere.' }
Write-Host "MSBuild : $msbuild"
Write-Host "Config  : $Configuration|$Platform  (/m:$MaxCpu)"

# --- optional i18n gate (needs Qt bin: lupdate/lrelease) ---
if($RunI18n){
  Step "i18n update / build / lint  (QtBin=$QtBin)"
  if(-not (Test-Path $QtBin)){ Fail "QtBin not found: $QtBin  (pass -QtBin <Qt msvc2019_64\bin>)" }
  & "$root\util\i18n\update_translations.ps1" -QtBin $QtBin
  & "$root\util\i18n\update_translations.ps1" -QtBin $QtBin -Check
  & "$root\util\i18n\build_translations.ps1"  -QtBin $QtBin -RequireComplete
  python "$root\util\i18n\lint_ts.py" "$root\qrenderdoc\Translations\qrenderdoc_zh_CN.ts" `
    --glossary "$root\qrenderdoc\Translations\glossary.zh_CN.yaml" `
    --source-root "$root" `
    --source-baseline "$root\util\i18n\source_literals_baseline.json" `
    --require-complete
  if($LASTEXITCODE){ Fail 'lint_ts.py reported problems.' }
}

# --- build ---
Step "MSBuild Build ($Configuration|$Platform)"
$mprops = @(
  "/m:$MaxCpu",
  '/t:Build',
  "/p:Configuration=$Configuration",
  "/p:Platform=$Platform",
  "/p:SolutionDir=$solutionDir"
)
if($SkipRuntime){ $mprops += '/p:SkipReplayLabRuntime=true' }
if($OptimizeReplay){ $mprops += '/p:ReplayOptimizeDevelopment=true' }
& $msbuild 'qrenderdoc\qrenderdoc_local.vcxproj' @mprops
if($LASTEXITCODE){ Fail "MSBuild failed (exit $LASTEXITCODE)." }

# --- deploy ReplayLab integration ---
if(-not $SkipDeploy){
  Step 'Deploy ReplayLab integration'
  & "$root\util\i18n\deploy_replaylab_integration.ps1" -Configuration $Configuration
}

Step 'Done'
$exe = Join-Path $root "x64\$Configuration\q_render_soco_doc.exe"
if(Test-Path $exe){ Write-Host "Built OK -> $exe" -ForegroundColor Green }
else { Write-Host "Build finished but exe not at expected path: $exe" -ForegroundColor Yellow }
