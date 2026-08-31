$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$starterPath = Join-Path $PSScriptRoot "start_agent.ps1"
$watchdogPath = Join-Path $PSScriptRoot "watchdog_release.ps1"
$recoveryBootstrapPath = Join-Path $PSScriptRoot "recovery_bootstrap.ps1"
$sourceWatchdogPath = Join-Path (Split-Path -Parent $PSScriptRoot) "bridge\watchdog.ps1"
$syncPath = Join-Path $PSScriptRoot "sync_release_tools.ps1"
$installerPath = Join-Path $PSScriptRoot "install_release.ps1"
$releaseBuilderPath = Join-Path $PSScriptRoot "build_release.ps1"
$releaseCoreBuilderPath = Join-Path $PSScriptRoot "build_release_core.ps1"
$upgradeRecoveryTestPath = Join-Path $PSScriptRoot "test_upgrade_recovery.ps1"

$sources = [ordered]@{}
foreach ($path in @($starterPath, $watchdogPath, $recoveryBootstrapPath, $sourceWatchdogPath, $syncPath, $installerPath, $releaseBuilderPath, $releaseCoreBuilderPath, $upgradeRecoveryTestPath)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Watchdog contract source is missing: $path"
  }
  $tokens = $null
  $parseErrors = $null
  [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors) | Out-Null
  if ($parseErrors.Count -gt 0) {
    throw "PowerShell syntax check failed for $path`: $($parseErrors[0].Message)"
  }
  $sources[$path] = Get-Content -LiteralPath $path -Raw -Encoding UTF8
}

$starter = $sources[$starterPath]
$watchdog = $sources[$watchdogPath]
$recoveryBootstrap = $sources[$recoveryBootstrapPath]
$sourceWatchdog = $sources[$sourceWatchdogPath]
$sync = $sources[$syncPath]
$installer = $sources[$installerPath]
$releaseBuilder = $sources[$releaseBuilderPath]
$releaseCoreBuilder = $sources[$releaseCoreBuilderPath]
$upgradeRecoveryTest = $sources[$upgradeRecoveryTestPath]

foreach ($entry in @(
  @{ Name = "starter"; Text = $starter },
  @{ Name = "watchdog"; Text = $watchdog },
  @{ Name = "upgrade recovery test"; Text = $upgradeRecoveryTest }
)) {
  if ($entry.Text -notmatch '/health/live') {
    throw "$($entry.Name) does not use the lightweight /health/live endpoint."
  }
}
foreach ($entry in @(
  @{ Name = "starter"; Text = $starter },
  @{ Name = "watchdog"; Text = $watchdog }
)) {
  if ($entry.Text -notmatch 'function\s+Test-ExpectedAgentListener' -or
      $entry.Text -notmatch '(?:ownerPath|ExecutablePath)\.Equals\(\$agentPath') {
    throw "$($entry.Name) accepts version-only health without proving the exact active executable listener."
  }
}
foreach ($entry in @(
  @{ Name = "starter"; Text = $starter },
  @{ Name = "watchdog"; Text = $watchdog }
)) {
  if ($entry.Text -notmatch 'legacyHealthUrl' -or $entry.Text -notmatch 'statusCode\s*-eq\s*404') {
    throw "$($entry.Name) does not restrict legacy /health fallback to an explicit 404."
  }
}

