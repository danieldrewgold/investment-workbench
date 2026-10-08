"""
Research Export

Exports research results to Excel workbook with formatted analyst-readable tabs.
One tab per section: Summary, Drivers, Sensitivity, Adversarial, Edge, Valuation.

Usage:
    from research.export import export_excel
    export_excel(result, "CMG_research.xlsx")
"""

from pathlib import Path
from datetime import datetime

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    from research.financial_model import build_financial_model, ModelInputs
    HAS_FINANCIAL_MODEL = True
except ImportError:
    HAS_FINANCIAL_MODEL = False


# Styles
HEADER_FONT = Font(bold=True, size=11, color="FFFFFF") if HAS_OPENPYXL else None
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid") if HAS_OPENPYXL else None
SUBHEADER_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid") if HAS_OPENPYXL else None
VERDICT_FONT = Font(bold=True, size=14) if HAS_OPENPYXL else None
THIN_BORDER = Border(
    bottom=Side(style="thin", color="CCCCCC")
) if HAS_OPENPYXL else None


def _header_row(ws, row, values):
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")


def _subheader_row(ws, row, values):
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.font = Font(bold=True)
        cell.fill = SUBHEADER_FILL


def export_excel(result: dict, filepath: str = None) -> str:
    """
    Export research result to a formatted Excel workbook.

    Args:
        result: dict from run_research()
        filepath: output path (default: data/exports/{ticker}_{timestamp}.xlsx)

    Returns:
        filepath of the created workbook
    """
    if not HAS_OPENPYXL:
        raise ImportError("openpyxl required: pip install openpyxl")

    ticker = result.get("ticker", "UNKNOWN")
    if not filepath:
        export_dir = Path("data/exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = str(export_dir / f"{ticker}_{ts}.xlsx")

    wb = Workbook()

    # ── Tab 1: Summary ──
    ws = wb.active
    ws.title = "Summary"
    _build_summary_tab(ws, result)

    # ── Tab 2: Drivers ──
    ws2 = wb.create_sheet("Drivers")
    _build_drivers_tab(ws2, result)

    # ── Tab 3: Sensitivity ──
    ws3 = wb.create_sheet("Sensitivity")
    _build_sensitivity_tab(ws3, result)

    # ── Tab 4: Adversarial ──
    ws4 = wb.create_sheet("Adversarial")
    _build_adversarial_tab(ws4, result)

    # ── Tab 5: Edge Detection ──
    ws5 = wb.create_sheet("Edge")
    _build_edge_tab(ws5, result)

    # ── Tab 6: Valuation ──
    ws6 = wb.create_sheet("Valuation")
    _build_valuation_tab(ws6, result)

    wb.save(filepath)
    return filepath


def _build_summary_tab(ws, r):
    """Executive summary page."""
    ticker = r.get("ticker", "?")

    # Title
    ws.cell(row=1, column=1, value=f"{ticker} Research Summary").font = Font(bold=True, size=16)
    ws.cell(row=2, column=1, value=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws.cell(row=2, column=1).font = Font(color="888888")

    # Decision verdict
    verdict = r.get("decision_verdict", "?")
    ws.cell(row=4, column=1, value="DECISION VERDICT")
    ws.cell(row=4, column=1).font = Font(bold=True, size=12)
    ws.cell(row=4, column=2, value=verdict)
    ws.cell(row=4, column=2).font = VERDICT_FONT

    # Key metrics
    row = 6
    _subheader_row(ws, row, ["Metric", "Value", "vs Consensus", "Notes"])
    row += 1

    cons_eps = r.get("consensus_eps")
    post_eps = r.get("post_eps", 0)
    pre_eps = r.get("pre_eps", 0)

    metrics = [
        ("Pre-Challenge EPS", f"${pre_eps:.2f}", "", "Before adversarial"),
        ("Post-Challenge EPS", f"${post_eps:.2f}",
         f"${post_eps - cons_eps:+.2f} ({(post_eps/cons_eps-1)*100:+.1f}%)" if cons_eps else "N/A",
         "After bear revisions"),
        ("Consensus EPS", f"${cons_eps:.2f}" if cons_eps else "N/A", "", "Forward street estimate"),
        ("Revenue", f"${r.get('post_revenue', 0):,.1f}M", "", ""),
        ("Schema", r.get("schema", "?"), "", r.get("quality_line", "")),
        ("Estimate Grade", r.get("estimate_grade", "?"), "", ""),
    ]
    for label, value, vs, notes in metrics:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        ws.cell(row=row, column=3, value=vs)
        ws.cell(row=row, column=4, value=notes)
        row += 1

    # Business context
    row += 1
    ws.cell(row=row, column=1, value="BUSINESS").font = Font(bold=True, size=12)
    row += 1
    ws.cell(row=row, column=1, value=r.get("business_description", "")[:200])
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 1
    ws.cell(row=row, column=1, value=f"Key Debate: {r.get('key_debate', '')[:200]}")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)

    # Edge hypothesis (from the research brain)
    if r.get("edge_hypothesis"):
        row += 2
        ws.cell(row=row, column=1, value="EDGE HYPOTHESIS").font = Font(bold=True, size=12, color="2F5496")
        row += 1
        ws.cell(row=row, column=1, value=r["edge_hypothesis"][:300])
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 1
        ws.cell(row=row, column=1, value="Edge Type")
        ws.cell(row=row, column=2, value=r.get("edge_type", ""))
        row += 1
        if r.get("why_market_is_wrong"):
            ws.cell(row=row, column=1, value="Where We Differ From Consensus")
            ws.cell(row=row, column=2, value=r["why_market_is_wrong"][:300])
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            row += 1

        # Consensus assumptions
        cons_assum = r.get("consensus_assumptions", {})
        if cons_assum:
            row += 1
            ws.cell(row=row, column=1, value="What Street Assumes").font = Font(bold=True)
            for driver, view in cons_assum.items():
                row += 1
                ws.cell(row=row, column=1, value=driver)
                ws.cell(row=row, column=2, value=view[:200])
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)

        # Guidance vs our view
        guidance = r.get("guidance_vs_our_view", {})
        if guidance:
            row += 1
            ws.cell(row=row, column=1, value="Guidance vs Our View").font = Font(bold=True)
            for driver, view in guidance.items():
                row += 1
                ws.cell(row=row, column=1, value=driver)
                ws.cell(row=row, column=2, value=view[:200])
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)

    # Edge detection results
    ea = r.get("edge_assessment") or {}
    if ea.get("verdict"):
        row += 2
        ws.cell(row=row, column=1, value="EDGE ASSESSMENT").font = Font(bold=True, size=12)
        row += 1
        ws.cell(row=row, column=1, value="Verdict")
        ws.cell(row=row, column=2, value=ea.get("verdict"))
        row += 1
        ws.cell(row=row, column=1, value="Actionability Score")
        ws.cell(row=row, column=2, value=round(ea.get("actionability_score", 0), 3))
        row += 1
        ws.cell(row=row, column=1, value="Variant")
        ws.cell(row=row, column=2, value=f"${ea.get('variant_eps',0):+.2f} ({ea.get('variant_pct',0):+.1f}%)")
        row += 1
        ws.cell(row=row, column=1, value="Time Horizon")
        ws.cell(row=row, column=2, value=ea.get("time_horizon", ""))
        if ea.get("edge_narrative"):
            row += 1
            ws.cell(row=row, column=1, value=ea["edge_narrative"][:300])
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)

    # Warnings
    warns = r.get("brief_warnings", [])
    if warns:
        row += 2
        ws.cell(row=row, column=1, value="WARNINGS").font = Font(bold=True, color="CC0000")
        for w in warns:
            row += 1
            ws.cell(row=row, column=1, value=w)

    # Column widths
    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 25
    ws.column_dimensions["D"].width = 40


