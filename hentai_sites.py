"""Resolve pasted links from the four sites in terminal.txt.

hanime.tv and hentaihaven.xxx publish one stream. hentaini.com and
hentaimama.io publish that stream plus the other hosters already used
elsewhere in this player. Those hosters are returned as mirrors.

No password is stored. No catalog is scraped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

_SITES = {
    'hanime.tv': 'hanime',
    'hentaihaven.xxx': 'hentaihaven',
    'hentaihaven.com': 'hentaihaven',
    'hentaini.com': 'hentaini',
    'hentaimama.io': 'hentaimama',
}

# Hosters this player already resolves. A new site may list them as
# mirrors. Hosts that are not already adopted are not added here.
_KNOWN_MIRROR_TOKENS = (
    'mega.nz', 'mega.co.nz',
    'terabox', '1024tera', '1024terabox', '4funbox', 'nephobox', 'mirrobox', 'dubox',
    'dood', 'streamtape', 'strtape',
    'voe.', 'voe.sx', 'mixdrop', 'mxdrop',
    'filemoon', 'streamwish', 'streamhg', 'swhoi', 'awish', 'wishembed',
    'vidara', 'emturbovid', 'turbovid', 'vidhide', 'filelions',
    'lulustream', 'luluvdo', 'earnvids',
    'bunkr', 'pixeldrain', 'gofile',
)

_AD_TOKENS = (
    'juicyads', 'exoclick', 'doubleclick', 'googlesyndication', 'popads',
    'trafficjunky', 'adnxs', 'clickadu', 'hilltopads', 'popcash',
)

_HANIME_KEY = bytes.fromhex(
    '5d657a4dcb0bad1c637ff2e221059b10ff17ae39fe855003e846918941f4ebe3')
_HANIME_HEADER = bytes.fromhex('6874762d696e7365637572652d7631')

# Set when a fetch comes back as the Cloudflare check instead of a page.
# The player reads this and opens the user's own Brave. It does not play
# the check page.
last_block = ''


class _Resp:
    def __init__(self, status, text, headers=None, url=''):
        self.status_code = status
        self.text = text or ''
        self.headers = headers or {}
        self.url = url or ''

    def json(self):
        return json.loads(self.text or '')


def site_host(value):
    text = str(value or '').strip().lower()
    if '://' in text:
        text = (urlparse(text).netloc or '').lower()
    if text.startswith('www.'):
        text = text[4:]
    return text.split(':', 1)[0]


def site_kind(value):
    return _SITES.get(site_host(value), '')


def needs_fresh_playback(value):
    """hanime and hentaihaven mint a short-lived stream. Re-read it on play."""
    return site_kind(value) in ('hanime', 'hentaihaven')


def _aes():
    try:
        from Cryptodome.Cipher import AES
        from Cryptodome.Random import get_random_bytes
        return AES, get_random_bytes
    except ImportError:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        return AES, get_random_bytes


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64_decode(text):
    raw = str(text or '').encode('ascii', errors='ignore')
    raw += b'=' * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw)


def hanime_seal(payload):
    """Seal the hanime handshake body. The key is the site's own public one."""
    AES, get_random_bytes = _aes()
    iv = get_random_bytes(12)
    cipher = AES.new(_HANIME_KEY, AES.MODE_GCM, iv)
    cipher.update(_HANIME_HEADER)
    ciphertext, tag = cipher.encrypt_and_digest(
        json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    blob = json.dumps({
        'v': 1,
        'alg': 'AES-256-GCM',
        'iv': _b64(iv),
        'tag': _b64(tag),
        'data': _b64(ciphertext),
    }, separators=(',', ':')).encode('utf-8')
    return _b64(blob)


def hanime_open(token):
    AES, _get_random_bytes = _aes()
    outer = json.loads(_b64_decode(token))
    cipher = AES.new(_HANIME_KEY, AES.MODE_GCM, _b64_decode(outer['iv']))
    cipher.update(_HANIME_HEADER)
    plain = cipher.decrypt_and_verify(
        _b64_decode(outer['data']), _b64_decode(outer['tag']))
    return json.loads(plain.decode('utf-8'))


def _rot13(text):
    out = []
    for char in str(text or ''):
        if 'a' <= char <= 'z':
            out.append(chr((ord(char) - 97 + 13) % 26 + 97))
        elif 'A' <= char <= 'Z':
            out.append(chr((ord(char) - 65 + 13) % 26 + 65))
        else:
            out.append(char)
    return ''.join(out)


def haven_decode_token(content):
    data = str(content or '').replace('sha512-', '', 1)
    for _ in range(3):
        data = _rot13(data)
        data = base64.b64decode(data).decode('utf-8')
    return json.loads(data)


def haven_encode_token(payload):
    data = json.dumps(payload, separators=(',', ':'))
    for _ in range(3):
        data = base64.b64encode(data.encode('utf-8')).decode('ascii')
        data = _rot13(data)
    return 'sha512-' + data


def _looks_like_challenge(text):
    low = str(text or '').lower()
    return (
        'just a moment' in low
        or 'un instant' in low
        or 'cf-challenge' in low
        or 'cdn-cgi/challenge-platform' in low
        or 'challenges.cloudflare.com' in low
    )


def signed_path_expiry(url):
    """Unix expiry baked into a hentaidoge playlist path, or 0.

    The player requests
    ``https://hentaidoge.org/s/<expiry>/<token>/output/.../master.m3u8``.
    That number is the expiry. It is not a query parameter, so a check
    that only reads ``exp=`` treats a still-valid capture as unsigned
    and opens a browser to mint the same playlist again.
    """
    try:
        parsed = urlparse(str(url or ''))
    except Exception:
        return 0.0
    host = (parsed.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    if host != 'hentaidoge.org' and not host.endswith('.hentaidoge.org'):
        return 0.0
    match = re.search(r'/s/(\d{10})(?:/|$)', parsed.path or '')
    if not match:
        return 0.0
    try:
        ts = float(match.group(1))
    except Exception:
        return 0.0
    if 1000000000 <= ts <= 4102444800:
        return ts
    return 0.0


def _mark_challenge(body, status, client):
    global last_block
    if _looks_like_challenge(body):
        last_block = 'cloudflare'
        print(
            f'[HENTAI] {client} HTTP {status} was the Cloudflare check',
            flush=True,
        )
        return True
    return False


def _default_fetch(method, url, headers=None, data=None, timeout=25):
    headers = dict(headers or {})
    headers.setdefault('Accept', 'text/html,application/xhtml+xml,application/json,*/*;q=0.8')
    headers.setdefault('Accept-Language', 'en-US,en;q=0.9')
    method = method.upper()
    # A made-up User-Agent on top of impersonation is what makes Cloudflare
    # show "Just a moment". Keep one only when the caller is replaying the
    # browser that just clicked the check — that cookie is tied to its agent.
    last = None
    try:
        import curl_cffi.requests as cfreq
        fn = cfreq.post if method == 'POST' else cfreq.get
        errors = []
        for persona in ('chrome', 'chrome131', 'chrome124'):
            try:
                response = fn(
                    url, impersonate=persona, timeout=timeout,
                    headers=headers, data=data)
            except Exception as exc:
                errors.append(f'{persona}:{type(exc).__name__}')
                continue
            body = getattr(response, 'text', '') or ''
            status = getattr(response, 'status_code', 0)
            if status == 200 and not _looks_like_challenge(body):
                return _Resp(
                    status, body,
                    getattr(response, 'headers', {}) or {},
                    getattr(response, 'url', url) or url)
            _mark_challenge(body, status, persona)
            last = response
        if errors and last is None:
            print('[HENTAI] curl_cffi failed: ' + ', '.join(errors), flush=True)
        if last is not None:
            return _Resp(
                getattr(last, 'status_code', 0),
                getattr(last, 'text', '') or '',
                getattr(last, 'headers', {}) or {},
                getattr(last, 'url', url) or url)
    except Exception as exc:
        print(f'[HENTAI] curl_cffi unavailable ({type(exc).__name__})', flush=True)
    headers.setdefault('User-Agent', _UA)
    import requests
    fn = requests.post if method == 'POST' else requests.get
    response = fn(
        url, timeout=timeout, headers=headers, data=data,
        allow_redirects=True)
    body = getattr(response, 'text', '') or ''
    status = getattr(response, 'status_code', 0)
    _mark_challenge(body, status, 'plain request')
    return _Resp(
        status, body,
        getattr(response, 'headers', {}) or {},
        getattr(response, 'url', url) or url)


def _header(headers, name):
    if not headers:
        return ''
    wanted = name.lower()
    try:
        value = headers.get(name) or headers.get(wanted)
        if value:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():
            if str(key).lower() == wanted and value:
                return str(value)
    except Exception:
        pass
    return ''


def _title_from_html(html):
    text = html or ''
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<h1[^>]*>(.*?)</h1>',
        r'<title[^>]*>(.*?)</title>',
    ):
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            continue
        title = re.sub(r'<[^>]+>', ' ', match.group(1))
        title = html_unescape(re.sub(r'\s+', ' ', title)).strip()
        title = re.sub(
            r'\s*[\-|–|]\s*(Watch|Hentai Haven|hanime\.tv|Hentaini|Hentaimama).*$',
            '', title, flags=re.I).strip(' -|')
        if title and title.lower() not in ('hentaimama', 'just a moment...'):
            return title[:180]
    return ''


def _abs(base, value):
    value = html_unescape(str(value or '')).replace('\\/', '/').strip()
    if not value or value.lower().startswith(('javascript:', 'data:', 'blob:')):
        return ''
    try:
        resolved = urljoin(base, value)
    except Exception:
        return ''
    parsed = urlparse(resolved)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
        return ''
    return resolved


def _is_ad(url):
    low = str(url or '').lower()
    return any(token in low for token in _AD_TOKENS)


def _is_direct(url):
    try:
        path = (urlparse(url).path or '').lower().rstrip('/')
    except Exception:
        path = str(url or '').lower()
    return path.endswith(('.m3u8', '.m3u', '.mp4', '.webm', '.mkv', '.m4v'))


def _known_mirror(url):
    host = site_host(url)
    return any(token in host for token in _KNOWN_MIRROR_TOKENS)


def _height_hint(url):
    match = re.search(r'(\d{3,4})p', str(url or ''), re.I)
    try:
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def _unique(urls):
    out = []
    seen = set()
    for url in urls or []:
        url = str(url or '').strip()
        if not url or _is_ad(url):
            continue
        key = url.split('#', 1)[0].rstrip('/')
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _playback_headers(page_url):
    parsed = urlparse(page_url)
    origin = f'{parsed.scheme}://{parsed.netloc}' if parsed.netloc else ''
    headers = {'User-Agent': _UA, 'Referer': page_url or origin + '/'}
    if origin:
        headers['Origin'] = origin
    return headers


def _result(kind, page_url, title, playback, mirrors, stable=False):
    playback = str(playback or '').strip()
    mirrors = [
        url for url in _unique(mirrors)
        if url and url.split('#', 1)[0].rstrip('/') != playback.split('#', 1)[0].rstrip('/')
        and site_host(url) not in _SITES
    ]
    if not playback and not mirrors:
        return None
    content_type = ''
    if _is_direct(playback) and '.m3u8' in playback.lower():
        content_type = 'application/vnd.apple.mpegurl'
    return {
        'provider': kind,
        'title': title or '',
        'playback_url': playback,
        'mirrors': mirrors,
        'headers': _playback_headers(page_url) if playback else {},
        'content_type': content_type,
        'stable': bool(stable),
        'origin_page': page_url,
    }


def _player_lists(text):
    lists = []

    def take(node):
        if isinstance(node, str) and node.startswith('['):
            try:
                parsed = json.loads(node)
            except Exception:
                return
            if (
                isinstance(parsed, list) and parsed
                and isinstance(parsed[0], dict)
                and any(item.get('url') for item in parsed if isinstance(item, dict))
            ):
                lists.append(parsed)
        elif isinstance(node, list):
            for item in node:
                take(item)
        elif isinstance(node, dict):
            for item in node.values():
                take(item)

    try:
        take(json.loads(text or ''))
    except Exception:
        pass
    if lists:
        return lists
    raw = html_unescape(text or '').replace('\\/', '/')
    for match in re.finditer(r'\[\s*\{[^\]]+?"url"\s*:\s*"https?:[^]]+\]', raw):
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            lists.append(parsed)
    return lists


def _split_players(players):
    direct = []
    mirrors = []
    downloads = []
    for item in players or []:
        if not isinstance(item, dict):
            continue
        url = _abs('https://hentaini.com/', item.get('url') or item.get('src') or '')
        if not url:
            continue
        name = str(item.get('name') or '').strip().lower()
        if name in ('hls', 'mp4', 'video', 'stream') or _is_direct(url):
            direct.append(url)
        elif name:
            mirrors.append(url)
        elif _known_mirror(url):
            downloads.append(url)
    direct.sort(key=lambda url: (_is_direct(url), _height_hint(url), '.m3u8' in url.lower()), reverse=True)
    return direct, _unique(mirrors + [url for url in downloads if _known_mirror(url)])


def resolve_hentaini(url, fetch):
    parsed = urlparse(url)
    path = (parsed.path or '').rstrip('/')
    match = re.search(r'/h/([^/]+)(?:/(\d+))?$', path, re.I)
    if not match:
        return None
    episode = match.group(2) or ''
    payload_url = url.split('#', 1)[0].rstrip('/') + '/_payload.json'
    response = fetch('GET', payload_url, headers={'Accept': 'application/json'})
    text = response.text if getattr(response, 'status_code', 0) == 200 else ''
    if '"players"' not in text and 'HLS' not in text:
        page = fetch('GET', url)
        text = (text or '') + '\n' + (page.text or '')
    lists = _player_lists(text)
    if not lists:
        return None
    chosen_index = 0
    for index, item in enumerate(lists):
        found_direct, _found_mirrors = _split_players(item)
        if found_direct:
            chosen_index = index
            break
    direct, mirrors = _split_players(lists[chosen_index])
    for item in lists[chosen_index + 1:]:
        more_direct, more_mirrors = _split_players(item)
        if more_direct:
            break
        mirrors.extend(more_mirrors)
    title = _title_from_html(text) or match.group(1).replace('-', ' ').title()
    if episode and episode not in title:
        title = f'{title} Episode {episode}'
    elif not episode:
        title = f'{title} Episode 1'
    playback = direct[0] if direct else ''
    return _result('hentaini', url, title, playback, direct[1:] + mirrors, stable=True)


def _haven_episode_url(page_url, html):
    parsed = urlparse(page_url)
    if re.search(r'/episode-\d+/?$', parsed.path or '', re.I):
        return page_url
    html = (html or '').replace('\\/', '/')
    match = re.search(
        r'https?://(?:www\.)?hentaihaven\.(?:xxx|com)/watch/[^"\']+/episode-\d+/?',
        html, re.I)
    if match:
        return match.group(0)
    slug = re.search(r'/watch/([^/?#]+)', parsed.path or '', re.I)
    if not slug:
        return ''
    return f'{parsed.scheme}://{parsed.netloc}/watch/{slug.group(1)}/episode-1/'


def haven_token_from_html(html):
    text = html_unescape(html or '')
    for tag in re.finditer(r'<meta\b[^>]*>', text, re.I):
        piece = tag.group(0)
        if 'x-secure-token' not in piece.lower():
            continue
        content = re.search(r'content=["\']([^"\']+)', piece, re.I)
        if content:
            return content.group(1)
    match = re.search(
        r'x-secure-token["\']?\s+content=["\']([^"\']+)', text, re.I)
    return match.group(1) if match else ''


def haven_from_token(page_url, token, fetch, title=''):
    """Finish the hentaihaven player once the browser already has the token."""
    try:
        config = haven_decode_token(token)
    except Exception:
        return None
    uri = str(config.get('uri') or '')
    if uri.startswith('//'):
        uri = 'https:' + uri
    if not uri or not config.get('en') or not config.get('iv'):
        return None
    api = uri.rstrip('/') + '/api.php'
    parsed = urlparse(page_url)
    origin = f'{parsed.scheme}://{parsed.netloc}' if parsed.netloc else 'https://hentaihaven.xxx'
    posted = fetch(
        'POST', api,
        headers={
            'Referer': origin + '/',
            'Origin': origin,
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        data={
            'action': 'zarat_get_data_player_ajax',
            'a': config['en'],
            'b': config['iv'],
        })
    try:
        payload = posted.json()
    except Exception:
        return None
    sources = ((payload.get('data') or {}).get('sources') or []) if isinstance(payload, dict) else []
    urls = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        src = _abs(api, source.get('src') or source.get('file') or '')
        if src and _is_direct(src) and not _is_challenge_url(src):
            urls.append(src)
    if not urls:
        return None
    urls.sort(key=_height_hint, reverse=True)
    return _result('hentaihaven', page_url, title, urls[0], [], stable=False)


_CAPTURE_MEDIA_RE = re.compile(
    rb'https?://[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]{12,500}'
    rb'\.(?:m3u8|mp4|m4v|webm)'
    rb'(?:\?[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]{0,800})?',
    re.I)
_CAPTURE_PLAYER_RE = re.compile(
    rb'(?:https?:)?//[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]{0,200}'
    rb'player\.php\?data=[A-Za-z0-9\-._~%+=]{8,800}',
    re.I)


def _is_challenge_url(url):
    low = str(url or '').lower()
    return (
        'cdn-cgi/' in low
        or 'challenge-platform' in low
        or 'challenges.cloudflare.com' in low
    )


_CAPTION_LANGS = frozenset((
    'ar', 'cs', 'da', 'de', 'el', 'en', 'es', 'fi', 'fr', 'he', 'hu',
    'id', 'it', 'ja', 'ko', 'nl', 'pl', 'pt', 'ro', 'ru', 'sv', 'th',
    'tr', 'uk', 'vi', 'zh',
))


_VTT_URL_RE = re.compile(
    r'https?://[^\s\"\'<>\\]+?\.vtt(?:\?[^\s\"\'<>\\]*)?',
    re.I,
)


def caption_tracks_from_blobs(blobs):
    """Caption files named in a capture list or in page text."""
    urls = []
    for blob in blobs or []:
        text = str(blob or '')
        if not text:
            continue
        low = text.lower()
        if low.startswith('http') and low.split('?', 1)[0].endswith('.vtt'):
            urls.append(text)
            continue
        urls.extend(_VTT_URL_RE.findall(text))
    return caption_tracks(urls)


def caption_tracks(urls):
    """Caption files the page player requested, English first."""
    tracks = []
    seen = set()
    for raw in urls or []:
        url = str(raw or '').split('#', 1)[0].strip()
        low = url.lower()
        if not low.startswith('http') or not low.split('?', 1)[0].endswith('.vtt'):
            continue
        if any(token in low for token in ('/ads/', 'havenclick', 'adtng.com', 'magsrv.com')):
            continue
        path = (urlparse(url).path or '').lower()
        lang = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        if lang not in _CAPTION_LANGS:
            lang = 'und'
        key = low.split('?', 1)[0]
        if key in seen:
            continue
        seen.add(key)
        tracks.append({
            'url': url,
            'lang': lang,
            'ext': 'vtt',
            'name': lang,
            'automatic': False,
        })
    tracks.sort(key=lambda item: (0 if item['lang'] == 'en' else 1, item['lang']))
    return tracks


def _stream_family(url):
    """Manifest id shared by one episode's playlists, or ''."""
    match = re.search(
        r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)',
        str(url or ''), re.I)
    return match.group(1).lower() if match else ''


def pick_split_stream(urls):
    """Video playlist plus its audio sibling.

    The master named playlist.m3u8 is what the page loads first. It does
    not carry the picture. Playing it leaves a clock running and a blank
    frame. The picture is the /v.m3u8 (or the vp9 playlist). Sound is
    the /a.m3u8 under snd/.

    Two pages resolving at once used to hand this row the other page's
    /v.m3u8, because that shape sorts above this page's master. Stay
    with the manifest id of the first URL, which is this page's capture.
    """
    videos = []
    audios = []
    masters = []
    vp9 = []
    others = []
    for raw in urls or []:
        url = str(raw or '').strip()
        if not url or _is_challenge_url(url) or _is_ad(url):
            continue
        low = url.lower()
        if '.m3u8' not in low or not low.startswith('http'):
            continue
        if any(token in low for token in (
            '/ads/', 'havenclick', 'adtng.com', 'magsrv.com',
            'sacdnssedge', 'bkcdn.net', 'googletagmanager',
        )):
            continue
        path = (urlparse(url).path or '').lower()
        if path.endswith('/a.m3u8') or '/snd/' in path:
            audios.append(url)
        elif path.endswith('playlist_vp9.m3u8'):
            vp9.append(url)
        elif path.endswith('/v.m3u8'):
            videos.append(url)
        elif path.endswith('/playlist.m3u8'):
            masters.append(url)
        else:
            others.append(url)
    anchor = ''
    for raw in urls or []:
        anchor = _stream_family(raw)
        if anchor:
            break
    if anchor:
        def _own(items):
            same = [item for item in items if _stream_family(item) == anchor]
            return same or [item for item in items if not _stream_family(item)]

        videos = _own(videos)
        vp9 = _own(vp9)
        audios = _own(audios)
        masters = _own(masters)
        others = _own(others)
    playback = (videos or vp9 or others or masters or [''])[0]
    audio = audios[0] if audios and audios[0] != playback else ''
    return playback, audio


def urls_from_capture(blob):
    """Media and player URLs from a browser cache file or network log.

    The Cloudflare check URL is not a video, even when its query is long.
    """
    if isinstance(blob, str):
        blob = blob.encode('utf-8', 'replace')
    blob = blob or b''
    media = []
    players = []
    for match in _CAPTURE_MEDIA_RE.finditer(blob):
        url = match.group(0).decode('ascii', 'ignore')
        if _is_direct(url) and not _is_ad(url) and not _is_challenge_url(url):
            media.append(url)
    for match in _CAPTURE_PLAYER_RE.finditer(blob):
        url = match.group(0).decode('ascii', 'ignore')
        if url.startswith('//'):
            url = 'https:' + url
        if not _is_challenge_url(url):
            players.append(url)
    return {
        'media': _unique(media),
        'players': _unique(players),
        'token': haven_token_from_html(blob.decode('utf-8', 'replace')),
    }


def resolve_hentaihaven(url, fetch):
    global last_block
    page = fetch('GET', url)
    html = page.text or ''
    episode_url = _haven_episode_url(getattr(page, 'url', '') or url, html)
    if episode_url and episode_url.rstrip('/') != str(url).rstrip('/'):
        page = fetch('GET', episode_url)
        html = page.text or ''
        episode_url = getattr(page, 'url', '') or episode_url
    else:
        episode_url = getattr(page, 'url', '') or url
    html = html_unescape(html or '').replace('\\/', '/')
    if _looks_like_challenge(html):
        last_block = 'cloudflare'
        print('[HENTAI] hentaihaven answered with the Cloudflare check, not the player', flush=True)
        return None
    match = re.search(r'(https?:)?(//[^"\'\s>]*player\.php\?data=[^"\'\s>]+)', html, re.I)
    if not match:
        match = re.search(r'(["\'])(/[^"\']*player\.php\?data=[^"\']+)', html, re.I)
        player_url = _abs(episode_url, match.group(2)) if match else ''
    else:
        player_url = _abs(episode_url, (match.group(1) or 'https:') + match.group(2))
    if not player_url:
        return None
    player = fetch('GET', player_url, headers={'Referer': episode_url})
    player_html = player.text or ''
    if _looks_like_challenge(player_html):
        last_block = 'cloudflare'
        print('[HENTAI] hentaihaven player was the Cloudflare check, not the token', flush=True)
        return None
    token = haven_token_from_html(player_html)
    if not token:
        return None
    title = _title_from_html(html) or _title_from_html(player_html)
    return haven_from_token(episode_url, token, fetch, title=title)


def _hanime_is_hls(url):
    path = (urlparse(str(url or '')).path or '').lower()
    return '/hls/' in path or path.endswith(('.m3u8', '.m3u'))


def _hanime_playback(sources):
    """The free stream. 1080p and 4K are paid and are not played."""
    ranked = []
    for source in sources or []:
        if not isinstance(source, dict) or source.get('kind') != 'normal':
            continue
        src = _abs('https://hanime.tv/', source.get('src') or source.get('url') or '')
        if not src or _is_ad(src):
            continue
        height = max(
            _height_hint(source.get('label') or ''),
            _height_hint(src),
            _height_hint(str(source.get('height') or '')),
        )
        ranked.append((height, src))
    if not ranked:
        return ''
    free = [item for item in ranked if 0 < item[0] <= 720]
    if free:
        free.sort(reverse=True)
        return free[0][1]
    unlabeled = [src for height, src in ranked if height == 0]
    if unlabeled:
        return unlabeled[0]
    ranked.sort()
    return ranked[0][1]


def _hanime_slug(url):
    match = re.search(
        r'hanime\.tv/(?:videos/hentai|hentai/video|playlists/[0-9a-z]+/video)/([0-9a-z-]+)',
        str(url or ''), re.I)
    return match.group(1) if match else ''


def resolve_hanime(url, fetch):
    slug = _hanime_slug(url)
    if not slug:
        return None
    title = ''
    try:
        page = fetch('GET', url)
        title = _title_from_html(page.text or '')
    except Exception:
        title = ''
    now = int(time.time())
    signature = hashlib.sha256(
        f'{now},Xkdi29,https://hanime.tv,mn2,{now}'.encode('utf-8')).hexdigest()
    sealed = hanime_seal({
        'timestamp_unix': now,
        'directive': 'htv_player_handshake',
        'slug': slug,
    })
    response = fetch(
        'POST', 'https://auth.hanime.tv/api/v11/handshake',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://hanime.tv',
            'Referer': 'https://hanime.tv/',
            'X-Csrf-Token': 'null',
            'X-Signature': signature,
            'X-Time': str(now),
            'X-Signature-Version': 'web2',
        },
        data=json.dumps({'token': sealed}))
    token = _header(response.headers, 'X-Token')
    if not token:
        try:
            token = str((response.json() or {}).get('token') or '')
        except Exception:
            token = ''
    if not token:
        return None
    manifest = hanime_open(token)
    playback = _hanime_playback(manifest.get('sources') or [])
    if not playback:
        return None
    found = _result(
        'hanime', url, title or slug.replace('-', ' '),
        playback, [], stable=False)
    # The free stream is an extensionless /hls/ path on hanime.tv. Without
    # the playlist type, the player treats that path as a file.
    if _hanime_is_hls(playback):
        found['content_type'] = 'application/vnd.apple.mpegurl'
    return found


