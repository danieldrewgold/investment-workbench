"""
Phantom-Entity Cross-Check.

When the adversarial audit flags a claim as "X is not mentioned in the
corpus" or "no evidence of X exists," that flag may be a false positive
— the entity is real and in the public record, we just didn't fetch the
source that mentions it. Before letting such a claim drive a contradiction
severity up to "serious," we cross-check the entity name against the
public web.

Concretely: for each adversarial contradiction whose counter_evidence
text looks like a phantom-entity flag, we extract the candidate entity
name and run a DuckDuckGo HTML search. If multiple reputable hits exist,
we demote the contradiction (phantom-flag was wrong). If no hits, we
promote it (likely genuine fabrication).

No API key required — DuckDuckGo's HTML endpoint is public.

Public API:
    cross_check_adversarial_phantoms(adv_response, ticker,
                                      verbose=False) -> list[PhantomCheckResult]

Also mutates adv_response in place: each contradiction gets a
`_phantom_check` field with the verdict, and severity gets adjusted
(serious→moderate on confirmed-real, moderate→serious on confirmed-
fabricated).
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


DDG_HTML = "https://html.duckduckgo.com/html/"
_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# --------------------------------------------------------------------------
# Phantom-flag detection
# --------------------------------------------------------------------------

# Patterns that suggest the adversarial is making a "this entity isn't
# in the corpus" claim. Order matters — more specific first.
_PHANTOM_PATTERNS = [
    re.compile(r"no\s+(?:evidence|mention|reference)\s+of\s+([^.,;]+)", re.IGNORECASE),
    re.compile(r"([^.,;:]+)\s+is\s+not\s+mentioned", re.IGNORECASE),
    re.compile(r"([^.,;:]+)\s+does\s+not\s+appear\s+in", re.IGNORECASE),
    re.compile(r"may\s+not\s+exist.*?\b([A-Z][A-Za-z0-9 ]{2,40})\b", re.IGNORECASE),
    re.compile(r"confused\s+with\s+another\s+(?:concept|initiative|product)", re.IGNORECASE),
    re.compile(r"phantom[- ]entity\s+([^.,;]+)", re.IGNORECASE),
    re.compile(r"fictional\s+or\s+obsolete[^.]*\b([A-Z][A-Za-z0-9 ]{2,40})\b", re.IGNORECASE),
]

# After we extract a candidate phrase, clean it: strip filler words and
# trim to a reasonable entity length.
_FILLER_PREFIX = re.compile(
    r"^(the|any|a|an|this|that|any such|the analyst|the supposed)\s+",
    re.IGNORECASE,
)

# Tokens that signal the entity name has ended — we truncate the raw
# regex capture at the first occurrence so "Smart Kitchen technology in
# current filings suggests..." becomes "Smart Kitchen technology".
_ENTITY_TERMINATORS = (
    # Prepositions that follow an entity name
    " in ", " at ", " on ", " by ", " from ", " for ", " with ", " within ",
    " during ", " across ",
    # Verbs commonly attached to a phantom-flag phrase
    " suggests ", " appears ", " appeared ", " seems ", " seemed ",
    " implies ", " implied ", " exists ", " existed ",
    # Connectives
    " which ", " that ", " whose ", " could ", " may ", " might ",
    # Punctuation-like
    " — ", " – ",
)

# Keep only proper-noun-like entities: require at least one capital letter
# in the first two words, otherwise reject (avoids capturing generic prose).
_HAS_PROPER_NOUN = re.compile(r"^[^\w]*[A-Z]")


def _extract_phantom_entity(counter_evidence: str) -> str | None:
    """
    Try to pull the name of the entity being flagged as phantom out of
    a counter_evidence string. Returns a cleaned candidate or None if
    no pattern matches.
    """
    if not counter_evidence or len(counter_evidence) < 15:
        return None
    for pat in _PHANTOM_PATTERNS:
        m = pat.search(counter_evidence)
        if not m:
            continue
        # pat may have 0 or 1 group; if 0 groups we can't extract an entity
        if pat.groups < 1:
            return None
        try:
            raw = m.group(1).strip()
        except (IndexError, Exception):
            continue
        # Truncate at the first entity-terminator token so we keep the
        # phantom entity name but drop the follow-on clause.
        lower = raw.lower()
        cut = len(raw)
        for term in _ENTITY_TERMINATORS:
            idx = lower.find(term)
            if idx != -1 and idx < cut:
                cut = idx
        entity = raw[:cut].strip(" .,;:\"'—")
        # Clean filler prefix
        entity = _FILLER_PREFIX.sub("", entity)

        # Find the rightmost run of Capital-Case / ALL-CAPS words — that's
        # the entity name. E.g. "but Project Zenith" -> "Project Zenith".
        # "Smart Kitchen technology" -> "Smart Kitchen technology" (single
        # run including the lowercase descriptor, which is fine).
        words = entity.split()
        # Walk right-to-left collecting the last uninterrupted capitalized run
        runs: list[list[str]] = [[]]
        for w in words:
            if not w:
                continue
            if w[0].isupper() or (runs[-1] and not w[0].isupper() and len(runs[-1]) >= 1):
                # Continue the current run; allow one lowercase tail-word
                runs[-1].append(w)
            else:
                if runs[-1]:
                    runs.append([])
        # Best run: longest capitalized-prefix sequence
        best = None
        for r in runs:
            if not r:
                continue
            if not r[0][0].isupper():
                continue
            if best is None or len(r) > len(best):
                best = r
        if best:
            entity = " ".join(best)
        # Cap word count to avoid grabbing sentence fragments
        if len(entity.split()) > 6:
            entity = " ".join(entity.split()[:6])
        # Must be proper-noun-ish and a reasonable length
        if not _HAS_PROPER_NOUN.search(entity):
            continue
        if 3 <= len(entity) <= 60:
            return entity
    return None


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------

_RESULT_LINK_RE = re.compile(r'class="result__url"[^>]*>([^<]+)<')
_RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)


@dataclass
class PhantomCheckResult:
    """Verdict on one phantom-entity flag."""
    contradiction_index: int       # position in adv_response.new_contradictions
    entity: str
    search_query: str
    hit_count: int = 0
    top_urls: list = field(default_factory=list)
    snippets: list = field(default_factory=list)
    verdict: str = "unknown"        # "real" | "fabricated" | "unknown"
    reason: str = ""
    error: str = ""


def _search_duckduckgo(query: str, verbose: bool = False,
                       timeout: float = 15.0) -> tuple[list[str], list[str]]:
    """
    Return (urls, snippets). Empty lists on error.
    """
    try:
        r = httpx.get(
            DDG_HTML, params={"q": query}, headers=_SEARCH_HEADERS,
            timeout=timeout, follow_redirects=True,
        )
    except Exception as e:
        if verbose:
            print(f"    DDG: {type(e).__name__}: {e}")
        return [], []
    if r.status_code != 200:
        if verbose:
            print(f"    DDG: HTTP {r.status_code}")
        return [], []

    urls = [m.strip() for m in _RESULT_LINK_RE.findall(r.text)]
    snippets = []
    for m in _RESULT_SNIPPET_RE.findall(r.text):
        # Strip tags and excess whitespace
        clean = re.sub(r"<[^>]+>", "", m)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            snippets.append(clean[:300])
    return urls[:10], snippets[:10]


# --------------------------------------------------------------------------
# Verdict logic
# --------------------------------------------------------------------------

# Reputation heuristic — when these domains appear in the hits, we treat
# the entity as genuinely real (not a fabrication). Not an exhaustive list;
# enough to cut through noise confidently.
_REPUTABLE_DOMAIN_HINTS = (
    "sec.gov", "bloomberg.com", "reuters.com", "wsj.com", "ft.com",
    "nytimes.com", "cnbc.com", "forbes.com", "businessinsider.com",
    "marketwatch.com", "seekingalpha.com", "fool.com", "barrons.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com",
    "restaurantbusinessonline.com", "restaurantdive.com", "qsrmagazine.com",
    "techcrunch.com", "theverge.com", "cnet.com", "wired.com",
    "investor.", "ir.",              # company IR pages
)


def _score_hits(entity: str, ticker: str, urls: list[str],
                 snippets: list[str]) -> tuple[str, str]:
    """
    Classify an entity as real/fabricated/unknown based on the hits.
    Returns (verdict, reason).

    Tightness notes:
      - The entity's FULL phrase must appear in a snippet to count as a
        co-occurrence hit. Loose word-level matching false-positives on
        common terms (e.g. "blockchain" alone would match many pages
        even for a fabricated "NeoQuantum Blockchain" entity).
      - We also check that the entity phrase appears in URLs or snippets
        at all. DDG returns generic/related results for nonsense queries,
        so "hits > 0" alone is a weak signal.
    """
    n_urls = len(urls)
    if n_urls == 0:
        return ("fabricated",
                "no DuckDuckGo results for the entity name paired with the ticker")

    entity_lower = entity.lower()
    # Strict: does the exact entity phrase appear in any snippet OR url?
    n_strict = 0
    combined_text = (" ".join(snippets) + " " + " ".join(urls)).lower()
    if entity_lower in combined_text:
        # Count occurrences across snippets+urls
        n_strict = combined_text.count(entity_lower)
    # Also try: entity's first two words as a phrase (for cases like
    # "Smart Kitchen technology" where only "Smart Kitchen" appears)
    entity_short = " ".join(entity_lower.split()[:2])
    if entity_short != entity_lower and entity_short in combined_text:
        n_strict = max(n_strict, combined_text.count(entity_short))

    # Reputable domain check
    n_reputable = sum(
        1 for u in urls
        if any(hint in u.lower() for hint in _REPUTABLE_DOMAIN_HINTS)
    )

    # Decision tree:
    # Strong positive: reputable + strict entity match -> real
    # Moderate: many strict entity matches without reputable -> real (common
    #   for products with trade-press coverage but not Bloomberg/WSJ)
    # Weak: strict matches exist but low count -> unknown
    # Negative: no strict matches at all -> fabricated (DDG returned
    #   unrelated results)
    if n_strict >= 2 and n_reputable >= 1:
        return ("real",
                f"{n_urls} results incl. {n_strict} mentions of "
                f"'{entity}' and {n_reputable} from reputable domains")
    if n_strict >= 3:
        return ("real",
                f"{n_urls} results with {n_strict} direct mentions of '{entity}'")
    if n_strict == 0:
        return ("fabricated",
                f"{n_urls} DDG results but NONE contain '{entity}' — "
                f"search engine returned unrelated fallback results")
    if n_strict <= 2 and n_reputable == 0:
        return ("unknown",
                f"{n_urls} results, only {n_strict} direct mentions, "
                f"no reputable domains — can't confidently verify")
    return ("unknown",
            f"{n_urls} results but insufficient strict-match signal "
            f"(strict={n_strict}, reputable={n_reputable})")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def cross_check_adversarial_phantoms(
    adv_response: dict | None,
    ticker: str,
    max_checks: int = 4,
    verbose: bool = False,
) -> list[PhantomCheckResult]:
    """
    Walk adv_response.new_contradictions, detect phantom-entity flags,
    run DuckDuckGo verification on each, and MUTATE the response in place
    so severity reflects the verdict:

      • verdict="real"       -> severity downgraded (serious→moderate,
                                 moderate→minor)
      • verdict="fabricated" -> severity upgraded if possible
      • verdict="unknown"    -> severity unchanged; _phantom_check still
                                 attached for transparency

    Caps at `max_checks` to keep a single run under ~15s of search time.
    """
    if not adv_response:
        return []

    contras = adv_response.get("new_contradictions") or []
    results: list[PhantomCheckResult] = []
    checked = 0

    for i, c in enumerate(contras):
        if checked >= max_checks:
            break
        counter = c.get("counter_evidence", "") or ""
        entity = _extract_phantom_entity(counter)
        if not entity:
            continue

        checked += 1
        # Build the search query: ticker + entity (if a stock ticker
        # appears to be meaningful here) or company context.
        query = f"{ticker} {entity}"
        if verbose:
            print(f"  Phantom check #{checked}: '{entity}' (query: '{query}')")

        result = PhantomCheckResult(
            contradiction_index=i,
            entity=entity,
            search_query=query,
        )

        try:
            urls, snippets = _search_duckduckgo(query, verbose=verbose)
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            results.append(result)
            continue

        result.hit_count = len(urls)
        result.top_urls = urls[:5]
        result.snippets = snippets[:3]
        verdict, reason = _score_hits(entity, ticker, urls, snippets)
        result.verdict = verdict
        result.reason = reason

        # Mutate the contradiction with the check outcome
        c["_phantom_check"] = {
            "entity": entity,
            "verdict": verdict,
            "reason": reason,
            "hits": len(urls),
        }

        # Adjust severity per verdict
        sev = (c.get("severity") or "").lower()
        if verdict == "real":
            # Demote severity — the adversarial was wrong about absence
            new_sev = {"serious": "moderate", "moderate": "minor",
                       "minor": "minor"}.get(sev, sev)
            if new_sev != sev:
                c["severity"] = new_sev
                if verbose:
                    print(f"    verdict=real, severity {sev} -> {new_sev}")
            # Also tag so the renderer can show the cross-check
            c["counter_evidence"] = (
                counter.rstrip(". ") +
                f". [Phantom-check: entity '{entity}' found in {len(urls)} "
                f"web results incl. reputable sources — this absence claim "
                f"is likely wrong; the entity is real but may not be in the "
                f"fetched corpus.]"
            )
        elif verdict == "fabricated":
            # Promote severity — the flag is confirmed as fabrication
            new_sev = {"minor": "moderate", "moderate": "serious",
                       "serious": "serious"}.get(sev, sev)
            if new_sev != sev:
                c["severity"] = new_sev
                if verbose:
                    print(f"    verdict=fabricated, severity {sev} -> {new_sev}")
            c["counter_evidence"] = (
                counter.rstrip(". ") +
                f". [Phantom-check: entity '{entity}' returned only "
                f"{len(urls)} web results — likely fabricated or confused.]"
            )

        results.append(result)
        # Polite delay between queries
        time.sleep(0.8)

    return results
