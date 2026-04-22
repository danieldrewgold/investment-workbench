"""
Slide Deck Loader.

Fetches investor presentation PDFs (typically 8-K Exhibit 99.2 or later)
from EDGAR and extracts text + tables per page using pdfplumber.

What we capture:
  - Per-page text (cleaned)
  - Per-page tables (as 2D string arrays + markdown)
  - Quarter mapping based on filing report date
  - Cached PDF bytes on disk for later re-parsing if we change the extractor

What we DON'T capture (v1):
  - Charts / figures (pdfplumber can't OCR; would need Claude vision)
  - Scanned decks (no text layer — would need OCR)
  - Slide layout / design info (which bullet is visually emphasized)

Public API:
    fetch_slide_decks(ticker, quarters=12, fiscal_year_end_month=12,
                      min_size_bytes=200_000, force=False, verbose=False)
        -> list[SlideDeck]

CLI:
    python -m ingestion.loaders.slide_deck_loader TICKER [--force]
                                                          [--quarters N]
                                                          [--show-page N]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import pdfplumber

from ingestion.loaders._edgar_utils import (
    ExhibitMeta, fetch_exhibit_bytes, infer_earnings_quarter, scan_8k_exhibits,
)


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class SlidePageTable:
    """A single table extracted from one slide page."""
    page_number: int                     # 1-indexed
    table_index_on_page: int             # 0-indexed within the page
    columns: list = field(default_factory=list)       # list[str]
    rows: list = field(default_factory=list)          # list[list[str]]
    markdown: str = ""


@dataclass
class SlidePage:
    """A single page/slide from the deck."""
    page_number: int                     # 1-indexed
    text: str = ""
    tables: list = field(default_factory=list)        # list[SlidePageTable]


@dataclass
class SlideDeck:
    """One investor presentation deck, quarter-mapped + parsed."""
    ticker: str = ""
    quarter: str = ""                     # populated for earnings decks; empty otherwise
    report_date: str = ""
    filing_date: str = ""
    accession: str = ""                   # EDGAR accession; empty if IR-sourced
    filing_type: str = ""
    exhibit_num: str = ""                 # empty if IR-sourced
    source_url: str = ""
    source: str = "edgar"                 # "edgar" | "ir_page"
    deck_type: str = "earnings"           # "earnings" | "investor_day" | "conference" | "shareholder_letter" | "other"
    event_metadata: dict = field(default_factory=dict)
    # Type-specific metadata (e.g., {broker, conference_name} for conference,
    # {event_year, event_name} for investor_day, {period} for shareholder_letter)
    classification_confidence: str = ""   # "explicit" | "inferred" | "fallback"
    title: str = ""                        # human-readable (e.g., "2026 Investor Day")
    page_count: int = 0
    pages: list = field(default_factory=list)         # list[SlidePage]
    total_tables: int = 0
    total_chars: int = 0
    fetched_at: str = ""
    content_hash: str = ""                # sha256 of raw PDF
    pdf_local_path: str = ""              # where we cached the bytes

    def to_dict(self) -> dict:
        return asdict(self)

    def to_prompt_text(self, max_chars: int = 20000) -> str:
        """Condense the deck into a single text block for Claude prompts."""
        header = f"=== {self.ticker} {self.quarter} Slide Deck ({self.report_date}) — {self.page_count} pages ===\n"
        parts = [header]
        remaining = max_chars - len(header)
        for page in self.pages:
            if remaining <= 0:
                parts.append(f"\n...[truncated after {page.page_number - 1} pages]")
                break
            page_header = f"\n--- Slide {page.page_number} ---\n"
            body = page.text.strip()
            table_blocks = "\n\n".join(t.markdown for t in page.tables if t.markdown)
            content = page_header + body + (f"\n\n{table_blocks}" if table_blocks else "")
            if len(content) > remaining:
                content = content[:remaining] + f"\n...[page {page.page_number} truncated]"
            parts.append(content)
            remaining -= len(content)
        return "".join(parts)


# --------------------------------------------------------------------------
# PDF parsing
# --------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"[ \t]+")


def _page_text(page) -> str:
    """Extract clean text from a pdfplumber page."""
    text = page.extract_text() or ""
    # Normalize weird whitespace. Preserve line breaks (meaningful in slides).
    cleaned_lines = []
    for line in text.splitlines():
        line = _WHITESPACE_RE.sub(" ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _page_tables(page, page_number: int) -> list[SlidePageTable]:
    """Extract tables from a pdfplumber page."""
    try:
        raw_tables = page.extract_tables() or []
    except Exception:
        raw_tables = []

    results: list[SlidePageTable] = []
    for ti, raw in enumerate(raw_tables):
        if not raw:
            continue
        # Normalize cells — pdfplumber may return None for empty cells
        rows = []
        for r in raw:
            rows.append([(c or "").strip() for c in r])
        # Drop rows that are entirely blank
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            continue

        n_cols = max(len(r) for r in rows)
        # Header heuristic: first row is header if it has fewer numeric cells
        first = rows[0]
        first_numeric = sum(1 for c in first if re.search(r"\d", c))
        if first_numeric < len(first) / 2:
            columns = first + [""] * (n_cols - len(first))
            data_rows = rows[1:]
        else:
            columns = [f"col{i+1}" for i in range(n_cols)]
            data_rows = rows

        # Pad rows to column width
        def pad(r, w):
            return r + [""] * (w - len(r))

        md_lines = [
            "| " + " | ".join(columns) + " |",
            "|" + "|".join("---" for _ in range(n_cols)) + "|",
        ]
        for r in data_rows:
            md_lines.append("| " + " | ".join(pad(r, n_cols)) + " |")

        results.append(SlidePageTable(
            page_number=page_number,
            table_index_on_page=ti,
            columns=columns,
            rows=data_rows,
            markdown="\n".join(md_lines),
        ))
    return results


def _parse_pdf(content: bytes, *, verbose: bool = False) -> list[SlidePage]:
    """Parse PDF bytes into a list of SlidePage."""
    pages: list[SlidePage] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_num = i + 1
                text = _page_text(page)
                tables = _page_tables(page, page_num)
                pages.append(SlidePage(page_number=page_num, text=text, tables=tables))
    except Exception as e:
        if verbose:
            print(f"  [DECK] pdfplumber open failed: {e}")
        return []
    return pages


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

CACHE_DIR = Path("data/slide_decks")


def _json_cache_path(ticker: str, accession: str) -> Path:
    safe_acc = accession.replace("/", "_")
    return CACHE_DIR / f"{ticker.upper()}_{safe_acc}.json"


def _pdf_cache_path(ticker: str, accession: str, filename: str) -> Path:
    safe_acc = accession.replace("/", "_")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return CACHE_DIR / "pdfs" / f"{ticker.upper()}_{safe_acc}__{safe_name}"


def _load_cached(path: Path) -> SlideDeck | None:
    try:
        with open(path) as f:
            data = json.load(f)
        deck_fields = {k: v for k, v in data.items()
                       if k in SlideDeck.__dataclass_fields__ and k != "pages"}
        pages = []
        for p in data.get("pages", []):
            tables = [SlidePageTable(**t) for t in p.get("tables", [])]
            pages.append(SlidePage(
                page_number=p.get("page_number", 0),
                text=p.get("text", ""),
                tables=tables,
            ))
        deck_fields["pages"] = pages
        return SlideDeck(**deck_fields)
    except Exception:
        return None


def _save_cache(deck: SlideDeck, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(deck.to_dict(), f, indent=2, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------
# Per-exhibit processing
# --------------------------------------------------------------------------

def _process_exhibit(
    ticker: str, meta: ExhibitMeta, fiscal_year_end_month: int,
    *, force: bool = False, verbose: bool = False,
) -> SlideDeck | None:
    """Fetch + parse + cache a single PDF exhibit."""
    json_path = _json_cache_path(ticker, meta.accession)
    if json_path.exists() and not force:
        cached = _load_cached(json_path)
        if cached is not None:
            if verbose:
                print(f"  [DECK] CACHE HIT {cached.quarter} ({json_path.name})")
            return cached

    content = fetch_exhibit_bytes(meta.url, timeout=120.0)
    if content is None:
        if verbose:
            print(f"  [DECK] fetch failed: {meta.url}")
        return None

    content_hash = hashlib.sha256(content).hexdigest()[:16]

    # Save raw PDF to disk for re-parsing later
    pdf_path = _pdf_cache_path(ticker, meta.accession, meta.filename)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(content)

    if verbose:
        print(f"  [DECK] parsing {meta.filename} ({len(content):,} bytes)...")
    pages = _parse_pdf(content, verbose=verbose)
    if not pages:
        if verbose:
            print(f"  [DECK] no pages extracted (scanned PDF or parse error)")
        return None

    quarter = infer_earnings_quarter(
        meta.filing_date or meta.report_date,
        fiscal_year_end_month=fiscal_year_end_month,
    )
    total_tables = sum(len(p.tables) for p in pages)
    total_chars = sum(len(p.text) for p in pages)

    deck = SlideDeck(
        ticker=ticker.upper(),
        quarter=quarter,
        report_date=meta.report_date,
        filing_date=meta.filing_date,
        accession=meta.accession,
        filing_type=meta.filing_type,
        exhibit_num=meta.exhibit_num,
        source_url=meta.url,
        source="edgar",
        deck_type="earnings",                 # EDGAR 8-K PDF exhibits are almost always earnings decks
        classification_confidence="explicit",  # exhibit number is deterministic
        title=f"{quarter} Earnings Presentation" if quarter else "Earnings Presentation",
        page_count=len(pages),
        pages=pages,
        total_tables=total_tables,
        total_chars=total_chars,
        fetched_at=datetime.utcnow().isoformat() + "Z",
        content_hash=content_hash,
        pdf_local_path=str(pdf_path),
    )
    _save_cache(deck, json_path)
    if verbose:
        print(f"  [DECK] {quarter or '?'} ({meta.report_date}): "
              f"{len(pages)} pages, {total_chars:,} chars, {total_tables} tables")
    return deck


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def fetch_slide_decks(
    ticker: str,
    *,
    quarters: int = 12,
    fiscal_year_end_month: int = 12,
    min_size_bytes: int = 200_000,
    force: bool = False,
    verbose: bool = False,
) -> list[SlideDeck]:
    """
    Fetch the last N investor presentation PDFs (8-K Ex 99.x) for the ticker.
    Returns a list in reverse chronological order (most recent first).

    Only PDFs larger than `min_size_bytes` are considered presentations.
    Smaller PDFs are usually press release supplementals or auditor
    consent letters — not decks.

    Not every 8-K has a slide deck; companies that DON'T file decks with
    earnings 8-Ks will return an empty list. Common: DE, CSCO, some HCs.
    """
    ticker = ticker.upper().strip()
    scan_budget = max(quarters + 8, 24)
    exhibits = scan_8k_exhibits(ticker, max_filings=scan_budget, verbose=verbose)

    # PDFs large enough to be a real presentation
    pdf_exhibits = [
        e for e in exhibits
        if e.content_type == "pdf" and e.size >= min_size_bytes
    ]
    if verbose:
        print(f"  [DECK] found {len(pdf_exhibits)} candidate PDF exhibits "
              f"(>= {min_size_bytes:,} bytes) of {len(exhibits)} total")

    # Dedupe by accession — some filings have multiple PDFs; keep the largest.
    by_accession: dict[str, ExhibitMeta] = {}
    for e in pdf_exhibits:
        key = e.accession
        if key not in by_accession or e.size > by_accession[key].size:
            by_accession[key] = e

    # Stable sort by report_date desc
    candidates = sorted(
        by_accession.values(),
        key=lambda e: e.report_date or e.filing_date,
        reverse=True,
    )

    results: list[SlideDeck] = []
    for meta in candidates:
        if len(results) >= quarters:
            break
        deck = _process_exhibit(ticker, meta, fiscal_year_end_month,
                                force=force, verbose=verbose)
        if deck is not None:
            results.append(deck)

    return results


# --------------------------------------------------------------------------
# Combined EDGAR + IR fetcher
# --------------------------------------------------------------------------

def fetch_all_slide_decks(
    ticker: str,
    *,
    quarters: int = 12,
    types: list[str] | None = None,
    include_ir: bool = True,
    fiscal_year_end_month: int = 12,
    min_size_bytes: int = 200_000,
    max_ir_decks: int = 20,
    force: bool = False,
    verbose: bool = False,
) -> list[SlideDeck]:
    """
    Fetch ALL slide decks (EDGAR + IR site) for a ticker.

    EDGAR is checked first (deterministic, cheap). IR site is then probed
    to fill gaps — earnings decks EDGAR didn't have, plus investor day,
    conference, and shareholder letter content that's IR-only.

    Args:
        quarters: max number of EDGAR earnings decks to return
        types: filter IR results to these types. EDGAR always returns
            earnings decks regardless (filter at caller level if needed).
            Valid types: earnings | investor_day | conference | shareholder_letter | other
        include_ir: set False to skip IR scraping entirely
        max_ir_decks: cap on IR-sourced decks
    """
    all_decks: list[SlideDeck] = []

    # EDGAR first
    edgar_decks = fetch_slide_decks(
        ticker,
        quarters=quarters,
        fiscal_year_end_month=fiscal_year_end_month,
        min_size_bytes=min_size_bytes,
        force=force,
        verbose=verbose,
    )
    all_decks.extend(edgar_decks)
    if verbose:
        print(f"  [ALL] EDGAR returned {len(edgar_decks)} earnings decks")

    if not include_ir:
        return all_decks

    # IR supplement — lazy import to avoid circular dependency
    try:
        from ingestion.loaders.ir_page_deck_loader import fetch_ir_slide_decks
    except Exception as e:
        if verbose:
            print(f"  [ALL] IR loader unavailable: {e}")
        return all_decks

    ir_decks = fetch_ir_slide_decks(
        ticker,
        types=types,
        max_decks=max_ir_decks,
        force=force,
        verbose=verbose,
    )
    if verbose:
        print(f"  [ALL] IR returned {len(ir_decks)} decks")

    # Dedupe: if an IR deck matches an EDGAR deck by filename or content hash, skip IR copy
    edgar_hashes = {d.content_hash for d in edgar_decks if d.content_hash}
    edgar_urls = {d.source_url.rsplit("/", 1)[-1].lower() for d in edgar_decks if d.source_url}
    for ir_deck in ir_decks:
        if ir_deck.content_hash and ir_deck.content_hash in edgar_hashes:
            if verbose:
                print(f"  [ALL] skip IR deck (same content as EDGAR): {ir_deck.title[:50]}")
            continue
        ir_filename = ir_deck.source_url.rsplit("/", 1)[-1].lower()
        if ir_filename and ir_filename in edgar_urls:
            continue
        all_decks.append(ir_deck)

    # Sort: earnings by quarter desc, then other types by date desc
    def sort_key(d: SlideDeck):
        # Earnings decks: sort by report_date desc (most recent first)
        # Other types: sort by report_date/date desc, with missing dates last
        return (d.report_date or d.filing_date or "0000-00-00",)
    all_decks.sort(key=sort_key, reverse=True)

    return all_decks


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m ingestion.loaders.slide_deck_loader",
        description="Fetch investor presentation PDFs from EDGAR 8-K exhibits.",
    )
    ap.add_argument("ticker")
    ap.add_argument("--quarters", type=int, default=12)
    ap.add_argument("--fy-end-month", type=int, default=12)
    ap.add_argument("--min-size", type=int, default=200_000,
                    help="Min PDF size in bytes to qualify as a presentation (default 200KB)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="Also fetch from IR site (investor day, conference, shareholder letters)")
    ap.add_argument("--types", type=str, default="",
                    help="With --all: filter IR results to these types (comma-separated)")
    ap.add_argument("--show-page", type=int, default=None,
                    help="Print the Nth page (1-indexed) of the most recent deck")
    args = ap.parse_args()

    if args.all:
        types = [t.strip() for t in args.types.split(",") if t.strip()] or None
        decks = fetch_all_slide_decks(
            args.ticker,
            quarters=args.quarters,
            types=types,
            include_ir=True,
            fiscal_year_end_month=args.fy_end_month,
            min_size_bytes=args.min_size,
            force=args.force,
            verbose=args.verbose,
        )
    else:
        decks = fetch_slide_decks(
            args.ticker,
            quarters=args.quarters,
            fiscal_year_end_month=args.fy_end_month,
            min_size_bytes=args.min_size,
            force=args.force,
            verbose=args.verbose,
        )
    if not decks:
        print(f"No slide decks found for {args.ticker}")
        print("  (not every company files decks with earnings 8-Ks)")
        return 2

    print()
    print(f"=== {args.ticker}: {len(decks)} slide decks ===")
    print()
    for d in decks:
        print(f"  [{d.source:<8}] {d.deck_type:<20} "
              f"{d.quarter or d.report_date or '-':<12}  "
              f"{d.page_count:>3}p {d.total_tables:>2}t "
              f"{d.total_chars:>6,}ch  "
              f"{d.title[:50] or d.source_url.split('/')[-1][:50]}")

    if args.show_page is not None and decks:
        d = decks[0]
        pg_idx = args.show_page - 1
        if 0 <= pg_idx < len(d.pages):
            p = d.pages[pg_idx]
            print()
            print(f"--- Slide {p.page_number} ({d.quarter}) ---")
            print(p.text[:2000])
            for t in p.tables:
                print()
                print(t.markdown)
        else:
            print(f"\nNo page #{args.show_page} — deck has {len(d.pages)} pages")

    return 0


if __name__ == "__main__":
    sys.exit(_main())
