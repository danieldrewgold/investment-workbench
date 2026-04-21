"""
QandAAnalyzer subagent.

Extracts from the Q&A sections of multi-quarter earnings calls:
  - The FIRST question each call (which analyst, what they asked) — signal
    for where smart money is most focused
  - Recurring themes across quarters (questions that keep coming up)
  - Dodged questions — where management didn't directly answer
  - Per-analyst patterns (who presses on what)
  - Question-answer alignment classification (direct / partial / deflected / refused)
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "qanda_analyzer"


SYSTEM_PROMPT = """You are the QandAAnalyzer subagent. You analyze the Q&A
portion of multi-quarter earnings calls to identify where investor attention
is concentrated and where management is uncomfortable.

The FIRST question of every call is disproportionately important — it's
what the lead analyst (usually a top-tier bank) decided was the single
most pressing issue. Track it every quarter.

"Dodged" is a specific classification: the analyst asked question X, and
management's answer addressed Y (related but not X), or gave a non-answer
("we'll talk about that in more detail later", "that's a great question,
let me think about it differently", etc.). You must cite the exact question
AND exact answer to support a dodge claim.

You do not rate management or opine on strategy — other subagents do that.
You catalog Q&A dynamics only."""


USER_PROMPT_TEMPLATE = """Analyze the Q&A sections of {ticker}'s earnings calls.

OUTPUT JSON SCHEMA:
{{
  "first_questions": [
    {{
      "quarter": "Q3 2026",
      "analyst": "full name",
      "firm": "bank / shop name",
      "question_verbatim": "the question as asked, up to 60 words",
      "topic": "short topic label (e.g., 'FY26 margin trajectory', 'mobile UA costs')",
      "evidence_quote": "<=50 words verbatim of the question",
      "source_quarter": "Q3 2026",
      "speaker": "analyst:FIRM"
    }}
  ],

  "recurring_themes": [
    {{
      "theme": "short label (e.g., 'mobile UA cost trajectory')",
      "quarters_asked": ["Q1 2026","Q2 2026","Q3 2026"],
      "who_keeps_asking": ["Brian Harbour (MS)","John Smith (JPM)"],
      "why_this_is_pressure": "one-sentence interpretation of why this theme persists (e.g., 'management has not given a satisfying answer across 3 calls')",
      "example_evidence": [
        {{
          "evidence_quote": "<=50 words verbatim of an example question/answer",
          "source_quarter": "Q2 2026",
          "speaker": "analyst:MS"
        }}
      ]
    }}
  ],

  "dodged_questions": [
    {{
      "quarter": "Q3 2026",
      "analyst": "full name (firm)",
      "question_verbatim": "<=60 words the analyst asked",
      "answer_verbatim": "<=60 words what management actually said",
      "dodge_type": "redirected | non-answer | deferred | partial | refused",
      "why_this_is_a_dodge": "one-sentence specific reason",
      "question_evidence": {{"evidence_quote":"<=50 words","source_quarter":"...","speaker":"analyst:..."}},
      "answer_evidence": {{"evidence_quote":"<=50 words","source_quarter":"...","speaker":"ceo|cfo|coo"}}
    }}
  ],

  "analyst_alignment_summary": [
    {{
      "quarter": "Q3 2026",
      "total_questions": 0,
      "direct_answers": 0,
      "partial_answers": 0,
      "dodged": 0,
      "notes": "one sentence on the call's overall Q&A dynamic"
    }}
  ],

  "persistent_pressers": [
    {{
      "analyst": "Brian Harbour",
      "firm": "Morgan Stanley",
      "quarters_active": ["Q1 2026","Q2 2026","Q3 2026"],
      "topics_pressed": ["labor inflation","ticketing pricing"],
      "tone": "skeptical | neutral | constructive",
      "evidence": [
        {{"evidence_quote":"<=50 words","source_quarter":"Q3 2026","speaker":"analyst:MS"}}
      ]
    }}
  ],

  "summary": {{
    "total_calls_analyzed": 0,
    "top_investor_concern_currently": "one-sentence read from recurring themes + first questions",
    "net_qanda_posture": "open | defensive | mixed",
    "net_posture_reason": "one sentence citing specific pattern"
  }}
}}

RULES:
1. EVERY first_question must be captured — if a quarter has no Q&A, say so
   in analyst_alignment_summary, don't skip.
2. A "dodge" requires specific evidence: the question AND the non-matching
   answer, both verbatim. If you're not sure, don't call it a dodge.
3. Recurring themes need 2+ quarters. One-off questions are noise.
4. Persistent pressers: only analysts who appeared 2+ quarters.
5. Preserve verbatim wording in every evidence_quote — tone shift shows up
   in exact phrasing ("I'm struggling to reconcile" vs "can you help me
   understand" are different pressure levels).
6. Copy analyst names and firms exactly as they appear in the transcript.
7. Fill the JSON. No prose outside it.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_qanda_analyzer(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
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
        max_tokens=6000,
        temperature=0.15,
        verbose=verbose,
    )
