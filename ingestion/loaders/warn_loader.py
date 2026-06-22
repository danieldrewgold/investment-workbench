"""
WARN Act notice dataset — best-effort aggregation of state mass-layoff filings,
cached weekly. A WARN notice is filed with the state ~60 days BEFORE a mass
layoff, so it can lead the company's own disclosure / the print.

warn-scraper (Big Local News) is NOT usable here — many of its scrapers require
Xvfb/selenium, which don't run on Windows. So this fetches state feeds DIRECTLY.
Coverage starts with California (by far the largest state by WARN volume, with a
clean machine-readable Excel) and is structured to add more states by appending
to `_SOURCES`. Partial coverage is acceptable — EDGAR 8-K Item 2.05 is the
complete signal for material public-company layoffs; WARN adds facility-level
cuts that may not be individually 8-K-material.

  fetch_warn_dataset(force=False) -> [ {state, company, notice_date,
      effective_date, employees, kind, site, county, source_url}, ... ]
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import httpx

_CACHE = Path("data/warn_cache/warn_notices.json")
_TTL = 7 * 24 * 60 * 60  # weekly
_UA = {"User-Agent": "Mozilla/5.0 (investment-workbench WARN aggregator)"}

_CA_URL = "https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx"


def _fetch_ca() -> list[dict]:
    import openpyxl
    b = httpx.get(_CA_URL, headers=_UA, timeout=60.0, follow_redirects=True).content
    wb = openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    # Header is the first row containing a 'Company' cell.
    hdr_i = next((i for i, r in enumerate(rows)
                  if r and any(str(c).strip() == "Company" for c in r if c)), 1)
    hdr = [str(c).strip().replace("\n", " ") if c else "" for c in rows[hdr_i]]

    def col(name: str):
        for j, h in enumerate(hdr):
            if name.lower() in h.lower():
                return j
        return None

    ci = {k: col(k) for k in ("Company", "Notice", "Effective", "Employees",
                              "Layoff", "Address", "County")}
    if ci["Company"] is None:
        return []
    out = []
    for r in rows[hdr_i + 1:]:
        if not r:
            continue

        def g(k):
            j = ci.get(k)
            return r[j] if (j is not None and j < len(r)) else None

        comp = g("Company")
        if not comp or not str(comp).strip():
            continue
        emp = g("Employees")
        try:
            emp = int(emp) if emp not in (None, "") else None
        except (ValueError, TypeError):
            emp = None
        out.append({
            "state": "CA",
            "company": str(comp).strip(),
            "notice_date": str(g("Notice"))[:10] if g("Notice") else "",
            "effective_date": str(g("Effective"))[:10] if g("Effective") else "",
            "employees": emp,
            "kind": str(g("Layoff") or "").strip(),
            "site": str(g("Address") or "").strip(),
            "county": str(g("County") or "").strip(),
            "source_url": _CA_URL,
        })
    return out


# (state_code, fetch_fn) — extend with more states here.
_SOURCES = [("CA", _fetch_ca)]


def fetch_warn_dataset(*, force: bool = False, verbose: bool = False) -> list[dict]:
    """Aggregated WARN notices across the wired state sources, cached weekly."""
    if not force and _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _TTL:
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    recs: list[dict] = []
    for st, fn in _SOURCES:
        try:
            r = fn()
            recs += r
            if verbose:
                print(f"  [WARN] {st}: {len(r)} notices")
        except Exception as e:
            if verbose:
                print(f"  [WARN] {st} failed: {type(e).__name__}: {e}")
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    return recs


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    d = fetch_warn_dataset(force="--force" in sys.argv, verbose=True)
    print(f"\nWARN dataset: {len(d)} notices ({sum(1 for r in d if r['state']=='CA')} CA)")
    for r in d[:5]:
        print(f"  {r['notice_date']} [{r['state']}] {r['company']}: "
              f"{r['employees']} — {r['kind']}")
