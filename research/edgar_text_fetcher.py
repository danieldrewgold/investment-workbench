"""
EDGAR Filing Text Fetcher

Lightweight sync fetcher for SEC filing text.
Fetches press releases, earnings call context, and filings.

Priority order for best research context:
  1. 8-K Exhibit 99.1 (press release - has actual financials, guidance, management commentary)
  2. 8-K shell (Item 2.02 - Results of Operations)
  3. 10-K MD&A section (annual - most comprehensive)
  4. 10-Q MD&A section (quarterly - more recent than 10-K)
"""

import re
import httpx

SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}

MAX_TEXT_LENGTH = 8000
_cik_cache = {}


def fetch_best_filing_text(ticker: str) -> tuple[str | None, str]:
    """
    Fetch the best available filing text for a ticker.

    Priority:
    1. 8-K Exhibit 99.1 (press release -- actual financials, guidance, commentary)
    2. 8-K shell (Item 2.02 -- Results of Operations)
    3. 10-K MD&A
    4. 10-Q MD&A

    Returns:
        (text, filing_type) tuple. text is None if all fail.
    """
    cik = _resolve_cik(ticker)
    if not cik:
        return None, "none"

    # Try press release first (Exhibit 99.1 from earnings 8-K)
    text = _fetch_press_release(cik)
    if text and len(text) > 500:
        return text, "press_release"

    # Try earnings 8-K shell
    text = _fetch_earnings_8k(cik)
    if text and len(text) > 300:
        return text, "8-K"

    # Try 10-K
    text = _fetch_filing(cik, "10-K")
    if text and len(text) > 300:
        return text, "10-K"

    # Try 10-Q
    text = _fetch_filing(cik, "10-Q")
    if text and len(text) > 300:
        return text, "10-Q"

    return None, "none"


def _fetch_press_release(cik: str) -> str | None:
    """
    Fetch Exhibit 99.1 (press release) from the most recent earnings 8-K.

    This is the richest free source -- contains actual financial results,
    comparable sales data, guidance, management quotes, segment breakdowns.
    Much better than the 8-K shell which just says "we filed a press release."
    """
    urls = _get_filing_urls(cik, "8-K", limit=5)
    cik_stripped = cik.lstrip("0")

    for u in urls:
        try:
            # Parse accession from URL
            # URL format: .../edgar/data/{cik}/{accession}/{doc}
            parts = u["url"].split("/")
            acc_idx = None
            for i, p in enumerate(parts):
                if p == "data" and i + 2 < len(parts):
                    acc_idx = i + 2
                    break
            if not acc_idx:
                continue
            acc = parts[acc_idx]

            # Get filing index to find Exhibit 99.1
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/index.json"
            idx_resp = httpx.get(idx_url, headers=SEC_HEADERS, timeout=15.0)
            if idx_resp.status_code != 200:
                continue

            items = idx_resp.json().get("directory", {}).get("item", [])
            exhibit_url = None

            for item in items:
                name = item.get("name", "").lower()
                # Look for ex99, exhibit99, press release exhibits
                if ("ex99" in name or "exhibit99" in name) and name.endswith((".htm", ".html", ".txt")):
                    exhibit_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/{item['name']}"
                    break

            if not exhibit_url:
                continue

            # Fetch and extract the press release
            resp = httpx.get(exhibit_url, headers=SEC_HEADERS, timeout=30.0, follow_redirects=True)
            if resp.status_code != 200:
                continue

            text = _clean_html(resp.text)

            # Verify this is an earnings press release (not a random exhibit)
            lower = text.lower()[:2000]
            is_earnings = any(kw in lower for kw in [
                "revenue", "earnings", "net income", "diluted", "fiscal",
                "results", "quarter", "operating", "comparable",
            ])

            if is_earnings and len(text) > 500:
                # Extract the most relevant sections (cap at MAX_TEXT_LENGTH)
                return _extract_press_release(text)

        except Exception:
            continue

    return None


