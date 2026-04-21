"""
Multi-Quarter Transcript Batch Fetcher

Fetches up to 12 quarters of earnings call transcripts from Motley Fool.

CRITICAL: MF has aggressive rate limiting (~20-30 requests before 429 block).
The block lasts 5-15 minutes on the IP level.

Strategy:
  1. First pass: construct all candidate URLs without fetching (using EDGAR dates)
  2. Fetch with 5-second delays between requests
  3. If 429 hit: stop, save progress, resume later
  4. Cache results to avoid re-fetching

Usage:
  from ingestion.loaders.transcript_batch import fetch_quarterly_transcripts
  results = fetch_quarterly_transcripts("CMG", quarters=12, delay=5.0)
  
  # Resume after rate limit (reads cache):
  results = fetch_quarterly_transcripts("CMG", quarters=12, resume=True)
"""

import re
import json
import httpx
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


SEC_HEADERS = {"User-Agent": "InvestmentWorkbench research@example.com"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.google.com/",
}

COMPANY_SLUGS = {
    "CMG": "chipotle", "DPZ": "dominos", "WING": "wingstop",
    "TXRH": "texas-roadhouse", "MCD": "mcdonalds", "SBUX": "starbucks",
    "YUM": "yum-brands", "CAVA": "cava-group", "SHAK": "shake-shack",
    "AAPL": "apple", "MSFT": "microsoft", "GOOGL": "alphabet",
    "AMZN": "amazon", "META": "meta-platforms", "NVDA": "nvidia",
    "TSLA": "tesla", "CRM": "salesforce", "NOW": "servicenow",
    "NFLX": "netflix", "DIS": "walt-disney",
    "JPM": "jpmorgan-chase", "GS": "goldman-sachs",
    "NKE": "nike", "LULU": "lululemon-athletica",
    "HD": "home-depot", "COST": "costco-wholesale",
    "V": "visa", "MA": "mastercard",
    "VRSK": "verisk-analytics", "STZ": "constellation-brands",
}

CACHE_DIR = Path("/tmp/transcript_cache")
URL_SUFFIXES = ["earnings-call-transcript", "earnings-transcript"]


@dataclass
class QuarterlyTranscript:
    ticker: str
    quarter: str          # "Q4 2025"
    filing_date: str      # EDGAR 8-K date
    source_url: str
    prepared_remarks: str
    qa_section: str
    full_text: str
    word_count: int
    has_qa: bool
    fetched_at: str


@dataclass
class BatchFetchResult:
    ticker: str
    transcripts: list[QuarterlyTranscript] = field(default_factory=list)
    missing_quarters: list[str] = field(default_factory=list)
    rate_limited: bool = False
    rate_limited_at: str = ""  # which quarter hit the limit


def _get_earnings_8k_dates(ticker: str) -> list[dict]:
    """
    Get 8-K filing dates from EDGAR, mapped to likely reported quarters.
    
    For each quarter window, collects ALL 8-K dates (not just the first).
    The batch fetcher tries URLs around each date, so more dates = more chances
    to find the correct MF URL.
    """
    cik = _resolve_cik(ticker)
    if not cik:
        return []
    
    try:
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                       headers=SEC_HEADERS, timeout=15)
        recent = r.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        
        all_8k_dates = [dates[i] for i, f in enumerate(forms) if f == "8-K"]
    except Exception:
        return []
    
    # Map each 8-K date to a quarter based on month
    # Earnings windows: Jan-Feb=Q4(prev yr), Apr-May=Q1, Jul-Aug=Q2, Oct-Nov=Q3
    earnings_windows = {
        (1,2): lambda yr: ("q4", yr-1),
        (4,5): lambda yr: ("q1", yr),
        (7,8): lambda yr: ("q2", yr),
        (10,11): lambda yr: ("q3", yr),
    }
    
    quarter_dates = {}  # qkey -> list of filing dates
    
    for fdate in all_8k_dates:
        y, m, _ = fdate.split("-")
        month, year = int(m), int(y)
        
        for months, qfn in earnings_windows.items():
            if month in months:
                q, qy = qfn(year)
                qkey = f"{q}-{qy}"
                if qkey not in quarter_dates:
                    quarter_dates[qkey] = []
                quarter_dates[qkey].append(fdate)
                quarter_dates[qkey].sort(reverse=True)  # latest first (earnings 8-K is usually latest in window)
                break
    
    # Sort quarters reverse-chronologically
    def sort_key(qkey):
        q, qy = qkey.split("-")
        q_num = int(q[1])
        return (int(qy), q_num)
    
    results = []
    for qkey in sorted(quarter_dates.keys(), key=sort_key, reverse=True):
        q, qy = qkey.split("-")
        results.append({
            "dates": quarter_dates[qkey],
            "quarter": qkey,
            "quarter_display": f"{q.upper()} {qy}",
        })
    
    return results


