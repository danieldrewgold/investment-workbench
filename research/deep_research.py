"""
Deep Research — Single Rich Claude API Call

This module makes ONE Claude API call that does qualitative reasoning:
  - Business understanding (what does this company do, how does it make money)
  - Schema reasoning (which economic structure fits)
  - Driver selection (what 2-4 drivers matter most for the forward estimate)
  - Assumption setting with basis (citing specific evidence from the filing)
  - Contradiction generation (real tensions in the data)
  - Bear revision design (how to stress-test)

IMPORTANT: Claude does NOT extract numbers from text. It receives
structured financials from Polygon/Alpha Vantage as input.
The numbers are already known. Claude's job is to reason about
what drives them forward.

The output is a ResearchBrief dataclass that the pipeline feeds
directly into the estimate engine.
"""

from __future__ import annotations

import os
import json
import httpx
from dataclasses import dataclass, field
from research.financials_fetcher import StructuredFinancials


ANTHROPIC_API_KEY = (
    os.environ.get("ANTHROPIC_API_KEY", "")
    or "***KEY-REMOVED-FROM-HISTORY***"
)
MODEL = "claude-sonnet-4-20250514"


@dataclass
class ResearchBrief:
    """Contract between upstream thinking and downstream modeling."""
    # Business understanding
    company_name: str = ""
    business_description: str = ""
    economic_structure: str = ""
    key_debate: str = ""

    # Edge hypothesis (the core of the research)
    edge_hypothesis: str = ""           # "Street underestimates SSS because..."
    edge_type: str = ""                 # EXPECTATION_GAP / VALUATION_GAP / QUALITY_GAP / DURATION_GAP
    why_market_is_wrong: str = ""       # specific reasoning about the mispricing
    consensus_assumptions: dict = field(default_factory=dict)
    # {driver_name: "what street likely assumes and why"}
    guidance_vs_our_view: dict = field(default_factory=dict)
    # {driver_name: "guidance says X, we think Y because Z"}

    # Schema
    schema_type: str = ""               # "restaurant", "franchise", "software", "general"
    schema_reasoning: str = ""

    # Drivers with real basis
    drivers: list = field(default_factory=list)
    # Each: {name, assumption_key, formula, unit, components: [{name, value, unit, confidence, basis}]}

    # Extra assumptions (below-line items, productivity, etc.)
    extra_assumptions: dict = field(default_factory=dict)

    # Contradictions from the filing
    contradictions: list = field(default_factory=list)
    # Each: {thesis, counter_evidence, severity, affected_driver}

    # Bear revisions (derived from contradictions)
    bear_revisions: list = field(default_factory=list)
    # Each: {driver, component, new_value, reason}

    # Readiness
    evidence_gaps: list = field(default_factory=list)
    confidence_notes: str = ""

    # Metadata
    source_method: str = ""             # "claude_api", "registry_fallback"
    raw_response: str = ""


def build_research_brief(
    ticker: str,
    financials: StructuredFinancials,
    earnings_text: str,
    consensus_eps: float = None,
    consensus_revenue_m: float = None,
    verbose: bool = False,
) -> ResearchBrief:
    """
    Make one rich Claude API call to produce a ResearchBrief.

    Claude receives:
      1. Structured financials (exact numbers from Polygon/AV)
      2. Earnings text (for qualitative context, guidance, management commentary)
      3. Available schema types with EXACT assumption keys

    Claude produces:
      1. Business understanding
      2. Schema selection with reasoning
      3. Forward drivers with component-level assumptions and basis
      4. Contradictions found in the data
      5. Bear revisions for stress testing
    """
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        if verbose:
            print("  No ANTHROPIC_API_KEY")
        return ResearchBrief(source_method="no_api_key")

    prompt = _build_prompt(ticker, financials, earnings_text, consensus_eps, consensus_revenue_m)

    if verbose:
        print(f"  Deep research: calling Claude ({MODEL})...")

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 4000,
                # Low temperature — research brief extraction needs stable
                # outputs, not creativity. At default (1.0) we were seeing
                # $3.96 stdev across 6 runs on the same ticker. 0.2 keeps
                # the brief deterministic-ish while preserving nuance.
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )

        if resp.status_code != 200:
            if verbose:
                print(f"  Deep research: API error {resp.status_code}")
            return ResearchBrief(source_method="claude_api_error")

        data = resp.json()
        text = data["content"][0]["text"]

        if verbose:
            print(f"  Deep research: parsing response ({len(text)} chars)")

        brief = _parse_response(text, ticker, financials)
        brief.raw_response = text
        brief.source_method = "claude_api"

        # Validate and fix common issues
        _validate_brief(brief, verbose)

        if verbose:
            print(f"  Deep research: {brief.schema_type} schema, "
                  f"{len(brief.drivers)} drivers, "
                  f"{len(brief.contradictions)} contradictions")

        return brief

    except Exception as e:
        if verbose:
            print(f"  Deep research: error - {e}")
        pass  # fall through to return error brief
        return ResearchBrief(source_method="claude_api_error")


