"""
Deck Analyzer — orchestrator for 8 specialized vision subagents.

Takes a SlideDeck (PDF + metadata from ingestion.loaders.slide_deck_loader)
or raw PDF bytes, rasterizes pages, dispatches 8 subagents in parallel
against Claude Vision, and caches the aggregated digest.

Public API:
    analyze_deck(pdf_bytes=..., ticker=..., deck_type=..., ...) -> DeckDigest
    analyze_slide_deck(slide_deck) -> DeckDigest   # takes a SlideDeck object

CLI:
    python -m research.deck_analyzer <PDF_PATH> --ticker TICKER
        [--deck-type earnings|investor_day|conference|shareholder_letter|other]
        [--force] [--verbose] [--summary-only]
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

from research.deck_subagents import (
    DeckSubagentResult, rasterize_pdf_cached,
)
from research.deck_subagents import (
    deck_guidance_extractor,
    lrp_extractor,
    qtd_color_extractor,
    business_narrative_extractor,
    segment_economics_extractor,
    capital_allocation_framework,
    targets_vs_reality,
    new_initiatives_extractor,
)


# --------------------------------------------------------------------------
# Subagent registry with deck-type gating
# --------------------------------------------------------------------------

# Each entry: (name, run_fn, applies_to_deck_types)
# applies_to_deck_types: None = all, otherwise set of strings
SUBAGENTS = [
    ("deck_guidance_extractor",      deck_guidance_extractor.run_deck_guidance_extractor,      None),
    ("lrp_extractor",                 lrp_extractor.run_lrp_extractor,                           None),
    ("qtd_color_extractor",           qtd_color_extractor.run_qtd_color_extractor,               {"earnings"}),
    ("business_narrative_extractor",  business_narrative_extractor.run_business_narrative_extractor, None),
    ("segment_economics_extractor",   segment_economics_extractor.run_segment_economics_extractor, None),
    ("capital_allocation_framework",  capital_allocation_framework.run_capital_allocation_framework, None),
    ("targets_vs_reality",            targets_vs_reality.run_targets_vs_reality,                 None),
    ("new_initiatives_extractor",     new_initiatives_extractor.run_new_initiatives_extractor,   None),
]


# --------------------------------------------------------------------------
# Digest dataclass
# --------------------------------------------------------------------------

@dataclass
class DeckDigest:
    ticker: str = ""
    deck_type: str = ""            # earnings | investor_day | conference | shareholder_letter | other
    pdf_content_hash: str = ""
    generated_at: str = ""
    page_count: int = 0
    dpi_used: int = 150
    subagents: dict = field(default_factory=dict)
    # Each entry: {ok, data, error, api_duration_seconds, pages_analyzed, output_chars}
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_prompt_text(self) -> str:
        """Format the 8 subagent outputs as a text block for injection
        into the deep_research brief prompt."""
        lines = [f"=== DECK ANALYSIS: {self.ticker} ({self.deck_type}, {self.page_count} pages) ==="]
        lines.append(f"Generated: {self.generated_at}")
        lines.append("")
        for name, _, _ in SUBAGENTS:
            entry = self.subagents.get(name) or {}
            if not entry.get("ok"):
                lines.append(f"--- {name.upper()}: {'SKIPPED' if entry.get('skipped') else 'FAILED'} "
                             f"({entry.get('error', entry.get('skip_reason', '?'))}) ---")
                lines.append("")
                continue
            lines.append(f"--- {name.upper()} ---")
            lines.append(json.dumps(entry.get("data", {}), indent=2, default=str))
            lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

DIGEST_CACHE_DIR = Path("data/deck_digests")


def _digest_cache_path(ticker: str, pdf_content_hash: str) -> Path:
    return DIGEST_CACHE_DIR / f"{ticker.upper()}_{pdf_content_hash}.json"


def _load_cached(path: Path) -> DeckDigest | None:
    try:
        with open(path) as f:
            data = json.load(f)
        fields = {k: v for k, v in data.items() if k in DeckDigest.__dataclass_fields__}
        return DeckDigest(**fields)
    except Exception:
        return None


def _save_digest(digest: DeckDigest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(digest.to_dict(), f, indent=2, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def analyze_deck(
    *,
    pdf_bytes: bytes,
    ticker: str,
    deck_type: str = "earnings",
    dpi: int = 150,
    cross_reference_text: str = "",
    max_parallel: int = 1,
    force: bool = False,
    verbose: bool = False,
) -> DeckDigest | None:
    """
    Analyze a deck PDF and return a DeckDigest.

    Args:
        pdf_bytes: raw PDF content
        ticker: stock ticker
        deck_type: earnings | investor_day | conference | shareholder_letter | other
        dpi: rasterization DPI (150 default — bump to 200 for dense
             financial tables)
        cross_reference_text: pre-existing transcript digest + press
            release text for the same period (fed into guidance extractor
            for "incremental to transcript" detection)
        max_parallel: concurrent subagent calls. Default 1 — each vision
                      call consumes ~100K input tokens (56 images at 150
                      DPI), so parallel requests blow Anthropic's ITPM
                      limit. Serial execution = ~5-8 min for 7 subagents
                      but completes reliably. Bump to 2 only on higher tiers.
        force: bypass cache, rebuild everything
        verbose: print progress

    Returns a DeckDigest or None if rasterization fails.
    """
    ticker = ticker.upper().strip()
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]

    def v(msg):
        if verbose:
            print(msg)

    # 1. Check digest cache — smart partial-retry similar to transcript analyzer
    cache_path = _digest_cache_path(ticker, pdf_hash)
    digest: DeckDigest | None = None
    subagents_to_run = [
        (name, fn, gates) for name, fn, gates in SUBAGENTS
        if gates is None or deck_type in gates
    ]
    gated_out = [
        (name, gates) for name, _, gates in SUBAGENTS
        if gates is not None and deck_type not in gates
    ]

    if cache_path.exists() and not force:
        cached = _load_cached(cache_path)
        if cached is not None and cached.subagents:
            need_rerun = [name for name, fn, gates in subagents_to_run
                          if name not in cached.subagents
                          or not (cached.subagents.get(name) or {}).get("ok")]
            if not need_rerun:
                v(f"  Deck analyzer: DIGEST CACHE HIT {cache_path.name}")
                return cached
            v(f"  Deck analyzer: partial cache hit — rerunning {len(need_rerun)}: {', '.join(need_rerun)}")
            digest = cached
            subagents_to_run = [(n, fn, g) for n, fn, g in subagents_to_run if n in need_rerun]

    # 2. Rasterize PDF (or load cached rasterization)
    v(f"  Deck analyzer: rasterizing PDF ({len(pdf_bytes):,} bytes) @ {dpi} DPI...")
    pages = rasterize_pdf_cached(pdf_bytes, dpi=dpi, verbose=verbose)
    if not pages:
        v(f"  Deck analyzer: rasterization failed")
        return None
    v(f"  Deck analyzer: {len(pages)} pages rasterized")

    # 3. Initialize digest
    if digest is None:
        digest = DeckDigest(
            ticker=ticker,
            deck_type=deck_type,
            pdf_content_hash=pdf_hash,
            generated_at=datetime.utcnow().isoformat() + "Z",
            page_count=len(pages),
            dpi_used=dpi,
        )
    else:
        digest.generated_at = datetime.utcnow().isoformat() + "Z"
        digest.page_count = len(pages)

    # Record gated-out subagents so caller sees why they're absent
    for name, gates in gated_out:
        if name not in digest.subagents:
            digest.subagents[name] = {
                "ok": False,
                "skipped": True,
                "skip_reason": f"deck_type={deck_type} not in applicable types {sorted(gates)}",
                "data": {},
                "error": None,
                "api_duration_seconds": 0,
                "pages_analyzed": 0,
                "output_chars": 0,
            }

    # 4. Dispatch subagents in parallel
    v(f"  Deck analyzer: dispatching {len(subagents_to_run)} subagent(s) "
      f"(max_parallel={max_parallel})...")

    def _run(name: str, fn, gates) -> tuple[str, DeckSubagentResult]:
        # deck_guidance_extractor takes cross_reference_text
        if name == "deck_guidance_extractor":
            result = fn(ticker, pages, cross_reference_text=cross_reference_text, verbose=verbose)
        else:
            result = fn(ticker, pages, verbose=verbose)
        return name, result

    # When max_parallel=1, run sequentially with a pause between calls to
    # let Anthropic's ITPM window recover. Each vision call burns ~100K
    # input tokens; without a pause, back-to-back calls hit rate limits
    # even with max_parallel=1.
    if max_parallel <= 1:
        import time as _time
        for idx, (name, fn, gates) in enumerate(subagents_to_run):
            if idx > 0:
                pause = 20.0
                v(f"    [pause] sleeping {pause}s between subagents for ITPM recovery")
                _time.sleep(pause)
            try:
                name, result = _run(name, fn, gates)
            except Exception as e:
                digest.errors.append(f"subagent raised: {type(e).__name__}: {e}")
                continue
            digest.subagents[name] = {
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
                "api_duration_seconds": round(result.api_duration_seconds, 2),
                "pages_analyzed": result.pages_analyzed,
                "output_chars": result.output_chars,
            }
            status = "ok" if result.ok else f"FAILED ({(result.error or '')[:80]})"
            v(f"    [{name}] {status} ({result.api_duration_seconds:.1f}s, "
              f"{result.output_chars:,}ch out)")
    else:
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = [executor.submit(_run, name, fn, gates)
                       for name, fn, gates in subagents_to_run]
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
                    "pages_analyzed": result.pages_analyzed,
                    "output_chars": result.output_chars,
                }
                status = "ok" if result.ok else f"FAILED ({(result.error or '')[:80]})"
                v(f"    [{name}] {status} ({result.api_duration_seconds:.1f}s, "
                  f"{result.output_chars:,}ch out)")

    # 5. Cache
    _save_digest(digest, cache_path)
    v(f"  Deck analyzer: cached to {cache_path.name}")

    return digest


def analyze_slide_deck(slide_deck, *, cross_reference_text: str | None = None,
                       auto_cross_reference: bool = True,
                       force: bool = False, verbose: bool = False,
                       max_parallel: int = 1) -> DeckDigest | None:
    """
    Convenience wrapper: take a SlideDeck object (from
    ingestion.loaders.slide_deck_loader) and analyze the underlying PDF.

    Reads the raw PDF from `slide_deck.pdf_local_path` so this doesn't
    need to re-download from the source URL.

    Args:
        slide_deck: a SlideDeck object from the ingestion loaders
        cross_reference_text: explicit cross-reference text. If None AND
            auto_cross_reference is True, we auto-load the matching
            transcript digest + press release from cache and format them.
            Pass "" to explicitly disable cross-reference (guidance
            extractor will flag all deck guides as potentially incremental).
        auto_cross_reference: if cross_reference_text is None, auto-build
            one from cached transcript + press release data. Default True.
        force / verbose / max_parallel: passed through to analyze_deck.
    """
    if not slide_deck.pdf_local_path:
        if verbose:
            print(f"  Deck analyzer: no pdf_local_path on SlideDeck")
        return None
    pdf_path = Path(slide_deck.pdf_local_path)
    if not pdf_path.exists():
        if verbose:
            print(f"  Deck analyzer: PDF not on disk at {pdf_path}")
        return None

    # Auto-populate cross-reference from cached transcript digest + press release
    if cross_reference_text is None and auto_cross_reference:
        from research.cross_reference_builder import build_cross_reference_for_deck
        cross_reference_text = build_cross_reference_for_deck(slide_deck, verbose=verbose)
    elif cross_reference_text is None:
        cross_reference_text = ""

    pdf_bytes = pdf_path.read_bytes()
    return analyze_deck(
        pdf_bytes=pdf_bytes,
        ticker=slide_deck.ticker,
        deck_type=slide_deck.deck_type or "other",
        cross_reference_text=cross_reference_text,
        force=force,
        verbose=verbose,
        max_parallel=max_parallel,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m research.deck_analyzer",
        description="Run 8 vision subagents against an investor deck PDF.",
    )
    ap.add_argument("pdf_path", help="Path to the deck PDF")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--deck-type", default="earnings",
                    choices=["earnings", "investor_day", "conference",
                             "shareholder_letter", "other"])
    ap.add_argument("--dpi", type=int, default=150,
                    help="Rasterization DPI (default 150; bump to 200 for dense tables)")
    ap.add_argument("--max-parallel", type=int, default=1,
                    help="Concurrent subagent calls (default 1; each burns ~100K input tokens, parallel hits ITPM limits)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--quarter", type=str, default="",
                    help="Fiscal quarter hint for cross-reference matching (e.g., 'Q3 2026')")
    ap.add_argument("--no-cross-ref", action="store_true",
                    help="Disable auto-built cross-reference from transcript digest + press release")
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return 2
    pdf_bytes = pdf_path.read_bytes()

    # Auto-build cross-reference from cached data unless disabled
    cross_ref = ""
    if not args.no_cross_ref:
        from research.cross_reference_builder import build_cross_reference
        cross_ref = build_cross_reference(args.ticker, args.quarter, verbose=args.verbose)
        if args.verbose and cross_ref:
            print(f"  Deck analyzer: cross-reference context ready ({len(cross_ref):,} chars)")
        elif args.verbose:
            print(f"  Deck analyzer: no cross-reference available "
                  f"(no cached transcript digest or press release for {args.ticker} {args.quarter})")

    digest = analyze_deck(
        pdf_bytes=pdf_bytes,
        ticker=args.ticker,
        deck_type=args.deck_type,
        dpi=args.dpi,
        cross_reference_text=cross_ref,
        max_parallel=args.max_parallel,
        force=args.force,
        verbose=args.verbose,
    )
    if digest is None:
        print(f"FAILED: no digest produced")
        return 2

    print()
    print(f"=== {digest.ticker} deck digest ({digest.deck_type}) ===")
    print(f"Pages:        {digest.page_count}")
    print(f"PDF hash:     {digest.pdf_content_hash}")
    print(f"Generated:    {digest.generated_at}")
    ok_n = sum(1 for v in digest.subagents.values() if v.get("ok"))
    total = sum(1 for name, _, _ in SUBAGENTS)
    print(f"Subagents ok: {ok_n} / {total}")
    for name, _, gates in SUBAGENTS:
        entry = digest.subagents.get(name) or {}
        if entry.get("skipped"):
            status = f"SKIPPED ({entry.get('skip_reason', '?')[:60]})"
        elif entry.get("ok"):
            status = "ok"
        else:
            status = f"FAIL: {(entry.get('error') or '')[:60]}"
        dur = entry.get("api_duration_seconds", 0)
        print(f"  {name:<33} {dur}s  {status}")

    if not args.summary_only:
        print()
        print("=" * 70)
        print(digest.to_prompt_text())

    return 0


if __name__ == "__main__":
    sys.exit(_main())
