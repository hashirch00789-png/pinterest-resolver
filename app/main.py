import asyncio
import json
import os
import re
import time
from collections import deque
from html import unescape
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from playwright.async_api import async_playwright

APP_NAME = "Pinterest Media Resolver"
VERSION = "1.0.0"

ALLOWED_PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com",
    "pin.it",
    "pinterest.co.uk", "www.pinterest.co.uk",
    "pinterest.ca", "www.pinterest.ca",
    "pinterest.de", "www.pinterest.de",
    "pinterest.fr", "www.pinterest.fr",
    "pinterest.es", "www.pinterest.es",
    "pinterest.it", "www.pinterest.it",
    "pinterest.com.au", "www.pinterest.com.au",
    "pinterest.jp", "www.pinterest.jp",
}
ALLOWED_CDN_HOSTS = {
    "i.pinimg.com", "v1.pinimg.com", "v2.pinimg.com",
    "v3.pinimg.com", "v.pinimg.com",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# Simple per-process rate limit. Put a real shared limiter in front of this
# service (Cloudflare/Upstash/etc.) for multi-instance production deployments.
RATE_WINDOW = 60
RATE_MAX = 30
_rate_hits: dict[str, deque[float]] = {}


class ResolveBody(BaseModel):
    url: HttpUrl


def clean_url(value: str) -> str:
    return value.replace("\\/", "/")


def host_of(value: str) -> str:
    try:
        return (urlparse(value).hostname or "").lower()
    except Exception:
        return ""


def is_allowed_pinterest_url(value: str) -> bool:
    host = host_of(value)
    return host in ALLOWED_PIN_HOSTS


def is_allowed_media_url(value: str) -> bool:
    host = host_of(value)
    return host in ALLOWED_CDN_HOSTS and value.lower().startswith(("https://", "http://"))


def normalize_pin_url(value: str) -> str:
    p = urlparse(value)
    return urlunparse(("https", p.netloc.lower(), p.path, "", "", ""))


def looks_like_mp4(value: str) -> bool:
    v = clean_url(value).lower()
    return "pinimg.com/videos/" in v and (
        ".mp4" in v or "/mp4/" in v or "720p" in v or "1080p" in v
    ) and ".m3u8" not in v


def looks_like_hls(value: str) -> bool:
    v = clean_url(value).lower()
    return ".m3u8" in v or "hls" in v and "pinimg.com/videos" in v


def quality_score(url: str) -> tuple:
    v = url.lower()
    score = 0
    # Prefer standard progressive MP4s.
    if ".mp4" in v:
        score += 1000
    if "720p" in v or "v_720p" in v or "_720w" in v:
        score += 700
    if "1080" in v:
        score += 900
    if "480" in v:
        score += 450
    if "360" in v:
        score += 300
    if "hls" in v or ".m3u8" in v:
        score -= 10000
    # Avoid mobile/HEVC ladders where a normal MP4 is available.
    if "mobile" in v or "hevc" in v:
        score -= 100
    return (score, len(v))


def balanced_json_strings(text: str) -> list[str]:
    """Extract JSON objects from Relay assignment scripts without the old
    non-greedy-regex bug that stops at the first nested closing brace."""
    found: list[str] = []
    markers = [
        "__PWS_RELAY_REGISTER_COMPLETED_REQUEST__",
        "__PWS_RELAY_REGISTER_FALBACK_REQUEST__",
        "__PWS_RELAY_REGISTER_REQUEST__",
    ]
    for marker in markers:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            brace = text.find("{", idx)
            if brace < 0:
                break
            depth = 0
            in_string = False
            escaped = False
            end = None
            for i in range(brace, min(len(text), brace + 2_500_000)):
                ch = text[i]
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
                        end = i + 1
                        break
            if end:
                found.append(text[brace:end])
                start = end
            else:
                break
    return found


def walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def collect_urls_from_obj(obj: Any) -> list[str]:
    urls: list[str] = []
    for node in walk(obj):
        for key, value in node.items():
            if not isinstance(value, str):
                continue
            value = clean_url(unescape(value))
            if "pinimg.com/videos/" in value:
                urls.append(value)
    return urls


def extract_media(html: str, network_urls: list[str]) -> dict:
    video_candidates: set[str] = set()
    image_candidates: set[str] = set()
    poster_candidates: set[str] = set()

    # 1. Network-observed video requests. This catches videos rendered after
    # initial HTML/SSR and avoids downloading the actual video in Playwright.
    for u in network_urls:
        if looks_like_mp4(u):
            video_candidates.add(u)

    # 2. JSON-LD VideoObject / contentUrl.
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in walk(data):
            for key in ("contentUrl", "embedUrl", "url"):
                value = node.get(key)
                if isinstance(value, str):
                    value = clean_url(unescape(value))
                    if looks_like_mp4(value):
                        video_candidates.add(value)
            thumb = node.get("thumbnailUrl")
            if isinstance(thumb, str) and "pinimg.com" in thumb:
                poster_candidates.add(thumb)

    # 3. Relay / SSR JSON.
    for raw in balanced_json_strings(html):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for u in collect_urls_from_obj(data):
            if looks_like_mp4(u):
                video_candidates.add(u)

    # 4. Direct CDN regex fallback.
    patterns = [
        r'https?://v\d?\.pinimg\.com/videos/[^"\'<>\s\\]+',
        r'https?:\\/\\/v\d?\.pinimg\.com\\/videos\\/[^"\'<>\s]+',
    ]
    for pattern in patterns:
        for m in re.findall(pattern, html):
            u = clean_url(unescape(m)).rstrip("\\")
            if looks_like_mp4(u):
                video_candidates.add(u)

    # 5. Explicit video tags / source tags.
    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-video-url"):
            value = tag.get(attr)
            if isinstance(value, str) and looks_like_mp4(value):
                video_candidates.add(value)

    # Images / poster fallback.
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        content = tag.get("content")
        if not isinstance(content, str):
            continue
        content = clean_url(unescape(content))
        if "og:image" in prop and "pinimg.com" in content:
            poster_candidates.add(content)
        if "og:video" in prop and looks_like_mp4(content):
            video_candidates.add(content)

    for tag in soup.find_all("img"):
        for attr in ("src", "data-src"):
            value = tag.get(attr)
            if isinstance(value, str) and "pinimg.com" in value:
                image_candidates.add(clean_url(unescape(value)))

    videos = sorted(
        [u for u in video_candidates if is_allowed_media_url(u)],
        key=quality_score,
        reverse=True,
    )
    images = [u for u in image_candidates if is_allowed_media_url(u)]
    posters = [u for u in poster_candidates if is_allowed_media_url(u)]

    return {
        "videos": list(dict.fromkeys(videos))[:12],
        "images": list(dict.fromkeys(images))[:20],
        "posters": list(dict.fromkeys(posters))[:10],
    }


async def resolve_short_url(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(
        url,
        follow_redirects=True,
        timeout=20,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    final = str(r.url)
    if not is_allowed_pinterest_url(final):
        raise ValueError("The URL did not resolve to Pinterest.")
    return final


async def browser_extract(url: str) -> tuple[str, dict]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = await context.new_page()
        network_urls: list[str] = []

        async def on_request(req):
            u = req.url
            if "pinimg.com/videos/" in u:
                network_urls.append(u)
                # Do not allow the resolver browser to consume the actual
                # media. We only need the URL.
                try:
                    await req.abort()
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            await page.wait_for_timeout(1800)
            html = await page.content()
            final_url = page.url

            # Give Pinterest a little more time if the initial document did
            # not expose a video.
            if "pinimg.com/videos/" not in html and not network_urls:
                await page.wait_for_timeout(1800)
                html = await page.content()
                final_url = page.url

            # Browser-side visible media sources.
            try:
                dom_urls = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('video,source'))
                       .map(x => x.currentSrc || x.src || x.getAttribute('data-src'))
                       .filter(Boolean)"""
                )
                network_urls.extend(dom_urls or [])
            except Exception:
                pass

            media = extract_media(html, network_urls)
            return final_url, media
        finally:
            await browser.close()


async def http_extract(url: str) -> tuple[str, dict]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
    }
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, headers=headers
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        final = str(r.url)
        if not is_allowed_pinterest_url(final):
            raise ValueError("Pinterest redirect validation failed.")
        return final, extract_media(r.text, [])


async def resolve(url: str) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as client:
        canonical = await resolve_short_url(client, url) if host_of(url) == "pin.it" else url

    # Fast server-side extraction first.
    errors = []
    try:
        final, media = await http_extract(canonical)
        if media["videos"]:
            return build_result(final, media)
    except Exception as exc:
        errors.append("http:" + str(exc))

    # Browser fallback is the important reliability layer.
    try:
        final, media = await browser_extract(canonical)
        if media["videos"]:
            return build_result(final, media)
        # Images are only returned after all video strategies have failed.
        if media["images"] or media["posters"]:
            return build_result(final, media)
    except Exception as exc:
        errors.append("browser:" + str(exc))

    raise RuntimeError(
        "No public downloadable media was detected."
        + (" " + " | ".join(errors[-2:]) if errors else "")
    )


def build_result(final_url: str, media: dict) -> dict:
    if media["videos"]:
        video = media["videos"][0]
        poster = media["posters"][0] if media["posters"] else (
            media["images"][0] if media["images"] else None
        )
        return {
            "success": True,
            "type": "video",
            "pin_url": final_url,
            "media": [{
                "type": "video",
                "format": "mp4",
                "quality": quality_label(video),
                "url": video,
                "download_url": "/api/download?url=" + video,
                "thumbnail": poster,
            }],
            "message": "Pinterest video found.",
        }

    images = media["images"] or media["posters"]
    return {
        "success": True,
        "type": "image",
        "pin_url": final_url,
        "media": [{
            "type": "image",
            "format": image_format(images[0]),
            "quality": "Original / best available",
            "url": images[0],
            "thumbnail": images[0],
        }] if images else [],
        "message": "Pinterest image media found.",
    }


def quality_label(url: str) -> str:
    v = url.lower()
    if "1080" in v:
        return "1080p"
    if "720" in v or "720w" in v:
        return "720p"
    if "480" in v:
        return "480p"
    if "360" in v:
        return "360p"
    return "MP4"


def image_format(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", url, re.I)
    return (m.group(1) if m else "jpg").upper()


def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _rate_hits.setdefault(ip, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        raise HTTPException(429, "Too many requests. Please try again later.")
    q.append(now)


app = FastAPI(title=APP_NAME, version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": APP_NAME,
        "version": VERSION,
        "status": "ok",
        "endpoint": "/api/resolve",
        "download": "/api/download?url=<pinimg-url>",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


@app.post("/api/resolve")
async def api_resolve(body: ResolveBody, request: Request):
    rate_limit(request)
    url = str(body.url)
    if not is_allowed_pinterest_url(url):
        raise HTTPException(400, "Only public Pinterest and pin.it URLs are supported.")

    try:
        result = await asyncio.wait_for(resolve(url), timeout=55)
        return JSONResponse(result)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Pinterest took too long to respond. Please try again.")
    except Exception as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/download")
async def api_download(url: str, request: Request):
    rate_limit(request)
    if not is_allowed_media_url(url):
        raise HTTPException(400, "Only Pinterest CDN media URLs are allowed.")

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://www.pinterest.com/",
        "Range": request.headers.get("range", ""),
    }
    # Remove an empty Range header.
    headers = {k: v for k, v in headers.items() if v}

    async def iterator():
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=None, headers=headers
        ) as client:
            async with client.stream("GET", url) as upstream:
                if upstream.status_code >= 400:
                    return
                async for chunk in upstream.aiter_bytes(1024 * 1024):
                    yield chunk

    # We inspect the CDN response once for status/headers, then stream it
    # through a fresh request so the HTTP client remains open for the entire
    # response lifetime.
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20, headers=headers
    ) as probe_client:
        probe = await probe_client.head(url)
        if probe.status_code >= 400 or probe.status_code == 405:
            # Some CDNs reject HEAD; allow GET streaming to determine status.
            probe = await probe_client.get(url, headers={**headers, "Range": "bytes=0-0"})
        if probe.status_code >= 400:
            raise HTTPException(probe.status_code, "Pinterest CDN refused the media request.")
        content_type = probe.headers.get("content-type", "video/mp4")
        content_length = probe.headers.get("content-length")
        accept_ranges = probe.headers.get("accept-ranges", "bytes")

    response_headers = {
        "Content-Type": content_type,
        "Content-Disposition": 'attachment; filename="pinterest-video.mp4"',
        "Cache-Control": "private, max-age=300",
        "Accept-Ranges": accept_ranges,
    }
    if content_length:
        response_headers["Content-Length"] = content_length

    return StreamingResponse(
        iterator(),
        status_code=200,
        headers=response_headers,
        media_type=content_type,
    )
