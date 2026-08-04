param(
  [string]$InstallRoot = "",
  [int]$Port = 8765,
  [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 60,
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
$healthUrl = "http://127.0.0.1:$Port/health"

function Test-AgentHealth {
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    return ($health.status -eq "ok" -and [string]$health.version -eq $version)
  } catch {
    return $false
  }
}

if (Test-AgentHealth) {
  Write-StartupState "healthy" "Autostart is healthy; Agent is already running"
  exit 0
}

$sha = [Security.Cryptography.SHA256]::Create()
try {
  $rootHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallRoot))).Replace("-", "").Substring(0, 16)
} finally {
  $sha.Dispose()
}
$watchdogMutex = New-Object Threading.Mutex($false, "Local\DianAgentWatchdog-$rootHash")
if (-not $watchdogMutex.WaitOne(0)) { exit 0 }

# Only stop a stale listener when its executable is inside this exact install.
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
  $process = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $listener.OwningProcess) -ErrorAction SilentlyContinue
  if (-not $process -or -not $process.ExecutablePath) {
    Write-StartupState "error" "The Agent port is owned by an unknown process" "port_owned_by_unknown_process"
    exit 3
  }
  $processPath = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
  $ownedAppRoot = $InstallRoot.TrimEnd('\') + '\'
  if (-not $processPath.StartsWith($ownedAppRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-StartupState "error" "The Agent port is owned by another application" "port_owned_by_other_application"
    exit 3
  }
  Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
}

$alreadyStarting = Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
  Where-Object {
    $_.ExecutablePath -and
    ([IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $agentPath)
  }

if (-not $alreadyStarting) {
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
