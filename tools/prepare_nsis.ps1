$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Build = Join-Path $Root ".build"
$Version = "3.12"
$Archive = Join-Path $Build "nsis-$Version.zip"
$Download = "$Archive.download"
$Destination = Join-Path $Build "nsis-$Version"
$Compiler = Join-Path $Destination "makensis.exe"
$Url = "https://netix.dl.sourceforge.net/project/nsis/NSIS%203/3.12/nsis-3.12.zip"
$Sha256 = "56581F90DB321581C5381193D796FFFCF2D24B2F8FED2160A6C6A3BAA67F2C4F"

function Test-NsisArchive([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash -eq $Sha256
}

if (Test-Path -LiteralPath $Compiler) {
    Write-Output "Portable NSIS ready: $Compiler"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Build | Out-Null
if (-not (Test-NsisArchive $Archive)) {
    Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
    & curl.exe -L --fail --retry 3 --output $Download $Url
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
        throw "Could not download the NSIS compiler archive"
    }
    if (-not (Test-NsisArchive $Download)) {
        Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
        throw "Downloaded NSIS archive failed SHA-256 verification"
    }
    Move-Item -LiteralPath $Download -Destination $Archive -Force
}

Expand-Archive -LiteralPath $Archive -DestinationPath $Build -Force
if (-not (Test-Path -LiteralPath $Compiler)) {
    throw "Portable NSIS archive did not contain makensis.exe"
}
Write-Output "Portable NSIS ready: $Compiler"
