"""
QTDExtractor subagent.

Extracts quarter-to-date (QTD) commentary from earnings calls with
VERBATIM wording preservation. The exact phrasing carries magnitude:

  "QTD is off to an excellent start" >> "QTD is tracking to our plan"
      >> "QTD is solid"
      >> "QTD is in line with expectations"

Each of these has a different implication for the print. The subagent's
job is to pull the verbatim language and classify magnitude, NOT to
paraphrase.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "qtd_extractor"


SYSTEM_PROMPT = """You are the QTDExtractor subagent. You find every
quarter-to-date (QTD) remark in earnings call transcripts and preserve
the EXACT wording. Paraphrasing is forbidden — the precise language is
the signal.

QTD commentary almost always appears in:
  - Prepared remarks toward the end of the CFO section (outlook paragraph)
  - The first 2-3 Q&A responses (analysts ask "how's the quarter trending?")
  - Responses to explicit "what are you seeing so far" questions

Magnitude ladder (approximate; use your judgment on borderline cases):
  VERY_POSITIVE   - "off to an excellent/exceptional start", "meaningfully
                    ahead of our plan", "significantly exceeding"
  POSITIVE        - "strong start", "outperforming our plan", "ahead of
                    where we expected"
  MILDLY_POSITIVE - "solid start", "tracking above our plan"
  NEUTRAL         - "in line with our plan", "tracking to our expectations",
                    "on pace", "consistent with"
  MILDLY_NEGATIVE - "slightly below our plan", "a bit softer than expected"
  NEGATIVE        - "below our plan", "weaker than expected", "challenging"
  VERY_NEGATIVE   - "materially below", "significantly weaker"

If management REFUSES to give QTD ("we don't comment on intra-quarter
trends"), that's also a data point — capture it.

You do NOT track full-quarter guides (GuidanceTracker does). You only
track commentary about the CURRENT (partial) quarter."""


USER_PROMPT_TEMPLATE = """Extract every quarter-to-date (QTD) remark from
{ticker}'s earnings calls.

OUTPUT JSON SCHEMA:
{{
  "qtd_remarks": [
    {{
      "source_quarter": "Q3 2026",
      "qtd_period": "Q4 2026 (the forward quarter they're giving color on)",
      "remark_verbatim": "<=80 words verbatim",
      "magnitude": "VERY_POSITIVE | POSITIVE | MILDLY_POSITIVE | NEUTRAL | MILDLY_NEGATIVE | NEGATIVE | VERY_NEGATIVE | REFUSED_TO_COMMENT",
      "magnitude_reasoning": "one sentence pointing to the exact word(s) that carry the magnitude (e.g., 'excellent start' carries VERY_POSITIVE)",
      "metrics_mentioned": ["traffic", "average check", "sales", "bookings"],
      "metric_details": [
        {{
          "metric": "traffic",
          "direction": "up | down | flat | not_specified",
          "magnitude_language": "verbatim phrase (e.g., 'up low-single digits')",
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q3 2026",
          "speaker": "ceo|cfo|coo"
        }}
      ],
      "evidence_quote": "<=80 words verbatim — the full QTD remark",
      "speaker": "ceo|cfo|coo|other",
      "context": "prepared_remarks | qanda_response | other"
    }}
  ],

  "qtd_language_changes_over_time": [
    {{
      "observation": "one sentence (e.g., 'QTD language downgraded from \\'strong start\\' Q2 to \\'tracking to plan\\' Q3')",
      "significance": "one sentence analyst interpretation",
      "evidence_chain": [
        {{"source_quarter": "Q2 2026", "qtd_period": "Q3 2026", "phrase": "strong start", "speaker": "cfo"}},
        {{"source_quarter": "Q3 2026", "qtd_period": "Q4 2026", "phrase": "tracking to plan", "speaker": "cfo"}}
      ]
    }}
  ],

  "quarters_with_no_qtd_commentary": [
    {{
      "quarter": "Q1 2026",
      "reason": "not_asked | management_declined | not_applicable",
      "evidence_quote_if_declined": "<=50 words verbatim or empty",
      "speaker": "..."
    }}
  ],

  "summary": {{
    "total_qtd_remarks_captured": 0,
    "most_recent_qtd_magnitude": "one of the magnitude values above",
    "most_recent_qtd_phrase": "verbatim phrase",
    "trend_direction": "improving | stable | deteriorating | mixed | n/a"
  }}
}}

RULES:
1. Preserve VERBATIM wording. Any paraphrase is a bug. The magnitude
   classification depends on the specific words used.
2. Only include remarks about the CURRENT (partial) quarter — not the next
   full quarter or fiscal year. "QTD" means "quarter-to-date".
3. If a remark is ambiguous between full-quarter guide and QTD color,
   don't include it — let GuidanceTracker take it.
4. "REFUSED_TO_COMMENT" is valid data — capture refusals with evidence.
5. If no QTD commentary exists in a quarter, mark it in
   `quarters_with_no_qtd_commentary` so we know you looked.
6. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_qtd_extractor(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
        max_tokens=5000,
        temperature=0.15,
        verbose=verbose,
    )
