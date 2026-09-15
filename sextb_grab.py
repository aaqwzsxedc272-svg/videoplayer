#!/usr/bin/env python3
"""
sextb_grab.py  -  Extract stream URLs from sextb.net video page.

Uses the sextb API endpoint directly: /api/episode/{data-source}/{data-id}
Then parses the iframe/stream from the response JSON.
Falls back to Playwright if the API approach fails.
"""

import re
import sys
import json
import time
from urllib.parse import urlparse
from html import unescape

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

WAIT_PAGE_SETTLE   = 3_000
WAIT_AFTER_CLICK   = 3_500


# Hosts whose iframes are advertising rather than a player.
AD_HOST_TOKENS = ('z5g022gc', 'trailerhg', 'googlesyndication', 'doubleclick',
                  'adsbygoogle', 'asg-interstitial', 'ads.', '/ads/',
                  # t.dtscout.com/idg/?su=... -- the tracker the TB button
                  # resolved to on jul-509-rm and nima-081-sub.
                  'dtscout', 'exoclick', 'juicyads', 'trafficjunky',
                  'popcash', 'popads', 'adsterra')

# Ad networks serve the creative from a "spot" endpoint, so the path gives it
# away even when the hostname is a random throwaway .xyz.
AD_PATH_TOKENS = ('/api/spots/', '/spots/', '/banner', '/popunder', '/pop.js')

# Player hosts, mirroring generic_jav_grab.EMBED_HOST_TOKENS. sextb is the
# same kind of aggregator as jav.guru / roshy / javgg: the buttons resolve to
# an embed on one of these, and the stream is scraped out of that page.
# Matching against an ALLOWLIST is what stops the ad whack-a-mole -- every
# field report so far has been a new ad domain slipping past a blocklist
# (duq8bcrl.xyz, then t.dtscout.com).
PLAYER_HOST_TOKENS = (
    'dood', 'doply', 'all3do', 'd-s', 'do7go', 'vide0', 'playmogo',
    'ds2play', 'ds2video', 'dsvplay', 'doodcdn', 'doodstream',
    'streamtape', 'voe', 'mixdrop', 'mxdrop', 'pornfhd', 'trailerhg',
    'emturbovid', 'turbovid', 'javclan', 'vidara',
    'filelions', 'lulustream', 'luluvdo', 'streamwish', 'vidhide',
    'vidwatch', 'maxstream', 'turtleviplay', 'roshy',
    'filemoon', 'swhoi', 'awish', 'javstreamhq',
    'kinoger', 'ryderjet', 'smoothpre', 'dhtpre', 'peytonepre', 'earnvids',
    'kamehamehaa', 'kamehaus', 'veev.to', 'chillx', 'streamsb', 'ssbstream',
    'sextb.net',
)

_MEDIA_SUFFIXES = ('.m3u8', '.m3u', '.mpd', '.mp4', '.m4v', '.webm', '.mkv')


def _looks_like_player(candidate: str) -> bool:
    """True when an iframe src is a player embed or a media file.

    The positive test, rather than another entry on the ad blocklist.
    """
    text = unescape((candidate or '').strip())
    if not text or _is_ad_iframe(text):
        return False
    low = text.lower()
    host = (urlparse(text).netloc if '//' in text else '').lower()
    if host and any(tok in host for tok in PLAYER_HOST_TOKENS):
        return True
    path = urlparse(text).path.lower() if '//' in text else low
    return any(path.endswith(suf) for suf in _MEDIA_SUFFIXES)


# An unexpanded tracking macro. A real player URL never carries one, but an ad
# tag rendered outside its own loader does: the field report captured
#     //duq8bcrl.xyz/api/spots/346725?p=1&s1=%subid1%&kw=
# which sailed past the host list and was added to the playlist as a stream.
_AD_MACRO_RE = re.compile(r'%[A-Za-z_][A-Za-z0-9_]*%')


