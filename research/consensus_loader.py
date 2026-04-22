"""
Consensus Loader — real sell-side consensus from yfinance.

Replaces the thin inline yfinance call in pipeline.py with a richer fetch
that captures:
  - Per-period EPS + revenue estimates (current Q, next Q, current FY, next FY)
    including mean / low / high / num analysts / YoY growth
  - Revision history (7d/30d/60d/90d ago) — detects if consensus is moving
  - Up/down revision counts (how many analysts moved estimates recently)
  - Price target distribution (mean / median / high / low)
  - Analyst rating distribution over last 4 months (strong buy → sell)
  - Long-term EPS growth rate (5yr consensus)
  - Next earnings date with high/low revenue expectations

Explicitly NOT captured (requires paid feeds):
  - Segment-level revenue / margin consensus (Visible Alpha)
  - Individual analyst estimates by name
  - EBITDA / FCF consensus for non-large-caps

Everything here is free via yfinance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class PeriodEstimate:
    """Analyst consensus for a specific reporting period."""
    period: str                      # "0q" | "+1q" | "0y" | "+1y"
    period_label: str                # "Current Q" | "Next Q" | "Current FY" | "Next FY"

    # EPS consensus
    eps_mean: float | None = None
    eps_low: float | None = None
    eps_high: float | None = None
    eps_year_ago: float | None = None
    eps_growth_yoy: float | None = None      # decimal (0.10 = 10%)
    eps_num_analysts: int = 0

    # Revenue consensus
    revenue_mean: float | None = None        # in dollars
    revenue_low: float | None = None
    revenue_high: float | None = None
    revenue_year_ago: float | None = None
    revenue_growth_yoy: float | None = None
    revenue_num_analysts: int = 0

    # EPS revision trend (how consensus has moved over recent windows)
    eps_current: float | None = None
    eps_7d_ago: float | None = None
    eps_30d_ago: float | None = None
    eps_60d_ago: float | None = None
    eps_90d_ago: float | None = None

    # Revision activity (how many analysts moved their estimates)
    up_revs_7d: int = 0
    up_revs_30d: int = 0
    down_revs_7d: int = 0
    down_revs_30d: int = 0

    # Derived — change in consensus over 30 days
    @property
    def eps_change_30d(self) -> float | None:
        if self.eps_current is None or self.eps_30d_ago is None:
            return None
        return self.eps_current - self.eps_30d_ago

    @property
    def eps_change_30d_pct(self) -> float | None:
        if self.eps_current is None or self.eps_30d_ago is None or self.eps_30d_ago == 0:
            return None
        return (self.eps_current - self.eps_30d_ago) / abs(self.eps_30d_ago)

    @property
    def net_revisions_30d(self) -> int:
        return self.up_revs_30d - self.down_revs_30d

    @property
    def revenue_mean_m(self) -> float | None:
        return self.revenue_mean / 1e6 if self.revenue_mean else None


@dataclass
class PriceTarget:
    current_price: float | None = None
    mean: float | None = None
    median: float | None = None
    high: float | None = None
    low: float | None = None

    @property
    def upside_pct(self) -> float | None:
        if self.current_price and self.mean and self.current_price > 0:
            return (self.mean - self.current_price) / self.current_price
        return None


@dataclass
class RatingSnapshot:
    """Analyst rating distribution at a point in time."""
    period: str            # "0m" | "-1m" | "-2m" | "-3m"
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0

    @property
    def total(self) -> int:
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def bullish_ratio(self) -> float | None:
        """(strong_buy + buy) / total — fraction of analysts with buy rating."""
        t = self.total
        if t == 0:
            return None
        return (self.strong_buy + self.buy) / t


@dataclass
class NextEarnings:
    date: str = ""                    # ISO YYYY-MM-DD
    days_out: int | None = None
    eps_mean: float | None = None
    eps_low: float | None = None
    eps_high: float | None = None
    revenue_mean: float | None = None
    revenue_low: float | None = None
    revenue_high: float | None = None


@dataclass
class ConsensusData:
    """Complete consensus snapshot for a ticker — replaces the thin inline fetch."""
    ticker: str = ""
    fetched_at: str = ""
    source: str = "yfinance"

    # Per-period estimates
    current_quarter: PeriodEstimate | None = None
    next_quarter: PeriodEstimate | None = None
    current_year: PeriodEstimate | None = None
    next_year: PeriodEstimate | None = None

    # Long-term growth
    ltg_eps_5yr: float | None = None     # 5-year consensus EPS CAGR

    # Price targets
    price_target: PriceTarget = field(default_factory=PriceTarget)

    # Ratings over time (most recent first)
    ratings: list = field(default_factory=list)  # list[RatingSnapshot]

    # Next reporting event
    next_earnings: NextEarnings = field(default_factory=NextEarnings)

    # Metadata
    max_analysts: int = 0                  # max n across periods
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    # --- Convenience accessors ---

    @property
    def forward_eps(self) -> float | None:
        """12-month-forward EPS = current_year consensus (most analysts track this)."""
        if self.current_year and self.current_year.eps_mean is not None:
            return self.current_year.eps_mean
        return None

    @property
    def next_year_eps(self) -> float | None:
        if self.next_year and self.next_year.eps_mean is not None:
            return self.next_year.eps_mean
        return None

    @property
    def current_price(self) -> float | None:
        return self.price_target.current_price

    def legacy_consensus_dict(self) -> dict:
        """Back-compat: return the flat {eps, revenue_m} shape the old
        inline pipeline code produced. Uses current_year (0y) as the
        reference period because that's what info['forwardEps'] proxied."""
        out = {}
        if self.current_year:
            if self.current_year.eps_mean is not None:
                out["eps"] = self.current_year.eps_mean
            if self.current_year.revenue_mean_m is not None:
                out["revenue_m"] = self.current_year.revenue_mean_m
        return out


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

