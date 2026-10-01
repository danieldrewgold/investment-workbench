"""
Refresh data/valuation_snapshot.json — a per-ticker valuation snapshot for the
dashboard overview: a sector-appropriate headline multiple (forward EV/EBITDA
where it's meaningful, EV/Sales for hyper-growth, P/E for financials) plus the
average analyst price target.

A true *forward* EV/EBITDA isn't published by yfinance (only trailing
enterpriseToEbitda) or in consensus, so forward EBITDA is estimated as
  forward_revenue (consensus) x trailing EBITDA margin
which is a reasonable proxy when margins are stable. Everything is labeled so
the dashboard can show what it actually is.

Usage:  python refresh_valuation.py [TICKER ...]   (default: all tickers with results)
Re-run to refresh (like quotes.json). No Anthropic API; yfinance only.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime

OUT = "data/valuation_snapshot.json"
RESULTS = "data/results"


def _latest_result(ticker: str) -> dict:
    files = glob.glob(os.path.join(RESULTS, f"{ticker}_*.json"))
    if not files:
        return {}
    try:
        return json.load(open(max(files, key=os.path.getmtime), encoding="utf-8")) or {}
    except Exception:
        return {}


def _sector_bucket(sector: str, industry: str) -> str:
    s, ind = (sector or "").lower(), (industry or "").lower()
    if "financial" in s or any(k in ind for k in ("bank", "insurance", "capital markets")):
        return "financial"
    if "real estate" in s or "reit" in ind:
        return "reit"
    if any(k in ind for k in ("software", "internet", "semiconductor")) or "technology" in s:
        return "tech"
    return "default"


def _f(info, *keys):
    for k in keys:
        v = info.get(k)
        if isinstance(v, (int, float)) and v == v:  # not NaN
            return float(v)
    return None


def build(ticker: str) -> dict | None:
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    if not info:
        return None

    ev = _f(info, "enterpriseValue")
    ebitda = _f(info, "ebitda")
    revenue = _f(info, "totalRevenue")
    ev_ebitda_ttm = _f(info, "enterpriseToEbitda")
    ev_sales_ttm = _f(info, "enterpriseToRevenue")
    fwd_pe = _f(info, "forwardPE")
    trail_pe = _f(info, "trailingPE")
    price = _f(info, "currentPrice", "regularMarketPrice")
    sector = info.get("sector") or ""
    industry = info.get("industry") or ""

    # Forward revenue from cached consensus (current FY mean), else next FY.
    res = _latest_result(ticker)
    cf = res.get("consensus_full") or {}
    fwd_rev = None
    for key in ("current_year", "next_year"):
        rm = ((cf.get(key) or {}).get("revenue_mean"))
        if isinstance(rm, (int, float)) and rm > 0:
            fwd_rev = float(rm)
            break

    # Forward EV/EBITDA estimate = EV / (fwd_rev x trailing EBITDA margin).
    fwd_ev_ebitda = None
    if ev and ebitda and revenue and fwd_rev and ebitda > 0 and revenue > 0:
        margin = ebitda / revenue
        if margin > 0:
            fwd_ev_ebitda = round(ev / (fwd_rev * margin), 1)
    fwd_ev_sales = round(ev / fwd_rev, 1) if (ev and fwd_rev) else None

    bucket = _sector_bucket(sector, industry)

    # Choose the headline multiple. EBITDA is meaningless when negative or
    # absurdly high (hyper-growth) — fall back to EV/Sales then forward P/E.
    def ebitda_ok(x):
        return isinstance(x, (int, float)) and 0 < x < 80

    headline_val = headline_lbl = None
    if bucket == "financial":
        if fwd_pe and fwd_pe > 0:
            headline_val, headline_lbl = round(fwd_pe, 1), "P/E (fwd)"
        elif trail_pe and trail_pe > 0:
            headline_val, headline_lbl = round(trail_pe, 1), "P/E (TTM)"
    elif bucket == "tech":
        if ebitda_ok(fwd_ev_ebitda):
            headline_val, headline_lbl = fwd_ev_ebitda, "EV/EBITDA (fwd est)"
        elif fwd_ev_sales:
            headline_val, headline_lbl = fwd_ev_sales, "EV/Sales (fwd)"
        elif ebitda_ok(ev_ebitda_ttm):
            headline_val, headline_lbl = round(ev_ebitda_ttm, 1), "EV/EBITDA (TTM)"
    if headline_val is None:  # default + fallbacks
        if ebitda_ok(fwd_ev_ebitda):
            headline_val, headline_lbl = fwd_ev_ebitda, "EV/EBITDA (fwd est)"
        elif ebitda_ok(ev_ebitda_ttm):
            headline_val, headline_lbl = round(ev_ebitda_ttm, 1), "EV/EBITDA (TTM)"
        elif fwd_ev_sales:
            headline_val, headline_lbl = fwd_ev_sales, "EV/Sales (fwd)"
        elif fwd_pe and fwd_pe > 0:
            headline_val, headline_lbl = round(fwd_pe, 1), "P/E (fwd)"
    if bucket == "reit" and headline_lbl:
        headline_lbl += " · no P/FFO"

    # Average analyst price target: prefer cached consensus, else yfinance.
    pt = (cf.get("price_target") or {})
    avg_tgt = pt.get("mean") if isinstance(pt.get("mean"), (int, float)) else _f(info, "targetMeanPrice")
    tgt_high = pt.get("high") if isinstance(pt.get("high"), (int, float)) else _f(info, "targetHighPrice")
    tgt_low = pt.get("low") if isinstance(pt.get("low"), (int, float)) else _f(info, "targetLowPrice")
    n_an = _f(info, "numberOfAnalystOpinions")
    upside = round((avg_tgt / price - 1) * 100, 1) if (avg_tgt and price) else None

    mktcap = _f(info, "marketCap")
    net_debt = round(ev - mktcap, 0) if (ev and mktcap) else None
    # Guard against yfinance's mixed-currency EV for some foreign ADRs: it can
    # report a USD market cap but native-currency debt (e.g. FMX → EV/net-debt
    # come out ~17x too large). If implied net debt is implausible vs market cap,
    # suppress EV/net-debt (market cap from price×shares is still reliable).
    ev_out = ev
    if mktcap and net_debt is not None and abs(net_debt) > 4 * mktcap:
        ev_out, net_debt = None, None

    return {
        "market_cap": mktcap, "enterprise_value": ev_out, "net_debt": net_debt,
        "shares_out": _f(info, "sharesOutstanding", "impliedSharesOutstanding"),
        "headline_multiple": headline_val, "headline_label": headline_lbl,
        "fwd_ev_ebitda": fwd_ev_ebitda, "ev_ebitda_ttm": round(ev_ebitda_ttm, 1) if ev_ebitda_ttm else None,
        "fwd_ev_sales": fwd_ev_sales, "ev_sales_ttm": round(ev_sales_ttm, 1) if ev_sales_ttm else None,
        "fwd_pe": round(fwd_pe, 1) if fwd_pe else None, "trailing_pe": round(trail_pe, 1) if trail_pe else None,
        "avg_price_target": round(avg_tgt, 2) if avg_tgt else None,
        "target_high": round(tgt_high, 2) if tgt_high else None,
        "target_low": round(tgt_low, 2) if tgt_low else None,
        "target_upside_pct": upside, "n_analysts": int(n_an) if n_an else None,
        "recommendation": info.get("recommendationKey") or None,
        "current_price": round(price, 2) if price else None,
        "sector": sector, "industry": industry, "sector_bucket": bucket,
    }


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        tickers = sorted({os.path.basename(f).split("_")[0]
                          for f in glob.glob(os.path.join(RESULTS, "*.json"))})
    snap = {}
    if os.path.exists(OUT):
        try:
            snap = (json.load(open(OUT, encoding="utf-8")) or {}).get("valuations", {})
        except Exception:
            snap = {}
    for t in tickers:
        v = build(t)
        if v:
            snap[t] = v
            print(f"  {t:6s} {v['headline_label'] or '-':22s} {v['headline_multiple']}  "
                  f"tgt ${v['avg_price_target']} ({v['target_upside_pct']}%) n={v['n_analysts']}")
        else:
            print(f"  {t:6s} no data")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"valuations": snap, "fetched_at": datetime.now().isoformat(timespec="seconds")},
              open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"-> wrote {OUT} ({len(snap)} tickers)")


if __name__ == "__main__":
    main()
