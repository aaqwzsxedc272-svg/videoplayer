"""
generic_jav_integration.py
──────────────────────────
Monkey-patches VideoPlayer so that pasting generic JAV URLs triggers the
static HTML grabber (generic_jav_grab.py), then adds the video as a single
playlist item whose title comes from the page and whose extra streams are
registered as mirrors.

HOW TO ACTIVATE
───────────────
Add one line at the very bottom of main.py (after the VideoPlayer class is
fully defined but before   if __name__ == '__main__':   ):

    import generic_jav_integration   # noqa
"""

import os
import re
import sys
import time
import threading
import json
import tempfile
import subprocess
from urllib.parse import urlparse, urljoin

from PyQt6.QtCore import pyqtSignal, pyqtSlot


# ── Utilities ─────────────────────────────────────────────────────────────────

def _is_generic_jav_url(url: str) -> bool:
    """Return True if *url* is a target generic video page."""
    try:
        parsed = urlparse(str(url or '').strip())
        host = (parsed.netloc or '').lower().lstrip('www.')
        # Target domains
        targets = [
            'javsubbed.net',
            'javenglish.cc',
            'javflix.cc',
            'javx.cc',
            'javx.org',
            'javhdporn.net',
            'jable.tv',
            'sextb.net',
            'javgg.net',
            'javdock.com',
        ]
        for t in targets:
            if t in host:
                return True
        return False
    except Exception:
        return False


def _generic_jav_grab_script_path() -> str:
    """Resolve the path to generic_jav_grab.py sitting next to main.py."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generic_jav_grab.py')


def _fetch_page_title(url):
    """Best-effort static title for the playlist row (og:title/<title>),
    falling back to the URL slug when the page can't be fetched."""
    try:
        import curl_cffi.requests as cfreq
        r = cfreq.get(
            url, impersonate='chrome131', timeout=12,
            headers={'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'},
        )
        if getattr(r, 'status_code', 0) == 200:
            html = r.text or ''
            m = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE)
            if not m:
                m = re.search(r'<title[^>]*>(.*?)</title>', html,
                              re.IGNORECASE | re.DOTALL)
            if m:
                from html import unescape as _unesc
                t = _unesc(re.sub(r'<[^>]+>', '', m.group(1))).strip()
                if t:
                    return t
    except Exception:
        pass
    try:
        slug = [p for p in urlparse(url).path.split('/') if p][-1]
        return slug or 'Video'
    except Exception:
        return 'Video'


# ── Main-Brave cache capture ───────────────────────────────────────────────────
#
# When the automated capture window can't get the site's player to stream
# ("sorry video streaming unavailable"), open the page in the user's REAL
# main Brave (their profile, session, cookies, shields - nothing automated)
# and watch Brave's disk cache: as soon as the user clicks play there, the
# streamhls.click m3u8 URLs land in the cache and we pick them up.

# Any HLS manifest URL with an optional signed query, restricted to RFC
# 3986 URL characters so binary junk between two cache/netlog URLs can
# never bridge them into one broken match (the old charset-anything regex
# did exactly that). javdock serves SOME videos from streamhls.click and
# others from tapecontent/streamtape CDNs - a streamhls-only pattern
# missed those entirely (field: gvg-879 was tapecontent).
_M3U8_URL_CHARS = rb"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_ANY_M3U8_RE = re.compile(
    rb'https?://' + _M3U8_URL_CHARS + rb'+\.m3u8(?:\?' + _M3U8_URL_CHARS + rb'*)?')


def _iter_m3u8_urls(blob):
    """Yield m3u8 URLs from raw bytes. When several URLs ran together
    with no delimiter at all (so the greedy pattern swallowed them into
    one match), split the run back apart and keep each piece that ends
    in .m3u8."""
    for m in _ANY_M3U8_RE.finditer(blob):
        frag = m.group(0)
        if frag.count(b'https://') + frag.count(b'http://') > 1:
            for piece in re.split(rb'(?=https?://)', frag):
                if piece and re.search(rb'\.m3u8(\?|$)', piece, re.IGNORECASE):
                    yield piece
        else:
            yield frag


def _running_brave_path():
    """Path of the Brave binary the user is ACTUALLY running right now.

    The user may have several Chromium installs; launching a different
    one opens a different profile (no session, no Cloudflare clearance)
    and the site refuses to stream. Asking the running process for its
    executable path guarantees we launch the exact same install."""
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '(Get-Process brave -ErrorAction SilentlyContinue | '
             'Select-Object -First 1).Path'],
            capture_output=True, text=True, timeout=10,
        )
        p = str(out.stdout or '').strip().strip('"').strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return ''


def _scan_netlog_for_m3u8(netlog_path, cursor=0):
    """Incrementally scan a Chromium net-log for ANY m3u8 URL.

    --log-net-log records EVERY network request URL - including the
    no-store HLS manifests that never reach the disk cache (why the
    cache scan found nothing while FetchV, watching live requests,
    captured the link). Returns (urls, new_cursor); re-reads a 2 KiB
    overlap so URLs split across chunks still match."""
    found = []
    try:
        start = max(0, cursor - 2048)
        with open(netlog_path, 'rb') as f:
            f.seek(start)
            blob = f.read()
        new_cursor = start + len(blob)
        if blob:
            for frag in _iter_m3u8_urls(blob):
                u = frag.decode('utf-8', 'replace')
                if u not in found:
                    found.append(u)
    except Exception:
        return [], cursor
    return found, new_cursor


def _ensure_brave_netlog(self, brave, source_url):
    """Ensure the user's Brave runs with a net-log we can tail.

    Chromium 136+ blocks the DevTools debug port on the DEFAULT profile
    (so no auto-click/network capture via CDP in the user's main
    browser), but --log-net-log is a plain diagnostic flag that still
    works there and records every request URL. Brave must be STARTED
    with the flag: if it is already running without it, ask the user to
    close it and wait. Returns the log path, or '' when unavailable."""
    existing = getattr(self, '_brave_netlog_path', '')
    proc = getattr(self, '_brave_netlog_proc', None)
    if (existing and os.path.isfile(existing)
            and proc is not None and proc.poll() is None):
        return existing  # our net-log instance from an earlier capture
    # Tidy older logs (locked files fail silently)
    try:
        import glob as _glob
        for old in _glob.glob(os.path.join(tempfile.gettempdir(), 'agpl_brave_netlog_*.json')):
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass
    if _running_brave_path():
        try:
            self.generic_jav_osd.emit(
                'One-time: close Brave completely so the app can capture its streams (waiting up to 90s)…',
                9000)
        except Exception:
            pass
        deadline = time.time() + 90
        while time.time() < deadline:
            if not _running_brave_path():
                break
            time.sleep(2)
        else:
            print('[GENERIC_JAV] Brave is still running - cannot enable the network log')
            return ''
    try:
        parsed = urlparse(source_url)
        site_root = f"{parsed.scheme or 'https'}://{parsed.netloc}/"
    except Exception:
        site_root = 'https://www.javdock.com/'
    netlog = os.path.join(tempfile.gettempdir(), f'agpl_brave_netlog_{int(time.time())}.json')
    try:
        proc = subprocess.Popen([brave, f'--log-net-log={netlog}',
                                 '--new-window', site_root])
    except Exception as exc:
        print(f'[GENERIC_JAV] main-Brave launch failed: {exc}')
        return ''
    self._brave_netlog_path = netlog
    self._brave_netlog_proc = proc
    time.sleep(4)
    print(f'[GENERIC_JAV] Brave network log: {netlog}')
    return netlog


def _looks_like_m3u8(u):
    """True for URLs that END in .m3u8 (optionally with a signed query) -
    excludes .m3u8.ts segment files, thumbnails, embed pages…"""
    return bool(re.search(r'\.m3u8(\?|$)', str(u or ''), re.IGNORECASE))


# Ad networks that serve their own (short) HLS playlists; a duration
# probe alone would already rank them below the movie, but they are
# filtered up front so they never pollute the mirror list.
_AD_M3U8_MARKERS = (
    'doubleclick', 'adnxs', 'exoclick', 'popads', 'popcash', 'trafficjunky',
    'googlesyndication', '/ads/', 'adtag', 'vast', 'taboola', 'outbrain',
    'creativecdn', 'whitetrafsa', 'adserve', 'banner',
)


def _is_ad_m3u8(u):
    low = str(u).lower()
    return any(mk in low for mk in _AD_M3U8_MARKERS)


def _probe_m3u8_durations(streams, page_url):
    """Fetch each m3u8 playlist and measure its duration (masters are
    followed into their first variant; duration = sum of #EXTINF). This
    is how a ~10 s preview or an ad clip gets told apart from the full
    movie - "capture all m3u8 then play the longest one", as the user
    put it. Works for streamhls (PNG-wrapped SEGMENTS - the playlist
    text itself is plain) and tapecontent alike. Failures map to 0.0."""
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

    for u in [str(x) for x in (streams or [])][:10]:
        if not _looks_like_m3u8(u):
            continue
        try:
            r = cfreq.get(u, impersonate='chrome131', timeout=8,
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
                                   timeout=8,
                                   headers={'Referer': page_url, 'Accept': '*/*'})
                    text = r2.text or ''
            durations[u] = _sum_extinf(text)
        except Exception:
            durations[u] = 0.0
    return durations


def _finalize_m3u8_capture(urls, page_url, title):
    """Finalizer for the all-m3u8 capture: derive streamhls master
    siblings, probe every playlist's duration, then keep the longest /
    heaviest ones (full movie beats 10 s previews and ad clips; equal
    durations prefer master.m3u8 so mpv gets quality selection)."""
    kept = [u for u in dict.fromkeys(urls)
            if _looks_like_m3u8(u) and not _is_ad_m3u8(u)]
    # Guard against STALE streamhls-family links (…/<sig>/<gibberish>/
    # <10-digit-ts>/<id>/x.m3u8). When Brave relaunches with the net-log
    # flag it also RESTORES the previous session's tabs, and a restored
    # video tab can re-request its still-valid old link - capturing a
    # PREVIOUS video's stream as a mirror of this one. Field data shows
    # the ts is FUTURE-dated relative to generation (expiry-style or
    # JST-shifted: this session's links carry ts several hours ahead of
    # the clock), so only treat a link as stale when its ts is more than
    # 48 h in the past - fresh links are always future-dated and can
    # never be dropped by this check.
    _now = time.time()
    fresh = []
    for u in kept:
        m = re.search(r'/(\d{10})(?:/\d+){1,2}/[^/?]+\.m3u8', u)
        if m and (_now - int(m.group(1))) > 48 * 3600:
            print(f'[GENERIC_JAV] dropping stale (restored-tab) link: {u[:110]}')
            continue
        fresh.append(u)
    kept = fresh
    candidates = _derive_streamhls_candidates(kept)
    try:
        durations = _probe_m3u8_durations(candidates, page_url)
        if durations:
            with_dur = [u for u in candidates if u in durations]
            without_dur = [u for u in candidates if u not in durations]
            with_dur.sort(
                key=lambda u: (durations.get(u, 0.0), 'master.m3u8' in u.lower()),
                reverse=True)
            # When a full-length stream exists, drop preview-length ones
            # (<60s) - they are the site's 10s previews or ad clips, not
            # usable mirrors.
            if with_dur and durations.get(with_dur[0], 0.0) >= 300.0:
                with_dur = [u for u in with_dur if durations.get(u, 0.0) >= 60.0] or with_dur
            candidates = with_dur + without_dur
            for u, d in sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:4]:
                print(f'  [duration] {d:8.1f}s  {u[:110]}')
    except Exception as exc:
        print(f'[GENERIC_JAV] duration probe failed: {exc}')
    seen_tok = set()
    dedup = []
    for u in candidates:
        m = re.search(r'/hls/([A-Za-z0-9_\-]+)/', u)
        tok = m.group(1) if m else u
        if tok in seen_tok:
            continue
        seen_tok.add(tok)
        dedup.append(u)
    dedup = dedup[:4]
    best = max(durations.values()) if durations else 0.0
    preview_only = bool(dedup) and 0.0 < best < 60.0
    print(f'[GENERIC_JAV] main-Brave capture found {len(dedup)} stream(s)')
    return {'streams': dedup, 'title': title, 'preview_only': preview_only}


def _title_keyword_from_url(source_url, title=''):
    """A short keyword that appears in the Brave window/tab title of the
    video page (JAV code like 'GVG-879', else the URL slug) - used by
    the UI auto-click to find the RIGHT window before clicking."""
    for src in (str(title or ''), str(source_url or '')):
        m = re.search(r'[A-Za-z]{2,6}-\d{2,6}', src)
        if m:
            return m.group(0)
    try:
        parts = [p for p in urlparse(source_url).path.split('/') if p]
        if parts:
            base = re.sub(r'[^A-Za-z0-9]', '', parts[-1])[:24]
            if len(base) >= 5:
                return base
    except Exception:
        pass
    return ''


