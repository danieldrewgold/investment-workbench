"""
The call, in two model steps around a code core.

Step A (Opus): propose the base-year bridge assumptions and, for bull/base/bear,
every driver value in the schema config, the multiple, the probability and the
reasoning. Code validates ranges and computes EPS, targets, expected value,
the stance (from expected value versus the 15% hurdle), conviction, where
consensus falls in the range, what consensus needs, and what the price implies.

Step B (Opus): write the narrative around the computed numbers. The stance is
fixed by the math; the narrative cannot change any number. Validation enforces
the structure (one-sentence thesis, dated catalysts, measurable kill criteria,
evidence with implications, self-serving claims never carrying the call,
ownership in two lines, no rumors, no em dashes). Each step gets one repair
round; after that the run fails.
"""

from __future__ import annotations

import json
import re
from datetime import date

from research.call import scenarios as S
from research.call.decide import _one_sentence, _fmt_consensus, _fmt_audit
from research.call.llm import call_json, OPUS
from research.call.text import scrub, EM_DASH


class CallError(RuntimeError):
    def __init__(self, errors: list[str], last: dict | None = None):
        super().__init__("call failed validation: " + "; ".join(errors))
        self.errors, self.last_call = errors, last


BANNED = re.compile(r"starbucks|takeover|merger (?:talk|rumou?r)|m&a rumou?r", re.I)

# ---------------------------------------------------------------- context blocks


def history_block(Q: dict, comps: dict, quarters: list[str]) -> str:
    L = ["=== REPORTED QUARTERLY LINES (adjusted basis, % of revenue unless noted) ===",
         "Quarter   Revenue  Comp  Units* Food  Labor  Occ   Other  RLM   G&A$M  Tax   EPS adj  Shares"]
    for q in quarters:
        r = Q.get(q)
        if not r or not r.get("complete"):
            continue
        R = r["revenue"]
        prior = Q.get(f"Q{q[1]} {int(q.split()[1]) - 1}")
        comp = comps.get(q)
        units = ((R / prior["revenue"] - 1) * 100 - comp) if prior and comp is not None else None
        L.append(f"{q:9s} {R:7.0f}  {comp if comp is not None else float('nan'):+5.1f} "
                 f"{units if units is not None else float('nan'):5.1f}  {r['food'] / R * 100:5.1f} {r['labor'] / R * 100:5.1f} "
                 f"{r['occupancy'] / R * 100:5.1f} {r['other_opex'] / R * 100:5.1f}  {r['rlm_pct']:5.1f} "
                 f"{r['g_and_a_adj']:6.1f} {r.get('tax_rate_adj', 0):5.1f}  {r.get('eps_adj', 0):5.2f}  {r.get('diluted_shares_m', 0):6.0f}")
    L.append("*Units = revenue growth minus comp, in pp (revenue from new restaurants).")
    return "\n".join(L)


def base_block(base: dict, cons_fy: float | None) -> str:
    rp = base["ratios_pct"]
    gap = f" vs consensus ${cons_fy:.2f} ({(base['eps'] / cons_fy - 1) * 100:+.1f}%)" if cons_fy else ""
    a = base["assumptions"]
    return (f"=== BASE YEAR FY{base['fy']}E (H1 reported + H2 built from H2 {base['fy'] - 1}) ===\n"
            f"Assumptions: H2 comp {a['h2_comp_pct']}%, H2 new-unit revenue {a['h2_unit_pp']}pp, "
            f"persistence of H1 cost-ratio changes {a.get('delta_persistence', 1.0)}.\n"
            f"H1 year-over-year ratio changes (pp): {base['h1_ratio_change_pp']}; adjusted G&A H1 growth "
            f"{base['h1_gna_growth_pct']}%.\n"
            f"Revenue ${base['revenue']:,.0f}M; food {rp['food']:.2f}%, labor {rp['labor']:.2f}%, occupancy "
            f"{rp['occupancy']:.2f}%, other {rp['other_opex']:.2f}%; restaurant margin {base['rlm_pct']:.2f}%; "
            f"adjusted G&A ${base['g_and_a_adj']:,.0f}M; tax {base['tax_rate']:.1f}%; shares {base['shares']:,.0f}M; "
            f"adjusted EPS ${base['eps']:.3f}{gap}.")


