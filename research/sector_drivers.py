"""
Sector Driver Schemas

Layer 2: Pluggable configuration that tells the sector-agnostic
engine how a specific type of business works economically.

Each schema defines:
  - revenue_model: how revenue builds from drivers
  - revenue_inputs: which assumption keys drive revenue
  - cost_buckets: how costs decompose with variable/fixed splits
  - below_line_costs: G&A, D&A, etc. structure
  - key_metrics: what to prioritize during orientation
  - typical_escalations: what analytical work is usually valuable

The schema is DATA, not CODE. The engine reads it;
the schema doesn't contain logic.
"""


RESTAURANT_DRIVERS = {
    "sector": "restaurant",
    "description": "Company-operated restaurant chain (e.g. CMG, CAVA)",

    # ── Revenue model ──
    "revenue_model": "sss_plus_new_stores",
    "revenue_inputs": ["sss_growth_pct", "new_restaurants", "new_store_productivity"],
    "revenue_doc": (
        "revenue = existing_stores × prior_AUV × (1 + SSS) "
        "+ new_stores × prior_AUV × productivity × 0.5"
    ),

    # ── Cost buckets (restaurant-level) ──
    # Each bucket: name, prior_year_key (% of revenue), variable_pct,
    #   delta_assumption_key (bps change the analyst can set)
    "cost_buckets": [
        {"name": "food",      "prior_key": "food_pct",             "variable_pct": 0.95,
         "delta_key": "food_cost_delta_bps",  "scales_with": "revenue", "above_gross": True},
        {"name": "labor",     "prior_key": "labor_pct",            "variable_pct": 0.60,
         "delta_key": "labor_cost_delta_bps", "scales_with": "revenue", "above_gross": True},
        {"name": "occupancy", "prior_key": "occupancy_pct",        "variable_pct": 0.05,
         "delta_key": None,                   "scales_with": "stores",  "above_gross": True},
        {"name": "other",     "prior_key": "other_operating_pct",  "variable_pct": 0.50,
         "delta_key": "other_cost_delta_bps", "scales_with": "revenue", "above_gross": True},
    ],
    "gross_profit_label": "restaurant_profit",
    "gross_margin_label": "restaurant_margin_pct",

    # ── Below-line costs ──
    "below_line": {
        "cash_ga":    {"prior_key": "cash_ga_m",    "growth_key": "cash_ga_growth_pct",    "default_growth": 5.0},
        "stock_comp": {"prior_key": "stock_comp_m", "growth_key": "stock_comp_growth_pct", "default_growth": None},
        "da":         {"prior_key": "da_m",         "scales_with": "stores"},
        "preopen":    {"prior_key": "preopen_m",    "scales_with": "new_stores"},
    },

    # ── Key metrics for orientation ──
    "key_metrics": [
        "sss_growth", "restaurant_margin", "new_restaurants",
        "traffic", "average_check", "digital_mix",
    ],

    # ── Typical escalations ──
    "typical_escalations": ["BRIDGE_ANALYSIS", "GUIDANCE_HISTORY", "CADENCE_TABLE", "BASELINE_FORECAST"],
    "usually_overkill": ["REGRESSION", "EXTERNAL_DATA", "FISCAL_ALIGNMENT"],
}


SOFTWARE_DRIVERS = {
    "sector": "software",
    "description": "SaaS / subscription software company (e.g. CRM, DDOG, SNOW)",

    # ── Revenue model ──
    # revenue = prior_ARR × (1 - churn) + new_ARR_bookings
    # ARR = seats × ARPU (or customers × ACV)
    "revenue_model": "arr_plus_new_bookings",
    "revenue_inputs": ["net_retention_pct", "new_arr_growth_pct"],
    "revenue_doc": (
        "revenue = prior_revenue × net_retention_rate "
        "+ prior_revenue × new_arr_growth_rate"
    ),

    # ── Cost buckets (as % of revenue) ──
    # Software has different cost structure: high gross margin, heavy S&M and R&D
    "cost_buckets": [
        {"name": "cogs",     "prior_key": "cogs_pct",    "variable_pct": 0.80,
         "delta_key": "cogs_delta_bps",    "scales_with": "revenue", "above_gross": True},
        {"name": "sales_marketing", "prior_key": "sm_pct", "variable_pct": 0.70,
         "delta_key": "sm_delta_bps",      "scales_with": "revenue", "above_gross": False},
        {"name": "research_dev",    "prior_key": "rd_pct", "variable_pct": 0.40,
         "delta_key": "rd_delta_bps",      "scales_with": "revenue", "above_gross": False},
        {"name": "general_admin",   "prior_key": "ga_pct", "variable_pct": 0.30,
         "delta_key": "ga_delta_bps",      "scales_with": "revenue", "above_gross": False},
    ],
    "gross_profit_label": "gross_profit",
    "gross_margin_label": "gross_margin_pct",

    # ── Below-line costs ──
    # For software, most costs are already in the cost buckets above.
    # Below-line is simpler: just stock comp (often reported separately) and D&A.
    "below_line": {
        "stock_comp": {"prior_key": "stock_comp_m", "growth_key": "stock_comp_growth_pct", "default_growth": None},
        "da":         {"prior_key": "da_m",         "growth_key": "da_growth_pct", "default_growth": 10.0},
    },

    # ── Key metrics for orientation ──
    "key_metrics": [
        "arr", "net_retention", "gross_margin", "rule_of_40",
        "customer_count", "arpu", "free_cash_flow_margin",
    ],

    # ── Typical escalations ──
    "typical_escalations": ["CADENCE_TABLE", "BASELINE_FORECAST", "GUIDANCE_HISTORY"],
    "usually_overkill": ["BRIDGE_ANALYSIS", "FISCAL_ALIGNMENT", "EXTERNAL_DATA"],
}


