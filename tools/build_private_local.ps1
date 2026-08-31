param(
  [string]$PythonPath = "",
  [switch]$Install,
  [string]$InstallRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectDir = Split-Path -Parent $PSScriptRoot
$python = $PythonPath
if (-not $python) {
  $candidate = Join-Path $projectDir "bridge\.venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $candidate) { $python = $candidate }
}
if (-not $python) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
  throw "Python 3.10+ with PyInstaller is required for the internal local build."
}

function New-SecureBuildDirectory([string]$Prefix) {
  $temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
  $leaf = "{0}-{1}-{2}" -f $Prefix, $PID, [Guid]::NewGuid().ToString("N")
  $candidate = [IO.Path]::GetFullPath((Join-Path $temporaryBase $leaf))
  if (-not $candidate.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase) -or
      (Split-Path -Leaf $candidate) -ne $leaf -or (Test-Path -LiteralPath $candidate)) {
    throw "Unsafe or already-existing internal build directory: $candidate"
  }
  New-Item -ItemType Directory -Path $candidate | Out-Null
  if ((Get-Item -LiteralPath $candidate -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Internal build directory became a reparse point: $candidate"
  }
  $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls.exe $candidate /inheritance:r /grant:r "*${userSid}:(OI)(CI)(F)" "*S-1-5-18:(OI)(CI)(F)" "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    throw "Could not protect the internal build directory ACL."
  }
  return $candidate
}