if ($watchdog -notmatch '\[ValidateRange\(3,\s*10\)\]\[int\]\$FailureThreshold\s*=\s*3') {
  throw "Watchdog must default to at least three consecutive failed probes."
}
if ($installer -match 'MaintenanceLockHeldByParent' -or
    $starter -match 'MaintenanceLockHeldByParent' -or
    $watchdog -match 'MaintenanceLockHeldByParent') {
  throw "A public command-line switch can bypass the per-install maintenance lock."
}
if ($installer -notmatch 'function\s+Start-InstalledAgentUnderMaintenanceLock' -or
    $installer -notmatch 'Get-NetTCPConnection\s+-LocalPort\s+\$Port') {
  throw "Installer-owned startup must run directly under the maintenance lock and prove the exact listener identity."
}
if ($installer -notmatch 'ownedProgramRoots' -or
    $installer -notmatch 'Join-Path\s+\$InstallRoot\s+"app"' -or
    $installer -notmatch 'Join-Path\s+\$InstallRoot\s+"versions"') {
  throw "Full installer does not retire Agents from both app/ and versions/ program layouts."
}
if ($watchdog -notmatch 'function\s+Confirm-ListeningAgentUnhealthy' -or
    $watchdog -notmatch 'Start-Sleep\s+-Milliseconds\s+\$RecoveryGraceMilliseconds') {
  throw "Watchdog is missing its consecutive-failure grace check."
}
foreach ($entry in @(
  @{ Name = "release watchdog"; Text = $watchdog; WaitFunction = "Wait-ForExactAgentStartupBudget" },
  @{ Name = "source watchdog"; Text = $sourceWatchdog; WaitFunction = "Wait-ForExactRuntimeStartupBudget" }
)) {
  if ($entry.Text -notmatch '\[ValidateRange\(1,\s*300\)\]\[int\]\$StartupTimeoutSeconds' -or
      $entry.Text -notmatch 'CreationDate' -or
      $entry.Text -notmatch [regex]::Escape("function $($entry.WaitFunction)") -or
      $entry.Text -notmatch '\.AddSeconds\(\$StartupTimeoutSeconds\)') {
    throw "$($entry.Name) does not reserve the remaining CreationDate-based cold-start budget."
  }
  $budgetIndex = $entry.Text.IndexOf("$($entry.WaitFunction) $", [StringComparison]::Ordinal)
  $prebindStopIndexForBudget = $entry.Text.IndexOf('Stop-Process -Id ([int]$stalledProcess.ProcessId)', [StringComparison]::Ordinal)
  if ($budgetIndex -lt 0 -or $prebindStopIndexForBudget -le $budgetIndex) {
    throw "$($entry.Name) can stop a pre-bind process before applying its cold-start budget."
  }
}

$graceIndex = $watchdog.IndexOf('Confirm-ListeningAgentUnhealthy', [StringComparison]::Ordinal)
$ownershipRefreshIndex = $watchdog.IndexOf('$refreshedListeners = @(Get-PortListeners)', [StringComparison]::Ordinal)
$processLivenessIndex = $watchdog.IndexOf('Get-Process -Id $owner.ProcessId', [StringComparison]::Ordinal)
$finalOwnershipIndex = $watchdog.IndexOf('$finalStopPids = @()', [StringComparison]::Ordinal)
$stopIndex = $watchdog.IndexOf('Stop-Process -Id $ownerPid', [StringComparison]::Ordinal)
if ($graceIndex -lt 0 -or $ownershipRefreshIndex -le $graceIndex -or
    $processLivenessIndex -le $ownershipRefreshIndex -or $finalOwnershipIndex -le $processLivenessIndex -or
    $stopIndex -le $finalOwnershipIndex) {
  throw "Watchdog restart order must be: grace probes, refreshed listener ownership, live process proof, final ownership proof, then stop."
}
if ($watchdog -notmatch 'Test-OwnedAgentPath' -or $watchdog -notmatch 'DianAgent\.exe') {
  throw "Watchdog does not constrain restart ownership to an installed DianAgent.exe."
}
if ($watchdog -notmatch 'One last live probe closes the race') {
  throw "Watchdog must probe once more immediately before a confirmed stop."
}
if ($watchdog -notmatch 'function\s+Get-ExactAgentProcesses' -or
    $watchdog -notmatch 'prebind_process_stalled' -or
    $watchdog -notmatch 'prebind_process_identity_changed' -or
    $watchdog -notmatch 'stalled_process_not_released') {
  throw "Watchdog cannot recover an exact current-version Agent that stalls before binding its port."
}
if ($sourceWatchdog -notmatch 'function\s+Get-ExactRuntimeProcesses' -or
    $sourceWatchdog -notmatch 'prebind_process_stalled' -or
    $sourceWatchdog -notmatch 'prebind_process_identity_changed' -or
    $sourceWatchdog -notmatch 'stalled_process_not_released' -or
    $sourceWatchdog -notmatch 'Stop-Process\s+-Id\s+\(\[int\]\$stalledProcess\.ProcessId\)') {
  throw "Source watchdog cannot replace an exact checkout runtime that stalls before binding its port."
}
if ($sourceWatchdog -notmatch '\.startup-state-' -or
    $sourceWatchdog -notmatch 'Move-Item\s+-LiteralPath\s+\$temporary\s+-Destination\s+\$startupStatePath\s+-Force') {
  throw "Source watchdog startup state is not published with an atomic replace."
}

