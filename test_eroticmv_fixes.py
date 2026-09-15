"""Execute the REAL functions shipped in main.py against the reported cases.

Nothing here re-implements the logic: each function body is lifted verbatim
out of main.py by AST and exec'd, so a regression in main.py fails these.
Runs without PyQt, mpv or network access. The app never imports this file —
run it by hand to validate a copy of main.py.

Covers: eroticmv playlist titles, exact 3s arrow seeks, the VOD-proxy routing
that makes backward seeking work, and the Google search query cleanup.
"""
import ast
import base64
import json
import os
import re
from html import unescape as html_unescape
from urllib.parse import urlparse, unquote, urljoin, urlunparse, parse_qs

SRC = open('main.py', encoding='utf-8').read()
TREE = ast.parse(SRC)


class _FakeQTimer:
    """Captures singleShot callbacks so the test can fire them."""
    callbacks = []

    @staticmethod
    def singleShot(ms, fn):
        _FakeQTimer.callbacks.append((ms, fn))


G = {
    're': re, 'os': os, 'urlparse': urlparse, 'unquote': unquote,
    'urljoin': urljoin, 'html_unescape': html_unescape, 'print': print,
    'QTimer': _FakeQTimer, 'base64': base64, 'json': json,
}


def lift(class_name, func_name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name == func_name:
                    mod = ast.Module(body=[item], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, f'<{class_name}.{func_name}>', 'exec'), G)
                    return G[func_name]
    raise AssertionError(f'{class_name}.{func_name} not found in main.py')


def lift_attr(class_name, attr_name):
    """Exec a class-body assignment from main.py verbatim and return its value."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == attr_name
                        for t in item.targets):
                    mod = ast.Module(body=[item], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, f'<{class_name}.{attr_name}>', 'exec'), G)
                    return G[attr_name]
    raise AssertionError(f'{class_name}.{attr_name} not found in main.py')


FAILS = 0


def report(ok, label, detail=''):
    global FAILS
    FAILS += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")


# ── 1. title cleaning ────────────────────────────────────────────────────────
banned = lift('VideoPlayer', '_is_banned_stream_title')
banned_fn = banned.__func__ if isinstance(banned, classmethod) else banned
clean = lift('VideoPlayer', '_clean_remote_title')


class TitleStub:
    _BANNED_STREAM_TITLES = lift_attr('VideoPlayer', '_BANNED_STREAM_TITLES')

    @classmethod
    def _is_banned_stream_title(cls, title):
        return banned_fn(cls, title)

    _clean_remote_title = clean


t = TitleStub()
for raw, want in [
    ('Watch Dressage (1986) - Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) | Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) – Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) - EroticMV', 'Dressage (1986)'),
    ('Watch Dressage (1986) - eroticmv.com', 'Dressage (1986)'),
    ('Dressage (1986)', 'Dressage (1986)'),
    ('  Watch   Some Film   -   Erotic Movies  ', 'Some Film'),
]:
    got = t._clean_remote_title(raw)
    report(got == want, f'title {raw!r} -> {got!r}', f'(want {want!r})')


# ── 2. seekRelative ──────────────────────────────────────────────────────────
class FakeMpv:
    def __init__(self, time_pos):
        self.time_pos = time_pos
        self.calls = []

    def get_property(self, name):
        assert name == 'time-pos', name
        return self.time_pos

    def seek(self, seconds, *flags):
        self.calls.append((seconds, flags))
        # Real mpv moves time-pos on a honoured seek.
        self.time_pos = seconds

    def command(self, *a):
        pass


class SeekStub:
    _mpv_time_pos_ms = lift('MpvMediaPlayerAdapter', '_mpv_time_pos_ms')
    seekRelative = lift('MpvMediaPlayerAdapter', 'seekRelative')
    _issue_absolute_seek = lift('MpvMediaPlayerAdapter', '_issue_absolute_seek')
    _verify_seek_landed = lift('MpvMediaPlayerAdapter', '_verify_seek_landed')

    def __init__(self, time_pos_s, duration_ms, file_loaded=True):
        self._mpv = FakeMpv(time_pos_s)
        self._file_loaded = file_loaded
        self._duration_ms = duration_ms
        self._position_ms = int((time_pos_s or 0) * 1000)
        self._pending_seek_ms = 999

    def position(self):
        return int(self._position_ms)

    def duration(self):
        return int(self._duration_ms)

    def setPosition(self, ms):
        self._last_set_position = max(0, int(ms))
        if self._mpv is not None:
            self._mpv.calls.append((self._last_set_position / 1000.0, ('setPosition',)))


def fire_timers():
    cbs, _FakeQTimer.callbacks[:] = _FakeQTimer.callbacks[:], []
    _FakeQTimer.callbacks.clear()
    for _ms, fn in cbs:
        fn()


def check_seek(label, stub, delta, want_s, want_flags):
    _FakeQTimer.callbacks.clear()
    stub.seekRelative(delta)
    got = stub._mpv.calls
    ok = len(got) == 1 and abs(got[0][0] - want_s) < 1e-6 \
        and tuple(got[0][1]) == tuple(want_flags)
    report(ok, label, f'seek{got} (want {want_s}s {want_flags})')


check_seek('right arrow at 100s, dur 1h',
           SeekStub(100.0, 3600_000), 3000, 103.0, ('absolute', 'exact'))
check_seek('left arrow at 100s, dur 1h',
           SeekStub(100.0, 3600_000), -3000, 97.0, ('absolute', 'exact'))
check_seek('left arrow at 1.5s clamps to 0',
           SeekStub(1.5, 3600_000), -3000, 0.0, ('absolute', 'exact'))
check_seek('right arrow past end clamps to duration',
           SeekStub(3599.0, 3600_000), 3000, 3600.0, ('absolute', 'exact'))
check_seek('left after clicking bar to 1800s',
           SeekStub(1800.0, 3600_000), -3000, 1797.0, ('absolute', 'exact'))
report(len(_FakeQTimer.callbacks) == 1, 'a verification callback was scheduled')

# time-pos unavailable -> cached position() is the base
s = SeekStub(None, 3600_000)
s._position_ms = 500_000
s.seekRelative(-3000)
got = s._mpv.calls
report(len(got) == 1 and abs(got[0][0] - 497.0) < 1e-6
       and got[0][1] == ('absolute', 'exact'),
       f'time-pos None falls back to position(): {got}')

# exact RAISES -> keyframe ABSOLUTE fallback, never 'relative'
s = SeekStub(100.0, 3600_000)


def _raise(seconds, *flags):
    if 'exact' in flags:
        raise RuntimeError('mpv: exact unsupported')
    s._mpv.calls.append((seconds, flags))


s._mpv.seek = _raise
s.seekRelative(3000)
report(len(s._mpv.calls) == 1 and abs(s._mpv.calls[0][0] - 103.0) < 1e-6
       and s._mpv.calls[0][1] == ('absolute',),
       f'exact raises -> absolute fallback: {s._mpv.calls}')

# exact SILENTLY no-ops (mpv honours nothing, time-pos stays) -> keyframe retry
s = SeekStub(1800.0, 3600_000)


def _noop_exact(seconds, *flags):
    if 'exact' in flags:
        return                      # accepted but ignored: the reported bug
    s._mpv.calls.append((seconds, flags))
    s._mpv.time_pos = seconds


s._mpv.seek = _noop_exact
_FakeQTimer.callbacks.clear()
s.seekRelative(-3000)             # user pressed Left after clicking to 1800s
report(len(s._mpv.calls) == 0, 'silent no-op: nothing moved yet')
fire_timers()
report(len(s._mpv.calls) == 1 and abs(s._mpv.calls[0][0] - 1797.0) < 1e-6
       and s._mpv.calls[0][1] == ('absolute',),
       f'silent no-op -> keyframe absolute retry: {s._mpv.calls}')

# exact landed -> verification must NOT seek again
s = SeekStub(1800.0, 3600_000)
_FakeQTimer.callbacks.clear()
s.seekRelative(-3000)
fire_timers()
report(len(s._mpv.calls) == 1, f'honoured exact seek: no duplicate seek {s._mpv.calls}')

# a newer seek supersedes an outstanding verification
s = SeekStub(1800.0, 3600_000)
s._mpv.seek = _noop_exact
_FakeQTimer.callbacks.clear()
s.seekRelative(-3000)
first = _FakeQTimer.callbacks[:]
s._mpv.time_pos = 900.0           # playback jumped elsewhere
s.seekRelative(-3000)             # second seek replaces the token
_FakeQTimer.callbacks.clear()
for _ms, fn in first:             # fire only the STALE callback
    fn()
report(len(s._mpv.calls) == 0, f'stale verification is ignored {s._mpv.calls}')

# mpv absent -> setPosition path, still absolute target
s = SeekStub(100.0, 3600_000)
s._mpv = None
s.seekRelative(3000)
report(getattr(s, '_last_set_position', None) == 103000,
       f"no-mpv path -> setPosition({getattr(s, '_last_set_position', None)}) (want 103000)")


# ── 3. eroticmv must route through the VOD-rewriting proxy ───────────────────
class ProxyStub:
    _is_hls_stream_url = lift('VideoPlayer', '_is_hls_stream_url')
    _hls_needs_vod_playlist_proxy = lift('VideoPlayer', '_hls_needs_vod_playlist_proxy')
    _HLS_VOD_PROXY_HOST_TOKENS = lift_attr(
        'VideoPlayer', '_HLS_VOD_PROXY_HOST_TOKENS')


p = ProxyStub()
for playback, source, want, label in [
    ('https://vidcdn2.eroticmv.com/dat1/abc/abc.m3u8',
     'https://eroticmv.com/watch/dressage-1986', True, 'vidcdn m3u8 + eroticmv page'),
    ('https://vidcdn2.eroticmv.com/dat1/abc/abc.m3u8', '', True, 'vidcdn host alone'),
    ('https://cdn.example.com/x/master.m3u8',
     'https://eroticmv.com/watch/dressage-1986', True, 'any CDN under an eroticmv page'),
    ('https://othercdn.com/x.m3u8', 'https://javdock.com/v/1', False, 'unrelated jav page'),
    ('https://vidcdn2.eroticmv.com/movie.mp4',
     'https://eroticmv.com/watch/x', False, 'non-HLS eroticmv file stays direct'),
]:
    got = p._hls_needs_vod_playlist_proxy(playback, source)
    report(got is want, f'{label} -> {got}', f'(want {want})')

# ── 4. Google search query must not carry the file extension ─────────────────
def lift_module_attr(attr_name):
    for item in TREE.body:
        if isinstance(item, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == attr_name
                for t in item.targets):
            mod = ast.Module(body=[item], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, f'<module.{attr_name}>', 'exec'), G)
            return G[attr_name]
    raise AssertionError(f'module-level {attr_name} not found in main.py')


for _ext_tuple in ('VIDEO_EXTENSIONS', 'AUDIO_EXTENSIONS',
                   'IMAGE_EXTENSIONS', 'ARCHIVE_EXTENSIONS'):
    lift_module_attr(_ext_tuple)


class SearchStub:
    _strip_seen_display_prefix = lift('VideoPlayer', '_strip_seen_display_prefix')
    _google_search_query_for_name = lift(
        'VideoPlayer', '_google_search_query_for_name')
    _SEARCH_QUERY_STRIPPED_EXTENSIONS = lift_attr(
        'VideoPlayer', '_SEARCH_QUERY_STRIPPED_EXTENSIONS')
    _SEARCH_QUERY_SERIES_COUNTER_RE = lift_attr(
        'VideoPlayer', '_SEARCH_QUERY_SERIES_COUNTER_RE')


q = SearchStub()
for raw, want, label in [
    ('Some Movie.mp4', 'Some Movie', 'the reported bug'),
    ('Some Movie.MP4', 'Some Movie', 'extension is case-insensitive'),
    ('Some Movie.mkv', 'Some Movie', 'mkv stripped too'),
    ('Some Movie.webm', 'Some Movie', 'webm stripped too'),
    ('- Some Movie.mp4', 'Some Movie', 'seen marker AND extension'),
    ('Movie.1080p.mp4', 'Movie.1080p', 'only the LAST extension goes'),
    ('Chapter.cbz', 'Chapter', 'archive extension'),
    ('Dressage (1986)', 'Dressage (1986)', 'clean title untouched'),
    ('Mr. Robot', 'Mr. Robot', 'a dot inside a title is not eaten'),
    ('  Some   Movie  ', 'Some Movie', 'whitespace collapsed'),
    ('.mp4', '', 'a bare extension yields nothing to search'),
    ('', '', 'empty stays empty'),
]:
    got = q._google_search_query_for_name(raw)
    report(got == want, f'search {raw!r} -> {got!r}  [{label}]', f'(want {want!r})')

from urllib.parse import quote_plus as _qp
for raw in ('Some Movie.mp4', '- Some Movie.mkv'):
    url = 'https://www.google.com/search?q=' + _qp(q._google_search_query_for_name(raw))
    report('mp4' not in url.lower() and 'mkv' not in url.lower(),
           f'query URL is extension-free: {url}')

# ── 5. favicon: prefer the icon the site DECLARES over /favicon.ico ──────────
class FaviconStub:
    _favicon_tag_attr = staticmethod(lift('VideoPlayer', '_favicon_tag_attr'))
    _FAVICON_HTML_REL_PREFERENCE = lift_attr(
        'VideoPlayer', '_FAVICON_HTML_REL_PREFERENCE')
    _favicon_urls_declared_in_html = lift(
        'VideoPlayer', '_favicon_urls_declared_in_html')

    def __init__(self, html_text, ctype='text/html; charset=utf-8'):
        self._html = html_text
        self._ctype = ctype
        self.requested = []

    # The network layer is stubbed; the parsing under test is real.
    def _favicon_http_get(self, url, headers, timeout=6, want_text=False):
        self.requested.append(url)
        if want_text:
            return self._html, self._ctype
        return None, ''


WORDPRESS_PAGE = """<!DOCTYPE html><html><head>
<link rel="profile" href="https://gmpg.org/xfn/11">
<link rel="apple-touch-icon" sizes="180x180"
      href="/wp-content/uploads/2021/03/cropped-logo-180.png">