_PERIOD_LABELS = {
    "0q": "Current Quarter",
    "+1q": "Next Quarter",
    "0y": "Current Fiscal Year",
    "+1y": "Next Fiscal Year",
}


def _safe(v, kind=float):
    """Return v cast to kind, or None if missing / NaN."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    try:
        out = kind(v)
        if kind is float and math.isnan(out):
            return None
        return out
    except Exception:
        return None


def _build_period_estimate(period: str, eps_row, rev_row, trend_row, rev_counts_row) -> PeriodEstimate:
    pe = PeriodEstimate(period=period, period_label=_PERIOD_LABELS.get(period, period))

    if eps_row is not None:
        pe.eps_mean = _safe(eps_row.get("avg"))
        pe.eps_low = _safe(eps_row.get("low"))
        pe.eps_high = _safe(eps_row.get("high"))
        pe.eps_year_ago = _safe(eps_row.get("yearAgoEps"))
        pe.eps_growth_yoy = _safe(eps_row.get("growth"))
        pe.eps_num_analysts = _safe(eps_row.get("numberOfAnalysts"), int) or 0

    if rev_row is not None:
        pe.revenue_mean = _safe(rev_row.get("avg"))
        pe.revenue_low = _safe(rev_row.get("low"))
        pe.revenue_high = _safe(rev_row.get("high"))
        pe.revenue_year_ago = _safe(rev_row.get("yearAgoRevenue"))
        pe.revenue_growth_yoy = _safe(rev_row.get("growth"))
        pe.revenue_num_analysts = _safe(rev_row.get("numberOfAnalysts"), int) or 0

    if trend_row is not None:
        pe.eps_current = _safe(trend_row.get("current"))
        pe.eps_7d_ago = _safe(trend_row.get("7daysAgo"))
        pe.eps_30d_ago = _safe(trend_row.get("30daysAgo"))
        pe.eps_60d_ago = _safe(trend_row.get("60daysAgo"))
        pe.eps_90d_ago = _safe(trend_row.get("90daysAgo"))

    if rev_counts_row is not None:
        pe.up_revs_7d = _safe(rev_counts_row.get("upLast7days"), int) or 0
        pe.up_revs_30d = _safe(rev_counts_row.get("upLast30days"), int) or 0
        pe.down_revs_7d = _safe(rev_counts_row.get("downLast7Days"), int) or 0
        pe.down_revs_30d = _safe(rev_counts_row.get("downLast30days"), int) or 0

    return pe


def fetch_consensus(ticker: str, *, verbose: bool = False) -> ConsensusData | None:
    """
    Fetch sell-side consensus data for ticker from yfinance.

    Returns a ConsensusData with all available fields. On total failure
    (network error, bad ticker) returns a ConsensusData with error set,
    not None — so callers always have a valid object to destructure.
    """
    ticker = ticker.upper().strip()
    cd = ConsensusData(
        ticker=ticker,
        fetched_at=datetime.utcnow().isoformat() + "Z",
    )

    try:
        import yfinance as yf
    except ImportError:
        cd.error = "yfinance not installed"
        return cd

    try:
        t = yf.Ticker(ticker)
    except Exception as e:
        cd.error = f"yf.Ticker failed: {e}"
        return cd

    def _as_dict(df, index_col_name=None):
        """Convert a yfinance DataFrame to {index_value: {col: val}} dict."""
        if df is None:
            return {}
        try:
            if df.empty:
                return {}
        except Exception:
            return {}
        try:
            if index_col_name and index_col_name in df.columns:
                return {row[index_col_name]: row for _, row in df.iterrows()}
            return {idx: row for idx, row in df.iterrows()}
        except Exception:
            return {}

    # Per-period estimates
    try:
        eps_est = _as_dict(t.earnings_estimate)
    except Exception:
        eps_est = {}
    try:
        rev_est = _as_dict(t.revenue_estimate)
    except Exception:
        rev_est = {}
    try:
        trend = _as_dict(t.eps_trend)
    except Exception:
        trend = {}
    try:
        revs = _as_dict(t.eps_revisions)
    except Exception:
        revs = {}

    for period, attr in [("0q", "current_quarter"), ("+1q", "next_quarter"),
                          ("0y", "current_year"), ("+1y", "next_year")]:
        if period in eps_est or period in rev_est:
            setattr(cd, attr, _build_period_estimate(
                period,
                eps_est.get(period),
                rev_est.get(period),
                trend.get(period),
                revs.get(period),
            ))

    # Long-term growth
    try:
        ge = t.growth_estimates
        if ge is not None and "LTG" in ge.index:
            row = ge.loc["LTG"]
            cd.ltg_eps_5yr = _safe(row.get("stockTrend"))
    except Exception:
        pass

    # Price targets
    try:
        pt = t.analyst_price_targets or {}
        cd.price_target = PriceTarget(
            current_price=_safe(pt.get("current")),
            mean=_safe(pt.get("mean")),
            median=_safe(pt.get("median")),
            high=_safe(pt.get("high")),
            low=_safe(pt.get("low")),
        )
    except Exception:
        pass

    # Ratings summary (up to 4 monthly snapshots)
    try:
        rs = t.recommendations_summary
        if rs is not None:
            for _, row in rs.iterrows():
                cd.ratings.append(RatingSnapshot(
                    period=str(row.get("period", "")),
                    strong_buy=_safe(row.get("strongBuy"), int) or 0,
                    buy=_safe(row.get("buy"), int) or 0,
                    hold=_safe(row.get("hold"), int) or 0,
                    sell=_safe(row.get("sell"), int) or 0,
                    strong_sell=_safe(row.get("strongSell"), int) or 0,
                ))
    except Exception:
        pass

    # Next earnings event
    try:
        cal = t.calendar or {}
        earnings_date = cal.get("Earnings Date")
        if earnings_date:
            first_date = earnings_date[0] if isinstance(earnings_date, list) and earnings_date else earnings_date
            if hasattr(first_date, "isoformat"):
                cd.next_earnings.date = first_date.isoformat()
                try:
                    days_out = (first_date - date.today()).days
                    cd.next_earnings.days_out = days_out
                except Exception:
                    pass
            else:
                cd.next_earnings.date = str(first_date)
        cd.next_earnings.eps_mean = _safe(cal.get("Earnings Average"))
        cd.next_earnings.eps_low = _safe(cal.get("Earnings Low"))
        cd.next_earnings.eps_high = _safe(cal.get("Earnings High"))
        cd.next_earnings.revenue_mean = _safe(cal.get("Revenue Average"))
        cd.next_earnings.revenue_low = _safe(cal.get("Revenue Low"))
        cd.next_earnings.revenue_high = _safe(cal.get("Revenue High"))
    except Exception:
        pass

    # Max analyst count across periods
    ns = []
    for pe in (cd.current_quarter, cd.next_quarter, cd.current_year, cd.next_year):
        if pe:
            ns.append(pe.eps_num_analysts)
            ns.append(pe.revenue_num_analysts)
    cd.max_analysts = max(ns) if ns else 0

    if verbose:
        print(f"  [CONSENSUS] {ticker} loaded: "
              f"EPS(0y) {cd.forward_eps}, "
              f"ratings {len(cd.ratings)} months, "
              f"max {cd.max_analysts} analysts, "
              f"next earnings {cd.next_earnings.date}")

    return cd
