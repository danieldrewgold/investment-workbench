"""
Workforce / restructuring signal — flags when a company is cutting a lot of
people, ideally before the market has fully digested it. Edge-seeking: a large
workforce reduction often precedes the margin / demand read the Street is still
modeling at trend.

Two sources, most-authoritative first:

  1. SEC 8-K **Item 2.05** ("Costs Associated with Exit or Disposal Activities")
     — the ticker-precise, mandatory disclosure of a material restructuring /
     workforce reduction. Free (EDGAR submissions + the 8-K body). This is the
     reliable core.

  2. **WARN Act** notices (state filings, ~60 days AHEAD of a mass layoff) — a
     best-effort, name-matched lookup. Catches facility-level cuts that may not
     be individually 8-K-material but aggregate into a real retrenchment. Best-
     effort because coverage is fragmented across ~50 state agencies and the
     match is employer-name fuzzy (no ticker on a WARN filing).

`build_workforce_signal(ticker, name)` returns a structured signal that is EMPTY
(has_signal=False, corpus_text="") when there's nothing meaningful — so the brief
and dashboard stay SILENT unless there's a real layoff story. Per Daniel: don't
mention it if it isn't meaningful in that scenario.
"""

from __future__ import annotations

import html
import re

import httpx

from ingestion.loaders._edgar_utils import (
    EDGAR_ARCHIVE_BASE, SEC_HEADERS, resolve_cik,
)

# How far back a restructuring 8-K still counts as a live signal.
_LOOKBACK_MONTHS = 24

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_ITEM_RE = re.compile(r"item\s+\d\.\d\d", re.I)

# Headcount: "approximately 15,000 positions / employees / jobs / roles".
_HEADCOUNT_RE = re.compile(
    r"(?:approximately|about|~|reduce[d]?(?:\s+\w+){0,4}?\s+by|eliminat\w+(?:\s+\w+){0,3}?)\s*"
    r"([\d][\d,]{2,})\s+(?:positions|employees|jobs|roles|workers|of\s+its)",
    re.I,
)
# "X% of its (global) workforce / employees / headcount".
_PCT_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*%\s+of\s+(?:its|our|the|their|global|total|worldwide\s+)*"
    r"(?:global\s+|total\s+|worldwide\s+)*(?:workforce|employees|headcount|staff)",
    re.I,
)
# Restructuring / severance charge, $X million|billion.
_CHARGE_RE = re.compile(r"\$\s?([\d][\d,]*(?:\.\d+)?)\s*(million|billion)", re.I)

_WORKFORCE_KW = re.compile(
    r"workforce reduction|reduction in force|reducing\s+(?:its\s+)?(?:global\s+)?workforce|"
    r"headcount reduction|reduce[d]?\s+(?:its\s+)?headcount|layoff|laid off|lay off|"
    r"severance|reduction of\s+(?:approximately\s+)?[\d,]+\s+(?:positions|employees|roles)|"
    r"restructuring|workforce restructuring|eliminate[d]?\s+(?:approximately\s+)?[\d,]+",
    re.I,
)


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = html.unescape(raw)
    return _WS_RE.sub(" ", raw).strip()


def _submissions(cik: str) -> dict:
    r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                  headers=SEC_HEADERS, timeout=20.0)
    return r.json() if r.status_code == 200 else {}


def _recent_2_05(cik: str, *, lookback_months: int = _LOOKBACK_MONTHS) -> list[dict]:
    """8-K filings tagged Item 2.05 within the lookback window, newest first."""
    data = _submissions(cik)
    rec = data.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    out = []
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=int(lookback_months * 30.4))).isoformat()
    for i, f in enumerate(forms):
        if not f.startswith("8-K"):
            continue
        items = rec.get("items", [])[i] if i < len(rec.get("items", [])) else ""
        if "2.05" not in (items or ""):
            continue
        fd = rec.get("filingDate", [])[i] if i < len(rec.get("filingDate", [])) else ""
        if fd < cutoff:
            continue
        out.append({
            "filing_date": fd,
            "accession": rec.get("accessionNumber", [])[i],
            "primary_doc": rec.get("primaryDocument", [])[i],
            "items": items,
        })
    return out


