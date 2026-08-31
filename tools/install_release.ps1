[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [string]$SourceRoot = "",
  [switch]$SkipAutostart,
  [switch]$SkipLaunch,
  [ValidateRange(5, 300)][int]$ExtensionReportTimeoutSeconds = 90,
  [ValidateRange(1, 300)][int]$MaintenanceLockTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$trustPolicyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $trustPolicyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $trustPolicyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "Windows trust policy component is missing or unsafe."
}
. $trustPolicyPath

function Get-FullPath([string]$Path) {
  return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Test-ReparsePoint([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-NoReparsePoints([string]$Path, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $queue = New-Object 'Collections.Generic.Queue[string]'
  $queue.Enqueue((Get-FullPath $Path))
  while ($queue.Count -gt 0) {
    $current = $queue.Dequeue()
    $item = Get-Item -LiteralPath $current -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "$Label contains a symbolic link, junction or other reparse point: $current"
    }
    if ($item.PSIsContainer) {
      foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
          throw "$Label contains a symbolic link, junction or other reparse point: $($child.FullName)"
        }
        if ($child.PSIsContainer) { $queue.Enqueue($child.FullName) }
      }
    }
  }
}

function Get-MaintenanceMutexName([string]$Path) {
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes((Get-FullPath $Path).ToUpperInvariant()))
    $rootHash = [BitConverter]::ToString($digest).Replace("-", "").Substring(0, 16)
  } finally {
    $sha.Dispose()
  }
  return "Local\DianAgentMaintenance-$rootHash"
}

function Enter-MaintenanceLock([string]$Path, [int]$TimeoutSeconds) {
  $mutex = New-Object Threading.Mutex($false, (Get-MaintenanceMutexName $Path))
  $acquired = $false
  try {
    try {
      $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
      # The previous installer crashed. Windows transferred ownership to this
      # process, so the abandoned lock is safe to recover and reuse.
      $acquired = $true
    }
    if (-not $acquired) {
      throw "Another Dian Agent install, repair or removal is already in progress for: $Path"
    }
    return $mutex
  } catch {
    $mutex.Dispose()
    throw
  }
}

function Assert-SafeInstallRoot([string]$Path) {
  $full = Get-FullPath $Path
  $root = [IO.Path]::GetPathRoot($full).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $blocked = @(
    $root,
    (Get-FullPath ([Environment]::GetFolderPath("UserProfile"))),
    (Get-FullPath ([Environment]::GetFolderPath("LocalApplicationData")))
  )
  if ($blocked -contains $full) { throw "Unsafe install root: $full" }
  return $full
}

function Copy-DirectoryContents([string]$Source, [string]$Destination) {
  Assert-NoReparsePoints $Source "Release directory"
  New-Item -ItemType Directory -Force -Path $Destination | Out-Null
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
  }
}

function New-DirectoryStage([string]$Parent, [string]$Label) {
  $fullParent = (Get-FullPath $Parent) + [IO.Path]::DirectorySeparatorChar
  $stage = Get-FullPath (Join-Path $Parent (".{0}-stage-{1}" -f $Label, [Guid]::NewGuid().ToString("N")))
  if (-not $stage.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe directory stage: $stage"
  }
  New-Item -ItemType Directory -Path $stage | Out-Null
  return $stage
}

function Remove-OrphanInstallStages([string]$Root) {
  $rootFull = Get-FullPath $Root
  $specifications = @(
    @{ Parent = Join-Path $rootFull "app"; Pattern = '^\.app-[0-9A-Za-z.+-]+-stage-[0-9a-f]{32}$' },
    @{ Parent = Join-Path $rootFull "extension"; Pattern = '^\.extension-[0-9A-Za-z.+-]+-stage-[0-9a-f]{32}$' },
    @{ Parent = $rootFull; Pattern = '^\.(?:extension-current|tools)-stage-[0-9a-f]{32}$' }
  )
  $candidates = New-Object 'Collections.Generic.List[string]'
  foreach ($specification in $specifications) {
    $parent = Get-FullPath ([string]$specification.Parent)
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not ($parent + [IO.Path]::DirectorySeparatorChar).StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
      throw "Unsafe orphan-stage parent: $parent"
    }
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { continue }
    if (Test-ReparsePoint $parent) { throw "Orphan-stage parent is a reparse point: $parent" }
    foreach ($item in Get-ChildItem -LiteralPath $parent -Force) {
      if ($item.Name -notmatch [string]$specification.Pattern) { continue }
      if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Orphan installation stage is unsafe: $($item.FullName)"
      }
      Assert-NoReparsePoints $item.FullName "Orphan installation stage"
      $candidates.Add([IO.Path]::GetFullPath($item.FullName))
    }
  }
  # Validate the complete set before deleting the first directory.
  foreach ($candidate in $candidates) {
    $parent = Get-FullPath (Split-Path -Parent $candidate)
    if ($parent -ne $rootFull -and $parent -ne (Get-FullPath (Join-Path $rootFull "app")) -and
        $parent -ne (Get-FullPath (Join-Path $rootFull "extension"))) {
      throw "Orphan installation stage escaped its managed parent: $candidate"
    }
  }
  foreach ($candidate in $candidates) { Remove-Item -LiteralPath $candidate -Recurse -Force }
}

