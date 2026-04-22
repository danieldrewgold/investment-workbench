"""
Pipeline step declarations.

Each step wraps an existing research function and declares its dependencies.
This file is pure declaration — no business logic. Functions live in
their original modules (financials_fetcher, transcript_analyzer, etc.).

Shape of the graph for `run_research_dag(ticker)`:

    ┌─ financials ──┐
    ├─ filing_text ─┤
    ├─ transcripts ─┼── transcript_digest ──┐
    ├─ consensus ───┤                       ├── (back to linear path)
    ├─ market_overlay ─┤                    │
    └─ press_releases ─┘                    │

Downstream (brief → model → adversarial → edge → valuation) stays linear
because it's tightly coupled math — DAG overhead isn't worth it for those.
"""

from __future__ import annotations

from datetime import date

from research.dag.core import Step, stable_hash


# --------------------------------------------------------------------------
# Step implementations — thin wrappers around existing functions
# --------------------------------------------------------------------------

def _step_financials(ctx: dict):
    """Fetch financials via Polygon + Alpha Vantage + EDGAR merge."""
    from research.financials_fetcher import fetch_financials
    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data")
    fin = fetch_financials(ticker, registry_data=registry_data, verbose=ctx.get("verbose", False))
    # Dataclass → dict for serializability (cache can round-trip it)
    from dataclasses import asdict
    return asdict(fin)


def _step_filing_text(ctx: dict) -> dict:
    """Fetch best filing text from EDGAR (10-K > 10-Q > 8-K > press release)."""
    ticker = ctx["ticker"]
    try:
        from research.edgar_text_fetcher import fetch_best_filing_text
        text, ftype = fetch_best_filing_text(ticker)
    except Exception as e:
        return {"text": "", "filing_type": "", "error": str(e)}
    return {"text": text or "", "filing_type": ftype or "", "error": ""}


def _step_transcripts(ctx: dict) -> dict:
    """Fetch 12 quarters of earnings call transcripts (EarningsCall.biz)."""
    ticker = ctx["ticker"]
    try:
        from research.transcript_fetcher import fetch_transcript_history
        text = fetch_transcript_history(ticker, quarters=12, verbose=ctx.get("verbose", False))
    except Exception as e:
        return {"text": "", "error": str(e), "char_count": 0}
    return {"text": text or "", "error": "", "char_count": len(text or "")}


def _step_consensus(ctx: dict) -> dict:
    """
    Fetch sell-side consensus via the rich consensus_loader — per-period
    EPS + revenue, revision history, price targets, ratings, next earnings.

    Returns a dict with shape:
      {
        "consensus":      {"eps": X, "revenue_m": Y},    # legacy back-compat
        "data":           {"eps", "current_price", "analyst_count", "earnings_date"},
        "full":           ConsensusData.to_dict()         # richer snapshot
      }
    """
    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}
    consensus = dict(registry_data.get("consensus", {}))
    data: dict = {}
    full_dict: dict | None = None
    if not consensus.get("eps"):
        try:
            from research.consensus_loader import fetch_consensus
            cd = fetch_consensus(ticker, verbose=ctx.get("verbose", False))
            if cd and not cd.error:
                legacy = cd.legacy_consensus_dict()
                if legacy.get("eps"):
                    consensus = legacy
                    data = {
                        "eps": legacy["eps"],
                        "current_price": cd.current_price,
                        "analyst_count": cd.max_analysts,
                    }
                    if cd.next_earnings.date:
                        data["earnings_date"] = cd.next_earnings.date
                    full_dict = cd.to_dict()
        except Exception:
            pass
    return {"consensus": consensus, "data": data, "full": full_dict}


def _step_market_overlay(ctx: dict) -> dict | None:
    """Fetch short interest, implied move, P/C ratio (market_overlay.fetch_market_data)."""
    ticker = ctx["ticker"]
    try:
        from research.market_overlay import fetch_market_data
        md = fetch_market_data(ticker)
    except Exception as e:
        return {"error": str(e)}
    if md is None:
        return None
    from dataclasses import asdict, is_dataclass
    try:
        return asdict(md) if is_dataclass(md) else dict(md)
    except Exception:
        return None


