"""
Quarterly Financials Loader.

Fetches 8-12 quarters of structured income statement data from Polygon's
quarterly endpoint. Without this, the brief had to INFER prior-year
quarterly numbers from YoY growth references in transcripts ("Q1 2025
EBITDA was approximately $22M based on the 17% YoY growth cited") —
which produced sloppy, sometimes-wrong sequential analysis. With this,
the brief sees real Q-by-Q actuals and can do bulletproof seasonal
comparisons.

Endpoint:
    GET https://api.polygon.io/vX/reference/financials
        ?ticker=X&timeframe=quarterly&limit=12&order=desc

Returns: revenue, gross profit, operating income, net income, EPS
(basic + diluted) per quarter, plus margins computed from those values.

The prompt-text rendering is a compact 12-quarter table with sequential
(Q/Q) and YoY (same-quarter prior year) deltas computed for each metric
— so Claude can cite specific numbers in the seasonal-trajectory
discipline without the corpus needing transcript YoY references.

Public API:
    fetch_quarterly_financials(ticker, *, n_quarters=12, verbose=False,
                                force_refresh=False) -> QuarterlyFinancialsBundle

Cache: per-ticker daily.
"""

from __future__ import annotations

import json
import os
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


def _get_polygon_key() -> str:
    """Reuse the same key the financials_fetcher uses."""
    try:
        from ingestion.loaders.polygon_financials import POLYGON_API_KEY
        return POLYGON_API_KEY or ""
    except Exception:
        return os.environ.get("POLYGON_API_KEY", "")


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class QuarterlyReport:
    """One quarter of structured income statement data."""
    fiscal_year: int = 0
    fiscal_period: str = ""        # "Q1" | "Q2" | "Q3" | "Q4" (or "FY")
    period_label: str = ""          # "Q4 2025" — derived for display
    start_date: str = ""            # YYYY-MM-DD
    end_date: str = ""              # YYYY-MM-DD

    # Income statement (in dollars; renderer divides for $M / $B display)
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps_basic: float | None = None
    eps_diluted: float | None = None

    # Derived margins (decimals, e.g. 0.32 = 32%)
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None

    # Sequential (Q over prior Q) and YoY (same Q prior year) deltas —
    # populated by the bundle after sorting all quarters chronologically.
    rev_qoq_pct: float | None = None
    rev_yoy_pct: float | None = None
    op_inc_qoq_pct: float | None = None
    op_inc_yoy_pct: float | None = None
    eps_diluted_yoy_pct: float | None = None


@dataclass
class QuarterlyFinancialsBundle:
    ticker: str
    fetched_at: str = ""
    reports: list = field(default_factory=list)   # list[QuarterlyReport], desc by date

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "reports": [asdict(r) if hasattr(r, "__dataclass_fields__") else r
                        for r in self.reports],
        }

    def to_prompt_text(self, max_quarters: int = 16) -> str:
        """
        Render a compact Q-by-Q table for prompt injection. Includes
        sequential (Q/Q) and YoY (same-Q prior year) deltas so Claude
        can cite specific numbers in the seasonal-trajectory discipline
        without inferring from transcripts.
        """
        if not self.reports:
            return ""
        rpts = self.reports[:max_quarters]
        # Render most-recent first
        lines = [
            "=== QUARTERLY HISTORICAL FINANCIALS (last "
            f"{len(rpts)} quarters from Polygon — REAL actuals, NOT inferred) ===",
            "(Use these for the SEQUENTIAL TRAJECTORY discipline — when "
            "comparing current Q sequential to prior-year same-period "
            "sequential, cite numbers from THIS table directly. The Q/Q and "
            "YoY columns show the percent change from immediately prior "
            "quarter and same quarter prior year, respectively. "
            "Don't infer Q1 2025 EBITDA from YoY growth in transcripts — "
            "look it up here.)",
            "",
        ]
        # Header
        header = (f"{'Period':<10} "
                  f"{'Revenue':>12} {'Q/Q':>7} {'YoY':>7}  "
                  f"{'GM%':>6} {'OpInc':>11} {'Op%':>6} {'OpYoY':>7}  "
                  f"{'NM%':>6} {'EPS':>7} {'EPS YoY':>8}")
        lines.append(header)
        lines.append("-" * len(header))
        for r in rpts:
            rev_str = (f"${r.revenue/1e6:>10,.0f}M" if r.revenue is not None else "      —")
            qoq = (f"{r.rev_qoq_pct*100:+5.1f}%" if r.rev_qoq_pct is not None else "    —")
            yoy = (f"{r.rev_yoy_pct*100:+5.1f}%" if r.rev_yoy_pct is not None else "    —")
            gm = (f"{r.gross_margin*100:>4.1f}%" if r.gross_margin is not None else "    —")
            opi = (f"${r.operating_income/1e6:>9,.0f}M" if r.operating_income is not None else "        —")
            opm = (f"{r.operating_margin*100:>4.1f}%" if r.operating_margin is not None else "    —")
            opy = (f"{r.op_inc_yoy_pct*100:+5.1f}%" if r.op_inc_yoy_pct is not None else "    —")
            nm = (f"{r.net_margin*100:>4.1f}%" if r.net_margin is not None else "    —")
            eps = (f"${r.eps_diluted:>5.2f}" if r.eps_diluted is not None else "    —")
            eps_yoy = (f"{r.eps_diluted_yoy_pct*100:+5.1f}%" if r.eps_diluted_yoy_pct is not None else "    —")
            lines.append(f"{r.period_label:<10} {rev_str:>12} {qoq:>7} {yoy:>7}  "
                          f"{gm:>6} {opi:>11} {opm:>6} {opy:>7}  "
                          f"{nm:>6} {eps:>7} {eps_yoy:>8}")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * len(header))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache (daily)
