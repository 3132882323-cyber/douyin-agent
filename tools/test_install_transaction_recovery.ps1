$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$policyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
  throw "Windows install transaction policy is missing."
}
. $policyPath

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
}

function New-FileSnapshot([string]$Path) {
  $full = [IO.Path]::GetFullPath($Path)
  if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
    return [pscustomobject][ordered]@{ path = $full; existed = $false; data_base64 = ""; sha256 = "" }
  }
  $bytes = [IO.File]::ReadAllBytes($full)
  return [pscustomobject][ordered]@{
    path = $full
    existed = $true
    data_base64 = [Convert]::ToBase64String($bytes)
    sha256 = Get-DianSha256Hex $bytes
  }
}

function New-TestTransaction([string]$Root, [bool]$HadTargets, [bool]$Activate) {
  $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
  New-Item -ItemType Directory -Force -Path $rootFull | Out-Null
  $version = "9.8.7"
  $suffixes = 1..4 | ForEach-Object { [Guid]::NewGuid().ToString("N") }
  $targets = @(
    (Join-Path $rootFull "app\$version"),
    (Join-Path $rootFull "extension\$version"),
    (Join-Path $rootFull "extension-current"),
    (Join-Path $rootFull "tools")
  )
  $preparedLeaves = @(
    ".app-$version-stage-$($suffixes[0])",
    ".extension-$version-stage-$($suffixes[1])",
    ".extension-current-stage-$($suffixes[2])",
    ".tools-stage-$($suffixes[3])"
  )
  $activations = @()
  for ($index = 0; $index -lt $targets.Count; $index++) {
    $target = [IO.Path]::GetFullPath($targets[$index])
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $prepared = Join-Path $parent $preparedLeaves[$index]
    $backup = Join-Path $parent (".{0}-backup-{1}" -f (Split-Path -Leaf $target), [Guid]::NewGuid().ToString("N"))
    if ($HadTargets) {
      New-Item -ItemType Directory -Force -Path $target | Out-Null
      [IO.File]::WriteAllText((Join-Path $target "payload.txt"), "old-$index", [Text.Encoding]::UTF8)
    }
    New-Item -ItemType Directory -Force -Path $prepared | Out-Null
    [IO.File]::WriteAllText((Join-Path $prepared "payload.txt"), "new-$index", [Text.Encoding]::UTF8)
    $activations += [pscustomobject][ordered]@{
      target = $target
      prepared = $prepared
      backup = $backup
      had_target = $HadTargets
    }
  }

  $versionPath = Join-Path $rootFull "current-version.txt"
  $currentPath = Join-Path $rootFull "current.json"
  $markerPath = Join-Path $rootFull ".dian-agent-install.json"
  $startupPath = Join-Path $rootFull "data\runtime\startup-state.json"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $startupPath) | Out-Null
  [IO.File]::WriteAllText($versionPath, "$version`r`n", [Text.Encoding]::ASCII)
  [IO.File]::WriteAllText($currentPath, '{"version":"9.8.7","version_path":"old-layout"}', [Text.Encoding]::UTF8)
  [IO.File]::WriteAllText($markerPath, '{"product":"DianAgent","generation":"old"}', [Text.Encoding]::UTF8)
  [IO.File]::WriteAllText($startupPath, '{"state":"old"}', [Text.Encoding]::UTF8)
  $snapshots = @(@($versionPath, $currentPath, $markerPath, $startupPath) | ForEach-Object { New-FileSnapshot $_ })

  $startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
  $programsShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"
  $repairShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk"
  $journal = [pscustomobject][ordered]@{
    schema_version = 1
    product = "DianAgent"
    transaction_id = [Guid]::NewGuid().ToString("N")
    install_root = $rootFull
    target_version = $version
    owner_pid = 2147483000
    owner_started_at = "2000-01-01T00:00:00.0000000Z"
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
    updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    state = "prepared"
    detail = "fixture"
    activations = $activations
    file_snapshots = $snapshots
    autostart = [pscustomobject][ordered]@{
      managed = $false
      task = [pscustomobject][ordered]@{ existed = $false; data_base64 = ""; sha256 = "" }
      shortcuts = @(
        [pscustomobject][ordered]@{ path = [IO.Path]::GetFullPath($startupShortcut); existed = $false; data_base64 = ""; sha256 = "" },
        [pscustomobject][ordered]@{ path = [IO.Path]::GetFullPath($programsShortcut); existed = $false; data_base64 = ""; sha256 = "" },
        [pscustomobject][ordered]@{ path = [IO.Path]::GetFullPath($repairShortcut); existed = $false; data_base64 = ""; sha256 = "" }
      )
    }
  }
  Write-DianInstallTransaction $rootFull $journal

  if ($Activate) {
    foreach ($activation in $activations) {
      if ($activation.had_target) { [IO.Directory]::Move($activation.target, $activation.backup) }
      [IO.Directory]::Move($activation.prepared, $activation.target)
    }
    [IO.File]::WriteAllText($currentPath, '{"version":"9.8.7","version_path":"new-layout"}', [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($markerPath, '{"product":"DianAgent","generation":"new"}', [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($startupPath, '{"state":"new"}', [Text.Encoding]::UTF8)
    Set-DianInstallTransactionState $rootFull $journal "pointers_activated" "fixture_activated"
  }
  return [pscustomobject]@{ Root = $rootFull; Journal = $journal; Activations = $activations; Snapshots = $snapshots }
}

function Assert-SnapshotsRestored([object]$Fixture) {
  foreach ($snapshot in @($Fixture.Snapshots)) {
    Assert-True (Test-Path -LiteralPath $snapshot.path -PathType Leaf) "Snapshot target was not restored: $($snapshot.path)"
    $actual = [IO.File]::ReadAllBytes([string]$snapshot.path)
    Assert-True ((Get-DianSha256Hex $actual) -eq [string]$snapshot.sha256) "Snapshot bytes changed during rollback: $($snapshot.path)"
  }
}

function Assert-OldTreesRestored([object]$Fixture) {
  for ($index = 0; $index -lt $Fixture.Activations.Count; $index++) {
    $activation = $Fixture.Activations[$index]
    Assert-True (Test-Path -LiteralPath $activation.target -PathType Container) "Old target is missing after rollback: $($activation.target)"
    Assert-True ((Get-Content -LiteralPath (Join-Path $activation.target "payload.txt") -Raw) -eq "old-$index") "Wrong tree is active after rollback: $($activation.target)"
    Assert-True (-not (Test-Path -LiteralPath $activation.prepared)) "Prepared tree survived rollback: $($activation.prepared)"
    Assert-True (-not (Test-Path -LiteralPath $activation.backup)) "Backup tree survived rollback: $($activation.backup)"
  }
}

function Invoke-HardKillRecovery([string]$Root, [string]$Point, [string]$ChildScript) {
  $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $startInfo = New-Object Diagnostics.ProcessStartInfo
  $startInfo.FileName = $powershell
  $startInfo.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PolicyPath "{1}" -InstallRoot "{2}"' -f $ChildScript, $policyPath, $Root
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.EnvironmentVariables["DIAN_AGENT_RECOVERY_CRASH_POINT"] = $Point
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $startInfo
  try {
    Assert-True ($process.Start()) "Could not start recovery fault process."
    if (-not $process.WaitForExit(15000)) {
      $process.Kill()
      throw "Recovery fault process did not exit at point: $Point"
    }
    Assert-True ($process.ExitCode -ne 0) "Recovery fault point did not hard-kill the process: $Point"
  } finally {
    $process.Dispose()
  }
  Assert-True (Test-Path -LiteralPath (Get-DianInstallTransactionPath $Root) -PathType Leaf) "Hard-kill lost the durable transaction journal: $Point"
}

function Invoke-StableBootstrapRecovery([string]$Root) {
  $bootstrap = Get-DianRecoveryBootstrapPath $Root
  Assert-True (Test-Path -LiteralPath $bootstrap -PathType Leaf) "Stable recovery bootstrap is missing."
  $launcher = Get-DianRecoveryBootstrapLauncherPath $Root
  Assert-True (Test-Path -LiteralPath $launcher -PathType Leaf) "Hidden recovery bootstrap launcher is missing."
  $wscript = Join-Path $env:WINDIR "System32\wscript.exe"
  $process = Start-Process -FilePath $wscript -ArgumentList ('"' + $launcher + '"') -WindowStyle Hidden -PassThru
  try {
    Assert-True ($process.WaitForExit(30000)) "Stable recovery bootstrap did not exit."
  } finally {
    if (-not $process.HasExited) { $process.Kill() }
    $process.Dispose()
  }
}

$testParent = Join-Path ([IO.Path]::GetTempPath()) "DianAgentInstallTransactionTests"
$sandbox = Join-Path $testParent ("journal-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))
$sandboxFull = [IO.Path]::GetFullPath($sandbox)
$testParentFull = [IO.Path]::GetFullPath($testParent).TrimEnd('\') + '\'
if (-not $sandboxFull.StartsWith($testParentFull, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe install transaction test sandbox."
}

try {
  New-Item -ItemType Directory -Force -Path $sandboxFull | Out-Null

  $ordinary = New-TestTransaction (Join-Path $sandboxFull "ordinary") $true $true
  $ordinaryResult = Invoke-DianRecoverInstallTransaction $ordinary.Root 5
  Assert-True ($ordinaryResult.Action -eq "rolled_back") "Activated transaction was not rolled back."
  Assert-OldTreesRestored $ordinary
  Assert-SnapshotsRestored $ordinary
  Assert-True (-not (Test-Path -LiteralPath (Get-DianInstallTransactionPath $ordinary.Root))) "Rollback journal was not removed."

  $fresh = New-TestTransaction (Join-Path $sandboxFull "fresh") $false $true
  $freshResult = Invoke-DianRecoverInstallTransaction $fresh.Root 5
  Assert-True ($freshResult.Action -eq "rolled_back") "Fresh transaction was not rolled back."
  foreach ($activation in $fresh.Activations) {
    Assert-True (-not (Test-Path -LiteralPath $activation.target)) "Fresh target remained active after rollback."
    Assert-True (-not (Test-Path -LiteralPath $activation.prepared)) "Fresh prepared tree survived rollback."
  }
  Assert-SnapshotsRestored $fresh

  $commit = New-TestTransaction (Join-Path $sandboxFull "commit") $true $true
  Set-DianInstallTransactionState $commit.Root $commit.Journal "commit_decided" "fixture_commit"
  $commitResult = Invoke-DianRecoverInstallTransaction $commit.Root 5
  Assert-True ($commitResult.Action -eq "finalized_commit") "Committed transaction was not finalized."
  for ($index = 0; $index -lt $commit.Activations.Count; $index++) {
    $activation = $commit.Activations[$index]
    Assert-True ((Get-Content -LiteralPath (Join-Path $activation.target "payload.txt") -Raw) -eq "new-$index") "Commit finalization changed the active program tree."
    Assert-True (-not (Test-Path -LiteralPath $activation.backup)) "Commit finalization retained a backup."
  }

  $ambiguous = New-TestTransaction (Join-Path $sandboxFull "ambiguous") $true $true
  Remove-Item -LiteralPath $ambiguous.Activations[0].backup -Recurse -Force
  $ambiguityRejected = $false
  try { [void](Invoke-DianRecoverInstallTransaction $ambiguous.Root 5) } catch { $ambiguityRejected = $true }
  Assert-True $ambiguityRejected "Ambiguous rollback state was not rejected."
  Assert-True (Test-Path -LiteralPath (Get-DianInstallTransactionPath $ambiguous.Root) -PathType Leaf) "Ambiguous rollback discarded its evidence."
  Assert-True ((Get-Content -LiteralPath (Join-Path $ambiguous.Activations[0].target "payload.txt") -Raw) -eq "new-0") "Ambiguous preflight mutated an active tree."

  $crossTarget = New-TestTransaction (Join-Path $sandboxFull "cross-target-journal") $true $false
  $crossTarget.Journal.activations[2].prepared = [string]$crossTarget.Journal.activations[3].prepared
  Write-DianInstallTransaction $crossTarget.Root $crossTarget.Journal
  $crossTargetRejected = $false
  try { [void](Invoke-DianRecoverInstallTransaction $crossTarget.Root 5) } catch { $crossTargetRejected = $true }
  Assert-True $crossTargetRejected "A program activation was allowed to borrow another target's prepared tree."
  Assert-True (Test-Path -LiteralPath (Get-DianInstallTransactionPath $crossTarget.Root) -PathType Leaf) "Cross-target journal evidence was discarded."

  $ownerFixture = New-TestTransaction (Join-Path $sandboxFull "live-owner") $true $false
  $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $owner = Start-Process -FilePath $powershell -ArgumentList '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 60"' -WindowStyle Hidden -PassThru
  try {
    $ownerFixture.Journal.owner_pid = $owner.Id
    $ownerFixture.Journal.owner_started_at = $owner.StartTime.ToUniversalTime().ToString("o")
    Write-DianInstallTransaction $ownerFixture.Root $ownerFixture.Journal
    $liveOwnerRejected = $false
    try { [void](Invoke-DianRecoverInstallTransaction $ownerFixture.Root 5) } catch { $liveOwnerRejected = ($_.Exception.Message -match "owner is still alive") }
    Assert-True $liveOwnerRejected "A live transaction owner did not block recovery."
    Assert-True (Test-Path -LiteralPath (Get-DianInstallTransactionPath $ownerFixture.Root) -PathType Leaf) "Live-owner check discarded the journal."
  } finally {
    Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
    [void]$owner.WaitForExit(5000)
    $owner.Dispose()
  }
  $ownerFixture.Journal.owner_pid = 2147483000
  Write-DianInstallTransaction $ownerFixture.Root $ownerFixture.Journal
  [void](Invoke-DianRecoverInstallTransaction $ownerFixture.Root 5)
  Assert-OldTreesRestored $ownerFixture

  $killFixture = New-TestTransaction (Join-Path $sandboxFull "hard-kill-same-version") $true $true
  $childScript = Join-Path $sandboxFull "invoke-recovery.ps1"
  @'
param([string]$PolicyPath, [string]$InstallRoot)
$ErrorActionPreference = "Stop"
. $PolicyPath
[void](Invoke-DianRecoverInstallTransaction $InstallRoot 5)
'@ | Set-Content -LiteralPath $childScript -Encoding UTF8
  Invoke-HardKillRecovery $killFixture.Root "after-rollback-state" $childScript
  for ($index = $killFixture.Activations.Count - 1; $index -ge 0; $index--) {
    foreach ($point in @(
      ("after-target-to-prepared-{0}" -f $index),
      ("after-backup-to-target-{0}" -f $index),
      ("after-directory-restored-{0}" -f $index),
      ("after-prepared-cleanup-{0}" -f $index)
    )) {
      Invoke-HardKillRecovery $killFixture.Root $point $childScript
    }
  }
  $killResult = Invoke-DianRecoverInstallTransaction $killFixture.Root 5
  Assert-True ($killResult.Action -eq "rolled_back") "Hard-kill sequence did not finish rollback."
  Assert-OldTreesRestored $killFixture
  Assert-SnapshotsRestored $killFixture

  # The task/Startup entrypoint must remain usable while the active tools path
  # is absent and after the old, journal-unaware tools tree has been restored.
  # Launch the exact stable bootstrap command instead of dot-sourcing the
  # repository policy as the older regression above deliberately does.
  foreach ($point in @("after-target-to-prepared-3", "after-backup-to-target-3")) {
    $entryFixture = New-TestTransaction (Join-Path $sandboxFull ("bootstrap-{0}" -f $point)) $true $true
    [void](Install-DianRecoveryBootstrapFiles $entryFixture.Root $PSScriptRoot)
    $legacyPolicy = Join-Path ([string]$entryFixture.Activations[3].backup) "windows_trust_policy.ps1"
    [IO.File]::WriteAllText($legacyPolicy, 'throw "legacy tools cannot read the new install journal"', [Text.Encoding]::UTF8)
    Invoke-HardKillRecovery $entryFixture.Root $point $childScript
    Invoke-StableBootstrapRecovery $entryFixture.Root
    Assert-True (-not (Test-Path -LiteralPath (Get-DianInstallTransactionPath $entryFixture.Root))) "Stable bootstrap did not finish recovery at: $point"
    Assert-OldTreesRestored $entryFixture
    Assert-SnapshotsRestored $entryFixture
    Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapPath $entryFixture.Root) -PathType Leaf) "Recovery removed its stable bootstrap."
    Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapLauncherPath $entryFixture.Root) -PathType Leaf) "Recovery removed its hidden stable bootstrap launcher."
  }

  Write-Host "Windows install transaction recovery tests passed." -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $sandboxFull) {
    Remove-Item -LiteralPath $sandboxFull -Recurse -Force
  }
}
