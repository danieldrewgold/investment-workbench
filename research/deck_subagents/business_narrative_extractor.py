"""
BusinessNarrativeExtractor subagent.

Decks — especially investor day decks — tell a story. Management chooses
a narrative arc: "here's our moat → here are our growth pillars → here's
the market → here's why we'll win → here's the algorithm." Capturing
that narrative gives the research brain the company's self-framed thesis.

This is distinct from transcript_subagents.business_understanding which
reads transcripts; decks offer richer framing (visual, polished,
deliberately curated) of the same story.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "business_narrative_extractor"


SYSTEM_PROMPT = """You are the BusinessNarrativeExtractor. You read
investor decks and capture the COMPANY'S OWN STORY as management has
chosen to frame it.

Your output helps an analyst answer: what is management's thesis about
why this company will win? Where do they say the durable advantages
come from? What is the order of arguments they've chosen to make?

You look for:
  - Opening framing slides ("Why invest", "Our opportunity", "Key
    takeaways" at the top of the deck)
  - Moat / durable advantage claims (often on slides titled "Competitive
    advantages", "Our differentiation")
  - Growth pillar slides (the 3-5 things the company says will drive
    growth — often with icons / side-by-side framing)
  - Flywheel / business model diagrams (visual loops showing how scale
    compounds)
  - Market / TAM slides (how big the opportunity is, per mgmt)
  - "Why now" slides (timing argument — tech inflection, demographic
    shift, category maturation)

You don't opine on whether the narrative is right. You CATALOG it
with verbatim quotes and page references. Downstream adversarial
review will stress-test it."""


USER_PROMPT_TEMPLATE = """Extract the company narrative as {ticker}
has chosen to present it in this deck.

OUTPUT JSON SCHEMA:
{{
  "opening_framing": {{
    "source_pages": [1, 2, 3],
    "elevator_pitch": "one-sentence verbatim-grounded summary of the
                      opening slides (what they lead with)",
    "key_takeaways_verbatim": "<=150 words verbatim from any early
                               'key takeaways' or 'why invest' slide",
    "tone": "aspirational | confident | defensive | transformational"
  }},

  "moat_and_differentiation": [
    {{
      "claim": "one-phrase moat claim (e.g., 'Only national scaled ticketing
                platform', 'Platform effects from developer ecosystem')",
      "evidence_mgmt_offers": "what mgmt cites as proof (e.g., 'market
                              share data, customer retention curves')",
      "source_page": 8,
      "evidence_quote": "<=60 words verbatim slide text",
      "moat_type": "scale | network_effects | brand | switching_costs | data | regulatory | IP | other"
    }}
  ],

  "growth_pillars": [
    {{
      "pillar": "short label (e.g., 'International expansion',
                 'Digital adoption', 'AI monetization', 'Unit growth')",
      "framing": "one sentence verbatim-derived how mgmt positions it",
      "quantitative_anchor_if_any": "e.g., '1,500 units by 2030',
                                       'DMS penetration from 15% to 40%'",
      "source_page": 14,
      "evidence_quote": "<=60 words verbatim",
      "ranking_by_deck_emphasis": 1
    }}
  ],

  "flywheel_or_business_model_diagram": {{
    "present": true,
    "source_page": 6,
    "description": "one sentence describing the visual loop/diagram",
    "nodes": ["data", "product", "customers", "scale"],
    "significance": "what mgmt is trying to convey with this visual"
  }},

  "market_and_tam_framing": [
    {{
      "market": "e.g., 'Global live entertainment', 'US transplant',
                 'Enterprise workflow automation'",
      "tam_stated": "verbatim number (e.g., '$300B+ TAM', '~$50B SAM')",
      "growth_rate_stated": "verbatim",
      "current_penetration_stated": "verbatim if given",
      "source_page": 10,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "why_now_argument": {{
    "present": true,
    "source_page": 4,
    "thesis": "one sentence: why mgmt says NOW is the moment (tech
               inflection, demographic, regulatory, category shift)",
    "evidence_quote": "<=80 words verbatim"
  }},

  "summary": {{
    "story_in_one_paragraph": "3-4 sentence synthesis of what the deck
                                is pitching, using mgmt's own framing and
                                grounded in verbatim slide content",
    "narrative_arc": "short label for the overall story (e.g.,
                      'scale + category ownership → margin expansion',
                      'AI inflection → platform leverage')",
    "strongest_part_of_narrative": "one sentence: where is the pitch
                                    most anchored in specifics?",
    "weakest_part_of_narrative": "one sentence: where is it most hand-wavy?
                                  (e.g., 'flywheel diagram has no quantified
                                  feedback loops')"
  }}
}}

RULES:
1. This is ABOUT mgmt's self-framing, not your opinion. Report the
   story they're telling.
2. Every claim grounded in a verbatim slide quote + page citation.
3. Rank growth pillars by deck emphasis (page count, visual prominence,
   ordering) — not by your external judgment.
4. If the deck is short (quarterly earnings deck with no narrative
   slides), return minimal results — don't force content.
{evidence_block}

Respond with the JSON object only."""


def run_business_narrative_extractor(
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
        temperature=0.2,     # narrative synthesis — slightly higher temp
        verbose=verbose,
    )
