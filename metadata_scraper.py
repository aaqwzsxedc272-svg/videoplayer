"""
metadata_scraper.py
===================
TeamSkeet metadata scraper + playlist name linker.

Scraping strategy
-----------------
Each /movies?page=N response includes a window.__INITIAL_STATE__ JSON blob.
The scraper reads those listing records directly, so a full scrape collects
series, models, dates, tags, images, trailers, stats, and descriptions without
calling yt-dlp for every movie. Individual movie pages and yt-dlp are kept as
fallbacks for entries that are still missing metadata.

Integration hooks in main.py
------------------------------
1.  init_metadata_scraper(player)       — call once after __init__
2.  _metadata_context_menu_hook(...)    — inject into _populate_playlist_context_menu
3.  In _playlist_display_name           — check _metadata_name_overrides first
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape as html_unescape
from typing import Optional
from urllib.parse import urlparse, unquote, urljoin

from PyQt6.QtCore  import Qt, QThread, pyqtSignal, QTimer, QObject
from PyQt6.QtGui   import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QMenu,
    QProgressBar, QTextEdit, QWidget, QFrame, QScrollArea, QSplitter,
    QComboBox,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TEAMSKEET_BASE_URL   = "https://www.teamskeet.com"
TEAMSKEET_MOVIES_URL = f"{TEAMSKEET_BASE_URL}/movies"
REPTYLE_SOURCES = [
    {"id": "teamskeet", "name": "TeamSkeet", "base_url": "https://www.teamskeet.com", "movies_url": "https://www.teamskeet.com/movies"},
    {"id": "pervz", "name": "Pervz", "base_url": "https://www.pervz.com", "movies_url": "https://www.pervz.com/movies"},
    {"id": "mylf", "name": "MYLF", "base_url": "https://www.mylf.com", "movies_url": "https://www.mylf.com/movies"},
    {"id": "familystrokes", "name": "FamilyStrokes", "base_url": "https://www.familystrokes.com", "movies_url": "https://www.familystrokes.com/movies"},
    {"id": "swappz", "name": "Swappz", "base_url": "https://www.swappz.com", "movies_url": "https://www.swappz.com/movies"},
]
DB_FILENAME          = "teamskeet_metadata.json"
OVERRIDES_FILENAME   = "metadata_name_overrides.json"
LINKS_FILENAME       = "metadata_links.json"

# Bumped on every change the user has to copy over by hand. Files are copied
# manually, so "is the running player the one that was just pushed?" has been
# an open question more than once and has cost whole test runs. This answers it
# from the first line of the log.
BUILD = "link7-scene"
print(f"[MetadataScraper] build {BUILD}")
MATCH_THRESHOLD      = 0.32
SCRAPE_DELAY         = 0.8     # seconds between yt-dlp calls
PAGE_DELAY           = 0.4     # seconds between listing page fetches

DEFAULT_SITE_ID = "teamskeet"
METADATA_SITES = {
    "teamskeet": {
        "id": "teamskeet",
        "name": "Reptyle",
        "scraper": "reptyle",
        "sources": REPTYLE_SOURCES,
        "db_filename": DB_FILENAME,
        "overrides_filename": OVERRIDES_FILENAME,
    },
    "nubiles": {
        "id": "nubiles",
        "name": "Nubiles-Porn",
        "scraper": "network_gallery",
        "base_url": "https://nubiles-porn.com",
        "gallery_url": "https://nubiles-porn.com/video/gallery",
        "page_url_template": "https://nubiles-porn.com/video/gallery/{offset}",
        "page_size": 12,
        "page_delay": 1.6,
        "reader_fallback": True,
        "fallback_gallery_urls": ["https://nubiles-porn.com/video/recent"],
        "fallback_page_url_templates": ["https://nubiles-porn.com/video/recent/{offset}"],
        "gallery_sources": [
            {
                "id": "nubiles-porn",
                "name": "Nubiles Porn",
                "base_url": "https://nubiles-porn.com",
                "gallery_url": "https://nubiles-porn.com/video/gallery",
                "page_url_template": "https://nubiles-porn.com/video/gallery/{offset}",
                "page_delay": 1.6,
                "fallback_gallery_urls": ["https://nubiles-porn.com/video/recent"],
                "fallback_page_url_templates": ["https://nubiles-porn.com/video/recent/{offset}"],
                "needs_browser": True,
            },
            {
                "id": "nubilefilms",
                "name": "NubileFilms",
                "base_url": "https://nubilefilms.com",
                "gallery_url": "https://nubilefilms.com/video/gallery",
                "page_url_template": "https://nubilefilms.com/video/gallery/{offset}",
                "page_delay": 1.6,
                "needs_browser": True,
            },
            {
                "id": "brattysis",
                "name": "BrattySis",
                "base_url": "https://brattysis.com",
                "gallery_url": "https://brattysis.com/video/gallery",
                "page_url_template": "https://brattysis.com/video/gallery/{offset}",
                "page_delay": 1.6,
                "needs_browser": True,
            },
            {
                "id": "momlover",
                "name": "MomLover",
                "base_url": "https://momlover.com",
                "gallery_url": "https://momlover.com/video/gallery",
                "page_url_template": "https://momlover.com/video/gallery/{offset}",
                "page_delay": 1.6,
                "needs_browser": True,
            },
        ],
        "db_filename": "nubiles_metadata.json",
        "overrides_filename": OVERRIDES_FILENAME,
    },
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.teamskeet.com/",
}

_HTTP_SESSION = None
_RATE_LIMITED_UNTIL: dict[str, float] = {}

# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp path detection
# ─────────────────────────────────────────────────────────────────────────────

def _find_ytdlp() -> Optional[str]:
    """Return path to yt-dlp executable, or None if not found."""
    candidates = []

    # 1. Path that the player itself already located (stored in mpv hook)
    env_path = os.environ.get("YTDLP_PATH", "")
    if env_path:
        candidates.append(env_path)

    # 2. Roaming AppData (where the player's yt-dlp usually lives on Windows)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for sub in [
            r"Python\Python314\Scripts\yt-dlp.exe",
            r"Python\Python313\Scripts\yt-dlp.exe",
            r"Python\Python312\Scripts\yt-dlp.exe",
            r"Python\Python311\Scripts\yt-dlp.exe",
        ]:
            candidates.append(os.path.join(appdata, sub))

    # 3. Common install locations
    candidates += [
        r"C:\yt-dlp\yt-dlp.exe",
        "yt-dlp",          # on PATH
        "yt-dlp.exe",
    ]

    for c in candidates:
        if c and os.path.isfile(c):
            return c
        if c in ("yt-dlp", "yt-dlp.exe"):
            try:
                subprocess.run([c, "--version"], capture_output=True, timeout=5)
                return c
            except Exception:
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MetadataDB
# ─────────────────────────────────────────────────────────────────────────────

# Fields worth persisting. Every entry here has a reader; anything else was
# dropped after tracing it, not after measuring its size. Field census of the
# shipped databases (bytes as a share of payload):
#
#   teamskeet  image 19.0%  trailer_url 18.7%  url 16.3%  video_id 8.9%
#              title 7.8%  slug 7.6%  models 5.4%  series 3.5%  date 3.1%
#   nubiles    url 30.4%  slug 15.3%  models 14.5%  title 10.2%  date 5.0%
#
# Retired because nothing reads them:
#   scraped_at   0 reads in main.py or here. The top-level last_full_scrape /
#                last_update_scrape already say when the DB was last touched.
#                233 KB on teamskeet, 118 KB on nubiles.
#   source_name  1:1 with source_site in all 15957 records (5 distinct pairs
#                on teamskeet, 4 on nubiles), so _source_display_name()
#                derives it from the site config instead. 47 KB.
#
# url is KEPT even though no part of the player opens it: 3724 of 15957
# records point at a host that cannot be rebuilt from the slug -- pervz, mylf,
# familystrokes and swappz on teamskeet, momlover, nubilefilms and brattysis
# on nubiles -- and source_site, the only other hint, is filled on just 3988
# records between the two files. It is the sole record of where a scene came
# from, so it stays despite costing 1.05 MB.
_MOVIE_KEEP_FIELDS = (
    "slug", "title", "series", "models", "date",
    "video_id", "source_site",
    "meta_fetched", "url",
    # Load-bearing, not decoration: NetworkGalleryScraper._run dedupes on
    # sources_seen and sets page_changed when a source is added to it. Trimming
    # it made `src_id not in sources_seen` permanently true, so every movie was
    # re-upserted on every scrape and the "caught up - no new listing metadata"
    # early-stop could never fire, turning an Update into a full 175-page crawl.
    "sources_seen",
    # Cover. The URL is signed and expires (see _image_expiry_epoch), so it is
    # held only until it dies -- and when it does, the movie's own watch page
    # mints a fresh one on hover. The bytes live in memory, never on disk.
    "image", "image_expires", "preview", "preview_expires",
    # TeamSkeet's trailer_url is a per-scene mp4 on images.psmcdn.net --
    # unsigned and permanently public, verified with a real/invented path
    # pair. At 0.91 MB across 10586 movies it is the cheapest working video
    # preview either network offers, so it earns its place.
    "trailer_url",
    # Neither network supplies a runtime today, so this costs nothing on disk;
    # it is kept so the criterion stays wired if a source ever provides one.
    "duration",
)


def _trim_movie(movie: dict) -> dict:
    """Keep only the fields anything reads back, dropping empty ones.

    An empty string, empty list or 0 is indistinguishable from an absent key
    to every reader in this module -- they all read `movie.get(k) or default`
    -- so persisting it costs bytes and buys nothing. In practice nubiles
    carried three dead keys on all 5371 records (preview, preview_expires,
    trailer_url) and teamskeet a fourth (duration, always 0 because neither
    network sends a runtime in any of its five aliases).
    """
    if not isinstance(movie, dict):
        return movie
    out = {}
    for k in _MOVIE_KEEP_FIELDS:
        if k not in movie:
            continue
        v = movie[k]
        if not v and k != "slug":
            continue
        out[k] = v
    return out


def _source_display_name(movie: dict) -> str:
    """The human name of a movie's source network, from the site config.

    source_name used to be stored on every record, but it was 1:1 with
    source_site everywhere, so it is derived. This matters for the display
    name: 1806 nubiles movies have no series and fall back to the network
    name, so dropping the field without this would have silently shortened
    all of those names from "NubileFilms - ..." to just the title.
    """
    sid = str((movie or {}).get("source_site") or "")
    if not sid:
        return ""
    for cfg in METADATA_SITES.values():
        srcs = list(cfg.get("sources") or []) + list(cfg.get("gallery_sources") or [])
        for src in srcs:
            if isinstance(src, dict) and str(src.get("id") or "") == sid:
                return str(src.get("name") or "")
    return ""


class MetadataDB:
    """
    JSON on-disk store.

    Each movie entry:
    {
      "slug":        str,
      "title":       str,
      "series":      str,
      "series_url":  str,
      "models":      [str, ...],
      "model_urls":  [str, ...],
      "date":        str,   # MM/DD/YYYY
      "url":         str,
      "scraped_at":  str,   # ISO-8601 UTC
      "meta_fetched": bool  # True once yt-dlp enriched this entry
    }
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock   = threading.Lock()
        self._data: dict = {
            "last_full_scrape":   None,
            "last_update_scrape": None,
            "movies": {}
        }
        self._load()

    def _load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "movies" in d:
                    self._data = d
                    # Older files carry the retired fields; drop them from
                    # memory now so the next save shrinks the file on disk.
                    self.compact_records()
                    # Dates used to be stored MM/DD/YYYY. Both networks send
                    # an unambiguous source value (teamskeet an ISO stamp,
                    # nubiles "May 26, 2026") and the builders now emit
                    # DD/MM/YYYY, so anything still MM/DD on disk is a
                    # leftover from an older build and is fixed here.
                    self.migrate_dates()
            except Exception as e:
                # Never carry on quietly from empty: the next save would write
                # that over the file and the metadata would be gone for good.
                try:
                    size = os.path.getsize(self.db_path)
                except Exception:
                    size = -1
                if size == 0:
                    # "Expecting value: line 1 column 1 (char 0)" is what a
                    # 0-byte file looks like, and it says nothing about why.
                    # The old save() truncated before writing a single byte, so
                    # an interrupted one left exactly this behind -- and there
                    # is nothing in it worth preserving.
                    print(f"[MetadataDB] {self.db_path} is empty (0 bytes) -- an "
                          f"interrupted save truncated it and there is nothing "
                          f"in it to recover")
                else:
                    print(f"[MetadataDB] cannot read {self.db_path}: {e}")
                    try:
                        kept = self.db_path + ".unreadable"
                        if os.path.exists(kept):
                            os.remove(kept)
                        os.replace(self.db_path, kept)
                        print(f"[MetadataDB] moved it to {kept} -- the metadata "
                              f"is still there, fix the cause and rename it back")
                    except Exception as e2:
                        print(f"[MetadataDB] and could not preserve it: {e2}")

    def compact_records(self) -> int:
        """Strip retired fields and dead signed URLs. Returns how many records
        changed."""
        movies = self._data.get("movies") or {}
        now = time.time()
        changed = 0
        for slug, movie in list(movies.items()):
            if not isinstance(movie, dict):
                continue
            trimmed = _trim_movie(movie)
            # A signed cover/preview URL past its e= expiry returns 403
            # forever, so keeping it is dead weight -- roughly 280 bytes per
            # record, which on a 45k-movie database is ~12 MB of strings that
            # can never be used. The cover survives as the file a scrape
            # downloaded, and the preview path is re-derivable from title and
            # series at any time via preview_loop_url().
            for url_key, exp_key in (("image", "image_expires"),
                                     ("preview", "preview_expires")):
                exp = int(trimmed.get(exp_key) or 0)
                if exp and exp < now:
                    trimmed.pop(url_key, None)
                    trimmed.pop(exp_key, None)
            if trimmed != movie:
                movies[slug] = trimmed
                changed += 1
        return changed

    def migrate_dates(self) -> int:
        """Rewrite legacy MM/DD/YYYY dates to DD/MM/YYYY. Returns the count.

        Only a value whose second field is above 12 can be MM/DD, so this
        never touches a date that is already DD/MM or one that is ambiguous.
        Verified against published_date_iso in the original teamskeet export,
        which is the ground truth for 10564 records.
        """
        movies = self._data.get("movies") or {}
        changed = 0
        for movie in movies.values():
            if not isinstance(movie, dict):
                continue
            raw = str(movie.get("date") or "")
            fixed = _normalise_display_date(raw)
            if fixed and fixed != raw:
                movie["date"] = fixed
                changed += 1
        if changed:
            self.save()
        return changed

    def compact(self) -> tuple:
        """Compact in memory and write back. Returns (before_bytes, after_bytes)."""
        before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        self.compact_records()
        self.save()
        after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return before, after

    def save(self):
        with self._lock:
            tmp = self.db_path + ".tmp"
            try:
                # Write beside the file, then move it into place. open(path,"w")
                # truncates before a single byte is written, so a disk that
                # fills up halfway used to leave a few dozen bytes of broken
                # JSON where megabytes of metadata had been -- and _load() read
                # that as an empty database. os.replace is atomic: either the
                # old file stands or the new one does, never half of either.
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.db_path)
                self._save_error_at = 0.0
                self._save_errors_hidden = 0
            except Exception as e:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                # A full disk made this print on every autosave: fifty
                # identical lines, burying the one thing worth reading in the
                # log. Report it, then stay quiet about the same failure.
                now = time.time()
                hidden = getattr(self, "_save_errors_hidden", 0) + 1
                self._save_errors_hidden = hidden
                if now - getattr(self, "_save_error_at", 0.0) > 300:
                    self._save_error_at = now
                    extra = (f" (+{hidden - 1} more since the last report)"
                             if hidden > 1 else "")
                    print(f"[MetadataDB] save error: {e}{extra}")

    # ── accessors ─────────────────────────────────────────────────────────────

    @property
    def movies(self) -> dict:
        return self._data.get("movies", {})

    @property
    def last_full_scrape(self) -> Optional[str]:
        return self._data.get("last_full_scrape")

    @property
    def last_update_scrape(self) -> Optional[str]:
        return self._data.get("last_update_scrape")

    def count(self) -> int:
        return len(self._data.get("movies", {}))

    def count_enriched(self) -> int:
        return sum(1 for m in self._data.get("movies", {}).values()
                   if m.get("meta_fetched"))

    def needs_full_scrape(self) -> bool:
        return not self.last_full_scrape or self.count_enriched() == 0

    def needs_update_scrape(self) -> bool:
        if self.count() > 0 and self.count_enriched() == 0:
            return True
        ts = self.last_update_scrape or self.last_full_scrape
        if not ts:
            return True
        try:
            last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - last).total_seconds() > 7 * 24 * 3600
        except Exception:
            return True

    # ── writers ───────────────────────────────────────────────────────────────

    def upsert_stub(self, slug: str):
        """Insert a slug-only stub if not already present."""
        with self._lock:
            movies = self._data.setdefault("movies", {})
            if slug not in movies:
                # meta_fetched: False is dropped by _trim_movie, which is the
                # same thing -- stubs_needing_enrichment() tests
                # `not m.get("meta_fetched")`, and an absent key reads as not
                # fetched.
                movies[slug] = _trim_movie({
                    "slug":         slug,
                    "title":        _slug_to_title(slug),
                    "series":       "",
                    "models":       [],
                    "date":         "",
                    "url":          f"https://www.teamskeet.com/movies/{slug}",
                    "meta_fetched": False,
                })

    def upsert(self, movie: dict):
        slug = movie.get("slug", "")
        if not slug:
            return
        with self._lock:
            self._data.setdefault("movies", {})[slug] = _trim_movie(movie)

    def stubs_needing_enrichment(self) -> list[str]:
        return [s for s, m in self._data.get("movies", {}).items()
                if not m.get("meta_fetched")]

    def mark_full_scrape(self):
        self._data["last_full_scrape"] = _utc_now()

    def mark_update_scrape(self):
        self._data["last_update_scrape"] = _utc_now()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — slug harvester (plain HTTP, no JS needed)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_html(url: str, timeout: int = 20) -> Optional[str]:
    global _HTTP_SESSION
    headers = dict(REQUEST_HEADERS)
    parsed = urlparse(url)
    host_key = (parsed.netloc or "").lower()
    if host_key and _RATE_LIMITED_UNTIL.get(host_key, 0) > time.time():
        return None
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        from curl_cffi import requests as curl_requests
        # curl_cffi uses JA3 impersonation to bypass Cloudflare
        r = curl_requests.get(url, headers=headers, impersonate="chrome110", timeout=timeout)
        if r.status_code == 429:
            if host_key:
                _RATE_LIMITED_UNTIL[host_key] = time.time() + 15 * 60
            print(f"[Scraper] rate limited fetching {url} (curl_cffi): HTTP 429")
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        if isinstance(e, ImportError):
            pass
        else:
            print(f"[Scraper] curl_cffi fetch fallback {url}: {e}")

    try:
        import requests
        if _HTTP_SESSION is None:
            _HTTP_SESSION = requests.Session()
            _HTTP_SESSION.headers.update(REQUEST_HEADERS)
        r = _HTTP_SESSION.get(url, headers=headers, timeout=timeout)
        if r.status_code == 429:
            if host_key:
                _RATE_LIMITED_UNTIL[host_key] = time.time() + 15 * 60
            print(f"[Scraper] rate limited fetching {url}: HTTP 429")
            return None
        r.raise_for_status()
        return r.text
    except Exception as req_error:
        print(f"[Scraper] requests fetch fallback {url}: {req_error}")

    try:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            import gzip
            import io
            return gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        if "HTTP Error 429" in str(e) and host_key:
            _RATE_LIMITED_UNTIL[host_key] = time.time() + 15 * 60
        print(f"[Scraper] fetch error {url}: {e}")
        return None


def _discard_storage_state(path) -> None:
    """Move a state file aside rather than deleting it."""
    try:
        os.replace(path, str(path) + ".unreadable")
    except Exception:
        pass


