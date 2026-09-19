#!/usr/bin/env python3
"""
generic_jav_grab.py  ?"  Extract stream URLs from generic JAV/video pages.
"""

import os
import sys
import json
import re
import tempfile
import traceback
from html import unescape
from urllib.parse import (urlparse, urljoin, urlunparse, parse_qsl, urlencode)
import base64

try:
    import curl_cffi.requests as cfreq
except ImportError:
    print(json.dumps({'error': 'curl_cffi is required. pip install curl_cffi'}))
    sys.exit(1)

# R45: embed-player hosters recognized on javgg / jav aggregator pages.
# The old hard-coded list (dood/streamtape/voe/mixdrop/...) missed the
# hosts javgg actually uses today — emturbovid, javclan, vidara — so those
# buttons' embeds were dropped from the capture. Keep this list broad:
# every host here has (or gets) a dedicated resolver in main.py.
EMBED_HOST_TOKENS = (
    'dood', 'doply', 'all3do', 'd-s', 'do7go', 'vide0', 'playmogo',
    'ds2play', 'ds2video', 'dsvplay', 'doodcdn', 'doodstream',
    'streamtape', 'voe', 'voe-unblock', 'voeunblock', 'voeunblck',
    'mixdrop', 'mxdrop', 'pornfhd', 'trailerhg', 'sextb',
    'javhdporn', 'javdock',
    # R45 field-named hosts + common jav-embed families
    'emturbovid', 'turbovid', 'javclan', 'vidara',
    'filelions', 'lulustream', 'luluvdo', 'streamwish', 'vidhide',
    'vidwatch', 'maxstream', 'turtleviplay', 'roshy',
    # R48: javgg/javguru-style packed-JS hosters (Server SW/VH/TB on javgg)
    'filemoon', 'swhoi', 'awish', 'javstreamhq',
    'kinoger', 'ryderjet', 'smoothpre', 'dhtpre', 'peytonepre', 'earnvids',
    # R49: jav.guru mirror hosters (field-named)
    'kamehamehaa', 'kamehaus',
    # R56: additional dood/voe aliases
    'dood.li', 'dood.yt', 'dood.to', 'dood.so', 'dood.watch', 'dood.pm',
    'dood.sh', 'dood.ws', 'dood.cx', 'dood.la', 'dood.one',
    'voe.sx', 'voe.ru', 'voe.ws', 'voe.is', 'veev.to', 'govoe', 'chillx',
    'eugenemakedraw', 'javlesbians',
)


# ── Bot gates ─────────────────────────────────────────────────────────────────
# supjav answers a plain HTTP GET with 403 from a Cloudflare-style gate while
# serving the page to a real browser. Same policy as javdock_grab: the user's
# own installed browser, HEADED (headless is reliably defeated), off-screen so
# it never appears on the desktop, with a persistent profile so the clearance
# cookie survives between captures and later links pass with no challenge.

