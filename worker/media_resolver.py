"""
Universal Media Stream Resolver for Riva Transit Pipeline.
Resolves cyberlocker/host URLs (DoodStream, Playmogo, Streamtape, Mixdrop, etc.)
into direct MP4/HLS downloadable stream URLs.
"""
import re
import time
import random
import string
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

try:
    from curl_cffi import requests as crequests
    HAS_CURL_CFFI = True
except ImportError:
    import urllib.request as urequest
    HAS_CURL_CFFI = False

logger = logging.getLogger("media_resolver")

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def http_get(url: str, referer: Optional[str] = None, timeout: int = 15) -> Tuple[int, str, dict]:
    """Helper to perform HTTP GET with browser impersonation."""
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"
    }
    if referer:
        headers["Referer"] = referer

    if HAS_CURL_CFFI:
        resp = crequests.get(url, headers=headers, impersonate="chrome120", timeout=timeout)
        return resp.status_code, resp.text, dict(resp.headers)
    else:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode('utf-8', errors='replace'), dict(resp.headers)
        except urllib.error.HTTPError as he:
            return he.code, "", {}


def resolve_doodstream(url: str) -> Optional[str]:
    """
    Resolves DoodStream / Playmogo / Dood.to / Dsvplay links to direct MP4 URL.
    Handles /d/ and /e/ URLs.
    """
    try:
        # Extract file code
        code_match = re.search(r'/(?:d|e)/([a-zA-Z0-9]+)', url)
        if not code_match:
            return None
        code = code_match.group(1)

        domain = "playmogo.com" if "playmogo" in url else "doodstream.com"
        embed_url = f"https://{domain}/e/{code}"

        status, html, _ = http_get(embed_url, referer="https://akwams.org/")
        if status != 200 or not html:
            # Fallback to playmogo if doodstream was blocked or redirected
            embed_url = f"https://playmogo.com/e/{code}"
            status, html, _ = http_get(embed_url, referer="https://akwams.org/")

        pass_md5_match = re.search(r'/pass_md5/([a-zA-Z0-9\-_/]+)', html)
        if not pass_md5_match:
            return None

        pass_md5_path = f"/pass_md5/{pass_md5_match.group(1)}"
        host = urlparse(embed_url).netloc
        md5_url = f"https://{host}{pass_md5_path}"

        st_md5, prefix, _ = http_get(md5_url, referer=embed_url)
        if st_md5 != 200 or not prefix:
            return None

        prefix = prefix.strip()
        random_str = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        token = pass_md5_path.split("/")[-1]
        expiry = int(time.time() * 1000)

        final_direct_url = f"{prefix}{random_str}?token={token}&expiry={expiry}"
        logger.info(f"Successfully resolved DoodStream/Playmogo to direct MP4: {final_direct_url[:60]}...")
        return final_direct_url
    except Exception as e:
        logger.warning(f"Failed to resolve DoodStream link {url}: {e}")
        return None


def resolve_streamtape(url: str) -> Optional[str]:
    """Resolves Streamtape links to direct MP4 URL."""
    try:
        code_match = re.search(r'/(?:e|v)/([a-zA-Z0-9]+)', url)
        if not code_match:
            return None
        code = code_match.group(1)
        embed_url = f"https://streamtape.com/e/{code}"

        status, html, _ = http_get(embed_url)
        if status != 200 or not html:
            return None

        m = re.search(r"document\.getElementById\(['\"](?:robotlink|ideolink)['\"]\)\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]", html)
        if not m:
            m = re.search(r"document\.getElementById\(['\"](?:robotlink|ideolink)['\"]\)\.innerHTML\s*=\s*(.*?);", html)
            if m:
                tokens = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
                link = "".join(tokens)
            else:
                return None
        else:
            link = m.group(1)

        if link.startswith("//"):
            link = "https:" + link
        logger.info(f"Successfully resolved Streamtape to direct MP4: {link[:60]}...")
        return link
    except Exception as e:
        logger.warning(f"Failed to resolve Streamtape link {url}: {e}")
        return None


def unpack_dean_edwards(p, a, c, k, e, d):
    """Unpacks Dean Edwards p,a,c,k,e,d JavaScript packer."""
    def e_func(c):
        return ('' if c < a else e_func(int(c / a))) + (chr(c % a + 29) if c % a > 35 else '0123456789abcdefghijklmnopqrstuvwxyz'[c % a])

    d = {}
    for i in range(c):
        d[e_func(i)] = k[i] if i < len(k) and k[i] else e_func(i)

    def sub_func(match):
        w = match.group(0)
        return d.get(w, w)

    return re.sub(r'\b\w+\b', sub_func, p)


def resolve_mixdrop(url: str) -> Optional[str]:
    """Resolves Mixdrop links to direct MP4 URL."""
    try:
        code_match = re.search(r'/(?:f|e)/([a-zA-Z0-9]+)', url)
        if not code_match:
            return None
        code = code_match.group(1)
        domain = "mxdrop.click" if "mxdrop" in url else "mixdrop.ps"
        embed_url = f"https://{domain}/e/{code}"

        status, html, _ = http_get(embed_url, referer="https://akwams.org/")
        if status != 200 or not html:
            embed_url = f"https://mxdrop.click/e/{code}"
            status, html, _ = http_get(embed_url, referer="https://akwams.org/")

        # Look for packed javascript
        packed_match = re.search(r'eval\(function\(p,a,c,k,e,d\)\{.*?return p\}\s*\(\s*([\'"].*?[\'"])\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\'"].*?[\'"])\.split\([\'"]\|[\'"]\)', html, re.DOTALL)
        if packed_match:
            p = packed_match.group(1)[1:-1]
            a = int(packed_match.group(2))
            c = int(packed_match.group(3))
            k = packed_match.group(4)[1:-1].split('|')
            unpacked = unpack_dean_edwards(p, a, c, k, 0, {})
            wurl_match = re.search(r'wurl\s*=\s*["\']([^"\']+)["\']', unpacked)
            if wurl_match:
                direct = wurl_match.group(1)
                if direct.startswith("//"):
                    direct = "https:" + direct
                logger.info(f"Successfully resolved Mixdrop to direct MP4: {direct[:60]}...")
                return direct

        return None
    except Exception as e:
        logger.warning(f"Failed to resolve Mixdrop link {url}: {e}")
        return None


def resolve_stream_to_direct_url(url: str) -> str:
    """
    Main entry point: Inspects any stream candidate URL.
    If it's a known cyberlocker/host, resolves it to the real direct video stream.
    Otherwise, returns the URL as-is (e.g. direct mp4 or m3u8).
    """
    u_lower = url.lower()

    if any(k in u_lower for k in ["doodstream", "playmogo", "dood.to", "dood.so", "dood.ws", "dood.cx", "dsvplay"]):
        resolved = resolve_doodstream(url)
        if resolved:
            return resolved

    if any(k in u_lower for k in ["streamtape", "streamta.pe", "strtape"]):
        resolved = resolve_streamtape(url)
        if resolved:
            return resolved

    if any(k in u_lower for k in ["mixdrop", "mxdrop"]):
        resolved = resolve_mixdrop(url)
        if resolved:
            return resolved

    return url
