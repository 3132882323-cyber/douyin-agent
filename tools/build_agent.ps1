param(
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$bridgeDir = Join-Path $projectDir "bridge"
$distDir = Join-Path $projectDir "dist\agent"
$python = $PythonPath
if (-not $python) {
  $venvPython = Join-Path $bridgeDir ".venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venvPython) { $python = $venvPython }
}
if (-not $python) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python) { throw "Python 3.10+ is required only on the release build machine." }

& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller is missing. Install it on the release build machine with: python -m pip install pyinstaller" }

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
& $python -m PyInstaller --noconfirm --clean --distpath $distDir --workpath (Join-Path $projectDir "dist\pyinstaller-work") (Join-Path $bridgeDir "dian_agent.spec")
if ($LASTEXITCODE -ne 0) { throw "Dian Agent executable build failed." }

$exe = Join-Path $distDir "DianAgent.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Build completed without DianAgent.exe." }
$updaterWork = Join-Path $projectDir "dist\pyinstaller-updater-work"
& $python -m PyInstaller --noconfirm --clean --onefile --console --name DianAgentUpdater `
  --distpath $distDir --workpath $updaterWork --specpath $updaterWork `
  (Join-Path $bridgeDir "offline_upgrade.py")
if ($LASTEXITCODE -ne 0) { throw "Offline updater executable build failed." }
$updater = Join-Path $distDir "DianAgentUpdater.exe"
if (-not (Test-Path -LiteralPath $updater)) { throw "Build completed without DianAgentUpdater.exe." }
Write-Host "Standalone local Agent: $exe"
Write-Host "Verified offline updater: $updater"
Write-Host "End users do not need Python when this executable is included in the installer."