_AUTOCLICK_PS1 = r"""
param([string]$Keyword = '')

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName UIAutomationClient | Out-Null
Add-Type -AssemblyName UIAutomationTypes | Out-Null
try {
  Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class AGPLMouse{[DllImport("user32.dll")]public static extern bool SetProcessDPIAware();[DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y);[DllImport("user32.dll")]public static extern void mouse_event(uint f,uint dx,uint dy,uint d,UIntPtr e);[DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();[DllImport("user32.dll")]public static extern void keybd_event(byte k,uint s,uint f,UIntPtr e);}' | Out-Null
} catch {}
try { [AGPLMouse]::SetProcessDPIAware() | Out-Null } catch {}

function Find-VideoWindow {
  param([string]$kw)
  try {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Chrome_WidgetWin_1')
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
    $fallback = $null
    foreach ($w in $wins) {
      try {
        $p = Get-Process -Id $w.Current.ProcessId -ErrorAction SilentlyContinue
        if (-not $p -or $p.ProcessName -notmatch 'brave') { continue }
        if ($kw) {
          if ($w.Current.Name -like "*$kw*") { return $w }
        } elseif (-not $fallback) { $fallback = $w }
      } catch {}
    }
    return $fallback
  } catch { return $null }
}

$win = Find-VideoWindow -kw $Keyword
if (-not $win) { Write-Output 'NOWINDOW'; exit 1 }

$fg = $false
try {
  $h = $win.Current.NativeWindowHandle
  [AGPLMouse]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero) | Out-Null
  [AGPLMouse]::SetForegroundWindow($h) | Out-Null
  [AGPLMouse]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero) | Out-Null
  Start-Sleep -Milliseconds 900
  $fg = ([AGPLMouse]::GetForegroundWindow() -eq $h)
} catch {}
try { $win.SetFocus() } catch {}

# Strategy 1: a real button named 'play' -> programmatic Invoke
# (works even when the window is not in the foreground)
try {
  $btnCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
  $btns = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
  foreach ($b in $btns) {
    $nm = ''
    try { $nm = [string]$b.Current.Name } catch {}
    if ($nm -match '^\s*(play|play video)\s*$') {
      try {
        $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        Write-Output ('CLICKED:invoke:' + $nm)
        exit 0
      } catch {}
    }
  }
} catch {}

# Strategy 2: the <video> element -> real click at its centre. Site
# players toggle on video click and the play overlay sits ON TOP of the
# video, so the click lands on the overlay. Only when Brave is the
# foreground window (a background click could hit another app).
try {
  $targets = @()
  foreach ($ct in @([System.Windows.Automation.ControlType]::Group, [System.Windows.Automation.ControlType]::Custom)) {
    try {
      $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct)
      $targets += $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $c)
    } catch {}
  }
  foreach ($g in $targets) {
    $lct = ''
    try { $lct = ([string]$g.Current.LocalizedControlType).ToLower() } catch {}
    if ($lct -eq 'video' -and $fg) {
      $r = $g.Current.BoundingRectangle
      if ($r.Width -gt 40 -and $r.Height -gt 30) {
        $cx = [int]($r.X + $r.Width / 2)
        $cy = [int]($r.Y + $r.Height / 2)
        [AGPLMouse]::SetCursorPos($cx, $cy) | Out-Null
        Start-Sleep -Milliseconds 200
        [AGPLMouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [AGPLMouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
        Write-Output 'CLICKED:video-center'
        exit 0
      }
    }
  }
} catch {}

# Strategy 2.5: javdock's play control is a BARE <div class="play-button">
# with no role, name or label - invisible to UIA. But it is rendered as a
# centred overlay ON TOP of the player poster image (the video thumbnail),
# which Chromium DOES expose as an Image element with a bounding rect.
# Click the centre of the largest ~16:9 image at least 380px wide - that
# is the player area; banner ads (728x90 / 300x250) fail the aspect check.
try {
  $best = $null
  $bestArea = 0
  foreach ($ct in @([System.Windows.Automation.ControlType]::Image, [System.Windows.Automation.ControlType]::Pane)) {
    try {
      $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct)
      $els = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $c)
      foreach ($e in $els) {
        try {
          $r = $e.Current.BoundingRectangle
          if ($r.Width -ge 380 -and $r.Height -ge 200) {
            $ar = $r.Width / $r.Height
            if ($ar -ge 1.3 -and $ar -le 2.6) {
              $area = $r.Width * $r.Height
              if ($area -gt $bestArea) { $bestArea = $area; $best = $e }
            }
          }
        } catch {}
      }
    } catch {}
  }
  if ($best -and $fg) {
    $r = $best.Current.BoundingRectangle
    $cx = [int]($r.X + $r.Width / 2)
    $cy = [int]($r.Y + $r.Height / 2)
    [AGPLMouse]::SetCursorPos($cx, $cy) | Out-Null
    Start-Sleep -Milliseconds 200
    [AGPLMouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    [AGPLMouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Write-Output 'CLICKED:poster-center'
    exit 0
  }
} catch {}

# Strategy 3: ANY element whose accessible name is 'play' and that can
# be invoked (site players often render the control as a plain div,
# which still lands in the accessibility tree when it has a label)
try {
  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    $nm = ''
    try { $nm = [string]$e.Current.Name } catch {}
    if ($nm -match '^\s*(play|play video)\s*$') {
      try {
        $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        Write-Output ('CLICKED:invoke-any:' + $nm)
        exit 0
      } catch {}
    }
  }
} catch {}

Write-Output 'NOBUTTON'
exit 2
"""


def _autoclick_play_in_main_brave(keyword=''):
    """Best-effort OS-level click on Play in the user's MAIN Brave.

    Chromium 136+ silently ignores --remote-debugging-port on the DEFAULT
    user profile (App-Bound Encryption protection), so CDP cannot drive
    the user's real browser at all; Windows UI Automation works on any
    window regardless. When every strategy misses we simply ask the user
    to click - the capture channels are unaffected either way.
    Returns 'clicked' | 'nobutton' | 'nowindow' | 'error'."""
    ps_path = ''
    try:
        ps_path = os.path.join(tempfile.gettempdir(), 'agpl_brave_autoclick.ps1')
        with open(ps_path, 'w', encoding='ascii', errors='replace') as f:
            f.write(_AUTOCLICK_PS1)
    except Exception:
        return 'error'
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', ps_path, '-Keyword', str(keyword or '')],
            capture_output=True, text=True, timeout=120)
        lines = (out.stdout or '').strip().splitlines()
        line = lines[0].strip() if lines else ''
    except Exception as exc:
        print(f'[GENERIC_JAV] auto-click error: {exc}')
        return 'error'
    if line.startswith('CLICKED'):
        print(f'[GENERIC_JAV] auto-click: {line}')
        return 'clicked'
    if line == 'NOBUTTON':
        print('[GENERIC_JAV] auto-click: play control not found yet')
        return 'nobutton'
    print(f'[GENERIC_JAV] auto-click: {line or "no output"}')
    return 'nowindow'


# ── FetchV direct-capture paste flow ──────────────────────────────────────────
# The user captures direct stream links with the FetchV extension in their
# own Brave (it watches live requests - the one channel that reliably sees
# the no-store HLS manifests) and pastes them into the app. javdock /
# javhdporn serve their streams from a rotating zoo of signed CDN hosts
# (streamhls.click, pianopic.com, maxstream.org, *.tapecontent.net,
# cloudatacdn.com, dailynewsbriefing.cyou "urlset" .txt playlists, ...),
# so detection is by URL SHAPE, never by host list.

_FETCHV_EXCLUDE_HOSTS = (
    # page sites the app already resolves itself
    'javdock.com', 'javhdporn.net', 'javsubbed.net', 'javenglish.cc',
    'javflix.cc', 'javx.cc', 'javx.org', 'jable.tv', 'sextb.net',
    'javgg.net', 'missav', '123av.com', 'milfnut.com',
    # providers with their own dedicated flows - keep existing behavior
    'gofile.io', 'mega.nz', 'fileditch', 'streamtape',
    'tulipvid.net', 'onlythot.net', 'surrit.com', 'doodstream',
    'youtube.com', 'youtu.be',
)


def _looks_like_fetchv_stream_url(u):
    """True for a pasted DIRECT stream link captured with FetchV."""
    u = str(u or '').strip()
    if not u.lower().startswith(('http://', 'https://')):
        return False
    try:
        p = urlparse(u)
    except Exception:
        return False
    host = (p.netloc or '').lower()
    if not host or any(h in host for h in _FETCHV_EXCLUDE_HOSTS):
        return False
    path = (p.path or '').lower()
    ext = os.path.splitext(path)[1]
    if ext in ('.html', '.htm', '.php', '.aspx', '.jsp'):
        return False
    if re.search(r'\.m3u8(?:[?#]|$)', u, re.IGNORECASE):
        return True
    # doodstream-style multi-bitrate playlists travel as .txt
    if 'urlset/' in path or re.search(r'index-f\d+-v\d+-a\d+\.txt(?:[?#]|$)', u, re.IGNORECASE):
        return True
    if ext in ('.mp4', '.mkv', '.webm', '.mov', '.m4v'):
        return True
    # extensionless signed CDN link (cloudatacdn style: ?token=…&expiry=…)
    if not ext and re.search(r'[?&](?:token|expiry|expires|signature)=', u, re.IGNORECASE):
        return True
    return False


_JAV_CODE_RE = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,6}-\d{2,6})(?![0-9])')

# Bumped every round that ships changes - printed at startup so a field
# log always shows which build is actually running (field R39: two test
# rounds were run against a stale file with no way to tell from the log).
_BUILD = 'R63-page-row'


def _jav_code_from_stream_url(u):
    """JAV code embedded in a stream URL (tapecontent mirror filenames
    carry it: KV-320-MOSAIC-….mp4). Used as the playlist title so the
    entry is easy to rename. Whole-segment and filename-prefix matches
    win over random substring hits; the boundary lookarounds keep
    'index-f2-v1-a1', 'NET-23062026' and stream signatures out."""
    u = str(u or '')
    try:
        from urllib.parse import unquote as _uq
        path = _uq(urlparse(u).path or '')
    except Exception:
        path = u
    for seg in path.split('/'):
        if re.fullmatch(r'[A-Za-z]{2,6}-\d{2,6}', seg):
            return seg.upper()
    base = path.rsplit('/', 1)[-1]
    m = re.match(r'^([A-Za-z]{2,6}-\d{2,6})(?![0-9])(?:[-_.]|$)', base)
    if m:
        return m.group(1).upper()
    m = _JAV_CODE_RE.search(u)
    return m.group(1).upper() if m else ''


def _fetchv_fallback_title(u):
    """Title for links without a JAV code: the most id-looking path
    segment (skips index-*/master*), else the host."""
    try:
        p = urlparse(str(u or ''))
        segs = [s for s in (p.path or '').split('/') if s]
        for seg in reversed(segs):
            base = os.path.splitext(seg)[0]
            if (len(base) >= 6 and re.fullmatch(r'[A-Za-z0-9_~\-]+', base)
                    and not base.lower().startswith(('index', 'master', 'playlist'))):
                return base[:40]
        return (p.netloc or 'Stream').split(':')[0]
    except Exception:
        return 'Stream'


_FETCHV_FAMILY_RE = re.compile(
    r'(?i)^/(?:[^/]+/)*?(?:hls|stream)/[^/]+/[^/]+/\d{6,12}/\d+/[^/]+\.m3u8$')


def _derive_fetchv_master(u):
    """streamhls-family CDNs (streamhls.click, pianopic.com, turbo,
    earnvidjavgg…) share the path shape
    /(hls|stream)/<sig>/<gibberish>/<10-digit-ts>/<id>/<file>.m3u8 -
    the master sits in the same directory. FetchV usually only catches
    the variant the player loaded, so derive the master for quality
    selection. Returns '' when the URL is not that shape."""
    u = str(u or '')
    try:
        p = urlparse(u)
    except Exception:
        return ''
    if not _FETCHV_FAMILY_RE.match(p.path or ''):
        return ''
    if (p.path or '').lower().endswith('/master.m3u8'):
        return ''
    return u.rsplit('/', 1)[0] + '/master.m3u8'


def _is_hls_like_fetchv_url(u):
    """HLS manifest by URL shape: .m3u8, or a urlset/.txt playlist."""
    return bool(re.search(
        r'(\.m3u8?(?:[?#]|$)|urlset/|index-f\d+-v\d+-a\d+\.txt(?:[?#]|$))',
        str(u or ''), re.IGNORECASE))