<link rel="icon" type="image/png" sizes="32x32"
      href="/wp-content/uploads/2021/03/cropped-logo-32.png">
<link rel="stylesheet" href="/wp-includes/css/style.css">
<link rel='shortcut icon' href='https://cdn.javgg.net/old.ico'>
</head><body></body></html>"""

f = FaviconStub(WORDPRESS_PAGE)
got = f._favicon_urls_declared_in_html('javgg.net', {})
report(f.requested == ['https://javgg.net/'],
       f'homepage probed once: {f.requested}')
report(len(got) == 3, f'only icon links kept, stylesheet/profile dropped: {got}')
report(bool(got) and got[0] ==
       'https://javgg.net/wp-content/uploads/2021/03/cropped-logo-32.png',
       f'rel="icon" wins and is resolved to an absolute URL: {got[0] if got else None}')
report(len(got) > 1 and got[1] == 'https://cdn.javgg.net/old.ico',
       f"single-quoted 'shortcut icon' kept, absolute href untouched: "
       f'{got[1] if len(got) > 1 else None}')
report(len(got) > 2 and got[2].endswith('cropped-logo-180.png'),
       f'apple-touch-icon ranked last: {got[2] if len(got) > 2 else None}')

h = FaviconStub('<html><head><title>x</title></head></html>')
report(h._favicon_urls_declared_in_html('eroticmv.com', {}) == [],
       'a page with no <link> tags yields no declared icons')

g = FaviconStub('<html><link rel="icon" href="a.png?x=1&amp;y=2"></html>')
report(g._favicon_urls_declared_in_html('eroticmv.com', {}) ==
       ['https://eroticmv.com/a.png?x=1&y=2'],
       f'HTML entities in href are decoded: '
       f'{g._favicon_urls_declared_in_html("eroticmv.com", {})}')

for tag, attr, want in [
    ('<link rel="icon" href="a.png">', 'href', 'a.png'),
    ("<link rel='icon' href='a.png'>", 'href', 'a.png'),
    ('<link rel=icon href=a.png>', 'href', 'a.png'),
    ('<link rel="icon" href="a.png">', 'rel', 'icon'),
    ('<link rel="icon">', 'href', ''),
    ('<link href="a&amp;b.png" rel="icon">', 'href', 'a&b.png'),
]:
    got_attr = FaviconStub('') ._favicon_tag_attr(tag, attr)
    report(got_attr == want, f'attr {attr} in {tag!r} -> {got_attr!r}', f'(want {want!r})')

# ── 6. favicon domain: a CDN row must ask the SITE for its icon ──────────────
class SiteDomainStub:
    _JAV_SITE_TOKENS = lift_attr('VideoPlayer', '_JAV_SITE_TOKENS')
    _is_jav_site_host = lift('VideoPlayer', '_is_jav_site_host')
    _jav_site_domain_for_host = lift('VideoPlayer', '_jav_site_domain_for_host')
    _favicon_domains_for_entry = lift('VideoPlayer', '_favicon_domains_for_entry')

    def __init__(self, site_url='', active_mirror=''):
        self._site_url = site_url
        self._active_mirror = active_mirror

    # Row-identity lookups are stubbed; the domain maths under test is real.
    def _jav_site_url_for_entry(self, entry):
        return self._site_url

    def _active_mirror_for_entry(self, entry):
        return self._active_mirror

    def _favicon_brand_domain_for_host(self, host):
        return ''

    def _favicon_hoster_brand_from_context(self, entry):
        return ''

    def _favicon_host_domain_for_entry(self, entry):
        return ''


d = SiteDomainStub()
for host, want in [
    ('vidcdn2.eroticmv.com', 'eroticmv.com'),
    ('vidcdn.eroticmv.com', 'eroticmv.com'),
    ('eroticmv.com', 'eroticmv.com'),
    ('www.eroticmv.com', 'eroticmv.com'),
    ('eroticmv.net', 'eroticmv.net'),
    ('javgg.net', 'javgg.net'),
    ('roshy.tv', 'roshy.tv'),
    ('cdn.roshy.tv', 'roshy.tv'),
    ('jav.guru', 'jav.guru'),
    ('sextb.net', 'sextb.net'),
    ('javhd.today', 'javhd.today'),
    ('eporner.com', 'eporner.com'),
    ('vid-eporner.com', ''),
    ('doodstream.com', ''),
    ('', ''),
]:
    got_dom = d._jav_site_domain_for_host(host)
    report(got_dom == want, f'site domain {host!r} -> {got_dom!r}', f'(want {want!r})')

CDN_ROW = 'https://vidcdn2.eroticmv.com/dat1/dressage/dressage.m3u8'
site, hoster = SiteDomainStub()._favicon_domains_for_entry(CDN_ROW)
report(site == 'eroticmv.com',
       f'CDN row with no harvested page still asks the site: {site!r}')

site, hoster = SiteDomainStub(
    site_url='https://eroticmv.com/watch/dressage-1986'
)._favicon_domains_for_entry(CDN_ROW)
report(site == 'eroticmv.com',
       f'harvested page host wins for the site icon: {site!r}')

site, hoster = SiteDomainStub()._favicon_domains_for_entry('https://javgg.net/v/abc123')
report(site == 'javgg.net', f'javgg row unchanged: {site!r}')

# The exact reported case: a linked series/sequel row's widget text.
for raw, want, label in [
    ('PrimalFetish Jasmine Grey Confronting Part 2.mp4  ·  2/3',
     'PrimalFetish Jasmine Grey Confronting Part 2', 'THE REPORTED BUG'),
    ('Some Movie.mp4 · 1/3', 'Some Movie', 'counter after extension'),
    ('Some Movie · 2/3', 'Some Movie', 'counter with no extension'),
    ('- Some Movie.mp4 · 2/3', 'Some Movie', 'seen marker + counter + ext'),
    ('Movie.1080p.mkv · 3/3', 'Movie.1080p', 'keeps the quality tag'),
    ('Half • 1/2', 'Half', 'bullet separator variant'),
    ('Some Movie 1/2', 'Some Movie 1/2', 'no separator = real title, left alone'),
    ('Dressage (1986) · 2/2', 'Dressage (1986)', 'clean title + counter'),
]:
    got = q._google_search_query_for_name(raw)
    report(got == want, f'series {raw!r} -> {got!r}  [{label}]', f'(want {want!r})')

# ── 7. HTML resolve must not pick the preview over the film ──────────────────
def unwrap(fn):
    return fn.__func__ if isinstance(fn, (staticmethod, classmethod)) else fn


class HtmlResolveStub:
    _PREVIEW_MEDIA_URL_TOKENS = lift_attr(
        'VideoPlayer', '_PREVIEW_MEDIA_URL_TOKENS')
    _media_url_looks_like_preview = lift(
        'VideoPlayer', '_media_url_looks_like_preview')
    _hls_playlist_total_seconds = staticmethod(unwrap(
        lift('VideoPlayer', '_hls_playlist_total_seconds')))
    _hls_best_variant_url = staticmethod(
        unwrap(lift('VideoPlayer', '_hls_best_variant_url')))
    _html_candidate_rank_score = staticmethod(unwrap(
        lift('VideoPlayer', '_html_candidate_rank_score')))


r = HtmlResolveStub()

for url, want in [
    ('https://cdn.example.com/preview/abc.m3u8', True),
    ('https://cdn.example.com/previewclip/abc.m3u8', True),
    ('https://trailerhg.xyz/e/abc.m3u8', True),
    ('https://cdn.example.com/v/trailer.m3u8', True),
    ('https://cdn.example.com/teaser/1.m3u8', True),
    ('https://cdn.example.com/x/sample.m3u8', True),
    ('https://cdn.example.com/x/sample/1.ts', True),
    ('https://vidcdn2.eroticmv.com/dat1/dressage/dressage.m3u8', False),
    ('https://cdn.example.com/movie.m3u8', False),
]:
    got_prev = r._media_url_looks_like_preview(url)
    report(got_prev is want, f'preview? {url!r} -> {got_prev}', f'(want {want})')

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXTINF:6.006,
seg-0.ts
#EXTINF:6.006,
seg-1.ts
#EXTINF: 4.5 ,
seg-2.ts
#EXT-X-ENDLIST
"""
total = r._hls_playlist_total_seconds(MEDIA_PLAYLIST)
report(abs(total - 16.512) < 0.01, f'media playlist EXTINF sum -> {total}', '(want 16.512)')
report(r._hls_playlist_total_seconds('#EXTM3U\n#EXT-X-ENDLIST\n') == 0.0,
       'playlist with no segments measures 0')
