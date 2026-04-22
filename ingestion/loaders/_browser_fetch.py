"""
Headless-browser fetch wrapper (Playwright + Chromium).

Used as a fallback when httpx fails because:
  - The site is behind CloudFlare / WAF with TLS-fingerprint checks
  - The site is client-side-rendered (React/Next/etc.) and static HTML is
    just a JS shell — the real DOM appears only after JS runs

Design:
  - Module-level singleton browser instance — launched once per run, reused
    across many fetches. Launch cost (~5s) amortized.
  - Automatic cleanup on process exit via atexit.
  - Sync API (we're called from sync code).
  - Ignores robots.txt deliberately — we're scraping already-public IR data
    the user has a legitimate interest in, not crawling at scale.

Public API:
    fetch_html_with_browser(url, *, timeout=30.0, wait_for="networkidle",
                            verbose=False) -> str | None

    close_browser()  # optional — atexit handles it automatically
"""

from __future__ import annotations

import atexit
import sys
import time

# Lazy import — if playwright isn't installed, the fallback code path
# simply doesn't run (module raises ImportError at first use)
_playwright_ctx = None  # context manager object
_browser = None         # Browser
_context = None         # BrowserContext


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _lazy_init(verbose: bool = False) -> bool:
    """Launch the shared browser on first use. Returns True on success."""
    global _playwright_ctx, _browser, _context
    if _browser is not None:
        return True
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("  [BROWSER] playwright not installed — browser fallback unavailable")
        return False

    if verbose:
        print("  [BROWSER] launching chromium...", flush=True)
    t0 = time.time()
    try:
        _playwright_ctx = sync_playwright().start()
        _browser = _playwright_ctx.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        _context = _browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            # Mimic browser navigator properties that CloudFlare fingerprints
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        # Reduce obvious automation markers
        _context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception as e:
        if verbose:
            print(f"  [BROWSER] launch failed: {type(e).__name__}: {e}")
        return False

    if verbose:
        print(f"  [BROWSER] ready ({time.time()-t0:.1f}s startup)", flush=True)
    atexit.register(close_browser)
    return True


def close_browser() -> None:
    """Shut down the shared browser. Safe to call multiple times."""
    global _playwright_ctx, _browser, _context
    try:
        if _context is not None:
            _context.close()
    except Exception:
        pass
    try:
        if _browser is not None:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright_ctx is not None:
            _playwright_ctx.stop()
    except Exception:
        pass
    _browser = None
    _context = None
    _playwright_ctx = None


