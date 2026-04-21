"""
Investor Presentation & Quarterly Supplement Loader

Fetches investor presentations and supplemental data from free sources.

Sources:
  1. SEC EDGAR 8-K exhibits (Ex 99.2+) — presentations filed with earnings
  2. SEC EDGAR DEF 14A / S-1 / investor day filings
  3. Company IR page scraping (best effort, not standardized)

Architecture notes:
  - Presentations are often PDFs. This loader identifies and downloads them.
  - Actual PDF parsing is handled downstream by the extraction pipeline
    (Claude vision or pdfplumber).
  - Returns PresentationResult with metadata + raw content or file path.

Paid alternatives (not implemented):
  - AlphaSense (best for investor day transcripts + presentations)
  - Tegus / Sentieo (presentation search)
  - Bloomberg DOCS function
"""

import re
import httpx
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from pathlib import Path


SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


@dataclass
class ExhibitInfo:
    """Metadata for a single EDGAR exhibit."""
    exhibit_num: str      # "99.1", "99.2", etc.
    filename: str
    url: str
    size: int
    content_type: str     # "html", "pdf", "xlsx", "unknown"
    filing_type: str      # "8-K", "DEF 14A", etc.
    filing_date: str
    description: str = ""


@dataclass
class PresentationResult:
    """Standardized presentation/supplement output."""
    ticker: str
    title: str
    date: str
    source: str            # "EDGAR_EXHIBIT", "IR_PAGE"
    source_url: str
    content_type: str      # "html", "pdf"
    
    # Content (one of these will be populated)
    text_content: str = ""       # for HTML exhibits
    pdf_path: str = ""           # for downloaded PDFs
    pdf_bytes: bytes = b""       # raw PDF content
    
    # Metadata
    exhibit_num: str = ""
    filing_type: str = ""
    word_count: int = 0
    fetch_timestamp: str = ""
    quality_notes: str = ""


@dataclass
class PresentationFetchReport:
    ticker: str
    exhibits_found: list[ExhibitInfo] = field(default_factory=list)
    presentations: list[PresentationResult] = field(default_factory=list)
    supplements: list[PresentationResult] = field(default_factory=list)
    sources_tried: list[dict] = field(default_factory=list)


# ── CIK resolution ──────────────────────────────────────────

_cik_cache = {}

def _resolve_cik(ticker: str) -> str | None:
    ticker = ticker.upper()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        resp = httpx.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_HEADERS, timeout=15.0)
        if resp.status_code == 200:
            for entry in resp.json().values():
                if entry.get("ticker", "").upper() == ticker:
                    cik = str(entry["cik_str"]).zfill(10)
                    _cik_cache[ticker] = cik
                    return cik
    except Exception:
        pass
    return None


# ── EDGAR Multi-Exhibit Scanner ──────────────────────────────

def _scan_edgar_exhibits(
    ticker: str, 
    form_types: list[str] = None,
    max_filings: int = 10,
    verbose: bool = False,
) -> list[ExhibitInfo]:
    """
    Scan recent EDGAR filings for ALL exhibits (not just 99.1).
    Returns metadata for each exhibit found.
    """
    if form_types is None:
        form_types = ["8-K", "8-K/A"]
    
    cik = _resolve_cik(ticker)
    if not cik:
        return []
    
    cik_stripped = cik.lstrip("0")
    exhibits = []
    
    try:
        resp = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=SEC_HEADERS, timeout=15.0)
        if resp.status_code != 200:
            return []
        
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        
        filing_count = 0
        for i, form in enumerate(forms):
            if form not in form_types:
                continue
            if filing_count >= max_filings:
                break
            
            acc = accessions[i].replace("-", "")
            date = dates[i] if i < len(dates) else ""
            filing_count += 1
            
            # Get filing index
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/index.json"
            try:
                idx_resp = httpx.get(idx_url, headers=SEC_HEADERS, timeout=15.0)
                if idx_resp.status_code != 200:
                    continue
                
                items = idx_resp.json().get("directory", {}).get("item", [])
                
                for item in items:
                    name = item.get("name", "")
                    name_lower = name.lower()
                    
                    # Identify exhibits
                    if not any(x in name_lower for x in ["ex99", "ex-99", "exhibit99", "exhibit-99"]):
                        continue
                    
                    # Determine exhibit number
                    exhibit_num = "99.1"
                    m = re.search(r'99[._-]?(\d)', name_lower)
                    if m:
                        exhibit_num = f"99.{m.group(1)}"
                    
                    # Determine content type
                    if name_lower.endswith(".pdf"):
                        content_type = "pdf"
                    elif name_lower.endswith((".htm", ".html")):
                        content_type = "html"
                    elif name_lower.endswith((".xlsx", ".xls")):
                        content_type = "xlsx"
                    else:
                        content_type = "unknown"
                    
                    ex_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/{name}"
                    
                    exhibits.append(ExhibitInfo(
                        exhibit_num=exhibit_num,
                        filename=name,
                        url=ex_url,
                        size=int(item.get("size", 0)),
                        content_type=content_type,
                        filing_type=form,
                        filing_date=date,
                    ))
                    
                    if verbose:
                        print(f"  [EDGAR] {date} {form} Ex {exhibit_num}: {name} ({content_type}, {item.get('size', '?')} bytes)")
            
            except Exception:
                continue
    
    except Exception as e:
        if verbose:
            print(f"  [EDGAR] Error scanning: {e}")
    
    return exhibits