# Key name mappings for common Claude mistakes
_KEY_ALIASES = {
    # Claude sometimes uses longer names; map to what the engine expects
    "new_store_count": "new_restaurants",
    "new_stores": "new_restaurants",
    "net_new_stores": "new_restaurants",
    "net_new_openings": "new_restaurants",
    "research_dev_delta_bps": "rd_delta_bps",
    "sales_marketing_delta_bps": "sm_delta_bps",
    "general_admin_delta_bps": "ga_delta_bps",
    "sga_delta_bps_key": "sga_delta_bps",
}

# Valid assumption keys per schema
_VALID_DRIVER_KEYS = {
    "restaurant": {"sss_growth_pct", "new_restaurants", "food_cost_delta_bps",
                    "labor_cost_delta_bps", "other_cost_delta_bps"},
    "franchise_restaurant": {"sss_growth_pct", "new_restaurants"},
    "software": {"net_retention_pct", "new_arr_growth_pct"},
    "general": {"revenue_growth_pct"},
}


def _validate_brief(brief: ResearchBrief, verbose: bool = False):
    """
    Validate and fix common issues in Claude's response.
    Fixes key name mismatches, clamps extreme values, ensures
    extra_assumptions have required fields.
    """
    schema = brief.schema_type

    # Fix driver assumption_key aliases
    for driver in brief.drivers:
        key = driver.get("assumption_key", "")
        if key in _KEY_ALIASES:
            if verbose:
                print(f"  Fixing key: {key} -> {_KEY_ALIASES[key]}")
            driver["assumption_key"] = _KEY_ALIASES[key]

    # Fix extra_assumptions key aliases
    fixed_extra = {}
    for k, v in brief.extra_assumptions.items():
        fixed_extra[_KEY_ALIASES.get(k, k)] = v
    brief.extra_assumptions = fixed_extra

    # Move misplaced cost delta drivers into extra_assumptions
    # (Claude sometimes puts cost deltas as separate drivers instead of extra_assumptions)
    cost_delta_keys = {"cogs_delta_bps", "sm_delta_bps", "rd_delta_bps", "ga_delta_bps",
                       "opex_delta_bps", "cos_delta_bps", "sga_delta_bps",
                       "food_cost_delta_bps", "labor_cost_delta_bps", "other_cost_delta_bps"}
    valid_driver_keys = _VALID_DRIVER_KEYS.get(schema, set())

    drivers_to_keep = []
    for driver in brief.drivers:
        key = driver.get("assumption_key", "")
        if key in cost_delta_keys and key not in valid_driver_keys:
            # Move to extra_assumptions instead
            total = sum(c.get("value", 0) for c in driver.get("components", []))
            brief.extra_assumptions[key] = total
            if verbose:
                print(f"  Moved {key}={total} from driver to extra_assumptions")
        else:
            drivers_to_keep.append(driver)
    brief.drivers = drivers_to_keep

    # Clamp extreme cost deltas (anything over 200 bps is suspicious)
    for key in list(brief.extra_assumptions.keys()):
        if "delta_bps" in key:
            val = brief.extra_assumptions[key]
            if abs(val) > 200:
                clamped = max(min(val, 200), -200)
                if verbose:
                    print(f"  Clamping {key}: {val} -> {clamped}")
                brief.extra_assumptions[key] = clamped

    # Validate bear_revisions reference valid drivers
    valid_driver_names = {d["name"] for d in brief.drivers}
    valid_revisions = []
    for rev in brief.bear_revisions:
        if rev.get("driver") in valid_driver_names:
            # Check component exists
            driver = next((d for d in brief.drivers if d["name"] == rev["driver"]), None)
            if driver:
                comp_names = {c["name"] for c in driver.get("components", [])}
                if rev.get("component") in comp_names:
                    valid_revisions.append(rev)
    brief.bear_revisions = valid_revisions


