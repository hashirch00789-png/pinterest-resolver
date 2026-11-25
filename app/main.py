
import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from html import unescape
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "Pinterest Media Resolver"
VERSION = "1.4.0"

SHORT_URL_TIMEOUT = 8
DIRECT_TIMEOUT = 7
HTTP_CONNECT_TIMEOUT = 4

BROWSER_NAV_TIMEOUT = 18000
BROWSER_SETTLE_TIME = 650
BROWSER_EXTRA_WAIT = 500

TOTAL_RESOLVE_TIMEOUT = 38

MAX_CONCURRENT_RESOLVES = int(
    os.getenv("MAX_CONCURRENT_RESOLVES", "3")
)

MAX_CONCURRENT_BROWSERS = int(
    os.getenv("MAX_CONCURRENT_BROWSERS", "2")
)

RATE_WINDOW = 60
RATE_MAX = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

ALLOWED_PIN_HOSTS = {
    "pinterest.com",
    "www.pinterest.com",
    "pin.it",
    "pinterest.co.uk",
    "www.pinterest.co.uk",
    "pinterest.ca",
    "www.pinterest.ca",
    "pinterest.de",
    "www.pinterest.de",
    "pinterest.fr",
    "www.pinterest.fr",
    "pinterest.es",
    "www.pinterest.es",
    "pinterest.it",
    "www.pinterest.it",
    "pinterest.com.au",
    "www.pinterest.com.au",
    "pinterest.jp",
    "www.pinterest.jp",
}

ALLOWED_CDN_HOSTS = {
    "i.pinimg.com",
    "v1.pinimg.com",
    "v2.pinimg.com",
    "v3.pinimg.com",
    "v.pinimg.com",
}


# ============================================================
# GLOBAL RESOURCES
# ============================================================

_rate_hits = {}

resolve_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_RESOLVES
)

browser_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_BROWSERS
)

browser_lock = asyncio.Lock()

playwright_instance = None
shared_browser = None
shared_http_client = None
browser_startup_error = None


# ============================================================
# REQUEST MODEL
# ============================================================

class ResolveBody(BaseModel):
    url: HttpUrl


# ============================================================
# URL VALIDATION
# ============================================================

def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_allowed_pinterest_url(url: str) -> bool:
    return host_of(url) in ALLOWED_PIN_HOSTS


def is_allowed_media_url(url: str) -> bool:
    parsed = urlparse(url)

    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower()
        in ALLOWED_CDN_HOSTS
    )


def normalize_pin_url(url: str) -> str:
    parsed = urlparse(url)

    return urlunparse((
        "https",
        parsed.netloc.lower(),
        parsed.path,
        "",
        "",
        ""
    ))


def clean_url(value: str) -> str:
    return value.replace("\\/", "/")


def looks_like_mp4(url: str) -> bool:
    value = clean_url(url).lower()

    return (
        "pinimg.com/videos/" in value
        and (
            ".mp4" in value
            or "/mp4/" in value
            or "720p" in value
            or "1080p" in value
        )
        and ".m3u8" not in value
    )


# ============================================================
# QUALITY DETECTION
# ============================================================

def quality_score(url: str) -> tuple:
    value = url.lower()

    score = 0

    if ".mp4" in value:
        score += 1000

    if "720p" in value or "v_720p" in value:
        score += 700

    if "1080" in value:
        score += 900

    if "480" in value:
        score += 450

    if "360" in value:
        score += 300

    if ".m3u8" in value or "hls" in value:
        score -= 10000

    if "mobile" in value or "hevc" in value:
        score -= 100

    return score, len(value)


def quality_label(url: str) -> str:
    value = url.lower()

    if "2160" in value or "4k" in value:
        return "2160p (4K)"

    if "1440" in value:
        return "1440p"

    if "1080" in value:
        return "1080p (Full HD)"

    if "720" in value:
        return "720p (HD)"

    if "480" in value:
        return "480p"

    if "360" in value:
        return "360p"

    return "Best Available MP4"


def image_format(url: str) -> str:
    match = re.search(
        r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)",
        url,
        re.I
    )

    return (
        match.group(1).upper()
        if match
        else "JPG"
    )


# ============================================================
# PINTEREST JSON EXTRACTION
# ============================================================

