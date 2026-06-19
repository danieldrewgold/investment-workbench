"""
Per-ticker corpus search (AlphaSense-style).

Search any term under a ticker and get back the exact SNIPPET + where it lives
(which quarter's transcript, which press release, which deck, etc.) across all
of that name's source corpus already cached in data/dag_cache/<T>/.

  search_ticker(T, query) -> [{source, location, date, n_hits, hits:[{snippet, pos}]}]

No new data — indexes transcripts (split by quarter), press releases / 8-K
supplements, investor decks, news, quarterly financials, and external research.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

CACHE = Path("data/dag_cache")
_QTR_MARK = re.compile(r"(---\s*Q[1-4]\s+\d{4}\s+EARNINGS CALL\s*\([\d-]+\)\s*---)")


def _latest(ticker: str, step: str):
    fs = sorted(glob.glob(str(CACHE / ticker.upper() / f"{step}_*.json")), key=os.path.getmtime,
                reverse=True)
    if not fs:
        return None
    try:
        return json.loads(Path(fs[0]).read_text(encoding="utf-8")).get("output")
    except Exception:
        return None


def _segments(ticker: str):
    """Yield (source, location, date, text) labeled segments for the ticker."""
    t = ticker.upper()

    tr = _latest(t, "transcripts")
    if isinstance(tr, dict) and tr.get("text"):
        parts = _QTR_MARK.split(tr["text"])
        # parts = [pre, marker, body, marker, body, ...]
        if len(parts) == 1:
            yield ("transcript", "earnings call", "", parts[0])
        else:
            for i in range(1, len(parts), 2):
                marker = parts[i].strip().strip("-").strip()
                body = parts[i + 1] if i + 1 < len(parts) else ""
                yield ("transcript", marker, "", body)

    pr = _latest(t, "press_releases")
    if isinstance(pr, list):
        for r in pr:
            if not isinstance(r, dict):
                continue
            kind = "supplement" if r.get("kind") == "supplement" else "press release"
            loc = (r.get("quarter") or "") + ((" · " + r.get("source_url", "").split("/")[-1])
                                              if r.get("source_url") else "")
            yield (kind, loc.strip(" ·"), str(r.get("report_date") or r.get("filing_date") or "")[:10],
                   r.get("full_text_with_tables") or r.get("text") or "")

    sd = _latest(t, "slide_decks")
    if isinstance(sd, dict) and sd.get("corpus_text"):
        yield ("investor deck", "", "", sd["corpus_text"])

    nw = _latest(t, "news")
    if isinstance(nw, dict) and nw.get("corpus_text"):
        yield ("news", "", "", nw["corpus_text"])

    qf = _latest(t, "quarterly_financials")
    if isinstance(qf, dict) and qf.get("corpus_text"):
        yield ("financials", "", "", qf["corpus_text"])

    # External (inbox) research already classified for this ticker.
    try:
        from research.external_research import research_for_ticker
        for r in research_for_ticker(t, min_tier=3):
            body = (r.get("thesis", "") + "\n" + "\n".join(r.get("key_points") or [])
                    + "\n" + "\n".join(r.get("notable_claims") or []))
            yield ("research", f"{r.get('sender','')} · {r.get('subject','')[:40]}",
                   str(r.get("date", ""))[:10], body)
    except Exception:
        pass


def search_ticker(ticker: str, query: str, *, context: int = 160,
                  max_hits_per_seg: int = 6, max_total: int = 80) -> list[dict]:
    """Find `query` (case-insensitive substring) across the ticker's corpus.

    Returns a list of segment hits, each with the matched snippets + context."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    # Whole-word match for a single alphabetic term (so "liver" doesn't hit
    # "delivered"); substring for tickers/phrases/numbers ($AAOI, 10b5-1, "op margin").
    if re.fullmatch(r"[A-Za-z][A-Za-z']+", q):
        pat = re.compile(r"\b" + re.escape(q) + r"\b", re.I)
    else:
        pat = re.compile(re.escape(q), re.I)
    results: list[dict] = []
    total = 0
    for source, loc, date, text in _segments(ticker):
        if not text or total >= max_total:
            continue
        hits = []
        for m in pat.finditer(text):
            s = max(0, m.start() - context)
            e = min(len(text), m.end() + context)
            snippet = re.sub(r"\s+", " ", text[s:e]).strip()
            if s > 0:
                snippet = "…" + snippet
            if e < len(text):
                snippet = snippet + "…"
            hits.append({"snippet": snippet, "pos": m.start()})
            total += 1
            if len(hits) >= max_hits_per_seg or total >= max_total:
                break
        if hits:
            results.append({"source": source, "location": loc, "date": date,
                            "n_hits": len(hits), "hits": hits})
    # Surface transcripts/press/research before bulk news/financials.
    order = {"transcript": 0, "press release": 1, "supplement": 1, "research": 2,
             "investor deck": 3, "news": 4, "financials": 5}
    results.sort(key=lambda r: (order.get(r["source"], 9), r.get("date", "")), reverse=False)
    return results


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tk = sys.argv[1].upper() if len(sys.argv) > 1 else "TMDX"
    qy = " ".join(sys.argv[2:]) or "guidance"
    res = search_ticker(tk, qy)
    print(f"=== '{qy}' in {tk}: {sum(r['n_hits'] for r in res)} hits across {len(res)} segments ===")
    for r in res:
        print(f"\n[{r['source']}] {r['location']} {r['date']}  ({r['n_hits']} hits)")
        for h in r["hits"][:3]:
            print(f"   …{h['snippet'][:200]}")
