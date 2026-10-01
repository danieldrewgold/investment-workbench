"""
FEMSA sum-of-the-parts comp tables: break out the Coca-Cola FEMSA (KOF) bottler
business from the OXXO / Proximity convenience-retail business, since FEMSA's
consolidated multiples blend a ~47%-owned Coke bottler with a convenience-retail
crown jewel (plus health/fuel/digital) and are not directly comparable to either.

Two curated peer sets, real multiples via research.peer_comps.build_peer_comps
(yfinance .info — EV/EBITDA, trailing & forward P/E, fwd growth; no LLM):
  1) Coke business  -> subject KOF vs global Coca-Cola bottlers
  2) OXXO business  -> convenience / proximity retail peers (no standalone ticker,
                       so FMX is shown only as a blended reference)

Writes data/dag_cache/FMX/peer_comps_sotp.json for the dashboard. Re-run anytime
(refreshes from yfinance). Usage: python build_femsa_comps.py
"""
from __future__ import annotations

import json
import os
from statistics import median

from research.peer_comps import build_peer_comps

# Coca-Cola bottler peers for KOF (FEMSA's Coke business). KO is the franchisor,
# not a bottler — included as a reference line but EXCLUDED from the peer median.
COKE_SUBJECT = "KOF"
# CCHGY = Coca-Cola HBC US ADR (clean USD) — the London line CCH.L quotes in
# pence, which corrupts the price/per-share display.
COKE_PEERS = ["CCEP", "COKE", "AC.MX", "AKO-B", "CCHGY", "KO"]
COKE_REFERENCE_ONLY = {"KO"}  # franchisor, trades richer than bottlers

# Convenience / proximity retail peers for OXXO. OXXO has no standalone listing,
# so FMX is the (blended) subject reference; the peers define the multiple.
OXXO_SUBJECT = "FMX"
OXXO_PEERS = ["ATD.TO", "SVNDY", "CASY", "MUSA", "WMMVY"]

OUT = "data/dag_cache/FMX/peer_comps_sotp.json"


def _med(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and v == v and v > 0]
    return round(median(vals), 1) if vals else None


def _fmt(v, suf="", pct=False):
    if not isinstance(v, (int, float)) or v != v:
        return "—"
    return (f"{v:+.1f}%" if pct else f"{v:.1f}{suf}")


def _table(pc, subject, reference_only=frozenset()):
    hdr = f"{'Ticker':<8}{'Price':>9}{'EV/EBITDA':>11}{'Trail P/E':>11}{'Fwd P/E':>9}{'Fwd Rev%':>10}{'Fwd EPS%':>10}{'EBITDA gr%':>12}"
    print(hdr)
    print("-" * len(hdr))
    peer_evebitda, peer_tpe, peer_fpe = [], [], []
    for r in pc.rows:
        tag = ""
        if r.ticker == subject.upper():
            tag = "  «subject»"
        elif r.ticker in reference_only:
            tag = "  (ref)"
        else:
            peer_evebitda.append(r.ev_ebitda)
            peer_tpe.append(r.trailing_pe)
            peer_fpe.append(r.fwd_pe)
        price = f"${r.current_price:.2f}" if isinstance(r.current_price, (int, float)) else "—"
        print(f"{r.ticker:<8}{price:>9}{_fmt(r.ev_ebitda,'x'):>11}{_fmt(r.trailing_pe,'x'):>11}"
              f"{_fmt(r.fwd_pe,'x'):>9}{_fmt(r.fwd_rev_growth_pct,pct=True):>10}"
              f"{_fmt(r.fwd_eps_growth_pct,pct=True):>10}{_fmt(r.ebitda_growth_pct,pct=True):>12}{tag}")
    med = {"ev_ebitda": _med(peer_evebitda), "trailing_pe": _med(peer_tpe), "fwd_pe": _med(peer_fpe)}
    print("-" * len(hdr))
    print(f"{'PEER MED':<8}{'':>9}{_fmt(med['ev_ebitda'],'x'):>11}{_fmt(med['trailing_pe'],'x'):>11}{_fmt(med['fwd_pe'],'x'):>9}")
    return med


def main():
    print("\n========== TABLE 1: COKE BUSINESS — Coca-Cola FEMSA (KOF) vs global Coke bottlers ==========\n")
    coke = build_peer_comps(COKE_SUBJECT, COKE_PEERS, schema_label="coke_bottlers", verbose=True)
    print()
    coke_med = _table(coke, COKE_SUBJECT, COKE_REFERENCE_ONLY)

    print("\n\n========== TABLE 2: OXXO BUSINESS — Proximity retail (FMX blended ref) vs convenience peers ==========\n")
    oxxo = build_peer_comps(OXXO_SUBJECT, OXXO_PEERS, schema_label="convenience_retail", verbose=True)
    print()
    oxxo_med = _table(oxxo, OXXO_SUBJECT)

    # FMX FY2026 consensus EPS "growth" is optical, not real: 2025 net income was
    # depressed by a ~Ps 17.7B FX loss on USD-denominated cash plus higher interest,
    # so the low base inflates the YoY (~+108%). Blank it and footnote rather than
    # let a misleading growth figure sit in the table.
    coke_rows = coke.to_dict()["rows"]
    oxxo_rows = oxxo.to_dict()["rows"]
    for r in oxxo_rows:
        if (r.get("ticker") or "").upper() == "FMX":
            r["fwd_eps_growth_pct"] = None
    footnotes = [
        "FMX FY2026 EPS 'growth' is optical: 2025 net income was depressed by a ~Ps 17.7B FX "
        "loss on USD cash + higher interest, so the low base inflates the YoY — not a real growth read.",
        "EV/EBITDA uses REPORTED (income-statement) EBITDA, so it reads ~1-2 turns above sell-side "
        "figures that use adjusted/comparable EBITDA — notably CCEP (~13x reported vs ~11x comparable).",
    ]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = {
        "subject": "FMX",
        "footnotes": footnotes,
        "tables": [
            {"label": "Coca-Cola FEMSA (KOF) — Coke bottlers", "segment": "coke",
             "subject_ticker": "KOF", "reference_only": list(COKE_REFERENCE_ONLY),
             "peer_median": coke_med, "rows": coke_rows, "fetched_at": coke.fetched_at},
            {"label": "OXXO / Proximity — convenience retail", "segment": "oxxo",
             "subject_ticker": "FMX", "reference_only": [],
             "peer_median": oxxo_med, "rows": oxxo_rows, "fetched_at": oxxo.fetched_at},
        ],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n-> wrote {OUT}")


if __name__ == "__main__":
    main()
