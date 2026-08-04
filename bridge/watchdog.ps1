param(
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$bridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $bridgeDir ".venv\Scripts\pythonw.exe"
$receiver = Join-Path $bridgeDir "http_receiver.py"
$packagedAgent = Join-Path (Split-Path -Parent $bridgeDir) "app\DianAgent.exe"
$healthUrl = "http://127.0.0.1:$Port/health"
$manifestPath = Join-Path (Split-Path -Parent $bridgeDir) "extension\manifest.json"
$expectedVersion = (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
$runtimeDir = Join-Path $bridgeDir "data\runtime"
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
  [ordered]@{
    schema_version = 1
    state = $State
    state_label = $Label
    autostart_enabled = $true
    keepalive_enabled = $true
    hidden_launcher = $true
    source = "source_development"
    task_name = "DianAgentDevKeepAlive"
    last_checked_at = $now
    last_healthy_at = $lastHealthyAt
    last_recovery_at = $lastRecoveryAt
    last_error = $lastError
  } | ConvertTo-Json | Set-Content -LiteralPath $startupStatePath -Encoding UTF8
}

try {
  $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
  if ($health.status -eq "ok" -and [string]$health.version -eq [string]$expectedVersion) {
    Write-StartupState "healthy" "Source Agent autostart is healthy"
    exit 0
  }
} catch {
  # A failed health probe is the condition this watchdog is designed to repair.
}

$sha = [Security.Cryptography.SHA256]::Create()
try {
  $rootHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($bridgeDir))).Replace("-", "").Substring(0, 16)
} finally {
  $sha.Dispose()
}
$watchdogMutex = New-Object Threading.Mutex($false, "Local\DianAgentDevWatchdog-$rootHash")
if (-not $watchdogMutex.WaitOne(0)) { exit 0 }

if (Test-Path -LiteralPath $packagedAgent) {
  $agentPath = [IO.Path]::GetFullPath($packagedAgent)
  $alreadyStarting = Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $agentPath }
  if (-not $alreadyStarting) {
    Start-Process -FilePath $packagedAgent -WorkingDirectory (Split-Path -Parent $packagedAgent) -WindowStyle Hidden
  }
} else {
  if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-StartupState "error" "Source runtime is missing" "pythonw_not_found"
    exit 2
  }

  $alreadyStarting = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($receiver) }
  if (-not $alreadyStarting) {
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $receiver + '"') -WorkingDirectory $bridgeDir -WindowStyle Hidden
  }
}

for ($attempt = 0; $attempt -lt 60; $attempt++) {
  Start-Sleep -Milliseconds 500
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($health.status -eq "ok" -and [string]$health.version -eq [string]$expectedVersion) {
      Write-StartupState "healthy" "Source Agent recovered automatically" "" -Recovered
      exit 0
    }
  } catch {
    # Retry briefly while Python imports and binds the local port.
  }
}
Write-StartupState "error" "Source Agent recovery failed" "startup_timeout"
exit 1
