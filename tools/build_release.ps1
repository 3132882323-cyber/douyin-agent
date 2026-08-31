param(
  [string]$PythonPath = ""
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
  throw "Python 3.10+ is required to prepare and verify the public release."
}

function New-SecureBuildDirectory([string]$Prefix) {
  $temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
  $leaf = "{0}-{1}-{2}" -f $Prefix, $PID, [Guid]::NewGuid().ToString("N")
  $candidate = [IO.Path]::GetFullPath((Join-Path $temporaryBase $leaf))
  if (-not $candidate.StartsWith($temporaryBase, [StringComparison]::OrdinalIgnoreCase) -or
      (Split-Path -Leaf $candidate) -ne $leaf -or (Test-Path -LiteralPath $candidate)) {
    throw "Unsafe or already-existing release build directory: $candidate"
  }
  New-Item -ItemType Directory -Path $candidate | Out-Null
  if ((Get-Item -LiteralPath $candidate -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Release build directory became a reparse point: $candidate"
  }
  $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls.exe $candidate /inheritance:r /grant:r "*${userSid}:(OI)(CI)(F)" "*S-1-5-18:(OI)(CI)(F)" "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    throw "Could not protect the public release build directory ACL."
  }
  return $candidate
}

# Never compile the public executable from the developer checkout.  The checkout
# may intentionally contain ignored commercial modules; the preparer rejects all
# unexpected private material and omits only the fixed, reviewed allow-list.
$temporaryRoot = New-SecureBuildDirectory "DianAgent-public"
$publicSource = Join-Path $temporaryRoot "source"
$prepare = Join-Path $PSScriptRoot "prepare_public_source.py"
$coreBuilder = Join-Path $PSScriptRoot "build_release_core.ps1"
$outputRoot = Join-Path $projectDir "dist"

try {
  if (-not (Test-Path -LiteralPath $temporaryRoot -PathType Container) -or
      ((Get-Item -LiteralPath $temporaryRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Protected public build directory is missing or unsafe."
  }
  & $python $prepare --source $projectDir --destination $publicSource
  if ($LASTEXITCODE -ne 0) { throw "Verified public source preparation failed." }

  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $coreBuilder `
    -SourceRoot $publicSource -OutputRoot $outputRoot -PythonPath $python
  if ($LASTEXITCODE -ne 0) { throw "Public release build failed." }
} finally {
  $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
  $systemTemporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\") + "\"
  if ($resolvedTemporary.StartsWith($systemTemporary, [StringComparison]::OrdinalIgnoreCase) -and
      (Split-Path -Leaf $resolvedTemporary).StartsWith("DianAgent-public-", [StringComparison]::Ordinal)) {
    $temporaryItem = Get-Item -LiteralPath $resolvedTemporary -Force -ErrorAction SilentlyContinue
    if ($temporaryItem -and -not ($temporaryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    } elseif ($temporaryItem) {
      Write-Warning "Refusing to recursively remove a reparse-point build directory: $resolvedTemporary"
    }
  }
}

Write-Host "Public/community build completed from a verified sanitized source tree." -ForegroundColor Green
Write-Host "Commercial modules included: false"
