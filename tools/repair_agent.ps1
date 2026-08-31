[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [int]$Port = 8765,
  [switch]$SkipRestart,
  [switch]$SkipAutostart,
  [ValidateRange(1, 300)][int]$MaintenanceLockTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) { $InstallRoot = Split-Path -Parent $PSScriptRoot }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$installPrefix = $InstallRoot + [IO.Path]::DirectorySeparatorChar
$logsDir = Join-Path $InstallRoot "logs"
$runtimeDir = Join-Path $InstallRoot "data\runtime"
$repairLog = Join-Path $logsDir "repair-agent.log"
$repairStatePath = Join-Path $runtimeDir "repair-state.json"
$script:repairAutostartChecked = $false
$script:repairAutostartRepaired = $false
$trustPolicyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $trustPolicyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $trustPolicyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "The Windows maintenance policy component is missing or unsafe."
}
. $trustPolicyPath
Assert-DianPathChainNoReparsePoints $InstallRoot "Installation root"
Assert-DianPathChainNoReparsePoints $PSScriptRoot "Installed maintenance tools"
foreach ($writeBoundary in @($logsDir, $runtimeDir, (Join-Path $InstallRoot "config"))) {
  Assert-DianPathChainNoReparsePoints $writeBoundary "Repair write boundary"
}

function Write-Utf8NoBomJson([string]$Path, [object]$Value) {
  $parent = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $temporary = Join-Path $parent (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString("N"))
  $encoding = New-Object Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 8) + "`n"), $encoding)
  Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Write-RepairLog([string]$Message) {
  New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
  Add-Content -LiteralPath $repairLog -Encoding UTF8 -Value ("{0} {1}" -f [DateTime]::UtcNow.ToString("o"), $Message)
}

function Write-RepairState(
  [string]$State,
  [string]$Code,
  [string]$Message,
  [string[]]$RepairedFiles = @(),
  [string]$BackupPath = "",
  [string]$VerifiedExtensionId = "",
  [string]$InstallId = ""
) {
  Write-Utf8NoBomJson $repairStatePath ([ordered]@{
    schema_version = 1
    state = $State
    code = $Code
    message = $Message
    checked_at = [DateTime]::UtcNow.ToString("o")
    repaired_files = @($RepairedFiles)
    backup_path = if ($BackupPath) { $BackupPath } else { $null }
    verified_extension_id = if ($VerifiedExtensionId) { $VerifiedExtensionId } else { $null }
    install_id = if ($InstallId) { $InstallId } else { $null }
    service_verified = ($State -eq "healthy")
    autostart_checked = $script:repairAutostartChecked
    autostart_repaired = $script:repairAutostartRepaired
  })
}

function Invoke-AuthenticatedRepairProbe(
  [string]$ExtensionId,
  [string]$ExpectedInstallId,
  [string]$ExpectedVersion
) {
  if ($ExtensionId -notmatch '^[a-p]{32}$' -or $ExpectedInstallId -notmatch '^[a-f0-9]{32}$') {
    throw "The trust receipt cannot be used for authenticated verification."
  }
  $origin = "chrome-extension://$ExtensionId"
  $session = $null
  $accessToken = ""
  try {
    $session = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/auth/session" -f $Port) -Method Post -TimeoutSec 5 `
      -Headers @{ Origin = $origin; "X-Dian-Agent" = "2"; "X-Dian-Agent-Extension-Version" = $ExpectedVersion } -ContentType "application/json" `
      -Body (@{ extension_id = $ExtensionId; extension_version = $ExpectedVersion } | ConvertTo-Json -Compress)
    if ($session.ok -ne $true -or [string]$session.install_id -ne $ExpectedInstallId -or
        [string]$session.token_type -ne "DianAgent" -or -not [string]$session.access_token) {
      throw "session_receipt_mismatch"
    }
    $accessToken = [string]$session.access_token
    $system = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/system/status" -f $Port) -Method Get -TimeoutSec 8 `
      -Headers @{ Origin = $origin; "X-Dian-Agent-Token" = $accessToken; "X-Dian-Agent-Extension-Version" = $ExpectedVersion }
    if ([string]$system.agent_version -ne $ExpectedVersion) { throw "protected_status_version_mismatch" }
    return [pscustomobject]@{ Verified = $true; InstallId = $ExpectedInstallId; ExtensionId = $ExtensionId }
  } catch {
    # Never include the request headers or token in logs/receipts.
    throw "Authenticated local API verification failed after repair."
  } finally {
    $accessToken = ""
    $session = $null
  }
}

