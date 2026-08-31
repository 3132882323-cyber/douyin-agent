[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [switch]$KeepData,
  [switch]$ClearData,
  [ValidateRange(1, 300)][int]$MaintenanceLockTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($KeepData -and $ClearData) { throw "Choose either -KeepData or -ClearData, not both." }
if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
if (Test-Path -LiteralPath $InstallRoot) {
  if ((Get-Item -LiteralPath $InstallRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Install root must not be a symbolic link or junction: $InstallRoot"
  }
}
$trustPolicyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $trustPolicyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $trustPolicyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "The Windows maintenance policy component is missing or unsafe."
}
. $trustPolicyPath
Assert-DianPathChainNoReparsePoints $InstallRoot "Installation root"
$markerPath = Join-Path $InstallRoot ".dian-agent-install.json"
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
  throw "Installation marker was not found. Refusing to uninstall from: $InstallRoot"
}
if ((Get-Item -LiteralPath $markerPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
  throw "Installation marker must not be a symbolic link or reparse point."
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
function Test-OwnedKeepAliveTask([object]$Task, [string]$Root) {
  return Test-DianOwnedKeepAliveTask $Task $Root
}

function Test-OwnedShortcut([object]$Shortcut, [string]$ShortcutPath, [string]$Root) {
  return Test-DianOwnedShortcut $Shortcut $ShortcutPath $Root
}

function Read-ValidatedInstallMarker([string]$Root, [string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Installation marker was not found. Refusing to uninstall from: $Root"
  }
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Installation marker must be a regular file, not a symbolic link or reparse point."
  }
  try {
    $record = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    $recordRoot = [IO.Path]::GetFullPath([string]$record.install_root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $schema = [int]$record.schema
  } catch {
    throw "Installation marker is unreadable or invalid. Refusing to uninstall from: $Root"
  }
  if ([string]$record.product -ne "DianAgent" -or $schema -ne 1 -or
      -not $recordRoot.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installation marker does not exactly match: $Root"
  }
  return $record
}

function Assert-SafeAtomicWriteDebris([string]$Path, [string]$Label) {
  $full = [IO.Path]::GetFullPath($Path)
  Assert-DianPathChainNoReparsePoints $full $Label
  $parent = Split-Path -Parent $full
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) { return }
  $leaf = Split-Path -Leaf $full
  $pattern = '^\.' + [regex]::Escape($leaf) + '\.(?:write|replace)-[a-f0-9]{32}$'
  foreach ($item in @(Get-ChildItem -LiteralPath $parent -Force -ErrorAction Stop | Where-Object { $_.Name -match $pattern })) {
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "$Label has unsafe atomic-write debris: $($item.FullName)"
    }
  }
}

