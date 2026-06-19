"""
Shared Claude Vision utilities for deck subagents.

Mirrors research.transcript_subagents._base (same structure, same result
type, same retry/JSON repair logic) but calls the vision-capable endpoint
with image content blocks attached.

Each subagent sends ALL deck pages as images in a single Claude Vision
request. Anthropic supports up to 100 images per request; typical decks
are 20-60 pages so this fits.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from dataclasses import dataclass, field

import httpx

# Reuse the robust JSON parser from transcript_subagents — same logic
from research.transcript_subagents._base import robust_json_parse

# Force UTF-8 stdout — verbose logs + error messages sometimes include
# Unicode characters (→, —, ≈) that Windows cp1252 can't encode.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Evidence-grounding callout adapted for vision:
# each claim must cite a page number. "Verbatim" for images means
# "text visible on the slide, transcribed exactly" — if the subagent is
# paraphrasing a chart title or bullet, it must preserve the exact wording.
EVIDENCE_BLOCK_VISION = """
==================================================================
EVIDENCE GROUNDING (mandatory for every claim you make)
==================================================================
Every factual claim in your output MUST carry:
  - "source_page": integer page number (1-indexed) where the fact is shown
  - "evidence_quote": VERBATIM text visible on that slide. If it's a chart
    label or bullet point, transcribe it exactly as rendered. Paraphrasing
    is forbidden here — the precise wording ("we expect" vs "we are
    targeting" vs "we believe") changes meaning materially.

If a claim can only be inferred from a chart's visuals (no readable text),
use "visual_evidence" instead: describe what the chart shows in one
sentence, and STATE that the fact is visual-only.

If you cannot cite either a verbatim quote or a specific visual, DO NOT
MAKE THE CLAIM. Omit fields rather than speculating.
"""


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class DeckSubagentResult:
    """One subagent's output."""
    subagent_name: str
    ticker: str
    ok: bool = True
    data: dict = field(default_factory=dict)
    error: str | None = None
    api_duration_seconds: float = 0.0
    pages_analyzed: int = 0
    output_chars: int = 0


# --------------------------------------------------------------------------
# Claude Vision caller
# --------------------------------------------------------------------------

from research.deep_research import ANTHROPIC_API_KEY  # noqa: E402


def call_vision_subagent(
    subagent_name: str,
    ticker: str,
    system_prompt: str,
    user_prompt: str,
    page_images: list,                 # list[PageImage]
    *,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 6000,
    temperature: float = 0.15,
    timeout: float = 300.0,             # vision calls are slower — 5 min cap
    max_pages: int = 60,                # Anthropic limit is 100; cap lower for speed
    verbose: bool = False,
) -> DeckSubagentResult:
    """
    Make a Claude Vision API call for a deck subagent.

    All page images are attached as base64 content blocks in one user
    message, followed by the text prompt. The model can reference pages
    by index (we also annotate each image's page number in the prompt
    so the model knows what to cite in `source_page`).

    Returns DeckSubagentResult with ok=False on failure (never raises).
    """
    result = DeckSubagentResult(
        subagent_name=subagent_name,
        ticker=ticker,
        pages_analyzed=min(len(page_images), max_pages),
    )
    if not ANTHROPIC_API_KEY:
        result.ok = False
        result.error = "No ANTHROPIC_API_KEY in env"
        return result
    if not page_images:
        result.ok = False
        result.error = "No page images to analyze"
        return result

    # Truncate to max_pages (take first N — cover pages are usually highest-signal)
    pages_used = page_images[:max_pages]

    # Build the user content: alternating page-label text + image blocks,
    # then the instruction prompt. Page labels help the model cite correctly.
    content_blocks: list = []
    for p in pages_used:
        content_blocks.append({
            "type": "text",
            "text": f"--- PAGE {p.page_number} ---",
        })
        content_blocks.append(p.to_anthropic_image_block())
    content_blocks.append({"type": "text", "text": user_prompt})

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": content_blocks}],
        "metadata": {"user_id": f"deck_subagent_{subagent_name}"},
    }

    start = time.time()
    try:
        # Retries on 429/529 with jittered exponential backoff.
        # Vision calls consume ~100K input tokens per request — when we
        # hit ITPM limits the window resets over 60s, so the first retry
        # MUST wait longer than 60s to clear the window. Starting at 20s,
        # scaled up to 5 attempts, covers most transient rate-limit cases.
        resp = None
        base_backoff = 20.0
        max_attempts = 5
        for attempt in range(max_attempts):
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=body, timeout=timeout,
            )
            if resp.status_code not in (429, 529):
                break
            # If the server sent retry-after, respect it (up to 120s)
            retry_after = resp.headers.get("retry-after") or ""
            server_wait = None
            try:
                server_wait = float(retry_after)
            except Exception:
                pass
            if server_wait and 5 <= server_wait <= 180:
                wait = server_wait + random.uniform(0, 2)
            else:
                # Sequence: ~20, 40, 65, 90, 120s (with jitter)
                wait = min(base_backoff * (1.6 ** attempt), 120.0) + random.uniform(0, 5)
            if verbose:
                print(f"    [{subagent_name}] HTTP {resp.status_code}, "
                      f"backoff {wait:.1f}s (attempt {attempt+1}/{max_attempts})")
            time.sleep(wait)

        if resp.status_code != 200:
            result.ok = False
            result.error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            result.api_duration_seconds = time.time() - start
            return result

        text = resp.json()["content"][0]["text"]
        result.output_chars = len(text)
        parsed = robust_json_parse(text, verbose=verbose)

        if parsed is None:
            # Retry once with stricter instruction
            if verbose:
                print(f"    [{subagent_name}] first parse failed, retrying strict...")
            strict_content = content_blocks[:-1] + [{
                "type": "text",
                "text": user_prompt + (
                    "\n\nCRITICAL: Respond with ONE valid JSON object and nothing else. "
                    "No prose, no code fences, no trailing commas. Escape inner quotes."
                ),
            }]
            strict_body = {**body, "messages": [{"role": "user", "content": strict_content}]}
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=strict_body, timeout=timeout,
            )
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