function Stop-ExactInstalledAgent([string]$AgentPath, [int]$TimeoutSeconds = 15) {
  $expectedPath = [IO.Path]::GetFullPath($AgentPath)
  $owned = @(Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $expectedPath })
  foreach ($process in $owned) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $remaining = @(Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $expectedPath })
    if ($remaining.Count -eq 0) { return @($owned | ForEach-Object { [int]$_.ProcessId }) }
    Start-Sleep -Milliseconds 200
  } while ((Get-Date) -lt $deadline)
  throw "The exact installed Agent process did not exit during repair."
}

function Start-ExactInstalledAgent([string]$AgentPath, [string]$ExpectedVersion, [int]$TimeoutSeconds = 40) {
  $expectedPath = [IO.Path]::GetFullPath($AgentPath)
  if (-not $expectedPath.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase) -or
      -not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) {
    throw "The repaired Agent executable is missing or outside this installation."
  }
  $names = @("DIAN_AGENT_INSTALL_ROOT", "DIAN_AGENT_DATA_DIR", "DIAN_AGENT_LOG_DIR", "BRIDGE_PORT")
  $previous = @{}
  foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
  $launched = $null
  try {
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_INSTALL_ROOT", $InstallRoot, "Process")
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_DATA_DIR", (Join-Path $InstallRoot "data"), "Process")
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_LOG_DIR", (Join-Path $InstallRoot "logs"), "Process")
    [Environment]::SetEnvironmentVariable("BRIDGE_PORT", [string]$Port, "Process")
    $launched = Start-Process -FilePath $expectedPath -WorkingDirectory (Split-Path -Parent $expectedPath) -WindowStyle Hidden -PassThru
  } finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process") }
  }
  try {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
      Start-Sleep -Milliseconds 500
      try {
        $health = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health/live" -f $Port) -TimeoutSec 2
        if ($health.status -eq "ok" -and [string]$health.version -eq $ExpectedVersion) {
          $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
          if ($listeners.Count -gt 0) {
            $foreign = @($listeners | Where-Object {
              $owner = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f [int]$_.OwningProcess) -ErrorAction SilentlyContinue
              -not $owner -or -not $owner.ExecutablePath -or
                -not ([IO.Path]::GetFullPath([string]$owner.ExecutablePath)).Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)
            })
            if ($foreign.Count -eq 0) { return }
          }
        }
      } catch { }
    } while ((Get-Date) -lt $deadline)
  } finally {
    if ($launched) { $launched.Dispose() }
  }
  [void](Stop-ExactInstalledAgent $expectedPath)
  throw "The repaired Agent did not become healthy under the maintenance lock."
}

function Invoke-TrustRepair([string]$AgentPath, [string]$ManifestPath) {
  $process = $null
  try {
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $AgentPath
    $startInfo.Arguments = '--repair-local-api-trust "{0}" "{1}"' -f $ManifestPath, $InstallRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "The Agent trust repair process could not be started." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
      try { $process.Kill() } catch { }
      return [pscustomobject]@{ Success = $false; ExitCode = 124; Output = ""; Error = "trust_repair_timeout"; Receipt = $null }
    }
    [void]$process.WaitForExit()
    # Start-Process can expose a null ExitCode for a windowed PyInstaller EXE.
    # System.Diagnostics.Process retains the real code and drains both pipes,
    # so a cold one-file launch cannot be mistaken for an empty success.
    $exitCode = [int]$process.ExitCode
    $stdout = [string]$stdoutTask.Result
    $stderr = [string]$stderrTask.Result
    $receiptValid = $false
    $receipt = $null
    if ($exitCode -eq 0) {
      try {
        $receipt = $stdout | ConvertFrom-Json
        $receiptValid = ($receipt.ok -eq $true -and [string]$receipt.install_id -match '^[a-f0-9]{32}$' -and
          [string]$receipt.extension_id -match '^[a-p]{32}$')
      } catch { }
    }
    if ($exitCode -eq 0 -and -not $receiptValid -and -not $stderr) { $stderr = "trust_repair_receipt_missing" }
    return [pscustomobject]@{
      Success = ($exitCode -eq 0 -and $receiptValid)
      ExitCode = $exitCode
      Output = $stdout.Trim()
      Error = $stderr.Trim()
      Receipt = $receipt
    }
  } finally {
    if ($process) { $process.Dispose() }
  }
}

