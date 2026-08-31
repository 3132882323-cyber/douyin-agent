[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [switch]$SkipLaunch,
  [int]$Port = 8765,
  [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 60,
  [string]$UpdaterPath = "",
  [switch]$DeferPendingConfirmation,
  [switch]$RecoveryAttempted,
  [switch]$ApplyMaintenance,
  [ValidateRange(0, 100)][int]$KeepRecentVersions = 2,
  [ValidateRange(0, 87600)][int]$MaintenanceAgeHours = 168
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$trustPolicyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $trustPolicyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $trustPolicyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "The Windows maintenance policy component is missing or unsafe; startup is blocked."
}
. $trustPolicyPath
Assert-DianPathChainNoReparsePoints $InstallRoot "Installation root"
$transactionRecovery = Invoke-DianRecoverInstallTransaction $InstallRoot 30
if ($transactionRecovery.Recovered) {
  Write-Host ("Recovered interrupted install transaction {0} ({1}) before startup." -f $transactionRecovery.TransactionId, $transactionRecovery.Action) -ForegroundColor Yellow
}
$toolsTransactionRecovery = Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 30
if ($toolsTransactionRecovery.Recovered) {
  Write-Host ("Recovered interrupted maintenance-tools transaction {0} ({1}) before startup." -f $toolsTransactionRecovery.TransactionId, $toolsTransactionRecovery.Action) -ForegroundColor Yellow
}
if ($SkipLaunch) {
  Write-Host "Initial launch was skipped."
  exit 0
}

if (-not $UpdaterPath) { $UpdaterPath = Join-Path $InstallRoot "tools\DianAgentUpdater.exe" }
$UpdaterPath = [IO.Path]::GetFullPath($UpdaterPath)
$pendingPath = Join-Path $InstallRoot ".offline-upgrade-rollback"
$pendingUpgrade = Test-Path -LiteralPath $pendingPath -PathType Container
if ($pendingUpgrade -and -not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf)) {
  throw "An offline upgrade recovery is pending, but DianAgentUpdater.exe is missing."
}

$currentPointer = Join-Path $InstallRoot "current.json"
$versionFile = Join-Path $InstallRoot "current-version.txt"
$agentPath = $null
if (Test-Path -LiteralPath $currentPointer -PathType Leaf) {
  $current = Get-Content -LiteralPath $currentPointer -Raw -Encoding UTF8 | ConvertFrom-Json
  $version = [string]$current.version
  $versionRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot ([string]$current.version_path)))
  $installPrefix = $InstallRoot.TrimEnd('\') + '\'
  if (-not $versionRoot.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Active version pointer is unsafe." }
  $agentPath = Join-Path $versionRoot "program\DianAgent.exe"
} else {
  if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) { throw "Dian Agent is not installed at: $InstallRoot" }
  $version = (Get-Content -LiteralPath $versionFile -Raw -Encoding ASCII).Trim()
  $agentPath = Join-Path $InstallRoot ("app\{0}\DianAgent.exe" -f $version)
}
if ($version -notmatch '^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$') { throw "Installed version is invalid." }
$agentPath = [IO.Path]::GetFullPath($agentPath)

$watchdog = Join-Path $InstallRoot "tools\watchdog_release.ps1"
if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf)) { throw "The Dian Agent launcher is incomplete." }
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
  # Compatibility is intentionally limited to an explicit 404 from a
  # pre-v4.11 Agent. Timeouts, connection failures and server errors never
  # fall back to the heavier legacy endpoint.
  $legacy = Invoke-AgentHealthProbe $legacyHealthUrl 5
  return ($legacy.Healthy -and (Test-ExpectedAgentListener))
}

function Test-ExpectedAgentListener {
  # Version strings are not process identity. Another installation can expose
  # the same version on the shared port, so health is accepted only when every
  # listener is owned by this exact active executable.
  $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
  if ($listeners.Count -eq 0) { return $false }
  foreach ($listener in $listeners) {
    $ownerPid = [int]$listener.OwningProcess
    $owner = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ownerPid) -ErrorAction SilentlyContinue
    if (-not $owner -or -not $owner.ExecutablePath) { return $false }
    try {
      $ownerPath = [IO.Path]::GetFullPath([string]$owner.ExecutablePath)
    } catch {
      return $false
    }
    if (-not $ownerPath.Equals($agentPath, [StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }
  }
  return $true
}

function Complete-PendingUpgradeFromHealth {
  if (-not $script:pendingUpgrade -or $DeferPendingConfirmation) { return }
  & $UpdaterPath recover --install-root $InstallRoot --health-url $healthUrl
  if ($LASTEXITCODE -ne 0) {
    throw "The Agent is healthy, but its interrupted upgrade transaction could not be confirmed safely."
  }
  if (Test-Path -LiteralPath $pendingPath -PathType Container) {
    Write-Warning "The upgraded Agent is healthy, but rollback evidence is retained until the target browser extension reloads and authenticates."
    return
  }
  $script:pendingUpgrade = $false
  Write-Host "Interrupted offline upgrade was confirmed from exact process health and fresh extension evidence." -ForegroundColor Green
}

function Invoke-ConservativeMaintenance {
  if (-not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf)) { return }
  $marker = Join-Path $InstallRoot "logs\last-offline-maintenance.txt"
  if (Test-Path -LiteralPath $marker -PathType Leaf) {
    try {
      if (((Get-Date) - (Get-Item -LiteralPath $marker).LastWriteTime).TotalHours -lt 24) { return }
    } catch { }
  }
  $arguments = @(
    "cleanup", "--install-root", $InstallRoot,
    "--keep-recent", [string]$KeepRecentVersions,
    "--min-age-hours", [string]$MaintenanceAgeHours
  )
  if ($ApplyMaintenance) { $arguments += "--apply" }
  & $UpdaterPath @arguments | Out-Null
  if ($LASTEXITCODE -eq 0) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marker) | Out-Null
    Set-Content -LiteralPath $marker -Encoding ASCII -Value ([DateTime]::UtcNow.ToString("o"))
  } else {
    Write-Warning "Offline upgrade maintenance check failed; no startup files were removed."
  }
}