def _fetchv_sum_extinf(text):
    try:
        return sum(float(m) for m in re.findall(r'#EXTINF:([\d.]+)', str(text or '')))
    except Exception:
        return 0.0


def _load_cookies_for_host(url):
    """Cookie header value for a stream URL from the app's Netscape
    cookies.txt files (generic + domain-specific, same files mpv uses).
    The user's browser sets CDN session cookies while the embedded player
    loads; if they exported them, this is a working cookie source."""
    try:
        host = (urlparse(str(url)).netloc or '').lower()
        if not host:
            return ''
        base = host[4:] if host.startswith('www.') else host
        app_dir = os.path.dirname(os.path.abspath(__file__))
        paths = [os.path.join(app_dir, 'cookies.txt')]
        try:
            import glob as _glob
            paths += _glob.glob(os.path.join(app_dir, 'cookies_*.txt'))
        except Exception:
            pass
        pairs = []
        for path in paths:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#HttpOnly_'):
                            line = line[len('#HttpOnly_'):]
                        elif not line or line.startswith('#'):
                            continue
                        parts = line.split('\t')
                        if len(parts) < 7:
                            continue
                        dom = parts[0].lstrip('.').lower()
                        if dom and (base == dom or base.endswith('.' + dom)
                                    or dom == host):
                            kv = parts[5]
                            if kv and kv not in [p.split('=', 1)[0] for p in pairs]:
                                pairs.append(f"{kv}={parts[6]}")
            except Exception:
                continue
        return '; '.join(pairs)
    except Exception:
        return ''


# Analytics / static-asset hosts that are never video embeds (field:
# the javhdporn static HTML only yielded googletagmanager, jsdelivr and
# the site logo - the player iframe is injected at render time).
_HARVEST_EXCLUDE_HOSTS = (
    'googletagmanager', 'google-analytics', 'googlesyndication',
    'doubleclick', 'jsdelivr', 'cdnjs', 'unpkg', 'gstatic',
    'fonts.g', 'bootstrap', 'cloudflare.com', 'facebook', 'twitter',
    'taboola', 'outbrain', 'exoclick', 'popads', 'popcash',
)
_HARVEST_EMBED_HOST_HINTS = (
    'maxstream', 'streamtape', 'dood', 'tapecontent', 'cloudata',
    'megacloud', 'streamsb', 'uptostream', 'vidmoly', 'streamhls',
    'pianopic', 'earnvid', 'turbo', 'javplayer', 'mxdcontent',
)


def _registrable_domain(host):
    """Last two labels of a hostname ('s2.maxstream.org' ->
    'maxstream.org'). A crude public-suffix-free heuristic - used only to
    generate EXTRA candidates/cookie scopes to try, never to reject
    anything, so '.co.uk'-style oddities are harmless."""
    try:
        host = str(host or '').lower().strip().strip('.')
        if not host:
            return ''
        labels = host.split('.')
        if len(labels) <= 2:
            return host
        return '.'.join(labels[-2:])
    except Exception:
        return ''


def _derive_fetchv_embeds(u):
    """Derive candidate EMBED page URLs from the stream URL itself.

    Two placements exist in this CDN family:
    - streamhls/pianopic: the player iframe lives on the SAME host at
      /e/<video-id> ('.../hls/<sig>/<ref>/<ts>/<id>/master.m3u8' ->
      'https://streamhls.click/e/<id>') - a SAME-ORIGIN m3u8 request.
    - maxstream: the iframe lives on the APEX domain
      ('s2.maxstream.org/hls2/.../<id>_n/master.m3u8' ->
      'https://maxstream.org/e/<id>') - the CDN subdomain /e/ 404s
      (field: s2/s5/s8.maxstream.org). The m3u8 request is then
      cross-ORIGIN but same-SITE.

    Both variants are derived; the probe tries them all and keeps what
    the CDN accepts. A trailing '_x' rendition suffix is stripped."""
    out = []
    try:
        p = urlparse(str(u))
        segs = [s for s in (p.path or '').split('/') if s]
        if len(segs) >= 2:
            vid = segs[-2]
            host = (p.netloc or '').lower()
            hosts = [host]
            apex = _registrable_domain(host)
            if apex and apex != host:
                hosts.append(apex)
            for h in hosts:
                if not h:
                    continue
                for cand_id in (re.sub(r'_[A-Za-z0-9]+$', '', vid), vid):
                    if (len(cand_id) >= 6
                            and re.fullmatch(r'[A-Za-z0-9]+', cand_id)):
                        cand = f'{p.scheme or "https"}://{h}/e/{cand_id}'
                        if cand not in out:
                            out.append(cand)
    except Exception:
        pass
    return out


def _warm_embed_cookies(urls, referer):
    """Visit embed pages to collect their Set-Cookie (and follow
    redirects - the FINAL url is a valid Referer candidate too).
    Returns (cookies_by_host, final_urls)."""
    cookies = {}
    finals = []
    try:
        import curl_cffi.requests as cfreq
    except ImportError:
        return cookies, finals
    for u in [str(x) for x in (urls or [])][:4]:
        try:
            r = cfreq.get(u, impersonate='chrome131', timeout=8,
                          allow_redirects=True,
                          headers={'Accept': 'text/html,*/*',
                                   'Referer': str(referer or '')})
            final = str(getattr(r, 'url', '') or u)
            if final not in finals:
                finals.append(final)
            host = urlparse(final).netloc.lower()
            if not host:
                continue
            jar = cookies.setdefault(host, [])
            # Every cookie the redirect chain set - curl_cffi exposes a
            # cookie jar per response; collect from all hops. These
            # back the m3u8 fetches later (scoped by registrable domain
            # in _probe_fetchv_streams, like a real browser's
            # Domain=.example.com cookies).
            found = {}
            for resp in [r] + list(getattr(r, 'history', None) or []):
                try:
                    for k, v in dict(resp.cookies or {}).items():
                        found[str(k)] = str(v)
                except Exception:
                    pass
            if not found:
                sc = r.headers.get('Set-Cookie') or ''
                kv = sc.split(';', 1)[0]
                if '=' in kv:
                    found[kv.split('=', 1)[0]] = kv.split('=', 1)[1]
            for name, value in found.items():
                jar[:] = [c for c in jar if not c.startswith(name + '=')]
                jar.append(f'{name}={value}')
        except Exception:
            continue
    return cookies, finals


def _fetchv_embed_candidates(origin_urls, sample_stream_url=''):
    """Discover EMBED page URLs + cookies for captured stream links.

    These CDNs hotlink-protect harder than streamhls: field proof
    (s5/s8.maxstream.org) - even a Chrome-TLS request with the video
    page as Referer is 403'd (plain nginx 403, no challenge), while the
    user's browser fetches the m3u8 fine. Inside the page the player
    runs in an EMBED (iframe on the CDN itself, e.g. …/e/<id>), so the
    browser sends THAT page as Referer - not the video page - plus
    cookies the embed page set. The static page HTML rarely carries the
    iframe (injected at render time), so harvesting is filtered to
    player-looking URLs only; the reliable source is the DERIVED embed
    (see _derive_fetchv_embeds), done by the caller. Returns
    (embed_urls, cookies_by_host)."""
    embed_urls = []
    page_hosts = set()
    for u in [str(x) for x in (origin_urls or []) if x]:
        try:
            page_hosts.add(urlparse(u).netloc.lower())
        except Exception:
            pass
    htmls = []
    try:
        import curl_cffi.requests as cfreq
    except ImportError:
        return embed_urls, {}
    for u in [str(x) for x in (origin_urls or []) if x]:
        try:
            r = cfreq.get(u, impersonate='chrome131', timeout=10,
                          headers={'Accept': 'text/html,*/*'})
            if getattr(r, 'status_code', 0) == 200:
                htmls.append(r.text or '')
        except Exception:
            continue
    seen = set()
    for html in htmls:
        for m in re.finditer(
                r'(?:src|file|source)["\']?\s*[:=]\s*["\']?(https?://[^"\'\s>]+)', html,
                re.IGNORECASE):
            cand = m.group(1)
            try:
                p = urlparse(cand)
            except Exception:
                continue
            host = (p.netloc or '').lower()
            path = (p.path or '').lower()
            if not (p.scheme in ('http', 'https') and host):
                continue
            if host in page_hosts or '.m3u8' in cand.lower() or cand in seen:
                continue
            if any(h in host for h in _HARVEST_EXCLUDE_HOSTS):
                continue
            if not (any(h in host for h in _HARVEST_EMBED_HOST_HINTS)
                    or any(k in path for k in ('/e/', '/embed', '/player', '/iframe', '/p/'))):
                continue
            seen.add(cand)
            embed_urls.append(cand)
    cookies_by_host, _finals = _warm_embed_cookies(embed_urls[:3],
                                                   (origin_urls or [''])[0])
    return embed_urls, cookies_by_host


def _probe_fetchv_streams(candidates, origin_urls=None, _fetch=None,
                          embed_referers=None, cookies_by_host=None):
    """Discover the request context each CDN accepts + playlist durations.

    javdock/javhdporn embed their players, and the CDNs they use
    (maxstream, pianopic, streamhls, urlset mirrors, ...) hotlink-protect.
    Field proof (s5/s8.maxstream.org, plain nginx 403): even a
    Chrome-TLS request with the video page as Referer is rejected - the
    browser plays these fine because the player runs inside an EMBED
    iframe on the CDN host itself, so the m3u8 request is SAME-ORIGIN:
    Referer = the /e/<id> embed page, Origin = the CDN origin (or none),
    Sec-Fetch-Site: same-origin, plus the embed page's cookies.

    Candidates per stream URL (first playlist wins, adaptively
    remembered): same-host embed pages (with Origin, then without),
    harvested embed pages, the pasted origin pages, javdock, javhdporn,
    no Referer. Cookies: embed-page cookies + the app's cookies.txt for
    the stream host. Returns {url: {'duration': seconds, 'headers':
    {...}}} - the exact winning header context for playback."""
    origin_urls = [str(u) for u in (origin_urls or []) if u]
    embeds = [str(u) for u in (embed_referers or []) if u]
    cookies_by_host = cookies_by_host or {}

    def _cookie_for(url):
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return ''
        parts = list(cookies_by_host.get(host) or [])
        # Embed-page cookies are keyed by the EMBED host (e.g.
        # maxstream.org) while the stream lives on a CDN subdomain
        # (s2.maxstream.org). A real browser sends Domain-scoped cookies
        # across the whole registrable domain, so merge every jar that
        # shares it.
        rdom = _registrable_domain(host)
        if rdom:
            for jar_host, jar in (cookies_by_host or {}).items():
                if jar_host == host or not jar:
                    continue
                if _registrable_domain(jar_host) == rdom:
                    parts.extend(jar)
        jar = _load_cookies_for_host(url)
        if jar:
            parts.append(jar)
        # dedupe by cookie name, first occurrence wins
        seen = {}
        for part in parts:
            if part and '=' in part:
                seen.setdefault(part.split('=', 1)[0], part)
        return '; '.join(seen.values())

    def _headers_for(url, ref, mode, cookie=''):
        hdrs = {
            'Accept': '*/*',
            # Explicit UA so the winning context is fully replayable by
            # the playback proxy (curl_cffi's impersonation default is
            # the identical Chrome/131 string, so nothing changes for
            # CDNs that already work).
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'),
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
        }
        if mode == 'same-origin':
            hdrs['Referer'] = ref
            hdrs['Sec-Fetch-Site'] = 'same-origin'
            try:
                p = urlparse(url)
                hdrs['Origin'] = f'{p.scheme or "https"}://{p.netloc}'
            except Exception:
                pass
        elif mode == 'same-origin-noorigin':
            hdrs['Referer'] = ref
            hdrs['Sec-Fetch-Site'] = 'same-origin'
        elif mode == 'same-site':
            # maxstream placement: the iframe document (Referer) sits on
            # the APEX domain while the manifest is fetched from a CDN
            # subdomain - cross-origin but same-site. The player's XHR
            # sends the EMBED's origin, not the CDN's.
            hdrs['Referer'] = ref
            hdrs['Sec-Fetch-Site'] = 'same-site'
            try:
                ep = urlparse(ref)
                hdrs['Origin'] = f'{ep.scheme or "https"}://{ep.netloc}'
            except Exception:
                pass
        elif mode == 'same-site-noorigin':
            hdrs['Referer'] = ref
            hdrs['Sec-Fetch-Site'] = 'same-site'
        elif mode == 'cross-site':
            hdrs['Referer'] = ref
            hdrs['Sec-Fetch-Site'] = 'cross-site'
        else:
            hdrs['Sec-Fetch-Site'] = 'no-referrer'
        if cookie:
            hdrs['Cookie'] = cookie
        return hdrs

    if _fetch is None:
        try:
            import curl_cffi.requests as cfreq
        except ImportError:
            return {}

        def _fetch(url, hdrs):
            # Some CDNs flag older impersonations - fall back through the
            # fingerprint list before giving up. Error bodies are returned
            # too (diagnostics).
            for imp in ('chrome131', 'chrome124'):
                try:
                    r = cfreq.get(url, impersonate=imp, timeout=6,
                                  headers=dict(hdrs))
                    if getattr(r, 'status_code', 0) == 200:
                        return True, (r.text or '')
                    if imp == 'chrome124':
                        return False, (r.text or '')
                except Exception:
                    if imp == 'chrome124':
                        return False, ''
            return False, ''

    def _valid(text):
        return ('#EXTM3U' in text
                and ('#EXTINF' in text or '#EXT-X-STREAM-INF' in text))

    def _duration(url, text, hdrs):
        dur = _fetchv_sum_extinf(text)
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
                vurl = urljoin(url, variant)
                ok2, vtext = _fetch(vurl, hdrs)
                if ok2 and _valid(vtext):
                    dur = _fetchv_sum_extinf(vtext)
        return dur

    results = {}
    preferred = None
    for u in [str(x) for x in (candidates or [])][:6]:
        if not _is_hls_like_fetchv_url(u):
            continue
        cookie = _cookie_for(u)
        try:
            stream_host = urlparse(u).netloc.lower()
        except Exception:
            stream_host = ''
        cands = []
        for e in embeds:
            try:
                ehost = urlparse(e).netloc.lower()
            except Exception:
                ehost = ''
            if ehost and ehost == stream_host:
                cands.append((e, 'same-origin'))
                cands.append((e, 'same-origin-noorigin'))
            elif (ehost and stream_host
                  and _registrable_domain(ehost) == _registrable_domain(stream_host)):
                # Same site, different subdomain (maxstream.org embed ->
                # s2.maxstream.org manifest): the real iframe context is
                # cross-origin but same-site.
                cands.append((e, 'same-site'))
                cands.append((e, 'same-site-noorigin'))
            else:
                cands.append((e, 'cross-site'))
        for o in origin_urls:
            cands.append((o, 'cross-site'))
        cands.append(('https://www.javdock.com/', 'cross-site'))
        cands.append(('https://www.javhdporn.net/', 'cross-site'))
        cands.append((None, 'none'))
        deduped = []
        for c in cands:
            if c not in deduped:
                deduped.append(c)
        cands = deduped
        if preferred in cands:
            cands.remove(preferred)
            cands.insert(0, preferred)
        last_body = ''
        for ref, mode in cands:
            hdrs = _headers_for(u, ref, mode, cookie)
            ok, text = _fetch(u, dict(hdrs))
            if ok and _valid(text):
                preferred = (ref, mode)
                stored = {k: v for k, v in hdrs.items() if k != 'Accept'}
                results[u] = {
                    'duration': _duration(u, text, hdrs),
                    'headers': stored,
                }
                try:
                    _host = urlparse(u).netloc or u[:30]
                    print(f"[GENERIC_JAV] FetchV probe: {_host} accepts "
                          f"referer={ref or 'none'} [{mode}]"
                          f"{'+cookie' if cookie else ''} "
                          f"({results[u]['duration']:.0f}s)")
                except Exception:
                    pass
                break
            if text:
                last_body = text
        if u not in results:
            try:
                snippet = re.sub(r'\s+', ' ', last_body or '')[:160]
                print(f'[GENERIC_JAV] FetchV probe: NO referer accepted for '
                      f'{u[:110]} | body: {snippet or "(empty)"}')
            except Exception:
                pass
    return results


