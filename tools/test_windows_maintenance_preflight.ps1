$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectDir = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $PSScriptRoot "install_release.ps1"
$uninstaller = Join-Path $PSScriptRoot "uninstall_release.ps1"
$repairScript = Join-Path $PSScriptRoot "repair_agent.ps1"
$installerContract = Get-Content -LiteralPath $installer -Raw -Encoding UTF8
$uninstallContract = Get-Content -LiteralPath $uninstaller -Raw -Encoding UTF8
$repairContract = Get-Content -LiteralPath $repairScript -Raw -Encoding UTF8

$rootPreflight = $installerContract.IndexOf('Assert-DianPathChainNoReparsePoints $InstallRoot "Install root"', [StringComparison]::Ordinal)
$installLock = $installerContract.IndexOf('$maintenanceMutex = Enter-MaintenanceLock $InstallRoot', [StringComparison]::Ordinal)
$installRootWrite = $installerContract.IndexOf('New-Item -ItemType Directory -Force -Path $InstallRoot', [StringComparison]::Ordinal)
$trustWrite = $installerContract.IndexOf('Invoke-PackagedTrustProvisioner $agentSource', [StringComparison]::Ordinal)
if ($rootPreflight -lt 0 -or $installLock -le $rootPreflight -or $installRootWrite -le $rootPreflight -or $trustWrite -le $rootPreflight) {
  throw "Installer custom-root ancestor validation does not precede locks and all target writes."
}

if ($installerContract -notmatch '(?s)if \(\$browser\)\s*\{\s*try\s*\{.+?Start-Process.+?\}\s*catch\s*\{\s*Write-Warning' -or
    $installerContract -notmatch '(?s)try\s*\{.+?Start-Process -FilePath "explorer\.exe".+?\}\s*catch\s*\{\s*Write-Warning') {
  throw "Post-commit browser or Explorer launch remains a fatal installer operation."
}
$explorerOpen = $installerContract.IndexOf('Start-Process -FilePath "explorer.exe"', [StringComparison]::Ordinal)
$durableCommit = $installerContract.LastIndexOf('$transactionCommitted = $true', $explorerOpen, [StringComparison]::Ordinal)
$commitRecovery = $installerContract.LastIndexOf('Invoke-DianRecoverInstallTransaction $InstallRoot 1', $explorerOpen, [StringComparison]::Ordinal)
if ($explorerOpen -lt 0 -or $durableCommit -lt 0 -or $commitRecovery -le $durableCommit -or $explorerOpen -le $commitRecovery) {
  throw "Browser UI is opened before the installation has durably finalized its commit."
}

$preflightBeforeRecovery = $uninstallContract.IndexOf('[void](New-UninstallPreflight $InstallRoot ([bool]$ClearData))', [StringComparison]::Ordinal)
$recoveryAfterPreflight = $uninstallContract.IndexOf('Invoke-DianRecoverInstallTransaction $InstallRoot 1', $preflightBeforeRecovery, [StringComparison]::Ordinal)
$frozenPlan = $uninstallContract.IndexOf('$plan = New-UninstallPreflight $InstallRoot ([bool]$ClearData)', $recoveryAfterPreflight, [StringComparison]::Ordinal)
$firstProcessMutation = $uninstallContract.IndexOf('Stop-Process -Id $_.ProcessId', $frozenPlan, [StringComparison]::Ordinal)
$firstTaskMutation = $uninstallContract.IndexOf('Unregister-ScheduledTask -TaskName "DianAgentKeepAlive"', $frozenPlan, [StringComparison]::Ordinal)
$firstFileMutation = $uninstallContract.IndexOf('Remove-Item -LiteralPath $shortcutPath', $frozenPlan, [StringComparison]::Ordinal)
if ($preflightBeforeRecovery -lt 0 -or $recoveryAfterPreflight -le $preflightBeforeRecovery -or $frozenPlan -le $recoveryAfterPreflight -or
    $firstProcessMutation -le $frozenPlan -or $firstTaskMutation -le $frozenPlan -or $firstFileMutation -le $frozenPlan) {
  throw "Keep-data uninstall can mutate process, task or files before its complete two-pass preflight."
}

