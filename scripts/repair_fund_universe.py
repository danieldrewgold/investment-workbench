"""
Auto-repair fund_universe.json by replacing bad/mislabeled names with the
canonical SEC entity name for each CIK. Drops entries whose CIK doesn't
file 13F-HR (probably wrong CIK entirely).

Reads:  data/fund_universe.json (existing) + scripts/fund_candidates.json (additions)
Writes: data/fund_universe.json (repaired + merged)
Backup: data/fund_universe.original.json
"""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}


def fetch_entity(cik: str) -> dict | None:
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
    return {
        "cik": cik_clean,
        "actual_name": d.get("name", ""),
        "n_13f_hr": n_13f,
        "entity_type": d.get("entityType", ""),
    }


def main():
    repo_root = Path(__file__).resolve().parents[1]
    universe_path = repo_root / "data" / "fund_universe.json"
    candidates_path = repo_root / "scripts" / "fund_candidates.json"
    backup_path = repo_root / "data" / "fund_universe.original.json"

    if not backup_path.exists():
        shutil.copy(universe_path, backup_path)
        print(f"Backed up original universe to {backup_path}")

    existing = json.loads(universe_path.read_text())
    added = json.loads(candidates_path.read_text()) if candidates_path.exists() else []

    seen_ciks: set[str] = set()
    repaired: list[dict] = []
    print(f"Repairing {len(existing)} existing + {len(added)} candidate entries...\n")

    for cand in existing + added:
        cik = cand["cik"].lstrip("0").zfill(10)
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        info = fetch_entity(cik)
        if info is None:
            print(f"  DROP    cik={cik}  '{cand['name']}'  (404)")
            continue
        if info["n_13f_hr"] == 0:
            print(f"  DROP    cik={cik}  '{info['actual_name']}'  (0 13F-HR filings)")
            continue
        sec_name = info["actual_name"]
        # Pick a fund_type: prefer existing label if reasonable, else default
        ftype = cand.get("type", "hedge_fund")
        repaired.append({"cik": cik, "name": sec_name, "type": ftype})
        if sec_name.upper() != cand["name"].upper():
            print(f"  REPAIR  cik={cik}  '{cand['name']}' -> '{sec_name}'")
        else:
            print(f"  OK      cik={cik}  '{sec_name}'")
        time.sleep(0.15)

    print(f"\nRepaired universe: {len(repaired)} entries (dropped {len(seen_ciks) - len(repaired)})")
    universe_path.write_text(json.dumps(repaired, indent=2))
    print(f"Wrote {universe_path}")


if __name__ == "__main__":
    main()
