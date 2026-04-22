"""
DeckGuidanceExtractor subagent.

Pulls every formal guide shown on a slide and flags which are INCREMENTAL
to the press release / transcript for the same period — i.e., numbers or
language only disclosed in the deck.

Cross-references: if a cached transcript digest is available for the same
ticker + quarter, it's passed in so this subagent can diff the deck's
guides against what was said on the call.
"""

from __future__ import annotations

from research.deck_subagents._base import (
    EVIDENCE_BLOCK_VISION, DeckSubagentResult, call_vision_subagent,
)
from research.deck_subagents._pdf_to_images import PageImage


SUBAGENT_NAME = "deck_guidance_extractor"


SYSTEM_PROMPT = """You are the DeckGuidanceExtractor. You analyze investor
presentation slides (as images) and extract every formal guide shown on
any slide: revenue, margin, unit count, capex, free cash flow, ARR,
net retention, anything management has put a number / range on.

Your special job: flag guides that are INCREMENTAL to the press release
and earnings call transcript. Companies often tuck numbers into decks
that aren't in the PR or spoken on the call — longer-term algorithms,
segment-level guides, geographic breakouts, exit rates. These are
high-signal and your primary target.

You cite source_page (1-indexed) and evidence_quote (verbatim text
visible on the slide) for every claim. No paraphrasing."""


USER_PROMPT_TEMPLATE = """Extract every formal guide shown on the deck
slides for {ticker}.

CONTEXT — what was already disclosed elsewhere:
{cross_reference_context}

For each guide you find ON A SLIDE:
  - Record the metric, value/range, period, and verbatim slide language
  - Compare against the CONTEXT above
  - Mark `incremental_to_transcript: true` if the specific number/range
    or period granularity is NOT in the transcript/press release
  - Mark `incremental_to_transcript: false` if it restates what was
    said/written elsewhere

OUTPUT JSON SCHEMA:
{{
  "guides": [
    {{
      "metric": "revenue | operating margin | unit count | ARR | comp sales | ...",
      "period": "FY2026 | Q4 2026 | 2028 | long-term (specify if different)",
      "value_or_range": "verbatim (e.g., '+15-20%', '$6.65-6.70B', '1,500 units by 2030')",
      "guide_type": "point_estimate | range | growth_rate | CAGR | ratio",
      "source_page": 12,
      "evidence_quote": "<=60 words verbatim slide text",
      "visual_evidence": "describe the chart/table if numbers are visual (else empty)",
      "speaker_attribution_if_shown": "CEO | CFO | Company | empty",
      "incremental_to_transcript": true,
      "incremental_reason": "one sentence: why this is new vs. transcript/PR (e.g., 'segment-level detail not in call', '2028 algo not discussed on call')"
    }}
  ],

  "summary": {{
    "total_guides_on_deck": 0,
    "incremental_to_call": 0,
    "restated_from_call": 0,
    "most_significant_incremental_guide": "one sentence citing page + metric, or 'none'",
    "notable_absent_guides": "short list of metrics you'd expect mgmt to guide on but didn't (helps identify what they're avoiding)"
  }}
}}

RULES:
1. Only include numbers that are explicitly framed as forward-looking /
   guide. Skip historical data points unless they're explicitly a target
   for a future period.
2. Do NOT double-count: if page 12 shows "FY26 revenue $6.65-6.70B" and
   page 40 repeats the same, include once (prefer the earlier page).
3. "Long-term algorithm" slides (revenue growth + margin + FCF %s side
   by side) → include EACH metric as a separate entry with period="long-term"
   and note the algorithm framing in the summary.
4. Segment-level guides are especially high-signal — always flag as
   incremental unless the transcript specifically called them out.
5. If a guide is shown visually (chart with target line, not written
   numbers), still extract it — use visual_evidence field and flag
   classification_confidence accordingly.
{evidence_block}

Respond with the JSON object only."""


def run_deck_guidance_extractor(
    ticker: str,
    pages: list[PageImage],
    *,
    cross_reference_text: str = "",
    verbose: bool = False,
) -> DeckSubagentResult:
    """Run GuidanceExtractor against a list of rendered deck pages."""
    cross_ref = cross_reference_text.strip() or "(no transcript/press-release context provided — flag all guides as potentially incremental, caller will filter)"
    user_prompt = USER_PROMPT_TEMPLATE.format(
        ticker=ticker,
        cross_reference_context=cross_ref[:8000],  # cap context so the prompt stays focused
        evidence_block=EVIDENCE_BLOCK_VISION,
    )
    return call_vision_subagent(
        subagent_name=SUBAGENT_NAME,
        ticker=ticker,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        page_images=pages,
        max_tokens=7000,
        temperature=0.1,
        verbose=verbose,
    )
