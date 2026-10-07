"""
BusinessUnderstanding subagent.

Builds a factual model of HOW THE COMPANY ACTUALLY MAKES MONEY based on
what management describes in calls. NOT investor-deck spin — the working
mental model an analyst needs: revenue streams, unit economics, competitive
positioning, real operational KPIs, and what management says is the moat.

This is foundation data for every other form of analysis. The other 7
subagents can reference "the business is X" without re-deriving it.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "business_understanding"


SYSTEM_PROMPT = """You are the BusinessUnderstanding subagent. Your job is
to extract, from management's own words across multiple quarters, a
factual working model of how the business operates.

What you're building is NOT a marketing summary. It's an analyst's mental
model:
  - How does revenue actually flow in (by segment, by product, by customer
    type)?
  - What are the unit economics management discusses (margin per unit,
    customer acquisition cost, payback, etc.)?
  - What operational KPIs does management track internally (not just what
    they report externally)?
  - What does management claim is the moat / durability story, and what
    evidence do they cite?
  - What are the KEY DRIVERS of revenue/margin/growth as management describes
    them?

You ONLY report what management SAID (verbatim-grounded). You do not
infer things they didn't say. If you can't cite a quote, omit the claim.

You do NOT track guide numbers (GuidanceTracker), tone (ToneTracker),
metric emphasis (MetricsHighlighted), or capital returns (CapitalAllocation).
You build the WHAT-IS mental model."""


USER_PROMPT_TEMPLATE = """Build a factual business model of {ticker} from
management's own words across the calls.

OUTPUT JSON SCHEMA:
{{
  "revenue_streams": [
    {{
      "stream": "e.g., 'Premium subscription revenue', 'Concert ticketing net',
                'Sponsorship'",
      "description": "one sentence in plain terms",
      "approximate_mix_if_stated": "e.g., '~65% of total revenue per Q2 call'",
      "economics_notes": "one sentence on pricing/contract structure if stated",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo|coo"
    }}
  ],

  "unit_economics_discussed": [
    {{
      "metric": "e.g., 'ARPU', 'gross margin per show', 'CAC payback'",
      "value_or_range_if_stated": "e.g., '$5.75 blended ARPU', 'mid-teens margin',
                                  'not disclosed'",
      "trajectory": "improving | stable | deteriorating | mixed | n/a",
      "management_commentary": "one sentence on what mgmt says about this metric",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q2 2026",
      "speaker": "ceo|cfo|coo"
    }}
  ],

  "key_drivers_per_management": [
    {{
      "driver": "e.g., 'pricing', 'attachment rate', 'stadium show mix',
                'Now Assist adoption'",
      "importance_per_mgmt": "primary | secondary | tertiary",
      "directional_impact_near_term": "tailwind | headwind | neutral | mixed",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo"
    }}
  ],

  "competitive_positioning": [
    {{
      "positioning_claim": "e.g., 'only scaled venue operator', 'category
                           owner in fast-casual Mexican'",
      "evidence_cited_by_mgmt": "what proof points mgmt offers",
      "competitor_named_if_any": "e.g., 'none', 'AEG', 'Spotify', 'Qualtrics'",
      "analyst_credibility_read": "strong | moderate | weak",
      "credibility_reason": "one sentence",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo"
    }}
  ],

  "moat_claims": [
    {{
      "moat_type": "scale | network | brand | switching_costs | supply_advantage | data | regulatory | other (specify)",
      "claim": "one sentence mgmt's claim in plain terms",
      "proof_points_cited": ["what mgmt offers as evidence"],
      "durability_read": "structural | cyclical | unclear",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo"
    }}
  ],

  "operational_kpis_mgmt_tracks": [
    {{
      "kpi": "e.g., 'app open rate', 'venue utilization', 'net new subs'",
      "current_value_or_range": "if stated",
      "how_mgmt_uses_it": "one sentence",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo|coo"
    }}
  ],

  "summary": {{
    "one_sentence_business_model": "in plain english, not mgmt spin",
    "primary_revenue_engine": "which stream dominates",
    "primary_growth_driver_currently": "what mgmt says is THE driver right now",
    "biggest_unit_economics_concern_from_mgmt": "if mgmt flagged a unit
      econ concern, surface it here (one sentence); else 'none flagged'"
  }}
}}

RULES:
1. Report only what mgmt SAID. Do not infer from financials or industry
   knowledge. This subagent's output is verbatim-sourced only.
2. "one_sentence_business_model" should be a plain-english description an
   intern could understand, built from mgmt's actual phrasing.
3. For "competitive_positioning", you DO make an analyst judgment on
   credibility — but ground it in whether mgmt offered proof points or not,
   not in your external knowledge of competitors.
4. "key_drivers_per_management" ranks by how mgmt talks about them
   (frequency + emphasis + explicit statements like "the key driver"),
   not by your judgment of what should be important.
5. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_business_understanding(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        ticker=pack.ticker,
        evidence_block=EVIDENCE_SCHEMA_BLOCK,
        context_pack=pack.to_subagent_text(),
    )
    return call_subagent(
        subagent_name=SUBAGENT_NAME,
        ticker=pack.ticker,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=12000,  # 4+ quarters of business detail truncated at 6K
        temperature=0.20,
        verbose=verbose,
    )