def _build_drivers_tab(ws, r):
    """Driver decomposition table."""
    ws.cell(row=1, column=1, value=f"{r['ticker']} Driver Decomposition").font = Font(bold=True, size=14)

    _header_row(ws, 3, ["Driver", "Component", "Value", "Unit", "Confidence", "Basis"])

    row = 4
    for dname, dinfo in r.get("drivers", {}).items():
        for cname, cdata in dinfo.get("components", {}).items():
            ws.cell(row=row, column=1, value=dname)
            ws.cell(row=row, column=2, value=cname)
            ws.cell(row=row, column=3, value=cdata["value"])
            ws.cell(row=row, column=4, value="")
            ws.cell(row=row, column=5, value=cdata.get("confidence", 0.5))
            ws.cell(row=row, column=5).number_format = '0%'
            row += 1
        # Driver total row
        ws.cell(row=row, column=1, value=dname)
        ws.cell(row=row, column=2, value="TOTAL")
        ws.cell(row=row, column=3, value=dinfo["value"])
        ws.cell(row=row, column=1).font = Font(bold=True)
        ws.cell(row=row, column=2).font = Font(bold=True)
        ws.cell(row=row, column=3).font = Font(bold=True)
        row += 1

    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 25
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 8
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 50


def _build_sensitivity_tab(ws, r):
    """Sensitivity analysis table."""
    ws.cell(row=1, column=1, value=f"{r['ticker']} Sensitivity Analysis").font = Font(bold=True, size=14)

    _header_row(ws, 3, ["Driver", "Component", "EPS Impact", "Per Unit", "Confidence", "Exposure"])

    for i, s in enumerate(r.get("sensitivities", []), 4):
        ws.cell(row=i, column=1, value=s["driver"])
        ws.cell(row=i, column=2, value=s["component"])
        ws.cell(row=i, column=3, value=s["eps_impact"])
        ws.cell(row=i, column=3).number_format = '$#,##0.0000'
        ws.cell(row=i, column=4, value=s.get("perturbation_label", ""))
        ws.cell(row=i, column=5, value=s.get("confidence", 0))
        ws.cell(row=i, column=5).number_format = '0%'
        ws.cell(row=i, column=6, value=s.get("exposure", 0))
        ws.cell(row=i, column=6).number_format = '0.0000'

    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 25
    ws.column_dimensions["C"].width = 15
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 12