$prebindCheckIndex = $watchdog.IndexOf('$listeners.Count -eq 0 -and $alreadyStarting.Count -gt 0', [StringComparison]::Ordinal)
$prebindBudgetIndex = $watchdog.IndexOf('Wait-ForExactAgentStartupBudget $alreadyStarting', $prebindCheckIndex, [StringComparison]::Ordinal)
$prebindGraceIndex = $watchdog.IndexOf('Confirm-ListeningAgentUnhealthy', $prebindBudgetIndex, [StringComparison]::Ordinal)
$prebindRefreshIndex = $watchdog.IndexOf('$stalledProcesses = @(Get-ExactAgentProcesses)', $prebindGraceIndex, [StringComparison]::Ordinal)
$prebindFinalProbeIndex = $watchdog.IndexOf('if (Test-AgentHealth)', $prebindRefreshIndex, [StringComparison]::Ordinal)
$prebindFinalRefreshIndex = $watchdog.IndexOf('$finalStalledProcesses = @(Get-ExactAgentProcesses)', $prebindFinalProbeIndex, [StringComparison]::Ordinal)
$prebindStopIndex = $watchdog.IndexOf('Stop-Process -Id ([int]$stalledProcess.ProcessId)', $prebindFinalRefreshIndex, [StringComparison]::Ordinal)
if ($prebindCheckIndex -lt 0 -or $prebindBudgetIndex -le $prebindCheckIndex -or
    $prebindGraceIndex -le $prebindBudgetIndex -or
    $prebindRefreshIndex -le $prebindGraceIndex -or $prebindFinalProbeIndex -le $prebindRefreshIndex -or
    $prebindFinalRefreshIndex -le $prebindFinalProbeIndex -or $prebindStopIndex -le $prebindFinalRefreshIndex) {
  throw "Pre-bind recovery must be: detect exact process, preserve its remaining startup budget, run grace probes, refresh identity, final health probe, refresh again, then stop."
}

$sourcePrebindCheckIndex = $sourceWatchdog.IndexOf('if ($alreadyStarting.Count -gt 0)', [StringComparison]::Ordinal)
$sourcePrebindBudgetIndex = $sourceWatchdog.IndexOf('Wait-ForExactRuntimeStartupBudget $alreadyStarting', $sourcePrebindCheckIndex, [StringComparison]::Ordinal)
$sourcePrebindStopIndex = $sourceWatchdog.IndexOf('Stop-Process -Id ([int]$stalledProcess.ProcessId)', $sourcePrebindBudgetIndex, [StringComparison]::Ordinal)
if ($sourcePrebindCheckIndex -lt 0 -or $sourcePrebindBudgetIndex -le $sourcePrebindCheckIndex -or
    $sourcePrebindStopIndex -le $sourcePrebindBudgetIndex -or
    $sourceWatchdog -notmatch '\$StartupTimeoutSeconds\s*\*\s*2') {
  throw "Source watchdog does not apply one startup timeout consistently before replacement and after launch."
}

$rollbackBranchIndex = $starter.IndexOf('if ($pendingUpgrade -and -not $DeferPendingConfirmation -and -not $RecoveryAttempted)', [StringComparison]::Ordinal)
$deferredFailureIndex = $starter.IndexOf('if ($pendingUpgrade -and $DeferPendingConfirmation)', [StringComparison]::Ordinal)
$deferredExactStopIndex = $starter.IndexOf('.Equals($agentPath, [StringComparison]::OrdinalIgnoreCase)', $deferredFailureIndex, [StringComparison]::Ordinal)
$rollbackIndex = $starter.IndexOf('rollback-if-unhealthy', $rollbackBranchIndex, [StringComparison]::Ordinal)
$watchdogWaitIndex = $starter.IndexOf('$launcher.WaitForExit(5000)', $rollbackBranchIndex, [StringComparison]::Ordinal)
$staleLauncherStopIndex = $starter.IndexOf('Stop-Process -Id $launcher.Id', $watchdogWaitIndex, [StringComparison]::Ordinal)
$failedVersionStopIndex = $starter.IndexOf('A broken executable can stay alive', [StringComparison]::Ordinal)
$recoveryLaunchIndex = $starter.IndexOf('$recoveryArguments = @(', [StringComparison]::Ordinal)
if ($deferredFailureIndex -lt 0 -or $deferredExactStopIndex -le $deferredFailureIndex -or
    $rollbackBranchIndex -le $deferredExactStopIndex -or $watchdogWaitIndex -le $rollbackBranchIndex -or
    $staleLauncherStopIndex -le $watchdogWaitIndex -or $rollbackIndex -le $staleLauncherStopIndex -or
    $failedVersionStopIndex -le $rollbackIndex -or
    $recoveryLaunchIndex -le $failedVersionStopIndex) {
  throw "Rollback startup must retire the stale watchdog before rollback, then retire the exact failed version before launching recovery."
}

