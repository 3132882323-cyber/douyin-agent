param(
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$bridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $bridgeDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "DianAgent.lnk"
$autostartScript = Join-Path $bridgeDir "autostart.vbs"

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

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = (Join-Path $env:WINDIR "System32\wscript.exe")
$shortcut.Arguments = '"' + $autostartScript + '"'
$shortcut.WorkingDirectory = $bridgeDir
$shortcut.Description = "Dian Agent - start automatically after Windows sign-in"
$shortcut.Save()

try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
} catch {
  Start-Process -FilePath (Join-Path $env:WINDIR "System32\wscript.exe") -ArgumentList ('"' + $autostartScript + '"') -WindowStyle Hidden
  Start-Sleep -Seconds 2
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 5
}

if ($health.status -ne "ok") { throw "The local Agent did not return a healthy status." }

Write-Host ""
Write-Host "Setup complete. Dian Agent is running and will start after Windows sign-in." -ForegroundColor Green
Write-Host "Startup shortcut: $shortcutPath"
Write-Host "Health check: http://127.0.0.1:8765/health"
