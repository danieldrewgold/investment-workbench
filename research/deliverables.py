"""
Phase 8: Deliverable Templates

Three output generators that match the exact formats from the
hedge-fund-equity-pitch skill:
  1. generate_tear_sheet()  — 30-second scanning format
  2. generate_pitch_doc()   — 550-word max prose document
  3. generate_talking_points() — 3-minute verbal delivery format

Pitch doc includes a required "Why This Could Be Fake Rigor" section.
Tear sheet and talking points include it only when explicitly provided.
Validation gates check source lineage, estimate traceability, and
consensus reconciliation — warnings by default, hard-fail in Investment Full mode.

Usage:
    from research.deliverables import generate_all
    outputs = generate_all(result, ticker="CMG", direction="LONG")
    print(outputs["tear_sheet"])
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Hard Validation Gates
# ═══════════════════════════════════════════════════════════════

class ValidationError(Exception):
    """Raised when a hard validation gate fails. Output is rejected."""
    pass


def validate_source_lineage(result: dict) -> list[str]:
    """
    Gate: Source lineage.
    Every key number must trace to a named source with an as-of date.
    Returns list of violations (empty = pass).
    """
    violations = []

    # Check key financial numbers for source attribution
    val = result.get("valuation") or {}
    drivers = result.get("drivers") or {}
    source_map = result.get("source_lineage") or {}

    # Required sourced fields
    required_sourced = []
    if val.get("current_price"):
        required_sourced.append(("current_price", val["current_price"]))
    if val.get("applied_multiple"):
        required_sourced.append(("applied_multiple", val["applied_multiple"]))
    if result.get("consensus_eps"):
        required_sourced.append(("consensus_eps", result["consensus_eps"]))

    for field, value in required_sourced:
        if field not in source_map:
            # Check if flagged as DATA GAP
            data_gaps = result.get("data_gaps") or []
            if field not in data_gaps:
                violations.append(
                    f"SOURCE LINEAGE: '{field}' = {value} has no source attribution. "
                    f"Expected format: source_lineage['{field}'] = {{'value': ..., 'source': ..., 'as_of': ...}} "
                    f"or flag as data_gap."
                )

    # Check driver sources
    for dname, dinfo in drivers.items():
        if isinstance(dinfo, dict) and dinfo.get("value") is not None:
            if dname not in source_map and dname not in (result.get("data_gaps") or []):
                # Check if driver has inline source
                if not dinfo.get("source") and not dinfo.get("evidence_basis"):
                    violations.append(
                        f"SOURCE LINEAGE: driver '{dname}' has no source attribution or evidence basis."
                    )

    return violations


def validate_estimate_traceability(result: dict) -> list[str]:
    """
    Gate: Estimate traceability.
    Every non-consensus forecast must have: evidence → assumption → forecast → value impact.
    Returns list of violations (empty = pass).
    """
    violations = []
    drivers = result.get("drivers") or {}
    consensus_drivers = result.get("consensus_drivers") or set()

    for dname, dinfo in drivers.items():
        if not isinstance(dinfo, dict):
            continue

        # Skip drivers held at consensus
        if dname in consensus_drivers:
            continue
        if dinfo.get("at_consensus", False):
            continue

        # Non-consensus driver — must have trace chain
        has_evidence = bool(dinfo.get("evidence_basis") or dinfo.get("evidence"))
        has_assumption = bool(dinfo.get("assumption") or dinfo.get("rationale"))
        has_forecast = dinfo.get("value") is not None

        if has_forecast and not has_evidence:
            violations.append(
                f"ESTIMATE TRACEABILITY: driver '{dname}' forecasts {dinfo.get('value')} "
                f"but has no evidence basis. 'We assume X' without 'because Y' fails."
            )
        if has_forecast and not has_assumption:
            violations.append(
                f"ESTIMATE TRACEABILITY: driver '{dname}' forecasts {dinfo.get('value')} "
                f"but has no stated assumption or rationale."
            )

    return violations


def validate_consensus_reconciliation(result: dict) -> list[str]:
    """
    Gate: Consensus reconciliation.
    Every independently modeled driver must show: consensus expects X, we expect Y, delta Z.
    Returns list of violations (empty = pass).
    """
    violations = []
    drivers = result.get("drivers") or {}
    consensus_drivers = result.get("consensus_drivers") or set()

    for dname, dinfo in drivers.items():
        if not isinstance(dinfo, dict):
            continue

        # Skip drivers explicitly held at consensus
        if dname in consensus_drivers:
            continue
        if dinfo.get("at_consensus", False):
            continue

        # Non-consensus driver — must have reconciliation
        has_consensus_value = dinfo.get("consensus_value") is not None
        has_delta = dinfo.get("vs_consensus") is not None or dinfo.get("delta") is not None

        if not has_consensus_value and not has_delta:
            violations.append(
                f"CONSENSUS RECONCILIATION: driver '{dname}' is independently modeled "
                f"but has no consensus comparison. Must show: consensus expects X, "
                f"we expect {dinfo.get('value', '?')}, delta = Z."
            )

    return violations


def run_validation_gates(result: dict, strict: bool = True) -> dict:
    """
    Run all three hard validation gates.

    Args:
        result: research result dict
        strict: if True, raise ValidationError on any failure.
                if False, return violations as warnings (for draft/WIP output).

    Returns:
        {"passed": bool, "violations": list[str], "gate_results": dict}
    """
    lineage = validate_source_lineage(result)
    traceability = validate_estimate_traceability(result)
    reconciliation = validate_consensus_reconciliation(result)

    all_violations = lineage + traceability + reconciliation
    passed = len(all_violations) == 0

    gate_results = {
        "source_lineage": {"passed": len(lineage) == 0, "violations": lineage},
        "estimate_traceability": {"passed": len(traceability) == 0, "violations": traceability},
        "consensus_reconciliation": {"passed": len(reconciliation) == 0, "violations": reconciliation},
    }

    if strict and not passed:
        msg = "HARD VALIDATION GATE FAILURE — output rejected.\n\n"
        for v in all_violations:
            msg += f"  ✗ {v}\n"
        msg += (
            "\nFix: add source_lineage, evidence_basis/assumption on drivers, "
            "and consensus_value on non-consensus drivers. "
            "Or flag missing data as data_gaps / at_consensus=True."
        )
        raise ValidationError(msg)

    return {"passed": passed, "violations": all_violations, "gate_results": gate_results}


# ═══════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════

def _fmt_price(val) -> str:
    if val is None or val == 0:
        return "N/A"
    return f"${val:,.2f}"


def _fmt_pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.1f}%"


def _fmt_prob(val) -> str:
    if val is None:
        return "?%"
    return f"{val:.0f}%"


def _safe_get(d: dict, *keys, default=None):
    """Nested safe dict access."""
    current = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current if current is not None else default


def _build_fake_rigor_section(result: dict, fake_rigor: dict = None) -> dict:
    """
    Build the fake rigor check content.
    Accepts explicit override dict or auto-generates from result.

    Returns dict with keys: weakest_link, held_at_consensus, data_wanted
    """
    if fake_rigor and all(k in fake_rigor for k in ("weakest_link", "held_at_consensus", "data_wanted")):
        return fake_rigor

    fr = fake_rigor or {}

    # Auto-generate from result metadata
    weakest_link = fr.get("weakest_link", "")
    if not weakest_link:
        # Find lowest-confidence driver
        drivers = result.get("drivers") or {}
        lowest_conf = 1.0
        lowest_name = ""
        for dname, dinfo in drivers.items():
            if isinstance(dinfo, dict):
                conf = dinfo.get("confidence", 1.0)
                if conf < lowest_conf:
                    lowest_conf = conf
                    lowest_name = dname
        if lowest_name:
            weakest_link = f"{lowest_name} forecast (confidence: {lowest_conf:.0%})"
        else:
            weakest_link = "⚠ NOT IDENTIFIED — analyst must specify"

    held_at_consensus = fr.get("held_at_consensus", "")
    if not held_at_consensus:
        consensus_drivers = result.get("consensus_drivers") or set()
        if consensus_drivers:
            held_at_consensus = ", ".join(list(consensus_drivers)[:3])
        else:
            held_at_consensus = "⚠ NOT IDENTIFIED — analyst must specify"

    data_wanted = fr.get("data_wanted", "")
    if not data_wanted:
        data_gaps = result.get("data_gaps") or []
        if data_gaps:
            data_wanted = ", ".join(data_gaps[:3])
        else:
            data_wanted = "⚠ NOT IDENTIFIED — analyst must specify"

    return {
        "weakest_link": weakest_link,
        "held_at_consensus": held_at_consensus,
        "data_wanted": data_wanted,
    }


# ═══════════════════════════════════════════════════════════════
# Tear Sheet — 30-Second Scanning Format
# ═══════════════════════════════════════════════════════════════

def generate_tear_sheet(
    result: dict,
    ticker: str = "",
    direction: str = "LONG",
    current_price: float = 0,
    target_price: float = 0,
    scenarios: dict = None,
    pillars: list = None,
    catalyst: str = "",
    catalyst_timeframe: str = "",
    valuation_summary: str = "",
    kill_conditions: list = None,
    bear_argument: str = "",
    confidence: str = "M",
    confidence_note: str = "",
    position_info: dict = None,
    flow_info: dict = None,
    fake_rigor: dict = None,
) -> str:
    """
    Generate tear sheet in exact skill format.
    Includes required FAKE RIGOR CHECK section.
    """
    ticker = ticker or result.get("ticker", "???")
    direction = direction.upper()

    # Price / target
    if not current_price:
        val = _safe_get(result, "valuation") or {}
        current_price = val.get("current_price", 0) or _safe_get(result, "current_price", default=0)
    if not target_price:
        val = _safe_get(result, "valuation") or {}
        target_price = val.get("implied_price", 0)

    upside = ((target_price / current_price - 1) * 100) if current_price and target_price else 0

    # Edge
    edge_type = _safe_get(result, "edge_type", default="")
    edge_hypothesis = _safe_get(result, "edge_hypothesis", default="")
    ea = _safe_get(result, "edge_assessment") or {}

    # Scenarios
    if not scenarios:
        scenarios = _safe_get(result, "scenarios") or {}

    bear = scenarios.get("bear", {})
    base = scenarios.get("base", {})
    bull = scenarios.get("bull", {})

    bear_prob = bear.get("probability", 25)
    base_prob = base.get("probability", 50)
    bull_prob = bull.get("probability", 25)
    bear_tp = bear.get("target_price", 0)
    base_tp = base.get("target_price", target_price)
    bull_tp = bull.get("target_price", 0)
    bear_ret = ((bear_tp / current_price - 1) * 100) if current_price and bear_tp else 0
    base_ret = ((base_tp / current_price - 1) * 100) if current_price and base_tp else 0
    bull_ret = ((bull_tp / current_price - 1) * 100) if current_price and bull_tp else 0

    pw_tp = (bear_prob / 100 * bear_tp + base_prob / 100 * base_tp + bull_prob / 100 * bull_tp)
    pw_ret = ((pw_tp / current_price - 1) * 100) if current_price and pw_tp else 0

    # Pillars
    if not pillars:
        drivers = result.get("drivers", {})
        pillars = []
        for dname, dinfo in drivers.items():
            conf = dinfo.get("confidence", 0.5) if isinstance(dinfo, dict) else 0.5
            val_d = dinfo.get("value", 0) if isinstance(dinfo, dict) else 0
            edge_level = "H" if conf >= 0.6 else "M" if conf >= 0.4 else "L"
            pillars.append({"name": dname, "key_number": val_d, "edge": edge_level})

    # Kill conditions
    if not kill_conditions:
        kill_conditions = _safe_get(result, "kill_conditions", default=[])
        if not kill_conditions:
            kill_conditions = ["TBD"]

    # Valuation
    if not valuation_summary:
        val = _safe_get(result, "valuation") or {}
        mult = val.get("applied_multiple", 0)
        mult_src = val.get("multiple_source", "")
        if mult:
            valuation_summary = f"{mult:.1f}x {mult_src}"

    # Bear argument
    if not bear_argument:
        bear_argument = _safe_get(result, "steel_man", default="TBD")

    # Catalyst
    if not catalyst:
        catalysts = _safe_get(result, "edge_assessment", "catalysts") or []
        if catalysts:
            catalyst = catalysts[0].get("event", "TBD")
            catalyst_timeframe = catalysts[0].get("timeframe", "")

    # Confidence
    if confidence == "M":
        ea_verdict = ea.get("verdict", "")
        if "STRONG" in str(ea_verdict).upper():
            confidence = "H"
        elif "WEAK" in str(ea_verdict).upper() or "NO" in str(ea_verdict).upper():
            confidence = "L"

    # Build the tear sheet
    lines = []
    lines.append(f"{ticker} {direction} {_fmt_price(current_price)} → {_fmt_price(target_price)} ({_fmt_pct(upside)})")
    lines.append("─" * 50)

    # Thesis & Edge
    thesis = _safe_get(result, "business_description", default="")[:80]
    if edge_hypothesis:
        lines.append(f"THESIS: {edge_hypothesis[:100]}")
    elif thesis:
        lines.append(f"THESIS: {thesis}")

    edge_str = f"{edge_type} — {ea.get('edge_narrative', '')[:80]}" if edge_type else "TBD"
    lines.append(f"EDGE: {edge_str}")
    lines.append("")

    # Pillars
    lines.append(f"{'PILLARS:':<40} {'EDGE':>6}")
    for i, p in enumerate(pillars[:3], 1):
        name = p.get("name", "?")
        key_num = p.get("key_number", "")
        edge = p.get("edge", "M")
        lines.append(f"{i}. {name} — {key_num:<30} [{edge}]")
    lines.append("")

    # Scenarios
    lines.append("SCENARIOS:")
    lines.append(f"  Bear {_fmt_prob(bear_prob)}  {_fmt_price(bear_tp)}  {_fmt_pct(bear_ret)} dn  │  "
                 f"Bull {_fmt_prob(bull_prob)}  {_fmt_price(bull_tp)}  {_fmt_pct(bull_ret)} up")
    lines.append(f"  Base {_fmt_prob(base_prob)}  {_fmt_price(base_tp)}  {_fmt_pct(base_ret)} up  │  "
                 f"PW-TP {_fmt_price(pw_tp)}  {_fmt_pct(pw_ret)} up")
    lines.append("")

    # Why Now
    lines.append(f"WHY NOW: {catalyst} — {catalyst_timeframe}")
    lines.append(f"VALUATION: {valuation_summary}")

    kill_str = "  ".join(f"{i+1}) {k}" for i, k in enumerate(kill_conditions[:3]))
    lines.append(f"KILL IF: {kill_str}")
    lines.append(f"BEST BEAR ARGUMENT: {bear_argument[:100]}")
    lines.append(f"CONFIDENCE: [{confidence}] — {confidence_note[:30] if confidence_note else ''}")

    # Fake Rigor Check (optional on tear sheet — included only if explicitly provided)
    if fake_rigor:
        fr = _build_fake_rigor_section(result, fake_rigor)
        lines.append("")
        lines.append("FAKE RIGOR CHECK:")
        lines.append(f"- Weakest link in this analysis: {fr['weakest_link']}")
        lines.append(f"- What I held at consensus but didn't verify: {fr['held_at_consensus']}")
        lines.append(f"- Data I wanted but couldn't get: {fr['data_wanted']}")

    # Optional: Position & Flow
    if position_info:
        pos_str = (f"{position_info.get('pct_of_book', '?')}% of book | "
                   f"Exit at {_fmt_price(position_info.get('exit_price'))} | "
                   f"Add if {position_info.get('add_trigger', '?')}")
        lines.append(f"POSITION: {pos_str}")

    if flow_info:
        flow_str = (f"{flow_info.get('top_holder', '?')} | "
                    f"{flow_info.get('short_interest', '?')}% SI | "
                    f"{flow_info.get('crowding', '?')} | "
                    f"Insider: {flow_info.get('insider', 'none')}")
        lines.append(f"FLOW: {flow_str}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Pitch Document — 550 Words Max
# ═══════════════════════════════════════════════════════════════

def generate_pitch_doc(
    result: dict,
    ticker: str = "",
    direction: str = "LONG",
    current_price: float = 0,
    target_price: float = 0,
    horizon: str = "12-18 months",
    thesis_narrative: str = "",
    why_now: str = "",
    valuation_narrative: str = "",
    risks_narrative: str = "",
    rebuttals: list = None,
    fake_rigor: dict = None,
) -> str:
    """
    Generate pitch document in exact skill format.
    550 words max, all prose. Now 7 sections (added: Why This Could Be Fake Rigor).
    """
    ticker = ticker or result.get("ticker", "???")
    direction = direction.upper()

    val = _safe_get(result, "valuation") or {}
    if not current_price:
        current_price = val.get("current_price", 0)
    if not target_price:
        target_price = val.get("implied_price", 0)
    upside = ((target_price / current_price - 1) * 100) if current_price and target_price else 0

    edge_hypothesis = _safe_get(result, "edge_hypothesis", default="")
    edge_type = _safe_get(result, "edge_type", default="")
    biz = _safe_get(result, "business_description", default="")
    key_debate = _safe_get(result, "key_debate", default="")

    lines = []
    lines.append(f"# {ticker} — {direction} Pitch")
    lines.append(f"*{datetime.now().strftime('%B %d, %Y')}*\n")

    # 1. Recommendation
    lines.append("## Recommendation\n")
    lines.append(
        f"**{direction} {ticker}** at {_fmt_price(current_price)} "
        f"with a {horizon} price target of {_fmt_price(target_price)} "
        f"({_fmt_pct(upside)} upside).\n"
    )

    # 2. Thesis
    lines.append("## Thesis\n")
    if thesis_narrative:
        lines.append(thesis_narrative + "\n")
    else:
        thesis = edge_hypothesis or key_debate or biz
        lines.append(f"{thesis}\n")
        if edge_type:
            lines.append(f"**Edge type:** {edge_type}\n")

    # 3. Why Now
    lines.append("## Why Now\n")
    if why_now:
        lines.append(why_now + "\n")
    else:
        ea = _safe_get(result, "edge_assessment") or {}
        catalysts = ea.get("catalysts", [])
        if catalysts:
            for c in catalysts[:2]:
                lines.append(f"- **{c.get('event', 'TBD')}** ({c.get('timeframe', '')}) — "
                             f"{c.get('impact', '')}\n")
        else:
            lines.append("[Catalyst and timeframe to be identified.]\n")

    # 4. Valuation
    lines.append("## Valuation\n")
    if valuation_narrative:
        lines.append(valuation_narrative + "\n")
    else:
        mult = val.get("applied_multiple", 0)
        mult_src = val.get("multiple_source", "")
        sens = val.get("sensitivity", {})
        if mult:
            lines.append(
                f"At {mult:.1f}x {mult_src}, the stock is worth {_fmt_price(target_price)}. "
                f"Sensitivity: {sens.get('low_pe', 0):.0f}x → {_fmt_price(sens.get('price_at_low', 0))} "
                f"({_fmt_pct(sens.get('upside_at_low', 0))}), "
                f"{sens.get('high_pe', 0):.0f}x → {_fmt_price(sens.get('price_at_high', 0))} "
                f"({_fmt_pct(sens.get('upside_at_high', 0))}).\n"
            )
        if val.get("narrative"):
            lines.append(val["narrative"][:200] + "\n")

    # 5. Risks & Kills
    lines.append("## Risks & Kill Conditions\n")
    if risks_narrative:
        lines.append(risks_narrative + "\n")
    else:
        kill_conds = _safe_get(result, "kill_conditions", default=[])
        if kill_conds:
            for kc in kill_conds:
                lines.append(f"- **Kill:** {kc}")
        steel = _safe_get(result, "steel_man", default="")
        if steel:
            lines.append(f"\n**Bear case:** {steel}\n")

    # 6. Rebuttals
    lines.append("## Rebuttals\n")
    if rebuttals:
        for r in rebuttals[:2]:
            lines.append(f"**Q: {r.get('question', '?')}**")
            lines.append(f"A: {r.get('answer', '...')}\n")
    else:
        contradictions = _safe_get(result, "contradictions", default=[])
        if contradictions:
            for c in contradictions[:2]:
                lines.append(f"**Q: {c.get('thesis', '?')}**")
                lines.append(f"A: {c.get('counter_evidence', '...')}\n")
        else:
            lines.append("[Rebuttals to be developed against PM pushback.]\n")

    # 7. Why This Could Be Fake Rigor (required in pitch doc)
    fr = _build_fake_rigor_section(result, fake_rigor)
    lines.append("## Why This Could Be Fake Rigor\n")
    lines.append(
        f"**Weakest link:** {fr['weakest_link']}. "
        f"**Held at consensus without independent verification:** {fr['held_at_consensus']}. "
        f"**Data wanted but couldn't get:** {fr['data_wanted']}."
    )
    lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Talking Points — 3-Minute Verbal Delivery
# ═══════════════════════════════════════════════════════════════

def generate_talking_points(
    result: dict,
    ticker: str = "",
    direction: str = "LONG",
    current_price: float = 0,
    target_price: float = 0,
    horizon: str = "12-18 months",
    edge_sentence: str = "",
    insight_text: str = "",
    math_text: str = "",
    catalyst_text: str = "",
    pushback_qa: list = None,
    close_text: str = "",
    fake_rigor: dict = None,
) -> str:
    """
    Generate talking points in exact skill format.
    ~3 minutes: opener (15s), insight (60s), math (30s),
    catalyst (30s), pushback (45s), close (15s), + fake rigor check.
    """
    ticker = ticker or result.get("ticker", "???")
    direction = direction.upper()

    val = _safe_get(result, "valuation") or {}
    if not current_price:
        current_price = val.get("current_price", 0)
    if not target_price:
        target_price = val.get("implied_price", 0)
    upside = ((target_price / current_price - 1) * 100) if current_price and target_price else 0

    edge_hypothesis = edge_sentence or _safe_get(result, "edge_hypothesis", default="")
    ea = _safe_get(result, "edge_assessment") or {}

    lines = []
    lines.append(f"# {ticker} Talking Points\n")

    # OPENER (15 sec)
    lines.append("**OPENER (15 sec):**")
    lines.append(
        f'"I want to {direction.lower()} {ticker} at {_fmt_price(current_price)} '
        f'for a {_fmt_pct(upside)} return over {horizon}.'
    )
    lines.append(f'{edge_hypothesis[:120]}"\n')

    # THE INSIGHT (60 sec)
    lines.append("**THE INSIGHT (60 sec):**")
    if insight_text:
        lines.append(insight_text + "\n")
    else:
        drivers = result.get("drivers", {})
        for dname, dinfo in list(drivers.items())[:2]:
            if isinstance(dinfo, dict):
                val_d = dinfo.get("value", 0)
                basis = ""
                for cname, cdata in dinfo.get("components", {}).items():
                    basis += f"{cname}={cdata.get('value', '?')} "
                lines.append(f"- **{dname}**: {val_d} ({basis.strip()})")
        lines.append("")

    # THE MATH (30 sec)
    lines.append("**THE MATH (30 sec):**")
    if math_text:
        lines.append(math_text + "\n")
    else:
        post_eps = result.get("post_eps", 0)
        cons_eps = result.get("consensus_eps", 0)
        lines.append(
            f"Our EPS: ${post_eps:.2f} vs consensus ${cons_eps:.2f} "
            f"({_fmt_pct((post_eps / cons_eps - 1) * 100 if cons_eps else 0)}). "
            f"At {val.get('applied_multiple', 0):.0f}x forward PE → {_fmt_price(target_price)}.\n"
        )

    # THE CATALYST (30 sec)
    lines.append("**THE CATALYST (30 sec):**")
    if catalyst_text:
        lines.append(catalyst_text + "\n")
    else:
        catalysts = ea.get("catalysts", [])
        if catalysts:
            for c in catalysts[:2]:
                lines.append(f"- {c.get('event', 'TBD')} ({c.get('timeframe', '')})")
        else:
            lines.append("[Catalyst to be identified.]")
        lines.append("")

    # PUSHBACK (45 sec)
    lines.append("**PUSHBACK (45 sec):**")
    if pushback_qa:
        for qa in pushback_qa[:2]:
            lines.append(f'Q: "{qa.get("question", "?")}"')
            lines.append(f'→ A: {qa.get("answer", "...")}\n')
    else:
        contradictions = _safe_get(result, "contradictions", default=[])
        if contradictions:
            for c in contradictions[:2]:
                lines.append(f'Q: "{c.get("thesis", "?")}"')
                lines.append(f'→ A: {c.get("counter_evidence", "...")}\n')
        else:
            lines.append('[PM pushback Q&A to be prepared.]\n')

    # THE CLOSE (15 sec)
    lines.append("**THE CLOSE (15 sec):**")
    if close_text:
        lines.append(f'"{close_text}"')
    else:
        kill_conds = _safe_get(result, "kill_conditions", default=["TBD"])
        first_catalyst = ea.get("catalysts", [{}])[0] if ea.get("catalysts") else {}
        lines.append(
            f'"Hard stop at {_fmt_price(target_price * 0.85 if target_price else 0)} '
            f'if {kill_conds[0] if kill_conds else "TBD"}. '
            f'First signpost is {first_catalyst.get("timeframe", "next earnings")}."'
        )

    # FAKE RIGOR CHECK (optional on talking points — included only if explicitly provided)
    if fake_rigor:
        fr = _build_fake_rigor_section(result, fake_rigor)
        lines.append("")
        lines.append("**FAKE RIGOR CHECK (internal, disclose if asked):**")
        lines.append(
            f'"Weakest link: {fr["weakest_link"]}. '
            f'Held at consensus without checking: {fr["held_at_consensus"]}. '
            f'Wanted but couldn\'t get: {fr["data_wanted"]}."'
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Generate All Deliverables
# ═══════════════════════════════════════════════════════════════

def generate_all(
    result: dict,
    ticker: str = "",
    direction: str = "LONG",
    save_files: bool = True,
    strict_validation: bool = False,
    **kwargs,
) -> dict:
    """
    Generate all three deliverables and optionally save to workspace.
    Runs hard validation gates BEFORE generating output.

    Args:
        result: research result dict
        ticker: override ticker
        direction: LONG or SHORT
        save_files: write markdown files to workspace
        strict_validation: if True, raises ValidationError on gate failure (use for Investment Full).
                          Defaults to False — gates run as warnings.
        **kwargs: passed through to individual generators

    Returns:
        {
            "tear_sheet": str,
            "pitch_doc": str,
            "talking_points": str,
            "validation": {"passed": bool, "violations": list},
            "files": {"tear_sheet": path, ...}
        }
    """
    ticker = ticker or result.get("ticker", "???")

    # Run hard validation gates FIRST
    validation = run_validation_gates(result, strict=strict_validation)

    tear_sheet = generate_tear_sheet(result, ticker=ticker, direction=direction, **kwargs)
    pitch_doc = generate_pitch_doc(result, ticker=ticker, direction=direction, **kwargs)
    talking_points = generate_talking_points(result, ticker=ticker, direction=direction, **kwargs)

    outputs = {
        "tear_sheet": tear_sheet,
        "pitch_doc": pitch_doc,
        "talking_points": talking_points,
        "validation": validation,
        "files": {},
    }

    if save_files:
        out_dir = Path(f"/workspace/investment-workbench/data/results/{ticker}")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        for name, content in [
            ("tear_sheet", tear_sheet),
            ("pitch_doc", pitch_doc),
            ("talking_points", talking_points),
        ]:
            path = out_dir / f"{name}_{ts}.md"
            path.write_text(content)
            outputs["files"][name] = str(path)
            print(f"  Saved: {path}")

    return outputs


# ═══════════════════════════════════════════════════════════════
# Summary Tab Text (for Shortcut to write into workbook)
# ═══════════════════════════════════════════════════════════════

def summary_for_workbook(result: dict, ticker: str = "") -> dict:
    """
    Extract key text fields for Shortcut to write into the Summary tab.
    Returns a dict keyed by section name with text values.
    """
    ticker = ticker or result.get("ticker", "???")
    ea = _safe_get(result, "edge_assessment") or {}
    val = _safe_get(result, "valuation") or {}

    return {
        "ticker": ticker,
        "edge_hypothesis": _safe_get(result, "edge_hypothesis", default=""),
        "edge_type": _safe_get(result, "edge_type", default=""),
        "business_description": _safe_get(result, "business_description", default=""),
        "key_debate": _safe_get(result, "key_debate", default=""),
        "post_eps": result.get("post_eps", 0),
        "consensus_eps": result.get("consensus_eps", 0),
        "implied_price": val.get("implied_price", 0),
        "current_price": val.get("current_price", 0),
        "upside_pct": val.get("upside_pct", 0),
        "decision_verdict": result.get("decision_verdict", ""),
        "quality_line": result.get("quality_line", ""),
        "edge_verdict": ea.get("verdict", ""),
        "actionability_score": ea.get("actionability_score", 0),
    }


if __name__ == "__main__":
    # Test with a minimal result dict — strict=False to show violations without failing
    test_result = {
        "ticker": "TEST",
        "edge_hypothesis": "Consensus underestimates margin expansion from throughput improvements.",
        "edge_type": "EXPECTATION_GAP",
        "business_description": "Fast-casual restaurant chain.",
        "key_debate": "Whether SSS can sustain above 5% with unit growth.",
        "post_eps": 1.25,
        "consensus_eps": 1.15,
        "drivers": {
            "sss_growth": {
                "value": 4.5, "confidence": 0.55,
                "evidence_basis": "Last 4Q avg SSS 4.8%, mgmt guided 4-5%",
                "assumption": "Traffic recovery + menu price sticking = 4.5% SSS",
                "consensus_value": 3.8, "vs_consensus": "+70bps",
                "source": "yfinance + company 10-Q",
                "components": {"traffic": {"value": 2.0}, "ticket": {"value": 2.5}},
            },
        },
        "consensus_drivers": {"cogs_pct", "sga_pct"},
        "data_gaps": ["channel-level sales mix"],
        "source_lineage": {
            "current_price": {"value": 55.0, "source": "yfinance", "as_of": "2026-03-31"},
            "consensus_eps": {"value": 1.15, "source": "yfinance", "as_of": "2026-03-31"},
            "applied_multiple": {"value": 52.0, "source": "peer median PE", "as_of": "2026-03-31"},
        },
        "valuation": {
            "current_price": 55.0,
            "implied_price": 65.0,
            "applied_multiple": 52.0,
            "multiple_source": "forward PE",
            "upside_pct": 18.2,
            "sensitivity": {"low_pe": 45, "high_pe": 58,
                           "price_at_low": 56.25, "price_at_high": 72.50,
                           "upside_at_low": 2.3, "upside_at_high": 31.8},
        },
        "edge_assessment": {
            "verdict": "ACTIONABLE EDGE",
            "actionability_score": 0.62,
            "edge_narrative": "Throughput-driven margin expansion not reflected in consensus.",
            "catalysts": [{"event": "Q2 earnings", "timeframe": "July 2026", "impact": "SSS proof point"}],
        },
    }

    # Strict validation — should pass with properly structured data
    print("=== VALIDATION TEST (strict) ===")
    try:
        validation = run_validation_gates(test_result, strict=True)
        print(f"PASSED: {validation['passed']}")
    except ValidationError as e:
        print(f"FAILED:\n{e}")

    print()
    outputs = generate_all(test_result, direction="LONG", save_files=False)

    print("=== TEAR SHEET ===")
    print(outputs["tear_sheet"])
    print("\n=== PITCH DOC ===")
    print(outputs["pitch_doc"])
    print("\n=== TALKING POINTS ===")
    print(outputs["talking_points"])
    print(f"\n=== VALIDATION ===")
    print(f"Passed: {outputs['validation']['passed']}")
    print(f"Violations: {outputs['validation']['violations']}")

    # Now test FAILURE case — missing source lineage
    print("\n\n=== FAILURE TEST (missing source lineage) ===")
    bad_result = {
        "ticker": "BAD",
        "drivers": {"revenue_growth": {"value": 8.0}},  # no source, no evidence
        "valuation": {"current_price": 100, "applied_multiple": 20},
        "consensus_eps": 5.0,
    }
    try:
        run_validation_gates(bad_result, strict=True)
    except ValidationError as e:
        print(f"CORRECTLY FAILED:\n{e}")
