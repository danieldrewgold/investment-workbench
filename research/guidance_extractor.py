"""
Guidance Extractor — aggregate management guidance from already-fetched sources.

Today the pipeline fetches three sources that contain forward-looking guidance:
  1. Deck (shareholder letter / investor day) — analyzed by deck_guidance_extractor
     subagent which produces structured `guides` with metric/period/value.
  2. Transcript (12 quarters of earnings calls) — analyzed by guidance_tracker
     subagent which produces `current_live_guides` and `guides_issued`.
  3. Press release text (raw 8-K Exhibit 99.1) — currently NOT structured;
     this module adds light regex extraction for common guide phrasings.

We aggregate all three into a single `GuidanceBundle` with per-metric items
and source attribution. The bundle is the canonical "MANAGEMENT GUIDANCE"
anchor block injected into the brief prompt.

Public API:
    extract_guidance(transcript_digest, deck_digest, press_releases)
        -> GuidanceBundle
    GuidanceBundle.to_prompt_text() -> str
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

# Canonical metric vocabulary so dedup works across sources. Maps loose
# phrasing → canonical key.
_METRIC_CANONICAL = {
    "revenue": "revenue",
    "net revenue": "revenue",
    "total revenue": "revenue",
    "sales": "revenue",
    "net sales": "revenue",
    "ebitda": "adj_ebitda",
    "adjusted ebitda": "adj_ebitda",
    "adj ebitda": "adj_ebitda",
    "adj. ebitda": "adj_ebitda",
    "operating income": "operating_income",
    "operating margin": "operating_margin",
    "op margin": "operating_margin",
    "operating margin pct": "operating_margin",
    "gross margin": "gross_margin",
    "eps": "eps",
    "earnings per share": "eps",
    "adjusted eps": "adj_eps",
    "adj eps": "adj_eps",
    "free cash flow": "fcf",
    "fcf": "fcf",
    "capex": "capex",
    "capital expenditures": "capex",
    "capital expenditure": "capex",
    "comp sales": "sss",
    "comparable sales": "sss",
    "same-store sales": "sss",
    "same store sales": "sss",
    "sss": "sss",
    "unit count": "unit_count",
    "new units": "new_units",
    "new restaurants": "new_units",
    "new stores": "new_units",
    "store count": "unit_count",
    "arr": "arr",
    "annual recurring revenue": "arr",
    "net retention": "net_retention",
    "ndr": "net_retention",
    "dau": "dau",
    "mau": "mau",
    "users": "users",
    "subscribers": "subscribers",
}


@dataclass
class GuidanceItem:
    """One piece of forward management guidance with provenance."""
    metric: str                       # canonical key (e.g. "revenue")
    metric_label: str                 # human-readable (e.g. "Revenue")
    period: str                       # "Q1 2026" | "FY2026" | "long_term" | etc.
    value_low: float | None = None
    value_high: float | None = None
    value_unit: str = ""              # "$M" | "$B" | "%" | "count" | "x"
    raw_value: str = ""               # verbatim as given (e.g., "$595-605M")
    source_type: str = ""             # "press_release" | "deck" | "transcript"
    source_detail: str = ""           # e.g., "Q4 2025 PR (filed 2026-02-05)" | "Q4 2025 call, CFO"
    quote: str = ""                   # verbatim quote ≤200 chars
    confidence: str = "explicit"      # "explicit" | "implied" — explicit = clear guide; implied = soft commentary

    def midpoint(self) -> float | None:
        if self.value_low is None and self.value_high is None:
            return None
        if self.value_low is None:
            return self.value_high
        if self.value_high is None:
            return self.value_low
        return (self.value_low + self.value_high) / 2

    def render_value(self) -> str:
        """One-line rendering of the value range."""
        if self.value_low is None and self.value_high is None:
            return self.raw_value or "(see quote)"
        unit_after = self.value_unit if self.value_unit and self.value_unit not in ("$M", "$B") else ""
        unit_before = "$" if self.value_unit in ("$M", "$B", "$") else ""
        unit_suffix = ""
        if self.value_unit == "$M":
            unit_suffix = "M"
        elif self.value_unit == "$B":
            unit_suffix = "B"
        elif self.value_unit == "%":
            unit_suffix = "%"
        if self.value_low is not None and self.value_high is not None and self.value_low != self.value_high:
            return f"{unit_before}{self.value_low:g}-{self.value_high:g}{unit_suffix}{(' '+unit_after) if unit_after else ''}"
        v = self.value_low if self.value_low is not None else self.value_high
        return f"{unit_before}{v:g}{unit_suffix}{(' '+unit_after) if unit_after else ''}"


@dataclass
class GuidanceBundle:
    ticker: str = ""
    items: list = field(default_factory=list)        # list[GuidanceItem]
    extracted_at: str = ""
    sources_used: list = field(default_factory=list) # ["press_release", "deck", "transcript"]
    notes: list = field(default_factory=list)        # any extraction warnings

    def to_dict(self) -> dict:
        return asdict(self)

    def is_empty(self) -> bool:
        return not self.items

    def by_period(self, period_match: str) -> list:
        """Return items where period contains the substring (case-insensitive)."""
        lc = (period_match or "").lower()
        return [i for i in self.items if lc in (i.period or "").lower()]

    def by_metric(self, metric: str) -> list:
        """Return items matching canonical metric key."""
        return [i for i in self.items if i.metric == metric]

    def to_prompt_text(self) -> str:
        """
        Render the guidance bundle as a structured prompt block. If empty,
        return a clear "no guidance available" placeholder so the brief
        prompt doesn't have a blank section.
        """
        header = "==================================================================\n" \
                 "MANAGEMENT GUIDANCE — actual disclosed forward statements\n" \
                 "=================================================================="

        if self.is_empty():
            return (header + "\n"
                    "(No formal management guidance was extractable from press\n"
                    "releases, transcripts, or deck. Either the company doesn't\n"
                    "guide forward, or guidance wasn't surfaced in the extracted\n"
                    "subagent outputs. Edge claims will need to anchor against\n"
                    "consensus only.)\n"
                    "=" * 66)

        # Group by period for readability
        by_period: dict[str, list] = {}
        for item in self.items:
            by_period.setdefault(item.period or "Unspecified period", []).append(item)

        # Stable period ordering: near-term Q first, then FY, then LT
        def _period_sort_key(p: str) -> tuple:
            pl = p.lower()
            if pl.startswith("q") and any(c.isdigit() for c in pl):
                return (0, pl)
            if pl.startswith("fy") or "fiscal" in pl:
                return (1, pl)
            if "long" in pl or "20" in pl[-4:]:
                return (2, pl)
            return (3, pl)

        lines = [header]
        for period in sorted(by_period.keys(), key=_period_sort_key):
            lines.append(f"\n{period}:")
            for item in by_period[period]:
                src = item.source_detail or item.source_type
                value = item.render_value()
                tag = "" if item.confidence == "explicit" else "  [implied]"
                lines.append(f"  • {item.metric_label}: {value}{tag}")
                if item.quote:
                    q = item.quote.replace("\n", " ").strip()
                    if len(q) > 180:
                        q = q[:177] + "..."
                    lines.append(f"      \"{q}\"  ({src})")
                else:
                    lines.append(f"      ({src})")
        lines.append("=" * 66)

        if self.notes:
            lines.append("Extraction notes: " + "; ".join(self.notes[:3]))

        return "\n".join(lines)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _canonical_metric(s: str) -> str:
    """Best-effort mapping from loose metric phrasing to canonical key."""
    if not s:
        return ""
    key = s.strip().lower()
    if key in _METRIC_CANONICAL:
        return _METRIC_CANONICAL[key]
    # Fuzzy: check substring containment
    for k, v in _METRIC_CANONICAL.items():
        if k in key:
            return v
    # Unknown — keep as-is, lowercased + underscored
    return re.sub(r"[^a-z0-9]+", "_", key).strip("_")


def _humanize_metric(canonical: str) -> str:
    """Human-readable label for a canonical metric key."""
    return {
        "revenue":          "Revenue",
        "adj_ebitda":       "Adj. EBITDA",
        "operating_income": "Operating Income",
        "operating_margin": "Operating Margin",
        "gross_margin":     "Gross Margin",
        "eps":              "EPS",
        "adj_eps":          "Adj. EPS",
        "fcf":              "Free Cash Flow",
        "capex":            "CapEx",
        "sss":              "Same-Store Sales",
        "unit_count":       "Unit Count",
        "new_units":        "New Units",
        "arr":              "ARR",
        "net_retention":    "Net Retention",
        "dau":              "DAU",
        "mau":              "MAU",
        "users":            "Users",
        "subscribers":      "Subscribers",
    }.get(canonical, canonical.replace("_", " ").title())


def _parse_dollar_range(s: str) -> tuple[float | None, float | None, str]:
    """
    Try to pull (low, high, unit) from raw guide text like "$595-605M",
    "$1.2-1.5 billion", "$0.85 to $0.92". Returns (None, None, "")
    if it can't parse.
    """
    if not s:
        return (None, None, "")
    text = s.strip()
    # Normalize various dashes
    text = text.replace("–", "-").replace("—", "-").replace(" to ", "-")
    # Range pattern: allow an optional suffix BEFORE the dash too, so
    # "$595 million-$605 million" parses as (595, 605, $M). The lo-suffix
    # group is optional; if both lo-suffix and hi-suffix are present we
    # use hi-suffix as authoritative (or lo-suffix as fallback).
    m = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)"
        r"\s*([bBmMkK]|billion|million|thousand)?"     # optional lo-suffix
        r"\s*-\s*"
        r"\$?\s*([\d,]+(?:\.\d+)?)"
        r"\s*([bBmMkK]|billion|million|thousand)?",    # optional hi-suffix
        text,
    )
    if m:
        try:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(3).replace(",", ""))
        except ValueError:
            return (None, None, "")
        # Prefer hi-suffix; fall back to lo-suffix if hi is missing
        suffix = ((m.group(4) or m.group(2)) or "").lower()
        if suffix in ("b", "billion"):
            return (lo, hi, "$B")
        if suffix in ("m", "million"):
            return (lo, hi, "$M")
        if suffix in ("k", "thousand"):
            return (lo / 1000.0, hi / 1000.0, "$M")
        # No suffix — magnitude-based heuristic
        if 50 <= lo <= 50000:
            return (lo, hi, "$M")
        return (lo, hi, "$")

    # Single value "$X[B|M]"
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*([bBmMkK]|billion|million|thousand)?", text)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            return (None, None, "")
        suffix = (m.group(2) or "").lower()
        unit = "$M" if suffix in ("m", "million") or 50 <= v <= 50000 else \
               "$B" if suffix in ("b", "billion") else "$"
        return (v, v, unit)

    # Percent range "X-Y%"
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\s*%", text)
    if m:
        try:
            lo = float(m.group(1))
            hi = float(m.group(2))
            return (lo, hi, "%")
        except ValueError:
            return (None, None, "")

    # Single percent "X%"
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text)
    if m:
        try:
            v = float(m.group(1))
            return (v, v, "%")
        except ValueError:
            pass

    return (None, None, "")


# --------------------------------------------------------------------------
# Source extractors
# --------------------------------------------------------------------------

def _extract_from_deck_subagent(deck_digest_dict: dict | None) -> list:
    """Pull GuidanceItems from the deck_guidance_extractor subagent output."""
    if not deck_digest_dict:
        return []
    subs = deck_digest_dict.get("subagents") or {}
    entry = subs.get("deck_guidance_extractor") or {}
    if not entry.get("ok"):
        return []
    data = entry.get("data") or {}
    raw_guides = data.get("guides") or []

    items: list = []
    for g in raw_guides:
        metric_raw = g.get("metric", "") or ""
        period = g.get("period", "") or "Unspecified"
        value_str = g.get("value_or_range", "") or ""
        page = g.get("source_page", "")
        canon = _canonical_metric(metric_raw)
        lo, hi, unit = _parse_dollar_range(value_str)
        items.append(GuidanceItem(
            metric=canon,
            metric_label=_humanize_metric(canon),
            period=period,
            value_low=lo,
            value_high=hi,
            value_unit=unit,
            raw_value=value_str,
            source_type="deck",
            source_detail=f"Deck p.{page}" if page else "Deck",
            quote=(g.get("evidence_quote") or "")[:200],
            confidence="explicit",
        ))
    return items


def _extract_from_transcript_subagent(transcript_digest_dict: dict | None) -> list:
    """Pull GuidanceItems from the guidance_tracker subagent output.
    Uses `current_live_guides` (the most recent live guide per metric+period
    after applying any raises/lowers/reaffirms across quarters)."""
    if not transcript_digest_dict:
        return []
    subs = transcript_digest_dict.get("subagents") or {}
    entry = subs.get("guidance_tracker") or {}
    if not entry.get("ok"):
        return []
    data = entry.get("data") or {}
    live = data.get("current_live_guides") or []

    items: list = []
    for g in live:
        metric_raw = g.get("metric", "") or ""
        period = g.get("period_guided", "") or "Unspecified"
        value_str = g.get("most_recent_statement", "") or ""
        speaker = (g.get("speaker") or "").upper()
        source_q = g.get("source_quarter", "") or ""
        canon = _canonical_metric(metric_raw)
        lo, hi, unit = _parse_dollar_range(value_str)
        detail_parts = []
        if source_q:
            detail_parts.append(source_q)
        detail_parts.append("call")
        if speaker:
            detail_parts.append(speaker)
        items.append(GuidanceItem(
            metric=canon,
            metric_label=_humanize_metric(canon),
            period=period,
            value_low=lo,
            value_high=hi,
            value_unit=unit,
            raw_value=value_str,
            source_type="transcript",
            source_detail=", ".join(detail_parts),
            quote=(g.get("most_recent_statement") or "")[:200],
            confidence="explicit",
        ))
    return items


# Press-release regex patterns for common guide phrasings
_PR_PATTERNS = [
    # "expect first quarter 2026 revenue [to be] in the range of $595-605M"
    re.compile(
        r"(?:expect|expects|expected|forecast|forecasted|project|projected|guide|guides|guiding)\s+"
        r"(?:.{0,40}?\b)?"
        r"(?P<period>(?:first|second|third|fourth|1st|2nd|3rd|4th|q[1-4]|fiscal\s+year|full\s+year|fy)\s*\d{0,4}|fy\d{2,4}|\d{4})"
        r".{0,40}?\b(?P<metric>revenue|sales|adjusted\s+ebitda|ebitda|operating\s+income|operating\s+margin|"
        r"gross\s+margin|eps|earnings\s+per\s+share|free\s+cash\s+flow|fcf|capex|capital\s+expenditures?|"
        r"comp(?:arable)?\s+sales|same[-\s]store\s+sales|sss|new\s+(?:units|restaurants|stores))"
        r"\b.{0,30}?(?P<value>\$?\s*[\d,.]+\s*(?:to|-|–|—)\s*\$?\s*[\d,.]+\s*[bBmMkK%]?|\$\s*[\d,.]+\s*[bBmMkK%]?|[+-]?\s*[\d.]+\s*-\s*[+-]?\s*[\d.]+\s*%)",
        re.IGNORECASE,
    ),
]


def _extract_from_press_releases(press_releases: list | None) -> list:
    """Light regex extraction of obvious guide patterns from press release
    text. Brittle by design — best-effort. The deck and transcript subagents
    are the authoritative sources; this is a backstop."""
    if not press_releases:
        return []
    items: list = []
    for pr in press_releases[:2]:  # only the most recent 2 quarters
        if not isinstance(pr, dict):
            continue
        text = pr.get("text") or pr.get("full_text_with_tables") or ""
        if not text:
            continue
        quarter = pr.get("quarter", "") or ""
        report_date = pr.get("report_date", "") or ""

        for pat in _PR_PATTERNS:
            for m in pat.finditer(text):
                period = (m.group("period") or "").strip()
                metric_raw = (m.group("metric") or "").strip()
                value_str = (m.group("value") or "").strip()
                if not period or not metric_raw or not value_str:
                    continue
                # Normalize period: "first quarter 2026" → "Q1 2026"
                period_norm = _normalize_period(period, default_year=quarter)
                canon = _canonical_metric(metric_raw)
                lo, hi, unit = _parse_dollar_range(value_str)
                # Snag a tighter quote: 80 chars before + match span
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 40)
                quote = text[start:end].strip().replace("\n", " ")
                items.append(GuidanceItem(
                    metric=canon,
                    metric_label=_humanize_metric(canon),
                    period=period_norm,
                    value_low=lo,
                    value_high=hi,
                    value_unit=unit,
                    raw_value=value_str,
                    source_type="press_release",
                    source_detail=f"{quarter} PR" + (f" ({report_date})" if report_date else ""),
                    quote=quote[:200],
                    confidence="explicit",
                ))
    return items


def _normalize_period(raw: str, default_year: str = "") -> str:
    """Normalize period strings like 'first quarter 2026' → 'Q1 2026'."""
    if not raw:
        return ""
    s = raw.strip().lower()
    qmap = {"first": "Q1", "1st": "Q1", "q1": "Q1",
            "second": "Q2", "2nd": "Q2", "q2": "Q2",
            "third": "Q3", "3rd": "Q3", "q3": "Q3",
            "fourth": "Q4", "4th": "Q4", "q4": "Q4"}
    # Try "Qn YYYY"
    qm = re.match(r"q([1-4])\s*(\d{2,4})?", s)
    if qm:
        q = "Q" + qm.group(1)
        year = qm.group(2) or ""
        if year and len(year) == 2:
            year = "20" + year
        return f"{q} {year}".strip()
    # Try "first quarter [YYYY]"
    for word, q in qmap.items():
        if s.startswith(word):
            year_match = re.search(r"(\d{4})", s)
            year = year_match.group(1) if year_match else ""
            if not year and default_year:
                # Try to extract year from "Q4 2025" type default
                ym = re.search(r"(\d{4})", default_year)
                if ym:
                    year = ym.group(1)
            return f"{q} {year}".strip()
    # Fiscal year forms
    if "fiscal" in s or s.startswith("fy") or s.startswith("full year"):
        ym = re.search(r"(\d{4})", s)
        year = ym.group(1) if ym else (re.search(r"(\d{4})", default_year).group(1) if default_year and re.search(r"(\d{4})", default_year) else "")
        return f"FY{year[-4:]}" if year else "FY"
    # Bare year
    ym = re.match(r"(\d{4})", s)
    if ym:
        return f"FY{ym.group(1)}"
    return raw.strip()


# --------------------------------------------------------------------------
# Aggregator + dedup
# --------------------------------------------------------------------------

def _dedup_items(items: list) -> list:
    """Dedup by (metric, period). Source priority: deck > transcript > press_release.
    The first-seen at higher priority wins."""
    priority = {"deck": 0, "transcript": 1, "press_release": 2}
    by_key: dict[tuple, GuidanceItem] = {}
    for item in items:
        key = (item.metric, item.period)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        if priority.get(item.source_type, 9) < priority.get(existing.source_type, 9):
            by_key[key] = item
    return list(by_key.values())


def extract_guidance(
    ticker: str,
    transcript_digest: dict | None = None,
    deck_digest: dict | None = None,
    press_releases: list | None = None,
) -> GuidanceBundle:
    """
    Aggregate all available management guidance into a single bundle.

    Inputs are the raw dict shapes already produced by upstream pipeline
    steps:
      - transcript_digest = TranscriptDigest.to_dict() (subagents key)
      - deck_digest       = DeckDigest.to_dict() (subagents key)
      - press_releases    = list of PressRelease.to_dict()

    All three are optional; missing sources just produce an empty
    GuidanceBundle, which renders honestly as "no guidance available."
    """
    bundle = GuidanceBundle(
        ticker=(ticker or "").upper(),
        extracted_at=datetime.now().isoformat(timespec="seconds"),
    )

    deck_items = _extract_from_deck_subagent(deck_digest)
    if deck_items:
        bundle.sources_used.append("deck")

    transcript_items = _extract_from_transcript_subagent(transcript_digest)
    if transcript_items:
        bundle.sources_used.append("transcript")

    pr_items = _extract_from_press_releases(press_releases)
    if pr_items:
        bundle.sources_used.append("press_release")

    all_items = deck_items + transcript_items + pr_items
    bundle.items = _dedup_items(all_items)

    if not bundle.items:
        bundle.notes.append("no guidance items found across any source")

    return bundle
