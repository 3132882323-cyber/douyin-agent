$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectDir = Split-Path -Parent $PSScriptRoot
$sandboxParent = Join-Path $projectDir "dist\installer-tests"
$sandboxRoot = Join-Path $sandboxParent "DianAgent"
$installer = Join-Path $PSScriptRoot "install_release.ps1"
$uninstaller = Join-Path $PSScriptRoot "uninstall_release.ps1"
$releaseManifestPath = Join-Path $projectDir "extension\manifest.json"
$releaseAgentPath = Join-Path $projectDir "dist\agent\DianAgent.exe"
foreach ($releasePrerequisite in @($releaseManifestPath, (Join-Path $projectDir "bridge\version.py"), $releaseAgentPath)) {
  if (-not (Test-Path -LiteralPath $releasePrerequisite -PathType Leaf)) {
    throw "Release installer test prerequisite is missing: $releasePrerequisite"
  }
}
$releaseVersion = [string](Get-Content -LiteralPath $releaseManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
$releaseSourceText = Get-Content -LiteralPath (Join-Path $projectDir "bridge\version.py") -Raw -Encoding UTF8
$releaseSourceMatch = [regex]::Match($releaseSourceText, '(?m)^\s*AGENT_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
if (-not $releaseVersion -or -not $releaseSourceMatch.Success -or $releaseSourceMatch.Groups[1].Value -ne $releaseVersion) {
  throw "Release installer test source version mismatch: bridge/version.py and extension/manifest.json must match exactly."
}
$releaseAgentVersionInfo = (Get-Item -LiteralPath $releaseAgentPath).VersionInfo
$releaseAgentFileVersion = [string]$releaseAgentVersionInfo.FileVersion
$releaseAgentProductVersion = [string]$releaseAgentVersionInfo.ProductVersion
if ($releaseAgentFileVersion -ne $releaseVersion -or $releaseAgentProductVersion -ne $releaseVersion) {
  throw "Release installer test requires a fresh DianAgent.exe for $releaseVersion; found FileVersion=$releaseAgentFileVersion ProductVersion=$releaseAgentProductVersion. Run tools/build_agent.ps1 first."
}
. (Join-Path $PSScriptRoot "windows_trust_policy.ps1")

$installerContract = Get-Content -LiteralPath $installer -Raw -Encoding UTF8
$policyContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "windows_trust_policy.ps1") -Raw -Encoding UTF8
if ($installerContract -notmatch '\[string\]\$trustResult\.agent_version\s+-ne\s+\$version') {
  throw "Installer does not fail closed when the packaged Agent version differs from the extension manifest."
}
if ($installerContract -match 'MaintenanceLockHeldByParent') {
  throw "Installer exposes a command-line maintenance-lock bypass."
}
$taskOwnershipCheck = $installerContract.IndexOf('Test-OwnedKeepAliveTask $existingKeepAliveTask $InstallRoot', [StringComparison]::Ordinal)
$trustProvisioning = $installerContract.IndexOf('Invoke-PackagedTrustProvisioner $stagedAgentSource', [StringComparison]::Ordinal)
$sourceBinaryVersionCheck = $installerContract.IndexOf('$agentFileVersion -ne $version -or $agentProductVersion -ne $version', [StringComparison]::Ordinal)
$stagedBinaryCopy = $installerContract.IndexOf('Copy-Item -LiteralPath $agentSource -Destination $stagedAgentSource -Force', [StringComparison]::Ordinal)
$stagedBinaryVersionCheck = $installerContract.IndexOf('$stagedAgentVersionInfo.FileVersion -ne $version', [StringComparison]::Ordinal)
if ($taskOwnershipCheck -lt 0 -or $trustProvisioning -lt 0 -or $taskOwnershipCheck -gt $trustProvisioning -or
    $sourceBinaryVersionCheck -lt 0 -or $sourceBinaryVersionCheck -gt $trustProvisioning -or
    $stagedBinaryCopy -lt 0 -or $stagedBinaryVersionCheck -lt $stagedBinaryCopy -or $stagedBinaryVersionCheck -gt $trustProvisioning -or
    ([regex]::Matches($installerContract, 'Copy-Item -LiteralPath \$agentSource -Destination')).Count -ne 1 -or
    $policyContract -notmatch '\$actions\.Count\s+-ne\s+1' -or
    $installerContract -notmatch 'cannot be claimed by this installation') {
  throw "Installer does not reject stale binaries or unowned task/shortcut names before trust or program mutation."
}
$repairContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "repair_agent.ps1") -Raw -Encoding UTF8
$uninstallContract = Get-Content -LiteralPath $uninstaller -Raw -Encoding UTF8
$syncContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "sync_release_tools.ps1") -Raw -Encoding UTF8
$startContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "start_agent.ps1") -Raw -Encoding UTF8
$watchdogContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "watchdog_release.ps1") -Raw -Encoding UTF8
$offlineUpgradeContract = Get-Content -LiteralPath (Join-Path $projectDir "bridge\offline_upgrade.py") -Raw -Encoding UTF8
if ($policyContract -notmatch 'Get-DianRecoveryBootstrapLauncherPath' -or
    $policyContract -notmatch 'New-ScheduledTaskAction -Execute \$wscript' -or
    $policyContract -notmatch 'shell\.Run\(command, 0, True\)' -or
    $policyContract -notmatch 'Test-DianOwnedDevelopmentKeepAliveTask' -or
    $policyContract -notmatch 'Test-DianOwnedDevelopmentShortcut') {
  throw "Stable bootstrap autostart is not routed through the owned hidden VBS launcher."
}
if ($installerContract -notmatch 'Assert-DianDevelopmentAutostartOwnership' -or
    $installerContract -notmatch 'Remove-CommittedDevelopmentAutostartArtifacts') {
  throw "Formal installation does not validate and retire source-development autostart."
}
if ($repairContract -notmatch 'Install-DianRecoveryBootstrapFiles \$InstallRoot \$PSScriptRoot' -or
    $repairContract -notmatch 'Set-DianRecoveryBootstrapAutostart \$InstallRoot \$true \$true' -or
    $repairContract -notmatch '\[switch\]\$SkipAutostart' -or
    $repairContract -notmatch 'autostart_checked\s*=\s*\$script:repairAutostartChecked' -or
    $repairContract -notmatch 'autostart_repaired\s*=\s*\$script:repairAutostartRepaired') {
  throw "Repair does not restore the stable bootstrap, task and Startup shortcut."
}
$repairTrustStart = $repairContract.IndexOf('$initialization = Invoke-TrustRepair', [StringComparison]::Ordinal)
$repairOwnershipGate = $repairContract.IndexOf('if (-not $SkipAutostart) {', [StringComparison]::Ordinal)
$repairOwnershipCheck = $repairContract.IndexOf('$existingKeepAliveTask = Get-ScheduledTask', [StringComparison]::Ordinal)
$repairAutostartGate = $repairContract.IndexOf('if (-not $SkipAutostart) {', $repairTrustStart, [StringComparison]::Ordinal)
$repairAutostartWrite = $repairContract.IndexOf('Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true', [StringComparison]::Ordinal)
if ($repairTrustStart -lt 0 -or $repairOwnershipGate -lt 0 -or
    $repairOwnershipCheck -le $repairOwnershipGate -or $repairOwnershipCheck -ge $repairTrustStart -or
    $repairAutostartGate -le $repairTrustStart -or $repairAutostartWrite -le $repairAutostartGate) {
  throw "Repair's explicit autostart skip weakened the default ownership checks or restore path."
}
$syncBootstrapIndex = $syncContract.IndexOf('Install-DianRecoveryBootstrapFiles $InstallRoot $SourceTools', [StringComparison]::Ordinal)
$syncSelfReturnIndex = $syncContract.IndexOf('if ($sourceIsInstalledTools)', [StringComparison]::Ordinal)
if ($syncBootstrapIndex -lt 0 -or $syncSelfReturnIndex -le $syncBootstrapIndex) {
  throw "Installed-tools self-sync returns before repairing bootstrap/autostart."
}
foreach ($contract in @($repairContract, $uninstallContract, $syncContract)) {
  if ($contract -notmatch 'Enter-DianMaintenanceLock' -or $contract -notmatch 'finally\s*\{') {
    throw "A Windows maintenance entrypoint is not serialized by the shared installation mutex."
  }
}
foreach ($entry in @(
  @{ Name = "repair"; Text = $repairContract },
  @{ Name = "start"; Text = $startContract },
  @{ Name = "watchdog"; Text = $watchdogContract },
  @{ Name = "uninstall"; Text = $uninstallContract },
  @{ Name = "tools sync"; Text = $syncContract }
)) {
  if ($entry.Text -notmatch 'Invoke-DianRecoverInstallTransaction') {
    throw "$($entry.Name) can mutate or launch around an unresolved primary install journal."
  }
}
$recoverBeforeCleanup = $installerContract.IndexOf('Invoke-DianRecoverInstallTransaction $InstallRoot 1', [StringComparison]::Ordinal)
$orphanCleanup = $installerContract.IndexOf('Remove-OrphanInstallStages $InstallRoot', [StringComparison]::Ordinal)
$installRootCreation = $installerContract.IndexOf('New-Item -ItemType Directory -Force -Path $InstallRoot', [StringComparison]::Ordinal)
$journalWrite = $installerContract.IndexOf('Write-DianInstallTransaction $InstallRoot $installJournal', [StringComparison]::Ordinal)
$directoryActivation = $installerContract.IndexOf('[void](Activate-PreparedDirectory $activationPlan)', [StringComparison]::Ordinal)
if ($recoverBeforeCleanup -lt 0 -or $orphanCleanup -le $recoverBeforeCleanup -or $installRootCreation -le $recoverBeforeCleanup -or
    $journalWrite -lt 0 -or $directoryActivation -le $journalWrite -or
    $installerContract -notmatch 'DIAN_AGENT_INSTALL_CRASH_POINT' -or
    $installerContract -notmatch 'commit_decided') {
  throw "Installer does not durably recover first, journal before activation, or expose its hard-kill commit boundaries."
}
if ($policyContract -notmatch 'FileOptions\]::WriteThrough' -or
    $policyContract -notmatch 'IO\.File\]::Replace' -or
    $policyContract -notmatch 'rollback_state' -or
    $policyContract -notmatch 'after-target-to-prepared-' -or
    $policyContract -notmatch 'after-backup-to-target-' -or
    $policyContract -notmatch 'rollback_incomplete') {
  throw "Persistent install recovery is missing durable writes, re-entrant directory rollback, or fail-closed retention."
}
$bootstrapContract = Get-Content -LiteralPath (Join-Path $PSScriptRoot "recovery_bootstrap.ps1") -Raw -Encoding UTF8
$bootstrapPublish = $installerContract.IndexOf('Install-DianRecoveryBootstrapFiles $InstallRoot $PSScriptRoot', [StringComparison]::Ordinal)
$bootstrapAutostart = $installerContract.IndexOf('Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true', [StringComparison]::Ordinal)
if ($bootstrapPublish -lt 0 -or $bootstrapPublish -ge $journalWrite -or
    $bootstrapAutostart -le $journalWrite -or $bootstrapAutostart -ge $directoryActivation -or
    $bootstrapContract -notmatch 'Invoke-DianRecoverInstallTransaction' -or
    $bootstrapContract -notmatch 'Invoke-DianRecoverReleaseToolsTransaction') {
  throw "Stable recovery bootstrap is not published before switching or does not cover both maintenance journals."
}
$ownershipRoot = Join-Path ([IO.Path]::GetTempPath()) "DianAgentOwnershipFixture"
$wscriptFixture = Join-Path $env:WINDIR "System32\wscript.exe"
$bootstrapTaskFixture = [pscustomobject]@{
  Actions = @([pscustomobject]@{
    Execute = $wscriptFixture
    Arguments = '"' + (Get-DianRecoveryBootstrapLauncherPath $ownershipRoot) + '"'
  })
}
if (-not (Test-DianOwnedKeepAliveTask $bootstrapTaskFixture $ownershipRoot)) {
  throw "Stable VBS keepalive task is not recognized as owned."
}
$developmentLauncherFixture = Join-Path $ownershipRoot "checkout\bridge\watchdog.vbs"
$developmentTaskFixture = [pscustomobject]@{
  Actions = @([pscustomobject]@{ Execute = $wscriptFixture; Arguments = '"' + $developmentLauncherFixture + '"' })
}
if (-not (Test-DianOwnedDevelopmentKeepAliveTask $developmentTaskFixture)) {
  throw "Source-development keepalive ownership policy rejected its canonical launcher."
}
$foreignDevelopmentTaskFixture = [pscustomobject]@{
  Actions = @([pscustomobject]@{ Execute = $wscriptFixture; Arguments = '"C:\foreign\other.vbs"' })
}
if (Test-DianOwnedDevelopmentKeepAliveTask $foreignDevelopmentTaskFixture) {
  throw "Source-development keepalive ownership policy accepted a foreign launcher."
}
$toolsJournalWrite = $syncContract.IndexOf('Write-DianReleaseToolsTransaction $InstallRoot $toolsJournal', [StringComparison]::Ordinal)
$toolsTargetMove = $syncContract.IndexOf('[IO.Directory]::Move($target, $backup)', [StringComparison]::Ordinal)
if ($toolsJournalWrite -lt 0 -or $toolsTargetMove -le $toolsJournalWrite -or
    $syncContract -notmatch 'after-target-to-backup') {
  throw "Release-tools update is not durably journaled before its target-missing window."
}
if (($offlineUpgradeContract | Select-String -Pattern '_assert_no_primary_install_transaction\(' -AllMatches).Matches.Count -lt 6) {
  throw "Offline updater mutation paths do not all fail closed on a pending primary install journal."
}
if ($syncContract -notmatch '\.dian-agent-install\.json' -or
    $syncContract -notmatch 'Assert-DianNoReparsePoints \$SourceTools' -or
    $syncContract -notmatch '\.release-tools-stage-') {
  throw "Release-tools synchronization lacks marker ownership, media reparse, or namespace isolation checks."
}