def _build_prompt(ticker: str, fin: StructuredFinancials, earnings_text: str,
                  consensus_eps: float = None, consensus_revenue_m: float = None) -> str:
    """Build the single rich prompt for Claude."""

    # Consensus context block
    consensus_block = ""
    if consensus_eps:
        consensus_block = f"""
CONSENSUS CONTEXT (this is what the street currently expects):
  Consensus forward EPS: ${consensus_eps:.2f}
  {"Consensus revenue: $" + f"{consensus_revenue_m:,.1f}M" if consensus_revenue_m else ""}

Your job is NOT just to build an estimate. It is to find WHERE THE MARKET IS WRONG.
Ask yourself: What does the street assume for each driver to get ${consensus_eps:.2f} EPS?
Where is that assumption vulnerable? What evidence from the filing suggests the street
is too high or too low on a specific driver?
"""

    return f"""You are an equity research analyst building a forward earnings estimate for {ticker}.
Your PRIMARY objective is to FIND THE EDGE -- where does the market's consensus view
have a blind spot, and what specific evidence supports a different view?

You are given EXACT structured financials (do NOT change these numbers). Your job is:
1. Understand the business and identify what drives value
2. Figure out what the street assumes and WHERE IT MIGHT BE WRONG
3. Build a forward estimate grounded in specific evidence
4. Identify the tensions and contradictions in the data
5. Design stress tests for your own assumptions

{fin.to_summary_text()}
{consensus_block}
EARNINGS/FILING TEXT (for qualitative context, guidance, management commentary):
{earnings_text}

AVAILABLE ECONOMIC SCHEMAS (you MUST use the EXACT assumption_key names listed below):

1. "restaurant" -- Company-operated restaurants.
   Revenue = existing_stores x AUV x (1+SSS) + new_stores x AUV x productivity x 0.5
   REQUIRED driver assumption_keys:
     - "sss_growth_pct": same-store sales growth (decompose into traffic + ticket components)
     - "new_restaurants": new store openings (unit: count)
   REQUIRED cost delta_bps keys (positive = cost increase, negative = cost decrease):
     - "food_cost_delta_bps", "labor_cost_delta_bps", "other_cost_delta_bps"
   REQUIRED extra_assumptions:
     - "new_store_productivity": 0.70-0.85
     - "cash_ga_growth_pct", "stock_comp_growth_pct"

2. "franchise_restaurant" -- Franchise-heavy chain.
   Revenue = system_wide_sales x (royalty_rate + ad_fund_rate) + company_owned + supply_chain
   REQUIRED driver assumption_keys: "sss_growth_pct", "new_restaurants"
   REQUIRED extra_assumptions: "royalty_rate_pct", "ad_fund_rate_pct", "company_owned_stores",
     "company_sss_pct", "cos_delta_bps", "sga_delta_bps", "stock_comp_growth_pct", "da_growth_pct"

   PITCH DISCIPLINE FOR RESTAURANT/RETAIL SSS (mandatory for "restaurant" and "franchise_restaurant"):
   If sss_growth_pct is positive (the "easy comps" case), you MUST address these 5 questions
   in the basis fields of the sss_growth_pct components, OR explicitly list them in evidence_gaps:
     1. What % of the comp came from new product vs. core business?
     2. Is the new product a repeat occasion or trial occasion?
     3. Does the company own the category (or is it new entry)?
     4. Is management investing in the product (media spend, operational capacity)?
     5. What's the quantified comp drag if it laps the negative period next year?
   RULE: Never pitch easy comps without decomposing what created the hard comps.
   If 3+ questions are unanswerable from filings/transcripts, lower confidence on sss_growth_pct
   to 0.40 or below and add the unanswered questions to evidence_gaps.

3. "software" -- SaaS/subscription.
   Revenue = prior_rev x (net_retention_pct/100) + prior_rev x (new_arr_growth_pct/100)
   REQUIRED driver assumption_keys: "net_retention_pct", "new_arr_growth_pct"
   REQUIRED cost delta_bps keys: "cogs_delta_bps", "sm_delta_bps", "rd_delta_bps", "ga_delta_bps"
   REQUIRED extra_assumptions: "stock_comp_growth_pct", "da_growth_pct"

4. "general" -- Any other P&L.
   Revenue = prior_rev x (1 + revenue_growth_pct/100)
   REQUIRED driver assumption_keys: "revenue_growth_pct"
   REQUIRED cost delta_bps keys: "cogs_delta_bps", "opex_delta_bps"
   REQUIRED extra_assumptions: "stock_comp_growth_pct", "da_growth_pct"

Respond in EXACTLY this JSON format (no markdown, no explanation outside the JSON):
{{
  "business_description": "2-3 sentences: what the company does, how it makes money",
  "economic_structure": "1-2 sentences: revenue model, key economic characteristics",
  "key_debate": "1-2 sentences: what the market is arguing about",

  "edge_hypothesis": "1-2 sentences: your specific thesis about what the market is getting wrong. Be precise -- not 'revenue might beat' but 'street underestimates ticket growth because menu pricing is sticky post-tariff and mix shift to premium items adds 0.5pp that consensus doesn't model'",
  "edge_type": "EXPECTATION_GAP|VALUATION_GAP|QUALITY_GAP|DURATION_GAP|BEHAVIORAL_GAP",
  "why_market_is_wrong": "2-3 sentences: specific reasoning about WHY consensus is vulnerable. Cite evidence from the filing. What is the street anchored on that may not hold?",

  "consensus_assumptions": {{
    "driver_name": "What the street likely assumes for this driver and why (1-2 sentences)"
  }},
  "guidance_vs_our_view": {{
    "driver_name": "Management guides X. We think Y because Z. (1-2 sentences)"
  }},

  "schema_type": "restaurant|franchise_restaurant|software|general",
  "schema_reasoning": "Why this schema fits",

  "drivers": [
    {{
      "name": "driver_name",
      "assumption_key": "MUST be one of the REQUIRED keys listed above for your chosen schema",
      "formula": "how components combine (e.g. traffic + ticket)",
      "unit": "pct|bps|count",
      "components": [
        {{
          "name": "component_name",
          "value": 0.0,
          "unit": "pct|bps|count",
          "confidence": 0.5,
          "basis": "Multi-sentence basis citing SPECIFIC evidence from the filing, written with INLINE ATTRIBUTION suitable for a research note — phrases like 'In the Q3 2025 call, CEO Brian Niccol said X' or 'The 10-Q disclosed Y' or 'The 8-K filed 2026-01-15 showed Z'. Do NOT use footnote markers like [1]. Write prose that reads naturally when dropped into a research document. Higher confidence (0.65+) requires multiple data points. Lower confidence (0.35-0.50) when extrapolating from limited data."
        }}
      ]
    }}
  ],

  "extra_assumptions": {{}},

  "contradictions": [
    {{
      "thesis": "What the bull case assumes",
      "counter_evidence": "SPECIFIC evidence from the filing that challenges it -- quote numbers, trends, management language",
      "severity": "serious|moderate|minor",
      "affected_driver": "which driver this challenges"
    }}
  ],

  "bear_revisions": [
    {{
      "driver": "driver_name (must match a driver name from your drivers list)",
      "component": "component_name (must match a component name from that driver)",
      "new_value": 0.0,
      "reason": "Why this bear scenario is plausible, citing evidence"
    }}
  ],

  "evidence_gaps": ["What SPECIFIC information we don't have that would change the estimate"],
  "confidence_notes": "Overall assessment: how strong is the edge? What's the biggest risk to this thesis?"
}}

CRITICAL RULES:
1. You MUST use the EXACT assumption_key names listed above. Do NOT invent new key names.
2. Drivers are FORWARD-LOOKING for the NEXT fiscal year.
3. Cost delta_bps: positive = costs INCREASE, negative = DECREASE. 100 bps = 1pp. Keep realistic (-100 to +100 typical).
4. Confidence scoring: 0.70+ requires 3+ specific data points from filing. 0.50-0.70 requires clear directional evidence. 0.30-0.50 is extrapolation or general industry view.
5. Bear revisions must EXACTLY match your driver/component names.
6. The edge_hypothesis is THE MOST IMPORTANT FIELD. If you can't articulate a specific edge, say "No clear edge identified -- estimate is close to consensus."
7. For consensus_assumptions: reason about what the street MUST be assuming to get their EPS number. Where is that assumption most fragile?
8. For guidance_vs_our_view: compare management guidance to your own view. If they diverge, explain why. If management historically guides conservatively, note that.

==================================================================
GUIDANCE ANCHORING (mandatory -- this is how analysts actually work)
==================================================================
9. ANCHOR ON GUIDANCE. For every driver whose assumption_key could map to something management guides (revenue_growth_pct, sss_growth_pct, net_retention_pct, new_restaurants, gross_margin_pct, operating_margin_pct, etc.):
   a. In the `basis` field, STATE MANAGEMENT'S MOST RECENT GUIDANCE for that driver in a specific quote ("On the Q3 2026 call, CFO guided FY26 revenue of $6.65-6.70B").
   b. STATE YOUR VALUE relative to guide ("our +11% growth is ~700bps BELOW the guide midpoint of +18%").
   c. JUSTIFY THE DEVIATION with a specific failure-mode OR confirmation-mode mechanism ("we model guide-break because X launched weak, management cited Y headwind in Q&A, analyst Z pressed on W and got a hedging answer"). Vague reasons like "conservative modeling" or "general caution" are NOT acceptable.

10. GUIDANCE DEVIATION BAR. If your driver value is >500bps away from management guidance:
   a. The thesis MUST hinge on a CONCRETE, NAMED failure mode (a specific product miss, cost program slippage, competitive response, macro signal) — NOT a general feeling.
   b. If you can't name the mechanism, MOVE CLOSER TO GUIDE and lower confidence. Do not pitch variance you can't defend.
   c. If you're BELOW guide (bearish) while management just RAISED guide, your `why_market_is_wrong` must explain why management is wrong or sandbagging-reversing. This is the rarest edge — demand extraordinary evidence.

11. GUIDANCE VS STREET. When consensus is ABOVE guide, say so explicitly in consensus_assumptions: "Street sits 300bps above guide midpoint, implicitly fading management's conservatism." Your job is to decide whether the street's implied fade is earned or not, and take a side.

12. SHORT-SIDE DISCIPLINE. A short pitch when management is RAISING guidance is statistically rare. If you're producing one, make sure the bear thesis is sharp and specific (cite the exact transcript passage, metric, or contradiction). If you only have general skepticism, the pitch is NOT a short — it's "close to consensus with a tilt."
"""


