"""
GuidanceTracker subagent.

Extracts from 12 quarters of earnings call transcripts:
  - Every guide management issued (metric, value/language, speaker, quarter)
  - Every beat/miss against those guides (with management's own attribution)
  - Credibility signals (raised-then-missed, sandbag patterns, etc.)

Output is strictly evidence-grounded: every claim carries a verbatim quote,
source quarter, and speaker. No paraphrasing in evidence_quote.

This is the most important transcript subagent because guidance vs. reality
IS management credibility, and everything else in the research brief rests
on whether you trust the numbers coming out of the next call.
"""

from __future__ import annotations

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, SubagentResult, call_subagent,
)
from research.transcript_subagents._context_pack import ContextPack


SUBAGENT_NAME = "guidance_tracker"


SYSTEM_PROMPT = """You are the GuidanceTracker subagent. You are one of eight
specialists analyzing multi-quarter earnings call transcripts for an equity
research pipeline. Your ONLY job is to extract management guidance and
beat/miss reconciliation history — you do not opine on valuation, strategy,
or tone. Other subagents handle those dimensions.

SCOPE LIMITATION (important):
Formal guidance numbers (e.g., "revenue of $6.65-6.70B") typically first
appear in the concurrent press release or investor slide deck. A separate
workflow ingests those. You are looking at TRANSCRIPTS — the earnings CALL —
which usually adds color to formally-issued guides rather than introducing
them.

Focus on what transcripts uniquely provide:
  - Hedging language around guides ("we continue to expect" vs "we now expect")
  - Q&A pushback on guides and how management responded
  - Reconciliation commentary for past beats/misses
  - Verbal metrics mentioned on the call that may not appear in the PR/deck
  - Speaker attribution (which exec owns each guide statement)
If formal guide numbers appear in prepared remarks, capture them — but know
the authoritative source is typically the press release.

You are rigorous about evidence grounding. You quote verbatim. You do not
speculate about what management "meant" — you report what they said, who
said it, and when. If something is ambiguous, flag it as ambiguous rather
than picking an interpretation."""


USER_PROMPT_TEMPLATE = """Extract the complete guidance history and beat/miss
reconciliation for {ticker} from the transcripts below.

GOAL: Produce a dataset an analyst can use to answer "how reliable is this
management team's guidance?" and "when they beat/miss, what do they blame?"

OUTPUT JSON SCHEMA:
{{
  "guides_issued": [
    {{
      "source_quarter": "Q3 2026",
      "metric": "revenue | same-store sales | operating margin | EPS | units | ARR | net retention | CapEx | free cash flow | gross margin | segment rev (specify) | other (specify)",
      "period_guided": "FY2026 | Q4 2026 | next 3 years | long-term",
      "guide_type": "initial | raised | lowered | reaffirmed | widened_range | narrowed_range | withdrawn",
      "guide_value": "verbatim number/range as given (e.g., '+2-4% SSS' or '$6.65-6.70B revenue')",
      "guide_language": "verbatim phrasing (e.g., 'we continue to expect' vs 'we are now targeting')",
      "evidence_quote": "<=50 words verbatim from transcript",
      "speaker": "ceo|cfo|coo|other"
    }}
  ],

  "beats_and_misses": [
    {{
      "quarter_reported": "Q4 2025",
      "metric": "same metric vocabulary as guides_issued",
      "what_was_guided": "what the prior guide said (reference the guides_issued entry)",
      "what_actual_came_in": "verbatim or best-reconstructed from transcript",
      "outcome": "beat | miss | in-line | mixed",
      "management_attribution": "what management said CAUSED the beat/miss",
      "attribution_evidence": {{
        "evidence_quote": "<=50 words verbatim, management's own words explaining",
        "source_quarter": "the quarter where they gave this attribution (usually same as quarter_reported)",
        "speaker": "ceo|cfo|coo|other"
      }},
      "attribution_credibility": "one of: specific_and_defensible | plausible | hand_wavy | blamed_externals | no_attribution_offered",
      "attribution_credibility_reason": "one sentence why you judged it that way"
    }}
  ],

  "credibility_patterns": [
    {{
      "pattern": "short description (e.g., 'Raised FY guide in Q2, then missed and lowered in Q3')",
      "quarters_involved": ["Q2 2025", "Q3 2025"],
      "significance": "brief analyst interpretation — one sentence",
      "evidence_quotes": [
        {{
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q2 2025",
          "speaker": "cfo"
        }},
        {{
          "evidence_quote": "<=50 words verbatim",
          "source_quarter": "Q3 2025",
          "speaker": "cfo"
        }}
      ]
    }}
  ],

  "current_live_guides": [
    {{
      "metric": "...",
      "period_guided": "FY2026",
      "most_recent_statement": "verbatim",
      "source_quarter": "Q3 2026",
      "speaker": "cfo",
      "evidence_quote": "<=50 words verbatim",
      "change_from_prior": "raised | lowered | reaffirmed | widened | first_issued"
    }}
  ],

  "summary": {{
    "total_guides_tracked": 0,
    "beat_count": 0,
    "miss_count": 0,
    "inline_count": 0,
    "net_credibility_read": "high | moderate-high | moderate | moderate-low | low",
    "net_credibility_reason": "one sentence citing specific pattern evidence"
  }}
}}

RULES:
1. Copy quotes VERBATIM in every evidence_quote. Hedging words matter.
   Capture "we continue to expect" vs "we now believe" vs "we are targeting"
   exactly. Wording changes magnitude.
2. Only include items you can cite. Empty lists are better than guesses.
3. A "raised" guide must reference both the prior and new values — if you
   only see one, it's "initial" or "reaffirmed", not "raised".
4. "management_attribution" only fills when management explicitly addressed
   the beat/miss. If they didn't, use "no_attribution_offered" and leave
   attribution_evidence blank.
5. "attribution_credibility" is the harsh honest read: did management give
   a specific named reason (specific_and_defensible), a vague reason
   (hand_wavy), or blame macro/weather/timing (blamed_externals)?
6. Do NOT summarize in prose. Fill the JSON.
7. Skip items that are backward-looking "results" commentary only — we want
   FORWARD-LOOKING guidance and the reconciliation of past guides.
{evidence_block}

{context_pack}

Respond with the JSON object only."""


def run_guidance_tracker(pack: ContextPack, *, verbose: bool = False) -> SubagentResult:
    """Run the GuidanceTracker subagent against a prepared context pack."""
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
        max_tokens=6000,          # guidance output is dense; allow room
        temperature=0.15,          # extraction task → low temp
        verbose=verbose,
    )
