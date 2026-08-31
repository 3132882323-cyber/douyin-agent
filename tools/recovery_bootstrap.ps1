[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [ValidateRange(1, 300)][int]$RecoveryLockTimeoutSeconds = 1,
  [ValidateRange(1, 65535)][int]$Port = 8765
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallRoot) {
  $InstallRoot = Split-Path -Parent $PSScriptRoot
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$expectedBootstrapRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot "bootstrap")).TrimEnd([IO.Path]::DirectorySeparatorChar)
$actualBootstrapRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
if (-not $actualBootstrapRoot.Equals($expectedBootstrapRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Recovery bootstrap is not running from the exact installation bootstrap directory."
}

function Assert-BootstrapPathChain([string]$Path, [string]$Label) {
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

Assert-BootstrapPathChain $InstallRoot "Installation root"
Assert-BootstrapPathChain $PSScriptRoot "Recovery bootstrap"
$policyPath = Join-Path $PSScriptRoot "windows_trust_policy.ps1"
if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf) -or
    ((Get-Item -LiteralPath $policyPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw "Stable Windows recovery policy is missing or unsafe."
}
. $policyPath

try {
  [void](Invoke-DianRecoverInstallTransaction $InstallRoot $RecoveryLockTimeoutSeconds)
  if (Get-Command Invoke-DianRecoverReleaseToolsTransaction -ErrorAction SilentlyContinue) {
    [void](Invoke-DianRecoverReleaseToolsTransaction $InstallRoot $RecoveryLockTimeoutSeconds)
  }
} catch {
  # A live installer owns the same per-install mutex. The next five-minute task
  # run retries; launching around a live or ambiguous transaction is forbidden.
  exit 4
}

$watchdog = Join-Path $InstallRoot "tools\watchdog_release.ps1"
if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf) -or
    ((Get-Item -LiteralPath $watchdog -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  exit 4
}
Assert-BootstrapPathChain $watchdog "Recovered watchdog"
& $watchdog -InstallRoot $InstallRoot -Port $Port
exit $LASTEXITCODE
