"""
ToneTracker subagent.

Tracks management tone across multi-quarter earnings calls:
  - Per-quarter tone read with verbatim evidence
  - Change vs prior quarter (inflection points)
  - Speaker-level differences (CEO vs CFO can diverge — meaningful)
  - Hedging language density (modal-verb frequency trend)
  - Language shifts on specific topics ("strong" -> "solid" -> "fine" is a downgrade)

Tone inflections often precede numbers. A CEO who went from confident to
hedging in Q3 is telling you Q4 will miss.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "tone_tracker"


SYSTEM_PROMPT = """You are the ToneTracker subagent. You identify the
emotional and linguistic posture of management across multi-quarter earnings
calls, with VERBATIM evidence for every assessment.

Tone is specific, not vague. "Cautious" without a quote is useless.
"'We're navigating a challenging environment while remaining focused on
controllables' — CFO, Q3" is a cautious tone observation backed by
evidence.

Pay special attention to:
  - Language DOWNGRADES on the same topic over time (e.g., performance
    described as "outstanding" in Q1, "strong" in Q2, "solid" in Q3 —
    that's a meaningful slide even if no number changed)
  - Hedging verb frequency increasing ("should" / "believe" / "expect"
    replacing "will" / "are" / "have")
  - CEO vs CFO tone divergence within the same call
  - Tone specifically about the next quarter (QTDExtractor handles QTD
    content, but you track POSTURE around forward statements)

You do not track guide numbers (GuidanceTracker does that) or metric
emphasis (MetricsHighlighted does that). You track how management SOUNDS."""


USER_PROMPT_TEMPLATE = """Extract management tone across {ticker}'s earnings
calls.

OUTPUT JSON SCHEMA:
{{
  "per_quarter_tone": [
    {{
      "quarter": "Q3 2026",
      "overall_tone": "confident | cautiously_optimistic | mixed | hedging | cautious | defensive | bearish",
      "tone_reasoning": "one sentence citing specific phrases",
      "speaker_tones": {{
        "ceo": {{
          "tone": "...",
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q3 2026",
          "speaker": "ceo"
        }},
        "cfo": {{
          "tone": "...",
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q3 2026",
          "speaker": "cfo"
        }}
      }},
      "ceo_cfo_divergence": "aligned | ceo_more_bullish | cfo_more_bullish",
      "divergence_reason_if_any": "one sentence or empty"
    }}
  ],

  "inflection_points": [
    {{
      "between_quarters": ["Q2 2026","Q3 2026"],
      "topic": "short label (e.g., 'mobile growth', 'labor cost outlook')",
      "shift_type": "upgrade | downgrade | confidence_loss | confidence_gain | topic_dropped | new_caveat",
      "prior_language": "verbatim how they talked about it before",
      "new_language": "verbatim how they talk about it now",
      "significance": "one-sentence analyst interpretation",
      "prior_evidence": {{"evidence_quote":"<=50 words","source_quarter":"Q2 2026","speaker":"ceo|cfo"}},
      "new_evidence": {{"evidence_quote":"<=50 words","source_quarter":"Q3 2026","speaker":"ceo|cfo"}}
    }}
  ],

  "hedging_language_patterns": [
    {{
      "observation": "e.g., 'Increased use of should/believe/expect in prepared remarks Q3 vs Q2'",
      "trend_direction": "hedging_rising | hedging_falling | stable",
      "example_evidence": [
        {{"evidence_quote":"<=50 words","source_quarter":"...","speaker":"..."}}
      ]
    }}
  ],

  "language_downgrades": [
    {{
      "topic": "e.g., 'same-store sales performance'",
      "language_over_time": [
        {{"quarter": "Q1 2026", "adjective_or_phrase": "outstanding", "evidence_quote":"...","speaker":"ceo"}},
        {{"quarter": "Q2 2026", "adjective_or_phrase": "strong", "evidence_quote":"...","speaker":"ceo"}},
        {{"quarter": "Q3 2026", "adjective_or_phrase": "solid", "evidence_quote":"...","speaker":"ceo"}}
      ],
      "direction": "downgrade | upgrade | stable",
      "significance": "one sentence analyst read"
    }}
  ],

  "tone_trajectory_summary": {{
    "overall_direction": "improving | stable | deteriorating | volatile",
    "most_recent_tone": "confident | cautiously_optimistic | mixed | hedging | cautious | defensive | bearish",
    "biggest_shift_last_2_quarters": "one sentence citing the most material inflection, or 'no material shift'",
    "ceo_credibility_signal": "strong | moderate | weak",
    "ceo_credibility_reason": "one sentence"
  }}
}}

RULES:
1. Every tone assessment needs a verbatim quote. "Cautious" by itself is a bug.
2. An "inflection point" requires the PRIOR language AND the NEW language,
   both verbatim. If you only have one side, don't list it.
3. Language downgrades: track the SAME TOPIC across quarters, not different
   things that happen to be discussed. The adjective/phrase used MUST refer
   to the same referent (e.g., Q1 "outstanding margins" vs Q3 "solid margins"
   is valid; Q1 "outstanding execution" vs Q3 "solid margins" is NOT).
4. Speaker-level tone is separate from overall tone — capture divergence
   when it exists; mark aligned when CEO and CFO sound similar.
5. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_tone_tracker(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
        max_tokens=6500,
        temperature=0.25,   # judgment-heavier than pure extraction
        verbose=verbose,
    )
