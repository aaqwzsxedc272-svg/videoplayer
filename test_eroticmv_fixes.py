"""Execute the REAL functions shipped in main.py against the reported cases.

Nothing here re-implements the logic: each function body is lifted verbatim
out of main.py by AST and exec'd, so a regression in main.py fails these.
Runs without PyQt, mpv or network access. The app never imports this file —
run it by hand to validate a copy of main.py.

Covers: eroticmv playlist titles, exact 3s arrow seeks, the VOD-proxy routing
that makes backward seeking work, and the Google search query cleanup.
"""
import ast
import tempfile
import subprocess
import shutil
import base64
import json
import os
import re
import threading
import time
from html import unescape as html_unescape
from urllib.parse import urlparse, unquote, urljoin, urlunparse, parse_qs

SRC = open('main.py', encoding='utf-8').read()
TREE = ast.parse(SRC)

# main.py code is lifted into fresh namespaces throughout this file. Any of it
# may now call a module-level helper, and a lift that cannot see one dies on
# NameError at the first call -- which reads like a product bug but is a
# harness bug. Provide them to every lifted namespace.
_LIFT_HELPERS = {}
try:
    from urllib.parse import urlsplit as _us_lift
    _LIFT_HELPERS['urlsplit'] = _us_lift
    for _lift_name in ('_ext_probe_path', '_wrap_menu_label'):
        _fn_lift = next((n for n in TREE.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == _lift_name), None)
        if _fn_lift is None:
            continue
        _lift_consts = [n for n in TREE.body
                        if isinstance(n, ast.Assign)
                        and getattr(n.targets[0], 'id', '').startswith(
                            '_MENU_LABEL_WRAP')]
        exec(compile(ast.Module(body=_lift_consts + [_fn_lift],
                                type_ignores=[]),
                     '<lift_helpers>', 'exec'), _LIFT_HELPERS)
    # The caption helpers are module-level too, and main.py code that scans a
    # page for captions now calls them.
    from urllib.parse import urlparse as _up_lift, urljoin as _uj_lift
    from html import unescape as _hu_lift
    _LIFT_HELPERS.setdefault('os', os)
    _LIFT_HELPERS.setdefault('re', re)
    _LIFT_HELPERS.setdefault('urlparse', _up_lift)
    _LIFT_HELPERS.setdefault('urljoin', _uj_lift)
    _LIFT_HELPERS.setdefault('html_unescape', _hu_lift)
    _lift_sub_fns = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('_caption_ext_of', '_lang_from_url',
                                    '_subtitle_endpoint_candidates',
                                    '_subtitle_tracks_from_payload',
                                    '_subtitle_page_fragments')]
    _lift_sub_consts = [n for n in TREE.body if isinstance(n, ast.Assign)
                        and getattr(n.targets[0], 'id', '').startswith(
                            ('_CAPTION_FILE_EXTS', '_SUB_'))]
    exec(compile(ast.Module(body=_lift_sub_consts + _lift_sub_fns,
                            type_ignores=[]),
                 '<lift_sub_helpers>', 'exec'), _LIFT_HELPERS)
except Exception as _e_lift:
    print('  WARN  lift helpers unavailable:', _e_lift)


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
    _SITE_PROMO_STEM_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_STEM_TOKENS')
    _SITE_PROMO_PATH_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
    _media_url_is_site_promo = lift(
        'VideoPlayer', '_media_url_is_site_promo')
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
    _SITE_PROMO_STEM_TOKENS = lift_attr('VideoPlayer', '_SITE_PROMO_STEM_TOKENS')
    _SITE_PROMO_PATH_TOKENS = lift_attr('VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
    _media_url_looks_like_preview = lift('VideoPlayer', '_media_url_looks_like_preview')
    _media_url_is_site_promo = lift('VideoPlayer', '_media_url_is_site_promo')
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
    _SITE_PROMO_STEM_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_STEM_TOKENS')
    _SITE_PROMO_PATH_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
    _media_url_is_site_promo = lift(
        'VideoPlayer', '_media_url_is_site_promo')


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
    _SITE_PROMO_STEM_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_STEM_TOKENS')
    _SITE_PROMO_PATH_TOKENS = lift_attr(
        'VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
    _media_url_is_site_promo = lift(
        'VideoPlayer', '_media_url_is_site_promo')
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
_g17.update(_LIFT_HELPERS)
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

# ── 27. turbo.cr resolves from the page HTML, no browser ─────────────────────
# A field capture opened a full browser window on this host purely to read one
# URL: /v/<id> embeds /embed/<id>, and that player requests a single signed
# turbocdn mp4. The resolver below tries two plain GETs and returns None when
# neither page carries the URL, so the capture still runs as a fallback.
_REAL27 = ("https://dl100.turbocdn.st/turbo/data/QsCF7fNEp6Lhp.mp4?exp=1789441508"
           "&token=60420cf4d38a0e4694beae70adbfb41fe1663852c34304cd978bbaf78529d087"
           "&fn=Ryder+Rey+%26+Vivienne+Vo")

_T27_WANT = ['_turbo_cr_media_candidates', '_resolve_turbo_cr_source']
_t27_tree = ast.parse(open('main.py', encoding='utf-8').read())
_t27_cls = next(n for n in ast.walk(_t27_tree)
                if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_t27_m = {x.name: x for x in _t27_cls.body
          if isinstance(x, ast.FunctionDef) and x.name in _T27_WANT}
report(sorted(_t27_m) == sorted(_T27_WANT),
       f'both turbo.cr methods were lifted from main.py (missing: '
       f'{sorted(set(_T27_WANT) - set(_t27_m))})')
_t27_stub = ast.ClassDef(name='_T27Stub', bases=[], keywords=[],
                         body=[_t27_m[w] for w in _T27_WANT], decorator_list=[])
_t27_mod = ast.Module(body=[_t27_stub], type_ignores=[])
ast.fix_missing_locations(_t27_mod)
_t27_ns = {'re': re, 'urlparse': urlparse}
exec(compile(_t27_mod, '<main.py>', 'exec'), _t27_ns)

# The extractor against the shapes the URL actually arrives in.
_t27 = _t27_ns['_T27Stub']()
_ROT27 = _REAL27.replace('dl100', 'dl3')
for _label27, _html27, _want27 in [
    # third field is (expected url, html): the dl prefix rotates per request,
    # so the rotated case must come back as the rotated host, untouched.
    ('as captured',        f'<script src="{_REAL27}"></script>', (_REAL27, 1)),
    ('entity-escaped',     _REAL27.replace('&', '&amp;'),        (_REAL27, 1)),
    ('js-escaped slashes', _REAL27.replace('/', chr(92) + '/'),  (_REAL27, 1)),
    ('rotated dl prefix',  _ROT27,                               (_ROT27,  1)),
    ('ad noise only',      '<script src="//bucklechemistdensity.com/on.js">'
                           '</script><script src="https://holahupa.com/tghr.js"></script>', ('', 0)),
    ('the embed tag itself', '<script src="https://turbo.cr/embed/QsCF7fNEp6Lhp"></script>', ('', 0)),
    ('empty page',         '',                                   ('', 0)),
]:
    _exp_url27, _want27 = _want27
    _got27 = _t27._turbo_cr_media_candidates(_html27)
    report(len(_got27) == _want27, f'extractor {_label27}: {_want27} url(s), got {len(_got27)}')
    if _want27 == 1 and _got27:
        report(_got27[0] == _exp_url27,
               f'extractor {_label27}: url survives byte-for-byte')

# The resolver's page walk, with the fetch and the probe stubbed.
class _T27Pages(_t27_ns['_T27Stub']):
    def __init__(self, pages, probe_ok=True, probe_url=None):
        self._pages, self._probe_ok = pages, probe_ok
        self.probed, self.fetched = [], []
    def _turbo_cr_page_html(self, url, source_url):
        self.fetched.append(url)
        return self._pages.get(url, '')
    def _probe_remote_media_candidate(self, target_url, referer=None, title=None):
        self.probed.append(target_url)
        if not self._probe_ok:
            return None
        return {'playback_url': target_url, 'headers': {}}

_W27 = 'https://turbo.cr/v/QsCF7fNEp6Lhp'
_E27 = 'https://turbo.cr/embed/QsCF7fNEp6Lhp'

_o27 = _T27Pages({_E27: f'src="{_REAL27}"'})
_r27 = _o27._resolve_turbo_cr_source(_W27)
report(_o27.fetched == [_W27, _E27], f'walks the watch page then the embed page (got {_o27.fetched})')
report(bool(_r27) and _r27['playback_url'] == _REAL27, 'returns the signed mp4 without a browser')
report(_r27 and _r27.get('resolver_provider') == 'turbo_cr_static', 'and labels the provider so the log shows which path won')

_o27b = _T27Pages({_W27: f'src="{_REAL27}"'})
report(bool(_o27b._resolve_turbo_cr_source(_W27)) and _o27b.fetched == [_W27],
       'stops at the watch page when it already carries the url')

_o27c = _T27Pages({})
report(_o27c._resolve_turbo_cr_source(_W27) is None,
       'returns None when neither page has it, so the browser capture still runs')

_o27d = _T27Pages({_E27: f'src="{_REAL27}"'}, probe_ok=False)
report(_o27d._resolve_turbo_cr_source(_W27) is None,
       'and returns None when the probe rejects the url')

_o27e = _T27Pages({_E27: f'src="{_REAL27}"'})
report(_o27e._resolve_turbo_cr_source('https://turbo.cr/') is None,
       'a turbo.cr url with no video id resolves to nothing')

# Two candidates: the one for this video must win over an unrelated one.
_OTHER27 = _REAL27.replace('QsCF7fNEp6Lhp', 'Zr2uqa61FxPgp')
_o27f = _T27Pages({_E27: f'src="{_OTHER27}" src="{_REAL27}"'})
_r27f = _o27f._resolve_turbo_cr_source(_W27)
report(_r27f and _r27f['playback_url'] == _REAL27,
       'the candidate matching the video id beats an unrelated one')

# ── 28. turbo.cr capture exits as soon as the signed file appears ────────────
# A field run confirmed the signed URL is minted by JavaScript: a plain GET of
# /v/<id> and of /embed/<id> does not contain it, so a browser IS needed. But
# the URL appears early in the capture and the browser then waits on
# VERIFIED_MEDIA — a <video> duration measurement that the probe can take
# itself. Leaving on the first turbocdn line is what makes the host fast.
_T28 = next(x for x in _t27_cls.body
            if isinstance(x, ast.FunctionDef) and x.name == '_is_turbocdn_media_line')
report(_T28 is not None and [ast.unparse(d) for d in _T28.decorator_list] == ['staticmethod'],
       '_is_turbocdn_media_line is defined once, as a staticmethod')
_t28_stub = ast.ClassDef(name='_T28Stub', bases=[], keywords=[], body=[_T28], decorator_list=[])
_t28_mod = ast.Module(body=[_t28_stub], type_ignores=[])
ast.fix_missing_locations(_t28_mod)
_t28_ns = {'urlparse': urlparse}
exec(compile(_t28_mod, '<main.py>', 'exec'), _t28_ns)
_t28 = _t28_ns['_T28Stub']()

_T28_TRUE = ("MEDIA_URL::https://dl100.turbocdn.st/turbo/data/Zr2uqa61FxPgp.mp4"
             "?exp=1789444585&token=916c938bd0d859daa52010e571089ed62c180282a59025c890333ad3ee7d4301"
             "&fn=%5B07-09-26%5D+%5BPornHub+with+Ads%5D.mp4")
for _label28, _line28, _want28 in [
    ('the signed file',      _T28_TRUE, True),
    ('rotated dl3 prefix',   _T28_TRUE.replace('dl100', 'dl3'), True),
    ('the embed tag',        'MEDIA_URL::https://turbo.cr/embed/Zr2uqa61FxPgp', False),
    ('the player library',   'MEDIA_URL::https://cdn.plyr.io/3.7.8/plyr.js', False),
    ('protocol-relative ad', 'MEDIA_URL:://bucklechemistdensity.com/on.js', False),
    ('ad beacon',            'MEDIA_URL::https://holahupa.com/aas/r45d/vki/2075904/tghr.js', False),
    ('analytics',            'MEDIA_URL::https://northstar.cr/js/s.js', False),
    ('bare cdn host',        'MEDIA_URL::https://cdn.tailwindcss.com', False),
    # The host check is what stops an unrelated site serving the same path
    # shape from arming the early exit.
    ('foreign host, same path', 'MEDIA_URL::https://evil.example/turbo/data/x.mp4?exp=1', False),
    ('not a MEDIA_URL line', _T28_TRUE.replace('MEDIA_URL::', 'VERIFIED_MEDIA::'), False),
    ('empty',                '', False),
]:
    report(_t28._is_turbocdn_media_line(_line28) is _want28, f'line detector {_label28} -> {_want28}')

# The candidate ranking must prefer the turbocdn shape on its own, because an
# early exit means VERIFIED_MEDIA (worth +6) may never fire for it.
report("self._is_turbocdn_media_line(f'MEDIA_URL::{url}')" in open('main.py', encoding='utf-8').read(),
       'the candidate scorer gives the turbocdn shape the same +6 the browser verification would')

# The download page is now part of the static walk, so a host that
# server-renders the link there is served without a browser at all.
_D27 = 'https://turbo.cr/d/QsCF7fNEp6Lhp'
_o28 = _T27Pages({_D27: f'src="{_REAL27}"'})
_r28 = _o28._resolve_turbo_cr_source(_W27)
report(_o28.fetched == [_W27, _E27, _D27],
       f'the walk tries watch, embed then download (got {[u.rsplit("/", 2)[-2] for u in _o28.fetched]})')
report(bool(_r28) and _r28['playback_url'] == _REAL27,
       'and a link found only on the download page still resolves without a browser')

# ── 29. the turbo.cr capture browser is never a visible window ───────────────
# The signed URL is minted by JavaScript, so a browser IS required — the field
# run proved the static path cannot find it. But it does not have to be a
# window on the user's desktop: --headless-capture is a true no-window mode
# (FamilyPornHD already uses it), and --headed-hidden only pushes a real
# window off-screen, which still lands in the taskbar.
_t29_cls = next(n for n in ast.walk(ast.parse(open('main.py', encoding='utf-8').read()))
                if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_t29_fn = [x for x in _t29_cls.body
           if isinstance(x, ast.FunctionDef) and x.name == '_resolve_stream_source']
report(len(_t29_fn) == 1, '_resolve_stream_source was located exactly once')
_t29_fn = _t29_fn[0]

# The dispatch branch for turbo.cr, walking only its own body so the rest of
# the elif chain cannot leak in.
_t29_branch = [n for n in ast.walk(_t29_fn)
               if isinstance(n, ast.If) and "endswith('.turbo.cr')" in ast.unparse(n.test)]
report(len(_t29_branch) == 1, 'the turbo.cr dispatch branch exists once')
_t29_body = _t29_branch[0]

def _t29_calls(statements):
    """(line, callee, kwargs) for capture calls in these statements only.

    Walks the branch BODY, never the whole If node: an If's end_lineno spans
    its orelse too, so walking the node would pull in every later elif in the
    dispatch chain and make a count assertion meaningless.
    """
    out = []
    for st in statements:
        for child in ast.walk(st):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr in ('_resolve_turbo_cr_source',
                                       '_resolve_stream_via_browser_click'):
                    out.append((child.lineno, child.func.attr,
                                {k.arg: ast.unparse(k.value) for k in child.keywords}))
    return sorted(out)

_t29_flat = _t29_calls(_t29_body.body)
report([c[1] for c in _t29_flat][:1] == ['_resolve_turbo_cr_source'],
       'the plain-HTTP attempt is still tried first')
_t29_caps = [c for c in _t29_flat if c[1] == '_resolve_stream_via_browser_click']
report(len(_t29_caps) == 2,
       f'exactly two capture attempts in the turbo.cr branch, no more (got {len(_t29_caps)})')
if len(_t29_caps) == 2:
    report(_t29_caps[0][2].get('headless') == 'True',
           'the first capture uses the true no-window mode, not an off-screen window')
    report(_t29_caps[1][2].get('headed_hidden') == 'True',
           'and only falls back to the off-screen headed engine if that fails')
    report(_t29_caps[0][0] < _t29_caps[1][0], 'in that order')

# And the generic fallthrough must not open a third one for the same link.
_t29_generic = [n for n in ast.walk(_t29_fn)
                if isinstance(n, ast.If) and "'turbo.cr' not in host" in ast.unparse(n.test)]
report(len(_t29_generic) == 1,
       "the generic capture path excludes turbo.cr so a failed headless try can't open another window")

# ── 30. a still-valid turbo.cr capture is not thrown away on double-click ────
# The playback path strips any cached playback_url that is neither
# use_mpv_ytdl nor pre_resolved. A browser_click result sets neither, so every
# double-click re-opened a browser to mint an identical URL. One field run
# shows both halves: one link reused its capture after 158 ms, another
# re-captured 75 s later, replacing exp=1789504083 with exp=1789504158 while
# the first was still valid.
_T30_WANT = ['_signed_url_expiry_epoch', '_signed_playback_url_is_expired',
             '_cached_signed_playback_is_trustworthy']
_t30_cls = next(n for n in ast.walk(ast.parse(open('main.py', encoding='utf-8').read()))
                if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_t30_m = {x.name: x for x in _t30_cls.body
          if isinstance(x, ast.FunctionDef) and x.name in _T30_WANT}
report(sorted(_t30_m) == sorted(_T30_WANT),
       f'all three expiry helpers were lifted from main.py (missing: '
       f'{sorted(set(_T30_WANT) - set(_t30_m))})')
_t30_stub = ast.ClassDef(name='_T30Stub', bases=[], keywords=[],
                         body=[_t30_m[w] for w in _T30_WANT], decorator_list=[])
_t30_mod = ast.Module(body=[_t30_stub], type_ignores=[])
ast.fix_missing_locations(_t30_mod)
_t30_ns = {'re': re, 'time': time, 'urlparse': urlparse, 'parse_qs': parse_qs}
exec(compile(_t30_mod, '<main.py>', 'exec'), _t30_ns)
_t30 = _t30_ns['_T30Stub']()

# exp values taken verbatim from the field log, plus offsets around `now`.
_NOW30 = int(time.time())
_FRESH30 = f'https://dl100.turbocdn.st/turbo/data/Zr2uqa61FxPgp.mp4?exp={_NOW30 + 3600}&token=4d404bdfea95'
for _label30, _url30, _want30 in [
    ('fresh turbocdn url',            _FRESH30, True),
    # NB: do NOT pin a literal epoch here. An earlier version reused the
    # exp=1789504079 from the field log and quietly started failing once the
    # clock passed 2026-09-15T20:27:59Z. Same shape, anchored to now.
    ('a real exp an hour out',     f'https://dl100.turbocdn.st/turbo/data/Zr2uqa61FxPgp.mp4?exp={_NOW30 + 3600}&token=4d404bdfea95', True),
    ('inside the 90 s grace window',  f'https://dl100.turbocdn.st/turbo/data/x.mp4?exp={_NOW30 + 30}&token=ab', False),
    ('already expired',               f'https://dl100.turbocdn.st/turbo/data/x.mp4?exp={_NOW30 - 600}&token=ab', False),
    ('turbocdn but no exp at all',    'https://dl100.turbocdn.st/turbo/data/x.mp4?token=ab', False),
    # The scope guard: widening this to other hosters needs field evidence
    # per host, so a foreign host must keep the old behaviour.
    ('fresh but not turbocdn',        f'https://cdn.example.net/turbo/data/x.mp4?exp={_NOW30 + 3600}&token=ab', False),
    ('rotated dl prefix',             f'https://dl3.turbocdn.st/turbo/data/x.mp4?exp={_NOW30 + 3600}&token=ab', True),
    ('empty',                         '', False),
    ('None',                          None, False),
]:
    report(_t30._cached_signed_playback_is_trustworthy(_url30) is _want30,
           f'trustworthy? {_label30} -> {_want30}')

# The playback condition itself must now be gated on that helper, and the
# pre-existing clauses must survive untouched.
_t30_src = open('main.py', encoding='utf-8').read()
report('_signed_still_fresh = self._cached_signed_playback_is_trustworthy(' in _t30_src,
       'the playback path consults the helper before stripping the cached url')
report('not _cached_entry.get(\'use_mpv_ytdl\') and not _cached_entry.get(\'pre_resolved_playback_url\')'
       in _t30_src,
       'the blanket clause is still there for every other host')
report('_family_capture_stale\n' in _t30_src or '_family_capture_stale' in _t30_src,
       'and the FamilyPornHD staleness clause is preserved')

# ── 31. the captured-links button is always up, except in fullscreen ─────────
# The third branch of the indicator used to hide the button outright whenever
# nothing had been captured and no analysis was running, so on a fresh start
# there was no way into the popup at all. It is now a permanent toggle; only
# fullscreen hides it, matching the other two branches.
_T31 = next(x for x in _t30_cls.body
            if isinstance(x, ast.FunctionDef) and x.name == '_update_remote_loading_indicator')
report(_T31 is not None, '_update_remote_loading_indicator was lifted from main.py')
_t31_stub = ast.ClassDef(name='_T31Stub', bases=[], keywords=[], body=[_T31], decorator_list=[])
_t31_mod = ast.Module(body=[_t31_stub], type_ignores=[])
ast.fix_missing_locations(_t31_mod)
_t31_ns = {}
exec(compile(_t31_mod, '<main.py>', 'exec'), _t31_ns)

class _T31Label:
    def __init__(self):
        self.shown, self.text, self.tooltip = None, '', ''
        self.adjusted = 0
    def show(self): self.shown = True
    def hide(self): self.shown = False
    def setText(self, t): self.text = t
    def adjustSize(self): self.adjusted += 1
    def setToolTip(self, t): self.tooltip = t

class _T31Timer:
    def __init__(self): self.active, self.stopped = False, 0
    def isActive(self): return self.active
    def start(self): self.active = True
    def stop(self): self.active = False; self.stopped += 1

class _T31Host(_t31_ns['_T31Stub']):
    def __init__(self, fullscreen, active_count, rows, unseen=0):
        self.remote_loading_label = _T31Label()
        self.remote_loading_timer = _T31Timer()
        self._remote_analysis_count = active_count
        self._link_flow_rows = rows
        self._link_flow_unseen = unseen
        self._remote_loading_frames = ('o', 'O')
        self._remote_loading_frame = 0
        self._fullscreen = fullscreen
        self.repositioned = 0
    def _is_app_fullscreen(self): return self._fullscreen
    def _reposition_hb_overlay(self): self.repositioned += 1

# (label, fullscreen, active, rows, expected_visible, expected_text_prefix)
for _label31, _fs31, _act31, _rows31, _want31, _txt31 in [
    ('fresh start, nothing captured', False, 0, None, True,  '≡'),
    ('fresh start, in fullscreen',    True,  0, None, False, '≡'),
    ('idle with captured links',      False, 0, ['http://a/1'], True, '≡'),
    ('idle with links, fullscreen',   True,  0, ['http://a/1'], False, '≡'),
    ('unseen badge',                  False, 0, ['http://a/1'], True, '≡3'),
    ('analysis running',              False, 2, None, True,  'o'),
    ('analysis running, fullscreen',  True,  2, None, False, 'o'),
]:
    _h31 = _T31Host(_fs31, _act31, _rows31,
                    unseen=3 if _label31 == 'unseen badge' else 0)
    _h31._update_remote_loading_indicator()
    report(_h31.remote_loading_label.shown is _want31,
           f'{_label31}: visible={_want31}')
    if _want31 and _txt31:
        report(_h31.remote_loading_label.text.startswith(_txt31),
               f'{_label31}: text starts {_txt31!r} (got {_h31.remote_loading_label.text!r})')

# And it has to come up at launch rather than after the first capture.
report('QTimer.singleShot(0, self._update_remote_loading_indicator)'
       in open('main.py', encoding='utf-8').read(),
       'the indicator is refreshed once at startup so the button is there from launch')
# Fullscreen must still be able to hide it, and exiting must bring it back.
report('self.remote_loading_label.hide()' in open('main.py', encoding='utf-8').read(),
       'entering fullscreen still hides it explicitly')

# ── 32. the captured-links popup resizes from its borders only ───────────────
# It is a Qt.Popup, which grabs the mouse, so a click on the desktop outside
# the frame is still delivered to the edge-resize filter with coordinates
# beyond the rect. "pos.x() <= MARGIN" is satisfied by x=-50, so an outside
# click read as the left edge: it started a resize AND swallowed the press,
# which is why the popup took two clicks to dismiss.
_t32_host = next(n for n in ast.walk(ast.parse(open('main.py', encoding='utf-8').read()))
                 if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_t32_m = [x for x in _t32_host.body
          if isinstance(x, ast.FunctionDef) and x.name == '_ensure_link_flow_panel'][0]
_t32_cls = [n for n in ast.walk(_t32_m)
            if isinstance(n, ast.ClassDef) and n.name == '_EdgeResizeFilter']
report(len(_t32_cls) == 1, '_EdgeResizeFilter was lifted out of _ensure_link_flow_panel')
_t32_mod = ast.Module(body=_t32_cls, type_ignores=[])
ast.fix_missing_locations(_t32_mod)

class _T32Pt:
    def __init__(self, x, y): self._x, self._y = x, y
    def x(self): return self._x
    def y(self): return self._y
    def __sub__(self, o): return _T32Pt(self._x - o._x, self._y - o._y)

class _T32Pos:
    def __init__(self, x, y): self._p = _T32Pt(x, y)
    def toPoint(self): return self._p

class _T32Rect:
    def __init__(self, g=None):
        # The filter copies with QRect(d.geometry()), i.e. from another Rect.
        if isinstance(g, _T32Rect):
            self._g = tuple(g._g)
        else:
            self._g = tuple(g or (0, 0, 860, 460))
    def x(self): return self._g[0]
    def y(self): return self._g[1]
    def width(self): return self._g[2]
    def height(self): return self._g[3]

class _T32Event:
    def __init__(self, et, x=0, y=0, gx=0, gy=0):
        self._et = et
        self._pos, self._gpos = _T32Pos(x, y), _T32Pos(gx, gy)
    def type(self): return self._et
    # Must be the enum, not a bare 'left': the filter compares
    # event.button() == Qt.MouseButton.LeftButton and a str never equals it.
    def button(self): return _T32Qt.MouseButton.LeftButton
    def position(self): return self._pos
    def globalPosition(self): return self._gpos
    def accept(self): pass

class _T32Dialog:
    def __init__(self, w=860, h=460):
        self._geo = [0, 0, w, h]; self.cursor = 'unset'
    def width(self): return self._geo[2]
    def height(self): return self._geo[3]
    def minimumWidth(self): return 480
    def minimumHeight(self): return 240
    def geometry(self): return _T32Rect(self._geo)
    def setGeometry(self, x, y, w, h): self._geo = [x, y, w, h]
    def setCursor(self, c): self.cursor = c
    def unsetCursor(self): self.cursor = 'unset'

class _T32QObject:
    def __init__(self, *a): pass

class _T32Enum:
    def __init__(self, name): self._n = name
    def __eq__(self, o): return isinstance(o, _T32Enum) and o._n == self._n
    def __hash__(self): return hash(self._n)

class _T32Qt:
    class MouseButton: LeftButton = _T32Enum('left')
    class CursorShape:
        SizeFDiagCursor = _T32Enum('fdiag'); SizeBDiagCursor = _T32Enum('bdiag')
        SizeHorCursor = _T32Enum('hor'); SizeVerCursor = _T32Enum('ver')

class _T32QEvent:
    class Type:
        MouseButtonPress = _T32Enum('press')
        MouseMove = _T32Enum('move')
        MouseButtonRelease = _T32Enum('release')

_t32_ns = {'QObject': _T32QObject, 'QRect': _T32Rect, 'Qt': _T32Qt,
           'QEvent': _T32QEvent}
exec(compile(_t32_mod, '<main.py>', 'exec'), _t32_ns)

_P32, _M32, _R32 = _T32QEvent.Type.MouseButtonPress, _T32QEvent.Type.MouseMove, _T32QEvent.Type.MouseButtonRelease

def _t32_new():
    d = _T32Dialog()
    return d, _t32_ns['_EdgeResizeFilter'](d)

# A press OUTSIDE the frame must not be treated as an edge, and must not be
# consumed -- swallowing it is what stopped the popup closing on one click.
for _label32, _x32, _y32 in [
    ('far left of the frame',   -50, 100),
    ('far above the frame',     100, -50),
    ('far right of the frame',  910, 100),
    ('far below the frame',     100, 510),
    ('outside bottom-right',    910, 510),
]:
    _d32, _f32 = _t32_new()
    _consumed32 = _f32.eventFilter(_d32, _T32Event(_P32, _x32, _y32, _x32, _y32))
    report(_consumed32 is not True, f'press {_label32} is NOT consumed by the resize filter')
    report(_f32._drag is None, f'press {_label32} does NOT start a resize')
    _geo_before32 = list(_d32._geo)
    _f32.eventFilter(_d32, _T32Event(_M32, _x32 - 200, _y32 - 200, _x32 - 200, _y32 - 200))
    report(_d32._geo == _geo_before32, f'dragging after a press {_label32} leaves the size alone')

# Genuine edges inside the frame must still resize.
for _label32, _x32, _y32, _want32 in [
    ('left edge',    3,   230, 'fdiag-or-hor'),
    ('right edge',   857, 230, 'fdiag-or-hor'),
    ('top edge',     430, 3,   'ver'),
    ('bottom edge',  430, 457, 'ver'),
    ('bottom-right', 857, 457, 'diag'),
]:
    _d32, _f32 = _t32_new()
    report(_f32.eventFilter(_d32, _T32Event(_P32, _x32, _y32, _x32, _y32)) is True,
           f'press on the {_label32} starts a resize')
    report(_f32._drag is not None, f'press on the {_label32} records the drag')

# And the drag itself still moves the geometry, then releases cleanly.
_d32, _f32 = _t32_new()
_f32.eventFilter(_d32, _T32Event(_P32, 857, 457, 857, 457))
_f32.eventFilter(_d32, _T32Event(_M32, 900, 500, 900, 500))
report(_d32._geo[2] > 860 and _d32._geo[3] > 460,
       f'dragging the bottom-right corner grows it (geo={_d32._geo})')
_f32.eventFilter(_d32, _T32Event(_R32, 900, 500, 900, 500))
report(_f32._drag is None, 'release clears the drag')

# The middle of the popup is not an edge either.
_d32, _f32 = _t32_new()
report(_f32.eventFilter(_d32, _T32Event(_P32, 430, 230, 430, 230)) is not True,
       'a press in the middle of the popup is left alone')

# ── 33. the resize handle sits in the bottom-LEFT corner ─────────────────────
# QSizeGrip is hardcoded to move the bottom-right corner with the top-left
# pinned, so it cannot just be moved to the left -- it would drag the edge
# opposite the cursor. The replacement keeps the top-RIGHT corner fixed.
_t33_cls = [n for n in ast.walk(_t32_m)
            if isinstance(n, ast.ClassDef) and n.name == '_BottomLeftGrip']
report(len(_t33_cls) == 1, '_BottomLeftGrip was lifted out of _ensure_link_flow_panel')
_t33_main = open('main.py', encoding='utf-8').read()
report('QSizeGrip as _LFSizeGrip' not in _t33_main and '_LFSizeGrip(' not in _t33_main,
       'the bottom-right-only QSizeGrip is no longer imported or instantiated')
_t33_mod = ast.Module(body=_t33_cls, type_ignores=[])
ast.fix_missing_locations(_t33_mod)

class _T33Widget:
    def __init__(self, *a): self.fixed = None; self.tip = ''; self.cur = None; self.sheet = ''
    def setFixedSize(self, w, h): self.fixed = (w, h)
    def setToolTip(self, t): self.tip = t
    def setCursor(self, c): self.cur = c
    def setStyleSheet(self, s): self.sheet = s
    def mousePressEvent(self, e): pass

class _T33Dialog:
    def __init__(self, x=100, y=80, w=860, h=460):
        self._geo = [x, y, w, h]
    def width(self): return self._geo[2]
    def height(self): return self._geo[3]
    def minimumWidth(self): return 480
    def minimumHeight(self): return 240
    def geometry(self): return _T32Rect(self._geo)
    def setGeometry(self, x, y, w, h): self._geo = [x, y, w, h]

_t33_ns = {'QWidget': _T33Widget, 'QRect': _T32Rect, 'Qt': _T32Qt}
exec(compile(_t33_mod, '<main.py>', 'exec'), _t33_ns)

def _t33_drag(dx, dy, start=(100, 80, 860, 460)):
    d = _T33Dialog(*start)
    g = _t33_ns['_BottomLeftGrip'](d)
    g.mousePressEvent(_T32Event(_P32, 5, 455, 105, 535))
    g.mouseMoveEvent(_T32Event(_M32, 5 + dx, 455 + dy, 105 + dx, 535 + dy))
    return d, g

# Dragging LEFT widens the popup and the right edge must not move.
_d33, _g33 = _t33_drag(-200, 0)
report(_d33._geo[2] == 1060, f'drag 200 left -> width 1060 (got {_d33._geo[2]})')
report(_d33._geo[0] == -100, f'and x moves to -100 (got {_d33._geo[0]})')
report(_d33._geo[0] + _d33._geo[2] == 960,
       f'the right edge stays put at 960 (got {_d33._geo[0] + _d33._geo[2]})')
report(_d33._geo[1] == 80, 'the top edge does not move')

# Dragging DOWN grows the height; the top stays pinned.
_d33, _g33 = _t33_drag(0, 140)
report(_d33._geo[3] == 600, f'drag 140 down -> height 600 (got {_d33._geo[3]})')
report(_d33._geo[1] == 80, 'height grows downward only')

# Both at once, which is what a corner drag actually is.
_d33, _g33 = _t33_drag(-100, 100)
report(_d33._geo == [0, 80, 960, 560], f'corner drag -> [0, 80, 960, 560] (got {_d33._geo})')

# Clamping at the minimums.
_d33, _g33 = _t33_drag(2000, 0)
report(_d33._geo[2] == 480, f'width clamps at the 480 minimum (got {_d33._geo[2]})')
report(_d33._geo[0] == 480, f'and x follows so the right edge holds (got {_d33._geo[0]})')
_d33, _g33 = _t33_drag(0, -2000)
report(_d33._geo[3] == 240, f'height clamps at the 240 minimum (got {_d33._geo[3]})')

# Release ends the drag; a move with no press is inert.
_d33, _g33 = _t33_drag(-50, 50)
_g33.mouseReleaseEvent(_T32Event(_T32QEvent.Type.MouseButtonRelease, 0, 0, 0, 0))
report(_g33._drag is None, 'release clears the drag')
_before33 = list(_d33._geo)
_g33.mouseMoveEvent(_T32Event(_M32, 400, 400, 900, 900))
report(_d33._geo == _before33, 'a move after release changes nothing')
_d33b = _T33Dialog()
_g33b = _t33_ns['_BottomLeftGrip'](_d33b)
_g33b.mouseMoveEvent(_T32Event(_M32, 300, 300, 700, 700))
report(_d33b._geo == [100, 80, 860, 460], 'a move with no press changes nothing')

# ── 34. sextb: an ad iframe is not a stream, and //host/path is not a file ──
# Pasting https://sextb.net/nima-081-sub put one row in the playlist that
# looked like a USB/network file. Two separate defects made that happen:
#   * the Playwright fallback captured //duq8bcrl.xyz/api/spots/346725?p=1&
#     s1=%subid1%&kw= -- an ad creative on a throwaway .xyz, which the old
#     AD_HOSTS list did not mention;
#   * that value is protocol-relative, _is_remote_url returns False for it, so
#     the playlist treated it as a local file and Windows rendered
#     \\duq8bcrl.xyz\api\spots\... as a UNC path.
import sextb_grab as sg34

_AD34 = '//duq8bcrl.xyz/api/spots/346725?p=1&amp;s1=%subid1%&amp;kw='
for _label34, _url34, _want34 in [
    ('the captured ad',            _AD34, True),
    ('ad spot on a plain host',    'https://cdn.example.net/api/spots/99?p=1', True),
    ('unexpanded macro only',      'https://player.example.net/e/x?kw=%keyword%', True),
    ('known ad host',              'https://trailerhg.xyz/e/abc', True),
    ('googlesyndication',          'https://googlesyndication.com/x', True),
    ('a real doodstream player',   'https://doodstream.com/e/qlb9nbe23jda', False),
    ('a real protocol-relative cdn', '//cdn.example.net/hls/x/master.m3u8', False),
    ('a real embed',               'https://emturbovid.com/t/abc123', False),
    ('empty',                      '', True),
    # Placeholder srcs the player leaves in the DOM. Field: every button on
    # bank-096-rm and aldn-072-rm yielded `javascript:false`, and because both
    # pages produced the identical string the second video was dropped as a
    # DUPLICATE -- which is why only one row reached the playlist.
    ('javascript: placeholder',    'javascript:false', True),
    ('about:blank',                'about:blank', True),
    ('blob: source',               'blob:https://player.example/x', True),
    ('data: uri',                  'data:text/html,x', True),
    ('bare fragment',              '#', True),
]:
    report(sg34._is_ad_iframe(_url34) is _want34, f'ad filter {_label34} -> {_want34}')

_sg34_src = open('sextb_grab.py', encoding='utf-8').read()
# Superseded twice. The click-through no longer scans for "the first iframe
# that is not a known ad"; it reads #sextb-player through _extract_inline_player,
# which unescapes the src, requires a positive player match and excludes the
# trailer and the page's own embed. Those behaviours live there now.
report('_extract_inline_player(html_check, url)' in _sg34_src,
       'the click-through reads #sextb-player, not the first iframe')
report('cand = unescape(im.group(1).strip())' in _sg34_src,
       'and the entity-escaped iframe src is unescaped before use')
report('AD_HOSTS' not in _sg34_src, 'the old host-only list is gone')
# The 403s on every /api/episode/ call are a TLS fingerprint rejection, so the
# session has to impersonate a browser. Order matters: curl_cffi first.
report('def _make_session(' in _sg34_src, '_make_session exists')
report(_sg34_src.index("cfreq.Session(impersonate='chrome131')")
       < _sg34_src.index('cloudscraper.create_scraper'),
       'curl_cffi is preferred over cloudscraper, which is preferred over requests')
report('session.get(url, headers=headers, timeout=30)' in _sg34_src,
       'the page fetch goes through that session rather than a bare requests one')
report('session={session_kind}' in _sg34_src,
       'and the log says which session was used')

# The protocol-relative half, against the real _sanitize_url.
_t34 = next(x for x in _t29_cls.body if isinstance(x, ast.FunctionDef) and x.name == '_sanitize_url')
_t34_stub = ast.ClassDef(name='_T34Stub', bases=[], keywords=[], body=[_t34], decorator_list=[])
_t34_mod = ast.Module(body=[_t34_stub], type_ignores=[])
ast.fix_missing_locations(_t34_mod)
_t34_ns = {'re': re}
exec(compile(_t34_mod, '<main.py>', 'exec'), _t34_ns)
_t34 = _t34_ns['_T34Stub']()

for _label34, _in34, _want34 in [
    ('protocol-relative', '//cdn.example.net/hls/x/master.m3u8', 'https://cdn.example.net/hls/x/master.m3u8'),
    ('the captured ad',   _AD34, 'https:' + _AD34),
    ('padded',            '  //turbo.cr/x.mp4  ', 'https://turbo.cr/x.mp4'),
    ('already absolute',  'https://doodstream.com/e/abc', 'https://doodstream.com/e/abc'),
    ('windows path',      'C:\\Videos\\movie.mp4', 'C:\\Videos\\movie.mp4'),
    ('posix path',        '/home/user/movie.mp4', '/home/user/movie.mp4'),
    ('UNC path',          '\\\\NAS\\share\\movie.mp4', '\\\\NAS\\share\\movie.mp4'),
    ('triple slash is not a host', '///weird', '///weird'),
    ('empty',             '', ''),
]:
    _got34 = _t34._sanitize_url(_in34)
    report(_got34 == _want34, f'sanitize {_label34}: {_want34!r} (got {_got34!r})')

# ── 35. sextb scrapes for a player, instead of blocklisting ads ──────────────
# Every field report so far has been a new ad domain slipping past a
# blocklist: first duq8bcrl.xyz, then t.dtscout.com. sextb is the same kind of
# aggregator as jav.guru / roshy / javgg, so it now uses the same positive
# test generic_jav_grab uses -- accept only a known player host or a media
# file. Also: with curl_cffi past the 403 the API body stopped parsing as
# JSON, so the parser scrapes the body as markup instead.
for _label35, _url35, _want35 in [
    ('the dtscout tracker', 'https://t.dtscout.com/idg/?su=104017895091880F84399B51FA39B561', False),
    ('the duq8bcrl ad',     '//duq8bcrl.xyz/api/spots/346725?p=1&s1=%subid1%&kw=', False),
    ('placeholder',         'javascript:false', False),
    ('sextb own embed',     'https://sextb.net/e/4124790', True),
    ('doodstream embed',    'https://doodstream.com/e/qlb9nbe23jda', True),
    ('emturbovid embed',    'https://emturbovid.com/t/abc123', True),
    ('plain cdn m3u8',      'https://cdn.example.net/hls/x/master.m3u8', True),
    ('plain cdn mp4',       'https://cdn.example.net/v/movie.mp4', True),
    ('entity-escaped embed', 'https://doodstream.com/e/abc?a=1&amp;b=2', True),
    ('empty',               '', False),
]:
    report(sg34._looks_like_player(_url35) is _want35, f'player test {_label35} -> {_want35}')

# Drive the real _fetch_episode_stream with stub responses.
class _T35Resp:
    def __init__(self, status=200, ctype='text/html', body='', json_data=None):
        self.status_code = status
        self.headers = {'Content-Type': ctype}
        self.text = body
        self._json = json_data
        self.url = 'x'
    def json(self):
        if self._json is None:
            raise ValueError('Expecting value: line 1 column 1 (char 0)')
        return self._json

class _T35Session:
    def __init__(self, resp): self.resp = resp; self.calls = []
    def get(self, url, headers=None, timeout=None):
        self.calls.append(url); return self.resp

_IFRAME_BODY = ('<div class="player"><iframe src="https://sextb.net/e/4124790" '
                'frameborder="0" allowfullscreen></iframe></div>')
for _label35, _resp35, _want35 in [
    ('HTML body with a player iframe',  _T35Resp(body=_IFRAME_BODY), 'https://sextb.net/e/4124790'),
    ('HTML body with only an ad',       _T35Resp(body='<iframe src="https://t.dtscout.com/idg/?su=abc"></iframe>'), None),
    ('json with a src key',             _T35Resp(ctype='application/json', json_data={'src': 'https://doodstream.com/e/zzz'}), 'https://doodstream.com/e/zzz'),
    ('json with an html blob',          _T35Resp(ctype='application/json', json_data={'html': _IFRAME_BODY}), 'https://sextb.net/e/4124790'),
    ('json that is not an object',      _T35Resp(ctype='application/json', body=_IFRAME_BODY), 'https://sextb.net/e/4124790'),
    ('empty body',                      _T35Resp(body=''), None),
    ('HTTP 403',                        _T35Resp(status=403, body='blocked'), None),
    ('bare m3u8 in the body',           _T35Resp(body='<script>var u="https://cdn.example.net/hls/x/master.m3u8";</script>'), 'https://cdn.example.net/hls/x/master.m3u8'),
]:
    _s35 = _T35Session(_resp35)
    _got35 = sg34._fetch_episode_stream('16934905', '4124790', 'https://sextb.net/jul-509-rm', _s35)
    report(_got35 == _want35, f'api parse {_label35} -> {_want35!r} (got {_got35!r})')

# ── 36. sextb inline player, against the real saved watch page ───────────────
# sextb.txt is the saved source of https://sextb.net/jul-509-rm (uploaded by
# the user). Every sextb turn before this one guessed at the page shape; this
# is the first fixture taken from the live site, so the assertions here are
# ground truth rather than hypothesis.
#
# What the real page shows: the active episode's player is ALREADY in the
# document, inside <div id="sextb-player">, as
#   https://turboplays.click/t/6a80cae62b911?poster=...
# grab_all_static never looked at the page's own iframes -- it went straight
# to the buttons and the /api/episode/ endpoint, which needs a Cloudflare
# Turnstile token and so returned 403, then non-JSON.
_SEXTB_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sextb.txt')
if os.path.exists(_SEXTB_PAGE):
    _sp = open(_SEXTB_PAGE, encoding='utf-8', errors='replace').read()
    _SEXTB_URL = 'https://sextb.net/jul-509-rm'
    _WANT_PLAYER = ('https://turboplays.click/t/6a80cae62b911'
                    '?poster=https://cdn001.imggle.net/cover-player.jpg')

    report(sg34._extract_inline_player(_sp, _SEXTB_URL) == _WANT_PLAYER,
           'inline player is the turboplays iframe')
    report('turboplays' in sg34.PLAYER_HOST_TOKENS, 'turboplays is on the player allowlist')
    report('turboplays' not in sg34.AD_HOST_TOKENS, 'turboplays is NOT on the ad blocklist')

    # Of every iframe on the page, exactly one may survive the whole filter.
    _iframes = [m.group(1) for m in re.finditer(r'<iframe[^>]+src="([^"]+)"', _sp, re.I)]
    report(len(_iframes) == 8, f'the saved page carries 8 iframes (got {len(_iframes)})')
    _survivors = [u for u in _iframes
                  if sg34._looks_like_player(u) and not sg34._is_trailer_iframe(u)
                  and not sg34._is_self_embed(u, _SEXTB_URL)]
    report(_survivors == [_WANT_PLAYER],
           f'exactly one iframe survives -> the film (got {_survivors})')

    # The three decoys, each rejected for its own reason.
    report(not sg34._looks_like_player('https://trailerhg.xyz/e/xfu7jtpb70d9')
           and sg34._is_trailer_iframe('https://trailerhg.xyz/e/xfu7jtpb70d9'),
           'the preview trailer is rejected (never the film)')
    report(sg34._is_self_embed('https://sextb.net/e/jul-509-rm', _SEXTB_URL)
           and not sg34._is_self_embed('https://sextb.net/e/other-slug', _SEXTB_URL),
           'only the page\'s own embed counts as a self-embed')
    # Five, not four: spot 346725 is embedded twice on the page.
    _ads = [u for u in _iframes if 'duq8bcrl.xyz' in u]
    report(len(_ads) == 5 and not any(sg34._looks_like_player(u) for u in _ads),
           f'all {len(_ads)} ad iframes are rejected')

    # The buttons really are what _extract_buttons reports -- and the two VIP
    # buttons on the page are excluded.
    _btns = sg34._extract_buttons(_sp)
    report([b['label'] for b in _btns] == ['TB', 'SW', 'PM', 'DD', 'FL'],
           f"5 non-VIP buttons with the right labels (got {[b['label'] for b in _btns]})")
    report(all(b['source'] == '16934905' for b in _btns), 'every button carries the film id')
    report(sg34._extract_title(_sp).startswith('JUL-509-RM'), 'title parsed from the real page')

    # Order matters: the inline player must be taken before any API call.
    _src36 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'sextb_grab.py'), encoding='utf-8').read()
    report(_src36.index('_extract_inline_player(html, url)') < _src36.index('_fetch_episode_stream(btn'),
           'the inline player is read before the API is called')
else:
    report(False, 'sextb.txt fixture is present')

# ── 37. sextb second hop: player page -> video file ──────────────────────────
# turboplays.click/t/<id> is HTML, so grab_all_static takes one more hop to
# scrape the .m3u8/.mp4 out of it, the way generic_jav_grab does. Without that
# the playlist receives a web page and mpv has nothing to play.
for _u37, _w37 in [
    ('https://cdn.example.net/hls/x/master.m3u8',            True),
    ('https://cdn.example.net/hls/x/master.m3u8?token=abc',  True),
    ('https://cdn.example.net/v/movie.mp4',                  True),
    ('https://turboplays.click/t/6a80cae62b911',             False),
    ('https://sextb.net/e/jul-509-rm',                       False),
    ('',                                                     False),
]:
    report(sg34._is_media_url(_u37) is _w37, f'media test {_u37[:44]!r} -> {_w37}')

_M3U8_PAGE = (
    '<html><script>var src = "https:\\/\\/cdn.example.net\\/hls\\/jul509\\/master.m3u8?token=zz";'
    '<video src="https://cdn.example.net/v/movie.mp4"></video>'
    '<source src="https://cdn.example.net/v/preview-10s.mp4"></source>'
    '<iframe src="https://doodstream.com/e/abc123"></iframe></script></html>')
report(sg34._scrape_media_from_html(_M3U8_PAGE) == [
    'https://cdn.example.net/hls/jul509/master.m3u8?token=zz',
    'https://cdn.example.net/v/movie.mp4'],
    'escaped-slash m3u8 and mp4 are unescaped, the 10 s preview is dropped')
report(sg34._nested_player_iframes(_M3U8_PAGE) == ['https://doodstream.com/e/abc123'],
       'the nested player iframe is followed')
report(sg34._scrape_media_from_html('') == [] and sg34._nested_player_iframes('') == [],
       'empty markup yields nothing')

class _T37Resp:
    def __init__(self, body): self.text = body; self.status_code = 200
    def json(self): return {}
class _T37Session:
    def __init__(self, pages): self.pages = pages; self.seen = []
    def get(self, url, headers=None, timeout=None):
        self.seen.append(url)
        return _T37Resp(self.pages.get(url, ''))

_s37 = _T37Session({'https://turboplays.click/t/6a80cae62b911': _M3U8_PAGE})
_got37 = sg34._resolve_embed_page('https://turboplays.click/t/6a80cae62b911',
                                  'https://sextb.net/jul-509-rm', _s37)
report(_got37 == ['https://cdn.example.net/hls/jul509/master.m3u8?token=zz',
                  'https://cdn.example.net/v/movie.mp4'],
       f'one hop turns the player page into media (got {_got37})')

# A page with no media but a nested player: follow it exactly once.
_s37b = _T37Session({
    'https://turboplays.click/t/6a80cae62b911': '<iframe src="https://doodstream.com/e/abc123"></iframe>',
    'https://doodstream.com/e/abc123': '<script>file:"https://cdn.example.net/v/deep.mp4";</script>',
})
_got37b = sg34._resolve_embed_page('https://turboplays.click/t/6a80cae62b911',
                                   'https://sextb.net/jul-509-rm', _s37b)
report(_got37b == ['https://cdn.example.net/v/deep.mp4'],
       f'the nested player is followed one hop (got {_got37b})')
report(_s37b.seen == ['https://turboplays.click/t/6a80cae62b911',
                      'https://doodstream.com/e/abc123'],
       'exactly two fetches -- it does not recurse forever')
report(sg34._resolve_embed_page('https://x.example/y', '', _T37Session({}), depth=2) == [],
       'the hop limit is enforced')

# ── 38. grab_all_static end to end, on the real saved page ───────────────────
# Drives the actual function with a stub HTTP layer: the real sextb.txt for the
# watch page and a plausible player page for the turboplays embed. This is the
# path the user's playlist depends on.
class _T38Time:
    @staticmethod
    def sleep(_s): return None
    @staticmethod
    def time(): return 1_700_000_000.0

class _T38Resp:
    def __init__(self, body, ctype='text/html'):
        self.text = body; self.status_code = 200
        self.headers = {'Content-Type': ctype}

# What each episode button resolves to on the real site. The page opens on TB.
_T38_HOSTERS = {
    '4124790': 'https://turboplays.click/t/6a80cae62b911?poster=https://cdn001.imggle.net/cover-player.jpg',
    '4124770': 'https://streamwish.com/e/sw0001',
    '4128035': 'https://doodstream.com/e/dd0002',
    '4124826': 'https://doodstream.com/e/dd0003',
    '4124772': 'https://filemoon.sx/e/fm0004',
}

def _t38_xor_encode(plain, key):
    import base64 as _b64
    return _b64.b64encode(bytes(ord(c) ^ ord(key[i % len(key)])
                                for i, c in enumerate(plain))).decode()

class _T38Session:
    """Stands in for curl_cffi, including POST /ajax/player with key rotation."""
    def __init__(self, pages, pk0='bdc3144fc04eaa106cf6dba2db0b8393'):
        self.pages = pages; self.seen = []; self.posts = []
        self._pk = pk0; self._n = 0
    def get(self, url, headers=None, timeout=None):
        self.seen.append(url); return _T38Resp(self.pages.get(url, ''))
    def post(self, url, data=None, headers=None, timeout=None):
        self.seen.append(url)
        self.posts.append(dict(data or {}))
        if url != 'https://sextb.net/ajax/player':
            return _T38Resp('{}', 'application/json')
        epid = str((data or {}).get('episode'))
        hoster = _T38_HOSTERS.get(epid)
        if not hoster:
            return _T38Resp(json.dumps({'error': 'not found'}), 'application/json')
        self._n += 1
        next_pk = 'k%031d' % self._n
        payload = {
            'player_enc': _t38_xor_encode(f'<iframe src="{hoster}"></iframe>', self._pk),
            'next_pt': 'pt%030d' % self._n,
            'next_pk': next_pk,
        }
        self._pk = next_pk          # the real site rotates the key every call
        return _T38Resp(json.dumps(payload), 'application/json')

_T38_MEDIA = ('<script>sources:[{file:"https:\\/\\/cdn.turbo.example\\/jul-509\\/index.m3u8'
              '?token=abc"}]</script>')
_real_time = sg34.time
_real_session = sg34._make_session
try:
    _sess38 = _T38Session({
        'https://sextb.net/jul-509-rm': _sp,
        'https://turboplays.click/t/6a80cae62b911?poster=https://cdn001.imggle.net/cover-player.jpg': _T38_MEDIA,
        'https://streamwish.com/e/sw0001': '<script>f:"https:\\/\\/cdn.example.net\\/sw\\/index.m3u8";</script>',
        'https://doodstream.com/e/dd0002': '<script>f:"https:\\/\\/cdn.example.net\\/dd2\\/index.m3u8";</script>',
        'https://doodstream.com/e/dd0003': '<script>f:"https:\\/\\/cdn.example.net\\/dd3\\/index.m3u8";</script>',
        'https://filemoon.sx/e/fm0004':    '<script>f:"https:\\/\\/cdn.example.net\\/fm\\/index.m3u8";</script>',
    })
    sg34.time = _T38Time
    sg34._make_session = lambda: (_sess38, 'test-stub')
    _res38 = sg34.grab_all_static('https://sextb.net/jul-509-rm')
finally:
    sg34.time = _real_time
    sg34._make_session = _real_session

report(_res38['streams'] == ['https://cdn.turbo.example/jul-509/index.m3u8?token=abc',
                             'https://cdn.example.net/sw/index.m3u8',
                             'https://cdn.example.net/dd2/index.m3u8',
                             'https://cdn.example.net/dd3/index.m3u8',
                             'https://cdn.example.net/fm/index.m3u8'],
       f"grab_all_static returns every hoster's .m3u8 (got {_res38['streams']})")
report(_res38['title'].startswith('JUL-509-RM'), 'grab_all_static keeps the real title')
# The page was fetched once, the embed once, and the remaining buttons still
# go to the API -- the inline player is an addition, not a replacement.
report(_sess38.seen[0] == 'https://sextb.net/jul-509-rm', 'the watch page is fetched first')
# Superseded: the buttons no longer call /api/episode/ (an endpoint that does
# not appear anywhere in sextb.js). They POST /ajax/player, once per button.
report(sum(1 for u in _sess38.seen if u == 'https://sextb.net/ajax/player') == 5,
       f'all 5 buttons POST /ajax/player (posts={len(_sess38.posts)})')
report(not any('/api/episode/' in u for u in _sess38.seen),
       'the dead /api/episode/ endpoint is no longer called')
report([p['episode'] for p in _sess38.posts] ==
       ['4124790', '4124770', '4128035', '4124826', '4124772'],
       'every button is sent with its own episode id')
report(all(p['filmId'] == '16934905' for p in _sess38.posts),
       'filmId comes from the page')
report(len({p['pt'] for p in _sess38.posts}) == 5,
       f'the token rotates and is threaded through (pts={[p["pt"][:6] for p in _sess38.posts]})')
report(any('turboplays.click' in u for u in _sess38.seen),
       'the inline player page was fetched for its media')

# If the embed page yields no media, the real player page is kept rather than
# an empty playlist row.
try:
    sg34.time = _T38Time
    sg34._make_session = lambda: (_T38Session({
        'https://sextb.net/jul-509-rm': _sp,
    }), 'test-stub')
    _res38b = sg34.grab_all_static('https://sextb.net/jul-509-rm')
finally:
    sg34.time = _real_time
    sg34._make_session = _real_session
# Every hoster is now found, so this scenario keeps all five player pages.
# The point is that none of them is replaced by a fabricated media URL.
report(_res38b['streams'][0] == _WANT_PLAYER and len(_res38b['streams']) == 5,
       f"no media anywhere -> all 5 player pages kept (got {len(_res38b['streams'])})")
report(all(not sg34._is_media_url(u) for u in _res38b['streams']),
       'and none is passed off as a media file')
report(not any('duq8bcrl' in u or 'dtscout' in u for u in _res38b['streams']),
       'no ad ever reaches the playlist')

# ── 39. sextb: the other hosters, and the API's not-found answer ─────────────
# The episode API is Turnstile-gated. Without a solved token it answers
# {"src": "https://sextb.net/not-found"} -- a 404 page that reached the
# playlist as a stream. The other hosters (SW/PM/DD/FL/US/PP) are therefore
# only reachable by clicking their buttons, so grab_all always runs the
# click-through and merges it with the static result.
report(not sg34._looks_like_player('https://sextb.net/not-found'),
       'sextb.net/not-found is not accepted as a player')
report(sg34._looks_like_player('https://sextb.net/e/jul-509-rm'),
       'a real sextb embed still is')

for _label39, _body39, _want39 in [
    ('the API\'s not-found answer', {'src': 'https://sextb.net/not-found'}, None),
    ('a real player src',           {'src': 'https://doodstream.com/e/abc'}, 'https://doodstream.com/e/abc'),
    ('an ad src',                   {'src': 'https://t.dtscout.com/idg/?su=zz'}, None),
    ('an escaped real src',         {'src': 'https://doodstream.com/e/a?a=1&amp;b=2'},
                                    'https://doodstream.com/e/a?a=1&b=2'),
]:
    _g39 = sg34._fetch_episode_stream('16934905', '4124790', 'https://sextb.net/jul-509-rm',
                                      _T35Session(_T35Resp(ctype='application/json', json_data=_body39)))
    report(_g39 == _want39, f'api src {_label39} -> {_want39!r} (got {_g39!r})')

# grab_all must merge both halves, dedupe, and resolve the player pages the
# click-through brings back.
_real_static = sg34.grab_all_static
_real_pw     = sg34.grab_all_playwright
_real_sess   = sg34._make_session
_T39_MEDIA = {
    'https://turboplays.click/t/AAA': '<script>f:"https://cdn.example.net/a/index.m3u8";</script>',
    'https://doodstream.com/e/BBB':   '<script>f:"https://cdn.example.net/b/index.m3u8";</script>',
    'https://streamwish.com/e/CCC':   '<script>f:"https://cdn.example.net/c/index.m3u8";</script>',
}
try:
    sg34.grab_all_static = lambda u: {
        'title': 'JUL-509-RM', 'streams': ['https://cdn.example.net/a/index.m3u8']}
    sg34.grab_all_playwright = lambda u, visible=False: {
        'title': 'JUL-509-RM',
        'streams': ['https://turboplays.click/t/AAA',      # the initial player, again
                    'https://doodstream.com/e/BBB',        # DD
                    'https://streamwish.com/e/CCC']}       # SW
    sg34._make_session = lambda: (_T37Session(_T39_MEDIA), 'test-stub')
    _r39 = sg34.grab_all('https://sextb.net/jul-509-rm')
finally:
    sg34.grab_all_static = _real_static
    sg34.grab_all_playwright = _real_pw
    sg34._make_session = _real_sess

report(_r39['streams'] == ['https://cdn.example.net/a/index.m3u8',
                           'https://cdn.example.net/b/index.m3u8',
                           'https://cdn.example.net/c/index.m3u8'],
       f'grab_all merges the static hit with the clicked hosters (got {_r39["streams"]})')
report(_r39['title'] == 'JUL-509-RM', 'grab_all keeps the title')

# A dead click-through must not lose the static result.
try:
    sg34.grab_all_static = lambda u: {'title': 'T', 'streams': ['https://cdn.example.net/a/index.m3u8']}
    sg34.grab_all_playwright = lambda u, visible=False: (_ for _ in ()).throw(RuntimeError('no browser'))
    _r39b = sg34.grab_all('https://sextb.net/jul-509-rm')
finally:
    sg34.grab_all_static = _real_static
    sg34.grab_all_playwright = _real_pw
report(_r39b['streams'] == ['https://cdn.example.net/a/index.m3u8'],
       f'a failed click-through still returns the static stream (got {_r39b["streams"]})')

# ── 40. sextb: artwork is not a player, and the click needs a real browser ───
# The API answered "https://sextb.net/images/actor/amateur.jpg" for ppbd-321-rm
# and bkd-342-rm. sextb.net is on the player allowlist for its /e/ embeds, so
# every other file on that host passed too and the 404 artwork became a
# playlist row.
for _u40, _w40 in [
    ('https://sextb.net/images/actor/amateur.jpg',              False),
    ('https://sextb.net/images/icons/android-icon-192x192.png', False),
    ('https://sextb.net/css/site.css',                          False),
    ('https://sextb.net/js/sextb.js',                           False),
    ('https://sextb.net/e/jul-509-rm',                          True),
    ('https://turboplays.click/t/6a80cae62b911',                True),
    ('https://cdn.example.net/v/movie.mp4',                     True),
    ('https://cdn.example.net/hls/x/master.m3u8',               True),
]:
    report(sg34._looks_like_player(_u40) is _w40, f'non-media test {_u40[:46]!r} -> {_w40}')

# The click-through must run in the user's installed browser: sextb's buttons
# only resolve once Turnstile has issued a token, and Turnstile refuses
# bundled headless Chromium. javdock_grab established this for Cloudflare.
_src40 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'sextb_grab.py'), encoding='utf-8').read()
report("globals().get('BROWSER_EXECUTABLE')" in _src40,
       'the click-through honours the app-supplied browser')
report('launch_persistent_context(' in _src40, 'and uses a persistent profile')
report("ignore_default_args=['--enable-automation']" in _src40,
       'and hides navigator.webdriver from Turnstile')
report('headless=False' in _src40, 'and runs headed, which Turnstile requires')
report('/api/episode/' in _src40 and 'API-CLICK' in _src40,
       'and logs the click-time API response')
report('pw.chromium.launch(' not in _src40, 'the bare headless launch is gone')

# _find_local_browser must degrade to "" rather than raise where no browser
# exists (this sandbox), and must prefer the app-supplied path.
_fb40 = sg34._find_local_browser()
report(isinstance(_fb40, str), f'_find_local_browser returns a str without raising (got {_fb40!r})')
_prev40 = sg34.__dict__.get('BROWSER_EXECUTABLE')
try:
    sg34.BROWSER_EXECUTABLE = __file__   # any existing file stands in
    report(sg34._find_local_browser() == __file__, 'the app-supplied browser wins when it exists')
    sg34.BROWSER_EXECUTABLE = '/nonexistent/brave.exe'
    report(sg34._find_local_browser() != '/nonexistent/brave.exe',
           'a missing override is ignored rather than returned')
finally:
    if _prev40 is None:
        sg34.__dict__.pop('BROWSER_EXECUTABLE', None)
    else:
        sg34.BROWSER_EXECUTABLE = _prev40

# ── 41. sextb: accept any hoster the endpoint returns ────────────────────────
# TB/DD/FL resolved and SW/PM/US/PP did not ("decrypted 198 chars, no player").
# The cause was the player allowlist: those four sit on hosts nobody has
# catalogued yet. But a decrypted /ajax/player response is not a page full of
# decoys -- it is authenticated by the rotating token and holds exactly the
# hoster that was clicked. So no allowlist applies there; only ads, the trailer
# and non-media assets are still refused.
for _label41, _frag41, _want41 in [
    ('unknown host, absolute',     '<iframe src="https://brand-new-host.xyz/v/abc"></iframe>',
     'https://brand-new-host.xyz/v/abc'),
    ('protocol-relative unknown',  '<iframe src="//somehost.click/e/abc123"></iframe>',
     'https://somehost.click/e/abc123'),
    ('video tag',                  '<video src="https://cdn.x.net/v/f.mp4"></video>',
     'https://cdn.x.net/v/f.mp4'),
    ('entity-escaped',             '<iframe src="https://h.example/e/a?a=1&amp;b=2"></iframe>',
     'https://h.example/e/a?a=1&b=2'),
    ('known host still works',     '<iframe src="https://ryderjet.com/v/abc?poster=x"></iframe>',
     'https://ryderjet.com/v/abc?poster=x'),
    ('the trailer is refused',     '<iframe src="https://trailerhg.xyz/e/abc"></iframe>', None),
    ('an ad is refused',           '<iframe src="//duq8bcrl.xyz/api/spots/1?p=1"></iframe>', None),
    ('a dtscout tracker is refused','<iframe src="https://t.dtscout.com/idg/?su=zz"></iframe>', None),
    ('an image is refused',        '<img src="https://sextb.net/images/actor/a.jpg">', None),
    ('empty fragment',             '', None),
]:
    _g41 = sg34._extract_player_from_fragment(_frag41)
    report(_g41 == _want41, f'fragment {_label41} -> {_want41!r} (got {_g41!r})')

# The whole grab now resolves all six hosters even though four are on hosts
# that appear on no list.
_T41_HOSTERS = dict(_T38_HOSTERS)
_T41_HOSTERS['4124770'] = 'https://streamwish-new.example/e/sw9'   # unknown host
_T41_HOSTERS['4128035'] = '//unknowncdn.example/e/pm9'             # protocol-relative
_real_static41, _real_sess41, _real_time41 = sg34.grab_all_static, sg34._make_session, sg34.time
# Each hoster must serve its own manifest, or the dedupe correctly collapses
# them into one and the test measures nothing.
_T41_PAGES = {'https://sextb.net/jul-509-rm': _sp}
for _n41, _u41 in enumerate(sorted(_T41_HOSTERS.values())):
    _abs41 = _u41 if _u41.startswith('http') else 'https:' + _u41
    _T41_PAGES[_abs41] = (
        '<script>f:"https:\\/\\/cdn.example.net\\/h%d\\/index.m3u8";</script>' % _n41)
try:
    sg34.time = _T38Time
    _T38_HOSTERS.update(_T41_HOSTERS)
    sg34._make_session = lambda: (_T38Session(_T41_PAGES), 'test-stub')
    _r41 = sg34.grab_all_static('https://sextb.net/jul-509-rm')
finally:
    sg34.time = _real_time41
    sg34._make_session = _real_sess41
report(len(_r41['streams']) == 5,
       f"every hoster resolves regardless of its host (got {len(_r41['streams'])}: {_r41['streams']})")
report(not any('trailerhg' in u or 'dtscout' in u or u.endswith('.jpg') for u in _r41['streams']),
       'and no trailer, tracker or artwork slips through')

# ── 42. sextb: the SW and US hosters, from DevTools ──────────────────────────
# The user read the live DOM after each click. SW is an iframe on
# audinifer.com and US is an iframe on player.upn.one whose player carries a
# direct master.m3u8. Both were being dropped by the player allowlist.
_T42_SW = ('<iframe width="100%" height="100%" '
           'src="https://audinifer.com/e/9ok8hstlbxde?poster=https://cdn001.imggle.net/cover-player.jpg" '
           'frameborder="0" scrolling="no" allowtransparency="" allowfullscreen=""></iframe>')
_T42_US = ('<iframe width="100%" height="100%" src="https://player.upn.one/e/6a587ecd" '
           'frameborder="0" scrolling="no" allowtransparency="" allowfullscreen=""></iframe>')
report(sg34._extract_player_from_fragment(_T42_SW) ==
       'https://audinifer.com/e/9ok8hstlbxde?poster=https://cdn001.imggle.net/cover-player.jpg',
       'the SW fragment resolves to audinifer.com')
report(sg34._extract_player_from_fragment(_T42_US) == 'https://player.upn.one/e/6a587ecd',
       'the US fragment resolves to player.upn.one')
# And the second hop, which follows nested iframes by allowlist, must know them.
for _t42 in ('audinifer', 'upn.one'):
    report(_t42 in sg34.PLAYER_HOST_TOKENS, f'{_t42} is on the player allowlist for the second hop')
report(sg34._looks_like_player('https://audinifer.com/e/9ok8hstlbxde'),
       'audinifer passes the allowlist')
report(sg34._looks_like_player('https://player.upn.one/e/6a587ecd'),
       'player.upn.one passes the allowlist')

# upn.one serves a direct manifest in a <source> tag; the scrape must find it.
_T42_UPN = ('<video crossorigin preload src="blob:https://player.upn.one/6a587ecd">'
            '<source src="https://player.upn.one/hlsmod/p16-ad-site-sign-sg.tiktokcdn.com/'
            'wFpgYVKHQJOWIbl-nmGwRA/ipt/j3tyqqon/h5r9ag/tt/master.m3u8?v=1766826492" '
            'type="application/x-mpegurl" data-vds></video>')
report(sg34._scrape_media_from_html(_T42_UPN) ==
       ['https://player.upn.one/hlsmod/p16-ad-site-sign-sg.tiktokcdn.com/'
        'wFpgYVKHQJOWIbl-nmGwRA/ipt/j3tyqqon/h5r9ag/tt/master.m3u8?v=1766826492'],
       'the upn.one master.m3u8 is scraped out of the <source> tag')

# End to end: six buttons, four different hoster families, all resolved.
_T42_HOSTERS = {
    '4124790': 'https://turboplays.click/t/6a80cae62b911?poster=https://cdn001.imggle.net/cover-player.jpg',
    '4124770': 'https://audinifer.com/e/9ok8hstlbxde?poster=https://cdn001.imggle.net/cover-player.jpg',
    '4128035': 'https://player.upn.one/e/6a587ecd',
    '4124826': 'https://dsvplay.com/e/99dhh6q0eo5b?c_poster=https://cdn001.imggle.net/cover-player.jpg',
    '4124772': 'https://ryderjet.com/v/tkqbo77j1737?poster=https://cdn001.imggle.net/cover-player.jpg',
}
_T42_PAGES = {'https://sextb.net/jul-509-rm': _sp,
              'https://player.upn.one/e/6a587ecd': _T42_UPN}
for _n42, _u42 in enumerate(sorted(_T42_HOSTERS.values())):
    _T42_PAGES.setdefault(_u42, '<script>f:"https:\\/\\/cdn.example.net\\/f%d\\/index.m3u8";</script>' % _n42)
_rs42, _rm42, _rt42 = sg34.grab_all_static, sg34._make_session, sg34.time
try:
    sg34.time = _T38Time
    _T38_HOSTERS.clear(); _T38_HOSTERS.update(_T42_HOSTERS)
    sg34._make_session = lambda: (_T38Session(_T42_PAGES), 'test-stub')
    _r42 = sg34.grab_all_static('https://sextb.net/jul-509-rm')
finally:
    sg34.time = _rt42; sg34._make_session = _rm42
report(len(_r42['streams']) == 5,
       f"all five hoster families resolve (got {len(_r42['streams'])}: {_r42['streams']})")
report(any('master.m3u8' in u and 'upn.one' in u for u in _r42['streams']),
       'including the upn.one manifest')

# ── 43. placeholder schemes are not media candidates ─────────────────────────
# playmate.to's capture began MEDIA_URL::javascript:false, the probe rejected
# every candidate, and the promote-an-unverified-capture fallback handed
# javascript:false to mpv as a playback_url -- "Cannot open file
# 'C:\\Users\\...\\javascript:false'" -- looping the load-failed ladder several
# times per link. urlparse('javascript:false') has no netloc and a path of
# 'false', so neither the host-token nor the suffix test could catch it.
import ast as _ast43
_main43 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py'),
               encoding='utf-8').read()
_tree43 = _ast43.parse(_main43)
_fn43 = None
_attrs43 = {}
for _node43 in _ast43.walk(_tree43):
    if isinstance(_node43, _ast43.FunctionDef) and _node43.name == '_capture_candidate_is_clearly_not_media':
        _fn43 = _node43
    if isinstance(_node43, _ast43.Assign):
        for _t43 in _node43.targets:
            if isinstance(_t43, _ast43.Name) and _t43.id in (
                    '_NON_MEDIA_HOST_TOKENS', '_NON_MEDIA_URL_SUFFIXES'):
                _attrs43[_t43.id] = _ast43.literal_eval(_node43.value)
report(_fn43 is not None, 'found the real _capture_candidate_is_clearly_not_media')
report(len(_attrs43) == 2, f'lifted both class attribute tuples (got {sorted(_attrs43)})')

class _T43Stub:
    _NON_MEDIA_HOST_TOKENS = _attrs43.get('_NON_MEDIA_HOST_TOKENS', ())
    _NON_MEDIA_URL_SUFFIXES = _attrs43.get('_NON_MEDIA_URL_SUFFIXES', ())
    exec(compile(_ast43.Module(body=[_fn43], type_ignores=[]), '<lifted>', 'exec'))

_s43 = _T43Stub()
for _u43, _w43 in [
    ('javascript:false',                                                    True),
    ('javascript:;',                                                        True),
    ('about:blank',                                                         True),
    ('data:text/html;base64,AAAA',                                          True),
    ('blob:https://turboplays.click/8e8069c3-08ef-41d3-b709-8a9f',          True),
    ('file:///C:/x.mp4',                                                    True),
    ('//cdn.example.net/v/movie.mp4',                                       False),  # protocol-relative is real
    ('https://cdn.example.net/v/movie.mp4',                                 False),
    ('https://hanerix.com/stream/abc/def/1/2/master.m3u8',                  False),
    ('/stream/9g6WyL4AylslzsFPhdAYuA/x/1/2/master.m3u8',                    False),
    ('',                                                                    False),
]:
    report(_s43._capture_candidate_is_clearly_not_media(_u43) is _w43,
           f'not-media {_u43[:52]!r} -> {_w43}')

# And the promotion path must actually consult it.
report('_capture_candidate_is_clearly_not_media(c)' in _main43,
       'the promote-unverified fallback still filters through it')

# ── 44. sextb hosters: source page, short ads, manifests ────────────────────
# A field log on sextb.net/546erofv-391 showed three separate failures:
#   * the browser's own source page promoted as the playback URL
#     (playback_url=https://playmate.to/embed/o9Mwa5Cd5r6es, then
#     https://player.upn.one/#6k6l3x) — mpv "Failed to recognize file
#     format", three rungs of the load-failed ladder each time;
#   * a 30-second advert promoted as the film —
#     MEDIA_DUR::https://video.sacdnssedge.com/video/ol_...mp4|30|854|480
#     followed by VERIFIED_MEDIA:: on the same URL, which earned the +6
#     "duration-verified" bonus and beat every real candidate;
#   * every failing hoster sitting out the whole ~120s capture window.
import ast as _ast44
_main44 = _tree43  # the parsed main.py tree from section 43
_lift44 = {}
_fn44 = {}
for _node44 in _ast44.walk(_main44):
    if isinstance(_node44, _ast44.Assign):
        for _t44 in _node44.targets:
            if isinstance(_t44, _ast44.Name) and _t44.id in (
                    'VIDEO_EXTENSIONS', 'AUDIO_EXTENSIONS'):
                _lift44[_t44.id] = _ast44.literal_eval(_node44.value)
    if isinstance(_node44, _ast44.FunctionDef) and _node44.name in (
            '_capture_candidate_is_source_page', '_capture_line_is_manifest'):
        _fn44[_node44.name] = _node44
report(sorted(_lift44) == ['AUDIO_EXTENSIONS', 'VIDEO_EXTENSIONS'],
       f'lifted the real extension tuples (got {sorted(_lift44)})')
report(sorted(_fn44) == ['_capture_candidate_is_source_page', '_capture_line_is_manifest'],
       f'found both new helpers in main.py (got {sorted(_fn44)})')

_g44 = {'urlparse': urlparse,
        'VIDEO_EXTENSIONS': _lift44['VIDEO_EXTENSIONS'],
        'AUDIO_EXTENSIONS': _lift44['AUDIO_EXTENSIONS']}
_g44.update(_LIFT_HELPERS)

class _T44Stub:
    VIDEO_EXTENSIONS = _lift44['VIDEO_EXTENSIONS']
    AUDIO_EXTENSIONS = _lift44['AUDIO_EXTENSIONS']
    # The real canonicalizer is a 200-line method with its own cache and
    # PyQt-dependent callers; it is stubbed here as "lowercase + strip a
    # trailing slash", which is what it does for these two URLs (the field
    # log shows the playlist key lowercased:
    # key='https://playmate.to/embed/sciowddkvvlug'). Everything this test
    # exercises — the extension carve-out and the comparison — is real.
    @staticmethod
    def _canonicalize_remote_source_url(u):
        u = str(u or '').strip().lower()
        return u[:-1] if u.endswith('/') else u
    @staticmethod
    def _sanitize_url(u):
        return str(u or '').strip()
exec(compile(_ast44.Module(body=[_fn44['_capture_candidate_is_source_page']],
                           type_ignores=[]), '<lifted>', 'exec'), _g44)
_T44Stub._capture_candidate_is_source_page = _g44['_capture_candidate_is_source_page']
_s44 = _T44Stub()

# (candidate, source_url, expected "is the source page")
for _c44, _src44, _w44 in [
    ('https://playmate.to/embed/o9Mwa5Cd5r6es', 'https://playmate.to/embed/o9Mwa5Cd5r6es', True),
    ('https://player.upn.one/#6k6l3x',          'https://player.upn.one/#6k6l3x',          True),
    ('https://playmate.to/embed/O9MWA5CD5R6ES', 'https://playmate.to/embed/o9Mwa5Cd5r6es', True),
    ('https://hglink.to/e/qws3puy5u0ec?poster=https://cdn001.imggle.net/cover-player.jpg',
     'https://hglink.to/e/qws3puy5u0ec?poster=https://cdn001.imggle.net/cover-player.jpg', True),
    # a different URL on the same host is NOT the source page
    ('https://srv1-2.plauymito.live/hls/g6nj1Z4FFkanYnUPa69J5oeEJrTMIXLh/master.txt',
     'https://playmate.to/embed/o9Mwa5Cd5r6es', False),
    ('https://cdn1.turboviplay.com/data3/6aa9ecba0f385/6aa9ecba0f385.m3u8',
     'https://sextb.net/546erofv-391', False),
    # when the source URL IS the file, keep it
    ('https://cdn.example.net/v/movie.mp4', 'https://cdn.example.net/v/movie.mp4', False),
    ('https://cdn.example.net/v/index.m3u8', 'https://cdn.example.net/v/index.m3u8', False),
    # degenerate inputs
    ('', 'https://playmate.to/embed/x', False),
    ('https://playmate.to/embed/x', '', False),
]:
    report(_s44._capture_candidate_is_source_page(_c44, _src44) is _w44,
           f'source-page {_c44[:46]!r} -> {_w44}')

# _capture_line_is_manifest is a real staticmethod — call it unbound.
_m44 = _T44Stub._capture_line_is_manifest = None
_g44m = dict(_g44)
exec(compile(_ast44.Module(body=[_fn44['_capture_line_is_manifest']],
                           type_ignores=[]), '<lifted>', 'exec'), _g44m)
class _T44M:
    _capture_line_is_manifest = staticmethod(_g44m['_capture_line_is_manifest'])
for _l44, _w44 in [
    ('MEDIA_URL::https://cdn1.turboviplay.com/data3/6aa9ecba0f385/6aa9ecba0f385.m3u8', True),
    ('VOE_M3U8::https://g271.turbosplayer.com/file/4cad7c7a/master.m3u8', True),
    ('MEDIA_URL::https://srv1-2.plauymito.live/hls/g6nj1Z4FFkanYnUPa69J5oeEJrTMIXLh/master.txt', True),
    ('MEDIA_URL::https://54pkdcyxbsxbermn.meadowbrookcreativeworks.cfd/vpusxz6e9t3q/hls3/01/14957/buun813z5jka_n/master.txt', True),
    ('MEDIA_URL::https://z6v2p9a8.bkcdn.net/library/984100/466b4fc1.mp4', False),
    ('MEDIA_URL::javascript:false', False),
    ('MEDIA_URL::/assets/index-DqFBtoPY.js', False),
    ('PAGE_TITLE::Loading...', False),
    ('', False),
]:
    report(_T44M._capture_line_is_manifest(_l44) is _w44,
           f'manifest-line {_l44[:52]!r} -> {_w44}')

# Wiring assertions — the two ranking blocks and the ladder. These read the
# source rather than executing it (the blocks live inside 400-line methods
# that need PyQt); each one names a line the field log proved wrong.
_src44 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py'),
              encoding='utf-8').read()
report(_src44.count('if not (0.1 <= _measured_duration(c) < 45)') == 2,
       'both promote-an-unverified fallbacks now cut ads at 45s')
report(_src44.count('if 0.1 <= _m_dur < 45:') == 2,
       'both probe loops now cut ads at 45s')
report('and not (0.1 <= _measured_duration(url) < 45.0)' in _src44
       and _src44.count('and not (0.1 <= _measured_duration(url) < 45.0)') == 2,
       'both "verified" bonuses are cancelled by a sub-45s measurement')
report('0.1 <= _measured_duration(c) < 20' not in _src44
       and '0.1 <= _m_dur < 20' not in _src44,
       'no 20-second ad ceiling left behind')
report(_src44.count('self._capture_candidate_is_source_page(c, ') >= 4,
       'the source-page filter is wired into both ranking blocks',
       str(_src44.count('self._capture_candidate_is_source_page(c, ')))
# Two of those four passed a bare `source_url` that no enclosing scope bound --
# the page URL is the parameter `page_url` there -- so the filter raised
# NameError instead of filtering, and nothing caught it. Section 69 is what
# keeps that from happening again.
report(_src44.count('self._capture_candidate_is_source_page(c, page_url)') == 2
       and _src44.count('self._capture_candidate_is_source_page(c, source_url)') == 2,
       'and each block passes the page URL it actually has in scope')
report("manifest captured and capture idle, closing browser" in _src44,
       'the idle-after-manifest browser close exists')
report("_pw_manifest_at is not None\n                            and _now - _pw_last_line_at > 12" in _src44,
       'that close is gated on 12s of capture silence')

# ── 45. one browser, not two, when headless already saw the page ────────────
# The headless-first ladder added last round doubled the browser count on
# every failing sextb hoster. The field log shows both engines running for
# the same URL and returning the same capture:
#   BROWSER_UA::... HeadlessChrome/153.0.0.0 ...   <- run 1, saw master.txt
#   BROWSER_UA::... Chrome/153.0.0.0 ...           <- run 2, saw master.txt
#   [BROWSER_CLICK] killed capture subprocess (manifest captured ...) x2
# The second window bought nothing, which is the "browser opens many time"
# complaint. Retry headed only when headless saw nothing at all.
_fn45 = None
for _node45 in _ast44.walk(_tree43):
    if (isinstance(_node45, _ast44.FunctionDef)
            and _node45.name == '_browser_retry_needs_headed'):
        _fn45 = _node45
report(_fn45 is not None, 'found _browser_retry_needs_headed in main.py')
_g45 = {}
_g45.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_fn45], type_ignores=[]), '<lifted>', 'exec'), _g45)
_r45 = staticmethod(_g45['_browser_retry_needs_headed'])
for _n45, _w45 in [
    (0, True),      # blocked before the page said anything -> try headed
    (None, True),
    ('', True),
    (1, False),     # saw something -> headed sees the same thing
    (11, False),    # playmate.to's 11 candidates
    (18, False),    # hglink.to's capture
    (-1, True),
]:
    report(_r45.__func__(_n45) is _w45, f'needs-headed({_n45!r}) -> {_w45}')

_src45 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py'),
              encoding='utf-8').read()
report('self._browser_retry_needs_headed(' in _src45,
       'the generic ladder consults the helper')
report('_last_browser_click_media_lines = sum(' in _src45,
       'the capture records how many media lines it saw')
report("""            self._last_browser_click_media_lines = 0
            resolved = self._resolve_stream_via_browser_click(
                source_url, headless=True,""" in _src45,
       'the counter is reset before each headless attempt')

# And the two fixes that the same field log confirmed firing.
report('f"[BROWSER_CLICK] dropped {len(_self_pages)} candidate(s) "' in _src45
       and 'f"that are the source page itself: {_self_pages[0][:120]}"' in _src45,
       'source-page drop message still present (log confirms it fired)')
report('manifest captured and capture idle, closing browser' in _src45,
       'idle-after-manifest close still present (log confirms it fired)')

# ── 46. the .txt-disguised HLS master (sextb hoster family) ─────────────────
# hglink.to and playmate.to both hand their stream over as master.txt. A user
# fetched one directly and it is ordinary cleartext HLS:
#     #EXTM3U
#     #EXT-X-VERSION:6
#     #EXT-X-STREAM-INF:BANDWIDTH=1696988,...,RESOLUTION=1920x1080
#     index_avc_1080p.txt
# Two separate things stopped it reaching mpv:
#   * media_candidates[:12] truncated it away — in hglink's capture it is
#     line 18, behind 17 player/ad scripts;
#   * nothing recognised .txt as a playlist, so the proxy passed the body
#     through unrewritten and the probe refused it.
# watchstreamhd's /cdn/hls/<id>/master.txt looks the same but is AES
# ciphertext ("\m3\...") and must stay excluded.
_fns46 = {}
for _node46 in _ast44.walk(_tree43):
    if isinstance(_node46, _ast44.FunctionDef) and _node46.name in (
            '_is_disguised_hls_manifest', '_browser_capture_candidate_window'):
        _fns46[_node46.name] = _node46
report(sorted(_fns46) == ['_browser_capture_candidate_window', '_is_disguised_hls_manifest'],
       f'found both new helpers (got {sorted(_fns46)})')
_g46 = {'urlparse': urlparse, 're': re}
_g46.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_fns46['_is_disguised_hls_manifest']],
                           type_ignores=[]), '<lifted>', 'exec'), _g46)
_dm46 = _g46['_is_disguised_hls_manifest']

for _u46, _w46 in [
    # playmate.to -> plauymito.live
    ('https://srv1-2.plauymito.live/hls/g6nj1Z4FFkanYnUPa69J5oeEJrTMIXLh/master.txt', True),
    # hglink.to -> auronetworkdesign.space (both captures from the field log)
    ('https://g4vsrqvtrj.auronetworkdesign.space/As4sNGJK0nXh/hls3/01/14959/x0o069k2qb38_o/master.txt', True),
    ('https://WzrlxFlI3sZHGR0.auronetworkdesign.space/As4sNGJK0nXh/hls3/01/14959/x0o069k2qb38_o/master.txt', True),
    ('https://2goita23a7njj.meadowlarkculinaryarts.space/dwjdqozeoybz/hls3/01/14950/xqqik7ubeszl_n/master.txt', True),
    # the variant playlist named inside that master
    ('https://srv1-2.plauymito.live/hls/g6nj1Z4FFkanYnUPa69J5oeEJrTMIXLh/index_avc_1080p.txt', True),
    ('https://x.auronetworkdesign.space/a/hls3/01/14959/x0o069k2qb38_o/index_avc_720p.txt', True),
    # watchstreamhd's ENCRYPTED master must stay out
    ('https://watchstreamhd.com/cdn/hls/4f0a1b2c3d4e5f60718293a4b5c6d7e8/master.txt', False),
    # not playlists
    ('https://cdn1.turboviplay.com/data3/68159c8f30605/68159c8f30605.m3u8', False),
    ('https://x.auronetworkdesign.space/a/hls3/01/14959/x/seg-1-v1-a1.ts', False),
    ('https://x.example.net/assets/player-core.txt', False),
    ('https://x.example.net/hls/abc/notes.txt', False),
    ('https://vd.ambotalaing.com/r19XC1eW9QAwPN/147054', False),
    ('', False),
]:
    report(_dm46(_u46) is _w46, f'disguised-manifest {_u46[:60]!r} -> {_w46}')

# the candidate window: hglink's real capture, master.txt at index 17
_hg46 = [
    'https://mc.yandex.ru/metrika/tag_phono.js',
    'https://www.googletagmanager.com/gtag/js?id=G-E2BG6CPV2J',
    'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1',
    'https://vibuxer.com/player/jw8/vast.js?v=32',
    'https://mc.yandex.ru/metrika/tag.js',
    '/js/jquery.min.js', '/js/xupload.js', '/js/jquery.cookie.js',
    'https://www.googletagmanager.com/gtag/js?id=G-2TL7NH453R',
    'https://www.googletagmanager.com/gtag/js?id=G-E2BG6CPV2J',
    '//www.gstatic.com/cast/sdk/libs/sender/1.0/cast_framework.js',
    '//www.gstatic.com/eureka/clank/153/cast_sender.js',
    '/player/jw8/jwplayer.js?v=7', '/js/localstorage-slim.js',
    '/assets/jquery/hg-function.js?type=adult&u=152&v=20260807213908',
    'https://static.cloudflareinsights.com/beacon.min.js/v31edd6df95cf4e85bb4c19e7a9bdbcba1788362987495',
    'https://llvpn.com/tag.min.js',
    'https://g4vsrqvtrj.auronetworkdesign.space/As4sNGJK0nXh/hls3/01/14959/x0o069k2qb38_o/master.txt',
]
_g46w = dict(_g46)
_g46w['VideoPlayer'] = type('_VP46', (), {'_is_disguised_hls_manifest': staticmethod(_dm46)})
exec(compile(_ast44.Module(body=[_fns46['_browser_capture_candidate_window']],
                           type_ignores=[]), '<lifted>', 'exec'), _g46w)
_win46 = _g46w['_browser_capture_candidate_window'](_hg46)
report(len(_hg46) == 18, f'the fixture really is 18 lines (got {len(_hg46)})')
report(_hg46[-1] in _win46, 'the .txt master at index 17 survives the window')
report(len(_win46) == 13, f'window is first 12 + the manifest (got {len(_win46)})')
report(_win46[:12] == _hg46[:12], 'the first 12 are unchanged and in order')
report(len(_g46w['_browser_capture_candidate_window'](['a', 'b'])) == 2,
       'a short capture is passed through untouched')

_src46 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py'),
              encoding='utf-8').read()
report(_src46.count('self._browser_capture_candidate_window(media_candidates)') == 2,
       'both ranking blocks use the window helper (no [:12] left)')
report('media_candidates[:12]:' not in _src46,
       'the fixed 12-candidate truncation is gone')
report("""            if self._is_disguised_hls_manifest(url):
                return True""" in _src46,
       'the local proxy rewrites disguised manifests')
report('or self._is_disguised_hls_manifest(final_url)' in _src46,
       'the media probe accepts disguised manifests as HLS')
report(_src46.count('score += 6') >= 4 and 'self._is_disguised_hls_manifest(url):' in _src46,
       'both rankers score the disguised master')
report("if '/cdn/hls/' in path:" in _src46,
       "watchstreamhd's encrypted master is carved out")

# ── 47. sextb.net/fns-257: PNG-wrapped HLS, the site promo, the ad fallback ──
# A field log on https://sextb.net/fns-257 showed three separate failures:
#   * hglink resolved cleanly to
#     https://audinifer.com/stream/…/74605270/master.m3u8 and mpv opened it,
#     but reported "codec=PNG (Portable Network Graphics)" at 0x0 and then
#     sat on that single frame for the playlist's whole claim until
#     "[PLAYBACK] end-of-stream stall watchdog fired (7208240/7208240 ms,
#     no EOF) - advancing playlist". The same log shows the turbo CDN
#     serving exactly this disguise (…origin.image segments unwrapped by the
#     proxy) - the stream was applied DIRECT, so nothing stripped it.
#     Reported by the user as "hglink stuck at the end".
#   * player.upn.one's headless capture never started the player, so every
#     candidate was a script or an ad endpoint; the promote-unverified
#     fallback picked https://vd.ambotalaing.com/r19XC1eW9QAwPN/147054,
#     costing two "unrecognized file format" loads and an anti-bot decoy
#     round trip. Reported as "takes too long but doesnt work".
#   * the ladder then decoded the sextb page's OWN preview,
#     https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4,
#     and played it as the film. Reported as "played a 2 minute trailer".
report(sorted(v._SITE_PROMO_STEM_TOKENS)[0] == 'pr',
       f'the promo token table lifted from main.py: {sorted(v._SITE_PROMO_STEM_TOKENS)}')

# 47a. the exact promo URL from the log, plus the false positives it must not hit
for _u47, _w47 in [
    ('https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4', True),
    ('https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR_720p.mp4', True),
    ('https://cdn.faleno.net/x/FNS-257-pr.mp4', True),
    ('https://cdn.faleno.net/x/FNS-257_trailer.mp4', True),
    ('https://cdn.faleno.net/x/ure-078_preview.mp4', True),
    ('https://cdn.faleno.net/x/ure-078_TEASER_1080p.mp4', True),
    # real product codes: a promo token must END the stem, not sit inside it
    ('https://cdn.example.net/v/SOD-PR123.mp4', False),
    ('https://cdn.example.net/v/APR-001.mp4', False),
    ('https://cdn.example.net/v/MIDV-789_1080p.mp4', False),
    ('https://cdn.example.net/v/movie.mp4', False),
    ('https://cdn.example.net/v/480p_v2.mp4', False),
    ('https://cdn.example.net/v/index-v1-a1.m3u8', False),
    ('https://audinifer.com/stream/a/b/1789619971/74605270/master.m3u8', False),
    ('', False),
]:
    report(v._media_url_is_site_promo(_u47) is _w47,
           f'site promo? {os.path.basename(urlparse(_u47).path) or _u47!r} -> {_w47}')

# 47b. a promo is dropped for good; a teaser RENDITION keeps its fallback
_FALENO47 = 'https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4'
report(v._rank_real_media_candidates([_FALENO47], 'VOE-mirror') == [],
       'a page whose only candidate is the site promo yields nothing to play')
_mixed47 = v._rank_real_media_candidates(
    [_FALENO47, 'https://cdn.example.net/v/720p.mp4', 'https://cdn.example.net/v/1080p.mp4'],
    'VOE-mirror')
report(_FALENO47 not in _mixed47 and _mixed47[0].endswith('1080p.mp4'),
       f'promo dropped, best real rendition first: {[os.path.basename(u) for u in _mixed47]}')
_teaser47 = v._rank_real_media_candidates(
    ['https://cdn2.pvvstream.pro/x/tr_240p.mp4', 'https://cdn2.pvvstream.pro/x/tr_720p.mp4'],
    'VOE-mirror')
report([os.path.basename(u) for u in _teaser47] == ['tr_720p.mp4', 'tr_240p.mp4'],
       f'a teaser-only page still plays one (fallback preserved): '
       f'{[os.path.basename(u) for u in _teaser47]}')

# 47c. the promote-unverified fallback needs a media SHAPE, not a blocklist
_lift47, _fn47 = {}, {}
for _node47 in _ast44.walk(_main44):
    if isinstance(_node47, _ast44.Assign):
        for _t47 in _node47.targets:
            if isinstance(_t47, _ast44.Name) and _t47.id in (
                    'VIDEO_EXTENSIONS', 'AUDIO_EXTENSIONS'):
                _lift47[_t47.id] = _ast44.literal_eval(_node47.value)
    if isinstance(_node47, _ast44.FunctionDef) and _node47.name in (
            '_capture_candidate_has_media_shape', '_is_disguised_hls_manifest',
            '_is_hls_stream_url'):
        _fn47[_node47.name] = _node47
report(sorted(_fn47) == ['_capture_candidate_has_media_shape',
                         '_is_disguised_hls_manifest', '_is_hls_stream_url'],
       f'found the three lifted methods (got {sorted(_fn47)})')
_g47 = {'urlparse': urlparse, 'unquote': unquote, 're': re, 'os': os,
        'urljoin': urljoin,
        'VIDEO_EXTENSIONS': _lift47['VIDEO_EXTENSIONS'],
        'AUDIO_EXTENSIONS': _lift47['AUDIO_EXTENSIONS']}
_g47.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_fn47['_is_disguised_hls_manifest'],
                                 _fn47['_is_hls_stream_url'],
                                 _fn47['_capture_candidate_has_media_shape']],
                           type_ignores=[]), '<lifted47>', 'exec'), _g47)


class _T47Stub:
    _PLAYABLE_MEDIA_SUFFIXES = lift_attr('VideoPlayer', '_PLAYABLE_MEDIA_SUFFIXES')
    VIDEO_EXTENSIONS = _lift47['VIDEO_EXTENSIONS']
    AUDIO_EXTENSIONS = _lift47['AUDIO_EXTENSIONS']


for _n47 in ('_is_disguised_hls_manifest', '_is_hls_stream_url',
             '_capture_candidate_has_media_shape'):
    setattr(_T47Stub, _n47, staticmethod(_g47[_n47])
            if _n47 == '_is_disguised_hls_manifest' else _g47[_n47])
_s47 = _T47Stub()

for _u47, _w47 in [
    # the ad endpoint the field log actually promoted: no extension, no query
    ('https://vd.ambotalaing.com/r19XC1eW9QAwPN/147054', False),
    ('//excavatenearbywand.com/on.js', False),
    ('javascript:false', False),
    ('blob:https://playmate.to/9f1c-4d0e', False),
    # a protocol-relative capture is judged on its path, not thrown away
    ('//cdn.example.net/v/movie_720p.mp4', True),
    ('https://c.adsco.re/', False),
    ('https://displayvertising.com/O/z/rjquery.fn.gantt.min.js', False),
    # real shapes that MUST survive the same filter
    ('https://audinifer.com/stream/a/b/1789619971/74605270/master.m3u8', True),
    ('https://cdn1.turboviplay.com/data1/6aa5b7d3efb53/6aa5b7d3efb53.m3u8', True),
    ('https://uio1105mk.cloudatacdn.com/abc/k2xybd3hxr~B0OPg12FPJ'
     '?token=vst1053l3qx7pdi1ee8q9n79&expiry=1789534923', True),
    ('https://x.auronetworkdesign.space/a/hls3/01/14959/x/master.txt', True),
    ('https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4', True),
    ('https://www.porn00.org/get_file/3/4c682e0f/5000/5665/5665_720p.mp4/', True),
    ('', False),
]:
    report(_s47._capture_candidate_has_media_shape(_u47) is _w47,
           f'media shape? {_u47[:58]!r} -> {_w47}')

# 47d. PNG-wrapped HLS is detected through a master playlist, one hop down.
#      The chain mirrors the log: the turbo CDN served exactly these
#      …origin.image segments and the proxy unwrapped them; audinifer's
#      master was applied direct and mpv decoded the wrapper as codec=PNG.
_MASTER47 = ('#EXTM3U\n#EXT-X-VERSION:6\n'
             '#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480\n'
             'index-v1-a1.m3u8\n')
_PNG_VARIANT47 = ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n'
                  '#EXTINF:10.0,\n'
                  '202609125d0d05dbf1a8b8014630839b_tplv-d5opwmad15-ttam-origin.image\n'
                  '#EXTINF:10.0,\n'
                  '202609125d0db3ed33baf0634ea3a034_tplv-d5opwmad15-ttam-origin.image\n'
                  '#EXT-X-ENDLIST\n')
_TS_VARIANT47 = ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n'
                 '#EXTINF:10.0,\nseg-1-v1-a1.ts\n'
                 '#EXTINF:10.0,\nseg-2-v1-a1.ts\n#EXT-X-ENDLIST\n')
_TS_MASTER47 = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nindex-v1-a1.m3u8\n')
_MEDIA47 = {'/png/master.m3u8': _MASTER47,
            '/png/index-v1-a1.m3u8': _PNG_VARIANT47,
            '/ts/master.m3u8': _TS_MASTER47,
            '/ts/index-v1-a1.m3u8': _TS_VARIANT47,
            '/flat/index-v1-a1.m3u8': _TS_VARIANT47,
            '/png/master.txt': _MASTER47.replace('index-v1-a1.m3u8', 'index.txt'),
            '/png/index.txt': _PNG_VARIANT47}


class _Resp47:
    def __init__(self, url, body, status=200):
        self.url, self.status_code = url, status
        self._body = body

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class _FakeRequests47:
    calls = []

    def get(self, url, headers=None, stream=False, timeout=None,
            allow_redirects=True, **kw):
        _FakeRequests47.calls.append(url)
        body = _MEDIA47.get(urlparse(str(url)).path)
        if body is None:
            return _Resp47(url, b'', status=404)
        return _Resp47(url, body.encode('utf-8'))


class _T47FetchStub:
    _is_disguised_hls_manifest = staticmethod(_g47['_is_disguised_hls_manifest'])
    _is_hls_stream_url = _g47['_is_hls_stream_url']

    @staticmethod
    def _stream_request_headers(referer=None, extra=None):
        h = {'User-Agent': 'test'}
        if referer:
            h['Referer'] = referer
        return h


_g47f = {'urlparse': urlparse, 'unquote': unquote, 're': re, 'os': os,
         'urljoin': urljoin}
_g47f.update(_LIFT_HELPERS)
for _n47 in ('_hls_playlist_looks_like_decoy', '_hls_best_variant_url'):
    _node47b = next(n for n in _ast44.walk(_main44)
                    if isinstance(n, _ast44.FunctionDef) and n.name == _n47)
    exec(compile(_ast44.Module(body=[_node47b], type_ignores=[]),
                 f'<lifted-{_n47}>', 'exec'), _g47f)
    setattr(_T47FetchStub, _n47,
            staticmethod(_g47f[_n47]) if _n47 == '_hls_best_variant_url'
            else _g47f[_n47])
for _n47 in ('_fetch_hls_playlist_text',
             '_hls_manifest_ships_png_wrapped_segments',
             '_flag_png_wrapped_hls_for_proxy'):
    _node47c = next(n for n in _ast44.walk(_main44)
                    if isinstance(n, _ast44.FunctionDef) and n.name == _n47)
    exec(compile(_ast44.Module(body=[_node47c], type_ignores=[]),
                 f'<lifted-{_n47}>', 'exec'), _g47f)
    setattr(_T47FetchStub, _n47, _g47f[_n47])

import sys as _sys47
_saved_requests47 = _sys47.modules.get('requests')
_sys47.modules['requests'] = _FakeRequests47()
try:
    _f47 = _T47FetchStub()
    report(_f47._hls_playlist_looks_like_decoy(_PNG_VARIANT47) is True,
           'the real decoy test does flag the .image segment playlist')
    report(_f47._hls_playlist_looks_like_decoy(_TS_VARIANT47) is False,
           'the real decoy test does not flag a plain .ts playlist')
    _AUDINIFER47 = 'https://audinifer.com/png/master.m3u8'
    report(_f47._hls_manifest_ships_png_wrapped_segments(_AUDINIFER47) is True,
           'a MASTER whose variant ships PNG-wrapped segments is detected one hop down')
    _FakeRequests47.calls = []
    report(_f47._hls_manifest_ships_png_wrapped_segments(
        'https://x/ts/master.m3u8') is False,
        'a MASTER with ordinary .ts segments is left alone')
    report(_f47._hls_manifest_ships_png_wrapped_segments(
        'https://x/flat/index-v1-a1.m3u8') is False,
        'a flat .ts media playlist is left alone')
    report(_f47._hls_manifest_ships_png_wrapped_segments(
        'https://x/png/master.txt') is True,
        'the .txt-disguised master is followed too')
    report(_f47._hls_manifest_ships_png_wrapped_segments(
        'https://x/missing/master.m3u8') is False,
        'an unreachable manifest is reported as "not PNG-wrapped", not as an error')
    report(_f47._hls_manifest_ships_png_wrapped_segments('') is False,
        'an empty URL is safe')

    # 47e. the resolved-stream flag the apply path reads
    _info47 = {'playback_url': _AUDINIFER47,
               'content_type': 'application/vnd.apple.mpegurl',
               'source_url': 'https://sextb.net/fns-257'}
    _f47._flag_png_wrapped_hls_for_proxy(_info47)
    report(_info47.get('route_local_proxy') is True,
           'route_local_proxy set on a PNG-wrapped HLS result')
    _mp4_47 = {'playback_url': 'https://cdn.faleno.net/x/FNS-257_PR.mp4'}
    _f47._flag_png_wrapped_hls_for_proxy(_mp4_47)
    report('route_local_proxy' not in _mp4_47,
           'a plain MP4 result is never routed through the HLS proxy')
    _tsinfo47 = {'playback_url': 'https://x/ts/master.m3u8'}
    _f47._flag_png_wrapped_hls_for_proxy(_tsinfo47)
    report('route_local_proxy' not in _tsinfo47,
           'ordinary HLS is still played direct (no behaviour change)')
    report(_f47._flag_png_wrapped_hls_for_proxy(None) is None,
           'a non-dict result passes through untouched')
finally:
    if _saved_requests47 is None:
        _sys47.modules.pop('requests', None)
    else:
        _sys47.modules['requests'] = _saved_requests47

# 47f. the wiring: every one of these is a string only the fix produces
report("""                or (provider == 'browser_click'
                    and stream_info.get('route_local_proxy'))""" in _src46,
       'the apply path routes a flagged browser_click HLS through the proxy')
report("""                    else 'PNGHLS_PROXY'
                    if (provider == 'browser_click'
                        and stream_info.get('route_local_proxy'))""" in _src46,
       "and labels it PNGHLS_PROXY so the field log shows why")
report("""                try:
                    self._flag_png_wrapped_hls_for_proxy(value)
                except Exception:
                    pass
            return value""" in _src46,
       'every browser_click result passes the PNG-wrap check on its way out')
report(_src46.count('if self._capture_candidate_has_media_shape(c)') == 2,
       'both promote-unverified fallbacks (browser_click and mixdrop) apply it')
report('with no media shape (unverified fallback)' in _src46,
       'the fallback logs what it dropped')
report("""                if self._media_url_is_site_promo(candidate):
                    continue""" in _src46,
       "the VOE raw scan skips the site's own promo too")
report('return sorted(real or usable, key=self._media_url_height_hint' in _src46,
       'the ranking fallback restores teaser renditions, never promos')

# ── 48. mpv showing one still image where a film should be ───────────────────
# The audinifer master.m3u8 loaded and mpv reported
#   [mpv][perf] profile=base 0x0 fps=0.00 ... codec=PNG (Portable Network
#   Graphics) image ...
# then held that frame until
#   [PLAYBACK] end-of-stream stall watchdog fired (7208240/7208240 ms, no EOF)
# Nothing in the log said WHY, so there was no way to tell whether the proxy
# was failing to strip the wrappers or the segments were genuine decoy
# images. This names it, once per source, and is deliberately narrow enough
# that no real film can trip it.
# Both live on MpvMediaPlayerAdapter (the mpv backend), not VideoPlayer --
# they read mpv properties, which only that class has.
_cls48 = next(n for n in _ast44.walk(_main44)
              if isinstance(n, _ast44.ClassDef) and n.name == 'MpvMediaPlayerAdapter')
_fn48 = {n.name: n for n in _cls48.body
         if isinstance(n, _ast44.FunctionDef)
         and n.name in ('_still_image_video_signature', '_report_still_image_stream')}
report(sorted(_fn48) == ['_report_still_image_stream',
                         '_still_image_video_signature'],
       f'found both new helpers on MpvMediaPlayerAdapter (got {sorted(_fn48)})')
_g48 = {'IMAGE_EXTENSIONS': G['IMAGE_EXTENSIONS'], 'print': print, 're': re}
_g48.update(_LIFT_HELPERS)
# _still_image_video_signature now calls _ext_probe_path, which lives at
# module level in main.py. A lifted copy raises NameError the moment it
# reaches that line unless the helper is lifted with it.
_fn48e = next((n for n in _main44.body
               if isinstance(n, _ast44.FunctionDef)
               and n.name == '_ext_probe_path'), None)
if _fn48e is not None:
    from urllib.parse import urlsplit as _us48
    _g48['urlsplit'] = _us48
    exec(compile(_ast44.Module(body=[_fn48e], type_ignores=[]),
                 '<lifted48e>', 'exec'), _g48)
exec(compile(_ast44.Module(body=[_fn48['_still_image_video_signature'],
                                 _fn48['_report_still_image_stream']],
                           type_ignores=[]), '<lifted48>', 'exec'), _g48)

_AUD48 = ('https://audinifer.com/stream/6uPtfdlS6WEKqtNSbLZRZw/kjhhiuahiuhgihdf'
          '/1789619971/74605270/master.m3u8')
_TURBO48 = 'http://127.0.0.1:51707/hls/e639f991ec9fd7a2/6aa5b7d3efb53.m3u8?id=9687ba43b1556663'
_FALENO48 = 'https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4'
_H264_48 = 'H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10'


class _T48Stub:
    _IMAGE_VIDEO_CODECS = lift_attr('MpvMediaPlayerAdapter', '_IMAGE_VIDEO_CODECS')
    _still_image_video_signature = _g48['_still_image_video_signature']
    _report_still_image_stream = _g48['_report_still_image_stream']

    def __init__(self, codec='', w=0, h=0, src=''):
        self._props = {'video-codec': codec, 'width': w, 'height': h,
                       'duration': 7208240}
        self.lines = []
        self._source = self
        self._src = src

    def toString(self):
        return self._src

    def _get_mpv_property(self, name, default=None):
        return self._props.get(name, default)


_s48 = _T48Stub()
report(_s48._IMAGE_VIDEO_CODECS and 'png' in _s48._IMAGE_VIDEO_CODECS,
       f'the image-codec table lifted from main.py: {_s48._IMAGE_VIDEO_CODECS}')

# (codec, width, height, source, expected) - the real rows from the field log
for _codec48, _w48, _h48, _src48, _want48 in [
    # THE failure: audinifer, played direct, wrappers never stripped
    ('PNG (Portable Network Graphics)', 0, 0, _AUD48, True),
    ('png', 0, 0, _AUD48, True),
    # the same session's working streams must NOT be called images
    (_H264_48, 854, 480, _TURBO48, False),
    (_H264_48, 1280, 720, _TURBO48, False),
    (_H264_48, 3840, 2160, _FALENO48, False),
    # an image codec WITH real dimensions is a picture, not a broken stream
    ('PNG (Portable Network Graphics)', 1920, 1080, _AUD48, False),
    # old MJPEG video is real video - it must never be flagged. This is the
    # substring trap: 'jpeg' IS contained in 'mjpeg'.
    ('mjpeg', 0, 0, _AUD48, False),
    ('MJPEG', 0, 0, _AUD48, False),
    ('Motion JPEG', 0, 0, _AUD48, False),
    # 'jpeg' on its own is still an image
    ('jpeg', 0, 0, _AUD48, True),
    # a local file the user opened is not a remote stream
    ('PNG (Portable Network Graphics)', 0, 0, 'C:/Users/Mouad/photo.png', False),
    ('PNG (Portable Network Graphics)', 0, 0, '/home/user/photo.png', False),
    # a remote .png URL is an image request, not a film
    ('PNG (Portable Network Graphics)', 0, 0, 'https://cdn001.imggle.net/cover-player.jpg', False),
    # nothing to go on
    ('', 0, 0, _AUD48, False),
    ('PNG (Portable Network Graphics)', 0, 0, '', False),
]:
    _got48 = _s48._still_image_video_signature(_codec48, _w48, _h48, _src48)[0]
    report(_got48 is _want48,
           f'still image? {(_codec48 or "(none)")[:34]!r} {_w48}x{_h48} -> {_want48}')

# the reason string names the codec and the empty frame, for the field log
_ok48, _why48 = _s48._still_image_video_signature('PNG (Portable Network Graphics)', 0, 0, _AUD48)
report('png' in _why48 and '0x0' in _why48,
       f'the reason says what mpv actually reported: {_why48!r}')

# logged once per source, and never for a real film
import io as _io48, contextlib as _ctx48
_img48 = _T48Stub('PNG (Portable Network Graphics)', 0, 0, _AUD48)
_buf48 = _io48.StringIO()
with _ctx48.redirect_stdout(_buf48):
    _img48._report_still_image_stream()
    _img48._report_still_image_stream()
_out48 = _buf48.getvalue()
report(_out48.count('[PLAYBACK][STILL_IMAGE_STREAM]') == 1,
       f'a broken stream is named exactly once, not on every tick '
       f'({_out48.count("[PLAYBACK][STILL_IMAGE_STREAM]")} line(s))')
report(_AUD48[:60] in _out48 and '7208240' in _out48,
       'and the line carries the source and the bogus duration')
_film48 = _T48Stub(_H264_48, 854, 480, _TURBO48)
_buf48b = _io48.StringIO()
with _ctx48.redirect_stdout(_buf48b):
    _film48._report_still_image_stream()
report('[PLAYBACK][STILL_IMAGE_STREAM]' not in _buf48b.getvalue(),
       'a real H.264 stream logs nothing')

# wiring: the check is armed from mpv's file-loaded callback
report('QTimer.singleShot(700, self._report_still_image_stream)' in _src46,
       'the still-image check is armed on file-loaded')

# ── 49. Two defects the field console caught in the shipped build ────────────
# (a) C:\...\main.py:3035: DeprecationWarning: 'maxsplit' is passed as
#     positional argument  -- on EVERY file load, from the still-image check
#     added one commit earlier.
# (b) sextb's playmate row stopped playing FNS-257_PR.mp4 (correct) and
#     started playing the next advert instead:
#       [VOE-mirror] Trusting unprobed MP4: .../3aaimRZTQPQTwYuGELr8_s_sample_zen/_3
#       [REMOTE_PROXY][CFFI_UPSTREAM_403] .../_3000.mp4
#     three times. The promo marker is in a PATH segment there, not in the
#     filename, so the stem test waved it through.
_np49 = next(n for n in _ast44.walk(_main44)
             if isinstance(n, _ast44.FunctionDef)
             and n.name == '_still_image_video_signature')
_splits49 = [n for n in _ast44.walk(_np49)
             if isinstance(n, _ast44.Call)
             and isinstance(n.func, _ast44.Attribute)
             and n.func.attr == 'split'
             and isinstance(n.func.value, _ast44.Name)
             and n.func.value.id == 're']
report(bool(_splits49), f'found the re.split call (got {len(_splits49)})')
_positional49 = [c for c in _splits49 if len(c.args) > 2]
report(not _positional49,
       're.split passes maxsplit as a KEYWORD, so Python 3.13+ stays quiet')

_g49 = {'IMAGE_EXTENSIONS': G['IMAGE_EXTENSIONS'], 'print': print, 're': re}
_g49.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_np49], type_ignores=[]), '<lifted49>', 'exec'), _g49)


class _T49Stub:
    _IMAGE_VIDEO_CODECS = lift_attr('MpvMediaPlayerAdapter', '_IMAGE_VIDEO_CODECS')
    _still_image_video_signature = _g49['_still_image_video_signature']


_s49 = _T49Stub()
report(_s49._still_image_video_signature('PNG (Portable Network Graphics)', 0, 0,
                                         'https://vibuxer.com/stream/x/master.m3u8')[0],
       'the keyword form still detects the PNG case it was written for')

# The path-segment promo tokens
_paths49 = lift_attr('VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
report('sample' in _paths49 and 'pr' not in _paths49,
       f'the path token list covers "sample" but not the too-short "pr": {_paths49}')

_fn49 = next(n for n in _ast44.walk(_main44)
             if isinstance(n, _ast44.FunctionDef)
             and n.name == '_media_url_is_site_promo')
_g49b = {'os': os, 're': re, 'urlparse': urlparse}
_g49b.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_fn49], type_ignores=[]), '<lifted49b>', 'exec'), _g49b)


class _T49Promo:
    _SITE_PROMO_STEM_TOKENS = lift_attr('VideoPlayer', '_SITE_PROMO_STEM_TOKENS')
    _SITE_PROMO_PATH_TOKENS = lift_attr('VideoPlayer', '_SITE_PROMO_PATH_TOKENS')
    _SITE_PROMO_PATH_TOKENS = _paths49
    _media_url_is_site_promo = _g49b['_media_url_is_site_promo']


_pm49 = _T49Promo()
# The advert that actually played, and the one from the previous report
for _u49, _want49 in [
    ('https://cdn-dl.webstream.ne.jp/gigadlcdn/dl/'
     '3aaimRZTQPQTwYuGELr8_s_sample_zen/_3000.mp4', True),
    ('https://cdn.faleno.net/top/wp-content/uploads/2026/08/FNS-257_PR.mp4', True),
    # every stream that PLAYED in the same session must survive
    ('https://audinifer.com/stream/Q69OBFl1Tz9LqOSOgRXcVw/kjhhiuahiuhgihdf'
     '/1789625947/74532113/master.m3u8', False),
    ('https://vibuxer.com/stream/PbWMV0c6WpcvanDMKfY3cg/kjhhiuahiuhgihdf'
     '/1789626446/74510439/master.m3u8', False),
    ('https://yXqC9C2Vqj7VEPBl.acek-cdn.com/hls2/01/08595/fa58rsem6w0c_n'
     '/master.m3u8?t=e37w4J1M', False),
    ('https://wt4PjIIVE9AGjPL.dramiyos-cdn.com/hls2/01/08594/n8dyzufgpzut_n'
     '/master.m3u8?t=wj6UZIe5', False),
    ('https://cdn.turboviplay.com/data1/6aa3672b0129f/6aa3672b0129f.m3u8', False),
    ('https://hls2.turbosplayer.com/file/0caf27dc37fbd3d7ccd62ed1a836ceb7da2c2c8c'
     '/master.m3u8', False),
    ('https://ll288op.cloudatacdn.com/u5kj6csbt7plsdgge5y3ujqajikawvdnevpfwvwxd6d'
     'qmzgkl37q7y72krga/y92b4an3xq~ryLB36Hgmd?token=v9xay&expiry=1', False),
    ('https://www.porn00.org/get_file/3/4c682e0f1d135cdeb1a1a22466f240e0/5000'
     '/5665/5665_720p.mp4/?v-acctoken=MTAxNHwx', False),
    ('https://watchporn.to/get_file/9/fcf01fb76ec4bd596563b6b0f2e6627e/2000'
     '/2980/2980_720p.mp4/?v-acctoken=MTI3Nnw', False),
    # a real product code is still safe
    ('https://cdn.example.com/vids/SOD-PR123.mp4', False),
    ('https://cdn.example.com/vids/APR-001.mp4', False),
]:
    _got49 = _pm49._media_url_is_site_promo(_u49)
    report(_got49 is _want49,
           f'promo? {_want49}  {_u49.split("/")[-2] + "/" if _want49 else ""}'
           f'{_u49.split("/")[-1][:34]}')

# ── 50. The headless capture never clicked JW Player's play button ───────────
# sextb's playmate.to embed is JW Player 8:
#   MEDIA_URL::/assets/jw8/jwplayer.js
#   MEDIA_URL::/assets/jw8/googima.js
#   MEDIA_URL::/assets/js/player-core.min.js
# ...and then not one media request, ever, on two separate runs. JW draws its
# own display layer over the <video>, so its play button must be found BEFORE
# the bare element -- but _click_play returns on the FIRST selector that
# exists, and "video" was listed above every .jw-* selector. So on a JW page
# the capture clicked the raw media element (which JW passes through) and
# never reached the button. Same root cause as the user's report: "works
# perfectly in browser but doesnt in player even as a solo link".
_sel50_nodes = [
    n for n in _ast44.walk(_main44)
    if isinstance(n, _ast44.Assign)
    and any(getattr(tg, 'id', None) == '_play_selectors' for tg in n.targets)
]
report(len(_sel50_nodes) == 1,
       f'found exactly one _play_selectors list (got {len(_sel50_nodes)})')
_sel50 = list(_ast44.literal_eval(_sel50_nodes[0].value))
report(len(_sel50) >= 6, f'the selector list lifted from main.py: {_sel50}')

_idx50 = {s: i for i, s in enumerate(_sel50)}
report('.jw-icon-display' in _idx50,
       "JW 8's real display button is in the list")
report(_idx50.get('.jw-icon-display', 99) < _idx50['video'],
       f"and it is tried BEFORE the bare <video> "
       f"({_idx50.get('.jw-icon-display')} < {_idx50['video']})")
report(_idx50.get('.jw-display-icon-display', 99) < _idx50['video'],
       'as is the JW display-icon wrapper')
# The two fixes this ordering was originally protecting must survive.
report(_sel50[0] == '.vjs-big-play-button',
       f"video.js's big play button is still first ({_sel50[0]!r})")
report(_idx50['.play-overlay'] > _idx50['video']
       and _idx50['.player-overlay'] > _idx50['video'],
       'and the Mixdrop ad click-catchers are still LAST, so they can never '
       'navigate the page to an advert before the real button is pressed')

# ── 51. The capture never announced a .txt playlist ──────────────────────────
# playmate.txt (the page the user saved) settles why playmate produced
# nothing. The media URL is NOT in the HTML: the page carries only
#   window.__PM = { videoId: 233091, ... }
#   <meta property="og:image" content="https://srv1-2.plauymito.live/thumbnail/xoDtAGdof2iJA.jpg">
# and /assets/js/player-core.min.js builds the stream at runtime. That CDN
# renames its playlists to .txt -- the shape this file already documents at
# _capture_candidate_is_clearly_not_media:
#   srv1-2.plauymito.live/hls/<id>/master.txt
# but handle_request only matched '.m3u8' or a media file extension, so the
# one request that mattered was never announced at all: the capture reported
# jwplayer.js, googima.js, player-core.min.js and a wall of adverts, and
# nothing else. Two halves: announce it, and stop the media-shape gate from
# discarding it again.
report('elif _is_renamed_hls_playlist(req_url):' in _src46,
       'the capture now announces a renamed .txt playlist')
report('VideoPlayer._is_disguised_hls_manifest(req_url)' in _src46,
       'and it reuses the predicate the rest of the file already trusts '
       '(including the watchstreamhd carve-out) rather than a second copy')
report('if self._is_disguised_hls_manifest(url):' in _src46,
       'the media-shape gate lets a renamed playlist through')

_fn51 = next(n for n in _ast44.walk(_main44)
             if isinstance(n, _ast44.FunctionDef)
             and n.name == '_capture_candidate_has_media_shape')
_g51 = {'urlparse': urlparse, 're': re}
_g51.update(_LIFT_HELPERS)
_g51['VIDEO_EXTENSIONS'] = G['VIDEO_EXTENSIONS']
_g51['AUDIO_EXTENSIONS'] = G['AUDIO_EXTENSIONS']
exec(compile(_ast44.Module(body=[_fn51], type_ignores=[]), '<lifted51>', 'exec'), _g51)
_disc51 = next(n for n in _ast44.walk(_main44)
               if isinstance(n, _ast44.FunctionDef)
               and n.name == '_is_disguised_hls_manifest')
exec(compile(_ast44.Module(body=[_disc51], type_ignores=[]), '<lifted51d>', 'exec'), _g51)


class _T51Stub:
    _PLAYABLE_MEDIA_SUFFIXES = ('.m3u8', '.m3u', '.mpd', '.mp4', '.m4v',
                                '.webm', '.mkv', '.ts', '.flv', '.mov', '.avi')
    VIDEO_EXTENSIONS = G['VIDEO_EXTENSIONS']
    AUDIO_EXTENSIONS = G['AUDIO_EXTENSIONS']
    _capture_candidate_has_media_shape = _g51['_capture_candidate_has_media_shape']
    _is_disguised_hls_manifest = staticmethod(_g51['_is_disguised_hls_manifest'])
    _is_hls_stream_url = lambda self, u, ct='': str(u).lower().split('?')[0].endswith(('.m3u8', '.m3u'))


_s51 = _T51Stub()
for _u51, _want51, _why51 in [
    # playmate's CDN, in the exact shape this file already documents
    ('https://srv1-2.plauymito.live/hls/xoDtAGdof2iJA/master.txt', True,
     "playmate's renamed master playlist"),
    ('https://srv1-2.plauymito.live/hls/xoDtAGdof2iJA/index_avc_1080p.txt', True,
     'and its variant'),
    # the hglink family's shape, same predicate
    ('https://g4vsrqvtrj.auronetworkdesign.space/As4sNGJK0nXh/hls3/01/14959'
     '/x0o069k2qb38_o/master.txt', True,
     "hglink's renamed master playlist"),
    # watchstreamhd's master.txt is AES ciphertext and must NEVER be playable
    ('https://watchstreamhd.com/cdn/hls/9f1c4d0e8b2a7361f5c0d9e8b7a63521/master.txt',
     False,
     "watchstreamhd's AES-ciphertext master.txt (the carve-out)"),
    # and nothing that was already rejected changes
    ('https://vd.ambotalaing.com/r19XC1eW9QAwPN/147054', False,
     'the playmate advert'),
    ('https://playmate.to/assets/js/player-core.min.js', False,
     'the player script itself'),
]:
    _got51 = _s51._capture_candidate_has_media_shape(_u51)
    report(_got51 is _want51, f'shape? {_want51}  {_why51}')

# ── 52. JW Player 8 keeps its media in item.sources[], not item.file ─────────
# The user pasted playmate's real URLs and said they "worked instantly" once
# pasted in by hand:
#   https://srv1-2.plauymito.live/hls/TVeZDYBcOvOWtCBrcFeAxlYXpuV1C3vV/master.txt
#   .../index_avc_1080p.txt
# ...and that FetchV sees them "even before starting the video". So the
# manifest is configured in the player and readable without playback -- but
# the DOM probe read only `item.file`, which JW Player 8 leaves undefined on
# a multi-source playlist. That is why the capture reported jwplayer.js and
# adverts and never the stream.
_js52 = next(n.value.value for n in _ast44.walk(_main44)
             if isinstance(n, _ast44.Assign)
             and any(getattr(tg, 'id', None) == '_dom_media_js' for tg in n.targets))
report('item.sources' in _js52, 'the probe now reads JW 8 item.sources[]')
report('src.file' in _js52, 'and pushes each source file')
report('getPlaylist' in _js52, 'and walks the whole playlist, not just the current item')
report('item.file' in _js52, 'while the JW 7 single-source shape still works')

# Execute the SHIPPED JavaScript against a stubbed playmate page, if node is
# present. This is the real string from main.py, not a copy.
import shutil as _sh52, subprocess as _sp52, tempfile as _tf52, json as _json52
if _sh52.which('node'):
    _MASTER52 = ('https://srv1-2.plauymito.live/hls/'
                 'TVeZDYBcOvOWtCBrcFeAxlYXpuV1C3vV/master.txt')
    _VAR52 = ('https://srv1-2.plauymito.live/hls/'
              'TVeZDYBcOvOWtCBrcFeAxlYXpuV1C3vV/index_avc_1080p.txt')
    _d52 = _tf52.mkdtemp()
    _mod52 = os.path.join(_d52, 'dom_media.js')
    with open(_mod52, 'w', encoding='utf-8') as _f52:
        _f52.write('module.exports = ' + _js52.strip() + ';\n')
    _harness52 = """
const fn = require(process.argv[2]);
const M = process.argv[3], V = process.argv[4];
const item = { sources: [ {file: M, type: 'hls'}, {file: V} ] };  // JW 8: no top-level .file
const div = { id: 'jwplayer' };
global.document = { querySelectorAll(sel) {
    if (sel === '.jwplayer') return [div];
    return [];                       // no <video>, no anchors: nothing has played
} };
global.window = { jwplayer: () => ({
    getPlaylist: () => [item], getPlaylistItem: () => item,
}) };
const urls = (fn() || []).map(r => r.u);
// and the JW 7 single-source shape, on a fresh stub
global.window = { jwplayer: () => ({ getPlaylistItem: () => ({ file: M }) }) };
const legacy = (fn() || []).map(r => r.u);
console.log(JSON.stringify({urls: urls, legacy: legacy}));
"""
    _h52 = os.path.join(_d52, 'run.js')
    with open(_h52, 'w', encoding='utf-8') as _f52:
        _f52.write(_harness52)
    _out52 = _sp52.run(['node', _h52, _mod52, _MASTER52, _VAR52],
                       capture_output=True, text=True, timeout=60)
    _res52 = _json52.loads(_out52.stdout.strip().splitlines()[-1]) if _out52.stdout.strip() else {}
    report(_MASTER52 in _res52.get('urls', []),
           'RUNNING the shipped JS: playmate\'s master.txt is captured from a '
           'JW 8 sources[] item that has never been played')
    report(_VAR52 in _res52.get('urls', []),
           'and so is the 1080p variant')
    report(_MASTER52 in _res52.get('legacy', []),
           'and a JW 7 single-source item is still captured')
else:
    report(True, 'node not available - skipped executing the capture JS')

# Downstream: the announced URL must survive the gates it has to pass.
_fn52 = next(n for n in _ast44.walk(_main44)
             if isinstance(n, _ast44.FunctionDef)
             and n.name == '_capture_candidate_has_media_shape')
_g52 = {'urlparse': urlparse, 're': re,
        'VIDEO_EXTENSIONS': G['VIDEO_EXTENSIONS'],
        'AUDIO_EXTENSIONS': G['AUDIO_EXTENSIONS']}
_g52.update(_LIFT_HELPERS)
exec(compile(_ast44.Module(body=[_fn52], type_ignores=[]), '<l52>', 'exec'), _g52)
_disc52 = next(n for n in _ast44.walk(_main44)
               if isinstance(n, _ast44.FunctionDef)
               and n.name == '_is_disguised_hls_manifest')
exec(compile(_ast44.Module(body=[_disc52], type_ignores=[]), '<l52d>', 'exec'), _g52)


class _T52:
    _PLAYABLE_MEDIA_SUFFIXES = ('.m3u8', '.m3u', '.mpd', '.mp4', '.m4v',
                                '.webm', '.mkv', '.ts', '.flv', '.mov', '.avi')
    VIDEO_EXTENSIONS = G['VIDEO_EXTENSIONS']
    AUDIO_EXTENSIONS = G['AUDIO_EXTENSIONS']
    _capture_candidate_has_media_shape = _g52['_capture_candidate_has_media_shape']
    _is_disguised_hls_manifest = staticmethod(_g52['_is_disguised_hls_manifest'])
    _is_hls_stream_url = lambda self, u, ct='': str(u).lower().split('?')[0].endswith(('.m3u8', '.m3u'))


for _u52 in (_MASTER52, _VAR52):
    report(_T52._is_disguised_hls_manifest(_u52)
           and _T52()._capture_candidate_has_media_shape(_u52),
           f'the announced {_u52.split("/")[-1]} then survives both gates')

# ── 53. The JW probe needed the .jwplayer CLASS, which only appears after
#        setup -- and it failed silently, so a stale build looked identical
#        to a real miss. ────────────────────────────────────────────────────
# The capture for playmate.to/embed/dl6XiG3upbbCb again showed jwplayer.js,
# player-core.min.js, adverts and no stream. The probe walked
# document.querySelectorAll('.jwplayer'), but JW applies that class only once
# setup has run, and queried jwplayer(el.id) -- so before setup there was
# nothing to iterate. jwplayer() with no argument returns the first instance
# regardless. And nothing was logged, so the run above could not distinguish
# "probe found nothing" from "this build has no probe".
report(".jwplayer, #jwplayer, .jwplayer-container, [id^=\"jwplayer\"]" in _js52,
       'the probe now finds the container by id as well as by class')
report('window.jwplayer().getPlaylistItem' in _js52,
       'and asks the no-argument jwplayer() for the playlist')
report('[BROWSER_CLICK][JWPROBE]' in _src46,
       'the capture now reports what the JW probe saw, once per run')
report('_jw_status_reported' in _src46,
       'and only once, so it cannot spam the watch loop')

import shutil as _sh53, subprocess as _sp53, tempfile as _tf53, json as _json53
if _sh53.which('node'):
    _st53 = next(n.value.value for n in _ast44.walk(_main44)
                 if isinstance(n, _ast44.Assign)
                 and any(getattr(tg, 'id', None) == '_jw_status_js' for tg in n.targets))
    _d53 = _tf53.mkdtemp()
    for _nm53, _body53 in (('probe', _js52), ('status', _st53)):
        with open(os.path.join(_d53, _nm53 + '.js'), 'w', encoding='utf-8') as _f53:
            _f53.write('module.exports = ' + _body53.strip() + ';\n')
    _h53 = os.path.join(_d53, 'run.js')
    with open(_h53, 'w', encoding='utf-8') as _f53:
        _f53.write("""
const probe = require(process.argv[2] + '/probe.js');
const status = require(process.argv[2] + '/status.js');
const M = process.argv[3];
const out = {};
function run(name, els, apiWorks) {
  global.document = { querySelectorAll(sel) {
      if (sel === '.jwplayer') return els.filter(e => e.cls);
      return els.filter(e => !e.cls);
  } };
  const inst = { getPlaylist: () => [{sources:[{file:M}]}],
                 getPlaylistItem: () => ({sources:[{file:M}]}) };
  global.window = apiWorks ? { jwplayer: () => inst } : {};
  const urls = (probe() || []).map(r => r.u);
  const st = status() || {};
  out[name] = {captured: urls.indexOf(M) !== -1, api: !!st.api,
               els: st.els || 0, sources: st.sources || 0};
}
run('setup',    [{id:'jwplayer', cls:true}],  true);
run('noclass',  [{id:'jwplayer', cls:false}], true);
run('noelement',[],                           true);
run('noapi',    [{id:'jwplayer', cls:false}], false);
console.log(JSON.stringify(out));
""")
    _M53 = 'https://srv1-2.plauymito.live/hls/ABC123def456/master.txt'
    _o53 = _sp53.run(['node', _h53, _d53, _M53], capture_output=True, text=True, timeout=60)
    _r53 = _json53.loads(_o53.stdout.strip().splitlines()[-1]) if _o53.stdout.strip() else {}
    report(_r53.get('setup', {}).get('captured') is True,
           'RUNNING the shipped JS: a fully set-up JW player is still captured')
    report(_r53.get('noclass', {}).get('captured') is True,
           'and one whose .jwplayer class has not been applied yet is captured '
           'too (this is the case the old probe missed)')
    report(_r53.get('noelement', {}).get('captured') is True,
           'and so is one with no container element in the DOM at all')
    report(_r53.get('noapi', {}).get('captured') is False
           and _r53.get('noapi', {}).get('api') is False,
           'while a page where jwplayer never loaded captures nothing and the '
           'diagnostic says api=false -- the difference is now visible')
else:
    report(True, 'node not available - skipped executing the capture JS')

# ── 54. playmate.to: call the site's own /api/s instead of capturing ──────────
# The embed page holds no media URL at all (window.__PM carries only
# videoId/referrer/countKey/duration) and player-core.min.js mints the stream
# with an XHR, so five capture rounds found nothing. player-core is
# javascript-obfuscator output; decoding its string array with the site's own
# decoder yields POST /api/s {"c":filecode,"d":device} -> {sx: <manifest>}.
# GET https://playmate.to/api/s answers {"error":"Method not allowed"}, which
# is what pins the route and the verb.
print()
print('54. playmate.to resolves through its own POST /api/s, no browser')

_pm_host = lift('VideoPlayer', '_is_playmate_host')
_pm_code = lift('VideoPlayer', '_playmate_filecode')
_pm_res = lift('VideoPlayer', '_resolve_playmate_source')
_PM_HOSTS = lift_attr('VideoPlayer', '_PLAYMATE_HOSTS')


class PlaymateStub:
    _PLAYMATE_HOSTS = _PM_HOSTS
    _is_playmate_host = _pm_host
    _playmate_filecode = _pm_code
    _resolve_playmate_source = _pm_res
    # real shipped helpers, so the HLS/proxy decision is the shipped one
    _is_disguised_hls_manifest = staticmethod(
        lift('VideoPlayer', '_is_disguised_hls_manifest'))
    _is_hls_stream_url = lift('VideoPlayer', '_is_hls_stream_url')
    _flag_png_wrapped_hls_for_proxy = lift(
        'VideoPlayer', '_flag_png_wrapped_hls_for_proxy')

    def __init__(self, payload=None, ok=True, status=200, png_wrap=False,
                 raise_exc=None):
        self.calls = []
        self._payload, self._ok, self._status = payload, ok, status
        self._png_wrap, self._raise = png_wrap, raise_exc

    def _stream_request_headers(self, referer=None, extra=None):
        return {'User-Agent': 'Mozilla/5.0 (stub)'}

    def _hls_request_headers(self, source_url):
        return {'Referer': str(source_url)}

    def _clean_remote_title(self, title):
        return str(title or '').strip()

    def _hls_manifest_ships_png_wrapped_segments(self, url, headers=None,
                                                 referer=None):
        return self._png_wrap


class _FakeCffiResponse:
    def __init__(self, stub):
        self._stub = stub
        self.status_code = stub._status
        self.ok = stub._ok
        self.text = json.dumps(stub._payload) if stub._payload is not None else ''

    def json(self):
        if self._stub._payload is None:
            raise ValueError('no json')
        return self._stub._payload


class _FakeCffiSession:
    def __init__(self, stub):
        self._stub = stub

    def post(self, url, headers=None, data=None, timeout=None,
             allow_redirects=None):
        self._stub.calls.append({
            'url': url, 'headers': dict(headers or {}), 'data': data,
            'timeout': timeout, 'allow_redirects': allow_redirects,
        })
        if self._stub._raise:
            raise self._stub._raise
        return _FakeCffiResponse(self._stub)


import sys as _sys54
import types as _types54


class _FakeCffiRequests(_types54.ModuleType):
    _stub = None

    @classmethod
    def Session(cls, impersonate=None):
        return _FakeCffiSession(cls._stub)


# the lifted resolver stamps resolved_at_ms, so it needs main.py's module globals
G.setdefault('time', time)


def _run_playmate(stub, url='https://playmate.to/embed/xoDtAGdof2iJA'):
    """Execute the shipped resolver with curl_cffi.requests stubbed out."""
    mod = _types54.ModuleType('curl_cffi')
    req = _FakeCffiRequests('curl_cffi.requests')
    # Session() is a classmethod on the shipped call path, so the stub has to
    # live on the class, not on the module instance.
    _FakeCffiRequests._stub = stub
    mod.requests = req
    saved = {k: _sys54.modules.get(k) for k in ('curl_cffi', 'curl_cffi.requests')}
    _sys54.modules['curl_cffi'] = mod
    _sys54.modules['curl_cffi.requests'] = req
    try:
        return stub, stub._resolve_playmate_source(url)
    finally:
        for k, v in saved.items():
            if v is None:
                _sys54.modules.pop(k, None)
            else:
                _sys54.modules[k] = v


PlaymateStub._resolve_playmate_source = _pm_res

_SX = 'https://srv1-2.plauymito.live/hls/TVeZDYBcOvOWtCBrcFeAxlYXpuV1C3vV/master.txt'
_GOOD = {'sx': _SX, 'tx': 'JIMMY-009', 'ix': 'https://srv1-2.plauymito.live/thumbnail/xoDtAGdof2iJA.jpg',
         'ax': '', 'lx': '', 'cx': 'xoDtAGdof2iJA', 'kx': []}

report('playmate.to' in _PM_HOSTS,
       'playmate.to is named as a playmate host', str(_PM_HOSTS))
_s54 = PlaymateStub(_GOOD)
report(_s54._is_playmate_host('playmate.to') and _s54._is_playmate_host('www.playmate.to'),
       'the host test matches playmate.to and its www subdomain')
report(not _s54._is_playmate_host('notplaymate.to')
       and not _s54._is_playmate_host('playmate.to.evil.example')
       and not _s54._is_playmate_host(''),
       "and not a lookalike host, a host merely starting with the name, or ''")
report(_s54._playmate_filecode('https://playmate.to/embed/xoDtAGdof2iJA') == 'xoDtAGdof2iJA'
       and _s54._playmate_filecode('https://playmate.to/embed/xoDtAGdof2iJA/') == 'xoDtAGdof2iJA',
       'the filecode is the embed path segment, with or without a trailing slash')

_stub54, _got54 = _run_playmate(PlaymateStub(_GOOD))
_got54 = _got54 or {}
report(len(_stub54.calls) == 1, 'the resolver issued exactly one HTTP call')
_c54 = (_stub54.calls or [{}])[0]
report(_c54.get('url') == 'https://playmate.to/api/s',
       'it POSTed to playmate\'s real endpoint /api/s (the decoded one)',
       str(_c54.get('url')))
report(json.loads(_c54.get('data') or '{}') == {'c': 'xoDtAGdof2iJA', 'd': 'web'},
       'with the body player-core sends: {"c": filecode, "d": device}',
       str(_c54.get('data')))
report(_c54.get('headers', {}).get('Content-Type') == 'application/json',
       'as application/json')
report(_c54.get('headers', {}).get('Referer') == 'https://playmate.to/embed/xoDtAGdof2iJA'
       and _c54.get('headers', {}).get('Origin') == 'https://playmate.to',
       'with the embed page as Referer and the site as Origin')
report(isinstance(_got54, dict) and _got54.get('playback_url') == _SX,
       'and the manifest from `sx` became the playback URL')
report(_got54.get('resolver_provider') == 'playmate_api'
       and _got54.get('pre_resolved_playback_url') is True,
       'tagged as pre-resolved, so no capture browser is opened for it')
# call through an instance so `self` binds the way it does at runtime
report(_got54.get('content_type') == 'application/vnd.apple.mpegurl'
       and _s54._is_hls_stream_url(_got54.get('playback_url'),
                                   _got54.get('content_type')),
       'and the .txt manifest is recognised as an HLS stream')
report(_s54._is_disguised_hls_manifest(_SX) is True,
       'specifically as a .txt-disguised master under /hls/, which is what '
       'makes the shipped playlist rewriter handle it')
report(_s54._is_disguised_hls_manifest(
           'https://watchstreamhd.com/cdn/hls/abc123/master.txt') is False,
       'while the AES-ciphertext master.txt on watchstreamhd stays excluded')
report(_got54.get('title') == 'JIMMY-009',
       "the API's `tx` became the title", str(_got54.get('title')))
report(_got54.get('route_local_proxy') is not True,
       'plaintext segments stay off the unwrapping proxy')

_stub54b, _got54b = _run_playmate(PlaymateStub(_GOOD, png_wrap=True))
_got54b = _got54b or {}
report(_got54b.get('route_local_proxy') is True,
       'but PNG-wrapped segments are routed through the local proxy, exactly '
       'as the field-confirmed hglink path is')

_stub54c, _got54c = _run_playmate(
    PlaymateStub(_GOOD), 'https://playmate.to/embed/xoDtAGdof2iJA?ref=sextb&t=1')
report((_stub54c.calls or [{}])[0].get('url')
       == 'https://playmate.to/api/s?ref=sextb&t=1',
       "the embed page's own query string is forwarded, as player-core does",
       str((_stub54c.calls or [{}])[0].get('url')))

_stub54i, _got54i = _run_playmate(
    PlaymateStub(_GOOD),
    'https://playmate.to/embed/xoDtAGdof2iJA?filecode=other&t=1')
report((_stub54i.calls or [{}])[0].get('url')
       == 'https://playmate.to/api/s?t=1',
       "except a 'filecode' parameter, which player-core deletes first "
       "(e.delete('filecode'))",
       str((_stub54i.calls or [{}])[0].get('url')))

_stub54d, _got54d = _run_playmate(PlaymateStub({'tx': 'no stream here'}))
report(_got54d is None and len(_stub54d.calls) == 1,
       'a response with no `sx` yields None, so the capture ladder still runs')

_stub54e, _got54e = _run_playmate(
    PlaymateStub(_GOOD, ok=False, status=405))
report(_got54e is None,
       'a 405 from the endpoint yields None rather than a bogus stream')

_stub54f, _got54f = _run_playmate(PlaymateStub(_GOOD), 'https://hglink.to/e/abc')
report(_got54f is None and len(_stub54f.calls) == 0,
       'a non-playmate host is untouched: not one request is made')

_stub54g, _got54g = _run_playmate(
    PlaymateStub({'sx': '/hls/abc123/master.txt'}))
_got54g = _got54g or {}
report(_got54g is not None
       and _got54g.get('playback_url') == 'https://playmate.to/hls/abc123/master.txt',
       'a relative `sx` is resolved against the site origin',
       str(_got54g.get('playback_url')))

_stub54h, _got54h = _run_playmate(
    PlaymateStub(_GOOD, raise_exc=OSError('tls handshake failed')))
report(_got54h is None,
       'a transport error is reported and yields None, never a silent pass')

_src54 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py'),
              encoding='utf-8').read()
report("elif self._is_playmate_host(host):" in _src54
       and "resolved = self._resolve_playmate_source(source_url)" in _src54,
       'the resolver is wired into _resolve_stream_source, ahead of the '
       'generic browser-capture fallback')
report("_PLAYMATE_HOSTS = ('playmate.to',)" in _src54
       and 'GET https://playmate.to/api/s answers' in _src54
       and '{"error":"Method not allowed"}' in _src54,
       'and the code records how the endpoint was established -- the decoded '
       'string array plus the live 405 on GET -- rather than a guessed route')
report("[PLAYMATE_API]" in _src54,
       'every branch of it prints a [PLAYMATE_API] line, so a field log can '
       'never be mistaken for a stale build')


# ── 55. Metadata Linker: rank by signal specificity, not signal count ────────
# The linker auto-applies its top candidate, so a wrong one overwrites the
# row's display name. Measured against the shipped nubiles DB
# (tools_eval_metadata_linker.py, 1500 movies x 5 query shapes), sorting by
# (signal_count, score) linked 12.20% of queries to a genuinely wrong movie:
# page_url 67.0%, host_style 71.7%. Cause: 'site' fires for every movie from
# that source and 'series' for every movie in the series, so the pair
# ('series','site') is the most common two-signal combination in the DB, while
# 'id' -- a unique numeric video id -- contributed nothing to _score at all.
print()
print('55. Metadata Linker ranks a unique video id above cheap shared signals')

import sys as _sys55, tempfile as _tf55, types as _types55

# metadata_scraper imports PyQt6 at module scope; the ranking under test needs
# none of it, so stub the module and import the real thing.
if 'PyQt6' not in _sys55.modules:
    class _Q55:
        def __init__(self, *a, **k): pass
        def __getattr__(self, n): return _Q55()
        def __call__(self, *a, **k): return _Q55()

    class _QtMod55(_types55.ModuleType):
        def __getattr__(self, n):
            return (lambda *a, **k: _Q55()) if n == 'pyqtSignal' else _Q55

    for _n55 in ('PyQt6', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui'):
        _sys55.modules[_n55] = _QtMod55(_n55)

import metadata_scraper as _msrc55

_TARGET55 = '248755-facials-for-my-stepsis-and-her-friend-s7e6'
_MOVIES55 = {}


def _mk55(slug, vid, title, series, models, date, source='nubiles-porn'):
    _MOVIES55[slug] = {
        'slug': slug, 'title': title, 'series': series, 'series_url': '',
        'series_slug': '', 'models': list(models), 'model_urls': [],
        'model_details': [], 'date': date, 'published_date': '',
        'published_date_iso': '', 'video_id': vid, 'type': 'video',
        'url': f'https://nubiles-porn.com/video/watch/{vid}/'
               + slug.split('-', 1)[1].replace('-s7e6', ''),
        'source_site': source, 'source_name': 'Nubiles Porn',
        'scraped_at': '', 'meta_fetched': True,
    }


# the real failure shape: one exact movie, plus same-series siblings that all
# share the cheap ('series','site') pair
_mk55(_TARGET55, '248755', 'Facials For My Stepsis And Her Friend',
      'NubilesPorn', ['Axel Haze', 'Delilah Dagger'], '08/04/2026')
for _n55b, _v55, _t55 in (
    ('253704-im-pretty-sure-stepsis-is-a-super-heroine', '253704',
     'Im Pretty Sure Stepsis Is A Super Heroine Friend'),
    ('253662-watching-stepsisters-nighttime-routine', '253662',
     'Watching My Stepsis Nighttime Routine With Her Friend'),
    ('254138-can-i-sleep-in-your-room-tonight-stepsis', '254138',
     'Can I Sleep In Your Room Tonight Stepsis'),
):
    _mk55(_n55b, _v55, _t55, 'NubilesPorn', ['Axel Haze'], '08/04/2026')

_dbf55 = os.path.join(_tf55.mkdtemp(), 'linker55.json')
with open(_dbf55, 'w', encoding='utf-8') as _f55:
    json.dump({'last_full_scrape': '2026-08-04T00:00:00Z',
               'last_update_scrape': None, 'movies': _MOVIES55}, _f55)
_db55 = _msrc55.MetadataDB(_dbf55)
_mt55 = _msrc55.TitleMatcher(_db55)

report(len(_mt55._records) == len(_MOVIES55),
       'the real TitleMatcher indexes the fixture', str(len(_mt55._records)))

_url55 = f'https://nubiles-porn.com/video/watch/248755/facials-for-my-stepsis-and-her-friend'
_hit55 = _mt55.match(_url55)
report(_hit55 is not None and _hit55.get('slug') == _TARGET55,
       'a page URL carrying the unique video id resolves to that exact movie '
       '(was: a same-series sibling won)', str((_hit55 or {}).get('slug')))

_host55 = _mt55.match('nubiles-porn 248755 Facials For My Stepsis And Her Friend')
report(_host55 is not None and _host55.get('slug') == _TARGET55,
       'and so does a file-host style name built from site + id + title',
       str((_host55 or {}).get('slug')))

_c55 = _mt55.match_candidates(_url55, limit=5, include_weak=True)[0]
report(_c55.get('signals') and 'id' in _c55['signals'],
       "the winning candidate is the one that matched the video id, "
       f"signals={_c55.get('signals')}")

_W55 = _msrc55.TitleMatcher._SIGNAL_WEIGHTS
_HIGH55 = _msrc55.TitleMatcher.HIGH_CONFIDENCE_STRENGTH
report(_mt55._signal_strength(['series', 'site']) < _HIGH55,
       "the near-free ('series','site') pair is NOT high confidence",
       f"{_mt55._signal_strength(['series','site'])} < {_HIGH55}")
# An id only exists on the creator's own site, so even corroborated by the
# site name it is no longer allowed to carry a match by itself -- the name,
# actors, series, date or runtime has to.
# A scene title is not an identifier either. 'Breaking the Rules' exists in
# more than one network and the preview path takes the first site that has it,
# so the title alone used to hand a row somebody else's poster.
report(getattr(_msrc55.TitleMatcher, '_LONE_SCENE_STRENGTH', _HIGH55) < _HIGH55
       and _mt55._signal_strength(['scene']) < _HIGH55,
       'a lone scene title is offered as a possible match, never auto-applied '
       '-- 93 titles are shared between the shipped teamskeet and nubiles '
       'databases, so the title alone cannot pick between networks',
       f"lone scene {_mt55._signal_strength(['scene'])} < {_HIGH55}")
report(_mt55._signal_strength(['scene', 'site']) >= _HIGH55
       and _mt55._signal_strength(['scene', 'model:a']) >= _HIGH55
       and _mt55._signal_strength(['scene', 'date']) >= _HIGH55,
       'and any one corroborating signal -- the site, a performer or the '
       'release date -- carries it back over the line, which is the whole '
       'point: a real filename is never only a title',
       str([_mt55._signal_strength(['scene', 'site']),
            _mt55._signal_strength(['scene', 'model:a']),
            _mt55._signal_strength(['scene', 'date'])]))
report(_mt55._signal_strength(['id', 'site']) < _HIGH55,
       'and a creator-site id, even with the site name, is not',
       f"{_mt55._signal_strength(['id','site'])} < {_HIGH55}")
# Video ids are only 5-6 digits, so an unrelated URL or filename carrying such
# a number must NOT be enough to rename a row on its own.
report(_msrc55.TitleMatcher._LONE_ID_STRENGTH < _HIGH55
       and _mt55._signal_strength(['id']) < _HIGH55,
       'a lone video id is offered as a possible match, never auto-applied',
       f"lone id {_mt55._signal_strength(['id'])} < {_HIGH55}")
report(_mt55._signal_strength(['id']) > _mt55._signal_strength(['series', 'site']),
       'but it still outranks the cheap series+site pair')
report(_mt55._signal_strength(['model:a'] * 9)
       == _mt55._signal_strength(['model:a', 'model:b']),
       'model matches are capped, so cast size cannot outvote a video id',
       str(_mt55._signal_strength(['model:a'] * 9)))
report(_W55['scene'] > _W55['duration'] > _W55['date'] > _W55['model']
       >= _W55['id'] > _W55['series'] > _W55['scene_partial'] > _W55['site'],
       'the weights are ordered by how well each survives being re-hosted',
       str(_W55))

_sib55 = next(r for s55, r in _mt55._records.items() if s55.startswith('253704'))
_tgt55 = _mt55._records[_TARGET55]
_qn55, _qt55, _qd55 = _msrc55._normalise(_url55), _msrc55._tokens(_url55), set()
report(_mt55._score(_url55, _qn55, _qt55, _qd55, _tgt55)
       > _mt55._score(_url55, _qn55, _qt55, _qd55, _sib55),
       'the fuzzy score now agrees: the video-id match outscores the sibling',
       f"{_mt55._score(_url55,_qn55,_qt55,_qd55,_tgt55):.3f} > "
       f"{_mt55._score(_url55,_qn55,_qt55,_qd55,_sib55):.3f}")

report(_mt55._match_signals('stepsis friend', _msrc55._normalise('stepsis friend'),
                            _msrc55._compact('stepsis friend'),
                            _msrc55._tokens('stepsis friend'), set(), _tgt55)
       and 'scene' not in _mt55._match_signals(
           'stepsis friend', _msrc55._normalise('stepsis friend'),
           _msrc55._compact('stepsis friend'),
           _msrc55._tokens('stepsis friend'), set(), _tgt55),
       'two shared title tokens are scene_partial, not a full scene match')

report(_msrc55._candidate_is_high_confidence({'strength': 1.0}) is True
       and _msrc55._candidate_is_high_confidence({'strength': 0.25}) is False,
       'the dialog auto-apply gate reads the same strength the matcher sorts by')
report(_msrc55._candidate_is_high_confidence({'signal_count': 2}) is True
       and _msrc55._candidate_is_high_confidence({'signal_count': 1}) is False,
       'and a candidate built before strength existed still gates on two signals')
report(_msrc55._candidate_is_high_confidence(None) is False
       and _msrc55._candidate_is_high_confidence({}) is False,
       'nothing auto-applies on an empty or missing candidate')

# A query carrying nothing but the series name and the site must not
# auto-apply, while the same query plus the video id must. (Assert the
# lengths, not just all(): over an empty list all() is trivially True.)
_weak55 = _mt55.match_candidates('NubilesPorn stepsis friend', limit=5,
                                 include_weak=False)
_strong55 = _mt55.match_candidates('NubilesPorn 248755 stepsis friend', limit=5,
                                   include_weak=False)
report(len(_weak55) == 0 and len(_strong55) == 1
       and _strong55[0]['movie']['slug'] == _TARGET55,
       'match(include_weak=False) drops a series+site-only query but keeps the '
       'one carrying a video id',
       f'weak={len(_weak55)} strong={len(_strong55)}')

# Determinism: hits is filled by iterating the q_tokens SET, whose order
# Python randomises per process, so ranking on count alone made the
# 80-slot candidate pool -- and therefore the answer -- differ between
# runs of the same query (measured 1989/1988/1990 on three identical
# runs before the slug tie-break).
report(_mt55._candidate_slugs(('facials', 'stepsis', 'friend'), set())
       == _mt55._candidate_slugs(('friend', 'stepsis', 'facials'), set()),
       'the candidate pool does not depend on token iteration order')
_cands55 = _mt55.match_candidates('NubilesPorn stepsis', limit=5, include_weak=True)
_tie55 = [c55['movie']['slug'] for c55 in _cands55]
report(len(_tie55) > 1 and _tie55 == [c55['movie']['slug'] for c55 in
                                      _mt55.match_candidates('NubilesPorn stepsis',
                                                             limit=5, include_weak=True)],
       'the same query resolves to the same order every time', str(_tie55))
_str55b = [c55['strength'] for c55 in _cands55]
report(_str55b == sorted(_str55b, reverse=True),
       'and that order is by strength first', str(_str55b))

_src55 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'metadata_scraper.py'), encoding='utf-8').read()

# A filehost URL shares no token with the index. Scoring the whole database
# for it cost ~1 s per row on the 5371-movie nubiles DB, and 246 of 300
# sampled playlist rows (pixeldrain / bunkr / gofile) hit that path inside
# _run_match's loop on the UI thread -- to produce no candidate at all.
_t055 = time.time()
_far55 = _mt55.match_candidates('https://pixeldrain.com/u/ksbtnekh', limit=5,
                                include_weak=True)
_dt55 = time.time() - _t055
report(_far55 == [] and _dt55 < 0.05,
       'an unrelated filehost URL short-circuits instead of scanning every '
       'record', f'{len(_far55)} candidates in {_dt55*1000:.2f} ms')
report('            candidate_slugs = set(self._records)' not in _src55,
       'the whole-database fallback is gone from match_candidates')

report(_src55.count('_candidate_is_high_confidence(top)') == 1
       and _src55.count('_candidate_is_high_confidence(candidates[0])') == 1,
       'both linker auto-apply sites use the shared gate, not their own '
       'signal_count >= 2')
report('signal_count", 0)) >= 2' not in _src55.replace(
           'int(candidate.get("signal_count", 0)) >= 2', ''),
       'and no auto-apply site is left spelling the old rule')
report('key=lambda item: (-item[1], item[0])' in _src55
       and 'item["strength"], -item["score"]' in _src55,
       'both ranking sorts carry an explicit deterministic tie-break')

# ── 56. Metadata Linker: runtime is a criterion, a creator-site id is not ────
# A video id only exists on the creator's own site. The links actually pasted
# in -- pixeldrain, bunkr, gofile, tube mirrors -- carry neither one, so the
# only criteria that survive re-hosting are name, actors, series, date and
# runtime. 'duration' did not exist anywhere in the scraper or the shipped
# data before this; 'id' was weighted 1.00 and dominated the ranking.
print()
print('56. Metadata Linker matches on runtime, and no longer on a creator-site id')

report(_msrc55._duration_seconds(754) == 754 and _msrc55._duration_seconds(754000) == 754
       and _msrc55._duration_seconds('12:34') == 754
       and _msrc55._duration_seconds('1:02:03') == 3723
       and _msrc55._duration_seconds('12m34s') == 754,
       'a runtime is normalised from seconds, ms, mm:ss, hh:mm:ss and 12m34s')
report(all(_msrc55._duration_seconds(v) == 0 for v in (None, '', 'abc', 0, True)),
       'and unknown or non-numeric runtimes normalise to 0, never a false match')

report(_msrc55._query_durations('Facials For My Stepsis 12:34') == {754}
       and _msrc55._query_durations('scene [1:02:03] x264') == {3723},
       'a runtime spelled out in a title or filename is read from the text')
report(_msrc55._query_durations('movie.1080p.2020.mp4') == set(),
       'but 1080p and a year are never mistaken for one')
report(_msrc55._durations_agree(754, 760) and not _msrc55._durations_agree(754, 800)
       and not _msrc55._durations_agree(0, 754),
       'runtimes match within re-encode drift (10 s or 3%), and 0 never matches')

# The case that justifies the criterion: same name, same actors, same series
# and same date, so nothing but the runtime can separate them.
_d56 = os.path.join(_tf55.mkdtemp(), 'dur56.json')
_rows56 = {}
for _sl56, _vid56, _dur56 in (('cut-scene-1', '111111', 754), ('full-scene-2', '222222', 1812)):
    _rows56[_sl56] = {
        'slug': _sl56, 'title': 'Facials For My Stepsis', 'series': 'NubilesPorn',
        'series_url': '', 'series_slug': '', 'models': ['Axel Haze'], 'model_urls': [],
        'model_details': [], 'date': '08/04/2026', 'published_date': '',
        'published_date_iso': '', 'video_id': _vid56, 'type': 'video',
        'url': f'https://nubiles-porn.com/video/watch/{_vid56}/x',
        'source_site': '', 'source_name': '', 'duration': _dur56,
        'scraped_at': '', 'meta_fetched': True,
    }
with open(_d56, 'w', encoding='utf-8') as _f56:
    json.dump({'movies': _rows56}, _f56)
_md56 = _msrc55.TitleMatcher(_msrc55.MetadataDB(_d56))
_q56 = 'Facials For My Stepsis Axel Haze'
_sig56 = _md56.match_candidates(_q56, limit=1, include_weak=True,
                                duration_ms=1812000)[0]['signals']
report('duration' in _sig56, 'a matching runtime raises a duration signal', str(_sig56))
report(_md56.match(_q56, duration_ms=1812000)['slug'] == 'full-scene-2'
       and _md56.match(_q56, duration_ms=754000)['slug'] == 'cut-scene-1',
       'and picks the right scene when name, actors, series and date all tie',
       f"{_md56.match(_q56, duration_ms=1812000)['slug']} / "
       f"{_md56.match(_q56, duration_ms=754000)['slug']}")
report(_md56.match(_q56 + ' 12:34')['slug'] == 'cut-scene-1',
       'the same works from a runtime written into the filename itself')
report(_md56._score(_q56, _msrc55._normalise(_q56), _msrc55._tokens(_q56), set(),
                    _md56._records['full-scene-2'], {1812})
       > _md56._score(_q56, _msrc55._normalise(_q56), _msrc55._tokens(_q56), set(),
                      _md56._records['full-scene-2'], {754}),
       'and the fuzzy score sees the runtime too, so it can rank on it')

class _Player56:
    def __init__(self):
        self.video_durations = {'/a/x.mp4': 754000}
        self._stream_resolution_cache = {'https://h/y': {'duration_ms': 1812000}}

_p56 = _Player56()
report(_msrc55._player_duration_ms(_p56, '/a/x.mp4') == 754000
       and _msrc55._player_duration_ms(_p56, 'https://h/y') == 1812000
       and _msrc55._player_duration_ms(_p56, '/a/unknown.mp4') == 0
       and _msrc55._player_duration_ms(None, '/a/x.mp4') == 0,
       "the linker reads the runtime the player already probed for that row")

_W56 = _msrc55.TitleMatcher._SIGNAL_WEIGHTS
report(_W56['duration'] > _W56['id'],
       'runtime now outweighs the creator-site video id',
       f"duration {_W56['duration']} > id {_W56['id']}")
report(_W56['id'] < _msrc55.TitleMatcher.HIGH_CONFIDENCE_STRENGTH,
       'and an id on its own can no longer auto-apply')

_src56 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'metadata_scraper.py'), encoding='utf-8').read()
report('_duration_seconds(' in _src56.split('def _site_movie_from_entry')[1].split('def ')[0]
       and '"duration":     _duration_seconds(data.get("duration"))' in _src56,
       'both scrapers store a runtime, so the criterion has something to read')
report('_player_duration_ms(self.player, fp)' in _src56
       and _src56.count('duration_ms=dur)') == 2,
       'and the linker passes each row runtime into the match -- on the '
       'single-network path and the all-network one alike')

# ── 57. Metadata DB stores only fields something reads back ──────────────────
# Measured on the shipped nubiles DB: model_details 1.46 MB and model_urls
# 0.79 MB were read by nothing, and image / trailer_url / description / tags /
# stats / _tags / _categories were filled in 0 of 5371 records. published_date,
# sources_seen and type were read by nothing. Together they were most of the
# file -- which is why the 71 MB teamskeet DB could not be pushed to GitHub.
print()
print('57. Metadata DB keeps only the name/actors/series/date fields that are used')

_K57 = _msrc55._MOVIE_KEEP_FIELDS
report(set(('slug', 'title', 'series', 'models', 'date')).issubset(set(_K57)),
       'the keep-list holds the four criteria that drive a match, plus the key')
report(all(k in _K57 for k in ('video_id', 'source_site', 'meta_fetched')),
       'and the tie-breaker, the site signal and the index gate')
report(not any(k in _K57 for k in ('scraped_at', 'source_name')),
       'but not scraped_at (0 reads in main.py or the scraper -- the top-level '
       'last_full_scrape already records when the DB was touched) nor '
       'source_name (1:1 with source_site, so _source_display_name derives it)',
       str(_K57))
report(not any(k in _K57 for k in ('model_details', 'model_urls', 'description',
                                   'tags', 'stats', '_tags', '_categories',
                                   'published_date', 'type')),
       'and none of the fields that were never read or never filled', str(_K57))
report(all(k in _K57 for k in ('sources_seen', 'image', 'trailer_url')),
       'except the load-bearing ones: sources_seen (the update early-stop '
       'depends on it), the cover, and the TeamSkeet per-scene trailer')

_fat57 = {
    'slug': 'x-1', 'title': 'Facials For My Stepsis', 'series': 'NubilesPorn',
    'models': ['Axel Haze'], 'date': '08/04/2026', 'video_id': '248755',
    'source_site': 'nubiles-porn', 'source_name': 'Nubiles Porn',
    'meta_fetched': True, 'scraped_at': '2026-08-04T00:00:00Z',
    'url': 'https://nubiles-porn.com/video/watch/248755/x',
    'model_details': [{'id': 'a', 'img': 'http://i/a.jpg', 'stats': {'v': 1}}],
    'model_urls': ['https://nubiles-porn.com/models/axel-haze'],
    'image': '', 'trailer_url': '', 'description': '', 'tags': [], 'stats': {},
    '_tags': [], '_categories': [], 'published_date': '', 'sources_seen': [],
    'type': 'video',
}
_thin57 = _msrc55._trim_movie(_fat57)
# _trim_movie drops empties as well as off-list fields, so the expectation
# has to allow for both.
_expect57 = [k for k in _K57 if _fat57.get(k) or k == 'slug']
report(list(_thin57) == _expect57,
       'a record is trimmed to the keep-list in a stable order',
       f'{len(_fat57)} fields -> {len(_thin57)}')
report(not any(k in _thin57 for k in ('model_details', 'model_urls',
                                      'description', 'tags', 'stats', '_tags',
                                      '_categories', 'published_date', 'type')),
       'no retired field survives the trim',
       f'{len(json.dumps(_fat57))} -> {len(json.dumps(_thin57))} bytes')
report(_msrc55._trim_movie(None) is None and _msrc55._trim_movie('x') == 'x',
       'a non-record passes through untouched instead of raising')

# A trimmed record must not look "changed" on the next scrape, or every
# update would rewrite the entire database.
report(_msrc55._movie_identity_changed(_thin57, _msrc55._trim_movie(_fat57)) is False,
       're-scraping the same movie into a trimmed record is not a change')
report(_msrc55._movie_identity_changed(
           _thin57, _msrc55._trim_movie(dict(_fat57, title='Something Else'))) is True,
       'but a real title change still is')

report(all(k in _thin57 for k in ('slug', 'title', 'series', 'models', 'date')),
       'the fields that drive a match survive the trim', str(sorted(_thin57)))
report('image' not in _thin57 and 'trailer_url' not in _thin57
       and 'sources_seen' not in _thin57,
       'and an empty cover, trailer or sources list is not stored at all -- '
       'every reader does movie.get(k) or default, so "" is the same as absent')

# source_name is derived now; if the derivation is wrong, 1806 nubiles movies
# with no series lose their network prefix.
report(_msrc55._source_display_name({'source_site': 'nubilefilms'}) == 'NubileFilms'
       and _msrc55._source_display_name({'source_site': 'momlover'}) == 'MomLover'
       and _msrc55._source_display_name({'source_site': 'brattysis'}) == 'BrattySis'
       and _msrc55._source_display_name({'source_site': 'nubiles-porn'}) == 'Nubiles Porn',
       'all four nubiles network names are derivable from source_site')
report(_msrc55._source_display_name({'source_site': 'mylf'}) == 'MYLF'
       and _msrc55._source_display_name({'source_site': 'pervz'}) == 'Pervz'
       and _msrc55._source_display_name({'source_site': 'familystrokes'}) == 'FamilyStrokes'
       and _msrc55._source_display_name({'source_site': 'swappz'}) == 'Swappz'
       and _msrc55._source_display_name({'source_site': 'teamskeet'}) == 'TeamSkeet',
       'and all five teamskeet ones')
report(_msrc55._source_display_name({}) == '' and _msrc55._source_display_name(None) == '',
       'with no source_site it returns empty rather than raising')
_noSeries = {'slug': 's', 'title': 'Some Scene', 'series': '', 'models': ['A B'],
             'date': '08/04/2026', 'source_site': 'nubilefilms'}
report(_msrc55.format_display_name(_noSeries).startswith('NubileFilms - '),
       'so a movie with no series still gets its network prefix',
       _msrc55.format_display_name(_noSeries)[:52])

# THE REGRESSION GUARD. `existing` comes off disk trimmed (empties dropped);
# `movie` is the raw builder output that still carries them. Comparing None
# against "" would report a change on every record at every scrape.
_rawNoSeries = dict(_fat57, series='', models=[])
_trimmedNoSeries = _msrc55._trim_movie(_rawNoSeries)
report('series' not in _trimmedNoSeries and 'models' not in _trimmedNoSeries,
       'a record with no series or models stores neither key')
report(_msrc55._movie_identity_changed(_trimmedNoSeries, _rawNoSeries) is False,
       'and re-scraping it against the raw builder output is still not a '
       'change -- the trimmed-vs-raw comparison is canonicalised')
report(_msrc55._movie_identity_changed(
           _trimmedNoSeries, dict(_rawNoSeries, series='BrattySis')) is True,
       'while a series appearing where there was none still is')

_dbp57 = os.path.join(_tf55.mkdtemp(), 'trim57.json')
_db57 = _msrc55.MetadataDB(_dbp57)
_db57.upsert(dict(_fat57))
report(sorted(_db57.movies['x-1']) == sorted(_expect57),
       'upsert stores the trimmed record, not the fat one',
       f"{len(_db57.movies['x-1'])} of {len(_fat57)} fields")
_db57.save()
_raw57 = json.load(open(_dbp57, encoding='utf-8'))['movies']['x-1']
report('model_details' not in _raw57 and 'model_urls' not in _raw57,
       'and nothing retired reaches the file')
_reload57 = _msrc55.MetadataDB(_dbp57)
_m57 = _reload57 and _msrc55.TitleMatcher(_reload57)
report(_m57.match('https://nubiles-porn.com/video/watch/248755/facials-for-my-stepsis')
       is not None,
       'a trimmed database still indexes and matches')

_legacy57 = os.path.join(_tf55.mkdtemp(), 'legacy57.json')
with open(_legacy57, 'w', encoding='utf-8') as _f57:
    json.dump({'movies': {'x-1': dict(_fat57)}}, _f57)
_before57 = os.path.getsize(_legacy57)
_ld57 = _msrc55.MetadataDB(_legacy57)
report(sorted(_ld57.movies['x-1']) == sorted(_expect57),
       'loading an older file drops the retired fields from memory',
       f"{len(_ld57.movies['x-1'])} of {len(_fat57)} fields")
_b57, _a57 = _ld57.compact()
report(_a57 < _before57, 'and compact() shrinks it on disk',
       f'{_before57} -> {_a57} bytes')

# ── 58. Gallery cover thumbnails ─────────────────────────────────────────────
# Every gallery card on nubiles-porn.com carries a cover at
# images.nubiles-porn.com/videos/<title_underscored>/samples/cover960.jpg,
# signed with st= and an e= expiry on the hour (~1 h after page load). The URL
# was already inside the HTML the scraper fetches; it was thrown away by a
# duplicate "image": "" key later in the same dict literal, which silently
# won -- so image was empty in all 5371 shipped records.
print()
print('58. Gallery scraper captures the cover thumbnail already in the page')

_HTML58 = """<html><body>
<div class="card">
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">
   <img src="https://images.nubiles-porn.com/videos/my_stepsis_is_a_hot_mess/samples/cover960.jpg?st=C5dl1YO4Wv&amp;e=1789617600" alt="x">
 </a>
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">My Stepsis Is A Hot Mess</a>
 <a href="https://nubiles-porn.com/model/profile/28550/arin-jones">Arin Jones</a>
 <a href="https://shesinmybed.com/">ShesInMyBed</a> &ndash; Sep 16, 2026
</div></body></html>"""
_MD58 = ('[![My Stepsis Is A Hot Mess](https://images.nubiles-porn.com/videos/'
         'my_stepsis_is_a_hot_mess/samples/cover960.jpg?st=Abc&e=1789621200)]'
         '(https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess)\n\n'
         '[My Stepsis Is A Hot Mess](https://nubiles-porn.com/video/watch/256651/'
         'my-stepsis-is-a-hot-mess)\n\n[Arin Jones](https://nubiles-porn.com/'
         'model/profile/28550/arin-jones)\n\n[ShesInMyBed](https://shesinmybed.com/)')

_pos58 = _HTML58.index('>My Stepsis Is A Hot Mess<')
_img58 = _msrc55._gallery_cover_image(_HTML58, _pos58)
report('cover960.jpg' in _img58 and _img58.startswith('https://images.nubiles-porn.com'),
       'the cover is read from the real HTML the browser session returns',
       _img58[:70])
report('cover960.jpg' in _msrc55._gallery_cover_image(_MD58, _MD58.rindex('My Stepsis')),
       'and from the markdown the reader fallback returns')
report(_msrc55._gallery_cover_image('<p>no images here</p>', 12) == '',
       'a card with no image yields an empty string, never a wrong one')

report(_msrc55._image_expiry_epoch('https://x/y.jpg?st=a&e=1789621200') == 1789621200
       and _msrc55._image_expiry_epoch('https://x/y.jpg') == 0
       and _msrc55._image_expiry_epoch(None) == 0,
       'the signature expiry is parsed from e=, and 0 when there is none')

_site58 = {"base_url": "https://nubiles-porn.com",
           "gallery_url": "https://nubiles-porn.com/video/gallery"}
_mv58 = _msrc55._gallery_movies_from_html(_HTML58, _site58)
report(len(_mv58) == 1 and _mv58[0]['slug'] == '256651-my-stepsis-is-a-hot-mess',
       'the gallery card still parses to one movie', str(len(_mv58)))
report('cover960.jpg' in _mv58[0].get('image', ''),
       'and that movie now carries its cover URL -- the duplicate "image": "" '
       'key no longer wipes it', _mv58[0].get('image', '')[:64])
report(_mv58[0].get('image_expires') == 1789617600,
       'with its expiry, so the player knows when the URL dies',
       str(_mv58[0].get('image_expires')))
report(_mv58[0]['models'] == ['Arin Jones'] and _mv58[0]['series'] == 'ShesInMyBed',
       'title, actors and series are unaffected')

# sources_seen is load-bearing: the gallery loop dedupes on it and flags
# page_changed when a source is added. Trimming it made the update early-stop
# unreachable, turning every Update into a full crawl.
report('sources_seen' in _msrc55._MOVIE_KEEP_FIELDS
       and 'image' in _msrc55._MOVIE_KEEP_FIELDS
       and 'image_expires' in _msrc55._MOVIE_KEEP_FIELDS,
       'sources_seen (the update early-stop) and the cover survive the trim')

# The cover the card paints is fetched into memory when the row is hovered,
# and never touches the disk -- a full disk made every download fail, and the
# URL was the only thing that mattered.
report(not any(hasattr(_msrc55, n) for n in (
           'save_thumbnail', 'thumbnails_dir', 'ensure_thumbnail_async',
           '_thumbnail_path_for_slug', '_THUMB_INFLIGHT')),
       'nothing in the scraper can write a cover to disk any more')
# Guarded so that reverting metadata_scraper.py to the version before the
# on-demand API yields failures rather than an AttributeError that truncates
# the run and hides every later assertion.
_API74 = ('cover_bytes', 'ensure_cover_async', 'signed_cover_url',
          'signed_media_url', 'refresh_signed_assets_async', '_COVER_CACHE',
          '_COVER_CACHE_LOCK', '_COVER_CACHE_MAX', '_COVER_INFLIGHT',
          '_REFRESH_LAST')
_have74 = all(hasattr(_msrc55, n) for n in _API74)
report(_have74,
       'the on-demand cover API is present',
       'missing: ' + ', '.join(n for n in _API74 if not hasattr(_msrc55, n)))
if _have74:
    report(_msrc55.cover_bytes('never-fetched-58') == b'',
           'an un-fetched cover reads as empty rather than raising')
    report(_msrc55.ensure_cover_async(object(), _msrc55.METADATA_SITES['nubiles'],
                                      '', 'https://x/y.jpg') is False
           and _msrc55.ensure_cover_async(
               None, _msrc55.METADATA_SITES['nubiles'], 'x', 'https://x/y.jpg') is False
           and _msrc55.ensure_cover_async(object(), _msrc55.METADATA_SITES['nubiles'],
                                          'x', '') is False,
           'and the fetch refuses to start without a player, a slug and a URL')
    _msrc55._COVER_CACHE.clear()
    for _i58 in range(_msrc55._COVER_CACHE_MAX + 30):
        with _msrc55._COVER_CACHE_LOCK:
            _msrc55._COVER_CACHE['cap-58-%d' % _i58] = b'x'
            while len(_msrc55._COVER_CACHE) > _msrc55._COVER_CACHE_MAX:
                _msrc55._COVER_CACHE.popitem(last=False)
    report(len(_msrc55._COVER_CACHE) == _msrc55._COVER_CACHE_MAX
           and _msrc55.cover_bytes('cap-58-0') == b'',
           'the memory cache is bounded, so hovering a whole playlist cannot grow it',
           str(len(_msrc55._COVER_CACHE)))
    _msrc55._COVER_CACHE.clear()

# ── 59. Hover preview loop ───────────────────────────────────────────────────
# Ground truth captured live from the gallery with a media sniffer (found.txt):
#   https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/videos/
#     loops/momsteachsex_stepmom_is_a_great_kisser_loop_480.mp4?st=..&e=..
# It is reproducible exactly from the title and series the DB already stores.
print()
print('59. Hover preview loop is derived from title + series, and captured when signed')

_FOUND59 = ('https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/'
            'videos/loops/momsteachsex_stepmom_is_a_great_kisser_loop_480.mp4')
report(_msrc55.preview_loop_url('Stepmom Is A Great Kisser', 'MomsTeachSex') == _FOUND59,
       'the derived loop URL matches the one captured from the live site',
       _msrc55.preview_loop_url('Stepmom Is A Great Kisser', 'MomsTeachSex')[-64:])
_mv59 = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'nubiles_metadata.json'), encoding='utf-8'))['movies']
_row59 = next(m for m in _mv59.values() if m.get('title') == 'Stepmom Is A Great Kisser')
report(_msrc55.preview_loop_url(_row59['title'], _row59['series']) == _FOUND59,
       'and it derives from the stored record, not just from hand-typed strings',
       f"{_row59['slug']} series={_row59['series']!r}")
report(_msrc55.preview_loop_url('Some Scene', '') .endswith('/videos/some_scene/videos/loops/some_scene_loop_480.mp4'),
       'a scene with no series still gets a loop path')
report(_msrc55.preview_loop_url('', 'MomsTeachSex') == '',
       'and no title means no invented path')
report(_msrc55._cdn_folder_name('Stepmom Is A Great Kisser') == 'stepmom_is_a_great_kisser'
       and _msrc55._cdn_folder_name('  WEIRD--Name!! 42 ') == 'weird_name_42',
       'titles are folded to the CDN folder form')

_SRC59 = ('<a href="/video/watch/1/x"><source src="https://images.nubiles-porn.com/videos/x/'
          'videos/loops/s_x_loop_480.mp4?st=a&e=1789621200"></a>'
          '<a href="/video/watch/1/x">Some Scene Title Here</a>')
_ATTR59 = _SRC59.replace('<source src="', '<a data-preview="').replace('"></a>', '">')
_p59 = _msrc55._gallery_preview_video(_SRC59, _SRC59.index('Some Scene Title Here'))
report('_loop_480.mp4' in _p59 and _msrc55._image_expiry_epoch(_p59) == 1789621200,
       'the signed loop is read out of a <source> tag, with its expiry', _p59[:64])
report('_loop_480.mp4' in _msrc55._gallery_preview_video(
           _ATTR59, _ATTR59.index('Some Scene Title Here')),
       'and out of a data-* attribute, since the player injects it on hover')
report(_msrc55._gallery_preview_video('<a href="/v">Some Scene Title Here</a>', 20) == '',
       'a card with no loop yields an empty string, never a wrong one')

_HTML59 = _SRC59.replace('/video/watch/1/x', 'https://nubiles-porn.com/video/watch/256294/stepmom-is-a-great-kisser-s27e1')
_HTML59 = _HTML59.replace('<a href="https://nubiles-porn.com/video/watch/256294/stepmom-is-a-great-kisser-s27e1"><source',
                          '<a href="https://nubiles-porn.com/video/watch/256294/stepmom-is-a-great-kisser-s27e1"><img src="https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/samples/cover960.jpg?st=z&e=1789621200"><source')
_pv59 = _msrc55._gallery_movies_from_html(_HTML59, _site58)
report(len(_pv59) == 1 and '_loop_480.mp4' in _pv59[0].get('preview', ''),
       'the gallery parser now stores the preview alongside the cover',
       _pv59[0].get('preview', '')[:60] or 'EMPTY')
report(_pv59[0].get('preview_expires') == 1789621200
       and _pv59[0].get('image_expires') == 1789621200,
       'both carrying the expiry, so the player knows the signed URL is dead '
       'in about an hour')
report('preview' in _msrc55._MOVIE_KEEP_FIELDS
       and 'preview_expires' in _msrc55._MOVIE_KEEP_FIELDS,
       'and both survive the field trim')

# ── 60. Instant hover preview for linked rows ────────────────────────────────
# Hover must not touch the network: the signed cover/preview URLs minted
# during a scrape die in about an hour. preview_info_for_path reads local
# data only, and rows renamed before metadata_links.json existed are
# recovered by matching the saved display name back through the matcher.
print()
print('60. Hover preview resolves a linked row, and fetches its cover behind it')

import shutil as _sh60

_work60 = _tf55.mkdtemp()
_sh60.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'nubiles_metadata.json'),
           os.path.join(_work60, 'nubiles_metadata.json'))


class _Player60:
    pass


_p60 = _Player60()
_p60.data_dir = _work60
_p60._metadata_dbs = {}
_p60._metadata_links = {}
_p60._metadata_name_overrides = {}
_p60._metadata_overrides_path = os.path.join(_work60, 'metadata_name_overrides.json')
_p60._metadata_links_path = os.path.join(_work60, 'metadata_links.json')
_p60._meta_norm_path = _msrc55._meta_norm_path

_db60 = _msrc55._metadata_db_for_site(_p60, _msrc55.METADATA_SITES['nubiles'])
_slug60 = '256294-stepmom-is-a-great-kisser-s27e1'
_name60 = _msrc55.format_display_name(_db60.movies[_slug60])
_row60 = 'https://pixeldrain.com/u/ksbtnekh'
_key60 = _msrc55._meta_norm_path(_row60)

report(_msrc55.preview_info_for_path(_p60, _row60) == {}
       and _msrc55.preview_info_for_path(None, _row60) == {}
       and _msrc55.preview_info_for_path(_p60, 'https://gofile.io/d/zzz') == {},
       'an unlinked row, a missing player and an unknown row all yield nothing')

# Legacy path: renamed before metadata_links.json existed, so only the
# display name is on disk. It has to be recovered by re-matching.
_p60._metadata_name_overrides[_key60] = _name60
_leg60 = _msrc55.preview_info_for_path(_p60, _row60)
report(_leg60.get('slug') == _slug60 and _leg60.get('site') == 'nubiles',
       'a row renamed before links were recorded is recovered by re-matching '
       'its saved display name', str(_leg60.get('slug')))
report(_leg60.get('preview_url', '').endswith(
           'momsteachsex_stepmom_is_a_great_kisser_loop_480.mp4'),
       'and its preview loop path is derived from the recovered record')

# New path: the linker recorded the movie it matched.
_p60._metadata_links[_key60] = {'site': 'nubiles', 'slug': _slug60, 'name': _name60}
if _have74:
    _msrc55._COVER_CACHE[_slug60] = b'\xff\xd8' + b'0' * 900
_new60 = _msrc55.preview_info_for_path(_p60, _row60)
report(_new60.get('slug') == _slug60 and len(_new60.get('cover_data') or b'') > 512,
       'a linked row resolves straight to its movie and its cover in memory',
       f'{len(_new60.get("cover_data") or b"")} bytes')
report(_new60.get('name') == _name60 and _new60.get('site_name') == 'Nubiles-Porn',
       'and carries the display name and network for the tooltip')
report(_new60.get('image_live') is False and _new60.get('preview_live') is False,
       'a signed URL past its expiry is reported as dead, not offered')

import time as _t60
_fut60 = int(_t60.time()) + 3600
_db60.movies[_slug60]['preview'] = 'https://images.nubiles-porn.com/x.mp4?st=a&e=%d' % _fut60
_db60.movies[_slug60]['preview_expires'] = _fut60
report(_msrc55.preview_info_for_path(_p60, _row60).get('preview_live') is True,
       'and one still inside its hour is reported as live')

# The playlist side, checked structurally: main.py is too large to import.
_main60 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'main.py'), encoding='utf-8').read()
_ast60 = ast.parse(_main60)
_cls60 = next(n for n in ast.walk(_ast60)
              if isinstance(n, ast.ClassDef) and n.name == 'DraggableTableWidget')
_meth60 = {n.name: n for n in _cls60.body if isinstance(n, ast.FunctionDef)}
# The hover card belongs to the player, not to the table widget: the player's
# eventFilter drives self.hover_preview and honours the user's hover/click
# trigger mode. The table only has to keep mouse tracking on so those hover
# moves arrive.
report('setMouseTracking(True)' in ast.get_source_segment(_main60, _meth60['__init__']),
       'hover works without a button held, because mouse tracking is on')
report('preview_info_for_path' in ast.dump(
           next(n for n in ast.walk(_ast60)
                if isinstance(n, ast.ImportFrom) and n.module == 'metadata_scraper')),
       'main.py imports the lookup rather than reimplementing it')
_vp60 = next(n for n in ast.walk(_ast60)
             if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer')
_meth60v = {n.name: n for n in _vp60.body if isinstance(n, ast.FunctionDef)}
_hov60 = ast.get_source_segment(_main60, _meth60v['_show_hover_preview'])
report('preview_info_for_path' in _hov60,
       'and the player hover card it already had is the one that reads the '
       'metadata -- the table does not raise a popup of its own')

# ── 61. Update stays cheap, and dead signed URLs are not hoarded ─────────────
print()
print('61. Update converges on the first pass and expired signed URLs are pruned')

# A page of movies that are all already known must not count as changed, or
# the "caught up" early-stop never fires and every Update crawls all 175
# pages of all four gallery sources. Simulated against the shipped DB, whose
# records carry almost no sources_seen.
_real61 = _msrc55.MetadataDB(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'nubiles_metadata.json'))
_by61 = {}
for _k61, _m61 in _real61.movies.items():
    _by61.setdefault(str(_m61.get('source_site')), []).append(_k61)
_seen61 = sum(1 for _m in _real61.movies.values() if _m.get('sources_seen'))
report(_seen61 < max(1, len(_real61.movies) // 100),
       'the shipped DB has almost no sources_seen, so the backfill is the live '
       'case', f'{_seen61} of {len(_real61.movies)}')

_movies61 = {k: dict(v) for k, v in _real61.movies.items()}
_up61 = _dup61 = 0
for _k61 in _by61['nubilefilms'][:50]:
    _ex = _movies61[_k61]
    _seen = _ex.get('sources_seen')
    if _seen is None:
        _seed = str(_ex.get('source_site') or 'nubilefilms')
        _seen = [_seed] if _seed else []
    else:
        _seen = list(_seen)
    if 'nubilefilms' not in _seen:
        _up61 += 1
    else:
        _dup61 += 1
report(_up61 == 0 and _dup61 == 50,
       'a page of known movies is all duplicates, so page_changed stays False '
       'and the early-stop can fire', f'{_up61} upserts / {_dup61} duplicates')

_run61 = ast.get_source_segment(
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'metadata_scraper.py'), encoding='utf-8').read(),
    next(n for n in ast.walk(ast.parse(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'metadata_scraper.py'),
        encoding='utf-8').read()))
         if isinstance(n, ast.ClassDef) and n.name == 'NetworkGalleryScraper'))
report('sources_seen' in _run61 and 'source_site' in _run61,
       'the gallery loop backfills sources_seen from source_site so the '
       'bookkeeping converges instead of being re-added every scrape')

# A signed URL past its expiry 403s forever, so keeping it is dead weight:
# about 280 bytes per record, which on a 45k-movie database is ~12 MB of
# strings that can never be used again.
_now61 = int(time.time())
_p61 = os.path.join(_tf55.mkdtemp(), 'prune61.json')
with open(_p61, 'w', encoding='utf-8') as _f61:
    json.dump({'movies': {'x-1': {
        'slug': 'x-1', 'title': 'T', 'series': 'S', 'models': ['A'],
        'date': '08/04/2026', 'meta_fetched': True,
        'image': 'https://i/c.jpg?st=a&e=%d' % (_now61 - 3600),
        'image_expires': _now61 - 3600,
        'preview': 'https://i/v.mp4?st=a&e=%d' % (_now61 + 3600),
        'preview_expires': _now61 + 3600,
    }}}, _f61)
_db61 = _msrc55.MetadataDB(_p61)
report('image' not in _db61.movies['x-1'] and 'image_expires' not in _db61.movies['x-1'],
       'an expired signed cover URL is dropped on load')
report('preview' in _db61.movies['x-1'] and 'preview_expires' in _db61.movies['x-1'],
       'and one still inside its hour is kept')
report(_msrc55.preview_loop_url('T', 'S').endswith('/videos/t/videos/loops/s_t_loop_480.mp4'),
       'pruning the preview loses nothing -- the path is re-derivable')

# ── 62. TeamSkeet: 90% of the DB was dead weight, and its covers never expire ─
# The real 74.50 MB / 10586-movie teamskeet DB held _tags 9.13 MB, tags 9.12,
# seo 8.38, description_html 7.11, description 7.02 and model_details 4.40 --
# 45 MB read by nothing. Its image and trailer_url, by contrast, are per-scene
# URLs on images.psmcdn.net with NO signature: a real path serves bytes while
# an invented one returns an "origin error..." page, so they are public and
# permanent, unlike nubiles' hour-long signed URLs.
print()
print('62. TeamSkeet DB is trimmed to the useful fields and its covers stay live')

report('trailer_url' in _msrc55._MOVIE_KEEP_FIELDS,
       "teamskeet's per-scene trailer mp4 is kept -- it is the cheapest "
       'working video preview either network offers')

_ts62p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'teamskeet_metadata.json')
if os.path.isfile(_ts62p):
    _ts62 = json.load(open(_ts62p, encoding='utf-8'))['movies']
    _ks62 = set()
    for _m62 in _ts62.values():
        _ks62 |= set(_m62)
    report(_ks62 <= set(_msrc55._MOVIE_KEEP_FIELDS),
           'every field in the shipped teamskeet DB is on the keep-list',
           str(sorted(_ks62)))
    report(not _ks62 & {'tags', '_tags', 'seo', 'description', 'description_html',
                        'model_details', 'model_urls', 'stats', 'sitelogo',
                        'published_date', 'series_url', 'item_id', 'type'},
           'the 45 MB of never-read fields is gone')
    report(sum(1 for m in _ts62.values() if m.get('image')) > 10000
           and sum(1 for m in _ts62.values() if m.get('trailer_url')) > 10000,
           'and the cover plus trailer survived for essentially every movie',
           f"{sum(1 for m in _ts62.values() if m.get('image'))} covers / "
           f"{sum(1 for m in _ts62.values() if m.get('trailer_url'))} trailers")
    report(os.path.getsize(_ts62p) < 12_000_000,
           'the file is a fraction of the 74.50 MB it was',
           f'{os.path.getsize(_ts62p)/1e6:.2f} MB')
    report(all('?' not in str(m.get('image') or '') for m in _ts62.values()
               if m.get('image')),
           'no teamskeet cover URL carries a signature, so none can expire')

# An unsigned URL has no expiry, so it must read as live. Requiring
# exp > now -- correct for nubiles -- made every teamskeet row report a
# dead cover.
class _Player62:
    pass


_p62 = _Player62()
_p62.data_dir = _tf55.mkdtemp()
_p62._metadata_dbs = {}
_p62._metadata_links = {}
_p62._metadata_name_overrides = {}
_p62._meta_norm_path = _msrc55._meta_norm_path
_dbp62 = os.path.join(_p62.data_dir, 'ts62.json')
with open(_dbp62, 'w', encoding='utf-8') as _f62:
    json.dump({'movies': {'scene-a': {
        'slug': 'scene-a', 'title': 'Some TeamSkeet Scene', 'series': 'Milfty',
        'models': ['Christie Stevens'], 'date': '05/23/2026', 'meta_fetched': True,
        'image': 'https://images.psmcdn.net/teamskeet/mfy/x/shared/med.jpg',
        'trailer_url': 'https://images.psmcdn.net/mfy/tour/pics/x/bio_small.mp4',
    }}}, _f62)
_site62 = dict(_msrc55.METADATA_SITES['teamskeet'])
_site62['db_filename'] = 'ts62.json'
_orig_sites62 = _msrc55.METADATA_SITES['teamskeet']
_msvc62 = _msrc55.METADATA_SITES
try:
    _msvc62['teamskeet'] = _site62
    _row62 = 'https://pixeldrain.com/u/ts_row'
    _p62._metadata_name_overrides[_msrc55._meta_norm_path(_row62)] = \
        _msrc55.format_display_name(json.load(open(_dbp62, encoding='utf-8'))['movies']['scene-a'])
    _i62 = _msrc55.preview_info_for_path(_p62, _row62)
    report(_i62.get('image_live') is True,
           'a teamskeet cover with no expiry is reported as live, not dead')
    report(_i62.get('slug') == 'scene-a' and 'psmcdn.net' in (_i62.get('image') or ''),
           'and a renamed row resolves to its teamskeet movie and cover',
           str(_i62.get('slug')))
finally:
    _msvc62['teamskeet'] = _orig_sites62

# The background fetch must collapse concurrent requests for one cover.
# The fetch is gated so the first one is genuinely still in flight when the
# second hover arrives. Without the gate the daemon thread can finish first,
# and a completed fetch SHOULD release the slot -- so the test would be
# asserting the wrong thing rather than the code being wrong.
#
# A slug of its own, too: the preview_info_for_path call above already
# started a real background fetch for 'scene-a', and because the in-flight
# set is keyed on (site, slug) that older thread's discard can clear the
# slot the newer one owns. In production that costs at worst a second
# download of the same cover -- idempotent -- so it is not worth tracking
# slot ownership for; here it would just make the test flaky.
_calls62 = []
_gate62 = threading.Event()
_orig_fetch62 = _msrc55._fetch_bytes


def _slow62(url, timeout=20, referer='', diag=None):
    _calls62.append(url)
    _gate62.wait(5)
    return b'\xff\xd8' + b'0' * 900


try:
    _msrc55._fetch_bytes = _slow62
    if _have74:
        _msrc55._COVER_INFLIGHT.clear()
        _msrc55._COVER_CACHE.clear()
        _a62 = _msrc55.ensure_cover_async(_p62, _site62, 'dedupe-62',
                                          'https://i/c.jpg')
        _b62 = _msrc55.ensure_cover_async(_p62, _site62, 'dedupe-62',
                                          'https://i/c.jpg')
        report(_a62 is True and _b62 is False,
               'a second hover on the same row does not start a second download',
               f'{len(_calls62)} fetch(es) started')
        report(_msrc55.ensure_cover_async(_p62, _site62, '', 'https://i/c.jpg') is False
               and _msrc55.ensure_cover_async(None, _site62, 'x', 'https://i/c.jpg') is False,
               'and it refuses to run without a slug or a player')
    else:
        report(False, 'a second hover on the same row does not start a second '
                      'download: the on-demand cover API is absent')
finally:
    _gate62.set()
    _msrc55._fetch_bytes = _orig_fetch62
    if _have74:
        _msrc55._COVER_INFLIGHT.clear()
        _msrc55._COVER_CACHE.clear()

# Future scrapes must stay lean: the teamskeet builder still produces the fat
# record, so upsert has to be what trims it.
_entry62 = {
    'id': 'some-scene', 'videoTitle': 'Some Scene', 'publishedDate': '2026-05-23',
    'models': [{'name': 'Christie Stevens', 'id': 'christie-stevens'}],
    'site': {'name': 'Milfty', 'nickName': 'mfy'},
    'tags': ['Blowjob', 'Brunette'], 'description': '<p>a long description</p>',
    'img': 'https://images.psmcdn.net/x.jpg', 'videoTrailer': 'https://images.psmcdn.net/t.mp4',
    'stats': {'views': 1}, 'seo': {'title': 'x', 'description': 'y'},
    'videoSrc': 'abc123', 'itemId': 9, 'type': 'video', 'isUpcoming': 'false',
}
_built62 = _msrc55._site_movie_from_entry(_entry62, {'id': 'teamskeet', 'base_url': 'https://www.teamskeet.com'})
report(len(_built62) > len(_msrc55._MOVIE_KEEP_FIELDS),
       'the teamskeet builder still produces the fat record',
       f"{len(_built62)} fields built")
_thin62 = _msrc55._trim_movie(_built62)
report(set(_thin62) <= set(_msrc55._MOVIE_KEEP_FIELDS)
       and _thin62.get('image') == 'https://images.psmcdn.net/x.jpg'
       and _thin62.get('trailer_url') == 'https://images.psmcdn.net/t.mp4',
       'and upsert trims it to the keep-list while keeping cover and trailer',
       f"{len(_built62)} -> {len(_thin62)} fields")

# trailer_url now has a reader. Before this it was kept and nothing consumed
# it, and worse: preview_url fell through to preview_loop_url(), which mints
# an images.nubiles-porn.com URL, so a TeamSkeet row was handed a nubiles
# address that cannot exist.
report(_i62.get('preview_url') == 'https://images.psmcdn.net/mfy/tour/pics/x/bio_small.mp4',
       'a TeamSkeet row previews from its own stored trailer',
       str(_i62.get('preview_url'))[:70])
report('nubiles-porn.com' not in str(_i62.get('preview_url') or ''),
       'and is no longer handed a fabricated nubiles loop URL')
report(_i62.get('preview_live') is True,
       'the unsigned trailer is reported as a live preview')

_nb62 = os.path.join(_p62.data_dir, 'nb62.json')
with open(_nb62, 'w', encoding='utf-8') as _f62:
    json.dump({'movies': {'stepmom-is-a-great-kisser': {
        'slug': 'stepmom-is-a-great-kisser', 'title': 'Stepmom Is A Great Kisser',
        'series': 'MomsTeachSex', 'models': [], 'date': '09/12/2026',
        'meta_fetched': True, 'image': '', 'trailer_url': '',
    }}}, _f62)
_nbs62 = dict(_msvc62['nubiles'])
_nbs62['db_filename'] = 'nb62.json'
_ob62 = _msvc62['nubiles']
try:
    _msvc62['nubiles'] = _nbs62
    _row62b = 'https://pixeldrain.com/u/nb_row'
    _p62._metadata_name_overrides[_msrc55._meta_norm_path(_row62b)] = \
        _msrc55.format_display_name(json.load(open(_nb62, encoding='utf-8'))['movies']['stepmom-is-a-great-kisser'])
    _j62 = _msrc55.preview_info_for_path(_p62, _row62b)
    report(_j62.get('preview_url') == (
               'https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/'
               'videos/loops/momsteachsex_stepmom_is_a_great_kisser_loop_480.mp4'),
           'a nubiles row still derives its loop URL', str(_j62.get('preview_url'))[-46:])
    report(_j62.get('preview_live') is False,
           'and still reports it as not live, since unsigned it 403s')
finally:
    _msvc62['nubiles'] = _ob62

# ── 63. Dates are stored DD/MM/YYYY ───────────────────────────────────────────
# Both networks send an unambiguous source value: teamskeet an ISO stamp
# (2026-05-23T00:00:00, the shape stored as published_date on all 10564
# records of the original export), nubiles a month name ("May 26, 2026").
# The stored MM/DD/YYYY was a leftover from an older build. Every correction
# below was checked against published_date_iso, which is exact ground truth.
print()
print('63. Dates are stored and shown as DD/MM/YYYY')

report(_msrc55._date_to_display('2026-05-23T00:00:00') == ('23/05/2026', '2026-05-23'),
       'the real teamskeet API stamp converts to DD/MM/YYYY',
       str(_msrc55._date_to_display('2026-05-23T00:00:00')))
report(_msrc55._date_to_display('May 26, 2026') == ('26/05/2026', '2026-05-26'),
       'and so does the nubiles gallery month name')
report(_msrc55._date_to_display('09/16/2026') == ('16/09/2026', '2026-09-16'),
       'a slash date that can only be MM/DD is swapped')
report(_msrc55._date_to_display('16/09/2026') == ('16/09/2026', '2026-09-16'),
       'one that can only be DD/MM is left alone')
report(_msrc55._date_to_display('2026-09-16') == ('16/09/2026', '2026-09-16')
       and _msrc55._date_to_display('20260916') == ('16/09/2026', '2026-09-16'),
       'and ISO / compact forms agree')

# The matcher strips separators from the query, so a record has to offer every
# ordering or a valid date stops matching the moment the format changes.
_k63 = _msrc55._date_keys('23/05/2026')
report(_k63 == {'20260523', '23052026', '05232026'},
       'a stored date is indexed under ISO, DD/MM and MM/DD digit orders',
       str(sorted(_k63)))
for _q63 in ('23/05/2026', '23-05-2026', '2026-05-23', '05/23/2026'):
    report(bool({re.sub(r'[^0-9]', '', _q63)} & _k63),
           f'a query written as {_q63!r} still hits the date signal')

report(_msrc55._normalise_display_date('09/16/2026') == '16/09/2026',
       'a legacy MM/DD value is still swapped for display')
report(_msrc55._normalise_display_date('16/09/2026') == '16/09/2026'
       and _msrc55._normalise_display_date('05/07/2026') == '05/07/2026',
       'but an ambiguous one is left alone -- guessing there is what produced '
       'the mixed display in the first place')

_d63 = os.path.join(_tf55.mkdtemp(), 'legacy63.json')
with open(_d63, 'w', encoding='utf-8') as _f63:
    json.dump({'movies': {
        'a': {'slug': 'a', 'title': 'A', 'date': '09/16/2026', 'meta_fetched': True},
        'b': {'slug': 'b', 'title': 'B', 'date': '16/09/2026', 'meta_fetched': True},
        'c': {'slug': 'c', 'title': 'C', 'date': '05/07/2026', 'meta_fetched': True},
    }}, _f63)
_db63 = _msrc55.MetadataDB(_d63)
_g63 = {k: v.get('date') for k, v in _db63.movies.items()}
report(_g63 == {'a': '16/09/2026', 'b': '16/09/2026', 'c': '05/07/2026'},
       'loading a legacy database rewrites its MM/DD dates on the spot',
       str(_g63))
report(json.load(open(_d63, encoding='utf-8'))['movies']['a']['date'] == '16/09/2026',
       'and the correction reaches the file, not just memory')
_db63b = _msrc55.MetadataDB(_d63)
report({k: v.get('date') for k, v in _db63b.movies.items()} == _g63,
       'reloading is idempotent')

_R63 = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')
for _fn63 in ('teamskeet_metadata.json', 'nubiles_metadata.json',
              'momlover_metadata.json'):
    _p63 = os.path.join(os.path.dirname(os.path.abspath(__file__)), _fn63)
    if not os.path.isfile(_p63):
        continue
    _m63 = json.load(open(_p63, encoding='utf-8'))['movies']
    _mm63 = sum(1 for m in _m63.values()
                if (_g := _R63.match(m.get('date') or '')) and int(_g.group(2)) > 12)
    report(_mm63 == 0, f'{_fn63} holds no MM/DD/YYYY date',
           f'{len(_m63)} records, {_mm63} still MM/DD')
    report(all(_msrc55._normalise_display_date(m.get('date') or '') == (m.get('date') or '')
               for m in _m63.values()),
           f'and the display normaliser is a no-op on it')

# ── 64. The metadata cover and clip go into the EXISTING hover card ────────────
# The player already had a hover preview card (self.hover_preview, a QFrame
# with hover_preview_image / hover_preview_text, driven by _show_hover_preview
# from the playlist eventFilter, honouring the user's hover/click trigger
# mode). A second popup competing with it on the same widget was wrong, so
# the cover and clip are added to the card that was already there.
print()
print('64. Metadata enriches the existing hover card instead of adding a popup')

report('MetadataHoverPopup' not in SRC,
       'the second popup is gone')
_cls64b = next((n for n in TREE.body if isinstance(n, ast.ClassDef)
                and n.name == 'DraggableTableWidget'), None)
_m64b = {n.name for n in _cls64b.body if isinstance(n, ast.FunctionDef)}
report('_show_metadata_hover_preview' not in _m64b
       and '_schedule_metadata_hover' not in _m64b
       and 'leaveEvent' not in _m64b,
       'and the playlist table no longer runs its own competing hover timer')
report('setMouseTracking(True)' in ast.get_source_segment(SRC, _cls64b),
       'mouse tracking stays on, because the player eventFilter needs hover moves')

def _vsrc64(name):
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == 'VideoPlayer')
    fn = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name == name), None)
    return ast.get_source_segment(SRC, fn) if fn is not None else ''

_show64 = _vsrc64('_show_hover_preview')
report('preview_info_for_path' in _show64,
       'the established card consults the metadata linker')
report('_metadata_cover_pixmap' in _show64 and '_start_metadata_hover_clip' in _show64,
       'and wires in the cover and the clip')
report('QVideoWidget(self.hover_preview)' in SRC,
       'the clip plays in a surface that belongs to that same card')
report('frames = [_cover' in _show64,
       'the cover is prepended to the frame list, so it is what shows first')

_clip64 = _vsrc64('_start_metadata_hover_clip')
report('preview_live' in _clip64 and 'preview_url' in _clip64,
       'the clip is only requested when the movie actually has a live one')
report('_hover_clip_timer.start()' in _clip64,
       'with a timeout, so a dead URL cannot leave the card stuck')
_st64b = _vsrc64('_on_hover_clip_status')
report('_swap_to_hover_clip' in _st64b,
       'a buffered clip hands over to the swap')
report('LoadedMedia' in _st64b and 'BufferedMedia' in _st64b,
       'and only on a buffered status')
_ab64b = _vsrc64('_abandon_hover_clip')
report('hover_preview_image.show()' in _ab64b,
       'a row with no working clip falls back to the cover')
_hide64 = _vsrc64('_hide_hover_preview')
report('_stop_metadata_hover_clip' in _hide64,
       'and hiding the card stops the clip')

# ── 65. An Update backfills a cover onto a record that never had one ───────────
# All 5371 nubiles records predate cover capture, and the dedupe branch used
# to `continue` without touching them -- so nothing would ever give them a
# cover, because the signed URL only exists while the gallery page is read.
print()
print('65. Update backfills the cover onto records scraped before capture')

_HTML65 = """<html><body><div class="card">
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">
   <img src="https://images.nubiles-porn.com/videos/my_stepsis_is_a_hot_mess/samples/cover960.jpg?st=C5dl1YO4Wv&amp;e=9999999999"></a>
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">My Stepsis Is A Hot Mess</a>
 <a href="https://nubiles-porn.com/model/profile/28550/arin-jones">Arin Jones</a>
 <a href="https://shesinmybed.com/">ShesInMyBed</a> &ndash; Sep 16, 2026
</div></body></html>"""

class _Sig65:
    def emit(self, *a):
        pass


class _Sigs65:
    progress = _Sig65()
    tick = _Sig65()
    finished = _Sig65()
    error = _Sig65()


_d65 = os.path.join(_tf55.mkdtemp(), 'bf65.json')
with open(_d65, 'w', encoding='utf-8') as _f65:
    json.dump({'movies': {'256651-my-stepsis-is-a-hot-mess': {
        'slug': '256651-my-stepsis-is-a-hot-mess', 'title': 'My Stepsis Is A Hot Mess',
        'series': 'ShesInMyBed', 'models': ['Arin Jones'], 'date': '16/09/2026',
        'video_id': '256651', 'meta_fetched': True,
        'url': 'https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess',
        'source_site': 'shesinmybed', 'sources_seen': ['shesinmybed'],
    }}}, _f65)
_db65 = _msrc55.MetadataDB(_d65)
report(not _db65.movies['256651-my-stepsis-is-a-hot-mess'].get('image'),
       'the record starts with no cover, like all 5371 shipped nubiles ones')

_site65 = dict(_msrc55.METADATA_SITES['nubiles'])
_site65['gallery_sources'] = [{'id': 'shesinmybed', 'name': 'ShesInMyBed',
                               'base_url': 'https://nubiles-porn.com',
                               'gallery_url': 'https://nubiles-porn.com/video/gallery',
                               'page_url_template': 'https://nubiles-porn.com/video/gallery/{offset}'}]
_scr65 = _msrc55.NetworkGalleryScraper(_db65, _site65, mode='update')
_scr65.signals = _Sigs65()
_scr65._fetch_gallery_page = lambda page: _HTML65 if page == 1 else ''
_saved65 = []
_orig65 = _msrc55._fetch_bytes
_msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: (
    _saved65.append((url, referer)), b'x' * 900)[1]
try:
    _scr65._run()
finally:
    _msrc55._fetch_bytes = _orig65

_after65 = _msrc55.MetadataDB(_d65).movies['256651-my-stepsis-is-a-hot-mess']
report('cover960.jpg' in str(_after65.get('image') or ''),
       'a known record gains the cover from the card it already appeared on',
       str(_after65.get('image'))[:64])
report(int(_after65.get('image_expires') or 0) == 9999999999,
       'along with its signature expiry, so a dead one can be pruned later')
report(not _saved65,
       'and the scrape downloads nothing -- the bytes are fetched on hover',
       f'{len(_saved65)} fetch(es)')
report(not os.path.isdir(os.path.join(os.path.dirname(_d65), 'thumbnails')),
       'so no thumbnails/ folder appears beside the database')
report(_after65.get('sources_seen') == ['shesinmybed'],
       'without disturbing the sources_seen bookkeeping')

# Second pass: the record now has a cover, so nothing is rewritten and the
# "caught up" early-stop can fire again.
_scr65b = _msrc55.NetworkGalleryScraper(_msrc55.MetadataDB(_d65), _site65, mode='update')
_scr65b.signals = _Sigs65()
_scr65b._fetch_gallery_page = lambda page: _HTML65 if page == 1 else ''
_saved65.clear()
# The stub has to be re-installed here: without it _saved65 could never grow,
# and the assertion below would pass no matter what the second pass did.
_msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: (
    _saved65.append((url, referer)), b'x' * 900)[1]
try:
    _scr65b._run()
finally:
    _msrc55._fetch_bytes = _orig65
report(not _saved65,
       'a second pass backfills nothing, so the crawl converges again',
       f'{len(_saved65)} fetches')
_after65b = _msrc55.MetadataDB(_d65).movies['256651-my-stepsis-is-a-hot-mess']
report(int(_after65b.get('image_expires') or 0) == 9999999999
       and 'cover960.jpg' in str(_after65b.get('image') or ''),
       'and the cover it already had is left exactly as it was')

# ── 66. Card placement on first show, and a 1 s hold on the cover ─────────────
print()
print('66. The card is placed correctly the first time and holds the cover 1 s')

_mv66 = _vsrc64('_move_preview_widget')
report('sizeHint()' in _mv66 and '2 * edge' in _mv66,
       'the card size is taken from its size hint and clamped before the edge '
       'test, so a pre-layout size '
       'cannot flip it into the top-left corner')
report('self.hover_preview.width() > ' not in _mv66,
       'and the raw pre-layout size is no longer compared directly')
_show66 = _vsrc64('_show_hover_preview')
report('hover_preview.show()' in _show66
       and '_reposition_hover_preview()' in _show66
       and _show66.index('hover_preview.show()') < _show66.index('_reposition_hover_preview()'),
       'it is repositioned once more after show(), when the layout is live')

report(lift_attr('VideoPlayer', 'HOVER_COVER_HOLD_MS') == 1000,
       'the cover is held for a full second before the clip takes over',
       str(lift_attr('VideoPlayer', 'HOVER_COVER_HOLD_MS')))
_init66 = _vsrc64('__init__')
report('HOVER_COVER_HOLD_MS' in _init66 and '_hover_cover_hold_timer.setSingleShot(True)' in _init66,
       'via a single-shot timer wired to the swap')
_clip66 = _vsrc64('_start_metadata_hover_clip')
report('_hover_cover_hold_timer.start()' in _clip66,
       'the hold starts when the clip is requested')
report(_clip66.index('_hover_cover_hold_timer.start()') < _clip66.index('setSource'),
       'and buffering starts during the hold, so the swap is not delayed by it')
_swap66 = _vsrc64('_swap_to_hover_clip')
report('_hover_clip_ready' in _swap66 and '_hover_cover_hold_timer.isActive()' in _swap66,
       'the swap needs both the clip buffered and the hold elapsed')
report('hover_preview_image.hide()' in _swap66 and 'hover_preview_video.show()' in _swap66
       and '_hover_clip_player.play()' in _swap66,
       'and it is what performs the swap')
report('_swap_to_hover_clip' in _vsrc64('_on_hover_clip_status'),
       'either side can arrive last, so both call it')
_ab66 = _vsrc64('_abandon_hover_clip')
report('_hover_cover_hold_timer.stop()' in _ab66,
       'giving up on the clip also cancels the hold, leaving the cover up')
report('_hover_cover_hold_timer.stop()' in _vsrc64('_stop_metadata_hover_clip'),
       'and so does hiding the card')

# ── 67. A gallery that lazy-loads still yields its cover ───────────────────────
# _IMG_SRC_RE only ever matched <img src="...">. A gallery that lazy-loads
# ships the real URL in data-src and a placeholder in src, so the cover came
# back empty -- indistinguishable, from the outside, from the site having no
# covers at all. That is what an empty image field looked like on all 5371
# nubiles records.
print()
print('67. The cover is found however the gallery markup carries it')

_COV67 = ("https://images.nubiles-porn.com/videos/my_stepsis/samples/"
          "cover960.jpg?st=A&e=1")
_T67 = 'My Stepsis Is A Hot Mess'


def _hit67(html, at=None):
    return _msrc55._gallery_cover_image(html, at if at is not None else html.index(_T67))


report('cover960' in _hit67(f'<img src="{_COV67}"><a>{_T67}</a>'),
       'a plain <img src> still works')
report('cover960' in _hit67(f'<img data-src="{_COV67}" src="data:image/gif;base64,R0"><a>{_T67}</a>'),
       'a lazy-loaded card carrying the URL in data-src is found',
       'this is the case that returned empty on every nubiles record')
report('cover960' in _hit67(f'<img data-original="{_COV67}" src="/ph.png"><a>{_T67}</a>'),
       'and so is data-original')
report('cover960' in _hit67(f'<img srcset="{_COV67} 960w, /small.jpg 320w"><a>{_T67}</a>'),
       'and srcset, taking the first candidate')
report('cover960' in _hit67(f'<a>{_T67}</a><img src="{_COV67}">'),
       'and a card whose image sits AFTER the title')
report('cover960' in _hit67(f'<div style="background-image:url({_COV67})"><a>{_T67}</a></div>'),
       'and a CSS background image')
_MD67 = (f'[![{_T67}]({_COV67})](https://nubiles-porn.com/video/watch/1/x)\n\n'
         f'[{_T67}](https://nubiles-porn.com/video/watch/1/x)')
report('cover960' in _hit67(_MD67, _MD67.rindex(_T67)),
       'and the markdown the reader fallback returns, at the anchor the caller uses')
report(_hit67(f'<img src="https://nubiles-porn.com/model/profile/28550/arin-jones.jpg"><a>{_T67}</a>') == '',
       'a model headshot is still not mistaken for the cover')
report(_hit67(f'<a>{_T67}</a>') == '',
       'and a card with no image yields empty rather than a wrong one')

_run67 = ast.get_source_segment(
    SRC if False else open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'metadata_scraper.py'), encoding='utf-8').read(),
    next(n for n in ast.walk(ast.parse(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'metadata_scraper.py'),
        encoding='utf-8').read()))
         if isinstance(n, ast.ClassDef) and n.name == 'NetworkGalleryScraper'))
report('covers {covers}' in _run67 or 'covers ' in _run67,
       'the scrape now reports how many covers it captured per page, so a '
       'markup change is visible instead of silently yielding nothing')
report('cover(s) captured' in _run67,
       'and totals them per source')

_poll67 = _vsrc64('_poll_hover_cover')
report('_metadata_cover_pixmap' in _poll67 and 'setPixmap' in _poll67,
       'a cover that is still downloading is picked up and painted')
report('_hover_cover_poll_tries > 25' in _poll67,
       'with a bound, so it cannot poll forever')
_show67 = _vsrc64('_show_hover_preview')
report('_hover_cover_poll_timer.start()' in _show67 and 'image_live' in _show67,
       'the poll only starts when there is a live URL and no file yet')
report('_hover_cover_poll_timer.stop()' in _vsrc64('_hide_hover_preview'),
       'and hiding the card stops it')

# ── 68. The whole cover chain, end to end ─────────────────────────────────────
# A full re-scrape on 2026-09-17 produced 5374 nubiles records with zero covers
# while every one of them had a title, models, date and url. So page fetching
# and anchor parsing worked and only the cover step failed -- the real URLs were
# in data-srcset and the shared request headers named the wrong network.
# compact_records() then deleted the dead URL, so the database read the same as
# a gallery with no covers at all. This drives the real scraper over a
# lazy-loaded card and asserts the URL is captured and nothing reaches disk.
print()
print('68. A lazy-loaded gallery card yields its cover URL and no file')

_HTML68 = """<html><body><div class="card">
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">
   <img loading="lazy" src="data:image/gif;base64,R0lGOD" data-src="https://images.nubiles-porn.com/videos/my_stepsis_is_a_hot_mess/samples/cover960.jpg?st=C5dl1YO4Wv&amp;e=9999999999"></a>
 <a href="https://nubiles-porn.com/video/watch/256651/my-stepsis-is-a-hot-mess">My Stepsis Is A Hot Mess</a>
 <a href="https://nubiles-porn.com/model/profile/28550/arin-jones">Arin Jones</a>
 <a href="https://shesinmybed.com/">ShesInMyBed</a> &ndash; Sep 16, 2026
</div></body></html>"""

_SLUG68 = '256651-my-stepsis-is-a-hot-mess'


class _Sig68:
    def __init__(self, sink=None):
        self._sink = sink if sink is not None else []

    def emit(self, *a):
        self._sink.append(' '.join(str(x) for x in a))


class _Sigs68:
    def __init__(self, msgs):
        self.progress = _Sig68(msgs)
        self.tick = _Sig68()
        self.finished = _Sig68()
        self.error = _Sig68()


def _scrape68(payload):
    """Run the real scraper over one lazy-loaded page. Returns (db, path, log)."""
    d = os.path.join(_tf55.mkdtemp(), 'cov68.json')
    with open(d, 'w', encoding='utf-8') as f:
        json.dump({'movies': {}}, f)
    site = dict(_msrc55.METADATA_SITES['nubiles'])
    site['gallery_sources'] = [{
        'id': 'shesinmybed', 'name': 'ShesInMyBed',
        'base_url': 'https://nubiles-porn.com',
        'gallery_url': 'https://nubiles-porn.com/video/gallery',
        'page_url_template': 'https://nubiles-porn.com/video/gallery/{offset}'}]
    msgs = []
    scr = _msrc55.NetworkGalleryScraper(_msrc55.MetadataDB(d), site, mode='update')
    scr.signals = _Sigs68(msgs)
    scr._fetch_gallery_page = lambda page: _HTML68 if page == 1 else ''
    orig = _msrc55._fetch_bytes
    _msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: payload
    try:
        scr._run()
    finally:
        _msrc55._fetch_bytes = orig
    return _msrc55.MetadataDB(d), d, msgs


_JPEG68 = b'\xff\xd8\xff\xe0' + b'0' * 900
_db68, _p68, _log68 = _scrape68(_JPEG68)
_rec68 = _db68.movies.get(_SLUG68) or {}
report('cover960.jpg' in str(_rec68.get('image') or ''),
       'a card that lazy-loads carries its cover through the real scraper',
       repr(str(_rec68.get('image'))[:70]))
report(not os.path.isdir(os.path.join(os.path.dirname(_p68), 'thumbnails')),
       'and writes no file for it -- the hover card fetches the URL on demand',
       os.path.dirname(_p68))
report(any('covers 1/1' in m for m in _log68),
       'the page line reports how many covers it captured',
       ' | '.join(_log68)[:180])
report(any('1 cover(s) captured from 1 card(s)' in m for m in _log68),
       'and the source line says so too, so a zero there means the markup '
       'changed rather than the site having no covers',
       ' | '.join(_log68)[:180])

# Capturing the URL and fetching the bytes are independent now: the scrape
# records the first and nothing else. A CDN that refuses to serve is therefore
# no longer visible here -- correctly, because the bytes are wanted at hover
# time and not before.
_db68b, _p68b, _log68b = _scrape68(b'')
report('cover960.jpg' in str((_db68b.movies.get(_SLUG68) or {}).get('image') or ''),
       'a gallery whose images cannot be fetched still records the cover URL',
       repr(str((_db68b.movies.get(_SLUG68) or {}).get('image'))[:70]))
report(not os.path.isdir(os.path.join(os.path.dirname(_p68b), 'thumbnails')),
       'and still leaves no thumbnails/ folder behind')

# compact_records() drops a cover whose signature has expired, so a database
# scraped an hour ago reads zero covers. That is now recoverable rather than
# permanent: the movie's own watch page mints a fresh signature on demand.
_db68c = _msrc55.MetadataDB(_p68)
_db68c.movies[_SLUG68]['image_expires'] = 1
_db68c.compact_records()
report('image' not in _db68c.movies[_SLUG68],
       'an expired signed URL is pruned from the record')
_db68d = _msrc55.MetadataDB(_p68)
_db68d.movies[_SLUG68]['image_expires'] = 9999999999
_db68d.compact_records()
report('cover960.jpg' in str(_db68d.movies[_SLUG68].get('image') or ''),
       'and a still-valid one is kept')
report(bool((_db68c.movies[_SLUG68] or {}).get('url')),
       'and the watch page it can be re-minted from survives the prune, so an '
       'empty cover is now a hover away from being fixed',
       str((_db68c.movies[_SLUG68] or {}).get('url'))[:70])

# ── 69. No name is read before it is bound ────────────────────────────────────
# _show_hover_preview tested `_cover64 is None` where the function's own cover
# variable is _cover62 -- a leftover of the scratch name used while writing the
# cover poll. py_compile cannot see that: an unassigned name is a legal global
# lookup right up until the line runs, so it shipped and raised NameError on
# every single hover. A guard of mine did report the name and I deleted the
# guard as a false positive instead of reading what it said. This checks every
# name in the file rather than one.
print()
print('69. Nothing reads a name that is never bound anywhere')

import builtins as _bi69
import symtable as _st69

_BI69 = set(dir(_bi69)) | {'__file__', '__name__', '__doc__', '__builtins__',
                           '__spec__', '__package__', '__loader__'}


def _undef69(src, name):
    """Names read in a function but bound nowhere: not local, not a parameter,
    not imported, not a closure cell, not a module global, not a builtin.
    symtable files these as implicit globals, which is what a typo becomes."""
    top = _st69.symtable(src, name, 'exec')
    module = {s.get_name() for s in top.get_symbols()
              if s.is_assigned() or s.is_imported()}
    hits = []

    def walk(t, qual):
        if t.get_type() == 'function':
            local = {s.get_name() for s in t.get_symbols()
                     if s.is_assigned() or s.is_parameter() or s.is_imported()}
            for s in t.get_symbols():
                n = s.get_name()
                if (s.is_referenced() and n not in local and not s.is_free()
                        and n not in _BI69 and n not in module):
                    hits.append(f'{n} in {qual}')
        for c in t.get_children():
            walk(c, qual + '.' + c.get_name())

    walk(top, name)
    return hits


_msrc69 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'metadata_scraper.py'), encoding='utf-8').read()
_bad69 = _undef69(SRC, 'main')
report(not _bad69,
       'main.py binds every name it reads, so a typo cannot take the hover card '
       'down at runtime', '; '.join(_bad69[:6]))
_bad69b = _undef69(_msrc69, 'metadata_scraper')
report(not _bad69b,
       'and neither does metadata_scraper.py', '; '.join(_bad69b[:6]))
report(_undef69('def f(a):\n    x = 1\n    if _nope is None:\n        return x\n', 'p')
       == ['_nope in p.f'],
       'the check still fires on the bug it was written for, so it cannot rot '
       'into a no-op')
report('_cover64' not in SRC and '_cover62 is None' in _vsrc64('_show_hover_preview'),
       'the cover poll reads the cover the function actually built')

# ── 70. The real nubiles gallery page, as captured from the site ──────────────
# nubiles.txt is the markup a Playwright session actually returns. Each card
# carries its cover ONLY in data-srcset: src and data-src are both the same
# inline-SVG placeholder. An extractor that read <img src> therefore found
# nothing on any card, which is exactly what the full re-scrape of 2026-09-17
# stored -- 5374 records, zero covers.
print()
print('70. The real nubiles gallery markup yields a cover for every card')

_HTML70 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nubiles.txt')
report(os.path.isfile(_HTML70), 'the captured gallery page is present', _HTML70)
_page70 = open(_HTML70, encoding='utf-8', errors='replace').read() if os.path.isfile(_HTML70) else ''
_site70 = dict(_msrc55.METADATA_SITES['nubiles'])
_site70['gallery_sources'] = [{
    'id': 'nubilesporn', 'name': 'NubilesPorn',
    'base_url': 'https://nubiles-porn.com',
    'gallery_url': 'https://nubiles-porn.com/video/gallery',
    'page_url_template': 'https://nubiles-porn.com/video/gallery/{offset}'}]
_mv70 = _msrc55._gallery_movies_from_html(_page70, _site70) if _page70 else []
_cov70 = [m for m in _mv70 if m.get('image')]
report(bool(_mv70) and len(_cov70) == len(_mv70),
       'every card on the page yields a cover', f'{len(_cov70)}/{len(_mv70)}')
report(bool(_cov70) and all('/samples/cover960.jpg' in str(m['image']) for m in _cov70),
       'and it is the widest of the three variants, not the 320 px one')
report(bool(_cov70) and all(int(m.get('image_expires') or 0) > 0 for m in _cov70),
       'each carrying the signature expiry its URL was minted with')
report(bool(_cov70) and not any(str(m['image']).startswith('data:') for m in _cov70),
       'and no card is handed the inline-SVG placeholder sitting in src')
report(_msrc55._best_srcset_url(
    'https://x/samples/cover320.jpg 320w,https://x/samples/cover960.jpg 960w,'
).endswith('cover960.jpg'),
       'a srcset resolves to its widest entry')
report(_msrc55._best_srcset_url('https://x/shared/med.jpg') == 'https://x/shared/med.jpg',
       'and an unlabelled one still resolves to its only URL')

# ── 71. A cover is fetched from the network that signed it ────────────────────
# REQUEST_HEADERS carries "Referer: https://www.teamskeet.com/". That is right
# for images.psmcdn.net, which is unsigned and public, and wrong for
# images.nubiles-porn.com, whose covers are signed precisely so they cannot be
# hotlinked. So even with the cover URL in hand, every nubiles download asked
# the wrong network for it -- and the download swallowed the refusal.
print()
print('71. A cover is requested from the network that signed it')

_ALL71 = (_msrc55.METADATA_SITES['teamskeet']['sources']
          + _msrc55.METADATA_SITES['nubiles']['gallery_sources'])


def _exp71(s):
    v = str((s or {}).get('base_url') or '')
    return v if (not v or v.endswith('/')) else v + '/'


_ref71 = getattr(_msrc55, '_referer_for', None)
report(_ref71 is not None and all(_ref71(s) == _exp71(s) for s in _ALL71),
       'every gallery source resolves to its own network as the referer',
       ', '.join(sorted({(_ref71 or _exp71)(s) for s in _ALL71})))
report(bool(_ref71) and _ref71({}) == '' and _ref71(None) == '',
       'and an unknown site leaves the module default alone')


class _Resp71:
    status_code = 200
    content = b'\xff\xd8\xff\xe0' + b'0' * 900


class _Refused71:
    status_code = 403
    content = b''


_cap71 = {}


def _install71(resp):
    _cap71.clear()

    def _get(url, headers=None, timeout=None, impersonate=None):
        _cap71['url'] = url
        _cap71['headers'] = dict(headers or {})
        return resp
    mod = _types55.ModuleType('curl_cffi')
    mod.requests = _types55.SimpleNamespace(get=_get)
    return mod


_saved71 = _sys55.modules.get('curl_cffi')
_U71 = ('https://images.nubiles-porn.com/videos/my_stepsis/samples/'
        'cover960.jpg?st=A&e=9999999999')
try:
    _sys55.modules['curl_cffi'] = _install71(_Resp71)
    _b71 = _msrc55._fetch_bytes(_U71, referer='https://nubiles-porn.com/')
    report(len(_b71) > 512,
           'a cover downloads when the request names the network that signed it',
           f'{len(_b71)} bytes')
    report(_cap71['headers'].get('Referer') == 'https://nubiles-porn.com/',
           'and that referer is what goes on the wire, not teamskeet',
           str(_cap71['headers'].get('Referer')))
    report(str(_cap71['headers'].get('Accept') or '').startswith('image/'),
           'asking for an image rather than an HTML document',
           str(_cap71['headers'].get('Accept'))[:40])
    _msrc55._fetch_bytes(_U71)
    report(_cap71['headers'].get('Referer') == 'https://www.teamskeet.com/',
           'with no referer given the teamskeet default is left as it was',
           str(_cap71['headers'].get('Referer')))
    _sys55.modules['curl_cffi'] = _install71(_Refused71)
    _diag71 = []
    _b71b = _msrc55._fetch_bytes(_U71, referer='https://nubiles-porn.com/',
                                 diag=_diag71)
    report(not _b71b and _diag71 == ['HTTP 403'],
           'a refused cover says why, so a 403 is not mistaken for a markup '
           'change', str(_diag71))
finally:
    if _saved71 is None:
        _sys55.modules.pop('curl_cffi', None)
    else:
        _sys55.modules['curl_cffi'] = _saved71

_run71 = ast.get_source_segment(
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'metadata_scraper.py'), encoding='utf-8').read(),
    next(n for n in ast.walk(ast.parse(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'metadata_scraper.py'),
        encoding='utf-8').read()))
         if isinstance(n, ast.ClassDef) and n.name == 'NetworkGalleryScraper'))
report('save_thumbnail' not in _run71 and 'src_referer' not in _run71,
       'the scrape downloads no cover at all now, so it cannot pass the wrong '
       'referer for one')
_src71 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'metadata_scraper.py'), encoding='utf-8').read()
_hit71 = [n for n in ast.walk(ast.parse(_src71))
          if isinstance(n, ast.FunctionDef) and n.name == 'ensure_cover_async']
_fn71 = ast.get_source_segment(_src71, _hit71[0]) if _hit71 else ''
report('_referer_for(site)' in (_fn71 or ''),
       'the fetch that does happen asks as the network that signed the cover')

# ── 72. A preview is streamed, never written to disk ──────────────────────────
# A field log showed the signed loop playing straight from the CDN, and a disk
# that was already full turning the cache into "cache failed (HTTP 200)" on six
# rows out of seven. So the file was never needed -- the URL plays -- and
# writing it was the only thing that could fail. Nothing is stored now.
print()
print('72. A preview is streamed, never written to disk')

_msrc72 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'metadata_scraper.py'), encoding='utf-8').read()
for _gone72 in ('def save_preview', 'def previews_dir', '_preview_path_for_slug',
                'preview_local', 'ensure_preview_async', 'refresh_preview_async'):
    report(_gone72 not in _msrc72,
           f'nothing writes a preview to disk any more: {_gone72} is gone')
_clip72 = _vsrc64('_start_metadata_hover_clip')
report('fromLocalFile' not in _clip72 and 'preview_local' not in _clip72,
       'the card plays the URL rather than a file')
report('preview_live' in _clip72 and 'QUrl(' in _clip72,
       'streaming it while its signature lasts')


class _P72:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._metadata_links = {}


_LIVE72 = ('https://images.nubiles-porn.com/videos/x/videos/loops/'
           'series_x_loop_480.mp4?st=A&e=9999999999')
_DEAD72 = ('https://images.nubiles-porn.com/videos/y/videos/loops/'
           'series_y_loop_480.mp4?st=B&e=1')


def _mk72(preview, expires):
    d = _tf55.mkdtemp()
    dbp = os.path.join(d, _msrc55.METADATA_SITES['nubiles']['db_filename'])
    with open(dbp, 'w', encoding='utf-8') as f:
        json.dump({'movies': {'r1': {
            'slug': 'r1', 'title': 'Some Scene', 'series': 'Series',
            'models': [], 'date': '16/09/2026', 'video_id': '1',
            'meta_fetched': True,
            'url': 'https://nubiles-porn.com/video/watch/1/s',
            'preview': preview, 'preview_expires': expires}}}, f)
    pl = _P72(d)
    row = 'https://watchporn.to/video/1/some-row/'
    pl._metadata_links[_msrc55._meta_norm_path(row)] = {
        'site': 'nubiles', 'slug': 'r1'}
    return _msrc55.preview_info_for_path(pl, row), d


_live72, _d72 = _mk72(_LIVE72, 9999999999)
report(_live72.get('preview_live') and _live72.get('preview_url') == _LIVE72,
       'a row inside its hour is handed the signed URL to stream',
       str(_live72.get('preview_url'))[:70])
report('preview_local' not in _live72,
       'and no local path is offered, because none is ever created')
_dead72, _ = _mk72(_DEAD72, 1)
report(not _dead72.get('preview_live'),
       'a row whose signature has died is not handed a dead link to fail on')

# ── 73. Update refreshes the signature, so a row keeps previewing ─────────────
# Nothing is on disk to fall back on, so the stored URL is the whole mechanism.
# compact_records() drops it once it expires, which leaves the record with none
# -- and the gallery page an Update is already reading carries a fresh one.
print()
print('73. Update refreshes the signature, so a row keeps previewing')

_BASE73 = {'slug': '256294-stepmom-is-a-great-kisser-s27e1',
           'title': 'Stepmom Is A Great Kisser', 'series': 'MomsTeachSex',
           'models': [], 'date': '16/09/2026', 'video_id': '256294',
           'meta_fetched': True,
           'url': 'https://nubiles-porn.com/video/watch/256294/'
                  'stepmom-is-a-great-kisser-s27e1'}


class _E73:
    def __init__(self, sink=None):
        self._sink = sink if sink is not None else []

    def emit(self, *a):
        self._sink.append(' '.join(str(x) for x in a))


def _run73(existing):
    d = os.path.join(_tf55.mkdtemp(),
                     _msrc55.METADATA_SITES['nubiles']['db_filename'])
    with open(d, 'w', encoding='utf-8') as f:
        json.dump({'movies': {existing['slug']: existing} if existing else {}}, f)
    scr = _msrc55.NetworkGalleryScraper(_msrc55.MetadataDB(d), _site70,
                                        mode='update')
    sink = []
    scr.signals.progress = _E73(sink)
    scr.signals.tick = _E73()
    scr.signals.finished = _E73()
    scr.signals.error = _E73()
    scr._fetch_gallery_page = lambda page: _page70 if page == 1 else ''
    # The scrape downloads nothing any more, so there is no fetch to stub out.
    scr._run()
    # The scraper's own DB, not a reloaded one: nubiles.txt was captured with a
    # signature that has since expired, and compact_records() prunes an expired
    # preview on load -- which is correct behaviour, but it would hide what the
    # scrape just wrote.
    return scr.db, d, sink


_db73, _d73, _sink73 = _run73(dict(_BASE73))
_rec73 = _db73.movies.get(_BASE73['slug']) or {}
report('/loops/' in str(_rec73.get('preview') or ''),
       'a record that lost its signature gets a fresh one off the page',
       str(_rec73.get('preview') or '(none)')[:80])
report(int(_rec73.get('preview_expires') or 0) > 0,
       'with its new expiry, so the player knows when to stop streaming it')
report(not os.path.isdir(os.path.join(os.path.dirname(_d73), 'previews')),
       'and no previews folder is created anywhere')
report(all('/loops/' in str(m.get('preview') or '')
           for m in _db73.movies.values() if m.get('preview')),
       'every record the page had a loop for is streamable',
       f"{sum(1 for m in _db73.movies.values() if m.get('preview'))} of "
       f"{len(_db73.movies)}")
_live73 = dict(_rec73)
_live73['preview'] = _LIVE72
_live73['preview_expires'] = 9999999999
_db73b, _, _ = _run73(_live73)
report((_db73b.movies.get(_BASE73['slug']) or {}).get('preview') == _LIVE72,
       'and a signature that is still live is left alone')

if _have74:
    # ── 74. A dead cover is re-minted on hover, from the movie's own watch page ───
    # A nubiles signature dies in about an hour, and the old answer was "run an
    # Update". But the watch page mints a fresh one every time it is loaded --
    # measured live, cover1280/cover614 both carrying a new e= -- so a hover can
    # recover the cover itself, with no scrape and nothing on disk. The page is
    # built below from what that page actually returned, distractors included: it
    # also carries the covers of its related videos and its photo thumbnails, and
    # picking one of those would show the wrong scene.
    print()
    print('74. A dead cover is re-minted on hover from the watch page')

    _PAGE74 = """
    ![Screenshot](https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/samples/cover1280.jpg?st=gYviyGTFR2y8Ci8PlnN7kQ&e=1789696800)

    [![preview image](https://content2a.nubiles-porn.com/exclusive/stepmom_is_a_great_kisser/photos/tn/stepmom_is_a_great_kisser_054.jpg?st=4kMgzJiUFs3liUJZPFw_tw&e=1789696800)](https://nubiles-porn.com/join)

    Related Videos
    [![My Swap Family Is Closer Than Ever - S11:E6](https://images.nubiles-porn.com/videos/my_swap_family_is_closer_than_ever/samples/cover960.jpg?st=RFjoV0KXfnF9JqfV65raYw&e=1789696800)](https://nubiles-porn.com/video/watch/246088/my-swap-family-is-closer-than-ever-s11e6)

    Related Photos
    [![Stepmom Is A Great Kisser - S27:E1](https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/samples/cover614.jpg?st=D9TO6Qs3eAssgLwx5Q1g2g&e=1789696800)](https://nubiles-porn.com/photo/gallery/256287/stepmom-is-a-great-kisser-s27e1)

    To view this video please enable JavaScript. Video Player is loading.
    1280x720 HD  960x540  640x360  480x270
    """

    _c74 = _msrc55.signed_cover_url(_PAGE74, 'Stepmom Is A Great Kisser')
    report('cover1280.jpg' in _c74 and 'st=gYviyGTFR2y8Ci8PlnN7kQ' in _c74,
           'the watch page hands back this movie\'s cover, freshly signed',
           _c74[:110] or '(none)')
    report('my_swap_family' not in _c74 and 'cover614' not in _c74,
           'and not a related video\'s, nor a narrower cut of its own')
    report(_msrc55.signed_cover_url(_PAGE74, 'My Swap Family Is Closer Than Ever')
           .count('my_swap_family_is_closer_than_ever') == 1,
           'a different movie on the same page resolves to its own cover instead')
    report(_msrc55.signed_cover_url(_PAGE74, 'NoSuchScene') == '',
           'a title the page does not carry yields nothing rather than a guess')
    report(_msrc55._image_expiry_epoch(_c74) == 1789696800,
           'with the new expiry, so it can be pruned again an hour later',
           str(_msrc55._image_expiry_epoch(_c74)))

    # The preview loop is not on that page -- its player sources are not in the
    # HTML, measured on six watch pages out of six, which is exactly what the field
    # log showed. So a refresh recovers the cover and cannot recover the preview.
    report(_msrc55.signed_media_url(_PAGE74, 'Stepmom Is A Great Kisser') == '',
           'and the same page carries no loop, which is why previews still need an Update')
    report('/loops/' in _msrc55.signed_media_url(
        _PAGE74 + ('<a href="https://images.nubiles-porn.com/videos/stepmom_is_a_'
                   'great_kisser/videos/loops/momsteachsex_stepmom_is_a_great_kisser'
                   '_loop_480.mp4?st=B&e=1789696800"></a>'),
        'Stepmom Is A Great Kisser'),
           'but one that did would be taken, not ignored')
    report(_msrc55.signed_media_url(_PAGE74 + (
        '<a href="https://images.nubiles-porn.com/videos/some_other_scene/videos/'
        'loops/x_loop_480.mp4?st=C&e=1789696800"></a>'),
        'Stepmom Is A Great Kisser') == '',
           'and another scene\'s loop is never borrowed for it')


    class _Player74:
        pass


    _p74 = _Player74()
    _p74.data_dir = _tf55.mkdtemp()
    _p74._metadata_dbs = {}
    _p74._metadata_links = {}
    _p74._metadata_name_overrides = {}
    _p74._meta_norm_path = _msrc55._meta_norm_path
    _d74 = os.path.join(_p74.data_dir, _msrc55.METADATA_SITES['nubiles']['db_filename'])
    _URL74 = 'https://nubiles-porn.com/video/watch/256294/stepmom-is-a-great-kisser-s27e1'
    _MOV74 = {
        'slug': '256294-stepmom-is-a-great-kisser-s27e1',
        'title': 'Stepmom Is A Great Kisser', 'series': 'MomsTeachSex',
        'models': ['Amirah Adara'], 'date': '14/09/2026', 'meta_fetched': True,
        'url': _URL74, 'source_site': 'momsteachsex',
        'image': 'https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/'
                 'samples/cover960.jpg?st=OLD&e=1',
        'image_expires': 1,
    }
    with open(_d74, 'w', encoding='utf-8') as _f74:
        json.dump({'movies': {_MOV74['slug']: dict(_MOV74)}}, _f74)
    _db74 = _msrc55.MetadataDB(_d74)
    report(not _db74.movies[_MOV74['slug']].get('image'),
           'an hour later the stored cover is gone, pruned as expired')

    _site74 = dict(_msrc55.METADATA_SITES['nubiles'])
    _orig_sites74 = _msrc55.METADATA_SITES['nubiles']
    _orig_html74 = _msrc55._fetch_html
    _orig_bytes74 = _msrc55._fetch_bytes
    _fetched74 = []
    try:
        _msrc55.METADATA_SITES['nubiles'] = _site74
        _msrc55._fetch_html = lambda url, timeout=20: (
            _fetched74.append(url), _PAGE74)[1]
        _msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: (
            _fetched74.append(url), b'\xff\xd8' + b'0' * 900)[1]
        _msrc55._COVER_CACHE.clear()
        _msrc55._COVER_INFLIGHT.clear()
        _msrc55._REFRESH_LAST.clear()
        report(_msrc55.refresh_signed_assets_async(
            _p74, _site74, _MOV74, _db74) is True,
           'a hover with a dead cover starts a re-mint in the background')
        for _ in range(200):
            if _db74.movies[_MOV74['slug']].get('image'):
                break
            time.sleep(0.01)
        _rec74 = _db74.movies[_MOV74['slug']]
        report('cover1280.jpg' in str(_rec74.get('image') or ''),
               'which replaces the dead URL with a live one in the database',
               str(_rec74.get('image') or '(none)')[:100])
        report(int(_rec74.get('image_expires') or 0) == 1789696800,
               'and records the new expiry so the cycle can repeat')
        report(_fetched74 and _fetched74[0] == _URL74,
               'having read the movie\'s own watch page',
               str(_fetched74[0]) if _fetched74 else 'no fetch')
        _n74 = len([u for u in _fetched74 if u == _URL74])
        report(_msrc55.refresh_signed_assets_async(
            _p74, _site74, _MOV74, _db74) is False,
           'and a second hover inside the cooldown does not fetch it again -- '
           'the poll re-reads the record every 400 ms, and with only an '
           'in-flight guard one movie fetched its watch page fifteen times in '
           'a minute', f'{_n74} page fetch(es) for one movie')

        # The case the field log actually showed: the page arrives and carries
        # no signature at all. That must be reported with a description, and it
        # must not be retried on the next poll either.
        _msrc55._REFRESH_LAST.clear()
        _fetched74.clear()
        _bare74 = ('<html><head><title>Stepmom Is A Great Kisser</title></head>'
                   '<body>Video Player is loading.</body></html>')
        _msrc55._fetch_html = lambda url, timeout=20: (
            _fetched74.append(url), _bare74)[1]
        report(_msrc55.refresh_signed_assets_async(
            _p74, _site74, _MOV74, _db74) is True,
           'a page that yields nothing is still tried once')
        for _ in range(200):
            if _fetched74:
                break
            time.sleep(0.01)
        time.sleep(0.05)
        report(_msrc55.refresh_signed_assets_async(
            _p74, _site74, _MOV74, _db74) is False,
           'and a page with no signature is not retried on the next poll',
           f'{len([u for u in _fetched74 if u == _URL74])} fetch(es)')
        _d74x = _msrc55._describe_page(_bare74)
        report('byte(s)' in _d74x and 'signed 0' in _d74x and 'title' in _d74x,
           'the failure says what the page held, not just how big it was -- '
           'an empty response, a bot challenge and real markup with no '
           'signature need three different fixes', _d74x)
        report(_msrc55._describe_page('') == 'empty response',
           'and an empty response says so rather than looking like markup')
        for _ in range(200):
            if _msrc55.cover_bytes(_MOV74['slug']):
                break
            time.sleep(0.01)
        report(len(_msrc55.cover_bytes(_MOV74['slug'])) > 512,
               'and then the bytes, into memory -- never onto the disk',
               f'{len(_msrc55.cover_bytes(_MOV74["slug"]))} bytes')
        report(not os.path.isdir(os.path.join(_p74.data_dir, 'thumbnails')),
               'so no thumbnails/ folder is created for it',
               _p74.data_dir)
        report(_msrc55.refresh_signed_assets_async(
            None, _site74, _MOV74, _db74) is False
               and _msrc55.refresh_signed_assets_async(
                   _p74, _site74, {'slug': '', 'url': _URL74}, _db74) is False,
               'and it refuses to run without a player and a slug')
    finally:
        _msrc55.METADATA_SITES['nubiles'] = _orig_sites74
        _msrc55._fetch_html = _orig_html74
        _msrc55._fetch_bytes = _orig_bytes74
        _msrc55._COVER_CACHE.clear()
        _msrc55._COVER_INFLIGHT.clear()
        _msrc55._REFRESH_LAST.clear()

    # The hover itself must hand the card something to paint and a reason to keep
    # polling, including for a row whose cover has already been pruned away.
    _p74b = _Player74()
    _p74b.data_dir = _tf55.mkdtemp()
    _p74b._metadata_dbs = {}
    _p74b._metadata_links = {}
    _p74b._metadata_name_overrides = {}
    _p74b._meta_norm_path = _msrc55._meta_norm_path
    _d74b = os.path.join(_p74b.data_dir, _msrc55.METADATA_SITES['nubiles']['db_filename'])
    with open(_d74b, 'w', encoding='utf-8') as _f74b:
        json.dump({'movies': {_MOV74['slug']: {
            'slug': _MOV74['slug'], 'title': _MOV74['title'],
            'series': _MOV74['series'], 'models': _MOV74['models'],
            'date': _MOV74['date'], 'meta_fetched': True, 'url': _URL74,
            'image': _MOV74['image'], 'image_expires': 9999999999,
        }}}, _f74b)
    _row74 = 'https://pixeldrain.com/u/row74'
    _orig_sites74b = _msrc55.METADATA_SITES['nubiles']
    _orig_bytes74b = _msrc55._fetch_bytes
    try:
        _msrc55.METADATA_SITES['nubiles'] = _site74
        _msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: b'\xff\xd8' + b'0' * 900
        _p74b._metadata_links[_msrc55._meta_norm_path(_row74)] = {
            'slug': _MOV74['slug'], 'site': 'nubiles'}
        _i74 = _msrc55.preview_info_for_path(_p74b, _row74)
        report('cover_data' in _i74 and 'thumbnail' not in _i74,
               'the hover hands the card cover bytes, not a file path',
               ','.join(sorted(_i74)))
        report(_i74.get('image_live') is True and _i74.get('can_refresh') is True,
               'and says both that the URL is live and where a dead one comes from')
        for _ in range(200):
            if _msrc55.cover_bytes(_MOV74['slug']):
                break
            time.sleep(0.01)
        _i74b = _msrc55.preview_info_for_path(_p74b, _row74)
        report(len(_i74b.get('cover_data') or b'') > 512,
               'and once the fetch lands the same call returns the bytes',
               f'{len(_i74b.get("cover_data") or b"")} bytes')
    finally:
        _msrc55.METADATA_SITES['nubiles'] = _orig_sites74b
        _msrc55._fetch_bytes = _orig_bytes74b
        _msrc55._COVER_CACHE.clear()

    _cover74 = _vsrc64('_metadata_cover_pixmap')
    report('loadFromData' in _cover74 and 'cover_data' in _cover74,
           'the card paints those bytes straight into a pixmap')
    report('os.path.isfile' not in _cover74 and 'thumbnail' not in _cover74,
           'and no longer looks for a file on disk to do it')
    _poll74 = _vsrc64('_poll_hover_cover')
    report('_hover_cover_poll_tries > 25' in _poll74,
           'the poll outlasts the two round trips a re-mint costs, not just 3 s')
    _show74 = _vsrc64('_show_hover_preview')
    report('can_refresh' in _show74,
           'and it keeps polling for a row whose cover was pruned, not only a live one')


print()

# Deliberately outside the guard above: this is the defect the field log
# showed, so it has to be able to fail against any version of the module. One
# movie fetched its watch page fifteen times in a minute, because the hover
# poll re-reads the record every 400 ms and an in-flight guard releases the
# moment a fetch ends.
class _Player75s:
    pass


_p75s = _Player75s()
_p75s.data_dir = _tf55.mkdtemp()
_p75s._metadata_dbs = {}
_p75s._metadata_links = {}
_p75s._metadata_name_overrides = {}
_p75s._meta_norm_path = _msrc55._meta_norm_path
_d75s = os.path.join(_p75s.data_dir, 'st75.json')
with open(_d75s, 'w', encoding='utf-8') as _f75s:
    json.dump({'movies': {}}, _f75s)
_site75s = dict(_msrc55.METADATA_SITES['nubiles'])
_site75s['db_filename'] = 'st75.json'
_MOV75s = {'slug': '75-storm-slug',
           'title': 'Stepmom Is A Great Kisser', 'series': 'MomsTeachSex',
           'url': 'https://nubiles-porn.com/video/watch/256294/storm'}
_seen75s = []
_orig_html75s = _msrc55._fetch_html
_orig_bytes75s = _msrc55._fetch_bytes
_orig_sites75s = _msrc55.METADATA_SITES['nubiles']
try:
    _msrc55.METADATA_SITES['nubiles'] = _site75s
    _msrc55._fetch_html = lambda url, timeout=20: (
        _seen75s.append(url), '<html><title>x</title></html>')[1]
    _msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: b''
    if hasattr(_msrc55, '_REFRESH_LAST'):
        _msrc55._REFRESH_LAST.pop(_MOV75s['slug'], None)
    _db75s = _msrc55.MetadataDB(_d75s)
    for _ in range(25):
        # Exactly what the hover poll does: re-read the record, which asks for
        # a refresh whenever the cover is still missing.
        _msrc55.refresh_signed_assets_async(_p75s, _site75s, _MOV75s, _db75s)
        time.sleep(0.02)
    time.sleep(0.3)
    report(len(_seen75s) == 1,
           'twenty-five hovers on one movie with no cover fetch its watch page '
           'once, not twenty-five times',
           f'{len(_seen75s)} page fetch(es)')
finally:
    _msrc55._fetch_html = _orig_html75s
    _msrc55._fetch_bytes = _orig_bytes75s
    _msrc55.METADATA_SITES['nubiles'] = _orig_sites75s
    if hasattr(_msrc55, '_REFRESH_LAST'):
        _msrc55._REFRESH_LAST.pop(_MOV75s['slug'], None)


# ── 75. A save that fails cannot destroy the metadata it is saving ────────────
# The field log showed about fifty "[MetadataDB] save error: [Errno 28] No space
# left on device". save() used to do open(path, "w"), which truncates the file
# before a single byte is written -- so each of those left a few dozen bytes of
# broken JSON where megabytes of records had been, and _load() ended in
# "except Exception: pass", reading that as an empty database and writing it
# back on the next save. Ten thousand movies gone, silently. Writes are atomic
# now, and an unreadable file is put where it cannot be overwritten.
print()
print('75. A failed save leaves the metadata intact')

_d75 = os.path.join(_tf55.mkdtemp(), 'db75.json')
_db75 = _msrc55.MetadataDB(_d75)
for _i75 in range(3000):
    _db75.upsert({'slug': f's75-{_i75}', 'title': 'Some Scene Title',
                  'series': 'MomsTeachSex', 'models': ['Amirah Adara'],
                  'date': '14/09/2026', 'meta_fetched': True,
                  'url': f'https://nubiles-porn.com/video/watch/{_i75}/s'})
_db75.save()
_good75 = open(_d75, 'rb').read()
report(len(_good75) > 200_000, 'a populated database is on disk',
       f'{len(_good75)} bytes')

import unittest.mock as _mock75
_ERR75 = OSError(28, 'No space left on device')
for _what75, _tgt75 in (('the write', 'os.fsync'), ('the move', 'os.replace')):
    # A distinct slug per pass, so the count below really is one per failure.
    _db75.upsert({'slug': f's75-new-{_what75}', 'title': 'Added After The Disk '
                  'Filled', 'meta_fetched': True})
    with _mock75.patch(_tgt75, side_effect=_ERR75):
        _db75.save()
    report(open(_d75, 'rb').read() == _good75,
           f'a disk that fills during {_what75} leaves the previous file intact',
           f'{os.path.getsize(_d75)} bytes still on disk')
    report(not os.path.exists(_d75 + '.tmp'),
           f'and cleans up the partial file it was writing', _what75)
report(_db75.count() == 3002,
       'the in-memory database keeps the records it could not write',
       str(_db75.count()))
_db75.save()
report(len(json.load(open(_d75, encoding='utf-8'))['movies']) == 3002,
       'and once the disk has room those records do reach the file')

# The other half: a file that is already broken -- which fifty failed saves
# could easily have left behind -- must not be read as an empty database and
# then overwritten with one.
_d75b = os.path.join(_tf55.mkdtemp(), 'db75b.json')
_broken75 = b'{"movies": {"a": {"slug": "a", "tit'
with open(_d75b, 'wb') as _f75b:
    _f75b.write(_broken75)
_db75b = _msrc55.MetadataDB(_d75b)
report(_db75b.count() == 0, 'an unreadable database starts empty in memory')
report(os.path.isfile(_d75b + '.unreadable')
       and open(_d75b + '.unreadable', 'rb').read() == _broken75,
       'but the bytes are moved aside byte-for-byte, not discarded',
       str(os.path.getsize(_d75b + '.unreadable') if os.path.isfile(
           _d75b + '.unreadable') else 'no file') + ' bytes')
report(not os.path.exists(_d75b),
       'and the corrupt path is clear, so a later save cannot overwrite it')
_db75b.upsert({'slug': 's75b', 'title': 'Fresh Start', 'meta_fetched': True})
_db75b.save()
report(os.path.isfile(_d75b + '.unreadable')
       and len(json.load(open(_d75b, encoding='utf-8'))['movies']) == 1,
       'a new save writes a new file while the old one stays recoverable')

# The shape the field log actually showed: not a partial write but a 0-byte
# file, because the old save() truncated before writing anything. The raw JSON
# error for that is "Expecting value: line 1 column 1 (char 0)", which names a
# column and not a cause, so it reads like a parsing bug rather than a file
# that was emptied.
_d75c = os.path.join(_tf55.mkdtemp(), 'db75c.json')
open(_d75c, 'wb').close()
report(os.path.getsize(_d75c) == 0, 'a truncated database is 0 bytes')
import io as _io75, contextlib as _ctx75
_buf75 = _io75.StringIO()
with _ctx75.redirect_stdout(_buf75):
    _db75c = _msrc55.MetadataDB(_d75c)
_out75 = _buf75.getvalue()
report('empty (0 bytes)' in _out75,
       'and it is reported as empty, not as a JSON parse error',
       _out75.strip().splitlines()[0][:110] if _out75.strip() else 'silent')
report(_db75c.count() == 0, 'it starts as an empty database')
report(not os.path.exists(_d75c + '.unreadable'),
       'and no .unreadable file is kept for something with nothing in it')
_db75c.upsert({'slug': 's75c', 'title': 'Rebuilt', 'meta_fetched': True})
_db75c.save()
report(len(json.load(open(_d75c, encoding='utf-8'))['movies']) == 1,
       'a fresh save writes over the empty one cleanly')


# ── 76. A site that challenges a plain HTTP client has to be read in a browser ──
print()
print("76. reading a challenged watch page")
def _fn_body(text, signature, ends=('\n\ndef ',)):
    """One function's source, or '' when it cannot be located.

    Never raises. A missing substring has to read as a failed assertion, not
    abort the section -- a ValueError here has silently hidden real failures
    four separate times, because everything below it stops running and the
    FAIL count comes out looking small.
    """
    try:
        i = text.index(signature)
    except ValueError:
        return ''
    # Only the markers the caller names, deliberately: a module-level function
    # can contain a nested def, and cutting at the nearest of both markers
    # silently dropped the last half of _fetch_page.
    cut = len(text)
    for stop in ends:
        j = text.find(stop, i + len(signature))
        if j != -1:
            cut = min(cut, j)
    return text[i:cut]


def _pos(text, needle, start=0):
    try:
        return text.index(needle, start)
    except ValueError:
        return -1


def _before(text, first, second, start=0):
    """True only when both appear and `first` comes first. Never raises."""
    if start < 0:
        return False
    try:
        i = text.index(first, start)
        return text.index(second, i) > i
    except ValueError:
        return False


if hasattr(_msrc55, '_fetch_page'):
    _FOLDER76 = 'stepmom_is_a_great_kisser'
    _URL76 = ('https://images.nubiles-porn.com/videos/' + _FOLDER76 +
              '/samples/cover1280.jpg?st=AbCdEfGhIjKlMnOpQrStUv&e=1999999999')
    _PAGE76 = '<html><img data-srcset="' + _URL76 + ' 1280w"></html>'
    _site76 = dict(_msrc55.METADATA_SITES['nubiles'])
    _site76['db_filename'] = 's76.json'
    _d76 = os.path.join(_tf55.mkdtemp(), 's76.json')
    with open(_d76, 'w', encoding='utf-8') as _f76:
        json.dump({'movies': {}}, _f76)

    class _Player76:
        pass

    _p76 = _Player76()
    _p76.data_dir = os.path.dirname(_d76)
    _MOV76 = {'slug': '256294-stepmom-is-a-great-kisser-s27e1',
              'title': 'Stepmom Is A Great Kisser', 'series': 'MomsTeachSex',
              'url': 'https://nubiles-porn.com/video/watch/256294/slug'}
    _db76 = _msrc55.MetadataDB(_d76)
    _calls76 = []
    _plain76 = []
    _ret76 = {'page': _PAGE76}

    class _Sess76:
        """Stands in for Playwright; _fetch_page builds this directly."""

        def __init__(self, cookie_path=None, on_status=None):
            self._state = cookie_path

        def get(self, url, wait_selector=None, timeout=None):
            _calls76.append((url, self._state))
            return _ret76['page']

        def close(self):
            pass

    _ob76 = _msrc55._BrowserGallerySession
    _oh76 = _msrc55._fetch_html
    _oby76 = _msrc55._fetch_bytes
    try:
        _msrc55._BrowserGallerySession = _Sess76
        _msrc55._fetch_bytes = lambda url, timeout=20, referer='', diag=None: (
            b'\xff\xd8' + b'0' * 800)
        _msrc55._fetch_html = lambda url, timeout=20: (
            _plain76.append(url), '')[1]
        _ret76['page'] = _PAGE76
        _msrc55._REFRESH_LAST.pop(_MOV76['slug'], None)
        report(_msrc55.refresh_signed_assets_async(
            _p76, _site76, _MOV76, _db76) is True,
           'a hover on a site that challenges plain requests tries a browser')
        for _ in range(300):
            if _db76.movies.get(_MOV76['slug'], {}).get('image'):
                break
            time.sleep(0.01)
        report(bool(_calls76) and _calls76[0][0] == _MOV76['url'],
           'which reads the movie\'s own watch page, because a plain request '
           'only ever gets a "Security Check" interstitial back',
           str(_calls76[0][0]) if _calls76 else 'no browser call')
        report(bool(_calls76) and _calls76[0][1].endswith(
            'nubiles-porn_browser_state.json'),
           'reusing the browser state Update already established -- keyed on '
           'the network the movie came from, not on the site -- so the second '
           'visit is not challenged again',
           str(_calls76[0][1]) if _calls76 else '(none)')
        report('cover1280.jpg' in str(
            _db76.movies.get(_MOV76['slug'], {}).get('image') or ''),
           'and a signed cover found that way lands in the database',
           str(_db76.movies.get(_MOV76['slug'], {}).get('image') or '(none)')[:90])
        report(_plain76 == [_MOV76['url']],
           'plain HTTP is tried first, because it is cheap and a site that '
           'answers is answered for in a few hundred ms -- only what it comes '
           'back with decides whether a browser is worth 12 s',
           str(_plain76))

        # The reverse: a page plain HTTP can actually serve must never cost a
        # browser launch. The old order opened one first and only fell back,
        # which is how a 238 ms page turned into a 27 s one.
        _d76b = os.path.join(_tf55.mkdtemp(), 's76b.json')
        with open(_d76b, 'w', encoding='utf-8') as _fb76:
            json.dump({'movies': {}}, _fb76)
        _db76b = _msrc55.MetadataDB(_d76b)
        _calls76.clear()
        _plain76.clear()
        _ret76['page'] = None
        _msrc55._fetch_html = lambda url, timeout=20: (
            _plain76.append(url), _PAGE76)[1]
        _msrc55._REFRESH_LAST.pop(_MOV76['slug'], None)
        _msrc55.refresh_signed_assets_async(_p76, _site76, _MOV76, _db76b)
        for _ in range(300):
            if _db76b.movies.get(_MOV76['slug'], {}).get('image'):
                break
            time.sleep(0.01)
        report(bool(_plain76) and not _calls76,
           'and when plain HTTP serves the page, no browser is opened at all',
           f'{len(_calls76)} browser / {len(_plain76)} plain')
    finally:
        _msrc55._BrowserGallerySession = _ob76
        _msrc55._fetch_html = _oh76
        _msrc55._fetch_bytes = _oby76
        _msrc55._REFRESH_LAST.pop(_MOV76['slug'], None)

    report('BOT CHALLENGE' in _msrc55._describe_page(
        '<html><head><title>Security Check</title></head></html>'),
       'an interstitial is named as one rather than described as if it were '
       'the page -- that distinction is what the field log was missing',
       _msrc55._describe_page('<title>Security Check</title>'))
    report('BOT CHALLENGE' not in _msrc55._describe_page(_PAGE76),
       'and a real page is not mislabelled as a challenge')
    _st76 = _msrc55._browser_state_path(_db76, _site76, _MOV76['url'])
    report(_st76.replace('\\', '/').endswith('/nubiles-porn_browser_state.json'),
       'the state file is named per network, next to the database', _st76)
    report(_msrc55._site_needs_browser(_site76, _MOV76['url']) is True,
       'needs_browser is read off the matching gallery_sources entry -- it is '
       'not on the site dict, so checking that alone never fires',
       f"site={_site76.get('needs_browser')!r} "
       f"source={_msrc55._source_for_url(_site76, _MOV76['url']).get('id')!r}")
    report(_msrc55._site_needs_browser(
        dict(_site76, gallery_sources=[]), _MOV76['url']) is False,
       'and a site with no browser-flagged networks is not sent through one')
    _txt76 = open(_msrc55.__file__, encoding='utf-8').read()
    for _fn76 in ('_browser_page_html', '_fetch_page'):
        _body76 = _fn_body(_txt76, 'def ' + _fn76 + '(')
        report(('_BROWSER_FETCH_LOCK' in _body76
                or '_browser_fetch_guard' in _body76),
           f'{_fn76} serializes browser fetches -- each one starts a Chromium, '
           'and hovers for several different movies arrive in a row',
           'no guard in ' + _fn76)
else:
    report(False, 'the browser-backed refresh is present',
           'missing: _fetch_page')


# ---------------------------------------------------------------------------
# 77. The browser state file, the transport label, and the playlist warm-up.
#
# The field log read "browser fetch failed: JSONDecodeError: Expecting value:
# line 1 column 1 (char 0)". That message is json.loads on an empty string, and
# the only JSON on that path is the saved browser state -- a full disk truncates
# it, and Playwright then raises from inside new_context, taking every cover
# refresh with it. This section pins the cause and the fix.
# ---------------------------------------------------------------------------
print()
print('--- 77: state file / transport label / bounded fetch ---')

_API77 = ('_storage_state_usable', '_save_storage_state', '_fetch_page',
          '_is_challenge_page', 'permanent_cover_url', 'page_cover_url')
_have77 = all(hasattr(_msrc55, n) for n in _API77)
report(_have77, 'the state-file, transport and warm-up API is present',
       'missing: ' + ', '.join(n for n in _API77 if not hasattr(_msrc55, n)))

if _have77:
    _d77 = _tf55.mkdtemp()

    # -- a truncated state file must not be handed to Playwright -------------
    _st77 = os.path.join(_d77, 'nubiles-porn_browser_state.json')
    open(_st77, 'w', encoding='utf-8').close()
    _sz77 = os.path.getsize(_st77)   # read BEFORE the call moves it aside
    report(_sz77 == 0, 'fixture: the state file is 0 bytes', str(_sz77))
    report(_msrc55._storage_state_usable(_st77) is False,
       'a 0-byte state file is rejected -- handing it to new_context is what '
       'raised JSONDecodeError in the field')
    report(not os.path.exists(_st77)
           and os.path.exists(_st77 + '.unreadable'),
       'and is moved aside rather than deleted, so the corruption is visible')

    with open(_st77, 'w', encoding='utf-8') as _f77:
        _f77.write('{"cookies": [], "origins": []}')
    report(_msrc55._storage_state_usable(_st77) is True,
       'a state file that still parses is used')
    with open(_st77, 'w', encoding='utf-8') as _f77:
        _f77.write('{"cookies": [')
    report(_msrc55._storage_state_usable(_st77) is False,
       'and a half-written one is rejected too')
    _st77b = os.path.join(_d77, 'never_existed.json')
    report(_msrc55._storage_state_usable(_st77b) is False
           and not os.path.exists(_st77b + '.unreadable'),
       'a missing state file is simply absent, not "unreadable"')

    # -- saving must never truncate the live file ---------------------------
    _wrote77 = []

    class _Ctx77:
        def __init__(self, payload):
            self._payload = payload

        def storage_state(self, path=None):
            _wrote77.append(path)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(self._payload)

    _live77 = os.path.join(_d77, 'live_state.json')
    with open(_live77, 'w', encoding='utf-8') as _f77:
        _f77.write('{"cookies": [{"name": "cf_clearance", "value": "KEEP"}]}')
    _wrote77.clear()
    report(_msrc55._save_storage_state(_Ctx77(''), _live77) is False,
       'a save that produces invalid JSON reports failure')
    report(bool(_wrote77) and _wrote77[0] != _live77
           and _wrote77[0].endswith('.tmp'),
       'and only ever writes a sibling .tmp -- the live file is never opened '
       'for writing, which is what truncated it to 0 bytes on a full disk',
       str(_wrote77[0]) if _wrote77 else '(nothing written)')
    report('KEEP' in open(_live77, encoding='utf-8').read(),
       'so the good state survives a failed save intact')
    report(_msrc55._save_storage_state(_Ctx77('{"cookies": [], "origins": []}'),
                                       _live77) is True
           and json.load(open(_live77, encoding='utf-8')) == {
               'cookies': [], 'origins': []}
           and not os.path.exists(_live77 + '.tmp'),
       'and a good save replaces it, leaving no .tmp behind')
    report(_msrc55._save_storage_state(None, _live77) is False,
       'no context, no save -- close() calls this unconditionally')

    # -- the transport label -------------------------------------------------
    _site77 = dict(_msrc55.METADATA_SITES['nubiles'])
    _MOV77 = {'slug': '256294-stepmom-is-a-great-kisser-s27e1',
              'title': 'Stepmom Is A Great Kisser',
              'url': 'https://nubiles-porn.com/video/watch/256294/slug'}

    class _Sess77:
        def __init__(self, cookie_path=None, on_status=None):
            pass

        def get(self, url, wait_selector=None, timeout=None):
            return '<html><img src="x"></html>'

        def close(self):
            pass

    _html77, _tr77, _ms77 = _msrc55._fetch_page(
        _site77, _MOV77['url'], None, session=_Sess77())
    report(_tr77 == 'browser' and _html77 and isinstance(_ms77, int),
       '_fetch_page names the transport that produced the bytes -- the field '
       'log could not tell a browser that failed the challenge from a plain '
       'request that never tried', f'{_tr77!r} / {_ms77} ms')
    report('via browser' in _msrc55._describe_page('', _tr77, _ms77)
           and ' ms' in _msrc55._describe_page('', _tr77, _ms77),
       'and the diagnostic carries both, so the next log can be judged '
       'against the ~1.5 s a hover can afford',
       _msrc55._describe_page('', _tr77, _ms77))

    _oh77 = _msrc55._fetch_html
    _osess77 = _msrc55._BrowserGallerySession
    _opened77 = []
    try:
        class _SessNone77:
            def __init__(self, cookie_path=None, on_status=None):
                _opened77.append(cookie_path)

            def get(self, url, wait_selector=None, timeout=None):
                return None

            def close(self):
                pass

        _msrc55._BrowserGallerySession = _SessNone77
        _msrc55._fetch_html = lambda url, timeout=20: ''
        _h, _t, _m = _msrc55._fetch_page(_site77, _MOV77['url'], None)
        report(_t == 'none' and bool(_opened77),
           'a browser that yields nothing falls back to a plain request, and '
           'an empty result is labelled "none" rather than left ambiguous',
           f'{_t!r}')
    finally:
        _msrc55._fetch_html = _oh77
        _msrc55._BrowserGallerySession = _osess77

    # -- a real page is not a challenge --------------------------------------
    # The field log labelled this page a bot challenge:
    #   via http, 238 ms, BOT CHALLENGE, not the page, 90852 byte(s), ...
    #   title "Stepmom Fertilizer | Exclusive TeamSkeet Porn Video"
    # It is not one. Every Cloudflare-fronted page carries challenge-platform,
    # so matching it anywhere in the body condemned real pages.
    _TS77 = ('<html><head><title>Stepmom Fertilizer | Exclusive TeamSkeet '
             'Porn Video</title><script src="/cdn-cgi/challenge-platform/h/b/'
             'orchestrate/chl_page.js"></script></head><body>'
             + 'x' * 90000 + '</body></html>')
    report(_msrc55._is_challenge_page(_TS77) is False,
       'a real Cloudflare-fronted page is not called a challenge -- the old '
       'test matched challenge-platform anywhere in the body, which every '
       'such page carries', 'mislabelled a 90852-byte teamskeet page')
    report('BOT CHALLENGE' not in _msrc55._describe_page(_TS77, 'http', 238),
       'so the diagnostic stops reporting a good page as an interstitial',
       _msrc55._describe_page(_TS77, 'http', 238)[:70])
    report(_msrc55._is_challenge_page(
        '<html><head><title>Security Check</title></head></html>') is True,
       'and a genuine interstitial still is one')
    report(_msrc55._is_challenge_page('') is False
           and _msrc55._is_challenge_page('<html>no title here</html>') is False,
       'a page with no title is not guessed at -- guessing wrong here sends a '
       '12 s browser fetch after a page that was already fine')

    # -- permanent covers ----------------------------------------------------
    _FOLDER77 = 'stepmom_fertilizer'
    _PSC77 = ('https://images.psmcdn.net/teamskeet/pervmom/'
              + _FOLDER77 + '/shared/med.jpg')
    _PAGE77b = ('<html><meta property="og:image" content="' + _PSC77 + '">'
                '<img src="https://images.psmcdn.net/teamskeet/pervmom/'
                'some_other_scene/shared/med.jpg"></html>')
    report(_msrc55.signed_cover_url(_PAGE77b, 'Stepmom Fertilizer') == '',
       'a teamskeet page carries no signature at all -- which is precisely '
       'why the refresh found nothing on it in the field, at 238 ms, on a '
       'page that had loaded perfectly')
    report(_msrc55.page_cover_url(_PAGE77b, 'Stepmom Fertilizer') == _PSC77,
       'page_cover_url takes the unsigned permanent cover instead, filtered '
       "to this movie's own CDN folder so a related scene is not shown",
       _msrc55.page_cover_url(_PAGE77b, 'Stepmom Fertilizer'))
    report(_msrc55.permanent_cover_url(_PAGE77b, 'NoSuchScene') == '',
       'and a page holding only other scenes yields nothing')
    report(_msrc55._image_expiry_epoch(_PSC77) == 0,
       'a permanent cover never expires, so it needs none of this re-minting')

    # -- the fetch is bounded ------------------------------------------------
    _oh77 = _msrc55._fetch_html
    _osess77 = _msrc55._BrowserGallerySession
    _opens77 = []
    _probe77 = _msrc55._PROBE_TIMEOUT
    _unreach77 = dict(_msrc55._UNREACHABLE_UNTIL)
    try:
        class _Sess77b:
            def __init__(self, cookie_path=None, on_status=None):
                _opens77.append(cookie_path)

            def get(self, url, wait_selector=None, timeout=None):
                return '<html><title>Real Page</title></html>'

            def close(self):
                pass

        _msrc55._BrowserGallerySession = _Sess77b
        _site77c = dict(_site77)          # a needs_browser site
        _mov77c = dict(_MOV77, url='https://brattysis.com/video/watch/203468/x')

        # A site that answers over plain HTTP must not cost a browser launch.
        _msrc55._fetch_html = lambda url, timeout=20: (
            '<html><title>Stepmom Fertilizer | TeamSkeet</title></html>')
        _h, _t, _m = _msrc55._fetch_page(_site77c, _mov77c['url'], None)
        report(_t == 'http' and not _opens77,
           'a page that plain HTTP serves is not followed by a 25 s browser '
           'launch -- teamskeet answered in 238 ms and the browser was never '
           'needed', f'{_t!r}, {len(_opens77)} browser(s)')

        # Only a real interstitial escalates.
        _msrc55._fetch_html = lambda url, timeout=20: (
            '<html><head><title>Security Check</title></head></html>')
        _h, _t, _m = _msrc55._fetch_page(_site77c, _mov77c['url'], None)
        report(_t == 'browser' and len(_opens77) == 1,
           'and a genuine challenge does escalate to the browser',
           f'{_t!r}, {len(_opens77)} browser(s)')

        # A host that will not answer is remembered, not re-probed per row.
        _msrc55._PROBE_TIMEOUT = 0.02          # 18 ms threshold
        _msrc55._fetch_html = lambda url, timeout=20: (time.sleep(0.05), None)[1]
        _h, _t, _m = _msrc55._fetch_page(_site77c, _mov77c['url'], None)
        report(_t == 'unreachable',
           'burning the whole probe on a connect timeout marks the host '
           'unreachable -- brattysis.com refused TCP outright and every row '
           're-proved it', f'{_t!r} after {_m} ms')
        _t0 = time.time()
        _h, _t, _m = _msrc55._fetch_page(_site77c, _mov77c['url'], None)
        report(_t == 'unreachable' and _m == 0
               and (time.time() - _t0) < 0.02,
           'so the next row of the same network costs nothing instead of '
           'another 128 s', f'{_t!r} in {int((time.time()-_t0)*1000)} ms')
        # The verdict must not outlive the thing that caused it: the nubiles
        # block is an IP ban, and a VPN lifts it without restarting the player.
        _probes77 = []
        _msrc55._fetch_html = lambda url, timeout=20: (_probes77.append(url),
                                                       None)[1]
        _msrc55._fetch_page(_site77c, _mov77c['url'], None)
        report(not _probes77,
           'a host found dead is not re-probed by the next row')
        _forced77 = ''
        try:
            _msrc55._fetch_page(_site77c, _mov77c['url'], None, force=True)
        except TypeError as _e77:
            # A missing parameter has to read as a failure, not abort the
            # section -- a crash here silently drops every assertion below it.
            _forced77 = f'{type(_e77).__name__}: {_e77}'
        report(len(_probes77) == 1,
           'but a link-time refresh re-probes anyway -- the user asked for '
           'that row by name, and switching a VPN on must recover it without '
           'a restart', _forced77 or f'{len(_probes77)} probe(s)')
        report(0 < _msrc55._UNREACHABLE_TTL <= 120.0,
           'and the verdict expires in minutes, not the ten it started at, so '
           'the rest of the playlist recovers on its own',
           f'{_msrc55._UNREACHABLE_TTL:.0f} s')
        report(_msrc55._PROBE_TIMEOUT * 1000 <= 8000
               and _msrc55._BROWSER_TIMEOUT <= 12000,
           'the probe and the browser are both capped, where the old path '
           'spent 25 s in the browser and then 20 s each in curl_cffi, '
           'requests and urllib',
           f'probe {_probe77*1000:.0f} ms, browser '
           f'{_msrc55._BROWSER_TIMEOUT} ms')
    finally:
        _msrc55._fetch_html = _oh77
        _msrc55._BrowserGallerySession = _osess77
        _msrc55._PROBE_TIMEOUT = _probe77
        _msrc55._UNREACHABLE_UNTIL.clear()
        _msrc55._UNREACHABLE_UNTIL.update(_unreach77)

    # -- fetched at link time, not an hour later -----------------------------
    _txt77 = open(_msrc55.__file__, encoding='utf-8').read()
    _body77 = _fn_body(_txt77, 'def _apply_to_file(', ends=('\n    def ',))
    report('refresh_signed_assets_async(' in _body77
           and 'force=True' in _body77,
       'linking a row fetches its cover then and there -- the user is sitting '
       'in the linker and a few seconds costs nothing, which is the moment to '
       'pay for it rather than at the click an hour later')
    _main77 = open(os.path.join(os.path.dirname(
        os.path.abspath(_msrc55.__file__)), 'main.py'),
        encoding='utf-8', errors='replace').read()
    report('warm_previews_async' not in _main77,
       'and the playlist-load batch fetch is gone -- it serialized 27 s '
       'browser timeouts across every row and was neither of the two moments '
       'that matter')

# ---------------------------------------------------------------------------
# 78. Did the browser reuse a clearance, or face the challenge cold?
#
# The field log answered the question it was asked -- "via browser, 16919 ms,
# BOT CHALLENGE, not the page" -- and raised the one that decides whether the
# browser approach is salvageable at all. A context carrying cf_clearance is a
# browser that has already been waved through; a cold one is not. _ensure_started
# printed nothing about which it had built.
# ---------------------------------------------------------------------------
print()
print('--- 78: browser state reuse / challenge short-circuit ---')

if hasattr(_msrc55, '_storage_state_cookie_count'):
    _d78 = _tf55.mkdtemp()
    _st78 = os.path.join(_d78, 'nubiles-porn_browser_state.json')
    with open(_st78, 'w', encoding='utf-8') as _f78:
        json.dump({'cookies': [{'name': 'cf_clearance', 'value': 'x'},
                               {'name': '__cf_bm', 'value': 'y'}],
                   'origins': []}, _f78)
    report(_msrc55._storage_state_cookie_count(_st78) == 2,
       'a saved state reports how many cookies it carries',
       str(_msrc55._storage_state_cookie_count(_st78)))
    with open(_st78, 'w', encoding='utf-8') as _f78:
        _f78.write('{"cookies": []}')
    report(_msrc55._storage_state_cookie_count(_st78) == 0,
       'an empty state says so, which is the cold-start case')
    with open(_st78, 'w', encoding='utf-8') as _f78:
        _f78.write('')
    report(_msrc55._storage_state_cookie_count(_st78) == 0,
       'and a truncated one reads as 0 rather than raising -- this is the same '
       '0-byte file that took the whole refresh path down two commits ago')
    report(_msrc55._storage_state_cookie_count(
        os.path.join(_d78, 'absent.json')) == 0,
       'a missing state reads as 0 too')

    _txt78 = open(_msrc55.__file__, encoding='utf-8').read()
    _body78 = _fn_body(_txt78, 'def _ensure_started(', ends=('\n    def ',))
    report('_storage_state_cookie_count(' in _body78
           and 'starting cold' in _body78,
       'the browser now says out loud whether it reused a clearance or started '
       'cold -- the last log could not tell those apart, and they have '
       'opposite conclusions')
    _body78b = _fn_body(_txt78, '    def get(self, url',
                        ends=('\n    def ',))
    report(_before(_body78b, '_is_challenge_page(', 'wait_for_selector('),
       'an interstitial is returned as soon as it is recognised, instead of '
       'spending the whole timeout waiting for a selector that will never '
       'appear on it -- that wait is what turned a page into 16919 ms',
       f'grace {_msrc55._CHALLENGE_GRACE:.0f}s')
    report(0 < _msrc55._CHALLENGE_GRACE <= 8.0,
       'and the grace given to a challenge is still bounded -- it moved off '
       '5s only because a clicked interstitial now has something to finish',
       f'{_msrc55._CHALLENGE_GRACE:.0f} s')
else:
    report(False, 'the browser-state diagnostic is present',
           'missing: _storage_state_cookie_count')

# ---------------------------------------------------------------------------
# 79. The gate is not Cloudflare. It is the site's own proof of work.
#
# The captured page carries an inline turnstileConfig and a web worker that
# hashes "<challenge>:<nonce>" until the digest has enough leading zero bits,
# then POSTs the nonce to /turnstile/verify. A proof of work is arithmetic, so
# it can be done here -- which matters, because the headless browser spent
# 16919 ms on this exact page and handed back the challenge unsolved.
#
# This section runs against the real captured page, not a reconstruction of it.
# ---------------------------------------------------------------------------
print()
print("--- 79: the site's own proof-of-work gate ---")
import hashlib as _hl79

_CAP79 = os.path.join(os.path.dirname(os.path.abspath(_msrc55.__file__)),
                      'watchpage_244784-my-stepmom-is-the-perfect-date-s5e6.html')
_API79 = ('_turnstile_config', 'solve_turnstile_pow', 'solve_turnstile_challenge',
          '_environment_checks')
# An absent capture is a skip, not a failure: the page is a user-owned
# artifact that gets cleaned out of the working copy, whereas the solver is
# shipped code and its absence has to stay red.
_NOCAP79 = not os.path.isfile(_CAP79)
_miss79 = [n for n in _API79 if not hasattr(_msrc55, n)]
report(not _miss79,
       'the proof-of-work solver is present',
       ('missing: ' + ', '.join(_miss79)) if _miss79 else 'ready')
if _NOCAP79:
    print('  SKIP  ' + os.path.basename(_CAP79) + ' is not in the working '
          'copy, so the solver is not exercised against a captured gate')
_HAVE79 = (not _NOCAP79) and not _miss79

if _HAVE79:
    _page79 = open(_CAP79, encoding='utf-8', errors='replace').read()
    _cfg79 = _msrc55._turnstile_config(_page79)
    report(bool(_cfg79.get('challenge')) and _cfg79.get('difficulty') == 15,
       "the gate's parameters are read out of the page the player actually "
       'captured', json.dumps(_cfg79)[:96])
    report(_msrc55._is_challenge_page(_page79) is True,
       'and that page is recognised as an interstitial')

    _t79 = time.time()
    _nonce79 = _msrc55.solve_turnstile_pow(_cfg79['challenge'],
                                           _cfg79['difficulty'])
    _ms79 = (time.time() - _t79) * 1000
    report(bool(_nonce79),
       'the proof of work is solved here, in Python, with no browser',
       f'nonce {_nonce79}')
    report(_ms79 < 2000,
       'in milliseconds -- the browser spent 16919 ms on this same page and '
       'came back with the challenge still up', f'{_ms79:.0f} ms')

    # Verified by a loop transcribed from the page's own checkLeadingZeroBits,
    # not by the function under test.
    _d79 = _hl79.sha256(
        (_cfg79['challenge'] + ':' + _nonce79).encode('utf-8')).digest()
    _bits79, _done79 = 0, False
    for _byte79 in _d79:
        for _pos79 in range(7, -1, -1):
            if (_byte79 >> _pos79) & 1:
                _done79 = True
                break
            _bits79 += 1
        if _done79:
            break
    report(_bits79 >= int(_cfg79['difficulty']),
       'and the nonce really carries the leading zero bits the gate demands',
       f'{_bits79} bits >= {_cfg79["difficulty"]}')
    report(_msrc55.solve_turnstile_pow(_cfg79['challenge'], 0) == ''
           and _msrc55.solve_turnstile_pow('', 15) == ''
           and _msrc55.solve_turnstile_pow('x', 99) == '',
       'a difficulty that is absent or absurd is refused rather than allowed '
       'to burn the whole nonce budget')
    _env79 = _msrc55._environment_checks()
    report(set(_env79) == {'screenWidth', 'screenHeight', 'hasCanvas',
                           'hasWebGL', 'colorDepth', 'timezoneOffset',
                           'languages', 'platform', 'cookieEnabled'},
       'the environment block matches the keys the page collects, no more',
       str(sorted(_env79)))

    _txt79 = open(_msrc55.__file__, encoding='utf-8').read()
    report(_before(_txt79, 'solve_turnstile_challenge(page_url, html=html)',
                   '_browser_fetch_guard():',
                   _pos(_txt79, 'def _fetch_page(')),
       'the solver is tried before any browser is even considered, and is '
       'handed the page rather than the bare URL')

    _site79 = dict(_msrc55.METADATA_SITES['nubiles'])
    _url79 = 'https://nubiles-porn.com/video/watch/244784/x'
    _oh79 = _msrc55._fetch_html
    _osess79 = _msrc55._BrowserGallerySession
    _ost79 = _msrc55.solve_turnstile_challenge
    _open79 = []
    try:
        class _S79:
            def __init__(self, cookie_path=None, on_status=None):
                _open79.append(cookie_path)

            def get(self, url, wait_selector=None, timeout=None):
                return _page79

            def close(self):
                pass

        _msrc55._BrowserGallerySession = _S79
        _got79 = []
        _msrc55._fetch_html = lambda url, timeout=20: _page79
        _msrc55.solve_turnstile_challenge = (
            lambda url, html=None, timeout=15: (
                _got79.append(html),
                '<html><title>The Real Page</title></html>')[1])
        _h, _t, _m = _msrc55._fetch_page(_site79, _url79, None)
        report(_t == 'turnstile' and not _open79 and 'Real Page' in _h,
           'so a gate we can solve ourselves is solved, and no browser is '
           'launched for it', f'{_t!r}, {len(_open79)} browser(s)')
        report(bool(_got79) and _got79[0] == _page79,
           'and the page already fetched is handed to the solver instead of '
           'being fetched a second time -- a second request from the same IP '
           'comes back as HTTP 429 with an empty body, which is what a run '
           'reported as "no gate config"',
           f'{len(_got79[0] or "") if _got79 else 0} byte(s) passed on')

        # A gate the solver cannot crack is not worth a browser: it only
        # starts on a click, which a headless browser will not make.
        _msrc55.solve_turnstile_challenge = (
            lambda url, html=None, timeout=15: '')
        _h, _t, _m = _msrc55._fetch_page(_site79, _url79, None)
        report(_t == 'turnstile-failed' and not _open79,
           'a proof-of-work gate that refuses us is reported as such rather '
           "than handed to a browser that would spend 10 s failing to click "
           'a checkbox', f'{_t!r}, {len(_open79)} browser(s)')

        # A challenge that is NOT this gate still gets the browser.
        _other79 = '<html><head><title>Just a moment...</title></head></html>'
        _msrc55._fetch_html = lambda url, timeout=20: _other79
        _msrc55.solve_turnstile_challenge = (
            lambda url, html=None, timeout=15: '')
        _h, _t, _m = _msrc55._fetch_page(_site79, _url79, None)
        report(_t == 'browser' and len(_open79) == 1,
           'and some other kind of interstitial still escalates to the '
           'browser', f'{_t!r}, {len(_open79)} browser(s)')
    finally:
        _msrc55._fetch_html = _oh79
        _msrc55._BrowserGallerySession = _osess79
        _msrc55.solve_turnstile_challenge = _ost79
        _msrc55._UNREACHABLE_UNTIL.clear()

# ---------------------------------------------------------------------------
# 80. Which build is running, and no exit that cannot be seen.
#
# A run came back with the browser on the challenge page and not one [TURNSTILE]
# line. solve_turnstile_challenge had two ways out that printed nothing --
# ImportError, and a page carrying no gate config -- so the log could not
# distinguish "the solver ran and bailed" from "the solver was never in the
# file that was running". Both are unobservable failures, and the second has
# cost whole test runs in this project because files are copied by hand.
# ---------------------------------------------------------------------------
print()
print('--- 80: build marker / observable exits ---')

report(bool(getattr(_msrc55, 'BUILD', '')),
       'metadata_scraper carries a build marker',
       str(getattr(_msrc55, 'BUILD', '(none)')))
_txt80 = open(_msrc55.__file__, encoding='utf-8').read()
report('print(f"[MetadataScraper] build {BUILD}")' in _txt80,
       'and prints it on import, so the first line of any log says which file '
       'is actually running -- files here are copied by hand, and "did the new '
       'code even run?" has burned test runs before')

_body80 = _fn_body(_txt80, 'def solve_turnstile_challenge(')
_lines80 = _body80.split('\n')
_silent80 = []
for _n80, _ln80 in enumerate(_lines80):
    if 'return ""' in _ln80:
        _ctx80 = '\n'.join(_lines80[max(0, _n80 - 3):_n80])
        if 'print(' not in _ctx80:
            _silent80.append(_ln80.strip())
report(not _silent80,
       'every way out of the solver says why -- two of them were silent, which '
       'is exactly why a failed run could not be told apart from a stale file',
       '; '.join(_silent80) or f'{_body80.count(chr(114) + "eturn") } returns, all narrated')
report('[TURNSTILE] gate on ' in _body80,
       'and a gate that IS found announces itself before solving, so a log with '
       'no [TURNSTILE] line at all now means the code never ran')

_have80t = hasattr(_msrc55, '_page_title')
report(_have80t, 'the page-title reader the solver narrates with is present',
       'missing: _page_title')
if _have80t:
    report(_msrc55._page_title(
        '<html><head><title>  Security\n Check </title></head></html>')
           == 'Security Check',
           'the page title is read and whitespace-collapsed')
    report(_msrc55._page_title('<html>no title</html>') == ''
           and _msrc55._page_title('') == '',
           'and a page with no title yields empty rather than raising')

    if _HAVE79:
        report(_msrc55._page_title(_page79) == 'Security Check',
           'the captured gate page reads as "Security Check"',
           repr(_msrc55._page_title(_page79)))

# ---------------------------------------------------------------------------
# 81. Search every network at once, show what decided the match, and let the
#     user keep a performer out of the name.
# ---------------------------------------------------------------------------
print()
print('--- 81: all-network search / criteria / hidden performers ---')

_API81 = ('match_candidates_all_sites', 'signal_breakdown', 'hide_performer',
          'load_hidden_performers', 'ALL_SITES_ID')
_have81 = all(hasattr(_msrc55, n) for n in _API81)
report(_have81, 'the all-network search, criteria and hidden-performer API is here',
       'missing: ' + ', '.join(n for n in _API81 if not hasattr(_msrc55, n)))

if _have81:
    # -- the breakdown must agree with the number that actually ranked it ----
    _dbf81s = os.path.join(_tf55.mkdtemp(), 'strength81.json')
    with open(_dbf81s, 'w', encoding='utf-8') as _f81:
        json.dump({'movies': {}}, _f81)
    _tm81 = _msrc55.TitleMatcher(_msrc55.MetadataDB(_dbf81s))
    for _sigs81 in (['scene'], ['scene', 'duration'], ['series', 'site'],
                    ['model:a', 'model:b'], ['model:a', 'model:b', 'model:c'],
                    ['scene', 'model:a', 'model:b', 'model:c', 'duration'],
                    ['id'], ['id', 'scene'], []):
        _bd81 = _msrc55.signal_breakdown(_sigs81)
        _want81 = _tm81._signal_strength(list(_sigs81))
        _got81 = (float(_bd81.rsplit('= ', 1)[1].split()[0])
                  if '= ' in _bd81 else 0.0)
        report(abs(_got81 - _want81) < 1e-9 or not _sigs81,
           f'the breakdown of {_sigs81} totals the strength that ranked it',
           f'shown {_got81} vs strength {_want81}')
    report('capped' in _msrc55.signal_breakdown(
        ['model:a', 'model:b', 'model:c']),
       'and says when the cast weight was capped, rather than quietly summing '
       'three performers past the cap',
       _msrc55.signal_breakdown(['model:a', 'model:b', 'model:c']))
    report('lone video id' in _msrc55.signal_breakdown(['id']),
       'a bare video id is labelled as the weak signal it is',
       _msrc55.signal_breakdown(['id']))

    # -- two networks, one ranking ------------------------------------------
    _dir81 = _tf55.mkdtemp()
    _sa81 = {'id': '__s81a__', 'name': 'Network A',
             'db_filename': 's81a.json', 'overrides_filename': 'o81a.json'}
    _sb81 = {'id': '__s81b__', 'name': 'Network B',
             'db_filename': 's81b.json', 'overrides_filename': 'o81b.json'}
    # meta_fetched is what _build_index keys on; without it a record exists in
    # the database and is invisible to the matcher.
    _rows_a = {'a-1': {'slug': 'a-1', 'title': 'Stepmom Is A Great Kisser',
                       'series': 'MomsTeachSex', 'models': ['Amirah Adara'],
                       'date': '12/03/2024', 'video_id': '256294',
                       'meta_fetched': True}}
    _rows_b = {'b-1': {'slug': 'b-1', 'title': 'Stepmom Is A Great Kisser',
                       'series': 'MomsTeachSex', 'models': ['Amirah Adara'],
                       'date': '12/03/2024', 'video_id': '999999',
                       'meta_fetched': True},
               'b-2': {'slug': 'b-2', 'title': 'Totally Unrelated Scene',
                       'series': 'OtherSeries', 'models': ['Someone Else'],
                       'date': '01/01/2020', 'video_id': '111111',
                       'meta_fetched': True}}
    for _fn81, _rows81 in (('s81a.json', _rows_a), ('s81b.json', _rows_b)):
        with open(os.path.join(_dir81, _fn81), 'w', encoding='utf-8') as _f81:
            json.dump({'movies': _rows81}, _f81)

    _saved81 = dict(_msrc55.METADATA_SITES)
    _msrc55.METADATA_SITES['__s81a__'] = _sa81
    _msrc55.METADATA_SITES['__s81b__'] = _sb81

    class _Player81:
        pass

    _p81 = _Player81()
    _p81.data_dir = _dir81
    _p81._metadata_dbs = {}
    _p81._metadata_matchers = {}
    try:
        _cands81 = _msrc55.match_candidates_all_sites(
            _p81, 'Amirah Adara Stepmom Is A Great Kisser MomsTeachSex 256294',
            limit=5)
        _nets81 = [c.get('site_id') for c in _cands81]
        report('__s81a__' in _nets81 and '__s81b__' in _nets81,
           'one query returns candidates from every network, so the right '
           'answer is no longer invisible because the wrong network was '
           'picked first', str(_nets81))
        report(all(c.get('site_name') for c in _cands81),
           'and each candidate names the network it came from, so the card '
           'can show it')
        _str81 = [float(c.get('strength') or 0) for c in _cands81]
        report(_str81 == sorted(_str81, reverse=True),
           'merged candidates are ranked together on strength, not listed '
           'network by network', str(_str81))
        _top81 = _cands81[0]
        report((_top81.get('movie') or {}).get('video_id') in ('256294', '999999'),
           'the decisive video id decides which of two same-title scenes wins')
        report(_msrc55.match_candidates_all_sites(
            _p81, 'Amirah Adara Stepmom Is A Great Kisser', limit=1).__len__() == 1,
           'and the limit is applied after merging, not per network')
        report(_msrc55.match_candidates_all_sites(
            _p81, 'nothing here matches at all xyzzy', limit=5) == [],
           'a query with nothing in common still returns nothing')
    finally:
        _msrc55.METADATA_SITES.clear()
        _msrc55.METADATA_SITES.update(_saved81)
        if hasattr(_msrc55, '_SITE_MATCHERS'):
            _msrc55._SITE_MATCHERS.clear()

    # -- hidden performers ---------------------------------------------------
    _d81h = _tf55.mkdtemp()
    _prev81 = set(_msrc55._HIDDEN_PERFORMERS)
    try:
        _msrc55._HIDDEN_PERFORMERS.clear()
        _mov81 = {'title': 'My Stepsis And I Share Everything',
                  'series': 'CumSwappingSis',
                  'models': ['Harley King', 'Molly Little', 'Rex Roundly'],
                  'date': '12/03/2024'}
        report('Rex Roundly' in _msrc55.format_display_name(_mov81),
           'an unlisted male performer starts out in the name -- the built-in '
           'list holds 81 of the 1916 performers in the shipped nubiles DB')
        report(_msrc55.hide_performer(_d81h, 'Rex Roundly') is True,
           'and can be dropped from every name from then on')
        _name81 = _msrc55.format_display_name(_mov81)
        report('Rex Roundly' not in _name81
               and 'Harley King' in _name81 and 'Molly Little' in _name81,
           'the hidden performer is gone and the others are untouched',
           _name81)
        report(os.path.isfile(os.path.join(_d81h, 'male_performers.json'))
               and not os.path.exists(
                   os.path.join(_d81h, 'male_performers.json.tmp')),
           'it is saved next to the databases, atomically, with no .tmp left')
        _msrc55._HIDDEN_PERFORMERS.clear()
        report('Rex Roundly' in _msrc55.format_display_name(_mov81),
           'clearing memory brings him back, so the test really is the file')
        _msrc55.load_hidden_performers(_d81h)
        report('Rex Roundly' not in _msrc55.format_display_name(_mov81),
           'and reloading from disk hides him again -- it survives a restart')
        with open(os.path.join(_d81h, 'male_performers.json'), 'w',
                  encoding='utf-8') as _f81:
            _f81.write('')
        report(_msrc55.load_hidden_performers(_d81h) == set(),
           'a truncated list reads as empty rather than raising')
        report(_msrc55.hide_performer(_d81h, '   ') is False,
           'and a blank name is refused')
    finally:
        _msrc55._HIDDEN_PERFORMERS.clear()
        _msrc55._HIDDEN_PERFORMERS.update(_prev81)

    # -- the dialog is wired to it ------------------------------------------
    _txt81 = open(_msrc55.__file__, encoding='utf-8').read()
    report('"All networks", ALL_SITES_ID' in _txt81,
       'the linker offers searching every network at once')
    _body81 = _fn_body(_txt81, '    def _apply_to_file(')
    report('site = METADATA_SITES.get(site_id) or self.site' in _body81,
       'and applying a row records the network its candidate came from, not '
       'whichever one the dialog happens to be showing')
    report('pyqtSignal(str, dict, str)' in _txt81,
       'the card carries that network back when it is applied')
    report('_hide_performer_menu' in _txt81 and 'male_performers.json' in _txt81,
       'and the card has a way to hide a performer without editing JSON')

# ---------------------------------------------------------------------------
# 82. Cast list coverage, and the hover card's size / position / clip rules.
# ---------------------------------------------------------------------------
print()
print('--- 82: cast list and hover card ---')

_new82 = ['charlie dean', 'marcus london', 'mike ox', 'anthony pierce',
          'kristof cale', 'apollo banks', 'nade nasty', 't stone']
report(all(n in _msrc55.MALE_PERFORMERS for n in _new82),
   'the credited men the shipped nubiles database actually carries are on the '
   'list -- it covered 81 of 1916 performers, so most reached the name',
   'missing: ' + ', '.join(n for n in _new82 if n not in _msrc55.MALE_PERFORMERS))

_women82 = ['Molly Little', 'Kyler Quinn', 'Alex Coal', 'Riley Reid',
            'Lulu Chu', 'Amirah Adara', 'Chloe Temple', 'Jodie Johnson',
            'Tiffany Tatum', 'Haley Reed', 'Anya Olsen', 'Kiara Cole',
            'Lexi Luna', 'Eliza Ibarra', 'Elsa Jean', 'Piper Perri']
report(not any(_msrc55._is_male_performer(w) for w in _women82),
   'and the most prolific women in that database are not caught by it -- '
   'hiding a performer the user wants is worse than one slipping through',
   'wrongly hidden: ' + ', '.join(
       w for w in _women82 if _msrc55._is_male_performer(w)))

_mov82 = {'title': 'Stepsis Loves Sex Games', 'series': 'BrattySis',
          'models': ['Bianca Bangs', 'Charlie Dean'], 'date': '29/11/2024'}
report(_msrc55.format_display_name(_mov82)
       == 'BrattySis - Bianca Bangs - Stepsis Loves Sex Games - 29/11/2024',
   'so a man credited alongside a woman no longer reaches the new name',
   _msrc55.format_display_name(_mov82))

# -- the hover card ----------------------------------------------------------
_main82 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py')
if os.path.isfile(_main82):
    _txt82 = open(_main82, encoding='utf-8').read()
    _mv82 = _fn_body(_txt82, '    def _move_preview_widget(')
    report('sizeHint()' in _mv82 and '.width()\n' not in _mv82[:200],
       'the card is positioned from its size hint, not from width()/height() '
       '-- before it has been shown once those are pre-layout values, and a '
       'height that reads too large parks it in the top-left corner',
       'no sizeHint in _move_preview_widget')
    report(_mv82.count('max(edge, min(') == 2,
       'and both axes are clamped into the window afterwards, so a flip that '
       'overshoots cannot leave it at the corner')
    report('HOVER_COVER_HOLD_MS = 1000' in _txt82,
       'the cover holds for a full second before the clip takes over',
       re.search(r'HOVER_COVER_HOLD_MS = \d+', _txt82).group(0)
       if re.search(r'HOVER_COVER_HOLD_MS = \d+', _txt82) else 'not found')
    report('setLoops(-1)' in _txt82,
       'and the clip loops rather than playing once and freezing on its last '
       'frame')
    report('HOVER_PREVIEW_W = 340' in _txt82,
       'the card is wider than it was (220 px of media)')
    report('setFixedSize(self.HOVER_PREVIEW_W - 16' in _txt82,
       'the clip surface is sized to the card instead of taking the layout '
       'default, which is what made the card change shape on the swap')
else:
    report(False, 'main.py is present to check the hover card against', 'missing')

# ---------------------------------------------------------------------------
# 83. The hover clip loops. Compiled out of main.py and driven for real, with a
#     stub media player -- grep would only prove the call exists, which is
#     exactly the mistake that shipped "it already loops".
# ---------------------------------------------------------------------------
print()
print('--- 83: the hover clip loops ---')


def _vfn83(name):
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == 'VideoPlayer')
    fn = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name == name), None)
    if fn is None:
        return None
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = dict(G)
    ns['QMediaPlayer'] = _QMP83
    exec(compile(mod, f'<VideoPlayer.{name}>', 'exec'), ns)
    return ns[name]


class _MS83:
    NoMedia = 'NoMedia'
    LoadingMedia = 'LoadingMedia'
    LoadedMedia = 'LoadedMedia'
    BufferingMedia = 'BufferingMedia'
    BufferedMedia = 'BufferedMedia'
    EndOfMedia = 'EndOfMedia'
    InvalidMedia = 'InvalidMedia'


class _PS83:
    StoppedState = 'StoppedState'
    PlayingState = 'PlayingState'
    PausedState = 'PausedState'


class _QMP83:
    MediaStatus = _MS83
    PlaybackState = _PS83


class _Player83:
    def __init__(self):
        self.calls = []
        self.state = _PS83.PlayingState
        self.pos = 0
        self.dur = 10000

    def setPosition(self, v):
        self.calls.append(('setPosition', v))
        self.pos = v

    def play(self):
        self.calls.append(('play',))

    def playbackState(self):
        return self.state

    def position(self):
        return self.pos

    def duration(self):
        return self.dur


class _Hold83:
    def __init__(self, active=False):
        self._active = active
        self.stopped = 0

    def isActive(self):
        return self._active

    def stop(self):
        self.stopped += 1
        self._active = False

    def start(self, *a):
        self._active = True


class _W83:
    def __init__(self):
        self.hidden = 0
        self.shown = 0

    def hide(self):
        self.hidden += 1

    def show(self):
        self.shown += 1


class _Timer83:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _Clip83:
    def __init__(self):
        self._hover_clip_wanted = True
        self._hover_clip_ready = True
        self._hover_clip_player = _Player83()
        self._hover_clip_timer = _Timer83()
        self._hover_cover_hold_timer = _Hold83(False)
        self._hover_cover_poll_timer = _Timer83()
        self._hover_clip_watchdog = _Hold83(False)
        self._hover_clip_repeat_timer = _Hold83(True)
        self._hover_clip_last_pos = -1
        self._hover_clip_stall = 0
        self.hover_preview_image = _W83()
        self.hover_preview_video = _W83()
        self.events = []

    # No stub for _swap_to_hover_clip: the real one is bound below, so a fake
    # here would just be shadowed and quietly stop proving anything.
    def _abandon_hover_clip(self):
        self.events.append('abandon')


_Clip83._on_hover_clip_status = _vfn83('_on_hover_clip_status')
_Clip83._restart_hover_clip = _vfn83('_restart_hover_clip')
_Clip83._swap_to_hover_clip = _vfn83('_swap_to_hover_clip')
_Clip83._check_hover_clip_started = _vfn83('_check_hover_clip_started')
_Clip83._on_hover_clip_repeat_tick = _vfn83('_on_hover_clip_repeat_tick')

# Gated separately on purpose. One gate for the whole section meant that a
# missing watchdog silently skipped the swap test too -- and the swap is the
# one that regressed into the black screen, so it has to fail on its own.
_core83 = ('_on_hover_clip_status', '_restart_hover_clip', '_swap_to_hover_clip')
_have83 = all(getattr(_Clip83, n, None) is not None for n in _core83)
report(_have83, 'the clip status, swap and restart paths are in main.py',
       'missing: ' + ', '.join(n for n in _core83
                               if getattr(_Clip83, n, None) is None))
_have_repeat83 = getattr(_Clip83, '_on_hover_clip_repeat_tick', None) is not None
report(_have_repeat83,
       'and there is a loop that does not wait to be told the clip ended')
_have_watch83 = getattr(_Clip83, '_check_hover_clip_started', None) is not None
report(_have_watch83,
       'and there is a watchdog to give the cover back when a clip fails')

if _have83:
    _c83 = _Clip83()
    _c83._on_hover_clip_status(_MS83.EndOfMedia)
    report(_c83._hover_clip_player.calls == [('setPosition', 0), ('play',)],
       'when the clip reaches its end it is wound back to the start and played '
       'again -- setLoops(-1) alone does not loop a remote stream on the '
       'FFmpeg backend, which is why it played once and froze',
       str(_c83._hover_clip_player.calls))

    _c83 = _Clip83()
    _c83._hover_clip_ready = False
    _c83._on_hover_clip_status(_MS83.EndOfMedia)
    report(_c83._hover_clip_ready is True
           and _c83.hover_preview_video.shown == 1
           and _c83._hover_clip_player.calls == [('play',)],
       'and a clip that runs out before the cover hold is up is taken as '
       'ready and swapped in rather than dropped',
       f'ready={_c83._hover_clip_ready}, '
       f'surface shown={_c83.hover_preview_video.shown}, '
       f'{_c83._hover_clip_player.calls}')

    _c83 = _Clip83()
    _c83._on_hover_clip_status(_MS83.InvalidMedia)
    report(_c83.events == ['abandon'] and not _c83._hover_clip_player.calls,
       'a clip that will not decode is still given up on, not restarted '
       'forever', f'{_c83.events}, {_c83._hover_clip_player.calls}')

    _c83 = _Clip83()
    _c83._hover_clip_wanted = False
    _c83._on_hover_clip_status(_MS83.EndOfMedia)
    report(not _c83._hover_clip_player.calls and not _c83.events,
       'and once the card is gone nothing is restarted behind it',
       f'{_c83._hover_clip_player.calls}, {_c83.events}')

    _c83 = _Clip83()
    _c83._hover_clip_player = None
    _c83._on_hover_clip_status(_MS83.EndOfMedia)
    report(True, 'a restart with no player left is a no-op rather than a crash')

    # The swap must hand the stream to play() and nothing else. A seek here
    # aborts the open on the FFmpeg backend, which is what the black screen was.
    _c83 = _Clip83()
    _c83._swap_to_hover_clip()
    report(_c83._hover_clip_player.calls == [('play',)],
       'the swap asks for playback and does not seek -- setPosition(0) at this '
       'point aborts the open ("Immediate exit requested", "partial file", '
       '"Demuxing failed") and leaves the video surface showing nothing',
       str(_c83._hover_clip_player.calls))
    report(_c83.hover_preview_image.hidden == 1
           and _c83.hover_preview_video.shown == 1,
       'and it really does put the clip surface up in place of the cover')
    report(_c83._hover_clip_watchdog.isActive(),
       'a watchdog is armed at the swap, so a clip that parses and then fails '
       'cannot leave a black rectangle behind')

if _have_repeat83:
    # The teamskeet trailers never signalled EndOfMedia at all, so the loop
    # cannot depend on being told. These drive the position-watching tick.
    _c83 = _Clip83()
    _c83._hover_clip_player.pos = 9800
    _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [('setPosition', 0), ('play',)],
       'a clip that has run to the end of its duration is wound back and '
       'replayed without waiting for EndOfMedia -- the teamskeet trailers on '
       'images.psmcdn.net never sent it, which is why only those failed to '
       'repeat', str(_c83._hover_clip_player.calls))

    _c83 = _Clip83()
    _c83._hover_clip_player.state = _PS83.StoppedState
    _c83._hover_clip_player.pos = 4000
    _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [('setPosition', 0), ('play',)],
       'and one the player has quietly stopped is restarted too',
       str(_c83._hover_clip_player.calls))

    _c83 = _Clip83()
    for _p83 in (1000, 2000, 3000):
        _c83._hover_clip_player.pos = _p83
        _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [],
       'a clip still moving through the middle of itself is left alone',
       str(_c83._hover_clip_player.calls))

    _c83 = _Clip83()
    _c83._hover_clip_player.pos = 5000
    _c83._on_hover_clip_repeat_tick()          # takes the baseline
    for _ in range(5):
        _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [],
       'a clip that has only just stopped moving is given a moment',
       str(_c83._hover_clip_player.calls))
    _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [('setPosition', 0), ('play',)],
       'and one claiming to play while its clock stands still for six ticks '
       'is restarted rather than left frozen',
       str(_c83._hover_clip_player.calls))

    _c83 = _Clip83()
    _c83._hover_clip_wanted = False
    _c83._hover_clip_player.pos = 9900
    _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == []
           and not _c83._hover_clip_repeat_timer.isActive(),
       'once the card is gone the loop stops itself and restarts nothing',
       f'{_c83._hover_clip_player.calls}, timer={_c83._hover_clip_repeat_timer.isActive()}')

    _c83 = _Clip83()
    _c83._hover_clip_player.dur = 0
    _c83._hover_clip_player.pos = 0
    for _ in range(3):
        _c83._on_hover_clip_repeat_tick()
    report(_c83._hover_clip_player.calls == [],
       'a stream that has not reported a duration yet is not restarted on a '
       'guess', str(_c83._hover_clip_player.calls))

if _have_watch83:
    _c83 = _Clip83()
    _c83._hover_clip_player.state = _PS83.StoppedState
    _c83._check_hover_clip_started()
    report(_c83.events == ['abandon'],
       'a clip that never reached PlayingState gives the cover back',
       str(_c83.events))

    _c83 = _Clip83()
    _c83._hover_clip_player.state = _PS83.PlayingState
    _c83._check_hover_clip_started()
    report(_c83.events == [],
       'and one that is playing is left alone', str(_c83.events))

    _c83 = _Clip83()
    _c83._hover_clip_player = None
    _c83._check_hover_clip_started()
    report(_c83.events == ['abandon'],
       'a released player counts as not playing rather than raising',
       str(_c83.events))

    _c83 = _Clip83()
    _c83._hover_clip_wanted = False
    _c83._hover_clip_player.state = _PS83.StoppedState
    _c83._check_hover_clip_started()
    report(_c83.events == [],
       'once the card is gone the watchdog does nothing', str(_c83.events))

# ---------------------------------------------------------------------------
# 84. Marking one row must not mark another. Two unrelated bunkr links were
#     marked together, and one porn00 row marked every porn00 row, because
#     both names yielded the same invented JAV code.
# ---------------------------------------------------------------------------
print()
print('--- 84: seen keys do not collide across unrelated rows ---')

_g84 = {'re': re, 'unquote': unquote}
_g84.update(_LIFT_HELPERS)
_fn84 = next((n for n in TREE.body
              if isinstance(n, ast.ClassDef) and n.name == 'VideoPlayer'), None)
_code84 = _bl84 = None
if _fn84 is not None:
    for _n84 in _fn84.body:
        if isinstance(_n84, ast.FunctionDef) and _n84.name == '_extract_jav_code':
            _m84 = ast.Module(body=[_n84], type_ignores=[])
            ast.fix_missing_locations(_m84)
            exec(compile(_m84, '<_extract_jav_code>', 'exec'), _g84)
            _code84 = _g84['_extract_jav_code']
        if isinstance(_n84, ast.Assign) and any(
                getattr(t, 'id', '') == '_JAV_CODE_PREFIX_BLACKLIST'
                for t in _n84.targets):
            _m84 = ast.Module(body=[_n84], type_ignores=[])
            ast.fix_missing_locations(_m84)
            exec(compile(_m84, '<_JAV_CODE_PREFIX_BLACKLIST>', 'exec'), _g84)
            _bl84 = _g84['_JAV_CODE_PREFIX_BLACKLIST']


class _J84:
    _extract_jav_code = _code84
    _JAV_CODE_PREFIX_BLACKLIST = _bl84


_have84 = _code84 is not None and _bl84 is not None
report(_have84, 'the JAV code extractor and its prefix blacklist are in main.py',
       f'fn={_code84 is not None}, blacklist={_bl84 is not None}')

if _have84:
    _j84 = _J84()
    _a84 = 'AnalTherapyXXX.26.06.30.Aleksa.Mink.Do.Not.Disturb.XXX.1080p.mp4'
    _b84 = 'PerfectGirlfriend.26.07.04.Jessi.Rae.The.Risky.Text.XXX.1080p.mp4'
    report(_j84._extract_jav_code(_a84) != _j84._extract_jav_code(_b84)
           or _j84._extract_jav_code(_a84) == '',
       'two unrelated release names no longer share a key -- both of these '
       'read as XXX-1080, taken from the porn marker and the resolution, so '
       'marking one marked the other',
       f'{_j84._extract_jav_code(_a84)!r} vs {_j84._extract_jav_code(_b84)!r}')

    _sites84 = ['Porn00XXX', 'AnalTherapyXXX', 'PerfectGirlfriendXXX',
                'WetVRXXX', 'LetsDoeItXXX']
    _keys84 = {_j84._extract_jav_code(
        f'{s}.26.07.04.Some.Title.Here.XXX.1080p.mp4') for s in _sites84}
    report(_keys84 == {''},
       'and a whole site\'s rows, all named the same way, no longer collapse '
       'onto one key -- marking one porn00 row used to mark every porn00 row',
       str(_keys84))

    report('XXX' in _bl84,
       'XXX is blacklisted as a prefix: it is a porn marker these names '
       'carry, not a studio label')

    for _real84, _want84 in (('SSIS-123 Some Title 1080p.mp4', 'SSIS-123'),
                             ('MIDE-480 Another Title.mp4', 'MIDE-480'),
                             ('IPX-999 Real Code 720p.mkv', 'IPX-999'),
                             ('ABW-123.mp4', 'ABW-123'),
                             ('FC2-PPV-1234567 something.mp4', 'FC2-PPV-1234567'),
                             ('HEYZO-1234 clip.mp4', 'HEYZO-1234')):
        report(_j84._extract_jav_code(_real84) == _want84,
           f'a real code is still read out of {_real84!r}',
           f'{_j84._extract_jav_code(_real84)!r} != {_want84!r}')

    report(_j84._extract_jav_code('MIDE-480 Another Title.mp4') == 'MIDE-480'
           and _j84._extract_jav_code('Whatever XXX 480p.mp4') == '',
       'the resolution guard needs the digits to be followed by P, so a code '
       'that happens to end in 480 survives and 480p does not become one')

# ---------------------------------------------------------------------------
# 85. Renaming rows through the metadata linker must be allowed to combine
#     them. Three fileditch quality variants of one scene stayed three rows.
# ---------------------------------------------------------------------------
print()
print('--- 85: a linker rename can combine fileditch variants ---')

_g85 = {'re': re, 'os': os, 'unquote': unquote, 'html_unescape': html_unescape,
        'urlparse': urlparse, 'parse_qs': parse_qs, 'urlunparse': urlunparse}
_g85.update(_LIFT_HELPERS)
_cls85 = next((n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == 'VideoPlayer'), None)
_want85 = ('_mirror_display_group_key', '_fileditch_filename_from_url',
           '_mirror_title_identity_key', '_is_fileditch_host')
_got85 = set()
if _cls85 is not None:
    for _n85 in _cls85.body:
        if isinstance(_n85, ast.FunctionDef) and _n85.name in _want85:
            _m85 = ast.Module(body=[_n85], type_ignores=[])
            ast.fix_missing_locations(_m85)
            exec(compile(_m85, f'<{_n85.name}>', 'exec'), _g85)
            _got85.add(_n85.name)

_urls85 = [
    'https://fileditchfiles.st/alpha29/3fada8aa128cb6223418/'
    'pervmom_alex_harper_full_720.mp4',
    'https://fileditchfiles.st/alpha29/4eeb0b1aa65c0607f6cf/'
    'pervmom_alex_harper_full_1080.mp4',
    'https://fileditchfiles.st/alpha29/52899c9bb4bdf2a88309/'
    'pervmom_alex_harper_full_2160.mp4',
]
_name85 = ('PervMom - Alex Harper - Use My Pussy and Keep Your Scolarship! '
           '- 13/09/2026')


class _Fake85:
    _mirror_display_group_key = _g85.get('_mirror_display_group_key')
    _fileditch_filename_from_url = _g85.get('_fileditch_filename_from_url')
    _mirror_title_identity_key = _g85.get('_mirror_title_identity_key')
    _is_fileditch_host = _g85.get('_is_fileditch_host')

    def __init__(self, overrides, disp=None):
        self._ov = overrides
        self._disp = disp or {}
        self._metadata_name_overrides = {}
        self._stream_resolution_cache = {}
        self._mirror_group_cache = {}

    def _is_remote_url(self, v):
        return str(v).startswith('http')

    def _canonicalize_remote_source_url(self, v):
        return v

    def _get_name_override(self, p):
        return self._ov.get(p, '')

    def _recent_file_saved_name(self, p):
        return ''

    def _playlist_display_name(self, p):
        return ''

    def _strip_seen_display_prefix(self, t):
        return t

    def _is_vidara_host(self, h):
        return False

    def _is_pornhub_host(self, h):
        return False

    def _jav_code_from_text(self, t):
        return ''

    def _is_generic_embed_title(self, t, p):
        return False


_have85 = _got85 == set(_want85)
report(_have85, 'the mirror group key and the fileditch filename rule are here',
       'missing: ' + ', '.join(sorted(set(_want85) - _got85)))

if _have85:
    _un85 = [_Fake85({})._mirror_display_group_key(u) for u in _urls85]
    report(len(set(_un85)) == 3 and all(_un85),
       'three fileditch files nobody has named still key apart -- that is the '
       'rule _split_conflicting_fileditch_mirrors depends on, and a rename '
       'must not weaken it for rows the user has not touched', str(_un85))

    _ov85 = {u: _name85 for u in _urls85}
    _ren85 = [_Fake85(_ov85)._mirror_display_group_key(u) for u in _urls85]
    report(len(set(_ren85)) == 1 and bool(_ren85[0]),
       'and once the linker has named all three the same, they share one key '
       'so the collapse pass folds them into a single row -- the 720 / 1080 / '
       '2160 variants of one scene were three rows before', str(_ren85[0]))

    _part85 = dict(_ov85)
    _part85.pop(_urls85[2])
    _mix85 = [_Fake85(_part85)._mirror_display_group_key(u) for u in _urls85]
    report(_mix85[0] == _mix85[1] and _mix85[2] != _mix85[0],
       'a row the user has not renamed stays separate from the two they have',
       str(_mix85))

    # A bunkr row and a fileditch row of the same video, across hosts, with no
    # rename on either: the display name carries "Fun &amp; Games" and the
    # fileditch filename carries "Fun_Games.mp4".
    _bunkr85 = 'https://bunkr.pk/f/C1WQFFpDS21QN'
    _fitch85 = ('https://fileditchfiles.st/alpha24/e84a701c612d8ad04ad7/'
                'Cassie2Sassy_26.08.02_Fun_Games.mp4')
    _disp85 = {'Cassie2Sassy 26.08.02 Fun &amp; Games.mp4': None}
    _t85 = 'Cassie2Sassy 26.08.02 Fun &amp; Games.mp4'
    _d85 = {_bunkr85: _t85, _fitch85: _t85}

    class _Named85(_Fake85):
        def _playlist_display_name(self, p):
            return self._disp.get(p, '')

    _cross85 = _Named85({}, _d85)
    _kb85 = _cross85._mirror_display_group_key(_bunkr85)
    _kf85 = _cross85._mirror_display_group_key(_fitch85)
    report(bool(_kb85) and _kb85 == _kf85,
       'the same video on bunkr and on fileditch groups with no rename at '
       'all -- "&" is punctuation, not a word, and keeping it made one key '
       "'fun & games' and the other 'fun games'",
       f'{_kb85!r} vs {_kf85!r}')

    _txt85 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'main.py'), encoding='utf-8').read()
    _split85 = _fn_body(_txt85, '    def _split_conflicting_fileditch_mirrors(')
    report('same_override' in _split85,
       'and the fileditch splitter agrees, so it does not pull apart the '
       'variants the group key has just folded together')
    _ms85 = open(_msrc55.__file__, encoding='utf-8').read()
    report('_flush_collapse_after_rename' in _ms85
           and '_collapse_rename_pending' in _ms85,
       'a rename re-runs the collapse pass instead of waiting for the next '
       'playlist load, coalesced so Apply All does not run it per row')

# ---------------------------------------------------------------------------
# 86. Phone and FTP rows are playlist rows too. The saved name was read only
#     for http(s), so a phone row the user had already renamed was matched
#     again from its camera filename.
# ---------------------------------------------------------------------------
print()
print('--- 86: phone / FTP rows in the linker ---')

_have86 = all(hasattr(_msrc55, n) for n in
              ('_is_phone_or_ftp_path', '_best_match_raw_title'))
report(_have86, 'the phone/FTP predicate and the row-title lookup are here',
       'missing: ' + ', '.join(
           n for n in ('_is_phone_or_ftp_path', '_best_match_raw_title')
           if not hasattr(_msrc55, n)))

if _have86:
    _ph86 = ('phone:///storage/emulated/0/Download/'
             'PervMom.26.08.02.Alex.Harper.Use.My.Pussy.1080p.mp4')
    _ft86 = 'ftp://mou:pw@192.168.1.20:2121/Download/Some.Scene.720p.mp4'
    _loc86 = os.path.join(_tf55.gettempdir(), 'Local.File.1080p.mp4')

    report(_msrc55._is_phone_or_ftp_path(_ph86)
           and _msrc55._is_phone_or_ftp_path(_ft86)
           and not _msrc55._is_phone_or_ftp_path(_loc86)
           and not _msrc55._is_phone_or_ftp_path('https://bunkr.pk/f/x'),
       'phone:// and ftp:// are recognised, and a local file or a web URL is '
       'not mistaken for one')

    class _P86:
        _metadata_name_overrides = {}
        _stream_resolution_cache = {}
        _meta_norm_path = staticmethod(_msrc55._meta_norm_path)

        def _playlist_display_name(self, p):
            return ''

        def _get_name_override_key(self, k):
            return _msrc55._meta_norm_path(k)

    _p86 = _P86()
    report(_msrc55._best_match_raw_title(_p86, _ph86)
           == 'PervMom.26.08.02.Alex.Harper.Use.My.Pussy.1080p',
       'with no rename, a phone row still matches on its own filename -- that '
       'is usually the real scene name and is the best there is')

    _p86._metadata_name_overrides[_msrc55._meta_norm_path(_ph86)] = (
        'PervMom - Alex Harper - Use My Pussy and Keep Your Scolarship! '
        '- 13/09/2026')
    _p86._metadata_name_overrides[_msrc55._meta_norm_path(_ft86)] = (
        'BrattySis - Bianca Bangs - Stepsis Loves Sex Games - 29/11/2024')
    report(_msrc55._best_match_raw_title(_p86, _ph86).startswith('PervMom - Alex Harper'),
       'a phone row the user already renamed matches on the name they gave it, '
       'not on the camera filename the override lookup used to skip',
       repr(_msrc55._best_match_raw_title(_p86, _ph86)))
    report(_msrc55._best_match_raw_title(_p86, _ft86).startswith('BrattySis - Bianca Bangs'),
       'and the same holds for a plain ftp:// row',
       repr(_msrc55._best_match_raw_title(_p86, _ft86)))
    report(_msrc55._best_match_raw_title(_p86, _loc86) == 'Local.File.1080p',
       'a local file is untouched by any of it',
       repr(_msrc55._best_match_raw_title(_p86, _loc86)))

    _txt86 = open(_msrc55.__file__, encoding='utf-8').read()
    report('fetch_remote_page and is_remote' in _txt86,
       'and the page-title fetch stays web-only -- an FTP path has no page to '
       'fetch')
    report('_no_runtime' in _txt86 and 'phone/FTP row(s) have no known ' in _txt86,
       'when the phone cannot be probed the linker says so, instead of weak '
       'matches looking like a matcher that does not work')

# ---------------------------------------------------------------------------
# 87. A restored session must resume on the row that was playing. current_index
#     indexes the playlist as it was SAVED; rows can vanish on the way back in
#     (missing from disk, relinked, mirrors folded), which shifts every row
#     after them -- so the index then lands on a different file at that file's
#     own remembered position.
# ---------------------------------------------------------------------------
print()
print('--- 87: a restored session resumes where it left off ---')

_src87 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'main.py'), encoding='utf-8').read()
report('elif 0 <= current_index < len(self.playlist):' not in _src87,
       'the restore no longer reaches for the saved index the moment the '
       'exact path misses -- that line was what put a different file on '
       'screen whenever the playlist came back a different length')
report('def _session_resume_target' in _src87
       and 'saved index, playlist shape unchanged' in _src87,
       'the index is now only trusted when the playlist came back the same '
       'length it was saved at')

_cls87 = next((n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == 'VideoPlayer'), None)
_fn87 = {n.name: n for n in _cls87.body
         if isinstance(n, ast.FunctionDef) and n.name in (
             '_session_resume_target', '_mirror_path_key',
             '_mirrors_for_visible_url', '_unique_paths')} if _cls87 else {}
report(len(_fn87) == 4,
       'the resume-target choice and the mirror helpers it needs are here',
       'missing: ' + ', '.join(
           n for n in ('_session_resume_target', '_mirror_path_key',
                       '_mirrors_for_visible_url', '_unique_paths')
           if n not in _fn87))

if len(_fn87) == 4:
    _g87 = {'os': os, 're': re, 'urlparse': urlparse, 'unquote': unquote,
            '_meta_norm_path': _msrc55._meta_norm_path}
    _g87.update(_LIFT_HELPERS)
    for _n87 in _fn87.values():
        _m87 = ast.Module(body=[_n87], type_ignores=[])
        ast.fix_missing_locations(_m87)
        exec(compile(_m87, '<main.py>', 'exec'), _g87)

    class _Fake87:
        _REMOTE_FOLDER_PREFIX = 'remotefolder://'

        def __init__(self, mirrors, missing=()):
            self._playlist_url_mirrors = mirrors
            self._mirror_path_cache = {}
            self._missing = set(missing)

        def _is_jav_site_host(self, h):
            return False

    # _unique_paths too: _mirrors_for_visible_url calls it, and leaving it off
    # made that call raise, which the resume lookup swallows -- the test would
    # then pass for the wrong reason by falling through to the first row.
    for _n87 in ('_session_resume_target', '_mirror_path_key',
                 '_mirrors_for_visible_url', '_unique_paths'):
        setattr(_Fake87, _n87, _g87[_n87])
    # Bound after the real ones so the real _playlist_entry_available does not
    # shadow the stub and report every synthetic path as missing from disk.
    _Fake87._playlist_entry_available = lambda self, p: p not in self._missing

    _rows87 = ['/movies/a%d.mp4' % i for i in range(10)]
    _surv87 = _rows87[1]
    _mir87 = {_surv87: [_rows87[2], _rows87[3]]}
    # Rows 2 and 3 folded into row 1, so the playlist comes back 2 shorter and
    # every row after them moved up.
    _screen87 = [_rows87[0], _surv87] + _rows87[4:]

    def _old87(playlist, cf, ci):
        if cf and cf in playlist:
            return cf
        if 0 <= ci < len(playlist):
            return playlist[ci]
        return playlist[0] if playlist else None

    _t87, _w87 = _Fake87(_mir87)._session_resume_target(
        _screen87, _rows87[3], 3, len(_rows87))
    report(_old87(_screen87, _rows87[3], 3) != _rows87[3],
       'the old rule really does land on a different file once mirrors have '
       'folded -- that is the reported bug, not a theory',
       repr(_old87(_screen87, _rows87[3], 3)))
    report(_t87 == _surv87 and _w87 == 'surviving mirror row',
       'the row that was playing folded into another row as one of its '
       'mirrors, and the restore follows it there instead of jumping two '
       'places down the list', f'{_t87!r} ({_w87})')

    _t87b, _w87b = _Fake87(_mir87)._session_resume_target(
        list(_rows87), _rows87[6], 6, len(_rows87))
    report(_t87b == _rows87[6] and _w87b == 'exact path',
       'when the saved row is still there nothing else is consulted',
       f'{_t87b!r} ({_w87b})')

    _t87c, _w87c = _Fake87(_mir87)._session_resume_target(
        list(_rows87), '/movies/gone.mp4', 4, len(_rows87))
    report(_t87c == _rows87[4] and 'shape unchanged' in _w87c,
       'the saved index is still trusted when -- and only when -- the '
       'playlist came back the same length it was saved at',
       f'{_t87c!r} ({_w87c})')

    _short87 = [_rows87[0], _rows87[1], _rows87[5], _rows87[7]]
    _t87d, _w87d = _Fake87(_mir87, missing=(_rows87[0],))._session_resume_target(
        _short87, '/movies/gone.mp4', 6, len(_rows87))
    report(_t87d == _rows87[1] and _w87d == 'first available row',
       'with the shape changed the index is not trusted at all: the restore '
       'starts on the first row that exists, skipping one that is missing',
       f'{_t87d!r} ({_w87d})')

    _t87e, _w87e = _Fake87({})._session_resume_target([], '', 0, 0)
    report(_t87e is None, 'and an empty playlist resumes nothing')

# ---------------------------------------------------------------------------
# 88. A video with no captions at all played with a subtitle on screen, and the
#     subtitle was a sprite image URL. The page's preview-scrubber track is a
#     .vtt too, so every extension filter accepted it.
# ---------------------------------------------------------------------------
print()
print('--- 88: a thumbnail-scrubber VTT is not a subtitle ---')

_src88pre = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'main.py'), encoding='utf-8').read()
report("not in ('subtitles', 'captions')" in _src88pre,
       'the track extractor now reads the kind attribute -- it used to take '
       'any <track> with a src, so a preview scrubber declared as '
       'kind="thumbnails" became a caption track')
report('[SUBTITLE][REJECTED]' in _src88pre,
       'and the cue payload is checked where subtitle bytes are parsed, so a '
       'sprite VTT already written into subtitle_mappings.json cannot come '
       'back on the next load')

_cls88 = next((n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == 'VideoPlayer'), None)
_fn88 = {n.name: n for n in _cls88.body
         if isinstance(n, ast.FunctionDef) and n.name in (
             '_subtitle_content_is_captions', '_extract_subtitle_tracks_from_html',
             '_preferred_remote_subtitle_tracks', '_normalize_extracted_media_url')} if _cls88 else {}
report(len(_fn88) == 4, 'the caption check and the track extractor are here',
       'missing: ' + ', '.join(
           n for n in ('_subtitle_content_is_captions',
                       '_extract_subtitle_tracks_from_html',
                       '_preferred_remote_subtitle_tracks',
                       '_normalize_extracted_media_url') if n not in _fn88))

if len(_fn88) == 4:
    _g88 = {'re': re, 'os': os, 'urlparse': urlparse, 'urljoin': urljoin,
            'html_unescape': html_unescape}
    _g88.update(_LIFT_HELPERS)
    for _n88 in _fn88.values():
        _m88 = ast.Module(body=[_n88], type_ignores=[])
        ast.fix_missing_locations(_m88)
        exec(compile(_m88, '<main.py>', 'exec'), _g88)

    class _Fake88:
        _SUBTITLE_SPRITE_HINTS = ('#xywh',)
        _SUBTITLE_IMAGE_EXT = re.compile(
            r'\.(?:jpe?g|png|webp|gif|bmp|avif)(?:[?#]|\s|$)', re.IGNORECASE)
        _NON_CAPTION_CUE_CTX = re.compile(
            r'(?:thumbnails?|preview|sprite|storyboard|chapters?|scrubber)'
            r'["\']?\s*[:=]', re.IGNORECASE)

    for _n88 in _fn88:
        setattr(_Fake88, _n88, _g88[_n88])
    _k88 = _Fake88()

    # The reported file: a Video.js scrubber track. Cue payload is a sprite
    # image URL with an #xywh= fragment -- the URL the user saw on screen.
    _thumb88 = (
        'WEBVTT\n\n'
        '00:00:00.000 --> 00:00:05.000\n'
        'https://images.trendyporn.com/thumbs/6/a/3/8/e/'
        '6a38e4d7f2502.mp4/vtt_002.jpg#xywh=0,0,160,90\n\n'
        '00:00:05.000 --> 00:00:10.000\n'
        'https://images.trendyporn.com/thumbs/6/a/3/8/e/'
        '6a38e4d7f2502.mp4/vtt_002.jpg#xywh=160,0,160,90\n\n'
        '00:00:10.000 --> 00:00:15.000\n'
        'https://images.trendyporn.com/thumbs/6/a/3/8/e/'
        '6a38e4d7f2502.mp4/vtt_003.jpg#xywh=0,90,160,90\n')
    _realvtt88 = (
        'WEBVTT\n\n'
        '00:00:00.000 --> 00:00:04.000\nHey, how are you doing?\n\n'
        '00:00:04.000 --> 00:00:08.000\nFine. Did you see the site?\n\n'
        '00:00:08.000 --> 00:00:12.000\n'
        'Yeah, example.com was down all day.\n')
    _realsrt88 = (
        '1\n00:00:00,000 --> 00:00:04,000\nHey, how are you doing?\n\n'
        '2\n00:00:04,000 --> 00:00:08,000\nFine, thanks.\n')
    _speech88 = (
        'WEBVTT\nNOTE Transcript\n\n'
        '00:00:00.000 --> 00:00:05.000 align:middle position:50%\n'
        '<v Roger>Good evening.</v>\n')

    report(not _k88._subtitle_content_is_captions(_thumb88),
       'the reported file is recognised as a sprite sheet, so it is never '
       'shown as a subtitle -- the payload was an image URL, which is the '
       'only thing that distinguishes it from a real caption track')
    report(_k88._subtitle_content_is_captions(_realvtt88)
           and _k88._subtitle_content_is_captions(_realsrt88)
           and _k88._subtitle_content_is_captions(_speech88),
       'a real caption VTT, a real SRT and a transcript with cue settings '
       'and a NOTE all still load -- including one that mentions a website')
    report(not _k88._subtitle_content_is_captions('')
           and not _k88._subtitle_content_is_captions(
               'https://images.trendyporn.com/thumbs/x/vtt_002.jpg#xywh=0,0,160,90'),
       'and something that is not a subtitle file at all is not one')

    _src88 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'main.py'), encoding='utf-8').read()
    report('[SUBTITLE][REJECTED]' in _src88,
       'the check runs where subtitle bytes are parsed, not only where they '
       'are downloaded, so a sprite VTT already written into '
       'subtitle_mappings.json stops coming back')

    _page88a = (
        '<video><track kind="thumbnails" src="https://videos.trendyporn.com/'
        'videos/6/a/3/8/e/6a38e4d7f2502.thumbs.vtt" srclang="en" '
        'label="Thumbnails"><track kind="chapters" '
        'src="https://videos.trendyporn.com/chapters.vtt">'
        '<track kind="metadata" src="https://videos.trendyporn.com/meta.vtt">'
        '<track kind="captions" src="https://videos.trendyporn.com/'
        'en-captions.vtt" srclang="en" label="English">'
        '<track src="https://videos.trendyporn.com/default-sub.vtt" '
        'srclang="de" label="Deutsch"></video>')
    _page88b = (
        'var player = { file: "https://cdn.example.com/movie.mp4", '
        'thumbnails: { src: "https://cdn.example.com/sprites/thumbs.vtt" }, '
        'tracks: [{ file: "https://cdn.example.com/subs/english.vtt", '
        'label: "English" }] };')
    _page88c = ('<div>subs at https://cdn.example.com/subs/movie.en.vtt '
                'please</div>')
    _urls88 = [
        t['url'] for t in _k88._extract_subtitle_tracks_from_html(
            _page88a, 'https://www.trendyporn.com/video/x.html')]
    report('https://videos.trendyporn.com/en-captions.vtt' in _urls88
           and 'https://videos.trendyporn.com/default-sub.vtt' in _urls88
           and not any('thumbs' in u or 'chapters' in u or 'meta.vtt' in u
                       for u in _urls88),
       'kind="thumbnails", "chapters" and "metadata" are dropped at '
       'extraction, while kind="captions" and a <track> with no kind at all '
       '-- which HTML defaults to subtitles -- are kept', str(_urls88))
    report(_urls88 and 'https://videos.trendyporn.com/videos/6/a/3/8/e/'
           '6a38e4d7f2502.thumbs.vtt' not in _urls88,
       'and the catch-all bare-URL scan does not pick the rejected track '
       'straight back up out of the same tag')

    _urls88b = [t['url'] for t in _k88._extract_subtitle_tracks_from_html(
        _page88b, 'https://cdn.example.com/watch')]
    report(_urls88b == ['https://cdn.example.com/subs/english.vtt'],
       'a thumbnail VTT named only by a JS config key is dropped too',
       str(_urls88b))
    _urls88c = [t['url'] for t in _k88._extract_subtitle_tracks_from_html(
        _page88c, 'https://cdn.example.com/watch')]
    report(_urls88c == ['https://cdn.example.com/subs/movie.en.vtt'],
       'and a page with no player keywords anywhere still gives up its '
       'caption track -- nothing is rejected for mentioning nothing',
       str(_urls88c))

# ---------------------------------------------------------------------------
# 89. A row nobody had renamed was given a poster and a preview from a scene it
#     had nothing to do with, because the two shared a title. From problem.txt.
# ---------------------------------------------------------------------------
print()
print('--- 89: a shared scene title is not a match ---')

_q89 = ('MomComes First.26.06.07. Brianna.Beach.Breaking.The.Rules.XXX.1080p')


def _db89(movies):
    d = _msrc55.MetadataDB.__new__(_msrc55.MetadataDB)
    d._data = {"movies": movies}
    d.db_path = ''
    return d


def _rec89(slug, title, series, models, date, site):
    return {slug: {'slug': slug, 'title': title, 'series': series,
                   'models': models, 'date': date, 'source_site': site,
                   'image': f'https://x/{slug}.jpg',
                   'preview': f'https://x/{slug}.mp4', 'duration': 0,
                   'video_id': '', 'meta_fetched': True}}


_hijab89 = _rec89('hijab-hookup-izzy-lush-breaking-the-rules',
                  'Breaking the Rules', 'Hijab Hookup', ['Izzy Lush'],
                  '22/08/2021', 'HijabHookup')
_mcf89 = _rec89('momcomesfirst-brianna-beach-breaking-the-rules',
                'Breaking The Rules', 'MomComesFirst', ['Brianna Beach'],
                '07/06/2026', 'MomComesFirst')

_m89 = _msrc55.TitleMatcher(_db89(dict(_hijab89)))
_c89 = _m89.match_candidates(_q89, limit=5, include_weak=True)
report(_c89 and _c89[0]['signals'] == ['scene'],
       'the reported row matches the unrelated scene on its title and nothing '
       'else -- the file is a MomComesFirst release and that record is in '
       'neither shipped database, so there was no site, performer, series or '
       'date to agree with', str(_c89[0]['signals']) if _c89 else 'no candidate')
report(_m89.match(_q89) is None,
       'so the preview path shows no poster at all instead of another '
       "network's scene: match() only returns high-confidence candidates, and "
       'a lone title is no longer one',
       repr((_m89.match(_q89) or {}).get('series')))
report(_c89 and _c89[0]['confidence'] == 'possible',
       'it is still offered in the linker as a possible match for the user to '
       'confirm -- dropped, not hidden')

_both89 = dict(_hijab89)
_both89.update(_mcf89)
_m289 = _msrc55.TitleMatcher(_db89(_both89))
_hit89 = _m289.match(_q89)
report(_hit89 is not None and _hit89['series'] == 'MomComesFirst',
       'and when the right record does exist it wins outright, with the '
       'release date now counted as agreement',
       (_hit89 or {}).get('series'))
_sig89 = next(c['signals'] for c in _m289.match_candidates(
    _q89, limit=5, include_weak=True)
    if c['movie']['series'] == 'MomComesFirst')
report('date' in _sig89,
       'the date 26.06.07 in the filename is read as a release date -- the old '
       'pattern wanted a four-digit year and a dash or a slash, so the date '
       'carried by nearly every scene name was invisible and nothing could '
       'contradict a title-only match', str(_sig89))

_qd89 = getattr(_msrc55, '_query_date_keys', None)
report(_qd89 is not None, 'the filename date reader exists')
if _qd89 is None:
    _qd89 = lambda s: set()
report(sorted(_qd89('26.06.07')) == ['20260607']
       and sorted(_qd89('Breaking.the.Rules.22.08.2021')) == ['20210822']
       and sorted(_qd89('2021-08-22')) == ['20210822']
       and sorted(_qd89('22/08/2021')) == ['20210822'],
       'YY.MM.DD, DD.MM.YYYY, ISO and DD/MM/YYYY are all read as dates')
report(not _qd89('10.12.45')
       and not _qd89('99.99.99')
       and not _qd89('1080p')
       and not _qd89('x264'),
       'and parts that cannot be a month and a day are not dates, so a '
       'duration or a codec tag never becomes one')

for _lbl89, _q289 in (
        ('site', 'HijabHookup.21.08.22.Breaking.The.Rules.XXX.1080p'),
        ('performer', 'Izzy Lush - Breaking the Rules.mp4'),
        ('date', 'Breaking.the.Rules.22.08.2021.mp4')):
    report(_msrc55.TitleMatcher(_db89(dict(_hijab89))).match(_q289) is not None,
           f'a filename that also carries the {_lbl89} still matches '
           'outright -- the title is only ever refused when it is all there '
           'is', _q289)
report(_msrc55.TitleMatcher(_db89(dict(_hijab89))).match(
           'Breaking the Rules.mp4') is None,
       'a bare title with nothing else is left for the user to pick')
report('the scene title alone' in _msrc55.signal_breakdown(['scene']),
       'and the card explains why it was held back rather than showing a total '
       'that does not match the ranking',
       _msrc55.signal_breakdown(['scene']))

# ---------------------------------------------------------------------------
# 90. A phone file whose name contains '#' played from a local disk but not
#     over FTP. The playback proxy unescapes the path for ffmpeg and then
#     urlsplit reads the '#' as a fragment, so it RETR'd the directory.
# ---------------------------------------------------------------------------
print()
print("--- 90: a '#' in a phone filename is not a URL fragment ---")

_src90pre = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'main.py'), encoding='utf-8').read()
report('remote_path = _ftp_remote_path_from_url(ftp_url)' in _src90pre,
       'the proxy serves the path the URL actually refers to -- it used to take '
       'parsed.path, which for a filename containing a hash is the folder')
report("quote(remote.lstrip('/'), safe='/')" in _src90pre,
       'and the thumbnail proxy escapes the URL it builds, which it imported '
       'quote for and then never did')

_tree90 = ast.parse(_src90pre)
_fn90 = {}
for _n90 in _tree90.body:
    if isinstance(_n90, ast.FunctionDef) and _n90.name in (
            '_ftp_url_for_native_client', '_ftp_remote_path_from_url'):
        _fn90[_n90.name] = _n90
report(len(_fn90) == 2,
       'the FTP path reader exists alongside the native-client URL builder',
       'missing: ' + ', '.join(
           n for n in ('_ftp_url_for_native_client', '_ftp_remote_path_from_url')
           if n not in _fn90))

if len(_fn90) == 2:
    from urllib.parse import (urlparse as _up90, urlunparse as _uu90,
                              urlsplit as _us90, unquote as _uq90,
                              quote as _q90)
    _g90 = {'urlparse': _up90, 'urlunparse': _uu90, 'urlsplit': _us90,
            'unquote': _uq90}
    _g90.update(_LIFT_HELPERS)
    for _n90 in _fn90.values():
        _m90 = ast.Module(body=[_n90], type_ignores=[])
        ast.fix_missing_locations(_m90)
        exec(compile(_m90, '<main.py>', 'exec'), _g90)
    _native90 = _g90['_ftp_url_for_native_client']
    _rp90 = _g90['_ftp_remote_path_from_url']

    # The file from the report, and the awkward neighbours around it.
    _cases90 = [
        'Anime/Prologue/#11-(16)Prologue 1 (LUCIA).mp4',
        'Movies/Some Scene 1080p.mp4',
        'Clips/what? ever (2).mp4',
        'Clips/100% real.mp4',
        '\u0412\u0438\u0434\u0435\u043e/\u00e9pisode #3.mp4',
        'a/b #c ?d.mp4',
        'simple.mp4',
    ]
    _bad90 = []
    for _remote90 in _cases90:
        _quoted90 = ('ftp://u:pw@192.168.1.20:2121/'
                     + _q90(_remote90, safe='/'))
        # exactly the chain the playback proxy runs: escape for the URL, then
        # unescape for ffmpeg, then read the path back out.
        _got90 = _rp90(_native90(_quoted90))
        if _got90 != '/' + _remote90:
            _bad90.append((_remote90, _got90))
    report(not _bad90,
       'a phone path survives the escape, the unescape-for-ffmpeg and the read '
       'back out -- including the reported file, whose name used to land in '
       "the URL's fragment so the proxy asked the phone for a folder",
       str(_bad90))
    report(_us90(_native90('ftp://u:pw@h:21/' + _q90(
        'Anime/Prologue/#11-(16)Prologue 1 (LUCIA).mp4', safe='/'))).path
       == '/Anime/Prologue/',
       'and plain urlsplit really does truncate it -- that is the bug being '
       'fixed here, not a hypothetical')
    _mismatch90 = [r for r in _cases90
                   if _rp90('ftp://u:pw@h:21/' + _q90(r, safe='/'))
                   != _rp90(_native90('ftp://u:pw@h:21/' + _q90(r, safe='/')))]
    report(not _mismatch90,
       'an escaped URL and its unescaped native form give the same path, so '
       'the SIZE probe and the RETR agree about which file they mean',
       str(_mismatch90))

    _src90 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'main.py'), encoding='utf-8').read()
    report(_src90.count('_ftp_remote_path_from_url(') >= 5,
       'the path is read through it at every site that used to reach into '
       'parsed.path -- the playback proxy on both ends, the SIZE probe that '
       'decides whether to drop a file as dead, and FTP subtitle loading',
       f"{_src90.count('_ftp_remote_path_from_url(')} call sites")
    report("quote(remote.lstrip('/'), safe='/')" in _src90,
       'and the thumbnail proxy escapes the path it builds a URL from -- it '
       'imported quote and never used it, so a hash dropped the filename '
       'before the proxy was even reached')

# ---------------------------------------------------------------------------
# 91. supjav.com -- an aggregator whose watch page carries no stream, only
#     server buttons whose data-link is an encrypted token.
# ---------------------------------------------------------------------------
print()
print('--- 91: supjav server tokens ---')

_gj91 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     'generic_jav_grab.py')
_src91pre = open(_gj91, encoding='utf-8').read()
report(re.search(r'_browser_fetch\(url[,)]', _src91pre) is not None,
       'the grab retries a gated page through a real browser -- a plain HTTP '
       'client gets 403 from supjav and that 403 used to be the verdict')
report('visible=_looks_blocked(status)' in _src91pre
       and 'the page target went away mid-wait' in _src91pre,
       'a gated page gets its window on screen from launch and a tab lost '
       'mid-wait does not abort the capture -- in the field the challenge '
       'reload destroyed the target and wait_for_timeout threw the whole '
       'fetch away after the user had already clicked through')
report('_GATE_WAIT_MS' in _src91pre and '_raise_window' in _src91pre,
       'and it waits for a challenge to clear with the window on screen. In '
       'the field the window opened off-screen, the captcha sat there '
       'unsolvable, and it closed again -- the server list came back empty and '
       'the failure was reported as No stream URL captured')
_tree91 = ast.parse(open(_gj91, encoding='utf-8').read())
_fn91 = {}
for _n91 in _tree91.body:
    if isinstance(_n91, ast.FunctionDef) and _n91.name in (
            'supjav_player_template', 'supjav_server_links',
            'supjav_destination', '_looks_blocked', '_page_is_gated',
            '_live_page'):
        _fn91[_n91.name] = _n91
_want91 = ('supjav_player_template', 'supjav_server_links',
           'supjav_destination', '_looks_blocked', '_page_is_gated',
           '_live_page')
report(len(_fn91) == len(_want91),
       'the server-list reader, the redirect reader and the bot-gate test are '
       'in the generic grabber, not in a scraper of their own -- supjav hands '
       'the URL to hosters the app already resolves',
       'missing: ' + ', '.join(n for n in _want91 if n not in _fn91))

_src91g = open(_gj91, encoding='utf-8').read()
report("'supjav' in host" in _src91g,
       'and the grab makes the supjav.php hop for a supjav page before it '
       'scans for streams -- the watch page itself contains none')
_gi91 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'generic_jav_integration.py'), encoding='utf-8').read()
report("'supjav.com'," in _gi91,
       'the URL gate claims supjav watch pages, so pasting one runs the '
       'generic grabber')
report('_SUPJAV_OWN_HOSTS' in _src91g,
       'a hop that lands back on supjav or its ad network is not treated as '
       'an answer')

if len(_fn91) == len(_want91):
    from urllib.parse import (urlparse as _up91, urlunparse as _uu91,
                              parse_qsl as _pq91, urlencode as _ue91)
    from html import unescape as _uq91
    # No 'sys' here: only _supjav_hoster_urls prints to stderr, and it is the
    # one function this section does not compile out (it does the fetching).
    _g91 = {'re': re, 'unescape': _uq91, 'urlparse': _up91,
            'urlunparse': _uu91, 'parse_qsl': _pq91, 'urlencode': _ue91}
    _g91.update(_LIFT_HELPERS)
    for _n91 in _tree91.body:
        if (isinstance(_n91, ast.Assign) and _n91.targets
                and getattr(_n91.targets[0], 'id', '').startswith('_SUPJAV')):
            _m91 = ast.Module(body=[_n91], type_ignores=[])
            ast.fix_missing_locations(_m91)
            exec(compile(_m91, '<g>', 'exec'), _g91)
    for _name91 in ('_looks_blocked', '_page_is_gated', '_live_page',
                    'supjav_player_template', 'supjav_server_links',
                    'supjav_destination'):
        if _name91 not in _fn91:
            continue
        _m91 = ast.Module(body=[_fn91[_name91]], type_ignores=[])
        ast.fix_missing_locations(_m91)
        exec(compile(_m91, '<g>', 'exec'), _g91)

    class _Tab91:
        def __init__(self, closed):
            self._c = closed

        def is_closed(self):
            return self._c

    class _Ctx91:
        def __init__(self, pages):
            self._p = pages

        @property
        def pages(self):
            return self._p

    _lp91 = _g91.get('_live_page')
    if _lp91 is not None:
        report(_lp91(_Ctx91([_Tab91(False)])) is not None
               and _lp91(_Ctx91([_Tab91(True), _Tab91(False)])) is not None
               and _lp91(_Ctx91([_Tab91(True)])) is None
               and _lp91(_Ctx91([])) is None
               and _lp91(object()) is None,
           'a tab lost mid-wait is survived by picking up whichever one the '
           'context still has. In the field the challenge reload took the '
           'target with it and the whole fetch died on wait_for_timeout, '
           'losing a capture the user had already clicked through')
    report('visible=_looks_blocked(status)' in _src91g,
       'a page that already answered 403 gets its window on screen from '
       'launch, not raised afterwards -- the first load is the one being '
       'fingerprinted, and a window parked at -32000 is part of what it sees')
    report('the page target went away mid-wait' in _src91g,
       'and losing the page does not lose the capture')
    report('could not read the page at the end' in _src91g,
       'the final read is guarded too, so a target that dies on the last '
       'moment does not throw the whole grab away')

    _g91.setdefault('_page_is_gated', None)
    _pg91 = _g91.get('_page_is_gated')

    class _Page91:
        def __init__(self, title, body):
            self._t, self._b = title, body

        def content(self):
            return self._b

        def title(self):
            return self._t

    _cf91 = ('<html><head><title>Just a moment...</title></head><body>'
             '<div id="challenge-form"><div class="cf-turnstile"></div>'
             'Verifying you are human</div></body></html>')
    if _pg91 is not None:
        report(_pg91(_Page91('Just a moment...', _cf91)) is True
               and _pg91(_Page91('', '')) is False,
           'a Cloudflare interstitial in the browser is recognised as one, so '
           'the grab waits instead of reading it as the page')
        # _cap91 is bound further down; this block runs before it.
        _cap91e = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'supjav.txt')
        if os.path.isfile(_cap91e):
            _real91 = open(_cap91e, encoding='utf-8', errors='replace').read()
            report(_pg91(_Page91('DVDES-886 ...', _real91)) is False,
               'and the real watch page is not mistaken for a challenge -- '
               'waiting on a page that already loaded would stall every '
               'capture for two minutes')
    report('_GATE_WAIT_MS' in _src91g and '_raise_window' in _src91g,
       'when a challenge is up the window comes on screen and stays there for '
       'up to two minutes. In the field it opened off-screen, the captcha sat '
       'there unsolvable, and the window closed -- which is what turned a '
       'solvable page into No stream URL captured')
    report("result['error'] = ('bot challenge not cleared" in _src91g
           or 'bot challenge not cleared' in _src91g,
       'and a page that never cleared says so, rather than reporting no '
       'stream and sending the hunt downstream for a page that was never read')
    report('OSD_CALLBACK' in _src91g
           and 'mod.OSD_CALLBACK' in open(
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'generic_jav_integration.py'),
               encoding='utf-8').read(),
       'the app is told on screen that a window needs a click, through the '
       'thread-safe OSD slot rather than a widget touched from the worker')
    report('use_browser' not in _src91g,
       'a browser is opened for a supjav.php hop only when that hop is itself '
       'blocked -- it goes to the player host, not to supjav, and forcing one '
       'per server would mean four launches for servers that answer plain HTTP')

    _gj91 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'generic_jav_grab.py'), encoding='utf-8').read()
    _gi91b = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'generic_jav_integration.py'), encoding='utf-8').read()

    # supjav's check has looped in three field reports: the page reloads the
    # challenge no matter what is clicked. Waiting out the full 120s gate poll
    # in a browser that cannot pass only delays the fallback that can.
    _hostile91 = None
    for _n91 in ast.parse(_gj91).body:
        if (isinstance(_n91, ast.Assign) and isinstance(_n91.targets[0], ast.Name)
                and _n91.targets[0].id == '_BROWSER_HOSTILE_HOSTS'):
            _hostile91 = ast.literal_eval(_n91.value)
    report(_hostile91 and 'supjav' in _hostile91,
       'a site whose bot check a driven browser provably cannot clear is '
       'reported as gated at once instead of being waited on. The log showed '
       '31 "waiting up to 120s" lines: the Brave fallback sat BEHIND that '
       'wait, so the browser that works did not even start for two minutes')
    _g91f = next(n for n in ast.parse(_gj91).body
                 if isinstance(n, ast.FunctionDef) and n.name == 'grab_all')
    _h91 = [n.lineno for n in ast.walk(_g91f)
            if isinstance(n, ast.Name) and n.id == '_BROWSER_HOSTILE_HOSTS']
    _b91 = [n.lineno for n in ast.walk(_g91f)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == '_browser_fetch']
    report(bool(_h91) and bool(_b91) and min(_h91) < min(_b91),
       'and that check runs before the browser is launched, not after')

    # The coupling that was silently broken: the gate error string is what the
    # worker matches to decide whether to fall back to the user's Brave. Reword
    # the message and the fallback stops firing, with nothing to complain.
    _keys91 = re.search(
        r"_gated = bool\(grab_error\) and any\(\s*\n"
        r"\s*k in str\(grab_error\)\.lower\(\)\s*\n"
        r"\s*for k in \((.*?)\)\)", _gi91b, re.S)
    _keys91 = ([k.strip().strip('\'"') for k in _keys91.group(1).split(',')]
               if _keys91 else [])
    _errs91 = []
    for _n91 in ast.walk(ast.parse(_gj91)):
        if (isinstance(_n91, ast.Assign) and len(_n91.targets) == 1
                and isinstance(_n91.targets[0], ast.Subscript)
                and isinstance(_n91.targets[0].slice, ast.Constant)
                and _n91.targets[0].slice.value == 'error'):
            try:
                _errs91.append(ast.literal_eval(_n91.value))
            except Exception:
                pass
    _gatey91 = [e for e in _errs91
                if e.startswith('bot check') or e.startswith('bot challenge')]
    report(bool(_keys91) and len(_gatey91) >= 2
           and all(any(k in e.lower() for k in _keys91) for e in _gatey91),
       'every gate error the grabber can raise contains a word the worker '
       'triggers on. The first wording of the new message said "cannot '
       'clear" -- the fallback would then have fired only by way of the '
       'supjav hostname check, and silently stopped the day that changed')

    report('self._main_brave_cache_capture(src_url, title)' in _gi91b,
       'when the app\u2019s own browser cannot clear the check, the capture '
       'falls back to the user\u2019s Brave. _main_brave_cache_capture was '
       'installed on VideoPlayer and called by nothing -- the one channel '
       'that uses a real profile, which is the thing a bot check is actually '
       'judging, sat unused')
    report("'supjav' in src_url or _gated" in _gi91b,
       'and the fallback fires for a supjav row or any grab that failed on a '
       'gate, not for every failed grab')
    report("'streams': dedup" in _gi91b,
       'the Brave capture hands back the same shape the worker already reads, '
       'so the fallback needs no translation')

    # The capture kept only .m3u8, so streamtape -- the server the user was
    # watching -- was in the network log and never made it into the row.
    for _n91 in ('_looks_like_m3u8', '_looks_like_direct_media',
                 '_looks_like_stream', '_is_ad_m3u8', '_AD_M3U8_MARKERS',
                 '_DIRECT_MEDIA_EXT_RE', '_DIRECT_MEDIA_PATH_MARKERS',
                 '_host_resolves', '_HOST_RESOLVES'):
        for _d91 in ast.parse(_gi91b).body:
            if (getattr(_d91, 'name', None) == _n91
                    or (isinstance(_d91, ast.Assign)
                        and isinstance(_d91.targets[0], ast.Name)
                        and _d91.targets[0].id == _n91)):
                exec(compile(ast.Module([_d91], []), '<gi91>', 'exec'), _g91)
                break
    # Degrade, do not raise: a build without these functions must report
    # failures, not abort the run half way through.
    _dm91 = _g91.get('_looks_like_direct_media') or (lambda u: False)
    _st91 = _g91.get('_looks_like_stream') or (lambda u: False)
    for _u91 in ['https://streamtape.com/get_video?id=AbCd&expires=1&token=x&stream=1',
                 'https://tapecontent.net/d/xyz/movie.mp4',
                 'https://cdn.example.com/v/abc/720p.mp4?token=q',
                 'https://host.com/file/movie.mkv',
                 'https://host.com/a.webm']:
        report(_dm91(_u91) and _st91(_u91),
           f'a directly playable file is captured, not just an m3u8: {_u91[:52]}')
    for _u91 in ['https://cdn3.turboviplay.com/seg/00001.ts',
                 'https://host.com/movie.m3u8.ts',
                 'https://img.supjav.com/images/2022-02-1dvdes886pl.jpg',
                 'https://supjav.com/137606.html',
                 'https://streamtape.com/e/AbCdEf',
                 'https://host.com/x.flac']:
        report(not _dm91(_u91),
           f'and capture noise is not: {_u91[:52]}. HLS segment requests '
           'alone would add hundreds of rows per stream')
    report('if not _looks_like_stream(u) or _is_ad_m3u8(u)' in _gi91b
           and 'if not _looks_like_m3u8(u) or _is_ad_m3u8(u)' not in _gi91b,
       'and the capture\u2019s collector actually uses the wider test -- '
       'adding a predicate nobody calls would change nothing')

    # Three of the four captured hosts no longer resolve at all, so the row
    # offered four mirrors and played none.
    report(re.search(r"if _host_resolves\(urlparse\(page_url\)\.hostname", _gi91b)
           is not None,
       'a link on a host that does not resolve is dropped -- but only after '
       'the page the user just loaded is checked as a control, so a broken '
       'resolver drops a whole capture rather than a dead CDN')
    report('def _probe_m3u8_durations(streams, page_url, dead=None)' in _gi91b
           and 'dead.append(u)' in _gi91b,
       'and a master playlist whose variants live on a dead host is dropped '
       'too: turboviplay answered 200 and mpv still reported "no audio or '
       'video data played", because every variant was on a host that does '
       'not resolve')

    # The captcha click, copied from the Play clicker's method.
    def _const91(name):
        for _n91 in ast.parse(_gi91b).body:
            if (isinstance(_n91, ast.Assign)
                    and getattr(_n91.targets[0], 'id', '') == name):
                return ast.literal_eval(_n91.value)
        return None
    _cap91 = _const91('_AUTOCLICK_CAPTCHA_PS1') or ''
    _play91 = _const91('_AUTOCLICK_PS1') or ''
    report(bool(_cap91),
       'a bot-challenge clicker exists, using the same OS-level mechanism as '
       'the Play clicker: UI Automation to find the widget, user32 for a real '
       'mouse click. CDP cannot do this -- Chromium 136+ ignores the debug '
       'port on the default profile, which is why the capture uses the real '
       'browser at all')
    report(bool(_cap91) and bool(_play91)
           and _cap91.split('# ---- bot-challenge click')[0].strip()
           == _play91.split('# Strategy 1:')[0].strip(),
       'and it reuses that clicker\u2019s harness verbatim rather than a '
       'second copy that can drift -- same window search, same foreground '
       'dance')
    report(bool(_cap91) and not [c for c in _cap91 if ord(c) > 127]
           and _cap91.count('{') == _cap91.count('}')
           and _cap91.count('(') == _cap91.count(')'),
       'the script stays pure ASCII and balanced: it is written with '
       'encoding=\'ascii\', so one smart quote or em dash would be '
       'silently replaced and break the parse on the user\u2019s machine')
    report('$titleGated = ($wname -match $CHAL)' in _cap91
           and 'if ($fg -and $titleGated)' in _cap91,
       'and it only clicks when a challenge is actually on screen -- a '
       'clicker that fires unconditionally would click whatever is under the '
       'cursor on a page that never had a gate')
    report('function Human-Click' in _cap91
           and 'SetCursorPos' in _cap91.split('function Human-Click')[1]
           .split('mouse_event')[0],
       'the pointer walks to the checkbox instead of teleporting onto it: '
       'Turnstile scores the pointer, and a click with no movement before it '
       'is the signature it looks for')
    _drv91 = [n for n in ast.walk(ast.parse(_gi91b))
              if isinstance(n, ast.FunctionDef)
              and n.name == '_autoclick_captcha_in_main_brave']
    _drvsrc91 = (ast.get_source_segment(_gi91b, _drv91[0])
                 if _drv91 else '')
    report('_AUTOCLICK_CAPTCHA_PS1' in _drvsrc91
           and '_AUTOCLICK_PS1' not in _drvsrc91.replace(
                   '_AUTOCLICK_CAPTCHA_PS1', ''),
       'the driver writes the challenge script, not the Play one -- a '
       'copy-paste here would click Play on a page that has no player')
    report("_autoclick_captcha_in_main_brave('')" in _gi91b,
       'and it is called with an EMPTY keyword. The challenge page\u2019s '
       'title is "Just a moment...", never the video\u2019s, so a '
       'title-keyed window search returns NOWINDOW at exactly the moment the '
       'click is needed -- which is the auto-click: NOWINDOW in the field log')
    report('if (-not $fallback) { $fallback = $w }' in _gi91b,
       'the window search now also falls back to the first Brave window when '
       'the keyword misses, instead of giving up')
    _ct91 = [n for n in ast.walk(ast.parse(_gi91b))
             if isinstance(n, ast.FunctionDef) and n.name == '_captcha_tick']
    report(bool(_ct91) and _gi91b.count('_captcha_tick(start)') == 2,
       'the tick runs on both capture channels -- the network log and the '
       'disk-cache fallback -- and stops once a stream is captured, so it '
       'cannot click the player after the gate is behind them')

    _lb91 = _g91.get('_looks_blocked')
    report(_lb91 is not None, 'the bot-gate test is in the grabber')
    if _lb91 is not None:
        _gated91 = [(s, b) for s, b in
                    ((403, ''), (429, ''), (503, ''),
                     (200, '<title>Just a moment...</title>'),
                     (200, '<div id="cf-browser-verification"></div>'))
                    if not _lb91(s, b)]
        report(not _gated91,
           'a 403, 429 or 503 and a Cloudflare interstitial are all read as a '
           'gate -- supjav answers 403 to a plain client and serves the page '
           'to a real browser, so the HTTP answer cannot be the verdict',
           str(_gated91))
        _notgated91 = [(s, b) for s, b in
                       ((200, '<html><video src=x></video></html>'),
                        (404, ''), (400, ''))
                       if _lb91(s, b)]
        report(not _notgated91,
           'and a 404 is a real answer, not a gate -- launching a browser for '
           'a dead link would only make it slow', str(_notgated91))
    report('headless=False' in _src91g and "--window-position=-32000,-32000" in _src91g
           and 'launch_persistent_context' in _src91g,
       'the browser fallback is the same policy javdock uses: the installed '
       'browser headed, because headless is reliably defeated, off-screen so '
       'it never appears, with a profile that keeps the clearance cookie')
    report('status == 0 or _looks_blocked(status)' in _src91g,
       'the retry happens for a gate or a failed connection, not for every '
       'non-200')

    _req91 = 'https://lk1.supremejav.com/supjav.php?l=abc&bg=undefined'
    _dest91 = _g91['supjav_destination']
    _shapes91 = [
        ('a real redirect',      200, 'https://sextb.net/embed/xyz123', ''),
        ('a meta refresh',       200, _req91,
         '<meta http-equiv="refresh" content="0;url=https://jav.guru/video/4411/">'),
        ('an iframe',            200, _req91,
         '<html><iframe src="https://roshy.tv/embed/v/9f2a"></iframe></html>'),
        ('a location assignment', 200, _req91,
         "<script>location.href = 'https://javgg.net/javid/88123.html';</script>"),
        ('a protocol-relative src', 200, _req91,
         '<iframe src="//roshy.tv/embed/v/77"></iframe>'),
        ('a bare URL in the body', 200, _req91,
         'redirecting to https://sextb.net/embed/abc ...'),
    ]
    _bad91 = [(l, _dest91(_req91, s, f, b)) for l, s, f, b in _shapes91
              if not _dest91(_req91, s, f, b)]
    report(not _bad91,
       'every ordinary shape a redirect page answers in yields the hoster -- '
       'there is no way to know from here which one supjav.php will use, so '
       'the reader accepts all of them', str(_bad91))
    _none91 = [(l, _dest91(_req91, s, f, b)) for l, s, f, b in [
        ('nothing but a block page', 200, _req91,
         '<html><body>blocked</body></html>'),
        ('a hop back to supjav', 200, _req91,
         '<meta http-equiv="refresh" content="0;url=https://lk2.supremejav.com/x">'),
        ('only an ad network', 200, _req91,
         '<iframe src="https://go.mayzaent.com/smartpop/abc"></iframe>'),
    ] if _dest91(_req91, s, f, b)]
    report(not _none91,
       'and a page that never left supjav, or only reached its ad network, '
       'is reported as no answer rather than as a stream', str(_none91))

# The capture is a user-uploaded artifact, so its absence is a skip.
_cap91 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'supjav.txt')
if not os.path.isfile(_cap91):
    print('  SKIP  supjav.txt is not in the working copy, so the server list '
          'is not read out of a real watch page')
elif len(_fn91) == len(_want91):
    _html91 = open(_cap91, encoding='utf-8', errors='replace').read()
    _links91 = _g91['supjav_server_links'](_html91)
    report(len(_links91) == 4 and [l for l, _ in _links91][0] == 'ST',
       'the real DVDES-886 page yields its four servers with the active one '
       'first, so a dead server is tried after the one supjav itself chose',
       str([l for l, _ in _links91]))
    report(_links91 and _links91[0][1] == _g91['supjav_player_template'](_html91),
       'and the URL built for the active server is byte-for-byte the iframe '
       'the page already had -- the token substitution is right, not merely '
       'plausible', (_links91[0][1] if _links91 else '')[:80])
    report(_links91 and all(
        _g91['supjav_server_links'](_html91)[i][1]
        != _g91['supjav_server_links'](_html91)[j][1]
        for i in range(len(_links91)) for j in range(len(_links91)) if i != j),
       'each server gets its own player URL, not four copies of one')
    report('supremejav.com/supjav.php?l=' in (_links91[0][1] if _links91 else ''),
       'the player host is read off the page rather than hard-coded -- the '
       'lk1 prefix is exactly the sort of thing that rotates',
       (_links91[0][1] if _links91 else '')[:60])

# ── 92. A URL's query string is not its filename ──────────────────────────────
# The doodstream row 'https://playmogo.com/e/<id>?c_poster=...cover-player.jpg'
# ends in .jpg, so every bare endswith(IMAGE_EXTENSIONS) test called that VIDEO
# an image. One wrong boolean, six symptoms -- the reported one being that the
# arrow keys walked the playlist instead of seeking.
from urllib.parse import urlsplit as _us92

_fn92 = next((n for n in TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == '_ext_probe_path'),
             None)
report(_fn92 is not None,
   'a file-extension test has somewhere to go that is not the whole URL',
   'defined' if _fn92 else 'MISSING')
if _fn92 is not None:
    _g92 = {'urlsplit': _us92}
    _g92.update(_LIFT_HELPERS)
    exec(compile(ast.Module([_fn92], []), '<m92>', 'exec'), _g92)
    _e92 = _g92['_ext_probe_path']
    _img92 = ast.literal_eval(next(
        n.value for n in TREE.body
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == 'IMAGE_EXTENSIONS'))

    def _isimg92(p):
        return _e92(p).lower().endswith(_img92)

    _DOOD92 = ('https://playmogo.com/e/5al8s55quahf?c_poster='
               'https://cdn001.imggle.net/cover-player.jpg')
    report(_isimg92(_DOOD92) is False and _DOOD92.lower().endswith(_img92),
       'the doodstream row from the field log is a VIDEO. It ends in .jpg only '
       'because of a c_poster query parameter, and the old bare endswith made '
       'is_image true -- which is why Left/Right called previous_video() and '
       'showed "No previous valid files in playlist" instead of seeking 3 s')
    for _p92, _w92, _lbl92 in [
            ('https://861113552.tapecontent.net/radosgw/a4A6/dvdes-886.mp4'
             '?stream=1', False, 'streamtape direct mp4 with a query'),
            ('https://il266m.cloudatacdn.com/u5kj/xwcv?token=a&expiry=1',
             False, 'the real dood playback URL'),
            ('https://cdn3.turboviplay.com/data/X/X.m3u8', False, 'an HLS stream'),
            ('https://img.supjav.com/images/2022-02-1dvdes886pl.jpg',
             True, 'a genuinely remote image'),
            (r'C:\Users\Mouad\Pics\photo.png', True, 'a local image'),
    ]:
        report(_isimg92(_p92) is _w92, f'classified correctly: {_lbl92}')
    # The regressions that matter: '?' and '#' are legal filename characters
    # locally and appear on FTP shares, so those must NOT be split.
    for _p92 in [r'C:\Users\Mouad\Pics\what?big.jpg',
                 r'C:\Users\Mouad\Pics\#12 cover.jpg']:
        report(_isimg92(_p92) is True,
           'a local filename keeps its literal ?/# rather than being split '
           'like a URL', _p92[-24:])
    report(_e92('phone:///Anime/Prologue/#11-(16)Prologue 1 (LUCIA).mp4')
           .endswith('(LUCIA).mp4'),
       'and a phone:// path is left whole, the same rule the FTP fragment fix '
       'depends on')

_SITES92 = {
    '53491 arrow keys':
        'is_image = _ext_probe_path(self.current_file).lower()'
        '.endswith(IMAGE_EXTENSIONS)',
    '2270 side-click':
        'is_single_image = (_ext_probe_path(current).lower()'
        '.endswith(IMAGE_EXTENSIONS)',
    '22329 playlist classifier':
        "_ext_probe_path(lower_path).endswith(IMAGE_EXTENSIONS)",
    '51688 stall watchdog':
        '_ext_probe_path(current).lower().endswith(IMAGE_EXTENSIONS)',
    '51708 near-end auto-advance':
        '_ext_probe_path(current_file).lower().endswith(IMAGE_EXTENSIONS)',
    '51814 auto-advance':
        '_ext_probe_path(self.current_file).lower()'
        '.endswith(IMAGE_EXTENSIONS)',
}
for _lbl92, _snip92 in _SITES92.items():
    report(_snip92 in SRC,
       f'the same wrong boolean is fixed at {_lbl92} too -- one cause, six '
       'symptoms; fixing only the key handler would leave the watchdog '
       'standing down and auto-advance cancelled on a playing video')
report(SRC.count('_ext_probe_path(') >= 1 + len(_SITES92),
   'and the call sites really call it', str(SRC.count('_ext_probe_path(')))

# ── 93. VOE: the browser fallback R45 removed, driven by voe.js ───────────────
# The field log had the same file id decode on a white-label mirror and fail on
# voe.sx itself: '[VOE] static decode failed for https://voe.sx/e/b4z1hwaphvtf
# - returning None (no browser, BBB-safe)'. voe.js was already on disk and
# already read by _get_voe_capture_js, but nothing ever ran it in a browser.
_vp93 = next((n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
              and n.name == 'VideoPlayer'), None)
_fn93 = {n.name: n for n in (_vp93.body if _vp93 else [])
         if isinstance(n, ast.FunctionDef)
         and n.name in ('_voe_browser_capture', '_resolve_voe_source',
                        '_launch_voe_playwright')}
report('_voe_browser_capture' in _fn93,
   'VOE has a browser capture again. R45 removed it on the grounds that a '
   'browser was never needed for VOE; the field log is the counter-evidence')
for _n93 in ('_resolve_voe_source', '_launch_voe_playwright'):
    _b93 = (ast.get_source_segment(SRC, _fn93[_n93])
            if _n93 in _fn93 else '')
    report('_voe_browser_capture' in _b93,
       f'{_n93} falls back to it when the static decode finds nothing -- '
       'both paths, not just the one that happened to log')
report('VOE never opens a browser' not in SRC
       and 'no browser, BBB-safe' not in SRC,
   'and the claim that VOE never needs a browser is gone rather than left '
   'standing next to the code that now contradicts it')
report("if 'certificate' not in str(_ssl_exc).lower()" in SRC
       and 'verify=False' in SRC,
   'a VOE mirror whose cert chain does not resolve is retried unverified '
   'instead of losing the page: lulu.st failed with curl (60) "unable to get '
   'local issuer certificate" and the whole decode went with it')

# Build the exact script the method injects, out of the real voe.js.
if '_voe_browser_capture' in _fn93 and os.path.isfile(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voe.js')):
    _stmts93 = [s for s in _fn93['_voe_browser_capture'].body
                if isinstance(s, ast.Assign)
                and getattr(s.targets[0], 'id', '') in ('shim', 'wrapped')]
    _g93 = {'js': open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'voe.js'),
        encoding='utf-8').read()}
    for _s93 in _stmts93:
        exec(compile(ast.Module([_s93], []), '<voebuild93>', 'exec'), _g93)
    _w93 = _g93.get('wrapped') or ''
    report(bool(_w93) and 'VOE_M3U8::' in _w93,
       'the injected script emits the console marker the Python side reads, '
       'so the URL comes off the console instead of being scraped out of '
       'voe.js\u2019s on-screen panel')
    report(bool(_w93) and "_m3u8CatcherFetchHooked = true" in _w93
           and 'DOMContentLoaded' in _w93,
       'the fetch/XHR hooks install at document-start while voe.js itself '
       'waits for the DOM: voe.js builds its panel BEFORE it hooks anything, '
       'so injected raw at document-start it throws on document.head and '
       'never hooks -- the one thing it is for')
    _node93 = shutil.which('node')
    if not _node93:
        report(True, 'node is not installed here, so the injected script is '
               'not executed (syntax and hook behaviour unchecked)')
    else:
        _tmp93 = os.path.join(tempfile.gettempdir(), 'voe_wrapped93.js')
        with open(_tmp93, 'w', encoding='utf-8') as _f93:
            _f93.write(_w93)
        _chk93 = subprocess.run([_node93, '--check', _tmp93],
                                capture_output=True, text=True)
        report(_chk93.returncode == 0,
           'the injected script is valid JavaScript',
           (_chk93.stderr or '')[:120])
        _h93 = os.path.join(tempfile.gettempdir(), 'voe_hook93.js')
        with open(_h93, 'w', encoding='utf-8') as _f93:
            _f93.write("""
const fs = require('fs');
const wrapped = fs.readFileSync(process.argv[2], 'utf8');
const seen = [];
const realLog = console.log;
console.log = (...a) => { seen.push(a.join(' ')); };
global.window = global;
global.document = { head: null, body: null, readyState: 'loading',
  title: 'VOE test', createElement: () => ({ style: {}, classList: {add(){}},
  appendChild(){}, querySelector: () => null, setAttribute(){} }),
  addEventListener: () => {}, querySelector: () => null,
  querySelectorAll: () => [], getElementById: () => null };
global.navigator = { clipboard: { writeText: async () => {} } };
global.location = { href: 'https://voe.sx/e/b4z1hwaphvtf' };
class XHR {}
XHR.prototype.open = function (m, u) { this._u = u; };
global.XMLHttpRequest = XHR;
global.fetch = async () => ({ ok: true, text: async () => '' });
global.setInterval = () => 0; global.setTimeout = () => 0; global.alert = () => {};
eval(wrapped);
const M3 = 'https://cdn.example.com/engine/hls2-c/01/14443/x_,n,.urlset/master.m3u8?t=a';
window.fetch(M3);
new XMLHttpRequest().open('GET', 'https://cdn.example.com/v/720/index.m3u8');
window.fetch('https://cdn.example.com/segment00001.ts');
window.fetch('https://cdn.example.com/poster.jpg');
const got = seen.filter(l => l.startsWith('VOE_M3U8::')).map(l => l.slice(10));
const out = {
  hooked_at_document_start: window._m3u8CatcherFetchHooked === true
    && window._m3u8CatcherXHRHooked === true,
  fetch_marker: got.includes(M3),
  xhr_marker: got.includes('https://cdn.example.com/v/720/index.m3u8'),
  no_ts: !got.some(u => u.endsWith('.ts')),
  no_jpg: !got.some(u => u.endsWith('.jpg')),
  count: got.length,
};
console.log = realLog;
console.log(JSON.stringify(out));
""")
        _r93 = subprocess.run([_node93, _h93, _tmp93],
                              capture_output=True, text=True, timeout=60)
        try:
            _o93 = json.loads((_r93.stdout or '').strip().splitlines()[-1])
        except Exception:
            _o93 = {}
        report(_o93.get('hooked_at_document_start') is True,
           'run in node with document.head null, both hooks install before '
           'any page script -- the panel is what waits, not the capture')
        report(_o93.get('fetch_marker') is True
               and _o93.get('xhr_marker') is True,
           'and an m3u8 reaching the page by fetch or by XHR both produce '
           'the marker, which is how the VOE player asks for its manifest')
        report(_o93.get('no_ts') is True and _o93.get('no_jpg') is True
               and _o93.get('count') == 2,
           'while HLS segments and poster images do not: a capture that '
           'returned segment URLs would hand mpv a single fragment',
           str(_o93.get('count')))

# ── 94. Fullscreen overlay submenus open on hover, including at level 0 ───────
# The top-level overlay panel stores _fullscreen_overlay_level = 0. The hover
# guard read it with `int(getattr(...) or -1)`, and `0 or -1` is -1, so the
# guard was false and hover was switched off for every submenu on the first
# panel -- Recent Files and Recent Playlists only opened on a click.
# main.py has several eventFilter methods; this is the one that drives the
# fullscreen overlay. Picking the first by name silently selects another
# class's handler and every assertion below passes or fails for the wrong
# reason.
_ef94 = next((n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == 'eventFilter'
              and '_fullscreen_overlay_submenu' in (
                  ast.get_source_segment(SRC, n) or '')), None)
_os94 = next((n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
              and n.name == '_open_fullscreen_overlay_submenu_button'), None)
report(_ef94 is not None and _os94 is not None,
   'the fullscreen overlay hover handler and the submenu opener are both here')


def _level_expr94(fn, want):
    """Pull the real statements that compute a menu level out of main.py."""
    got = []
    for n in ast.walk(fn) if fn else []:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in want):
            got.append(n)
    got.sort(key=lambda n: getattr(n, 'lineno', 0))
    return got


for _fn94, _names94, _var94, _argname94 in (
        (_ef94, ('_olvl', 'overlay_level'), 'overlay_level', 'obj'),
        (_os94, ('_blvl', 'level'), 'level', 'button')):
    _stmts94 = _level_expr94(_fn94, _names94)
    report(len(_stmts94) == len(_names94),
       f'{_var94} is computed in {_argname94}\u2019s handler',
       str([getattr(s.targets[0], 'id', '?') for s in _stmts94]))
    if len(_stmts94) != len(_names94):
        continue
    for _lvl94, _want94 in ((0, 0), (1, 1), (2, 2), (None, -1)):
        _o94 = type('B', (), {})()
        if _lvl94 is not None:
            setattr(_o94, '_fullscreen_overlay_level', _lvl94)
        _g94 = {_argname94: _o94}
        for _s94 in _stmts94:
            exec(compile(ast.Module([_s94], []), '<lvl94>', 'exec'), _g94)
        report(_g94.get(_var94) == _want94,
           f'a button at overlay level {_lvl94} reads back as {_want94}'
           + ('' if _lvl94 != 0 else
              ' -- level 0 is the panel the user right-clicked, and `0 or -1`'
              ' is -1, which is what turned hover off'),
           f'got {_g94.get(_var94)}')

report("_fullscreen_overlay_level', -1) or -1" not in SRC,
   'and the `or -1` coercion is gone from both places, not just the one that '
   'was reported -- the same idiom in the submenu opener would have created '
   'the child panel at level 0, on top of its own parent')
_efsrc94 = ast.get_source_segment(SRC, _ef94) if _ef94 else ''
report('if overlay_level >= 0:' in _efsrc94
       and 'if overlay_submenu and _etype in hover_events:' in _efsrc94,
   'the hover guard that was being skipped is still the thing that opens the '
   'submenu, so fixing the level is what re-enables it')
report("btn.setAttribute(Qt.WidgetAttribute.WA_Hover, True)" in SRC
       and 'btn.setMouseTracking(True)' in SRC,
   'and the buttons still ask for hover events -- WA_Hover plus mouse '
   'tracking, without which no HoverEnter or MouseMove ever arrives')

# ── 95. A submenu that is already open must not be rebuilt on hover ───────────
# Hover is not one event: entering a menu button delivers Enter and then a
# stream of MouseMove / HoverMove while the pointer stays on it, and an 80 ms
# timer fires too. Re-enabling the hover handler without a guard rebuilt the
# submenu panel on every one of them -- the list blinked and the UI lagged.
_vp95 = next((n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
              and n.name == 'VideoPlayer'), None)
_fn95 = {n.name: n for n in (_vp95.body if _vp95 else [])
         if isinstance(n, ast.FunctionDef)
         and n.name in ('_fullscreen_overlay_submenu_open_for',
                        '_open_fullscreen_overlay_submenu_button',
                        '_show_fullscreen_overlay_menu')}
report('_fullscreen_overlay_submenu_open_for' in _fn95,
   'there is a way to ask whether this button\u2019s submenu is already up',
   'defined' if '_fullscreen_overlay_submenu_open_for' in _fn95 else 'MISSING')
if '_fullscreen_overlay_submenu_open_for' in _fn95:
    _g95 = {}
    exec(compile(ast.Module([_fn95['_fullscreen_overlay_submenu_open_for']], []),
                 '<guard95>', 'exec'), _g95)
    _G95 = _g95['_fullscreen_overlay_submenu_open_for']

    class _Panel95:
        def __init__(self, anchor, visible=True):
            self._fullscreen_overlay_anchor = anchor
            self._visible95 = visible

        def isVisible(self):
            return self._visible95

    class _Self95:
        def __init__(self, panels):
            self._fullscreen_overlay_menu_panels = panels

    class _Btn95:
        pass

    _a95, _b95 = _Btn95(), _Btn95()
    for _lbl95, _slf95, _btn95, _want95 in [
            ('no panels at all', _Self95([]), _a95, False),
            ('a panel anchored to this button', _Self95([_Panel95(_a95)]),
             _a95, True),
            ('a panel anchored to a different button',
             _Self95([_Panel95(_b95)]), _a95, False),
            ('this button, but the panel is hidden',
             _Self95([_Panel95(_a95, visible=False)]), _a95, False),
            ('two panels, the second one mine',
             _Self95([_Panel95(_b95), _Panel95(_a95)]), _a95, True),
            ('a panel with no anchor recorded',
             _Self95([type('P95', (), {'isVisible': lambda s: True})()]),
             _a95, False),
            ('no button at all', _Self95([_Panel95(_a95)]), None, False),
    ]:
        report(_G95(_slf95, _btn95) is _want95,
           f'the guard answers {_want95} for {_lbl95}')

    _osrc95 = ast.get_source_segment(
        SRC, _fn95['_open_fullscreen_overlay_submenu_button']) or ''
    _gline95 = _osrc95.find('_fullscreen_overlay_submenu_open_for(button)')
    _cline95 = _osrc95.find("_fullscreen_overlay_open_submenu'")
    report(_gline95 > 0 and (_cline95 < 0 or _gline95 < _cline95),
       'and the opener consults it BEFORE it rebuilds -- checking afterwards '
       'would still tear the panel down on every mouse move')

_sm95 = _fn95.get('_show_fullscreen_overlay_menu')
_smsrc95 = ast.get_source_segment(SRC, _sm95) if _sm95 else ''
report(bool(_sm95) and 'anchor_button=None' in _smsrc95
       and 'panel._fullscreen_overlay_anchor = anchor_button' in _smsrc95,
   'the panel records which button opened it -- the only thing the guard has '
   'to compare against')
report(SRC.count('anchor_button=b,') + SRC.count('anchor_button=button,') >= 2,
   'and both routes that open a submenu pass the anchor, so the guard works '
   'whether the button was built from a spec or from a QMenu snapshot',
   str(SRC.count('anchor_button=b,') + SRC.count('anchor_button=button,')))

# ── 96. Fullscreen uses the hand-drawn overlay -- a deliberate choice ─────────
# Showing the native QMenu instead was tried and reverted: the user tested it
# and kept the overlay. So the overlay is the default again, and what has to
# hold is that it behaves -- the level guard in 94 and the rebuild guard in 95
# are what make that tolerable. The native path stays written and reachable
# for any caller that passes no fallback.
_ep96 = next((n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
              and n.name == '_exec_popup_menu'), None)
_esrc96 = ast.get_source_segment(SRC, _ep96) if _ep96 else ''
report(_ep96 is not None, 'the fullscreen popup path is here')
report('QTimer.singleShot(0, fullscreen_fallback)' in _esrc96,
   'a caller that supplies an overlay gets the overlay in fullscreen -- the '
   'behaviour that was tested and kept over the native menu')
report('menu.popup(target_pos)' in _esrc96,
   'and the native popup is still written for callers that pass no fallback, '
   'so the overlay is a choice at the call site rather than the only path')
report('_menu.isVisible()' in _esrc96,
   'the switch to the overlay is still gated on the native menu never having '
   'become visible')
_pp96 = next((n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
              and n.name == '_prepare_popup_menu'), None)
_ppsrc96 = ast.get_source_segment(SRC, _pp96) if _pp96 else ''
report(bool(_pp96)
       and 'menu.setParent(None)' in _ppsrc96
       and 'Qt.WindowType.Popup' in _ppsrc96
       and 'WindowStaysOnTopHint' in _ppsrc96,
   'the menu is still reparented and flagged Popup/Frameless/StaysOnTop, '
   'which is what would let a native popup appear over a borderless-maximized '
   'window if that path is ever wanted again')

# ── 97. Fullscreen: mirrors survive the snapshot, and submenus have no header ─
# Two things the fullscreen overlay got wrong that a native menu gets right.
_vp97 = next((n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
              and n.name == 'VideoPlayer'), None)
_fn97 = {n.name: n for n in (_vp97.body if _vp97 else [])
         if isinstance(n, ast.FunctionDef)
         and n.name in ('_fullscreen_widget_action_rows',
                        '_build_fullscreen_qmenu_snapshot',
                        '_show_fullscreen_overlay_menu')}
report('_fullscreen_widget_action_rows' in _fn97,
   'there is a way to read a QWidgetAction row',
   'defined' if '_fullscreen_widget_action_rows' in _fn97 else 'MISSING')
if '_fullscreen_widget_action_rows' in _fn97:
    _g97 = {'QPushButton': object}
    exec(compile(ast.Module(
        [_fn97['_fullscreen_widget_action_rows']], []), '<rows97>', 'exec'),
        _g97)
    _F97 = _g97['_fullscreen_widget_action_rows']

    class _Btn97:
        def __init__(self, text, enabled=True):
            self._t = text
            self._e = enabled
            self.clicks97 = 0

        def text(self):
            return self._t

        def isEnabled(self):
            return self._e

        def click(self):
            self.clicks97 += 1

    class _W97:
        def __init__(self, buttons):
            self._b = buttons

        def findChildren(self, _t):
            return list(self._b)

    class _A97:
        def __init__(self, widget):
            self._w = widget

        def defaultWidget(self):
            return self._w

    class _NoW97:
        def defaultWidget(self):
            return None

    _U97 = 'https://kamehaus.net/e/37n3u6rwt2hf'
    for _lbl97, _act97, _want97 in [
            ('a mirror row: load button plus Split',
             _A97(_W97([_Btn97(_U97), _Btn97('Split')])), 1),
            ('the same row with Split discovered first',
             _A97(_W97([_Btn97('Split'), _Btn97(_U97)])), 1),
            ('an action with no default widget', _NoW97(), 0),
            ('an action with no defaultWidget() at all', object(), 0),
            ('a widget holding no buttons', _A97(_W97([])), 0),
            ('a row whose only button is disabled',
             _A97(_W97([_Btn97(_U97, enabled=False)])), 0),
    ]:
        report(len(_F97(None, _act97)) == _want97,
           f'{_lbl97} yields {_want97} entr'
           + ('y' if _want97 == 1 else 'ies'))

    _rows97 = _F97(None, _A97(_W97([_Btn97('Split'), _Btn97(_U97)])))
    _r97 = _rows97[0] if _rows97 else {}
    # .get() throughout: reading a key that a reverted main.py does not
    # produce would abort the run and hide every failure after this point.
    _b97l = [b.get('label', '') for b in (_r97.get('buttons') or [])]
    report(_r97.get('label') == _U97,
       'the row is labelled with the mirror URL, not with the five-letter '
       'Split button, whatever order findChildren returned them in')
    report(len(_b97l) == 2,
       'and it carries BOTH of its buttons inside itself -- one menu item, '
       'two buttons, the way the native row is drawn', str(_b97l))
    report(_b97l.count(_U97) == 1 and _b97l.count('Split') == 1,
       'each button keeps its own text: the URL is not repeated as a second '
       'item, and Split is not renamed to "<url> - Split"', str(_b97l))
    report(bool(_b97l) and not any(' - ' in b for b in _b97l),
       'nothing was suffixed onto anything -- that is exactly what made '
       'every mirror show up twice', str(_b97l))
    report(sum(1 for b in (_r97.get('buttons') or []) if b.get('primary')) == 1,
       'exactly one button is marked as the row label, whichever one it is')
    _ld97, _sp97 = _Btn97(_U97), _Btn97('Split')
    _e97 = (_F97(None, _A97(_W97([_ld97, _sp97]))) or [{}])[0]
    for _b97s in (_e97.get('buttons') or []):
        if _b97s['primary']:
            _b97s['callback']()
        else:
            _b97s['callback']()
    report(_ld97.clicks97 == 1 and _sp97.clicks97 == 1,
       'and each button drives its own click on the hidden source menu, not '
       'the other one\'s')
    report('primary' not in _r97,
       'the sort key stays inside the button list, not on the entry where '
       'the panel builder would read it as part of the spec')
    _d97 = (_F97(None, _A97(_W97([_Btn97(_U97, enabled=False),
                                  _Btn97('Split')]))) or [{}])[0]
    report([b.get('primary') for b in (_d97.get('buttons') or [])] == [True],
       'when the label button is the disabled one, the survivor is promoted '
       'rather than leaving the row with no label at all')

    _ssrc97 = ast.get_source_segment(
        SRC, _fn97['_build_fullscreen_qmenu_snapshot']) or ''
    _k97 = _ssrc97.find('_fullscreen_widget_action_rows(')
    _c97 = _ssrc97.find('if not label and submenu is None:')
    report(0 < _c97 < _k97,
       'and the snapshot reads those rows instead of skipping them. A '
       'QWidgetAction has no text and no submenu, which is exactly the shape '
       'the skip was written for -- so every mirror vanished from the '
       'fullscreen menu and only the plain current-file row survived')

_sm97 = _fn97.get('_show_fullscreen_overlay_menu')
_smsrc97 = ast.get_source_segment(SRC, _sm97) if _sm97 else ''
report('if title and int(level or 0) == 0:' in _smsrc97,
   'only the top-level panel gets a header. A submenu is passed the label of '
   'the button that opened it, so it repeated that name on top of the list it '
   'came from -- and out of fullscreen no submenu has a header at all')

# ── 98. A mirror in fullscreen is one row with a Split button on it ───────────
# The overlay drew every mirror twice: the URL, then "<url> - Split". Out of
# fullscreen a mirror is a QWidgetAction -- the URL on the left, a Split
# button on the right -- so the overlay rebuilds that row instead of
# flattening it. This runs the real builder against fake Qt widgets.
_fn98 = {n.name: n for n in (_vp97.body if _vp97 else [])
         if isinstance(n, ast.FunctionDef)
         and n.name in ('_fullscreen_overlay_widget_row',
                        '_fullscreen_overlay_click_handler')}
report(set(_fn98) == {'_fullscreen_overlay_widget_row',
                      '_fullscreen_overlay_click_handler'},
   'the overlay can build a row that holds more than one button',
   str(sorted(_fn98)))
report("row_specs = action.get('buttons')" in _smsrc97
       and '_fullscreen_overlay_widget_row(' in _smsrc97,
   'and the panel builder routes a button-bearing entry to it instead of '
   'making a button per entry')
report('QPushButton#fullscreenOverlayMenuRowAction {' in _smsrc97
       and 'QFrame#fullscreenOverlayMenuRow {' in _smsrc97,
   'the row and its Split button are styled, so the button reads as a button '
   'rather than as another line of text')
if set(_fn98) == {'_fullscreen_overlay_widget_row',
                  '_fullscreen_overlay_click_handler'}:
    _g98 = {'QPushButton': object}

    class _Sig98:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def fire(self):
            for s in list(self.slots):
                s()

    class _Btn98:
        def __init__(self, text='', parent=None):
            self._t = text
            self.name98 = ''
            self.flat98 = False
            self.on98 = True
            self.pol98 = None
            self.cur98 = None
            self.clicked = _Sig98()

        def setObjectName(self, n):
            self.name98 = n

        def setFlat(self, v):
            self.flat98 = bool(v)

        def setEnabled(self, v):
            self.on98 = bool(v)

        def setSizePolicy(self, h, v):
            self.pol98 = (h, v)

        def setCursor(self, c):
            self.cur98 = c

        def text(self):
            return self._t

    class _Lay98:
        def __init__(self, parent=None):
            self.items = []
            self.margins98 = None
            self.spacing98 = None

        def setContentsMargins(self, *a):
            self.margins98 = a

        def setSpacing(self, v):
            self.spacing98 = v

        def addWidget(self, w, stretch=0):
            self.items.append((w, stretch))

    class _Row98:
        def __init__(self, parent=None):
            self.name98 = ''
            self.shape98 = None

        def setObjectName(self, n):
            self.name98 = n

        def setFrameShape(self, s):
            self.shape98 = s

    class _Pol98:
        class Policy:
            Expanding = 'expanding'
            Fixed = 'fixed'

    class _Shape98(_Row98):
        class Shape:
            NoFrame = 'noframe'

        def __init__(self, parent=None):
            _Row98.__init__(self, parent)
            self.parent98 = parent

    class _Cur98:
        class CursorShape:
            PointingHandCursor = 'hand'

    class _Self98:
        def __init__(self):
            self.closed98 = []
            self._fullscreen_overlay_source_menu = 'SOURCE'
            self._fullscreen_overlay_qmenu_clone = None
            self._fullscreen_overlay_menu_widget = None

        def _close_fullscreen_overlay_menu(self, clear_menus=True,
                                           keep_levels=0):
            self.closed98.append(clear_menus)

    # _Lay98 records itself on the row it is handed, so the test can see the
    # widgets the row added and the stretch each of them got.
    class _Lay98b(_Lay98):
        def __init__(self, parent=None):
            _Lay98.__init__(self, parent)
            parent._lay98 = self

    _g98.update({
        'QFrame': _Shape98, 'QPushButton': _Btn98, 'QHBoxLayout': _Lay98b,
        'QSizePolicy': _Pol98, 'Qt': _Cur98,
    })
    exec(compile(ast.Module(list(_fn98.values()), []), '<row98>', 'exec'), _g98)
    _Self98._fullscreen_overlay_click_handler = \
        _g98['_fullscreen_overlay_click_handler']
    _R98 = _g98['_fullscreen_overlay_widget_row']

    _U98 = 'https://kamehaus.net/e/37n3u6rwt2hf'
    _P98 = object()
    hits98 = []

    def _spec98(order):
        # Exactly what _fullscreen_widget_action_rows hands over, in the order
        # findChildren happened to return the buttons.
        return [{'label': t, 'enabled': True, 'primary': t == _U98,
                 'callback': (lambda _c=False, n=t: hits98.append(n))}
                for t in order]

    _row98 = _R98(_Self98(), _P98, _spec98([_U98, 'Split']))
    report(_row98 is not None and _row98.name98 == 'fullscreenOverlayMenuRow',
       'the row is a container of its own, not two loose buttons dropped into '
       'the panel')

    for _lbl98, _order98 in [('the URL first', [_U98, 'Split']),
                             ('Split discovered first', ['Split', _U98])]:
        _slf98 = _Self98()
        _row98 = _R98(_slf98, _P98, _spec98(_order98))
        _b98 = [w for w, _s in _row98._lay98.items]
        _st98 = [s for _w, s in _row98._lay98.items]
        report(len(_b98) == 2, f'{_lbl98}: the row holds both buttons',
           str([b.text() for b in _b98]))
        report([b.text() for b in _b98] == [_U98, 'Split'],
           f'{_lbl98}: the URL is on the left and Split on the right, '
           'whatever order the source row yielded them in')
        report([b.name98 for b in _b98]
               == ['fullscreenOverlayMenuButton',
                   'fullscreenOverlayMenuRowAction'],
           f'{_lbl98}: the URL button is styled as a menu item and Split as a '
           'button')
        report(_st98 == [1, 0]
               and _b98[0].pol98 == ('expanding', 'fixed')
               and _b98[1].pol98 == ('fixed', 'fixed'),
           f'{_lbl98}: the URL takes the space and Split keeps its own width, '
           'the same stretch the native row uses')
        report(_row98._lay98.margins98 == (0, 0, 0, 0),
           f'{_lbl98}: the row adds no padding of its own, so it lines up '
           'with the plain items above and below it')

    # Clicking: both buttons close the menu and drive their own action.
    _slf98 = _Self98()
    hits98 = []
    _row98 = _R98(_slf98, _P98, _spec98([_U98, 'Split']))
    _b98 = [w for w, _s in _row98._lay98.items]
    _b98[0].clicked.fire()
    report(hits98 == [_U98] and _slf98.closed98 == [False],
       'clicking the URL loads that mirror and takes the menu down -- once, '
       'with no second item doing the same thing')
    _slf98 = _Self98()
    hits98 = []
    _row98 = _R98(_slf98, _P98, _spec98([_U98, 'Split']))
    _b98 = [w for w, _s in _row98._lay98.items]
    _b98[1].clicked.fire()
    report(hits98 == ['Split'] and _slf98.closed98 == [False],
       'and clicking Split splits that mirror out of the group, which is the '
       'button the fullscreen menu was missing')
    report(_b98[1].cur98 == 'hand',
       'Split shows a pointing hand, so it reads as something to press and '
       'not as a second label')

    _slf98 = _Self98()
    _row98 = _R98(_slf98, _P98, [{'label': 'Quiet', 'enabled': True,
                            'primary': True, 'callback': None}])
    _b98 = [w for w, _s in _row98._lay98.items]
    _b98[0].clicked.fire()
    report(_slf98.closed98 == [True],
       'a button with nothing to do just closes the menu rather than '
       'swallowing the click')
    for _lbl98, _specs98 in [('no specs at all', []),
                             ('only blanks', [{'label': '  '}]),
                             ('not dicts', ['x', 3])]:
        report(_R98(_Self98(), _P98, _specs98) is None,
           f'{_lbl98} builds no row')

    # End to end: the reader from 97 feeding the builder here.
    _f97e = _fn97.get('_fullscreen_widget_action_rows')
    if _f97e:
        _g98e = {'QPushButton': object}
        exec(compile(ast.Module([_f97e], []), '<rows98e>', 'exec'), _g98e)

        class _B98e:
            def __init__(self, t):
                self._t = t
                self.clicks = 0

            def text(self):
                return self._t

            def isEnabled(self):
                return True

            def click(self):
                self.clicks += 1

        class _W98e:
            def __init__(self, b):
                self._b = b

            def findChildren(self, _t):
                return list(self._b)

        class _A98e:
            def __init__(self, w):
                self._w = w

            def defaultWidget(self):
                return self._w

        _l98e, _s98e = _B98e(_U98), _B98e('Split')
        _entry98 = _g98e['_fullscreen_widget_action_rows'](
            None, _A98e(_W98e([_s98e, _l98e])))
        _slf98 = _Self98()
        _row98 = _R98(_slf98, _P98, _entry98[0]['buttons'])
        _b98 = [w for w, _s in _row98._lay98.items]
        report([b.text() for b in _b98] == [_U98, 'Split'],
           'read from a real QWidgetAction and built into a real row: one '
           'mirror, one line, two buttons')
        _b98[1].clicked.fire()
        report(_s98e.clicks == 1 and _l98e.clicks == 0,
           'and the Split button on that row is the source menu\'s own Split '
           'button')

# ── 99. A long recent-file name wraps instead of widening the menu ────────────
# A QMenu is as wide as its longest item and the fullscreen overlay panel is
# too, so one long name in Recent Files / Recent Playlists stretched the list
# across the screen. Neither will wrap on its own, so the break is made in
# _wrap_menu_label; this executes that function against the reported cases.
_LONG99 = ('S01E01 - The One With The Extremely Long Episode Title That '
           'Nobody Can Read (1080p) [x264] [AAC] - Group.m3u8')
_g99 = {}


class _Lab99:
    """Stands in for QLabel: records what the menu row asked of it."""

    def __init__(self, text='', parent=None):
        self._t = text
        self.attrs99 = []
        self.style99 = ''
        self.fmt99 = None
        self.tracking99 = None
        self.filters99 = []

    def installEventFilter(self, f):
        self.filters99.append(f)

    def setTextFormat(self, f):
        self.fmt99 = f

    def setMouseTracking(self, v):
        self.tracking99 = bool(v)

    def setAttribute(self, a, v):
        self.attrs99.append((a, v))

    def setStyleSheet(self, s):
        self.style99 = s

    def text(self):
        return self._t


class _Lay99:
    def __init__(self, parent=None):
        self.items = []
        self.margins99 = None

    def setContentsMargins(self, *a):
        self.margins99 = a

    def setSpacing(self, v):
        pass

    def addWidget(self, w, stretch=0):
        self.items.append((w, stretch))


class _Row99:
    def __init__(self, parent=None):
        self._lay99 = None


class _Lay99b(_Lay99):
    def __init__(self, parent=None):
        _Lay99.__init__(self, parent)
        parent._lay99 = self


class _Act99:
    """Stands in for QWidgetAction."""

    def __init__(self, parent=None):
        self._text99 = None
        self._widget99 = None
        self.tip99 = None

    def setDefaultWidget(self, w):
        self._widget99 = w

    def defaultWidget(self):
        return self._widget99

    def setText(self, t):
        self._text99 = t

    def text(self):
        return self._text99

    def setToolTip(self, t):
        self.tip99 = t


class _Menu99:
    def __init__(self):
        self.added99 = []

    def addAction(self, a):
        self.added99.append(a)
        return a


class _Qt99:
    class TextFormat:
        PlainText = 'plain'

    class WidgetAttribute:
        WA_TransparentForMouseEvents = 'transparent'
        WA_Hover = 'hover'


_wl99 = next((n for n in TREE.body if isinstance(n, ast.FunctionDef)
              and n.name == '_wrap_menu_label'), None)
_wc99 = [n for n in TREE.body if isinstance(n, ast.Assign)
         and getattr(n.targets[0], 'id', '').startswith('_MENU_LABEL_WRAP')]
report(bool(_wl99) and len(_wc99) == 2,
   'there is a place where a long menu label is broken over lines',
   f'function={bool(_wl99)} constants={len(_wc99)}')
if _wl99 is not None:
    exec(compile(ast.Module(body=_wc99 + [_wl99], type_ignores=[]),
                 '<wrap99>', 'exec'), _g99)
_W99 = _g99.get('_wrap_menu_label')
_WID99 = _g99.get('_MENU_LABEL_WRAP_WIDTH', 44)
_MAX99 = _g99.get('_MENU_LABEL_WRAP_MAX_LINES', 3)
if _W99 is not None:
    _HUGE99 = 'word ' * 90

    for _lbl99, _in99 in [('nothing at all', ''), ('None', None),
                          ('a short name', 'Dressage (1986)'),
                          ('exactly one line wide', 'x' * _WID99),
                          ('a long file name', _LONG99),
                          ('an absurd name', _HUGE99),
                          ('one word longer than a line',
                           'x' * (_WID99 * 4))]:
        _lines99 = _W99(_in99).split('\n')
        report(all(len(_l) <= _WID99 for _l in _lines99),
           f'{_lbl99}: no line is wider than {_WID99} characters',
           str([len(_l) for _l in _lines99]))
        report(all(_l == _l.strip() for _l in _lines99),
           f'{_lbl99}: no line starts or ends on a space, so nothing hangs '
           'off the edge of the menu')
        report(len(_lines99) <= _MAX99,
           f'{_lbl99}: at most {_MAX99} lines, so the submenu cannot grow '
           'tall either', str(len(_lines99)))

    report(_W99('') == '' and _W99(None) == '',
       'an empty label is still empty')
    report(_W99('Dressage (1986)') == 'Dressage (1986)'
           and _W99('x' * _WID99) == 'x' * _WID99,
       'a name that already fits is returned byte for byte, so nothing that '
       'looks right today changes')
    report('\n' in _W99('x' * (_WID99 + 1)),
       'and one character past the limit is what starts the wrapping')
    report(' '.join(_W99(_LONG99).split()) == _LONG99,
       'a name that fits in the line budget keeps every word, in order -- '
       'it is broken, not shortened')
    _h99 = _W99(_HUGE99).split('\n')
    report(len(_h99) == _MAX99 and _h99[-1].endswith('\u2026')
           and len(_h99[-1]) <= _WID99,
       'a name that does not fit is cut on the last line with an ellipsis '
       'instead of running on', str([len(_l) for _l in _h99]))
    report(_W99('x' * (_WID99 * 4)).split('\n')[0] == 'x' * _WID99,
       'a single word longer than a line is cut inside the word -- there is '
       'no space to break on, and one unbroken token is exactly what was '
       'widening the menu')
    report(_W99('abcdef', width=0) == 'abcdef',
       'width=0 means "do not wrap", not "use the default" -- `width or '
       'DEFAULT` would have read it the other way')
    report(len(_W99('aa bb cc dd', width=2, max_lines=0).split('\n')) == 1,
       'max_lines is never below one, so a label cannot wrap to nothing')
    for _w99 in (8, 12, 44, 80):
        report(all(len(_l) <= _w99
                   for _l in _W99(_HUGE99, width=_w99).split('\n')),
           f'the budget is honoured at width={_w99}')

# ── 99b. The menu item that can be two lines tall ─────────────────────────────
_vp99 = next((n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
              and n.name == 'VideoPlayer'), None)
_am99 = next((n for n in (_vp99.body if _vp99 else [])
              if isinstance(n, ast.FunctionDef)
              and n.name == '_add_wrapped_menu_action'), None)
report(_am99 is not None, 'and a menu can be given such a label',
   'defined' if _am99 is not None else 'MISSING')
if _am99 is not None:
    _asrc99 = ast.get_source_segment(SRC, _am99) or ''
    report('action.setText(' not in _asrc99,
       'the action carries no text of its own -- the menu would draw it next '
       'to the widget and the name would appear twice, which is the same '
       'mistake the mirror rows already avoid')
    report('.setWordWrap(' not in _asrc99,
       'and the label is not left to setWordWrap: a word-wrapping QLabel '
       'reports the UNWRAPPED text as its size hint, so the menu would '
       'measure itself against the full name and widen anyway')
    report('WA_TransparentForMouseEvents' not in _asrc99,
       'the label is NOT made transparent to the mouse: it has to see the '
       'pointer in order to paint the highlight a menu will not paint for it')
    report('QLabel:hover' in _asrc99,
       'it carries a hover rule of its own. QMenu reserves space for a '
       'QWidgetAction\u2019s widget and never paints the item highlight over '
       'it, which is exactly why the single-line entries highlighted on '
       'hover and the wrapped ones did not')
    report('WA_Hover' in _asrc99 and 'label.setMouseTracking(True)' in _asrc99,
       'and it asks for hover events and mouse tracking, without which a '
       ':hover rule never changes state')

    _g99b = dict(_g99)
    _g99b.update({'QWidgetAction': _Act99, 'QWidget': _Row99,
                  'QHBoxLayout': _Lay99b, 'QLabel': _Lab99, 'Qt': _Qt99})
    exec(compile(ast.Module([_am99], []), '<addwrap99>', 'exec'), _g99b)
    _A99 = _g99b['_add_wrapped_menu_action']

    _m99 = _Menu99()
    _a99 = _A99(None, _m99, 'Dressage (1986)')
    report(_m99.added99 == [_a99] and not isinstance(_a99, _Act99),
       'a name that fits goes in as an ordinary action -- no widget, no '
       'extra painting, nothing about it changes')
    _m99 = _Menu99()
    _a99 = _A99(None, _m99, _LONG99)
    report(isinstance(_a99, _Act99) and _m99.added99 == [_a99],
       'and a name that does not fit becomes a widget action that is still '
       'added to the menu')
    _lab99 = _a99.defaultWidget()._lay99.items[0][0]
    report(_lab99.text() == _W99(_LONG99) and '\n' in _lab99.text(),
       'the widget holds the broken text, so the item is two or three lines '
       'tall instead of one very long one', repr(_lab99.text()))
    report(all(len(_l) <= _WID99 for _l in _lab99.text().split('\n')),
       'which is what stops the submenu taking the width of the screen')
    report(_a99.text() is None and _a99.tip99 == _LONG99,
       'the full name is not lost -- it is kept on the action as its tooltip')
    report(('hover', True) in _lab99.attrs99
           and _lab99.tracking99 is True,
       'the label really is given both at build time, not merely named in '
       'the source', str(_lab99.attrs99))
    report(':hover' in _lab99.style99
           and 'background: transparent' in _lab99.style99,
       'it paints nothing until it is hovered, so the menu\u2019s own '
       'background shows through otherwise')
    report('padding: 6px 22px 6px 24px' in _lab99.style99,
       'and its padding is the menu\u2019s own item padding, so the text '
       'starts where a single-line item\u2019s text starts')
    report(_a99.defaultWidget()._lay99.margins99 == (0, 0, 0, 0),
       'the row adds no margins of its own, so the highlight spans the whole '
       'item the way a plain one does',
       str(_a99.defaultWidget()._lay99.margins99))
    report(_A99(None, _Menu99(), None) is not None,
       'an empty label does not blow up on the way in')

# ── 99c. The fullscreen snapshot has to read a label row back ─────────────────
# The playlist context menu is snapshotted to build the fullscreen overlay, so
# a QWidgetAction the snapshot cannot read would make those entries vanish --
# exactly what happened to the mirrors.
_fn99c = next((n for n in (_vp99.body if _vp99 else [])
               if isinstance(n, ast.FunctionDef)
               and n.name == '_fullscreen_widget_action_rows'), None)
report(_fn99c is not None,
   'the snapshot reader that recovered the mirror rows is still there')
if _fn99c is not None and _W99 is not None:
    _g99c = {'QPushButton': object, 'QLabel': _Lab99}
    exec(compile(ast.Module([_fn99c], []), '<rows99c>', 'exec'), _g99c)
    _F99c = _g99c['_fullscreen_widget_action_rows']

    class _Wid99c:
        def __init__(self, labels=(), buttons=()):
            self._l = list(labels)
            self._b = list(buttons)

        def findChildren(self, t):
            return list(self._l) if t is _Lab99 else list(self._b)

    class _Act99c:
        def __init__(self, w):
            self._w = w
            self.triggers99 = 0

        def defaultWidget(self):
            return self._w

        def trigger(self):
            self.triggers99 += 1

    _wrapped99 = _W99(_LONG99)
    _act99c = _Act99c(_Wid99c(labels=[_Lab99(_wrapped99)]))
    _rows99c = _F99c(None, _act99c)
    report(len(_rows99c) == 1, 'a wrapping label row is ONE overlay entry',
       str(len(_rows99c)))
    report(bool(_rows99c) and _rows99c[0].get('label') == _wrapped99,
       'and it keeps the broken text, so the fullscreen menu wraps it too '
       'instead of dropping the entry or stretching the panel')
    report('buttons' not in (_rows99c[0] if _rows99c else {}),
       'with no buttons to lay out, it is a plain item rather than a row')
    if _rows99c:
        _rows99c[0]['callback']()
    report(_act99c.triggers99 == 1,
       'and picking it triggers the action itself -- a label row has no '
       'button to click')
    report(_F99c(None, _Act99c(_Wid99c())) == [],
       'a custom row with nothing readable in it still yields nothing')
    _r99c = _F99c(None, _Act99c(
        _Wid99c(labels=[_Lab99('short'), _Lab99(_wrapped99)])))
    report(bool(_r99c) and _r99c[0].get('label') == _wrapped99,
       'with more than one label in the row, the one that is actually the '
       'name wins')

# ── 99d. Every recent-file list goes through it ───────────────────────────────
report(SRC.count('_add_wrapped_menu_action(') >= 6,
   'all four places that build a Recent Files or Recent Playlists menu use '
   'it, not just the one that was reported',
   str(SRC.count('_add_wrapped_menu_action(')))
for _pat99 in ('addAction(self._get_playlist_name_for_path(file_path))',
               'addAction(os.path.basename(file_path))',
               'addAction(self.parent_player._get_playlist_name_for_path('
               'file_path))'):
    report(_pat99 not in SRC,
       'no recent-list site still adds the raw name straight to the menu',
       _pat99)
report(SRC.count("'label': _wrap_menu_label(") == 2,
   'and the overlay fallback used when the snapshot comes up empty wraps its '
   'labels too, so fullscreen behaves the same either way',
   str(SRC.count("'label': _wrap_menu_label(")))

# ── 100. Two highlights at once: the menu never learns the pointer moved ──────
# With the label seeing the mouse, the menu stops tracking it, so the item it
# highlighted before stayed lit next to the wrapped one under the pointer
# (QTBUG-10605). QMenu::setActiveAction clears that -- Qt does it itself from
# leaveEvent, with setActiveAction(0) -- and setCurrentAction repaints the
# rect of the action it replaces. This runs the real filter that calls it.
_hv100 = next((n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == '_WrappedMenuItemHover'), None)
report(_hv100 is not None,
   'something tells the menu which item the pointer is really on',
   'defined' if _hv100 is not None else 'MISSING')
report('_WrappedMenuItemHover(menu, action, label)' in SRC
       and 'label.installEventFilter(hover_sync)' in SRC,
   'and every wrapped item installs one on its own label, so the menu is '
   'told at the moment the pointer arrives rather than by something '
   'watching the menu and guessing')
if _hv100 is not None:

    class _Obj100:
        def __init__(self, parent=None):
            self.parent100 = parent

    class _QE100:
        class Type:
            Enter = 'enter'
            Leave = 'leave'
            MouseMove = 'move'

    class _Ev100:
        def __init__(self, t):
            self._t = t

        def type(self):
            return self._t

    class _Menu100:
        def __init__(self, raises=False):
            self.active100 = []
            self._raises = raises

        def setActiveAction(self, a):
            self.active100.append(a)
            if self._raises:
                raise RuntimeError('menu gone')

    _g100 = {'QObject': _Obj100, 'QEvent': _QE100}
    exec(compile(ast.Module([_hv100], []), '<hover100>', 'exec'), _g100)
    _H100 = _g100['_WrappedMenuItemHover']

    _act100 = object()
    _menu100 = _Menu100()
    _f100 = _H100(_menu100, _act100)
    report(_f100.eventFilter(None, _Ev100('enter')) is False,
       'entering the wrapped item hands the menu that action')
    report(_menu100.active100 == [_act100],
       'and nothing else -- setActiveAction is what repaints the rect of the '
       'item it replaces, which is the highlight that was left behind',
       str(len(_menu100.active100)))
    for _t100 in ('leave', 'move'):
        _menu100 = _Menu100()
        _H100(_menu100, _act100).eventFilter(None, _Ev100(_t100))
        report(_menu100.active100 == [],
           f'a {_t100} event changes nothing: the menu is still tracking the '
           'pointer wherever it is not inside this widget')
    report(_H100(None, _act100).eventFilter(None, _Ev100('enter')) is False
           and _H100(_Menu100(), None).eventFilter(
               None, _Ev100('enter')) is False,
       'with no menu or no action there is nothing to tell, and it says '
       'nothing')
    report(_H100(_Menu100(raises=True), _act100).eventFilter(
        None, _Ev100('enter')) is False,
       'a menu that has gone away mid-hover does not take the menu down with '
       'it')
    _menu100 = _Menu100()
    for _ in range(3):
        _f100b = _H100(_menu100, _act100)
        report(_f100b.eventFilter(None, _Ev100('enter')) is False,
           'and the filter never swallows the event -- the label still needs '
           'the Enter, and the press still has to reach the menu to trigger '
           'the action')

    _asrc100 = ast.get_source_segment(SRC, _am99) if _am99 else ''
    report('_WrappedMenuItemHover(menu, action, label)' in _asrc100
           and 'label.installEventFilter(hover_sync)' in _asrc100,
       'every wrapped item gets one, parented to its own label so it lives '
       'exactly as long as the row does')
    if _am99 is not None:
        _g100b = dict(_g99)   # the real _wrap_menu_label and its constants
        _built100 = []

        class _H100b:
            def __init__(self, menu, action, parent=None):
                self.args100 = (menu, action, parent)
                _built100.append(self)

        _g100b.update({'_WrappedMenuItemHover': _H100b,
                       'QWidgetAction': _Act99, 'QWidget': _Row99,
                       'QHBoxLayout': _Lay99b, 'QLabel': _Lab99,
                       'Qt': _Qt99})
        exec(compile(ast.Module([_am99], []), '<addwrap100>', 'exec'), _g100b)
        _m100 = _Menu99()
        _a100 = _g100b['_add_wrapped_menu_action'](None, _m100, _LONG99)
        _lab100 = _a100.defaultWidget()._lay99.items[0][0]
        report(len(_built100) == 1, 'building a wrapped item builds one',
           str(len(_built100)))
        if _built100:
            report(_built100[0].args100[0] is _m100
                   and _built100[0].args100[1] is _a100
                   and _built100[0].args100[2] is _lab100,
               'wired to this menu, this action and this label -- not to '
               'whichever menu was built last')
        report(_lab100.filters99 and _lab100.filters99[0] is _built100[0],
           'and installed on the label, which is the widget the pointer '
           'actually lands on')
        _m100 = _Menu99()
        _n100 = len(_built100)
        _g100b['_add_wrapped_menu_action'](None, _m100, 'Dressage (1986)')
        report(len(_built100) == _n100,
           'a one-line name gets no filter: the menu already tracks those '
           'correctly, which is why only the wrapped ones doubled up')

# ── 101. Captions a site generates itself (auto subs and their translations) ──
# The reported sites put no caption FILE in the page: the player is handed an
# endpoint that lists the tracks, so the scan of the page for .vtt/.srt finds
# nothing and the app offers no subtitles. These run the real functions that
# look for that endpoint and read whatever JSON comes back.
report('def _subtitle_endpoint_candidates(' in SRC
       and 'def _subtitle_tracks_from_payload(' in SRC
       and 'def _extract_subtitle_tracks_from_endpoints(' in SRC
       and 'def _subtitle_tracks_from_body(' in SRC
       and 'def _subtitle_endpoint_body(' in SRC,
   'the app can read a site\'s own subtitle list, not just a caption file')
_r101 = SRC.index('def _resolve_stream_from_html(')
_body101 = SRC[_r101:_r101 + 14000]
_first101 = _body101.find('_extract_subtitle_tracks_from_html(')
_second101 = _body101.find('_extract_subtitle_tracks_from_endpoints(')
report(_first101 != -1, 'the page is still scanned for caption files first')
report(_second101 != -1,
   'and when the page holds none, the endpoint it points at is read -- that '
   'is the resolver the field log\'s [HTML_RESOLVE] lines come from')
report(-1 < _first101 < _second101,
   'as a fallback, so a site that does hand over a .vtt is untouched')

_sub_fns = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
            and n.name in ('_caption_ext_of', '_lang_from_url',
                           '_subtitle_endpoint_candidates',
                           '_subtitle_tracks_from_payload')]
_sub_consts = [n for n in TREE.body if isinstance(n, ast.Assign)
               and getattr(n.targets[0], 'id', '').startswith(
                   ('_CAPTION_FILE_EXTS', '_SUB_'))]
report(len(_sub_fns) == 4, 'the caption helpers sit at module level, reusable',
       'found %d of 4' % len(_sub_fns))
from urllib.parse import urlsplit as _us101
_g101 = {'re': re, 'os': os, 'json': json, 'urlparse': urlparse,
         'urljoin': urljoin, 'urlsplit': _us101,
         'html_unescape': html_unescape, 'print': print}
exec(compile(ast.Module(body=_sub_consts + _sub_fns, type_ignores=[]),
             'subs101', 'exec'), _g101)
_candidates = _g101['_subtitle_endpoint_candidates']
_from_payload = _g101['_subtitle_tracks_from_payload']
report(_g101['_caption_ext_of']('https://a/b.vtt?x=1') == 'vtt',
   'a caption file is recognised through a query string')
report(_g101['_caption_ext_of']('https://a/video-subtitles/9') == '',
   'and an endpoint is not one, which is how the two are told apart')
report(_g101['_lang_from_url']('https://cdn/a/en.vtt') == 'en'
       and _g101['_lang_from_url']('https://cdn/a/s.vtt?lang=ja') == 'ja',
   'a track\'s language is read off its URL when nothing else states one')
report(_g101['_lang_from_url']('https://cdn/embed/xyz/a.vtt') == '',
   'and a path segment that is not a language is not dressed up as one')

# A page that hands over a caption file must not also invent an endpoint out
# of the kind="captions" attribute it was rejected by.
_track_page = '<track kind="captions" srclang="en" src="https://cdn.x.com/a/en.vtt">'
report(_candidates(_track_page, 'https://x.com/v/1') == [],
   'a page that already gives a caption file invents no endpoint',
   repr(_candidates(_track_page, 'https://x.com/v/1')))
report(_candidates('<script>var s="https://cdn.x.com/m/1.m3u8";</script>',
                   'https://x.com/v/1') == [],
   'a page with no subtitle reference yields none')
report(_candidates('', 'https://x.com/v/1') == [], 'an empty page yields none')
report(_candidates('<a href="subtitles">subs</a>', '') == [],
   'a bare word is not joined onto the page URL to make one up')
# What the reported sites look like: an endpoint inside a JS string, so the
# slashes are escaped.
_endpoint_page = (
    '<script>html5player.setSubtitlesUrl('
    '"https:\\/\\/www.xvideos.com\\/video-subtitles\\/upbutthfd81");'
    'var cfg = {"captions":"\\/api\\/captions?id=9",'
    '"other":"https:\\/\\/cdn.elsewhere.com\\/subtitles\\/9.json",'
    '"thumb":"https:\\/\\/cdn.xvideos.com\\/thumbs\\/1.jpg"};'
    '</script>')
_found101 = _candidates(_endpoint_page,
                        'https://www.xvideos.com/video.upbutthfd81/title')
report('https://www.xvideos.com/video-subtitles/upbutthfd81' in _found101,
   'an endpoint written with escaped slashes is found', repr(_found101))
report('https://www.xvideos.com/api/captions?id=9' in _found101,
   'and a relative one is joined onto the page it came from')
report('https://cdn.xvideos.com/thumbs/1.jpg' not in _found101,
   'while an unrelated CDN URL is not mistaken for one')
report(bool(_found101) and _found101[0].startswith('https://www.xvideos.com/'),
   'the page\'s own host is asked first', repr(_found101))
report(len(_candidates(_endpoint_page, 'https://x.com/v', limit=2)) <= 2,
   'and the list is capped, so a noisy page costs a few requests, not dozens')

report(_from_payload([
    {'lang': 'en', 'label': 'English (auto)', 'url': 'https://cdn/a/en.vtt'},
    {'lang': 'fr', 'label': 'French', 'src': '/subs/fr.vtt', 'auto': True},
], 'https://cdn/a/list.json') == [
    {'url': 'https://cdn/a/en.vtt', 'lang': 'en', 'ext': 'vtt',
     'name': 'English (auto)', 'automatic': True},
    {'url': 'https://cdn/subs/fr.vtt', 'lang': 'fr', 'ext': 'vtt',
     'name': 'French', 'automatic': True}],
   'a flat track list is read, relative URLs resolved against the endpoint')
_keyed101 = _from_payload({
    'en': {'url': 'https://cdn/a/en.vtt', 'name': 'English'},
    'de': {'file': 'de.vtt', 'automatic': True},
}, 'https://cdn/a/list')
report([t['lang'] for t in _keyed101] == ['en', 'de'],
   'a payload keyed by language is read too, whatever shape the site chose')
report([t['automatic'] for t in _keyed101] == [False, True],
   'and a machine track is marked as one while an authored one is not')
_nested101 = _from_payload({'player': {'config': {'subtitleTracks': [
    {'language': 'ja', 'data': 'https://cdn/a/ja.vtt'},
    {'language': 'en', 'url': 'https://cdn/a/none.txt'}]}}}, '')
report([t['lang'] for t in _nested101] == ['ja'],
   'tracks buried in a player config are found, and a non-caption file is not')
report(_from_payload({'url': 'https://cdn/a/en.vtt'}, '') == [
    {'url': 'https://cdn/a/en.vtt', 'lang': 'en', 'ext': 'vtt',
     'name': '', 'automatic': False}],
   'a bare caption URL with no language beside it is still a track')
report(_from_payload({'a': 1}, '') == [] and _from_payload([1, 2], '') == []
       and _from_payload(None, '') == [],
   'a payload with no caption URL in it yields nothing rather than raising')
report(len(_from_payload([{'lang': 'en', 'url': 'https://cdn/a/en.vtt'}] * 3,
                         '')) == 1,
   'and a track listed three times is offered once')

# The two methods, against a stub that scripts what each endpoint answers.
_sub_meths = []
for _node101 in ast.walk(TREE):
    if isinstance(_node101, ast.ClassDef) and _node101.name == 'VideoPlayer':
        for _m101 in _node101.body:
            if isinstance(_m101, ast.FunctionDef) and _m101.name in (
                    '_subtitle_tracks_from_body',
                    '_extract_subtitle_tracks_from_endpoints'):
                _sub_meths.append(_m101)
report(len(_sub_meths) == 2, 'both endpoint methods are on VideoPlayer')


class _SubStub101(object):
    def __init__(self, bodies):
        self.bodies = dict(bodies)
        self.asked = []

    def _subtitle_endpoint_body(self, url, referer=''):
        self.asked.append(url)
        return self.bodies.get(url, '')

    def _preferred_remote_subtitle_tracks(self, tracks, limit=4):
        return list(tracks)[:max(1, int(limit or 1))]


for _m101 in _sub_meths:
    exec(compile(ast.Module(body=[_m101], type_ignores=[]), 'subm101', 'exec'),
         _g101)
_g101['_subtitle_endpoint_body'] = _SubStub101._subtitle_endpoint_body
_g101['_preferred_remote_subtitle_tracks'] = \
    _SubStub101._preferred_remote_subtitle_tracks
_SubStub101._subtitle_tracks_from_body = _g101['_subtitle_tracks_from_body']
_SubStub101._report_subtitle_page = lambda self, html, page_url: ''
import io as _io101
import contextlib as _ctx101


def _run101(page_html, page_url, bodies):
    stub = _SubStub101(bodies)
    buf = _io101.StringIO()
    with _ctx101.redirect_stdout(buf):
        out = _g101['_extract_subtitle_tracks_from_endpoints'](
            stub, page_html, page_url)
    return stub, out, buf.getvalue()


_stub101, _tracks101, _log101 = _run101(
    _endpoint_page, 'https://www.xvideos.com/video.upbutthfd81/title',
    {'https://www.xvideos.com/video-subtitles/upbutthfd81':
     '[{"lang":"en","url":"https://cdn/a/en.vtt"},'
     '{"lang":"es","url":"https://cdn/a/es.vtt","auto":true}]'})
report([t['lang'] for t in _tracks101] == ['en', 'es'],
   'the endpoint pass turns a site\'s subtitle list into real tracks',
   repr(_tracks101))
report(bool(_tracks101)
       and all(t['ext'] == 'vtt' for t in _tracks101),
   'each carrying the extension the player needs to load it')
report(_stub101.asked[:1] == [
    'https://www.xvideos.com/video-subtitles/upbutthfd81'],
   'the page\'s own endpoint is the one asked first')
report('[SUBS]' in _log101 and 'subtitle endpoint' in _log101
       and '2 track(s)' in _log101,
   'and the attempt is logged, so a site that still misses says why',
   repr(_log101))

_stub101, _tracks101, _log101 = _run101(_track_page, 'https://x.com/v/1', {})
report(_tracks101 == [] and _stub101.asked == [],
   'no endpoint in the page means nothing is fetched and nothing is offered')
report('no caption file and no subtitle endpoint' in _log101,
   'and that is said out loud rather than failing silently', repr(_log101))

_stub101, _tracks101, _log101 = _run101(
    _endpoint_page, 'https://www.xvideos.com/v/1',
    {'https://www.xvideos.com/video-subtitles/upbutthfd81': ''})
report(_tracks101 == [] and 'no response' in _log101,
   'an endpoint that answers nothing is reported, not swallowed',
   repr(_log101))

_body101fn = _g101['_subtitle_tracks_from_body']
_vtt101 = _body101fn(None, 'WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n',
                     'https://cdn/a/en.vtt')
report(len(_vtt101) == 1 and _vtt101[0]['lang'] == 'en'
       and _vtt101[0]['automatic'] is True,
   'an endpoint that is the caption file itself is taken as one track',
   repr(_vtt101))
report(_body101fn(None, '', 'https://cdn/a/en.vtt') == []
       and _body101fn(None, '<html>nope</html>', 'https://cdn/a/x') == []
       and _body101fn(None, '{"broken": ', 'https://cdn/a/x') == [],
   'and an empty, HTML or malformed answer yields no track instead of raising')

# ── 102. When a page's captions cannot be read, the page is written down ───────
# xvideos proves the feature exists -- a CC video's page carries a Subtitles
# menu of 25 languages and a non-CC video's page carries no such menu at all
# -- but the caption URLs are in markup that cannot be read from here. So the
# app keeps the subtitle-bearing lines of any page that yielded no track, in
# subtitles_debug.txt, which is what says where the files actually are.
report('def _subtitle_page_fragments(' in SRC
       and 'def _dump_subtitle_report(' in SRC
       and 'def _report_subtitle_page(' in SRC,
   'a page that yields no caption track is written down instead of dropped')
report(SRC.count("subtitles_debug.txt") >= 2,
   'and it lands in one named file, so it can just be sent over')
_pass102 = SRC[SRC.index('def _extract_subtitle_tracks_from_endpoints('):]
_pass102 = _pass102[:_pass102.index('\n    def ')]
report(_pass102.count('self._report_subtitle_page(html, page_url)') == 2,
   'both ways of finding nothing are reported -- no endpoint in the page, '
   'and an endpoint that returned no track',
   'found %d' % _pass102.count('self._report_subtitle_page(html, page_url)'))

_frag_fn = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
            and n.name == '_subtitle_page_fragments']
_frag_const = [n for n in TREE.body if isinstance(n, ast.Assign)
               and getattr(n.targets[0], 'id', '') == '_SUB_REPORT_KEYWORDS']
report(len(_frag_fn) == 1 and len(_frag_const) == 1,
   'the fragment picker is a module-level function')
from urllib.parse import (urlparse as _up102, urljoin as _uj102,
                          urlsplit as _us102b)
_g102 = {'os': os, 're': re, 'time': time, 'print': print, 'json': json,
         'urlparse': _up102, 'urljoin': _uj102, 'urlsplit': _us102b,
         'html_unescape': html_unescape}
_sub_all102 = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
               and n.name in ('_caption_ext_of', '_lang_from_url',
                              '_subtitle_endpoint_candidates',
                              '_subtitle_tracks_from_payload',
                              '_subtitle_page_fragments')]
_sub_const102 = [n for n in TREE.body if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], 'id', '').startswith(
                     ('_CAPTION_FILE_EXTS', '_SUB_'))]
exec(compile(ast.Module(body=_sub_const102 + _sub_all102, type_ignores=[]),
             'frag102', 'exec'), _g102)
_frags = _g102['_subtitle_page_fragments']

_cc_page = (
    '<html><head><title>x</title></head><body>\n'
    '<div class="videoPlayer">nothing to see</div>\n'
    '<li class="subtitle-option" data-lang="en">English</li>\n'
    '<li class="subtitle-option" data-lang="es">Espanol</li>\n'
    '<script>var x = 1;</script>\n'
    '</body></html>')
_got = _frags(_cc_page)
report(len(_got) == 2 and all('subtitle-option' in g for g in _got),
   'only the lines that talk about subtitles are kept', repr(_got))
report(_frags('<html><body><p>no media here</p></body></html>') == [],
   'a page with no subtitle markup yields nothing')
report(_frags('') == [] and _frags(None) == [], 'an empty page yields nothing')
_dupe = ('<li class="subtitle-option">English</li>\n'
         '<li class="subtitle-option">English</li>\n')
report(len(_frags(_dupe)) == 1, 'an identical line is kept once')
report(len(_frags('\n'.join('<i class="caption">%d</i>' % i
                            for i in range(50)), limit=5)) == 5,
   'and the list is capped, so a huge page stays a small file')
_long = ('<script>' + 'a' * 400 + ' setSubtitles("https://cdn/x/en.vtt") '
         + 'b' * 400 + '</script>')
_cut = _frags(_long)
report(len(_cut) == 1 and len(_cut[0]) <= 401 and 'setSubtitles' in _cut[0],
   'a minified line is cut down around the keyword rather than kept whole',
   '%d chars' % (len(_cut[0]) if _cut else -1))

_dump_meths = []
for _node102 in ast.walk(TREE):
    if isinstance(_node102, ast.ClassDef) and _node102.name == 'VideoPlayer':
        for _m102 in _node102.body:
            if isinstance(_m102, ast.FunctionDef) and _m102.name in (
                    '_dump_subtitle_report', '_report_subtitle_page',
                    '_extract_subtitle_tracks_from_endpoints',
                    '_subtitle_tracks_from_body'):
                _dump_meths.append(_m102)
report(len(_dump_meths) == 4, 'the report writer and the pass are both present',
       'found %d' % len(_dump_meths))


class _SubStub102(object):
    def __init__(self, bodies, data_dir):
        self.bodies = dict(bodies)
        self.asked = []
        self.data_dir = data_dir

    def _subtitle_endpoint_body(self, url, referer=''):
        self.asked.append(url)
        return self.bodies.get(url, '')

    def _preferred_remote_subtitle_tracks(self, tracks, limit=4):
        return list(tracks)[:max(1, int(limit or 1))]


for _m102 in _dump_meths:
    exec(compile(ast.Module(body=[_m102], type_ignores=[]), 'dump102', 'exec'),
         _g102)
_g102['_subtitle_endpoint_body'] = _SubStub102._subtitle_endpoint_body
_g102['_preferred_remote_subtitle_tracks'] = \
    _SubStub102._preferred_remote_subtitle_tracks
_SubStub102._subtitle_tracks_from_body = _g102['_subtitle_tracks_from_body']
_SubStub102._dump_subtitle_report = _g102['_dump_subtitle_report']
_SubStub102._report_subtitle_page = _g102['_report_subtitle_page']

import io as _io102
import contextlib as _ctx102
_dir102 = tempfile.mkdtemp(prefix='subs102_')
_stub102 = _SubStub102({}, _dir102)
_path102, _count102 = _g102['_dump_subtitle_report'](
    _stub102, _cc_page, 'https://www.xvideos.com/video.omkdvbufe4d/x')
report(_path102 == os.path.join(_dir102, 'subtitles_debug.txt')
       and os.path.exists(_path102),
   'the report is written next to the app\'s own data files', _path102)
report(_count102 == 2, 'and it says how many subtitle lines it kept')
_text102 = open(_path102, encoding='utf-8').read()
report('video.omkdvbufe4d' in _text102 and 'subtitle-option' in _text102,
   'carrying both the page it came from and the markup itself')
_g102['_dump_subtitle_report'](_stub102, _cc_page,
                               'https://xhamster.com/videos/second-xhb0btt')
_text102 = open(_path102, encoding='utf-8').read()
report('video.omkdvbufe4d' in _text102 and 'second-xhb0btt' in _text102,
   'a second page is appended, so one file covers a whole session')
shutil.rmtree(_dir102, ignore_errors=True)

# The whole pass, end to end, on a page that has captions but no readable URL.
_stub102 = _SubStub102({}, tempfile.mkdtemp(prefix='subs102b_'))
_buf102 = _io102.StringIO()
with _ctx102.redirect_stdout(_buf102):
    _out102 = _g102['_extract_subtitle_tracks_from_endpoints'](
        _stub102, _cc_page, 'https://www.xvideos.com/video.omkdvbufe4d/x')
_log102 = _buf102.getvalue()
report(_out102 == [], 'a page whose captions cannot be read yields no track')
report('subtitles_debug.txt' in _log102,
   'and the run says where the evidence went', repr(_log102))
report(os.path.exists(os.path.join(_stub102.data_dir, 'subtitles_debug.txt')),
   'the file is really there')
shutil.rmtree(_stub102.data_dir, ignore_errors=True)

# ── 103. The subtitle path says what it did ───────────────────────────────────
# Three runs of logs answered nothing because the path was silent on success
# and silent on a skipped 404 alike: "found the wrong file", "found nothing"
# and "never looked" all printed the same thing. Every step now speaks, and
# this runs the consumer that used to be mute.
report('caption detection build' in SRC,
   'the running main.py announces itself, so a log says which copy it is')
_resolve103 = SRC[SRC.index('def _resolve_stream_from_html('):]
_resolve103 = _resolve103[:_resolve103.index('\n    def ')]
report('page scan named' in _resolve103,
   'a caption file the page scan finds is named, not just counted')
_first103 = _resolve103.index('page scan named')
_host103 = _resolve103.index("page_host = (urlparse(page_url).netloc or '')")
report(_host103 < _first103,
   'and the host it is named under is worked out before the line that uses it')
report('_is_fileditch_host(page_host)' in _resolve103[_first103:],
   'moving that line up left the rest of the resolver intact')
report(_resolve103.count("page_host = (urlparse(page_url).netloc or '')") == 1,
   'and it is assigned exactly once, not twice')

_consumer103 = SRC[SRC.index('def _load_remote_subtitles_async('):]
_consumer103 = _consumer103[:_consumer103.index('\n    def ')]
for _needle103 in ('no usable caption track', 'fetching', 'skipped',
                   '-> saved ', 'could be fetched'):
    report(_needle103 in _consumer103,
           'the caption fetcher reports: ' + _needle103)

import io as _io103
import contextlib as _ctx103
_load_fn = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name == '_load_remote_subtitles_async']
report(len(_load_fn) == 1, 'the consumer is one method on VideoPlayer')
_g103 = {'print': print, 're': re, 'os': os, 'tempfile': tempfile}
exec(compile(ast.Module(body=_load_fn, type_ignores=[]), 'cons103', 'exec'),
     _g103)


class _ConsStub103(object):
    def __init__(self, subtitles=None):
        self.subtitles = subtitles
        self.submitted = []
        self.thread_pool = self
        self._remote_subtitle_pending = set()

    def submit(self, fn):
        self.submitted.append(fn)

    def _preferred_remote_subtitle_tracks(self, tracks, limit=4):
        return [t for t in (tracks or [])][:max(1, int(limit or 1))]


def _cons103(stream_info, subtitles=None):
    stub = _ConsStub103(subtitles)
    buf = _io103.StringIO()
    with _ctx103.redirect_stdout(buf):
        out = _g103['_load_remote_subtitles_async'](
            stub, 'https://xhamster.com/videos/xhb0btt', stream_info)
    return stub, out, buf.getvalue()


_stub103, _out103, _log103 = _cons103({})
report(_out103 is False and 'no usable caption track' in _log103
       and '(0 offered by the resolver)' in _log103,
   'a video with no caption at all says so, with how many it was offered',
   repr(_log103))
_stub103, _out103, _log103 = _cons103(
    {'subtitle_tracks': [{'url': 'https://cdn/a/x.txt', 'ext': 'txt'}]})
report(_out103 is False and '(1 offered by the resolver)' in _log103,
   'and a track in a format the player cannot read is reported as unusable, '
   'not dropped quietly', repr(_log103))
_stub103, _out103, _log103 = _cons103(
    {'subtitle_tracks': [{'url': 'https://cdn/a/en.vtt', 'ext': 'vtt',
                          'lang': 'en'}]})
report(_out103 is True and 'fetching 1 caption track(s)' in _log103,
   'a real track is announced before it is fetched', repr(_log103))
report(len(_stub103.submitted) == 1, 'and the fetch is queued exactly once')
_stub103, _out103, _log103 = _cons103(
    {'subtitle_tracks': [{'url': 'https://cdn/a/en.vtt', 'ext': 'vtt'}]},
    subtitles=[{'start': 0, 'end': 1, 'text': 'already here'}])
report(_out103 is False and 'no usable caption track' not in _log103
       and 'fetching' not in _log103,
   'a video whose captions are already loaded is left alone, silently -- '
   'that is not a failure and must not read like one', repr(_log103))

# ── 104. The captions were found all along; fetching them was refused ──────────
# A field log settled it: xhamster's page named six caption files, and every
# fetch came back HTTP 403 with 564 bytes of error page, because the request
# carried a bare user agent and no Referer. The same log showed the labels one
# entry off -- sw_es_1.vtt offered as "en" -- so even a successful fetch would
# have shown Spanish. Both are fixed here, against the URLs from that log.
_XH_VTT = ('https://thumb-v1.xhcdn.com/a/ALOB4UyeKwnvO9pkrOW4Mg/030/627/881/'
           'sw_es_1.vtt')
_XH_EN = ('https://thumb-v1.xhcdn.com/a/hvm1kQu2FMDvlNaaCaiWMA/030/627/881/'
          'sw_en_1.vtt')
_XH_TR = ('https://thumb-v1.xhcdn.com/a/ypkJ653vWC5tAX_3qpUWCQ/030/627/881/'
          'sw_tr_1.vtt')
report(_g101['_lang_from_url'](_XH_VTT) == 'es'
       and _g101['_lang_from_url'](_XH_EN) == 'en'
       and _g101['_lang_from_url'](_XH_TR) == 'tr',
   'a filename that carries a prefix and a language gives up the language, '
   'not the prefix',
   repr([_g101['_lang_from_url'](u) for u in (_XH_VTT, _XH_EN, _XH_TR)]))
report(_g101['_lang_from_url']('https://cdn/a/en.vtt') == 'en'
       and _g101['_lang_from_url']('https://cdn/a/s.vtt?lang=ja') == 'ja'
       and _g101['_lang_from_url']('https://cdn/embed/xyz/a.vtt') == '',
   'and the plainer shapes still read the same')

_pref_fn = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name == '_preferred_remote_subtitle_tracks']
report(len(_pref_fn) == 1, 'the track chooser is one method')
_g104 = {'print': print, 're': re, 'os': os, 'max': max, 'int': int,
         'str': str, 'list': list}
exec(compile(ast.Module(body=_pref_fn, type_ignores=[]), 'pref104', 'exec'),
     _g104)


class _Pref104(object):
    _preferred_remote_subtitle_tracks = _g104[
        '_preferred_remote_subtitle_tracks']


_mislabelled = [
    {'url': _XH_VTT, 'lang': 'en', 'ext': 'vtt', 'name': '',
     'automatic': False},
    {'url': _XH_EN, 'lang': 'English (auto-generated)', 'ext': 'vtt',
     'name': '', 'automatic': True},
    {'url': _XH_TR, 'lang': 'ar', 'ext': 'vtt', 'name': '',
     'automatic': False},
]
_picked_bad = _Pref104()._preferred_remote_subtitle_tracks(_mislabelled,
                                                           limit=1)
report(bool(_picked_bad) and _picked_bad[0]['url'] == _XH_VTT,
   'with the labels as the page scraped them, Spanish is the track offered '
   'as English -- which is the bug, reproduced',
   repr([t['url'][-12:] for t in _picked_bad]))
_correct = [
    {'url': _XH_VTT, 'lang': 'es', 'ext': 'vtt',
     'name': 'Espanol', 'automatic': False},
    {'url': _XH_EN, 'lang': 'en', 'ext': 'vtt',
     'name': 'English (auto-generated)', 'automatic': True},
    {'url': _XH_TR, 'lang': 'tr', 'ext': 'vtt', 'name': 'Turkce',
     'automatic': False},
]
_picked = _Pref104()._preferred_remote_subtitle_tracks(_correct, limit=2)
report([t['lang'] for t in _picked] == ['en', 'es'],
   'with the language read off the filename, English is offered first',
   repr([t['lang'] for t in _picked]))

# ── the fetch itself, run for real against a stubbed transport ──
_fetch_fn = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
             and n.name == '_fetch_caption_body']
report(len(_fetch_fn) == 1, 'caption files are fetched through one helper')
report('def _fetch_caption_body(' in SRC
       and "headers['Sec-Fetch-Dest'] = 'empty'" in SRC,
   'and that helper sends a browser-shaped request, not a bare user agent')
_worker104 = SRC[SRC.index('def _load_remote_subtitles_async('):]
_worker104 = _worker104[:_worker104.index('\n    def ')]
report('self._fetch_caption_body(sub_url, file_path)' in _worker104,
   'the worker fetches each caption with the watch page as its referer')
report('_stream_request_headers(file_path)' not in _worker104,
   'and no longer with the bare headers the CDN answered 403')

import sys as _sys104
import types as _types104
exec(compile(ast.Module(body=_fetch_fn, type_ignores=[]), 'fetch104', 'exec'),
     _g104)


class _Resp104(object):
    def __init__(self, content, status):
        self.content = content
        self.status_code = status
        self.ok = 200 <= status < 400
        self.text = content.decode('utf-8', 'replace')


class _FetchStub104(object):
    def _media_playback_headers(self, page_url=None, media_url=None,
                                extra=None):
        return {'User-Agent': 'stub-agent',
                'Referer': str(page_url or ''),
                'Origin': 'https://xhamster.com'}

    def _cookie_header_for(self, url):
        return ''

    def _refusal_snippet(self, response):
        return ''

    def _session_cookie_count(self, session):
        return 0

    def _warm_caption_session(self, session, page_url, headers):
        return 0

    def _fetch_caption_body_bare(self, url):
        return b'', 0


class _FakeRequests104(object):
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, dict(headers or {})))
        return self.resp


def _fetch104(resp):
    fake = _FakeRequests104(resp)
    mod = _types104.ModuleType('requests')
    mod.get = fake.get
    fake.cookies = []
    mod.Session = lambda: fake
    saved_req = _sys104.modules.get('requests')
    saved_cc = _sys104.modules.get('curl_cffi')
    _sys104.modules['requests'] = mod
    _sys104.modules['curl_cffi'] = None   # force the plain-requests branch
    try:
        out = _g104['_fetch_caption_body'](
            _FetchStub104(), _XH_EN, 'https://xhamster.com/videos/xhb0btt')
    finally:
        if saved_req is None:
            _sys104.modules.pop('requests', None)
        else:
            _sys104.modules['requests'] = saved_req
        if saved_cc is None:
            _sys104.modules.pop('curl_cffi', None)
        else:
            _sys104.modules['curl_cffi'] = saved_cc
    return out, fake.calls


_body104, _calls104 = _fetch104(_Resp104(b'WEBVTT\n\n00:00:01.000 --> x\nhi\n',
                                          200))
report(_body104 == (b'WEBVTT\n\n00:00:01.000 --> x\nhi\n', 200),
   'a caption the CDN accepts comes back as bytes the player can read',
   repr(_body104)[:60])
_h104 = _calls104[0][1] if _calls104 else {}
report(_h104.get('Referer') == 'https://xhamster.com/videos/xhb0btt',
   'carrying the watch page as Referer', repr(_h104.get('Referer')))
report(_h104.get('Origin') == 'https://xhamster.com',
   'and the site as Origin, which is what a bare user agent was missing')
report('text/vtt' in str(_h104.get('Accept', ''))
       and _h104.get('Sec-Fetch-Dest') == 'empty',
   'asking for a caption rather than a video',
   repr(_h104.get('Accept')))
_body104, _calls104 = _fetch104(_Resp104(b'<html>403 Forbidden</html>' * 20,
                                          403))
report(_body104 == (b'', 403),
   'and a refusal comes back as a status the caller can report, not silence',
   repr(_body104))

# ── 105. Send the cookies mpv sends, and print why a fetch is refused ──────────
# Referer and Origin did not clear the 403 -- the field log proved that. The
# video from the same CDN family plays, and the one request in this app that
# carries cookies.txt is mpv's. So the caption request now carries it too, and
# a refusal now prints the CDN's own reason instead of discarding it.
report('caption detection build' in SRC,
   'the running copy is identifiable from the log')
report('def _cookie_header_for(' in SRC and 'def _refusal_snippet(' in SRC,
   'the caption request can carry cookies, and a refusal has a reason')
_ck_fns = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
           and n.name in ('_cookie_header_for', '_refusal_snippet',
                          '_fetch_caption_body')]
report(len(_ck_fns) == 3, 'all three are on VideoPlayer', 'found %d' % len(_ck_fns))
_g105 = {'print': print, 're': re, 'os': os, 'urlparse': urlparse}
exec(compile(ast.Module(body=_ck_fns, type_ignores=[]), 'ck105', 'exec'), _g105)

_ck_dir = tempfile.mkdtemp(prefix='ck105_')
_ck_file = os.path.join(_ck_dir, 'cookies.txt')
with open(_ck_file, 'w', encoding='utf-8') as _fh:
    _fh.write('# Netscape HTTP Cookie File\n')
    _fh.write('.xhcdn.com\tTRUE\t/\tFALSE\t0\tsessionid\tabc123\n')
    _fh.write('.xhamster.com\tTRUE\t/\tFALSE\t0\tsitepref\tdark\n')


class _CkStub105(object):
    def _cookies_txt_paths(self, domains=None):
        return [_ck_file]

    def _media_playback_headers(self, page_url=None, media_url=None,
                                extra=None):
        return {'User-Agent': 'stub-agent', 'Referer': str(page_url or ''),
                'Origin': 'https://xhamster.com'}


_CkStub105._cookie_header_for = _g105['_cookie_header_for']
_CkStub105._refusal_snippet = _g105['_refusal_snippet']
_CkStub105._fetch_caption_body = _g105['_fetch_caption_body']
_CkStub105._session_cookie_count = lambda self, session: 0
_CkStub105._warm_caption_session = (
    lambda self, session, page_url, headers: 0)
_CkStub105._fetch_caption_body_bare = lambda self, url: (b'', 0)

_hdr105 = _CkStub105()._cookie_header_for(
    'https://thumb-v1.xhcdn.com/a/hvm1kQu2FMDvlNaaCaiWMA/030/627/881/sw_en_1.vtt')
report('sessionid=abc123' in _hdr105,
   'the cookie for the caption CDN is picked up out of cookies.txt',
   repr(_hdr105))
report('sitepref' not in _hdr105,
   'and a cookie for another site is not sent to this one')
report(_CkStub105()._cookie_header_for('not a url') == '',
   'a URL with no host yields no cookie header')

import sys as _sys105
import types as _types105


class _R105(object):
    def __init__(self, content, status):
        self.content = content
        self.status_code = status
        self.ok = 200 <= status < 400
        self.text = content.decode('utf-8', 'replace')


class _Fake105(object):
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, dict(headers or {})))
        return self.resp


def _ck_fetch105(resp):
    fake = _Fake105(resp)
    mod = _types105.ModuleType('requests')
    mod.get = fake.get
    fake.cookies = []
    mod.Session = lambda: fake
    saved = _sys105.modules.get('requests')
    saved_cc = _sys105.modules.get('curl_cffi')
    _sys105.modules['requests'] = mod
    _sys105.modules['curl_cffi'] = None
    buf = _io105.StringIO()
    try:
        with _ctx105.redirect_stdout(buf):
            out = _g105['_fetch_caption_body'](
                _CkStub105(), _XH_EN, 'https://xhamster.com/videos/xhb0btt')
    finally:
        if saved is None:
            _sys105.modules.pop('requests', None)
        else:
            _sys105.modules['requests'] = saved
        if saved_cc is None:
            _sys105.modules.pop('curl_cffi', None)
        else:
            _sys105.modules['curl_cffi'] = saved_cc
    return out, fake.calls, buf.getvalue()


import io as _io105
import contextlib as _ctx105
_out105, _calls105, _log105 = _ck_fetch105(
    _R105(b'<html><head><title>403 Forbidden</title></head>'
          b'<body>Referer denied by CDN edge</body></html>', 403))
report(_out105 == (b'', 403), 'a refusal still comes back as a status',
   repr(_out105))
_h105b = _calls105[0][1] if _calls105 else {}
report('sessionid=abc123' in str(_h105b.get('Cookie', '')),
   'and the caption request now goes out with the cookies mpv uses',
   repr(_h105b.get('Cookie')))
report('cookies=yes' in _log105, 'the log says cookies were attached',
   repr(_log105))
report('Referer denied by CDN edge' in _log105,
   'and it prints the reason the CDN gave, which is the thing that was being '
   'thrown away', repr(_log105))
_out105, _calls105, _log105 = _ck_fetch105(
    _R105(b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n', 200))
report(_out105[1] == 200 and _out105[0].startswith(b'WEBVTT'),
   'an accepted caption still comes back as bytes')
report('refused' not in _log105, 'and a success logs no refusal',
   repr(_log105))
shutil.rmtree(_ck_dir, ignore_errors=True)

# ── 106. The wheel over the candidates combo, and the session that fetches it ──
# Two things. Scrolling the linker's list with the pointer over the candidates
# combo changed the picked match, which renames a file to the wrong movie. And
# the caption CDN answered a bare nginx 403 to a request carrying no cookies,
# so the watch page is now asked for first, in the same session.
_MS_SRC = open('metadata_scraper.py', encoding='utf-8').read()
_MS_TREE = ast.parse(_MS_SRC)
_nwc106 = [n for n in _MS_TREE.body if isinstance(n, ast.ClassDef)
           and n.name == '_NoWheelComboBox']
report(len(_nwc106) == 1, 'the linker has a combo box the wheel cannot change')
report('combo = _NoWheelComboBox()' in _MS_SRC,
   'and the candidates combo is that one, not a plain QComboBox')
report('Qt.FocusPolicy.StrongFocus' in _MS_SRC,
   'kept clickable, so the wheel still works once it is chosen on purpose')
_nwc_body = (_MS_SRC.split('class _NoWheelComboBox')[1]
             .split('class _MovieResultCard')[0])
report('event.ignore()' in _nwc_body and 'installEventFilter' not in _nwc_body,
   'done by ignoring the event, not by swallowing it -- a filter that returns '
   'True would stop the list scrolling at all')


class _FakeComboBase106(object):
    def __init__(self, parent=None):
        self._focused = False
        self.base_wheel_calls = 0

    def hasFocus(self):
        return self._focused

    def wheelEvent(self, event):
        self.base_wheel_calls += 1


class _FakeWheel106(object):
    def __init__(self):
        self.ignored = False

    def ignore(self):
        self.ignored = True


_g106 = {'QComboBox': _FakeComboBase106}
exec(compile(ast.Module(body=_nwc106, type_ignores=[]), 'nwc106', 'exec'),
     _g106)
_c106 = _g106['_NoWheelComboBox']()
_e106 = _FakeWheel106()
_c106.wheelEvent(_e106)
report(_c106.base_wheel_calls == 0 and _e106.ignored is True,
   'scrolling over it without focus changes nothing and passes the wheel on',
   'base calls=%d ignored=%s' % (_c106.base_wheel_calls, _e106.ignored))
_c106._focused = True
_e106b = _FakeWheel106()
_c106.wheelEvent(_e106b)
report(_c106.base_wheel_calls == 1 and _e106b.ignored is False,
   'and with focus the wheel changes the value as it should')

# ── the caption session is warmed from the watch page first ──
_warm106 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name in ('_warm_caption_session', '_session_cookie_count',
                           '_fetch_caption_body')]
report(len(_warm106) == 3, 'the caption fetch warms its session first',
   'found %d' % len(_warm106))
_g106b = {'print': print, 're': re, 'os': os, 'urlparse': urlparse}
exec(compile(ast.Module(body=_warm106, type_ignores=[]), 'warm106', 'exec'),
     _g106b)


class _R106(object):
    def __init__(self, content, status):
        self.content = content
        self.status_code = status
        self.ok = 200 <= status < 400
        self.text = content.decode('utf-8', 'replace')


class _Rec106(object):
    """One session, recording the order it was asked for things."""

    def __init__(self, resp):
        self.resp = resp
        self.order = []
        self.cookies = ['ck1', 'ck2']

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.order.append(url)
        return self.resp


class _WarmStub106(object):
    _session_cookie_count = _g106b['_session_cookie_count']
    _warm_caption_session = _g106b['_warm_caption_session']

    def _media_playback_headers(self, page_url=None, media_url=None,
                                extra=None):
        return {'Referer': str(page_url or '')}

    def _cookie_header_for(self, url):
        return ''

    def _refusal_snippet(self, response):
        return ''

    def _fetch_caption_body_bare(self, url):
        return b'', 0


_PAGE106 = 'https://xhamster.com/videos/my-stepson-xhb0btt'
_rec106 = _Rec106(_R106(b'', 403))
_cnt106 = _WarmStub106()._warm_caption_session(_rec106, _PAGE106, {})
report(_cnt106 == 2 and _rec106.order == [_PAGE106],
   'warming asks for the watch page and reports the cookies it set',
   repr(_rec106.order))
report(_WarmStub106()._warm_caption_session(_Rec106(_R106(b'', 403)), '', {})
       == 0,
   'and with no page to warm from it does nothing')

import sys as _sys106
import types as _types106


def _warm_fetch106(resp):
    rec = _Rec106(resp)
    mod = _types106.ModuleType('requests')
    mod.Session = lambda: rec
    saved = _sys106.modules.get('requests')
    saved_cc = _sys106.modules.get('curl_cffi')
    _sys106.modules['requests'] = mod
    _sys106.modules['curl_cffi'] = None
    buf = _io105.StringIO()
    try:
        with _ctx105.redirect_stdout(buf):
            out = _g106b['_fetch_caption_body'](
                _WarmStub106(), _XH_EN, _PAGE106)
    finally:
        if saved is None:
            _sys106.modules.pop('requests', None)
        else:
            _sys106.modules['requests'] = saved
        if saved_cc is None:
            _sys106.modules.pop('curl_cffi', None)
        else:
            _sys106.modules['curl_cffi'] = saved_cc
    return out, rec.order, buf.getvalue()


_out106, _order106, _log106 = _warm_fetch106(_R106(b'<html>403</html>', 403))
report(_order106 == [_PAGE106, _XH_EN],
   'the watch page is fetched in the same session, before the caption',
   repr([u[:34] for u in _order106]))
report(_out106 == (b'', 403), 'a refusal still comes back as a status')
report('2 from the page' in _log106,
   'and the log says how many cookies the page yielded', repr(_log106))

# ── 107. A click-gated interstitial, and the one header set not yet tried ─────
# The nubiles-family scraper waited for its challenge page to clear on its
# own. The captured interstitial started its proof-of-work from a click
# handler, not on load, and a headless page never clicks -- so no length of
# waiting could ever have cleared it. And the caption CDN's 403 survived five
# cookies from the page, which closes the cookie theory; the one combination
# never tried is a request with no Referer at all.
_ck_fns107 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
              and n.name == '_click_through_challenge']
report(len(_ck_fns107) == 1, 'the scraper can click a challenge page')
_get107 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
           and n.name == 'get'
           and '_is_challenge_page' in (ast.get_source_segment(_MS_SRC, n)
                                        or '')]
report(bool(_get107), 'found the browser fetch that meets the challenge')
_gsrc107 = ast.get_source_segment(_MS_SRC, _get107[0]) if _get107 else ''
_c107i = _gsrc107.find('_click_through_challenge(')
_d107i = _gsrc107.find('deadline = time.time()')
report(-1 < _c107i < _d107i,
   'and it clicks before it starts waiting, not after giving up')
_grace107 = [n for n in _MS_TREE.body if isinstance(n, ast.Assign)
             and getattr(n.targets[0], 'id', '') == '_CHALLENGE_GRACE']
report(bool(_grace107) and _grace107[0].value.value >= 6,
   'with long enough to finish once it has been started',
   repr(_grace107[0].value.value if _grace107 else None))

_g107 = {'print': print}
exec(compile(ast.Module(body=_ck_fns107, type_ignores=[]), 'clk107', 'exec'),
     _g107)


class _FakeMouse107(object):
    def __init__(self, owner):
        self.owner = owner

    def click(self, x, y):
        self.owner.mouse_clicks.append((x, y))


class _FakePage107(object):
    def __init__(self, refuse_selectors=False):
        self.clicks = []
        self.mouse_clicks = []
        self.refuse = refuse_selectors
        self.mouse = _FakeMouse107(self)

    def click(self, selector, timeout=None, force=None):
        if self.refuse:
            raise RuntimeError('not clickable')
        self.clicks.append(selector)


class _ClkStub107(object):
    def __init__(self, page):
        self._page = page


_Click107 = _g107['_click_through_challenge']
_p107 = _FakePage107()
_b107 = _io105.StringIO()
with _ctx105.redirect_stdout(_b107):
    _Click107(_ClkStub107(_p107), 'https://nubiles-porn.com/video/gallery')
report(_p107.clicks == ['body'] and _p107.mouse_clicks == [],
   'a challenge page gets clicked, which is what starts it working',
   repr(_p107.clicks))
report('clicked the challenge page' in _b107.getvalue(),
   'and says so, so a run that never clicks is visible',
   repr(_b107.getvalue()[:70]))
_p107b = _FakePage107(refuse_selectors=True)
with _ctx105.redirect_stdout(_io105.StringIO()):
    _Click107(_ClkStub107(_p107b), 'https://momlover.com/video/gallery')
report(_p107b.mouse_clicks == [(640, 450)],
   'and if no element takes the click it clicks the middle of the page',
   repr(_p107b.mouse_clicks))

# ── the caption retry with no Referer ──
_bare107 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name in ('_fetch_caption_body', '_fetch_caption_body_bare',
                           '_warm_caption_session', '_session_cookie_count')]
report(len(_bare107) == 4, 'a refusal is retried once with no Referer',
   'found %d' % len(_bare107))
_g107b = {'print': print, 're': re, 'os': os, 'urlparse': urlparse}
exec(compile(ast.Module(body=_bare107, type_ignores=[]), 'bare107', 'exec'),
     _g107b)


class _BareStub107(object):
    _session_cookie_count = _g107b['_session_cookie_count']
    _warm_caption_session = _g107b['_warm_caption_session']
    _fetch_caption_body_bare = _g107b['_fetch_caption_body_bare']

    def _media_playback_headers(self, page_url=None, media_url=None,
                                extra=None):
        return {'Referer': str(page_url or ''), 'Origin': 'https://xhamster.com'}

    def _cookie_header_for(self, url):
        return ''

    def _refusal_snippet(self, response):
        return 'nginx 403'

    def _stream_user_agent(self):
        return 'stub-agent'


import sys as _sys107
import types as _types107


def _bare_run107(session_resp, bare_resp):
    rec = _Rec106(session_resp)
    calls = []

    def _get(url, headers=None, timeout=None, allow_redirects=True):
        calls.append((url, dict(headers or {})))
        return bare_resp

    mod = _types107.ModuleType('requests')
    mod.Session = lambda: rec
    mod.get = _get
    saved = _sys107.modules.get('requests')
    saved_cc = _sys107.modules.get('curl_cffi')
    _sys107.modules['requests'] = mod
    _sys107.modules['curl_cffi'] = None
    buf = _io105.StringIO()
    try:
        with _ctx105.redirect_stdout(buf):
            out = _g107b['_fetch_caption_body'](
                _BareStub107(), _XH_EN, _PAGE106)
    finally:
        if saved is None:
            _sys107.modules.pop('requests', None)
        else:
            _sys107.modules['requests'] = saved
        if saved_cc is None:
            _sys107.modules.pop('curl_cffi', None)
        else:
            _sys107.modules['curl_cffi'] = saved_cc
    return out, calls, buf.getvalue()


_out107, _calls107, _log107 = _bare_run107(
    _R106(b'<html>403</html>', 403),
    _R106(b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n', 200))
report(_out107[1] == 200 and _out107[0].startswith(b'WEBVTT'),
   'when a Referer is what the CDN objects to, the retry gets the caption',
   repr(_out107)[:60])
_h107c = _calls107[0][1] if _calls107 else {}
report('Referer' not in _h107c and 'Origin' not in _h107c,
   'sent with neither Referer nor Origin', repr(sorted(_h107c)))
report('no Referer' in _log107, 'and the log says which attempt worked',
   repr(_log107))
_out107, _calls107, _log107 = _bare_run107(
    _R106(b'<html>403</html>', 403), _R106(b'<html>403</html>', 403))
report(_out107 == (b'', 403),
   'and when both are refused the original 403 is what gets reported, '
   'not a 0 that looks like no answer at all', repr(_out107))
report('nginx 403' in _log107, 'with the CDN\'s own reason still attached',
   repr(_log107))

# -----------------------------------------------------------------------
# 108. The click works. A field log showed the challenge page starting to
#      navigate the moment it was clicked -- and then the fetch dying with
#      "Page.content: Unable to retrieve content because the page is
#      navigating and changing the content." The wait loop called content()
#      outside any guard, so the first exception escaped the loop and the
#      whole fetch. The click was the fix; the loop threw it away.
# -----------------------------------------------------------------------
_get108 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
           and n.name == 'get'
           and '_is_challenge_page' in (ast.get_source_segment(_MS_SRC, n)
                                        or '')]
report(bool(_get108), 'found the browser fetch that meets the challenge')

_g108 = {
    'print': print,
    'time': time,
    '_is_challenge_page': lambda html: 'CHALLENGE-MARKER' in (html or ''),
    '_CHALLENGE_GRACE': 8.0,
    '_save_storage_state': lambda *a, **k: None,
    '_click_through_challenge': _g107['_click_through_challenge'],
    'Optional': __import__('typing').Optional,
}
exec(compile(ast.Module(body=_get108, type_ignores=[]), 'nav108', 'exec'),
     _g108)

_CH108 = '<html><head><title>CHALLENGE-MARKER</title></head></html>'
_OK108 = ('<html><body><a href="/video/gallery/1">a scene</a></body></html>')
_NAV108 = ('Page.content: Unable to retrieve content because the page is '
           'navigating and changing the content.')


class _FakePage108(object):
    """A page that refuses content() while the click's navigation runs."""

    def __init__(self, raise_times):
        self.raise_times = raise_times
        self.content_calls = 0
        self.clicks = []
        self.states = []

    def goto(self, url, wait_until=None, timeout=None):
        pass

    def content(self):
        self.content_calls += 1
        if self.content_calls == 1:
            return _CH108
        if self.content_calls - 1 <= self.raise_times:
            raise RuntimeError(_NAV108)
        return _OK108

    def click(self, selector, timeout=None, force=None):
        self.clicks.append(selector)

    def wait_for_load_state(self, state, timeout=None):
        self.states.append(state)


class _NavStub108(object):
    _click_through_challenge = _g107['_click_through_challenge']

    def __init__(self, page):
        self._page = page
        self._context = None
        self._cookie_path = 'state.json'

    def _ensure_started(self):
        pass


def _nav_run108(raise_times):
    page = _FakePage108(raise_times)
    buf = _io105.StringIO()
    with _ctx105.redirect_stdout(buf):
        out = _g108['get'](_NavStub108(page),
                           'https://nubiles-porn.com/video/gallery')
    return out, page, buf.getvalue()


_out108, _pg108, _log108 = _nav_run108(2)
report(_out108 == _OK108,
   'a challenge that navigates after the click is waited for instead of '
   'being abandoned -- this is what turned four galleries into fetch errors',
   repr(_out108)[:70])
report(_pg108.clicks == ['body'] and _pg108.content_calls == 5,
   'and the read is retried until the navigation lands, not given up on -- '
   'one challenge read, two mid-navigation refusals, the page, then the '
   'settled re-read',
   'clicks=%r reads=%d' % (_pg108.clicks, _pg108.content_calls))
report('the challenge cleared after the click' in _log108,
   'and a run that got through says so, so a run that did not is visible',
   repr(_log108[:80]))
report('browser fetch error' not in _log108,
   'with no fetch error reported for a page that was only mid-navigation',
   repr(_log108[:80]))

class _StuckPage108(_FakePage108):
    """A gate that never lets go, whatever is done to it."""

    def content(self):
        self.content_calls += 1
        return _CH108


def _stuck_run108():
    page = _StuckPage108(0)
    buf = _io105.StringIO()
    with _ctx105.redirect_stdout(buf):
        out = _g108['get'](_NavStub108(page),
                           'https://nubiles-porn.com/video/gallery')
    return out, page, buf.getvalue()


_grace_saved108 = _g108['_CHALLENGE_GRACE']
_g108['_CHALLENGE_GRACE'] = 0.3
_out108, _pg108, _log108 = _stuck_run108()
_g108['_CHALLENGE_GRACE'] = _grace_saved108
report(_out108 == _CH108,
   'a page that never lands is still handed back rather than lost',
   repr(_out108)[:60])
report('browser still on the challenge page' in _log108
       and 'browser fetch error' not in _log108,
   'and it is reported as a challenge that did not clear, not as a crash',
   repr(_log108[:80]))

# The no-Referer retry has to say what happened either way. A retry that
# only logs on success leaves a field log that cannot distinguish "it was
# refused too" from "it never ran" -- and that log said neither.
_out107, _calls107, _log107 = _bare_run107(
    _R106(b'<html>403</html>', 403), _R106(b'<html>403</html>', 403))
report('retried with no Referer: HTTP 403' in _log107,
   'a refused no-Referer retry is logged, so the next field log can say '
   'the attempt was made instead of leaving it to guesswork',
   repr(_log107[-160:]))
_out107, _calls107, _log107 = _bare_run107(
    _R106(b'<html>403</html>', 403),
    _R106(b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n', 200))
report('retried with no Referer: HTTP 200 -- caption body arrived'
       in _log107,
   'and a retry that worked says that too', repr(_log107[-160:]))


# -----------------------------------------------------------------------
# 109. Closing the metadata linker used to stop the update. The dialog is
#      a local in _open_metadata_dialog, so once exec() returned the last
#      reference was gone and Python destroyed a QThread that was still
#      running -- the nubiles networks were abandoned part way through.
#      The run now belongs to the player, and nothing emits into a widget
#      that no longer exists.
# -----------------------------------------------------------------------
_det109 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
           and n.name == '_detach_running_scraper']
report(len(_det109) == 1, 'the linker can hand a running scrape on')
_done109 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
            and n.name == 'done'
            and '_detach_running_scraper' in (ast.get_source_segment(
                _MS_SRC, n) or '')]
_close109 = [n for n in ast.walk(_MS_TREE) if isinstance(n, ast.FunctionDef)
             and n.name == 'closeEvent'
             and '_detach_running_scraper' in (ast.get_source_segment(
                 _MS_SRC, n) or '')]
report(bool(_done109) and bool(_close109),
   'and both ways out of the dialog do it -- the X button and Escape both '
   'route through done(), which closeEvent alone would miss')

_g109 = {'print': print}
if _det109:
    exec(compile(ast.Module(body=_det109, type_ignores=[]), 'det109', 'exec'),
         _g109)
# A no-op stand-in when the method is absent, so every assertion below
# still runs against a revision that has it missing and fails on the
# behaviour instead of aborting the whole file on a KeyError.
_detach109 = _g109.get('_detach_running_scraper') or (lambda self: None)


class _Sig109(object):
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Scraper109(object):
    def __init__(self, running=True):
        self._running = running
        self.signals = _types107.SimpleNamespace(
            progress=_Sig109(), tick=_Sig109(),
            finished=_Sig109(), error=_Sig109())

    def isRunning(self):
        return self._running


class _Player109(object):
    pass


class _Dlg109(object):
    def __init__(self, scraper, player):
        self._scraper = scraper
        self.player = player
        self.site = {'name': 'nubiles'}
        self.seen = []

    def _log_msg(self, m):
        self.seen.append(('log', m))

    def _on_tick(self, d, t):
        self.seen.append(('tick', d, t))

    def _on_done(self, n):
        self.seen.append(('done', n))

    def _on_err(self, m):
        self.seen.append(('err', m))


_pl109 = _Player109()
_sc109 = _Scraper109()
_dg109 = _Dlg109(_sc109, _pl109)
_sc109.signals.progress.connect(_dg109._log_msg)
_sc109.signals.tick.connect(_dg109._on_tick)
_sc109.signals.finished.connect(_dg109._on_done)
_sc109.signals.error.connect(_dg109._on_err)

_b109 = _io105.StringIO()
with _ctx105.redirect_stdout(_b109):
    _detach109(_dg109)
report(_dg109._scraper is None,
   'closing the linker lets go of the scrape instead of taking it down')
report(getattr(_pl109, '_bg_scrapers', None) == [_sc109],
   'and the player holds the thread, which is the only thing keeping a '
   'parentless QThread alive once the dialog is gone',
   repr(getattr(_pl109, '_bg_scrapers', None)))
report('keeps running after the linker closed' in _b109.getvalue(),
   'and says so, so a run that was abandoned is not silent about it',
   repr(_b109.getvalue()[:70]))

with _ctx105.redirect_stdout(_io105.StringIO()):
    _sc109.signals.progress.emit('halfway through brattysis')
    _sc109.signals.tick.emit(4, 10)
report(_dg109.seen == [],
   'with the dialog\'s own slots let go first, so nothing emits into a '
   'widget that no longer exists', repr(_dg109.seen))

_b109 = _io105.StringIO()
with _ctx105.redirect_stdout(_b109):
    _sc109.signals.finished.emit(37)
report('kept running after the linker closed: 37 enriched'
       in _b109.getvalue(),
   'and the run still reports what it finished with',
   repr(_b109.getvalue()[:90]))
report(getattr(_pl109, '_bg_scrapers', None) == [],
   'and is released once it is done, so finished runs do not pile up',
   repr(getattr(_pl109, '_bg_scrapers', None)))

_b109 = _io105.StringIO()
with _ctx105.redirect_stdout(_b109):
    _detach109(_dg109)
report(getattr(_pl109, '_bg_scrapers', None) == [],
   'handing it on twice does nothing the second time')

_pl109b = _Player109()
_dg109b = _Dlg109(_Scraper109(running=False), _pl109b)
with _ctx105.redirect_stdout(_io105.StringIO()):
    _detach109(_dg109b)
report(not hasattr(_pl109b, '_bg_scrapers'),
   'and a scrape that already finished is not adopted as a background one')


# -----------------------------------------------------------------------
# 110. Every stream in a field log failed its first load with a TLS
#      certificate error and then played fine on the retry with the check
#      off -- three videos, three CDNs, three wasted loads. The failure was
#      recognised precisely and then forgotten, so the next video paid for
#      it again. The host is now remembered for the session.
# -----------------------------------------------------------------------
_note110 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name == 'note_tls_untrusted_host']
report(len(_note110) == 1, 'a CDN that refuses its certificate is remembered')

_g110 = {'print': print, 'urlparse': urlparse}
if _note110:
    exec(compile(ast.Module(body=_note110, type_ignores=[]), 'note110',
                 'exec'), _g110)
_note_fn110 = _g110.get('note_tls_untrusted_host') or (lambda self, url: False)


class _TlsStub110(object):
    def __init__(self, with_set=True):
        if with_set:
            self._tls_untrusted_hosts = set()


_st110 = _TlsStub110()
report(_note_fn110(_st110, 'https://video-nss-h.xhcdn.com/a/b/2160p.m3u8')
       and _st110._tls_untrusted_hosts == {'video-nss-h.xhcdn.com'},
   'by the host that actually served it, not the page it came from',
   repr(_st110._tls_untrusted_hosts))
_note_fn110(_st110, 'https://hls-cdn77.xvideos-cdn.com:8443/x/y.m3u8')
report('hls-cdn77.xvideos-cdn.com' in _st110._tls_untrusted_hosts
       and len(_st110._tls_untrusted_hosts) == 2,
   'with the port dropped, since the same CDN answers on more than one',
   repr(sorted(_st110._tls_untrusted_hosts)))
_note_fn110(_st110, 'https://user:pw@WatchPorn.to/get_file/5/x.mp4')
report('watchporn.to' in _st110._tls_untrusted_hosts,
   'and the credentials and case in the URL do not stop it matching',
   repr(sorted(_st110._tls_untrusted_hosts)))
_before110 = len(_st110._tls_untrusted_hosts)
for _bad110 in ('', None, 'not a url', 'file:///C:/x.mp4'):
    report(_note_fn110(_st110, _bad110) is False
           and len(_st110._tls_untrusted_hosts) == _before110,
       'and something with no host in it is refused rather than recorded '
       'as an empty string that would match nothing', repr(_bad110))
_note_fn110(_st110, 'https://video-nss-h.xhcdn.com/other.mp4')
report(len(_st110._tls_untrusted_hosts) == _before110,
   'and remembering the same CDN twice does not grow the list')

_st110b = _TlsStub110(with_set=False)
report(_note_fn110(_st110b, 'https://video5.xhcdn.com/k.mp4')
       and _st110b._tls_untrusted_hosts == {'video5.xhcdn.com'},
   'even on a player that was built before the set existed')

# Located by line number rather than by calling get_source_segment on
# every function in a 2.8 MB file, which took the whole suite ten minutes.
_e_off110 = SRC.find("'eporner' in _target_host")
report(_e_off110 > 0, 'found the one place that decides verification per '
   'stream')
_e_line110 = SRC.count('\n', 0, _e_off110) + 1
_enc110 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
           and n.lineno <= _e_line110 <= (n.end_lineno or n.lineno)]
_enc110.sort(key=lambda n: n.lineno)
_lsrc110 = (ast.get_source_segment(SRC, _enc110[-1]) if _enc110 else '')
report(bool(_lsrc110), 'and read the function that contains it',
   _enc110[-1].name if _enc110 else 'none')

_e110 = _lsrc110.find("'eporner' in _target_host")
_u110 = _lsrc110.find('_tls_untrusted_hosts')
report(-1 < _e110 < _u110,
   'and it is consulted before the load is attempted, not after it has '
   'already failed once')
report('_tls_verify = False' in _lsrc110[_u110:]
       and 'certificate check off for' in _lsrc110[_u110:],
   'which is what stops the wasted load and its retry, and says so')
_fail110 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name == '_handle_playback_load_failed']
_fsrc110 = (ast.get_source_segment(SRC, _fail110[0])
            if _fail110 else '')
_r110 = _fsrc110.find('if should_retry_tls_disabled:')
_n110 = _fsrc110.find('note_tls_untrusted_host(')
report(-1 < _r110 < _n110,
   'and it is recorded at the moment the certificate failure is '
   'recognised, so only hosts that genuinely failed are trusted less')
report('certificate verify failed' in _fsrc110
       and 'error:0a000086' in _fsrc110,
   'on the strength of the real mpv error, not a generic loading failed')


# -----------------------------------------------------------------------
# 111. Renaming links onto a video lost the mirrors that were already
#      there. _set_mirrors_for_primary deleted every overlapping group and
#      wrote only the list it was handed, so adding a third link to a video
#      that had two left it with the one being played and the newest, and
#      joining two videos that each had two mirrors gave three, not four.
#      Overlapping groups are folded together now -- except for a URL that
#      has been promoted to a playlist row of its own, which is how the
#      split and promote actions take a mirror away on purpose.
# -----------------------------------------------------------------------
_setm111_names = ('_set_mirrors_for_primary', '_unique_paths',
                  '_quality_variant_stem', '_drop_quality_variants',
                  '_eporner_quality_rank')
_setm111 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
            and n.name in _setm111_names]
report(len(_setm111) == len(_setm111_names),
   'found the mirror group writer and everything it calls',
   'found %d of %d' % (len(_setm111), len(_setm111_names)))

_g111 = {'print': print, 're': re, 'urlparse': urlparse}
if _setm111:
    exec(compile(ast.Module(body=_setm111, type_ignores=[]), 'setm111',
                 'exec'), _g111)


class _MirStub111(object):
    def __init__(self, groups=None, playlist=None):
        self._playlist_url_mirrors = dict(groups or {})
        self.playlist = list(playlist or [])

    def _mirror_path_key(self, path):
        return str(path or '').strip().lower().rstrip('/')

    def _is_jav_site_host(self, host):
        return 'supjav' in str(host or '')


if _g111.get('_set_mirrors_for_primary'):
    _MirStub111._set_mirrors_for_primary = _g111['_set_mirrors_for_primary']
    _MirStub111._unique_paths = _g111['_unique_paths']
    # .get() with a pass-through fallback, not a direct index: on a
    # revision that predates the helpers a KeyError here aborts the whole
    # file before the behaviour is ever asserted, which is a falsification
    # that proves nothing.
    _MirStub111._quality_variant_stem = (
        _g111.get('_quality_variant_stem') or (lambda self, path: ''))
    _MirStub111._drop_quality_variants = (
        _g111.get('_drop_quality_variants')
        or (lambda self, primary, paths: list(paths or [])))
    _MirStub111._eporner_quality_rank = staticmethod(
        _g111.get('_eporner_quality_rank') or (lambda url: -1))
else:
    _MirStub111._set_mirrors_for_primary = lambda self, p, m: None
    _MirStub111._unique_paths = lambda self, paths: list(paths or [])
    _MirStub111._quality_variant_stem = lambda self, path: ''
    _MirStub111._drop_quality_variants = (
        lambda self, primary, paths: list(paths or []))
    _MirStub111._eporner_quality_rank = staticmethod(lambda url: -1)

_A111 = 'https://cdn1.example.com/a.mp4'
_B111 = 'https://cdn2.example.com/b.mp4'
_C111 = 'https://cdn3.example.com/c.mp4'

# The reported bug: a third link renamed onto a video that already had one.
_m111 = _MirStub111({_A111: [_B111]}, playlist=[_A111])
_m111._set_mirrors_for_primary(_A111, [_C111])
report(set(_m111._playlist_url_mirrors.get(_A111, [])) == {_B111, _C111},
   'renaming another link onto a video keeps the mirrors it already had '
   'instead of leaving only the newest one',
   repr(_m111._playlist_url_mirrors.get(_A111)))

# Two videos with two links each joining up: four, not three.
_X111 = 'https://cdn1.example.com/x.mp4'
_Y111 = 'https://cdn2.example.com/y.mp4'
_Z111 = 'https://cdn3.example.com/z.mp4'
_W111 = 'https://cdn4.example.com/w.mp4'
_m111 = _MirStub111({_X111: [_Y111], _Z111: [_W111]}, playlist=[_Z111])
_m111._set_mirrors_for_primary(_Z111, [_X111])
_group111 = set(_m111._playlist_url_mirrors.get(_Z111, []))
report(_group111 == {_X111, _Y111, _W111},
   'and joining a video that has two links to one that has two links gives '
   'four, not the three it used to give', repr(sorted(_group111)))
report(len(_m111._playlist_url_mirrors) == 1,
   'with the absorbed group retired rather than left behind pointing at '
   'the same links', repr(sorted(_m111._playlist_url_mirrors)))

# An empty list is still how a group is cleared.
_m111 = _MirStub111({_A111: [_B111, _C111]}, playlist=[_A111, _B111, _C111])
_m111._set_mirrors_for_primary(_A111, [])
report(_A111 not in _m111._playlist_url_mirrors,
   'and passing nothing still clears the group, which is how mirrors are '
   'split out into playlist rows of their own',
   repr(_m111._playlist_url_mirrors))

# A mirror promoted to its own row must not be folded straight back in.
_m111 = _MirStub111({_A111: [_B111, _C111]}, playlist=[_A111, _B111])
_m111._set_mirrors_for_primary(_A111, [_C111])
report(set(_m111._playlist_url_mirrors.get(_A111, [])) == {_C111},
   'and a mirror that was just promoted to a visible row is not pulled '
   'back into the group, which would undo the split',
   repr(_m111._playlist_url_mirrors.get(_A111)))

_m111 = _MirStub111({_A111: [_B111]}, playlist=[_A111])
_m111._set_mirrors_for_primary(_A111, [_A111, _C111])
report(_A111 not in _m111._playlist_url_mirrors.get(_A111, []),
   'with a video never listed as a mirror of itself')

_J111 = 'https://supjav.com/watch/12345'
_m111 = _MirStub111({_J111: [_B111]}, playlist=[_A111])
_m111._set_mirrors_for_primary(_A111, [_C111, _J111])
report(all('supjav' not in p for p in
           _m111._playlist_url_mirrors.get(_A111, [])),
   'and a Jav aggregator page is still kept out of the mirrors, absorbed '
   'or not', repr(_m111._playlist_url_mirrors.get(_A111)))

_P111 = 'https://cdn9.example.com/p.mp4'
_Q111 = 'https://cdn9.example.com/q.mp4'
_m111 = _MirStub111({_A111: [_B111], _P111: [_Q111]}, playlist=[_A111])
_m111._set_mirrors_for_primary(_A111, [_C111])
report(_m111._playlist_url_mirrors.get(_P111) == [_Q111],
   'and a group with nothing in common is left alone',
   repr(_m111._playlist_url_mirrors.get(_P111)))


# -----------------------------------------------------------------------
# 112. eporner's bitrates were listed as mirrors. One scene came back as
#      five alternates -- 1080p, 720p, 480p, 360p, 240p -- which a mirror
#      menu is not for: choosing one changes nothing but the resolution.
#      They share the scene's numeric id and differ only by the suffix, so
#      the primary's own variants are dropped and a mirror list is left
#      holding genuine alternates only.
# -----------------------------------------------------------------------
_names112 = ('_quality_variant_stem', '_drop_quality_variants',
             '_set_mirrors_for_primary', '_unique_paths',
             '_eporner_quality_rank')
_fns112 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
           and n.name in _names112]
report(len(_fns112) == len(_names112),
   'found the quality-variant helpers beside the mirror group writer',
   'found %d of %d' % (len(_fns112), len(_names112)))

_g112 = {'print': print, 're': re, 'urlparse': urlparse}
if _fns112:
    exec(compile(ast.Module(body=_fns112, type_ignores=[]), 'qv112', 'exec'),
         _g112)


class _QvStub112(object):
    def __init__(self, groups=None, playlist=None):
        self._playlist_url_mirrors = dict(groups or {})
        self.playlist = list(playlist or [])

    def _mirror_path_key(self, path):
        return str(path or '').strip().lower().rstrip('/')

    def _is_jav_site_host(self, host):
        return 'supjav' in str(host or '')


for _n112 in ('_quality_variant_stem', '_drop_quality_variants',
              '_set_mirrors_for_primary', '_unique_paths'):
    setattr(_QvStub112, _n112,
            _g112.get(_n112) or (lambda self, *a: None))
# It is a @staticmethod in the app; binding it as a plain function here
# would hand it `self` in place of the url.
_QvStub112._eporner_quality_rank = staticmethod(
    _g112.get('_eporner_quality_rank') or (lambda url: -1))

_EP112 = ('https://vid-s9-n50-de-cdn.eporner.com/v3/NdCiNRb04e4EHifK6E9xHw/'
          '1790026695_197.146.181.231_798/16974704-1080p.mp4')
_Q112 = ['http://vid-s9-n50-de-cdn.eporner.com/16974704-%dp.mp4' % h
         for h in (720, 480, 360, 240)]
_GV112 = 'https://gvideo.eporner.com/zSjKrbmmZ8t.mp4'

_qv112 = _QvStub112()
report(_qv112._quality_variant_stem(_EP112)
       == _qv112._quality_variant_stem(_Q112[0])
       == '16974704.mp4@eporner.com',
   'the bitrates of one scene share an identity even though the site '
   'serves the top quality from a token path and the rest from the root',
   repr(_qv112._quality_variant_stem(_EP112)))
report(_qv112._quality_variant_stem(_GV112) == '',
   'and a link with no resolution in it is not claimed as a variant of '
   'anything', repr(_qv112._quality_variant_stem(_GV112)))

_b112 = _io105.StringIO()
with _ctx105.redirect_stdout(_b112):
    _kept112 = _qv112._drop_quality_variants(_EP112, _Q112 + [_GV112])
report(_kept112 == [_GV112],
   'so the four lower bitrates of the scene being played are not offered '
   'as alternates, and the one genuine mirror is', repr(_kept112))
report('other bitrates of the same video' in _b112.getvalue(),
   'and the log says links were dropped for that reason, so a missing '
   'mirror is not a mystery', repr(_b112.getvalue()[:80]))

# When nothing is being played at a known quality, the best variant has to
# survive or the video would be unreachable.
_b112 = _io105.StringIO()
_plain112 = 'https://www.eporner.com/video-16974704/some-title/'
with _ctx105.redirect_stdout(_b112):
    _kept112 = _qv112._drop_quality_variants(_plain112, _Q112)
report(_kept112 == [_Q112[0]],
   'and with no resolution on the primary the best of the set is kept '
   'rather than all of them being dropped', repr(_kept112))

_other112 = ['https://cdn.example.com/99999999-720p.mp4',
             'https://cdn.example.com/99999999-480p.mp4',
             'https://elsewhere.example.net/totally-different.mp4']
_b112 = _io105.StringIO()
with _ctx105.redirect_stdout(_b112):
    _kept112 = _qv112._drop_quality_variants(_EP112, _other112)
report(set(_kept112) == {'https://cdn.example.com/99999999-720p.mp4',
                         'https://elsewhere.example.net/totally-different.mp4'},
   'a different scene is left alone, and one link of each of its own '
   'bitrate sets survives', repr(_kept112))

# End to end, through the writer the rest of the app uses.
_m112 = _QvStub112({}, playlist=[_EP112])
_b112 = _io105.StringIO()
with _ctx105.redirect_stdout(_b112):
    _m112._set_mirrors_for_primary(_EP112, _Q112 + [_GV112])
report(_m112._playlist_url_mirrors.get(_EP112) == [_GV112],
   'so an eporner video ends up with its real mirrors, not five copies of '
   'itself', repr(_m112._playlist_url_mirrors.get(_EP112)))

_m112 = _QvStub112({}, playlist=[_EP112])
with _ctx105.redirect_stdout(_io105.StringIO()):
    _m112._set_mirrors_for_primary(_EP112, _Q112)
report(_EP112 not in _m112._playlist_url_mirrors,
   'and a video whose every alternate was a bitrate of itself is left with '
   'no mirror group at all, rather than an empty one',
   repr(_m112._playlist_url_mirrors))


# -----------------------------------------------------------------------
# 113. A video that stops part way through was silent about it. A host
#      that cuts a transfer off ends the HTTP response, mpv reports a clean
#      eof, and from the app's side that is exactly what the end of a video
#      looks like -- so playback stopped at a fifth of the way in with
#      nothing in the log to say it was not over.
# -----------------------------------------------------------------------
_tr113 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
          and n.name == '_report_truncated_stream']
report(len(_tr113) == 1, 'a stream that ends early is now named as such')

_g113 = {'print': print, 'urlparse': urlparse}
if _tr113:
    exec(compile(ast.Module(body=_tr113, type_ignores=[]), 'trunc113',
                 'exec'), _g113)
_report113 = (_g113.get('_report_truncated_stream')
              or (lambda self, reason: None))


class _Src113(object):
    def __init__(self, url):
        self._url = url

    def toString(self):
        return self._url


class _TruncStub113(object):
    _report_truncated_stream = _report113

    def __init__(self, position_ms, duration_ms,
                 url='https://s15.keep2share.cc/dl/abc123/video.mp4'):
        self._position_ms = position_ms
        self._duration_ms = duration_ms
        self._source = _Src113(url)
        self._last_truncated_end = None


_K2S113 = 'https://s15.keep2share.cc/dl/abc123/video.mp4'


def _trunc_run113(position_ms, duration_ms, url=_K2S113):
    stub = _TruncStub113(position_ms, duration_ms, url)
    buf = _io105.StringIO()
    with _ctx105.redirect_stdout(buf):
        stub._report_truncated_stream('eof')
    return stub, buf.getvalue()


# A 30-minute video that stops at 6 minutes.
_st113, _log113 = _trunc_run113(360000, 1800000)
report('TRUNCATED' in _log113 and 'stopped at 20%' in _log113,
   'a video that stops a fifth of the way in says so, with the percentage',
   repr(_log113[:110]))
report('1440s missing' in _log113,
   'and how much of it never arrived', repr(_log113[:140]))
report('keep2share.cc' in _log113,
   'and which host stopped sending, since that is what decides whether a '
   'data cap, an expiring link or a dropped connection is to blame',
   repr(_log113[:160]))
report('not the end of the video' in _log113,
   'in words that cannot be mistaken for a normal ending')
report((_st113._last_truncated_end or {}).get('percent') == 20
       and (_st113._last_truncated_end or {}).get('host')
       == 's15.keep2share.cc',
   'with the numbers kept for whatever tries to pick the stream back up',
   repr(_st113._last_truncated_end))

_st113, _log113 = _trunc_run113(1799000, 1800000)
report('TRUNCATED' not in _log113,
   'and a video that actually finished is not accused of being cut short',
   repr(_log113[:80]))
_st113, _log113 = _trunc_run113(1795000, 1800000)
report('TRUNCATED' not in _log113,
   'nor one that stopped a few seconds early, which is how real endings '
   'arrive once the last frame is short of the stated duration',
   repr(_log113[:80]))
_st113, _log113 = _trunc_run113(360000, 0)
report('TRUNCATED' not in _log113
       and _st113._last_truncated_end is None,
   'and with no duration there is nothing to compare, so nothing is claimed')
_st113, _log113 = _trunc_run113(0, 1800000)
report('TRUNCATED' not in _log113,
   'nor when nothing played at all, which is a load failure with its own '
   'report rather than a truncation')


# -----------------------------------------------------------------------
# 114. The charge of the audio device, in the top bar. Windows reports
#      every paired Bluetooth device that has a level, so the part that can
#      actually go wrong is picking the one that is in use -- showing the
#      number for earbuds sitting in their case instead of the headset on
#      your head would be worse than showing nothing.
# -----------------------------------------------------------------------
_bat114 = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
           and n.name == '_bluetooth_battery_levels']
report(len(_bat114) == 1, 'the player can ask Windows for a device charge')

# The module-level dict the probe reports through, seeded here because
# only the function is lifted, not the assignment beside it.
# The probe's two module-level companions come with it: the PowerShell
# script it runs, and the dict it reports through. Lifting the function on
# its own leaves it raising NameError the moment it is called.
_ps114 = [n for n in TREE.body if isinstance(n, ast.Assign)
          and getattr(n.targets[0], 'id', '') in
          ('_BATTERY_POWERSHELL', '_BATTERY_PROBE_STATE',
           '_GENERIC_AUDIO_NAMES')]
report(len(_ps114) == 3,
   'with the script it runs and the state it reports through',
   'found %d' % len(_ps114))
_g114 = {'print': print, 'os': os,
         '_BATTERY_PROBE_STATE': {'reported': False}}
if _ps114:
    exec(compile(ast.Module(body=_ps114, type_ignores=[]), 'batps114',
                 'exec'), _g114)
if _bat114:
    exec(compile(ast.Module(body=_bat114, type_ignores=[]), 'bat114', 'exec'),
         _g114)
_probe114 = _g114.get('_bluetooth_battery_levels') or (lambda: {})

report(_probe114() == {},
   'and on anything that is not Windows it says nothing rather than '
   'guessing, so the label simply never appears', repr(_probe114()))


class _Proc114(object):
    def __init__(self, stdout='', stderr=''):
        self.stdout = stdout
        self.stderr = stderr


import sys as _sys114
import types as _types114
import os as _os114


def _probe_run114(stdout, stderr='', exc=None):
    calls = {}

    def _run(cmd, **kwargs):
        calls['cmd'] = cmd
        calls['kwargs'] = kwargs
        if exc is not None:
            raise exc
        return _Proc114(stdout, stderr)

    mod = _types114.ModuleType('subprocess')
    mod.run = _run
    mod.CREATE_NO_WINDOW = 0x08000000
    saved = _sys114.modules.get('subprocess')
    saved_name = _os114.name
    _sys114.modules['subprocess'] = mod
    _g114['_BATTERY_PROBE_STATE']['reported'] = True
    _os114.name = 'nt'
    try:
        out = _probe114()
    finally:
        _os114.name = saved_name
        if saved is None:
            _sys114.modules.pop('subprocess', None)
        else:
            _sys114.modules['subprocess'] = saved
    return out, calls


_out114, _calls114 = _probe_run114(
    'WH-1000XM4\t78\nAirPods Pro\t45\r\n')
report(_out114 == {'wh-1000xm4': 78, 'airpods pro': 45},
   'Windows\' answer is parsed into charge per device, lowercased so it can '
   'be matched against the audio output name', repr(_out114))
# .get() rather than an index: a subscript on a dict that a failed run
# never filled aborts the whole file instead of recording a failure.
_cmd114 = _calls114.get('cmd') or ['']
_kw114 = _calls114.get('kwargs') or {}
report(str(_cmd114[0]).lower() == 'powershell'
       and '-NoProfile' in _cmd114,
   'through a shell that ships with Windows, so nothing has to be installed')
report(_kw114.get('startupinfo') is not None
       or _kw114.get('creationflags') is not None,
   'with its window suppressed -- a console flashing up once a minute would '
   'be its own bug')

_out114, _ = _probe_run114('garbage line\nNoBatteryHere\nBuds\t12\n')
report(_out114 == {'buds': 12},
   'and a line with no level in it is skipped rather than becoming a device')
_out114, _ = _probe_run114('', exc=OSError('no powershell'))
report(_out114 == {},
   'and a machine where the probe cannot run at all costs nothing but an '
   'empty label')

_apply114 = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
             and n.name == 'apply_audio_battery']
report(len(_apply114) == 1, 'the top bar can be told what to show')

# pyqtSlot comes with the source segment, so it has to exist here; a
# pass-through decorator is all the lifted function needs.
_g114b = {'print': print, 'json': json,
          '_GENERIC_AUDIO_NAMES': _g114.get('_GENERIC_AUDIO_NAMES')
          or frozenset(),
          'pyqtSlot': lambda *a, **k: (lambda f: f)}
if _apply114:
    exec(compile(ast.Module(body=_apply114, type_ignores=[]), 'apply114',
                 'exec'), _g114b)


class _Label114(object):
    def __init__(self):
        self.text = ''
        self.tip = ''
        self.style = ''
        self.shown = False

    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tip = value

    def setStyleSheet(self, value):
        self.style = value

    def setVisible(self, value):
        self.shown = bool(value)


class _BatStub114(object):
    apply_audio_battery = (_g114b.get('apply_audio_battery')
                           or (lambda self, payload: None))

    def __init__(self, device):
        self.battery_label = _Label114()
        self._device = device
        self.repositioned = 0

    def _current_audio_device_name(self):
        return self._device

    def _is_headphone_like_device(self, value):
        return any(k in str(value or '').lower() for k in
                   ('headphone', 'headset', 'earphone', 'earbud',
                    'airpod', 'buds'))

    def _is_app_fullscreen(self):
        return False

    def _reposition_hb_overlay(self):
        self.repositioned += 1


_st114 = _BatStub114('WH-1000XM4 (Stereo)')
_st114.apply_audio_battery(
    json.dumps({'wh-1000xm4': 78, 'linkbuds': 9}))
report(_st114.battery_label.text == '\U0001F50B 78%'
       and _st114.battery_label.shown,
   'the device that is actually selected is the one whose charge is shown, '
   'matched by name rather than by whichever came back first',
   repr(_st114.battery_label.text))
report(_st114.repositioned > 0,
   'and the top bar is re-laid out so it lands beside the subtitle tool')

_st114 = _BatStub114('Headphones (WH-1000XM4)')
_st114.apply_audio_battery(json.dumps({'headphones': 20, 'wh-1000xm4': 78}))
report(_st114.battery_label.text == '\U0001F50B 78%',
   'and the most specific name wins, so a generic Headphones entry cannot '
   'take the number away from the device it actually belongs to',
   repr(_st114.battery_label.text))

_st114 = _BatStub114('Speakers (Realtek Audio)')
_st114.apply_audio_battery(json.dumps({'airpods pro': 62}))
report(_st114.battery_label.text == '\U0001F50B 62%',
   'and when the selected output reports no charge at all, a headphone-like '
   'device is used rather than showing nothing',
   repr(_st114.battery_label.text))

_st114 = _BatStub114('Speakers (Realtek Audio)')
_st114.apply_audio_battery(json.dumps({'monitor audio': 55}))
report(not _st114.battery_label.shown,
   'and a device that is neither the selected output nor headphone-like is '
   'left alone, since its charge says nothing about what you are hearing')

_st114 = _BatStub114('Buds')
_st114.apply_audio_battery(json.dumps({'buds': 55}))
report(_st114.battery_label.shown, 'a charge is on screen to begin with')
_st114.apply_audio_battery('{}')
report(not _st114.battery_label.shown,
   'and an empty answer hides the label instead of leaving a stale charge '
   'on screen')

_st114 = _BatStub114('Buds')
_st114.apply_audio_battery(json.dumps({'buds': 140}))
report(_st114.battery_label.text == '\U0001F50B 100%',
   'with an out-of-range reading clamped rather than printed as it came')
_st114.apply_audio_battery(json.dumps({'buds': 10}))
report('#e05252' in _st114.battery_label.style,
   'and a nearly flat device is coloured as a warning',
   repr(_st114.battery_label.style[:60]))
report('buds' in _st114.battery_label.tip and '10%' in _st114.battery_label.tip,
   'with the device named in the tooltip, since the bar has room only for '
   'the number', repr(_st114.battery_label.tip))
_st114.apply_audio_battery('not json at all')
report(not _st114.battery_label.shown,
   'and an unparseable answer hides the label rather than raising on the '
   'GUI thread')


# -----------------------------------------------------------------------
# 115. The battery probe came back with "Windows reported 0 device(s)" and
#      nothing else, which cannot distinguish a headset Windows has no
#      charge for from a probe that never got as far as asking. It now
#      reports every step it took.
# -----------------------------------------------------------------------
_script115 = str(_g114.get('_BATTERY_POWERSHELL') or '')
# Used to assert three literal scope names. It was right when written and
# it caught this rewrite correctly, but the names it pinned were ones I
# had made up -- PnpObjectType has no 'AssociatedEndpoints'. What the
# assertion was actually for is the coverage, so that is what it tests
# now, and section 119 pins where the names come from.
report('foreach ($kind in $scopes)' in _script115
       and '$scopes += ' in _script115,
   'the probe covers every device scope rather than betting on one, '
   'since the charge is not exposed the same way in each')
report(_script115.count('Say ') >= 10 and "'SilentlyContinue'" not in _script115,
   'and it no longer swallows its own errors, which is what left the last '
   'field log with a bare zero and no reason')

_out115, _ = _probe_run114(
    '#diag winrt=loaded\n'
    '#diag AssociatedEndpoints seen=7 withCharge=1\n'
    'itel T1Neo\t64\n')
report(_out115 == {'itel t1neo': 64},
   "the probe's own progress lines are kept out of the device list",
   repr(_out115))


def _probe_loud115(stdout):
    def _run(cmd, **kwargs):
        return _Proc114(stdout, '')

    mod = _types114.ModuleType('subprocess')
    mod.run = _run
    mod.CREATE_NO_WINDOW = 0x08000000
    saved = _sys114.modules.get('subprocess')
    saved_name = _os114.name
    _sys114.modules['subprocess'] = mod
    _g114['_BATTERY_PROBE_STATE']['reported'] = False
    _os114.name = 'nt'
    buf = _io105.StringIO()
    try:
        with _ctx105.redirect_stdout(buf):
            _probe114()
    finally:
        _os114.name = saved_name
        _g114['_BATTERY_PROBE_STATE']['reported'] = True
        if saved is None:
            _sys114.modules.pop('subprocess', None)
        else:
            _sys114.modules['subprocess'] = saved
    return buf.getvalue()


_log115 = _probe_loud115(
    '#diag winrt=loaded\n'
    '#diag astask=ok\n'
    '#diag AssociatedEndpoints seen=7 withCharge=0\n'
    '#diag AssociatedEndpoints noCharge=itel T1Neo Stereo; Speakers\n')
report('withCharge=0' in _log115 and 'itel T1Neo Stereo' in _log115,
   'and the first probe prints how far it got and what it saw, so a headset '
   'Windows has no charge for is told apart from a probe that never asked',
   repr(_log115[:170]))
_log115 = _probe_loud115('#diag await=timeout\n')
report('await=timeout' in _log115,
   'including a WinRT call that hung, which would otherwise look exactly '
   'like a machine with no Bluetooth at all', repr(_log115[:100]))


# -----------------------------------------------------------------------
# 116. The probe reported nothing at all, on a machine with a headset
#      connected, because PnpObject.FindAllAsync has no two-argument
#      overload: the form that takes a property list also takes an AQS
#      filter. All three scopes failed identically and said so in the
#      user's own language. Pinned here because nothing in Python can
#      catch an argument count in a script it only ships as text.
# -----------------------------------------------------------------------
# Calling the method by name is gone entirely: PowerShell's overload
# binder rejected both the two- and the three-argument form of a method
# whose three-argument form certainly exists, so the signature is looked
# up by reflection and invoked directly instead of being left to it.
report(_script115.count('FindAllAsync(') == 0
       and "$mi.Invoke" in _script115
       and "GetParameters().Count -eq 3" in _script115,
   'the enumeration goes through reflection on the three-argument '
   'signature rather than a call PowerShell cannot resolve',
   'direct calls=%d, reflection=%s' % (
       _script115.count('FindAllAsync('), "$mi.Invoke" in _script115))
report("$props, ''" in _script115
       and 'List[string]' in _script115,
   'with the property list and the empty filter it needs, as a type the '
   'projected signature will actually accept')
report("'overloads='" in _script115,
   'and it prints which overloads it can see before trying any of them, '
   'so a binder that disagrees with the API says so on the first run '
   'instead of costing two')
report('OutputEncoding' in _script115
       and "encoding='utf-8'" in SRC,
   'with the console set to UTF-8 and read back as UTF-8 -- setting only '
   'one of the two is what turned the last message into mojibake')


# -----------------------------------------------------------------------
# 117. PowerShell 5.1 does not resolve WinRT bracket syntax until the
#      projection has been registered once, in assembly-qualified form.
#      The field log named the symptom exactly -- "Type [Windows.Devices.
#      Enumeration.Pnp.PnpObject] introuvable" -- and it arrived only
#      because the previous build finally printed the reason instead of
#      swallowing it. Three builds were spent on the method call when the
#      type itself had never resolved.
# -----------------------------------------------------------------------
_reg117 = _script115.find('ContentType = WindowsRuntime')
_lookup117 = _script115.find('$pnType = [Windows.Devices.Enumeration.Pnp.PnpObject]')
report(_reg117 != -1 and _lookup117 != -1 and _reg117 < _lookup117,
   'the WinRT projection is registered in assembly-qualified form before '
   'the type is looked up, which is the only reason that lookup can '
   'succeed at all',
   'register@%d lookup@%d' % (_reg117, _lookup117))
report("'projection=" in _script115,
   'and the registration reports its own outcome, so a machine where it '
   'still fails says so instead of failing the lookup two lines later '
   'for an unrelated-looking reason')


# -----------------------------------------------------------------------
# 118. GoFile's CDN answers a ranged request with HTTP 200 and the whole
#      file. The proxy treated that as a retryable error: twelve backoffs
#      over ~100s, a 502, then the player re-resolved and did it all
#      again -- six playlist entries and two live proxies in the field
#      log, and the video never played. A 200 to a Range is an answer,
#      not a failure; the honest response is to stream it through and
#      say plainly that seeking is not available.
# -----------------------------------------------------------------------
import io as _io118
import time as _timemod118
import threading as _threading118
import urllib.request as _urlreq118
from urllib.parse import urlparse as _urlparse118

_fn118 = None
for _node118 in ast.walk(TREE):
    if isinstance(_node118, ast.FunctionDef) and _node118.name == '_serve_gofile_resilient':
        _fn118 = _node118
        break
_serve118 = ast.get_source_segment(SRC, _fn118) if _fn118 is not None else ''
report(bool(_serve118), 'the gofile streaming closure was located to run against')

_log118 = []


def _print118(*a, **k):
    _log118.append(' '.join(str(x) for x in a))


class _Time118:
    """Real clock, no waiting: the retry backoff must not slow the suite."""
    @staticmethod
    def time():
        return _timemod118.time()

    @staticmethod
    def sleep(_s):
        return None


class _Owner118:
    pass


class _Resp118:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = dict(headers or {})

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body)
        out = self._body[:n]
        self._body = self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Handler118:
    def __init__(self, headers):
        self.headers = dict(headers)
        self.code = None
        self.hdrs = {}
        self.wfile = _io118.BytesIO()
        self.error = None

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass

    def send_error(self, code, msg=''):
        self.error = (code, str(msg))


_body118 = [b'']
_mode118 = {'mode': 'ignore-range'}
_calls118 = []


def _urlopen118(request, timeout=None, context=None):
    _calls118.append(dict(getattr(request, 'headers', None) or {}))
    if _mode118['mode'] == 'boom':
        raise IOError('upstream gone')
    return _Resp118(_body118[0], 200,
                    {'Content-Type': 'video/mp4',
                     'Content-Length': str(len(_body118[0]))})


_ns118 = {'re': re, 'urlparse': _urlparse118, 'time': _Time118,
          'threading': _threading118, 'print': _print118,
          'owner': _Owner118()}
exec(_serve118, _ns118)
_serve = _ns118.get('_serve_gofile_resilient')
_real_urlopen118 = _urlreq118.urlopen
_urlreq118.urlopen = _urlopen118
try:
    # --- A: upstream ignores Range -> stream it through, do not retry.
    _body118[0] = b'X' * 1000
    _mode118['mode'] = 'ignore-range'
    del _log118[:]
    del _calls118[:]
    _hA = _Handler118({'Range': 'bytes=0-'})
    _okA = _serve(_hA, 'https://store-eu-par-5.gofile.io/download/web/x.mp4', {})
    report(_okA is True and _hA.error is None and _hA.code == 200,
       'a CDN that ignores Range is served rather than refused',
       'ok=%s code=%s error=%s' % (_okA, _hA.code, _hA.error))
    report(_hA.wfile.getvalue() == b'X' * 1000,
       'and the whole body reaches the player, which is what it was '
       'asking for',
       '%d byte(s)' % len(_hA.wfile.getvalue()))
    report(_hA.hdrs.get('Accept-Ranges') == 'none',
       'without advertising seek -- promising ranges on a CDN that '
       'ignores them is what started the retry storm',
       'Accept-Ranges=%s' % _hA.hdrs.get('Accept-Ranges'))
    report(len(_calls118) == 2,
       'at the cost of two requests instead of thirteen',
       '%d upstream call(s)' % len(_calls118))
    report(not any('CHUNK_RETRY' in _l for _l in _log118),
       'with no backoff loop behind it',
       '%d retry line(s)' % sum('CHUNK_RETRY' in _l for _l in _log118))
    report(any('NO_RANGE' in _l for _l in _log118)
           and any('PASSTHROUGH' in _l for _l in _log118),
       'and the log says why it fell back, plus how many bytes came '
       'through, so a failure here is never silent again')

    # --- B: a mid-file start still has to line up with what is sent.
    del _log118[:]
    del _calls118[:]
    _hB = _Handler118({'Range': 'bytes=100-'})
    _serve(_hB, 'https://store-eu-par-5.gofile.io/download/web/x.mp4', {})
    report(_hB.wfile.getvalue() == b'X' * 900
           and _hB.hdrs.get('Content-Length') == '900',
       'a start partway into the file skips those bytes upstream and '
       'reports the length it actually sends',
       '%d byte(s), Content-Length=%s' % (len(_hB.wfile.getvalue()),
                                          _hB.hdrs.get('Content-Length')))

    # --- C: a genuine upstream failure must still give up honestly.
    _mode118['mode'] = 'boom'
    del _log118[:]
    del _calls118[:]
    _hC = _Handler118({'Range': 'bytes=0-'})
    _serve(_hC, 'https://store-eu-par-5.gofile.io/download/web/x.mp4', {})
    report(_hC.error is not None and _hC.error[0] == 502
           and len(_calls118) == 13,
       'while a real failure still exhausts the retry ladder and returns '
       '502 -- only the ignored-Range case was reclassified',
       'error=%s calls=%d' % (_hC.error, len(_calls118)))
finally:
    _urlreq118.urlopen = _real_urlopen118

report('range ignored (HTTP 200) for chunk fetch' not in SRC,
   'the error text that made a working response look like a broken one '
   'is gone from the source')


# -----------------------------------------------------------------------
# 119. The probe reached the API at last and the field log named the next
#      wall: "La valeur demandee 'AssociatedEndpoints' est introuvable."
#      PnpObjectType has no such member. The members are singular --
#      AssociationEndpoint, Device, DeviceInterface -- and the list of
#      three had been written from memory, not from the enum. Rather than
#      substitute a second guessed list, the scopes are now read off the
#      enum itself, so a name cannot be wrong again.
# -----------------------------------------------------------------------
_bad119 = [_w for _w in ("'AssociatedEndpoints'", "'Devices'", "'DeviceInterfaces'")
           if _w in _script115]
report(not _bad119,
   'the invented scope names are gone from the script',
   'still present: %s' % (_bad119 or 'none'))
report('[Enum]::GetNames(' in _script115 and 'foreach ($kind in $scopes)' in _script115,
   'the scopes are read off PnpObjectType instead of being spelled out, '
   'so the list cannot drift from the enum it enumerates',
   'GetNames=%s' % ('[Enum]::GetNames(' in _script115))
_p119 = [_script115.find(_w) for _w in ("'AssociationEndpoint'", "'Device'",
                                        "'DeviceInterface'")]
report(-1 not in _p119 and _p119 == sorted(_p119),
   'and AssociationEndpoint is asked first, that being the scope where a '
   'paired peripheral reports System.Devices.Aep.Battery.LevelPercent',
   'offsets=%s' % _p119)
report("-ne 'Unknown'" in _script115,
   'skipping Unknown, which the enum documents as unused')
report("'enumnames='" in _script115,
   'printing every name the enum actually has, so the next disagreement '
   'is answered on the first run rather than the fourth')


print('FAILURES:', FAILS)
raise SystemExit(1 if FAILS else 0)