def _classify_exhibit(exhibit: ExhibitInfo, text_preview: str = "") -> str:
    """
    Classify an exhibit as: press_release, presentation, supplement, financial_table, other.
    Uses exhibit number heuristics + content analysis.
    """
    # Ex 99.1 is almost always the press release
    if exhibit.exhibit_num == "99.1":
        return "press_release"
    
    # PDFs are usually presentations or supplements
    if exhibit.content_type == "pdf":
        if exhibit.size > 500_000:  # >500KB = likely a presentation
            return "presentation"
        return "supplement"
    
    # Excel files are usually supplemental data
    if exhibit.content_type == "xlsx":
        return "supplement"
    
    # For HTML, check content
    if text_preview:
        lower = text_preview.lower()[:2000]
        if any(kw in lower for kw in ["slide", "presentation", "investor day", "strategic"]):
            return "presentation"
        if any(kw in lower for kw in ["supplemental", "operating statistics", "portfolio"]):
            return "supplement"
        if any(kw in lower for kw in ["revenue", "earnings per", "net income", "results"]):
            return "press_release"
    
    # Ex 99.2+ default to supplement
    return "supplement"


def _fetch_exhibit_content(exhibit: ExhibitInfo, download_dir: str = "/tmp") -> PresentationResult:
    """Fetch and return exhibit content."""
    result = PresentationResult(
        ticker="",  # filled by caller
        title=f"Exhibit {exhibit.exhibit_num} - {exhibit.filing_date}",
        date=exhibit.filing_date,
        source="EDGAR_EXHIBIT",
        source_url=exhibit.url,
        content_type=exhibit.content_type,
        exhibit_num=exhibit.exhibit_num,
        filing_type=exhibit.filing_type,
        fetch_timestamp=datetime.utcnow().isoformat() + "Z",
    )
    
    try:
        resp = httpx.get(exhibit.url, headers=SEC_HEADERS, timeout=60.0, follow_redirects=True)
        if resp.status_code != 200:
            result.quality_notes = f"HTTP {resp.status_code}"
            return result
        
        if exhibit.content_type == "pdf":
            # Save PDF for downstream parsing
            pdf_path = f"{download_dir}/{exhibit.filename}"
            Path(pdf_path).write_bytes(resp.content)
            result.pdf_path = pdf_path
            result.pdf_bytes = resp.content
            result.quality_notes = f"PDF downloaded ({len(resp.content)} bytes)"
        
        elif exhibit.content_type in ("html", "unknown"):
            text = _clean_html(resp.text)
            result.text_content = text[:30000]
            result.word_count = len(text.split())
            result.quality_notes = f"HTML extracted ({result.word_count} words)"
        
        elif exhibit.content_type == "xlsx":
            pdf_path = f"{download_dir}/{exhibit.filename}"
            Path(pdf_path).write_bytes(resp.content)
            result.pdf_path = pdf_path  # reuse field for file path
            result.quality_notes = f"Excel downloaded ({len(resp.content)} bytes)"
    
    except Exception as e:
        result.quality_notes = f"Fetch error: {e}"
    
    return result


# ── Orchestrator ─────────────────────────────────────────────

