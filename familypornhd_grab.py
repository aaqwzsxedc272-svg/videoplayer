#!/usr/bin/env python3
"""Extract media links from familypornhd.com video pages.

The site currently puts the playable URL in the page source.  This module is
kept deliberately static: it fetches the page once and extracts the URL from
HTML/inline JSON instead of trying to automate the player.

USAGE:
    python familypornhd_grab.py <url>
"""

from __future__ import annotations

import json
import os
import re
import sys
from html import unescape as html_unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:  # Keep the pure HTML extractors usable without requests.
    requests = None


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_MEDIA_EXTENSIONS = (
    ".mp4", ".m3u8", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".mpd",
)


def _normalise_candidate(value: str, base_url: str) -> str:
    """Turn an HTML/JavaScript URL value into an absolute URL.

    Page source commonly contains JSON-escaped slashes (``https:\\/\\/``),
    HTML entities, protocol-relative URLs, and a little punctuation from the
    surrounding JavaScript expression.  ``urljoin`` handles both absolute and
    relative paths once those wrappers are removed.
    """
    value = html_unescape(str(value or "")).strip()
    if not value:
        return ""

    # JSON/JavaScript escaping seen in inline player configuration.
    value = value.replace("\\/", "/")
    value = value.replace("\\u002f", "/").replace("\\u002F", "/")
    value = value.replace("\\u003f", "?").replace("\\u003F", "?")
    value = value.replace("\\u0026", "&")
    value = value.strip().strip("\\\"'")

    # Do not mistake a player API/javascript URL for a media URL.
    if value.lower().startswith(("javascript:", "data:", "blob:")):
        return ""

    # A quoted URL can be followed by a JS/JSON delimiter.  Do not strip
    # valid URL characters such as ')' when they are percent-encoded.
    value = value.rstrip("\r\n\t ,;)]}")
    if not value:
        return ""

    try:
        resolved = urljoin(base_url, value)
        parsed = urlparse(resolved)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return resolved


def _looks_like_media_url(value: str) -> bool:
    """Return whether *value* names a playable media resource.

    The extension may be in the path or in a query value, e.g. a signed
    ``/download?id=...&file=movie.mp4`` URL.  Match an extension at a URL
    boundary rather than merely searching for ``.mp4``: preview images on
    this site are named like ``preview_1080p.mp4.jpg`` and must not be
    mistaken for the video.
    """
    try:
        parsed = urlparse(value)
        haystack = f"{parsed.path}?{parsed.query}#{parsed.fragment}".lower()
    except Exception:
        haystack = str(value or "").lower()
    extensions = "|".join(re.escape(ext.lstrip(".")) for ext in _MEDIA_EXTENSIONS)
    return bool(re.search(rf"\.(?:{extensions})(?:/)?(?=$|[?#&])", haystack))


def _append_media_candidate(
    out: list[str],
    seen: set[str],
    raw: str,
    base_url: str,
    *,
    allow_extensionless: bool = False,
) -> None:
    candidate = _normalise_candidate(raw, base_url)
    if not candidate or (not allow_extensionless and not _looks_like_media_url(candidate)):
        return
    if candidate not in seen:
        seen.add(candidate)
        out.append(candidate)


