#!/usr/bin/env python3
"""
generic_jav_grab.py  ?"  Extract stream URLs from generic JAV/video pages.
"""

import sys
import json
import re
import traceback
from html import unescape
from urllib.parse import urlparse, urljoin

try:
    import curl_cffi.requests as cfreq
except ImportError:
    print(json.dumps({'error': 'curl_cffi is required. pip install curl_cffi'}))
    sys.exit(1)

# R45: embed-player hosters recognized on javgg / jav aggregator pages.
# The old hard-coded list (dood/streamtape/voe/mixdrop/...) missed the
# hosts javgg actually uses today — emturbovid, javclan, vidara — so those
# buttons' embeds were dropped from the capture. Keep this list broad:
# every host here has (or gets) a dedicated resolver in main.py.
EMBED_HOST_TOKENS = (
    'dood', 'doply', 'all3do', 'd-s', 'do7go', 'vide0', 'playmogo',
    'ds2play', 'ds2video', 'dsvplay', 'doodcdn', 'doodstream',
    'streamtape', 'voe', 'voe-unblock', 'voeunblock', 'voeunblck',
    'mixdrop', 'mxdrop', 'pornfhd', 'trailerhg', 'sextb',
    'javhdporn', 'javdock',
    # R45 field-named hosts + common jav-embed families
    'emturbovid', 'turbovid', 'javclan', 'vidara',
    'filelions', 'lulustream', 'luluvdo', 'streamwish', 'vidhide',
    'vidwatch', 'maxstream', 'turtleviplay', 'roshy',
    # R48: javgg/javguru-style packed-JS hosters (Server SW/VH/TB on javgg)
    'filemoon', 'swhoi', 'awish', 'javstreamhq',
    'kinoger', 'ryderjet', 'smoothpre', 'dhtpre', 'peytonepre', 'earnvids',
    # R49: jav.guru mirror hosters (field-named)
    'kamehamehaa', 'kamehaus',
    # R56: additional dood/voe aliases
    'dood.li', 'dood.yt', 'dood.to', 'dood.so', 'dood.watch', 'dood.pm',
    'dood.sh', 'dood.ws', 'dood.cx', 'dood.la', 'dood.one',
    'voe.sx', 'voe.ru', 'voe.ws', 'voe.is', 'veev.to', 'govoe', 'chillx',
    'eugenemakedraw', 'javlesbians',
)


def _extract_title(html: str) -> str:
    """Best-effort title extraction."""
    # 1. og:title
    m = re.search(r'<meta[^>]+property=[\"\']og:title[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']', html, re.IGNORECASE)
    if m:
        t = unescape(m.group(1)).strip()
        if t: return t
        
    # 2. title tag
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        t = unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
        if t: return t
        
    return "Unknown Generic Video"