def _storage_state_cookie_count(path) -> int:
    """How many cookies a saved browser state carries, 0 if it cannot be read.

    The count is the whole point of logging it: a state with cf_clearance in it
    is a browser that has already been waved through, and one without is a
    browser facing the challenge from scratch. Those two look identical from
    the outside, and they have opposite conclusions.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return len(json.load(fh).get("cookies") or [])
    except Exception:
        return 0


def _storage_state_usable(path) -> bool:
    """Is this saved browser state something Playwright can actually load?

    The state file is rewritten after every page, and on a full disk that
    rewrite truncates it to zero bytes -- after which new_context raises
    JSONDecodeError from deep inside Playwright. That error surfaced as
    "browser fetch failed: JSONDecodeError: Expecting value: line 1 column 1
    (char 0)" and cost every cover refresh the browser it needed. An
    unreadable state is treated as no state: a fresh browser still passes the
    challenge, it simply starts without cookies.
    """
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 2:
            if path and os.path.isfile(path):
                _discard_storage_state(path)
            return False
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        return True
    except Exception:
        _discard_storage_state(path)
        return False


def _save_storage_state(context, path) -> bool:
    """Persist browser state atomically, and only if it still parses.

    Never open the real path for writing: that truncates it before a byte is
    written, and a full disk then leaves it empty -- the exact corruption
    _storage_state_usable exists to survive.
    """
    if not context or not path:
        return False
    tmp = str(path) + ".tmp"
    try:
        context.storage_state(path=tmp)
        with open(tmp, encoding="utf-8") as fh:
            json.load(fh)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


class _BrowserGallerySession:
    """
    Opens one real, visible Chromium window (via Playwright) and reuses it to
    load a series of gallery pages. Meant for sites whose listing content only
    exists after real browser JS execution (Cloudflare JS/fingerprint checks,
    client-rendered galleries, etc.) — i.e. a normal browser passes them, a
    bare HTTP client doesn't.

    Now runs headlessly as requested by the user, impersonating a normal browser 
    silently to pass JS checks while remaining invisible on the desktop.
    One window is opened per scrape run and reused across all pages/sources,
    then closed automatically when the run finishes.
    """

    def __init__(self, cookie_path: Optional[str] = None, on_status=None):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._cookie_path = cookie_path
        self._on_status = on_status or (lambda msg: None)

    def _ensure_started(self):
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright isn't installed. Run:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from e

        self._on_status("Opening browser window to load gallery pages...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)

        storage_state = None
        if _storage_state_usable(self._cookie_path):
            storage_state = self._cookie_path
            print(f"[Scraper] browser state: {self._cookie_path} "
                  f"({_storage_state_cookie_count(storage_state)} cookie(s))")
        else:
            print(f"[Scraper] browser state: nothing usable at "
                  f"{self._cookie_path or '(no path)'} -- starting cold, so "
                  "any challenge has to be solved from scratch")

        self._context = self._browser.new_context(
            storage_state=storage_state,
            user_agent=REQUEST_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._context.new_page()

    def get(self, url: str, wait_selector: str = "a[href*='/video/']", timeout: int = 25000) -> Optional[str]:
        self._ensure_started()
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            html = self._page.content()
            if _is_challenge_page(html):
                deadline = time.time() + _CHALLENGE_GRACE
                while time.time() < deadline:
                    try:
                        self._page.wait_for_load_state("networkidle",
                                                       timeout=1000)
                    except Exception:
                        pass
                    html = self._page.content()
                    if not _is_challenge_page(html):
                        break
                else:
                    print(f"[Scraper] browser still on the challenge page "
                          f"after {_CHALLENGE_GRACE:.0f}s: {url}")
                _save_storage_state(self._context, self._cookie_path)
                return html
            try:
                self._page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                # Page may still have loaded fine even if this particular
                # selector never shows (e.g. an empty last page).
                self._page.wait_for_load_state("networkidle", timeout=timeout)
            html = self._page.content()
            _save_storage_state(self._context, self._cookie_path)
            return html
        except Exception as e:
            print(f"[Scraper] browser fetch error {url}: {e}")
            return None

    def close(self):
        _save_storage_state(self._context, self._cookie_path)
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None


def _host_of(url) -> str:
    match = re.match(r"https?://([^/]+)", str(url or "").strip(), re.IGNORECASE)
    if not match:
        return ""
    host = match.group(1).lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _source_for_url(site: dict, url) -> dict:
    """The gallery_sources entry whose network a URL belongs to.

    A site is a family of networks -- nubiles-porn.com, nubilefilms.com,
    brattysis.com, momlover.com -- and the per-network entry is what carries
    needs_browser, so the site dict on its own says nothing.
    """
    host = _host_of(url)
    for source in (site or {}).get("gallery_sources") or []:
        if host and _host_of(source.get("base_url")) == host:
            return source
    return {}


def _site_needs_browser(site: dict, url) -> bool:
    """Whether a page from this site has to be read in a browser.

    nubiles answers a plain HTTP client with a page titled "Security Check",
    and every one of its networks sets needs_browser -- on the gallery_sources
    entry, never on the site itself.
    """
    if (site or {}).get("needs_browser"):
        return True
    source = _source_for_url(site, url)
    if source:
        return bool(source.get("needs_browser"))
    return any(s.get("needs_browser")
               for s in (site or {}).get("gallery_sources") or [])


def _browser_state_path(db, site: dict, url) -> str:
    """Where Update keeps the browser state that got past a site's challenge.

    Reusing it matters: a fresh context is far more likely to be challenged
    again than one that has already been through. The name has to match the one
    Update writes, which is keyed on the network, not on the site.
    """
    directory = os.path.dirname(getattr(db, "db_path", "") or "") or "."
    source = _source_for_url(site, url)
    ident = str(source.get("id") or (site or {}).get("id") or "gallery")
    return os.path.join(directory, f"{ident}_browser_state.json")


_PROBE_TIMEOUT = 8          # plain HTTP gets this long before we give up
_BROWSER_TIMEOUT = 12000    # ms; 25000 measured 27 s of dead air per row
_CHALLENGE_GRACE = 5.0      # s to let an auto-solving interstitial clear
# Short on purpose. This exists to stop eleven rows re-proving the same dead
# host eleven times in a row, not to remember a verdict for the session -- the
# nubiles block is an IP ban that a VPN lifts, and the user switches one on
# without restarting the player.
_UNREACHABLE_TTL = 120.0
_UNREACHABLE_UNTIL: dict = {}
_UNREACHABLE_LOCK = threading.Lock()


def _host_unreachable(host: str) -> bool:
    with _UNREACHABLE_LOCK:
        return bool(host) and _UNREACHABLE_UNTIL.get(host, 0.0) > time.time()


def _mark_host_unreachable(host: str) -> None:
    """Remember that a host did not answer, so the next row skips it.

    The field log spent 128 s on one row and then repeated it for every other
    row of the same network: brattysis.com and nubiles-porn.com refuse the TCP
    connection outright, and each row re-proved that through curl_cffi, requests,
    urllib and a browser in turn.
    """
    if not host:
        return
    with _UNREACHABLE_LOCK:
        _UNREACHABLE_UNTIL[host] = time.time() + _UNREACHABLE_TTL


_BROWSER_FETCH_LOCK = threading.Lock()


class _browser_fetch_guard:
    """No-op stand-in when a caller already holds the browser lock."""

    def __init__(self, lock=_BROWSER_FETCH_LOCK):
        self._lock = lock

    def __enter__(self):
        if self._lock is not None:
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()
        return False


def _browser_page_html(url: str, state_path: str,
                       wait_selector: str = "a[href*='/video/']",
                       timeout: int = 25000) -> Optional[str]:
    """Fetch one page through a headless browser, one session per call.

    Sites that challenge a plain HTTP client -- nubiles answers one with a page
    titled "Security Check" -- can only be read this way. A session is opened
    and closed per call: Playwright's sync API is bound to the thread that
    started it, and this runs on a hover thread.
    """
    with _browser_fetch_guard():
        session = _BrowserGallerySession(cookie_path=state_path)
        try:
            return session.get(url, wait_selector=wait_selector, timeout=timeout)
        finally:
            session.close()


def _fetch_page(site: dict, page_url: str, db=None, session=None,
                force: bool = False):
    """Read one page with whichever transport the site actually needs.

    Returns ``(html, transport, elapsed_ms)``. ``transport`` names what
    produced the bytes -- ``browser``, ``http``, or ``none`` -- because the
    field log could not otherwise tell a browser that failed to clear a
    challenge from a plain request that never tried one, and those two need
    opposite fixes. ``elapsed_ms`` is there to judge the fetch against the
    ~1.5 s a hover can afford, which a browser launch is nowhere near.
    """
    started = time.time()

    def _ms():
        return int((time.time() - started) * 1000)

    if session is not None:
        # Caller owns the session (a batch reusing one browser).
        try:
            return session.get(page_url) or "", "browser", _ms()
        except Exception as exc:
            return "", f"browser error {type(exc).__name__}: {exc}", _ms()

    host = _host_of(page_url)
    if not force and _host_unreachable(host):
        return "", "unreachable", 0

    # Plain HTTP first. A site that answers is answered for in a few hundred
    # milliseconds -- teamskeet measured 238 ms -- and launching a browser
    # costs more than the whole budget a hover has.
    html = _fetch_html(page_url, timeout=_PROBE_TIMEOUT) or ""
    waited = _ms()
    if not html:
        if waited >= _PROBE_TIMEOUT * 900:
            # Burned the whole probe on a connect timeout: the host is not
            # answering, and no transport will change that.
            _mark_host_unreachable(host)
            return "", "unreachable", waited
    elif not _is_challenge_page(html):
        return html, "http", waited

    # A gate we can solve ourselves beats launching a browser for one, by
    # about three orders of magnitude.
    solved = solve_turnstile_challenge(page_url, html=html)
    if solved:
        return solved, "turnstile", _ms()
    if _turnstile_config(html):
        # The gate only starts on a click -- addEventListener('click',
        # startChallenge) -- and a headless browser is not going to make one.
        # Ten seconds to rediscover that, per row, is not a good trade.
        return html, "turnstile-failed", _ms()

    if not _site_needs_browser(site, page_url):
        return html, ("http" if html else "none"), waited

    # Escalate only when there is something a browser could fix: a challenge
    # page, or a site that serves nothing to a plain client at all.
    with _browser_fetch_guard():
        opened = _BrowserGallerySession(
            cookie_path=_browser_state_path(db, site, page_url))
        try:
            page = opened.get(page_url, timeout=_BROWSER_TIMEOUT) or ""
            if page:
                return page, "browser", _ms()
        except Exception as exc:
            print(f"[COVER] browser fetch failed: {type(exc).__name__}: {exc}")
        finally:
            opened.close()
    return html, ("http" if html else "none"), _ms()


def _reader_url(url: str) -> str:
    return "https://r.jina.ai/http://r.jina.ai/http://" + str(url or "").strip()


def _fetch_reader_text(url: str, timeout: int = 30) -> Optional[str]:
    """Fetch public page text through the reader endpoint when direct HTML is rate-limited."""
    reader = _reader_url(url)
    headers = dict(REQUEST_HEADERS)
    try:
        import requests
        r = requests.get(reader, headers=headers, timeout=timeout)
        r.raise_for_status()
        text = r.text or ""
        return text if "Markdown Content:" in text or "/video/watch/" in text else None
    except Exception as req_error:
        print(f"[Scraper] reader requests fallback {url}: {req_error}")

    try:
        import urllib.request
        req = urllib.request.Request(reader, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        text = raw.decode("utf-8", errors="replace")
        return text if "Markdown Content:" in text or "/video/watch/" in text else None
    except Exception as e:
        print(f"[Scraper] reader fetch error {url}: {e}")
        return None


def _harvest_slugs_from_page(html: str) -> list[str]:
    """Extract /movies/SLUG hrefs from the page HTML."""
    hits = re.findall(
        r'href=["\'](?:https?://[^"\']+)?/movies/([^"\'/?#\s]+)["\']',
        html, re.IGNORECASE
    )
    # Filter out navigation/category links (they contain no hyphens or are too short)
    seen = set()
    out  = []
    for slug in hits:
        slug = slug.strip().lower()
        if slug and "-" in slug and len(slug) > 4 and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _has_next_page(html: str, current_page: int) -> bool:
    """Return True if there's evidence of a next page."""
    # Look for next-page link or load-more indicators
    next_pat = re.compile(
        rf'(?:page[=/"\']{current_page + 1}'
        rf'|href=["\'][^"\']*page={current_page + 1})',
        re.IGNORECASE
    )
    if next_pat.search(html):
        return True
    # Also accept if there are any numbered page links beyond current
    pages = re.findall(r'[?&]page=(\d+)', html)
    if pages:
        max_page = max(int(p) for p in pages)
        return max_page > current_page
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — yt-dlp metadata enrichment
# ─────────────────────────────────────────────────────────────────────────────

def _duration_seconds(value) -> int:
    """Normalise any runtime representation to whole seconds (0 = unknown).

    Accepts plain seconds, milliseconds, "mm:ss", "hh:mm:ss" and "12m34s".
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return 0
        # Values this large are milliseconds, not a 3-hour scene.
        return int(round(v / 1000.0)) if v > 20000 else int(round(v))
    text = str(value).strip()
    if not text:
        return 0
    m = re.match(r'^(\d{1,2}):(\d{1,2}):(\d{1,2})$', text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.match(r'^(\d{1,3}):(\d{1,2})$', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r'^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?(?:s)?)?\s*(?:(\d+)\s*s)?$',
                 text, re.IGNORECASE)
    if m and any(m.groups()):
        return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
                + int(m.group(3) or 0))
    try:
        return _duration_seconds(float(text))
    except (TypeError, ValueError):
        return 0


def _query_durations(raw: str) -> set:
    """Runtime candidates spelled out in a title/filename, in seconds.

    Only explicit mm:ss / hh:mm:ss forms are read, so resolution markers like
    '1080p' or a year are never mistaken for a runtime.
    """
    out = set()
    for h, m, sec in re.findall(r'(?<![\d:])(\d{1,2}):([0-5]\d):([0-5]\d)(?![\d:])',
                                str(raw or "")):
        out.add(int(h) * 3600 + int(m) * 60 + int(sec))
    for m, sec in re.findall(r'(?<![\d:])(\d{1,3}):([0-5]\d)(?![\d:])', str(raw or "")):
        out.add(int(m) * 60 + int(sec))
    return {d for d in out if d > 0}


def _durations_agree(a: int, b: int) -> bool:
    """Same runtime, allowing for re-encode drift: 10 s or 3%, whichever is more."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(10, int(0.03 * max(a, b)))


def _player_duration_ms(player, path: str) -> int:
    """The runtime the player already knows for a playlist row, in ms.

    video_durations is populated by the local probe and by
    _schedule_remote_duration_probes, and _stream_resolution_cache keeps the
    same value for rows restored from a saved playlist.
    """
    if player is None:
        return 0
    for key in (path, _unwrap_path(path)):
        if not key:
            continue
        try:
            v = int((getattr(player, "video_durations", {}) or {}).get(key) or 0)
        except Exception:
            v = 0
        if v > 0:
            return v
        try:
            cached = (getattr(player, "_stream_resolution_cache", {}) or {}).get(key) or {}
            v = int(cached.get("duration_ms") or 0)
        except Exception:
            v = 0
        if v > 0:
            return v
    return 0


def _clean_ws(value) -> str:
    return re.sub(r"\s+", " ", html_unescape(str(value or ""))).strip()


def _slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def _extract_balanced_json_at(text: str, start: int) -> Optional[str]:
    """Return the JSON object starting at/after start by matching braces."""
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "{":
        return None

    depth = 0
    in_string = False
    escaped = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def _extract_initial_state(html: str) -> Optional[dict]:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html or "")
    if not m:
        return None
    raw = _extract_balanced_json_at(html, m.end())
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[Scraper] initial state parse error: {e}")
        return None


def _listing_entries_from_html(html: str) -> tuple[list[dict], int]:
    """Extract the movie cards embedded in /movies?page=N."""
    state = _extract_initial_state(html)
    if not state:
        return [], 0

    items = (
        state.get("content", {})
             .get("latestVideos", {})
             .get("items", {})
    )
    pages = items.get("pages") or []
    entries: list[dict] = []
    for page_items in pages:
        if isinstance(page_items, list):
            entries.extend(x for x in page_items if isinstance(x, dict))
    return entries, int(items.get("count") or 0)


def _find_movie_entry_in_state(state: dict, slug: str) -> Optional[dict]:
    """Find one movie object inside an initial-state blob."""
    content = state.get("content", {}) if isinstance(state, dict) else {}
    videos = content.get("videosContent")
    if isinstance(videos, dict):
        direct = videos.get(slug)
        if isinstance(direct, dict):
            return direct

    stack = [state]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if obj.get("id") == slug and obj.get("type") == "video":
                return obj
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    return None


def _date_to_display(raw_date) -> tuple[str, str]:
    """Return (DD/MM/YYYY, YYYY-MM-DD) where possible."""
    if raw_date in (None, ""):
        return "", ""

    if isinstance(raw_date, (int, float)):
        try:
            ts = float(raw_date)
            if ts > 9999999999:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%d/%m/%Y"), dt.strftime("%Y-%m-%d")
        except Exception:
            return "", ""

    s = str(raw_date).strip()
    if not s:
        return "", ""

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        # YYYYMMDD -> DD/MM/YYYY
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}", f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12 and b <= 12:
            # Unambiguously DD/MM already.
            return s, f"{y}-{m.group(2)}-{m.group(1)}"
        # Both networks send MM/DD/YYYY: 6319 records on teamskeet and 3138
        # on nubiles are unambiguously MM/DD against 49 and 50 that are
        # unambiguously DD/MM. So a slash date that is not unambiguously
        # DD/MM is read as MM/DD and swapped, and DD/MM/YYYY is what gets
        # stored from here on.
        return f"{m.group(2)}/{m.group(1)}/{y}", f"{y}-{m.group(1)}-{m.group(2)}"

    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.strftime("%d/%m/%Y"), dt.strftime("%Y-%m-%d")
        except Exception:
            parts = s[:10].split("-")
            return f"{parts[2]}/{parts[1]}/{parts[0]}", s[:10]

    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%d/%m/%Y"), dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    if s.isdigit():
        try:
            dt = datetime.fromtimestamp(int(s), tz=timezone.utc)
            return dt.strftime("%d/%m/%Y"), dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    return "", ""


def _normalise_display_date(raw_d: str) -> str:
    """Display a stored date as DD/MM/YYYY.

    Dates are stored canonically as DD/MM/YYYY now, so this is normally a
    no-op. It still swaps a value whose SECOND field is above 12 -- that can
    only be a legacy MM/DD/YYYY record -- so an old database on disk still
    displays correctly. An ambiguous value is left alone: guessing on those
    is what produced the mixed display in the first place.
    """
    raw_d = str(raw_d or "").strip()
    import re as _re
    _m = _re.match(r'^(\d{2})/(\d{2})/(\d{4})$', raw_d)
    if not _m:
        return raw_d
    a, b = int(_m.group(1)), int(_m.group(2))
    if b > 12 and a <= 12:
        return f"{_m.group(2)}/{_m.group(1)}/{_m.group(3)}"
    return raw_d


def _html_to_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"<\s*br\s*/?\s*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_ws(text)


def _site_movie_from_entry(entry: dict, source: Optional[dict] = None) -> Optional[dict]:
    """Map Reptyle/TeamSkeet-family initial-state movie JSON to the local DB schema."""
    if not isinstance(entry, dict):
        return None
    source = source or REPTYLE_SOURCES[0]
    base_url = str(source.get("base_url") or TEAMSKEET_BASE_URL).rstrip("/")

    slug = _clean_ws(entry.get("id") or entry.get("slug")).lower()
    if not slug:
        return None

    seo = entry.get("seo") if isinstance(entry.get("seo"), dict) else {}
    title = _clean_ws(
        entry.get("videoTitle")
        or entry.get("title")
        or entry.get("alt")
        or seo.get("title")
        or _slug_to_title(slug)
    )

    site = entry.get("site") if isinstance(entry.get("site"), dict) else {}
    site_seo = site.get("seo") if isinstance(site.get("seo"), dict) else {}
    series = _clean_ws(site.get("name") or site.get("title") or site.get("nickName"))
    if series.lower() in ("teamskeet", "team skeet", "ts"):
        series = ""
    series_slug = _clean_ws(site_seo.get("seoSlug") or site.get("nickName") or site.get("shortName"))
    if not series_slug and series:
        series_slug = _slugify(series)
    series_url = f"{base_url}/series/{series_slug}" if series_slug else ""

    models: list[str] = []
    model_urls: list[str] = []
    model_details: list[dict] = []
    raw_models = entry.get("models") or []
    if isinstance(raw_models, dict):
        raw_models = list(raw_models.values())
    for model in raw_models:
        if isinstance(model, str):
            name = _clean_ws(model)
            model_slug = _slugify(name)
            detail = {"id": model_slug, "name": name}
        elif isinstance(model, dict):
            name = _clean_ws(model.get("name") or model.get("title") or model.get("alt") or model.get("id"))
            model_slug = _clean_ws(model.get("id") or _slugify(name))
            detail = {
                "id": model_slug,
                "name": name,
                "img": model.get("img") or "",
                "cover": model.get("cover") or "",
                "stats": model.get("stats") if isinstance(model.get("stats"), dict) else {},
            }
        else:
            continue

        if not name:
            continue
        model_url = f"{base_url}/models/{model_slug}" if model_slug else ""
        detail["url"] = model_url
        if name not in models:
            models.append(name)
            model_urls.append(model_url)
            model_details.append(detail)

    date_raw = (
        entry.get("publishedDate")
        or entry.get("releaseDate")
        or entry.get("releasedDate")
        or entry.get("publishDate")
        or entry.get("createdDate")
    )
    date, date_iso = _date_to_display(date_raw)

    tags = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]
    description_html = entry.get("description") or seo.get("description") or ""

    return {
        "slug":               slug,
        "title":              title,
        "series":             series,
        "series_url":         series_url,
        "series_slug":        series_slug,
        "models":             models,
        "model_urls":         model_urls,
        "model_details":      model_details,
        "date":               date,
        "published_date":     str(date_raw or ""),
        "published_date_iso": date_iso,
        "url":                f"{base_url}/movies/{slug}",
        "source_site":        source.get("id") or "",
        "source_name":        source.get("name") or "",
        "image":              entry.get("img") or "",
        "trailer_url":        entry.get("videoTrailer") or "",
        "video_id":           entry.get("videoSrc") or entry.get("video") or "",
        "sitelogo":           entry.get("sitelogo") or "",
        "description_html":   description_html,
        "description":        _html_to_text(description_html),
        "tags":               tags,
        "stats":              entry.get("stats") if isinstance(entry.get("stats"), dict) else {},
        # Runtime is a first-class match criterion: it survives re-hosting,
        # which the site's own video_id does not.
        "duration":           _duration_seconds(
                                  entry.get("duration")
                                  or entry.get("durationSeconds")
                                  or entry.get("length")
                                  or entry.get("runtime")
                                  or (seo.get("duration") if isinstance(seo, dict) else None)
                              ),
        "item_id":            entry.get("itemId"),
        "type":               entry.get("type") or "video",
        "is_upcoming":        bool(entry.get("isUpcoming")),
        "is_x_series":        str(entry.get("isXSeries", "")).lower() == "true",
        "seo":                seo,
        "scraped_at":         _utc_now(),
        "meta_fetched":       True,
        "_tags":              tags,
        "_categories":        [],
    }


