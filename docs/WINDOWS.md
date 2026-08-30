# MixMill Desktop 1.0

MixMill Desktop packages the Python backend and web UI with PyInstaller and
pywebview. It runs as a normal window; target PCs need no terminal, Python,
Docker, or Git.

## Install and use

1. Download the installer or portable ZIP from the project's GitHub Releases
   page. Avoid mirrors and reposts.
2. Verify the files against `SHA256SUMS.txt`.
3. Run `MixMill-1.0.0-Windows-x64-Setup.exe`. Because the community build is
   currently unsigned, Windows may show a SmartScreen warning. Confirm the
   filename and checksum before choosing **More info → Run anyway**.
4. On first launch, choose the folder containing release videos.

The installer is per-user and needs no administrator access. The portable ZIP
is an alternative. Supported targets are x64 Windows 10 version 1809 or newer
and Windows 11.

MixMill only reads the chosen media tree. Settings, database, caches, backups,
logs, and generated exports live under `%LOCALAPPDATA%\MixMill`. Upgrade,
uninstall, or portable-app replacement preserves that folder. Downloads use a
native Windows **Save As** dialog, initially in Downloads. Completed exports
remain cached under `%LOCALAPPDATA%\MixMill\data\exports` until deleted in
MixMill.

The backend binds an OS-assigned port on `127.0.0.1` only. Each launch uses a
random HttpOnly SameSite session cookie. Mutations require an integrity header;
non-loopback clients are rejected; session attempts are rate limited.

## Build an unsigned community release

From PowerShell on 64-bit Windows with Python 3.11 or newer:

```powershell
.\tools\build_windows.ps1
```

The build pins and verifies FFmpeg, matching FFmpeg source, Microsoft's WebView2
bootstrapper, NSIS 3.12, and Python dependencies. It compiles Python, runs source
smoke tests, builds the package, launches its smoke test, then installs,
launches, upgrades, and uninstalls the NSIS package from an isolated test area.

Useful options:

- `-Python C:\path\to\python.exe`
- `-SkipInstall` after pinned Python build dependencies are installed

## Optional signed release

Signing is not required to publish the open-source community build. If a trusted
Authenticode certificate is available later, the existing optional path signs
both executables, adds an RFC3161 SHA-256 timestamp, and verifies the signatures:

```powershell
.\tools\build_windows.ps1 `
  -Release `
  -CertificateThumbprint "YOUR_CERTIFICATE_SHA1_THUMBPRINT"
```

Optional signing parameters are `-SignTool` and `-TimestampUrl`.

## Publish set

For version `1.0.0`, publish these five files together:

- `MixMill-1.0.0-Windows-x64-Setup.exe`
- `MixMill-1.0.0-Windows-x64-portable.zip`
- `MixMill-1.0.0-FFmpeg-bf1b838f2a-source.zip`
- `MixMill-1.0.0-release.json`
- `SHA256SUMS.txt`

Also publish the repository's `v1.0.0` source tag and keep `LICENSE`, privacy,
support, security, third-party notices, and release notes visible from the
release page. The manifest truthfully records `signed_release: false` for an
unsigned community release. Never describe an unsigned file as signed.

The download page must identify the Windows files as unsigned, explain the
possible SmartScreen warning, mention FFmpeg, and link its corresponding-source
ZIP. PDF previews use bundled PDFium through `pypdfium2`; Poppler is not needed.

## Verification

```powershell
.\.build\venv\Scripts\python.exe tests\desktop_smoke.py
$env:MIXMILL_FFMPEG = (Resolve-Path '.build\vendor\ffmpeg\bin\ffmpeg.exe').Path
$env:MIXMILL_FFPROBE = (Resolve-Path '.build\vendor\ffmpeg\bin\ffprobe.exe').Path
.\.build\venv\Scripts\python.exe tests\smoke.py
.\tests\installer_smoke.ps1 -Setup artifacts\MixMill-1.0.0-Windows-x64-Setup.exe
```

Before announcing broadly, test the hosted downloads on clean Windows 10 and 11
machines, including one without WebView2. Install, create both mix types, export,
download, upgrade, and uninstall. Confirm no media originals change and no
terminal windows flash.

## GitHub release checklist

- Make the repository public and add a `v1.0.0` tag from the exact tested commit.
- Attach the complete five-file publish set to that tag's GitHub Release.
- Put the unsigned/SmartScreen disclosure near the download links.
- Recompute every hosted file's SHA-256 and compare with `SHA256SUMS.txt`.
- Link `PRIVACY.md`, `SUPPORT.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, and
  the FFmpeg source archive.
- Add a donation link only after the real account URL exists; do not make
  donations a condition of use or support.

MixMill does not supply workout media. Users and distributors are responsible
for rights to their own media. This is engineering guidance, not legal advice.
