"""MixMill Windows desktop entry point."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import secrets
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from desktop.runtime import (
    bootstrap_url,
    configure_environment,
    ensure_app_storage,
    is_link_like,
    load_media_dir,
    local_app_root,
    read_media_setting,
    save_media_dir,
    smoke_desktop_session,
    start_backend,
    validate_media_dir,
)
from desktop.version import APP_VERSION


MUTEX_NAME = "Local\\MixMillDesktop-4B35C76A"
ERROR_ALREADY_EXISTS = 183


def message(title: str, text: str, error: bool = False) -> None:
    try:
        if os.name == "nt":
            icon = 0x10 if error else 0x40
            ctypes.WinDLL("user32").MessageBoxW(None, text, title, icon)
        else:
            logging.error("%s: %s", title, text)
    except Exception:
        logging.error("%s: %s", title, text)


def choose_media_folder(
    app_root: Path,
    current_setting: str | None = None,
    recovery: bool = False,
) -> Path | None:
    if os.name != "nt":
        raise RuntimeError("MixMill desktop folder selection requires Windows")

    from ctypes import wintypes

    detail = (
        "Your saved media folder is unavailable. Reconnect it or choose a new folder.\n\n"
        if recovery else "Choose the folder containing workout release videos.\n\n"
    )
    if current_setting:
        detail += f"Saved folder:\n{current_setting}\n\n"
    detail += (
        "MixMill reads originals there. Database, previews, backups, and exports "
        "stay separately in LOCALAPPDATA\\MixMill."
    )
    answer = ctypes.WinDLL("user32").MessageBoxW(
        None, detail, f"MixMill setup — {APP_VERSION}", 0x01 | 0x40,
    )
    if answer != 1:
        return None

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            # This is a caller-owned output buffer, not a Python string.
            # POINTER(c_wchar) accepts create_unicode_buffer on Python 3.12+;
            # c_wchar_p rejects the array during Structure construction.
            ("pszDisplayName", ctypes.POINTER(ctypes.c_wchar)),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    while True:
        display_name = ctypes.create_unicode_buffer(260)
        browse = BrowseInfo(
            None, None, display_name,
            "Choose MixMill media folder",
            0x0001 | 0x0010 | 0x0040,
            None, 0, 0,
        )
        item_id = shell32.SHBrowseForFolderW(ctypes.byref(browse))
        if not item_id:
            return None
        try:
            selected = ctypes.create_unicode_buffer(32768)
            if not shell32.SHGetPathFromIDListW(item_id, selected):
                message("Folder cannot be used", "Windows could not resolve that folder.", True)
                continue
        finally:
            ole32.CoTaskMemFree(item_id)
        try:
            return validate_media_dir(selected.value, app_root)
        except ValueError as exc:
            message("Folder cannot be used", str(exc), True)


def acquire_single_instance():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    return handle


def setup_logging(app_root: Path) -> None:
    log_dir = app_root / "logs"
    if log_dir.exists() and is_link_like(log_dir):
        raise RuntimeError("MixMill log folder cannot be a link or junction")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "desktop.log"
    if is_link_like(log_file):
        raise RuntimeError("MixMill log file cannot be a link or junction")
    handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
        force=True,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=not getattr(sys, "frozen", False))
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--choose-media", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def webview2_runtime_version() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    client = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = (
        (winreg.HKEY_CURRENT_USER,
         rf"Software\Microsoft\EdgeUpdate\Clients\{client}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"),
    )
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
            if version and version != "0.0.0.0":
                return version
        except OSError:
            continue
    return None


def run(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    app_root = local_app_root()
    try:
        ensure_app_storage(app_root)
        setup_logging(app_root)
    except Exception as exc:  # noqa: BLE001
        message("MixMill could not start", str(exc), error=True)
        return 1

    # Build/installer smoke tests use isolated app data and must not be mistaken
    # for a second interactive instance when the user's MixMill is already open.
    mutex = None if args.smoke_test else acquire_single_instance()
    if mutex is False:
        message("MixMill", "MixMill is already running.")
        return 0

    backend = None
    try:
        logging.info("Starting MixMill Desktop %s", APP_VERSION)
        if args.smoke_test:
            supplied = os.environ.get("MIXMILL_DESKTOP_SMOKE_MEDIA", "")
            if not supplied:
                raise RuntimeError("MIXMILL_DESKTOP_SMOKE_MEDIA is required")
            media = validate_media_dir(supplied, app_root)
        else:
            saved_setting = read_media_setting(app_root)
            media = load_media_dir(app_root)
            if args.choose_media or media is None:
                media = choose_media_folder(
                    app_root,
                    current_setting=saved_setting,
                    recovery=saved_setting is not None and media is None,
                )
                if media is None:
                    return 0
                save_media_dir(app_root, media)

        token = secrets.token_urlsafe(48)
        configure_environment(media, app_root, token)
        backend = start_backend()

        if args.smoke_test:
            smoke_desktop_session(backend.port, token)
            database = app_root / "data" / "mixmill.db"
            if not database.is_file() or not database.stat().st_size:
                raise AssertionError("desktop database was not created")
            return 0

        if not webview2_runtime_version():
            raise RuntimeError(
                "Microsoft Edge WebView2 Runtime is missing. Re-run the MixMill "
                "installer while connected to the internet."
            )

        import webview

        # Downloads are disabled by default. Enabling them opens a native
        # Save As dialog, initially pointed at the Windows Downloads folder.
        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(
            f"MixMill — {APP_VERSION}", bootstrap_url(backend.port, token),
            width=1360, height=880, min_size=(900, 620),
            background_color="#0b0d0b", text_select=True,
        )
        window.events.closed += lambda *_: setattr(backend.server, "should_exit", True)
        webview.start(
            gui="edgechromium", debug=False, private_mode=False,
            storage_path=str(app_root / "webview"),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.exception("MixMill desktop failed")
        if not args.smoke_test:
            message(
                "MixMill could not start",
                f"{exc}\n\nDetails: {app_root / 'logs' / 'desktop.log'}",
                error=True,
            )
        return 1
    finally:
        if backend is not None:
            backend.stop()
        if mutex not in (None, False) and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(run())