def _movie_identity_changed(existing: Optional[dict], movie: dict) -> bool:
    if not existing or not existing.get("meta_fetched"):
        return True
    # Only persisted fields, and canonicalised. `existing` comes off disk
    # where _trim_movie has already dropped empty values, while `movie` is
    # the raw builder output that still carries them, so a plain comparison
    # would see None against "" on every record and report a change at every
    # scrape -- which sets page_changed, which turns an Update back into a
    # full 175-page crawl.
    keys = ("title", "series", "models", "date", "video_id")
    return any((existing.get(k) or None) != (movie.get(k) or None) for k in keys)


def _fetch_movie_page_metadata(slug: str, source: Optional[dict] = None) -> Optional[dict]:
    source = source or REPTYLE_SOURCES[0]
    base_url = str(source.get("base_url") or TEAMSKEET_BASE_URL).rstrip("/")
    html = _fetch_html(f"{base_url}/movies/{slug}")
    if not html:
        return None
    state = _extract_initial_state(html)
    if not state:
        return None
    entry = _find_movie_entry_in_state(state, slug)
    return _site_movie_from_entry(entry, source) if entry else None


def _extract_remote_page_title(url: str) -> str:
    """Fetch a generic file-hosting page title, e.g. filester og:title."""
    html = _fetch_html(url, timeout=15)
    if not html:
        return ""

    title = ""
    patterns = [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title[^>]*>(.*?)</title>",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            title = _html_to_text(m.group(1))
            break
    if not title:
        return ""

    host = (urlparse(url).netloc or "").replace("www.", "")
    if host:
        title = re.sub(rf"\s*(?:\||-|/)\s*{re.escape(host)}\s*$", "", title, flags=re.IGNORECASE)
    return _clean_ws(title)


_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)


def _iter_anchors(html: str):
    pat = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
    href_pat = re.compile(r"""href\s*=\s*["'](?P<href>[^"']+)["']""", re.IGNORECASE)
    for match in pat.finditer(html or ""):
        attrs = match.group("attrs") or ""
        href_match = href_pat.search(attrs)
        if not href_match:
            continue
        href = html_unescape(href_match.group("href")).strip()
        text = _html_to_text(match.group("body"))
        yield match.start(), match.end(), href, text

    md_pat = re.compile(r"(?<!!)\[(?P<text>[^\[\]]{0,240}?)\]\((?P<href>https?://[^)\s]+)\)", re.DOTALL)
    for match in md_pat.finditer(html or ""):
        href = html_unescape(match.group("href")).strip()
        text = _clean_ws(re.sub(r"\s+", " ", match.group("text") or ""))
        yield match.start(), match.end(), href, text


def _is_gallery_video_href(href: str, base_url: str) -> bool:
    try:
        path = urlparse(urljoin(base_url, href)).path
    except Exception:
        return False
    return bool(re.search(r"/video/(?:watch/)?\d+(?:/|$)", path, re.IGNORECASE))