def _chunks(body):
    text = body or ''
    try:
        data = json.loads(text)
    except Exception:
        return [text]

    chunks = []

    def walk(node):
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(data)
    return chunks or [text]


def _urls_in(html, base):
    text = html_unescape(html or '').replace('\\/', '/')
    found = []
    for match in re.finditer(
            r'''(?:src|href|file|url)\s*[:=]\s*["\']([^"\']+)["\']''', text, re.I):
        url = _abs(base, match.group(1))
        if url:
            found.append(url)
    for match in re.finditer(r'https?://[^\s"\'<>\\]+', text):
        url = _abs(base, match.group(0).rstrip('.,);'))
        if url:
            found.append(url)
    return _unique(found)


def _mama_post_id(html):
    for pattern in (
        r'data-post=["\'](\d+)["\']',
        r'data-id=["\'](\d+)["\']',
        r'postid["\']?\s*[:=]\s*["\']?(\d+)',
    ):
        match = re.search(pattern, html or '', re.I)
        if match:
            return match.group(1)
    return ''


def _mama_options(html):
    options = []
    for tag in re.finditer(r'<li\b[^>]*dooplay_player_option[^>]*>', html or '', re.I):
        piece = tag.group(0)
        post = re.search(r'data-post=["\'](\d+)["\']', piece, re.I)
        nume = re.search(r'data-nume=["\'](\d+)["\']', piece, re.I)
        kind = re.search(r'data-type=["\']([^"\']+)["\']', piece, re.I)
        if post and nume:
            options.append((post.group(1), nume.group(1), kind.group(1) if kind else 'movie'))
    return options


