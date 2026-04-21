"""
Transcript Analyzer

Standalone module that processes multi-quarter earnings call transcripts
and produces structured insights for the research brain.

This runs BEFORE the main research brief. It extracts:
  - Management guidance evolution (what they guided each quarter)
  - Key metric trends (SSS, margins, store counts quarter by quarter)
  - Tone shifts (language changes in how management discusses topics)
  - Analyst concerns (what questions keep getting asked)
  - Forward signals (commitments management makes about future periods)

The output is a structured AnalysisReport that gets injected into
the Claude research brief prompt, giving it pre-digested context
instead of raw 50K-char transcripts.

Usage:
    from research.transcript_analyzer import analyze_transcripts
    report = analyze_transcripts("CMG", verbose=True)
    # Feed report.to_prompt_text() into deep_research.py
"""

import os
import re
import json
import httpx
from dataclasses import dataclass, field


# ---------------------------------------------------------------
# Robust JSON parsing -- Claude sometimes emits JSON with trailing commas,
# smart quotes, or embedded newlines in strings that json.loads rejects.
# This module sees this often enough that we need a repair layer.
# ---------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Remove ``` or ```json fences, preserving inner content."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first line (```json or ```)
        lines = lines[1:]
        # Drop trailing fence if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _extract_outer_object(text: str) -> str | None:
    """Find the outermost {...} balanced object, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # Unbalanced -- likely truncation. Return what we have; repair layer may salvage.
    return text[start:]


def _repair_common_issues(s: str) -> str:
    """Fix trailing commas, smart quotes, and lone newlines inside strings."""
    # Smart quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    # Trailing commas before } or ]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _close_unbalanced(s: str) -> str:
    """If truncation left a dangling string or object, close it best-effort."""
    # Count unescaped quotes to see if we're in an open string
    in_str = False
    escape = False
    last_good = len(s)
    depth = 0
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth >= 0 and not in_str:
            last_good = i + 1
    s = s[:last_good]
    # If we ended mid-string, truncate back to last ," or {" boundary
    if in_str:
        last_comma = s.rfind('",')
        last_brace = s.rfind('",{')
        cut = max(last_comma, last_brace)
        if cut > 0:
            s = s[:cut + 1]
    # Balance braces/brackets
    opens_braces = s.count("{") - s.count("}")
    opens_brackets = s.count("[") - s.count("]")
    s = s.rstrip().rstrip(",")
    s += "]" * max(0, opens_brackets)
    s += "}" * max(0, opens_braces)
    return s


def _robust_json_parse(text: str, verbose: bool = False) -> dict | None:
    """Try hard to parse Claude's response as JSON. Returns dict or None."""
    text = _strip_code_fences(text)
    obj = _extract_outer_object(text)
    if obj is None:
        if verbose:
            print("    robust_json: no opening brace found")
        return None

    # Attempt 1: raw
    try:
        return json.loads(obj)
    except json.JSONDecodeError as e1:
        if verbose:
            print(f"    robust_json: raw parse failed ({e1.msg} at col {e1.colno})")

    # Attempt 2: after trailing-comma / smart-quote repair
    repaired = _repair_common_issues(obj)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e2:
        if verbose:
            print(f"    robust_json: repaired parse failed ({e2.msg} at col {e2.colno})")

    # Attempt 3: close unbalanced braces from likely truncation
    closed = _close_unbalanced(repaired)
    try:
        return json.loads(closed)
    except json.JSONDecodeError as e3:
        if verbose:
            print(f"    robust_json: close-unbalanced parse failed ({e3.msg} at col {e3.colno})")

    return None


@dataclass
class QuarterInsight:
    """Structured insights from one quarter's earnings call."""
    quarter: str = ""           # "Q4 2025"
    # Guidance given
    guidance_items: list = field(default_factory=list)
    # {metric, value_or_range, vs_prior_guidance, management_language}
    # Key metrics mentioned
    key_metrics: list = field(default_factory=list)
    # {metric, value, direction, context}
    # Management tone
    tone_signals: list = field(default_factory=list)
    # "confident", "cautious", "defensive", "hedging"
    # Analyst Q&A themes
    analyst_concerns: list = field(default_factory=list)
    # What analysts kept pressing on
    # Forward commitments
    forward_signals: list = field(default_factory=list)
    # Things management committed to for future quarters


