#!/usr/bin/env python3
"""
sextb_grab.py  -  Extract stream URLs from sextb.net video page.

Uses the sextb API endpoint directly: /api/episode/{data-source}/{data-id}
Then parses the iframe/stream from the response JSON.
Falls back to Playwright if the API approach fails.
"""

import re
import sys
import json
import time
from urllib.parse import urlparse
from html import unescape

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

WAIT_PAGE_SETTLE   = 3_000
WAIT_AFTER_CLICK   = 3_500


def _extract_title(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        t = unescape(m.group(1)).strip()
        if t: return t
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        t = unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())
        if t: return t
    return ''


def _extract_buttons(html: str) -> list[dict]:
    """Parse all episode buttons from sextb page HTML."""
    buttons = []
    # Match: <button class="btn-player episode" data-source="16922333" data-id="3968335">..TB..
    for m in re.finditer(
        r'<button\s[^>]*class="btn-player\s+episode[^"]*"[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
        html, re.DOTALL | re.IGNORECASE
    ):
        label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if label:
            buttons.append({
                'label': label,
                'source': m.group(1),
                'epid': m.group(2),
            })
    # Also try data-id before data-source ordering
    if not buttons:
        for m in re.finditer(
            r'<button\s[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*class="[^"]*episode[^"]*"[^>]*>(.*?)</button>',
            html, re.DOTALL | re.IGNORECASE
        ):
            label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if label:
                buttons.append({
                    'label': label,
                    'source': m.group(1),
                    'epid': m.group(2),
                })
    return buttons