def fetch_presentations(
    ticker: str,
    max_filings: int = 10,
    download_pdfs: bool = True,
    download_dir: str = "/tmp",
    verbose: bool = False,
) -> PresentationFetchReport:
    """
    Fetch all investor presentations and quarterly supplements for a ticker.
    Scans EDGAR 8-K exhibits, classifies them, and fetches content.
    """
    ticker = ticker.upper()
    report = PresentationFetchReport(ticker=ticker)
    
    # Scan EDGAR exhibits
    exhibits = _scan_edgar_exhibits(ticker, max_filings=max_filings, verbose=verbose)
    report.exhibits_found = exhibits
    report.sources_tried.append({
        "source": "EDGAR_8K_EXHIBITS",
        "status": "success" if exhibits else "none_found",
        "detail": f"{len(exhibits)} exhibits across {max_filings} filings scanned",
    })
    
    # Classify and fetch non-press-release exhibits
    for exhibit in exhibits:
        # Skip 99.1 (press release — handled by transcript_loader)
        if exhibit.exhibit_num == "99.1":
            continue
        
        # Fetch content
        result = _fetch_exhibit_content(exhibit, download_dir=download_dir)
        result.ticker = ticker
        
        classification = _classify_exhibit(exhibit, result.text_content[:2000])
        
        if classification == "presentation":
            result.title = f"Investor Presentation - {exhibit.filing_date}"
            report.presentations.append(result)
        elif classification == "supplement":
            result.title = f"Quarterly Supplement - {exhibit.filing_date}"
            report.supplements.append(result)
        
        if verbose:
            print(f"  [CLASSIFY] Ex {exhibit.exhibit_num} → {classification} ({result.quality_notes})")
    
    return report


# ── IR Page Scraper (best effort) ────────────────────────────

def _scrape_ir_page(ticker: str, verbose=False) -> list[dict]:
    """
    Attempt to find and scrape the company's IR page for presentations.
    
    This is inherently fragile — every company's IR page is different.
    Returns a list of {title, url, type, date} dicts for found documents.
    
    NOTE: This is a best-effort heuristic. For reliable coverage,
    paid services like AlphaSense or S&P Capital IQ are recommended.
    """
    found = []
    
    # Common IR URL patterns
    ir_patterns = [
        f"https://ir.{_guess_domain(ticker)}/",
        f"https://investor.{_guess_domain(ticker)}/",
        f"https://investors.{_guess_domain(ticker)}/",
        f"https://www.{_guess_domain(ticker)}/investors",
        f"https://www.{_guess_domain(ticker)}/investor-relations",
    ]
    
    for url in ir_patterns:
        try:
            resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=10.0, follow_redirects=True)
            if resp.status_code != 200:
                continue
            
            if verbose:
                print(f"  [IR] Found IR page: {url}")
            
            # Look for links to presentations/supplements
            pdf_links = re.findall(
                r'href="([^"]*(?:presentation|supplement|investor|quarterly|earnings)[^"]*\.pdf)"',
                resp.text, re.IGNORECASE
            )
            
            for link in pdf_links[:5]:
                if not link.startswith("http"):
                    link = resp.url.join(link).__str__() if hasattr(resp.url, 'join') else url + link
                found.append({
                    "title": Path(link).stem.replace("-", " ").replace("_", " "),
                    "url": link,
                    "type": "presentation" if "presentation" in link.lower() else "supplement",
                    "date": "",
                })
            
            if found:
                break
                
        except Exception:
            continue
    
    return found


def _guess_domain(ticker: str) -> str:
    """Best-effort domain guess. Very imprecise."""
    known = {
        "CMG": "chipotle.com", "AAPL": "apple.com", "MSFT": "microsoft.com",
        "NVDA": "nvidia.com", "DPZ": "dominos.com", "WING": "wingstop.com",
        "TXRH": "texasroadhouse.com",
    }
    return known.get(ticker.upper(), f"{ticker.lower()}.com")


def _clean_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ── CLI test ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "CMG"
    report = fetch_presentations(ticker, verbose=True)
    print(f"\n{'='*60}")
    print(f"Presentation Fetch Report: {ticker}")
    print(f"{'='*60}")
    print(f"Total exhibits found: {len(report.exhibits_found)}")
    print(f"Presentations: {len(report.presentations)}")
    print(f"Supplements: {len(report.supplements)}")
    for p in report.presentations:
        print(f"  PRES: {p.title} | {p.content_type} | {p.quality_notes}")
    for s in report.supplements:
        print(f"  SUPP: {s.title} | {s.content_type} | {s.quality_notes}")
