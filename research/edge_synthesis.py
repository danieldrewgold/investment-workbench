"""
Edge synthesis + self-improvement.

Given the external research on a ticker (research/external_research), build an
EDGE thesis — not a summary, but a variant-perception read: where the market may
be wrong, what would have to be true, what breaks it — weighted by author tier.

Then a critic pass scores whether the output is genuine edge and proposes
concrete edits to the synthesis prompt. Those refinements accumulate in a
training log so the method improves across loop iterations ("train yourself").

  synthesize_edge(ticker)        -> edge thesis dict
  critique_edge(ticker, thesis)  -> {edge_score, is_genuine_edge, weaknesses, prompt_improvements}
  run_training_iteration(ticker) -> synth + critique + append to training log
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from research.transcript_subagents._base import call_subagent
from research.external_research import research_for_ticker, corpus_for_ticker, load_our_view

# Bump when the synthesis prompt below changes. The critic's prompt_improvements
# are how this version advances across loop iterations.
#   v1 -> v2 (COHR critique): quantify consensus-vs-variant mispricing; magnitude
#   thresholds + dates on every driver; contract-terms interrogation; falsification
#   path per market-structure claim; steelman the variant's bear case.
EDGE_PROMPT_VERSION = "v2"

TRAINING_LOG = Path("data/email_research/edge_training_log.json")
EDGE_DIR = Path("data/email_research/edge")

# ---------------------------------------------------------------------------
# Synthesis (v1) — the prompt under training. Edits should target genuine edge:
# variant perception, falsifiable drivers, what-breaks-it, source weighting.
# ---------------------------------------------------------------------------
_EDGE_SYSTEM = """You are an edge-seeking analyst. Your job is NOT to summarize \
research — it is to extract the EDGE: where consensus / current price may be \
WRONG, and the variant view an investor could underwrite — QUANTIFIED and \
FALSIFIABLE. Return ONLY JSON.

You are given independent research (newsletters / Substacks) on one ticker, each \
tagged with author credibility tier (1 = highest conviction, 3 = baseline) and \
stance. Weight tier-1 claims heavily; treat tier-3 / unverified claims as leads, \
not facts. A claim corroborated by multiple independent tier-1/2 sources is \
stronger. Be specific and skeptical: generic restatement, a one-sided bull case \
dressed as "variant perception", and unfalsifiable assertions are FAILURES.

Produce JSON with exactly these keys:
- variant_perception: 1-3 sentences — the non-consensus view these sources \
collectively support that the market likely UNDER-appreciates. If it is really \
just consensus / no edge, say so plainly.
- consensus_vs_variant: object {consensus_view: what the street / current price \
appears to assume for the key driver (e.g. "OCS revenue modeled ~$0 discretely"), \
variant_scenario: the alternative WITH ROUGH MAGNITUDE (e.g. "$300-500M OCS rev by \
2027"), implied_mispricing: how large the gap is and why it persists}.
- mechanism: the causal chain that makes the variant view pay off.
- falsifiable_drivers: 3-6 strings, EACH naming a specific METRIC, a THRESHOLD \
(magnitude — never a bare "announced"/"confirmed"), and a DATE/quarter to check \
(e.g. "OCS revenue >= $50M annualized run-rate disclosed on an earnings call by \
Q4 2026").
- kill_criteria: 2-4 specific things that would BREAK the thesis.
- contract_terms_check: for any strategic investment / supply agreement cited as a \
"demand floor", identify the actual contractual mechanism (take-or-pay, volume \
minimum, pricing floor/ceiling) and state explicitly if it is UNCONFIRMED. Else "n/a".
- catalysts: dated/near-term events that force a re-rating, with timing.
- key_supporting_claims: array of objects {claim, source_tier, verifiable: \
"verifiable"|"partially"|"assertion", falsification_path: for any market-structure \
claim (duopoly, penetration %, attach ratio) a specific test that would invalidate \
it — REQUIRED for any claim the thesis leans on}.
- variant_bear_case: object {most_nonconsensus_claim: the single load-bearing \
claim, steelman: the strongest counter-argument for why it may be wrong, \
probability_variant_correct: a rough % the variant (not consensus) is right}.
- edge_confidence: high | medium | low + one line (source quality + corroboration \
+ falsifiability).
- actionable_thesis: one sentence an investor could act on (direction + why now).
- our_view_cross_ref: if a prior workbench view is provided, whether the research \
REINFORCES or CONTRADICTS it and where the gap is; else "n/a".

Output JSON only with exactly those keys."""

_CRITIC_SYSTEM = """You are a demanding editor grading an EDGE thesis built from \
research. Judge ONLY whether it surfaces genuine investable edge. Return ONLY JSON.

Score harshly. A summary dressed as a thesis scores low.

