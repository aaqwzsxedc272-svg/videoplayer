#!/usr/bin/env python3
"""
roshy_grab.py  —  Extract ALL stream URLs (original + mirrors) from a roshy.tv page.
                  No browser needed — URLs are embedded in the page HTML as base64.

Usage:
    python roshy_grab.py              # prompts you to enter the URL
    python roshy_grab.py <roshy_url>  # or pass it directly as before

Requirements:
    pip install requests beautifulsoup4
    # Optional Cloudflare bypass:
    pip install cloudscraper
"""

import sys
import re
import base64
import json
import os
import time
from html import unescape


# ── Config ────────────────────────────────────────────────────────────────────
DELAY_BETWEEN_REQUESTS = 0.8   # seconds — be polite to the server
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Brave/1.66"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://roshy.tv/",
}

_scraper = None   # lazy-init cloudscraper once


def fetch_html(url: str) -> str:
    """Fetch page HTML. Uses curl_cffi first (bypasses Cloudflare), then cloudscraper, then requests."""
    global _scraper
    # R56: try curl_cffi first — best CF bypass
    try:
        import curl_cffi.requests as cfreq
        r = cfreq.get(url, impersonate="chrome131", headers=HEADERS, timeout=20)
        if r.status_code == 200 and r.text and len(r.text) > 1000:
            # Check for roshy player marker or generic content
            if "beeteam" in r.text.lower() or "video_url" in r.text.lower() or "iframe" in r.text.lower():
                return r.text
            # Even if marker missing, return if status ok (CF may have changed marker)
            if "roshy" in r.text.lower() or "<html" in r.text.lower():
                return r.text
    except ImportError:
        pass
    except Exception as e:
        print(f"[curl_cffi] fetch failed: {e}")

    try:
        import requests as req
        r = req.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and "beeteam" in r.text.lower():
            return r.text
        if r.status_code == 200 and len(r.text) > 1000:
            return r.text
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: cloudscraper
    if _scraper is None:
        try:
            import cloudscraper
            _scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows"}
            )
        except ImportError:
            print("[!] Both requests and cloudscraper failed.")
            print("    Run: pip install cloudscraper or curl_cffi")
            sys.exit(1)

    r = _scraper.get(url, headers=HEADERS, timeout=20)
    return r.text


# ── Extract embed URL from a single page ─────────────────────────────────────

def extract_embed_url(html: str) -> str | None:
    """
    Find the inline base64 <script> that holds beeteam*_pro_player({...}),
    decode it, parse the JSON, and return the iframe src.
    R56: more generic — handles player name changes and direct iframe in decoded blob.
    """
    for b64 in re.findall(
        r'<script[^>]+src=["\']data:text/javascript;base64,([A-Za-z0-9+/=_-]+)["\']',
        html,
        re.IGNORECASE,
    ):
        try:
            # Handle URL-safe base64 and padding
            padded = b64 + ('=' * (-len(b64) % 4))
            padded = padded.replace('-', '+').replace('_', '/')
            decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            continue

        # Try beeteam pattern (any number)
        if "beeteam" in decoded.lower() or "pro_player" in decoded.lower():
            for pat in (r"beeteam\d*_pro_player\((\{.*?\})\)\s*;", r"pro_player\((\{.*?\})\)\s*;", r"beeteam.*pro_player\((\{.*?\})\)"):
                m = re.search(pat, decoded, re.DOTALL | re.IGNORECASE)
                if not m:
                    continue
                try:
                    config = json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
                video_url_html = config.get("video_url", "") or config.get("videoUrl", "") or ""
                if not video_url_html:
                    continue
                iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', video_url_html, re.IGNORECASE)
                if iframe_m:
                    return iframe_m.group(1).replace("\\/", "/").replace("\\/", "/")
                # Direct URL in video_url
                url_m = re.search(r'(https?://[^"\'<>]+)', video_url_html)
                if url_m:
                    return url_m.group(1).replace("\\/", "/")

        # Generic fallback: any iframe src in decoded blob
        iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', decoded, re.IGNORECASE)
        if iframe_m:
            return iframe_m.group(1).replace("\\/", "/")

        # Direct dood/voe/mixdrop URL in decoded blob
        for url_pat in (r'(https?://[^"\'<>]+?/e/[^"\'<>]+)', r'(https?://[^"\'<>]*dood[^"\'<>]+)', r'(https?://[^"\'<>]*voe[^"\'<>]+)'):
            url_m = re.search(url_pat, decoded, re.IGNORECASE)
            if url_m:
                return url_m.group(1).replace("\\/", "/")

    return None


# ── Parse mirror links from the main page ────────────────────────────────────

