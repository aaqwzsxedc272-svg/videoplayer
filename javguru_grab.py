#!/usr/bin/env python3
"""
javguru_grab.py  —  Extract ALL stream URLs from a jav.guru video page.

HOW IT WORKS
────────────
1. A headless Chromium browser loads the jav.guru page.
   Cloudflare is satisfied by a real browser — no manual cookie juggling needed.

2. Every HTTP response is monitored via Playwright's response event.
   When a /searcho/ request fires, its redirect destination (Location header)
   is captured immediately at the network level.

3. For each stream button on the page the script simulates the real user flow:
      a. click the stream-selection button   (e.g. "STREAM SB", "STREAM AV")
      b. click  .middle .inner-box .playbutton   ← ad trigger, popunder ignored
      c. click  .play-overlay                    ← fires the /searcho/ request

4. The /searcho/ endpoint responds with a 3xx redirect to the actual player.
   That redirect destination is captured and printed/saved.

WHY "OPEN IN ANOTHER TAB IT DOESN'T WORK"
──────────────────────────────────────────
The /searcho/ endpoint validates every request against:
  • Referer  — must originate from jav.guru
  • Cloudflare clearance cookies — must belong to an active jav.guru session

Both conditions are met automatically here because the entire click sequence
happens inside the same browser session that loaded jav.guru. The browser sends
the right Referer and session cookies without any manual intervention.

USAGE
─────
    python javguru_grab.py                     # prompts for URL
    python javguru_grab.py <url>
    python javguru_grab.py <url> --visible     # show the browser window (debug)

REQUIREMENTS
────────────
    pip install playwright
    playwright install chromium

    # Optional — improves Cloudflare evasion significantly:
    pip install playwright-stealth
"""

import re
import sys
import os
import json
from html import unescape
from urllib.parse import urlparse