def _unpack_packjs(text, max_rounds=3):
    """Decode Dean Edwards packed inline scripts -
    eval(function(p,a,c,k,e,d){...}('payload',radix,count,
    'kw0|kw1|…'.split('|'),0,{})).

    maxstream hides its whole player config - including the tokenized
    m3u8 - inside one of these: the raw embed HTML has no stream URL at
    all (field JUR-356/FTHTD-199: 'no stream URLs in …/e/<id>'), and
    three public Maxstream extractors (Cloudstream-style Kotlin, Bagol
    JS) all unpack this script and read file:"…" from the result. Pure
    word-level base-N substitution (radix up to 62), no JS engine
    needed. Returns every decoded text found (nested packing
    included)."""
    # Anchored on the packer function's closing brace so the payload
    # group does not swallow the packer boilerplate (which itself
    # contains '…',digits,digits,'…' shapes); quote-agnostic like the
    # reference python-js-unpacker; loose pattern is a fallback for
    # de-boilerplated call sites.
    anchored = re.compile(
        r"\}\s*\(\s*(['\"])([\s\S]+?)\1\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*"
        r"(['\"])([\s\S]+?)\5\s*\.split\(\s*(['\"])\|\7\s*\)")
    loose = re.compile(
        r"\(\s*(['\"])([\s\S]+?)\1\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*"
        r"(['\"])([\s\S]+?)\5\s*\.split\(\s*(['\"])\|\7\s*\)")
    out = []
    queue = [str(text or '')]
    # The packer's base-N alphabet: 0-9a-z, then A-Z for values > 35
    # (Dean Edwards packer: c > 35 ? String.fromCharCode(c + 29) :
    # c.toString(36)). Python int() only parses base <= 36, so decode
    # larger radices by hand.
    b62 = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'

    def _frombase(w, radix):
        try:
            if radix <= 36:
                return int(w, radix)
            n = 0
            for ch in w:
                i = b62.find(ch)
                if i < 0 or i >= radix:
                    return None
                n = n * radix + i
            return n
        except Exception:
            return None

    for _ in range(max_rounds):
        nxt = []
        for chunk in queue:
            matches = list(anchored.finditer(chunk))
            if not matches:
                matches = list(loose.finditer(chunk))
            for m in matches:
                try:
                    payload = m.group(2)
                    radix = int(m.group(3))
                    keywords = m.group(6).split('|')
                    if radix < 2 or radix > 62:
                        continue

                    def _sub(wm, _r=radix, _k=keywords, _fb=_frombase):
                        w = wm.group(0)
                        n = _fb(w, _r)
                        if n is None:
                            return w
                        if 0 <= n < len(_k) and _k[n]:
                            return _k[n]
                        return w

                    decoded = re.sub(r'[A-Za-z0-9_]+', _sub, payload)
                    if decoded and decoded not in out:
                        out.append(decoded)
                        nxt.append(decoded)
                except Exception:
                    continue
        if not nxt:
            break
        queue = nxt
    return out


def _fetchv_fresh_embed_streams(embed_urls, origin_url, _get=None):
    """Load embed pages FRESH and harvest stream URLs minted for THAT
    fetch's session.

    maxstream proof (field, R33-R37): the hls2 *.m3u8?t=…&s=…&e=43200
    tokens 403 for EVERY replay of a browser-captured URL - no Referer
    combination, cookies.txt, or Chrome TLS fixes it - because the token
    is minted per embed-page LOAD and bound to that client's context.
    A FetchV-captured token belongs to the user's browser session and
    can never be replayed by the app. Re-loading the embed page
    OURSELVES (same IP, Chrome TLS, fresh cookies) mints a token bound
    to OUR context.

    The config hides in a PACKED eval script on maxstream - decode with
    _unpack_packjs and scan raw + decoded text. Harvested streams are
    stored WITHOUT being fetched: if the minted tokens are single-use,
    a validation request would consume them before playback starts, so
    the FIRST fetch of each URL must be playback itself (public
    maxstream extractors do exactly this: unpack and hand over).

    Returns {url: {'duration': seconds, 'headers': {…}}} exactly like
    _probe_fetchv_streams, so callers can merge the two."""
    out = {}
    embed_urls = [str(u) for u in (embed_urls or []) if u][:3]
    if not embed_urls:
        return out

    if _get is None:
        try:
            import curl_cffi.requests as cfreq
        except ImportError:
            return out

        def _get(url, headers=None):
            return cfreq.get(url, impersonate='chrome131', timeout=10,
                             allow_redirects=True,
                             headers=dict(headers or {}))

    _UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
           'AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/131.0.0.0 Safari/537.36')
    for embed in embed_urls:
        try:
            hdrs = {'Accept': 'text/html,*/*', 'User-Agent': _UA}
            if origin_url:
                hdrs['Referer'] = str(origin_url)
            r = _get(embed, hdrs)
        except Exception:
            continue
        try:
            if getattr(r, 'status_code', 0) != 200:
                continue
            html = r.text or ''
            final = str(getattr(r, 'url', '') or embed)
            cookie_pairs = {}
            for resp in [r] + list(getattr(r, 'history', None) or []):
                try:
                    for k, v in dict(resp.cookies or {}).items():
                        cookie_pairs[str(k)] = str(v)
                except Exception:
                    pass
        except Exception:
            continue
        if not html:
            continue

        # Harvest stream URLs: player configs (flashvars / kt_player /
        # JW) embed the tokenized manifest as a plain string - on
        # maxstream it hides inside a PACKED eval script, so decode
        # those first and scan decoded + raw together. Unescape \/ and
        # \u002F so the URL regexes see real URLs.
        packed = _unpack_packjs(html)
        flat = '\n'.join(
            p.replace('\\/', '/')
             .replace('\\u002F', '/').replace('\\u002f', '/')
             .replace('&amp;', '&')
            for p in (packed + [html]))
        found = []
        for m in re.finditer(r'https?://[^\s"\'<>\\]+', flat):
            cand = m.group(0).rstrip(');,')
            if cand not in found and _is_hls_like_fetchv_url(cand):
                found.append(cand)
        for m in re.finditer(
                r'(?:video_url|video_alt_url|file|source|src)["\']?\s*[:=]\s*'
                r'["\']([^"\']+\.m3u8[^"\']*)', flat, re.IGNORECASE):
            try:
                cand = urljoin(final, m.group(1).strip())
            except Exception:
                continue
            if cand not in found and _is_hls_like_fetchv_url(cand):
                found.append(cand)
        # relative manifest paths inside quotes ('/hls2/.../master.m3u8?t=')
        for m in re.finditer(r'["\'](/[^"\']*\.[mM]3[uU]8[^"\']*)["\']', flat):
            try:
                cand = urljoin(final, m.group(1).strip())
            except Exception:
                continue
            if cand not in found and _is_hls_like_fetchv_url(cand):
                found.append(cand)
        if not found:
            try:
                print(f'[GENERIC_JAV] FetchV fresh embed: no stream URLs '
                      f'in {final[:110]} (packed scripts decoded: '
                      f'{len(packed)})')
            except Exception:
                pass
            continue

        # The context the player itself would use from inside the iframe.
        try:
            ep = urlparse(final)
            eorigin = f'{ep.scheme or "https"}://{ep.netloc}'
            ehost = (ep.netloc or '').lower()
        except Exception:
            eorigin, ehost = '', ''
        # Duration from the player config when it carries one (the
        # playlists stay unfetched, so this is the only source).
        dur = 0.0
        for dm in re.finditer(
                r'duration[\"\']?\s*[:=]\s*[\"\']?(\d{2,6})\b',
                flat, re.IGNORECASE):
            try:
                dv = float(dm.group(1))
            except Exception:
                continue
            if 30 <= dv <= 172800:
                dur = dv
                break
        for su in found[:4]:
            try:
                shost = (urlparse(su).netloc or '').lower()
            except Exception:
                shost = ''
            if ehost and ehost == shost:
                site = 'same-origin'
            elif (ehost and shost
                  and _registrable_domain(ehost) == _registrable_domain(shost)):
                site = 'same-site'
            else:
                site = 'cross-site'
            # Stored WITHOUT fetching the URL: the token was minted for
            # THIS embed load, and if tokens are single-use the first
            # request must be playback, not a probe. Context headers
            # below are exactly what the iframe player sends.
            hdrs = {
                'User-Agent': _UA,
                'Referer': final,
                'Origin': eorigin,
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': site,
            }
            if cookie_pairs:
                hdrs['Cookie'] = '; '.join(
                    f'{k}={v}' for k, v in cookie_pairs.items())
            out[su] = {'duration': dur, 'headers': dict(hdrs)}
            try:
                print(f'[GENERIC_JAV] FetchV fresh embed: {su[:110]} '
                      f'via {final[:70]} [{site}] ({dur:.0f}s, kept '
                      f'unfetched for playback)')
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# In-app embed-browser capture (R40). Last resort for CDNs whose minted
# tokens cannot be replayed outside the browser session that made them:
# run the embed page in the app's own Chromium (QtWebEngine), let its
# JavaScript do the unpacking/handshake, and intercept the manifest
# request at the network layer. Works no matter how the site obfuscates
# its player config - we never parse it at all.
# ---------------------------------------------------------------------------

