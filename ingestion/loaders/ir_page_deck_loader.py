"""
IR Page Deck Loader.

Fetches investor presentations, shareholder letters, and conference/
investor-day decks from a company's Investor Relations page. Used when
EDGAR 8-K exhibits don't have a slide deck (common for many restaurants,
consumer names, and companies that post decks on IR but don't exhibit).

Flow:
  1. Find IR URL via _ir_page_finder (pattern-guess → Claude fallback)
  2. Fetch IR root HTML
  3. Classify links on IR root (Claude subagent: HTML → deck catalog)
  4. Also try common sub-pages: /presentations, /events, /financial-reports,
     /shareholder-letters
  5. Merge catalogs, dedupe by URL
  6. Download each PDF, parse with pdfplumber (reuse slide_deck_loader
     machinery), return SlideDeck list

Public API:
    fetch_ir_slide_decks(ticker, types=None, max_decks=20, force=False,
                         verbose=False) -> list[SlideDeck]

CLI:
    python -m ingestion.loaders.ir_page_deck_loader TICKER [--types ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ingestion.loaders._ir_page_classifier import (
    DeckCandidate, classify_ir_page,
)
from ingestion.loaders._ir_page_finder import (
    BROWSER_HEADERS, find_ir_url,
)
from ingestion.loaders.slide_deck_loader import (
    SlideDeck, SlidePage, SlidePageTable, _parse_pdf,
)


CACHE_DIR = Path("data/slide_decks")           # shared with EDGAR loader
IR_PAGE_CACHE = Path("data/ir_page_html")      # raw IR HTML snapshots


# --------------------------------------------------------------------------
# Post-classifier blocklist — drop things that aren't real slide decks
# --------------------------------------------------------------------------

# Patterns matched against title + URL path (case-insensitive). A single
# match drops the candidate. The list is conservative: a real earnings deck
# titled "Q4 Earnings Presentation" won't match any of these.
_NON_DECK_PATTERNS = [
    # SEC filings — handled by other loaders, not what we want here
    r"\b10-?k\b", r"\b10-?q\b", r"\b8-?k\b", r"\bdef\s*14a\b",
    r"\bproxy\s*statement\b", r"\bproxy\b",
    r"\bannual\s*report\b", r"\bform\s*10\b",
    # Earnings-adjacent but NOT the deck
    r"\bnon-?gaap\s*reconciliation\b", r"\bgaap\s*reconciliation\b",
    r"\bsupplemental(\s+(financial|investor))?\s*(information|data|pack(age)?)?\b",
    r"\breconciliation\s+of\b",
    r"\bfinancial\s*(tables?|schedules?|supplement)\b",
    r"\bfact\s*sheet\b", r"\bfactsheet\b",
    # ESG / sustainability / DEI
    r"\besg\s*(report|document)?\b", r"\bsustainability\s*report\b",
    r"\bcorporate\s*responsibility\b", r"\bimpact\s*report\b",
    # Audio/video — classifier shouldn't pick these, but guard anyway
    r"\bwebcast\b", r"\bpodcast\b", r"\btranscript\b", r"\bmp3\b", r"\bmp4\b",
    # Press releases — handled by press_release_loader
    r"\bpress\s*release\b",
]

_NON_DECK_RE = re.compile("|".join(_NON_DECK_PATTERNS), re.IGNORECASE)


def _is_non_deck(candidate) -> bool:
    """Return True if the candidate should be filtered out (not a real deck)."""
    haystack = f"{candidate.title}  {candidate.url}  {candidate.source_anchor_text}"
    return bool(_NON_DECK_RE.search(haystack))


# --------------------------------------------------------------------------
# Sub-page probes
# --------------------------------------------------------------------------

# Common sub-page suffixes relative to IR root.
# Covers two generations of Q4 Inc. vendor IR layouts plus generic custom
# IR pages. Most companies use one of these patterns.
IR_SUBPAGES = [
    "",                          # the IR root itself
    # Old-style Q4 + generic custom IR
    "presentations",
    "presentations/",
    "events-and-presentations",
    "events-and-presentations/",
    "events",
    "events/",
    "financial-reports",
    "financial-reports/",
    "shareholder-letters",
    "shareholder-letters/",
    "annual-reports",
    "annual-reports/",
    "reports",
    # New-style Q4 Inc. (used by WING, many others)
    "news-and-events/events-and-presentations/",
    "news-and-events/events-and-presentations",
    "news-and-events/press-releases/",
    "financials/quarterly-results/",
    "financials/quarterly-results",
    "financials/annual-reports/",
    "financials/annual-reports",
    "financials/sec-filings/",
]


def _probe_subpages(ir_url: str, *, use_browser: bool = True,
                    force_browser_mode: bool = False,
                    verbose: bool = False) -> list[tuple[str, str]]:
    """
    Return list of (url, html) for IR root + any sub-pages that load.

    Two-mode fetch:
      - Default: httpx with cookie-jar + Referer chain (fast)
      - Fallback: when httpx returns WAF signals (CloudFlare) or a thin shell
        that smells JS-rendered, retry with Playwright (slower but bulletproof)

    If `force_browser_mode=True`, httpx is skipped entirely and every probe
    goes through Playwright. This is required for SPAs that return the same
    JS shell for every route — httpx would get 13 identical HTML blobs.
    """
    from ingestion.loaders._browser_fetch import fetch_html_with_browser, is_available
    browser_available = use_browser and is_available()

    results: list[tuple[str, str]] = []
    root_url = ir_url.rstrip("/") + "/"
    seen: set[str] = set()

    # If caller already knows this is a JS/WAF site, skip httpx entirely
    if force_browser_mode:
        if not browser_available:
            print(f"  [IR] JS/WAF site but playwright unavailable — cannot probe")
            return results
        if verbose:
            print(f"  [IR] force_browser_mode=True — probing all sub-pages via browser")
        for suffix in IR_SUBPAGES:
            url = urljoin(root_url, suffix) if suffix else ir_url
            if url in seen:
                continue
            seen.add(url)
            html = fetch_html_with_browser(url, verbose=verbose)
            if html and len(html) > 1000:
                results.append((url, html))
                if verbose:
                    print(f"  [IR] probed {url}: {len(html):,} chars (browser)")
        return results

    # Detect whether the root itself is WAF-blocked or thin. If so, we'll
    # use browser for the full probe set rather than alternating per-URL.
    force_browser = False

    def _probe_root_with_httpx(client) -> tuple[str, str] | None:
        """Return (url, html) on success; else None (sets force_browser)."""
        nonlocal force_browser
        try:
            r = client.get(ir_url)
            if r.status_code == 200 and len(r.text) > 1000:
                # Cheap sanity: is the HTML a real page or a JS shell?
                # Heuristic: >= 50K and low <script> density = real.
                # We accept what we got; classifier handles the content.
                return (ir_url, r.text)
            server = r.headers.get("server", "").lower()
            if "cloudflare" in server or r.headers.get("cf-ray"):
                if verbose:
                    print(f"  [IR] root {ir_url}: CloudFlare WAF detected (HTTP {r.status_code})")
                force_browser = True
                return None
            if verbose:
                print(f"  [IR] root {ir_url}: HTTP {r.status_code}, {len(r.text):,} chars")
            # 200 but thin — could be JS shell
            if r.status_code == 200:
                force_browser = True
            return None
        except Exception as e:
            if verbose:
                print(f"  [IR] root fetch exception: {type(e).__name__}: {e}")
            force_browser = True
            return None

    with httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=15.0,
        follow_redirects=True,
        http2=False,
    ) as client:
        root_result = _probe_root_with_httpx(client)
        if root_result is not None:
            results.append(root_result)
            seen.add(ir_url)
            if verbose:
                print(f"  [IR] probed {ir_url}: {len(root_result[1]):,} chars")

        # Promote to browser-only mode if we detected WAF/JS at root
        if force_browser and not browser_available:
            print(f"  [IR] WAF/JS-rendered site detected but playwright not available — skipping")
            return results

        if force_browser:
            if verbose:
                print(f"  [IR] switching to browser mode for all sub-page probes")
            # Retry root with browser
            if ir_url not in seen:
                html = fetch_html_with_browser(ir_url, verbose=verbose)
                if html and len(html) > 1000:
                    results.append((ir_url, html))
                    seen.add(ir_url)
            # Probe all sub-pages with browser
            for suffix in IR_SUBPAGES:
                url = urljoin(root_url, suffix) if suffix else ir_url
                if url in seen:
                    continue
                seen.add(url)
                html = fetch_html_with_browser(url, verbose=verbose)
                if html and len(html) > 1000:
                    results.append((url, html))
            return results

        # Normal httpx path for the rest
        sub_headers = {"Referer": ir_url,
                       "Sec-Fetch-Site": "same-origin",
                       "Sec-Fetch-User": "?1"}
        for suffix in IR_SUBPAGES:
            url = urljoin(root_url, suffix) if suffix else ir_url
            if url in seen:
                continue
            seen.add(url)
            try:
                r = client.get(url, headers=sub_headers)
                if r.status_code == 200 and len(r.text) > 1000:
                    results.append((url, r.text))
                    if verbose:
                        print(f"  [IR] probed {url}: {len(r.text):,} chars")
                elif verbose and r.status_code != 404:
                    print(f"  [IR] {url}: HTTP {r.status_code}")
            except Exception as e:
                if verbose:
                    print(f"  [IR] {url}: {type(e).__name__}")
                continue

    return results


# --------------------------------------------------------------------------
# PDF fetch + cache
# --------------------------------------------------------------------------

def _pdf_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def _ir_deck_cache_path(ticker: str, content_hash: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}_ir_{content_hash}.json"


def _ir_pdf_local_path(ticker: str, content_hash: str, url: str) -> Path:
    filename = url.rsplit("/", 1)[-1] or "deck.pdf"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return CACHE_DIR / "pdfs" / f"{ticker.upper()}_ir_{content_hash}__{filename}"


def _fetch_and_parse_pdf(url: str, ticker: str, *,
                         use_browser: bool = True,
                         verbose: bool = False) -> tuple[bytes, list[SlidePage]] | tuple[None, None]:
    """
    Fetch a PDF and parse with pdfplumber. Falls back to browser fetch
    when httpx gets 403/WAF — CloudFlare often protects both the IR page
    AND the PDFs linked from it.
    """
    content: bytes | None = None

    # Pass 1: httpx (fast path)
    try:
        resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=120.0,
                         follow_redirects=True)
        if resp.status_code == 200:
            content = resp.content
        else:
            if verbose:
                print(f"  [IR-PDF] httpx HTTP {resp.status_code} {url}")
    except Exception as e:
        if verbose:
            print(f"  [IR-PDF] httpx exception {url}: {type(e).__name__}")

    # Pass 2: browser fallback
    if content is None and use_browser:
        from ingestion.loaders._browser_fetch import fetch_bytes_with_browser, is_available
        if is_available():
            if verbose:
                print(f"  [IR-PDF] retrying with browser: {url}")
            content = fetch_bytes_with_browser(url, verbose=verbose)

    if not content or len(content) < 5000:
        if verbose and content is not None:
            print(f"  [IR-PDF] too small ({len(content)} bytes) {url}")
        return None, None

    # Sanity check — bytes start with %PDF?
    if not content[:5].startswith(b"%PDF"):
        if verbose:
            print(f"  [IR-PDF] not a valid PDF (magic bytes: {content[:5]!r}) {url}")
        return None, None

    pages = _parse_pdf(content, verbose=verbose)
    if not pages:
        if verbose:
            print(f"  [IR-PDF] pdfplumber returned no pages {url}")
        return None, None
    return content, pages


def _load_cached_deck(path: Path) -> SlideDeck | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    deck_fields = {k: v for k, v in data.items()
                   if k in SlideDeck.__dataclass_fields__ and k != "pages"}
    pages = []
    for p in data.get("pages", []):
        tables = [SlidePageTable(**t) for t in p.get("tables", [])]
        pages.append(SlidePage(
            page_number=p.get("page_number", 0),
            text=p.get("text", ""),
            tables=tables,
        ))
    deck_fields["pages"] = pages
    return SlideDeck(**deck_fields)


def _save_deck(deck: SlideDeck, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(deck), f, indent=2, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------
# Build SlideDeck from a classified candidate
# --------------------------------------------------------------------------

def _candidate_to_slidedeck(
    ticker: str, candidate: DeckCandidate, *,
    use_browser: bool = True,
    verbose: bool = False,
) -> SlideDeck | None:
    """Fetch PDF, parse, package as SlideDeck. None on failure."""
    # Accept .pdf/.pptx URLs AND extension-less document-download URLs —
    # modern IR sites (Drupal / Q4) serve decks from opaque paths like
    # /static-files/<uuid> with no extension. The %PDF magic-byte check in
    # _fetch_and_parse_pdf is the real gate; here we only weed out links that
    # are clearly an HTML page rather than a file download.
    _u = candidate.url.lower()
    _looks_doc = (
        _u.endswith(".pdf") or _u.endswith(".pptx")
        or "/static-files/" in _u or "/download" in _u
        or "/files/doc" in _u or "q4cdn" in _u
        or (candidate.title or "").lower().endswith(".pdf")
    )
    if re.search(r"\.(html?|aspx|php|jsp)(\?|$)", _u) or not _looks_doc:
        if verbose:
            print(f"  [IR] skip non-PDF: {candidate.url}")
        return None

    content, pages = _fetch_and_parse_pdf(
        candidate.url, ticker, use_browser=use_browser, verbose=verbose,
    )
    if content is None:
        return None

    content_hash = _pdf_hash(content)
    cache_path = _ir_deck_cache_path(ticker, content_hash)
    if cache_path.exists():
        cached = _load_cached_deck(cache_path)
        if cached is not None:
            if verbose:
                print(f"  [IR] cache hit {cached.deck_type} ({cache_path.name})")
            return cached

    # Cache raw PDF
    pdf_path = _ir_pdf_local_path(ticker, content_hash, candidate.url)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(content)

    total_tables = sum(len(p.tables) for p in pages)
    total_chars = sum(len(p.text) for p in pages)

    deck = SlideDeck(
        ticker=ticker.upper(),
        quarter=candidate.quarter,
        report_date=candidate.date,
        filing_date=candidate.date,
        accession="",                           # not an EDGAR accession
        filing_type="",
        exhibit_num="",
        source_url=candidate.url,
        source="ir_page",
        deck_type=candidate.deck_type or "other",
        event_metadata=candidate.event_metadata or {},
        classification_confidence=candidate.classification_confidence or "fallback",
        title=candidate.title or "",
        page_count=len(pages),
        pages=pages,
        total_tables=total_tables,
        total_chars=total_chars,
        fetched_at=datetime.utcnow().isoformat() + "Z",
        content_hash=content_hash,
        pdf_local_path=str(pdf_path),
    )
    _save_deck(deck, cache_path)
    if verbose:
        print(f"  [IR] parsed {deck.deck_type} ({deck.title[:50]}): "
              f"{len(pages)} pages, {total_chars:,} chars, {total_tables} tables")
    return deck


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _link_catalog(html: str, max_chars: int = 45000) -> str:
    """Compact, link-focused extract of a page: deck-like anchors first, then
    headings, then the rest of the anchors (href/title/type attributes intact).
    A huge IR SPA can lean to hundreds of KB and blow the classifier's input
    budget, truncating the real deck links off the end. Sending just the links
    (decks prioritized) keeps every page's decks within budget."""
    anchors = re.findall(r"<a\b[^>]*>.*?</a>", html or "", re.I | re.S)
    deckish, other = [], []
    for a in anchors:
        (deckish if re.search(r"\.pdf|application/pdf|static-files|/files/doc|present|slide|investor.?day", a, re.I)
         else other).append(a)
    heads = re.findall(r"<h[1-5]\b[^>]*>.*?</h[1-5]>", html or "", re.I | re.S)
    out, total = [], 0
    for p in deckish + heads + other:
        p = re.sub(r"\s+", " ", p).strip()[:400]
        if not p:
            continue
        out.append(p)
        total += len(p)
        if total > max_chars:
            break
    return "\n".join(out)


