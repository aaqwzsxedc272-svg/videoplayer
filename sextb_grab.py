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
AD_PATH_TOKENS = ('/api/spots/', '/spots/', '/banner', '/popunder', '/pop.js',
                  # The episode API answers https://sextb.net/not-found when
                  # the request carries no solved Turnstile token, and that
                  # 404 page was reaching the playlist as a "stream".
                  '/not-found', '/404')

# Player hosts, mirroring generic_jav_grab.EMBED_HOST_TOKENS. sextb is the
# same kind of aggregator as jav.guru / roshy / javgg: the buttons resolve to
# an embed on one of these, and the stream is scraped out of that page.
# Matching against an ALLOWLIST is what stops the ad whack-a-mole -- every
# field report so far has been a new ad domain slipping past a blocklist
# (duq8bcrl.xyz, then t.dtscout.com).
PLAYER_HOST_TOKENS = (
    'dood', 'doply', 'all3do', 'd-s', 'do7go', 'vide0', 'playmogo',
    'ds2play', 'ds2video', 'dsvplay', 'doodcdn', 'doodstream',
    'streamtape', 'voe', 'mixdrop', 'mxdrop', 'pornfhd',
    'emturbovid', 'turbovid', 'javclan', 'vidara',
    'filelions', 'lulustream', 'luluvdo', 'streamwish', 'vidhide',
    'vidwatch', 'maxstream', 'turtleviplay', 'roshy',
    'filemoon', 'swhoi', 'awish', 'javstreamhq',
    'kinoger', 'ryderjet', 'smoothpre', 'dhtpre', 'peytonepre', 'earnvids',
    'kamehamehaa', 'kamehaus', 'veev.to', 'chillx', 'streamsb', 'ssbstream',
    'sextb.net',
    # The player sextb renders inside <div id="sextb-player"> on
    # https://sextb.net/jul-509-rm -- confirmed against the saved page.
    # trailerhg is deliberately NOT here: it is the preview trailer, and it
    # stays on AD_HOST_TOKENS so the film is never replaced by the trailer.
    'turboplays',
)

_MEDIA_SUFFIXES = ('.m3u8', '.m3u', '.mpd', '.mp4', '.m4v', '.webm', '.mkv')


def _is_media_url(candidate: str) -> bool:
    """True when the URL is a video file rather than a player page."""
    try:
        path = urlparse(candidate or '').path.lower()
    except Exception:
        return False
    return path.endswith(_MEDIA_SUFFIXES)


_MEDIA_SCAN_RES = (
    re.compile(r'https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', re.I),
    re.compile(r'https?://[^\s"\'<>\\]+?\.mp4[^\s"\'<>\\]*', re.I),
)


def _scrape_media_from_html(page_html: str) -> list:
    """Pull direct video URLs out of an embed page's markup.

    Same approach as generic_jav_grab: the markup carries the .m3u8/.mp4
    literally, often with escaped slashes.
    """
    out = []
    if not page_html:
        return out
    # Player pages routinely ship the URL with every slash escaped
    # ("https:\\/\\/cdn.example.net\\/x.m3u8"). Un-escape the whole document
    # first -- the regexes need a literal "://" to anchor on.
    page_html = page_html.replace('\\/', '/')
    for rx in _MEDIA_SCAN_RES:
        for m in rx.finditer(page_html):
            u = unescape(m.group(0)).replace('\\/', '/')
            low = u.lower()
            # the site's ~10 s preview must never stand in for the film
            if any(t in low for t in ('preview', 'trailer', 'sample')):
                continue
            if u not in out:
                out.append(u)
    return out


def _nested_player_iframes(page_html: str) -> list:
    """Player iframes embedded one level deeper in an embed page."""
    out = []
    if not page_html:
        return out
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', page_html, re.I):
        cand = unescape(m.group(1).strip()).replace('\\/', '/')
        if cand.startswith('//'):
            cand = 'https:' + cand
        if _looks_like_player(cand) and not _is_trailer_iframe(cand) and cand not in out:
            out.append(cand)
    return out


