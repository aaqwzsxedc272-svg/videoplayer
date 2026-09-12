"""
javhd_integration.py
──────────────────────
Monkey-patches VideoPlayer so that pasting a javhd.today URL triggers the
grabber (javhd_grab.grab_all), then adds the video as a playlist item whose title
comes from the page and whose extra streams (e.g. Lulustream m3u8, Streambeast) are
registered as mirrors.

HOW TO ACTIVATE
───────────────
Imported at the bottom of main.py:
    import javhd_integration   # noqa
"""

import os
import sys
import queue
import threading
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot


# ── Utilities ─────────────────────────────────────────────────────────────────

def _is_javhd_url(url: str) -> bool:
    """Return True if *url* is a javhd.today video page we can scrape."""
    try:
        parsed = urlparse(str(url or '').strip())
        host = (parsed.netloc or '').lower().lstrip('www.')
        return 'javhd.today' in host and bool(parsed.path.strip('/'))
    except Exception:
        return False


def _javhd_grab_script_path() -> str:
    """Resolve the path to javhd_grab.py sitting next to main.py."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'javhd_grab.py')


# ── Methods injected into VideoPlayer ─────────────────────────────────────────

def _vp_is_javhd_url(self, url: str) -> bool:
    return _is_javhd_url(url)


def _vp_launch_javhd_grab(self, source_url: str, play_first: bool = True) -> bool:
    """
    Queue grab_all() in a background daemon thread for *source_url*.
    Returns True immediately.
    """
    if not _is_javhd_url(source_url):
        return False

    q = getattr(self, '_javhd_queue', None)
    if q is None:
        self._javhd_queue = queue.Queue()
        q = self._javhd_queue

    pending: set = getattr(self, '_javhd_pending', set())
    if not hasattr(self, '_javhd_pending'):
        self._javhd_pending = pending
        
    pending_key = source_url.lower()
    if pending_key in pending:
        self.show_osd("javhd.today link is already queued…", duration=2200)
        return True

    pending.add(pending_key)
    q.put((source_url, play_first, pending_key))
    
    qsize = q.qsize()
    if qsize > 1:
        self.show_osd(f"javhd.today link queued ({qsize} pending)", duration=3000)
    else:
        self.show_osd("Grabbing javhd.today streams…", duration=15_000)

    worker_thread = getattr(self, '_javhd_worker_thread', None)
    if worker_thread is None or not worker_thread.is_alive():
        def _worker_loop():
            while True:
                try:
                    item = self._javhd_queue.get_nowait()
                except queue.Empty:
                    break
                    
                src_url, p_first, p_key = item
                self._remote_analysis_begin()
                try:
                    grab_script = _javhd_grab_script_path()
                    if not os.path.exists(grab_script):
                        raise FileNotFoundError(f"javhd_grab.py not found at: {grab_script}")
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('javhd_grab', grab_script)
                    mod  = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)

                    title, streams = mod.grab_all(src_url)

                    if not streams:
                        raise RuntimeError("grab_all() returned no streams for javhd.today link")

                    import json
                    streams_json = json.dumps(streams)
                    self._javhd_bridge.capture_ready.emit(src_url, streams_json, title or '', bool(p_first))

                except Exception as exc:
                    print(f"[JAVHD] Failed to grab {src_url}: {exc}")
                    self._javhd_bridge.capture_failed.emit(src_url, str(exc))

                finally:
                    try:
                        self._javhd_pending.discard(p_key)
                    except Exception:
                        pass
                    self._remote_analysis_end()
                    self._javhd_queue.task_done()

        self._javhd_worker_thread = threading.Thread(target=_worker_loop, daemon=True, name='javhd-grab-loop')
        self._javhd_worker_thread.start()

    return True


@pyqtSlot(str, str, str, bool)
def _vp_on_javhd_capture_ready(
    self,
    source_url: str,
    streams_json: str,
    title: str,
    play_first: bool,
):
    """
    Called on the UI thread once grab_all() has finished successfully.
    """
    import json

    try:
        streams: dict = json.loads(streams_json)
    except Exception as exc:
        self._on_javhd_capture_failed(source_url, f"Bad streams payload: {exc}")
        return

    if not streams:
        self._on_javhd_capture_failed(source_url, "No streams in payload")
        return

    stream_items = list(streams.items())          # [(label, url), …]
    primary_label, primary_url = stream_items[0]
    mirror_urls = [url for _, url in stream_items[1:]]

    primary_url = self._canonicalize_remote_source_url(
        self._sanitize_url(primary_url)
    )

    already_in_playlist = self._playlist_contains_remote_url(primary_url)

    if not already_in_playlist:
        self.playlist_widget.setUpdatesEnabled(False)
        try:
            self.add_video_to_playlist(primary_url, batch_mode=True)
        finally:
            self.playlist_widget.setUpdatesEnabled(True)

        if mirror_urls:
            cleaned_mirrors = [
                self._canonicalize_remote_source_url(self._sanitize_url(u))
                for u in mirror_urls
                if u
            ]
            cleaned_mirrors = [u for u in cleaned_mirrors if u]
            if cleaned_mirrors:
                self._set_mirrors_for_primary(primary_url, cleaned_mirrors)

        if not self._collapse_duplicate_url_mirrors():
            self.apply_playlist_filtering()
        self._schedule_remote_duration_probes([primary_url])

    # R57: keep the javhd.today page title + site icon on every hoster
    # row so switching mirrors never replaces them with the embed's name.
    try:
        all_hosters = [primary_url]
        for m_url in (mirror_urls or []):
            m_url_c = self._canonicalize_remote_source_url(self._sanitize_url(m_url))
            if m_url_c:
                all_hosters.append(m_url_c)
        self._stamp_jav_site_identity(all_hosters, source_url, title)
    except Exception:
        if title:
            self._merge_stream_info(primary_url, {'title': title})
            self._remember_recent_file_name(primary_url, title, save=True)

    try:
        self._refresh_playlist_row_metadata(primary_url)
    except Exception:
        pass

    if play_first and not already_in_playlist:
        self.set_media(primary_url)
        self._start_current_media_playback()

    n_mirrors = len(mirror_urls)
    if already_in_playlist:
        self.show_osd("javhd.today stream already in playlist", duration=2600)
    else:
        msg = f"Added javhd.today: {n_mirrors + 1} stream(s)"
        if n_mirrors:
            msg += f" ({n_mirrors} mirror{'s' if n_mirrors != 1 else ''})"
        self.show_osd(msg, duration=3000)


@pyqtSlot(str, str)
def _vp_on_javhd_capture_failed(self, source_url: str, error_message: str):
    error_message = str(error_message or "Unknown error").strip()
    print(f"[JAVHD] capture failed for {source_url}: {error_message}")
    self.show_osd("javhd.today grab failed — check console", duration=3500)


class _JavhdBridge(QObject):
    capture_ready = pyqtSignal(str, str, str, bool)
    capture_failed = pyqtSignal(str, str)


def _vp_queue_add_urls_patched_javhd(self, text: str, play_first=True, request_source='manual'):
    """
    Interceptor for javhd.today URLs.
    """
    text = str(text or '').strip()
    if not text:
        return False

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    javhd_urls = []
    other_lines = []
    
    for line in lines:
        if _is_javhd_url(line):
            javhd_urls.append(line)
        else:
            other_lines.append(line)

    for i, j_url in enumerate(javhd_urls):
        do_play = play_first and (i == 0) and (not other_lines)
        self._launch_javhd_grab(j_url, play_first=do_play)

    if other_lines:
        prev_queue_fn = getattr(self, '_queue_add_urls_to_playlist_text_prev_javhd', None)
        if prev_queue_fn:
            return prev_queue_fn('\n'.join(other_lines), play_first=play_first, request_source=request_source)

    return False


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
        print("[JAVHD] WARNING: VideoPlayer class not found — patch not applied.")
        return

    if getattr(VP, '_javhd_patch_applied', False):
        return

    VP._is_javhd_url            = _vp_is_javhd_url
    VP._launch_javhd_grab       = _vp_launch_javhd_grab
    VP._on_javhd_capture_ready  = _vp_on_javhd_capture_ready
    VP._on_javhd_capture_failed = _vp_on_javhd_capture_failed

    original = VP.queue_add_urls_to_playlist_text
    VP._queue_add_urls_to_playlist_text_prev_javhd = original
    VP.queue_add_urls_to_playlist_text = _vp_queue_add_urls_patched_javhd

    original_init = VP.__init__

    def _patched_init(self_vp, *args, **kwargs):
        original_init(self_vp, *args, **kwargs)
        self_vp._javhd_bridge = _JavhdBridge(self_vp)
        self_vp._javhd_bridge.capture_ready.connect(self_vp._on_javhd_capture_ready)
        self_vp._javhd_bridge.capture_failed.connect(self_vp._on_javhd_capture_failed)

    VP.__init__ = _patched_init

    VP._javhd_patch_applied = True
    print("[JAVHD] VideoPlayer patched — javhd.today URL detection active.")


_apply_patch()