report(r._hls_playlist_total_seconds('') == 0.0, 'empty playlist measures 0')

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720
mid/index.m3u8
"""
got_var = r._hls_best_variant_url(MASTER, 'https://cdn.example.com/master.m3u8')
report(got_var == 'https://cdn.example.com/high/index.m3u8',
       f'highest-bandwidth variant chosen: {got_var}')
report(r._hls_best_variant_url('#EXTM3U\n', 'https://x/y.m3u8') == '',
       'a media playlist has no variant to follow')
abs_master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://other.example/a.m3u8\n'
report(r._hls_best_variant_url(abs_master, 'https://cdn.example.com/m.m3u8')
       == 'https://other.example/a.m3u8', 'absolute variant URI left intact')

# The reported failure: the preview appears FIRST in the page.
PREVIEW = 'https://cdn.example.com/preview/teaser.m3u8'
FILM = 'https://cdn.example.com/v/movie.m3u8'
ordered = [(0, PREVIEW), (8, FILM)]
kept = [i for i in ordered if not r._media_url_looks_like_preview(i[1])] or ordered
report([c for _p, c in kept] == [FILM],
       f'preview-token URL dropped before ranking: {[c for _p, c in kept]}')

# Ranking when the preview URL gives no token hint, only its short duration.
A = 'https://cdn.example.com/a.m3u8'
B = 'https://cdn.example.com/b.m3u8'
MP4 = 'https://cdn.example.com/c.mp4'
durations = {A: 45.0, B: 3600.0}
picked = sorted([(0, A), (5, B), (9, MP4)],
                key=lambda i: r._html_candidate_rank_score(i[0], i[1], durations),
                reverse=True)
report([c for _p, c in picked][0] == B,
       f'45s teaser loses to the 3600s film: {[c for _p, c in picked]}')

picked = sorted([(0, MP4), (3, A)],
                key=lambda i: r._html_candidate_rank_score(i[0], i[1], {A: 45.0}),
                reverse=True)
report([c for _p, c in picked][0] == MP4,
       f'unmeasured mp4 beats a measured 45s teaser: {[c for _p, c in picked]}')

picked = sorted([(0, MP4), (4, A)],
                key=lambda i: r._html_candidate_rank_score(i[0], i[1], {}),
                reverse=True)
report([c for _p, c in picked][0] == MP4,
       f'nothing measured -> earlier pattern wins: {[c for _p, c in picked]}')

# ── 8. noodlemagazine: real sources live on the /download/ page ──────────────
class NoodleStub:
    _NOODLE_FAMILY_DOMAINS = lift_attr('VideoPlayer', '_NOODLE_FAMILY_DOMAINS')
    _is_noodle_family_host = lift('VideoPlayer', '_is_noodle_family_host')
    _parse_window_playlist_json = staticmethod(
        unwrap(lift('VideoPlayer', '_parse_window_playlist_json')))
    _noodle_sources_from_playlist = staticmethod(
        unwrap(lift('VideoPlayer', '_noodle_sources_from_playlist')))


nd = NoodleStub()
for host, want in [
    ('noodlemagazine.com', True),
    ('mat6tube.com', True),              # the site from the reported log
    ('www.mat6tube.com', True),
    ('adult.noodlemagazine.com', True),  # subdomain of a family domain
    ('mat6tube.com:443', True),          # port stripped
    ('ukdevilz.com', True),
    ('exporntoons.net', True),
    ('tyler-brown.com', True),
    ('actionviewphotography.com', True),
    ('noodlemagazine.best', False),      # phishing clone, not the family
    ('mat6tube.plus', False),            # phishing clone, not the family
    ('notmat6tube.com', False),          # substring must not match
    ('cdn.example.com', False),
    ('', False),
]:
    got_host = nd._is_noodle_family_host(host)
    report(got_host is want, f'noodle family? {host!r} -> {got_host}', f'(want {want})')

DOWNLOAD_PAGE = '''<html><head><script>
window.playlist = {"sources":[{"file":"https://cdn.example.com/v/1080.m3u8","label":"1080p","height":1080},{"file":"https://cdn.example.com/v/480.m3u8","label":"480p","height":480}],"previews":[{"file":"https://cdn.example.com/preview/teaser.mp4","label":"preview"}],"title":"Some Video"};
</script></head></html>'''

parsed = nd._parse_window_playlist_json(DOWNLOAD_PAGE)
report(isinstance(parsed, dict) and 'sources' in parsed and 'previews' in parsed,
       f'window.playlist parsed: {sorted(parsed or {})}')

semicolon_page = '<script>window.playlist = {"title":"a;b;c","sources":["https://x/y.m3u8"]};</script>'
parsed_semi = nd._parse_window_playlist_json(semicolon_page)
report((parsed_semi or {}).get('title') == 'a;b;c',
       f'a semicolon inside a string does not truncate: {(parsed_semi or {}).get("title")!r}')
report(nd._parse_window_playlist_json('<html>no playlist here</html>') is None,
       'page without window.playlist -> None')
report(nd._parse_window_playlist_json('<script>window.playlist = {"sources":[;</script>') is None,
       'truncated JSON -> None')

sources = nd._noodle_sources_from_playlist(parsed)
report(len(sources) == 2, f'preview key skipped, 2 real sources kept: {len(sources)}')
report(not any('preview' in u.lower() for u, _l, _h in sources),
       f'no preview URL among the sources: {[u for u, _l, _h in sources]}')
best = sorted(sources, key=lambda item: item[2] or 0, reverse=True)[0]
report(best[0].endswith('/1080.m3u8') and best[2] == 1080,
       f'highest source wins after sort: {best[0]} ({best[2]}p)')

label_only = nd._noodle_sources_from_playlist(
    {'sources': [{'file': 'https://x/720.m3u8', 'label': '720p'}]})
report(label_only and label_only[0][2] == 720,
       f'height recovered from label: {label_only}')

report(nd._noodle_sources_from_playlist(
    {'sources': [{'file': 'https://x/preview/a.m3u8'}]}) == [],
    'a source URL that is itself a preview is dropped')
report(nd._noodle_sources_from_playlist(None) == [],
   'None playlist -> no sources')

# ── 9. VOE/pvvstream must not resolve to the tr_ teaser ──────────────────────
class VoeStub:
    _PREVIEW_MEDIA_URL_TOKENS = lift_attr('VideoPlayer', '_PREVIEW_MEDIA_URL_TOKENS')
    _media_url_looks_like_preview = lift('VideoPlayer', '_media_url_looks_like_preview')
    _media_url_is_trailer = staticmethod(
        unwrap(lift('VideoPlayer', '_media_url_is_trailer')))
    _media_url_height_hint = staticmethod(
        unwrap(lift('VideoPlayer', '_media_url_height_hint')))
    _rank_real_media_candidates = lift('VideoPlayer', '_rank_real_media_candidates')


v = VoeStub()
CDN = 'https://cdn2.pvvstream.pro/videos/-128731116/456242439/'
for url, want in [
    (CDN + 'tr_240p.mp4', True),      # the exact URL from the reported log
    (CDN + 'tr_1080p.mp4', True),
    (CDN + '240p.mp4', False),
    (CDN + 'movie.mp4', False),
    (CDN + 'trailer.mp4', False),     # caught by the preview tokens instead
    (CDN + 'track.mp4', False),       # 'tr' + letters must not match
]:
    got_tr = v._media_url_is_trailer(url)
    report(got_tr is want, f'trailer? …/{url.rsplit("/", 1)[-1]} -> {got_tr}', f'(want {want})')

for url, want in [(CDN + 'tr_240p.mp4', 240), (CDN + '1080p.mp4', 1080),
                  (CDN + '480p_v2.mp4', 480), (CDN + 'movie.mp4', 0),
                  (CDN + '1080pixels.mp4', 0)]:
    got_h = v._media_url_height_hint(url)
    report(got_h == want, f'height …/{url.rsplit("/", 1)[-1]} -> {got_h}', f'(want {want})')

ranked = v._rank_real_media_candidates([
    CDN + 'tr_240p.mp4', CDN + '240p.mp4', CDN + 'tr_1080p.mp4',
    CDN + '1080p.mp4', CDN + '720p.mp4',
], 'VOE-mirror')
report([u.rsplit('/', 1)[-1] for u in ranked] == ['1080p.mp4', '720p.mp4', '240p.mp4'],
       f'teasers dropped, best quality first: {[u.rsplit("/", 1)[-1] for u in ranked]}')

only_trailers = v._rank_real_media_candidates(
    [CDN + 'tr_240p.mp4', CDN + 'tr_720p.mp4'], '')
report([u.rsplit('/', 1)[-1] for u in only_trailers] == ['tr_720p.mp4', 'tr_240p.mp4'],
       f'a page with only teasers still plays one: '
       f'{[u.rsplit("/", 1)[-1] for u in only_trailers]}')

report(v._rank_real_media_candidates([], '') == [], 'no candidates -> no candidates')

# ── 10. Captured-links panel: mirrors share one row, and say so ───────────────
class _FakeTreeItem:
    """Stand-in for the QTreeWidgetItem rows in the Captured-links panel."""

    def __init__(self, cols):
        self._cols = list(cols)

    def text(self, col):
        return self._cols[col] if col < len(self._cols) else ''

    def setText(self, col, value):
        while len(self._cols) <= col:
            self._cols.append('')
        self._cols[col] = value


class _FakeViewport:
    def update(self):
        pass


class _FakePlaylistWidget:
    def __init__(self, labels):
        self._labels = list(labels)

    def rowCount(self):
        return len(self._labels)

    def insertRow(self, idx):
        self._labels.insert(idx, '')

    def item(self, idx):
        if 0 <= idx < len(self._labels):
            return _FakeTreeItem([self._labels[idx]])
        return None

    def removeRow(self, idx):
        if 0 <= idx < len(self._labels):
            del self._labels[idx]

    def viewport(self):
        return _FakeViewport()


class AbsorbStub:
    """Real _relabel_absorbed_links; the rest of the panel is faked."""

    _relabel_absorbed_links = lift('VideoPlayer', '_relabel_absorbed_links')

    def __init__(self, absorbed, panel_urls=(), key_raises=False):
        self._last_mirror_absorptions = absorbed
        self._key_raises = key_raises
        self.notes = []
        self._link_flow_rows = {}
        for u in panel_urls:
            # key_raises stubs cannot build the dict through _link_flow_key;
            # they fall back to the same lowercase form main.py uses.
            k = (str(u or '').strip().lower() if key_raises
                 else self._link_flow_key(u))
            self._link_flow_rows[k] = _FakeTreeItem(
                ['STREAM  ' + u, 'done', 'added to playlist'])

    def _link_flow_key(self, url):
        if self._key_raises:
            raise RuntimeError('boom')
        return str(url or '').strip().lower()

    def _note_link(self, url, status='', detail=''):
        self.notes.append((url, status, detail))
        try:
            key = self._link_flow_key(url)
        except Exception:
            key = str(url or '').strip().lower()
        item = self._link_flow_rows.get(key)
        if item is not None:
            item.setText(1, status)
            item.setText(2, detail)


PAGE = 'https://javgg.net/v/abc'          # the row that survives
MIRROR = 'https://javstreamhq.to/e/abc'   # folded in, row deleted
LABEL = 'Dressage (1986)'
MERGED = ('merged into \u201c' + LABEL + '\u201d \u2014 use its mirror menu')

a = AbsorbStub({MIRROR: (PAGE, LABEL)}, panel_urls=[MIRROR, PAGE])
a._relabel_absorbed_links()
report(a.notes == [(MIRROR, 'mirror', MERGED)],
       f'absorbed link relabelled and names the row that kept it: {a.notes}')

b = AbsorbStub({MIRROR: (PAGE, LABEL)}, panel_urls=[])
b._relabel_absorbed_links()
report(b.notes == [] and b._link_flow_rows == {},
       f'a link that was never captured gets no panel row of its own: {b.notes}')

c = AbsorbStub({}, panel_urls=[MIRROR])
c._relabel_absorbed_links()
report(c.notes == [], f'nothing absorbed -> nothing relabelled: {c.notes}')

d = AbsorbStub({MIRROR: (PAGE, LABEL)}, panel_urls=[MIRROR], key_raises=True)
d._relabel_absorbed_links()
report(d.notes == [(MIRROR, 'mirror', MERGED)],
       f'a raising key function falls back and still matches: {d.notes}')

e = AbsorbStub({MIRROR: (PAGE, LABEL)}, panel_urls=[MIRROR])
e._relabel_absorbed_links()
e._relabel_absorbed_links()
report(len(e.notes) == 1,
       f're-running the collapse does not re-note the same row: {len(e.notes)} note(s)')

f = AbsorbStub({MIRROR: PAGE}, panel_urls=[MIRROR])
f._relabel_absorbed_links()
report(f.notes == [(MIRROR, 'mirror',
                    'merged into an existing row \u2014 use its mirror menu')],
       f'an absorption recorded without a label still relabels: {f.notes}')

g = AbsorbStub({MIRROR: (PAGE, 'X' * 80)}, panel_urls=[MIRROR])
g._relabel_absorbed_links()
report(len(g.notes) == 1 and g.notes[0][2].count('X') == 57
       and '\u2026' in g.notes[0][2],
       f'an over-long row label is truncated to 57 chars + ellipsis: '
       f'{g.notes[0][2].count("X")} X(s) kept')

h = AbsorbStub({MIRROR: (PAGE, LABEL)},
               panel_urls=[MIRROR, 'https://unrelated.example/x'])
h._relabel_absorbed_links()
report(h.notes == [(MIRROR, 'mirror', MERGED)],
       f'an unrelated captured link is untouched: {h.notes}')


# ── 10b. The collapse itself: one row, mirrors stored, panel corrected ────────
class CollapseStub(AbsorbStub):
    """Runs the REAL _collapse_duplicate_url_mirrors over two mirror rows."""

    _collapse_duplicate_url_mirrors = lift(
        'VideoPlayer', '_collapse_duplicate_url_mirrors')

    # The grouping keys are unchanged by this fix and depend on the metadata
    # layer; stub them so the collapse's own row/mirror/panel behaviour is
    # what runs here.
    def _mirror_display_group_key(self, file_path):
        return str(file_path or '').rsplit('/', 1)[-1].lower()

    def _mirror_path_key(self, file_path):
        return str(file_path or '').strip().rstrip('/').lower()

    def __init__(self, playlist, labels, panel_urls=(), split_urls=()):
        AbsorbStub.__init__(self, {}, panel_urls=panel_urls)
        self.playlist = list(playlist)
        self._split_urls = list(split_urls)
        self.rebuilds = 0
        self.playlist_widget = _FakePlaylistWidget(labels)
        self._playlist_url_mirrors = {}
        self.current_file = ''
        self._mirror_exact_name_cache = {}
        self._mirror_group_cache = {}
        self._mirror_path_cache = {}
        self.filtered_out = 0

    def _is_remote_url(self, p):
        return str(p or '').lower().startswith('http')

    def _canonicalize_remote_source_url(self, p):
        return p

    def _is_jav_site_host(self, host):
        return False

    def _mirrors_for_visible_url(self, p):
        return list(self._playlist_url_mirrors.get(p) or [])

    def _set_mirrors_for_primary(self, primary, mirrors):
        self._playlist_url_mirrors[primary] = list(mirrors)

    def _unique_paths(self, paths):
        out, seen = [], set()
        for p in paths:
            k = str(p or '').strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(p)
        return out

    def _remember_active_mirror(self, a, b):
        pass

    def _split_conflicting_fileditch_mirrors(self):
        # Mirrors the real helper exactly: it inserts into self.playlist and
        # never touches playlist_widget.
        for url in self._split_urls:
            self.playlist.insert(1, url)
        return bool(self._split_urls)

    def _split_conflicting_pornhub_mirrors(self):
        return False

    def rebuild_playlist_table(self):
        self.rebuilds += 1
        self.playlist_widget._labels = ['row'] * len(self.playlist)
        return True

    def apply_playlist_filtering(self):
        self.filtered_out += 1


cs = CollapseStub([PAGE, MIRROR], ['  ' + LABEL, 'javstreamhq mirror'],
                  panel_urls=[MIRROR, PAGE])
changed = cs._collapse_duplicate_url_mirrors()
report(changed is True and cs.playlist == [PAGE],
       f'mirrors share one row: playlist={cs.playlist}')
report(cs._mirrors_for_visible_url(PAGE) == [MIRROR],
       f'the folded link is stored on the surviving row, so its mirror menu '
       f'can still play it: {cs._mirrors_for_visible_url(PAGE)}')
report(cs._playlist_url_mirrors and MIRROR not in cs.playlist,
       'the absorbed row is gone from the playlist')
report(cs.notes == [(MIRROR, 'mirror', MERGED)],
       f'and the panel says where it went, naming that row: {cs.notes}')
report(cs.playlist_widget.item(0) is not None
       and cs.playlist_widget.item(1) is None,
       'the widget lost exactly the absorbed row')

cs2 = CollapseStub([PAGE, 'https://example.org/only-one'],
                   [LABEL, 'something else'], panel_urls=[PAGE])
report(cs2._collapse_duplicate_url_mirrors() is False
       and cs2.notes == [] and cs2.playlist == [PAGE, 'https://example.org/only-one'],
       f'rows that are not mirrors are left alone: {cs2.playlist}')

# ── 11. sxyprn: signed CDN path, and the mirrors written into the h1 ──────────
class SxyprnStub:
    _is_sxyprn_host = lift('VideoPlayer', '_is_sxyprn_host')
    _sxyprn_digit_sum = staticmethod(lift('VideoPlayer', '_sxyprn_digit_sum'))
    _sxyprn_cdn_token = staticmethod(lift('VideoPlayer', '_sxyprn_cdn_token'))
    _sxyprn_cdn_path = lift('VideoPlayer', '_sxyprn_cdn_path')
    _sxyprn_clean_title = staticmethod(lift('VideoPlayer', '_sxyprn_clean_title'))
    _sxyprn_title_and_mirrors = lift('VideoPlayer', '_sxyprn_title_and_mirrors')
    _sxyprn_vnfo_sources = staticmethod(lift('VideoPlayer', '_sxyprn_vnfo_sources'))
    _media_url_height_hint = staticmethod(
        lift('VideoPlayer', '_media_url_height_hint'))
    _SXYPRN_TITLE_LINK_DENYLIST = lift_attr(
        'VideoPlayer', '_SXYPRN_TITLE_LINK_DENYLIST')
    _SXYPRN_MIRROR_STOP_MARKERS = lift_attr(
        'VideoPlayer', '_SXYPRN_MIRROR_STOP_MARKERS')
    _sxyprn_scan_anchor = lift('VideoPlayer', '_sxyprn_scan_anchor')


sx = SxyprnStub()
for host, want in [('sxyprn.com', True), ('www.sxyprn.com', True),
                   ('sxyprn.net', True), ('sub.sxyprn.io', True),
                   ('sxyprn.com:8443', True), ('SXYPRN.COM', True),
                   ('notsxyprn.com', False), ('sxyprn-clone.com', False),
                   ('sxyprn.com.evil.net', False), ('sxyprn', False),
                   ('', False)]:
    got = sx._is_sxyprn_host(host)
    report(got == want, f'sxyprn host? {host!r} -> {got}', f'(want {want})')

report(sx._sxyprn_digit_sum('64d19fdf6970d') == 42,
       f'digit sum of a hex segment: {sx._sxyprn_digit_sum("64d19fdf6970d")}')
report(sx._sxyprn_digit_sum('1789333200') == 36
       and sx._sxyprn_digit_sum('abc') == 0 and sx._sxyprn_digit_sum('') == 0,
       'digit sum of a timestamp / of a segment with no digits')

report(sx._sxyprn_cdn_token(42, 'sxyprn.com', 40) == 'NDItc3h5cHJuLmNvbS00MA..',
       f'token is base64 with = -> . : {sx._sxyprn_cdn_token(42, "sxyprn.com", 40)}')
report(sx._sxyprn_cdn_token(42, 'sxyprn.com', 40, urlsafe=False)
       == 'NDItc3h5cHJuLmNvbS00MA==',
       'the plain-base64 fallback keeps the padding')

RAW_VNFO = '/sd/1/9/vid/1789333200/64d19fdf6970d/6aa5e08ed8733/0.mp4'
got_path = sx._sxyprn_cdn_path(RAW_VNFO, 'sxyprn.com')
report(got_path == ('/sd8/NDItc3h5cHJuLmNvbS00MA../1/9/vid/1789333118/'
                    '64d19fdf6970d/6aa5e08ed8733/0.mp4'),
       f'segment 1 gets the 8/<token>, the timestamp is wound back by 82: '
       f'{got_path}')
report(sx._sxyprn_cdn_path(RAW_VNFO, 'sxyprn.com', urlsafe=False) == (
    '/sd8/NDItc3h5cHJuLmNvbS00MA==/1/9/vid/1789333118/'
    '64d19fdf6970d/6aa5e08ed8733/0.mp4'),
    'the plain-alphabet variant differs only in the token')
report(sx._sxyprn_cdn_path(
    '/sd/1/9/vid/9_MnhCBZC0X9suB3k8e0ww/64d19fdf6970d/6aa5e08ed8733/0.mp4',
    'sxyprn.com') == ('/sd8/NDItc3h5cHJuLmNvbS00MA../1/9/vid/'
                      '9_MnhCBZC0X9suB3k8e0ww/64d19fdf6970d/6aa5e08ed8733/0.mp4'),
    'a non-numeric segment 5 still signs the path, it just is not rewound')
report(sx._sxyprn_cdn_path('/sd/1/9.mp4', 'sxyprn.com') == ''
       and sx._sxyprn_cdn_path('', 'sxyprn.com') == '',
       'a path too short to sign is rejected')

srcs = sx._sxyprn_vnfo_sources(
    """<div class="vidsnfo" data-vnfo='{"480p":"/a/b/c/d/e/1/2/3.mp4",
       "720p":"/a/b/c/d/e/1/2/4.mp4"}'></div>""")
