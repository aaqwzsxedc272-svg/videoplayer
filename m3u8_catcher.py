#!/usr/bin/env python3
"""
m3u8_catcher.py

Give it a page URL (an embed page, an iframe src, whatever) and it drives a
real headless browser, watches every network request the page makes, and
reports any .m3u8 URLs it sees. Falls back to regex-scanning the rendered
HTML (main frame + all iframes) in case a URL is embedded in inline JS but
never actually fetched during load.

One-time setup:
    pip install playwright
    playwright install chromium

Usage:
    python m3u8_catcher.py "https://example.com/embed/abc123"
    python m3u8_catcher.py "https://example.com/embed/abc123" --headed -v
    python m3u8_catcher.py "https://example.com/embed/abc123" --json
    python m3u8_catcher.py "https://example.com/e/xyz" --referer "https://example.com/watch/xyz"

Importable:
    from m3u8_catcher import catch_m3u8
    results = await catch_m3u8("https://example.com/embed/abc123")
    # -> {"https://cdn.../master.m3u8": {"source": "request", "t": 1737...}}

Notes:
- Some hosts fire a short "preview"/promo .m3u8 first and the real one a few
  seconds later (or after a click) -- this keeps listening for --extra-wait
  seconds after load/click and returns every distinct URL it saw, in the
  order first seen, so you can eyeball which one is the real stream.
- Cloudflare-gated hosts may still block a plain headless browser outright;
  the stealth tweaks here (hiding navigator.webdriver, etc.) cover the common
  case but not a full JS challenge. If a host needs cookies from a prior
  browser session, that's a per-host problem, not something this generic
  tool solves.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time

from playwright.async_api import async_playwright, Request, Response

M3U8_RE = re.compile(r"https?://[^\s'\"<>\\]+\.m3u8[^\s'\"<>\\]*", re.IGNORECASE)

# Selectors that commonly sit on top of a video and start playback on click.
PLAY_SELECTORS = [
    ".jw-icon-playback", ".vjs-big-play-button", ".plyr__control--overlaid",
    "[class*='play-button']", "[class*='playButton']", "[class*='play_button']",
    "[id*='play']", ".plyr", "#player", "video",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


def find_installed_brave_executable():
    def usable(path):
        if not path:
            return ""
        candidate = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
        if "brave-automation-tool" in os.path.normcase(candidate).lower():
            return ""
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "brave.exe" if os.name == "nt" else "brave")
        return os.path.abspath(candidate) if os.path.isfile(candidate) else ""

    for key in ("VIDEO_PLAYER_BRAVE_PATH", "BRAVE_PATH", "BRAVE_EXE"):
        found = usable(os.environ.get(key))
        if found:
            return found

    for name in ("brave.exe", "brave-browser", "brave"):
        found = usable(shutil.which(name))
        if found:
            return found

    if os.name == "nt":
        roots = [
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ]
        rels = (
            ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            ("BraveSoftware", "Brave-Browser-Beta", "Application", "brave.exe"),
            ("BraveSoftware", "Brave-Browser-Nightly", "Application", "brave.exe"),
            ("BraveSoftware", "Brave-Browser-Dev", "Application", "brave.exe"),
        )
        for root in roots:
            if not root:
                continue
            for rel in rels:
                found = usable(os.path.join(root, *rel))
                if found:
                    return found
    return ""


class M3U8Catcher:
    def __init__(self, headless=True, timeout=25.0, click=True, verbose=False,
                 extra_wait=4.0, referer=None):
        self.headless = headless
        self.timeout = timeout
        self.click = click
        self.verbose = verbose
        self.extra_wait = extra_wait
        self.referer = referer
        self.found = {}          # url -> {"source": ..., "t": ...}, preserves first-seen order
        self.all_requests = []   # every request URL seen, for --dump-requests
        self.page_title = ""

    def _log(self, *a):
        if self.verbose:
            print("[catcher]", *a, file=sys.stderr)

    def _note(self, url, source):
        if url not in self.found:
            self.found[url] = {"source": source, "t": time.time()}
            self._log(f"MATCH ({source}): {url}")

    def _on_request(self, request: Request):
        self.all_requests.append(request.url)
        if ".m3u8" in request.url.lower():
            self._note(request.url, "request")

    def _on_response(self, response: Response):
        url = response.url
        ctype = ""
        try:
            ctype = response.headers.get("content-type", "")
        except Exception:
            pass
        if ".m3u8" in url.lower() or "mpegurl" in ctype.lower():
            self._note(url, "response")

    async def _capture_page_title(self, page):
        selectors = [
            "h1.watch__title",
            "h1",
            "title",
        ]
        for frame in page.frames:
            for sel in selectors:
                try:
                    el = await frame.query_selector(sel)
                    if not el:
                        continue
                    text = (await el.inner_text() or "").strip()
                    if text:
                        return text
                except Exception:
                    continue
        try:
            return (await page.title() or "").strip()
        except Exception:
            return ""

    async def _try_click_through(self, page):
        """Click likely play buttons -- including inside iframes -- to kick off loading."""
        for frame in page.frames:
            # only one click per frame -- a second click on the same play/pause
            # toggle could cancel the very request we're trying to trigger
            for sel in PLAY_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                    if el:
                        await el.click(timeout=1500)
                        self._log(f"clicked {sel!r} in frame {frame.url}")
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
        # Generic center-click fallback for custom players with no recognizable class.
        try:
            box = page.viewport_size or {"width": 1280, "height": 720}
            await page.mouse.click(box["width"] // 2, box["height"] // 2)
        except Exception:
            pass

    async def _close_popup(self, popup_page):
        """Popunders/ad tabs are noise, but sniff them briefly before closing --
        some hosts open the real player in a new tab instead of the same one."""
        try:
            popup_page.on("request", self._on_request)
            popup_page.on("response", self._on_response)
            await popup_page.wait_for_timeout(1200)
        except Exception:
            pass
        finally:
            try:
                await popup_page.close()
                self._log("closed a popup/popunder")
            except Exception:
                pass

    async def catch(self, url: str):
        async with async_playwright() as p:
            brave_path = find_installed_brave_executable()
            if not brave_path:
                raise RuntimeError("Installed Brave browser was not found")
            browser = await p.chromium.launch(
                executable_path=brave_path,
                headless=self.headless,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context_kwargs = dict(user_agent=UA, viewport={"width": 1280, "height": 720})
            if self.referer:
                context_kwargs["extra_http_headers"] = {"Referer": self.referer}
            context = await browser.new_context(**context_kwargs)
            await context.add_init_script(STEALTH_JS)

            page = await context.new_page()
            page.on("request", self._on_request)
            page.on("response", self._on_response)
            # NOTE: this must be page.on("popup", ...), not context.on("page", ...) --
            # the context-level "page" event also fires for this main page's own
            # creation above, which would close our own tab immediately.
            page.on("popup", lambda p2: asyncio.create_task(self._close_popup(p2)))

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            except Exception as e:
                self._log(f"goto warning: {e}")

            # let initial requests / any autoplay settle
            await page.wait_for_timeout(1500)

            if self.click:
                await self._try_click_through(page)

            # keep listening -- some hosts fire a throwaway preview clip first
            # and the real manifest a few seconds later
            await page.wait_for_timeout(self.extra_wait * 1000)

            # regex fallback: catches URLs sitting in inline JS/JSON that were
            # never actually requested during this load
            try:
                self.page_title = await self._capture_page_title(page)
            except Exception:
                self.page_title = self.page_title or ""
            try:
                html = await page.content()
                for m in M3U8_RE.findall(html):
                    self._note(m, "html")
            except Exception:
                pass
            for frame in page.frames:
                try:
                    html = await frame.content()
                    for m in M3U8_RE.findall(html):
                        self._note(m, "frame-html")
                except Exception:
                    continue

            await context.close()
            await browser.close()

        return self.found


async def catch_m3u8(url, headless=True, timeout=25.0, click=True, verbose=False,
                      extra_wait=4.0, referer=None):
    """Importable entry point. Returns {url: {"source": ..., "t": ...}}."""
    catcher = M3U8Catcher(headless=headless, timeout=timeout, click=click,
                          verbose=verbose, extra_wait=extra_wait, referer=referer)
    return await catcher.catch(url)


def main():
    ap = argparse.ArgumentParser(description="Detect .m3u8 stream URLs loaded by a web page.")
    ap.add_argument("url", help="Page URL to load (embed page, iframe src, etc.)")
    ap.add_argument("--headed", action="store_true", help="Show the browser window (debugging)")
    ap.add_argument("--timeout", type=float, default=25.0, help="Page load timeout in seconds (default: 25)")
    ap.add_argument("--extra-wait", type=float, default=4.0,
                     help="Extra seconds to keep listening after load/click (default: 4)")
    ap.add_argument("--no-click", action="store_true", help="Don't attempt to click play buttons")
    ap.add_argument("--referer", default=None,
                     help="Force a Referer header -- useful if you're feeding in a raw "
                          "iframe/player src URL directly, bypassing its normal parent page")
    ap.add_argument("--json", action="store_true", help="Output results as JSON instead of plain URLs")
    ap.add_argument("--dump-requests", metavar="FILE",
                     help="Write every network request URL seen to FILE (handy if nothing "
                          ".m3u8 turns up and you want to eyeball what did load)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print debug info to stderr")
    args = ap.parse_args()

    catcher = M3U8Catcher(
        headless=not args.headed,
        timeout=args.timeout,
        click=not args.no_click,
        verbose=args.verbose,
        extra_wait=args.extra_wait,
        referer=args.referer,
    )
    results = asyncio.run(catcher.catch(args.url))

    if args.dump_requests:
        with open(args.dump_requests, "w") as f:
            f.write("\n".join(catcher.all_requests))
        print(f"Wrote {len(catcher.all_requests)} request URLs to {args.dump_requests}", file=sys.stderr)

    if not results:
        print("No .m3u8 URLs detected.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        if catcher.page_title:
            print(f"TITLE: {catcher.page_title}", file=sys.stderr)
        print(json.dumps([{"url": u, **meta} for u, meta in results.items()], indent=2))
    else:
        for u in results:
            print(u)


if __name__ == "__main__":
    main()