FRANCHISE_DRIVERS = {
    "sector": "franchise_restaurant",
    "description": "Franchise-heavy restaurant chain (e.g. WING, QSR, DPZ). "
                   "Revenue = royalties + ad fund fees + small company-owned. "
                   "P&L dominated by SG&A and interest, not food/labor/occupancy.",

    # ── Revenue model ──
    # Revenue = (system_wide_sales × royalty_rate)
    #         + (system_wide_sales × ad_fund_rate)
    #         + (company_owned_stores × AUV × (1 + company_SSS))
    # System-wide sales = total_stores × AUV × (1 + SSS)
    "revenue_model": "franchise_royalty",
    "revenue_inputs": ["sss_growth_pct", "new_restaurants", "royalty_rate_pct",
                       "ad_fund_rate_pct", "company_owned_stores"],
    "revenue_doc": (
        "revenue = system_wide_sales × (royalty_rate + ad_fund_rate) "
        "+ company_owned_stores × AUV × (1 + co_sss). "
        "system_wide_sales = total_stores × prior_AUV × (1 + SSS)."
    ),

    # ── Cost buckets ──
    # For franchise: COGS = company-owned restaurant costs only (small).
    # Ad expenses ≈ ad fee revenue (pass-through). SG&A is the big cost line.
    "cost_buckets": [
        {"name": "cost_of_sales",  "prior_key": "cos_pct",  "variable_pct": 0.85,
         "delta_key": "cos_delta_bps",   "scales_with": "revenue", "above_gross": True},
        {"name": "ad_expense",     "prior_key": "ad_exp_pct", "variable_pct": 0.95,
         "delta_key": None,              "scales_with": "revenue", "above_gross": False},
        {"name": "sga",            "prior_key": "sga_pct",  "variable_pct": 0.40,
         "delta_key": "sga_delta_bps",   "scales_with": "revenue", "above_gross": False},
    ],
    "gross_profit_label": "gross_profit",
    "gross_margin_label": "gross_margin_pct",

    # ── Below-line costs ──
    "below_line": {
        "stock_comp": {"prior_key": "stock_comp_m", "growth_key": "stock_comp_growth_pct", "default_growth": None},
        "da":         {"prior_key": "da_m",         "growth_key": "da_growth_pct", "default_growth": 15.0},
    },

    # ── Key metrics for orientation ──
    "key_metrics": [
        "system_wide_sales", "domestic_sss", "franchise_unit_growth",
        "royalty_rate", "adr_fund_rate", "sga_as_pct_revenue",
    ],

    # ── Typical escalations ──
    "typical_escalations": ["GUIDANCE_HISTORY", "CADENCE_TABLE", "BASELINE_FORECAST"],
    "usually_overkill": ["BRIDGE_ANALYSIS", "EXTERNAL_DATA"],
}


GENERAL_DRIVERS = {
    "sector": "general",
    "description": "General-purpose model for any company with a P&L. "
                   "Revenue = prior × (1 + growth%). Costs = COGS + OpEx buckets. "
                   "Works for hardware, industrial, pre-profit, or any structure "
                   "that doesn't fit a specialized schema.",

    "revenue_model": "simple_growth",
    "revenue_inputs": ["revenue_growth_pct"],
    "revenue_doc": "revenue = prior_revenue × (1 + revenue_growth_pct / 100)",

    "cost_buckets": [
        {"name": "cogs",     "prior_key": "cogs_pct",    "variable_pct": 0.85,
         "delta_key": "cogs_delta_bps",    "scales_with": "revenue", "above_gross": True},
        {"name": "opex",     "prior_key": "opex_pct",    "variable_pct": 0.50,
         "delta_key": "opex_delta_bps",    "scales_with": "revenue", "above_gross": False},
    ],
    "gross_profit_label": "gross_profit",
    "gross_margin_label": "gross_margin_pct",

    "below_line": {
        "stock_comp": {"prior_key": "stock_comp_m", "growth_key": "stock_comp_growth_pct", "default_growth": None},
        "da":         {"prior_key": "da_m",         "growth_key": "da_growth_pct", "default_growth": 5.0},
    },

    "key_metrics": ["revenue_growth", "gross_margin", "operating_margin", "free_cash_flow"],
    "typical_escalations": ["BASELINE_FORECAST", "CADENCE_TABLE"],
    "usually_overkill": ["BRIDGE_ANALYSIS"],
}


# ── Registry ──
DRIVER_REGISTRY = {
    "restaurant": RESTAURANT_DRIVERS,
    "franchise_restaurant": FRANCHISE_DRIVERS,
    "software": SOFTWARE_DRIVERS,
    "general": GENERAL_DRIVERS,
}


def get_driver_schema(sector: str) -> dict:
    """Look up a driver schema by sector name."""
    schema = DRIVER_REGISTRY.get(sector)
    if not schema:
        raise ValueError(
            f"No driver schema for sector '{sector}'. "
            f"Available: {list(DRIVER_REGISTRY.keys())}"
        )
    return schema
