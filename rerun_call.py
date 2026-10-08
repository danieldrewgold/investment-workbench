"""
Re-run only the call stage for a ticker, from its cached DAG steps and its latest result.
No brief, no red team, no data fetches beyond the macro series.

    python rerun_call.py CMG                    # fresh price, scenario inputs + narrative (two Opus calls)
    python rerun_call.py CMG --narrative-only   # keep the last scenario inputs and price;
                                                # rewrite the narrative only (one Opus call)
    python rerun_call.py CMG --inputs-from data/reports/failed/CMG_<stamp>_call_FAILED.json
                                                # same, using the inputs and price a failed call saved
    python rerun_call.py CMG --render-only      # re-render the latest digest and pitch after a
                                                # renderer change (no model call, free)

Writes a new data/results/<T>_<stamp>.json (the latest result with the call replaced,
tagged call_rerun_of) plus a new digest and pitch under data/reports/.
Restart the dashboard afterwards to see it.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from research.call.price import fetch_live_price, PriceError
from research.call.stage import run_call_stage, CallFailed

STEPS = ("live_price", "management_ledger", "quarterly_financials", "peer_comps", "press_releases")
BRIEF_KEYS = ("key_debate", "edge_hypothesis", "why_market_is_wrong", "consensus_assumptions",
              "guidance_vs_our_view", "contradictions", "edge_claims", "narrative_synthesis")


def latest_result(t: str) -> Path:
    files = sorted(glob.glob(f"data/results/{t}_*.json"), key=os.path.getmtime)
    if not files:
        sys.exit(f"no result for {t} in data/results; run the pipeline first")
    return Path(files[-1])


def cached_steps(t: str) -> dict:
    """Newest DAG-keyed cache file per step (skips hand-written files like peer_comps_curated)."""
    out = {}
    for step in STEPS:
        files = [f for f in glob.glob(f"data/dag_cache/{t}/{step}_*.json")
                 if re.fullmatch(rf"{step}_[0-9a-f]{{16}}\.json", os.path.basename(f))]
        if files:
            raw = json.loads(Path(max(files, key=os.path.getmtime)).read_text(encoding="utf-8"))
            out[step] = raw.get("output", raw)
    return out


def render_only(t: str, src: Path, result: dict, dag: dict) -> None:
    """Rewrite the digest and pitch files of the latest call from what the result saved."""
    from datetime import date
    from research.call import reported_lines, valuation_pack
    from research.call.report import render_digest, render_pitch
    res = result.get("call") or {}
    if not (res.get("scenario_result") and res.get("digest_path")):
        sys.exit(f"{src.name} has no scenario call to re-render")
    saved, led = res.get("context") or {}, dag.get("management_ledger") or {}
    stamp = re.search(r"_(\d{8})_\d{6}$", src.stem)      # the run's date, not today's
    run_date = f"{stamp[1][:4]}-{stamp[1][4:6]}-{stamp[1][6:]}" if stamp else date.today().isoformat()
    ctx = {"schema": reported_lines.load_schema(result.get("schema") or "general"), "today": run_date,
           "valuation_block": valuation_pack.render_block(res.get("valuation_pack") or {}),
           "guidance_verdict": saved.get("guidance_verdict") or [], "history_block": saved.get("history_block", ""),
           "macro_block": saved.get("macro_block", ""), "base_block_final": saved.get("base_block", ""),
           "guidance_block": led.get("guidance_block", ""), "mgmt_block": led.get("mgmt_block", ""),
           "brief": {"narrative_synthesis": result.get("narrative_synthesis") or ""},
           "audit": result.get("adversarial_response") or {}}
    for path, text in ((res["digest_path"], render_digest(t, res, ctx)), (res["pitch_path"], render_pitch(t, res, ctx))):
        Path(path).write_text(text, encoding="utf-8")
        print(f"  Rewrote {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--narrative-only", action="store_true")
    ap.add_argument("--inputs-from", help="a failed-call file whose scenario inputs and price to reuse")
    ap.add_argument("--render-only", action="store_true")
    a = ap.parse_args()
    t = a.ticker.upper()
    src = latest_result(t)
    result = json.loads(src.read_text(encoding="utf-8"))
    dag = cached_steps(t)
    if a.render_only:
        return render_only(t, src, result, dag)
    reuse = None
    if a.inputs_from:
        saved = json.loads(Path(a.inputs_from).read_text(encoding="utf-8")).get("last_call") or {}
        if not (saved.get("scenario_inputs") and saved.get("live_price")):
            sys.exit(f"{a.inputs_from} has no saved scenario inputs and price")
        reuse, dag["live_price"] = saved["scenario_inputs"], saved["live_price"]
    elif a.narrative_only:
        prev = result.get("call") or {}
        if not prev.get("scenario_inputs"):
            sys.exit(f"{src.name} has no scenario inputs to reuse; run without --narrative-only")
        reuse = prev["scenario_inputs"]
        dag["live_price"] = prev["live_price"]
    else:
        try:
            dag["live_price"] = fetch_live_price(t).to_dict()
        except PriceError as e:
            sys.exit(f"no usable price: {e}")
    brief = {k: result.get(k) for k in BRIEF_KEYS}
    brief["schema_type"] = result.get("schema")
    print(f"{t}: re-running the call from {src.name}" + (" (narrative only)" if reuse else ""))
    try:
        call = run_call_stage(t, dag, brief, result.get("adversarial_response"), result.get("post_eps"),
                              result.get("our_next_fy_eps"), result.get("consensus_full"), verbose=True,
                              reuse_inputs=reuse)
    except CallFailed as e:
        sys.exit(f"CALL FAILED: {e}")
    result.update({"call": call, "call_error": None, "call_rerun_of": src.name})
    out = Path(f"data/results/{t}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"  Result: {out}")


if __name__ == "__main__":
    main()