def balanced_json_strings(text: str) -> list:
    found = []

    markers = [
        "__PWS_RELAY_REGISTER_COMPLETED_REQUEST__",
        "__PWS_RELAY_REGISTER_FALBACK_REQUEST__",
        "__PWS_RELAY_REGISTER_REQUEST__",
    ]

    for marker in markers:

        start = 0

        while True:

            index = text.find(marker, start)

            if index < 0:
                break

            brace = text.find("{", index)

            if brace < 0:
                break

            depth = 0
            in_string = False
            escaped = False
            end = None

            for i in range(
                brace,
                min(len(text), brace + 2500000)
            ):

                char = text[i]

                if in_string:

                    if escaped:
                        escaped = False

                    elif char == "\\":
                        escaped = True

                    elif char == '"':
                        in_string = False

                    continue

                if char == '"':
                    in_string = True

                elif char == "{":
                    depth += 1

                elif char == "}":
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

        for value in obj.values():
            yield from walk(value)

    elif isinstance(obj, list):

        for value in obj:
            yield from walk(value)


def collect_urls_from_obj(obj: Any) -> list:

    urls = []

    for node in walk(obj):

        for value in node.values():

            if not isinstance(value, str):
                continue

            value = clean_url(unescape(value))

            if "pinimg.com/videos/" in value:
                urls.append(value)

    return urls


# ============================================================
# MEDIA EXTRACTION
# ============================================================

def extract_media(
    html: str,
    network_urls: list
) -> dict:

    video_candidates = set()
    image_candidates = set()
    poster_candidates = set()

    # Network-observed videos

    for url in network_urls:

        if looks_like_mp4(url):
            video_candidates.add(url)

    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD extraction

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):

        try:

            data = json.loads(
                script.string or script.get_text()
            )

        except Exception:
            continue

        for node in walk(data):

            for key in (
                "contentUrl",
                "embedUrl",
                "url"
            ):

                value = node.get(key)

                if (
                    isinstance(value, str)
                    and looks_like_mp4(value)
                ):
                    video_candidates.add(value)

            thumb = node.get("thumbnailUrl")

            if (
                isinstance(thumb, str)
                and "pinimg.com" in thumb
            ):
                poster_candidates.add(thumb)

    # Pinterest Relay / SSR JSON

    for raw in balanced_json_strings(html):

        try:
            data = json.loads(raw)

        except Exception:
            continue

        for url in collect_urls_from_obj(data):

            if looks_like_mp4(url):
                video_candidates.add(url)

    # Regex extraction

    patterns = [
        r'https?://v\d?\.pinimg\.com/videos/[^"\'<>\s\\]+',
        r'https?:\\/\\/v\d?\.pinimg\.com\\/videos\\/[^"\'<>\s]+',
    ]

    for pattern in patterns:

        matches = re.findall(pattern, html)

        for match in matches:

            url = clean_url(
                unescape(match)
            ).rstrip("\\")

            if looks_like_mp4(url):
                video_candidates.add(url)

    # Video and source tags

    for tag in soup.find_all(["video", "source"]):

        for attr in (
            "src",
            "data-src",
            "data-video-url"
        ):

            value = tag.get(attr)

            if (
                isinstance(value, str)
                and looks_like_mp4(value)
            ):
                video_candidates.add(value)

    # Open Graph metadata

    for tag in soup.find_all("meta"):

        prop = (
            tag.get("property")
            or tag.get("name")
            or ""
        ).lower()

        content = tag.get("content")

        if not isinstance(content, str):
            continue

        content = clean_url(unescape(content))

        if (
            "og:image" in prop
            and "pinimg.com" in content
        ):
            poster_candidates.add(content)

        if (
            "og:video" in prop
            and looks_like_mp4(content)
        ):
            video_candidates.add(content)

    # Image extraction

    for tag in soup.find_all("img"):

        for attr in ("src", "data-src"):

            value = tag.get(attr)

            if (
                isinstance(value, str)
                and "pinimg.com" in value
            ):
                image_candidates.add(
                    clean_url(unescape(value))
                )

    # Validate CDN hosts

    videos = sorted(
        [
            url for url in video_candidates
            if is_allowed_media_url(url)
        ],
        key=quality_score,
        reverse=True
    )

    images = [
        url for url in image_candidates
        if is_allowed_media_url(url)
    ]

    posters = [
        url for url in poster_candidates
        if is_allowed_media_url(url)
    ]

    return {
        "videos": list(dict.fromkeys(videos))[:12],
        "images": list(dict.fromkeys(images))[:20],
        "posters": list(dict.fromkeys(posters))[:10],
    }


