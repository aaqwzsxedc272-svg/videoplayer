"""Public TeraBox share links.

yt-dlp has no TeraBox extractor. A share page is not a video file. This
module asks TeraBox's own public share API for the file list and a fresh
signed file URL — the same calls the official player makes. It does not
log in, and it does not try to get past a verification wall.

Signed file URLs expire, so callers should mint one at play time rather
than store it on the playlist row. The page player asks /share/streaming
for an m3u8 when the download API does not hand back a file URL. A
thumbnail URL is never that film, and a playlist that is one random
chunk is not the film either.
"""

from __future__ import annotations

import json
import re
import socket
import time
from html import unescape
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

APP_ID = '250528'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

# Share-page domains. Matched on the host, not as a substring of the URL,
# so a lookalike name does not get sent to this API. CDN hosts such as
# d.terabox.com still match the suffix; those are not share links because
# they have no surl, and the caller leaves them alone.
HOST_SUFFIXES = (
    'terabox.com',
    'terabox.app',
    'teraboxapp.com',
    'terabox.fun',
    'teraboxlink.com',
    'terasharelink.com',
    'teraboxshare.com',
    '1024tera.com',
    '1024terabox.com',
    '1024tera.co',
    '4funbox.com',
    '4funbox.co',
    'mirrobox.com',
    'nephobox.com',
    'momerybox.com',
    'tibibox.com',
    'freeterabox.com',
    'dubox.com',
)

VIDEO_EXT = {
    'mp4', 'mkv', 'webm', 'avi', 'mov', 'm4v', 'wmv', 'flv', 'ts', 'm2ts',
    'mpg', 'mpeg', '3gp', 'ogv',
}
AUDIO_EXT = {'mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus'}

_MAX_FILES = 40
_MAX_DEPTH = 2
# The page player on 1024tera asked for the 480p playlist. Try that
# first, then the other renditions the same API serves.
STREAMING_TYPES = (
    'M3U8_FLV_264_480',
    'M3U8_FLV_264_720',
    'M3U8_FLV_264_1080',
)
_CHUNK_INDEX_RE = re.compile(r'_(\d+)_ts(?:/|$)')
_playlist_servers = []
_playlist_servers_lock = threading.Lock()


def is_terabox_host(host):
    host = str(host or '').strip().lower().split(':')[0]
    if host.startswith('www.'):
        host = host[4:]
    if not host:
        return False
    return any(host == suffix or host.endswith('.' + suffix) for suffix in HOST_SUFFIXES)


def surl_from(url):
    """Bare share id. The leading ``1`` on ``/s/1AbCd`` is a routing marker."""
    try:
        parsed = urlparse(str(url or '').strip())
    except Exception:
        return ''
    raw = ''
    try:
        raw = (parse_qs(parsed.query or '').get('surl') or [''])[0]
    except Exception:
        raw = ''
    if not raw:
        match = re.search(r'/s/([A-Za-z0-9_-]+)', parsed.path or '')
        raw = match.group(1) if match else ''
    raw = str(raw or '').strip().rstrip('/')
    if not raw:
        return ''
    if len(raw) > 8 and raw.startswith('1'):
        return raw[1:]
    return raw


def password_from_url(url):
    try:
        params = parse_qs(urlparse(str(url or '')).query or '')
    except Exception:
        return ''
    for key in ('pwd', 'password', 'pass'):
        values = params.get(key) or []
        if values and str(values[0] or '').strip():
            return str(values[0]).strip()
    return ''


def _fragment_params(url):
    try:
        return parse_qs(urlparse(str(url or '')).fragment or '')
    except Exception:
        return {}


def fs_id_from(url):
    values = _fragment_params(url).get('fid') or []
    return str(values[0] or '').strip() if values else ''


def file_name_from(url):
    values = _fragment_params(url).get('name') or []
    return unquote(str(values[0] or '')).strip() if values else ''


def file_page_url(source_url, surl, fs_id='', name=''):
    """Stable playlist URL. The fragment picks one file out of a folder."""
    surl = str(surl or '').strip()
    if not surl:
        return str(source_url or '').strip()
    try:
        parsed = urlparse(str(source_url or '').strip())
    except Exception:
        parsed = None
    scheme = (parsed.scheme if parsed else '') or 'https'
    host = (parsed.netloc if parsed else '') or 'www.terabox.com'
    parts = []
    if fs_id:
        parts.append('fid=' + quote(str(fs_id), safe=''))
    if name:
        parts.append('name=' + quote(str(name), safe=''))
    return urlunparse((scheme, host, f'/s/1{surl}', '', '', '&'.join(parts)))


def _extension(name):
    match = re.search(r'\.([A-Za-z0-9]{1,5})$', str(name or ''))
    return match.group(1).lower() if match else ''


def _playable(entry):
    ext = _extension(entry.get('name'))
    if ext in VIDEO_EXT or ext in AUDIO_EXT:
        return True
    try:
        category = int(entry.get('category') or 0)
    except Exception:
        category = 0
    return category in (1, 2)


def _duration_ms(value):
    try:
        number = int(float(value or 0))
    except Exception:
        return 0
    if number <= 0:
        return 0
    # The API usually reports seconds. A value this large is already ms.
    if number > 100000:
        return number
    return number * 1000


