"""
Per-ticker AI Q&A (AlphaSense-style "ask anything about a name").

Retrieval-augmented: pulls the relevant passages from the ticker's corpus
(reusing corpus_search) plus the pre-digested knowledge we already hold
(transcript digest, our workbench view, the external edge thesis, financials),
then has Claude answer GROUNDED in that context with inline source citations.

  ask_ticker(T, question) -> {answer, confidence, sources_used, snippets}
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from research.transcript_subagents._base import call_subagent
from research.corpus_search import search_ticker, _latest
from research.external_research import load_our_view

_STOP = set("""what when where which who why how does did do is are was were be been being
the a an and or of to in on for with about into over after before from by at as it its their
this that these those management company stock guidance say said tell explain give show me
our we they you most more than vs versus""".split())

_SYSTEM = """You answer questions about ONE stock using ONLY the provided context \
(the company's earnings-call transcripts, filings, press releases, investor decks, \
news, our workbench model view, and independent research). Return ONLY JSON.

Rules:
- Ground EVERY claim in the context. Cite inline with the bracketed source label \
exactly as given, e.g. [transcript Q3 2025] or [press release Q1 2026].
- If the answer is not in the context, say so plainly — never invent numbers or facts.
- Be specific and quantitative: cite numbers, quarters, and direction of change.
- Synthesize across sources when relevant (e.g. management's claim vs. our model vs. \
independent research).

JSON keys: answer (markdown, with inline [source] citations), confidence \
(high|medium|low), sources_used (list of the source labels you relied on)."""


def _question_terms(q: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", (q or "").lower())
    out, seen = [], set()
    for w in words:
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:8]


def _knowledge_pack(ticker: str, max_chars: int = 7000) -> str:
    """Pre-digested context we already hold on the name."""
    parts: list[str] = []
    ov = load_our_view(ticker)
    if ov:
        parts.append(ov)
    td = _latest(ticker, "transcript_digest")
    if isinstance(td, dict):
        keep = {k: td[k] for k in ("tone_trajectory", "management_credibility",
                                   "guidance_evolution", "recurring_concerns",
                                   "key_inflection_points") if td.get(k)}
        if keep:
            parts.append("TRANSCRIPT DIGEST: " + json.dumps(keep, ensure_ascii=False)[:2500])
    ef = Path(f"data/email_research/edge/{ticker.upper()}.json")
    if ef.exists():
        try:
            e = json.loads(ef.read_text(encoding="utf-8"))
            parts.append("EXTERNAL EDGE THESIS: " + json.dumps(
                {k: e.get(k) for k in ("variant_perception", "actionable_thesis",
                                       "edge_confidence")}, ensure_ascii=False))
        except Exception:
            pass
    qf = _latest(ticker, "quarterly_financials")
    if isinstance(qf, dict) and qf.get("corpus_text"):
        parts.append("RECENT FINANCIALS:\n" + qf["corpus_text"][:1500])
    return "\n\n".join(str(p) for p in parts)[:max_chars]


def ask_ticker(ticker: str, question: str, *, verbose: bool = False) -> dict | None:
    ticker = ticker.upper().strip()
    q = (question or "").strip()
    if len(q) < 3:
        return None

    # Retrieve question-relevant passages from the corpus.
    snips: list[str] = []
    seen: set = set()
    for term in _question_terms(q):
        for r in search_ticker(ticker, term, context=180, max_hits_per_seg=2, max_total=10):
            for h in r["hits"]:
                key = h["snippet"][:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                label = r["source"] + (f" {r['location']}" if r["location"] else "") + \
                    (f" {r['date']}" if r["date"] else "")
                snips.append(f"[{label}] {h['snippet']}")
    snips = snips[:24]

    pack = _knowledge_pack(ticker)
    user = (f"CONTEXT FOR {ticker}:\n{pack}\n\nRETRIEVED PASSAGES:\n"
            + "\n\n".join(snips) + f"\n\nQUESTION: {q}")
    res = call_subagent("ticker_qa", ticker, _SYSTEM, user, max_tokens=1800, verbose=verbose)
    if not res.ok or not res.data:
        return {"answer": f"(no answer — {res.error or 'parse failed'})",
                "confidence": "", "sources_used": [], "snippets": snips}
    out = dict(res.data)
    out["snippets"] = snips
    out["n_retrieved"] = len(snips)
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tk = sys.argv[1].upper() if len(sys.argv) > 1 else "TMDX"
    qn = " ".join(sys.argv[2:]) or "What did management say about margins and guidance?"
    r = ask_ticker(tk, qn, verbose=False)
    print(f"Q: {qn}\n")
    print("ANSWER:", (r or {}).get("answer", "(none)"))
    print("\nconfidence:", (r or {}).get("confidence"), "| retrieved:", (r or {}).get("n_retrieved"))
    print("sources:", (r or {}).get("sources_used"))