def _is_ad_iframe(candidate: str) -> bool:
    """True when an iframe src is an advert, or otherwise not a usable stream."""
    text = (candidate or '').strip()
    if not text:
        return True
    low = text.lower()
    # Placeholder srcs the player leaves in the DOM before a real source
    # loads. Field: every button on bank-096-rm and aldn-072-rm yielded
    # `javascript:false`, which is not a URL at all — and because both pages
    # produced the identical string, the second video was then dropped as a
    # DUPLICATE and only one row reached the playlist.
    if low.startswith(('javascript:', 'about:', 'data:', 'blob:', 'vbscript:', '#')):
        return True
    if any(t in low for t in AD_HOST_TOKENS):
        return True
    if any(t in low for t in AD_PATH_TOKENS):
        return True
    if _AD_MACRO_RE.search(text):
        return True
    return False


def _extract_title(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        t = unescape(m.group(1)).strip()
        if t: return t
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        t = unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())
        if t: return t
    return ''


def _extract_buttons(html: str) -> list[dict]:
    """Parse all episode buttons from sextb page HTML."""
    buttons = []
    # Match: <button class="btn-player episode" data-source="16922333" data-id="3968335">..TB..
    for m in re.finditer(
        r'<button\s[^>]*class="btn-player\s+episode[^"]*"[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
        html, re.DOTALL | re.IGNORECASE
    ):
        label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if label:
            buttons.append({
                'label': label,
                'source': m.group(1),
                'epid': m.group(2),
            })
    # Also try data-id before data-source ordering
    if not buttons:
        for m in re.finditer(
            r'<button\s[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*class="[^"]*episode[^"]*"[^>]*>(.*?)</button>',
            html, re.DOTALL | re.IGNORECASE
        ):
            label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if label:
                buttons.append({
                    'label': label,
                    'source': m.group(1),
                    'epid': m.group(2),
                })
    return buttons


def _make_session():
    """Build the most Cloudflare-capable session available.

    Plain requests gets a hard 403 from the sextb episode API — every one of
    the six /api/episode/ calls on bank-096-rm and on aldn-072-rm came back
    403 in the field, while the HTML page itself loaded fine. Cloudflare
    fingerprints the TLS handshake, not just the headers, so no amount of
    header tuning fixes it. curl_cffi impersonates a real Chrome and is what
    main.py already uses for exactly this reason; the others are fallbacks so
    the script still runs where it is not installed.
    """
    try:
        import curl_cffi.requests as cfreq
        return cfreq.Session(impersonate='chrome131'), 'curl_cffi'
    except Exception:
        pass
    try:
        import cloudscraper
        return cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows'}), 'cloudscraper'
    except Exception:
        pass
    import requests
    return requests.Session(), 'requests'


