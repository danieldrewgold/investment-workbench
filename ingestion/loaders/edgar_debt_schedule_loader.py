"""
EDGAR Long-Term Debt Schedule Loader

Per-issuer fetch of outstanding bond series from the most recent 10-K.
Approach:
  1. Resolve ticker -> issuer CIK (via the shared cache from edgar_13d_loader)
  2. Pull the most recent 10-K filing index
  3. Read FilingSummary.xml to find the report named like
     "Schedule of Long-Term Debt (Details)" — this is the structured XBRL-
     rendered table (R##.htm), not free-form HTML
  4. Parse that table for: series_label, coupon_pct, par_amount_m, maturity_date
  5. Best-effort scan of the main Long-Term Debt note (R13.htm or similar)
     for redemption / call language to populate is_callable + call_type
     + redemption_terms

The XBRL R##.htm tables are far more structured than the free-form 10-K
prose — they're tagged tables that always have the same row layout
(line-item rows under a member axis identifying each debt series).

CUSIPs are NOT in the long-term debt note in most issuer 10-Ks. They live
in Exhibit 4 indenture filings or the cover-page Securities Registered
table. v1 leaves CUSIPs nullable; FINRA TRACE search by issuer + maturity
will resolve them when the price loader runs.

Cache: weekly per-ticker. 10-K filings only update annually so even daily
would be wasteful.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from ingestion.loaders.edgar_13d_loader import SEC_HEADERS, resolve_ticker_to_cik


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class BondSeries:
    """One outstanding bond series from the issuer's debt schedule."""
    series_label: str = ""
    coupon_pct: float | None = None
    par_amount_m: float | None = None
    maturity_date: str = ""             # YYYY-MM-DD
    cusip: str = ""                      # often empty from the LT-debt note
    is_callable: bool = False
    call_type: str = ""                  # 'make_whole'|'fixed_schedule'|'continuous'|''
    call_price_pct: float | None = None
    first_call_date: str = ""
    redemption_terms: str = ""           # short excerpt of redemption clause


@dataclass
class DebtScheduleBundle:
    ticker: str = ""
    issuer_cik: str = ""
    issuer_name: str = ""
    fetched_at: str = ""
    most_recent_10k_accession: str = ""
    most_recent_10k_filed_date: str = ""
    bonds: list = field(default_factory=list)        # list[BondSeries]
    other_long_term_debt_m: float | None = None      # catch-all not broken out
    total_long_term_debt_m: float | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer_cik": self.issuer_cik,
            "issuer_name": self.issuer_name,
            "fetched_at": self.fetched_at,
            "most_recent_10k_accession": self.most_recent_10k_accession,
            "most_recent_10k_filed_date": self.most_recent_10k_filed_date,
            "bonds": [asdict(b) for b in self.bonds],
            "other_long_term_debt_m": self.other_long_term_debt_m,
            "total_long_term_debt_m": self.total_long_term_debt_m,
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        if not self.bonds and not self.total_long_term_debt_m:
            return ""

        lines = [
            f"=== ISSUER DEBT SCHEDULE ({self.ticker}, latest 10-K filed "
            f"{self.most_recent_10k_filed_date}) ===",
            f"(Outstanding bond series + maturities. Source: SEC 10-K Long-"
            f"Term Debt note. Spread / pricing data is loaded separately by "
            f"the spread monitor when FINRA TRACE credentials are present.)",
            "",
        ]
        if self.total_long_term_debt_m is not None:
            lines.append(
                f"Total long-term debt outstanding: "
                f"${self.total_long_term_debt_m/1000:.2f}B"
            )
            lines.append("")

        # Sort by maturity ascending
        sorted_bonds = sorted(
            self.bonds,
            key=lambda b: (b.maturity_date or "9999-99-99"),
        )
        for b in sorted_bonds:
            par = (f"${b.par_amount_m:,.0f}M" if b.par_amount_m else "?")
            cpn = (f"{b.coupon_pct:.3f}%" if b.coupon_pct is not None else "?")
            mat = b.maturity_date or "?"
            call_tag = "[CALLABLE] " if b.is_callable else ""
            lines.append(f"  {call_tag}{b.series_label}")
            lines.append(f"      Par: {par} | Coupon: {cpn} | Maturity: {mat}")
            if b.is_callable and b.redemption_terms:
                lines.append(f"      Redemption: {b.redemption_terms[:200]}")

        if self.other_long_term_debt_m:
            lines.append(
                f"  + Other long-term debt: ${self.other_long_term_debt_m:,.0f}M "
                f"(not broken out into series)"
            )

        lines.append("")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 10-K discovery
