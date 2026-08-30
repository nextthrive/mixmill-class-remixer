$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Build = Join-Path $Root ".build"
$Target = Join-Path $Build "MicrosoftEdgeWebview2Setup.exe"
$Download = "$Target.download"
$Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

function Test-MicrosoftSignature([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    return ($Signature.Status -eq "Valid" -and
            $Signature.SignerCertificate.Subject -match "O=Microsoft Corporation")
}

New-Item -ItemType Directory -Force -Path $Build | Out-Null
if (Test-MicrosoftSignature $Target) {
    Write-Output "Verified WebView2 bootstrapper ready: $Target"
    exit 0
}

Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
Invoke-WebRequest -Uri $Url -OutFile $Download
if (-not (Test-MicrosoftSignature $Download)) {
    Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
    throw "Downloaded WebView2 bootstrapper lacks a valid Microsoft signature"
}
Move-Item -LiteralPath $Download -Destination $Target -Force
Write-Output "Verified WebView2 bootstrapper ready: $Target"