function Remove-CommittedProgramDebris([string]$Root) {
  $rootFull = Get-FullPath $Root
  $specifications = @(
    @{ Parent = Join-Path $rootFull "app"; Pattern = '^\.[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?-backup-[0-9a-f]{32}$' },
    @{ Parent = Join-Path $rootFull "extension"; Pattern = '^\.[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?-backup-[0-9a-f]{32}$' },
    @{ Parent = $rootFull; Pattern = '^\.(?:extension-current|tools)-backup-[0-9a-f]{32}$' },
    @{ Parent = $rootFull; Pattern = '^\.extension-backup-[0-9]+(?:\.[0-9]+){2}-[0-9]{8}-[0-9]{6}$' }
  )
  $candidates = New-Object 'Collections.Generic.List[string]'
  foreach ($specification in $specifications) {
    $parent = Get-FullPath ([string]$specification.Parent)
    if (-not (Test-Path -LiteralPath $parent -PathType Container) -or (Test-ReparsePoint $parent)) { continue }
    foreach ($item in Get-ChildItem -LiteralPath $parent -Force) {
      if ($item.Name -notmatch [string]$specification.Pattern) { continue }
      if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Committed program debris is unsafe: $($item.FullName)"
      }
      Assert-NoReparsePoints $item.FullName "Committed program debris"
      $candidates.Add([IO.Path]::GetFullPath($item.FullName))
    }
  }
  foreach ($candidate in $candidates) {
    $parent = Get-FullPath (Split-Path -Parent $candidate)
    if ($parent -ne $rootFull -and $parent -ne (Get-FullPath (Join-Path $rootFull "app")) -and
        $parent -ne (Get-FullPath (Join-Path $rootFull "extension"))) {
      throw "Committed program debris escaped its managed parent: $candidate"
    }
  }
  foreach ($candidate in $candidates) { Remove-Item -LiteralPath $candidate -Recurse -Force }
}

function Test-OwnedKeepAliveTask([object]$Task, [string]$Root) {
  return Test-DianOwnedKeepAliveTask $Task $Root
}

function Test-OwnedShortcut([object]$Shortcut, [string]$ShortcutPath, [string]$Root) {
  return Test-DianOwnedShortcut $Shortcut $ShortcutPath $Root
}

function Remove-OwnedAutostartArtifacts([string]$Root) {
  $task = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
  if ($task -and (Test-OwnedKeepAliveTask $task $Root)) {
    Unregister-ScheduledTask -TaskName "DianAgentKeepAlive" -Confirm:$false
  }
  $shell = New-Object -ComObject WScript.Shell
  foreach ($path in @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk")
  )) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
      $shortcut = $shell.CreateShortcut($path)
      if (Test-OwnedShortcut $shortcut $path $Root) { Remove-Item -LiteralPath $path -Force }
    }
  }
}

function Remove-CommittedDevelopmentAutostartArtifacts {
  try {
    $removed = @(Remove-DianDevelopmentAutostartArtifacts)
    if ($removed.Count -gt 0) {
      Write-Host ("Removed conflicting source-development autostart: {0}" -f ($removed -join ", ")) -ForegroundColor Yellow
    }
  } catch {
    # Program trees and exact-version health may already be committed.  Never
    # misreport that durable installation as rolled back because an unrelated
    # task changed after preflight; retain it and make the conflict explicit.
    Write-Warning "The formal installation is committed, but a source-development autostart entry could not be removed safely: $($_.Exception.Message)"
  }
}

function New-PreparedDirectoryActivation([string]$Prepared, [string]$Target) {
  $preparedFull = Get-FullPath $Prepared
  $targetFull = Get-FullPath $Target
  $parent = Get-FullPath (Split-Path -Parent $targetFull)
  $prefix = $parent + [IO.Path]::DirectorySeparatorChar
  if (-not $preparedFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
      -not $targetFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
      $preparedFull.Equals($targetFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe prepared-directory activation: $targetFull"
  }
  Assert-NoReparsePoints $preparedFull "Prepared install tree"
  if (Test-Path -LiteralPath $targetFull) {
    if (-not (Test-Path -LiteralPath $targetFull -PathType Container) -or (Test-ReparsePoint $targetFull)) {
      throw "Installed directory is not a safe owned directory: $targetFull"
    }
    Assert-NoReparsePoints $targetFull "Installed directory"
  }
  $backup = Get-FullPath (Join-Path $parent (".{0}-backup-{1}" -f (Split-Path -Leaf $targetFull), [Guid]::NewGuid().ToString("N")))
  $hadTarget = Test-Path -LiteralPath $targetFull -PathType Container
  return [pscustomobject]@{ Target = $targetFull; Prepared = $preparedFull; Backup = $backup; HadTarget = $hadTarget }
}

function Activate-PreparedDirectory([object]$Activation) {
  if (-not $Activation) { throw "Prepared-directory activation plan is missing." }
  $preparedFull = Get-FullPath ([string]$Activation.Prepared)
  $targetFull = Get-FullPath ([string]$Activation.Target)
  $backup = Get-FullPath ([string]$Activation.Backup)
  $currentHadTarget = Test-Path -LiteralPath $targetFull -PathType Container
  if ($currentHadTarget -ne [bool]$Activation.HadTarget -or (Test-Path -LiteralPath $backup)) {
    throw "Prepared-directory activation state changed after the durable transaction journal was written."
  }
  try {
    if ($Activation.HadTarget) { [IO.Directory]::Move($targetFull, $backup) }
    [IO.Directory]::Move($preparedFull, $targetFull)
  } catch {
    if (-not (Test-Path -LiteralPath $targetFull) -and (Test-Path -LiteralPath $backup -PathType Container)) {
      [IO.Directory]::Move($backup, $targetFull)
    }
    throw
  }
  return $Activation
}

function Invoke-InstallFault([string]$Point) {
  if ([string]$env:DIAN_AGENT_INSTALL_CRASH_POINT -eq $Point) {
    # Test-only hard termination: unlike a catchable injected exception this
    # proves that the next process can recover from the durable journal alone.
    [Diagnostics.Process]::GetCurrentProcess().Kill()
    Start-Sleep -Seconds 30
  }
  if ([string]$env:DIAN_AGENT_INSTALL_FAULT_POINT -eq $Point) {
    throw "Injected installer failure at: $Point"
  }
}

function New-InstallerFileSnapshot([string]$Path) {
  $full = [IO.Path]::GetFullPath($Path)
  if (Test-Path -LiteralPath $full) {
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Transaction snapshot target is not a safe regular file: $full"
    }
    $bytes = [IO.File]::ReadAllBytes($full)
    return [ordered]@{ path = $full; existed = $true; data_base64 = [Convert]::ToBase64String($bytes); sha256 = Get-DianSha256Hex $bytes }
  }
  return [ordered]@{ path = $full; existed = $false; data_base64 = ""; sha256 = "" }
}