# --------------------------------------------------------------------------

_10K_ROW_RE = re.compile(
    r'<td[^>]*>(10-K(?:/A)?)</td>\s*'
    r'<td[^>]*>\s*<a[^>]*href="'
    r'(/Archives/edgar/data/(\d+)/\d+/([\d-]+)-index\.htm)"',
    re.DOTALL,
)
_DATE_AFTER_RE = re.compile(r"<td[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</td>")


def _find_latest_10k(issuer_cik: str, *, verbose: bool = False) -> dict | None:
    """Return {accession_number, filed_date, index_url, accession_dir} for
    the most recent 10-K, or None if not found."""
    cik = issuer_cik.lstrip("0").zfill(10)
    try:
        r = httpx.get(
            "https://www.sec.gov/cgi-bin/browse-edgar",
            params={
                "action": "getcompany", "CIK": cik, "type": "10-K",
                "dateb": "", "owner": "include", "count": 5,
            },
            headers=SEC_HEADERS, timeout=30, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
    except Exception:
        return None

    body = r.text
    for m in _10K_ROW_RE.finditer(body):
        idx_path = m.group(2)
        accession = m.group(4)
        rest = body[m.end():m.end() + 800]
        d = _DATE_AFTER_RE.search(rest)
        filed_date = d.group(1) if d else ""
        # accession_dir is the directory the filing lives in (no extension)
        accession_dir = idx_path.rsplit("/", 1)[0]
        return {
            "accession_number": accession,
            "filed_date": filed_date,
            "index_url": "https://www.sec.gov" + idx_path,
            "accession_dir": "https://www.sec.gov" + accession_dir,
        }
    if verbose:
        print(f"  Debt loader: no 10-K found for CIK {cik}")
    return None


# --------------------------------------------------------------------------
# Find the right R##.htm via FilingSummary.xml
# --------------------------------------------------------------------------

# Disclosure-table name matching — issuer 10-Ks use a wide range of phrasings
# for the per-series debt schedule. We classify ShortNames into two buckets:
# STRUCTURED (preferred) — these are XBRL-rendered tables with consistent
# row layout per debt instrument; FALLBACK — text-heavy notes used only when
# no structured schedule is present. We also scan for convertible-notes
# schedules separately so issuers with both senior + convertible series
# (e.g. AAOI) are fully captured.
_STRUCTURED_PATTERNS = [
    re.compile(r"schedule\s+of\s+(?:notes?\s+payable\s+(?:and|&)\s+)?long.?term\s+debt", re.I),
    re.compile(r"schedule\s+of\s+long.?term\s+debt", re.I),
    re.compile(r"schedule\s+of\s+notes?\s+payable", re.I),
    re.compile(r"carrying\s+value\s+of\s+long.?term\s+debt", re.I),
    re.compile(r"long.?term\s+debt.{0,40}\(details\)\s*$", re.I),
]
_CONVERTIBLE_PATTERNS = [
    re.compile(r"schedule\s+of\s+(?:carrying\s+value\s+of\s+)?convertible\s+(?:senior\s+)?notes?", re.I),
    re.compile(r"convertible\s+(?:senior\s+)?notes?.{0,40}\(details\)\s*$", re.I),
    re.compile(r"carrying\s+value\s+of\s+convertible", re.I),
]
# 'Textual' / 'Parentheticals' tables are prose, not structured rows — skip
_SKIP_PATTERNS = [
    re.compile(r"\bparenthetical", re.I),
    re.compile(r"\btextual\b", re.I),
    re.compile(r"\(tables?\)\s*$", re.I),  # "(Tables)" is the layout container, not the data
]


def _find_debt_report_htmls(accession_dir: str, *, verbose: bool = False) -> list[tuple[str, str, str]]:
    """Return [(rendered_html_url, short_name, kind)] for every structured
    debt-schedule table in the filing. kind is 'senior' or 'convertible'.
    Empty list if none found."""
    summary_url = f"{accession_dir}/FilingSummary.xml"
    try:
        r = httpx.get(summary_url, headers=SEC_HEADERS, timeout=30,
                      follow_redirects=True)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    body = r.text
    matches = re.findall(
        r"<Report[^>]*>.*?<ShortName>([^<]+)</ShortName>.*?<HtmlFileName>([^<]+\.htm)</HtmlFileName>",
        body, re.DOTALL,
    )

    found: list[tuple[str, str, str]] = []
    seen_html: set[str] = set()
    for short_name, html_name in matches:
        if html_name in seen_html:
            continue
        if any(p.search(short_name) for p in _SKIP_PATTERNS):
            continue
        kind = None
        if any(p.search(short_name) for p in _CONVERTIBLE_PATTERNS):
            kind = "convertible"
        elif any(p.search(short_name) for p in _STRUCTURED_PATTERNS):
            kind = "senior"
        if kind:
            found.append((f"{accession_dir}/{html_name}", short_name, kind))
            seen_html.add(html_name)

    if verbose:
        if found:
            for url, sn, k in found:
                print(f"  Debt loader: found [{k}] '{sn}' in {url.rsplit('/', 1)[1]}")
        else:
            print(f"  Debt loader: no structured debt-schedule table found in FilingSummary")
    return found


# --------------------------------------------------------------------------
# Parse the rendered XBRL debt schedule table
# --------------------------------------------------------------------------

# Series label pattern. Matches things like:
#   "3.000% Senior Notes due May 2027"
#   "1.375% Senior Notes due June 2027"
#   "2.750% Senior Notes due May 2024"
#   "5.250% Senior Notes due 2030"
#   "3.20% Notes due January 2030"
#   "0.000% Convertible Senior Notes due 2026"
_SERIES_LABEL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s+"                 # 1: coupon
    r"((?:Convertible\s+)?(?:Senior\s+)?(?:Subordinated\s+)?(?:Secured\s+)?(?:Unsecured\s+)?Notes?)"  # 2: type
    r"\s+(?:due|maturing)\s+"
    r"(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"  # 3: month (optional)
    r"(\d{4})",                                # 4: year
    re.IGNORECASE,
)

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _series_to_maturity(month: str | None, year: str) -> str:
    """Convert ('May', '2027') to '2027-05-15' (mid-month default if day
    unknown). Year-only -> '2027-12-31' as a conservative default."""
    if not year:
        return ""
    y = int(year)
    if month:
        m = _MONTH_NUM.get(month.lower())
        if m:
            return f"{y:04d}-{m:02d}-15"
    return f"{y:04d}-12-31"