$manifestIdFixture = "a" * 32
$legacyIdFixture = "b" * 32
$untrustedIdFixture = "c" * 32
$registryFixture = [pscustomobject]@{ schema_version = 1; extension_ids = @($manifestIdFixture, $legacyIdFixture) }
$freshAccepted = @(Get-DianAcceptedExtensionIds $manifestIdFixture $registryFixture $false)
$upgradeAccepted = @(Get-DianAcceptedExtensionIds $manifestIdFixture $registryFixture $true)
if ($freshAccepted.Count -ne 1 -or $freshAccepted[0] -ne $manifestIdFixture) {
  throw "Fresh-install report policy accepted an extension other than the manifest-derived ID."
}
if ($upgradeAccepted.Count -ne 2 -or $upgradeAccepted -notcontains $legacyIdFixture) {
  throw "Upgrade report policy did not retain a previously trusted legacy extension ID."
}
$policyStartedAt = [DateTimeOffset]::UtcNow.AddSeconds(-1)
$legacyReportFixture = [pscustomobject]@{
  origin_verified = $true
  version = "4.13.4"
  extension_id = $legacyIdFixture
  reported_at = [DateTimeOffset]::UtcNow.ToString("o")
}
if (-not (Test-DianExtensionReport $legacyReportFixture $upgradeAccepted "4.13.4" $policyStartedAt).Ready) {
  throw "Upgrade report policy rejected a pre-authorized legacy extension ID."
}
if ((Test-DianExtensionReport $legacyReportFixture $freshAccepted "4.13.4" $policyStartedAt).Ready) {
  throw "Fresh-install report policy accepted a legacy extension ID."
}
$legacyReportFixture.extension_id = $untrustedIdFixture
$untrustedAssessment = Test-DianExtensionReport $legacyReportFixture $upgradeAccepted "4.13.4" $policyStartedAt
if ($untrustedAssessment.Ready -or $untrustedAssessment.Reason -ne "extension_not_pre_authorized") {
  throw "Upgrade report policy accepted an untrusted self-reported extension ID."
}
$legacyReportFixture.extension_id = $legacyIdFixture
$legacyReportFixture.reported_at = $policyStartedAt.AddSeconds(-1).ToString("o")
if ((Test-DianExtensionReport $legacyReportFixture $upgradeAccepted "4.13.4" $policyStartedAt).Reason -ne "report_stale") {
  throw "Upgrade report policy accepted a receipt from before this installation attempt."
}
$legacyReportFixture.reported_at = [DateTimeOffset]::UtcNow.ToString("o")
$legacyReportFixture.origin_verified = $false
if ((Test-DianExtensionReport $legacyReportFixture $upgradeAccepted "4.13.4" $policyStartedAt).Reason -ne "origin_not_verified") {
  throw "Upgrade report policy accepted a report without browser-Origin verification."
}

