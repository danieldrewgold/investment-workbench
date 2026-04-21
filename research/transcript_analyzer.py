"""
Transcript Analyzer — orchestrator for 8 specialized subagents.

Replaces the old monolithic digest. Fetches multi-quarter transcripts,
builds a structured context pack, dispatches 8 subagents in parallel,
aggregates results, and caches.

Public API:
    analyze_transcripts(ticker, transcript_text=None, verbose=False, force=False)
        -> TranscriptDigest | None

TranscriptDigest.to_prompt_text() formats the 8 subagent outputs into a
text block suitable for injection into the deep_research brief prompt.

CLI:
    python -m research.transcript_analyzer CMG [--force] [--verbose]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from research.transcript_subagents._base import SubagentResult
from research.transcript_subagents._context_pack import (
    ContextPack, build_context_pack,
)
from research.transcript_subagents import (
    guidance_tracker, qanda_analyzer, tone_tracker, qtd_extractor,
    metrics_highlighted, business_understanding, capital_allocation,
    unusual_disclosures,
)


# --------------------------------------------------------------------------
# Subagent registry
# --------------------------------------------------------------------------

SUBAGENTS = [
    ("guidance_tracker", guidance_tracker.run_guidance_tracker),
    ("qanda_analyzer", qanda_analyzer.run_qanda_analyzer),
    ("tone_tracker", tone_tracker.run_tone_tracker),
    ("qtd_extractor", qtd_extractor.run_qtd_extractor),
    ("metrics_highlighted", metrics_highlighted.run_metrics_highlighted),
    ("business_understanding", business_understanding.run_business_understanding),
    ("capital_allocation", capital_allocation.run_capital_allocation),
    ("unusual_disclosures", unusual_disclosures.run_unusual_disclosures),
]


# --------------------------------------------------------------------------
# Digest dataclass
# --------------------------------------------------------------------------

@dataclass
class TranscriptDigest:
    ticker: str = ""
    generated_at: str = ""
    context_pack_hash: str = ""
    quarters_count: int = 0
    subagents: dict = field(default_factory=dict)
    # Each entry: {ok: bool, data: {...}, error: str|None, api_duration_seconds: float}
    errors: list = field(default_factory=list)
    # High-level convenience fields derived from subagent outputs; populated by _derive()
    tone_trajectory: str = ""
    management_credibility: str = ""
    recurring_concerns: list = field(default_factory=list)
    key_inflection_points: list = field(default_factory=list)
    guidance_evolution: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    # ----------------------------------------------------------------
    # Back-compat derived fields (used by pipeline.py + Word report)
    # ----------------------------------------------------------------

    def _derive(self) -> None:
        tt = (self.subagents.get("tone_tracker") or {}).get("data") or {}
        self.tone_trajectory = (tt.get("tone_trajectory_summary") or {}).get("overall_direction", "")

        gt = (self.subagents.get("guidance_tracker") or {}).get("data") or {}
        self.management_credibility = (gt.get("summary") or {}).get("net_credibility_read", "")

        # Guidance evolution = most recent live guides (with the per-entry "change_from_prior")
        live = gt.get("current_live_guides") or []
        self.guidance_evolution = [
            f"{g.get('metric','?')} ({g.get('period_guided','?')}): {g.get('most_recent_statement','?')} — {g.get('change_from_prior','?')} [{g.get('source_quarter','?')}]"
            for g in live[:6]
        ]

        # Recurring concerns from Q&A themes
        qa = (self.subagents.get("qanda_analyzer") or {}).get("data") or {}
        themes = qa.get("recurring_themes") or []
        self.recurring_concerns = [
            f"{t.get('theme','?')} (asked in {', '.join(t.get('quarters_asked',[])[:3])})"
            for t in themes[:5]
        ]

        # Inflection points from tone + unusual disclosures (top topics)
        inflections = tt.get("inflection_points") or []
        self.key_inflection_points = [
            {
                "quarter": "→".join((ip.get("between_quarters") or ["?", "?"])[:2]),
                "description": f"{ip.get('topic','?')}: {ip.get('shift_type','?')} — {ip.get('significance','')[:100]}",
            }
            for ip in inflections[:4]
        ]

    # ----------------------------------------------------------------
    # Prompt-ready text block for deep_research brief
    # ----------------------------------------------------------------

    def to_prompt_text(self) -> str:
        """
        Format the 8 subagent outputs into a verbose, structured text block
        for injection into the deep_research brief prompt. Preserves the
        evidence-grounded detail — no summarization beyond what subagents
        already produced.
        """
        lines = [f"=== TRANSCRIPT INSIGHTS: {self.ticker} ({self.quarters_count} quarters) ==="]
        lines.append(f"Generated: {self.generated_at}")
        lines.append("")

        for name, _ in SUBAGENTS:
            entry = self.subagents.get(name) or {}
            if not entry.get("ok"):
                err = entry.get("error", "(no error recorded)")
                lines.append(f"--- {name.upper()}: FAILED ({err}) ---")
                lines.append("")
                continue
            data = entry.get("data") or {}
            lines.append(f"--- {name.upper()} ---")
            # Each subagent's output is a JSON dict; serialize it verbose but readable
            lines.append(json.dumps(data, indent=2, default=str))
            lines.append("")

        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _digest_cache_path(ticker: str, context_pack_hash: str) -> Path:
    return Path("data/transcript_digests") / f"{ticker.upper()}_{context_pack_hash}.json"


def _load_cached_digest(path: Path) -> TranscriptDigest | None:
    try:
        with open(path) as f:
            data = json.load(f)
        d = TranscriptDigest(**{k: v for k, v in data.items() if k in TranscriptDigest.__dataclass_fields__})
        return d
    except Exception:
        return None


def _save_digest(digest: TranscriptDigest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(digest.to_dict(), f, indent=2, default=str)


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def analyze_transcripts(
    ticker: str,
    transcript_text: str | None = None,
    *,
    verbose: bool = False,
    force: bool = False,
    max_parallel: int = 3,
) -> TranscriptDigest | None:
    """
    Orchestrate the 8 subagents and return a TranscriptDigest.

    Args:
        ticker: ticker symbol
        transcript_text: already-fetched raw transcript text; if None,
            fetches via transcript_fetcher.fetch_transcript_history
        verbose: print progress
        force: bypass cache, rebuild everything
        max_parallel: how many subagents to run concurrently (default 3
            — empirically 4 triggers 429s ~half the time on this tier;
            3 trades ~10-15s latency for materially fewer retries)

    Returns TranscriptDigest on success, None if we can't even get
    transcripts.
    """
    ticker = ticker.upper().strip()

    def v(msg):
        if verbose:
            print(msg)

    # 1. Fetch transcripts if not provided
    if not transcript_text:
        try:
            from research.transcript_fetcher import fetch_transcript_history
            transcript_text = fetch_transcript_history(ticker, quarters=12, verbose=verbose)
        except Exception as e:
            v(f"  Transcript analyzer: fetch failed - {e}")
            return None

    if not transcript_text or len(transcript_text) < 500:
        v(f"  Transcript analyzer: insufficient transcript text ({len(transcript_text or ''):,} chars)")
        return None

    v(f"  Transcript analyzer: {len(transcript_text):,} chars of transcript input")

    # 2. Build (or load) context pack
    pack = build_context_pack(ticker, transcript_text, force=force, verbose=verbose)
    if pack is None:
        v(f"  Transcript analyzer: context pack build failed")
        return None

    # 3. Check digest cache (keyed on context pack raw_hash).
    # Smart cache: if the cached digest has failed subagents, retry only
    # those rather than forcing a full rebuild. A full successful cache hit
    # returns immediately.
    cache_path = _digest_cache_path(ticker, pack.raw_hash)
    digest: TranscriptDigest | None = None
    subagents_to_run = list(SUBAGENTS)

    if cache_path.exists() and not force:
        cached = _load_cached_digest(cache_path)
        if cached is not None and cached.subagents:
            failed = [name for name, entry in cached.subagents.items()
                      if not entry.get("ok")]
            missing = [name for name, _ in SUBAGENTS
                       if name not in cached.subagents]
            to_retry = failed + missing
            if not to_retry:
                v(f"  Transcript analyzer: DIGEST CACHE HIT {cache_path.name} (8/8 ok)")
                cached._derive()
                return cached
            v(f"  Transcript analyzer: partial cache hit — retrying {len(to_retry)} subagent(s): {', '.join(to_retry)}")
            digest = cached
            subagents_to_run = [(n, fn) for n, fn in SUBAGENTS if n in to_retry]
        else:
            v(f"  Transcript analyzer: cache read failed, recomputing")

    # 4. Dispatch subagents in parallel (all 8, or only the failed/missing)
    if digest is None:
        digest = TranscriptDigest(
            ticker=ticker,
            generated_at=datetime.utcnow().isoformat() + "Z",
            context_pack_hash=pack.raw_hash,
            quarters_count=pack.quarters_count,
        )
    else:
        # Update generated_at so we know when the retry happened
        digest.generated_at = datetime.utcnow().isoformat() + "Z"

    v(f"  Transcript analyzer: dispatching {len(subagents_to_run)} subagent(s) (max_parallel={max_parallel})...")

    def _run(name: str, fn) -> tuple[str, SubagentResult]:
        result = fn(pack, verbose=verbose)
        return name, result

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = [executor.submit(_run, name, fn) for name, fn in subagents_to_run]
        for fut in as_completed(futures):
            try:
                name, result = fut.result()
            except Exception as e:
                digest.errors.append(f"subagent future raised: {type(e).__name__}: {e}")
                continue
            digest.subagents[name] = {
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
                "api_duration_seconds": round(result.api_duration_seconds, 2),
                "input_chars": result.input_chars,
                "output_chars": result.output_chars,
            }
            status = "ok" if result.ok else f"FAILED ({result.error[:80] if result.error else 'unknown'})"
            v(f"    [{name}] {status} ({result.api_duration_seconds:.1f}s, {result.output_chars:,}ch out)")

    # 5. Derive convenience fields
    digest._derive()

    # 6. Cache
    _save_digest(digest, cache_path)
    v(f"  Transcript analyzer: cached to {cache_path.name}")

    return digest


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    # Force UTF-8 stdout so unicode chars (→, etc.) don't crash on Windows cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m research.transcript_analyzer",
        description="Run 8 transcript subagents against a ticker.",
    )
    ap.add_argument("ticker", help="Stock ticker (e.g., CMG)")
    ap.add_argument("--force", action="store_true", help="Bypass cache, recompute")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose progress")
    ap.add_argument("--max-parallel", type=int, default=3,
                    help="Subagent concurrency (default 3; bump to 4 if your API tier allows)")
    ap.add_argument("--summary-only", action="store_true",
                    help="Print high-level summary, not full subagent JSON")
    args = ap.parse_args()

    digest = analyze_transcripts(
        args.ticker,
        verbose=args.verbose,
        force=args.force,
        max_parallel=args.max_parallel,
    )
    if digest is None:
        print(f"FAILED: no digest produced for {args.ticker}")
        return 2

    print()
    print(f"=== {digest.ticker} transcript digest ===")
    print(f"Generated:     {digest.generated_at}")
    print(f"Quarters:      {digest.quarters_count}")
    print(f"Pack hash:     {digest.context_pack_hash}")
    print(f"Tone:          {digest.tone_trajectory or '?'}")
    print(f"Credibility:   {digest.management_credibility or '?'}")
    ok_n = sum(1 for v in digest.subagents.values() if v.get("ok"))
    print(f"Subagents ok:  {ok_n} / {len(SUBAGENTS)}")
    for name, _ in SUBAGENTS:
        entry = digest.subagents.get(name) or {}
        status = "ok" if entry.get("ok") else f"FAIL: {(entry.get('error') or '')[:80]}"
        print(f"  {name:<25} {entry.get('api_duration_seconds','?')}s  {status}")

    if digest.guidance_evolution:
        print(f"\nGuidance evolution (top {len(digest.guidance_evolution)}):")
        for g in digest.guidance_evolution:
            print(f"  - {g}")

    if digest.recurring_concerns:
        print(f"\nRecurring concerns:")
        for c in digest.recurring_concerns:
            print(f"  - {c}")

    if digest.key_inflection_points:
        print(f"\nKey inflection points:")
        for ip in digest.key_inflection_points:
            print(f"  [{ip.get('quarter','?')}] {ip.get('description','')}")

    if not args.summary_only:
        print()
        print("=" * 70)
        print("Full subagent outputs:")
        print("=" * 70)
        print(digest.to_prompt_text())

    return 0


if __name__ == "__main__":
    sys.exit(_main())
