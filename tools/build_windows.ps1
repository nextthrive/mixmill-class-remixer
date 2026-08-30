param(
    [string]$Python = "",
    [switch]$SkipInstall,
    [switch]$Release,
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "https://timestamp.digicert.com",
    [string]$SignTool = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Build = Join-Path $Root ".build"
$Venv = Join-Path $Build "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

function Resolve-SignTool([string]$Requested) {
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested)) { throw "SignTool not found: $Requested" }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $Command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $KitRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $KitRoot) {
        $Candidate = Get-ChildItem -Path (Join-Path $KitRoot "*\x64\signtool.exe") -File |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($Candidate) { return $Candidate.FullName }
    }
    return ""
}

function Sign-ReleaseArtifact([string]$Path) {
    & $script:ResolvedSignTool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 /d "MixMill Desktop" $Path
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $Path" }
    & $script:ResolvedSignTool verify /pa /all /tw $Path
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $Path" }
}

if (-not $Python) {
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py) { $Python = $Py.Source }
    else {
        $Py = Get-Command python -ErrorAction SilentlyContinue
        if ($Py) { $Python = $Py.Source }
    }
}
if (-not $Python) {
    throw "Python 3.11+ is required to build. Pass -Python C:\\path\\to\\python.exe."
}

New-Item -ItemType Directory -Force -Path $Build | Out-Null
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create build virtual environment" }
}

Push-Location $Root
try {
    $AppVersion = (& $VenvPython -c "from desktop.version import APP_VERSION; print(APP_VERSION)").Trim()
    $AppVersionNumeric = (& $VenvPython -c "from desktop.version import FILE_VERSION; print('.'.join(map(str, FILE_VERSION)))").Trim()
}
finally { Pop-Location }
if (-not $AppVersion -or -not $AppVersionNumeric) { throw "Could not read desktop version metadata" }

$script:ResolvedSignTool = ""
if ($Release) {
    if ($AppVersion.Contains("-")) { throw "Release build requires a stable APP_VERSION" }
    if (-not $CertificateThumbprint) { throw "Release build requires -CertificateThumbprint" }
    $script:ResolvedSignTool = Resolve-SignTool $SignTool
    if (-not $script:ResolvedSignTool) { throw "Release build requires SignTool from the Windows SDK" }
}
if (-not $SkipInstall) {
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip" }
    & $VenvPython -m pip install -r (Join-Path $Root "requirements-windows.txt")
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Windows requirements" }
}

& $VenvPython (Join-Path $Root "tools\fetch_ffmpeg_windows.py")
if ($LASTEXITCODE -ne 0) { throw "Failed to fetch FFmpeg" }
& $VenvPython (Join-Path $Root "tools\make_windows_icon.py")
if ($LASTEXITCODE -ne 0) { throw "Failed to create Windows icon" }
& $VenvPython (Join-Path $Root "tools\make_windows_version.py")
if ($LASTEXITCODE -ne 0) { throw "Failed to create Windows version metadata" }
& (Join-Path $Root "tools\fetch_webview2.ps1")
if ($LASTEXITCODE -ne 0) { throw "Failed to fetch verified WebView2 prerequisite" }
& (Join-Path $Root "tools\prepare_nsis.ps1")
if ($LASTEXITCODE -ne 0) { throw "Failed to prepare NSIS" }
& $VenvPython -m py_compile (Join-Path $Root "app\main.py") (Join-Path $Root "desktop\runtime.py") (Join-Path $Root "desktop\launcher.py") (Join-Path $Root "desktop\version.py")
if ($LASTEXITCODE -ne 0) { throw "Python compilation failed" }
& $VenvPython (Join-Path $Root "tests\desktop_smoke.py")
if ($LASTEXITCODE -ne 0) { throw "Desktop source smoke test failed" }
& $VenvPython -m PyInstaller --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath (Join-Path $Build "pyinstaller") (Join-Path $Root "MixMill.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
$PackagedRoot = Join-Path $Root "dist\MixMill"
foreach ($Document in @("LICENSE", "PRIVACY.md", "SUPPORT.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md")) {
    Copy-Item -LiteralPath (Join-Path $Root $Document) -Destination (Join-Path $PackagedRoot $Document) -Force
}
$PackagedExe = Join-Path $Root "dist\MixMill\MixMill.exe"
if ($Release) { Sign-ReleaseArtifact $PackagedExe }

$SmokeRoot = Join-Path $Build "artifact-smoke-local"
$SmokeMedia = Join-Path $Build "artifact-smoke-media"
New-Item -ItemType Directory -Force -Path $SmokeRoot,$SmokeMedia | Out-Null
$OldLocal = $env:LOCALAPPDATA
$OldMedia = $env:MIXMILL_DESKTOP_SMOKE_MEDIA
try {
    $env:LOCALAPPDATA = $SmokeRoot
    $env:MIXMILL_DESKTOP_SMOKE_MEDIA = $SmokeMedia
    $Process = Start-Process -FilePath $PackagedExe -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        $Log = Join-Path $SmokeRoot "MixMill\logs\desktop.log"
        if (Test-Path -LiteralPath $Log) { Get-Content -LiteralPath $Log -Tail 80 }
        throw "Packaged MixMill smoke test failed with exit code $($Process.ExitCode)"
    }
}
finally {
    $env:LOCALAPPDATA = $OldLocal
    $env:MIXMILL_DESKTOP_SMOKE_MEDIA = $OldMedia
}

