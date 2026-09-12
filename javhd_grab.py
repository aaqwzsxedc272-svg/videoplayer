#!/usr/bin/env python3
"""
javhd_grab.py  —  Extract stream URLs (m3u8 master playlist links) from javhd.today video pages.

USAGE:
    python javhd_grab.py                     # process url.txt in current directory
    python javhd_grab.py <url_or_file>
"""

import re
import sys
import os
import json
import base64
from urllib.parse import urljoin
import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def unpack_packer(p: str, a: int, c: int, k: str) -> str:
    """Unpack Dean Edwards packed JavaScript code."""
    k_list = k.split('|')
    def baseN(num, b):
        return ((num == 0) and '0') or (baseN(num // b, b).lstrip('0') + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"[num % b])

    for i in range(c - 1, -1, -1):
        if i < len(k_list) and k_list[i]:
            word_key = baseN(i, a)
            p = re.sub(r'\b' + word_key + r'\b', k_list[i], p)
    return p

def extract_m3u8_from_lulustream(lulu_url: str, session: requests.Session) -> str | None:
    """Extract direct m3u8 stream URL from Lulustream embed page."""
    headers = {'User-Agent': _UA, 'Referer': 'https://javhd.today/'}
    try:
        resp = session.get(lulu_url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return None
            
        # Try direct regex first
        m3u8s = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', resp.text)
        if m3u8s:
            return m3u8s[0]
            
        # Try unpacking Dean Edwards JS packer
        m = re.search(r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.+?)'\.split\('\|'\)\)\)", resp.text, re.DOTALL)
        if m:
            p, a, c, k = m.groups()
            unpacked = unpack_packer(p, int(a), int(c), k)
            m3u8s = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', unpacked)
            if m3u8s:
                return m3u8s[0]
    except Exception as e:
        print(f"    [!] Error unpacking {lulu_url}: {e}")
    return None

def _decode_embed_url(raw: str) -> str:
    raw = str(raw or '').strip()
    if not raw:
        return ''
    try:
        decoded = base64.b64decode(raw + '===').decode('utf-8', errors='ignore').strip()
        if decoded.startswith('http'):
            return decoded
    except Exception:
        pass
    return raw

def _extract_title(html: str) -> str:
    patterns = [
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        r'<title[^>]*>(.*?)</title>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            title = re.sub(r'\s*-\s*Javhd\.today.*$', '', title, flags=re.IGNORECASE).strip()
            if title:
                return title
    return ''

def grab_streams_from_javhd(url: str, session: requests.Session) -> dict:
    """Extract all available stream links from a javhd.today video page."""
    result = {'url': url, 'title': '', 'streams': {}}
    headers = {'User-Agent': _UA, 'Referer': 'https://javhd.today/'}
    
    try:
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            result['error'] = f"HTTP {resp.status_code}"
            return result
        html = resp.text
    except Exception as e:
        result['error'] = str(e)
        return result

    result['title'] = _extract_title(html)

    embed_candidates = []
    # Primary player buttons on the page use data-embed base64 payloads.
    for m in re.finditer(
        r'<button[^>]+data-embed=["\']([^"\']+)["\'][^>]*data-name=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        embed_candidates.append((_decode_embed_url(m.group(1)), m.group(2).strip()))

    # Download buttons can also be used as provider fallbacks.
    for m in re.finditer(
        r'window\.open\(\s*[\"\']([^\"\']+)[\"\']\s*\)',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        embed_candidates.append((_decode_embed_url(m.group(1)), 'Download'))

    seen = set()
    ordered = []
    for embed_url, label in embed_candidates:
        embed_url = urljoin(url, embed_url).strip()
        if not embed_url or embed_url in seen:
            continue
        seen.add(embed_url)
        ordered.append((embed_url, label or 'Stream'))

    def score(item):
        link, label = item
        label_l = (label or '').lower()
        link_l = link.lower()
        if 'lulustream' in link_l or 'lulustream' in label_l:
            return 0
        if 'cloudwish' in link_l or 'cloudwish' in label_l:
            return 1
        if 'mycloudz' in link_l or 'mycloudz' in label_l:
            return 2
        if 'turbovid' in link_l or 'turbovid' in label_l:
            return 3
        if 'streambeast' in link_l or 'upn' in label_l:
            return 4
        return 10

    ordered.sort(key=score)

    # Resolve Lulustream embeds to direct m3u8 when possible, but keep the embed as fallback.
    for i, (embed_url, label) in enumerate(ordered, 1):
        clean_label = label or f"STREAM {i}"
        lower = embed_url.lower()
        if 'lulustream' in lower or 'luluvdo' in lower:
            m3u8 = extract_m3u8_from_lulustream(embed_url, session)
            if m3u8:
                result['streams']['Lulustream'] = m3u8
                result['streams']['Lulustream Embed'] = embed_url
                continue
            result['streams']['Lulustream Embed'] = embed_url
        elif 'streambeast' in lower:
            result['streams']['Streambeast'] = embed_url
        else:
            result['streams'][clean_label] = embed_url

    # Also probe any iframes for embedded players.
    if not result['streams']:
        iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        for iframe_src in iframes:
            full_iframe = urljoin(url, iframe_src)
            if 'javhd.today/embed/' in full_iframe:
                try:
                    r_iframe = session.get(full_iframe, headers=headers, timeout=10)
                    if r_iframe.status_code == 200:
                        for m in re.finditer(r'https?://[^\s"\'<>]+', r_iframe.text):
                            link = m.group(0)
                            if link not in seen and any(host in link for host in ['lulustream.fit', 'lulustream.com', 'luluvdo.com', 'streambeast.upn.one']):
                                seen.add(link)
                                result['streams'][f"STREAM {len(result['streams']) + 1}"] = link
                except Exception:
                    pass

    # Prefer a playable primary stream first.
    if 'Lulustream' in result['streams']:
        primary = result['streams'].pop('Lulustream')
        result['streams'] = {'Lulustream': primary, **result['streams']}

    return result

def grab_all(url: str) -> tuple[str | None, dict[str, str]]:
    """Convenience function returning (title, streams_dict)."""
    session = requests.Session()
    res = grab_streams_from_javhd(url, session)
    return res.get('title'), res.get('streams', {})

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else 'url.txt'
    urls = []

    if os.path.isfile(target):
        content = open(target, encoding='utf-8', errors='ignore').read()
        matches = re.findall(r'https://javhd\.today/\d+/[^\s"<>]+', content)
        seen = set()
        for u in matches:
            u_clean = u.rstrip('/')
            if u_clean not in seen and not any(f'/{lang}/' in u_clean for lang in ['cn', 'ko', 'in', 'id', 'ja', 'fr', 'de', 'vi']):
                seen.add(u_clean)
                urls.append(u_clean)
    elif target.startswith('http'):
        urls.append(target)

    if not urls:
        print("[!] No valid javhd.today URLs found.")
        sys.exit(1)

    print(f"[*] Processing {len(urls)} javhd.today URL(s)...\n")
    session = requests.Session()
    
    all_results = []
    for u in urls:
        print(f"[*] Fetching: {u}")
        res = grab_streams_from_javhd(u, session)
        all_results.append(res)
        if res.get('title'):
            print(f"  Title  : {res['title']}")
        if res.get('streams'):
            print("  Streams:")
            for lbl, stream in res['streams'].items():
                print(f"    [{lbl}] {stream[:100]}")
        else:
            print("  [!] No streams found.")
        print()

    out_file = "last_javhd.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in all_results:
            f.write(f"URL: {r['url']}\n")
            if r.get('title'):
                f.write(f"Title: {r['title']}\n")
            for lbl, s_url in r.get('streams', {}).items():
                f.write(f"{lbl}: {s_url}\n")
            f.write("\n")
    print(f"[*] Saved results to {out_file}")

if __name__ == '__main__':
    main()
