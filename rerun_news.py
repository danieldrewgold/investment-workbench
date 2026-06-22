"""
Surgical re-run of the `news` step (paid feeds + Google News RSS + Hacker News)
for one or more tickers, writing through the DAG cache so the dashboard picks up
the broadened feed. FREE — no LLM, no brief re-run.

Usage:  python rerun_news.py INTC [TICKER ...]
"""
from __future__ import annotations

import glob
import json
import os
import sys

from research.dag.core import _write_to_cache
from research.dag.steps import _step_news, _daily_ticker_key


def _company_name(ticker: str) -> str:
    """Resolved company name from the latest result (drives the keyword search)."""
    files = glob.glob(f"data/results/{ticker}_*.json")
    if files:
        try:
            d = json.load(open(max(files, key=os.path.getmtime), encoding="utf-8"))
            return (d or {}).get("name") or ""
        except Exception:
            pass
    return ""


def run(ticker: str) -> None:
    ticker = ticker.upper()
    name = _company_name(ticker)
    ctx = {"ticker": ticker, "verbose": True, "registry_data": {"name": name}}
    print(f"\n=== {ticker} ({name or 'name unknown -> ticker search'}) ===")
    out = _step_news(ctx)
    if out.get("error"):
        print(f"  error: {out['error']}")
    print(f"  {out.get('n_items', 0)} display item(s)")
    _write_to_cache(ticker, "news", _daily_ticker_key(ctx), out)
    print("  -> wrote news cache")


if __name__ == "__main__":
    for t in (sys.argv[1:] or ["INTC"]):
        try:
            run(t)
        except Exception as e:
            print(f"  {t}: FAILED {type(e).__name__}: {e}")
    print("\n=== done ===")
