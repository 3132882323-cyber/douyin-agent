$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectDir = Split-Path -Parent $PSScriptRoot
$sandboxParent = Join-Path $projectDir "dist\installer-tests"
$sandboxRoot = Join-Path $sandboxParent "DianAgent"
$installer = Join-Path $PSScriptRoot "install_release.ps1"
$uninstaller = Join-Path $PSScriptRoot "uninstall_release.ps1"

if (Test-Path -LiteralPath $sandboxRoot) {
  $fullSandbox = [IO.Path]::GetFullPath($sandboxRoot)
  $fullParent = [IO.Path]::GetFullPath($sandboxParent).TrimEnd('\') + '\'
  if (-not $fullSandbox.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe installer test sandbox: $fullSandbox"
  }
  Remove-Item -LiteralPath $sandboxRoot -Recurse -Force
}

& $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
$version = (Get-Content -LiteralPath (Join-Path $sandboxRoot "current-version.txt") -Raw).Trim()
foreach ($required in @(
  "app\$version\DianAgent.exe",
  "extension\$version\manifest.json",
  "extension-current\manifest.json",
  "tools\start_agent.ps1",
  "tools\watchdog_release.ps1",
  "tools\watchdog_release.vbs",
  "tools\uninstall_release.ps1",
  "tools\sync_release_tools.ps1",
  "tools\DianAgentUpdater.exe",
  "data", "config", "knowledge", "backup", "logs",
  ".dian-agent-install.json"
)) {
  if (-not (Test-Path -LiteralPath (Join-Path $sandboxRoot $required))) { throw "Missing installed item: $required" }
}

# Simulate an older installed launcher and prove that release media replaces
# the whole maintenance-tool set without touching user data.
$installedStarter = Join-Path $sandboxRoot "tools\start_agent.ps1"
Set-Content -LiteralPath $installedStarter -Value "# stale launcher" -Encoding ASCII
$toolsSync = Join-Path $PSScriptRoot "sync_release_tools.ps1"
$mediaTools = Join-Path $sandboxParent "release-media-tools"
if (Test-Path -LiteralPath $mediaTools) { Remove-Item -LiteralPath $mediaTools -Recurse -Force }
New-Item -ItemType Directory -Path $mediaTools | Out-Null
foreach ($name in @("start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "uninstall_release.ps1", "install_release.ps1", "sync_release_tools.ps1")) {
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $mediaTools $name)
}
Copy-Item -LiteralPath (Join-Path $projectDir "dist\agent\DianAgentUpdater.exe") -Destination (Join-Path $mediaTools "DianAgentUpdater.exe")
& $toolsSync -InstallRoot $sandboxRoot -SourceTools $mediaTools
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installedStarter).Hash -ne
    (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot "start_agent.ps1")).Hash) {
  throw "Transactional release-tools synchronization did not replace the stale launcher."
}
if (-not (Test-Path -LiteralPath (Join-Path $sandboxRoot "tools\release-tools.json") -PathType Leaf)) {
  throw "Release-tools protocol marker was not installed."
}
Remove-Item -LiteralPath $mediaTools -Recurse -Force

$sentinel = Join-Path $sandboxRoot "data\preserve-me.txt"
Set-Content -LiteralPath $sentinel -Value "test"
& $uninstaller -InstallRoot $sandboxRoot -KeepData
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) { throw "Keep-data uninstall removed user data." }
if (Test-Path -LiteralPath (Join-Path $sandboxRoot "app")) { throw "Keep-data uninstall left program files." }
if (Test-Path -LiteralPath (Join-Path $sandboxRoot "extension-current")) { throw "Keep-data uninstall left the stable extension path." }

& $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
& $uninstaller -InstallRoot $sandboxRoot -ClearData
if (Test-Path -LiteralPath $sandboxRoot) { throw "Clear-data uninstall left the install root behind." }

# A forged or stale marker must never authorize recursive deletion.
New-Item -ItemType Directory -Force -Path $sandboxRoot | Out-Null
$sentinel = Join-Path $sandboxRoot "must-survive.txt"
Set-Content -LiteralPath $sentinel -Value "test"
[ordered]@{
  product = "DianAgent"
  schema = 1
  install_root = (Join-Path $sandboxParent "different-root")
  current_version = "0.0.0"
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $sandboxRoot ".dian-agent-install.json") -Encoding UTF8
$refusedUnsafeClear = $false
try {
  & $uninstaller -InstallRoot $sandboxRoot -ClearData
} catch {
  $refusedUnsafeClear = $true
}
if (-not $refusedUnsafeClear -or -not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "Uninstaller path-marker safety check failed."
}
Remove-Item -LiteralPath $sandboxRoot -Recurse -Force

Write-Host "Release installer smoke test passed in the repository dist sandbox." -ForegroundColor Green