def _build_adversarial_tab(ws, r):
    """Adversarial review: contradictions + revisions."""
    ws.cell(row=1, column=1, value=f"{r['ticker']} Adversarial Review").font = Font(bold=True, size=14)

    contras = r.get("contradictions", [])

    # Coverage
    cc = r.get("contradiction_coverage", {})
    if cc:
        ws.cell(row=3, column=1, value="Coverage")
        ws.cell(row=3, column=2, value=f"{len(cc.get('covered',[]))}/{len(cc.get('key_assumptions',[]))} assumptions challenged")
        ws.cell(row=3, column=2).font = Font(bold=True)

    # Contradictions table
    row = 5
    _header_row(ws, row, ["Severity", "Thesis / Bull Case", "Counter Evidence", "Affected Driver"])
    row += 1
    for c in contras:
        ws.cell(row=row, column=1, value=c.get("severity", ""))
        sev = c.get("severity", "")
        if sev == "serious":
            ws.cell(row=row, column=1).font = Font(bold=True, color="CC0000")
        ws.cell(row=row, column=2, value=c.get("thesis", "")[:100])
        ws.cell(row=row, column=3, value=c.get("counter_evidence", "")[:100])
        ws.cell(row=row, column=4, value=c.get("affected_driver", ""))
        row += 1

    # Revision traces
    traces = r.get("traces", [])
    if traces:
        row += 1
        ws.cell(row=row, column=1, value="REVISIONS APPLIED").font = Font(bold=True, size=12)
        row += 1
        _header_row(ws, row, ["Chain", "", "", ""])
        row += 1
        for t in traces:
            ws.cell(row=row, column=1, value=t.get("chain", ""))
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            row += 1

    # Adversarial Claude assessment
    adv = r.get("adversarial_response") or {}
    if adv.get("overall_assessment"):
        row += 1
        ws.cell(row=row, column=1, value="INDEPENDENT ADVERSARIAL ASSESSMENT").font = Font(bold=True, size=12)
        row += 1
        ws.cell(row=row, column=1, value=adv["overall_assessment"])
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)

    if adv.get("blind_spots"):
        row += 1
        ws.cell(row=row, column=1, value="Blind Spots:").font = Font(bold=True)
        for b in adv["blind_spots"]:
            row += 1
            ws.cell(row=row, column=1, value=f"  - {b}")

    ws.column_dimensions["A"].width = 15
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 25


