# Third-party notices

MixMill Desktop includes third-party software. This notice is informational,
not legal advice; the corresponding license texts control.

## FFmpeg

This product uses the `ffmpeg` and `ffprobe` programs from the
[FFmpeg project](https://ffmpeg.org/) under GNU GPL version 3. The Windows
binary is the Gyan.dev FFmpeg 9.0.1 essentials build, compiled from FFmpeg
commit `bf1b838f2a` with GPL components enabled.

The complete corresponding FFmpeg source archive
`MixMill-<version>-FFmpeg-bf1b838f2a-source.zip` must be published beside every
MixMill Desktop binary download. The archive, binary build README/configuration,
GPL text, and all public artifacts are covered by `SHA256SUMS.txt`. The installed
application also includes `media-tools\README-FFMPEG.txt` and
`media-tools\LICENSE-FFMPEG.txt`.

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.
MixMill is not affiliated with or endorsed by the FFmpeg project.

## Python packages

- FastAPI — MIT License
- Uvicorn — BSD 3-Clause License
- Starlette — BSD 3-Clause License
- Pydantic and pydantic-core — MIT License
- NumPy — BSD 3-Clause License; its distribution includes notices for bundled
  components
- pypdf — BSD 3-Clause License
- ReportLab — BSD License
- pywebview — BSD 3-Clause License
- pypdfium2 — BSD 3-Clause License; PDFium and bundled components retain their
  own notices
- PyInstaller — GNU GPL version 2 or later with its exception permitting
  distribution of bundled applications
- AnyIO, Click, h11, idna, typing-extensions, typing-inspection,
  annotated-types, and annotated-doc — licenses supplied by their respective
  projects

Package versions are pinned in `requirements-windows.txt` in the source tree.

## Other components

- Anton font — SIL Open Font License 1.1. The license is installed at
  `app\static\fonts\OFL-Anton.txt`.
- Microsoft Edge WebView2 Bootstrapper — Microsoft software license terms. It
  is Microsoft-signed and runs only when the WebView2 Runtime is missing.
- NSIS 3.12 — used to create the Windows installer under the zlib/libpng
  license. NSIS is free for any use. Its LZMA compression module is covered by
  the Common Public License 1.0 with the NSIS exception permitting use in an
  installer.

Project pages:

- FFmpeg: https://ffmpeg.org/legal.html
- Gyan.dev builds: https://www.gyan.dev/ffmpeg/builds/
- Python packages: https://pypi.org/
- Microsoft Edge WebView2: https://developer.microsoft.com/microsoft-edge/webview2/
- NSIS license: https://nsis.sourceforge.io/Docs/AppendixI.html