function New-InstallerTaskSnapshot {
  $task = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
  if (-not $task) { return [ordered]@{ existed = $false; data_base64 = ""; sha256 = "" } }
  $xmlBytes = (New-Object Text.UTF8Encoding($false)).GetBytes([string](Export-ScheduledTask -TaskName "DianAgentKeepAlive"))
  return [ordered]@{ existed = $true; data_base64 = [Convert]::ToBase64String($xmlBytes); sha256 = Get-DianSha256Hex $xmlBytes }
}

function New-InstallerShortcutSnapshot([string]$Path) {
  return New-InstallerFileSnapshot $Path
}

function Wait-FileUnlocked([string]$Path, [int]$TimeoutSeconds = 15) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $stream = $null
    try {
      $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
      return
    } catch [IO.IOException] {
      if ((Get-Date) -ge $deadline) {
        throw "Installed Agent did not release its executable within $TimeoutSeconds seconds: $Path"
      }
      Start-Sleep -Milliseconds 200
    } finally {
      if ($stream) { $stream.Dispose() }
    }
  } while ($true)
}

function Start-InstalledAgentUnderMaintenanceLock(
  [string]$AgentPath,
  [string]$ExpectedVersion,
  [string]$TargetRoot,
  [int]$Port = 8765,
  [int]$TimeoutSeconds = 60
) {
  $expectedPath = [IO.Path]::GetFullPath($AgentPath)
  $rootPrefix = (Get-FullPath $TargetRoot) + [IO.Path]::DirectorySeparatorChar
  if (-not $expectedPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
      -not (Test-Path -LiteralPath $expectedPath -PathType Leaf) -or (Test-ReparsePoint $expectedPath)) {
    throw "The prepared Agent executable is missing or outside the installation root."
  }

  $environmentNames = @("DIAN_AGENT_INSTALL_ROOT", "DIAN_AGENT_DATA_DIR", "DIAN_AGENT_LOG_DIR", "BRIDGE_PORT")
  $previousEnvironment = @{}
  foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
  }
  $launched = $null
  try {
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_INSTALL_ROOT", $TargetRoot, "Process")
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_DATA_DIR", (Join-Path $TargetRoot "data"), "Process")
    [Environment]::SetEnvironmentVariable("DIAN_AGENT_LOG_DIR", (Join-Path $TargetRoot "logs"), "Process")
    [Environment]::SetEnvironmentVariable("BRIDGE_PORT", [string]$Port, "Process")
    $launched = Start-Process -FilePath $expectedPath -WorkingDirectory (Split-Path -Parent $expectedPath) `
      -WindowStyle Hidden -PassThru
  } finally {
    foreach ($name in $environmentNames) {
      [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
  }

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  try {
    while ((Get-Date) -lt $deadline) {
      Start-Sleep -Milliseconds 500
      $healthy = $false
      try {
        $health = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health/live" -f $Port) -TimeoutSec 2
        $healthy = ($health.status -eq "ok" -and [string]$health.version -eq $ExpectedVersion)
      } catch { }
      if (-not $healthy) { continue }

      $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
      if ($listeners.Count -eq 0) { continue }
      $allOwned = $true
      foreach ($listener in $listeners) {
        $owner = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f [int]$listener.OwningProcess) -ErrorAction SilentlyContinue
        if (-not $owner -or -not $owner.ExecutablePath -or
            -not ([IO.Path]::GetFullPath([string]$owner.ExecutablePath)).Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
          $allOwned = $false
          break
        }
      }
      if ($allOwned) { return }
    }
  } finally {
    if ($launched) { $launched.Dispose() }
  }

  Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  throw "Dian Agent did not become healthy under the installer maintenance lock within $TimeoutSeconds seconds."
}

function Write-InstallVerification([string]$Path, [object]$Value) {
  $encoding = New-Object Text.UTF8Encoding($false)
  $bytes = $encoding.GetBytes((($Value | ConvertTo-Json -Depth 6) + "`n"))
  Write-DianAtomicBytes $Path $bytes
}

function Invoke-PackagedTrustProvisioner(
  [string]$AgentPath,
  [string]$ManifestPath,
  [string]$TargetRoot,
  [int]$TimeoutSeconds = 30
) {
  $process = $null
  try {
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $AgentPath
    $startInfo.Arguments = '--initialize-local-api-trust "{0}" "{1}"' -f $ManifestPath, $TargetRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "The packaged Agent trust process could not be started." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      # A release built before the installer-only command may start its server
      # instead of exiting. Stop only executables whose canonical path is the
      # exact release-media Agent that this installer invoked.
      $expectedPath = [IO.Path]::GetFullPath($AgentPath)
      Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $expectedPath } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      throw "The packaged Agent did not finish local trust initialization within $TimeoutSeconds seconds."
    }
    [void]$process.WaitForExit()
    return [pscustomobject]@{
      ExitCode = [int]$process.ExitCode
      Output = ([string]$stdoutTask.Result).Trim()
      Error = ([string]$stderrTask.Result).Trim()
    }
  } finally {
    if ($process) { $process.Dispose() }
  }
}