report(sorted(srcs) == [('480p', '/a/b/c/d/e/1/2/3.mp4'),
                        ('720p', '/a/b/c/d/e/1/2/4.mp4')],
       f'data-vnfo parsed: {sorted(srcs)}')
ordered = sorted(srcs, key=lambda it: sx._media_url_height_hint(it[0]),
                 reverse=True)
report(ordered[0][0] == '720p', f'best quality probed first: {ordered[0][0]}')

escaped = sx._sxyprn_vnfo_sources(
    '<span data-vnfo="{&quot;1080p&quot;:&quot;/x/1/2/3/4/5/6/7.mp4&quot;}">')
report(escaped == [('1080p', '/x/1/2/3/4/5/6/7.mp4')],
       f'HTML-escaped data-vnfo also parses: {escaped}')
report(sx._sxyprn_vnfo_sources('<span data-vnfo="{not json">') == []
       and sx._sxyprn_vnfo_sources('<span data-vnfo="[1,2]">') == []
       and sx._sxyprn_vnfo_sources('<p>nothing here</p>') == [],
       'broken JSON, a non-object and a missing attribute all give nothing')

H1 = ('<h1 class="post_el_title"><span class="title_part"><b>NEW</b> '
      '<a href="https://sxyprn.com/Martina-Smeraldi.html">Martina Smeraldi</a> '
      '<a href="https://sxyprn.com/LeoLulu.html">LeoLulu</a> New Anal Scene '
      'With The Hottest French Couple In The World 2026 '
      '<a class="hash_link" href="https://sxyprn.com/Anal.html?sm=trending">#Anal</a> '
      '<a class="hash_link" href="https://sxyprn.com/POV.html?sm=trending">#POV</a> '
      '<a href="https://doodstream.com/e/qlb9nbe23jda" title="&gt;External Link!&lt;">doodstream.com</a> '
      '<a href="https://lulustream.com/e/iqxjl8h8yted" title="&gt;External Link!&lt;">lulustream.com</a>'
      '</span></h1>')
title, found = sx._sxyprn_title_and_mirrors(
    '<html><body>' + H1 + '<div>rest of page</div></body></html>',
    'https://sxyprn.com/post/6aa5e08ed8733.html')
report(found == ['https://doodstream.com/e/qlb9nbe23jda',
                 'https://lulustream.com/e/iqxjl8h8yted'],
       f'the hosters in the h1 are the mirrors: {found}')
report(title == ('NEW Martina Smeraldi LeoLulu New Anal Scene With The '
                 'Hottest French Couple In The World 2026'),
       f'row name keeps the models, drops hashtags and hoster text: {title!r}')

bare_title, bare_mirrors = sx._sxyprn_title_and_mirrors(
    '<h1><a href="https://sxyprn.com/Model.html">Model</a> Solo Scene '
    '<a href="https://sxyprn.com/Solo.html?sm=trending">#Solo</a></h1>',
    'https://sxyprn.net/post/x.html')
report(bare_mirrors == [] and bare_title == 'Model Solo Scene',
       f'a post with no hosters has no mirrors: {bare_title!r} {bare_mirrors}')

junk_title, junk_mirrors = sx._sxyprn_title_and_mirrors(
    '<h1>Scene 2026 '
    '<a href="https://myporn.club/t/ffa6WDgH">TORRENT</a> '
    '<a href="https://sxypix.com/x">PIX</a> '
    '<a href="https://sub.sxyprn.com/y">sxyprn</a> '
    '<a href="https://vidara.to/e/rexdaO4iYPQu">vidara.to</a></h1>',
    'https://sxyprn.com/post/y.html')
report(junk_mirrors == ['https://vidara.to/e/rexdaO4iYPQu'],
       f'torrent/pix/sxyprn links are not mirrors, vidara is: {junk_mirrors}')
report(junk_title == 'Scene 2026 TORRENT PIX sxyprn',
       f'and their text stays in the title: {junk_title!r}')

# the real shape seen on a post that also links other scenes by the model
other_title, other_mirrors = sx._sxyprn_title_and_mirrors(
    '<h1>I Take Max Hangover Away With My Tight Pussy After A Long Night '
    '<a class="hash_link" href="https://sxyprn.com/anal.html?sm=trending">#anal</a> '
    'FULL HD -> <a href="https://vidara.so/v/OzStT6iJG0R8v">vidara.so</a> '
    '{More scenes of this model} SCENE 1 -> '
    '<a href="https://vidara.so/v/6xm99e2D6kzUy">vidara.so</a> SCENE 2 -> '
    '<a href="https://vidara.so/v/JwJFVVuR4gM9W">vidara.so</a></h1>',
    'https://sxyprn.com/post/6aa63889680a3.html')
report(other_mirrors == ['https://vidara.so/v/OzStT6iJG0R8v'],
       f'only the FULL HD link is a mirror; the SCENE links are other videos: '
       f'{other_mirrors}')
report(other_title == ('I Take Max Hangover Away With My Tight Pussy After A '
                       'Long Night'),
       f'and the title stops at the {{More scenes}} marker: {other_title!r}')

dupe_title, dupe_mirrors = sx._sxyprn_title_and_mirrors(
    '<h1>Twice <a href="https://doodstream.com/e/aaa">a</a> '
    '<a href="https://doodstream.com/e/aaa">a again</a></h1>',
    'https://sxyprn.com/post/z.html')
report(dupe_mirrors == ['https://doodstream.com/e/aaa'],
       f'the same hoster listed twice is one mirror: {dupe_mirrors}')

no_h1, no_h1_mirrors = sx._sxyprn_title_and_mirrors(
    '<html><body><div>no heading</div></body></html>',
    'https://sxyprn.com/post/w.html')
report(no_h1 == '' and no_h1_mirrors == [],
       'a page without an h1 yields nothing rather than garbage')

# ── 10c. A mirror split must not desync the model from the widget ─────────────
SPLIT_URL = 'https://fileditchfiles.me/other/SomeOtherFile.mp4'
sp = CollapseStub([PAGE, MIRROR], [LABEL, 'javstreamhq mirror'],
                  split_urls=[SPLIT_URL])
sp._collapse_duplicate_url_mirrors()
report(sp.rebuilds == 1,
       f'a split that added a row rebuilds the table: {sp.rebuilds} rebuild(s)')
report(len(sp.playlist) == sp.playlist_widget.rowCount(),
       f'model and widget agree: {len(sp.playlist)} playlist entries vs '
       f'{sp.playlist_widget.rowCount()} widget rows')
report(SPLIT_URL in sp.playlist, f'the detached file is its own row: {SPLIT_URL}')

no_split = CollapseStub([PAGE, MIRROR], [LABEL, 'javstreamhq mirror'])
no_split._collapse_duplicate_url_mirrors()
report(no_split.rebuilds == 0,
       f'no split -> no needless rebuild: {no_split.rebuilds} rebuild(s)')
report(len(no_split.playlist) == no_split.playlist_widget.rowCount(),
       f'an ordinary collapse stays in sync: {len(no_split.playlist)} vs '
       f'{no_split.playlist_widget.rowCount()}')

# ── 12. FamilyPornHD with no get_file stream: keep the other hoster ──────────
class FamilyStub:
    _familypornhd_non_ad_media_candidates = lift(
        'VideoPlayer', '_familypornhd_non_ad_media_candidates')
    _capture_candidate_is_media = lift(
        'VideoPlayer', '_capture_candidate_is_media')
    _FAMILYPORNHD_AD_HOST_TOKENS = lift_attr(
        'VideoPlayer', '_FAMILYPORNHD_AD_HOST_TOKENS')
    _PLAYABLE_MEDIA_SUFFIXES = lift_attr(
        'VideoPlayer', '_PLAYABLE_MEDIA_SUFFIXES')
    _rank_real_media_candidates = lift(
        'VideoPlayer', '_rank_real_media_candidates')
    _media_url_is_trailer = staticmethod(
        lift('VideoPlayer', '_media_url_is_trailer'))
    # instance method in main.py, so it binds normally (no staticmethod wrap)
    _media_url_looks_like_preview = lift(
        'VideoPlayer', '_media_url_looks_like_preview')
    _media_url_height_hint = staticmethod(
        lift('VideoPlayer', '_media_url_height_hint'))
    _PREVIEW_MEDIA_URL_TOKENS = lift_attr(
        'VideoPlayer', '_PREVIEW_MEDIA_URL_TOKENS')


fam = FamilyStub()
AD = 'https://playhubconnect.com/media/12345/video.mp4'
HOSTER = 'https://doodstream.example/d/abc/master.m3u8'
kept = fam._familypornhd_non_ad_media_candidates([AD, HOSTER, AD])
report(kept == [HOSTER],
       f'the advert is dropped, the other hoster survives: {kept}')
report(fam._familypornhd_non_ad_media_candidates([AD, AD]) == [],
       'a page with only the ad network still yields nothing')
report(fam._familypornhd_non_ad_media_candidates([]) == []
       and fam._familypornhd_non_ad_media_candidates(None) == [],
       'no candidates in, no candidates out')
report(fam._familypornhd_non_ad_media_candidates(
    ['https://cdn.example.org/v/720.mp4']) == ['https://cdn.example.org/v/720.mp4'],
    'an unknown host is treated as a possible hoster, not an advert')
report(fam._familypornhd_non_ad_media_candidates(
    ['https://cdn.example.org/r/a-ads.com/spot.mp4']) == [],
    'an ad token in the path is dropped too')
report(fam._familypornhd_non_ad_media_candidates(
    ['https://cdn.example.org/v/360p.mp4', 'https://cdn.example.org/v/1080p.mp4',
     'https://cdn.example.org/v/720p.mp4'])
    == ['https://cdn.example.org/v/1080p.mp4',
        'https://cdn.example.org/v/720p.mp4',
        'https://cdn.example.org/v/360p.mp4'],
    'the tallest rendition is probed first')

# ── 13. The real watchstreamhd capture: gtag/js must never be the video ──────
class CaptureStub:
    _capture_candidate_is_media = lift(
        'VideoPlayer', '_capture_candidate_is_media')
    _capture_candidate_is_clearly_not_media = lift(
        'VideoPlayer', '_capture_candidate_is_clearly_not_media')
    _familypornhd_non_ad_media_candidates = lift(
        'VideoPlayer', '_familypornhd_non_ad_media_candidates')
    _PLAYABLE_MEDIA_SUFFIXES = lift_attr(
        'VideoPlayer', '_PLAYABLE_MEDIA_SUFFIXES')
    _NON_MEDIA_URL_SUFFIXES = lift_attr(
        'VideoPlayer', '_NON_MEDIA_URL_SUFFIXES')
    _NON_MEDIA_HOST_TOKENS = lift_attr(
        'VideoPlayer', '_NON_MEDIA_HOST_TOKENS')
    _FAMILYPORNHD_AD_HOST_TOKENS = lift_attr(
        'VideoPlayer', '_FAMILYPORNHD_AD_HOST_TOKENS')
    _rank_real_media_candidates = lift(
        'VideoPlayer', '_rank_real_media_candidates')
    _media_url_is_trailer = staticmethod(
        lift('VideoPlayer', '_media_url_is_trailer'))
    _media_url_looks_like_preview = lift(
        'VideoPlayer', '_media_url_looks_like_preview')
    _media_url_height_hint = staticmethod(
        lift('VideoPlayer', '_media_url_height_hint'))
    _PREVIEW_MEDIA_URL_TOKENS = lift_attr(
        'VideoPlayer', '_PREVIEW_MEDIA_URL_TOKENS')
    _familypornhd_player_page = lift(
        'VideoPlayer', '_familypornhd_player_page')
    _capture_candidate_is_obfuscated_master = lift(
        'VideoPlayer', '_capture_candidate_is_obfuscated_master')
    _is_familypornhd_direct_video_url = lift(
        'VideoPlayer', '_is_familypornhd_direct_video_url')


cap = CaptureStub()