def _mama_collect(html, base):
    direct = []
    mirrors = []
    for url in _urls_in(html, base):
        if site_host(url) == 'hentaimama.io' and urlparse(url).path.lower().endswith('.php'):
            if 'admin-ajax' not in url:
                mirrors.append(url)  # resolved separately; kept only if it stays direct
            continue
        if _is_direct(url):
            direct.append(url)
        elif _known_mirror(url) or '/embed' in urlparse(url).path.lower() or '/e/' in urlparse(url).path.lower():
            mirrors.append(url)
    return direct, mirrors


def resolve_hentaimama(url, fetch):
    parsed = urlparse(url)
    path = parsed.path or ''
    if '/episodes/' not in path:
        page = fetch('GET', url)
        match = re.search(
            r'https?://(?:www\.)?hentaimama\.io/episodes/[^"\'\s<]+',
            (page.text or '').replace('\\/', '/'), re.I)
        if not match:
            return None
        return resolve_hentaimama(match.group(0).rstrip('\\'), fetch)

    page = fetch('GET', url, headers={'Referer': 'https://hentaimama.io/'})
    html = page.text or ''
    title = _title_from_html(html)
    base = 'https://hentaimama.io'
    direct, mirrors = _mama_collect(html, url)
    same_site_players = []

    def _keep(found_direct, found_mirrors):
        direct.extend(found_direct)
        for mirror in found_mirrors:
            path = (urlparse(mirror).path or '').lower()
            if site_host(mirror) == 'hentaimama.io' and path.endswith('.php') and 'admin-ajax' not in path:
                same_site_players.append(mirror)
            else:
                mirrors.append(mirror)

    page_direct, page_mirrors = direct, mirrors
    direct, mirrors = [], []
    _keep(page_direct, page_mirrors)
    post_id = _mama_post_id(html)
    ajax = base + '/wp-admin/admin-ajax.php'
    ajax_headers = {
        'Referer': url,
        'Origin': base,
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    bodies = []
    if post_id:
        try:
            posted = fetch(
                'POST', ajax, headers=ajax_headers,
                data={'action': 'get_player_contents', 'a': post_id})
            bodies.extend(_chunks(posted.text or ''))
        except Exception:
            pass
    for post, nume, kind in _mama_options(html):
        try:
            posted = fetch(
                'POST', ajax, headers=ajax_headers,
                data={
                    'action': 'doo_player_ajax',
                    'post': post,
                    'nume': nume,
                    'type': kind or 'movie',
                })
            bodies.append(posted.text or '')
        except Exception:
            continue
    for body in bodies:
        _keep(*_mama_collect(body, url))
    seen_players = set()
    for player_url in same_site_players:
        if player_url in seen_players:
            continue
        seen_players.add(player_url)
        try:
            player = fetch('GET', player_url, headers={'Referer': url})
        except Exception:
            continue
        more_direct, more_mirrors = _mama_collect(player.text or '', player_url)
        direct.extend(more_direct)
        mirrors.extend(more_mirrors)
    direct = _unique(direct)
    direct.sort(key=lambda item: (_height_hint(item), '.m3u8' in item.lower()), reverse=True)
    playback = direct[0] if direct else ''
    return _result(
        'hentaimama', url, title, playback,
        direct[1:] + _unique(mirrors), stable=bool(playback))


def same_document(left, right):
    try:
        a = urlparse(str(left or ''))
        b = urlparse(str(right or ''))
    except Exception:
        return False
    host_a = (a.netloc or '').lower()
    host_b = (b.netloc or '').lower()
    if host_a.startswith('www.'):
        host_a = host_a[4:]
    if host_b.startswith('www.'):
        host_b = host_b[4:]
    return bool(host_a) and host_a == host_b and (a.path or '').rstrip('/').lower() == (b.path or '').rstrip('/').lower()


def fetch_using_page(page_url, html, fetch):
    """Serve a page the browser already cleared, then fetch the rest normally.

    The Cloudflare check is not a page. It is never served.
    """
    served = {'done': False}

    def wrapped(method, url, headers=None, data=None, timeout=25):
        if (
            str(method or '').upper() == 'GET'
            and not served['done']
            and html
            and not _looks_like_challenge(html)
            and same_document(url, page_url)
        ):
            served['done'] = True
            return _Resp(200, html, {}, url)
        return fetch(method, url, headers=headers, data=data, timeout=timeout)

    return wrapped


def resolve(url, fetch=None):
    global last_block
    last_block = ''
    fetch = fetch or _default_fetch
    kind = site_kind(url)
    if kind == 'hentaini':
        found = resolve_hentaini(url, fetch)
    elif kind == 'hentaihaven':
        found = resolve_hentaihaven(url, fetch)
    elif kind == 'hanime':
        found = resolve_hanime(url, fetch)
    elif kind == 'hentaimama':
        found = resolve_hentaimama(url, fetch)
    else:
        found = None
    if found:
        last_block = ''
    return found