def _build_candidate_urls(ticker: str, filing_dates: list, quarter: str) -> list[str]:
    """Build candidate MF URLs ordered for fastest discovery.
    
    Strategy: try exact dates with primary suffix first (across all dates),
    then expand to offsets. This minimizes requests before finding a match.
    """
    slug = COMPANY_SLUGS.get(ticker.upper(), ticker.lower())
    
    if isinstance(filing_dates, str):
        filing_dates = [filing_dates]
    
    urls = []
    seen = set()
    
    def add(url):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    
    # Pass 1: exact date, primary suffix (1 request per date)
    for fd in filing_dates:
        y, m, d = fd.split("-")
        add(f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{d}/{slug}-{ticker.lower()}-{quarter}-earnings-call-transcript/")
    
    # Pass 2: exact date, alternate suffix
    for fd in filing_dates:
        y, m, d = fd.split("-")
        add(f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{d}/{slug}-{ticker.lower()}-{quarter}-earnings-transcript/")
    
    # Pass 3: +/-1 day offsets
    for fd in filing_dates:
        y, m, d = fd.split("-")
        dy = int(d)
        for offset in [-1, 1]:
            dd = dy + offset
            if dd < 1 or dd > 28:
                continue
            for suffix in URL_SUFFIXES:
                add(f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{dd:02d}/{slug}-{ticker.lower()}-{quarter}-{suffix}/")
    
    # Pass 4: +/-2 day offsets (last resort)
    for fd in filing_dates:
        y, m, d = fd.split("-")
        dy = int(d)
        for offset in [-2, 2]:
            dd = dy + offset
            if dd < 1 or dd > 28:
                continue
            add(f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{dd:02d}/{slug}-{ticker.lower()}-{quarter}-earnings-call-transcript/")
    
    return urls


def _parse_transcript(html: str) -> dict:
    """Parse transcript HTML into text sections."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    
    # Find boundaries
    start = len(text)
    for pat in [r"(?i)prepared\s+remarks", r"(?i)good\s+(morning|afternoon|evening)", r"(?i)operator"]:
        m = re.search(pat, text)
        if m and m.start() < start:
            start = m.start()
    
    end = len(text)
    for pat in [r"(?i)the\s+motley\s+fool\s+(has|recommends)", r"(?i)should\s+you\s+invest", r"(?i)where\s+to\s+invest"]:
        m = re.search(pat, text[start:])
        if m and start + m.start() < end:
            end = start + m.start()
    
    transcript = text[start:end].strip()
    
    # Split Q&A
    prepared, qa = transcript, ""
    for pat in [r"(?i)question.{0,5}and.{0,5}answer", r"(?i)\bQ\s*&\s*A\b"]:
        m = re.search(pat, transcript)
        if m and m.start() > len(transcript) * 0.1:
            prepared = transcript[:m.start()].strip()
            qa = transcript[m.start():].strip()
            break
    
    return {"prepared": prepared, "qa": qa, "full": transcript}



def _has_transcript_content(html: str) -> bool:
    """Check if HTML contains actual transcript content (not just a shell page)."""
    lower = html.lower()
    # Must have at least 2 of these markers to be a real transcript
    markers = [
        "prepared remarks",
        "question and answer",
        "question-and-answer",
        "operator",
        "good morning",
        "good afternoon",
        "good evening",
        "earnings call",
    ]
    hits = sum(1 for m in markers if m in lower)
    return hits >= 2 and len(html) > 50000


def fetch_quarterly_transcripts(
    ticker: str,
    quarters: int = 12,
    delay: float = 5.0,
    cache_dir: str = None,
    verbose: bool = True,
) -> BatchFetchResult:
    """
    Fetch up to N quarters of transcripts from Motley Fool.
    
    Args:
        ticker: Stock ticker
        quarters: Number of quarters to fetch (default 12)
        delay: Seconds between requests (default 5.0 — DO NOT reduce below 3)
        cache_dir: Directory for caching (default /tmp/transcript_cache)
        verbose: Print progress
    
    Returns:
        BatchFetchResult with transcripts and any missing quarters
    """
    ticker = ticker.upper()
    result = BatchFetchResult(ticker=ticker)
    cache = Path(cache_dir or CACHE_DIR) / ticker
    cache.mkdir(parents=True, exist_ok=True)
    
    # Get earnings dates
    earnings = _get_earnings_8k_dates(ticker)
    if verbose:
        print(f"Found {len(earnings)} potential earnings dates for {ticker}")
    
    targets = earnings[:quarters]
    
    for i, target in enumerate(targets):
        qkey = target["quarter"]
        q_display = target["quarter_display"]
        
        # Check cache first
        cache_file = cache / f"{qkey}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            result.transcripts.append(QuarterlyTranscript(**cached))
            if verbose:
                print(f"  CACHE {q_display} | {cached['word_count']} words")
            continue
        
        # Build and try URLs
        urls = _build_candidate_urls(ticker, target["dates"], qkey)
        
        found = False
        for url in urls:
            if i > 0 or found:  # delay between requests (not before first)
                time.sleep(delay)
            
            try:
                r = httpx.get(url, headers=BROWSER_HEADERS, timeout=10, follow_redirects=True)
                
                if r.status_code == 429:
                    if verbose:
                        print(f"  429  {q_display} — rate limited. Stop and retry later.")
                    result.rate_limited = True
                    result.rate_limited_at = q_display
                    # Add remaining as missing
                    for remaining in targets[i:]:
                        result.missing_quarters.append(remaining["quarter_display"])
                    return result
                
                if r.status_code == 200 and _has_transcript_content(r.text):
                    parsed = _parse_transcript(r.text)
                    transcript = QuarterlyTranscript(
                        ticker=ticker,
                        quarter=q_display,
                        filing_date=target["dates"][0],
                        source_url=url,
                        prepared_remarks=parsed["prepared"][:50000],
                        qa_section=parsed["qa"][:50000],
                        full_text=parsed["full"][:100000],
                        word_count=len(parsed["full"].split()),
                        has_qa=bool(parsed["qa"]),
                        fetched_at=datetime.utcnow().isoformat() + "Z",
                    )
                    result.transcripts.append(transcript)
                    
                    # Cache it
                    cache_file.write_text(json.dumps(asdict(transcript)))
                    
                    if verbose:
                        wc = transcript.word_count
                        qa = "Q&A" if transcript.has_qa else "no Q&A"
                        print(f"  OK   {q_display} | {wc:,} words | {qa}")
                    found = True
                    break
            except Exception as e:
                if verbose:
                    print(f"  ERR  {q_display} | {e}")
        
        if not found and not result.rate_limited:
            result.missing_quarters.append(q_display)
            if verbose:
                print(f"  MISS {q_display}")
    
    return result


# ── Helpers ──────────────────────────────────────────────────

_cik_cache = {}

def _resolve_cik(ticker):
    ticker = ticker.upper()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        r = httpx.get("https://www.sec.gov/files/company_tickers.json",
                       headers=SEC_HEADERS, timeout=15)
        for entry in r.json().values():
            if entry.get("ticker", "").upper() == ticker:
                cik = str(entry["cik_str"]).zfill(10)
                _cik_cache[ticker] = cik
                return cik
    except:
        pass
    return None


# ── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "CMG"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    
    result = fetch_quarterly_transcripts(ticker, quarters=n, delay=5.0, verbose=True)
    
    print(f"\n{'='*60}")
    print(f"Results: {len(result.transcripts)}/{n} quarters fetched")
    print(f"Missing: {result.missing_quarters}")
    if result.rate_limited:
        print(f"Rate limited at: {result.rate_limited_at}")
        print(f"Run again to resume (cached results will be reused)")
    
    total_words = sum(t.word_count for t in result.transcripts)
    qa_count = sum(1 for t in result.transcripts if t.has_qa)
    print(f"Total words: {total_words:,}")
    print(f"With Q&A: {qa_count}/{len(result.transcripts)}")