@dataclass
class TranscriptAnalysis:
    """Full analysis across multiple quarters."""
    ticker: str = ""
    quarters_analyzed: int = 0
    quarter_insights: list = field(default_factory=list)  # [QuarterInsight]
    # Cross-quarter patterns
    guidance_evolution: list = field(default_factory=list)
    # How guidance changed quarter to quarter
    recurring_concerns: list = field(default_factory=list)
    # Issues that keep coming up in Q&A
    tone_trajectory: str = ""
    # "improving", "deteriorating", "stable", "volatile"
    key_inflection_points: list = field(default_factory=list)
    # Moments where the narrative shifted
    management_credibility: str = ""
    # "high" (beats guidance), "moderate", "low" (misses guidance)

    def to_prompt_text(self) -> str:
        """
        Produce a condensed text block for injection into the research brief prompt.
        This replaces raw transcripts with pre-digested insights.
        """
        lines = [f"TRANSCRIPT ANALYSIS ({self.quarters_analyzed} quarters):"]

        if self.tone_trajectory:
            lines.append(f"Management tone trajectory: {self.tone_trajectory}")
        if self.management_credibility:
            lines.append(f"Management credibility: {self.management_credibility}")

        if self.guidance_evolution:
            lines.append("\nGuidance evolution:")
            for g in self.guidance_evolution[:8]:
                lines.append(f"  {g}")

        if self.recurring_concerns:
            lines.append("\nRecurring analyst concerns:")
            for c in self.recurring_concerns[:5]:
                lines.append(f"  - {c}")

        if self.key_inflection_points:
            lines.append("\nKey inflection points:")
            for ip in self.key_inflection_points[:4]:
                lines.append(f"  - {ip}")

        for qi in self.quarter_insights[:4]:
            lines.append(f"\n{qi.quarter}:")
            for m in qi.key_metrics[:3]:
                lines.append(f"  {m}")
            for g in qi.guidance_items[:2]:
                lines.append(f"  Guidance: {g}")
            if qi.tone_signals:
                lines.append(f"  Tone: {', '.join(qi.tone_signals[:3])}")
            if qi.analyst_concerns:
                lines.append(f"  Analysts asking about: {'; '.join(qi.analyst_concerns[:3])}")

        return "\n".join(lines)


