"""
Unified Ingestion Orchestrator

Single entry point: fetch_all_sources(ticker) → IngestionReport

Coordinates all free ingestion loaders:
  1. Earnings call transcripts (Motley Fool, EDGAR exhibits, FMP)
  2. Press releases (EDGAR Exhibit 99.1)
  3. Investor presentations (EDGAR exhibits 99.2+, IR pages)
  4. Quarterly supplements (EDGAR exhibits, IR pages)
  5. SEC filings (10-K, 10-Q, 8-K text)
  6. Structured financials (yfinance, Alpha Vantage, Polygon)

Reports what was found, what failed, and what needs paid sources.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from ingestion.loaders.transcript_loader import (
    fetch_transcript, TranscriptFetchReport,
)
from ingestion.loaders.presentation_loader import (
    fetch_presentations, PresentationFetchReport,
)
from research.edgar_text_fetcher import fetch_best_filing_text


@dataclass
class SourceCoverage:
    """Coverage assessment for a single source type."""
    source_type: str
    status: str         # "covered", "partial", "gap", "needs_paid"
    free_source: str    # what free source we used
    quality: str        # "high", "medium", "low", "none"
    detail: str         # specifics
    paid_alternatives: list[str] = field(default_factory=list)


@dataclass
class IngestionReport:
    """Complete ingestion report for a ticker."""
    ticker: str
    timestamp: str
    
    # Sub-reports
    transcript_report: Optional[TranscriptFetchReport] = None
    presentation_report: Optional[PresentationFetchReport] = None
    filing_text: Optional[str] = None
    filing_type: Optional[str] = None
    
    # Coverage summary
    coverage: list[SourceCoverage] = field(default_factory=list)
    
    # Gaps that need paid APIs
    paid_gaps: list[dict] = field(default_factory=list)
    
    def summary(self) -> str:
        lines = [
            f"\n{'='*70}",
            f"  INGESTION REPORT: {self.ticker}",
            f"  {self.timestamp}",
            f"{'='*70}",
            "",
            "  SOURCE COVERAGE:",
            f"  {'Source':<30s} {'Status':<12s} {'Quality':<10s} {'Detail'}",
            f"  {'-'*70}",
        ]
        for c in self.coverage:
            lines.append(f"  {c.source_type:<30s} {c.status:<12s} {c.quality:<10s} {c.detail}")
        
        if self.paid_gaps:
            lines.append("")
            lines.append("  GAPS REQUIRING PAID SOURCES:")
            for g in self.paid_gaps:
                lines.append(f"  ⚠ {g['source_type']}: {g['detail']}")
                lines.append(f"    Paid options: {', '.join(g['alternatives'])}")
        
        lines.append("")
        covered = sum(1 for c in self.coverage if c.status in ("covered", "partial"))
        total = len(self.coverage)
        lines.append(f"  Overall: {covered}/{total} sources covered")
        
        return "\n".join(lines)


def fetch_all_sources(
    ticker: str,
    fmp_api_key: str = None,
    alpha_vantage_key: str = None,
    verbose: bool = False,
) -> IngestionReport:
    """
    Fetch everything available from free sources for a ticker.
    Returns comprehensive report with coverage assessment.
    """
    ticker = ticker.upper()
    report = IngestionReport(
        ticker=ticker,
        timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    )
    
    # ── 1. Earnings call transcript ──────────────────────────
    if verbose:
        print(f"\n[1/4] Fetching transcripts for {ticker}...")
    
    report.transcript_report = fetch_transcript(ticker, fmp_api_key=fmp_api_key, verbose=verbose)
    tr = report.transcript_report
    
    if tr.best_result and tr.best_result.is_complete:
        report.coverage.append(SourceCoverage(
            source_type="Earnings Call Transcript",
            status="covered",
            free_source=tr.best_result.source,
            quality="high",
            detail=f"{tr.best_result.word_count} words, prepared+Q&A",
        ))
    elif tr.best_result:
        report.coverage.append(SourceCoverage(
            source_type="Earnings Call Transcript",
            status="partial",
            free_source=tr.best_result.source,
            quality="medium",
            detail=f"{tr.best_result.word_count} words ({tr.best_result.quality_notes})",
            paid_alternatives=["Seeking Alpha Premium", "AlphaSense", "Sentieo", "Refinitiv"],
        ))
        report.paid_gaps.append({
            "source_type": "Full Earnings Call Transcript (with Q&A)",
            "detail": f"Only got {tr.best_result.source} ({tr.best_result.quality_notes}). Full Q&A is where analysts press mgmt on guidance.",
            "alternatives": ["Financial Modeling Prep (free key)", "Seeking Alpha Premium ($20/mo)", "AlphaSense"],
        })
    else:
        report.coverage.append(SourceCoverage(
            source_type="Earnings Call Transcript",
            status="gap",
            free_source="none",
            quality="none",
            detail="No transcript found from any free source",
            paid_alternatives=["FMP (free key)", "Seeking Alpha Premium", "AlphaSense"],
        ))
        report.paid_gaps.append({
            "source_type": "Earnings Call Transcript",
            "detail": "No free transcript available. This is the richest qualitative source — mgmt tone, Q&A pressure, guidance language.",
            "alternatives": ["Financial Modeling Prep (free signup)", "Seeking Alpha Premium ($20/mo)", "AlphaSense ($$$)"],
        })
    
    # ── 2. Press release (Exhibit 99.1) ──────────────────────
    if verbose:
        print(f"\n[2/4] Fetching filing text for {ticker}...")
    
    filing_text, filing_type = fetch_best_filing_text(ticker)
    report.filing_text = filing_text
    report.filing_type = filing_type
    
    if filing_text and filing_type == "press_release":
        report.coverage.append(SourceCoverage(
            source_type="Earnings Press Release (Ex 99.1)",
            status="covered",
            free_source="EDGAR",
            quality="high",
            detail=f"{len(filing_text.split())} words from Exhibit 99.1",
        ))
    elif filing_text:
        report.coverage.append(SourceCoverage(
            source_type="Earnings Press Release (Ex 99.1)",
            status="partial",
            free_source="EDGAR",
            quality="medium",
            detail=f"Got {filing_type} instead ({len(filing_text.split())} words)",
        ))
    else:
        report.coverage.append(SourceCoverage(
            source_type="Earnings Press Release (Ex 99.1)",
            status="gap",
            free_source="none",
            quality="none",
            detail="Could not fetch from EDGAR",
        ))
    
    # ── 3. Investor presentations & supplements ──────────────
    if verbose:
        print(f"\n[3/4] Scanning for presentations/supplements for {ticker}...")
    
    report.presentation_report = fetch_presentations(ticker, verbose=verbose)
    pr = report.presentation_report
    
    if pr.presentations:
        report.coverage.append(SourceCoverage(
            source_type="Investor Presentations",
            status="covered",
            free_source="EDGAR_EXHIBIT",
            quality="medium" if any(p.content_type == "pdf" for p in pr.presentations) else "low",
            detail=f"{len(pr.presentations)} found (need PDF parsing for full value)",
        ))
    else:
        report.coverage.append(SourceCoverage(
            source_type="Investor Presentations",
            status="gap",
            free_source="none",
            quality="none",
            detail="No presentations found in EDGAR exhibits",
            paid_alternatives=["AlphaSense", "S&P Capital IQ", "Company IR page (manual)"],
        ))
        report.paid_gaps.append({
            "source_type": "Investor Presentations",
            "detail": "Not all companies file presentations as EDGAR exhibits. Investor day decks, strategic updates often only on IR pages.",
            "alternatives": ["AlphaSense (searchable presentation library)", "Capital IQ", "Manual IR page download"],
        })
    
    if pr.supplements:
        report.coverage.append(SourceCoverage(
            source_type="Quarterly Supplements",
            status="covered",
            free_source="EDGAR_EXHIBIT",
            quality="medium",
            detail=f"{len(pr.supplements)} found",
        ))
    else:
        report.coverage.append(SourceCoverage(
            source_type="Quarterly Supplements",
            status="gap",
            free_source="none",
            quality="none",
            detail="No supplements found in EDGAR exhibits (many companies only post to IR page)",
            paid_alternatives=["Company IR page (manual)", "S&P Capital IQ"],
        ))
    
    # ── 4. 10-K/10-Q MD&A ───────────────────────────────────
    if verbose:
        print(f"\n[4/4] Checking 10-K/10-Q coverage for {ticker}...")
    
    # Already have this from edgar_text_fetcher if press release wasn't available
    # Check if we need to separately fetch the annual filing
    if filing_type in ("press_release", "8-K"):
        # Also grab 10-K for comprehensive coverage
        from research.edgar_text_fetcher import fetch_filing_text
        annual_text = fetch_filing_text(ticker, "10-K")
        if annual_text:
            report.coverage.append(SourceCoverage(
                source_type="10-K Annual Filing (MD&A)",
                status="covered",
                free_source="EDGAR",
                quality="high",
                detail=f"{len(annual_text.split())} words from MD&A",
            ))
        else:
            report.coverage.append(SourceCoverage(
                source_type="10-K Annual Filing (MD&A)",
                status="gap",
                free_source="EDGAR",
                quality="none",
                detail="Could not fetch 10-K",
            ))
    else:
        report.coverage.append(SourceCoverage(
            source_type="10-K Annual Filing (MD&A)",
            status="covered" if filing_text else "gap",
            free_source="EDGAR",
            quality="high" if filing_text else "none",
            detail=f"Using {filing_type}" if filing_text else "Not available",
        ))
    
    return report


# ── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "CMG"
    
    # Try loading FMP key
    fmp_key = None
    try:
        import json
        keys = json.loads(open("/skills/user/data-apis/keys.json").read())
        fmp_key = keys.get("fmp_api_key")
    except Exception:
        pass
    
    report = fetch_all_sources(ticker, fmp_api_key=fmp_key, verbose=True)
    print(report.summary())