class _MediaHTMLParser(HTMLParser):
    """Collect media-bearing attributes without assuming attribute order."""

    _MEDIA_ATTRIBUTES = (
        "src", "href", "data-file", "data-src", "data-video", "data-url",
        "data-media", "content",
    )

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []
        self._seen: set[str] = set()
        self.title_parts: list[str] = []
        self._in_title = False

    def _add(self, value: str, *, allow_extensionless: bool = False) -> None:
        _append_media_candidate(
            self.urls,
            self._seen,
            value,
            self.base_url,
            allow_extensionless=allow_extensionless,
        )

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(k).lower(): str(v or "") for k, v in attrs}

        if tag == "title":
            self._in_title = True

        if tag in ("video", "source", "a", "link", "iframe"):
            for key in self._MEDIA_ATTRIBUTES:
                if key in attrs_dict:
                    # A URL in <video>/<source> or a data-file-style player
                    # attribute is media by definition even when the CDN uses
                    # an extensionless token path.
                    self._add(
                        attrs_dict[key],
                        allow_extensionless=(tag in ("video", "source") or key.startswith("data-")),
                    )
        elif tag == "meta":
            prop = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            # og:video and common player metadata use arbitrary attribute
            # ordering, so inspect the parsed dictionary rather than a regex.
            if prop.startswith(("og:video", "twitter:player:stream")):
                self._add(attrs_dict.get("content", ""), allow_extensionless=True)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def _quoted_values(html: str):
    """Yield quoted inline-script values, including escaped URLs."""
    # This is intentionally not limited to ``url``/``src`` keys.  Several
    # versions of the player use a short variable name for the actual file.
    for match in re.finditer(r"(['\"])(.*?)(?:\\\1|\1)", html, re.IGNORECASE | re.DOTALL):
        yield match.group(2)


