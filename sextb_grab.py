#!/usr/bin/env python3
"""
sextb_grab.py  -  Extract stream URLs from sextb.net video page.

Uses the sextb API endpoint directly: /api/episode/{data-source}/{data-id}
Then parses the iframe/stream from the response JSON.
Falls back to Playwright if the API approach fails.
"""

import os
import re
import sys
import json
import time
import base64
import tempfile
from shutil import which
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


def _is_fragment_candidate_ok(candidate: str) -> bool:
    """Filter for URLs inside a decrypted /ajax/player response."""
    if not candidate or _is_ad_iframe(candidate) or _is_trailer_iframe(candidate):
        return False
    try:
        if urlparse(candidate).path.lower().endswith(_IMAGE_SUFFIXES):
            return False
    except Exception:
        pass
    return True


def _extract_player_from_fragment(fragment: str) -> str | None:
    """Pull the hoster out of a decrypted /ajax/player response.

    Unlike the watch page, this fragment is not a page full of decoys: it comes
    from an endpoint authenticated by the rotating __pt token and holds exactly
    one thing, the hoster the user clicked. So there is deliberately no player
    allowlist here -- requiring one is what dropped SW, PM, US and PP while TB,
    DD and FL happened to be on the list. Ads, the trailer and non-media assets
    are still rejected.
    """
    if not fragment:
        return None
    for rx in (re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I),
               re.compile(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', re.I),
               re.compile(r'https?://[^\s"\'<>\\]+|//[A-Za-z0-9.-]+/[^\s"\'<>\\]*')):
        for m in rx.finditer(fragment):
            cand = unescape(m.group(1) if m.groups() else m.group(0)).strip()
            if cand.startswith('//'):
                cand = 'https:' + cand
            if _is_fragment_candidate_ok(cand):
                return cand
    return None


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


