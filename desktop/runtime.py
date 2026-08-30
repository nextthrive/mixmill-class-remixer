"""Testable runtime plumbing for the Windows desktop shell."""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "MixMill"
SETTINGS_VERSION = 1
MAX_SETTINGS_BYTES = 64 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024


def local_app_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA", "").strip()
    if not base:
        raise RuntimeError("Windows LOCALAPPDATA is unavailable")
    return Path(base).expanduser().resolve() / APP_NAME


def data_dir(app_root: Path) -> Path:
    return app_root / "data"


def settings_path(app_root: Path) -> Path:
    return app_root / "settings.json"


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def paths_overlap(first: Path, second: Path) -> bool:
    a, b = first.resolve(), second.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def validate_media_dir(value: str | os.PathLike[str], app_root: Path) -> Path:
    media = Path(value).expanduser()
    if not media.is_absolute():
        raise ValueError("Choose an absolute media folder")
    if is_link_like(media):
        raise ValueError("Media folder itself cannot be a shortcut, junction, or symlink")
    media = media.resolve()
    if not media.is_dir():
        raise ValueError("Selected media folder does not exist")
    if paths_overlap(media, app_root):
        raise ValueError("Media folder must be separate from MixMill app data")
    try:
        with os.scandir(media) as entries:
            next(entries, None)
    except OSError as exc:
        raise ValueError(f"Media folder cannot be read: {exc}") from exc
    return media


def read_media_setting(app_root: Path) -> str | None:
    path = settings_path(app_root)
    if not path.is_file() or is_link_like(path):
        return None
    try:
        if path.stat().st_size > MAX_SETTINGS_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != SETTINGS_VERSION:
            return None
        value = payload["media_dir"]
        return value if isinstance(value, str) and value.strip() else None
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def load_media_dir(app_root: Path) -> Path | None:
    value = read_media_setting(app_root)
    if value is None:
        return None
    try:
        return validate_media_dir(value, app_root)
    except ValueError:
        return None


def save_media_dir(app_root: Path, media: Path) -> None:
    media = validate_media_dir(media, app_root)
    app_root.mkdir(parents=True, exist_ok=True)
    target = settings_path(app_root)
    if is_link_like(target):
        raise RuntimeError("MixMill settings file cannot be a link or junction")
    temporary = app_root / "settings.json.tmp"
    if is_link_like(temporary):
        raise RuntimeError("MixMill temporary settings file cannot be a link or junction")
    temporary.write_text(
        json.dumps(
            {"version": SETTINGS_VERSION, "media_dir": str(media)},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[1]


def ensure_app_storage(app_root: Path) -> None:
    if app_root.exists() and is_link_like(app_root):
        raise RuntimeError("MixMill app-data folder cannot be a link or junction")
    app_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(app_root).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            "MixMill needs at least 256 MB free in LOCALAPPDATA to start"
        )
    probe = app_root / f".write-test-{os.getpid()}"
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"MixMill")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise RuntimeError(f"MixMill app-data folder is not writable: {exc}") from exc
    finally:
        probe.unlink(missing_ok=True)


def ensure_database_healthy(app_root: Path) -> None:
    database = data_dir(app_root) / "mixmill.db"
    if not database.exists():
        return
    if is_link_like(database):
        raise RuntimeError("MixMill database cannot be a link or junction")
    connection = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            "MixMill database is damaged. Keep it for recovery and restore a "
            f"backup from {data_dir(app_root) / 'backups'}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(
            "MixMill database failed its safety check. Keep it for recovery and "
            f"restore a backup from {data_dir(app_root) / 'backups'}"
        )


def configure_environment(media: Path, app_root: Path, token: str) -> None:
    if len(token) < 32:
        raise ValueError("Desktop token must be at least 32 characters")
    ensure_app_storage(app_root)
    root = app_root.resolve()
    media = validate_media_dir(media, root)
    state = data_dir(root)
    if paths_overlap(media, state):
        raise ValueError("Media and app-data folders overlap")
    state.mkdir(parents=True, exist_ok=True)
    ensure_database_healthy(root)
    os.environ.update({
        "VIDEO_DIR": str(media.resolve()),
        "DATA_DIR": str(state),
        "MIXMILL_DESKTOP_TOKEN": token,
        "MIXMILL_ALLOW_INSECURE": "0",
        # Desktop cannot enforce a read-only Windows mount. Backend source-path
        # validation and the absence of source write operations are the guard.
        "MIXMILL_REQUIRE_READ_ONLY": "0",
    })
    tools = bundle_root() / "media-tools"
    ffmpeg = tools / "ffmpeg.exe"
    ffprobe = tools / "ffprobe.exe"
    if getattr(sys, "frozen", False) and not (ffmpeg.is_file() and ffprobe.is_file()):
        raise RuntimeError("Bundled ffmpeg tools are missing")
    if tools.is_dir():
        os.environ["MIXMILL_FFMPEG"] = str(ffmpeg)
        os.environ["MIXMILL_FFPROBE"] = str(ffprobe)
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")


def bootstrap_url(port: int, token: str) -> str:
    query = urllib.parse.urlencode({"token": token})
    return f"http://127.0.0.1:{port}/desktop/session?{query}"


@dataclass
class DesktopBackend:
    server: object
    thread: threading.Thread
    listener: socket.socket
    port: int

    def stop(self) -> None:
        setattr(self.server, "should_exit", True)
        self.thread.join(timeout=10)
        try:
            self.listener.close()
        except OSError:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)


def start_backend(timeout: float = 20.0) -> DesktopBackend:
    import uvicorn
    from app.main import app

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False, log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]},
        name="mixmill-loopback", daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + timeout
    health = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        if not thread.is_alive():
            listener.close()
            raise RuntimeError("MixMill backend stopped during startup")
        try:
            with urllib.request.urlopen(health, timeout=1) as response:
                if response.status == 200:
                    return DesktopBackend(server, thread, listener, port)
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    setattr(server, "should_exit", True)
    listener.close()
    thread.join(timeout=2)
    raise RuntimeError("MixMill backend did not become ready")


def smoke_desktop_session(port: int, token: str) -> None:
    root = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(root + "/", timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    else:
        raise AssertionError("desktop root accepted request without session")

    try:
        urllib.request.urlopen(bootstrap_url(port, "x" * 48), timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    else:
        raise AssertionError("desktop bootstrap accepted wrong token")

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    with opener.open(bootstrap_url(port, token), timeout=10) as response:
        html = response.read()
        if response.status != 200 or b"MixMill" not in html:
            raise AssertionError("desktop session did not reach MixMill UI")
    missing_header = urllib.request.Request(root + "/api/scan", method="POST")
    try:
        opener.open(missing_header, timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
    else:
        raise AssertionError("desktop mutation accepted missing integrity header")
    request = urllib.request.Request(
        root + "/api/scan", method="POST",
        headers={"X-MixMill-Request": "1", "Content-Length": "0"},
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise AssertionError("authenticated desktop scan failed")
