"""
EDGAR 13D / 13G Loader

For a given target ticker, fetches all SC 13D / 13D-A / 13G / 13G-A
filings where the company is the SUBJECT (not the filer). These catch
the holders that 13F doesn't:

  - Private equity sponsors (One Rock, Metropoulos, Apollo, etc.)
  - Activist hedge funds with >5% positions
  - Strategic / corporate holders
  - Founder / insider concentrations

13F is a quarterly portfolio disclosure for institutional managers.
13D / 13G are per-position disclosures triggered when ownership crosses
the 5% threshold:
  - SC 13D = active intent (engagement, control, change of board)
  - SC 13G = passive (mutual funds, indexers, qualified institutions)

Approach:
  1. Resolve ticker -> CIK via SEC's company_tickers.json
  2. browse-edgar with type=SC+13 and the target CIK returns all 13D/G
     filings WHERE the company is the subject (filed by holders)
  3. Each filing's index page lists the filer name + filer CIK in
     <div class="companyInfo"> blocks
  4. Persist to filing_13d table

Cache: weekly per-ticker (13D filings drop infrequently — when ownership
crosses 5%, when a holder amends, or when the position is exited).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from core.provenance.database import hash_content, new_id, now_iso, upsert

SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "text/html,application/json",
}

# Cache for ticker -> CIK lookups
_TICKER_CIK_CACHE_PATH = Path("data/ticker_cik_cache.json")


@dataclass
class Filing13D:
    """One SC 13D / 13G filing about a target company."""
    filer_name: str = ""
    filer_cik: str = ""
    target_cik: str = ""
    target_ticker: str = ""
    target_name: str = ""
    form_type: str = ""           # SC 13D, SC 13D/A, SC 13G, SC 13G/A
    accession_number: str = ""
    filed_date: str = ""
    event_date: str = ""           # date triggering filing (often = filed_date)
    shares_held: float | None = None
    pct_of_class: float | None = None
    activist_intent: bool = False
    purpose_excerpt: str = ""
    primary_doc_url: str = ""


@dataclass
class Filing13DBundle:
    """All 13D/G filings for a target ticker."""
    ticker: str = ""
    target_cik: str = ""
    target_name: str = ""
    fetched_at: str = ""
    filings: list = field(default_factory=list)   # list[Filing13D]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "target_cik": self.target_cik,
            "target_name": self.target_name,
            "fetched_at": self.fetched_at,
            "filings": [
                asdict(f) if hasattr(f, "__dataclass_fields__") else f
                for f in self.filings
            ],
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        """Render as a labeled corpus block for the brief prompt."""
        if not self.filings:
            return ""
        # Group by filer to deduplicate amendments — show most recent per filer
        by_filer: dict[str, Filing13D] = {}
        for f in self.filings:
            key = f.filer_cik or f.filer_name
            existing = by_filer.get(key)
            if existing is None or f.filed_date > existing.filed_date:
                by_filer[key] = f
        latest = sorted(by_filer.values(), key=lambda f: f.filed_date, reverse=True)

        lines = [
            f"=== 5%+ HOLDERS (SEC 13D/13G filings on {self.ticker}) ===",
            f"({len(latest)} unique filer(s) across {len(self.filings)} total filings. "
            f"13D = active/control intent; 13G = passive disclosure.)",
            "",
        ]
        for f in latest:
            tag = "[ACTIVIST]" if f.activist_intent else "[PASSIVE] "
            pct_str = (f"{f.pct_of_class:.2f}% of class" if f.pct_of_class is not None
                        else "% not parsed")
            lines.append(
                f"  {tag} {f.filer_name:<40s} "
                f"({f.form_type}, filed {f.filed_date})"
            )
            if f.pct_of_class is not None or f.shares_held is not None:
                detail_bits = []
                if f.shares_held is not None:
                    detail_bits.append(f"{int(f.shares_held):,} shares")
                if f.pct_of_class is not None:
                    detail_bits.append(f"{f.pct_of_class:.2f}% of class")
                lines.append(f"      {' / '.join(detail_bits)}")
            if f.purpose_excerpt:
                lines.append(f"      Purpose: {f.purpose_excerpt[:180]}")
        lines.append("")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Ticker -> CIK resolution
# --------------------------------------------------------------------------

def _load_ticker_cik_cache() -> dict:
    if not _TICKER_CIK_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_TICKER_CIK_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_ticker_cik_cache(cache: dict) -> None:
    try:
        _TICKER_CIK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TICKER_CIK_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def resolve_ticker_to_cik(ticker: str, verbose: bool = False) -> tuple[str, str] | None:
    """
    Returns (cik, company_name) or None. Hits SEC's company_tickers.json
    file (~9MB JSON of ALL listed company tickers + CIKs). Cached locally
    after first fetch.
    """
    cache = _load_ticker_cik_cache()
    cached = cache.get(ticker.upper())
    if cached:
        return cached["cik"], cached["name"]

    try:
        r = httpx.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS, timeout=30, follow_redirects=True,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        if verbose:
            print(f"  Ticker -> CIK lookup failed: {type(e).__name__}: {e}")
        return None

    # Build fresh cache from full payload
    fresh: dict[str, dict] = {}
    for entry in data.values():
        t = (entry.get("ticker") or "").upper()
        if not t:
            continue
        fresh[t] = {
            "cik": str(entry["cik_str"]).zfill(10),
            "name": entry.get("title", ""),
        }
    _save_ticker_cik_cache(fresh)

    hit = fresh.get(ticker.upper())
    if not hit:
        return None
    return hit["cik"], hit["name"]


# --------------------------------------------------------------------------
# Per-target listing: browse-edgar to find SC 13 filings against the target
# --------------------------------------------------------------------------

# Two-step parsing of the SEC search results page. Each filing occupies a
# <tr> block of 5 <td>s: form_type, Documents-link, description+acc-no,
# filed_date, file-number link. Form type and Documents-link cells are
# adjacent so we anchor on those, then look for the YYYY-MM-DD date later
# in the same row.
_ROW_HEAD_RE = re.compile(
    r'<td[^>]*>(SC 13[^<]*?)</td>\s*'
    r'<td[^>]*>\s*<a[^>]*href="'
    r'(/Archives/edgar/data/(\d+)/\d+/([\d-]+)-index\.htm)"',
    re.DOTALL,
)
# Match a date inside its own <td> — accession numbers like
# "0001193125-24-265387" otherwise match a 4-2-2 substring.
_DATE_AFTER_RE = re.compile(r"<td[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</td>")


def _list_13d_filings(target_cik: str, verbose: bool = False) -> list[dict]:
    """Returns [{form_type, accession, filer_url_cik, filed_date, index_url}]
    for all SC 13D / 13D-A / 13G / 13G-A filings on the target."""
    out: list[dict] = []
    cik = target_cik.lstrip("0").zfill(10)
    try:
        r = httpx.get(
            "https://www.sec.gov/cgi-bin/browse-edgar",
            params={
                "action": "getcompany", "CIK": cik, "type": "SC 13",
                "dateb": "", "owner": "include", "count": 40,
            },
            headers=SEC_HEADERS, timeout=30, follow_redirects=True,
        )
        if r.status_code != 200:
            return out
    except Exception:
        return out
    body = r.text
    for m in _ROW_HEAD_RE.finditer(body):
        form_type = m.group(1).strip()
        idx_path = m.group(2)
        url_cik = m.group(3)
        accession = m.group(4)
        # Find the next YYYY-MM-DD date after this match — that's the
        # filing's filed_date column. Search the remainder of the row.
        rest = body[m.end():m.end() + 800]
        d = _DATE_AFTER_RE.search(rest)
        filed_date = d.group(1) if d else ""
        out.append({
            "form_type": form_type,
            "accession_number": accession,
            "filer_url_cik": url_cik,
            "filed_date": filed_date,
            "index_url": "https://www.sec.gov" + idx_path,
        })
    if verbose:
        print(f"  13D loader: found {len(out)} SC 13 filings for CIK {cik}")
    return out


# --------------------------------------------------------------------------
# Per-filing parse: extract filer name + filer CIK + cover-page details
# --------------------------------------------------------------------------

# Filer + Subject info on the index page is in <div class="companyInfo"> blocks
_COMPANY_INFO_RE = re.compile(
    r'<div class="companyInfo">(.*?)</div>', re.DOTALL,
)


def _parse_filing_index(html: str) -> dict:
    """Extract filer + subject from a filing index HTML page."""
    info: dict = {"filer_name": "", "filer_cik": "", "target_name": "", "target_cik": ""}
    for block in _COMPANY_INFO_RE.findall(html):
        # Strip tags, normalize whitespace
        cleaned = re.sub(r"<[^>]+>", " ", block)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Parse "<NAME> (Filed by) CIK : <CIK> ..."
        m_filer = re.match(r"(.+?)\s*\(Filed by\)\s*CIK\s*:\s*(\d+)", cleaned)
        if m_filer:
            info["filer_name"] = m_filer.group(1).strip()
            info["filer_cik"] = m_filer.group(2).zfill(10)
            continue
        m_subject = re.match(r"(.+?)\s*\(Subject\)\s*CIK\s*:\s*(\d+)", cleaned)
        if m_subject:
            info["target_name"] = m_subject.group(1).strip()
            info["target_cik"] = m_subject.group(2).zfill(10)
    return info


# Cover-page text patterns for shares + pct of class.
# Lazy [^%]{0,200}? lets us skip past any "Row (11)" / "Row (13)" reference
# numbers — only the digits IMMEDIATELY before the % count as the percent
# of class. Same trick for shares: skip the row label, then capture the
# next big comma-formatted integer.
_PCT_OF_CLASS_RE = re.compile(
    r"PERCENT\s+OF\s+CLASS[^%]{0,200}?([\d.]+)\s*%",
    re.IGNORECASE,
)
_SHARES_HELD_RE = re.compile(
    r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED[^\n]{0,200}?(\d{1,3}(?:,\d{3})+|\d{4,})",
    re.IGNORECASE,
)


def _parse_primary_doc(html_or_text: str) -> dict:
    """Best-effort parse of position size + intent from the primary doc."""
    import html as _html_mod
    out: dict = {"shares_held": None, "pct_of_class": None,
                  "activist_intent": False, "purpose_excerpt": ""}

    # Strip HTML tags then decode entities. We MUST decode entities BEFORE
    # the digit regex runs — SC 13D cover pages use &#8194; (EN SPACE) as
    # a column separator between "Aggregate Amount Beneficially Owned" and
    # the share count, and an undecoded &#8194; gets captured as the
    # number 8194 by [\d,]+, masking the real value.
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = _html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text)

    m_pct = _PCT_OF_CLASS_RE.search(text)
    if m_pct:
        try:
            out["pct_of_class"] = float(m_pct.group(1))
        except ValueError:
            pass

    m_shares = _SHARES_HELD_RE.search(text)
    if m_shares:
        try:
            out["shares_held"] = float(m_shares.group(1).replace(",", ""))
        except ValueError:
            pass

    # Activist intent — Item 4 of 13D includes "Purpose of Transaction".
    # Look for hot phrases.
    activist_phrases = [
        "engage with management", "board of directors", "change of control",
        "strategic alternatives", "operational improvements",
        "capital allocation", "actively engage",
    ]
    text_lower = text.lower()
    for phrase in activist_phrases:
        if phrase in text_lower:
            out["activist_intent"] = True
            # Capture a short excerpt around the phrase
            idx = text_lower.find(phrase)
            out["purpose_excerpt"] = text[max(0, idx-30):idx+220].strip()
            break

    return out


# --------------------------------------------------------------------------
# Class wrapper for persistence (mirrors Edgar13FLoader pattern)
# --------------------------------------------------------------------------

class Edgar13DLoader:

    def __init__(self, conn=None, run_id: str = ""):
        self.conn = conn
        self.run_id = run_id

    def _persist(self, f: Filing13D) -> None:
        if self.conn is None:
            return
        # Source document
        doc_id = new_id()
        try:
            upsert(self.conn, "source_document", {
                "document_id": doc_id,
                "source_type": "FILING",
                "source_name": "SEC_EDGAR_13D",
                "source_locator": f.primary_doc_url
                                   or f"https://www.sec.gov/Archives/edgar/data/{f.filer_cik.lstrip('0')}/"
                                      f"{f.accession_number.replace('-', '')}/",
                "source_published_at": f.filed_date,
                "fetched_at": now_iso(),
                "content_hash": hash_content(f.accession_number + f.filer_cik),
                "content_summary": f"{f.form_type} by {f.filer_name} on {f.target_ticker}",
                "run_id": self.run_id,
            }, conflict_columns=["source_type", "source_locator"],
            update_columns=["fetched_at", "run_id"])
        except Exception:
            doc_id = ""

        try:
            upsert(self.conn, "filing_13d", {
                "filing_13d_id": new_id(),
                "filer_name": f.filer_name,
                "filer_cik": f.filer_cik,
                "target_cik": f.target_cik,
                "target_ticker": f.target_ticker,
                "target_name": f.target_name,
                "form_type": f.form_type,
                "accession_number": f.accession_number,
                "filed_date": f.filed_date,
                "event_date": f.event_date or f.filed_date,
                "shares_held": f.shares_held,
                "pct_of_class": f.pct_of_class,
                "activist_intent": 1 if f.activist_intent else 0,
                "purpose_excerpt": f.purpose_excerpt,
                "source_document_id": doc_id or None,
                "run_id": self.run_id,
                "created_at": now_iso(),
            }, conflict_columns=["filer_cik", "target_cik", "accession_number"],
            update_columns=[
                "filer_name", "form_type", "filed_date", "event_date",
                "shares_held", "pct_of_class", "activist_intent",
                "purpose_excerpt", "run_id",
            ])
            self.conn.commit()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Public API: fetch all 13D/G filings for a ticker
# --------------------------------------------------------------------------

def fetch_13d_filings(ticker: str, *, target_cik: str | None = None,
                       max_filings: int = 25, verbose: bool = False,
                       parse_primary_doc: bool = True) -> Filing13DBundle:
    """
    Returns all SC 13D/G filings on the target ticker (most recent first).
    Stops after `max_filings` to bound work on heavily-followed names.
    """
    bundle = Filing13DBundle(ticker=ticker.upper(),
                              fetched_at=datetime.now().isoformat(timespec="seconds"))

    # Resolve target CIK if not provided
    if not target_cik:
        resolved = resolve_ticker_to_cik(ticker, verbose=verbose)
        if not resolved:
            bundle.error = f"Could not resolve ticker {ticker} to a CIK"
            if verbose:
                print(f"  13D: {bundle.error}")
            return bundle
        target_cik, target_name = resolved
        bundle.target_cik = target_cik
        bundle.target_name = target_name
    else:
        bundle.target_cik = target_cik.lstrip("0").zfill(10)

    rows = _list_13d_filings(bundle.target_cik, verbose=verbose)
    if not rows:
        return bundle

    # Polite rate limiting — SEC asks for ≤10 req/s sustained
    for row in rows[:max_filings]:
        try:
            r = httpx.get(row["index_url"], headers=SEC_HEADERS,
                          timeout=20, follow_redirects=True)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        info = _parse_filing_index(r.text)

        f = Filing13D(
            filer_name=info["filer_name"] or "(unknown filer)",
            filer_cik=info["filer_cik"] or row["filer_url_cik"].zfill(10),
            target_cik=info["target_cik"] or bundle.target_cik,
            target_name=info["target_name"] or bundle.target_name,
            target_ticker=bundle.ticker,
            form_type=row["form_type"],
            accession_number=row["accession_number"],
            filed_date=row["filed_date"],
            event_date=row["filed_date"],
        )

        # Find the primary doc link in the filing index (usually first .htm
        # entry that isn't the index itself)
        if parse_primary_doc:
            primary_match = re.search(
                r'href="(/Archives/edgar/data/\d+/[\d]+/[^"]*\.htm)"[^>]*>(?!\s*Documents)',
                r.text,
            )
            if primary_match:
                primary_path = primary_match.group(1)
                # Skip the index itself
                if "-index" not in primary_path:
                    primary_url = "https://www.sec.gov" + primary_path
                    try:
                        r2 = httpx.get(primary_url, headers=SEC_HEADERS,
                                       timeout=30, follow_redirects=True)
                        if r2.status_code == 200:
                            cover = _parse_primary_doc(r2.text)
                            f.shares_held = cover["shares_held"]
                            f.pct_of_class = cover["pct_of_class"]
                            f.activist_intent = cover["activist_intent"]
                            f.purpose_excerpt = cover["purpose_excerpt"]
                            f.primary_doc_url = primary_url
                    except Exception:
                        pass

        # Default activist_intent = True for 13D forms (vs 13G passive)
        if not f.activist_intent and "13D" in f.form_type and "13G" not in f.form_type:
            f.activist_intent = True

        bundle.filings.append(f)
        time.sleep(0.15)  # SEC rate limit politeness

    if verbose:
        print(f"  13D: {len(bundle.filings)} filings parsed for {ticker}")
    return bundle
