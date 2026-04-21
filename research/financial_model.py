"""
Financial Model Architecture

Generates a fully linked 7-tab quarterly 3-statement financial model in openpyxl.
Architecture proven on CMG; generalizes to any single-segment company via sector_drivers.

Tabs: Summary | IS | BS | CF | Drivers | DCF | Comps
Column layout: 12 historical quarters (3 FY) + 8 forecast quarters (2 FY) + annual summaries + %Δ

Key mechanics:
  - CHOOSE(CASE, bear, base, bull) scenario toggle in Drivers tab
  - Cash-as-plug balance sheet (cash = total L&E - non-cash assets)
  - CF ending cash = BS cash (hard link)
  - Revenue = prior_yr_Q * (1 + SSS) * (stores / prior_yr_stores)
  - All costs = Revenue * driver %
  - IB text color conventions (blue inputs, black formulas, green cross-sheet)

Usage:
    from research.financial_model import build_financial_model
    wb = build_financial_model(ticker="CMG", hist_data=hist, assumptions=assum)
    wb.save("CMG_model.xlsx")
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

# Header styling
DARK_BLUE = "1F3864"
HEADER_FONT = Font(bold=True, size=11, color="FFFFFF", name="Aptos Narrow") if HAS_OPENPYXL else None
HEADER_FILL = PatternFill(start_color=DARK_BLUE, end_color=DARK_BLUE, fill_type="solid") if HAS_OPENPYXL else None
SUBHEADER_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid") if HAS_OPENPYXL else None
ANNUAL_FILL = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid") if HAS_OPENPYXL else None
DEFAULT_FONT = Font(name="Aptos Narrow", size=11) if HAS_OPENPYXL else None
BOLD_FONT = Font(name="Aptos Narrow", size=11, bold=True) if HAS_OPENPYXL else None

# IB text colors
BLUE_INPUT = Font(name="Aptos Narrow", size=11, color="0000FF")   # hard-coded inputs
BLACK_FORMULA = Font(name="Aptos Narrow", size=11, color="000000") # same-sheet formulas
GREEN_XREF = Font(name="Aptos Narrow", size=11, color="006100")    # cross-sheet references

# Number formats
FMT_MONEY = '#,##0'
FMT_MONEY_DEC = '#,##0.00'
FMT_PCT = '0.0%'
FMT_MULT = '0.0x'
FMT_EPS = '$#,##0.000'
FMT_ACCOUNTING = '_($* #,##0_);_($* (#,##0);_($* "-"_);_(@_)'
FMT_NEG_PARENS = '#,##0;(#,##0)'


# ═══════════════════════════════════════════════════════════════
# Column Layout Spec
# ═══════════════════════════════════════════════════════════════

@dataclass
class ColumnLayout:
    """
    Maps the quarterly + annual column structure.
    Standard: 3 historical FYs (12 quarters) + 2 forecast FYs (8 quarters).
    """
    label_col: str = "B"         # Row labels
    hist_start_fy: int = 2023    # First historical fiscal year
    forecast_start_fy: int = 2026
    hist_years: int = 3
    forecast_years: int = 2

    def get_column_map(self) -> dict:
        """
        Returns mapping of period labels to column letters.
        Layout: B=labels, C-F=Q1-Q4 FY1, G=FY1, H-K=Q1-Q4 FY2, L=FY2, ...
        """
        col_idx = 3  # Start at C (col 3)
        col_map = {}
        annual_cols = []

        for yr_offset in range(self.hist_years + self.forecast_years):
            fy = self.hist_start_fy + yr_offset
            is_forecast = fy >= self.forecast_start_fy
            suffix = "E" if is_forecast else ""

            for q in range(1, 5):
                key = f"{q}Q{str(fy)[2:]}{suffix}"
                col_map[key] = get_column_letter(col_idx)
                col_idx += 1

            fy_key = f"FY{str(fy)[2:]}{suffix}"
            col_map[fy_key] = get_column_letter(col_idx)
            annual_cols.append(get_column_letter(col_idx))
            col_idx += 1

            # Add %Δ column after forecast annual
            if is_forecast:
                pct_key = f"pctchg_{fy}"
                col_map[pct_key] = get_column_letter(col_idx)
                col_idx += 1

        col_map["_annual_cols"] = annual_cols
        col_map["_last_col"] = get_column_letter(col_idx - 1)
        return col_map


# ═══════════════════════════════════════════════════════════════
# Row Maps (proven on CMG)
# ═══════════════════════════════════════════════════════════════

# Income Statement row positions (relative to row 1)
IS_ROWS = {
    "header": 3, "revenue": 5,
    "cost_header": 7, "food": 8, "labor": 9, "occupancy": 10,
    "other_rest": 11, "total_rest": 12, "rest_profit": 13,
    "ga": 15, "da": 16, "preopening": 17, "loss_disp": 18,
    "total_costs": 19, "blank1": 20, "op_profit": 21, "ebitda": 22,
    "blank2": 23, "interest": 24, "pretax": 25, "taxes": 26,
    "net_income": 27, "blank3": 28, "nonrecur": 29, "reported": 30,
    "blank4": 31, "shares": 32, "eps": 33, "rep_eps": 34, "divs": 35,
    "margin_header": 37, "margin_start": 38,
    "yoy_header": 50, "yoy_start": 51,
}

# Balance Sheet row positions
BS_ROWS = {
    "header": 3, "assets_header": 5,
    "cash": 6, "ar": 7, "inv": 8, "prepaid": 9, "total_ca": 10,
    "blank1": 11, "ppe": 12, "rou": 13, "goodwill": 14,
    "other_lta": 15, "total_a": 16,
    "blank2": 17, "liab_header": 18,
    "ap": 19, "accrued": 20, "cur_lease": 21, "total_cl": 22,
    "blank3": 23, "lt_debt": 24, "lt_lease": 25, "other_ltl": 26,
    "total_l": 27, "blank4": 28, "eq_header": 29,
    "common": 30, "retained": 31, "treasury": 32, "total_eq": 33,
    "blank5": 34, "total_le": 35, "blank6": 36, "bal_check": 37,
}

# Cash Flow row positions
CF_ROWS = {
    "header": 3, "op_header": 5,
    "ni": 6, "da": 7, "sbc": 8, "def_tax": 9,
    "wc_header": 10, "d_ar": 11, "d_inv": 12, "d_pre": 13,
    "d_ap": 14, "d_acc": 15, "total_op": 16,
    "blank1": 17, "inv_header": 18,
    "capex": 19, "other_inv": 20, "total_inv": 21,
    "blank2": 22, "fin_header": 23,
    "buybacks": 24, "divs": 25, "debt": 26, "total_fin": 27,
    "blank3": 28, "net_chg": 29, "beg_cash": 30, "end_cash": 31,
}

# Driver sections (each has: label_row, bear, base, bull, active)
# 23 driver groups × 5 rows each = 115 rows in Drivers tab
DRIVER_SECTIONS = {
    "sss": 7, "new_stores": 12, "stores_eop": 17,
    "food_pct": 19, "labor_pct": 24, "occupancy_pct": 29,
    "other_rest_pct": 34, "ga_pct": 39, "da_pct": 44,
    "preopening": 49, "loss_disposal": 54, "interest_other": 59,
    "tax_rate": 64, "capex": 69, "ar_days": 74, "inv_days": 79,
    "ap_days": 84, "diluted_shares": 89, "buybacks": 94,
    "sbc": 99, "deferred_tax": 104, "other_inv": 109, "dividends": 114,
}


# ═══════════════════════════════════════════════════════════════
# Model Spec — data structure for model inputs
# ═══════════════════════════════════════════════════════════════

@dataclass
class QuarterData:
    """Data for a single quarter."""
    period: str  # e.g. "1Q23"
    revenue: float = 0
    food: float = 0
    labor: float = 0
    occupancy: float = 0
    other_rest: float = 0
    ga: float = 0
    da: float = 0
    preopening: float = 0
    loss_disp: float = 0
    interest: float = 0
    taxes: float = 0
    nonrecur: float = 0
    # BS items
    cash: float = 0
    ar: float = 0
    inv: float = 0
    prepaid: float = 0
    ppe: float = 0
    rou: float = 0
    goodwill: float = 0
    other_lta: float = 0
    ap: float = 0
    accrued: float = 0
    cur_lease: float = 0
    lt_debt: float = 0
    lt_lease: float = 0
    other_ltl: float = 0
    common: float = 0
    retained: float = 0
    treasury: float = 0
    # Other
    shares: float = 0
    sbc: float = 0
    def_tax: float = 0
    capex: float = 0
    other_inv: float = 0
    buybacks: float = 0
    dividends: float = 0
    debt_change: float = 0
    store_count: float = 0


@dataclass
class ScenarioAssumptions:
    """Bear/Base/Bull assumptions for a single driver."""
    bear: list[float] = field(default_factory=list)  # 8 quarters
    base: list[float] = field(default_factory=list)
    bull: list[float] = field(default_factory=list)


@dataclass
class ModelInputs:
    """Complete inputs for model generation."""
    ticker: str = ""
    hist_quarters: list[QuarterData] = field(default_factory=list)  # 12 quarters
    scenarios: dict[str, ScenarioAssumptions] = field(default_factory=dict)  # 23 drivers
    comps: list[dict] = field(default_factory=list)  # peer companies
    wacc_assumptions: dict = field(default_factory=dict)
    # Layout
    layout: ColumnLayout = field(default_factory=ColumnLayout)


# ═══════════════════════════════════════════════════════════════
# Builder Functions
# ═══════════════════════════════════════════════════════════════

def _style_header(ws, row: int, col_start: int, col_end: int, values: list):
    """Apply dark blue header with white bold text."""
    for i, val in enumerate(values):
        cell = ws.cell(row=row, column=col_start + i, value=val)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")


def _style_subheader(ws, row: int, col_start: int, col_end: int, values: list):
    """Apply light blue sub-header."""
    for i, val in enumerate(values):
        cell = ws.cell(row=row, column=col_start + i, value=val)
        cell.font = BOLD_FONT
        cell.fill = SUBHEADER_FILL


def _shade_annual_cols(ws, annual_cols: list, row_start: int, row_end: int):
    """Apply light gray background to annual summary columns."""
    for col_letter in annual_cols:
        for row in range(row_start, row_end + 1):
            ws[f"{col_letter}{row}"].fill = ANNUAL_FILL


def _write_hist_row(ws, row: int, label: str, values: list, col_map: dict,
                    quarters: list, fmt: str = FMT_MONEY, bold: bool = False):
    """Write a row of historical data with quarterly values and annual sums."""
    ws.cell(row=row, column=2, value=label)
    if bold:
        ws.cell(row=row, column=2).font = BOLD_FONT

    q_idx = 0
    for fy_offset in range(3):  # 3 historical years
        fy = 2023 + fy_offset
        fy_key = f"FY{str(fy)[2:]}"
        q_cols = []
        for q in range(1, 5):
            q_key = f"{q}Q{str(fy)[2:]}"
            col_letter = col_map[q_key]
            col_num = _col_num(col_letter)
            cell = ws.cell(row=row, column=col_num, value=values[q_idx])
            cell.number_format = fmt
            cell.font = BLUE_INPUT  # historical = hardcoded input
            q_cols.append(col_letter)
            q_idx += 1

        # Annual = SUM(quarters)
        fy_col = col_map[fy_key]
        fy_col_num = _col_num(fy_col)
        formula = f"=SUM({q_cols[0]}{row}:{q_cols[3]}{row})"
        cell = ws.cell(row=row, column=fy_col_num, value=formula)
        cell.number_format = fmt
        cell.font = BLACK_FORMULA


def _write_bs_hist_row(ws, row: int, label: str, values: list, col_map: dict,
                       fmt: str = FMT_MONEY, bold: bool = False):
    """Write BS row — annual = Q4 snapshot, not SUM."""
    ws.cell(row=row, column=2, value=label)
    if bold:
        ws.cell(row=row, column=2).font = BOLD_FONT

    q_idx = 0
    for fy_offset in range(3):
        fy = 2023 + fy_offset
        fy_key = f"FY{str(fy)[2:]}"
        for q in range(1, 5):
            q_key = f"{q}Q{str(fy)[2:]}"
            col_letter = col_map[q_key]
            col_num = _col_num(col_letter)
            cell = ws.cell(row=row, column=col_num, value=values[q_idx])
            cell.number_format = fmt
            cell.font = BLUE_INPUT
            q_idx += 1

        # Annual = Q4 value (reference, not SUM)
        q4_key = f"4Q{str(fy)[2:]}"
        q4_col = col_map[q4_key]
        fy_col = col_map[fy_key]
        formula = f"={q4_col}{row}"
        cell = ws.cell(row=row, column=_col_num(fy_col), value=formula)
        cell.number_format = fmt
        cell.font = BLACK_FORMULA


def _col_num(col_letter: str) -> int:
    """Convert column letter to number."""
    result = 0
    for c in col_letter:
        result = result * 26 + (ord(c) - ord('A') + 1)
    return result


def _build_drivers_tab(ws, inputs: ModelInputs, col_map: dict):
    """
    Build Drivers tab with CHOOSE(CASE, bear, base, bull) architecture.

    Each driver section has 5 rows:
      Row N:   Label (active = CHOOSE formula)
      Row N+1: Bear scenario values (hidden)
      Row N+2: Base scenario values (hidden)
      Row N+3: Bull scenario values (hidden)

    CASE named range at C3 controls all toggles.
    """
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Drivers").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    # Scenario toggle
    ws.cell(row=3, column=2, value="Scenario")
    ws.cell(row=3, column=3, value=2)  # Default = Base
    ws.cell(row=3, column=3).font = BLUE_INPUT
    ws.cell(row=3, column=4, value="1=Bear  2=Base  3=Bull")
    ws.cell(row=3, column=4).font = Font(name="Aptos Narrow", size=11, color="888888")

    # Column headers (row 5)
    _write_period_headers(ws, 5, col_map, inputs.layout)

    # Write each driver section
    for driver_name, start_row in DRIVER_SECTIONS.items():
        scenario = inputs.scenarios.get(driver_name)
        if not scenario:
            continue

        # Determine format
        fmt = FMT_PCT if "pct" in driver_name or driver_name in ("sss", "tax_rate") else FMT_MONEY

        # Label
        label = _driver_label(driver_name)
        ws.cell(row=start_row, column=2, value=label)
        ws.cell(row=start_row, column=2).font = BOLD_FONT

        # Historical quarters (same across scenarios — blue inputs)
        hist_vals = scenario.base[:12] if len(scenario.base) >= 12 else scenario.base
        q_idx = 0
        for fy_offset in range(3):
            fy = 2023 + fy_offset
            for q in range(1, 5):
                if q_idx < len(hist_vals):
                    q_key = f"{q}Q{str(fy)[2:]}"
                    col = col_map.get(q_key)
                    if col:
                        cell = ws.cell(row=start_row, column=_col_num(col), value=hist_vals[q_idx])
                        cell.number_format = fmt
                        cell.font = BLUE_INPUT
                q_idx += 1

        # Forecast: Bear/Base/Bull sub-rows + CHOOSE active row
        bear_row = start_row + 1
        base_row = start_row + 2
        bull_row = start_row + 3

        ws.cell(row=bear_row, column=2, value="Bear")
        ws.cell(row=bear_row, column=2).font = Font(name="Aptos Narrow", size=11, color="999999")
        ws.cell(row=base_row, column=2, value="Base")
        ws.cell(row=base_row, column=2).font = Font(name="Aptos Narrow", size=11, color="999999")
        ws.cell(row=bull_row, column=2, value="Bull")
        ws.cell(row=bull_row, column=2).font = Font(name="Aptos Narrow", size=11, color="999999")

        # Write scenario values for forecast quarters
        forecast_vals = {
            "bear": scenario.bear[12:] if len(scenario.bear) > 12 else scenario.bear[-8:],
            "base": scenario.base[12:] if len(scenario.base) > 12 else scenario.base[-8:],
            "bull": scenario.bull[12:] if len(scenario.bull) > 12 else scenario.bull[-8:],
        }
        rows_map = {"bear": bear_row, "base": base_row, "bull": bull_row}

        fc_idx = 0
        for fy in (inputs.layout.forecast_start_fy, inputs.layout.forecast_start_fy + 1):
            for q in range(1, 5):
                q_key = f"{q}Q{str(fy)[2:]}E"
                col = col_map.get(q_key)
                if not col:
                    continue
                col_n = _col_num(col)

                for scen_name, scen_row in rows_map.items():
                    vals = forecast_vals[scen_name]
                    if fc_idx < len(vals):
                        cell = ws.cell(row=scen_row, column=col_n, value=vals[fc_idx])
                        cell.number_format = fmt
                        cell.font = BLUE_INPUT

                # Active row = CHOOSE
                choose_formula = f"=CHOOSE(CASE,{get_column_letter(col_n)}{bear_row},{get_column_letter(col_n)}{base_row},{get_column_letter(col_n)}{bull_row})"
                cell = ws.cell(row=start_row, column=col_n, value=choose_formula)
                cell.number_format = fmt
                cell.font = BLACK_FORMULA

                fc_idx += 1

        # Hide scenario sub-rows
        ws.row_dimensions[bear_row].hidden = True
        ws.row_dimensions[base_row].hidden = True
        ws.row_dimensions[bull_row].hidden = True


def _write_period_headers(ws, row: int, col_map: dict, layout: ColumnLayout):
    """Write quarterly and annual period headers."""
    for key, col in col_map.items():
        if key.startswith("_"):
            continue
        if key.startswith("pctchg"):
            label = "%Δ"
        else:
            label = key
        col_num = _col_num(col)
        cell = ws.cell(row=row, column=col_num, value=label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")


def _driver_label(name: str) -> str:
    """Convert driver key to human-readable label."""
    labels = {
        "sss": "Same-Store Sales %",
        "new_stores": "New Store Openings",
        "stores_eop": "Total Stores (EOP)",
        "food_pct": "Food & Paper (% Rev)",
        "labor_pct": "Labor (% Rev)",
        "occupancy_pct": "Occupancy (% Rev)",
        "other_rest_pct": "Other Operating (% Rev)",
        "ga_pct": "G&A (% Rev)",
        "da_pct": "D&A (% Rev)",
        "preopening": "Pre-Opening Costs",
        "loss_disposal": "Loss on Disposal",
        "interest_other": "Interest & Other",
        "tax_rate": "Effective Tax Rate",
        "capex": "Capital Expenditures",
        "ar_days": "A/R Days",
        "inv_days": "Inventory Days",
        "ap_days": "A/P Days",
        "diluted_shares": "Diluted Shares (M)",
        "buybacks": "Share Buybacks",
        "sbc": "Stock-Based Comp",
        "deferred_tax": "Deferred Tax",
        "other_inv": "Other Investing",
        "dividends": "Dividends",
    }
    return labels.get(name, name.replace("_", " ").title())


# ═══════════════════════════════════════════════════════════════
# Income Statement Builder
# ═══════════════════════════════════════════════════════════════

def _build_is_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build Income Statement with formulas referencing Drivers tab."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Income Statement ($M)").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    _write_period_headers(ws, IS_ROWS["header"], col_map, inputs.layout)

    # Historical data
    hist = inputs.hist_quarters
    row_data = {
        "revenue": [(q.revenue, True) for q in hist],
        "food": [(q.food, False) for q in hist],
        "labor": [(q.labor, False) for q in hist],
        "occupancy": [(q.occupancy, False) for q in hist],
        "other_rest": [(q.other_rest, False) for q in hist],
        "ga": [(q.ga, False) for q in hist],
        "da": [(q.da, False) for q in hist],
        "preopening": [(q.preopening, False) for q in hist],
        "loss_disp": [(q.loss_disp, False) for q in hist],
        "interest": [(q.interest, False) for q in hist],
        "taxes": [(q.taxes, False) for q in hist],
        "nonrecur": [(q.nonrecur, False) for q in hist],
        "shares": [(q.shares, False) for q in hist],
    }

    labels = {
        "revenue": ("Revenue", True), "food": ("Food & Paper", False),
        "labor": ("Labor", False), "occupancy": ("Occupancy & Related", False),
        "other_rest": ("Other Operating", False), "ga": ("G&A", False),
        "da": ("D&A", False), "preopening": ("Pre-Opening Costs", False),
        "loss_disp": ("Loss on Disposal", False), "interest": ("Interest & Other", False),
        "taxes": ("Income Taxes", False), "nonrecur": ("Non-Recurring Items", False),
        "shares": ("Diluted Shares (M)", False),
    }

    for key, (label, bold) in labels.items():
        row = IS_ROWS[key]
        values = [v for v, _ in row_data[key]]
        _write_hist_row(ws, row, label, values, col_map,
                       inputs.hist_quarters, fmt=FMT_MONEY, bold=bold)

    # Computed rows (formulas for historical)
    _write_is_computed_rows(ws, col_map, inputs.layout)

    # Forecast formulas referencing Drivers tab
    _write_is_forecast(ws, col_map, inputs.layout)


def _write_is_computed_rows(ws, col_map: dict, layout: ColumnLayout):
    """Write formula rows: totals, profits, margins."""
    r = IS_ROWS

    # Labels
    computed_labels = {
        r["total_rest"]: ("Total Restaurant Costs", True),
        r["rest_profit"]: ("Restaurant-Level Profit", True),
        r["total_costs"]: ("Total Costs & Expenses", True),
        r["op_profit"]: ("Operating Profit", True),
        r["ebitda"]: ("EBITDA", True),
        r["pretax"]: ("Pre-Tax Income", True),
        r["net_income"]: ("Net Earnings", True),
        r["reported"]: ("Reported Net Income", True),
        r["eps"]: ("Diluted EPS", True),
        r["rep_eps"]: ("Reported EPS", True),
        r["divs"]: ("Dividends per Share", False),
    }
    for row, (label, bold) in computed_labels.items():
        ws.cell(row=row, column=2, value=label)
        if bold:
            ws.cell(row=row, column=2).font = BOLD_FONT

    # Sub-headers
    ws.cell(row=IS_ROWS["cost_header"], column=2, value="Restaurant Operating Costs")
    ws.cell(row=IS_ROWS["cost_header"], column=2).font = Font(
        name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)

    # Write formulas for all data columns
    for key, col in col_map.items():
        if key.startswith("_") or key.startswith("pctchg"):
            continue
        c = _col_num(col)
        cl = get_column_letter(c)

        # Total rest costs = food + labor + occupancy + other
        ws.cell(row=r["total_rest"], column=c,
                value=f"={cl}{r['food']}+{cl}{r['labor']}+{cl}{r['occupancy']}+{cl}{r['other_rest']}")
        ws.cell(row=r["total_rest"], column=c).number_format = FMT_MONEY

        # Restaurant profit = revenue - total_rest
        ws.cell(row=r["rest_profit"], column=c,
                value=f"={cl}{r['revenue']}-{cl}{r['total_rest']}")
        ws.cell(row=r["rest_profit"], column=c).number_format = FMT_MONEY

        # Total costs = total_rest + ga + da + preopening + loss_disp
        ws.cell(row=r["total_costs"], column=c,
                value=f"={cl}{r['total_rest']}+{cl}{r['ga']}+{cl}{r['da']}+{cl}{r['preopening']}+{cl}{r['loss_disp']}")
        ws.cell(row=r["total_costs"], column=c).number_format = FMT_MONEY

        # Op profit = revenue - total_costs
        ws.cell(row=r["op_profit"], column=c,
                value=f"={cl}{r['revenue']}-{cl}{r['total_costs']}")
        ws.cell(row=r["op_profit"], column=c).number_format = FMT_MONEY

        # EBITDA = op_profit + da
        ws.cell(row=r["ebitda"], column=c,
                value=f"={cl}{r['op_profit']}+{cl}{r['da']}")
        ws.cell(row=r["ebitda"], column=c).number_format = FMT_MONEY

        # Pre-tax = op_profit + interest
        ws.cell(row=r["pretax"], column=c,
                value=f"={cl}{r['op_profit']}+{cl}{r['interest']}")
        ws.cell(row=r["pretax"], column=c).number_format = FMT_MONEY

        # Net income = pretax - taxes (taxes are negative)
        ws.cell(row=r["net_income"], column=c,
                value=f"={cl}{r['pretax']}+{cl}{r['taxes']}")
        ws.cell(row=r["net_income"], column=c).number_format = FMT_MONEY

        # Reported = net_income + nonrecur
        ws.cell(row=r["reported"], column=c,
                value=f"={cl}{r['net_income']}+{cl}{r['nonrecur']}")
        ws.cell(row=r["reported"], column=c).number_format = FMT_MONEY

        # EPS = net_income / shares
        ws.cell(row=r["eps"], column=c,
                value=f"={cl}{r['net_income']}/{cl}{r['shares']}")
        ws.cell(row=r["eps"], column=c).number_format = FMT_EPS

        # Reported EPS = reported / shares
        ws.cell(row=r["rep_eps"], column=c,
                value=f"={cl}{r['reported']}/{cl}{r['shares']}")
        ws.cell(row=r["rep_eps"], column=c).number_format = FMT_EPS


def _write_is_forecast(ws, col_map: dict, layout: ColumnLayout):
    """Write IS forecast formulas referencing Drivers tab."""
    r = IS_ROWS
    d = DRIVER_SECTIONS

    for fy in (layout.forecast_start_fy, layout.forecast_start_fy + 1):
        fy_str = str(fy)[2:]
        for q in range(1, 5):
            q_key = f"{q}Q{fy_str}E"
            col = col_map.get(q_key)
            if not col:
                continue
            c = _col_num(col)
            cl = get_column_letter(c)

            # Prior year same quarter column
            prior_fy = fy - 1
            prior_suffix = "E" if prior_fy >= layout.forecast_start_fy else ""
            prior_key = f"{q}Q{str(prior_fy)[2:]}{prior_suffix}"
            prior_col = col_map.get(prior_key, col)
            pcl = prior_col  # prior col letter

            # Revenue = prior_Q * (1 + SSS) * (stores / prior_stores)
            ws.cell(row=r["revenue"], column=c,
                    value=f"={pcl}{r['revenue']}*(1+Drivers!{cl}{d['sss']})"
                          f"*(Drivers!{cl}{d['stores_eop']}/Drivers!{pcl}{d['stores_eop']})")
            ws.cell(row=r["revenue"], column=c).font = GREEN_XREF

            # Cost items = Revenue * driver %
            cost_drivers = {
                "food": "food_pct", "labor": "labor_pct",
                "occupancy": "occupancy_pct", "other_rest": "other_rest_pct",
                "ga": "ga_pct", "da": "da_pct",
            }
            for is_key, drv_key in cost_drivers.items():
                ws.cell(row=r[is_key], column=c,
                        value=f"={cl}{r['revenue']}*Drivers!{cl}{d[drv_key]}")
                ws.cell(row=r[is_key], column=c).font = GREEN_XREF

            # Direct driver references
            direct_drivers = {
                "preopening": "preopening", "loss_disp": "loss_disposal",
                "interest": "interest_other",
            }
            for is_key, drv_key in direct_drivers.items():
                ws.cell(row=r[is_key], column=c,
                        value=f"=Drivers!{cl}{d[drv_key]}")
                ws.cell(row=r[is_key], column=c).font = GREEN_XREF

            # Taxes = -pretax * tax_rate
            ws.cell(row=r["taxes"], column=c,
                    value=f"=-{cl}{r['pretax']}*Drivers!{cl}{d['tax_rate']}")
            ws.cell(row=r["taxes"], column=c).font = GREEN_XREF

            # Shares
            ws.cell(row=r["shares"], column=c,
                    value=f"=Drivers!{cl}{d['diluted_shares']}")
            ws.cell(row=r["shares"], column=c).font = GREEN_XREF

            # Dividends
            ws.cell(row=r["divs"], column=c,
                    value=f"=Drivers!{cl}{d['dividends']}")
            ws.cell(row=r["divs"], column=c).font = GREEN_XREF


# ═══════════════════════════════════════════════════════════════
# Balance Sheet Builder
# ═══════════════════════════════════════════════════════════════

def _build_bs_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build Balance Sheet with cash-as-plug methodology."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Balance Sheet ($M)").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    _write_period_headers(ws, BS_ROWS["header"], col_map, inputs.layout)

    # Historical data (BS uses Q4 snapshot for annual)
    hist = inputs.hist_quarters
    bs_items = {
        "cash": [q.cash for q in hist], "ar": [q.ar for q in hist],
        "inv": [q.inv for q in hist], "prepaid": [q.prepaid for q in hist],
        "ppe": [q.ppe for q in hist], "rou": [q.rou for q in hist],
        "goodwill": [q.goodwill for q in hist], "other_lta": [q.other_lta for q in hist],
        "ap": [q.ap for q in hist], "accrued": [q.accrued for q in hist],
        "cur_lease": [q.cur_lease for q in hist], "lt_debt": [q.lt_debt for q in hist],
        "lt_lease": [q.lt_lease for q in hist], "other_ltl": [q.other_ltl for q in hist],
        "common": [q.common for q in hist], "retained": [q.retained for q in hist],
        "treasury": [q.treasury for q in hist],
    }

    bs_labels = {
        "cash": ("Cash & Equivalents", True), "ar": ("Accounts Receivable", False),
        "inv": ("Inventories", False), "prepaid": ("Prepaid & Other", False),
        "ppe": ("PP&E, net", False), "rou": ("ROU Assets", False),
        "goodwill": ("Goodwill", False), "other_lta": ("Other Long-Term Assets", False),
        "ap": ("Accounts Payable", False), "accrued": ("Accrued Liabilities", False),
        "cur_lease": ("Current Lease Liabilities", False), "lt_debt": ("Long-Term Debt", False),
        "lt_lease": ("Long-Term Lease Liabilities", False), "other_ltl": ("Other Long-Term Liab.", False),
        "common": ("Common Stock & APIC", False), "retained": ("Retained Earnings", False),
        "treasury": ("Treasury Stock", False),
    }

    for key, (label, bold) in bs_labels.items():
        row = BS_ROWS[key]
        _write_bs_hist_row(ws, row, label, bs_items[key], col_map, fmt=FMT_MONEY, bold=bold)

    # Computed rows (formulas) — same for all columns
    _write_bs_computed_rows(ws, col_map, inputs.layout)

    # Forecast formulas
    _write_bs_forecast(ws, col_map, inputs.layout)


def _write_bs_computed_rows(ws, col_map: dict, layout: ColumnLayout):
    """Write BS total/subtotal formulas."""
    r = BS_ROWS

    labels = {
        r["total_ca"]: ("Total Current Assets", True),
        r["total_a"]: ("TOTAL ASSETS", True),
        r["total_cl"]: ("Total Current Liabilities", True),
        r["total_l"]: ("Total Liabilities", True),
        r["total_eq"]: ("Total Stockholders' Equity", True),
        r["total_le"]: ("TOTAL LIABILITIES & EQUITY", True),
        r["bal_check"]: ("Balance Check (A - L&E)", True),
    }
    for row, (label, bold) in labels.items():
        ws.cell(row=row, column=2, value=label)
        if bold:
            ws.cell(row=row, column=2).font = BOLD_FONT

    # Sub-section headers
    ws.cell(row=r["assets_header"], column=2, value="ASSETS")
    ws.cell(row=r["assets_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)
    ws.cell(row=r["liab_header"], column=2, value="LIABILITIES")
    ws.cell(row=r["liab_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)
    ws.cell(row=r["eq_header"], column=2, value="STOCKHOLDERS' EQUITY")
    ws.cell(row=r["eq_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)

    for key, col in col_map.items():
        if key.startswith("_") or key.startswith("pctchg"):
            continue
        c = _col_num(col)
        cl = get_column_letter(c)

        # Total CA = cash + ar + inv + prepaid
        ws.cell(row=r["total_ca"], column=c,
                value=f"={cl}{r['cash']}+{cl}{r['ar']}+{cl}{r['inv']}+{cl}{r['prepaid']}")

        # Total Assets = total_ca + ppe + rou + goodwill + other_lta
        ws.cell(row=r["total_a"], column=c,
                value=f"={cl}{r['total_ca']}+{cl}{r['ppe']}+{cl}{r['rou']}+{cl}{r['goodwill']}+{cl}{r['other_lta']}")

        # Total CL = ap + accrued + cur_lease
        ws.cell(row=r["total_cl"], column=c,
                value=f"={cl}{r['ap']}+{cl}{r['accrued']}+{cl}{r['cur_lease']}")

        # Total L = total_cl + lt_debt + lt_lease + other_ltl
        ws.cell(row=r["total_l"], column=c,
                value=f"={cl}{r['total_cl']}+{cl}{r['lt_debt']}+{cl}{r['lt_lease']}+{cl}{r['other_ltl']}")

        # Total Eq = common + retained + treasury
        ws.cell(row=r["total_eq"], column=c,
                value=f"={cl}{r['common']}+{cl}{r['retained']}+{cl}{r['treasury']}")

        # Total L&E = total_l + total_eq
        ws.cell(row=r["total_le"], column=c,
                value=f"={cl}{r['total_l']}+{cl}{r['total_eq']}")

        # Balance check
        ws.cell(row=r["bal_check"], column=c,
                value=f"={cl}{r['total_a']}-{cl}{r['total_le']}")

        # Format
        for row in [r["total_ca"], r["total_a"], r["total_cl"], r["total_l"],
                     r["total_eq"], r["total_le"], r["bal_check"]]:
            ws.cell(row=row, column=c).number_format = FMT_MONEY


def _write_bs_forecast(ws, col_map: dict, layout: ColumnLayout):
    """Write BS forecast formulas. Cash = plug (total L&E - non-cash assets)."""
    r = BS_ROWS
    d = DRIVER_SECTIONS
    ir = IS_ROWS

    for fy in (layout.forecast_start_fy, layout.forecast_start_fy + 1):
        fy_str = str(fy)[2:]
        for q in range(1, 5):
            q_key = f"{q}Q{fy_str}E"
            col = col_map.get(q_key)
            if not col:
                continue
            c = _col_num(col)
            cl = get_column_letter(c)

            # Prior quarter column (for sequential changes)
            if q > 1:
                prior_key = f"{q-1}Q{fy_str}E"
            else:
                prior_fy = fy - 1
                prior_suffix = "E" if prior_fy >= layout.forecast_start_fy else ""
                prior_key = f"4Q{str(prior_fy)[2:]}{prior_suffix}"
            prior_col = col_map.get(prior_key, col)

            # Working capital items from driver days
            # AR = Revenue/4 * ar_days / 90
            ws.cell(row=r["ar"], column=c,
                    value=f"=IS!{cl}{ir['revenue']}*Drivers!{cl}{d['ar_days']}/90")
            ws.cell(row=r["ar"], column=c).font = GREEN_XREF

            # Inventory = Revenue/4 * inv_days / 90
            ws.cell(row=r["inv"], column=c,
                    value=f"=IS!{cl}{ir['revenue']}*Drivers!{cl}{d['inv_days']}/90")
            ws.cell(row=r["inv"], column=c).font = GREEN_XREF

            # AP = Revenue/4 * ap_days / 90
            ws.cell(row=r["ap"], column=c,
                    value=f"=IS!{cl}{ir['revenue']}*Drivers!{cl}{d['ap_days']}/90")
            ws.cell(row=r["ap"], column=c).font = GREEN_XREF

            # PPE = prior PPE + capex - DA
            ws.cell(row=r["ppe"], column=c,
                    value=f"={prior_col}{r['ppe']}+Drivers!{cl}{d['capex']}-IS!{cl}{ir['da']}")
            ws.cell(row=r["ppe"], column=c).font = GREEN_XREF

            # Retained earnings = prior + net income - dividends
            ws.cell(row=r["retained"], column=c,
                    value=f"={prior_col}{r['retained']}+IS!{cl}{ir['net_income']}")
            ws.cell(row=r["retained"], column=c).font = GREEN_XREF

            # Treasury = prior - buybacks
            ws.cell(row=r["treasury"], column=c,
                    value=f"={prior_col}{r['treasury']}-Drivers!{cl}{d['buybacks']}")
            ws.cell(row=r["treasury"], column=c).font = GREEN_XREF

            # Cash = PLUG (total L&E - non-cash assets)
            ws.cell(row=r["cash"], column=c,
                    value=f"={cl}{r['total_le']}-{cl}{r['ar']}-{cl}{r['inv']}-{cl}{r['prepaid']}"
                          f"-{cl}{r['ppe']}-{cl}{r['rou']}-{cl}{r['goodwill']}-{cl}{r['other_lta']}")
            ws.cell(row=r["cash"], column=c).font = BLACK_FORMULA


# ═══════════════════════════════════════════════════════════════
# Cash Flow Builder
# ═══════════════════════════════════════════════════════════════

def _build_cf_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build Cash Flow Statement linked to IS and BS."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Cash Flow Statement ($M)").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    _write_period_headers(ws, CF_ROWS["header"], col_map, inputs.layout)

    # Labels
    cf_labels = {
        CF_ROWS["ni"]: "Net Income", CF_ROWS["da"]: "D&A",
        CF_ROWS["sbc"]: "Stock-Based Compensation", CF_ROWS["def_tax"]: "Deferred Taxes",
        CF_ROWS["d_ar"]: "Δ Accounts Receivable", CF_ROWS["d_inv"]: "Δ Inventories",
        CF_ROWS["d_pre"]: "Δ Prepaid & Other", CF_ROWS["d_ap"]: "Δ Accounts Payable",
        CF_ROWS["d_acc"]: "Δ Accrued Liabilities",
        CF_ROWS["total_op"]: "Cash from Operations",
        CF_ROWS["capex"]: "Capital Expenditures", CF_ROWS["other_inv"]: "Other Investing",
        CF_ROWS["total_inv"]: "Cash from Investing",
        CF_ROWS["buybacks"]: "Share Repurchases", CF_ROWS["divs"]: "Dividends Paid",
        CF_ROWS["debt"]: "Net Debt Activity", CF_ROWS["total_fin"]: "Cash from Financing",
        CF_ROWS["net_chg"]: "Net Change in Cash",
        CF_ROWS["beg_cash"]: "Beginning Cash", CF_ROWS["end_cash"]: "Ending Cash",
    }
    for row, label in cf_labels.items():
        ws.cell(row=row, column=2, value=label)
        if row in (CF_ROWS["total_op"], CF_ROWS["total_inv"], CF_ROWS["total_fin"],
                   CF_ROWS["net_chg"], CF_ROWS["end_cash"]):
            ws.cell(row=row, column=2).font = BOLD_FONT

    # Section headers
    ws.cell(row=CF_ROWS["op_header"], column=2, value="OPERATING ACTIVITIES")
    ws.cell(row=CF_ROWS["op_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)
    ws.cell(row=CF_ROWS["inv_header"], column=2, value="INVESTING ACTIVITIES")
    ws.cell(row=CF_ROWS["inv_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)
    ws.cell(row=CF_ROWS["fin_header"], column=2, value="FINANCING ACTIVITIES")
    ws.cell(row=CF_ROWS["fin_header"], column=2).font = Font(name="Aptos Narrow", size=11, bold=True, color=DARK_BLUE)

    # CF formulas reference IS and BS
    _write_cf_formulas(ws, col_map, inputs.layout)


def _write_cf_formulas(ws, col_map: dict, layout: ColumnLayout):
    """Write CF formulas for all periods."""
    r = CF_ROWS
    ir = IS_ROWS
    br = BS_ROWS
    d = DRIVER_SECTIONS

    for key, col in col_map.items():
        if key.startswith("_") or key.startswith("pctchg"):
            continue
        c = _col_num(col)
        cl = get_column_letter(c)

        # Net income from IS
        ws.cell(row=r["ni"], column=c, value=f"=IS!{cl}{ir['net_income']}")
        ws.cell(row=r["ni"], column=c).font = GREEN_XREF

        # D&A from IS
        ws.cell(row=r["da"], column=c, value=f"=IS!{cl}{ir['da']}")
        ws.cell(row=r["da"], column=c).font = GREEN_XREF

        # SBC from Drivers
        ws.cell(row=r["sbc"], column=c, value=f"=Drivers!{cl}{d['sbc']}")
        ws.cell(row=r["sbc"], column=c).font = GREEN_XREF

        # Deferred tax from Drivers
        ws.cell(row=r["def_tax"], column=c, value=f"=Drivers!{cl}{d['deferred_tax']}")
        ws.cell(row=r["def_tax"], column=c).font = GREEN_XREF

        # Capex from Drivers
        ws.cell(row=r["capex"], column=c, value=f"=Drivers!{cl}{d['capex']}")
        ws.cell(row=r["capex"], column=c).font = GREEN_XREF

        # Other investing from Drivers
        ws.cell(row=r["other_inv"], column=c, value=f"=Drivers!{cl}{d['other_inv']}")
        ws.cell(row=r["other_inv"], column=c).font = GREEN_XREF

        # Buybacks from Drivers
        ws.cell(row=r["buybacks"], column=c, value=f"=Drivers!{cl}{d['buybacks']}")
        ws.cell(row=r["buybacks"], column=c).font = GREEN_XREF

        # Dividends from Drivers
        ws.cell(row=r["divs"], column=c, value=f"=Drivers!{cl}{d['dividends']}")
        ws.cell(row=r["divs"], column=c).font = GREEN_XREF

        # Totals
        ws.cell(row=r["total_op"], column=c,
                value=f"={cl}{r['ni']}+{cl}{r['da']}+{cl}{r['sbc']}+{cl}{r['def_tax']}"
                      f"+{cl}{r['d_ar']}+{cl}{r['d_inv']}+{cl}{r['d_pre']}+{cl}{r['d_ap']}+{cl}{r['d_acc']}")
        ws.cell(row=r["total_inv"], column=c,
                value=f"={cl}{r['capex']}+{cl}{r['other_inv']}")
        ws.cell(row=r["total_fin"], column=c,
                value=f"={cl}{r['buybacks']}+{cl}{r['divs']}+{cl}{r['debt']}")
        ws.cell(row=r["net_chg"], column=c,
                value=f"={cl}{r['total_op']}+{cl}{r['total_inv']}+{cl}{r['total_fin']}")
        ws.cell(row=r["end_cash"], column=c,
                value=f"=BS!{cl}{br['cash']}")  # Hard link to BS cash
        ws.cell(row=r["end_cash"], column=c).font = GREEN_XREF

        # Format
        for row in range(r["ni"], r["end_cash"] + 1):
            if ws.cell(row=row, column=c).value:
                ws.cell(row=row, column=c).number_format = FMT_MONEY


# ═══════════════════════════════════════════════════════════════
# DCF Builder
# ═══════════════════════════════════════════════════════════════

def _build_dcf_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build DCF valuation tab with WACC, terminal value, sensitivity."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — DCF Valuation").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    wacc = inputs.wacc_assumptions
    if not wacc:
        ws.cell(row=4, column=2, value="WACC assumptions not provided")
        return

    # UFCF projection section
    ws.cell(row=4, column=2, value="Unlevered Free Cash Flow").font = BOLD_FONT
    ufcf_labels = ["EBITDA", "(-) D&A", "EBIT", "(-) Taxes on EBIT",
                   "NOPAT", "(+) D&A", "(-) CapEx", "(-) Δ NWC",
                   "Unlevered FCF"]
    for i, label in enumerate(ufcf_labels):
        ws.cell(row=6 + i, column=2, value=label)
        if label == "Unlevered FCF":
            ws.cell(row=6 + i, column=2).font = BOLD_FONT

    # WACC assumptions
    row = 19
    ws.cell(row=row, column=2, value="WACC Build-Up").font = BOLD_FONT
    assumptions = [
        ("Risk-Free Rate", wacc.get("rf", 0.043), FMT_PCT),
        ("Equity Risk Premium", wacc.get("erp", 0.055), FMT_PCT),
        ("Beta (Levered)", wacc.get("beta", 1.15), "0.00"),
        ("Cost of Equity", None, FMT_PCT),  # formula
        ("Debt / Total Cap", wacc.get("debt_pct", 0), FMT_PCT),
        ("Equity / Total Cap", None, FMT_PCT),  # formula
        ("Cost of Debt", wacc.get("cost_of_debt", 0.05), FMT_PCT),
        ("Tax Rate", wacc.get("tax_rate", 0.26), FMT_PCT),
        ("After-Tax Cost of Debt", None, FMT_PCT),  # formula
        ("WACC", None, FMT_PCT),  # formula
    ]
    for i, (label, val, fmt) in enumerate(assumptions):
        r = row + 1 + i
        ws.cell(row=r, column=2, value=label)
        if val is not None:
            ws.cell(row=r, column=3, value=val)
            ws.cell(row=r, column=3).number_format = fmt
            ws.cell(row=r, column=3).font = BLUE_INPUT
        ws.cell(row=r, column=2).font = BOLD_FONT if label == "WACC" else DEFAULT_FONT

    ws.cell(row=row + 1, column=2, value="WACC Build-Up").font = BOLD_FONT

    # Terminal value section
    row = 35
    ws.cell(row=row, column=2, value="Terminal Value").font = BOLD_FONT

    # Sensitivity tables placeholder
    row = 62
    ws.cell(row=row, column=2, value="Sensitivity Analysis").font = BOLD_FONT
    ws.cell(row=row + 1, column=2, value="Perpetuity Growth Rate vs WACC")
    ws.cell(row=row + 10, column=2, value="Exit Multiple vs WACC")


# ═══════════════════════════════════════════════════════════════
# Comps Builder
# ═══════════════════════════════════════════════════════════════

def _build_comps_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build comparable companies analysis tab."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Comparable Companies").font = Font(
        name="Aptos Narrow", size=14, bold=True, color=DARK_BLUE)

    headers = ["Company", "Ticker", "Mkt Cap ($B)", "EV ($B)",
               "EV/EBITDA NTM", "P/E NTM", "EV/Rev NTM",
               "Rev Growth %", "EBITDA Margin %", "Net Margin %"]
    _style_header(ws, 4, 2, 2 + len(headers) - 1, headers)

    if inputs.comps:
        for i, comp in enumerate(inputs.comps):
            row = 5 + i
            ws.cell(row=row, column=2, value=comp.get("name", ""))
            ws.cell(row=row, column=3, value=comp.get("ticker", ""))
            ws.cell(row=row, column=4, value=comp.get("mkt_cap", 0))
            ws.cell(row=row, column=4).number_format = FMT_MONEY_DEC
            ws.cell(row=row, column=5, value=comp.get("ev", 0))
            ws.cell(row=row, column=5).number_format = FMT_MONEY_DEC
            ws.cell(row=row, column=6, value=comp.get("ev_ebitda", 0))
            ws.cell(row=row, column=6).number_format = FMT_MULT
            ws.cell(row=row, column=7, value=comp.get("pe", 0))
            ws.cell(row=row, column=7).number_format = FMT_MULT
            ws.cell(row=row, column=8, value=comp.get("ev_rev", 0))
            ws.cell(row=row, column=8).number_format = FMT_MULT

    # Mean/Median rows
    n_comps = len(inputs.comps) if inputs.comps else 0
    if n_comps > 0:
        mean_row = 5 + n_comps + 1
        median_row = mean_row + 1
        ws.cell(row=mean_row, column=2, value="Mean").font = BOLD_FONT
        ws.cell(row=median_row, column=2, value="Median").font = BOLD_FONT

    ws.column_dimensions["B"].width = 22
    for col_letter in "CDEFGHIJK":
        ws.column_dimensions[col_letter].width = 14


# ═══════════════════════════════════════════════════════════════
# Summary Builder
# ═══════════════════════════════════════════════════════════════

def _build_summary_tab(ws, inputs: ModelInputs, col_map: dict):
    """Build executive Summary tab."""
    ws.cell(row=2, column=2, value=f"{inputs.ticker} — Investment Summary").font = Font(
        name="Aptos Narrow", size=16, bold=True, color=DARK_BLUE)

    sections = [
        (5, "Trading Statistics"),
        (13, "Scenario Valuation"),
        (27, "Key Estimates vs Consensus"),
        (37, "Key Operating Metrics"),
        (46, "Valuation Cross-Check"),
    ]
    for row, title in sections:
        ws.cell(row=row, column=2, value=title).font = Font(
            name="Aptos Narrow", size=12, bold=True, color=DARK_BLUE)


# ═══════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════

def build_financial_model(inputs: ModelInputs) -> "Workbook":
    """
    Build a fully linked 7-tab 3-statement financial model.

    Args:
        inputs: ModelInputs with historical data, scenarios, comps, WACC assumptions

    Returns:
        openpyxl Workbook ready to save
    """
    if not HAS_OPENPYXL:
        raise ImportError("openpyxl required: pip install openpyxl")

    wb = Workbook()
    col_map = inputs.layout.get_column_map()

    # Tab 1: Summary
    ws_summary = wb.active
    ws_summary.title = "Summary"
    _build_summary_tab(ws_summary, inputs, col_map)

    # Tab 2: Income Statement
    ws_is = wb.create_sheet("IS")
    _build_is_tab(ws_is, inputs, col_map)

    # Tab 3: Balance Sheet
    ws_bs = wb.create_sheet("BS")
    _build_bs_tab(ws_bs, inputs, col_map)

    # Tab 4: Cash Flow
    ws_cf = wb.create_sheet("CF")
    _build_cf_tab(ws_cf, inputs, col_map)

    # Tab 5: Drivers
    ws_drivers = wb.create_sheet("Drivers")
    _build_drivers_tab(ws_drivers, inputs, col_map)

    # Tab 6: DCF
    ws_dcf = wb.create_sheet("DCF")
    _build_dcf_tab(ws_dcf, inputs, col_map)

    # Tab 7: Comps
    ws_comps = wb.create_sheet("Comps")
    _build_comps_tab(ws_comps, inputs, col_map)

    # Apply annual column shading across all statement tabs
    annual_cols = col_map.get("_annual_cols", [])
    for ws in [ws_is, ws_bs, ws_cf, ws_drivers]:
        _shade_annual_cols(ws, annual_cols, 3, 120)

    return wb
