"""
IR page URL discovery.

Two-stage approach:
  1. Pattern-guess from the company's root website (ir.DOMAIN, investors.DOMAIN,
     DOMAIN/investor-relations, etc.) — zero API cost, fast, brittle per-company
  2. Claude fallback: when pattern-guessing returns nothing usable, ask Claude
     once. Cached forever — IR URLs almost never change.

Cache: data/ir_pages.json keyed on ticker.

The loader returns ONE URL — the best candidate. Downstream loaders (the
IR page deck loader) may then navigate to sub-pages like /presentations,
/events, /financial-reports, etc.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx


CACHE_PATH = Path("data/ir_pages.json")


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Sec-Ch-Ua": '"Chromium";v="120", "Not_A Brand";v="24", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class IRPageResult:
    ticker: str = ""
    url: str = ""
    verified: bool = False
    method: str = ""              # "pattern_guess" | "claude_fallback" | "cached"
    discovered_at: str = ""
    pattern_tried: list = None    # list[str] of patterns attempted
    error: str = ""
    was_browser_needed: bool = False
    # True if URL discovery required the Playwright fallback (WAF or
    # JS-rendered). Callers should use this to decide whether subsequent
    # sub-page probes should also go through the browser.


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Domain resolution
# --------------------------------------------------------------------------

_DOMAIN_CACHE: dict[str, str] = {}


_IR_SUBDOMAIN_PREFIXES = ("ir.", "investors.", "investor.", "investorrelations.")


def _strip_ir_prefix(domain: str) -> tuple[str, str | None]:
    """
    If the domain is already an IR subdomain, return (bare_domain, original).
    Otherwise return (domain, None).

    E.g., "ir.wingstop.com" -> ("wingstop.com", "ir.wingstop.com")
          "chipotle.com"    -> ("chipotle.com", None)
    """
    for prefix in _IR_SUBDOMAIN_PREFIXES:
        if domain.startswith(prefix):
            return domain[len(prefix):], domain
    return domain, None


def _get_company_domain(ticker: str, verbose: bool = False) -> tuple[str, str | None]:
    """
    Resolve ticker -> (bare_domain, ir_subdomain_if_known).

    Uses yfinance if available. If yfinance's website is already an IR
    subdomain (e.g., "ir.wingstop.com"), strip it to get the bare domain
    AND return the known IR subdomain as a priority candidate.

    Returns ("", None) on failure — caller falls through to Claude.
    """
    ticker = ticker.upper().strip()
    if ticker in _DOMAIN_CACHE:
        # _DOMAIN_CACHE stores the tuple
        cached = _DOMAIN_CACHE[ticker]
        if isinstance(cached, tuple):
            return cached
        # Legacy string cache entry — normalize
        return cached, None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        website = info.get("website") or ""
        m = re.match(r"https?://(?:www\.)?([^/]+)", website.strip())
        if m:
            raw_domain = m.group(1).lower()
            bare, known_ir = _strip_ir_prefix(raw_domain)
            result = (bare, known_ir)
            _DOMAIN_CACHE[ticker] = result
            if verbose:
                msg = f"  [IR] resolved {ticker} -> {bare}"
                if known_ir:
                    msg += f" (yfinance returned IR subdomain directly: {known_ir})"
                print(msg)
            return result
    except Exception as e:
        if verbose:
            print(f"  [IR] yfinance domain resolve failed: {e}")
    return "", None


# --------------------------------------------------------------------------
# Pattern-guess
# --------------------------------------------------------------------------

def _candidate_urls(domain: str, known_ir_subdomain: str | None = None) -> list[str]:
    """Enumerate likely IR URLs for a given base domain."""
    d = domain.rstrip("/")
    # NOTE: use explicit prefix strip, NOT lstrip("www.") — lstrip takes a
    # character set, so "wingstop.com".lstrip("www.") → "ingstop.com" (bug).
    if d.startswith("www."):
        d = d[4:]
    candidates: list[str] = []
    # If yfinance already gave us an IR subdomain, try it first
    if known_ir_subdomain:
        candidates.append(f"https://{known_ir_subdomain}/")
    # Standard pattern enumeration
    candidates.extend([
        f"https://ir.{d}/",
        f"https://investors.{d}/",
        f"https://investor.{d}/",
        f"https://www.{d}/investor-relations",
        f"https://www.{d}/investor-relations/",
        f"https://www.{d}/investors",
        f"https://www.{d}/investors/",
        f"https://{d}/investor-relations",
        f"https://{d}/investors",
        f"https://www.{d}/ir",
        f"https://www.{d}/investors/default.aspx",
    ])
    # Dedupe while preserving order
    seen = set()
    deduped = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


_IR_URL_PATH_RE = re.compile(
    r"(^|/)(ir|investors?|investor[_-]relations?)(/|$)", re.IGNORECASE
)


def _url_path_looks_like_ir(url: str) -> bool:
    """True if the URL path strongly indicates an IR page."""
    # Strip scheme + host to compare path only
    m = re.match(r"https?://[^/]+(/.*)?$", url)
    path = (m.group(1) or "") if m else ""
    # Subdomain prefixes (ir.company.com) also count as strong signal
    host_match = re.match(r"https?://([^/]+)/?", url)
    host = host_match.group(1).lower() if host_match else ""
    if any(host.startswith(p) for p in _IR_SUBDOMAIN_PREFIXES):
        return True
    return bool(_IR_URL_PATH_RE.search(path))


def _page_looks_like_ir(html: str, ticker: str, url: str = "") -> bool:
    """
    Return True if the fetched HTML appears to be an IR page.

    Two-part heuristic:
      1. Content keywords (for server-rendered pages) — 2+ matches.
      2. URL path signal (for JS-rendered pages where static HTML is a
         thin shell but the URL itself is unambiguous, like .../investors/).
         A single "investors" content mention + IR-like path is enough.
    """
    if not html or len(html) < 500:
        return False
    lower = html.lower()
    ir_keywords = [
        "investor relations", "investor contact", "sec filings",
        "financial reports", "quarterly earnings", "earnings release",
        "press release", "annual report", "proxy statement",
        "stock price", "investors",
    ]
    matches = sum(1 for k in ir_keywords if k in lower)
    if matches >= 2:
        return True
    # JS-rendered fallback: URL path says IR + page at least mentions "investor"
    if url and _url_path_looks_like_ir(url) and matches >= 1:
        return True
    return False


def _is_js_shell(html: str) -> bool:
    """
    Return True if the HTML looks like a client-side-rendered shell
    (React/Vue/Next/etc.) OR a server-rendered page that hides deck links
    behind AJAX (WordPress + Divi, sites with CMS-loaded blocks, etc.).

    Either way: the static HTML doesn't have the deck links we need,
    so the caller should promote to browser-mode fetching.

    Signals (any one trips detection):
      - SPA framework markers (react root, __next, ng-version, data-v-app)
      - Page has no .pdf references AT ALL (IR pages with real decks
        always have at least a handful of .pdf hrefs in the static HTML
        even when the rendered page has more)
      - Very thin anchor count relative to page size
    """
    if not html or len(html) < 500:
        return False
    lower = html.lower()
    # SPA framework markers — strong signal
    spa_markers = [
        '<div id="root"',        # React
        "<div id='root'",
        '<div id="__next"',      # Next.js
        "<div id='__next'",
        'data-reactroot',
        'ng-version',            # Angular
        'data-v-app',             # Vue 3
        'data-server-rendered="false"',
        '__nuxt__',              # Nuxt
    ]
    if any(m in lower for m in spa_markers):
        return True
    # Zero PDFs on a 50K+ IR page is a tell — either it's a landing page
    # without content OR the decks are CMS-loaded via AJAX (Divi/Elementor
    # blocks, shortcode content, widget-based IR suites). Browser mode
    # needed either way.
    if len(html) > 30_000 and ".pdf" not in lower:
        return True
    # Link density — real IR pages have 30+ anchors; shells usually < 10
    anchor_count = lower.count('<a ')
    if len(html) > 50_000 and anchor_count < 15:
        return True
    return False


def _try_pattern_guess(domain: str, ticker: str, known_ir: str | None = None,
                       use_browser: bool = True,
                       verbose: bool = False) -> tuple[str | None, list[str], bool]:
    """Returns (url_or_None, tried_list, used_browser_bool)."""
    """Try each candidate URL. Return (first working URL, list of tried).

    Two passes:
      1. httpx — fast path for server-rendered sites
      2. If no httpx hit AND any attempt looked WAF-blocked OR returned a
         plausible URL with thin content (JS-rendered), retry with browser.
    """
    tried: list[str] = []
    any_waf_signal = False
    first_plausible_url: str | None = None  # 200-OK but failed keyword check (likely JS-rendered)

    candidates = _candidate_urls(domain, known_ir_subdomain=known_ir)

    # Pass 1: httpx
    for url in candidates:
        tried.append(url)
        try:
            resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=8.0,
                             follow_redirects=True)
            if resp.status_code == 200:
                if _page_looks_like_ir(resp.text, ticker, url=str(resp.url)):
                    is_shell = _is_js_shell(resp.text)
                    final = str(resp.url)
                    m_host = re.match(r"https?://([^/]+)", final)
                    host = m_host.group(1).lower() if m_host else ""
                    is_subdomain = any(host.startswith(p) for p in _IR_SUBDOMAIN_PREFIXES)
                    # A real, server-rendered IR SUBDOMAIN portal (ir./investors.)
                    # — take it immediately.
                    if is_subdomain and not is_shell:
                        if verbose:
                            print(f"  [IR] pattern-guess HIT (httpx): {url} -> {final}")
                        return final, tried, False
                    # Otherwise it's a www/path landing or a JS shell. Keep it as
                    # a fallback, but force the browser pass to try the real
                    # ir./investors. subdomains first — httpx is often blocked
                    # there (TLS fingerprint), so a www hit is NOT proof the
                    # subdomain portal doesn't exist.
                    if first_plausible_url is None:
                        first_plausible_url = final
                    any_waf_signal = True
                    if verbose:
                        print(f"  [IR] {url}: 200 (www/shell landing) — will try subdomains via browser")
                    continue
                # 200 but keyword-thin → might be JS-rendered shell. Remember.
                if first_plausible_url is None and _url_path_looks_like_ir(str(resp.url)):
                    first_plausible_url = str(resp.url)
                continue
            if resp.status_code in (403, 503):
                server = resp.headers.get("server", "").lower()
                if "cloudflare" in server or resp.headers.get("cf-ray"):
                    any_waf_signal = True
                if verbose:
                    print(f"  [IR] {url}: HTTP {resp.status_code} "
                          f"({'cloudflare' if any_waf_signal else 'waf?'})")
                continue
        except Exception as e:
            if verbose:
                print(f"  [IR] {url}: {type(e).__name__}")
            # TLS-fingerprint blocks surface as ConnectError / ReadError, and
            # bot-managed IR subdomains often just hang (ReadTimeout). Any of
            # these means "httpx can't reach it — try the browser."
            if isinstance(e, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)):
                any_waf_signal = True
            continue

    # Pass 2: browser fallback if we have signals it's worth trying.
    # IMPORTANT: prefer WAF-blocked URLs over the "plausible" marketing
    # landing. A 403 on ir.X/ almost always means the real vendor IR portal
    # is there — browser gets past CloudFlare. The marketing landing at
    # www.X/investors is usually just a pointer page.
    if use_browser and (any_waf_signal or first_plausible_url):
        from ingestion.loaders._browser_fetch import fetch_html_with_browser, is_available
        if is_available():
            if verbose:
                print(f"  [IR] httpx pass 1 failed; trying browser fallback...")
            # Priority order:
            # 1. Top httpx candidates (ir.X/, investors.X/, investor.X/) —
            #    these are the most likely real IR URLs, and WAF-blocked
            #    status actually means "there's a real IR portal here"
            # 2. The plausible URL (thin-content marketing landing) as fallback
            browser_targets = []
            for url in candidates[:4]:
                browser_targets.append(url)
            if first_plausible_url and first_plausible_url not in browser_targets:
                browser_targets.append(first_plausible_url)
            for url in browser_targets:
                html = fetch_html_with_browser(url, verbose=verbose)
                if not html or len(html) < 1000:
                    continue
                if _page_looks_like_ir(html, ticker, url=url):
                    if verbose:
                        print(f"  [IR] pattern-guess HIT (browser): {url}")
                    return url, tried, True
        elif verbose:
            print(f"  [IR] browser fallback unavailable (install playwright)")

    return None, tried, False


# --------------------------------------------------------------------------
# Claude fallback
# --------------------------------------------------------------------------

_CLAUDE_FALLBACK_SYSTEM = (
    "You are a web-reference assistant. You know the official Investor Relations "
    "URLs of publicly-traded US companies. You respond with a single URL and no prose."
)

_CLAUDE_FALLBACK_PROMPT = """What is the official Investor Relations URL for
{ticker} (the publicly-traded US company)? Respond with just the URL — no prose,
no "the URL is", just the URL itself. If you don't know with reasonable
confidence, respond with "UNKNOWN". The URL should be the landing page for
their investor relations section (not a specific sub-page).

