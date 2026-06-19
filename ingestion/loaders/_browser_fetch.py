"""
Headless-browser fetch wrapper (Playwright + Chromium).

Used as a fallback when httpx fails because:
  - The site is behind CloudFlare / WAF with TLS-fingerprint checks
  - The site is client-side-rendered (React/Next/etc.) and static HTML is
    just a JS shell — the real DOM appears only after JS runs

Threading model:
  Playwright's SYNC API binds to the thread that created it (it runs a
  greenlet event loop on that thread). The research DAG executes steps in a
  ThreadPoolExecutor, so the browser can be launched on one worker thread and
  then used from another — which raises "Cannot switch to a different thread".
  To make the browser safe to call from ANY thread, every Playwright op runs
  on ONE dedicated worker thread; callers marshal work to it through a queue
  and block for the result. Access is serialized (fine — fetches are I/O bound
  and sequential), and the greenlet never moves threads.

Public API (unchanged):
    fetch_html_with_browser(url, *, timeout_ms=15000, wait_for="load",
                            post_load_wait_ms=2000, verbose=False) -> str | None
    fetch_bytes_with_browser(url, *, referer=None, timeout_ms=60000,
                             verbose=False) -> bytes | None
    close_browser()      # optional — atexit handles it automatically
    is_available()
"""

from __future__ import annotations

import atexit
import queue
import threading
import time

# Playwright objects — all created and touched ONLY on the worker thread.
_playwright_ctx = None  # context manager object
_browser = None         # Browser
_context = None         # BrowserContext


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Dedicated single-thread browser worker
# --------------------------------------------------------------------------

_req_q: "queue.Queue" = queue.Queue()
_worker: "threading.Thread | None" = None
_worker_lock = threading.Lock()
_atexit_registered = False


def _worker_loop() -> None:
    """Owns the Playwright greenlet. Runs submitted jobs one at a time."""
    while True:
        item = _req_q.get()
        if item is None:           # shutdown sentinel
            try:
                _do_close()
            except Exception:
                pass
            return
        fn, holder, done = item
        try:
            holder["result"] = fn()
        except Exception as e:     # marshal the exception back to the caller
            holder["error"] = e
        finally:
            done.set()


def _ensure_worker() -> None:
    global _worker, _atexit_registered
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="browser-worker",
                                       daemon=True)
            _worker.start()
        if not _atexit_registered:
            atexit.register(close_browser)
            _atexit_registered = True


def _submit(fn, *, timeout: float = 120.0):
    """Run fn() on the dedicated browser thread and block for its result.
    Returns None on timeout; re-raises any exception fn() raised."""
    _ensure_worker()
    holder: dict = {}
    done = threading.Event()
    _req_q.put((fn, holder, done))
    if not done.wait(timeout):
        return None
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


# --------------------------------------------------------------------------
# Browser lifecycle (these run ON the worker thread, via _submit)
# --------------------------------------------------------------------------

def _lazy_init(verbose: bool = False) -> bool:
    """Launch the shared browser on first use. Returns True on success.
    MUST run on the worker thread."""
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
        _launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        # Prefer the SYSTEM Chrome/Edge binary. Its genuine TLS fingerprint
        # gets past CDN bot-management (Akamai / Q4-hosted IR sites) that abort
        # the bundled Chromium at the network layer with
        # ERR_HTTP2_PROTOCOL_ERROR. Fall back to Edge, then bundled Chromium.
        _browser = None
        for _ch in ("chrome", "msedge", None):
            try:
                _browser = (
                    _playwright_ctx.chromium.launch(headless=True, channel=_ch, args=_launch_args)
                    if _ch else
                    _playwright_ctx.chromium.launch(headless=True, args=_launch_args)
                )
                if verbose:
                    print(f"  [BROWSER] engine: {_ch or 'bundled chromium'}")
                break
            except Exception:
                continue
        if _browser is None:
            raise RuntimeError("no chrome / edge / bundled chromium could launch")
        _context = _browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        _context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception as e:
        if verbose:
            print(f"  [BROWSER] launch failed: {type(e).__name__}: {e}")
        return False

    if verbose:
        print(f"  [BROWSER] ready ({time.time()-t0:.1f}s startup)", flush=True)
    return True