function Assert-NoUtf8Bom([string]$Path) {
  $bytes = [IO.File]::ReadAllBytes($Path)
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    throw "Local trust record contains a UTF-8 BOM: $Path"
  }
}

if (Test-Path -LiteralPath $sandboxRoot) {
  $fullSandbox = [IO.Path]::GetFullPath($sandboxRoot)
  $fullParent = [IO.Path]::GetFullPath($sandboxParent).TrimEnd('\') + '\'
  if (-not $fullSandbox.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe installer test sandbox: $fullSandbox"
  }
  Remove-Item -LiteralPath $sandboxRoot -Recurse -Force
}

& $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
$version = (Get-Content -LiteralPath (Join-Path $sandboxRoot "current-version.txt") -Raw).Trim()
$installedAgentPath = Join-Path $sandboxRoot "app\$version\DianAgent.exe"
$installedVersionInfo = (Get-Item -LiteralPath $installedAgentPath).VersionInfo
if ([string]$installedVersionInfo.FileVersion -ne $version -or
    [string]$installedVersionInfo.ProductVersion -ne $version) {
  throw "Installed DianAgent.exe FileVersion/ProductVersion does not exactly match manifest version $version."
}
foreach ($required in @(
  "app\$version\DianAgent.exe",
  "extension\$version\manifest.json",
  "extension-current\manifest.json",
  "tools\start_agent.ps1",
  "tools\watchdog_release.ps1",
  "tools\watchdog_release.vbs",
  "tools\recovery_bootstrap.ps1",
  "tools\repair_agent.ps1",
  "tools\repair_agent.vbs",
  "tools\windows_trust_policy.ps1",
  "tools\uninstall_release.ps1",
  "tools\sync_release_tools.ps1",
  "tools\DianAgentUpdater.exe",
  "bootstrap\recovery_bootstrap.ps1",
  "bootstrap\recovery_bootstrap.vbs",
  "bootstrap\windows_trust_policy.ps1",
  "config\local_api_auth.json",
  "config\trusted_extension_ids.json",
  "data", "config", "knowledge", "backup", "logs",
  ".dian-agent-install.json"
)) {
  if (-not (Test-Path -LiteralPath (Join-Path $sandboxRoot $required))) { throw "Missing installed item: $required" }
}
$localApiAuthPath = Join-Path $sandboxRoot "config\local_api_auth.json"
$trustedIdsPath = Join-Path $sandboxRoot "config\trusted_extension_ids.json"
Assert-NoUtf8Bom $localApiAuthPath
Assert-NoUtf8Bom $trustedIdsPath
$localApiAuth = Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($localApiAuth.schema_version -ne 1 -or [string]$localApiAuth.install_id -notmatch '^[a-f0-9]{32}$') {
  throw "Installer did not create a valid local API authentication identity."
}
$installationSecret = [string]$localApiAuth.secret
if ($installationSecret.Length -lt 40) { throw "Installer local API secret is not 256-bit encoded material." }
$trustedIds = Get-Content -LiteralPath $trustedIdsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($trustedIds.schema_version -ne 1 -or @($trustedIds.extension_ids).Count -lt 1 -or
    @($trustedIds.extension_ids | Where-Object { [string]$_ -notmatch '^[a-p]{32}$' }).Count -gt 0) {
  throw "Installer did not provision a valid trusted extension registry."
}

# A terminated installer can leave only its randomized, exact-name stages.
# The next run must remove those stages, preserve lookalikes, and fail closed
# before trust mutation if a stale stage contains a junction.
$orphanStage = Join-Path $sandboxRoot ("app\.app-{0}-stage-{1}" -f $version, [Guid]::NewGuid().ToString("N"))
$orphanLookalike = Join-Path $sandboxRoot ("app\.app-{0}-stage-not-a-guid" -f $version)
New-Item -ItemType Directory -Path $orphanStage, $orphanLookalike | Out-Null
Set-Content -LiteralPath (Join-Path $orphanStage "stale.txt") -Encoding ASCII -Value "stale"
& $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
if ((Test-Path -LiteralPath $orphanStage) -or -not (Test-Path -LiteralPath $orphanLookalike -PathType Container)) {
  throw "Installer orphan-stage cleanup was not bounded to exact transaction names."
}
Remove-Item -LiteralPath $orphanLookalike -Recurse -Force

$unsafeOrphanStage = Join-Path $sandboxRoot ("app\.app-{0}-stage-{1}" -f $version, [Guid]::NewGuid().ToString("N"))
$unsafeOrphanLink = Join-Path $unsafeOrphanStage "escaped-junction"
New-Item -ItemType Directory -Path $unsafeOrphanStage | Out-Null
New-Item -ItemType Junction -Path $unsafeOrphanLink -Target (Join-Path $projectDir "extension") | Out-Null
$authBeforeUnsafeCleanup = [IO.File]::ReadAllBytes($localApiAuthPath)
$unsafeOrphanRefused = $false
try {
  & $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
} catch {
  $unsafeOrphanRefused = ($_.Exception.Message -match "reparse point|junction|symbolic link")
}
if (-not $unsafeOrphanRefused -or
    [Convert]::ToBase64String($authBeforeUnsafeCleanup) -ne
      [Convert]::ToBase64String([IO.File]::ReadAllBytes($localApiAuthPath))) {
  throw "Installer followed an unsafe orphan stage or mutated trust before refusing it."
}
[IO.Directory]::Delete($unsafeOrphanLink)
Remove-Item -LiteralPath $unsafeOrphanStage -Recurse -Force

# A same-version reinstall used to delete the active app directory before the
# replacement copy was complete. Inject a failure immediately after the first
# directory rename and prove that every previously committed tree and pointer
# is restored, with no orphaned stages or backups.
$appRollbackSentinel = Join-Path $sandboxRoot "app\$version\preserve-on-install-fault.txt"
$extensionRollbackSentinel = Join-Path $sandboxRoot "extension-current\preserve-on-install-fault.txt"
Set-Content -LiteralPath $appRollbackSentinel -Encoding ASCII -Value "old-app-tree"
Set-Content -LiteralPath $extensionRollbackSentinel -Encoding ASCII -Value "old-extension-tree"
$previousFaultPoint = [string]$env:DIAN_AGENT_INSTALL_FAULT_POINT
$faultWasRefused = $false
try {
  $env:DIAN_AGENT_INSTALL_FAULT_POINT = "after-app-activate"
  try {
    & $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
  } catch {
    $faultWasRefused = ($_.Exception.Message -match "Injected installer failure")
  }
} finally {
  $env:DIAN_AGENT_INSTALL_FAULT_POINT = $previousFaultPoint
}
if (-not $faultWasRefused -or
    (Get-Content -LiteralPath $appRollbackSentinel -Raw).Trim() -ne "old-app-tree" -or
    (Get-Content -LiteralPath $extensionRollbackSentinel -Raw).Trim() -ne "old-extension-tree" -or
    (Get-Content -LiteralPath (Join-Path $sandboxRoot "current-version.txt") -Raw).Trim() -ne $version) {
  throw "Same-version installer fault did not restore the previous committed installation."
}

# Backups must remain available through the exact-version Agent health gate.
# Fail after pointers and autostart preparation but before Agent launch, then
# prove the old app, extension and pointers are still restored.
$healthGateFaultWasRefused = $false
try {
  $env:DIAN_AGENT_INSTALL_FAULT_POINT = "before-agent-start"
  try {
    & $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -ExtensionReportTimeoutSeconds 5
  } catch {
    $healthGateFaultWasRefused = ($_.Exception.Message -match "Injected installer failure")
  }
} finally {
  $env:DIAN_AGENT_INSTALL_FAULT_POINT = $previousFaultPoint
}
if (-not $healthGateFaultWasRefused -or
    (Get-Content -LiteralPath $appRollbackSentinel -Raw).Trim() -ne "old-app-tree" -or
    (Get-Content -LiteralPath $extensionRollbackSentinel -Raw).Trim() -ne "old-extension-tree" -or
    (Get-Content -LiteralPath (Join-Path $sandboxRoot "current-version.txt") -Raw).Trim() -ne $version) {
  throw "Installer discarded rollback backups before the exact Agent health commit gate."
}
$transactionDebris = @(
  Get-ChildItem -LiteralPath $sandboxRoot -Force -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\..+-(?:stage|backup)-[0-9a-f]{32}$' }
)
if ($transactionDebris.Count -gt 0) {
  throw "Installer fault left transaction debris: $($transactionDebris[0].FullName)"
}
Remove-Item -LiteralPath $appRollbackSentinel, $extensionRollbackSentinel -Force

# Release media is an input boundary. A nested junction must be refused before
# its manifest can authorize writes or a recursive copy can escape the media
# tree. Directory junction creation does not require Developer Mode.
$unsafeMedia = Join-Path $sandboxParent "unsafe-junction-media"
$unsafeExtension = Join-Path $unsafeMedia "extension"
$unsafeInstall = Join-Path $sandboxParent "DianAgent-unsafe-media"
if (Test-Path -LiteralPath $unsafeMedia) { Remove-Item -LiteralPath $unsafeMedia -Recurse -Force }
if (Test-Path -LiteralPath $unsafeInstall) { Remove-Item -LiteralPath $unsafeInstall -Recurse -Force }
New-Item -ItemType Directory -Path $unsafeMedia | Out-Null
New-Item -ItemType Junction -Path $unsafeExtension -Target (Join-Path $projectDir "extension") | Out-Null
$unsafeMediaRefused = $false
try {
  & $installer -InstallRoot $unsafeInstall -SourceRoot $unsafeMedia -SkipAutostart -SkipLaunch
} catch {
  $unsafeMediaRefused = ($_.Exception.Message -match "reparse point|symbolic link|junction")
}
if (-not $unsafeMediaRefused -or (Test-Path -LiteralPath $unsafeInstall)) {
  throw "Installer followed a release-media junction or mutated the install root before refusing it."
}
[IO.Directory]::Delete($unsafeExtension)
Remove-Item -LiteralPath $unsafeMedia -Force

# A second installer must fail before trust or program mutation while the
# per-install maintenance mutex is held by another process.
$lockSandbox = Join-Path $sandboxParent "DianAgent-lock-refusal"
if (Test-Path -LiteralPath $lockSandbox) { Remove-Item -LiteralPath $lockSandbox -Recurse -Force }
$lockFull = [IO.Path]::GetFullPath($lockSandbox).TrimEnd('\')
$lockSha = [Security.Cryptography.SHA256]::Create()
try {
  $lockHash = [BitConverter]::ToString($lockSha.ComputeHash([Text.Encoding]::UTF8.GetBytes($lockFull.ToUpperInvariant()))).Replace("-", "").Substring(0, 16)
} finally {
  $lockSha.Dispose()
}
$heldMaintenanceMutex = New-Object Threading.Mutex($false, "Local\DianAgentMaintenance-$lockHash")
$heldMaintenanceMutex.WaitOne() | Out-Null
try {
  $testPowershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
  $lockArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -SourceRoot "{2}" -SkipAutostart -SkipLaunch -MaintenanceLockTimeoutSeconds 1' -f `
    $installer, $lockSandbox, $projectDir
  $blockedInstaller = Start-Process -FilePath $testPowershell -ArgumentList $lockArguments -WindowStyle Hidden -PassThru -Wait
  if ($blockedInstaller.ExitCode -eq 0 -or
      (Test-Path -LiteralPath (Join-Path $lockSandbox "current-version.txt")) -or
      (Test-Path -LiteralPath (Join-Path $lockSandbox ".dian-agent-install.json"))) {
    throw "Concurrent installer was not rejected before active installation state was written."
  }
} finally {
  $heldMaintenanceMutex.ReleaseMutex()
  $heldMaintenanceMutex.Dispose()
  if (Test-Path -LiteralPath $lockSandbox) { Remove-Item -LiteralPath $lockSandbox -Recurse -Force }
}

# A corrupt existing credential must stop an upgrade before program files are
# mutated. The authoritative Agent initializer must never silently rotate it.
$authBytes = [IO.File]::ReadAllBytes($localApiAuthPath)
[IO.File]::WriteAllText($localApiAuthPath, "{corrupt", (New-Object Text.UTF8Encoding($false)))
$refusedCorruptUpgrade = $false
try {
  & $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
} catch {
  $refusedCorruptUpgrade = $true
}
if (-not $refusedCorruptUpgrade -or [IO.File]::ReadAllText($localApiAuthPath) -ne "{corrupt") {
  throw "Installer did not fail closed on a corrupt local API credential."
}
[IO.File]::WriteAllBytes($localApiAuthPath, $authBytes)

# The explicit repair path preserves both trust records together before
# rotating a damaged installation identity. Repair receipts are not healthy
# service claims unless the authenticated probe also succeeds.
$repairScript = Join-Path $sandboxRoot "tools\repair_agent.ps1"
$powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$repairStateLockFixture = Join-Path $sandboxRoot "data\runtime\repair-state.json"
[IO.File]::WriteAllText($repairStateLockFixture, "{`"sentinel`":true}`n", (New-Object Text.UTF8Encoding($false)))
$repairStateBeforeLock = [IO.File]::ReadAllBytes($repairStateLockFixture)
$heldRepairMutex = New-Object Threading.Mutex($false, (Get-DianMaintenanceMutexName $sandboxRoot))
$heldRepairMutex.WaitOne() | Out-Null
try {
  $repairLockArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -SkipRestart -SkipAutostart -MaintenanceLockTimeoutSeconds 1' -f `
    $repairScript, $sandboxRoot
  $blockedRepair = Start-Process -FilePath $powershell -ArgumentList $repairLockArguments -WindowStyle Hidden -PassThru -Wait
  if ($blockedRepair.ExitCode -eq 0 -or
      [Convert]::ToBase64String($repairStateBeforeLock) -ne
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($repairStateLockFixture))) {
    throw "Concurrent repair was not rejected without mutating another operation's state."
  }
} finally {
  $heldRepairMutex.ReleaseMutex()
  $heldRepairMutex.Dispose()
}
[IO.File]::WriteAllText($trustedIdsPath, "{corrupt", (New-Object Text.UTF8Encoding($false)))
& $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -SkipRestart -SkipAutostart
if ($LASTEXITCODE -ne 0) { throw "Repair did not recover the corrupt extension trust registry." }
$trustRepairState = Get-Content -LiteralPath (Join-Path $sandboxRoot "data\runtime\repair-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$trustRepairStartupState = Get-Content -LiteralPath (Join-Path $sandboxRoot "data\runtime\startup-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($trustRepairState.autostart_checked -ne $false -or $trustRepairState.autostart_repaired -ne $false -or
    @($trustRepairState.repaired_files | Where-Object { [string]$_ -like "scheduled_task:*" -or [string]$_ -like "startup_shortcut:*" }).Count -ne 0) {
  throw "Skip-autostart repair falsely claimed that global autostart was checked or repaired."
}
if ($trustRepairStartupState.state -ne "not_checked" -or
    $trustRepairStartupState.source -ne "release_repair_skip_autostart" -or
    $trustRepairStartupState.last_error -ne "autostart_not_checked" -or
    $null -ne $trustRepairStartupState.autostart_enabled -or
    $null -ne $trustRepairStartupState.keepalive_enabled -or
    $null -ne $trustRepairStartupState.hidden_launcher) {
  throw "Skip-autostart repair wrote an authoritative autostart health claim."
}
$trustRepairSecret = [string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret)
if (-not $trustRepairSecret -or $trustRepairSecret -eq $installationSecret) {
  throw "Repair did not rotate the installation identity after extension trust corruption."
}
$installationSecret = $trustRepairSecret
Assert-NoUtf8Bom $trustedIdsPath
$repairedRegistry = Get-Content -LiteralPath $trustedIdsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($repairedRegistry.extension_ids).Count -lt 1) { throw "Repair did not restore the fixed extension trust." }
$trustBackups = @(Get-ChildItem -LiteralPath (Join-Path $sandboxRoot "config\repair-backup") -Recurse -Filter "trusted_extension_ids.json")
if ($trustBackups.Count -ne 1 -or [IO.File]::ReadAllText($trustBackups[0].FullName) -ne "{corrupt") {
  throw "Repair did not retain exactly one forensic backup of the corrupt trust registry."
}
$authBackups = @(Get-ChildItem -LiteralPath (Join-Path $sandboxRoot "config\repair-backup") -Recurse -Filter "local_api_auth.json")
if ($authBackups.Count -ne 1) { throw "Repair did not preserve the paired credential beside corrupt trust." }

[IO.File]::WriteAllText($localApiAuthPath, "{corrupt", (New-Object Text.UTF8Encoding($false)))
& $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -SkipRestart -SkipAutostart
if ($LASTEXITCODE -ne 0) { throw "Repair did not recover the corrupt installation credential." }
$repairedSecret = [string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret)
if (-not $repairedSecret -or $repairedSecret -eq $installationSecret) {
  throw "Repair did not rotate the already-corrupt installation credential."
}
$installationSecret = $repairedSecret
Assert-NoUtf8Bom $localApiAuthPath
$repairState = Get-Content -LiteralPath (Join-Path $sandboxRoot "data\runtime\repair-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($repairState.state -ne "trust_verified" -or $repairState.code -ne "trust_repair_staged" -or $repairState.service_verified -ne $false) {
  throw "Repair did not write a truthful trust-only verification receipt."
}

# Diagnostics are post-commit and non-authoritative. Make the log leaf a
# directory so Add-Content fails after the success receipt, then prove repair
# still exits successfully and does not overwrite that receipt with an error.
$repairLogFailureFixture = Join-Path $sandboxRoot "logs\repair-agent.log"
if (Test-Path -LiteralPath $repairLogFailureFixture) { Remove-Item -LiteralPath $repairLogFailureFixture -Force }
New-Item -ItemType Directory -Path $repairLogFailureFixture | Out-Null
& $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -SkipRestart -SkipAutostart
if ($LASTEXITCODE -ne 0) { throw "A diagnostics log append failure incorrectly reported repair failure." }
$repairState = Get-Content -LiteralPath (Join-Path $sandboxRoot "data\runtime\repair-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($repairState.state -ne "trust_verified" -or $repairState.code -eq "repair_failed") {
  throw "A diagnostics log append failure overwrote the committed repair success receipt."
}
Remove-Item -LiteralPath $repairLogFailureFixture -Recurse -Force
Set-Content -LiteralPath $repairLogFailureFixture -Encoding UTF8 -Value "repair log resumed after append-failure regression"

# PowerShell must agree with the Python authority on exact JSON types. A scalar
# extension ID is not a registry array even when its characters look valid.
$fixedExtensionId = [string](@($repairedRegistry.extension_ids)[0])
$scalarRegistry = '{"schema_version":1,"extension_ids":"' + $fixedExtensionId + '"}'
[IO.File]::WriteAllText($trustedIdsPath, $scalarRegistry, (New-Object Text.UTF8Encoding($false)))
& $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -SkipRestart -SkipAutostart
if ($LASTEXITCODE -ne 0) { throw "Repair did not recognize and recover a scalar extension trust registry." }
$scalarRepairSecret = [string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret)
if (-not $scalarRepairSecret -or $scalarRepairSecret -eq $installationSecret) {
  throw "Scalar trust corruption did not rotate the damaged installation identity."
}
$installationSecret = $scalarRepairSecret

# .NET's decoder accepts unused non-zero Base64 padding bits, while the Agent
# deliberately rejects them. Repair must classify that non-canonical secret as
# corrupt instead of getting stuck between two validators.
$authRecord = Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json
$alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
$lastIndex = $alphabet.IndexOf($installationSecret[$installationSecret.Length - 1])
if ($lastIndex -lt 0 -or ($lastIndex % 4) -ne 0) { throw "Generated installation secret was not canonical Base64url." }
$authRecord.secret = $installationSecret.Substring(0, $installationSecret.Length - 1) + $alphabet[$lastIndex + 1]
[IO.File]::WriteAllText($localApiAuthPath, ($authRecord | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
& $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -SkipRestart -SkipAutostart
if ($LASTEXITCODE -ne 0) { throw "Repair did not recognize and recover a non-canonical installation secret." }
$canonicalRepairSecret = [string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret)
if (-not $canonicalRepairSecret -or $canonicalRepairSecret -eq $installationSecret) {
  throw "Repair did not replace the non-canonical installation secret."
}
$installationSecret = $canonicalRepairSecret

# A production Repair is complete only when the exact installation can issue
# an origin-bound session and that session can read a protected route. Exercise
# the real packaged service on an ephemeral port, plus two fail-closed cases.
$portLease = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
$portLease.Start()
$repairPort = ([Net.IPEndPoint]$portLease.LocalEndpoint).Port
$portLease.Stop()
$sandboxAgentPath = [IO.Path]::GetFullPath((Join-Path $sandboxRoot "app\$version\DianAgent.exe"))
$oldAccessToken = ""
try {
  & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $sandboxRoot "tools\start_agent.ps1") `
    -InstallRoot $sandboxRoot -Port $repairPort
  if ($LASTEXITCODE -ne 0) { throw "The pre-repair packaged service did not start." }
  $initialListener = @(Get-NetTCPConnection -LocalPort $repairPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($initialListener.Count -ne 1) { throw "The pre-repair service did not own its test port." }
  $initialOwnerPid = [int]$initialListener[0].OwningProcess
  $preRepairRegistry = Get-Content -LiteralPath $trustedIdsPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $preRepairExtensionId = [string](@($preRepairRegistry.extension_ids)[0])
  $preRepairOrigin = "chrome-extension://$preRepairExtensionId"
  $oldSession = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/auth/session" -f $repairPort) -Method Post -TimeoutSec 5 `
    -Headers @{ Origin = $preRepairOrigin; "X-Dian-Agent" = "2"; "X-Dian-Agent-Extension-Version" = $version } -ContentType "application/json" `
    -Body (@{ extension_id = $preRepairExtensionId; extension_version = $version } | ConvertTo-Json -Compress)
  $oldAccessToken = [string]$oldSession.access_token
  [IO.File]::WriteAllText($trustedIdsPath, "{corrupt-while-running", (New-Object Text.UTF8Encoding($false)))
  & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repairScript -InstallRoot $sandboxRoot -Port $repairPort -SkipAutostart
  if ($LASTEXITCODE -ne 0) { throw "Repair did not verify the authenticated local API against the real packaged service." }
  $repairState = Get-Content -LiteralPath (Join-Path $sandboxRoot "data\runtime\repair-state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($repairState.state -ne "healthy" -or $repairState.code -ne "repair_verified" -or $repairState.service_verified -ne $true) {
    throw "Repair claimed success without a healthy authenticated verification receipt."
  }
  $replacementListener = @(Get-NetTCPConnection -LocalPort $repairPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($replacementListener.Count -ne 1 -or [int]$replacementListener[0].OwningProcess -eq $initialOwnerPid) {
    throw "Repair reused the pre-repair process instead of restarting the exact installation."
  }
  $installationSecret = [string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret)
  $verifiedOrigin = "chrome-extension://$($repairState.verified_extension_id)"
  if ([string]$oldSession.install_id -eq [string]$repairState.install_id) {
    throw "Running-state repair did not rotate the damaged installation identity."
  }
  $oldTokenStatus = 0
  try {
    Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/system/status" -f $repairPort) -TimeoutSec 5 `
      -Headers @{ Origin = $preRepairOrigin; "X-Dian-Agent-Token" = $oldAccessToken; "X-Dian-Agent-Extension-Version" = $version } | Out-Null
  } catch {
    if ($_.Exception.Response) { $oldTokenStatus = [int]$_.Exception.Response.StatusCode }
  }
  if ($oldTokenStatus -ne 401) { throw "A pre-repair session remained valid after credential rotation." }
  $validSession = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/auth/session" -f $repairPort) -Method Post -TimeoutSec 5 `
    -Headers @{ Origin = $verifiedOrigin; "X-Dian-Agent" = "2"; "X-Dian-Agent-Extension-Version" = $version } -ContentType "application/json" `
    -Body (@{ extension_id = [string]$repairState.verified_extension_id; extension_version = $version } | ConvertTo-Json -Compress)
  $protectedStatus = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/system/status" -f $repairPort) -TimeoutSec 8 `
    -Headers @{ Origin = $verifiedOrigin; "X-Dian-Agent-Token" = [string]$validSession.access_token; "X-Dian-Agent-Extension-Version" = $version }
  if ([string]$protectedStatus.agent_version -ne $version -or [string]$validSession.install_id -ne [string]$repairState.install_id) {
    throw "Authenticated repair probe reached the wrong installation or version."
  }
  $repairArtifacts = [IO.File]::ReadAllText((Join-Path $sandboxRoot "data\runtime\repair-state.json")) +
    [IO.File]::ReadAllText((Join-Path $sandboxRoot "logs\repair-agent.log"))
  if ($repairArtifacts.Contains([string]$validSession.access_token) -or $repairArtifacts.Contains($oldAccessToken) -or
      $repairArtifacts -match 'X-Dian-Agent-Token') {
    throw "Repair persisted a local API session token in diagnostics."
  }

  $untrustedStatus = 0
  try {
    Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/auth/session" -f $repairPort) -Method Post -TimeoutSec 5 `
      -Headers @{ Origin = ("chrome-extension://{0}" -f ("p" * 32)); "X-Dian-Agent" = "2"; "X-Dian-Agent-Extension-Version" = $version } -ContentType "application/json" `
      -Body (@{ extension_id = ("p" * 32); extension_version = $version } | ConvertTo-Json -Compress) | Out-Null
  } catch {
    if ($_.Exception.Response) { $untrustedStatus = [int]$_.Exception.Response.StatusCode }
  }
  if ($untrustedStatus -ne 403) { throw "An untrusted extension origin obtained or reached a local API session." }

  $missingTokenStatus = 0
  try {
    Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/system/status" -f $repairPort) -TimeoutSec 5 `
      -Headers @{ Origin = $verifiedOrigin; "X-Dian-Agent-Extension-Version" = $version } | Out-Null
  } catch {
    if ($_.Exception.Response) { $missingTokenStatus = [int]$_.Exception.Response.StatusCode }
  }
  if ($missingTokenStatus -ne 401) { throw "A protected route accepted the trusted Origin without its session token." }
} finally {
  $oldAccessToken = ""
  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $sandboxAgentPath } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# Simulate older installed launchers and prove that release media replaces the
# whole maintenance-tool set without touching user data.
$installedStarter = Join-Path $sandboxRoot "tools\start_agent.ps1"
$installedWatchdog = Join-Path $sandboxRoot "tools\watchdog_release.ps1"
Set-Content -LiteralPath $installedStarter -Value "# stale launcher" -Encoding ASCII
Set-Content -LiteralPath $installedWatchdog -Value "# stale watchdog" -Encoding ASCII
$toolsSync = Join-Path $PSScriptRoot "sync_release_tools.ps1"
$mediaTools = Join-Path $sandboxParent "release-media-tools"
if (Test-Path -LiteralPath $mediaTools) { Remove-Item -LiteralPath $mediaTools -Recurse -Force }
New-Item -ItemType Directory -Path $mediaTools | Out-Null
foreach ($name in @("start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1", "repair_agent.ps1", "repair_agent.vbs", "windows_trust_policy.ps1", "uninstall_release.ps1", "install_release.ps1", "sync_release_tools.ps1")) {
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $mediaTools $name)
}
Copy-Item -LiteralPath (Join-Path $projectDir "dist\agent\DianAgentUpdater.exe") -Destination (Join-Path $mediaTools "DianAgentUpdater.exe")
$heldSyncMutex = New-Object Threading.Mutex($false, (Get-DianMaintenanceMutexName $sandboxRoot))
$heldSyncMutex.WaitOne() | Out-Null
try {
  $syncLockArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -SourceTools "{2}" -SkipAutostartMigration -MaintenanceLockTimeoutSeconds 1' -f `
    $toolsSync, $sandboxRoot, $mediaTools
  $blockedSync = Start-Process -FilePath $powershell -ArgumentList $syncLockArguments -WindowStyle Hidden -PassThru -Wait
  if ($blockedSync.ExitCode -eq 0 -or (Get-Content -LiteralPath $installedStarter -Raw).Trim() -ne "# stale launcher") {
    throw "Concurrent release-tools synchronization mutated the active tools tree."
  }
} finally {
  $heldSyncMutex.ReleaseMutex()
  $heldSyncMutex.Dispose()
}
& $toolsSync -InstallRoot $sandboxRoot -SourceTools $mediaTools -SkipAutostartMigration
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installedStarter).Hash -ne
    (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot "start_agent.ps1")).Hash) {
  throw "Transactional release-tools synchronization did not replace the stale launcher."
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installedWatchdog).Hash -ne
    (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot "watchdog_release.ps1")).Hash) {
  throw "Transactional release-tools synchronization did not replace the stale watchdog."
}
if (-not (Test-Path -LiteralPath (Join-Path $sandboxRoot "tools\release-tools.json") -PathType Leaf)) {
  throw "Release-tools protocol marker was not installed."
}
Remove-Item -LiteralPath $mediaTools -Recurse -Force

