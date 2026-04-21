"""
Phase 7: Export Wiring — Ticker → Live Data → Financial Model .xlsx

Glue code that:
  1. Takes a ticker
  2. Fetches live yfinance data (historicals, financials, info)
  3. Maps yfinance fields into ModelInputs dataclass
  4. Calls build_financial_model() to generate the .xlsx
  5. Saves to /workspace/investment-workbench/data/exports/

Usage:
    from research.pipeline_runner import build_model_for_ticker
    path = build_model_for_ticker("CMG")
"""

from __future__ import annotations

import json
import shutil
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", category=FutureWarning)

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

from research.financial_model import (
    ModelInputs, QuarterData, ScenarioAssumptions, ColumnLayout,
    build_financial_model,
)


# ═══════════════════════════════════════════════════════════════
# API Key Loading
# ═══════════════════════════════════════════════════════════════

KEYS_PATH = Path("/skills/user/data-apis/keys.json")

def _load_api_keys() -> dict:
    try:
        if KEYS_PATH.exists():
            return json.loads(KEYS_PATH.read_text())
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════
# yfinance → QuarterData Mapping
# ═══════════════════════════════════════════════════════════════

def _safe_get(df, col, default=0):
    try:
        val = df[col] if col in df.index else default
        if val is None:
            return default
        import math
        if isinstance(val, float) and math.isnan(val):
            return default
        return float(val)
    except (KeyError, TypeError, ValueError):
        return default


def _to_millions(val) -> float:
    if val is None or val == 0:
        return 0
    return round(val / 1e6, 1)


def _period_label(date, fiscal_year_end_month: int = 12) -> str:
    month = date.month
    year = date.year
    if fiscal_year_end_month == 12:
        q = (month - 1) // 3 + 1
        fy = year
    else:
        offset_month = (month - fiscal_year_end_month - 1) % 12
        q = offset_month // 3 + 1
        fy = year if month > fiscal_year_end_month else year
    return f"{q}Q{str(fy)[2:]}"