_EMBED_BROWSER_WAIT_S = 95.0
_EMBED_CAPTURE_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) '
                     'Chrome/131.0.0.0 Safari/537.36')

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView as _EWView
    from PyQt6.QtWebEngineCore import (
        QWebEngineProfile as _EWProfile,
        QWebEnginePage as _EWPage,
        QWebEngineUrlRequestInterceptor as _EWInterceptor,
    )
    _EMBED_CAPTURE_BROWSER_AVAILABLE = True
except Exception:
    _EWView = _EWProfile = _EWPage = _EWInterceptor = None
    _EMBED_CAPTURE_BROWSER_AVAILABLE = False

if _EMBED_CAPTURE_BROWSER_AVAILABLE:

    class _EmbedStreamInterceptor(_EWInterceptor):
        """Emits every manifest URL (.m3u8 / tokenized .mp4) the loaded
        page requests. Runs on WebEngine's IO thread - only ever emits
        a signal here, like the GoFile interceptor."""
        stream_url_seen = pyqtSignal(str)

        def interceptRequest(self, info):
            try:
                url = info.requestUrl().toString()
                low = url.lower()
                if '.m3u8' in low or ('.mp4' in low
                                      and ('token' in low or '?' in low)):
                    self.stream_url_seen.emit(url)
            except Exception:
                pass


    class _EmbedCapturePage(_EWPage):
        """Popup-free page: dood-family embeds window.open() ad windows
        on click; createWindow() -> None swallows them all."""

        def createWindow(self, _type):
            return None

        def javaScriptConsoleMessage(self, level, message, line_number,
                                     source_id):
            try:
                print(f'[GENERIC_JAV][EMBED_BROWSER][JS] '
                      f'{str(message)[:160]}')
            except Exception:
                pass

else:
    _EmbedStreamInterceptor = None
    _EmbedCapturePage = None


_EMBED_CLICK_JS = (
    "(function(){var s=['.vjs-big-play-button','.jw-icon-display',"
    "'.jw-display-icon-display','.play-btn','#vplayer .play',"
    "'.vjs-play-button','video'];"
    "for(var i=0;i<s.length;i++){try{var e=document.querySelector(s[i]);"
    "if(e){e.click();}}catch(x){}}"
    "try{var v=document.querySelector('video');"
    "if(v&&v.play){var p=v.play();if(p&&p.catch)p.catch(function(){});}}"
    "catch(x){}})()"
)


