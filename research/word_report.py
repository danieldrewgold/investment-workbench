"""
Word doc research report renderer.

Walks the pipeline's `result` dict (what `run_research()` returns) plus the
`ResearchBrief` and emits a stylized .docx. Renders 6 sections + header + appendix
per the approved plan in C:\\Users\\Daniel\\.claude\\plans\\unified-toasting-globe.md

Entry point:
    render_word_report(result, brief, outpath) -> Path

Design rules:
    * No new data computation here -- pure rendering. All values come from
      pre-computed objects. New logic lives in the pipeline/adversarial/edge modules.
    * Stress tests appear as italic gray pull-quotes woven into the prose,
      not as labeled subsections.
    * Attribution is inline ("In the Q3 call, CEO said...") -- no footnotes.
    * Confidence banner is intentionally absent (user preference).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches

from research.word_styles import (
    COLOR_ACCENT, COLOR_HEADING, COLOR_WARNING_BG,
    apply_styles, shade_cell, set_table_borders,
)


# ======================================================================
# Public entry point
# ======================================================================

def render_word_report(result: dict, brief=None, outpath: str | Path | None = None,
                        deep: bool = False) -> Path:
    """Render a research note to .docx. Returns the output path.

    Args:
        result: dict returned by `run_research()`.
        brief: the `ResearchBrief` (optional). If provided, we render rich
            basis prose per driver; if absent, the Drivers section is table-only.
        outpath: where to write. Defaults to
            data/reports/{TICKER}_{YYYYMMDD_HHMM}.docx
        deep: if True, caller asked for Option-B per-section deep recursion.
            v1 accepts the flag but prints a gated-message; does not execute.
    """
    ticker = (result.get("ticker") or "UNKNOWN").upper()
    if outpath is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        outpath = Path("data/reports") / f"{ticker}_{ts}.docx"
    outpath = Path(outpath)
    outpath.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    apply_styles(doc)
    # Tight page margins
    for section in doc.sections:
        section.top_margin = Inches(0.7)
        section.bottom_margin = Inches(0.7)
        section.left_margin = Inches(0.85)
        section.right_margin = Inches(0.85)

    critiques = _collect_critiques(result)

    _render_header_card(doc, result, brief)
    _render_warnings_banner(doc, result)
    _render_street_consensus(doc, result)
    _render_edge(doc, result, brief, critiques)
    _render_drivers(doc, result, brief, critiques)
    _render_consensus_and_valuation(doc, result, brief, critiques)
    _render_catalysts(doc, result)
    _render_risks_and_kill_criteria(doc, result, brief)
    _render_appendix(doc, result, brief)

    doc.save(str(outpath))
    if deep:
        print("  [Word] --deep flag received: Option B disabled pending explicit approval.")
    return outpath


# ======================================================================
# Header Card
# ======================================================================

def _render_header_card(doc, result, brief):
    ticker = result.get("ticker", "?")
    # Title: "{TICKER} - {Company}"
    company = ""
    if brief is not None:
        company = (brief.company_name or "").strip()
    if not company and result.get("business_description"):
        company = result["business_description"].split(".")[0][:70]
    title_text = f"{ticker}" + (f" — {company}" if company else "")
    doc.add_paragraph(title_text, style="ReportTitle")

    # Meta line
    schema = result.get("schema", "?")
    fin_source = result.get("financials_source", "?")
    fy = result.get("financials_fy", "")
    as_of = datetime.now().strftime("%Y-%m-%d")
    meta = f"As of {as_of}  |  Schema: {schema}  |  Data: {fin_source}"
    if fy:
        meta += f" (FY{fy})"
    doc.add_paragraph(meta, style="SmallMeta")

    # Price/PT/Direction line
    valuation = result.get("valuation") or {}
    pt = valuation.get("price_target") or valuation.get("pt")
    current = valuation.get("current_price")
    direction = _infer_direction(result, brief)
    bits = []
    if direction:
        bits.append(f"Direction: {direction}")
    if pt:
        bits.append(f"PT: ${pt:.2f}")
    if current:
        bits.append(f"Current: ${current:.2f}")
        if pt:
            upside = (pt - current) / current * 100
            bits.append(f"Upside: {upside:+.1f}%")
    if bits:
        p = doc.add_paragraph(style="SubHeading")
        run = p.add_run(" | ".join(bits))
        run.font.color.rgb = COLOR_ACCENT

    # 3-bullet summary: thesis / catalyst / risk
    thesis = (result.get("edge_hypothesis") or "").strip()
    ea = result.get("edge_assessment") or {}
    cats = ea.get("catalysts") or []
    cat = cats[0] if cats else None
    risk = _top_risk_oneliner(result)

    if thesis or cat or risk:
        doc.add_paragraph("Summary", style="SubHeading")
        if thesis:
            _bullet(doc, f"Thesis: {thesis}")
        if cat:
            _bullet(doc, f"Next catalyst: {cat.get('event','?')} ({cat.get('timeframe','?')})")
        if risk:
            _bullet(doc, f"Top risk: {risk}")


# ======================================================================
# Warnings Banner (reasonability / extraordinary-variant callouts)
# ======================================================================

def _render_warnings_banner(doc, result):
    """
    Render pipeline warnings that the reader MUST NOT MISS at the top of
    the doc. Specifically the REASONABILITY / EXTRAORDINARY-VARIANT
    warning emitted when our EPS variant exceeds 50% of consensus — it
    signals the number is either an exceptional conviction call OR the
    adversarial/bear pass compounded absurdly and the estimate is
    mis-calibrated.

    Comp-discipline warnings are intentionally excluded here — those are
    already rendered in the Drivers section where they belong.
    Evidence-gap notes are skipped here too — they flow into the Appendix.

    Style: shaded single-cell table in muted crimson, bold accent text.
    Nothing rendered if there are no non-comp warnings.
    """
    warnings = result.get("brief_warnings") or []
    if not warnings:
        return

    # Exclude warnings already surfaced elsewhere
    def _skip(w: str) -> bool:
        lw = w.lower()
        if "decomposition" in lw or "comp discipline" in lw:
            return True  # rendered in Drivers section
        return False

    banner_warnings = [w for w in warnings if not _skip(w)]
    if not banner_warnings:
        return

    # Single-cell shaded table as a callout box
    table = doc.add_table(rows=len(banner_warnings), cols=1)
    for i, w in enumerate(banner_warnings):
        cell = table.rows[i].cells[0]
        shade_cell(cell, hex_fill=COLOR_WARNING_BG)
        # Replace the cell's default paragraph with a styled one
        cell.paragraphs[0].text = ""
        # Bold prefix marker so the reader's eye catches the callout
        prefix = "⚠ "
        # Strip any trailing whitespace and keep the full warning body
        cell.paragraphs[0].text = prefix + w.strip()
        for p in cell.paragraphs:
            p.style = doc.styles["WarningBanner"]
    set_table_borders(table)


# ======================================================================
# Street Consensus (between Header Card and Edge)
# ======================================================================

def _render_street_consensus(doc, result):
    """
    Render the sell-side consensus snapshot: per-period EPS + revenue,
    revision activity, price targets, rating distribution, next earnings
    window. All real data — no inference.
    """
    c = result.get("consensus_full")
    if not c:
        return  # Nothing to render — upstream fetch failed or registry-only

    doc.add_paragraph("Street Consensus", style="SectionHeading")

    # Per-period estimates table: EPS across 4 periods + revenue summary
    periods = []
    for key in ("current_quarter", "next_quarter", "current_year", "next_year"):
        pe = c.get(key)
        if pe and pe.get("eps_mean") is not None:
            periods.append(pe)

    if periods:
        # Compact table: Period | EPS mean | Range | n | YoY | 30d Δ | Revisions
        table = doc.add_table(rows=1, cols=7)
        hdr = table.rows[0].cells
        for i, label in enumerate(["Period", "EPS (mean)", "Range", "n", "YoY", "30d Δ", "Revs 30d"]):
            hdr[i].text = label
            shade_cell(hdr[i])
            for p in hdr[i].paragraphs:
                for r in p.runs:
                    r.bold = True
        for pe in periods:
            row = table.add_row().cells
            row[0].text = pe.get("period_label", pe.get("period", "?"))
            mean = pe.get("eps_mean")
            lo = pe.get("eps_low")
            hi = pe.get("eps_high")
            row[1].text = f"${mean:.2f}" if mean is not None else "—"
            row[2].text = f"${lo:.2f} – ${hi:.2f}" if lo is not None and hi is not None else "—"
            row[3].text = str(pe.get("eps_num_analysts", 0))
            growth = pe.get("eps_growth_yoy")
            row[4].text = f"{growth*100:+.1f}%" if growth is not None else "—"
            cur, d30 = pe.get("eps_current"), pe.get("eps_30d_ago")
            if cur is not None and d30 is not None:
                delta = cur - d30
                direction = "↑" if delta > 0.005 else "↓" if delta < -0.005 else "≈"
                row[5].text = f"{direction} {delta:+.3f}"
            else:
                row[5].text = "—"
            up = pe.get("up_revs_30d", 0)
            dn = pe.get("down_revs_30d", 0)
            row[6].text = f"↑{up} / ↓{dn}" if (up + dn) > 0 else "—"
        set_table_borders(table)

    # Revenue consensus in a compact line (per period, $B)
    rev_line_bits = []
    for key, label in [("current_quarter", "Q"), ("next_quarter", "Q+1"),
                        ("current_year", "FY"), ("next_year", "FY+1")]:
        pe = c.get(key)
        if pe and pe.get("revenue_mean") is not None:
            rev_b = pe["revenue_mean"] / 1e9
            growth = pe.get("revenue_growth_yoy")
            bit = f"{label} ${rev_b:.2f}B"
            if growth is not None:
                bit += f" ({growth*100:+.1f}%)"
            rev_line_bits.append(bit)
    if rev_line_bits:
        doc.add_paragraph("Revenue consensus: " + "  |  ".join(rev_line_bits))

    # Price target distribution
    pt = c.get("price_target") or {}
    pt_bits = []
    if pt.get("mean") is not None:
        pt_bits.append(f"Mean ${pt['mean']:.2f}")
    if pt.get("median") is not None:
        pt_bits.append(f"Median ${pt['median']:.2f}")
    if pt.get("low") is not None and pt.get("high") is not None:
        pt_bits.append(f"Range ${pt['low']:.2f}–${pt['high']:.2f}")
    if pt.get("current_price") is not None and pt.get("mean") is not None and pt["current_price"] > 0:
        upside = (pt["mean"] - pt["current_price"]) / pt["current_price"] * 100
        pt_bits.append(f"Current ${pt['current_price']:.2f} ({upside:+.1f}% to mean)")
    if pt_bits:
        doc.add_paragraph("Price targets: " + "  |  ".join(pt_bits))

    # Rating distribution (current snapshot)
    ratings = c.get("ratings") or []
    if ratings:
        r = ratings[0]
        total = (r.get("strong_buy", 0) + r.get("buy", 0) + r.get("hold", 0)
                 + r.get("sell", 0) + r.get("strong_sell", 0))
        if total > 0:
            bull = (r.get("strong_buy", 0) + r.get("buy", 0)) / total * 100
            doc.add_paragraph(
                f"Analyst ratings (n={total}): "
                f"Strong Buy {r.get('strong_buy', 0)} · Buy {r.get('buy', 0)} · "
                f"Hold {r.get('hold', 0)} · Sell {r.get('sell', 0)} · "
                f"Strong Sell {r.get('strong_sell', 0)}  ·  {bull:.0f}% bullish"
            )

    # Next earnings event with expected range
    ne = c.get("next_earnings") or {}
    if ne.get("date"):
        days = ne.get("days_out")
        eps_m = ne.get("eps_mean")
        eps_lo = ne.get("eps_low")
        eps_hi = ne.get("eps_high")
        rev_m = ne.get("revenue_mean")
        bits = [f"Next earnings: {ne['date']}"]
        if days is not None:
            bits[-1] += f" ({days}d out)"
        if eps_m is not None:
            range_str = ""
            if eps_lo is not None and eps_hi is not None:
                range_str = f" (${eps_lo:.2f}–${eps_hi:.2f})"
            bits.append(f"EPS ${eps_m:.2f}{range_str}")
        if rev_m is not None:
            bits.append(f"Rev ${rev_m/1e9:.2f}B")
        doc.add_paragraph("  ·  ".join(bits))

    # LTG (long-term EPS growth) as a one-line
    ltg = c.get("ltg_eps_5yr")
    if ltg is not None:
        doc.add_paragraph(f"Long-term EPS growth (5yr consensus): {ltg*100:+.1f}%")


# ======================================================================
# 1. Edge
# ======================================================================

def _render_edge(doc, result, brief, critiques):
    doc.add_paragraph("1. Edge", style="SectionHeading")

    edge_hyp = (result.get("edge_hypothesis") or "").strip()
    why_wrong = (result.get("why_market_is_wrong") or "").strip()
    key_debate = (result.get("key_debate") or "").strip()

    if edge_hyp:
        doc.add_paragraph(edge_hyp)
    if why_wrong:
        doc.add_paragraph(why_wrong)
    if key_debate and key_debate not in (edge_hyp, why_wrong):
        doc.add_paragraph(f"The debate: {key_debate}")

    # Consensus assumptions (prose-style, compact)
    cons_asm = result.get("consensus_assumptions") or {}
    if cons_asm:
        doc.add_paragraph("What consensus assumes:", style="SubHeading")
        for drv, desc in cons_asm.items():
            if desc:
                _bullet(doc, f"{drv}: {desc}")

    # Guidance vs. our view
    gvo = result.get("guidance_vs_our_view") or {}
    if gvo:
        doc.add_paragraph("Where we differ from guidance:", style="SubHeading")
        for drv, desc in gvo.items():
            if desc:
                _bullet(doc, f"{drv}: {desc}")

    # Transcript inflection tone (if available) -- supports the "market is wrong" view
    ta = result.get("transcript_analysis") or {}
    inflections = ta.get("inflection_points") or []
    if inflections:
        lead = inflections[0]
        if isinstance(lead, dict):
            q = lead.get("quarter") or lead.get("when") or "a recent call"
            txt = lead.get("description") or lead.get("note") or ""
        else:
            q, txt = "a recent call", str(lead)
        if txt:
            doc.add_paragraph(f"Management tone shift in {q}: {txt}")

    _render_critiques_for(doc, critiques, section="edge")


# ======================================================================
# 2. Drivers
# ======================================================================

def _render_drivers(doc, result, brief, critiques):
    doc.add_paragraph("2. Drivers", style="SectionHeading")

    drivers = result.get("drivers") or {}
    brief_drivers = (brief.drivers if brief is not None else []) or []
    # Map for easy lookup by assumption_key
    brief_by_name = {d.get("name"): d for d in brief_drivers}
    brief_by_key = {d.get("assumption_key"): d for d in brief_drivers}

    # Driver table
    if drivers:
        table = doc.add_table(rows=1, cols=4)
        hdr = table.rows[0].cells
        hdr[0].text = "Driver / Component"
        hdr[1].text = "Value"
        hdr[2].text = "Conf."
        hdr[3].text = "Basis"
        for c in hdr:
            shade_cell(c)
            for p in c.paragraphs:
                for r in p.runs:
                    r.bold = True
        for dname, dinfo in drivers.items():
            brief_d = brief_by_name.get(dname) or brief_by_key.get(dname) or {}
            brief_comps = {c.get("name"): c for c in (brief_d.get("components") or [])}
            # Driver total row
            row = table.add_row().cells
            row[0].text = dname
            total_val = dinfo.get("value")
            row[1].text = _fmt_val(total_val, unit=brief_d.get("unit", "pct"))
            row[2].text = ""
            row[3].text = (brief_d.get("basis") or "")[:160]
            for cell in row:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.bold = True
            # Component rows
            for cname, cinfo in (dinfo.get("components") or {}).items():
                brief_c = brief_comps.get(cname, {})
                row = table.add_row().cells
                row[0].text = f"    {cname}"
                row[1].text = _fmt_val(cinfo.get("value"), unit=brief_c.get("unit", "pct"))
                conf = cinfo.get("confidence")
                row[2].text = f"{conf:.2f}" if isinstance(conf, (int, float)) else ""
                row[3].text = (brief_c.get("basis") or "")[:160]
        set_table_borders(table)

    # Bear revisions (annotate what was revised and why)
    bear_revs = (brief.bear_revisions if brief is not None else []) or []
    if bear_revs:
        doc.add_paragraph("Bear revisions:", style="SubHeading")
        for rev in bear_revs:
            drv = rev.get("driver", "?")
            comp = rev.get("component", "?")
            new_val = rev.get("new_value")
            reason = rev.get("reason", "")
            # Find original
            brief_d = brief_by_name.get(drv) or {}
            comps = {c.get("name"): c for c in (brief_d.get("components") or [])}
            orig = comps.get(comp, {}).get("value")
            line = f"{drv}.{comp}: "
            if orig is not None and new_val is not None:
                line += f"{orig} → {new_val}"
            elif new_val is not None:
                line += f"revised to {new_val}"
            if reason:
                line += f" — {reason}"
            _bullet(doc, line)

    # Restaurant/franchise: surface comp discipline warning if fired
    warnings = result.get("brief_warnings") or []
    comp_warnings = [w for w in warnings if "decomposition" in w.lower() or "comp" in w.lower()]
    if comp_warnings:
        doc.add_paragraph("Comp decomposition check:", style="SubHeading")
        for w in comp_warnings:
            _bullet(doc, w)

    _render_critiques_for(doc, critiques, section="drivers")


# ======================================================================
# 3. Consensus Gap & Valuation
# ======================================================================

def _render_consensus_and_valuation(doc, result, brief, critiques):
    doc.add_paragraph("3. Consensus Gap & Valuation", style="SectionHeading")

    our_eps = result.get("post_eps")
    cons_eps = result.get("consensus_eps")
    ea = result.get("edge_assessment") or {}
    variant_pct = ea.get("variant_pct", 0)
    verdict = ea.get("verdict", "")

    if our_eps is not None and cons_eps is not None:
        diff = our_eps - cons_eps
        direction = "above" if diff > 0 else "below"
        doc.add_paragraph(
            f"Our estimate: EPS ${our_eps:.2f} vs. consensus ${cons_eps:.2f} "
            f"(${abs(diff):.2f} / {abs(variant_pct):.1f}% {direction})."
        )
    elif our_eps is not None:
        doc.add_paragraph(f"Our estimate: EPS ${our_eps:.2f} (no consensus reference).")

    if ea.get("edge_narrative"):
        doc.add_paragraph(ea["edge_narrative"])

    # Implied street drivers vs ours -- only if we have variants
    variants = ea.get("variants") or []
    if variants:
        table = doc.add_table(rows=1, cols=5)
        hdr = table.rows[0].cells
        hdr[0].text = "Driver"
        hdr[1].text = "Ours"
        hdr[2].text = "Street"
        hdr[3].text = "Δ EPS"
        hdr[4].text = "Conf."
        for c in hdr:
            shade_cell(c)
            for p in c.paragraphs:
                for r in p.runs:
                    r.bold = True
        had_unreached = False
        for v in variants[:8]:
            reach = v.get("reachability", "reached")
            unreached = reach in ("clamped_lower", "clamped_upper", "unreached")
            row = table.add_row().cells
            row[0].text = f"{v.get('driver','?')}.{v.get('component','?')}"
            row[1].text = f"{v.get('our_value', 0):+.2f}"
            # Don't quote a back-solve-hit-bound as a "street value" —
            # those numbers aren't real implied-street assumptions.
            if unreached:
                row[2].text = "n/a (unreachable)"
                row[3].text = "—"
                had_unreached = True
            else:
                row[2].text = f"{v.get('consensus_value', 0):+.2f}"
                row[3].text = f"{v.get('eps_contribution', 0):+.3f}"
            conf = v.get("confidence")
            row[4].text = f"{conf:.2f}" if isinstance(conf, (int, float)) else ""
        set_table_borders(table)

        if had_unreached:
            # Explain the "n/a" rows so the reader knows what it means
            note = doc.add_paragraph(
                "Rows marked 'n/a (unreachable)' mean no single value of that driver "
                "within plausible bounds would produce consensus EPS when other "
                "drivers are held at our values — i.e., the EPS gap can't be "
                "attributed to that driver alone. The ΔEPS column is not shown "
                "for those rows because the implied-street value would be a "
                "search-bound artifact, not a real derivation.",
                style="PullQuote",
            )

    # Valuation
    val = result.get("valuation") or {}
    if val:
        bits = []
        if val.get("price_target") is not None:
            bits.append(f"PT ${val['price_target']:.2f}")
        if val.get("implied_multiple") is not None:
            bits.append(f"implied P/E {val['implied_multiple']:.1f}×")
        if val.get("method"):
            bits.append(f"method: {val['method']}")
        if bits:
            doc.add_paragraph("Valuation: " + ", ".join(bits) + ".")

    # Priced-in read
    priced = ea.get("priced_in") or {}
    if priced:
        p_txt = "Priced in: likely" if priced.get("likely_priced_in") else "Priced in: not fully"
        reason = priced.get("reasoning", "")
        if reason:
            p_txt += f" — {reason}"
        doc.add_paragraph(p_txt)

    if verdict:
        doc.add_paragraph(f"Edge verdict: {verdict} (score {ea.get('actionability_score', 0):.3f}).")

    _render_critiques_for(doc, critiques, section="valuation")
    _render_critiques_for(doc, critiques, section="consensus")


# ======================================================================
# 4. Catalysts
# ======================================================================

def _render_catalysts(doc, result):
    doc.add_paragraph("4. Catalysts", style="SectionHeading")

    ea = result.get("edge_assessment") or {}
    catalysts = ea.get("catalysts") or []

    if catalysts:
        for c in catalysts[:6]:
            event = c.get("event", "?")
            timeframe = c.get("timeframe", "?")
            resolves = c.get("resolves_driver", "")
            impact = c.get("impact", "")
            line = f"{event} ({timeframe})"
            if resolves:
                line += f" — resolves {resolves}"
            if impact:
                line += f"; {impact}"
            _bullet(doc, line)
    else:
        doc.add_paragraph("No dated catalysts identified in filings or available market data.")

    # Market overlay signals (if available via edge assessment or top-level)
    overlay_bits = []
    if "short_interest_pct" in result:
        overlay_bits.append(f"Short interest: {result['short_interest_pct']:.1f}%")
    if "implied_move_pct" in result:
        overlay_bits.append(f"Implied move: {result['implied_move_pct']:.1f}%")
    if "put_call_ratio" in result:
        overlay_bits.append(f"P/C: {result['put_call_ratio']:.2f}")
    if overlay_bits:
        doc.add_paragraph("Positioning: " + "; ".join(overlay_bits) + ".")


# ======================================================================
# 5. Risks, Blind Spots, Kill Criteria
# ======================================================================

def _render_risks_and_kill_criteria(doc, result, brief):
    doc.add_paragraph("5. Risks, Blind Spots & Kill Criteria", style="SectionHeading")

    # Top 3 knowable risks from contradictions
    contras = result.get("contradictions") or []
    if contras:
        doc.add_paragraph("Top risks:", style="SubHeading")
        # Sort by severity if present: serious > moderate > minor
        sev_rank = {"serious": 0, "moderate": 1, "minor": 2}
        contras_sorted = sorted(
            contras,
            key=lambda c: sev_rank.get((c.get("severity") or "").lower(), 3),
        )
        for c in contras_sorted[:3]:
            thesis = c.get("thesis", "") or c.get("claim_under_attack", "")
            counter = c.get("counter_evidence", "") or c.get("counter_argument", "")
            sev = c.get("severity", "")
            line = thesis
            if counter:
                line += f" — counter: {counter}"
            if sev:
                line += f" [{sev}]"
            _bullet(doc, line)

    # Top 3 blind spots from adversarial response
    adv = result.get("adversarial_response") or {}
    blind_spots = adv.get("blind_spots") or []
    if blind_spots:
        doc.add_paragraph("Blind spots (auditor-independent):", style="SubHeading")
        for bs in blind_spots[:3]:
            _bullet(doc, str(bs))

    # Kill criteria -- synthesized from evidence_gaps or adversarial assessment
    kill_line = _synthesize_kill_criteria(result, brief)
    if kill_line:
        doc.add_paragraph(f"What would change our mind: {kill_line}",
                          style="KillCriteria")


# ======================================================================
# Appendix
# ======================================================================

def _render_appendix(doc, result, brief):
    schema = (result.get("schema") or "").lower()
    niche = schema in ("general", "other", "")
    business = result.get("business_description") or ""
    gaps = result.get("evidence_gaps")
    if gaps is None and brief is not None:
        gaps = brief.evidence_gaps or []
    gaps = gaps or []

    # Only render appendix if we have something worth showing
    has_content = (niche and business) or gaps
    if not has_content:
        return

    doc.add_paragraph("Appendix", style="SectionHeading")

    if niche and business:
        doc.add_paragraph("Business context:", style="SubHeading")
        doc.add_paragraph(business)

    if gaps:
        doc.add_paragraph("Evidence gaps (research TODO):", style="SubHeading")
        for g in gaps[:8]:
            _bullet(doc, str(g))


# ======================================================================
# Helpers
# ======================================================================

def _bullet(doc, text: str) -> None:
    """Add a bulleted paragraph using the built-in 'List Bullet' style."""
    p = doc.add_paragraph(text, style="List Bullet")
    p.style.font.size = p.style.font.size  # no-op; keep Normal font size


def _fmt_val(val, unit: str = "pct") -> str:
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        if unit == "pct":
            return f"{val:+.2f}%"
        if unit == "bps":
            return f"{val:+.0f}bps"
        if unit == "count":
            return f"{int(val)}"
        return f"{val:+.2f}"
    return str(val)


def _infer_direction(result, brief) -> str:
    """LONG / SHORT / n/a. Prefer edge_type, fall back to variant sign."""
    edge_type = (result.get("edge_type") or "").lower()
    ea = result.get("edge_assessment") or {}
    variant_eps = ea.get("variant_eps", 0)
    if "short" in edge_type or "over" in (result.get("why_market_is_wrong") or "").lower():
        return "SHORT"
    if "long" in edge_type or "under" in (result.get("why_market_is_wrong") or "").lower():
        return "LONG"
    if variant_eps > 0:
        return "LONG"
    if variant_eps < 0:
        return "SHORT"
    return ""


def _top_risk_oneliner(result) -> str:
    """Pick the most severe contradiction as a one-line risk summary."""
    contras = result.get("contradictions") or []
    if not contras:
        # fall back to blind spot
        adv = result.get("adversarial_response") or {}
        bs = adv.get("blind_spots") or []
        return str(bs[0]) if bs else ""
    sev_rank = {"serious": 0, "moderate": 1, "minor": 2}
    top = sorted(contras,
                 key=lambda c: sev_rank.get((c.get("severity") or "").lower(), 3))[0]
    return (top.get("thesis") or top.get("counter_evidence") or "")[:140]


def _synthesize_kill_criteria(result, brief) -> str:
    """One-line synthesis of what would disprove the thesis."""
    # Prefer explicit evidence_gaps first item if it's phrased as a gap
    gaps = result.get("evidence_gaps")
    if gaps is None and brief is not None:
        gaps = brief.evidence_gaps or []
    gaps = gaps or []

    # Try to synthesize from the top REACHABLE variant driver. Skip
    # variants where bisection hit a bound — those "street values" are
    # search-bound artifacts, not real implied-street assumptions.
    ea = result.get("edge_assessment") or {}
    variants = ea.get("variants") or []
    reachable = [v for v in variants
                 if v.get("reachability", "reached") == "reached"]
    if reachable:
        top = reachable[0]
        drv = f"{top.get('driver','?')}.{top.get('component','?')}"
        our = top.get("our_value", 0)
        street = top.get("consensus_value", 0)
        direction = "below" if our > street else "above"
        return (f"Print on {drv} comes in {direction} our {our:+.1f} figure "
                f"(toward street {street:+.1f}) in the next 1-2 prints.")
    # No reachable variant — fall through to evidence gaps or return empty.

    # Fall back to first evidence gap
    if gaps:
        return f"Unable to close gap: {gaps[0]}"
    return ""


def _collect_critiques(result) -> list[dict]:
    """Pull structural_critiques from adversarial_response if present.

    Mutable 'rendered' flag lets us dedupe: a critique targeting multiple
    sections appears only in the first one we render (edge > drivers > valuation).
    """
    adv = result.get("adversarial_response") or {}
    crits = adv.get("structural_critiques") or []
    cleaned = []
    for c in crits:
        if not isinstance(c, dict):
            continue
        cleaned.append({
            "target_sections": [s.lower() for s in (c.get("target_sections") or [])],
            "claim_under_attack": c.get("claim_under_attack", ""),
            "counter_argument": c.get("counter_argument", ""),
            "severity": (c.get("severity") or "").lower(),
            "rendered": False,
        })
    return cleaned


def _render_critiques_for(doc, critiques, *, section: str) -> None:
    """Emit italic pull-quotes for critiques tagged to this section.

    Dedupes: once a critique is rendered, it won't render again even if it
    targets later sections too.
    """
    if not critiques:
        return
    for c in critiques:
        if c.get("rendered"):
            continue
        if section not in c["target_sections"]:
            continue
        claim = c["claim_under_attack"].strip()
        counter = c["counter_argument"].strip()
        if not counter:
            continue
        if claim:
            text = f"Counter: the analyst claims {_trim(claim)} — but {_trim(counter)}"
        else:
            text = f"Counter: {_trim(counter)}"
        doc.add_paragraph(text, style="PullQuote")
        c["rendered"] = True


def _trim(text: str, max_len: int = 400) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rsplit(" ", 1)[0] + "…"
