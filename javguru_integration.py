"""
javguru_integration.py
──────────────────────
Monkey-patches VideoPlayer so that pasting a jav.guru URL triggers the
Playwright grabber (javguru_grab.grab_all), then adds the video as a single
playlist item whose title comes from the page and whose extra streams are
registered as mirrors — exactly the same pattern as the MissAV integration.

HOW TO ACTIVATE
───────────────
Add one line at the very bottom of main.py (after the VideoPlayer class is
fully defined but before   if __name__ == '__main__':   ):

    import javguru_integration   # noqa

That's it.  The patch is applied the moment the module is imported.

THREAD SAFETY
─────────────
grab_all() runs on a plain daemon thread (same as _launch_missav_playwright).
All UI work is done on the Qt main thread via signals connected with the
default AutoConnection (queued across threads, direct within the same thread).
"""

import os
import sys
import re
import threading
from urllib.parse import urlparse

from PyQt6.QtCore import pyqtSignal, pyqtSlot


# ── Utilities ─────────────────────────────────────────────────────────────────

def _is_javguru_url(url: str) -> bool:
    """Return True if *url* is a jav.guru video page we can scrape."""
    try:
        parsed = urlparse(str(url or '').strip())
        host = (parsed.netloc or '').lower().lstrip('www.')
        return host in ('jav.guru', 'javguru.net') and bool(parsed.path.strip('/'))
    except Exception:
        return False


