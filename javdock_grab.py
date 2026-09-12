#!/usr/bin/env python3
"""
javdock_grab.py  -  Extract stream URL from a javdock.com video page.

javdock.com has Cloudflare protection and requires a real browser with stealth.
The video is embedded via a Tapecontent/growcdnssedge HLS player.

Strategy:
1. Launch Playwright with stealth to pass Cloudflare
2. Wait for the player to fully initialize
3. Intercept the growcdnssedge/tapecontent m3u8 master playlist URL from the network
4. Return only the master m3u8 (not segment chunks)
"""

import os
import re
import sys
import time
import json
import tempfile
from shutil import which
from urllib.parse import urlparse, urlunparse, urljoin
from html import unescape

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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


def _is_master_m3u8(url: str) -> bool:
    """True if this is a master playlist (not a 240p/segment manifest)."""
    if 'master' in url and '.m3u8' in url:
        return True
    # e.g. /hls/124974172/master/124974172_480p.m3u8  ← these are quality-specific but playable
    if '.m3u8' in url and 'psch=' not in url:
        return True
    return False


def _find_local_browser() -> str:
    """Prefer the user's installed Brave, then Chrome/Edge.

    javdock/javhdporn sit behind Cloudflare, which reliably defeats
    headless Chromium - a real installed browser, HEADED, is required
    (same policy as the app's other CF capture flows). The app overrides
    this via the BROWSER_EXECUTABLE module attribute (it passes its own
    Brave discovery result in).
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


def _rank_and_dedupe_m3u8(m3u8_streams):
    """Order captured m3u8s best-first and drop redundant siblings.

    streamhls.click (javdock/javhdporn's PNG-wrapped HLS) loads a
    master.m3u8 plus one index-f*-v1-a1.m3u8 per quality. Keep only the
    master per stream token - the app's proxy unwraps the PNG segments
    and mpv switches renditions from the master itself. If the master
    was not captured, keep the highest-quality index instead.
    """
    def priority(u):
        lu = str(u).lower()
        if 'streamhls.click' in lu:
            if 'master.m3u8' in lu:
                return 2000
            q = re.search(r'f(\d+)-', lu)
            return 1500 + (int(q.group(1)) if q else 0)
        if 'doppiocdn' in lu and ('_auto' in lu or '/master/' in lu):
            return 1000
        if 'edge-hls' in lu and ('_auto' in lu or '/master/' in lu):
            return 900
        q = re.search(r'_(\d+)p\.m3u8', u)
        return int(q.group(1)) if q else 50

    # dict.fromkeys: stable de-duplication that keeps CAPTURE order for
    # equal priorities (set() would shuffle ties arbitrarily).
    ranked = sorted(list(dict.fromkeys(m3u8_streams)), key=priority, reverse=True)

    # Collapse streamhls siblings (master + index-fN) by their token.
    seen_tokens = set()
    collapsed = []
    for u in ranked:
        if 'streamhls.click' in str(u).lower():
            m = re.search(r'/hls/([A-Za-z0-9]+)/', u)
            token = m.group(1) if m else u
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
        collapsed.append(u)

    # Older dedupe for doppiocdn-style /hls/<digits>/ ids.
    seen_ids = set()
    deduped = []
    for u in collapsed:
        m = re.search(r'/hls/(\d+)/', u)
        sid = m.group(1) if m else u
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        deduped.append(u)
    return deduped


def _probe_streamhls_durations(streams, page_url):
    """Fetch each streamhls playlist and measure its duration.

    Masters are followed into their first variant playlist; durations are
    the sum of #EXTINF values. This is how the ~10 s PREVIEW streams these
    sites load first get told apart from the full movie. Returns
    {url: seconds}; failures map to 0.0 and non-streamhls URLs are skipped.
    """
    durations = {}
    try:
        import curl_cffi.requests as cfreq
    except ImportError:
        return durations

    def _sum_extinf(text):
        try:
            return sum(float(m) for m in re.findall(r'#EXTINF:([\d.]+)', text))
        except Exception:
            return 0.0

    for u in streams or []:
        if 'streamhls.click' not in str(u).lower():
            continue
        try:
            r = cfreq.get(u, impersonate='chrome131', timeout=10,
                          headers={'Referer': page_url, 'Accept': '*/*'})
            text = r.text or ''
            if '#EXT-X-STREAM-INF' in text:
                variant = ''
                lines = [l.strip() for l in text.splitlines()]
                for i, l in enumerate(lines):
                    if l.startswith('#EXT-X-STREAM-INF'):
                        for j in range(i + 1, len(lines)):
                            if lines[j] and not lines[j].startswith('#'):
                                variant = lines[j]
                                break
                        break
                if variant:
                    r2 = cfreq.get(urljoin(u, variant), impersonate='chrome131',
                                   timeout=10,
                                   headers={'Referer': page_url, 'Accept': '*/*'})
                    text = r2.text or ''
            durations[u] = _sum_extinf(text)
        except Exception:
            durations[u] = 0.0
    return durations


def _finalize_streams(captured, page_url):
    """Pick the best captured URLs: direct embeds > the LONGEST streamhls
    master > other m3u8s. Returns (streams, preview_only)."""
    direct_streams = [
        u for u in captured
        if ('tapecontent' in u or 'streamtape' in u or 'cloudatacdn' in u)
        and not _is_image_asset(u)
    ]
    m3u8_streams = [u for u in captured if '.m3u8' in u]
    if direct_streams:
        return direct_streams, False
    if not m3u8_streams:
        return [], False
    ranked = _rank_and_dedupe_m3u8(m3u8_streams)
    durations = _probe_streamhls_durations(ranked, page_url)
    if not durations:
        return ranked, False
    streamhls_ranked = [u for u in ranked if u in durations]
    others = [u for u in ranked if u not in durations]
    streamhls_ranked.sort(key=lambda u: durations.get(u, 0.0), reverse=True)
    best = streamhls_ranked[0]
    keep = [best]
    m = re.search(r'/hls/([A-Za-z0-9]+)/', best)
    best_tok = m.group(1) if m else best
    for u in streamhls_ranked[1:]:
        m2 = re.search(r'/hls/([A-Za-z0-9]+)/', u)
        tok = m2.group(1) if m2 else u
        if tok != best_tok and durations.get(u, 0.0) >= 300.0:
            keep.append(u)
    for u, d in sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:3]:
        print(f'  [duration] {d:8.1f}s  {u[:110]}')
    preview_only = durations.get(best, 0.0) < 60.0
    if preview_only:
        print('  [!] Only short (preview) streamhls streams were captured')
    return keep + others, preview_only


# Ad networks these pages load (popunders, same-tab hijacks, banners).
# Blocking at the network level also CANCELS a same-tab hijack outright:
# a top-level navigation to an aborted URL never happens.
_AD_NET_HOSTS = (
    'whitetrafsa', 'exoclick', 'trafficjunky', 'juicyads', 'popads',
    'popcash', 'propellerads', 'clickadu', 'adnxs', 'doubleclick',
    'googlesyndication', 'taboola', 'outbrain', 'creativecdn', 'adsterra',
    'hilltopads', 'mgid', 'revcontent', 'zedo', 'clickiocdn', 'adsrvr',
    'smartadserver', 'lijit', 'sonobi', 'tradedoubler', 'adcash',
)

# Hosts the capture tab is allowed to sit on; anything else after a play
# click is a same-tab ad hijack.
_ALLOWED_NAV_HOSTS = (
    'javdock.com', 'javhdporn.net', 'streamhls.click', 'tiktokcdn',
    'cloudflare', 'about:', 'data:', 'chrome',
)


def _ad_route_handler(route, request):
    """Abort ad-network requests, pass everything else through."""
    try:
        u = str(request.url or '').lower()
        if any(h in u for h in _AD_NET_HOSTS):
            route.abort()
            return
    except Exception:
        pass
    try:
        route.continue_()
    except Exception:
        pass


def _recover_if_hijacked(page, hijack_state):
    """If the tab was navigated to an ad site, go back and re-click play.

    Belt & suspenders for hijacks that slip past _ad_route_handler
    (e.g. redirect chains through unknown hosts)."""
    try:
        u = str(page.url or '')
        if u and not any(a in u.lower() for a in _ALLOWED_NAV_HOSTS):
            if hijack_state.get('n', 0) >= 3:
                print('  [!] repeated same-tab hijacks - no more recoveries')
                return False
            hijack_state['n'] = hijack_state.get('n', 0) + 1
            print(f'  [!] same-tab ad hijack ({u[:90]}) -> going back')
            page.go_back(timeout=15000)
            page.wait_for_timeout(4000)
            if page.locator('.play-button').count():
                page.locator('.play-button').first.click(force=True, timeout=5000)
                print('  [*] play re-clicked after hijack recovery')
            return True
    except Exception:
        pass
    return False


_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.ico', '.bmp')


def _is_image_asset(u):
    """Thumbnails/banners (e.g. thumb.tapecontent.net/thumb/xx.jpg) must
    never count as captured streams - they match the naive 'tapecontent'
    substring check and end up played as a 'video' (a contact-sheet jpg)."""
    lu = str(u or '').lower()
    try:
        p = urlparse(lu).path or ''
    except Exception:
        p = lu
    return (lu.startswith('https://thumb.') or '/thumb' in p
            or p.endswith(_IMAGE_EXTS))


def _is_tapecontent_video(u):
    return ('tapecontent' in u or 'streamtape' in u) and not _is_image_asset(u)


def _page_alive(page):
    try:
        _ = page.url
        return True
    except Exception:
        return False


def _fallback_to_javhdporn(url: str) -> dict:
    """Try the matching javhdporn page when javdock serves its unavailable iframe."""
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    parts = [p for p in (parsed.path or '').split('/') if p]
    if 'javdock.com' not in host or len(parts) < 2 or parts[0].lower() != 'video':
        return {'title': '', 'streams': []}

    fallback_url = urlunparse(('https', 'www.javhdporn.net', f'/video/{parts[1].strip("/")}/', '', '', ''))
    try:
        import generic_jav_grab
    except Exception as exc:
        return {'title': '', 'streams': [], 'error': f'javhdporn fallback unavailable: {exc}'}

    data = generic_jav_grab.grab_all(fallback_url) or {}
    streams = data.get('streams') or []
    if streams:
        print(f'  [fallback] javhdporn: {fallback_url}')
        return {
            'title': data.get('title') or '',
            'streams': streams,
        }
    return {'title': data.get('title') or '', 'streams': []}


def grab_all(url: str, visible: bool = False) -> dict:
    result = {'title': '', 'streams': []}

    # NOTE: the static javhdporn mirror fallback runs AFTER the browser
    # flow now. The streamhls.click HLS links these pages serve only
    # appear once the page's .play-button div is clicked and the player
    # starts fetching - a static HTML scan finds only junk embeds and
    # would short-circuit the real capture.
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        fallback = _fallback_to_javhdporn(url)
        if fallback.get('streams'):
            return fallback
        result['error'] = 'playwright not installed'
        return result

    stealth_fn = None
    try:
        from playwright_stealth import stealth
        stealth_fn = stealth
    except ImportError:
        pass

    launch_args = [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars',
        '--window-size=1280,800',
    ]
    # Keep the real headed Chromium engine required by Cloudflare, but do not
    # expose its page on the user's desktop.  ``visible=True`` remains a
    # debugging opt-in for the standalone grabber; the app calls the default
    # off-screen mode and receives the captured HLS URL directly.
    if visible:
        launch_args.append('--window-position=60,60')
    else:
        launch_args.append('--window-position=-32000,-32000')

    with sync_playwright() as pw:
        # javdock/javhdporn are Cloudflare-protected: launch the user's
        # installed browser (Brave first) HEADED - never headless, which
        # CF reliably defeats. Fall back to Playwright's bundled Chromium
        # (still headed) when no installed browser is found.
        #
        # Persistent context: the window looks like a regular browser
        # (real profile UI, no blank automation shell) and Cloudflare
        # clearance + cookies persist between captures, so later links
        # typically pass CF without any challenge at all.
        browser = None
        exe = _find_local_browser()
        profile_dir = os.path.join(tempfile.gettempdir(), 'javdock_capture_profile')
        for attempt_exe in ([exe] if exe else []) + [None]:
            try:
                browser = pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=False,
                    executable_path=attempt_exe or None,
                    args=launch_args,
                    # Hide the automation switch: it flips navigator.webdriver
                    # and drives the site player's 'streaming unavailable'
                    # refusal (observed even for MANUAL clicks in the window).
                    ignore_default_args=['--enable-automation'],
                    ignore_https_errors=True,
                    locale='en-US',
                )
                if attempt_exe:
                    print(f'  [browser] launched installed browser: {os.path.basename(attempt_exe)}')
                else:
                    print('  [browser] launched bundled Chromium (no installed browser found)')
                break
            except Exception as exc:
                print(f'  [!] browser launch failed ({(attempt_exe or "bundled chromium")}): {exc}')
                browser = None
        if browser is None:
            fallback = _fallback_to_javhdporn(url)
            if fallback.get('streams'):
                return fallback
            result['error'] = 'could not launch a browser for capture'
            return result
        ctx = browser  # persistent context IS the context

        # Reuse the context's initial page instead of opening another tab.
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        except Exception:
            page = ctx.new_page()
        # The persistent profile RESTORES the previous session - every tab
        # from earlier captures (including leftover ad pages) reopens.
        # Close them all so the window starts clean and stale tabs can't
        # fire popups or pollute the capture.
        try:
            for _p in list(ctx.pages or []):
                if _p is not page:
                    try:
                        _p.close()
                    except Exception:
                        pass
        except Exception:
            pass
        if stealth_fn:
            try:
                stealth_fn(page)
            except Exception:
                pass

        # Popunders: clicking play on these sites routinely fires an ad
        # window.open. Close popups instantly so they can't steal focus,
        # contribute ad-player m3u8s to our capture, or accumulate tabs.
        # IMPORTANT: our OWN tabs are tracked in _our_pages (with a
        # creation flag for the new_page() event race) - ctx 'page' events
        # fire for every new tab, including ours (closing our own tab is
        # what once made the window vanish instantly).
        _popup_urls = []
        _our_pages = set()
        _creating = {'now': False}
        try:
            _our_pages.add(page)
        except Exception:
            pass

        def on_popup(p):
            try:
                if _creating['now'] or p in _our_pages:
                    return
                pu = p.url or ''
                if pu and pu != 'about:blank':
                    _popup_urls.append(pu)
                print(f'  [popup closed] {pu[:110]}')
                p.close()
            except Exception:
                pass

        ctx.on('page', on_popup)

        # ── Anti-automation-detection ───────────────────────────────
        # The site's player refuses to stream for automated browsers -
        # 'sorry video streaming unavailable' appeared even for MANUAL
        # clicks inside the window. Playwright exposes
        # navigator.webdriver=true by default; hide it in every frame
        # and give the page a plain window.chrome shape. (Automating the
        # user's MAIN Brave profile instead is not possible: modern
        # Chromium forbids remote debugging on the default profile.)
        try:
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:function(){return undefined;}});"
                "try{if(!window.chrome){window.chrome={};}"
                "if(!window.chrome.runtime){window.chrome.runtime={};}}catch(e){}"
            )
        except Exception:
            pass

        # Block ad networks at the network level: no popunders, no
        # banners - and a blocked top-level navigation cancels the
        # same-tab ad hijack outright.
        try:
            ctx.route('**/*', _ad_route_handler)
        except Exception:
            pass

        _hijack = {'n': 0}
        _reloads = {'n': 0}

        def _handle_streaming_unavailable(cur_page):
            """The site's own player sometimes gives up right after the
            play click ('sorry video streaming unavailable'). A reload +
            re-click usually gets the stream going. Max twice."""
            try:
                _body_l = (cur_page.content() or '').lower()
                if ((('streaming unavailable' in _body_l)
                        or ('video unavailable' in _body_l))
                        and _reloads['n'] < 2):
                    _reloads['n'] += 1
                    print('  [!] Site player: streaming unavailable - reloading and re-clicking play')
                    cur_page.reload(wait_until='domcontentloaded', timeout=60000)
                    cur_page.wait_for_timeout(5000)
                    if cur_page.locator('.play-button').count():
                        cur_page.locator('.play-button').first.click(force=True, timeout=5000)
                    return True
            except Exception:
                pass
            return False

        # Capture streams across ALL frames (main page + iframes)
        captured = []

        def on_request(req):
            u = req.url
            frame_url = ''
            try:
                frame_url = req.frame.url or ''
            except Exception:
                frame_url = ''
            if _popup_urls and frame_url and any(pu and pu in frame_url for pu in _popup_urls):
                return  # request belongs to a closed ad popup
            if 'tapecontent.net' in u:
                if _is_image_asset(u):
                    return  # thumbnail/banner, not a stream
                if u not in captured:
                    captured.append(u)
                    print(f'  [net] tapecontent: {u[:120]}')
                return
            if 'cloudatacdn.com' in u:
                if u not in captured:
                    captured.append(u)
                    print(f'  [net] cloudatacdn: {u[:120]}')
                return
            if 'streamtape' in u and '.mp4' in u:
                if u not in captured:
                    captured.append(u)
                    print(f'  [net] streamtape mp4: {u[:120]}')
                return

            # Capture AUTHENTICATED doppiocdn sub-playlist (the one with real segments)
            # These have ?psch=v2&pkey= and come from media-hls.doppiocdn.media
            if '.m3u8' in u and 'psch=v2' in u and 'pkey=' in u:
                if 'doppiocdn.media' in u and 'video.javdock.com' in frame_url:
                    if u not in captured:
                        captured.append(u)
                        print(f'  [net] doppio-auth: {u[:120]}')
                return  # skip growcdnssedge psch (preview only)

            # m3u8 streams - filter out ads and keepalive pings
            if '.m3u8' in u:
                if 'doppiocdn.media' in u and 'video.javdock.com' not in frame_url:
                    return
                if any(x in u for x in ('growcdnssedge', 'psch=v2', 'pkey=', 'ping.m3u8')):
                    return
                if u not in captured:
                    captured.append(u)
                    print(f'  [net] m3u8: {u[:120]} frame={frame_url[:70]}')

        ctx.on('request', on_request)

        # Human-like opening (field-proven in the user's main Brave): the
        # video page refuses to stream when it is a window's FIRST load,
        # but plays after a human navigates to it. Warm up on the site
        # homepage first, then open the video URL as a NEW TAB.
        try:
            _parsed = urlparse(url)
            site_root = f'{_parsed.scheme or "https"}://{_parsed.netloc}/'
        except Exception:
            site_root = 'https://www.javdock.com/'
        try:
            page.goto(site_root, wait_until='domcontentloaded', timeout=60_000)
            print(f'[*] Warm-up: {site_root}')
            page.wait_for_timeout(4000)
        except Exception as warm_exc:
            print(f'  [!] homepage warm-up failed: {warm_exc}')
        _creating['now'] = True
        try:
            video_page = ctx.new_page()
            _our_pages.add(video_page)
        except Exception:
            video_page = page
        finally:
            _creating['now'] = False
        if video_page is not page and stealth_fn:
            try:
                stealth_fn(video_page)
            except Exception:
                pass
        page = video_page

        print(f'[*] Loading: {url}')
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        except PwTimeout:
            result['error'] = 'Page load timed out'
            try:
                browser.close()
            except Exception:
                pass
            return result

        # Wait for Cloudflare challenge to pass (if any)
        # The page will redirect after challenge is solved
        try:
            page.wait_for_function(
                "!document.title.includes('Just a moment') && !document.title.includes('Verifying')",
                timeout=60_000
            )
        except PwTimeout:
            # The window is visible: if Cloudflare shows its checkbox,
            # the user can click it manually while we keep waiting.
            print("  [!] Cloudflare may not have resolved (click the checkbox in the window if one is shown)")

        # Give player time to initialize (growcdnssedge preview needs ~6s to create video element)
        page.wait_for_timeout(8000)

        html = page.content()
        result['title'] = _extract_title(html)
        print(f"  Title: {result['title'] or '(none)'}")

        def has_real_stream():
            return any(
                _is_tapecontent_video(u) or 'cloudatacdn' in u
                or ('doppiocdn.media' in u and '.m3u8' in u)
                or 'streamhls.click' in u
                for u in captured
            )

        # Try clicking the play button - iterate ALL frames including cross-origin iframes
        # Skip ad/analytics frames
        AD_FRAME_HOSTS = ('whitetrafsa', 'googlesyndication', 'doubleclick', 'adnxs',
                          'taboola', 'outbrain', 'creativecdn', 'exoclick', 'trafficjunky',
                          'pornfhd.com', 'popads', 'popcash')
        def _is_ad_frame(url: str) -> bool:
            if any(h in url for h in AD_FRAME_HOSTS):
                return True
            # Banner ad files always have pixel dimensions in URL like 300x250.html, 728x90.html
            if re.search(r'\d+x\d+', url):
                return True
            # Other ad path patterns
            if 'banner_' in url or '/ads/' in url or 'ad_frame' in url:
                return True
            return False
        if not has_real_stream():
            # Wait for ANY video element to appear (player initialises asynchronously)
            # then click it to trigger the real stream load
            try:
                try:
                    page.evaluate(
                        "() => { if (typeof _0x44e232 !== 'undefined') _0x44e232 = false; window._0x44e232 = false; }"
                    )
                except Exception:
                    pass
                if page.locator('.play-button').count():
                    page.locator('.play-button').first.click(force=True, timeout=5000)
                    print(f"  [*] Clicked javdock play button")
                else:
                    page.wait_for_selector('video', timeout=12000)
                    page.locator('video').first.click(force=True)
                    print(f"  [*] Clicked video element via page locator")
            except Exception as e:
                print(f"  [!] video click failed: {e}")
                page.screenshot(path="javdock_fail.png")
                with open("javdock_fail.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("  [!] Dumped javdock_fail.png and javdock_fail.html")
                # Fallback: iterate frames and click the first video we find
                for frame in page.frames:
                    if _is_ad_frame(frame.url):
                        continue
                    try:
                        el = frame.query_selector('video')
                        if el:
                            el.click()
                            print(f"  [*] Clicked video in frame: {frame.url[:80]}")
                            break
                    except Exception:
                        pass
            for _tick in range(4):
                page.wait_for_timeout(2000)
                if (_recover_if_hijacked(page, _hijack)
                        or _handle_streaming_unavailable(page)):
                    page.wait_for_timeout(3000)

        if not _page_alive(page):
            print('  [!] capture tab was closed - finalizing with what was captured so far')

        # Check DOM for tapecontent iframes
        html = page.content() if _page_alive(page) else ''
        for m in re.finditer(r'<iframe[^>]*src=["\']([^"\']*(?:tapecontent\.net|streamtape)[^"\']*)["\']', html, re.IGNORECASE):
            src = m.group(1).replace('\\/', '/')
            if src.startswith('//'):
                src = 'https:' + src
            if src not in captured:
                captured.append(src)
                print(f"  [dom] tapecontent iframe: {src[:100]}")

        # Wait more for network stream to arrive
        if _page_alive(page) and not has_real_stream():
            for _tick in range(3):
                page.wait_for_timeout(2000)
                if _recover_if_hijacked(page, _hijack):
                    page.wait_for_timeout(3000)

            # Re-check DOM one last time
            html = page.content()
            for m in re.finditer(r'<iframe[^>]*src=["\']([^"\']*(?:tapecontent\.net|streamtape)[^"\']*)["\']', html, re.IGNORECASE):
                src = m.group(1).replace('\\/', '/')
                if src.startswith('//'):
                    src = 'https:' + src
                if src not in captured:
                    captured.append(src)
                    print(f"  [dom] tapecontent iframe (late): {src[:100]}")

        # ── Preview vs full movie ──────────────────────────────────
        # These pages load a ~10 s PREVIEW stream first; the full movie's
        # m3u8 only appears when the preview finishes or the play overlay
        # is clicked again. Poll for a second streamhls token instead of
        # returning with the preview. (Direct tapecontent/doppio streams
        # need no polling.)
        def _streamhls_tokens():
            toks = set()
            for u in captured:
                if 'streamhls.click' in u and '.m3u8' in u:
                    m = re.search(r'/hls/([A-Za-z0-9]+)/', u)
                    toks.add(m.group(1) if m else u)
            return toks

        _has_direct = any(
            _is_tapecontent_video(u) or ('cloudatacdn' in u)
            or ('doppiocdn.media' in u)
            for u in captured
        )
        if _page_alive(page) and not _has_direct:
            try:
                _poll_start = time.time()
                _first_tokens = _streamhls_tokens()
                _first_seen_at = time.time() if _first_tokens else None
                _last_nudge = 0.0
                while True:
                    page.wait_for_timeout(2000)
                    if _recover_if_hijacked(page, _hijack):
                        continue
                    _now = time.time()
                    if _now - _poll_start >= 75:
                        break
                    _toks = _streamhls_tokens()
                    if _toks and _toks != _first_tokens:
                        print(f'  [*] New stream token appeared ({len(_toks)} total) - full stream likely loading')
                        page.wait_for_timeout(3000)
                        break
                    if _first_seen_at is not None and _now - _first_seen_at >= 40:
                        break  # the preview had ample time to hand over
                    if _now - _last_nudge >= 8:
                        _last_nudge = _now
                        try:
                            if _handle_streaming_unavailable(page):
                                continue
                            if page.locator('.play-button').count():
                                page.locator('.play-button').first.click(force=True, timeout=2000)
                                print('  [*] Clicked play button again (post-preview nudge)')
                        except Exception:
                            pass
            except Exception as poll_exc:
                print(f'  [!] preview poll interrupted: {poll_exc}')

        try:
            browser.close()
        except Exception:
            # Tab/context may already be gone (hijack + manual close) -
            # the captured list is still worth finalizing.
            pass

    # Pick the best captures: direct embeds > the LONGEST streamhls
    # master (full movie, not the 10 s preview) > other m3u8s.
    streams, preview_only = _finalize_streams(captured, url)
    if streams:
        result['streams'] = streams
        if preview_only:
            result['preview_only'] = True
    else:
        # Last resort: the static javhdporn mirror of this page.
        fallback = _fallback_to_javhdporn(url)
        if fallback.get('streams'):
            result['title'] = fallback.get('title') or result['title']
            result['streams'] = fallback['streams']
            return result
        result['error'] = 'No stream URL captured'

    return result


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: javdock_grab.py <url> [--visible]'}))
        sys.exit(1)
    target_url = sys.argv[1]
    vis = '--visible' in sys.argv
    data = grab_all(target_url, visible=vis)
    print(json.dumps(data))