def _parse_cookie_header(raw):
    jar = {}
    for part in str(raw or '').split(';'):
        if '=' not in part:
            continue
        name, value = part.split('=', 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            jar[name] = value
    return jar


def _cookie_header(jar):
    return '; '.join(f'{name}={value}' for name, value in jar.items() if name and value)


def _decode(value):
    try:
        return unquote(str(value or ''))
    except Exception:
        return str(value or '')


def _normalise_js_token(raw):
    value = str(raw or '').strip()
    if not value:
        return ''
    inner = re.search(r'%22([A-Za-z0-9_%+/.-]{16,})%22', value)
    candidate = _decode(inner.group(1) if inner else value)
    candidate = candidate.strip().strip('"').strip("'")
    if len(candidate) < 8:
        return ''
    return candidate[:200]


def _template_data(html):
    marker = re.search(
        r'(?:locals|window|var|const|let)[.\s]+templateData\s*=\s*\{',
        html or '',
    )
    if not marker:
        return {}
    start = html.find('{', marker.start())
    if start < 0:
        return {}
    depth = 0
    quote = ''
    limit = min(len(html), start + 2000000)
    index = start
    while index < limit:
        char = html[index]
        if quote:
            if char == '\\':
                index += 2
                continue
            if char == quote:
                quote = ''
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(html[start:index + 1])
                except Exception:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
        index += 1
    return {}


def extract_tokens(html):
    html = str(html or '')
    template = _template_data(html)
    tokens = {}

    def _tmpl(key):
        value = template.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value:
            return str(value)
        return ''

    tokens['jsToken'] = _normalise_js_token(_tmpl('jsToken'))
    if not tokens['jsToken']:
        patterns = (
            r'"jsToken"\s*:\s*"function%20fn%28a%29%7Bwindow\.jsToken%20%3D%20a%7D%3Bfn%28%22([^"\\]+)%22%29',
            r'fn\("%28%22([A-Za-z0-9_%+/.-]{16,})%22%29"\)',
            r'fn%28%22([A-Za-z0-9_%+/.-]{16,})%22%29',
            r'window\.jsToken\s*[:=]\s*["\']([^"\']{8,})',
            r'jsToken["\'\s:=]+([A-Za-z0-9_+-]{8,})',
        )
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if not match:
                continue
            token = _normalise_js_token(match.group(1))
            if token:
                tokens['jsToken'] = token
                break

    log_match = re.search(r'(?:dp-logid=|"dp-logid"\s*:\s*"?)(\d{6,})', html)
    if log_match:
        tokens['dpLogId'] = log_match.group(1)
    elif _tmpl('logid'):
        tokens['dpLogId'] = _tmpl('logid')

    for key, pattern in (
        ('shareid', r'["\']?shareid["\']?\s*[:=]\s*["\']?(\d{4,})'),
        ('uk', r'["\']?uk["\']?\s*[:=]\s*["\']?(\d{4,})'),
        ('sign', r'["\']?sign["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{8,})["\']'),
        ('timestamp', r'["\']?timestamp["\']?\s*[:=]\s*["\']?(\d{8,})'),
        ('randsk', r'["\']?randsk["\']?\s*[:=]\s*["\']([^"\']{8,})["\']'),
    ):
        if _tmpl(key):
            tokens[key] = _tmpl(key)
            continue
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            tokens[key] = match.group(1)

    title_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html,
        re.IGNORECASE,
    )
    if title_match:
        tokens['title'] = unescape(_decode(title_match.group(1)))
    return tokens


def _session(cookie_header):
    jar = _parse_cookie_header(cookie_header)
    session = None
    try:
        import curl_cffi.requests as cfreq
        session = cfreq.Session(impersonate='chrome131')
    except Exception:
        session = None
    if session is None:
        import requests
        session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT
    if jar:
        session.headers['Cookie'] = _cookie_header(jar)
    return session, jar


def _absorb(session, response, jar):
    try:
        cookies = getattr(response, 'cookies', None)
        if cookies is not None:
            for name, value in cookies.items():
                if name and value:
                    jar[str(name)] = str(value)
    except Exception:
        pass
    header = _cookie_header(jar)
    if header:
        session.headers['Cookie'] = header


def _get(session, url, headers, timeout, stream=False):
    return session.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
        stream=stream,
    )


def _json_errno(payload):
    if not isinstance(payload, dict):
        return -1, ''
    try:
        errno = int(payload.get('errno', payload.get('code', 0)) or 0)
    except Exception:
        errno = -1
    return errno, str(payload.get('errmsg') or '')


def _entries(payload):
    if not isinstance(payload, dict):
        return []
    for key in ('list', 'file_list'):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _meta_from(payload, tokens):
    payload = payload if isinstance(payload, dict) else {}
    meta = {}
    for key in ('shareid', 'uk', 'sign', 'timestamp'):
        raw = payload.get(key)
        if raw in (None, ''):
            raw = tokens.get(key)
        if raw not in (None, ''):
            meta[key] = str(raw)
    randsk = payload.get('randsk') or tokens.get('randsk') or ''
    if randsk:
        meta['sekey'] = _decode(str(randsk))
        meta['randsk'] = str(randsk)
    return meta


