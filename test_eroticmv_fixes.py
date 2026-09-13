"""Execute the REAL functions shipped in main.py against the reported cases.

Nothing here re-implements the logic: each function body is lifted verbatim
out of main.py by AST and exec'd, so a regression in main.py fails these.
Runs without PyQt, mpv or network access. The app never imports this file —
run it by hand to validate a copy of main.py.

Covers: eroticmv playlist titles, exact 3s arrow seeks, the VOD-proxy routing
that makes backward seeking work, and the Google search query cleanup.
"""
import ast
import os
import re
from html import unescape as html_unescape
from urllib.parse import urlparse, unquote, urljoin

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
    'QTimer': _FakeQTimer,
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

# ── 10. Captured-links panel must not claim a row the collapse deleted ───────
class AbsorbStub:
    _relabel_absorbed_links = lift('VideoPlayer', '_relabel_absorbed_links')

    def __init__(self, absorbed, key_raises=False):
        self._last_mirror_absorptions = absorbed
        self._key_raises = key_raises
        self.notes = []

    def _link_flow_key(self, url):
        if self._key_raises:
            raise RuntimeError('boom')
        return str(url or '').strip().lower()

    def _note_link(self, url, status='', detail=''):
        self.notes.append((url, status, detail))


PAGE = 'https://javgg.net/v/abc'          # the row that survives
MIRROR = 'https://javstreamhq.to/e/abc'   # folded in, row deleted
a = AbsorbStub({MIRROR: PAGE})
a._relabel_absorbed_links([MIRROR, PAGE])
report(a.notes == [(MIRROR, 'mirror',
                    'merged into an existing row — use its mirror menu')],
       f'absorbed link relabelled, surviving row left alone: {a.notes}')

b = AbsorbStub({})
b._relabel_absorbed_links([MIRROR])
report(b.notes == [], f'nothing absorbed -> nothing relabelled: {b.notes}')

c = AbsorbStub({MIRROR: PAGE}, key_raises=True)
c._relabel_absorbed_links([MIRROR])
report(c.notes == [(MIRROR, 'mirror',
                    'merged into an existing row — use its mirror menu')],
       f'a raising key function falls back and still matches: {c.notes}')

d = AbsorbStub({MIRROR: PAGE})
d._relabel_absorbed_links([])
report(d.notes == [], f'no added urls -> no notes: {d.notes}')

e = AbsorbStub({MIRROR: PAGE})
e._relabel_absorbed_links(['https://unrelated.example/x'])
report(e.notes == [], f'an unrelated link is untouched: {e.notes}')

print()
print('FAILURES:', FAILS)
raise SystemExit(1 if FAILS else 0)