def _item_205_text(body: str) -> str:
    """Slice the Item 2.05 narrative out of the 8-K body text."""
    m = re.search(r"item\s+2\.05[^a-z0-9]", body, re.I)
    if not m:
        return ""
    tail = body[m.end():]
    nxt = _ITEM_RE.search(tail)
    end = nxt.start() if nxt else len(tail)
    end = min(end, 4000)
    return tail[:end].strip()


def _extract(text: str) -> dict:
    """Pull headcount / %-of-workforce / charge / a short snippet."""
    headcount = None
    mh = _HEADCOUNT_RE.search(text)
    if mh:
        try:
            headcount = int(mh.group(1).replace(",", ""))
        except ValueError:
            pass
    pct = None
    mp = _PCT_RE.search(text)
    if mp:
        try:
            pct = float(mp.group(1))
        except ValueError:
            pass
    charge_m = None
    mc = _CHARGE_RE.search(text)
    if mc:
        try:
            val = float(mc.group(1).replace(",", ""))
            charge_m = val * (1000 if mc.group(2).lower() == "billion" else 1)
        except ValueError:
            pass
    # A readable snippet: the first sentence mentioning a workforce action.
    snippet = ""
    kw = _WORKFORCE_KW.search(text)
    if kw:
        s = max(0, kw.start() - 120)
        snippet = text[s:kw.start() + 200].strip()
        snippet = snippet[snippet.find(" ") + 1:] if s > 0 else snippet
    return {"headcount": headcount, "pct_of_workforce": pct,
            "charge_usd_m": charge_m, "snippet": snippet}


def scan_restructuring_8ks(ticker: str, *, verbose: bool = False) -> list[dict]:
    """Item 2.05 restructuring events for the ticker, newest first. Each event
    carries the disclosed headcount / %-of-workforce / charge when present."""
    cik = resolve_cik(ticker, verbose=verbose)
    if not cik:
        return []
    cik_stripped = cik.lstrip("0")
    events = []
    for f in _recent_2_05(cik):
        acc = f["accession"].replace("-", "")
        url = f"{EDGAR_ARCHIVE_BASE}/{cik_stripped}/{acc}/{f['primary_doc']}"
        try:
            r = httpx.get(url, headers=SEC_HEADERS, timeout=25.0)
            body = _strip_html(r.text) if r.status_code == 200 else ""
        except Exception:
            body = ""
        sect = _item_205_text(body)
        ex = _extract(sect or body)
        events.append({
            "source": "8-K Item 2.05", "filing_date": f["filing_date"],
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/{f['primary_doc']}",
            **ex,
        })
        if verbose:
            print(f"  [2.05] {f['filing_date']} headcount={ex['headcount']} "
                  f"pct={ex['pct_of_workforce']} charge=${ex['charge_usd_m']}M")
    return events


# --------------------------------------------------------------------------
# WARN Act notices — best-effort, name-matched. Coverage is fragmented; this is
# a prototype layer that degrades gracefully to [] when a source is unreachable
# or warn-scraper isn't installed.
# --------------------------------------------------------------------------

_LEGAL_RE = re.compile(
    r"\b(?:inc|incorporated|corp|corporation|co|company|companies|llc|l\.l\.c|"
    r"lp|l\.p|ltd|limited|holdings?|group|plc|sa|nv|ag|the|usa|us|na|n\.a|"
    r"international|intl|technologies|technology|systems|industries|enterprises)\b\.?",
    re.I,
)


def _brand_tokens(name: str) -> list[str]:
    """Distinctive (non-generic) tokens of a company name, longest-meaningful
    first — the first is treated as the brand for matching."""
    n = _LEGAL_RE.sub(" ", name.lower())
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return [t for t in n.split() if len(t) >= 4]


