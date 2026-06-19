#!/usr/bin/env python3
"""Orchestrate the inbox-research pipeline end to end: fetch -> classify ->
edge-synth (with workbench cross-ref). One command, cron-able.

  python refresh_research.py                      # default: Substack, last 30d
  python refresh_research.py --known-senders      # broaden to every sender we've
                                                  #   previously classified as research
  python refresh_research.py --query "category:updates newer_than:30d"   # wide net
  python refresh_research.py --no-fetch            # just re-classify + re-synth
  python refresh_research.py --edge-min-sources 1  # synth even single-source names

Broadening is safe: the auto-classifier marks non-research (chat / unsubscribe /
personal) is_research=false, so a wider pull doesn't pollute the knowledge base.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _authed_accounts() -> list:
    """Every Gmail account we have a token for: '' = primary (token.json),
    named = token_<name>.json. So a plain run pulls all connected inboxes."""
    import glob
    import os
    accts = []
    for f in glob.glob("data/gmail/token*.json"):
        b = os.path.basename(f)[:-5]
        if b == "token":
            accts.append("")
        elif b.startswith("token_"):
            accts.append(b[len("token_"):])
    return accts or [""]


def _known_sender_query(days: int) -> str:
    """Build a Gmail query from senders previously classified as research —
    a self-growing allowlist, so we pull wider without hoovering personal mail."""
    from research.external_research import load_classified
    domains = set()
    for r in load_classified():
        if not r.get("is_research"):
            continue
        em = (r.get("sender_email") or r.get("author", {}).get("name", "")).lower()
        if "@" in em:
            domains.add(em.split("@", 1)[1])
    domains.add("substack.com")
    froms = " OR ".join(f"from:{d}" for d in sorted(domains))
    return f"({froms}) newer_than:{days}d"


def main():
    ap = argparse.ArgumentParser(prog="python refresh_research.py")
    ap.add_argument("--query", default="", help="Gmail search query (default: Substack)")
    ap.add_argument("--accounts", default="", help="Comma list of named inboxes to pull "
                    "(e.g. 'research'); empty = primary inbox only. Primary is always included.")
    ap.add_argument("--known-senders", action="store_true",
                    help="Pull from every previously-classified research sender + Substack")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("--edge-min-sources", type=int, default=2,
                    help="Only synthesize edge for tickers with >= N research pieces")
    ap.add_argument("--no-fetch", action="store_true", help="Skip Gmail fetch; re-process cache")
    ap.add_argument("--force", action="store_true", help="Re-classify even cached emails")
    args = ap.parse_args()

    # 1. Fetch — primary inbox plus any named accounts
    if not args.no_fetch:
        from ingestion.loaders.email_research_loader import fetch_messages
        if args.known_senders:
            q = _known_sender_query(args.days)
        else:
            q = args.query or f"from:substack.com newer_than:{args.days}d"
        if args.accounts.strip():
            accounts = [""] + [a.strip() for a in args.accounts.split(",") if a.strip()]
        else:
            accounts = _authed_accounts()  # default: every connected inbox
        accounts = list(dict.fromkeys(accounts))  # de-dup, preserve order
        total = 0
        for acct in accounts:
            try:
                got = fetch_messages(query=q, account=acct, max_results=args.max,
                                     force=False, verbose=False)
                total += len(got)
                print(f"[1/3] {acct or 'primary':10} fetched {len(got)}")
            except Exception as e:
                print(f"[1/3] {acct or 'primary':10} fetch FAILED: {type(e).__name__}: {e}")
        print(f"[1/3] total fetched {total}  (q={q[:70]}{'…' if len(q) > 70 else ''})")
    else:
        print("[1/3] fetch skipped (--no-fetch)")

    # 2. Classify (cached emails are reused unless --force)
    from ingestion.loaders.email_research_classifier import classify_cached
    recs = classify_cached(force=args.force, verbose=False)
    research = [r for r in recs if r.get("is_research")]
    print(f"[2/3] classified {len(recs)} emails — {len(research)} research, "
          f"{len(recs) - len(research)} noise stripped")

    # 3. Edge synth (with workbench cross-ref) for tickers with enough coverage
    from research.external_research import universe_coverage
    from research.edge_synthesis import run_training_iteration
    cov = universe_coverage(research)
    targets = [t for t, n in cov.items() if n >= args.edge_min_sources]
    stamp = datetime.now(tz=timezone.utc).isoformat()
    print(f"[3/3] synthesizing edge for {len(targets)} tickers "
          f"(>= {args.edge_min_sources} sources)")
    rows = []
    for t in targets:
        r = run_training_iteration(t, stamp=stamp, verbose=False)
        e = r.get("log_entry", {})
        rows.append((t, e.get("edge_score"), cov[t]))
        print(f"   {t:6} edge {e.get('edge_score')}/5   ({cov[t]} sources)")
    print(f"\nDONE — {len(rows)} edge theses at data/email_research/edge/. "
          f"Browse: dashboard /research and /co/<T>/research")


if __name__ == "__main__":
    main()
