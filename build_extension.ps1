$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExtensionDir = Join-Path $ProjectDir "extension"
$ManifestPath = Join-Path $ExtensionDir "manifest.json"
$DistDir = Join-Path $ProjectDir "dist"

$Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
if ($Manifest.manifest_version -ne 3) { throw "manifest_version must be 3" }
if ($Manifest.host_permissions -contains "<all_urls>") { throw "<all_urls> is not allowed" }

$RequiredFiles = @(
  "manifest.json", "background.js", "content-common.js", "content-doudian.js",
  "content-qianchuan.js", "popup.html", "popup.css", "popup.js",
  "sidepanel.html", "sidepanel.css", "sidepanel.js", "icon48.png", "icon128.png",
  "upgrade.html", "upgrade.js", "sync.html", "sync.js", "scan.html", "scan.js", "cancel-scan.html", "cancel-scan.js", "retry-scan.html", "retry-scan.js", "smoke-scan.html", "smoke-scan.js"
)
foreach ($File in $RequiredFiles) {
  if (-not (Test-Path (Join-Path $ExtensionDir $File))) { throw "Missing extension file: $File" }
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$ZipPath = Join-Path $DistDir ("dian-agent-chrome-v" + $Manifest.version + ".zip")
if (Test-Path $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

$PackageFiles = $RequiredFiles | ForEach-Object { Join-Path $ExtensionDir $_ }
Compress-Archive -Path $PackageFiles -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Extension package: $ZipPath"
Write-Host "Chrome unpacked extension directory: $ExtensionDir"