def drivers_block(schema: dict) -> str:
    L = ["=== DRIVERS (config) ===", schema.get("description", "")]
    for k, d in schema["drivers"].items():
        L.append(f"  {k}: {d['label']} [{d['unit']}], allowed {d['min']} to {d['max']}")
    L.append("Cost-line behavior: " + "; ".join(f"{v['label']} {int(v['fixed_share'] * 100)}% fixed per store"
                                               for v in schema["cost_lines"].values()))
    L.append("Base-year bridge: " + "; ".join(f"{k}: {v['label']} [{v['unit']}] {v['min']} to {v['max']}"
                                             for k, v in schema["bridge_year"].items()))
    return "\n".join(L)


def results_block(R: dict, schema: dict) -> str:
    cs, b = R["cases"], R["base_year"]
    L = ["=== COMPUTED SCENARIOS (code; you cannot change these) ===",
         f"{'':28s} {'FY' + str(b['fy']) + 'E':>9s} {'bear':>9s} {'base':>9s} {'bull':>9s}"]
    row = lambda lab, f0, fn: L.append(f"{lab:28s} {f0:>9s} " + " ".join(f"{fn(cs[n]):>9s}" for n in ("bear", "base", "bull")))
    row("Comp", "", lambda c: f"{c['comp_pct']:+.1f}%")
    for k, d in schema["drivers"].items():
        row(d["label"][:28], "", lambda c, k=k, d=d: f"{float(c['drivers'][k]):+.1f}{'pp' if d['unit'] == 'pp' else '%'}")
    for ln, spec in schema["cost_lines"].items():
        row(spec["label"][:28] + " %", f"{b['ratios_pct'][ln]:.2f}", lambda c, ln=ln: f"{c['ratios_pct'][ln]:.2f}")
    row("Restaurant margin %", f"{b['rlm_pct']:.2f}", lambda c: f"{c['rlm_pct']:.2f}")
    row("Adjusted EPS", f"{b['eps']:.3f}", lambda c: f"{c['eps']:.3f}")
    row("Multiple", "", lambda c: f"{c['multiple']:.1f}x")
    row("Target", "", lambda c: f"${c['target']:.2f}")
    row("vs price", "", lambda c: f"{c['return_pct']:+.1f}%")
    row("Probability", "", lambda c: f"{c['probability']:.0%}")
    L.append(f"Probability-weighted value ${R['expected_value']:.2f} ({R['expected_return_pct']:+.1f}% vs price). "
             f"Stance from the math: {R['stance']} ({R['conviction']} conviction).")
    if R.get("consensus"):
        c = R["consensus"]
        L.append(f"Consensus next-FY EPS ${c['eps']:.2f} sits {c['position']}. With our base-case margins it needs "
                 f"traffic of {c['traffic_needed_pct']:+.1f}%; with our base-case revenue it needs a restaurant margin "
                 f"of {c['rlm_needed_pct']:.2f}%.")
    p = R["price_implies"]
    L.append(f"At ${p['price']:.2f} the stock trades at {p['multiple_on_base_eps']:.1f}x our base EPS; at our base "
             f"multiple of {p['base_multiple']:.1f}x the price implies EPS of ${p['eps_at_base_multiple']:.2f}, which "
             f"with base margins needs traffic of {p['traffic_at_base_multiple_pct']:+.1f}%.")
    flags = [n for n in ("bear", "base", "bull") if cs[n]["margin_recovery"]]
    if flags:
        L.append("Cases that assume restaurant margin recovers above the base year (a long-dated management "
                 f"promise): {', '.join(flags)}.")
    return "\n".join(L)


# ---------------------------------------------------------------- step A

SYSTEM_A = """You set the scenario inputs for a stock. Code will compute every number from them.

Build bull, base and bear for next fiscal year by choosing a value for every driver in the
DRIVERS config, plus a P/E multiple on next-year EPS and a probability for each case.
Ground each driver in the data you are given:
- the REPORTED QUARTERLY LINES (cost ratios, comps, new-unit revenue, G&A, tax, share count);
- the MACRO BY COST LINE block, using each series only for the line it measures
  (never PPI all commodities);
- the company's own commodity and cost guides, adjusted for the GUIDANCE TRACK RECORD verdict;
- the MANAGEMENT LEDGER: management is a biased source. Self-serving narrative (category e)
  cannot set a driver unless independent data confirms it. A claim tested only against
  conditions that never held is untested, not refuted.
Flex the drivers the key debate depends on; keep the others close across cases.

Also set the base-year bridge (H2 comp, H2 new-unit revenue, and how much of H1's
year-over-year cost-ratio change persists into H2). Calibrate it against the H2 the
prior year already absorbed, and say why. Show your reasoning against consensus
current-year EPS.

Multiples: justify each by the growth that case delivers and the stock's own
multiple-versus-growth history. Peers are loose context only.

Probabilities are your proposal for the reader to adjust. Reason about each.

Return JSON only:
{"bridge_year": {"h2_comp_pct": 0.0, "h2_unit_pp": 0.0, "delta_persistence": 1.0, "reasoning": ""},
 "cases": {"bull": {"drivers": {"<driver>": 0.0}, "multiple": 0.0, "probability": 0.0,
                    "reasoning": "", "driver_notes": {"<driver>": "why this value"}},
           "base": {...}, "bear": {...}},
 "promise_dependence": [{"case": "", "driver": "", "promise": "", "ledger_ref": ""}]}
Do not cite takeover or merger rumors. Plain punctuation only: no em dashes."""


