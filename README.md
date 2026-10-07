# Pinterest Media Resolver — SSSPin-style architecture

This is a separate backend for an Elementor/Pantheon Pinterest downloader.

## Why this exists

Elementor HTML/JavaScript cannot reliably scrape Pinterest because browser CORS and Pinterest's server-side rendering make direct client-side extraction unreliable.

The frontend should call:

POST /api/resolve
{
  "url": "https://pin.it/..."
}

The resolver uses two extraction layers:

1. Direct server-side HTTP extraction.
2. Headless Chromium/Playwright fallback.

The extractor looks for:
- Pinterest Relay/SSR payloads
- JSON-LD VideoObject contentUrl
- og:video
- actual v1.pinimg.com/videos/*.mp4 URLs
- video/source elements
- video URLs observed by the browser before the media request is aborted

It deliberately does NOT convert a poster JPG into MP4.

## Deploy on Render

1. Create a GitHub repository.
2. Upload this folder.
3. Connect the repository to Render.
4. Render uses Dockerfile automatically.
5. Set `CORS_ORIGINS` to your exact frontend origin, e.g.
   https://dev-pinterest-video-download.pantheonsite.io

Health:
GET /health

Resolve:
POST /api/resolve

Download proxy:
GET /api/download?url=<URL-encoded-pinterest-cdn-url>

## Important

This supports public Pinterest content only. It does not bypass private/restricted access.

Progressive MP4 is preferred. HLS-only pins are reported as unsupported rather than being mislabeled as MP4.

For production traffic, put Cloudflare/rate limiting in front and use a shared rate limiter instead of the in-process limiter.

## Elementor

After deployment, replace the API base in the supplied Elementor HTML:

const API_BASE = "https://YOUR-RENDER-SERVICE.onrender.com";

Then the frontend calls:
POST ${API_BASE}/api/resolve

The returned media item includes the direct Pinterest CDN URL and a backend `/api/download` proxy can be used for one-click download.


## v1.1 CORS troubleshooting

v1.1 allows public cross-origin browser requests so an Elementor/Pantheon
frontend can call the Render API without a CORS preflight failure.

After deployment, test:
- GET /health
- GET /api/cors-test

If both open in the browser, the Render API is reachable. The Elementor
frontend must use the exact Render `onrender.com` service URL as `API_BASE`.

After the first successful production test, CORS can be restricted to the
production frontend domain.