# MEDIA_URL lines, in the order the field log recorded them.
CAPTURE = [
    'https://playhubconnect.com/bn/alternative/709/557/01d/70955701d2cb9942742e85402b1cc08a9aba7c9b-alt.mp4',
    'https://www.google-analytics.com/analytics.js',
    'https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c&gtm=4e6992',
    'https://familypornhd.com/wp-content/plugins/wordpress-popular-posts/assets/js/wpp.min.js?ver=7.4.2',
    'https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1',
    'https://familypornhd.com/wp-includes/js/jquery/jquery-migrate.min.js?ver=3.4.1',
    'https://familypornhd.com/wp-content/themes/bimber/js/modernizr/modernizr-custom.min.js?ver=3.3.0',
    'https://www.googletagmanager.com/gtag/js?id=UA-111541081-1',
    'https://39d40f50ae.0b00553438.com/bcdb62f5b5b2c30a9c3bbfc8ee20abf3.js',
    'https://js.capndr.com/advertising.js',
    'https://39d40f50ae.0b00553438.com/c83f59e5c147ebcaaaaf9577fb5e80a9.js',
    'https://39d40f50ae.0b00553438.com/72bd3653831c516f6cb321b195ed824a.js',
    'https://familypornhd.com/wp-includes/js/wp-emoji-release.min.js?ver=e1578e0491d256a9d0bd12bdc3f7d822',
    'https://secure.gravatar.com/avatar/7d5bc0da74f3aae75a18708660ce5075f6d99228cbb56a2cbbe5f36ced39e5b5?s=40&d=mm&r=x',
    'https://watchstreamhd.com/video/b20bb95ab626d93fd976af958fbc61ba',
    'https://familypornhd.com/wp-includes/js/comment-reply.min.js?ver=e1578e0491d256a9d0bd12bdc3f7d822',
    'https://familypornhd.com/wp-content/themes/bimber/js/stickyfill/stickyfill.min.js?ver=2.0.3',
    'https://familypornhd.com/wp-content/themes/bimber/js/jquery.placeholder/placeholders.jquery.min.js?ver=4.0.1',
    'https://familypornhd.com/wp-content/themes/bimber/js/jquery.timeago/jquery.timeago.js?ver=1.5.2',
    'https://familypornhd.com/wp-content/themes/bimber/js/jquery.timeago/locales/jquery.timeago.en.js',
    'https://familypornhd.com/wp-content/themes/bimber/js/matchmedia/matchmedia.js',
    'https://familypornhd.com/wp-content/themes/bimber/js/matchmedia/matchmedia.addlistener.js',
    'https://familypornhd.com/wp-content/themes/bimber/js/picturefill/picturefill.min.js?ver=2.3.1',
    'https://familypornhd.com/wp-content/themes/bimber/js/jquery.waypoints/jquery.waypoints.min.js?ver=4.0.0',
    'https://familypornhd.com/wp-content/themes/bimber/js/enquire/enquire.min.js?ver=2.1.2',
    'https://familypornhd.com/wp-content/themes/bimber/js/global.js?ver=9.2.5',
    'https://familypornhd.com/wp-content/themes/bimber/js/libgif/libgif.js',
    'https://familypornhd.com/wp-content/themes/bimber/js/players.js?ver=9.2.5',
    'https://familypornhd.com/wp-includes/js/jquery/ui/core.min.js?ver=1.14.2',
    'https://familypornhd.com/wp-includes/js/jquery/ui/menu.min.js?ver=1.14.2',
    'https://familypornhd.com/wp-includes/js/dist/dom-ready.min.js?ver=3fe927cab37bf38d6a23',
    'https://familypornhd.com/wp-includes/js/dist/hooks.min.js?ver=f0f188028580e8dc1255',
    'https://familypornhd.com/wp-includes/js/dist/i18n.min.js?ver=1dfe7db3940c23ea9216',
    'https://familypornhd.com/wp-includes/js/dist/a11y.min.js?ver=31c6cec5a4ff7aff483d',
    'https://familypornhd.com/wp-includes/js/jquery/ui/autocomplete.min.js?ver=1.14.2',
    'https://familypornhd.com/wp-content/themes/bimber/js/ajax-search.js?ver=9.2.5',
    'https://familypornhd.com/wp-content/themes/bimber/js/single.js?ver=9.2.5',
    'https://familypornhd.com/wp-content/themes/bimber/js/skin-mode.js?ver=9.2.5',
    'https://familypornhd.com/wp-content/themes/bimber/js/back-to-top.js?ver=9.2.5',
    'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1',
    'https://watchstreamhd.com/player/assets/scripts.php?v=6',
    'https://watchstreamhd.com/player/assets/remodal/remodal.min.js',
    'https://ssl.p.jwpcdn.com/player/v/8.34.3/jwplayer.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes.min.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes-format.js',
    'https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/master.txt',
    'https://mediaboxplayer.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_360p.mp4?md5=ZS0deTcKybv2DvkSvx1Q1A&expires=1789424661',
    'https://mediaboxplayer.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_720p.mp4?md5=DxDniXVsldpT5PoFc98DGA&expires=1789424661',
]
MP4_720 = CAPTURE[-1]
MP4_360 = CAPTURE[-2]
MASTER = CAPTURE[-3]

report(cap._capture_candidate_is_media(MP4_720) is True,
       'the mediaboxplayer MP4 is media')
report(cap._capture_candidate_is_media(MASTER) is False,
       'the master is disguised as .txt but is ciphertext, not a stream')
report(cap._capture_candidate_is_obfuscated_master(MASTER) is True,
       'it is recognised as the player master instead')
report(cap._capture_candidate_is_media(
    'https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c') is False,
    'the Google tag endpoint that got played as a video is not media')
report(all(not cap._capture_candidate_is_media(u) for u in CAPTURE
           if u.endswith(('.js', '.php')) or '/wp-' in u or 'gravatar' in u),
    'no script, theme asset or avatar in the capture passes as media')

ordered = sorted(CAPTURE, key=lambda u: 0 if cap._capture_candidate_is_media(u) else 1)
window = ordered[:12]
report(MP4_720 in window and MP4_360 in window,
       f'both real streams fit the 12-candidate window after sorting: '
       f'{[u.rsplit("/", 1)[-1][:34] for u in window if cap._capture_candidate_is_media(u)]}')
report(MP4_720 not in CAPTURE[:12],
       'and before sorting they were outside it, which is why nothing played')

kept = cap._familypornhd_non_ad_media_candidates(CAPTURE)
report(MP4_720 in kept and MP4_360 in kept and MASTER not in kept,
       f'the fallback keeps the two streams and not the master: '
       f'{len(kept)} candidate(s)')
report(not any('googletagmanager' in u or u.endswith('.js') or u.endswith('.php')
               for u in kept),
       f'and no script among them: {[u.rsplit("/", 1)[-1][:30] for u in kept]}')
report(not any('playhubconnect' in u for u in kept),
       'the playhubconnect pre-roll is still dropped as an advert')

report(cap._capture_candidate_is_clearly_not_media(
    'https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c') is True,
    'the unverified-promotion fallback now rejects the tag endpoint')
report(cap._capture_candidate_is_clearly_not_media(
    'https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1') is True,
    'and a jQuery file')
report(cap._capture_candidate_is_clearly_not_media(
    'https://cdn.example.org/get_video?id=12345') is False,
    'but an extension-less stream still survives, so the fallback keeps working')
report(cap._capture_candidate_is_clearly_not_media(MASTER) is False
       and cap._capture_candidate_is_clearly_not_media(MP4_720) is False,
    'the real streams are not rejected either')

# ── 14. Referer for the embedded player's CDN, not the article ───────────────
ARTICLE = 'https://familypornhd.com/caught-red-handed/'
PLAYER = 'https://watchstreamhd.com/video/b20bb95ab626d93fd976af958fbc61ba'
report(cap._familypornhd_player_page(CAPTURE, ARTICLE) == PLAYER,
       f'the watchstreamhd player page becomes the Referer: '
       f'{cap._familypornhd_player_page(CAPTURE, ARTICLE)}')

OWN_PLAYER = [
    'https://dev.familypornhd.com/get_file/0/0JjZXisx.mp4/?v-acctoken=NTB8&embed=true',
    'https://dev.familypornhd.com/embed/50',
    'https://dev.familypornhd.com/player/kt_player.js?v=9.15.15',
    'https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1',
]
report(cap._familypornhd_player_page(OWN_PLAYER, ARTICLE) == '',
       f"the site's own player on dev.familypornhd.com is left alone: "
       f'{cap._familypornhd_player_page(OWN_PLAYER, ARTICLE)!r}')
report(cap._familypornhd_player_page(OWN_PLAYER, 'https://familypornhd.com/x/') == ''
       and cap._familypornhd_player_page([], ARTICLE) == ''
       and cap._familypornhd_player_page(None, ARTICLE) == '',
       'a subdomain of the article host, an empty list and None all give nothing')

HLS_ONLY = [
    'https://watchstreamhd.com/player/assets/scripts.php?v=6',
    'https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/master.txt',
    'https://mediaboxnow.com/cdn/down/76603cb/files/x_eng_720p.mp4?md5=a&expires=1',
]
report(cap._familypornhd_player_page(HLS_ONLY, ARTICLE) == 'https://watchstreamhd.com',
       f'with no /video/ page the player origin is used: '
       f'{cap._familypornhd_player_page(HLS_ONLY, ARTICLE)}')
report(cap._familypornhd_player_page(
    ['https://mediaboxnow.com/cdn/down/x/files/y_eng_720p.mp4?md5=a&expires=1'],
    ARTICLE) == '',
    'the CDN alone does not identify a player')

# ── 15. master.txt is ciphertext, not a stream ───────────────────────────────
# Both captures below are verbatim from the field log. "straight-narrow" is
# the case where the player reached its master but never fetched a /cdn/down/
# file inside the capture window; "finally" is the case that played at
# 1280x720 once the Referer became the player page.
NOISE = [
    'https://playhubconnect.com/bn/alternative/709/557/01d/70955701d2cb9942742e85402b1cc08a9aba7c9b-alt.mp4',
    'https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c&gtm=4e6992',
    'https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1',
    'https://js.capndr.com/advertising.js',
    'https://secure.gravatar.com/avatar/7d5bc0da74f3aae75a18708660ce5075f6d99228cbb56a2cbbe5f36ced39e5b5?s=40&d=mm&r=x',
    'https://static.cloudflareinsights.com/beacon.min.js/v31edd6df95cf4e85bb4c19e7a9bdbcba1788362987495',
    '//phonydepth.com/c/Dw9/6.bf2j5_l_ScW_Qc9ONvj/AlzXNETTUd0/OkC/0/2BMeDsM/1aN/TFQS5t',
]
WS_ASSETS = [
    'https://watchstreamhd.com/player/assets/scripts.php?v=6',
    'https://watchstreamhd.com/player/assets/remodal/remodal.min.js',
    'https://ssl.p.jwpcdn.com/player/v/8.34.3/jwplayer.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes.min.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes-format.js',
]

NARROW = NOISE + [
    'https://watchstreamhd.com/video/c44e503833b64e9f27197a484f4257c0',
] + WS_ASSETS + [
    'https://watchstreamhd.com/cdn/hls/871885b5ce8f4eb8663ee09a36fa44a7/master.txt',
]
FINALLY = NOISE + [
    'https://watchstreamhd.com/video/97af4fb322bb5c8973ade16764156bed',
] + WS_ASSETS + [
    'https://watchstreamhd.com/cdn/hls/43c313477ed1dfebd68cb9d0d26eb8c8/master.txt',
    'https://videostreamingworld.com/cdn/down/43c313477ed1dfebd68cb9d0d26eb8c8/files/finally_und_360p.mp4?md5=LK-vMtRPKtRdC2SxYj5QFg&expires=1789431441',
    'https://videostreamingworld.com/cdn/down/43c313477ed1dfebd68cb9d0d26eb8c8/files/finally_und_720p.mp4?md5=m0wGaIrlvoMtVtT_mdW3fw&expires=1789431441',
]
NARROW_MASTER = 'https://watchstreamhd.com/cdn/hls/871885b5ce8f4eb8663ee09a36fa44a7/master.txt'

report(cap._capture_candidate_is_obfuscated_master(NARROW_MASTER) is True,
       'the disguised master is recognised as one')
report(cap._capture_candidate_is_media(NARROW_MASTER) is False,
       'but no longer counts as media, so it cannot be queued for mpv')
report(cap._capture_candidate_is_media(
    'https://videostreamingworld.com/cdn/down/x/files/y_eng_720p.mp4?md5=a&expires=1') is True
    and cap._capture_candidate_is_obfuscated_master(
        'https://videostreamingworld.com/cdn/down/x/files/y_eng_720p.mp4?md5=a&expires=1') is False,
    'a real /cdn/down/ MP4 is media and is not mistaken for a master')

report(cap._familypornhd_non_ad_media_candidates(NARROW) == [],
       f'a capture holding only the master yields no candidate: '
       f'{cap._familypornhd_non_ad_media_candidates(NARROW)}')
report(cap._familypornhd_player_page(
           NARROW, 'https://familypornhd.com/straight-narrow/')
       == 'https://watchstreamhd.com/video/c44e503833b64e9f27197a484f4257c0',
       'the master still identifies the player for the Referer')

_fin = cap._familypornhd_non_ad_media_candidates(FINALLY)
report(len(_fin) == 2 and 'finally_und_720p.mp4' in _fin[0]
       and 'finally_und_360p.mp4' in _fin[1],
       f'the played 720p file is still first and the master is dropped: {_fin}')
report(cap._familypornhd_player_page(
           FINALLY, 'https://familypornhd.com/finally/')
       == 'https://watchstreamhd.com/video/97af4fb322bb5c8973ade16764156bed',
       'the finally capture still resolves its player page')

_caught = cap._familypornhd_non_ad_media_candidates(CAPTURE)
report(len(_caught) == 2 and 'caught-red-handed_eng_720p.mp4' in _caught[0],
       f'regression guard — caught-red-handed still picks 720p: {_caught}')

