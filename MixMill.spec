from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPEC).resolve().parent
FFMPEG = ROOT / ".build" / "vendor" / "ffmpeg"
for required in (FFMPEG / "bin" / "ffmpeg.exe", FFMPEG / "bin" / "ffprobe.exe"):
    if not required.is_file():
        raise SystemExit(f"Missing {required}; run tools/fetch_ffmpeg_windows.py first")

pdfium_datas, pdfium_binaries, pdfium_hidden = collect_all("pypdfium2")

datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(FFMPEG / "LICENSE-FFMPEG.txt"), "media-tools"),
    (str(FFMPEG / "README-FFMPEG.txt"), "media-tools"),
] + pdfium_datas

binaries = [
    (str(FFMPEG / "bin" / "ffmpeg.exe"), "media-tools"),
    (str(FFMPEG / "bin" / "ffprobe.exe"), "media-tools"),
] + pdfium_binaries

a = Analysis(
    [str(ROOT / "desktop_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=pdfium_hidden + [
        "app.main",
        "webview.platforms.edgechromium",
        "webview.platforms.win32",
        "webview.platforms.winforms",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MixMill",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / ".build" / "MixMill.ico"),
    version=str(ROOT / ".build" / "MixMill-version.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MixMill",
)
