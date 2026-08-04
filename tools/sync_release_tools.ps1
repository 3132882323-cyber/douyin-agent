[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [string]$SourceTools = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
if (-not $SourceTools) { $SourceTools = $PSScriptRoot }

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$SourceTools = [IO.Path]::GetFullPath($SourceTools).TrimEnd([IO.Path]::DirectorySeparatorChar)
$rootOfInstall = [IO.Path]::GetPathRoot($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ($InstallRoot -eq $rootOfInstall) { throw "Unsafe install root: $InstallRoot" }

$target = [IO.Path]::GetFullPath((Join-Path $InstallRoot "tools"))
if ($SourceTools.Equals($target, [StringComparison]::OrdinalIgnoreCase)) {
  Write-Host "Installed maintenance tools are already current."
  exit 0
}

$required = @(
  "start_agent.ps1",
  "watchdog_release.ps1",
  "watchdog_release.vbs",
  "uninstall_release.ps1",
  "install_release.ps1",
  "sync_release_tools.ps1",
  "DianAgentUpdater.exe"
)
foreach ($name in $required) {
  if (-not (Test-Path -LiteralPath (Join-Path $SourceTools $name) -PathType Leaf)) {
    throw "Release maintenance component is missing: tools\$name"
  }
}

$suffix = [Guid]::NewGuid().ToString("N")
$stage = [IO.Path]::GetFullPath((Join-Path $InstallRoot ".tools-stage-$suffix"))
$backup = [IO.Path]::GetFullPath((Join-Path $InstallRoot ".tools-backup-$suffix"))
$installPrefix = $InstallRoot + [IO.Path]::DirectorySeparatorChar
foreach ($path in @($target, $stage, $backup)) {
  if (-not $path.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe maintenance tools path: $path"
  }
}

$targetMoved = $false
$stageActivated = $false
try {
  New-Item -ItemType Directory -Path $stage | Out-Null
  if (Test-Path -LiteralPath $target -PathType Container) {
    Get-ChildItem -LiteralPath $target -Force | ForEach-Object {
      Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
    }
  }
  foreach ($name in $required) {
    Copy-Item -LiteralPath (Join-Path $SourceTools $name) -Destination (Join-Path $stage $name) -Force
  }
  [ordered]@{
    schema_version = 1
    protocol = "offline-upgrade-v1"
    updated_at = [DateTime]::UtcNow.ToString("o")
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage "release-tools.json") -Encoding UTF8

  if (Test-Path -LiteralPath $target) {
    [IO.Directory]::Move($target, $backup)
    $targetMoved = $true
  }
  [IO.Directory]::Move($stage, $target)
  $stageActivated = $true
} catch {
  if ($stageActivated -and (Test-Path -LiteralPath $target)) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  if ($targetMoved -and (Test-Path -LiteralPath $backup)) {
    [IO.Directory]::Move($backup, $target)
  }
  if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
  }
  throw
}

if (Test-Path -LiteralPath $backup) {
  try { Remove-Item -LiteralPath $backup -Recurse -Force } catch {
    Write-Warning "New maintenance tools are active, but the previous tools backup could not be removed: $backup"
  }
}
Write-Host "Installed maintenance tools were updated transactionally." -ForegroundColor Green