def _do_close() -> None:
    """Actual teardown — runs on the worker thread."""
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


def close_browser() -> None:
    """Shut down the shared browser. Safe to call multiple times, from any
    thread — the teardown is marshaled to the worker thread."""
    global _worker
    with _worker_lock:
        w = _worker
        if w is not None and w.is_alive():
            try:
                _req_q.put(None)
                w.join(timeout=5)
            except Exception:
                pass
        _worker = None


# --------------------------------------------------------------------------
# Public fetch API — thin wrappers that marshal the real work to the worker
# --------------------------------------------------------------------------

def fetch_html_with_browser(
    url: str,
    *,
    timeout_ms: int = 15_000,
    wait_for: str = "load",
    post_load_wait_ms: int = 2000,
    verbose: bool = False,
) -> str | None:
    """Fetch a URL with a real headless browser and return rendered HTML.

    Navigates with wait_until=<wait_for>, waits post_load_wait_ms for SPA
    render, then captures content. If navigation times out, still reads
    whatever DOM exists. Returns None on failure. Safe to call from any thread.
    """
    def _job():
        if not _lazy_init(verbose=verbose):
            return None
        page = None
        t0 = time.time()
        try:
            page = _context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until=wait_for)
                nav_ok = True
            except Exception:
                nav_ok = False
                if verbose:
                    print(f"  [BROWSER] {url}: nav timeout ({time.time()-t0:.1f}s), "
                          f"trying partial content")
            try:
                page.wait_for_timeout(post_load_wait_ms)
            except Exception:
                pass
            try:
                html = page.content()
            except Exception:
                if verbose:
                    print(f"  [BROWSER] {url}: content() failed")
                return None
            # SPA that returned only a shell (Drupal/React IR sites populate
            # the DOM via XHR after load)? Let it settle and re-read once.
            if html is not None and len(html) < 3000:
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(3500)
                except Exception:
                    pass
                try:
                    html = page.content()
                except Exception:
                    pass
            if verbose:
                status = "fetched" if nav_ok else "fetched (partial, nav timed out)"
                print(f"  [BROWSER] {status} {url}: {len(html):,} chars "
                      f"in {time.time()-t0:.1f}s")
            return html if len(html) > 500 else None
        except Exception as e:
            if verbose:
                print(f"  [BROWSER] {url}: {type(e).__name__}: {str(e)[:100]}")
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    return _submit(_job, timeout=(timeout_ms / 1000.0) + 30.0)


def fetch_bytes_with_browser(
    url: str,
    *,
    referer: str | None = None,
    timeout_ms: int = 60_000,
    verbose: bool = False,
) -> bytes | None:
    """Fetch arbitrary bytes (typically a PDF) via the browser's context +
    JS runtime, inheriting CloudFlare cookies / TLS. Safe to call from any
    thread."""
    def _job():
        if not _lazy_init(verbose=verbose):
            return None
        from urllib.parse import urlparse
        p = urlparse(url)
        same_origin = f"{p.scheme}://{p.netloc}/"
        page_url = referer or same_origin

        # Strategy 1: in-page fetch() from a same-origin context
        page = None
        try:
            page = _context.new_page()
            try:
                page.goto(page_url, timeout=15_000, wait_until="load")
            except Exception:
                pass
            result = page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {
                            credentials: 'include',
                            headers: {'Accept': 'application/pdf,*/*'},
                        });
                        if (!resp.ok) return {error: 'HTTP ' + resp.status};
                        const buf = await resp.arrayBuffer();
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

        # Strategy 2: context.request.get with Referer
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

    return _submit(_job, timeout=(timeout_ms / 1000.0) + 30.0)


def is_available() -> bool:
    """Return True if Playwright is installed and can be used."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False