def analyze_transcripts(ticker: str, transcript_text: str = None,
                        verbose: bool = False) -> TranscriptAnalysis | None:
    """
    Analyze earnings call transcripts and produce structured insights.

    If transcript_text is provided, uses it directly.
    Otherwise, fetches from EarningsCall.biz API.

    Uses a Claude API call specifically for transcript analysis --
    separate from the research brief call. This is the "pre-digestion"
    step that turns raw transcripts into structured input for the brain.
    """
    def v(msg):
        if verbose:
            print(msg)

    # Get transcript text if not provided
    if not transcript_text:
        try:
            from research.transcript_fetcher import fetch_transcript_history
            transcript_text = fetch_transcript_history(ticker, quarters=12, verbose=verbose)
        except Exception as e:
            v(f"  Transcript analyzer: fetch failed - {e}")
            return None

    if not transcript_text or len(transcript_text) < 500:
        v(f"  Transcript analyzer: insufficient text ({len(transcript_text) if transcript_text else 0} chars)")
        return None

    v(f"  Transcript analyzer: processing {len(transcript_text):,} chars...")

    # Claude call for transcript analysis
    from research.deep_research import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        v(f"  Transcript analyzer: no API key")
        return None

    prompt = f"""You are analyzing {ticker} earnings call transcripts from the past 3 years.
Your job is to extract STRUCTURED INSIGHTS that an equity analyst needs, not summaries.

TRANSCRIPTS:
{transcript_text[:30000]}

Extract the following in JSON format:
{{
  "guidance_evolution": [
    "Q4 2025: Guided flat comps for 2026 (down from low-single-digit prior quarter)",
    "Q3 2025: Guided low-single-digit comp growth (maintained from Q2)"
  ],
  "recurring_concerns": [
    "Traffic decline trajectory -- analysts asked every quarter",
    "Labor cost inflation vs pricing power -- persistent pushback"
  ],
  "tone_trajectory": "deteriorating|improving|stable|volatile",
  "tone_reasoning": "Why you assessed the tone this way, citing specific language changes",
  "key_inflection_points": [
    "Q2 2025: First negative comp quarter, management shifted from 'growth' to 'resilience' framing",
    "Q4 2024: Peak margins, management began flagging tariff risks"
  ],
  "management_credibility": "high|moderate|low",
  "credibility_reasoning": "How has management's track record on guidance been?",
  "quarter_details": [
    {{
      "quarter": "Q4 2025",
      "key_metrics": ["Revenue $3.0B (+4.9%)", "Comps -2.5%", "Restaurant margin 23.4%"],
      "guidance_items": ["2026 comps: approximately flat", "New stores: 350-370"],
      "tone_signals": ["cautious", "defensive on margins"],
      "analyst_concerns": ["Traffic decline acceleration", "Tariff impact on food costs"],
      "forward_signals": ["Efficiency package to 2,000 restaurants by year-end"]
    }}
  ]
}}

RULES:
1. Quote specific numbers from the transcripts (not general statements)
2. Track how guidance CHANGED quarter to quarter (upgraded, maintained, lowered)
3. Note where management hedged or was evasive vs confident
4. Identify what analysts are REPEATEDLY asking about (that's where the edge is)
5. Flag any management commitments that can be checked next quarter
6. Be SPECIFIC about inflection points -- cite the quarter and what changed"""

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 5000,   # bumped from 3000 -- truncation was a real risk
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120.0,
        )

        if resp.status_code != 200:
            v(f"  Transcript analyzer: API error {resp.status_code}")
            return None

        text = resp.json()["content"][0]["text"].strip()
        data = _robust_json_parse(text, verbose=verbose)
        if data is None:
            # One retry with a stricter instruction if parse failed
            v(f"  Transcript analyzer: first parse failed, retrying with strict JSON instruction...")
            strict_prompt = prompt + (
                "\n\nCRITICAL: Respond with ONE valid JSON object and nothing else. "
                "No code fences, no prose before or after. Escape all quotes inside strings "
                "as \\\". Do NOT include trailing commas. Keep the response under 4500 tokens."
            )
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 5000,
                      "messages": [{"role": "user", "content": strict_prompt}]},
                timeout=120.0,
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"].strip()
                data = _robust_json_parse(text, verbose=verbose)
        if data is None:
            v(f"  Transcript analyzer: gave up after retry")
            return None

        # Build the analysis
        analysis = TranscriptAnalysis(
            ticker=ticker,
            guidance_evolution=data.get("guidance_evolution", []),
            recurring_concerns=data.get("recurring_concerns", []),
            tone_trajectory=data.get("tone_trajectory", ""),
            key_inflection_points=data.get("key_inflection_points", []),
            management_credibility=data.get("management_credibility", ""),
        )

        for qd in data.get("quarter_details", []):
            qi = QuarterInsight(
                quarter=qd.get("quarter", ""),
                guidance_items=qd.get("guidance_items", []),
                key_metrics=qd.get("key_metrics", []),
                tone_signals=qd.get("tone_signals", []),
                analyst_concerns=qd.get("analyst_concerns", []),
                forward_signals=qd.get("forward_signals", []),
            )
            analysis.quarter_insights.append(qi)

        analysis.quarters_analyzed = len(analysis.quarter_insights)

        v(f"  Transcript analyzer: {analysis.quarters_analyzed} quarters analyzed")
        v(f"  Tone: {analysis.tone_trajectory} | Credibility: {analysis.management_credibility}")
        if analysis.recurring_concerns:
            v(f"  Recurring: {analysis.recurring_concerns[0][:60]}")

        return analysis

    except Exception as e:
        v(f"  Transcript analyzer: error - {e}")
        return None
