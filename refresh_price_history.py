"""
Refresh data/price_history.json — multi-year daily + 5-day intraday closes per
ticker, for the dashboard's interactive (Google-Finance-style) price chart.

Daily (~6y) powers 1M/6M/YTD/1Y/5Y/MAX; intraday (5d @ 5m) powers 1D/5D.
Re-run to refresh (like quotes.json / valuation_snapshot.json). yfinance only.

Usage:  python refresh_price_history.py [TICKER ...]   (default: all w/ results)
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime

OUT = "data/price_history.json"
RESULTS = "data/results"


def _series(t, period, interval, fmt):
    import pandas as pd
    try:
        h = t.history(period=period, interval=interval, auto_adjust=True)
    except Exception:
        return []
    out = []
    for idx, row in h.iterrows():
        c = row.get("Close")
        if c is None or pd.isna(c):
            continue
        out.append([idx.strftime(fmt), round(float(c), 2)])
    return out


def build(ticker: str) -> dict | None:
    import yfinance as yf
    t = yf.Ticker(ticker)
    daily = _series(t, "6y", "1d", "%Y-%m-%d")
    if not daily:
        daily = _series(t, "max", "1d", "%Y-%m-%d")
    intraday = _series(t, "5d", "5m", "%Y-%m-%dT%H:%M")
    if not daily:
        return None
    return {"daily": daily, "intraday": intraday,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        tickers = sorted({os.path.basename(f).split("_")[0]
                          for f in glob.glob(os.path.join(RESULTS, "*.json"))})
    snap = {}
    if os.path.exists(OUT):
        try:
            snap = (json.load(open(OUT, encoding="utf-8")) or {}).get("history", {})
        except Exception:
            snap = {}
    for t in tickers:
        try:
            v = build(t)
        except Exception as e:
            print(f"  {t:6s} ERR {type(e).__name__}: {str(e)[:50]}")
            continue
        if v:
            snap[t] = v
            print(f"  {t:6s} {len(v['daily'])} daily, {len(v['intraday'])} intraday")
        else:
            print(f"  {t:6s} no data")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"history": snap, "fetched_at": datetime.now().isoformat(timespec="seconds")},
              open(OUT, "w", encoding="utf-8"))
    print(f"-> wrote {OUT} ({len(snap)} tickers)")


if __name__ == "__main__":
    main()
