"""
AI Schema Builder

Instead of picking from a fixed menu of schemas, reads the extracted
observations and builds a tailored model structure for this specific company.

Uses Claude API to:
  1. Identify the revenue model type
  2. Identify the specific cost buckets from the P&L
  3. Extract prior-year values for each bucket
  4. Identify below-line items
  5. Extract key constants (tax rate, shares, interest)

Returns a complete driver_schema dict + prior_year dict that the
ModelSpec engine can consume directly.
"""

import json
import os
import urllib.request


BUILD_SCHEMA_PROMPT = """You are building a financial model structure for a specific company based on extracted observations from its earnings release.

Your job: read the observations and produce a tailored model configuration that captures THIS company's actual P&L structure.

Return ONLY a JSON object with these fields:

{
  "company_description": "One sentence describing the business model",
  "revenue_model": "simple_growth",
  "cost_buckets": [
    {
      "name": "<cost line name, lowercase, underscored>",
      "prior_pct": <this cost as % of prior year revenue, e.g. 29.8>,
      "variable_pct": <0.0 to 1.0, how much scales with revenue vs fixed>,
      "above_gross": <true if this is cost of revenue/COGS, false if operating expense>,
      "delta_bps": <expected change in bps for next year, positive = cost increase>
    }
  ],
  "below_line": {
    "stock_comp_m": <stock-based compensation in $M, or 0>,
    "da_m": <depreciation & amortization in $M, or 0>,
    "stock_comp_growth_pct": <expected growth %, or 0>,
    "da_growth_pct": <expected growth %, or 5>
  },
  "prior_year": {
    "revenue_m": <prior year total revenue in $M>,
    "store_count": <number of locations/stores, or 1 if not applicable>
  },
  "constants": {
    "tax_rate": <effective tax rate as decimal, e.g. 0.22>,
    "shares_m": <diluted shares in millions>,
    "net_interest_m": <net interest income(+) or expense(-) in $M>
  },
  "revenue_growth_estimate_pct": <estimated next-year revenue growth %>
}

RULES:
- Extract ACTUAL numbers from the observations. Do not guess.
- Cost buckets should match the company's actual P&L line items.
- For restaurants: use food, labor, occupancy, other_operating (above_gross=true), then G&A below.
- For SaaS: use cost_of_revenue (above_gross=true), then sales_marketing, research_dev, general_admin.
- For hardware/manufacturing: use cost_of_goods_sold (above_gross=true), then R&D, SG&A.
- For franchise: use cost_of_sales (above_gross=true), ad_expense, sga.
- variable_pct: 0.9+ for COGS/food, 0.5-0.7 for S&M, 0.3-0.5 for R&D/G&A, 0.05 for rent/occupancy.
- If D&A or stock comp is already included in the cost bucket percentages, set below_line values to 0.
- delta_bps: positive = cost goes up as % of revenue. Negative = operating leverage.
- If the company is unprofitable, still model the cost structure accurately.
- net_interest_m: negative for companies with debt (interest expense), positive for cash-rich companies.

Here are the extracted observations:

"""


def build_schema_from_observations(
    observations: list,
    api_key: str = None,
) -> tuple:
    """
    Use Claude API to build a tailored schema from extracted observations.
    
    Returns (driver_schema, prior_year, constants, revenue_growth_pct) or
    None if the API call fails.
    """
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    obs_text = "\n".join(
        f"- [{obs.get('type', '?')}] {obs.get('value', obs.get('text', ''))[:150]}"
        for obs in observations
    )

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": BUILD_SCHEMA_PROMPT + obs_text}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read())

        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        config = json.loads(text)

        # Build driver_schema dict that ModelSpec can consume
        cost_buckets = []
        prior_year = config.get("prior_year", {})
        prior_year["store_count"] = prior_year.get("store_count", 1)

        for i, bucket in enumerate(config.get("cost_buckets", [])):
            name = bucket["name"]
            prior_key = f"{name}_pct"
            delta_key = f"{name}_delta_bps"

            # Store the prior-year percentage in prior_year dict
            prior_year[prior_key] = bucket.get("prior_pct", 0)

            cost_buckets.append({
                "name": name,
                "prior_key": prior_key,
                "variable_pct": bucket.get("variable_pct", 0.5),
                "delta_key": delta_key,
                "scales_with": "revenue",
                "above_gross": bucket.get("above_gross", False),
            })

        # Build below_line
        bl = config.get("below_line", {})
        prior_year["stock_comp_m"] = bl.get("stock_comp_m", 0)
        prior_year["da_m"] = bl.get("da_m", 0)

        driver_schema = {
            "sector": "custom",
            "description": config.get("company_description", "Custom schema"),
            "revenue_model": "simple_growth",
            "revenue_inputs": ["revenue_growth_pct"],
            "revenue_doc": "revenue = prior_revenue × (1 + growth%)",
            "cost_buckets": cost_buckets,
            "gross_profit_label": "gross_profit",
            "gross_margin_label": "gross_margin_pct",
            "below_line": {
                "stock_comp": {
                    "prior_key": "stock_comp_m",
                    "growth_key": "stock_comp_growth_pct",
                    "default_growth": bl.get("stock_comp_growth_pct"),
                },
                "da": {
                    "prior_key": "da_m",
                    "growth_key": "da_growth_pct",
                    "default_growth": bl.get("da_growth_pct", 5.0),
                },
            },
            "key_metrics": ["revenue_growth", "gross_margin", "operating_margin"],
            "typical_escalations": ["BASELINE_FORECAST"],
            "usually_overkill": [],
        }

        constants = config.get("constants", {})
        revenue_growth = config.get("revenue_growth_estimate_pct", 10.0)

        # Build extra_assumptions from cost deltas
        extra_assumptions = {
            "revenue_growth_pct": revenue_growth,
            "stock_comp_growth_pct": bl.get("stock_comp_growth_pct", 0),
            "da_growth_pct": bl.get("da_growth_pct", 5.0),
        }
        for bucket in config.get("cost_buckets", []):
            delta_key = f"{bucket['name']}_delta_bps"
            extra_assumptions[delta_key] = bucket.get("delta_bps", 0)

        return driver_schema, prior_year, constants, extra_assumptions

    except Exception as e:
        return None