if ($repairContract -notmatch '(?s)Write-RepairState \$state \$code \$message.+?try\s*\{\s*Write-RepairLog \$message\s*\}\s*catch\s*\{\s*Write-Warning') {
  throw "A post-success repair log failure can still overwrite the committed success receipt."
}

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$sandbox = [IO.Path]::GetFullPath((Join-Path $temporaryRoot ("dian-maint-{0}" -f [Guid]::NewGuid().ToString("N").Substring(0, 8))))
if (-not $sandbox.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe maintenance-test sandbox." }
$physicalParent = Join-Path $sandbox "physical-parent"
$junctionParent = Join-Path $sandbox "custom-root-junction"
$uninstallRoot = Join-Path $sandbox "uninstall-root"
$outsideRoot = Join-Path $sandbox "outside"
$unsafeProgramLink = Join-Path $uninstallRoot "bootstrap\unsafe-junction"
$unsafeAtomicLink = Join-Path $uninstallRoot ("..dian-agent-install.json.replace-{0}" -f [Guid]::NewGuid().ToString("N"))
try {
  New-Item -ItemType Directory -Force -Path $physicalParent, $outsideRoot | Out-Null
  New-Item -ItemType Junction -Path $junctionParent -Target $physicalParent | Out-Null
  $unsafeInstallRoot = Join-Path $junctionParent "DianAgent"
  $ancestorRefused = $false
  try {
    & $installer -InstallRoot $unsafeInstallRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
  } catch {
    $ancestorRefused = ($_.Exception.Message -match "reparse point|junction|symbolic link")
  }
  if (-not $ancestorRefused -or (Test-Path -LiteralPath (Join-Path $physicalParent "DianAgent"))) {
    throw "Installer wrote directories or trust configuration through a custom-root ancestor junction."
  }
  [IO.Directory]::Delete($junctionParent)

  $programSentinel = Join-Path $uninstallRoot "app\4.13.4\must-survive.exe"
  $dataSentinel = Join-Path $uninstallRoot "data\must-survive.txt"
  $outsideSentinel = Join-Path $outsideRoot "outside-must-survive.txt"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $programSentinel), (Split-Path -Parent $dataSentinel), (Split-Path -Parent $unsafeProgramLink) | Out-Null
  Set-Content -LiteralPath $programSentinel -Encoding ASCII -Value "program"
  Set-Content -LiteralPath $dataSentinel -Encoding ASCII -Value "data"
  Set-Content -LiteralPath $outsideSentinel -Encoding ASCII -Value "outside"
  Set-Content -LiteralPath (Join-Path $uninstallRoot "current-version.txt") -Encoding ASCII -Value "4.13.4"
  [ordered]@{
    product = "DianAgent"
    schema = 1
    install_root = [IO.Path]::GetFullPath($uninstallRoot)
    current_version = "4.13.4"
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $uninstallRoot ".dian-agent-install.json") -Encoding UTF8
  New-Item -ItemType Junction -Path $unsafeProgramLink -Target $outsideRoot | Out-Null

  $unsafeUninstallRefused = $false
  try {
    & $uninstaller -InstallRoot $uninstallRoot -KeepData
  } catch {
    $unsafeUninstallRefused = ($_.Exception.Message -match "reparse point|unsafe|junction|symbolic link")
  }
  if (-not $unsafeUninstallRefused -or
      -not (Test-Path -LiteralPath $programSentinel -PathType Leaf) -or
      -not (Test-Path -LiteralPath $dataSentinel -PathType Leaf) -or
      -not (Test-Path -LiteralPath $outsideSentinel -PathType Leaf) -or
      -not (Test-Path -LiteralPath (Join-Path $uninstallRoot "current-version.txt") -PathType Leaf) -or
      -not (Test-Path -LiteralPath (Join-Path $uninstallRoot ".dian-agent-install.json") -PathType Leaf)) {
    throw "Keep-data uninstall mutated installation state before rejecting an unsafe late program candidate."
  }
  [IO.Directory]::Delete($unsafeProgramLink)

  New-Item -ItemType Junction -Path $unsafeAtomicLink -Target $outsideRoot | Out-Null
  $unsafeAtomicRefused = $false
  try {
    & $uninstaller -InstallRoot $uninstallRoot -KeepData
  } catch {
    $unsafeAtomicRefused = ($_.Exception.Message -match "atomic-write debris|reparse point|unsafe")
  }
  if (-not $unsafeAtomicRefused -or
      -not (Test-Path -LiteralPath $programSentinel -PathType Leaf) -or
      -not (Test-Path -LiteralPath (Join-Path $uninstallRoot "current-version.txt") -PathType Leaf)) {
    throw "Keep-data uninstall mutated state before rejecting unsafe marker-write debris."
  }
  [IO.Directory]::Delete($unsafeAtomicLink)

  & $uninstaller -InstallRoot $uninstallRoot -KeepData
  $uninstalledMarker = Get-Content -LiteralPath (Join-Path $uninstallRoot ".dian-agent-install.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  if ((Test-Path -LiteralPath (Join-Path $uninstallRoot "app")) -or
      (Test-Path -LiteralPath (Join-Path $uninstallRoot "bootstrap")) -or
      (Test-Path -LiteralPath (Join-Path $uninstallRoot "current-version.txt")) -or
      -not (Test-Path -LiteralPath $dataSentinel -PathType Leaf) -or
      $null -ne $uninstalledMarker.current_version -or -not [string]$uninstalledMarker.uninstalled_at) {
    throw "Safe keep-data uninstall did not execute its frozen plan or preserve user data."
  }
} finally {
  if (Test-Path -LiteralPath $unsafeProgramLink) { [IO.Directory]::Delete($unsafeProgramLink) }
  if (Test-Path -LiteralPath $unsafeAtomicLink) { [IO.Directory]::Delete($unsafeAtomicLink) }
  if (Test-Path -LiteralPath $junctionParent) { [IO.Directory]::Delete($junctionParent) }
  if (Test-Path -LiteralPath $sandbox) { Remove-Item -LiteralPath $sandbox -Recurse -Force }
}

Write-Host "Windows maintenance preflight regression test passed." -ForegroundColor Green