def _find_local_browser() -> str:
    """Prefer the user's installed Brave, then Chrome/Edge.

    sextb's buttons only resolve when the page's Cloudflare Turnstile widget
    has issued a token, and Turnstile reliably refuses bundled headless
    Chromium. Same policy as javdock_grab: a real installed browser, headed.
    The app overrides this through the BROWSER_EXECUTABLE module attribute.
    """
    override = globals().get('BROWSER_EXECUTABLE')
    if override and os.path.isfile(str(override)):
        return str(override)
    candidates = []
    if os.name == 'nt':
        program_files = os.environ.get('PROGRAMFILES', r'C:\Program Files')
        program_files_x86 = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        for root in (program_files, program_files_x86):
            for rel in (
                ('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
                ('Google', 'Chrome', 'Application', 'chrome.exe'),
                ('Microsoft', 'Edge', 'Application', 'msedge.exe'),
            ):
                candidates.append(os.path.join(root, *rel))
        if local_appdata:
            candidates.append(os.path.join(
                local_appdata, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'))
    else:
        candidates.extend(
            ('brave-browser', 'brave', 'google-chrome', 'chromium', 'microsoft-edge'))
    for cand in candidates:
        try:
            if os.path.isfile(cand) or which(cand):
                return cand
        except Exception:
            continue
    return ''


# ── The real player endpoint ────────────────────────────────────────────────
# sextb.js binds the buttons like this:
#
#   $('button.btn-player').on('click', function () {
#       var episode = $(this).attr('data-id');
#       $.post('/ajax/player', {episode: episode, filmId: filmId, pt: window.__pt},
#              function (response) {
#           var item = JSON.parse(response);
#           if (item.error) return;
#           var html = xorDecrypt(item.player_enc, window.__pk);
#           $('#sextb-player').html(html);
#           window.__pt = item.next_pt;
#           window.__pk = item.next_pk;
#       });
#   });
#
# So the hosters come from POST /ajax/player, NOT from /api/episode/... -- an
# endpoint that does not appear anywhere in sextb.js. filmId, __pt and __pk are
# all in the page HTML, the token rotates on every call, and no Turnstile is
# involved. That means no browser is needed to reach the other hosters.
_PLAYER_TOKEN_RES = (
    re.compile(r'var\s+filmId\s*=\s*(\d+)'),
    re.compile(r'window\.__pt\s*=\s*["\']([^"\']+)["\']'),
    re.compile(r'window\.__pk\s*=\s*["\']([^"\']+)["\']'),
)


def _extract_player_tokens(page_html: str):
    """Pull (filmId, __pt, __pk) out of the watch page.

    All three are emitted in an inline <script> near the top of the document.
    """
    if not page_html:
        return None
    vals = []
    for rx in _PLAYER_TOKEN_RES:
        m = rx.search(page_html)
        if not m:
            return None
        vals.append(m.group(1))
    return vals[0], vals[1], vals[2]


def _xor_decrypt(encoded: str, key: str) -> str:
    """Port of sextb.js xorDecrypt: base64-decode, then XOR against the key.

    The JS returns '' for an empty input or key; the key must be non-empty or
    `i % key.length` divides by zero.
    """
    if not encoded or not key:
        return ''
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return ''
    return ''.join(chr(raw[i] ^ ord(key[i % len(key)])) for i in range(len(raw)))


def _fetch_player_via_ajax(epid: str, film_id: str, pt: str, pk: str,
                           referer: str, session):
    """POST /ajax/player and return (decrypted_html, next_pt, next_pk).

    The token rotates on every call, so the caller must thread next_pt/next_pk
    through the remaining buttons or every call after the first fails.
    """
    headers = {
        'User-Agent': _UA,
        'Referer': referer or 'https://sextb.net/',
        'Origin': 'https://sextb.net',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': '*/*',
    }
    try:
        resp = session.post('https://sextb.net/ajax/player',
                            data={'episode': epid, 'filmId': film_id, 'pt': pt},
                            headers=headers, timeout=20)
    except Exception as e:
        print(f"    [AJAX] episode {epid} -> request failed: {e}")
        return None, pt, pk

    body = resp.text or ''
    try:
        item = json.loads(body)
    except Exception:
        print(f"    [AJAX] episode {epid} -> HTTP {resp.status_code}, not JSON,"
              f" {len(body)} bytes, head={body[:120]!r}")
        return None, pt, pk
    if not isinstance(item, dict):
        print(f"    [AJAX] episode {epid} -> unexpected payload {type(item).__name__}")
        return None, pt, pk
    if item.get('error'):
        print(f"    [AJAX] episode {epid} -> error={str(item.get('error'))[:60]!r}")
        return None, pt, pk

    enc = item.get('player_enc') or ''
    html = _xor_decrypt(enc, pk)
    if not html:
        print(f"    [AJAX] episode {epid} -> decrypted to nothing"
              f" (player_enc {len(enc)} chars, pk {len(pk)} chars)")
    return (html or None), (item.get('next_pt') or pt), (item.get('next_pk') or pk)


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


# sextb.net is on the player allowlist because its /e/ embeds are real, so
# every other file on that host used to pass too. The episode API answers
# "https://sextb.net/images/actor/amateur.jpg" for some films and that 404
# artwork reached the playlist as a stream.
_IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico',
                   '.css', '.js', '.json', '.txt', '.xml', '.woff', '.woff2')


def _looks_like_player(candidate: str) -> bool:
    """True when an iframe src is a player embed or a media file.

    The positive test, rather than another entry on the ad blocklist.
    """
    text = unescape((candidate or '').strip())
    if not text or _is_ad_iframe(text):
        return False
    try:
        if urlparse(text).path.lower().endswith(_IMAGE_SUFFIXES):
            return False
    except Exception:
        pass
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
    # <div id="sextb-player">), so take it first.
    inline = _extract_inline_player(html, url)
    if inline:
        result['streams'].append(inline)
        print(f"  [+] inline player: {inline[:90]}")

    buttons = _extract_buttons(html)
    print(f"  Found {len(buttons)} episode buttons: {[b['label'] for b in buttons]} (session={session_kind})")

    # filmId / __pt / __pk drive POST /ajax/player -- the call the site itself
    # makes when a button is clicked. It needs no Turnstile token, so the other
    # hosters are reachable over plain HTTP and no browser window is required.
    tokens = _extract_player_tokens(html)
    if tokens:
        film_id, pt, pk = tokens
    else:
        film_id = pt = pk = None
        print("  [!] filmId/__pt/__pk not in the page; falling back to /api/episode/")

    seen = set(result['streams'])
    for btn in buttons:
        if btn['label'].upper().startswith('VIP'):
            continue  # skip VIP buttons

        stream_url = None
        if tokens:
            # The token rotates on every response; thread it through or every
            # call after the first is rejected.
            page_html, pt, pk = _fetch_player_via_ajax(
                btn['epid'], film_id, pt, pk, url, session)
            if page_html:
                cand = _extract_player_from_fragment(page_html)
                if cand:
                    stream_url = cand
                else:
                    # Print the fragment: it is small and it is the ground
                    # truth for whichever hoster is still being missed.
                    flat = re.sub(r'\s+', ' ', page_html)[:220]
                    print(f"    [AJAX] {btn['label']}: decrypted {len(page_html)} chars,"
                          f" no player; fragment={flat!r}")
        else:
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
        # Turnstile defeats bundled headless Chromium, so launch the user's
        # installed browser HEADED with a persistent profile -- the same
        # approach javdock_grab uses for Cloudflare. Clearance and cookies
        # then survive between captures. The user agent is deliberately NOT
        # overridden: a real browser claiming a different UA is itself a
        # fingerprint mismatch.
        exe = _find_local_browser()
        profile_dir = os.path.join(tempfile.gettempdir(), 'sextb_capture_profile')
        ctx = None
        for attempt_exe in ([exe] if exe else []) + [None]:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=False,
                    executable_path=attempt_exe or None,
                    args=[
                        '--no-sandbox', '--disable-dev-shm-usage',
                        '--window-size=1280,800',
                    ],
                    # navigator.webdriver makes Turnstile refuse the token.
                    ignore_default_args=['--enable-automation'],
                    ignore_https_errors=True,
                    locale='en-US',
                    viewport={'width': 1280, 'height': 800},
                )
                print(f"    [browser] {'installed: ' + os.path.basename(attempt_exe) if attempt_exe else 'bundled chromium'}")
                break
            except Exception as launch_err:
                print(f"    [browser] {attempt_exe or 'bundled chromium'} failed: {launch_err}")
        if ctx is None:
            result['error'] = 'could not launch a browser'
            return result
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if stealth_fn:
            try:
                stealth_fn(page)
            except Exception:
                pass

        # Block navigations away from the main site to prevent ad hijacking
        def block_ads(route):
            req = route.request
            if req.is_navigation_request() and req.frame == page.main_frame and req.url != url:
                print(f"    [!] Blocked ad redirect to: {req.url}")
                route.abort()
            else:
                route.continue_()
        page.route("**/*", block_ads)

        # The buttons resolve through an AJAX call carrying a Turnstile token.
        # Log its status and body: if the click is firing but the token is
        # missing, the response says so instead of leaving us guessing.
        def log_api(resp):
            try:
                if '/api/episode/' not in resp.url:
                    return
                try:
                    body = (resp.text() or '')[:200]
                except Exception:
                    body = '<unreadable>'
                print(f"    [API-CLICK] HTTP {resp.status} {resp.url[-40:]} -> {body!r}")
            except Exception:
                pass
        page.on('response', log_api)

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
                        # Say what the player actually is, so "nothing changed"
                        # is distinguishable from "the player disappeared".
                        _cur = _extract_inline_player(html_now, url) if html_now else None
                        print(f"    [!] No change for {label} -- player still"
                              f" {(_cur or 'none')[:64]}. Retrying click...")
                        page.wait_for_timeout(1000)

            except Exception as btn_err:
                print(f"    [!] Error on {label}: {btn_err}. Skipping.")
                continue

        ctx.close()

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

    # /ajax/player reaches every hoster over plain HTTP, so the browser is a
    # last resort -- and it opens a visible window, which is not something to
    # do on every link.
    if len(static_streams) >= 2:
        print("  [OK] every hoster resolved without a browser")
        result['streams'] = static_streams
        print(f"  [OK] sextb: {len(static_streams)} stream(s) total")
        return result

    print("  [!] Fewer than two hosters over HTTP; falling back to a browser click-through")
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
