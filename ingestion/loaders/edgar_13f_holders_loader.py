"""
EDGAR Reverse-13F Holders Loader (by CUSIP)

The tracked-fund 13F corpus (workbench.db, ~47 funds) is a biased SAMPLE of
who owns a stock — it only sees the funds we chose to follow. This loader
answers the complete question "who are ALL the institutional holders of this
security?" by reverse-indexing the SEC full-text search on the security's
CUSIP.

Pipeline:
  1. efts full-text search:  q="<cusip>" forms=13F-HR  over a recent date
     window. Every 13F-HR information table that lists this CUSIP is a hit,
     so each hit is one manager holding the name. The hit `_id` is
     "<accession>:<infotable_filename>", giving the exact doc to fetch.
  2. Pick the fully-reported quarter (the period_ending shared by the most
     filers) and keep one filing per manager (latest amendment wins).
  3. Fetch each manager's information table, parse the CUSIP row for shares
     + value (reusing Edgar13FLoader.parse_13f_xml), excluding option
     (put/call) lines.
  4. Aggregate fund-family sub-entities (Vanguard files under ~8 CIKs) and
     classify each holder into an ownership bucket (PE/strategic, hedge
     fund, asset manager, index/passive, other institutional).

This is the authoritative free source for "all holders": for PRMB it returns
One Rock (its CURRENT 31% via 13F, not the stale 57.7% on the 2024 13D),
Fidelity/FMR, Sachem Head, Samlyn, Vanguard, BlackRock, etc.

Cache: weekly per-ticker (13F data only changes quarterly).
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import httpx

SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json,text/html",
}
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"


class _RateGate:
    """Global throttle so concurrent fetches stay under SEC's 10 req/s limit.
    Enforces a minimum interval between request STARTS across all threads."""
    def __init__(self, min_interval: float = 0.11):
        self._lock = threading.Lock()
        self._min = min_interval
        self._next = 0.0

    def wait(self):
        # Assign a future slot under the lock, then sleep OUTSIDE it so
        # concurrent workers pipeline (sleeping in-lock serializes everything).
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self._min
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_SEC_GATE = _RateGate(0.11)   # ~9 req/s, polite under SEC's 10/s ceiling


# --------------------------------------------------------------------------
# Holder-type classification
# --------------------------------------------------------------------------
# Ownership buckets. Order here is the canonical display order.
BUCKET_PE = "PE / strategic"
BUCKET_HF = "Hedge fund"
BUCKET_AM = "Asset manager"
BUCKET_INDEX = "Index / passive"
BUCKET_OTHER = "Other institutional"
BUCKET_FLOAT = "Public float / other"
BUCKET_ORDER = [BUCKET_PE, BUCKET_HF, BUCKET_AM, BUCKET_INDEX, BUCKET_OTHER, BUCKET_FLOAT]

# Known fund families: a matched substring maps a raw filer name to a single
# canonical display name AND its bucket. This both de-dupes sub-entities
# (Vanguard files under ~8 CIKs) and classifies the family. Checked in order,
# first match wins — put more specific keys before generic ones.
_FAMILIES: list[tuple[str, str, str]] = [
    # (substring to match in UPPER name, canonical display name, bucket)
    ("VANGUARD", "Vanguard Group", BUCKET_INDEX),
    ("BLACKROCK", "BlackRock", BUCKET_INDEX),
    ("STATE STREET", "State Street", BUCKET_INDEX),
    ("GEODE", "Geode Capital", BUCKET_INDEX),
    ("NORTHERN TRUST", "Northern Trust", BUCKET_INDEX),
    ("CHARLES SCHWAB", "Charles Schwab", BUCKET_INDEX),
    ("SCHWAB", "Charles Schwab", BUCKET_INDEX),
    ("NORGES", "Norges Bank", BUCKET_INDEX),
    ("LEGAL & GENERAL", "Legal & General", BUCKET_INDEX),
    ("DIMENSIONAL", "Dimensional", BUCKET_INDEX),
    ("FMR LLC", "Fidelity (FMR)", BUCKET_AM),
    ("FMR CO", "Fidelity (FMR)", BUCKET_AM),
    ("FIDELITY", "Fidelity (FMR)", BUCKET_AM),
    ("PRICE T ROWE", "T. Rowe Price", BUCKET_AM),
    ("T. ROWE PRICE", "T. Rowe Price", BUCKET_AM),
    ("T ROWE PRICE", "T. Rowe Price", BUCKET_AM),
    ("CAPITAL RESEARCH", "Capital Group", BUCKET_AM),
    ("CAPITAL WORLD", "Capital Group", BUCKET_AM),
    ("CAPITAL INTERNATIONAL", "Capital Group", BUCKET_AM),
    ("WELLINGTON", "Wellington Mgmt", BUCKET_AM),
    ("INVESCO", "Invesco", BUCKET_AM),
    ("FRANKLIN RESOURCES", "Franklin Resources", BUCKET_AM),
    ("NUVEEN", "Nuveen", BUCKET_AM),
    ("AMUNDI", "Amundi", BUCKET_AM),
    ("JANUS", "Janus Henderson", BUCKET_AM),
    ("BAILLIE GIFFORD", "Baillie Gifford", BUCKET_AM),
    ("JENNISON", "Jennison Associates", BUCKET_AM),
    ("SANDS CAPITAL", "Sands Capital", BUCKET_AM),
    ("EDGEWOOD", "Edgewood", BUCKET_AM),
    ("MACQUARIE", "Macquarie", BUCKET_AM),
    ("AMERIPRISE", "Ameriprise", BUCKET_AM),
    ("ALLSPRING", "Allspring", BUCKET_AM),
    ("NOMURA", "Nomura", BUCKET_AM),
    ("WASATCH", "Wasatch Advisors", BUCKET_AM),
    ("FISHER", "Fisher Investments", BUCKET_AM),
    ("LSV ASSET", "LSV Asset Mgmt", BUCKET_AM),
    ("VICTORY CAPITAL", "Victory Capital", BUCKET_AM),
    ("SCHRODER", "Schroders", BUCKET_AM),
    ("MORGAN STANLEY", "Morgan Stanley", BUCKET_AM),
    ("GOLDMAN SACHS", "Goldman Sachs", BUCKET_AM),
    ("JPMORGAN", "JPMorgan", BUCKET_AM),
    ("JP MORGAN", "JPMorgan", BUCKET_AM),
    ("J.P. MORGAN", "JPMorgan", BUCKET_AM),
    ("BANK OF AMERICA", "Bank of America", BUCKET_AM),
    ("WELLS FARGO", "Wells Fargo", BUCKET_AM),
    ("UBS ", "UBS", BUCKET_AM),
    ("DEUTSCHE BANK", "Deutsche Bank", BUCKET_AM),
    ("BNP PARIBAS", "BNP Paribas", BUCKET_AM),
    ("ALLIANCEBERNSTEIN", "AllianceBernstein", BUCKET_AM),
    # Hedge funds (known names)
    ("MILLENNIUM MANAGEMENT", "Millennium", BUCKET_HF),
    ("CITADEL ADVISORS", "Citadel", BUCKET_HF),
    ("POINT72", "Point72", BUCKET_HF),
    ("BALYASNY", "Balyasny", BUCKET_HF),
    ("D. E. SHAW", "D.E. Shaw", BUCKET_HF),
    ("D.E. SHAW", "D.E. Shaw", BUCKET_HF),
    ("TWO SIGMA", "Two Sigma", BUCKET_HF),
    ("AQR CAPITAL", "AQR Capital", BUCKET_HF),
    ("ELLIOTT", "Elliott Mgmt", BUCKET_HF),
    ("PERSHING SQUARE", "Pershing Square", BUCKET_HF),
    ("SACHEM HEAD", "Sachem Head", BUCKET_HF),
    ("SAMLYN", "Samlyn Capital", BUCKET_HF),
    ("GOTHAM ASSET", "Gotham Asset Mgmt", BUCKET_HF),
    ("COATUE", "Coatue", BUCKET_HF),
    ("TIGER GLOBAL", "Tiger Global", BUCKET_HF),
    ("VIKING GLOBAL", "Viking Global", BUCKET_HF),
    ("LONE PINE", "Lone Pine", BUCKET_HF),
    ("WHALE ROCK", "Whale Rock", BUCKET_HF),
    ("LIGHT STREET", "Light Street", BUCKET_HF),
    ("MARSHALL WACE", "Marshall Wace", BUCKET_HF),
    ("HOLOCENE", "Holocene", BUCKET_HF),
    ("KENSICO", "Kensico Capital", BUCKET_HF),
    ("WCM INVESTMENT", "WCM Investment", BUCKET_AM),
    # PE / strategic / control holders
    ("ONE ROCK", "One Rock Capital", BUCKET_PE),
    ("ORCP", "One Rock Capital", BUCKET_PE),
    ("METROPOULOS", "Metropoulos", BUCKET_PE),
    ("APOLLO", "Apollo", BUCKET_PE),
    ("CARLYLE", "Carlyle", BUCKET_PE),
    ("KKR", "KKR", BUCKET_PE),
    ("BLACKSTONE", "Blackstone", BUCKET_PE),
    ("BAIN CAPITAL", "Bain Capital", BUCKET_PE),
    ("CLAYTON DUBILIER", "Clayton Dubilier", BUCKET_PE),
    ("CLAYTON, DUBILIER", "Clayton Dubilier", BUCKET_PE),
    ("ADVENT INTERNATIONAL", "Advent", BUCKET_PE),
    ("LEONARD GREEN", "Leonard Green", BUCKET_PE),
    ("HELLMAN", "Hellman & Friedman", BUCKET_PE),
    ("THOMA BRAVO", "Thoma Bravo", BUCKET_PE),
    ("SILVER LAKE", "Silver Lake", BUCKET_PE),
    ("VISTA EQUITY", "Vista Equity", BUCKET_PE),
    ("WARBURG PINCUS", "Warburg Pincus", BUCKET_PE),
    ("TPG ", "TPG", BUCKET_PE),
]

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|LP|L\.P\.|LLP|INC|INC\.|CORP|CORPORATION|CO|CO\.|"
    r"LTD|LIMITED|PLC|GP|TRUST|COMPANY|MANAGEMENT|MGMT|CAPITAL|ADVISORS?|"
    r"ADVISERS?|PARTNERS?|GROUP|HOLDINGS?|ASSET|INVESTMENTS?)\b",
    re.IGNORECASE,
)


def classify_holder(name: str) -> tuple[str, str]:
    """
    Map a raw 13F filer name to (canonical_display_name, bucket).

    Known families collapse sub-entities to one name. Unknown names are
    classified heuristically by legal structure / keywords and keep a
    title-cased version of their own name.
    """
    up = (name or "").upper()
    for key, disp, bucket in _FAMILIES:
        if key in up:
            return disp, bucket

    # Heuristic fallback for unknown managers. Order matters: boutique
    # "capital" hedge-fund shops first, then clear asset-manager descriptors
    # (so "Schroder Investment Management" / "LSV Asset Management" / "Wasatch
    # Advisors" don't fall through to the generic hedge-fund catch), then the
    # broad LP/capital/partners → hedge fund default.
    disp = _title_case(name)
    if (re.search(r"\b(MASTER FUND|OFFSHORE|HEDGE)\b", up) or
            "CAPITAL MANAGEMENT" in up or "CAPITAL PARTNERS" in up or
            "CAPITAL ADVISORS" in up or "CAPITAL ADVISERS" in up):
        return disp, BUCKET_HF
    if (re.search(r"\b(BANK|TRUST|WEALTH|INSURANCE|ASSURANCE|MUTUAL|RETIREMENT|"
                  r"PENSION|SUPERANNUATION|ENDOWMENT|SECURITIES|FINANCIAL)\b", up) or
            "ASSET MANAGEMENT" in up or "INVESTMENT MANAGEMENT" in up or
            "INVESTMENT MANAGERS" in up or "INVESTMENT COUNSEL" in up or
            "FUND MANAGEMENT" in up or "INVESTMENTS" in up or "INVESTORS" in up or
            "INVESTMENT ADVIS" in up or "GLOBAL ADVIS" in up or
            up.endswith("ADVISORS") or up.endswith("ADVISERS") or
            " ADVISORS " in f" {up} " or " ADVISERS " in f" {up} "):
        return disp, BUCKET_AM
    if re.search(r"\b(L\.?P\.?)\b", up) or "CAPITAL" in up or \
       "MANAGEMENT" in up or "PARTNERS" in up:
        return disp, BUCKET_HF
    return disp, BUCKET_OTHER


# Trailing corporate-form tokens to drop for a tidier legend label.
_TRAIL_SUFFIX_RE = re.compile(
    r"(?:[,\s]+(?:INC|INC\.|LLC|L\.L\.C\.|LP|L\.P\.|LLP|CORP|CORPORATION|"
    r"CO|CO\.|LTD|LTD\.|LIMITED|PLC|GP|N\.?V\.?|S\.?A\.?|TRUST|HOLDINGS?|"
    r"& CO|MANAGEMENT))+\s*$",
    re.IGNORECASE,
)


def _title_case(name: str) -> str:
    """Readable display name from a SHOUTING / suffix-heavy SEC filer name."""
    name = (name or "").strip()
    if not name:
        return "(unknown filer)"
    # Drop trailing legal-form tokens once for a shorter label, but never
    # trim a very short name down to nothing.
    trimmed = _TRAIL_SUFFIX_RE.sub("", name).strip(" ,")
    if len(trimmed) >= 4:
        name = trimmed
    # Already mixed-case (newer filers) — leave it (apart from the trim).
    if name != name.upper():
        return name
    out = []
    for w in name.split():
        if w in ("LLC", "LP", "LLP", "GP", "PLC", "L.P.", "L.L.C.", "USA", "US"):
            out.append(w)
        elif "." in w or "&" in w:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class HolderPosition:
    """One holder's aggregated position in the target security."""
    name: str = ""               # canonical display name (family-collapsed)
    bucket: str = ""             # ownership bucket
    shares: int = 0
    value_usd: float = 0.0
    ciks: list = field(default_factory=list)   # filer CIK(s) merged in
    n_entities: int = 1          # how many filing entities merged


