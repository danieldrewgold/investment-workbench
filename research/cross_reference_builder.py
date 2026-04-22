"""
Cross-reference builder.

Given a SlideDeck (or a raw ticker+quarter), assembles a compact text
block showing what was ALREADY publicly disclosed via:
  - Earnings call transcripts (via cached transcript digest)
  - Quarterly earnings press releases (via cached press release data)

This cross-reference text is passed into
`deck_guidance_extractor.run_deck_guidance_extractor()` so the vision
subagent can accurately flag which deck guides are INCREMENTAL to what's
already public (vs. just repeating the press release numbers).

Without this wiring, the guidance extractor conservatively marks
everything as incremental — which is how the first WING run returned
"12 of 12 guides are incremental" even though some numbers came from
the concurrent press release.

Public API:
    build_cross_reference_for_deck(slide_deck, *, verbose=False) -> str
    build_cross_reference(ticker, quarter, *, verbose=False) -> str
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import asdict
from pathlib import Path


# Character budget for the cross-reference text. The deck_guidance_extractor
# prompt caps cross_reference_context at 8K chars, but that's a hard clip —
# we aim at ~6K here so the most important content is never truncated.
MAX_CROSS_REF_CHARS = 6500


# --------------------------------------------------------------------------
# Transcript digest loading
# --------------------------------------------------------------------------

def _most_recent_transcript_digest_path(ticker: str) -> Path | None:
    """Return the most-recent cached transcript digest JSON path for ticker."""
    pattern = f"data/transcript_digests/{ticker.upper()}_*.json"
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Most-recent by mtime — the hash in the filename isn't a timestamp
    return Path(max(matches, key=os.path.getmtime))


def _load_transcript_digest(ticker: str) -> dict | None:
    """Return the cached transcript digest dict for ticker, or None."""
    path = _most_recent_transcript_digest_path(ticker)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _extract_transcript_guidance_evidence(digest: dict) -> list[str]:
    """
    Pull the parts of the transcript digest the guidance extractor
    needs to see: guidance_tracker guides + qtd_extractor remarks.

    Returns a list of formatted lines ready for prompt injection.
    """
    lines: list[str] = []
    subagents = digest.get("subagents") or {}

    # Guidance from transcripts
    gt = (subagents.get("guidance_tracker") or {}).get("data") or {}
    live = gt.get("current_live_guides") or []
    history = gt.get("guides_issued") or []

    if live:
        lines.append("Currently live guides (from earnings calls):")
        for g in live[:10]:
            metric = g.get("metric", "?")
            period = g.get("period_guided", "?")
            statement = g.get("most_recent_statement", "?")
            qtr = g.get("source_quarter", "?")
            change = g.get("change_from_prior", "?")
            lines.append(f"  - [{qtr}] {metric} ({period}): {statement[:180]} — {change}")

    if history:
        lines.append("\nRecent guides issued on calls:")
        # Prefer the most recent 6 entries
        for g in history[-8:]:
            metric = g.get("metric", "?")
            period = g.get("period_guided", "?")
            value = g.get("guide_value", "?")
            qtr = g.get("source_quarter", "?")
            gtype = g.get("guide_type", "?")
            lines.append(f"  - [{qtr}] {metric} / {period}: {value} ({gtype})")

    # QTD commentary
    qtd = (subagents.get("qtd_extractor") or {}).get("data") or {}
    remarks = qtd.get("qtd_remarks") or []
    if remarks:
        lines.append("\nQTD color from calls:")
        for r in remarks[:5]:
            qtr = r.get("source_quarter", "?")
            period = r.get("qtd_period", "?")
            mag = r.get("magnitude", "?")
            verbatim = (r.get("remark_verbatim") or "")[:200]
            lines.append(f"  - [{qtr} on {period}] {mag}: \"{verbatim}\"")

    # Business understanding — relevant for framing
    bu = (subagents.get("business_understanding") or {}).get("data") or {}
    summary = bu.get("summary") or {}
    if summary.get("primary_growth_driver_currently"):
        lines.append(f"\nPrimary growth driver per calls: {summary['primary_growth_driver_currently']}")

    return lines


# --------------------------------------------------------------------------
# Press release loading
# --------------------------------------------------------------------------

def _press_release_paths_for_ticker(ticker: str) -> list[Path]:
    """Return all cached press release JSON paths for ticker, most recent first."""
    pattern = f"data/press_releases/{ticker.upper()}_*.json"
    matches = glob.glob(pattern)
    return [Path(p) for p in sorted(matches, key=os.path.getmtime, reverse=True)]


def _load_press_release(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _find_press_release_for_quarter(ticker: str, quarter: str) -> dict | None:
    """
    Find a cached press release whose `quarter` matches the target.

    If no exact match, returns the most-recent cached release (useful
    for investor day decks where there's no specific matching quarter —
    the most recent PR is the closest concurrent public disclosure).
    """
    paths = _press_release_paths_for_ticker(ticker)
    if not paths:
        return None

    target = (quarter or "").strip()

    # Exact match first
    if target:
        for p in paths:
            pr = _load_press_release(p)
            if pr and (pr.get("quarter") or "").strip() == target:
                return pr

    # Fallback: most recent
    return _load_press_release(paths[0])


def _format_press_release_for_guidance(pr: dict) -> list[str]:
    """
    Extract the parts of a press release that matter for guidance
    cross-reference: the full text (capped) and any tables that look
    guidance-related (by table heading context).
    """
    lines: list[str] = []
    quarter = pr.get("quarter") or "?"
    report_date = pr.get("report_date") or "?"
    lines.append(f"Press release {quarter} (filed {report_date})")

    # Prose — cap hard because PR bodies can be ~20K chars
    text = pr.get("text") or ""
    if text:
        # Focus on guidance-like paragraphs — the last third of a PR
        # almost always holds the outlook / guidance section.
        body = text.strip()
        # Heuristic: take the last 2500 chars + anything with guidance words
        relevant_parts: list[str] = []
        lower = body.lower()
        for kw in ("outlook", "guidance", "we expect", "we continue to expect",
                   "we anticipate", "full year", "fiscal year", "full-year"):
            idx = lower.find(kw)
            while idx >= 0 and len(relevant_parts) < 6:
                start = max(0, idx - 120)
                end = min(len(body), idx + 500)
                relevant_parts.append(body[start:end])
                idx = lower.find(kw, end)
        # Dedupe overlapping snippets
        seen_hashes = set()
        clean_parts = []
        for p in relevant_parts:
            h = hash(p[:100])
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            clean_parts.append(p)
        if clean_parts:
            lines.append("Guidance-adjacent prose from press release:")
            for p in clean_parts[:4]:
                lines.append(f"  ...{p.strip()[:600]}...")
        else:
            # No keyword hits — fall back to the tail
            lines.append(f"Press release tail: ...{body[-1500:].strip()}...")

    # Tables that might carry guidance
    tables = pr.get("tables") or []
    if tables:
        lines.append(f"\nPress release tables ({len(tables)} tables found):")
        shown = 0
        for t in tables:
            heading = t.get("heading_context") or ""
            heading_low = heading.lower()
            # Only include tables whose heading suggests guidance or outlook
            is_guidance = any(kw in heading_low for kw in
                               ("outlook", "guid", "expect", "target", "full year",
                                "full-year", "2026", "2027", "2028"))
            if not is_guidance and shown >= 1:
                continue
            md = t.get("markdown") or ""
            if md:
                lines.append(f"  Table (heading: {heading[:80]!r}):")
                for row_line in md.split("\n")[:10]:
                    lines.append(f"    {row_line[:200]}")
                shown += 1
                if shown >= 3:
                    break

    return lines


# --------------------------------------------------------------------------
# Main assembler
# --------------------------------------------------------------------------

def build_cross_reference(
    ticker: str,
    quarter: str = "",
    *,
    verbose: bool = False,
) -> str:
    """
    Build a cross-reference text block for a ticker (+ optional quarter).

    Returns an empty string if no cached data is available. Callers use
    the empty string as "no cross-reference context provided" signal —
    the guidance extractor handles that gracefully.
    """
    ticker = ticker.upper().strip()
    sections: list[str] = []

    # Transcript side
    digest = _load_transcript_digest(ticker)
    if digest:
        transcript_lines = _extract_transcript_guidance_evidence(digest)
        if transcript_lines:
            sections.append("--- TRANSCRIPT (already disclosed on calls) ---")
            sections.extend(transcript_lines)
            if verbose:
                print(f"  [XREF] loaded transcript digest for {ticker} "
                      f"({digest.get('quarters_count', '?')} quarters)")
    elif verbose:
        print(f"  [XREF] no cached transcript digest for {ticker}")

    # Press release side
    pr = _find_press_release_for_quarter(ticker, quarter)
    if pr:
        sections.append("\n--- PRESS RELEASE (already public) ---")
        sections.extend(_format_press_release_for_guidance(pr))
        if verbose:
            print(f"  [XREF] loaded press release {pr.get('quarter', '?')} "
                  f"({pr.get('accession', '?')})")
    elif verbose:
        print(f"  [XREF] no cached press release for {ticker} {quarter or ''}")

    if not sections:
        return ""

    out = "\n".join(sections)
    if len(out) > MAX_CROSS_REF_CHARS:
        out = out[:MAX_CROSS_REF_CHARS] + "\n...[cross-reference truncated]"
    if verbose:
        print(f"  [XREF] built cross-reference: {len(out):,} chars")
    return out


def build_cross_reference_for_deck(
    slide_deck,
    *,
    verbose: bool = False,
) -> str:
    """Convenience wrapper: pull ticker + quarter from a SlideDeck object."""
    return build_cross_reference(
        ticker=slide_deck.ticker,
        quarter=slide_deck.quarter or "",
        verbose=verbose,
    )