function Wait-ExtensionReport(
  [string]$Path,
  [string[]]$AcceptedExtensionIds,
  [string]$TargetVersion,
  [DateTimeOffset]$StartedAt,
  [int]$TimeoutSeconds
) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $lastReason = "report_missing"
  do {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
      try {
        $report = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $assessment = Test-DianExtensionReport $report $AcceptedExtensionIds $TargetVersion $StartedAt
        $lastReason = $assessment.Reason
        if ($assessment.Ready) {
          return [pscustomobject]@{
            Ready = $true
            Reason = $assessment.Reason
            AcceptedExtensionId = $assessment.AcceptedExtensionId
            ReportedAt = $assessment.ReportedAt
          }
        }
      } catch {
        $lastReason = "report_unreadable"
      }
    }
    Start-Sleep -Milliseconds 750
  } while ((Get-Date) -lt $deadline)
  return [pscustomobject]@{ Ready = $false; Reason = $lastReason; AcceptedExtensionId = ""; ReportedAt = $null }
}

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = Assert-SafeInstallRoot $InstallRoot
# Refuse a custom root whose existing ancestor chain crosses a junction before
# acquiring the maintenance mutex or creating trust/config/program directories.
Assert-DianPathChainNoReparsePoints $InstallRoot "Install root"
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
$SourceRoot = Get-FullPath $SourceRoot

$extensionSource = Join-Path $SourceRoot "extension"
$manifestPath = Join-Path $extensionSource "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
  throw "Extension manifest was not found under: $extensionSource"
}
Assert-NoReparsePoints $extensionSource "Browser extension release tree"
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$version = [string]$manifest.version
if ($version -notmatch '^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$') {
  throw "Invalid release version in extension manifest: $version"
}
$agentCandidates = @(
  (Join-Path $SourceRoot "app\DianAgent.exe"),
  (Join-Path $SourceRoot "dist\agent\DianAgent.exe")
)
$agentSource = $agentCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $agentSource) { throw "DianAgent.exe was not found in app or dist\agent." }
if (Test-ReparsePoint $agentSource) { throw "DianAgent.exe must not be a symbolic link or reparse point." }
$agentVersionInfo = (Get-Item -LiteralPath $agentSource).VersionInfo
$agentFileVersion = [string]$agentVersionInfo.FileVersion
$agentProductVersion = [string]$agentVersionInfo.ProductVersion
if ($agentFileVersion -ne $version -or $agentProductVersion -ne $version) {
  throw "DianAgent.exe version does not match the extension manifest. Expected=$version FileVersion=$agentFileVersion ProductVersion=$agentProductVersion. Rebuild the release Agent before installing."
}

$requiredTools = @("start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1", "repair_agent.ps1", "repair_agent.vbs", "windows_trust_policy.ps1", "uninstall_release.ps1", "sync_release_tools.ps1")
foreach ($name in $requiredTools) {
  $toolSource = Join-Path $PSScriptRoot $name
  if (-not (Test-Path -LiteralPath $toolSource -PathType Leaf)) {
    throw "Required installer component is missing: tools\$name"
  }
  if (Test-ReparsePoint $toolSource) { throw "Required installer component is a reparse point: tools\$name" }
}
$updaterSource = @(
  (Join-Path $PSScriptRoot "DianAgentUpdater.exe"),
  (Join-Path $SourceRoot "dist\agent\DianAgentUpdater.exe")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $updaterSource) { throw "DianAgentUpdater.exe is missing from the release." }
if (Test-ReparsePoint $updaterSource) { throw "DianAgentUpdater.exe must not be a symbolic link or reparse point." }

if (Test-ReparsePoint $InstallRoot) { throw "Install root must not be a symbolic link or junction: $InstallRoot" }
$installStartedAt = [DateTimeOffset]::UtcNow
$isUpgrade = $false