def _extract_media_urls(html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    # Properly parse HTML attributes first.  This covers video/source tags,
    # og:video, data-file, and relative URLs.
    try:
        parser = _MediaHTMLParser(base_url)
        parser.feed(html or "")
        for url in parser.urls:
            if url not in seen:
                seen.add(url)
                urls.append(url)
    except Exception:
        # Keep the regex fallback useful even for malformed HTML.
        pass

    # Quoted values in inline JSON/JavaScript.  Both absolute and relative
    # values are accepted as long as they resolve to a media-looking URL.
    for value in _quoted_values(html or ""):
        _append_media_candidate(urls, seen, value, base_url)

    # Unquoted absolute/protocol-relative values occasionally appear in a
    # JavaScript object.  The look-ahead stops before HTML/JSON punctuation.
    absolute_pattern = re.compile(
        r"(?P<url>(?:https?:)?//[^\s\"'<>]+?"
        r"\.(?:mp4|m3u8|webm|mkv|mov|avi|m4v|mpd)"
        # Some FamilyPornHD file URLs put a slash between the extension and
        # the signed query: ``...mp4/?v-acctoken=...``.  Require a URL
        # boundary after the extension so ``preview.mp4.jpg`` is not cut off
        # and returned as if it were a video.
        r"(?:/(?:\?[^\s\"'<>]*)?|\?[^\s\"'<>]*|#[^\s\"'<>]*|(?=[\s\"'<>])))",
        re.IGNORECASE,
    )
    for match in absolute_pattern.finditer(html or ""):
        _append_media_candidate(urls, seen, match.group("url"), base_url)

    return urls


def extract_m3u8_from_page(html: str, base_url: str) -> list[str]:
    """Extract HLS/DASH playlist URLs from page HTML."""
    return [
        url for url in _extract_media_urls(html or "", base_url)
        if urlparse(url).path.lower().endswith((".m3u8", ".m3u", ".mpd"))
        or any(ext in (urlparse(url).query or "").lower() for ext in (".m3u8", ".m3u", ".mpd"))
    ]


def extract_video_urls_from_html(html: str, base_url: str) -> list[str]:
    """Extract direct video/HLS/DASH URLs from HTML and inline scripts."""
    return _extract_media_urls(html or "", base_url)


def extract_m3u8_from_packed_js(html: str, base_url: str) -> list[str]:
    """Compatibility fallback for callers that used the old function name."""
    return extract_m3u8_from_page(html, base_url)


def _extract_title(html: str, fallback_url: str) -> str:
    try:
        parser = _MediaHTMLParser(fallback_url)
        parser.feed(html or "")
        title = re.sub(r"\s+", " ", html_unescape("".join(parser.title_parts))).strip()
        if title:
            return title
    except Exception:
        pass
    match = re.search(
        r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
        html or "", re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", html_unescape(match.group(1))).strip() if match else ""


# ── KVS player embed page ────────────────────────────────────────────────────
#
# FamilyPornHD is a KVS (Kernel Video Sharing) install.  The watch page only
# carries an <iframe src=".../embed/<video_id>">, so fetching the article
# finds no media at all.  The embed page is the one that matters: it is plain
# server-rendered HTML holding the whole player config, with a freshly minted
# ``v-acctoken`` on every rendition::
#
#     video_url:      'https://dev.familypornhd.com/get_file/0/<id>.mp4/?v-acctoken=...&embed=true',
#     video_url_text: '480p',
#     video_alt_url:  '.../get_file/0/<id>.mp4/?v-acctoken=...',
#     video_alt_url_text: '720p',
#     video_alt_url2: '.../get_file/0/<id>.mp4/?v-acctoken=...',
#     video_alt_url2_text: '1080p',
#
# The token is minted server-side at render time and carries no expiry field
# (it decodes to "<n>|<embed>|<n>|<md5>" plus a 16-char hex suffix), so a URL
# read off a freshly fetched embed page is ready to hand to mpv immediately.
# This is what makes the whole browser capture unnecessary for these videos:
# two HTTP requests replace a 90-second Playwright session, and there is no
# window in which the token can go stale.
#
# ``event_reporting2`` also holds a get_file URL, but it is the stats beacon
# and must never be treated as a rendition, so only the video_* keys are read.

_KVS_URL_KEY_RE = re.compile(
    r"\b(video_url|video_alt_url\d*)\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_KVS_TEXT_KEY_RE = re.compile(
    r"\b(video_url_text|video_alt_url\d*_text)\s*:\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE)
_KVS_EMBED_RE = re.compile(r"(https?://[a-z0-9.\-]+/embed/\d+)", re.IGNORECASE)
_KVS_EMBED_REL_RE = re.compile(r'''["']/?((?:embed|player)/\d+)["']''', re.IGNORECASE)
_KVS_HEIGHT_RE = re.compile(r"(\d{3,4})\s*p(?![0-9a-z])", re.IGNORECASE)


def _kvs_quality_rank(key: str, label: str) -> tuple:
    """Sort key for one KVS rendition, best first.

    Prefer the explicit ``_text`` label (``1080p``); fall back to the key's
    numeric suffix, because KVS numbers its renditions in ascending quality
    (``video_url`` < ``video_alt_url`` < ``video_alt_url2``).
    """
    match = _KVS_HEIGHT_RE.search(label or "")
    height = int(match.group(1)) if match else 0
    suffix = re.search(r"(\d+)$", key or "")
    if suffix:
        order = int(suffix.group(1))
    else:
        # An unnumbered key is still ordered by KVS: plain ``video_url`` is the
        # lowest rendition and ``video_alt_url`` sits between it and
        # ``video_alt_url2``, so it must not tie with ``video_url`` at 0.
        order = 1 if str(key or "").startswith("video_alt_url") else 0
    return (height, order)


def _extract_kvs_player_streams(html: str, base_url: str) -> list:
    """Pull every rendition out of a KVS player config, best quality first."""
    text = html or ""
    if "video_url" not in text:
        return []
    labels = {}
    for match in _KVS_TEXT_KEY_RE.finditer(text):
        labels[match.group(1).lower()] = match.group(2).strip()

    found, seen = [], set()
    for match in _KVS_URL_KEY_RE.finditer(text):
        key = match.group(1).lower()
        url = _normalise_candidate(match.group(2), base_url)
        if not url or url in seen or not _looks_like_media_url(url):
            continue
        seen.add(url)
        label = labels.get(key + "_text", "")
        found.append((_kvs_quality_rank(key, label), label, url))

    found.sort(key=lambda item: item[0], reverse=True)
    if found:
        best = found[0]
        print(
            f"[FAMILYPORNHD] KVS player config offers {len(found)} rendition(s), "
            f"best is {best[1] or 'unlabelled'}: {best[2][:120]}"
        )
    return [url for _rank, _label, url in found]


def _find_kvs_embed_url(html: str, base_url: str) -> str:
    """Return the KVS ``/embed/<id>`` page URL referenced by a watch page."""
    text = html or ""
    match = _KVS_EMBED_RE.search(text)
    if match:
        return match.group(1)
    match = _KVS_EMBED_REL_RE.search(text)
    if match:
        return urljoin(base_url, "/" + match.group(1))
    return ""


# ── FirePlayer (watchstreamhd) ───────────────────────────────────────────────
#
# The articles that are NOT hosted by the site's own KVS player embed
# ``watchstreamhd.com/video/<32-hex>``, which runs FirePlayer.  Its source is
# fetched by an XHR whose shape is fixed and readable straight out of
# ``/player/assets/scripts.php``::
#
#     $.ajax({ type:"POST", url:"/player/index.php?data="+ID+"&do=getVideo",
#              data:{hash:ID, r:document.referrer},
#              success: function(data){ var jData = JSON.parse(data); ... } })
#
# and then either ``jwSettings.file = jData.videoSource`` (hls) or
# ``jwSettings.sources = jData.videoSources``.  The site blocks DevTools -- the
# player will not even start while it is open -- so the response is read here
# instead of in a browser, and printed so its shape is visible in the console.
#
# The three articles that DO resolve show the player requesting plain
# ``/cdn/down/<id>/files/<slug>_und_720p.mp4?md5=...&expires=...`` URLs a
# second after the encrypted ``master.txt``, which is what those MP4s look
# like when the decryption succeeds.

_WATCHSTREAM_EMBED_RE = re.compile(
    r"(https?://[a-z0-9.\-]+/video/([0-9a-f]{16,40}))", re.IGNORECASE)


def _http_post(url: str, data: dict, referer: str = "", timeout: int = 20) -> tuple:
    """POST *data* and return ``(final_url, text)``, preferring curl_cffi."""
    headers = {
        "User-Agent": _UA,
        "Referer": referer or "https://familypornhd.com/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": referer.rsplit("/", 1)[0] if referer else "",
    }
    errors = []
    try:
        import curl_cffi.requests as cfreq

        response = cfreq.post(url, data=data, headers=headers,
                              impersonate="chrome131", timeout=timeout,
                              allow_redirects=True)
        if getattr(response, "status_code", 200) < 400:
            return (str(getattr(response, "url", "") or url),
                    str(response.text or ""))
        errors.append(f"curl_cffi HTTP {response.status_code}")
    except ImportError:
        errors.append("curl_cffi not installed")
    except Exception as exc:
        errors.append(f"curl_cffi {type(exc).__name__}: {exc}")

    if requests is None:
        raise RuntimeError("; ".join(errors) or "no HTTP transport available")
    response = requests.post(url, data=data, headers=headers, timeout=timeout,
                             allow_redirects=True)
    response.raise_for_status()
    return (str(response.url or url), str(response.text or ""))


# The same endpoint answers a plain GET with the rendered player page, and
# that page's Download menu carries the direct MP4s in cleartext -- the AES
# blobs in the POST response are only how the download button is dressed up.
# Verified against /video/b20bb95ab626d93fd976af958fbc61ba, which served
#   [ENG] 360p -> https://bestvideostream.com/cdn/down/<id>/files/…_eng_360p.mp4?md5=…&expires=…
#   [ENG] 720p -> …_eng_720p.mp4?md5=…&expires=…
# The CDN host rotates between requests (video-streams.com and
# bestvideostream.com in two back-to-back fetches), so it is never assumed.

_MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _is_hls_master_url(url: str) -> bool:
    """Whether *url* is an HLS playlist rather than a progressive file."""
    return str(url or "").split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))


def _is_absolute_http_url(url: str) -> bool:
    """Whether *url* is an absolute http(s) URL, as opposed to a blob or a blob
    of ciphertext.

    The FirePlayer POST puts AES ciphertext in ``downloadLinks[].file`` -- a
    bare ``{"ct":…,"iv":…,"s":…}`` string -- and that shape was being handed to
    the playlist as if it were a stream.  ``_looks_like_media_url`` alone would
    already reject it, but the scheme is checked separately so the two rules
    stay independently readable.
    """
    return str(url or "").strip().lower().startswith(("http://", "https://"))


def _fireplayer_download_links(page_url: str) -> list:
    """Scrape the cleartext Download menu off a FirePlayer player page."""
    try:
        _final, text = _http_get(page_url)
    except Exception as exc:
        print(f"[FAMILYPORNHD] FirePlayer page fetch failed: {exc}")
        return []

    found, seen = [], set()
    for url in _MEDIA_URL_RE.findall(text or ""):
        url = html_unescape(url)
        if url in seen or "/cdn/down/" not in url:
            continue
        if not _is_absolute_http_url(url) or not _looks_like_media_url(url):
            continue
        seen.add(url)
        match = _KVS_HEIGHT_RE.search(url)
        found.append((int(match.group(1)) if match else 0, len(found), url))

    found.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)
    if found:
        print(f"[FAMILYPORNHD] FirePlayer download menu offers "
              f"{len(found)} direct URL(s), best {found[0][2][:120]}")
    return [url for _height, _order, url in found]