function New-UninstallPreflight([string]$Root, [bool]$ClearAll) {
  # Build and validate the complete mutation plan before stopping the Agent or
  # deleting the first task, shortcut, pointer or program directory.
  Assert-DianPathChainNoReparsePoints $Root "Installation root"
  $validatedMarkerPath = Join-Path $Root ".dian-agent-install.json"
  $validatedMarker = Read-ValidatedInstallMarker $Root $validatedMarkerPath
  Assert-SafeAtomicWriteDebris $validatedMarkerPath "Installation marker"
  $programDirectories = New-Object 'Collections.Generic.List[string]'
  foreach ($name in @("app", "versions", "extension", "extension-current", ".extension-current-previous", "tools", "bootstrap")) {
    $target = [IO.Path]::GetFullPath((Join-Path $Root $name))
    if (-not (Test-Path -LiteralPath $target)) { continue }
    $item = Get-Item -LiteralPath $target -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Managed program directory is unsafe: $target"
    }
    Assert-DianNoReparsePoints $target "Managed program directory"
    $programDirectories.Add($target)
  }

  $debrisDirectories = New-Object 'Collections.Generic.List[string]'
  foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop | Where-Object {
    $_.Name -match '^\.(?:extension-current|tools)-(?:stage|backup)-[0-9a-f]{32}$' -or
    $_.Name -match '^\.release-tools-(?:stage|backup)-[0-9a-f]{32}$' -or
    $_.Name -match '^\.extension-backup-[0-9]+(?:\.[0-9]+){2}-[0-9]{8}-[0-9]{6}$'
  })) {
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Managed program debris is unsafe: $($item.FullName)"
    }
    Assert-DianNoReparsePoints $item.FullName "Managed program debris"
    $debrisDirectories.Add([IO.Path]::GetFullPath($item.FullName))
  }

  $pointerFiles = New-Object 'Collections.Generic.List[string]'
  foreach ($name in @("current-version.txt", "current.json")) {
    $path = [IO.Path]::GetFullPath((Join-Path $Root $name))
    Assert-SafeAtomicWriteDebris $path "Managed version pointer"
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Managed version pointer is unsafe: $path"
    }
    $pointerFiles.Add($path)
  }
  foreach ($name in @(".install-transaction.json", ".release-tools-transaction.json")) {
    $path = Join-Path $Root $name
    Assert-SafeAtomicWriteDebris $path "Maintenance transaction record"
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Maintenance transaction record is unsafe: $path"
    }
  }
  $startupStatePath = Join-Path $Root "data\runtime\startup-state.json"
  Assert-SafeAtomicWriteDebris $startupStatePath "Startup state"
  if (Test-Path -LiteralPath $startupStatePath) {
    $item = Get-Item -LiteralPath $startupStatePath -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Startup state is unsafe: $startupStatePath"
    }
  }

  $shortcutFiles = New-Object 'Collections.Generic.List[string]'
  $shell = New-Object -ComObject WScript.Shell
  foreach ($path in @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk")
  )) {
    Assert-SafeAtomicWriteDebris $path "Related shortcut"
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "A related shortcut is unsafe: $path"
    }
    $shortcut = $shell.CreateShortcut($path)
    if (Test-OwnedShortcut $shortcut $path $Root) {
      $shortcutFiles.Add([IO.Path]::GetFullPath($path))
    }
  }

  $task = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
  $removeTask = [bool]($task -and (Test-OwnedKeepAliveTask $task $Root))
  if ($ClearAll) {
    # Clear-data removal owns every descendant, so every descendant must pass
    # the same reparse-point check before external startup state is changed.
    Assert-DianNoReparsePoints $Root "Installation root"
  }
  return [pscustomobject]@{
    Marker = $validatedMarker
    ProgramDirectories = @($programDirectories)
    DebrisDirectories = @($debrisDirectories)
    PointerFiles = @($pointerFiles)
    ShortcutFiles = @($shortcutFiles)
    RemoveTask = $removeTask
  }
}

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

$maintenanceMutex = Enter-DianMaintenanceLock $InstallRoot $MaintenanceLockTimeoutSeconds
try {
  # The first pass makes journal recovery itself conditional on a completely
  # safe current namespace. Recovery can change the set of program trees, so a
  # second pass freezes the exact deletion plan before the first mutation.
  [void](New-UninstallPreflight $InstallRoot ([bool]$ClearData))
  [void](Invoke-DianRecoverInstallTransaction $InstallRoot 1)
  [void](Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1)
  $plan = New-UninstallPreflight $InstallRoot ([bool]$ClearData)
  $marker = $plan.Marker

  $ownedAppRoot = $InstallRoot.TrimEnd('\') + '\'
  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.ExecutablePath) {
      $processPath = [IO.Path]::GetFullPath([string]$_.ExecutablePath)
      if ($processPath.StartsWith($ownedAppRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
      }
    }
  }

  if ($plan.RemoveTask) {
    Unregister-ScheduledTask -TaskName "DianAgentKeepAlive" -Confirm:$false
  }

  foreach ($shortcutPath in @($plan.ShortcutFiles)) {
    Remove-Item -LiteralPath $shortcutPath -Force
  }

  if ($ClearData) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    Write-Host "Dian Agent and all local data were removed." -ForegroundColor Green
    exit 0
  }

  foreach ($target in @($plan.ProgramDirectories)) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  foreach ($target in @($plan.DebrisDirectories)) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  foreach ($target in @($plan.PointerFiles)) {
    Remove-Item -LiteralPath $target -Force
  }
  $marker.current_version = $null
  $marker | Add-Member -NotePropertyName "uninstalled_at" -NotePropertyValue ([DateTime]::UtcNow.ToString("o")) -Force
  $encoding = New-Object Text.UTF8Encoding($false)
  Write-DianAtomicBytes $markerPath $encoding.GetBytes((($marker | ConvertTo-Json) + "`n"))

  Write-Host "Dian Agent was removed. User data was kept at: $InstallRoot" -ForegroundColor Green
  Write-Host "Preserved folders: data, config, knowledge, backup, logs"
} finally {
  Exit-DianMaintenanceLock $maintenanceMutex
}