Ticker: {ticker}
Company name hint: {company_hint}
"""


def _claude_fallback(ticker: str, company_hint: str = "", verbose: bool = False) -> str | None:
    """Ask Claude for the IR URL. Returns URL or None."""
    try:
        from research.deep_research import ANTHROPIC_API_KEY
    except Exception:
        return None
    if not ANTHROPIC_API_KEY:
        return None

    prompt = _CLAUDE_FALLBACK_PROMPT.format(ticker=ticker, company_hint=company_hint or "(unknown)")
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 200,
                "temperature": 0.0,
                "system": _CLAUDE_FALLBACK_SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        if resp.status_code != 200:
            if verbose:
                print(f"  [IR] Claude fallback HTTP {resp.status_code}")
            return None
        text = resp.json()["content"][0]["text"].strip()
    except Exception as e:
        if verbose:
            print(f"  [IR] Claude fallback exception: {e}")
        return None

    # Strip quotes/markdown if present
    text = text.strip("` \n\"'")
    if text.lower() in ("unknown", "none", "n/a"):
        return None
    if not text.startswith("http"):
        if verbose:
            print(f"  [IR] Claude returned non-URL: {text[:80]}")
        return None
    # Verify it loads. Accept 200 (content-verified) OR 403/503 (WAF-blocked
    # but the URL clearly exists on the right domain).
    try:
        r = httpx.get(text, headers=BROWSER_HEADERS, timeout=8.0, follow_redirects=True)
        if r.status_code == 200 and _page_looks_like_ir(r.text, ticker, url=str(r.url)):
            if verbose:
                print(f"  [IR] Claude fallback verified (content): {text} -> {r.url}")
            return str(r.url)
        if r.status_code in (403, 503):
            if verbose:
                print(f"  [IR] Claude fallback WAF-blocked ({r.status_code}) but accepting: {text}")
            return str(r.url)
        if verbose:
            print(f"  [IR] Claude URL didn't verify: {text} (HTTP {r.status_code})")
    except Exception as e:
        if verbose:
            print(f"  [IR] Claude URL verify exception: {e}")
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

# Curated IR / deck-page URLs for names the auto-resolver can't reach — e.g.
# foreign ADRs whose IR site isn't derivable from SEC/domain data (Jollibee's
# decks live on jollibeegroup.com, not anything tied to the JBFCY ADR). Checked
# BEFORE cache + pattern-guess. Point at the presentations page when there's a
# clean one (the deck loader extracts the PDF links from there).
_IR_URL_OVERRIDES = {
    "JBFCY": "https://www.jollibeegroup.com/ir-presentations/",
    # FEMSA: as a foreign filer it has no 8-K earnings releases on EDGAR, and the
    # auto-resolver lands on ir.femsa.com (an SPA shell that 404s the PDF paths).
    # The real quarterly earnings-release PDFs live on the gcs-web host (verified
    # downloadable: "PR 3Q25 vf.pdf" etc.); the direct-PDF extractor reads them here.
    "FMX": "https://femsa.gcs-web.com/financial-reports/quarterly-results",
}


def find_ir_url(ticker: str, *, force: bool = False, use_browser: bool = True,
                verbose: bool = False) -> IRPageResult:
    """
    Find the Investor Relations URL for a ticker.

    Strategy: cache -> pattern-guess (httpx, with browser fallback on WAF)
    -> Claude fallback. Returns IRPageResult with url="" and verified=False
    on total failure (logged, caller decides what to do).

    use_browser: when True (default), fall back to Playwright for sites
    that httpx can't reach (CloudFlare WAF, JS-rendered pages). Set False
    to disable the browser path entirely (e.g., in CI without Playwright).
    """
    ticker = ticker.upper().strip()

    if ticker in _IR_URL_OVERRIDES:
        url = _IR_URL_OVERRIDES[ticker]
        if verbose:
            print(f"  [IR] override {ticker} -> {url}")
        return IRPageResult(ticker=ticker, url=url, verified=True, method="override")

    cache = _load_cache()

    if not force and ticker in cache:
        entry = cache[ticker]
        if entry.get("url") and entry.get("verified"):
            if verbose:
                print(f"  [IR] cache hit {ticker} -> {entry['url']}")
            return IRPageResult(
                ticker=ticker,
                url=entry["url"],
                verified=True,
                method="cached",
                discovered_at=entry.get("discovered_at", ""),
                was_browser_needed=entry.get("was_browser_needed", False),
            )

    tried: list[str] = []

    # Stage A: pattern guess
    domain, known_ir = _get_company_domain(ticker, verbose=verbose)
    if domain:
        url, patterns_tried, used_browser = _try_pattern_guess(
            domain, ticker, known_ir=known_ir,
            use_browser=use_browser, verbose=verbose,
        )
        tried.extend(patterns_tried)
        if url:
            result = IRPageResult(
                ticker=ticker, url=url, verified=True,
                method="pattern_guess",
                discovered_at=datetime.utcnow().isoformat() + "Z",
                pattern_tried=tried,
                was_browser_needed=used_browser,
            )
            cache[ticker] = {
                "url": url, "verified": True, "method": "pattern_guess",
                "was_browser_needed": used_browser,
                "discovered_at": result.discovered_at,
            }
            _save_cache(cache)
            return result
    else:
        if verbose:
            print(f"  [IR] no domain for {ticker}; skipping pattern guess")

    # Stage B: Claude fallback
    if verbose:
        print(f"  [IR] pattern guess failed for {ticker}; trying Claude fallback")
    url = _claude_fallback(ticker, company_hint=domain or "", verbose=verbose)
    if url:
        # Claude fallback is used when pattern guess couldn't reach any site;
        # the returned URL is often behind WAF or JS. Default to browser for probing.
        result = IRPageResult(
            ticker=ticker, url=url, verified=True,
            method="claude_fallback",
            was_browser_needed=True,
            discovered_at=datetime.utcnow().isoformat() + "Z",
            pattern_tried=tried,
        )
        cache[ticker] = {
            "url": url, "verified": True, "method": "claude_fallback",
            "discovered_at": result.discovered_at,
        }
        _save_cache(cache)
        return result

    # Total failure: log, return empty, do NOT cache (so we retry later)
    return IRPageResult(
        ticker=ticker, url="", verified=False,
        method="failed",
        discovered_at=datetime.utcnow().isoformat() + "Z",
        pattern_tried=tried,
        error="pattern_guess returned no match and Claude fallback failed",
    )
