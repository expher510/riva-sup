"""
Universal Media Stream Resolver for Riva Transit Pipeline.
Resolves cyberlocker/host URLs (DoodStream, Playmogo, StreamHG/HGCloud, StreamWish,
VidHide, Streamtape, Mixdrop, Uqload, etc.) into direct MP4/HLS downloadable stream URLs
along with the exact required Referer header.
"""
import re
import time
import random
import string
import logging
import http.cookiejar
import urllib.request
import urllib.error
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin

try:
    from curl_cffi import requests as crequests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger("media_resolver")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _session_get(
    url: str,
    referer: Optional[str] = None,
    timeout: int = 12,
    session_obj=None,
) -> Tuple[int, str, dict, str, object]:
    """
    Cookie-preserving HTTP GET that tracks final redirected URL.
    Returns: (status_code, body_text, headers_dict, final_url, session_obj)
    """
    if HAS_CURL_CFFI:
        cffi_headers = {}
        if referer:
            cffi_headers["Referer"] = referer
            if ".m3u8" in (url or "").lower():
                p_ref = urlparse(referer)
                if p_ref.scheme and p_ref.netloc:
                    cffi_headers["Origin"] = f"{p_ref.scheme}://{p_ref.netloc}"
        sess = session_obj or crequests.Session(impersonate="chrome120")
        resp = sess.get(url, headers=cffi_headers, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.text, dict(resp.headers), str(resp.url), sess
    else:
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
            if ".m3u8" in (url or "").lower():
                p_ref = urlparse(referer)
                if p_ref.scheme and p_ref.netloc:
                    headers["Origin"] = f"{p_ref.scheme}://{p_ref.netloc}"
        opener = session_obj
        if opener is None:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body, dict(resp.headers), resp.geturl(), opener
        except urllib.error.HTTPError as he:
            return he.code, "", {}, url, opener


def http_get(url: str, referer: Optional[str] = None, timeout: int = 15) -> Tuple[int, str, dict]:
    """Helper to perform HTTP GET with browser impersonation (backward compatible)."""
    status, text, hdrs, _, _ = _session_get(url, referer=referer, timeout=timeout)
    return status, text, hdrs


def unpack_dean_edwards(p: str, a: int, c: int, k: list, e=0, d=None) -> str:
    """Unpacks Dean Edwards p,a,c,k,e,d JavaScript packer."""
    def e_func(num: int) -> str:
        first = "" if num < a else e_func(int(num / a))
        rem = num % a
        second = chr(rem + 29) if rem > 35 else "0123456789abcdefghijklmnopqrstuvwxyz"[rem]
        return first + second

    sym_map = {}
    for i in range(c):
        key = e_func(i)
        sym_map[key] = k[i] if i < len(k) and k[i] else key

    return re.sub(r"\b\w+\b", lambda m: sym_map.get(m.group(0), m.group(0)), p)


def _extract_unpacked_js(html: str) -> Optional[str]:
    """Finds and unpacks p,a,c,k,e,d packed JS blocks in an embed page."""
    if not html or "eval(function(p,a,c,k,e,d)" not in html:
        return None
    packed_match = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?return p\}\s*\(\s*(['\"].*?['\"])\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(['\"].*?['\"])\.split\(['\"]\|['\"]\)",
        html,
        re.DOTALL,
    )
    if not packed_match:
        return None
    p = packed_match.group(1)[1:-1]
    a = int(packed_match.group(2))
    c = int(packed_match.group(3))
    k = packed_match.group(4)[1:-1].split("|")
    return unpack_dean_edwards(p, a, c, k)


def _select_best_hls_variant(master_url: str, referer: str, session_obj=None) -> str:
    """
    Fetches an HLS master.m3u8 playlist and selects the optimal HD variant (720p/1080p <= 1.85 Mbps)
    so 2-hour Turkish episodes stay safely under Telegram Bot's 2.0 GB MTProto limit (~850 MB - 1.35 GB)
    with fast 2-4 MB segments instead of 3+ GB raw streams that exceed 2 GB or stall on 25 MB segments.
    """
    import os
    max_bw = int(os.getenv("MAX_HLS_BANDWIDTH", "1850000"))
    try:
        st, m3u8_text, _, final_master, _ = _session_get(
            master_url, referer=referer, timeout=14, session_obj=session_obj
        )
        if st != 200 or "#EXTM3U" not in m3u8_text:
            return master_url
        if "#EXT-X-STREAM-INF" not in m3u8_text:
            return final_master or master_url

        variants = []
        lines = [ln.strip() for ln in m3u8_text.splitlines() if ln.strip()]
        for idx, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                bw_m = re.search(r"BANDWIDTH=(\d+)", ln)
                bw = int(bw_m.group(1)) if bw_m else 0
                if idx + 1 < len(lines) and not lines[idx + 1].startswith("#"):
                    variants.append((bw, lines[idx + 1]))

        if variants:
            under_cap = [v for v in variants if v[0] <= max_bw]
            if under_cap:
                chosen_uri = max(under_cap, key=lambda x: x[0])[1]
            else:
                chosen_uri = min(variants, key=lambda x: x[0])[1]
            return urljoin(final_master or master_url, chosen_uri)
    except Exception as e:
        logger.debug(f"Could not parse HLS master variants for {master_url[:60]}: {e}")
    return master_url


def resolve_doodstream_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """
    Resolves DoodStream / Playmogo / MyVidPlay / D000d / Dsvplay links to direct MP4 URL.
    Preserves cookies and uses the final redirected domain for /pass_md5/ verification.
    """
    try:
        code_match = re.search(r"/(?:d|e)/([a-zA-Z0-9]+)", url)
        if not code_match:
            return None
        code = code_match.group(1)

        candidates = [
            f"https://playmogo.com/e/{code}",
            f"https://doodstream.com/e/{code}",
        ]
        orig_host = urlparse(url).netloc.lower()
        if orig_host and orig_host not in ("doodstream.com", "playmogo.com", "dsvplay.com"):
            candidates.insert(0, f"https://{orig_host}/e/{code}")

        html = ""
        final_embed = ""
        sess = None
        for cand_embed in candidates:
            try:
                st, body, _, fin_u, sess = _session_get(cand_embed, referer="https://akwams.org/", timeout=12)
                if st == 200 and body and "/pass_md5/" in body:
                    html = body
                    final_embed = fin_u or cand_embed
                    break
            except Exception:
                continue

        if not html:
            return None

        pass_md5_match = re.search(r"(/pass_md5/[a-zA-Z0-9\-_/]+)", html)
        if not pass_md5_match:
            return None

        pass_md5_path = pass_md5_match.group(1)
        host = urlparse(final_embed).netloc
        md5_url = f"https://{host}{pass_md5_path}"

        st_md5, prefix, _, _, _ = _session_get(md5_url, referer=final_embed, timeout=10, session_obj=sess)
        if st_md5 != 200 or not prefix:
            return None

        prefix = prefix.strip()
        if not prefix.startswith("http"):
            return None

        random_str = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        token = pass_md5_path.split("/")[-1]
        expiry = int(time.time() * 1000)

        final_direct_url = f"{prefix}{random_str}?token={token}&expiry={expiry}"
        referer = f"https://{host}/"
        logger.info(f"Resolved DoodStream/Playmogo -> {final_direct_url[:65]}... (ref={referer})")
        return final_direct_url, referer
    except Exception as e:
        logger.warning(f"Failed to resolve DoodStream link {url}: {e}")
        return None


def resolve_doodstream(url: str) -> Optional[str]:
    res = resolve_doodstream_with_referer(url)
    return res[0] if res else None


def resolve_packed_hls_embed_with_referer(url: str, default_referer: str = "https://cimalight.charity/") -> Optional[Tuple[str, str]]:
    """
    Resolves packed-JS / XVideoSharing HLS & MP4 embed hosts
    (1vid.xyz, morencius.com, liiivideo.com, cdnplus.sbs, vidoba.cyou, mp4plus.cyou, anafast.cyou, vidspeed.space)
    into the optimal HD variant .m3u8 playlist URL or direct .mp4 URL + required player Referer header.
    """
    try:
        u_low = (url or "").lower()
        if any(d in u_low for d in ("cdnplus.", "mp4plus.", "vidoba.", "anafast.", "vidspeed.")):
            default_referer = "https://w.anaplayer.online/"
        elif "liiivideo." in u_low:
            default_referer = "https://a.qfilm.tv/"
        elif "egybestvid." in u_low:
            default_referer = "https://egydead.ca/"

        st, html, _, fin_u, sess = _session_get(url, referer=default_referer, timeout=12)
        if st != 200 or not html:
            return None
        low = html.lower()
        if "file is no longer available" in low or "file was deleted" in low or "not found" in low[:600]:
            return None

        unpacked = _extract_unpacked_js(html)
        search_space = (unpacked or "") + "\n" + html
        fin_host = urlparse(fin_u or url).netloc
        referer = f"https://{fin_host}/"

        stream_candidate = None
        for key in ("hls2", "hls4", "file"):
            m_u = re.search(rf'["\']?{key}["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', search_space)
            if m_u:
                cand = m_u.group(1)
                if cand.startswith("http"):
                    stream_candidate = cand
                    break
                elif cand.startswith("/"):
                    stream_candidate = f"https://{fin_host}{cand}"
                    break

        if not stream_candidate:
            m_any = re.search(r'(https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*)', search_space)
            if m_any:
                stream_candidate = m_any.group(1)

        if stream_candidate:
            if ".m3u8" in stream_candidate:
                stream_candidate = _select_best_hls_variant(stream_candidate, referer=referer, session_obj=sess)
            logger.info(f"Resolved Packed HLS ({fin_host}) -> {stream_candidate[:65]}...")
            return stream_candidate, referer

        # Direct MP4 sources fallback (e.g. MP4Plus {file:"https://.../v.mp4",label:"480p"})
        labeled_mp4s = []
        for m_obj in re.finditer(r'\{[^{}]*file\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\'][^{}]*\}', search_space):
            block = m_obj.group(0)
            mp4_url = m_obj.group(1)
            lbl_m = re.search(r'label\s*:\s*["\']?(\d{3,4})p', block, re.I)
            res_score = int(lbl_m.group(1)) if lbl_m else 480
            labeled_mp4s.append((res_score, mp4_url))
        if labeled_mp4s:
            labeled_mp4s.sort(key=lambda x: x[0], reverse=True)
            best_mp4 = labeled_mp4s[0][1]
            logger.info(f"Resolved XVideoSharing MP4 ({fin_host}) -> {best_mp4[:65]}...")
            return best_mp4, referer

        m_mp4_any = re.search(r'(https?://[^"\'\s<>]+\.mp4[^"\'\s<>]*)', search_space)
        if m_mp4_any:
            mp4_u = m_mp4_any.group(1)
            logger.info(f"Resolved Direct MP4 ({fin_host}) -> {mp4_u[:65]}...")
            return mp4_u, referer

        return None
    except Exception as e:
        logger.warning(f"Failed to resolve packed HLS/MP4 embed {url}: {e}")
        return None


def resolve_hgcloud_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """
    Resolves HGCloud / StreamHG / Vibuxer / StreamWish / VidHide / FileLions / Morencius links
    into the highest-bitrate HD HLS (.m3u8) or MP4 direct stream.
    Handles /embed/{code}, /e/{code}, /d/{code}, and bare /{code} URLs.
    """
    try:
        code_match = re.search(r"/(?:embed|e|d|f|v)/([a-zA-Z0-9]{8,24})", url)
        if not code_match:
            code_match = re.search(r"https?://[^/]+/([a-zA-Z0-9]{10,24})(?:[/?#]|$)", url)
        if not code_match:
            return None
        code = code_match.group(1)

        # Active unpackable mirrors for StreamHG / HGCloud / StreamWish / VidHide
        mirrors = ["vibuxer.com", "hanerix.com", "streamwish.to", "byseraguci.com"]
        orig_host = urlparse(url).netloc.lower().replace("www.", "")
        candidate_embeds = []
        if "/embed/" in url.lower():
            candidate_embeds.append(url)
        if orig_host and orig_host not in ("hgcloud.to", "dhcplay.com", "vidhidevip.com") and orig_host not in mirrors:
            candidate_embeds.append(f"https://{orig_host}/e/{code}")
        for dom in mirrors:
            candidate_embeds.append(f"https://{dom}/e/{code}")

        for embed_url in candidate_embeds:
            dom = urlparse(embed_url).netloc
            try:
                st, html, _, fin_u, sess = _session_get(embed_url, referer="https://akwams.org/", timeout=10)
                if st != 200 or not html:
                    continue
                low = html.lower()
                if "file is no longer available" in low or "file was deleted" in low or "not found" in low[:600]:
                    return None

                unpacked = _extract_unpacked_js(html)
                search_space = (unpacked or "") + "\n" + html

                fin_host = urlparse(fin_u or embed_url).netloc
                referer = f"https://{fin_host}/"

                hls_candidates = []
                for key in ("hls4", "hls2", "hls3", "file"):
                    for m_u in re.finditer(rf'["\']?{key}["\']?\s*:\s*["\']([^"\']+)["\']', search_space):
                        cand = m_u.group(1).strip()
                        if not (".m3u8" in cand.lower() or "/stream/" in cand.lower()):
                            continue
                        if any(ext in cand.lower() for ext in (".jpg", ".png", ".vtt", ".srt", "get_slides")):
                            continue
                        if cand.startswith("/"):
                            cand = f"https://{fin_host}{cand}"
                        if cand.startswith("http") and cand not in hls_candidates:
                            hls_candidates.append(cand)

                for cand_hls in hls_candidates:
                    try:
                        st_m, body_m, _, fin_m, _ = _session_get(cand_hls, referer=referer, timeout=8, session_obj=sess)
                        if st_m == 200 and "#EXTM3U" in (body_m or ""):
                            selected = _select_best_hls_variant(fin_m or cand_hls, referer=referer, session_obj=sess)
                            logger.info(f"Resolved HGCloud/StreamWish ({dom}) -> {selected[:65]}...")
                            return selected, referer
                    except Exception:
                        continue

                m_mp4 = re.search(r'["\']?(?:file|mp4)["\']?\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', search_space)
                if m_mp4:
                    mp4_u = m_mp4.group(1)
                    logger.info(f"Resolved HGCloud/StreamWish MP4 ({dom}) -> {mp4_u[:65]}...")
                    return mp4_u, referer
            except Exception:
                continue

        return None
    except Exception as e:
        logger.warning(f"Failed to resolve HGCloud/StreamWish link {url}: {e}")
        return None


def resolve_mixdrop_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """Resolves Mixdrop links to direct MP4 URL along with matching domain Referer."""
    try:
        code_match = re.search(r"/(?:f|e)/([a-zA-Z0-9]+)", url)
        if not code_match:
            return None
        code = code_match.group(1)

        for domain in ["mxdrop.click", "mixdrop.ps", "mixdrop.ag", "mixdrop.co", "mixdrop.to"]:
            embed_url = f"https://{domain}/e/{code}"
            status, resp_html, _, fin_u, _ = _session_get(embed_url, referer="https://akwams.org/", timeout=10)
            if status != 200 or not resp_html:
                continue
            low = resp_html.lower()
            if "can't find the video" in low or "we are sorry" in low:
                return None
            unpacked = _extract_unpacked_js(resp_html)
            if unpacked:
                wurl_match = re.search(r'wurl\s*=\s*["\']([^"\']+)["\']', unpacked)
                if wurl_match:
                    direct = wurl_match.group(1)
                    if direct.startswith("//"):
                        direct = "https:" + direct
                    fin_host = urlparse(fin_u or embed_url).netloc or domain
                    referer = f"https://{fin_host}/"
                    logger.info(f"Resolved Mixdrop ({fin_host}) -> {direct[:65]}...")
                    return direct, referer

        return None
    except Exception as e:
        logger.warning(f"Failed to resolve Mixdrop link {url}: {e}")
        return None


def resolve_mixdrop(url: str) -> Optional[str]:
    res = resolve_mixdrop_with_referer(url)
    return res[0] if res else None


def resolve_streamtape_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """Resolves Streamtape links to direct MP4 URL."""
    try:
        code_match = re.search(r"/(?:e|v)/([a-zA-Z0-9]+)", url)
        if not code_match:
            return None
        code = code_match.group(1)
        embed_url = f"https://streamtape.com/e/{code}"

        status, html, _ = http_get(embed_url, timeout=10)
        if status != 200 or not html:
            return None

        m = re.search(
            r"document\.getElementById\(['\"](?:robotlink|ideolink)['\"]\)\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]",
            html,
        )
        if not m:
            m = re.search(
                r"document\.getElementById\(['\"](?:robotlink|ideolink)['\"]\)\.innerHTML\s*=\s*(.*?);",
                html,
            )
            if m:
                tokens = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
                link = "".join(tokens)
            else:
                return None
        else:
            link = m.group(1)

        if link.startswith("//"):
            link = "https:" + link
        if not link.startswith("http"):
            return None
        logger.info(f"Resolved Streamtape -> {link[:65]}...")
        return link, "https://streamtape.com/"
    except Exception as e:
        logger.warning(f"Failed to resolve Streamtape link {url}: {e}")
        return None


def resolve_streamtape(url: str) -> Optional[str]:
    res = resolve_streamtape_with_referer(url)
    return res[0] if res else None


def resolve_uqload_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """Resolves Uqload links to direct MP4 URL."""
    try:
        m = re.search(r"uqload\.[a-z]+/(?:embed-)?([a-zA-Z0-9]+)", url)
        if not m:
            return None
        code = m.group(1)
        host = urlparse(url).netloc or "uqload.vc"
        embed_url = f"https://{host}/embed-{code}.html"
        st, html, _, fin_u, _ = _session_get(embed_url, referer="https://akwams.org/", timeout=10)
        if st != 200 or not html:
            return None
        mp4_m = re.search(r'sources\s*:\s*\[\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', html)
        if mp4_m:
            fin_host = urlparse(fin_u or embed_url).netloc or host
            return mp4_m.group(1), f"https://{fin_host}/"
        return None
    except Exception:
        return None


def resolve_qfilm_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """Resolves qfilm.tv watch.php / play.php / embed.php pages to their underlying stream."""
    try:
        m = re.search(r"vid=([a-zA-Z0-9]+)", url or "")
        if not m:
            return None
        vid = m.group(1)
        host = urlparse(url).netloc or "a.qfilm.tv"
        play_u = f"https://{host}/play.php?vid={vid}"
        watch_u = f"https://{host}/watch.php?vid={vid}"
        st, html, _, _, _ = _session_get(play_u, referer=watch_u, timeout=14)
        if st != 200 or not html:
            st, html, _, _, _ = _session_get(f"https://{host}/embed.php?vid={vid}", referer=play_u, timeout=14)
        if st != 200 or not html:
            return None

        cands = re.findall(r'data-embed-url=["\']([^"\']+)["\']', html, re.I)
        cands += re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
        for cu in cands:
            cu = cu.strip()
            if cu.startswith("//"):
                cu = "https:" + cu
            if not cu.startswith("http") or "qfilm.tv" in cu.lower():
                continue
            direct, ref = resolve_stream_with_headers(cu)
            if direct and direct != cu:
                return direct, ref
        return None
    except Exception as e:
        logger.warning(f"Failed resolving qfilm page {url}: {e}")
        return None


def resolve_okru_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """
    Resolves Odnoklassniki (ok.ru/video/<id> or ok.ru/videoembed/<id>) into a direct MP4 or HLS stream URL.
    """
    import html as _html
    import json as _json
    try:
        m_id = re.search(r"ok\.ru/(?:video|videoembed)/(\d+)", url or "")
        if not m_id:
            return None
        vid_id = m_id.group(1)
        embed_url = f"https://ok.ru/videoembed/{vid_id}"
        st, body, _, _, _ = _session_get(embed_url, referer="https://ok.ru/", timeout=14)
        if st != 200 or not body:
            return None
        m_opts = re.search(r'data-options=(["\'])(.*?)\1', body, re.DOTALL)
        if not m_opts:
            return None
        opts = _json.loads(_html.unescape(m_opts.group(2)))
        meta_raw = (opts.get("flashvars") or {}).get("metadata") or {}
        meta = meta_raw if isinstance(meta_raw, dict) else _json.loads(meta_raw)

        vids = meta.get("videos") or []
        by_name = {v.get("name"): v.get("url") for v in vids if v.get("name") and v.get("url")}
        for q_pref in ("hd", "sd", "full", "low", "lowest", "mobile"):
            if by_name.get(q_pref):
                return by_name[q_pref], embed_url

        hls = meta.get("ondemandHls") or meta.get("hlsManifestUrl")
        if hls:
            return hls, embed_url
        return None
    except Exception as e:
        logger.warning(f"Failed resolving ok.ru video {url}: {e}")
        return None


def resolve_ytdlp_with_referer(url: str) -> Optional[Tuple[str, str]]:
    """
    Uses yt-dlp metadata extraction to resolve VK Video (vkvideo.ru / vk.ru / vk.com)
    and Dailymotion URLs into a playable stream URL + Referer.
    """
    import json as _json
    import shutil as _shutil
    import subprocess as _sp
    if not _shutil.which("yt-dlp"):
        return None
    try:
        res = _sp.run(
            [
                "yt-dlp",
                "--simulate",
                "--no-warnings",
                "-f",
                "best[height<=720][ext=mp4]/best[height<=720]/best",
                "-J",
                url,
            ],
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
            timeout=20,
        )
        if res.returncode != 0:
            return None
        info = _json.loads(res.stdout.decode("utf-8", errors="replace"))
        direct = info.get("url")
        if not direct and info.get("requested_formats"):
            direct = info["requested_formats"][0].get("url")
        if not direct and info.get("formats"):
            direct = info["formats"][-1].get("url")
        if direct and direct.startswith("http"):
            hdrs = info.get("http_headers") or {}
            ref = hdrs.get("Referer") or f"https://{urlparse(url).netloc}/"
            return direct, ref
        return None
    except Exception as e:
        logger.warning(f"yt-dlp resolve failed for {url}: {e}")
        return None


def resolve_stream_with_headers(url: str) -> Tuple[str, str]:
    """
    Main resolver returning (direct_stream_url, required_referer).
    """
    u_lower = (url or "").lower()

    if "ok.ru/" in u_lower:
        res = resolve_okru_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["vkvideo.ru", "vk.ru/video", "vk.com/video", "dailymotion.com"]):
        res = resolve_ytdlp_with_referer(url)
        if res:
            return res

    if "qfilm.tv" in u_lower:
        res = resolve_qfilm_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["1vid.", "morencius.", "liiivideo.", "cdnplus.", "mp4plus.", "vidoba.", "anafast.", "vidspeed.", "vidspeeds.", "egybestvid."]):
        res = resolve_packed_hls_embed_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["hgcloud", "hglink", "streamhg", "vibuxer", "hanerix", "dhcplay", "streamwish", "vidhide", "byseraguci", "filelions"]):
        res = resolve_hgcloud_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["doodstream", "playmogo", "dood.", "d000d", "myvidplay", "dsvplay", "vide0"]):
        res = resolve_doodstream_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["mixdrop", "mxdrop"]):
        res = resolve_mixdrop_with_referer(url)
        if res:
            return res

    if any(k in u_lower for k in ["streamtape", "streamta.pe", "strtape"]):
        res = resolve_streamtape_with_referer(url)
        if res:
            return res

    if "uqload." in u_lower:
        res = resolve_uqload_with_referer(url)
        if res:
            return res

    # Fallback default referer if already a direct CDN link
    if "mxcontent" in u_lower:
        return url, "https://mxdrop.click/"
    if "cloudatacdn" in u_lower:
        return url, "https://playmogo.com/"
    if "premilkyway" in u_lower:
        return url, "https://vibuxer.com/"
    if "1vid.online" in u_lower:
        return url, "https://1vid.xyz/"
    if "acek-cdn" in u_lower:
        return url, "https://morencius.com/"
    if "brqz.online" in u_lower:
        return url, "https://cdnplus.sbs/"
    if "cdnz.quest" in u_lower:
        return url, "https://vidspeed.org/" if "vsped" in u_lower else "https://mp4plus.cyou/"
    if "egybestvid." in u_lower:
        return url, "https://egybestvid.com/"
    return url, "https://akwams.org/"


