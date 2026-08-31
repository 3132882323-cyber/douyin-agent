[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [string]$SourceTools = "",
  [switch]$SkipAutostartMigration,
  [ValidateRange(1, 300)][int]$MaintenanceLockTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-PathChainNoReparsePoints([string]$Path, [string]$Label) {
  $current = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
  while ($current) {
    if (Test-Path -LiteralPath $current) {
      $item = Get-Item -LiteralPath $current -Force
      if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$Label crosses a symbolic link, junction or other reparse point: $current"
      }
    }
    $parent = Split-Path -Parent $current
    if (-not $parent -or $parent.Equals($current, [StringComparison]::OrdinalIgnoreCase)) { break }
    $current = $parent
  }
}

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
if (-not $SourceTools) { $SourceTools = $PSScriptRoot }

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$SourceTools = [IO.Path]::GetFullPath($SourceTools).TrimEnd([IO.Path]::DirectorySeparatorChar)
$rootOfInstall = [IO.Path]::GetPathRoot($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ($InstallRoot -eq $rootOfInstall) { throw "Unsafe install root: $InstallRoot" }
foreach ($boundary in @($InstallRoot, $SourceTools)) {
  if (Test-Path -LiteralPath $boundary) {
    $boundaryItem = Get-Item -LiteralPath $boundary -Force
    if (-not $boundaryItem.PSIsContainer -or
        ($boundaryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Maintenance tools boundary is missing, not a directory, or a reparse point: $boundary"
    }
  }
}
Assert-PathChainNoReparsePoints $InstallRoot "Installation root"
Assert-PathChainNoReparsePoints $SourceTools "Release maintenance tools"

$target = [IO.Path]::GetFullPath((Join-Path $InstallRoot "tools"))

$required = @(
  "start_agent.ps1",
  "watchdog_release.ps1",
  "watchdog_release.vbs",
  "recovery_bootstrap.ps1",
  "repair_agent.ps1",
  "repair_agent.vbs",
  "windows_trust_policy.ps1",
  "uninstall_release.ps1",
  "install_release.ps1",
  "sync_release_tools.ps1",
  "DianAgentUpdater.exe"
)
foreach ($name in $required) {
  $requiredPath = Join-Path $SourceTools $name
  if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
    throw "Release maintenance component is missing: tools\$name"
  }
  if ((Get-Item -LiteralPath $requiredPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Release maintenance component is a reparse point: tools\$name"
  }
}

. (Join-Path $SourceTools "windows_trust_policy.ps1")
$maintenanceMutex = Enter-DianMaintenanceLock $InstallRoot $MaintenanceLockTimeoutSeconds
try {
[void](Invoke-DianRecoverInstallTransaction $InstallRoot 1)
[void](Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1)
$sourceIsInstalledTools = $SourceTools.Equals($target, [StringComparison]::OrdinalIgnoreCase)

$markerPath = Join-Path $InstallRoot ".dian-agent-install.json"
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $markerPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "Installation ownership marker is missing or unsafe. Refusing to replace tools."
}
$marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
$markerRoot = [IO.Path]::GetFullPath([string]$marker.install_root).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ([string]$marker.product -ne "DianAgent" -or [int]$marker.schema -ne 1 -or
    -not $markerRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Installation ownership marker does not exactly match this tools target."
}
Assert-DianNoReparsePoints $SourceTools "Release maintenance tools"
if (Test-Path -LiteralPath $target) {
  Assert-DianNoReparsePoints $target "Installed maintenance tools"
}

# Migrate any existing automatic entrypoint to a recovery pair that is outside
# the tools directory before this script authorizes the first directory rename.
$existingTask = if ($SkipAutostartMigration) { $null } else { Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue }
$startupShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
$existingStartupShortcut = (-not $SkipAutostartMigration) -and (Test-Path -LiteralPath $startupShortcutPath -PathType Leaf)
if ($existingTask -and -not (Test-DianOwnedKeepAliveTask $existingTask $InstallRoot)) {
  throw "The existing keepalive task is not owned by this installation."
}
if ($existingStartupShortcut) {
  if ((Get-Item -LiteralPath $startupShortcutPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "The existing startup shortcut is a reparse point."
  }
  $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($startupShortcutPath)
  if (-not (Test-DianOwnedShortcut $shortcut $startupShortcutPath $InstallRoot)) {
    throw "The existing startup shortcut is not owned by this installation."
  }
}
if (-not $SkipAutostartMigration) { Assert-DianDevelopmentAutostartOwnership }
[void](Install-DianRecoveryBootstrapFiles $InstallRoot $SourceTools)
if (-not $SkipAutostartMigration) {
  $autostartDesired = [bool]($existingTask -or $existingStartupShortcut)
  $startupStatePath = Join-Path $InstallRoot "data\runtime\startup-state.json"
  if (Test-Path -LiteralPath $startupStatePath -PathType Leaf) {
    if ((Get-Item -LiteralPath $startupStatePath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "The startup-state record is a reparse point."
    }
    try {
      $startupState = Get-Content -LiteralPath $startupStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($startupState.autostart_enabled -eq $true -or $startupState.keepalive_enabled -eq $true) {
        $autostartDesired = $true
      }
    } catch {
      if (-not $autostartDesired) {
        throw "The startup-state record is unreadable; refusing to guess whether autostart should be enabled."
      }
    }
  }
  if ($autostartDesired) {
    # Autostart is one product capability: repair both halves if either the
    # prior state or one surviving artifact proves it was enabled.
    Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true
  }
  $removedDevelopmentAutostart = @(Remove-DianDevelopmentAutostartArtifacts)
  if ($removedDevelopmentAutostart.Count -gt 0) {
    Write-Host ("Removed conflicting source-development autostart: {0}" -f ($removedDevelopmentAutostart -join ", ")) -ForegroundColor Yellow
  }
}

# A self-invocation from the installed tools directory is a supported repair
# operation.  Bootstrap publication and autostart reconciliation above must run
# before deciding that no tools-tree swap is necessary.
if ($sourceIsInstalledTools) {
  Write-Host "Installed maintenance tools and stable autostart entrypoints are current."
  exit 0
}

# These names are deliberately distinct from the full installer's `.tools-*`
# transaction paths so one recovery mechanism can never delete the other's
# rollback evidence.
$orphanStages = @(Get-ChildItem -LiteralPath $InstallRoot -Force -Directory -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^\.release-tools-stage-[0-9a-f]{32}$' })
$orphanBackups = @(Get-ChildItem -LiteralPath $InstallRoot -Force -Directory -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^\.release-tools-backup-[0-9a-f]{32}$' })
foreach ($candidate in @($orphanStages + $orphanBackups)) {
  if ($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Release-tools recovery path is a reparse point: $($candidate.FullName)"
  }
  Assert-DianNoReparsePoints $candidate.FullName "Release-tools recovery path"
}
foreach ($candidate in $orphanStages) { Remove-Item -LiteralPath $candidate.FullName -Recurse -Force }
if (-not (Test-Path -LiteralPath $target -PathType Container) -and $orphanBackups.Count -eq 1) {
  [IO.Directory]::Move($orphanBackups[0].FullName, $target)
  $orphanBackups = @()
} elseif (-not (Test-Path -LiteralPath $target -PathType Container) -and $orphanBackups.Count -gt 1) {
  throw "Multiple release-tools rollback trees exist while the active tools directory is missing."
}
if (Test-Path -LiteralPath $target -PathType Container) {
  foreach ($candidate in $orphanBackups) { Remove-Item -LiteralPath $candidate.FullName -Recurse -Force }
}
if (-not (Test-Path -LiteralPath $target -PathType Container)) {
  throw "Installed maintenance tools are missing and no exact rollback tree can restore them."
}

$suffix = [Guid]::NewGuid().ToString("N")
$stage = [IO.Path]::GetFullPath((Join-Path $InstallRoot ".release-tools-stage-$suffix"))
$backup = [IO.Path]::GetFullPath((Join-Path $InstallRoot ".release-tools-backup-$suffix"))
$installPrefix = $InstallRoot + [IO.Path]::DirectorySeparatorChar
foreach ($path in @($target, $stage, $backup)) {
  if (-not $path.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe maintenance tools path: $path"
  }
}

$targetMoved = $false
$stageActivated = $false
$toolsJournal = $null
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

  $toolsJournal = [pscustomobject][ordered]@{
    schema_version = 1
    product = "DianAgent"
    # The transaction ID is also the exact stage/backup suffix, which lets the
    # recovery reader reject a valid-looking journal aimed at another swap.
    transaction_id = $suffix
    install_root = $InstallRoot
    owner_pid = $PID
    owner_started_at = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
    updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    state = "prepared"
    detail = "stage_validated"
    target = $target
    stage = $stage
    backup = $backup
    had_target = $true
  }
  Write-DianReleaseToolsTransaction $InstallRoot $toolsJournal
  Set-DianReleaseToolsTransactionState $InstallRoot $toolsJournal "activating" "switching_tools_tree"

  if (Test-Path -LiteralPath $target) {
    [IO.Directory]::Move($target, $backup)
    $targetMoved = $true
    if ([string]$env:DIAN_AGENT_TOOLS_CRASH_POINT -eq "after-target-to-backup") {
      [Diagnostics.Process]::GetCurrentProcess().Kill()
    }
  }
  [IO.Directory]::Move($stage, $target)
  $stageActivated = $true
  if ([string]$env:DIAN_AGENT_TOOLS_CRASH_POINT -eq "after-stage-to-target") {
    [Diagnostics.Process]::GetCurrentProcess().Kill()
  }
  Set-DianReleaseToolsTransactionState $InstallRoot $toolsJournal "commit_decided" "new_tools_tree_active"
  if ([string]$env:DIAN_AGENT_TOOLS_CRASH_POINT -eq "after-commit-decided") {
    [Diagnostics.Process]::GetCurrentProcess().Kill()
  }
} catch {
  if ($toolsJournal) { [void](Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1) }
  throw
}
[void](Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1)
Write-Host "Installed maintenance tools were updated transactionally." -ForegroundColor Green
} finally {
  Exit-DianMaintenanceLock $maintenanceMutex
}