def validate_a(A: dict, schema: dict) -> list[str]:
    errs = []
    by = A.get("bridge_year") or {}
    for k, spec in schema["bridge_year"].items():
        try:
            v = float(by.get(k, spec.get("default")))
            if not spec["min"] <= v <= spec["max"]:
                errs.append(f"bridge_year.{k}={v} outside {spec['min']} to {spec['max']}")
        except (TypeError, ValueError):
            errs.append(f"bridge_year.{k} must be a number")
    cases = A.get("cases") or {}
    if set(cases) != {"bull", "base", "bear"}:
        return errs + ["cases must be exactly bull, base, bear"]
    psum = 0.0
    for n, c in cases.items():
        errs += [f"{n}: {e}" for e in S.validate_drivers(c.get("drivers") or {}, schema)]
        try:
            if not 5 <= float(c["multiple"]) <= 80:
                errs.append(f"{n}: multiple must be 5x to 80x")
            p = float(c["probability"])
            psum += p
            if not 0 <= p <= 1:
                errs.append(f"{n}: probability must be 0 to 1")
        except (KeyError, TypeError, ValueError):
            errs.append(f"{n}: multiple and probability must be numbers")
        if len((c.get("reasoning") or "").split()) < 12:
            errs.append(f"{n}: reasoning must explain the case")
    if abs(psum - 1) > 0.02:
        errs.append(f"probabilities sum to {psum:.2f}, must sum to 1")
    if BANNED.search(json.dumps(cases)):
        errs.append("remove takeover and merger rumors from the case reasoning")
    return errs


def order_errors(R: dict) -> list[str]:
    cs = R["cases"]
    errs = []
    if not cs["bull"]["eps"] >= cs["base"]["eps"] >= cs["bear"]["eps"]:
        errs.append("computed EPS must order bull >= base >= bear: " +
                    ", ".join(f"{n} {cs[n]['eps']:.3f}" for n in ("bull", "base", "bear")))
    if not cs["bull"]["target"] >= cs["base"]["target"] >= cs["bear"]["target"]:
        errs.append("computed targets must order bull >= base >= bear")
    return errs


# ---------------------------------------------------------------- step B

SYSTEM_B = """You are the portfolio manager writing up a call that the numbers have already made.
The scenarios, expected value, stance and conviction in COMPUTED SCENARIOS are final. Do not
change or contradict them; explain them.

Rules:
- Every sentence must say what a number means for an estimate, a probability or the stance.
  Do not write sentences that only restate numbers shown in the tables.
- Management is a biased source. Self-serving narrative (category e) cannot support the stance
  or a driver without independent data. A claim is refuted only when tested against the
  conditions management attached to it; otherwise call it untested.
- If a case depends on a long-dated management promise (for example a margin recovery next
  year), say so plainly and name the promise.
- Do not mention takeover or merger rumors. Ownership: at most two short lines.
- Put any GAAP versus adjusted reconciliation in "reconciliation" (appendix), not in the body.
- Plain English, short sentences, no em dashes.

Return JSON only:
{"thesis": "one sentence consistent with the computed stance",
 "where_we_differ": [{"key": "comp | restaurant_margin", "why": "", "refs": [""]}],
 "multiple_view": {"current_multiple": 0.0, "basis": "", "verdict": "fair|high|low",
                   "direction": "compress|hold|expand", "reasoning": ""},
 "price_implies_read": "what the implied EPS and multiple say about market expectations",
 "what_would_change_the_stance": "specific price levels or data points, with numbers",
 "why_not_short": "required when the stance is avoid",
 "catalysts": [{"date": "YYYY-MM-DD", "event": "", "what_we_expect": "", "if_wrong": ""}],
 "kill_criteria": ["measurable condition, with a number"],
 "evidence": [{"point": "", "implication": "what it changes", "tag": "R|G|$|AI|MC|IND|EST",
               "refs": [""], "supports": "stance|driver|probability|kill"}],
 "management_read": {"credibility": "", "flow_through": "the 40%-style margin claim stated accurately with its conditions, if relevant", "signals": [""]},
 "strongest_counter": "",
 "ownership": "at most two short lines",
 "reconciliation": "appendix only"}"""


