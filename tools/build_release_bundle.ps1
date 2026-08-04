param(
  [string]$PythonPath = "",
  [string]$InstallRoot = "",
  [string]$MinimumCurrentVersion = "3.7.0",
  [string]$MaximumCurrentVersion = "",
  [string]$SigningPrivateKeyPath = "",
  [string]$SigningKeyId = "",
  [switch]$DevelopmentTestSigning,
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$projectDirFull = [IO.Path]::GetFullPath($projectDir)
$bridgeDir = Join-Path $projectDir "bridge"
$manifestPath = Join-Path $projectDir "extension\manifest.json"
$extensionManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
$version = [string]$extensionManifest.version
if (-not $version) { throw "Extension version is missing." }

$pythonCommand = $PythonPath
if (-not $pythonCommand) {
  $venvPython = Join-Path $bridgeDir ".venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venvPython) { $pythonCommand = $venvPython }
}
if (-not $pythonCommand) {
  $pythonExecutable = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($pythonExecutable) { $pythonCommand = $pythonExecutable.Source }
}
if (-not $pythonCommand) { throw "Python 3.10+ with cryptography is required on the release build machine." }

$distribution = "production"
if ($DevelopmentTestSigning) {
  if ($SigningPrivateKeyPath -or $SigningKeyId) {
    throw "DevelopmentTestSigning cannot be combined with production signing parameters."
  }
  $distribution = "development_test"
  $SigningKeyId = "development-test-rfc8032-1"
} else {
  if (-not $SigningPrivateKeyPath -or -not $SigningKeyId) {
    throw "Production offline bundles require -SigningPrivateKeyPath and -SigningKeyId. The private key must be provisioned outside this repository."
  }
  if ($SigningKeyId -notmatch '^[a-z0-9][a-z0-9._-]{2,63}$') {
    throw "SigningKeyId must match ^[a-z0-9][a-z0-9._-]{2,63}$."
  }
  $privateKeyFull = [IO.Path]::GetFullPath($SigningPrivateKeyPath)
  if (-not (Test-Path -LiteralPath $privateKeyFull -PathType Leaf)) {
    throw "Offline signing private key does not exist: $privateKeyFull"
  }
  if ($privateKeyFull.StartsWith($projectDirFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Production signing private keys must stay outside the repository."
  }
  $SigningPrivateKeyPath = $privateKeyFull
}

if (-not $SkipBuild) {
  $releaseBuild = Join-Path $PSScriptRoot "build_release.ps1"
  if ($PythonPath) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $releaseBuild -PythonPath $PythonPath
  } else {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $releaseBuild
  }
  if ($LASTEXITCODE -ne 0) { throw "Release build failed." }
}

$programSource = Join-Path $projectDir "dist\agent\DianAgent.exe"
$modernSource = Join-Path $projectDir "dist\dian-agent-modern"
$compatibleSource = Join-Path $projectDir "dist\dian-agent-compatible"
foreach ($required in @($programSource, $modernSource, $compatibleSource)) {
  if (-not (Test-Path -LiteralPath $required)) { throw "Missing release artifact: $required" }
}

$outputRoot = Join-Path $projectDir "dist\offline"
$stagingDir = Join-Path $outputRoot ".bundle-content-$version"
$bundleName = if ($DevelopmentTestSigning) {
  "DianAgent-v$version-development-test-offline.zip"
} else {
  "DianAgent-v$version-offline.zip"
}
$bundlePath = Join-Path $outputRoot $bundleName
$outputRootFull = [IO.Path]::GetFullPath($outputRoot)
$stagingFull = [IO.Path]::GetFullPath($stagingDir)
if (-not $stagingFull.StartsWith($outputRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe bundle staging directory: $stagingFull"
}
New-Item -ItemType Directory -Force -Path $outputRootFull | Out-Null
if (Test-Path -LiteralPath $stagingFull) { Remove-Item -LiteralPath $stagingFull -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stagingFull "program") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stagingFull "extension\modern") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stagingFull "extension\compatible") | Out-Null

Copy-Item -LiteralPath $programSource -Destination (Join-Path $stagingFull "program\DianAgent.exe") -Force
Copy-Item -Path (Join-Path $modernSource "*") -Destination (Join-Path $stagingFull "extension\modern") -Recurse -Force
Copy-Item -Path (Join-Path $compatibleSource "*") -Destination (Join-Path $stagingFull "extension\compatible") -Recurse -Force

