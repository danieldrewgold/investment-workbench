"""
CapitalAllocationFramework subagent.

Decks — especially investor day decks — usually have a dedicated slide
spelling out capital allocation priorities: leverage target, dividend
policy, buyback authorization, M&A criteria, capex intensity, ROIC
thresholds. This is the company's self-stated capital discipline.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "capital_allocation_framework"


SYSTEM_PROMPT = """You are the CapitalAllocationFramework subagent. You
analyze investor deck slides and extract management's stated capital
allocation framework.

What to look for:
  - A slide titled "Capital allocation", "Capital priorities",
    "Financial policy", "Balance sheet"
  - A priority-ordered list of uses (typically: reinvest → M&A →
    dividend → buyback, but the ORDER is the signal)
  - Quantified targets: leverage range, dividend payout %,
    buyback authorization dollars, minimum ROIC on new investments
  - Historical capital returns charts showing cumulative buyback /
    dividend / capex over multiple years

Companies that DON'T return capital (high-growth) will say so
explicitly — that's also a data point. Capture the posture either way."""


USER_PROMPT_TEMPLATE = """Extract {ticker}'s capital allocation framework
from the deck.

OUTPUT JSON SCHEMA:
{{
  "stated_priorities": [
    {{
      "rank": 1,
      "priority": "e.g., 'Reinvest in organic growth',
                   'Strategic M&A', 'Return to shareholders'",
      "quantified_criterion": "e.g., '>15% ROIC', 'bolt-on <$500M',
                                '50% payout target'",
      "source_page": 36,
      "evidence_quote": "<=60 words verbatim slide text"
    }}
  ],

  "leverage_policy": {{
    "target_range_or_level": "verbatim (e.g., '2.5-3.5x net leverage',
                               '<1x net debt', 'investment-grade ratings')",
    "current_level_if_shown": "verbatim or empty",
    "policy_framing": "verbatim (e.g., 'maintain flexibility for M&A',
                       'gradually delever to 2x by 2028')",
    "source_page": 37,
    "evidence_quote": "<=60 words verbatim"
  }},

  "dividend_policy": {{
    "policy_statement": "verbatim (e.g., 'grow dividend in line with
                         earnings', '50% payout ratio', 'no dividend')",
    "current_amount_if_shown": "verbatim or empty",
    "growth_commitment_if_stated": "verbatim (e.g., 'mid-single-digit
                                     growth', 'double-digit CAGR')",
    "source_page": 38,
    "evidence_quote": "<=60 words verbatim"
  }},

  "buyback_policy": {{
    "authorization_dollars": "verbatim (e.g., '$2B authorization
                               through 2027', 'ongoing opportunistic')",
    "remaining_on_program": "verbatim or empty",
    "framing": "opportunistic | systematic | programmatic | ASR |
                minimum_level | none | not_specified",
    "anti_dilution_stated": "if any mention of offsetting SBC dilution",
    "source_page": 38,
    "evidence_quote": "<=60 words verbatim"
  }},

  "m_and_a_criteria": {{
    "posture": "actively_hunting | opportunistic | discipline_focused |
                digesting_recent | no_m_and_a",
    "size_preference": "verbatim (e.g., 'bolt-on', '<$500M',
                        'transformational')",
    "return_criteria": "verbatim (e.g., '>15% IRR', 'accretive year 1',
                        'strategic fit prioritized')",
    "source_page": 39,
    "evidence_quote": "<=60 words verbatim"
  }},

  "capex_intensity": {{
    "level": "verbatim (e.g., '~5% of revenue', '$800M/yr',
              'stepping up to support growth')",
    "trajectory": "rising | stable | falling",
    "categories_prioritized": ["new unit builds", "technology", "capacity"],
    "source_page": 40,
    "evidence_quote": "<=60 words verbatim"
  }},

  "cumulative_returns_shown": {{
    "total_capital_returned_to_shareholders": "verbatim if shown (e.g.,
                                                '$12B cumulative FY2020-FY2025')",
    "breakdown_if_shown": "e.g., '$8B buybacks, $4B dividends'",
    "source_page": 42,
    "evidence_quote": "<=60 words verbatim",
    "visual_evidence": "describe the cumulative-returns chart if present"
  }},

  "summary": {{
    "framework_posture_one_sentence": "e.g., 'Prioritize reinvestment +
                                        opportunistic M&A; return excess
                                        via dividend growth + opportunistic
                                        buybacks; leverage 2-3x',
                                        'Growth-mode: reinvest all cash,
                                        no dividend, no buyback'",
    "quantified_commitments_count": 0,
    "most_specific_commitment": "one sentence — most precisely-quantified
                                  commitment (e.g., '50% payout ratio by
                                  FY28 per slide 38')",
    "notable_silences": "what you'd expect to be stated but isn't
                         (e.g., 'no explicit buyback authorization
                         dollar amount', 'no leverage target given')"
  }}
}}

RULES:
1. Only capture what's EXPLICIT on a slide. If the deck doesn't have
   a capital allocation slide, leave sections empty — don't infer
   from prior years.
2. Priority ORDER matters — rank by how mgmt shows them on the slide.
3. Capex + M&A are separate tracks; don't conflate.
4. Growth-stage companies without capital return programs — note
   explicitly in summary.
{evidence_block}

Respond with the JSON object only."""


def run_capital_allocation_framework(
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
