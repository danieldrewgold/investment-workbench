"""
Verify a list of candidate (CIK, name, type) entries against SEC.

For each candidate, GET /submissions/CIK<cik>.json and check:
  - HTTP 200
  - At least one 13F-HR filing in recent history
  - Returned entity name "looks like" the candidate name (token overlap)

Outputs a JSON ready to merge into data/fund_universe.json.

Usage:
    python scripts/verify_fund_ciks.py path/to/candidates.json
"""
import json
import sys
import time

import httpx

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}


def verify(cik: str, expected_name: str) -> dict | None:
    cik_clean = cik.lstrip("0").zfill(10)
    try:
        r = httpx.get(
            SUBMISSIONS.format(cik=cik_clean),
            headers=HEADERS, timeout=20, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None
    forms = d.get("filings", {}).get("recent", {}).get("form", []) or []
    n_13f = sum(1 for f in forms if f in ("13F-HR", "13F-HR/A"))
    actual_name = d.get("name", "")
    # Token overlap heuristic — drop trivial words
    drop = {"the", "and", "of", "lp", "llc", "ltd", "inc", "co", "corp",
            "company", "corporation", "limited", "lp.", "l.p.", "&", "group",
            "management", "advisors", "fund", "capital", "partners"}
    expected_tokens = {
        t.lower().strip(".,") for t in expected_name.split()
        if t.lower().strip(".,") not in drop
    }
    actual_tokens = {
        t.lower().strip(".,") for t in actual_name.split()
        if t.lower().strip(".,") not in drop
    }
    overlap = len(expected_tokens & actual_tokens)
    return {
        "cik": cik_clean, "actual_name": actual_name,
        "n_13f_hr": n_13f, "entity_type": d.get("entityType", ""),
        "overlap": overlap,
        "expected_tokens": list(expected_tokens),
        "actual_tokens": list(actual_tokens),
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    with open(sys.argv[1]) as f:
        candidates = json.load(f)
    confirmed: list[dict] = []
    rejected: list[dict] = []
    print(f"Verifying {len(candidates)} candidates...\n")
    for cand in candidates:
        cik = cand["cik"]
        name = cand["name"]
        info = verify(cik, name)
        if info is None:
            print(f"  REJECT  cik={cik}  name={name}  (404 or fetch error)")
            rejected.append({**cand, "reason": "404"})
            continue
        if info["n_13f_hr"] == 0:
            print(f"  REJECT  cik={cik}  '{info['actual_name']}'  (no 13F-HR filings)")
            rejected.append({**cand, "reason": "no 13F-HR", "actual": info["actual_name"]})
            continue
        # Require at least one matching token (drops totally-wrong CIKs)
        if info["overlap"] == 0 and info["expected_tokens"]:
            print(f"  REJECT  cik={cik}  '{info['actual_name']}' — name mismatch (expected {name!r})")
            rejected.append({**cand, "reason": "name mismatch", "actual": info["actual_name"]})
            continue
        print(f"  OK      cik={cik}  '{info['actual_name']}'  ({info['n_13f_hr']} 13F-HRs)")
        confirmed.append({
            "cik": cik, "name": info["actual_name"],
            "type": cand.get("type", "hedge_fund"),
        })
        time.sleep(0.15)
    print(f"\nConfirmed: {len(confirmed)} / {len(candidates)}")
    print(f"Rejected:  {len(rejected)}")
    print()
    print("=== Confirmed entries (paste into fund_universe.json) ===")
    print(json.dumps(confirmed, indent=2))


if __name__ == "__main__":
    main()
