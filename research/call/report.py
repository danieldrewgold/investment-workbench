"""
Render the call: a full digest (provenance tags, appendix for everything that doesn't
change an estimate, a probability or the stance) and a one-page pitch (no tags).
Both are scrubbed of em dashes; the stage refuses to publish if any remain.
"""

from __future__ import annotations

import re

from research.call.text import no_em_dash, strip_tags, TAGS

STANCE = {"long": "LONG", "short": "SHORT", "avoid": "AVOID", "no_edge": "NO EDGE"}
_RUMOR = re.compile(r"starbucks|takeover|merger (?:talk|rumou?r)|m&a rumou?r", re.I)

MOVED = [
    ("GAAP to adjusted reconciliation", "method detail; the scenarios are on the adjusted basis consensus uses"),
    ("Per-case EPS bridges and the base-year build", "they prove the scenario table foots; the table carries the result"),
    ("Reported quarterly lines and macro by cost line", "inputs to the drivers, shown once"),
    ("Full guidance history and management ledger", "the verdict and the signals are in the body"),
    ("Claim verifier results, mechanical driver model, back-solve, edge score, decision gate", "diagnostics"),
    ("Long-form analyst essay", "working notes behind the estimates"),
    ("Ownership flows", "two lines in the body; nothing in them moves an estimate"),
    ("M&A rumors", "removed: unsubstantiated, no value assigned"),
]


def _n(x, d=2):
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return str(x)


def _refs(r):
    r = [x for x in (r or []) if x]
    return f" ({', '.join(r)})" if r else ""


