"""
StockTwits Retail Dialogue Loader

Pulls the most recent ticker-tagged messages from StockTwits' public API.
StockTwits is locked behind Cloudflare for scripted access, so we go
through the existing headless-browser fetch helper.

Strict framing in the corpus block: this is RETAIL OPINIONS, not facts.
The brief should treat each surfaced topic as a research question to
investigate independently (the claim_verifier pass will then web-search
substantive claims). The block header carries this framing explicitly so
Claude doesn't cite StockTwits posts as authoritative sources.

Cache: daily per ticker. StockTwits is high-volume; one snapshot per day
is plenty for orientation purposes.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# StockTwits public messages stream by ticker
STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"

# Cache (daily — retail dialogue moves fast but per-day snapshot is enough)
_CACHE_DIR = Path("data/stocktwits_cache")
_CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class StockTwitsMessage:
    """One retail message from StockTwits."""
    id: int = 0
    created_at: str = ""
    body: str = ""
    username: str = ""
    user_followers: int = 0
    user_official: bool = False           # platform-verified status (rare)
    user_classification: str = ""         # "suggested", "official", or empty
    sentiment: str = ""                   # "Bullish" | "Bearish" | "" (unlabeled)
    reshares: int = 0
    likes: int = 0
    engagement: int = 0                   # reshares + likes (combined)
    mentioned_users: list = field(default_factory=list)
    other_symbols: list = field(default_factory=list)   # other tickers tagged

    @property
    def is_high_signal(self) -> bool:
        """Heuristic: does this message look like substantive retail
        commentary vs. cashtag-spam pump?"""
        body_lower = self.body.lower()
        # Length floor — one-liners like "$TMDX MOON" are noise
        if len(self.body) < 60:
            return False
        # Spam pattern: heavy on emojis, no claims
        if self.body.count("🚀") + self.body.count("📈") + self.body.count("💎") >= 3:
            return False
        # Pump pattern: ALL CAPS plus exclamation marks
        if self.body.upper() == self.body and self.body.count("!") >= 2:
            return False
        # Want at least some engagement OR a verified-tier author
        if self.engagement < 2 and not self.user_official and self.user_followers < 200:
            return False
        return True


@dataclass
class StockTwitsBundle:
    ticker: str = ""
    fetched_at: str = ""
    messages: list = field(default_factory=list)         # list[StockTwitsMessage]
    sentiment_bull: int = 0
    sentiment_bear: int = 0
    sentiment_none: int = 0
    n_total: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "n_total": self.n_total,
            "sentiment_bull": self.sentiment_bull,
            "sentiment_bear": self.sentiment_bear,
            "sentiment_none": self.sentiment_none,
            "messages": [asdict(m) if hasattr(m, "__dataclass_fields__") else m
                          for m in self.messages],
            "error": self.error,
        }

    def to_prompt_text(self, max_messages: int = 18) -> str:
        """
        Render a labeled corpus block. Framing is deliberate: retail
        opinions, not facts. The block tells Claude to use this as topic
        radar, not source-of-truth.
        """
        if not self.messages:
            return ""

        # Pick top messages by engagement, but ensure both sentiment sides
        # are represented. Otherwise a heavily-bullish stream will swallow
        # the bear case (or vice versa).
        bulls = sorted(
            [m for m in self.messages if m.sentiment == "Bullish" and m.is_high_signal],
            key=lambda m: -m.engagement,
        )
        bears = sorted(
            [m for m in self.messages if m.sentiment == "Bearish" and m.is_high_signal],
            key=lambda m: -m.engagement,
        )
        neutral = sorted(
            [m for m in self.messages
             if m.sentiment not in ("Bullish", "Bearish") and m.is_high_signal],
            key=lambda m: -m.engagement,
        )
        # Round-robin pick to keep both sides visible
        picks: list[StockTwitsMessage] = []
        i = 0
        while len(picks) < max_messages and (i < len(bulls) or i < len(bears) or i < len(neutral)):
            if i < len(bulls):
                picks.append(bulls[i])
            if len(picks) < max_messages and i < len(bears):
                picks.append(bears[i])
            if len(picks) < max_messages and i < len(neutral):
                picks.append(neutral[i])
            i += 1

        # Sentiment summary
        total = self.sentiment_bull + self.sentiment_bear + self.sentiment_none
        if total > 0:
            bull_pct = self.sentiment_bull * 100 / total
            bear_pct = self.sentiment_bear * 100 / total
        else:
            bull_pct = bear_pct = 0.0

        lines = [
            f"=== STOCKTWITS RETAIL DIALOGUE (${self.ticker}) ===",
            "(THESE ARE RETAIL OPINIONS, NOT FACTS. StockTwits skews retail / "
            "speculative; sell-side analysts and institutional voices are "
            "underrepresented. Use this as TOPIC RADAR — surfaces what people "
            "are debating about the name. Each substantive claim or topic "
            "raised below should be independently verified before being cited "
            "as fact. Treat sentiment as a positioning signal, not analysis.)",
            "",
            f"Sentiment mix (last {self.n_total} tagged messages): "
            f"{self.sentiment_bull} bullish ({bull_pct:.0f}%) / "
            f"{self.sentiment_bear} bearish ({bear_pct:.0f}%) / "
            f"{self.sentiment_none} unlabeled.",
            "",
            "Top high-engagement messages (filtered for length, non-spam, "
            "non-pump patterns; ranked by reshares + likes):",
            "",
        ]
        for m in picks:
            tag = (
                "[BULL]" if m.sentiment == "Bullish"
                else "[BEAR]" if m.sentiment == "Bearish"
                else "[--]"
            )
            user_tier = "verified" if m.user_official else f"{m.user_followers:,}f"
            other = ""
            if m.other_symbols:
                # Comma list of other tickers mentioned (flag cross-ticker theses)
                other = f"  also tags: {', '.join('$' + s for s in m.other_symbols[:5])}"
            lines.append(
                f"{tag} {m.created_at[:10]}  @{m.username} ({user_tier}, "
                f"{m.engagement} engagement){other}"
            )
            # Compress body — strip URL-only lines, collapse whitespace
            body_clean = re.sub(r"https?://\S+", "[link]", m.body)
            body_clean = re.sub(r"\s+", " ", body_clean).strip()
            lines.append(f"  \"{body_clean[:380]}\"")
            lines.append("")

        lines.append(
            "Reminder: any topic above ('Scorpion report', 'Q1 print', "
            "'European expansion timing', etc.) is a research question. "
            "Don't repeat retail claims as fact — investigate independently "
            "and cite primary sources."
        )
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}.json"


def _load_cache(ticker: str) -> StockTwitsBundle | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        bundle = StockTwitsBundle(
            ticker=d.get("ticker", ticker),
            fetched_at=d.get("fetched_at", ""),
            sentiment_bull=d.get("sentiment_bull", 0),
            sentiment_bear=d.get("sentiment_bear", 0),
            sentiment_none=d.get("sentiment_none", 0),
            n_total=d.get("n_total", 0),
            error=d.get("error", ""),
        )
        known = {f for f in StockTwitsMessage.__dataclass_fields__}
        for m in d.get("messages", []):
            bundle.messages.append(StockTwitsMessage(
                **{k: v for k, v in m.items() if k in known}
            ))
        return bundle
    except Exception:
        return None


def _save_cache(bundle: StockTwitsBundle) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(bundle.ticker).write_text(
        json.dumps(bundle.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------

def _extract_json_from_browser_html(html: str) -> dict | None:
    """The browser fetches the API URL and returns the response wrapped
    in a Chrome JSON viewer (<pre>JSON</pre>). Extract the JSON payload."""
    if not html:
        return None
    # Pre-tagged JSON
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.DOTALL)
    candidate = m.group(1) if m else html
    # Strip common HTML entities
    candidate = candidate.replace("&quot;", '"').replace("&amp;", "&")
    candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except Exception:
        # Try to find the first { ... } block
        m = re.search(r"\{.*\}", candidate, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _parse_message(raw: dict, ticker: str) -> StockTwitsMessage | None:
    """Convert a raw StockTwits message dict to our dataclass."""
    if not isinstance(raw, dict):
        return None
    user = raw.get("user") or {}
    entities = raw.get("entities") or {}
    sentiment = ((entities.get("sentiment") or {}).get("basic") or "")
    reshares = (raw.get("reshares") or {}).get("reshared_count", 0) or 0
    likes = (raw.get("likes") or {}).get("total", 0) or 0
    other_symbols = []
    for s in (raw.get("symbols") or []):
        sym = (s.get("symbol") or "").upper()
        if sym and sym != ticker.upper():
            other_symbols.append(sym)
    import html as _html_mod
    return StockTwitsMessage(
        id=raw.get("id", 0),
        created_at=raw.get("created_at", ""),
        body=_html_mod.unescape(raw.get("body", "") or ""),
        username=user.get("username", ""),
        user_followers=user.get("followers", 0) or 0,
        user_official=bool(user.get("official", False)),
        user_classification=user.get("classification", "") or "",
        sentiment=sentiment,
        reshares=reshares,
        likes=likes,
        engagement=reshares + likes,
        mentioned_users=[
            (u.get("username", "") if isinstance(u, dict) else str(u))
            for u in (raw.get("mentioned_users") or [])
        ],
        other_symbols=other_symbols,
    )


def fetch_stocktwits(ticker: str, *, days_back: int = 30,
                     max_fetch: int = 200,
                     max_pages: int = 10,
                     verbose: bool = False,
                     force_refresh: bool = False) -> StockTwitsBundle:
    """
    Fetch the recent StockTwits stream for a ticker. Pages backwards
    through the message stream (30 messages/page) until any of:
      - we collect `max_fetch` messages
      - we hit a page where every message is older than `days_back`
      - we exceed `max_pages` API calls (safety cap)

    Pagination uses ?max=<oldest_seen_id> so each page returns messages
    older than the previous page. Browser is reused (singleton in
    _browser_fetch) so subsequent fetches are ~2-3s after the first.

    Cached daily per ticker.
    """
    ticker = ticker.upper().strip()
    bundle = StockTwitsBundle(
        ticker=ticker,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    if not force_refresh:
        cached = _load_cache(ticker)
        if cached:
            if verbose:
                print(f"  StockTwits: cache hit ({cached.n_total} messages, "
                      f"fetched {cached.fetched_at})")
            return cached

    try:
        from ingestion.loaders._browser_fetch import fetch_html_with_browser
    except Exception as e:
        bundle.error = f"browser_fetch import: {type(e).__name__}: {e}"
        if verbose:
            print(f"  StockTwits: {bundle.error}")
        return bundle

    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    parsed: list[StockTwitsMessage] = []
    seen_ids: set[int] = set()
    next_max_id: int | None = None
    pages_fetched = 0

    while pages_fetched < max_pages and len(parsed) < max_fetch:
        url = STREAM_URL.format(ticker=ticker)
        if next_max_id is not None:
            url = f"{url}?max={next_max_id}"
        try:
            html = fetch_html_with_browser(url, verbose=verbose)
        except Exception as e:
            bundle.error = f"browser_fetch error: {type(e).__name__}: {e}"
            break
        pages_fetched += 1
        if not html:
            bundle.error = "browser_fetch returned no body"
            break
        payload = _extract_json_from_browser_html(html)
        if payload is None:
            bundle.error = "could not parse JSON from browser response"
            break
        raw_msgs = payload.get("messages") or []
        if not raw_msgs:
            break

        page_added = 0
        page_all_too_old = True
        page_min_id: int | None = None
        for raw in raw_msgs:
            m = _parse_message(raw, ticker)
            if m is None or m.id in seen_ids:
                continue
            # Track the lowest ID seen on this page for next-page pagination
            if page_min_id is None or m.id < page_min_id:
                page_min_id = m.id
            if m.created_at and m.created_at < cutoff:
                # Older than window — skip but keep walking the page in case
                # the API returned an out-of-order entry
                continue
            page_all_too_old = False
            seen_ids.add(m.id)
            parsed.append(m)
            page_added += 1
            if len(parsed) >= max_fetch:
                break

        if verbose:
            print(f"    StockTwits page {pages_fetched}: "
                  f"+{page_added} (total {len(parsed)})")
        if page_all_too_old:
            # Every message on this page was older than cutoff — done
            break
        if page_min_id is None:
            break
        # Set up next page's max parameter (must be lower than the lowest
        # ID we saw, hence -1)
        next_max_id = page_min_id - 1

    bundle.messages = parsed
    bundle.n_total = len(parsed)
    bundle.sentiment_bull = sum(1 for m in parsed if m.sentiment == "Bullish")
    bundle.sentiment_bear = sum(1 for m in parsed if m.sentiment == "Bearish")
    bundle.sentiment_none = sum(1 for m in parsed
                                  if m.sentiment not in ("Bullish", "Bearish"))

    try:
        _save_cache(bundle)
    except Exception as e:
        if verbose:
            print(f"  StockTwits: cache save failed: {e}")

    if verbose:
        print(f"  StockTwits: {bundle.n_total} message(s), "
              f"{bundle.sentiment_bull} bull / {bundle.sentiment_bear} bear / "
              f"{bundle.sentiment_none} unlabeled")
    return bundle
