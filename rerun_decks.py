#!/usr/bin/env python3
"""One-off: refresh the slide_decks DAG cache so every name carries the new
`decks` metadata list (title/type/date/source_url/page_count/analyzed).

Calls _step_slide_decks DIRECTLY — it does NOT run the full DAG, so the
expensive downstream brief / claim_verifications are never touched. Result is
written through the normal DAG cache (`slide_decks_<key>.json`); the dashboard
picks the newest-mtime slide_decks_* file, so this supersedes the stale
`slide_decks_enriched.json` written before the metadata code existed.

Deck vision analysis is content-hash cached in deck_analyzer, so unchanged
decks re-analyze for free. Usage:
    python rerun_decks.py             # full stale set + PRMB/WING
    python rerun_decks.py TMDX COST   # just these
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # IR deck titles carry unicode
except Exception:
    pass

from research.dag.steps import _step_slide_decks, _daily_ticker_key
from research.dag.core import _write_to_cache

# 12 stale (enriched file written today but OLD schema, no `decks`) + the
# two with no enriched file at all. ELF already has the new metadata.
DEFAULT = ["TMDX", "COST", "AXON", "ORLA", "APP", "BAND", "AAOI",
           "RDDT", "GOOG", "LYV", "CMG", "SBH", "PRMB", "WING"]


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or DEFAULT
    print(f"Refreshing slide_decks metadata for {len(tickers)} tickers\n")
    print(f"  {'Ticker':<7} {'decks':>6} {'analyzed':>9}  cache file")
    print(f"  {'-' * 55}")
    summary = []
    for t in tickers:
        ctx = {"ticker": t, "verbose": False}
        try:
            out = _step_slide_decks(ctx)
        except Exception as e:
            print(f"  {t:<7} ERROR {type(e).__name__}: {e}")
            summary.append((t, "ERR", str(e)[:40]))
            continue
        if out.get("error"):
            print(f"  {t:<7} step-error: {out['error'][:50]}")
        decks = out.get("decks") or []
        n_an = out.get("n_analyzed", 0)
        key = _daily_ticker_key(ctx)
        _write_to_cache(t, "slide_decks", key, out)
        print(f"  {t:<7} {len(decks):>6} {n_an:>9}  slide_decks_{key}.json")
        summary.append((t, len(decks), n_an))
    print(f"\nDONE — {len(summary)} processed")
    zeros = [t for t, d, *_ in summary if d == 0]
    if zeros:
        print(f"Still zero (likely no published deck): {', '.join(zeros)}")


if __name__ == "__main__":
    main()