def _extract_press_release(text: str) -> str:
    """
    Extract key sections from a press release.
    Press releases have: headline, financial highlights, segment data,
    guidance/outlook, management quotes, and financial tables.
    """
    sections = []

    # Get the headline and first few paragraphs (usually the key numbers)
    # Skip boilerplate header (company name, date, etc.)
    start = 0
    for marker in ["reported", "announced", "highlights", "results"]:
        idx = text.lower().find(marker)
        if idx > 0 and idx < 1000:
            start = max(0, idx - 100)
            break

    # First section: headline + key metrics (first 2000 chars from start)
    sections.append(text[start:start + 2000])

    # Look for specific high-value sections
    section_markers = [
        r"(?i)guidance|outlook|expect",
        r"(?i)comparable.{0,20}(restaurant|store|sales)",
        r"(?i)segment|breakdown",
        r"(?i)margin|operating income",
        r"(?i)new.{0,10}(restaurant|store|unit|opening)",
        r"(?i)digital|online|delivery",
        r"(?i)(chief|ceo|cfo|president).{0,30}(said|stated|commented|noted)",
    ]

    for marker in section_markers:
        match = re.search(marker, text)
        if match:
            s = max(0, match.start() - 50)
            e = min(len(text), match.start() + 800)
            chunk = text[s:e].strip()
            if len(chunk) > 80 and chunk not in sections:
                sections.append(chunk)

    combined = "\n\n[...]\n\n".join(sections[:6])
    return combined[:MAX_TEXT_LENGTH]


def _fetch_earnings_8k(cik: str) -> str | None:
    """
    Fetch the most recent EARNINGS 8-K (Item 2.02).
    Fallback when Exhibit 99.1 is not available.
    """
    urls = _get_filing_urls(cik, "8-K", limit=5)
    for u in urls:
        text = _fetch_and_extract(u["url"], "8-K")
        if text and ("item 2.02" in text.lower() or
                     "results of operations" in text.lower() or
                     "earnings" in text.lower()[:500]):
            return text
    return None


def fetch_filing_text(ticker: str, filing_type: str = "10-K") -> str | None:
    """Fetch a specific filing type. Backward compatible."""
    cik = _resolve_cik(ticker)
    if not cik:
        return None
    return _fetch_filing(cik, filing_type)


def _resolve_cik(ticker: str) -> str | None:
    """Resolve ticker to CIK number. Cached."""
    ticker = ticker.upper()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        resp = httpx.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_HEADERS, timeout=15.0)
        if resp.status_code != 200:
            return None
        for entry in resp.json().values():
            if entry.get("ticker", "").upper() == ticker:
                cik = str(entry["cik_str"]).zfill(10)
                _cik_cache[ticker] = cik
                return cik
    except Exception:
        pass
    return None


def _fetch_filing(cik: str, filing_type: str) -> str | None:
    """Fetch and extract text from the most recent filing of given type."""
    url = _get_filing_url(cik, filing_type)
    if not url:
        return None
    return _fetch_and_extract(url, filing_type)


def _get_filing_url(cik: str, filing_type: str) -> str | None:
    """Get URL of most recent filing of given type."""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = httpx.get(url, headers=SEC_HEADERS, timeout=15.0)
        if resp.status_code != 200:
            return None

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        for i, form in enumerate(forms):
            if form == filing_type and i < len(accessions) and i < len(primary_docs):
                acc = accessions[i].replace("-", "")
                doc = primary_docs[i]
                return f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc}/{doc}"
    except Exception:
        pass
    return None


def _get_filing_urls(cik: str, filing_type: str, limit: int = 3) -> list[str]:
    """Get URLs of multiple recent filings of given type."""
    urls = []
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = httpx.get(url, headers=SEC_HEADERS, timeout=15.0)
        if resp.status_code != 200:
            return urls

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        dates = recent.get("filingDate", [])

        for i, form in enumerate(forms):
            if form == filing_type and i < len(accessions) and i < len(primary_docs):
                acc = accessions[i].replace("-", "")
                doc = primary_docs[i]
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc}/{doc}"
                date = dates[i] if i < len(dates) else ""
                urls.append({"url": filing_url, "date": date})
                if len(urls) >= limit:
                    break
    except Exception:
        pass
    return urls