def validate_b(B: dict, R: dict, ctx: dict) -> list[str]:
    errs = []
    stance = R["stance"]
    if not _one_sentence(B.get("thesis", "")):
        errs.append(f"thesis must be one sentence of at most 45 words: {B.get('thesis', '')[:200]}")
    wd = B.get("where_we_differ") or []
    if not 1 <= len(wd) <= 2 or any(x.get("key") not in ("comp", "restaurant_margin") for x in wd):
        errs.append("where_we_differ needs one or two items keyed comp or restaurant_margin")
    mv = B.get("multiple_view") or {}
    if (mv.get("verdict") or "").lower() not in ("fair", "high", "low") or \
            (mv.get("direction") or "").lower() not in ("compress", "hold", "expand"):
        errs.append("multiple_view needs verdict fair/high/low and direction compress/hold/expand")
    if not re.search(r"\d", B.get("what_would_change_the_stance") or ""):
        errs.append("what_would_change_the_stance needs specific numbers")
    if stance == "avoid" and not (B.get("why_not_short") or "").strip():
        errs.append("avoid requires why_not_short")
    session = date.fromisoformat(ctx["live_price"]["session_date"])
    cats = B.get("catalysts") or []
    if not cats:
        errs.append("at least one dated catalyst is required")
    for c in cats:
        try:
            if date.fromisoformat(str(c.get("date"))[:10]) < session:
                errs.append(f"catalyst '{c.get('event')}' is in the past")
        except ValueError:
            errs.append(f"catalyst '{c.get('event')}' needs a YYYY-MM-DD date")
    kills = B.get("kill_criteria") or []
    if len(kills) < 2 or any(not re.search(r"\d", str(k)) for k in kills):
        errs.append("give at least two kill criteria, each with a number")
    stmts = (ctx.get("mgmt_ledger") or {}).get("statements") or []
    e_ids = {s.get("id") for s in stmts if s.get("category") == "e"}
    hard = {s.get("id") for s in stmts if s.get("category") in ("a", "c")}
    for ev in B.get("evidence") or []:
        if len((ev.get("implication") or "").split()) < 5:
            errs.append(f"evidence '{ev.get('point', '')[:60]}' needs an implication, not a restated number")
        refs = [str(x).strip() for x in ev.get("refs") or [] if str(x).strip()]
        if ev.get("supports") in ("stance", "driver"):
            uses_e = ev.get("tag") == "MC" or any(r in e_ids for r in refs)
            indep = any(r.upper().startswith(("IND", "R:", "R ", "$")) or r in hard for r in refs)
            if uses_e and not indep:
                errs.append(f"'{ev.get('point', '')[:60]}' rests on self-serving narrative without independent data")
    if not B.get("evidence"):
        errs.append("evidence is required")
    own = (B.get("ownership") or "").strip()
    if len(own.splitlines()) > 2 or len(own.split()) > 60:
        errs.append("ownership must be at most two short lines")
    body = json.dumps({k: v for k, v in B.items() if k != "reconciliation"})
    if BANNED.search(body):
        errs.append("remove takeover and merger rumors")
    if EM_DASH in json.dumps(B):
        errs.append("no em dashes")
    return errs


# ---------------------------------------------------------------- run


def _repair(system: str, user: str, prev: dict, errs: list[str]) -> dict:
    fix = (user + "\n\n=== YOUR PREVIOUS ANSWER ===\n" + json.dumps(prev)
           + "\n\n=== PROBLEMS TO FIX ===\n- " + "\n- ".join(errs) + "\n\nReturn the complete corrected JSON.")
    return scrub(call_json(system, fix, model=OPUS, effort="high", max_tokens=32000))


