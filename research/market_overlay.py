"""
Market Structure Overlay

Sits DOWNSTREAM of the core research engine. Runs AFTER:
  orientation → schema → estimate → adversarial → decision gate

Produces one SETUP_ASSESSMENT workpaper with:
  - short interest / days to cover
  - next earnings date
  - implied volatility / implied move
  - put/call context
  - simple positioning note
  - clear labeling of missing/unavailable data

Uses only free data (yfinance). Degrades gracefully when
data is unavailable — reports what's missing rather than
failing silently or guessing.

This module NEVER:
  - drives schema selection
  - replaces estimate work
  - overrides the core thesis
  - generates trade signals from flow alone
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math


@dataclass
class MarketData:
    """Raw market data fetched from free sources."""
    ticker: str = ""
    fetch_time: str = ""
    price: float = None
    market_cap: float = None
    beta: float = None
    short_pct_float: float = None
    short_ratio: float = None       # days to cover
    avg_volume: float = None
    earnings_dates: list = field(default_factory=list)
    implied_volatility: float = None
    options_available: bool = False
    put_call_ratio: float = None     # aggregate put OI / call OI
    near_term_iv: float = None
    historical_vol_30d: float = None
    errors: list = field(default_factory=list)
    data_quality: str = "none"       # "good", "partial", "minimal", "none"


def fetch_market_data(ticker: str) -> MarketData:
    """
    Fetch market-structure data from yfinance.
    Returns whatever is available; reports what's missing.
    """
    md = MarketData(ticker=ticker, fetch_time=datetime.utcnow().isoformat())
    fields_found = 0
    fields_total = 8

    try:
        import yfinance as yf
        import io, sys as _sys, logging
        # Suppress yfinance noise
        _stderr = _sys.stderr
        _sys.stderr = io.StringIO()
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        try:
            t = yf.Ticker(ticker)

            # Basic info
            try:
                info = t.info or {}
                md.price = info.get("currentPrice") or info.get("regularMarketPrice")
                md.market_cap = info.get("marketCap")
                md.beta = info.get("beta")
                md.short_pct_float = info.get("shortPercentOfFloat")
                if md.short_pct_float and md.short_pct_float > 1:
                    md.short_pct_float = md.short_pct_float / 100
                md.short_ratio = info.get("shortRatio")
                md.avg_volume = info.get("averageVolume")
                md.implied_volatility = info.get("impliedVolatility")
                if md.price: fields_found += 1
                if md.short_pct_float: fields_found += 1
                if md.short_ratio: fields_found += 1
                if md.beta: fields_found += 1
            except Exception as e:
                md.errors.append(f"Info fetch: {type(e).__name__}")

            # Earnings dates
            try:
                cal = t.calendar
                if cal is not None and isinstance(cal, dict):
                    ed = cal.get("Earnings Date", [])
                    if ed:
                        md.earnings_dates = [str(d) for d in ed[:2]]
                        fields_found += 1
            except Exception:
                pass

            # Options
            try:
                exps = t.options
                if exps:
                    md.options_available = True
                    chain = t.option_chain(exps[0])
                    total_call_oi = chain.calls["openInterest"].sum()
                    total_put_oi = chain.puts["openInterest"].sum()
                    if total_call_oi > 0:
                        md.put_call_ratio = round(total_put_oi / total_call_oi, 2)
                        fields_found += 1
                    if md.price:
                        calls = chain.calls
                        atm = calls.iloc[(calls["strike"] - md.price).abs().argsort()[:1]]
                        if not atm.empty and "impliedVolatility" in atm.columns:
                            md.near_term_iv = round(atm["impliedVolatility"].values[0], 4)
                            fields_found += 1
            except Exception:
                pass

            # Historical volatility
            try:
                hist = t.history(period="3mo")
                if hist is not None and len(hist) > 20:
                    returns = hist["Close"].pct_change().dropna()
                    md.historical_vol_30d = round(returns.tail(21).std() * math.sqrt(252), 4)
                    fields_found += 1
            except Exception:
                pass

        finally:
            _sys.stderr = _stderr

    except ImportError:
        md.errors.append("yfinance not installed")
    except Exception as e:
        md.errors.append(f"Connection failed: {type(e).__name__}")

    # Quality assessment
    if fields_found >= 6:
        md.data_quality = "good"
    elif fields_found >= 3:
        md.data_quality = "partial"
    elif fields_found >= 1:
        md.data_quality = "minimal"
    else:
        md.data_quality = "none"

    return md


# ═══════════════════════════════════════════════════════════════
# Setup Assessment
# ═══════════════════════════════════════════════════════════════

@dataclass
class SetupAssessment:
    """The overlay's output: one concise positioning/setup read."""
    ticker: str = ""
    thesis_eps: float = None
    consensus_eps: float = None
    variant_pct: float = None        # our EPS vs consensus, %

    # Market structure facts
    short_interest_pct: float = None
    days_to_cover: float = None
    short_context: str = ""          # "low", "moderate", "elevated", "high"
    next_earnings: str = ""
    implied_move_pct: float = None
    put_call_ratio: float = None
    put_call_context: str = ""       # "bearish skew", "neutral", "bullish skew"
    vol_context: str = ""            # IV vs HV comparison
    historical_vol: float = None
    implied_vol: float = None

    # Assessment
    setup_note: str = ""             # one-line positioning summary
    data_quality: str = "none"
    missing_data: list = field(default_factory=list)
    caveats: list = field(default_factory=list)


