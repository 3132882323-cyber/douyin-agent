[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [switch]$KeepData,
  [switch]$ClearData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($KeepData -and $ClearData) { throw "Choose either -KeepData or -ClearData, not both." }
if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$markerPath = Join-Path $InstallRoot ".dian-agent-install.json"
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
  throw "Installation marker was not found. Refusing to uninstall from: $InstallRoot"
}
$marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
$markerRoot = [IO.Path]::GetFullPath([string]$marker.install_root).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ([string]$marker.product -ne "DianAgent" -or [int]$marker.schema -ne 1 -or
    -not $markerRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Installation marker does not exactly match: $InstallRoot"
}

$driveRoot = [IO.Path]::GetPathRoot($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$blocked = @(
  $driveRoot,
  [IO.Path]::GetFullPath([Environment]::GetFolderPath("UserProfile")).TrimEnd('\'),
  [IO.Path]::GetFullPath([Environment]::GetFolderPath("LocalApplicationData")).TrimEnd('\')
)
if ($blocked -contains $InstallRoot) { throw "Unsafe uninstall root: $InstallRoot" }

if (-not $KeepData -and -not $ClearData) {
  Write-Host "Uninstall Dian Agent from: $InstallRoot"
  Write-Host "[K] Keep user data (default)  [C] Clear all data  [Q] Cancel"
  $choice = (Read-Host "Choose").Trim().ToUpperInvariant()
  if ($choice -eq "Q") { Write-Host "Uninstall cancelled."; exit 0 }
  if ($choice -eq "C") {
    $confirmation = Read-Host "Type the full install path to confirm permanent deletion"
    if (-not ([IO.Path]::GetFullPath($confirmation).TrimEnd('\')).Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
      throw "The confirmation path did not exactly match. Nothing was removed."
    }
    $ClearData = $true
  } else {
    $KeepData = $true
  }
}

$ownedAppRoot = $InstallRoot.TrimEnd('\') + '\'
Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.ExecutablePath) {
    $processPath = [IO.Path]::GetFullPath([string]$_.ExecutablePath)
    if ($processPath.StartsWith($ownedAppRoot, [StringComparison]::OrdinalIgnoreCase)) {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
}

$task = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
if ($task) {
  $watchdogPath = Join-Path $InstallRoot "tools\watchdog_release.ps1"
  $watchdogLauncher = Join-Path $InstallRoot "tools\watchdog_release.vbs"
  $ownedTask = $task.Actions | Where-Object {
    $_.Arguments -and ($_.Arguments.Contains($watchdogPath) -or $_.Arguments.Contains($watchdogLauncher))
  }
  if ($ownedTask) { Unregister-ScheduledTask -TaskName "DianAgentKeepAlive" -Confirm:$false }
}

foreach ($shortcutPath in @(
  (Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"),
  (Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk")
)) {
  if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    if ($shortcut.Arguments -and $shortcut.Arguments.Contains($InstallRoot)) {
      Remove-Item -LiteralPath $shortcutPath -Force
    }
  }
}

if ($ClearData) {
  # Re-read immediately before deletion so a swapped marker cannot widen scope.
  $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $markerRoot = [IO.Path]::GetFullPath([string]$marker.install_root).TrimEnd('\')
  if ([string]$marker.product -ne "DianAgent" -or [int]$marker.schema -ne 1 -or
      -not $markerRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installation marker changed during uninstall. Refusing to clear data."
  }
  Remove-Item -LiteralPath $InstallRoot -Recurse -Force
  Write-Host "Dian Agent and all local data were removed." -ForegroundColor Green
  exit 0
}

foreach ($name in @("app", "versions", "extension", "extension-current", ".extension-current-previous", "tools")) {
  $target = Join-Path $InstallRoot $name
  if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
$versionFile = Join-Path $InstallRoot "current-version.txt"
if (Test-Path -LiteralPath $versionFile) { Remove-Item -LiteralPath $versionFile -Force }
$currentPointer = Join-Path $InstallRoot "current.json"
if (Test-Path -LiteralPath $currentPointer) { Remove-Item -LiteralPath $currentPointer -Force }
$marker.current_version = $null
$marker | Add-Member -NotePropertyName "uninstalled_at" -NotePropertyValue ([DateTime]::UtcNow.ToString("o")) -Force
$marker | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8

Write-Host "Dian Agent was removed. User data was kept at: $InstallRoot" -ForegroundColor Green
Write-Host "Preserved folders: data, config, knowledge, backup, logs"
