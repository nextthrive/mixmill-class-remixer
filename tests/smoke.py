"""End-to-end smoke test: boots the real app against synthetic fixtures.

Usage: python tests/smoke.py
Needs ffmpeg/ffprobe on PATH. Creates its own temp video/data dirs.
"""
import json
import os
import re
import base64
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
AUTH_USER = "smoke"
AUTH_PASSWORD = "smoke-test-password"
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{AUTH_USER}:{AUTH_PASSWORD}".encode()
).decode()
TEST_ENV = {
    "MIXMILL_USERNAME": AUTH_USER,
    "MIXMILL_PASSWORD": AUTH_PASSWORD,
    "MIXMILL_REQUIRE_READ_ONLY": "0",
}


# run_export() in-process against a mix whose release row no longer exists.
# Prints "<status>|<error>" from the exports row it wrote.
GHOST_EXPORT_SRC = """
import sys, time
sys.path.insert(0, sys.argv[1])
from app import main as m

eid = "0" * 32  # uuid4().hex shape, which is all the id validator wants
with m.db() as conn:
    conn.execute(
        "INSERT INTO exports (id, mix_id, mix_name, mode, status, created_at)"
        " VALUES (?,?,?,?,?,?)", (eid, 1, "ghost", "fast", "running", time.time()))
m.run_export(eid, {"name": "ghost", "audio": 0,
                   "items": [{"release_id": 999999, "name": "Ghost Track",
                              "start": 0.0, "end": 1.0}]}, "fast")
with m.db() as conn:
    row = conn.execute("SELECT status, error FROM exports WHERE id=?", (eid,)).fetchone()
print(row["status"] + "|" + (row["error"] or ""))
"""