def _fireplayer_pick_best(payload: dict) -> list:
    """Pull playable URLs out of a FirePlayer getVideo payload, best first.

    Handled defensively because the site blocks DevTools and the exact key
    layout has never been observed: JW Player takes ``sources`` as objects
    with a ``file``, FirePlayer's own download list uses ``downloadLinks``,
    and a bare ``videoSource`` string is the HLS fallback.
    """
    if not isinstance(payload, dict):
        return []

    found, seen = [], set()

    def _add(url, label):
        url = str(url or "").strip()
        if (not url or url in seen or not _is_absolute_http_url(url)
                or not _looks_like_media_url(url)):
            return
        seen.add(url)
        match = _KVS_HEIGHT_RE.search(str(label or "")) or _KVS_HEIGHT_RE.search(url)
        height = int(match.group(1)) if match else 0
        found.append((height, len(found), url))

    for key in ("videoSources", "sources", "downloadLinks", "attachmentLinks"):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                _add(item.get("file") or item.get("src") or item.get("url")
                     or item.get("href") or item.get("link"),
                     item.get("label") or item.get("title") or "")
            else:
                _add(item, "")

    found.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)
    return [url for _height, _order, url in found]


def _extract_fireplayer_streams(html: str, base_url: str) -> tuple:
    """Resolve a watchstreamhd/FirePlayer embed.

    Returns ``(links, downloads_absent)``.  ``downloads_absent`` is True when
    the getVideo response carried no ``downloadLinks`` at all.

    That distinction decides whether the browser capture is worth running.
    When the response lists download variants the capture reliably ends early
    on the ``/cdn/down/`` MP4 it fetches, and that file is better to seek in
    than the HLS master -- so the capture gets first shot.  When the list is
    empty there is no such file to fetch, the capture can only run to its full
    deadline and come back with nothing, and the master should be played
    straight away.  Three HLS-only articles in one field log each burned the
    whole deadline before falling back: age_ms 91274, 50778 and 88906.
    """
    match = _WATCHSTREAM_EMBED_RE.search(html or "")
    if not match:
        return [], False
    page_url, video_id = match.group(1), match.group(2)
    parsed = urlparse(page_url)
    if not (parsed.scheme and parsed.netloc):
        return [], False

    links = _fireplayer_download_links(page_url)
    if links:
        return links, False

    api = f"{parsed.scheme}://{parsed.netloc}/player/index.php?data={video_id}&do=getVideo"
    print(f"[FAMILYPORNHD] FirePlayer embed {page_url} -> POST {api}")
    try:
        _final, text = _http_post(api, {"hash": video_id, "r": base_url},
                                  referer=base_url)
    except Exception as exc:
        print(f"[FAMILYPORNHD] FirePlayer getVideo failed: {exc}")
        return [], False

    # The endpoint answers plain text when it refuses ("Video not found."),
    # so JSON is the only success signal -- same test the player itself uses.
    try:
        payload = json.loads(text)
    except Exception:
        print(f"[FAMILYPORNHD] FirePlayer getVideo was not JSON: {str(text)[:200]!r}")
        return [], False

    if isinstance(payload, dict):
        print(
            "[FAMILYPORNHD] FirePlayer getVideo keys: "
            f"{sorted(payload.keys())} hls={payload.get('hls')!r}"
        )
        # Printed deliberately: DevTools is blocked on this site, so this is
        # the only way the real payload shape ever reaches us. Truncated
        # because it can carry a long encrypted playlist.
        print(f"[FAMILYPORNHD] FirePlayer getVideo raw: {str(text)[:1200]}")

    links = _fireplayer_pick_best(payload if isinstance(payload, dict) else {})
    if links:
        print(f"[FAMILYPORNHD] FirePlayer yielded {len(links)} URL(s), "
              f"best {links[0][:160]}")
        return links, False

    # No progressive MP4 to be had: for hls videos downloadLinks[].file is AES
    # ciphertext, and some videos carry no download variants at all.  The
    # response still names a signed HLS master in cleartext --
    #   "securedLink":"https://watchstreamhd.com/cdn/hls/<id>/master.m3u8?md5=…&expires=…"
    # -- which is the same playlist the in-page player streams, and mpv reads
    # it directly.  It needs no Referer trick and no decryption, so it is what
    # the videos with an empty downloadLinks fall back to.
    secured = str(payload.get("securedLink") or "").strip() if isinstance(payload, dict) else ""
    if _is_absolute_http_url(secured):
        _has_downloads = bool(isinstance(payload, dict) and (payload.get("downloadLinks") or []))
        print(
            f"[FAMILYPORNHD] FirePlayer signed HLS master: {secured[:150]}"
            + ("" if _has_downloads else
               " (no download variants offered, skipping the browser capture)")
        )
        return [secured], not _has_downloads
    return links, False


