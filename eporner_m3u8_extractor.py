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
import json
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import urljoin, urlparse, urlencode

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
WAIT_SECONDS = 15       # extra time after skip for the real HLS manifests
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

CLOSE_PLAY_SELECTORS = (
    "a.vjs-inplayer-resume-button-text",
    ".vjs-inplayer-resume-button-text",
    "a.vjs-inplayer-resume-button",
    ".vjs-inplayer-resume-button",
    ".vjs-inplayer-resume",
    "[class*='inplayer-resume']",
    "[class*='resume-button']",
)
PLAY_BUTTON_SELECTORS = (
    "button.vjs-big-play-button",
    ".vjs-big-play-button",
    ".vjs-play-control.vjs-paused",
    ".vjs-play-control",
    '[title="Play Video"]',
    '[aria-label="Play Video"]',
    '[aria-label="Play"]',
    "button.play",
)
VIDEO_SPACE_SELECTORS = (
    ".vjs-poster",
    "video.vjs-tech",
    "#EPvideo video",
    "#EPvideo",
    ".video-js video",
    "video",
    ".vjs-tech",
)
SKIP_AD_SELECTORS = (
    ".vast-skip-button.enabled",
    ".vast-skip-button",
    "button.vast-skip-button",
    "a.vast-skip-button",
    ".videojs-ads-info a.enabled",
    ".videojs-ads-info a",
    ".vjs-skip-button",
    ".skip-button.enabled",
    ".skip-button",
)

CLOSE_PLAY_LOOK_MS = 4000
PLAY_BUTTON_LOOK_MS = 3500
SKIP_IN_WAIT_MS = 22000
SKIP_AD_AFTER_COUNTDOWN_MS = 5000
SKIP_AD_LOOK_MS = 4000
LOOK_POLL_MS = 350

_BLOCK_POPUPS_JS = """
(() => {
    try { window.open = function () { return null; }; } catch (e) {}
    try {
        document.addEventListener('click', (ev) => {
            const a = ev.target && ev.target.closest ? ev.target.closest('a[target="_blank"], a[href]') : null;
            if (!a) return;
            const href = String(a.href || '');
            const target = String(a.target || '');
            if (target === '_blank' && !/eporner\\.(com|eu)/i.test(href)) {
                ev.preventDefault();
                ev.stopPropagation();
            }
        }, true);
    } catch (e) {}
})();
"""

_DISMISS_OVERLAY_JS = """() => {
    let n = 0;
    const skipPlayer = /close\\s*(?:&|and)\\s*play|skip\\s+in\\s+\\d+|skip\\s*ad/i;
    const closeTxt = /^(×|✕|x|close|cerrar|no thanks|got it)$/i;
    const closeCls = /(ad[-_]?close|overlay[-_]?close|close[-_]?btn|exx|exit-ad|popup[-_]?close)/i;
    const nodes = document.querySelectorAll('button, a, div, span, i');
    for (const el of nodes) {
        let txt = '';
        try { txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(); } catch (e) {}
        if (skipPlayer.test(txt)) continue;
        const cls = String(el.className || '') + ' ' + String(el.id || '') + ' ' + String(el.getAttribute('aria-label') || '');
        const hit = closeTxt.test(txt) || closeCls.test(cls);
        if (!hit) continue;
        try {
            const r = el.getBoundingClientRect();
            if (r.width < 8 || r.height < 8 || r.width > 90 || r.height > 90) continue;
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') continue;
            el.click();
            n++;
        } catch (e) {}
    }
    return n;
}"""


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
        await keep_eporner_tab(page)
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


_VISIBLE_TEXT_JS = """(kind) => {
    const skipIn = /skip\\s+in\\s+\\d+/i;
    const skipAd = /skip\\s*ad\\s*>*|skip\\s*>{1,}/i;
    const closePlay = /close\\s*(?:&|and)\\s*play/i;
    const playBtn = /^(play|play video)$/i;
    const nodes = document.querySelectorAll('button, a, div, span, p, label, input');
    for (const el of nodes) {
        let txt = '';
        try { txt = (el.innerText || el.textContent || el.getAttribute('title') || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(); } catch (e) {}
        if (!txt || txt.length > 80) continue;
        let hit = false;
        if (kind === 'countdown') hit = skipIn.test(txt);
        else if (kind === 'skip') hit = skipAd.test(txt) && !skipIn.test(txt);
        else if (kind === 'closeplay') hit = closePlay.test(txt);
        else if (kind === 'play') hit = playBtn.test(txt);
        if (!hit) continue;
        try {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            if (r.width < 2 || r.height < 2) continue;
            if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0)
                continue;
        } catch (e) {}
        return txt;
    }
    return null;
}"""

