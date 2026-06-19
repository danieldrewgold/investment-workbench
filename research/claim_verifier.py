"""
Claim verification + research enrichment pass.

After the brief is built, walk every quantified claim, forward-looking
timeline, named risk, and inferred ownership/management/macro angle, and
hawk it against the public record. Findings come back as inline sub-bullets
attached to the relevant driver / risk / narrative paragraph in the Word
report.

Two kinds of topics get researched per run:

  • CLAIM_VERIFY — the brief asserted a specific factual or quantitative
    thing ("Mountain Valley operational mid-2026", "$325M interest expense",
    "Vanguard divested completely"). We go check it. Wrong-fact briefs
    produce wrong theses.

  • MISSING_ANGLE — the brief omitted something a thesis-quality analyst
    would chase given the company's profile. PE concentration crowding
    when only one PE owner is mentioned. Temperature anomaly checks for
    weather-sensitive names (beverages / AC / heaters / agri). Insider
    Form 4 activity when ownership questions are raised. The brief
    didn't think of it; we still do.

Public API:
    run_all_verifications(brief, ticker, registry_data, verbose=False)
        -> list[Verification]

The DAG step in research/dag/steps.py wraps this and caches by content hash.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import httpx

from research.deep_research import ANTHROPIC_API_KEY


# Bump when the extraction prompt or verdict schema changes — propagates
# through the DAG cache key for claim_verifications.
VERIFICATION_VERSION = "v5"  # v5: paragraph_anchor for inline placement after relevant para

_EXTRACTION_MODEL = "claude-sonnet-4-6"
_SUMMARY_MODEL = "claude-haiku-4-5-20251001"

_VALID_KINDS = {"claim_verify", "missing_angle"}
_VALID_VERDICTS = {
    "CONFIRMS", "CONTRADICTS", "UPDATES",
    "ADDS_CONTEXT", "NO_INFO",
}

# Reuse phantom_check's DDG plumbing — same endpoint, headers, regexes
DDG_HTML = "https://html.duckduckgo.com/html/"
_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
_RESULT_LINK_RE = re.compile(r'class="result__url"[^>]*>([^<]+)<')
_RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------

@dataclass
class ResearchTopic:
    """One claim to verify or missing angle to research."""
    kind: str               # "claim_verify" | "missing_angle"
    target_field: str       # "narrative" | "driver:<name>" | "contradiction:<idx>" | "edge_claim:<idx>"
    query: str              # web search query
    rationale: str          # 1-line why this matters
    verbatim_quote: str = ""  # exact text being verified (claim_verify only)
    # Verbatim phrase from the narrative_synthesis paragraph this verification
    # should render after. Renderer scans each paragraph for this string and
    # places the sub-bullet immediately below the matching paragraph.
    paragraph_anchor: str = ""


@dataclass
class Verification:
    """Result of running research on a topic."""
    topic_kind: str
    target_field: str
    query: str
    rationale: str
    verbatim_quote: str
    verdict: str              # one of _VALID_VERDICTS
    summary: str              # 1-2 sentence finding
    citation_url: str = ""
    verbatim_source_quote: str = ""
    fetched_at: str = ""
    n_search_hits: int = 0
    error: str = ""
    paragraph_anchor: str = ""   # see ResearchTopic.paragraph_anchor

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Topic extraction (1 Sonnet call against the brief)
# --------------------------------------------------------------------------

_EXTRACTION_PROMPT = """You are a hawkish research analyst reviewing a draft research brief on {ticker} ({company_name}). Your job is to identify specific topics that need verification or deeper research before the thesis can be trusted. Output a JSON list of research topics.

Each topic is either:
- "claim_verify": a specific factual or quantitative claim in the brief that should be cross-checked against public sources (10-K, press release, news, regulatory filing, EDGAR ownership filings). Wrong facts produce wrong theses.
- "missing_angle": something the brief didn't cover but that's clearly relevant given the company's revenue drivers, regulatory environment, ownership structure, or macro sensitivity.