$appTarget = Join-Path $InstallRoot ("app\{0}" -f $version)
$extensionTarget = Join-Path $InstallRoot ("extension\{0}" -f $version)
$stableExtension = Join-Path $InstallRoot "extension-current"
$toolsTarget = Join-Path $InstallRoot "tools"
$installedToolNames = @("install_release.ps1", "uninstall_release.ps1", "start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1", "repair_agent.ps1", "repair_agent.vbs", "windows_trust_policy.ps1", "sync_release_tools.ps1")
$maintenanceMutex = Enter-MaintenanceLock $InstallRoot $MaintenanceLockTimeoutSeconds
$maintenanceLockHeld = $true
$restartRestoredAfterFailure = $false
$journalWritten = $false
$preparedStages = @()
$ownedAgentWasRunning = $false
try {
  $recovery = Invoke-DianRecoverInstallTransaction $InstallRoot 1
  if ($recovery.Recovered) {
    Write-Warning ("Recovered interrupted install transaction {0}: {1}" -f $recovery.TransactionId, $recovery.Action)
  }
  $toolsRecovery = Invoke-DianRecoverReleaseToolsTransaction $InstallRoot 1
  if ($toolsRecovery.Recovered) {
    Write-Warning ("Recovered interrupted maintenance-tools transaction {0}: {1}" -f $toolsRecovery.TransactionId, $toolsRecovery.Action)
  }
  New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
  foreach ($name in @("data", "config", "knowledge", "backup", "logs", "app", "extension", "tools")) {
    $managedDirectory = Join-Path $InstallRoot $name
    if (Test-ReparsePoint $managedDirectory) { throw "Managed install directory must not be a symbolic link or junction: $managedDirectory" }
    New-Item -ItemType Directory -Force -Path $managedDirectory | Out-Null
  }
  $isUpgrade = (Test-Path -LiteralPath (Join-Path $InstallRoot "current-version.txt") -PathType Leaf) -or
    (Test-Path -LiteralPath (Join-Path $InstallRoot "current.json") -PathType Leaf)
  Remove-OrphanInstallStages $InstallRoot
  # Pin the validated executable into the transaction-owned app stage before
  # trust provisioning. The exact staged binary is then used for both trust and
  # activation, so swapping release media after the preflight cannot execute or
  # install a different version.
  $appStage = New-DirectoryStage (Join-Path $InstallRoot "app") "app-$version"
  $preparedStages = @($appStage)
  $stagedAgentSource = Join-Path $appStage "DianAgent.exe"
  Copy-Item -LiteralPath $agentSource -Destination $stagedAgentSource -Force
  Assert-NoReparsePoints $appStage "Prepared Agent tree"
  $stagedAgentVersionInfo = (Get-Item -LiteralPath $stagedAgentSource).VersionInfo
  if ([string]$stagedAgentVersionInfo.FileVersion -ne $version -or
      [string]$stagedAgentVersionInfo.ProductVersion -ne $version) {
    throw "The staged DianAgent.exe version changed after release preflight. Installation was stopped before trust provisioning."
  }
  if (-not $SkipAutostart) {
    $existingKeepAliveTask = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
    if ($existingKeepAliveTask -and -not (Test-OwnedKeepAliveTask $existingKeepAliveTask $InstallRoot)) {
      throw "A scheduled task named DianAgentKeepAlive exists but is not owned by this installation."
    }
    $ownershipShell = New-Object -ComObject WScript.Shell
    foreach ($shortcutPath in @(
      (Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"),
      (Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"),
      (Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk")
    )) {
      if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
        if ((Get-Item -LiteralPath $shortcutPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
          throw "A shortcut at $shortcutPath is a reparse point and cannot be claimed by this installation."
        }
        $existingShortcut = $ownershipShell.CreateShortcut($shortcutPath)
        if (-not (Test-OwnedShortcut $existingShortcut $shortcutPath $InstallRoot)) {
          throw "A shortcut at $shortcutPath exists but is not owned by this installation."
        }
      }
    }
  }
  # Source-development setup uses different task/shortcut names.  Validate
  # their exact Dian Agent ownership before any program mutation, then remove
  # them only after this formal installation has durably committed.
  if (-not $SkipAutostart) { Assert-DianDevelopmentAutostartOwnership }
  # Provision authentication while holding the same per-install lock used by
  # keepalive. Existing credentials and approved extension IDs are preserved.
  $trustCommand = Invoke-PackagedTrustProvisioner $stagedAgentSource $manifestPath $InstallRoot
  if ($trustCommand.ExitCode -ne 0) {
    $detail = if ($trustCommand.Error) { $trustCommand.Error } else { "exit_code:$($trustCommand.ExitCode)" }
    throw "Local API trust initialization failed. Existing authentication state was left unchanged; run Repair Dian Agent or restore the config backup before retrying. $detail"
  }
  try {
    $trustResult = $trustCommand.Output | ConvertFrom-Json
  } catch {
    throw "The packaged Agent returned an invalid local API trust initialization receipt."
  }
  if ($trustResult.ok -ne $true -or [string]$trustResult.agent_version -ne $version -or
      [string]$trustResult.install_id -notmatch '^[a-f0-9]{32}$' -or
      [string]$trustResult.extension_id -notmatch '^[a-p]{32}$') {
    throw "The packaged Agent did not return a valid, version-matched local API trust initialization receipt."
  }
  foreach ($trustFile in @(
    (Join-Path $InstallRoot "config\local_api_auth.json"),
    (Join-Path $InstallRoot "config\trusted_extension_ids.json")
  )) {
    if (-not (Test-Path -LiteralPath $trustFile -PathType Leaf) -or (Test-ReparsePoint $trustFile)) {
      throw "The packaged Agent did not create a safe local trust file: $trustFile"
    }
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $trustFile /inheritance:r /grant:r "${currentIdentity}:(F)" "SYSTEM:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the local trust file ACL: $trustFile" }
  }
  $trustedRegistry = Get-Content -LiteralPath (Join-Path $InstallRoot "config\trusted_extension_ids.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  $acceptedExtensionIds = @(Get-DianAcceptedExtensionIds ([string]$trustResult.extension_id) $trustedRegistry $isUpgrade)

  # Prepare every program-owned tree before stopping the active Agent. A copy,
  # validation or disk-space failure therefore cannot damage a same-version
  # reinstall or leave Chrome pointing at a half-copied directory.
  $extensionVersionStage = New-DirectoryStage (Join-Path $InstallRoot "extension") "extension-$version"
  $preparedStages += $extensionVersionStage
  $stableExtensionStage = New-DirectoryStage $InstallRoot "extension-current"
  $preparedStages += $stableExtensionStage
  $toolsStage = New-DirectoryStage $InstallRoot "tools"
  $preparedStages += $toolsStage
  $ownedAgentWasRunning = $false
  try {
    Copy-DirectoryContents $extensionSource $extensionVersionStage
    Copy-DirectoryContents $extensionSource $stableExtensionStage
    if (Test-Path -LiteralPath $toolsTarget -PathType Container) {
      Copy-DirectoryContents $toolsTarget $toolsStage
    }
    foreach ($name in $installedToolNames) {
      Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $toolsStage $name) -Force
    }
    Copy-Item -LiteralPath $updaterSource -Destination (Join-Path $toolsStage "DianAgentUpdater.exe") -Force
    Assert-NoReparsePoints $extensionVersionStage "Prepared extension tree"
    Assert-NoReparsePoints $stableExtensionStage "Prepared stable extension tree"
    Assert-NoReparsePoints $toolsStage "Prepared maintenance tools tree"
    foreach ($stagedManifest in @(
      (Join-Path $extensionVersionStage "manifest.json"),
      (Join-Path $stableExtensionStage "manifest.json")
    )) {
      $stagedVersion = [string](Get-Content -LiteralPath $stagedManifest -Raw -Encoding UTF8 | ConvertFrom-Json).version
      if ($stagedVersion -ne $version) { throw "Prepared extension version does not match the Agent release." }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $appStage "DianAgent.exe") -PathType Leaf)) {
      throw "Prepared Agent executable is missing."
    }

    # This recovery pair is deliberately outside the four switched program
    # trees. Publish and verify it before the transaction journal can authorize
    # a directory rename, so a task never depends on the tools directory while
    # that directory is temporarily absent.
    $recoveryBootstrap = Install-DianRecoveryBootstrapFiles $InstallRoot $PSScriptRoot

    $versionFile = Join-Path $InstallRoot "current-version.txt"
    $offlinePointer = Join-Path $InstallRoot "current.json"
    $markerPath = Join-Path $InstallRoot ".dian-agent-install.json"
    $runtimeDir = Join-Path $InstallRoot "data\runtime"
    $startupStatePath = Join-Path $runtimeDir "startup-state.json"
    $programsShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"
    $repairShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk"
    $startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
    $activationPlans = @(
      (New-PreparedDirectoryActivation $appStage $appTarget),
      (New-PreparedDirectoryActivation $extensionVersionStage $extensionTarget),
      (New-PreparedDirectoryActivation $stableExtensionStage $stableExtension),
      (New-PreparedDirectoryActivation $toolsStage $toolsTarget)
    )
    $transactionId = [Guid]::NewGuid().ToString("N")
    $ownerStartedAt = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
    $autostartSnapshot = if ($SkipAutostart) {
      [ordered]@{
        managed = $false
        task = [ordered]@{ existed = $false; data_base64 = ""; sha256 = "" }
        shortcuts = @(
          [ordered]@{ path = [IO.Path]::GetFullPath($startupShortcut); existed = $false; data_base64 = ""; sha256 = "" },
          [ordered]@{ path = [IO.Path]::GetFullPath($programsShortcut); existed = $false; data_base64 = ""; sha256 = "" },
          [ordered]@{ path = [IO.Path]::GetFullPath($repairShortcut); existed = $false; data_base64 = ""; sha256 = "" }
        )
      }
    } else {
      [ordered]@{
        managed = $true
        task = New-InstallerTaskSnapshot
        shortcuts = @(
          (New-InstallerShortcutSnapshot $startupShortcut),
          (New-InstallerShortcutSnapshot $programsShortcut),
          (New-InstallerShortcutSnapshot $repairShortcut)
        )
      }
    }
    $installJournal = [pscustomobject][ordered]@{
      schema_version = 1
      product = "DianAgent"
      transaction_id = $transactionId
      install_root = $InstallRoot
      target_version = $version
      owner_pid = $PID
      owner_started_at = $ownerStartedAt
      created_at = [DateTimeOffset]::UtcNow.ToString("o")
      updated_at = [DateTimeOffset]::UtcNow.ToString("o")
      state = "prepared"
      detail = "all_stages_validated"
      activations = @($activationPlans | ForEach-Object {
        [ordered]@{
          target = [string]$_.Target
          prepared = [string]$_.Prepared
          backup = [string]$_.Backup
          had_target = [bool]$_.HadTarget
        }
      })
      file_snapshots = @(
        (New-InstallerFileSnapshot $versionFile),
        (New-InstallerFileSnapshot $offlinePointer),
        (New-InstallerFileSnapshot $markerPath),
        (New-InstallerFileSnapshot $startupStatePath)
      )
      autostart = $autostartSnapshot
    }
    Write-DianInstallTransaction $InstallRoot $installJournal
    $journalWritten = $true
    Invoke-InstallFault "after-transaction-journal"
    if (-not $SkipAutostart) {
      Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true
      Invoke-InstallFault "after-recovery-bootstrap-autostart"
    }

    # A full installer can follow an offline upgrade. Retire Agents from both
    # supported program layouts so an active versions/<version> runtime cannot
    # retain the port while the app/<version> release is activated.
    $ownedProgramRoots = @(
      ([IO.Path]::GetFullPath((Join-Path $InstallRoot "app")).TrimEnd('\') + '\'),
      ([IO.Path]::GetFullPath((Join-Path $InstallRoot "versions")).TrimEnd('\') + '\')
    )
    $ownedAgentProcesses = @(Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue | Where-Object {
      if ($_.ExecutablePath) {
        $processPath = [IO.Path]::GetFullPath([string]$_.ExecutablePath)
        foreach ($ownedProgramRoot in $ownedProgramRoots) {
          if ($processPath.StartsWith($ownedProgramRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
          }
        }
      }
      return $false
    })
    $ownedAgentWasRunning = $ownedAgentProcesses.Count -gt 0
    $ownedAgentProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Wait-FileUnlocked (Join-Path $appTarget "DianAgent.exe")

    Set-DianInstallTransactionState $InstallRoot $installJournal "activating" "switching_program_trees"
    foreach ($activationPlan in $activationPlans) {
      [void](Activate-PreparedDirectory $activationPlan)
      if ([string]$activationPlan.Target -eq [IO.Path]::GetFullPath($appTarget)) {
        Invoke-InstallFault "after-app-activate"
      }
    }
    Invoke-InstallFault "after-directory-activate"

    Write-DianAtomicBytes $versionFile ([Text.Encoding]::ASCII.GetBytes("$version`r`n"))
    if (Test-Path -LiteralPath $offlinePointer) { Remove-Item -LiteralPath $offlinePointer -Force }
    Write-InstallVerification $markerPath ([ordered]@{
      product = "DianAgent"
      schema = 1
      install_root = $InstallRoot
      current_version = $version
      updated_at = [DateTime]::UtcNow.ToString("o")
    })
    Set-DianInstallTransactionState $InstallRoot $installJournal "pointers_activated" "version_pointer_and_marker_active"
    Invoke-InstallFault "after-pointer-activate"
  } catch {
    throw
  } finally {
    if (-not $journalWritten) {
      foreach ($stage in $preparedStages) {
        if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
      }
    }
  }
} catch {
  $transactionFailure = $_
  $recoveryFailure = $null
  try {
    $recovery = Invoke-DianRecoverInstallTransaction $InstallRoot 1
    if ($recovery.Recovered -and $recovery.Action -eq "rolled_back") {
      $restartRestoredAfterFailure = $ownedAgentWasRunning
    }
    if (-not $recovery.Recovered -and $preparedStages) {
      foreach ($stage in $preparedStages) {
        if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
      }
    }
  } catch {
    $recoveryFailure = $_
  }
  if ($maintenanceLockHeld) {
    $maintenanceMutex.ReleaseMutex()
    $maintenanceLockHeld = $false
  }
  $maintenanceMutex.Dispose()
  if ($restartRestoredAfterFailure) {
    $restoredStart = Join-Path $InstallRoot "tools\start_agent.ps1"
    if (Test-Path -LiteralPath $restoredStart -PathType Leaf) {
      $restoredPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
      & $restoredPowerShell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restoredStart -InstallRoot $InstallRoot
      if ($LASTEXITCODE -ne 0) {
        Write-Warning "The previous installation was restored, but its Agent could not be restarted automatically."
      }
    }
  }
  if ($recoveryFailure) {
    throw "Installation failed and durable rollback could not complete. Run Repair Dian Agent before retrying. Original: $($transactionFailure.Exception.Message) Recovery: $($recoveryFailure.Exception.Message)"
  }
  throw $transactionFailure
}

$transactionCommitted = $false
try {
$marker = Get-Content -LiteralPath (Join-Path $InstallRoot ".dian-agent-install.json") -Raw -Encoding UTF8 | ConvertFrom-Json

$powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$startAgent = Join-Path $InstallRoot "tools\start_agent.ps1"
$startArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f $startAgent, $InstallRoot

$runtimeDir = Join-Path $InstallRoot "data\runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Set-DianInstallTransactionState $InstallRoot $installJournal "autostart_activating" "writing_startup_state_and_owned_autostart"
$startupState = [ordered]@{
  schema_version = 1
  state = if ($SkipAutostart) { "manual" } else { "configured" }
  state_label = if ($SkipAutostart) { "Autostart was not configured" } else { "Autostart and keepalive are configured" }
  autostart_enabled = -not $SkipAutostart
  keepalive_enabled = -not $SkipAutostart
  hidden_launcher = $true
  source = "release_install"
  task_name = if ($SkipAutostart) { "" } else { "DianAgentKeepAlive" }
  last_checked_at = [DateTime]::UtcNow.ToString("o")
  last_healthy_at = $null
  last_recovery_at = $null
  last_error = if ($SkipAutostart) { "autostart_skipped" } else { $null }
}
Write-InstallVerification (Join-Path $runtimeDir "startup-state.json") $startupState

if (-not $SkipAutostart) {
  $shell = New-Object -ComObject WScript.Shell
  $programsShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"
  $shortcut = $shell.CreateShortcut($programsShortcut)
  $shortcut.TargetPath = $powershell
  $shortcut.Arguments = $startArguments
  $shortcut.WorkingDirectory = $InstallRoot
  $shortcut.IconLocation = (Join-Path $appTarget "DianAgent.exe") + ",0"
  $shortcut.Description = "Start Dian Agent"
  $shortcut.Save()

  $repairShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk"
  $shortcut = $shell.CreateShortcut($repairShortcut)
  $shortcut.TargetPath = $wscript
  $shortcut.Arguments = '"' + (Join-Path $InstallRoot "tools\repair_agent.vbs") + '"'
  $shortcut.WorkingDirectory = $InstallRoot
  $shortcut.IconLocation = (Join-Path $appTarget "DianAgent.exe") + ",0"
  $shortcut.Description = "Repair and restart Dian Agent silently"
  $shortcut.Save()

  # Refresh the already-active stable bootstrap entrypoints. They intentionally
  # remain outside tools so no maintenance directory rename can strand them.
  Set-DianRecoveryBootstrapAutostart $InstallRoot $true $true
  Invoke-InstallFault "after-shortcuts-configured"
  Invoke-InstallFault "after-task-configured"
}

if ($SkipLaunch) {
  Set-DianInstallTransactionState $InstallRoot $installJournal "commit_decided" "skip_launch_explicitly_committed"
  $transactionCommitted = $true
  Invoke-InstallFault "after-commit-decided"
  [void](Invoke-DianRecoverInstallTransaction $InstallRoot 1)
  if (-not $SkipAutostart) { Remove-CommittedDevelopmentAutostartArtifacts }
}

if (-not $SkipLaunch) {
  Set-DianInstallTransactionState $InstallRoot $installJournal "health_checking" "waiting_for_exact_agent_health"
  Invoke-InstallFault "before-agent-start"
  Start-InstalledAgentUnderMaintenanceLock (Join-Path $appTarget "DianAgent.exe") $version $InstallRoot 8765 60
  $startupStatePath = Join-Path $runtimeDir "startup-state.json"
  $startupState = Get-Content -LiteralPath $startupStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
  $startupState.state = "healthy"
  $startupState.state_label = "Agent installation and startup are healthy"
  $startupState.last_checked_at = [DateTime]::UtcNow.ToString("o")
  $startupState.last_healthy_at = $startupState.last_checked_at
  $startupState.last_error = $null
  Write-InstallVerification $startupStatePath $startupState

  # Exact-version health and exact listener ownership are the commit gate.
  # Keep every previous-tree backup until this point so a launch failure can
  # still restore app, extension and tools in reverse order.
  Set-DianInstallTransactionState $InstallRoot $installJournal "commit_decided" "exact_agent_health_verified"
  $transactionCommitted = $true
  Invoke-InstallFault "after-commit-decided"
  [void](Invoke-DianRecoverInstallTransaction $InstallRoot 1)
  if (-not $SkipAutostart) { Remove-CommittedDevelopmentAutostartArtifacts }
  try { Remove-CommittedProgramDebris $InstallRoot } catch {
    Write-Warning "The verified installation could not remove old program staging debris: $($_.Exception.Message)"
  }

  $browser = $null
  try {
    foreach ($specification in @(
      @{ Root = [string]$env:ProgramFiles; Relative = "Google\Chrome\Application\chrome.exe" },
      @{ Root = [string]${env:ProgramFiles(x86)}; Relative = "Microsoft\Edge\Application\msedge.exe" },
      @{ Root = [string]$env:ProgramFiles; Relative = "Microsoft\Edge\Application\msedge.exe" }
    )) {
      if (-not $specification.Root) { continue }
      $candidate = Join-Path $specification.Root $specification.Relative
      if (Test-Path -LiteralPath $candidate -PathType Leaf) { $browser = $candidate; break }
    }
  } catch {
    Write-Warning "The installation is committed, but browser discovery failed: $($_.Exception.Message)"
  }
  if ($browser) {
    try {
      Start-Process -FilePath $browser -ArgumentList "chrome://extensions/" | Out-Null
    } catch {
      Write-Warning "The installation is committed, but the browser extension page could not be opened automatically: $($_.Exception.Message)"
    }
  }
  try {
    Start-Process -FilePath "explorer.exe" -ArgumentList ('"' + $stableExtension + '"') | Out-Null
  } catch {
    Write-Warning "The installation is committed, but the extension folder could not be opened automatically: $($_.Exception.Message)"
  }

  Write-Host "Waiting for the browser extension to authenticate and report version $version..."
  $extensionReport = Wait-ExtensionReport `
    (Join-Path $InstallRoot "config\distribution_state.json") `
    $acceptedExtensionIds $version $installStartedAt $ExtensionReportTimeoutSeconds
  $verificationPath = Join-Path $runtimeDir "install-verification.json"
  if (-not $extensionReport.Ready) {
    Write-InstallVerification $verificationPath ([ordered]@{
      schema_version = 1
      state = "extension_report_required"
      target_version = $version
      started_at = $installStartedAt.ToString("o")
      checked_at = [DateTimeOffset]::UtcNow.ToString("o")
      accepted_extension_id = $null
      reason = $extensionReport.Reason
    })
    throw "Dian Agent files were installed and the service is healthy, but browser extension activation was not verified. Open chrome://extensions, click Reload for Dian Agent, then run the installer again to complete verification."
  }
  Write-InstallVerification $verificationPath ([ordered]@{
    schema_version = 1
    state = "verified"
    target_version = $version
    started_at = $installStartedAt.ToString("o")
    checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    reported_at = $extensionReport.ReportedAt.ToString("o")
    accepted_extension_id = $extensionReport.AcceptedExtensionId
    accepted_scope = if ($isUpgrade) { "preauthorized_upgrade_registry" } else { "manifest_id_fresh_install" }
  })
}
} catch {
  $postCommitFailure = $_
  $postCommitRecoveryFailure = $null
  if (-not $transactionCommitted) {
    try {
      $recovery = Invoke-DianRecoverInstallTransaction $InstallRoot 1
      $restartRestoredAfterFailure = ($ownedAgentWasRunning -and $recovery.Recovered -and $recovery.Action -eq "rolled_back")
    } catch {
      $postCommitRecoveryFailure = $_
    }
  }
  if ($maintenanceLockHeld) {
    $maintenanceMutex.ReleaseMutex()
    $maintenanceLockHeld = $false
  }
  if ($restartRestoredAfterFailure) {
    $restoredStart = Join-Path $InstallRoot "tools\start_agent.ps1"
    if (Test-Path -LiteralPath $restoredStart -PathType Leaf) {
      $restoredPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
      & $restoredPowerShell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restoredStart -InstallRoot $InstallRoot
      if ($LASTEXITCODE -ne 0) {
        Write-Warning "The previous installation was restored, but its Agent could not be restarted automatically."
      }
    }
  }
  if ($postCommitRecoveryFailure) {
    throw "Installation failed and durable rollback could not complete. Run Repair Dian Agent before retrying. Original: $($postCommitFailure.Exception.Message) Recovery: $($postCommitRecoveryFailure.Exception.Message)"
  }
  throw $postCommitFailure
} finally {
  if ($maintenanceLockHeld) {
    $maintenanceMutex.ReleaseMutex()
    $maintenanceLockHeld = $false
  }
  $maintenanceMutex.Dispose()
}

Write-Host ""
if ($SkipLaunch) {
  Write-Host "Dian Agent $version files were staged; service and browser-extension verification were skipped." -ForegroundColor Yellow
} else {
  Write-Host "Dian Agent $version installed successfully; Agent and browser extension were both verified." -ForegroundColor Green
}
Write-Host "Application: $appTarget"
Write-Host "Browser extension (stable path): $stableExtension"
if ($SkipLaunch) { Write-Host "Launch the Agent and reload the extension before treating this installation as ready." }
Write-Host "User data: $(Join-Path $InstallRoot 'data')"
if ($SkipAutostart) { Write-Host "Automatic startup was skipped." }
if ($SkipLaunch) { Write-Host "Initial launch was skipped." }
