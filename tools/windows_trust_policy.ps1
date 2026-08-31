Set-StrictMode -Version Latest

function Get-DianMaintenanceMutexName([string]$Path) {
  $full = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar).ToUpperInvariant()
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($full))
    $rootHash = [BitConverter]::ToString($digest).Replace("-", "").Substring(0, 16)
  } finally {
    $sha.Dispose()
  }
  return "Local\DianAgentMaintenance-$rootHash"
}

function Enter-DianMaintenanceLock([string]$Path, [int]$TimeoutSeconds = 30) {
  if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300) { throw "Invalid maintenance lock timeout." }
  $mutex = New-Object Threading.Mutex($false, (Get-DianMaintenanceMutexName $Path))
  $acquired = $false
  try {
    try {
      $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
      $acquired = $true
    }
    if (-not $acquired) { throw "Another Dian Agent install, repair or removal is already in progress." }
    return $mutex
  } catch {
    $mutex.Dispose()
    throw
  }
}

function Exit-DianMaintenanceLock([Threading.Mutex]$Mutex) {
  if ($null -eq $Mutex) { return }
  try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function Assert-DianNoReparsePoints([string]$Path, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $queue = New-Object 'Collections.Generic.Queue[string]'
  $queue.Enqueue([IO.Path]::GetFullPath($Path))
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

function Assert-DianPathChainNoReparsePoints([string]$Path, [string]$Label) {
  $current = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
  while ($current) {
    if (Test-Path -LiteralPath $current) {
      $item = Get-Item -LiteralPath $current -Force
      if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$Label crosses a symbolic link, junction or other reparse point: $current"
      }
    }
    $parent = Split-Path -Parent $current
    if (-not $parent -or $parent.Equals($current, [StringComparison]::OrdinalIgnoreCase)) { break }
    $current = $parent
  }
}

function Get-DianSha256Hex([byte[]]$Bytes) {
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
}

function Write-DianAtomicBytes([string]$Path, [byte[]]$Bytes) {
  $full = [IO.Path]::GetFullPath($Path)
  $parent = Split-Path -Parent $full
  Assert-DianPathChainNoReparsePoints $parent "Atomic write parent"
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  if (Test-Path -LiteralPath $full) {
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Atomic write target is not a safe regular file: $full"
    }
  }
  $leaf = Split-Path -Leaf $full
  $replacePattern = '^\.' + [regex]::Escape($leaf) + '\.replace-[a-f0-9]{32}$'
  foreach ($orphan in @(Get-ChildItem -LiteralPath $parent -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match $replacePattern })) {
    if ($orphan.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Atomic replace backup is a reparse point: $($orphan.FullName)" }
    Remove-Item -LiteralPath $orphan.FullName -Force
  }
  $temporary = Join-Path $parent (".{0}.write-{1}" -f (Split-Path -Leaf $full), [Guid]::NewGuid().ToString("N"))
  $replacementBackup = Join-Path $parent (".{0}.replace-{1}" -f $leaf, [Guid]::NewGuid().ToString("N"))
  $stream = $null
  try {
    $stream = New-Object IO.FileStream(
      $temporary,
      [IO.FileMode]::CreateNew,
      [IO.FileAccess]::Write,
      [IO.FileShare]::None,
      4096,
      [IO.FileOptions]::WriteThrough
    )
    $stream.Write($Bytes, 0, $Bytes.Length)
    $stream.Flush($true)
    $stream.Dispose()
    $stream = $null
    if (Test-Path -LiteralPath $full -PathType Leaf) {
      [IO.File]::Replace($temporary, $full, $replacementBackup, $true)
      Remove-Item -LiteralPath $replacementBackup -Force
    } else {
      [IO.File]::Move($temporary, $full)
    }
  } finally {
    if ($stream) { $stream.Dispose() }
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $replacementBackup) { Remove-Item -LiteralPath $replacementBackup -Force -ErrorAction SilentlyContinue }
  }
}

function Get-DianInstallTransactionPath([string]$InstallRoot) {
  return Join-Path ([IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)) ".install-transaction.json"
}

function Write-DianInstallTransaction([string]$InstallRoot, [object]$Journal) {
  $path = Get-DianInstallTransactionPath $InstallRoot
  $encoding = New-Object Text.UTF8Encoding($false)
  $bytes = $encoding.GetBytes((($Journal | ConvertTo-Json -Depth 16) + "`n"))
  Write-DianAtomicBytes $path $bytes
}

function Set-DianInstallTransactionState([string]$InstallRoot, [object]$Journal, [string]$State, [string]$Detail = "") {
  $Journal | Add-Member -NotePropertyName state -NotePropertyValue $State -Force
  $Journal | Add-Member -NotePropertyName updated_at -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString("o")) -Force
  $Journal | Add-Member -NotePropertyName detail -NotePropertyValue $Detail -Force
  Write-DianInstallTransaction $InstallRoot $Journal
}

function Invoke-DianRecoveryFault([string]$Point) {
  if ([string]$env:DIAN_AGENT_RECOVERY_CRASH_POINT -eq $Point) {
    # This hard-kill hook exists so the recovery protocol can be tested at the
    # exact durable boundaries that an OS restart or power loss may expose.
    [Diagnostics.Process]::GetCurrentProcess().Kill()
  }
}