def _build_edge_tab(ws, r):
    """Edge detection results."""
    ea = r.get("edge_assessment") or {}
    ws.cell(row=1, column=1, value=f"{r['ticker']} Edge Detection").font = Font(bold=True, size=14)

    row = 3
    fields = [
        ("Verdict", ea.get("verdict", "N/A")),
        ("Actionability Score", ea.get("actionability_score", 0)),
        ("Variant EPS", f"${ea.get('variant_eps',0):+.2f}"),
        ("Variant %", f"{ea.get('variant_pct',0):+.1f}%"),
        ("Time Horizon", ea.get("time_horizon", "")),
    ]
    for label, val in fields:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=val)
        row += 1

    # Variant drivers table
    variants = ea.get("variants", [])
    if variants:
        row += 1
        _header_row(ws, row, ["Driver", "Component", "Our Value", "Street Value", "Delta", "EPS Contribution", "% of Variant", "Source"])
        row += 1
        for var in variants:
            ws.cell(row=row, column=1, value=var["driver"])
            ws.cell(row=row, column=2, value=var["component"])
            ws.cell(row=row, column=3, value=var["our_value"])
            ws.cell(row=row, column=4, value=round(var["consensus_value"], 2))
            ws.cell(row=row, column=5, value=round(var["delta"], 2))
            ws.cell(row=row, column=6, value=round(var["eps_contribution"], 4))
            ws.cell(row=row, column=6).number_format = '$#,##0.0000'
            ws.cell(row=row, column=7, value=f"{var.get('pct_of_total',0):.1f}%")
            ws.cell(row=row, column=8, value=var.get("source", ""))
            row += 1

    # Catalysts
    catalysts = ea.get("catalysts", [])
    if catalysts:
        row += 1
        ws.cell(row=row, column=1, value="CATALYSTS").font = Font(bold=True, size=12)
        row += 1
        _header_row(ws, row, ["Event", "Timeframe", "Resolves Driver", "Impact"])
        row += 1
        for c in catalysts:
            ws.cell(row=row, column=1, value=c.get("event", "")[:60])
            ws.cell(row=row, column=2, value=c.get("timeframe", ""))
            ws.cell(row=row, column=3, value=c.get("resolves_driver", ""))
            ws.cell(row=row, column=4, value=c.get("impact", ""))
            row += 1

    # Narrative
    if ea.get("edge_narrative"):
        row += 1
        ws.cell(row=row, column=1, value=ea["edge_narrative"][:500])
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)

    for col in "ABCDEFGH":
        ws.column_dimensions[col].width = 18