def make_call(ctx: dict) -> dict:
    """ctx: ticker, live_price, schema, quarters, comps, base_fy, consensus_full, blocks, mgmt_ledger, brief, audit."""
    schema, Q, price = ctx["schema"], ctx["quarters"], ctx["live_price"]["price"]
    cf = ctx.get("consensus_full") or {}
    cons_fy = (cf.get("current_year") or {}).get("eps_mean")
    cons_next = (cf.get("next_year") or {}).get("eps_mean")
    fy = ctx["base_fy"]
    default_bridge = ctx["default_bridge"]
    base0 = S.build_base_year(Q, fy, default_bridge)
    common = "\n\n".join([
        f"TICKER {ctx['ticker']}  TODAY {ctx['today']}  PRICE ${price:,.2f} (close {ctx['live_price']['session_date']})",
        history_block(Q, ctx["comps"], ctx["history_quarters"]), base_block(base0, cons_fy),
        drivers_block(schema), ctx["macro_block"], _fmt_consensus(cf), ctx["valuation_block"],
        ctx["guidance_block"], ctx["mgmt_block"], ctx["brief_block"], _fmt_audit(ctx.get("audit")),
    ])
    user_a = common + "\n\nThe base year above uses default bridge assumptions; set your own."
    A = scrub(call_json(SYSTEM_A, user_a, model=OPUS, effort="high", max_tokens=32000))
    repaired = []

    def compute(A_):
        by = {k: float((A_.get("bridge_year") or {}).get(k, v.get("default", 0)))
              for k, v in schema["bridge_year"].items()}
        base = S.build_base_year(Q, fy, by)
        cases = {n: {"drivers": {k: float(v) for k, v in c["drivers"].items()},
                     "multiple": float(c["multiple"]), "probability": float(c["probability"]),
                     "reasoning": c.get("reasoning", "")} for n, c in A_["cases"].items()}
        return S.evaluate(base, cases, schema, price, cons_next)

    errs = validate_a(A, schema)
    R = compute(A) if not errs else None
    if R is not None:
        errs += order_errors(R)
    if errs:
        A = _repair(SYSTEM_A, user_a, A, errs)
        repaired.append("scenario inputs")
        errs = validate_a(A, schema)
        R = compute(A) if not errs else None
        if R is not None:
            errs += order_errors(R)
        if errs:
            raise CallError(["scenario inputs: " + e for e in errs], A)
    foot = S.foot_problems(R)
    if foot:
        raise CallError(["scenario math does not foot: " + e for e in foot], A)

    user_b = results_block(R, schema) + "\n\n" + common
    B = scrub(call_json(SYSTEM_B, user_b, model=OPUS, effort="high", max_tokens=24000))
    errs = validate_b(B, R, ctx)
    if errs:
        B = _repair(SYSTEM_B, user_b, B, errs)
        repaired.append("narrative")
        errs = validate_b(B, R, ctx)
        if errs:
            raise CallError(["narrative: " + e for e in errs], B)

    cs = R["cases"]
    base_case = cs["base"]
    differ = []
    for x in B.get("where_we_differ") or []:
        if x["key"] == "comp" and R.get("consensus", {}).get("traffic_needed_pct") is not None:
            cons_comp = R["consensus"]["traffic_needed_pct"] + float(base_case["drivers"]["check_pct"])
            differ.append({"driver": "Comparable sales", "ours": base_case["comp_pct"], "consensus": cons_comp,
                           "unit": "%", "why": x.get("why", ""), "refs": x.get("refs", []),
                           "consensus_basis": "comp that gets our model to consensus EPS at base-case margins"})
        elif x["key"] == "restaurant_margin" and R.get("consensus", {}).get("rlm_needed_pct") is not None:
            differ.append({"driver": "Restaurant margin", "ours": base_case["rlm_pct"],
                           "consensus": R["consensus"]["rlm_needed_pct"], "unit": "%", "why": x.get("why", ""),
                           "refs": x.get("refs", []),
                           "consensus_basis": "margin that gets our model to consensus EPS at base-case revenue"})
    call = {**B, "stance": R["stance"], "conviction": R["conviction"], "where_we_differ": differ,
            "no_edge_trigger": B.get("what_would_change_the_stance", ""), "key_drivers": differ}
    derived = {"scenarios": {n: {"eps": cs[n]["eps"], "multiple": cs[n]["multiple"], "target": cs[n]["target"],
                                 "return_pct": cs[n]["return_pct"], "probability": cs[n]["probability"],
                                 "reasoning": cs[n]["reasoning"]} for n in cs},
               "expected_value": R["expected_value"], "expected_return_pct": R["expected_return_pct"],
               "conviction": R["conviction"], "stance": R["stance"]}
    return {"call": call, "derived": derived, "scenario_result": R, "scenario_inputs": A,
            "repaired": repaired, "live_price": ctx["live_price"], "hurdle_pct": S.HURDLE * 100, "model": OPUS}
