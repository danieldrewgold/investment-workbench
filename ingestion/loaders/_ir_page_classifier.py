"""
IR Page Classifier subagent.

Takes the HTML of an IR page (typically /presentations, /events, or the IR
root) and returns a structured catalog of investor decks / letters found on
the page. The point is resilience: IR page layouts vary wildly across
companies, so we let Claude read the HTML and tell us what's there.

Output schema (returned as list of DeckCandidate):
  deck_type: earnings | investor_day | conference | shareholder_letter | other
  url: absolute URL to the PDF (or other document)
  title: human-readable label
  date: YYYY-MM-DD if identifiable, else ""
  quarter: "Q3 2026" if earnings, else ""
  event_metadata: type-specific (broker, conference_name, event_year, period)
  classification_confidence: explicit | inferred | fallback

The classifier does NOT download PDFs — it only catalogs what's referenced
on the page. The caller (ir_page_deck_loader) downloads + parses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

from research.transcript_subagents._base import (
    EVIDENCE_SCHEMA_BLOCK, call_subagent,
)


# ==========================================================================
# HTML → lean signal: strip script/style/svg/etc. so the classifier sees
# links + labels + headings, not megabytes of JS bundles and inlined fonts.
# Typical 1MB rendered React page compresses to ~60-120KB of relevant HTML.
# ==========================================================================

# Full-section removals (tag + content)
_STRIP_SECTIONS = re.compile(
    r"<(script|style|svg|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Self-closing non-content tags
_STRIP_SELF_CLOSING = re.compile(
    r"<(meta|link|base|source|track|wbr|br|hr)\b[^>]*/?>\s*",
    re.IGNORECASE,
)
# Inline event handlers (on*=), style=, and very long data-* attributes —
# preserve href/src/text/alt which carry signal.
_STRIP_NOISY_ATTRS = re.compile(
    r'\s+(?:on\w+|style|data-reactid|data-react-helmet)\s*=\s*(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)
# Collapse runs of whitespace
_COLLAPSE_WS = re.compile(r"[ \t\r\n]+")


def _lean_html(html: str) -> str:
    """Return an HTML string with scripts/styles/etc. stripped — keeps the
    link/heading/text structure but cuts 80-95% of bytes from rendered pages."""
    s = _STRIP_SECTIONS.sub("", html)
    s = _STRIP_SELF_CLOSING.sub("", s)
    s = _STRIP_NOISY_ATTRS.sub("", s)
    s = _COLLAPSE_WS.sub(" ", s)
    return s.strip()


SUBAGENT_NAME = "ir_page_classifier"


@dataclass
class DeckCandidate:
    url: str = ""                       # absolute URL (resolved from relative)
    deck_type: str = ""                 # earnings | investor_day | conference | shareholder_letter | other
    title: str = ""
    date: str = ""                      # YYYY-MM-DD or ""
    quarter: str = ""                   # "Q3 2026" for earnings; else ""
    event_metadata: dict = field(default_factory=dict)
    classification_confidence: str = "" # explicit | inferred | fallback
    source_anchor_text: str = ""        # the anchor/link text on the IR page
    source_context: str = ""            # brief surrounding text Claude used

    def to_dict(self) -> dict:
        return asdict(self)


SYSTEM_PROMPT = """You are the IR Page Classifier subagent. You analyze HTML
from a company's Investor Relations page and identify links to REAL SLIDE
DECKS and INVESTOR PRESENTATIONS.

You are looking for actual presentation material: earnings call decks,
investor day presentations, conference presentations, and shareholder
letters. That's it.

