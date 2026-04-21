"""
Context Pack — preprocess raw transcripts into a structured, speaker-tagged,
quarter-tagged, section-tagged form that all 8 subagents consume.

Runs ONCE per (ticker, raw-transcript-hash). Cached to data/context_packs/.

Flow:
  raw transcripts (50K chars concatenated)
    -> Haiku Claude call with segmentation prompt
    -> ContextPack dataclass
    -> cached JSON

Why Haiku? Segmentation is mechanical (labeling) — doesn't need Sonnet. Cheaper.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

from research.transcript_subagents._base import robust_json_parse
from research.deep_research import ANTHROPIC_API_KEY


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class QuarterPack:
    """Structured single-quarter transcript."""
    quarter: str = ""           # "Q3 2026"
    date: str = ""              # "2025-10-23"
    prepared_remarks: dict = field(default_factory=dict)
    # Keys: "ceo", "cfo", "coo", "other" -> list[str] of paragraphs
    qanda: list = field(default_factory=list)
    # Each: {analyst, firm, question, responder, answer}
    guidance_section: list = field(default_factory=list)
    # Paragraphs containing forward-looking statements and guidance


@dataclass
class ContextPack:
    """All quarters structured. This is what each subagent receives."""
    ticker: str = ""
    quarters_count: int = 0
    quarters: list = field(default_factory=list)   # list[QuarterPack]
    raw_hash: str = ""                              # sha256 of raw input
    built_via: str = ""                             # "claude_haiku" | "regex_fallback"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "quarters_count": self.quarters_count,
            "raw_hash": self.raw_hash,
            "built_via": self.built_via,
            "quarters": [asdict(q) for q in self.quarters],
        }

    def to_subagent_text(self) -> str:
        """
        Format the pack as a human-readable text block for injection into
        subagent prompts. Subagents can navigate by quarter/speaker/section.
        """
        lines = [f"=== TRANSCRIPT CONTEXT PACK: {self.ticker} ({self.quarters_count} quarters) ===\n"]
        for q in self.quarters:
            lines.append(f"\n### {q.quarter} ({q.date or 'date unknown'})\n")
            if q.prepared_remarks:
                lines.append("--- Prepared Remarks ---")
                for speaker, paras in q.prepared_remarks.items():
                    if not paras:
                        continue
                    lines.append(f"\n[{speaker.upper()}]")
                    for p in paras:
                        lines.append(p)
            if q.qanda:
                lines.append("\n--- Q&A ---")
                for i, qa in enumerate(q.qanda, 1):
                    analyst = qa.get("analyst", "analyst")
                    firm = qa.get("firm", "")
                    responder = qa.get("responder", "mgmt")
                    lines.append(f"\n[Q{i}] {analyst} ({firm}): {qa.get('question','')}")
                    lines.append(f"[A{i}] {responder.upper()}: {qa.get('answer','')}")
            if q.guidance_section:
                lines.append("\n--- Guidance / Forward-Looking ---")
                for g in q.guidance_section:
                    lines.append(g)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

SEGMENTATION_SYSTEM = """You are a transcript preprocessor. Your only job is to segment
earnings call transcripts into structured JSON. You do NOT analyze, interpret,
or summarize — you ONLY label what's in the text."""


SEGMENTATION_PROMPT_TEMPLATE = """Segment these {ticker} earnings call transcripts into structured JSON.

INPUT: concatenated raw transcripts covering multiple quarters. Each quarter
starts with a header like "Q3 2026" or similar. Quarters may be in any order.

OUTPUT: JSON object with this exact shape:
{{
  "quarters": [
    {{
      "quarter": "Q3 2026",
      "date": "YYYY-MM-DD" or "" if unknown,
      "prepared_remarks": {{
        "ceo": ["paragraph 1", "paragraph 2", ...],
        "cfo": ["..."],
        "coo": ["..."],
        "other": ["..."]
      }},
      "qanda": [
        {{
          "analyst": "full name if known, else 'Unknown'",
          "analyst_attribution_confidence": "explicit | inferred | unknown",
          "firm": "firm name if known, else ''",
          "firm_attribution_confidence": "explicit | inferred | unknown",
          "question": "the analyst's question verbatim",
          "responder": "ceo|cfo|coo|other|mixed",
          "responder_attribution_confidence": "explicit | inferred | unknown",
          "answer": "management's answer verbatim"
        }}
      ],
      "guidance_section": [
        "paragraph with forward-looking language: guidance, outlook, targets, expectations"
      ]
    }}
  ]
}}

RULES:
1. Do NOT summarize or paraphrase. Copy text verbatim into the appropriate buckets.
2. Identify speakers by explicit attribution in the transcript ("Brian Niccol - CEO:", "Jack Hartung - CFO:", etc.)
   - If you see "President" or similar, bucket as "other" unless you're certain of the role.
3. Q&A entries preserve full question text AND full answer text.
4. `guidance_section` should contain paragraphs with forward-looking statements:
   outlook for next quarter/year, target margins, expected comp ranges, capex plans,
   anything framed as "we expect", "we anticipate", "we're guiding to", etc.
5. Skip boilerplate: safe harbor statements, operator intros, conference-call-ID chatter.
6. If a quarter can't be clearly identified in the text, use quarter="unknown" but still segment its content.
7. Keep ALL substantive content — err on the side of including rather than dropping.

SPEAKER ATTRIBUTION INFERENCE — important, fill gaps carefully:
Transcripts often drop explicit attribution on follow-up questions and on
continuation responses. You may INFER speaker identity when the conversational
context makes it clear, but you MUST mark your confidence honestly.

  a) ANALYST FOLLOW-UPS: When an analyst asks a question, management answers,
     and then a follow-up question appears WITHOUT a new name and WITHOUT
     operator handoff ("The next question comes from..."), it is the SAME
     analyst continuing. Carry forward their name + firm. Mark
     analyst_attribution_confidence = "inferred".

  b) NEW ANALYST HANDOFF: When the operator announces a new analyst
     ("Our next question comes from Brian at Morgan Stanley"), that RESETS
     the analyst for all subsequent Q&A entries until the next handoff.

  c) MANAGEMENT CONTINUATION: If the CEO is answering and the answer flows
     continuously without another name appearing, keep responder="ceo"
     (confidence "inferred" if no explicit re-attribution). Same for CFO.

  d) MID-ANSWER HANDOFF: If a single answer pivots to another executive
     (e.g., CEO answers, then "and from a margin perspective..." clearly
     pivots to CFO mid-response), you may set responder="mixed" and include
     both voices in the answer field. Only do this when the pivot is
     unambiguous — a name mentioned or a clear content domain shift
     (margins/numbers -> CFO, strategy/product -> CEO).

  e) DON'T GUESS. When attribution is genuinely ambiguous, use
     analyst="Unknown", firm="", responder="other", and mark
     confidence="unknown". A shorter honest output beats a wrong guess.

  f) FIRM INFERENCE: You may infer firm from known analyst-firm pairings
     that appear ELSEWHERE in the SAME transcript batch (e.g., if "Brian
     Harbour" is named as "Morgan Stanley" in Q3 and appears unattributed
     in Q4, confidence "inferred"). Do NOT use external knowledge —
     infer only from the transcript text provided.

TRANSCRIPTS (raw):
{transcripts}

Respond with the JSON object only."""


