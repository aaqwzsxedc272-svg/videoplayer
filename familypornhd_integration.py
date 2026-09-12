"""familypornhd integration for :class:`VideoPlayer`.

The old integration only ran the grabber and wrote ``url.txt``.  Nothing ever
read that file or added the extracted links to the playlist, so a successful
HTML extraction looked like a failed playback.  This module follows the same
signal-based pattern as the other site integrations: fetch in a worker, then
add/cache/play the result on Qt's UI thread.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from urllib.parse import urlparse

from PyQt6.QtCore import pyqtSignal, pyqtSlot


def _is_familypornhd_url(url: str) -> bool:
    """Return True only for a familypornhd.com page URL."""
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        return (
            bool(parsed.path.strip("/"))
            and (host == "familypornhd.com" or host.endswith(".familypornhd.com"))
        )
    except Exception:
        return False


def _familypornhd_grab_script_path() -> str:
    """Resolve the path to familypornhd_grab.py next to main.py."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "familypornhd_grab.py")


# ── Worker-side methods ───────────────────────────────────────────────────────

def _vp_is_familypornhd_url(self, url: str) -> bool:
    return _is_familypornhd_url(url)


def _vp_launch_familypornhd_grab(self, source_url: str, play_first: bool = True) -> bool:
    """Queue one page for static HTML extraction."""
    if not _is_familypornhd_url(source_url):
        return False

    q = getattr(self, "_familypornhd_queue", None)
    if q is None:
        self._familypornhd_queue = queue.Queue()
        q = self._familypornhd_queue

    pending = getattr(self, "_familypornhd_pending", None)
    if pending is None:
        pending = set()
        self._familypornhd_pending = pending

    pending_key = source_url.strip().lower()
    if pending_key in pending:
        self.show_osd("familypornhd link is already queued…", duration=2200)
        return True

    pending.add(pending_key)
    q.put((source_url, bool(play_first), pending_key))
    qsize = q.qsize()
    if qsize > 1:
        self.show_osd(f"familypornhd link queued ({qsize} pending)", duration=3000)
    else:
        self.show_osd("Reading familypornhd HTML…", duration=15000)

    worker_thread = getattr(self, "_familypornhd_worker_thread", None)
    if worker_thread is None or not worker_thread.is_alive():
        def _worker_loop():
            while True:
                try:
                    src_url, p_first, p_key = self._familypornhd_queue.get_nowait()
                except queue.Empty:
                    break

                begin = getattr(self, "_remote_analysis_begin", None)
                end = getattr(self, "_remote_analysis_end", None)
                if callable(begin):
                    begin()
                try:
                    from familypornhd_grab import grab_all

                    static_result = grab_all(src_url, p_first)
                    # Keep compatibility with an older/local grabber that
                    # returned a bare list instead of the result dictionary.
                    if isinstance(static_result, dict):
                        static_links = static_result.get("links") or []
                        static_error = str(static_result.get("error") or "")
                    else:
                        static_links = static_result or []
                        static_error = ""
                        static_result = {
                            "source_url": src_url,
                            "title": "",
                            "links": static_links,
                            "headers": {},
                        }

                    static_links = [
                        str(link).strip()
                        for link in static_links
                        if str(link).strip()
                    ]
                    static_result["links"] = static_links

                    # A URL in the initial HTML can be a pre-roll/ad asset.
                    # Visit the page in the same headed Playwright capture
                    # used by the main resolver and let it click Play, wait
                    # through the ad, and select the duration-verified media
                    # request.  This is the important distinction between
                    # "a URL was present in HTML" and "this is the actual
                    # video".  If browser capture is unavailable/fails, keep
                    # the static extractor as a compatibility fallback.
                    browser_result = None
                    capture_handoff = {"result": None, "emitted": False}

                    def _on_browser_capture(resolved_payload):
                        """Queue the verified URL immediately when the
                        browser resolver has finished probing it.  Do not
                        wait for the worker's normal return-value handoff.
                        """
                        if not isinstance(resolved_payload, dict):
                            return
                        capture_handoff["result"] = dict(resolved_payload)
                        handoff_url = str(
                            resolved_payload.get("playback_url")
                            or resolved_payload.get("download_url")
                            or resolved_payload.get("stream_url")
                            or ""
                        ).strip()
                        if not handoff_url:
                            return
                        handoff_path = urlparse(handoff_url).path.lower()
                        handoff_type = str(resolved_payload.get("content_type") or "").lower()
                        if handoff_type.startswith("image/") or handoff_path.endswith(
                            (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico")
                        ):
                            return
                        handoff_result = {
                            "source_url": src_url,
                            "title": resolved_payload.get("title") or static_result.get("title") or "",
                            "links": [handoff_url],
                            "headers": resolved_payload.get("headers") or static_result.get("headers") or {},
                            "resolved_info": dict(resolved_payload),
                            "capture_method": "browser_duration_verified",
                        }
                        try:
                            self.familypornhd_capture_ready.emit(
                                src_url, handoff_result, bool(p_first)
                            )
                            capture_handoff["emitted"] = True
                            print(f"[FAMILYPORNHD] queued captured stream immediately: {handoff_url[:180]}")
                        except Exception as handoff_exc:
                            print(f"[FAMILYPORNHD] immediate playlist handoff failed: {handoff_exc}")

                    browser_capture = getattr(self, "_resolve_stream_via_browser_click", None)
                    if callable(browser_capture):
                        try:
                            browser_result = browser_capture(
                                src_url,
                                page_title=str(static_result.get("title") or ""),
                                max_watch=90,
                                # FamilyPornHD uses a normal HTML5 player;
                                # capture it headlessly so the resolver never
                                # opens a visible Brave/Chrome window.
                                headed_hidden=False,
                                headless=True,
                                referer=src_url,
                                dump_html=True,
                                capture_callback=_on_browser_capture,
                            )
                        except Exception as capture_exc:
                            print(f"[FAMILYPORNHD] browser capture failed for {src_url}: {capture_exc}")

                    # The shared resolver normally returns a dict with
                    # ``playback_url``.  Accept its other established URL
                    # aliases as well so a provider-specific resolver cannot
                    # lose a stream after it has already been captured.  The
                    # resolver also keeps a same-thread handoff copy for a
                    # Windows/Qt worker boundary that drops the return value
                    # after the success message was printed.
                    if not isinstance(browser_result, dict):
                        browser_result = (
                            capture_handoff.get("result")
                            or getattr(self, "_last_browser_click_result", None)
                        )
                    resolved_result = browser_result if isinstance(browser_result, dict) else None

                    def _resolved_url(payload):
                        if not isinstance(payload, dict):
                            return ""
                        candidate_url = str(
                            payload.get("playback_url")
                            or payload.get("download_url")
                            or payload.get("stream_url")
                            or payload.get("url")
                            or ""
                        ).strip()
                        if not candidate_url:
                            for _candidate in (payload.get("links") or payload.get("streams") or []):
                                if str(_candidate or "").strip():
                                    candidate_url = str(_candidate).strip()
                                    break
                        return candidate_url

                    browser_url = _resolved_url(resolved_result)
                    # If a local build returns after logging a successful
                    # browser probe but does not hand the mapping back (or if
                    # the browser path is unavailable), make one direct HTML
                    # resolver pass before reporting failure.  This page puts
                    # the signed ``get_file/...mp4/?...`` URL in its rendered
                    # markup, so this is a safe fallback and still preserves
                    # the page Referer in the returned metadata.
                    if not browser_url:
                        html_resolver = getattr(self, "_resolve_stream_from_html", None)
                        if callable(html_resolver):
                            try:
                                html_result = html_resolver(src_url)
                            except Exception as html_exc:
                                print(f"[FAMILYPORNHD] direct HTML fallback failed for {src_url}: {html_exc}")
                                html_result = None
                            if isinstance(html_result, dict) and _resolved_url(html_result):
                                resolved_result = html_result
                                browser_url = _resolved_url(html_result)
                                print(f"[FAMILYPORNHD] recovered stream from direct HTML fallback: {browser_url[:180]}")

                    if resolved_result and browser_url:
                        browser_type = str(resolved_result.get("content_type") or "").lower()
                        browser_path = urlparse(browser_url).path.lower() if browser_url else ""
                        obvious_image = (
                            browser_type.startswith("image/")
                            or browser_path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"))
                        )
                        if not obvious_image:
                            result = {
                                "source_url": src_url,
                                "title": resolved_result.get("title") or static_result.get("title") or "",
                                "links": [browser_url],
                                "headers": resolved_result.get("headers") or static_result.get("headers") or {},
                                "resolved_info": resolved_result,
                                "capture_method": "browser_duration_verified",
                            }
                            print(f"[FAMILYPORNHD] selected browser-captured video: {browser_url[:180]}")
                        else:
                            result = static_result
                    else:
                        result = static_result

                    links = [str(link).strip() for link in (result.get("links") or []) if str(link).strip()]
                    if not links:
                        detail = static_error or "no playable media URLs were found after visiting the page"
                        raise RuntimeError(detail)
                    result["links"] = links
                    if not capture_handoff.get("emitted"):
                        self.familypornhd_capture_ready.emit(src_url, result, bool(p_first))
                except Exception as exc:
                    print(f"[FAMILYPORNHD] Failed to grab {src_url}: {exc}")
                    self.familypornhd_capture_failed.emit(src_url, str(exc))
                finally:
                    try:
                        self._familypornhd_pending.discard(p_key)
                    except Exception:
                        pass
                    if callable(end):
                        end()
                    self._familypornhd_queue.task_done()

        self._familypornhd_worker_thread = threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="familypornhd-grab-loop",
        )
        self._familypornhd_worker_thread.start()

    return True