def fetch_yfinance_quarterly(ticker: str, verbose: bool = False) -> list[QuarterData]:
    """Fetch quarterly financial data from yfinance and map to QuarterData."""
    if not HAS_YFINANCE:
        raise ImportError("yfinance required")

    def v(msg):
        if verbose:
            print(f"    {msg}")

    t = yf.Ticker(ticker)
    info = t.info or {}

    fy_end = info.get("fiscalYearEnd", "December")
    month_map = {"January": 1, "February": 2, "March": 3, "April": 4,
                 "May": 5, "June": 6, "July": 7, "August": 8,
                 "September": 9, "October": 10, "November": 11, "December": 12}
    fy_end_month = month_map.get(fy_end, 12)

    try:
        is_q = t.quarterly_income_stmt
        bs_q = t.quarterly_balance_sheet
        cf_q = t.quarterly_cashflow
    except Exception as e:
        v(f"Error fetching quarterly data: {e}")
        return []

    if is_q is None or is_q.empty:
        v("No quarterly income statement data available")
        return []

    quarters = []
    dates = sorted(is_q.columns)[-12:]

    for date in dates:
        label = _period_label(date, fy_end_month)

        revenue = _to_millions(_safe_get(is_q[date], "Total Revenue"))
        if revenue == 0:
            revenue = _to_millions(_safe_get(is_q[date], "Operating Revenue"))

        cogs = _to_millions(_safe_get(is_q[date], "Cost Of Revenue"))
        sga = _to_millions(_safe_get(is_q[date], "Selling General And Administration"))
        da = _to_millions(_safe_get(is_q[date], "Reconciled Depreciation"))
        if da == 0:
            da = _to_millions(_safe_get(is_q[date], "Depreciation And Amortization Income Statement"))
        interest = _to_millions(_safe_get(is_q[date], "Interest Expense"))
        tax = _to_millions(_safe_get(is_q[date], "Tax Provision"))

        shares_raw = _safe_get(is_q[date], "Diluted Average Shares",
                               _safe_get(is_q[date], "Basic Average Shares"))
        shares = _to_millions(shares_raw) if shares_raw > 1000 else shares_raw

        # Balance Sheet
        bs_data = {}
        if bs_q is not None and not bs_q.empty:
            bs_dates = sorted(bs_q.columns)
            closest_bs = min(bs_dates, key=lambda d: abs((d - date).days)) if bs_dates else None
            if closest_bs and abs((closest_bs - date).days) < 100:
                bc = bs_q[closest_bs]
                bs_data = {
                    "cash": _to_millions(_safe_get(bc, "Cash And Cash Equivalents",
                                                    _safe_get(bc, "Cash Cash Equivalents And Short Term Investments"))),
                    "ar": _to_millions(_safe_get(bc, "Accounts Receivable", _safe_get(bc, "Receivables"))),
                    "inv": _to_millions(_safe_get(bc, "Inventory")),
                    "prepaid": _to_millions(_safe_get(bc, "Prepaid Assets", _safe_get(bc, "Other Current Assets"))),
                    "ppe": _to_millions(_safe_get(bc, "Net PPE", _safe_get(bc, "Gross PPE"))),
                    "goodwill": _to_millions(_safe_get(bc, "Goodwill")),
                    "other_lta": _to_millions(_safe_get(bc, "Other Non Current Assets")),
                    "ap": _to_millions(_safe_get(bc, "Accounts Payable")),
                    "accrued": _to_millions(_safe_get(bc, "Current Accrued Expenses",
                                                       _safe_get(bc, "Other Current Liabilities"))),
                    "lt_debt": _to_millions(_safe_get(bc, "Long Term Debt")),
                    "lt_lease": _to_millions(_safe_get(bc, "Long Term Capital Lease Obligation")),
                    "common": _to_millions(_safe_get(bc, "Common Stock Equity",
                                                      _safe_get(bc, "Stockholders Equity"))),
                    "retained": _to_millions(_safe_get(bc, "Retained Earnings")),
                    "treasury": _to_millions(_safe_get(bc, "Treasury Stock")),
                }

        # Cash Flow
        cf_data = {}
        if cf_q is not None and not cf_q.empty:
            cf_dates = sorted(cf_q.columns)
            closest_cf = min(cf_dates, key=lambda d: abs((d - date).days)) if cf_dates else None
            if closest_cf and abs((closest_cf - date).days) < 100:
                cc = cf_q[closest_cf]
                cf_data = {
                    "capex": _to_millions(_safe_get(cc, "Capital Expenditure")),
                    "sbc": _to_millions(_safe_get(cc, "Stock Based Compensation")),
                    "buybacks": _to_millions(_safe_get(cc, "Repurchase Of Capital Stock")),
                    "dividends": _to_millions(_safe_get(cc, "Common Stock Dividend Paid",
                                                         _safe_get(cc, "Cash Dividends Paid"))),
                }

        qd = QuarterData(
            period=label,
            revenue=revenue,
            food=cogs * 0.42 if cogs else 0,
            labor=cogs * 0.38 if cogs else 0,
            occupancy=cogs * 0.08 if cogs else 0,
            other_rest=cogs * 0.12 if cogs else 0,
            ga=sga, da=da, interest=interest, taxes=tax, shares=shares,
            cash=bs_data.get("cash", 0), ar=bs_data.get("ar", 0),
            inv=bs_data.get("inv", 0), prepaid=bs_data.get("prepaid", 0),
            ppe=bs_data.get("ppe", 0), goodwill=bs_data.get("goodwill", 0),
            other_lta=bs_data.get("other_lta", 0), ap=bs_data.get("ap", 0),
            accrued=bs_data.get("accrued", 0), lt_debt=bs_data.get("lt_debt", 0),
            lt_lease=bs_data.get("lt_lease", 0), common=bs_data.get("common", 0),
            retained=bs_data.get("retained", 0), treasury=bs_data.get("treasury", 0),
            capex=cf_data.get("capex", 0), sbc=cf_data.get("sbc", 0),
            buybacks=cf_data.get("buybacks", 0), dividends=cf_data.get("dividends", 0),
        )

        v(f"  {label}: Rev=${revenue:.0f}M  DA=${da:.0f}M  Tax=${tax:.0f}M  Shares={shares:.0f}M")
        quarters.append(qd)

    return quarters


# ═══════════════════════════════════════════════════════════════
# Default Scenario Generation
# ═══════════════════════════════════════════════════════════════