$maintenanceMutex = $null
$maintenanceLockHeld = $false
$repairOperationStarted = $false
try {
  $maintenanceMutex = Enter-DianMaintenanceLock $InstallRoot $MaintenanceLockTimeoutSeconds
  $maintenanceLockHeld = $true
  $transactionRecovery = Invoke-DianRecoverInstallTransaction $InstallRoot 1
  if ($transactionRecovery.Recovered) {
    Write-Host ("Recovered interrupted install transaction {0} ({1})." -f $transactionRecovery.TransactionId, $transactionRecovery.Action) -ForegroundColor Yellow
  }
  $toolsTransactionRecovery = Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1
  if ($toolsTransactionRecovery.Recovered) {
    Write-Host ("Recovered interrupted maintenance-tools transaction {0} ({1})." -f $toolsTransactionRecovery.TransactionId, $toolsTransactionRecovery.Action) -ForegroundColor Yellow
  }
  $repairOperationStarted = $true
  $markerPath = Join-Path $InstallRoot ".dian-agent-install.json"
  if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { throw "The Dian Agent installation marker is missing." }
  $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($marker.product -ne "DianAgent" -or $marker.schema -ne 1 -or
      -not ([IO.Path]::GetFullPath([string]$marker.install_root)).Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The Dian Agent installation marker does not match this installation."
  }

  $currentPointer = Join-Path $InstallRoot "current.json"
  if (Test-Path -LiteralPath $currentPointer -PathType Leaf) {
    $current = Get-Content -LiteralPath $currentPointer -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$current.version
    $versionRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot ([string]$current.version_path)))
    if (-not $versionRoot.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "The active version pointer is unsafe." }
    $agentPath = Join-Path $versionRoot "program\DianAgent.exe"
  } else {
    $versionFile = Join-Path $InstallRoot "current-version.txt"
    if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) { throw "The active Dian Agent version is missing." }
    $version = (Get-Content -LiteralPath $versionFile -Raw -Encoding ASCII).Trim()
    $versionRoot = Join-Path $InstallRoot ("app\{0}" -f $version)
    $agentPath = Join-Path $versionRoot "DianAgent.exe"
  }
  if ($version -notmatch '^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$') { throw "The active Dian Agent version is invalid." }
  $agentPath = [IO.Path]::GetFullPath($agentPath)
  if (-not $agentPath.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase) -or
      -not (Test-Path -LiteralPath $agentPath -PathType Leaf)) { throw "The active Dian Agent executable is missing or unsafe." }
  Assert-DianNoReparsePoints $versionRoot "Active Agent version"
  $manifestPath = Join-Path $InstallRoot "extension-current\manifest.json"
  if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "The installed extension manifest is missing." }
  Assert-DianNoReparsePoints (Join-Path $InstallRoot "extension-current") "Installed browser extension"

  if (-not $SkipAutostart) {
    # Validate every automatic entrypoint before the first bootstrap write. A
    # foreign task/shortcut with a colliding name is never claimed by repair.
    $existingKeepAliveTask = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
    if ($existingKeepAliveTask -and -not (Test-DianOwnedKeepAliveTask $existingKeepAliveTask $InstallRoot)) {
      throw "The existing keepalive task is not owned by this installation."
    }
    $startupShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
    if (Test-Path -LiteralPath $startupShortcutPath -PathType Leaf) {
      if ((Get-Item -LiteralPath $startupShortcutPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The existing startup shortcut is a reparse point."
      }
      $existingStartupShortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($startupShortcutPath)
      if (-not (Test-DianOwnedShortcut $existingStartupShortcut $startupShortcutPath $InstallRoot)) {
        throw "The existing startup shortcut is not owned by this installation."
      }
    }
    Assert-DianDevelopmentAutostartOwnership
    $script:repairAutostartChecked = $true
  }
  [void](Install-DianRecoveryBootstrapFiles $InstallRoot $PSScriptRoot)

  $authPath = Join-Path $InstallRoot "config\local_api_auth.json"
  $trustPath = Join-Path $InstallRoot "config\trusted_extension_ids.json"
  $initialization = Invoke-TrustRepair $agentPath $manifestPath
  if (-not $initialization.Success) {
    $detail = if ($initialization.Error) { $initialization.Error } else { "exit_code:$($initialization.ExitCode)" }
    throw "The Agent could not safely repair local trust: $detail"
  }
  $repairedFiles = @($initialization.Receipt.repaired_files | ForEach-Object { [string]$_ })
  $backupDir = [string]$initialization.Receipt.backup_path
  if ($initialization.Receipt.repaired -eq $true -and -not $backupDir) { throw "The trust repair receipt omitted its backup path." }
  if (-not (Test-Path -LiteralPath $authPath -PathType Leaf) -or -not (Test-Path -LiteralPath $trustPath -PathType Leaf)) {
    throw "The Agent trust repair returned success without both trust records."
  }
  $verifiedExtensionId = [string]$initialization.Receipt.extension_id
  $verifiedInstallId = [string]$initialization.Receipt.install_id
  if ($verifiedExtensionId -notmatch '^[a-p]{32}$' -or $verifiedInstallId -notmatch '^[a-f0-9]{32}$') {
    throw "The Agent trust initializer did not return a usable verification identity."
  }
  $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  foreach ($trustFile in @($authPath, $trustPath)) {
    & icacls.exe $trustFile /inheritance:r /grant:r "${currentIdentity}:(F)" "SYSTEM:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the repaired local trust file ACL: $trustFile" }
  }

  if (-not $SkipRestart) {
    # Repair is explicitly a restart operation. A same-version process may
    # still hold pre-repair state, so liveness alone cannot authorize reuse.
    # Stop only this installation's canonical executable, never a name match.
    [void](Stop-ExactInstalledAgent $agentPath)
    Start-ExactInstalledAgent $agentPath $version
    $health = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health/live" -f $Port) -TimeoutSec 5
    if ($health.status -ne "ok" -or [string]$health.version -ne $version) {
      throw "The Agent restart did not return exact version health evidence."
    }
    [void](Invoke-AuthenticatedRepairProbe $verifiedExtensionId $verifiedInstallId $version)
  }

  $repairedFiles = @($repairedFiles) + @(
    "bootstrap/recovery_bootstrap.ps1",
    "bootstrap/recovery_bootstrap.vbs",
    "bootstrap/windows_trust_policy.ps1"
  )
  if (-not $SkipAutostart) {
    # Repair is the explicit self-heal operation, so restore both automatic
    # entrypoints even when one or both were deleted. They target the stable
    # VBS wrapper outside tools and cannot flash a PowerShell console at logon.
    Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true
    $script:repairAutostartRepaired = $true
    $removedDevelopmentAutostart = @(Remove-DianDevelopmentAutostartArtifacts)
    $repairedFiles = @($repairedFiles) + @(
      "scheduled_task:DianAgentKeepAlive",
      "startup_shortcut:DianAgent.lnk"
    ) + @($removedDevelopmentAutostart)
  }

  $startupStatePath = Join-Path $runtimeDir "startup-state.json"
  Write-Utf8NoBomJson $startupStatePath ([ordered]@{
    schema_version = 1
    state = if ($SkipAutostart) { "not_checked" } elseif ($SkipRestart) { "configured" } else { "healthy" }
    state_label = if ($SkipAutostart) { "Autostart was not checked or changed by this repair" } elseif ($SkipRestart) { "Autostart was repaired; service restart was skipped" } else { "Agent repair and autostart are healthy" }
    autostart_enabled = if ($SkipAutostart) { $null } else { $true }
    keepalive_enabled = if ($SkipAutostart) { $null } else { $true }
    hidden_launcher = if ($SkipAutostart) { $null } else { $true }
    source = if ($SkipAutostart) { "release_repair_skip_autostart" } else { "release_repair" }
    task_name = if ($SkipAutostart) { $null } else { "DianAgentKeepAlive" }
    last_checked_at = if ($SkipAutostart) { $null } else { [DateTime]::UtcNow.ToString("o") }
    last_healthy_at = if ($SkipAutostart -or $SkipRestart) { $null } else { [DateTime]::UtcNow.ToString("o") }
    last_recovery_at = if ($SkipAutostart) { $null } else { [DateTime]::UtcNow.ToString("o") }
    last_error = if ($SkipAutostart) { "autostart_not_checked" } elseif ($SkipRestart) { "restart_skipped" } else { $null }
  })

  $message = if ($SkipRestart) {
    "Local trust was repaired; service verification was intentionally skipped."
  } elseif ($repairedFiles.Count -gt 0) {
    "Local trust, extension pairing and protected API access were repaired and verified."
  } else {
    "Local trust, extension pairing and protected API access were verified."
  }
  $state = if ($SkipRestart) { "trust_verified" } else { "healthy" }
  $code = if ($SkipRestart) { "trust_repair_staged" } else { "repair_verified" }
  Write-RepairState $state $code $message $repairedFiles $backupDir $verifiedExtensionId $verifiedInstallId
  # The durable repair receipt is authoritative. A diagnostics-only append
  # failure after that commit must not overwrite it with repair_failed.
  try {
    Write-RepairLog $message
  } catch {
    Write-Warning "Repair succeeded, but its diagnostics log could not be appended: $($_.Exception.Message)"
  }
  Write-Host $message -ForegroundColor Green
  exit 0
} catch {
  $message = $_.Exception.Message
  # Failing to acquire the shared maintenance lock means another installer,
  # repair or uninstall owns the installation. Do not mutate its state/logs.
  if ($maintenanceLockHeld -and $repairOperationStarted) {
    try { Write-RepairState "error" "repair_failed" $message } catch { }
    try { Write-RepairLog ("Repair failed: {0}" -f $message) } catch { }
  }
  Write-Error $message
  exit 1
} finally {
  if ($maintenanceLockHeld) {
    Exit-DianMaintenanceLock $maintenanceMutex
    $maintenanceLockHeld = $false
  }
}