What to look for (not exhaustive):

FORWARD TIMELINE CLAIMS — capacity expansions, factory openings, restructuring deadlines, integration milestones. Verify the exact target date against company press releases. The brief often softens or shifts dates; the original PR is canonical.

NUMERICAL CLAIMS — interest expense, tax rate, debt levels, capex, cost savings, leverage ratio. Verify against 10-K / 10-Q. If the brief inferred a number ("net interest expense likely ~$325M based on current debt levels"), do the math against actual disclosed debt + rates.

PE / HEDGE FUND OWNERSHIP AND CROWDING — IMPORTANT: when the brief mentions ONE PE sponsor by name (e.g., "One Rock Capital" or "Apollo" or "Bain"), you MUST emit a missing_angle topic that specifically probes for CO-INVESTORS and the original deal structure. Search for "<company name> sponsors co-investors merger" or "<company name> deal partners". Two PE sponsors means double the exit overhang and frequently the brief misses one. Also check 13F filings for major-holder changes. If the brief flags "complete divestiture" by a major holder, verify it's not an internal restructure (Vanguard subsidiary realignments are a recurring false alarm).

MANAGEMENT TRACK RECORD — specific past company outcomes, NOT general "experienced leader" language. If brief mentions a CEO's prior roles ("led X integration"), check the actual results at those roles — synergies hit, share price action, holding periods.

WEATHER / SEASONALITY — when revenue depends on weather-sensitive consumer demand (beverages, ice cream, snow gear, AC, heaters, agri inputs, ski resorts), check temperature anomalies vs. seasonal norms in key markets. For bottled water specifically, hot summers in major US cities drive consumption; drought is a separate (water-source-availability) signal.

INSIDER ACTIVITY — Form 4 trades by execs. If there are recent sells worth flagging, the brief should know.

REGULATORY / LEGAL OVERHANGS — pending litigation, FDA, EPA, antitrust. New developments since last 10-K.

COMPETITIVE MOVES — peer guidance changes, peer M&A, new entrants.

For each topic, output a JSON object:
- "kind": "claim_verify" or "missing_angle"
- "target_field": where to attach the verification — one of:
    - "narrative" (general; woven into the synthesis)
    - "driver:<driver_name>" — driver_name MUST match an existing driver name from the list below
    - "contradiction:<index>" — 0-indexed into the contradictions list below
    - "edge_claim:<index>" — 0-indexed into edge_claims
- "query": web search query, SHORT (4-7 keywords max). Format like a real search engine query, NOT a sentence. ALWAYS include the FULL COMPANY NAME ("{company_name}") — NOT the bare ticker — to avoid collisions with same-letter tickers (e.g. PRMB ≠ PRI; AAPL ≠ APP). Add the most distinctive proper noun / event / location / person from the claim. AVOID dollar amounts ($325M), percentages (2.5%), or specific numerics — search engines ignore them. AVOID quantifier words ("approximately", "expected", "scheduled"). GOOD: "Primo Brands Mountain Valley factory groundbreaking", "Eric Foss Aramark integration track record", "Primo Brands Vanguard 13G amendment". BAD (ticker-only, ambiguous): "PRMB net interest expense", "PRMB insider trading Form 4". BAD (numeric-laden): "Primo Brands $325M debt 10-K filing".
- "rationale": 1 sentence on why this matters to the thesis (≤200 chars)
- "verbatim_quote": for claim_verify, the exact text from the brief being checked (≤220 chars). Empty string if missing_angle.
- "paragraph_anchor": REQUIRED when target_field is "narrative". Pick a short verbatim phrase (≤80 chars) copied EXACTLY from the narrative_synthesis paragraph that this verification should render right after. The renderer searches each narrative paragraph for this string, so it must be present verbatim. For missing_angle topics, choose the paragraph most semantically relevant to the angle (e.g. an ownership probe anchors on a sentence discussing PE/sponsors; a weather check anchors on a sentence about demand/seasonality). Empty string for non-narrative target_field.

