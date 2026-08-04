param(
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $projectDir "extension\manifest.json"
$manifest = Get-Content -Raw -Encoding UTF8 $manifestPath | ConvertFrom-Json
$version = [string]$manifest.version
if (-not $version) { throw "Extension version is missing." }

$agentBuild = Join-Path $PSScriptRoot "build_agent.ps1"
$browserBuild = Join-Path $PSScriptRoot "build_browser_packages.ps1"
if ($PythonPath) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $agentBuild -PythonPath $PythonPath
} else {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $agentBuild
}
if ($LASTEXITCODE -ne 0) { throw "Standalone Agent build failed." }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $browserBuild
if ($LASTEXITCODE -ne 0) { throw "Browser package build failed." }

$releaseRoot = Join-Path $projectDir "dist\release"
$releaseDir = Join-Path $releaseRoot "DianAgent-v$version"
$zipPath = Join-Path $releaseRoot "DianAgent-v$version-windows.zip"
$releaseRootFull = [IO.Path]::GetFullPath($releaseRoot)
$releaseDirFull = [IO.Path]::GetFullPath($releaseDir)
if (-not $releaseDirFull.StartsWith($releaseRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe release directory: $releaseDirFull"
}
if (Test-Path -LiteralPath $releaseDirFull) {
  Remove-Item -LiteralPath $releaseDirFull -Recurse -Force
}
$releaseDir = $releaseDirFull
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "app") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "extension") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "extension-compatible") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "tools") | Out-Null

Copy-Item -LiteralPath (Join-Path $projectDir "dist\agent\DianAgent.exe") -Destination (Join-Path $releaseDir "app\DianAgent.exe") -Force
Copy-Item -LiteralPath (Join-Path $projectDir "dist\agent\DianAgentUpdater.exe") -Destination (Join-Path $releaseDir "tools\DianAgentUpdater.exe") -Force
Copy-Item -Path (Join-Path $projectDir "dist\dian-agent-modern\*") -Destination (Join-Path $releaseDir "extension") -Recurse -Force
Copy-Item -Path (Join-Path $projectDir "dist\dian-agent-compatible\*") -Destination (Join-Path $releaseDir "extension-compatible") -Recurse -Force

$releaseTools = @(
  "install_release.ps1",
  "uninstall_release.ps1",
  "start_agent.ps1",
  "watchdog_release.ps1",
  "watchdog_release.vbs",
  "sync_release_tools.ps1"
)
foreach ($name in $releaseTools) {
  Copy-Item -LiteralPath (Join-Path $projectDir "tools\$name") -Destination (Join-Path $releaseDir "tools\$name") -Force
}

$rootFiles = @(
  "install_dian_agent.bat",
  "upgrade_dian_agent.bat",
  "README.md",
  "BROWSER_SUPPORT.md",
  "DEPLOYMENT.md",
  "SECURITY.md",
  "LICENSE"
)
foreach ($name in $rootFiles) {
  Copy-Item -LiteralPath (Join-Path $projectDir $name) -Destination (Join-Path $releaseDir $name) -Force
}

if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zipPath.sha256" -Encoding ASCII -Value "$hash  $(Split-Path -Leaf $zipPath)"

Write-Host "Portable Windows release: $zipPath"
Write-Host "SHA-256: $hash"
Write-Host "The release contains app\DianAgent.exe, so end users do not need Python."
