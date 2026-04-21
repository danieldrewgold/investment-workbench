"""
Earnings Call Transcript Loader v2

Proven approach: Motley Fool publishes full free transcripts (prepared remarks + Q&A)
for most US large/mid-cap companies. URL discovery uses EDGAR 8-K dates + slug matching.

Tested: CMG ✅ (74K chars, full Q&A), DPZ ✅, multiple others
Coverage: ~70-80% of large/mid-cap US companies

Sources (priority order):
  1. Motley Fool — full transcript, free, best quality
  2. API Ninjas — free tier (non-commercial), needs free signup at api-ninjas.com
  3. EDGAR 8-K Exhibit 99.1 — press release fallback (no Q&A but always available)

NOT free:
  - FMP: $125/mo for transcripts (skip)
  - Seeking Alpha Premium: $20/mo but API access unclear
  - AlphaSense: enterprise pricing

The MF URL pattern:
  fool.com/earnings/call-transcripts/YYYY/MM/DD/{slug}-{ticker}-qX-YYYY-earnings-call-transcript/

Slug varies by company. Discovery uses EDGAR 8-K dates + pattern matching.
"""

import re
import json
import httpx
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


SEC_HEADERS = {"User-Agent": "InvestmentWorkbench research@example.com"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.5",
}

MAX_TRANSCRIPT_LENGTH = 50000


@dataclass
class TranscriptResult:
    ticker: str
    quarter: str
    date: str
    source: str
    source_url: str
    prepared_remarks: str = ""
    qa_section: str = ""
    full_text: str = ""
    participants: list[str] = field(default_factory=list)
    word_count: int = 0
    is_complete: bool = False
    is_truncated: bool = False
    quality_notes: str = ""


@dataclass
class TranscriptFetchReport:
    ticker: str
    results: list[TranscriptResult] = field(default_factory=list)
    sources_tried: list[dict] = field(default_factory=list)
    best_result: Optional[TranscriptResult] = None


# ── Known company slugs ──────────────────────────────────────

COMPANY_SLUGS = {
    "CMG": ["chipotle"], "DPZ": ["dominos"], "WING": ["wingstop"],
    "TXRH": ["texas-roadhouse"], "MCD": ["mcdonalds"], "SBUX": ["starbucks"],
    "YUM": ["yum-brands"], "QSR": ["restaurant-brands-international"],
    "CAVA": ["cava-group", "cava"], "SHAK": ["shake-shack"],
    "AAPL": ["apple"], "MSFT": ["microsoft"], "GOOGL": ["alphabet"],
    "AMZN": ["amazon"], "META": ["meta-platforms"], "NVDA": ["nvidia"],
    "TSLA": ["tesla"], "CRM": ["salesforce"], "NOW": ["servicenow"],
    "SNOW": ["snowflake"], "PLTR": ["palantir-technologies", "palantir"],
    "JPM": ["jpmorgan-chase"], "GS": ["goldman-sachs"],
    "MS": ["morgan-stanley"], "BAC": ["bank-of-america"],
    "NKE": ["nike"], "LULU": ["lululemon-athletica", "lululemon"],
    "TGT": ["target"], "WMT": ["walmart"], "COST": ["costco-wholesale", "costco"],
    "NFLX": ["netflix"], "DIS": ["walt-disney", "disney"],
    "V": ["visa"], "MA": ["mastercard"],
    "HD": ["home-depot"], "LOW": ["lowes-companies", "lowes"],
    "UNH": ["unitedhealth-group"], "JNJ": ["johnson-and-johnson"],
    "PG": ["procter-and-gamble", "procter-gamble"],
    "KO": ["coca-cola"], "PEP": ["pepsico"],
    "VRSK": ["verisk-analytics", "verisk"],
    "STZ": ["constellation-brands"],
}

URL_SUFFIXES = ["earnings-call-transcript", "earnings-transcript"]


# ── CIK + EDGAR helpers ─────────────────────────────────────

_cik_cache = {}

def _resolve_cik(ticker):
    ticker = ticker.upper()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        r = httpx.get("https://www.sec.gov/files/company_tickers.json",
                       headers=SEC_HEADERS, timeout=15)
        if r.status_code == 200:
            for entry in r.json().values():
                if entry.get("ticker", "").upper() == ticker:
                    cik = str(entry["cik_str"]).zfill(10)
                    _cik_cache[ticker] = cik
                    return cik
    except Exception:
        pass
    return None


def _get_8k_dates(ticker):
    cik = _resolve_cik(ticker)
    if not cik:
        return []
    try:
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                       headers=SEC_HEADERS, timeout=15)
        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        return [recent["filingDate"][i] for i, f in enumerate(recent.get("form", [])) if f == "8-K"][:8]
    except Exception:
        return []