# ── UI-thread result handlers ─────────────────────────────────────────────────

def _familypornhd_stream_info(
    page_url: str,
    title: str,
    playback_url: str,
    headers: dict,
    resolved_info: dict | None = None,
) -> dict:
    """Build cache metadata for an HTML or browser-captured stream."""
    resolved_info = resolved_info if isinstance(resolved_info, dict) else {}
    path = (urlparse(playback_url).path or "").lower()
    info = {
        "title": title,
        "origin_page": page_url,
        "playback_url": playback_url,
        "download_url": playback_url,
        "headers": dict(resolved_info.get("headers") or headers or {}),
        "pre_resolved_playback_url": True,
        "resolver_provider": resolved_info.get("resolver_provider") or "familypornhd_html",
        "resolved_at_ms": int(resolved_info.get("resolved_at_ms") or time.time() * 1000),
    }
    if path.endswith((".m3u8", ".m3u", ".mpd")):
        info["content_type"] = "application/vnd.apple.mpegurl"
    for key in ("content_type", "size_bytes", "size_text", "variants", "subtitle_tracks", "tls_verify", "route_local_proxy"):
        if resolved_info.get(key) not in (None, "", [], {}):
            info[key] = resolved_info[key]
    return info


@pyqtSlot(str, object, bool)
def _vp_on_familypornhd_capture_ready(self, source_url: str, result, play_first: bool):
    """Add the first HTML URL and retain the rest as mirrors."""
    if not isinstance(result, dict):
        result = {"links": result or []}

    raw_links = result.get("links") or []
    print(f"[FAMILYPORNHD] playlist handoff received: {len(raw_links)} stream(s)")
    title = str(result.get("title") or "").strip()
    page_url = str(result.get("source_url") or source_url).strip() or source_url
    headers = result.get("headers") or {}
    resolved_info = result.get("resolved_info") or {}
    resolved_playback_url = str(resolved_info.get("playback_url") or "").strip()

    links = []
    seen = set()
    for raw in raw_links:
        try:
            cleaned = self._canonicalize_remote_source_url(self._sanitize_url(str(raw).strip()))
        except Exception:
            cleaned = str(raw).strip()
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        links.append(cleaned)

    if not links:
        self._on_familypornhd_capture_failed(source_url, "no valid media URLs in payload")
        return

    if not title:
        try:
            title = self._playlist_display_name(source_url)
        except Exception:
            title = "familypornhd video"

    primary_url = links[0]
    mirror_urls = links[1:]
    already_in_playlist = self._playlist_contains_remote_url(primary_url)

    # Cache every extracted URL before autoplay.  set_media() intentionally
    # re-enters the normal async resolver for remote URLs; the
    # pre_resolved_playback_url flag makes that resolver return this exact
    # HTML URL instead of trying yt-dlp against the CDN.
    all_urls = [primary_url] + mirror_urls
    for media_url in all_urls:
        media_resolved_info = (
            resolved_info
            if resolved_playback_url
            and media_url.lower() == resolved_playback_url.lower()
            else None
        )
        self._merge_stream_info(
            media_url,
            _familypornhd_stream_info(
                page_url,
                title,
                media_url,
                headers,
                resolved_info=media_resolved_info,
            ),
        )
        try:
            self._remember_recent_file_name(media_url, title, save=False)
        except Exception:
            pass

    added = False
    if not already_in_playlist:
        self.playlist_widget.setUpdatesEnabled(False)
        try:
            before = len(self.playlist)
            self.add_video_to_playlist(primary_url, batch_mode=True)
            added = len(self.playlist) > before
        finally:
            self.playlist_widget.setUpdatesEnabled(True)

    if mirror_urls:
        try:
            existing = list(self._mirrors_for_visible_url(primary_url) or [])
        except Exception:
            existing = []
        merged_mirrors = []
        for media_url in existing + mirror_urls:
            if media_url and media_url != primary_url and media_url not in merged_mirrors:
                merged_mirrors.append(media_url)
        if merged_mirrors:
            self._set_mirrors_for_primary(primary_url, merged_mirrors)

    try:
        self._refresh_playlist_row_metadata(primary_url)
    except Exception:
        pass
    if added or already_in_playlist:
        if not self._collapse_duplicate_url_mirrors():
            self.apply_playlist_filtering()
        try:
            self._schedule_remote_duration_probes([primary_url])
        except Exception:
            pass

    if play_first and added:
        self.set_media(primary_url)
        self._start_current_media_playback()

    if already_in_playlist:
        self.show_osd("familypornhd stream already in playlist", duration=2600)
    elif added:
        count = len(mirror_urls) + 1
        suffix = f" ({len(mirror_urls)} mirror{'s' if len(mirror_urls) != 1 else ''})" if mirror_urls else ""
        self.show_osd(f"Added familypornhd: {count} stream(s){suffix}", duration=3000)
    else:
        self.show_osd("familypornhd link was not added", duration=3000)


