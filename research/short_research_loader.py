"""
Short-Research / Bear-Thesis Loader.

Pulls public bear-case research from major short-seller research firms
and surfaces it as a labeled corpus source for the brief. Without this
input the pipeline can produce briefs that miss the contrarian view —
e.g. an APP brief with no awareness of Fuzzy Panda's "Formers Allege
Ad Fraud" report or Culper's allegations on traffic quality.

Sources:
  • Fuzzy Panda Research      — listing page parseable, titles include ticker
  • Spruce Point Capital      — listing page parseable, slugs are company names
  • Hindenburg Research       — home page parseable, archive of historical reports
  • Wolfpack Research          — listing page parseable
  • DuckDuckGo search fallback — for sites that block direct scraping
    (Culper, Iceberg, Muddy Waters)

Public API:
    fetch_short_research(ticker, *, company_name=None, verbose=False,
                         force_refresh=False) -> ShortResearchBundle

The bundle's `to_prompt_text()` produces a labeled block ready to inject
into the brief prompt.

Cache: per-ticker per-week. Short reports are sticky — old reports remain
relevant if claims weren't refuted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Browser-like UA defeats most non-CF rate limiting; sites still 403'ing
# fall through to DDG search.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Sites we attempt to scrape directly (each has a parser below).
# Sites NOT in this list rely on DDG search fallback.
_DIRECT_SITES = [
    "fuzzypanda",
    "sprucepoint",
    "hindenburg",
    "wolfpack",
]

# Domains that are known short-research producers. Used to filter DDG
# results to relevant sources only.
_SHORT_RESEARCH_DOMAINS = {
    "hindenburgresearch.com",
    "fuzzypandaresearch.com",
    "culperresearch.com",
    "sprucepointcap.com",
    "muddywatersresearch.com",
    "iceberg-research.com",
    "wolfpackresearch.com",
    "kerrisdalecap.com",
    "citronresearch.com",
    "viceroyresearch.org",
    "scorpioncapital.com",
    "shortintelligencegroup.com",
    "marcuscampagnoli.com",
    "blueorcacapital.com",
    "rotaresearch.com",
}


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class ShortReport:
    """One bear-case research report."""
    source: str             # e.g. "Fuzzy Panda" / "Spruce Point" / "Hindenburg"
    ticker: str
    title: str
    url: str
    publish_date: str = ""  # YYYY-MM-DD if extractable
    summary: str = ""       # excerpt or first-paragraph snippet
    via: str = "direct"     # "direct" | "search" — source of discovery


@dataclass
class ShortResearchBundle:
    ticker: str
    fetched_at: str = ""
    reports: list = field(default_factory=list)   # list[ShortReport]
    sources_attempted: list = field(default_factory=list)  # which sites we tried
    sources_blocked: list = field(default_factory=list)    # which sites returned 403/blocked

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "sources_attempted": self.sources_attempted,
            "sources_blocked": self.sources_blocked,
            "reports": [asdict(r) if hasattr(r, "__dataclass_fields__") else r
                        for r in self.reports],
        }

    def to_prompt_text(self) -> str:
        """Render as a labeled corpus block for the brief prompt."""
        if not self.reports:
            return ""
        lines = [
            "=== BEAR-CASE / SHORT-SELLER RESEARCH ===",
            f"({len(self.reports)} report(s) found across major short-research "
            f"firms — these are PUBLIC bear theses on this name. Treat as one "
            f"perspective, not gospel; weigh evidence against the rest of the "
            f"corpus. Acknowledge the bear case in the synthesis where the "
            f"specific claims are credible.)",
        ]
        for r in self.reports[:6]:
            lines.append("")
            date_part = f" ({r.publish_date})" if r.publish_date else ""
            lines.append(f"--- {r.source}{date_part} ---")
            lines.append(f"Title: {r.title}")
            if r.summary:
                lines.append(f"Excerpt: {r.summary[:600]}")
            lines.append(f"URL: {r.url}")
        if self.sources_blocked:
            lines.append("")
            lines.append(f"Note — blocked sources (403/Cloudflare): "
                         f"{', '.join(self.sources_blocked)}. "
                         f"Some bear research from these firms may exist but "
                         f"could not be retrieved directly.")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

_CACHE_DIR = Path("data/short_research_cache")
_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60   # weekly refresh — bear reports are sticky


def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}.json"


def _load_cache(ticker: str) -> ShortResearchBundle | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        bundle = ShortResearchBundle(
            ticker=d.get("ticker", ticker),
            fetched_at=d.get("fetched_at", ""),
            sources_attempted=d.get("sources_attempted", []),
            sources_blocked=d.get("sources_blocked", []),
        )
        known = {f for f in ShortReport.__dataclass_fields__}
        for rd in d.get("reports", []):
            bundle.reports.append(
                ShortReport(**{k: v for k, v in rd.items() if k in known})
            )
        return bundle
    except Exception:
        return None


def _save_cache(bundle: ShortResearchBundle) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(bundle.ticker).write_text(
        json.dumps(bundle.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Per-site scrapers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _ticker_in_text(ticker: str, company_name: str | None, text: str) -> bool:
    """Check whether text references the ticker or company name."""
    text_l = text.lower()
    # Word-boundary ticker match (avoid matching APP inside "happen")
    if re.search(rf"\b{re.escape(ticker.upper())}\b", text):
        return True
    if re.search(rf"\b{re.escape(ticker.upper())}\b", text_l, re.IGNORECASE):
        return True
    # Company name fallback (looser)
    if company_name and len(company_name) >= 4 and company_name.lower() in text_l:
        return True
    return False


def _fetch(url: str, timeout: float = 20.0) -> tuple[int, str]:
    """Single GET with browser UA. Returns (status, text)."""
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=timeout,
                       follow_redirects=True)
        return r.status_code, r.text
    except Exception:
        return 0, ""


def _scrape_fuzzy_panda(ticker: str, company_name: str | None,
                        verbose: bool = False) -> tuple[list, bool]:
    """
    Fuzzy Panda's research listing page lists posts with ticker in titles.
    Each post has a `<a title="...">` containing the report headline and
    a snippet near the link with the thesis.
    Returns (reports_list, blocked_bool).
    """
    url = "https://fuzzypandaresearch.com/research/"
    status, text = _fetch(url)
    if status != 200 or not text:
        return [], True
    reports = []
    # Find all <a title="..." href="..."> entries — each is a report card
    for m in re.finditer(
        r'<a[^>]+title="([^"]*)"[^>]+href="(https://fuzzypandaresearch\.com/[^"]+)"',
        text,
    ):
        title = m.group(1).strip()
        href = m.group(2)
        if not _ticker_in_text(ticker, company_name, title):
            continue
        # Get a snippet from the surrounding text
        ctx_start = max(0, m.start() - 200)
        ctx_end = min(len(text), m.end() + 1500)
        ctx = _strip_html(text[ctx_start:ctx_end])
        # Extract the bear-thesis sentence (usually starts with "We are short")
        snippet = ""
        m_short = re.search(
            r"(We are short[^.]+\.[^.]*\.[^.]*\.)", ctx, re.IGNORECASE,
        )
        if m_short:
            snippet = m_short.group(1).strip()[:600]
        else:
            snippet = ctx[:400]
        # De-dup by URL
        if any(r.url == href for r in reports):
            continue
        # Date extraction — Fuzzy Panda URLs sometimes contain dates,
        # otherwise leave blank
        date = ""
        m_date = re.search(r"(\d{4})-(\d{2})-(\d{2})", href)
        if m_date:
            date = f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}"
        reports.append(ShortReport(
            source="Fuzzy Panda",
            ticker=ticker.upper(),
            title=title,
            url=href,
            publish_date=date,
            summary=snippet,
            via="direct",
        ))
    if verbose:
        print(f"    Fuzzy Panda: {len(reports)} match(es)")
    return reports, False


def _scrape_spruce_point(ticker: str, company_name: str | None,
                         verbose: bool = False) -> tuple[list, bool]:
    """
    Spruce Point lists reports with company-name slugs.
    e.g. /research/zoom-communications-inc — no ticker in URL.
    Need to match by company_name. The page title near each link
    sometimes carries the ticker.
    """
    url = "https://www.sprucepointcap.com/research/"
    status, text = _fetch(url)
    if status != 200 or not text:
        return [], True
    reports = []
    # Find each report block — typically a card with a heading + ticker + link.
    # Spruce uses "/research/<slug>" pattern
    for m in re.finditer(r'<a[^>]+href="(/research/[^"]+)"[^>]*>([^<]+)</a>', text):
        href = m.group(1)
        link_text = m.group(2).strip()
        # The block surrounding this link usually has the company name + ticker
        ctx_start = max(0, m.start() - 1200)
        ctx_end = min(len(text), m.end() + 200)
        ctx = _strip_html(text[ctx_start:ctx_end])
        if not _ticker_in_text(ticker, company_name, ctx):
            continue
        # Slug-derived title fallback
        slug = href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        title = slug
        # Date — Spruce sometimes puts dates in surrounding text
        date = ""
        m_date = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\b[A-Z][a-z]+\s+\d{1,2},\s*\d{4})", ctx)
        if m_date:
            date = m_date.group(1)
        full_url = f"https://www.sprucepointcap.com{href}"
        if any(r.url == full_url for r in reports):
            continue
        reports.append(ShortReport(
            source="Spruce Point",
            ticker=ticker.upper(),
            title=title,
            url=full_url,
            publish_date=date,
            summary=ctx[:400],
            via="direct",
        ))
    if verbose:
        print(f"    Spruce Point: {len(reports)} match(es)")
    return reports, False


def _scrape_hindenburg(ticker: str, company_name: str | None,
                       verbose: bool = False) -> tuple[list, bool]:
    """
    Hindenburg homepage lists their reports (they wound down active short-
    selling in 2025 but archive remains useful for historical context).
    Reports are linked at /research/<slug>/.
    """
    url = "https://hindenburgresearch.com/"
    status, text = _fetch(url)
    if status != 200 or not text:
        return [], True
    reports = []
    for m in re.finditer(
        r'<a[^>]+href="(https://hindenburgresearch\.com/[^"]+)"[^>]*>([^<]{5,200})</a>',
        text,
    ):
        href = m.group(1)
        link_text = m.group(2).strip()
        # Surrounding context
        ctx_start = max(0, m.start() - 400)
        ctx_end = min(len(text), m.end() + 600)
        ctx = _strip_html(text[ctx_start:ctx_end])
        if not _ticker_in_text(ticker, company_name, ctx):
            continue
        # Skip nav links / category pages
        if any(skip in href.lower() for skip in
               ("/about", "/contact", "/category", "/page/", "/disclaimer",
                "facebook.com", "twitter.com", "linkedin.com")):
            continue
        if any(r.url == href for r in reports):
            continue
        date = ""
        m_date = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s*\d{4})", ctx)
        if m_date:
            date = m_date.group(1)
        reports.append(ShortReport(
            source="Hindenburg",
            ticker=ticker.upper(),
            title=link_text or "Hindenburg Report",
            url=href,
            publish_date=date,
            summary=ctx[:400],
            via="direct",
        ))
    if verbose:
        print(f"    Hindenburg: {len(reports)} match(es)")
    return reports, False


def _scrape_wolfpack(ticker: str, company_name: str | None,
                     verbose: bool = False) -> tuple[list, bool]:
    """Wolfpack home page lists their research."""
    url = "https://www.wolfpackresearch.com/"
    status, text = _fetch(url)
    if status != 200 or not text:
        return [], True
    reports = []
    for m in re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*>([^<]{5,200})</a>',
        text,
    ):
        href = m.group(1)
        link_text = m.group(2).strip()
        # Filter to research-page-looking links
        if "research" not in href.lower() and "report" not in href.lower():
            continue
        ctx_start = max(0, m.start() - 300)
        ctx_end = min(len(text), m.end() + 500)
        ctx = _strip_html(text[ctx_start:ctx_end])
        if not _ticker_in_text(ticker, company_name, ctx):
            continue
        if any(r.url == href for r in reports):
            continue
        reports.append(ShortReport(
            source="Wolfpack",
            ticker=ticker.upper(),
            title=link_text or "Wolfpack Report",
            url=href if href.startswith("http") else f"https://www.wolfpackresearch.com{href}",
            summary=ctx[:400],
            via="direct",
        ))
    if verbose:
        print(f"    Wolfpack: {len(reports)} match(es)")
    return reports, False


# --------------------------------------------------------------------------
# DuckDuckGo search fallback (for blocked sites)
# --------------------------------------------------------------------------

def _decode_ddg_redirect(url: str) -> str:
    """
    DuckDuckGo wraps result URLs in a redirect:
      //duckduckgo.com/l/?uddg=<URL-encoded real URL>&...
    Extract the real URL when present; else return the original.
    """
    if "duckduckgo.com/l/" not in url:
        return url
    m = re.search(r"uddg=([^&]+)", url)
    if not m:
        return url
    try:
        from urllib.parse import unquote
        return unquote(m.group(1))
    except Exception:
        return url


def _ddg_search(ticker: str, company_name: str | None,
                verbose: bool = False) -> list:
    """
    Search DuckDuckGo for ticker + short-research domain mentions.
    Returns list of ShortReport entries built from search hit metadata
    (no body fetch — title + snippet from the SERP only).
    """
    queries = [
        f"{ticker} short report culper research",
        f"{ticker} short report iceberg research",
        f"{ticker} short report muddy waters",
        f"{ticker} short seller research",
    ]
    if company_name and len(company_name) >= 4:
        queries.append(f'"{company_name}" short report')

    found = []
    seen_urls = set()
    for q in queries:
        try:
            r = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": q},
                headers={**_HEADERS},
                timeout=15.0,
                follow_redirects=True,
            )
            if r.status_code != 200:
                continue
            text = r.text
        except Exception:
            continue

        # DDG result blocks: each has class="result"
        for m in re.finditer(
            r'class="result__url"[^>]*>([^<]+)<.*?'
            r'class="result__a"[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
            r'.*?class="result__snippet"[^>]*>(.*?)</a>',
            text, re.DOTALL,
        ):
            domain = m.group(1).strip().lower()
            url = _decode_ddg_redirect(m.group(2).strip())
            title = _strip_html(m.group(3))
            snippet = _strip_html(m.group(4))[:500]

            # Filter: must be from a known short-research domain
            if not any(d in domain for d in _SHORT_RESEARCH_DOMAINS):
                continue
            if url in seen_urls:
                continue
            # And must mention the ticker/company in title or snippet
            combined = f"{title} {snippet}"
            if not _ticker_in_text(ticker, company_name, combined):
                continue

            # Source label — pick the matching domain
            source = "Other"
            for d in _SHORT_RESEARCH_DOMAINS:
                if d in domain:
                    source = (d
                              .replace("research", " Research")
                              .replace("capital", " Capital")
                              .replace("cap", " Capital")
                              .replace(".com", "")
                              .replace(".org", "")
                              .replace("-", " ")
                              .title()
                              .strip())
                    break
            seen_urls.add(url)
            found.append(ShortReport(
                source=source,
                ticker=ticker.upper(),
                title=title or "Short report",
                url=url,
                summary=snippet,
                via="search",
            ))
        time.sleep(0.6)
    if verbose:
        print(f"    DDG search: {len(found)} result(s)")
    return found


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def fetch_short_research(
    ticker: str,
    *,
    company_name: str | None = None,
    verbose: bool = False,
    force_refresh: bool = False,
) -> ShortResearchBundle:
    """
    Aggregate bear-case research across the major short-seller sites.
    Returns a bundle with reports + cache metadata; cache hits unless
    `force_refresh` or stale.
    """
    ticker = ticker.upper().strip()

    if not force_refresh:
        cached = _load_cache(ticker)
        if cached:
            if verbose:
                print(f"  Short research: cache hit "
                      f"({len(cached.reports)} report(s), fetched "
                      f"{cached.fetched_at})")
            return cached

    bundle = ShortResearchBundle(
        ticker=ticker,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    # Direct site scrapers
    direct_scrapers = [
        ("Fuzzy Panda",   _scrape_fuzzy_panda),
        ("Spruce Point",  _scrape_spruce_point),
        ("Hindenburg",    _scrape_hindenburg),
        ("Wolfpack",      _scrape_wolfpack),
    ]
    for name, fn in direct_scrapers:
        if verbose:
            print(f"  Short research: scanning {name}...")
        bundle.sources_attempted.append(name)
        try:
            reports, blocked = fn(ticker, company_name, verbose=verbose)
            if blocked:
                bundle.sources_blocked.append(name)
            else:
                bundle.reports.extend(reports)
        except Exception as e:
            if verbose:
                print(f"    {name} failed: {type(e).__name__}: {e}")
            bundle.sources_blocked.append(name)
        # Polite delay between sites
        time.sleep(0.4)

    # DDG search fallback (covers Culper, Iceberg, Muddy Waters which block direct)
    if verbose:
        print(f"  Short research: searching DDG for blocked-site reports...")
    bundle.sources_attempted.append("DuckDuckGo Fallback")
    try:
        ddg_reports = _ddg_search(ticker, company_name, verbose=verbose)
        # Dedupe against direct hits
        existing_urls = {r.url for r in bundle.reports}
        for r in ddg_reports:
            if r.url not in existing_urls:
                bundle.reports.append(r)
                existing_urls.add(r.url)
    except Exception as e:
        if verbose:
            print(f"    DDG search failed: {type(e).__name__}: {e}")

    # Sort: most recent first (when date available), then by source
    def _sort_key(r):
        # Reports with dates rank ahead of those without; newer ahead of older
        return (0 if r.publish_date else 1, -ord(r.source[0]) if r.source else 0)
    bundle.reports.sort(key=_sort_key)

    try:
        _save_cache(bundle)
    except Exception as e:
        if verbose:
            print(f"  Short research: cache save failed: {e}")

    if verbose:
        print(f"  Short research: {len(bundle.reports)} report(s) found "
              f"across {len(bundle.sources_attempted)} sources "
              f"({len(bundle.sources_blocked)} blocked)")

    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch bear-case short research")
    p.add_argument("ticker")
    p.add_argument("--company", help="Company name for fuzzy matching")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    bundle = fetch_short_research(
        args.ticker,
        company_name=args.company,
        verbose=True,
        force_refresh=args.refresh,
    )
    print()
    print(bundle.to_prompt_text())


if __name__ == "__main__":
    _main()
