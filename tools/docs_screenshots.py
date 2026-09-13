r"""Regenerate the screenshots used by docs/USER_GUIDE.md.

Point the script at a running MixMill instance that already contains a real
library, and it walks every view and writes PNG files into docs/images/.

tools/docs_demo_skin.js runs before the page scripts, so program names, release
titles, song names, covers, video frames and choreography pages are replaced
with neutral demo values. The instance itself is never modified.

Usage (PowerShell):

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install playwright
    playwright install chromium
    $env:MIXMILL_URL   = "http://127.0.0.1:2999/"
    $env:MIXMILL_USERNAME = "your-user"
    py tools/docs_screenshots.py

The password is read from $env:MIXMILL_PASSWORD when it is set, otherwise the
script asks for it and does not echo it.

Options:
    --no-skin     capture the instance as it really is (private screenshots)
    --url URL     override MIXMILL_URL
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover - dependency hint
    sys.exit("playwright is not installed. Run: pip install playwright && playwright install chromium")

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "tools" / "docs_demo_skin.js"
OUT = ROOT / "docs" / "images"

VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2


def settle(page: Page, ms: int = 900) -> None:
    page.wait_for_timeout(ms)


HIDE_STICKY = "header, .skip-link { visibility: hidden !important; }"


def shot(page: Page, name: str, selector: str | None = None, full: bool = False) -> None:
    """Write one PNG. Element shots hide the sticky header so it cannot overlap."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    if selector:
        style = page.add_style_tag(content=HIDE_STICKY)
        page.locator(selector).first.scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        page.locator(selector).first.screenshot(path=str(path))
        page.evaluate("el => el.remove()", style)
    else:
        page.screenshot(path=str(path), full_page=full)
    print(f"  wrote {path.relative_to(ROOT)}")


def pick_ids(page: Page) -> dict:
    """Choose the most complete release and mix so the guide shows a full screen."""
    return page.evaluate(
        """async () => {
            const rs = await (await fetch('/api/releases')).json();
            const releases = Array.isArray(rs) ? rs : (rs.releases || rs.items || []);
            const usable = releases.filter(r => !r.missing && !r.vaulted && r.track_count > 3);
            const curated = usable.filter(r => r.curated);
            const best = (curated.length ? curated : usable)
                .sort((a, b) => b.track_count - a.track_count)[0];
            const review = usable.filter(r => !r.curated).sort((a, b) => b.track_count - a.track_count)[0];
            const ms = await (await fetch('/api/mixes')).json();
            const mixes = Array.isArray(ms) ? ms : (ms.mixes || []);
            const mix = mixes.sort((a, b) => (b.item_count || 0) - (a.item_count || 0))[0];
            const vault = releases.find(r => r.vaulted);
            return {
                release: best ? best.id : null,
                review: review ? review.id : null,
                mix: mix ? mix.id : null,
                vault: vault ? vault.id : null,
            };
        }"""
    )


def capture(page: Page) -> None:
    ids = pick_ids(page)
    print(f"  using release {ids['release']}, mix {ids['mix']}")

    # --- Library -----------------------------------------------------------
    page.evaluate("showView('library')")
    settle(page)
    shot(page, "library")
    shot(page, "library-toolbar", selector=".library-toolbar")

    page.evaluate("document.getElementById('library-filter-menu').open = true")
    settle(page, 400)
    shot(page, "library-filters", selector=".filter-popover")
    page.evaluate("document.getElementById('library-filter-menu').open = false")

    page.evaluate("setViewMode('list')")
    settle(page, 500)
    shot(page, "library-list")
    page.evaluate("setViewMode('grid')")
    settle(page, 400)

    # --- Release editor ----------------------------------------------------
    if ids["release"]:
        page.evaluate(f"openRelease({ids['release']})")
        settle(page, 1500)
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "release-editor")
        shot(page, "release-tracks", selector=".track-table")
        page.evaluate("document.getElementById('notes-studio').open = true")
        settle(page, 1500)
        preview = page.locator("#notes-mapping-list button:not([disabled])").filter(has_text="Preview")
        if preview.count():
            preview.first.click()
            settle(page, 1500)
        shot(page, "release-notes", selector="#notes-studio")
        if page.locator("#music-section:not(.hidden)").count():
            page.locator("#music-section").scroll_into_view_if_needed()
            settle(page, 400)
            shot(page, "release-music", selector="#music-section")

    if ids["review"]:
        page.evaluate(f"openRelease({ids['review']})")
        settle(page, 1500)
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "release-needs-review")

    # --- Mixes -------------------------------------------------------------
    page.evaluate("showView('mixes')")
    settle(page, 1200)
    shot(page, "mixes")
    shot(page, "generator", selector=".generator-panel")
    page.evaluate("document.querySelector('.generator-advanced').open = true")
    settle(page, 400)
    shot(page, "generator-advanced", selector=".generator-panel")
    page.evaluate("document.querySelector('.generator-advanced').open = false")

    if ids["mix"]:
        page.evaluate(f"openMix({ids['mix']})")
        settle(page, 1500)
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "mix-editor")
        shot(page, "mix-export", selector="#view-mix .mix-toolbar")

    # --- Vault and exports -------------------------------------------------
    page.evaluate("showView('vault')")
    settle(page, 900)
    shot(page, "vault", selector="#view-vault")

    page.evaluate("showView('exports')")
    settle(page, 900)
    shot(page, "exports", selector="#view-exports")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("MIXMILL_URL", "http://127.0.0.1:2999/"))
    parser.add_argument("--no-skin", action="store_true", help="capture real names and frames")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    user = os.environ.get("MIXMILL_USERNAME", "")
    password = os.environ.get("MIXMILL_PASSWORD", "")
    if user and not password:
        password = getpass.getpass(f"MixMill password for {user}: ")

    credentials = {"username": user, "password": password} if user else None
    print(f"MixMill: {args.url}  skin: {'off' if args.no_skin else 'on'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=SCALE,
            http_credentials=credentials,
            ignore_https_errors=True,
        )
        if not args.no_skin:
            context.add_init_script(path=str(SKIN))
        page = context.new_page()
        page.goto(args.url, wait_until="networkidle", timeout=60_000)
        page.wait_for_function("typeof showView === 'function'", timeout=30_000)
        settle(page, 1500)
        capture(page)
        context.close()
        browser.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