def _http_get(url: str, referer: str = "", timeout: int = 20) -> tuple:
    """Fetch *url* and return ``(final_url, text)``.

    Prefers curl_cffi so the TLS/JA3 fingerprint looks like a real browser --
    the site is behind Cloudflare (its embed page loads rocket-loader) and a
    bare ``requests`` GET can be challenged.  Falls back to ``requests`` when
    curl_cffi is missing or errors, and raises only if both fail.
    """
    headers = {
        "User-Agent": _UA,
        "Referer": referer or "https://familypornhd.com/",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    errors = []
    try:
        import curl_cffi.requests as cfreq

        response = cfreq.get(url, headers=headers, impersonate="chrome131",
                             timeout=timeout, allow_redirects=True)
        if getattr(response, "status_code", 200) < 400:
            return (str(getattr(response, "url", "") or url),
                    str(response.text or ""))
        errors.append(f"curl_cffi HTTP {response.status_code}")
    except ImportError:
        errors.append("curl_cffi not installed")
    except Exception as exc:
        errors.append(f"curl_cffi {type(exc).__name__}: {exc}")

    if requests is None:
        raise RuntimeError("; ".join(errors) or "no HTTP transport available")
    response = requests.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True)
    response.raise_for_status()
    return (str(response.url or url), str(response.text or ""))