# Version the preprocessor prompt so older context packs auto-invalidate
# when the attribution inference rules change. Bump this whenever you
# materially change the prompt above.
SEGMENTATION_PROMPT_VERSION = "v2-attribution-inference"


def _hash_raw(text: str) -> str:
    """
    Hash the raw transcript + preprocessor prompt version.

    Mixing in the prompt version means old context pack caches (built with
    a prior prompt) become stale automatically when we improve the prompt.
    The downstream digest cache is keyed on THIS hash, so it invalidates
    transitively too.
    """
    payload = text + "||" + SEGMENTATION_PROMPT_VERSION
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cache_path(ticker: str, raw_hash: str) -> Path:
    return Path("data/context_packs") / f"{ticker.upper()}_{raw_hash}.json"


def build_context_pack(
    ticker: str,
    raw_transcript_text: str,
    *,
    force: bool = False,
    verbose: bool = False,
) -> ContextPack | None:
    """
    Build or load a ContextPack for a ticker.

    Cached on raw-transcript content hash. Pass force=True to rebuild.
    Returns None if Claude call fails.
    """
    raw_hash = _hash_raw(raw_transcript_text)
    cache = _cache_path(ticker, raw_hash)
    if cache.exists() and not force:
        try:
            with open(cache) as f:
                data = json.load(f)
            quarters = [QuarterPack(**q) for q in data.get("quarters", [])]
            pack = ContextPack(
                ticker=ticker.upper(),
                quarters_count=len(quarters),
                quarters=quarters,
                raw_hash=raw_hash,
                built_via=data.get("built_via", "cached"),
            )
            if verbose:
                print(f"  Context pack: CACHE HIT {cache.name} ({pack.quarters_count} quarters)")
            return pack
        except Exception as e:
            if verbose:
                print(f"  Context pack: cache read failed ({e}), rebuilding")

    if not ANTHROPIC_API_KEY:
        if verbose:
            print(f"  Context pack: no API key, can't build")
        return None

    if verbose:
        print(f"  Context pack: building via Sonnet ({len(raw_transcript_text):,} chars)...")

    prompt = SEGMENTATION_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        transcripts=raw_transcript_text[:80000],  # 80K char ceiling for Haiku
    )

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                # Use the same model as other calls in the project.
                # Haiku model name varied across API versions; Sonnet is safe and the
                # cost difference is minor for this one-time preprocessing step.
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 8000,
                "temperature": 0.1,
                "system": SEGMENTATION_SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=180.0,
        )
        if resp.status_code != 200:
            if verbose:
                print(f"  Context pack: API error {resp.status_code}: {resp.text[:200]}")
            return None

        text = resp.json()["content"][0]["text"]
        parsed = robust_json_parse(text, verbose=verbose)
        if parsed is None or "quarters" not in parsed:
            if verbose:
                print(f"  Context pack: parse failed or missing 'quarters'")
            return None

        quarters = []
        for qd in parsed["quarters"]:
            qp = QuarterPack(
                quarter=qd.get("quarter", ""),
                date=qd.get("date", ""),
                prepared_remarks=qd.get("prepared_remarks", {}) or {},
                qanda=qd.get("qanda", []) or [],
                guidance_section=qd.get("guidance_section", []) or [],
            )
            quarters.append(qp)

        pack = ContextPack(
            ticker=ticker.upper(),
            quarters_count=len(quarters),
            quarters=quarters,
            raw_hash=raw_hash,
            built_via="claude_sonnet",
        )

        # Cache
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as f:
            json.dump(pack.to_dict(), f, indent=2, default=str)
        if verbose:
            print(f"  Context pack: built {pack.quarters_count} quarters, cached {cache.name}")
        return pack

    except Exception as e:
        if verbose:
            print(f"  Context pack: exception {type(e).__name__}: {e}")
        return None
