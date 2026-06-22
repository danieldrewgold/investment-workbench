"""
Backfill the `ir_press` step — press-release links from each company's IR /
newsroom site (nicer than EDGAR) + product/company news. Writes the DAG cache.
Uses httpx + the Playwright browser worker (no Anthropic API).

Usage:  python rerun_ir_press.py [TICKER ...]   (default: all w/ results)
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from research.dag.steps import _step_ir_press, _weekly_ticker_key
from research.dag.core import _write_to_cache


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        tickers = sorted({os.path.basename(f).split("_")[0]
                          for f in glob.glob("data/results/*.json")})
    def run(t):
        out = _step_ir_press({"ticker": t, "verbose": False})
        n = len(out.get("items") or [])
        print(f"  {t:6s} {n:3d} items · {len(out.get('feeds') or [])} feed(s) · "
              f"{out.get('ir_url','')[:40]}  {out.get('error') or ''}", flush=True)
        _write_to_cache(t, "ir_press", _weekly_ticker_key({"ticker": t}), out)
        return n

    zeros = [t for t in tickers if run(t) == 0]
    # The headless browser degrades under bulk SPA renders, so names flake to 0
    # even when they normally resolve. One retry pass recovers most of them.
    if zeros:
        print(f"\n-- retrying {len(zeros)} zero-result names --")
        try:
            from ingestion.loaders._browser_fetch import close_browser
            close_browser()  # fresh browser for the retry
        except Exception:
            pass
        for t in zeros:
            run(t)
    print("\nDONE")


if __name__ == "__main__":
    main()
