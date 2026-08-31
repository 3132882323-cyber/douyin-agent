param(
  [Parameter(Mandatory = $true)][string]$SourceRoot,
  [Parameter(Mandatory = $true)][string]$OutputRoot,
  [Parameter(Mandatory = $true)][string]$PythonPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

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

$sourceRootFull = [IO.Path]::GetFullPath($SourceRoot)
$outputRootFull = [IO.Path]::GetFullPath($OutputRoot)
if (-not (Test-Path -LiteralPath $sourceRootFull -PathType Container)) { throw "Prepared source is missing: $sourceRootFull" }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Python is missing: $PythonPath" }
Assert-BuildPathChainNoReparsePoints $sourceRootFull "Prepared public source"
Assert-BuildPathChainNoReparsePoints $outputRootFull "Public build output"

$manifestPath = Join-Path $sourceRootFull "extension\manifest.json"
$manifest = Get-Content -Raw -Encoding UTF8 $manifestPath | ConvertFrom-Json
$version = [string]$manifest.version
if (-not $version) { throw "Extension version is missing." }
$versionSource = Get-Content -LiteralPath (Join-Path $sourceRootFull "bridge\version.py") -Raw -Encoding UTF8
$versionMatch = [regex]::Match($versionSource, '(?m)^\s*AGENT_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
if (-not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $version) {
  throw "Public source version mismatch: bridge/version.py and extension/manifest.json must match exactly."
}

$releaseCheck = Join-Path $sourceRootFull "tools\check_public_release.py"
& $PythonPath $releaseCheck --source $sourceRootFull
if ($LASTEXITCODE -ne 0) { throw "Prepared public source boundary check failed." }

$agentDir = Join-Path $outputRootFull "agent"
$modernOutput = Join-Path $outputRootFull "dian-agent-modern"
$compatibleOutput = Join-Path $outputRootFull "dian-agent-compatible"
foreach ($buildPath in @($agentDir, $modernOutput, $compatibleOutput)) {
  Assert-BuildPathChainNoReparsePoints $buildPath "Public build output"
}
$agentBuild = Join-Path $PSScriptRoot "build_agent.ps1"
$browserBuild = Join-Path $PSScriptRoot "build_browser_packages.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $agentBuild `
  -PythonPath $PythonPath -SourceRoot $sourceRootFull -DistDir $agentDir `
  -WorkRoot (Join-Path $sourceRootFull "dist\pyinstaller")
if ($LASTEXITCODE -ne 0) { throw "Standalone public Agent build failed." }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $browserBuild `
  -PythonPath $PythonPath -SourceRoot $sourceRootFull -DistDir $outputRootFull
if ($LASTEXITCODE -ne 0) { throw "Public browser package build failed." }

$agentExecutable = Join-Path $agentDir "DianAgent.exe"
$agentVersionInfo = (Get-Item -LiteralPath $agentExecutable).VersionInfo
$agentFileVersion = [string]$agentVersionInfo.FileVersion
$agentProductVersion = [string]$agentVersionInfo.ProductVersion
$modernExtensionVersion = [string](Get-Content -LiteralPath (Join-Path $modernOutput "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
$compatibleExtensionVersion = [string](Get-Content -LiteralPath (Join-Path $compatibleOutput "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
if ($agentFileVersion -ne $version -or $agentProductVersion -ne $version -or
    $modernExtensionVersion -ne $version -or $compatibleExtensionVersion -ne $version) {
  throw "Public build version evidence does not exactly match manifest version $version."
}

$buildMetadata = [ordered]@{
  schema_version = 1
  product = "DianAgent"
  version = $version
  edition = "community"
  build_flavor = "public_community"
  source_boundary = "verified_sanitized_source"
  commercial_modules_included = $false
  redistributable = $true
  version_evidence = "exact_match"
  source_agent_version = $versionMatch.Groups[1].Value
  agent_file_version = $agentFileVersion
  agent_product_version = $agentProductVersion
  modern_extension_version = $modernExtensionVersion
  compatible_extension_version = $compatibleExtensionVersion
}
$buildMetadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $agentDir "PUBLIC_BUILD.json") -Encoding UTF8

$releaseRoot = Join-Path $outputRootFull "release"
$releaseDir = Join-Path $releaseRoot "DianAgent-v$version"
$zipPath = Join-Path $releaseRoot "DianAgent-v$version-windows.zip"
$releaseRootFull = [IO.Path]::GetFullPath($releaseRoot)
$releaseDirFull = [IO.Path]::GetFullPath($releaseDir)
Assert-BuildPathChainNoReparsePoints $releaseRootFull "Public release output"
Assert-BuildPathChainNoReparsePoints $releaseDirFull "Public release staging directory"
Assert-BuildPathChainNoReparsePoints $zipPath "Public release ZIP"
if (-not $releaseDirFull.StartsWith($releaseRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe release directory: $releaseDirFull"
}
if (Test-Path -LiteralPath $releaseDirFull) { Remove-Item -LiteralPath $releaseDirFull -Recurse -Force }
$releaseDir = $releaseDirFull
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "app") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "extension") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "extension-compatible") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir "tools") | Out-Null

Copy-Item -LiteralPath $agentExecutable -Destination (Join-Path $releaseDir "app\DianAgent.exe") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "DianAgentUpdater.exe") -Destination (Join-Path $releaseDir "tools\DianAgentUpdater.exe") -Force
Copy-Item -Path (Join-Path $outputRootFull "dian-agent-modern\*") -Destination (Join-Path $releaseDir "extension") -Recurse -Force
Copy-Item -Path (Join-Path $outputRootFull "dian-agent-compatible\*") -Destination (Join-Path $releaseDir "extension-compatible") -Recurse -Force
$buildMetadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $releaseDir "distribution.json") -Encoding UTF8

$releaseTools = @(
  "install_release.ps1", "uninstall_release.ps1", "start_agent.ps1",
  "watchdog_release.ps1", "watchdog_release.vbs", "recovery_bootstrap.ps1", "repair_agent.ps1", "repair_agent.vbs",
  "windows_trust_policy.ps1", "sync_release_tools.ps1"
)
foreach ($name in $releaseTools) {
  Copy-Item -LiteralPath (Join-Path $sourceRootFull "tools\$name") -Destination (Join-Path $releaseDir "tools\$name") -Force
}

$rootFiles = @(
  "install_dian_agent.bat", "upgrade_dian_agent.bat", "README.md",
  "BROWSER_SUPPORT.md", "DEPLOYMENT.md", "SECURITY.md", "LICENSE"
)
foreach ($name in $rootFiles) {
  Copy-Item -LiteralPath (Join-Path $sourceRootFull $name) -Destination (Join-Path $releaseDir $name) -Force
}

& $PythonPath $releaseCheck --artifact $releaseDir
if ($LASTEXITCODE -ne 0) { throw "Public release staging boundary check failed." }

if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
& $PythonPath $releaseCheck --artifact $zipPath
if ($LASTEXITCODE -ne 0) {
  Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
  throw "Public release ZIP boundary check failed."
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zipPath.sha256" -Encoding ASCII -Value "$hash  $(Split-Path -Leaf $zipPath)"
Write-Host "Portable public Windows release: $zipPath"
Write-Host "SHA-256: $hash"
