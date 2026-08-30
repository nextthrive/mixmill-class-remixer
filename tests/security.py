"""Targeted fail-closed security checks for MixMill.

Run inside the production image so Linux mount/path semantics match deployment.
The suite creates only temporary fixtures.
"""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"
USER = "security"
PASSWORD = "x"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok: {message}")


def request(method, path, body=None, *, auth=True, mutation=True, raw_body=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = AUTH
    if mutation:
        headers["X-MixMill-Request"] = "1"
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(BASE + path, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read()
        return response.status, {k.lower(): v for k, v in response.headers.items()}, (
            json.loads(payload) if payload else None
        )


def expect_http(status, method, path, **kwargs):
    try:
        request(method, path, **kwargs)
    except urllib.error.HTTPError as exc:
        check(exc.code == status, f"{method} {path} returns {status}")
        return
    raise AssertionError(f"{method} {path} unexpectedly succeeded")


def import_probe(videos, data, **extra):
    env = {
        **os.environ,
        "VIDEO_DIR": str(videos),
        "DATA_DIR": str(data),
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mixmill-security-"))

    # Authentication and read-only media are mandatory by default.
    videos = tmp / "startup-videos"
    data = tmp / "startup-data"
    videos.mkdir()
    data.mkdir()
    proc = import_probe(videos, data, MIXMILL_REQUIRE_READ_ONLY="0")
    check(proc.returncode != 0 and "MIXMILL_USERNAME" in proc.stderr,
          "startup fails without authentication configuration")

    proc = import_probe(
        videos, data, MIXMILL_USERNAME=USER, MIXMILL_PASSWORD=PASSWORD,
    )
    check(proc.returncode != 0 and "read-only" in proc.stderr.lower(),
          "startup fails when media is writable")

    foreign_data = tmp / "foreign-data"
    foreign_data.mkdir()
    (foreign_data / "family-photo.jpg").write_text("do not touch", encoding="utf-8")
    proc = import_probe(
        videos, foreign_data, MIXMILL_USERNAME=USER, MIXMILL_PASSWORD=PASSWORD,
        MIXMILL_REQUIRE_READ_ONLY="0",
    )
    check(proc.returncode != 0 and "dedicated" in proc.stderr.lower(),
          "startup refuses a non-dedicated data directory")
    check((foreign_data / "family-photo.jpg").read_text(encoding="utf-8") == "do not touch",
          "foreign data remains untouched")

    # Boot a real authenticated server against disposable media.
    live = tmp / "live"
    live_videos, live_data, outside = live / "videos", live / "data", live / "outside"
    live_videos.mkdir(parents=True)
    live_data.mkdir()
    outside.mkdir()
    (live_videos / "inside.mp4").write_bytes(b"not a real video")
    (outside / "outside.mp4").write_bytes(b"private")
    (live_videos / "escape.mp4").symlink_to(outside / "outside.mp4")
    (outside / "insideChoreographyNotes.pdf").write_bytes(b"private notes")
    (live_videos / "insideChoreographyNotes.pdf").symlink_to(
        outside / "insideChoreographyNotes.pdf"
    )
    env = {
        **os.environ,
        "VIDEO_DIR": str(live_videos),
        "DATA_DIR": str(live_data),
        "MIXMILL_USERNAME": USER,
        "MIXMILL_PASSWORD": PASSWORD,
        "MIXMILL_REQUIRE_READ_ONLY": "0",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(60):
            if server.poll() is not None:
                raise AssertionError(f"security server exited: {server.stderr.read()}")
            try:
                status, _, _ = request("GET", "/api/health", auth=False)
                if status == 200:
                    break
            except urllib.error.URLError:
                time.sleep(0.25)
        else:
            raise AssertionError("security server did not become ready")

        expect_http(401, "GET", "/api/releases", auth=False)
        status, headers, _ = request("GET", "/api/releases")
        check(status == 200, "authenticated request succeeds")
        check(headers.get("x-content-type-options") == "nosniff",
              "security headers are present")
        expect_http(403, "POST", "/api/scan", mutation=False)

        _, _, scan = request("POST", "/api/scan")
        check(scan["found"] == 1, "scanner excludes symlinks escaping the media root")
        _, _, releases = request("GET", "/api/releases")
        release_id = releases[0]["id"]

        _, _, track = request("POST", f"/api/releases/{release_id}/tracks", body={
            "name": "01 Warmup", "start": 0, "end": 1,
        })
        _, _, mix = request("POST", "/api/mixes", body={"name": "Path probe"})
        request("PUT", f"/api/mixes/{mix['id']}/items", body={
            "track_ids": [track["id"]],
        })
        _, _, status = request(
            "GET", f"/api/mixes/{mix['id']}/choreography-notes/status"
        )
        check(not status["ready"], "choreography notes cannot follow outside symlinks")
        expect_http(
            404, "GET", f"/api/releases/{release_id}/choreography-notes/source"
        )

        expect_http(422, "POST", f"/api/releases/{release_id}/tracks",
                    body={"name": "x" * 300, "start": 0, "end": 1})
        expect_http(422, "POST", f"/api/releases/{release_id}/tracks",
                    body={"name": "ok", "start": 0, "end": 1, "surprise": True})
        expect_http(413, "POST", f"/api/releases/{release_id}/tracks",
                    raw_body=b" " * (1024 * 1024 + 1))

        with sqlite3.connect(live_data / "mixmill.db") as conn:
            conn.execute(
                "INSERT INTO releases (relpath, title, program, duration, added_at) "
                "VALUES ('../outside/outside.mp4', 'escape', '', 1, ?)",
                (time.time(),),
            )
            escape_id = conn.execute(
                "SELECT id FROM releases WHERE title='escape'"
            ).fetchone()[0]
        expect_http(404, "GET", f"/api/stream/{escape_id}")
        check((outside / "outside.mp4").read_bytes() == b"private",
              "out-of-root file remains untouched")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    print("SECURITY PASS")


if __name__ == "__main__":
    main()