def _signed(meta):
    return all(meta.get(key) for key in ('shareid', 'uk', 'sign', 'timestamp'))


def _to_file(entry):
    name = str(entry.get('server_filename') or entry.get('filename') or '').strip()
    if not name:
        return None
    path = str(entry.get('path') or f'/{name}').strip() or f'/{name}'
    try:
        size = int(entry.get('size') or 0)
    except Exception:
        size = 0
    dlink = str(entry.get('dlink') or '').replace('&amp;', '&').strip()
    try:
        is_dir = int(entry.get('isdir') or 0) == 1
    except Exception:
        is_dir = False
    return {
        'name': name,
        'path': path,
        'size': size if size > 0 else 0,
        'is_dir': is_dir,
        'fs_id': '' if entry.get('fs_id') in (None, '') else str(entry.get('fs_id')),
        'category': entry.get('category'),
        'duration_ms': _duration_ms(entry.get('duration')),
        'dlink': dlink,
    }


def _error(code, message):
    return {
        'ok': False,
        'error_code': code,
        'error': message,
        'title': '',
        'files': [],
    }


def _headers(referer, accept):
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': accept,
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    return headers


def _api_bases(source_url):
    # Canonical hosts first. Short-link domains (teraboxlink.com and the like)
    # only redirect; asking them for the file list just burns the timeout.
    bases = [
        'https://www.terabox.com',
        'https://www.1024terabox.com',
        'https://www.4funbox.com',
    ]
    try:
        parsed = urlparse(str(source_url or ''))
        host = (parsed.netloc or '').lower()
        if is_terabox_host(host):
            pasted = f"{parsed.scheme or 'https'}://{parsed.netloc}"
            if pasted not in bases:
                bases.append(pasted)
    except Exception:
        pass
    return bases[:3]


def _request_json(session, url, referer, jar, timeout):
    response = _get(
        session,
        url,
        _headers(referer, 'application/json, text/plain, */*'),
        timeout,
    )
    _absorb(session, response, jar)
    text = ''
    try:
        text = response.text or ''
    except Exception:
        text = ''
    try:
        payload = json.loads(text) if text else None
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        status = int(getattr(response, 'status_code', 0) or 0)
        return None, status
    return payload, int(getattr(response, 'status_code', 0) or 0)


def _verify_password(session, base, surl, password, referer, jar, timeout):
    if not password:
        return {}
    params = {
        'app_id': APP_ID,
        'web': '1',
        'channel': 'dubox',
        'clienttype': '0',
        'surl': surl,
        'pwd': password,
    }
    payload, _status = _request_json(
        session,
        f'{base}/share/verify?{urlencode(params)}',
        referer,
        jar,
        timeout,
    )
    errno, errmsg = _json_errno(payload)
    print(f'[TERABOX] verify errno={errno} {errmsg}', flush=True)
    if errno != 0 or not isinstance(payload, dict):
        return {}
    randsk = str(payload.get('randsk') or '')
    if randsk:
        jar['BDCLND'] = randsk
        session.headers['Cookie'] = _cookie_header(jar)
    return _meta_from(payload, {})


def _list_dir(session, base, surl, directory, tokens, meta, referer, jar, timeout, anonymous=False):
    params = {
        'app_id': APP_ID,
        'web': '1',
        'channel': 'dubox',
        'clienttype': '0',
        'page': '1',
        'num': '100',
        'by': 'name',
        'order': 'asc',
        'shorturl': surl,
    }
    if directory:
        params['dir'] = directory
    else:
        params['root'] = '1'
    if not anonymous:
        for key in ('sign', 'timestamp', 'shareid', 'uk'):
            if meta.get(key):
                params[key] = meta[key]
        if meta.get('sekey'):
            params['sekey'] = meta['sekey']
        if tokens.get('jsToken'):
            params['jsToken'] = tokens['jsToken']
        if tokens.get('dpLogId'):
            params['dp-logid'] = tokens['dpLogId']
    payload, status = _request_json(
        session,
        f'{base}/share/list?{urlencode(params)}',
        referer,
        jar,
        timeout,
    )
    errno, errmsg = _json_errno(payload)
    print(f'[TERABOX] share/list errno={errno} status={status} {errmsg}', flush=True)
    if errno != 0:
        return [], errno, errmsg
    return _entries(payload), 0, ''


def _init_share(session, base, surl, tokens, referer, jar, timeout):
    def _call(shorturl):
        params = {
            'app_id': APP_ID,
            'web': '1',
            'channel': 'dubox',
            'clienttype': '0',
            'root': '1',
            'scene': '',
            'shorturl': shorturl,
        }
        if tokens.get('jsToken'):
            params['jsToken'] = tokens['jsToken']
        if tokens.get('dpLogId'):
            params['dp-logid'] = tokens['dpLogId']
        return _request_json(
            session,
            f'{base}/api/shorturlinfo?{urlencode(params)}',
            referer,
            jar,
            timeout,
        )

    payload, status = _call('1' + surl)
    errno, errmsg = _json_errno(payload)
    if errno != 0 and errno not in (400210, 460020, -9):
        bare_payload, bare_status = _call(surl)
        bare_errno, bare_errmsg = _json_errno(bare_payload)
        if bare_errno == 0:
            payload, status, errno, errmsg = bare_payload, bare_status, bare_errno, bare_errmsg
    print(f'[TERABOX] shorturlinfo errno={errno} status={status} {errmsg}', flush=True)
    return payload, errno, errmsg


