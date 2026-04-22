"""
LRPExtractor subagent — long-range plan / algorithm.

Every mature public company ends an investor day with "our algorithm":
a stacked slide showing revenue growth %, margin expansion bps, tax
rate, shares buyback, and the implied EPS CAGR. These are THE numbers
management commits to over a multi-year horizon.

Also captures segment-level LRP targets and strategic milestones tied
to specific years.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "lrp_extractor"


SYSTEM_PROMPT = """You are the LRPExtractor. You analyze investor
presentation slides and extract the company's LONG-RANGE PLAN — the
multi-year algorithm management has publicly committed to.

Classic signals:
  - A slide titled "Algorithm", "Long-term targets", "Financial
    framework", "Path to $X", "Our model", "Long-term ambition"
  - A stacked/waterfall visual showing %s: revenue growth + margin
    expansion + buyback/tax = EPS growth
  - Specific-year targets: "$X revenue by 2028", "25% op margin by 2027"
  - Milestone slides: "Path to X units", "Road to X% penetration"

Your output lets an analyst answer: what is management publicly on the
hook for, and by when?

You cite source_page + evidence_quote (verbatim slide text) for every
number. Paraphrasing is forbidden."""


USER_PROMPT_TEMPLATE = """Extract {ticker}'s long-range plan / algorithm
from the deck.

OUTPUT JSON SCHEMA:
{{
  "algorithm_lines": [
    {{
      "line_item": "e.g., 'Revenue growth', 'Operating margin expansion',
                    'Net share repurchase', 'Tax rate', 'EPS growth'",
      "target_value": "verbatim (e.g., 'high-single-digit to low-double-digit',
                       '+50-75 bps per year', '~2% reduction annually')",
      "period": "FY2026-FY2028 | long-term | next 3 years | through 2030",
      "source_page": 12,
      "evidence_quote": "<=60 words verbatim slide text",
      "visual_evidence": "describe the chart if the number is visual (else empty)"
    }}
  ],

  "dated_milestones": [
    {{
      "milestone": "one phrase (e.g., '$10B revenue', '1,500 units', 'EBITDA doubling')",
      "target_year": "2028 | 2030 | empty if relative",
      "current_state_if_shown": "e.g., 'at $6B in FY25' or empty",
      "source_page": 28,
      "evidence_quote": "<=60 words verbatim",
      "significance": "one sentence analyst read"
    }}
  ],

  "segment_or_business_line_targets": [
    {{
      "segment": "e.g., 'International', 'Digital', 'Streaming',
                  'Now Assist', 'Venue Nation'",
      "metric": "revenue | margin | penetration | units",
      "target": "verbatim value",
      "period": "...",
      "source_page": 18,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "strategic_pillars": [
    {{
      "pillar": "short label (e.g., 'International expansion',
                 'AI monetization', 'Digital transformation')",
      "description": "one sentence mgmt's framing, verbatim-derived",
      "linked_to_metric": "which algo line this supports (if shown)",
      "source_page": 8,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "summary": {{
    "has_explicit_algorithm_slide": true,
    "algorithm_slide_page": 12,
    "top_line_algo_summary": "one sentence: what does the algorithm say?
                              (e.g., '+8-10% revenue, +50bps margin/yr,
                              ~2%/yr buyback → ~15-20% EPS CAGR through FY28')",
    "biggest_structural_commitment": "one sentence: the longest-dated or
                                       most quantifiable commitment on the deck",
    "credibility_signal": "strong | moderate | weak",
    "credibility_reason": "one sentence — e.g., 'Prior algo slides show
                            track record of hitting targets' or 'First LRP
                            issued, no track record' or 'Last algo from 2021
                            was lowered in 2023'"
  }}
}}

RULES:
1. Do NOT conflate near-term guidance (FY2026) with the LRP — LRP is
   multi-year by definition. If unsure, prefer the guide extractor.
2. If the deck has NO long-range algorithm slide, set
   has_explicit_algorithm_slide=false and leave algorithm_lines empty.
3. Segment-level LRP is rare but high-signal — capture it when present.
4. Credibility reasoning uses only what's ON the deck (e.g., "page 32
   shows prior 2023 target of X, current achievement Y"). Do NOT opine
   using external knowledge.
{evidence_block}

Respond with the JSON object only."""


def run_lrp_extractor(
    ticker: str,
    pages: list[PageImage],
    *,
    verbose: bool = False,
) -> DeckSubagentResult:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        ticker=ticker, evidence_block=EVIDENCE_BLOCK_VISION,
    )
    return call_vision_subagent(
        subagent_name=SUBAGENT_NAME,
        ticker=ticker,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        page_images=pages,
        max_tokens=6500,
        temperature=0.15,
        verbose=verbose,
    )