def _sent(s):
    s = (s or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


def _strip_rumors(text: str) -> str:
    keep = []
    for para in (text or "").split("\n"):
        if _RUMOR.search(para):
            continue
        keep.append(para)
    return "\n".join(keep)


def scenario_table(R: dict, schema: dict) -> list[str]:
    cs, b = R["cases"], R["base_year"]
    order = ("bear", "base", "bull")
    L = [f"| | FY{b['fy']}E | Bear | Base | Bull |", "|---|---|---|---|---|"]
    row = lambda lab, f0, fn: L.append(f"| {lab} | {f0} | " + " | ".join(fn(cs[n]) for n in order) + " |")
    row("Comp", "", lambda c: f"{c['comp_pct']:+.1f}%")
    for k, d in schema["drivers"].items():
        unit = "pp" if d["unit"] == "pp" else "%"
        row(d["label"], "", lambda c, k=k, unit=unit: f"{float(c['drivers'][k]):+.1f}{unit}"
            if k != "tax_rate_pct" else f"{float(c['drivers'][k]):.1f}%")
    for ln, spec in schema["cost_lines"].items():
        row(f"{spec['label']}, % of revenue", f"{b['ratios_pct'][ln]:.2f}%", lambda c, ln=ln: f"{c['ratios_pct'][ln]:.2f}%")
    row("**Restaurant margin**", f"**{b['rlm_pct']:.2f}%**", lambda c: f"**{c['rlm_pct']:.2f}%**")
    row("**Adjusted EPS**", f"**${b['eps']:.2f}**", lambda c: f"**${c['eps']:.2f}**")
    row("P/E multiple", "", lambda c: f"{c['multiple']:.1f}x")
    row("**Target**", "", lambda c: f"**${c['target']:.2f}**")
    row("vs price", "", lambda c: f"{c['return_pct']:+.1f}%")
    row("Probability (proposed)", "", lambda c: f"{c['probability']:.0%}")
    row("Relies on a long-dated promise", "", lambda c: "yes: margin recovery" if c["margin_recovery"] else "no")
    return L


def render_digest(ticker: str, res: dict, ctx: dict) -> str:
    c, R, lp, schema = res["call"], res["scenario_result"], res["live_price"], ctx["schema"]
    cs = R["cases"]
    out = []
    A = out.append
    A(f"# {ticker}: {STANCE[R['stance']]}, {R['conviction']} conviction")
    A(f"Price ${lp['price']:,.2f} (close {lp['session_date']}, {lp['source']}). Probability-weighted value "
      f"${R['expected_value']:.2f}, {R['expected_return_pct']:+.1f}% vs price. The stance follows from that "
      f"against a {res['hurdle_pct']:.0f}% hurdle. Run {ctx['today']}.")
    A("")
    A("Provenance tags: " + "; ".join(f"[{k}] {v}" for k, v in TAGS.items()) + ".")
    A("")
    A("## Thesis")
    A(c.get("thesis", ""))
    A("")
    A("## Where we differ from consensus")
    A("| Driver | Ours (base) | What consensus needs | Why |")
    A("|---|---|---|---|")
    for d in c.get("where_we_differ") or []:
        A(f"| {d['driver']} | {d['ours']:.2f}% | {d['consensus']:.2f}% | {d['why']}{_refs(d.get('refs'))} |")
    for d in c.get("where_we_differ") or []:
        A(f"\n_Consensus column for {d['driver'].lower()}: the {d['consensus_basis']}._")
    A("")
    A(f"## Scenarios: FY{R['base_year']['fy'] + 1} EPS built from the cost lines")
    out.extend(scenario_table(R, schema))
    A("")
    if R.get("consensus"):
        k = R["consensus"]
        A(f"- **Consensus ${k['eps']:.2f}** sits {k['position']}. Getting there needs traffic of "
          f"{k['traffic_needed_pct']:+.1f}% at base-case margins, or a {k['rlm_needed_pct']:.2f}% restaurant margin "
          f"at base-case revenue.")
    p = R["price_implies"]
    A(f"- **The price implies** EPS of ${p['eps_at_base_multiple']:.2f} at our base multiple of "
      f"{p['base_multiple']:.1f}x (traffic of {p['traffic_at_base_multiple_pct']:+.1f}% at base margins), and "
      f"{p['multiple_on_base_eps']:.1f}x our base EPS. {c.get('price_implies_read', '')}")
    A(f"- **Probability-weighted value ${R['expected_value']:.2f}**, {R['expected_return_pct']:+.1f}% vs "
      f"${lp['price']:,.2f}: **{STANCE[R['stance']]}**.")
    if R["stance"] == "no_edge" or c.get("what_would_change_the_stance"):
        A(f"- **What would change the stance:** {c.get('what_would_change_the_stance', '')}")
    if R["stance"] == "avoid":
        A(f"- **Why not short:** {c.get('why_not_short', '')}")
    A("")
    A("Case reasoning (probabilities are proposals; override in data/overrides/" + ticker + ".json):")
    for n in ("bull", "base", "bear"):
        A(f"- **{n.title()}:** {cs[n]['reasoning']}")
    A("")
    mv = c.get("multiple_view") or {}
    A("## Is the multiple fair?")
    A("```")
    A(ctx.get("valuation_block", "").strip())
    A("```")
    A(f"**{_n(mv.get('current_multiple'), 1)}x {mv.get('basis', '')} looks {mv.get('verdict')}, more likely to "
      f"{mv.get('direction')}.** {mv.get('reasoning', '')}")
    A("")
    mr = c.get("management_read") or {}
    A("## Management read")
    for line in (ctx.get("guidance_verdict") or []):
        A(f"- {line}")
    if mr.get("flow_through"):
        A(f"- **Flow-through:** {mr['flow_through']}")
    if mr.get("credibility"):
        A(f"- **Credibility:** {mr['credibility']}")
    for s in mr.get("signals") or []:
        A(f"- {s}")
    A("")
    A("## Catalysts")
    for k in sorted(c.get("catalysts") or [], key=lambda x: str(x.get("date"))):
        A(f"- **{k.get('date')}** {k.get('event')}. We expect: {_sent(k.get('what_we_expect'))} "
          f"If wrong: {_sent(k.get('if_wrong'))}")
    A("")
    A("## Kill criteria")
    for k in c.get("kill_criteria") or []:
        A(f"- {k}")
    A("")
    A("## Strongest counter")
    A(c.get("strongest_counter", ""))
    A("")
    A("## Evidence")
    for e in c.get("evidence") or []:
        A(f"- {_sent(e.get('point'))} {_sent(e.get('implication'))} [{e.get('tag')}]{_refs(e.get('refs'))}")
    A("")
    if c.get("ownership"):
        A("## Ownership")
        A(c["ownership"])
        A("")
    A("---")
    A("## Appendix: material that does not change an estimate, a probability or the stance")
    A("### A1. Section audit (moved here, not deleted)")
    for name, why in MOVED:
        A(f"- {name}: {why}")
    A("")
    A("### A2. Reconciliation")
    A(c.get("reconciliation") or "_none_")
    A("")
    A(f"### A3. EPS bridge, FY{R['base_year']['fy']}E to FY{R['base_year']['fy'] + 1}, by case (computed; each foots)")
    groups = [s["group"] for s in cs["base"]["eps_bridge"]]
    A("| Step | " + " | ".join(n.title() for n in ("bear", "base", "bull")) + " |")
    A("|---|---|---|---|")
    A(f"| FY{R['base_year']['fy']}E EPS | " + " | ".join(f"${R['base_year']['eps']:.3f}" for _ in range(3)) + " |")
    for i, g in enumerate(groups):
        A(f"| {g.replace('_', ' ')} | " + " | ".join(f"{cs[n]['eps_bridge'][i]['eps_change']:+.3f}"
                                                  for n in ("bear", "base", "bull")) + " |")
    A(f"| FY{R['base_year']['fy'] + 1} EPS | " + " | ".join(f"${cs[n]['eps']:.3f}" for n in ("bear", "base", "bull")) + " |")
    A("")
    A("### A4. Base-year build")
    A("```")
    A(ctx.get("base_block_final", "").strip())
    A("```")
    by = (res.get("scenario_inputs") or {}).get("bridge_year") or {}
    if by.get("reasoning"):
        A(by["reasoning"])
    A("")
    A("### A5. Reported quarterly lines")
    A("```")
    A(ctx.get("history_block", "").strip())
    A("```")
    A("")
    A("### A6. Macro by cost line")
    A("```")
    A(ctx.get("macro_block", "").strip())
    A("```")
    A("")
    A("### A7. Driver notes by case")
    for n in ("bull", "base", "bear"):
        notes = ((res.get("scenario_inputs") or {}).get("cases", {}).get(n) or {}).get("driver_notes") or {}
        if notes:
            A(f"**{n.title()}:** " + " ".join(f"{k}: {_sent(v)}" for k, v in notes.items()))
    A("")
    A("### A8. Guidance track record")
    A("```")
    A(ctx.get("guidance_block", "").strip())
    A("```")
    A("")
    A("### A9. Management ledger")
    A("```")
    A(ctx.get("mgmt_block", "").strip())
    A("```")
    brief = ctx.get("brief") or {}
    A("")
    A("### A10. Analyst working notes (original brief narrative)")
    A(_strip_rumors(brief.get("narrative_synthesis", "")) or "_none_")
    if (ctx.get("audit") or {}).get("overall_assessment"):
        A("")
        A("### A11. Red team verdict on the analyst brief")
        A(_strip_rumors(ctx["audit"]["overall_assessment"]))
    A("")
    A("### A12. Method")
    A(f"Model {res.get('model')}. Driver definitions and cost-line behavior come from the "
      f"'{schema['schema']}' schema config. EPS, targets, expected value, stance, conviction, consensus "
      f"position and price-implied figures are computed in code."
      + (f" Repair rounds: {', '.join(res['repaired'])}." if res.get("repaired") else ""))
    return no_em_dash("\n".join(out)).replace("no_edge", "no edge")


def render_pitch(ticker: str, res: dict, ctx: dict) -> str:
    c, R, lp, schema = res["call"], res["scenario_result"], res["live_price"], ctx["schema"]
    cs = R["cases"]
    L = [f"# {ticker}: {STANCE[R['stance']]} ({R['conviction']} conviction)",
         f"**${lp['price']:,.2f}** (close {lp['session_date']}) | probability-weighted value "
         f"**${R['expected_value']:.2f}** ({R['expected_return_pct']:+.1f}%)", "",
         f"**Thesis.** {c.get('thesis', '')}", ""]
    L.append("**Where we differ from consensus.**")
    for d in c.get("where_we_differ") or []:
        L.append(f"- {d['driver']}: ours {d['ours']:.2f}% vs {d['consensus']:.2f}% needed for consensus EPS. {_sent(d['why'])}")
    L += ["", "| | Bear | Base | Bull |", "|---|---|---|---|"]
    L.append("| Comp | " + " | ".join(f"{cs[n]['comp_pct']:+.1f}%" for n in ("bear", "base", "bull")) + " |")
    L.append("| Restaurant margin | " + " | ".join(f"{cs[n]['rlm_pct']:.1f}%" for n in ("bear", "base", "bull")) + " |")
    L.append("| EPS | " + " | ".join(f"${cs[n]['eps']:.2f}" for n in ("bear", "base", "bull")) + " |")
    L.append("| Multiple | " + " | ".join(f"{cs[n]['multiple']:.0f}x" for n in ("bear", "base", "bull")) + " |")
    L.append("| Target | " + " | ".join(f"${cs[n]['target']:.2f} ({cs[n]['return_pct']:+.0f}%)" for n in ("bear", "base", "bull")) + " |")
    L.append("| Probability | " + " | ".join(f"{cs[n]['probability']:.0%}" for n in ("bear", "base", "bull")) + " |")
    k = R.get("consensus") or {}
    if k:
        L += ["", f"Consensus ${k['eps']:.2f} sits {k['position']}. The price implies "
                  f"${R['price_implies']['eps_at_base_multiple']:.2f} at our base multiple."]
    L += ["", f"**What would change the stance.** {c.get('what_would_change_the_stance', '')}", "", "**Catalysts.**"]
    for x in sorted(c.get("catalysts") or [], key=lambda x: str(x.get("date")))[:3]:
        L.append(f"- {x.get('date')}: {x.get('event')}")
    L += ["", "**Kill criteria.**"] + [f"- {x}" for x in (c.get("kill_criteria") or [])[:3]]
    L += ["", f"**Main risk.** {c.get('strongest_counter', '')}"]
    return no_em_dash(strip_tags("\n".join(L))).replace("no_edge", "no edge")