def _collect(session, base, surl, root_entries, tokens, meta, referer, jar, timeout):
    files = []
    queue = [(root_entries, 0)]
    while queue and len(files) < _MAX_FILES:
        entries, depth = queue.pop(0)
        for raw in entries:
            if len(files) >= _MAX_FILES:
                break
            item = _to_file(raw)
            if not item:
                continue
            files.append(item)
            if item['is_dir'] and depth < _MAX_DEPTH:
                nested, errno, _errmsg = _list_dir(
                    session, base, surl, item['path'], tokens, meta,
                    referer, jar, timeout, anonymous=not _signed(meta),
                )
                if errno != 0 or not nested:
                    nested, errno, _errmsg = _list_dir(
                        session, base, surl, item['path'], tokens, meta,
                        referer, jar, timeout, anonymous=True,
                    )
                if nested:
                    queue.append((nested, depth + 1))
    return files


def _load_base(session, base, surl, source_url, password, jar, timeout):
    referer = f'{base}/sharing/link?surl={quote(surl)}'
    tokens = {}
    pages = [referer]
    # /main and the embed shell are only needed when the share page did not
    # carry a jsToken. Hitting all three on every mirror is what made a dead
    # host eat a full minute.
    for page in pages:
        try:
            response = _get(session, page, _headers(referer, 'text/html,*/*'), timeout)
        except Exception as exc:
            print(f'[TERABOX] page failed {page[:80]}: {exc}', flush=True)
            if page == referer:
                return _error('unreachable', 'TeraBox did not answer.')
            continue
        _absorb(session, response, jar)
        try:
            html = response.text or ''
        except Exception:
            html = ''
        found = extract_tokens(html)
        for key, value in found.items():
            if value and not tokens.get(key):
                tokens[key] = value
        final_url = str(getattr(response, 'url', '') or '')
        if final_url and page == referer:
            referer = final_url
            try:
                origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
                if origin.startswith('http'):
                    base = origin
            except Exception:
                pass

    meta = _meta_from({}, tokens)
    if password:
        verified = _verify_password(session, base, surl, password, referer, jar, timeout)
        meta.update({key: value for key, value in verified.items() if value})

    entries = []
    last_errno = 0
    last_errmsg = ''
    payload, errno, errmsg = _init_share(session, base, surl, tokens, referer, jar, timeout)
    last_errno, last_errmsg = errno, errmsg
    if errno == 0 and isinstance(payload, dict):
        meta.update({key: value for key, value in _meta_from(payload, tokens).items() if value})
        entries = _entries(payload)
        title = str(payload.get('title') or '').lstrip('/')
        if title:
            tokens['title'] = title
    elif errno == -9:
        return _error('password', 'That TeraBox share needs a password.')

    if not entries:
        signed_entries, errno, errmsg = _list_dir(
            session, base, surl, '', tokens, meta, referer, jar, timeout, anonymous=False,
        )
        last_errno, last_errmsg = errno, errmsg
        if errno == 0:
            entries = signed_entries
        elif errno == -9:
            return _error('password', 'That TeraBox share needs a password.')

    if not entries:
        anon_entries, errno, errmsg = _list_dir(
            session, base, surl, '', tokens, meta, referer, jar, timeout, anonymous=True,
        )
        last_errno, last_errmsg = errno, errmsg
        if errno == 0:
            entries = anon_entries

    if not entries:
        if last_errno in (400210, 460020, 400141):
            return _error(
                'verify',
                'TeraBox blocked this request. A logged-in terabox.com cookie '
                'is needed for this link.',
            )
        if last_errno in (-9,):
            return _error('password', 'That TeraBox share needs a password.')
        if last_errno in (-10, -62, 115, 130):
            return _error('gone', 'That TeraBox link is expired or private.')
        if last_errno == 9000:
            return _error('region', 'TeraBox is not available from this network.')
        return _error(
            'upstream',
            f'TeraBox did not list that share (errno {last_errno}).',
        )

    files = _collect(session, base, surl, entries, tokens, meta, referer, jar, timeout)
    media = [item for item in files if not item['is_dir']]
    if not media:
        return _error('empty', 'That TeraBox share has no files.')
    playable = [item for item in media if _playable(item)]
    title = tokens.get('title') or (playable[0]['name'] if playable else media[0]['name'])
    return {
        'ok': True,
        'error_code': '',
        'error': '',
        'title': title,
        'files': playable or media,
        'page_url': referer,
        'api_base': base,
        'surl': surl,
        'source_url': source_url,
        'tokens': tokens,
        'meta': meta,
        'cookie_header': _cookie_header(jar),
    }