function Assert-BuildPathChainNoReparsePoints([string]$Path, [string]$Label) {
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

$temporaryRoot = New-SecureBuildDirectory "DianAgent-private"
$privateSource = Join-Path $temporaryRoot "source"
$privateOutput = Join-Path $projectDir "dist\private-local"
$buildOutput = Join-Path $projectDir "dist\private-local-build"
$prepare = Join-Path $PSScriptRoot "prepare_private_local_source.py"
foreach ($buildPath in @($temporaryRoot, $privateSource, $privateOutput, $buildOutput)) {
  Assert-BuildPathChainNoReparsePoints $buildPath "Internal build path"
}

try {
  if (-not (Test-Path -LiteralPath $temporaryRoot -PathType Container) -or
      ((Get-Item -LiteralPath $temporaryRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Protected internal build directory is missing or unsafe."
  }
  & $python $prepare --source $projectDir --destination $privateSource
  if ($LASTEXITCODE -ne 0) { throw "Internal source preparation failed." }

  & $python (Join-Path $PSScriptRoot "verify_private_source.py") (Join-Path $privateSource "bridge")
  if ($LASTEXITCODE -ne 0) { throw "Internal commercial runtime import verification failed." }

  # Reject a source-version mismatch before either expensive compiler runs.
  # build_agent.ps1 repeats this check as a direct-build safety boundary.
  $manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $privateSource "extension\manifest.json") | ConvertFrom-Json
  $version = [string]$manifest.version
  $versionSource = Get-Content -LiteralPath (Join-Path $privateSource "bridge\version.py") -Raw -Encoding UTF8
  $versionMatch = [regex]::Match($versionSource, '(?m)^\s*AGENT_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
  if (-not $version -or -not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $version) {
    throw "Internal source version mismatch: bridge/version.py and extension/manifest.json must match exactly before building."
  }

  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_agent.ps1") `
    -PythonPath $python -SourceRoot $privateSource -DistDir (Join-Path $buildOutput "agent") `
    -WorkRoot (Join-Path $privateSource "dist\pyinstaller")
  if ($LASTEXITCODE -ne 0) { throw "Internal Agent build failed." }

  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_browser_packages.ps1") `
    -PythonPath $python -SourceRoot $privateSource -DistDir $buildOutput
  if ($LASTEXITCODE -ne 0) { throw "Internal browser package build failed." }

  $builtAgent = Join-Path $buildOutput "agent\DianAgent.exe"
  $agentVersionInfo = (Get-Item -LiteralPath $builtAgent).VersionInfo
  $agentFileVersion = [string]$agentVersionInfo.FileVersion
  $agentProductVersion = [string]$agentVersionInfo.ProductVersion
  $modernExtensionVersion = [string](Get-Content -LiteralPath (Join-Path $buildOutput "dian-agent-modern\manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
  $compatibleExtensionVersion = [string](Get-Content -LiteralPath (Join-Path $buildOutput "dian-agent-compatible\manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
  if ($agentFileVersion -ne $version -or $agentProductVersion -ne $version -or
      $modernExtensionVersion -ne $version -or $compatibleExtensionVersion -ne $version) {
    throw "Internal build version evidence does not exactly match manifest version $version."
  }
  $bundle = Join-Path $privateOutput "DianAgent-v$version-internal"
  $privateRootFull = [IO.Path]::GetFullPath($privateOutput).TrimEnd("\") + "\"
  $bundleFull = [IO.Path]::GetFullPath($bundle)
  Assert-BuildPathChainNoReparsePoints $bundleFull "Internal bundle output"
  if (-not $bundleFull.StartsWith($privateRootFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe internal bundle path: $bundleFull"
  }
  if (Test-Path -LiteralPath $bundleFull) { Remove-Item -LiteralPath $bundleFull -Recurse -Force }
  foreach ($directory in @("app", "extension", "extension-compatible", "tools")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $bundleFull $directory) | Out-Null
  }
  Copy-Item -LiteralPath $builtAgent -Destination (Join-Path $bundleFull "app\DianAgent.exe") -Force
  Copy-Item -LiteralPath (Join-Path $buildOutput "agent\DianAgentUpdater.exe") -Destination (Join-Path $bundleFull "tools\DianAgentUpdater.exe") -Force
  Copy-Item -Path (Join-Path $buildOutput "dian-agent-modern\*") -Destination (Join-Path $bundleFull "extension") -Recurse -Force
  Copy-Item -Path (Join-Path $buildOutput "dian-agent-compatible\*") -Destination (Join-Path $bundleFull "extension-compatible") -Recurse -Force

  foreach ($name in @("install_release.ps1", "uninstall_release.ps1", "start_agent.ps1", "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1", "repair_agent.ps1", "repair_agent.vbs", "windows_trust_policy.ps1", "sync_release_tools.ps1")) {
    Copy-Item -LiteralPath (Join-Path $privateSource "tools\$name") -Destination (Join-Path $bundleFull "tools\$name") -Force
  }
  foreach ($name in @("install_dian_agent.bat", "upgrade_dian_agent.bat", "README.md", "BROWSER_SUPPORT.md", "DEPLOYMENT.md", "SECURITY.md", "LICENSE")) {
    Copy-Item -LiteralPath (Join-Path $privateSource $name) -Destination (Join-Path $bundleFull $name) -Force
  }
  [ordered]@{
    schema_version = 1
    product = "DianAgent"
    version = $version
    edition = "commercial"
    build_flavor = "private_commercial"
    commercial_modules_included = $true
    redistributable = $false
    notice = "INTERNAL USE ONLY - DO NOT REDISTRIBUTE"
    version_evidence = "exact_match"
    source_agent_version = $versionMatch.Groups[1].Value
    agent_file_version = $agentFileVersion
    agent_product_version = $agentProductVersion
    modern_extension_version = $modernExtensionVersion
    compatible_extension_version = $compatibleExtensionVersion
  } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundleFull "INTERNAL_COMMERCIAL_BUILD.json") -Encoding UTF8

  Write-Host "Internal commercial bundle: $bundleFull" -ForegroundColor Yellow
  Write-Host "Redistributable: false"
  if ($Install) {
    $arguments = @{ SourceRoot = $bundleFull }
    if ($InstallRoot) { $arguments.InstallRoot = $InstallRoot }
    & (Join-Path $bundleFull "tools\install_release.ps1") @arguments
  }
} finally {
  $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
  $systemTemporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\") + "\"
  if ($resolvedTemporary.StartsWith($systemTemporary, [StringComparison]::OrdinalIgnoreCase) -and
      (Split-Path -Leaf $resolvedTemporary).StartsWith("DianAgent-private-", [StringComparison]::Ordinal)) {
    $temporaryItem = Get-Item -LiteralPath $resolvedTemporary -Force -ErrorAction SilentlyContinue
    if ($temporaryItem -and -not ($temporaryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    } elseif ($temporaryItem) {
      Write-Warning "Refusing to recursively remove a reparse-point build directory: $resolvedTemporary"
    }
  }
}
