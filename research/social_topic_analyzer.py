"""
Social topic analyzer.

Takes recent StockTwits messages PLUS the earnings call transcripts and
in a single Sonnet call:

  1. Clusters retail dialogue into 4-8 distinct themes (concerns, debates,
     expectations, recurring narratives).
  2. For each theme, checks whether management has addressed it in the
     transcripts (verbatim evidence cited if yes).
  3. Flags themes with `silence_flag = True` when retail is debating
     something substantive (>=3 messages) that management has NOT
     publicly addressed.

The TMDX fuel-cost case is the canonical example: 12 retail messages
debating fuel pass-through dynamics, zero substantive transcript
mentions, and Q1 GMs compressed 300bps citing supply chain costs.
Management silence on a publicly-debated operational concern is itself
a high-signal flag for the brief.

Public API:
    analyze_social_topics(ticker, messages, transcripts_text, verbose=False)
        -> SocialThemeBundle

The DAG step in research/dag/steps.py wraps this and content-hash caches
on the stocktwits + transcripts inputs.
"""

from __future__ import annotations

import json
import re
import time
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime

import httpx

from research.deep_research import ANTHROPIC_API_KEY


_CLUSTERING_MODEL = "claude-sonnet-4-6"
ANALYZER_VERSION = "v1"

_VALID_SENTIMENT_LEANS = {"bullish", "bearish", "mixed", "neutral"}


@dataclass
class SocialTheme:
    """One clustered theme from retail dialogue."""
    name: str = ""                            # e.g. "Fuel cost pass-through concerns"
    n_messages: int = 0                       # how many of the input msgs map to this theme
    sentiment_lean: str = "neutral"           # bullish / bearish / mixed / neutral
    representative_quotes: list = field(default_factory=list)  # 2-3 short quotes
    addressed_in_transcripts: bool = False    # mgmt has spoken about it
    transcript_evidence: str = ""             # verbatim transcript snippet, if any
    silence_flag: bool = False                # True when n_messages >= 3 AND not addressed
    notes: str = ""                           # optional analyst note (e.g. "topic recurs across 3 quarters")


@dataclass
class SocialThemeBundle:
    ticker: str = ""
    fetched_at: str = ""
    n_input_messages: int = 0
    n_input_transcript_chars: int = 0
    themes: list = field(default_factory=list)   # list[SocialTheme]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fetched_at": self.fetched_at,
            "n_input_messages": self.n_input_messages,
            "n_input_transcript_chars": self.n_input_transcript_chars,
            "themes": [asdict(t) if hasattr(t, "__dataclass_fields__") else t
                        for t in self.themes],
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        """
        Render a labeled corpus block for the brief. Surfaces
        management-silence themes prominently — those are the highest-
        signal output (retail concerned + mgmt has not addressed).
        """
        if not self.themes:
            return ""

        silent_themes = [t for t in self.themes if t.silence_flag]
        addressed_themes = [t for t in self.themes
                              if t.addressed_in_transcripts and not t.silence_flag]
        other_themes = [t for t in self.themes
                         if not t.silence_flag and not t.addressed_in_transcripts]

        lines = [
            f"=== SOCIAL TOPIC ANALYSIS (${self.ticker}) ===",
            "(Themes clustered from StockTwits retail dialogue, cross-checked "
            "against earnings call transcripts. Themes flagged with [SILENCE] "
            "indicate retail debating a topic management hasn't publicly "
            "addressed — historically these become real risks.)",
            "",
        ]

        if silent_themes:
            lines.append(f"** MANAGEMENT-SILENCE FLAGS ({len(silent_themes)}) — "
                          f"retail is debating these; management hasn't addressed:")
            for t in silent_themes:
                lines.append(f"  [SILENCE] {t.name}  ({t.n_messages} msgs, "
                              f"{t.sentiment_lean})")
                for q in t.representative_quotes[:2]:
                    qclean = re.sub(r"\s+", " ", q).strip()
                    lines.append(f"      \"{qclean[:240]}\"")
                if t.notes:
                    lines.append(f"      Note: {t.notes}")
            lines.append("")

        if addressed_themes:
            lines.append(f"Themes management HAS addressed ({len(addressed_themes)}):")
            for t in addressed_themes:
                lines.append(f"  [ADDRESSED] {t.name}  ({t.n_messages} msgs, "
                              f"{t.sentiment_lean})")
                if t.transcript_evidence:
                    ev = re.sub(r"\s+", " ", t.transcript_evidence).strip()
                    lines.append(f"      Mgmt: \"{ev[:240]}\"")
            lines.append("")

        if other_themes:
            lines.append(f"Other retail themes ({len(other_themes)}):")
            for t in other_themes:
                lines.append(f"  [{t.sentiment_lean:^9s}] {t.name}  "
                              f"({t.n_messages} msgs)")
            lines.append("")

        lines.append(
            "Brief should treat [SILENCE] flags as research questions to "
            "investigate independently — management not addressing a topic "
            "publicly debated by retail is itself a signal."
        )
        lines.append("=" * 60)
        return "\n".join(lines)


