"""
MetricsHighlighted subagent.

Tracks which metrics management CHOSE to emphasize across multi-quarter
calls. The order they lead with, what they drop, what they newly introduce
— all signals.

  - What mgmt opens with ("Let me start with...") = their pitch
  - What they stop talking about = often what's breaking
  - What they newly introduce = their new narrative framing
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "metrics_highlighted"


SYSTEM_PROMPT = """You are the MetricsHighlighted subagent. Your job is to
track the METRICS MANAGEMENT CHOSE TO DISCUSS across multi-quarter calls,
with particular attention to three signals:

  1. LEAD METRICS — what management opens with in prepared remarks. This
     is their self-framing of "here's what matters this quarter."
  2. DROPPED METRICS — metrics emphasized in prior quarters that don't
     appear this quarter. Often correlates with something breaking.
  3. NEWLY INTRODUCED METRICS — metrics that appear for the first time.
     Either a genuine new KPI or a narrative-management move.

You do NOT track guide numbers (GuidanceTracker) or QTD commentary
(QTDExtractor). You catalog metric emphasis patterns."""


USER_PROMPT_TEMPLATE = """Analyze metric emphasis across {ticker}'s earnings
calls.

OUTPUT JSON SCHEMA:
{{
  "lead_metrics_by_quarter": [
    {{
      "quarter": "Q3 2026",
      "opening_metrics": [
        {{
          "metric": "same-store sales",
          "order_mentioned": 1,
          "framing_language": "verbatim intro language (e.g., 'Starting with same-store sales, we delivered...')",
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q3 2026",
          "speaker": "ceo|cfo|coo"
        }}
      ],
      "overall_narrative_frame": "one sentence: what was this quarter's pitch (e.g., 'Margin resilience despite traffic softness')"
    }}
  ],

  "dropped_metrics": [
    {{
      "metric": "digital sales",
      "last_mentioned_quarter": "Q4 2025",
      "quarters_absent": ["Q1 2026","Q2 2026","Q3 2026"],
      "prior_emphasis_level": "high | medium | low",
      "analyst_interpretation": "one sentence on what the drop might indicate",
      "last_mention_evidence": {{
        "evidence_quote": "<=50 words verbatim of the last time it was discussed",
        "source_quarter": "Q4 2025",
        "speaker": "ceo|cfo"
      }}
    }}
  ],

  "newly_introduced_metrics": [
    {{
      "metric": "Venue Nation-built shows",
      "first_introduced_quarter": "Q2 2026",
      "introduction_framing": "verbatim language when first mentioned",
      "subsequent_mentions": ["Q2 2026","Q3 2026"],
      "likely_reason": "new_KPI | new_narrative_frame | segment_split | other",
      "reason_evidence": "one sentence",
      "first_mention_evidence": {{
        "evidence_quote": "<=50 words verbatim",
        "source_quarter": "Q2 2026",
        "speaker": "ceo|cfo|coo"
      }}
    }}
  ],

  "metric_emphasis_shifts": [
    {{
      "metric": "labor inflation",
      "prior_emphasis": "high | medium | low | absent",
      "current_emphasis": "high | medium | low | absent",
      "shift_type": "increased | decreased | new | dropped",
      "significance": "one sentence analyst read",
      "evidence_then": {{"evidence_quote":"<=50 words","source_quarter":"Q1 2026","speaker":"cfo"}},
      "evidence_now": {{"evidence_quote":"<=50 words","source_quarter":"Q3 2026","speaker":"cfo"}}
    }}
  ],

  "recurring_key_metrics": [
    {{
      "metric": "same-store sales",
      "mentioned_in_quarters": ["Q1 2026","Q2 2026","Q3 2026","Q4 2025"],
      "always_first_N_mentioned": true,
      "notes": "one sentence on the pattern"
    }}
  ],

  "summary": {{
    "current_narrative_frame": "one sentence: how mgmt wants the story told right now",
    "most_significant_shift": "one sentence: biggest emphasis change last 4 quarters, or 'none material'",
    "narrative_pivot_detected": "yes | no",
    "narrative_pivot_detail": "if yes, one sentence describing; if no, empty"
  }}
}}

RULES:
1. "Order mentioned" matters — the metric mentioned FIRST in prepared
   remarks is meaningfully more prioritized than the third.
2. A "dropped metric" must have been materially emphasized in prior
   quarters (not just mentioned in passing).
3. A "newly introduced metric" must actually be new — not a metric that's
   always been there but is now discussed more.
4. For narrative frame: capture what mgmt is PITCHING, not what's actually
   happening. These can diverge.
5. Every evidence_quote verbatim — framing language ("we're pleased to"
   vs "we are continuing to navigate") is load-bearing.
6. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_metrics_highlighted(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