def fetch_and_extract(url: str, session=None) -> dict:
    """Fetch one page and return its extracted links plus playback headers.

    The KVS embed page is tried first because it is the only place the signed
    ``get_file`` URLs actually appear.  The generic HTML sweep stays as a
    fallback for pages that do not use the KVS player.
    """
    del session  # each hop now uses its own browser-impersonating request

    page_url, html = _http_get(url)
    title = _extract_title(html, page_url)

    links = _extract_kvs_player_streams(html, page_url)
    kvs_embed = bool(links)
    # The player page the request should look like it came from.  KVS rejects
    # (or redirects away from) a get_file request with an empty Referer, hence
    # ``empty_referer_redirect`` in the config, so this is not optional.
    referer = page_url

    if not links:
        embed_url = _find_kvs_embed_url(html, page_url)
        if embed_url and embed_url != page_url:
            print(f"[FAMILYPORNHD] watch page points at KVS embed: {embed_url}")
            try:
                embed_page_url, embed_html = _http_get(embed_url, referer=page_url)
            except Exception as exc:
                print(f"[FAMILYPORNHD] embed page fetch failed for {embed_url}: {exc}")
            else:
                links = _extract_kvs_player_streams(embed_html, embed_page_url)
                kvs_embed = bool(links)
                referer = embed_page_url
                if not title:
                    title = _extract_title(embed_html, embed_page_url)

    fireplayer = False
    fireplayer_hls = False
    if not links:
        # Articles whose video is externally hosted embed FirePlayer instead
        # of the KVS player. The player page's Download menu carries the
        # direct MP4s in cleartext, so no browser is needed and the encrypted
        # master.txt the player streams from is never touched.
        links, _fp_no_downloads = _extract_fireplayer_streams(html, page_url)
        if links:
            _embed22 = _WATCHSTREAM_EMBED_RE.search(html or "")
            referer = _embed22.group(1) if _embed22 else page_url
            # With download variants on offer the browser capture ends early on
            # the progressive MP4, which is better to seek in than HLS -- so
            # only that case withholds the short-circuit.  Without them the
            # capture has nothing to find and would idle to its deadline.
            if _is_hls_master_url(links[0]) and not _fp_no_downloads:
                fireplayer_hls = True
            else:
                fireplayer = True

    if not links:
        links = extract_video_urls_from_html(html, page_url)
    if not links:
        links = extract_m3u8_from_packed_js(html, page_url)

    parsed = urlparse(referer)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    headers = {"User-Agent": _UA, "Referer": referer}
    if origin:
        headers["Origin"] = origin
    return {
        "source_url": page_url,
        "title": title,
        "links": links,
        "headers": headers,
        # True only when these links were read out of a KVS player config,
        # i.e. they are signed get_file URLs minted moments ago rather than
        # whatever the generic HTML sweep happened to find.
        "kvs_embed": kvs_embed,
        # True when the links came from a FirePlayer Download menu. Like
        # ``kvs_embed`` this marks them as minted on purpose, so the caller
        # can skip the browser capture.
        "fireplayer": fireplayer,
        # True when the only thing FirePlayer offered was a signed HLS master.
        # Deliberately does not short-circuit the browser capture.
        "fireplayer_hls": fireplayer_hls,
    }


def grab_all(url: str, play_first: bool = True) -> dict:
    """Fetch *url*, print/save the found links, and return a result dict.

    ``play_first`` is accepted for compatibility with the other integrations;
    playlist autoplay is handled by the Qt integration, not by this module.
    """
    del play_first
    result = {"source_url": url, "title": "", "links": [], "headers": {},
              "kvs_embed": False, "fireplayer": False, "fireplayer_hls": False}
    try:
        result = fetch_and_extract(url)
        links = result["links"]
        if not links:
            print("No stream URLs found on the page.")
            return result

        print(f"Found {len(links)} stream URL(s):")
        for i, link in enumerate(links, 1):
            print(f"  {i}. {link}")

        output_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "url.txt")
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(links) + "\n")
        print(f"\nSaved URLs to {output_file}")
    except Exception as exc:
        result["error"] = str(exc)
        print(f"Error grabbing familypornhd: {exc}")
    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    url = args[0] if args else None
    if not url:
        url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "url.txt")
        if os.path.isfile(url_file):
            with open(url_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            url = lines[0] if lines else ""
        if not url:
            print("Usage: python familypornhd_grab.py <url>")
            raise SystemExit(1)
    grab_all(url)