def _find_local_browser() -> str:
    override = globals().get('BROWSER_EXECUTABLE')
    if override and os.path.isfile(str(override)):
        return str(override)
    from shutil import which
    candidates = []
    if os.name == 'nt':
        roots = [os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                 os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')]
        for root in roots:
            for rel in (('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
                        ('Google', 'Chrome', 'Application', 'chrome.exe'),
                        ('Microsoft', 'Edge', 'Application', 'msedge.exe')):
                candidates.append(os.path.join(root, *rel))
        _lad = os.environ.get('LOCALAPPDATA', '')
        if _lad:
            candidates.append(os.path.join(
                _lad, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'))
    else:
        candidates.extend(('brave-browser', 'brave', 'google-chrome',
                           'chromium', 'microsoft-edge'))
    for cand in candidates:
        try:
            if os.path.isfile(cand) or which(cand):
                return cand
        except Exception:
            continue
    return ''


def _browser_fetch(url: str, referer: str = '', settle_ms: int = 2500):
    """(final_url, html) for *url* through a real browser, or ('', '').

    Used both to read a gated watch page and to follow a gated redirect: the
    returned final_url is where the browser actually ended up, which for a
    redirect page is the answer.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[grab] playwright not installed -- cannot pass a bot gate',
              file=sys.stderr)
        return '', ''
    final, html = '', ''
    try:
        with sync_playwright() as pw:
            browser = None
            exe = _find_local_browser()
            profile = os.path.join(tempfile.gettempdir(), 'generic_jav_profile')
            args = ['--no-sandbox', '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars', '--window-size=1280,800',
                    '--window-position=-32000,-32000']
            for attempt in ([exe] if exe else []) + [None]:
                try:
                    browser = pw.chromium.launch_persistent_context(
                        profile, headless=False,
                        executable_path=attempt or None, args=args,
                        ignore_default_args=['--enable-automation'],
                        ignore_https_errors=True, locale='en-US')
                    break
                except Exception as exc:
                    print(f'[grab] browser launch failed '
                          f'({attempt or "bundled chromium"}): {exc}',
                          file=sys.stderr)
                    browser = None
            if browser is None:
                return '', ''
            try:
                ctx_opts = {}
                if referer:
                    ctx_opts['referer'] = referer
                page = browser.new_page(**ctx_opts)
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
                try:
                    page.wait_for_load_state('networkidle', timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(int(settle_ms or 0))
                final = str(page.url or '')
                html = page.content() or ''
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        print(f'[grab] browser fetch failed: {exc}', file=sys.stderr)
        return '', ''
    return final, html


def _looks_blocked(status, body='') -> bool:
    """True for the responses a bot gate answers with instead of the page."""
    try:
        code = int(status or 0)
    except Exception:
        code = 0
    if code in (403, 429, 503):
        return True
    text = str(body or '')[:4000].lower()
    return ('cf-browser-verification' in text or 'challenge-platform' in text
            or 'just a moment' in text or 'checking your browser' in text
            or 'attention required' in text)


# ── supjav.com ────────────────────────────────────────────────────────────────
# supjav is an aggregator whose watch page carries no stream at all -- only a
# row of server buttons whose data-link is an encrypted token. The token goes
# to the player endpoint the page itself iframes (supjav.php?l=<token> on a
# supremejav host), and that hop is what finally names the real hoster:
# sextb, jav.guru, roshy, javgg. So the grab has to make that hop before the
# m3u8 / embed scanning below can find anything.

_SUPJAV_SERVER_RE = re.compile(
    r'<a[^>]+class="btn-server([^"]*)"[^>]+data-link="([0-9a-fA-F]+)"'
    r'[^>]*>\s*([^<]*?)\s*<', re.IGNORECASE)
_SUPJAV_IFRAME_RE = re.compile(
    r'<iframe[^>]+src=["\']([^"\']*supjav\.php[^"\']*)["\']', re.IGNORECASE)
# Hosts that are part of supjav's own plumbing or its ad network. A redirect
# landing back on one of these is not an answer.
_SUPJAV_OWN_HOSTS = ('supjav', 'supremejav', 'mayzaent', 'mnaspm', 'eix304')


def supjav_player_template(html: str) -> str:
    """The supjav.php URL the page itself iframes, with its token intact."""
    m = _SUPJAV_IFRAME_RE.search(str(html or ''))
    return unescape(m.group(1)) if m else ''


def supjav_server_links(html: str) -> list:
    """[(label, player_url)] for a supjav watch page, active server first.

    The player host is read off the page rather than hard-coded: it has been
    seen as lk1.supremejav.com and the number is exactly the sort of thing
    that rotates.
    """
    template = supjav_player_template(html)
    if not template:
        return []
    try:
        parsed = urlparse(template)
        base_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    except Exception:
        return []
    out, seen = [], set()
    active, rest = [], []
    for m in _SUPJAV_SERVER_RE.finditer(str(html or '')):
        classes, token, label = m.group(1), m.group(2), (m.group(3) or '').strip()
        if not token or token in seen:
            continue
        seen.add(token)
        query = dict(base_query)
        query['l'] = token
        url = urlunparse(parsed._replace(query=urlencode(query)))
        (active if 'active' in (classes or '').lower() else rest).append(
            (label or f'server{len(seen)}', url))
    out.extend(active)
    out.extend(rest)
    return out


def supjav_destination(requested_url: str, status, final_url: str,
                       body: str) -> str:
    """The hoster URL a supjav.php response points at, or '' for none.

    Deliberately a pure function of the response, separate from the fetch, so
    the shapes can be tested without a network. The endpoint is a redirect
    page and there is no way to know from here which form it will answer in,
    so every ordinary one is accepted: a real redirect, a meta refresh, an
    iframe, a location assignment, or a bare URL in the body.
    """
    def _foreign(candidate: str) -> str:
        candidate = unescape(str(candidate or '')).strip()
        if candidate.startswith('//'):
            candidate = 'https:' + candidate
        if not candidate.lower().startswith(('http://', 'https://')):
            return ''
        try:
            host = (urlparse(candidate).netloc or '').lower()
        except Exception:
            return ''
        if not host or any(h in host for h in _SUPJAV_OWN_HOSTS):
            return ''
        return candidate

    # A redirect that left supjav's own plumbing is the answer already.
    moved = _foreign(final_url or '')
    if moved and str(final_url).strip() != str(requested_url or '').strip():
        return moved

    text = str(body or '')
    m = re.search(r'http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>;]+)',
                  text, re.IGNORECASE)
    if m:
        hit = _foreign(m.group(1))
        if hit:
            return hit
    m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        hit = _foreign(m.group(1))
        if hit:
            return hit
    m = re.search(r'location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', text,
                  re.IGNORECASE) or \
        re.search(r'location\.replace\(\s*["\']([^"\']+)["\']', text,
                  re.IGNORECASE)
    if m:
        hit = _foreign(m.group(1))
        if hit:
            return hit
    for m in re.finditer(r'https?://[^\s"\'<>\\]+', text):
        hit = _foreign(m.group(0))
        if hit:
            return hit
    return ''


def _supjav_hoster_urls(html: str, page_url: str, limit: int = 4,
                        use_browser: bool = False):
    """Resolve a supjav watch page to the hoster URLs behind its servers."""
    found = []
    for label, player_url in supjav_server_links(html)[:max(1, int(limit or 1))]:
        dest, status = '', '?'
        try:
            r = cfreq.get(
                player_url,
                impersonate="chrome131",
                timeout=12,
                allow_redirects=True,
                headers={'Referer': page_url or player_url,
                         'Accept': 'text/html,*/*;q=0.8'},
            )
            status = r.status_code
            dest = supjav_destination(player_url, r.status_code,
                                      str(getattr(r, 'url', '') or ''),
                                      r.text)
        except Exception as exc:
            print(f"[SUPJAV] server {label}: {exc}", file=sys.stderr)
        # The gate that 403s the watch page gates this hop too. Following it
        # in a real browser is also the better way to see a redirect: the
        # final URL is the answer.
        if not dest and (use_browser or _looks_blocked(status)):
            print(f"[SUPJAV] server {label}: HTTP {status} over HTTP, "
                  f"retrying in a browser", file=sys.stderr)
            final, body = _browser_fetch(player_url, referer=page_url)
            if final or body:
                dest = supjav_destination(player_url, 200, final, body)
        if dest:
            print(f"[SUPJAV] server {label} -> {dest}", file=sys.stderr)
            found.append(dest)
        else:
            print(f"[SUPJAV] server {label}: no hoster in what came back "
                  f"(HTTP {status})", file=sys.stderr)
    return found


def _extract_title(html: str) -> str:
    """Best-effort title extraction."""
    # 1. og:title
    m = re.search(r'<meta[^>]+property=[\"\']og:title[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']', html, re.IGNORECASE)
    if m:
        t = unescape(m.group(1)).strip()
        if t: return t
        
    # 2. title tag
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        t = unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
        if t: return t
        
    return "Unknown Generic Video"

def grab_all(url: str):
    result = {'title': '', 'streams': []}
    streams_seed = set()
    
    try:
        parsed_url = urlparse(url)
        host = parsed_url.netloc.lower()
        # Derive a slug from the URL to filter out related videos (like javdock/javhdporn previews)
        slug = ""
        parts = [p for p in parsed_url.path.split('/') if p.strip()]
        if parts:
            slug = parts[-1].lower().replace('.html', '')

        status, html = 0, ''
        try:
            r = cfreq.get(
                url,
                impersonate="chrome131",
                timeout=15,
                headers={
                    'Referer': url,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                }
            )
            status = r.status_code
            if status == 200 and not _looks_blocked(status, r.text):
                html = r.text
        except Exception as exc:
            print(f"[grab] HTTP fetch failed: {exc}", file=sys.stderr)

        # A bot gate answers 403 to a plain client and serves the page to a
        # real browser. supjav does exactly this, so the HTTP answer is only
        # the first attempt -- never the verdict. A 404 is a real answer
        # though, and launching a browser for it only makes a dead link slow.
        if not html and (status == 0 or _looks_blocked(status)):
            print(f"[grab] {host} answered HTTP {status or 'nothing'}; "
                  f"retrying through a real browser", file=sys.stderr)
            _, html = _browser_fetch(url)
        if not html:
            result['error'] = f"HTTP {status or 'no response'}"
            return result

        result['used_browser'] = bool(status != 200)
        result['title'] = _extract_title(html)

        # supjav: the watch page has no stream in it, only server tokens. Make
        # the supjav.php hop for each server first and carry on with whatever
        # hoster URLs come back -- sextb / jav.guru / roshy / javgg all have
        # resolvers already, they just never saw the URL before.
        if 'supjav' in host:
            result['supjav_servers'] = [
                {'label': label, 'player_url': player_url}
                for label, player_url in supjav_server_links(html)
            ]
            for dest in _supjav_hoster_urls(html, url,
                                            use_browser=bool(status != 200)):
                streams_seed.add(dest)

        def _unwrap_b64_host(u):
            try:
                parsed = urlparse(u)
                host = (parsed.netloc or '').split('@')[-1]
                blob = host
                for suffix in ('.m3u8', '.m3u', '.mp4'):
                    if blob.lower().endswith(suffix):
                        blob = blob[: -len(suffix)]
                        break
                blob = blob.strip().rstrip('=')
                if blob.startswith('aHR0c'):
                    pad = blob + '=' * ((4 - len(blob) % 4) % 4)
                    decoded = base64.b64decode(pad).decode('utf-8', errors='ignore').strip()
                    if decoded.startswith(('http://', 'https://')):
                        return decoded
            except Exception:
                pass
            return u
        
        streams = set(streams_seed)
        
        # 1. Find m3u8
        for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.m3u8[^\s\"\'<>]*)', html):
            streams.add(m.group(1).replace('\\/', '/'))
            
        # 2. Find mp4 (ignore on jable to avoid 21 previews)
        if 'jable.tv' not in host:
            for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.mp4[^\s\"\'<>]*)', html):
                streams.add(m.group(1).replace('\\/', '/'))
            
        # 3. Find known embeds (dood, streamtape, voe, mixdrop, video.pornfhd,
        #    trailerhg, sextb, emturbovid, javclan, vidara, ... — EMBED_HOST_TOKENS)
        # Using a broad regex for /embed/, /e/, /v/
        for m in re.finditer(r'(https?://[^\s\"\'<>]+?(?:/embed/|/e/|/v/)[^\s\"\'<>]*)', html):
            embed_url = m.group(1).replace('\\/', '/')
            if any(x in embed_url.lower() for x in EMBED_HOST_TOKENS):
                streams.add(embed_url)
                
        # 4. Try looking for iframe src directly if it didn't match above.
        #    R45: also scan data-src / data-litespeed-src / data-embed-src —
        #    LiteSpeed-cached jav pages swap the real src in via JS.
        for m in re.finditer(
                r'<iframe[^>]+(?:src|data-src|data-litespeed-src|data-embed-src|data-frame-src)=[\"\']([^\"\']+)[\"\']',
                html, re.IGNORECASE):
            src = m.group(1).replace('\\/', '/')
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = f"{parsed_url.scheme}://{parsed_url.netloc}{src}"
                
            if any(x in src.lower() for x in EMBED_HOST_TOKENS):
                streams.add(src)
                
        # Extract the JAV code from the title or og:title (e.g. "SDMF-033 ...") for filtering.
        # This is more reliable than the URL slug because CDN filenames use the code too.
        jav_code = ''
        title_text = result.get('title', '')
        jav_code_match = re.search(r'\b([A-Z]{2,8}-\d{3,6})\b', title_text.upper())
        if not jav_code_match:
            # Fallback: try extracting from the URL slug
            jav_code_match = re.search(r'\b([A-Z]{2,8}-\d{3,6})\b', slug.upper())
        if jav_code_match:
            jav_code = jav_code_match.group(1).lower()  # e.g. 'sdmf-033'

        # Filter streams to avoid related video previews using the JAV code
        # Only filter mp4 streams — m3u8 and embed links are never related-video lists
        final_streams = set()
        for u in streams:
            u_lower = u.lower()
            if '123av.com' in host:
                if 'surrit.com' in u or 'v.m3u8' in u_lower or 'video.m3u8' in u_lower:
                    final_streams.add(u)
                    continue
            
            if any(tok in u_lower for tok in ('preview', 'trailer', 'sample', 'trailerhg')):
                continue
            # For CDN mp4s, check that the JAV code appears in the filename
            if jav_code and u_lower.endswith('.mp4'):
                # Code appears as e.g. "_SDMF-033.mp4" or "sdmf-033" in path
                code_no_dash = jav_code.replace('-', '')
                if jav_code not in u_lower and code_no_dash not in u_lower:
                    continue
            final_streams.add(u)
            
        # Recursively fetch embed pages (sextb.net/e/..., trailerhg.xyz/e/...) to get the real streams
        # Also remove them from the final list since they're page URLs, not direct streams
        embed_pages = set()
        real_streams = set()
        for s in final_streams:
            if 'sextb.net/e/' in s or 'trailerhg.xyz/e/' in s:
                embed_pages.add(s)
            else:
                real_streams.add(s)
        final_streams = real_streams
        
        for s in embed_pages:
            try:
                r2 = cfreq.get(s, impersonate="chrome131", timeout=12, headers={'Referer': url})
                html2 = r2.text
                for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.m3u8[^\s\"\'<>]*)', html2):
                    final_streams.add(m.group(1).replace('\\/', '/'))
                for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.mp4[^\s\"\'<>]*)', html2):
                    u = m.group(1).replace('\\/', '/')
                    if 'preview' not in u.lower():
                        final_streams.add(u)
                # Also check for nested iframes pointing to real players
                for m in re.finditer(r'<iframe[^>]+src=[\"\']([^\"\']+)[\"\']', html2, re.IGNORECASE):
                    src2 = m.group(1).replace('\\/', '/')
                    if src2.startswith('//'):
                        src2 = 'https:' + src2
                    if any(x in src2.lower() for x in EMBED_HOST_TOKENS):
                        final_streams.add(src2)
            except Exception:
                pass
        
        # Remove the original page URL itself if it snuck into streams
        page_url_norm = url.rstrip('/')
        final_streams.discard(page_url_norm)
        final_streams.discard(page_url_norm + '/')

        result['streams'] = [_unwrap_b64_host(u) for u in final_streams]
        
    except Exception as e:
        result['error'] = f"Exception: {str(e)}"
        
    return result

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: generic_jav_grab.py <url>'}))
        sys.exit(1)
        
    target_url = sys.argv[1]
    data = grab_all(target_url)
    print(json.dumps(data))
