"""
Shared EDGAR helpers: CIK resolution, 8-K exhibit scanning, quarter mapping.

Used by press_release_loader and slide_deck_loader. Extracted from
presentation_loader.py's internal helpers to avoid duplication and add
caching + better fiscal quarter handling.

EDGAR is rate-limited (10 req/sec per IP). These helpers respect that by
using short timeouts and not issuing concurrent requests from the same
process — if you need parallelism, batch-fetch first then parse locally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx


# SEC requires a real User-Agent with contact info. Use a project identifier.
SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research-agent research@example.com",
    "Accept": "application/json",
}

EDGAR_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"


# --------------------------------------------------------------------------
# CIK resolution (module-level cache — CIKs don't change)
# --------------------------------------------------------------------------

_cik_cache: dict[str, str] = {}


def resolve_cik(ticker: str, *, verbose: bool = False) -> str | None:
    """Return the 10-digit CIK for a ticker, or None if not found."""
    ticker = ticker.upper().strip()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        resp = httpx.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS, timeout=20.0,
        )
        if resp.status_code != 200:
            if verbose:
                print(f"  [EDGAR] CIK lookup failed HTTP {resp.status_code}")
            return None
        for entry in resp.json().values():
            if entry.get("ticker", "").upper() == ticker:
                cik = str(entry["cik_str"]).zfill(10)
                _cik_cache[ticker] = cik
                return cik
    except Exception as e:
        if verbose:
            print(f"  [EDGAR] CIK lookup exception: {e}")
    return None


# --------------------------------------------------------------------------
# Filing + exhibit discovery
# --------------------------------------------------------------------------

@dataclass
class ExhibitMeta:
    """Metadata for a single exhibit found in an 8-K filing."""
    cik: str
    accession: str              # raw format, e.g. "0001193125-26-001234"
    accession_compact: str      # no dashes
    filing_type: str            # "8-K"
    filing_date: str            # "YYYY-MM-DD"
    report_date: str            # "YYYY-MM-DD" if given; may equal filing_date
    exhibit_num: str            # "99.1", "99.2", etc.
    filename: str
    url: str
    size: int
    content_type: str           # "html", "pdf", "xlsx", "unknown"


def scan_8k_exhibits(
    ticker: str,
    *,
    max_filings: int = 12,
    form_types: list[str] | None = None,
    verbose: bool = False,
) -> list[ExhibitMeta]:
    """
    Scan the last N 8-K filings for the ticker and return ALL exhibits
    with their metadata. Does NOT download content — that's the caller's job.

    Default max_filings=12 targets 3 years of quarterly earnings 8-Ks.
    """
    if form_types is None:
        form_types = ["8-K", "8-K/A"]

    cik = resolve_cik(ticker, verbose=verbose)
    if not cik:
        return []

    cik_stripped = cik.lstrip("0")

    try:
        resp = httpx.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS, timeout=20.0,
        )
        if resp.status_code != 200:
            if verbose:
                print(f"  [EDGAR] submissions HTTP {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:
        if verbose:
            print(f"  [EDGAR] submissions exception: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    items_meta = recent.get("items", [])
    # 8-K item codes that carry a press release / supplement (earnings, other
    # events, officer changes, Reg FD, material agreement). Prolific filers
    # (e.g. WING, with a whole-business securitization) bury quarterly earnings
    # 8-Ks under dozens of governance/ABS filings — skipping the non-material
    # ones keeps the scan budget from being spent before we reach the releases.
    material_items = ("2.02", "8.01", "5.02", "7.01", "1.01")

    # Order filings earnings-first (Item 2.02), then other material 8-Ks — both
    # newest-first within each group (stable sort preserves the recency order).
    # Without this, a prolific 8.01/ABS filer's noise crowds quarterly earnings
    # releases out of the budget.
    candidates: list[int] = []
    for i, form in enumerate(forms):
        if form not in form_types:
            continue
        codes = items_meta[i] if i < len(items_meta) else ""
        if codes and not any(c in codes for c in material_items):
            continue
        candidates.append(i)
    candidates.sort(key=lambda i: 0 if "2.02" in (items_meta[i] if i < len(items_meta) else "") else 1)

    results: list[ExhibitMeta] = []
    filings_scanned = 0

    for i in candidates:
        if filings_scanned >= max_filings:
            break
        filings_scanned += 1

        accession = accessions[i] if i < len(accessions) else ""
        acc_compact = accession.replace("-", "")
        filing_date = filing_dates[i] if i < len(filing_dates) else ""
        report_date = report_dates[i] if i < len(report_dates) else filing_date

        idx_url = f"{EDGAR_ARCHIVE_BASE}/{cik_stripped}/{acc_compact}/index.json"
        try:
            idx_resp = httpx.get(idx_url, headers=SEC_HEADERS, timeout=20.0)
            if idx_resp.status_code != 200:
                continue
            items = idx_resp.json().get("directory", {}).get("item", [])
        except Exception:
            continue

        for item in items:
            name = item.get("name", "")
            name_lower = name.lower()
            # Identify Exhibit 99.x documents by the exhibit-number token in the
            # filename — names vary wildly (tmdx-ex99_1.htm, a991wingearnings…,
            # q120268kexh991.htm, costex9918-k.htm), so keyword matching misses
            # them. Capture a SINGLE digit after 99 (8-K exhibits are 99.1–99.9;
            # Costco's "ex9918-k" = Ex 99.1 + "8-K" must not read as "99.18").
            # Exclude XBRL / index machinery (R1.htm, *-index.html, MetaLinks…).
            if not name_lower.endswith((".htm", ".html", ".pdf")):
                continue
            if ("index" in name_lower or "-headers" in name_lower
                    or re.match(r"r\d+\.htm", name_lower)
                    or "metalinks" in name_lower or "filingsummary" in name_lower):
                continue
            # Allow an optional leading zero — some filers zero-pad ("ex9901"
            # = Ex 99.01 = 99.1, AAOI's convention).
            m = re.search(r"99[._-]?0?([1-9])", name_lower)
            if m:
                exhibit_num = f"99.{m.group(1)}"
            elif re.search(r"(earnings|press|news)[._-]?release", name_lower):
                # Some filers name the earnings press release DESCRIPTIVELY with
                # no 99-token in the filename (Intel: q126earningsrelease.htm /
                # q425earningsrelease.htm). Treat a clearly-named release as the
                # Ex 99.1 it conventionally is. The 8-K cover body
                # (<ticker>-<date>.htm) never matches this keyword, so it stays
                # out; 99-token filers (WING/COST) hit the number path first, so
                # this never double-counts.
                exhibit_num = "99.1"
            else:
                continue

            # Content type from extension
            if name_lower.endswith(".pdf"):
                content_type = "pdf"
            elif name_lower.endswith((".htm", ".html")):
                content_type = "html"
            elif name_lower.endswith((".xlsx", ".xls")):
                content_type = "xlsx"
            else:
                content_type = "unknown"

            url = f"{EDGAR_ARCHIVE_BASE}/{cik_stripped}/{acc_compact}/{name}"

            results.append(ExhibitMeta(
                cik=cik,
                accession=accession,
                accession_compact=acc_compact,
                filing_type=form,
                filing_date=filing_date,
                report_date=report_date,
                exhibit_num=exhibit_num,
                filename=name,
                url=url,
                size=int(item.get("size", 0)),
                content_type=content_type,
            ))
        if verbose:
            print(f"  [EDGAR] {filing_date} {form} acc {accession}: scanned")

    return results


# --------------------------------------------------------------------------
# Quarter inference
# --------------------------------------------------------------------------

def infer_fiscal_quarter(report_date: str, fiscal_year_end_month: int = 12) -> str:
    """
    Map a period-end date to a fiscal-quarter label like "Q3 2026".

    NOTE: This expects `report_date` to be the PERIOD-END date, NOT the
    filing date. For 10-K/10-Q periodic reports this is correct. For 8-K
    earnings releases, use `infer_earnings_quarter()` instead — those
    filings report a PRIOR completed quarter, so the filing date needs
    to be shifted back to land in the reported period.

    Defaults to calendar-year fiscal end (December). For non-December
    fiscal years (e.g., TGT: January, DE: October, CSCO: July), pass
    `fiscal_year_end_month` explicitly.

    Returns empty string if the date can't be parsed.
    """
    if not report_date:
        return ""
    try:
        d = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        return ""

    # Calendar-year case (fiscal end = December): calendar quarter works directly.
    if fiscal_year_end_month == 12:
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"

    # Non-December: shift the year end to align.
    # E.g., fiscal_year_end_month = 1 (Target): FY2026 ends Jan 2026,
    # Q1 FY2026 = Feb-Apr 2025, Q2 = May-Jul, Q3 = Aug-Oct, Q4 = Nov-Jan.
    fy_start_month = (fiscal_year_end_month % 12) + 1  # month AFTER FY end
    months_into_fy = (d.month - fy_start_month) % 12
    q = months_into_fy // 3 + 1

    # Fiscal year label: if we're past the fiscal year end month,
    # we're already in the next FY. Otherwise we're still in the prior one.
    if d.month > fiscal_year_end_month:
        fy_year = d.year + 1
    else:
        fy_year = d.year

    return f"Q{q} {fy_year}"


def infer_earnings_quarter(
    filing_date: str,
    *,
    fiscal_year_end_month: int = 12,
    reporting_lag_days: int = 35,
) -> str:
    """
    Map an earnings-release FILING date to the fiscal quarter being REPORTED.

    Earnings 8-Ks are filed after the quarter ends, typically 30-45 days
    after. So a 2025-10-29 filing is reporting Q3 2025 (the quarter that
    ended 2025-09-30). The default 35-day shift reliably lands in the
    middle of the reported quarter across most companies.

    Use this instead of `infer_fiscal_quarter` when you have a filing
    date (not a period-end date).
    """
    if not filing_date:
        return ""
    try:
        d = datetime.strptime(filing_date, "%Y-%m-%d").date()
    except ValueError:
        return ""

    from datetime import timedelta
    shifted = d - timedelta(days=reporting_lag_days)
    return infer_fiscal_quarter(
        shifted.strftime("%Y-%m-%d"),
        fiscal_year_end_month=fiscal_year_end_month,
    )


# --------------------------------------------------------------------------
# Content fetch (HTML text / PDF bytes)
# --------------------------------------------------------------------------

def fetch_exhibit_bytes(url: str, *, timeout: float = 60.0) -> bytes | None:
    """Fetch raw bytes from an EDGAR exhibit URL. Returns None on failure."""
    try:
        resp = httpx.get(url, headers=SEC_HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception:
        return None
