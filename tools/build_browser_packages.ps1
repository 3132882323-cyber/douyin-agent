$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $repoRoot "extension"
$distDir = Join-Path $repoRoot "dist"
$modernDir = Join-Path $distDir "dian-agent-modern"
$compatDir = Join-Path $distDir "dian-agent-compatible"

if (-not (Test-Path -LiteralPath (Join-Path $sourceDir "manifest.json"))) {
    throw "Missing extension/manifest.json"
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceDir "manifest.compat.json"))) {
    throw "Missing extension/manifest.compat.json"
}

$resolvedRepo = [IO.Path]::GetFullPath($repoRoot).TrimEnd("\") + "\"
foreach ($target in @($modernDir, $compatDir)) {
    $resolvedTarget = [IO.Path]::GetFullPath($target)
    if (-not $resolvedTarget.StartsWith($resolvedRepo, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a directory outside the repository: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolvedTarget -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceDir "*") -Destination $resolvedTarget -Recurse -Force
    Get-ChildItem -LiteralPath $resolvedTarget -Filter "test-*.js" -File |
        Remove-Item -Force
}

Remove-Item -LiteralPath (Join-Path $modernDir "manifest.compat.json") -Force
Copy-Item -LiteralPath (Join-Path $compatDir "manifest.compat.json") -Destination (Join-Path $compatDir "manifest.json") -Force
Remove-Item -LiteralPath (Join-Path $compatDir "manifest.compat.json") -Force

$modernManifest = Get-Content -LiteralPath (Join-Path $modernDir "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$compatManifest = Get-Content -LiteralPath (Join-Path $compatDir "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($modernManifest.version -ne $compatManifest.version) {
    throw "Browser package versions do not match"
}
if ($modernManifest.side_panel -or $compatManifest.side_panel) {
    throw "Browser packages must use the standalone workbench"
}
if ($modernManifest.action.default_popup -ne "popup.html" -or $compatManifest.action.default_popup -ne "popup.html") {
    throw "Browser toolbar click must open the lightweight sentinel popup"
}

Write-Host "Modern package: $modernDir"
Write-Host "Compatible package: $compatDir"
Write-Host "Version: $($modernManifest.version)"
