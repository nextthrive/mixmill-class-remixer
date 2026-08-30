"""Windows desktop isolation and loopback-session smoke test."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.runtime import (
    ensure_app_storage,
    ensure_database_healthy,
    load_media_dir,
    local_app_root,
    paths_overlap,
    save_media_dir,
    settings_path,
    validate_media_dir,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok: {message}")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    launcher_source = (ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")
    backend_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    build_source = (ROOT / "tools" / "build_windows.ps1").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    check('webview.settings["ALLOW_DOWNLOADS"] = True' in launcher_source,
          "desktop enables native Save As downloads")
    check(backend_source.count("subprocess.run(") == 1
          and "subprocess.CREATE_NO_WINDOW" in backend_source,
          "media subprocesses stay hidden on Windows")
    check("prepare_nsis.ps1" in build_source
          and "open-source-community" in build_source
          and "signed_release" in build_source,
          "community release uses NSIS and records optional signing state")
    check("FFmpeg-bf1b838f2a-source.zip" in notices,
          "public notices require matching FFmpeg source")
    check('"LICENSE"' in build_source and "PRIVACY.md" in build_source
          and "THIRD_PARTY_NOTICES.md" in build_source,
          "license and public documents are copied beside the portable executable")

    with tempfile.TemporaryDirectory(prefix="mixmill-desktop-") as folder:
        temp = Path(folder)
        local = temp / "LocalAppData"
        media = temp / "Média 📼 مكتبة"
        media.mkdir(parents=True)
        sentinel = media / "original.mp4"
        sentinel.write_bytes(b"original-media-must-not-change")
        before_hash = digest(sentinel)
        before_stat = sentinel.stat()

        app_root = local_app_root({"LOCALAPPDATA": str(local)})
        check(app_root == (local / "MixMill").resolve(),
              "app root is LOCALAPPDATA\\MixMill")
        check(not paths_overlap(media, app_root), "media and app data are separate")
        ensure_app_storage(app_root)
        check(not list(app_root.glob(".write-test-*")), "storage write probe cleans up")
        save_media_dir(app_root, media)
        check(load_media_dir(app_root) == media.resolve(), "media choice persists")
        saved_settings = settings_path(app_root).read_bytes()
        settings_path(app_root).write_bytes(b"{" + b"x" * (65 * 1024))
        check(load_media_dir(app_root) is None, "oversized corrupt settings fail closed")
        settings_path(app_root).write_text("[]", encoding="utf-8")
        check(load_media_dir(app_root) is None, "non-object settings fail closed")
        settings_path(app_root).write_bytes(saved_settings)
        try:
            validate_media_dir(app_root, app_root)
        except ValueError:
            pass
        else:
            raise AssertionError("overlapping media folder was accepted")
        check(True, "overlapping media/app-data folders are rejected")

        env = {
            **os.environ,
            "LOCALAPPDATA": str(local),
            "MIXMILL_DESKTOP_SMOKE_MEDIA": str(media),
        }
        process = subprocess.run(
            [sys.executable, "-m", "desktop.launcher", "--smoke-test"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
        )
        if process.returncode:
            log = app_root / "logs" / "desktop.log"
            detail = log.read_text(encoding="utf-8") if log.is_file() else process.stderr
            raise AssertionError(f"desktop launcher smoke failed:\n{detail[-3000:]}")

        after_stat = sentinel.stat()
        check(digest(sentinel) == before_hash, "media bytes remain unchanged")
        check(after_stat.st_mtime_ns == before_stat.st_mtime_ns,
              "media timestamp remains unchanged")
        check((app_root / "data" / "mixmill.db").is_file(),
              "database lives under LOCALAPPDATA\\MixMill")
        check((app_root / "data" / "exports").is_dir(),
              "exports live under LOCALAPPDATA\\MixMill")
        check(not (media / ".mixmill-data").exists(), "no app marker written to media")

        database = app_root / "data" / "mixmill.db"
        database.write_bytes(b"not-a-sqlite-database")
        try:
            ensure_database_healthy(app_root)
        except RuntimeError as exc:
            check("backup" in str(exc).lower(), "damaged database gives recovery path")
        else:
            raise AssertionError("damaged database passed safety check")

    print("DESKTOP SMOKE PASS")


if __name__ == "__main__":
    main()
