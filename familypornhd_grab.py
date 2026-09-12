#!/usr/bin/env python3
"""Extract media links from familypornhd.com video pages.

The site currently puts the playable URL in the page source.  This module is
kept deliberately static: it fetches the page once and extracts the URL from
HTML/inline JSON instead of trying to automate the player.

USAGE:
    python familypornhd_grab.py <url>
"""

from __future__ import annotations

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


def fetch_and_extract(url: str, session=None) -> dict:
    """Fetch one page and return its extracted links plus playback headers."""
    if requests is None:
        raise RuntimeError("requests is required for familypornhd extraction (pip install requests)")

    if session is None:
        session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Referer": "https://familypornhd.com/"})

    response = session.get(url, timeout=20, allow_redirects=True)
    response.raise_for_status()
    page_url = response.url or url
    html = response.text or ""
    links = extract_video_urls_from_html(html, page_url)
    if not links:
        links = extract_m3u8_from_packed_js(html, page_url)

    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    headers = {"User-Agent": _UA, "Referer": page_url}
    if origin:
        headers["Origin"] = origin
    return {
        "source_url": page_url,
        "title": _extract_title(html, page_url),
        "links": links,
        "headers": headers,
    }


def grab_all(url: str, play_first: bool = True) -> dict:
    """Fetch *url*, print/save the found links, and return a result dict.

    ``play_first`` is accepted for compatibility with the other integrations;
    playlist autoplay is handled by the Qt integration, not by this module.
    """
    del play_first
    result = {"source_url": url, "title": "", "links": [], "headers": {}}
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
