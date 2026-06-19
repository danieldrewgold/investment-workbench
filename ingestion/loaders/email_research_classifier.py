"""
Email Research Classifier.

Turns raw inbox emails (from email_research_loader) into structured, analytical
records: is-this-research, which ticker(s)/theme, thesis, stance, key points —
enriched with the curated author-quality tier (data/research_authors.json).

This is the layer that feeds the knowledge base and the per-ticker brief, and
the substrate for the edge layer (does a trusted author reinforce or contradict
our view). Deterministic noise-stripping happens via the LLM category:
chat threads / unsubscribe / "you followed" / off-topic are flagged non-research.

CLI:
    python -m ingestion.loaders.email_research_classifier --limit 30
    python -m ingestion.loaders.email_research_classifier --research-only
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

from research.transcript_subagents._base import call_subagent

RAW_DIR = Path("data/email_research/raw")
CLASSIFIED_DIR = Path("data/email_research/classified")
AUTHORS_PATH = Path("data/research_authors.json")

_SYSTEM = """You classify and structure investment-research emails (newsletters / \
Substack posts) for an analyst's knowledge base. Return ONLY a JSON object, no prose.

First decide the category:
- "research": genuine analysis of a company, stock, sector, or macro/market topic \
(a thesis, financial/industry teardown, trade idea, data analysis).
- "admin": platform/account mechanics — unsubscribe confirmations, "recommendations \
from your Substacks", billing, contentless "new post" notices.
- "social": community chatter — "new thread from X", comment/like notifications.
- "promo": pure marketing/upsell, no analytical content.
- "offtopic": real writing but not about investing/markets (writing craft, lifestyle).
Set is_research=true ONLY for "research".

For research emails, extract:
- tickers: US tickers discussed (UPPERCASE, no $). Map names to tickers when \
unambiguous (Corning->GLW, Coherent->COHR, Applied Optoelectronics->AAOI, \
ClearPoint->CLPT, Nvidia->NVDA). Omit unknown/private names.
- primary_ticker: the single main subject ticker, or null.
- companies: primary company names discussed.
- themes: short topic tags (e.g. "optical","CPO","AI capex","rates","biotech").
- thesis: 1-3 sentences capturing the author's core argument.
- stance: bull | bear | neutral | mixed | na (toward the primary subject).
- key_points: up to 5 crisp bullets of the substantive claims.
- catalysts: specific upcoming events/datapoints flagged, else [].
- time_horizon: short | medium | long | na.
- notable_claims: specific falsifiable claims (numbers/predictions) worth \
verifying, else [].

For non-research, set is_research=false and leave analytical fields empty/null.
Output JSON only with keys: category, is_research, tickers, primary_ticker, \
companies, themes, thesis, stance, key_points, catalysts, time_horizon, notable_claims."""


def _load_authors() -> dict:
    try:
        return json.loads(AUTHORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"default_tier": 3, "authors": []}


def _match_author(sender: str, sender_email: str, registry: dict) -> dict:
    hay = f"{sender} {sender_email}".lower()
    for a in registry.get("authors", []):
        if any(m.lower() in hay for m in a.get("match", [])):
            return {"name": a["name"], "tier": a.get("tier", registry.get("default_tier", 3)),
                    "domains": a.get("domains", []), "source": a.get("source", "user"),
                    "matched": True, "note": a.get("note", "")}
    return {"name": sender, "tier": registry.get("default_tier", 3), "domains": [],
            "source": "default", "matched": False, "note": ""}


def _on_domain(themes: list, domains: list) -> bool:
    """Loose overlap: does the post's subject sit in the author's domain?"""
    if not domains:
        return True  # unknown author — no domain claim to violate
    blob = " ".join(str(t).lower() for t in (themes or []))
    return any(d.lower() in blob for d in domains)


def classify_email(email: dict, registry: dict | None = None,
                   *, force: bool = False, verbose: bool = False) -> dict | None:
    """Classify one cached raw email dict; cache + return the enriched record."""
    registry = registry or _load_authors()
    mid = email.get("id", "")
    if not mid:
        return None
    CLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)
    cp = CLASSIFIED_DIR / f"{mid}.json"
    if cp.exists() and not force:
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            pass

    body = (email.get("body_text") or "")[:14000]
    user_prompt = (
        f"From: {email.get('sender','')} <{email.get('sender_email','')}>\n"
        f"Subject: {email.get('subject','')}\n"
        f"Date: {email.get('date','')}\n"
        f"Source: {email.get('source_hint','')}\n\n--- BODY ---\n{body}"
    )
    res = call_subagent("email_classifier", email.get("primary_ticker", "") or "",
                        _SYSTEM, user_prompt, max_tokens=1500, verbose=verbose)
    if not res.ok or not res.data:
        if verbose:
            print(f"  [CLS] failed {mid}: {res.error}")
        return None

    d = res.data
    author = _match_author(email.get("sender", ""), email.get("sender_email", ""), registry)
    author["on_domain"] = _on_domain(d.get("themes"), author["domains"])
    # Effective weight: trusted author on their domain keeps tier; off-domain
    # drops a tier (a tier-1 semis voice isn't tier-1 on biotech).
    eff = author["tier"] + (0 if author["on_domain"] else 1)
    record = {
        "id": mid,
        "date": email.get("date", ""),
        "sender": email.get("sender", ""),
        "subject": email.get("subject", ""),
        "source_hint": email.get("source_hint", ""),
        "category": d.get("category", ""),
        "is_research": bool(d.get("is_research")),
        "tickers": d.get("tickers") or [],
        "primary_ticker": d.get("primary_ticker"),
        "companies": d.get("companies") or [],
        "themes": d.get("themes") or [],
        "thesis": d.get("thesis", ""),
        "stance": d.get("stance", ""),
        "key_points": d.get("key_points") or [],
        "catalysts": d.get("catalysts") or [],
        "time_horizon": d.get("time_horizon", ""),
        "notable_claims": d.get("notable_claims") or [],
        "author": author,
        "effective_tier": min(eff, 3),
    }
    cp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        tag = (record["primary_ticker"] or ",".join(record["tickers"][:2]) or "—")
        flag = "RESEARCH" if record["is_research"] else record["category"].upper()
        print(f"  [CLS] {flag:9} t{record['effective_tier']} {tag:8} {record['sender'][:22]:22} | {record['subject'][:46]}")
    return record


def classify_cached(*, limit: int = 0, force: bool = False, verbose: bool = False) -> list[dict]:
    registry = _load_authors()
    files = sorted(glob.glob(str(RAW_DIR / "*.json")), key=os.path.getmtime, reverse=True)
    if limit:
        files = files[:limit]
    out = []
    for f in files:
        try:
            email = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        rec = classify_email(email, registry, force=force, verbose=verbose)
        if rec:
            out.append(rec)
    return out


def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m ingestion.loaders.email_research_classifier")
    ap.add_argument("--limit", type=int, default=0, help="Classify only the N most recent")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--research-only", action="store_true", help="Only print research records")
    args = ap.parse_args()

    recs = classify_cached(limit=args.limit, force=args.force, verbose=True)
    research = [r for r in recs if r["is_research"]]
    print(f"\n=== {len(recs)} classified — {len(research)} research, {len(recs)-len(research)} noise ===")
    from collections import Counter
    print("by category:", dict(Counter(r["category"] for r in recs)))
    tick = Counter(t for r in research for t in r["tickers"])
    if tick:
        print("tickers seen:", dict(tick.most_common(12)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
