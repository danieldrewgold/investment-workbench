"""
Phase 0: Universe Sourcing & Screening

Input: sector/theme/ticker list → yfinance data pull → composite scoring → triage table.

Usage:
    from research.universe import screen_universe
    results = screen_universe(
        tickers=["CMG", "CAVA", "TXRH", "DPZ", "WING", "SG", "SHAK"],
        sector="restaurant",
    )

    # Or from CLI:
    python3 -m research.universe --tickers CMG,CAVA,TXRH,DPZ,WING --sector restaurant
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", category=FutureWarning)

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ═══════════════════════════════════════════════════════════════
# Sector-Specific Metrics (from skill comps reference table)
# ═══════════════════════════════════════════════════════════════

SECTOR_METRICS = {
    "restaurant": {
        "extra_cols": ["Rest. Margin %", "SSS %", "Unit Growth %", "Franchise %"],
        "info_keys": {
            "Rest. Margin %": lambda info: None,  # not in yfinance — leave blank
            "SSS %": lambda info: None,
            "Unit Growth %": lambda info: None,
            "Franchise %": lambda info: None,
        },
        "weight_overrides": {"rev_growth": 0.15, "ebitda_margin": 0.20, "peg": 0.15,
                             "ev_ebitda": 0.20, "pe": 0.15, "mkt_cap": 0.15},
    },
    "saas": {
        "extra_cols": ["Rule of 40", "NRR %", "ARR Growth %", "Gross Margin %"],
        "info_keys": {
            "Rule of 40": lambda info: _safe_add(
                info.get("revenueGrowth", 0) * 100,
                info.get("ebitdaMargins", 0) * 100
            ),
            "NRR %": lambda info: None,
            "ARR Growth %": lambda info: _pct(info.get("revenueGrowth")),
            "Gross Margin %": lambda info: _pct(info.get("grossMargins")),
        },
        "weight_overrides": {"rev_growth": 0.25, "ebitda_margin": 0.10, "peg": 0.20,
                             "ev_ebitda": 0.15, "pe": 0.10, "mkt_cap": 0.20},
    },
    "retail": {
        "extra_cols": ["Gross Margin %", "SSS %", "Inv Turns"],
        "info_keys": {
            "Gross Margin %": lambda info: _pct(info.get("grossMargins")),
            "SSS %": lambda info: None,
            "Inv Turns": lambda info: _safe_div(
                info.get("totalRevenue", 0),
                info.get("inventory", 1)
            ),
        },
        "weight_overrides": {},
    },
    "industrials": {
        "extra_cols": ["ROIC %", "Book/Bill", "Backlog $B"],
        "info_keys": {
            "ROIC %": lambda info: _pct(info.get("returnOnCapital")),
            "Book/Bill": lambda info: None,
            "Backlog $B": lambda info: None,
        },
        "weight_overrides": {"ebitda_margin": 0.25, "peg": 0.20},
    },
    "financials": {
        "extra_cols": ["ROE %", "NIM %", "Efficiency Ratio"],
        "info_keys": {
            "ROE %": lambda info: _pct(info.get("returnOnEquity")),
            "NIM %": lambda info: None,
            "Efficiency Ratio": lambda info: None,
        },
        "weight_overrides": {"rev_growth": 0.10, "ebitda_margin": 0.10},
    },
}

# Default weights for composite score
DEFAULT_WEIGHTS = {
    "rev_growth": 0.20,
    "ebitda_margin": 0.20,
    "peg": 0.15,
    "ev_ebitda": 0.15,
    "pe": 0.15,
    "mkt_cap": 0.15,
}


# ═══════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════

def _pct(val) -> Optional[float]:
    if val is None:
        return None
    return round(val * 100, 1) if abs(val) < 5 else round(val, 1)


def _safe_div(a, b) -> Optional[float]:
    if not b or b == 0:
        return None
    return round(a / b, 2)


def _safe_add(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(a + b, 1)


def _billions(val) -> Optional[float]:
    if val is None or val == 0:
        return None
    return round(val / 1e9, 2)


def _fmt(val, fmt=",.1f") -> str:
    if val is None:
        return "—"
    try:
        return f"{val:{fmt}}"
    except (ValueError, TypeError):
        return str(val)


# ═══════════════════════════════════════════════════════════════
# Data Fetching
# ═══════════════════════════════════════════════════════════════

@dataclass
class TickerScreenData:
    """Screening data for a single ticker."""
    ticker: str = ""
    name: str = ""
    mkt_cap_b: float = 0
    ev_b: float = 0
    ev_ebitda: Optional[float] = None
    pe_fwd: Optional[float] = None
    pe_trail: Optional[float] = None
    rev_growth: Optional[float] = None
    eps_growth: Optional[float] = None
    ebitda_margin: Optional[float] = None
    peg: Optional[float] = None
    price: float = 0
    high_52w: float = 0
    low_52w: float = 0
    pct_from_high: Optional[float] = None
    sector_metrics: dict = field(default_factory=dict)
    fetch_error: str = ""
    composite_score: float = 0
    rank: int = 0


def fetch_ticker_data(ticker: str, sector: str = "") -> TickerScreenData:
    """Fetch screening metrics for a single ticker via yfinance."""
    if not HAS_YFINANCE:
        return TickerScreenData(ticker=ticker, fetch_error="yfinance not installed")

    result = TickerScreenData(ticker=ticker)

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        result.name = info.get("shortName", info.get("longName", ticker))[:30]
        result.price = info.get("currentPrice", info.get("regularMarketPrice", 0)) or 0

        # Market cap & EV
        mc = info.get("marketCap", 0) or 0
        result.mkt_cap_b = round(mc / 1e9, 2)
        ev = info.get("enterpriseValue", 0) or 0
        result.ev_b = round(ev / 1e9, 2)

        # Valuation multiples
        result.ev_ebitda = info.get("enterpriseToEbitda")
        if result.ev_ebitda and result.ev_ebitda < 0:
            result.ev_ebitda = None
        result.pe_fwd = info.get("forwardPE")
        if result.pe_fwd and result.pe_fwd < 0:
            result.pe_fwd = None
        result.pe_trail = info.get("trailingPE")
        if result.pe_trail and result.pe_trail < 0:
            result.pe_trail = None

        # Growth
        result.rev_growth = _pct(info.get("revenueGrowth"))
        result.eps_growth = _pct(info.get("earningsGrowth"))
        result.ebitda_margin = _pct(info.get("ebitdaMargins"))

        # PEG
        peg_raw = info.get("pegRatio")
        if peg_raw and 0 < peg_raw < 10:
            result.peg = round(peg_raw, 2)
        elif result.pe_fwd and result.eps_growth and result.eps_growth > 0:
            result.peg = round(result.pe_fwd / result.eps_growth, 2)

        # 52-week range
        result.high_52w = info.get("fiftyTwoWeekHigh", 0) or 0
        result.low_52w = info.get("fiftyTwoWeekLow", 0) or 0
        if result.price and result.high_52w:
            result.pct_from_high = round((result.price / result.high_52w - 1) * 100, 1)

        # Sector-specific metrics
        if sector and sector in SECTOR_METRICS:
            sm = SECTOR_METRICS[sector]
            for col_name, fn in sm["info_keys"].items():
                val = fn(info)
                if val is not None:
                    result.sector_metrics[col_name] = val

    except Exception as e:
        result.fetch_error = str(e)[:80]

    return result


# ═══════════════════════════════════════════════════════════════
# Composite Scoring
# ═══════════════════════════════════════════════════════════════

def compute_composite_scores(data: list[TickerScreenData], sector: str = "") -> list[TickerScreenData]:
    """
    Rank tickers by composite score.
    Higher is better. Each metric is rank-normalized (percentile within the group).
    """
    n = len(data)
    if n == 0:
        return data

    # Get weights
    weights = dict(DEFAULT_WEIGHTS)
    if sector and sector in SECTOR_METRICS:
        weights.update(SECTOR_METRICS[sector].get("weight_overrides", {}))

    # Normalize weights
    total_w = sum(weights.values())
    weights = {k: v / total_w for k, v in weights.items()}

    # Extract raw metric vectors
    def _vals(attr: str) -> list:
        return [getattr(d, attr) for d in data]

    metrics = {
        "rev_growth": _vals("rev_growth"),       # higher = better
        "ebitda_margin": _vals("ebitda_margin"),  # higher = better
        "peg": _vals("peg"),                      # lower = better (invert)
        "ev_ebitda": _vals("ev_ebitda"),           # lower = better (invert)
        "pe": _vals("pe_fwd"),                     # lower = better (invert)
        "mkt_cap": _vals("mkt_cap_b"),            # higher = better (liquidity)
    }

    inverted = {"peg", "ev_ebitda", "pe"}

    # Rank-normalize: percentile rank within group
    def _rank_normalize(values: list, invert: bool = False) -> list[float]:
        """Convert to 0-1 scores. None values get 0.5 (neutral)."""
        valid = [(i, v) for i, v in enumerate(values) if v is not None]
        scores = [0.5] * len(values)

        if len(valid) < 2:
            return scores

        sorted_valid = sorted(valid, key=lambda x: x[1], reverse=not invert)
        for rank, (idx, _) in enumerate(sorted_valid):
            scores[idx] = 1.0 - rank / (len(valid) - 1)

        return scores

    # Compute composite
    for i, d in enumerate(data):
        score = 0.0
        for metric_name, w in weights.items():
            vals = metrics[metric_name]
            normalized = _rank_normalize(vals, invert=(metric_name in inverted))
            score += w * normalized[i]
        d.composite_score = round(score, 4)

    # Rank
    ranked = sorted(data, key=lambda x: x.composite_score, reverse=True)
    for i, d in enumerate(ranked):
        d.rank = i + 1

    return ranked


# ═══════════════════════════════════════════════════════════════
# Triage Table Output
# ═══════════════════════════════════════════════════════════════

def format_triage_table(data: list[TickerScreenData], sector: str = "") -> str:
    """Format screening results as a markdown triage table."""
    lines = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines.append(f"# Universe Screen — {sector.title() if sector else 'General'}")
    lines.append(f"*Screened {len(data)} names | {timestamp} | Source: yfinance*\n")

    # Build header
    base_cols = ["Rank", "Ticker", "Name", "Mkt Cap $B", "EV/EBITDA",
                 "P/E Fwd", "Rev Gr %", "EBITDA Mg %", "PEG",
                 "Price", "% from 52w Hi", "Score"]

    extra_cols = []
    if sector and sector in SECTOR_METRICS:
        extra_cols = SECTOR_METRICS[sector]["extra_cols"]

    all_cols = base_cols + extra_cols
    header = "| " + " | ".join(all_cols) + " |"
    sep = "| " + " | ".join(["---"] * len(all_cols)) + " |"

    lines.append(header)
    lines.append(sep)

    for d in data:
        row = [
            str(d.rank),
            d.ticker,
            d.name[:20],
            _fmt(d.mkt_cap_b, ",.1f"),
            _fmt(d.ev_ebitda, ".1f") + "x" if d.ev_ebitda else "—",
            _fmt(d.pe_fwd, ".1f") + "x" if d.pe_fwd else "—",
            _fmt(d.rev_growth),
            _fmt(d.ebitda_margin),
            _fmt(d.peg, ".2f"),
            f"${d.price:,.0f}" if d.price else "—",
            _fmt(d.pct_from_high) + "%" if d.pct_from_high is not None else "—",
            _fmt(d.composite_score, ".3f"),
        ]

        # Add sector-specific columns
        for col_name in extra_cols:
            val = d.sector_metrics.get(col_name)
            row.append(_fmt(val) if val is not None else "—")

        lines.append("| " + " | ".join(row) + " |")

    # Error footnotes
    errors = [d for d in data if d.fetch_error]
    if errors:
        lines.append("")
        lines.append("**Fetch errors:**")
        for d in errors:
            lines.append(f"- {d.ticker}: {d.fetch_error}")

    return "\n".join(lines)


def format_console_table(data: list[TickerScreenData]) -> str:
    """Compact console-friendly table for terminal output."""
    lines = []
    lines.append(f"{'Rank':>4}  {'Ticker':<6}  {'Name':<20}  {'MktCap':>8}  {'EV/EBITDA':>9}  "
                 f"{'P/E':>6}  {'RevGr':>6}  {'EBITDA%':>7}  {'PEG':>5}  {'Score':>6}")
    lines.append("─" * 95)

    for d in data:
        lines.append(
            f"{d.rank:>4}  {d.ticker:<6}  {d.name[:20]:<20}  "
            f"${d.mkt_cap_b:>6.1f}B  "
            f"{_fmt(d.ev_ebitda, '.1f') + 'x':>9}  "
            f"{_fmt(d.pe_fwd, '.1f') + 'x':>6}  "
            f"{_fmt(d.rev_growth):>5}%  "
            f"{_fmt(d.ebitda_margin):>6}%  "
            f"{_fmt(d.peg, '.2f'):>5}  "
            f"{d.composite_score:>6.3f}"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════

def screen_universe(
    tickers: list[str],
    sector: str = "",
    save_markdown: bool = True,
    verbose: bool = True,
) -> list[TickerScreenData]:
    """
    Screen a universe of tickers with composite scoring.

    Args:
        tickers: List of ticker symbols
        sector: Sector key for extra metrics (restaurant, saas, retail, etc.)
        save_markdown: Save triage table to /workspace/investment-workbench/data/results/
        verbose: Print console output

    Returns:
        Ranked list of TickerScreenData
    """
    if verbose:
        print(f"\n  Screening {len(tickers)} tickers ({sector or 'general'})...")

    # Fetch data for each ticker
    data = []
    for ticker in tickers:
        if verbose:
            print(f"    Fetching {ticker}...", end=" ", flush=True)
        d = fetch_ticker_data(ticker.upper(), sector)
        if verbose:
            if d.fetch_error:
                print(f"⚠ {d.fetch_error}")
            else:
                print(f"${d.price:,.0f} | MktCap ${d.mkt_cap_b:.1f}B")
        data.append(d)

    # Score and rank
    ranked = compute_composite_scores(data, sector)

    # Output
    if verbose:
        print(f"\n{format_console_table(ranked)}\n")

    md_table = format_triage_table(ranked, sector)

    if save_markdown:
        out_dir = Path("/workspace/investment-workbench/data/results")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"universe_{sector or 'general'}_{ts}.md"
        out_path.write_text(md_table)
        if verbose:
            print(f"  Saved: {out_path}")

    return ranked


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Universe Screening Tool")
    parser.add_argument("--tickers", required=True, help="Comma-separated ticker list")
    parser.add_argument("--sector", default="", help="Sector: restaurant, saas, retail, industrials, financials")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")]
    screen_universe(tickers=tickers, sector=args.sector)
