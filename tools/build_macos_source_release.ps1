param(
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

function Assert-NoReparsePointPathChain([string]$Path) {
  $cursor = [IO.Path]::GetFullPath($Path)
  while ($cursor) {
    if (Test-Path -LiteralPath $cursor) {
      $item = Get-Item -LiteralPath $cursor -Force
      if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Unsafe reparse point in macOS release path: $cursor"
      }
    }
    $trimmed = $cursor.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $parent = [IO.Path]::GetDirectoryName($trimmed)
    if (-not $parent -or $parent -eq $cursor) { break }
    $cursor = $parent
  }
}

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
  throw "Python is required to verify and package the public macOS source release."
}

# Match the Windows community build boundary: never package directly from the
# developer checkout, which may contain the registered internal runtime.  The
# preparer rejects unexpected private material and omits only the reviewed
# commercial allow-list before any source file is copied into the Mac bundle.
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("DianAgent-macos-public-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))
$publicSource = Join-Path $temporaryRoot "source"
$prepare = Join-Path $PSScriptRoot "prepare_public_source.py"
$stageFull = $null
$zipPath = $null
$releaseFull = $null
$buildComplete = $false

try {
  New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
  & $python $prepare --source $projectDir --destination $publicSource
  if ($LASTEXITCODE -ne 0) { throw "Verified public source preparation failed." }

  $manifest = Get-Content -LiteralPath (Join-Path $publicSource "extension\manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  $version = [string]$manifest.version
  if ($version -notmatch '\A[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?\z') {
    throw "Extension version is missing or invalid."
  }
  $versionSource = Get-Content -LiteralPath (Join-Path $publicSource "bridge\version.py") -Raw -Encoding UTF8
  $versionMatch = [regex]::Match($versionSource, '(?m)^\s*AGENT_VERSION\s*=\s*["'']([^"'']+)["'']\s*$')
  if (-not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $version) {
    throw "Agent version does not exactly match the extension/package version."
  }
  $releaseCheck = Join-Path $publicSource "tools\check_public_release.py"
  & $python $releaseCheck --source $publicSource
  if ($LASTEXITCODE -ne 0) { throw "Prepared public source boundary check failed." }

  $releaseRoot = Join-Path $projectDir "dist\release"
  $stage = Join-Path $releaseRoot "DianAgent-v$version-macos-source"
  $zipPath = "$stage.zip"
  $releaseFull = [IO.Path]::GetFullPath($releaseRoot).TrimEnd("\") + "\"
  $stageFull = [IO.Path]::GetFullPath($stage)
  if (-not $stageFull.StartsWith($releaseFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe macOS staging path: $stageFull"
  }
  Assert-NoReparsePointPathChain $releaseRoot
  Assert-NoReparsePointPathChain $stageFull
  Assert-NoReparsePointPathChain $zipPath
  if (Test-Path -LiteralPath $stageFull) { Remove-Item -LiteralPath $stageFull -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $stageFull | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $stageFull "bridge") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $stageFull "extension") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $stageFull "tools\macos") | Out-Null

  Get-ChildItem -LiteralPath (Join-Path $publicSource "bridge") -Filter "*.py" -File |
    Where-Object { $_.Name -notlike "test_*.py" } |
    Copy-Item -Destination (Join-Path $stageFull "bridge") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "bridge\requirements-agent.txt") -Destination (Join-Path $stageFull "bridge\requirements-agent.txt") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "assets") -Destination (Join-Path $stageFull "assets") -Recurse -Force
  Copy-Item -Path (Join-Path $publicSource "extension\*") -Destination (Join-Path $stageFull "extension") -Recurse -Force
  Get-ChildItem -LiteralPath (Join-Path $stageFull "extension") -Filter "test-*.js" -File | Remove-Item -Force
  Remove-Item -LiteralPath (Join-Path $stageFull "extension\manifest.compat.json") -Force -ErrorAction SilentlyContinue
  $runtimeTools = @(
    "launch_agent.sh",
    "repair_dian_agent.command",
    "uninstall_dian_agent.command",
    "install_dian_agent_source.command",
    "atomic_directory_update.sh",
    "recovery_bootstrap.sh",
    "initialize_local_api_trust.py",
    "verify_local_api.sh"
  )
  foreach ($name in $runtimeTools) {
    Copy-Item -LiteralPath (Join-Path $publicSource "tools\macos\$name") -Destination (Join-Path $stageFull "tools\macos\$name") -Force
  }
  Copy-Item -LiteralPath (Join-Path $publicSource "README-MAC.md") -Destination (Join-Path $stageFull "README-MAC.md") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "LICENSE") -Destination (Join-Path $stageFull "LICENSE") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "tools\macos\install_dian_agent_source.command") -Destination (Join-Path $stageFull "install_dian_agent.command") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "tools\macos\repair_dian_agent.command") -Destination (Join-Path $stageFull "repair_dian_agent.command") -Force
  Copy-Item -LiteralPath (Join-Path $publicSource "tools\macos\uninstall_dian_agent.command") -Destination (Join-Path $stageFull "uninstall_dian_agent.command") -Force
  Set-Content -LiteralPath (Join-Path $stageFull "macos-architecture.txt") -Encoding ASCII -Value "source"

  & $python $releaseCheck --artifact $stageFull
  if ($LASTEXITCODE -ne 0) { throw "Public macOS source staging boundary check failed." }
  foreach ($oldOutput in @($zipPath, "$zipPath.sha256")) {
    if (Test-Path -LiteralPath $oldOutput) { Remove-Item -LiteralPath $oldOutput -Force }
  }

  & $python (Join-Path $publicSource "tools\create_unix_zip.py") $stageFull $zipPath
  if ($LASTEXITCODE -ne 0) { throw "macOS source ZIP creation failed." }
  & $python -c "import sys, zipfile; archive = zipfile.ZipFile(sys.argv[1]); bad = archive.testzip(); archive.close(); raise SystemExit(1 if bad else 0)" $zipPath
  if ($LASTEXITCODE -ne 0) { throw "macOS source ZIP integrity verification failed." }
  & $python $releaseCheck --artifact $zipPath
  if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    throw "Public macOS source ZIP boundary check failed."
  }
  $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
  Set-Content -LiteralPath "$zipPath.sha256" -Encoding ASCII -Value "$hash  $(Split-Path -Leaf $zipPath)"
  $buildComplete = $true
  Write-Host "macOS source release: $zipPath"
  Write-Host "SHA-256: $hash"
  Write-Host "This source preview requires Python 3.10+ and network access during first install."
} finally {
  if (-not $buildComplete) {
    if ($stageFull -and $releaseFull -and
        $stageFull.StartsWith($releaseFull, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $stageFull).StartsWith("DianAgent-v", [StringComparison]::Ordinal)) {
      $stageItem = Get-Item -LiteralPath $stageFull -Force -ErrorAction SilentlyContinue
      if ($stageItem -and (($stageItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)) {
        Remove-Item -LiteralPath $stageFull -Recurse -Force -ErrorAction SilentlyContinue
      }
    }
    if ($zipPath -and $releaseFull -and
        [IO.Path]::GetFullPath($zipPath).StartsWith($releaseFull, [StringComparison]::OrdinalIgnoreCase)) {
      foreach ($failedOutput in @($zipPath, "$zipPath.sha256")) {
        $failedItem = Get-Item -LiteralPath $failedOutput -Force -ErrorAction SilentlyContinue
        if (-not $failedItem -or (($failedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)) {
          Remove-Item -LiteralPath $failedOutput -Force -ErrorAction SilentlyContinue
        }
      }
    }
  }
  $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
  $systemTemporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\") + "\"
  if ($resolvedTemporary.StartsWith($systemTemporary, [StringComparison]::OrdinalIgnoreCase) -and
      (Split-Path -Leaf $resolvedTemporary).StartsWith("DianAgent-macos-public-", [StringComparison]::Ordinal)) {
    Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
  }
}