def _get_slugs(ticker):
    if ticker in COMPANY_SLUGS:
        return COMPANY_SLUGS[ticker]
    try:
        import yfinance as yf
        name = (yf.Ticker(ticker).info.get("shortName") or "").lower()
        slug = re.sub(r"\b(inc|corp|corporation|ltd|limited|plc|co|company|holdings|group)\b\.?", "", name)
        slug = re.sub(r"[^a-z0-9\s-]", "", slug).strip()
        slug = re.sub(r"\s+", "-", slug).strip("-")
        return [slug] if slug else [ticker.lower()]
    except Exception:
        return [ticker.lower()]


def _guess_quarters(month, year):
    if month in [1, 2]:    return [("q4", year-1), ("q3", year-1)]
    if month in [3, 4]:    return [("q4", year-1), ("q1", year)]
    if month in [5, 6]:    return [("q1", year), ("q2", year)]
    if month in [7, 8]:    return [("q2", year), ("q1", year)]
    if month in [9, 10]:   return [("q3", year), ("q2", year)]
    return [("q3", year), ("q4", year)]


# ── Motley Fool ──────────────────────────────────────────────

def _find_mf_url(ticker, verbose=False):
    dates = _get_8k_dates(ticker)
    slugs = _get_slugs(ticker)
    if verbose:
        print(f"  [MF] 8-K dates: {dates[:3]}, slugs: {slugs}")
    
    for fdate in dates[:3]:
        y, m, d = fdate.split("-")
        year, month, day = int(y), int(m), int(d)
        for q, qy in _guess_quarters(month, year):
            for slug in slugs:
                for suffix in URL_SUFFIXES:
                    for offset in [0, -1, 1, -2, 2]:
                        dd = day + offset
                        if dd < 1 or dd > 28:
                            continue
                        url = f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{dd:02d}/{slug}-{ticker.lower()}-{q}-{qy}-{suffix}/"
                        try:
                            r = httpx.get(url, headers=BROWSER_HEADERS, timeout=5, follow_redirects=True)
                            if r.status_code == 200 and len(r.text) > 50000:
                                if verbose:
                                    print(f"  [MF] Found: {url}")
                                return url, r.text
                        except Exception:
                            pass
    return None, None


def _parse_mf(html, ticker, url):
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    
    # Find transcript boundaries
    start_idx = len(text)
    for marker in [r"(?i)prepared\s+remarks", r"(?i)operator.*good\s+(morning|afternoon)", r"(?i)good\s+(morning|afternoon).*welcome"]:
        m = re.search(marker, text)
        if m and m.start() < start_idx:
            start_idx = m.start()
    
    end_idx = len(text)
    for marker in [r"(?i)this\s+article\s+represents", r"(?i)the\s+motley\s+fool\s+(has|recommends)", r"(?i)should\s+you\s+invest", r"(?i)where\s+to\s+invest"]:
        m = re.search(marker, text[start_idx:])
        if m and start_idx + m.start() < end_idx:
            end_idx = start_idx + m.start()
    
    transcript = text[start_idx:end_idx].strip()
    
    # Split prepared/Q&A
    prepared, qa = transcript, ""
    for marker in [r"(?i)question.{0,5}and.{0,5}answer", r"(?i)\bQ\s*&\s*A\b"]:
        m = re.search(marker, transcript)
        if m and m.start() > len(transcript) * 0.1:
            prepared = transcript[:m.start()].strip()
            qa = transcript[m.start():].strip()
            break
    
    # Participants
    participants = list(dict.fromkeys(
        re.findall(r"([A-Z][a-z]+ [A-Z][a-z]+)\s*[-–—]\s*(?:CEO|CFO|COO|President|Chairman|Chief)", text)
    ))[:10]
    
    q_match = re.search(r"q(\d)-(\d{4})", url)
    quarter = f"Q{q_match.group(1)} {q_match.group(2)}" if q_match else ""
    
    return TranscriptResult(
        ticker=ticker, quarter=quarter, date="",
        source="MOTLEY_FOOL", source_url=url,
        prepared_remarks=prepared[:MAX_TRANSCRIPT_LENGTH],
        qa_section=qa[:MAX_TRANSCRIPT_LENGTH],
        full_text=transcript[:MAX_TRANSCRIPT_LENGTH * 2],
        participants=participants,
        word_count=len(transcript.split()),
        is_complete=bool(prepared and qa),
        quality_notes="Full transcript with Q&A" if qa else "Transcript (Q&A not identified)",
    )


def _fetch_motley_fool(ticker, verbose=False):
    if verbose:
        print(f"  [MF] Searching for {ticker} transcript...")
    url, html = _find_mf_url(ticker, verbose)
    if not url:
        return None
    return _parse_mf(html, ticker, url)


# ── EDGAR Press Release ──────────────────────────────────────

