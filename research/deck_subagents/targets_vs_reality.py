"""
TargetsVsReality subagent.

Good decks show a "track record" slide that explicitly compares prior
targets to what was actually delivered: "2021 target of 15% margin by
2024 → achieved 15.4%." These slides are management's own credibility
evidence — harder to spin than narrative claims.

Also catches forward commitments framed as "we committed to X at our
2022 investor day, and here's where we are" — useful for building a
multi-year credibility track.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "targets_vs_reality"


SYSTEM_PROMPT = """You are the TargetsVsReality subagent. You analyze
investor deck slides looking for SELF-STATED TRACK RECORD content:

  - Slides comparing PRIOR targets (from an earlier investor day / LRP /
    annual shareholder letter) to CURRENT actual results
  - "We said we would X; we delivered Y" framings
  - Multi-year commitment charts with bar-by-bar "target vs achieved"
  - Algo reconciliation slides: "our framework expected X; we did Y"
  - Progress checkmarks / green-yellow-red scorecards on stated
    commitments

These slides are the strongest objective signal of management
credibility because they can't easily be spun — the prior target is
on record.

Also capture: if the deck RENEWS a commitment by stating "and
continuing, we commit to Z through 2028" — that's a fresh anchor.

You DO NOT opine on whether the track record is good. You catalog it."""


USER_PROMPT_TEMPLATE = """Find every "targets vs reality" claim in
{ticker}'s deck.

OUTPUT JSON SCHEMA:
{{
  "track_record_items": [
    {{
      "commitment_period": "when was the original target set
                            (e.g., '2022 Investor Day', 'FY2023 guide')",
      "metric": "revenue | margin | unit count | ROIC | ...",
      "original_target": "verbatim (e.g., '15% operating margin by 2024',
                          '$5B revenue by FY25')",
      "actual_result": "verbatim (e.g., '15.4% achieved in FY24',
                        '$5.2B delivered')",
      "outcome": "beat | met | missed | partially_met | ongoing | rescinded",
      "outcome_magnitude": "e.g., 'beat by 40bps', 'missed by $500M', empty",
      "source_page": 26,
      "evidence_quote": "<=80 words verbatim slide text",
      "visual_evidence": "if the comparison is shown as chart bars,
                          describe them precisely"
    }}
  ],

  "fresh_commitments_renewed_or_new": [
    {{
      "commitment": "verbatim commitment text",
      "period": "FY2026-FY2028 | by 2030 | long-term",
      "renews_prior_commitment": "true if this continues a prior target
                                    (e.g., 'extending our 15% margin
                                    commitment to 17% by 2028')",
      "source_page": 43,
      "evidence_quote": "<=80 words verbatim"
    }}
  ],

  "scorecard_or_progress_indicators": [
    {{
      "commitment": "what was committed",
      "progress_indicator": "on-track | ahead | behind | achieved | rescinded",
      "visual_framing": "e.g., 'green/yellow/red indicator',
                          'checkmark/X', 'progress bar'",
      "source_page": 30,
      "evidence_quote": "<=60 words verbatim"
    }}
  ],

  "summary": {{
    "has_explicit_track_record_slide": true,
    "track_record_slide_pages": [26, 27],
    "count_beat": 0,
    "count_met": 0,
    "count_missed": 0,
    "count_ongoing": 0,
    "overall_credibility_read": "strong | moderate | weak | no_track_record",
    "overall_credibility_reason": "one sentence citing specific items
                                    (e.g., '3 of 4 FY22 targets beat or
                                    met per slide 26; margin commitment
                                    missed by 80bps')",
    "most_important_hit_or_miss": "one sentence: the most material target
                                     where mgmt either beat or missed,
                                     with verbatim evidence"
  }}
}}

RULES:
1. Only include items where BOTH the original target AND the actual
   result are shown on the deck. A standalone "we did 15% margin" is
   not a track record item unless there's a prior commitment it's
   being compared to.
2. If the deck has no track record slide, return empty
   track_record_items and set has_explicit_track_record_slide=false.
3. "Ongoing" outcome = commitment period hasn't ended yet, but current
   progress is shown.
4. For scorecards, describe the visual framing precisely so an analyst
   can tell whether mgmt is claiming green/yellow/red.
5. Be harsh-honest on credibility read — if 3 of 5 are missed, say so.
{evidence_block}

Respond with the JSON object only."""


def run_targets_vs_reality(
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
        max_tokens=6000,
        temperature=0.15,
        verbose=verbose,
    )