@dataclass
class HoldersBundle:
    ticker: str = ""
    cusip: str = ""
    period_ending: str = ""      # the fully-reported 13F quarter used
    fetched_at: str = ""
    n_managers: int = 0          # unique filing entities fetched for the period
    n_managers_total: int = 0    # unique entities BEFORE the max_holders cap
    efts_total: int = 0          # total efts hits for the CUSIP (filings, incl amendments)
    truncated: bool = False      # True if coverage is materially incomplete
    n_holders: int = 0           # after family collapse
    total_inst_shares: int = 0
    total_inst_value_usd: float = 0.0
    holders: list = field(default_factory=list)   # list[HolderPosition], sorted desc
    error: str = ""
    n_fetch_failed: int = 0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "cusip": self.cusip,
            "period_ending": self.period_ending,
            "fetched_at": self.fetched_at,
            "n_managers": self.n_managers,
            "n_managers_total": self.n_managers_total,
            "efts_total": self.efts_total,
            "truncated": self.truncated,
            "n_holders": self.n_holders,
            "total_inst_shares": self.total_inst_shares,
            "total_inst_value_usd": self.total_inst_value_usd,
            "n_fetch_failed": self.n_fetch_failed,
            "holders": [asdict(h) for h in self.holders],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# efts paging
# --------------------------------------------------------------------------

