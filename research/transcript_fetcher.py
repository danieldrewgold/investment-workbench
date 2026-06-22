"""
Earnings Call Transcript Fetcher

Fetches earnings call transcripts via EarningsCall.biz API.
Supports fetching 3 years of quarterly transcripts for deep research.

Requires: pip install earningscall
API key: set ECALL_API_KEY env var or pass directly

The transcripts are cached locally by the earningscall library
in a SQLite database, so each transcript is only fetched once.
"""

import os

ECALL_API_KEY = os.environ.get("ECALL_API_KEY", "") or os.environ.get(
    "EARNINGSCALL_API_KEY",
    "***REMOVED***"
)

MAX_TRANSCRIPT_PER_QUARTER = 15000   # Chars per quarter to keep prompt manageable
MAX_TOTAL_TRANSCRIPT = 40000         # Total chars across all quarters for Claude prompt


def fetch_transcript_history(ticker: str, quarters: int = 12, verbose: bool = False) -> str | None:
    """
    Produce the condensed transcript digest for Claude's research brief — key
    excerpts (prepared-remarks opener + financial mentions + Q&A) per quarter,
    capped to the brief's token budget. Built from the full transcripts.
    """
    raw = fetch_full_transcripts(ticker, quarters=quarters, verbose=verbose)
    if not raw:
        return None
    digest = _digest_quarters(raw)
    if verbose and digest:
        print(f"  Transcript: digest {len(digest):,} chars from {len(raw)} quarters")
    return digest


import re as _re

# Operator phrases that mark the START of the Q&A session (not the preamble's
# "there will be an opportunity to ask questions"). Used to split prepared
# remarks from Q&A in the speaker turns.
_QA_START_RE = _re.compile(
    r"(question[-\s]and[-\s]answer\s+session|we (?:will|'?ll)\s+now\s+(?:begin|open|take)"
    r"|(?:our\s+)?first\s+question\s+(?:comes|is\s+from)|comes\s+from\s+the\s+line\s+of"
    r"|open\s+(?:up\s+)?the\s+(?:call|floor|line)\s+(?:for|to)\s+question"
    r"|begin\s+the\s+q\s*&\s*a)", _re.I)


def _detect_qa_start(speakers: list[dict]) -> int | None:
    """Index of the first Q&A turn, or None if it can't be located. Skips the
    operator's opening preamble (the first couple of turns)."""
    for i, s in enumerate(speakers):
        if i < 2:
            continue
        if _QA_START_RE.search((s.get("text") or "")[:500]):
            return i
    return None


_ECALL_PATCHED = False


def _patch_earningscall():
    """The EarningsCall.biz library uses requests_cache, whose cached-response
    model has a TYPE_CHECKING-only `RequestsCookieJar` annotation that Python
    3.14's stricter get_type_hints (via cattrs) can't resolve — breaking every
    call. Swap its CachedSession for a plain requests.Session so the cattrs
    serialization path is never hit. (Local caching is redundant anyway — the
    DAG cache sits on top.)"""
    global _ECALL_PATCHED
    if _ECALL_PATCHED:
        return
    try:
        import requests
        import earningscall.api as eapi
        _plain = requests.Session()
        eapi.cache_session = lambda *a, **k: _plain
        _ECALL_PATCHED = True
    except Exception:
        pass


def fetch_full_transcripts(ticker: str, quarters: int = 12,
                            max_per_quarter: int = 200000, verbose: bool = False) -> list[dict]:
    """
    Fetch the FULL text of each available earnings call (complete prepared
    remarks + Q&A, no key-section extraction) for the dashboard's raw view and
    the per-quarter analyzers. Returns newest-first:
      [{quarter, year, date, source_url, text, char_count}, ...]

    Primary source: EarningsCall.biz (premium API, patched for Python 3.14).
    Falls back to the Motley Fool scrape if the API yields nothing.
    """
    _patch_earningscall()
    out = []
    try:
        import earningscall
        earningscall.api_key = ECALL_API_KEY
        if hasattr(earningscall.symbols, "_symbols") and earningscall.symbols._symbols is not None:
            if len(list(earningscall.symbols._symbols.get_all())) <= 2:  # demo mode
                earningscall.symbols._symbols = None
        from earningscall import get_company
        company = get_company(ticker.lower())
        events = list(company.events())
        for event in events[:quarters]:
            try:
                # level 2 = speaker-separated turns (with a name map), so we can
                # render the call as labeled speaker paragraphs + a Q&A split.
                tr = company.get_transcript(event=event, level=2)
                if not tr or not (tr.text or tr.speakers):
                    continue
                speakers = []
                for spk in (tr.speakers or []):
                    info = getattr(spk, "speaker_info", None)
                    name = (getattr(info, "name", None) or spk.speaker or "Speaker")
                    speakers.append({
                        "name": name,
                        "title": (getattr(info, "title", None) or ""),
                        "text": (spk.text or "").strip(),
                    })
                text = (tr.text or "")[:max_per_quarter]
                out.append({
                    "quarter": event.quarter, "year": event.year,
                    "date": (event.conference_date.strftime("%Y-%m-%d")
                             if event.conference_date else ""),
                    "source_url": "", "text": text, "char_count": len(text),
                    "speakers": speakers, "qa_start": _detect_qa_start(speakers),
                })
                if verbose:
                    print(f"  Q{event.quarter} {event.year}: {len(speakers)} speaker turns, {len(text):,} chars")
            except Exception:
                continue
    except Exception as e:
        if verbose:
            print(f"  Transcript(full) earningscall: {e}")
    # Optional Motley Fool fallback (off by default — its scrape rate-limits
    # hard in bulk and stalls). Pass use_mf_fallback=True for one-off names.
    if not out and verbose:
        print("  Transcript(full): earningscall returned nothing")
    return out