def assess_setup(
    ticker: str,
    research_result: dict,
    market_data: MarketData = None,
) -> SetupAssessment:
    """
    Produce a setup assessment from a completed research run + market data.

    This runs AFTER the fundamental engine. It reads the thesis;
    it never writes to or overrides the thesis.
    """
    if market_data is None:
        market_data = fetch_market_data(ticker)

    sa = SetupAssessment(
        ticker=ticker,
        thesis_eps=research_result.get("post_eps"),
        consensus_eps=research_result.get("consensus_eps"),
        data_quality=market_data.data_quality,
    )

    # Variant view
    if sa.thesis_eps and sa.consensus_eps and sa.consensus_eps != 0:
        sa.variant_pct = round((sa.thesis_eps - sa.consensus_eps) / sa.consensus_eps * 100, 1)

    # Short interest
    if market_data.short_pct_float is not None:
        sa.short_interest_pct = round(market_data.short_pct_float * 100, 1)
        if sa.short_interest_pct > 10:
            sa.short_context = "high"
        elif sa.short_interest_pct > 5:
            sa.short_context = "elevated"
        elif sa.short_interest_pct > 2:
            sa.short_context = "moderate"
        else:
            sa.short_context = "low"
    else:
        sa.missing_data.append("short interest")

    # Days to cover
    if market_data.short_ratio is not None:
        sa.days_to_cover = round(market_data.short_ratio, 1)
    else:
        sa.missing_data.append("days to cover")

    # Earnings date
    if market_data.earnings_dates:
        sa.next_earnings = market_data.earnings_dates[0]
    else:
        sa.missing_data.append("earnings date")

    # Implied move
    if market_data.near_term_iv is not None and market_data.earnings_dates:
        # Rough implied move = IV × sqrt(days to expiry / 365)
        # For earnings, approximate as ~1 day event: IV × sqrt(1/252)
        sa.implied_vol = round(market_data.near_term_iv * 100, 1)
        sa.implied_move_pct = round(market_data.near_term_iv / math.sqrt(252) * 100, 1)
    elif market_data.implied_volatility is not None:
        sa.implied_vol = round(market_data.implied_volatility * 100, 1)
    else:
        sa.missing_data.append("implied volatility")

    # Put/call
    if market_data.put_call_ratio is not None:
        sa.put_call_ratio = market_data.put_call_ratio
        if sa.put_call_ratio > 1.2:
            sa.put_call_context = "bearish skew"
        elif sa.put_call_ratio < 0.7:
            sa.put_call_context = "bullish skew"
        else:
            sa.put_call_context = "neutral"
    else:
        sa.missing_data.append("put/call ratio")

    # Volatility context
    if market_data.historical_vol_30d is not None:
        sa.historical_vol = round(market_data.historical_vol_30d * 100, 1)
        if sa.implied_vol:
            ratio = sa.implied_vol / sa.historical_vol if sa.historical_vol > 0 else 1
            if ratio > 1.2:
                sa.vol_context = "IV premium — market expects more movement than recent history"
            elif ratio < 0.8:
                sa.vol_context = "IV discount — market expects less movement than recent history"
            else:
                sa.vol_context = "IV roughly in line with realized vol"
    else:
        sa.missing_data.append("historical volatility")

    # ── Build one-line setup note ──
    notes = []

    if sa.variant_pct is not None:
        direction = "above" if sa.variant_pct > 0 else "below"
        notes.append(f"Estimate {abs(sa.variant_pct):.1f}% {direction} consensus")

    if sa.short_context:
        notes.append(f"short interest {sa.short_context} ({sa.short_interest_pct:.1f}%)")

    if sa.put_call_context:
        notes.append(f"P/C {sa.put_call_context} ({sa.put_call_ratio:.2f})")

    if sa.implied_move_pct and sa.variant_pct:
        if abs(sa.variant_pct) > sa.implied_move_pct * 2:
            notes.append("variant exceeds implied move — setup may support position")
        elif abs(sa.variant_pct) < sa.implied_move_pct * 0.5:
            notes.append("variant within noise — setup doesn't clearly support position")

    if notes:
        sa.setup_note = ". ".join(notes) + "."
    else:
        sa.setup_note = "Insufficient data for setup assessment."

    # Caveats
    if market_data.data_quality == "none":
        sa.caveats.append("No market data available — setup assessment is empty")
    elif market_data.data_quality == "minimal":
        sa.caveats.append("Very limited market data — setup assessment is unreliable")
    if len(sa.missing_data) >= 3:
        sa.caveats.append(f"Missing: {', '.join(sa.missing_data)}")

    return sa