# --------------------------------------------------------------------------

_CACHE_DIR = Path("data/quarterly_financials_cache")
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}.json"


def _load_cache(ticker: str) -> QuarterlyFinancialsBundle | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        bundle = QuarterlyFinancialsBundle(
            ticker=d.get("ticker", ticker),
            fetched_at=d.get("fetched_at", ""),
        )
        known = {f for f in QuarterlyReport.__dataclass_fields__}
        for rd in d.get("reports", []):
            bundle.reports.append(
                QuarterlyReport(**{k: v for k, v in rd.items() if k in known})
            )
        return bundle
    except Exception:
        return None


def _save_cache(bundle: QuarterlyFinancialsBundle) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(bundle.ticker).write_text(
        json.dumps(bundle.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Polygon quarterly fetch + parse
# --------------------------------------------------------------------------

def _polygon_value(field_dict: dict | None, key: str) -> float | None:
    """Extract a numeric value from Polygon's nested {value, unit} shape."""
    if not isinstance(field_dict, dict):
        return None
    inner = field_dict.get(key)
    if not isinstance(inner, dict):
        return None
    v = inner.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_quarterly_report(raw: dict) -> QuarterlyReport | None:
    """Convert one Polygon report dict into a QuarterlyReport."""
    fp = (raw.get("fiscal_period") or "").strip()
    if fp not in ("Q1", "Q2", "Q3", "Q4"):
        return None  # Skip annuals returned in quarterly view
    try:
        fy = int(raw.get("fiscal_year") or 0)
    except (TypeError, ValueError):
        fy = 0

    income = (raw.get("financials") or {}).get("income_statement") or {}

    revenue = _polygon_value(income, "revenues")
    gross_profit = _polygon_value(income, "gross_profit")
    operating_income = _polygon_value(income, "operating_income_loss")
    net_income = _polygon_value(income, "net_income_loss")
    eps_basic = _polygon_value(income, "basic_earnings_per_share")
    eps_diluted = _polygon_value(income, "diluted_earnings_per_share")

    # Derived margins
    gm = (gross_profit / revenue) if (gross_profit is not None and revenue) else None
    om = (operating_income / revenue) if (operating_income is not None and revenue) else None
    nm = (net_income / revenue) if (net_income is not None and revenue) else None

    return QuarterlyReport(
        fiscal_year=fy,
        fiscal_period=fp,
        period_label=f"{fp} {fy}",
        start_date=raw.get("start_date") or "",
        end_date=raw.get("end_date") or "",
        revenue=revenue,
        gross_profit=gross_profit,
        operating_income=operating_income,
        net_income=net_income,
        eps_basic=eps_basic,
        eps_diluted=eps_diluted,
        gross_margin=gm,
        operating_margin=om,
        net_margin=nm,
    )


def _compute_deltas(reports: list) -> None:
    """
    Compute Q/Q and YoY deltas in-place. Reports must already be sorted
    chronologically (oldest first) for sequential lookup; the bundle
    sorts back to descending after this runs.
    """
    by_period = {(r.fiscal_year, r.fiscal_period): r for r in reports}

    def _prior_q(r: QuarterlyReport) -> QuarterlyReport | None:
        """Return the immediately prior quarter."""
        order = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
        cur = order.get(r.fiscal_period, 0)
        if cur == 1:
            return by_period.get((r.fiscal_year - 1, "Q4"))
        prev_label = {2: "Q1", 3: "Q2", 4: "Q3"}[cur]
        return by_period.get((r.fiscal_year, prev_label))

    def _prior_yoy(r: QuarterlyReport) -> QuarterlyReport | None:
        """Return the same quarter, prior year."""
        return by_period.get((r.fiscal_year - 1, r.fiscal_period))

    def _pct(cur: float | None, prior: float | None) -> float | None:
        if cur is None or prior is None or prior == 0:
            return None
        return (cur - prior) / abs(prior)

    for r in reports:
        prev = _prior_q(r)
        if prev:
            r.rev_qoq_pct = _pct(r.revenue, prev.revenue)
            r.op_inc_qoq_pct = _pct(r.operating_income, prev.operating_income)
        yoy = _prior_yoy(r)
        if yoy:
            r.rev_yoy_pct = _pct(r.revenue, yoy.revenue)
            r.op_inc_yoy_pct = _pct(r.operating_income, yoy.operating_income)
            r.eps_diluted_yoy_pct = _pct(r.eps_diluted, yoy.eps_diluted)


def fetch_quarterly_financials(
    ticker: str,
    *,
    n_quarters: int = 16,
    verbose: bool = False,
    force_refresh: bool = False,
) -> QuarterlyFinancialsBundle:
    """
    Fetch the last n quarters of structured income statement data from
    Polygon. Returns a bundle with reports descending by date.
    """
    ticker = ticker.upper().strip()

    if not force_refresh:
        cached = _load_cache(ticker)
        if cached and cached.reports:
            if verbose:
                print(f"  Quarterly financials: cache hit ({len(cached.reports)} "
                      f"quarters, fetched {cached.fetched_at})")
            return cached

    polygon_key = _get_polygon_key()
    bundle = QuarterlyFinancialsBundle(
        ticker=ticker,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    if not polygon_key:
        if verbose:
            print(f"  Quarterly financials: no POLYGON_API_KEY")
        return bundle

    try:
        r = httpx.get(
            "https://api.polygon.io/vX/reference/financials",
            params={
                "ticker": ticker,
                "timeframe": "quarterly",
                "limit": n_quarters,
                "order": "desc",
                "apiKey": polygon_key,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            if verbose:
                print(f"  Quarterly financials: HTTP {r.status_code}")
            return bundle
        data = r.json()
    except Exception as e:
        if verbose:
            print(f"  Quarterly financials: {type(e).__name__}: {e}")
        return bundle

    raw_reports = data.get("results") or []
    parsed = []
    for raw in raw_reports:
        rep = _parse_quarterly_report(raw)
        if rep is None or rep.revenue is None:
            continue
        parsed.append(rep)

    if not parsed:
        if verbose:
            print(f"  Quarterly financials: no usable quarters parsed")
        return bundle

    # Sort ascending for delta computation
    parsed.sort(key=lambda r: (r.fiscal_year, r.fiscal_period))
    _compute_deltas(parsed)
    # Sort descending for display (most recent first)
    parsed.sort(key=lambda r: (r.fiscal_year, r.fiscal_period), reverse=True)
    bundle.reports = parsed

    try:
        _save_cache(bundle)
    except Exception as e:
        if verbose:
            print(f"  Quarterly financials: cache save failed: {e}")

    if verbose:
        print(f"  Quarterly financials: {len(bundle.reports)} quarters loaded")

    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch quarterly financials from Polygon")
    p.add_argument("ticker")
    p.add_argument("--quarters", type=int, default=12)
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    bundle = fetch_quarterly_financials(
        args.ticker, n_quarters=args.quarters,
        verbose=True, force_refresh=args.refresh,
    )
    print()
    print(bundle.to_prompt_text(max_quarters=args.quarters))


if __name__ == "__main__":
    _main()
