"""
Alpha Vantage Financials Loader

Fetches structured income statement data from Alpha Vantage's
INCOME_STATEMENT endpoint (free tier, 5 calls/min, 500/day).

This is the fallback when Polygon is unavailable.
Returns the same normalized FinancialData format as polygon_financials.py.
"""

import os
import httpx


AV_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "***REMOVED***")
BASE_URL = "https://www.alphavantage.co/query"


def _safe_float(val, default=0.0):
    """Convert Alpha Vantage string values to float, handling 'None' strings."""
    if val is None or val == "None" or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def fetch_av_financials(ticker: str) -> dict | None:
    """
    Fetch income statement from Alpha Vantage.

    Returns normalized dict matching polygon_financials format,
    or None if the API call fails.
    """
    if not AV_API_KEY:
        return None

    params = {
        "function": "INCOME_STATEMENT",
        "symbol": ticker.upper(),
        "apikey": AV_API_KEY,
    }

    try:
        resp = httpx.get(BASE_URL, params=params, timeout=15.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    # Check for error responses
    if "Error Message" in data or "Note" in data:
        return None

    annual = data.get("annualReports", [])
    if not annual:
        return None

    latest = annual[0]
    prior = annual[1] if len(annual) > 1 else None

    parsed = _parse_av_report(latest)
    if prior:
        parsed["prior_year"] = _parse_av_report(prior)

    return parsed


def _parse_av_report(report: dict) -> dict:
    """Parse a single Alpha Vantage annual report into normalized format."""
    revenue = _safe_float(report.get("totalRevenue"))
    cost_of_revenue = _safe_float(report.get("costOfRevenue"))
    cogs = _safe_float(report.get("costofGoodsAndServicesSold"))
    gross_profit = _safe_float(report.get("grossProfit"))
    operating_income = _safe_float(report.get("operatingIncome"))
    operating_expenses = _safe_float(report.get("operatingExpenses"))
    sga = _safe_float(report.get("sellingGeneralAndAdministrative"))
    rd = _safe_float(report.get("researchAndDevelopment"))
    da = _safe_float(report.get("depreciationAndAmortization"))
    ebit = _safe_float(report.get("ebit"))
    ebitda = _safe_float(report.get("ebitda"))
    interest_expense = _safe_float(report.get("interestExpense"))
    interest_income = _safe_float(report.get("interestIncome"))
    net_interest = _safe_float(report.get("netInterestIncome"))
    pretax_income = _safe_float(report.get("incomeBeforeTax"))
    tax_expense = _safe_float(report.get("incomeTaxExpense"))
    net_income = _safe_float(report.get("netIncome"))

    # Use costofGoodsAndServicesSold if costOfRevenue is empty
    if not cost_of_revenue and cogs:
        cost_of_revenue = cogs

    # Derived percentages
    pcts = {}
    if revenue and revenue > 0:
        if cost_of_revenue:
            pcts["cost_of_revenue_pct"] = round(cost_of_revenue / revenue * 100, 1)
        if gross_profit:
            pcts["gross_margin_pct"] = round(gross_profit / revenue * 100, 1)
        if sga:
            pcts["sga_pct"] = round(sga / revenue * 100, 1)
        if rd:
            pcts["rd_pct"] = round(rd / revenue * 100, 1)
        if operating_income:
            pcts["operating_margin_pct"] = round(operating_income / revenue * 100, 1)
        if operating_expenses and operating_expenses != revenue:
            pcts["opex_pct"] = round(operating_expenses / revenue * 100, 1)

    # Tax rate
    tax_rate = 0.0
    if pretax_income and pretax_income > 0 and tax_expense:
        tax_rate = round(tax_expense / pretax_income, 3)

    # Net interest (positive = income, negative = expense)
    net_interest_val = net_interest
    if not net_interest_val:
        net_interest_val = (interest_income or 0) - (interest_expense or 0)

    # Fiscal date
    fiscal_date = report.get("fiscalDateEnding", "")
    fiscal_year = int(fiscal_date[:4]) if fiscal_date else None

    return {
        "source": "alpha_vantage",
        "fiscal_year": fiscal_year,
        "fiscal_period": "FY",
        "filing_date": fiscal_date,
        "income_statement": {
            "revenue": revenue,
            "revenue_m": round(revenue / 1e6, 1) if revenue else 0,
            "cost_of_revenue": cost_of_revenue,
            "gross_profit": gross_profit,
            "operating_income": operating_income,
            "operating_expenses": operating_expenses,
            "sga": sga,
            "research_and_development": rd,
            "depreciation_amortization": da,
            "ebit": ebit,
            "ebitda": ebitda,
            "interest_expense": interest_expense,
            "interest_income": interest_income,
            "net_interest": net_interest_val,
            "pretax_income": pretax_income,
            "tax_expense": tax_expense,
            "net_income": net_income,
            # AV doesn't provide per-share data in income_statement endpoint
            "basic_eps": 0,
            "diluted_eps": 0,
            "basic_shares": 0,
            "diluted_shares": 0,
            "total_costs": (cost_of_revenue or 0) + (operating_expenses or 0),
        },
        "percentages": pcts,
        "tax_rate": tax_rate,
        "balance_sheet": {},
        "cash_flow": {},
    }


def fetch_av_overview(ticker: str) -> dict | None:
    """
    Fetch company overview from Alpha Vantage for shares outstanding, EPS, etc.
    This supplements the income statement with per-share data.
    """
    if not AV_API_KEY:
        return None

    params = {
        "function": "OVERVIEW",
        "symbol": ticker.upper(),
        "apikey": AV_API_KEY,
    }

    try:
        resp = httpx.get(BASE_URL, params=params, timeout=15.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if "Error Message" in data or "Symbol" not in data:
        return None

    return {
        "shares_outstanding": _safe_float(data.get("SharesOutstanding")),
        "diluted_eps_ttm": _safe_float(data.get("DilutedEPSTTM")),
        "market_cap": _safe_float(data.get("MarketCapitalization")),
        "pe_ratio": _safe_float(data.get("PERatio")),
        "sector": data.get("Sector", ""),
        "industry": data.get("Industry", ""),
    }
