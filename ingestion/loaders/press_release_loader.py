"""
Press Release Loader.

Fetches quarterly earnings press releases (8-K Exhibit 99.1) from EDGAR,
parses HTML into clean text AND preserves tables as structured markdown.
Tables matter because guidance ranges, segment breakdowns, and the
reconciliation numbers the transcript analyzer can't see live there.

Public API:
    fetch_press_releases(ticker, quarters=12, fiscal_year_end_month=12,
                         force=False, verbose=False) -> list[PressRelease]

CLI:
    python -m ingestion.loaders.press_release_loader TICKER [--force]
                                                          [--quarters N]
                                                          [--fy-end-month M]
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ingestion.loaders._edgar_utils import (
    ExhibitMeta, fetch_exhibit_bytes, infer_earnings_quarter, scan_8k_exhibits,
)


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class PressReleaseTable:
    """A single HTML table extracted from a press release."""
    index: int                          # position in the document (0-indexed)
    heading_context: str = ""           # nearest preceding <h*> or <strong>
    columns: list = field(default_factory=list)        # list[str]
    rows: list = field(default_factory=list)            # list[list[str]]
    markdown: str = ""                   # pipe-table rendering


@dataclass
class PressRelease:
    """One earnings press release, quarter-mapped + structured."""
    ticker: str = ""
    quarter: str = ""                    # "Q3 2026"
    report_date: str = ""                # YYYY-MM-DD
    filing_date: str = ""                # YYYY-MM-DD
    accession: str = ""
    filing_type: str = ""
    exhibit_num: str = ""
    source_url: str = ""
    text: str = ""                        # cleaned prose, tables stripped
    tables: list = field(default_factory=list)   # list[PressReleaseTable]
    full_text_with_tables: str = ""       # text + inline markdown tables
    fetched_at: str = ""
    content_hash: str = ""                # sha256 of raw HTML (for cache)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_prompt_text(self, max_chars: int = 20000) -> str:
        """Human-readable rendering for injection into Claude prompts."""
        header = f"=== {self.ticker} {self.quarter} Press Release ({self.report_date}) ===\n"
        body = self.full_text_with_tables or self.text
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n...[truncated at {max_chars} chars]"
        return header + body


# --------------------------------------------------------------------------
# HTML → clean text + tables
# --------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


def _cell_text(cell: Tag) -> str:
    """Extract text from a <td> or <th>, normalizing whitespace."""
    txt = cell.get_text(separator=" ", strip=True)
    txt = _WHITESPACE_RE.sub(" ", txt).strip()
    return txt


def _table_to_structured(table: Tag, idx: int, heading_context: str = "") -> PressReleaseTable:
    """Convert a BeautifulSoup <table> into a PressReleaseTable."""
    rows_raw: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rows_raw.append([_cell_text(c) for c in cells])

    # Strip empty leading/trailing rows
    rows_raw = [r for r in rows_raw if any(c.strip() for c in r)]

    columns: list[str] = []
    data_rows: list[list[str]] = rows_raw

    if rows_raw:
        # Heuristic: first row is header if it's short and doesn't contain
        # numeric-heavy cells. Otherwise treat as data and synthesize columns.
        first = rows_raw[0]
        first_numeric = sum(1 for c in first if re.search(r"\d", c))
        if first_numeric < len(first) / 2:
            columns = first
            data_rows = rows_raw[1:]
        else:
            columns = [f"col{i+1}" for i in range(len(first))]

    # Build markdown (pipe table). Pad rows to column width.
    n_cols = max(len(columns), max((len(r) for r in data_rows), default=0))
    columns = (columns + [""] * n_cols)[:n_cols] if columns else [f"col{i+1}" for i in range(n_cols)]

    def pad(row, width):
        return row + [""] * (width - len(row))

    md_lines = []
    if n_cols > 0:
        md_lines.append("| " + " | ".join(columns) + " |")
        md_lines.append("|" + "|".join("---" for _ in range(n_cols)) + "|")
        for r in data_rows:
            md_lines.append("| " + " | ".join(pad(r, n_cols)) + " |")

    return PressReleaseTable(
        index=idx,
        heading_context=heading_context[:200],
        columns=columns,
        rows=data_rows,
        markdown="\n".join(md_lines),
    )


def _walk_html(soup: BeautifulSoup) -> tuple[str, list[PressReleaseTable]]:
    """
    Walk the soup in document order, producing:
      - `text`: prose only, tables stripped (for the text field)
      - `tables`: structured extractions
      - `full`: prose with tables inlined as markdown (for prompt text)
    """
    tables: list[PressReleaseTable] = []
    text_parts: list[str] = []          # prose only
    full_parts: list[str] = []          # prose + inlined tables
    current_heading = ""
    table_idx = 0

    # The body may or may not have been explicitly marked; fall back to root.
    root = soup.body or soup

    # We traverse top-level block elements to control paragraphs and tables.
    for el in root.descendants:
        if isinstance(el, Tag):
            tag = el.name.lower()
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                current_heading = el.get_text(separator=" ", strip=True)
            elif tag == "strong" and not current_heading:
                # Strong in absence of formal heading often serves as section label
                strong_txt = el.get_text(separator=" ", strip=True)
                if len(strong_txt) < 120:
                    current_heading = strong_txt
            elif tag == "table":
                # Extract and mark visited so descendants loop doesn't re-process cells as text
                t = _table_to_structured(el, table_idx, current_heading)
                tables.append(t)
                full_parts.append(f"\n\n[TABLE {table_idx}: {t.heading_context}]\n{t.markdown}\n")
                table_idx += 1
                # Mark the table and its children as processed
                el["data-pr-processed"] = "1"

    # Now get the plain text with tables stripped.
    # Replace every processed <table> with a placeholder, then extract text.
    stripped = BeautifulSoup(str(soup), "html.parser")
    for t in stripped.find_all("table"):
        t.replace_with(f"\n[TABLE_OMITTED]\n")
    prose_text = stripped.get_text(separator="\n", strip=False)
    prose_text = htmllib.unescape(prose_text)
    prose_text = _WHITESPACE_RE.sub(" ", prose_text)
    prose_text = _NEWLINES_RE.sub("\n\n", prose_text).strip()

    # full = prose with tables inlined (approximate — insert table markdown at
    # table-omitted placeholders by order).
    full_parts_iter = iter(tables)
    def _replace_placeholder(match):
        try:
            t = next(full_parts_iter)
            return f"\n\n{t.markdown}\n\n"
        except StopIteration:
            return ""
    full_text = re.sub(r"\[TABLE_OMITTED\]", _replace_placeholder, prose_text)
    full_text = _NEWLINES_RE.sub("\n\n", full_text).strip()

    return prose_text, tables, full_text


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

CACHE_DIR = Path("data/press_releases")


def _cache_path(ticker: str, accession: str) -> Path:
    safe_acc = accession.replace("/", "_")
    return CACHE_DIR / f"{ticker.upper()}_{safe_acc}.json"


def _load_cached(path: Path) -> PressRelease | None:
    try:
        with open(path) as f:
            data = json.load(f)
        fields = {k: v for k, v in data.items() if k in PressRelease.__dataclass_fields__}
        tables = [PressReleaseTable(**t) for t in data.get("tables", [])]
        fields["tables"] = tables
        return PressRelease(**fields)
    except Exception:
        return None


def _save_cache(pr: PressRelease, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pr.to_dict(), f, indent=2, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------
# Per-release processing
# --------------------------------------------------------------------------

def _process_exhibit(
    ticker: str, meta: ExhibitMeta, fiscal_year_end_month: int,
    *, force: bool = False, verbose: bool = False,
) -> PressRelease | None:
    """Fetch + parse + cache a single Exhibit 99.1."""
    cache_path = _cache_path(ticker, meta.accession)
    if cache_path.exists() and not force:
        cached = _load_cached(cache_path)
        if cached is not None:
            if verbose:
                print(f"  [PR] CACHE HIT {cached.quarter} ({cache_path.name})")
            return cached

    content = fetch_exhibit_bytes(meta.url)
    if content is None:
        if verbose:
            print(f"  [PR] fetch failed: {meta.url}")
        return None

    try:
        raw_html = content.decode("utf-8", errors="replace")
    except Exception:
        if verbose:
            print(f"  [PR] decode failed: {meta.url}")
        return None

    content_hash = hashlib.sha256(content).hexdigest()[:16]

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
    except Exception as e:
        if verbose:
            print(f"  [PR] HTML parse failed: {e}")
        return None

    text, tables, full = _walk_html(soup)

    # Use filing_date (not report_date) because for 8-Ks, EDGAR's reportDate
    # usually equals filing_date (same-day event). Then shift back to land
    # in the reported quarter.
    quarter = infer_earnings_quarter(
        meta.filing_date or meta.report_date,
        fiscal_year_end_month=fiscal_year_end_month,
    )

    pr = PressRelease(
        ticker=ticker.upper(),
        quarter=quarter,
        report_date=meta.report_date,
        filing_date=meta.filing_date,
        accession=meta.accession,
        filing_type=meta.filing_type,
        exhibit_num=meta.exhibit_num,
        source_url=meta.url,
        text=text,
        tables=tables,
        full_text_with_tables=full,
        fetched_at=datetime.utcnow().isoformat() + "Z",
        content_hash=content_hash,
    )
    _save_cache(pr, cache_path)
    if verbose:
        print(f"  [PR] fetched {quarter or '?'} ({meta.report_date}): "
              f"{len(text):,} chars, {len(tables)} tables")
    return pr


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def fetch_press_releases(
    ticker: str,
    *,
    quarters: int = 12,
    fiscal_year_end_month: int = 12,
    force: bool = False,
    verbose: bool = False,
) -> list[PressRelease]:
    """
    Fetch the last N quarterly earnings press releases (8-K Ex 99.1) for
    the ticker. Returns a list in REVERSE chronological order (most recent
    first), with each release quarter-mapped and structured.

    Args:
        ticker: stock ticker
        quarters: max number of quarterly press releases to return (default 12).
            We scan ~quarters+4 8-Ks to account for non-earnings 8-Ks that
            don't have Ex 99.1 press releases.
        fiscal_year_end_month: calendar month of fiscal year end (default 12).
            Override for non-December fiscal years (e.g., TGT=1, CSCO=7).
        force: bypass cache and refetch everything
        verbose: print progress
    """
    ticker = ticker.upper().strip()
    # Scan more filings than we need — many 8-Ks aren't earnings releases
    scan_budget = max(quarters + 6, 20)
    exhibits = scan_8k_exhibits(ticker, max_filings=scan_budget, verbose=verbose)

    # Filter to Ex 99.1 only and HTML content (occasionally PDFs — skip here)
    ex_99_1 = [e for e in exhibits if e.exhibit_num == "99.1" and e.content_type in ("html", "unknown")]
    if verbose:
        print(f"  [PR] found {len(ex_99_1)} Ex 99.1 HTML exhibits (of {len(exhibits)} total)")

    results: list[PressRelease] = []
    for meta in ex_99_1:
        if len(results) >= quarters:
            break
        pr = _process_exhibit(ticker, meta, fiscal_year_end_month,
                              force=force, verbose=verbose)
        if pr is not None:
            results.append(pr)

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m ingestion.loaders.press_release_loader",
        description="Fetch quarterly earnings press releases from EDGAR.",
    )
    ap.add_argument("ticker", help="Stock ticker (e.g., CMG)")
    ap.add_argument("--quarters", type=int, default=12)
    ap.add_argument("--fy-end-month", type=int, default=12,
                    help="Fiscal year end month (default 12 = Dec)")
    ap.add_argument("--force", action="store_true", help="Bypass cache")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--show-table", type=int, default=None,
                    help="Print the Nth table from the most recent PR")
    args = ap.parse_args()

    releases = fetch_press_releases(
        args.ticker,
        quarters=args.quarters,
        fiscal_year_end_month=args.fy_end_month,
        force=args.force,
        verbose=args.verbose,
    )
    if not releases:
        print(f"No press releases found for {args.ticker}")
        return 2

    print()
    print(f"=== {args.ticker}: {len(releases)} press releases ===")
    print()
    for pr in releases:
        print(f"  {pr.quarter or '?':<10} report {pr.report_date or '?':<12} "
              f"acc {pr.accession:<25}  "
              f"{len(pr.text):>7,} chars, {len(pr.tables):>2} tables  "
              f"({pr.source_url.split('/')[-1]})")

    if args.show_table is not None and releases:
        pr = releases[0]
        if 0 <= args.show_table < len(pr.tables):
            t = pr.tables[args.show_table]
            print()
            print(f"--- Table {t.index} ({pr.quarter}): {t.heading_context} ---")
            print(t.markdown)
        else:
            print(f"\nNo table #{args.show_table} — most recent PR has {len(pr.tables)} tables")

    return 0


if __name__ == "__main__":
    sys.exit(_main())
