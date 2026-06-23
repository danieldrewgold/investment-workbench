"""
Yahoo Finance earnings-call transcript loader — a transcript source for
international / thinly-covered names that EarningsCall.biz doesn't carry (foreign
ADRs like Jollibee). Yahoo hosts SPEAKER-STRUCTURED transcripts, but httpx 404s
(consent/JS-gated), so we render via the Playwright browser path.

URLs aren't auto-discoverable yet (the transcript page links only to itself), so
known transcript URLs are SEEDED in `_YAHOO_TRANSCRIPT_URLS` — add more as found.

  fetch_yahoo_transcripts(ticker) -> list[quarter dict] matching the format of
  transcript_fetcher.fetch_full_transcripts (quarter/year/date/source_url/text/
  char_count/speakers[{name,title,text}]/qa_start), so the existing digest +
  dashboard render work unchanged.
"""

from __future__ import annotations

import html as _html
import re

# Known Yahoo transcript URLs per ticker (auto-discovery TBD). Key BOTH ADR
# tickers when they differ — the pipeline runs JBFCY; Yahoo hosts under JBFCF.
_YAHOO_TRANSCRIPT_URLS = {
    "JBFCY": ["https://finance.yahoo.com/quote/JBFCF/earnings/JBFCF-Q1-2026-earnings_call-593685.html"],
    "JBFCF": ["https://finance.yahoo.com/quote/JBFCF/earnings/JBFCF-Q1-2026-earnings_call-593685.html"],
}

_NAME_RE = re.compile(r'<span[^>]*class="type-label-lg-med[^"]*"[^>]*>', re.I)
_TITLE_RE = re.compile(r'speakerDesc[^>]*>.*?<span[^>]*>(.*?)</span>', re.S)
_PARA_RE = re.compile(r'class="type-paragraph-md-reg[^"]*"[^>]*>(.*?)</p>', re.S)
_TS_RE = re.compile(r"^\s*\d+:\d\d")  # timestamp paragraphs to skip
_Q_MONTH = {1: "05", 2: "08", 3: "11", 4: "03"}  # ~report month (Dec-FYE)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _extract_speakers(html: str) -> list[dict]:
    """Parse Yahoo's speaker blocks (name span + speakerDesc title + paragraphs)."""
    speakers = []
    for seg in _NAME_RE.split(html)[1:]:
        name = _clean(seg.split("</span>")[0])
        if not name or len(name) > 60:
            continue
        mt = _TITLE_RE.search(seg)
        title = _clean(mt.group(1)) if mt else ""
        text = " ".join(_clean(p) for p in _PARA_RE.findall(seg)
                         if not _TS_RE.match(_clean(p))).strip()
        if text and "[Presentation]" not in text:
            speakers.append({"name": name, "title": title, "text": text})
    return speakers


def _parse_meta(url: str) -> tuple[int | None, int | None]:
    m = re.search(r"-Q([1-4])-((?:19|20)\d\d)-earnings_call", url)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def fetch_yahoo_transcripts(ticker: str, *, verbose: bool = False) -> list[dict]:
    """Browser-render + extract the seeded Yahoo transcript URLs for a ticker.
    Returns [] when none are seeded or extraction fails (caller falls through)."""
    urls = _YAHOO_TRANSCRIPT_URLS.get(ticker.upper(), [])
    if not urls:
        return []
    try:
        from ingestion.loaders._browser_fetch import fetch_html_with_browser
        from research.transcript_fetcher import _detect_qa_start
    except Exception as e:
        if verbose:
            print(f"  [Yahoo-transcript] import error: {type(e).__name__}: {e}")
        return []

    out = []
    for url in urls:
        try:
            html = fetch_html_with_browser(url, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  [Yahoo-transcript] fetch failed {url}: {type(e).__name__}")
            continue
        if not html:
            continue
        speakers = _extract_speakers(html)
        if not speakers:
            if verbose:
                print(f"  [Yahoo-transcript] no speakers parsed from {url}")
            continue
        q, y = _parse_meta(url)
        text = "\n\n".join(
            f"{s['name']}{(' (' + s['title'] + ')') if s['title'] else ''}: {s['text']}"
            for s in speakers)
        date = f"{y}-{_Q_MONTH.get(q, '01')}-01" if (q and y) else ""
        out.append({
            "quarter": q, "year": y, "date": date,
            "source_url": url, "text": text, "char_count": len(text),
            "speakers": speakers, "qa_start": _detect_qa_start(speakers),
        })
        if verbose:
            print(f"  [Yahoo-transcript] Q{q} {y}: {len(speakers)} turns, {len(text):,} chars")
    out.sort(key=lambda r: (r.get("year") or 0, r.get("quarter") or 0), reverse=True)
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t = sys.argv[1] if len(sys.argv) > 1 else "JBFCY"
    qs = fetch_yahoo_transcripts(t, verbose=True)
    print(f"\n{len(qs)} quarter(s)")
    for q in qs:
        print(f"  Q{q['quarter']} {q['year']}: {len(q['speakers'])} turns, "
              f"qa_start={q['qa_start']}, {q['char_count']:,} chars")
