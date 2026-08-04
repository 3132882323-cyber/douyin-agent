[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [string]$SourceRoot = "",
  [switch]$SkipAutostart,
  [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FullPath([string]$Path) {
  return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
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
  New-Item -ItemType Directory -Force -Path $Destination | Out-Null
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
  }
}

function Reset-VersionDirectory([string]$Parent, [string]$Target) {
  $fullParent = (Get-FullPath $Parent) + [IO.Path]::DirectorySeparatorChar
  $fullTarget = Get-FullPath $Target
  if (-not $fullTarget.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe version directory: $fullTarget"
  }
  if (Test-Path -LiteralPath $fullTarget) { Remove-Item -LiteralPath $fullTarget -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $fullTarget | Out-Null
}

if (-not $InstallRoot) {
  $InstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DianAgent"
}
$InstallRoot = Assert-SafeInstallRoot $InstallRoot
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
$SourceRoot = Get-FullPath $SourceRoot

$extensionSource = Join-Path $SourceRoot "extension"
$manifestPath = Join-Path $extensionSource "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
  throw "Extension manifest was not found under: $extensionSource"
}
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

$requiredTools = @("start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "uninstall_release.ps1", "sync_release_tools.ps1")
foreach ($name in $requiredTools) {
  if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name) -PathType Leaf)) {
    throw "Required installer component is missing: tools\$name"
  }
}
$updaterSource = @(
  (Join-Path $PSScriptRoot "DianAgentUpdater.exe"),
  (Join-Path $SourceRoot "dist\agent\DianAgentUpdater.exe")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $updaterSource) { throw "DianAgentUpdater.exe is missing from the release." }

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
foreach ($name in @("data", "config", "knowledge", "backup", "logs", "app", "extension", "tools")) {
  New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot $name) | Out-Null
}