def req(method, path, body=None, raw=False, timeout=180):
    r = urllib.request.Request(BASE + path, method=method,
                               headers={"Content-Type": "application/json",
                                        "Authorization": AUTH_HEADER,
                                        "X-MixMill-Request": "1"},
                               data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        data = resp.read()
        return data if raw else (json.loads(data) if data else None)


def check(cond, msg):
    if not cond:
        sys.exit(f"SMOKE FAIL: {msg}")
    print(f"  ok: {msg}")


def wait_job(job_id, timeout=300):
    """Poll a background job until done; returns its result dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = req("GET", f"/api/jobs/{job_id}")
        if j["status"] == "done":
            return j["result"]
        if j["status"] == "error":
            sys.exit(f"SMOKE FAIL: job {job_id} errored: {j['error']}")
        time.sleep(1)
    sys.exit(f"SMOKE FAIL: job {job_id} timed out")


def check_legacy_curation_migration():
    """Pre-curation databases with saved tracks keep their existing pool."""
    tmp = Path(tempfile.mkdtemp(prefix="mixmill-legacy-"))
    videos, data = tmp / "videos", tmp / "data"
    videos.mkdir()
    data.mkdir()
    conn = sqlite3.connect(data / "mixmill.db")
    conn.executescript("""
        CREATE TABLE releases (
            id INTEGER PRIMARY KEY, relpath TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, program TEXT DEFAULT '', duration REAL DEFAULT 0,
            missing INTEGER DEFAULT 0, vaulted INTEGER DEFAULT 0, added_at REAL
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, release_id INTEGER NOT NULL,
            name TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
            position INTEGER DEFAULT 0
        );
        INSERT INTO releases (id, relpath, title) VALUES (1, 'old.mp4', 'OLD 1');
        INSERT INTO tracks (release_id, name, start, end) VALUES (1, '01 Warmup', 0, 60);
    """)
    conn.commit()
    conn.close()
    code = """
from app import main as m
with m.db() as conn:
    release = conn.execute('SELECT curated FROM releases WHERE id=1').fetchone()
    track = conn.execute('SELECT rejected FROM tracks WHERE id=1').fetchone()
print(f'{release[0]}|{track[0]}')
"""
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT,
        env={**os.environ, **TEST_ENV,
             "VIDEO_DIR": str(videos), "DATA_DIR": str(data)},
        capture_output=True, text=True,
    )
    check(proc.returncode == 0 and proc.stdout.strip().endswith("1|0"),
          "legacy saved tracks migrate to Curated")


def main():
    check_legacy_curation_migration()
    tmp = Path(tempfile.mkdtemp(prefix="mixmill-smoke-"))
    videos, data = tmp / "videos", tmp / "data"
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixtures.py"),
                    str(videos)], check=True)
    env = {**os.environ, **TEST_ENV,
           "VIDEO_DIR": str(videos), "DATA_DIR": str(data)}

    def start_server():
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=ROOT, env=env)

    def wait_ready(proc):
        for _ in range(60):
            if proc.poll() is not None:
                sys.exit(f"SMOKE FAIL: server exited early (code {proc.returncode})")
            try:
                req("GET", "/api/releases")
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.5)
        sys.exit("SMOKE FAIL: server never came up")

    server = start_server()
    try:
        wait_ready(server)

        # --- scan & natural sort
        res = req("POST", "/api/scan")
        check(res["found"] == 3 and res["added"] == 3, f"scan found 3 ({res})")
        rels = req("GET", "/api/releases")
        titles = [r["title"] for r in rels]
        check(titles == ["COMBAT 2", "COMBAT 10", "PUMP 5"],
              f"natural sort order ({titles})")
        by_title = {r["title"]: r for r in rels}
        c2, c10, p5 = (by_title[t] for t in ("COMBAT 2", "COMBAT 10", "PUMP 5"))

        # --- music listing
        m = req("GET", f"/api/releases/{c2['id']}/music")
        check([x["name"] for x in m] == ["01 Warmup", "02 Power", "03 Cooldown"],
              f"music list for COMBAT 2 ({[x['name'] for x in m]})")
        check(req("GET", f"/api/releases/{p5['id']}/music") == [],
              "PUMP 5 has no music")
        body = req("GET", f"/api/releases/{c2['id']}/music/0", raw=True)
        check(len(body) > 10000, "music file streams")

        # --- auto-tracks: chapters release (job-aware for later tasks)
        r = req("POST", f"/api/releases/{c2['id']}/auto-tracks")
        if "job_id" in r:
            r = wait_job(r["job_id"])
        check(r["method"] == "chapters" and r["imported"] == 3,
              f"COMBAT 2 chapters import ({r})")
        # --- auto-tracks: correlation release
        r = req("POST", f"/api/releases/{c10['id']}/auto-tracks")
        if "job_id" in r:
            r = wait_job(r["job_id"])
        check(r["method"] == "music" and r["imported"] == 3,
              f"COMBAT 10 music match ({r})")
        det = req("GET", f"/api/releases/{c10['id']}")
        starts = sorted(t["start"] for t in det["tracks"])
        for got, want in zip(starts, [0, 8, 16]):
            check(abs(got - want) < 1.5, f"match offset {got:.2f} ~= {want}")

        # --- track CRUD
        t = req("POST", f"/api/releases/{p5['id']}/tracks",
                {"name": "01 Manual", "start": 1.0, "end": 9.0})
        t = req("PATCH", f"/api/tracks/{t['id']}", {"end": 10.0})
        check(t["end"] == 10.0, "track patch")
        p5_track_id = t["id"]

        # --- mix + music pairing
        c2_tracks = req("GET", f"/api/releases/{c2['id']}")["tracks"]
        mix = req("POST", "/api/mixes", {"name": "Smoke Mix"})
        mix = req("PUT", f"/api/mixes/{mix['id']}/items",
                  {"track_ids": [c2_tracks[0]["id"], c2_tracks[1]["id"]]})
        check(mix["items"][0]["music_index"] == 0
              and mix["items"][0]["music_name"] == "01 Warmup",
              "music auto-paired by number")

        # --- voice audio extraction
        body = req("GET", f"/api/releases/{c2['id']}/audio?track=1", raw=True)
        check(len(body) > 10000, "second audio track extracts")

        # --- export (fast)
        ex = req("POST", f"/api/mixes/{mix['id']}/export", {"mode": "fast"})
        for _ in range(120):
            st = req("GET", f"/api/exports/{ex['export_id']}")
            if st["status"] in ("done", "error"):
                break
            time.sleep(1)
        check(st["status"] == "done", f"fast export done ({st.get('error')})")
        out = req("GET", f"/api/exports/{ex['export_id']}/download", raw=True)
        check(len(out) > 50000, "export downloads")

        extra_checks(mix, c2, c10, p5, data, videos, p5_track_id)

        # Task 2: restarting with an existing DB must snapshot it
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server = start_server()
        wait_ready(server)
        snaps = list((data / "backups").glob("mixmill-*.db"))
        check(len(snaps) >= 1, "startup snapshot of existing DB")

        print("SMOKE PASS")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def extra_checks(mix, c2, c10, p5, data, videos, p5_track_id):
    """Later tasks append their assertions here."""
    # Task 2: backups dir exists (created at startup)
    backups = data / "backups"
    check(backups.is_dir(), "backups directory created")

    # Task 3: back-to-back exports must serialize but both finish
    e1 = req("POST", f"/api/mixes/{mix['id']}/export", {"mode": "precise"})
    e2 = req("POST", f"/api/mixes/{mix['id']}/export", {"mode": "fast"})
    st2 = req("GET", f"/api/exports/{e2['export_id']}")
    check(st2["status"] in ("queued", "running"), f"second export queues ({st2['status']})")
    both = [req("GET", f"/api/exports/{e['export_id']}")["status"] for e in (e1, e2)]
    check(both.count("running") <= 1, f"never two running ({both})")
    for _ in range(180):
        s1 = req("GET", f"/api/exports/{e1['export_id']}")
        s2 = req("GET", f"/api/exports/{e2['export_id']}")
        if s1["status"] == "done" and s2["status"] == "done":
            break
        time.sleep(1)
    check(s1["status"] == "done" and s2["status"] == "done", "both exports finish")

    # Task 7: exported mp4 has named chapters
    out_file = Path(tempfile.gettempdir()) / "mixmill_smoke_export.mp4"
    out_file.write_bytes(req("GET", f"/api/exports/{e1['export_id']}/download", raw=True))
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(out_file)],
        capture_output=True, text=True)
    chapters = json.loads(probe.stdout or "{}").get("chapters", [])
    names = [(c.get("tags") or {}).get("title") for c in chapters]
    check(names == ["01 Warmup", "02 Power"], f"export chapters ({names})")
    out_file.unlink(missing_ok=True)

    # Task 8: songs zip
    import io
    import zipfile as zf
    blob = req("GET", f"/api/mixes/{mix['id']}/songs.zip", raw=True)
    names = zf.ZipFile(io.BytesIO(blob)).namelist()
    check(names == ["01 01 Warmup.m4a", "02 02 Power.m4a"], f"zip entries ({names})")

    package_blob = req(
        "GET", f"/api/mixes/{mix['id']}/package.zip?export_id={e1['export_id']}", raw=True
    )
    package = zf.ZipFile(io.BytesIO(package_blob))
    package_names = package.namelist()
    check(
        package_names == [
            "video/Smoke_Mix.mp4",
            "study/Smoke_Mix_choreography_notes.pdf",
            "music/01 01 Warmup.m4a",
            "music/02 02 Power.m4a",
            "README.txt",
        ],
        f"complete package includes video, PDF, and music ({package_names})",
    )
    package_readme = package.read("README.txt").decode()
    check("Video: included (precise export)" in package_readme
          and "Study PDF: included (2/2 tracks mapped)" in package_readme,
          "complete package describes coverage")

    # Study PDF: source pages follow mix order across releases; a release with
    # no notes produces partial coverage without blocking available pages.
    from pypdf import PdfReader
    c2_tracks = req("GET", f"/api/releases/{c2['id']}")["tracks"]
    c10_tracks = req("GET", f"/api/releases/{c10['id']}")["tracks"]
    notes_mix = req("POST", "/api/mixes", {"name": "Cross Release Study"})
    notes_mix = req("PUT", f"/api/mixes/{notes_mix['id']}/items", {
        "track_ids": [c2_tracks[0]["id"], c10_tracks[1]["id"], p5_track_id],
    })
    status = req("GET", f"/api/mixes/{notes_mix['id']}/choreography-notes/status")
    check(status["ready"] and not status["complete"]
          and status["matched_tracks"] == 2 and status["total_tracks"] == 3,
          f"study notes report partial coverage ({status})")
    blob = req("GET", f"/api/mixes/{notes_mix['id']}/choreography-notes", raw=True)
    notes = PdfReader(io.BytesIO(blob))
    check(len(notes.pages) == 5, f"study PDF has cover plus four source pages ({len(notes.pages)})")
    cover_text = notes.pages[0].extract_text() or ""
    check("Cross Release Study" in cover_text and "NOT FOUND" in cover_text,
          "study PDF cover lists mix and missing notes")
    source_text = "\n".join((page.extract_text() or "") for page in notes.pages[1:])
    check(source_text.index("01. WARMUP") < source_text.index("02. POWER"),
          "study PDF source pages follow mix order")
    check(len(notes.outline) == 2, "study PDF has clickable track bookmarks")
    links = [annotation.get_object() for annotation in notes.pages[0].get("/Annots", [])]
    check(len(links) == 2 and all(link.get("/Subtype") == "/Link" for link in links),
          "study PDF cover rows link to source sections")

    # V2 Mapping Studio API: automatic mappings, image previews, persistent
    # manual/disabled choices, and stale review after the source PDF changes.
    release_notes = req("GET", f"/api/releases/{c2['id']}/choreography-notes")
    check(release_notes["available"] and release_notes["mapped_tracks"] == 3,
          f"release notes map all tracks ({release_notes['mapped_tracks']}/3)")
    first_mapping = release_notes["tracks"][0]
    check(first_mapping["mapping_mode"] == "auto" and first_mapping["options"],
          "release notes expose automatic group choices")
    third_options = {option["key"] for option in release_notes["tracks"][2]["options"]}
    check(third_options == {"3", "3A"},
          f"release notes expose sibling alternative variants ({sorted(third_options)})")
    bonus_track = req("POST", f"/api/releases/{c2['id']}/tracks", {
        "name": "B5 Bonus", "start": 0, "end": 8,
    })
    bonus_notes = req("GET", f"/api/releases/{c2['id']}/choreography-notes")
    bonus_mapping = next(
        row for row in bonus_notes["tracks"] if row["track_id"] == bonus_track["id"]
    )
    check(bonus_mapping["matched"] and {option["key"] for option in bonus_mapping["options"]} == {"5B"},
          "B5 bonus heading maps to numbered slot 5")
    req("DELETE", f"/api/tracks/{bonus_track['id']}?permanent=1")
    source = req(
        "GET", f"/api/releases/{c2['id']}/choreography-notes/source", raw=True
    )
    check(source[:5] == b"%PDF-" and len(source) > 1000,
          "source choreography PDF opens for instructor review")
    preview = req(
        "GET", f"/api/releases/{c2['id']}/choreography-notes/pages/2", raw=True
    )
    check(preview[:8] == b"\x89PNG\r\n\x1a\n" and len(preview) > 1000,
          "choreography source page renders as inline preview")
    manual = req("PUT", f"/api/tracks/{c2_tracks[0]['id']}/choreography-mapping", {
        "mode": "manual", "page_start": 4, "page_end": 5,
    })
    manual_row = next(row for row in manual["tracks"] if row["track_id"] == c2_tracks[0]["id"])
    check(manual_row["mapping_mode"] == "manual"
          and manual_row["source_pages"] == [4, 5] and not manual_row["stale"],
          "manual choreography page range persists")
    mapped_mix = req("GET", f"/api/mixes/{notes_mix['id']}/choreography-notes/status")
    mapped_item = next(item for item in mapped_mix["items"]
                       if item["track_id"] == c2_tracks[0]["id"])
    check(mapped_item["mapping_mode"] == "manual" and mapped_item["source_pages"] == [4, 5],
          "mix rows use saved manual notes mapping")
    source_pdf = next((videos / "COMBAT" / "COMBAT 2").glob("*ChoreographyNotes.pdf"))
    source_stat = source_pdf.stat()
    os.utime(source_pdf, (source_stat.st_atime + 2, source_stat.st_mtime + 2))
    rescanned = req("POST", f"/api/releases/{c2['id']}/choreography-notes/rescan")
    stale_row = next(row for row in rescanned["tracks"] if row["track_id"] == c2_tracks[0]["id"])
    check(stale_row["mapping_mode"] == "manual" and stale_row["stale"]
          and stale_row["source_pages"] == [4, 5],
          "source change preserves manual range and flags review")
    disabled = req("PUT", f"/api/tracks/{c2_tracks[0]['id']}/choreography-mapping",
                   {"mode": "disabled"})
    disabled_row = next(row for row in disabled["tracks"] if row["track_id"] == c2_tracks[0]["id"])
    check(not disabled_row["matched"] and disabled_row["mapping_mode"] == "disabled",
          "track notes can be explicitly disabled")
    automatic = req("PUT", f"/api/tracks/{c2_tracks[0]['id']}/choreography-mapping",
                    {"mode": "auto"})
    automatic_row = next(row for row in automatic["tracks"] if row["track_id"] == c2_tracks[0]["id"])
    check(automatic_row["matched"] and automatic_row["mapping_mode"] == "auto"
          and not automatic_row["stale"], "automatic mapping can be restored")

    # Task 9: duplicate mix
    dup = req("POST", f"/api/mixes/{mix['id']}/duplicate")
    check(dup["name"] == "Smoke Mix copy" and len(dup["items"]) == len(mix["items"]),
          "duplicate copies items")
    check([i["track_id"] for i in dup["items"]] == [i["track_id"] for i in mix["items"]],
          "duplicate keeps order")
    check(dup["items"][0]["music_index"] == mix["items"][0]["music_index"],
          "duplicate keeps song pairing")

    # Task 10: smart mix — program mode (COMBAT: 2 reviewed releases x 3 slots)
    g = req("POST", "/api/mixes/generate",
            {"mode": "program", "program": "COMBAT", "minutes": 10,
             "source_pool": "discovery"})
    slots = [i["name"][:2] for i in g["items"]]
    check(slots == sorted(set(slots)), f"one track per slot, ordered ({slots})")
    check(len(g["items"]) == 3, f"3 slots picked ({len(g['items'])})")
    # any mode
    g2 = req("POST", "/api/mixes/generate",
             {"mode": "any", "minutes": 10, "source_pool": "discovery"})
    check(len(g2["items"]) >= 1, "surprise mode returns tracks")
    # validation
    try:
        req("POST", "/api/mixes/generate", {"mode": "program", "minutes": 60})
        check(False, "missing program rejected")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "missing program rejected")

    # Task 5: duplicate auto-detect requests coalesce to one job
    a = req("POST", f"/api/releases/{c10['id']}/auto-tracks")
    b = req("POST", f"/api/releases/{c10['id']}/auto-tracks")
    check("job_id" in a and a["job_id"] == b["job_id"], "duplicate job coalesced")
    wait_job(a["job_id"])

    # Task 6: bulk auto-detect covers remaining zero-track releases.
    # Drop PUMP 5's manual track first: with no chapters and no music folder
    # its detection must FAIL, proving per-release failures are recorded
    # without killing the job.
    req("DELETE", f"/api/tracks/{p5_track_id}?permanent=1")
    r = req("POST", "/api/auto-tracks-all")
    res = wait_job(r["job_id"])
    check(res["releases"] == 1 and res["imported"] == 0 and len(res["failed"]) == 1,
          f"bulk ran, failure recorded ({res})")
    check("PUMP 5" in res["failed"][0], f"failure names the release ({res['failed']})")

    # Task 12: PWA assets served
    man = json.loads(req("GET", "/manifest.webmanifest", raw=True))
    check(man["name"] == "MixMill", "manifest served")
    icon = req("GET", "/icon.svg", raw=True)
    check(len(icon) > 100 and b"<polyline" in icon,
          "font-independent logo icon served")
    check(len(req("GET", "/fonts/Anton-Regular.ttf", raw=True)) > 100_000,
          "local display font served")
    for name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        blob = req("GET", f"/{name}", raw=True)
        check(blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) > 500, f"{name} served")
    srcs = {i["src"] for i in man["icons"]}
    check({"/icon-192.png", "/icon-512.png"} <= srcs, f"manifest lists PNG icons ({srcs})")
    head = req("GET", "/index.html", raw=True).decode()
    check('rel="apple-touch-icon" href="/apple-touch-icon.png"' in head,
          "index links the apple-touch-icon")
    check('id="btn-notes-source"' in head and 'id="btn-notes-browse"' in head
          and 'id="notes-preview-page"' in head and 'id="btn-notes-issues"' in head,
          "mapping studio exposes full-PDF browse and issue-review controls")
    check('id="btn-package"' in head and "Build your own mix" in head
          and "Create empty mix" in head,
          "mix builder separates manual creation and complete package")

    # a release purged between queueing an export and the worker starting it
    # must fail with a readable message, not a raw TypeError from release_path.
    # Runs against its own DATA_DIR so importing the app cannot disturb the
    # server under test (import time rewrites running/queued exports).
    ghost_data = data.parent / "ghost-data"
    out = subprocess.run(
        [sys.executable, "-c", GHOST_EXPORT_SRC, str(ROOT)],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, **TEST_ENV,
             "DATA_DIR": str(ghost_data), "VIDEO_DIR": str(videos)})
    check(out.returncode == 0, f"ghost-release export probe ran ({out.stderr[-400:]})")
    status, _, err = out.stdout.strip().splitlines()[-1].partition("|")
    check(status == "error", f"ghost-release export fails ({status})")
    check("was removed before the export started" in err and "TypeError" not in err,
          f"ghost-release error is readable ({err})")

    v3_checks(mix, c2, c10, p5, data)

    # Task 4: purge missing (LAST — deletes a fixture file)
    t2 = req("POST", f"/api/releases/{p5['id']}/tracks",
             {"name": "02 Cascade Probe", "start": 2.0, "end": 8.0})
    victim = next(videos.rglob("PUMP5.mp4"))
    victim.unlink()
    req("POST", "/api/scan")
    rels = req("GET", "/api/releases")
    check(any(r["missing"] for r in rels), "PUMP 5 flagged missing")
    res = req("DELETE", "/api/releases/missing?confirm=true")
    check(res["deleted"] == 1, f"purged 1 ({res})")
    rels = req("GET", "/api/releases")
    check(all(not r["missing"] for r in rels) and len(rels) == 2, "ghost gone")

    try:
        req("PATCH", f"/api/tracks/{t2['id']}", {"end": 11.0})
        check(False, "purged release's track gone (cascade)")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "purged release's track gone (cascade)")


def v3_checks(mix, c2, c10, p5, data):
    """v3 assertions. Runs before the destructive purge block."""
    # Task 1 (v3): migration fields present and PATCHable
    rels = req("GET", "/api/releases")
    check(all("vaulted" in r and "curated" in r and "rejected_count" in r
              and "added_at" in r for r in rels),
          "releases carry vault/curation fields")
    check(all(r["vaulted"] == 0 for r in rels), "vaulted defaults to 0")
    check(all(r["curated"] == 0 for r in rels),
          "newly detected releases remain Discovery")
    check(all(isinstance(r["added_at"], float) and r["added_at"] > 0 for r in rels),
          "added_at populated")
    r = req("PATCH", f"/api/releases/{c10['id']}", {"vaulted": 1})
    check(r["vaulted"] == 1, "vault flag set")
    r = req("PATCH", f"/api/releases/{c10['id']}", {"vaulted": 0})
    check(r["vaulted"] == 0, "vault flag cleared")
    try:
        req("PATCH", f"/api/releases/{c10['id']}", {"vaulted": 2})
        check(False, "vaulted=2 rejected")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "vaulted=2 rejected")
    try:
        req("PATCH", f"/api/releases/{c10['id']}", {"curated": 2})
        check(False, "curated=2 rejected")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "curated=2 rejected")

    # Task 2 (v3): poster thumbnail, cached on second hit
    for label in ("generated", "cached"):
        blob = req("GET", f"/api/releases/{c2['id']}/thumb", raw=True)
        check(blob[:2] == b"\xff\xd8" and len(blob) > 1000, f"thumb {label}")
    for _ in range(65):
        req("GET", f"/api/releases/{c2['id']}/thumb", raw=True)
    check(True, "large thumbnail grids are not request-limited")
    video_thumb = req("GET", f"/api/releases/{c2['id']}/thumb", raw=True)
    for label in ("generated", "cached"):
        cover_thumb = req(
            "GET", f"/api/releases/{c2['id']}/thumb?music_cover=1", raw=True)
        check(cover_thumb[:2] == b"\xff\xd8" and len(cover_thumb) > 1000
              and cover_thumb != video_thumb, f"music cover thumb {label}")
    fallback = req("GET", f"/api/releases/{p5['id']}/thumb?music_cover=1", raw=True)
    video_fallback = req("GET", f"/api/releases/{p5['id']}/thumb", raw=True)
    check(fallback == video_fallback, "music cover falls back to video thumb")

    # Task 3 (v3): audio status + extract jobs
    st = req("GET", f"/api/releases/{c10['id']}/audio-status")
    check(st == {"cached": False, "streams": 2}, f"c10 audio-status fresh ({st})")
    r = req("POST", f"/api/releases/{c10['id']}/extract-audio")
    res = wait_job(r["job_id"])
    check(res["extracted"] == 1, f"extract job ran ({res})")
    st = req("GET", f"/api/releases/{c10['id']}/audio-status")
    check(st["cached"] is True, "c10 audio cached after job")
    st = req("GET", f"/api/releases/{p5['id']}/audio-status")
    check(st["streams"] == 1, f"PUMP 5 single stream ({st})")
    r = req("POST", f"/api/releases/{p5['id']}/extract-audio")
    res = wait_job(r["job_id"])
    check(res["extracted"] == 0 and "no second" in res.get("reason", ""),
          f"single-stream extract skips ({res})")
    r = req("POST", "/api/extract-audio-all")
    res = wait_job(r["job_id"])
    check(res["extracted"] == 0 and res["skipped"] == 1 and not res["failed"],
          f"bulk extract: only PUMP 5 uncached, skipped ({res})")

    # Task 4 (v3): music pairing exposed in library payload + mix covers
    rels = req("GET", "/api/releases?include_tracks=1")
    c2full = next(r for r in rels if r["id"] == c2["id"])
    names = [t["music_name"] for t in c2full["tracks"]]
    check(names == ["01 Warmup", "02 Power", "03 Cooldown"],
          f"include_tracks carries music_name ({names})")
    detail = req("GET", f"/api/releases/{c2['id']}")
    detail_names = [t["music_name"] for t in detail["tracks"]]
    check(detail_names == names,
          f"release editor carries matched music names ({detail_names})")
    mixes = req("GET", "/api/mixes")
    smoke_mix = next(m for m in mixes if m["id"] == mix["id"])
    check(smoke_mix["cover_release_id"] == c2["id"],
          f"mix cover release ({smoke_mix['cover_release_id']})")

    # Task 5 (v3): slot-ladder generator + filters + vault exclusion
    base = lambda name: int(re.match(r"\s*0*(\d+)", name).group(1))
    try:
        req("POST", "/api/mixes/generate", {"mode": "any", "minutes": 10})
        check(False, "Curated pool excludes unchecked releases")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "Curated pool excludes unchecked releases")

    discovery_track = req("POST", f"/api/releases/{p5['id']}/tracks", {
        "name": "01 Discovery", "start": 1.0, "end": 9.0,
    })
    discovery_track_id = discovery_track["id"]
    discovery = req("POST", "/api/mixes/generate", {
        "mode": "program", "program": "PUMP", "minutes": 10,
        "source_pool": "discovery",
    })
    check([i["track_id"] for i in discovery["items"]] == [discovery_track_id],
          "Discovery uses unchecked detected release")
    req("DELETE", f"/api/tracks/{discovery_track_id}")
    rejected_detail = req("GET", f"/api/releases/{p5['id']}")
    check(not rejected_detail["tracks"] and
          [t["id"] for t in rejected_detail["rejected_tracks"]] == [discovery_track_id],
          "deleted segment becomes restorable rejection")
    try:
        req("POST", "/api/mixes/generate", {
            "mode": "program", "program": "PUMP", "minutes": 10,
            "source_pool": "discovery",
        })
        check(False, "Discovery excludes rejected segments by default")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "Discovery excludes rejected segments by default")
    discovery = req("POST", "/api/mixes/generate", {
        "mode": "program", "program": "PUMP", "minutes": 10,
        "source_pool": "discovery", "include_rejected": True,
    })
    check([i["track_id"] for i in discovery["items"]] == [discovery_track_id],
          "Discovery can include rejected segments")
    req("PATCH", f"/api/tracks/{discovery_track_id}", {"rejected": 0})

    for release in (c2, c10):
        marked = req("PATCH", f"/api/releases/{release['id']}", {"curated": 1})
        check(marked["curated"] == 1, f"release marked Curated ({release['title']})")
    g = req("POST", "/api/mixes/generate", {"mode": "any", "minutes": 10})
    bases = [base(i["name"]) for i in g["items"]]
    check(bases == sorted(bases), f"ladder ascending ({bases})")
    check(bases[0] == 1 and bases[-1] == 3, f"warmup first, cooldown last ({bases})")
    check(len(bases) == len(set(bases)) == 3, f"every base slot stays unique ({bases})")
    g = req("POST", "/api/mixes/generate",
            {"mode": "any", "minutes": 10, "slot_min": 2, "slot_max": 2})
    bases = [base(i["name"]) for i in g["items"]]
    check(bases == [2], f"slot window still picks one base-2 variant ({bases})")
    g = req("POST", "/api/mixes/generate",
            {"mode": "any", "minutes": 10, "programs": ["COMBAT"],
             "release_min": 5, "release_max": 20})
    check({i["release_id"] for i in g["items"]} == {c10["id"]},
          "release window keeps only COMBAT 10")
    try:
        req("POST", "/api/mixes/generate",
            {"mode": "any", "minutes": 10, "slot_min": 5, "slot_max": 2})
        check(False, "inverted slot range rejected")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "inverted slot range rejected")
    req("PATCH", f"/api/releases/{c10['id']}", {"vaulted": 1})
    try:
        req("POST", "/api/mixes/generate",
            {"mode": "any", "minutes": 10, "programs": ["COMBAT"],
             "release_min": 5, "release_max": 20})
        check(False, "vaulted release excluded from generator")
    except urllib.error.HTTPError as e:
        check(e.code == 400, "vaulted release excluded from generator")
    g = req("POST", "/api/mixes/generate",
            {"mode": "any", "minutes": 10, "programs": ["COMBAT"],
             "release_min": 5, "release_max": 20, "include_vault": True})
    check({i["release_id"] for i in g["items"]} == {c10["id"]},
          "advanced toggle includes Vault content")
    g = req("POST", "/api/mixes/generate",
            {"mode": "program", "program": "COMBAT", "minutes": 10})
    check({i["release_id"] for i in g["items"]} == {c2["id"]},
          "program mode skips vaulted release")
    req("PATCH", f"/api/releases/{c10['id']}", {"vaulted": 0})

    # 3/3A/3B are alternatives, never three separate slots. Slot 10 (core)
    # survives duration trimming in both program and surprise generators.
    extra_ids = []
    try:
        for pos, name in enumerate(
                ["Intro text", "03A Bonus", "03B Alternate", "04 Track", "05 Track", "06 Track",
                 "07 Track", "08 Track", "09 Track", "10 Core", "11 Track", "12 Cooldown"]):
            t = req("POST", f"/api/releases/{c2['id']}/tracks",
                    {"name": name, "start": pos * 180, "end": (pos + 1) * 180})
            extra_ids.append(t["id"])
        for mode, extra in (("program", {"program": "COMBAT"}), ("any", {})):
            full = req("POST", "/api/mixes/generate",
                       {"mode": mode, "minutes": 120, **extra})
            full_bases = [base(i["name"]) for i in full["items"]]
            check(len(full_bases) == len(set(full_bases)),
                  f"variant base slots never duplicate ({mode}: {full_bases})")
            check(full_bases.count(3) == 1,
                  f"one random 3/3A/3B variant ({mode}: {full_bases})")
            check(all(i["name"] != "Intro text" for i in full["items"]),
                  f"unnumbered intro excluded ({mode})")
            trimmed = req("POST", "/api/mixes/generate",
                          {"mode": mode, "minutes": 10, **extra})
            trimmed_bases = [base(i["name"]) for i in trimmed["items"]]
            check(len(trimmed_bases) == len(set(trimmed_bases)),
                  f"trimmed base slots stay unique ({mode}: {trimmed_bases})")
            check(10 in trimmed_bases,
                  f"slot 10 core survives trimming ({mode}: {trimmed_bases})")

        advanced_body = {
            "mode": "any", "minutes": 120, "programs": ["COMBAT"],
            "name": "Custom constrained mix", "required_slots": [4, 10],
            "excluded_slots": [3, 7], "max_per_release": 2, "seed": 501,
        }
        advanced = req("POST", "/api/mixes/generate", advanced_body)
        repeated = req("POST", "/api/mixes/generate", advanced_body)
        advanced_bases = [base(i["name"]) for i in advanced["items"]]
        counts = {}
        for item in advanced["items"]:
            counts[item["release_id"]] = counts.get(item["release_id"], 0) + 1
        check(advanced["name"] == "Custom constrained mix", "custom generator name")
        check({4, 10}.issubset(advanced_bases),
              f"required slots included ({advanced_bases})")
        check(not ({3, 7} & set(advanced_bases)),
              f"excluded slots omitted ({advanced_bases})")
        check(max(counts.values()) <= 2, f"release cap respected ({counts})")
        check([i["track_id"] for i in advanced["items"]] ==
              [i["track_id"] for i in repeated["items"]],
              "seed reproduces track picks")
        try:
            req("POST", "/api/mixes/generate", {
                "mode": "any", "minutes": 60,
                "required_slots": [10], "excluded_slots": [10],
            })
            check(False, "overlapping required/excluded slots rejected")
        except urllib.error.HTTPError as e:
            check(e.code == 400, "overlapping required/excluded slots rejected")
    finally:
        for track_id in extra_ids:
            req("DELETE", f"/api/tracks/{track_id}")


if __name__ == "__main__":
    main()