def _efts_get(params: dict, retries: int = 4) -> dict:
    """One efts request with polite retry. Returns {} on persistent failure."""
    for attempt in range(retries):
        try:
            _SEC_GATE.wait()
            r = httpx.get(EFTS_URL, params=params, headers=SEC_HEADERS, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and "hits" in j:
                    return j
        except Exception:
            pass
        time.sleep(0.6 + 0.4 * attempt)
    return {}


def _recent_window() -> tuple[str, str]:
    """
    (startdt, enddt) covering the most recent COMPLETE 13F quarter's filing
    season. 13F-HR is due 45 days after quarter end; we look back from the
    latest quarter-end that is >=46 days old so we target a fully-filed
    quarter, and run the window to today to catch late amendments.
    """
    today = datetime.now().date()
    # Most recent calendar quarter end.
    q_ends = []
    for y in (today.year, today.year - 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q_ends.append(datetime(y, m, d).date())
    q_ends = sorted([q for q in q_ends if q <= today], reverse=True)
    target = next((q for q in q_ends if (today - q).days >= 46), q_ends[-1])
    start = target + timedelta(days=1)
    return start.isoformat(), today.isoformat()


# efts returns up to 100 hits per request, so we page in steps of 100. (The
# earlier step of 10 only advanced 1/10th as fast, silently capping coverage
# at ~900 unique hits on names with thousands of filers.) efts allows deep
# paging up to its ~10k result window; we stop once we've seen `total`.
_EFTS_PAGE = 100


def _list_13f_filers(cusip: str, *, max_pages: int = 120,
                      verbose: bool = False) -> tuple[list[dict], int | None, bool]:
    """
    Page efts for all 13F-HR info tables that list this CUSIP in the recent
    window. Returns (rows, total_hits, complete) where each row is
      {cik, name, adsh, doc, period_ending, file_date, form}
    and `complete` is False if we couldn't retrieve every hit (paged out).
    """
    startdt, enddt = _recent_window()
    base = {
        "q": f'"{cusip}"',
        "forms": "13F-HR",
        "startdt": startdt,
        "enddt": enddt,
    }
    out: list[dict] = []
    seen_ids: set[str] = set()
    total = None
    for page in range(max_pages):
        j = _efts_get(dict(base, **{"from": page * _EFTS_PAGE}))
        hits = (((j.get("hits") or {}).get("hits")) or [])
        if total is None:
            total = (((j.get("hits") or {}).get("total")) or {}).get("value")
        if not hits:
            break
        new_in_page = 0
        for h in hits:
            _id = h.get("_id") or ""
            if _id in seen_ids:
                continue          # efts can repeat past the result set — stop on no progress
            seen_ids.add(_id)
            new_in_page += 1
            s = h.get("_source") or {}
            doc = _id.split(":", 1)[1] if ":" in _id else ""
            ciks = s.get("ciks") or []
            names = s.get("display_names") or []
            out.append({
                "cik": (ciks[0] if ciks else ""),
                "name": _strip_cik_suffix(names[0] if names else ""),
                "adsh": s.get("adsh") or (_id.split(":", 1)[0] if _id else ""),
                "doc": doc,
                "period_ending": s.get("period_ending") or "",
                "file_date": s.get("file_date") or "",
                "form": s.get("form") or "",
            })
        if new_in_page == 0:
            break                 # page yielded only duplicates — done
        if total is not None and len(seen_ids) >= total:
            break
    complete = (total is None) or (len(seen_ids) >= total)
    if verbose:
        cap = "" if complete else f" INCOMPLETE (paged out at {len(out)})"
        print(f"  reverse-13F: {len(out)} filing hits for CUSIP {cusip} "
              f"(window {startdt}..{enddt}, total={total}){cap}")
    return out, total, complete


_CIK_SUFFIX_RE = re.compile(r"\s*\(CIK\s*\d+\)\s*$", re.IGNORECASE)
_TICKER_SUFFIX_RE = re.compile(r"\s*\([A-Z]{1,6}\)\s*$")


def _strip_cik_suffix(display_name: str) -> str:
    """'Sachem Head Capital Management LP  (CIK 0001582090)' -> name only.
    efts sometimes also appends a ticker like '(BLK)'."""
    n = _CIK_SUFFIX_RE.sub("", display_name or "")
    n = _TICKER_SUFFIX_RE.sub("", n)
    return n.strip()


# --------------------------------------------------------------------------
# Info-table fetch + parse (reuses Edgar13FLoader.parse_13f_xml)
# --------------------------------------------------------------------------

def _position_in_filing(cik: str, adsh: str, doc: str, cusip: str) -> tuple[int, float] | None:
    """Fetch one info table and return (shares, value_usd) for the CUSIP.
    Excludes option (put/call) lines and non-share principal amounts."""
    from ingestion.loaders.edgar_13f_loader import Edgar13FLoader
    if not (cik and adsh):
        return None
    cik_int = str(int(cik)) if str(cik).isdigit() else str(cik).lstrip("0")
    accn = adsh.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/"
    url = base + doc if doc else base
    for attempt in range(3):
        try:
            _SEC_GATE.wait()
            r = httpx.get(url, headers=SEC_HEADERS, timeout=30, follow_redirects=True)
            if r.status_code == 200 and ("infoTable" in r.text or "informationTable" in r.text):
                rows = Edgar13FLoader.parse_13f_xml(r.text)
                shares, value_k = 0, 0
                for h in rows:
                    if h.get("cusip") != cusip:
                        continue
                    if h.get("put_call"):           # option line, not ownership
                        continue
                    if (h.get("sh_prn_type") or "SH") != "SH":  # bond principal
                        continue
                    shares += int(h.get("shares_or_amount") or 0)
                    value_k += int(h.get("value_thousands") or 0)
                return shares, value_k * 1000.0
            # If we got the directory page instead of the doc, find the table.
            if r.status_code == 200 and doc == "":
                m = re.search(r'href="([^"]*(?:infotable|INFOTABLE)[^"]*\.xml)"', r.text, re.I)
                if m:
                    doc = m.group(1).split("/")[-1]
                    continue
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def fetch_all_13f_holders(cusip: str, ticker: str = "", *, max_holders: int = 600,
                           workers: int = 8, verbose: bool = False) -> HoldersBundle:
    """
    Return the complete institutional (13F) holder list for a CUSIP, one
    aggregated position per fund family, classified into ownership buckets.
    """
    bundle = HoldersBundle(ticker=(ticker or "").upper(), cusip=cusip,
                           fetched_at=datetime.now().isoformat(timespec="seconds"))
    if not cusip:
        bundle.error = "no CUSIP provided"
        return bundle

    # Page enough to retrieve every filer up to the cap (100 hits/page).
    max_pages = min(120, max_holders // _EFTS_PAGE + 3)
    hits, efts_total, efts_complete = _list_13f_filers(cusip, max_pages=max_pages, verbose=verbose)
    if not hits:
        bundle.error = "no 13F filers found for CUSIP"
        return bundle

    # Target the fully-reported quarter = the period_ending shared by the
    # most filers (avoids a sparse just-opened quarter).
    from collections import Counter
    period_counts = Counter(h["period_ending"] for h in hits if h["period_ending"])
    if not period_counts:
        bundle.error = "no period_ending on hits"
        return bundle
    target_period = period_counts.most_common(1)[0][0]
    bundle.period_ending = target_period

    # One filing per manager CIK for the target period: latest file_date wins
    # (an amendment supersedes the original).
    by_cik: dict[str, dict] = {}
    for h in hits:
        if h["period_ending"] != target_period:
            continue
        cik = h["cik"]
        if not cik:
            continue
        cur = by_cik.get(cik)
        if cur is None or (h["file_date"] or "") > (cur["file_date"] or ""):
            by_cik[cik] = h
    all_managers = list(by_cik.values())
    bundle.n_managers_total = len(all_managers)
    bundle.efts_total = efts_total or 0
    managers = all_managers[:max_holders]
    bundle.n_managers = len(managers)
    # Flag truncation only when it's MATERIAL: a real max_holders cap, or efts
    # returning materially fewer than all filers (>5% short). A handful of
    # transient page failures (99%+ coverage) isn't worth a warning.
    cap_truncated = bundle.n_managers_total > len(managers)
    efts_short = bool(efts_total) and len(hits) < 0.95 * efts_total
    bundle.truncated = cap_truncated or efts_short
    if verbose:
        bits = []
        if cap_truncated:
            bits.append(f"capped from {bundle.n_managers_total}")
        if not efts_complete:
            bits.append(f"efts {len(hits)}/{efts_total}")
        cap_note = f" ({'; '.join(bits)})" if bits else ""
        print(f"  reverse-13F: {bundle.n_managers} unique managers for {target_period}{cap_note}")

    # Fetch info tables concurrently (polite: small worker pool).
    positions: dict[str, dict] = {}  # family display name -> aggregate
    failed = 0

    def work(m):
        pos = _position_in_filing(m["cik"], m["adsh"], m["doc"], cusip)
        return m, pos

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, m) for m in managers]
        for fut in as_completed(futs):
            m, pos = fut.result()
            if pos is None:
                failed += 1
                continue
            shares, value_usd = pos
            if shares <= 0:
                continue
            disp, bucket = classify_holder(m["name"])
            agg = positions.setdefault(disp, {
                "name": disp, "bucket": bucket, "shares": 0,
                "value_usd": 0.0, "ciks": [], "n_entities": 0,
            })
            agg["shares"] += shares
            agg["value_usd"] += value_usd
            agg["ciks"].append(m["cik"])
            agg["n_entities"] += 1

    bundle.n_fetch_failed = failed
    holders = [
        HolderPosition(name=a["name"], bucket=a["bucket"], shares=a["shares"],
                       value_usd=a["value_usd"], ciks=a["ciks"], n_entities=a["n_entities"])
        for a in positions.values()
    ]
    holders.sort(key=lambda h: -h.shares)
    bundle.holders = holders
    bundle.n_holders = len(holders)
    bundle.total_inst_shares = sum(h.shares for h in holders)
    bundle.total_inst_value_usd = sum(h.value_usd for h in holders)
    if verbose:
        print(f"  reverse-13F: {bundle.n_holders} holders, "
              f"{bundle.total_inst_shares:,} shares, {failed} fetch failures")
    return bundle