Output strict JSON only. No prose, no markdown fences. Schema: {{"topics": [...]}}.

Aim for 6-10 topics. Prioritize cases where being wrong would change the investment view. Skip trivial verifications.

## The brief to interrogate:

NARRATIVE SYNTHESIS:
{narrative_synthesis}

DRIVERS:
{drivers_summary}

CONTRADICTIONS / RISKS (numbered):
{contradictions_summary}

EDGE CLAIMS (numbered):
{edge_claims_summary}

KEY DEBATE: {key_debate}
SCHEMA / SECTOR: {schema_type}
"""


def _build_extraction_prompt(brief, ticker: str,
                              registry_data: dict | None) -> str:
    company_name = (registry_data or {}).get("name") or ticker

    drivers_lines: list[str] = []
    for d in (brief.drivers or [])[:10]:
        comps = ", ".join(
            f"{c.get('name','?')}={c.get('value','?')}"
            for c in (d.get("components") or [])[:6]
        )
        basis = (d.get("basis") or "")[:300]
        drivers_lines.append(f"- {d.get('name')}: {comps}. Basis: {basis}")
    drivers_text = "\n".join(drivers_lines) or "(no drivers)"

    contra_lines: list[str] = []
    for i, c in enumerate(brief.contradictions or []):
        thesis = (c.get("thesis") or "")[:180]
        counter = (c.get("counter_evidence") or "")[:220]
        sev = c.get("severity", "")
        contra_lines.append(f"[{i}] [{sev}] {thesis} — counter: {counter}")
    contra_text = "\n".join(contra_lines) or "(none)"

    edge_lines: list[str] = []
    for i, c in enumerate(brief.edge_claims or []):
        atype = c.get("anchor_type", "?")
        ours = c.get("our_value", "?")
        anchor = c.get("anchor_value", "?")
        rat = (c.get("rationale") or "")[:200]
        edge_lines.append(f"[{i}] {atype}: ours={ours} vs anchor={anchor}. {rat}")
    edge_text = "\n".join(edge_lines) or "(no structured edge claims)"

    return _EXTRACTION_PROMPT.format(
        ticker=ticker,
        company_name=company_name,
        narrative_synthesis=(brief.narrative_synthesis or "")[:8000],
        drivers_summary=drivers_text,
        contradictions_summary=contra_text,
        edge_claims_summary=edge_text,
        key_debate=(brief.key_debate or "")[:300],
        schema_type=(brief.schema_type or "general"),
    )


def _parse_extraction_response(text: str) -> list[ResearchTopic]:
    text = text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        # ```json\n{...}\n```
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: regex-extract the first {...} block containing "topics"
        m = re.search(r'\{[^{}]*"topics".*\}', text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    raw_topics = data.get("topics") or []
    out: list[ResearchTopic] = []
    for raw in raw_topics:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind", "")
        if kind not in _VALID_KINDS:
            continue
        out.append(ResearchTopic(
            kind=kind,
            target_field=str(raw.get("target_field", "narrative"))[:100],
            query=str(raw.get("query", ""))[:200],
            rationale=str(raw.get("rationale", ""))[:300],
            verbatim_quote=str(raw.get("verbatim_quote", ""))[:300],
            paragraph_anchor=str(raw.get("paragraph_anchor", ""))[:200],
        ))
    return out


def extract_research_topics(brief, ticker: str,
                              registry_data: dict | None = None,
                              verbose: bool = False) -> list[ResearchTopic]:
    """One Sonnet call: identify what to verify + missing angles to research."""
    if not ANTHROPIC_API_KEY:
        if verbose:
            print("  Claim verifier: no ANTHROPIC_API_KEY — skipping extraction")
        return []
    prompt = _build_extraction_prompt(brief, ticker, registry_data)

    def _post():
        return httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _EXTRACTION_MODEL,
                "max_tokens": 4096,
                "temperature": 0.3,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90.0,
        )

    resp = None
    for attempt in range(3):
        try:
            resp = _post()
        except Exception as e:
            if verbose:
                print(f"  Claim verifier: extraction request error "
                      f"{type(e).__name__}: {e}")
            time.sleep(5 + attempt * 5)
            continue
        if resp.status_code not in (429, 529):
            break
        wait = min(20.0 * (1.6 ** attempt), 120.0) + random.uniform(0, 3)
        if verbose:
            print(f"  Claim verifier: extraction HTTP {resp.status_code}, "
                  f"backoff {wait:.0f}s")
        time.sleep(wait)

    if resp is None or resp.status_code != 200:
        if verbose:
            print(f"  Claim verifier: extraction failed "
                  f"({resp.status_code if resp else 'no response'})")
        return []

    try:
        text = resp.json()["content"][0]["text"]
    except Exception as e:
        if verbose:
            print(f"  Claim verifier: extraction parse error {e}")
        return []

    topics = _parse_extraction_response(text)
    if verbose:
        print(f"  Claim verifier: {len(topics)} topic(s) extracted")
    return topics


# --------------------------------------------------------------------------
# Per-topic verification (DDG search + Haiku summary)
# --------------------------------------------------------------------------

_QUERY_STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "at", "by", "to",
    "and", "or", "with", "from", "about", "regarding", "is", "are",
    "be", "been", "was", "were", "as", "into", "via", "per", "vs",
    "approximately", "expected", "scheduled", "planned", "supposedly",
    "actual", "actuals",
}


def _simplify_query(q: str, ticker: str) -> str:
    """
    Distill a query to high-signal terms when DDG returns nothing on the
    full query. Keep proper nouns, 4-digit years, and the ticker; drop
    dollar amounts, integers, percentages, stopwords. Cap at 6 tokens —
    DDG-HTML matches better on short queries than long.
    """
    if not q:
        return ticker
    keep: list[str] = []
    for raw in q.split():
        # Strip surrounding punctuation but keep internal hyphens
        bare = re.sub(r"^[^\w]+|[^\w]+$", "", raw)
        if not bare:
            continue
        # Drop dollar / percent / integer tokens (DDG ignores them anyway)
        if re.match(r"^\$?[\d,.]+(M|B|K)?%?$", bare, re.IGNORECASE):
            # ...but keep 4-digit years
            if not (bare.isdigit() and len(bare) == 4):
                continue
        if bare.lower() in _QUERY_STOPWORDS:
            continue
        keep.append(bare)
    # Always include the ticker if absent
    if ticker and ticker.upper() not in {k.upper() for k in keep}:
        keep.insert(0, ticker)
    return " ".join(keep[:6])


def _ddg_search(query: str, verbose: bool = False,
                 timeout: float = 15.0) -> tuple[list[str], list[str]]:
    """Returns (urls, snippets). Empty lists on error."""
    try:
        r = httpx.get(
            DDG_HTML, params={"q": query}, headers=_SEARCH_HEADERS,
            timeout=timeout, follow_redirects=True,
        )
    except Exception as e:
        if verbose:
            print(f"    DDG search error: {type(e).__name__}: {e}")
        return [], []
    if r.status_code != 200:
        if verbose:
            print(f"    DDG HTTP {r.status_code}")
        return [], []
    urls = [m.strip() for m in _RESULT_LINK_RE.findall(r.text)]
    snippets: list[str] = []
    for m in _RESULT_SNIPPET_RE.findall(r.text):
        clean = re.sub(r"<[^>]+>", "", m)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            snippets.append(clean[:400])
    return urls[:8], snippets[:8]


_VERIFY_PROMPT = """You are running a hawkish web-search verification on one research topic and returning a structured verdict. Use the web_search tool (≤2 searches), then output strict JSON.

