#!/usr/bin/env python3
"""
m3u8_extractor.py — pull HLS (.m3u8) stream URLs out of a webpage.

Loads the page in a real Chromium-based browser (via Playwright) and
watches every network request for anything containing ".m3u8" — this is
what catches streams that only appear once the page's JS player starts.
Also regex-scans the rendered HTML/JS as a fallback, for links (absolute
or relative) that are embedded in the source but never actually fetched
during the observation window.

Setup (one-time, in whichever Python env you run this from):
    pip install playwright
    playwright install chromium

Run:
    python m3u8_extractor.py
    python m3u8_extractor.py https://example.com/some-video-page   # pre-fills + auto-runs
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import urljoin, urlparse

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Missing dependency in this environment. Run:\n"
          "    pip install playwright\n"
          "    playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config — tweak these per-site if extraction comes up empty
# ---------------------------------------------------------------------------

# Mode A (default): Playwright launches its own browser — simplest, works
# right after `playwright install chromium`.
# Mode B: connect to a Brave/Chrome you already started with remote debugging
# (keeps your real cookies/session/extensions — closer to roshy_grab.py /
# javguru_grab.py, and often better at getting past bot/age checks):
#   1) relaunch Brave with:
#        "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
#   2) set USE_CDP = True
USE_CDP = False
CDP_URL = "http://localhost:9222"

# Only used in Mode A. Leave None to use Playwright's bundled Chromium.
BROWSER_EXECUTABLE = None
# BROWSER_EXECUTABLE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

HEADLESS = False        # kept False on purpose: real (non-headless) Chromium behaves
                        # differently from headless in ways some sites detect/react to.
                        # OFFSCREEN below hides the window instead of using headless mode.
OFFSCREEN = True        # only applies when HEADLESS is False: launches a normal visible
                        # browser but positions its window far outside the screen area,
                        # so nothing actually appears on your display. Set False to watch it.
WAIT_SECONDS = 8        # time given for the page's player to request its stream
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

M3U8_ABS_RE = re.compile(r'https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*', re.IGNORECASE)
M3U8_REL_RE = re.compile(r'["\']([^"\'<>\s]+?\.m3u8[^"\'<>\s]*)["\']', re.IGNORECASE)

# DASH is the other manifest format that shows up in place of HLS on some sites
MPD_ABS_RE = re.compile(r'https?://[^\s"\'<>]+?\.mpd[^\s"\'<>]*', re.IGNORECASE)
MPD_REL_RE = re.compile(r'["\']([^"\'<>\s]+?\.mpd[^"\'<>\s]*)["\']', re.IGNORECASE)

# Content-types that mean "this response is a manifest" even if the URL
# itself has no .m3u8/.mpd in it (some sites proxy the manifest through a
# tokenized/opaque path)
MANIFEST_CONTENT_TYPES = (
    "mpegurl",           # application/vnd.apple.mpegurl, audio/mpegurl, etc.
    "dash+xml",          # application/dash+xml
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

RESUME_SELECTOR = "a.vjs-inplayer-resume-button-text"  # "Close & Play" — dismisses the in-player ad slot
POSTER_SELECTOR = ".vjs-poster"           # thumbnail overlay that starts playback
SKIP_AD_SELECTOR = ".vast-skip-button.enabled"

SKIP_AD_INITIAL_DELAY_MS = 5000     # wait after the 2nd click before even looking for the skip button
SKIP_AD_SEARCH_ATTEMPTS = 8         # ~4s at 500ms intervals
SKIP_AD_SEARCH_INTERVAL_MS = 500
NO_SKIP_MAX_WAIT_SECONDS = 30       # if the skip button never shows, wait this long for the ad to run out
M3U8_POLL_ATTEMPTS = 8              # ~4s at 500ms intervals, checking if a manifest turned up meanwhile
M3U8_POLL_INTERVAL_MS = 500

FALLBACK_PLAY_SELECTORS = (
    "button.vjs-big-play-button",
    '[title="Play Video"]',
    '[class*="play"]',
    '[class*="Play"]',
    "video",
)


def has_master_and_index(found: dict) -> bool:
    """True once we've captured at least one manifest URL containing
    'master' and at least one containing 'index' — the pair the target
    site actually needs (master playlist + variant/index playlist)."""
    urls = found.keys()
    has_master = any("master" in u.lower() for u in urls)
    has_index = any("index" in u.lower() for u in urls)
    return has_master and has_index


async def wait_or_ready(page, total_ms: int, found: dict, poll_ms: int = 300) -> bool:
    """Sleep up to total_ms, but return as soon as has_master_and_index(found)
    goes True instead of waiting out the full duration."""
    elapsed = 0
    while elapsed < total_ms:
        if has_master_and_index(found):
            return True
        step = min(poll_ms, total_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    return has_master_and_index(found)


async def find_in_frames(page, selector):
    """Return (element, frame) for the first frame containing a match,
    or (None, None) — the player is very often inside an embed iframe."""
    for frame in page.frames:
        try:
            el = await frame.query_selector(selector)
        except Exception:
            continue
        if el:
            return el, frame
    return None, None


async def click_element(el, frame, selector, log):
    """Click, falling back to force=True (bypasses Playwright's visibility/
    receives-events checks) when something is intercepting the click."""
    try:
        await el.click(timeout=1500)
        log(f"  clicked {selector!r} in {frame.url}")
        return True
    except Exception as e:
        log(f"  click on {selector!r} failed ({e}); trying force click")
    try:
        await el.click(timeout=1500, force=True)
        log(f"  forced click on {selector!r} worked")
        return True
    except Exception as e:
        log(f"  forced click on {selector!r} also failed: {e}")
        return False


async def find_skip_countdown(page):
    """Find the player's visible countdown text, for example ``Skip in 5``.

    The player has used several different elements for this label, so inspect
    visible text in every frame instead of depending on one CSS class.
    Returns (element, frame) or (None, None).
    """
    pattern = re.compile(r"\bskip\s+in\s+\d+\b", re.IGNORECASE)
    for frame in page.frames:
        try:
            locator = frame.get_by_text(pattern).first
            if await locator.count() and await locator.is_visible():
                return locator, frame
        except Exception:
            continue
    return None, None


async def wait_for_skip_countdown(page, log, timeout_ms=1800):
    """Poll briefly for the ad countdown after a play click."""
    elapsed = 0
    while elapsed < timeout_ms:
        element, frame = await find_skip_countdown(page)
        if element is not None:
            try:
                text = (await element.inner_text()).strip()
            except Exception:
                text = "Skip in ..."
            log(f"  detected ad countdown {text!r} in {frame.url}")
            return element, frame
        step = min(250, timeout_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    return None, None


async def dismiss_ad(page, log, found: dict):
    """Step 4: wait a beat after the 2nd play-click, then look for the
    skip-ad button on a short poll. If it never appears, wait out the ad's
    max runtime, then poll to see if a manifest has already turned up in
    the background (via the request/response listeners) before handing
    control back for the explicit extraction pass. Every wait in here
    bails out immediately once both a master and index manifest are seen."""
    log(f"  waiting {SKIP_AD_INITIAL_DELAY_MS / 1000:.0f}s before checking for a skip-ad button...")
    if await wait_or_ready(page, SKIP_AD_INITIAL_DELAY_MS, found):
        log("  master + index manifest already captured — skipping ad handling")
        return

    for _ in range(SKIP_AD_SEARCH_ATTEMPTS):
        if has_master_and_index(found):
            log("  master + index manifest already captured — skipping ad handling")
            return
        el, frame = await find_in_frames(page, SKIP_AD_SELECTOR)
        if el:
            await click_element(el, frame, SKIP_AD_SELECTOR, log)
            return
        await page.wait_for_timeout(SKIP_AD_SEARCH_INTERVAL_MS)

    log(f"  {SKIP_AD_SELECTOR!r} never appeared — waiting up to {NO_SKIP_MAX_WAIT_SECONDS}s for the ad to finish")
    if await wait_or_ready(page, NO_SKIP_MAX_WAIT_SECONDS * 1000, found):
        log("  master + index manifest captured mid-wait — moving on")
        return

    log(f"  polling for a manifest link every {M3U8_POLL_INTERVAL_MS}ms...")
    for _ in range(M3U8_POLL_ATTEMPTS):
        if has_master_and_index(found):
            log(f"  master + index manifest found ({len(found)} link(s) total) — moving on")
            return
        await page.wait_for_timeout(M3U8_POLL_INTERVAL_MS)
    log("  still nothing after polling — proceeding to the full extraction pass")


async def try_click_play(page, log, found: dict, attempts: int = 5, retry_delay_ms: int = 1000):
    """Start playback: click 'Close & Play' to dismiss the in-player ad
    slot (falling back to the poster/generic play-button search for sites
    without that resume button), then dismiss/wait out a pre-roll ad.
    Selectors can take a moment to render (or their iframe to attach), so
    the sweep retries a few times with a short pause.
    """
    for attempt in range(attempts):
        frames = page.frames
        log(f"  click attempt {attempt + 1}/{attempts}: {len(frames)} frame(s) present")

        el, frame = await find_in_frames(page, RESUME_SELECTOR)
        selector_used = RESUME_SELECTOR
        if not el:
            el, frame = await find_in_frames(page, POSTER_SELECTOR)
            selector_used = POSTER_SELECTOR
        if not el:
            for selector in FALLBACK_PLAY_SELECTORS:
                el, frame = await find_in_frames(page, selector)
                if el:
                    selector_used = selector
                    break

        if el:
            log(f"    {selector_used!r} found in {frame.url}")
            if await click_element(el, frame, selector_used, log):
                # A successful DOM click does not always start playback: an
                # overlay can consume it. Keep retrying until the player proves
                # that the click reached the ad by showing its "Skip in N"
                # countdown. The bounded loop avoids the old click storm while
                # covering the intermittent first-click failure.
                countdown, countdown_frame = await wait_for_skip_countdown(
                    page, log, timeout_ms=1400
                )
                if countdown is None:
                    el2, frame2 = await find_in_frames(page, selector_used)
                    if el2:
                        log("    no 'Skip in N' countdown yet — retrying player click")
                        await click_element(el2, frame2, selector_used, log)
                        countdown, countdown_frame = await wait_for_skip_countdown(
                            page, log, timeout_ms=1400
                        )
                if countdown is not None:
                    await dismiss_ad(page, log, found)
                    return f"{selector_used!r} in {frame.url}"
                # Some pages have no pre-roll countdown at all. If the control
                # disappeared after the click, treat that as a successful
                # non-ad playback start; otherwise let the outer retry sweep
                # try again rather than returning after an unverified click.
                remaining_el, remaining_frame = await find_in_frames(page, selector_used)
                if remaining_el is None:
                    await dismiss_ad(page, log, found)
                    return f"{selector_used!r} in {frame.url}"

        if attempt < attempts - 1:
            await page.wait_for_timeout(retry_delay_ms)
    return None


async def extract_m3u8_async(url: str, log) -> tuple[list[str], str]:
    found: dict[str, None] = {}
    media_requests_seen: list[str] = []  # diagnostic only, used if we come up empty
    title = ""

    def on_request(request):
        if (".m3u8" in request.url or ".mpd" in request.url) and request.url not in found:
            found[request.url] = None
            log(f"  found (request URL): {request.url}")

    async def on_response(response):
        # Catches manifests served from a URL that doesn't contain .m3u8/.mpd —
        # tokenized/opaque paths that only reveal themselves via content-type.
        try:
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:
            return
        if any(tag in ctype for tag in MANIFEST_CONTENT_TYPES) and response.url not in found:
            found[response.url] = None
            log(f"  found (response content-type {ctype!r}): {response.url}")
        elif ctype.startswith(("video/", "audio/", "application/octet-stream")):
            # not a manifest, but useful to see if nothing else turns up —
            # tells you the player IS fetching media, just not via a manifest
            if response.url not in media_requests_seen:
                media_requests_seen.append(response.url)

    async with async_playwright() as p:
        if USE_CDP:
            log(f"connecting to browser at {CDP_URL} ...")
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            log(f"launching browser (headless={HEADLESS}) ...")
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if not HEADLESS and OFFSCREEN:
                log("  positioning window off-screen (OFFSCREEN=True) — you won't see it")
                launch_args += ["--window-position=-32000,-32000", "--window-size=1280,800"]
            launch_kwargs = {"headless": HEADLESS, "args": launch_args}
            if BROWSER_EXECUTABLE:
                launch_kwargs["executable_path"] = BROWSER_EXECUTABLE
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(user_agent=USER_AGENT)

        page = await context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        log(f"loading {url} ...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            raw_title = await page.title()
            if raw_title:
                title = raw_title.strip()
                if title.endswith(" - Eporner"):
                    title = title[:-10].strip()
                elif title.endswith(" - HD porn video"):
                    title = title[:-16].strip()
                    
        except Exception as e:
            log(f"page load warning: {e}")

        clicked = await try_click_play(page, log, found)
        if not clicked:
            log("  no poster/play element could be clicked")

        if has_master_and_index(found):
            log("  master + index manifest already captured — closing browser now")
        else:
            log(f"waiting up to {WAIT_SECONDS}s for the player to request its stream...")
            if await wait_or_ready(page, WAIT_SECONDS * 1000, found):
                log("  master + index manifest captured — closing browser now")

        if not has_master_and_index(found):
            # Diagnostic: what does the <video> element itself think its source is?
            # If clicking genuinely started playback, currentSrc is usually set even
            # when it's a blob: URL (MSE) that no network sniffing will ever catch.
            try:
                for frame in page.frames:
                    video_info = await frame.evaluate(
                        """() => {
                            const v = document.querySelector('video');
                            if (!v) return null;
                            return {src: v.src, currentSrc: v.currentSrc, paused: v.paused, readyState: v.readyState};
                        }"""
                    )
                    if video_info:
                        log(f"  <video> in {frame.url}: {video_info}")
                        src = video_info.get("currentSrc") or video_info.get("src")
                        if src and (".m3u8" in src or ".mpd" in src) and src not in found:
                            found[src] = None
                            log(f"  found (video element src): {src}")
            except Exception as e:
                log(f"  video element check failed: {e}")

        if not has_master_and_index(found):
            try:
                html = await page.content()
                base = page.url
                for pattern_abs, pattern_rel, label in (
                    (M3U8_ABS_RE, M3U8_REL_RE, "m3u8"),
                    (MPD_ABS_RE, MPD_REL_RE, "mpd"),
                ):
                    for m in pattern_abs.findall(html):
                        if m not in found:
                            found[m] = None
                            log(f"  found (in page source, {label}): {m}")
                    for m in pattern_rel.findall(html):
                        if m.startswith("http"):
                            continue  # already caught by the absolute-URL pass above
                        resolved = urljoin(base, m)
                        if resolved not in found:
                            found[resolved] = None
                            log(f"  found (in page source, {label}, relative): {resolved}")
            except Exception:
                pass

        if not found and media_requests_seen:
            log(f"\n  no manifest found, but {len(media_requests_seen)} media-typed response(s) were fetched:")
            for m in media_requests_seen[:10]:
                log(f"    {m}")
            log("  (likely MSE/blob playback or a chunked format this script doesn't parse yet)")

        await page.close()
        if not USE_CDP:
            await browser.close()

    return list(found.keys()), title


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, initial_url: str = ""):
        self.root = root
        self.links: list[str] = []
        root.title("m3u8 extractor")
        root.geometry("760x500")

        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=8)
        tk.Label(top, text="URL:").pack(side="left")
        self.url_entry = tk.Entry(top)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.url_entry.insert(0, initial_url)
        self.url_entry.bind("<Return>", lambda e: self.start())
        self.go_btn = tk.Button(top, text="Extract", command=self.start)
        self.go_btn.pack(side="left")

        self.output = scrolledtext.ScrolledText(root, wrap="word")
        self.output.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(bottom, text="Copy all links", command=self.copy_links).pack(side="left")
        tk.Button(bottom, text="Save to file...", command=self.save_links).pack(side="left", padx=(6, 0))

        if initial_url:
            root.after(200, self.start)

    # -- thread-safe logging: worker thread only ever calls self.log(),
    #    which marshals the actual widget update onto the Tk main loop.
    def log(self, msg: str):
        self.root.after(0, self._append_log, msg)

    def _append_log(self, msg: str):
        self.output.insert("end", msg + "\n")
        self.output.see("end")

    def start(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        if not urlparse(url).scheme:
            url = "https://" + url
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)

        self.go_btn.config(state="disabled")
        self.output.delete("1.0", "end")
        self.links = []
        threading.Thread(target=self._run, args=(url,), daemon=True).start()

    def _run(self, url: str):
        try:
            results, title = asyncio.run(extract_m3u8_async(url, self.log))
        except Exception as e:
            self.log(f"error: {e}")
            results = []
        self.root.after(0, self._finish, results)

    def _finish(self, results: list[str]):
        self.links = results
        if results:
            self._append_log(f"\n{len(results)} link(s) total.")
        else:
            tip = ("try setting OFFSCREEN = False to watch the window, or raise WAIT_SECONDS"
                   if HEADLESS is False else "try setting HEADLESS = False, or raise WAIT_SECONDS")
            self._append_log(f"\nno .m3u8 links found — {tip} in the script.")
        self.go_btn.config(state="normal")

    def copy_links(self):
        if not self.links:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(self.links))
        messagebox.showinfo("Copied", f"{len(self.links)} link(s) copied to clipboard.")

    def save_links(self):
        if not self.links:
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                             filetypes=[("Text files", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.links))


def main():
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description="m3u8 extractor")
    parser.add_argument("url", nargs="?", default="", help="URL to extract from")
    parser.add_argument("--json", action="store_true", help="Output JSON without GUI")
    args = parser.parse_args()

    if args.json:
        if not args.url:
            print(json.dumps({"error": "No URL provided"}))
            sys.exit(1)
            
        def dummy_log(msg):
            pass
            
        try:
            url = args.url
            if not urlparse(url).scheme:
                url = "https://" + url
            links, title = asyncio.run(extract_m3u8_async(url, dummy_log))
            print(json.dumps({"title": title, "links": links}))
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        return

    root = tk.Tk()
    App(root, args.url)
    root.mainloop()


if __name__ == "__main__":
    main()
