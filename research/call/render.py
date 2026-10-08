"""
Render the call as (1) a full digest with provenance tags and an appendix for
everything that doesn't change an estimate, a probability or the stance, and
(2) a one-page pitch with no tags. Both are scrubbed of em dashes.
"""

from __future__ import annotations

from research.call.text import no_em_dash, strip_tags, split_basis, TAGS

STANCE_LABEL = {"long": "LONG", "short": "SHORT", "avoid": "AVOID", "no_edge": "NO EDGE"}

# Step 4 section audit: brief/digest sections that do not change an estimate,
# a probability or the stance. They move to the appendix, they are not deleted.
MOVED_SECTIONS = [
    ("Business description and economic structure", "background; no estimate depends on it"),
    ("EPS base reconciliation (GAAP vs adjusted)", "method detail; the adjusted base is stated once in the drivers"),
    ("Ownership and 13F flows", "does not move estimates or probabilities unless an activist or holder event is a catalyst"),
    ("Macro color (sentiment, saving rate)", "kept only where it feeds a driver as evidence"),
    ("M&A rumors", "unsubstantiated; no value assigned"),
    ("Claim verifier results", "fact-check trail, kept for audit"),
    ("Mechanical driver model EPS, consensus back-solve, edge score, decision gate label", "diagnostics; the call uses the estimate claims and the scenarios"),
    ("Long-form analyst essay", "working notes behind the estimates; the call restates what matters"),
]