def _fetch_episode_stream(source_id: str, epid: str, referer: str, session) -> str | None:
    """
    Call the sextb API to get the player URL for a given episode ID.
    Returns the player iframe src URL or None.

    The session is supplied by the caller (see _make_session) — this function
    does not need requests itself, and importing it here used to make the call
    fail outright on a machine without requests even though a perfectly good
    curl_cffi session had been passed in.
    """
    api_url = f"https://sextb.net/api/episode/{source_id}/{epid}"
    headers = {
        'User-Agent': _UA,
        'Referer': referer,
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/plain, */*',
    }
    
    try:
        resp = session.get(api_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"    [API] {api_url} -> HTTP {resp.status_code}")
            return None
        ctype = str(resp.headers.get('Content-Type') or '').lower()
        body = resp.text or ''
        if 'json' in ctype:
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict):
                src = (
                    data.get('src') or data.get('url') or
                    data.get('embed') or data.get('iframe') or
                    data.get('link') or data.get('stream')
                )
                if src:
                    return src
                body = data.get('html') or data.get('content') or body
        # Not JSON -- or JSON with none of the keys above. Once curl_cffi got
        # past the 403 the response stopped parsing as JSON entirely
        # ("Expecting value: line 1 column 1"), so treat the body as markup
        # and scrape it the way generic_jav_grab scrapes an embed page.
        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', body, re.IGNORECASE):
            cand = unescape(m.group(1).strip())
            if _looks_like_player(cand):
                return cand
        for m in re.finditer(r'https?://[^\s"\'<>]+', body):
            cand = unescape(m.group(0).strip())
            if _looks_like_player(cand):
                return cand
        print(f"    [API] {api_url} -> 200 {ctype or 'no content-type'},"
              f" {len(body)} bytes, no player; head={body[:150]!r}")
    except Exception as e:
        print(f"    [API] {api_url} -> error: {e}")
    
    return None


def grab_all_static(url: str) -> dict:
    """
    Try to extract streams without launching a browser:
    1. Fetch the page HTML
    2. Parse button data-source and data-id attributes
    3. Call the /api/episode/ endpoint for each button
    """
    result = {'title': '', 'streams': []}
    
    try:
        session, session_kind = _make_session()
    except Exception as e:
        result['error'] = f'no HTTP session available: {e}'
        return result

    headers = {
        'User-Agent': _UA,
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://sextb.net/',
    }

    try:
        resp = session.get(url, headers=headers, timeout=30)
        html = resp.text
    except Exception as e:
        result['error'] = f'Page fetch failed: {e}'
        return result
    
    result['title'] = _extract_title(html)
    buttons = _extract_buttons(html)
    print(f"  Found {len(buttons)} episode buttons: {[b['label'] for b in buttons]} (session={session_kind})")
    
    seen = set()
    for btn in buttons:
        if btn['label'].upper().startswith('VIP'):
            continue  # skip VIP buttons
        
        stream_url = _fetch_episode_stream(btn['source'], btn['epid'], url, session)
        if stream_url and stream_url not in seen:
            seen.add(stream_url)
            result['streams'].append(stream_url)
            print(f"  [+] {btn['label']}: {stream_url[:80]}")
        
        time.sleep(0.5)
    
    return result


def grab_all_playwright(url: str, visible: bool = False) -> dict:
    """
    Playwright fallback: click each button and capture the iframe src.
    """
    result = {'title': '', 'streams': []}
    
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        result['error'] = 'playwright not installed'
        return result

    stealth_fn = None
    try:
        from playwright_stealth import stealth_sync
        stealth_fn = stealth_sync
    except ImportError:
        pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not visible,
            args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--window-size=1280,800',
            ],
        )
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={'width': 1280, 'height': 800},
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
        )
        page = ctx.new_page()
        if stealth_fn:
            stealth_fn(page)

        # Block navigations away from the main site to prevent ad hijacking
        def block_ads(route):
            req = route.request
            if req.is_navigation_request() and req.frame == page.main_frame and req.url != url:
                print(f"    [!] Blocked ad redirect to: {req.url}")
                route.abort()
            else:
                route.continue_()
        page.route("**/*", block_ads)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        except PwTimeout:
            result['error'] = 'Page load timed out'
            browser.close()
            return result

        page.wait_for_timeout(WAIT_PAGE_SETTLE)

        # Collect button data upfront from HTML (data-id is stable)
        html = page.content()
        result['title'] = _extract_title(html)
        print(f"  Title: {result['title'] or '(none)'}")

        btn_info = []
        for m in re.finditer(
            r'<button\s[^>]*class="btn-player\s+episode[^"]*"[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
            html, re.DOTALL | re.IGNORECASE
        ):
            label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if label and not label.upper().startswith('VIP'):
                btn_info.append({'label': label, 'source': m.group(1), 'epid': m.group(2)})

        # Also try alternate attribute ordering
        if not btn_info:
            for m in re.finditer(
                r'<button\s[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
                html, re.DOTALL | re.IGNORECASE
            ):
                label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
                if label and not label.upper().startswith('VIP'):
                    btn_info.append({'label': label, 'source': m.group(1), 'epid': m.group(2)})

        print(f"  Found {len(btn_info)} stream buttons: {[b['label'] for b in btn_info]}")

        seen = set()
        for btn in btn_info:
            label = btn['label']
            epid  = btn['epid']
            try:
                selector = f'button[data-id="{epid}"]'
                print(f"  [*] Clicking: {label} (epid={epid})")
                
                # If button is completely missing from DOM for some reason, recover
                if page.locator(selector).count() == 0:
                    print("    [!] Button missing. Recovering page...")
                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        page.wait_for_timeout(WAIT_PAGE_SETTLE)
                    except Exception:
                        pass

                iframe_found = False
                for attempt in range(2):
                    try:
                        # Use JS click - completely immune to any ad overlay interception
                        page.evaluate(f'''
                            (function() {{
                                var btn = document.querySelector('button[data-id="{epid}"]');
                                if (btn) {{ btn.dispatchEvent(new MouseEvent('click', {{bubbles: true}})); }}
                            }})()
                        ''')
                    except Exception as e:
                        print(f"    [!] Click failed: {e}")
                        break

                    # Read HTML IMMEDIATELY (500ms) — doodstream iframe URL appears
                    # in the page HTML right after the click but before any navigation
                    page.wait_for_timeout(500)
                    early_html = None
                    try:
                        early_html = page.content()
                    except Exception:
                        pass

                    page.wait_for_timeout(WAIT_AFTER_CLICK - 500)

                    # If ad hijacked the page, reload sextb before reading HTML
                    try:
                        current_url = page.url
                        if 'sextb.net' not in current_url:
                            print(f"    [!] Ad redirected to {current_url[:60]}. Reloading...")
                            page.goto(url, wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(WAIT_PAGE_SETTLE)
                    except Exception:
                        pass

                    # Read from HTML (safe even if context was destroyed by navigation)
                    try:
                        html_now = page.content()
                    except Exception:
                        break

                    src_found = None
                    # Scan entire HTML for any iframe that's not an ad
                    # After clicking, the player iframe is usually the only non-ad iframe present
                    # Try the early snapshot first (captures doodstream before redirect)
                    for html_check in ([early_html, html_now] if early_html else [html_now]):
                        if not html_check:
                            continue
                        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html_check, re.IGNORECASE):
                            # iframe src attributes arrive HTML-escaped, so a
                            # query separator shows up as &amp; and would
                            # corrupt the URL if passed on as-is.
                            candidate = unescape(m.group(1).strip())
                            if _looks_like_player(candidate):
                                src_found = candidate
                                break
                        if src_found:
                            break

                    if src_found and src_found not in seen:
                        seen.add(src_found)
                        result['streams'].append(src_found)
                        print(f"  [+] {label}: {src_found[:80]}")
                        iframe_found = True

                    if iframe_found:
                        break
                    else:
                        print(f"    [!] Iframe not loaded for {label}. Retrying click...")
                        page.wait_for_timeout(1000)

            except Exception as btn_err:
                print(f"    [!] Error on {label}: {btn_err}. Skipping.")
                continue

        browser.close()

    return result


def grab_all(url: str, visible: bool = False) -> dict:
    """Try static API first, then Playwright as fallback."""
    print(f"[sextb] Fetching: {url}")
    
    # First attempt: static API call (fast, no browser needed)
    result = grab_all_static(url)
    if result.get('streams'):
        print(f"  [OK] Static grab found {len(result['streams'])} stream(s)")
        return result
    
    print("  [!] Static grab failed, trying Playwright...")
    result = grab_all_playwright(url, visible=visible)
    return result


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: sextb_grab.py <url> [--visible]'}))
        sys.exit(1)
    target_url = sys.argv[1]
    vis = '--visible' in sys.argv
    data = grab_all(target_url, visible=vis)
    print(json.dumps(data))
