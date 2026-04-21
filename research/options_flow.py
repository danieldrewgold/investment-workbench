"""
Options Flow Analyzer

Extracts positioning signals from the full options chain:
  - Expected move (straddle-derived)
  - IV term structure across expirations
  - Skew analysis (put vs call IV)
  - Unusual activity detection (volume vs OI)
  - Volume-based put/call ratio
  - Max pain calculation

Uses only free data (yfinance). Degrades gracefully when
data is unavailable or sparse.

This module NEVER:
  - generates trade signals alone
  - overrides the core thesis
  - replaces fundamental research
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
import math


def _safe_float(val, default=0.0):
    """Convert a value to float, treating NaN/None/inf as default."""
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


@dataclass
class OptionsFlowAnalysis:
    """Full options-derived positioning read."""

    # Expected move
    expected_move_pct: float = None          # straddle-derived, nearest expiry
    expected_move_earnings_pct: float = None  # straddle for expiry nearest earnings

    # Term structure
    iv_term_structure: list = field(default_factory=list)  # [{expiry, days_out, atm_iv}]
    term_structure_shape: str = ""  # "normal", "inverted", "flat", "humped"

    # Skew
    put_skew_25d: float = None     # IV of ~25-delta put minus ATM IV
    call_skew_25d: float = None    # IV of ~25-delta call minus ATM IV
    skew_signal: str = ""          # "heavy_put_hedging", "call_chasing", "balanced"

    # Unusual activity
    unusual_calls: list = field(default_factory=list)   # [{strike, expiry, volume, oi, ratio}]
    unusual_puts: list = field(default_factory=list)
    flow_bias: str = ""            # "bullish_flow", "bearish_flow", "mixed", "quiet"

    # Volume-based P/C
    volume_put_call_ratio: float = None

    # Max pain
    max_pain_strike: float = None
    max_pain_vs_price_pct: float = None  # (max_pain - price) / price * 100

    # Composite
    positioning_signal: str = ""   # "bullish", "bearish", "neutral", "conflicted"
    confidence: float = 0.0        # 0-1, based on data completeness and signal clarity
    summary: str = ""              # one-line read
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "expected_move_pct": self.expected_move_pct,
            "expected_move_earnings_pct": self.expected_move_earnings_pct,
            "iv_term_structure": self.iv_term_structure,
            "term_structure_shape": self.term_structure_shape,
            "put_skew_25d": self.put_skew_25d,
            "call_skew_25d": self.call_skew_25d,
            "skew_signal": self.skew_signal,
            "unusual_calls": self.unusual_calls[:5],
            "unusual_puts": self.unusual_puts[:5],
            "flow_bias": self.flow_bias,
            "volume_put_call_ratio": self.volume_put_call_ratio,
            "max_pain_strike": self.max_pain_strike,
            "max_pain_vs_price_pct": self.max_pain_vs_price_pct,
            "positioning_signal": self.positioning_signal,
            "confidence": self.confidence,
            "summary": self.summary,
        }


def analyze_options_flow(
    ticker: str,
    price: float,
    earnings_date_str: str = None,
) -> OptionsFlowAnalysis:
    """
    Analyze the full options chain for positioning signals.

    Args:
        ticker: stock ticker
        price: current stock price
        earnings_date_str: next earnings date as "YYYY-MM-DD" (optional)
    """
    ofa = OptionsFlowAnalysis()

    if not price or price <= 0:
        ofa.errors.append("No valid price")
        return ofa

    try:
        import yfinance as yf
        import io, sys as _sys, logging
        _stderr = _sys.stderr
        _sys.stderr = io.StringIO()
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        try:
            chains = _fetch_all_chains(yf.Ticker(ticker))
        finally:
            _sys.stderr = _stderr
    except ImportError:
        ofa.errors.append("yfinance not installed")
        return ofa
    except Exception as e:
        ofa.errors.append(f"Fetch failed: {type(e).__name__}")
        return ofa

    if not chains:
        ofa.errors.append("No options chains available")
        return ofa

    # Parse earnings date
    earnings_date = None
    if earnings_date_str:
        try:
            earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass

    # Run each analysis, catching errors individually
    try:
        _compute_expected_move(ofa, chains, price, earnings_date)
    except Exception as e:
        ofa.errors.append(f"Expected move: {type(e).__name__}")

    try:
        _analyze_term_structure(ofa, chains, price)
    except Exception as e:
        ofa.errors.append(f"Term structure: {type(e).__name__}")

    # Skew on nearest expiry
    nearest_expiry = sorted(chains.keys())[0]
    try:
        _analyze_skew(ofa, chains[nearest_expiry], price)
    except Exception as e:
        ofa.errors.append(f"Skew: {type(e).__name__}")

    try:
        _detect_unusual_activity(ofa, chains)
    except Exception as e:
        ofa.errors.append(f"Unusual activity: {type(e).__name__}")

    try:
        _compute_max_pain(ofa, chains[nearest_expiry], price)
    except Exception as e:
        ofa.errors.append(f"Max pain: {type(e).__name__}")

    # Volume P/C across all chains
    try:
        _compute_volume_pc(ofa, chains)
    except Exception as e:
        ofa.errors.append(f"Volume P/C: {type(e).__name__}")

    # Synthesize all signals
    _synthesize_signals(ofa)

    return ofa


# ═══════════════════════════════════════════════════════════════
# Chain Fetching
# ═══════════════════════════════════════════════════════════════

def _fetch_all_chains(ticker_obj) -> dict:
    """
    Fetch options chains for all available expirations.
    Returns {expiry_str: (calls_df, puts_df)}.
    Limits to first 6 expirations to avoid excessive API calls.
    """
    exps = ticker_obj.options
    if not exps:
        return {}

    chains = {}
    for exp in exps[:6]:
        try:
            chain = ticker_obj.option_chain(exp)
            if chain.calls is not None and chain.puts is not None:
                if len(chain.calls) > 0 and len(chain.puts) > 0:
                    chains[exp] = (chain.calls, chain.puts)
        except Exception:
            continue
    return chains


# ═══════════════════════════════════════════════════════════════
# Expected Move (Straddle-Derived)
# ═══════════════════════════════════════════════════════════════

def _compute_expected_move(ofa, chains, price, earnings_date):
    """
    ATM straddle price / stock price = market's priced-in move.
    More accurate than IV * sqrt(1/252).
    """
    sorted_expiries = sorted(chains.keys())

    # Nearest expiry expected move
    nearest = sorted_expiries[0]
    calls, puts = chains[nearest]
    move = _straddle_expected_move(calls, puts, price)
    if move is not None:
        ofa.expected_move_pct = round(move, 2)

    # Earnings-specific expected move
    if earnings_date:
        best_expiry = _find_nearest_expiry(sorted_expiries, earnings_date)
        if best_expiry and best_expiry in chains:
            ec, ep = chains[best_expiry]
            emove = _straddle_expected_move(ec, ep, price)
            if emove is not None:
                ofa.expected_move_earnings_pct = round(emove, 2)


def _straddle_expected_move(calls, puts, price):
    """
    Compute ATM straddle price as % of stock price.

    Prefers mid-price (bid+ask)/2 over lastPrice since lastPrice
    can be hours stale. Subtracts intrinsic value when the nearest
    strike isn't exactly ATM to avoid inflating the expected move.
    """
    if "strike" not in calls.columns:
        return None

    atm_call = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
    atm_put = puts.iloc[(puts["strike"] - price).abs().argsort()[:1]]

    if atm_call.empty or atm_put.empty:
        return None

    call_strike = _safe_float(atm_call["strike"].values[0])
    put_strike = _safe_float(atm_put["strike"].values[0])

    # Get option prices — prefer mid-price from bid/ask over stale lastPrice
    call_price = _get_option_mid(atm_call, calls.columns)
    put_price = _get_option_mid(atm_put, puts.columns)

    if call_price is None or put_price is None:
        return None

    # Subtract intrinsic value so we measure extrinsic (time value) only.
    # ATM straddle extrinsic ≈ market's expected move for that expiry.
    call_intrinsic = max(price - call_strike, 0)
    put_intrinsic = max(put_strike - price, 0)
    call_extrinsic = max(call_price - call_intrinsic, 0)
    put_extrinsic = max(put_price - put_intrinsic, 0)

    straddle_extrinsic = call_extrinsic + put_extrinsic
    if straddle_extrinsic <= 0:
        return None

    return (straddle_extrinsic / price) * 100


def _get_option_mid(atm_row, columns):
    """
    Get the best available option price from a single-row ATM selection.
    Prefers mid-price (bid+ask)/2, falls back to lastPrice.
    Returns None if no valid price found.
    """
    # Try mid-price first
    if "bid" in columns and "ask" in columns:
        bid = _safe_float(atm_row["bid"].values[0])
        ask = _safe_float(atm_row["ask"].values[0])
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2

    # Fall back to lastPrice
    if "lastPrice" in columns:
        lp = _safe_float(atm_row["lastPrice"].values[0])
        if lp > 0:
            return lp

    return None


def _find_nearest_expiry(expiries, target_date):
    """Find the expiry closest to (but not before) target_date."""
    best = None
    best_diff = float("inf")
    for exp_str in expiries:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = (exp_date - target_date).days
        if diff >= 0 and diff < best_diff:
            best_diff = diff
            best = exp_str
    # If nothing after earnings, take closest before
    if best is None:
        for exp_str in reversed(expiries):
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            diff = abs((exp_date - target_date).days)
            if diff < best_diff:
                best_diff = diff
                best = exp_str
    return best


# ═══════════════════════════════════════════════════════════════
# IV Term Structure
# ═══════════════════════════════════════════════════════════════

def _analyze_term_structure(ofa, chains, price):
    """ATM IV across expirations — shape reveals market expectations."""
    today = date.today()
    term = []

    for exp_str in sorted(chains.keys()):
        calls, puts = chains[exp_str]
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        days_out = max((exp_date - today).days, 1)

        atm_iv = _get_atm_iv(calls, price)
        if atm_iv is not None and atm_iv > 0:
            term.append({
                "expiry": exp_str,
                "days_out": days_out,
                "atm_iv": round(atm_iv * 100, 1),
            })

    ofa.iv_term_structure = term

    if len(term) < 2:
        ofa.term_structure_shape = "insufficient_data"
        return

    # Classify shape
    ivs = [t["atm_iv"] for t in term]
    front = ivs[0]
    back = ivs[-1]
    mid = ivs[len(ivs) // 2] if len(ivs) >= 3 else (front + back) / 2

    if front > back * 1.15:
        ofa.term_structure_shape = "inverted"  # elevated near-term fear
    elif back > front * 1.15:
        ofa.term_structure_shape = "normal"  # rising — event ahead
    elif mid > max(front, back) * 1.1:
        ofa.term_structure_shape = "humped"  # mid-term event priced
    else:
        ofa.term_structure_shape = "flat"


def _get_atm_iv(calls, price):
    """Get ATM implied volatility from a calls dataframe."""
    if "impliedVolatility" not in calls.columns:
        return None
    atm = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
    if atm.empty:
        return None
    iv = _safe_float(atm["impliedVolatility"].values[0])
    return iv if iv > 0 else None


# ═══════════════════════════════════════════════════════════════
# Skew Analysis
# ═══════════════════════════════════════════════════════════════

def _analyze_skew(ofa, chain_tuple, price):
    """
    Compare OTM put IV vs OTM call IV at ~25-delta equivalent distance.
    Approximation: 25-delta ~ 5-8% OTM for typical vol levels.
    """
    calls, puts = chain_tuple

    if "impliedVolatility" not in calls.columns or "impliedVolatility" not in puts.columns:
        ofa.skew_signal = "insufficient_data"
        return

    atm_iv = _get_atm_iv(calls, price)
    if atm_iv is None:
        ofa.skew_signal = "insufficient_data"
        return

    # ~25-delta approximation: strikes ~5-10% OTM
    otm_put_target = price * 0.93   # ~7% OTM put
    otm_call_target = price * 1.07  # ~7% OTM call

    # Find OTM put IV — filter NaN from impliedVolatility before comparison
    valid_puts = puts.dropna(subset=["impliedVolatility"])
    otm_puts = valid_puts[(valid_puts["strike"] < price) & (valid_puts["impliedVolatility"] > 0)]
    if not otm_puts.empty:
        nearest_put = otm_puts.iloc[(otm_puts["strike"] - otm_put_target).abs().argsort()[:1]]
        put_iv = _safe_float(nearest_put["impliedVolatility"].values[0])
        if put_iv > 0:
            ofa.put_skew_25d = round((put_iv - atm_iv) * 100, 1)  # in vol points

    # Find OTM call IV
    valid_calls = calls.dropna(subset=["impliedVolatility"])
    otm_calls = valid_calls[(valid_calls["strike"] > price) & (valid_calls["impliedVolatility"] > 0)]
    if not otm_calls.empty:
        nearest_call = otm_calls.iloc[(otm_calls["strike"] - otm_call_target).abs().argsort()[:1]]
        call_iv = _safe_float(nearest_call["impliedVolatility"].values[0])
        if call_iv > 0:
            ofa.call_skew_25d = round((call_iv - atm_iv) * 100, 1)

    # Classify skew
    if ofa.put_skew_25d is not None and ofa.call_skew_25d is not None:
        if ofa.put_skew_25d > 8 and ofa.put_skew_25d > ofa.call_skew_25d + 5:
            ofa.skew_signal = "heavy_put_hedging"
        elif ofa.call_skew_25d > 5 and ofa.call_skew_25d > ofa.put_skew_25d + 3:
            ofa.skew_signal = "call_chasing"
        else:
            ofa.skew_signal = "balanced"
    elif ofa.put_skew_25d is not None and ofa.put_skew_25d > 8:
        ofa.skew_signal = "heavy_put_hedging"
    elif ofa.call_skew_25d is not None and ofa.call_skew_25d > 5:
        ofa.skew_signal = "call_chasing"
    else:
        ofa.skew_signal = "balanced"


# ═══════════════════════════════════════════════════════════════
# Unusual Activity Detection
# ═══════════════════════════════════════════════════════════════

def _detect_unusual_activity(ofa, chains):
    """
    Identify strikes where volume >> open interest (new positions).
    Ratio > 3x with meaningful volume = unusual.

    Volume threshold scales with the chain's median OI so it works
    across micro-caps (low OI) and mega-caps (high OI).
    """
    unusual_calls = []
    unusual_puts = []

    # Compute adaptive volume threshold from median OI across all chains
    all_oi = []
    for _, (calls, puts) in chains.items():
        for df in (calls, puts):
            if "openInterest" in df.columns:
                ois = df["openInterest"].dropna().tolist()
                all_oi.extend([_safe_float(x) for x in ois if _safe_float(x) > 0])
    if all_oi:
        all_oi.sort()
        median_oi = all_oi[len(all_oi) // 2]
        # Threshold: at least 50 contracts, but scale up for liquid names
        vol_threshold = max(50, median_oi * 0.5)
    else:
        vol_threshold = 100

    for exp_str, (calls, puts) in chains.items():
        for df, target_list, side in [(calls, unusual_calls, "call"), (puts, unusual_puts, "put")]:
            if "volume" not in df.columns or "openInterest" not in df.columns:
                continue

            for _, row in df.iterrows():
                vol = _safe_float(row.get("volume", 0))
                oi = _safe_float(row.get("openInterest", 0))

                if vol < vol_threshold:
                    continue

                ratio = vol / max(oi, 1)
                if ratio >= 3.0:
                    target_list.append({
                        "strike": _safe_float(row["strike"]),
                        "expiry": exp_str,
                        "volume": int(vol),
                        "oi": int(oi),
                        "ratio": round(ratio, 1),
                    })

    # Sort by volume descending, keep top 10
    unusual_calls.sort(key=lambda x: x["volume"], reverse=True)
    unusual_puts.sort(key=lambda x: x["volume"], reverse=True)
    ofa.unusual_calls = unusual_calls[:10]
    ofa.unusual_puts = unusual_puts[:10]

    # Classify flow bias
    total_unusual_call_vol = sum(x["volume"] for x in ofa.unusual_calls)
    total_unusual_put_vol = sum(x["volume"] for x in ofa.unusual_puts)
    total = total_unusual_call_vol + total_unusual_put_vol

    if total == 0:
        ofa.flow_bias = "quiet"
    elif total_unusual_call_vol > total_unusual_put_vol * 2:
        ofa.flow_bias = "bullish_flow"
    elif total_unusual_put_vol > total_unusual_call_vol * 2:
        ofa.flow_bias = "bearish_flow"
    elif total > 0:
        ofa.flow_bias = "mixed"
    else:
        ofa.flow_bias = "quiet"


# ═══════════════════════════════════════════════════════════════
# Max Pain
# ═══════════════════════════════════════════════════════════════

def _compute_max_pain(ofa, chain_tuple, price):
    """
    Max pain = strike where total ITM value of all options is minimized.
    Price gravitational center for near-term expiry.
    """
    calls, puts = chain_tuple

    if "openInterest" not in calls.columns or "openInterest" not in puts.columns:
        return

    strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
    if not strikes:
        return

    # Build OI maps, converting NaN to 0
    call_oi = {_safe_float(k): _safe_float(v) for k, v in
               zip(calls["strike"], calls["openInterest"].fillna(0))}
    put_oi = {_safe_float(k): _safe_float(v) for k, v in
              zip(puts["strike"], puts["openInterest"].fillna(0))}

    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for test_price in strikes:
        total_pain = 0
        # Call holders' pain: for each call, if test_price > strike, call is ITM
        for strike, oi in call_oi.items():
            if test_price > strike:
                total_pain += (test_price - strike) * oi
        # Put holders' pain: for each put, if test_price < strike, put is ITM
        for strike, oi in put_oi.items():
            if test_price < strike:
                total_pain += (strike - test_price) * oi

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = test_price

    ofa.max_pain_strike = max_pain_strike
    if price > 0:
        ofa.max_pain_vs_price_pct = round((max_pain_strike - price) / price * 100, 2)


# ═══════════════════════════════════════════════════════════════
# Volume Put/Call Ratio
# ═══════════════════════════════════════════════════════════════

def _compute_volume_pc(ofa, chains):
    """Volume-based P/C ratio across all expirations. More current than OI."""
    total_call_vol = 0.0
    total_put_vol = 0.0

    for _, (calls, puts) in chains.items():
        if "volume" in calls.columns:
            total_call_vol += _safe_float(calls["volume"].fillna(0).sum())
        if "volume" in puts.columns:
            total_put_vol += _safe_float(puts["volume"].fillna(0).sum())

    if total_call_vol > 0:
        ofa.volume_put_call_ratio = round(total_put_vol / total_call_vol, 2)


# ═══════════════════════════════════════════════════════════════
# Signal Synthesis
# ═══════════════════════════════════════════════════════════════

def _synthesize_signals(ofa):
    """Combine all signals into a composite positioning read."""
    votes = []  # (direction, weight)  direction: +1 bullish, -1 bearish, 0 neutral

    # Skew signal
    if ofa.skew_signal == "heavy_put_hedging":
        votes.append((-1, 1.0))
    elif ofa.skew_signal == "call_chasing":
        votes.append((+1, 1.0))
    elif ofa.skew_signal == "balanced":
        votes.append((0, 0.5))

    # Flow bias
    if ofa.flow_bias == "bullish_flow":
        votes.append((+1, 1.5))
    elif ofa.flow_bias == "bearish_flow":
        votes.append((-1, 1.5))
    elif ofa.flow_bias == "mixed":
        votes.append((0, 0.3))

    # Volume P/C
    if ofa.volume_put_call_ratio is not None:
        if ofa.volume_put_call_ratio > 1.5:
            votes.append((-1, 1.0))
        elif ofa.volume_put_call_ratio < 0.5:
            votes.append((+1, 1.0))
        else:
            votes.append((0, 0.5))

    # Term structure
    if ofa.term_structure_shape == "inverted":
        votes.append((-1, 0.7))  # near-term fear
    elif ofa.term_structure_shape == "normal":
        votes.append((0, 0.3))

    # Max pain
    if ofa.max_pain_vs_price_pct is not None:
        if ofa.max_pain_vs_price_pct > 3:
            votes.append((+1, 0.5))  # max pain above price = upward pull
        elif ofa.max_pain_vs_price_pct < -3:
            votes.append((-1, 0.5))

    if not votes:
        ofa.positioning_signal = "no_data"
        ofa.confidence = 0.0
        ofa.summary = "No options data for positioning assessment."
        return

    # Weighted vote
    total_weight = sum(w for _, w in votes)
    weighted_score = sum(d * w for d, w in votes) / total_weight if total_weight > 0 else 0

    # Agreement = how many signals point the same way
    directions = [d for d, w in votes if d != 0]
    if directions:
        dominant = sum(directions) / len(directions)
        agreement = abs(dominant)  # 1.0 = all agree, 0.0 = split
    else:
        agreement = 0.0

    # Positioning signal
    if abs(weighted_score) < 0.25:
        ofa.positioning_signal = "neutral"
    elif weighted_score >= 0.25:
        ofa.positioning_signal = "bullish"
    else:
        ofa.positioning_signal = "bearish"

    # Conflicted if signals disagree
    if len(directions) >= 2 and agreement < 0.4:
        ofa.positioning_signal = "conflicted"

    # Confidence: based on data completeness and signal agreement
    data_completeness = min(len(votes) / 4.0, 1.0)  # 4 signals = full data
    ofa.confidence = round(data_completeness * (0.5 + 0.5 * agreement), 2)

    # Build summary
    parts = []
    if ofa.expected_move_pct is not None:
        parts.append(f"market pricing {ofa.expected_move_pct:.1f}% move")
    if ofa.skew_signal and ofa.skew_signal != "balanced":
        parts.append(ofa.skew_signal.replace("_", " "))
    if ofa.flow_bias and ofa.flow_bias not in ("quiet", "mixed"):
        parts.append(ofa.flow_bias.replace("_", " "))
    if ofa.term_structure_shape == "inverted":
        parts.append("inverted term structure (near-term fear)")
    parts.append(f"positioning {ofa.positioning_signal}")

    ofa.summary = "; ".join(parts) + "."
