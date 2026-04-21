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
    Fetch up to 3 years of earnings call transcripts and produce
    a condensed summary for Claude's research brief.

    Returns a formatted string with key excerpts from each quarter,
    or None if transcripts aren't available.
    """
    try:
        import earningscall
        earningscall.api_key = ECALL_API_KEY
        # Clear cached demo symbols on first use
        if hasattr(earningscall.symbols, '_symbols') and earningscall.symbols._symbols is not None:
            sym_count = len(list(earningscall.symbols._symbols.get_all()))
            if sym_count <= 2:  # demo mode, need to reload
                earningscall.symbols._symbols = None

        from earningscall import get_company
    except ImportError:
        if verbose:
            print("  Transcript: earningscall library not installed (pip install earningscall)")
        return None

    if not ECALL_API_KEY:
        if verbose:
            print("  Transcript: no API key")
        return None

    try:
        company = get_company(ticker.lower())
        if verbose:
            print(f"  Transcript: found {company}")
    except Exception as e:
        if verbose:
            print(f"  Transcript: company lookup failed - {e}")
        return None

    events = list(company.events())
    if not events:
        if verbose:
            print(f"  Transcript: no events found")
        return None

    sections = []
    total_chars = 0
    fetched = 0

    for event in events[:quarters]:
        try:
            transcript = company.get_transcript(event=event)
            if not transcript or not transcript.text:
                continue

            text = transcript.text
            fetched += 1

            # Extract key sections: prepared remarks opener + Q&A highlights
            excerpt = _extract_key_sections(text, MAX_TRANSCRIPT_PER_QUARTER)

            header = f"\n--- Q{event.quarter} {event.year} EARNINGS CALL ({event.conference_date.strftime('%Y-%m-%d') if event.conference_date else '?'}) ---\n"
            section = header + excerpt
            sections.append(section)
            total_chars += len(section)

            if verbose:
                print(f"  Q{event.quarter} {event.year}: {len(text):,} chars -> {len(excerpt):,} excerpt")

            # Stop if we've hit the total budget
            if total_chars >= MAX_TOTAL_TRANSCRIPT:
                break

        except Exception as e:
            if verbose:
                print(f"  Q{event.quarter} {event.year}: error - {e}")
            continue

    if not sections:
        return None

    if verbose:
        print(f"  Transcript: {fetched} quarters fetched, {total_chars:,} chars total")

    return "\n".join(sections)


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