def _resolve_streams(streams, url, session) -> list:
    """Turn a list of player pages into playable media where possible.

    A page that yields no media is kept rather than dropped: a real player
    page is still better than an empty row, and _resolve_embed_page has
    already logged what it held.
    """
    resolved = []
    for cand in streams or []:
        if _is_media_url(cand):
            if cand not in resolved:
                resolved.append(cand)
            continue
        found = _resolve_embed_page(cand, url, session)
        if found:
            for u in found:
                if u not in resolved:
                    resolved.append(u)
        elif cand not in resolved:
            resolved.append(cand)
    return resolved


def _resolve_embed_page(embed_url: str, referer: str, session, depth: int = 0) -> list:
    """Fetch a player page and scrape the actual video out of it.

    turboplays.click/t/<id> is HTML, not a video, so the playlist needs the
    .m3u8/.mp4 it contains. One extra hop is allowed for nested players.
    """
    if depth > 1 or not embed_url:
        return []
    headers = {
        'User-Agent': _UA,
        'Referer': referer or 'https://sextb.net/',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        resp = session.get(embed_url, headers=headers, timeout=20)
        body = resp.text or ''
    except Exception as e:
        print(f"    [EMBED] {embed_url[:80]} -> fetch failed: {e}")
        return []

    found = _scrape_media_from_html(body)
    for nested in _nested_player_iframes(body):
        if nested == embed_url:
            continue
        found.extend(_resolve_embed_page(nested, embed_url, session, depth + 1))

    seen, out = set(), []
    for u in found:
        if u not in seen and _is_media_url(u):
            seen.add(u); out.append(u)

    if out:
        for u in out:
            print(f"    [EMBED] {embed_url[:60]} -> {u[:90]}")
    else:
        print(f"    [EMBED] {embed_url[:60]} -> no media in {len(body)} bytes,"
              f" head={body[:150]!r}")
    return out


def _is_trailer_iframe(candidate: str) -> bool:
    """True for the preview trailer, which must never be taken for the film.

    sextb puts one in the same document
    (<iframe id="IframeTrailer" src="https://trailerhg.xyz/e/xfu7jtpb70d9">)
    and it is a well-formed player URL that every other filter waves through.
    """
    low = (candidate or '').lower()
    return 'trailer' in low


def _is_self_embed(candidate: str, page_url: str | None) -> bool:
    """True for the watch page's own embed box (sextb.net/e/<same slug>).

    It is real markup, not a stream -- returning it would send the caller
    straight back to the page we just fetched.
    """
    if not page_url:
        return False
    low = (candidate or '').lower()
    if 'sextb.net/e/' not in low:
        return False
    try:
        slug = urlparse(page_url).path.strip('/').split('/')[0].lower()
    except Exception:
        return False
    return bool(slug) and f'/e/{slug}' in low


def _extract_inline_player(page_html: str, page_url: str | None = None) -> str | None:
    """Pull the active episode's player straight out of the watch-page HTML.

    sextb ships it already rendered, no API call and no browser needed:

        <div id="sextb-player" class="player" style="...">
          <iframe src="https://turboplays.click/t/6a80cae62b911?poster=...">
        </div>

    The same document also carries a trailer
    (<iframe id="IframeTrailer" src="https://trailerhg.xyz/e/...">), a
    self-embed of the watch page itself (sextb.net/e/<slug>, used for the
    "embed" box) and several ad iframes. All of those pass a naive iframe
    scan, so scope the search to the player container first.
    """
    if not page_html:
        return None

    # Preferred: the iframe inside the player container. Take a window after
    # the opening tag rather than matching to </div> -- the container markup
    # is not ours to assume stays balanced.
    for m in re.finditer(
            r'<div[^>]+(?:id=["\']sextb-player["\']|class=["\'][^"\']*player'
            r'(?:-wrapper)?[^"\']*["\'])[^>]*>',
            page_html, re.I):
        window = page_html[m.end():m.end() + 2000]
        for im in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', window, re.I):
            cand = unescape(im.group(1).strip())
            if (_looks_like_player(cand) and not _is_trailer_iframe(cand)
                    and not _is_self_embed(cand, page_url)):
                return cand

    # Fallback: first acceptable iframe in document order that is neither the
    # trailer nor the page's own embed.
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', page_html, re.I):
        cand = unescape(m.group(1).strip())
        if (_looks_like_player(cand) and not _is_trailer_iframe(cand)
                and not _is_self_embed(cand, page_url)):
            return cand
    return None


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
                # Gate it: without a solved Turnstile token the API answers
                # {"src": "https://sextb.net/not-found"} and that 404 page was
                # going straight into the playlist.
                if src and _looks_like_player(unescape(str(src))):
                    return unescape(str(src))
                if src:
                    print(f"    [API] {api_url} -> rejected src={str(src)[:70]!r}")
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

    # The active episode's player is already rendered in the document (inside
    # <div id="sextb-player">), so take it before spending anything on the
    # API -- which wants a Cloudflare Turnstile token we do not have.
    inline = _extract_inline_player(html, url)
    if inline:
        result['streams'].append(inline)
        print(f"  [+] inline player: {inline[:90]}")

    buttons = _extract_buttons(html)
    print(f"  Found {len(buttons)} episode buttons: {[b['label'] for b in buttons]} (session={session_kind})")
    
    seen = set(result['streams'])
    for btn in buttons:
        if btn['label'].upper().startswith('VIP'):
            continue  # skip VIP buttons
        
        stream_url = _fetch_episode_stream(btn['source'], btn['epid'], url, session)
        if stream_url and stream_url not in seen:
            seen.add(stream_url)
            result['streams'].append(stream_url)
            print(f"  [+] {btn['label']}: {stream_url[:80]}")
        
        time.sleep(0.5)

    # Everything collected so far is a player PAGE, not a video file. Take one
    # hop to scrape the .m3u8/.mp4 out of it -- otherwise the playlist gets an
    # HTML document and mpv has nothing to play. A page that yields no media is
    # kept rather than dropped: a real player page is still better than an
    # empty row, and the log above says what it held.
    result['streams'] = _resolve_streams(result['streams'], url, session)

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
        # The page opens with one hoster already loaded (TB on jul-509-rm);
        # record it so the click-through only collects the ones that differ.
        prev_src = _extract_inline_player(html, url)
        if prev_src:
            seen.add(prev_src)
            result['streams'].append(prev_src)
            print(f"  [+] initial player: {prev_src[:80]}")

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

                    # Scope to #sextb-player. The page also carries the
                    # trailer and its own embed box, and both pass a naive
                    # "first acceptable iframe" scan.
                    #
                    # A click only counts when the player actually CHANGED:
                    # TB is already loaded when the page opens, so reading the
                    # iframe without comparing would record TB again for every
                    # button and the other hosters would never be collected.
                    src_found = None
                    for html_check in ([early_html, html_now] if early_html else [html_now]):
                        cand = _extract_inline_player(html_check, url)
                        if cand and cand != prev_src:
                            src_found = cand
                            break
                    if src_found:
                        prev_src = src_found

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
    """Read the page statically, then click through the hosters in a browser.

    The static read gets the hoster the page opens with. The episode API is
    gated by Cloudflare Turnstile and answers "sextb.net/not-found" without a
    solved token, so the other hosters -- SW, PM, DD, FL, US, PP -- are only
    reachable by clicking their buttons, exactly as on jav.guru / roshy /
    javgg. The browser runs headless, so no window appears.
    """
    print(f"[sextb] Fetching: {url}")

    result = grab_all_static(url)
    static_streams = list(result.get('streams') or [])
    if static_streams:
        print(f"  [OK] Static grab found {len(static_streams)} stream(s)")
    else:
        print("  [!] Static grab found nothing")

    try:
        pw = grab_all_playwright(url, visible=visible)
    except Exception as e:
        print(f"  [!] Click-through failed: {e}")
        pw = {}

    extra = [u for u in (pw.get('streams') or []) if u not in static_streams]
    if extra:
        # The click-through yields player pages, so take the same second hop.
        try:
            session, _kind = _make_session()
        except Exception:
            session = None
        if session is not None:
            extra = _resolve_streams(extra, url, session)
        print(f"  [+] Click-through added {len(extra)} hoster(s)")

    merged = list(static_streams)
    for u in extra:
        if u not in merged:
            merged.append(u)
    if merged:
        result['streams'] = merged
    elif pw.get('streams'):
        result = pw
    if not result.get('title'):
        result['title'] = pw.get('title') or ''
    result.pop('error', None) if merged else None
    print(f"  [OK] sextb: {len(result.get('streams') or [])} stream(s) total")
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