def fetch_warn_notices(company_name: str, *, verbose: bool = False,
                       max_results: int = 10) -> list[dict]:
    """WARN-notice lookup by employer name against the cached state dataset
    (currently California). Conservative: matches the company's brand token as a
    WHOLE WORD in the WARN employer name. Returns [] when nothing matches or the
    dataset is unavailable. Employer-name matching is inherently fuzzy (no ticker
    on a WARN filing) — callers should treat hits as a lead to verify."""
    if not company_name:
        return []
    try:
        from ingestion.loaders.warn_loader import fetch_warn_dataset
        data = fetch_warn_dataset(verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"  [WARN] dataset unavailable: {type(e).__name__}: {e}")
        return []
    toks = _brand_tokens(company_name)
    if not toks or not data:
        return []
    brand = toks[0]
    if len(brand) < 4:
        return []
    pat = re.compile(r"\b" + re.escape(brand) + r"\b", re.I)
    hits = [r for r in data if pat.search(r.get("company", ""))]
    hits.sort(key=lambda r: r.get("notice_date", ""), reverse=True)
    if verbose and hits:
        print(f"  [WARN] {len(hits)} notice(s) matched on '{brand}'")
    return hits[:max_results]


def _fmt_event(e: dict) -> str:
    bits = []
    if e.get("headcount"):
        bits.append(f"{e['headcount']:,} positions")
    if e.get("pct_of_workforce"):
        bits.append(f"{e['pct_of_workforce']:g}% of workforce")
    if e.get("charge_usd_m"):
        c = e["charge_usd_m"]
        bits.append(f"${c/1000:.1f}B charge" if c >= 1000 else f"${c:.0f}M charge")
    head = f"{e['filing_date']} — {e['source']}"
    if bits:
        head += ": " + ", ".join(bits)
    snip = e.get("snippet") or ""
    return head + (f"\n    \"{snip[:240]}\"" if snip else "")


def build_workforce_signal(ticker: str, name: str | None = None,
                           *, verbose: bool = False) -> dict:
    """Combined workforce/restructuring signal. EMPTY (has_signal=False,
    corpus_text="") when there is nothing meaningful — callers should render
    nothing in that case."""
    events = scan_restructuring_8ks(ticker, verbose=verbose)
    warn = fetch_warn_notices(name or "", verbose=verbose)
    has_signal = bool(events or warn)
    if not has_signal:
        return {"ticker": ticker.upper(), "has_signal": False, "events": [],
                "warn": [], "corpus_text": "", "summary": ""}
    # One-line headline summary — prefer the 8-K (authoritative); fall back to WARN.
    latest = events[0] if events else None
    summary = ""
    if latest:
        n = latest.get("headcount")
        p = latest.get("pct_of_workforce")
        mag = (f"~{n:,} positions" if n else (f"~{p:g}% of workforce" if p else "a restructuring"))
        summary = f"{ticker.upper()} disclosed {mag} ({latest['filing_date']}, 8-K Item 2.05)"
    elif warn:
        tot = sum(w.get("employees") or 0 for w in warn)
        sts = ", ".join(sorted({w.get("state", "") for w in warn if w.get("state")}))
        summary = (f"{ticker.upper()}: {len(warn)} WARN mass-layoff notice(s) "
                   f"(~{tot:,} employees, {sts}) — employer-name matched, verify")
    lines = [f"=== WORKFORCE / RESTRUCTURING SIGNAL ({ticker.upper()}) ==="]
    if events:
        lines.append(f"{len(events)} restructuring 8-K(s) (Item 2.05) in the last "
                     f"{_LOOKBACK_MONTHS} months — material workforce/exit actions:")
        lines += ["  " + _fmt_event(e) for e in events]
    if warn:
        tot = sum(w.get("employees") or 0 for w in warn)
        lines.append(f"{len(warn)} WARN notice(s) matched by employer name (~{tot:,} "
                     f"employees; state filings ~60d ahead of the layoff — VERIFY the "
                     f"employer is this issuer, not a namesake):")
        for w in warn[:12]:
            lines.append(f"  {w.get('notice_date','?')} [{w.get('state','')}] "
                         f"{w.get('company','?')}: {w.get('employees','?')} employees "
                         f"— {w.get('kind','')} ({w.get('site','')})")
    lines.append("=" * 56)
    return {"ticker": ticker.upper(), "has_signal": True, "events": events,
            "warn": warn, "corpus_text": "\n".join(lines), "summary": summary}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t = sys.argv[1] if len(sys.argv) > 1 else "INTC"
    sig = build_workforce_signal(t, verbose=True)
    print(f"\nhas_signal={sig['has_signal']}  summary={sig['summary']!r}")
    print(sig["corpus_text"])
