"""
Write a hand-curated peer-comps set for a ticker, overriding the schema picker's
auto-selected peers (which mis-bucket niche names — e.g. SRAD, a sports-betting
data company, gets generic SaaS peers NOW/CRM/DDOG). Writes
data/dag_cache/<T>/peer_comps_curated.json, which the dashboard's peer-comps panel
picks up as the newest peer_comps_* file (cache_steps maps the 'peer_comps_curated'
basename -> step 'peer_comps'). The subject is included as a row.

Multiples are enriched via research.peer_comps.build_peer_comps, so they inherit
the currency-consistent EV/EBITDA, statement-EBITDA, and forwardPE-preferred fixes.

Usage: python build_curated_comps.py SRAD GENI,DKNG,FLUT,EVVTY
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from research.peer_comps import build_peer_comps


def main():
    if len(sys.argv) < 3:
        print("Usage: python build_curated_comps.py <SUBJECT> <PEER1,PEER2,...>")
        sys.exit(1)
    subject = sys.argv[1].upper()
    peers = [p.strip().upper() for p in sys.argv[2].split(",") if p.strip()]

    pc = build_peer_comps(subject, peers, schema_label="curated",
                          include_subject=True, verbose=True)
    d = pc.to_dict()
    out = {
        "corpus_text": pc.to_prompt_text(),   # feeds the brief if re-run
        "schema": "curated",
        "subject": subject,
        "n_peers": len(peers),
        "rows": d["rows"],
        "fetched_at": d["fetched_at"],
    }
    path = os.path.join("data", "dag_cache", subject, "peer_comps_curated.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"step": "peer_comps", "cache_key": "curated",
               "written_at": datetime.now().isoformat(timespec="seconds"), "output": out}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    print()
    print(pc.to_prompt_text())
    print(f"\n-> wrote {path} ({len(out['rows'])} rows incl. subject)")


if __name__ == "__main__":
    main()