function Get-DianRecoveryBootstrapPath([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  return Join-Path $root "bootstrap\recovery_bootstrap.ps1"
}

function Get-DianRecoveryBootstrapLauncherPath([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  return Join-Path $root "bootstrap\recovery_bootstrap.vbs"
}

function Get-DianRecoveryBootstrapArguments([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $bootstrap = Get-DianRecoveryBootstrapPath $root
  return '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f $bootstrap, $root
}

function Get-DianRecoveryBootstrapLauncherBytes {
  # Keep the scheduled-task and Startup entrypoint console-free.  Calling
  # powershell.exe directly can briefly create a console before
  # -WindowStyle Hidden is processed, especially for an interactive logon
  # shortcut.  This stable VBS wrapper lives outside the replaceable tools
  # tree and waits for the bootstrap so Task Scheduler records its real exit.
  $source = @'
Option Explicit

Dim shell, fileSystem, bootstrapDir, installRoot, bootstrapPath, powershellPath, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

bootstrapDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
installRoot = fileSystem.GetParentFolderName(bootstrapDir)
bootstrapPath = fileSystem.BuildPath(bootstrapDir, "recovery_bootstrap.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powershellPath & """ -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & bootstrapPath & """ -InstallRoot """ & installRoot & """"
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
'@
  return [Text.Encoding]::ASCII.GetBytes(($source.TrimStart("`r", "`n") + "`r`n"))
}

function Install-DianRecoveryBootstrapFiles([string]$InstallRoot, [string]$SourceTools) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $source = [IO.Path]::GetFullPath($SourceTools).TrimEnd([IO.Path]::DirectorySeparatorChar)
  Assert-DianPathChainNoReparsePoints $root "Installation root"
  Assert-DianNoReparsePoints $source "Recovery bootstrap source"
  $bootstrapRoot = Join-Path $root "bootstrap"
  if (Test-Path -LiteralPath $bootstrapRoot) {
    $bootstrapItem = Get-Item -LiteralPath $bootstrapRoot -Force
    if (-not $bootstrapItem.PSIsContainer -or ($bootstrapItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Recovery bootstrap destination is not a safe directory."
    }
  }
  New-Item -ItemType Directory -Force -Path $bootstrapRoot | Out-Null
  foreach ($name in @("windows_trust_policy.ps1", "recovery_bootstrap.ps1")) {
    $sourcePath = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
      throw "Recovery bootstrap component is missing: $name"
    }
    $sourceItem = Get-Item -LiteralPath $sourcePath -Force
    if ($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "Recovery bootstrap component is a reparse point: $name"
    }
    $bytes = [IO.File]::ReadAllBytes($sourcePath)
    $destination = Join-Path $bootstrapRoot $name
    Write-DianAtomicBytes $destination $bytes
    if ((Get-DianSha256Hex ([IO.File]::ReadAllBytes($destination))) -ne (Get-DianSha256Hex $bytes)) {
      throw "Recovery bootstrap component did not verify after installation: $name"
    }
  }
  $launcherBytes = Get-DianRecoveryBootstrapLauncherBytes
  $launcher = Get-DianRecoveryBootstrapLauncherPath $root
  Write-DianAtomicBytes $launcher $launcherBytes
  if ((Get-DianSha256Hex ([IO.File]::ReadAllBytes($launcher))) -ne (Get-DianSha256Hex $launcherBytes)) {
    throw "Recovery bootstrap launcher did not verify after installation."
  }
  Assert-DianNoReparsePoints $bootstrapRoot "Installed recovery bootstrap"
  return Get-DianRecoveryBootstrapPath $root
}

function Test-DianOwnedKeepAliveTask([object]$Task, [string]$InstallRoot) {
  if (-not $Task) { return $false }
  $actions = @($Task.Actions)
  if ($actions.Count -ne 1) { return $false }
  try { $actualHost = [IO.Path]::GetFullPath([string]$actions[0].Execute) } catch { return $false }
  $actualArguments = ([string]$actions[0].Arguments).Trim()
  $legacyHost = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\wscript.exe"))
  $legacyLauncher = [IO.Path]::GetFullPath((Join-Path $InstallRoot "tools\watchdog_release.vbs"))
  if ($actualHost.Equals($legacyHost, [StringComparison]::OrdinalIgnoreCase) -and
      $actualArguments.Equals(('"' + $legacyLauncher + '"'), [StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }
  $bootstrapLauncher = [IO.Path]::GetFullPath((Get-DianRecoveryBootstrapLauncherPath $InstallRoot))
  if ($actualHost.Equals($legacyHost, [StringComparison]::OrdinalIgnoreCase) -and
      $actualArguments.Equals(('"' + $bootstrapLauncher + '"'), [StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }
  $bootstrapHost = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"))
  return $actualHost.Equals($bootstrapHost, [StringComparison]::OrdinalIgnoreCase) -and
    $actualArguments.Equals((Get-DianRecoveryBootstrapArguments $InstallRoot), [StringComparison]::OrdinalIgnoreCase)
}

function Get-DianQuotedAbsoluteArgumentPath([string]$Arguments) {
  $value = ([string]$Arguments).Trim()
  if ($value -notmatch '^"([^\"]+)"$') { return "" }
  try { return [IO.Path]::GetFullPath([string]$Matches[1]) } catch { return "" }
}

function Test-DianOwnedDevelopmentKeepAliveTask([object]$Task) {
  if (-not $Task) { return $false }
  $actions = @($Task.Actions)
  if ($actions.Count -ne 1) { return $false }
  try { $hostPath = [IO.Path]::GetFullPath([string]$actions[0].Execute) } catch { return $false }
  $wscript = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\wscript.exe"))
  $launcher = Get-DianQuotedAbsoluteArgumentPath ([string]$actions[0].Arguments)
  if (-not $hostPath.Equals($wscript, [StringComparison]::OrdinalIgnoreCase) -or -not $launcher) { return $false }
  return $launcher.EndsWith("\bridge\watchdog.vbs", [StringComparison]::OrdinalIgnoreCase)
}

function Test-DianOwnedDevelopmentShortcut([object]$Shortcut, [string]$ShortcutPath) {
  if (-not $Shortcut -or (Split-Path -Leaf $ShortcutPath) -ne "DianAgentDev.lnk") { return $false }
  try { $hostPath = [IO.Path]::GetFullPath([string]$Shortcut.TargetPath) } catch { return $false }
  try { $working = [IO.Path]::GetFullPath([string]$Shortcut.WorkingDirectory).TrimEnd('\') } catch { return $false }
  $wscript = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\wscript.exe"))
  $launcher = Get-DianQuotedAbsoluteArgumentPath ([string]$Shortcut.Arguments)
  if (-not $hostPath.Equals($wscript, [StringComparison]::OrdinalIgnoreCase) -or -not $launcher) { return $false }
  $expectedWorking = [IO.Path]::GetFullPath((Split-Path -Parent $launcher)).TrimEnd('\')
  return $launcher.EndsWith("\bridge\watchdog.vbs", [StringComparison]::OrdinalIgnoreCase) -and
    $working.Equals($expectedWorking, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-DianDevelopmentAutostartOwnership {
  $task = Get-ScheduledTask -TaskName "DianAgentDevKeepAlive" -ErrorAction SilentlyContinue
  if ($task -and -not (Test-DianOwnedDevelopmentKeepAliveTask $task)) {
    throw "A scheduled task named DianAgentDevKeepAlive exists but is not an owned Dian Agent development task."
  }
  $shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgentDev.lnk"
  if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    if ((Get-Item -LiteralPath $shortcutPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "The DianAgentDev startup shortcut is a reparse point."
    }
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    if (-not (Test-DianOwnedDevelopmentShortcut $shortcut $shortcutPath)) {
      throw "A DianAgentDev startup shortcut exists but is not owned by Dian Agent development mode."
    }
  }
}

function Remove-DianDevelopmentAutostartArtifacts {
  Assert-DianDevelopmentAutostartOwnership
  $removed = New-Object 'Collections.Generic.List[string]'
  $task = Get-ScheduledTask -TaskName "DianAgentDevKeepAlive" -ErrorAction SilentlyContinue
  if ($task) {
    Unregister-ScheduledTask -TaskName "DianAgentDevKeepAlive" -Confirm:$false
    $removed.Add("scheduled_task:DianAgentDevKeepAlive")
  }
  $shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgentDev.lnk"
  if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $shortcutPath -Force
    $removed.Add("startup_shortcut:DianAgentDev.lnk")
  }
  return @($removed)
}

function Test-DianOwnedShortcut([object]$Shortcut, [string]$ShortcutPath, [string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $powershell = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"))
  $wscript = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\wscript.exe"))
  $leaf = Split-Path -Leaf $ShortcutPath
  if ($leaf -eq "Dian Agent.lnk") {
    $expectedTarget = $powershell
    $expectedArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f (Join-Path $root "tools\start_agent.ps1"), $root
  } elseif ($leaf -eq "Repair Dian Agent.lnk") {
    $expectedTarget = $wscript
    $expectedArguments = '"' + (Join-Path $root "tools\repair_agent.vbs") + '"'
  } elseif ($leaf -eq "DianAgent.lnk") {
    try { $actualTarget = [IO.Path]::GetFullPath([string]$Shortcut.TargetPath) } catch { return $false }
    try { $actualWorking = [IO.Path]::GetFullPath([string]$Shortcut.WorkingDirectory).TrimEnd('\') } catch { return $false }
    $actualArguments = ([string]$Shortcut.Arguments).Trim()
    $legacyArguments = '"' + (Join-Path $root "tools\watchdog_release.vbs") + '"'
    $legacyOwned = $actualTarget.Equals($wscript, [StringComparison]::OrdinalIgnoreCase) -and
      $actualArguments.Equals($legacyArguments, [StringComparison]::OrdinalIgnoreCase)
    $bootstrapLauncherArguments = '"' + (Get-DianRecoveryBootstrapLauncherPath $root) + '"'
    $bootstrapLauncherOwned = $actualTarget.Equals($wscript, [StringComparison]::OrdinalIgnoreCase) -and
      $actualArguments.Equals($bootstrapLauncherArguments, [StringComparison]::OrdinalIgnoreCase)
    $bootstrapOwned = $actualTarget.Equals($powershell, [StringComparison]::OrdinalIgnoreCase) -and
      $actualArguments.Equals((Get-DianRecoveryBootstrapArguments $root), [StringComparison]::OrdinalIgnoreCase)
    return ($legacyOwned -or $bootstrapLauncherOwned -or $bootstrapOwned) -and
      $actualWorking.Equals($root, [StringComparison]::OrdinalIgnoreCase)
  } else { return $false }
  try { $actualTarget = [IO.Path]::GetFullPath([string]$Shortcut.TargetPath) } catch { return $false }
  try { $actualWorking = [IO.Path]::GetFullPath([string]$Shortcut.WorkingDirectory).TrimEnd('\') } catch { return $false }
  return $actualTarget.Equals($expectedTarget, [StringComparison]::OrdinalIgnoreCase) -and
    ([string]$Shortcut.Arguments).Trim().Equals($expectedArguments, [StringComparison]::OrdinalIgnoreCase) -and
    $actualWorking.Equals($root, [StringComparison]::OrdinalIgnoreCase)
}

function Set-DianRecoveryBootstrapAutostart(
  [string]$InstallRoot,
  [bool]$ConfigureTask,
  [bool]$ConfigureStartupShortcut
) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $bootstrap = Get-DianRecoveryBootstrapPath $root
  $bootstrapLauncher = Get-DianRecoveryBootstrapLauncherPath $root
  if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf) -or
      ((Get-Item -LiteralPath $bootstrap -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "The stable recovery bootstrap is missing or unsafe."
  }
  if (-not (Test-Path -LiteralPath $bootstrapLauncher -PathType Leaf) -or
      ((Get-Item -LiteralPath $bootstrapLauncher -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "The stable recovery bootstrap launcher is missing or unsafe."
  }
  $wscript = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\wscript.exe"))
  $arguments = '"' + $bootstrapLauncher + '"'
  if ($ConfigureStartupShortcut) {
    $shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
    if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
      if ((Get-Item -LiteralPath $shortcutPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The startup shortcut is a reparse point."
      }
      $existingShortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
      if (-not (Test-DianOwnedShortcut $existingShortcut $shortcutPath $root)) {
        throw "The startup shortcut is not owned by this installation."
      }
    }
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $wscript
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = $wscript + ",0"
    $shortcut.Description = "Recover and start Dian Agent after Windows sign-in"
    $shortcut.Save()
  }
  if ($ConfigureTask) {
    $existingTask = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
    if ($existingTask -and -not (Test-DianOwnedKeepAliveTask $existingTask $root)) {
      throw "The keepalive task is not owned by this installation."
    }
    $taskAction = New-ScheduledTaskAction -Execute $wscript -Argument $arguments
    $taskTrigger = @(
      (New-ScheduledTaskTrigger -AtLogOn),
      (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
    )
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "DianAgentKeepAlive" -Action $taskAction -Trigger $taskTrigger `
      -Settings $taskSettings -Description "Recovers interrupted maintenance and keeps Dian Agent available." -Force | Out-Null
  }
}

function ConvertFrom-DianSnapshotBytes([object]$Snapshot, [string]$Label) {
  if ($Snapshot.existed -ne $true) { return $null }
  try { $bytes = [Convert]::FromBase64String([string]$Snapshot.data_base64) } catch {
    throw "$Label snapshot is not valid Base64."
  }
  $expected = ([string]$Snapshot.sha256).ToLowerInvariant()
  if ($expected -notmatch '^[a-f0-9]{64}$' -or (Get-DianSha256Hex $bytes) -ne $expected) {
    throw "$Label snapshot digest does not match."
  }
  return [byte[]]$bytes
}

function Read-DianInstallTransaction([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $path = Get-DianInstallTransactionPath $root
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
  $item = Get-Item -LiteralPath $path -Force
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Install transaction journal is a reparse point." }
  try { $journal = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch {
    throw "Install transaction journal is corrupt; refusing automatic recovery."
  }
  $journalRoot = ""
  try { $journalRoot = [IO.Path]::GetFullPath([string]$journal.install_root).TrimEnd([IO.Path]::DirectorySeparatorChar) } catch { }
  if ($journal.schema_version -ne 1 -or [string]$journal.product -ne "DianAgent" -or
      -not $journalRoot.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
      [string]$journal.transaction_id -notmatch '^[a-f0-9]{32}$' -or
      [string]$journal.target_version -notmatch '^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$' -or
      [string]$journal.state -notin @("prepared", "activating", "pointers_activated", "autostart_activating", "health_checking", "commit_decided", "rolling_back", "rollback_incomplete")) {
    throw "Install transaction journal identity or state is invalid."
  }
  $journalOwnerPid = 0
  $journalOwnerStarted = [DateTimeOffset]::MinValue
  try { $journalOwnerPid = [int]$journal.owner_pid } catch { }
  if ($journalOwnerPid -le 0 -or
      -not [DateTimeOffset]::TryParse([string]$journal.owner_started_at, [ref]$journalOwnerStarted)) {
    throw "Install transaction owner identity is invalid."
  }
  $activations = @($journal.activations)
  if ($activations.Count -ne 4) { throw "Install transaction journal does not describe all four program trees." }
  $expectedTargets = @(
    [IO.Path]::GetFullPath((Join-Path $root ("app\{0}" -f [string]$journal.target_version))),
    [IO.Path]::GetFullPath((Join-Path $root ("extension\{0}" -f [string]$journal.target_version))),
    [IO.Path]::GetFullPath((Join-Path $root "extension-current")),
    [IO.Path]::GetFullPath((Join-Path $root "tools"))
  )
  $seenTargets = @()
  $seenPrepared = @()
  $seenBackups = @()
  foreach ($activation in $activations) {
    try {
      $target = [IO.Path]::GetFullPath([string]$activation.target)
      $prepared = [IO.Path]::GetFullPath([string]$activation.prepared)
      $backup = [IO.Path]::GetFullPath([string]$activation.backup)
    } catch { throw "Install transaction contains an invalid program path." }
    if ($expectedTargets -notcontains $target -or $seenTargets -contains $target -or
        $seenPrepared -contains $prepared -or $seenBackups -contains $backup -or
        $activation.had_target -isnot [bool]) {
      throw "Install transaction program target set is invalid."
    }
    $seenTargets += $target
    $seenPrepared += $prepared
    $seenBackups += $backup
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $target))
    $preparedParent = [IO.Path]::GetFullPath((Split-Path -Parent $prepared))
    $backupParent = [IO.Path]::GetFullPath((Split-Path -Parent $backup))
    $preparedPrefix = if ($target.Equals($expectedTargets[0], [StringComparison]::OrdinalIgnoreCase)) {
      ".app-$([string]$journal.target_version)-stage-"
    } elseif ($target.Equals($expectedTargets[1], [StringComparison]::OrdinalIgnoreCase)) {
      ".extension-$([string]$journal.target_version)-stage-"
    } elseif ($target.Equals($expectedTargets[2], [StringComparison]::OrdinalIgnoreCase)) {
      ".extension-current-stage-"
    } else {
      ".tools-stage-"
    }
    if (-not $preparedParent.Equals($parent, [StringComparison]::OrdinalIgnoreCase) -or
        -not $backupParent.Equals($parent, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $prepared) -notmatch ('^' + [regex]::Escape($preparedPrefix) + '[a-f0-9]{32}$') -or
        (Split-Path -Leaf $backup) -notmatch ('^\.' + [regex]::Escape((Split-Path -Leaf $target)) + '-backup-[a-f0-9]{32}$')) {
      throw "Install transaction stage or backup path is outside its exact managed parent."
    }
    if ($activation.PSObject.Properties.Name -contains "rollback_state") {
      if ([string]$activation.rollback_state -notin @("pending", "restoring", "restored")) {
        throw "Install transaction directory rollback state is invalid."
      }
    }
  }
  $snapshots = @($journal.file_snapshots)
  $expectedSnapshotPaths = @(
    [IO.Path]::GetFullPath((Join-Path $root "current-version.txt")),
    [IO.Path]::GetFullPath((Join-Path $root "current.json")),
    [IO.Path]::GetFullPath((Join-Path $root ".dian-agent-install.json")),
    [IO.Path]::GetFullPath((Join-Path $root "data\runtime\startup-state.json"))
  )
  if ($snapshots.Count -ne $expectedSnapshotPaths.Count) { throw "Install transaction file snapshot set is incomplete." }
  $seenSnapshots = @()
  foreach ($snapshot in $snapshots) {
    try { $snapshotPath = [IO.Path]::GetFullPath([string]$snapshot.path) } catch { throw "Install transaction file snapshot path is invalid." }
    if ($expectedSnapshotPaths -notcontains $snapshotPath -or $seenSnapshots -contains $snapshotPath -or $snapshot.existed -isnot [bool]) {
      throw "Install transaction file snapshot set is invalid."
    }
    $seenSnapshots += $snapshotPath
    if ($snapshot.existed) { [void](ConvertFrom-DianSnapshotBytes $snapshot "File") }
  }
  if ($null -eq $journal.autostart -or $journal.autostart.managed -isnot [bool]) { throw "Install transaction autostart scope is invalid." }
  $taskSnapshot = $journal.autostart.task
  if ($null -eq $taskSnapshot -or $taskSnapshot.existed -isnot [bool]) { throw "Install transaction task snapshot is invalid." }
  if ($taskSnapshot.existed) { [void](ConvertFrom-DianSnapshotBytes $taskSnapshot "Scheduled task") }
  $shortcutSnapshots = @($journal.autostart.shortcuts)
  $expectedShortcuts = @(
    [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk")),
    [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk")),
    [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath("Programs")) "Repair Dian Agent.lnk"))
  )
  if ($shortcutSnapshots.Count -ne $expectedShortcuts.Count) { throw "Install transaction shortcut snapshot set is incomplete." }
  $seenShortcuts = @()
  foreach ($snapshot in $shortcutSnapshots) {
    try { $shortcutPath = [IO.Path]::GetFullPath([string]$snapshot.path) } catch { throw "Install transaction shortcut path is invalid." }
    if ($expectedShortcuts -notcontains $shortcutPath -or $seenShortcuts -contains $shortcutPath -or $snapshot.existed -isnot [bool]) {
      throw "Install transaction shortcut snapshot set is invalid."
    }
    $seenShortcuts += $shortcutPath
    if ($snapshot.existed) { [void](ConvertFrom-DianSnapshotBytes $snapshot "Shortcut") }
  }
  return $journal
}

function Remove-DianInstallTransactionTemps([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  if (-not (Test-Path -LiteralPath $root -PathType Container)) { return }
  $temps = @(Get-ChildItem -LiteralPath $root -Force -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^\.\.install-transaction\.json\.(?:write|replace)-[a-f0-9]{32}$'
  })
  foreach ($temp in $temps) {
    if ($temp.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Install transaction temporary file is a reparse point." }
  }
  foreach ($temp in $temps) { Remove-Item -LiteralPath $temp.FullName -Force }
}

function Invoke-DianRecoverInstallTransaction([string]$InstallRoot, [int]$LockTimeoutSeconds = 30) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $mutex = Enter-DianMaintenanceLock $root $LockTimeoutSeconds
  try {
    $journal = Read-DianInstallTransaction $root
    if ($null -eq $journal) {
      Remove-DianInstallTransactionTemps $root
      return [pscustomobject]@{ Recovered = $false; Action = "none"; TransactionId = "" }
    }
    $ownerPid = 0
    try { $ownerPid = [int]$journal.owner_pid } catch { throw "Install transaction owner PID is invalid." }
    if ($ownerPid -gt 0 -and $ownerPid -ne $PID) {
      $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
      if ($owner) {
        try { $ownerStarted = $owner.StartTime.ToUniversalTime().ToString("o") } catch {
          throw "Install transaction owner is alive but its start identity cannot be verified."
        }
        if ($ownerStarted -eq [string]$journal.owner_started_at) {
          throw "Install transaction owner is still alive; refusing concurrent recovery."
        }
      }
    }

    $activations = @($journal.activations)
    foreach ($activation in $activations) {
      foreach ($candidate in @([string]$activation.target, [string]$activation.prepared, [string]$activation.backup)) {
        if (Test-Path -LiteralPath $candidate) {
          if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { throw "Install transaction program path is not a directory: $candidate" }
          Assert-DianNoReparsePoints $candidate "Install transaction program tree"
        }
      }
    }

    if ([string]$journal.state -eq "commit_decided") {
      foreach ($activation in $activations) {
        if (-not (Test-Path -LiteralPath ([string]$activation.target) -PathType Container)) {
          throw "Committed install transaction is missing an active program tree."
        }
      }
      $pointerVersion = ""
      $versionFile = Join-Path $root "current-version.txt"
      if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        $pointerVersion = (Get-Content -LiteralPath $versionFile -Raw -Encoding ASCII).Trim()
      } elseif (Test-Path -LiteralPath (Join-Path $root "current.json") -PathType Leaf) {
        $pointerVersion = [string](Get-Content -LiteralPath (Join-Path $root "current.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
      }
      if ($pointerVersion -ne [string]$journal.target_version) { throw "Committed install transaction pointer does not match its target version." }
      foreach ($activation in $activations) {
        foreach ($candidate in @([string]$activation.prepared, [string]$activation.backup)) {
          if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Recurse -Force }
        }
      }
      $journalPath = Get-DianInstallTransactionPath $root
      Remove-Item -LiteralPath $journalPath -Force
      Remove-DianInstallTransactionTemps $root
      return [pscustomobject]@{ Recovered = $true; Action = "finalized_commit"; TransactionId = [string]$journal.transaction_id }
    }

    # Preflight every rollback decision before mutating the first artifact. A
    # per-directory write-ahead state makes rollback re-entrant even if this
    # recovery process is itself killed between either directory rename.
    foreach ($activation in $activations) {
      $targetExists = Test-Path -LiteralPath ([string]$activation.target) -PathType Container
      $preparedExists = Test-Path -LiteralPath ([string]$activation.prepared) -PathType Container
      $backupExists = Test-Path -LiteralPath ([string]$activation.backup) -PathType Container
      $rollbackState = if ($activation.PSObject.Properties.Name -contains "rollback_state") {
        [string]$activation.rollback_state
      } else { "pending" }
      if ($rollbackState -eq "restored") {
        if (-not $activation.had_target -and $targetExists) {
          throw "A fresh program target reappeared after durable rollback: $($activation.target)"
        }
        if ($activation.had_target -and (-not $targetExists -or $backupExists)) {
          throw "A restored program target is incomplete or still has a backup: $($activation.target)"
        }
      } elseif ($activation.had_target) {
        $isBeforeActivation = $targetExists -and $preparedExists -and -not $backupExists
        $isAfterFirstRename = -not $targetExists -and $preparedExists -and $backupExists
        $isActivated = $targetExists -and -not $preparedExists -and $backupExists
        $isRestoredBeforeCleanup = $targetExists -and $preparedExists -and -not $backupExists -and $rollbackState -eq "restoring"
        if (-not ($isBeforeActivation -or $isAfterFirstRename -or $isActivated -or $isRestoredBeforeCleanup)) {
          throw "Previous program backup is missing and activation state is ambiguous: $($activation.target)"
        }
      } else {
        $isBeforeActivation = -not $targetExists -and $preparedExists -and -not $backupExists
        $isActivated = $targetExists -and -not $preparedExists -and -not $backupExists
        $isRestoredBeforeCleanup = -not $targetExists -and $preparedExists -and -not $backupExists -and $rollbackState -eq "restoring"
        if ($backupExists -or (-not ($isBeforeActivation -or $isActivated -or $isRestoredBeforeCleanup))) {
          throw "Fresh program activation state is ambiguous: $($activation.target)"
        }
      }
    }

    $taskSnapshot = $journal.autostart.task
    $currentTask = $null
    if ($journal.autostart.managed) {
      $currentTask = Get-ScheduledTask -TaskName "DianAgentKeepAlive" -ErrorAction SilentlyContinue
      if ($currentTask -and -not (Test-DianOwnedKeepAliveTask $currentTask $root)) {
        throw "A non-owned keepalive task appeared during the interrupted install; refusing to overwrite it."
      }
      $shell = New-Object -ComObject WScript.Shell
      foreach ($shortcutSnapshot in @($journal.autostart.shortcuts)) {
        $shortcutPath = [string]$shortcutSnapshot.path
        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
          $shortcutItem = Get-Item -LiteralPath $shortcutPath -Force
          if ($shortcutItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Interrupted-install shortcut became a reparse point." }
          $shortcut = $shell.CreateShortcut($shortcutPath)
          if (-not (Test-DianOwnedShortcut $shortcut $shortcutPath $root)) {
            throw "A non-owned shortcut appeared during the interrupted install; refusing to overwrite it: $shortcutPath"
          }
        }
      }
    }
    foreach ($snapshot in @($journal.file_snapshots)) {
      if ($snapshot.existed) { [void](ConvertFrom-DianSnapshotBytes $snapshot "File") }
      if (Test-Path -LiteralPath ([string]$snapshot.path)) {
        $snapshotItem = Get-Item -LiteralPath ([string]$snapshot.path) -Force
        if ($snapshotItem.PSIsContainer -or ($snapshotItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
          throw "Interrupted-install file target is unsafe: $($snapshot.path)"
        }
      }
    }

    try {
      foreach ($activation in $activations) {
        if (-not ($activation.PSObject.Properties.Name -contains "rollback_state")) {
          $activation | Add-Member -NotePropertyName rollback_state -NotePropertyValue "pending"
        }
      }
      Set-DianInstallTransactionState $root $journal "rolling_back" "dead_owner_recovery"
      Invoke-DianRecoveryFault "after-rollback-state"
      $appTarget = [IO.Path]::GetFullPath((Join-Path $root ("app\{0}" -f [string]$journal.target_version)))
      $appActivation = @($activations | Where-Object {
        ([IO.Path]::GetFullPath([string]$_.target)).Equals($appTarget, [StringComparison]::OrdinalIgnoreCase)
      })
      if ($appActivation.Count -ne 1) { throw "Install transaction app activation is missing or duplicated." }
      $newAgentPath = Join-Path ([string]$appActivation[0].target) "DianAgent.exe"
      if (Test-Path -LiteralPath $newAgentPath -PathType Leaf) {
        $expectedAgent = [IO.Path]::GetFullPath($newAgentPath)
        Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).Equals($expectedAgent, [StringComparison]::OrdinalIgnoreCase) } |
          ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        $stopDeadline = (Get-Date).AddSeconds(15)
        do {
          $remainingAgent = @(Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and ([IO.Path]::GetFullPath([string]$_.ExecutablePath)).Equals($expectedAgent, [StringComparison]::OrdinalIgnoreCase) })
          if ($remainingAgent.Count -eq 0) { break }
          Start-Sleep -Milliseconds 200
        } while ((Get-Date) -lt $stopDeadline)
        if ($remainingAgent.Count -gt 0) { throw "Interrupted-install Agent process did not exit before rollback." }
      }

      foreach ($snapshot in @($journal.file_snapshots)) {
        $snapshotPath = [string]$snapshot.path
        if ($snapshot.existed) {
          Write-DianAtomicBytes $snapshotPath (ConvertFrom-DianSnapshotBytes $snapshot "File")
        } elseif (Test-Path -LiteralPath $snapshotPath) {
          Remove-Item -LiteralPath $snapshotPath -Force
        }
      }
      for ($index = $activations.Count - 1; $index -ge 0; $index--) {
        $activation = $activations[$index]
        $target = [string]$activation.target
        $prepared = [string]$activation.prepared
        $backup = [string]$activation.backup
        if ([string]$activation.rollback_state -ne "restored") {
          $activation.rollback_state = "restoring"
          Write-DianInstallTransaction $root $journal
          Invoke-DianRecoveryFault ("before-directory-rollback-{0}" -f $index)

          if ($activation.had_target) {
            if ((Test-Path -LiteralPath $target -PathType Container) -and
                (Test-Path -LiteralPath $backup -PathType Container) -and
                -not (Test-Path -LiteralPath $prepared)) {
              # Preserve the new tree instead of deleting it. This rename is
              # the key invariant that makes every following crash recoverable.
              [IO.Directory]::Move($target, $prepared)
              Invoke-DianRecoveryFault ("after-target-to-prepared-{0}" -f $index)
            }
            if (-not (Test-Path -LiteralPath $target) -and
                (Test-Path -LiteralPath $backup -PathType Container)) {
              [IO.Directory]::Move($backup, $target)
              Invoke-DianRecoveryFault ("after-backup-to-target-{0}" -f $index)
            }
            if (-not (Test-Path -LiteralPath $target -PathType Container) -or
                (Test-Path -LiteralPath $backup)) {
              throw "Previous program tree could not be restored: $target"
            }
          } else {
            if ((Test-Path -LiteralPath $target -PathType Container) -and
                -not (Test-Path -LiteralPath $prepared)) {
              [IO.Directory]::Move($target, $prepared)
              Invoke-DianRecoveryFault ("after-target-to-prepared-{0}" -f $index)
            }
            if (Test-Path -LiteralPath $target) {
              throw "Fresh program target could not be removed from active state: $target"
            }
          }
          $activation.rollback_state = "restored"
          Write-DianInstallTransaction $root $journal
          Invoke-DianRecoveryFault ("after-directory-restored-{0}" -f $index)
        }
        if (Test-Path -LiteralPath $prepared) { Remove-Item -LiteralPath $prepared -Recurse -Force }
        Invoke-DianRecoveryFault ("after-prepared-cleanup-{0}" -f $index)
      }
      # Keep the stable bootstrap task and Startup shortcut alive until every
      # directory (especially tools) has reached its restored location. A hard
      # kill in either tools rename window can therefore start this recovery
      # again without depending on the directory being switched.
      if ($journal.autostart.managed) {
        if ($taskSnapshot.existed) {
          $taskXml = [Text.Encoding]::UTF8.GetString((ConvertFrom-DianSnapshotBytes $taskSnapshot "Scheduled task"))
          Register-ScheduledTask -TaskName "DianAgentKeepAlive" -Xml $taskXml -Force | Out-Null
        } elseif ($currentTask) {
          Unregister-ScheduledTask -TaskName "DianAgentKeepAlive" -Confirm:$false
        }
        foreach ($shortcutSnapshot in @($journal.autostart.shortcuts)) {
          $shortcutPath = [string]$shortcutSnapshot.path
          if ($shortcutSnapshot.existed) {
            Write-DianAtomicBytes $shortcutPath (ConvertFrom-DianSnapshotBytes $shortcutSnapshot "Shortcut")
          } elseif (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            Remove-Item -LiteralPath $shortcutPath -Force
          }
        }
      }
      Remove-Item -LiteralPath (Get-DianInstallTransactionPath $root) -Force
      Remove-DianInstallTransactionTemps $root
      return [pscustomobject]@{ Recovered = $true; Action = "rolled_back"; TransactionId = [string]$journal.transaction_id }
    } catch {
      try { Set-DianInstallTransactionState $root $journal "rollback_incomplete" $_.Exception.Message } catch { }
      throw "Interrupted install could not be recovered completely; transaction journal was retained. $($_.Exception.Message)"
    }
  } finally {
    Exit-DianMaintenanceLock $mutex
  }
}

function Get-DianReleaseToolsTransactionPath([string]$InstallRoot) {
  return Join-Path ([IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)) ".release-tools-transaction.json"
}

function Write-DianReleaseToolsTransaction([string]$InstallRoot, [object]$Journal) {
  $encoding = New-Object Text.UTF8Encoding($false)
  $bytes = $encoding.GetBytes((($Journal | ConvertTo-Json -Depth 8) + "`n"))
  Write-DianAtomicBytes (Get-DianReleaseToolsTransactionPath $InstallRoot) $bytes
}

function Set-DianReleaseToolsTransactionState([string]$InstallRoot, [object]$Journal, [string]$State, [string]$Detail = "") {
  $Journal | Add-Member -NotePropertyName state -NotePropertyValue $State -Force
  $Journal | Add-Member -NotePropertyName updated_at -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString("o")) -Force
  $Journal | Add-Member -NotePropertyName detail -NotePropertyValue $Detail -Force
  Write-DianReleaseToolsTransaction $InstallRoot $Journal
}

function Read-DianReleaseToolsTransaction([string]$InstallRoot) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $path = Get-DianReleaseToolsTransactionPath $root
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
  if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Release-tools transaction journal is a reparse point."
  }
  try { $journal = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch {
    throw "Release-tools transaction journal is corrupt; refusing automatic recovery."
  }
  $journalRoot = ""
  $target = ""
  $stage = ""
  $backup = ""
  try {
    $journalRoot = [IO.Path]::GetFullPath([string]$journal.install_root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $target = [IO.Path]::GetFullPath([string]$journal.target)
    $stage = [IO.Path]::GetFullPath([string]$journal.stage)
    $backup = [IO.Path]::GetFullPath([string]$journal.backup)
  } catch { throw "Release-tools transaction contains an invalid path." }
  $expectedTarget = [IO.Path]::GetFullPath((Join-Path $root "tools"))
  $transactionId = [string]$journal.transaction_id
  $expectedStage = [IO.Path]::GetFullPath((Join-Path $root (".release-tools-stage-{0}" -f $transactionId)))
  $expectedBackup = [IO.Path]::GetFullPath((Join-Path $root (".release-tools-backup-{0}" -f $transactionId)))
  if ($journal.schema_version -ne 1 -or [string]$journal.product -ne "DianAgent" -or
      -not $journalRoot.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
      [string]$journal.transaction_id -notmatch '^[a-f0-9]{32}$' -or
      [string]$journal.state -notin @("prepared", "activating", "commit_decided", "rolling_back", "rollback_incomplete") -or
      $journal.had_target -isnot [bool] -or $journal.had_target -ne $true -or
      -not $target.Equals($expectedTarget, [StringComparison]::OrdinalIgnoreCase) -or
      -not $stage.Equals($expectedStage, [StringComparison]::OrdinalIgnoreCase) -or
      -not $backup.Equals($expectedBackup, [StringComparison]::OrdinalIgnoreCase) -or
      $stage.Equals($backup, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Release-tools transaction identity, state or path scope is invalid."
  }
  $ownerPid = 0
  $ownerStarted = [DateTimeOffset]::MinValue
  try { $ownerPid = [int]$journal.owner_pid } catch { }
  if ($ownerPid -le 0 -or -not [DateTimeOffset]::TryParse([string]$journal.owner_started_at, [ref]$ownerStarted)) {
    throw "Release-tools transaction owner identity is invalid."
  }
  return $journal
}

function Invoke-DianToolsRecoveryFault([string]$Point) {
  if ([string]$env:DIAN_AGENT_TOOLS_RECOVERY_CRASH_POINT -eq $Point) {
    [Diagnostics.Process]::GetCurrentProcess().Kill()
  }
}

function Invoke-DianRecoverReleaseToolsTransaction([string]$InstallRoot, [int]$LockTimeoutSeconds = 30) {
  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $mutex = Enter-DianMaintenanceLock $root $LockTimeoutSeconds
  try {
    $journal = Read-DianReleaseToolsTransaction $root
    if ($null -eq $journal) {
      return [pscustomobject]@{ Recovered = $false; Action = "none"; TransactionId = "" }
    }
    $ownerPid = [int]$journal.owner_pid
    if ($ownerPid -ne $PID) {
      $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
      if ($owner) {
        try { $ownerStarted = $owner.StartTime.ToUniversalTime().ToString("o") } catch {
          throw "Release-tools transaction owner is alive but cannot be verified."
        }
        if ($ownerStarted -eq [string]$journal.owner_started_at) {
          throw "Release-tools transaction owner is still alive; refusing concurrent recovery."
        }
      }
    }
    $target = [string]$journal.target
    $stage = [string]$journal.stage
    $backup = [string]$journal.backup
    foreach ($candidate in @($target, $stage, $backup)) {
      if (Test-Path -LiteralPath $candidate) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
          throw "Release-tools transaction path is not a directory: $candidate"
        }
        Assert-DianNoReparsePoints $candidate "Release-tools transaction tree"
      }
    }
    if ([string]$journal.state -eq "commit_decided") {
      if (-not (Test-Path -LiteralPath $target -PathType Container)) {
        throw "Committed release-tools transaction has no active tools directory."
      }
      foreach ($candidate in @($stage, $backup)) {
        if (Test-Path -LiteralPath $candidate) { Remove-Item -LiteralPath $candidate -Recurse -Force }
      }
      Remove-Item -LiteralPath (Get-DianReleaseToolsTransactionPath $root) -Force
      return [pscustomobject]@{ Recovered = $true; Action = "finalized_commit"; TransactionId = [string]$journal.transaction_id }
    }
    try {
      $priorState = [string]$journal.state
      Set-DianReleaseToolsTransactionState $root $journal "rolling_back" "dead_owner_recovery"
      $targetExists = Test-Path -LiteralPath $target -PathType Container
      $stageExists = Test-Path -LiteralPath $stage -PathType Container
      $backupExists = Test-Path -LiteralPath $backup -PathType Container
      if ($targetExists -and $stageExists -and -not $backupExists) {
        # Either no rename occurred, or the previous tree was already restored.
      } elseif (-not $targetExists -and $stageExists -and $backupExists) {
        [IO.Directory]::Move($backup, $target)
        Invoke-DianToolsRecoveryFault "after-backup-to-target"
      } elseif ($targetExists -and -not $stageExists -and $backupExists) {
        [IO.Directory]::Move($target, $stage)
        Invoke-DianToolsRecoveryFault "after-target-to-stage"
        [IO.Directory]::Move($backup, $target)
        Invoke-DianToolsRecoveryFault "after-backup-to-target"
      } elseif ($targetExists -and -not $stageExists -and -not $backupExists -and
                $priorState -in @("rolling_back", "rollback_incomplete")) {
        # A prior recovery restored and cleaned the trees before it could remove
        # the durable journal.
      } else {
        throw "Release-tools transaction state is ambiguous; recovery evidence was retained."
      }
      if (-not (Test-Path -LiteralPath $target -PathType Container) -or (Test-Path -LiteralPath $backup)) {
        throw "Previous maintenance tools could not be restored."
      }
      if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
      Invoke-DianToolsRecoveryFault "after-stage-cleanup"
      Remove-Item -LiteralPath (Get-DianReleaseToolsTransactionPath $root) -Force
      return [pscustomobject]@{ Recovered = $true; Action = "rolled_back"; TransactionId = [string]$journal.transaction_id }
    } catch {
      try { Set-DianReleaseToolsTransactionState $root $journal "rollback_incomplete" $_.Exception.Message } catch { }
      throw "Interrupted release-tools update could not be recovered; journal retained. $($_.Exception.Message)"
    }
  } finally {
    Exit-DianMaintenanceLock $mutex
  }
}

function Get-DianAcceptedExtensionIds(
  [string]$ProvisionedExtensionId,
  [object]$TrustedRegistry,
  [bool]$IsUpgrade
) {
  $provisioned = $ProvisionedExtensionId.Trim().ToLowerInvariant()
  if ($provisioned -notmatch '^[a-p]{32}$') { throw "The provisioned extension ID is invalid." }
  if ($null -eq $TrustedRegistry -or $TrustedRegistry.schema_version -ne 1 -or
      -not ($TrustedRegistry.PSObject.Properties.Name -contains "extension_ids") -or
      $TrustedRegistry.extension_ids -isnot [System.Array]) {
    throw "The trusted extension registry is invalid."
  }
  $trusted = @($TrustedRegistry.extension_ids | ForEach-Object { [string]$_ } | ForEach-Object { $_.Trim().ToLowerInvariant() })
  if ($trusted.Count -lt 1 -or @($trusted | Where-Object { $_ -notmatch '^[a-p]{32}$' }).Count -gt 0) {
    throw "The trusted extension registry contains an invalid extension ID."
  }
  $trusted = @($trusted | Sort-Object -Unique)
  if ($trusted -notcontains $provisioned) { throw "The trusted extension registry omits the provisioned extension ID." }
  # A clean installation can only be completed by the manifest-derived ID.
  # During an upgrade, an older ID is accepted only if it was already present
  # in the strictly validated pre-authorized registry.
  if ($IsUpgrade) { return @($trusted) }
  return @($provisioned)
}

function Test-DianExtensionReport(
  [object]$Report,
  [string[]]$AcceptedExtensionIds,
  [string]$TargetVersion,
  [DateTimeOffset]$InstallStartedAt
) {
  if ($null -eq $Report) {
    return [pscustomobject]@{ Ready = $false; Reason = "report_missing"; AcceptedExtensionId = "" }
  }
  if ($Report.origin_verified -ne $true) {
    return [pscustomobject]@{ Ready = $false; Reason = "origin_not_verified"; AcceptedExtensionId = "" }
  }
  if ([string]$Report.version -ne $TargetVersion) {
    return [pscustomobject]@{ Ready = $false; Reason = "version_mismatch"; AcceptedExtensionId = "" }
  }
  $extensionId = ([string]$Report.extension_id).Trim().ToLowerInvariant()
  if ($extensionId -notmatch '^[a-p]{32}$' -or $AcceptedExtensionIds -notcontains $extensionId) {
    return [pscustomobject]@{ Ready = $false; Reason = "extension_not_pre_authorized"; AcceptedExtensionId = "" }
  }
  try {
    $reportedAt = [DateTimeOffset]::Parse(
      [string]$Report.reported_at,
      [Globalization.CultureInfo]::InvariantCulture,
      [Globalization.DateTimeStyles]::RoundtripKind
    )
  } catch {
    return [pscustomobject]@{ Ready = $false; Reason = "reported_at_invalid"; AcceptedExtensionId = "" }
  }
  if ($reportedAt -lt $InstallStartedAt) {
    return [pscustomobject]@{ Ready = $false; Reason = "report_stale"; AcceptedExtensionId = "" }
  }
  return [pscustomobject]@{ Ready = $true; Reason = "verified"; AcceptedExtensionId = $extensionId; ReportedAt = $reportedAt }
}
