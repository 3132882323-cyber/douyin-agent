$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectDir = Split-Path -Parent $PSScriptRoot
$sandboxParent = Join-Path $projectDir "dist\startup-recovery-tests"
$sandboxParentFull = [IO.Path]::GetFullPath($sandboxParent)
$python = Join-Path $projectDir "bridge\.venv\Scripts\python.exe"
$updaterScript = Join-Path $projectDir "bridge\offline_upgrade.py"
$agentSource = Join-Path $projectDir "dist\agent\DianAgent.exe"
$starter = Join-Path $PSScriptRoot "start_agent.ps1"
$watchdog = Join-Path $PSScriptRoot "watchdog_release.ps1"
foreach ($required in @($python, $updaterScript, $agentSource, $starter, $watchdog)) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Recovery test prerequisite is missing: $required" }
}

function Stop-SandboxAgents([string]$Root) {
  $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.ExecutablePath) {
      $path = [IO.Path]::GetFullPath([string]$_.ExecutablePath)
      if ($path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

function Write-Utf8Json([string]$Path, $Value) {
  $json = $Value | ConvertTo-Json -Depth 8
  [IO.File]::WriteAllText($Path, $json + "`n", (New-Object Text.UTF8Encoding($false)))
}

function Initialize-Case([string]$Name, [string]$CurrentVersion, [string]$PreviousVersion, [bool]$HealthyCurrent) {
  $root = Join-Path $sandboxParentFull $Name
  if (Test-Path -LiteralPath $root) {
    $rootFull = [IO.Path]::GetFullPath($root)
    if (-not $rootFull.StartsWith($sandboxParentFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
      throw "Unsafe recovery test root: $rootFull"
    }
    Stop-SandboxAgents $rootFull
    Remove-Item -LiteralPath $rootFull -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $root "tools"), (Join-Path $root "data"), (Join-Path $root "logs") | Out-Null
  Copy-Item -LiteralPath $watchdog -Destination (Join-Path $root "tools\watchdog_release.ps1")
  $wrapper = "@echo off`r`n`"$python`" `"$updaterScript`" %*`r`n"
  Set-Content -LiteralPath (Join-Path $root "tools\DianAgentUpdater.cmd") -Encoding ASCII -Value $wrapper

  $currentProgram = Join-Path $root "versions\$CurrentVersion\program"
  $previousProgram = Join-Path $root "versions\$PreviousVersion\program"
  New-Item -ItemType Directory -Force -Path $currentProgram, $previousProgram | Out-Null
  if ($HealthyCurrent) {
    Copy-Item -LiteralPath $agentSource -Destination (Join-Path $currentProgram "DianAgent.exe")
  } else {
    Copy-Item -LiteralPath (Join-Path $env:WINDIR "System32\cmd.exe") -Destination (Join-Path $currentProgram "DianAgent.exe")
  }
  Copy-Item -LiteralPath $agentSource -Destination (Join-Path $previousProgram "DianAgent.exe")

  $current = [ordered]@{
    schema_version = 1
    version = $CurrentVersion
    version_path = "versions/$CurrentVersion"
    activated_at = "2026-08-03T00:00:00+00:00"
  }
  Write-Utf8Json (Join-Path $root "current.json") $current
  $rollback = Join-Path $root ".offline-upgrade-rollback"
  New-Item -ItemType Directory -Path $rollback | Out-Null
  $previous = [ordered]@{
    schema_version = 1
    version = $PreviousVersion
    version_path = "versions/$PreviousVersion"
    activated_at = "2026-08-02T00:00:00+00:00"
  }
  Write-Utf8Json (Join-Path $rollback "previous-current.json") $previous
  Write-Utf8Json (Join-Path $rollback "state.json") ([ordered]@{
    schema_version = 1
    had_current = $true
    had_extension = $false
    previous_version = $PreviousVersion
    new_version = $CurrentVersion
  })
  return $root
}

New-Item -ItemType Directory -Force -Path $sandboxParentFull | Out-Null
$powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
try {
  $healthyRoot = Initialize-Case "healthy-confirm" "4.0.0" "3.9.0" $true
  & $powershell -NoProfile -ExecutionPolicy Bypass -File $starter -InstallRoot $healthyRoot -Port 18765 `
    -StartupTimeoutSeconds 8 -UpdaterPath (Join-Path $healthyRoot "tools\DianAgentUpdater.cmd")
  if ($LASTEXITCODE -ne 0) { throw "Healthy pending upgrade recovery returned $LASTEXITCODE." }
  if (Test-Path -LiteralPath (Join-Path $healthyRoot ".offline-upgrade-rollback")) {
    throw "Healthy pending upgrade was not confirmed."
  }
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:18765/health" -TimeoutSec 3
  if ($health.version -ne "4.0.0") { throw "Healthy recovery started the wrong version." }
  Stop-SandboxAgents $healthyRoot

  $rollbackRoot = Initialize-Case "unhealthy-rollback" "4.1.0" "4.0.0" $false
  & $powershell -NoProfile -ExecutionPolicy Bypass -File $starter -InstallRoot $rollbackRoot -Port 18766 `
    -StartupTimeoutSeconds 8 -UpdaterPath (Join-Path $rollbackRoot "tools\DianAgentUpdater.cmd")
  if ($LASTEXITCODE -ne 4) { throw "Unhealthy pending upgrade should return recovery code 4, got $LASTEXITCODE." }
  if (Test-Path -LiteralPath (Join-Path $rollbackRoot ".offline-upgrade-rollback")) {
    throw "Unhealthy pending upgrade transaction was not rolled back."
  }
  $pointer = Get-Content -LiteralPath (Join-Path $rollbackRoot "current.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  if ([string]$pointer.version -ne "4.0.0") { throw "Rollback did not restore the previous pointer." }
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:18766/health" -TimeoutSec 3
  if ($health.version -ne "4.0.0") { throw "Previous Agent did not become healthy after rollback." }
  Stop-SandboxAgents $rollbackRoot
} finally {
  if (Test-Path -LiteralPath $sandboxParentFull) {
    Get-ChildItem -LiteralPath $sandboxParentFull -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      Stop-SandboxAgents $_.FullName
    }
  }
}

Write-Host "Interrupted-upgrade startup recovery sandbox passed." -ForegroundColor Green
