$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$policyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
$syncPath = Join-Path $PSScriptRoot "sync_release_tools.ps1"
. $policyPath

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
}

$testParent = Join-Path ([IO.Path]::GetTempPath()) "DianAgentReleaseToolsTests"
$sandbox = [IO.Path]::GetFullPath((Join-Path $testParent ("tools-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))))
$parentPrefix = [IO.Path]::GetFullPath($testParent).TrimEnd('\') + '\'
if (-not $sandbox.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe release-tools test sandbox."
}

try {
  $root = Join-Path $sandbox "DianAgent"
  $target = Join-Path $root "tools"
  $media = Join-Path $sandbox "media-tools"
  New-Item -ItemType Directory -Force -Path $target, $media | Out-Null
  [IO.File]::WriteAllText((Join-Path $target "old-sentinel.txt"), "old-tools", [Text.Encoding]::UTF8)
  [IO.File]::WriteAllText((Join-Path $target "windows_trust_policy.ps1"), 'throw "legacy tools cannot recover the new journal"', [Text.Encoding]::UTF8)
  [IO.File]::WriteAllText(
    (Join-Path $target "watchdog_release.ps1"),
    'param([string]$InstallRoot,[int]$Port) exit 0',
    [Text.Encoding]::UTF8
  )
  [ordered]@{
    product = "DianAgent"
    schema = 1
    install_root = [IO.Path]::GetFullPath($root).TrimEnd('\')
    current_version = "1.0.0"
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root ".dian-agent-install.json") -Encoding UTF8

  foreach ($name in @(
    "start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1",
    "repair_agent.ps1", "repair_agent.vbs", "windows_trust_policy.ps1", "uninstall_release.ps1",
    "install_release.ps1", "sync_release_tools.ps1"
  )) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $media $name) -Force
  }
  Copy-Item -LiteralPath (Join-Path $projectRoot "dist\agent\DianAgentUpdater.exe") `
    -Destination (Join-Path $media "DianAgentUpdater.exe") -Force

  $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $startInfo = New-Object Diagnostics.ProcessStartInfo
  $startInfo.FileName = $powershell
  $startInfo.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -SourceTools "{2}" -SkipAutostartMigration' -f $syncPath, $root, $media
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.EnvironmentVariables["DIAN_AGENT_TOOLS_CRASH_POINT"] = "after-target-to-backup"
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $startInfo
  try {
    Assert-True ($process.Start()) "Could not start release-tools hard-kill process."
    Assert-True ($process.WaitForExit(30000)) "Release-tools hard-kill process did not exit."
    Assert-True ($process.ExitCode -ne 0) "Release-tools fault hook did not hard-kill the process."
  } finally {
    if (-not $process.HasExited) { $process.Kill() }
    $process.Dispose()
  }
  Assert-True (-not (Test-Path -LiteralPath $target)) "Tools target was not absent at the injected rename boundary."
  Assert-True (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $root) -PathType Leaf) "Release-tools hard kill lost its journal."
  Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapPath $root) -PathType Leaf) "Stable bootstrap was not published before the tools rename."
  Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapLauncherPath $root) -PathType Leaf) "Hidden stable bootstrap launcher was not published before the tools rename."

  $wscript = Join-Path $env:WINDIR "System32\wscript.exe"
  $bootstrapProcess = Start-Process -FilePath $wscript `
    -ArgumentList ('"' + (Get-DianRecoveryBootstrapLauncherPath $root) + '"') -WindowStyle Hidden -PassThru
  try {
    Assert-True ($bootstrapProcess.WaitForExit(30000)) "Stable bootstrap did not finish release-tools recovery."
  } finally {
    if (-not $bootstrapProcess.HasExited) { $bootstrapProcess.Kill() }
    $bootstrapProcess.Dispose()
  }
  Assert-True (Test-Path -LiteralPath (Join-Path $target "old-sentinel.txt") -PathType Leaf) "Stable bootstrap did not restore the previous tools tree."
  Assert-True (-not (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $root))) "Stable bootstrap left the release-tools journal behind."
  Assert-True (@(Get-ChildItem -LiteralPath $root -Force -Directory | Where-Object { $_.Name -match '^\.release-tools-(?:stage|backup)-' }).Count -eq 0) "Release-tools recovery left stage or backup debris."

  # Kill the stable bootstrap during its own first rollback rename, then invoke
  # the same external entrypoint again. The write-ahead journal must make the
  # second recovery deterministic while tools is absent.
  $recoverySuffix = [Guid]::NewGuid().ToString("N")
  $recoveryStage = Join-Path $root ".release-tools-stage-$recoverySuffix"
  $recoveryBackup = Join-Path $root ".release-tools-backup-$recoverySuffix"
  New-Item -ItemType Directory -Path $recoveryStage | Out-Null
  [IO.File]::WriteAllText((Join-Path $recoveryStage "new-sentinel.txt"), "new-tools", [Text.Encoding]::UTF8)
  $recoveryJournal = [pscustomobject][ordered]@{
    schema_version = 1; product = "DianAgent"; transaction_id = $recoverySuffix
    install_root = $root; owner_pid = 2147483000; owner_started_at = "2000-01-01T00:00:00Z"
    state = "activating"; target = $target; stage = $recoveryStage; backup = $recoveryBackup; had_target = $true
  }
  Write-DianReleaseToolsTransaction $root $recoveryJournal
  [IO.Directory]::Move($target, $recoveryBackup)
  [IO.Directory]::Move($recoveryStage, $target)

  $recoveryCrashInfo = New-Object Diagnostics.ProcessStartInfo
  $recoveryCrashInfo.FileName = $powershell
  $recoveryCrashInfo.Arguments = Get-DianRecoveryBootstrapArguments $root
  $recoveryCrashInfo.UseShellExecute = $false
  $recoveryCrashInfo.CreateNoWindow = $true
  $recoveryCrashInfo.EnvironmentVariables["DIAN_AGENT_TOOLS_RECOVERY_CRASH_POINT"] = "after-target-to-stage"
  $recoveryCrash = New-Object Diagnostics.Process
  $recoveryCrash.StartInfo = $recoveryCrashInfo
  try {
    Assert-True ($recoveryCrash.Start()) "Could not start the bootstrap recovery hard-kill process."
    Assert-True ($recoveryCrash.WaitForExit(30000)) "Bootstrap recovery hard-kill process did not exit."
    Assert-True ($recoveryCrash.ExitCode -ne 0) "Bootstrap recovery fault hook did not hard-kill the process."
  } finally {
    if (-not $recoveryCrash.HasExited) { $recoveryCrash.Kill() }
    $recoveryCrash.Dispose()
  }
  Assert-True (-not (Test-Path -LiteralPath $target)) "Recovery hard kill did not expose the target-missing boundary."
  Assert-True (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $root) -PathType Leaf) "Recovery hard kill lost its journal."
  $reentry = Start-Process -FilePath $powershell -ArgumentList (Get-DianRecoveryBootstrapArguments $root) `
    -WindowStyle Hidden -PassThru
  try {
    Assert-True ($reentry.WaitForExit(30000)) "Stable bootstrap re-entry did not finish."
  } finally {
    if (-not $reentry.HasExited) { $reentry.Kill() }
    $reentry.Dispose()
  }
  Assert-True (Test-Path -LiteralPath (Join-Path $target "old-sentinel.txt") -PathType Leaf) "Stable bootstrap re-entry did not restore old tools."
  Assert-True (-not (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $root))) "Stable bootstrap re-entry left its journal."

  # An exact-root mismatch must retain both the journal and active tools tree.
  $forged = [pscustomobject][ordered]@{
    schema_version = 1; product = "DianAgent"; transaction_id = [Guid]::NewGuid().ToString("N")
    install_root = $root; owner_pid = 2147483000; owner_started_at = "2000-01-01T00:00:00Z"
    state = "prepared"; target = $target; stage = (Join-Path $root ".release-tools-stage-00000000000000000000000000000000")
    backup = (Join-Path $root ".release-tools-backup-00000000000000000000000000000000"); had_target = $true
  }
  Write-DianReleaseToolsTransaction $root $forged
  $forgedRejected = $false
  try { [void](Invoke-DianRecoverReleaseToolsTransaction $root 5) } catch { $forgedRejected = $true }
  Assert-True $forgedRejected "Out-of-root release-tools journal was not rejected."
  Assert-True (Test-Path -LiteralPath (Join-Path $target "old-sentinel.txt") -PathType Leaf) "Forged journal mutated the active tools tree."
  Assert-True (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $root) -PathType Leaf) "Forged journal evidence was discarded."

  # Running the synchronizer from the already-installed tools directory must
  # still self-heal the external bootstrap before returning "already current".
  $selfRoot = Join-Path $sandbox "self-sync-root"
  $selfTools = Join-Path $selfRoot "tools"
  New-Item -ItemType Directory -Force -Path $selfTools | Out-Null
  Get-ChildItem -LiteralPath $media -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $selfTools -Recurse -Force
  }
  [ordered]@{
    product = "DianAgent"
    schema = 1
    install_root = [IO.Path]::GetFullPath($selfRoot).TrimEnd('\')
    current_version = "1.0.0"
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $selfRoot ".dian-agent-install.json") -Encoding UTF8
  $selfSyncArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -SourceTools "{2}" -SkipAutostartMigration' -f `
    (Join-Path $selfTools "sync_release_tools.ps1"), $selfRoot, $selfTools
  $selfSyncProcess = Start-Process -FilePath $powershell -ArgumentList $selfSyncArguments -WindowStyle Hidden -Wait -PassThru
  try {
    Assert-True ($selfSyncProcess.ExitCode -eq 0) "Installed-tools self-sync failed."
  } finally {
    $selfSyncProcess.Dispose()
  }
  Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapPath $selfRoot) -PathType Leaf) "Installed-tools self-sync skipped bootstrap repair."
  Assert-True (Test-Path -LiteralPath (Get-DianRecoveryBootstrapLauncherPath $selfRoot) -PathType Leaf) "Installed-tools self-sync skipped hidden-launcher repair."
  Assert-True (-not (Test-Path -LiteralPath (Get-DianReleaseToolsTransactionPath $selfRoot))) "Installed-tools self-sync created an unnecessary tools transaction."

  Write-Host "Release-tools stable bootstrap recovery tests passed." -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $sandbox) { Remove-Item -LiteralPath $sandbox -Recurse -Force }
}