def _generate_default_scenarios(quarters, info):
    n_hist = len(quarters)
    n_fcst = 8

    revenues = [q.revenue for q in quarters if q.revenue > 0]
    rev_growth_hist = []
    for i in range(4, len(quarters)):
        prior = quarters[i - 4].revenue
        if prior and prior > 0:
            rev_growth_hist.append((quarters[i].revenue / prior - 1) * 100)
    avg_growth = sum(rev_growth_hist) / len(rev_growth_hist) if rev_growth_hist else 5.0

    scenarios = {}

    hist_sss = [avg_growth] * n_hist
    scenarios["sss"] = ScenarioAssumptions(
        bear=hist_sss + [max(avg_growth - 3, -2)] * n_fcst,
        base=hist_sss + [avg_growth] * n_fcst,
        bull=hist_sss + [avg_growth + 2] * n_fcst,
    )

    def _cost_pct(cost_attr, driver_name):
        hist_vals = []
        for q in quarters:
            rev = q.revenue if q.revenue > 0 else 1
            cost = getattr(q, cost_attr, 0)
            hist_vals.append(round(cost / rev * 100, 2))
        avg_pct = sum(hist_vals) / len(hist_vals) if hist_vals else 30
        scenarios[driver_name] = ScenarioAssumptions(
            bear=hist_vals + [round(avg_pct + 0.5, 2)] * n_fcst,
            base=hist_vals + [round(avg_pct, 2)] * n_fcst,
            bull=hist_vals + [round(avg_pct - 0.3, 2)] * n_fcst,
        )

    _cost_pct("food", "food_pct")
    _cost_pct("labor", "labor_pct")
    _cost_pct("occupancy", "occupancy_pct")
    _cost_pct("other_rest", "other_rest_pct")
    _cost_pct("ga", "ga_pct")

    da_vals = [q.da for q in quarters]
    avg_da = sum(da_vals) / len(da_vals) if da_vals else 50
    scenarios["da_pct"] = ScenarioAssumptions(
        bear=da_vals + [round(avg_da * 1.05, 1)] * n_fcst,
        base=da_vals + [round(avg_da * 1.02, 1)] * n_fcst,
        bull=da_vals + [round(avg_da, 1)] * n_fcst,
    )

    hist_tax = []
    for q in quarters:
        pretax = q.revenue - q.food - q.labor - q.occupancy - q.other_rest - q.ga - q.da - q.interest
        if pretax and pretax > 0 and q.taxes:
            hist_tax.append(round(abs(q.taxes) / pretax * 100, 2))
        else:
            hist_tax.append(25.0)
    avg_tax = sum(hist_tax) / len(hist_tax)
    scenarios["tax_rate"] = ScenarioAssumptions(
        bear=hist_tax + [round(avg_tax + 1, 2)] * n_fcst,
        base=hist_tax + [round(avg_tax, 2)] * n_fcst,
        bull=hist_tax + [round(avg_tax - 1, 2)] * n_fcst,
    )

    share_vals = [q.shares for q in quarters]
    avg_shares = share_vals[-1] if share_vals else 100
    scenarios["diluted_shares"] = ScenarioAssumptions(
        bear=share_vals + [round(avg_shares * 0.99, 1)] * n_fcst,
        base=share_vals + [round(avg_shares * 0.98, 1)] * n_fcst,
        bull=share_vals + [round(avg_shares * 0.97, 1)] * n_fcst,
    )

    capex_vals = [q.capex for q in quarters]
    avg_capex = sum(capex_vals) / len(capex_vals) if capex_vals else -100
    scenarios["capex"] = ScenarioAssumptions(
        bear=capex_vals + [round(avg_capex * 1.10, 1)] * n_fcst,
        base=capex_vals + [round(avg_capex, 1)] * n_fcst,
        bull=capex_vals + [round(avg_capex * 0.95, 1)] * n_fcst,
    )

    bb_vals = [q.buybacks for q in quarters]
    avg_bb = sum(bb_vals) / len(bb_vals) if bb_vals else 0
    scenarios["buybacks"] = ScenarioAssumptions(
        bear=bb_vals + [round(avg_bb * 0.8, 1)] * n_fcst,
        base=bb_vals + [round(avg_bb, 1)] * n_fcst,
        bull=bb_vals + [round(avg_bb * 1.2, 1)] * n_fcst,
    )

    sbc_vals = [q.sbc for q in quarters]
    avg_sbc = sum(sbc_vals) / len(sbc_vals) if sbc_vals else 0
    scenarios["sbc"] = ScenarioAssumptions(
        bear=sbc_vals + [round(avg_sbc * 1.05, 1)] * n_fcst,
        base=sbc_vals + [round(avg_sbc, 1)] * n_fcst,
        bull=sbc_vals + [round(avg_sbc * 0.95, 1)] * n_fcst,
    )

    return scenarios


# ═══════════════════════════════════════════════════════════════
# Comps Fetching
# ═══════════════════════════════════════════════════════════════