_PROMPT = """You are analyzing retail dialogue from StockTwits for ticker {ticker} and cross-checking each theme against management's earnings call transcripts to identify topics retail is debating that management has NOT addressed publicly.

Why this matters: when retail is actively debating an operational concern (fuel costs, supply chain, regulatory risk, capacity) and management is silent on it in calls, the topic is often a real-but-undisclosed risk — historically these turn into negative surprises (e.g. TMDX retail debating jet fuel pass-through for weeks while management stayed silent; Q1 print revealed 300bps gross margin compression citing "higher supply chain and operating costs").

## RETAIL MESSAGES from StockTwits (last 30 days, top by engagement)

{messages_block}

## EARNINGS CALL TRANSCRIPTS (last 3 quarters of {ticker})

{transcripts_block}

## YOUR JOB

Output strict JSON only. No prose, no markdown fences. Schema:

{{"themes": [
  {{
    "name": "short descriptive theme (5-10 words)",
    "n_messages": <integer count of input messages mapping to this theme>,
    "sentiment_lean": "bullish" | "bearish" | "mixed" | "neutral",
    "representative_quotes": ["verbatim quote from messages, ≤220 chars", "another quote", ...],
    "addressed_in_transcripts": true | false,
    "transcript_evidence": "verbatim quote from transcript if addressed (≤240 chars), else empty",
    "silence_flag": true | false,
    "notes": "optional 1-line note (e.g. 'theme has recurred across 3 quarters')"
  }},
  ...
]}}

Rules:

1. Identify 4-8 distinct themes. Cluster aggressively — multiple messages saying the same thing (e.g. "fuel prices" + "jet fuel" + "transportation cost") = one theme.

2. For each theme:
   - addressed_in_transcripts = true ONLY if management explicitly addressed the topic in prepared remarks or Q&A. Casual/tangential mentions don't count.
   - transcript_evidence = the verbatim phrase from the transcript that addresses the theme. Empty string if not addressed.
   - silence_flag = true ONLY when n_messages >= 3 AND addressed_in_transcripts = false. This identifies high-signal management-silence cases.

3. Skip pure-pump or noise themes ("$TMDX moon", "diamond hands", "lfg") — those don't qualify as substantive themes.

4. Quotes must be verbatim from messages. transcript_evidence must be verbatim from transcripts. Do NOT paraphrase.

5. If retail is bullish on a theme but management has been silent (e.g. clinical trial speculation), silence_flag is still true — silence on either bullish or bearish public dialogue is the signal.
"""