def _vp_open_embed_capture_browser(self, embeds, box, done_event):
    """UI-thread slot behind generic_jav_embed_browser_capture.

    Opens the embed page in an off-the-record QtWebEngine dialog with a
    Chrome UA, auto-clicks the player's play button (Chromium's autoplay
    policy may still require a human click - the hint label says so),
    and captures the manifest URL(s) the page's own player requests via
    a network-level interceptor, plus the session cookies the profile
    collected. Result goes into box['streams']/box['headers'] (headers
    = the exact context the in-page player used: Referer=embed page,
    Origin, Cookie, Chrome UA, Sec-Fetch-Site); done_event is set when
    finished either way, so the FetchV worker can continue.
    """
    if not _EMBED_CAPTURE_BROWSER_AVAILABLE:
        box['error'] = 'QtWebEngine unavailable'
        done_event.set()
        return

    from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel
    from PyQt6.QtCore import QTimer, QUrl

    state = {'urls': [], 'cookies': {}, 'page': '', 'done': False,
             'queue': [str(u) for u in (embeds or []) if u]}
    timers = []
    refs = {}

    def _finish():
        if state['done']:
            return
        state['done'] = True
        for t in timers:
            try:
                t.stop()
            except Exception:
                pass
        try:
            urls, seen = [], set()
            for u in state['urls']:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            urls.sort(key=lambda u: 0 if 'master.m3u8' in u.lower() else 1)
            if urls:
                page = state['page'] or (state['queue'] or [''])[0]
                try:
                    p = urlparse(page)
                    origin = f'{p.scheme or "https"}://{p.netloc}'
                    phost = (p.netloc or '').lower()
                except Exception:
                    origin, phost = '', ''
                try:
                    shost = (urlparse(urls[0]).netloc or '').lower()
                except Exception:
                    shost = ''
                if phost and phost == shost:
                    site = 'same-origin'
                elif (phost and shost
                      and _registrable_domain(phost)
                      == _registrable_domain(shost)):
                    site = 'same-site'
                else:
                    site = 'cross-site'
                headers = {
                    'User-Agent': _EMBED_CAPTURE_UA,
                    'Referer': page,
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': site,
                }
                if origin:
                    headers['Origin'] = origin
                cookie = '; '.join(
                    f'{k}={v}' for k, v in state['cookies'].items())
                if cookie:
                    headers['Cookie'] = cookie
                box['streams'] = urls
                box['headers'] = headers
                print(f'[GENERIC_JAV] Embed browser captured '
                      f'{len(urls)} URL(s) [{site}]: {urls[0][:110]}')
            else:
                box['error'] = ('no stream URL requested by the page '
                                '(play never started?)')
        except Exception as exc:
            box['error'] = f'capture error: {exc}'
        for w in (refs.get('view'), refs.get('dlg')):
            if w is None:
                continue
            try:
                w.close()
            except Exception:
                pass
            try:
                w.deleteLater()
            except Exception:
                pass
        if getattr(self, '_generic_jav_embed_capture_dialog', None) \
                is refs.get('dlg'):
            self._generic_jav_embed_capture_dialog = None
        done_event.set()

    if not state['queue']:
        box['error'] = 'no embed URL'
        done_event.set()
        return

    try:
        dlg = QDialog(self)
        dlg.setWindowTitle('Fetching stream link…')
        dlg.resize(960, 620)
        lay = QVBoxLayout(dlg)
        hint = QLabel(
            'Capture browser (R47: always off-screen, never on the '
            'desktop). Loading the video page to capture a fresh stream '
            'link; the Play button is clicked automatically and the '
            'window closes itself as soon as the link is captured.')
        hint.setWordWrap(True)
        lay.addWidget(hint)
        view = _EWView()
        lay.addWidget(view, 1)
        refs['dlg'] = dlg
        refs['view'] = view
        self._generic_jav_embed_capture_dialog = dlg

        profile = _EWProfile(view)  # off-the-record (in-memory)
        profile.setHttpCacheType(_EWProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(
            _EWProfile.PersistentCookiesPolicy.NoPersistentCookies)
        profile.setHttpUserAgent(_EMBED_CAPTURE_UA)

        interceptor = _EmbedStreamInterceptor(profile)
        finalize_timer = QTimer(dlg)
        finalize_timer.setSingleShot(True)
        finalize_timer.timeout.connect(_finish)
        timers.append(finalize_timer)

        def _on_stream_url(url):
            if state['done']:
                return
            state['urls'].append(str(url))
            # first manifest seen: give the player ~2s to also request
            # the variant playlist, then hand everything to the worker
            if not finalize_timer.isActive():
                finalize_timer.start(1800)

        interceptor.stream_url_seen.connect(_on_stream_url)
        profile.setUrlRequestInterceptor(interceptor)

        def _on_cookie(cookie):
            try:
                name = bytes(cookie.name()).decode('utf-8', 'ignore')
                value = bytes(cookie.value()).decode('utf-8', 'ignore')
                if name:
                    state['cookies'][name] = value
            except Exception:
                pass

        profile.cookieStore().cookieAdded.connect(_on_cookie)

        page = _EmbedCapturePage(profile, view)
        view.setPage(page)

        stall_timer = QTimer(dlg)
        stall_timer.setSingleShot(True)
        timers.append(stall_timer)

        def _load_next():
            if state['done'] or not state['queue']:
                return
            nxt = state['queue'].pop(0)
            print(f'[GENERIC_JAV] Embed browser loading {nxt}')
            view.load(QUrl(nxt))
            # this embed produced nothing after 15s (e.g. a 404 error
            # page on the wrong subdomain) -> try the next candidate
            stall_timer.start(15000)

        def _on_load_finished(ok):
            if state['done']:
                return
            state['page'] = view.url().toString()
            if not ok:
                _load_next()

        view.loadFinished.connect(_on_load_finished)
        view.urlChanged.connect(
            lambda u: state.__setitem__('page', u.toString()))

        click_timer = QTimer(dlg)
        clicks = {'n': 0}
        timers.append(click_timer)

        def _try_click():
            if state['done']:
                return
            clicks['n'] += 1
            try:
                page.runJavaScript(_EMBED_CLICK_JS)
            except Exception:
                pass
            if clicks['n'] < 12:
                click_timer.start(800)

        view.loadFinished.connect(lambda ok: click_timer.start(400))
        click_timer.timeout.connect(_try_click)

        global_timer = QTimer(dlg)
        global_timer.setSingleShot(True)
        global_timer.timeout.connect(_finish)
        global_timer.start(90000)
        timers.append(global_timer)

        # user closed the dialog (X == reject) -> finish with what we have
        dlg.rejected.connect(_finish)

        _load_next()
        # R47 standing rule: capture browser windows are NEVER shown on
        # the screen. Real window (a headless engine gets fingerprinted
        # and some players never start playback), but parked far
        # off-screen exactly like the missav/eporner capture windows:
        # the page stays 'visible' to Chromium, so the player and the
        # JS auto-clicker keep working while nothing appears on the
        # desktop. No raise_/activateWindow and no focus steal — it
        # must never jump on top, not even for a dead link (file
        # deleted / hoster down).
        try:
            from PyQt6.QtCore import Qt as _Qt
            dlg.setWindowFlag(_Qt.WindowType.WindowDoesNotAcceptFocus, True)
        except Exception:
            pass
        dlg.move(-32000, -32000)
        dlg.show()
        dlg.move(-32000, -32000)
    except Exception as exc:
        box['error'] = f'browser open failed: {exc}'
        _finish()


# ---------------------------------------------------------------------------
# R41: FetchV pastes are processed ONE AT A TIME, app-wide. A single
# dispatcher thread owns the paste queue; the next paste does not even
# start probing until the previous paste's row(s) have been added to the
# playlist (ack via _FETCHV_ACK_EVENTS, set by the UI-thread handler
# after the last group's rows land). Consecutive rapid pastes used to
# spawn parallel workers that raced each other (overlapping embed
# dialogs, interleaved probes) - "some links treated incorrectly".
# ---------------------------------------------------------------------------

_FETCHV_ACK_SEQ = [0]
_FETCHV_ACK_EVENTS = {}
_FETCHV_PASTE_STATE = {'queue': [], 'thread': None}
_FETCHV_PASTE_LOCK = threading.Lock()


def _fetchv_paste_dispatch(self, urls, play_first, origin_urls):
    """Enqueue a FetchV paste for the single dispatcher thread."""
    with _FETCHV_PASTE_LOCK:
        _FETCHV_PASTE_STATE['queue'].append(
            (list(urls), bool(play_first), list(origin_urls or [])))
        thr = _FETCHV_PASTE_STATE['thread']
        if thr is not None and thr.is_alive():
            return
        _FETCHV_PASTE_STATE['thread'] = threading.Thread(
            target=_fetchv_paste_dispatcher_loop, args=(self,), daemon=True)
        _FETCHV_PASTE_STATE['thread'].start()


def _fetchv_paste_dispatcher_loop(self):
    while True:
        with _FETCHV_PASTE_LOCK:
            if not _FETCHV_PASTE_STATE['queue']:
                return
            urls, play_first, origin_urls = _FETCHV_PASTE_STATE['queue'].pop(0)
        _FETCHV_ACK_SEQ[0] += 1
        ack = f'fetchv-ack-{_FETCHV_ACK_SEQ[0]}'
        _FETCHV_ACK_EVENTS[ack] = threading.Event()
        try:
            self.link_flow_note.emit(urls[0], 'analyzing',
                                     f'FetchV paste ({len(urls)} links)')
        except Exception:
            pass
        try:
            _fetchv_capture_worker(self, urls, play_first, origin_urls,
                                   ack_id=ack)
        except Exception as exc:
            print(f'[GENERIC_JAV] FetchV paste worker failed: {exc}')
        # wait until the UI thread has actually added this paste's rows
        ev = _FETCHV_ACK_EVENTS.get(ack)
        if ev is not None:
            try:
                ev.wait(timeout=20)
            finally:
                _FETCHV_ACK_EVENTS.pop(ack, None)
        # rows are in (or the ack timed out): release the one-link-at-a-
        # time clipboard gate hold and start the next paste
        try:
            self.link_flow_note.emit(urls[0], 'done',
                                     'merged into playlist')
        except Exception:
            pass
        try:
            self.generic_jav_link_flow_done.emit()
        except Exception:
            pass


def _vp_handle_fetchv_capture(self, urls, play_first=True, origin_urls=None):
    """Merge FetchV-captured direct stream links into playlist entries.

    Links pasted together are mirrors of the same video (that is how the
    extension is used: capture one video's streams, paste them all):
    a master.m3u8 wins as the playback target, everything else becomes a
    mirror of the same row. When a JAV code is embedded in any link
    (tapecontent mirror filenames carry it) the row is titled with it so
    renaming is easy. HLS links are tagged 'fetchv_capture' so
    _apply_resolved_remote_stream routes them through the local proxy
    (PNG-unwrap + consistent headers) - that is what fixes 'shows
    duration, stuck at end' playback on these CDNs. mp4/direct files
    (tapecontent, cloudatacdn) are not HLS and keep playing direct.

    origin_urls: javdock/javhdporn PAGE URLs pasted along with the links.
    They tell the app which site the links came from (its page is the
    Referer the embedded player sends - the CDNs hotlink-protect and a
    wrong/absent Referer is rejected). They are NOT opened or grabbed:
    the user already has the links. Runs in a worker thread because the
    probe does network I/O."""
    urls = [u for u in (urls or []) if _looks_like_fetchv_stream_url(u)]
    if not urls:
        return False
    origins = [u for u in (origin_urls or []) if _is_generic_jav_url(u)]
    try:
        self.show_osd(f"Merging {len(urls)} FetchV link(s)…", duration=3000)
    except Exception:
        pass
    # R41: hold the one-link-at-a-time clipboard gate until this paste
    # is fully treated (rows in the playlist); the dispatcher releases
    # it via generic_jav_link_flow_done after the UI ack.
    gate = getattr(self, '_begin_link_add_flow', None)
    if gate is not None:
        try:
            gate()
        except Exception:
            pass
    _fetchv_paste_dispatch(self, urls, bool(play_first), origins)
    return True


def _fetchv_capture_worker(self, urls, play_first, origin_urls, ack_id=None):
    """Background half of the FetchV paste flow (see above). Probes each
    HLS link with the origin Referers to discover what the CDN accepts,
    then emits the merged entry via the capture-ready signal. Runs on
    the R41 paste dispatcher - one paste at a time; ack_id marks the
    last emitted group so the UI can acknowledge rows-added back to the
    dispatcher before the next paste starts."""
    groups = {}
    order = []
    for u in urls:
        code = _jav_code_from_stream_url(u)
        key = ('code', code) if code else ('nocode',)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(u)
    # a single coded group + a single code-less group = one video whose
    # mp4 mirror carries the code but whose HLS links do not -> merge
    if (len(groups) == 2 and ('nocode',) in groups
            and any(k[0] == 'code' for k in groups)):
        coded = next(k for k in groups if k[0] == 'code')
        groups[coded] = groups[coded] + groups[('nocode',)]
        del groups[('nocode',)]
        order.remove(('nocode',))

    # Page title of the first pasted origin - its JAV code names the
    # entry when the stream links themselves carry none.
    page_title = ''
    if origin_urls:
        try:
            page_title = _fetch_page_title(origin_urls[0])
        except Exception:
            page_title = ''
    title_code = ''
    if page_title:
        m = _JAV_CODE_RE.search(page_title)
        if m:
            title_code = m.group(1).upper()

    def _rank(x):
        low = str(x).lower()
        is_master = 'master.m3u8' in low
        is_variant = bool(re.search(r'index-f\d+-v\d+', low))
        is_hls = _is_hls_like_fetchv_url(low)
        return 0 if is_master else 1 if is_variant else 2 if is_hls else 3

    added = 0
    for key in order:
        stream_list = []
        seen = set()
        for u in groups[key]:
            for cand in (u, _derive_fetchv_master(u)):
                if cand and cand not in seen:
                    seen.add(cand)
                    stream_list.append(cand)
        stream_list.sort(key=_rank)

        # Probe the HLS candidates: discovers the accepted Referer/cookies
        # per CDN (embed page / origin page / javdock / javhdporn / none)
        # and real durations, so the longest full stream lands first and
        # the working headers are stored for playback. The embed pages the
        # player iframes run in are harvested from the origin page HTML -
        # the browser sends THOSE as Referer (field: maxstream 403s even a
        # Chrome-TLS request with the video page as Referer).
        probe_cands = [u for u in stream_list if _is_hls_like_fetchv_url(u)][:6]
        embeds, embed_cookies = [], {}
        if probe_cands:
            try:
                # DERIVED embeds first - the maxstream/doodstream family
                # puts the video id right in the HLS path, so /e/<id> on
                # the stream host (streamhls placement) AND on the apex
                # domain (maxstream placement - the CDN subdomains 404
                # /e/) are the player iframes the browser uses as
                # Referer. The static page HTML carries no iframe
                # (injected at render time), so the harvest alone found
                # only analytics scripts.
                derived = []
                for pc in probe_cands[:2]:
                    for d in _derive_fetchv_embeds(pc):
                        if d not in derived:
                            derived.append(d)
                if derived:
                    print(f'[GENERIC_JAV] FetchV derived embed: '
                          f'{", ".join(d[:80] for d in derived[:4])}')
                d_cookies, d_finals = _warm_embed_cookies(
                    derived, (origin_urls or [''])[0])
                harvested, h_cookies = _fetchv_embed_candidates(
                    origin_urls, probe_cands[0])
                embeds = (d_finals or derived) + harvested
                embed_cookies = dict(h_cookies)
                for host, jar in d_cookies.items():
                    base = embed_cookies.setdefault(host, [])
                    embed_cookies[host] = jar + [c for c in base if c not in jar]
                if embeds:
                    print(f'[GENERIC_JAV] FetchV embed candidates: '
                          f'{", ".join(e[:70] for e in embeds[:3])}')
            except Exception as exc:
                print(f'[GENERIC_JAV] embed discovery failed: {exc}')
        try:
            probed = _probe_fetchv_streams(
                probe_cands, origin_urls,
                embed_referers=embeds, cookies_by_host=embed_cookies)
        except Exception as exc:
            print(f'[GENERIC_JAV] FetchV probe failed: {exc}')
            probed = {}
        # maxstream-style CDNs mint the t= token per embed-page LOAD for
        # THAT client's context - the browser-captured token can never be
        # replayed by the app, so no Referer combination will ever
        # authorize it. When nothing probed, re-load the embed pages
        # OURSELVES and harvest a FRESH token bound to our context.
        if not probed and embeds:
            try:
                fresh = _fetchv_fresh_embed_streams(
                    embeds, (origin_urls or [''])[0])
            except Exception as exc:
                print(f'[GENERIC_JAV] FetchV fresh embed failed: {exc}')
                fresh = {}
            if fresh:
                probed = dict(fresh)
                for fu in fresh:
                    if fu not in stream_list:
                        stream_list.append(fu)
        # LAST resort: nothing replayable was found anywhere - run the
        # embed page in the in-app browser and intercept the manifest
        # request the page's own player makes. The engine executes
        # whatever handshake the CDN requires (packed configs, per-load
        # tokens), so this works even when static parsing cannot.
        if not probed and embeds and probe_cands:
            box, ev = {}, threading.Event()
            sig = getattr(self, 'generic_jav_embed_browser_capture', None)
            if sig is not None:
                try:
                    apex, rest = [], []
                    try:
                        rdom = _registrable_domain(
                            urlparse(probe_cands[0]).netloc or '')
                    except Exception:
                        rdom = ''
                    for e in embeds:
                        try:
                            if rdom and _registrable_domain(
                                    urlparse(e).netloc) == rdom:
                                apex.append(e)
                            else:
                                rest.append(e)
                        except Exception:
                            rest.append(e)
                    ordered = (apex + rest)[:2]
                    if ordered:
                        print(f'[GENERIC_JAV] No replayable link found - '
                              f'opening embed page in-app: {ordered[0]}')
                        try:
                            self.link_flow_note.emit(
                                probe_cands[0], 'in app browser',
                                'capturing fresh link from embed page')
                        except Exception:
                            pass
                        sig.emit(ordered, box, ev)
                        ev.wait(timeout=_EMBED_BROWSER_WAIT_S)
                except Exception as exc:
                    print(f'[GENERIC_JAV] embed browser capture failed: '
                          f'{exc}')
            b_streams = box.get('streams') or []
            if b_streams:
                b_headers = box.get('headers') or {}
                probed = {u: {'duration': 0.0, 'headers': dict(b_headers)}
                          for u in b_streams}
                for u in b_streams:
                    if u not in stream_list:
                        stream_list.append(u)
            elif box.get('error'):
                print(f'[GENERIC_JAV] embed browser capture: '
                      f'{box["error"]}')
        if probed:
            probed_urls = [u for u in stream_list if u in probed]
            probed_urls.sort(
                key=lambda u: (probed[u]['duration'], 'master.m3u8' in u.lower()),
                reverse=True)
            stream_list = probed_urls + [u for u in stream_list if u not in probed]

        code = key[1] if key[0] == 'code' else ''
        # The origin page's title names the video the user is actually
        # watching; a mirror's filename code can be stale or mislabeled,
        # so for the first group the page-title code wins when both
        # exist. Without an origin page the filename code rules.
        title = ((title_code if added == 0 else '') or code
                 or _fetchv_fallback_title(stream_list[0]))
        payload = {
            'streams': stream_list,
            'preview_only': False,
            'proxy_provider': 'fetchv_capture',
            # per-stream headers the probe found working; ''-Referer
            # entries ({} headers) mean the CDN wants NO Referer
            'stream_headers': {
                u: dict(probed[u]['headers']) for u in probed
            },
            # R50: the jav page the links came from — the playlist rows
            # show the SITE's icon next to the HOSter's icon.
            '_origin_site': (origin_urls or [''])[0],
        }
        if ack_id and added == len(order) - 1:
            # R41: last group of this paste - the UI handler sets the
            # matching ack event once these rows are in the playlist,
            # which is what lets the dispatcher start the next paste.
            payload['_ack'] = ack_id
        added += 1
        self.generic_jav_capture_ready.emit(
            stream_list[0], json.dumps(payload), title,
            bool(play_first and added == 1))
    if added:
        try:
            # generic_jav_osd, not show_osd: we are on a worker thread
            self.generic_jav_osd.emit(f"Added {added} FetchV stream group(s)", 2600)
        except Exception:
            pass


def _brave_cache_dirs():
    """All Brave profile cache dirs on this machine."""
    dirs = []
    local = os.environ.get('LOCALAPPDATA', '')
    if not local:
        return dirs
    base = os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data')
    if not os.path.isdir(base):
        return dirs
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return dirs
    for name in names:
        p = os.path.join(base, name, 'Cache', 'Cache_Data')
        if os.path.isdir(p):
            dirs.append(p)
    return dirs


def _scan_cache_dirs_for_m3u8(cache_dirs, since_ts):
    """Return {url: mtime} for ANY m3u8 URL in recently-written cache
    files. The URL is the cache entry key - stored in plaintext at the
    start of each simple-cache entry file."""
    found = {}
    for d in cache_dirs:
        try:
            entries = list(os.scandir(d))
        except Exception:
            continue
        for ent in entries:
            try:
                if not ent.is_file():
                    continue
                st = ent.stat()
                if st.st_mtime < since_ts - 5 or st.st_size > 50 * 1024 * 1024:
                    continue
                with open(ent.path, 'rb') as f:
                    blob = f.read(65536)
                for frag in _iter_m3u8_urls(blob):
                    u = frag.decode('utf-8', 'replace')
                    if u not in found or st.st_mtime > found[u]:
                        found[u] = st.st_mtime
            except Exception:
                continue
    return found


def _derive_streamhls_candidates(urls):
    """From any captured streamhls URL, also derive its master.m3u8
    sibling (same directory) - the variant may be captured while the
    master was served with no-store. Non-streamhls URLs (tapecontent…)
    pass through unchanged: their master path is not a predictable
    pattern and a wrong sibling would just be a dead mirror."""
    out = []
    seen = set()
    for u in urls:
        if 'streamhls.click' in str(u):
            cands = (u, u.rsplit('/', 1)[0] + '/master.m3u8')
        else:
            cands = (u,)
        for cand in cands:
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def _vp_main_brave_cache_capture(self, source_url, title=''):
    """Open source_url in the user's main Brave, auto-click Play via
    OS-level UI automation, and capture EVERY m3u8 the browser requests.

    Preferred channel: a --log-net-log network log (records EVERY request
    URL, including the no-store HLS manifests the disk cache never holds
    - that is why the previous cache-only scan timed out while FetchV, an
    in-browser extension, captured the link fine). Fallback: disk-cache
    scan (used when Brave was already running and the user chose not to
    restart it). The sites serve some videos from streamhls.click and
    others from tapecontent/streamtape CDNs, so the capture is no longer
    streamhls-specific: the finalizer probes each m3u8's duration and
    keeps the longest ones."""
    # Prefer the EXACT Brave binary the user is running right now, then
    # the app's discovery as fallback.
    brave = _running_brave_path()
    if not brave:
        try:
            brave = self._brave_executable_path()
        except Exception:
            brave = ''
    if not brave:
        print('[GENERIC_JAV] main-Brave capture: Brave not found')
        return None
    print(f'[GENERIC_JAV] using Brave at: {brave}')

    netlog = ''
    try:
        netlog = _ensure_brave_netlog(self, brave, source_url)
    except Exception as exc:
        print(f'[GENERIC_JAV] net-log setup failed: {exc}')

    urls = {}

    def _collect(new_urls):
        for u in new_urls:
            if not _looks_like_m3u8(u) or _is_ad_m3u8(u):
                continue
            urls.setdefault(u, time.time())

    keyword = _title_keyword_from_url(source_url, title)

    def _autoclick_tick(clicks, started):
        """One best-effort Play click per ~15 s until an m3u8 appears
        (a click AFTER playback started would only pause the video).
        Without a title keyword the window search cannot be pinned to
        the video tab, so we do not click at all rather than click in
        the wrong window."""
        if urls or clicks['left'] <= 0 or not keyword:
            return
        if time.time() - started < 6:
            return
        if clicks['last'] and time.time() - clicks['last'] < 15:
            return
        clicks['left'] -= 1
        clicks['last'] = time.time()
        res = _autoclick_play_in_main_brave(keyword)
        if res == 'clicked':
            try:
                self.generic_jav_osd.emit('Play clicked in your Brave — capturing…', 4000)
            except Exception:
                pass
        elif not clicks['warned']:
            clicks['warned'] = True
            try:
                self.generic_jav_osd.emit(
                    'Could not auto-click — please click Play in the Brave window', 7000)
            except Exception:
                pass

    if netlog:
        try:
            subprocess.Popen([brave, source_url])
        except Exception as exc:
            print(f'[GENERIC_JAV] main-Brave launch failed: {exc}')
            return None
        try:
            self.generic_jav_osd.emit(
                'Opened in your Brave — the app clicks Play for you (or click it yourself)', 8000)
        except Exception:
            pass
        print('[GENERIC_JAV] main-Brave capture: tailing the network log for m3u8s…')
        start = time.time()
        cursor = 0
        first_hit = None
        clicks = {'left': 4, 'last': None, 'warned': False}
        while time.time() - start < 240:
            time.sleep(3)
            found, cursor = _scan_netlog_for_m3u8(netlog, cursor)
            _collect(found)
            _autoclick_tick(clicks, start)
            if urls and first_hit is None:
                first_hit = time.time()
            if first_hit is not None:
                if len(urls) >= 2:
                    # a second manifest (variant/master, or the full
                    # movie following the preview) - grab it, finalise
                    time.sleep(4)
                    found, cursor = _scan_netlog_for_m3u8(netlog, cursor)
                    _collect(found)
                    break
                if time.time() - first_hit >= 30:
                    break
    else:
        # No net-log (Brave was running and the user did not restart it):
        # fall back to the disk-cache scan.
        cache_dirs = _brave_cache_dirs()
        if not cache_dirs:
            print('[GENERIC_JAV] main-Brave capture: no capture channel available')
            return None
        try:
            subprocess.Popen([brave, source_url])
        except Exception as exc:
            print(f'[GENERIC_JAV] main-Brave launch failed: {exc}')
            return None
        try:
            self.generic_jav_osd.emit(
                'Opened in your Brave — the app clicks Play for you (or click it yourself)', 8000)
        except Exception:
            pass
        print('[GENERIC_JAV] main-Brave capture: scanning Brave cache for m3u8s…')
        start = time.time()
        first_hit = None
        clicks = {'left': 4, 'last': None, 'warned': False}
        while time.time() - start < 200:
            time.sleep(3)
            _collect(list(_scan_cache_dirs_for_m3u8(cache_dirs, start).keys()))
            _autoclick_tick(clicks, start)
            if urls and first_hit is None:
                first_hit = time.time()
            if first_hit is not None:
                if len(urls) >= 2:
                    time.sleep(4)
                    _collect(list(_scan_cache_dirs_for_m3u8(cache_dirs, start).keys()))
                    break
                if time.time() - first_hit >= 25:
                    break

    if not urls:
        print('[GENERIC_JAV] main-Brave capture: nothing captured (timeout)')
        return None
    return _finalize_m3u8_capture(list(urls), source_url, title)


# ── Methods injected into VideoPlayer ─────────────────────────────────────────

def _vp_is_generic_jav_url(self, url: str) -> bool:
    return _is_generic_jav_url(url)


def _vp_launch_generic_jav_grab(self, source_url: str, play_first: bool = True) -> bool:
    if not _is_generic_jav_url(source_url):
        return False

    import queue
    q = getattr(self, '_generic_jav_queue', None)
    if q is None:
        self._generic_jav_queue = queue.Queue()
        q = self._generic_jav_queue

    pending: set = getattr(self, '_generic_jav_pending', set())
    if not hasattr(self, '_generic_jav_pending'):
        self._generic_jav_pending = pending
        
    pending_key = source_url.lower()
    if pending_key in pending:
        self.show_osd("Generic JAV link is already queued…", duration=2200)
        return True

    pending.add(pending_key)
    # R41: hold the one-link-at-a-time clipboard gate until this grab
    # finishes (capture_ready/capture_failed); released via the
    # generic_jav_link_flow_done emit in the grab worker's finally.
    gate = getattr(self, '_begin_link_add_flow', None)
    if gate is not None:
        try:
            gate()
        except Exception:
            pass
    q.put((source_url, play_first, pending_key))
    
    qsize = q.qsize()
    if qsize > 1:
        self.show_osd(f"Link queued ({qsize} pending)", duration=3000)
    elif 'javdock.com' in source_url or 'javhdporn.net' in source_url:
        self.show_osd("Opening in your Brave — the app will click Play…", duration=7000)
    else:
        self.show_osd("Grabbing streams…", duration=10_000)

    worker_thread = getattr(self, '_generic_jav_worker_thread', None)
    if worker_thread is None or not worker_thread.is_alive():
        def _worker_loop():
            while True:
                try:
                    item = self._generic_jav_queue.get_nowait()
                except queue.Empty:
                    break

                src_url, p_first, p_key = item
                self._remote_analysis_begin()
                try:
                    import importlib.util

                    # Use the dedicated anti-Cloudflare grabber in a private,
                    # off-screen capture context.  The old javdock/javhdporn
                    # branch opened the user's main Brave and captured its
                    # network log; that exposed the source page and ad tabs
                    # instead of handing the stream back to this player.
                    # javdock_grab.py still uses a real Chromium engine, but
                    # it now keeps the capture window off-screen and returns
                    # the HLS URL directly to the playlist.
                    if ('javdock.com' in src_url or 'javhdporn.net' in src_url
                            or '123av.com' in src_url):
                        script_name = 'javdock_grab.py'
                    elif 'sextb.net' in src_url:
                        script_name = 'sextb_grab.py'
                    else:
                        script_name = 'generic_jav_grab.py'

                    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
                    if not os.path.exists(script_path):
                        raise FileNotFoundError(f"{script_name} not found at: {script_path}")

                    # Load module in-process (avoids subprocess timeout + encoding bugs)
                    spec = importlib.util.spec_from_file_location(script_name[:-3], script_path)
                    mod  = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)

                    # Cloudflare-protected pages need the user's REAL
                    # installed browser; pass the app's Brave discovery in.
                    try:
                        _brave = self._brave_executable_path()
                    except Exception:
                        _brave = ''
                    if _brave:
                        mod.BROWSER_EXECUTABLE = _brave

                    grab_error = ''
                    try:
                        data = mod.grab_all(src_url) or {}
                    except Exception as grab_exc:
                        grab_error = str(grab_exc)
                        data = {}
                    if data.get('error') and not grab_error:
                        grab_error = str(data['error'])

                    title   = data.get('title') or 'Generic Video'
                    streams = data.get('streams') or []

                    if not streams:
                        raise RuntimeError(grab_error or 'No stream URL captured')

                    payload = {
                        'streams': streams,
                        # True when the browser grabber only saw the site's
                        # ~10 s preview stream (full movie never loaded).
                        'preview_only': bool(data.get('preview_only')),
                    }
                    if script_name == 'javdock_grab.py':
                        # Tags primary + mirrors so the stream rows use the
                        # app's local HLS handling (PNG-unwrapped segments,
                        # consistent Referer) rather than falling back to a
                        # browser navigation.
                        payload['proxy_provider'] = 'javdock_capture'
                    self.generic_jav_capture_ready.emit(src_url, json.dumps(payload), title, bool(p_first))
                except Exception as e:
                    self.generic_jav_capture_failed.emit(src_url, str(e))
                finally:
                    self._remote_analysis_end()
                    try:
                        self._generic_jav_pending.discard(p_key)
                    except Exception:
                        pass
                    # R41: this grab is fully treated (or failed) -
                    # release its one-link-at-a-time clipboard gate hold.
                    try:
                        self.generic_jav_link_flow_done.emit()
                    except Exception:
                        pass

        self._generic_jav_worker_thread = threading.Thread(target=_worker_loop, daemon=True)
        self._generic_jav_worker_thread.start()

    return True