def _step_press_releases(ctx: dict) -> list[dict]:
    """Fetch last N quarterly earnings press releases from EDGAR 8-K Ex 99.1."""
    ticker = ctx["ticker"]
    try:
        from ingestion.loaders.press_release_loader import fetch_press_releases
        releases = fetch_press_releases(ticker, quarters=8, verbose=ctx.get("verbose", False))
    except Exception:
        return []
    # Dataclass list → dict list for serializability
    from dataclasses import asdict
    return [asdict(r) for r in releases]


def _step_transcript_digest(ctx: dict) -> dict | None:
    """Run 8 transcript subagents on fetched transcripts."""
    ticker = ctx["ticker"]
    transcripts = ctx.get("transcripts") or {}
    transcript_text = transcripts.get("text", "")
    if not transcript_text or len(transcript_text) < 500:
        return None
    try:
        from research.transcript_analyzer import analyze_transcripts
        digest = analyze_transcripts(ticker, transcript_text, verbose=ctx.get("verbose", False))
    except Exception:
        return None
    if digest is None:
        return None
    return digest.to_dict()


# --------------------------------------------------------------------------
# Cache-key helpers
# --------------------------------------------------------------------------

def _ticker_key(ctx: dict) -> str:
    """Cache key that depends only on the ticker. Re-run via --force."""
    return stable_hash(ctx.get("ticker", ""))


def _daily_ticker_key(ctx: dict) -> str:
    """Cache key that invalidates daily — for consensus/overlay which change."""
    return stable_hash(ctx.get("ticker", ""), date.today().isoformat())


def _content_hash_key(*input_names: str):
    """Cache key based on content hashes of named input step outputs."""
    def _key(ctx: dict) -> str:
        return stable_hash(
            ctx.get("ticker", ""),
            *[repr(ctx.get(n)) for n in input_names],
        )
    return _key


# --------------------------------------------------------------------------
# Step registry for the research pipeline
# --------------------------------------------------------------------------

def build_research_steps() -> list[Step]:
    """
    Return the set of Steps for the fetch + analysis layer of the pipeline.

    Everything downstream of `transcript_digest` (research brief, model,
    adversarial, edge, valuation, outputs) stays linear in pipeline.py —
    it's tightly coupled math that doesn't benefit from DAG overhead.
    """
    return [
        # ─── Independent fetches (parallel) ───
        Step(
            name="financials",
            inputs=[],
            run=_step_financials,
            # Fetches from 2 APIs + merges. Financial data is stable —
            # cache per ticker, --force to refresh on a new quarterly print.
            cache_key=_ticker_key,
        ),
        Step(
            name="filing_text",
            inputs=[],
            run=_step_filing_text,
            cache_key=_ticker_key,
        ),
        Step(
            name="transcripts",
            inputs=[],
            run=_step_transcripts,
            # transcript_fetcher has its own SQLite cache;
            # DAG cache on top saves parse time but is redundant.
            cache_key=_ticker_key,
        ),
        Step(
            name="consensus",
            inputs=[],
            run=_step_consensus,
            # Consensus can change daily (analyst revisions).
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="market_overlay",
            inputs=[],
            run=_step_market_overlay,
            # Short interest / options flow move daily.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="press_releases",
            inputs=[],
            run=_step_press_releases,
            # EDGAR 8-Ks accumulate but existing ones are immutable.
            cache_key=_ticker_key,
        ),

        # ─── Analysis (depends on fetches) ───
        Step(
            name="transcript_digest",
            inputs=["transcripts"],
            run=_step_transcript_digest,
            # Key on transcript content hash — if transcripts haven't
            # changed, digest output is stable.
            cache_key=_content_hash_key("transcripts"),
        ),
    ]
