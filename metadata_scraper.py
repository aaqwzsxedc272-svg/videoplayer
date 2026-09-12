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

import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape as html_unescape
from typing import Optional
from urllib.parse import urlparse, unquote, urljoin

from PyQt6.QtCore  import Qt, QThread, pyqtSignal, QTimer, QObject
from PyQt6.QtGui   import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
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
            except Exception:
                pass

    def save(self):
        with self._lock:
            try:
                with open(self.db_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[MetadataDB] save error: {e}")

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
                movies[slug] = {
                    "slug":         slug,
                    "title":        _slug_to_title(slug),
                    "series":       "",
                    "series_url":   "",
                    "models":       [],
                    "model_urls":   [],
                    "date":         "",
                    "url":          f"https://www.teamskeet.com/movies/{slug}",
                    "scraped_at":   _utc_now(),
                    "meta_fetched": False,
                }

    def upsert(self, movie: dict):
        slug = movie.get("slug", "")
        if not slug:
            return
        with self._lock:
            self._data.setdefault("movies", {})[slug] = movie

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
        if self._cookie_path and os.path.isfile(self._cookie_path):
            storage_state = self._cookie_path

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
            try:
                self._page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                # Page may still have loaded fine even if this particular
                # selector never shows (e.g. an empty last page).
                self._page.wait_for_load_state("networkidle", timeout=timeout)
            html = self._page.content()
            if self._cookie_path:
                try:
                    self._context.storage_state(path=self._cookie_path)
                except Exception:
                    pass
            return html
        except Exception as e:
            print(f"[Scraper] browser fetch error {url}: {e}")
            return None

    def close(self):
        try:
            if self._cookie_path and self._context:
                self._context.storage_state(path=self._cookie_path)
        except Exception:
            pass
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
        # Already DD/MM/YYYY or MM/DD/YYYY — treat first two fields as DD/MM
        return s, f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

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
    """Convert a stored MM/DD/YYYY date to DD/MM/YYYY for display.
    Dates that already have a first field > 12 are returned unchanged.
    """
    raw_d = str(raw_d or "").strip()
    import re as _re
    _m = _re.match(r'^(\d{2})/(\d{2})/(\d{4})$', raw_d)
    if not _m:
        return raw_d
    mm, dd, yyyy = _m.group(1), _m.group(2), _m.group(3)
    if int(mm) > 12:
        # First field is already a day (>12) — it's DD/MM/YYYY
        return raw_d
    # Either ambiguous (both <=12) or mm<=12, dd>12 => old MM/DD/YYYY, swap
    return f"{dd}/{mm}/{yyyy}"


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
    keys = (
        "title", "series", "series_url", "series_slug", "models", "model_urls",
        "date", "published_date_iso", "image", "trailer_url", "video_id",
        "description", "tags", "_tags",
    )
    return any(existing.get(k) != movie.get(k) for k in keys)


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
            "source_site":        site.get("id") or "",
            "source_name":        site.get("name") or "",
            "image":              "",
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
                        changed += 1
                    movie["sources_seen"] = [source.get("id") or ""] if source.get("id") else []
                    self.db.upsert(movie)

                total_hint = f"/{max_pages}" if max_pages else ""
                self.signals.progress.emit(
                    f"  {source_name} page {page}{total_hint}: {len(movies)} movies parsed"
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


def _date_keys(date: str) -> set[str]:
    keys: set[str] = set()
    parts = re.match(r'(\d{2})/(\d{2})/(\d{4})', str(date or ""))
    if parts:
        mm, dd, yyyy = parts.groups()
        keys.add(f"{yyyy}{mm}{dd}")
        keys.add(f"{mm}{dd}{yyyy}")
    return keys


class TitleMatcher:
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
                "site_norm": _compact(movie.get("source_site", "") or movie.get("source_name", "")),
            }
            self._records[slug] = record

            for token in search_tokens:
                self._token_index.setdefault(token, set()).add(slug)
            for key in _date_keys(record["date"]):
                self._date_index.setdefault(key, set()).add(slug)

    def match(self, raw: str) -> Optional[dict]:
        matches = self.match_candidates(raw, limit=1, include_weak=False)
        return matches[0]["movie"] if matches else None

    def match_candidates(self, raw: str, limit: int = 5, include_weak: bool = True) -> list[dict]:
        if not raw or not self._records:
            return []

        q_norm   = _normalise(raw)
        q_compact = _compact(raw)
        q_tokens = _tokens(raw)
        q_date   = re.findall(r'\d{8}|\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}', raw)
        q_date_d = set(_date_digits(d) for d in q_date if len(_date_digits(d)) == 8)
        candidate_slugs = self._candidate_slugs(q_tokens, q_date_d)
        if not candidate_slugs:
            candidate_slugs = set(self._records)

        matches = []

        for slug in candidate_slugs:
            record = self._records.get(slug)
            if not record:
                continue
            score = self._score(raw, q_norm, q_tokens, q_date_d, record)
            signals = self._match_signals(raw, q_norm, q_compact, q_tokens, q_date_d, record)
            signal_count = len(signals)
            if score < MATCH_THRESHOLD and signal_count == 0:
                continue
            if not include_weak and signal_count < 2:
                continue
            confidence = "high" if signal_count >= 2 else "possible"
            matches.append({
                "movie": record["movie"],
                "score": score,
                "signals": signals,
                "signal_count": signal_count,
                "confidence": confidence,
            })

        matches.sort(key=lambda item: (item["signal_count"], item["score"]), reverse=True)
        return matches[:max(1, int(limit or 1))]

    def _match_signals(self, raw: str, q_norm: str, q_compact: str, q_tokens: set,
                       q_date_d: set, record: dict) -> list[str]:
        signals = []
        if record["series_norm"] and (
            record["series_norm"] in q_norm or record["series_compact"] in q_compact
        ):
            signals.append("series")

        title_overlap = q_tokens & (record["title_tokens"] - record["model_tokens"])
        if record["title_compact"] and record["title_compact"] in q_compact:
            signals.append("scene")
        elif len(title_overlap) >= 2:
            signals.append("scene")

        for model_name, mn, mc in zip(record["models"], record["model_norms"], record["model_compacts"]):
            matched = bool(mn and mn in q_norm) or bool(mc and mc in q_compact)
            if not matched and mc:
                matched = any(_sim(mc, token) >= 0.88 for token in q_tokens if len(token) >= 6)
            if matched:
                signals.append(f"model:{model_name}")

        if record["date"] and q_date_d and (q_date_d & _date_keys(record["date"])):
            signals.append("date")

        if record.get("video_id") and record["video_id"] in q_norm.split():
            signals.append("id")
        elif record.get("video_id") and f"/{record['video_id']}/" in raw:
            signals.append("id")

        if record.get("site_norm") and record["site_norm"] in q_compact:
            signals.append("site")

        return signals

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

        ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
        cutoff = 1 if len(q_tokens) <= 3 else 2
        candidates = {slug for slug, count in ranked[:80] if count >= cutoff}
        if len(candidates) < 40:
            candidates.update(slug for slug, _ in ranked[:80])
        return candidates

    def _score(self, raw: str, q_norm: str, q_tokens: set,
               q_date_d: set, record: dict) -> float:
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

        score = (
            s_slug      * 0.30 +
            s_title     * 0.20 +
            s_title_tok * 0.15 +
            s_series    +
            s_model     +
            s_date
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
    'adam black', 'adam ocelot', 'derrick pierce', 'jedd harris', 'steve holmes',
    'mike adriano', 'rocco siffredi', 'trenton eclipse', 'toni ribas', 'bill bailey',
    'tommy gunn', 'chad white', 'sterling cooper', 'alex puller', 'models'
}


def _is_male_performer(name: str) -> bool:
    if not name:
        return False
    norm = str(name).strip().lower()
    if norm in MALE_PERFORMERS:
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

def _best_match_raw_title(player, file_path: str, *, fetch_remote_page: bool = False) -> str:
    fp = str(file_path or "")
    inner_fp = _unwrap_path(fp)
    is_remote = inner_fp.startswith(("http://", "https://"))

    if is_remote:
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

        if fetch_remote_page:
            raw = _extract_remote_page_title(inner_fp)
            if raw:
                return raw

    return _extract_raw_title(fp)


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

def init_metadata_scraper(player):
    app_dir   = getattr(player, "data_dir", os.path.dirname(os.path.abspath(__file__)))
    site      = METADATA_SITES[DEFAULT_SITE_ID]
    over_path = os.path.join(app_dir, site["overrides_filename"])

    player._metadata_dbs            = {}
    player._metadata_db             = _metadata_db_for_site(player, site)
    player._metadata_overrides_path = over_path
    player._metadata_name_overrides = {}
    player._meta_norm_path          = _meta_norm_path
    player._metadata_sites          = METADATA_SITES
    player._metadata_active_site    = DEFAULT_SITE_ID
    player._metadata_updates_running = set()

    if os.path.exists(over_path):
        try:
            with open(over_path, "r", encoding="utf-8") as f:
                player._metadata_name_overrides = json.load(f)
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
    apply_clicked = pyqtSignal(str, dict)  # file_path, movie

    def __init__(self, file_path: str, movie: Optional[dict], parent=None,
                 display_name: str = "", candidates: Optional[list[dict]] = None,
                 auto_apply: bool = True):
        super().__init__(parent)
        self.file_path    = file_path
        self.movie        = movie
        self.candidates   = candidates or []
        self.auto_apply   = bool(auto_apply and movie)
        if self.movie is None and self.candidates:
            self.movie = self.candidates[0].get("movie")
        # display_name: human-readable label (stream title or cleaned filename/
        # URL segment — never the raw URL string).
        self._display_name = unquote(display_name or _extract_raw_title(file_path))
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
                    combo.addItem(f"{prefix}  {format_display_name(movie)}", cand)
                combo.currentIndexChanged.connect(lambda *_: self._select_candidate(combo.currentData()))
                lay.addWidget(combo)

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
        self.apply_clicked.emit(self.file_path, m)

    def _select_candidate(self, candidate):
        if not isinstance(candidate, dict):
            return
        movie = candidate.get("movie") if "movie" in candidate else candidate
        if not isinstance(movie, dict) or not movie:
            return
        self.auto_apply = int(candidate.get("signal_count", 0)) >= 2
        self.movie = movie
        if hasattr(self, "name_edit"):
            self.name_edit.setPlainText(format_display_name(movie))


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

    def _run_match(self):
        self._clear_results()
        self._cards.clear()
        matched = 0
        possible = 0

        for fp in self.file_paths:
            raw = _best_match_raw_title(self.player, fp)
            candidates = self.matcher.match_candidates(raw, limit=5, include_weak=True)
            movie = None
            auto_apply = False
            if candidates:
                top = candidates[0]
                if int(top.get("signal_count", 0)) >= 2:
                    movie = top.get("movie")
                    auto_apply = True
                else:
                    possible += 1
            card  = _MovieResultCard(fp, movie, self._res_container,
                                     display_name=raw,
                                     candidates=candidates,
                                     auto_apply=auto_apply)
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

        candidates = self.matcher.match_candidates(raw, limit=5, include_weak=True)
        movie = candidates[0].get("movie") if candidates and int(candidates[0].get("signal_count", 0)) >= 2 else None
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

    def _on_apply_single(self, file_path: str, movie: dict):
        custom = movie.pop("_override_name", None)
        self._apply_to_file(file_path, movie, custom_name=custom)
        self._log_msg(f"Applied: {custom or format_display_name(movie)}")
        self._run_match()

    def _apply_all(self):
        n = 0
        for card in self._cards:
            if card.movie and getattr(card, "auto_apply", False):
                name = card.name_edit.toPlainText().strip() if hasattr(card, "name_edit") else None
                self._apply_to_file(card.file_path, card.movie, custom_name=name)
                n += 1
        self._log_msg(f"Applied {n} matches.")
        self._set_status(f"{n} matches applied.")
        self._run_match()

    def _apply_to_file(self, file_path: str, movie: dict, custom_name: Optional[str]):
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
