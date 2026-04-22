"""
SegmentEconomicsExtractor subagent.

Segment-level data, unit economics, cohort retention, and returns analysis
usually live on CHART slides in investor decks — these numbers rarely
make it into the press release prose or transcript with this level of
granularity. Vision is essential here.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "segment_economics_extractor"


SYSTEM_PROMPT = """You are the SegmentEconomicsExtractor. You analyze
investor deck slides and extract:

  1. SEGMENT-LEVEL P&L DETAIL — revenue + margin by reportable segment
     or business line, trajectory over time
  2. UNIT ECONOMICS — per-unit / per-customer / per-location economics
     (AUV, ARPU, CAC, LTV, payback, contribution margin per unit)
  3. COHORT / RETENTION data — cohort curves, net retention by vintage,
     returning-customer %
  4. RETURNS ANALYSIS — ROIC / ROIIC by vintage or segment, payback on
     new investments / new unit openings

These data points are the highest-signal content typically ONLY shown on
decks, not in prose. Extract them precisely.

You're allowed — and encouraged — to describe chart contents in
visual_evidence when the numbers appear in bars/columns without written
labels. Be specific: "bar chart on page 18 shows segment revenue growth
trajectory from ~$2B FY21 to ~$3.2B FY25, with each year labeled"."""


USER_PROMPT_TEMPLATE = """Extract segment-level and unit-economics data
from {ticker}'s deck.

OUTPUT JSON SCHEMA:
{{
  "segment_pnl": [
    {{
      "segment": "e.g., 'Domestic restaurants', 'International licensing',
                  'Subscription', 'Advertising', 'Streaming'",
      "metric": "revenue | gross margin | operating margin | EBITDA margin",
      "values_over_time": [
        {{"period": "FY2023", "value": "verbatim number or description"}},
        {{"period": "FY2024", "value": "..."}},
        {{"period": "FY2025", "value": "..."}}
      ],
      "trajectory": "improving | stable | deteriorating | volatile",
      "source_page": 18,
      "evidence_quote": "<=80 words verbatim slide text / chart labels",
      "visual_evidence": "describe the chart if numbers are primarily visual"
    }}
  ],

  "unit_economics": [
    {{
      "metric": "e.g., 'AUV per store', 'ARPU', 'CAC', 'LTV',
                 'Payback period', 'Contribution margin per unit'",
      "value_or_range": "verbatim (e.g., '$3.2M AUV', '$5.75 ARPU',
                          '<18 month payback')",
      "current_vs_prior": "e.g., 'up from $2.9M in FY22' or 'stable vs prior year'",
      "source_page": 22,
      "evidence_quote": "<=60 words verbatim",
      "visual_evidence": "describe chart if visual"
    }}
  ],

  "cohort_or_retention_data": [
    {{
      "metric": "e.g., 'Net revenue retention', 'Year-1 retention',
                 'Returning guest %', 'Cohort lifetime revenue'",
      "value_or_curve": "verbatim number or describe the curve
                         (e.g., '110-115% NRR', '60% of FY18 cohort
                         still active in FY25')",
      "cohort_vintages_shown": ["FY2020", "FY2021", "FY2022"],
      "trajectory": "improving | stable | deteriorating",
      "source_page": 28,
      "evidence_quote": "<=60 words verbatim",
      "visual_evidence": "describe the cohort chart if primarily visual"
    }}
  ],

  "returns_analysis": [
    {{
      "metric": "e.g., 'ROIIC', 'ROIC', 'IRR on new units',
                 'Payback on remodel'",
      "value": "verbatim",
      "vintage_or_segment": "e.g., 'FY2023 new units', 'International vs Domestic'",
      "benchmark_if_shown": "e.g., 'vs 15% cost of capital', empty",
      "source_page": 35,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "capacity_utilization_or_productivity": [
    {{
      "metric": "e.g., 'Restaurants per market', 'AWA (avg wait accepted)',
                 'Seats utilized', 'Network transmission %'",
      "current_value": "verbatim",
      "trajectory": "improving | stable | deteriorating",
      "source_page": 31,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "summary": {{
    "strongest_segment_trend": "one sentence: best-performing segment
                                 and its trajectory, verbatim-anchored",
    "weakest_segment_trend": "one sentence",
    "most_interesting_unit_econ_data_point": "one sentence citing
                                              page + metric",
    "cohort_retention_health": "strong | moderate | weak | not_shown",
    "notable_gaps": "metrics you'd expect a company this type to show
                      but didn't (e.g., 'no per-unit AUV trend shown')"
  }}
}}

RULES:
1. Prefer data that's IN the deck, not inferred. Chart visuals count
   but describe them precisely in visual_evidence.
2. If the deck doesn't break out segments (very short earnings deck),
   return empty segment_pnl — don't fabricate.
3. Cohort data is rare but THE HIGHEST SIGNAL when present — triple-check
   the numbers.
4. Don't double-count: if segment revenue appears on 3 slides, record
   once with the richest source page.
{evidence_block}

Respond with the JSON object only."""


def run_segment_economics_extractor(
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
        max_tokens=7500,
        temperature=0.1,   # extraction-heavy
        verbose=verbose,
    )