@pyqtSlot(str, str)
def _vp_on_familypornhd_capture_failed(self, source_url: str, error_message: str):
    message = str(error_message or "unknown error").strip()
    print(f"[FAMILYPORNHD] capture failed for {source_url}: {message}")
    self.show_osd("familypornhd HTML extraction failed — check console", duration=3500)


# ── URL queue interception ────────────────────────────────────────────────────

def _vp_queue_add_urls_patched_familypornhd(self, text: str, play_first=True, request_source="manual"):
    text = str(text or "").strip()
    if not text:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    family_urls = [line for line in lines if _is_familypornhd_url(line)]
    other_lines = [line for line in lines if not _is_familypornhd_url(line)]

    for index, family_url in enumerate(family_urls):
        # Preserve the existing integrations' behavior: autoplay only when
        # this is the sole submitted URL.
        do_play = bool(play_first and index == 0 and not other_lines)
        self._launch_familypornhd_grab(family_url, play_first=do_play)

    if other_lines:
        previous = getattr(self, "_queue_add_urls_to_playlist_text_prev_familypornhd", None)
        if previous is not None:
            return previous(
                "\n".join(other_lines),
                play_first=play_first,
                request_source=request_source,
            )

    # Like the other asynchronous page grabbers, an all-familypornhd batch
    # has been accepted but has not completed synchronously.  Returning False
    # prevents the clipboard monitor from treating the submission as a
    # completed normal URL batch.
    return False