# ============================================================
# SHORT URL RESOLUTION
# ============================================================

async def resolve_short_url(
    client: httpx.AsyncClient,
    url: str
) -> str:

    response = await client.get(
        url,
        follow_redirects=True,
        timeout=httpx.Timeout(
            SHORT_URL_TIMEOUT,
            connect=HTTP_CONNECT_TIMEOUT
        ),
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    final_url = str(response.url)

    if not is_allowed_pinterest_url(final_url):
        raise ValueError(
            "The URL did not resolve to Pinterest."
        )

    return final_url


# ============================================================
# REUSABLE CHROMIUM
# ============================================================

async def get_browser():

    global playwright_instance
    global shared_browser
    global browser_startup_error

    async with browser_lock:
        if shared_browser is not None and shared_browser.is_connected():
            return shared_browser

        try:
            if playwright_instance is None:
                playwright_instance = await async_playwright().start()

            shared_browser = await playwright_instance.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-networking",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                ],
            )
            browser_startup_error = None
            print("[BROWSER] Chromium launched and ready")
            return shared_browser

        except Exception as exc:
            browser_startup_error = str(exc)
            print("[BROWSER] Chromium startup failed:", browser_startup_error)
            if playwright_instance is not None:
                try:
                    await playwright_instance.stop()
                except Exception:
                    pass
                playwright_instance = None
            shared_browser = None
            raise


async def close_browser():

    global playwright_instance
    global shared_browser

    async with browser_lock:
        if shared_browser is not None:
            try:
                await shared_browser.close()
            except Exception:
                pass
            shared_browser = None

        if playwright_instance is not None:
            try:
                await playwright_instance.stop()
            except Exception:
                pass
            playwright_instance = None

    print("[BROWSER] Chromium closed")


# ============================================================
# PLAYWRIGHT EXTRACTION
# ============================================================

