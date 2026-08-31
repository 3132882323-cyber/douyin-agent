param(
  [int]$Port = 8765,
  [ValidateRange(1, 300)][int]$StartupTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$bridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $bridgeDir ".venv\Scripts\pythonw.exe"
$receiver = Join-Path $bridgeDir "http_receiver.py"
$packagedAgent = Join-Path (Split-Path -Parent $bridgeDir) "app\DianAgent.exe"
$healthUrl = "http://127.0.0.1:$Port/health/live"
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
  $payload = [ordered]@{
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
  }
  $temporary = Join-Path $runtimeDir (".startup-state-{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  $payload | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $startupStatePath -Force
}

function Test-AgentHealth {
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    return ($health.status -eq "ok" -and [string]$health.version -eq [string]$expectedVersion)
  } catch {
    return $false
  }
}

function Get-ExactRuntimeProcesses {
  if (Test-Path -LiteralPath $packagedAgent) {
    $agentPath = [IO.Path]::GetFullPath($packagedAgent)
    return @(
      Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $agentPath }
    )
  }
  return @(
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine.Contains($receiver) }
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

function Wait-ForExactRuntimeStartupBudget([object[]]$InitialProcesses) {
  $initialPids = @($InitialProcesses | ForEach-Object { [int]$_.ProcessId })
  $creationTimes = @()
  foreach ($process in $InitialProcesses) {
    $createdAt = Get-ProcessCreationTimeUtc $process
    if (-not $createdAt) { return "CreationTimeUnknown" }
    $creationTimes += $createdAt
  }
  if ($creationTimes.Count -eq 0) { return "Exited" }

  # If more than one exact runtime exists, the youngest process owns the full
  # startup allowance. This prevents an older sibling from causing a newly
  # launched runtime to be killed before it can bind.
  $startupDeadlineUtc = ($creationTimes | Sort-Object -Descending | Select-Object -First 1).AddSeconds($StartupTimeoutSeconds)
  while ([DateTime]::UtcNow -lt $startupDeadlineUtc) {
    if (Test-AgentHealth) { return "Healthy" }
    $currentProcesses = @(Get-ExactRuntimeProcesses)
    if (@($currentProcesses | Where-Object { $initialPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
      return "IdentityChanged"
    }
    if ($currentProcesses.Count -eq 0) { return "Exited" }
    $remainingMilliseconds = [Math]::Max(1, [int][Math]::Ceiling(($startupDeadlineUtc - [DateTime]::UtcNow).TotalMilliseconds))
    Start-Sleep -Milliseconds ([Math]::Min(500, $remainingMilliseconds))
  }
  return "Expired"
}

if (Test-AgentHealth) {
  Write-StartupState "healthy" "Source Agent autostart is healthy"
  exit 0
}

$sha = [Security.Cryptography.SHA256]::Create()
try {
  $rootHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($bridgeDir))).Replace("-", "").Substring(0, 16)
} finally {
  $sha.Dispose()
}
$watchdogMutex = New-Object Threading.Mutex($false, "Local\DianAgentDevWatchdog-$rootHash")
if (-not $watchdogMutex.WaitOne(0)) { exit 0 }

# A frozen runtime can remain alive before opening the port. Mere process
# existence must not suppress every future scheduled recovery. CreationDate
# gives a genuine cold start its remaining startup budget; only an over-age,
# continuously unhealthy exact runtime can reach replacement.
$alreadyStarting = @(Get-ExactRuntimeProcesses)
if ($alreadyStarting.Count -gt 0) {
  $initialPrebindPids = @($alreadyStarting | ForEach-Object { [int]$_.ProcessId })
  $startupBudgetResult = Wait-ForExactRuntimeStartupBudget $alreadyStarting
  if ($startupBudgetResult -eq "Healthy") {
    Write-StartupState "healthy" "Source Agent recovered while starting"
    exit 0
  }
  if ($startupBudgetResult -eq "IdentityChanged") {
    Write-StartupState "checking" "The source Agent process changed during startup" "prebind_process_identity_changed"
    exit 0
  }
  if ($startupBudgetResult -eq "CreationTimeUnknown") {
    Write-StartupState "checking" "The source Agent startup age could not be verified" "prebind_creation_time_unknown"
    exit 0
  }
  if ($startupBudgetResult -eq "Exited") {
    $alreadyStarting = @()
  }

  for ($failure = 1; $failure -le 3; $failure++) {
    if (Test-AgentHealth) {
      Write-StartupState "healthy" "Source Agent recovered while starting"
      exit 0
    }
    if ($failure -lt 3) { Start-Sleep -Milliseconds 750 }
  }
  $stalledProcesses = @(Get-ExactRuntimeProcesses)
  if (@($stalledProcesses | Where-Object { $initialPrebindPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
    Write-StartupState "checking" "The source Agent process changed during recovery" "prebind_process_identity_changed"
    exit 0
  }
  if (Test-AgentHealth) {
    Write-StartupState "healthy" "Source Agent recovered before replacement"
    exit 0
  }
  $finalStalledProcesses = @(Get-ExactRuntimeProcesses)
  if (@($finalStalledProcesses | Where-Object { $initialPrebindPids -notcontains [int]$_.ProcessId }).Count -gt 0) {
    Write-StartupState "checking" "A new source Agent process started before replacement" "prebind_process_identity_changed"
    exit 0
  }
  foreach ($stalledProcess in $finalStalledProcesses) {
    Stop-Process -Id ([int]$stalledProcess.ProcessId) -Force -ErrorAction SilentlyContinue
  }
  $stalledExitDeadline = (Get-Date).AddSeconds(5)
  while ((Get-Date) -lt $stalledExitDeadline -and @(Get-ExactRuntimeProcesses).Count -gt 0) {
    Start-Sleep -Milliseconds 200
  }
  if (@(Get-ExactRuntimeProcesses).Count -gt 0) {
    Write-StartupState "error" "The stalled source Agent could not be replaced" "stalled_process_not_released"
    exit 1
  }
  Write-StartupState "recovering" "Replacing a source Agent that stalled before opening its port" "prebind_process_stalled" -Recovered
}

if (Test-Path -LiteralPath $packagedAgent) {
  if (@(Get-ExactRuntimeProcesses).Count -eq 0) {
    Start-Process -FilePath $packagedAgent -WorkingDirectory (Split-Path -Parent $packagedAgent) -WindowStyle Hidden
  }
} else {
  if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-StartupState "error" "Source runtime is missing" "pythonw_not_found"
    exit 2
  }

  if (@(Get-ExactRuntimeProcesses).Count -eq 0) {
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $receiver + '"') -WorkingDirectory $bridgeDir -WindowStyle Hidden
  }
}

for ($attempt = 0; $attempt -lt ($StartupTimeoutSeconds * 2); $attempt++) {
  Start-Sleep -Milliseconds 500
  if (Test-AgentHealth) {
    Write-StartupState "healthy" "Source Agent recovered automatically" "" -Recovered
    exit 0
  }
}
Write-StartupState "error" "Source Agent recovery failed" "startup_timeout"
exit 1
