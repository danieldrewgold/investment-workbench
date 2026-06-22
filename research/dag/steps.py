"""
Pipeline step declarations.

Each step wraps an existing research function and declares its dependencies.
This file is pure declaration — no business logic. Functions live in
their original modules (financials_fetcher, transcript_analyzer, etc.).

Shape of the graph for `run_research_dag(ticker)`:

    ┌─ financials ──┐
    ├─ filing_text ─┤
    ├─ transcripts ─┼── transcript_digest ─┐
    ├─ consensus ───┤                      ├─ slide_decks ──┐
    ├─ market_overlay ─┤                   │                ├─ guidance_bundle ─┐
    ├─ press_releases ─┘                   │                │                   │
    ├─ fred_macro ───────────────────────  │                │                   │
    ├─ bls_macro  ───────────────────────  │                │                   │
    ├─ bea_macro  ───────────────────────  │                │                   │
    ├─ peer_comps ───────────────────────  │                │                   │
    ├─ quarterly_financials ────────────   │                │                   │
    ├─ news ───────────────────────────    │                │                   │
    └─ bear_research ──────────────────    │                │                   │

Downstream (research brief → adversarial → EPS bridge → edge → valuation)
is added by Tier 2 of the DAG refactor. The post-brief math (overlay → edge
detection → valuation → decision) stays linear because it's tightly coupled
arithmetic on already-computed values.
"""

from __future__ import annotations

import os
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
    """Fetch 12 quarters of earnings call transcripts (EarningsCall.biz).
    Stores both a compact digest (`text`, for the brief's token budget) and the
    FULL per-quarter transcripts (`raw_quarters`, for the dashboard's raw view)."""
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from research.transcript_fetcher import fetch_full_transcripts, _digest_quarters
        raw = fetch_full_transcripts(ticker, quarters=12, verbose=verbose) or []
        text = _digest_quarters(raw) or ""
    except Exception as e:
        return {"text": "", "raw_quarters": [], "error": str(e), "char_count": 0}
    return {"text": text, "raw_quarters": raw, "error": "",
            "char_count": len(text), "n_raw_quarters": len(raw)}


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
    # Always run fetch_consensus to populate the full per-period structure —
    # the brief now needs full anchor data (consensus_full + guidance) not
    # just the legacy {eps, revenue_m}. If registry already had basic
    # consensus.eps, we still upgrade to the rich structure when fetch
    # succeeds; otherwise we fall back to the registry value.
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
        releases = fetch_press_releases(ticker, quarters=12, verbose=ctx.get("verbose", False))
    except Exception:
        return []
    # Dataclass list → dict list for serializability
    from dataclasses import asdict
    return [asdict(r) for r in releases]


def _step_external_research(ctx: dict) -> dict:
    """Independent analyst / newsletter research on the ticker (inbox-ingested,
    classified, author-tier-weighted) — feeds the brief as VARIANT PERCEPTION.
    Gated to tier 1-2 so only credible voices reach the brief; the dashboard KB
    can surface everything. Empty when no external research mentions the name."""
    ticker = ctx["ticker"]
    try:
        from research.external_research import corpus_for_ticker, research_for_ticker
        recs = research_for_ticker(ticker, min_tier=2)
        text = corpus_for_ticker(ticker, min_tier=2, max_items=6)
    except Exception as e:
        return {"corpus_text": "", "n_pieces": 0, "error": f"{type(e).__name__}: {e}"}
    return {"corpus_text": text, "n_pieces": len(recs)}


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
# Step implementations — Tier 1 additions (parallel fetches that previously
# ran sequentially in run_research)
# --------------------------------------------------------------------------

def _step_slide_decks(ctx: dict) -> dict:
    """
    Fetch top 2 slide decks (earnings + investor day priority) and run the
    8 vision subagents on each. Gated by DECK_ANALYSIS_ENABLED env var
    (default on).

    Returns:
      {
        "digests": list of DeckDigest.to_dict()  — for guidance / result
        "corpus_text": str  — concatenated to_prompt_text() blocks
        "n_picked": int, "n_analyzed": int, "skipped": bool, "error": str
      }

    The deck_analyzer has its own content-hash cache for digests; the
    DAG cache adds trace-level visibility on top of that.
    """
    if os.environ.get("DECK_ANALYSIS_ENABLED", "1") == "0":
        return {
            "digests": [], "corpus_text": "",
            "n_picked": 0, "n_analyzed": 0,
            "skipped": True, "skip_reason": "DECK_ANALYSIS_ENABLED=0",
        }

    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)

    try:
        from ingestion.loaders.slide_deck_loader import fetch_all_slide_decks
        from research.deck_analyzer import analyze_slide_deck
    except Exception as e:
        return {
            "digests": [], "corpus_text": "",
            "n_picked": 0, "n_analyzed": 0,
            "error": f"import: {type(e).__name__}: {e}",
        }

    try:
        decks = fetch_all_slide_decks(ticker, quarters=2, max_ir_decks=4, verbose=verbose)
    except Exception as e:
        return {
            "digests": [], "corpus_text": "",
            "n_picked": 0, "n_analyzed": 0,
            "error": f"fetch_all_slide_decks: {type(e).__name__}: {e}",
        }

    # Pick top 2 by deck-type priority: most recent earnings + most recent
    # investor day (or fall back to other types if those aren't available).
    picked: list = []
    seen_types: set = set()
    priority = ["earnings", "investor_day", "conference",
                "shareholder_letter", "other"]
    for dtype in priority:
        for d in decks:
            if d.deck_type == dtype and d.deck_type not in seen_types:
                picked.append(d)
                seen_types.add(d.deck_type)
                if len(picked) >= 2:
                    break
        if len(picked) >= 2:
            break

    digests: list[dict] = []
    corpus_parts: list[str] = []
    for deck in picked:
        try:
            digest = analyze_slide_deck(deck, verbose=verbose, force=False)
        except Exception:
            continue
        if digest is None:
            continue
        digests.append(digest.to_dict())
        text = digest.to_prompt_text()
        if text:
            corpus_parts.append(text)

    # Metadata for EVERY fetched deck (not just the 2 analyzed) so the
    # dashboard can list them all with a click-to-open link.
    picked_hashes = {getattr(d, "content_hash", None) for d in picked}
    deck_meta = [
        {
            "title": getattr(d, "title", "") or "",
            "deck_type": getattr(d, "deck_type", "") or "",
            "date": getattr(d, "report_date", "") or getattr(d, "filing_date", "") or "",
            "quarter": getattr(d, "quarter", "") or "",
            "page_count": getattr(d, "page_count", 0),
            "source_url": getattr(d, "source_url", "") or "",
            "source": getattr(d, "source", "") or "",
            "analyzed": getattr(d, "content_hash", None) in picked_hashes,
        }
        for d in decks
    ]
    return {
        "digests": digests,
        "decks": deck_meta,
        "corpus_text": "\n\n".join(corpus_parts).strip(),
        "n_picked": len(picked),
        "n_analyzed": len(digests),
    }


def _step_fred_macro(ctx: dict) -> dict:
    """Fetch FRED macro context (single global cache, no ticker key)."""
    try:
        from ingestion.loaders.fred_macro_loader import fetch_macro_context
        macro = fetch_macro_context(verbose=ctx.get("verbose", False))
    except Exception as e:
        return {"corpus_text": "", "n_series": 0, "error": f"{type(e).__name__}: {e}"}
    if not macro or not macro.series:
        return {"corpus_text": "", "n_series": 0}
    return {
        "corpus_text": macro.to_prompt_text(),
        "n_series": len(macro.series),
        "fetched_at": macro.fetched_at,
    }


def _step_bls_macro(ctx: dict) -> dict:
    """Fetch BLS macro context (single global cache)."""
    try:
        from ingestion.loaders.bls_macro_loader import fetch_bls_context
        bls = fetch_bls_context(verbose=ctx.get("verbose", False))
    except Exception as e:
        return {"corpus_text": "", "n_series": 0, "error": f"{type(e).__name__}: {e}"}
    if not bls or not bls.series:
        return {"corpus_text": "", "n_series": 0}
    return {
        "corpus_text": bls.to_prompt_text(),
        "n_series": len(bls.series),
        "fetched_at": bls.fetched_at,
    }


def _step_bea_macro(ctx: dict) -> dict:
    """Fetch BEA macro context (single global cache; gracefully skips if no
    BEA_API_KEY set)."""
    try:
        from ingestion.loaders.bea_macro_loader import fetch_bea_context
        bea = fetch_bea_context(verbose=ctx.get("verbose", False))
    except Exception as e:
        return {"corpus_text": "", "n_series": 0, "error": f"{type(e).__name__}: {e}"}
    if not bea or not bea.series or bea.no_key:
        return {
            "corpus_text": "", "n_series": 0,
            "no_key": getattr(bea, "no_key", False) if bea else True,
        }
    return {
        "corpus_text": bea.to_prompt_text(),
        "n_series": len(bea.series),
        "fetched_at": bea.fetched_at,
    }