async def browser_extract(url: str) -> tuple:

    async with browser_semaphore:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.pinterest.com/",
            },
        )
        page = await context.new_page()
        network_urls = []

        async def intercept(route):
            request = route.request
            request_url = request.url
            resource_type = request.resource_type

            if "pinimg.com/videos/" in request_url:
                network_urls.append(request_url)
                await route.abort()
                return

            if resource_type in {"image", "font", "media"}:
                await route.abort()
                return

            await route.continue_()

        try:
            await page.route("**/*", intercept)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_NAV_TIMEOUT,
            )
            await page.wait_for_timeout(BROWSER_SETTLE_TIME)
            html = await page.content()
            final_url = page.url

            if "pinimg.com/videos/" not in html and not network_urls:
                await page.wait_for_timeout(BROWSER_EXTRA_WAIT)
                html = await page.content()
                final_url = page.url

            try:
                dom_urls = await page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('video,source'))
                      .map(x => x.currentSrc || x.src || x.getAttribute('data-src'))
                      .filter(Boolean)
                    """
                )
                network_urls.extend(dom_urls or [])
            except Exception:
                pass

            return final_url, extract_media(html, network_urls)

        finally:
            await context.close()


# ============================================================
# DIRECT HTTP EXTRACTION
# ============================================================

async def http_extract(url: str, client: httpx.AsyncClient) -> tuple:

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
    }

    response = await client.get(
        url,
        headers=headers,
        timeout=httpx.Timeout(DIRECT_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
        follow_redirects=True,
    )
    response.raise_for_status()
    final_url = str(response.url)

    if not is_allowed_pinterest_url(final_url):
        raise ValueError("Pinterest redirect validation failed.")

    return final_url, extract_media(response.text, [])


# ============================================================
# BUILD API RESPONSE
# ============================================================

def build_result(
    final_url: str,
    media: dict
) -> dict:

    if media["videos"]:

        poster = (
            media["posters"][0]
            if media["posters"]
            else (
                media["images"][0]
                if media["images"]
                else None
            )
        )

        unique = []
        seen = set()

        for video in media["videos"]:

            if video in seen:
                continue

            seen.add(video)

            unique.append({
                "type": "video",
                "format": "mp4",
                "quality": quality_label(video),
                "url": video,
                "download_url": (
                    "/api/download?url=" + video
                ),
                "thumbnail": poster,
            })

        return {
            "success": True,
            "type": "video",
            "pin_url": final_url,
            "media": unique[:12],
            "message": (
                "Pinterest video found. "
                "Available MP4 options are listed below."
            ),
        }

    images = media["images"] or media["posters"]

    return {
        "success": True,
        "type": "image",
        "pin_url": final_url,
        "media": [
            {
                "type": "image",
                "format": image_format(images[0]),
                "quality": "Original / best available",
                "url": images[0],
                "thumbnail": images[0],
            }
        ] if images else [],
        "message": "Pinterest image media found.",
    }


# ============================================================
# MAIN RESOLUTION WORKFLOW
# ============================================================

async def resolve(url: str) -> dict:

    total_start = time.perf_counter()

    errors = []

    canonical = normalize_pin_url(url)

    # STEP 1: Resolve pin.it short URLs.

    if host_of(url) == "pin.it":

        stage_start = time.perf_counter()

        if shared_http_client is None:
            raise RuntimeError("HTTP client is not initialized.")

        canonical = await resolve_short_url(shared_http_client, url)

        print(
            "[TIMING] short_url:",
            round(time.perf_counter() - stage_start, 2),
            "seconds"
        )

    # STEP 2: Fast direct extraction.

    stage_start = time.perf_counter()

    try:

        if shared_http_client is None:
            raise RuntimeError("HTTP client is not initialized.")

        final_url, media = await http_extract(canonical, shared_http_client)

        print(
            "[TIMING] direct_http:",
            round(time.perf_counter() - stage_start, 2),
            "seconds"
        )

        if (
            media["videos"]
            or media["images"]
            or media["posters"]
        ):

            print("[SUCCESS] Direct HTTP extraction")

            return build_result(final_url, media)

    except Exception as exc:

        errors.append("HTTP: " + str(exc))

        print(
            "[HTTP EXTRACTION FAILED]",
            str(exc)
        )

    # STEP 3: Browser fallback only when necessary.

    stage_start = time.perf_counter()

    try:

        final_url, media = await browser_extract(
            canonical
        )

        print(
            "[TIMING] browser:",
            round(time.perf_counter() - stage_start, 2),
            "seconds"
        )

        if (
            media["videos"]
            or media["images"]
            or media["posters"]
        ):

            print("[SUCCESS] Browser extraction")

            return build_result(final_url, media)

    except Exception as exc:

        errors.append("Browser: " + str(exc))

        print(
            "[BROWSER EXTRACTION FAILED]",
            str(exc)
        )

    print(
        "[TIMING] TOTAL:",
        round(time.perf_counter() - total_start, 2),
        "seconds"
    )

    raise RuntimeError(
        "No public downloadable media was detected. "
        "The Pin may be private, unavailable, or Pinterest "
        "may not expose a direct MP4."
        + (
            " Details: " + " | ".join(errors[-2:])
            if errors else ""
        )
    )


# ============================================================
# RATE LIMITING
# ============================================================

def rate_limit(request: Request):

    ip = (
        request.client.host
        if request.client
        else "unknown"
    )

    now = time.time()

    hits = _rate_hits.setdefault(
        ip,
        deque()
    )

    while hits and now - hits[0] > RATE_WINDOW:
        hits.popleft()

    if len(hits) >= RATE_MAX:

        raise HTTPException(
            429,
            "Too many requests. Please wait and try again."
        )

    hits.append(now)


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    global shared_http_client
    global browser_startup_error

    print(f"[STARTUP] {APP_NAME} v{VERSION}")
    print("[CONFIG] Resolve concurrency:", MAX_CONCURRENT_RESOLVES)
    print("[CONFIG] Browser concurrency:", MAX_CONCURRENT_BROWSERS)

    shared_http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(DIRECT_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
        limits=httpx.Limits(
            max_connections=30,
            max_keepalive_connections=15,
            keepalive_expiry=30,
        ),
        headers={"User-Agent": UA},
    )
    app.state.http = shared_http_client

    # Eager startup makes Chromium status visible in /health immediately.
    try:
        await get_browser()
    except Exception as exc:
        browser_startup_error = str(exc)
        print("[STARTUP] Chromium unavailable; HTTP extraction remains enabled.")

    try:
        yield
    finally:
        await close_browser()
        if shared_http_client is not None:
            await shared_http_client.aclose()
            shared_http_client = None
        app.state.http = None
        print("[SHUTDOWN] Application stopped")


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    lifespan=lifespan
)


cors_origins = os.getenv(
    "CORS_ORIGINS",
    "*"
)

allowed_origins = [
    origin.strip().rstrip("/")
    for origin in cors_origins.split(",")
    if origin.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Accept",
        "Range"
    ],
    expose_headers=[
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Content-Disposition"
    ],
    max_age=86400
)


# ============================================================
# ROOT AND HEALTH ENDPOINTS
# ============================================================

@app.get("/")
async def root():

    return {
        "service": APP_NAME,
        "version": VERSION,
        "status": "ok",
        "endpoint": "/api/resolve",
        "download": "/api/download?url=<pinimg-url>"
    }


@app.get("/health")
async def health():

    chromium_ready = (
        shared_browser is not None
        and shared_browser.is_connected()
    )

    return {
        "status": "ok",
        "version": VERSION,
        "chromium": chromium_ready,
        "browser_error": (
            browser_startup_error
            if not chromium_ready and browser_startup_error
            else None
        ),
    }


@app.get("/api/cors-test")
async def cors_test():

    return {
        "success": True,
        "status": "ok",
        "cors": "enabled",
        "version": VERSION
    }


# ============================================================
# PINTEREST RESOLVE ENDPOINT
# ============================================================

@app.post("/api/resolve")
async def api_resolve(
    body: ResolveBody,
    request: Request
):

    rate_limit(request)

    url = str(body.url)

    if not is_allowed_pinterest_url(url):

        raise HTTPException(
            400,
            "Only public Pinterest and pin.it URLs are supported."
        )

    started = time.perf_counter()

    try:

        async with resolve_semaphore:

            result = await asyncio.wait_for(
                resolve(url),
                timeout=TOTAL_RESOLVE_TIMEOUT
            )

        print(
            "[REQUEST TOTAL]",
            round(time.perf_counter() - started, 2),
            "seconds | SUCCESS"
        )

        return JSONResponse(result)

    except asyncio.TimeoutError:

        print(
            "[REQUEST TIMEOUT]",
            round(time.perf_counter() - started, 2),
            "seconds"
        )

        raise HTTPException(
            504,
            "Pinterest took too long to respond. Please try again."
        )

    except HTTPException:
        raise

    except Exception as exc:

        print(
            "[REQUEST ERROR]",
            str(exc)
        )

        raise HTTPException(
            422,
            str(exc)
        )


# ============================================================
# STREAMING DOWNLOAD ENDPOINT
# ============================================================

@app.get("/api/download")
async def api_download(
    url: str,
    request: Request
):

    rate_limit(request)

    if not is_allowed_media_url(url):

        raise HTTPException(
            400,
            "Only HTTPS Pinterest CDN media URLs are allowed."
        )

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://www.pinterest.com/"
    }

    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    client = getattr(request.app.state, "http", None)
    if client is None:
        raise HTTPException(503, "Download service is starting. Please retry.")

    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                url,
                headers=headers,
                timeout=httpx.Timeout(60, connect=10),
            ),
            stream=True,
        )
        if not is_allowed_media_url(str(upstream.url)):
            await upstream.aclose()
            raise HTTPException(400, "Media redirect was not an allowed Pinterest CDN host.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            502,
            "Could not retrieve media from Pinterest CDN."
        ) from exc

    if upstream.status_code >= 400:

        status = upstream.status_code

        await upstream.aclose()

        raise HTTPException(
            status,
            "Pinterest CDN refused the media request."
        )

    async def stream():

        try:

            async for chunk in upstream.aiter_bytes(
                256 * 1024
            ):
                yield chunk

        finally:

            await upstream.aclose()

    content_type = upstream.headers.get(
        "content-type",
        "application/octet-stream"
    )

    filename = (
        "pinterest-image"
        if content_type.startswith("image/")
        else "pinterest-video.mp4"
    )

    response_headers = {
        "Content-Disposition": (
            f'attachment; filename="{filename}"'
        ),
        "Cache-Control": "private, max-age=300",
        "Accept-Ranges": upstream.headers.get(
            "accept-ranges",
            "bytes"
        )
    }

    for header in (
        "content-length",
        "content-range"
    ):

        if upstream.headers.get(header):

            response_headers[
                header.title()
            ] = upstream.headers[header]

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type
    )
