#!/usr/bin/env python3
"""Re-fetch quarterly financials at 16 quarters (was 12) so the estimates
matrix can reach FY2022 / Q1-Q3 2022. Writes through the DAG cache."""
import glob
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from research.quarterly_financials_loader import fetch_quarterly_financials
from research.dag.steps import _daily_ticker_key
from research.dag.core import _write_to_cache


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or sorted(
        os.path.basename(p) for p in glob.glob("data/dag_cache/*")
        if os.path.isdir(p) and not os.path.basename(p).startswith("_"))
    print(f"Re-fetching quarterly financials (16q) for {len(tickers)} tickers\n")
    for t in tickers:
        try:
            b = fetch_quarterly_financials(t, n_quarters=16, force_refresh=True)
        except Exception as e:
            print(f"  {t:6} ERR {type(e).__name__}: {str(e)[:50]}")
            continue
        if not b or not b.reports:
            print(f"  {t:6} 0 quarters")
            continue
        out = {"corpus_text": b.to_prompt_text(max_quarters=16),
               "n_quarters": len(b.reports), "fetched_at": b.fetched_at}
        _write_to_cache(t, "quarterly_financials", _daily_ticker_key({"ticker": t}), out)
        print(f"  {t:6} {len(b.reports)} quarters", flush=True)
        time.sleep(20)  # Polygon free tier = 5 req/min
    print("\nDONE")


if __name__ == "__main__":
    main()
