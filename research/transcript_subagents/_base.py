"""
Shared utilities for transcript subagents.

Each subagent uses this module to:
  - Make a Claude API call with retry + robust JSON parsing
  - Apply the evidence-grounding requirement (every claim needs quote + quarter + speaker)
  - Return a standardized SubagentResult

Design: keep this thin. Each subagent's intelligence lives in its own prompt file.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


# --------------------------------------------------------------------------
# Evidence-grounding requirement — pasted into every subagent prompt
# --------------------------------------------------------------------------

EVIDENCE_SCHEMA_BLOCK = """
==================================================================
EVIDENCE GROUNDING (mandatory for every claim you make)
==================================================================
Every factual claim in your output MUST carry three fields:
  - "evidence_quote": VERBATIM text from the transcript (not paraphrased).
    Keep <= 50 words. Use "..." if you trim. The exact wording matters.
  - "source_quarter": "Q3 2026" style. Which call it came from.
  - "speaker": one of "ceo", "cfo", "coo", "analyst:<firm>", "other"

If you cannot cite a verbatim quote for a claim, DO NOT MAKE THE CLAIM.
Omit the field or leave the list empty. A shorter honest output is
better than a padded speculative one.

PARAPHRASING IS FORBIDDEN in evidence_quote. Copy the exact words
including hedging ("we expect", "we believe", "we think"), disfluencies
("you know", "I mean"), and magnitude modifiers ("strong", "solid",
"fine", "encouraging"). The precise language changes the interpretation.
"""


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class SubagentResult:
    """One subagent's output."""
    subagent_name: str
    ticker: str
    ok: bool = True
    data: dict = field(default_factory=dict)   # subagent-specific structure
    error: str | None = None
    api_duration_seconds: float = 0.0
    input_chars: int = 0
    output_chars: int = 0


class SubagentError(Exception):
    """Raised when a subagent fails irrecoverably (caller catches, marks result.ok=False)."""


# --------------------------------------------------------------------------
# Robust JSON parse (lifted from old transcript_analyzer.py)
# --------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _extract_outer_object(text: str) -> str | None:
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
    return text[start:]


def _repair_json(s: str) -> str:
    # Smart quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    # Trailing commas
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _close_unbalanced(s: str) -> str:
    in_str = False
    escape = False
    depth = 0
    for ch in s:
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
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    if in_str:
        cut = max(s.rfind('",'), s.rfind('",{'))
        if cut > 0:
            s = s[:cut + 1]
    s = s.rstrip().rstrip(",")
    s += "]" * max(0, s.count("[") - s.count("]"))
    s += "}" * max(0, s.count("{") - s.count("}"))
    return s


def robust_json_parse(text: str, verbose: bool = False) -> dict | None:
    """Try hard to parse a Claude response as JSON. Returns dict or None."""
    text = _strip_code_fences(text)
    obj = _extract_outer_object(text)
    if obj is None:
        return None
    for attempt, transform in enumerate([
        lambda s: s,
        _repair_json,
        lambda s: _close_unbalanced(_repair_json(s)),
    ]):
        try:
            return json.loads(transform(obj))
        except json.JSONDecodeError as e:
            if verbose:
                print(f"    parse attempt {attempt+1}: {e.msg} at col {e.colno}")
    return None


# --------------------------------------------------------------------------
# Claude API caller
# --------------------------------------------------------------------------

# Read key the same way other modules do
from research.deep_research import ANTHROPIC_API_KEY  # noqa: E402


def call_subagent(
    subagent_name: str,
    ticker: str,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 5000,
    temperature: float = 0.2,
    timeout: float = 180.0,
    verbose: bool = False,
) -> SubagentResult:
    """
    Make a Claude API call for a subagent. Returns SubagentResult.

    - temperature 0.2 (not default 1.0) to reduce stochastic variance
    - Retries once with a stricter instruction on first JSON parse failure
    - Always returns a result (ok=False on failure) — never raises to caller
    """
    result = SubagentResult(
        subagent_name=subagent_name,
        ticker=ticker,
        input_chars=len(system_prompt) + len(user_prompt),
    )
    if not ANTHROPIC_API_KEY:
        result.ok = False
        result.error = "No ANTHROPIC_API_KEY in env"
        return result

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    def _post(prompt_override: str = None) -> httpx.Response:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt_override or user_prompt}],
            "metadata": {"user_id": f"subagent_{subagent_name}"},
        }
        return httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=timeout,
        )

    start = time.time()
    try:
        # Retry up to 4 times on HTTP 429 (rate limit) and 529 (overloaded)
        # with jittered exponential backoff. These are transient and common
        # with parallel subagent dispatch. Jitter decorrelates simultaneous
        # 429s from multiple subagents that would otherwise all wake at the
        # same second and collide again.
        resp = None
        base_backoff = 5.0
        for attempt in range(4):
            resp = _post()
            if resp.status_code not in (429, 529):
                break
            # Exponential + 0-3s jitter: 5-8s, 10-13s, 20-23s, 40-43s
            wait = base_backoff * (2 ** attempt) + random.uniform(0, 3)
            if verbose:
                print(f"    [{subagent_name}] HTTP {resp.status_code}, backoff {wait:.1f}s (attempt {attempt+1}/4)")
            time.sleep(wait)

        if resp.status_code != 200:
            result.ok = False
            result.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            result.api_duration_seconds = time.time() - start
            return result

        text = resp.json()["content"][0]["text"]
        result.output_chars = len(text)
        parsed = robust_json_parse(text, verbose=verbose)

        if parsed is None:
            # Retry once with stricter instruction
            if verbose:
                print(f"    [{subagent_name}] first parse failed, retrying strict...")
            strict = user_prompt + (
                "\n\nCRITICAL: Respond with ONE valid JSON object and nothing else. "
                "No prose before or after. No code fences. Escape all inner quotes as \\\". "
                "No trailing commas. Keep under the max_tokens limit."
            )
            resp = _post(strict)
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                result.output_chars = len(text)
                parsed = robust_json_parse(text, verbose=verbose)

        result.api_duration_seconds = time.time() - start

        if parsed is None:
            result.ok = False
            result.error = "JSON parse failed after retry"
            return result

        result.data = parsed
        return result

    except Exception as e:
        result.ok = False
        result.error = f"exception: {type(e).__name__}: {e}"
        result.api_duration_seconds = time.time() - start
        return result