def _javguru_grab_script_path() -> str:
    """Resolve the path to javguru_grab.py sitting next to main.py."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'javguru_grab.py')


# ── Methods injected into VideoPlayer ─────────────────────────────────────────

def _vp_is_javguru_url(self, url: str) -> bool:
    return _is_javguru_url(url)


def _vp_launch_javguru_grab(self, source_url: str, play_first: bool = True) -> bool:
    """
    Queue grab_all() in a background daemon thread for *source_url*.
    Returns True immediately (the grab is queued), False if the URL is
    not a jav.guru link or a grab for this URL is already pending.
    """
    if not _is_javguru_url(source_url):
        return False

    import queue
    q = getattr(self, '_javguru_queue', None)
    if q is None:
        self._javguru_queue = queue.Queue()
        q = self._javguru_queue

    pending: set = getattr(self, '_javguru_pending', set())
    if not hasattr(self, '_javguru_pending'):
        self._javguru_pending = pending
        
    pending_key = source_url.lower()
    if pending_key in pending:
        self.show_osd("jav.guru link is already queued…", duration=2200)
        return True

    pending.add(pending_key)
    q.put((source_url, play_first, pending_key))
    
    qsize = q.qsize()
    if qsize > 1:
        self.show_osd(f"jav.guru link queued ({qsize} pending)", duration=3000)
    else:
        self.show_osd("Grabbing jav.guru streams… (this takes ~30 s)", duration=35_000)

    worker_thread = getattr(self, '_javguru_worker_thread', None)
    if worker_thread is None or not worker_thread.is_alive():
        def _worker_loop():
            while True:
                try:
                    item = self._javguru_queue.get_nowait()
                except queue.Empty:
                    break
                    
                src_url, p_first, p_key = item
                self._remote_analysis_begin()
                try:
                    grab_script = _javguru_grab_script_path()
                    if not os.path.exists(grab_script):
                        raise FileNotFoundError(f"javguru_grab.py not found at: {grab_script}")
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('javguru_grab', grab_script)
                    mod  = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)

                    title, streams = mod.grab_all(src_url, visible=False)

                    if not streams:
                        raise RuntimeError("grab_all() returned no streams — check Playwright / Cloudflare and try --visible")

                    import json
                    streams_json = json.dumps(streams)
                    self.javguru_capture_ready.emit(src_url, streams_json, title or '', bool(p_first))

                except Exception as exc:
                    print(f"[JAVGURU] Failed to grab {src_url}: {exc}")
                    self.javguru_capture_failed.emit(src_url, str(exc))

                finally:
                    try:
                        self._javguru_pending.discard(p_key)
                    except Exception:
                        pass
                    self._remote_analysis_end()
                    self._javguru_queue.task_done()

        self._javguru_worker_thread = threading.Thread(target=_worker_loop, daemon=True, name='javguru-grab-loop')
        self._javguru_worker_thread.start()

    return True


@pyqtSlot(str, str, str, bool)
def _vp_on_javguru_capture_ready(
    self,
    source_url: str,
    streams_json: str,
    title: str,
    play_first: bool,
):
    """
    Called on the UI thread once grab_all() has finished successfully.

    streams_json — JSON-encoded dict  {stream_label: embed_url}
    The jav.guru page is the playlist row; every stream hoster is a
    mirror so switching never drops the scraped title or guru icon.
    """
    import json

    try:
        streams: dict = json.loads(streams_json)
    except Exception as exc:
        self._on_javguru_capture_failed(source_url, f"Bad streams payload: {exc}")
        return

    if not streams:
        self._on_javguru_capture_failed(source_url, "No streams in payload")
        return

    stream_items = list(streams.items())
    hoster_urls = []
    for _, raw in stream_items:
        if not raw:
            continue
        cleaned = self._canonicalize_remote_source_url(self._sanitize_url(raw))
        if cleaned:
            hoster_urls.append(cleaned)

    # R56: JAV site URL must NOT be playlist row or mirror.
    # Playlist entry is first HOSTER (dood/voe/etc.), JAV page is origin.
    if not hoster_urls:
        self._on_javguru_capture_failed(source_url, "No hoster URLs")
        return

    visible_url = hoster_urls[0]
    remaining = hoster_urls[1:]

    # Dedupe remaining
    seen = set()
    deduped = []
    for u in remaining:
        low = u.lower()
        if low in seen:
            continue
        seen.add(low)
        deduped.append(u)
    remaining = deduped

    already_in_playlist = self._playlist_contains_remote_url(visible_url)

    if not already_in_playlist:
        self.playlist_widget.setUpdatesEnabled(False)
        try:
            self.add_video_to_playlist(visible_url, batch_mode=True)
        finally:
            self.playlist_widget.setUpdatesEnabled(True)

        if remaining:
            self._set_mirrors_for_primary(visible_url, remaining)

        if not self._collapse_duplicate_url_mirrors():
            self.apply_playlist_filtering()
        self._schedule_remote_duration_probes([visible_url])

    # Stamp identity: hoster URLs get JAV site icon + title
    try:
        self._stamp_jav_site_identity(
            [visible_url] + list(remaining), source_url, title)
    except Exception:
        pass

    # Seed cache with title + origin_page
    try:
        info = {'title': title, 'origin_page': source_url, 'roshy_source_url': source_url}
        self._merge_stream_info(visible_url, info)
        self._remember_recent_file_name(visible_url, title, save=True)
        for m in remaining:
            self._merge_stream_info(m, {'title': title, 'origin_page': source_url, 'roshy_source_url': source_url})
    except Exception:
        pass

    try:
        self._refresh_playlist_row_metadata(visible_url)
    except Exception:
        pass

    if play_first and not already_in_playlist:
        self.set_media(visible_url)
        self._start_current_media_playback()

    n_mirrors = len(remaining)
    if already_in_playlist:
        self.show_osd("jav.guru stream already in playlist", duration=2600)
    else:
        msg = f"Added jav.guru: {n_mirrors + 1} stream(s)"
        if n_mirrors:
            msg += f" ({n_mirrors} mirror{'s' if n_mirrors != 1 else ''})"
        self.show_osd(msg, duration=3000)


@pyqtSlot(str, str)
def _vp_on_javguru_capture_failed(self, source_url: str, error_message: str):
    error_message = str(error_message or "Unknown error").strip()
    print(f"[JAVGURU] capture failed for {source_url}: {error_message}")
    self.show_osd("jav.guru grab failed — check console for details", duration=3500)


def _vp_queue_add_urls_patched(self, text: str, play_first=True, request_source='manual'):
    """
    Replacement for queue_add_urls_to_playlist_text.
    Intercepts jav.guru URLs and routes them to the Playwright grabber;
    everything else falls through to the original method unchanged.
    """
    text = str(text or '').strip()
    if not text:
        return False

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    javguru_urls = []
    other_lines = []
    
    for line in lines:
        if _is_javguru_url(line):
            javguru_urls.append(line)
        else:
            other_lines.append(line)

    # Launch grabber for all javguru urls
    for i, j_url in enumerate(javguru_urls):
        # We only play the FIRST one if play_first is True and it's the very first URL overall
        do_play = play_first and (i == 0) and (not other_lines)
        self._launch_javguru_grab(j_url, play_first=do_play)

    # If there are non-javguru URLs, process them normally.
    # This will return True and emit the signal when done, unlocking the clipboard.
    if other_lines:
        return self._queue_add_urls_to_playlist_text_original(
            '\n'.join(other_lines), play_first=play_first, request_source=request_source
        )

    # If there were ONLY javguru urls, we MUST return False here!
    # Returning False tells the clipboard monitor that it doesn't need to wait
    # for a background thread's signal (prepared_url_addition_ready), 
    # which immediately releases the `_clipboard_auto_processing` lock 
    # and allows subsequent copied URLs to be processed.
    return False


# ── Patch application ─────────────────────────────────────────────────────────

def _apply_patch():
    """
    Inject new methods and signals into VideoPlayer at import time.
    Safe to call multiple times (idempotent guard on _javguru_patch_applied).
    """
    # Locate VideoPlayer — it must be defined in the __main__ module
    # (or already imported into the top-level namespace).
    import __main__ as _main_mod
    VP = getattr(_main_mod, 'VideoPlayer', None)
    if VP is None:
        # Try every already-imported module for a class called VideoPlayer.
        for _mod in list(sys.modules.values()):
            if _mod is None:
                continue
            _cls = getattr(_mod, 'VideoPlayer', None)
            if _cls is not None and isinstance(_cls, type):
                VP = _cls
                break

    if VP is None:
        print("[JAVGURU] WARNING: VideoPlayer class not found — patch not applied.")
        return

    if getattr(VP, '_javguru_patch_applied', False):
        return  # already patched

    # ── Add signals ──────────────────────────────────────────────────────────
    # pyqtSignal must be a class attribute; we can't add one to an instance.
    # Adding it to the class after definition works in PyQt6 as long as it's
    # done before any instance is created (or before the metaclass finalises
    # the signal descriptors).  Since we're imported at the bottom of main.py,
    # before   if __name__ == '__main__':   creates the QApplication, this is
    # always satisfied.
    VP.javguru_capture_ready  = pyqtSignal(str, str, str, bool)  # source_url, streams_json, title, play_first
    VP.javguru_capture_failed = pyqtSignal(str, str)             # source_url, error_message

    # ── Inject instance methods ───────────────────────────────────────────────
    VP._is_javguru_url                  = _vp_is_javguru_url
    VP._launch_javguru_grab             = _vp_launch_javguru_grab
    VP._on_javguru_capture_ready        = _vp_on_javguru_capture_ready
    VP._on_javguru_capture_failed       = _vp_on_javguru_capture_failed

    # ── Patch queue_add_urls_to_playlist_text ────────────────────────────────
    original = VP.queue_add_urls_to_playlist_text
    VP._queue_add_urls_to_playlist_text_original = original
    VP.queue_add_urls_to_playlist_text = _vp_queue_add_urls_patched

    # ── Connect signals when __init__ finishes ───────────────────────────────
    # We wrap __init__ to connect the two new signals after super().__init__
    # completes, because signal connections require the QObject to be
    # initialised first.
    original_init = VP.__init__

    def _patched_init(self_vp, *args, **kwargs):
        original_init(self_vp, *args, **kwargs)
        self_vp.javguru_capture_ready.connect(self_vp._on_javguru_capture_ready)
        self_vp.javguru_capture_failed.connect(self_vp._on_javguru_capture_failed)

    VP.__init__ = _patched_init

    VP._javguru_patch_applied = True
    print("[JAVGURU] VideoPlayer patched — jav.guru URL detection active.")


# Apply the patch immediately when this module is imported.
_apply_patch()