def _parse_response(text: str, ticker: str, fin: StructuredFinancials) -> ResearchBrief:
    """Parse Claude's JSON response into a ResearchBrief."""
    # Try to extract JSON from the response
    text = text.strip()
    if text.startswith("```"):
        # Strip markdown code fences
        lines = text.split("\n")
        start = 1
        end = len(lines)
        for i, line in enumerate(lines):
            if i > 0 and line.strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                return ResearchBrief(
                    source_method="claude_api_parse_error",
                    confidence_notes=f"Failed to parse JSON response: {text[:200]}",
                )
        else:
            return ResearchBrief(source_method="claude_api_parse_error")

    brief = ResearchBrief(
        company_name=f"{ticker}",
        business_description=data.get("business_description", ""),
        economic_structure=data.get("economic_structure", ""),
        key_debate=data.get("key_debate", ""),
        edge_hypothesis=data.get("edge_hypothesis", ""),
        edge_type=data.get("edge_type", ""),
        why_market_is_wrong=data.get("why_market_is_wrong", ""),
        consensus_assumptions=data.get("consensus_assumptions", {}),
        guidance_vs_our_view=data.get("guidance_vs_our_view", {}),
        schema_type=data.get("schema_type", "general"),
        schema_reasoning=data.get("schema_reasoning", ""),
        drivers=data.get("drivers", []),
        extra_assumptions=data.get("extra_assumptions", {}),
        contradictions=data.get("contradictions", []),
        bear_revisions=data.get("bear_revisions", []),
        evidence_gaps=data.get("evidence_gaps", []),
        confidence_notes=data.get("confidence_notes", ""),
    )

    return brief


