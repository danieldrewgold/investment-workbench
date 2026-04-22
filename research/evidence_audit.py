"""
Evidence audit pass.

After the research brief is generated, walk each driver component and
cross-check two things:

1. Self-labeling consistency — does the component's declared
   `evidence_strength` match its `citation` field? A "cited" claim with
   no citation gets auto-demoted to "speculative." A "cited" claim whose
   citation text doesn't appear anywhere in the corpus gets flagged.

2. Numeric grounding — extract distinctive numeric literals from the
   `basis` text (percentages, dollar amounts, bps, large counts). Grep
   the evidence corpus for each. Numbers that don't appear anywhere in
   the corpus are flagged — the claim is likely confabulated.

The audit does NOT mutate the brief. It returns a list of findings that
the caller (pipeline) can surface as brief_warnings or use to downgrade
evidence_strength before rendering. This keeps audit logic reversible
and makes it easy to inspect what got flagged.

Design principle: "speculative" is a valid label, not a failure. We
downgrade silently-false "cited" claims to "speculative" so the Word
renderer can style them accordingly — we don't drop the claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# Patterns for distinctive numeric literals worth grep-checking.
# We intentionally ignore tiny integers (year tags, confidence 0.5, etc.)
# and focus on claims a human would parse as evidence.
_RE_PERCENT = re.compile(r"(?<![\w.])([+-]?\d{1,3}(?:\.\d+)?)\s*%")
_RE_DOLLAR = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*([bBmMkK])?")
_RE_BPS = re.compile(r"(?<![\w.])([+-]?\d{2,4})\s*(?:bps|basis\s+points?)", re.IGNORECASE)
_RE_UNIT_COUNT = re.compile(r"\b(\d{2,5})\s+(?:stores?|units?|restaurants?|locations?|franchises?)\b", re.IGNORECASE)


# Minimum absolute magnitude for a percent to be worth grepping.
# "3%" is too ambiguous (too many random 3s); ">=10%" and decimals like
# "3.5%" are much more distinctive.
_MIN_PCT_ABS = 1.5


@dataclass
class AuditFinding:
    """One claim that failed its self-labeled evidence grade."""
    driver: str
    component: str
    original_strength: str
    suggested_strength: str
    reason: str
    extracted_numbers: list = field(default_factory=list)
    missing_numbers: list = field(default_factory=list)
    citation: str = ""


def _extract_numeric_tokens(text: str) -> list[str]:
    """
    Pull distinctive numeric literals out of a basis string. Returns a
    list of normalized token strings like "36.8%", "$4.44", "140M",
    "350 stores", "100bps". Empty list if nothing notable.
    """
    if not text:
        return []
    tokens: list[str] = []

    # Percentages — skip trivial magnitudes and common round numbers that
    # are likely to accidentally match anywhere.
    for m in _RE_PERCENT.finditer(text):
        raw = m.group(1)
        try:
            val = float(raw)
        except ValueError:
            continue
        if abs(val) < _MIN_PCT_ABS:
            continue
        # Skip "round-number decoration" like "50%" or "100%" unless it has
        # a decimal (which makes it distinctive).
        if "." not in raw and abs(val) in (5, 10, 15, 20, 25, 50, 100):
            # still track for completeness but mark as low-distinctiveness
            tokens.append(f"{raw}%:low")
        else:
            tokens.append(f"{raw}%")

    # Dollars (optionally with B/M/K suffix)
    for m in _RE_DOLLAR.finditer(text):
        amount = m.group(1).replace(",", "")
        suffix = (m.group(2) or "").upper()
        if suffix:
            tokens.append(f"${amount}{suffix}")
        else:
            # Only capture multi-digit dollar figures; "$4" alone is too
            # common in filings.
            try:
                if float(amount) >= 1:
                    tokens.append(f"${amount}")
            except ValueError:
                pass

    # Basis points
    for m in _RE_BPS.finditer(text):
        tokens.append(f"{m.group(1)}bps")

    # Unit counts like "350 stores"
    for m in _RE_UNIT_COUNT.finditer(text):
        tokens.append(f"{m.group(1)} units")

    return tokens


def _corpus_contains(token: str, corpus_flat: str) -> bool:
    """
    Look up a normalized numeric token in the flattened lowercased corpus.
    Tolerates common typographic variants (commas, spaces, % vs percent).
    """
    if not token or not corpus_flat:
        return False
    # Strip the :low decoration if present
    core = token.split(":")[0]

    # Direct substring match
    if core.lower() in corpus_flat:
        return True

    # Percent variants: "36.8%" -> try "36.8 percent" too
    if core.endswith("%"):
        stem = core[:-1]
        if f"{stem} percent" in corpus_flat:
            return True
        if f"{stem}%" in corpus_flat:
            return True

    # Dollar + suffix variants: "$4.44B" -> also try "$4.44 billion"
    if core.startswith("$") and core[-1:].upper() in ("B", "M", "K"):
        amount = core[1:-1]
        suffix_word = {"B": "billion", "M": "million", "K": "thousand"}[core[-1].upper()]
        if f"${amount} {suffix_word}" in corpus_flat:
            return True
        if f"{amount} {suffix_word}" in corpus_flat:
            return True

    # Dollars without suffix: try the raw number
    if core.startswith("$"):
        raw = core[1:]
        # comma variant: 140 -> try 140,000,000 etc. Skip — too ambiguous.
        if raw in corpus_flat:
            return True

    # Bps variants: "140bps" -> "140 basis points"
    if core.endswith("bps"):
        stem = core[:-3]
        if f"{stem} basis points" in corpus_flat:
            return True
        if f"{stem} bps" in corpus_flat:
            return True

    # "350 units" — already tried substring; try without the word
    if core.endswith(" units"):
        stem = core.rsplit(" ", 1)[0]
        if stem in corpus_flat:
            return True

    return False


def audit_brief_evidence(brief, corpus: dict) -> list[AuditFinding]:
    """
    Run the evidence audit over every component in the brief.

    Arguments:
        brief: ResearchBrief-like object with .drivers
        corpus: dict[str, str] with labeled sources (same shape passed to
            call_adversarial_claude). None/empty entries are tolerated.

    Returns a list of AuditFinding objects. Each finding is a reason to
    downgrade a component's evidence_strength. An empty list = everything
    passed.

    Audit rules:
        A. evidence_strength="cited" with empty/short citation
           → downgrade to "speculative" (it's unbacked).
        B. evidence_strength="cited" but the citation quote text doesn't
           appear in the corpus
           → flag as "cited_unverified" and downgrade to "inferred".
        C. Any strength with distinctive numbers in `basis` that don't
           appear anywhere in the corpus
           → flag "numeric_unverified"; suggests downgrade.
        D. Missing / malformed evidence_strength field
           → default to "inferred" (most charitable).
    """
    findings: list[AuditFinding] = []

    if not brief or not getattr(brief, "drivers", None):
        return findings

    # Build a flattened, lowercased concatenation of all corpus text.
    # Used for both citation text lookup and numeric grep.
    corpus_flat = ""
    for _, txt in (corpus or {}).items():
        if txt:
            corpus_flat += " " + txt.lower()

    has_corpus = len(corpus_flat.strip()) > 200

    for d in brief.drivers:
        dname = d.get("name") or d.get("assumption_key") or "?"
        for comp in d.get("components", []) or []:
            cname = comp.get("name", "?")
            strength = (comp.get("evidence_strength") or "").strip().lower()
            citation = (comp.get("citation") or "").strip()
            basis = (comp.get("basis") or "").strip()

            # D. Missing strength
            if strength not in ("cited", "inferred", "speculative"):
                findings.append(AuditFinding(
                    driver=dname, component=cname,
                    original_strength=strength or "(missing)",
                    suggested_strength="inferred",
                    reason="evidence_strength field missing or invalid; "
                           "defaulting to 'inferred'",
                    citation=citation,
                ))
                strength = "inferred"  # continue audit with the default

            # A. Cited + empty citation → speculative
            if strength == "cited" and len(citation) < 15:
                findings.append(AuditFinding(
                    driver=dname, component=cname,
                    original_strength="cited",
                    suggested_strength="speculative",
                    reason="labeled 'cited' but citation field is empty or "
                           "too short to verify — demoted to 'speculative'",
                    citation=citation,
                ))
                continue

            # B. Cited + citation text not in corpus → inferred
            if strength == "cited" and has_corpus:
                # Use the first 60 chars of the citation as a fingerprint
                # (skip very short citations which would false-match).
                fp = citation.lower()[:60].strip()
                if len(fp) >= 20 and fp not in corpus_flat:
                    findings.append(AuditFinding(
                        driver=dname, component=cname,
                        original_strength="cited",
                        suggested_strength="inferred",
                        reason=f"citation text not found in corpus: "
                               f"'{citation[:80]}...'" if len(citation) > 80
                               else f"citation text not found in corpus: '{citation}'",
                        citation=citation,
                    ))
                    # continue — still run numeric grep below

            # C. Distinctive numbers in basis not found in corpus
            if has_corpus and basis:
                tokens = _extract_numeric_tokens(basis)
                # Filter out low-distinctiveness tokens for grep reliability
                distinctive = [t for t in tokens if not t.endswith(":low")]
                missing = [t for t in distinctive
                           if not _corpus_contains(t, corpus_flat)]
                if missing and len(missing) >= 1 and len(distinctive) >= 1:
                    # Suggest downgrade: cited → inferred, inferred → speculative
                    downgrade_map = {
                        "cited": "inferred",
                        "inferred": "speculative",
                        "speculative": "speculative",  # no change
                    }
                    suggested = downgrade_map.get(strength, strength)
                    # Only emit a finding if we're actually suggesting a change,
                    # OR if the original was cited (needs to be explicit even if
                    # we'd land on "inferred" because cited → inferred IS a change).
                    if suggested != strength or strength == "cited":
                        findings.append(AuditFinding(
                            driver=dname, component=cname,
                            original_strength=strength,
                            suggested_strength=suggested,
                            reason=f"basis contains numbers not found in "
                                   f"corpus: {', '.join(missing[:4])}",
                            extracted_numbers=distinctive,
                            missing_numbers=missing,
                            citation=citation,
                        ))

    return findings


def apply_findings_to_brief(brief, findings: Iterable[AuditFinding]) -> int:
    """
    Apply audit findings IN PLACE to the brief: update each flagged
    component's evidence_strength to the suggested value, and tag the
    basis with a short `[auto-downgraded: reason]` marker so the
    downgrade is visible to any downstream reader.

    Returns the number of components modified. Safe to call with empty
    findings list.
    """
    # Build (driver, component) -> finding map; last-write-wins.
    fmap = {}
    for f in findings:
        fmap[(f.driver, f.component)] = f

    if not fmap or not brief or not getattr(brief, "drivers", None):
        return 0

    n = 0
    for d in brief.drivers:
        dname = d.get("name") or d.get("assumption_key") or ""
        for comp in d.get("components", []) or []:
            cname = comp.get("name", "")
            key = (dname, cname)
            if key not in fmap:
                continue
            f = fmap[key]
            comp["evidence_strength"] = f.suggested_strength
            comp["_audit_note"] = f.reason
            n += 1
    return n


def summarize_findings(findings: list[AuditFinding]) -> str:
    """One-line summary suitable for verbose logs or brief_warnings."""
    if not findings:
        return "evidence audit: 0 findings, all components self-consistent"
    by_kind = {}
    for f in findings:
        key = f"{f.original_strength}→{f.suggested_strength}"
        by_kind[key] = by_kind.get(key, 0) + 1
    parts = [f"{k}: {v}" for k, v in sorted(by_kind.items())]
    return f"evidence audit: {len(findings)} finding(s) — " + ", ".join(parts)