def _discover_deck_pages(html: str, base_url: str) -> list[str]:
    """From IR HTML, return same-domain links to events / presentations
    sub-pages. Companies hide decks behind arbitrary paths (e.g.
    /stock-and-financial/events-and-presentations) that the fixed IR_SUBPAGES
    suffix guesses never hit, but the root nav always links straight to them."""
    from urllib.parse import urlparse
    base_host = urlparse(base_url).netloc.lower()
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html or "", re.I | re.S):
        href = m.group(1)
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        hay = (href + " " + text).lower()
        if not re.search(r"present|events?[-\s]?and|investor[-\s]?day|slide", hay):
            continue
        if re.search(r"webcast|video|\.mp4|\.mp3|audio|podcast|press[-\s]?release", hay):
            continue
        absu = urljoin(base_url, href.split("#")[0]).rstrip("/")
        if not absu.startswith("http") or urlparse(absu).netloc.lower() != base_host:
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
    return out[:8]


def _extract_direct_pdf_links(pages, base_url: str, verbose: bool = False) -> list:
    """Extract deck candidates from DIRECT <a href="...pdf"> links on the IR
    pages — no Claude call. Handles static 'presentations' pages (e.g. Jollibee's
    jollibeegroup.com/ir-presentations, 20 PDF decks) and avoids the classifier
    being a single point of failure (HTTP 529 overloaded)."""
    from urllib.parse import urljoin, unquote
    page_list = list(pages)
    have = {u.rstrip("/") for u, _ in page_list}
    if base_url.rstrip("/") not in have:
        try:
            r = httpx.get(base_url, timeout=20.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (workbench deck loader)"})
            if r.status_code == 200 and len(r.text) > 1000:
                page_list.append((base_url, r.text))
        except Exception:
            pass

    cands, seen = [], set()
    for page_url, html in page_list:
        for m in re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I):
            url = urljoin(page_url, m.group(1).strip())
            key = url.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            fname = re.sub(r"\.pdf.*$", "", url.rsplit("/", 1)[-1], flags=re.I)
            title = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", unquote(fname))).strip()
            low = url.lower()
            if "earnings" in low or re.search(r"q[1-4][-_ ]?(?:fy)?[-_ ]?\d{2,4}", low):
                dtype = "earnings"
            elif re.search(r"investor|briefing|conference|capital[ -]markets|analyst[ -]day", low):
                dtype = "conference" if "conference" in low else "investor_day"
            elif "letter" in low:
                dtype = "shareholder_letter"
            else:
                dtype = "other"
            dm = re.search(r"/((?:19|20)\d\d)/(\d{2})/", url)
            date = f"{dm.group(1)}-{dm.group(2)}-01" if dm else ""
            qm = re.search(r"q([1-4])[-_ ]?(?:fy)?[-_ ]?((?:19|20)?\d\d)", title, re.I)
            quarter = ""
            if qm:
                yr = qm.group(2)
                quarter = f"Q{qm.group(1)} {'20' + yr if len(yr) == 2 else yr}"
            cands.append(DeckCandidate(
                url=url, deck_type=dtype, title=title[:120], date=date, quarter=quarter,
                classification_confidence="explicit", source_anchor_text=title[:120]))
    cands.sort(key=lambda c: c.date or "", reverse=True)
    if verbose and cands:
        print(f"  [IR] direct PDF links: {len(cands)} found")
    return cands