def _fetch_episode_stream(source_id: str, epid: str, referer: str, session) -> str | None:
    """
    Call the sextb API to get the player URL for a given episode ID.
    Returns the player iframe src URL or None.
    """
    import requests as r_mod
    
    api_url = f"https://sextb.net/api/episode/{source_id}/{epid}"
    headers = {
        'User-Agent': _UA,
        'Referer': referer,
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/plain, */*',
    }
    
    try:
        resp = session.get(api_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # Check various response structures
            src = (
                data.get('src') or data.get('url') or 
                data.get('embed') or data.get('iframe') or
                data.get('link') or data.get('stream')
            )
            if src:
                return src
            # Maybe it returns HTML with an iframe
            html_chunk = data.get('html') or data.get('content') or ''
            if html_chunk:
                m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html_chunk, re.IGNORECASE)
                if m:
                    return m.group(1)
    except Exception as e:
        print(f"    [API] {api_url} -> error: {e}")
    
    return None


def grab_all_static(url: str) -> dict:
    """
    Try to extract streams without launching a browser:
    1. Fetch the page HTML
    2. Parse button data-source and data-id attributes
    3. Call the /api/episode/ endpoint for each button
    """
    result = {'title': '', 'streams': []}
    
    try:
        import requests
    except ImportError:
        result['error'] = 'requests not installed'
        return result
    
    session = requests.Session()
    headers = {
        'User-Agent': _UA,
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://sextb.net/',
    }
    
    try:
        # Try cloudscraper first if available (bypasses Cloudflare)
        try:
            import cloudscraper
            cs = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows'})
            resp = cs.get(url, timeout=30)
            html = resp.text
        except ImportError:
            resp = session.get(url, headers=headers, timeout=30)
            html = resp.text
    except Exception as e:
        result['error'] = f'Page fetch failed: {e}'
        return result
    
    result['title'] = _extract_title(html)
    buttons = _extract_buttons(html)
    print(f"  Found {len(buttons)} episode buttons: {[b['label'] for b in buttons]}")
    
    seen = set()
    for btn in buttons:
        if btn['label'].upper().startswith('VIP'):
            continue  # skip VIP buttons
        
        stream_url = _fetch_episode_stream(btn['source'], btn['epid'], url, session)
        if stream_url and stream_url not in seen:
            seen.add(stream_url)
            result['streams'].append(stream_url)
            print(f"  [+] {btn['label']}: {stream_url[:80]}")
        
        time.sleep(0.5)
    
    return result


def grab_all_playwright(url: str, visible: bool = False) -> dict:
    """
    Playwright fallback: click each button and capture the iframe src.
    """
    result = {'title': '', 'streams': []}
    
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        result['error'] = 'playwright not installed'
        return result

    stealth_fn = None
    try:
        from playwright_stealth import stealth_sync
        stealth_fn = stealth_sync
    except ImportError:
        pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not visible,
            args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--window-size=1280,800',
            ],
        )
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={'width': 1280, 'height': 800},
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
        )
        page = ctx.new_page()
        if stealth_fn:
            stealth_fn(page)

        # Block navigations away from the main site to prevent ad hijacking
        def block_ads(route):
            req = route.request
            if req.is_navigation_request() and req.frame == page.main_frame and req.url != url:
                print(f"    [!] Blocked ad redirect to: {req.url}")
                route.abort()
            else:
                route.continue_()
        page.route("**/*", block_ads)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        except PwTimeout:
            result['error'] = 'Page load timed out'
            browser.close()
            return result

        page.wait_for_timeout(WAIT_PAGE_SETTLE)

        # Collect button data upfront from HTML (data-id is stable)
        html = page.content()
        result['title'] = _extract_title(html)
        print(f"  Title: {result['title'] or '(none)'}")

        btn_info = []
        for m in re.finditer(
            r'<button\s[^>]*class="btn-player\s+episode[^"]*"[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
            html, re.DOTALL | re.IGNORECASE
        ):
            label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if label and not label.upper().startswith('VIP'):
                btn_info.append({'label': label, 'source': m.group(1), 'epid': m.group(2)})

        # Also try alternate attribute ordering
        if not btn_info:
            for m in re.finditer(
                r'<button\s[^>]*data-source="(\d+)"[^>]*data-id="(\d+)"[^>]*>(.*?)</button>',
                html, re.DOTALL | re.IGNORECASE
            ):
                label = re.sub(r'<[^>]+>', '', m.group(3)).strip()
                if label and not label.upper().startswith('VIP'):
                    btn_info.append({'label': label, 'source': m.group(1), 'epid': m.group(2)})

        print(f"  Found {len(btn_info)} stream buttons: {[b['label'] for b in btn_info]}")

        seen = set()
        for btn in btn_info:
            label = btn['label']
            epid  = btn['epid']
            try:
                selector = f'button[data-id="{epid}"]'
                print(f"  [*] Clicking: {label} (epid={epid})")
                
                # If button is completely missing from DOM for some reason, recover
                if page.locator(selector).count() == 0:
                    print("    [!] Button missing. Recovering page...")
                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        page.wait_for_timeout(WAIT_PAGE_SETTLE)
                    except Exception:
                        pass

                iframe_found = False
                for attempt in range(2):
                    try:
                        # Use JS click - completely immune to any ad overlay interception
                        page.evaluate(f'''
                            (function() {{
                                var btn = document.querySelector('button[data-id="{epid}"]');
                                if (btn) {{ btn.dispatchEvent(new MouseEvent('click', {{bubbles: true}})); }}
                            }})()
                        ''')
                    except Exception as e:
                        print(f"    [!] Click failed: {e}")
                        break

                    # Read HTML IMMEDIATELY (500ms) — doodstream iframe URL appears
                    # in the page HTML right after the click but before any navigation
                    page.wait_for_timeout(500)
                    early_html = None
                    try:
                        early_html = page.content()
                    except Exception:
                        pass

                    page.wait_for_timeout(WAIT_AFTER_CLICK - 500)

                    # If ad hijacked the page, reload sextb before reading HTML
                    try:
                        current_url = page.url
                        if 'sextb.net' not in current_url:
                            print(f"    [!] Ad redirected to {current_url[:60]}. Reloading...")
                            page.goto(url, wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(WAIT_PAGE_SETTLE)
                    except Exception:
                        pass

                    # Read from HTML (safe even if context was destroyed by navigation)
                    try:
                        html_now = page.content()
                    except Exception:
                        break

                    src_found = None
                    # Scan entire HTML for any iframe that's not an ad
                    # After clicking, the player iframe is usually the only non-ad iframe present
                    AD_HOSTS = ('z5g022gc', 'trailerhg', 'googlesyndication', 'doubleclick',
                                'adsbygoogle', 'asg-interstitial', 'ads.', '/ads/')
                    # Try the early snapshot first (captures doodstream before redirect)
                    for html_check in ([early_html, html_now] if early_html else [html_now]):
                        if not html_check:
                            continue
                        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html_check, re.IGNORECASE):
                            candidate = m.group(1).strip()
                            if candidate and not any(h in candidate for h in AD_HOSTS):
                                src_found = candidate
                                break
                        if src_found:
                            break

                    if src_found and src_found not in seen:
                        seen.add(src_found)
                        result['streams'].append(src_found)
                        print(f"  [+] {label}: {src_found[:80]}")
                        iframe_found = True

                    if iframe_found:
                        break
                    else:
                        print(f"    [!] Iframe not loaded for {label}. Retrying click...")
                        page.wait_for_timeout(1000)

            except Exception as btn_err:
                print(f"    [!] Error on {label}: {btn_err}. Skipping.")
                continue

        browser.close()

    return result


def grab_all(url: str, visible: bool = False) -> dict:
    """Try static API first, then Playwright as fallback."""
    print(f"[sextb] Fetching: {url}")
    
    # First attempt: static API call (fast, no browser needed)
    result = grab_all_static(url)
    if result.get('streams'):
        print(f"  [OK] Static grab found {len(result['streams'])} stream(s)")
        return result
    
    print("  [!] Static grab failed, trying Playwright...")
    result = grab_all_playwright(url, visible=visible)
    return result


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: sextb_grab.py <url> [--visible]'}))
        sys.exit(1)
    target_url = sys.argv[1]
    vis = '--visible' in sys.argv
    data = grab_all(target_url, visible=vis)
    print(json.dumps(data))
