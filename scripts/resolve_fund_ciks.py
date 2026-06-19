"""
Name -> CIK resolver for SEC 13F filers.

Two-pass approach:
  1. SEC's company browse (action=getcompany&company=<name>&type=13F-HR)
     returns HTML with a list of matching filers and CIKs.
  2. For each candidate, GET /submissions/CIK<cik>.json and confirm
     the entity name contains the query term AND they actually file 13F-HR.

Usage:
    python scripts/resolve_fund_ciks.py "Pershing Square" "BlackRock Inc" ...
    python scripts/resolve_fund_ciks.py --batch path/to/names.json
"""
import json
import re
import sys
import time
from urllib.parse import quote_plus

import httpx

BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "text/html,application/json",
}


def browse_filers(name: str) -> list[dict]:
    """Return list of (cik, name) from EDGAR company browse search."""
    try:
        r = httpx.get(
            BROWSE,
            params={
                "action": "getcompany", "company": name, "type": "13F-HR",
                "dateb": "", "owner": "include", "count": 40,
            },
            headers=HEADERS, timeout=20, follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]
    # SEC HTML pattern (ampersands are HTML-encoded as &amp;):
    #   ...CIK=0001336528...">0001336528</a></td>
    #   <td scope="row">Pershing Square Capital Management, L.P.</td>
    rows: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'CIK=(\d{10})[^"]*"[^>]*>\d{10}</a></td>\s*<td[^>]*>([^<]+)</td>',
        r.text,
    ):
        cik, ent_name = m.group(1), m.group(2).strip()
        if cik in seen:
            continue
        seen.add(cik)
        rows.append({"cik": cik, "name": ent_name})
    return rows[:10]


def confirm_cik(cik: str) -> dict | None:
    """Hit SEC submissions endpoint, return name + count of 13F-HR forms."""
    try:
        r = httpx.get(
            SUBMISSIONS.format(cik=cik),
            headers={**HEADERS, "Accept": "application/json"},
            timeout=20, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None
    forms = d.get("filings", {}).get("recent", {}).get("form", []) or []
    n_13f = sum(1 for f in forms if f in ("13F-HR", "13F-HR/A"))
    return {
        "cik": cik,
        "name": d.get("name", ""),
        "n_13f_hr": n_13f,
        "entity_type": d.get("entityType", ""),
    }


def best_match(name: str) -> dict | None:
    """Return the best candidate or None. Picks the one whose name contains
    the query AND has the most recent 13F-HR filings."""
    cands = browse_filers(name)
    if cands and "error" in cands[0]:
        return {"query": name, "error": cands[0]["error"]}
    if not cands:
        return {"query": name, "error": "no candidates from browse"}

    confirmed: list[dict] = []
    name_lower = name.lower()
    for c in cands[:8]:
        info = confirm_cik(c["cik"])
        if info and info["n_13f_hr"] > 0:
            # Prefer entities whose own name contains the query terms
            score = sum(1 for tok in name_lower.split() if tok in info["name"].lower())
            confirmed.append({**info, "score": score})
        time.sleep(0.15)
    if not confirmed:
        return {"query": name, "error": "no confirmed 13F-HR filer"}
    confirmed.sort(key=lambda x: (-x["score"], -x["n_13f_hr"]))
    top = confirmed[0]
    return {
        "query": name,
        "cik": top["cik"], "name": top["name"],
        "n_13f_hr": top["n_13f_hr"],
        "entity_type": top["entity_type"],
        "score": top["score"],
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    if sys.argv[1] == "--batch":
        with open(sys.argv[2]) as f:
            names = json.load(f)
    else:
        names = sys.argv[1:]
    results = []
    for n in names:
        print(f"\n== {n!r} ==")
        m = best_match(n)
        if m and "cik" in m:
            print(f"  cik={m['cik']:<10} name={m['name']}")
            print(f"  13F-HR filings: {m['n_13f_hr']}, score={m['score']}, type={m['entity_type']}")
        else:
            print(f"  NO MATCH: {m.get('error','?') if m else 'none'}")
        results.append(m)
        time.sleep(0.3)
    print()
    print("=== JSON output (paste into fund_universe.json after review) ===")
    out = []
    for r in results:
        if r and "cik" in r:
            out.append({"cik": r["cik"], "name": r["name"], "type": "hedge_fund"})
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