# ── Patch application ─────────────────────────────────────────────────────────

def _apply_patch():
    import __main__ as main_module
    video_player = getattr(main_module, "VideoPlayer", None)
    if video_player is None:
        import sys
        for module in list(sys.modules.values()):
            candidate = getattr(module, "VideoPlayer", None) if module else None
            if candidate is not None and isinstance(candidate, type):
                video_player = candidate
                break

    if video_player is None:
        print("[FAMILYPORNHD] WARNING: VideoPlayer class not found — patch not applied.")
        return
    if getattr(video_player, "_familypornhd_patch_applied", False):
        return

    video_player.familypornhd_capture_ready = pyqtSignal(str, object, bool)
    video_player.familypornhd_capture_failed = pyqtSignal(str, str)
    video_player._is_familypornhd_url = _vp_is_familypornhd_url
    video_player._launch_familypornhd_grab = _vp_launch_familypornhd_grab
    video_player._on_familypornhd_capture_ready = _vp_on_familypornhd_capture_ready
    video_player._on_familypornhd_capture_failed = _vp_on_familypornhd_capture_failed

    original_queue = video_player.queue_add_urls_to_playlist_text
    video_player._queue_add_urls_to_playlist_text_prev_familypornhd = original_queue
    video_player.queue_add_urls_to_playlist_text = _vp_queue_add_urls_patched_familypornhd

    original_init = video_player.__init__

    def _patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.familypornhd_capture_ready.connect(self._on_familypornhd_capture_ready)
        self.familypornhd_capture_failed.connect(self._on_familypornhd_capture_failed)

    video_player.__init__ = _patched_init
    video_player._familypornhd_patch_applied = True
    print("[FAMILYPORNHD] VideoPlayer patched — HTML URL detection active.")


_apply_patch()