# ── 16. the player page must survive the 12-candidate window ────────────────
# af078f0 made master.txt non-media, which correctly kept it away from mpv but
# also pushed the only /cdn/hls/ marker out of the truncated window. The
# Referer detection then found nothing and the /cdn/down/ files went back to
# 500. This capture is the field list for caught-red-handed, trimmed to the
# entries that decide the outcome but keeping their real positions.
CRH = [
    'https://playhubconnect.com/bn/alternative/709/557/01d/70955701d2cb9942742e85402b1cc08a9aba7c9b-alt.mp4',
    'https://www.google-analytics.com/analytics.js',
    'https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c&gtm=4e6992',
    'https://familypornhd.com/wp-content/plugins/wordpress-popular-posts/assets/js/wpp.min.js?ver=7.4.2',
    'https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1',
    'https://familypornhd.com/wp-includes/js/jquery/jquery-migrate.min.js?ver=3.4.1',
    'https://familypornhd.com/wp-content/themes/bimber/js/modernizr/modernizr-custom.min.js?ver=3.3.0',
    'https://www.googletagmanager.com/gtag/js?id=UA-111541081-1',
    'https://855c33fea3.df3f107f93.com/1a39882995cce8f50cbf6de58c176067.js',
    'https://js.capndr.com/advertising.js',
    'https://855c33fea3.df3f107f93.com/d6aea5d7618730bc69da23ec176067.js',
    'https://855c33fea3.df3f107f93.com/67fab6bb68b16a2c7592f9cd4bdb2727.js',
    'https://familypornhd.com/wp-includes/js/wp-emoji-release.min.js',
    'https://secure.gravatar.com/avatar/7d5bc0da74f3aae75a18708660ce5075?s=40&d=mm&r=x',
    'https://watchstreamhd.com/video/b20bb95ab626d93fd976af958fbc61ba',
    'https://familypornhd.com/wp-content/themes/bimber/js/global.js?ver=9.2.5',
    '//phonydepth.com/c/Dw9/6.bf2j5_l_ScW_Qc9ONvj/AlzXNETTUd0/OkC/0/2BMeDsM/1aN/TFQS5t',
    'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1',
    '//code.jquery.com/jquery-1.12.4.min.js',
    'https://watchstreamhd.com/player/assets/scripts.php?v=6',
    'https://ssl.p.jwpcdn.com/player/v/8.34.3/jwplayer.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes.min.js',
    'https://watchstreamhd.com/player/assets/js/cryptojs-aes-format.js',
    'https://static.cloudflareinsights.com/beacon.min.js/v31edd6df95cf4e8',
    'https://excavatenearbywand.com/aas/r45d/vki/2089950/402c05c4.js',
    'https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/master.txt',
    'https://mediaplayerboxx.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_360p.mp4?md5=gwV4m907cX99BcsXQuePcw&expires=1789433374',
    'https://mediaplayerboxx.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_720p.mp4?md5=a1vp09GSCu5O6hfQ_D4Fog&expires=1789433374',
]
ART = 'https://familypornhd.com/caught-red-handed/'
PLAYER_PAGE = 'https://watchstreamhd.com/video/b20bb95ab626d93fd976af958fbc61ba'

_sorted = sorted(CRH, key=lambda u: 0 if cap._capture_candidate_is_media(u) else 1)
_window = _sorted[:12]
report(PLAYER_PAGE not in _window,
       f'the player page really is outside the 12-candidate window '
       f'({_window.index(PLAYER_PAGE) if PLAYER_PAGE in _window else "absent"}), '
       f'which is what broke the Referer')
report(cap._familypornhd_player_page(_window, ART) == '',
       'so handing the detector the truncated list finds no player at all')
report(cap._familypornhd_player_page(_sorted, ART) == PLAYER_PAGE,
       f'handing it the full capture finds the player page: '
       f'{cap._familypornhd_player_page(_sorted, ART)}')

_call = re.search(
    r'_family_referer\s*=\s*self\._familypornhd_player_page\(\s*(\w+)',
    open('main.py', encoding='utf-8').read())
report(_call is not None and _call.group(1) == 'media_candidates',
       f'the call site passes the full capture, not the truncated window: '
       f'{_call.group(1) if _call else "call site not found"}')

# ── 17. both renditions survive the capture, as one row plus a mirror ───────
# The field capture always sees the 360p and the 720p together. Only the
# 720p was handed over, and on these ~450 MiB non-faststart files it
# truncates: "https: Stream ends prematurely at 468433764, should be
# 470257707" then "moov atom not found". The 360p is the fallback.
import ast as _ast17
_int17 = open('familypornhd_integration.py', encoding='utf-8').read()
_fn17 = next(n for n in _ast17.walk(_ast17.parse(_int17))
             if isinstance(n, _ast17.FunctionDef)
             and n.name == '_familypornhd_capture_links')
_g17 = {}
exec(compile(_ast17.Module(body=[_fn17], type_ignores=[]), '<int>', 'exec'), _g17)
_links17 = _g17['_familypornhd_capture_links']

P720 = 'https://mediaboxplayer.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_720p.mp4?md5=Ka5CZaq6QDPBw0a1QZh-OA&expires=1789436138'
P360 = 'https://mediaboxplayer.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_360p.mp4?md5=oqJtBdViU9hzPGUnTVQAcg&expires=1789436138'

report(_links17(P720, {'alternate_urls': [P360]}) == [P720, P360],
       f'the 360p rides along behind the 720p: '
       f'{[u.rsplit("/", 1)[-1][:30] for u in _links17(P720, {"alternate_urls": [P360]})]}')
report(_links17(P720, {'alternate_urls': [P720, P360, P360]}) == [P720, P360],
       'the primary and a repeated mirror are each kept once')
report(_links17(P720, {}) == [P720] and _links17(P720, None) == [P720]
       and _links17(P720, {'alternate_urls': []}) == [P720],
       'no alternates, no payload and an empty list all give just the primary')
report(_links17('', {'alternate_urls': [P360]}) == [P360],
       'a missing primary falls back to the first alternate instead of an empty row')

_main17 = open('main.py', encoding='utf-8').read()
report("'alternate_urls': [c for c in _non_ad[1:] if c != best]" in _main17,
       'the resolver now returns the remaining renditions instead of dropping them')
report(_int17.count('_familypornhd_capture_links(') == 3,
       'both handoff sites build their links through the helper')

# Inserting the helper next to a decorated def once put it BETWEEN the
# @pyqtSlot decorator and its function, which silently stole the decorator
# and left the signal handler unregistered. Guard the decorators explicitly.
_decos17 = {n.name: [ast.unparse(d) for d in n.decorator_list]
            for n in _ast17.walk(_ast17.parse(_int17))
            if isinstance(n, _ast17.FunctionDef)}
report(_decos17.get('_vp_on_familypornhd_capture_ready') == ['pyqtSlot(str, object, bool)'],
       f'the capture-ready handler keeps its pyqtSlot decorator: '
       f'{_decos17.get("_vp_on_familypornhd_capture_ready")}')
report(_decos17.get('_vp_on_familypornhd_capture_failed') == ['pyqtSlot(str, str)'],
       'and so does the capture-failed handler')
report(_decos17.get('_familypornhd_capture_links') == [],
       'the new helper is undecorated and defined exactly once')
report(sum(1 for n in _ast17.walk(_ast17.parse(_int17))
           if isinstance(n, _ast17.FunctionDef)
           and n.name == '_familypornhd_capture_links') == 1,
       'no duplicate definition of the helper')

# ── 18. the remote_control.php delivery form is the one that actually plays ─
# FetchV pulled this URL off the one-way-or-another page and it plays both in
# a browser and in the app. It lives on srv1.familypornhd.com and carries its
# token in the QUERY, so the old path-only test (which required /get_file/)
# would have discarded it. Its acctoken is base64 of
# "<md5>|<expiry>|0|<host>|0|<client IP>|<md5>" — signed against the expiry,
# the host and the requesting IP, which is why a captured get_file URL dies
# after a few minutes and why we cannot mint a replacement.
RC = ('https://srv1.familypornhd.com/remote_control.php'
      '?file=zThQYIHld6o29k2VXJ-jNnQUy8AEzJZH6dS3JO_TE3TzSUk43BNaazaGuZ9_SmWo_kd5Kb8G'
      'kzsMrF7jdfrrNs7BxVIHYtdpCb63i3tdVZ65-eUKlbXmRixe2ctAJnxtKAgXZuC21xTIEpC27_S6RWeaeG'
      '72YrP1CGS8wujiJ9N_RTv7AG15yT3dy2SQUS04Zkdt1j3ACw.mp4'
      '&acctoken=Yzg4Yjg3MmZlY2RjZmFmMTg0OWMwYWNkZDMzY2MyN2VmYTJjNTBmYmE0YmJmMDFmNDYyYzdiZDNh')
GF = ('https://dev.familypornhd.com/get_file/0/_f6WasOxVWKNQ67C0Bwyt0kE8bJk35UiTyBdLvsoqpyipXDppw'
      '8mtAp0HxDYrRUWE9WilMPa37QuXLpkyRfqLmdv2aQBa6hhvBXwC1mXIE9--A.mp4/'
      '?v-acctoken=OTA0fDF8MTN8&embed=true&rnd=1789349719311')

report(cap._is_familypornhd_direct_video_url(RC) is True,
       'the working remote_control.php URL is recognised as a direct video')
report(cap._is_familypornhd_direct_video_url(GF) is True,
       'and get_file still is')
report(cap._is_familypornhd_direct_video_url(
    'https://watchstreamhd.com/cdn/hls/871885b5ce8f4eb8663ee09a36fa44a7/master.txt') is False,
    'the player master is still not a direct video')
report(cap._is_familypornhd_direct_video_url(
    'https://familypornhd.com/one-way-or-another-2/') is False,
    'nor is the article page')
report(cap._is_familypornhd_direct_video_url(
    'https://example.org/remote_control.php?file=a.mp4&acctoken=b') is False,
    'the same path on a non-family host is still rejected')
report(cap._is_familypornhd_direct_video_url(
    'https://srv1.familypornhd.com/remote_control.php?file=a.mp4') is False,
    'remote_control.php without an acctoken is not a delivery URL')

import base64 as _b64_18
_tok18 = ('Yzg4Yjg3MmZlY2RjZmFmMTg0OWMwYWNkZDMzY2MyN2VmYTJjNTBmYmE0YmJmMDFmNDYyYzdiZDNh'
          'NGZkYzIwZnwxNzg5MzU5OTczfDB8ZGV2LmZhbWlseXBvcm5oZC5jb218MHwxOTcuMTQ2LjU0LjIz'
          'NnxmYTkzMmY4YWIxMDJhYWE3YTMxZjc1NDZlMTJhNTI1OQ')
_dec18 = _b64_18.b64decode(_tok18 + '=' * (-len(_tok18) % 4)).decode()
report(_dec18.split('|')[1] == '1789359973' and _dec18.split('|')[3] == 'dev.familypornhd.com'
       and _dec18.split('|')[5] == '197.146.54.236',
       f'the token is signed against expiry, host and client IP: {_dec18}')

_main18 = open('main.py', encoding='utf-8').read()
report('has no remote_control.php recovery' in _main18,
       'the comment now says plainly that no recovery step exists')

# ── 19. the KVS embed page carries every signed rendition in plain HTML ──────
# site.txt is the verbatim source of https://dev.familypornhd.com/embed/42
# ("One Way Or Another") that the user saved from their browser.  The app used
# to fetch only the watch page, which contains nothing but an <iframe>, and
# then spend 90 seconds driving a player to capture what was already sitting in
# this HTML.  These assertions run the shipped extractors over the real page.
import familypornhd_grab as fg19

_SITE19 = 'site.txt'
report(os.path.isfile(_SITE19),
       f'{_SITE19} (the saved KVS embed page) is present as the fixture')
_html19 = open(_SITE19, encoding='utf-8').read() if os.path.isfile(_SITE19) else ''
_EMBED19 = 'https://dev.familypornhd.com/embed/42'

_streams19 = fg19._extract_kvs_player_streams(_html19, _EMBED19)
report(len(_streams19) == 3,
       f'all three renditions are read out of the player config, got {len(_streams19)}')
report(bool(_streams19) and _streams19[0].startswith(
    'https://dev.familypornhd.com/get_file/0/zwQJ0A71'),
       f'best quality first: {_streams19[0][:96] if _streams19 else "nothing"}')
report(all('/get_file/' in s for s in _streams19),
       'every rendition is a signed get_file URL')
report(not any('E2ZW1slF' in s for s in _streams19),
       'the event_reporting2 stats beacon is not mistaken for a rendition')
report(not any('preview' in s.lower() for s in _streams19),
       'no preview/poster asset is offered as a rendition')

# the quality labels drive the ordering, not the key order in the page
report(fg19._kvs_quality_rank('video_alt_url2', '1080p')
       > fg19._kvs_quality_rank('video_alt_url', '720p')
       > fg19._kvs_quality_rank('video_url', '480p'),
       'labelled renditions rank 1080p > 720p > 480p')
report(fg19._kvs_quality_rank('video_alt_url2', '')
       > fg19._kvs_quality_rank('video_alt_url', '')
       > fg19._kvs_quality_rank('video_url', ''),
       'unlabelled renditions still rank by the key suffix KVS assigns')

# the watch page only points at the embed page; finding it is the whole trick
report(fg19._find_kvs_embed_url(
    '<iframe src="https://dev.familypornhd.com/embed/42" allowfullscreen></iframe>',
    'https://familypornhd.com/one-way-or-another/') == 'https://dev.familypornhd.com/embed/42',
    'the absolute embed URL is lifted out of the watch page iframe')
report(fg19._find_kvs_embed_url(
    '<div data-src="/embed/77"></div>', 'https://familypornhd.com/x/')
    == 'https://familypornhd.com/embed/77',
    'a relative embed path resolves against the watch page')
report(fg19._find_kvs_embed_url('<p>no player here</p>', 'https://x/') == '',
    'a page with no player yields no embed URL rather than a guess')
report(fg19._extract_kvs_player_streams('<p>no player here</p>', 'https://x/') == [],
    'a page with no player config yields no streams')

