"""
NewInitiativesExtractor subagent.

Decks are often where companies FIRST publicly disclose new programs:
a new geography, a new product category, an AI initiative, a strategic
partnership, a category entry. These first-public disclosures are
classic edge sources — if you catch them before the sell-side models
update, you have the setup asymmetry the research brain is looking for.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "new_initiatives_extractor"


SYSTEM_PROMPT = """You are the NewInitiativesExtractor. You identify
programs, products, geographies, partnerships, category entries, and
other strategic initiatives that are shown on the deck as new or
upcoming.

These are the things management is BETTING ON that aren't already
reflected in historical P&L. They're high-signal for building a
variant vs. consensus.

Signal patterns:
  - Slides titled "What's next", "New initiatives", "Recent launches",
    "Growth investments", "Strategic priorities"
  - Named programs with dates (e.g., "OCS Enhanced Part A — launching Q3")
  - Visual roadmaps showing launches / milestones by quarter or year
  - Geographic expansion announcements ("Entering 5 new markets in FY26")
  - New product categories entering test / pilot / scaled rollout

Classify each initiative by maturity: announcement | pilot | test |
scaled_rollout | launched. Pilot/test stage is earliest-signal,
scaled_rollout is most concrete.

You do NOT opine on whether the initiative will succeed. You CATALOG
what's publicly disclosed with dates and scale language."""


USER_PROMPT_TEMPLATE = """Identify new/upcoming initiatives in {ticker}'s
deck.

OUTPUT JSON SCHEMA:
{{
  "initiatives": [
    {{
      "name": "short label (e.g., 'OCS Enhanced Part A',
                'Venue Nation North Expansion', 'Now Assist Fleet')",
      "category": "new_product | new_geography | new_segment |
                   new_partnership | new_channel | new_capability |
                   M&A_integration | technology_platform | other",
      "description": "one-sentence mgmt-framed description",
      "maturity_stage": "announcement | pilot | test | scaled_rollout |
                          launched | roadmap_mention",
      "timing": "verbatim timing language (e.g., 'launching Q3 2026',
                 'pilot in 2H 2025', 'by end of 2026', 'multi-year')",
      "target_scale_or_opportunity": "verbatim framing of size / impact
                                        (e.g., '$500M TAM', '15% of stores
                                        by 2028', 'not quantified')",
      "source_page": 24,
      "evidence_quote": "<=80 words verbatim slide text",
      "visual_evidence": "describe visual roadmap / timeline / chart
                           if relevant"
    }}
  ],

  "strategic_partnerships_or_alliances": [
    {{
      "partner": "named partner if given, else empty",
      "purpose": "one sentence mgmt-framed purpose",
      "source_page": 19,
      "evidence_quote": "<=60 words verbatim",
      "maturity": "announced | active | expanded | exploratory"
    }}
  ],

  "product_roadmap_or_launches_by_period": [
    {{
      "period": "Q3 2025 | 1H 2026 | FY2027 | ongoing",
      "items": [
        "short item description tied to that period"
      ],
      "source_page": 25,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "geographic_expansion": [
    {{
      "geography": "country / region",
      "stage": "entering | early | scaling | mature",
      "current_vs_target_if_shown": "verbatim",
      "source_page": 32,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "summary": {{
    "total_initiatives_flagged": 0,
    "earliest_stage_count": "count of announcement/pilot/test stage items",
    "highest_conviction_initiative_per_mgmt_framing": "one sentence:
                                                       which one does mgmt
                                                       frame as most important
                                                       (by deck emphasis /
                                                       ordering / detail)",
    "most_quantified_initiative": "one sentence: the initiative with the
                                    clearest size/TAM/target",
    "initiatives_without_timing": "count — mgmt that flags something
                                     without a date is a softer commitment",
    "notable_absent_initiative_categories": "what you'd expect to see but
                                              don't (e.g., 'no AI / gen AI
                                              initiatives on deck despite
                                              industry context')"
  }}
}}

RULES:
1. An "initiative" must be (a) named / described specifically and
   (b) presented as new or upcoming (not a mature business line).
2. Distinguish between roadmap MENTIONS (brief reference in a list)
   and dedicated slides (much stronger signal).
3. If an initiative has NO timing attached, capture it but flag in
   the summary as a softer commitment.
4. M&A is only an initiative if it's framed forward-looking
   ("integrating our recent acquisition" → yes; "we acquired X
   in 2019" → no, that's history).
5. Timing language matters — "launching Q3" > "in development" >
   "exploring opportunities".
{evidence_block}

Respond with the JSON object only."""


def run_new_initiatives_extractor(
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
        max_tokens=7000,
        temperature=0.2,
        verbose=verbose,
    )