_CLICK_VISIBLE_TEXT_JS = """(kind) => {
    const skipIn = /skip\\s+in\\s+\\d+/i;
    const skipAd = /skip\\s*ad\\s*>*|skip\\s*>{1,}/i;
    const closePlay = /close\\s*(?:&|and)\\s*play/i;
    const playBtn = /^(play|play video)$/i;
    const nodes = document.querySelectorAll('button, a, div, span, p, label, input');
    for (const el of nodes) {
        let txt = '';
        try { txt = (el.innerText || el.textContent || el.getAttribute('title') || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(); } catch (e) {}
        if (!txt || txt.length > 80) continue;
        let hit = false;
        if (kind === 'countdown') hit = skipIn.test(txt);
        else if (kind === 'skip') hit = skipAd.test(txt) && !skipIn.test(txt);
        else if (kind === 'closeplay') hit = closePlay.test(txt);
        else if (kind === 'play') hit = playBtn.test(txt);
        if (!hit) continue;
        try {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            if (r.width < 2 || r.height < 2) continue;
            if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0)
                continue;
            el.click();
            return txt;
        } catch (e) {}
    }
    return null;
}"""


async def frames_of(page):
    """Only the Eporner page (and same-origin child frames). Ad iframes
    often contain a fake Play control — clicking those is why the real
    Close & Play / Play Video buttons never fire."""
    try:
        frames = list(page.frames)
    except Exception:
        return [page]
    kept = []
    for frame in frames:
        try:
            frame_url = frame.url or ""
        except Exception:
            frame_url = ""
        if (
            _is_eporner_url(frame_url)
            or frame_url in ("", "about:blank", "about:srcdoc")
            or frame == page.main_frame
        ):
            kept.append(frame)
    return kept or [page]


def _page_source_url(page):
    return str(getattr(page, "_eporner_source_url", "") or "")


def _is_eporner_url(url: str) -> bool:
    host = ""
    try:
        host = (urlparse(str(url or "")).netloc or "").lower()
    except Exception:
        host = str(url or "").lower()
    return "eporner." in host


