"""
Pipeline glue for the call layer: assemble the context from the DAG results,
make the call, write the digest and the one-page pitch.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

from research.call import valuation_pack
from research.call.decide import make_call, CallError

REPORTS = Path("data/reports")


class CallFailed(RuntimeError):
    pass


def _daily_prices(ticker: str) -> list:
    try:
        ph = json.loads(Path("data/price_history.json").read_text(encoding="utf-8"))
        return (ph.get("history", {}).get(ticker.upper()) or {}).get("daily") or []
    except (OSError, json.JSONDecodeError):
        return []


def run_call_stage(ticker: str, dag_results: dict, brief, audit: dict | None,
                   our_fy_eps: float | None, our_next_fy_eps: float | None,
                   consensus_full: dict | None, verbose: bool = False) -> dict:
    """Make the call. Raises CallFailed if the price is missing or the call stays invalid."""
    v = print if verbose else (lambda *a, **k: None)
    lp = dag_results.get("live_price") or {}
    if not lp.get("price"):
        raise CallFailed(f"no live price: {lp.get('error', 'missing')}")
    led = dag_results.get("management_ledger") or {}
    cf = consensus_full or {}
    cons_fy = (cf.get("current_year") or {}).get("eps_mean")
    cons_next = (cf.get("next_year") or {}).get("eps_mean")
    pack = valuation_pack.build(
        ticker, lp["price"], cons_fy, cons_next,
        (dag_results.get("quarterly_financials") or {}).get("corpus_text", ""),
        _daily_prices(ticker),
        (dag_results.get("peer_comps") or {}).get("corpus_text", ""),
    )
    brief_dict = asdict(brief) if is_dataclass(brief) else dict(brief or {})
    ctx = {
        "ticker": ticker.upper(), "today": date.today().isoformat(), "live_price": lp,
        "our_fy_eps": round(our_fy_eps, 2) if our_fy_eps else None,
        "our_next_fy_eps": round(our_next_fy_eps, 2) if our_next_fy_eps else None,
        "consensus_full": cf, "valuation_pack": pack,
        "valuation_block": valuation_pack.render_block(pack),
        "guidance_block": led.get("guidance_block", ""),
        "mgmt_block": led.get("mgmt_block", ""),
        "mgmt_ledger": led.get("mgmt") or {},
        "brief": brief_dict, "audit": audit or {},
    }
    v(f"\n-- Call --\n  Price {lp['price']:.2f} (close {lp['session_date']}); asking for the call...")
    try:
        res = make_call(ctx)
    except CallError as e:
        REPORTS.mkdir(parents=True, exist_ok=True)
        failed = REPORTS / f"{ticker.upper()}_{datetime.now().strftime('%Y%m%d_%H%M')}_call_FAILED.json"
        failed.write_text(json.dumps({"errors": e.errors, "last_call": e.last_call}, indent=1), encoding="utf-8")
        raise CallFailed("; ".join(e.errors) + f" (failed call saved to {failed})")

    from research.call.render import render_digest, render_pitch
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    digest_path = REPORTS / f"{ticker.upper()}_{stamp}_digest.md"
    pitch_path = REPORTS / f"{ticker.upper()}_{stamp}_pitch.md"
    digest_path.write_text(render_digest(ticker.upper(), res, ctx), encoding="utf-8")
    pitch_path.write_text(render_pitch(ticker.upper(), res, ctx), encoding="utf-8")
    d = res["derived"]
    v(f"  {res['call'].get('stance', '').upper()} ({d.get('conviction')}): EV ${d.get('expected_value')} "
      f"({d.get('expected_return_pct'):+.1f}%)")
    v(f"  Digest: {digest_path}\n  Pitch:  {pitch_path}")
    return {**res, "valuation_pack": pack, "digest_path": str(digest_path), "pitch_path": str(pitch_path)}