def _full_via_motley_fool(ticker, quarters, max_per_quarter, verbose) -> list[dict]:
    import re as _re
    try:
        from ingestion.loaders.transcript_batch import fetch_quarterly_transcripts
        res = fetch_quarterly_transcripts(ticker, quarters=quarters, delay=6.0, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"  Transcript(MF): {e}")
        return []
    out = []
    for t in (getattr(res, "transcripts", None) or []):
        full = (getattr(t, "full_text", "") or "")[:max_per_quarter]
        if not full:
            continue
        m = _re.match(r"Q?\s*([1-4])\D+(\d{4})", str(getattr(t, "quarter", "") or ""))
        out.append({
            "quarter": int(m.group(1)) if m else None,
            "year": int(m.group(2)) if m else None,
            "date": getattr(t, "filing_date", "") or "",
            "source_url": getattr(t, "source_url", "") or "",
            "text": full, "char_count": len(full),
        })
    return out


def _digest_quarters(raw_quarters: list[dict]) -> str | None:
    """Build the compact, brief-budget transcript digest from full quarters."""
    sections, total = [], 0
    for q in raw_quarters:
        excerpt = _extract_key_sections(q.get("text") or "", MAX_TRANSCRIPT_PER_QUARTER)
        if not excerpt:
            continue
        head = f"\n--- Q{q.get('quarter')} {q.get('year')} EARNINGS CALL ({q.get('date') or '?'}) ---\n"
        sections.append(head + excerpt)
        total += len(head) + len(excerpt)
        if total >= MAX_TOTAL_TRANSCRIPT:
            break
    return "\n".join(sections) if sections else None


def fetch_latest_transcript(ticker: str, verbose: bool = False) -> str | None:
    """Fetch just the most recent transcript. Returns full text."""
    try:
        import earningscall
        earningscall.api_key = ECALL_API_KEY
        if hasattr(earningscall.symbols, '_symbols') and earningscall.symbols._symbols is not None:
            sym_count = len(list(earningscall.symbols._symbols.get_all()))
            if sym_count <= 2:
                earningscall.symbols._symbols = None

        from earningscall import get_company
        company = get_company(ticker.lower())
        events = list(company.events())

        for event in events[:3]:  # Try latest 3 in case most recent isn't available
            transcript = company.get_transcript(event=event)
            if transcript and transcript.text:
                if verbose:
                    print(f"  Latest transcript: Q{event.quarter} {event.year} ({len(transcript.text):,} chars)")
                return transcript.text
        return None

    except Exception as e:
        if verbose:
            print(f"  Transcript: {e}")
        return None


def _extract_key_sections(text: str, max_chars: int) -> str:
    """
    Extract the most valuable sections from a transcript.

    Priority:
    1. CEO/CFO prepared remarks opening (first 3000 chars of prepared remarks)
    2. Key financial metrics mentions (revenue, EPS, margin, guidance)
    3. Q&A highlights (analyst questions about key drivers)
    """
    import re

    sections = []

    # Find prepared remarks section
    prepared_start = None
    for marker in [
        r'(?i)prepared\s+remarks',
        r'(?i)opening\s+remarks',
        r'(?i)good\s+(morning|afternoon|evening)',
        r'(?i)thank\s+you.{0,30}(joining|standing|call)',
    ]:
        match = re.search(marker, text)
        if match:
            prepared_start = match.start()
            break

    if prepared_start is None:
        prepared_start = 0

    # Get prepared remarks (first ~4000 chars from start)
    prepared = text[prepared_start:prepared_start + 4000]
    sections.append(prepared)

    # Find key financial mentions throughout the transcript
    financial_markers = [
        r'(?i)revenue\s+(was|of|grew|increased|decreased|came\s+in)',
        r'(?i)(earnings|EPS|diluted).{0,20}(per\s+share|was|of)',
        r'(?i)(comparable|same.store|comp).{0,20}(sales|growth|increased|decreased)',
        r'(?i)operating\s+margin',
        r'(?i)guidance.{0,30}(for|of|expect|anticipate|range)',
        r'(?i)(new\s+store|new\s+restaurant|new\s+unit).{0,30}(open|plan|target)',
        r'(?i)free\s+cash\s+flow',
    ]

    for marker in financial_markers:
        for match in re.finditer(marker, text):
            start = max(0, match.start() - 100)
            end = min(len(text), match.start() + 500)
            chunk = text[start:end].strip()
            if chunk not in sections and len(chunk) > 50:
                sections.append(f"[...] {chunk}")

    # Find Q&A section
    qa_start = None
    for marker in [
        r'(?i)question.and.answer',
        r'(?i)Q\s*&\s*A',
        r'(?i)we.ll\s+now\s+(take|open)',
        r'(?i)operator.{0,50}(question|first\s+question)',
    ]:
        match = re.search(marker, text)
        if match:
            qa_start = match.start()
            break

    if qa_start:
        # Get first 3000 chars of Q&A (usually the most important questions)
        qa_text = text[qa_start:qa_start + 3000]
        sections.append(f"\n[Q&A SESSION]\n{qa_text}")

    combined = "\n".join(sections)
    return combined[:max_chars]