def _clean_html(html: str) -> str:
    """Strip HTML tags and clean whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _fetch_and_extract(url: str, filing_type: str = "10-K") -> str | None:
    """Fetch filing and extract relevant text sections."""
    try:
        resp = httpx.get(url, headers=SEC_HEADERS, timeout=30.0, follow_redirects=True)
        if resp.status_code != 200:
            return None

        content = resp.text

        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        text = re.sub(r'&#\d+;', ' ', text)

        if len(text) < 200:
            return None

        # Different extraction strategies by filing type
        if filing_type == "8-K":
            return _extract_8k(text)
        else:
            return _extract_10k_10q(text)

    except Exception:
        return None


def _extract_8k(text: str) -> str | None:
    """
    Extract from 8-K earnings release.
    8-Ks are shorter and more focused. Look for:
    - Results of operations
    - Revenue/earnings highlights
    - Guidance/outlook
    """
    sections = []

    markers = [
        r'(?i)results?\s+of\s+operations',
        r'(?i)financial\s+results',
        r'(?i)fourth\s+quarter|first\s+quarter|second\s+quarter|third\s+quarter',
        r'(?i)fiscal\s+(year|20\d{2})\s+results',
        r'(?i)highlights',
        r'(?i)outlook|guidance|expects?\s+(?:revenue|earnings|EPS)',
        r'(?i)same.store\s+sales|comparable\s+(?:restaurant|store)\s+sales',
        r'(?i)operating\s+margin|operating\s+income',
    ]

    for marker in markers:
        match = re.search(marker, text)
        if match:
            start = max(0, match.start() - 50)
            end = min(len(text), match.start() + 1500)
            section = text[start:end].strip()
            if len(section) > 80 and section not in sections:
                sections.append(section)

    if sections:
        combined = " [...] ".join(sections[:4])
        return combined[:MAX_TEXT_LENGTH]

    # Fallback: take a chunk from near the start (skip boilerplate header)
    if len(text) > 1000:
        return text[500:500 + MAX_TEXT_LENGTH].strip()
    return None


def _extract_10k_10q(text: str) -> str | None:
    """
    Extract from 10-K or 10-Q.
    Longer filings. Target MD&A, results of operations, risk factors.
    """
    sections = []

    markers = [
        r'(?i)results?\s+of\s+operations',
        r'(?i)management.s?\s+discussion\s+and\s+analysis',
        r'(?i)financial\s+highlights',
        r'(?i)overview\s+of\s+(?:our|the)\s+(?:business|company)',
        r'(?i)revenue\s+(?:recognition|overview|discussion)',
        r'(?i)outlook|guidance',
    ]

    for marker in markers:
        match = re.search(marker, text)
        if match:
            start = max(0, match.start() - 100)
            end = min(len(text), match.start() + 2000)
            section = text[start:end].strip()
            if len(section) > 100:
                sections.append(section)

    if sections:
        combined = " [...] ".join(sections[:3])
        return combined[:MAX_TEXT_LENGTH]

    mid = len(text) // 3
    return text[mid:mid + MAX_TEXT_LENGTH].strip() if len(text) > MAX_TEXT_LENGTH else None


def get_filing_metadata(ticker: str) -> dict:
    """
    Get metadata about available filings for a ticker.
    Useful for the CLI to show what's available.
    """
    cik = _resolve_cik(ticker)
    if not cik:
        return {"ticker": ticker, "cik": None, "filings": []}

    filings = []
    for ftype in ["8-K", "10-K", "10-Q"]:
        urls = _get_filing_urls(cik, ftype, limit=2)
        for u in urls:
            filings.append({"type": ftype, "date": u["date"], "url": u["url"]})

    return {"ticker": ticker, "cik": cik, "filings": filings}