def resolve_stream_to_direct_url(url: str) -> str:
    """Backward-compatible wrapper returning only the direct stream URL."""
    direct, _ = resolve_stream_with_headers(url)
    return direct


def verify_stream_playable(url: str, timeout: int = 8) -> bool:
    """
    Resolves a candidate host link AND probes the resulting CDN edge (1KB Range for MP4,
    or #EXTM3U playlist check for HLS) so 502/404/HTML error edges are caught BEFORE queueing!
    """
    if not url or not url.startswith("http"):
        return False
    try:
        direct_url, referer = resolve_stream_with_headers(url)
        if not direct_url or not direct_url.startswith("http"):
            return False

        # If resolver returned the raw cyberlocker landing URL unchanged, it failed resolution
        if direct_url == url and not any(k in direct_url.lower() for k in [".mp4", ".m3u8", "cloudatacdn", "get_video", "mxcontent", "premilkyway", "1vid.online", "acek-cdn", "cdnz.quest", "brqz.online", "okcdn.ru", "vkuser"]):
            return False

        if ".m3u8" in direct_url.lower():
            # If _select_best_hls_variant already fetched master.m3u8 and selected an index variant, it is verified live!
            if "index-" in direct_url.lower() and "master.m3u8" not in direct_url.lower():
                return True
            st, body, _, _, _ = _session_get(direct_url, referer=referer, timeout=max(timeout, 12))
            return st == 200 and "#EXTM3U" in (body or "")

        # Direct MP4 / binary stream: probe first 1024 bytes to catch 502 Bad Gateway / 403 / HTML
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "*/*",
            "Referer": referer,
            "Range": "bytes=0-1023",
        }
        if HAS_CURL_CFFI:
            resp = crequests.get(direct_url, headers=headers, impersonate="chrome120", timeout=timeout, allow_redirects=True, stream=True)
            code = resp.status_code
            ctype = (resp.headers.get("content-type") or "").lower()
            resp.close()
            return code in (200, 206) and "text/html" not in ctype
        else:
            req = urllib.request.Request(direct_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                return resp.status in (200, 206) and "text/html" not in ctype
    except Exception as e:
        logger.debug(f"Playability probe failed for {url[:65]}: {e}")
        return False
