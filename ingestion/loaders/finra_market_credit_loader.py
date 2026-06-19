"""
FINRA Market Credit Sentiment Loader

Pulls the FINRA Data API CORPORATEMARKETSENTIMENT dataset and renders a
macro-credit signal panel for the brief: investment-grade vs high-yield
customer net flow direction, today vs 30d trailing average, plus the
credit-equity divergence at the market level (HY customers net-selling
while equity flat or up — a risk-off signal that's historically led
equity drawdowns).

This is the layer that the FREE FINRA Data Gateway tier supports. Per-
CUSIP TRACE prices need a separate paid TRACE subscription — those flow
through `finra_trace_loader.py` and stay silent until creds for the
paid product are added (or until manual prices are dropped into
`data/manual_bond_prices.csv`).

Public API:
    fetch_market_credit_sentiment(*, lookback_days=60, verbose=False)
        -> MarketCreditBundle

Cache: daily flat file. Sentiment statistics update once per trading day.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, date
from pathlib import Path

import httpx

from core.env import load_dotenv  # noqa: F401
from ingestion.loaders.finra_trace_loader import _get_credentials, _get_oauth_token


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


_DATA_URL = (
    "https://api.finra.org/data/group/FIXEDINCOMEMARKET/name/"
    "CORPORATEMARKETSENTIMENT"
)
_CACHE_DIR = Path("data/finra_market_credit_cache")
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h — this updates once daily, but allow
                                    # intra-day re-fetch in case end-of-day
                                    # data lands mid-run


# --------------------------------------------------------------------------
# Data type
# --------------------------------------------------------------------------

@dataclass
class CreditSentimentDay:
    trade_date: str = ""
    # Investment grade
    ig_customer_buy_vol: float = 0.0
    ig_customer_sell_vol: float = 0.0
    ig_total_vol: float = 0.0
    # High yield
    hy_customer_buy_vol: float = 0.0
    hy_customer_sell_vol: float = 0.0
    hy_total_vol: float = 0.0
    # Convertibles (relevant for equity-linked credit signal)
    cv_customer_buy_vol: float = 0.0
    cv_customer_sell_vol: float = 0.0
    cv_total_vol: float = 0.0

    @property
    def ig_net_flow(self) -> float:
        return self.ig_customer_buy_vol - self.ig_customer_sell_vol

    @property
    def hy_net_flow(self) -> float:
        return self.hy_customer_buy_vol - self.hy_customer_sell_vol

    @property
    def hy_ig_volume_ratio(self) -> float:
        if not self.ig_total_vol:
            return 0.0
        return self.hy_total_vol / self.ig_total_vol


@dataclass
class MarketCreditBundle:
    fetched_at: str = ""
    auth_status: str = ""
    n_days: int = 0
    days: list = field(default_factory=list)  # list[CreditSentimentDay], asc
    error: str = ""

    @property
    def latest(self) -> CreditSentimentDay | None:
        return self.days[-1] if self.days else None

    def trailing_avg_net(self, *, days: int, attr: str) -> float | None:
        """Trailing-window simple average of `attr` (e.g. 'ig_net_flow')."""
        if not self.days:
            return None
        window = self.days[-days:] if len(self.days) > days else self.days
        if not window:
            return None
        return sum(getattr(d, attr) for d in window) / len(window)

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "auth_status": self.auth_status,
            "n_days": self.n_days,
            "latest_date": self.latest.trade_date if self.latest else "",
            "days": [asdict(d) for d in self.days],
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        if not self.days:
            if self.auth_status == "no_credentials":
                return ""
            return ""

        latest = self.latest
        ig_avg30 = self.trailing_avg_net(days=30, attr="ig_net_flow") or 0.0
        hy_avg30 = self.trailing_avg_net(days=30, attr="hy_net_flow") or 0.0

        def _direction(v: float, scale: float = 100.0) -> str:
            if v > scale: return "net BUYING"
            if v < -scale: return "net SELLING"
            return "balanced"

        ig_today_dir = _direction(latest.ig_net_flow, scale=200)
        hy_today_dir = _direction(latest.hy_net_flow, scale=50)
        ig_30d_dir = _direction(ig_avg30, scale=100)
        hy_30d_dir = _direction(hy_avg30, scale=30)

        lines = [
            f"=== CORPORATE BOND MARKET SENTIMENT (FINRA, "
            f"as of {latest.trade_date}) ===",
            "(Daily customer flow direction across all corporate bonds. "
            "Volume in $M par. Negative net flow = customers net-selling; "
            "positive = net-buying. HY direction is a risk-on/risk-off "
            "leading indicator for equities.)",
            "",
            f"  Investment Grade today: customers {ig_today_dir} "
            f"(${latest.ig_net_flow:+,.0f}M net flow on "
            f"${latest.ig_total_vol:,.0f}M total vol)",
            f"  Investment Grade 30d avg: customers {ig_30d_dir} "
            f"(${ig_avg30:+,.0f}M/day)",
            f"  High Yield today: customers {hy_today_dir} "
            f"(${latest.hy_net_flow:+,.0f}M net flow on "
            f"${latest.hy_total_vol:,.0f}M total vol)",
            f"  High Yield 30d avg: customers {hy_30d_dir} "
            f"(${hy_avg30:+,.0f}M/day)",
            f"  HY/IG volume ratio today: {latest.hy_ig_volume_ratio:.2f} "
            f"(higher = risk-on activity)",
        ]

        # Regime read — single-line diagnosis
        regime = _classify_regime(latest, ig_avg30, hy_avg30)
        if regime:
            lines.append("")
            lines.append(f"  CREDIT REGIME: {regime}")

        lines.append("")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


def _classify_regime(
    latest: CreditSentimentDay,
    ig_avg30: float,
    hy_avg30: float,
) -> str:
    """Single-line credit-regime read combining today + trend.

    Buckets:
      RISK-ON         HY net buying + 30d trend net buying
      RISK-OFF        HY net selling + 30d trend net selling
      DEFENSIVE       IG net buying + HY net selling (rotation into quality)
      DETERIORATING   30d trend was buying but today flipped to selling
      RECOVERING      30d trend was selling but today flipped to buying
      MIXED           anything else
    """
    today_hy = latest.hy_net_flow
    today_ig = latest.ig_net_flow

    def _is_buy(v, scale): return v > scale
    def _is_sell(v, scale): return v < -scale

    if _is_buy(today_hy, 50) and _is_buy(hy_avg30, 30):
        return "RISK-ON (HY buying today + 30d)"
    if _is_sell(today_hy, 50) and _is_sell(hy_avg30, 30):
        return "RISK-OFF (HY selling today + 30d)"
    if _is_buy(today_ig, 200) and _is_sell(today_hy, 50):
        return "DEFENSIVE ROTATION (IG net buying, HY net selling today)"
    if _is_buy(hy_avg30, 30) and _is_sell(today_hy, 50):
        return "DETERIORATING (HY trend was buying, today flipped to selling)"
    if _is_sell(hy_avg30, 30) and _is_buy(today_hy, 50):
        return "RECOVERING (HY trend was selling, today flipped to buying)"
    return ""


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _cache_path() -> Path:
    return _CACHE_DIR / "market_credit_sentiment.json"


def _load_cache() -> MarketCreditBundle | None:
    p = _cache_path()
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        days = [
            CreditSentimentDay(**{k: v for k, v in dd.items()
                                    if k in CreditSentimentDay.__dataclass_fields__})
            for dd in d.get("days", [])
        ]
        return MarketCreditBundle(
            fetched_at=d.get("fetched_at", ""),
            auth_status=d.get("auth_status", ""),
            n_days=d.get("n_days", len(days)),
            days=days,
            error=d.get("error", ""),
        )
    except Exception:
        return None


def _save_cache(b: MarketCreditBundle) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(
            json.dumps(b.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def fetch_market_credit_sentiment(
    *,
    lookback_days: int = 60,
    verbose: bool = False,
    force_refresh: bool = False,
) -> MarketCreditBundle:
    """Pull the recent CORPORATEMARKETSENTIMENT history. Returns a bundle
    with one CreditSentimentDay per trading day (ascending order)."""
    if not force_refresh:
        cached = _load_cache()
        if cached and cached.days:
            if verbose:
                print(f"  FINRA market credit: cache hit "
                      f"({cached.n_days} days, latest {cached.latest.trade_date})")
            return cached

    bundle = MarketCreditBundle(
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    creds = _get_credentials()
    if not creds:
        bundle.auth_status = "no_credentials"
        bundle.error = "FINRA_DATA_CLIENT_ID/SECRET not set"
        return bundle

    cid, cs = creds
    token = _get_oauth_token(cid, cs, verbose=verbose)
    if not token:
        bundle.auth_status = "auth_failed"
        bundle.error = "FINRA OAuth failed"
        return bundle
    bundle.auth_status = "ok"

    end = date.today()
    start = end - timedelta(days=lookback_days + 5)  # buffer for weekends/holidays
    body = {
        "dateRangeFilters": [{
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "fieldName": "tradeReportDate",
        }],
        "limit": 5000,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(_DATA_URL, json=body, headers=headers, timeout=60)
    except Exception as e:
        bundle.error = f"fetch error: {type(e).__name__}: {e}"
        return bundle
    if r.status_code != 200:
        bundle.error = f"HTTP {r.status_code}: {r.text[:200]}"
        return bundle

    rows = r.json() or []
    bundle.days = _aggregate_rows(rows)
    bundle.n_days = len(bundle.days)

    try:
        _save_cache(bundle)
    except Exception:
        pass

    if verbose:
        print(f"  FINRA market credit: {bundle.n_days} days "
              f"(latest {bundle.latest.trade_date if bundle.latest else '?'})")

    return bundle


def _aggregate_rows(rows: list) -> list:
    """Group raw FINRA rows by date + tradeType + productCategory and
    project onto our flat-per-day schema."""
    by_date: dict[str, CreditSentimentDay] = {}
    for r in rows:
        d = r.get("tradeReportDate", "")
        if not d:
            continue
        day = by_date.setdefault(d, CreditSentimentDay(trade_date=d))
        tt = (r.get("tradeType") or "").strip().lower()
        pc = (r.get("productCategory") or "").strip().lower()
        vol = float(r.get("totalVolume") or 0)
        if tt == "investment grade":
            if pc == "customer buy":
                day.ig_customer_buy_vol = vol
            elif pc == "customer sell":
                day.ig_customer_sell_vol = vol
            elif pc == "all securities":
                day.ig_total_vol = vol
        elif tt == "high yield":
            if pc == "customer buy":
                day.hy_customer_buy_vol = vol
            elif pc == "customer sell":
                day.hy_customer_sell_vol = vol
            elif pc == "all securities":
                day.hy_total_vol = vol
        elif tt == "convertible bonds":
            if pc == "customer buy":
                day.cv_customer_buy_vol = vol
            elif pc == "customer sell":
                day.cv_customer_sell_vol = vol
            elif pc == "all securities":
                day.cv_total_vol = vol
    # Sort ascending by date
    return [by_date[d] for d in sorted(by_date.keys())]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="FINRA corporate market sentiment")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    b = fetch_market_credit_sentiment(lookback_days=args.days,
                                        verbose=True,
                                        force_refresh=args.refresh)
    print()
    if args.json:
        print(json.dumps(b.to_dict(), indent=2, default=str))
    else:
        print(b.to_prompt_text() or "(no data)")
        if b.error:
            print(f"ERROR: {b.error}")


if __name__ == "__main__":
    _main()