@pyqtSlot(str, str, str, bool)
def _vp_on_generic_jav_capture_ready(self, primary_url: str, streams_json: str, title: str, play_first: bool):
    import json
    # R63: key EVERYTHING (row, mirror group, caches) off the canonical
    # form of the page URL. add_video_to_playlist() canonicalizes before
    # appending, so a raw paste whose canonical form differs (redirect
    # wrappers, case, trailing bits) previously left the mirror group
    # keyed by the raw URL — the row then showed NO mirrors at all.
    try:
        _canon = self._canonicalize_remote_source_url(self._sanitize_url(primary_url))
        if _canon:
            primary_url = _canon
    except Exception:
        pass
    try:
        parsed = json.loads(streams_json)
    except Exception:
        parsed = []
    _origin_site = ''
    _provider = ''
    _stream_headers = {}
    if isinstance(parsed, dict):
        # R41: this is the last group of a FetchV paste - its rows are
        # about to be added below; acknowledge to the paste dispatcher so
        # the NEXT paste can start (one link fully treated at a time).
        _ack = parsed.get('_ack')
        if _ack:
            _ev = _FETCHV_ACK_EVENTS.get(str(_ack))
            if _ev is not None:
                _ev.set()
        # {'streams': [...], 'preview_only': bool} from the browser grabber
        mirror_urls = parsed.get('streams') or []
        preview_only = bool(parsed.get('preview_only'))
        _provider = str(parsed.get('proxy_provider') or '').strip()
        # R50: remember which jav site these rows came from so the
        # playlist can paint the site icon next to the hoster icon.
        _origin_site = str(parsed.get('_origin_site') or '').strip()
        # {'url': {'Referer':…, 'Origin':…}} discovered by the FetchV
        # probe; {} headers = that CDN wants NO Referer
        if isinstance(parsed.get('stream_headers'), dict):
            for _k, _v in parsed['stream_headers'].items():
                if isinstance(_v, dict):
                    _stream_headers[str(_k)] = dict(_v)
        try:
            self.link_flow_note.emit(
                primary_url, 'done',
                f'{len(mirror_urls)} stream(s) captured')
        except Exception:
            pass
    else:
        mirror_urls = parsed
        preview_only = False

    if not mirror_urls:
        self.show_osd("No streams found on page", duration=2500)
        return
    if preview_only:
        print(f"[GENERIC_JAV] only a preview-length stream was captured for {primary_url}")
        self.show_osd("Only the ~10s preview was captured - retry the link", duration=4000)

    # Treat the page URL as the visible playlist entry, but play the first
    # extracted stream directly. Otherwise javdock/javhdporn page URLs fall
    # back into the normal resolver and can hit ad/player gates.
    # Never auto-pick an image asset (a tapecontent thumbnail once won as
    # 'first stream' and the player showed a contact-sheet jpg).
    # R45: prefer a DIRECT stream (m3u8 / media file). An embed-page URL
    # (doodstream/emturbovid/javclan/vidara/... player) is NOT a playable
    # stream — pre-seeding it as playback_url made the row try to play the
    # HTML page. Embed mirrors are seeded with their title only, so they
    # resolve through main.py's per-hoster resolver when played.
    _IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')

    def _is_direct_stream(u):
        u = str(u or '').strip()
        if not u or u.lower().endswith(_IMG_EXTS):
            return False
        if _is_hls_like_fetchv_url(u):
            return True
        try:
            _ext = os.path.splitext(urlparse(u).path)[1].lower()
        except Exception:
            _ext = ''
        return _ext in ('.mp4', '.mkv', '.webm', '.mov', '.m4v', '.mpg', '.mpeg', '.avi', '.ts')

    # R56: JAV site URL must NOT be in mirror list or playlist row.
    # The playlist entry is now the first HOSTER URL (dood/voe/etc.),
    # not the jav aggregator page. The aggregator page is kept only as
    # origin_page for Referer and site-icon purposes. This fixes:
    # - jav URL stuck at top as "Current" mirror
    # - VOE getting ads / opening jav site in browser window
    playback_url = ''
    for _cand in mirror_urls:
        if _is_direct_stream(_cand):
            playback_url = str(_cand or '').strip()
            break

    # Choose visible playlist entry: prefer direct stream, else first hoster
    visible_url = ''
    if playback_url:
        visible_url = playback_url
    elif mirror_urls:
        visible_url = str(mirror_urls[0] or '').strip()
    else:
        visible_url = primary_url

    try:
        visible_url = self._canonicalize_remote_source_url(self._sanitize_url(visible_url)) or visible_url
    except Exception:
        pass

    # Mirrors = all hoster URLs except the visible one
    remaining_mirrors = []
    seen_rm = set()
    for u in mirror_urls:
        try:
            cu = self._canonicalize_remote_source_url(self._sanitize_url(u)) or u
        except Exception:
            cu = u
        if not cu:
            continue
        # skip the visible entry itself
        try:
            if self._mirror_path_key(cu) == self._mirror_path_key(visible_url):
                continue
        except Exception:
            if cu == visible_url:
                continue
        low = cu.lower()
        if low in seen_rm:
            continue
        seen_rm.add(low)
        remaining_mirrors.append(cu)

    # Site hints: map every hoster to the JAV site for icon purposes
    if _origin_site or primary_url:
        try:
            hints = getattr(self, '_entry_site_hints', None)
            if not isinstance(hints, dict):
                hints = {}
                self._entry_site_hints = hints
            site_for_hint = _origin_site or primary_url
            for _u in [visible_url] + remaining_mirrors:
                try:
                    _key = self._canonicalize_remote_source_url(
                        self._sanitize_url(str(_u or '')))
                except Exception:
                    _key = str(_u or '')
                if _key:
                    hints[_key] = site_for_hint
        except Exception:
            pass

    already_in_playlist = self._playlist_contains_remote_url(visible_url)
    # Also check if the JAV page itself was already added (avoid dupes)
    if not already_in_playlist:
        try:
            if self._playlist_contains_remote_url(primary_url):
                already_in_playlist = True
        except Exception:
            pass

    # Pre-seed the cache with the title + origin_page (JAV site)
    c = getattr(self, '_stream_resolution_cache', {})
    if visible_url not in c:
        c[visible_url] = {'title': title}
    else:
        c[visible_url]['title'] = title
    # Remember JAV page as origin for Referer
    try:
        c[visible_url]['origin_page'] = primary_url
        c[visible_url]['roshy_source_url'] = primary_url
    except Exception:
        pass
    if playback_url and playback_url != visible_url:
        c[visible_url]['playback_url'] = playback_url
        c[visible_url]['pre_resolved_playback_url'] = True
        if _provider:
            c[visible_url]['resolver_provider'] = _provider
        if _is_hls_like_fetchv_url(playback_url):
            c[visible_url]['content_type'] = 'application/vnd.apple.mpegurl'
            _probed = _stream_headers.get(playback_url)
            if _probed is not None:
                c[visible_url]['headers'] = dict(_probed)
                c[visible_url]['headers']['_fetchv_probed'] = True
            elif _is_generic_jav_url(primary_url) or _provider:
                try:
                    c[visible_url]['headers'] = self._contextual_hls_request_headers(
                        playback_url, primary_url)
                except Exception:
                    pass
    # If visible is direct stream, also seed it as its own playback
    elif playback_url and playback_url == visible_url:
        c[visible_url]['playback_url'] = playback_url
        c[visible_url]['pre_resolved_playback_url'] = True
        if _provider:
            c[visible_url]['resolver_provider'] = _provider
        if _is_hls_like_fetchv_url(playback_url):
            c[visible_url]['content_type'] = 'application/vnd.apple.mpegurl'
    self._stream_resolution_cache = c

    if not already_in_playlist:
        self.playlist_widget.setUpdatesEnabled(False)
        try:
            self.add_video_to_playlist(visible_url, batch_mode=True)
        finally:
            self.playlist_widget.setUpdatesEnabled(True)
        self._apply_missav_playlist_name(visible_url, title)

    if remaining_mirrors:
        self._set_mirrors_for_primary(visible_url, remaining_mirrors)
        for m_url in remaining_mirrors:
            m_url_c = self._canonicalize_remote_source_url(self._sanitize_url(m_url))
            if not m_url_c:
                continue
            mirror_info = {'title': title}
            try:
                if primary_url and _is_generic_jav_url(primary_url):
                    mirror_info['origin_page'] = primary_url
                    mirror_info['roshy_source_url'] = primary_url
            except Exception:
                pass
            if _is_direct_stream(m_url_c):
                mirror_info['playback_url'] = m_url_c
                if _provider:
                    mirror_info['resolver_provider'] = _provider
                if _is_hls_like_fetchv_url(m_url_c):
                    mirror_info['content_type'] = 'application/vnd.apple.mpegurl'
                    if m_url_c in _stream_headers:
                        mirror_info['headers'] = dict(_stream_headers[m_url_c])
                        mirror_info['headers']['_fetchv_probed'] = True
            self._merge_stream_info(m_url_c, mirror_info)
            self._remember_recent_file_name(m_url_c, title, save=False)

    try:
        self._stamp_jav_site_identity(
            [visible_url] + remaining_mirrors, _origin_site or primary_url, title)
    except Exception:
        pass

    # R63: merge any older row for the same video (e.g. a hoster embed
    # added earlier via FetchV or a previous session) UNDER the page row
    # now, instead of waiting for a duration-probe collapse to do it.
    # The R63 collapse keeps the jav page as the visible row and absorbs
    # the hoster as a mirror (field: "the javgg link is added as a mirror
    # itself" — the old sort let the playing hoster win the row).
    try:
        if not self._collapse_duplicate_url_mirrors():
            self.apply_playlist_filtering()
    except Exception:
        pass

    try:
        self._refresh_playlist_row_metadata(visible_url)
    except Exception:
        pass

    if play_first and not already_in_playlist:
        self.set_media(visible_url)
        self._start_current_media_playback()

    n_mirrors = len(remaining_mirrors)
    if already_in_playlist:
        self.show_osd("Stream already in playlist", duration=2600)
    else:
        msg = f"Added video: {n_mirrors + 1} stream(s)"
        if n_mirrors:
            msg += f" ({n_mirrors} mirror{'s' if n_mirrors != 1 else ''})"
        self.show_osd(msg, duration=3000)


