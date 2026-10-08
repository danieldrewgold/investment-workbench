"""
Pipeline glue for the call layer: assemble the context from the DAG results,
make the call, write the digest and the one-page pitch.

Names whose schema has a scenario config (research/call/schemas/<schema>.json)
get the bottom-up scenario call (pm.py). Others fall back to the earlier
model-proposed scenarios (decide.py) until a config is written for them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

from research.call import valuation_pack, guidance_ledger, macro_lines, reported_lines, scenarios
from research.call.text import EM_DASH

REPORTS = Path("data/reports")


class CallFailed(RuntimeError):
    pass


def _daily_prices(ticker: str) -> list:
    try:
        ph = json.loads(Path("data/price_history.json").read_text(encoding="utf-8"))
        return (ph.get("history", {}).get(ticker.upper()) or {}).get("daily") or []
    except (OSError, json.JSONDecodeError):
        return []


def _write(ticker: str, digest: str, pitch: str) -> tuple[Path, Path]:
    for name, text in (("digest", digest), ("pitch", pitch)):
        if EM_DASH in text:
            raise CallFailed(f"{name} still contains an em dash after cleanup; refusing to publish")
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dp, pp = REPORTS / f"{ticker}_{stamp}_digest.md", REPORTS / f"{ticker}_{stamp}_pitch.md"
    dp.write_text(digest, encoding="utf-8")
    pp.write_text(pitch, encoding="utf-8")
    return dp, pp


def _save_failed(ticker: str, e) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    p = REPORTS / f"{ticker}_{datetime.now().strftime('%Y%m%d_%H%M')}_call_FAILED.json"
    p.write_text(json.dumps({"errors": e.errors, "last_call": e.last_call}, indent=1, default=str), encoding="utf-8")
    return p


def _brief_block(b: dict) -> str:
    keep = ["key_debate", "edge_hypothesis", "why_market_is_wrong", "consensus_assumptions",
            "guidance_vs_our_view", "contradictions", "evidence_gaps"]
    L = ["=== ANALYST BRIEF (inputs; restate nothing from it without checking the data) ==="]
    for k in keep:
        if b.get(k):
            v = b[k]
            label = "where_we_differ_from_consensus" if k == "why_market_is_wrong" else k
            L.append(f"--- {label} ---\n{v if isinstance(v, str) else json.dumps(v, default=str)}")
    for c in b.get("edge_claims") or []:
        L.append(f"claim {c.get('anchor_type')}: anchor {c.get('anchor_value')} -> ours {c.get('our_value')}: "
                 f"{c.get('rationale', '')}")
    if b.get("narrative_synthesis"):
        L.append("--- analyst narrative ---\n" + b["narrative_synthesis"][:14000])
    return "\n".join(L)


def run_call_stage(ticker: str, dag_results: dict, brief, audit: dict | None,
                   our_fy_eps: float | None, our_next_fy_eps: float | None,
                   consensus_full: dict | None, verbose: bool = False,
                   reuse_inputs: dict | None = None) -> dict:
    """Make the call. Raises CallFailed if the price is missing or the call stays invalid.
    reuse_inputs: scenario inputs from an earlier call; skips the inputs step and rewrites
    only the narrative (scenario-config names only)."""
    v = print if verbose else (lambda *a, **k: None)
    T = ticker.upper()
    lp = dag_results.get("live_price") or {}
    if not lp.get("price"):
        raise CallFailed(f"no live price: {lp.get('error', 'missing')}")
    led = dag_results.get("management_ledger") or {}
    cf = consensus_full or {}
    cons_fy = (cf.get("current_year") or {}).get("eps_mean")
    cons_next = (cf.get("next_year") or {}).get("eps_mean")
    pack = valuation_pack.build(T, lp["price"], cons_fy, cons_next,
                                (dag_results.get("quarterly_financials") or {}).get("corpus_text", ""),
                                _daily_prices(T), (dag_results.get("peer_comps") or {}).get("corpus_text", ""))
    brief_dict = asdict(brief) if is_dataclass(brief) else dict(brief or {})
    gl = led.get("guidance") or {}
    ctx = {"ticker": T, "today": date.today().isoformat(), "live_price": lp, "consensus_full": cf,
           "valuation_pack": pack, "valuation_block": valuation_pack.render_block(pack),
           "guidance_block": led.get("guidance_block", ""), "mgmt_block": led.get("mgmt_block", ""),
           "mgmt_ledger": led.get("mgmt") or {}, "brief": brief_dict, "audit": audit or {},
           "guidance_verdict": (gl.get("verdict") or {}).get("lines") or []}

    schema_name = (brief_dict.get("schema_type") or "").strip() or "general"
    try:
        schema = reported_lines.load_schema(schema_name)
    except FileNotFoundError:
        schema = None
    if schema is None:
        v(f"\n-- Call (no scenario config for schema '{schema_name}'; model-proposed scenarios) --")
        return _legacy(T, ctx, our_fy_eps, our_next_fy_eps, pack, v)

    from research.call import pm
    from research.call.report import render_digest, render_pitch
    Q = reported_lines.quarterly_lines(dag_results.get("press_releases") or [], schema)
    if not Q:
        raise CallFailed("could not read reported quarterly lines from the press releases")
    comps = {r["period"]: float(r["value"]) for r in gl.get("reported") or []
             if r.get("metric") == "comps_pct" and r.get("period", "").startswith("Q")}
    last = max(Q, key=lambda q: (int(q.split()[1]), int(q[1])))
    fy = int(last.split()[1]) if last[1] != "4" else int(last.split()[1]) + 1
    k = scenarios.reported_quarters_in(Q, fy)
    actual_comps = [comps.get(f"Q{i} {fy}") for i in range(1, k + 1)]
    guide = next((g for g in gl.get("live_guidance") or [] if g.get("metric") == "comps_pct"
                  and g.get("period") == f"FY{fy}"), None)
    if guide and k and all(c is not None for c in actual_comps):
        rest_comp = (4 * float(guide["adjusted_mid"]) - sum(actual_comps)) / (4 - k)
    else:
        rest_comp = sum(c for c in actual_comps if c is not None) / max(1, len([c for c in actual_comps if c is not None]))
    recent = [q for q in Q if Q[q].get("complete") and q in comps][-4:]
    units = []
    for q in recent:
        prior = Q.get(f"Q{q[1]} {int(q.split()[1]) - 1}")
        if prior and prior.get("complete"):
            units.append((Q[q]["revenue"] / prior["revenue"] - 1) * 100 - comps[q])
    default_bridge = {"h2_comp_pct": round(rest_comp, 2), "h2_unit_pp": round(sum(units) / len(units), 2) if units else 7.0,
                      "delta_persistence": schema["bridge_year"]["delta_persistence"].get("default", 1.0)}
    cost_guides = [g for g in gl.get("live_guidance") or [] if g.get("metric") in ("cost_inflation_pct", "price_increase_pct")]
    macro = macro_lines.fetch(schema)
    hist_q = [q for q in Q if int(q.split()[1]) >= fy - 1]
    ctx.update({"schema": schema, "quarters": Q, "comps": comps, "base_fy": fy, "default_bridge": default_bridge,
                "macro": macro, "macro_block": macro_lines.render_block(macro, cost_guides),
                "history_quarters": hist_q, "history_block": pm.history_block(Q, comps, hist_q),
                "brief_block": _brief_block(brief_dict), "reuse_scenario_inputs": reuse_inputs})
    v(f"\n-- Call --\n  Price {lp['price']:.2f} (close {lp['session_date']}); base year FY{fy} "
      f"({k} quarters reported); proposing scenarios...")
    from research.call.llm import LLMError
    try:
        res = pm.make_call(ctx)
    except pm.CallError as e:
        raise CallFailed("; ".join(e.errors) + f" (failed call saved to {_save_failed(T, e)})")
    except LLMError as e:
        raise CallFailed(f"Claude API call failed during the call stage: {e}")
    R = res["scenario_result"]
    ctx["base_block_final"] = pm.base_block(R["base_year"], cons_fy)
    dp, pp = _write(T, render_digest(T, res, ctx), render_pitch(T, res, ctx))
    v(f"  {R['stance'].upper()} ({R['conviction']}): EV ${R['expected_value']:.2f} ({R['expected_return_pct']:+.1f}%)")
    v(f"  Digest: {dp}\n  Pitch:  {pp}")
    return {**res, "valuation_pack": pack, "digest_path": str(dp), "pitch_path": str(pp),
            "context": {"history_block": ctx["history_block"], "macro_block": ctx["macro_block"],
                        "base_block": ctx["base_block_final"], "guidance_verdict": ctx["guidance_verdict"]}}


def _legacy(T, ctx, our_fy_eps, our_next_fy_eps, pack, v):
    from research.call.decide import make_call, CallError
    from research.call.render import render_digest, render_pitch
    ctx.update({"our_fy_eps": round(our_fy_eps, 2) if our_fy_eps else None,
                "our_next_fy_eps": round(our_next_fy_eps, 2) if our_next_fy_eps else None})
    from research.call.llm import LLMError
    try:
        res = make_call(ctx)
    except CallError as e:
        raise CallFailed("; ".join(e.errors) + f" (failed call saved to {_save_failed(T, e)})")
    except LLMError as e:
        raise CallFailed(f"Claude API call failed during the call stage: {e}")
    dp, pp = _write(T, render_digest(T, res, ctx), render_pitch(T, res, ctx))
    v(f"  Digest: {dp}\n  Pitch:  {pp}")
    return {**res, "valuation_pack": pack, "digest_path": str(dp), "pitch_path": str(pp)}