def _compact_messages(messages: list, max_chars: int = 5000) -> str:
    """Take the input message dicts/objects and format into a compact
    block the model can scan. Sort by engagement, drop spammy ones."""
    out: list[str] = []
    used = 0
    for m in messages:
        body = (m.get("body") if isinstance(m, dict) else getattr(m, "body", "")) or ""
        if not body or len(body) < 30:
            continue
        body = re.sub(r"https?://\S+", "[link]", body)
        body = re.sub(r"\s+", " ", body).strip()
        sentiment = (m.get("sentiment") if isinstance(m, dict)
                      else getattr(m, "sentiment", "")) or ""
        date = (m.get("created_at") if isinstance(m, dict)
                 else getattr(m, "created_at", "")) or ""
        line = f"[{sentiment or '--':^7s}] {date[:10]}  {body[:280]}"
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _compact_transcripts(text: str, max_chars: int = 25000) -> str:
    """Take the most recent N chars of transcripts. The most recent
    quarters are at the end of the concatenated text."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    # Keep the last `max_chars` (most recent quarters)
    return "...[earlier quarters omitted]...\n\n" + text[-max_chars:]


def _parse_response(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*"themes".*\}', text, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def analyze_social_topics(ticker: str,
                           messages: list,
                           transcripts_text: str,
                           verbose: bool = False) -> SocialThemeBundle:
    """
    Single Sonnet call to cluster messages + cross-check against
    transcripts. Returns a SocialThemeBundle ready for corpus injection.
    """
    bundle = SocialThemeBundle(
        ticker=ticker.upper(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        n_input_messages=len(messages or []),
        n_input_transcript_chars=len(transcripts_text or ""),
    )

    if not ANTHROPIC_API_KEY:
        bundle.error = "no ANTHROPIC_API_KEY"
        return bundle
    if not messages:
        bundle.error = "no input messages"
        return bundle

    msgs_block = _compact_messages(messages)
    tr_block = _compact_transcripts(transcripts_text or "")

    prompt = _PROMPT.format(
        ticker=ticker,
        messages_block=msgs_block or "(no high-signal messages)",
        transcripts_block=tr_block or "(no transcripts available)",
    )

    def _post():
        return httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _CLUSTERING_MODEL,
                "max_tokens": 3000,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120.0,
        )

    resp = None
    for attempt in range(3):
        try:
            resp = _post()
        except Exception as e:
            if verbose:
                print(f"  Social topic analyzer: request error "
                      f"{type(e).__name__}: {e}")
            time.sleep(5 + 5 * attempt)
            continue
        if resp.status_code not in (429, 529):
            break
        wait = min(20.0 * (1.6 ** attempt), 90.0) + random.uniform(0, 3)
        if verbose:
            print(f"  Social topic analyzer: HTTP {resp.status_code}, "
                  f"backoff {wait:.0f}s")
        time.sleep(wait)

    if resp is None or resp.status_code != 200:
        bundle.error = (
            f"HTTP {resp.status_code if resp else 'no response'}"
            if resp is None or resp.status_code != 200 else ""
        )
        return bundle

    try:
        text = resp.json()["content"][0]["text"]
    except Exception as e:
        bundle.error = f"parse error: {e}"
        return bundle

    parsed = _parse_response(text)
    raw_themes = parsed.get("themes") or []
    for raw in raw_themes:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        sentiment = str(raw.get("sentiment_lean", "")).lower()
        if sentiment not in _VALID_SENTIMENT_LEANS:
            sentiment = "neutral"
        n_msgs = int(raw.get("n_messages") or 0)
        addressed = bool(raw.get("addressed_in_transcripts", False))
        # Re-derive silence_flag on our side so the model can't claim a
        # silence flag on a 1-message topic
        silence = (not addressed) and n_msgs >= 3
        if "silence_flag" in raw:
            silence = bool(raw["silence_flag"]) and silence  # AND with our gate
        bundle.themes.append(SocialTheme(
            name=name[:200],
            n_messages=n_msgs,
            sentiment_lean=sentiment,
            representative_quotes=[
                str(q)[:300] for q in (raw.get("representative_quotes") or [])
            ][:3],
            addressed_in_transcripts=addressed,
            transcript_evidence=str(raw.get("transcript_evidence", ""))[:400],
            silence_flag=silence,
            notes=str(raw.get("notes", ""))[:200],
        ))

    if verbose:
        n_silent = sum(1 for t in bundle.themes if t.silence_flag)
        print(f"  Social topic analyzer: {len(bundle.themes)} themes "
              f"({n_silent} silence-flagged)")
    return bundle