def grab_all(url: str):
    result = {'title': '', 'streams': []}
    
    try:
        parsed_url = urlparse(url)
        host = parsed_url.netloc.lower()
        # Derive a slug from the URL to filter out related videos (like javdock/javhdporn previews)
        slug = ""
        parts = [p for p in parsed_url.path.split('/') if p.strip()]
        if parts:
            slug = parts[-1].lower().replace('.html', '')

        r = cfreq.get(
            url,
            impersonate="chrome131",
            timeout=15,
            headers={
                'Referer': url,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            }
        )
        if r.status_code != 200:
            result['error'] = f"HTTP {r.status_code}"
            return result
            
        html = r.text
        result['title'] = _extract_title(html)
        
        streams = set()
        
        # 1. Find m3u8
        for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.m3u8[^\s\"\'<>]*)', html):
            streams.add(m.group(1).replace('\\/', '/'))
            
        # 2. Find mp4 (ignore on jable to avoid 21 previews)
        if 'jable.tv' not in host:
            for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.mp4[^\s\"\'<>]*)', html):
                streams.add(m.group(1).replace('\\/', '/'))
            
        # 3. Find known embeds (dood, streamtape, voe, mixdrop, video.pornfhd,
        #    trailerhg, sextb, emturbovid, javclan, vidara, ... — EMBED_HOST_TOKENS)
        # Using a broad regex for /embed/, /e/, /v/
        for m in re.finditer(r'(https?://[^\s\"\'<>]+?(?:/embed/|/e/|/v/)[^\s\"\'<>]*)', html):
            embed_url = m.group(1).replace('\\/', '/')
            if any(x in embed_url.lower() for x in EMBED_HOST_TOKENS):
                streams.add(embed_url)
                
        # 4. Try looking for iframe src directly if it didn't match above.
        #    R45: also scan data-src / data-litespeed-src / data-embed-src —
        #    LiteSpeed-cached jav pages swap the real src in via JS.
        for m in re.finditer(
                r'<iframe[^>]+(?:src|data-src|data-litespeed-src|data-embed-src|data-frame-src)=[\"\']([^\"\']+)[\"\']',
                html, re.IGNORECASE):
            src = m.group(1).replace('\\/', '/')
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = f"{parsed_url.scheme}://{parsed_url.netloc}{src}"
                
            if any(x in src.lower() for x in EMBED_HOST_TOKENS):
                streams.add(src)
                
        # Extract the JAV code from the title or og:title (e.g. "SDMF-033 ...") for filtering.
        # This is more reliable than the URL slug because CDN filenames use the code too.
        jav_code = ''
        title_text = result.get('title', '')
        jav_code_match = re.search(r'\b([A-Z]{2,8}-\d{3,6})\b', title_text.upper())
        if not jav_code_match:
            # Fallback: try extracting from the URL slug
            jav_code_match = re.search(r'\b([A-Z]{2,8}-\d{3,6})\b', slug.upper())
        if jav_code_match:
            jav_code = jav_code_match.group(1).lower()  # e.g. 'sdmf-033'

        # Filter streams to avoid related video previews using the JAV code
        # Only filter mp4 streams — m3u8 and embed links are never related-video lists
        final_streams = set()
        for u in streams:
            u_lower = u.lower()
            if '123av.com' in host:
                if 'surrit.com' in u or 'v.m3u8' in u_lower or 'video.m3u8' in u_lower:
                    final_streams.add(u)
                    continue
            
            if 'preview' in u_lower:
                continue
            # For CDN mp4s, check that the JAV code appears in the filename
            if jav_code and u_lower.endswith('.mp4'):
                # Code appears as e.g. "_SDMF-033.mp4" or "sdmf-033" in path
                code_no_dash = jav_code.replace('-', '')
                if jav_code not in u_lower and code_no_dash not in u_lower:
                    continue
            final_streams.add(u)
            
        # Recursively fetch embed pages (sextb.net/e/..., trailerhg.xyz/e/...) to get the real streams
        # Also remove them from the final list since they're page URLs, not direct streams
        embed_pages = set()
        real_streams = set()
        for s in final_streams:
            if 'sextb.net/e/' in s or 'trailerhg.xyz/e/' in s:
                embed_pages.add(s)
            else:
                real_streams.add(s)
        final_streams = real_streams
        
        for s in embed_pages:
            try:
                r2 = cfreq.get(s, impersonate="chrome131", timeout=12, headers={'Referer': url})
                html2 = r2.text
                for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.m3u8[^\s\"\'<>]*)', html2):
                    final_streams.add(m.group(1).replace('\\/', '/'))
                for m in re.finditer(r'(https?://[^\s\"\'<>]+?\.mp4[^\s\"\'<>]*)', html2):
                    u = m.group(1).replace('\\/', '/')
                    if 'preview' not in u.lower():
                        final_streams.add(u)
                # Also check for nested iframes pointing to real players
                for m in re.finditer(r'<iframe[^>]+src=[\"\']([^\"\']+)[\"\']', html2, re.IGNORECASE):
                    src2 = m.group(1).replace('\\/', '/')
                    if src2.startswith('//'):
                        src2 = 'https:' + src2
                    if any(x in src2.lower() for x in EMBED_HOST_TOKENS):
                        final_streams.add(src2)
            except Exception:
                pass
        
        # Remove the original page URL itself if it snuck into streams
        page_url_norm = url.rstrip('/')
        final_streams.discard(page_url_norm)
        final_streams.discard(page_url_norm + '/')

        result['streams'] = list(final_streams)
        
    except Exception as e:
        result['error'] = f"Exception: {str(e)}"
        
    return result

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: generic_jav_grab.py <url>'}))
        sys.exit(1)
        
    target_url = sys.argv[1]
    data = grab_all(target_url)
    print(json.dumps(data))
