[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) `
    "deploy_replaylab_integration.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0)
{
    throw "deploy_replaylab_integration.ps1 does not parse: $($parseErrors[0].Message)"
}

$source = Get-Content -LiteralPath $scriptPath -Raw
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
    if ($source -notmatch [regex]::Escape(('"' + $requiredFile + '"')))
    {
        throw "Deployment script does not fail closed on required source: $requiredFile"
    }
}
if ($source -notmatch 'Get-ChildItem[^\r\n]+-Recurse' -or
    $source -notmatch '\$segments -contains "__pycache__"' -or
    $source -notmatch 'Join-Path \$target \$sourceFile\.RelativePath' -or
    $source -notmatch 'Get-FileHash')
{
    throw "Deployment script does not recursively copy and verify safe relative paths"
}
if ($source -notmatch 'integrations/replaylab/python/renderdoc_mcp_bridge' -or
    $source -match 'extension\.json' -or
    $source -match 'qrenderdoc/extensions' -or
    $source -match '\$env:APPDATA')
{
    throw "Deployment script does not enforce the fixed built-in ReplayLab integration layout"
}

foreach ($name in @(
        "Get-ReplayLabRelativePath",
        "Assert-ReplayLabChildPath",
        "Assert-ReplayLabNoReparseComponents",
        "Get-ReplayLabIntegrationSourceFiles"))
{
    $definition = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true))
    if ($definition.Count -ne 1)
    {
        throw "Expected exactly one $name definition in the deployment script"
    }
    . ([scriptblock]::Create($definition[0].Extent.Text))
}

$testRoot = Join-Path ([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) (
    "replaylab-deploy-test-" + [guid]::NewGuid().ToString("N"))
try
{
    $sourceRoot = Join-Path $testRoot "source"
    $nestedFile = Join-Path $sourceRoot "project_tools\nested_panel.py"
    $cacheFile = Join-Path $sourceRoot "project_tools\__pycache__\nested_panel.py"
    New-Item -ItemType Directory -Force -Path `
        ([IO.Path]::GetDirectoryName($nestedFile)), `
        ([IO.Path]::GetDirectoryName($cacheFile)) | Out-Null
    [IO.File]::WriteAllText((Join-Path $sourceRoot "integration.json"), "{}")
    [IO.File]::WriteAllText((Join-Path $sourceRoot "extension.json"), "{}")
    [IO.File]::WriteAllText((Join-Path $sourceRoot "__init__.py"), "# fixture")
    [IO.File]::WriteAllText($nestedFile, "# nested fixture")
    [IO.File]::WriteAllText($cacheFile, "# cache fixture")
    [IO.File]::WriteAllText(
        (Join-Path $sourceRoot "project_tools\ignored.json"), "{}")

    $relativePaths = @(
        Get-ReplayLabIntegrationSourceFiles $sourceRoot |
            ForEach-Object { $_.RelativePath.Replace('\', '/') })
    $expected = @("__init__.py", "integration.json", "project_tools/nested_panel.py")
    if (($relativePaths -join '|') -ne ($expected -join '|'))
    {
        throw "Recursive deployment discovery returned an unsafe or incomplete set: $($relativePaths -join '|')"
    }

    $outside = Join-Path $testRoot "outside.py"
    [IO.File]::WriteAllText($outside, "# outside")
    $message = ""
    try
    {
        Assert-ReplayLabChildPath $sourceRoot $outside | Out-Null
    }
    catch
    {
        $message = $_.Exception.Message
    }
    if ($message -notmatch "outside the ReplayLab integration root")
    {
        throw "Sibling path did not fail the child-path boundary check: $message"
    }

    # Every canonical module is a fail-closed deployment prerequisite. This
    # catches a source file that exists in a developer checkout but was omitted
    # from Git and is therefore absent in a clean build checkout.
    foreach ($missingFile in $requiredFiles)
    {
        $fixtureRoot = Join-Path $testRoot (
            "missing-" + [IO.Path]::GetFileNameWithoutExtension($missingFile))
        $fixtureSource = Join-Path $fixtureRoot "renderdoc_mcp_bridge"
        New-Item -ItemType Directory -Force -Path $fixtureSource | Out-Null
        foreach ($presentFile in $requiredFiles)
        {
            if ($presentFile -ne $missingFile)
            {
                [IO.File]::WriteAllText(
                    (Join-Path $fixtureSource $presentFile), "# fixture")
            }
        }
        $missingMessage = ""
        try
        {
            & $scriptPath `
                -SourceRoot $fixtureSource `
                -TargetRoot (Join-Path $fixtureRoot "deployed") | Out-Null
        }
        catch
        {
            $missingMessage = $_.Exception.Message
        }
        if ($missingMessage -notmatch "Required ReplayLab integration source is missing" -or
            $missingMessage -notmatch [regex]::Escape($missingFile))
        {
            throw "Missing canonical source '$missingFile' did not fail closed: $missingMessage"
        }
    }
}
finally
{
    if (Test-Path -LiteralPath $testRoot)
    {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Write-Host "ReplayLab built-in integration deployment source checks passed."