def parse_mirror_links(html: str) -> list[tuple[str, str]]:
    """
    Return [(label, url), ...] for all stream buttons:
      - Original Stream (the base URL)
      - Mirror 1 … Mirror N  (?ml-group=0&ml-url=N)
    """
    results = []

    # Match every .btn-p-group-item link with its label
    for m in re.finditer(
        r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*btn-p-group-item[^"\']*["\'][^>]*>'
        r'.*?<span>([^<]+)</span>',
        html,
        re.DOTALL,
    ):
        href = unescape(m.group(1).strip())
        label = m.group(2).strip()
        results.append((label, href))

    return results


# ── Extract <h1> title from the main page ────────────────────────────────────

def extract_title(html: str) -> str | None:
    """
    Return the text content of the first <h1> tag, or None if not found.
    Strips any inner HTML tags (e.g. <span>, <a>) and HTML-unescapes the result.
    """
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return unescape(inner) or None


# ── Extract <title> tab title from the main page ─────────────────────────────

def extract_tab_title(html: str) -> str | None:
    """
    Return the text content of the <title> tag (browser tab title), or None.
    HTML-unescapes the result.
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return unescape(inner) or None


# ── Pick the longest non-empty title ─────────────────────────────────────────

def best_title(*candidates: str | None) -> str | None:
    """
    Return the longest string among all non-None, non-empty candidates.
    Ties go to the first one in argument order.
    """
    valid = [c for c in candidates if c]
    if not valid:
        return None
    return max(valid, key=len)


# ── Main ──────────────────────────────────────────────────────────────────────

def grab_all(roshy_url: str) -> tuple[str | None, dict[str, str]]:
    """
    Returns (title, {label: embed_url}) for Original Stream + all mirrors.
    title is the longer of <title> (tab) and <h1>, or None if neither found.
    """
    # ── Step 1: fetch main page, get title + original embed + mirror links ────
    print(f"[*] Fetching main page …  {roshy_url}")
    main_html = fetch_html(roshy_url)

    h1_title  = extract_title(main_html)
    tab_title = extract_tab_title(main_html)

    print(f"  {'✔' if h1_title  else '!'}  <h1>    → {h1_title  or 'not found'}")
    print(f"  {'✔' if tab_title else '!'}  <title> → {tab_title or 'not found'}")

    title = best_title(h1_title, tab_title)
    if title:
        src = "<h1>" if title == h1_title else "<title>"
        print(f"  ★  Using {src} (longest): {title}")
    else:
        print("  [!] No title found in either <h1> or <title>")

    original_embed = extract_embed_url(main_html)
    mirror_links = parse_mirror_links(main_html)

    results: dict[str, str] = {}

    if original_embed:
        results["Original Stream"] = original_embed
        print(f"  ✔  Original Stream  →  {original_embed}")
    else:
        print("  [!] Original Stream embed not found")

    # ── Step 2: fetch each mirror page ────────────────────────────────────────
    for label, href in mirror_links:
        if "ml-url" not in href:
            continue                    # skip the "Original Stream" anchor itself

        time.sleep(DELAY_BETWEEN_REQUESTS)
        print(f"[*] Fetching {label} …  {href}")

        try:
            mirror_html = fetch_html(href)
            embed = extract_embed_url(mirror_html)
            if embed:
                results[label] = embed
                print(f"  ✔  {label}  →  {embed}")
            else:
                print(f"  [!] {label}: no embed found")
        except Exception as e:
            print(f"  [!] {label}: fetch failed — {e}")

    return title, results


def save_results(title: str | None, results: dict[str, str], script_dir: str):
    path = os.path.join(script_dir, "last_embed.txt")
    with open(path, "w", encoding="utf-8") as f:
        if title:
            f.write(f"Title: {title}\n\n")
        for label, url in results.items():
            f.write(f"{label}: {url}\n")
    print(f"\n[*] Saved {len(results)} URL(s) to: {path}")


if __name__ == "__main__":
    # Accept URL from CLI arg OR prompt interactively
    if len(sys.argv) >= 2:
        url = sys.argv[1].strip()
    else:
        print("roshy_grab — Stream URL extractor")
        print("─" * 40)
        url = input("Enter roshy.tv URL: ").strip()
        if not url:
            print("[!] No URL provided.")
            sys.exit(1)

    title, results = grab_all(url)

    print(f"\n{'─'*60}")

    # Always display and save title, whether or not streams were found
    if title:
        print(f"Title: {title}")
    else:
        print("[!] No title found in <h1> or <title>.")

    if results:
        print(f"Found {len(results)} stream(s):\n")
        for label, embed in results.items():
            print(f"  [{label}]")
            print(f"  {embed}\n")
        save_results(title, results, os.path.dirname(os.path.abspath(__file__)))
    else:
        # Still write the title even if no streams found
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, "last_embed.txt")
        with open(path, "w", encoding="utf-8") as f:
            if title:
                f.write(f"Title: {title}\n\n")
            f.write("No stream URLs found.\n")
        print(f"\n[!] No stream URLs found. Title written to: {path}")
        sys.exit(1)