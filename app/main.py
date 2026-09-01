"""MixMill — self-hosted workout mix builder for Les Mills-style releases.

Scan a folder of release videos, mark the tracks (songs) in each one,
build mixes out of your favourite tracks, play them back-to-back in the
browser, or export a single mp4 with ffmpeg.
"""
import hashlib
import json
import os
import queue
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware

try:
    import pypdfium2 as pdfium
except ImportError:  # Docker keeps using its existing pdftoppm package.
    pdfium = None


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _run_subprocess(*args, **kwargs):
    """Run media tools without flashing console windows in desktop builds."""
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(*args, **kwargs)


FFMPEG_COMMAND = (
    os.environ.get("MIXMILL_FFMPEG", "").strip()
    or shutil.which("ffmpeg")
    or "ffmpeg"
)
FFPROBE_COMMAND = (
    os.environ.get("MIXMILL_FFPROBE", "").strip()
    or shutil.which("ffprobe")
    or "ffprobe"
)


AUTH_USER = os.environ.get("MIXMILL_USERNAME", "")
AUTH_PASSWORD = os.environ.get("MIXMILL_PASSWORD", "")
DESKTOP_TOKEN = os.environ.get("MIXMILL_DESKTOP_TOKEN", "")
ALLOW_INSECURE = env_bool("MIXMILL_ALLOW_INSECURE", False)
if DESKTOP_TOKEN and len(DESKTOP_TOKEN) < 32:
    raise RuntimeError("MIXMILL_DESKTOP_TOKEN must be at least 32 characters")
if not ALLOW_INSECURE and not DESKTOP_TOKEN and (not AUTH_USER or not AUTH_PASSWORD):
    raise RuntimeError(
        "MIXMILL_USERNAME and MIXMILL_PASSWORD are required; set "
        "MIXMILL_ALLOW_INSECURE=1 only for isolated development"
    )
if AUTH_USER and len(AUTH_USER) > 128:
    raise RuntimeError("MIXMILL_USERNAME must be at most 128 characters")

_video_input = Path(os.environ.get("VIDEO_DIR", "/videos")).expanduser()
_data_input = Path(os.environ.get("DATA_DIR", "/data")).expanduser()
if _video_input.is_symlink() or _data_input.is_symlink():
    raise RuntimeError("VIDEO_DIR and DATA_DIR themselves may not be symlinks")
VIDEO_DIR = _video_input.resolve()
DATA_DIR = _data_input.resolve()
if (VIDEO_DIR == DATA_DIR or VIDEO_DIR.is_relative_to(DATA_DIR)
        or DATA_DIR.is_relative_to(VIDEO_DIR)):
    raise RuntimeError("VIDEO_DIR and DATA_DIR must be separate, non-overlapping paths")

if not VIDEO_DIR.is_dir():
    raise RuntimeError(f"video directory {VIDEO_DIR} does not exist")
if env_bool("MIXMILL_REQUIRE_READ_ONLY", True):
    if not hasattr(os, "statvfs") or not (os.statvfs(VIDEO_DIR).f_flag & os.ST_RDONLY):
        raise RuntimeError(
            f"video directory {VIDEO_DIR} is not mounted read-only; use a Docker :ro "
            "bind mount or set MIXMILL_REQUIRE_READ_ONLY=0 only for isolated development"
        )

DATA_DIR.mkdir(parents=True, exist_ok=True)
_managed_root_names = {
    ".mixmill-data", "mixmill.db", "mixmill.db-shm", "mixmill.db-wal",
    "exports", "audiocache", "backups", "tmp", "thumbcache",
}
_unknown_data = sorted(p.name for p in DATA_DIR.iterdir()
                       if p.name not in _managed_root_names)
if _unknown_data:
    raise RuntimeError(
        "DATA_DIR must be dedicated to MixMill; refusing unrelated entries: "
        + ", ".join(_unknown_data[:10])
    )
_marker = DATA_DIR / ".mixmill-data"
if _marker.is_symlink():
    raise RuntimeError("the MixMill data marker may not be a symlink")
_marker.write_text("MixMill managed data v1\n", encoding="utf-8")


def managed_dir(name: str) -> Path:
    path = DATA_DIR / name
    if path.is_symlink():
        raise RuntimeError(f"managed data directory {name} may not be a symlink")
    path.mkdir(parents=False, exist_ok=True)
    resolved = path.resolve()
    if resolved.parent != DATA_DIR:
        raise RuntimeError(f"managed data directory {name} escaped DATA_DIR")
    return resolved


EXPORT_DIR = managed_dir("exports")
DB_PATH = DATA_DIR / "mixmill.db"
if DB_PATH.is_symlink():
    raise RuntimeError("mixmill.db may not be a symlink")
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".ts", ".webm"}
MIME_BY_EXT = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".avi": "video/x-msvideo", ".ts": "video/mp2t",
}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma"}
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".wma": "audio/x-ms-wma",
}

AUDIO_CACHE = managed_dir("audiocache")
BACKUP_DIR = managed_dir("backups")
TMP_DIR = managed_dir("tmp")
THUMB_CACHE = managed_dir("thumbcache")
THUMBNAIL_SLOTS = threading.BoundedSemaphore(2)
CHOREOGRAPHY_PREVIEW_SLOTS = threading.BoundedSemaphore(2)

MAX_BODY_BYTES = int(os.environ.get("MIXMILL_MAX_BODY_BYTES", str(1024 * 1024)))
MEDIA_TIMEOUT = int(os.environ.get("MIXMILL_MEDIA_TIMEOUT", "3600"))
MAX_CHOREOGRAPHY_PDF_BYTES = int(
    os.environ.get("MIXMILL_MAX_CHOREOGRAPHY_PDF_BYTES", str(256 * 1024 * 1024))
)
if not 4096 <= MAX_BODY_BYTES <= 16 * 1024 * 1024:
    raise RuntimeError("MIXMILL_MAX_BODY_BYTES must be between 4096 and 16777216")
if not 60 <= MEDIA_TIMEOUT <= 86400:
    raise RuntimeError("MIXMILL_MEDIA_TIMEOUT must be between 60 and 86400 seconds")
if not 1024 * 1024 <= MAX_CHOREOGRAPHY_PDF_BYTES <= 1024 * 1024 * 1024:
    raise RuntimeError(
        "MIXMILL_MAX_CHOREOGRAPHY_PDF_BYTES must be between 1048576 and 1073741824"
    )

class SelectiveGZipMiddleware:
    """Compress text/JSON without touching seekable media responses."""

    _media_api = re.compile(
        r"^/api/(?:stream/|releases/\d+/(?:thumb|audio|music/\d+|"
        r"choreography-notes/(?:source|pages/\d+))$|"
        r"mixes/\d+/(?:songs\.zip|package\.zip|choreography-notes)$|"
        r"exports/[^/]+/download$)"
    )

    def __init__(self, app, minimum_size: int = 500, compresslevel: int = 5):
        self.app = app
        self.compressed_app = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        compress = path in {"/", "/index.html"} or path.startswith("/fonts/") or (
            path.startswith("/api/") and not self._media_api.match(path)
        )
        target = self.compressed_app if compress else self.app
        return await target(scope, receive, send)


app = FastAPI(title="MixMill")
app.add_middleware(SelectiveGZipMiddleware, minimum_size=500, compresslevel=5)

_RATE_LOCK = threading.Lock()
_RATE_EVENTS: dict[str, deque[float]] = defaultdict(deque)
_EXPENSIVE_POST_PATHS = {
    "/api/scan", "/api/extract-audio-all", "/api/auto-tracks-all",
}
_EXPENSIVE_RELEASE_POST = re.compile(
    r"^/api/releases/\d+/(?:auto-tracks|extract-audio)$"
)
_EXPENSIVE_MIX_EXPORT = re.compile(r"^/api/mixes/\d+/export$")
_EXPENSIVE_RELEASE_AUDIO = re.compile(r"^/api/releases/\d+/audio$")
_EXPENSIVE_SONG_ZIP = re.compile(r"^/api/mixes/\d+/songs\.zip$")
_EXPENSIVE_PACKAGE_ZIP = re.compile(r"^/api/mixes/\d+/package\.zip$")
_EXPENSIVE_CHOREOGRAPHY_PDF = re.compile(r"^/api/mixes/\d+/choreography-notes$")
_EXPENSIVE_CHOREOGRAPHY_PAGE = re.compile(
    r"^/api/releases/\d+/choreography-notes/pages/\d+$"
)


def _is_expensive_request(method: str, path: str) -> bool:
    """Rate-limit work starters without charging lightweight status polling."""
    if method == "POST":
        return (path in _EXPENSIVE_POST_PATHS
                or bool(_EXPENSIVE_RELEASE_POST.fullmatch(path))
                or bool(_EXPENSIVE_MIX_EXPORT.fullmatch(path)))
    if method == "GET":
        return (bool(_EXPENSIVE_RELEASE_AUDIO.fullmatch(path))
                or bool(_EXPENSIVE_SONG_ZIP.fullmatch(path))
                or bool(_EXPENSIVE_PACKAGE_ZIP.fullmatch(path))
                or bool(_EXPENSIVE_CHOREOGRAPHY_PDF.fullmatch(path))
                or bool(_EXPENSIVE_CHOREOGRAPHY_PAGE.fullmatch(path)))
    return False


def _rate_allowed(key: str, limit: int, window: float = 60.0) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        events = _RATE_EVENTS[key]
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Routing and security decisions must use the ASGI path, not a URL rebuilt
    # from attacker-controlled Host data.
    path = request.scope.get("path", "")
    client = request.client.host if request.client else "unknown"
    if DESKTOP_TOKEN and client != "127.0.0.1":
        return Response("Desktop server accepts loopback clients only", status_code=403)
    if path != "/api/health" and DESKTOP_TOKEN:
        # Desktop shell enters once through /desktop/session. That endpoint turns
        # the launch secret into an HttpOnly, same-site session cookie, then
        # removes the secret from the address bar with a redirect.
        if path == "/desktop/session":
            if not _rate_allowed(f"desktop-session:{client}", 30):
                return Response("Too many desktop session attempts", status_code=429)
        else:
            candidate = request.cookies.get("mixmill_desktop_session", "")
            if not secrets.compare_digest(candidate, DESKTOP_TOKEN):
                return Response("Desktop session required", status_code=401)
    elif path != "/api/health" and not ALLOW_INSECURE:
        authorization = request.headers.get("authorization", "")
        valid = False
        if authorization.startswith("Basic "):
            try:
                import base64
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                username, password = decoded.split(":", 1)
                valid = (secrets.compare_digest(username, AUTH_USER)
                         and secrets.compare_digest(password, AUTH_PASSWORD))
            except (ValueError, UnicodeError):
                valid = False
        if not valid:
            if not _rate_allowed(f"auth:{client}", 30):
                return Response("Too many authentication attempts", status_code=429)
            return Response(
                "Authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="MixMill", charset="UTF-8"'},
            )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("x-mixmill-request") != "1":
            return Response("Missing request-integrity header", status_code=403)
        transfer = request.headers.get("transfer-encoding", "").lower()
        if transfer and transfer != "identity":
            return Response("Chunked request bodies are not accepted", status_code=413)
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_BODY_BYTES:
                return Response("Request body too large", status_code=413)
        except ValueError:
            return Response("Invalid Content-Length", status_code=400)
    if _is_expensive_request(request.method, path):
        if not _rate_allowed(f"expensive:{client}", 60):
            return Response("Too many expensive requests", status_code=429)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; "
        "style-src 'self' 'unsafe-inline'; font-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    if re.fullmatch(r"/api/releases/\d+/thumb", path):
        response.headers["Cache-Control"] = "private, max-age=3600"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/fonts/") or re.fullmatch(
            r"/(?:icon(?:-\d+)?\.(?:png|svg)|apple-touch-icon\.png)", path):
        response.headers["Cache-Control"] = "private, max-age=604800"
    elif path == "/manifest.webmanifest":
        response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


@app.get("/desktop/session", include_in_schema=False)
def desktop_session(token: str = Query(default="", max_length=256)):
    if not DESKTOP_TOKEN:
        raise HTTPException(404, "desktop session unavailable")
    if not secrets.compare_digest(token, DESKTOP_TOKEN):
        raise HTTPException(401, "invalid desktop session")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "mixmill_desktop_session", DESKTOP_TOKEN,
        httponly=True, secure=False, samesite="strict", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response