# the v-acctoken is server-minted and carries no expiry field, unlike the
# remote_control.php acctoken -- which is why a freshly read page is usable
_tok19 = re.search(r'v-acctoken=([A-Za-z0-9]+)', _streams19[0]) if _streams19 else None
_dec19 = ''
if _tok19:
    _raw19 = _tok19.group(1)
    for _cut19 in range(len(_raw19), 0, -1):
        try:
            _try19 = base64.b64decode(_raw19[:_cut19] + '=' * (-_cut19 % 4)).decode('utf-8')
        except Exception:
            continue
        if _try19.isprintable():
            _dec19 = _try19
            break
_parts19 = _dec19.split('|')
report(len(_parts19) == 4 and len(_parts19[3]) == 32,
       f'v-acctoken decodes to "<n>|<embed>|<n>|<md5>": {_dec19!r}')
report(not any(p.isdigit() and len(p) == 10 for p in _parts19),
       'no unix expiry inside the token, so a freshly read page is immediately usable')

# the URLs the static path now produces must survive the family URL filter
report(cap._is_familypornhd_direct_video_url(_streams19[0]) is True if _streams19 else False,
       'the get_file URL the embed page yields is accepted as a direct video URL')

# the worker must prefer this over the browser capture, and the aged-URL
# refresh must re-read the embed page instead of the (video-less) article page
_int19 = open('familypornhd_integration.py', encoding='utf-8').read()
report('static_result.get("kvs_embed")' in _int19
       and '"kvs_embed_static" if static_result.get("kvs_embed")' in _int19
       and 'if static_links and _static_kind:' in _int19,
       'the worker hands KVS renditions straight to the playlist, skipping the browser')
_grab19 = open('familypornhd_grab.py', encoding='utf-8').read()
report('"kvs_embed": kvs_embed' in _grab19,
       'fetch_and_extract reports whether the links came from a KVS player config')
_main19 = open('main.py', encoding='utf-8').read()
report('from familypornhd_grab import fetch_and_extract as _kvs_fetch' in _main19
       and "'resolver_provider': 'familypornhd_kvs_embed'" in _main19,
       'the aged-URL refresh re-reads the KVS embed page instead of the article page')
report('refreshing aged captured URL from article page' not in _main19,
       'the log line no longer claims the refresh comes from the article page')
report('_family_fresh = self._resolve_stream_from_html(_family_origin_page)' in _main19,
       'the generic HTML pass is kept as a fallback, not deleted')

# ── 20. stop the capture once the decrypted renditions are in the pipe ───────
# Every FamilyPornHD capture in the user's log printed
# "killed capture subprocess (deadline reached)" BEFORE selecting a stream:
# the two /cdn/down/ MP4s arrived within a second of master.txt, then the
# capture idled for the rest of the ~135s window because the blob:/MSE player
# never raises VERIFIED_MEDIA. These are the verbatim lines from that log.
_is_family_cdn_media_line = lift('VideoPlayer', '_is_family_cdn_media_line')

_CDN_HITS20 = [
    'MEDIA_URL::https://video-streams.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_360p.mp4?md5=3BBnQR6V0PPi-taiZfFPNA&expires=1789442476',
    'MEDIA_URL::https://video-streams.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/files/caught-red-handed_eng_720p.mp4?md5=DjwzKnlNZWSzlnxcVCaAnQ&expires=1789442476',
    'MEDIA_URL::https://bestvideostream.com/cdn/down/43c313477ed1dfebd68cb9d0d26eb8c8/files/finally_und_720p.mp4?md5=PAwqVX7oJEPxeOLeD1awiA&expires=1789442616',
]
report(all(_is_family_cdn_media_line(line) is True for line in _CDN_HITS20),
       f'all {len(_CDN_HITS20)} real /cdn/down/ renditions arm the early exit')

# the pre-roll advert is also an MP4 on a CDN-looking path -- it must not arm
# the exit, or the capture would bail before the real video appeared
report(_is_family_cdn_media_line(
    'MEDIA_URL::https://playhubconnect.com/bn/alternative/709/557/01d/'
    '70955701d2cb9942742e85402b1cc08a9aba7c9b-alt.mp4') is False,
    'the playhubconnect pre-roll advert does not arm the early exit')

_CDN_MISSES20 = [
    # the encrypted master playlist itself: a signal, never a playable file
    'MEDIA_URL::https://watchstreamhd.com/cdn/hls/871885b5ce8f4eb8663ee09a36fa44a7/master.txt',
    'MEDIA_URL::https://www.googletagmanager.com/gtag/js?id=G-C8V6DNVNQK&cx=c&gtm=4e6992',
    'MEDIA_URL::https://familypornhd.com/wp-includes/js/jquery/jquery.min.js?ver=3.7.1',
    'MEDIA_URL::https://watchstreamhd.com/player/assets/js/cryptojs-aes.min.js',
    'MEDIA_URL::https://excavatenearbywand.com/aas/r45d/vki/2089950/402c05c4.js',
    'MEDIA_URL::https://goodimpressioncrboost.com/v1/track/impression?data=eyJhbGciOi',
    'MEDIA_URL::https://ssl.p.jwpcdn.com/player/v/8.34.3/jwplayer.js',
    # the KVS path has its own resolver; it must not trip this rung
    'MEDIA_URL::https://dev.familypornhd.com/get_file/0/zwQJ0A71.mp4/?v-acctoken=abc',
    'PAGE_TITLE::Myra Moans - Straight & Narrow - Family Therapy',
    'BROWSER_COOKIES::_gid=GA1.2.327975906.1789336876',
    '',
]
report(all(_is_family_cdn_media_line(line) is False for line in _CDN_MISSES20),
       f'none of the {len(_CDN_MISSES20)} other captured lines arm it')

# the predicate is only useful if the loop actually consults it
_main20 = open('main.py', encoding='utf-8').read()
report('self._is_family_cdn_media_line(line)' in _main20
       and "_pw_family_cdn_at = None" in _main20
       and "'family CDN renditions captured, closing browser'" in _main20,
       'the capture loop records the timestamp and breaks on it')
report(_main20.count('_pw_family_cdn_at') == 5,
       f'timestamp initialised, set, and tested exactly once each '
       f'({_main20.count("_pw_family_cdn_at")} references)')

# ── 21. FirePlayer: read the getVideo response instead of the browser ────────
# The watchstreamhd player refuses to start while DevTools is open, so the
# getVideo response cannot be inspected by hand. The endpoint is fixed and
# readable out of /player/assets/scripts.php, so the app POSTs to it directly
# and prints the payload -- the console log is the only channel that works.
_EMBED21 = 'https://watchstreamhd.com/video/c44e503833b64e9f27197a484f4257c0'
_HTML21 = f'<iframe src="{_EMBED21}" allowfullscreen></iframe>'

_m21 = fg19._WATCHSTREAM_EMBED_RE.search(_HTML21)
report(bool(_m21) and _m21.group(2) == 'c44e503833b64e9f27197a484f4257c0',
       f'the FirePlayer video id is lifted out of the watch page iframe: '
       f'{_m21.group(2) if _m21 else "no match"}')
report(fg19._WATCHSTREAM_EMBED_RE.search(
    'https://playhubconnect.com/bn/alternative/709/557/01d/70955701d2cb-alt.mp4') is None
    and fg19._WATCHSTREAM_EMBED_RE.search('<p>no player</p>') is None,
    'the advert and a player-less page are not mistaken for a FirePlayer embed')

_payload21 = {
    'hls': True,
    'videoSource': 'https://watchstreamhd.com/cdn/hls/871885b5ce8f4eb8663ee09a36fa44a7/master.txt',
    'videoSources': [
        {'file': 'https://video-streams.com/cdn/down/x/files/straight-narrow_und_360p.mp4?md5=a&expires=1',
         'label': '360p'},
        {'file': 'https://video-streams.com/cdn/down/x/files/straight-narrow_und_720p.mp4?md5=b&expires=1',
         'label': '720p'},
    ],
    'downloadLinks': [
        {'label': '1080p', 'size': '0',
         'url': 'https://video-streams.com/cdn/down/x/files/straight-narrow_und_1080p.mp4?md5=c&expires=1'},
    ],
}
_picked21 = fg19._fireplayer_pick_best(_payload21)
report(len(_picked21) == 3 and '1080p' in _picked21[0],
       f'renditions from every list, best first ({len(_picked21)}): '
       f'{[u.split("/files/")[1][:16] for u in _picked21]}')
report(not any('master.txt' in u for u in _picked21),
       'the encrypted master.txt is never offered as a playable rendition')
report(fg19._fireplayer_pick_best({'videoSources': [{'file': 'blob:https://watchstreamhd.com/x'}]}) == [],
    'a blob: MediaSource URL is rejected -- mpv cannot open one')
report(fg19._fireplayer_pick_best({}) == [] and fg19._fireplayer_pick_best(None) == [],
    'an empty or missing payload yields nothing rather than raising')

_real21 = fg19._http_post
try:
    fg19._http_post = lambda url, data, referer="", timeout=20: (url, json.dumps(_payload21))
    _got21, _abs21 = fg19._extract_fireplayer_streams(_HTML21, 'https://familypornhd.com/straight-narrow/')
    report(len(_got21) == 3 and '1080p' in _got21[0],
           f'the resolver POSTs and returns {len(_got21)} playable URL(s), 1080p first')

    fg19._http_post = lambda url, data, referer="", timeout=20: (url, 'Video not found.')
    report(fg19._extract_fireplayer_streams(_HTML21, 'https://x/') == ([], False),
           'a plain-text refusal is reported and yields no links')

    def _boom21(url, data, referer="", timeout=20):
        raise RuntimeError('network down')
    fg19._http_post = _boom21
    report(fg19._extract_fireplayer_streams(_HTML21, 'https://x/') == ([], False),
           'a transport failure degrades to the browser capture instead of raising')
finally:
    fg19._http_post = _real21

report(fg19._extract_fireplayer_streams('<p>no player</p>', 'https://x/') == ([], False),
       'a page with no FirePlayer embed is left alone')

_grab21 = open('familypornhd_grab.py', encoding='utf-8').read()
report('FirePlayer getVideo raw:' in _grab21,
       'the raw payload is printed, since DevTools is blocked on this site')
report('links, _fp_no_downloads = _extract_fireplayer_streams(html, page_url)' in _grab21,
       'fetch_and_extract falls through to FirePlayer when the KVS player finds nothing')

# ── 22. FirePlayer: the cleartext Download menu, and never a ciphertext blob ─
# /video/<id> answers a plain GET with the rendered player page whose Download
# menu holds the direct MP4s unencrypted. The POST response encrypts them, and
# that ciphertext was being handed to the playlist as if it were a stream.
_PAGE22 = (
    '<a href="https://bestvideostream.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/'
    'files/caught-red-handed_eng_360p.mp4?md5=NHwGqH8mH1oafdfKU53XBA&amp;expires=1789447486">'
    '[ENG] 360p (84.97 MB)</a>'
    '<a href="https://bestvideostream.com/cdn/down/76603cb0d5efcec0eda71ebb2160ee77/'
    'files/caught-red-handed_eng_720p.mp4?md5=2O9Pn8glglM4gW9q_Vhqjw&amp;expires=1789447486">'
    '[ENG] 720p (448.47 MB)</a>'
    '<script src="https://ssl.p.jwpcdn.com/player/v/8.34.3/jwplayer.js"></script>'
    '<img src="https://mediaboxplayer.com/p/cover.jpg">'
)
_CT22 = ('{"ct":"Z5eG/iA5lUAG1Bh1yrjrbEKQQ7Grj2tFhJ6qXDKzdf2cZPUy1Lo5k3k1vCWF6atPFDHH1yh==",'
         '"iv":"5cc6efdd6e15495c6d231845e3f937c1","s":"fd848abc7cb19aad"}')

report(fg19._looks_like_media_url(_CT22) is False,
       'a CryptoJS ciphertext blob is not accepted as a media URL')
report(fg19._looks_like_media_url('blob:https://watchstreamhd.com/74910ba6') is False
       and fg19._looks_like_media_url('https://x.com/a.js') is False
       and fg19._looks_like_media_url('https://x.com/cover.jpg') is False
       and fg19._looks_like_media_url('') is False,
       'blob:, scripts, images and empties are all rejected')
report(fg19._looks_like_media_url(
    'https://video-streams.com/cdn/down/x/files/v_eng_720p.mp4?md5=a&expires=1') is True,
    'a signed /cdn/down/ MP4 is accepted')

_real_get22, _real_post22 = fg19._http_get, fg19._http_post
try:
    fg19._http_get = lambda url, referer="", timeout=20: (url, _PAGE22)
    _menu22 = fg19._fireplayer_download_links(
        'https://watchstreamhd.com/video/b20bb95ab626d93fd976af958fbc61ba')
    report(len(_menu22) == 2 and '_720p.mp4' in _menu22[0],
           f'the Download menu is scraped, 720p first ({len(_menu22)} link(s))')
    report(all('&amp;' not in u for u in _menu22),
           'HTML entities in the query string are decoded, not passed through')
    report(not any('jwplayer' in u or 'cover.jpg' in u for u in _menu22),
           'the player script and the cover image are not mistaken for renditions')

    _enc22 = {'hls': True,
              'videoSource': 'https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/master.txt',
              'downloadLinks': [{'language': 'eng', 'label': '720p', 'file': _CT22,
                                 'size': '448.47 MB'}]}
    report(fg19._fireplayer_pick_best(_enc22) == [],
           'the encrypted downloadLinks from the real payload yield nothing')

    fg19._http_post = lambda url, data, referer="", timeout=20: (url, json.dumps(_enc22))
    _res22, _abs22 = fg19._extract_fireplayer_streams(_HTML21, 'https://familypornhd.com/caught-red-handed/')
    report(len(_res22) == 2 and all('/cdn/down/' in u for u in _res22),
           f'the cleartext menu wins over the encrypted POST ({len(_res22)} link(s))')

    fg19._http_get = lambda url, referer="", timeout=20: (url, '<html>no downloads</html>')
    report(fg19._extract_fireplayer_streams(_HTML21, 'https://x/') == ([], False),
           'with no menu and only ciphertext, the resolver yields nothing at all')

    def _boom22(url, referer="", timeout=20):
        raise RuntimeError('offline')
    fg19._http_get = _boom22
    report(fg19._extract_fireplayer_streams(_HTML21, 'https://x/') == ([], False),
           'a failed page fetch falls through instead of raising')
