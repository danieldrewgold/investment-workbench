"""
Refresh the FREE data steps for every researched ticker (no Claude calls), so the
dashboard's estimates, market data, ownership, insiders, comps, press and quarterly
history are current without paying for full research runs.

Each fresh output is compared with what was there before. If the refresh came back
empty, errored, or smaller (a rate limit, a flaky source), the previous output is
restored, so a refresh can never make the dashboard worse.

    python refresh_data.py              # every ticker with a research result
    python refresh_data.py WDC STX      # just these
    python refresh_data.py --light      # quick panels only (~1 min/ticker vs ~5)

The transcripts step is deliberately excluded: without a transcript subscription
it would replace saved transcripts with empty output.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import time

from research.dag import run_dag
from research.dag.steps import build_research_steps

FREE_STEPS = [
    "financials", "consensus", "market_overlay", "press_releases", "peer_comps",
    "quarterly_financials", "crowding_assessment", "filing_13d", "ownership_holders",
    "filing_form4", "workforce_signal", "bond_health", "bear_research",
]
# Skips the slow SEC crawls (ownership holders, 13D, insider net worth) and Polygon history.
LIGHT_STEPS = [
    "consensus", "market_overlay", "press_releases", "workforce_signal", "bond_health",
]
CACHE = os.path.join("data", "dag_cache")


def _tickers() -> list[str]:
    return sorted({os.path.basename(f).split("_")[0]
                   for f in glob.glob(os.path.join("data", "results", "*.json"))} - {""})


def _company_name(ticker: str) -> str:
    files = glob.glob(os.path.join("data", "results", f"{ticker}_*.json"))
    if files:
        try:
            d = json.load(open(max(files, key=os.path.getmtime), encoding="utf-8"))
            return (d or {}).get("name") or ""
        except Exception:
            pass
    return ""


def _newest(ticker: str) -> dict[str, str]:
    """step -> newest cache file path (the one the dashboard shows)."""
    out: dict[str, tuple[str, float]] = {}
    for fn in glob.glob(os.path.join(CACHE, ticker, "*.json")):
        step = os.path.basename(fn)[:-5].rsplit("_", 1)[0]
        mt = os.path.getmtime(fn)
        if step not in out or mt > out[step][1]:
            out[step] = (fn, mt)
    return {k: v[0] for k, v in out.items()}


def _size(path: str) -> tuple[bool, int]:
    """(usable, content size) for a cached step output."""
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except Exception:
        return False, 0
    out = raw.get("output", raw) if isinstance(raw, dict) else raw
    if out in (None, "", [], {}):
        return False, 0
    if isinstance(out, dict) and out.get("error") and len(out) <= 3:
        return False, 0
    return True, len(json.dumps(out, default=str))


def refresh(ticker: str, registry: dict, step_names: list[str] = FREE_STEPS) -> list[str]:
    before = _newest(ticker)
    backups: dict[str, str] = {}
    for step in step_names:
        if step in before:
            bak = before[step] + ".bak"
            shutil.copy2(before[step], bak)
            backups[step] = bak

    reg = registry.get(ticker) or {"name": _company_name(ticker)}
    steps = [s for s in build_research_steps() if s.name in step_names]
    t0 = time.time()
    try:
        run_dag(steps, ticker=ticker, context={"ticker": ticker, "registry_data": reg, "verbose": False},
                max_parallel=4, read_cache=False, write_cache=True, verbose=False, write_trace=False)
    except Exception as e:
        print(f"  {ticker}: DAG error {type(e).__name__}: {e}")

    after = _newest(ticker)
    notes = []
    for step in step_names:
        new, old = after.get(step), before.get(step)
        if not new:
            continue
        ok_new, n_new = _size(new)
        ok_old, n_old = _size(backups[step]) if step in backups else (False, 0)
        worse = ok_old and (not ok_new or n_new < 0.6 * n_old)
        if worse and old:
            if new != old:
                os.remove(new)                      # older file becomes newest again
                os.utime(old)                       # and is marked current
            else:
                shutil.copy2(backups[step], old)    # same-key file overwritten: restore it
                os.utime(old)
            notes.append(f"kept old {step}")
        elif new == old or new not in before.values():
            notes.append(step if ok_new else f"{step}(empty)")
    for bak in backups.values():
        try:
            os.remove(bak)
        except OSError:
            pass
    print(f"  {ticker:6s} {time.time() - t0:5.0f}s  refreshed: "
          f"{', '.join(n for n in notes if not n.startswith('kept')) or '-'}"
          + (f"  | {', '.join(n for n in notes if n.startswith('kept'))}" if any(n.startswith('kept') for n in notes) else ""),
          flush=True)
    return notes


def main() -> None:
    from research.company_registry import COMPANY_REGISTRY
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    step_names = LIGHT_STEPS if "--light" in sys.argv else FREE_STEPS
    tickers = [t.upper() for t in args] or _tickers()
    print(f"Refreshing {len(tickers)} tickers: {', '.join(step_names)}", flush=True)
    for t in tickers:
        refresh(t, COMPANY_REGISTRY, step_names)
    print("done", flush=True)


if __name__ == "__main__":
    main()
