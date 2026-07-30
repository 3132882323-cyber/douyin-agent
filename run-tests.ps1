$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> bridge unit tests"
Push-Location (Join-Path $Root "bridge")
try {
  python -m unittest discover -s . -p "test_*.py"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}

Write-Host "==> extension node tests"
Push-Location (Join-Path $Root "extension")
try {
  node test-content-common.js
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node test-scan-policy.js
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node test-content-qianchuan.js
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node test-content-qianchuan-executor.js
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node test-page-contracts.js
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}

Write-Host "all tests passed"
