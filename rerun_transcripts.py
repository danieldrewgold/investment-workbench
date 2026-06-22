"""
Re-run the `transcripts` step so the dashboard's raw-transcript view gets the
FULL per-quarter text (raw_quarters), not just the brief digest. Writes through
the DAG cache. Uses the EarningsCall.biz API (no Anthropic API).

Usage:  python rerun_transcripts.py [TICKER ...]   (default: all tickers with results)
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

from research.dag.steps import _step_transcripts, _ticker_key
from research.dag.core import _write_to_cache


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        tickers = sorted({os.path.basename(f).split("_")[0]
                          for f in glob.glob("data/results/*.json")})
    for t in tickers:
        ctx = {"ticker": t, "verbose": False}
        out = _step_transcripts(ctx)
        nq = out.get("n_raw_quarters", 0)
        chars = sum(q.get("char_count", 0) for q in (out.get("raw_quarters") or []))
        if out.get("error"):
            print(f"  {t:6s} ERR {out['error'][:60]}")
        else:
            print(f"  {t:6s} {nq} full quarters, {chars:,} chars")
        _write_to_cache(t, "transcripts", _ticker_key(ctx), out)
    print("\nDONE")


if __name__ == "__main__":
    main()