$fileEntries = @()
Get-ChildItem -LiteralPath $stagingFull -File -Recurse | Sort-Object FullName | ForEach-Object {
  $relative = $_.FullName.Substring($stagingFull.Length + 1).Replace("\", "/")
  $fileEntries += [ordered]@{
    path = $relative
    size = [long]$_.Length
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
  }
}

$compatibility = [ordered]@{
  platform = "windows"
  min_current_version = $MinimumCurrentVersion
  max_current_version = if ($MaximumCurrentVersion) { $MaximumCurrentVersion } else { $null }
}
$offlineManifest = [ordered]@{
  manifest_version = 1
  product = "DianAgent"
  version = $version
  distribution = $distribution
  compatibility = $compatibility
  files = $fileEntries
}
$offlineManifestPath = Join-Path $stagingFull "offline-manifest.json"
$offlineManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $offlineManifestPath -Encoding UTF8

# Signing happens after all payload hashes are final and before compression.
# The production private key is read from an explicitly supplied path outside
# the repository and is never copied into staging or printed to build logs.
$signerCode = @'
import base64
import json
import sys
from pathlib import Path

bridge_dir = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
key_id = sys.argv[3]
private_key_path = sys.argv[4]
development_test = sys.argv[5] == "1"
sys.path.insert(0, str(bridge_dir))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from offline_upgrade import canonical_offline_manifest_bytes, verify_offline_manifest_signature

if development_test:
    # RFC 8032 test vector 1. Public, non-secret test material.
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    ))
else:
    raw = Path(private_key_path).read_bytes()
    if b"-----BEGIN" in raw:
        loaded = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise TypeError("offline signing key must be an Ed25519 private key")
        key = loaded
    else:
        stripped = raw.strip()
        try:
            text = stripped.decode("ascii")
            key_bytes = bytes.fromhex(text) if len(text) == 64 else base64.b64decode(text, validate=True)
        except (UnicodeDecodeError, ValueError):
            key_bytes = stripped
        if len(key_bytes) != 32:
            raise ValueError("raw Ed25519 private key must contain a 32-byte seed")
        key = Ed25519PrivateKey.from_private_bytes(key_bytes)

manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
signature = key.sign(canonical_offline_manifest_bytes(manifest))
manifest["signature"] = {
    "algorithm": "ed25519",
    "key_id": key_id,
    "value": base64.b64encode(signature).decode("ascii"),
}
# This also proves that a production private key matches a public key pinned in
# the updater.  With no provisioned production public key the build fails here.
verify_offline_manifest_signature(manifest, allow_test_keys=development_test)
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
'@

$signerScriptPath = Join-Path $outputRootFull ".sign-offline-manifest-$PID.py"
Set-Content -LiteralPath $signerScriptPath -Encoding UTF8 -Value $signerCode
try {
  $signingPrivateKeyArgument = if ($DevelopmentTestSigning) { "-" } else { $SigningPrivateKeyPath }
  & $pythonCommand $signerScriptPath $bridgeDir $offlineManifestPath $SigningKeyId $signingPrivateKeyArgument $(if ($DevelopmentTestSigning) { "1" } else { "0" })
  if ($LASTEXITCODE -ne 0) { throw "Offline manifest signing failed." }
} finally {
  Remove-Item -LiteralPath $signerScriptPath -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $bundlePath) { Remove-Item -LiteralPath $bundlePath -Force }
Compress-Archive -Path (Join-Path $stagingFull "*") -DestinationPath $bundlePath -CompressionLevel Optimal
$bundleHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $bundlePath).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$bundlePath.sha256" -Encoding ASCII -Value "$bundleHash  $(Split-Path -Leaf $bundlePath)"
Remove-Item -LiteralPath $stagingFull -Recurse -Force

Write-Host "Offline release bundle: $bundlePath"
Write-Host "SHA-256: $bundleHash"

if ($InstallRoot) {
  $installArguments = @(
    (Join-Path $projectDir "bridge\offline_upgrade.py"),
    "install",
    $bundlePath,
    "--install-root",
    $InstallRoot
  )
  if ($DevelopmentTestSigning) { $installArguments += "--allow-test-keys" }
  & $pythonCommand @installArguments
  if ($LASTEXITCODE -ne 0) { throw "Offline bundle installation failed." }
}
