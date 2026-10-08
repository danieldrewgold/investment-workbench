"""Minimal Claude caller for the call layer (raw HTTP, matching the project)."""

from __future__ import annotations

import json
import random
import time

import httpx

from research.deep_research import ANTHROPIC_API_KEY

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"


class LLMError(RuntimeError):
    pass


def _text(data: dict) -> str:
    return "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text")


def call_json(system: str, user: str, *, model: str = OPUS, effort: str = "high",
              max_tokens: int = 32000, timeout: float = 1200.0) -> dict:
    """One Messages call that must return a JSON object. Retries 429/529."""
    if not ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY not set")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "fallbacks": "default",
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "server-side-fallback-2026-07-01",
        "content-type": "application/json",
    }
    resp = None
    for attempt in range(5):
        resp = httpx.post("https://api.anthropic.com/v1/messages", headers=headers,
                          json=body, timeout=timeout)
        if resp.status_code not in (429, 529):
            break
        time.sleep(min(20 * 1.6 ** attempt, 120) + random.uniform(0, 3))
    if resp is None or resp.status_code != 200:
        raise LLMError(f"HTTP {getattr(resp, 'status_code', '?')}: {getattr(resp, 'text', '')[:300]}")
    data = resp.json()
    if data.get("stop_reason") == "refusal":
        raise LLMError("model refused")
    text = _text(data).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start = text.find("{")
    if start < 0:
        raise LLMError(f"no JSON object in response (stop_reason={data.get('stop_reason')})")
    try:
        # raw_decode takes the first complete object and ignores anything after it
        obj, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
        return obj
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON parse failed: {e} (stop_reason={data.get('stop_reason')})")