# ═══════════════════════════════════════════════════════════════
# Workpaper Production
# ═══════════════════════════════════════════════════════════════

def produce_setup_workpaper(conn, company_id: str, sa: SetupAssessment,
                             run_id: str = None) -> str:
    """Produce a SETUP_ASSESSMENT workpaper."""
    from research.escalation import WorkpaperBuilder

    wb = WorkpaperBuilder(conn, company_id)

    content = {
        "ticker": sa.ticker,
        "thesis_eps": sa.thesis_eps,
        "consensus_eps": sa.consensus_eps,
        "variant_pct": sa.variant_pct,
        "short_interest_pct": sa.short_interest_pct,
        "days_to_cover": sa.days_to_cover,
        "short_context": sa.short_context,
        "next_earnings": sa.next_earnings,
        "implied_move_pct": sa.implied_move_pct,
        "implied_vol": sa.implied_vol,
        "historical_vol": sa.historical_vol,
        "vol_context": sa.vol_context,
        "put_call_ratio": sa.put_call_ratio,
        "put_call_context": sa.put_call_context,
        "setup_note": sa.setup_note,
        "data_quality": sa.data_quality,
        "missing_data": sa.missing_data,
        "caveats": sa.caveats,
    }

    wid = wb.create(
        workpaper_type="SETUP_ASSESSMENT",
        title=f"{sa.ticker} Setup / Positioning Assessment",
        content=content,
        question="Given our thesis, what does market structure say about setup?",
        methodology="Market-structure overlay using free data (yfinance). "
                    "Runs AFTER core research. Does not affect thesis or estimate. "
                    f"Data quality: {sa.data_quality}. "
                    f"Missing: {', '.join(sa.missing_data) if sa.missing_data else 'none'}.",
        run_id=run_id,
    )

    return wid


def format_setup(sa: SetupAssessment) -> str:
    """Format the setup assessment for CLI display."""
    lines = []
    lines.append(f"\n── Market Structure Overlay ({sa.data_quality} data) ──")

    if sa.data_quality == "none":
        lines.append("  No market data available (network restricted or yfinance unavailable)")
        if sa.variant_pct is not None:
            lines.append(f"  Thesis variant: {sa.variant_pct:+.1f}% vs consensus")
        lines.append("  Setup assessment: unavailable")
        return "\n".join(lines)

    # Facts section
    if sa.short_interest_pct is not None:
        dtc = f", {sa.days_to_cover:.1f} days to cover" if sa.days_to_cover else ""
        lines.append(f"  Short interest: {sa.short_interest_pct:.1f}% of float ({sa.short_context}){dtc}")

    if sa.next_earnings:
        lines.append(f"  Next earnings: {sa.next_earnings}")

    if sa.implied_vol is not None:
        iv_line = f"  Implied vol: {sa.implied_vol:.1f}%"
        if sa.implied_move_pct:
            iv_line += f" (implied move ~{sa.implied_move_pct:.1f}%)"
        lines.append(iv_line)

    if sa.historical_vol is not None:
        lines.append(f"  Realized vol (30d): {sa.historical_vol:.1f}%")

    if sa.vol_context:
        lines.append(f"  Vol context: {sa.vol_context}")

    if sa.put_call_ratio is not None:
        lines.append(f"  Put/call OI: {sa.put_call_ratio:.2f} ({sa.put_call_context})")

    # Assessment
    lines.append(f"\n  Setup: {sa.setup_note}")

    if sa.caveats:
        for c in sa.caveats:
            lines.append(f"  ⚠ {c}")

    if sa.missing_data:
        lines.append(f"  Missing: {', '.join(sa.missing_data)}")

    return "\n".join(lines)