def fetch_comps(ticker, peer_tickers=None, verbose=False):
    if not HAS_YFINANCE or not peer_tickers:
        return []
    comps = []
    for pticker in peer_tickers:
        try:
            t = yf.Ticker(pticker)
            info = t.info or {}
            comps.append({
                "name": info.get("shortName", pticker)[:25],
                "ticker": pticker,
                "mkt_cap": round((info.get("marketCap", 0) or 0) / 1e9, 2),
                "ev": round((info.get("enterpriseValue", 0) or 0) / 1e9, 2),
                "ev_ebitda": info.get("enterpriseToEbitda"),
                "pe": info.get("forwardPE"),
                "ev_rev": info.get("enterpriseToRevenue"),
                "rev_growth": round((info.get("revenueGrowth", 0) or 0) * 100, 1),
                "ebitda_margin": round((info.get("ebitdaMargins", 0) or 0) * 100, 1),
                "net_margin": round((info.get("profitMargins", 0) or 0) * 100, 1),
            })
        except Exception as e:
            if verbose:
                print(f"    Comp {pticker} error: {e}")
    return comps


# ═══════════════════════════════════════════════════════════════
# Main Builder
# ═══════════════════════════════════════════════════════════════

def build_model_for_ticker(
    ticker: str,
    peer_tickers: list[str] = None,
    output_path: str = None,
    verbose: bool = True,
) -> str:
    """End-to-end: ticker → yfinance → ModelInputs → .xlsx"""
    if not HAS_YFINANCE:
        raise ImportError("yfinance required")

    def v(msg):
        if verbose:
            print(f"  {msg}")

    ticker = ticker.upper()
    v(f"Building model for {ticker}...")

    # Step 1: Fetch quarterly data
    v("Fetching quarterly financials from yfinance...")
    raw_quarters = fetch_yfinance_quarterly(ticker, verbose=verbose)
    if not raw_quarters:
        raise ValueError(f"No quarterly data available for {ticker}")
    v(f"  Got {len(raw_quarters)} quarters")

    # Step 2: Use default layout — model builder has hardcoded year iteration
    layout = ColumnLayout()  # defaults: hist_start_fy=2023, 3 hist years, 2 forecast

    # Map available quarters into expected grid, pad missing with zeros
    expected_periods = []
    for fy_offset in range(layout.hist_years):
        fy = layout.hist_start_fy + fy_offset
        for q in range(1, 5):
            expected_periods.append(f"{q}Q{str(fy)[2:]}")

    q_lookup = {q.period: q for q in raw_quarters}
    quarters = []
    for period in expected_periods:
        if period in q_lookup:
            quarters.append(q_lookup[period])
        else:
            quarters.append(QuarterData(period=period))

    filled = sum(1 for q in quarters if q.revenue > 0)
    v(f"  Layout: FY{layout.hist_start_fy}-FY{layout.hist_start_fy + layout.hist_years - 1} hist, "
      f"FY{layout.forecast_start_fy}-FY{layout.forecast_start_fy + layout.forecast_years - 1} fcst, "
      f"{len(quarters)} quarters ({filled} with data)")

    # Step 3: Fetch info for scenarios
    t = yf.Ticker(ticker)
    info = t.info or {}

    # Step 4: Generate scenarios
    v("Generating default scenarios...")
    scenarios = _generate_default_scenarios(quarters, info)

    # Step 5: Fetch comps
    comps = []
    if peer_tickers:
        v(f"Fetching comps for {len(peer_tickers)} peers...")
        comps = fetch_comps(ticker, peer_tickers, verbose=verbose)

    # Step 6: WACC
    beta = info.get("beta", 1.15) or 1.15
    wacc = {
        "rf": 0.043, "erp": 0.055, "beta": round(beta, 2),
        "debt_pct": 0.10, "cost_of_debt": 0.05,
        "tax_rate": 0.26, "terminal_growth": 0.03,
    }

    # Step 7: Assemble
    inputs = ModelInputs(
        ticker=ticker,
        hist_quarters=quarters,
        scenarios=scenarios,
        comps=comps,
        wacc_assumptions=wacc,
        layout=layout,
    )

    # Step 8: Build
    v("Building 7-tab financial model...")
    wb = build_financial_model(inputs)

    # Step 9: Save via /tmp (GCS compat)
    if not output_path:
        export_dir = Path("/workspace/investment-workbench/data/exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(export_dir / f"{ticker}_model_{ts}.xlsx")

    tmp_path = f"/tmp/{ticker}_model_tmp.xlsx"
    wb.save(tmp_path)
    shutil.copy(tmp_path, output_path)
    Path(tmp_path).unlink(missing_ok=True)
    v(f"Model saved: {output_path}")

    return output_path


def build_model_from_research(result: dict, peer_tickers: list[str] = None) -> str:
    """Build financial model from pipeline research result."""
    ticker = result.get("ticker", "")
    if not ticker:
        raise ValueError("No ticker in research result")
    return build_model_for_ticker(ticker=ticker, peer_tickers=peer_tickers, verbose=True)


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "CMG"
    peers = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    path = build_model_for_ticker(ticker, peer_tickers=peers)
    print(f"\nDone: {path}")
