param(
  [string]$PythonPath = "",
  [string]$SourceRoot = "",
  [string]$DistDir = "",
  [string]$WorkRoot = ""
)

$ErrorActionPreference = "Stop"
$projectDir = if ($SourceRoot) { [IO.Path]::GetFullPath($SourceRoot) } else { Split-Path -Parent $PSScriptRoot }
$bridgeDir = Join-Path $projectDir "bridge"
$manifestPath = Join-Path $projectDir "extension\manifest.json"
$versionSourcePath = Join-Path $bridgeDir "version.py"
foreach ($requiredSource in @($manifestPath, $versionSourcePath)) {
  if (-not (Test-Path -LiteralPath $requiredSource -PathType Leaf)) {
    throw "Agent build version prerequisite is missing: $requiredSource"
  }
}
$manifestVersion = [string](Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
$versionSource = Get-Content -LiteralPath $versionSourcePath -Raw -Encoding UTF8
$versionMatch = [regex]::Match($versionSource, '(?m)^\s*AGENT_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
if (-not $manifestVersion -or -not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $manifestVersion) {
  throw "Agent build source version mismatch: bridge/version.py and extension/manifest.json must match exactly before PyInstaller runs."
}
if (-not $DistDir) { $DistDir = Join-Path $projectDir "dist\agent" }
$distDir = [IO.Path]::GetFullPath($DistDir)
if (-not $WorkRoot) { $WorkRoot = Join-Path $projectDir "dist" }
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
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

& $python -c "import importlib.metadata as m, sys; expected={'pyinstaller':'6.21.0','pyinstaller-hooks-contrib':'2026.6','cryptography':'50.0.0','mcp':'1.29.0'}; sys.exit(0 if all(m.version(k)==v for k,v in expected.items()) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "Audited build dependencies are missing or mismatched. Install them with: python -m pip install -r bridge/requirements-build.txt"
}

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
# A direct or internal build must never inherit public provenance from an older
# artifact.  The verified public orchestrator recreates this marker only after
# compiling from its sanitized source tree.
Remove-Item -LiteralPath (Join-Path $distDir "PUBLIC_BUILD.json") -Force -ErrorAction SilentlyContinue
& $python -m PyInstaller --noconfirm --clean --distpath $distDir --workpath (Join-Path $WorkRoot "pyinstaller-work") (Join-Path $bridgeDir "dian_agent.spec")
if ($LASTEXITCODE -ne 0) { throw "Dian Agent executable build failed." }

$exe = Join-Path $distDir "DianAgent.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Build completed without DianAgent.exe." }
$agentVersionInfo = (Get-Item -LiteralPath $exe).VersionInfo
if ([string]$agentVersionInfo.FileVersion -ne $manifestVersion -or
    [string]$agentVersionInfo.ProductVersion -ne $manifestVersion) {
  throw "Built DianAgent.exe version does not exactly match source version $manifestVersion."
}
$updaterWork = Join-Path $WorkRoot "pyinstaller-updater-work"
& $python -m PyInstaller --noconfirm --clean --onefile --console --name DianAgentUpdater `
  --distpath $distDir --workpath $updaterWork --specpath $updaterWork `
  (Join-Path $bridgeDir "offline_upgrade.py")
if ($LASTEXITCODE -ne 0) { throw "Offline updater executable build failed." }
$updater = Join-Path $distDir "DianAgentUpdater.exe"
if (-not (Test-Path -LiteralPath $updater)) { throw "Build completed without DianAgentUpdater.exe." }
Write-Host "Standalone local Agent: $exe"
Write-Host "Verified offline updater: $updater"
Write-Host "End users do not need Python when this executable is included in the installer."