$watchdogSafetyExitIndex = $starter.IndexOf('$launcher.ExitCode -eq 4', [StringComparison]::Ordinal)
$rollbackBlockedIndex = $starter.IndexOf('if ($rollbackBlocked)', [StringComparison]::Ordinal)
if ($watchdogSafetyExitIndex -lt 0 -or $rollbackBlockedIndex -le $watchdogSafetyExitIndex -or
    $rollbackBranchIndex -le $rollbackBlockedIndex) {
  throw "A watchdog safety or transaction-recovery failure can still be misclassified as a version rollback."
}

if ($recoveryBootstrap -notmatch 'Invoke-DianRecoverInstallTransaction' -or
    $recoveryBootstrap -notmatch 'Invoke-DianRecoverReleaseToolsTransaction' -or
    $recoveryBootstrap -notmatch 'bootstrap\\windows_trust_policy\.ps1|Join-Path\s+\$PSScriptRoot\s+"windows_trust_policy\.ps1"') {
  throw "Stable bootstrap does not recover both durable maintenance journals from its external policy."
}

foreach ($name in @("start_agent.ps1", "watchdog_release.ps1", "recovery_bootstrap.ps1", "repair_agent.ps1", "windows_trust_policy.ps1")) {
  if ($sync -notmatch [regex]::Escape('"' + $name + '"')) {
    throw "Transactional release-tools synchronization omits $name."
  }
  if ($releaseCoreBuilder -notmatch [regex]::Escape('"' + $name + '"')) {
    throw "Windows release packaging omits $name."
  }
}

if ($releaseBuilder -notmatch 'build_release_core\.ps1') {
  throw "Verified public release wrapper does not delegate packaging to build_release_core.ps1."
}