TOPIC:
- kind: {kind}
- target_field (where this attaches in the report): {target_field}
- rationale: {rationale}
- suggested search query: {query}
{verbatim_block}

After searching, output STRICT JSON only — no prose, no code fences, no preamble. Schema:

{{
  "verdict": "CONFIRMS" | "CONTRADICTS" | "UPDATES" | "ADDS_CONTEXT" | "NO_INFO",
  "summary": "1-2 sentence finding stated factually. ≤300 chars.",
  "citation_url": "single best source URL",
  "verbatim_source_quote": "short verbatim phrase from a source, ≤200 chars"
}}

Verdict guide:
- CONFIRMS: search results directly verify the verbatim claim
- CONTRADICTS: search results show the verbatim claim is wrong or materially misframed
- UPDATES: search results confirm the substance of the claim but with a different timing / number / detail. Call out the specific delta in the summary (e.g., "PR confirms spring 2026 target, not mid-2026 as brief stated")
- ADDS_CONTEXT: for missing_angle topics, search returned thesis-relevant info worth attaching to the report
- NO_INFO: search returned nothing relevant (rare with web_search; only use when truly empty)

Rules:
- Don't hedge ("seems to", "appears") if a source states something directly.
- Don't invent facts not in the search results.
- For numerical claims, cite the specific number from the source.
- citation_url should be the most authoritative source available (SEC > IR > major news > trade press).
- verbatim_source_quote must be a phrase actually present in a search result, not paraphrased.
"""


def _build_verify_prompt(topic: ResearchTopic) -> str:
    if topic.verbatim_quote:
        verbatim_block = f'- verbatim claim from brief: "{topic.verbatim_quote}"'
    else:
        verbatim_block = "- (no verbatim — this is a missing_angle topic)"
    return _VERIFY_PROMPT.format(
        kind=topic.kind,
        target_field=topic.target_field,
        rationale=topic.rationale,
        query=topic.query,
        verbatim_block=verbatim_block,
    )


def _parse_verify_response(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[^{}]*"verdict".*?\}', text, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def _disambiguate_query(query: str, company_name: str | None,
                          ticker: str) -> str:
    """
    Belt-and-suspenders against ticker collisions. If the query lacks the
    company name AND uses a generic short ticker, prepend the company name.
    Example: "PRMB insider trading" can return Primerica (PRI) results;
    "Primo Brands insider trading" disambiguates.
    """
    if not query:
        return query
    if not company_name:
        return query
    qlower = query.lower()
    # Already disambiguated if the company name (or its first word) appears
    company_first = company_name.split()[0].lower() if company_name else ""
    if company_name.lower() in qlower:
        return query
    if company_first and len(company_first) >= 4 and company_first in qlower:
        return query
    return f"{company_name} {query}".strip()


def verify_topic(topic: ResearchTopic, ticker: str,
                  company_name: str | None = None,
                  verbose: bool = False) -> Verification:
    """
    One Anthropic API call per topic with the server-side web_search tool
    enabled. Claude does both the search and the summary — we just parse
    the final JSON verdict out of its response. Single-call replaces the
    prior DDG-HTML + separate Haiku-summary approach (DDG-HTML now serves
    a 202 anti-bot challenge for scripted access, returning 0 results for
    every query).
    """
    started = datetime.now().isoformat(timespec="seconds")
    # Disambiguate the query in case Claude emitted a bare-ticker query
    # ("PRMB Form 4") that collides with another listed company.
    effective_query = _disambiguate_query(topic.query, company_name, ticker)
    base = Verification(
        topic_kind=topic.kind,
        target_field=topic.target_field,
        query=effective_query,
        rationale=topic.rationale,
        verbatim_quote=topic.verbatim_quote,
        paragraph_anchor=topic.paragraph_anchor,
        verdict="NO_INFO",
        summary="",
        fetched_at=started,
    )

    if not ANTHROPIC_API_KEY:
        base.error = "no ANTHROPIC_API_KEY"
        return base
    if not effective_query:
        base.error = "empty query"
        return base

    # Use the disambiguated query in the prompt
    topic_for_prompt = ResearchTopic(
        kind=topic.kind, target_field=topic.target_field,
        query=effective_query, rationale=topic.rationale,
        verbatim_quote=topic.verbatim_quote,
    )
    prompt = _build_verify_prompt(topic_for_prompt)
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _SUMMARY_MODEL,
                "max_tokens": 1500,
                "temperature": 0.2,
                "tools": [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 2,
                }],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120.0,
        )
    except Exception as e:
        base.error = f"request: {type(e).__name__}: {e}"
        return base

    if r.status_code != 200:
        # Surface a short error string — the body may be huge
        body_snippet = (r.text or "")[:300].replace("\n", " ")
        base.error = f"HTTP {r.status_code}: {body_snippet}"
        return base

    try:
        resp = r.json()
    except Exception as e:
        base.error = f"json parse: {e}"
        return base

    # Walk the multi-content-block response. We expect (in order):
    #   server_tool_use (Claude requests web_search)
    #   web_search_tool_result (search results)
    #   text (final JSON verdict)
    final_text = ""
    n_searches = 0
    fallback_url = ""
    for block in resp.get("content") or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("type", "")
        if bt == "text":
            final_text = block.get("text", "") or final_text
        elif bt == "server_tool_use":
            n_searches += 1
        elif bt == "web_search_tool_result":
            # Capture the first URL as a fallback citation
            content = block.get("content")
            if isinstance(content, list) and not fallback_url:
                for item in content:
                    if isinstance(item, dict):
                        u = item.get("url") or item.get("source")
                        if u:
                            fallback_url = u
                            break
    base.n_search_hits = n_searches

    if not final_text:
        base.error = "no text block in response"
        if fallback_url:
            base.citation_url = fallback_url
        return base

    parsed = _parse_verify_response(final_text)
    if not parsed:
        # Claude produced prose instead of JSON — at least surface what it said
        base.summary = final_text[:300]
        if fallback_url:
            base.citation_url = fallback_url
        return base

    verdict = str(parsed.get("verdict", "")).upper()
    if verdict not in _VALID_VERDICTS:
        verdict = "NO_INFO"
    base.verdict = verdict
    base.summary = str(parsed.get("summary", ""))[:400]
    citation = str(parsed.get("citation_url", ""))[:500]
    if not citation and fallback_url:
        citation = fallback_url
    base.citation_url = citation
    base.verbatim_source_quote = str(parsed.get("verbatim_source_quote", ""))[:300]
    return base


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def run_all_verifications(brief, ticker: str,
                            registry_data: dict | None = None,
                            max_topics: int = 12,
                            max_parallel: int = 4,
                            verbose: bool = False) -> list[Verification]:
    """
    Full pipeline: extract topics from the brief, verify each in parallel,
    return all results. Failures on individual topics don't kill the batch
    — each Verification is independent and may carry its own .error.
    """
    topics = extract_research_topics(brief, ticker, registry_data, verbose=verbose)
    if not topics:
        return []
    topics = topics[:max_topics]

    company_name = (registry_data or {}).get("name") or ticker

    if verbose:
        print(f"  Claim verifier: verifying {len(topics)} topic(s) "
              f"(max_parallel={max_parallel})...")

    results: list[Verification] = [None] * len(topics)

    def _run_one(i: int, t: ResearchTopic) -> tuple[int, Verification]:
        try:
            return i, verify_topic(t, ticker, company_name=company_name,
                                    verbose=verbose)
        except Exception as e:
            return i, Verification(
                topic_kind=t.kind, target_field=t.target_field,
                query=t.query, rationale=t.rationale,
                verbatim_quote=t.verbatim_quote,
                verdict="NO_INFO", summary="",
                error=f"verify_topic raised: {type(e).__name__}: {e}",
                fetched_at=datetime.now().isoformat(timespec="seconds"),
            )

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run_one, i, t) for i, t in enumerate(topics)]
        for f in futures:
            i, v = f.result()
            results[i] = v
            if verbose:
                snippet = v.summary[:80].replace("\n", " ")
                print(f"    [{v.verdict}] {v.target_field}: {snippet}")

    return [r for r in results if r is not None]