Keys:
- edge_score: integer 1-5 (5 = sharp, non-consensus, falsifiable, well-sourced; \
1 = generic summary with no variant view).
- is_genuine_edge: boolean — does it identify a real variant perception vs consensus?
- weaknesses: 2-5 specific failings (e.g. "restates the bull case without saying \
what consensus misses", "drivers aren't falsifiable", "leans on a tier-3 claim").
- prompt_improvements: 2-5 concrete, actionable edits to the SYNTHESIS prompt that \
would fix those weaknesses next time (these are the training signal — be specific, \
e.g. "require each falsifiable_driver to name a metric and a threshold")."""


def _coerce(records):
    lines = []
    for r in records:
        a = r.get("author", {}) or {}
        lines.append(
            f"--- {str(r.get('date',''))[:10]} | {a.get('name', r.get('sender',''))} "
            f"| tier {r.get('effective_tier','?')} | stance {r.get('stance','')} ---\n"
            f"Subject: {r.get('subject','')}\nThesis: {r.get('thesis','')}\n"
            + "\n".join(f"- {k}" for k in (r.get('key_points') or [])[:6])
            + ("\n" + "\n".join(f"* {c}" for c in (r.get('notable_claims') or [])[:4])
               if r.get('notable_claims') else "")
        )
    return "\n\n".join(lines)


def synthesize_edge(ticker: str, *, our_view: str | None = None, records=None,
                    verbose: bool = False) -> dict | None:
    ticker = ticker.upper().strip()
    recs = records if records is not None else research_for_ticker(ticker, min_tier=3)
    if not recs:
        return None
    if our_view is None:  # auto-corroborate against the workbench's own view
        our_view = load_our_view(ticker)
    body = _coerce(recs)
    user = (f"TICKER: {ticker}\n"
            f"PRIOR WORKBENCH VIEW: {our_view or '(none provided)'}\n\n"
            f"INDEPENDENT RESEARCH ({len(recs)} pieces):\n\n{body}")
    res = call_subagent("edge_synth", ticker, _EDGE_SYSTEM, user,
                        max_tokens=3400, verbose=verbose)
    if not res.ok or not res.data:
        if verbose:
            print(f"  [EDGE] synth failed {ticker}: {res.error}")
        return None
    out = dict(res.data)
    out["_ticker"] = ticker
    out["_n_sources"] = len(recs)
    out["_version"] = EDGE_PROMPT_VERSION
    EDGE_DIR.mkdir(parents=True, exist_ok=True)
    (EDGE_DIR / f"{ticker}.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def critique_edge(ticker: str, thesis: dict, *, records=None, verbose: bool = False) -> dict | None:
    recs = records if records is not None else research_for_ticker(ticker.upper(), min_tier=3)
    user = (f"TICKER: {ticker}\n\nNUMBER OF SOURCE PIECES: {len(recs)}\n\n"
            f"EDGE THESIS TO GRADE:\n{json.dumps({k: v for k, v in thesis.items() if not k.startswith('_')}, ensure_ascii=False, indent=2)}")
    res = call_subagent("edge_critic", ticker, _CRITIC_SYSTEM, user,
                        max_tokens=1200, verbose=verbose)
    if not res.ok or not res.data:
        return None
    return res.data


def run_training_iteration(ticker: str, *, our_view: str | None = None, stamp: str = "",
                           verbose: bool = True) -> dict:
    """Synthesize an edge thesis, critique it, append the result to the training
    log. Returns the combined record. `stamp` is an ISO timestamp from the caller."""
    thesis = synthesize_edge(ticker, our_view=our_view, verbose=verbose)
    if thesis is None:
        return {"ticker": ticker, "error": "no research / synth failed"}
    crit = critique_edge(ticker, thesis, verbose=verbose) or {}
    entry = {
        "stamp": stamp or datetime.now(tz=timezone.utc).isoformat(),
        "version": EDGE_PROMPT_VERSION,
        "ticker": ticker.upper(),
        "n_sources": thesis.get("_n_sources"),
        "edge_score": crit.get("edge_score"),
        "is_genuine_edge": crit.get("is_genuine_edge"),
        "weaknesses": crit.get("weaknesses", []),
        "prompt_improvements": crit.get("prompt_improvements", []),
    }
    log = []
    if TRAINING_LOG.exists():
        try:
            log = json.loads(TRAINING_LOG.read_text(encoding="utf-8"))
        except Exception:
            log = []
    log.append(entry)
    TRAINING_LOG.parent.mkdir(parents=True, exist_ok=True)
    TRAINING_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"thesis": thesis, "critique": crit, "log_entry": entry}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tk = (sys.argv[1] if len(sys.argv) > 1 else "AAOI").upper()
    out = run_training_iteration(tk)
    print(json.dumps(out, indent=2, ensure_ascii=False)[:6000])