# ---------------------------------------------------------------- database

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS releases (
                id INTEGER PRIMARY KEY,
                relpath TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                program TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                missing INTEGER DEFAULT 0,
                vaulted INTEGER DEFAULT 0,
                curated INTEGER DEFAULT 0,
                added_at REAL
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY,
                release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                position INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS mixes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS mix_items (
                id INTEGER PRIMARY KEY,
                mix_id INTEGER NOT NULL REFERENCES mixes(id) ON DELETE CASCADE,
                track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                position INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY,
                mix_id INTEGER,
                mix_name TEXT,
                mode TEXT,
                status TEXT,
                progress REAL DEFAULT 0,
                filename TEXT,
                error TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS choreography_mappings (
                track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK(mode IN ('manual', 'disabled')),
                page_start INTEGER,
                page_end INTEGER,
                source_fingerprint TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            """
        )


def backup_db():
    """Snapshot the DB with sqlite's online backup API; prune to 10. Never
    raises — a failed backup must not block startup or the daily timer."""
    if not DB_PATH.exists():
        return
    try:
        dest = BACKUP_DIR / f"mixmill-{time.strftime('%Y%m%d-%H%M%S')}.db"
        src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
        for old in sorted(BACKUP_DIR.glob("mixmill-*.db"))[:-10]:
            old.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"db backup failed: {exc}")


backup_db()  # before init_db/migrations touch the file
init_db()

with db() as _conn:
    # older databases predate these columns
    try:
        _conn.execute("ALTER TABLE releases ADD COLUMN curated INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    else:
        # Before Curated existed, having saved tracks was the app's definition
        # of reviewed. Preserve that pool during migration.
        _conn.execute(
            """UPDATE releases SET curated=1 WHERE EXISTS (
                   SELECT 1 FROM tracks WHERE tracks.release_id=releases.id
               )"""
        )
    try:
        _conn.execute("ALTER TABLE tracks ADD COLUMN rejected INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for _stmt in ("ALTER TABLE mixes ADD COLUMN audio INTEGER DEFAULT 0",
                  "ALTER TABLE mix_items ADD COLUMN music_idx INTEGER",
                  "ALTER TABLE releases ADD COLUMN vaulted INTEGER DEFAULT 0",
                  "ALTER TABLE releases ADD COLUMN added_at REAL"):
        try:
            _conn.execute(_stmt)
        except sqlite3.OperationalError:
            pass  # column already there
    # rows from before added_at existed: stamp them so "newest" sort works
    _conn.execute("UPDATE releases SET added_at=? WHERE added_at IS NULL",
                  (time.time(),))

with db() as _conn:
    # read the running ids BEFORE the UPDATE below rewrites them: they are the
    # ones this sweep must not touch, and afterwards nothing says 'running' any
    # more. Each is either a crashed run of ours or an export a second process
    # is writing right now (uvicorn --workers N, a gunicorn respawn, a --reload
    # child starting while the old one drains) — indistinguishable from here.
    _live = {r["id"] for r in _conn.execute(
        "SELECT id FROM exports WHERE status IN ('running','queued')")}
    _conn.execute(
        "UPDATE exports SET status='error', error='interrupted by server restart' "
        "WHERE status IN ('running','queued')"
    )
# The segments of an export killed mid-flight are invisible to the UI. Sweep
# only the exact UUID-shaped scratch names MixMill creates; unknown entries are
# left alone and a symlink is unlinked rather than followed.
for _tmp in EXPORT_DIR.iterdir():
    _match = re.fullmatch(r"tmp_([0-9a-f]{32})", _tmp.name)
    if _match and _match.group(1) not in _live:
        if _tmp.is_symlink() or _tmp.is_file():
            _tmp.unlink(missing_ok=True)
        elif _tmp.is_dir():
            shutil.rmtree(_tmp, ignore_errors=True)

# Transient song ZIPs whose delete-after-send callback never ran. Unknown files
# are not ours and are deliberately left untouched.
for _tmp in TMP_DIR.iterdir():
    if (re.fullmatch(r"songs_[0-9a-f]{32}\.zip", _tmp.name)
            or re.fullmatch(r"package_[0-9a-f]{32}\.zip", _tmp.name)
            or re.fullmatch(r"choreo_[0-9a-f]{32}\.pdf", _tmp.name)):
        _tmp.unlink(missing_ok=True)


def _backup_loop():
    while True:
        time.sleep(86400)
        backup_db()


threading.Thread(target=_backup_loop, daemon=True).start()

# ---------------------------------------------------------------- helpers

def ffprobe_json(path: Path, *extra: str) -> dict:
    cmd = [FFPROBE_COMMAND, "-v", "quiet", "-protocol_whitelist", "file,pipe",
           "-print_format", "json", *extra, str(path)]
    try:
        out = _run_subprocess(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        # Scanning must remain useful when a file is malformed, ffprobe stalls,
        # or the media tools are temporarily unavailable. Callers already treat
        # an empty probe as unknown metadata.
        return {}
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def probe_duration(path: Path) -> float:
    info = ffprobe_json(path, "-show_format")
    try:
        return float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        return 0.0


def audio_stream_count(path: Path) -> int:
    info = ffprobe_json(path, "-show_streams", "-select_streams", "a")
    return len(info.get("streams", []))


def keyframe_before(path: Path, t: float) -> float:
    """Last video keyframe at or before t. t and the return value are
    start_time-relative (the timeline ffmpeg's -ss uses); ffprobe reports
    absolute PTS, so shift into that timeline and back. Any probe failure
    falls back to t, which reproduces the old (start-snapped) behavior."""
    try:
        try:
            off = float(ffprobe_json(path, "-show_entries", "format=start_time")
                        .get("format", {}).get("start_time") or 0.0)
        except (TypeError, ValueError):
            off = 0.0
        abs_t = t + off
        win_start = max(0.0, abs_t - 30)
        info = ffprobe_json(
            path,
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-read_intervals", f"{win_start}%{abs_t + 1}",
        )
        best = None
        for fr in info.get("frames", []):
            try:
                pts = float(fr.get("pts_time"))
            except (TypeError, ValueError):
                continue
            if pts <= abs_t and (best is None or pts > best):
                best = pts
        return best - off if best is not None else t
    except Exception:  # noqa: BLE001
        # A stalled or missing ffprobe must not take the whole export down.
        return t


TRACKNUM_RE = re.compile(
    r"^\s*(?:0*(\d+)([A-Za-z]?)|([A-Za-z])0*(\d+))(?![A-Za-z0-9])"
)


def track_number_key(name: str) -> str | None:
    """Normalize track labels used by media and notes.

    ``03A`` and vendor-style bonus label ``A03`` both become ``3A``.
    """
    m = TRACKNUM_RE.match(name or "")
    if not m:
        return None
    if m.group(1) is not None:
        return str(int(m.group(1))) + m.group(2).upper()
    return str(int(m.group(4))) + m.group(3).upper()


RELNUM_RE = re.compile(r"(\d+)\s*$")


def release_number(title: str) -> int | None:
    """Trailing number of a release title: 'BodyPump 120' -> 120."""
    m = RELNUM_RE.search(title or "")
    return int(m.group(1)) if m else None


def base_slot(name: str) -> int | None:
    """Leading track number without its letter: '03A Dance' -> 3."""
    key = track_number_key(name)
    if key is None:
        return None
    m = re.match(r"\d+", key)
    return int(m.group(0)) if m else None


def natural_key(s: str):
    """Sort key that orders embedded numbers numerically, so 'bc 59' comes
    before 'bc 102'. re.split with a capturing group alternates text/digit
    parts, so two keys always compare str-to-str and int-to-int."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def clean_name(s: str) -> str:
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def infer_title_program(relpath: str) -> tuple[str, str]:
    """Infer (title, program) from a path like
    'BodyPump/BodyPump 120/BODYPUMP120.mp4' -> ('BodyPump 120', 'BodyPump').
    Falls back gracefully for flatter layouts."""
    parts = Path(relpath).parts
    program = clean_name(parts[0]) if len(parts) > 1 else ""
    if len(parts) >= 3:
        title = clean_name(parts[-2])  # release folder name
    else:
        title = clean_name(Path(relpath).stem)
    return title or clean_name(Path(relpath).stem), program


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def release_path(row) -> Path:
    """Resolve a database relative path without permitting symlink/.. escape."""
    relpath = Path(row["relpath"])
    if relpath.is_absolute():
        raise HTTPException(404, "media path is outside the library")
    path = (VIDEO_DIR / relpath).resolve()
    if not is_within(path, VIDEO_DIR):
        raise HTTPException(404, "media path is outside the library")
    return path


def music_files(row) -> list[Path]:
    """Audio files for a release. The music usually sits in a single subfolder
    (any name) of the folder holding the video; when several subfolders exist,
    take the one with the most audio files."""
    folder = release_path(row).parent
    if folder == VIDEO_DIR:
        # flat layout: the subfolders here are program folders, not music
        return []
    skip = {"@eadir", "#recycle"}
    best: list[Path] = []
    try:
        subs = [d for d in folder.iterdir() if d.is_dir() and d.name.lower() not in skip]
    except OSError:
        return []
    for sub in subs:
        files = sorted(
            (f.resolve() for f in sub.rglob("*")
             if f.is_file() and f.suffix.lower() in AUDIO_EXTS
             and is_within(f.resolve(), VIDEO_DIR)),
            key=lambda p: natural_key(p.name),
        )
        if len(files) > len(best):
            best = files
    return best


_PDF_TRACK_HEADING_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:0*(\d{2})([A-Za-z]?)|([A-Za-z])0*(\d{1,2}))\.\s+(?=[A-Z])"
)
_PDF_DURATION_RE = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)\s*mins?\b", re.I)
_CHOREOGRAPHY_INDEX_CACHE: dict[str, tuple[int, int, dict]] = {}
_CHOREOGRAPHY_CACHE_LOCK = threading.Lock()
_PDF_FONT_LOCK = threading.Lock()


def choreography_pdf_for_release(row) -> Path | None:
    """Best choreography-notes PDF inside the release folder.

    Paths are resolved back through VIDEO_DIR so a symlink cannot turn a notes
    download into arbitrary file access. In a flat library, only PDFs whose
    name resembles the release are eligible; otherwise every root-level PDF
    would appear to belong to every release.
    """
    media = release_path(row)
    folder = media.parent
    try:
        raw_candidates = [p for p in folder.glob("*") if p.suffix.lower() == ".pdf"]
        if folder != VIDEO_DIR:
            raw_candidates += [
                p for p in folder.rglob("*") if p.suffix.lower() == ".pdf"
            ]
    except OSError:
        return None

    release_tokens = {
        re.sub(r"[^a-z0-9]+", "", (row["title"] or "").lower()),
        re.sub(r"[^a-z0-9]+", "", media.stem.lower()),
    }
    release_tokens.discard("")
    candidates = []
    seen = set()
    for candidate in raw_candidates:
        try:
            path = candidate.resolve()
            if path in seen or not path.is_file() or not is_within(path, VIDEO_DIR):
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > MAX_CHOREOGRAPHY_PDF_BYTES:
            continue
        seen.add(path)
        compact = re.sub(r"[^a-z0-9]+", "", path.stem.lower())
        lower = path.stem.lower()
        score = 0
        if "choreography" in lower or "choreo" in lower:
            score += 100
        if "notes" in lower:
            score += 30
        if any(token and token in compact for token in release_tokens):
            score += 20
        if path.parent == folder:
            score += 10
        if folder == VIDEO_DIR and score < 20:
            continue
        candidates.append((score, natural_key(path.name), path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def choreography_pdf_index(path: Path) -> dict:
    """Track-number to contiguous source-page groups, cached by file stat."""
    try:
        stat = path.stat()
    except OSError:
        return {"error": "Choreography PDF is unavailable", "groups": {}}
    cache_key = str(path)
    with _CHOREOGRAPHY_CACHE_LOCK:
        cached = _CHOREOGRAPHY_INDEX_CACHE.get(cache_key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
    if stat.st_size > MAX_CHOREOGRAPHY_PDF_BYTES:
        result = {"error": "Choreography PDF is too large", "groups": {}}
    else:
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise ValueError("encrypted PDF")
            if not reader.pages or len(reader.pages) > 2000:
                raise ValueError("unsupported page count")
            page_keys: list[str | None] = []
            page_text: list[str] = []
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    text = ""
                keys = set()
                for match in _PDF_TRACK_HEADING_RE.finditer(text):
                    if match.group(1) is not None:
                        keys.add(str(int(match.group(1))) + match.group(2).upper())
                    else:
                        keys.add(str(int(match.group(4))) + match.group(3).upper())
                page_keys.append(next(iter(keys)) if len(keys) == 1 else None)
                page_text.append(text)
            # A continuation page without its own title belongs to the same
            # track only when both neighbours agree. Never propagate into
            # glossary or declaration pages at the end of a release.
            for i in range(1, len(page_keys) - 1):
                if (page_keys[i] is None and page_keys[i - 1]
                        and page_keys[i - 1] == page_keys[i + 1]):
                    page_keys[i] = page_keys[i - 1]
            groups: dict[str, list[dict]] = defaultdict(list)
            i = 0
            while i < len(page_keys):
                key = page_keys[i]
                if key is None:
                    i += 1
                    continue
                pages = [i]
                i += 1
                while i < len(page_keys) and page_keys[i] == key:
                    pages.append(i)
                    i += 1
                text = "\n".join(page_text[p] for p in pages)
                durations = sorted({
                    int(match.group(1)) * 60 + int(match.group(2))
                    for match in _PDF_DURATION_RE.finditer(text)
                })
                groups[key].append({
                    "pages": pages,
                    "durations": durations,
                    "express": bool(re.search(r"\b(?:EXP|EXPRESS)\b", text, re.I)),
                })
            first = reader.pages[0].mediabox
            page_size = (float(first.width), float(first.height))
            result = {
                "error": None,
                "groups": dict(groups),
                "page_count": len(reader.pages),
                "page_size": page_size,
            }
        except Exception:  # noqa: BLE001
            result = {"error": "Choreography PDF could not be read", "groups": {}}
    with _CHOREOGRAPHY_CACHE_LOCK:
        _CHOREOGRAPHY_INDEX_CACHE[cache_key] = (
            stat.st_mtime_ns, stat.st_size, result
        )
        # Bound memory on very large libraries. Oldest insertion is sufficient;
        # entries repopulate on demand and file-stat invalidation still works.
        while len(_CHOREOGRAPHY_INDEX_CACHE) > 256:
            _CHOREOGRAPHY_INDEX_CACHE.pop(next(iter(_CHOREOGRAPHY_INDEX_CACHE)))
    return result


def choreography_source_fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def choreography_options_for_track(index: dict, track_name: str,
                                    duration: float) -> tuple[str | None, list[dict]]:
    key = track_number_key(track_name)
    if key is None:
        return None, []
    candidates: list[tuple[str, dict]] = []
    groups_by_key = index.get("groups", {})
    exact_groups = groups_by_key.get(key, [])
    base = re.match(r"\d+", key).group(0)
    variants = sorted(
        (
            (candidate, candidate_groups)
            for candidate, candidate_groups in groups_by_key.items()
            if re.match(r"\d+", candidate).group(0) == base
        ),
        key=lambda item: natural_key(item[0]),
    )
    # An exact numbered heading remains the automatic choice, while sibling
    # alternatives (3A/3B, express, etc.) stay available for manual selection.
    # When only ambiguous siblings exist there is intentionally no auto pick.
    candidates = [
        (candidate, group)
        for candidate, candidate_groups in variants
        for group in candidate_groups
    ]
    if not candidates:
        return key, []
    wants_express = bool(re.search(r"\b(?:EXP|EXPRESS)\b", track_name, re.I))

    def group_score(candidate):
        candidate_key, group = candidate
        exact_gap = 0 if candidate_key == key else 1
        duration_gap = min(
            (abs(candidate - duration) for candidate in group["durations"]),
            default=10_000,
        )
        express_gap = 0 if group["express"] == wants_express else 1
        return exact_gap, duration_gap, express_gap

    recommended = (
        min(range(len(candidates)), key=lambda i: group_score(candidates[i]))
        if exact_groups or len(variants) == 1 else None
    )
    options = []
    for i, (candidate_key, group) in enumerate(candidates):
        pages = group["pages"]
        duration_labels = [
            f"{seconds // 60}:{seconds % 60:02d}" for seconds in group["durations"]
        ]
        kind = "Express" if group["express"] else "Full"
        page_label = str(pages[0] + 1) if len(pages) == 1 else f"{pages[0] + 1}-{pages[-1] + 1}"
        details = " / ".join(duration_labels) if duration_labels else kind
        options.append({
            "key": candidate_key,
            "pages": pages,
            "page_start": pages[0] + 1,
            "page_end": pages[-1] + 1,
            "durations": group["durations"],
            "express": group["express"],
            "recommended": i == recommended,
            "label": f"{candidate_key} · pages {page_label} · {details}",
        })
    return key, options


def choreography_pages_for_track(index: dict, track_name: str,
                                  duration: float) -> tuple[str | None, list[int]]:
    key, options = choreography_options_for_track(index, track_name, duration)
    chosen = next((option for option in options if option["recommended"]), None)
    return key, chosen["pages"] if chosen else []


def resolve_choreography_mapping(index: dict, track: dict,
                                 override: dict | None,
                                 fingerprint: str = "") -> dict:
    """Resolve one track. Manual ranges survive source changes and become stale."""
    key, options = choreography_options_for_track(
        index, track["name"], track["end"] - track["start"]
    )
    result = {
        "key": key,
        "matched": False,
        "pages": [],
        "source_pages": [],
        "mapping_mode": "auto",
        "stale": False,
        "reason": "",
        "options": options,
    }
    if override and override["mode"] == "disabled":
        result.update(mapping_mode="disabled", reason="Notes disabled for this track")
        return result
    if override and override["mode"] == "manual":
        start, end = override["page_start"], override["page_end"]
        page_count = index.get("page_count", 0)
        result["mapping_mode"] = "manual"
        result["stale"] = bool(
            override.get("source_fingerprint")
            and override["source_fingerprint"] != fingerprint
        )
        if not start or not end or start < 1 or end < start or end > page_count:
            result["reason"] = "Saved page range is outside the current PDF"
            return result
        result.update(
            matched=True,
            pages=list(range(start - 1, end)),
            source_pages=list(range(start, end + 1)),
        )
        return result
    chosen = next((option for option in options if option["recommended"]), None)
    if chosen:
        result.update(
            matched=True,
            pages=chosen["pages"],
            source_pages=[page + 1 for page in chosen["pages"]],
        )
    else:
        result["reason"] = (
            f"Track {key} was not found in choreography PDF"
            if key else "Track number is missing"
        )
    return result


def choreography_plan(mix: dict, releases: dict[int, dict],
                      overrides: dict[int, dict] | None = None) -> dict:
    """Resolve every mix item to unchanged source pages, preserving mix order."""
    items = []
    sections = []
    section_lookup = {}
    source_cache: dict[int, Path | None] = {}
    page_size = (595.28, 841.89)
    overrides = overrides or {}
    for position, item in enumerate(mix["items"], start=1):
        row = {
            "position": position,
            "item_id": item["item_id"],
            "track_id": item["track_id"],
            "track_name": item["name"],
            "release_title": item["release_title"],
            "matched": False,
            "reason": "",
            "mapping_mode": "auto",
            "stale": False,
            "section_index": None,
        }
        release = releases.get(item["release_id"])
        if not release:
            row["reason"] = "Source release is unavailable"
            items.append(row)
            continue
        release_id = item["release_id"]
        if release_id not in source_cache:
            source_cache[release_id] = choreography_pdf_for_release(release)
        source = source_cache[release_id]
        if source is None:
            row["reason"] = "No choreography PDF in release folder"
            items.append(row)
            continue
        index = choreography_pdf_index(source)
        if index.get("error"):
            row["reason"] = index["error"]
            items.append(row)
            continue
        try:
            fingerprint = choreography_source_fingerprint(source)
        except OSError:
            fingerprint = ""
        resolved = resolve_choreography_mapping(
            index, item, overrides.get(item["track_id"]), fingerprint
        )
        row["mapping_mode"] = resolved["mapping_mode"]
        row["stale"] = resolved["stale"]
        key, pages = resolved["key"], resolved["pages"]
        if not resolved["matched"]:
            row["reason"] = resolved["reason"]
            items.append(row)
            continue
        identity = (str(source), tuple(pages))
        section_index = section_lookup.get(identity)
        if section_index is None:
            section_index = len(sections)
            section_lookup[identity] = section_index
            sections.append({
                "source": source,
                "source_name": source.name,
                "source_pages": [page + 1 for page in pages],
                "pages": pages,
                "key": key,
                "release_title": item["release_title"],
                "track_name": item["name"],
            })
            if len(sections) == 1:
                width, height = index.get("page_size", page_size)
                if 350 <= width <= 2000 and 500 <= height <= 2000:
                    page_size = (width, height)
        row["matched"] = True
        row["section_index"] = section_index
        row["source_pages"] = sections[section_index]["source_pages"]
        items.append(row)

    rows_per_cover = max(8, int((page_size[1] - 250) // 38))
    cover_pages = max(1, (len(items) + rows_per_cover - 1) // rows_per_cover)
    next_page = cover_pages + 1
    for section in sections:
        section["generated_page"] = next_page
        next_page += len(section["pages"])
    for item in items:
        if item["section_index"] is not None:
            item["generated_page"] = sections[item["section_index"]]["generated_page"]
    return {
        "items": items,
        "sections": sections,
        "page_size": page_size,
        "rows_per_cover": rows_per_cover,
        "cover_pages": cover_pages,
        "matched_items": sum(1 for item in items if item["matched"]),
    }


def _register_pdf_display_font() -> str:
    name = "MixMillDisplay"
    with _PDF_FONT_LOCK:
        if name in pdfmetrics.getRegisteredFontNames():
            return name
        path = Path(__file__).parent / "static" / "fonts" / "Anton-Regular.ttf"
        try:
            pdfmetrics.registerFont(TTFont(name, str(path)))
            return name
        except Exception:  # noqa: BLE001
            return "Helvetica-Bold"


def _pdf_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:max(1, limit - 3)].rstrip() + "..."


def choreography_cover(mix: dict, plan: dict) -> BytesIO:
    """Build MixMill cover/index pages; source choreography pages stay intact."""
    buffer = BytesIO()
    width, height = plan["page_size"]
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setTitle(f"{mix['name']} - Choreography Notes")
    pdf.setAuthor("MixMill")
    display = _register_pdf_display_font()
    bg, panel = "#0B0D0B", "#151915"
    text, dim, green, red, line = "#F1F5F1", "#98A199", "#00E05F", "#FF5C5C", "#2C322D"
    rows = plan["rows_per_cover"]
    chunks = [plan["items"][i:i + rows] for i in range(0, len(plan["items"]), rows)] or [[]]
    for page_number, chunk in enumerate(chunks, start=1):
        pdf.setFillColor(bg)
        pdf.rect(0, 0, width, height, stroke=0, fill=1)
        pdf.setFillColor(green)
        pdf.rect(0, height - 8, width, 8, stroke=0, fill=1)
        margin = 42
        pdf.setFillColor(dim)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(margin, height - 45, "MIXMILL / STUDY EDITION")
        pdf.setFillColor(text)
        pdf.setFont(display, 31 if page_number == 1 else 23)
        heading = _pdf_text(mix["name"] if page_number == 1 else "TRACK INDEX / CONTINUED", 42)
        pdf.drawString(margin, height - 92, heading)
        if page_number == 1:
            pdf.setFillColor(dim)
            pdf.setFont("Helvetica", 9)
            pdf.drawString(
                margin, height - 112,
                f"{len(plan['items'])} mix tracks - {plan['matched_items']} with study notes",
            )
            pdf.setFillColor(green)
            pdf.rect(margin, height - 137, 54, 3, stroke=0, fill=1)
            y = height - 174
        else:
            y = height - 135
        for item in chunk:
            pdf.setFillColor(panel)
            pdf.rect(margin, y - 25, width - 2 * margin, 34, stroke=0, fill=1)
            if item["matched"]:
                item["cover_page"] = page_number - 1
                item["cover_rect"] = [margin, y - 25, width - margin, y + 9]
            pdf.setFillColor(green if item["matched"] else red)
            pdf.setFont(display, 13)
            pdf.drawString(margin + 9, y - 4, f"{item['position']:02d}")
            pdf.setFillColor(text)
            pdf.setFont("Helvetica-Bold", 9.5)
            pdf.drawString(margin + 40, y, _pdf_text(item["track_name"], 54))
            pdf.setFillColor(dim)
            pdf.setFont("Helvetica", 7.5)
            subtitle = item["release_title"]
            if item["matched"]:
                source_pages = item["source_pages"]
                source_range = (
                    str(source_pages[0]) if len(source_pages) == 1
                    else f"{source_pages[0]}-{source_pages[-1]}"
                )
                subtitle += f" - source pages {source_range}"
            else:
                subtitle += " - " + item["reason"]
            pdf.drawString(margin + 40, y - 13, _pdf_text(subtitle, 78))
            pdf.setFillColor(green if item["matched"] else red)
            pdf.setFont("Helvetica-Bold", 8)
            status = f"PDF {item['generated_page']}" if item["matched"] else "NOT FOUND"
            pdf.drawRightString(width - margin - 9, y - 5, status)
            y -= 38
        pdf.setStrokeColor(line)
        pdf.line(margin, 40, width - margin, 40)
        pdf.setFillColor(dim)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(margin, 25, "Original choreography pages follow unchanged.")
        pdf.drawRightString(width - margin, 25, f"INDEX {page_number}/{len(chunks)}")
        pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer


def music_index_for(track_name: str, music_names: list[str],
                    manual: int | None = None) -> int | None:
    """Index of the song paired with a track: a valid manual pick wins,
    otherwise match by the shared leading track number."""
    if manual is not None and 0 <= manual < len(music_names):
        return manual
    key = track_number_key(track_name)
    if key is None:
        return None
    return next((j for j, nm in enumerate(music_names)
                 if track_number_key(nm) == key), None)


def get_release_or_404(conn, release_id: int):
    row = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
    if not row:
        raise HTTPException(404, "release not found")
    return row


EXPORT_ID_RE = re.compile(r"[0-9a-f]{32}")  # uuid4().hex — the only shape we mint


def export_path(name: str) -> Path:
    """A path directly inside EXPORT_DIR, or 404. Everything that reaches the
    filesystem goes through here: a backslash is a legal URL path segment
    character, and Windows canonicalises EXPORT_DIR/'tmp_..\\..' straight back
    to EXPORT_DIR itself."""
    path = (EXPORT_DIR / name).resolve()
    if path.parent != EXPORT_DIR.resolve():
        raise HTTPException(404, "export not found")
    return path


def scratch_dir(export_id: str) -> Path:
    if not EXPORT_ID_RE.fullmatch(export_id):
        raise HTTPException(404, "export not found")
    return export_path(f"tmp_{export_id}")


# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=32)


def new_job(jtype: str, payload: dict) -> dict:
    job = {"id": uuid.uuid4().hex, "type": jtype, "status": "queued",
           "progress": 0.0, "detail": "", "result": None, "error": None,
           "payload": payload, "created_at": time.time()}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        finished = sorted((j for j in JOBS.values()
                           if j["status"] in ("done", "error")),
                          key=lambda j: j["created_at"])
        for j in finished[:-50]:
            JOBS.pop(j["id"], None)
    try:
        JOB_QUEUE.put_nowait(job["id"])
    except queue.Full:
        with JOBS_LOCK:
            JOBS.pop(job["id"], None)
        raise HTTPException(429, "background job queue is full; try again later")
    return job


def _job_worker():
    while True:
        jid = JOB_QUEUE.get()
        with JOBS_LOCK:
            job = JOBS.get(jid)
        if not job:
            continue
        job["status"] = "running"
        try:
            if job["type"] == "auto":
                job["result"] = _auto_tracks_impl(job["payload"]["release_id"], job)
            elif job["type"] == "auto_all":
                job["result"] = _auto_all_impl(job)
            elif job["type"] == "extract":
                job["result"] = _extract_impl(job["payload"]["release_id"])
            elif job["type"] == "extract_all":
                job["result"] = _extract_all_impl(job)
            job["progress"] = 1.0
            job["status"] = "done"
        except HTTPException as exc:
            job["status"] = "error"
            job["error"] = str(exc.detail)
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)


def _auto_all_impl(job: dict) -> dict:
    with db() as conn:
        rows = conn.execute(
            """SELECT r.* FROM releases r
               LEFT JOIN tracks t ON t.release_id = r.id
               WHERE r.missing=0 GROUP BY r.id HAVING COUNT(t.id)=0"""
        ).fetchall()
    total_imported, failed = 0, []
    for i, row in enumerate(rows):
        job["detail"] = row["title"]
        job["progress"] = i / max(1, len(rows))
        try:
            res = _auto_tracks_impl(row["id"])
            total_imported += res["imported"]
        except HTTPException as exc:
            failed.append(f"{row['title']}: {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{row['title']}: {exc}")
    return {"releases": len(rows), "imported": total_imported, "failed": failed}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "job not found")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return {k: v for k, v in job.items() if k != "payload"}


# ---------------------------------------------------------------- models

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_control_characters(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError("control characters are not allowed")
        return value


ShortProgram = Annotated[str, Field(max_length=100)]
PositiveId = Annotated[int, Field(gt=0, le=9223372036854775807)]
SlotNumber = Annotated[int, Field(ge=1, le=100)]


class ReleasePatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    program: str | None = Field(default=None, max_length=100)
    vaulted: int | None = None
    curated: int | None = None


class TrackIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    start: float = Field(ge=0, le=86400)
    end: float = Field(gt=0, le=86400)


class TrackPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    start: float | None = Field(default=None, ge=0, le=86400)
    end: float | None = Field(default=None, gt=0, le=86400)
    rejected: int | None = None


class ChoreographyMappingIn(StrictModel):
    mode: str = Field(max_length=16)
    page_start: int | None = Field(default=None, ge=1, le=2000)
    page_end: int | None = Field(default=None, ge=1, le=2000)


class MixIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class MixPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    audio: int | None = None  # 0 = file default, 1 = second audio track


class MixItemsIn(StrictModel):
    track_ids: list[PositiveId] = Field(max_length=500)


class MixItemPatch(StrictModel):
    music_index: int | None = Field(default=None, ge=0, le=1000)


class ExportIn(StrictModel):
    mode: str = "fast"  # "fast" (stream copy, keyframe-snapped) | "precise" (re-encode)


class GenerateIn(StrictModel):
    mode: str  # "program" | "any"
    source_pool: str = "curated"  # "curated" | "discovery"
    program: str | None = Field(default=None, max_length=100)
    programs: list[ShortProgram] | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    minutes: int = Field(default=60, ge=1, le=720)
    slot_min: int | None = Field(default=None, ge=1, le=100)
    slot_max: int | None = Field(default=None, ge=1, le=100)
    release_min: int | None = Field(default=None, ge=1, le=10000)
    release_max: int | None = Field(default=None, ge=1, le=10000)
    required_slots: list[SlotNumber] | None = Field(default=None, max_length=100)
    excluded_slots: list[SlotNumber] | None = Field(default=None, max_length=100)
    max_per_release: int | None = Field(default=None, ge=1, le=100)
    seed: int | None = Field(default=None, ge=0, le=9223372036854775807)
    include_rejected: bool = False
    include_vault: bool = False


# ---------------------------------------------------------------- library

SCAN_LOCK = threading.Lock()


@app.post("/api/scan")
def scan_library():
    if not SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a library scan is already running")
    try:
        return _scan_library_impl()
    finally:
        SCAN_LOCK.release()


def _scan_library_impl():
    if not VIDEO_DIR.exists():
        raise HTTPException(500, f"video directory {VIDEO_DIR} does not exist")
    skip_dirs = {"music", "musique", "audio", "@eadir", "#recycle"}
    found = []
    for p in sorted(VIDEO_DIR.rglob("*")):
        if not (p.is_file() and p.suffix.lower() in VIDEO_EXTS):
            continue
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if not is_within(resolved, VIDEO_DIR):
            continue
        rel = p.relative_to(VIDEO_DIR)
        # skip music/audio subfolders and NAS system folders anywhere in the path
        if any(part.lower() in skip_dirs for part in rel.parts[:-1]):
            continue
        found.append(str(rel))
    with db() as conn:
        known = {r["relpath"]: r for r in conn.execute("SELECT * FROM releases")}
    # ffprobe takes seconds per file, and sqlite3 keeps the write lock from the first
    # INSERT until the commit, so probe everything with no connection open — otherwise
    # every concurrent write blocks past busy_timeout for the whole scan.
    fresh = []
    for rel in found:
        if rel not in known:
            title, program = infer_title_program(rel)
            fresh.append((rel, title, program, probe_duration(VIDEO_DIR / rel),
                          time.time()))
    on_disk = set(found)
    added, updated = 0, 0
    with db() as conn:
        for row in fresh:
            # a scan racing this one may have inserted it while we were probing
            added += conn.execute(
                "INSERT OR IGNORE INTO releases (relpath, title, program, duration, added_at)"
                " VALUES (?,?,?,?,?)",
                row,
            ).rowcount
        for rel, row in known.items():
            if rel in on_disk:
                if row["missing"]:
                    conn.execute("UPDATE releases SET missing=0 WHERE id=?", (row["id"],))
                    updated += 1
            elif not row["missing"]:
                conn.execute("UPDATE releases SET missing=1 WHERE id=?", (row["id"],))
    return {"found": len(found), "added": added, "restored": updated}


@app.get("/api/releases")
def list_releases(include_tracks: bool = False):
    with db() as conn:
        rows = conn.execute(
            """SELECT r.*,
                      SUM(CASE WHEN t.id IS NOT NULL AND t.rejected=0 THEN 1 ELSE 0 END) AS track_count,
                      SUM(CASE WHEN t.id IS NOT NULL AND t.rejected=1 THEN 1 ELSE 0 END) AS rejected_count,
                      COUNT(t.id) AS detected_count,
                      (SELECT COUNT(DISTINCT mi.mix_id) FROM mix_items mi
                       JOIN tracks t2 ON t2.id = mi.track_id
                       WHERE t2.release_id = r.id) AS mix_count
               FROM releases r LEFT JOIN tracks t ON t.release_id = r.id
               GROUP BY r.id"""
        ).fetchall()
        releases = [dict(r) for r in rows]
        releases.sort(key=lambda r: (natural_key(r["program"]), natural_key(r["title"])))
        if include_tracks:
            # one extra query for every release, not one per release
            by_rel = {}
            for t in conn.execute(
                    "SELECT * FROM tracks WHERE rejected=0 ORDER BY release_id, start"):
                by_rel.setdefault(t["release_id"], []).append(dict(t))
            for r in releases:
                r["tracks"] = by_rel.get(r["id"], [])
                names = ([clean_name(f.stem) for f in music_files(r)]
                         if r["tracks"] else [])
                for t in r["tracks"]:
                    idx = music_index_for(t["name"], names)
                    t["music_index"] = idx
                    t["music_name"] = names[idx] if idx is not None else None
    return releases


@app.delete("/api/releases/missing")
def purge_missing(confirm: bool = False):
    if not confirm:
        raise HTTPException(400, "confirm=true is required to purge missing releases")
    with db() as conn:
        n = conn.execute("DELETE FROM releases WHERE missing=1").rowcount
    return {"deleted": n}


@app.get("/api/releases/{release_id}")
def get_release(release_id: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
        tracks = [dict(t) for t in conn.execute(
            "SELECT * FROM tracks WHERE release_id=? AND rejected=0 ORDER BY start",
            (release_id,)
        ).fetchall()]
        rejected_tracks = [dict(t) for t in conn.execute(
            "SELECT * FROM tracks WHERE release_id=? AND rejected=1 ORDER BY start",
            (release_id,)
        ).fetchall()]
    release = dict(row)
    all_tracks = tracks + rejected_tracks
    names = [clean_name(f.stem) for f in music_files(release)] if all_tracks else []
    for track in all_tracks:
        idx = music_index_for(track["name"], names)
        track["music_index"] = idx
        track["music_name"] = names[idx] if idx is not None else None
    return {**release, "tracks": tracks, "rejected_tracks": rejected_tracks}


def release_choreography_status(release_id: int, refresh: bool = False) -> dict:
    with db() as conn:
        release_row = get_release_or_404(conn, release_id)
        release = dict(release_row)
        tracks = [dict(row) for row in conn.execute(
            "SELECT * FROM tracks WHERE release_id=? AND rejected=0 ORDER BY start",
            (release_id,),
        )]
        overrides = {
            row["track_id"]: dict(row)
            for row in conn.execute(
                """SELECT cm.* FROM choreography_mappings cm
                   JOIN tracks t ON t.id=cm.track_id WHERE t.release_id=?""",
                (release_id,),
            )
        }
    source = choreography_pdf_for_release(release)
    if source is None:
        rows = []
        for track in tracks:
            override = overrides.get(track["id"])
            mode = override["mode"] if override else "auto"
            rows.append({
                "track_id": track["id"], "name": track["name"],
                "matched": False, "mapping_mode": mode, "stale": False,
                "source_pages": [], "page_start": None, "page_end": None,
                "options": [],
                "reason": ("Notes disabled for this track" if mode == "disabled"
                           else "No choreography PDF in release folder"),
            })
        return {
            "available": False, "source_name": None, "source_fingerprint": None,
            "page_count": 0, "mapped_tracks": 0, "total_tracks": len(tracks),
            "stale_tracks": 0, "tracks": rows,
        }
    try:
        fingerprint = choreography_source_fingerprint(source)
    except OSError:
        raise HTTPException(404, "choreography PDF is unavailable") from None
    if refresh:
        with _CHOREOGRAPHY_CACHE_LOCK:
            _CHOREOGRAPHY_INDEX_CACHE.pop(str(source), None)
        for cached in THUMB_CACHE.glob(f"choreo_{release_id}_*.png"):
            cached.unlink(missing_ok=True)
    index = choreography_pdf_index(source)
    rows = []
    for track in tracks:
        if index.get("error"):
            resolved = {
                "matched": False, "mapping_mode": "auto", "stale": False,
                "source_pages": [], "reason": index["error"], "options": [],
            }
        else:
            resolved = resolve_choreography_mapping(
                index, track, overrides.get(track["id"]), fingerprint
            )
        source_pages = resolved.get("source_pages", [])
        options = [
            {key: value for key, value in option.items() if key != "pages"}
            for option in resolved.get("options", [])
        ]
        rows.append({
            "track_id": track["id"], "name": track["name"],
            "matched": resolved["matched"],
            "mapping_mode": resolved["mapping_mode"],
            "stale": resolved["stale"],
            "source_pages": source_pages,
            "page_start": source_pages[0] if source_pages else None,
            "page_end": source_pages[-1] if source_pages else None,
            "options": options, "reason": resolved["reason"],
        })
    return {
        "available": not bool(index.get("error")),
        "source_name": source.name,
        "source_fingerprint": fingerprint,
        "page_count": index.get("page_count", 0),
        "mapped_tracks": sum(1 for row in rows if row["matched"]),
        "total_tracks": len(rows),
        "stale_tracks": sum(1 for row in rows if row["stale"]),
        "tracks": rows,
        "error": index.get("error"),
    }


@app.get("/api/releases/{release_id}/choreography-notes")
def release_choreography_notes(release_id: int):
    return release_choreography_status(release_id)


@app.post("/api/releases/{release_id}/choreography-notes/rescan")
def rescan_release_choreography_notes(release_id: int):
    return release_choreography_status(release_id, refresh=True)


@app.get("/api/releases/{release_id}/choreography-notes/source")
def choreography_notes_source(release_id: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    source = choreography_pdf_for_release(row)
    if source is None:
        raise HTTPException(404, "choreography PDF not found")
    index = choreography_pdf_index(source)
    if index.get("error"):
        raise HTTPException(422, index["error"])
    return FileResponse(source, media_type="application/pdf")


@app.get("/api/releases/{release_id}/choreography-notes/pages/{page_number}")
def choreography_note_page_preview(release_id: int, page_number: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    source = choreography_pdf_for_release(row)
    if source is None:
        raise HTTPException(404, "choreography PDF not found")
    index = choreography_pdf_index(source)
    if index.get("error"):
        raise HTTPException(422, index["error"])
    if not 1 <= page_number <= index["page_count"]:
        raise HTTPException(404, "choreography page not found")
    fingerprint = choreography_source_fingerprint(source)[:16]
    cache = THUMB_CACHE / f"choreo_{release_id}_{fingerprint}_{page_number}.png"
    if not cache.is_file() or not cache.stat().st_size:
        with CHOREOGRAPHY_PREVIEW_SLOTS:
            if not cache.is_file() or not cache.stat().st_size:
                prefix = THUMB_CACHE / f"tmp_choreo_{uuid.uuid4().hex}"
                rendered = prefix.with_suffix(".png")
                if pdfium is not None:
                    try:
                        document = pdfium.PdfDocument(str(source))
                        page = document[page_number - 1]
                        bitmap = page.render(scale=110 / 72)
                        image = bitmap.to_pil()
                        image.save(rendered, format="PNG")
                        image.close()
                        bitmap.close()
                        page.close()
                        document.close()
                    except Exception as exc:  # noqa: BLE001
                        rendered.unlink(missing_ok=True)
                        raise HTTPException(422, "could not render choreography page") from exc
                else:
                    try:
                        proc = _run_subprocess(
                            ["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                             "-singlefile", "-png", "-r", "110", str(source), str(prefix)],
                            capture_output=True, text=True, timeout=60,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        rendered.unlink(missing_ok=True)
                        raise HTTPException(503, "PDF preview generator unavailable") from None
                    if proc.returncode != 0:
                        rendered.unlink(missing_ok=True)
                        raise HTTPException(422, "could not render choreography page")
                if not rendered.is_file() or not rendered.stat().st_size:
                    rendered.unlink(missing_ok=True)
                    raise HTTPException(422, "could not render choreography page")
                os.replace(rendered, cache)
    return FileResponse(cache, media_type="image/png")


@app.patch("/api/releases/{release_id}")
def patch_release(release_id: int, body: ReleasePatch):
    with db() as conn:
        get_release_or_404(conn, release_id)
        if body.title is not None:
            conn.execute("UPDATE releases SET title=? WHERE id=?", (body.title, release_id))
        if body.program is not None:
            conn.execute("UPDATE releases SET program=? WHERE id=?", (body.program, release_id))
        if body.vaulted is not None:
            if body.vaulted not in (0, 1):
                raise HTTPException(400, "vaulted must be 0 or 1")
            conn.execute("UPDATE releases SET vaulted=? WHERE id=?",
                         (body.vaulted, release_id))
        if body.curated is not None:
            if body.curated not in (0, 1):
                raise HTTPException(400, "curated must be 0 or 1")
            if body.curated and not conn.execute(
                    "SELECT 1 FROM tracks WHERE release_id=? AND rejected=0 LIMIT 1",
                    (release_id,)).fetchone():
                raise HTTPException(400, "at least one kept segment is required")
            conn.execute("UPDATE releases SET curated=? WHERE id=?",
                         (body.curated, release_id))
    return get_release(release_id)


@app.get("/api/releases/{release_id}/music")
def list_music(release_id: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    files = music_files(row)
    return [{"index": i, "name": clean_name(f.stem), "filename": f.name}
            for i, f in enumerate(files)]


@app.get("/api/releases/{release_id}/music/{index}")
def stream_music(release_id: int, index: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    files = music_files(row)
    # served by index into the server-built list, so no client path ever
    # touches the filesystem
    if not 0 <= index < len(files):
        raise HTTPException(404, "music track not found")
    f = files[index]
    return FileResponse(f, media_type=AUDIO_MIME.get(f.suffix.lower(), "audio/mpeg"),
                        filename=f.name)


@app.get("/api/releases/{release_id}/thumb")
def release_thumb(release_id: int, music_cover: bool = False):
    """Preferred embedded music cover or poster frame, cached as jpeg.
    Music-cover failures fall back to the existing video thumbnail."""
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    # Cached responses leave this section almost immediately. Cache misses are
    # deliberately capped so opening a large grid cannot launch dozens of
    # ffmpeg processes at once on a small NAS.
    with THUMBNAIL_SLOTS:
        return _release_thumb_response(release_id, row, music_cover)


def _release_thumb_response(release_id: int, row: sqlite3.Row,
                            music_cover: bool):
    if music_cover:
        songs = music_files(row)
        if songs:
            cover_cache = THUMB_CACHE / f"cover_{release_id}.jpg"
            try:
                newest_source = max(song.stat().st_mtime for song in songs)
            except OSError:
                newest_source = time.time()
            if (not cover_cache.is_file()
                    or cover_cache.stat().st_mtime < newest_source):
                cover_cache.unlink(missing_ok=True)
                for song in songs:
                    tmp = THUMB_CACHE / f"tmp_{uuid.uuid4().hex}.jpg"
                    try:
                        proc = _run_subprocess(
                            [FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
                             "-y", "-i", str(song), "-map", "0:v:0?",
                             "-frames:v", "1", "-vf",
                             "scale=480:270:force_original_aspect_ratio=decrease,"
                             "pad=480:270:(ow-iw)/2:(oh-ih)/2:black",
                             "-q:v", "3", str(tmp)],
                            capture_output=True, text=True, timeout=60,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        proc = None
                    if (proc and proc.returncode == 0 and tmp.is_file()
                            and tmp.stat().st_size):
                        os.replace(tmp, cover_cache)
                        break
                    tmp.unlink(missing_ok=True)
            if cover_cache.is_file() and cover_cache.stat().st_size:
                return FileResponse(cover_cache, media_type="image/jpeg")
    src = release_path(row)
    if not src.is_file():
        raise HTTPException(404, "video file missing on disk")
    cache = THUMB_CACHE / f"{release_id}.jpg"
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        dur = row["duration"] or probe_duration(src)
        tmp = THUMB_CACHE / f"tmp_{uuid.uuid4().hex}.jpg"
        try:
            proc = _run_subprocess(
                [FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
                 "-y", "-ss", str(max(0.0, dur * 0.2)), "-i", str(src),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(tmp)],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            tmp.unlink(missing_ok=True)
            raise HTTPException(503, "thumbnail generator unavailable") from None
        if proc.returncode != 0 or not tmp.is_file() or not tmp.stat().st_size:
            tmp.unlink(missing_ok=True)
            raise HTTPException(404, "could not generate thumbnail")
        os.replace(tmp, cache)
    return FileResponse(cache, media_type="image/jpeg")


MATCH_SR = 4000  # mono sample rate used for audio matching; plenty for alignment


def decode_pcm(path: Path, sr: int = MATCH_SR, offset: float = 0.0,
               duration: float | None = None) -> np.ndarray:
    """Mono 16-bit PCM samples of a media file's audio via ffmpeg."""
    cmd = [FFMPEG_COMMAND, "-nostdin", "-v", "quiet",
           "-protocol_whitelist", "file,pipe"]
    if offset:
        cmd += ["-ss", str(offset)]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        out = _run_subprocess(cmd, capture_output=True, timeout=min(600, MEDIA_TIMEOUT))
    except (OSError, subprocess.TimeoutExpired):
        return np.empty(0, dtype=np.int16)
    if out.returncode != 0:
        return np.empty(0, dtype=np.int16)
    return np.frombuffer(out.stdout, dtype=np.int16)


def best_offset(hay: np.ndarray, needle: np.ndarray) -> tuple[int, float]:
    """(sample offset where needle best matches inside hay, score 0..1).
    FFT cross-correlation normalized by local energy, so a loud section
    can't out-score the actual match by volume alone."""
    if not len(needle) or len(hay) < len(needle):
        return 0, 0.0
    hay_f = hay.astype(np.float32)
    ndl = needle.astype(np.float32)
    n = len(hay_f) + len(ndl) - 1
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay_f, nfft) * np.fft.rfft(ndl[::-1], nfft), nfft)
    corr = corr[len(ndl) - 1: len(hay_f)]
    csum = np.concatenate(([0.0], np.cumsum(hay_f.astype(np.float64) ** 2)))
    win = csum[len(ndl):] - csum[: len(hay_f) - len(ndl) + 1]
    denom = np.sqrt(win * float(np.sum(ndl.astype(np.float64) ** 2))) + 1e-6
    scores = corr / denom
    i = int(np.argmax(scores))
    return i, float(scores[i])


def _auto_tracks_impl(release_id: int, job: dict | None = None) -> dict:
    """Create tracks automatically: embedded chapter markers when the file has
    them, otherwise align each song from the music folder against the video's
    audio and cut a segment per song."""
    res = import_chapters(release_id)
    # Existing chapter rows are still proof that this file uses chapters.
    # Falling through when every chapter was skipped makes a second run scan
    # the music folder and create near-overlapping duplicate tracks.
    if res["chapters"]:
        return {"method": "chapters", **res}
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    songs = music_files(row)
    if not songs:
        raise HTTPException(
            400, "no chapter markers in the file and no music folder to match against")
    video = release_path(row)
    if not video.is_file():
        raise HTTPException(404, "video file missing on disk")
    hay = decode_pcm(video)
    if not len(hay):
        raise HTTPException(500, "could not decode the video's audio")
    if job is not None:
        job["progress"] = 0.1
        job["detail"] = "decoded video audio"
    video_dur = len(hay) / MATCH_SR
    matched, guessed = 0, 0
    found: list[tuple[str, float, float]] = []
    cursor = 0.0  # where the previous song ended; songs run in order
    for song_i, song in enumerate(songs):
        song_dur = probe_duration(song)
        if song_dur <= 0:
            continue
        # snippet from a few seconds in: song intros are often quiet or
        # spoken over in class, mid-song audio is the reliable part
        snip_off = min(5.0, song_dur / 10)
        snip_dur = min(12.0, max(3.0, song_dur - snip_off))
        needle = decode_pcm(song, offset=snip_off, duration=snip_dur)
        lo = max(0.0, cursor - 45.0)
        hi = min(video_dur, cursor + 240.0 + snip_dur)
        seg = hay[int(lo * MATCH_SR): int(hi * MATCH_SR)]
        idx, score = best_offset(seg, needle)
        if score >= 0.30:
            start = max(0.0, lo + idx / MATCH_SR - snip_off)
            matched += 1
        else:
            # correlation found nothing convincing (talk-over remix, missing
            # song): assume it follows the previous one back-to-back
            start = cursor
            guessed += 1
        end = min(start + song_dur, video_dur)
        if end - start < 3:
            continue
        found.append((clean_name(song.stem), start, end))
        cursor = end
        if job is not None:
            job["progress"] = 0.1 + 0.85 * (song_i + 1) / len(songs)
            job["detail"] = clean_name(song.stem)
    imported, skipped = 0, 0
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT start, end FROM tracks WHERE release_id=?", (release_id,)
        ).fetchall()
        for i, (name, start, end) in enumerate(found):
            if any(abs(e["start"] - start) < 0.5 and abs(e["end"] - end) < 0.5
                   for e in existing):
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO tracks (release_id, name, start, end, position) VALUES (?,?,?,?,?)",
                (release_id, name, start, end, i),
            )
            imported += 1
        if imported:
            conn.execute("UPDATE releases SET curated=0 WHERE id=?", (release_id,))
    return {"method": "music", "imported": imported, "skipped": skipped,
            "matched": matched, "guessed": guessed}


@app.post("/api/releases/{release_id}/auto-tracks")
def auto_tracks(release_id: int):
    with db() as conn:
        get_release_or_404(conn, release_id)
    with JOBS_LOCK:
        for j in JOBS.values():
            if (j["type"] == "auto" and j["status"] in ("queued", "running")
                    and j["payload"].get("release_id") == release_id):
                return {"job_id": j["id"]}
    return {"job_id": new_job("auto", {"release_id": release_id})["id"]}


@app.post("/api/auto-tracks-all")
def auto_tracks_all():
    with JOBS_LOCK:
        for j in JOBS.values():
            if j["type"] == "auto_all" and j["status"] in ("queued", "running"):
                return {"job_id": j["id"]}
    return {"job_id": new_job("auto_all", {})["id"]}


def cached_audio_path(release_id: int, track: int = 1) -> Path:
    return AUDIO_CACHE / f"{release_id}_a{track}.m4a"


def ensure_audio_cache(release_id: int, row, track: int = 1) -> Path:
    """Extract the release's Nth audio stream to the cache (idempotent).
    Raises HTTPException on missing file / missing stream / ffmpeg failure."""
    src = release_path(row)
    if not src.is_file():
        raise HTTPException(404, "video file missing on disk")
    if track < 0 or audio_stream_count(src) <= track:
        raise HTTPException(404, "file has no such audio track")
    cache = cached_audio_path(release_id, track)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        tmp = AUDIO_CACHE / f"tmp_{uuid.uuid4().hex}.m4a"
        # stream copy when the codec fits an m4a container, else transcode
        media_tool_unavailable = False
        for args in (["-c", "copy"], ["-c:a", "aac", "-b:a", "192k"]):
            try:
                proc = _run_subprocess(
                    [FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
                     "-y", "-i", str(src), "-map", f"0:a:{track}",
                     "-vn", *args, str(tmp)],
                    capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                media_tool_unavailable = True
                break
            if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size:
                break
        else:
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "could not extract the audio track")
        if media_tool_unavailable:
            tmp.unlink(missing_ok=True)
            raise HTTPException(503, "audio extractor unavailable")
        os.replace(tmp, cache)
    return cache


@app.get("/api/releases/{release_id}/audio")
def stream_release_audio(release_id: int, track: int = Query(default=1, ge=0, le=8)):
    """The release's Nth audio stream as a seekable m4a, extracted to the data
    volume on first use — how the player plays the voice-free track, since
    browsers can't switch embedded audio streams."""
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    cache = ensure_audio_cache(release_id, row, track)
    return FileResponse(cache, media_type="audio/mp4")


_STREAMS_CACHE: dict[int, tuple[float, int]] = {}  # release_id -> (mtime, count)


@app.get("/api/releases/{release_id}/audio-status")
def release_audio_status(release_id: int, track: int = Query(default=1, ge=0, le=8)):
    """Cache/stream state for the player's "voice off" toggle, so the UI can
    show a "preparing..." badge instead of blocking on ffmpeg."""
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    src = release_path(row)
    if not src.is_file():
        raise HTTPException(404, "video file missing on disk")
    mtime = src.stat().st_mtime
    memo = _STREAMS_CACHE.get(release_id)
    if memo is None or memo[0] != mtime:
        memo = (mtime, audio_stream_count(src))
        _STREAMS_CACHE[release_id] = memo
    cache = cached_audio_path(release_id, track)
    cached = cache.is_file() and cache.stat().st_mtime >= mtime
    return {"cached": cached, "streams": memo[1]}


def _extract_impl(release_id: int) -> dict:
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    src = release_path(row)
    if not src.is_file():
        raise HTTPException(404, "video file missing on disk")
    if audio_stream_count(src) < 2:
        return {"extracted": 0, "reason": "no second audio stream"}
    ensure_audio_cache(release_id, row)
    return {"extracted": 1}


def _extract_all_impl(job: dict) -> dict:
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM releases WHERE missing=0 AND vaulted=0")]
    todo = []
    for row in rows:
        src = release_path(row)
        if src.is_file() and not (
                cached_audio_path(row["id"]).is_file()
                and cached_audio_path(row["id"]).stat().st_mtime >= src.stat().st_mtime):
            todo.append(row)
    extracted, skipped, failed = 0, 0, []
    for i, row in enumerate(todo):
        job["detail"] = row["title"]
        job["progress"] = i / max(1, len(todo))
        try:
            res = _extract_impl(row["id"])
            extracted += res["extracted"]
            skipped += 0 if res["extracted"] else 1
        except HTTPException as exc:
            failed.append(f"{row['title']}: {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{row['title']}: {exc}")
    return {"releases": len(todo), "extracted": extracted,
            "skipped": skipped, "failed": failed}


@app.post("/api/releases/{release_id}/extract-audio")
def extract_audio(release_id: int):
    with db() as conn:
        get_release_or_404(conn, release_id)
    with JOBS_LOCK:
        for j in JOBS.values():
            if (j["type"] == "extract" and j["status"] in ("queued", "running")
                    and j["payload"].get("release_id") == release_id):
                return {"job_id": j["id"]}
    return {"job_id": new_job("extract", {"release_id": release_id})["id"]}


@app.post("/api/extract-audio-all")
def extract_audio_all():
    with JOBS_LOCK:
        for j in JOBS.values():
            if j["type"] == "extract_all" and j["status"] in ("queued", "running"):
                return {"job_id": j["id"]}
    return {"job_id": new_job("extract_all", {})["id"]}


@app.post("/api/releases/{release_id}/import-chapters")
def import_chapters(release_id: int):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    info = ffprobe_json(release_path(row), "-show_chapters")
    chapters = info.get("chapters", [])
    if not chapters:
        return {"chapters": 0, "imported": 0, "skipped": 0,
                "message": "no chapter markers found in this file"}
    imported, skipped = 0, 0
    with db() as conn:
        # sqlite3 is in deferred mode, so without this the dedupe read below runs in
        # autocommit and two concurrent imports (double-clicked button) both see an
        # empty table and both insert. BEGIN IMMEDIATE takes the write lock up front
        # so the read-then-insert is atomic and the loser skips.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT start, end FROM tracks WHERE release_id=?", (release_id,)
        ).fetchall()
        for i, ch in enumerate(chapters):
            start, end = float(ch["start_time"]), float(ch["end_time"])
            # same rule as add_track: a zero-length track exports as a 0-byte
            # segment, which ffmpeg -f concat then drops without an error
            if end <= start or any(abs(e["start"] - start) < 0.5 and abs(e["end"] - end) < 0.5
                                   for e in existing):
                skipped += 1
                continue
            name = (ch.get("tags") or {}).get("title") or f"Track {i + 1}"
            conn.execute(
                "INSERT INTO tracks (release_id, name, start, end, position) VALUES (?,?,?,?,?)",
                (release_id, name, start, end, i),
            )
            imported += 1
        if imported:
            conn.execute("UPDATE releases SET curated=0 WHERE id=?", (release_id,))
    return {"chapters": len(chapters), "imported": imported, "skipped": skipped}


# ---------------------------------------------------------------- tracks

@app.post("/api/releases/{release_id}/tracks")
def add_track(release_id: int, body: TrackIn):
    if body.end <= body.start:
        raise HTTPException(400, "end must be after start")
    with db() as conn:
        get_release_or_404(conn, release_id)
        cur = conn.execute(
            "INSERT INTO tracks (release_id, name, start, end) VALUES (?,?,?,?)",
            (release_id, body.name, body.start, body.end),
        )
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.patch("/api/tracks/{track_id}")
def patch_track(track_id: int, body: TrackPatch):
    with db() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            raise HTTPException(404, "track not found")
        name = body.name if body.name is not None else row["name"]
        start = body.start if body.start is not None else row["start"]
        end = body.end if body.end is not None else row["end"]
        rejected = body.rejected if body.rejected is not None else row["rejected"]
        if end <= start:
            raise HTTPException(400, "end must be after start")
        if rejected not in (0, 1):
            raise HTTPException(400, "rejected must be 0 or 1")
        conn.execute(
            "UPDATE tracks SET name=?, start=?, end=?, rejected=? WHERE id=?",
            (name, start, end, rejected, track_id)
        )
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    return dict(row)


@app.put("/api/tracks/{track_id}/choreography-mapping")
def put_choreography_mapping(track_id: int, body: ChoreographyMappingIn):
    if body.mode not in {"auto", "manual", "disabled"}:
        raise HTTPException(400, "mode must be auto, manual, or disabled")
    with db() as conn:
        track = conn.execute(
            """SELECT t.*, r.relpath, r.title AS title, r.program,
                      r.duration, r.missing, r.vaulted, r.curated, r.added_at
               FROM tracks t JOIN releases r ON r.id=t.release_id WHERE t.id=?""",
            (track_id,),
        ).fetchone()
        if not track:
            raise HTTPException(404, "track not found")
        release_id = track["release_id"]
        if body.mode == "auto":
            conn.execute("DELETE FROM choreography_mappings WHERE track_id=?", (track_id,))
        else:
            source = choreography_pdf_for_release(track)
            fingerprint = ""
            if source is not None:
                fingerprint = choreography_source_fingerprint(source)
            start = end = None
            if body.mode == "manual":
                if source is None:
                    raise HTTPException(400, "release has no choreography PDF")
                if body.page_start is None or body.page_end is None:
                    raise HTTPException(400, "manual mapping requires a page range")
                if body.page_end < body.page_start:
                    raise HTTPException(400, "page end must be after page start")
                if body.page_end - body.page_start + 1 > 100:
                    raise HTTPException(400, "manual mapping may contain at most 100 pages")
                index = choreography_pdf_index(source)
                if index.get("error"):
                    raise HTTPException(422, index["error"])
                if body.page_end > index["page_count"]:
                    raise HTTPException(400, "page range exceeds the choreography PDF")
                start, end = body.page_start, body.page_end
            conn.execute(
                """INSERT INTO choreography_mappings
                   (track_id, mode, page_start, page_end, source_fingerprint, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(track_id) DO UPDATE SET mode=excluded.mode,
                     page_start=excluded.page_start, page_end=excluded.page_end,
                     source_fingerprint=excluded.source_fingerprint,
                     updated_at=excluded.updated_at""",
                (track_id, body.mode, start, end, fingerprint, time.time()),
            )
    return release_choreography_status(release_id)


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: int, permanent: bool = False):
    with db() as conn:
        row = conn.execute("SELECT release_id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            raise HTTPException(404, "track not found")
        if permanent:
            conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            return {"ok": True, "deleted": True}
        conn.execute("UPDATE tracks SET rejected=1 WHERE id=?", (track_id,))
        conn.execute(
            """UPDATE releases SET curated=0 WHERE id=? AND NOT EXISTS (
                   SELECT 1 FROM tracks WHERE release_id=? AND rejected=0
               )""",
            (row["release_id"], row["release_id"]),
        )
    return {"ok": True, "rejected": True}


# ---------------------------------------------------------------- streaming

CHUNK = 1024 * 1024


@app.get("/api/stream/{release_id}")
def stream_video(release_id: int, request: Request):
    with db() as conn:
        row = get_release_or_404(conn, release_id)
    path = release_path(row)
    if not path.is_file():
        raise HTTPException(404, "video file missing on disk")
    size = path.stat().st_size
    range_header = request.headers.get("range")
    content_type = MIME_BY_EXT.get(path.suffix.lower(), "video/mp4")

    start, end = 0, size - 1
    status = 200
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        first, last = (m.group(1), m.group(2)) if m else ("", "")
        if first or last:  # neither present is malformed: fall through to a 200
            if first:
                start = int(first)
                if last:
                    end = min(int(last), size - 1)
            else:
                # bytes=-N asks for the LAST N bytes (RFC 7233), clamped to the
                # file — how curl -r and other tail-fetching clients reach the
                # moov atom of an mp4 not muxed with +faststart. Reading it as
                # 0-N hands back the head, and the client just mis-seeks.
                # A suffix of 0 lands start past the end and 416s below.
                start = max(0, size - int(last))
            if start > end or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206

    def iterator(s: int, e: int):
        with open(path, "rb") as f:
            f.seek(s)
            remaining = e - s + 1
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(iterator(start, end), status_code=status,
                             media_type=content_type, headers=headers)


# ---------------------------------------------------------------- mixes

def mix_detail(conn, mix_id: int):
    mix = conn.execute("SELECT * FROM mixes WHERE id=?", (mix_id,)).fetchone()
    if not mix:
        raise HTTPException(404, "mix not found")
    items = conn.execute(
        """SELECT mi.id AS item_id, mi.position, mi.music_idx,
                  t.id AS track_id, t.name, t.start, t.end,
                  r.id AS release_id, r.relpath, r.title AS release_title,
                  r.program, r.missing
           FROM mix_items mi
           JOIN tracks t ON t.id = mi.track_id
           JOIN releases r ON r.id = t.release_id
           WHERE mi.mix_id=? ORDER BY mi.position""",
        (mix_id,),
    ).fetchall()
    out = {**dict(mix), "items": [dict(i) for i in items]}
    # pair each item with its song from the release's music folder: a manual
    # pick (music_idx) wins, otherwise match by the shared track numbering
    music_cache: dict[int, list[str]] = {}
    for it in out["items"]:
        rid = it["release_id"]
        if rid not in music_cache:
            music_cache[rid] = [clean_name(f.stem) for f in music_files(it)]
        names = music_cache[rid]
        idx = music_index_for(it["name"], names, it["music_idx"])
        it["music_index"] = idx
        it["music_name"] = names[idx] if idx is not None else None
        del it["relpath"]  # internal detail, not part of the API shape
    out["total_duration"] = sum(i["end"] - i["start"] for i in out["items"])
    return out


@app.get("/api/mixes")
def list_mixes():
    with db() as conn:
        rows = conn.execute(
            """SELECT m.*, COUNT(mi.id) AS item_count,
                      COALESCE(SUM(t.end - t.start), 0) AS total_duration,
                      (SELECT t2.release_id FROM mix_items mi2
                       JOIN tracks t2 ON t2.id = mi2.track_id
                       WHERE mi2.mix_id = m.id
                       ORDER BY mi2.position LIMIT 1) AS cover_release_id
               FROM mixes m
               LEFT JOIN mix_items mi ON mi.mix_id = m.id
               LEFT JOIN tracks t ON t.id = mi.track_id
               GROUP BY m.id ORDER BY m.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/mixes")
def create_mix(body: MixIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO mixes (name, created_at) VALUES (?,?)", (body.name, time.time())
        )
        return mix_detail(conn, cur.lastrowid)


def matched_song_entries(mix: dict, releases: dict[int, dict]) -> list[tuple[str, Path]]:
    """Ordered, safely named music files available for one mix."""
    entries = []
    files_cache: dict[int, list[Path]] = {}
    for i, item in enumerate(mix["items"]):
        if item["music_index"] is None:
            continue
        release_id = item["release_id"]
        if release_id not in files_cache:
            release = releases.get(release_id)
            files_cache[release_id] = music_files(release) if release else []
        files = files_cache[release_id]
        if item["music_index"] < len(files):
            source = files[item["music_index"]]
            name = f"{i + 1:02d} {clean_name(source.stem)}{source.suffix.lower()}"
            entries.append((name, source))
    return entries


@app.get("/api/mixes/{mix_id}/songs.zip")
def songs_zip(mix_id: int):
    with db() as conn:
        mix = mix_detail(conn, mix_id)
        rels = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM releases")}
    entries = matched_song_entries(mix, rels)
    if not entries:
        raise HTTPException(400, "no matched songs in this mix")
    tmp_zip = TMP_DIR / f"songs_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED) as z:
        for name, f in entries:
            z.write(f, name)
    safe = re.sub(r"[^\w\-]+", "_", mix["name"]).strip("_") or "mix"
    return FileResponse(tmp_zip, media_type="application/zip",
                        filename=f"{safe}_songs.zip",
                        background=BackgroundTask(tmp_zip.unlink, missing_ok=True))


def mix_choreography_plan(mix_id: int) -> tuple[dict, dict]:
    with db() as conn:
        mix = mix_detail(conn, mix_id)
        release_ids = {item["release_id"] for item in mix["items"]}
        releases = {
            row["id"]: dict(row)
            for row in conn.execute("SELECT * FROM releases")
            if row["id"] in release_ids
        }
        track_ids = {item["track_id"] for item in mix["items"]}
        overrides = {
            row["track_id"]: dict(row)
            for row in conn.execute("SELECT * FROM choreography_mappings")
            if row["track_id"] in track_ids
        }
    return mix, choreography_plan(mix, releases, overrides)


@app.get("/api/mixes/{mix_id}/choreography-notes/status")
def choreography_notes_status(mix_id: int):
    mix, plan = mix_choreography_plan(mix_id)
    missing = [
        {
            "position": item["position"],
            "track_name": item["track_name"],
            "release_title": item["release_title"],
            "reason": item["reason"],
        }
        for item in plan["items"] if not item["matched"]
    ]
    matched = plan["matched_items"]
    total = len(mix["items"])
    return {
        "ready": matched > 0,
        "complete": total > 0 and matched == total,
        "matched_tracks": matched,
        "total_tracks": total,
        "source_pdfs": len({str(section["source"]) for section in plan["sections"]}),
        "items": [
            {
                "item_id": item["item_id"], "track_id": item["track_id"],
                "position": item["position"], "matched": item["matched"],
                "source_pages": item.get("source_pages", []),
                "mapping_mode": item.get("mapping_mode", "auto"),
                "stale": item.get("stale", False), "reason": item["reason"],
            }
            for item in plan["items"]
        ],
        "missing": missing,
    }


def build_choreography_pdf(mix: dict, plan: dict) -> Path:
    """Build study PDF for direct download or complete package."""
    if not mix["items"]:
        raise HTTPException(400, "mix has no tracks")
    if not plan["sections"]:
        raise HTTPException(400, "no matching choreography notes found for this mix")
    tmp_pdf = TMP_DIR / f"choreo_{uuid.uuid4().hex}.pdf"
    try:
        writer = PdfWriter()
        cover = PdfReader(choreography_cover(mix, plan), strict=False)
        for page in cover.pages:
            writer.add_page(page)
        readers: dict[Path, PdfReader] = {}
        for section in plan["sections"]:
            source = section["source"]
            reader = readers.get(source)
            if reader is None:
                reader = PdfReader(str(source), strict=False)
                if reader.is_encrypted and reader.decrypt("") == 0:
                    raise ValueError("source choreography PDF is encrypted")
                readers[source] = reader
            for page_number in section["pages"]:
                writer.add_page(reader.pages[page_number])
        for item in plan["items"]:
            if not item["matched"]:
                continue
            target = item["generated_page"] - 1
            writer.add_outline_item(
                f"{item['position']:02d} {item['track_name']} - {item['release_title']}",
                target,
            )
            writer.add_annotation(
                item["cover_page"],
                Link(rect=tuple(item["cover_rect"]), target_page_index=target),
            )
        writer.add_metadata({
            "/Title": f"{mix['name']} - Choreography Notes",
            "/Author": "MixMill",
            "/Subject": "Custom mix study notes assembled from source releases",
        })
        with open(tmp_pdf, "wb") as stream:
            writer.write(stream)
        expected_pages = plan["cover_pages"] + sum(
            len(section["pages"]) for section in plan["sections"]
        )
        check = PdfReader(str(tmp_pdf), strict=False)
        if len(check.pages) != expected_pages:
            raise ValueError("generated choreography PDF failed page verification")
    except Exception as exc:  # noqa: BLE001
        tmp_pdf.unlink(missing_ok=True)
        print(f"choreography PDF build failed: {exc}")
        raise HTTPException(500, "could not build choreography notes") from exc
    return tmp_pdf


@app.get("/api/mixes/{mix_id}/choreography-notes")
def choreography_notes_pdf(mix_id: int):
    mix, plan = mix_choreography_plan(mix_id)
    tmp_pdf = build_choreography_pdf(mix, plan)
    safe = re.sub(r"[^\w\-]+", "_", mix["name"]).strip("_") or "mix"
    return FileResponse(
        tmp_pdf,
        media_type="application/pdf",
        filename=f"{safe}_choreography_notes.pdf",
        background=BackgroundTask(tmp_pdf.unlink, missing_ok=True),
    )


@app.get("/api/mixes/{mix_id}/package.zip")
def mix_package_zip(
    mix_id: int,
    export_id: str = Query(min_length=32, max_length=32),
):
    """Bundle completed video, available study notes, and matched music."""
    if not EXPORT_ID_RE.fullmatch(export_id):
        raise HTTPException(404, "export not found")
    with db() as conn:
        mix = mix_detail(conn, mix_id)
        export = conn.execute(
            "SELECT * FROM exports WHERE id=? AND mix_id=?",
            (export_id, mix_id),
        ).fetchone()
        releases = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM releases")}
    if not export or export["status"] != "done" or not export["filename"]:
        raise HTTPException(409, "video export is not ready")
    video = export_path(export["filename"])
    if not video.is_file():
        raise HTTPException(404, "export file missing")

    safe = re.sub(r"[^\w\-]+", "_", mix["name"]).strip("_") or "mix"
    songs = matched_song_entries(mix, releases)
    _, plan = mix_choreography_plan(mix_id)
    notes = None
    package = TMP_DIR / f"package_{uuid.uuid4().hex}.zip"
    try:
        if plan["sections"]:
            notes = build_choreography_pdf(mix, plan)
        manifest = [
            "MIXMILL COMPLETE PACKAGE",
            f"Mix: {mix['name']}",
            f"Tracks: {len(mix['items'])}",
            f"Video: included ({export['mode']} export)",
            (
                f"Study PDF: included ({plan['matched_items']}/{len(mix['items'])} tracks mapped)"
                if notes else "Study PDF: unavailable (no mapped choreography pages)"
            ),
            f"Music: {len(songs)}/{len(mix['items'])} matched tracks included",
            "",
            "TRACK LIST",
            *[
                f"{index:02d}. {item['name']} — {item['release_title']}"
                for index, item in enumerate(mix["items"], start=1)
            ],
            "",
            "Missing PDF pages or songs are listed in MixMill before download.",
        ]
        with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as archive:
            archive.write(video, f"video/{safe}.mp4")
            if notes:
                archive.write(notes, f"study/{safe}_choreography_notes.pdf")
            for name, source in songs:
                archive.write(source, f"music/{name}")
            archive.writestr("README.txt", "\n".join(manifest) + "\n")
    except Exception:
        package.unlink(missing_ok=True)
        raise
    finally:
        if notes:
            notes.unlink(missing_ok=True)
    return FileResponse(
        package,
        media_type="application/zip",
        filename=f"{safe}_complete_package.zip",
        background=BackgroundTask(package.unlink, missing_ok=True),
    )


@app.post("/api/mixes/{mix_id}/duplicate")
def duplicate_mix(mix_id: int):
    with db() as conn:
        src = conn.execute("SELECT * FROM mixes WHERE id=?", (mix_id,)).fetchone()
        if not src:
            raise HTTPException(404, "mix not found")
        cur = conn.execute(
            "INSERT INTO mixes (name, created_at, audio) VALUES (?,?,?)",
            (src["name"] + " copy", time.time(), src["audio"]))
        new_id = cur.lastrowid
        conn.execute(
            """INSERT INTO mix_items (mix_id, track_id, position, music_idx)
               SELECT ?, track_id, position, music_idx FROM mix_items
               WHERE mix_id=?""",
            (new_id, mix_id))
        return mix_detail(conn, new_id)


def _filter_pool(rows, programs: set[str], slot_lo, slot_hi, rel_lo, rel_hi):
    """Generator candidates: numbered tracks only, program/slot/release-window
    filters applied. Sets r['base_slot'] on survivors."""
    out = []
    for r in rows:
        if programs and (r["program"] or "").lower() not in programs:
            continue
        if rel_lo is not None or rel_hi is not None:
            num = release_number(r["release_title"])
            if num is None:
                continue
            if rel_lo is not None and num < rel_lo:
                continue
            if rel_hi is not None and num > rel_hi:
                continue
        b = base_slot(r["name"])
        if b is None:
            continue
        if slot_lo is not None and b < slot_lo:
            continue
        if slot_hi is not None and b > slot_hi:
            continue
        r["base_slot"] = b
        out.append(r)
    return out


CORE_SLOT = 10


def _trim_to_target(picked: list[dict], target: float,
                    protected_slots: set[int] | None = None) -> list[dict]:
    """Trim high middle slots without dropping endpoints or protected slots."""
    picked.sort(key=lambda r: r["base_slot"])
    protected = {CORE_SLOT, *(protected_slots or set())}

    def total():
        return sum(r["end"] - r["start"] for r in picked)

    while total() > target and len(picked) > 2:
        removable = [r for r in picked[1:-1]
                     if r["base_slot"] not in protected]
        if not removable:
            break
        picked.remove(max(removable, key=lambda r: r["base_slot"]))
    return picked


def _ladder_pick(pool: list[dict], target: float, rng: random.Random,
                 required_slots: set[int] | None = None,
                 max_per_release: int | None = None) -> list[dict]:
    """Choose one random candidate per base slot, so 3/3A/3B are alternatives
    rather than duplicate slot 3 entries. Optional caps spread picks across
    releases; required slots are selected first and survive duration trimming."""
    by_base: dict[int, list[dict]] = {}
    for r in pool:
        by_base.setdefault(r["base_slot"], []).append(r)
    required = required_slots or set()
    missing = required - set(by_base)
    if missing:
        slots = ", ".join(str(n) for n in sorted(missing))
        raise HTTPException(400, f"required slots unavailable: {slots}")
    if not by_base:
        return []
    # Scarce required slots go first so a release cap cannot be consumed by a
    # flexible slot before a slot that exists in only one release.
    priority = sorted(required, key=lambda b: (
        len({r["release_id"] for r in by_base[b]}), b))
    bases = priority + [b for b in sorted(by_base) if b not in required]
    release_counts: dict[int, int] = {}
    picked = []
    for base in bases:
        candidates = by_base[base]
        if max_per_release is not None:
            candidates = [r for r in candidates
                          if release_counts.get(r["release_id"], 0) < max_per_release]
        if not candidates:
            if base in required:
                raise HTTPException(
                    400, f"release cap prevents required slot {base}")
            continue
        if max_per_release is not None:
            least = min(release_counts.get(r["release_id"], 0)
                        for r in candidates)
            candidates = [r for r in candidates
                          if release_counts.get(r["release_id"], 0) == least]
        chosen = rng.choice(candidates)
        picked.append(chosen)
        rid = chosen["release_id"]
        release_counts[rid] = release_counts.get(rid, 0) + 1
    return _trim_to_target(picked, target, required)


@app.post("/api/mixes/generate")
def generate_mix(body: GenerateIn):
    if body.mode not in ("program", "any"):
        raise HTTPException(400, "mode must be 'program' or 'any'")
    if body.source_pool not in ("curated", "discovery"):
        raise HTTPException(400, "source_pool must be 'curated' or 'discovery'")
    if not 10 <= body.minutes <= 120:
        raise HTTPException(400, "minutes must be between 10 and 120")
    for lo, hi, what in ((body.slot_min, body.slot_max, "slot"),
                         (body.release_min, body.release_max, "release")):
        if any(v is not None and v < 1 for v in (lo, hi)):
            raise HTTPException(400, f"{what} range values must be positive")
        if lo is not None and hi is not None and lo > hi:
            raise HTTPException(400, f"{what}_min must not exceed {what}_max")
    required_slots = set(body.required_slots or [])
    excluded_slots = set(body.excluded_slots or [])
    if any(slot < 1 for slot in required_slots | excluded_slots):
        raise HTTPException(400, "required and excluded slots must be positive")
    overlap = required_slots & excluded_slots
    if overlap:
        slots = ", ".join(str(n) for n in sorted(overlap))
        raise HTTPException(400, f"slots cannot be required and excluded: {slots}")
    if body.max_per_release is not None and not 1 <= body.max_per_release <= 50:
        raise HTTPException(400, "max_per_release must be between 1 and 50")
    if body.seed is not None and body.seed < 0:
        raise HTTPException(400, "seed must be zero or positive")
    custom_name = (body.name or "").strip()
    if len(custom_name) > 120:
        raise HTTPException(400, "name must be 120 characters or fewer")
    target = body.minutes * 60
    conditions = ["r.missing=0"]
    if not body.include_vault:
        conditions.append("r.vaulted=0")
    if body.source_pool == "curated":
        conditions.append("r.curated=1")
    if not body.include_rejected:
        conditions.append("t.rejected=0")
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT t.*, r.program, r.title AS release_title FROM tracks t
                JOIN releases r ON r.id = t.release_id
                WHERE {' AND '.join(conditions)}""")]
    if body.mode == "program":
        if not body.program:
            raise HTTPException(400, "program is required in program mode")
        programs = {body.program.lower()}
    else:
        programs = {p.strip().lower() for p in (body.programs or []) if p.strip()}
    pool = _filter_pool(rows, programs, body.slot_min, body.slot_max,
                        body.release_min, body.release_max)
    pool = [r for r in pool if r["base_slot"] not in excluded_slots]
    rng = random.Random(body.seed)
    picked = _ladder_pick(pool, target, rng, required_slots,
                          body.max_per_release)
    label = "all detected" if body.source_pool == "discovery" else "curated"
    if body.mode == "program":
        default_name = f"{body.program} {label} {time.strftime('%Y-%m-%d %H:%M')}"
    else:
        default_name = f"Mixed {label} {time.strftime('%Y-%m-%d %H:%M')}"
    name = custom_name or default_name
    if not picked:
        raise HTTPException(400, "not enough marked tracks match these filters")
    with db() as conn:
        cur = conn.execute("INSERT INTO mixes (name, created_at) VALUES (?,?)",
                           (name, time.time()))
        new_id = cur.lastrowid
        for pos, r in enumerate(picked):
            conn.execute(
                "INSERT INTO mix_items (mix_id, track_id, position) VALUES (?,?,?)",
                (new_id, r["id"], pos))
        return mix_detail(conn, new_id)


@app.get("/api/mixes/{mix_id}")
def get_mix(mix_id: int):
    with db() as conn:
        return mix_detail(conn, mix_id)


@app.put("/api/mixes/{mix_id}/items")
def set_mix_items(mix_id: int, body: MixItemsIn):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM mixes WHERE id=?", (mix_id,)).fetchone():
            raise HTTPException(404, "mix not found")
        for tid in body.track_ids:
            if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone():
                raise HTTPException(400, f"track {tid} does not exist")
        conn.execute("DELETE FROM mix_items WHERE mix_id=?", (mix_id,))
        for pos, tid in enumerate(body.track_ids):
            conn.execute(
                "INSERT INTO mix_items (mix_id, track_id, position) VALUES (?,?,?)",
                (mix_id, tid, pos),
            )
        return mix_detail(conn, mix_id)


@app.patch("/api/mix-items/{item_id}")
def patch_mix_item(item_id: int, body: MixItemPatch):
    with db() as conn:
        row = conn.execute("SELECT * FROM mix_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "mix item not found")
        conn.execute("UPDATE mix_items SET music_idx=? WHERE id=?",
                     (body.music_index, item_id))
        return mix_detail(conn, row["mix_id"])


@app.delete("/api/mixes/{mix_id}")
def delete_mix(mix_id: int):
    with db() as conn:
        conn.execute("DELETE FROM mixes WHERE id=?", (mix_id,))
    return {"ok": True}


@app.patch("/api/mixes/{mix_id}")
def patch_mix(mix_id: int, body: MixPatch):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM mixes WHERE id=?", (mix_id,)).fetchone():
            raise HTTPException(404, "mix not found")
        if body.name is not None:
            conn.execute("UPDATE mixes SET name=? WHERE id=?", (body.name, mix_id))
        if body.audio is not None:
            if body.audio not in (0, 1):
                raise HTTPException(400, "audio must be 0 or 1")
            conn.execute("UPDATE mixes SET audio=? WHERE id=?", (body.audio, mix_id))
        return mix_detail(conn, mix_id)


# ---------------------------------------------------------------- export

def export_write(sql: str, params: tuple, timeout: float = 60.0) -> int | None:
    """Retry a write while another connection holds the lock. The export worker
    runs in a thread with nobody to catch its exceptions, so a failed write must
    come back as None rather than propagate. Returns the number of rows matched:
    0 means the export row is gone, i.e. the user deleted it under us."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with db() as conn:
                return conn.execute(sql, params).rowcount
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)


def run_export(export_id: str, mix: dict, mode: str):
    tmp_dir = scratch_dir(export_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    seg_files = []
    seg_durs = []
    n = len(mix["items"])
    try:
        with db() as conn:
            rels = {
                i["release_id"]: conn.execute(
                    "SELECT * FROM releases WHERE id=?", (i["release_id"],)
                ).fetchone()
                for i in mix["items"]
            }
        want_audio = mix.get("audio") or 0
        astreams: dict[int, int] = {}
        for idx, item in enumerate(mix["items"]):
            rel = rels[item["release_id"]]
            if rel is None:
                # purged (or cascade-deleted) between queueing and running: the
                # mix snapshot still names it, the releases row is gone
                raise RuntimeError(
                    f"release of '{item['name']}' was removed before the export started"
                )
            src = release_path(rel)
            if not src.is_file():
                raise RuntimeError(f"source file missing: {src.name}")
            seg = tmp_dir / f"seg_{idx:03d}.ts"
            dur = item["end"] - item["start"]
            # honour the mix's audio-track choice on files that have that
            # track; files with a single audio stream keep their default
            maps = []
            if want_audio:
                rid = item["release_id"]
                if rid not in astreams:
                    astreams[rid] = audio_stream_count(src)
                if want_audio < astreams[rid]:
                    maps = ["-map", "0:v:0", "-map", f"0:a:{want_audio}"]
            if mode == "precise":
                cmd = [
                    FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
                    "-y", "-ss", str(item["start"]), "-i", str(src),
                    "-t", str(dur),
                    "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    *maps,
                    str(seg),
                ]
            else:
                kf = keyframe_before(src, item["start"])
                cmd = [
                    FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
                    "-y", "-ss", str(kf), "-i", str(src),
                    "-t", str(item["end"] - kf), "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    *maps,
                    str(seg),
                ]
            proc = _run_subprocess(
                cmd, capture_output=True, text=True, timeout=MEDIA_TIMEOUT)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg failed on '{item['name']}': {proc.stderr[-800:]}"
                )
            if not seg.is_file() or seg.stat().st_size == 0:
                raise RuntimeError(
                    f"'{item['name']}' ({item['start']}-{item['end']}) produced an empty "
                    "segment; ffmpeg exits 0 for those and concat then skips them"
                )
            seg_files.append(seg)
            seg_durs.append(probe_duration(seg) or dur)
            # a dropped progress tick is cosmetic; never fail the export over it.
            # No row to tick means the user deleted the export: that is how a
            # running one is cancelled, so stop here and let finally clean up.
            if export_write("UPDATE exports SET progress=? WHERE id=?",
                            (0.9 * (idx + 1) / n, export_id), timeout=0) == 0:
                return
        concat_list = tmp_dir / "list.txt"
        concat_list.write_text(
            "".join(f"file '{s.as_posix()}'\n" for s in seg_files)
        )
        meta_file = tmp_dir / "meta.txt"
        lines = [";FFMETADATA1"]
        t0 = 0.0
        for item, d in zip(mix["items"], seg_durs):
            title = re.sub(r"[=;#\\\n]", " ", item["name"])
            lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                      f"START={int(t0 * 1000)}", f"END={int((t0 + d) * 1000)}",
                      f"title={title}"]
            t0 += d
        meta_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        safe_name = re.sub(r"[^\w\-]+", "_", mix["name"]).strip("_") or "mix"
        out_name = f"{safe_name}_{export_id[:8]}.mp4"
        out_path = export_path(out_name)
        cmd = [
            FFMPEG_COMMAND, "-nostdin", "-protocol_whitelist", "file,pipe",
            "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", str(meta_file), "-map_metadata", "1",
            "-c", "copy", "-movflags", "+faststart", str(out_path),
        ]
        proc = _run_subprocess(
            cmd, capture_output=True, text=True, timeout=MEDIA_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr[-800:]}")
        # concat exits 0 even when it silently drops a segment it cannot open, and
        # everything after it. Only a *short* result is suspicious: fast mode snaps
        # -ss back to a keyframe, so its segments legitimately run longer.
        want = sum(i["end"] - i["start"] for i in mix["items"])
        got = probe_duration(out_path)
        if got < want - (1 + 0.5 * n):
            out_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"export is {got:.1f}s but the mix is {want:.1f}s — "
                "segments were dropped or came out short"
            )
        recorded = export_write(
            # clears the error another process's startup cleanup may have written
            # onto this row while we were still working
            "UPDATE exports SET status='done', progress=1, error=NULL, filename=? "
            "WHERE id=?",
            (out_name, export_id),
        )
        if recorded == 0:
            # deleted while we were concatenating — nothing can reach this file
            # through the API any more, so don't leave it on disk
            out_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        recorded = export_write(
            "UPDATE exports SET status='error', error=? WHERE id=?",
            (str(exc), export_id),
        )
    finally:
        # must not raise whatever became of the directory: a delete or another
        # process's startup sweep may have taken it while we were working
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if recorded is None:
        print(f"export {export_id}: database locked, terminal status not recorded")


@app.post("/api/mixes/{mix_id}/export")
def export_mix(mix_id: int, body: ExportIn):
    if body.mode not in ("fast", "precise"):
        raise HTTPException(400, "mode must be 'fast' or 'precise'")
    with db() as conn:
        mix = mix_detail(conn, mix_id)
    if not mix["items"]:
        raise HTTPException(400, "mix has no tracks")
    export_id = uuid.uuid4().hex
    with EXPORT_QUEUE_LOCK:
        if EXPORT_QUEUE.full():
            raise HTTPException(429, "export queue is full; try again later")
        with db() as conn:
            conn.execute(
                "INSERT INTO exports (id, mix_id, mix_name, mode, status, created_at)"
                " VALUES (?,?,?,?, 'queued', ?)",
                (export_id, mix_id, mix["name"], body.mode, time.time()),
            )
        EXPORT_QUEUE.put_nowait((export_id, mix, body.mode))
    return {"export_id": export_id}


@app.get("/api/exports")
def list_exports():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM exports ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/exports/{export_id}")
def export_status(export_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
    if not row:
        raise HTTPException(404, "export not found")
    return dict(row)


@app.get("/api/exports/{export_id}/download")
def export_download(export_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
    if not row or row["status"] != "done" or not row["filename"]:
        raise HTTPException(404, "export not ready")
    path = export_path(row["filename"])
    if not path.is_file():
        raise HTTPException(404, "export file missing")
    return FileResponse(path, media_type="video/mp4", filename=row["filename"])


@app.delete("/api/exports/{export_id}")
def delete_export(export_id: str):
    tmp_dir = scratch_dir(export_id)  # validate before anything touches the disk
    with db() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if row and row["filename"]:
            export_path(row["filename"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM exports WHERE id=?", (export_id,))
    # dropping the row is also the cancel signal: run_export finds its next
    # UPDATE matching nothing, gives up and clears its own scratch dir. Pulling
    # that dir out from under a live ffmpeg instead only orphans the output.
    if not row or row["status"] != "running":
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"ok": True}


# a single worker drains this queue so exports run one at a time — two ffmpeg
# encodes in parallel melt the NAS CPU. New exports enqueue as 'queued'; the
# worker flips each to 'running' only once it actually starts on it.
EXPORT_QUEUE: "queue.Queue[tuple[str, dict, str]]" = queue.Queue(maxsize=16)
EXPORT_QUEUE_LOCK = threading.Lock()


def _export_worker():
    while True:
        export_id, mix, mode = EXPORT_QUEUE.get()
        try:
            with db() as conn:
                row = conn.execute("SELECT status FROM exports WHERE id=?",
                                   (export_id,)).fetchone()
            if not row or row["status"] != "queued":
                continue  # deleted (cancelled) while waiting
            if export_write("UPDATE exports SET status='running' WHERE id=?",
                            (export_id,)) is None:
                print(f"export {export_id}: could not mark running (database locked)")
            run_export(export_id, mix, mode)
        except Exception as exc:  # noqa: BLE001
            # nothing may escape: this one thread is the whole export pipeline
            print(f"export worker: {export_id} failed outside run_export: {exc}")
            export_write("UPDATE exports SET status='error', error=? WHERE id=?",
                         (str(exc), export_id))


threading.Thread(target=_export_worker, daemon=True).start()
threading.Thread(target=_job_worker, daemon=True).start()

# ---------------------------------------------------------------- static UI

static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
