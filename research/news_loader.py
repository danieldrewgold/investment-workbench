"""
News Loader.

Pulls recent financial news headlines on the ticker from APIs we already
pay for (Polygon + Alpha Vantage). Without this the brief misses material
events between earnings — exec departures, M&A, regulatory actions,
analyst rating changes, sector developments — that sit between quarterly
prints and aren't captured by transcripts/filings/decks.

Two sources, complementary:
  • Polygon /v2/reference/news     — clean, substantive publications
                                      (Benzinga, Motley Fool, Reuters)
                                      ~5-10 items per request
  • Alpha Vantage NEWS_SENTIMENT   — high-volume aggregator (50 items),
                                      includes sentiment scores per
                                      article + per-ticker

Combine + dedupe by URL. Filter price-action noise ("Stock Moved Down
3% on May 4") that adds no analytical signal.

Public API:
    fetch_news(ticker, days_back=90, verbose=False) -> NewsBundle

Cache: per-ticker daily.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Reuse already-configured keys from the financials loaders.
def _get_api_keys() -> tuple[str, str]:
    """Return (polygon_key, av_key). Falls back to env vars."""
    pk = ""
    ak = ""
    try:
        from ingestion.loaders.polygon_financials import POLYGON_API_KEY
        pk = POLYGON_API_KEY or ""
    except Exception:
        pk = os.environ.get("POLYGON_API_KEY", "")
    try:
        from ingestion.loaders.alpha_vantage_financials import AV_API_KEY
        ak = AV_API_KEY or ""
    except Exception:
        ak = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    return pk, ak


# Headlines that are pure price-action noise — skip them.
_NOISE_PATTERNS = [
    re.compile(r"\bMoved\s+(Up|Down)\s+by\b", re.IGNORECASE),
    re.compile(r"\bSignal Does It Send\b", re.IGNORECASE),
    re.compile(r"\b(top|best|worst)\s+\d+\s+stocks?\b", re.IGNORECASE),
    re.compile(r"\bstocks?\s+to\s+(watch|buy|avoid)\b", re.IGNORECASE),
    re.compile(r"\bWeekly\s+(Mover|Loser|Winner)s?\b", re.IGNORECASE),
    re.compile(r"\b(Pre|After)[-\s]?Market\s+(Movers|Trading)\b", re.IGNORECASE),
    re.compile(r"\bDividend\s+(Stock|History|Analysis)\b", re.IGNORECASE),
    re.compile(r"\bBook value per share\b", re.IGNORECASE),
    re.compile(r"\bClosed\s+(Up|Down)\s+by\b", re.IGNORECASE),
    re.compile(r"\bClosed at all-time\b", re.IGNORECASE),
    # Generic "what to expect" / "what to know" — usually thin
    re.compile(r"^What\s+(You\s+Need\s+To\s+Know|to\s+expect)", re.IGNORECASE),
]


# Patterns that mark MATERIAL-EVENT headlines: insider transactions, M&A,
# regulatory, exec changes, rating actions, capital actions, partnerships,
# litigation. These get prioritized when filtering for the brief context
# where prompt-budget is precious.
_MATERIAL_EVENT_PATTERNS = [
    # Insider transactions
    re.compile(r"\b(insider|director|CEO|CFO|COO|CTO|chairman)\s+(sold|sells|sell|"
               r"bought|buys|buy|purchases|purchased|grants?|grant)\b", re.IGNORECASE),
    re.compile(r"\b(form\s+4|form\s+144|13D|13G|13F)\b", re.IGNORECASE),
    re.compile(r"\b\$\d+(?:\.\d+)?\s*(?:million|M|billion|B)\s+(?:in|of)?\s*shares?\b", re.IGNORECASE),
    re.compile(r"\bsells?\s+\$\d+", re.IGNORECASE),
    re.compile(r"\bRSU\b|\brestricted stock\b|\bstock options?\b", re.IGNORECASE),
    # M&A / capital
    re.compile(r"\b(acquires?|acquisition|merger|merge|buyout|takeover|divest)\b", re.IGNORECASE),
    re.compile(r"\b(buyback|repurchase|tender offer|secondary offering|IPO)\b", re.IGNORECASE),
    re.compile(r"\bdebt offering|debt issuance|notes? offering\b", re.IGNORECASE),
    # Regulatory / legal
    re.compile(r"\b(SEC|FDA|FTC|DOJ|FCC)\s+(probe|investigat|approv|denies|warns?)", re.IGNORECASE),
    re.compile(r"\b(lawsuit|sued|settlement|fine|penalty|consent decree)\b", re.IGNORECASE),
    re.compile(r"\b(class action|complaint|allegations?)\b", re.IGNORECASE),
    # Exec changes
    re.compile(r"\b(CEO|CFO|COO|CTO|president|chairman)\s+(resigns|departing|"
               r"departure|step down|appoints?|named)\b", re.IGNORECASE),
    re.compile(r"\bappoints?\s+(new\s+)?(CEO|CFO|COO|CTO)\b", re.IGNORECASE),
    # Rating / analyst actions
    re.compile(r"\b(upgrade|downgrade|raises?\s+price\s+target|cuts?\s+price\s+target|"
               r"price\s+target\s+(raised|cut|lowered))\b", re.IGNORECASE),
    re.compile(r"\b(initiates?\s+coverage|reiterates?|maintains?)\b", re.IGNORECASE),
    # Partnerships / contracts
    re.compile(r"\b(announces?\s+partnership|signs?\s+(deal|agreement|contract)|"
               r"awards?\s+contract|wins?\s+contract)\b", re.IGNORECASE),
    # Earnings beats / guides
    re.compile(r"\b(beats?\s+(estimates|consensus|expectations)|raises?\s+guidance|"
               r"cuts?\s+guidance|preannounce|warns?\s+on)\b", re.IGNORECASE),
    # Restructuring
    re.compile(r"\b(layoffs?|restructuring|cost\s+(cuts|reduction)|workforce\s+reduction)\b", re.IGNORECASE),
]


def _is_noise(title: str) -> bool:
    """Filter out price-action / list-style headlines that add no signal."""
    for pat in _NOISE_PATTERNS:
        if pat.search(title):
            return True
    return False


def _is_material_event(item: "NewsItem") -> bool:
    """
    True if this article is a material-event headline (insider transaction,
    M&A, regulatory, exec change, rating action, etc.) that the brief
    should specifically attend to.
    """
    text = f"{item.title} {item.summary}"
    for pat in _MATERIAL_EVENT_PATTERNS:
        if pat.search(text):
            return True
    return False


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class NewsItem:
    """One news article."""
    ticker: str
    title: str
    source: str             # publisher name, e.g. "Benzinga" / "Reuters"
    url: str
    published: str          # ISO date YYYY-MM-DD
    summary: str = ""
    sentiment_label: str = ""    # bullish | somewhat-bullish | neutral | bearish | somewhat-bearish
    sentiment_score: float | None = None    # -1 to 1
    via: str = "polygon"    # "polygon" | "alphavantage"


@dataclass
class NewsBundle:
    ticker: str
    fetched_at: str = ""
    items: list = field(default_factory=list)   # list[NewsItem]

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "items": [asdict(i) if hasattr(i, "__dataclass_fields__") else i
                      for i in self.items],
        }

    def to_prompt_text(self, max_items: int = 12,
                       events_only: bool = False) -> str:
        """
        Render a compact news block for prompt injection. By default,
        prioritizes material-event items (insider transactions, M&A,
        regulatory actions, exec changes, rating actions) — these are
        rendered as a separate "MATERIAL EVENTS" subsection at the top.
        Other items follow as "OTHER NEWS".

        events_only=True omits the routine items entirely (used for
        the brief context where token budget is precious).
        """
        if not self.items:
            return ""

        material = [i for i in self.items if _is_material_event(i)]
        routine = [i for i in self.items if not _is_material_event(i)]
        # Sort each group by date desc
        material.sort(key=lambda x: x.published or "", reverse=True)
        routine.sort(key=lambda x: x.published or "", reverse=True)

        # Sentiment summary (across all items, not just rendered ones)
        bull = sum(1 for i in self.items
                   if "bull" in (i.sentiment_label or "").lower())
        bear = sum(1 for i in self.items
                   if "bear" in (i.sentiment_label or "").lower())
        neutral = sum(1 for i in self.items
                      if "neutral" in (i.sentiment_label or "").lower())
        sentiment_summary = ""
        if bull or bear or neutral:
            total = bull + bear + neutral
            sentiment_summary = (
                f" Sentiment mix (across all {len(self.items)} items): "
                f"{bull} bullish / {neutral} neutral / {bear} bearish."
            )

        lines = [
            f"=== RECENT NEWS — material events first; "
            f"{len(material)} material / {len(routine)} routine "
            f"({len(self.items)} total){sentiment_summary} ===",
            "(Material events: insider transactions, M&A, regulatory actions, "
            "exec changes, rating actions, capital actions, litigation, "
            "partnerships. The synthesis MUST surface anything material that "
            "affects the thesis — e.g. an insider sale of $X, an SEC probe, "
            "exec departure, downgrade, contract win. Routine items are "
            "background context.)",
            "",
        ]

        if material:
            lines.append(f"--- MATERIAL EVENTS ({len(material)}) ---")
            for item in material[:max_items]:
                sentiment_part = ""
                if item.sentiment_label:
                    sentiment_part = f"  [{item.sentiment_label}]"
                lines.append(f"• {item.published}  [{item.source}]{sentiment_part}")
                lines.append(f"  {item.title}")
                if item.summary and len(item.summary) > 30:
                    lines.append(f"  {item.summary[:280]}")
                lines.append(f"  {item.url}")
                lines.append("")

        # Routine — only render if requested AND room
        if not events_only and routine:
            remaining = max(0, max_items - len(material))
            if remaining > 0:
                lines.append(f"--- OTHER NEWS ({len(routine)}; showing {min(remaining, len(routine))}) ---")
                for item in routine[:remaining]:
                    sentiment_part = ""
                    if item.sentiment_label:
                        sentiment_part = f"  [{item.sentiment_label}]"
                    lines.append(f"• {item.published}  [{item.source}]{sentiment_part}: "
                                 f"{item.title[:140]}")
                    lines.append("")

        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache (daily — news is freshest of all corpora)
# --------------------------------------------------------------------------

_CACHE_DIR = Path("data/news_cache")
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}.json"


def _load_cache(ticker: str) -> NewsBundle | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        bundle = NewsBundle(
            ticker=d.get("ticker", ticker),
            fetched_at=d.get("fetched_at", ""),
        )
        known = {f for f in NewsItem.__dataclass_fields__}
        for it in d.get("items", []):
            bundle.items.append(NewsItem(**{k: v for k, v in it.items() if k in known}))
        return bundle
    except Exception:
        return None


def _save_cache(bundle: NewsBundle) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(bundle.ticker).write_text(
        json.dumps(bundle.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Polygon news
# --------------------------------------------------------------------------

def _fetch_polygon_news(ticker: str, polygon_key: str, days_back: int,
                        verbose: bool = False) -> list:
    """Fetch from Polygon /v2/reference/news. Returns list of NewsItem."""
    if not polygon_key:
        return []
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/reference/news",
            params={
                "ticker": ticker.upper(),
                "order": "desc",
                "limit": 50,
                "published_utc.gte": cutoff,
                "apiKey": polygon_key,
            },
            timeout=20.0,
        )
        if r.status_code != 200:
            if verbose:
                print(f"    Polygon news: HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        if verbose:
            print(f"    Polygon news: {type(e).__name__}: {e}")
        return []

    items = []
    for art in data.get("results", []):
        title = (art.get("title") or "").strip()
        if not title or _is_noise(title):
            continue
        pub_utc = (art.get("published_utc") or "")[:10]
        publisher = (art.get("publisher") or {}).get("name") or "Polygon"
        items.append(NewsItem(
            ticker=ticker.upper(),
            title=title,
            source=publisher,
            url=art.get("article_url") or "",
            published=pub_utc,
            summary=(art.get("description") or "")[:400],
            via="polygon",
        ))
    if verbose:
        print(f"    Polygon: {len(items)} item(s) (after noise filter)")
    return items


# --------------------------------------------------------------------------
# Alpha Vantage NEWS_SENTIMENT
# --------------------------------------------------------------------------

def _parse_av_time(t: str) -> str:
    """AV uses YYYYMMDDTHHMMSS — convert to YYYY-MM-DD."""
    if not t or len(t) < 8:
        return ""
    return f"{t[0:4]}-{t[4:6]}-{t[6:8]}"


def _fetch_av_news(ticker: str, av_key: str, days_back: int,
                   verbose: bool = False) -> list:
    """Fetch from Alpha Vantage NEWS_SENTIMENT. Includes sentiment scores."""
    if not av_key:
        return []
    cutoff_dt = datetime.now() - timedelta(days=days_back)
    cutoff_str = cutoff_dt.strftime("%Y%m%dT0000")
    try:
        r = httpx.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": ticker.upper(),
                "limit": 50,
                "time_from": cutoff_str,
                "apikey": av_key,
                "sort": "LATEST",
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            if verbose:
                print(f"    AV news: HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        if verbose:
            print(f"    AV news: {type(e).__name__}: {e}")
        return []

    if "Note" in data or "Information" in data:
        # AV rate-limit messages
        if verbose:
            print(f"    AV news: rate limited / quota: "
                  f"{data.get('Information') or data.get('Note')}")
        return []

    items = []
    for art in data.get("feed", []):
        title = (art.get("title") or "").strip()
        if not title or _is_noise(title):
            continue
        # Find ticker-specific sentiment if available
        ticker_sent_label = ""
        ticker_sent_score = None
        for ts in art.get("ticker_sentiment", []):
            if (ts.get("ticker") or "").upper() == ticker.upper():
                ticker_sent_label = ts.get("ticker_sentiment_label") or ""
                try:
                    ticker_sent_score = float(ts.get("ticker_sentiment_score", 0))
                except (TypeError, ValueError):
                    pass
                break
        # Fallback to article-level sentiment
        if not ticker_sent_label:
            ticker_sent_label = art.get("overall_sentiment_label") or ""
            try:
                ticker_sent_score = float(art.get("overall_sentiment_score", 0))
            except (TypeError, ValueError):
                pass

        items.append(NewsItem(
            ticker=ticker.upper(),
            title=title,
            source=art.get("source") or "AV",
            url=art.get("url") or "",
            published=_parse_av_time(art.get("time_published") or ""),
            summary=(art.get("summary") or "")[:400],
            sentiment_label=ticker_sent_label.lower(),
            sentiment_score=ticker_sent_score,
            via="alphavantage",
        ))
    if verbose:
        print(f"    AV: {len(items)} item(s) (after noise filter)")
    return items


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def fetch_news(ticker: str, *, days_back: int = 90,
               verbose: bool = False, force_refresh: bool = False,
               max_items: int = 30) -> NewsBundle:
    """
    Aggregate news from Polygon + Alpha Vantage. Dedupes by URL,
    drops noise headlines, sorts by recency.
    """
    ticker = ticker.upper().strip()

    if not force_refresh:
        cached = _load_cache(ticker)
        if cached:
            if verbose:
                print(f"  News: cache hit ({len(cached.items)} items, "
                      f"fetched {cached.fetched_at})")
            return cached

    polygon_key, av_key = _get_api_keys()

    bundle = NewsBundle(
        ticker=ticker,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    if verbose:
        print(f"  News: fetching Polygon + AV for {ticker}...")
    polygon_items = _fetch_polygon_news(ticker, polygon_key, days_back, verbose=verbose)
    av_items = _fetch_av_news(ticker, av_key, days_back, verbose=verbose)

    # Combine + dedupe by URL
    seen_urls = set()
    combined = []
    for item in polygon_items + av_items:
        u = (item.url or "").strip()
        if not u:
            continue
        if u in seen_urls:
            continue
        seen_urls.add(u)
        combined.append(item)

    # Sort by published date descending (most recent first)
    combined.sort(key=lambda x: x.published or "", reverse=True)

    # Cap at max_items
    bundle.items = combined[:max_items]

    try:
        _save_cache(bundle)
    except Exception as e:
        if verbose:
            print(f"  News: cache save failed: {e}")

    if verbose:
        bull = sum(1 for i in bundle.items if "bull" in (i.sentiment_label or "").lower())
        bear = sum(1 for i in bundle.items if "bear" in (i.sentiment_label or "").lower())
        print(f"  News: {len(bundle.items)} item(s) total "
              f"(Polygon: {len(polygon_items)}, AV: {len(av_items)}; "
              f"sentiment: {bull} bull / {bear} bear)")

    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch recent news for a ticker")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--max", type=int, default=20)
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    bundle = fetch_news(
        args.ticker, days_back=args.days,
        max_items=args.max,
        verbose=True, force_refresh=args.refresh,
    )
    print()
    print(bundle.to_prompt_text(max_items=args.max))


if __name__ == "__main__":
    _main()