$sentinel = Join-Path $sandboxRoot "data\preserve-me.txt"
Set-Content -LiteralPath $sentinel -Value "test"
$legacyProgramDebris = Join-Path $sandboxRoot ".extension-backup-3.6.0-20260101-000000"
New-Item -ItemType Directory -Path $legacyProgramDebris | Out-Null
Set-Content -LiteralPath (Join-Path $legacyProgramDebris "legacy.txt") -Encoding ASCII -Value "legacy"
$heldUninstallMutex = New-Object Threading.Mutex($false, (Get-DianMaintenanceMutexName $sandboxRoot))
$heldUninstallMutex.WaitOne() | Out-Null
try {
  $uninstallLockArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}" -KeepData -MaintenanceLockTimeoutSeconds 1' -f `
    $uninstaller, $sandboxRoot
  $blockedUninstall = Start-Process -FilePath $powershell -ArgumentList $uninstallLockArguments -WindowStyle Hidden -PassThru -Wait
  if ($blockedUninstall.ExitCode -eq 0 -or -not (Test-Path -LiteralPath $sentinel -PathType Leaf) -or
      -not (Test-Path -LiteralPath (Join-Path $sandboxRoot "app\$version\DianAgent.exe") -PathType Leaf)) {
    throw "Concurrent uninstall was not rejected before program or user-data mutation."
  }
} finally {
  $heldUninstallMutex.ReleaseMutex()
  $heldUninstallMutex.Dispose()
}
& $uninstaller -InstallRoot $sandboxRoot -KeepData
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) { throw "Keep-data uninstall removed user data." }
if (Test-Path -LiteralPath (Join-Path $sandboxRoot "app")) { throw "Keep-data uninstall left program files." }
if (Test-Path -LiteralPath (Join-Path $sandboxRoot "extension-current")) { throw "Keep-data uninstall left the stable extension path." }
if (Test-Path -LiteralPath (Join-Path $sandboxRoot "bootstrap")) { throw "Keep-data uninstall left the stable recovery bootstrap." }
if (Test-Path -LiteralPath $legacyProgramDebris) { throw "Keep-data uninstall left an exact legacy program backup." }