def list_share(source_url, cookie_header='', password='', timeout=18):
    """File list for a public share. Does not mint a download URL."""
    surl = surl_from(source_url)
    if not surl:
        return _error('invalid', 'That does not look like a TeraBox share link.')
    password = str(password or '').strip() or password_from_url(source_url)
    last = _error('unreachable', 'TeraBox did not answer.')
    for base in _api_bases(source_url):
        # A fresh session per mirror. Cookies minted by a dead host must not
        # be sent to the next one.
        session, jar = _session(cookie_header)
        try:
            result = _load_base(session, base, surl, source_url, password, jar, timeout)
        except Exception as exc:
            print(f'[TERABOX] {base} failed: {exc}', flush=True)
            last = _error('unreachable', 'TeraBox did not answer.')
            continue
        if result.get('ok'):
            return result
        last = result
        if result.get('error_code') in ('password', 'gone', 'invalid'):
            return result
    return last


def _dlink_from(payload):
    if not isinstance(payload, dict):
        return ''
    direct = str(payload.get('dlink') or '').replace('&amp;', '&').strip()
    if direct.startswith('http'):
        return direct
    for entry in _entries(payload):
        link = str(entry.get('dlink') or '').replace('&amp;', '&').strip()
        if link.startswith('http'):
            return link
    return ''


def _mint_dlink(session, share, item, jar, timeout):
    existing = str(item.get('dlink') or '').strip()
    if existing.startswith('http'):
        return existing
    meta = share.get('meta') or {}
    tokens = share.get('tokens') or {}
    fs_id = str(item.get('fs_id') or '').strip()
    if not fs_id or not _signed(meta):
        return ''
    params = {
        'app_id': APP_ID,
        'web': '1',
        'channel': 'dubox',
        'clienttype': '0',
        'uk': meta.get('uk') or '',
        'sign': meta.get('sign') or '',
        'timestamp': meta.get('timestamp') or '',
        'shareid': meta.get('shareid') or '',
        'primaryid': meta.get('shareid') or '',
        'product': 'share',
        'nozip': '0',
        'fid_list': f'[{fs_id}]',
    }
    if meta.get('sekey'):
        params['sekey'] = meta['sekey']
    if tokens.get('jsToken'):
        params['jsToken'] = tokens['jsToken']
    base = str(share.get('api_base') or 'https://www.terabox.com').rstrip('/')
    referer = share.get('page_url') or base
    payload, status = _request_json(
        session,
        f'{base}/share/download?{urlencode(params)}',
        referer,
        jar,
        timeout,
    )
    errno, errmsg = _json_errno(payload)
    print(f'[TERABOX] share/download errno={errno} status={status} {errmsg}', flush=True)
    link = _dlink_from(payload)
    if link:
        return link
    host = urlparse(base).netloc.replace('www.', '')
    rest_hosts = ['data.terabox.com']
    if host:
        rest_hosts.append(f'data.{host}')
    for rest_host in rest_hosts:
        rest_params = dict(params)
        rest_params['method'] = 'locatedownload'
        payload, status = _request_json(
            session,
            f'https://{rest_host}/rest/2.0/share/download?{urlencode(rest_params)}',
            referer,
            jar,
            timeout,
        )
        errno, errmsg = _json_errno(payload)
        print(f'[TERABOX] locatedownload {rest_host} errno={errno} status={status} {errmsg}', flush=True)
        link = _dlink_from(payload)
        if link:
            return link
    return ''


def _follow(session, dlink, referer, timeout):
    response = _get(
        session,
        dlink,
        _headers(referer, '*/*'),
        timeout,
        stream=True,
    )
    final = str(getattr(response, 'url', '') or dlink)
    status = int(getattr(response, 'status_code', 0) or 0)
    try:
        ctype = str(response.headers.get('content-type') or '')
    except Exception:
        ctype = ''
    try:
        response.close()
    except Exception:
        pass
    if status >= 400:
        print(f'[TERABOX] file link HTTP {status}', flush=True)
        return ''
    if 'html' in ctype.lower() or 'json' in ctype.lower():
        print(f'[TERABOX] file link was {ctype or "html"}, not a video', flush=True)
        return ''
    try:
        parsed = urlparse(final)
    except Exception:
        return ''
    if parsed.scheme != 'https' or not parsed.netloc:
        return ''
    return final


def is_thumbnail_url(url):
    """Preview image the share page paints into a <video> before Play.

    The field capture played ``data.1024tera.com/thumbnail/...?ft=video``
    and mpv reported Motion JPEG. That URL is not the film.
    """
    try:
        path = unquote(urlparse(str(url or '')).path or '').lower()
    except Exception:
        path = str(url or '').lower()
    return '/thumbnail/' in path


def is_streaming_url(url):
    try:
        path = unquote(urlparse(str(url or '')).path or '').lower()
    except Exception:
        path = str(url or '').lower()
    return '/share/streaming' in path


def streaming_type_rank(url):
    """Higher is better. Unknown types still beat a thumbnail."""
    try:
        values = parse_qs(urlparse(str(url or '')).query or '').get('type') or []
        kind = str(values[0] if values else '').lower()
    except Exception:
        kind = ''
    score = 1
    for token, points in (
        ('1080', 50),
        ('720', 40),
        ('480', 30),
        ('360', 20),
        ('240', 10),
    ):
        if token in kind:
            score = points
            break
    if 'flv_264' in kind:
        score += 3
    elif 'auto' in kind:
        score += 1
    return score