def _parse_par_amount_m(text: str) -> float | None:
    """Extract a $-amount-in-millions from a cell. Cells look like
    '$ 1,000', '1,250', '$  805 $  919', etc. We take the FIRST monetary
    figure (the most recent fiscal-year value)."""
    # Strip $ and commas, find first multi-digit number
    cleaned = re.sub(r"[$\s,]", " ", text)
    m = re.search(r"\b(\d{2,7})(?:\.\d+)?\b", cleaned)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


_SERIES_HEADER_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*%\s+"                              # 1: coupon
    r"((?:Convertible\s+)?(?:Senior\s+|Subordinated\s+|Secured\s+|Unsecured\s+)*Notes?)\s+"  # 2: type
    r"(?:due|maturing)\s+"
    r"(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"  # 3: month (optional)
    r"(\d{4})\b",                                             # 4: year
    re.IGNORECASE,
)


def _parse_debt_schedule_html(html: str) -> tuple[list[BondSeries], float | None, float | None]:
    """Parse the rendered XBRL debt-schedule table HTML.

    Strategy: locate every series-header in the flattened text, then for
    each header, take the slice from that header to the NEXT header
    (or to the next major section like 'Other Long Term Debt') and
    extract par + coupon from within that slice. More robust than a
    single mega-regex because it tolerates blank cells, missing rows
    on matured series, and ordering quirks across issuers.
    """
    # Strip styles/scripts and decode entities BEFORE stripping tags so
    # &#160; (non-breaking space) becomes a regular space.
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    import html as _html_mod
    text = _html_mod.unescape(text)
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()

    # Find every series header position
    headers: list[tuple[int, int, re.Match]] = [
        (m.start(), m.end(), m) for m in _SERIES_HEADER_RE.finditer(plain)
    ]
    # Also find "Other Long Term Debt" / "Other Long-Term Debt" as a sentinel
    # marking the end of the per-series block
    other_m_match = re.search(r"Other Long.?Term Debt", plain, re.IGNORECASE)
    other_pos = other_m_match.start() if other_m_match else len(plain)

    bonds: list[BondSeries] = []
    seen_labels: set[str] = set()

    for i, (start, end, header_match) in enumerate(headers):
        # Slice from end of this header to start of next header (or 'Other')
        next_start = headers[i + 1][0] if i + 1 < len(headers) else other_pos
        slice_text = plain[end:next_start]

        # Build the series label from the header match
        coupon_str = header_match.group(1)
        notes_type = header_match.group(2)
        month = header_match.group(3)
        year = header_match.group(4)
        try:
            coupon_pct = float(coupon_str)
        except ValueError:
            continue
        # Reconstruct the canonical label as the issuer would write it
        # (preserve trailing zeros: "3.000%" not "3.0%")
        # Use the original substring so we keep the issuer's exact formatting
        label = plain[start:end].strip()
        if label in seen_labels:
            continue

        maturity = _series_to_maturity(month, year)

        # Extract par from the slice. Look for "Long-Term Debt, Gross $ X,XXX"
        # pattern — only the FIRST $-amount (most-recent fiscal year) is the
        # current outstanding par. If "Long-Term Debt, Gross" doesn't appear
        # in the slice, the series has no current-period par (matured).
        par_m = None
        gm = re.search(
            r"Long.?Term Debt,?\s*Gross\s*\$?\s*([\d,]+)",
            slice_text, re.IGNORECASE,
        )
        if gm:
            try:
                par_m = float(gm.group(1).replace(",", ""))
            except ValueError:
                pass

        # If no par captured, treat as matured and skip
        if par_m is None or par_m <= 0:
            continue

        bonds.append(BondSeries(
            series_label=label,
            coupon_pct=coupon_pct,
            par_amount_m=par_m,
            maturity_date=maturity,
        ))
        seen_labels.add(label)

    # Total long-term debt outstanding: the FIRST "Long-Term Debt, Gross"
    # in the table (typically the rollup row at the top: "$ 5,805 $ 5,919")
    total_m = None
    tm = re.search(r"Long.?Term Debt,?\s*Gross\s*\$?\s*([\d,]+)", plain, re.IGNORECASE)
    if tm:
        try:
            total_m = float(tm.group(1).replace(",", ""))
        except ValueError:
            pass

    # Other long-term debt: the slice AFTER 'Other Long Term Debt' header
    other_m = None
    if other_m_match:
        other_slice = plain[other_m_match.end():other_m_match.end() + 400]
        om = re.search(r"Long.?Term Debt,?\s*Gross\s*\$?\s*([\d,]+)",
                        other_slice, re.IGNORECASE)
        if om:
            try:
                other_m = float(om.group(1).replace(",", ""))
            except ValueError:
                pass

    return bonds, other_m, total_m