def _build_valuation_tab(ws, r):
    """Valuation analysis."""
    val = r.get("valuation") or {}
    ws.cell(row=1, column=1, value=f"{r['ticker']} Valuation").font = Font(bold=True, size=14)

    if not val.get("implied_price"):
        ws.cell(row=3, column=1, value="No valuation data available (negative EPS or missing market data)")
        return

    row = 3
    fields = [
        ("Implied Price", f"${val['implied_price']:.2f}"),
        ("Current Price", f"${val.get('current_price',0):.2f}"),
        ("Upside/Downside", f"{val['upside_pct']:+.1f}%"),
        ("Applied Multiple", f"{val['applied_multiple']:.1f}x"),
        ("Multiple Source", val.get("multiple_source", "")),
    ]
    for label, value in fields:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        if "Upside" in label:
            ws.cell(row=row, column=2).font = Font(bold=True,
                color="008000" if val["upside_pct"] > 0 else "CC0000")
        row += 1

    # Sensitivity table
    sens = val.get("sensitivity", {})
    if sens:
        row += 1
        _header_row(ws, row, ["Scenario", "PE Multiple", "Implied Price", "Upside"])
        row += 1
        scenarios = [
            ("Bear", sens.get("low_pe",0), sens.get("price_at_low",0), sens.get("upside_at_low",0)),
            ("Base", val["applied_multiple"], val["implied_price"], val["upside_pct"]),
            ("Bull", sens.get("high_pe",0), sens.get("price_at_high",0), sens.get("upside_at_high",0)),
        ]
        for name, pe, price, upside in scenarios:
            ws.cell(row=row, column=1, value=name)
            ws.cell(row=row, column=2, value=f"{pe:.1f}x")
            ws.cell(row=row, column=3, value=f"${price:.2f}")
            ws.cell(row=row, column=4, value=f"{upside:+.1f}%")
            if name == "Base":
                for col in range(1, 5):
                    ws.cell(row=row, column=col).font = Font(bold=True)
            row += 1

    if val.get("narrative"):
        row += 1
        ws.cell(row=row, column=1, value=val["narrative"][:400])
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 20


# ═══════════════════════════════════════════════════════════════
# Financial Model Export (7-tab 3-statement model)
# ═══════════════════════════════════════════════════════════════

def export_financial_model(inputs: "ModelInputs", filepath: str = None) -> str:
    """
    Export a fully linked 7-tab 3-statement financial model.

    This is separate from the research export (which generates analyst workpapers).
    The financial model is a live, formula-driven workbook with:
      - IS/BS/CF linked via formulas
      - CHOOSE(CASE, bear, base, bull) scenario toggles in Drivers
      - Cash-as-plug balance sheet
      - DCF with WACC build-up and sensitivity tables
      - Comparable companies analysis

    Args:
        inputs: ModelInputs from research.financial_model
        filepath: Output path (auto-generated if None)

    Returns:
        Path to saved .xlsx file
    """
    if not HAS_OPENPYXL:
        raise ImportError("openpyxl required")
    if not HAS_FINANCIAL_MODEL:
        raise ImportError("research.financial_model required")

    if filepath is None:
        export_dir = Path("exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = str(export_dir / f"{inputs.ticker}_model_{ts}.xlsx")

    wb = build_financial_model(inputs)
    wb.save(filepath)
    return filepath


def export_full_package(result: dict, model_inputs: "ModelInputs" = None,
                        filepath_prefix: str = None) -> dict:
    """
    Export both research workpapers AND financial model as separate files.

    Returns dict with paths:
        {"research": "CMG_research_20260331.xlsx",
         "model": "CMG_model_20260331.xlsx"}
    """
    paths = {}

    # Research workpapers (existing 6-tab format)
    research_path = export_excel(result, filepath_prefix + "_research.xlsx" if filepath_prefix else None)
    paths["research"] = research_path

    # Financial model (new 7-tab format)
    if model_inputs and HAS_FINANCIAL_MODEL:
        model_path = export_financial_model(
            model_inputs,
            filepath_prefix + "_model.xlsx" if filepath_prefix else None
        )
        paths["model"] = model_path

    return paths
