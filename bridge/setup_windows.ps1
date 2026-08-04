param(
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$bridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $bridgeDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$packagedAgent = Join-Path (Split-Path -Parent $bridgeDir) "app\DianAgent.exe"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "DianAgentDev.lnk"
$legacySourceShortcutPath = Join-Path $startupDir "DianAgent.lnk"
$legacyShortcutPath = Join-Path $startupDir "抖店千川数据桥-Bridge.lnk"
$watchdogScript = Join-Path $bridgeDir "watchdog.ps1"
$watchdogLauncher = Join-Path $bridgeDir "watchdog.vbs"
$watchdogTaskName = "DianAgentDevKeepAlive"
$manifestPath = Join-Path (Split-Path -Parent $bridgeDir) "extension\manifest.json"
$expectedVersion = (Get-Content -Raw -Encoding UTF8 $manifestPath | ConvertFrom-Json).version

function Resolve-Python {
  if ($PythonPath -and (Test-Path -LiteralPath $PythonPath)) {
    return (Resolve-Path -LiteralPath $PythonPath).Path
  }
  if ($env:DIAN_AGENT_PYTHON -and (Test-Path -LiteralPath $env:DIAN_AGENT_PYTHON)) {
    return (Resolve-Path -LiteralPath $env:DIAN_AGENT_PYTHON).Path
  }
  foreach ($name in @("py.exe", "python.exe")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
  }
  throw "Python was not found. Install Python 3.10+ or pass -PythonPath."
}

if (-not (Test-Path -LiteralPath $packagedAgent)) {
  if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Resolve-Python
    Write-Host "Creating the Dian Agent local runtime..."
    if ((Split-Path -Leaf $python) -ieq "py.exe") {
      & $python -3 -m venv $venvDir
    } else {
      & $python -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the local runtime." }
  }

  Write-Host "Installing local Agent dependencies..."
  & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $bridgeDir "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network and retry." }
} else {
  Write-Host "Using the packaged Dian Agent runtime (Python is not required)."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = (Join-Path $env:WINDIR "System32\wscript.exe")
$shortcut.Arguments = '"' + $watchdogLauncher + '"'
$shortcut.WorkingDirectory = $bridgeDir
$shortcut.Description = "Dian Agent - start automatically after Windows sign-in"
$shortcut.Save()
if (Test-Path -LiteralPath $legacySourceShortcutPath -PathType Leaf) {
  $legacySourceShortcut = $shell.CreateShortcut($legacySourceShortcutPath)
  if ($legacySourceShortcut.Arguments -and $legacySourceShortcut.Arguments.Contains($watchdogLauncher)) {
    Remove-Item -LiteralPath $legacySourceShortcutPath -Force
  }
}
if (Test-Path -LiteralPath $legacyShortcutPath) {
  Remove-Item -LiteralPath $legacyShortcutPath -Force
}

# The Startup shortcut handles normal sign-in. The scheduled watchdog also
# repairs an Agent that exits later because of sleep, updates or a crash.
$legacyTask = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
if ($legacyTask) {
  $ownedLegacyTask = $legacyTask.Actions | Where-Object {
    $_.Arguments -and $_.Arguments.Contains($watchdogLauncher)
  }
  if ($ownedLegacyTask) {
    Unregister-ScheduledTask -TaskName "DianAgentKeepAlive" -Confirm:$false
  }
}
$taskAction = New-ScheduledTaskAction `
  -Execute (Join-Path $env:WINDIR "System32\wscript.exe") `
  -Argument ('"' + $watchdogLauncher + '"')
$taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 5) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
$taskSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
  -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $watchdogTaskName -Action $taskAction -Trigger $taskTrigger `
  -Settings $taskSettings -Description "Keeps the Dian Agent local service available." -Force | Out-Null

$runtimeDir = Join-Path $bridgeDir "data\runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
[ordered]@{
  schema_version = 1
  state = "configured"
  state_label = "源码开发模式自动启动已配置"
  autostart_enabled = $true
  keepalive_enabled = $true
  hidden_launcher = $true
  source = "source_development"
  task_name = $watchdogTaskName
  last_checked_at = [DateTime]::UtcNow.ToString("o")
  last_healthy_at = $null
  last_recovery_at = $null
  last_error = $null
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir "startup-state.json") -Encoding UTF8

try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
  if ([string]$health.version -ne [string]$expectedVersion) {
    $listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
      $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
      if ($process.CommandLine -and $process.CommandLine.Contains((Join-Path $bridgeDir "http_receiver.py"))) {
        Stop-Process -Id $listener.OwningProcess -Force
      } elseif ($process.ExecutablePath -and $process.ExecutablePath -eq $packagedAgent) {
        Stop-Process -Id $listener.OwningProcess -Force
      }
    }
    throw "Restart the local Agent after an update."
  }
} catch {
  & (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
    -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $watchdogScript
  Start-Sleep -Milliseconds 500
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 5
}

if ($health.status -ne "ok") { throw "The local Agent did not return a healthy status." }
if ([string]$health.version -ne [string]$expectedVersion) { throw "The local Agent version does not match the extension." }

Write-Host ""
Write-Host "Setup complete. Dian Agent is running and will start after Windows sign-in." -ForegroundColor Green
Write-Host "Startup shortcut: $shortcutPath"
Write-Host "Recovery watchdog: $watchdogTaskName (every 5 minutes)"
Write-Host "Health check: http://127.0.0.1:8765/health"