async def recover_if_left_eporner(page, log=None):
    """If an ad navigated the *same* tab off eporner, go back to the video page."""
    original = _page_source_url(page)
    try:
        current = page.url or ""
    except Exception:
        current = ""
    if _is_eporner_url(current):
        return
    if not original:
        return
    if log:
        log(f"  page left eporner for ad {current[:120]!r} — returning to video page")
    try:
        await page.goto(original, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
    try:
        await page.evaluate(_BLOCK_POPUPS_JS)
    except Exception:
        pass


async def dismiss_overlay_ads(page, log=None):
    """Click small overlay X/close controls — never Close & Play / Skip ad."""
    total = 0
    for frame in await frames_of(page):
        try:
            total += int(await frame.evaluate(_DISMISS_OVERLAY_JS) or 0)
        except Exception:
            continue
    if total and log:
        log(f"  dismissed {total} overlay close control(s)")


async def keep_eporner_tab(page, log=None, dismiss_overlays=True):
    """Deal with ads: close extra tabs/popups, bounce off-site navigations,
    dismiss overlay X buttons, stay on the Eporner video page."""
    try:
        context = page.context
    except Exception:
        context = None
    extras = []
    if context is not None:
        try:
            extras = [extra for extra in list(context.pages) if extra is not page]
        except Exception:
            extras = []
    for extra in extras:
        try:
            extra_url = extra.url
        except Exception:
            extra_url = ""
        if log:
            log(f"  extra tab/popup {extra_url or '(ad)'} — closing, staying on eporner")
        try:
            await extra.close()
        except Exception:
            pass
    try:
        await page.bring_to_front()
    except Exception:
        pass
    await recover_if_left_eporner(page, log)
    await dismiss_overlay_ads(page, log)


def attach_eporner_tab_guard(page, log):
    """Close ad popups/new tabs as they open so Playwright never leaves the video page."""

    async def on_popup(popup):
        if log:
            log("  ad popup opened — closing it, staying on eporner")
        try:
            await popup.close()
        except Exception:
            pass
        await keep_eporner_tab(page, log)

    async def on_page(new_page):
        if new_page is page:
            return
        if log:
            log("  extra browser tab opened — closing it, staying on eporner")
        try:
            await new_page.close()
        except Exception:
            pass
        await keep_eporner_tab(page, log)

    page.on("popup", on_popup)
    try:
        page.context.on("page", on_page)
    except Exception:
        pass


async def find_visible_text(page, kind):
    """kind is 'countdown' | 'skip' | 'closeplay' | 'play'. Returns (text, frame) or (None, None)."""
    for frame in await frames_of(page):
        try:
            text = await frame.evaluate(_VISIBLE_TEXT_JS, kind)
        except Exception:
            continue
        if text:
            return str(text).strip(), frame
    return None, None


async def click_visible_text(page, kind, log, label):
    for frame in await frames_of(page):
        try:
            text = await frame.evaluate(_CLICK_VISIBLE_TEXT_JS, kind)
        except Exception:
            continue
        if text:
            log(f"  clicked {label} {text!r} in {frame.url}")
            return True
    return False


async def click_playwright_texts(page, texts, log, label):
    """Click visible text only — never force-click hidden vjs-control-text."""
    for frame in await frames_of(page):
        for raw in texts:
            try:
                locator = frame.get_by_text(raw, exact=False).first
                if not await locator.count():
                    continue
                if not await locator.is_visible():
                    continue
                await locator.click(timeout=1500)
                log(f"  clicked {label} via text {raw!r} in {frame.url}")
                return True
            except Exception:
                continue
    return False


async def wait_for_skip_countdown(page, log, timeout_ms=SKIP_IN_WAIT_MS):
    """Keep looking for ``Skip in N`` for at least timeout_ms (default ~22s)."""
    elapsed = 0
    attempt = 0
    timeout_ms = max(timeout_ms, 3000)
    while elapsed < timeout_ms:
        await keep_eporner_tab(page, log)
        attempt += 1
        text, frame = await find_visible_text(page, "countdown")
        if text:
            log(f"  detected ad countdown {text!r} in {frame.url} after {elapsed / 1000:.1f}s")
            return text, frame
        if attempt == 1 or attempt % 5 == 0:
            log(f"  still waiting for 'Skip in' ({elapsed / 1000:.1f}s / {timeout_ms / 1000:.0f}s)")
        step = min(LOOK_POLL_MS, timeout_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    log("  'Skip in' did not appear")
    return None, None


async def click_skip_ad(page, log):
    """Click ``Skip ad >>`` once it is enabled after the countdown."""
    for selector in SKIP_AD_SELECTORS:
        el, frame = await find_in_frames(page, selector)
        if el:
            if await click_element(el, frame, selector, log):
                return True
    if await click_visible_text(page, "skip", log, "Skip ad"):
        return True
    if await click_playwright_texts(page, ("Skip ad >>", "Skip Ad >>", "Skip ad", "Skip Ad"), log, "Skip ad"):
        return True
    pattern = re.compile(r"skip\s*ad|skip\s*>+", re.IGNORECASE)
    for frame in await frames_of(page):
        try:
            locator = frame.get_by_text(pattern).first
            if await locator.count():
                await locator.click(timeout=1500, force=True)
                log(f"  clicked Skip ad via text locator in {frame.url}")
                return True
        except Exception:
            continue
    log("  Skip ad control not found")
    return False


async def click_skip_ad_with_retries(page, log, timeout_ms=SKIP_AD_LOOK_MS):
    """Keep trying Skip ad >> for at least ~3 seconds."""
    elapsed = 0
    attempt = 0
    timeout_ms = max(timeout_ms, 3000)
    while elapsed < timeout_ms:
        await keep_eporner_tab(page, log)
        attempt += 1
        log(f"  Skip ad try {attempt} ({elapsed / 1000:.1f}s)")
        if await click_skip_ad(page, log):
            return True
        step = min(LOOK_POLL_MS, timeout_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    log("  Skip ad was never clickable")
    return False


async def click_close_and_play(page, log):
    """Find and click in-player Close & Play (CSS, text, Playwright locators)."""
    for selector in CLOSE_PLAY_SELECTORS:
        el, frame = await find_in_frames(page, selector)
        if el:
            log(f"  Close & Play selector {selector!r} in {frame.url}")
            if await click_element(el, frame, selector, log):
                return True
    if await click_visible_text(page, "closeplay", log, "Close & Play"):
        return True
    if await click_playwright_texts(
        page,
        ("Close & Play", "Close and Play", "Close & play", "CLOSE & PLAY"),
        log,
        "Close & Play",
    ):
        return True
    return False


async def click_play_button(page, log):
    """Click the real Video.js play control — not the poster, which is often an ad.

    Live Eporner pages label this control ``Play Video``.
    """
    if await click_playwright_texts(page, ("Play Video",), log, "Play Video"):
        return True
    for selector in PLAY_BUTTON_SELECTORS:
        el, frame = await find_in_frames(page, selector)
        if el:
            log(f"  play button {selector!r} in {frame.url}")
            if await click_element(el, frame, selector, log):
                return True
    if await click_visible_text(page, "play", log, "Play"):
        return True
    return False


async def click_close_and_play_with_retries(page, log, timeout_ms=CLOSE_PLAY_LOOK_MS):
    elapsed = 0
    attempt = 0
    timeout_ms = max(timeout_ms, 3000)
    while elapsed < timeout_ms:
        await keep_eporner_tab(page, log, dismiss_overlays=False)
        attempt += 1
        log(f"  Close & Play try {attempt} ({elapsed / 1000:.1f}s)")
        if await click_close_and_play(page, log):
            return True
        step = min(LOOK_POLL_MS, timeout_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def click_play_button_with_retries(page, log, timeout_ms=PLAY_BUTTON_LOOK_MS):
    elapsed = 0
    attempt = 0
    timeout_ms = max(timeout_ms, 3000)
    while elapsed < timeout_ms:
        await keep_eporner_tab(page, log)
        attempt += 1
        log(f"  play-button try {attempt} ({elapsed / 1000:.1f}s)")
        if await click_play_button(page, log):
            return True
        step = min(LOOK_POLL_MS, timeout_ms - elapsed)
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def start_playback_and_skip_ad(page, log, found: dict):
    """Eporner sequence that actually exposes the HLS manifests:

    1. Search Close & Play for at least ~3s (CSS + visible text).
    2. If missing, click the real play button (not the poster/ad overlay).
    3. Keep closing ad popups / bouncing off-site navigations.
    4. Wait until ``Skip in N`` appears, wait ~5s, click ``Skip ad >>``.
    """
    log("  waiting for the player chrome...")
    try:
        await page.wait_for_selector(
            "button.vjs-big-play-button, .vjs-big-play-button, .vjs-inplayer-resume-button-text, video",
            timeout=10000,
        )
    except Exception:
        pass
    # Don't click overlay X while hunting Close & Play / Play Video — those
    # first clicks were being stolen by ad/close controls.
    await keep_eporner_tab(page, log, dismiss_overlays=False)

    started = await click_close_and_play_with_retries(page, log)
    if started:
        log("  Close & Play clicked")
    else:
        log("  Close & Play not found after 3s — clicking the play button")
        started = await click_play_button_with_retries(page, log)
        if started:
            log("  play button clicked")
        else:
            log("  no Close & Play / play button could be clicked")

    await keep_eporner_tab(page, log)

    if has_master_and_index(found):
        log("  master + index already captured after play click")
        return started

    log("  waiting for 'Skip in' countdown...")
    countdown, _frame = await wait_for_skip_countdown(page, log, timeout_ms=SKIP_IN_WAIT_MS)
    if countdown:
        log(f"  waiting {SKIP_AD_AFTER_COUNTDOWN_MS / 1000:.0f}s after 'Skip in' before clicking Skip ad")
        elapsed = 0
        while elapsed < SKIP_AD_AFTER_COUNTDOWN_MS:
            await keep_eporner_tab(page, log)
            if has_master_and_index(found):
                log("  master + index captured during the 5s wait")
                return started
            step = min(LOOK_POLL_MS, SKIP_AD_AFTER_COUNTDOWN_MS - elapsed)
            await page.wait_for_timeout(step)
            elapsed += step
        await click_skip_ad_with_retries(page, log)
    else:
        log("  'Skip in' never appeared — trying Skip ad anyway")
        await click_skip_ad_with_retries(page, log)

    await keep_eporner_tab(page, log)
    return started


async def try_click_play(page, log, found: dict, attempts: int = 1, retry_delay_ms: int = 1000):
    """Back-compat wrapper used by extract_m3u8_async."""
    started = await start_playback_and_skip_ad(page, log, found)
    return "player" if started else None


_EPORNER_ID_RE = re.compile(
    r"https?://(?:www\.)?eporner\.(?:com|eu)/(?:(?:hd-porn|embed)/|video-)(?P<id>\w+)",
    re.IGNORECASE,
)


def _eporner_encode_base36(num: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    num = int(num)
    if num == 0:
        return "0"
    out = []
    while num:
        num, rem = divmod(num, 36)
        out.append(alphabet[rem])
    return "".join(reversed(out))


def _eporner_player_hash(hex_hash: str) -> str:
    """Same transform Eporner's vjs.js / yt-dlp uses for /xhr/video/."""
    hex_hash = str(hex_hash or "").strip()
    if len(hex_hash) < 32:
        return ""
    hex_hash = hex_hash[:32]
    return "".join(_eporner_encode_base36(int(hex_hash[i:i + 8], 16)) for i in range(0, 32, 8))


def _eporner_page_id_hash_title(html: str, url: str) -> tuple[str, str, str]:
    title = ""
    title_match = re.search(r"<title>(.+?)\s*-\s*EPORNER", html or "", re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()
    id_match = _EPORNER_ID_RE.search(url or "") or _EPORNER_ID_RE.search(html or "")
    video_id = id_match.group("id") if id_match else ""
    hex_hash = ""
    for pattern in (
        r'hash\s*[:=]\s*[\'"]([\da-f]{32})',
        r'data-hash\s*=\s*[\'"]([\da-f]{32})',
        r'["\']hash["\']\s*:\s*["\']([\da-f]{32})',
    ):
        hash_match = re.search(pattern, html or "", re.IGNORECASE)
        if hash_match:
            hex_hash = hash_match.group(1)
            break
    return video_id, hex_hash, title


def _eporner_xhr_api_url(video_id: str, hex_hash: str) -> str:
    calc = _eporner_player_hash(hex_hash)
    if not video_id or not calc:
        return ""
    query = urlencode({
        "hash": calc,
        "device": "generic",
        "domain": "www.eporner.com",
        "fallback": "false",
    })
    return f"https://www.eporner.com/xhr/video/{video_id}?{query}"


def _eporner_collect_source_urls(payload) -> list[str]:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            src = node.get("src")
            if isinstance(src, str) and src.startswith("http"):
                found.append(src.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            text = node.strip()
            lower = text.lower().split("?", 1)[0]
            if text.startswith("http") and lower.endswith((".m3u8", ".mp4", ".mpd", ".webm")):
                found.append(text)

    walk(payload)
    ordered: list[str] = []
    seen = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


_DLOAD_ABS_RE = re.compile(
    r'https?://(?:www\.)?eporner\.(?:com|eu)/dload/[^\s"\'<>]+',
    re.IGNORECASE,
)
_DLOAD_REL_RE = re.compile(r'(/dload/[^\s"\'<>]+)', re.IGNORECASE)


def _http_get(url: str, headers: dict, timeout: int = 20) -> tuple[bytes, str]:
    """Fetch without a browser. urllib first, then curl (often survives TLS quirks)."""
    import urllib.request

    last_error = None
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read(), response.geturl() or url
    except Exception as exc:
        last_error = exc

    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        cmd = [
            curl, "-sS", "-L", "--compressed", "--max-time", str(timeout),
            "-A", headers.get("User-Agent") or USER_AGENT,
        ]
        for key, value in headers.items():
            if str(key).lower() == "user-agent":
                continue
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.extend(["-w", "\n__EP_FINAL_URL__%{url_effective}", url])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = proc.stdout or b""
            marker = b"\n__EP_FINAL_URL__"
            final = url
            if marker in raw:
                raw, _, tail = raw.rpartition(marker)
                final = tail.decode("utf-8", "replace").strip() or url
            if raw:
                return raw, final
            last_error = RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[:300] or f"curl exit {proc.returncode}")
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("http get failed")


def _eporner_quality(url: str) -> int:
    match = re.search(r"(\d{3,4})p", str(url or ""), re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _eporner_host_score(url: str) -> int:
    host = (urlparse(url).netloc or "").lower()
    path = (urlparse(url).path or "").lower()
    if host.startswith("vid-") or "-cdn.eporner" in host:
        return 3
    if host.startswith("gvideo."):
        return 0
    if "/dload/" in path:
        return 1
    return 2


def _order_eporner_media_urls(links: list[str]) -> list[str]:
    """Prefer h264 CDN mp4 over /dload/ wrappers, AV1, and HLS."""
    seen = []
    for item in links:
        item = str(item or "").strip()
        if item and item not in seen:
            seen.append(item)

    def rank(url: str):
        path = url.lower().split("?", 1)[0]
        is_mp4 = path.endswith(".mp4")
        is_hls = ".m3u8" in path
        kind = 2 if is_mp4 else (1 if is_hls else 0)
        not_av1 = 0 if "-av1" in path else 1
        return (kind, not_av1, _eporner_quality(url), _eporner_host_score(url))

    return sorted(seen, key=rank, reverse=True)


def _resolve_redirect_url(url: str, headers: dict, timeout: int = 12) -> str:
    """Follow /dload/ to the vid-* CDN without downloading the file."""
    hop_headers = dict(headers)
    hop_headers["Range"] = "bytes=0-0"
    try:
        _body, final = _http_get(url, hop_headers, timeout=timeout)
        if isinstance(final, str) and final.startswith("http"):
            return final
    except Exception:
        pass
    return url


def _eporner_dload_urls(html: str, page_url: str) -> list[str]:
    found = []
    for match in _DLOAD_ABS_RE.findall(html or ""):
        found.append(match)
    for match in _DLOAD_REL_RE.findall(html or ""):
        found.append(urljoin(page_url or "https://www.eporner.com/", match))
    return found


def _eporner_jsonld_urls(html: str) -> list[str]:
    found = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        blob = json.dumps(data)
        for item in re.findall(r'https?://[^\\s"\'<>]+', blob):
            lower = item.lower().split("?", 1)[0]
            if lower.endswith((".mp4", ".m3u8", ".webm")):
                found.append(item)
    return found


def extract_eporner_via_xhr(url: str, log) -> tuple[list[str], str]:
    """No-browser path: page HTML → /dload/ mp4s + /xhr/video JSON.

    Live Eporner pages already publish Download MP4 links (240p–1080p) and
    the player hash used by vjs.js. Ads / Close & Play are unnecessary.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.eporner.com/",
    }
    log("http: fetching video page (no browser)")
    try:
        body, page_url = _http_get(url, headers, timeout=20)
        html = body.decode("utf-8", "replace")
    except Exception as exc:
        log(f"http: page fetch failed ({exc})")
        return [], ""

    video_id, hex_hash, title = _eporner_page_id_hash_title(html, page_url or url)
    if not title:
        title_match = re.search(r"<title>(.+?)\s*-\s*EPORNER", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()

    collected: list[str] = []
    collected.extend(_eporner_dload_urls(html, page_url or url))
    collected.extend(_eporner_jsonld_urls(html))
    if collected:
        log(f"http: {len(collected)} download/json-ld media URL(s) in page HTML")

    api_url = _eporner_xhr_api_url(video_id, hex_hash)
    if not api_url:
        log(f"http: missing id/hash (id={video_id!r} hash={hex_hash!r}) — using page links only")
    else:
        log(f"http: GET /xhr/video/{video_id}")
        api_headers = dict(headers)
        api_headers["Accept"] = "application/json, text/javascript, */*;q=0.01"
        api_headers["Referer"] = page_url or url
        api_headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            raw, _final = _http_get(api_url, api_headers, timeout=20)
            payload = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(payload, dict) and payload.get("available") is False:
                log(f"http: xhr unavailable ({payload.get('message')})")
            else:
                xhr_links = _eporner_collect_source_urls(payload)
                log(f"http: xhr returned {len(xhr_links)} source(s)")
                collected.extend(xhr_links)
        except Exception as exc:
            log(f"http: xhr failed ({exc})")

    cdn_qualities = {
        _eporner_quality(item)
        for item in collected
        if (urlparse(item).netloc or "").lower().startswith("vid-")
        or "-cdn.eporner" in (urlparse(item).netloc or "").lower()
    }
    expanded: list[str] = []
    for item in collected:
        if "/dload/" in item.lower():
            quality = _eporner_quality(item)
            if quality in cdn_qualities or "-av1" in item.lower():
                continue
            final = _resolve_redirect_url(item, headers)
            if final != item:
                log(f"http: dload {quality}p -> {final}")
            expanded.append(final)
        else:
            expanded.append(item)

    ordered = _order_eporner_media_urls(expanded)
    mp4n = sum(1 for u in ordered if u.lower().split("?", 1)[0].endswith(".mp4"))
    hlsn = sum(1 for u in ordered if ".m3u8" in u.lower())
    log(f"http: {mp4n} mp4 + {hlsn} m3u8 playable URL(s) without browser")
    return ordered, title


async def extract_m3u8_async(url: str, log) -> tuple[list[str], str]:
    found: dict[str, None] = {}
    media_requests_seen: list[str] = []  # diagnostic only, used if we come up empty
    title = ""

    xhr_links, xhr_title = extract_eporner_via_xhr(url, log)
    if xhr_title:
        title = xhr_title
    if xhr_links:
        log("http: got playable sources without opening a browser")
        return xhr_links, title
    log("http: no sources - falling back to Close & Play / Play Video / Skip ad")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("playwright is not installed; cannot fall back to browser clicks")
        return [], title

    def ingest_xhr_payload(payload, origin: str = "xhr/video"):
        links = _eporner_collect_source_urls(payload)
        for item in links:
            if item not in found:
                found[item] = None
                log(f"  found ({origin}): {item}")

    def on_request(request):
        if (".m3u8" in request.url or ".mpd" in request.url) and request.url not in found:
            found[request.url] = None
            log(f"  found (request URL): {request.url}")

    async def on_response(response):
        resp_url = response.url or ""
        if "/xhr/video/" in resp_url and "heatmap" not in resp_url:
            try:
                payload = await response.json()
                ingest_xhr_payload(payload)
            except Exception:
                try:
                    ingest_xhr_payload(json.loads(await response.text()))
                except Exception:
                    pass
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
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if not HEADLESS and OFFSCREEN:
                log("  positioning window off-screen (OFFSCREEN=True) — you won't see it")
                launch_args += ["--window-position=-32000,-32000", "--window-size=1280,800"]
            launch_kwargs = {"headless": HEADLESS, "args": launch_args}
            if BROWSER_EXECUTABLE:
                launch_kwargs["executable_path"] = BROWSER_EXECUTABLE
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(user_agent=USER_AGENT)

        try:
            await context.add_init_script(_BLOCK_POPUPS_JS)
        except Exception:
            pass

        page = await context.new_page()
        page._eporner_source_url = url
        page.on("request", on_request)
        page.on("response", on_response)
        attach_eporner_tab_guard(page, log)

        log(f"loading {url} ...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.evaluate(_BLOCK_POPUPS_JS)
            except Exception:
                pass
            
            raw_title = await page.title()
            if raw_title:
                title = raw_title.strip()
                if title.endswith(" - Eporner"):
                    title = title[:-10].strip()
                elif title.endswith(" - HD porn video"):
                    title = title[:-16].strip()
                    
        except Exception as e:
            log(f"page load warning: {e}")

        await keep_eporner_tab(page, log, dismiss_overlays=False)

        if any(u.lower().split("?", 1)[0].endswith((".m3u8", ".mp4")) for u in found):
            log("  media already captured from /xhr/video — skipping Close & Play / ads")
        else:
            try:
                html = await page.content()
            except Exception:
                html = ""
            video_id, hex_hash, page_title = _eporner_page_id_hash_title(html, page.url or url)
            if page_title and not title:
                title = page_title
            for item in _order_eporner_media_urls(
                _eporner_dload_urls(html, page.url or url) + _eporner_jsonld_urls(html)
            ):
                if item not in found:
                    found[item] = None
                    log(f"  found (page html): {item}")
            api_url = _eporner_xhr_api_url(video_id, hex_hash)
            if api_url:
                log(f"  browser xhr: GET /xhr/video/{video_id}")
                try:
                    xhr_resp = await page.request.get(
                        api_url,
                        headers={
                            "Accept": "application/json, text/javascript, */*;q=0.01",
                            "Referer": page.url or url,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        timeout=20000,
                    )
                    ingest_xhr_payload(await xhr_resp.json(), origin="browser xhr")
                except Exception as exc:
                    log(f"  browser xhr failed ({exc})")
            if any(u.lower().split("?", 1)[0].endswith((".m3u8", ".mp4")) for u in found):
                log("  media from page/xhr — skipping Close & Play / ads")
            else:
                clicked = await try_click_play(page, log, found)
                if not clicked:
                    log("  Close & Play / Play Video could not be clicked")

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
            # Keep JSON on stdout; relay steps on stderr so the player can log them.
            print(msg, file=sys.stderr, flush=True)
            
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