# Real-process regression: an exact current-version executable that remains
# alive without binding must be replaced, not mistaken for perpetual startup.
$prebindSandbox = Join-Path ([IO.Path]::GetTempPath()) ("dian-watchdog-prebind-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))
$prebindVersion = "9.9.9"
$prebindAgentDir = Join-Path $prebindSandbox "app\$prebindVersion"
$prebindAgentPath = Join-Path $prebindAgentDir "DianAgent.exe"
$stalledAgent = $null
try {
  New-Item -ItemType Directory -Force -Path $prebindAgentDir | Out-Null
  Set-Content -LiteralPath (Join-Path $prebindSandbox "current-version.txt") -Encoding ASCII -Value $prebindVersion
  Add-Type -TypeDefinition @'
using System.Threading;
public static class DianWatchdogPrebindStub {
  public static void Main() { Thread.Sleep(120000); }
}
'@ -Language CSharp -OutputAssembly $prebindAgentPath -OutputType ConsoleApplication

  $portLease = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
  $portLease.Start()
  $prebindPort = ([Net.IPEndPoint]$portLease.LocalEndpoint).Port
  $portLease.Stop()

  $stalledAgent = Start-Process -FilePath $prebindAgentPath -WindowStyle Hidden -PassThru
  Start-Sleep -Milliseconds 300
  if ($stalledAgent.HasExited) { throw "Pre-bind regression stub exited before the watchdog test." }

  $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -Port {2} -StartupTimeoutSeconds 4 -FailureThreshold 3 -FailureProbeIntervalMilliseconds 100 -RecoveryGraceMilliseconds 500' -f `
    $watchdogPath, $prebindSandbox, $prebindPort

  # Hold the exact installer mutex and prove that keepalive reports maintenance
  # instead of stopping or replacing a process while program trees may be in
  # the middle of an atomic switch.
  $maintenanceSha = [Security.Cryptography.SHA256]::Create()
  try {
    $maintenanceHash = [BitConverter]::ToString($maintenanceSha.ComputeHash(
      [Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($prebindSandbox).TrimEnd('\').ToUpperInvariant()))
    )).Replace("-", "").Substring(0, 16)
  } finally {
    $maintenanceSha.Dispose()
  }
  $maintenanceMutex = New-Object Threading.Mutex($false, "Local\DianAgentMaintenance-$maintenanceHash")
  $maintenanceMutex.WaitOne() | Out-Null
  try {
    $maintenanceProbe = Start-Process -FilePath $powershell -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $maintenanceProbe.WaitForExit(10000)) {
      Stop-Process -Id $maintenanceProbe.Id -Force -ErrorAction SilentlyContinue
      throw "Watchdog did not leave a bounded installer maintenance window."
    }
    if ($maintenanceProbe.ExitCode -ne 0 -or -not (Get-Process -Id $stalledAgent.Id -ErrorAction SilentlyContinue)) {
      throw "Watchdog changed the Agent process while installer maintenance was locked."
    }
    $maintenanceState = Get-Content -LiteralPath (Join-Path $prebindSandbox "data\runtime\startup-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($maintenanceState.state -ne "maintenance" -or $maintenanceState.last_error -ne "maintenance_in_progress") {
      throw "Watchdog did not publish a truthful maintenance state while install was locked."
    }
  } finally {
    $maintenanceMutex.ReleaseMutex()
    $maintenanceMutex.Dispose()
  }

  # Start a fresh process after the maintenance check so this assertion has a
  # precise CreationDate. The watchdog must leave it alive during the configured
  # cold-start budget, then replace it once that budget and health probes expire.
  Stop-Process -Id $stalledAgent.Id -Force -ErrorAction SilentlyContinue
  $stalledAgent.WaitForExit(5000) | Out-Null
  $stalledAgent = Start-Process -FilePath $prebindAgentPath -WindowStyle Hidden -PassThru
  Start-Sleep -Milliseconds 100
  if ($stalledAgent.HasExited) { throw "Fresh pre-bind regression stub exited before the cold-start test." }

  # Start-Process -Wait also waits for descendant processes on Windows, which
  # would make this regression wait for the deliberately hung replacement.
  $probe = Start-Process -FilePath $powershell -ArgumentList $arguments -WindowStyle Hidden -PassThru
  Start-Sleep -Milliseconds 1500
  if ($probe.HasExited -or -not (Get-Process -Id $stalledAgent.Id -ErrorAction SilentlyContinue)) {
    throw "Watchdog replaced a fresh exact-path Agent before its cold-start budget expired."
  }
  if (-not $probe.WaitForExit(60000)) {
    Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue
    throw "Pre-bind watchdog smoke exceeded its bounded recovery window."
  }
  if ($probe.ExitCode -ne 1) {
    throw "Pre-bind watchdog smoke returned $($probe.ExitCode); the non-listening replacement should still fail health."
  }
  if (Get-Process -Id $stalledAgent.Id -ErrorAction SilentlyContinue) {
    throw "Watchdog left the original exact-path pre-bind process alive."
  }
  $replacement = @(
    Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
      Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq [IO.Path]::GetFullPath($prebindAgentPath)
      }
  )
  if ($replacement.Count -ne 1 -or [int]$replacement[0].ProcessId -eq $stalledAgent.Id) {
    throw "Watchdog did not launch exactly one replacement for the stalled pre-bind Agent."
  }
} finally {
  if (Test-Path -LiteralPath $prebindAgentPath -PathType Leaf) {
    $exactPath = [IO.Path]::GetFullPath($prebindAgentPath)
    Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $exactPath } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  if (Test-Path -LiteralPath $prebindSandbox) {
    $cleanupDeadline = (Get-Date).AddSeconds(5)
    do {
      try {
        Remove-Item -LiteralPath $prebindSandbox -Recurse -Force -ErrorAction Stop
        break
      } catch {
        if ((Get-Date) -ge $cleanupDeadline) { throw }
        Start-Sleep -Milliseconds 100
      }
    } while ($true)
  }
}

Write-Host "Watchdog reliability and release synchronization contract passed." -ForegroundColor Green