finally:
    fg19._http_get, fg19._http_post = _real_get22, _real_post22

_int22 = open('familypornhd_integration.py', encoding='utf-8').read()
report('if static_links and _static_kind:' in _int22
       and '"fireplayer_static" if static_result.get("fireplayer")' in _int22,
       'the worker skips the browser capture for FirePlayer links too, not just KVS')
report('pyqtSlot(str, object, bool)' in _int22,
       'the capture handler is still a registered pyqtSlot')
_grab22 = open('familypornhd_grab.py', encoding='utf-8').read()
report(_grab22.count('"fireplayer"') >= 2,
       'fetch_and_extract advertises the fireplayer flag on every return path')

# A second def of an existing helper silently shadows it, and the loser is the
# one every other caller depends on: a redefined _looks_like_media_url quietly
# stopped accepting the KVS ".mp4/?v-acctoken=…" shape and zeroed the whole KVS
# path. Both modules are checked for redefinition.
for _path22 in ('familypornhd_grab.py', 'familypornhd_integration.py'):
    _names22 = [n.name for n in ast.parse(
        open(_path22, encoding='utf-8').read()).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    _dups22 = sorted({n for n in _names22 if _names22.count(n) > 1})
    report(not _dups22, f'{_path22} defines no helper twice: {_dups22 or "clean"}')

report(fg19._looks_like_media_url(
    'https://dev.familypornhd.com/get_file/0/zwQJ0A71x.mp4/?v-acctoken=abc&embed=true')
    is True,
    'the signed KVS ".mp4/?v-acctoken=…" shape is still accepted as media')
report(fg19._is_absolute_http_url('blob:https://watchstreamhd.com/x') is False
       and fg19._is_absolute_http_url('{"ct":"x"}') is False
       and fg19._is_absolute_http_url('https://x.com/a.mp4') is True,
       'a blob URL and a ciphertext blob fail the absolute-URL rule')
_site22 = open('site.txt', encoding='utf-8').read() if os.path.isfile('site.txt') else ''
report(len(fg19._extract_kvs_player_streams(_site22, _EMBED19)) == 3,
       f'the KVS extractor still yields all 3 renditions from site.txt '
       f'(got {len(fg19._extract_kvs_player_streams(_site22, _EMBED19))})')

# ── 23. FirePlayer: the signed HLS master is the fallback, not the primary ──
# The Download menu is built by scripts.php from jData.downloadLinks at
# runtime, so it is absent from the raw HTML the app fetches -- scraping it
# finds nothing. And for hls videos downloadLinks[].file is AES ciphertext,
# which cannot be decrypted without a key we do not have. Every response does
# carry a signed HLS master in cleartext, including the videos whose
# downloadLinks is empty, and mpv plays that directly.
_EMPTY_DL23 = {
    'hls': True,
    'videoSource': 'https://watchstreamhd.com/cdn/hls/a305902aeacf302c3a44accafc0894cd/master.txt',
    'securedLink': ('https://watchstreamhd.com/cdn/hls/a305902aeacf302c3a44accafc0894cd/'
                    'master.m3u8?md5=j22qa6ShPYGlnPQFbIy0xg&expires=1789370878'),
    'downloadLinks': [], 'attachmentLinks': [],
}
_ENC_DL23 = {
    'hls': True,
    'videoSource': 'https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/master.txt',
    'securedLink': ('https://watchstreamhd.com/cdn/hls/76603cb0d5efcec0eda71ebb2160ee77/'
                    'master.m3u8?md5=nrpII79Bipk4zc2OwUB8JA&expires=1789370667'),
    'downloadLinks': [{'language': 'eng', 'label': '720p', 'size': '448.47 MB',
                       'file': _CT22}],
}
_VID23 = 'f47330643ae134ca204bf6b2481fec47'
_HTML23 = (f'<html><title>Stepbro</title>'
           f'<iframe src="https://watchstreamhd.com/video/{_VID23}"></iframe></html>')

report(fg19._is_hls_master_url('https://x/cdn/hls/a/master.m3u8?md5=1&expires=2') is True
       and fg19._is_hls_master_url('https://x/a.mp4?md5=1') is False
       and fg19._is_hls_master_url('') is False,
       'an HLS master is told apart from a progressive file by its path, not its query')

# raw HTML carries no JS-built menu, which is the case that defeated the scrape
_raw23_get, _raw23_post = fg19._http_get, fg19._http_post
try:
    fg19._http_get = lambda url, referer="", timeout=20: (
        url, _HTML23 if 'familypornhd.com/' in url else "<html><div id='downloads'></div></html>")

    def _stub_post23(payload):
        def _post(url, data, referer="", timeout=20):
            return (url, json.dumps(payload))
        return _post

    for _label23, _payload23 in (('empty downloadLinks', _EMPTY_DL23),
                                 ('encrypted downloadLinks', _ENC_DL23)):
        fg19._http_post = _stub_post23(_payload23)
        _got23, _abs23 = fg19._extract_fireplayer_streams(_HTML23, 'https://familypornhd.com/x/')
        report(_got23 == [_payload23['securedLink']],
               f'with {_label23} the signed HLS master is returned instead of nothing')

    fg19._http_post = _stub_post23(_EMPTY_DL23)
    _res23 = fg19.fetch_and_extract('https://familypornhd.com/i-can-get-naked-when-i-want-stepbro-2/')
    report(_res23['fireplayer'] is True and _res23['fireplayer_hls'] is False,
           'an HLS master reached with no download variants is played straight away')
    report(_res23['headers'].get('Referer')
           == f'https://watchstreamhd.com/video/{_VID23}',
           'the Referer is the player page the master was issued for, not the article')
    report(not any('master.txt' in u for u in _res23['links'])
           and not any(u.startswith('{') for u in _res23['links']),
           'neither the encrypted master.txt nor a ciphertext blob is ever offered')
finally:
    fg19._http_get, fg19._http_post = _raw23_get, _raw23_post

# Superseded by section 25: the worker now short-circuits on the master too,
# because the captured 720p MP4 proved slower, not better. What must survive is
# that both kinds are still gated on the static result being trustworthy.
report('"fireplayer_static" if static_result.get("fireplayer")' in _int22,
       'the worker still short-circuits on KVS and FirePlayer renditions')
report('result = static_result' in _int22,
       'the static links are still used when the browser capture comes back empty')

# ── 24. do not idle through the capture deadline when there is nothing to find
# Three HLS-only articles in one field log each ran the capture to its full
# deadline before falling back to a master the resolver had within a second:
# age_ms 91274, 50778, 88906. Their getVideo responses all had
# "downloadLinks":[], so no /cdn/down/ MP4 exists for the capture to find.
# When download variants ARE listed the capture ends early on that MP4, which
# is the better thing to play -- so only the empty case short-circuits.
_SEC24 = ('https://watchstreamhd.com/cdn/hls/fc6bf663d70156aa8d22873e18ad802a/'
          'master.m3u8?md5=M4hBVEF9Tb1yPQjrS0I4mA&expires=1789413751')
_VID24 = '2ac2406e835bd49c70469acae337d292'
_HTML24 = (f'<html><title>Apple Pie</title>'
           f'<iframe src="https://watchstreamhd.com/video/{_VID24}"></iframe></html>')
_NO_DL24 = {'hls': True, 'videoSource': 'https://watchstreamhd.com/cdn/hls/x/master.txt',
            'securedLink': _SEC24, 'downloadLinks': [], 'attachmentLinks': []}

_get24, _post24 = fg19._http_get, fg19._http_post
try:
    fg19._http_get = lambda url, referer="", timeout=20: (
        url, _HTML24 if 'familypornhd.com/' in url else "<html><div id='downloads'></div></html>")

    def _stub24(payload):
        def _post(url, data, referer="", timeout=20):
            return (url, json.dumps(payload))
        return _post

    fg19._http_post = _stub24(_NO_DL24)
    _links24, _absent24 = fg19._extract_fireplayer_streams(_HTML24, 'https://familypornhd.com/x/')
    report(_links24 == [_SEC24] and _absent24 is True,
           'an empty downloadLinks marks the master as all there is')

    _r24 = fg19.fetch_and_extract('https://familypornhd.com/my-best-friends-stepmom-gave-me-warm-apple-pie/')
    report(_r24['fireplayer'] is True and _r24['fireplayer_hls'] is False,
           'so the master is used at once instead of waiting out the capture deadline')

    _WITH_DL24 = dict(_NO_DL24, downloadLinks=[{'language': 'eng', 'label': '720p',
                                               'file': _CT22, 'size': '448.47 MB'}])
    fg19._http_post = _stub24(_WITH_DL24)
    _links24b, _absent24b = fg19._extract_fireplayer_streams(_HTML24, 'https://familypornhd.com/x/')
    report(_links24b == [_SEC24] and _absent24b is False,
           'encrypted download variants are still reported as present')

    _r24b = fg19.fetch_and_extract('https://familypornhd.com/caught-red-handed/')
    report(_r24b['fireplayer'] is False and _r24b['fireplayer_hls'] is True,
           'and there the capture keeps first shot, since it ends early on the MP4')
finally:
    fg19._http_get, fg19._http_post = _get24, _post24

# ── 25. the signed master is played at once, not after a browser capture ────
# In a 24-link field run the three slowest links were the three
# browser-captured 720p MP4s (448.47 MB, 436.65 MB). The CDN closed those
# transfers short, their moov atom is at the end of the file, and mpv had to
# re-download the whole thing three or four times. The master from the same
# getVideo response streamed first time on all twelve rows that used it, and
# needs no capture at all.
_int25 = open('familypornhd_integration.py', encoding='utf-8').read()
report('"fireplayer_hls_static" if static_result.get("fireplayer_hls")' in _int25,
       'the worker short-circuits on a FirePlayer HLS master as well')
report(_int25.index('"fireplayer_hls_static"') < _int25.index('if static_links and _static_kind:'),
       'and that rung is evaluated before the short-circuit decision, not after')
report('pyqtSlot(str, object, bool)' in _int25,
       'the capture handler is still a registered pyqtSlot')
_names25 = [n.name for n in ast.parse(_int25).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
report(not {n for n in _names25 if _names25.count(n) > 1},
       'familypornhd_integration.py still defines no helper twice')

# Both response shapes must now reach a short-circuiting flag, so no FirePlayer
# article is left waiting on a capture.
_get25, _post25 = fg19._http_get, fg19._http_post
try:
    fg19._http_get = lambda url, referer="", timeout=20: (
        url, _HTML24 if 'familypornhd.com/' in url else "<html><div id='downloads'></div></html>")

    def _stub25(payload):
        def _post(url, data, referer="", timeout=20):
            return (url, json.dumps(payload))
        return _post

    for _label25, _payload25 in (('empty downloadLinks', _NO_DL24),
                                 ('encrypted downloadLinks',
                                  dict(_NO_DL24, downloadLinks=[
                                      {'language': 'eng', 'label': '720p',
                                       'file': _CT22, 'size': '448.47 MB'}]))):
        fg19._http_post = _stub25(_payload25)
        _r25 = fg19.fetch_and_extract('https://familypornhd.com/caught-red-handed/')
        report(bool(_r25['fireplayer'] or _r25['fireplayer_hls']) and _r25['links'] == [_SEC24],
               f'{_label25}: the master is returned with a short-circuiting flag')
finally:
    fg19._http_get, fg19._http_post = _get25, _post25

# ── 26. turbo.cr: fold the /d/ download path onto the /v/ watch page ─────────
# turbo.cr serves one clip under two paths: /d/<id> is a bare download page
# and /v/<id> is the watch page that embeds the player. Links arrive in the
# /d/ form, which has no player in it. This lifts the real shipped method out
# of main.py by ast and runs it, so the assertions below exercise the code
# that ships rather than a copy of it.
_T26_WANT = ['_sanitize_url', '_is_remote_url', '_unwrap_base64_hostname_media_url',
             '_is_dood_host', '_canonicalize_remote_source_url_uncached']
_t26_tree = ast.parse(open('main.py', encoding='utf-8').read())
_t26_cls = next(n for n in ast.walk(_t26_tree)
                if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_t26_m = {x.name: x for x in _t26_cls.body
          if isinstance(x, ast.FunctionDef) and x.name in _T26_WANT}
report(sorted(_t26_m) == sorted(_T26_WANT),
       f'all five canonicalisation helpers were lifted from main.py (missing: '
       f'{sorted(set(_T26_WANT) - set(_t26_m))})')
_t26_stub = ast.ClassDef(name='_T26Stub', bases=[], keywords=[],
                         body=[_t26_m[w] for w in _T26_WANT], decorator_list=[])
_t26_mod = ast.Module(body=[_t26_stub], type_ignores=[])
ast.fix_missing_locations(_t26_mod)
_t26_ns = {'re': re, 'base64': base64, 'urlparse': urlparse,
           'urlunparse': urlunparse, 'parse_qs': parse_qs}
exec(compile(_t26_mod, '<main.py>', 'exec'), _t26_ns)
_t26 = _t26_ns['_T26Stub']()

for _src26, _want26 in [
    ('https://turbo.cr/d/F2o26UhL9kk',  'https://turbo.cr/v/F2o26UhL9kk'),
    ('https://turbo.cr/d/Tzyr96uVlhC',  'https://turbo.cr/v/Tzyr96uVlhC'),
    ('https://turbo.cr/v/F2o26UhL9kk',  'https://turbo.cr/v/F2o26UhL9kk'),
    ('https://turbo.cr/embed/abc123',   'https://turbo.cr/embed/abc123'),
    ('https://beta.turbo.cr/d/abc123',  'https://beta.turbo.cr/v/abc123'),
    ('https://noturbo.creep/d/abc123',  'https://noturbo.creep/d/abc123'),
    ('https://turbo.cr/',               'https://turbo.cr/'),
    # Regression guard for the rule sitting immediately above the new one.
    ('https://dood.to/d/xyz789',        'https://dood.to/e/xyz789'),
]:
    _got26 = _t26._canonicalize_remote_source_url_uncached(_src26)
    report(_got26 == _want26, f'{_src26} -> {_want26}   (got {_got26})')

print()
print('FAILURES:', FAILS)
raise SystemExit(1 if FAILS else 0)