$appTarget = Join-Path $InstallRoot ("app\{0}" -f $version)
$extensionTarget = Join-Path $InstallRoot ("extension\{0}" -f $version)
$ownedAppRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot "app")).TrimEnd('\') + '\'
Get-CimInstance Win32_Process -Filter "Name='DianAgent.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.ExecutablePath) {
    $processPath = [IO.Path]::GetFullPath([string]$_.ExecutablePath)
    if ($processPath.StartsWith($ownedAppRoot, [StringComparison]::OrdinalIgnoreCase)) {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
}
Reset-VersionDirectory (Join-Path $InstallRoot "app") $appTarget
Reset-VersionDirectory (Join-Path $InstallRoot "extension") $extensionTarget
Copy-Item -LiteralPath $agentSource -Destination (Join-Path $appTarget "DianAgent.exe") -Force
Copy-DirectoryContents $extensionSource $extensionTarget

# Chrome keeps the unpacked extension path. Keep that path stable across releases.
$stableExtension = Join-Path $InstallRoot "extension-current"
$extensionStage = Join-Path $InstallRoot (".extension-current-stage-{0}" -f $PID)
$extensionPrevious = Join-Path $InstallRoot ".extension-current-previous"
foreach ($path in @($extensionStage, $extensionPrevious)) {
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Copy-DirectoryContents $extensionSource $extensionStage
try {
  if (Test-Path -LiteralPath $stableExtension) { Move-Item -LiteralPath $stableExtension -Destination $extensionPrevious }
  Move-Item -LiteralPath $extensionStage -Destination $stableExtension
  if (Test-Path -LiteralPath $extensionPrevious) { Remove-Item -LiteralPath $extensionPrevious -Recurse -Force }
} catch {
  if (-not (Test-Path -LiteralPath $stableExtension) -and (Test-Path -LiteralPath $extensionPrevious)) {
    Move-Item -LiteralPath $extensionPrevious -Destination $stableExtension
  }
  throw
}

foreach ($name in @("install_release.ps1", "uninstall_release.ps1", "start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "sync_release_tools.ps1")) {
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $InstallRoot "tools\$name") -Force
}
Copy-Item -LiteralPath $updaterSource -Destination (Join-Path $InstallRoot "tools\DianAgentUpdater.exe") -Force

Set-Content -LiteralPath (Join-Path $InstallRoot "current-version.txt") -Encoding ASCII -Value $version
$offlinePointer = Join-Path $InstallRoot "current.json"
if (Test-Path -LiteralPath $offlinePointer) { Remove-Item -LiteralPath $offlinePointer -Force }
$marker = [ordered]@{
  product = "DianAgent"
  schema = 1
  install_root = $InstallRoot
  current_version = $version
  updated_at = [DateTime]::UtcNow.ToString("o")
}
$marker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallRoot ".dian-agent-install.json") -Encoding UTF8

$powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$watchdog = Join-Path $InstallRoot "tools\watchdog_release.ps1"
$watchdogLauncher = Join-Path $InstallRoot "tools\watchdog_release.vbs"
$startAgent = Join-Path $InstallRoot "tools\start_agent.ps1"
$startArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f $startAgent, $InstallRoot

if (-not $SkipAutostart) {
  $shell = New-Object -ComObject WScript.Shell
  $developmentTask = Get-ScheduledTask -TaskName "DianAgentDevKeepAlive" -ErrorAction SilentlyContinue
  if ($developmentTask) {
    $ownedDevelopmentTask = $developmentTask.Actions | Where-Object {
      $_.Arguments -and $_.Arguments.Contains("watchdog.vbs")
    }
    if ($ownedDevelopmentTask) {
      Unregister-ScheduledTask -TaskName "DianAgentDevKeepAlive" -Confirm:$false
    }
  }
  $developmentShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgentDev.lnk"
  if (Test-Path -LiteralPath $developmentShortcut -PathType Leaf) {
    $candidate = $shell.CreateShortcut($developmentShortcut)
    if ($candidate.Arguments -and $candidate.Arguments.Contains("watchdog.vbs")) {
      Remove-Item -LiteralPath $developmentShortcut -Force
    }
  }
  $programsShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Dian Agent.lnk"
  $shortcut = $shell.CreateShortcut($programsShortcut)
  $shortcut.TargetPath = $powershell
  $shortcut.Arguments = $startArguments
  $shortcut.WorkingDirectory = $InstallRoot
  $shortcut.IconLocation = (Join-Path $appTarget "DianAgent.exe") + ",0"
  $shortcut.Description = "Start Dian Agent"
  $shortcut.Save()

  $startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "DianAgent.lnk"
  $shortcut = $shell.CreateShortcut($startupShortcut)
  $shortcut.TargetPath = $wscript
  $shortcut.Arguments = '"' + $watchdogLauncher + '"'
  $shortcut.WorkingDirectory = $InstallRoot
  $shortcut.IconLocation = (Join-Path $appTarget "DianAgent.exe") + ",0"
  $shortcut.Description = "Start Dian Agent after Windows sign-in"
  $shortcut.Save()

  $taskAction = New-ScheduledTaskAction -Execute $wscript -Argument ('"' + $watchdogLauncher + '"')
  $taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
  $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName "DianAgentKeepAlive" -Action $taskAction -Trigger $taskTrigger `
    -Settings $taskSettings -Description "Keeps the Dian Agent local service available." -Force | Out-Null

  $runtimeDir = Join-Path $InstallRoot "data\runtime"
  New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
  [ordered]@{
    schema_version = 1
    state = "configured"
    state_label = "自动启动与保活已配置"
    autostart_enabled = $true
    keepalive_enabled = $true
    hidden_launcher = $true
    source = "release_install"
    task_name = "DianAgentKeepAlive"
    last_checked_at = [DateTime]::UtcNow.ToString("o")
    last_healthy_at = $null
    last_recovery_at = $null
    last_error = $null
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir "startup-state.json") -Encoding UTF8
}

if (-not $SkipLaunch) {
  & $powershell -NoProfile -ExecutionPolicy Bypass -File $startAgent -InstallRoot $InstallRoot
  if ($LASTEXITCODE -ne 0) { throw "Dian Agent was installed but did not become healthy." }

  $browser = $null
  foreach ($candidate in @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
  )) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $browser = $candidate; break }
  }
  if ($browser) { Start-Process -FilePath $browser -ArgumentList "chrome://extensions/" | Out-Null }
  Start-Process -FilePath "explorer.exe" -ArgumentList ('"' + $stableExtension + '"') | Out-Null
}

Write-Host ""
Write-Host "Dian Agent $version installed successfully." -ForegroundColor Green
Write-Host "Application: $appTarget"
Write-Host "Browser extension (stable path): $stableExtension"
Write-Host "After an upgrade, click Reload on chrome://extensions if the browser has not refreshed it yet."
Write-Host "User data: $(Join-Path $InstallRoot 'data')"
if ($SkipAutostart) { Write-Host "Automatic startup was skipped." }
if ($SkipLaunch) { Write-Host "Initial launch was skipped." }