def streaming_params_from_url(url):
    try:
        query = parse_qs(urlparse(str(url or '')).query or '')
    except Exception:
        query = {}

    def one(key):
        values = query.get(key) or []
        return str(values[0] or '').strip() if values else ''

    return {
        'uk': one('uk'),
        'shareid': one('shareid'),
        'fid': one('fid'),
        'sign': one('sign'),
        'timestamp': one('timestamp'),
        'jsToken': one('jsToken'),
        'type': one('type'),
        'app_id': one('app_id') or APP_ID,
        'channel': one('channel') or 'dubox',
    }


def build_streaming_url(base, meta, fs_id, js_token='', quality='M3U8_FLV_264_480'):
    """The same query the page player sends. Not a stored link."""
    meta = meta or {}
    params = {
        'uk': meta.get('uk') or '',
        'shareid': meta.get('shareid') or '',
        'type': quality,
        'fid': str(fs_id or ''),
        'sign': meta.get('sign') or '',
        'timestamp': meta.get('timestamp') or '',
        'esl': '1',
        'isplayer': '1',
        'ehps': '1',
        'clienttype': '0',
        'app_id': APP_ID,
        'web': '1',
        'channel': 'dubox',
    }
    token = str(js_token or '').strip()
    if token:
        params['jsToken'] = token
    root = str(base or 'https://www.terabox.com').rstrip('/')
    return f'{root}/share/streaming?{urlencode(params)}'


def _media_lines(text):
    duration = 0.0
    items = []
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            try:
                duration = float(line.split(':', 1)[1].split(',')[0])
            except Exception:
                duration = 0.0
            continue
        if not line or line.startswith('#'):
            continue
        items.append((line, duration))
        duration = 0.0
    return items


def chunk_index(url):
    try:
        path = unquote(urlparse(str(url or '')).path or '')
    except Exception:
        path = str(url or '')
    match = _CHUNK_INDEX_RE.search(path)
    if not match:
        match = _CHUNK_INDEX_RE.search(str(url or ''))
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def classify_playlist(text):
    """``normal`` is a film playlist. ``random_chunk`` is one burst, not the film.

    A measured duration under a minute is not what decides this. The share
    page reported 30 seconds for a real /share/streaming URL; that reading
    is not an advert.
    """
    body = str(text or '').lstrip()
    if not body.startswith('#EXTM3U'):
        return 'not_playlist'
    items = _media_lines(body)
    if not items:
        if '#EXT-X-STREAM-INF' in body:
            return 'master'
        return 'empty'
    if '#EXT-X-STREAM-INF' in body and all(
        '.m3u8' in url.lower() or '/share/streaming' in url.lower()
        for url, _duration in items
    ):
        return 'master'
    if all(is_thumbnail_url(url) for url, _duration in items):
        return 'thumbnail'
    indexes = [chunk_index(url) for url, _duration in items]
    end = '#EXT-X-ENDLIST' in body
    if any(index is None for index in indexes):
        if len(items) == 1 and not end:
            url = items[0][0].lower()
            if url.endswith('.ts') or '.ts?' in url or '_ts/' in url:
                return 'random_chunk'
        return 'normal'
    unique = sorted(set(indexes))
    contiguous = unique[-1] - unique[0] + 1 == len(unique)
    starts = unique[0] <= 1
    if end and contiguous and starts:
        return 'normal'
    return 'random_chunk'


def absolutize_playlist(text, base_url):
    def _tag(line):
        return re.sub(
            r'(URI=")([^"]+)(")',
            lambda match: match.group(1) + urljoin(base_url, match.group(2)) + match.group(3),
            line,
        )

    out = []
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            out.append(_tag(line))
            continue
        out.append(urljoin(base_url, line))
    return '\n'.join(out) + ('\n' if out else '')


def prepare_playback_playlist(text, base_url):
    """Absolute segment URLs, marked VOD so a seek is not stuck at the live edge."""
    shape = classify_playlist(text)
    absolute = absolutize_playlist(text, base_url)
    if shape != 'normal':
        return absolute
    lines = [line for line in absolute.splitlines() if line.strip()]
    if not any(line.startswith('#EXT-X-PLAYLIST-TYPE') for line in lines):
        lines.insert(1 if lines else 0, '#EXT-X-PLAYLIST-TYPE:VOD')
    if not any(line.startswith('#EXT-X-ENDLIST') for line in lines):
        lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines) + '\n'


def _redact_url(url):
    try:
        parsed = urlparse(str(url or ''))
        query = parse_qs(parsed.query or '')
        kept = []
        for key in ('type', 'fid'):
            values = query.get(key) or []
            if values and values[0]:
                kept.append(f'{key}={values[0]}')
        suffix = ('?' + '&'.join(kept)) if kept else ''
        return f'{parsed.scheme}://{parsed.netloc}{parsed.path}{suffix}'
    except Exception:
        return 'share/streaming'


def _fetch_text(session, url, referer, timeout):
    response = _get(
        session,
        url,
        _headers(referer, 'application/vnd.apple.mpegurl,application/x-mpegURL,*/*'),
        timeout,
    )
    status = int(getattr(response, 'status_code', 0) or 0)
    try:
        text = response.text
    except Exception:
        raw = getattr(response, 'content', b'') or b''
        text = raw[:2_000_000].decode('utf-8', errors='replace')
    if status >= 400:
        print(f'[TERABOX] player stream HTTP {status} {_redact_url(url)}', flush=True)
        return ''
    if text and not str(text).lstrip().startswith('#EXTM3U'):
        print(f'[TERABOX] player stream was not a playlist ({status}) {_redact_url(url)}', flush=True)
    return text or ''


