"""
Surgical re-run of the `ownership_holders` step (reverse-13F-by-CUSIP +
13D/13G merge → all-holders ownership pies) for one or more tickers, writing
through the DAG cache so the dashboard picks it up. Touches ONLY this step;
never the expensive downstream brief.

Usage:  python rerun_ownership.py PRMB [TICKER ...]

Reads the upstream steps (crowding_assessment, financials, filing_13d) from
the existing dag_cache so it needs no API calls beyond SEC EDGAR.
"""
from __future__ import annotations

import glob
import json
import os
import sys

from research.dag.core import _write_to_cache
from research.dag.steps import _step_ownership_holders, _weekly_ticker_key

CACHE = "data/dag_cache"


def _latest_output(ticker: str, step: str) -> dict:
    files = glob.glob(os.path.join(CACHE, ticker, f"{step}_*.json"))
    if not files:
        return {}
    newest = max(files, key=os.path.getmtime)
    try:
        return (json.load(open(newest, encoding="utf-8")) or {}).get("output") or {}
    except Exception:
        return {}


def run(ticker: str) -> None:
    ticker = ticker.upper()
    ctx = {
        "ticker": ticker,
        "verbose": True,
        "registry_data": {},
        # Thorough backfill: fetch every 13F filer (no practical cap), so the
        # top-holders pie and buckets are complete even on mega-caps.
        "ownership_max_holders": 6000,
        "crowding_assessment": _latest_output(ticker, "crowding_assessment"),
        "financials": _latest_output(ticker, "financials"),
        "filing_13d": _latest_output(ticker, "filing_13d"),
    }
    print(f"\n=== {ticker} ===")
    out = _step_ownership_holders(ctx)
    if out.get("error"):
        print(f"  error: {out['error']}")
    holders = out.get("holders") or []
    print(f"  period={out.get('period_ending')} SO={out.get('shares_outstanding_m')}M "
          f"({out.get('so_source')}) holders={out.get('n_holders')} "
          f"inst={out.get('total_inst_pct')}% float={out.get('float_pct')}%")
    for h in holders[:12]:
        src = "" if h.get("source") == "13F" else f" [{h['source']}]"
        print(f"    {h['name']:<26s} {h['pct']:>5.1f}%  {h['bucket']}{src}")
    print("  buckets:", ", ".join(f"{b['bucket']} {b['pct']}%" for b in (out.get('buckets') or [])))
    _write_to_cache(ticker, "ownership_holders", _weekly_ticker_key(ctx), out)
    print("  -> wrote ownership_holders cache")


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["PRMB"]
    for t in tickers:
        try:
            run(t)
        except Exception as e:
            print(f"  {t}: FAILED {type(e).__name__}: {e}")
    print("\n=== backfill complete ===")
