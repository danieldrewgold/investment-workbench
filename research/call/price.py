"""
Live price with its date. Never inferred: if it is missing or stale the run fails.

Polygon's free tier gives the close of the last completed session, so "live"
means that close, stamped with its session date. Stale means older than the
last two completed NYSE sessions (one session of slack covers Polygon's
end-of-day processing lag without letting a days-old price through).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from core.env import load_dotenv  # noqa: F401  (loads .env so POLYGON_API_KEY resolves)

ET = ZoneInfo("America/New_York")

NYSE_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


class PriceError(RuntimeError):
    """Missing or stale price. The call cannot be made without a real price."""


@dataclass
class LivePrice:
    ticker: str
    price: float
    session_date: str      # YYYY-MM-DD of the session the close belongs to
    source: str
    fetched_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        return f"${self.price:,.2f} (close {self.session_date}, {self.source})"


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def last_completed_session(now: datetime | None = None) -> date:
    """Most recent NYSE session whose 16:00 ET close has passed."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    d = now.date()
    if not (is_session(d) and now.time() >= time(16, 0)):
        d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def previous_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def check_fresh(session_date: date, now: datetime | None = None) -> None:
    last = last_completed_session(now)
    oldest_ok = previous_session(last)
    if session_date < oldest_ok:
        raise PriceError(f"price is stale: session {session_date} is older than "
                         f"{oldest_ok} (last completed session {last})")


def fetch_live_price(ticker: str, now: datetime | None = None) -> LivePrice:
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        raise PriceError("POLYGON_API_KEY not set; refusing to infer a price")
    try:
        r = httpx.get(f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/prev",
                      params={"adjusted": "true", "apiKey": key}, timeout=20.0)
        res = (r.json().get("results") or []) if r.status_code == 200 else []
    except Exception as e:
        raise PriceError(f"price fetch failed: {type(e).__name__}: {e}")
    if not res or not res[0].get("c"):
        raise PriceError(f"no price returned for {ticker} (HTTP {r.status_code})")
    bar = res[0]
    session = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).astimezone(ET).date()
    check_fresh(session, now)
    return LivePrice(ticker=ticker.upper(), price=float(bar["c"]), session_date=session.isoformat(),
                     source="Polygon previous-session close",
                     fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