& $installer -InstallRoot $sandboxRoot -SourceRoot $projectDir -SkipAutostart -SkipLaunch
if ([string]((Get-Content -LiteralPath $localApiAuthPath -Raw -Encoding UTF8 | ConvertFrom-Json).secret) -ne $installationSecret) {
  throw "Upgrade rotated the local API installation secret instead of preserving active sessions."
}
& $uninstaller -InstallRoot $sandboxRoot -ClearData
if (Test-Path -LiteralPath $sandboxRoot) { throw "Clear-data uninstall left the install root behind." }

# A forged or stale marker must never authorize recursive deletion.
New-Item -ItemType Directory -Force -Path $sandboxRoot | Out-Null
$sentinel = Join-Path $sandboxRoot "must-survive.txt"
Set-Content -LiteralPath $sentinel -Value "test"
[ordered]@{
  product = "DianAgent"
  schema = 1
  install_root = (Join-Path $sandboxParent "different-root")
  current_version = "0.0.0"
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $sandboxRoot ".dian-agent-install.json") -Encoding UTF8
$refusedUnsafeClear = $false
try {
  & $uninstaller -InstallRoot $sandboxRoot -ClearData
} catch {
  $refusedUnsafeClear = $true
}
if (-not $refusedUnsafeClear -or -not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "Uninstaller path-marker safety check failed."
}
Remove-Item -LiteralPath $sandboxRoot -Recurse -Force

Write-Host "Release installer smoke test passed in the repository dist sandbox." -ForegroundColor Green