# --------------------------------------------------------------------------
# Best-effort scan of redemption / call language from the main Debt note
# --------------------------------------------------------------------------

_CALL_CONTEXT_PATTERNS = [
    (re.compile(r"make.?whole\s+(?:premium|amount|provision)", re.I),
     "make_whole"),
    (re.compile(r"redeemable\s+at\s+(?:our|the\s+Company['']s)\s+option", re.I),
     "continuous"),
    (re.compile(r"on\s+or\s+after\s+([A-Z][a-z]+\s+\d+,?\s+\d{4})", re.I),
     "fixed_schedule"),
]


def _scan_redemption_language(accession_dir: str, *, verbose: bool = False) -> dict[str, dict]:
    """Return {bond_label_substring: {is_callable, call_type, terms_excerpt}}
    by scanning the main Debt note (R13.htm-equivalent) for redemption
    clauses. Best-effort; if patterns don't match, callable defaults to
    True for senior notes (most are), call_type empty."""
    summary_url = f"{accession_dir}/FilingSummary.xml"
    out: dict[str, dict] = {}
    try:
        r = httpx.get(summary_url, headers=SEC_HEADERS, timeout=30,
                      follow_redirects=True)
        if r.status_code != 200:
            return out
        body = r.text
    except Exception:
        return out

    # Find the main Debt note (not the Tables / Details). Pattern: ShortName
    # exactly "Debt" or "Long-Term Debt" or "Senior Notes" without "Details"
    matches = re.findall(
        r"<Report[^>]*>.*?<ShortName>([^<]+)</ShortName>.*?<HtmlFileName>([^<]+\.htm)</HtmlFileName>",
        body, re.DOTALL,
    )
    debt_note_html = None
    for short_name, html_name in matches:
        sn = short_name.strip().lower()
        if sn in ("debt", "long-term debt", "long term debt", "senior notes"):
            debt_note_html = html_name
            break

    if not debt_note_html:
        return out

    try:
        r = httpx.get(f"{accession_dir}/{debt_note_html}",
                      headers=SEC_HEADERS, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            return out
    except Exception:
        return out

    import html as _html_mod
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = _html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    # Heuristic: scan for the global redemption mention. Most issuers have a
    # blanket redemption clause covering all senior notes ("The notes may be
    # redeemed at our option, in whole or in part, at any time, at a make-
    # whole premium..."). We extract that and apply to all senior notes.
    blanket_call_type = ""
    blanket_terms = ""
    for pat, ctype in _CALL_CONTEXT_PATTERNS:
        m = pat.search(text)
        if m:
            blanket_call_type = ctype
            start = max(0, m.start() - 80)
            blanket_terms = text[start:m.end() + 220].strip()
            break

    if blanket_call_type:
        out["__blanket__"] = {
            "is_callable": True,
            "call_type": blanket_call_type,
            "redemption_terms": blanket_terms,
        }

    return out


# --------------------------------------------------------------------------
# Top-level fetch
# --------------------------------------------------------------------------

def fetch_debt_schedule(
    ticker: str,
    *,
    verbose: bool = False,
) -> DebtScheduleBundle:
    """Public API. Returns a DebtScheduleBundle with parsed bond series."""
    bundle = DebtScheduleBundle(
        ticker=ticker.upper(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    resolved = resolve_ticker_to_cik(ticker, verbose=verbose)
    if not resolved:
        bundle.error = f"could not resolve {ticker} -> CIK"
        return bundle
    cik, name = resolved
    bundle.issuer_cik = cik
    bundle.issuer_name = name

    latest_10k = _find_latest_10k(cik, verbose=verbose)
    if not latest_10k:
        bundle.error = "no 10-K found"
        return bundle
    bundle.most_recent_10k_accession = latest_10k["accession_number"]
    bundle.most_recent_10k_filed_date = latest_10k["filed_date"]

    debt_reports = _find_debt_report_htmls(latest_10k["accession_dir"], verbose=verbose)
    if not debt_reports:
        bundle.error = "no long-term-debt schedule report found in filing"
        return bundle

    all_bonds: list[BondSeries] = []
    seen_labels: set[str] = set()
    other_m: float | None = None
    total_m: float | None = None
    last_err: str = ""

    for debt_html_url, short_name, kind in debt_reports:
        try:
            r = httpx.get(debt_html_url, headers=SEC_HEADERS, timeout=30,
                          follow_redirects=True)
            if r.status_code != 200:
                last_err = f"{short_name}: HTTP {r.status_code}"
                continue
        except Exception as e:
            last_err = f"{short_name}: {type(e).__name__}: {e}"
            continue
        bonds_i, other_i, total_i = _parse_debt_schedule_html(r.text)
        # Tag convertibles in the label for downstream clarity
        if kind == "convertible":
            for b in bonds_i:
                if "convertible" not in b.series_label.lower():
                    b.series_label = b.series_label + " (Convertible)"
        for b in bonds_i:
            if b.series_label in seen_labels:
                continue
            all_bonds.append(b)
            seen_labels.add(b.series_label)
        # Take the FIRST encountered total/other (typically from the senior
        # schedule, which is the primary aggregate)
        if total_m is None and total_i is not None:
            total_m = total_i
        if other_m is None and other_i is not None:
            other_m = other_i

    bundle.bonds = all_bonds
    bundle.other_long_term_debt_m = other_m
    bundle.total_long_term_debt_m = total_m
    if not all_bonds and last_err:
        bundle.error = last_err

    # Best-effort redemption-language scan
    redemption_map = _scan_redemption_language(latest_10k["accession_dir"], verbose=verbose)
    blanket = redemption_map.get("__blanket__")
    if blanket:
        for b in bundle.bonds:
            # Apply blanket redemption to senior notes (most issuer notes
            # are callable under a make-whole or after-date provision)
            if "senior" in b.series_label.lower() or "notes" in b.series_label.lower():
                b.is_callable = blanket["is_callable"]
                b.call_type = blanket["call_type"]
                b.redemption_terms = blanket["redemption_terms"]

    if verbose:
        print(f"  Debt loader: {len(bundle.bonds)} bond series, "
              f"total ${bundle.total_long_term_debt_m or 0:,.0f}M")

    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Fetch issuer's bond schedule from latest 10-K")
    p.add_argument("ticker")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    bundle = fetch_debt_schedule(args.ticker, verbose=True)
    if args.json:
        print(json.dumps(bundle.to_dict(), indent=2, default=str))
    else:
        print()
        print(bundle.to_prompt_text() or "(no debt schedule data)")
        if bundle.error:
            print(f"ERROR: {bundle.error}")


if __name__ == "__main__":
    _main()