def fetch_html_with_browser(
    url: str,
    *,
    timeout_ms: int = 15_000,
    wait_for: str = "load",
    post_load_wait_ms: int = 2000,
    verbose: bool = False,
) -> str | None:
    """
    Fetch a URL with a real headless browser and return the rendered HTML.

    Strategy:
      1. navigate with wait_until="load" (fires on window.load — fast and
         reliable. "networkidle" is more thorough but hangs on SPAs with
         persistent analytics/websocket connections.)
      2. extra fixed wait for SPA render (React/Vue/etc. populate DOM
         AFTER window.load)
      3. capture HTML

    Also: if the initial navigation times out, we still try to read whatever
    HTML is in the DOM at that moment — often it's populated even if the
    load event never fired.

    Args:
        url: target URL
        timeout_ms: navigation timeout (default 15s — down from 25s)
        wait_for: "load" | "domcontentloaded" | "networkidle"
        post_load_wait_ms: extra wait after load event for SPA render
        verbose: print progress

    Returns the page HTML after JS has executed, or None on failure.
    """
    if not _lazy_init(verbose=verbose):
        return None

    page = None
    t0 = time.time()
    try:
        page = _context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until=wait_for)
            nav_ok = True
        except Exception as e:
            # Navigation timed out, but the DOM may still be populated.
            # Fall through to content read — partial content beats nothing.
            nav_ok = False
            if verbose:
                print(f"  [BROWSER] {url}: nav timeout ({time.time()-t0:.1f}s), "
                      f"trying partial content")
        # Give SPA render a moment (React/Vue populate DOM post-load)
        try:
            page.wait_for_timeout(post_load_wait_ms)
        except Exception:
            pass
        try:
            html = page.content()
        except Exception as e:
            if verbose:
                print(f"  [BROWSER] {url}: content() failed: {type(e).__name__}")
            return None

        if verbose:
            status = "fetched" if nav_ok else "fetched (partial, nav timed out)"
            print(f"  [BROWSER] {status} {url}: {len(html):,} chars "
                  f"in {time.time()-t0:.1f}s")
        return html if len(html) > 500 else None
    except Exception as e:
        if verbose:
            msg = str(e)[:100]
            print(f"  [BROWSER] {url}: {type(e).__name__}: {msg}")
        return None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def fetch_bytes_with_browser(
    url: str,
    *,
    referer: str | None = None,
    timeout_ms: int = 60_000,
    verbose: bool = False,
) -> bytes | None:
    """
    Fetch arbitrary bytes (typically a PDF) using the browser's shared
    context + JS runtime. Bypasses CloudFlare protection by running the
    actual fetch() inside a page loaded on the same origin — the request
    inherits all cookies, headers, and TLS fingerprint set during prior
    navigations.

    Strategies tried in order:
      1. In-page fetch() from a same-origin page context (most reliable
         for CloudFlare-protected static assets — the fetch looks exactly
         like a normal browser request, not an API call)
      2. context.request.get() with same-origin Referer (fast path for
         sites without aggressive Referer validation)
    """
    if not _lazy_init(verbose=verbose):
        return None

    from urllib.parse import urlparse
    p = urlparse(url)
    same_origin = f"{p.scheme}://{p.netloc}/"
    # Use the provided referer or fall back to the origin root
    page_url = referer or same_origin

    # Strategy 1: in-page fetch (best for CloudFlare-protected PDFs)
    page = None
    try:
        page = _context.new_page()
        try:
            page.goto(page_url, timeout=15_000, wait_until="load")
        except Exception:
            # Even a partial load is OK — we just need a same-origin context
            pass
        # Run fetch() inside the page. Uses browser-native fetch → inherits
        # CloudFlare session cookies + TLS. Returns base64 so Playwright IPC
        # can transport binary data.
        result = page.evaluate(
            """async (url) => {
                try {
                    const resp = await fetch(url, {
                        credentials: 'include',
                        headers: {'Accept': 'application/pdf,*/*'},
                    });
                    if (!resp.ok) return {error: 'HTTP ' + resp.status};
                    const buf = await resp.arrayBuffer();
                    // Chunked conversion — avoids stack overflow on large PDFs
                    const bytes = new Uint8Array(buf);
                    let binary = '';
                    const CHUNK = 0x8000;
                    for (let i = 0; i < bytes.length; i += CHUNK) {
                        binary += String.fromCharCode.apply(
                            null, bytes.subarray(i, i + CHUNK)
                        );
                    }
                    return {data: btoa(binary), size: bytes.length};
                } catch (e) {
                    return {error: String(e)};
                }
            }""",
            url,
        )
        if "data" in result:
            import base64
            content = base64.b64decode(result["data"])
            if verbose:
                print(f"  [BROWSER] in-page fetched {url}: {len(content):,} bytes")
            return content
        if verbose:
            print(f"  [BROWSER] in-page fetch {url}: {result.get('error','unknown error')}")
    except Exception as e:
        if verbose:
            print(f"  [BROWSER] in-page fetch exception {url}: {type(e).__name__}: {str(e)[:100]}")
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    # Strategy 2: context.request.get with Referer (fallback)
    headers = {
        "Referer": page_url,
        "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = _context.request.get(url, headers=headers, timeout=timeout_ms)
        if resp.ok:
            content = resp.body()
            if content:
                if verbose:
                    print(f"  [BROWSER] request.get fallback {url}: {len(content):,} bytes")
                return content
        elif verbose:
            print(f"  [BROWSER] request.get fallback {url}: HTTP {resp.status}")
    except Exception as e:
        if verbose:
            print(f"  [BROWSER] request.get fallback {url}: {type(e).__name__}")

    return None


def is_available() -> bool:
    """Return True if Playwright is installed and can be used."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False