def _fmt_num(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _plain(text: str) -> str:
    """Internal stance tokens read as words in prose."""
    return text.replace("no_edge", "no edge")


def _sentence(s) -> str:
    s = (s or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


def _refs(r) -> str:
    r = [x for x in (r or []) if x]
    return f" ({', '.join(r)})" if r else ""


def render_digest(ticker: str, res: dict, ctx: dict) -> str:
    c, d, lp = res["call"], res["derived"], res["live_price"]
    stance = STANCE_LABEL.get(c.get("stance"), c.get("stance"))
    conv = d.get("conviction", c.get("conviction"))
    sc = d.get("scenarios") or {}
    out: list[str] = []
    A = out.append
    A(f"# {ticker}: {stance}, {conv} conviction")
    A(f"Price ${lp['price']:,.2f} (close {lp['session_date']}, {lp['source']}). Expected value "
      f"${d.get('expected_value')} ({d.get('expected_return_pct'):+.1f}% vs price; hurdle {res['hurdle_pct']:.0f}%). "
      f"Run {ctx.get('today')}.")
    A("")
    A("Provenance tags: " + "; ".join(f"[{k}] {v}" for k, v in TAGS.items()) + ".")
    A("")
    A("## Thesis  _(sets the stance)_")
    A(c.get("thesis", ""))
    if c.get("stance") == "avoid" and c.get("why_not_short"):
        A(f"\nWhy not a short: {c['why_not_short']}")
    if c.get("stance") == "no_edge":
        A(f"\nWhat would create an edge: {c.get('no_edge_trigger', '')}")
    if d.get("conviction_note"):
        A(f"\n_{d['conviction_note']}_")
    A("")
    A("## Is the multiple fair?  _(sets the stance and the scenario multiples)_")
    A("```")
    A(ctx.get("valuation_block", "").strip())
    A("```")
    A(c.get("price_implies", ""))
    mv = c.get("multiple_view") or {}
    if mv:
        A("")
        label, extra = split_basis(mv.get("basis", ""))
        A(f"**Verdict:** {mv.get('current_multiple')}x{' ' + label if label else ''} looks **{mv.get('verdict')}** and is "
          f"more likely to **{mv.get('direction')}**. {(extra + ' ') if extra else ''}{mv.get('reasoning', '')}")
    A("")
    A("## The drivers that matter  _(change the estimates)_")
    A("| Driver | Period | Ours | Consensus | Unit | Why |")
    A("|---|---|---|---|---|---|")
    for k in c.get("key_drivers") or []:
        A(f"| {k.get('driver')} | {k.get('period', '')} | {_fmt_num(k.get('ours'))} | {_fmt_num(k.get('consensus'))} | "
          f"{k.get('unit', '')} | {k.get('why', '')}{_refs(k.get('refs'))} |")
    A("")
    A(f"## Scenarios on {c.get('scenario_eps_period', 'next FY')} EPS  _(set the probabilities and the stance)_")
    A("Probabilities are proposed for your override (data/overrides/" + ticker + ".json).")
    A("")
    A("| Case | EPS | Multiple | Target | vs price | Probability | Reasoning |")
    A("|---|---|---|---|---|---|---|")
    for n in ("bull", "base", "bear"):
        s = sc.get(n) or {}
        A(f"| {n} | ${_fmt_num(s.get('eps'))} | {_fmt_num(s.get('multiple'), 1)}x | "
          f"${_fmt_num(s.get('target'))} | {s.get('return_pct'):+.1f}% | "
          f"{float(s.get('probability', 0)):.0%} | {s.get('reasoning', '')} |")
    A(f"\nProbability-weighted value **${d.get('expected_value')}**, {d.get('expected_return_pct'):+.1f}% vs "
      f"${lp['price']:,.2f}.")
    if d.get("overrides_applied"):
        A("Your overrides applied: " + "; ".join(d["overrides_applied"]) + ".")
    A("")
    for b in d.get("bridges") or []:
        A(f"## {b['name']}  _(changes the estimates; computed in code)_")
        A(f"Start {b['start_pct']:.2f}% ({b['start_basis']}), end **{b['end_pct']:.2f}%**, change {b['total_bps']:+.0f}bp.")
        A("")
        A("| Component | Method | Inputs | bp |")
        A("|---|---|---|---|")
        for comp in b["components"]:
            ins = ", ".join(f"{k} {v}" for k, v in comp["inputs"].items())
            A(f"| {comp['name']} | {comp['method']} | {ins or comp.get('basis', '')} | {comp['bps']:+.1f} |")
        A(f"| **Total** | | | **{b['total_bps']:+.1f}** |")
        for w in b.get("warnings") or []:
            A(f"\n_Model arithmetic disagreed and was replaced: {w}._")
        A("")
    A("## Evidence  _(supports the stance, drivers and probabilities)_")
    for e in c.get("evidence") or []:
        A(f"- {e.get('point')} [{e.get('tag')}]{_refs(e.get('refs'))}")
    A("")
    mr = c.get("management_read") or {}
    A("## Management read  _(adjusts guidance and probabilities)_")
    if mr.get("credibility"):
        A(f"**Credibility:** {mr['credibility']}")
    if mr.get("how_guidance_was_used"):
        A(f"\n**How guidance was used:** {mr['how_guidance_was_used']}")
    for s in mr.get("signals") or []:
        A(f"- {s}")
    A("\n```")
    A(ctx.get("guidance_block", "").strip())
    A("```")
    A("")
    A("## Catalysts  _(time the stance)_")
    for k in sorted(c.get("catalysts") or [], key=lambda x: str(x.get("date"))):
        A(f"- **{k.get('date')}** {k.get('event')}. We expect: {_sentence(k.get('what_we_expect'))} "
          f"If wrong: {_sentence(k.get('if_wrong'))}")
    A("")
    A("## Kill criteria  _(end the thesis)_")
    for k in c.get("kill_criteria") or []:
        A(f"- {k}")
    A("")
    A("## Strongest counter")
    A(c.get("strongest_counter", ""))
    A("")
    A("---")
    A("## Appendix: material that does not change an estimate, a probability or the stance")
    A("### A1. Section audit (moved here, not deleted)")
    for name, why in MOVED_SECTIONS:
        A(f"- {name}: {why}")
    for x in c.get("appendix_only") or []:
        A(f"- {x}")
    A("")
    A("### A2. Management ledger")
    A("```")
    A(ctx.get("mgmt_block", "").strip())
    A("```")
    brief = ctx.get("brief") or {}
    A("")
    A("### A3. Analyst working notes (original brief narrative)")
    A(brief.get("narrative_synthesis", "") or "_none_")
    if brief.get("business_description"):
        A(f"\n**Business:** {brief['business_description']}")
    audit = ctx.get("audit") or {}
    if audit.get("overall_assessment"):
        A("")
        A("### A4. Red team verdict on the analyst brief")
        A(audit["overall_assessment"])
    A("")
    A("### A5. Method")
    A(f"Model {res.get('model')}. Stance hurdle {res['hurdle_pct']:.0f}% expected return. "
      f"{'One repair round was needed. ' if res.get('repaired') else ''}"
      "Targets, expected value and bridges are computed in code from the model's inputs.")
    return _plain(no_em_dash("\n".join(out)))


def render_pitch(ticker: str, res: dict, ctx: dict) -> str:
    c, d, lp = res["call"], res["derived"], res["live_price"]
    sc = d.get("scenarios") or {}
    stance = STANCE_LABEL.get(c.get("stance"), c.get("stance"))
    L = [f"# {ticker}: {stance} ({d.get('conviction', c.get('conviction'))} conviction)",
         f"**${lp['price']:,.2f}** (close {lp['session_date']}) | expected value **${d.get('expected_value')}** "
         f"({d.get('expected_return_pct'):+.1f}%)", "",
         f"**Thesis.** {c.get('thesis', '')}", ""]
    if c.get("stance") == "avoid" and c.get("why_not_short"):
        L += [f"**Why not short.** {c['why_not_short']}", ""]
    if c.get("stance") == "no_edge":
        L += [f"**What would create an edge.** {c.get('no_edge_trigger', '')}", ""]
    L.append("**Where we differ.**")
    for k in c.get("key_drivers") or []:
        u = k.get("unit", "") or ""
        o, cv = _fmt_num(k.get("ours")), _fmt_num(k.get("consensus"))
        if u.strip().startswith("%"):
            rest = u.strip()[1:].strip()
            vals = f"ours {o}% vs consensus {cv}%" + (f" {rest}" if rest else "")
        else:
            vals = f"ours {o} vs consensus {cv} {u}".rstrip()
        L.append(f"- {k.get('driver')} {k.get('period', '')}: {vals}")
    mv = c.get("multiple_view") or {}
    label = split_basis(mv.get("basis", ""))[0]
    L += ["", f"**The multiple.** {mv.get('current_multiple')}x{' ' + label if label else ''} looks {mv.get('verdict')}, "
          f"likely to {mv.get('direction')}. {c.get('price_implies', '')}", "",
          "| Case | EPS | Multiple | Target | Prob. |", "|---|---|---|---|---|"]
    for n in ("bull", "base", "bear"):
        s = sc.get(n) or {}
        L.append(f"| {n} | ${_fmt_num(s.get('eps'))} | {_fmt_num(s.get('multiple'), 1)}x | ${_fmt_num(s.get('target'))} "
                 f"({s.get('return_pct'):+.0f}%) | {float(s.get('probability', 0)):.0%} |")
    L += ["", "**Catalysts.**"]
    for k in sorted(c.get("catalysts") or [], key=lambda x: str(x.get("date")))[:3]:
        L.append(f"- {k.get('date')}: {k.get('event')}")
    L += ["", "**Kill criteria.**"]
    L += [f"- {k}" for k in (c.get("kill_criteria") or [])[:3]]
    L += ["", f"**Main risk.** {c.get('strongest_counter', '')}"]
    return _plain(no_em_dash(strip_tags("\n".join(L))))
