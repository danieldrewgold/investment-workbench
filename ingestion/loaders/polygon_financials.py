"""
Polygon.io Financials Loader

Fetches structured income statement, balance sheet, and cash flow data
from Polygon's /vX/reference/financials endpoint (free tier, 2yr history).

Returns a normalized FinancialData dict that the pipeline uses directly,
so the Claude API call doesn't need to extract numbers from text.
"""

import os
import json
import httpx
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv():
    """Load repo-root .env (gitignored) so keys stay out of source/history."""
    try:
        p = Path(__file__).resolve().parents[2] / ".env"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


_load_dotenv()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")   # from gitignored .env, never hardcoded
BASE_URL = "https://api.polygon.io"


def _get_value(section: dict, key: str, default=None):
    """Extract value from Polygon's {value, unit, label} format."""
    item = section.get(key)
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get("value", default)
    return item


def fetch_polygon_financials(ticker: str, timeframe: str = "annual", limit: int = 2) -> dict | None:
    """
    Fetch financials from Polygon /vX/reference/financials.

    Returns dict with:
      - income_statement: revenue, costs, eps, shares, etc.
      - balance_sheet: total assets, equity, debt (if available)
      - cash_flow: operating CF, capex, FCF (if available)
      - metadata: fiscal_year, fiscal_period, source
    Or None if the API call fails.
    """
    if not POLYGON_API_KEY:
        return None

    url = f"{BASE_URL}/vX/reference/financials"
    params = {
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "limit": limit,
        "apiKey": POLYGON_API_KEY,
    }

    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        if resp.status_code == 429:
            import time
            time.sleep(12)
            resp = httpx.get(url, params=params, timeout=15.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Parse most recent filing
    latest = results[0]
    prior = results[1] if len(results) > 1 else None

    parsed = _parse_filing(latest)
    if prior:
        parsed["prior_year"] = _parse_filing(prior)

    return parsed


def _parse_filing(filing: dict) -> dict:
    """Parse a single Polygon filing result into our normalized format."""
    fins = filing.get("financials", {})
    ic = fins.get("income_statement", {})
    bs = fins.get("balance_sheet", {})
    cf = fins.get("cash_flow_statement", {})

    # Revenue and cost structure
    revenue = _get_value(ic, "revenues", 0)
    cost_of_revenue = _get_value(ic, "cost_of_revenue", 0)
    gross_profit = _get_value(ic, "gross_profit", 0)
    operating_income = _get_value(ic, "operating_income_loss", 0)
    operating_expenses = _get_value(ic, "operating_expenses", 0)
    sga = _get_value(ic, "selling_general_and_administrative_expenses", 0)
    rd = _get_value(ic, "research_and_development", 0)
    da = _get_value(ic, "depreciation_and_amortization", 0)
    interest_expense = _get_value(ic, "interest_expense_operating", 0)
    net_income = _get_value(ic, "net_income_loss", 0)
    net_income_parent = _get_value(ic, "net_income_loss_attributable_to_parent", net_income)
    tax_expense = _get_value(ic, "income_tax_expense_benefit", 0)
    pretax_income = _get_value(ic, "income_loss_from_continuing_operations_before_tax", 0)
    basic_eps = _get_value(ic, "basic_earnings_per_share", 0)
    diluted_eps = _get_value(ic, "diluted_earnings_per_share", 0)
    basic_shares = _get_value(ic, "basic_average_shares", 0)
    diluted_shares = _get_value(ic, "diluted_average_shares", 0)
    total_costs = _get_value(ic, "benefits_costs_expenses", 0) or _get_value(ic, "costs_and_expenses", 0)

    # Balance sheet
    total_assets = _get_value(bs, "assets", 0)
    total_equity = _get_value(bs, "equity", 0)
    total_debt = _get_value(bs, "long_term_debt", 0)

    # Cash flow
    operating_cf = _get_value(cf, "net_cash_flow_from_operating_activities", 0)
    capex = abs(_get_value(cf, "net_cash_flow_from_investing_activities", 0))

    # Derived percentages (as % of revenue)
    rev_m = revenue / 1e6 if revenue else 0
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

    # Effective tax rate
    tax_rate = 0.0
    if pretax_income and pretax_income > 0 and tax_expense:
        tax_rate = round(tax_expense / pretax_income, 3)

    return {
        "source": "polygon",
        "fiscal_year": filing.get("fiscal_year"),
        "fiscal_period": filing.get("fiscal_period"),
        "filing_date": filing.get("filing_date"),
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
            "interest_expense": interest_expense,
            "pretax_income": pretax_income,
            "tax_expense": tax_expense,
            "net_income": net_income_parent or net_income,
            "basic_eps": basic_eps,
            "diluted_eps": diluted_eps,
            "basic_shares": basic_shares,
            "diluted_shares": diluted_shares,
            "total_costs": total_costs,
        },
        "percentages": pcts,
        "tax_rate": tax_rate,
        "balance_sheet": {
            "total_assets": total_assets,
            "total_equity": total_equity,
            "long_term_debt": total_debt,
        },
        "cash_flow": {
            "operating_cf": operating_cf,
            "capex": capex,
            "fcf": (operating_cf - capex) if operating_cf else 0,
        },
    }


def fetch_polygon_quarterly(ticker: str, limit: int = 4) -> list[dict] | None:
    """Fetch quarterly financials for trend analysis."""
    result = fetch_polygon_financials(ticker, timeframe="quarterly", limit=limit)
    if not result:
        return None
    # For quarterly, we just return the latest quarter + metadata
    return result
