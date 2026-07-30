param(
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$bridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $bridgeDir ".venv\Scripts\pythonw.exe"
$receiver = Join-Path $bridgeDir "http_receiver.py"
$healthUrl = "http://127.0.0.1:$Port/health"

try {
  $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
  if ($health.status -eq "ok") { exit 0 }
} catch {
  # A failed health probe is the condition this watchdog is designed to repair.
}

if (-not (Test-Path -LiteralPath $pythonw)) { exit 2 }

$alreadyStarting = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($receiver) }
if ($alreadyStarting) { exit 0 }

Start-Process -FilePath $pythonw -ArgumentList ('"' + $receiver + '"') -WorkingDirectory $bridgeDir -WindowStyle Hidden

for ($attempt = 0; $attempt -lt 10; $attempt++) {
  Start-Sleep -Milliseconds 500
  try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($health.status -eq "ok") { exit 0 }
  } catch {
    # Retry briefly while Python imports and binds the local port.
  }
}
exit 1
