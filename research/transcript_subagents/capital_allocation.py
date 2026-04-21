"""
CapitalAllocation subagent.

Tracks the full capital-returns story across multi-quarter calls:
  - Buybacks (pace, pricing, authorization)
  - Dividends (declarations, changes, policy signals)
  - M&A (closed deals, pipeline, explicit posture)
  - Debt (issuance, paydown, refinancing, leverage commentary)
  - CapEx (plans, changes, prioritization between categories)

How management views their own stock, and how they're deploying cash,
is one of the highest-signal inputs for a thesis — buybacks during a
guide-down are different from buybacks during a raise.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "capital_allocation"


SYSTEM_PROMPT = """You are the CapitalAllocation subagent. You extract
all capital-returns commentary from multi-quarter earnings calls:
buybacks, dividends, M&A, debt, capex. VERBATIM grounding always.

The signal you're after: how does management view their own stock right
now, and how are they deploying cash? Specific patterns to notice:
  - Buybacks suspended / deferred (bearish self-signal)
  - Buybacks opportunistically increased when shares down (bullish)
  - Dividend raises funded through debt (often late-cycle signal)
  - M&A posture: actively hunting vs. digesting vs. none
  - CapEx being pulled forward / pushed out relative to prior plan
  - Leverage commentary (comfort zone, target range)

You do NOT make recommendations. You catalog capital-allocation actions
and management's stated rationales, with verbatim evidence."""


USER_PROMPT_TEMPLATE = """Extract the complete capital-allocation picture
for {ticker} from the calls.

OUTPUT JSON SCHEMA:
{{
  "buyback_history": [
    {{
      "quarter": "Q3 2026",
      "action": "executed | authorized | increased_authorization | reduced_pace | suspended | resumed | commented",
      "dollar_amount": "e.g., '$150M', 'not disclosed'",
      "shares_repurchased": "e.g., '1.2M shares', 'not disclosed'",
      "avg_price_if_stated": "e.g., '$125', or empty",
      "program_status": "e.g., '$500M remaining on $2B authorization'",
      "stated_rationale": "mgmt's stated reason (verbatim or paraphrase)",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "cfo|ceo"
    }}
  ],

  "dividend_history": [
    {{
      "quarter": "Q3 2026",
      "action": "declared | raised | maintained | suspended | initiated | commented",
      "amount_per_share": "e.g., '$0.45', 'n/a'",
      "change_from_prior": "e.g., '+8% YoY', 'flat', 'first time'",
      "policy_signal_if_any": "e.g., 'committed to 50% payout ratio long-term', empty",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "cfo|ceo"
    }}
  ],

  "ma_activity": [
    {{
      "quarter": "Q3 2026",
      "posture": "actively_hunting | opportunistic | digesting | none | not_discussed",
      "deals_closed": ["short description of any closed deal, or empty"],
      "pipeline_commentary": "verbatim language about M&A pipeline if given",
      "size_preference_if_stated": "e.g., 'bolt-on under $500M', empty",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo"
    }}
  ],

  "debt_commentary": [
    {{
      "quarter": "Q3 2026",
      "action": "issued | repaid | refinanced | drawn_on_revolver | commented_on_leverage | none",
      "amount": "e.g., '$500M senior notes', 'not disclosed'",
      "terms_if_stated": "e.g., '6.25% due 2032'",
      "leverage_target": "e.g., '2.5-3.0x net leverage', empty",
      "current_leverage_if_stated": "e.g., '2.8x net debt to EBITDA'",
      "stated_rationale": "verbatim or paraphrase",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "cfo"
    }}
  ],

  "capex_commentary": [
    {{
      "quarter": "Q3 2026",
      "fy_plan_if_stated": "e.g., '$800M for FY26'",
      "change_from_prior_plan": "raised | lowered | maintained | no_prior_comparison",
      "category_prioritization": "what categories are being prioritized/deprioritized",
      "pull_forward_or_push_out_signals": "if mgmt signaled timing shifts, describe",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "cfo|coo"
    }}
  ],

  "capital_returns_posture": {{
    "current_quarter_posture": "aggressive_return | steady_return | preservation | deleveraging | accumulating | mixed",
    "buyback_signal_read": "bullish_self_signal | routine | bearish_self_signal | n/a",
    "buyback_signal_reason": "one sentence why (e.g., 'increased repurchase pace on stock weakness Q3')",
    "notable_shifts_last_4_quarters": "one sentence or 'none material'",
    "evidence_for_shifts": [
      {{"evidence_quote":"<=50 words","source_quarter":"...","speaker":"..."}}
    ]
  }},

  "summary": {{
    "total_buyback_dollars_tracked": "e.g., '$850M across 4 quarters', or 'not sufficiently disclosed'",
    "dividend_policy_currently": "growing | flat | none | suspended",
    "ma_posture_currently": "actively_hunting | opportunistic | digesting | none | not_discussed",
    "leverage_trajectory": "deleveraging | stable | releveraging | n/a",
    "most_recent_capital_allocation_framing": "one sentence: how mgmt described capital priorities on the most recent call (verbatim-grounded)"
  }}
}}

RULES:
1. Only include actions and commentary that appear in the transcripts.
   Do not pull from press releases or filings — another workflow handles
   those.
2. For each quarter, if a capital-allocation topic wasn't discussed,
   simply omit the record — don't create placeholder entries.
3. "buyback_signal_read" is an analyst judgment. Ground it in SPECIFIC
   evidence (stock price context + buyback pace change) from verbatim
   quotes. Default to "routine" if pattern is ambiguous.
4. Do not speculate about unstated dollar amounts. If mgmt said "we
   bought back shares" without a number, record it as "not disclosed".
5. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_capital_allocation(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
        max_tokens=5500,
        temperature=0.20,
        verbose=verbose,
    )