def fetch_ir_slide_decks(
    ticker: str,
    *,
    types: list[str] | None = None,
    max_decks: int = 20,
    force: bool = False,
    use_browser: bool = True,
    verbose: bool = False,
) -> list[SlideDeck]:
    """
    Fetch investor decks / letters from the company's IR site.

    Args:
        ticker: stock ticker
        types: filter to these deck types. Default = all types.
            Valid: earnings | investor_day | conference | shareholder_letter | other
        max_decks: stop after downloading N decks (oldest dropped)
        force: bypass IR URL cache + deck cache
        use_browser: fall back to Playwright for CloudFlare-protected or
            JS-rendered IR pages that httpx can't reach. Default True.
        verbose: print progress

    Returns list in reverse chronological order (most recent first).
    """
    ticker = ticker.upper().strip()

    # 1. Find IR URL
    ir_result = find_ir_url(ticker, force=force, use_browser=use_browser, verbose=verbose)
    if not ir_result.verified or not ir_result.url:
        if verbose:
            print(f"  [IR] no IR URL for {ticker}: {ir_result.error}")
        return []
    if verbose:
        print(f"  [IR] using {ir_result.url} ({ir_result.method})")

    # 2. Probe IR root + common sub-pages. If URL discovery required the
    # browser (CloudFlare / JS-rendered), sub-page probes must also use
    # the browser — otherwise httpx returns the same JS shell for every
    # route and the classifier sees duplicate content.
    pages = _probe_subpages(
        ir_result.url,
        use_browser=use_browser,
        force_browser_mode=ir_result.was_browser_needed,
        verbose=verbose,
    )
    if not pages:
        if verbose:
            print(f"  [IR] no probeable pages under {ir_result.url}")
        return []

    # 2b. Discover events/presentations pages from the nav of what we already
    #     have, and probe any we haven't seen. Decks often live behind arbitrary
    #     paths (e.g. /stock-and-financial/events-and-presentations) that fixed-
    #     suffix guessing misses; the nav links straight to them.
    try:
        from ingestion.loaders._browser_fetch import fetch_html_with_browser
        seen_urls = {u.rstrip("/") for u, _ in pages}
        discovered: list[str] = []
        for _u, _html in list(pages):
            for d in _discover_deck_pages(_html, ir_result.url):
                if d.rstrip("/") not in seen_urls and d not in discovered:
                    discovered.append(d)
        for d in discovered[:6]:
            if d.rstrip("/") in seen_urls:
                continue
            html = fetch_html_with_browser(d, verbose=verbose) if use_browser else None
            if html and len(html) > 1000:
                pages.append((d, html))
                seen_urls.add(d.rstrip("/"))
                if verbose:
                    print(f"  [IR] discovered + probed {d}: {len(html):,} chars")
    except Exception as e:
        if verbose:
            print(f"  [IR] nav-discovery skipped: {type(e).__name__}")

    # 2c. Also probe the OTHER common IR domain variant. Sometimes the real
    #     decks live on bare.com/investors/ even when investors.bare.com exists
    #     (ORLA), or vice-versa. Cheap insurance against picking the wrong host;
    #     the classifier dedupes, so extra pages only help.
    try:
        from urllib.parse import urlparse
        from ingestion.loaders._browser_fetch import fetch_html_with_browser
        host = urlparse(ir_result.url).netloc.lower()
        bare = re.sub(r"^(www\.|ir\.|investors?\.|investorrelations\.)", "", host)
        if host == bare or host.startswith("www."):
            alts = [f"https://investors.{bare}/", f"https://ir.{bare}/"]
        else:
            alts = [f"https://{bare}/investors/", f"https://www.{bare}/investors/",
                    f"https://{bare}/investor-relations/"]
        seen_urls = {u.rstrip("/") for u, _ in pages}
        for alt in alts:
            if alt.rstrip("/") in seen_urls or not use_browser:
                continue
            html = fetch_html_with_browser(alt, verbose=verbose)
            if not html or len(html) < 1000:
                continue
            pages.append((alt, html))
            seen_urls.add(alt.rstrip("/"))
            if verbose:
                print(f"  [IR] probed alt-variant {alt}: {len(html):,} chars")
            for d in _discover_deck_pages(html, alt)[:4]:
                if d.rstrip("/") in seen_urls:
                    continue
                h2 = fetch_html_with_browser(d, verbose=verbose)
                if h2 and len(h2) > 1000:
                    pages.append((d, h2))
                    seen_urls.add(d.rstrip("/"))
                    if verbose:
                        print(f"  [IR] discovered + probed {d}: {len(h2):,} chars")
    except Exception as e:
        if verbose:
            print(f"  [IR] alt-variant probe skipped: {type(e).__name__}")

    # 3. First try DIRECT extraction — pages that list deck PDFs as plain
    #    <a href="...pdf"> links (Jollibee's presentations page) need no Claude
    #    call. Only fall back to the classifier when decks hide behind JS / non-
    #    .pdf links. Bonus: the classifier is no longer a single point of failure
    #    (it HTTP 529'd on Jollibee).
    all_candidates = _extract_direct_pdf_links(pages, ir_result.url, verbose=verbose)
    if all_candidates:
        if verbose:
            print(f"  [IR] direct PDF extraction: {len(all_candidates)} deck link(s) "
                  f"— classifier skipped")
    else:
        # Concatenate all probed page HTML into one blob and classify in ONE
        # Claude call (per-page calls hit rate limits + duplicate candidates).
        combined_html_parts = []
        for page_url, html in pages:
            combined_html_parts.append(
                f"\n\n<!-- BEGIN SUBPAGE: {page_url} -->\n{_link_catalog(html)}\n<!-- END SUBPAGE -->\n")
        combined_html = "".join(combined_html_parts)
        all_candidates = classify_ir_page(
            ticker, ir_result.url, combined_html, verbose=verbose,
        )
        if verbose:
            print(f"  [IR] classifier returned {len(all_candidates)} candidates from combined HTML")

    # Post-classifier blocklist: kill items that aren't real slide decks
    # regardless of what the classifier said. Prompt compliance is unreliable;
    # regex is deterministic.
    unique: list[DeckCandidate] = []
    dropped = 0
    for c in all_candidates:
        if _is_non_deck(c):
            dropped += 1
            if verbose:
                print(f"  [IR] drop non-deck: {c.title[:60]} ({c.url.rsplit('/',1)[-1][:40]})")
            continue
        unique.append(c)
    if verbose and dropped:
        print(f"  [IR] blocklist dropped {dropped} non-deck items")

    # Filter by requested types
    if types:
        wanted = set(types)
        unique = [c for c in unique if c.deck_type in wanted]

    # Sort by date desc (best effort; missing dates go last)
    unique.sort(key=lambda c: c.date or "0000-00-00", reverse=True)

    if verbose:
        print(f"  [IR] {len(unique)} unique deck candidates across {len(pages)} pages")
        type_counts = {}
        for c in unique:
            type_counts[c.deck_type] = type_counts.get(c.deck_type, 0) + 1
        for t, n in sorted(type_counts.items()):
            print(f"       {t:<22} {n}")

    # 4. Download + parse each (up to max_decks). Pass through the browser
    # flag — CloudFlare sites gate the PDFs themselves, not just the pages.
    results: list[SlideDeck] = []
    for c in unique[:max_decks]:
        deck = _candidate_to_slidedeck(
            ticker, c, use_browser=use_browser, verbose=verbose,
        )
        if deck is not None:
            results.append(deck)

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m ingestion.loaders.ir_page_deck_loader",
        description="Fetch investor decks + letters from a company's IR page.",
    )
    ap.add_argument("ticker")
    ap.add_argument("--types", type=str, default="",
                    help="Comma-separated filter: earnings,investor_day,conference,shareholder_letter,other (default: all)")
    ap.add_argument("--max-decks", type=int, default=20)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-browser", action="store_true",
                    help="Disable Playwright fallback (httpx only). Default: auto-fallback on WAF/JS detection.")
    ap.add_argument("--show-page", type=int, default=None,
                    help="Print page N of the most recent deck")
    args = ap.parse_args()

    types = [t.strip() for t in args.types.split(",") if t.strip()] or None

    decks = fetch_ir_slide_decks(
        args.ticker,
        types=types,
        max_decks=args.max_decks,
        force=args.force,
        use_browser=not args.no_browser,
        verbose=args.verbose,
    )
    if not decks:
        print(f"No IR decks found for {args.ticker}")
        return 2

    print()
    print(f"=== {args.ticker}: {len(decks)} IR decks ===")
    print()
    for d in decks:
        q = d.quarter or d.event_metadata.get("event_year") or d.event_metadata.get("period") or "-"
        print(f"  {d.deck_type:<20} {q!s:<12} {d.report_date or '-':<12} "
              f"{d.page_count:>3}p {d.total_tables:>2}t {d.total_chars:>6,}ch "
              f" {d.title[:50]}  ({d.classification_confidence})")

    if args.show_page is not None and decks:
        d = decks[0]
        pg_idx = args.show_page - 1
        if 0 <= pg_idx < len(d.pages):
            p = d.pages[pg_idx]
            print()
            print(f"--- Page {p.page_number} of {d.title} ({d.deck_type}) ---")
            print(p.text[:2000])
            for t in p.tables:
                print()
                print(t.markdown)

    return 0


if __name__ == "__main__":
    sys.exit(_main())
