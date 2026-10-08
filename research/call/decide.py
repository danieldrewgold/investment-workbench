"""
The call: stance, scenarios, expected value, catalysts, kill criteria.

Opus proposes; code checks and computes. Every number that can be computed is
computed here (scenario targets, expected value vs price, implied growth behind
each multiple, margin bridges), and the model's own arithmetic is discarded.
Validation failures get one repair round; after that the run fails.

Stance rules (HURDLE = 15% expected return):
  long     expected value >= +HURDLE above price
  short    expected value <= -HURDLE below price
  avoid    expected value below +HURDLE and either negative (<= -5%) or with a
           bear case worse than -25%; must say why it is not a short
  no_edge  |expected return| < HURDLE; must state a specific trigger (a price
           level or a data point) that would create an edge
Pair trades are not offered.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from research.call.bridges import compute_bridge, check_foots, BridgeError
from research.call.llm import call_json, OPUS
from research.call.text import scrub

HURDLE = 0.15
STANCES = ("long", "short", "avoid", "no_edge")
OVERRIDES_DIR = Path("data/overrides")


class CallError(RuntimeError):
    def __init__(self, errors: list[str], last_call: dict | None = None):
        super().__init__("call failed validation: " + "; ".join(errors))
        self.errors = errors
        self.last_call = last_call


SYSTEM = """You are the portfolio manager. An analyst has done the research and a red team has
attacked it. Your job is to make the call: what to do with this stock now, at this price.

How to weigh sources:
- Management is a biased source. Its statements are hypotheses to test against data, not
  evidence. Use the MANAGEMENT LEDGER categories: (a) reported fact and (c) action backed by
  money carry weight; (b) guidance is a forecast, so use the bias-adjusted figure from the
  GUIDANCE TRACK RECORD, not the raw guide; (d) statements against interest are credible
  because they cost management something; (e) self-serving narrative cannot support any
  conclusion unless independent data confirms it. An excuse for a miss stays unverified
  until a later quarter proves it.
- Dodged questions, dropped metrics and tone shifts are signals. Say what they imply.
- Independent data (reported financials, consensus and revisions, macro series, peers,
  ownership, insider trades, short interest, news) outranks anything management says.

How to decide:
- Judge the multiple. Using WHERE THE MULTIPLE SITS (the stock's own P/E history next to the
  EPS growth the market was paying for each year), plus estimate revisions, the margin trend,
  guidance credibility and positioning, decide whether today's multiple is fair, high or low,
  and whether it is more likely to compress, hold or expand over the next year. Peers are loose
  context only: they differ in growth, margins and format.
- Each scenario multiple must be justified in its reasoning by that scenario's growth and the
  multiple-versus-growth history, not by formula or by peers.
- The stance follows from expected value versus price, not from whether estimates are above or
  below consensus.
- Probabilities are your proposal for the reader to override. Show the reasoning for each.
- Stances: long, short, avoid, no_edge. No pair trades. "Not a short" with no alternative is
  not an answer. no_edge must name the specific price level or data point that would create
  an edge.
- Expected value hurdle is 15%: long needs EV at least 15% above price, short at least 15%
  below. avoid means do not own: EV below +15% and either negative or with a bear case worse
  than -25%, and you must say why it is not a short.

Writing rules: plain English, short sentences, no em dashes, no hedging filler. Every evidence
point carries a provenance tag and refs: tags R (reported fact), G (guidance, bias-adjusted),
$ (action backed by money), AI (statement against interest), MC (management claim,
unverified), IND (independent data), EST (our estimate or inference). Refs are ledger ids
(S07, G44, D02, M01) or short source labels ("IND: BLS leisure wages Aug 2026").

Margin bridges: if a margin is a key driver, give the bridge as inputs, not results. Methods:
  price {price_pct}: one per bridge at most
  cost_inflation {cost_inflation_pct, cost_base_pct_of_revenue}: one per cost bucket. When the
      bridge has a price or any cost bucket, the buckets must cover ALL costs (100% minus the
      start margin); list fixed or flat buckets at 0% inflation
  operating_leverage {flow_through_pct, revenue_change_pct, current_margin_pct}
  stated {bps} with a basis, for disclosed one-offs only