_IMG_SRC_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["\'](?P<src>[^"\']+)["\']""", re.IGNORECASE)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((?P<src>https?://[^)\s]+)\)")

# A gallery that lazy-loads ships <img data-src="the real cover" src="a 1px
# placeholder">, and reading only src= then finds nothing -- which is what an
# empty image field looks like from the outside. Scan the whole tag for any of
# the attributes the common lazy-load libraries use, plus srcset and a video
# poster, and a CSS background as a last resort.
_IMG_TAG_RE = re.compile(r"<(?:img|video|source)\b[^>]*>", re.IGNORECASE)
_IMG_ATTR_RE = re.compile(
    r"""\b(?P<name>src|data-src|data-original|data-lazy-src|data-lazy|data-cfsrc|"""
    r"""data-srcset|srcset|poster)\s*=\s*["\'](?P<src>[^"\']+)["\']""",
    re.IGNORECASE)
_SRCSET_ENTRY_RE = re.compile(r"^(?P<url>\S+)(?:\s+(?P<w>\d+)w)?", re.IGNORECASE)


def _best_srcset_url(value: str) -> str:
    """The widest URL in a srcset list, or its first when none are labelled.

    The real nubiles card carries three variants in one data-srcset --
    cover320, cover640 and cover960 -- so taking the first would cache a
    320 px image for a card that is wider than that.
    """
    best_url, best_w = "", -1
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _SRCSET_ENTRY_RE.match(part)
        if not m:
            continue
        url = m.group("url")
        w = int(m.group("w") or 0)
        if w > best_w:
            best_url, best_w = url, w
    return best_url
_BG_URL_RE = re.compile(
    r"""background(?:-image)?\s*:\s*url\(\s*["\']?(?P<src>https?://[^"\')\s]+)""",
    re.IGNORECASE)
_COVER_URL_HINT_RE = re.compile(r"/samples/|/videos/|cover|thumb|/med\.|psmcdn",
                                re.IGNORECASE)


def _cover_url_candidates(region: str):
    """Every plausible cover URL in a chunk of markup, nearest-first."""
    out = []
    for match in _MD_IMG_RE.finditer(region):
        out.append(match.group("src"))
    for tag in _IMG_TAG_RE.finditer(region):
        for attr in _IMG_ATTR_RE.finditer(tag.group(0)):
            value = attr.group("src")
            if str(attr.group("name") or "").lower().endswith("srcset"):
                value = _best_srcset_url(value)
            out.append(value)
    for bg in _BG_URL_RE.finditer(region):
        out.append(bg.group("src"))
    seen = set()
    for src in out:
        src = html_unescape(str(src or "")).strip()
        if not src or src.startswith("data:") or src in seen:
            continue
        seen.add(src)
        if _COVER_URL_HINT_RE.search(src):
            yield src


def _gallery_cover_image(html: str, title_start: int, window: int = 4000) -> str:
    """The card's cover image, from the markup around its title.

    The usual card is <a href=watch><img src=cover></a> followed by the title
    link, so the cover is the last image before the title anchor -- that is
    tried first, taking the LAST candidate because it is the nearest. Some
    layouts put the image after the title instead, so that region is tried
    too, taking the FIRST candidate for the same reason. Handles the real HTML
    the Playwright session returns and the markdown the reader fallback
    returns.
    """
    text = html or ""
    before = text[max(0, title_start - window):title_start]
    cands = list(_cover_url_candidates(before))
    if cands:
        return cands[-1]
    after = text[title_start:title_start + window]
    cands = list(_cover_url_candidates(after))
    if cands:
        return cands[0]
    return ""


_MEDIA_SRC_RE = re.compile(
    r"""(?:\bsrc\s*=\s*|\bdata-[a-z-]*\s*=\s*|\(|["\'])"""
    r"""["\']?(?P<src>https?://[^"\'\s)]+?\.mp4[^"\'\s)]*)""", re.IGNORECASE)


def _cdn_folder_name(text: str) -> str:
    """Title or series -> the CDN folder/name form the site uses."""
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def preview_loop_url(title: str, series: str, height: int = 480) -> str:
    """The hover-preview loop for a scene, derived from stored fields.

    Verified against a URL captured live from the gallery with a media
    sniffer:

        https://images.nubiles-porn.com/videos/stepmom_is_a_great_kisser/
          videos/loops/momsteachsex_stepmom_is_a_great_kisser_loop_480.mp4

    reproduced exactly from title='Stepmom Is A Great Kisser' and
    series='MomsTeachSex'. NOTE: unsigned it returns 403, so this only
    locates the asset -- a signature has to come from a loaded page.
    """
    t = _cdn_folder_name(title)
    if not t:
        return ""
    ser = _cdn_folder_name(series)
    name = f"{ser}_{t}_loop_{height}.mp4" if ser else f"{t}_loop_{height}.mp4"
    return f"https://images.nubiles-porn.com/videos/{t}/videos/loops/{name}"


def _gallery_preview_video(html: str, title_start: int, window: int = 4000) -> str:
    """The hover-preview mp4 for a card, from the markup just before its title.

    The player injects it on hover, so it may sit in a <source>, a data-*
    attribute or an inline script; anything ending in .mp4 in the card region
    is taken, preferring the /loops/ path the site actually uses.
    """
    region = (html or "")[max(0, title_start - window):title_start]
    found = []
    for match in _MEDIA_SRC_RE.finditer(region):
        src = html_unescape(match.group("src") or "").strip()
        if src and src not in found:
            found.append(src)
    for src in found:
        if "/loops/" in src or "_loop_" in src:
            return src
    return found[-1] if found else ""


def _image_expiry_epoch(url: str) -> int:
    """The signed URL's expiry, from its e= parameter (0 if unsigned/unknown).

    Measured on nubiles-porn.com: e=1789617600 -> 2026-09-17T04:00:00Z and
    e=1789621200 -> 2026-09-17T05:00:00Z, fetched at 03:33Z, i.e. the signature
    is minted for roughly an hour. Requesting the same path with no signature
    returns 403, so a stored URL is a dead link almost immediately -- which is
    why the bytes are downloaded at scrape time instead.
    """
    m = re.search(r"[?&]e=(\d{9,11})", str(url or ""))
    return int(m.group(1)) if m else 0


def _fetch_bytes(url: str, timeout: int = 20, referer: str = "",
                 diag: list | None = None) -> bytes:
    """Binary GET through the same impersonating client the scraper uses.

    `referer` overrides the module default when the caller knows which network
    the asset belongs to: REQUEST_HEADERS claims teamskeet, which is right for
    images.psmcdn.net and wrong everywhere else. `diag` receives a short reason
    on failure, because "no cover" otherwise looks the same whether the markup
    changed, the signature expired, or the CDN refused the request.
    """
    try:
        from curl_cffi import requests as _cff
    except Exception:
        if diag is not None:
            diag.append("curl_cffi unavailable")
        return b""
    headers = dict(REQUEST_HEADERS)
    headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if referer:
        headers["Referer"] = referer
    try:
        r = _cff.get(url, headers=headers, timeout=timeout,
                     impersonate="chrome110")
        if diag is not None:
            diag.append(f"HTTP {r.status_code}")
        if r.status_code == 200:
            return r.content or b""
    except Exception as exc:
        if diag is not None:
            diag.append(f"{type(exc).__name__}: {exc}"[:140])
    return b""


def _referer_for(site: dict) -> str:
    """The Referer a network's own image CDN expects, '' if unknown."""
    s = site or {}
    for key in ("base_url", "gallery_url"):
        value = str(s.get(key) or "").strip()
        if value:
            return value if value.endswith("/") else value + "/"
    return ""


_ANY_URL_RE = re.compile(r"""https?://[^"'\s)<>]+""")


def _signed_urls(html: str):
    """Every URL on a page that carries a signature, de-duplicated."""
    seen = set()
    for match in _ANY_URL_RE.finditer(html or ""):
        url = html_unescape(match.group(0)).rstrip('"\'),;')
        if "st=" in url and url not in seen:
            seen.add(url)
            yield url


def signed_cover_url(html: str, hint: str = "") -> str:
    """This movie's own cover out of a freshly loaded page, '' if there is none.

    A watch page mints a new signature for its cover on every load -- measured
    live: cover1280, cover960 and cover614 all carrying the same fresh e= -- so
    a hover can recover a cover an hour after the scrape without anything being
    written to disk. The widest variant wins.

    `hint` is a title or series and is a hard filter: the same page also carries
    the covers of its related videos, and picking one of those would show the
    wrong scene.
    """
    needle = _cdn_folder_name(hint) if hint else ""
    best, best_key = "", (0, -1)
    for url in _signed_urls(html):
        match = re.search(r"/samples/cover(\d+)\.jpe?g", url, re.IGNORECASE)
        if not match:
            continue
        if needle and needle not in url.lower():
            continue
        key = (int(match.group(1)), _image_expiry_epoch(url))
        if key > best_key:
            best, best_key = url, key
    return best


_PERMANENT_HOSTS = ("psmcdn.net",)
_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|webp|png)(?:[?#]|$)", re.IGNORECASE)


def permanent_cover_url(html: str, hint: str = "") -> str:
    """An unsigned, never-expiring cover off the page, '' if there is none.

    teamskeet serves images.psmcdn.net/<code>/<folder>/shared/med.jpg with no
    signature at all, so it needs none of this re-minting. The field log shows
    its watch page loading over plain HTTP in 238 ms and then yielding nothing,
    because signed_cover_url only ever looks at URLs carrying st=.
    """
    needle = _cdn_folder_name(hint) if hint else ""
    best, best_rank = "", -1
    for match in _ANY_URL_RE.finditer(html or ""):
        url = html_unescape(match.group(0)).rstrip('"\'),;')
        low = url.lower()
        if not any(h in low for h in _PERMANENT_HOSTS):
            continue
        if not _IMG_EXT_RE.search(url):
            continue
        if needle and needle not in low:
            continue
        # /shared/med.jpg is the standard cover; a thumbnail is better than
        # nothing but worse than that.
        rank = 2 if "/shared/med." in low else (1 if "/shared/" in low else 0)
        if rank > best_rank or (rank == best_rank and len(url) > len(best)):
            best, best_rank = url, rank
    return best


def page_cover_url(html: str, hint: str = "") -> str:
    """The page's own cover: a fresh signature if it mints them, else a
    permanent one. A signed nubiles cover dies in about an hour; a teamskeet
    one never expires, and asking which kind a site uses is the caller's
    problem no more."""
    return signed_cover_url(html, hint) or permanent_cover_url(html, hint)


def signed_media_url(html: str, hint: str = "") -> str:
    """A freshly signed preview video out of a page, '' if it carries none.

    Only the gallery page has one. A watch page does not -- its player sources
    are not in the HTML, measured on six watch pages out of six -- but a page
    that does carry one should not be ignored.
    """
    needle = _cdn_folder_name(hint) if hint else ""
    best, best_exp = "", -1
    for url in _signed_urls(html):
        if ".mp4" not in url.lower():
            continue
        if needle and needle not in url.lower():
            continue
        exp = _image_expiry_epoch(url)
        if exp > best_exp:
            best, best_exp = url, exp
    return best


_COVER_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_COVER_CACHE_MAX = 96
_COVER_CACHE_LOCK = threading.Lock()
_COVER_INFLIGHT: set = set()
_COVER_LOCK = threading.Lock()


def cover_bytes(slug: str) -> bytes:
    """The cover held in memory for a movie, or b''."""
    with _COVER_CACHE_LOCK:
        data = _COVER_CACHE.get(slug)
        if data:
            _COVER_CACHE.move_to_end(slug)
        return data or b""


def ensure_cover_async(player, site: dict, slug: str, url: str) -> bool:
    """Fetch a cover into memory. Nothing is ever written to disk for it.

    The card paints it on the next poll of the same hover, so a cover costs one
    image request the first time a row is hovered and nothing after that.
    """
    if not url or not slug or player is None:
        return False
    with _COVER_CACHE_LOCK:
        if slug in _COVER_CACHE:
            return False
    key = ((site or {}).get("id"), slug)
    with _COVER_LOCK:
        if key in _COVER_INFLIGHT:
            return False
        _COVER_INFLIGHT.add(key)
    referer = _referer_for(site)

    def _work():
        try:
            data = _fetch_bytes(url, referer=referer)
            if len(data) >= 512:
                with _COVER_CACHE_LOCK:
                    _COVER_CACHE[slug] = data
                    _COVER_CACHE.move_to_end(slug)
                    while len(_COVER_CACHE) > _COVER_CACHE_MAX:
                        _COVER_CACHE.popitem(last=False)
        except Exception:
            pass
        finally:
            with _COVER_LOCK:
                _COVER_INFLIGHT.discard(key)

    threading.Thread(target=_work, daemon=True).start()
    return True


# A hover polls for a late cover, and each poll re-reads the record, so an
# in-flight guard alone is not enough: the moment a fetch finishes the next
# poll would start another. The field log showed one movie fetching its watch
# page fifteen times in a minute. So the attempt is stamped up front and not
# retried for a while, whatever the outcome.
_REFRESH_COOLDOWN = 900.0
_REFRESH_LAST: dict = {}
_REFRESH_DUMPS = [0]
_REFRESH_DUMP_MAX = 3
_REFRESH_LOCK = threading.Lock()


_CHALLENGE_TITLE_RE = re.compile(
    r"^(?:\s|\W)*(?:just a moment|security check|attention required|"
    r"access denied|verify you are (?:a )?human|are you a robot|"
    r"cloudflare|ddos|enable javascript and cookies)", re.IGNORECASE)


def _page_title(html) -> str:
    """The <title> of a page, '' if it has none."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "",
                      re.IGNORECASE | re.DOTALL)
    return (" ".join(html_unescape(match.group(1)).split())[:120]
            if match else "")


def _is_challenge_page(html) -> bool:
    """Is this an interstitial rather than the page that was asked for?

    The earlier test looked for "challenge-platform" anywhere in the body.
    Every Cloudflare-fronted page carries that script, so a real teamskeet
    watch page -- 90852 bytes, titled "Stepmom Fertilizer | Exclusive
    TeamSkeet Porn Video", loaded over plain HTTP in 238 ms -- was reported as
    a bot challenge. The title is the signal, and a page with no title is not
    called a challenge, because guessing wrong here sends a 25 s browser fetch
    after a page that was already fine.
    """
    if not html:
        return False
    title = _page_title(html)
    return bool(title) and bool(_CHALLENGE_TITLE_RE.match(title))


_TURNSTILE_CONFIG_RE = re.compile(
    r"var\s+turnstileConfig\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_TURNSTILE_SESSION = None
_TURNSTILE_PAUSE = 0.4      # s before re-reading; these sites 429 on a burst
_TURNSTILE_MAX_NONCE = 4_000_000


def _turnstile_config(html) -> dict:
    """The challenge parameters a page is hiding behind, {} if there are none.

    This is not Cloudflare Turnstile despite the class names. It is the site's
    own gate: an inline config carrying a proof-of-work challenge, and a script
    that hashes nonces in a web worker until one comes out with enough leading
    zero bits. That matters, because a proof of work is arithmetic -- it can be
    done here, in milliseconds, without a browser.
    """
    match = _TURNSTILE_CONFIG_RE.search(html or "")
    if not match:
        return {}
    try:
        cfg = json.loads(match.group(1))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _has_leading_zero_bits(digest: bytes, bits: int) -> bool:
    """Mirror of the page's checkLeadingZeroBits, bit for bit."""
    full_bytes, rem_bits = divmod(int(bits), 8)
    if digest[:full_bytes] != b"\x00" * full_bytes:
        return False
    if rem_bits and (digest[full_bytes] >> (8 - rem_bits)):
        return False
    return True


def solve_turnstile_pow(challenge: str, difficulty: int,
                        max_nonce: int = _TURNSTILE_MAX_NONCE) -> str:
    """The nonce the gate wants, or '' if none is found within max_nonce.

    SHA-256("<challenge>:<nonce>") with `difficulty` leading zero bits. At the
    difficulty the site actually serves -- 15 -- that is about 32768 hashes on
    average, which is tens of milliseconds here. The browser spent 16919 ms on
    the same page and came back with the challenge still up.
    """
    if not challenge:
        return ""
    difficulty = int(difficulty or 0)
    if difficulty <= 0 or difficulty > 40:
        return ""
    prefix = f"{challenge}:".encode("utf-8")
    for nonce in range(max_nonce):
        digest = hashlib.sha256(prefix + str(nonce).encode("utf-8")).digest()
        if _has_leading_zero_bits(digest, difficulty):
            return str(nonce)
    return ""


def _environment_checks() -> dict:
    """The values the gate's collectEnvironmentChecks() would have gathered.

    Sent because the page sends them. Nothing here is validated against a real
    browser -- it is a plausible desktop Chrome on Windows, which is what the
    player is running on.
    """
    try:
        offset = int((time.altzone if (time.daylight and time.localtime().tm_isdst)
                      else time.timezone) / 60)
    except Exception:
        offset = 0
    return {
        "screenWidth": 1920,
        "screenHeight": 1080,
        "hasCanvas": True,
        "hasWebGL": True,
        "colorDepth": 24,
        "timezoneOffset": offset,
        "languages": "en-US,en",
        "platform": "Win32",
        "cookieEnabled": True,
    }


def solve_turnstile_challenge(url: str, html=None, timeout: int = 15) -> str:
    """Walk a page's own proof-of-work gate and return the page behind it.

    '' when there is no such gate, the proof cannot be solved, or the server
    refuses it.

    `html` is the challenge page the caller has already fetched, and passing it
    is the whole point: these sites answer a second request from the same IP
    with HTTP 429 and an empty body. A run that re-fetched logged exactly that
    -- "no gate config in what came back (0 byte(s), HTTP 429)" -- and gave up
    holding a perfectly good challenge page it had already been handed.
    """
    global _TURNSTILE_SESSION
    try:
        import requests
    except ImportError:
        print("[TURNSTILE] the requests library is unavailable, cannot solve")
        return ""
    if _TURNSTILE_SESSION is None:
        _TURNSTILE_SESSION = requests.Session()
        _TURNSTILE_SESSION.headers.update(REQUEST_HEADERS)
    session = _TURNSTILE_SESSION
    try:
        if html:
            held = str(html)
            origin = "already fetched"
        else:
            first = session.get(url, timeout=timeout)
            held = first.text or ""
            origin = f"HTTP {first.status_code}"
        cfg = _turnstile_config(held)
        challenge = str(cfg.get("challenge") or "")
        difficulty = int(cfg.get("difficulty") or 0)
        if not challenge or not difficulty:
            print(f"[TURNSTILE] no gate config in the page ({origin}, "
                  f"{len(held)} byte(s), title {_page_title(held)!r}) "
                  "-- nothing to solve")
            return ""
        print(f"[TURNSTILE] gate on {urlparse(url).netloc}: "
              f"difficulty {difficulty}, solving")
        started = time.time()
        nonce = solve_turnstile_pow(challenge, difficulty)
        if not nonce:
            print(f"[TURNSTILE] no nonce found for difficulty {difficulty}")
            return ""
        parsed = urlparse(url)
        verify = f"{parsed.scheme}://{parsed.netloc}/turnstile/verify"
        reply = session.post(verify, json={
            "nonce": nonce,
            "timestamp": cfg.get("timestamp"),
            "difficulty": difficulty,
            "environmentChecks": _environment_checks(),
            "returnTo": cfg.get("returnTo") or parsed.path,
        }, timeout=timeout)
        try:
            verdict = reply.json()
        except Exception:
            verdict = {}
        if not verdict.get("success"):
            print(f"[TURNSTILE] verify refused: "
                  f"{verdict.get('error') or reply.status_code}")
            return ""
        time.sleep(_TURNSTILE_PAUSE)
        again = session.get(url, timeout=timeout)
        page = again.text or ""
        solved_ms = int((time.time() - started) * 1000)
        if page and not _is_challenge_page(page):
            print(f"[TURNSTILE] cleared in {solved_ms} ms "
                  f"(nonce {nonce}, difficulty {difficulty})")
            return page
        print(f"[TURNSTILE] verified but the page is still a challenge "
              f"after {solved_ms} ms")
        return ""
    except Exception as exc:
        print(f"[TURNSTILE] {type(exc).__name__}: {exc}")
        return ""


def _describe_page(page: str, transport: str = "", elapsed_ms=None) -> str:
    """One line saying what a fetched page actually held.

    "No signed asset" on its own cannot tell an empty response from a bot
    challenge from real markup that simply carries no signature, and those
    three need completely different fixes.
    """
    head = []
    if transport:
        head.append(f"via {transport}")
    if elapsed_ms is not None:
        head.append(f"{int(elapsed_ms)} ms")
    if not page:
        return ", ".join(head + ["empty response"])
    match = re.search(r"<title[^>]*>(.*?)</title>", page,
                      re.IGNORECASE | re.DOTALL)
    title = " ".join(html_unescape(match.group(1)).split())[:60] if match else ""
    low = page.lower()
    bits = [f"{len(page)} byte(s)",
            f"img-host {low.count('images.nubiles-porn.com')}",
            f"srcset {low.count('data-srcset')}",
            f"signed {page.count('st=')}",
            f"loops {low.count('/loops/')}",
            f"samples {low.count('/samples/')}"]
    if title:
        bits.append(f'title "{title}"')
    if _is_challenge_page(page):
        bits.insert(0, "BOT CHALLENGE, not the page")
    return ", ".join(head + bits)


def refresh_signed_assets_async(player, site: dict, movie: dict, db,
                                force: bool = False) -> bool:
    """Try to mint a fresh signature for one movie, on demand, from its page.

    A stored nubiles signature dies in about an hour, so a hover with no cover
    re-reads the movie's watch page and keeps any signed URL it finds. That is
    a best effort, not a guarantee: the field log shows those pages arriving as
    ~14 KB with no signature in them at all, which is why the failure is
    reported with a description of the page rather than a bare byte count, and
    why the first few are written out for inspection.

    The preview loop is not on that page, so it stays whatever the last scrape
    stored -- which is why previews still need an Update.
    """
    slug = str((movie or {}).get("slug") or "")
    page_url = str((movie or {}).get("url") or "")
    if not slug or not page_url or player is None or db is None:
        return False
    now = time.time()
    with _REFRESH_LOCK:
        if (not force
                and now - float(_REFRESH_LAST.get(slug, 0.0)) < _REFRESH_COOLDOWN):
            return False
        _REFRESH_LAST[slug] = now
    hint = str(movie.get("title") or "") or str(movie.get("series") or "")
    record = dict(movie)
    dump_dir = str(getattr(player, "data_dir", "") or "")

    def _work():
        try:
            page, transport, elapsed = _fetch_page(site, page_url, db,
                                                   force=force)
            cover = page_cover_url(page, hint)
            loop = signed_media_url(page, hint)
            if not cover and not loop:
                print(f"[COVER] {slug}: watch page carried no signed asset "
                      f"({_describe_page(page, transport, elapsed)})")
                if page and dump_dir and _REFRESH_DUMPS[0] < _REFRESH_DUMP_MAX:
                    _REFRESH_DUMPS[0] += 1
                    dest = os.path.join(dump_dir, f"watchpage_{slug}.html")
                    try:
                        with open(dest, "w", encoding="utf-8") as fh:
                            fh.write(page)
                        print(f"[COVER] wrote {dest} for inspection")
                    except Exception:
                        pass
                return
            if cover:
                record["image"] = cover
                record["image_expires"] = _image_expiry_epoch(cover)
            if loop:
                record["preview"] = loop
                record["preview_expires"] = _image_expiry_epoch(loop)
            db.upsert(record)
            db.save()
            print(f"[COVER] {slug}: re-signed"
                  + (" cover" if cover else "")
                  + (" + preview" if loop else "")
                  + f" (via {transport}, {int(elapsed)} ms)")
            if cover:
                ensure_cover_async(player, site, slug, cover)
        except Exception as exc:
            print(f"[COVER] {slug}: {type(exc).__name__}: {exc}")

    threading.Thread(target=_work, daemon=True).start()
    return True


def _gallery_slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    video_id = ""
    title_slug = ""
    for idx, part in enumerate(parts):
        if part == "watch" and idx + 1 < len(parts):
            video_id = parts[idx + 1]
            if idx + 2 < len(parts):
                title_slug = parts[idx + 2]
            break
        if part == "video" and idx + 1 < len(parts) and parts[idx + 1].isdigit():
            video_id = parts[idx + 1]
            if idx + 2 < len(parts):
                title_slug = parts[idx + 2]
            break
    slug = _slugify(title_slug or path.rsplit("/", 1)[-1])
    return f"{video_id}-{slug}" if video_id and slug else (slug or video_id)


def _looks_like_gallery_title(text: str) -> bool:
    if not text or len(text) < 6:
        return False
    t = text.strip().lower()
    bad = (
        "new season", "play now", "full access", "get access", "downloads",
        "to view this video", "enable javascript", "previous next",
    )
    return any(ch.isalpha() for ch in t) and not any(b in t for b in bad)


def _gallery_total_pages(html: str) -> int:
    pages = [int(p) for p in re.findall(r"\b1\s+of\s+(\d{1,4})\b", html or "", re.IGNORECASE)]
    if pages:
        return max(pages)
    pages = [int(p) for p in re.findall(r"/video/(?:gallery|recent)/(\d{1,4})(?:[\"'/?#\s]|$)", html or "", re.IGNORECASE)]
    return max(pages) if pages else 0


def _gallery_movies_from_html(html: str, site: dict) -> list[dict]:
    base_url = site.get("base_url") or site.get("gallery_url") or ""
    base_host = (urlparse(base_url).netloc or "").replace("www.", "")
    title_hits: dict[str, dict] = {}

    for start, end, href, text in _iter_anchors(html):
        if not _is_gallery_video_href(href, base_url) or not _looks_like_gallery_title(text):
            continue
        url = urljoin(base_url, href)
        current = title_hits.get(url)
        if not current or len(text) > len(current["title"]):
            title_hits[url] = {"start": start, "end": end, "url": url, "title": text}

    title_list = sorted(title_hits.values(), key=lambda item: item["start"])
    movies = []
    for idx, hit in enumerate(title_list):
        block_end = title_list[idx + 1]["start"] if idx + 1 < len(title_list) else min(len(html), hit["end"] + 2600)
        block = html[hit["end"]:block_end]

        image = _gallery_cover_image(html, hit["start"])
        preview = _gallery_preview_video(html, hit["start"])
        models = []
        model_urls = []
        series = ""
        series_url = ""
        for _, _, href, text in _iter_anchors(block):
            if not text:
                continue
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            host = (parsed.netloc or "").replace("www.", "")
            path = parsed.path.lower()
            if "/model/" in path or "/models/" in path:
                if text not in models:
                    models.append(text)
                    model_urls.append(full_url)
                continue
            if not series and host and host != base_host and "members." not in host and "support" not in text.lower():
                series = text
                series_url = full_url
            elif not series and "/series/" in path:
                series = text
                series_url = full_url

        if not series and site.get("name"):
            series = site.get("name")
            series_url = base_url

        date = ""
        date_iso = ""
        date_match = _MONTH_DATE_RE.search(block[:1800])
        if date_match:
            date, date_iso = _date_to_display(date_match.group(0))

        slug = _gallery_slug_from_url(hit["url"])
        if not slug:
            continue
        title = re.sub(r"\s*-\s*S\d+\s*:\s*E\d+\s*$", "", hit["title"], flags=re.IGNORECASE)
        movies.append({
            "slug":               slug,
            "title":              title,
            "series":             series,
            "series_url":         series_url,
            "series_slug":        _slugify(series),
            "models":             models,
            "model_urls":         model_urls,
            "model_details":      [{"name": m, "url": u, "id": _slugify(m)} for m, u in zip(models, model_urls)],
            "date":               date,
            "published_date":     date_match.group(0) if date_match else "",
            "published_date_iso": date_iso,
            "url":                hit["url"],
            "image":              image,
            "image_expires":      _image_expiry_epoch(image),
            "preview":            preview,
            "preview_expires":    _image_expiry_epoch(preview),
            "source_site":        site.get("id") or "",
            "source_name":        site.get("name") or "",
            # NOTE: this literal used to repeat "image": "" further down, and a
            # duplicate key in a dict literal silently wins -- which is why
            # image was empty in every one of the 5371 shipped records even
            # though the cover URL was sitting in the same HTML.
            "trailer_url":        "",
            "video_id":           slug.split("-", 1)[0] if slug else "",
            "description":        "",
            "tags":               [],
            "stats":              {},
            "type":               "video",
            "scraped_at":         _utc_now(),
            "meta_fetched":       True,
            "_tags":              [],
            "_categories":        [],
        })
    return movies


def _gallery_source_config(parent_site: dict, source: dict) -> dict:
    merged = dict(source or parent_site or {})
    merged.setdefault("page_size", parent_site.get("page_size") or 12)
    merged.setdefault("reader_fallback", parent_site.get("reader_fallback", True))
    merged.setdefault("db_filename", parent_site.get("db_filename", ""))
    merged.setdefault("overrides_filename", parent_site.get("overrides_filename", OVERRIDES_FILENAME))
    return merged


def _ytdlp_fetch_movie(slug: str, ytdlp_path: str, source: Optional[dict] = None) -> Optional[dict]:
    """
    Call yt-dlp --dump-json on one movie page.
    Returns a parsed movie dict or None on failure.
    """
    source = source or REPTYLE_SOURCES[0]
    base_url = str(source.get("base_url") or TEAMSKEET_BASE_URL).rstrip("/")
    url = f"{base_url}/movies/{slug}"
    try:
        result = subprocess.run(
            [ytdlp_path,
             "--dump-json",
             "--no-playlist",
             "--no-warnings",
             "--quiet",
             url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        data = json.loads(result.stdout.strip().splitlines()[-1])
        return _parse_ytdlp_movie(data, slug, source)

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"[Scraper] yt-dlp error for {slug}: {e}")
        return None


def _parse_ytdlp_movie(data: dict, slug: str, source: Optional[dict] = None) -> dict:
    """
    Map yt-dlp JSON fields → our movie schema.

    yt-dlp returns different field names depending on the site extractor,
    so we check multiple fallback keys for each piece of metadata.
    """
    source = source or REPTYLE_SOURCES[0]
    BASE = str(source.get("base_url") or TEAMSKEET_BASE_URL).rstrip("/")

    # ── title ─────────────────────────────────────────────────────────────────
    title = (
        data.get("title")
        or data.get("fulltitle")
        or _slug_to_title(slug)
    ).strip()

    # ── models / cast ─────────────────────────────────────────────────────────
    models:     list[str] = []
    model_urls: list[str] = []

    # yt-dlp puts performers in: cast, actor, actors, tags, categories, creator
    raw_cast = (
        data.get("cast")
        or data.get("actor")
        or data.get("actors")
        or []
    )
    if isinstance(raw_cast, str):
        raw_cast = [raw_cast]

    # Also mine tags and categories for model names
    tags       = data.get("tags", []) or []
    categories = data.get("categories", []) or []

    for name in raw_cast:
        name = str(name).strip()
        if name and name not in models:
            models.append(name)
            model_urls.append(f"{BASE}/models/{name.lower().replace(' ', '-')}")

    # ── series / channel ──────────────────────────────────────────────────────
    series = (
        data.get("series")
        or data.get("channel")
        or data.get("uploader")
        or data.get("creator")
        or ""
    ).strip()

    # Remove generic site name
    if series.lower() in ("teamskeet", "team skeet", "ts"):
        series = ""

    series_url = ""
    if series:
        series_slug = series.lower().replace(" ", "-")
        series_url  = f"{BASE}/series/{series_slug}"

    # ── date ──────────────────────────────────────────────────────────────────
    date = ""
    raw_date = (
        data.get("release_date")
        or data.get("upload_date")
        or data.get("timestamp")
        or ""
    )
    if raw_date:
        raw_date = str(raw_date)
        # YYYYMMDD
        m = re.match(r'^(\d{4})(\d{2})(\d{2})$', raw_date)
        if m:
            date = f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
        # YYYY-MM-DD
        elif re.match(r'^\d{4}-\d{2}-\d{2}', raw_date):
            parts = raw_date[:10].split("-")
            date  = f"{parts[2]}/{parts[1]}/{parts[0]}"
        # Unix timestamp
        elif raw_date.isdigit():
            try:
                dt   = datetime.fromtimestamp(int(raw_date), tz=timezone.utc)
                date = dt.strftime("%d/%m/%Y")
            except Exception:
                pass

    return {
        "slug":         slug,
        "title":        title,
        "series":       series,
        "series_url":   series_url,
        "models":       models,
        "model_urls":   model_urls,
        "date":         date,
        "url":          f"{BASE}/movies/{slug}",
        "source_site":  source.get("id") or "",
        "source_name":  source.get("name") or "",
        "duration":     _duration_seconds(data.get("duration")),
        "scraped_at":   _utc_now(),
        "meta_fetched": True,
        # preserve raw yt-dlp tags for richer matching
        "_tags":        [str(t) for t in tags],
        "_categories":  [str(c) for c in categories],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scraper QThread
# ─────────────────────────────────────────────────────────────────────────────

class ScrapeSignals(QObject):
    progress  = pyqtSignal(str)
    tick      = pyqtSignal(int, int)   # done, total
    finished  = pyqtSignal(int)
    error     = pyqtSignal(str)


class TeamSkeetScraper(QThread):
    """
    TeamSkeet scraper thread.

    mode="full"   - crawl all listing pages and store full card metadata
    mode="update" - crawl newest pages until already-known metadata is reached
    mode="enrich" - fetch individual movie pages for entries still missing data
    """

    def __init__(self, db: MetadataDB, mode: str = "full",
                 ytdlp_path: Optional[str] = None,
                 sources: Optional[list[dict]] = None):
        super().__init__()
        self.db          = db
        self.mode        = mode
        self.ytdlp_path  = ytdlp_path or _find_ytdlp()
        self.sources     = sources or REPTYLE_SOURCES
        self.signals     = ScrapeSignals()
        self._cancel     = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.signals.error.emit(str(e))

    def _run(self):
        # ── Phase 1: harvest slugs ────────────────────────────────────────────
        changed_slugs: list[str] = []

        if self.mode != "enrich":
            changed_slugs = self._harvest_slugs()
            if self._cancel:
                return

        # Listing pages now carry most metadata. This fallback is only for old
        # stubs or rare pages that failed to expose the initial-state JSON.
        if self.mode == "enrich":
            to_enrich = self.db.stubs_needing_enrichment()
        elif self.mode == "full" or self.db.count_enriched() == 0:
            to_enrich = self.db.stubs_needing_enrichment()
        else:
            to_enrich = []

        total = len(to_enrich)
        if total:
            self.signals.progress.emit(
                f"Fallback: enriching {total} movie page(s) missing listing metadata..."
            )

        enriched = len(changed_slugs)
        for i, slug in enumerate(to_enrich):
            if self._cancel:
                break

            self.signals.progress.emit(
                f"[{i+1}/{total}] Fetching movie page: {slug}"
            )
            self.signals.tick.emit(i + 1, total)

            source = (getattr(self, "_stub_sources", {}) or {}).get(slug) or self.sources[0]
            movie = _fetch_movie_page_metadata(slug, source)
            if not movie and self.ytdlp_path:
                movie = _ytdlp_fetch_movie(slug, self.ytdlp_path, source)
            if movie:
                self.db.upsert(movie)
                enriched += 1
                self.signals.progress.emit(
                    f"  OK {movie['title']}"
                    + (f" | {movie['series']}" if movie.get('series') else "")
                    + (f" | {', '.join(movie['models'])}" if movie.get('models') else "")
                    + (f" | {movie['date']}" if movie.get('date') else "")
                )
            else:
                self.signals.progress.emit(f"  Failed: {slug}")

            if i and i % 10 == 0:
                self.db.save()

            time.sleep(SCRAPE_DELAY)

        self.db.save()
        if self.mode == "full":
            self.db.mark_full_scrape()
        else:
            self.db.mark_update_scrape()
        self.db.save()

        self.signals.finished.emit(enriched)

    def _harvest_slugs(self) -> list[str]:
        """Collect movie metadata from listing pages."""
        changed_slugs: list[str] = []
        self._stub_sources = {}
        stop_after = 2

        for source_index, source in enumerate(self.sources):
            if self._cancel:
                break
            page = 1
            consecutive_unchanged = 0
            estimated_pages = 0
            movies_url = str(source.get("movies_url") or "").rstrip("/")
            source_name = source.get("name") or movies_url

            while not self._cancel:
                url = movies_url
                if page > 1:
                    url = movies_url + f"?page={page}"

                self.signals.progress.emit(f"Harvesting {source_name} page {page}...")
                html = _fetch_html(url)
                if not html:
                    self.signals.progress.emit(f"  Failed to fetch {source_name} page {page}, stopping this source.")
                    break

                entries, total_count = _listing_entries_from_html(html)
                if entries and total_count and not estimated_pages:
                    estimated_pages = max(1, (total_count + len(entries) - 1) // len(entries))

                if not entries:
                    # Last-resort fallback for a changed site shell: keep slug
                    # stubs rather than losing track of pages entirely.
                    slugs = _harvest_slugs_from_page(html)
                    for slug in slugs:
                        self.db.upsert_stub(slug)
                        self._stub_sources.setdefault(slug, source)
                    if slugs:
                        self.signals.progress.emit(
                            f"  {source_name} page {page}: {len(slugs)} slug stubs harvested | DB: {self.db.count()}"
                        )
                    else:
                        self.signals.progress.emit(f"  No movies on {source_name} page {page}, done.")
                        break
                else:
                    page_changed = False
                    page_slugs: list[str] = []
                    duplicate_count = 0
                    for entry in entries:
                        movie = _site_movie_from_entry(entry, source)
                        if not movie:
                            slug = _clean_ws(entry.get("id") or entry.get("slug")).lower()
                            if slug:
                                self.db.upsert_stub(slug)
                                self._stub_sources.setdefault(slug, source)
                            continue

                        slug = movie["slug"]
                        page_slugs.append(slug)
                        existing = self.db.movies.get(slug)
                        if existing and existing.get("meta_fetched"):
                            if _movie_identity_changed(existing, movie):
                                # Keep the first network copy as canonical, but
                                # remember where else it was seen.
                                sources_seen = list(existing.get("sources_seen") or [])
                                src_id = source.get("id") or ""
                                if src_id and src_id not in sources_seen:
                                    existing = dict(existing)
                                    sources_seen.append(src_id)
                                    existing["sources_seen"] = sources_seen
                                    self.db.upsert(existing)
                                    page_changed = True
                            else:
                                duplicate_count += 1
                            continue
                        if _movie_identity_changed(existing, movie):
                            page_changed = True
                            if slug not in changed_slugs:
                                changed_slugs.append(slug)
                        movie["sources_seen"] = [source.get("id") or ""] if source.get("id") else []
                        self.db.upsert(movie)
                    total_hint = f"/{estimated_pages}" if estimated_pages else ""
                    self.signals.progress.emit(
                        f"  {source_name} page {page}{total_hint}: {len(page_slugs)} movies parsed"
                        + (f" | {duplicate_count} duplicate(s)" if duplicate_count else "")
                        + f" | DB: {self.db.count()}"
                    )
                    self.signals.tick.emit(source_index * 1000 + page, len(self.sources) * 1000)

                    if self.mode == "update" and not page_changed:
                        consecutive_unchanged += 1
                        if consecutive_unchanged >= stop_after:
                            self.signals.progress.emit(f"  {source_name}: caught up - no new listing metadata.")
                            break
                    else:
                        consecutive_unchanged = 0

                if not entries and not _has_next_page(html, page):
                    self.signals.progress.emit(f"  No next page link after {source_name} page {page}.")
                    break

                if page % 5 == 0:
                    self.db.save()
                page += 1
                time.sleep(float(source.get("page_delay") or PAGE_DELAY))

        self.db.save()
        return changed_slugs


class NetworkGalleryScraper(QThread):
    """
    Generic HTML gallery scraper for MomLover/Nubiles-style network pages.

    mode="full" crawls all pages found in pagination.
    mode="update" stops after two unchanged pages.
    mode="enrich" is a no-op because the listing already carries the fields
    needed by the matcher.
    """

    def __init__(self, db: MetadataDB, site: dict, mode: str = "full"):
        super().__init__()
        self.db      = db
        self.site    = site
        self.mode    = mode
        self.signals = ScrapeSignals()
        self._cancel = False
        self.sources = [_gallery_source_config(site, src) for src in (site.get("gallery_sources") or [site])]
        self._active_source = self.sources[0]
        self._active_gallery_url = self._active_source.get("gallery_url", "")
        self._active_page_template = self._active_source.get("page_url_template", "")
        self._browser_session: Optional[_BrowserGallerySession] = None

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            if self._browser_session is not None:
                self._browser_session.close()
                self._browser_session = None

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self._active_gallery_url
        template = self._active_page_template or (self._active_gallery_url.rstrip("/") + "/{page}")
        offset = (page - 1) * int(self._active_source.get("page_size") or 12)
        return template.format(page=page, offset=offset)

    def _fetch_gallery_page(self, page: int) -> Optional[str]:
        url = self._page_url(page)

        if self._active_source.get("needs_browser"):
            if self._browser_session is None:
                db_dir = os.path.dirname(getattr(self.db, "db_path", "") or "") or "."
                cookie_path = os.path.join(
                    db_dir,
                    f"{self._active_source.get('id') or self.site.get('id') or 'gallery'}_browser_state.json",
                )
                self._browser_session = _BrowserGallerySession(
                    cookie_path=cookie_path,
                    on_status=lambda msg: self.signals.progress.emit(f"  {msg}"),
                )
            return self._browser_session.get(url)

        html = _fetch_html(url)
        if not html and self._active_source.get("reader_fallback"):
            self.signals.progress.emit("  Direct fetch blocked/rate-limited; trying reader fallback.")
            html = _fetch_reader_text(url)
            if html:
                return html
        if html or page != 1:
            return html
        fallback_urls = list(self._active_source.get("fallback_gallery_urls") or [])
        fallback_templates = list(self._active_source.get("fallback_page_url_templates") or [])
        for index, fallback_url in enumerate(fallback_urls):
            self.signals.progress.emit(f"  Primary gallery unavailable; trying {fallback_url}")
            html = _fetch_html(fallback_url)
            if not html and self._active_source.get("reader_fallback"):
                html = _fetch_reader_text(fallback_url)
            if html:
                self._active_gallery_url = fallback_url
                self._active_page_template = (
                    fallback_templates[index]
                    if index < len(fallback_templates)
                    else fallback_url.rstrip("/") + "/{page}"
                )
                return html
        return None

    def _run(self):
        if self.mode == "enrich":
            self.signals.progress.emit("This provider reads full metadata from listing pages; run Update or Full Scrape.")
            self.signals.finished.emit(0)
            return

        changed = 0
        parsed_any = False

        for source_index, source in enumerate(self.sources):
            if self._cancel:
                break
            self._active_source = source
            self._active_gallery_url = source.get("gallery_url", "")
            self._active_page_template = source.get("page_url_template", "")
            page = 1
            max_pages = 0
            consecutive_unchanged = 0
            src_covers = 0
            src_movies = 0
            # The cover CDN signs its URLs so they cannot be hotlinked, and
            # REQUEST_HEADERS claims teamskeet -- the right referer for
            # images.psmcdn.net, the wrong one here.
            source_name = source.get("name") or self.site.get("name", "site")

            while not self._cancel:
                url = self._page_url(page)
                if not url:
                    break
                self.signals.progress.emit(f"Harvesting {source_name} page {page}...")
                html = self._fetch_gallery_page(page)
                if not html:
                    self.signals.progress.emit(f"  Failed to fetch {source_name} page {page}, stopping this source.")
                    break

                if not max_pages:
                    max_pages = _gallery_total_pages(html)

                movies = _gallery_movies_from_html(html, source)
                if not movies:
                    self.signals.progress.emit(f"  No movie cards parsed on {source_name} page {page}, stopping this source.")
                    break
                parsed_any = True

                page_changed = False
                duplicate_count = 0
                for movie in movies:
                    existing = self.db.movies.get(movie["slug"])
                    if existing and existing.get("meta_fetched"):
                        src_id = source.get("id") or ""
                        sources_seen = existing.get("sources_seen")
                        if sources_seen is None:
                            # Record predates the field (the DBs shipped
                            # before it was on the keep-list). Seed it from
                            # source_site so the bookkeeping converges
                            # instead of being re-added on every scrape.
                            seed = str(existing.get("source_site") or src_id or "")
                            sources_seen = [seed] if seed else []
                        else:
                            sources_seen = list(sources_seen)
                        if src_id and src_id not in sources_seen:
                            existing = dict(existing)
                            sources_seen.append(src_id)
                            existing["sources_seen"] = sources_seen
                            self.db.upsert(existing)
                            # Deliberately NOT page_changed. Recording which
                            # source a known movie also appears on is
                            # bookkeeping, not new content -- flagging it
                            # meant a page of entirely-known movies still
                            # counted as changed, so the "caught up - no new
                            # listing metadata" early-stop could never fire
                            # and every Update crawled all 175 pages of all
                            # four sources.
                        else:
                            duplicate_count += 1
                        # A record scraped before covers were captured has
                        # none, and nothing else will ever give it one: the
                        # signed URL only exists while the gallery page is
                        # being read. Backfill it here.
                        #
                        # This one DOES set page_changed, unlike the
                        # sources_seen bookkeeping above. A backfill is
                        # one-time work -- once the record carries an image
                        # the branch stops firing for it -- so the first
                        # Update after this change crawls the whole catalogue
                        # to fill in 5371 missing covers, and every Update
                        # after that converges on the first pass again. The
                        # preview half is not one-time: its signature dies in
                        # about an hour, so it re-arms on every Update.
                        _want_image = bool(movie.get("image")) and not existing.get("image")
                        # compact_records() drops a preview whose signature has
                        # expired, so a record with none here either never had
                        # one or lost it. This page is carrying a live signature
                        # for it right now, and since previews are streamed
                        # rather than downloaded, refreshing the stored URL is
                        # the only thing that keeps a nubiles row previewing.
                        _want_preview = (bool(movie.get("preview"))
                                         and not existing.get("preview"))
                        if _want_image or _want_preview:
                            existing = dict(existing)
                            if _want_image:
                                existing["image"] = movie["image"]
                                existing["image_expires"] = int(movie.get("image_expires") or 0)
                            if _want_preview:
                                existing["preview"] = movie["preview"]
                                existing["preview_expires"] = int(movie.get("preview_expires") or 0)
                            self.db.upsert(existing)
                            page_changed = True
                        continue
                    if _movie_identity_changed(existing, movie):
                        page_changed = True
                        changed += 1
                    movie["sources_seen"] = [source.get("id") or ""] if source.get("id") else []
                    self.db.upsert(movie)
                # Cover telemetry: if a gallery changes its markup or starts
                # lazy-loading, the cover silently stops being captured and
                # the only symptom is a hover card with nothing in it. Saying
                # how many were found per page makes that visible at once.
                covers = sum(1 for m in movies if m.get("image"))
                src_covers += covers
                src_movies += len(movies)
                total_hint = f"/{max_pages}" if max_pages else ""
                self.signals.progress.emit(
                    f"  {source_name} page {page}{total_hint}: {len(movies)} movies parsed"
                    + f" | covers {covers}/{len(movies)}"
                    + (f" | {duplicate_count} duplicate(s)" if duplicate_count else "")
                    + f" | DB: {self.db.count()}"
                )
                self.signals.tick.emit(source_index * 1000 + page, len(self.sources) * 1000)

                if self.mode == "update" and not page_changed:
                    consecutive_unchanged += 1
                    if consecutive_unchanged >= 2:
                        self.signals.progress.emit(f"  {source_name}: caught up - no new listing metadata.")
                        break
                else:
                    consecutive_unchanged = 0

                if page % 5 == 0:
                    self.db.save()
                if max_pages and page >= max_pages:
                    break

                page += 1
                time.sleep(PAGE_DELAY)

            self.signals.progress.emit(
                f"  {source_name}: {src_covers} cover(s) captured from "
                f"{src_movies} card(s) across {max(0, page - 1)} page(s)")

        self.db.save()
        if parsed_any or self.db.count() > 0:
            if self.mode == "full":
                self.db.mark_full_scrape()
            else:
                self.db.mark_update_scrape()
        else:
            self.signals.progress.emit("No metadata saved; scrape did not reach a readable gallery page.")
        self.db.save()
        self.signals.finished.emit(changed)


# ─────────────────────────────────────────────────────────────────────────────
# TitleMatcher — improved
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    s = str(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.replace("_", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _compact(s: str) -> str:
    return _normalise(s).replace(" ", "")


_STOP = {"the", "and", "with", "for", "this", "that", "from", "into",
         "you", "her", "his", "she", "him", "our", "out", "are", "was",
         "not", "but", "just", "get", "got", "its", "all"}

_STRIP_TAIL = re.compile(
    r'\b(1080p?|720p?|2160p?|4k|480p?|hd|fhd|uhd|x264|x265|hevc|avc|'
    r'mkv|mp4|avi|mov|wmv|webm|flv|m4v|xvid|divx)\b', re.IGNORECASE
)


def _tokens(s: str) -> set[str]:
    s = str(s).replace("_", " ")
    s = _STRIP_TAIL.sub(" ", s)
    s = re.sub(r'\d{4}[-/]\d{2}[-/]\d{2}', " ", s)
    s = re.sub(r'\d{8}', " ", s)
    return {t for t in _normalise(s).split() if len(t) > 2 and t not in _STOP}


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _date_digits(s: str) -> str:
    """Extract only digits from a date string."""
    return re.sub(r"\D", "", s)


_QDATE_SEP = r'[./\-]'
_QDATE_BARE  = re.compile(r'(?<!\d)(\d{8})(?!\d)')
_QDATE_ISO   = re.compile(r'(?<!\d)(\d{4})' + _QDATE_SEP + r'(\d{1,2})'
                          + _QDATE_SEP + r'(\d{1,2})(?!\d)')
_QDATE_DMY   = re.compile(r'(?<!\d)(\d{1,2})' + _QDATE_SEP + r'(\d{1,2})'
                          + _QDATE_SEP + r'(\d{4})(?!\d)')
_QDATE_SHORT = re.compile(r'(?<!\d)(\d{2})' + _QDATE_SEP + r'(\d{2})'
                          + _QDATE_SEP + r'(\d{2})(?!\d)')


def _query_date_keys(raw: str) -> set:
    """Every YYYYMMDD key a date inside a filename or URL could stand for.

    The old pattern wanted a four-digit year and a dash or a slash, which left
    out the two forms this material is actually named with: YY.MM.DD, as in
    "MomComes First.26.06.07.Brianna Beach...", and DD.MM.YYYY. The release
    date was therefore invisible to the matcher for most rows, and the
    strongest corroborating signal after runtime never fired -- which is how a
    file could be matched on its scene title with nothing to contradict it.

    A DD.MM.YYYY pair is offered in both orderings, mirroring _date_keys on the
    record side: which of the two a site meant is not knowable from here, and
    guessing one silently drops the other. A YY.MM.DD is not, because that
    convention is unambiguous. Parts that cannot be a month and a day are
    dropped, so a duration like 10.12.45 never becomes a date.
    """
    keys: set = set()
    text = str(raw or "")

    def _add(year: str, month: str, day: str) -> None:
        if len(year) == 2:
            year = ("20" if int(year) <= 69 else "19") + year
        month, day = month.zfill(2), day.zfill(2)
        if not 1 <= int(month) <= 12 or not 1 <= int(day) <= 31:
            return
        keys.add(f"{year}{month}{day}")

    for m in _QDATE_BARE.finditer(text):
        s = m.group(1)
        _add(s[:4], s[4:6], s[6:])
    for m in _QDATE_ISO.finditer(text):
        _add(m.group(1), m.group(2), m.group(3))
    for m in _QDATE_DMY.finditer(text):
        a, b, y = m.group(1), m.group(2), m.group(3)
        _add(y, b, a)
        _add(y, a, b)
    for m in _QDATE_SHORT.finditer(text):
        _add(m.group(1), m.group(2), m.group(3))
    return keys


def _date_keys(date: str) -> set[str]:
    """Every digit-ordering a stored date could be queried as.

    Dates are stored DD/MM/YYYY. The query side just strips separators, so a
    pasted link may arrive as ISO, DD/MM or MM/DD -- the record has to offer
    all three, or a valid date silently stops matching the moment the storage
    format changes. It did: while dates were stored MM/DD, the ISO key was
    built from the wrong pair and a DD/MM query never hit.
    """
    keys: set[str] = set()
    parts = re.match(r'(\d{2})/(\d{2})/(\d{4})', str(date or ""))
    if parts:
        dd, mm, yyyy = parts.groups()
        keys.add(f"{yyyy}{mm}{dd}")   # ISO
        keys.add(f"{dd}{mm}{yyyy}")   # DD/MM/YYYY as typed
        keys.add(f"{mm}{dd}{yyyy}")   # MM/DD/YYYY as typed
    return keys


class TitleMatcher:
    # Signals are not equally trustworthy, and treating them as if they were
    # is exactly what made the linker auto-apply the WRONG movie. 'site'
    # fires for every movie scraped from that source whenever the query
    # mentions the source, and 'series' fires for every movie in the series,
    # so ('series','site') is the most common two-signal combination in the
    # whole database -- while 'id' is a unique numeric video id.
    #
    # Measured against the shipped nubiles DB (tools_eval_metadata_linker.py,
    # 1500 movies x 5 query shapes) with the old sort key
    # (signal_count, score):
    #     page_url 67.0%   host_style 71.7%   display_name 98.8%
    # The exact movie lost to a same-series one because _score had NO term
    # for the video id and none for a whole-title match, so the two
    # decisive signals were invisible to the ranking.
    # Weighted by how well each criterion survives being re-hosted. A video
    # id only exists on the creator's own site, so it is useless for the
    # links actually pasted in (pixeldrain, bunkr, gofile, tube mirrors) and
    # is held down to a tie-breaker; name, actors, series, date and runtime
    # all carry across to a mirror, so they drive the decision.
    _SIGNAL_WEIGHTS = {
        'scene':         0.60,  # the whole scene title appears in the query
        'duration':      0.55,  # runtime survives any re-host, near-exact
        'date':          0.45,
        'model':         0.35,  # per model, capped by _MODEL_SIGNAL_CAP
        'id':            0.35,  # creator-site-only; tie-breaker, not a driver
        'series':        0.20,
        'scene_partial': 0.15,  # only two or more overlapping title tokens
        'site':          0.05,  # near-free: every movie from that source
    }
    _MODEL_SIGNAL_CAP = 0.50
    # An id with nothing corroborating it. Video ids are only 5-6 digits
    # (2938 six-digit, 2433 five-digit in the shipped nubiles DB), so an
    # unrelated URL or filename that happens to carry such a number -- e.g.
    # randomtube.example/watch/248755/totally-unrelated -- would otherwise
    # rename the row to a movie it has nothing to do with. Held below
    # HIGH_CONFIDENCE_STRENGTH so it is offered as a possible match for the
    # user to confirm rather than auto-applied. A real source URL always
    # carries the site and/or the scene title alongside the id, so it is
    # unaffected.
    _LONE_ID_STRENGTH = 0.30
    # A scene title is not an identifier either. "Breaking the Rules" exists in
    # more than one network, the preview path searches every site and takes the
    # first hit, so a lone scene signal showed whichever network's copy sorted
    # first -- 93 titles are shared between the shipped teamskeet and nubiles
    # databases alone, and a MomComesFirst file whose real record is in neither
    # was given a Hijab Hookup poster on the strength of the title alone.
    # Held below HIGH_CONFIDENCE_STRENGTH for the same reason a lone id is:
    # offered as a possible match to confirm, never auto-applied. A real
    # filename carries the site, series, a performer or a date alongside the
    # title, and any one of those corroborates it back above the line.
    _LONE_SCENE_STRENGTH = 0.45
    # Auto-apply threshold: 'id' or 'scene' alone clears it, while the
    # ('series','site') pair at 0.25 does not.
    HIGH_CONFIDENCE_STRENGTH = 0.60

    def __init__(self, db: MetadataDB):
        self.db = db
        self._records: dict[str, dict] = {}
        self._token_index: dict[str, set[str]] = {}
        self._date_index: dict[str, set[str]] = {}
        self._build_index()

    def _build_index(self):
        for slug, movie in self.db.movies.items():
            if not movie.get("meta_fetched"):
                continue

            title = movie.get("title", "")
            series = movie.get("series", "")
            models = movie.get("models", []) or []
            slug_tokens = _tokens(slug.replace("-", " "))
            title_tokens = _tokens(title)
            model_tokens = set()
            search_tokens = set(slug_tokens) | set(title_tokens) | _tokens(series)
            for compact_token in (_compact(title), _compact(series)):
                if len(compact_token) > 2:
                    search_tokens.add(compact_token)
            for model in models:
                tokens = _tokens(model)
                model_tokens |= tokens
                search_tokens |= tokens
                compact_model = _compact(model)
                if len(compact_model) > 2:
                    search_tokens.add(compact_model)

            record = {
                "movie": movie,
                "slug": slug,
                "title": title,
                "series": series,
                "models": models,
                "date": movie.get("date", ""),
                "slug_tokens": slug_tokens,
                "title_tokens": title_tokens,
                "model_tokens": model_tokens,
                "title_norm": _normalise(title),
                "title_compact": _compact(title),
                "series_norm": _normalise(series),
                "series_compact": _compact(series),
                "model_norms": [_normalise(m) for m in models if m],
                "model_compacts": [_compact(m) for m in models if m],
                "video_id": movie.get("video_id", ""),
                "duration": int(movie.get("duration") or 0),
                "site_norm": _compact(movie.get("source_site", "") or movie.get("source_name", "")),
            }
            self._records[slug] = record

            for token in search_tokens:
                self._token_index.setdefault(token, set()).add(slug)
            for key in _date_keys(record["date"]):
                self._date_index.setdefault(key, set()).add(slug)

    def match(self, raw: str, duration_ms: int = 0) -> Optional[dict]:
        matches = self.match_candidates(raw, limit=1, include_weak=False,
                                        duration_ms=duration_ms)
        return matches[0]["movie"] if matches else None

    def match_candidates(self, raw: str, limit: int = 5, include_weak: bool = True,
                         duration_ms: int = 0) -> list[dict]:
        if not raw or not self._records:
            return []

        q_norm   = _normalise(raw)
        q_compact = _compact(raw)
        q_tokens = _tokens(raw)
        q_date_d = _query_date_keys(raw)
        q_dur = _query_durations(raw)
        try:
            if int(duration_ms or 0) > 0:
                q_dur = q_dur | {int(int(duration_ms) / 1000)}
        except (TypeError, ValueError):
            pass

        candidate_slugs = self._candidate_slugs(q_tokens, q_date_d)
        # Deliberately NOT falling back to every record when the inverted
        # index turns up nothing. _candidate_slugs indexes title, series,
        # model and slug tokens plus release dates, so a query that shares
        # none of them cannot match a record on any real signal -- the only
        # things left are the fuzzy _sim terms, which produce noise. Scoring
        # the whole database instead cost ~1 s per query on the 5371-movie
        # nubiles DB and ~246 of 300 sampled playlist rows (pixeldrain,
        # bunkr, gofile URLs -- exactly the ones this app is used with) hit
        # that path, inside _run_match's loop, on the UI thread.
        if not candidate_slugs:
            return []

        matches = []

        for slug in candidate_slugs:
            record = self._records.get(slug)
            if not record:
                continue
            score = self._score(raw, q_norm, q_tokens, q_date_d, record, q_dur)
            signals = self._match_signals(raw, q_norm, q_compact, q_tokens, q_date_d,
                                          record, q_dur)
            strength = self._signal_strength(signals)
            if score < MATCH_THRESHOLD and not signals:
                continue
            if not include_weak and strength < self.HIGH_CONFIDENCE_STRENGTH:
                continue
            confidence = ("high" if strength >= self.HIGH_CONFIDENCE_STRENGTH
                          else "possible")
            matches.append({
                "movie": record["movie"],
                "score": score,
                "signals": signals,
                "signal_count": len(signals),
                "strength": round(strength, 4),
                "confidence": confidence,
            })

        # Strength first: a unique video id must beat a pile of cheap
        # population-level signals however well the fuzzy score reads. The
        # slug is the final tie-break so that two equally-plausible records
        # always resolve the same way instead of following set iteration
        # order over candidate_slugs.
        matches.sort(key=lambda item: (-item["strength"], -item["score"],
                                       str((item.get("movie") or {}).get("slug")
                                           or "")))
        return matches[:max(1, int(limit or 1))]

    def _match_signals(self, raw: str, q_norm: str, q_compact: str, q_tokens: set,
                       q_date_d: set, record: dict, q_dur: set = ()) -> list[str]:
        signals = []
        if record["series_norm"] and (
            record["series_norm"] in q_norm or record["series_compact"] in q_compact
        ):
            signals.append("series")

        title_overlap = q_tokens & (record["title_tokens"] - record["model_tokens"])
        if record["title_compact"] and record["title_compact"] in q_compact:
            signals.append("scene")
        elif len(title_overlap) >= 2:
            # Two or more shared title tokens is weak on this material:
            # 'stepsis' and 'friend' alone are enough to trip it.
            signals.append("scene_partial")

        for model_name, mn, mc in zip(record["models"], record["model_norms"], record["model_compacts"]):
            matched = bool(mn and mn in q_norm) or bool(mc and mc in q_compact)
            if not matched and mc:
                matched = any(_sim(mc, token) >= 0.88 for token in q_tokens if len(token) >= 6)
            if matched:
                signals.append(f"model:{model_name}")

        if record["date"] and q_date_d and (q_date_d & _date_keys(record["date"])):
            signals.append("date")

        rec_dur = int(record.get("duration") or 0)
        if rec_dur > 0 and any(_durations_agree(rec_dur, d) for d in (q_dur or ())):
            signals.append("duration")

        if record.get("video_id") and record["video_id"] in q_norm.split():
            signals.append("id")
        elif record.get("video_id") and f"/{record['video_id']}/" in raw:
            signals.append("id")

        if record.get("site_norm") and record["site_norm"] in q_compact:
            signals.append("site")

        return signals

    def _signal_strength(self, signals: list) -> float:
        """Specificity-weighted strength of a signal list.

        Model matches are summed but capped, so a six-performer scene cannot
        outvote a unique video id on cast size alone.
        """
        total = 0.0
        model_total = 0.0
        sigs = [str(s) for s in (signals or ())]
        if sigs == ["id"]:
            return self._LONE_ID_STRENGTH
        if sigs == ["scene"]:
            return self._LONE_SCENE_STRENGTH
        for sig in sigs:
            if str(sig).startswith("model:"):
                model_total += self._SIGNAL_WEIGHTS["model"]
                continue
            total += self._SIGNAL_WEIGHTS.get(str(sig), 0.0)
        return total + min(model_total, self._MODEL_SIGNAL_CAP)

    def _candidate_slugs(self, q_tokens: set, q_date_d: set) -> set[str]:
        hits: dict[str, int] = {}
        for token in q_tokens:
            for slug in self._token_index.get(token, ()):
                hits[slug] = hits.get(slug, 0) + 1
        for key in q_date_d:
            for slug in self._date_index.get(key, ()):
                hits[slug] = hits.get(slug, 0) + 3

        if not hits:
            return set()

        # Tie-break on the slug, not on dict insertion order: hits is filled
        # by iterating the q_tokens SET, whose order Python randomises per
        # process, so a stable sort on count alone made ranked[:80] -- and
        # therefore the answer -- differ between runs of the same query.
        ranked = sorted(hits.items(), key=lambda item: (-item[1], item[0]))
        cutoff = 1 if len(q_tokens) <= 3 else 2
        candidates = {slug for slug, count in ranked[:80] if count >= cutoff}
        if len(candidates) < 40:
            candidates.update(slug for slug, _ in ranked[:80])
        return candidates

    def _score(self, raw: str, q_norm: str, q_tokens: set,
               q_date_d: set, record: dict, q_dur: set = ()) -> float:
        title   = record["title"]
        series  = record["series"]
        date    = record["date"]

        # S1: slug token overlap — most reliable signal
        slug_tokens = record["slug_tokens"]
        union = q_tokens | slug_tokens
        inter = q_tokens & slug_tokens
        s_slug = len(inter) / len(union) if union else 0.0

        # S2: title string similarity
        s_title = _sim(q_norm, record["title_norm"]) if title else 0.0

        # S3: title token overlap
        title_tokens = record["title_tokens"]
        union2 = q_tokens | title_tokens
        inter2 = q_tokens & title_tokens
        s_title_tok = len(inter2) / len(union2) if union2 else 0.0

        # S4: series name anywhere in query
        s_series = 0.0
        if series:
            series_n = record["series_norm"]
            if series_n in q_norm:
                s_series = 0.35
            elif _sim(q_norm, series_n) > 0.6:
                s_series = 0.20

        # S5: model name in query
        s_model = 0.0
        for mn in record["model_norms"]:
            if mn in q_norm:
                s_model = 0.25
                break
            if _sim(q_norm, mn) > 0.75:
                s_model = 0.15
                break

        # S6: date match — very strong signal
        s_date = 0.0
        if date and q_date_d:
            if q_date_d & _date_keys(date):
                s_date = 0.45

        # S7: the unique numeric video id. Worth more than everything else
        # combined, and previously absent from this sum entirely -- which is
        # why an exact /watch/248755/ URL scored below a same-series movie
        # that merely shared the site name.
        s_id = 0.0
        vid = str(record.get("video_id") or "")
        if vid and (vid in q_norm.split() or f"/{vid}/" in str(raw)):
            s_id = 0.55

        # S8: runtime. Kept in the sum for the same reason the video id had
        # to be -- a signal that is invisible to the score cannot rank.
        s_dur = 0.0
        rec_dur = int(record.get("duration") or 0)
        if rec_dur > 0 and any(_durations_agree(rec_dur, d) for d in (q_dur or ())):
            s_dur = 0.45

        score = (
            s_slug      * 0.30 +
            s_title     * 0.20 +
            s_title_tok * 0.15 +
            s_series    +
            s_model     +
            s_date      +
            s_dur       +
            s_id
        )
        return score


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


_REMOTE_FOLDER_PREFIX = "__remote_folder__::"


def _is_remote_folder_entry(path: str) -> bool:
    return isinstance(path, str) and path.startswith(_REMOTE_FOLDER_PREFIX)


def _unwrap_path(path: str) -> str:
    """Strip the remote-folder prefix if present, returning a plain URL or path."""
    if _is_remote_folder_entry(path):
        return path[len(_REMOTE_FOLDER_PREFIX):]
    return path


def _extract_raw_title(file_path: str) -> str:
    # Unwrap folder entries so we get the bare URL/path
    inner = _unwrap_path(file_path)
    if inner.startswith("http://") or inner.startswith("https://"):
        parsed = urlparse(inner)
        seg    = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return unquote(seg) or parsed.netloc
    base = os.path.basename(inner)
    if base.startswith("-"):
        base = base[1:].lstrip()
    name, _ = os.path.splitext(base)
    return name


MALE_PERFORMERS = {
    'jay romero', 'parker ambrose', 'damon dice', 'codey steele', 'juan el caballo loco',
    'tyler nixon', 'ricky spanish', 'tony', 'jayrock', 'kyle mason', 'van wylde',
    'ryan driller', 'nathan bronson', 'rion king', 'nikki nuttz', 'robby apples',
    'raul costa', 'will pounder', 'joshua lewis', 'bambino', 'charles dera',
    'bruce venture', 'logan pierce', 'oliver flynn', 'max fills', 'jayden marcos',
    'renato', 'lucas frost', 'diego perez', 'alex charger', 'brad sterling',
    'ricky rascal', 'sam bourne', 'seth gamble', 'logan long', 'jimmy michaels',
    'tyler cruise', 'johnny castle', 'jake adams', 'ryan mclane', 'enzo east',
    'michael fly', 'ethan seeks', 'lucky fate', 'quinton james', 'justin hunt',
    'stanley johnson', 'jason x', 'max dior', 'nick ross', 'nick strokes',
    'alex mack', 'alex legend', 'alex adams', 'axel haze', 'lutro', 'arin jones',
    'jimmy bud', 'leo valentino', 'peter fox', 'zane walker', 'wrex oliver',
    'johnny sins', 'manuel ferrara', 'danny d', 'mick blue', 'small hands',
    'ramon nomar', 'xander corvus', 'isiah maxwell', 'james deen', 'chad alva',
    'kieran lee', 'tommy pistol', 'jax slayher', 'barrett blade', 'michael vegas',
    'markus dupree', 'troy francisco', 'jmac', 'j-mac', 'christian clay',
    'danny mountain', 'john strong', 'keiran lee', 'aaron wilcoxxx', 'alberto blanco',
    # Extended from the shipped nubiles database: these are the credited male
    # performers it actually carries that the list above was missing, ordered
    # by how often they turn up. 1916 distinct performers appear there and the
    # curated list covered 81, so most men still reached the display name.
    # Only names read off that data, and only ones that are unambiguously men
    # -- a false positive hides a performer the user does want to see, which
    # is worse than a man slipping through and being hidden by hand.
    'charlie dean', 'anthony pierce', 'marcus london', 'kristof cale',
    'victor ray', 'apollo banks', 'rico hernandez', 'matt denae',
    'clarke kent', 'johnny', 'thomas stone', 'roman knight', 'ken feels',
    'murgur', 'gunnar bishop', 'brick danger', 'matthew meier', 't stone',
    'mike ox', 'angelo godshack', 'tommy gold', 'zac wild', 'richard glaze',
    'tyler steel', 'denis reed', 'tysen rich', 'danny steele',
    'ralf christian', 'charlie red', 'nade nasty',
    # Well-known names that turn up lower down the same casts.
    'ricky sinz', 'evan stone', 'lexington steele', 'mandingo', 'nacho vidal',
    'erik everhard', 'jon jon', 'mr pete', 'scott nails', 'danny wylde',
    'kurt lockwood', 'tony everready', 'dale dabone', 'sean michaels',
    'johnny thrust', 'wesley pipes', 'mark wood', 'robby blake',
    'adam black', 'adam ocelot', 'derrick pierce', 'jedd harris', 'steve holmes',
    'mike adriano', 'rocco siffredi', 'trenton eclipse', 'toni ribas', 'bill bailey',
    'tommy gunn', 'chad white', 'sterling cooper', 'alex puller', 'models'
}


MALE_PERFORMERS_FILENAME = "male_performers.json"

# The built-in list above is finite and the cast is not: 1916 distinct
# performers in the shipped nubiles DB, 81 of them on it. There is no gender
# field in either network's data -- a model entry carries only name, img,
# cover and stats -- and scene count does not separate them either (the most
# prolific unlisted names are women). So the list has to be extendable by the
# person looking at the row, and it has to survive a restart.
_HIDDEN_PERFORMERS: set = set()


def hidden_performers_path(app_dir) -> str:
    return os.path.join(str(app_dir or "."), MALE_PERFORMERS_FILENAME)


def load_hidden_performers(app_dir) -> set:
    global _HIDDEN_PERFORMERS
    path = hidden_performers_path(app_dir)
    names = set()
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            names = {str(n).strip().lower()
                     for n in (data or []) if str(n).strip()}
    except Exception as exc:
        print(f"[MetadataScraper] could not read {path}: "
              f"{type(exc).__name__}: {exc}")
    _HIDDEN_PERFORMERS = names
    return names


def hide_performer(app_dir, name: str) -> bool:
    """Leave a performer out of every display name, permanently."""
    norm = str(name or "").strip().lower()
    if not norm:
        return False
    _HIDDEN_PERFORMERS.add(norm)
    path = hidden_performers_path(app_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(_HIDDEN_PERFORMERS), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        print(f"[MetadataScraper] could not save {path}: "
              f"{type(exc).__name__}: {exc}")
        return False


def _is_male_performer(name: str) -> bool:
    if not name:
        return False
    norm = str(name).strip().lower()
    if norm in MALE_PERFORMERS or norm in _HIDDEN_PERFORMERS:
        return True
    tokens = norm.split()
    if any(w in tokens for w in ['stepbro', 'stepson', 'stepdad', 'husband', 'boyfriend', 'guy', 'dad', 'brother', 'father', 'son']):
        return True
    return False


def format_display_name(movie: dict) -> str:
    """Series - Model1 & Model2 - Title - Date"""
    parts = []
    series = (
        movie.get("series")
        or movie.get("source_name")
        or _source_display_name(movie)
        or movie.get("site_name")
        or movie.get("site")
        or movie.get("studio")
    )
    if series and str(series).strip():
        parts.append(str(series).strip())
    if movie.get("models"):
        female_models = [m.strip() for m in movie["models"] if m and not _is_male_performer(m)]
        if female_models:
            parts.append(" & ".join(female_models))
    if movie.get("title"):
        parts.append(movie["title"].strip())
    if movie.get("date"):
        parts.append(_normalise_display_date(movie["date"]))
    return " - ".join(parts)


def _meta_norm_path(path: str) -> str:
    return path.replace("\\", "/").lower().strip()


def _useful_display_title(raw: str, *paths: str) -> bool:
    raw = str(raw or "").strip()
    if not raw or raw.lower().startswith(("http://", "https://")):
        return False

    # Host-decorated fallbacks like "i45dvX [gofile.io]" are not real file
    # names; skip them so the matcher does not chase folder ids as titles.
    without_host = re.sub(r"\s+\[[^\]]+\]\s*$", "", raw).strip().lower()
    path_titles = {_extract_raw_title(path).strip().lower() for path in paths if path}
    return without_host not in path_titles


def _get_name_override(player, path: str) -> str:
    if hasattr(player, '_get_name_override'):
        return player._get_name_override(path)
    overrides = getattr(player, "_metadata_name_overrides", {}) or {}
    return overrides.get(_meta_norm_path(path)) or ""

_PHONE_OR_FTP_PREFIXES = ("phone://", "ftp://", "ftps://", "sftp://")


def _is_phone_or_ftp_path(path) -> bool:
    """A row that lives on the phone over FTP rather than on a web host."""
    return str(path or "").strip().lower().startswith(_PHONE_OR_FTP_PREFIXES)


def _best_match_raw_title(player, file_path: str, *, fetch_remote_page: bool = False) -> str:
    fp = str(file_path or "")
    inner_fp = _unwrap_path(fp)
    is_remote = inner_fp.startswith(("http://", "https://"))
    # A phone or FTP row is remote in every way that matters for naming: it can
    # carry a rename and a display name, and its path basename is the phone's
    # own filename -- often a camera stamp -- rather than anything the user
    # chose. Reading the saved name only for http(s) rows meant a phone row the
    # user had already renamed was matched again from its camera filename.
    is_named = is_remote or _is_phone_or_ftp_path(inner_fp)

    if is_named:
        for key in (inner_fp, fp):
            raw = str(_get_name_override(player, key) or "").strip()
            if raw:
                return raw

        cache = getattr(player, "_stream_resolution_cache", {}) or {}
        for key in (inner_fp, fp):
            raw = str((cache.get(key, {}) or {}).get("title") or "").strip()
            if raw:
                return raw

        display_name = getattr(player, "_playlist_display_name", None)
        if callable(display_name):
            for key in (fp, inner_fp):
                try:
                    raw = str(display_name(key) or "").strip()
                except Exception:
                    raw = ""
                if _useful_display_title(raw, fp, inner_fp):
                    return raw

        # Only a web page can be fetched for its title; an FTP path has none.
        if fetch_remote_page and is_remote:
            raw = _extract_remote_page_title(inner_fp)
            if raw:
                return raw

    return _extract_raw_title(fp)


def _candidate_is_high_confidence(candidate) -> bool:
    """The linker's auto-apply gate, in one place.

    TitleMatcher and MetadataScraperDialog used to each spell their own
    version of this rule, so the two could disagree about what 'high
    confidence' means. Candidates built before strength existed fall back to
    the old two-signal test rather than being silently demoted.
    """
    if not isinstance(candidate, dict):
        return False
    strength = candidate.get("strength")
    if strength is None:
        return int(candidate.get("signal_count", 0) or 0) >= 2
    return float(strength) >= TitleMatcher.HIGH_CONFIDENCE_STRENGTH


def _db_signature(db: MetadataDB) -> tuple:
    return (
        getattr(db, "db_path", ""),
        db.count(),
        db.count_enriched(),
        db.last_full_scrape,
        db.last_update_scrape,
    )


def _metadata_db_for_site(player, site: dict) -> MetadataDB:
    app_dir = getattr(player, "data_dir", os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(app_dir, site["db_filename"])
    dbs = getattr(player, "_metadata_dbs", None)
    if dbs is None:
        dbs = {}
        player._metadata_dbs = dbs
    if db_path not in dbs:
        dbs[db_path] = MetadataDB(db_path)
    return dbs[db_path]


def _get_metadata_matcher(player, db: MetadataDB, force: bool = False) -> TitleMatcher:
    sig = _db_signature(db)
    matcher = getattr(player, "_metadata_matcher", None)
    if force or matcher is None or getattr(player, "_metadata_matcher_signature", None) != sig:
        matcher = TitleMatcher(db)
        player._metadata_matcher = matcher
        player._metadata_matcher_signature = sig
    return matcher


# ─────────────────────────────────────────────────────────────────────────────
# Integration
# ─────────────────────────────────────────────────────────────────────────────

def _flush_collapse_after_rename(player):
    """Fold rows together once a rename has made them the same video."""
    try:
        player._collapse_rename_pending = False
        collapse = getattr(player, "_collapse_duplicate_url_mirrors", None)
        if callable(collapse):
            collapse()
    except Exception as exc:
        print(f"[MetadataScraper] mirror collapse after rename failed: "
              f"{type(exc).__name__}: {exc}")


ALL_SITES_ID = "__all__"

_SITE_MATCHERS: dict = {}


def _matcher_for_db(db) -> "TitleMatcher":
    """A TitleMatcher per database, cached on its signature.

    _get_metadata_matcher keeps a single slot on the player, so walking every
    network through it rebuilds the index on each hop -- measured at 0.71 s for
    the 10564-record teamskeet DB and 0.45 s for nubiles, which per playlist
    row on the UI thread is a visible freeze.
    """
    sig = _db_signature(db)
    matcher = _SITE_MATCHERS.get(sig)
    if matcher is None:
        matcher = TitleMatcher(db)
        _SITE_MATCHERS[sig] = matcher
    return matcher


def signal_breakdown(signals) -> str:
    """Exactly what a match was decided on, with each criterion's weight.

    Mirrors _signal_strength, cap included, so the number shown is the number
    that ranked the candidate rather than a paraphrase of it.
    """
    tm = TitleMatcher
    sigs = [str(s) for s in (signals or ())]
    if not sigs:
        return ""
    if sigs == ["id"]:
        return (f"a lone video id = {tm._LONE_ID_STRENGTH:.2f} (held below "
                f"{tm.HIGH_CONFIDENCE_STRENGTH:.2f} on purpose)")
    if sigs == ["scene"]:
        return (f"the scene title alone = {tm._LONE_SCENE_STRENGTH:.2f} (held "
                f"below {tm.HIGH_CONFIDENCE_STRENGTH:.2f}: titles repeat across "
                f"networks)")
    parts, total, model_total, model_n = [], 0.0, 0.0, 0
    for sig in sigs:
        if sig.startswith("model:"):
            model_total += tm._SIGNAL_WEIGHTS["model"]
            model_n += 1
            parts.append(f"{sig.split(':', 1)[1]} "
                         f"{tm._SIGNAL_WEIGHTS['model']:.2f}")
            continue
        weight = float(tm._SIGNAL_WEIGHTS.get(sig, 0.0))
        total += weight
        parts.append(f"{sig} {weight:.2f}")
    cast = min(model_total, tm._MODEL_SIGNAL_CAP)
    if model_n:
        note = (f", capped at {tm._MODEL_SIGNAL_CAP:.2f}"
                if model_total > tm._MODEL_SIGNAL_CAP else "")
        parts.append(f"cast {cast:.2f}{note}")
    return " + ".join(parts) + f" = {total + cast:.2f}"


def match_candidates_all_sites(player, raw: str, limit: int = 5,
                               include_weak: bool = True, duration_ms: int = 0,
                               site_ids=None) -> list:
    """Candidates from every network, ranked against each other.

    Picking a network before searching means the right answer is simply
    missing whenever the pick was wrong, and nothing in the window says so --
    the user sees a weak match and concludes there is no good one. Searching
    all of them and ranking on the same signals puts the decision on the
    evidence. Each candidate carries site_id/site_name so the card can name
    the network and applying it can record the right one.
    """
    out = []
    for site_id, site in METADATA_SITES.items():
        if site_ids and site_id not in site_ids:
            continue
        try:
            db = _metadata_db_for_site(player, site)
            if not db.count():
                continue
            matcher = _matcher_for_db(db)
            if matcher is None:
                continue
            for cand in matcher.match_candidates(
                    raw, limit=limit, include_weak=include_weak,
                    duration_ms=duration_ms):
                cand = dict(cand)
                cand["site_id"] = site_id
                cand["site_name"] = site.get("name") or site_id
                out.append(cand)
        except Exception as exc:
            print(f"[MetadataScraper] {site_id} search failed: "
                  f"{type(exc).__name__}: {exc}")
    out.sort(key=lambda c: (-float(c.get("strength") or 0.0),
                            -float(c.get("score") or 0.0),
                            str((c.get("movie") or {}).get("slug") or "")))
    return out[:max(1, int(limit or 1))]


def init_metadata_scraper(player):
    app_dir   = getattr(player, "data_dir", os.path.dirname(os.path.abspath(__file__)))
    site      = METADATA_SITES[DEFAULT_SITE_ID]
    over_path = os.path.join(app_dir, site["overrides_filename"])

    player._metadata_dbs            = {}
    player._metadata_db             = _metadata_db_for_site(player, site)
    player._metadata_overrides_path = over_path
    player._metadata_links_path     = os.path.join(app_dir, LINKS_FILENAME)
    player._metadata_name_overrides = {}
    player._metadata_links          = {}
    player._meta_norm_path          = _meta_norm_path
    player._metadata_sites          = METADATA_SITES
    player._metadata_active_site    = DEFAULT_SITE_ID
    player._metadata_updates_running = set()
    load_hidden_performers(app_dir)

    if os.path.exists(over_path):
        try:
            with open(over_path, "r", encoding="utf-8") as f:
                player._metadata_name_overrides = json.load(f)
        except Exception:
            pass

    if os.path.exists(player._metadata_links_path):
        try:
            with open(player._metadata_links_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    player._metadata_links = loaded
        except Exception:
            pass

    player._metadata_update_all_now = lambda: _start_all_background_updates(
        player, force=True, manual=True
    )
    player._metadata_check_auto_update = lambda: _start_all_background_updates(
        player, force=False, manual=False
    )

    # Silent weekly background update after a DB exists. The timer keeps long
    # app sessions fresh too, instead of only checking once at startup.
    QTimer.singleShot(5000, player._metadata_check_auto_update)
    timer = QTimer(player)
    timer.setInterval(60 * 60 * 1000)
    timer.timeout.connect(player._metadata_check_auto_update)
    timer.start()
    player._metadata_auto_update_timer = timer


def _save_links(player):
    try:
        with open(player._metadata_links_path, "w", encoding="utf-8") as f:
            json.dump(getattr(player, "_metadata_links", {}) or {}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[MetadataScraper] save links error: {e}")


def _db_path_for_site(player, site: dict) -> str:
    return os.path.join(getattr(player, "data_dir", "")
                        or os.path.dirname(os.path.abspath(__file__)),
                        (site or {}).get("db_filename") or "")


def preview_info_for_path(player, path: str) -> dict:
    """Everything needed to preview a playlist row.

    Never blocks: hover has to be instant, so nothing here waits on a socket.
    What it does start, in the background, is the work a cover needs -- the
    signed URL minted during a scrape is dead within about an hour, and the
    row's own watch page can mint a new one. Returns {} when the row is not
    linked to any movie.

    Rows linked before metadata_links.json existed are recovered by matching
    the saved display name back through the matcher, so previously renamed
    rows preview too.
    """
    info: dict = {}
    if player is None:
        return info
    norm = _meta_norm_path(path or "")
    links = getattr(player, "_metadata_links", {}) or {}
    link = links.get(norm) or links.get(_meta_norm_path(_unwrap_path(path or "")))

    site = None
    movie = None
    if isinstance(link, dict) and link.get("slug"):
        site = METADATA_SITES.get(link.get("site") or DEFAULT_SITE_ID)
        if site:
            db = _metadata_db_for_site(player, site)
            movie = (getattr(db, "movies", {}) or {}).get(link["slug"])

    if movie is None:
        # Fall back to re-matching the stored display name.
        name = str(_get_name_override(player, path) or "").strip()
        if not name:
            return info
        cache = getattr(player, "_metadata_preview_matchers", None)
        if cache is None:
            cache = {}
            player._metadata_preview_matchers = cache
        for site_id, cand_site in METADATA_SITES.items():
            try:
                db = _metadata_db_for_site(player, cand_site)
                if not db.count():
                    continue
                matcher = cache.get(site_id)
                if matcher is None or matcher_db_sig(matcher) != _db_signature(db):
                    matcher = TitleMatcher(db)
                    matcher._sig = _db_signature(db)
                    cache[site_id] = matcher
                hit = matcher.match(name)
            except Exception:
                continue
            if hit:
                site, movie = cand_site, hit
                break
    if movie is None or site is None:
        return info

    now = time.time()
    slug = str(movie.get("slug") or "")
    image = str(movie.get("image") or "")
    preview = str(movie.get("preview") or "")
    trailer = str(movie.get("trailer_url") or "")
    img_exp = int(movie.get("image_expires") or 0)
    prv_exp = int(movie.get("preview_expires") or 0)
    # Covers are held in memory, never on disk, and previews are streamed
    # straight from the CDN. A signature that has died is re-minted from the
    # movie's own watch page in the background, and the card repaints itself
    # when the bytes land -- so a row recovers on hover with nothing stored.
    cover_data = cover_bytes(slug)
    if not cover_data:
        if image and (img_exp == 0 or img_exp > now):
            ensure_cover_async(player, site, slug, image)
        elif movie.get("url"):
            refresh_signed_assets_async(player, site, movie,
                                        _metadata_db_for_site(player, site))
    # A stored preview is a real asset; TeamSkeet keeps one per scene. The
    # loop URL derived from title+series is nubiles-shaped and only locates
    # the asset (unsigned it 403s), so it must never be invented for a
    # TeamSkeet movie -- that hands the caller a nubiles URL that cannot
    # exist. Derived URLs are reported as not live.
    preview_url = preview or trailer
    if not preview_url and str(site.get("id") or "") != "teamskeet":
        preview_url = preview_loop_url(movie.get("title") or "",
                                       movie.get("series") or "")
    info.update({
        "slug":          slug,
        "site":          site.get("id") or "",
        "site_name":     site.get("name") or "",
        "name":          format_display_name(movie),
        "cover_data":    cover_data,
        "image":         image,
        # exp == 0 means the URL carries no signature at all. TeamSkeet's
        # covers are like that and stay valid forever, so absence of an
        # expiry must read as live -- requiring exp > now made every
        # TeamSkeet row report a dead cover.
        "image_live":    bool(image) and (img_exp == 0 or img_exp > now),
        "preview":       preview,
        # A stored TeamSkeet trailer is unsigned, so prv_exp is 0 and it
        # reads as live -- it is a permanent asset, not a signed one.
        "preview_live":  bool(preview or trailer) and (prv_exp == 0 or prv_exp > now),
        "preview_url":   preview_url,
        "series":        movie.get("series") or "",
        "models":        list(movie.get("models") or []),
        "date":          movie.get("date") or "",
        # True when the row's watch page is known, so a dead cover has
        # somewhere to be re-minted from and the card is worth polling.
        "can_refresh":   bool(movie.get("url")),
    })
    return info


def matcher_db_sig(matcher) -> tuple:
    return getattr(matcher, "_sig", ())


def _save_overrides(player):
    try:
        with open(player._metadata_overrides_path, "w", encoding="utf-8") as f:
            json.dump(player._metadata_name_overrides, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[MetadataScraper] save overrides error: {e}")


def _start_all_background_updates(player, force: bool = False, manual: bool = False) -> int:
    started = 0
    for site in METADATA_SITES.values():
        db = _metadata_db_for_site(player, site)
        should_run = force or (db.count() > 0 and db.needs_update_scrape())
        if not should_run:
            continue
        mode = "full" if db.count() == 0 or db.needs_full_scrape() else "update"
        if _start_background_update(player, site, db, mode=mode, manual=manual):
            started += 1
    return started


def _start_background_update(player, site: Optional[dict] = None, db: Optional[MetadataDB] = None,
                             mode: str = "update", manual: bool = False):
    site = site or METADATA_SITES[DEFAULT_SITE_ID]
    db = db or player._metadata_db
    site_id = site.get("id") or site.get("name") or "metadata"
    running = getattr(player, "_metadata_updates_running", None)
    if running is None:
        running = set()
        player._metadata_updates_running = running
    if site_id in running:
        return False
    running.add(site_id)
    ytdlp = _find_ytdlp()
    if site.get("scraper") == "network_gallery":
        scraper = NetworkGalleryScraper(db, site, mode=mode)
    else:
        scraper = TeamSkeetScraper(db, mode=mode, ytdlp_path=ytdlp,
                                   sources=site.get("sources") or REPTYLE_SOURCES)

    def _done(n, name=site.get("name", "metadata"), sid=site_id):
        try:
            getattr(player, "_metadata_updates_running", set()).discard(sid)
        except Exception:
            pass
        print(f"[MetadataScraper] {name} bg {mode} done: {n} enriched")
        try:
            if getattr(player, "_metadata_active_site", DEFAULT_SITE_ID) == sid:
                _get_metadata_matcher(player, db, force=True)
        except Exception:
            pass
        if manual and hasattr(player, "show_osd"):
            player.show_osd(f"{name} metadata updated", duration=2200)

    def _err(msg, name=site.get("name", "metadata"), sid=site_id):
        try:
            getattr(player, "_metadata_updates_running", set()).discard(sid)
        except Exception:
            pass
        print(f"[MetadataScraper] {name} bg {mode} error: {msg}")
        if manual and hasattr(player, "show_osd"):
            player.show_osd(f"{name} metadata update failed", duration=2600)

    scraper.signals.finished.connect(_done)
    scraper.signals.error.connect(_err)
    scraper.start()
    if not hasattr(player, "_bg_scrapers"):
        player._bg_scrapers = []
    player._bg_scrapers.append(scraper)
    return True


def _metadata_context_menu_hook(menu, player, selected_rows):
    if not selected_rows:
        return
    menu.addSeparator()
    act = menu.addAction("🔍  Find Metadata")
    act.triggered.connect(
        lambda checked=False, rows=list(selected_rows):
            _open_metadata_dialog(player, rows)
    )


def _open_metadata_dialog(player, rows):
    paths = [player.playlist[r] for r in rows
             if 0 <= r < len(getattr(player, "playlist", []))]
    if not paths:
        return
    dlg = MetadataScraperDialog(player, paths)
    dlg.exec()


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────

_DARK   = "#1a1a2e"
_MID    = "#16213e"
_PANEL  = "#0f3460"
_ACCENT = "#e94560"
_GOLD   = "#f5a623"
_TEXT   = "#e0e0e0"
_DIM    = "#888"
_GREEN  = "#4caf50"

_STYLE = f"""
QDialog, QWidget {{ background:{_DARK}; color:{_TEXT};
    font-family:'Segoe UI',Consolas,sans-serif; }}
QLabel {{ color:{_TEXT}; }}
QLineEdit {{ background:{_MID}; color:{_TEXT}; border:1px solid {_PANEL};
    border-radius:4px; padding:6px 10px; font-size:13px; }}
QLineEdit:focus {{ border-color:{_ACCENT}; }}
QPushButton {{ background:{_PANEL}; color:{_TEXT}; border:none;
    border-radius:4px; padding:7px 18px; font-size:12px; font-weight:bold; }}
QPushButton:hover {{ background:{_ACCENT}; }}
QPushButton:disabled {{ background:#333; color:{_DIM}; }}
QProgressBar {{ background:{_MID}; border:1px solid {_PANEL};
    border-radius:3px; height:10px; text-align:center; color:{_TEXT}; }}
QProgressBar::chunk {{ background:{_ACCENT}; border-radius:3px; }}
QTextEdit {{ background:{_MID}; color:{_TEXT}; border:1px solid {_PANEL};
    border-radius:4px; font-family:Consolas,monospace; font-size:11px; }}
QScrollBar:vertical {{ background:{_MID}; width:8px; }}
QScrollBar::handle:vertical {{ background:{_PANEL}; border-radius:4px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}

"""


class _StretchedContainer(QWidget):
    """Container that always clamps its width to the viewport, preventing
    horizontal scrolling in the QScrollArea."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        for i in range(self.layout().count()):
            item = self.layout().itemAt(i)
            if item and item.widget():
                item.widget().setMaximumWidth(w)


class _MovieResultCard(QFrame):
    apply_clicked = pyqtSignal(str, dict, str)  # file_path, movie, site_id

    def __init__(self, file_path: str, movie: Optional[dict], parent=None,
                 display_name: str = "", candidates: Optional[list[dict]] = None,
                 auto_apply: bool = True):
        super().__init__(parent)
        self.file_path    = file_path
        self.movie        = movie
        self.candidates   = candidates or []
        # Which network the shown candidate came from. With every network
        # searched at once this is no longer the dialog's current selection.
        self.site_id      = str((self.candidates[0].get("site_id")
                                 if self.candidates else "") or "")
        self.auto_apply   = bool(auto_apply and movie)
        if self.movie is None and self.candidates:
            self.movie = self.candidates[0].get("movie")
        # display_name: human-readable label (stream title or cleaned filename/
        # URL segment — never the raw URL string).
        self._display_name = unquote(display_name or _extract_raw_title(file_path))
        self._player = None
        self._build()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_name_edit_height()
        self.updateGeometry()

    def _build(self):
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(f"""
            QFrame {{ background:{_MID}; border:1px solid {_PANEL};
                      border-radius:6px; margin:2px 0; }}
        """)
        from PyQt6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(5)

        # ── FILE label + filename (no HBoxLayout/stretch — that drove width) ──
        fl = QLabel(f"<span style='color:{_DIM};font-size:10px;'>FILE</span>")
        fl.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(fl)

        fn = QLabel(self._display_name)
        fn.setWordWrap(True)
        fn.setStyleSheet(f"color:{_TEXT}; font-size:12px;")
        fn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(fn)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{_PANEL}; max-height:1px;")
        lay.addWidget(sep)

        if self.movie:
            confidence = "high" if self.auto_apply else "possible"
            conf_color = _GREEN if confidence == "high" else _GOLD
            label = "HIGH MATCH" if confidence == "high" else "POSSIBLE MATCH"
            ml = QLabel(f"<b style='color:{conf_color};font-size:11px;'>{label}</b>")
            ml.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(ml)

            # ── Candidates combo ──────────────────────────────────────────────
            # AdjustToMinimumContentsLengthWithIcon stops Qt measuring item
            # text to derive a minimum width, which was the main width driver.
            if self.candidates:
                combo = QComboBox()
                combo.setStyleSheet(
                    f"background:{_DARK}; color:{_TEXT}; border:1px solid {_PANEL};"
                    f" border-radius:4px; padding:5px 8px; font-size:12px;"
                )
                combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                combo.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
                )
                combo.setMinimumContentsLength(0)
                for cand in self.candidates:
                    movie = cand.get("movie") or {}
                    sig = cand.get("signal_count") or 0
                    if cand.get("related"):
                        prefix = "RELATED"
                    elif sig >= 2:
                        prefix = "HIGH"
                    else:
                        prefix = "MAYBE"
                    net = cand.get("site_name") or ""
                    combo.addItem(
                        f"{prefix}  {format_display_name(movie)}"
                        + (f"   [{net}]" if net else ""), cand)
                combo.currentIndexChanged.connect(lambda *_: self._select_candidate(combo.currentData()))
                lay.addWidget(combo)

                self._why_label = QLabel("")
                self._why_label.setTextFormat(Qt.TextFormat.RichText)
                self._why_label.setWordWrap(True)
                lay.addWidget(self._why_label)
                self._update_why(self.candidates[0] if self.candidates else None)

                related_count = sum(1 for c in self.candidates if c.get("related"))
                if related_count:
                    rl = QLabel(
                        f"<span style='color:{_DIM};font-size:10px;'>"
                        f"+ {related_count} more from same series &amp; actor</span>"
                    )
                    rl.setTextFormat(Qt.TextFormat.RichText)
                    rl.setWordWrap(True)
                    lay.addWidget(rl)

            # ── Series / Models / Scene / Date rows ───────────────────────────
            _female_models = [m for m in self.movie.get("models", []) if m and not _is_male_performer(m)]
            for k, v in [
                ("Series",  self.movie.get("series", "")),
                ("Models",  " & ".join(_female_models)),
                ("Scene",   self.movie.get("title", "")),
                ("Date",    _normalise_display_date(self.movie.get("date", ""))),
            ]:
                if not v:
                    continue
                row = QHBoxLayout()
                kl  = QLabel(f"<span style='color:{_GOLD};font-size:11px;'>{k}</span>")
                kl.setTextFormat(Qt.TextFormat.RichText)
                kl.setFixedWidth(55)
                vl  = QLabel(v)
                vl.setWordWrap(True)
                vl.setStyleSheet(f"color:{_TEXT}; font-size:12px;")
                vl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                row.addWidget(kl)
                row.addWidget(vl, 1)
                if k == "Models":
                    self._models_value = vl
                    hb = QPushButton("hide…")
                    hb.setFixedWidth(52)
                    hb.setToolTip("Leave a performer out of every display name.\n"
                                  "Saved to male_performers.json.")
                    hb.clicked.connect(lambda *_: self._hide_performer_menu(hb))
                    row.addWidget(hb)
                lay.addLayout(row)

            sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
            sep2.setStyleSheet(f"background:{_PANEL}; max-height:1px;")
            lay.addWidget(sep2)

            # ── NEW DISPLAY NAME — QTextEdit wraps long names instead of
            #    scrolling horizontally like QLineEdit would ─────────────────
            nl = QLabel(f"<span style='color:{_DIM};font-size:10px;'>NEW DISPLAY NAME</span>")
            nl.setTextFormat(Qt.TextFormat.RichText)
            nl.setWordWrap(True)
            lay.addWidget(nl)

            self.name_edit = QTextEdit(format_display_name(self.movie))
            self.name_edit.setStyleSheet(
                f"background:{_DARK}; color:{_ACCENT}; font-weight:bold;"
                f" font-size:13px; border:1px solid {_ACCENT};"
                f" border-radius:4px; padding:5px 8px;"
            )
            self.name_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            self.name_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.name_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.name_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            self.name_edit.setMinimumWidth(0)
            self.name_edit.document().contentsChanged.connect(self._adjust_name_edit_height)
            QTimer.singleShot(0, self._adjust_name_edit_height)
            lay.addWidget(self.name_edit)

            br = QHBoxLayout()
            btn = QPushButton("✓  Apply to Playlist")
            btn.setStyleSheet(
                f"background:{_ACCENT}; color:white; font-weight:bold;"
                f" padding:7px 20px; border-radius:4px;"
            )
            btn.clicked.connect(self._apply)
            br.addStretch(); br.addWidget(btn)
            lay.addLayout(br)
        else:
            nl = QLabel(
                f"<span style='color:{_ACCENT};'>No match found</span>"
                f"  <span style='color:{_DIM};font-size:11px;'>"
                f"(DB has {{}}: try scraping first or refine filename)</span>"
            )
            nl.setTextFormat(Qt.TextFormat.RichText)
            nl.setWordWrap(True)
            nl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lay.addWidget(nl)

    def _adjust_name_edit_height(self):
        """Shrink-wrap the QTextEdit height to its wrapped content."""
        if not hasattr(self, "name_edit"):
            return
        
        w = self.name_edit.viewport().width()
        if w > 0:
            self.name_edit.document().setTextWidth(w)
            
        doc_h = int(self.name_edit.document().size().height())
        m     = self.name_edit.contentsMargins()
        self.name_edit.setFixedHeight(max(doc_h + m.top() + m.bottom() + 12, 36))

    def _apply(self):
        m = dict(self.movie)
        m["_override_name"] = self.name_edit.toPlainText().strip()
        self.apply_clicked.emit(self.file_path, m, self.site_id)

    def _select_candidate(self, candidate):
        if not isinstance(candidate, dict):
            return
        movie = candidate.get("movie") if "movie" in candidate else candidate
        if not isinstance(movie, dict) or not movie:
            return
        self.auto_apply = int(candidate.get("signal_count", 0)) >= 2
        self.movie = movie
        self.site_id = str(candidate.get("site_id") or self.site_id or "")
        if hasattr(self, "name_edit"):
            self.name_edit.setPlainText(format_display_name(movie))
        self._update_why(candidate)

    def _update_why(self, candidate=None):
        """Show the criteria that decided this match, with their weights."""
        label = getattr(self, "_why_label", None)
        if label is None:
            return
        cand = candidate if isinstance(candidate, dict) else (
            self.candidates[0] if self.candidates else {})
        why = signal_breakdown((cand or {}).get("signals"))
        net = (cand or {}).get("site_name") or ""
        text = "  |  ".join(x for x in (
            f"network: {net}" if net else "", why) if x)
        label.setText(f"<span style='color:{_DIM};font-size:10px;'>{text}</span>")
        label.setVisible(bool(text))

    def _hide_performer_menu(self, button):
        """Let the user drop a performer from every display name, for good."""
        names = [m for m in (self.movie or {}).get("models", []) if m]
        if not names:
            return
        menu = QMenu(self)
        for name in names:
            act = menu.addAction(str(name))
            act.setData(str(name))
        picked = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        if picked is None:
            return
        app_dir = getattr(getattr(self, "_player", None), "data_dir", "") or ""
        if hide_performer(app_dir, picked.data()):
            self.name_edit.setPlainText(format_display_name(self.movie))
            kept = [m for m in (self.movie or {}).get("models", [])
                    if m and not _is_male_performer(m)]
            if getattr(self, "_models_value", None) is not None:
                self._models_value.setText(" & ".join(kept))


class MetadataScraperDialog(QDialog):

    def __init__(self, player, file_paths: list[str], parent=None):
        super().__init__(parent or player)
        self.player     = player
        self.file_paths = file_paths
        self.db         = player._metadata_db
        self.site_id    = DEFAULT_SITE_ID
        self.site       = METADATA_SITES[self.site_id]
        self.matcher    = _get_metadata_matcher(player, self.db)
        self._scraper:  Optional[QThread]          = None
        self._cards:    list[_MovieResultCard]     = []

        self.setWindowTitle("Metadata Linker")
        self.setMinimumSize(920, 640)
        self.setStyleSheet(_STYLE)
        self._build_ui()
        self._refresh_db_status()

        if self.db.count_enriched() > 0:
            QTimer.singleShot(150, self._run_match)

    # ── build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(f"background:{_PANEL};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(18, 0, 18, 0)
        tl = QLabel("🔍  Metadata Linker")
        tl.setStyleSheet(f"color:{_TEXT}; font-size:16px; font-weight:bold;")
        hl.addWidget(tl); hl.addStretch()
        self._db_badge = QLabel()
        self._db_badge.setStyleSheet(f"color:{_GOLD}; font-size:12px;")
        hl.addWidget(self._db_badge)
        root.addWidget(hdr)

        # Body splitter
        spl = QSplitter(Qt.Orientation.Horizontal)
        spl.setChildrenCollapsible(False)
        spl.setStyleSheet("QSplitter::handle{background:#0f3460;width:2px;}")

        # ── LEFT ──────────────────────────────────────────────────────────────
        left = QWidget(); left.setMinimumWidth(330)
        ll = QVBoxLayout(left); ll.setContentsMargins(14, 14, 14, 14); ll.setSpacing(10)

        # Site selector card
        sf = QFrame()
        sf.setStyleSheet(f"QFrame{{background:{_MID};border:1px solid {_PANEL};border-radius:6px;}}")
        sl = QVBoxLayout(sf); sl.setContentsMargins(12, 10, 12, 10); sl.setSpacing(6)
        site_title = QLabel("SITES")
        site_title.setStyleSheet(f"color:{_GOLD};font-size:11px;font-weight:bold;border:none;")
        sl.addWidget(site_title)
        self._site_combo = QComboBox()
        self._site_combo.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid {_PANEL};"
            f" border-radius:4px; padding:5px 8px; font-size:12px;"
        )
        self._site_combo.addItem("All networks", ALL_SITES_ID)
        for site_id, site in METADATA_SITES.items():
            self._site_combo.addItem(site["name"], site_id)
        self._site_combo.setCurrentIndex(0)
        self._site_combo.currentIndexChanged.connect(self._on_site_changed)
        sl.addWidget(self._site_combo)
        ll.addWidget(sf)

        # DB status card
        df = QFrame()
        df.setStyleSheet(f"QFrame{{background:{_MID};border:1px solid {_PANEL};border-radius:6px;}}")
        dl = QVBoxLayout(df); dl.setContentsMargins(12, 10, 12, 10); dl.setSpacing(6)
        db_title = QLabel("DATABASE")
        db_title.setStyleSheet(f"color:{_GOLD};font-size:11px;font-weight:bold;border:none;")
        dl.addWidget(db_title)
        self._db_status_lbl = QLabel()
        self._db_status_lbl.setWordWrap(True)
        self._db_status_lbl.setStyleSheet(f"color:{_TEXT};font-size:12px;border:none;")
        dl.addWidget(self._db_status_lbl)

        # yt-dlp status
        self._ytdlp_lbl = QLabel()
        self._ytdlp_lbl.setWordWrap(True)
        self._ytdlp_lbl.setStyleSheet(f"color:{_DIM};font-size:11px;border:none;")
        dl.addWidget(self._ytdlp_lbl)

        br = QHBoxLayout()
        self._btn_full   = QPushButton("Full Scrape")
        self._btn_update = QPushButton("Update")
        self._btn_enrich = QPushButton("Enrich Only")
        self._btn_full.setToolTip("Harvest all movie listing pages for the selected site")
        self._btn_update.setToolTip("Manually fetch new movies since the last weekly update")
        self._btn_enrich.setToolTip("Fetch movie pages for entries missing metadata")
        self._btn_full.clicked.connect(lambda: self._start_scrape("full"))
        self._btn_update.clicked.connect(lambda: self._start_scrape("update"))
        self._btn_enrich.clicked.connect(lambda: self._start_scrape("enrich"))
        br.addWidget(self._btn_full)
        br.addWidget(self._btn_update)
        br.addWidget(self._btn_enrich)
        dl.addLayout(br)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        dl.addWidget(self._progress)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setVisible(False)
        self._btn_cancel.clicked.connect(self._cancel_scrape)
        dl.addWidget(self._btn_cancel)
        ll.addWidget(df)

        # URL linker card
        uf = QFrame()
        uf.setStyleSheet(f"QFrame{{background:{_MID};border:1px solid {_PANEL};border-radius:6px;}}")
        ul = QVBoxLayout(uf); ul.setContentsMargins(12, 10, 12, 10); ul.setSpacing(6)
        url_title = QLabel("LINK VIDEO TO URL")
        url_title.setStyleSheet(f"color:{_GOLD};font-size:11px;font-weight:bold;border:none;")
        ul.addWidget(url_title)
        url_hint = QLabel(
            "Paste the source URL (e.g. filester link).\n"
            "Title is extracted and matched against the DB."
        )
        url_hint.setWordWrap(True)
        url_hint.setStyleSheet(f"color:{_DIM};font-size:11px;border:none;")
        ul.addWidget(url_hint)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://filester.me/d/xxxxxx")
        ul.addWidget(self._url_input)
        btn_link = QPushButton("🔗  Match from URL")
        btn_link.clicked.connect(self._match_from_url)
        ul.addWidget(btn_link)
        ll.addWidget(uf)

        # Log
        log_lbl = QLabel("LOG")
        log_lbl.setStyleSheet(f"color:{_GOLD};font-size:11px;font-weight:bold;")
        ll.addWidget(log_lbl)
        self._log = QTextEdit(); self._log.setReadOnly(True)
        self._log.setMaximumHeight(170)
        ll.addWidget(self._log)
        ll.addStretch()
        spl.addWidget(left)

        # ── RIGHT ─────────────────────────────────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right); rl.setContentsMargins(14, 14, 14, 14); rl.setSpacing(8)

        rh = QHBoxLayout()
        rl_lbl = QLabel("MATCHES")
        rl_lbl.setStyleSheet(f"color:{_GOLD};font-size:11px;font-weight:bold;")
        rh.addWidget(rl_lbl); rh.addStretch()
        self._btn_apply_all = QPushButton("✓  Apply All Matches")
        self._btn_apply_all.setEnabled(False)
        self._btn_apply_all.clicked.connect(self._apply_all)
        rh.addWidget(self._btn_apply_all)
        rl.addLayout(rh)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._res_container = _StretchedContainer()
        self._res_container.setMinimumWidth(0)
        self._res_layout    = QVBoxLayout(self._res_container)
        self._res_layout.setContentsMargins(0, 0, 0, 0)
        self._res_layout.setSpacing(6)
        self._res_layout.addStretch()
        scroll.setWidget(self._res_container)
        rl.addWidget(scroll)
        spl.addWidget(right)

        spl.setSizes([350, 570])
        root.addWidget(spl, 1)

        # Footer
        ft = QWidget(); ft.setFixedHeight(44)
        ft.setStyleSheet(f"background:{_PANEL};")
        fl2 = QHBoxLayout(ft); fl2.setContentsMargins(16, 0, 16, 0)
        self._status_lbl = QLabel("Ready.")
        self._status_lbl.setStyleSheet(f"color:{_DIM};font-size:11px;")
        fl2.addWidget(self._status_lbl); fl2.addStretch()
        cb = QPushButton("Close"); cb.clicked.connect(self.accept)
        fl2.addWidget(cb)
        root.addWidget(ft)

        # Refresh yt-dlp indicator
        ytdlp = _find_ytdlp()
        if ytdlp:
            self._ytdlp_lbl.setText("yt-dlp fallback: found")
            self._ytdlp_lbl.setStyleSheet(f"color:{_GREEN};font-size:11px;border:none;")
        else:
            self._ytdlp_lbl.setText("yt-dlp fallback: not found - direct site scrape still works")
            self._ytdlp_lbl.setStyleSheet(f"color:{_ACCENT};font-size:11px;border:none;")

    # ── DB status ─────────────────────────────────────────────────────────────

    def _on_site_changed(self, *_):
        site_id = self._site_combo.currentData() or DEFAULT_SITE_ID
        self.site_id = site_id
        if site_id == ALL_SITES_ID:
            # The scrape buttons still need a concrete site to point at; only
            # the search spans all of them.
            self.site = METADATA_SITES[DEFAULT_SITE_ID]
            self.db = _metadata_db_for_site(self.player, self.site)
            self.player._metadata_active_site = site_id
            self.matcher = _get_metadata_matcher(self.player, self.db)
            self._clear_results()
            self._refresh_db_status()
            self._set_status("Searching every network.")
            QTimer.singleShot(50, self._run_match)
            return
        self.site = METADATA_SITES.get(site_id, METADATA_SITES[DEFAULT_SITE_ID])
        self.db = _metadata_db_for_site(self.player, self.site)
        self.player._metadata_active_site = site_id
        self.matcher = _get_metadata_matcher(self.player, self.db)
        self._clear_results()
        self._refresh_db_status()
        self._set_status(f"{self.site['name']} selected.")
        if self.db.count_enriched() > 0:
            QTimer.singleShot(50, self._run_match)

    def _refresh_db_status(self):
        total    = self.db.count()
        enriched = self.db.count_enriched()
        stubs    = total - enriched
        last_f   = (self.db.last_full_scrape or "Never")[:10]
        last_u   = (self.db.last_update_scrape or "—")[:10]
        self._db_status_lbl.setText(
            f"<b>{self.site['name']}</b><br>"
            f"<b>{total}</b> movies total  |  "
            f"<b style='color:{_GREEN}'>{enriched}</b> with full metadata  |  "
            f"<b style='color:{_GOLD}'>{stubs}</b> stubs only\n"
            f"Full scrape: {last_f}    Last update: {last_u}"
        )
        self._db_badge.setText(f"{self.site['name']}: {enriched}/{total} enriched")

    # ── scrape ────────────────────────────────────────────────────────────────

    def _start_scrape(self, mode: str):
        ytdlp = _find_ytdlp()
        if self.site.get("scraper") == "network_gallery":
            self._scraper = NetworkGalleryScraper(self.db, self.site, mode=mode)
        else:
            self._scraper = TeamSkeetScraper(
                self.db,
                mode=mode,
                ytdlp_path=ytdlp,
                sources=self.site.get("sources") or REPTYLE_SOURCES,
            )
        self._scraper.signals.progress.connect(self._log_msg)
        self._scraper.signals.tick.connect(self._on_tick)
        self._scraper.signals.finished.connect(self._on_done)
        self._scraper.signals.error.connect(self._on_err)
        self._scraper.start()
        for b in [self._btn_full, self._btn_update, self._btn_enrich]:
            b.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setRange(0, 100)
        self._progress.setVisible(True)
        self._btn_cancel.setVisible(True)
        self._set_status(f"Scraping ({mode})…")

    def _cancel_scrape(self):
        if self._scraper:
            self._scraper.cancel()

    def _on_tick(self, done: int, total: int):
        if total > 0:
            self._progress.setValue(int(done * 100 / total))
        self._refresh_db_status()

    def _on_done(self, n: int):
        self._progress.setVisible(False)
        self._btn_cancel.setVisible(False)
        for b in [self._btn_full, self._btn_update, self._btn_enrich]:
            b.setEnabled(True)
        self._log_msg(f"✓ Done. {n} movies enriched. DB: {self.db.count_enriched()}/{self.db.count()}")
        self._set_status(f"Complete — {n} movies enriched.")
        self._refresh_db_status()
        self.matcher = _get_metadata_matcher(self.player, self.db, force=True)
        self._run_match()

    def _on_err(self, msg: str):
        self._progress.setVisible(False)
        self._btn_cancel.setVisible(False)
        for b in [self._btn_full, self._btn_update, self._btn_enrich]:
            b.setEnabled(True)
        self._log_msg(f"✗ Error: {msg}")

    # ── matching ──────────────────────────────────────────────────────────────

    def _candidates_for(self, raw: str, fp=None) -> list:
        """Candidates for one row, from every network or just the chosen one."""
        dur = _player_duration_ms(self.player, fp) if fp else 0
        if (self._site_combo.currentData() or DEFAULT_SITE_ID) == ALL_SITES_ID:
            return match_candidates_all_sites(
                self.player, raw, limit=5, include_weak=True, duration_ms=dur)
        return self.matcher.match_candidates(
            raw, limit=5, include_weak=True, duration_ms=dur)

    def _run_match(self):
        self._clear_results()
        self._cards.clear()
        matched = 0
        possible = 0

        # Without a connection the phone cannot be probed, so those rows lose
        # the runtime criterion and match on the name alone. Say so, rather
        # than letting weak matches look like a matcher that does not work.
        _no_runtime = [fp for fp in self.file_paths
                       if _is_phone_or_ftp_path(fp)
                       and not _player_duration_ms(self.player, fp)]
        if _no_runtime:
            self._log_msg(
                f"\u2139 {len(_no_runtime)} phone/FTP row(s) have no known "
                f"runtime, so the duration criterion "
                f"({TitleMatcher._SIGNAL_WEIGHTS['duration']:.2f}) cannot help "
                f"them. Connect FTP to probe them, or match on the name.")

        for fp in self.file_paths:
            raw = _best_match_raw_title(self.player, fp)
            candidates = self._candidates_for(raw, fp)
            movie = None
            auto_apply = False
            if candidates:
                top = candidates[0]
                if _candidate_is_high_confidence(top):
                    movie = top.get("movie")
                    auto_apply = True
                else:
                    possible += 1
            card  = _MovieResultCard(fp, movie, self._res_container,
                                     display_name=raw,
                                     candidates=candidates,
                                     auto_apply=auto_apply)
            card._player = self.player
            card.apply_clicked.connect(self._on_apply_single)
            idx = self._res_layout.count() - 1
            self._res_layout.insertWidget(idx, card)
            self._cards.append(card)
            if auto_apply and movie:
                matched += 1
                signals = ", ".join(candidates[0].get("signals", []))
                suffix = f" ({signals})" if signals else ""
                self._log_msg(f"High match: {raw!r} -> {format_display_name(movie)!r}{suffix}")
            elif candidates:
                top_movie = candidates[0].get("movie") or {}
                signals = ", ".join(candidates[0].get("signals", []))
                suffix = f" ({signals})" if signals else ""
                self._log_msg(f"Possible match: {raw!r} -> {format_display_name(top_movie)!r}{suffix}")
            else:
                self._log_msg(f"No match: {raw!r}")

        self._btn_apply_all.setEnabled(matched > 0)
        extra = f", {possible} possible" if possible else ""
        self._set_status(f"{matched}/{len(self.file_paths)} high-confidence matched{extra}.")

    def _match_from_url(self):
        url = self._url_input.text().strip()
        if not url:
            return
        raw = _best_match_raw_title(self.player, url, fetch_remote_page=True)
        self._log_msg(f"URL title: {raw!r}")

        candidates = self._candidates_for(raw)
        movie = (candidates[0].get("movie")
                 if candidates and _candidate_is_high_confidence(candidates[0])
                 else None)
        if not movie:
            if candidates:
                top_movie = candidates[0].get("movie") or {}
                self._log_msg(f"Possible match only: {format_display_name(top_movie)}")
                self._set_status("Possible match found; choose it manually from the results.")
                self._run_match()
            else:
                self._log_msg("No match found for URL title.")
                self._set_status("No match.")
            return

        self._log_msg(f"High match -> {format_display_name(movie)}")
        for fp in self.file_paths:
            self._apply_to_file(fp, movie, custom_name=None)
        self._set_status(f"Applied to {len(self.file_paths)} file(s).")
        self._run_match()

    def _on_apply_single(self, file_path: str, movie: dict, site_id: str = ""):
        custom = movie.pop("_override_name", None)
        self._apply_to_file(file_path, movie, custom_name=custom,
                            site_id=site_id)
        self._log_msg(f"Applied: {custom or format_display_name(movie)}")
        self._run_match()

    def _apply_all(self):
        n = 0
        for card in self._cards:
            if card.movie and getattr(card, "auto_apply", False):
                name = card.name_edit.toPlainText().strip() if hasattr(card, "name_edit") else None
                self._apply_to_file(card.file_path, card.movie, custom_name=name,
                                    site_id=getattr(card, "site_id", ""))
                n += 1
        self._log_msg(f"Applied {n} matches.")
        self._set_status(f"{n} matches applied.")
        self._run_match()

    def _apply_to_file(self, file_path: str, movie: dict,
                       custom_name: Optional[str], site_id: str = ""):
        # With every network searched at once, a row's network is the one its
        # candidate came from -- not whichever site the dialog happens to show.
        site = METADATA_SITES.get(site_id) or self.site
        name = custom_name or format_display_name(movie)
        if not name:
            return
        strip_seen_prefix = getattr(self.player, "_strip_seen_display_prefix", None)
        if callable(strip_seen_prefix):
            name = strip_seen_prefix(name)
            if not name:
                return
        old_key = getattr(self.player, '_get_name_override_key', lambda k: self.player._meta_norm_path(k))(file_path)
        old_override = getattr(self.player, "_metadata_name_overrides", {}).get(old_key) if old_key else None
        
        target_key = old_key or self.player._meta_norm_path(file_path)
        self.player._metadata_name_overrides[target_key] = name
        _save_overrides(self.player)

        # Remember WHICH movie this row was linked to, so the hover preview
        # can find it without re-matching. Keyed the same way as the override.
        # Renaming can turn three separate rows into one video: three quality
        # variants of the same scene share a title once the linker has named
        # them. Re-run the mirror collapse so they fold now instead of at the
        # next playlist load. Deferred and coalesced, because Apply All renames
        # a row at a time and the collapse walks the whole playlist.
        try:
            if not getattr(self.player, "_collapse_rename_pending", False):
                self.player._collapse_rename_pending = True
                QTimer.singleShot(
                    0, lambda: _flush_collapse_after_rename(self.player))
        except Exception as exc:
            print(f"[MetadataScraper] could not schedule the mirror collapse: {exc}")

        try:
            if not hasattr(self.player, "_metadata_links"):
                self.player._metadata_links = {}
            self.player._metadata_links[target_key] = {
                "site": site.get("id") or DEFAULT_SITE_ID,
                "slug": movie.get("slug") or "",
                "name": name,
            }
            _save_links(self.player)
        except Exception as e:
            print(f"[MetadataScraper] link record error: {e}")

        # A signed cover dies in about an hour, so get it now -- while the
        # user is sitting in the linker and a few seconds costs nothing --
        # instead of at the click an hour later.
        try:
            refresh_signed_assets_async(
                self.player, site, movie,
                _metadata_db_for_site(self.player, site), force=True)
        except Exception as e:
            print(f"[MetadataScraper] link refresh error: {e}")

        # Record in rename undo stack & persist
        if not hasattr(self.player, "_rename_undo_stack"):
            self.player._rename_undo_stack = []
        import time
        self.player._rename_undo_stack.append({
            'type': 'metadata',
            'path': file_path,
            'old_override': old_override,
            'new_override': name,
            'timestamp': time.time()
        })
        if len(self.player._rename_undo_stack) > 100:
            self.player._rename_undo_stack = self.player._rename_undo_stack[-100:]
        save_hist = getattr(self.player, 'save_rename_history', None)
        if callable(save_hist):
            save_hist()
        rows = [i for i, fp in enumerate(getattr(self.player, "playlist", []))
                if self.player._meta_norm_path(fp) == target_key]
        pw = getattr(self.player, "playlist_widget", None)
        if pw:
            for row in rows:
                updater = getattr(self.player, "update_row_appearance", None)
                if callable(updater):
                    updater(row)
                else:
                    display_name = name
                    playlist_display_name = getattr(self.player, "_playlist_display_name", None)
                    if callable(playlist_display_name):
                        try:
                            display_name = playlist_display_name(file_path)
                        except Exception:
                            display_name = name
                    item = pw.item(row, 0)
                    if item:
                        item.setText(display_name)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _clear_results(self):
        while self._res_layout.count() > 1:
            item = self._res_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _log_msg(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(
            f"<span style='color:{_DIM}'>[{ts}]</span> {msg}"
        )
        self._log.ensureCursorVisible()

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