def _playback_headers(referer, jar, session=None):
    headers = {
        'User-Agent': USER_AGENT,
        'Referer': referer or 'https://www.terabox.com/',
        'Accept': '*/*',
    }
    cookie = _cookie_header(jar)
    if not cookie and session is not None:
        try:
            cookie = str(session.headers.get('Cookie') or '')
        except Exception:
            cookie = ''
    if cookie:
        headers['Cookie'] = cookie
    return headers


def _serve_m3u8(text):
    """Hand mpv a real playlist. Segment URLs stay on the CDN.

    mpv already fetched this CDN directly (the failed run opened the
    thumbnail from data.1024tera.com). It sends the page Referer and the
    capture Cookie on those requests. The local server only serves the
    playlist text so the extensionless /share/streaming URL is not what
    mpv has to sniff.
    """
    body = str(text or '').encode('utf-8')
    if not body.strip():
        return ''

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            payload = body
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                return

        def log_message(self, _format, *_args):
            return

    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    except Exception as exc:
        print(f'[TERABOX] local playlist server failed: {exc}', flush=True)
        return ''
    thread = threading.Thread(target=server.serve_forever, name='terabox-playlist', daemon=True)
    thread.start()
    ready = False
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=0.2):
                ready = True
                break
        except Exception:
            time.sleep(0.05)
    if not ready:
        print('[TERABOX] local playlist server did not accept a connection', flush=True)
        try:
            server.shutdown()
        except Exception:
            pass
        return ''
    with _playlist_servers_lock:
        _playlist_servers.append(server)
        while len(_playlist_servers) > 3:
            old = _playlist_servers.pop(0)
            try:
                old.shutdown()
            except Exception:
                pass
    port = server.server_address[1]
    return f'http://127.0.0.1:{port}/playlist.m3u8'


def _hls_result(playback_url, referer, jar, session, remote=False):
    return {
        'ok': True,
        'error_code': '',
        'error': '',
        'title': '',
        'playback_url': playback_url,
        'headers': _playback_headers(referer, jar, session),
        'content_type': 'application/vnd.apple.mpegurl',
        'route_local_proxy': bool(remote),
        'local_hls': not remote,
    }


def _file_result(playback_url, referer, jar, session):
    return {
        'ok': True,
        'error_code': '',
        'error': '',
        'title': '',
        'playback_url': playback_url,
        'headers': _playback_headers(referer, jar, session),
        'content_type': '',
        'route_local_proxy': False,
        'local_hls': False,
    }


def _dlink_from_streaming_url(session, streaming_url, referer, jar, timeout):
    params = streaming_params_from_url(streaming_url)
    if not all(params.get(key) for key in ('uk', 'shareid', 'fid', 'sign', 'timestamp')):
        return ''
    parsed = urlparse(streaming_url)
    share = {
        'meta': {
            'uk': params['uk'],
            'shareid': params['shareid'],
            'sign': params['sign'],
            'timestamp': params['timestamp'],
        },
        'tokens': {'jsToken': params.get('jsToken') or ''},
        'api_base': f'{parsed.scheme}://{parsed.netloc}',
        'page_url': referer or f'{parsed.scheme}://{parsed.netloc}/',
    }
    return _mint_dlink(session, share, {'fs_id': params['fid']}, jar, timeout)


def _playlist_playback(session, streaming_url, body, referer, jar):
    shape = classify_playlist(body)
    if shape == 'random_chunk':
        print(
            f'[TERABOX] player stream is one fragment, not the film ({_redact_url(streaming_url)})',
            flush=True,
        )
        return _error(
            'fragment',
            'TeraBox only returned a fragment of that video, not the file.',
        )
    if shape == 'thumbnail':
        return _error('fragment', 'TeraBox returned a preview image, not the video.')
    if shape == 'master':
        # Nested variant URLs are themselves /share/streaming. The app
        # proxy rewrites those; a local copy would hand mpv extensionless
        # variant URLs.
        print(f'[TERABOX] master playlist {_redact_url(streaming_url)}', flush=True)
        return _hls_result(streaming_url, referer, jar, session, remote=True)
    if shape != 'normal':
        return None
    prepared = prepare_playback_playlist(body, streaming_url)
    local = _serve_m3u8(prepared)
    if local:
        print(f'[TERABOX] playing playlist from {_redact_url(streaming_url)}', flush=True)
        return _hls_result(local, referer, jar, session, remote=False)
    print(f'[TERABOX] playlist server unavailable, proxying {_redact_url(streaming_url)}', flush=True)
    return _hls_result(streaming_url, referer, jar, session, remote=True)


