param(
  [string]$InstallRoot = "",
  [int]$Port = 8765,
  [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 60,
  [ValidateRange(3, 10)][int]$FailureThreshold = 3,
  [ValidateRange(100, 5000)][int]$FailureProbeIntervalMilliseconds = 750,
  [ValidateRange(500, 15000)][int]$RecoveryGraceMilliseconds = 2500,
  [switch]$Launch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$runtimeDir = Join-Path $InstallRoot "data\runtime"
$startupStatePath = Join-Path $runtimeDir "startup-state.json"

function Write-StartupState([string]$State, [string]$Label, [string]$ErrorMessage = "", [switch]$Recovered) {
  New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
  $previous = $null
  if (Test-Path -LiteralPath $startupStatePath -PathType Leaf) {
    try { $previous = Get-Content -LiteralPath $startupStatePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
  }
  $now = [DateTime]::UtcNow.ToString("o")
  $lastHealthyAt = if ($State -eq "healthy") { $now } elseif ($previous) { $previous.last_healthy_at } else { $null }
  $lastRecoveryAt = if ($Recovered) { $now } elseif ($previous) { $previous.last_recovery_at } else { $null }
  $lastError = if ($ErrorMessage) { $ErrorMessage.Substring(0, [Math]::Min(300, $ErrorMessage.Length)) } else { $null }
  $payload = [ordered]@{
    schema_version = 1
    state = $State
    state_label = $Label
    autostart_enabled = $true
    keepalive_enabled = $true
    hidden_launcher = $true
    source = "release_watchdog"
    task_name = "DianAgentKeepAlive"
    last_checked_at = $now
    last_healthy_at = $lastHealthyAt
    last_recovery_at = $lastRecoveryAt
    last_error = $lastError
  }
  $temporary = Join-Path $runtimeDir (".startup-state-{0}.tmp" -f $PID)
  $payload | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $startupStatePath -Force
}

$trustPolicyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $trustPolicyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $trustPolicyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  # Without the shared path policy this process cannot prove that even the
  # runtime status destination is safe. Fail without writing through it.
  exit 4
}
. $trustPolicyPath
Assert-DianPathChainNoReparsePoints $InstallRoot "Installation root"
$rootHash = (Get-DianMaintenanceMutexName $InstallRoot).Substring("Local\DianAgentMaintenance-".Length)
$maintenanceMutex = $null
$ownsMaintenanceMutex = $false
$maintenanceMutex = New-Object Threading.Mutex($false, "Local\DianAgentMaintenance-$rootHash")
try {
  try {
    $ownsMaintenanceMutex = $maintenanceMutex.WaitOne(0)
  } catch [Threading.AbandonedMutexException] {
    $ownsMaintenanceMutex = $true
  }
  if (-not $ownsMaintenanceMutex) {
    Write-StartupState "maintenance" "Agent update or repair is in progress" "maintenance_in_progress"
    exit 0
  }
  try {
    $transactionRecovery = Invoke-DianRecoverInstallTransaction $InstallRoot 1
    $toolsTransactionRecovery = Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1
  } catch {
    Write-StartupState "error" "Interrupted installation requires manual repair" "install_transaction_recovery_failed"
    exit 4
  }
} catch {
  $maintenanceMutex.Dispose()
  throw
}

$currentPointer = Join-Path $InstallRoot "current.json"
$versionFile = Join-Path $InstallRoot "current-version.txt"
if (Test-Path -LiteralPath $currentPointer -PathType Leaf) {
  try {
    $current = Get-Content -LiteralPath $currentPointer -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$current.version
    $versionRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot ([string]$current.version_path)))
    $installPrefix = $InstallRoot.TrimEnd('\') + '\'
    if (-not $versionRoot.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { exit 2 }
    $agentPath = Join-Path $versionRoot "program\DianAgent.exe"
  } catch { exit 2 }
} else {
  if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) { exit 2 }
  $version = (Get-Content -LiteralPath $versionFile -Raw -Encoding ASCII).Trim()
  $agentPath = Join-Path $InstallRoot ("app\{0}\DianAgent.exe" -f $version)
}
if ($version -notmatch '^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$') { exit 2 }
if (-not (Test-Path -LiteralPath $agentPath -PathType Leaf)) { exit 2 }
$agentPath = [IO.Path]::GetFullPath($agentPath)
$healthUrl = "http://127.0.0.1:$Port/health/live"
$legacyHealthUrl = "http://127.0.0.1:$Port/health"

function Invoke-AgentHealthProbe([string]$Uri, [int]$TimeoutSeconds) {
  try {
    $health = Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSeconds
    return [pscustomobject]@{
      Healthy = ($health.status -eq "ok" -and [string]$health.version -eq $version)
      Unsupported = $false
    }
  } catch {
    $statusCode = 0
    if ($_.Exception.Response) {
      try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { }
    }
    return [pscustomobject]@{
      Healthy = $false
      Unsupported = ($statusCode -eq 404)
    }
  }
}

function Test-AgentHealth {
  $live = Invoke-AgentHealthProbe $healthUrl 2
  if ($live.Healthy) { return (Test-ExpectedAgentListener) }
  if (-not $live.Unsupported) { return $false }
  # Only an explicit 404 proves that the active rollback target predates the
  # liveness endpoint. Ordinary slowness must never fan out into legacy probes.
  $legacy = Invoke-AgentHealthProbe $legacyHealthUrl 5
  return ($legacy.Healthy -and (Test-ExpectedAgentListener))
}

function Get-PortListeners {
  return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-ListenerOwner([object]$Listener) {
  $ownerPid = [int]$Listener.OwningProcess
  $process = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ownerPid) -ErrorAction SilentlyContinue
  if (-not $process -or -not $process.ExecutablePath) { return $null }
  try {
    $processPath = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
  } catch {
    return $null
  }
  return [pscustomobject]@{
    ProcessId = $ownerPid
    ExecutablePath = $processPath
  }
}

function Test-ExpectedAgentListener {
  $listeners = @(Get-PortListeners)
  if ($listeners.Count -eq 0) { return $false }
  foreach ($listener in $listeners) {
    $owner = Get-ListenerOwner $listener
    if (-not $owner -or
        -not $owner.ExecutablePath.Equals($agentPath, [StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }
  }
  return $true
}

function Get-ExactAgentProcesses {
  return @(
    Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
      Where-Object {
        $_.ExecutablePath -and
        ([IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $agentPath)
      }
  )
}

function Get-ProcessCreationTimeUtc([object]$Process) {
  if (-not $Process -or
      -not $Process.PSObject.Properties["CreationDate"] -or
      -not $Process.CreationDate) {
    return $null
  }
  if ($Process.CreationDate -is [DateTime]) {
    return ([DateTime]$Process.CreationDate).ToUniversalTime()
  }
  try {
    return [Management.ManagementDateTimeConverter]::ToDateTime([string]$Process.CreationDate).ToUniversalTime()
  } catch {
    try { return [DateTime]::Parse([string]$Process.CreationDate).ToUniversalTime() } catch { return $null }
  }
}

function Wait-ForExactAgentStartupBudget([object[]]$InitialProcesses) {
  $initialPids = @($InitialProcesses | ForEach-Object { [int]$_.ProcessId })
  $creationTimes = @()
  foreach ($process in $InitialProcesses) {
    $createdAt = Get-ProcessCreationTimeUtc $process
    if (-not $createdAt) { return "CreationTimeUnknown" }
    $creationTimes += $createdAt
  }
  if ($creationTimes.Count -eq 0) { return "Exited" }

  # Multiple exact processes are abnormal, but the youngest still owns a full
  # cold-start allowance. PID refreshes below prevent that grace from being
  # transferred silently to a different process.
  $startupDeadlineUtc = ($creationTimes | Sort-Object -Descending | Select-Object -First 1).AddSeconds($StartupTimeoutSeconds)
  while ([DateTime]::UtcNow -lt $startupDeadlineUtc) {
    if (Test-AgentHealth) { return "Healthy" }
    $currentProcesses = @(Get-ExactAgentProcesses)
    if (@($currentProcesses | Where-Object { $initialPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
      return "IdentityChanged"
    }
    if ($currentProcesses.Count -eq 0) { return "Exited" }
    $remainingMilliseconds = [Math]::Max(1, [int][Math]::Ceiling(($startupDeadlineUtc - [DateTime]::UtcNow).TotalMilliseconds))
    Start-Sleep -Milliseconds ([Math]::Min(500, $remainingMilliseconds))
  }
  return "Expired"
}

function Test-OwnedAgentPath([string]$ProcessPath) {
  if (-not $ProcessPath -or -not ([IO.Path]::GetFileName($ProcessPath)).Equals("DianAgent.exe", [StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }
  $versionsPrefix = [IO.Path]::GetFullPath((Join-Path $InstallRoot "versions")).TrimEnd('\') + '\'
  $legacyAppPrefix = [IO.Path]::GetFullPath((Join-Path $InstallRoot "app")).TrimEnd('\') + '\'
  return (
    $ProcessPath.StartsWith($versionsPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $ProcessPath.StartsWith($legacyAppPrefix, [StringComparison]::OrdinalIgnoreCase)
  )
}

function Confirm-ListeningAgentUnhealthy {
  # A single two-second timeout is only a weak signal.  Require a fresh run of
  # consecutive failures, then leave a short grace window for a busy Agent to
  # recover before considering a restart.
  for ($failure = 1; $failure -le $FailureThreshold; $failure++) {
    if (Test-AgentHealth) { return $false }
    if ($failure -lt $FailureThreshold) {
      Start-Sleep -Milliseconds $FailureProbeIntervalMilliseconds
    }
  }
  Write-StartupState "checking" "Agent is slow to respond; checking again before recovery" "consecutive_health_failures:$FailureThreshold"
  Start-Sleep -Milliseconds $RecoveryGraceMilliseconds
  return (-not (Test-AgentHealth))
}

if (Test-AgentHealth) {
  Write-StartupState "healthy" "Autostart is healthy; Agent is already running"
  exit 0
}

# Keep the maintenance mutex for the lifetime of this watchdog process. This
# serializes stop/start recovery with installer directory switches. Every exit
# below terminates the process, so Windows releases the kernel mutex even after
# an abrupt failure.
$watchdogMutex = New-Object Threading.Mutex($false, "Local\DianAgentWatchdog-$rootHash")
if (-not $watchdogMutex.WaitOne(0)) { exit 0 }

# Validate every current listener before recovery.  Unknown and unrelated
# owners are never stopped.
$listeners = @(Get-PortListeners)
$ownedListenerPids = @()
foreach ($listener in $listeners) {
  $owner = Get-ListenerOwner $listener
  if (-not $owner) {
    Write-StartupState "error" "The Agent port is owned by an unknown process" "port_owned_by_unknown_process"
    exit 3
  }
  if (-not (Test-OwnedAgentPath $owner.ExecutablePath)) {
    Write-StartupState "error" "The Agent port is owned by another application" "port_owned_by_other_application"
    exit 3
  }
  $ownedListenerPids += [int]$owner.ProcessId
}

if ($listeners.Count -gt 0) {
  $listenerProcesses = @(Get-ExactAgentProcesses)
  $listenerStartupBudget = Wait-ForExactAgentStartupBudget $listenerProcesses
  if ($listenerStartupBudget -eq "Healthy") {
    Write-StartupState "healthy" "Autostart is healthy; Agent completed its cold start"
    exit 0
  }
  if ($listenerStartupBudget -eq "IdentityChanged") {
    Write-StartupState "checking" "The Agent process changed during cold start" "listener_process_identity_changed"
    exit 0
  }
  if ($listenerStartupBudget -eq "CreationTimeUnknown") {
    Write-StartupState "checking" "The Agent startup age could not be verified" "listener_creation_time_unknown"
    exit 0
  }

  if (-not (Confirm-ListeningAgentUnhealthy)) {
    Write-StartupState "healthy" "Autostart is healthy; Agent recovered during the grace check"
    exit 0
  }

  # The evidence above can become stale while probes run.  Immediately before
  # stopping anything, prove that the same port is still listening, its owner
  # is still alive, and the executable is still this installation's Agent.
  $refreshedListeners = @(Get-PortListeners)
  $confirmedStopPids = @()
  foreach ($listener in $refreshedListeners) {
    $owner = Get-ListenerOwner $listener
    if (-not $owner) {
      Write-StartupState "error" "The Agent port owner changed during recovery" "port_owner_changed_or_exited"
      exit 3
    }
    if (-not (Test-OwnedAgentPath $owner.ExecutablePath)) {
      Write-StartupState "error" "The Agent port was taken by another application" "port_owner_changed_to_other_application"
      exit 3
    }
    $liveProcess = Get-Process -Id $owner.ProcessId -ErrorAction SilentlyContinue
    if (-not $liveProcess) {
      Write-StartupState "checking" "The previous Agent exited; waiting for the port to settle" "listener_owner_exited"
      continue
    }
    if ($ownedListenerPids -notcontains [int]$owner.ProcessId) {
      Write-StartupState "error" "The Agent port owner changed during recovery" "port_owner_pid_changed"
      exit 3
    }
    $confirmedStopPids += [int]$owner.ProcessId
  }

  # One last live probe closes the race between ownership verification and the
  # stop operation.  This makes a recovered service win over watchdog action.
  if (Test-AgentHealth) {
    Write-StartupState "healthy" "Autostart is healthy; Agent recovered before restart"
    exit 0
  }

  # The last probe can itself take up to two seconds.  Refresh ownership once
  # more afterwards so no PID or released port is acted on from stale evidence.
  $finalStopPids = @()
  foreach ($listener in @(Get-PortListeners)) {
    $owner = Get-ListenerOwner $listener
    if (-not $owner -or -not (Test-OwnedAgentPath $owner.ExecutablePath)) {
      Write-StartupState "error" "The Agent port owner changed immediately before restart" "final_port_owner_mismatch"
      exit 3
    }
    if ($confirmedStopPids -notcontains [int]$owner.ProcessId -or
        -not (Get-Process -Id $owner.ProcessId -ErrorAction SilentlyContinue)) {
      Write-StartupState "error" "The Agent process changed immediately before restart" "final_process_identity_mismatch"
      exit 3
    }
    $finalStopPids += [int]$owner.ProcessId
  }
  foreach ($ownerPid in @($finalStopPids | Select-Object -Unique)) {
    Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
  }

  $portReleaseDeadline = (Get-Date).AddSeconds(5)
  while ((Get-Date) -lt $portReleaseDeadline -and @(Get-PortListeners).Count -gt 0) {
    Start-Sleep -Milliseconds 200
  }
  if (@(Get-PortListeners).Count -gt 0) {
    Write-StartupState "error" "The previous Agent could not release its port" "recovery_port_not_released"
    exit 1
  }
}

# A process can survive before binding the port (for example a frozen one-file
# bootloader).  The former code treated its mere existence as proof that a
# start was already in progress, so every scheduled watchdog run waited and
# failed forever.  Require the same grace evidence used for a listening Agent,
# then retire only the exact current-version executable after one final probe.
$alreadyStarting = @(Get-ExactAgentProcesses)
if ($listeners.Count -eq 0 -and $alreadyStarting.Count -gt 0) {
  $initialPrebindPids = @($alreadyStarting | ForEach-Object { [int]$_.ProcessId })
  $startupBudgetResult = Wait-ForExactAgentStartupBudget $alreadyStarting
  if ($startupBudgetResult -eq "Healthy") {
    Write-StartupState "healthy" "Autostart is healthy; Agent recovered while starting"
    exit 0
  }
  if ($startupBudgetResult -eq "IdentityChanged") {
    Write-StartupState "checking" "The starting Agent process changed during its startup budget" "prebind_process_identity_changed"
    exit 0
  }
  if ($startupBudgetResult -eq "CreationTimeUnknown") {
    Write-StartupState "checking" "The starting Agent age could not be verified" "prebind_creation_time_unknown"
    exit 0
  }
  if ($startupBudgetResult -eq "Exited") {
    $alreadyStarting = @()
  }

  if (-not (Confirm-ListeningAgentUnhealthy)) {
    Write-StartupState "healthy" "Autostart is healthy; Agent recovered while starting"
    exit 0
  }
  $stalledProcesses = @(Get-ExactAgentProcesses)
  if (@($stalledProcesses | Where-Object { $initialPrebindPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
    Write-StartupState "checking" "The starting Agent process changed during recovery" "prebind_process_identity_changed"
    exit 0
  }
  if (Test-AgentHealth) {
    Write-StartupState "healthy" "Autostart is healthy; Agent recovered before replacement"
    exit 0
  }
  $finalStalledProcesses = @(Get-ExactAgentProcesses)
  if (@($finalStalledProcesses | Where-Object { $initialPrebindPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
    Write-StartupState "checking" "A new Agent process started before replacement" "prebind_process_identity_changed"
    exit 0
  }
  foreach ($stalledProcess in $finalStalledProcesses) {
    Stop-Process -Id ([int]$stalledProcess.ProcessId) -Force -ErrorAction SilentlyContinue
  }
  $stalledExitDeadline = (Get-Date).AddSeconds(5)
  while ((Get-Date) -lt $stalledExitDeadline -and @(Get-ExactAgentProcesses).Count -gt 0) {
    Start-Sleep -Milliseconds 200
  }
  if (@(Get-ExactAgentProcesses).Count -gt 0) {
    Write-StartupState "error" "The stalled Agent process could not be replaced" "stalled_process_not_released"
    exit 1
  }
  Write-StartupState "recovering" "Replacing an Agent process that stalled before opening its port" "prebind_process_stalled" -Recovered
  $alreadyStarting = @()
}

if ($alreadyStarting.Count -eq 0) {
  $env:DIAN_AGENT_DATA_DIR = Join-Path $InstallRoot "data"
  $env:DIAN_AGENT_LOG_DIR = Join-Path $InstallRoot "logs"
  $env:BRIDGE_PORT = [string]$Port
  Start-Process -FilePath $agentPath -WorkingDirectory (Split-Path -Parent $agentPath) -WindowStyle Hidden
}

for ($attempt = 0; $attempt -lt ($StartupTimeoutSeconds * 2); $attempt++) {
  Start-Sleep -Milliseconds 500
  if (Test-AgentHealth) {
    Write-StartupState "healthy" "Agent recovered automatically" "" -Recovered
    exit 0
  }
}
Write-StartupState "error" "Automatic recovery failed; open diagnostics" "startup_timeout"
exit 1
