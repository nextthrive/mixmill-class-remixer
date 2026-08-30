"""Fetch and verify pinned 64-bit FFmpeg tools for the Windows bundle."""
from __future__ import annotations

import hashlib
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


VERSION = "9.0.1"
URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.1-essentials_build.zip"
FALLBACK_URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip"
SHA256 = "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9"
SOURCE_COMMIT = "bf1b838f2a"
SOURCE_URL = f"https://github.com/FFmpeg/FFmpeg/archive/{SOURCE_COMMIT}.zip"
SOURCE_SHA256 = "a9440bcc594a24ed24b1fffde8536e415eed65d4eb6997cc7c413505058696f3"
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
ARCHIVE = BUILD / f"ffmpeg-{VERSION}-essentials.zip"
SOURCE_ARCHIVE = BUILD / f"ffmpeg-{SOURCE_COMMIT}-source.zip"
TARGET = BUILD / "vendor" / "ffmpeg"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_verified(archive: Path, sha256: str, urls: tuple[str, ...], label: str) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    if archive.is_file() and file_hash(archive) == sha256:
        return
    temporary = archive.with_suffix(".download")
    temporary.unlink(missing_ok=True)
    print(f"Downloading {label}...")
    last_error = None
    for url in urls:
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "MixMill-Windows-Builder/0.1"}
                )
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as out:
                    shutil.copyfileobj(response, out, length=1024 * 1024)
                last_error = None
                break
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
                time.sleep(attempt + 1)
        if last_error is None:
            break
    if last_error is not None:
        raise RuntimeError(f"Could not download {label}: {last_error}") from last_error
    if file_hash(temporary) != sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{label} archive checksum mismatch")
    temporary.replace(archive)


def download() -> None:
    download_verified(ARCHIVE, SHA256, (URL, FALLBACK_URL), f"FFmpeg {VERSION}")
    download_verified(
        SOURCE_ARCHIVE,
        SOURCE_SHA256,
        (SOURCE_URL,),
        f"FFmpeg source {SOURCE_COMMIT}",
    )


def extract_member(archive: zipfile.ZipFile, suffix: str, destination: Path) -> None:
    matches = [name for name in archive.namelist() if name.replace("\\", "/").endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one FFmpeg archive member ending in {suffix}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(matches[0]) as source, destination.open("wb") as output:
        shutil.copyfileobj(source, output)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows build tools can only be fetched on Windows")
    download()
    with zipfile.ZipFile(ARCHIVE) as archive:
        extract_member(archive, "/bin/ffmpeg.exe", TARGET / "bin" / "ffmpeg.exe")
        extract_member(archive, "/bin/ffprobe.exe", TARGET / "bin" / "ffprobe.exe")
        extract_member(archive, "/LICENSE", TARGET / "LICENSE-FFMPEG.txt")
        extract_member(archive, "/README.txt", TARGET / "README-FFMPEG.txt")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        path = TARGET / "bin" / name
        if not path.is_file() or path.stat().st_size < 1_000_000:
            raise RuntimeError(f"Extracted {name} is invalid")
    print(f"FFmpeg tools ready: {TARGET}")
    print(f"FFmpeg corresponding source ready: {SOURCE_ARCHIVE}")


if __name__ == "__main__":
    main()