def playback_from_streaming(streaming_url, referer='', cookie_header='', timeout=18,
                            session=None, jar=None, try_dlink=True):
    """Prefer a file URL minted from the player params, else a real m3u8.

    Does not play a thumbnail, and does not play a one-chunk burst as the
    film. ``jsToken`` is used for this request only.
    """
    streaming_url = str(streaming_url or '').strip()
    if not is_streaming_url(streaming_url):
        return _error('invalid', 'Not a TeraBox player stream.')
    own_session = session is None
    if own_session:
        session, jar = _session(cookie_header)
    elif jar is None:
        jar = _parse_cookie_header(cookie_header)
    referer = referer or 'https://www.terabox.com/'
    if try_dlink:
        try:
            link = _dlink_from_streaming_url(session, streaming_url, referer, jar, timeout)
        except Exception as exc:
            print(f'[TERABOX] file link from player params failed: {exc}', flush=True)
            link = ''
        if link:
            try:
                playback = _follow(session, link, referer, timeout) or ''
            except Exception as exc:
                print(f'[TERABOX] follow failed: {exc}', flush=True)
                playback = ''
            if playback:
                try:
                    host = urlparse(playback).netloc
                except Exception:
                    host = ''
                print(f'[TERABOX] fresh file link host={host}', flush=True)
                return _file_result(playback, referer, jar, session)
    try:
        body = _fetch_text(session, streaming_url, referer, timeout)
    except Exception as exc:
        print(f'[TERABOX] player stream fetch failed: {exc}', flush=True)
        return None
    if not body:
        return None
    return _playlist_playback(session, streaming_url, body, referer, jar)


def _streaming_from_share(session, listed, chosen, jar, timeout):
    meta = listed.get('meta') or {}
    fs_id = str(chosen.get('fs_id') or '').strip()
    if not fs_id or not _signed(meta):
        return None
    base = str(listed.get('api_base') or 'https://www.terabox.com')
    referer = listed.get('page_url') or base
    token = str((listed.get('tokens') or {}).get('jsToken') or '')
    last = None
    for quality in STREAMING_TYPES:
        url = build_streaming_url(base, meta, fs_id, token, quality)
        print(f'[TERABOX] player stream type={quality} fid={fs_id}', flush=True)
        try:
            body = _fetch_text(session, url, referer, min(int(timeout or 8), 8))
        except Exception as exc:
            print(f'[TERABOX] player stream fetch failed: {exc}', flush=True)
            continue
        if not body:
            continue
        result = _playlist_playback(session, url, body, referer, jar)
        if result and result.get('ok'):
            return result
        if result and result.get('error_code') == 'fragment':
            last = result
            continue
    return last


def open_playback(source_url, cookie_header='', password='', timeout=18):
    """Mint a fresh file URL for the share, or for ``#fid=`` inside a folder."""
    listed = list_share(source_url, cookie_header=cookie_header, password=password, timeout=timeout)
    if not listed.get('ok'):
        return listed
    wanted = fs_id_from(source_url)
    files = listed.get('files') or []
    chosen = None
    if wanted:
        for item in files:
            if str(item.get('fs_id') or '') == wanted:
                chosen = item
                break
    if chosen is None:
        playable = [item for item in files if _playable(item) and not item.get('is_dir')]
        chosen = playable[0] if playable else (files[0] if files else None)
    if not chosen or chosen.get('is_dir'):
        return _error('empty', 'That TeraBox share has no video.')
    if not _playable(chosen) and len(files) == 1:
        return _error('empty', 'That TeraBox share has no video.')

    session, jar = _session(listed.get('cookie_header') or cookie_header)
    try:
        dlink = _mint_dlink(session, listed, chosen, jar, timeout)
    except Exception as exc:
        print(f'[TERABOX] mint failed: {exc}', flush=True)
        dlink = ''
    if not dlink:
        existing = str(chosen.get('dlink') or '')
        dlink = existing if existing.startswith('http') else ''
    referer = listed.get('page_url') or 'https://www.terabox.com/'
    playback = ''
    if dlink and not is_thumbnail_url(dlink):
        try:
            playback = _follow(session, dlink, referer, timeout) or ''
        except Exception as exc:
            print(f'[TERABOX] follow failed: {exc}', flush=True)
            playback = ''
        if playback and is_thumbnail_url(playback):
            print('[TERABOX] file link was a thumbnail, not the film', flush=True)
            playback = ''
    if playback:
        try:
            host = urlparse(playback).netloc
        except Exception:
            host = ''
        print(f'[TERABOX] playback host={host} file={chosen.get("name")}', flush=True)
        result = _file_result(playback, referer, jar, session)
    else:
        print('[TERABOX] no file link; asking the page player', flush=True)
        try:
            result = _streaming_from_share(session, listed, chosen, jar, timeout)
        except Exception as exc:
            print(f'[TERABOX] player stream failed: {exc}', flush=True)
            result = None
        if not result and dlink and not is_thumbnail_url(dlink):
            result = _file_result(dlink, referer, jar, session)
        if not result:
            return _error(
                'verify',
                'TeraBox listed the file but did not give a playable link. '
                'A logged-in terabox.com cookie is needed for this share.',
            )
        if not result.get('ok'):
            return result
    result['title'] = chosen.get('name') or listed.get('title') or ''
    result['size_bytes'] = int(chosen.get('size') or 0)
    result['duration_ms'] = int(chosen.get('duration_ms') or 0)
    result['page_url'] = referer
    result['fs_id'] = chosen.get('fs_id') or ''
    result['surl'] = listed.get('surl') or ''
    return result
