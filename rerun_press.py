#!/usr/bin/env python3
"""One-off: populate the press_releases DAG cache (8-K Ex 99.1 earnings /
material releases) for every cached ticker, so the dashboard Press tab can
show them under "Company releases" with their SEC hyperlinks.

Calls fetch_press_releases directly (per-release content cache makes repeats
cheap) and writes through the DAG cache via _write_to_cache. Independent of
the rest of the DAG — no brief re-run. Usage:
    python rerun_press.py             # every ticker under data/dag_cache
    python rerun_press.py COST TMDX   # just these
"""
import glob
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ingestion.loaders.press_release_loader import fetch_press_releases
from research.dag.steps import _ticker_key
from research.dag.core import _write_to_cache
import dashboard as D  # for _pr_headline preview


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        tickers = sorted(
            os.path.basename(p) for p in glob.glob("data/dag_cache/*")
            if os.path.isdir(p) and not os.path.basename(p).startswith("_")
        )
    print(f"Refreshing press_releases for {len(tickers)} tickers\n")
    print(f"  {'Ticker':<7} {'rels':>4}  most-recent headline")
    print(f"  {'-' * 72}")
    zeros = []
    for t in tickers:
        try:
            rels = fetch_press_releases(t, quarters=12, verbose=False)
        except Exception as e:
            print(f"  {t:<7} ERR  {type(e).__name__}: {str(e)[:50]}")
            zeros.append(t)
            continue
        out = [asdict(r) for r in rels]
        _write_to_cache(t, "press_releases", _ticker_key({"ticker": t}), out)
        top = D._pr_headline(rels[0].text or "") if rels else ""
        print(f"  {t:<7} {len(out):>4}  {top[:64]}")
        if not out:
            zeros.append(t)
    print(f"\nDONE — {len(tickers)} processed")
    if zeros:
        print(f"No releases (may not file 8-K Ex 99.1 prose): {', '.join(zeros)}")


if __name__ == "__main__":
    main()
