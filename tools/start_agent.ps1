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

$watchdog = Join-Path $InstallRoot "tools\watchdog_release.ps1"
if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf)) { throw "The Dian Agent launcher is incomplete." }
$healthUrl = "http://127.0.0.1:$Port/health"

function Test-AgentHealth {
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    return ($health.status -eq "ok" -and [string]$health.version -eq $version)
  } catch {
    return $false
  }
}

function Complete-PendingUpgradeFromHealth {
  if (-not $script:pendingUpgrade -or $DeferPendingConfirmation) { return }
  & $UpdaterPath recover --install-root $InstallRoot --health-url $healthUrl
  if ($LASTEXITCODE -ne 0) {
    throw "The Agent is healthy, but its interrupted upgrade transaction could not be confirmed safely."
  }
  $script:pendingUpgrade = $false
  Write-Host "Interrupted offline upgrade was confirmed from exact local health evidence." -ForegroundColor Green
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

if ($pendingUpgrade -and -not $DeferPendingConfirmation -and -not $RecoveryAttempted) {
  Write-Warning "The pending new version did not become healthy; restoring the previous version."
  & $UpdaterPath recover --install-root $InstallRoot --health-url $healthUrl --rollback-if-unhealthy
  if ($LASTEXITCODE -ne 0) {
    throw "Pending upgrade recovery failed after startup error: $($startupFailure.Exception.Message)"
  }
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