def _step_peer_comps(ctx: dict) -> dict:
    """
    Fetch per-peer consensus rows. Schema is inferred via:
      1) registry_data['schema'] (curated override)
      2) yfinance industry/sector mapping
      3) PEER_GROUPS membership (subject ticker in a curated peer list)

    Returns the schema in the result so a post-brief retry can detect
    a brief.schema_type mismatch.
    """
    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}
    verbose = ctx.get("verbose", False)
    try:
        from research.peer_comps import fetch_peer_comps
        from research.peer_registry import PEER_GROUPS, infer_schema_from_yfinance
    except Exception as e:
        return {
            "corpus_text": "", "schema": "", "n_peers": 0,
            "error": f"import: {type(e).__name__}: {e}",
        }

    schema_guess = (registry_data or {}).get("schema") or ""
    if not schema_guess:
        try:
            schema_guess = infer_schema_from_yfinance(ticker, verbose=verbose)
        except Exception:
            schema_guess = ""
    if not schema_guess:
        for sk, tickers in PEER_GROUPS.items():
            if ticker.upper() in tickers:
                schema_guess = sk
                break
    if not schema_guess:
        return {"corpus_text": "", "schema": "", "n_peers": 0}

    try:
        peers = fetch_peer_comps(ticker, schema_guess, max_peers=4, verbose=verbose)
    except Exception as e:
        return {
            "corpus_text": "", "schema": schema_guess, "n_peers": 0,
            "error": f"fetch_peer_comps: {type(e).__name__}: {e}",
        }
    if not peers or not peers.rows:
        return {"corpus_text": "", "schema": schema_guess, "n_peers": 0}
    return {
        "corpus_text": peers.to_prompt_text(),
        "schema": schema_guess,
        "n_peers": len(peers.rows),
        "fetched_at": peers.fetched_at,
    }


def _step_quarterly_financials(ctx: dict) -> dict:
    """Fetch 12 quarters of structured income statement data from Polygon."""
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from research.quarterly_financials_loader import fetch_quarterly_financials
        bundle = fetch_quarterly_financials(ticker, n_quarters=24, verbose=verbose)
    except Exception as e:
        return {"corpus_text": "", "n_quarters": 0, "error": f"{type(e).__name__}: {e}"}
    if not bundle or not bundle.reports:
        return {"corpus_text": "", "n_quarters": 0}
    return {
        "corpus_text": bundle.to_prompt_text(max_quarters=24),
        "n_quarters": len(bundle.reports),
        "fetched_at": bundle.fetched_at,
    }


def _step_news(ctx: dict) -> dict:
    """Fetch recent news (Polygon + AV) with material-event prioritization."""
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from research.news_loader import fetch_news
        bundle = fetch_news(ticker, days_back=90, verbose=verbose, max_items=20)
    except Exception as e:
        return {"corpus_text": "", "n_items": 0, "error": f"{type(e).__name__}: {e}"}
    if not bundle or not bundle.items:
        return {"corpus_text": "", "n_items": 0}
    return {
        "corpus_text": bundle.to_prompt_text(max_items=20),
        "n_items": len(bundle.items),
        "fetched_at": bundle.fetched_at,
    }


def _step_bear_research(ctx: dict) -> dict:
    """
    Fetch bear-case research from Fuzzy Panda / Spruce Point / Hindenburg /
    Wolfpack + DDG fallback for blocked sites. Cached weekly because bear
    reports are sticky.
    """
    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}
    verbose = ctx.get("verbose", False)
    company_name = (registry_data or {}).get("name") if registry_data else None
    try:
        from research.short_research_loader import fetch_short_research
        bundle = fetch_short_research(ticker, company_name=company_name, verbose=verbose)
    except Exception as e:
        return {"corpus_text": "", "n_reports": 0, "error": f"{type(e).__name__}: {e}"}
    if not bundle or not bundle.reports:
        return {"corpus_text": "", "n_reports": 0}
    return {
        "corpus_text": bundle.to_prompt_text(),
        "n_reports": len(bundle.reports),
        "fetched_at": bundle.fetched_at,
    }


def _step_social_topic_analysis(ctx: dict) -> dict:
    """
    Cluster StockTwits dialogue into themes and cross-check each against
    earnings call transcripts to flag retail-debated-but-management-silent
    topics. Single Sonnet call. Output feeds the brief corpus.

    Inputs: stocktwits (with messages), transcripts (raw text).
    """
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    sw = ctx.get("stocktwits") or {}
    msgs = sw.get("messages") or []
    if not msgs:
        return {"corpus_text": "", "n_themes": 0,
                "n_silence_flags": 0, "error": "no stocktwits messages"}

    tr = ctx.get("transcripts") or {}
    tr_text = tr.get("text", "") if isinstance(tr, dict) else ""

    try:
        from research.social_topic_analyzer import analyze_social_topics
        bundle = analyze_social_topics(
            ticker, msgs, tr_text, verbose=verbose,
        )
    except Exception as e:
        return {"corpus_text": "", "n_themes": 0,
                "n_silence_flags": 0,
                "error": f"{type(e).__name__}: {e}"}

    n_silent = sum(1 for t in bundle.themes if t.silence_flag)
    return {
        "ticker": bundle.ticker,
        "n_themes": len(bundle.themes),
        "n_silence_flags": n_silent,
        "themes": [
            {
                "name": t.name, "n_messages": t.n_messages,
                "sentiment_lean": t.sentiment_lean,
                "representative_quotes": list(t.representative_quotes),
                "addressed_in_transcripts": t.addressed_in_transcripts,
                "transcript_evidence": t.transcript_evidence,
                "silence_flag": t.silence_flag,
                "notes": t.notes,
            }
            for t in bundle.themes
        ],
        "corpus_text": bundle.to_prompt_text(),
        "fetched_at": bundle.fetched_at,
        "error": bundle.error,
    }


def _step_stocktwits(ctx: dict) -> dict:
    """
    StockTwits retail dialogue. Pulls last ~30 ticker-tagged messages,
    filters spam/pump patterns, returns a labeled corpus block framed as
    'topic radar' rather than fact. The brief should treat surfaced
    topics as research questions; the claim_verifier pass will then
    independently verify substantive claims via web_search.
    """
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from research.stocktwits_loader import fetch_stocktwits
        bundle = fetch_stocktwits(ticker, days_back=30, verbose=verbose)
    except Exception as e:
        return {"corpus_text": "", "n_messages": 0,
                "error": f"{type(e).__name__}: {e}"}
    if not bundle or not bundle.messages:
        return {"corpus_text": "", "n_messages": 0, "messages": [],
                "error": bundle.error if bundle else ""}
    return {
        "corpus_text": bundle.to_prompt_text(max_messages=10),
        "n_messages": bundle.n_total,
        "sentiment_bull": bundle.sentiment_bull,
        "sentiment_bear": bundle.sentiment_bear,
        "sentiment_none": bundle.sentiment_none,
        "fetched_at": bundle.fetched_at,
        # Include messages so downstream steps (social_topic_analysis)
        # can re-cluster them. Keeps each message lean — body + key meta.
        "messages": [
            {
                "id": m.id, "created_at": m.created_at,
                "body": m.body, "username": m.username,
                "user_followers": m.user_followers,
                "user_official": m.user_official,
                "sentiment": m.sentiment,
                "engagement": m.engagement,
            }
            for m in bundle.messages
        ],
    }


def _step_filing_13d(ctx: dict) -> dict:
    """
    Per-ticker SC 13D / 13G lookup. Catches PE / activist / strategic
    holders that 13F doesn't surface (PE sponsors file 13D for control
    positions, not 13F). The corpus_text feeds the brief so PE crowding
    appears in the synthesis.
    """
    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}
    verbose = ctx.get("verbose", False)
    target_cik = (registry_data or {}).get("cik") or None
    try:
        from ingestion.loaders.edgar_13d_loader import fetch_13d_filings
        bundle = fetch_13d_filings(
            ticker, target_cik=target_cik,
            max_filings=20, verbose=verbose, parse_primary_doc=True,
        )
    except Exception as e:
        return {
            "n_filings": 0, "corpus_text": "",
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "ticker": bundle.ticker,
        "target_cik": bundle.target_cik,
        "target_name": bundle.target_name,
        "n_filings": len(bundle.filings),
        "n_unique_filers": len({f.filer_cik or f.filer_name for f in bundle.filings}),
        "filings": [
            {
                "filer_name": f.filer_name, "filer_cik": f.filer_cik,
                "form_type": f.form_type, "filed_date": f.filed_date,
                "shares_held": f.shares_held, "pct_of_class": f.pct_of_class,
                "activist_intent": f.activist_intent,
                "purpose_excerpt": f.purpose_excerpt,
                "primary_doc_url": f.primary_doc_url,
            }
            for f in bundle.filings
        ],
        "corpus_text": bundle.to_prompt_text(),
        "fetched_at": bundle.fetched_at,
        "error": bundle.error,
    }


