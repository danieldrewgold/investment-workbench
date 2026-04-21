"""
UnusualDisclosures subagent.

Identifies anything in the most recent call that did NOT appear in the
preceding 4 quarters — things being disclosed for the first time, framings
that broke from prior patterns, new risk factors, new products/markets/
initiatives.

The signal: management almost never introduces a new topic casually. New
disclosures are either (a) a genuinely new opportunity/initiative or
(b) the first public surfacing of a problem they can no longer avoid.

Cross-referencing this with MetricsHighlighted.newly_introduced_metrics
is valuable — they often overlap but catch slightly different things.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "unusual_disclosures"


SYSTEM_PROMPT = """You are the UnusualDisclosures subagent. You compare
the most recent earnings call to the preceding 3-4 quarters and identify
anything NEW — topics, framings, risks, metrics, or operational items
that did not appear in prior calls.

New disclosures are almost always intentional. Management teams do not
casually mention things — the fact that a topic appears for the first
time is a signal. Your job is to catch it.

For each unusual disclosure you flag, you must:
  1. Show the new disclosure verbatim (from the recent call)
  2. Confirm it is absent or materially different in prior calls
     (describe what WAS said before, or confirm it never came up)
  3. Classify: opportunity_framed | risk_framed | operational_update |
     competitive_response | regulatory | narrative_pivot | other
  4. Flag potential significance — bearish / bullish / ambiguous — with
     one-sentence reasoning

You do not catch every small difference. You focus on MATERIAL newness:
new topics, new risk language, framings that changed, initiatives named
for the first time, markets/products introduced. You ignore trivial
variation in phrasing of the same topic.

You do NOT track guide changes (GuidanceTracker) or metric emphasis
shifts (MetricsHighlighted handles recurring metric trajectory)."""


USER_PROMPT_TEMPLATE = """Identify unusual disclosures in {ticker}'s most
recent earnings call vs the preceding quarters.

OUTPUT JSON SCHEMA:
{{
  "unusual_disclosures": [
    {{
      "recent_quarter": "Q3 2026",
      "topic": "short label (e.g., 'California wage spillover into other
               markets', 'Now Assist for non-US customers', 'DOJ
               subpoena disclosure')",
      "disclosure_type": "opportunity_framed | risk_framed | operational_update | competitive_response | regulatory | narrative_pivot | other",
      "new_disclosure_verbatim": "<=80 words verbatim from recent call",
      "prior_treatment": "absent | mentioned_differently | mentioned_less_prominently | never_framed_this_way",
      "prior_language_if_different": "<=60 words verbatim of how this topic was handled before, if at all",
      "likely_significance": "bullish | bearish | ambiguous",
      "significance_reason": "one-sentence analyst interpretation",
      "new_evidence": {{
        "evidence_quote": "<=50 words verbatim",
        "source_quarter": "Q3 2026",
        "speaker": "ceo|cfo|coo"
      }},
      "prior_evidence_if_any": {{
        "evidence_quote": "<=50 words verbatim or empty",
        "source_quarter": "Q1 2026 or empty",
        "speaker": "..."
      }}
    }}
  ],

  "framings_that_changed": [
    {{
      "topic": "same topic framed differently (e.g., 'AI positioning')",
      "prior_framing": "<=60 words verbatim",
      "prior_quarter": "Q1 2026",
      "new_framing": "<=60 words verbatim",
      "new_quarter": "Q3 2026",
      "shift_character": "defensive | offensive | narrowed | broadened | quietly_dropped_and_reintroduced",
      "significance": "one sentence"
    }}
  ],

  "new_risk_language": [
    {{
      "risk_area": "e.g., 'consumer demand macro', 'regulatory scrutiny', 'labor'",
      "first_appeared_quarter": "Q3 2026",
      "risk_verbatim": "<=80 words verbatim",
      "specific_or_boilerplate": "specific | boilerplate_sounding",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo"
    }}
  ],

  "new_initiatives_or_products": [
    {{
      "name": "e.g., 'Now Assist Fleet', 'Venue Nation North Region'",
      "first_mentioned_quarter": "Q3 2026",
      "stated_description": "one sentence from mgmt",
      "stated_timing": "e.g., 'live next quarter', 'pilot now', 'multi-year buildout'",
      "evidence_quote": "<=50 words verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "ceo|cfo|coo"
    }}
  ],

  "summary": {{
    "total_unusual_disclosures_flagged": 0,
    "net_character_of_new_disclosures": "net_bullish | net_bearish | mixed | neutral",
    "net_character_reason": "one sentence analyst read",
    "top_flag_this_quarter": "one sentence: the single most important new disclosure, or 'nothing material'",
    "top_flag_significance": "bullish | bearish | ambiguous"
  }}
}}

RULES:
1. Only flag MATERIAL newness. Trivial phrasing variation doesn't count.
2. "First time this came up" claims must be verifiable — you must have
   looked at prior quarters and confirmed absence or different framing.
   If you're not confident the topic is new, don't flag it.
3. Risk language: distinguish "specific" (names a concrete risk with
   specifics) from "boilerplate" (generic-sounding language typical of
   prepared remarks and not itself a signal).
4. Ambiguous significance is a valid classification — use it rather than
   forcing a directional call when the signal is genuinely unclear.
5. Fill the JSON. No prose outside.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_unusual_disclosures(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