HARD EXCLUDES — you MUST skip these document types entirely, even if they
appear prominently on the page:
  - SEC filings: 10-K, 10-Q, 8-K, DEF 14A, proxy statements
  - Annual reports / corporate responsibility / ESG / sustainability reports
  - Non-GAAP reconciliation PDFs (these are earnings-adjacent but NOT decks)
  - Supplemental financial information / supplemental investor information
    (spreadsheet-style backups to earnings releases — not decks)
  - Factsheets
  - Press releases (they're HTML or short PDFs, not decks)
  - Audio/video links (mp3, mp4, webcast URLs)
  - Transcripts

If a document's ONLY content is tables of GAAP-to-non-GAAP math, or
quarterly financial detail backup, it is NOT a deck. Skip it.

You do NOT download anything. You only catalog links using anchor text,
nearby headings, and URL patterns as evidence. If a link's purpose is
ambiguous, DO NOT INCLUDE it — missing a deck is better than including
a non-deck."""


USER_PROMPT_TEMPLATE = """The HTML below is from the Investor Relations page
of {ticker}. Your job is to identify every link that points to an investor
deck, shareholder letter, or related investor-facing document, and classify it.

DECK TYPES:
  - "earnings" — quarterly earnings presentation slide deck (e.g., "Q3 2025
    Earnings Presentation", "Fourth Quarter 2025 Earnings Call Deck")
  - "investor_day" — periodic (often annual) investor/analyst day deck
    (e.g., "2026 Investor Day", "Analyst Day Presentation 2025")
  - "conference" — presentation at a broker or industry conference
    (e.g., "JPMorgan Healthcare Conference 2026", "Goldman Sachs Tech
    Conference Presentation")
  - "shareholder_letter" — quarterly or annual shareholder/CEO letter, often
    PDF (e.g., "Q3 2025 Shareholder Letter", "2025 Annual Letter to Shareholders")
  - "other" — something investor-adjacent that doesn't fit the above
    (e.g., supplemental data pack, ESG report, factsheet)

OUTPUT JSON SCHEMA:
{{
  "decks": [
    {{
      "url": "full URL (absolute — if the href is relative, resolve against {base_url})",
      "deck_type": "earnings | investor_day | conference | shareholder_letter | other",
      "title": "the anchor text, cleaned up",
      "date": "YYYY-MM-DD or empty if undeterminable",
      "quarter": "Q3 2026 if earnings and you can infer the fiscal quarter, else empty",
      "event_metadata": {{
        // For earnings: {{"fiscal_year": 2026}}
        // For investor_day: {{"event_year": 2026, "event_name": "2026 Investor Day"}}
        // For conference: {{"broker": "JPMorgan", "conference_name": "Healthcare Conference 2026"}}
        // For shareholder_letter: {{"period": "Q3 2026" or "FY2025"}}
        // For other: {{"description": "one phrase"}}
      }},
      "classification_confidence": "explicit | inferred | fallback",
      "source_anchor_text": "the exact text that appeared on the page for this link",
      "source_context": "a short (~20 word) snippet of nearby HTML that helped you classify"
    }}
  ],
  "summary": {{
    "total_candidates": 0,
    "by_type": {{"earnings": 0, "investor_day": 0, "conference": 0, "shareholder_letter": 0, "other": 0}},
    "page_looks_like": "earnings_archive | events_and_presentations | ir_root | mixed | other"
  }}
}}

CLASSIFICATION RULES:
1. INCLUDE a link if it points to a REAL presentation deck or shareholder
   letter. Recognize the PDF by ANY of these signals, NOT just the file
   extension (many modern IR sites serve PDFs from extension-less URLs):
     - the href ends in .pdf or .pptx, OR
     - the anchor carries a type="application/pdf" attribute, OR
     - the anchor's title attribute ends in .pdf
       (e.g. title="TransMedics Q1 2026 Earnings Presentation.pdf"), OR
     - the href is an opaque document-download path such as /static-files/<id>,
       /files/doc..., /node/<id>/download, or a *.q4cdn.com static file.
   When the URL is an opaque id, use the title attribute or the nearby heading
   text as the deck title. Skip HTML landing pages, videos, webcasts,
   transcripts, and press releases.
2. REJECT any link whose anchor text or URL matches ANY of these:
   - "non-gaap reconciliation", "gaap reconciliation", "reconciliation of"
   - "supplemental" (investor information, financial data, reporting, etc.)
   - "proxy statement", "DEF 14A", "10-K", "10-Q", "8-K", "form 10"
   - "annual report" (this is the full AR, not the earnings deck)
   - "factsheet", "fact sheet", "ESG report", "sustainability report",
     "corporate responsibility"
   - "webcast", "transcript", "press release"
   These are not decks. Skip them even if they're prominently linked.
3. classification_confidence:
   - "explicit" — anchor text or heading clearly names the type
     (e.g., "Q3 2025 Earnings Presentation" → earnings, explicit)
   - "inferred" — type determined from URL pattern only
     (e.g., file name contains "investor-day" with no surrounding context)
   - "fallback" — DO NOT USE "fallback" — if you'd use fallback, just skip the link
4. Resolve relative URLs against {base_url} — if the href is absolute, leave it.
5. Dedupe: same PDF appearing multiple times → include once.
6. "other" category: only for documents that ARE decks/presentations but don't
   fit earnings/investor_day/conference/shareholder_letter. Do NOT use "other"
   as a catch-all for things you're not sure about — SKIP those instead.

HTML (from {ir_url}):
{html}

Respond with the JSON object only."""


def classify_ir_page(
    ticker: str,
    ir_url: str,
    html: str,
    *,
    verbose: bool = False,
) -> list[DeckCandidate]:
    """
    Run the classifier over IR page HTML. Returns a list of DeckCandidate.

    Empty list on failure — caller can fall through to other IR sub-pages
    or give up.
    """
    # Parse base URL for relative-link resolution
    parsed = urlparse(ir_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Strip scripts/styles/etc. before sending — rendered React pages are
    # ~90% non-content bytes. Lean HTML lets far more signal fit in budget.
    lean = _lean_html(html)

    if verbose and len(html) > 50_000:
        print(f"  [IR-CLS] HTML lean: {len(html):,} → {len(lean):,} chars "
              f"({len(lean)/len(html)*100:.0f}% retained)")

    # After stripping, we can afford a larger classifier input.
    # 150K chars fits comfortably in Sonnet's 200K context even with the
    # prompt overhead. Still truncate as a safety rail.
    html_trimmed = lean[:150_000]

    user_prompt = USER_PROMPT_TEMPLATE.format(
        ticker=ticker,
        ir_url=ir_url,
        base_url=base_url,
        html=html_trimmed,
    )
    user_prompt = user_prompt + "\n" + EVIDENCE_SCHEMA_BLOCK

    result = call_subagent(
        subagent_name=SUBAGENT_NAME,
        ticker=ticker,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=4000,
        temperature=0.1,
        verbose=verbose,
    )
    if not result.ok:
        if verbose:
            print(f"  [IR-CLS] call failed: {result.error}")
        return []

    raw_decks = (result.data or {}).get("decks") or []
    candidates: list[DeckCandidate] = []
    for d in raw_decks:
        url = d.get("url", "") or ""
        if not url:
            continue
        # Ensure absolute — model sometimes forgets to resolve
        if not url.startswith("http"):
            url = urljoin(base_url, url)
        candidates.append(DeckCandidate(
            url=url,
            deck_type=d.get("deck_type", "other") or "other",
            title=d.get("title", "") or "",
            date=d.get("date", "") or "",
            quarter=d.get("quarter", "") or "",
            event_metadata=d.get("event_metadata", {}) or {},
            classification_confidence=d.get("classification_confidence", "fallback") or "fallback",
            source_anchor_text=d.get("source_anchor_text", "") or "",
            source_context=d.get("source_context", "") or "",
        ))

    # Dedupe by URL (classifier may miss this)
    seen: set[str] = set()
    deduped: list[DeckCandidate] = []
    for c in candidates:
        if c.url in seen:
            continue
        seen.add(c.url)
        deduped.append(c)

    if verbose:
        summary = (result.data or {}).get("summary") or {}
        print(f"  [IR-CLS] {len(deduped)} decks catalogued "
              f"(by_type: {summary.get('by_type', {})})")
    return deduped
