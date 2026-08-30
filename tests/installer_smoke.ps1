param(
    [string]$Setup = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Setup) {
    $Version = (& (Join-Path $Root ".build\venv\Scripts\python.exe") -c "from desktop.version import APP_VERSION; print(APP_VERSION)").Trim()
    $Setup = Join-Path $Root "artifacts\MixMill-$Version-Windows-x64-Setup.exe"
}
$Setup = (Resolve-Path -LiteralPath $Setup).Path
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$TestRoot = Join-Path $Root ".build\installer-smoke-$Stamp"
$Install = Join-Path $TestRoot "Program"
$Local = Join-Path $TestRoot "LocalAppData"
$Media = Join-Path $TestRoot "Media originals"
New-Item -ItemType Directory -Force -Path $Install,$Local,$Media | Out-Null

$Sentinel = Join-Path $Media "original.mp4"
[IO.File]::WriteAllBytes($Sentinel, [Text.Encoding]::UTF8.GetBytes("original-media-must-not-change"))
$BeforeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Sentinel).Hash
$BeforeWrite = (Get-Item -LiteralPath $Sentinel).LastWriteTimeUtc

function Invoke-Checked([string]$File, [string[]]$Arguments, [string]$Label) {
    $Process = Start-Process -FilePath $File -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        if ($script:InstallerLog -and (Test-Path -LiteralPath $script:InstallerLog)) {
            Get-Content -LiteralPath $script:InstallerLog -Tail 80 | Write-Output
        }
        throw "$Label failed with exit code $($Process.ExitCode)"
    }
}

$InstallArgs = @("/S", "/D=$Install")
Invoke-Checked $Setup $InstallArgs "installer"
$InstalledExe = Join-Path $Install "MixMill.exe"
if (-not (Test-Path -LiteralPath $InstalledExe)) { throw "installed MixMill.exe is missing" }

$OldLocal = $env:LOCALAPPDATA
$OldMedia = $env:MIXMILL_DESKTOP_SMOKE_MEDIA
try {
    $env:LOCALAPPDATA = $Local
    $env:MIXMILL_DESKTOP_SMOKE_MEDIA = $Media
    Invoke-Checked $InstalledExe @("--smoke-test") "installed app smoke test"
}
finally {
    $env:LOCALAPPDATA = $OldLocal
    $env:MIXMILL_DESKTOP_SMOKE_MEDIA = $OldMedia
}

$AppData = Join-Path $Local "MixMill"
$Marker = Join-Path $AppData "upgrade-marker.txt"
[IO.File]::WriteAllText($Marker, "preserve-across-upgrade-and-uninstall", [Text.UTF8Encoding]::new($false))

Invoke-Checked $Setup $InstallArgs "installer upgrade"
if (-not (Test-Path -LiteralPath $Marker)) { throw "upgrade removed app data" }

$Uninstaller = Join-Path $Install "Uninstall.exe"
if (-not (Test-Path -LiteralPath $Uninstaller)) { throw "uninstaller is missing" }
Invoke-Checked $Uninstaller @("/S") "uninstaller"

if (Test-Path -LiteralPath $InstalledExe) { throw "uninstall left MixMill.exe behind" }
if (-not (Test-Path -LiteralPath $Marker)) { throw "uninstall removed app data" }
if (-not (Test-Path -LiteralPath (Join-Path $AppData "data\mixmill.db"))) {
    throw "uninstall removed database"
}
$AfterHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Sentinel).Hash
$AfterWrite = (Get-Item -LiteralPath $Sentinel).LastWriteTimeUtc
if ($AfterHash -ne $BeforeHash -or $AfterWrite -ne $BeforeWrite) {
    throw "installer lifecycle changed media original"
}

Write-Output "INSTALLER SMOKE PASS"
Write-Output "Preserved test app data: $AppData"
