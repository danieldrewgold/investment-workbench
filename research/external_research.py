"""
External research access layer.

Makes the classified inbox research (newsletters / Substacks, produced by
ingestion/loaders/email_research_classifier) fetchable + digestible PER TICKER,
so it can feed the per-ticker brief and the edge layer.

  research_for_ticker(T)  -> classified records mentioning T, best author first
  corpus_for_ticker(T)    -> a labeled text block for the brief's corpus
  universe_coverage()     -> {ticker: n_records} across all classified research
"""

from __future__ import annotations

import glob
import json
import re
from collections import defaultdict
from pathlib import Path

CLASSIFIED_DIR = Path("data/email_research/classified")
RESULTS_DIR = Path("data/results")


def _clip(x, n=320):
    s = str(x or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def load_our_view(ticker: str) -> str:
    """Compact summary of the workbench's OWN view on a ticker (latest result),
    so the edge synth can corroborate/refute external claims against it."""
    t = (ticker or "").upper().strip()
    files = sorted(glob.glob(str(RESULTS_DIR / f"{t}_*.json")))
    if not files:
        return ""
    try:
        d = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    except Exception:
        return ""
    ea = d.get("edge_assessment") or {}
    val = d.get("valuation") or {}
    pi = ea.get("priced_in") or {}
    L = [f"OUR WORKBENCH VIEW on {t} (model run {Path(files[-1]).stem.split('_', 1)[-1]}):"]
    if d.get("edge_hypothesis"):
        L.append(f"- edge hypothesis: {_clip(d['edge_hypothesis'])}")
    if d.get("why_market_is_wrong"):
        L.append(f"- why market is wrong (our take): {_clip(d['why_market_is_wrong'])}")
    if d.get("key_debate"):
        L.append(f"- key debate: {_clip(d['key_debate'])}")
    if ea.get("verdict"):
        L.append(f"- edge verdict: {ea['verdict']} (actionability {ea.get('actionability_score')}, "
                 f"variant {ea.get('variant_pct')}%)")
    if ea.get("edge_narrative"):
        L.append(f"- edge narrative: {_clip(ea['edge_narrative'])}")
    if d.get("post_eps") is not None and d.get("consensus_eps") is not None:
        try:
            L.append(f"- our EPS {round(d['post_eps'], 2)} vs consensus {round(d['consensus_eps'], 2)}")
        except Exception:
            pass
    if val.get("upside_pct") is not None:
        L.append(f"- our valuation: {val.get('upside_pct')}% upside (implied {val.get('implied_price')}, "
                 f"{val.get('applied_multiple')}x)")
    if pi.get("reasoning"):
        L.append(f"- priced-in read: {_clip(pi['reasoning'])}")
    return "\n".join(L)


def load_classified(dedupe: bool = True) -> list[dict]:
    """Load classified research records. By default DEDUPES across accounts —
    the same article delivered to two inboxes (e.g. a Gaetano Substack to both
    a primary and a secondary inbox) has distinct message ids but identical
    subject+date, so without this it would double-count per-ticker coverage."""
    raw = []
    for f in glob.glob(str(CLASSIFIED_DIR / "*.json")):
        try:
            raw.append(json.loads(Path(f).read_text(encoding="utf-8")))
        except Exception:
            continue
    if not dedupe:
        return raw
    best: dict = {}
    keyless = []
    for r in raw:
        subj = re.sub(r"\s+", " ", (r.get("subject") or "").strip().lower())[:80]
        if not subj:
            keyless.append(r)
            continue
        key = (subj, str(r.get("date", ""))[:10])
        prev = best.get(key)
        # On collision keep the more complete classification (most tickers).
        if prev is None or len(r.get("tickers") or []) > len(prev.get("tickers") or []):
            best[key] = r
    return list(best.values()) + keyless


def research_for_ticker(ticker: str, *, records: list[dict] | None = None,
                        min_tier: int = 3) -> list[dict]:
    """Research records that name `ticker`, credible-first.

    Sorted by effective_tier (1 = best) then date desc. `min_tier` keeps only
    records at least that credible (lower number = stricter).
    """
    t = (ticker or "").upper().strip()
    recs = records if records is not None else load_classified()
    hits = [
        r for r in recs
        if r.get("is_research")
        and r.get("effective_tier", 3) <= min_tier
        and (t == (r.get("primary_ticker") or "").upper()
             or t in [str(x).upper() for x in (r.get("tickers") or [])])
    ]
    hits.sort(key=lambda r: (r.get("effective_tier", 3), str(r.get("date", ""))[:10]),
              reverse=False)
    hits.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    hits.sort(key=lambda r: r.get("effective_tier", 3))
    return hits


def _record_block(r: dict) -> str:
    a = r.get("author", {}) or {}
    head = (f"[{str(r.get('date',''))[:10]}] {a.get('name', r.get('sender',''))} "
            f"(tier {r.get('effective_tier','?')}) — {r.get('stance','')} on "
            f"{r.get('primary_ticker') or ','.join(r.get('tickers') or [])}")
    lines = [head, f"  Subject: {r.get('subject','')}"]
    if r.get("thesis"):
        lines.append(f"  Thesis: {r['thesis']}")
    for k in (r.get("key_points") or [])[:5]:
        lines.append(f"  - {k}")
    for c in (r.get("notable_claims") or [])[:3]:
        lines.append(f"  * claim: {c}")
    if r.get("catalysts"):
        lines.append(f"  Catalysts: {'; '.join(r['catalysts'][:3])}")
    return "\n".join(lines)


def corpus_for_ticker(ticker: str, *, records: list[dict] | None = None,
                      max_items: int = 6, min_tier: int = 3) -> str:
    """Labeled corpus block of external research on `ticker` for the brief."""
    hits = research_for_ticker(ticker, records=records, min_tier=min_tier)[:max_items]
    if not hits:
        return ""
    header = (f"=== EXTERNAL RESEARCH on {ticker.upper()} "
              f"({len(hits)} pieces from independent analysts / newsletters; "
              f"tier 1 = highest-conviction source) ===")
    return header + "\n\n" + "\n\n".join(_record_block(r) for r in hits)


def universe_coverage(records: list[dict] | None = None) -> dict:
    recs = records if records is not None else load_classified()
    counts: dict = defaultdict(int)
    for r in recs:
        if not r.get("is_research"):
            continue
        for t in set([str(x).upper() for x in (r.get("tickers") or [])]
                     + ([str(r["primary_ticker"]).upper()] if r.get("primary_ticker") else [])):
            counts[t] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) > 1:
        t = sys.argv[1].upper()
        print(corpus_for_ticker(t) or f"No external research on {t}")
    else:
        cov = universe_coverage()
        print("External-research coverage by ticker:")
        for k, v in list(cov.items())[:25]:
            print(f"  {k:6} {v}")