def _resolve_cusip(ticker: str, company_name: str = "") -> str | None:
    """Resolve a ticker -> 9-char equity CUSIP for the reverse-13F lookup.
    Primary: the workbench.db 13F holdings (issuer-name match, most-held class
    wins — picks e.g. GOOG's Class C over GOOGL's Class A). Fallback: SEC
    full-text search of 13F-HR info tables by company name (covers names no
    tracked fund holds, e.g. ELF)."""
    import re
    import sqlite3
    from pathlib import Path

    # Hand-verified CUSIPs for names where the issuer-name search is unreliable
    # (e.g. dotted acronyms our funds don't hold).
    _CUSIP_OVERRIDES = {"ELF": "26856L103"}
    if ticker.upper() in _CUSIP_OVERRIDES:
        return _CUSIP_OVERRIDES[ticker.upper()]

    name = (company_name or "").upper()
    if not name:
        try:
            from ingestion.loaders.edgar_13d_loader import resolve_ticker_to_cik
            r = resolve_ticker_to_cik(ticker)
            name = (r[1] if r else ticker).upper()
        except Exception:
            name = ticker.upper()
    STOP = {"INC", "CORP", "CORPORATION", "CO", "LTD", "LIMITED", "PLC", "THE",
            "COMPANY", "HOLDINGS", "GROUP", "CLASS", "COM", "NEW", "NV", "SA", "AG"}
    # Drop dots WITHOUT splitting so dotted acronyms survive ("E.L.F." -> "ELF").
    name_nd = name.replace(".", "")
    toks = [t for t in re.sub(r"[^A-Z0-9 ]", " ", name_nd).split()
            if len(t) >= 2 and t not in STOP]

    # 1) workbench.db holdings
    db = Path("data/workbench.db")
    if db.exists() and toks:
        try:
            con = sqlite3.connect(str(db))
            rows = con.execute(
                "SELECT cusip, issuer_name, COUNT(*) c FROM holding_13f "
                "WHERE issuer_name LIKE ? GROUP BY cusip ORDER BY c DESC",
                (toks[0] + "%",),
            ).fetchall()
            con.close()
            best, best_score = None, -1
            for cusip, iss, c in rows:
                if not cusip or len(cusip) != 9:
                    continue
                itoks = set(re.sub(r"[^A-Z0-9 ]", " ", (iss or "").upper()).split())
                score = len(set(toks) & itoks) * 1000 + c
                if score > best_score:
                    best, best_score = cusip, score
            if best:
                return best
        except Exception:
            pass

    # 2) efts full-text fallback: search 13F-HR info tables by company name,
    # collect candidate CUSIPs whose issuer matches, then keep the one with the
    # MOST 13F filers (a real position has hundreds; a mis-match has a handful).
    try:
        import time
        import httpx
        from ingestion.loaders.edgar_13f_holders_loader import SEC_HEADERS, _recent_window
        from ingestion.loaders.edgar_13f_loader import Edgar13FLoader
        startdt, enddt = _recent_window()
        toks_nd = [t.replace(".", "") for t in toks]

        def efts(params):
            for _ in range(3):
                try:
                    r = httpx.get("https://efts.sec.gov/LATEST/search-index",
                                  params=params, headers=SEC_HEADERS, timeout=30)
                    j = r.json()
                    if "hits" in j:
                        return j
                except Exception:
                    pass
                time.sleep(0.7)
            return {}

        q = " ".join(toks_nd[:3]) or name
        hits = (((efts({"q": f'"{q}"', "forms": "13F-HR",
                        "startdt": startdt, "enddt": enddt}).get("hits")) or {}).get("hits")) or []
        candidates = set()
        for hit in hits[:3]:
            s = hit.get("_source") or {}
            _id = hit.get("_id") or ""
            doc = _id.split(":", 1)[1] if ":" in _id else ""
            cik = (s.get("ciks") or [""])[0]
            accn = (s.get("adsh") or "").replace("-", "")
            cik_int = str(int(cik)) if str(cik).isdigit() else cik
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{doc}"
            try:
                rr = httpx.get(url, headers=SEC_HEADERS, timeout=30, follow_redirects=True)
                if rr.status_code != 200:
                    continue
            except Exception:
                continue
            for h in Edgar13FLoader.parse_13f_xml(rr.text):
                if h.get("put_call") or len(h.get("cusip") or "") != 9:
                    continue
                iss = re.sub(r"[^A-Z0-9 ]", " ", (h.get("issuer_name") or "").upper())
                itoks = set(iss.split()) | {iss.replace(" ", "")}
                if set(toks_nd) & itoks:
                    candidates.add(h["cusip"])
            time.sleep(0.15)
        # Validate: the right CUSIP is the one with the most 13F filers.
        best, best_n = None, -1
        for c in list(candidates)[:6]:
            n = (((efts({"q": f'"{c}"', "forms": "13F-HR",
                        "startdt": startdt, "enddt": enddt}).get("hits")) or {}).get("total") or {}).get("value", 0)
            if n > best_n:
                best, best_n = c, n
            time.sleep(0.15)
        if best and best_n >= 5:
            return best
    except Exception:
        pass
    return None