if (Test-AgentHealth) {
  Complete-PendingUpgradeFromHealth
  Invoke-ConservativeMaintenance
  Write-Host "Dian Agent $version is already running." -ForegroundColor Green
  exit 0
}

$sha = [Security.Cryptography.SHA256]::Create()
try {
  $rootHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallRoot))).Replace("-", "").Substring(0, 16)
} finally {
  $sha.Dispose()
}
$mutex = New-Object Threading.Mutex($false, "Local\DianAgentStart-$rootHash")
$ownsMutex = $false
$launcher = $null
$startupFailure = $null
$rollbackBlocked = $false
try {
  $ownsMutex = $mutex.WaitOne(0)
  if ($ownsMutex) {
    Write-Host "Starting Dian Agent $version..."
    $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $watchdogArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -Port {2} -StartupTimeoutSeconds {3}' -f $watchdog, $InstallRoot, $Port, $StartupTimeoutSeconds
    $launcher = Start-Process -FilePath $powershell -ArgumentList $watchdogArguments -WindowStyle Hidden -PassThru
  } else {
    Write-Host "Another Dian Agent start is already in progress; waiting for it to finish..."
  }

  $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
  $attempt = 0
  while ((Get-Date) -lt $deadline) {
    $attempt++
    $remaining = [Math]::Max(0, [int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds))
    $percent = [Math]::Min(99, [int](100 * ($StartupTimeoutSeconds - $remaining) / $StartupTimeoutSeconds))
    Write-Progress -Activity "Starting Dian Agent" -Status "Waiting for local service (up to $remaining seconds)" -PercentComplete $percent
    if (Test-AgentHealth) {
      Write-Progress -Activity "Starting Dian Agent" -Completed
      Complete-PendingUpgradeFromHealth
      Invoke-ConservativeMaintenance
      Write-Host "Dian Agent $version is ready at $healthUrl" -ForegroundColor Green
      exit 0
    }
    if ($launcher -and $launcher.HasExited) {
      if ($launcher.ExitCode -eq 2) { throw "The installed Agent files are incomplete." }
      if ($launcher.ExitCode -eq 3) { throw "Port $Port is already in use by another application." }
      if ($launcher.ExitCode -eq 4) {
        $rollbackBlocked = $true
        throw "The Agent startup safety policy or interrupted-install recovery failed; version rollback is blocked until the installation is repaired."
      }
    }
    Start-Sleep -Seconds 1
  }
  Write-Progress -Activity "Starting Dian Agent" -Completed
  throw "Dian Agent did not become healthy within $StartupTimeoutSeconds seconds. Check $(Join-Path $InstallRoot 'logs')."
} catch {
  $startupFailure = $_
} finally {
  if ($ownsMutex) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}

if ($rollbackBlocked) {
  throw $startupFailure
}

if ($pendingUpgrade -and $DeferPendingConfirmation) {
  # The wrapper owns rollback in deferred mode, but this starter still owns the
  # attempted target process. Retire its watchdog and exact executable before
  # returning failure so rollback cannot leave two Agent generations alive.
  if ($launcher -and -not $launcher.HasExited) {
    if (-not $launcher.WaitForExit(5000)) {
      Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
      [void]$launcher.WaitForExit(2000)
    }
  }
  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.ExecutablePath -and
      ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).Equals($agentPath, [StringComparison]::OrdinalIgnoreCase)
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  throw $startupFailure
}

if ($pendingUpgrade -and -not $DeferPendingConfirmation -and -not $RecoveryAttempted) {
  Write-Warning "The pending new version did not become healthy; restoring the previous version."
  # The first watchdog may still own the per-install mutex for a few moments
  # after both startup loops reach their deadline.  Wait for that exact child
  # instead of launching a recovery watchdog that immediately exits behind the
  # stale mutex and leaves the restored Agent stopped.
  if ($launcher -and -not $launcher.HasExited) {
    if (-not $launcher.WaitForExit(5000)) {
      Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
      [void]$launcher.WaitForExit(2000)
    }
  }
  & $UpdaterPath recover --install-root $InstallRoot --health-url $healthUrl --rollback-if-unhealthy
  if ($LASTEXITCODE -ne 0) {
    throw "Pending upgrade recovery failed after startup error: $($startupFailure.Exception.Message)"
  }
  # A broken executable can stay alive without becoming healthy (for example,
  # a one-file bootloader stuck before binding). The rollback decision is now
  # durable, so stop only processes whose executable is the exact failed
  # version path before starting the restored pointer.
  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.ExecutablePath -and
      ([IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $agentPath)
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  $powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $recoveryArguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath,
    "-InstallRoot", $InstallRoot, "-Port", [string]$Port,
    "-StartupTimeoutSeconds", [string]$StartupTimeoutSeconds,
    "-UpdaterPath", $UpdaterPath, "-RecoveryAttempted"
  )
  if ($ApplyMaintenance) { $recoveryArguments += "-ApplyMaintenance" }
  & $powershell @recoveryArguments
  if ($LASTEXITCODE -ne 0) {
    throw "The previous version pointer was restored, but the previous Agent did not become healthy."
  }
  Write-Warning "The interrupted upgrade was rolled back and the previous Agent is healthy."
  exit 4
}

throw $startupFailure