def _fetch_edgar_press_release(ticker, verbose=False):
    if verbose:
        print(f"  [EDGAR] Fetching press release...")
    cik = _resolve_cik(ticker)
    if not cik:
        return None
    cik_stripped = cik.lstrip("0")
    
    try:
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_HEADERS, timeout=15)
        recent = r.json().get("filings", {}).get("recent", {})
        
        for i, form in enumerate(recent.get("form", [])):
            if form != "8-K":
                continue
            acc = recent["accessionNumber"][i].replace("-", "")
            date = recent["filingDate"][i]
            
            idx_r = httpx.get(f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/index.json",
                               headers=SEC_HEADERS, timeout=15)
            if idx_r.status_code != 200:
                continue
            
            for item in idx_r.json().get("directory", {}).get("item", []):
                name = item.get("name", "").lower()
                if "ex99" not in name and "ex-99" not in name:
                    continue
                
                ex_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/{item['name']}"
                resp = httpx.get(ex_url, headers=SEC_HEADERS, timeout=30, follow_redirects=True)
                if resp.status_code != 200:
                    continue
                
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s+", " ", text)
                
                if sum(1 for kw in ["revenue", "earnings", "net income", "diluted"] if kw in text.lower()[:3000]) >= 2:
                    if verbose:
                        print(f"  [EDGAR] Found press release from {date}")
                    return TranscriptResult(
                        ticker=ticker, quarter=_infer_quarter(text, date), date=date,
                        source="EDGAR_PRESS_RELEASE", source_url=ex_url,
                        prepared_remarks=text[:MAX_TRANSCRIPT_LENGTH],
                        full_text=text[:MAX_TRANSCRIPT_LENGTH],
                        word_count=len(text.split()),
                        is_complete=False,
                        quality_notes="Press release only (no Q&A)",
                    )
    except Exception as e:
        if verbose:
            print(f"  [EDGAR] Error: {e}")
    return None


# ── API Ninjas ───────────────────────────────────────────────

def _fetch_api_ninjas(ticker, api_key=None, verbose=False):
    if not api_key:
        if verbose:
            print("  [API_NINJAS] No key — free signup at api-ninjas.com")
        return None
    try:
        r = httpx.get(f"https://api.api-ninjas.com/v1/earningstranscript?ticker={ticker}",
                       headers={"X-Api-Key": api_key}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("transcript"):
            return None
        text = data["transcript"]
        prepared, qa = _split_pq(text)
        return TranscriptResult(
            ticker=ticker, quarter=f"Q{data.get('quarter','')} {data.get('year','')}",
            date=data.get("date", ""), source="API_NINJAS", source_url="api-ninjas.com",
            prepared_remarks=prepared, qa_section=qa, full_text=text,
            word_count=len(text.split()), is_complete=bool(qa),
            quality_notes="Full transcript from API Ninjas",
        )
    except Exception:
        return None


# ── Orchestrator ─────────────────────────────────────────────

def fetch_transcript(ticker, api_ninjas_key=None, verbose=False):
    ticker = ticker.upper()
    report = TranscriptFetchReport(ticker=ticker)
    
    for name, fn, kwargs in [
        ("MOTLEY_FOOL", _fetch_motley_fool, {"verbose": verbose}),
        ("API_NINJAS", _fetch_api_ninjas, {"api_key": api_ninjas_key, "verbose": verbose}),
        ("EDGAR_PRESS_RELEASE", _fetch_edgar_press_release, {"verbose": verbose}),
    ]:
        try:
            result = fn(ticker, **kwargs)
            status = "success" if result else ("no_key" if name == "API_NINJAS" and not api_ninjas_key else "not_found")
            detail = f"{result.word_count} words, complete={result.is_complete}" if result else ""
            report.sources_tried.append({"source": name, "status": status, "detail": detail})
            if result:
                report.results.append(result)
        except Exception as e:
            report.sources_tried.append({"source": name, "status": "error", "detail": str(e)})
    
    if report.results:
        report.results.sort(key=lambda r: (r.is_complete * 200) + (bool(r.qa_section) * 100) + (r.word_count / 100), reverse=True)
        report.best_result = report.results[0]
    
    return report


def _split_pq(text):
    for marker in [r"(?i)question.{0,5}and.{0,5}answer", r"(?i)\bQ\s*&\s*A\b"]:
        m = re.search(marker, text)
        if m and m.start() > len(text) * 0.15:
            return text[:m.start()].strip(), text[m.start():].strip()
    return text, ""

def _infer_quarter(text, date):
    m = re.search(r"(?i)(Q[1-4]|first|second|third|fourth)\s*(quarter)?\s*(\d{4})?", text)
    if m:
        q = {"FIRST":"Q1","SECOND":"Q2","THIRD":"Q3","FOURTH":"Q4"}.get(m.group(1).upper(), m.group(1).upper())
        return f"{q} {m.group(3) or date[:4]}"
    return f"FY{date[:4]}"


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "CMG"
    report = fetch_transcript(ticker, verbose=True)
    print(f"\n{'='*60}")
    for s in report.sources_tried:
        print(f"  {s['source']:25s} {s['status']:12s} {s['detail']}")
    if report.best_result:
        r = report.best_result
        print(f"\nBest: {r.source} | {r.word_count} words | Q&A: {bool(r.qa_section)}")
