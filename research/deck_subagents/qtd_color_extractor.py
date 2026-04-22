"""
QTDColorExtractor subagent — only runs for deck_type == 'earnings'.

Quarterly earnings decks often tuck QTD commentary on a late slide
("Outlook" / "Current Trends") with metric-specific color not in the
press release. Precise wording matters: "strong start" vs "tracking
to plan" vs "in line" carry different implications.

Mirrors transcript_subagents.qtd_extractor but reads the deck visually.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "qtd_color_extractor"


SYSTEM_PROMPT = """You are the QTDColorExtractor. You analyze earnings
deck slides and find every statement about the CURRENT (in-progress)
quarter — QTD trends, early reads, "so far this quarter" language.

Wording carries magnitude. Transcribe verbatim and classify:
  VERY_POSITIVE — "off to an exceptional start", "materially ahead"
  POSITIVE      — "strong start", "ahead of plan", "encouraging"
  MILDLY_POSITIVE — "solid", "tracking above plan"
  NEUTRAL       — "in line with expectations", "on plan", "on pace"
  MILDLY_NEGATIVE — "a bit softer", "slightly below"
  NEGATIVE      — "challenging", "below plan"
  VERY_NEGATIVE — "materially weaker"

QTD-specific metric hints (e.g., "April traffic +LSD") are especially
high-signal — capture them separately.

If this isn't a quarterly earnings deck and QTD isn't addressed,
return empty lists — don't force content that isn't there."""


USER_PROMPT_TEMPLATE = """Find every QTD (quarter-to-date) statement in
{ticker}'s deck.

OUTPUT JSON SCHEMA:
{{
  "qtd_statements": [
    {{
      "source_page": 22,
      "qtd_period": "the in-progress quarter being referenced (e.g., 'Q2 2026')",
      "remark_verbatim": "<=80 words exact slide text",
      "magnitude": "VERY_POSITIVE | POSITIVE | MILDLY_POSITIVE | NEUTRAL | MILDLY_NEGATIVE | NEGATIVE | VERY_NEGATIVE | REFUSED_TO_COMMENT",
      "magnitude_reasoning": "one sentence citing the exact phrase that carries the magnitude",
      "metric_hints": [
        {{
          "metric": "traffic | ticket | bookings | ARR | daily active users | ...",
          "direction": "up | down | flat",
          "magnitude_language": "verbatim phrase (e.g., 'up low-single digits')",
          "source_page": 22
        }}
      ],
      "evidence_quote": "<=80 words verbatim slide text"
    }}
  ],

  "summary": {{
    "qtd_addressed_on_deck": true,
    "most_recent_qtd_magnitude": "one of the magnitude values above",
    "most_recent_qtd_phrase": "verbatim phrase",
    "notes": "one sentence — e.g., 'Deck shows QTD traffic + AOV separately on slide 22' or 'No QTD color on deck'"
  }}
}}

RULES:
1. Only include QTD content about the CURRENT in-progress quarter.
   Do NOT include full-year or next-quarter guides — those are for other
   subagents.
2. Preserve exact wording. Any paraphrase is a bug.
3. If the deck is NOT a quarterly earnings deck (e.g., investor day),
   return empty qtd_statements and set qtd_addressed_on_deck=false.
{evidence_block}

Respond with the JSON object only."""


def run_qtd_color_extractor(
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
        max_tokens=4500,
        temperature=0.1,
        verbose=verbose,
    )