# ── Timing constants (milliseconds) ──────────────────────────────────────────
WAIT_PAGE_SETTLE    = 3_000   # extra settle time after goto() completes
WAIT_AFTER_STREAM   = 1_800   # after clicking a stream selection button
WAIT_AFTER_PLAYBTN  =   900   # after clicking .playbutton
WAIT_AFTER_OVERLAY  = 2_800   # after clicking .play-overlay (net request time)
# ─────────────────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_title(html: str) -> str | None:
    """
    Best-effort title extraction from raw page HTML.
    Priority: JSON-LD Article.headline → <title> tag → <h1> tag.
    """
    # 1. JSON-LD schema.org headline
    for m in re.finditer(
        r'<script\b[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1))
            nodes = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for node in nodes:
                if isinstance(node, dict) and node.get("@type") == "Article":
                    h = unescape(str(node.get("headline") or "")).strip()
                    if h:
                        return h
        except Exception:
            pass

    # 2. <title> tag — strip the " ✶ Jav Guru ⋆ …" suffix
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        t = unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
        t = re.sub(
            r'\s*[\u2736\u22c6\u2605\u2736\xb7\u2022|–\-]+\s*Jav\s*Guru.*$',
            '', t, flags=re.IGNORECASE,
        ).strip()
        if t:
            return t

    # 3. <h1>
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    if m:
        inner = unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
        if inner:
            return inner

    return None


def _find_in_page_or_frames(page, selector: str):
    """
    Search *selector* in the main page document, then in every child iframe.
    Returns the first ElementHandle found (or None if absent everywhere).

    The player overlay (.middle, .play-overlay) can end up inside an iframe
    depending on how jav.guru injects the player widget.
    """
    # Main frame first
    el = page.query_selector(selector)
    if el:
        return el

    # Child frames (iframes embedded in the page)
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            el = frame.query_selector(selector)
            if el:
                return el
        except Exception:
            pass

    return None


# ── Core extractor ────────────────────────────────────────────────────────────

def grab_all(javguru_url: str, visible: bool = False) -> tuple[str | None, dict[str, str]]:
    """
    Open *javguru_url* in a headless Chromium browser, simulate the click
    sequence for every stream button, and capture the player embed URL that
    the /searcho/ redirect resolves to.

    Returns
    ───────
    (title, {stream_label: player_embed_url})
      title            — video title extracted from the page (or None)
      stream_label     — e.g. "STREAM SB", "STREAM AV", "STREAM LU"
      player_embed_url — the actual player page after the /searcho/ redirect
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        sys.exit(
            "\n[!] Playwright is not installed. Run:\n"
            "        pip install playwright && playwright install chromium\n"
        )

    # Optional stealth mode (hides headless Chromium fingerprints from Cloudflare)
    stealth_fn = None
    try:
        from playwright_stealth import stealth_sync  # type: ignore
        stealth_fn = stealth_sync
        print("[*] playwright-stealth active — enhanced Cloudflare evasion")
    except ImportError:
        pass

    title: str | None = None
    results: dict[str, str] = {}

    with sync_playwright() as pw:
        # ── Launch browser ────────────────────────────────────────────────────
        browser = pw.chromium.launch(
            headless=not visible,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()

        if stealth_fn:
            stealth_fn(page)

        # ── Network interception ──────────────────────────────────────────────
        # capture["url"] holds the most-recently-seen /searcho/ redirect target.
        # It is reset to None before each stream button is clicked, then read
        # after the overlay click + settle wait.
        capture: dict[str, str | None] = {"url": None}

        def on_response(resp):
            url = resp.url
            if "/searcho/" not in url:
                return

            status   = resp.status
            location = resp.headers.get("location", "")
            loc_host = urlparse(location).netloc.lower()
            url_host = urlparse(url).netloc.lower()

            if status in (301, 302, 303, 307, 308) and location:
                # Redirect from /searcho/ → player
                if "jav.guru" not in loc_host:
                    print(f"    [net {status}] /searcho/ → {location}")
                    capture["url"] = location
                else:
                    print(f"    [net {status}] /searcho/ redirected back to jav.guru (CF check?)")

            elif status == 200 and "jav.guru" not in url_host:
                # /searcho/ itself returned 200 from an off-site host (rare)
                print(f"    [net 200]  /searcho/ final → {url}")
                capture["url"] = url

        page.on("response", on_response)

        # Capture any new tabs the player or ad might open
        # (some stream providers open in a new window instead of an iframe)
        new_tabs: list[str] = []
        new_page_objects = []

        def on_new_page(np):
            # Do not close the popup from inside the browser-context event
            # callback. Closing it synchronously can invalidate Chromium's
            # execution context while the main page is being re-rendered;
            # that is what caused intermittent "Cannot find context with
            # specified id" failures on later stream buttons.
            try:
                np.wait_for_load_state("domcontentloaded", timeout=8_000)
                u = np.url
                nh = urlparse(u).netloc.lower()
                if u and u != "about:blank" and "jav.guru" not in nh:
                    new_tabs.append(u)
                    print(f"    [new tab] {u}")
                new_page_objects.append(np)
            except Exception:
                try:
                    new_page_objects.append(np)
                except Exception:
                    pass

        ctx.on("page", on_new_page)

        # ── Step 1: Load the main page ────────────────────────────────────────
        print(f"\n[*] Loading: {javguru_url}")
        try:
            page.goto(javguru_url, wait_until="networkidle", timeout=60_000)
        except PwTimeout:
            # networkidle can time-out on heavy pages; domcontentloaded is enough
            print("  [!] networkidle timed out — falling back to domcontentloaded")
            try:
                page.goto(javguru_url, wait_until="domcontentloaded", timeout=60_000)
            except PwTimeout:
                print("  [!] Page failed to load — check the URL or your connection")
                browser.close()
                return None, {}

        page.wait_for_timeout(WAIT_PAGE_SETTLE)

        # Extract title from the live rendered HTML
        page_html = page.content()
        title = _extract_title(page_html)
        if not title:
            raw = page.title()
            title = re.sub(
                r'\s*[\u2736\u22c6\u2605\u2736\xb7\u2022|–\-]+\s*Jav\s*Guru.*$',
                '', raw, flags=re.IGNORECASE,
            ).strip() or None
        print(f"  Title : {title or '(not found)'}")

        # ── Step 2: Locate stream buttons ─────────────────────────────────────
        try:
            page.wait_for_selector("a.wp-btn-iframe__shortcode", timeout=12_000)
        except PwTimeout:
            print("  [!] No stream buttons appeared — page structure may have changed")
            browser.close()
            return title, {}

        def live_buttons():
            try:
                return page.query_selector_all("a.wp-btn-iframe__shortcode")
            except Exception as exc:
                # A provider/ad navigation can briefly destroy the document
                # execution context. Let the per-stream retry logic recover
                # instead of aborting the entire JavGuru capture.
                print(f"  [~] Stream-button context refreshed: {exc}")
                try:
                    page.wait_for_timeout(700)
                    return page.query_selector_all("a.wp-btn-iframe__shortcode")
                except Exception:
                    return []

        initial = live_buttons()
        stream_labels: list[tuple[int, str]] = [
            (i, (b.inner_text() or f"STREAM {i+1}").strip())
            for i, b in enumerate(initial)
        ]
        print(f"  Streams: {', '.join(lbl for _, lbl in stream_labels)}\n")

        # ── Step 3: Click each stream button in sequence ──────────────────────
        for btn_idx, label in stream_labels:
            print(f"[*] Processing: {label}")
            capture["url"] = None
            new_tabs.clear()

            # 3a. Click the stream selection button ───────────────────────────
            # Re-query AND scroll+click in a tight retry loop. jav.guru can
            # rebuild its DOM between the re-query and the click, detaching the
            # element reference we just obtained. Re-querying right before each
            # action and retrying on detachment errors fixes STREAM ST drops.
            clicked = False
            for _attempt in range(4):
                btns = live_buttons()
                if btn_idx >= len(btns):
                    if _attempt < 3:
                        # Buttons vanished entirely — page is likely doing a heavy
                        # re-render. Wait for them to come back before giving up.
                        print(f"  [~] Buttons gone on attempt {_attempt+1}, waiting for DOM to settle…")
                        try:
                            page.wait_for_selector("a.wp-btn-iframe__shortcode", timeout=5000)
                            page.wait_for_timeout(600)
                        except Exception:
                            page.wait_for_timeout(1200)
                        continue
                    else:
                        print(f"  [!] Button index {btn_idx} no longer in DOM — skipping\n")
                        break
                try:
                    btn = btns[btn_idx]
                    btn.scroll_into_view_if_needed()
                    # Re-query one more time immediately before click in case
                    # scroll_into_view triggered a DOM mutation
                    btns2 = live_buttons()
                    if btn_idx < len(btns2):
                        btns2[btn_idx].click()
                    else:
                        btn.click()
                    print(f"  [✔] Stream button clicked")
                    clicked = True
                    break
                except Exception as e:
                    err_msg = str(e)
                    if "not attached" in err_msg or "detached" in err_msg.lower():
                        print(f"  [~] Button detached on attempt {_attempt+1}, retrying…")
                        page.wait_for_timeout(400)
                        continue
                    print(f"  [!] Could not click stream button: {e} — skipping\n")
                    break
            if not clicked:
                continue

            page.wait_for_timeout(WAIT_AFTER_STREAM)

            # 3b. Click .middle .inner-box .playbutton ────────────────────────
            #     This is the ad trigger (pemsrv.com popunder). The popunder
            #     opens in a new tab which our on_new_page handler closes.
            #     The element may live inside a child iframe — _find_in_page_or_frames
            #     checks both the main document and all embedded iframes.
            pb_el = _find_in_page_or_frames(page, ".middle .inner-box .playbutton")
            if pb_el:
                try:
                    pb_el.scroll_into_view_if_needed()
                    pb_el.click()
                    print(f"  [✔] .inner-box .playbutton clicked")
                    page.wait_for_timeout(WAIT_AFTER_PLAYBTN)
                except Exception as e:
                    print(f"  [?] .playbutton click error: {e}")
            else:
                print(f"  [?] .inner-box .playbutton not found — skipping this step")

            # 3c. Click .play-overlay ─────────────────────────────────────────
            #     This is the real trigger: clicking it fires the /searcho/
            #     request with the correct Referer + session cookies already
            #     set by the browser, so the server redirect resolves properly.
            ov_el = _find_in_page_or_frames(page, ".play-overlay")
            if ov_el:
                try:
                    ov_el.scroll_into_view_if_needed()
                    ov_el.click()
                    print(f"  [✔] .play-overlay clicked  — waiting for network…")
                    # Wait for the /searcho/ HTTP request to complete
                    page.wait_for_timeout(WAIT_AFTER_OVERLAY)
                except Exception as e:
                    print(f"  [?] .play-overlay click error: {e}")
            else:
                print(f"  [?] .play-overlay not found")

            # ── Collect result ────────────────────────────────────────────────
            final_url = capture["url"] or (new_tabs[-1] if new_tabs else None)

            if final_url:
                results[label] = final_url
                print(f"  [✔] {label}  →  {final_url}")
            else:
                print(f"  [?] No URL captured for '{label}'")
                # Last-ditch: current page URL (only useful if player opened in-page)
                current_url = page.url
                if "jav.guru" not in urlparse(current_url).netloc:
                    results[label] = current_url
                    print(f"  [~] Falling back to current page URL: {current_url}")

            # Close popups only after this stream's URL has been collected and
            # outside the context event callback. Keep the main JavGuru page
            # alive for the next stream button.
            for popup in list(new_page_objects):
                try:
                    if not popup.is_closed():
                        popup.close()
                except Exception:
                    pass
            new_page_objects.clear()
            new_tabs.clear()

            print()   # blank line between streams

        browser.close()

    return title, results


# ── Output helpers ─────────────────────────────────────────────────────────────

def save_results(title: str | None, results: dict[str, str], out_dir: str) -> str:
    path = os.path.join(out_dir, "last_javguru.txt")
    with open(path, "w", encoding="utf-8") as f:
        if title:
            f.write(f"Title: {title}\n\n")
        if results:
            for lbl, url in results.items():
                f.write(f"{lbl}: {url}\n")
        else:
            f.write("No stream URLs found.\n")
    return path


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    raw_args = sys.argv[1:]
    visible_mode = "--visible" in raw_args
    url_args     = [a for a in raw_args if not a.startswith("--")]

    if url_args:
        url = url_args[0].strip()
    else:
        print("javguru_grab — Stream URL extractor for jav.guru")
        print("─" * 52)
        url = input("Enter jav.guru URL: ").strip()
        if not url:
            sys.exit("[!] No URL provided.")

    if not url.startswith("http"):
        url = "https://" + url

    title, results = grab_all(url, visible=visible_mode)

    # ── Summary ───────────────────────────────────────────────────────────────
    sep = "─" * 62
    print(sep)
    if title:
        print(f"  Title  : {title}")
    print(f"  Streams: {len(results)} found\n")

    for lbl, embed in results.items():
        print(f"  [{lbl}]")
        print(f"  {embed}\n")

    if not results:
        print("  [!] No stream URLs were extracted.")
        print("      Tips:")
        print("        • Run with  --visible  to watch the browser and debug")
        print("        • Install playwright-stealth for better CF bypass:")
        print("            pip install playwright-stealth")

    out_dir  = os.path.dirname(os.path.abspath(__file__))
    out_path = save_results(title, results, out_dir)
    print(f"{sep}")
    print(f"  Saved → {out_path}")

    # ── Usage note ────────────────────────────────────────────────────────────
    if results:
        print()
        print("  NOTE: Some player embeds also check the Referer header.")
        print("  If a URL doesn't load in your media player, try:")
        print("    mpv  --referrer='https://jav.guru/' <url>")
        print("    vlc  --http-referrer='https://jav.guru/' <url>")
        print("    yt-dlp --referer 'https://jav.guru/' <url>")

    sys.exit(0 if results else 1)