def _fetch_float_data(ticker: str) -> dict:
    """yfinance float/short snapshot for the retail-float decomposition.
    Returns {} on any failure (foreign filers, offline). All best-effort."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return {}
    out = {
        "shares_outstanding": info.get("sharesOutstanding"),
        "float_shares": info.get("floatShares"),
        "insider_pct": info.get("heldPercentInsiders"),
        "institution_pct": info.get("heldPercentInstitutions"),
        "short_pct_float": info.get("shortPercentOfFloat"),
    }
    return out if out.get("float_shares") else {}


def _ownership_decomposition(bundle, so_used, float_data, pe_bucket) -> dict | None:
    """
    Split shares outstanding into closely-held (strategic + insiders),
    institutional float (NET of short interest), and retail / other float.

    Institutional ownership routinely reads >100% of float because every
    shorted share is owned by two longs (the lender and the buyer from the
    short seller). Subtracting short interest from the float-institutional
    pool is the standard way to recover a sane net-long figure, and the
    residual of the float is the rough retail estimate the question asks for.
    """
    if not float_data:
        return None
    float_sh = float_data.get("float_shares") or 0
    so = float_data.get("shares_outstanding") or so_used
    if not (float_sh and so) or float_sh > so * 1.05:
        return None
    # Control block = the strategic/PE holders we identified (e.g. One Rock).
    # They sit OUTSIDE the float, so remove them from the institutional pool.
    control_sh = sum(h.shares for h in bundle.holders if h.bucket == pe_bucket)
    # Gross institutional shares: our measured reverse-13F sum, unless yfinance's
    # complete institutional % is larger (our sum can be capped on mega-caps
    # with thousands of filers — the vendor figure then keeps retail honest).
    inst_gross_sh = float(bundle.total_inst_shares)
    ip = float_data.get("institution_pct")
    if ip and ip * so > inst_gross_sh:
        inst_gross_sh = ip * so
    inst_float_sh = max(0.0, inst_gross_sh - control_sh)
    short_sh = (float_data.get("short_pct_float") or 0) * float_sh
    inst_net_sh = min(float_sh, max(0.0, inst_float_sh - short_sh))
    retail_sh = max(0.0, float_sh - inst_net_sh)
    nonfloat_sh = max(0.0, so - float_sh)
    return {
        "closely_held_pct": round(nonfloat_sh / so * 100, 1),
        "institutional_net_pct": round(inst_net_sh / so * 100, 1),
        "retail_pct": round(retail_sh / so * 100, 1),
        "float_pct": round(float_sh / so * 100, 1),
        "control_pct": round(control_sh / so * 100, 1),
        "insider_pct": round((float_data.get("insider_pct") or 0) * 100, 1),
        "short_pct_of_float": round((float_data.get("short_pct_float") or 0) * 100, 1),
        "inst_reported_pct": round(inst_gross_sh / so * 100, 1),
        "method": ("retail = public float − institutional (net of short interest); "
                   "closely-held = shares outside the public float"),
    }


def _step_ownership_holders(ctx: dict) -> dict:
    """
    Complete ownership picture for the stakeholder pies. Unlike
    crowding_assessment (which only sees our ~47 tracked 13F funds), this
    reverse-indexes the SEC full-text search on the security's CUSIP to find
    EVERY 13F manager holding the name, then layers in 5%+ holders that
    don't file 13F (13D/13G). Each holder is converted to % of shares
    outstanding and classified into an ownership bucket (PE/strategic, hedge
    fund, asset manager, index/passive, other) so the dashboard can draw a
    by-holder pie and a by-type pie that sum to 100% of the company.

    Sources, in order of authority per holder:
      - 13F (current quarter)        : uniform, fresh, complete institutional
      - 13D/13G (pct_of_class)       : only when MORE RECENT than the 13F, or
                                       when the holder files no 13F at all
    """
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    registry_data = ctx.get("registry_data") or {}
    crowding = ctx.get("crowding_assessment") or {}
    financials = ctx.get("financials") or {}
    d13 = ctx.get("filing_13d") or {}

    cusip = (crowding.get("cusip") or registry_data.get("cusip") or "").strip()
    if not cusip:
        # crowding only resolves a CUSIP for names our tracked funds hold; fall
        # back to a DB/efts resolver so any ticker can be looked up.
        cusip = (_resolve_cusip(ticker, registry_data.get("name") or "") or "").strip()
    if not cusip:
        return {"n_holders": 0, "holders": [], "buckets": [], "corpus_text": "",
                "error": "no CUSIP available (crowding_assessment / registry / resolver)"}

    # Float / short snapshot (for the retail decomposition + a basic shares
    # outstanding that's consistent with the float figure).
    float_data = _fetch_float_data(ticker)

    # Shares outstanding: prefer Yahoo basic shares (consistent with the float
    # figure and the standard denominator for ownership %); fall back to the
    # financials diluted count, then a 13D/13G filing that discloses both
    # shares and pct_of_class (shares / pct implies the class size).
    so = None
    so_source = ""
    if float_data.get("shares_outstanding"):
        so = float(float_data["shares_outstanding"])
        so_source = "yfinance (basic)"
    dsm = financials.get("diluted_shares_m")
    if so is None and dsm and dsm > 0:
        so = float(dsm) * 1e6
        so_source = "financials (diluted)"
    if so is None:
        for f in (d13.get("filings") or []):
            sh, pc = f.get("shares_held"), f.get("pct_of_class")
            if sh and pc and pc > 0:
                so = float(sh) / (pc / 100.0)
                so_source = f"implied from {f.get('filer_name', '13D')}"
                break

    try:
        from ingestion.loaders.edgar_13f_holders_loader import (
            fetch_all_13f_holders, classify_holder, BUCKET_ORDER, BUCKET_PE,
            BUCKET_FLOAT,
        )
    except Exception as e:
        return {"n_holders": 0, "holders": [], "buckets": [], "corpus_text": "",
                "error": f"import error: {type(e).__name__}: {e}"}

    # Per-run cold pipeline keeps a bounded cap so the step doesn't dominate
    # runtime on mega-caps; a thorough backfill (rerun_ownership.py) raises it
    # to fetch every filer. efts isn't size-ordered, so a cap below the true
    # filer count can miss large holders — truncation is flagged downstream.
    max_h = int(ctx.get("ownership_max_holders") or 800)
    try:
        bundle = fetch_all_13f_holders(cusip, ticker, max_holders=max_h, verbose=verbose)
    except Exception as e:
        return {"n_holders": 0, "holders": [], "buckets": [], "corpus_text": "",
                "error": f"{type(e).__name__}: {e}"}

    if so is None or so <= 0:
        # Can't convert shares -> %. Still surface raw error context.
        return {"n_holders": bundle.n_holders, "holders": [], "buckets": [],
                "cusip": cusip, "period_ending": bundle.period_ending,
                "corpus_text": "",
                "error": "shares outstanding unavailable; cannot compute % of class"}

    # ── 13F holders -> % of shares outstanding ──
    holders: dict[str, dict] = {}
    for h in bundle.holders:
        pct = h.shares / so * 100.0
        holders[h.name] = {
            "name": h.name, "bucket": h.bucket, "pct": round(pct, 2),
            "shares": h.shares, "value_m": round(h.value_usd / 1e6, 1),
            "source": "13F", "as_of": bundle.period_ending,
            "n_entities": h.n_entities,
        }

    # ── Layer in 13D/13G holders ──
    # Dedup 13D filings to the latest per filer family, then either supersede
    # a stale 13F entry (13D more recent) or add a non-13F holder outright.
    latest_13d: dict[str, dict] = {}
    for f in (d13.get("filings") or []):
        disp, _bk = classify_holder(f.get("filer_name", ""))
        cur = latest_13d.get(disp)
        if cur is None or (f.get("filed_date") or "") > (cur.get("filed_date") or ""):
            latest_13d[disp] = f
    for disp, f in latest_13d.items():
        pcl = f.get("pct_of_class")
        if pcl is None or pcl <= 0:
            continue
        _d, bucket = classify_holder(f.get("filer_name", ""))
        if f.get("activist_intent"):
            bucket = BUCKET_PE  # control / activist stake
        filed = f.get("filed_date") or ""
        existing = holders.get(disp)
        if existing is not None:
            # Same holder already in 13F. Use whichever disclosure is newer.
            if filed > (existing.get("as_of") or ""):
                existing.update(pct=round(float(pcl), 2), source="13D/13G",
                                as_of=filed, bucket=bucket)
            # else: keep the fresher 13F number (e.g. One Rock sold down).
        else:
            holders[disp] = {
                "name": disp, "bucket": bucket, "pct": round(float(pcl), 2),
                "shares": int(f.get("shares_held") or 0),
                "value_m": None, "source": "13D/13G", "as_of": filed,
                "n_entities": 1,
            }

    holder_list = sorted(holders.values(), key=lambda x: -(x["pct"] or 0))
    total_pct = sum(h["pct"] or 0 for h in holder_list)
    overlap_flag = total_pct > 100.5  # 13F shared-discretion double counting
    disclosed = min(total_pct, 100.0)
    float_pct = round(max(0.0, 100.0 - disclosed), 2)

    # Bucket aggregation (+ float).
    bucket_pct: dict[str, float] = {}
    for h in holder_list:
        bucket_pct[h["bucket"]] = bucket_pct.get(h["bucket"], 0.0) + (h["pct"] or 0)
    if overlap_flag:
        # Scale buckets down to 100% so the type pie stays a valid whole.
        scale = 100.0 / total_pct
        bucket_pct = {k: v * scale for k, v in bucket_pct.items()}
    else:
        bucket_pct[BUCKET_FLOAT] = float_pct
    buckets = [{"bucket": b, "pct": round(bucket_pct.get(b, 0.0), 2)}
               for b in BUCKET_ORDER if bucket_pct.get(b, 0.0) > 0.05]

    as_of_note = (f"13F holdings as of {bundle.period_ending}"
                  + (f"; {len(latest_13d)} 13D/13G filer(s) layered in" if latest_13d else "")
                  + (". Institutional holdings exceed 100% (13F shared-discretion "
                     "overlap) — buckets normalized." if overlap_flag else "."))

    decomposition = _ownership_decomposition(bundle, so, float_data, BUCKET_PE)

    out = {
        "ticker": ticker.upper(), "cusip": cusip,
        "period_ending": bundle.period_ending,
        "shares_outstanding_m": round(so / 1e6, 1), "so_source": so_source,
        "n_managers": bundle.n_managers, "n_holders": len(holder_list),
        "n_managers_total": bundle.n_managers_total, "truncated": bundle.truncated,
        "efts_total": getattr(bundle, "efts_total", 0),
        "n_fetch_failed": bundle.n_fetch_failed,
        "total_inst_pct": round(sum(h["pct"] for h in holder_list if h["source"] == "13F"), 1),
        "total_disclosed_pct": round(disclosed, 1),
        "float_pct": float_pct, "overlap_flag": overlap_flag,
        "holders": holder_list, "buckets": buckets,
        "decomposition": decomposition,
        "as_of_note": as_of_note,
        "corpus_text": _ownership_corpus_text(ticker, holder_list, buckets, float_pct,
                                               bundle.period_ending, so, decomposition),
        "fetched_at": bundle.fetched_at,
        "error": bundle.error,
    }
    return out


def _ownership_corpus_text(ticker, holders, buckets, float_pct, period, so, decomp=None) -> str:
    """Render the ownership structure as a labeled corpus block for the brief."""
    if not holders:
        return ""
    lines = [
        f"=== OWNERSHIP STRUCTURE ({ticker}, 13F as of {period}) ===",
        f"(All institutional + 5%+ holders, % of ~{so/1e6:,.0f}M shares outstanding.)",
        "",
        "Top holders:",
    ]
    for h in holders[:15]:
        src = "" if h["source"] == "13F" else f" [{h['source']}]"
        lines.append(f"  {h['name']:<30s} {h['pct']:>5.1f}%  ({h['bucket']}){src}")
    lines.append("")
    lines.append("By holder type:")
    for b in buckets:
        lines.append(f"  {b['bucket']:<24s} {b['pct']:>5.1f}%")
    if decomp:
        lines.append("")
        lines.append("Float decomposition (% of shares outstanding):")
        lines.append(f"  Closely held (strategic + insiders) {decomp['closely_held_pct']:>5.1f}%")
        lines.append(f"  Institutions (net of short interest) {decomp['institutional_net_pct']:>5.1f}%")
        lines.append(f"  Retail / other float                {decomp['retail_pct']:>5.1f}%")
        lines.append(f"  (public float {decomp['float_pct']:.0f}%, short interest "
                     f"{decomp['short_pct_of_float']:.0f}% of float, "
                     f"institutions report {decomp['inst_reported_pct']:.0f}% gross)")
    lines.append("=" * 56)
    return "\n".join(lines)


def _step_ir_press(ctx: dict) -> dict:
    """Press-release links from the company's IR / newsroom site (nicer UI than
    raw EDGAR exhibits) + product / company news. Best-effort across IR vendors;
    EDGAR (press_releases step) remains the complete-history fallback."""
    ticker = ctx["ticker"]
    try:
        from ingestion.loaders.ir_press_loader import fetch_ir_press
        return fetch_ir_press(ticker, max_items=60, verbose=ctx.get("verbose", False))
    except Exception as e:
        return {"items": [], "error": f"{type(e).__name__}: {e}"}


def _step_bond_health(ctx: dict) -> dict:
    """
    Issuer credit / bond health snapshot. Pulls outstanding bond series
    from the latest 10-K, fetches recent TRACE prices (when FINRA creds
    are present), computes G-spreads vs the Treasury curve, and tracks
    30d/90d trend + 1-stdev widening flags. Also surfaces the credit-
    equity divergence flag (avg spread widening while equity flat or up).
    Persists daily snapshots to workbench.db so trailing percentile
    context accumulates over time.

    Silent-but-useful when prices are missing: bond ladder still rendered
    with maturity / par / coupon for the brief.
    """
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from research.bond_health import assess_bond_health
        bundle = assess_bond_health(ticker, persist=True, verbose=verbose)
    except Exception as e:
        return {
            "bond_count": 0, "n_priced": 0, "corpus_text": "",
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "ticker": bundle.ticker,
        "issuer_cik": bundle.issuer_cik,
        "issuer_name": bundle.issuer_name,
        "snapshot_date": bundle.snapshot_date,
        "bond_count": bundle.bond_count,
        "n_priced": bundle.n_priced,
        "total_long_term_debt_m": bundle.total_long_term_debt_m,
        "auth_status": bundle.auth_status,
        "avg_g_spread_bps": bundle.avg_g_spread_bps,
        "avg_spread_30d_chg_bps": bundle.avg_spread_30d_chg_bps,
        "avg_spread_90d_chg_bps": bundle.avg_spread_90d_chg_bps,
        "n_widening_1stdev": bundle.n_widening_1stdev,
        "credit_equity_divergence_flag": bundle.credit_equity_divergence_flag,
        "equity_30d_chg_pct": bundle.equity_30d_chg_pct,
        "equity_90d_chg_pct": bundle.equity_90d_chg_pct,
        "findings": [
            {
                "series_label": f.series_label,
                "coupon_pct": f.coupon_pct,
                "par_amount_m": f.par_amount_m,
                "maturity_date": f.maturity_date,
                "is_callable": f.is_callable,
                "has_price": f.has_price,
                "last_price": f.last_price,
                "ytw_pct": f.ytw_pct,
                "g_spread_bps": f.g_spread_bps,
                "spread_30d_chg_bps": f.spread_30d_chg_bps,
                "spread_90d_chg_bps": f.spread_90d_chg_bps,
                "widen_1stdev_flag": f.widen_1stdev_flag,
                "price_source": f.price_source,
            }
            for f in bundle.findings
        ],
        "corpus_text": bundle.to_prompt_text(),
        "fetched_at": bundle.fetched_at,
        "error": bundle.error,
    }


def _step_filing_form4(ctx: dict) -> dict:
    """
    Per-ticker Form 4 lookup. Catches insider open-market transactions
    (sales / buys by directors, officers, 10%+ owners) with structured
    fields the news layer can't provide: shares sold, % of stake sold,
    post-transaction holdings. Critical for distinguishing "$50M sold by
    CEO" (sounds large) from "CEO sold 0.6% of his stake and retains
    $1B" (small relative to wealth). Window default 180 days.
    """
    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    try:
        from ingestion.loaders.edgar_form4_loader import fetch_form4_filings
        bundle = fetch_form4_filings(
            ticker, window_days=180, max_filings=100, verbose=verbose,
        )
    except Exception as e:
        return {
            "n_filings_total": 0, "corpus_text": "",
            "error": f"{type(e).__name__}: {e}",
        }
    # Best-effort: estimate each insider's net worth as their disclosed equity
    # across ALL companies they file Form 4s for (deterministic SEC aggregation,
    # cached per-CIK daily). This is a floor, not true net worth (it can miss
    # old untraded stakes and never sees private wealth). Gated by env so the
    # extra SEC crawl can be disabled; wrapped so it is never fatal to the step.
    import os as _os
    networth_by_cik: dict = {}
    if _os.environ.get("INSIDER_NETWORTH", "1") != "0":
        try:
            from research.insider_networth import estimate_networth
            for a in bundle.aggregates[:12]:
                if a.filer_cik and a.filer_cik not in networth_by_cik:
                    try:
                        networth_by_cik[a.filer_cik] = estimate_networth(
                            a.filer_cik, verbose=verbose)
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        "ticker": bundle.ticker,
        "issuer_cik": bundle.issuer_cik,
        "issuer_name": bundle.issuer_name,
        "window_days": bundle.window_days,
        "n_filings_total": bundle.n_filings_total,
        "n_unique_insiders": len(bundle.aggregates),
        "aggregates": [
            {
                "filer_name": a.filer_name,
                "filer_cik": a.filer_cik,
                "relationship": a.relationship,
                "sale_shares": a.sale_shares,
                "sale_value": a.sale_value,
                "sale_avg_price": a.sale_avg_price,
                "buy_shares": a.buy_shares,
                "buy_value": a.buy_value,
                "buy_avg_price": a.buy_avg_price,
                "shares_held_post_latest": a.shares_held_post_latest,
                "pct_of_stake_sold": a.pct_of_stake_sold,
                "pct_of_stake_value_sold": a.pct_of_stake_value_sold,
                "approx_stake_value_remaining": a.approx_stake_value_remaining,
                "networth": networth_by_cik.get(a.filer_cik),
            }
            for a in bundle.aggregates
        ],
        "corpus_text": bundle.to_prompt_text(),
        "fetched_at": bundle.fetched_at,
        "error": bundle.error,
    }


def _step_crowding_assessment(ctx: dict) -> dict:
    """
    13F institutional crowding lookup. Reads the persistent workbench DB
    populated by `python cli.py refresh-13f` (offline, quarterly). Computes
    AUM-weighted crowding score, entry/exit trend, historical + peer
    percentiles, and top holders for the ticker.

    The result feeds corpus_assembly so the research brief sees ownership
    structure as part of the thesis (PE crowding, fund accumulation /
    distribution, co-investor overhang).
    """
    import asyncio
    from pathlib import Path

    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}
    verbose = ctx.get("verbose", False)
    cik_hint = (registry_data or {}).get("cik", "") or ""
    cusip_hint = (registry_data or {}).get("cusip")

    db_path = Path("data/workbench.db")
    if not db_path.exists():
        return {
            "data_quality": "none",
            "corpus_text": "",
            "variant_signal": (
                "13F crowding unavailable: workbench.db not initialized. "
                "Run `python cli.py refresh-13f` to populate."
            ),
            "caveats": ["workbench.db missing"],
        }

    # Resolve the SEC-canonical company name. registry_data.name is often
    # missing or short ("PRMB"); we need the full "PRIMO BRANDS CORP" so
    # the fuzzy CUSIP lookup in crowding_analysis (which does a substring
    # LIKE on holding_13f.issuer_name) actually matches.
    try:
        from ingestion.loaders.edgar_13d_loader import resolve_ticker_to_cik
        sec_resolved = resolve_ticker_to_cik(ticker, verbose=False)
    except Exception:
        sec_resolved = None
    if sec_resolved:
        sec_cik, sec_name = sec_resolved
        company_name = sec_name or ((registry_data or {}).get("name") or ticker)
        if not cik_hint:
            cik_hint = sec_cik
    else:
        company_name = (registry_data or {}).get("name") or ticker

    try:
        from core.provenance.database import init_db, RunContext, new_id
        from research.crowding_analysis import get_crowding_for_ticker
    except Exception as e:
        return {
            "data_quality": "none", "corpus_text": "",
            "variant_signal": f"crowding import error: {type(e).__name__}: {e}",
        }

    conn = init_db(db_path)
    try:
        # Ensure the company row exists in the persistent DB with the
        # full SEC name so the fuzzy CUSIP lookup downstream works. If the
        # row already exists with a degraded name (e.g. just the ticker),
        # upgrade it.
        existing = conn.execute(
            "SELECT company_id, name FROM company WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
        if not existing:
            cid = new_id()
            conn.execute(
                "INSERT INTO company (company_id, name, ticker, cik, sic_code) "
                "VALUES (?,?,?,?,?)",
                (cid, company_name, ticker.upper(), cik_hint, ""),
            )
            conn.commit()
        elif existing[1] != company_name and len(company_name) > len(existing[1] or ""):
            # Upgrade ticker-only name to the SEC canonical name
            conn.execute(
                "UPDATE company SET name = ?, cik = COALESCE(NULLIF(cik, ''), ?) "
                "WHERE company_id = ?",
                (company_name, cik_hint, existing[0]),
            )
            conn.commit()

        with RunContext(conn, "crowding_lookup", {"ticker": ticker}) as run:
            ca = asyncio.run(get_crowding_for_ticker(
                conn, ticker, run.run_id,
                cusip=cusip_hint, force_refresh=False,
            ))

        if ca.data_quality == "none":
            return {
                "data_quality": "none",
                "corpus_text": "",
                "variant_signal": ca.variant_signal,
                "caveats": list(ca.caveats),
            }

        def _holder_dict(h):
            return {
                "fund_name": h.fund_name, "fund_type": h.fund_type,
                "value_m": h.value_m, "pct_of_fund": h.pct_of_fund,
                "shares": h.shares, "quarters_held": h.quarters_held,
                "entry_exit": h.entry_exit,
                "prior_value_m": h.prior_value_m,
                "delta_value_m": h.delta_value_m,
                "delta_pct": h.delta_pct,
            }
        return {
            "ticker": ca.ticker,
            "cusip": ca.cusip,
            "report_date": ca.report_date,
            "latest_filed_quarter": ca.latest_filed_quarter,
            "fully_reported_quarter": ca.fully_reported_quarter,
            "funds_filed_latest": ca.funds_filed_latest,
            "funds_filed_fully_reported": ca.funds_filed_fully_reported,
            "prior_quarter": ca.prior_quarter,
            "funds_holding": ca.funds_holding,
            "funds_tracked": ca.funds_tracked,
            "ownership_pct": ca.ownership_pct,
            "weighted_score": ca.weighted_score,
            "crowding_level": ca.crowding_level,
            "net_entries": ca.net_entries,
            "net_exits": ca.net_exits,
            "entry_exit_trend": ca.entry_exit_trend,
            "historical_percentile": ca.historical_percentile,
            "peer_percentile": ca.peer_percentile,
            "quarters_of_history": ca.quarters_of_history,
            "avg_position_pct": ca.avg_position_pct,
            "top_holders": [_holder_dict(h) for h in ca.top_holders],
            "top_buyers": [_holder_dict(h) for h in ca.top_buyers],
            "top_sellers": [_holder_dict(h) for h in ca.top_sellers],
            "variant_signal": ca.variant_signal,
            "data_quality": ca.data_quality,
            "caveats": list(ca.caveats),
            "corpus_text": _format_crowding_for_brief(ca),
        }
    except Exception as e:
        return {
            "data_quality": "error",
            "corpus_text": "",
            "variant_signal": f"crowding lookup error: {type(e).__name__}: {e}",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _format_crowding_for_brief(ca) -> str:
    """
    Render a CrowdingAssessment as a labeled corpus block for the brief
    prompt. Surfaces the per-fund QoQ disagreement (top buyers vs top
    sellers) which is much richer signal than the static "X of N hold"
    summary alone.
    """
    if ca.data_quality == "none":
        return ""

    # Header with the fully-reported reference quarter (which may differ
    # from the latest filed quarter when Q-end filings are still arriving)
    ref_q = ca.fully_reported_quarter or ca.report_date
    header = (
        f"=== INSTITUTIONAL CROWDING (13F) — reference quarter {ref_q} ==="
        if ref_q else "=== INSTITUTIONAL CROWDING (13F) ==="
    )
    lines = [
        header,
        f"(AUM-weighted across {ca.funds_tracked} tracked hedge funds. "
        f"Trend metrics use the most recent fully-reported quarter to avoid "
        f"in-flight filing bias.)",
        "",
        f"Crowding score: {ca.weighted_score:.0f}/100 ({ca.crowding_level})",
        f"Funds holding: {ca.funds_holding}/{ca.funds_tracked} tracked "
        f"({ca.ownership_pct:.1f}%). Avg position: {ca.avg_position_pct:.2f}% of fund portfolio.",
    ]
    # Show net-entries/exits relative to ca.prior_quarter, not just "latest"
    if ca.prior_quarter:
        lines.append(
            f"Quarter trend ({ca.prior_quarter} → {ref_q}): "
            f"+{ca.net_entries} new entries, -{ca.net_exits} full exits "
            f"→ {ca.entry_exit_trend}"
        )
    else:
        lines.append(
            f"Quarter trend: +{ca.net_entries} entries, -{ca.net_exits} exits "
            f"→ {ca.entry_exit_trend}"
        )
    if ca.historical_percentile is not None:
        lines.append(
            f"Historical: {ca.historical_percentile:.0f}th percentile vs own "
            f"{ca.quarters_of_history}Q history (higher = more crowded than usual)"
        )
    if ca.peer_percentile is not None:
        lines.append(
            f"Peer percentile: {ca.peer_percentile:.0f}th vs sector peers"
        )

    # Per-fund flow — the actual disagreement
    if ca.top_buyers or ca.top_sellers:
        lines.append("")
        lines.append(
            f"Per-fund QoQ flow ({ca.prior_quarter} → {ref_q}, "
            f"funds with |delta| >= $0.5M):"
        )
        if ca.top_buyers:
            lines.append("  Top buyers (added position):")
            for h in ca.top_buyers:
                pct = (f"+{h.delta_pct:.0f}%" if h.delta_pct is not None
                        else "NEW")
                tag = h.entry_exit.replace("_", " ")
                lines.append(
                    f"    [{tag:>9}] {h.fund_name:<32s} "
                    f"+${h.delta_value_m:>7.1f}M  ({pct})"
                )
        if ca.top_sellers:
            lines.append("  Top sellers (cut position):")
            for h in ca.top_sellers:
                pct = (f"{h.delta_pct:.0f}%" if h.delta_pct is not None
                        else "-100% (exit)")
                tag = h.entry_exit.replace("_", " ")
                lines.append(
                    f"    [{tag:>9}] {h.fund_name:<32s} "
                    f"${h.delta_value_m:>+8.1f}M  ({pct})"
                )

    if ca.top_holders:
        lines.append("")
        lines.append(
            f"Top holders by current $ value ({ref_q}, top 8):"
        )
        for h in ca.top_holders[:8]:
            status = (h.entry_exit or "HOLD").replace("_", " ")
            lines.append(
                f"  [{status:>9}] {h.fund_name:<32s} "
                f"${h.value_m:>8.1f}M  "
                f"({h.pct_of_fund:.1f}% of fund, held {h.quarters_held}Q)"
            )

    lines.append("")
    lines.append(f"Variant signal: {ca.variant_signal}")
    if ca.caveats:
        for c in ca.caveats[:4]:
            lines.append(f"Caveat: {c}")
    lines.append("=" * 60)
    return "\n".join(lines)


def _step_guidance_bundle(ctx: dict) -> dict:
    """
    Aggregate management guidance from transcript_digest's guidance subagent +
    deck_digest's guidance subagent + press releases. Returns the bundle
    serialized as a dict (reconstructable into GuidanceBundle on the consumer).
    """
    ticker = ctx["ticker"]
    transcript_digest = ctx.get("transcript_digest")  # dict | None
    slide_decks = ctx.get("slide_decks") or {}
    press_releases = ctx.get("press_releases") or []

    # Use the most recent deck digest as the primary structured-guide source
    deck_digests = slide_decks.get("digests") or []
    primary_deck = deck_digests[0] if deck_digests else None

    try:
        from research.guidance_extractor import extract_guidance
        bundle = extract_guidance(
            ticker=ticker,
            transcript_digest=transcript_digest,
            deck_digest=primary_deck,
            press_releases=press_releases,
        )
    except Exception as e:
        return {
            "ticker": (ticker or "").upper(),
            "items": [], "extracted_at": "",
            "sources_used": [], "notes": [],
            "error": f"{type(e).__name__}: {e}",
        }
    return bundle.to_dict()


# --------------------------------------------------------------------------
# Step implementations — Tier 2 (corpus assembly + the central brief call)
# --------------------------------------------------------------------------

def _step_corpus_assembly(ctx: dict) -> dict:
    """
    Concatenate all DAG corpus sources into the brief's filing_text mega-blob,
    AND keep each piece labeled separately for the adversarial corpus dict.
    Pure transformation — no I/O, no Claude — so cache hits are essentially
    free. The expensive thing this enables is caching `research_brief` on the
    same content hash.

    Returns:
      {
        "filing_text":            big concatenated blob → build_research_brief
        "raw_filing_text":        registry seed + EDGAR only (for adv corpus)
        "transcripts_corpus_text": TranscriptDigest.to_prompt_text()
        "press_corpus_text":      top-4 press releases concatenated
        "deck_corpus_text":       slide_decks.corpus_text (passthrough)
        "macro_corpus_text":      fred_macro.corpus_text (passthrough)
        "bls_corpus_text":        bls_macro.corpus_text  (passthrough)
        "bea_corpus_text":        bea_macro.corpus_text  (passthrough)
        "peer_corpus_text":       peer_comps.corpus_text (passthrough)
        "quarterly_corpus_text":  quarterly_financials.corpus_text (passthrough)
        "news_corpus_text":       news.corpus_text (passthrough)
        "bear_research_text":     bear_research.corpus_text (passthrough)
      }
    """
    from dataclasses import fields as _dc_fields

    ticker = ctx["ticker"]
    registry_data = ctx.get("registry_data") or {}

    # ── 1. Filing text seed: registry_data.earnings_text + EDGAR body ──
    fin_dict = ctx.get("financials") or {}
    revenue_m = fin_dict.get("revenue_m") or 0.0
    diluted_eps = fin_dict.get("diluted_eps") or 0.0

    filing_text = registry_data.get("earnings_text", "") if registry_data else ""
    ft_dict = ctx.get("filing_text") or {}
    ft_body = ft_dict.get("text", "") if isinstance(ft_dict, dict) else ""
    if ft_body:
        filing_text = (filing_text + "\n\n" + ft_body).strip() if filing_text else ft_body
    if not filing_text:
        # Last-resort placeholder so downstream isn't fed an empty string
        filing_text = (f"{ticker} fiscal year results. "
                       f"Revenue ${revenue_m:,.1f}M. "
                       f"EPS ${diluted_eps:.2f}.")
    raw_filing_text = filing_text  # snapshot pre-corpus pile

    # ── 2. Raw transcripts (verbatim text) ──
    tr_dict = ctx.get("transcripts") or {}
    tr_text = tr_dict.get("text", "") if isinstance(tr_dict, dict) else ""
    if tr_text:
        filing_text = (
            filing_text
            + "\n\nEARNINGS CALL TRANSCRIPTS (3 YEARS):\n"
            + tr_text
        )

    # ── 3. Transcript digest (8-subagent decomposition) ──
    transcripts_corpus_text = ""
    td_dict = ctx.get("transcript_digest")
    if td_dict:
        try:
            from research.transcript_analyzer import TranscriptDigest
            td_fields = {f.name for f in _dc_fields(TranscriptDigest)}
            ta = TranscriptDigest(
                **{k: v for k, v in td_dict.items() if k in td_fields}
            )
            ta._derive()
            transcripts_corpus_text = ta.to_prompt_text()
            if transcripts_corpus_text:
                filing_text = filing_text + "\n\n" + transcripts_corpus_text
        except Exception:
            pass

    # ── 4–9. Pre-formatted corpus pieces from other DAG steps ──
    deck_corpus_text = (ctx.get("slide_decks") or {}).get("corpus_text", "") or ""
    if deck_corpus_text:
        filing_text = filing_text + "\n\n" + deck_corpus_text

    macro_corpus_text = (ctx.get("fred_macro") or {}).get("corpus_text", "") or ""
    if macro_corpus_text:
        filing_text = filing_text + "\n\n" + macro_corpus_text

    bls_corpus_text = (ctx.get("bls_macro") or {}).get("corpus_text", "") or ""
    if bls_corpus_text:
        filing_text = filing_text + "\n\n" + bls_corpus_text

    bea_corpus_text = (ctx.get("bea_macro") or {}).get("corpus_text", "") or ""
    if bea_corpus_text:
        filing_text = filing_text + "\n\n" + bea_corpus_text

    peer_corpus_text = (ctx.get("peer_comps") or {}).get("corpus_text", "") or ""
    if peer_corpus_text:
        filing_text = filing_text + "\n\n" + peer_corpus_text

    quarterly_corpus_text = (ctx.get("quarterly_financials") or {}).get("corpus_text", "") or ""
    if quarterly_corpus_text:
        filing_text = filing_text + "\n\n" + quarterly_corpus_text

    news_corpus_text = (ctx.get("news") or {}).get("corpus_text", "") or ""
    if news_corpus_text:
        filing_text = filing_text + "\n\n" + news_corpus_text

    bear_research_text = (ctx.get("bear_research") or {}).get("corpus_text", "") or ""
    if bear_research_text:
        filing_text = filing_text + "\n\n" + bear_research_text

    crowding_corpus_text = (ctx.get("crowding_assessment") or {}).get("corpus_text", "") or ""
    if crowding_corpus_text:
        filing_text = filing_text + "\n\n" + crowding_corpus_text

    filing_13d_text = (ctx.get("filing_13d") or {}).get("corpus_text", "") or ""
    if filing_13d_text:
        filing_text = filing_text + "\n\n" + filing_13d_text

    form4_text = (ctx.get("filing_form4") or {}).get("corpus_text", "") or ""
    if form4_text:
        filing_text = filing_text + "\n\n" + form4_text

    bond_health_text = (ctx.get("bond_health") or {}).get("corpus_text", "") or ""
    if bond_health_text:
        filing_text = filing_text + "\n\n" + bond_health_text

    stocktwits_text = (ctx.get("stocktwits") or {}).get("corpus_text", "") or ""
    if stocktwits_text:
        filing_text = filing_text + "\n\n" + stocktwits_text

    social_topic_text = (ctx.get("social_topic_analysis") or {}).get("corpus_text", "") or ""
    if social_topic_text:
        filing_text = filing_text + "\n\n" + social_topic_text

    # ── 10. Press releases (NOT added to filing_text — adv-corpus only) ──
    # Keep releases and supplements as SEPARATE budgets so the operating
    # supplement (comp-sales / segment tables in Ex 99.2) augments rather than
    # displaces the quarterly earnings releases. 6 release quarters + the 3 most
    # recent supplements, each labeled, newest first.
    pr_dicts = [p for p in (ctx.get("press_releases") or []) if isinstance(p, dict)]
    releases = [p for p in pr_dicts if p.get("kind", "release") != "supplement"]
    supplements = [p for p in pr_dicts if p.get("kind") == "supplement"]
    selected = releases[:6] + supplements[:3]
    press_corpus_text = ""
    if selected:
        selected.sort(
            key=lambda p: str(p.get("report_date") or p.get("filing_date") or ""),
            reverse=True,
        )
        pr_parts: list[str] = []
        for pr in selected:
            kind_lbl = "SUPPLEMENT" if pr.get("kind") == "supplement" else "RELEASE"
            header = (
                f"=== {pr.get('ticker','?')} {pr.get('quarter','?')} {kind_lbl} "
                f"({pr.get('report_date','?')}) ==="
            )
            body = (pr.get("full_text_with_tables") or pr.get("text") or "")[:5000]
            if body:
                pr_parts.append(f"{header}\n{body}")
        press_corpus_text = "\n\n".join(pr_parts)

    # ── 11. Ownership structure (all-holders reverse-13F + 13D/13G + float) ──
    # The brief should reason over WHO actually holds the company — activist/PE
    # overhangs, float, retail — not just our ~47 tracked funds (which is all
    # crowding_assessment sees). The ownership_holders step reverse-indexes every
    # 13F manager on the CUSIP and layers in 5%+ filers; append its labeled block.
    ownership_structure_text = (ctx.get("ownership_holders") or {}).get("corpus_text", "") or ""
    if ownership_structure_text:
        filing_text = filing_text + "\n\n" + ownership_structure_text

    # ── 12. External research (independent analysts / newsletters, tier 1-2) ──
    # Appended to filing_text so the brief reasons over it as variant perception
    # alongside our own corpus.
    external_research_text = (ctx.get("external_research") or {}).get("corpus_text", "") or ""
    if external_research_text:
        filing_text = filing_text + "\n\n" + external_research_text

    return {
        "filing_text": filing_text,
        "raw_filing_text": raw_filing_text,
        "transcripts_corpus_text": transcripts_corpus_text,
        "deck_corpus_text": deck_corpus_text,
        "press_corpus_text": press_corpus_text,
        "ownership_structure_text": ownership_structure_text,
        "external_research_text": external_research_text,
        "macro_corpus_text": macro_corpus_text,
        "bls_corpus_text": bls_corpus_text,
        "bea_corpus_text": bea_corpus_text,
        "peer_corpus_text": peer_corpus_text,
        "quarterly_corpus_text": quarterly_corpus_text,
        "news_corpus_text": news_corpus_text,
        "bear_research_text": bear_research_text,
        "crowding_corpus_text": crowding_corpus_text,
        "filing_13d_text": filing_13d_text,
        "form4_text": form4_text,
        "bond_health_text": bond_health_text,
        "stocktwits_text": stocktwits_text,
        "social_topic_text": social_topic_text,
        "total_chars": len(filing_text),
    }


# Bump this constant when the brief prompt changes meaningfully
# (research/deep_research._build_prompt). Bumping it invalidates the
# brief cache so a stale prior brief doesn't mask a prompt regression.
_BRIEF_PROMPT_VERSION = "v2"  # v2: narrative sequential-math repair pass


def _step_claim_verifications(ctx: dict) -> list[dict]:
    """
    Hawkish post-brief research pass. Walks every quantified claim,
    forward-looking timeline, named risk, and inferred ownership/management/
    macro angle in the brief; runs targeted DDG searches + a Haiku summary
    per topic; returns inline verification records that the Word renderer
    surfaces as sub-bullets under each driver / risk / synthesis paragraph.

    Returns a list of dicts (Verification.to_dict()).
    """
    from dataclasses import fields as _dc_fields
    from research.claim_verifier import run_all_verifications
    from research.deep_research import ResearchBrief

    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)
    registry_data = ctx.get("registry_data") or {}

    brief_dict = ctx.get("research_brief") or {}
    if not brief_dict:
        return []

    brief_fields = {f.name for f in _dc_fields(ResearchBrief)}
    try:
        brief = ResearchBrief(
            **{k: v for k, v in brief_dict.items() if k in brief_fields}
        )
    except Exception:
        return []

    verifications = run_all_verifications(
        brief=brief, ticker=ticker,
        registry_data=registry_data, verbose=verbose,
    )
    return [v.to_dict() for v in verifications]


def _step_research_brief(ctx: dict) -> dict:
    """
    The central rich Claude call. Produces a ResearchBrief: structured
    edge_claims + multi-paragraph narrative_synthesis + drivers +
    contradictions + bear_revisions. Returns the brief as a dict (asdict
    round-trip; ResearchBrief has no nested dataclasses).
    """
    from dataclasses import asdict, fields as _dc_fields
    from research.deep_research import build_research_brief, ResearchBrief
    from research.financials_fetcher import StructuredFinancials

    ticker = ctx["ticker"]
    verbose = ctx.get("verbose", False)

    # Reconstruct StructuredFinancials from the DAG dict
    fin_dict = ctx.get("financials") or {}
    if not fin_dict:
        return asdict(ResearchBrief(source_method="claude_api_error"))
    fin_fields = {f.name for f in _dc_fields(StructuredFinancials)}
    financials = StructuredFinancials(
        **{k: v for k, v in fin_dict.items() if k in fin_fields}
    )

    # Pull consensus pieces (legacy + full snapshot)
    cons_wrap = ctx.get("consensus") or {}
    consensus = cons_wrap.get("consensus") or {}
    cons_eps = consensus.get("eps")
    cons_revenue_m = consensus.get("revenue_m")
    consensus_full_dict = cons_wrap.get("full")

    # Reconstruct the GuidanceBundle dataclass
    gb_dict = ctx.get("guidance_bundle") or {}
    guidance_bundle = None
    if gb_dict and not gb_dict.get("error"):
        try:
            from research.guidance_extractor import GuidanceBundle, GuidanceItem
            item_fields = {f for f in GuidanceItem.__dataclass_fields__}
            items = [
                GuidanceItem(**{k: v for k, v in i.items() if k in item_fields})
                for i in (gb_dict.get("items") or [])
            ]
            bundle_fields = {
                f for f in GuidanceBundle.__dataclass_fields__ if f != "items"
            }
            kwargs = {k: v for k, v in gb_dict.items() if k in bundle_fields}
            guidance_bundle = GuidanceBundle(items=items, **kwargs)
        except Exception:
            guidance_bundle = None

    filing_text = (ctx.get("corpus_assembly") or {}).get("filing_text", "")

    brief = build_research_brief(
        ticker=ticker,
        financials=financials,
        earnings_text=filing_text,
        consensus_eps=cons_eps,
        consensus_revenue_m=cons_revenue_m,
        consensus_full=consensus_full_dict,
        guidance_bundle=guidance_bundle,
        verbose=verbose,
    )
    return asdict(brief)


def _research_brief_key(ctx: dict) -> str:
    """Hash all brief inputs + the prompt version. Brief invalidates
    automatically when corpus_assembly changes, when consensus revises,
    when financials update, or when the prompt is bumped."""
    return stable_hash(
        ctx.get("ticker", ""),
        ctx.get("corpus_assembly"),
        ctx.get("financials"),
        ctx.get("consensus"),
        ctx.get("guidance_bundle"),
        _BRIEF_PROMPT_VERSION,
    )


# --------------------------------------------------------------------------
# Cache-key helpers
# --------------------------------------------------------------------------

def _ticker_key(ctx: dict) -> str:
    """Cache key that depends only on the ticker. Re-run via --force."""
    return stable_hash(ctx.get("ticker", ""))


def _daily_ticker_key(ctx: dict) -> str:
    """Cache key that invalidates daily — for consensus/overlay which change.
    Includes a schema version (`v2` post-edge-pipeline-restructure) so old
    caches with `full: null` are automatically invalidated."""
    return stable_hash(ctx.get("ticker", ""), date.today().isoformat(), "v2")


def _daily_global_key(ctx: dict) -> str:
    """Cache key that invalidates daily, with NO ticker — for shared macro
    data (FRED / BLS / BEA) where the same payload applies to every ticker."""
    return stable_hash(date.today().isoformat(), "v2")


def _weekly_ticker_key(ctx: dict) -> str:
    """Cache key that invalidates weekly — for sticky data like short-seller
    research that doesn't change between earnings cycles."""
    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    return stable_hash(ctx.get("ticker", ""), f"{iso_year}-W{iso_week:02d}", "v2")


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
    Return the set of Steps for the fetch + first-pass-analysis layer of
    the pipeline. After Tier 1 expansion, this covers everything before
    the research brief: 6 fetches + transcript digest + slide decks + 4
    macro/peer/news/bear fetches + guidance bundle.

    Downstream (research brief, EPS bridge, adversarial, edge, valuation,
    decision) stays in `pipeline.run_research()` for now — Tier 2 will
    pull the brief + adversarial in.
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
        Step(
            name="external_research",
            inputs=[],
            run=_step_external_research,
            # Inbox research accumulates as new emails are classified — refresh daily.
            cache_key=_daily_ticker_key,
        ),

        # ─── Tier 1: more independent fetches that previously ran
        #     sequentially in run_research, now parallel ───
        Step(
            name="fred_macro",
            inputs=[],
            run=_step_fred_macro,
            # Macro shifts slowly; FRED loader caches daily globally.
            cache_key=_daily_global_key,
        ),
        Step(
            name="bls_macro",
            inputs=[],
            run=_step_bls_macro,
            cache_key=_daily_global_key,
        ),
        Step(
            name="bea_macro",
            inputs=[],
            run=_step_bea_macro,
            cache_key=_daily_global_key,
        ),
        Step(
            name="peer_comps",
            inputs=[],
            run=_step_peer_comps,
            # Schema inference via yfinance + per-peer consensus fetches.
            # Brief.schema_type may differ from the inferred schema; the
            # post-brief retry in run_research handles that case.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="quarterly_financials",
            inputs=[],
            run=_step_quarterly_financials,
            # New quarter prints daily.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="news",
            inputs=[],
            run=_step_news,
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="bear_research",
            inputs=[],
            run=_step_bear_research,
            # Sticky — bear reports drop infrequently. Weekly cache
            # avoids hitting the same scrapers on every run.
            cache_key=_weekly_ticker_key,
        ),
        Step(
            name="crowding_assessment",
            inputs=[],
            run=_step_crowding_assessment,
            # Read-only query against the persistent workbench.db that
            # `python cli.py refresh-13f` populates offline. The data only
            # changes quarterly; daily-ticker key keeps the query fast
            # without staling between refreshes.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="filing_13d",
            inputs=[],
            run=_step_filing_13d,
            # 5%+ holder filings are sticky — drop only when ownership
            # crosses thresholds or holders amend. Weekly cache is fine.
            cache_key=_weekly_ticker_key,
        ),
        Step(
            name="ownership_holders",
            # Reverse-13F-by-CUSIP. Needs the CUSIP (from crowding), shares
            # outstanding (from financials), and the 13D/13G filers to layer
            # in non-13F 5%+ holders.
            inputs=["crowding_assessment", "financials", "filing_13d"],
            run=_step_ownership_holders,
            # ~350 SEC info-table fetches on a popular name — heavy but the
            # underlying 13F data only changes quarterly, so weekly cache.
            cache_key=_weekly_ticker_key,
        ),
        Step(
            name="ir_press",
            inputs=[],
            run=_step_ir_press,
            # IR newsroom links are sticky — weekly cache (browser-fetch is heavy).
            cache_key=_weekly_ticker_key,
        ),
        Step(
            name="filing_form4",
            inputs=[],
            run=_step_filing_form4,
            # Form 4s drop daily on active names but the prior week's
            # snapshot already captures the meaningful insider trends.
            # Weekly cache keeps EDGAR traffic low without losing signal.
            cache_key=_weekly_ticker_key,
        ),
        Step(
            name="bond_health",
            inputs=[],
            run=_step_bond_health,
            # Daily — TRACE prices update daily. The bond universe (10-K
            # debt schedule) only changes annually but is cheap to re-pull
            # alongside the price refresh.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="stocktwits",
            inputs=[],
            run=_step_stocktwits,
            # Retail dialogue moves fast but daily cache is plenty for
            # orientation; no need to re-fetch per run.
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="social_topic_analysis",
            inputs=["stocktwits", "transcripts"],
            run=_step_social_topic_analysis,
            # Cache on content hash of inputs — if either retail dialogue
            # or the transcript snapshot changes, re-cluster.
            cache_key=_content_hash_key("stocktwits", "transcripts"),
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
        Step(
            name="slide_decks",
            # Depends on transcript_digest + press_releases for the
            # auto_cross_reference path inside analyze_slide_deck (it
            # reads cached digests off disk to label deck guidance as
            # incremental vs. cross-confirmed).
            inputs=["transcript_digest", "press_releases"],
            run=_step_slide_decks,
            cache_key=_daily_ticker_key,
        ),
        Step(
            name="guidance_bundle",
            inputs=["transcript_digest", "slide_decks", "press_releases"],
            run=_step_guidance_bundle,
            # Pure transformation; key on content of source digests.
            cache_key=_content_hash_key("transcript_digest", "slide_decks", "press_releases"),
        ),

        # ─── Tier 2: corpus assembly + the central brief Claude call ───
        Step(
            name="corpus_assembly",
            inputs=[
                # Everything the brief prompt's filing_text concatenates,
                # plus what the adversarial step reads as labeled corpus.
                "financials", "filing_text", "transcripts", "transcript_digest",
                "slide_decks", "fred_macro", "bls_macro", "bea_macro",
                "peer_comps", "quarterly_financials", "news", "bear_research",
                "crowding_assessment", "filing_13d", "filing_form4",
                "bond_health", "stocktwits", "social_topic_analysis",
                "press_releases", "ownership_holders", "external_research",
            ],
            run=_step_corpus_assembly,
            # Pure string concat — content hash on every input ensures
            # corpus_assembly's cache invalidates when ANY upstream changes.
            cache_key=_content_hash_key(
                "financials", "filing_text", "transcripts", "transcript_digest",
                "slide_decks", "fred_macro", "bls_macro", "bea_macro",
                "peer_comps", "quarterly_financials", "news", "bear_research",
                "crowding_assessment", "filing_13d", "filing_form4",
                "bond_health", "stocktwits", "social_topic_analysis",
                "press_releases", "ownership_holders", "external_research",
            ),
        ),
        Step(
            name="research_brief",
            inputs=["corpus_assembly", "financials", "consensus", "guidance_bundle"],
            run=_step_research_brief,
            # Hash of all inputs + prompt version. Cache hits when re-running
            # within the same day on the same ticker (consensus has daily
            # cache; corpus_assembly is content-hashed on its inputs).
            cache_key=_research_brief_key,
        ),
        Step(
            name="claim_verifications",
            inputs=["research_brief"],
            run=_step_claim_verifications,
            # Cache on brief content + verification version constant. Bump
            # VERIFICATION_VERSION in claim_verifier.py to invalidate.
            cache_key=lambda ctx: stable_hash(
                ctx.get("ticker", ""),
                ctx.get("research_brief"),
                # Import at call time to avoid module-load circularity
                __import__("research.claim_verifier",
                           fromlist=["VERIFICATION_VERSION"]).VERIFICATION_VERSION,
            ),
        ),
    ]