@pyqtSlot(str, int)
def _vp_on_generic_jav_osd(self, message: str, duration: int):
    """Thread-safe OSD: worker threads emit generic_jav_osd instead of
    touching Qt widgets directly."""
    try:
        self.show_osd(str(message), duration=int(duration or 3000))
    except Exception:
        pass


@pyqtSlot(str, str)
def _vp_on_generic_jav_capture_failed(self, source_url: str, error_message: str):
    error_message = str(error_message or "Unknown error").strip()
    print(f"[GENERIC_JAV] capture failed for {source_url}: {error_message}")
    try:
        self.link_flow_note.emit(source_url, 'failed', error_message[:120])
    except Exception:
        pass
    self.show_osd(f"Grab failed: {error_message[:40]}", duration=3500)


def _vp_queue_add_urls_patched_generic(self, text: str, play_first=True, request_source='manual'):
    text = str(text or '').strip()
    if not text:
        return False

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    target_urls = []
    fetchv_urls = []
    other_lines = []

    for line in lines:
        # FetchV captures first: direct stream links the user copied out
        # of the extension (a line may hold several space-separated URLs).
        tokens = [t for t in re.split(r'\s+', line) if t]
        fetchv_tokens = [t for t in tokens if _looks_like_fetchv_stream_url(t)]
        if fetchv_tokens:
            fetchv_urls.extend(fetchv_tokens)
            leftovers = [t for t in tokens
                         if t not in fetchv_tokens
                         and t.lower().startswith(('http://', 'https://'))]
            if leftovers:
                other_lines.append(' '.join(leftovers))
        elif _is_generic_jav_url(line):
            target_urls.append(line)
        else:
            other_lines.append(line)

    # javdock/javhdporn page URLs pasted TOGETHER with FetchV links are
    # the ORIGIN of those links (the page whose embedded player sent
    # them) - the app uses them as the Referer candidates for the CDNs
    # and does NOT open or grab them: the user already captured the
    # links with the extension. Pasted alone, pages keep their normal
    # capture flow.
    origin_urls = []
    if fetchv_urls and target_urls:
        origin_urls = target_urls
        target_urls = []

    if fetchv_urls:
        try:
            _vp_handle_fetchv_capture(
                self, fetchv_urls,
                play_first=play_first and not target_urls and not other_lines,
                origin_urls=origin_urls)
        except Exception as exc:
            print(f'[GENERIC_JAV] FetchV paste handling failed: {exc}')

    for i, j_url in enumerate(target_urls):
        do_play = play_first and (i == 0) and (not other_lines)
        self._launch_generic_jav_grab(j_url, play_first=do_play)

    if other_lines:
        return self._queue_add_urls_to_playlist_text_original_generic(
            '\n'.join(other_lines), play_first=play_first, request_source=request_source
        )

    return bool(fetchv_urls or target_urls)


# ── Patch application ─────────────────────────────────────────────────────────

def _apply_patch():
    import __main__ as _main_mod
    VP = getattr(_main_mod, 'VideoPlayer', None)
    if VP is None:
        for _mod in list(sys.modules.values()):
            if _mod is None:
                continue
            _cls = getattr(_mod, 'VideoPlayer', None)
            if _cls is not None and isinstance(_cls, type):
                VP = _cls
                break

    if VP is None:
        print("[GENERIC_JAV] WARNING: VideoPlayer class not found.")
        return

    if getattr(VP, '_generic_jav_patch_applied', False):
        return

    VP.generic_jav_capture_ready  = pyqtSignal(str, str, str, bool)
    VP.generic_jav_capture_failed = pyqtSignal(str, str)
    VP.generic_jav_osd            = pyqtSignal(str, int)
    # (embed_urls, result_box, done_event) - worker thread -> UI thread
    VP.generic_jav_embed_browser_capture = pyqtSignal(object, object, object)
    # R41: an async link flow finished (rows in playlist) - releases a
    # one-link-at-a-time clipboard gate hold on the UI thread
    VP.generic_jav_link_flow_done = pyqtSignal()

    VP._is_generic_jav_url                  = _vp_is_generic_jav_url
    VP._launch_generic_jav_grab             = _vp_launch_generic_jav_grab
    VP._on_generic_jav_capture_ready        = _vp_on_generic_jav_capture_ready
    VP._on_generic_jav_capture_failed       = _vp_on_generic_jav_capture_failed
    VP._on_generic_jav_osd                = _vp_on_generic_jav_osd
    VP._on_generic_jav_embed_browser_capture = _vp_open_embed_capture_browser

    def _vp_on_generic_jav_link_flow_done(self):
        end = getattr(self, '_end_link_add_flow', None)
        if end is not None:
            try:
                end()
            except Exception:
                pass
    VP._on_generic_jav_link_flow_done = _vp_on_generic_jav_link_flow_done
    VP._main_brave_cache_capture            = _vp_main_brave_cache_capture

    # We might be patching over another patch (like javguru_integration).
    # This is fine, we just save whatever is currently there.
    original = VP.queue_add_urls_to_playlist_text
    VP._queue_add_urls_to_playlist_text_original_generic = original
    VP.queue_add_urls_to_playlist_text = _vp_queue_add_urls_patched_generic

    original_init = VP.__init__
    def _patched_init(self_vp, *args, **kwargs):
        original_init(self_vp, *args, **kwargs)
        self_vp.generic_jav_capture_ready.connect(self_vp._on_generic_jav_capture_ready)
        self_vp.generic_jav_capture_failed.connect(self_vp._on_generic_jav_capture_failed)
        self_vp.generic_jav_osd.connect(self_vp._on_generic_jav_osd)
        self_vp.generic_jav_embed_browser_capture.connect(
            self_vp._on_generic_jav_embed_browser_capture)
        self_vp.generic_jav_link_flow_done.connect(
            self_vp._on_generic_jav_link_flow_done)

    VP.__init__ = _patched_init

    VP._generic_jav_patch_applied = True
    print(f"[GENERIC_JAV] VideoPlayer patched — URL detection active. (build {_BUILD})")

_apply_patch()