$Artifacts = Join-Path $Root "artifacts"
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
$Zip = Join-Path $Artifacts "MixMill-$AppVersion-Windows-x64-portable.zip"
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -Path (Join-Path $Root "dist\MixMill\*") -DestinationPath $Zip -CompressionLevel Optimal

$FfmpegSource = Join-Path $Build "ffmpeg-bf1b838f2a-source.zip"
if (-not (Test-Path -LiteralPath $FfmpegSource)) { throw "FFmpeg corresponding source archive is missing" }
$SourceArtifact = Join-Path $Artifacts "MixMill-$AppVersion-FFmpeg-bf1b838f2a-source.zip"
Copy-Item -LiteralPath $FfmpegSource -Destination $SourceArtifact -Force

$MakeNsis = Join-Path $Build "nsis-3.12\makensis.exe"
if (-not (Test-Path -LiteralPath $MakeNsis)) { throw "NSIS compiler is missing" }
& $MakeNsis "/DAPP_VERSION=$AppVersion" "/DAPP_VERSION_NUMERIC=$AppVersionNumeric" (Join-Path $Root "installer\MixMill.nsi")
if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed" }
$Setup = Join-Path $Artifacts "MixMill-$AppVersion-Windows-x64-Setup.exe"
if (-not (Test-Path -LiteralPath $Setup)) { throw "Installer artifact is missing" }
if ($Release) {
    Sign-ReleaseArtifact $Setup
}
& (Join-Path $Root "tests\installer_smoke.ps1") -Setup $Setup
if ($LASTEXITCODE -ne 0) { throw "Installer lifecycle smoke test failed" }

$ReleaseFiles = @($Zip, $Setup, $SourceArtifact)
$ManifestArtifacts = foreach ($Artifact in $ReleaseFiles) {
    [ordered]@{
        name = [IO.Path]::GetFileName($Artifact)
        bytes = (Get-Item -LiteralPath $Artifact).Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
    }
}
$Manifest = [ordered]@{
    product = "MixMill Desktop"
    version = $AppVersion
    platform = "Windows x64"
    created_utc = [DateTime]::UtcNow.ToString("o")
    license = "AGPL-3.0-or-later"
    distribution = "open-source-community"
    signed_release = [bool]$Release
    artifacts = $ManifestArtifacts
}
$ManifestPath = Join-Path $Artifacts "MixMill-$AppVersion-release.json"
[IO.File]::WriteAllText($ManifestPath, ($Manifest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))

$ChecksumFile = Join-Path $Artifacts "SHA256SUMS.txt"
$ChecksumLines = foreach ($Artifact in @($ReleaseFiles + $ManifestPath)) {
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
    "$Hash  $([IO.Path]::GetFileName($Artifact))"
}
[IO.File]::WriteAllLines($ChecksumFile, $ChecksumLines, [Text.UTF8Encoding]::new($false))

Write-Output "Windows artifact: $Zip"
Write-Output "Windows installer: $Setup"
Write-Output "FFmpeg source: $SourceArtifact"
Write-Output "Release manifest: $ManifestPath"
Write-Output "Checksums: $ChecksumFile"
if (-not $Release) {
    Write-Warning "Unsigned community release: Windows may show a SmartScreen warning. Publish source, LICENSE, FFmpeg source, manifest, and checksums beside it."
}