Code computes each component and the end margin, and the end margin must equal your estimate
for that margin in key_drivers (same metric and period). State the start margin and exactly
which year and definition it is.

Return JSON only:
{
 "stance": "long|short|avoid|no_edge",
 "conviction": "high|medium|low",
 "thesis": "one sentence",
 "why_not_short": "required for avoid",
 "no_edge_trigger": "required for no_edge: the price level or data point that creates an edge",
 "key_drivers": [{"driver": "", "ours": 0.0, "consensus": 0.0, "unit": "", "period": "",
                  "why": "", "refs": [""]}],
 "price_implies": "2-3 sentences: what today's multiple says the market expects, read against the growth-versus-multiple history",
 "multiple_view": {"current_multiple": 0.0, "basis": "e.g. next-FY P/E", "verdict": "fair|high|low",
                   "direction": "compress|hold|expand", "reasoning": "cite growth vs multiple history, revisions, margins, credibility"},
 "scenarios": {
   "bull": {"eps": 0.0, "multiple": 0.0, "probability": 0.0, "reasoning": ""},
   "base": {"eps": 0.0, "multiple": 0.0, "probability": 0.0, "reasoning": ""},
   "bear": {"eps": 0.0, "multiple": 0.0, "probability": 0.0, "reasoning": ""}},
 "scenario_eps_period": "the fiscal year the scenario EPS refers to (next FY)",
 "bridges": [{"name": "", "metric": "", "period": "", "start_pct": 0.0, "start_basis": "",
              "components": [{"name": "", "method": "", "inputs": {}, "basis": ""}]}],
 "catalysts": [{"date": "YYYY-MM-DD", "event": "", "what_we_expect": "", "if_wrong": ""}],
 "kill_criteria": ["specific, measurable condition that ends the thesis"],
 "evidence": [{"point": "", "tag": "", "refs": [""], "supports": "stance|driver|probability|kill"}],
 "management_read": {"credibility": "", "how_guidance_was_used": "", "signals": [""]},
 "strongest_counter": "the red team's best argument and why the stance survives it",
 "appendix_only": ["sections of the analyst brief that do not change an estimate, a probability or the stance, and why"]
}"""


def _fmt_consensus(cf: dict) -> str:
    if not cf:
        return "CONSENSUS: unavailable"
    L = ["=== CONSENSUS (independent) ==="]
    for k, lab in (("current_quarter", "Current quarter"), ("next_quarter", "Next quarter"),
                   ("current_year", "Current FY"), ("next_year", "Next FY")):
        p = cf.get(k) or {}
        if p.get("eps_mean") is not None:
            rev = p.get("revenue_mean")
            L.append(f"{lab}: EPS ${p['eps_mean']:.2f} (low {p.get('eps_low')}, high {p.get('eps_high')}, "
                     f"n={p.get('eps_num_analysts')})" + (f", revenue ${rev / 1e6:,.0f}M" if rev else "")
                     + f", revisions 30d up {p.get('up_revs_30d', '?')} / down {p.get('down_revs_30d', '?')}")
    pt = cf.get("price_target") or {}
    if pt.get("mean"):
        L.append(f"Analyst price target mean ${pt['mean']:.2f} (low {pt.get('low')}, high {pt.get('high')})")
    ne = cf.get("next_earnings") or {}
    if ne.get("date"):
        L.append(f"Next earnings: {ne['date']}")
    return "\n".join(L)


def _fmt_brief(brief: dict) -> str:
    keep = ["key_debate", "edge_hypothesis", "why_market_is_wrong", "consensus_assumptions",
            "guidance_vs_our_view", "narrative_synthesis", "contradictions", "evidence_gaps"]
    L = ["=== ANALYST BRIEF ==="]
    for k in keep:
        v = brief.get(k)
        if v:
            L.append(f"--- {k} ---\n{v if isinstance(v, str) else json.dumps(v, default=str)}")
    claims = brief.get("edge_claims") or []
    if claims:
        L.append("--- estimate claims vs published anchors ---")
        for c in claims:
            L.append(f"{c.get('anchor_type')}: anchor {c.get('anchor_value')} -> ours {c.get('our_value')}; "
                     f"{c.get('rationale', '')} Falsifier: {c.get('falsifier', '')}")
    return "\n".join(L)


def _fmt_audit(audit: dict | None) -> str:
    if not audit:
        return "=== RED TEAM === none"
    L = ["=== RED TEAM (independent adversarial review) ===",
         f"Verdict: {audit.get('overall_assessment', '')}"]
    for k in ("new_contradictions", "structural_critiques", "blind_spots"):
        if audit.get(k):
            L.append(f"--- {k} ---\n{json.dumps(audit[k], default=str)}")
    return "\n".join(L)


def build_user(ctx: dict) -> str:
    lp = ctx["live_price"]
    return "\n\n".join([
        f"TICKER: {ctx['ticker']}   TODAY: {ctx['today']}   PRICE: ${lp['price']:,.2f} "
        f"(close {lp['session_date']}, {lp['source']})",
        f"OUR ESTIMATES: current FY EPS ${ctx.get('our_fy_eps') or 'n/a'}; next FY EPS "
        f"${ctx.get('our_next_fy_eps') or 'n/a'}",
        ctx.get("valuation_block", ""),
        _fmt_consensus(ctx.get("consensus_full") or {}),
        ctx.get("guidance_block", ""),
        ctx.get("mgmt_block", ""),
        _fmt_brief(ctx.get("brief") or {}),
        _fmt_audit(ctx.get("audit")),
    ])


_ABBREV = re.compile(r"\b(?:vs|e\.g|i\.e|etc|Inc|Corp|Co|No|approx|U\.S|est)\.", re.I)


def _sentence_count(s: str) -> int:
    body = _ABBREV.sub("ABBR", re.sub(r"\d\.\d", "0", (s or "").strip()))
    # a sentence break is terminal punctuation followed by a capital letter or the end
    return len(re.findall(r"[.!?](?=\s+[A-Z]|\s*$)", body)) or (1 if body else 0)


def _one_sentence(s: str) -> bool:
    return 0 < len((s or "").split()) <= 45 and _sentence_count(s) <= 1


def load_overrides(ticker: str) -> dict:
    p = OVERRIDES_DIR / f"{ticker.upper()}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"error": f"could not parse {p}"}
    return {}


def compute(call: dict, price: float, overrides: dict | None = None) -> dict:
    """Scenario targets and expected value; applies overrides."""
    sc = json.loads(json.dumps(call.get("scenarios") or {}))
    applied = []
    for field, key in (("probabilities", "probability"), ("multiples", "multiple"), ("eps", "eps")):
        for name, val in ((overrides or {}).get(field) or {}).items():
            if name in sc:
                sc[name][key] = val
                applied.append(f"{name} {key} = {val}")
    rows, ev, psum = {}, 0.0, 0.0
    for name in ("bull", "base", "bear"):
        s = sc.get(name) or {}
        eps, mult, p = float(s["eps"]), float(s["multiple"]), float(s["probability"])
        target = eps * mult
        rows[name] = {**s, "target": round(target, 2), "return_pct": round((target / price - 1) * 100, 1)}
        ev += p * target
        psum += p
    if abs(psum - 1) > 1e-9 and psum > 0:
        ev = ev / psum
    return {"scenarios": rows, "expected_value": round(ev, 2),
            "expected_return_pct": round((ev / price - 1) * 100, 1),
            "probability_sum": round(psum, 3), "overrides_applied": applied}


def validate(call: dict, ctx: dict) -> tuple[list[str], dict]:
    """Return (errors, derived). Errors mean the call is incomplete or inconsistent."""
    errs: list[str] = []
    price = ctx["live_price"]["price"]
    session = date.fromisoformat(ctx["live_price"]["session_date"])
    stance = (call.get("stance") or "").strip().lower()
    if stance == "pair":
        errs.append("pair trades are not offered; choose long, short, avoid or no_edge")
    elif stance not in STANCES:
        errs.append(f"stance must be one of {STANCES}")
    if (call.get("conviction") or "").lower() not in ("high", "medium", "low"):
        errs.append("conviction must be high, medium or low")
    if not _one_sentence(call.get("thesis", "")):
        t = call.get("thesis", "") or ""
        errs.append(f"thesis must be one sentence of at most 45 words (got {len(t.split())} words, "
                    f"{_sentence_count(t)} sentences): {t[:300]}")

    drivers = call.get("key_drivers") or []
    if not 1 <= len(drivers) <= 2:
        errs.append("give the one or two drivers that matter (key_drivers)")
    for d in drivers:
        try:
            _ = (float(d.get("ours")), float(d.get("consensus")))
        except (TypeError, ValueError):
            errs.append(f"driver '{d.get('driver')}' needs numeric ours and consensus")
    if not (call.get("price_implies") or "").strip():
        errs.append("price_implies is required")
    mv = call.get("multiple_view") or {}
    if (mv.get("verdict") or "").lower() not in ("fair", "high", "low"):
        errs.append("multiple_view.verdict must be fair, high or low")
    if (mv.get("direction") or "").lower() not in ("compress", "hold", "expand"):
        errs.append("multiple_view.direction must be compress, hold or expand")
    if len((mv.get("reasoning") or "").split()) < 15:
        errs.append("multiple_view.reasoning must explain the judgment (growth vs multiple history, revisions, margins)")

    derived: dict = {}
    sc = call.get("scenarios") or {}
    if set(sc) != {"bull", "base", "bear"}:
        errs.append("scenarios must be exactly bull, base, bear")
    else:
        try:
            for n in ("bull", "base", "bear"):
                s = sc[n]
                if float(s["eps"]) <= 0 or not 3 <= float(s["multiple"]) <= 150:
                    errs.append(f"{n}: eps must be positive and multiple between 3x and 150x")
                if not 0 <= float(s["probability"]) <= 1:
                    errs.append(f"{n}: probability must be between 0 and 1")
                if not (s.get("reasoning") or "").strip():
                    errs.append(f"{n}: reasoning required")
            derived = compute(call, price)
            if abs(derived["probability_sum"] - 1) > 0.02:
                errs.append(f"probabilities sum to {derived['probability_sum']}, must sum to 1")
            t = {n: derived["scenarios"][n]["target"] for n in ("bull", "base", "bear")}
            if not t["bull"] >= t["base"] >= t["bear"]:
                errs.append(f"targets must order bull >= base >= bear (got {t})")
        except (KeyError, TypeError, ValueError) as e:
            errs.append(f"scenarios malformed: {e}")

    if derived and stance in STANCES:
        r = derived["expected_return_pct"] / 100
        bear_r = derived["scenarios"]["bear"]["return_pct"] / 100
        if stance == "long" and r < HURDLE:
            errs.append(f"long needs expected return >= +{HURDLE:.0%}; EV is {r:+.1%}")
        if stance == "short" and r > -HURDLE:
            errs.append(f"short needs expected return <= -{HURDLE:.0%}; EV is {r:+.1%}")
        if stance == "avoid":
            if r >= HURDLE:
                errs.append(f"avoid is inconsistent with EV {r:+.1%} (that is a long)")
            elif not (r <= -0.05 or bear_r <= -0.25):
                errs.append(f"avoid needs EV <= -5% or a bear case worse than -25% (EV {r:+.1%}, bear {bear_r:+.1%})")
            if not (call.get("why_not_short") or "").strip():
                errs.append("avoid requires why_not_short")
        if stance == "no_edge":
            if abs(r) >= HURDLE:
                errs.append(f"no_edge is inconsistent with EV {r:+.1%}")
            if not re.search(r"\d", call.get("no_edge_trigger") or ""):
                errs.append("no_edge requires a specific trigger with a number (price level or data point)")
        conv = (call.get("conviction") or "").lower()
        if conv == "high" and abs(r) < 2 * HURDLE:
            derived["conviction_note"] = f"conviction capped at medium: |EV| {abs(r):.1%} < {2 * HURDLE:.0%}"
            derived["conviction"] = "medium"
        else:
            derived["conviction"] = conv

    cats = call.get("catalysts") or []
    if not cats:
        errs.append("at least one dated catalyst is required")
    for c in cats:
        try:
            d = date.fromisoformat(str(c.get("date"))[:10])
            if d < session:
                errs.append(f"catalyst '{c.get('event')}' is dated in the past ({d})")
        except ValueError:
            errs.append(f"catalyst '{c.get('event')}' needs a YYYY-MM-DD date")
    kills = call.get("kill_criteria") or []
    if len(kills) < 2:
        errs.append("at least two kill criteria are required")
    for k in kills:
        if not re.search(r"\d", str(k)):
            errs.append(f"kill criterion must be measurable (contain a number): '{k}'")

    stmts = (ctx.get("mgmt_ledger") or {}).get("statements") or []
    e_ids = {s.get("id") for s in stmts if s.get("category") == "e"}
    hard_ids = {s.get("id") for s in stmts if s.get("category") in ("a", "c")}
    for ev in call.get("evidence") or []:
        refs = [str(x).strip() for x in (ev.get("refs") or []) if str(x).strip()]
        if ev.get("supports") not in ("stance", "driver"):
            continue
        uses_e = ev.get("tag") == "MC" or any(r in e_ids for r in refs)
        independent = any(r.upper().startswith(("IND", "R:", "R ", "$")) or r in hard_ids for r in refs)
        if uses_e and not independent:
            errs.append(f"'{ev.get('point', '')[:80]}' rests on self-serving management narrative "
                        f"(category e) with no independent confirmation; it cannot support the "
                        f"{ev.get('supports')}")
    if not (call.get("evidence") or []):
        errs.append("evidence list is required")

    bridges = []
    for b in call.get("bridges") or []:
        try:
            br = compute_bridge(b)
            problems = check_foots(br)
            if problems:
                errs.extend(f"bridge '{br.name}': {p}" for p in problems)
            bridges.append(br.to_dict())
        except BridgeError as e:
            errs.append(f"bridge '{b.get('name', '?')}': {e}")
    derived["bridges"] = bridges
    # A bridge must land on the margin estimate it explains.
    for kd in drivers:
        if "margin" not in (kd.get("driver") or "").lower():
            continue
        for b in bridges:
            same_period = (b.get("period") or "").strip().lower() == (kd.get("period") or "").strip().lower()
            if same_period and "margin" in (b.get("metric") or b.get("name") or "").lower():
                try:
                    ours = float(kd.get("ours"))
                except (TypeError, ValueError):
                    continue
                if abs(b["end_pct"] - ours) > 0.15:
                    errs.append(f"bridge '{b['name']}' ends at {b['end_pct']:.2f}% but the {kd.get('driver')} "
                                f"estimate is {ours:.2f}%; fix the inputs or the estimate")
    return errs, derived


def make_call(ctx: dict) -> dict:
    """Run the call stage. Raises CallError if the call stays invalid after one repair."""
    user = build_user(ctx)
    call = scrub(call_json(SYSTEM, user, model=OPUS, effort="high", max_tokens=32000))
    errs, derived = validate(call, ctx)
    repaired = False
    if errs:
        fix = (user + "\n\n=== YOUR PREVIOUS ANSWER ===\n" + json.dumps(call)
               + "\n\n=== PROBLEMS TO FIX ===\n- " + "\n- ".join(errs)
               + "\n\nReturn the complete corrected JSON.")
        call = scrub(call_json(SYSTEM, fix, model=OPUS, effort="high", max_tokens=32000))
        errs, derived = validate(call, ctx)
        repaired = True
    if errs:
        raise CallError(errs, call)
    overrides = load_overrides(ctx["ticker"])
    if overrides and not overrides.get("error"):
        derived.update(compute(call, ctx["live_price"]["price"], overrides))
    return {"call": call, "derived": derived, "repaired": repaired, "overrides": overrides,
            "live_price": ctx["live_price"], "hurdle_pct": HURDLE * 100, "model": OPUS}
