"""
Email Research Loader.

Pulls investment-research email from Gmail (read-only) — sell-side notes,
newsletters, and PAID Substack posts (Substack emails the full post, paywall
and all, to subscribers, so this captures paywalled research legitimately from
your own inbox without any scraping).

This module is the FETCH layer only: authenticate, pull messages matching a
query, parse the body, cache raw. Classification (is-this-research / which
ticker) and corpus integration live in separate steps on top of this.

One-time setup (see _CRED_HELP below):
  1. Create a Google Cloud project, enable the Gmail API.
  2. Make an OAuth client ID of type "Desktop app", download the JSON.
  3. Save it as data/gmail/credentials.json.
First run opens a browser for read-only consent and writes data/gmail/token.json.

CLI:
    python -m ingestion.loaders.email_research_loader --list-labels
    python -m ingestion.loaders.email_research_loader --days 30 --max 50
    python -m ingestion.loaders.email_research_loader --label Research --days 90
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

# Read-only — this loader can never modify or send mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

GMAIL_DIR = Path("data/gmail")
CRED_PATH = GMAIL_DIR / "credentials.json"
TOKEN_PATH = GMAIL_DIR / "token.json"
RAW_DIR = Path("data/email_research/raw")

_CRED_HELP = f"""
Gmail credentials not found at {CRED_PATH}.

One-time setup:
  1. https://console.cloud.google.com/  → create (or pick) a project.
  2. APIs & Services → Library → enable "Gmail API".
  3. APIs & Services → OAuth consent screen → External → add yourself as a
     test user (research@example.com).
  4. APIs & Services → Credentials → Create credentials → OAuth client ID →
     Application type "Desktop app" → download the JSON.
  5. Save it as: {CRED_PATH}

Then re-run; a browser opens once for read-only consent.
""".strip()


# --------------------------------------------------------------------------
# Data type
# --------------------------------------------------------------------------

@dataclass
class ResearchEmail:
    id: str = ""
    account: str = ""              # which inbox this came from (label, e.g. "research")
    thread_id: str = ""
    date: str = ""                 # ISO8601 (UTC)
    sender: str = ""               # display name
    sender_email: str = ""         # bare address
    subject: str = ""
    list_id: str = ""              # List-Id header (newsletters/Substack carry this)
    source_hint: str = ""          # "substack" | "newsletter" | "unknown"
    snippet: str = ""              # Gmail's short preview
    body_text: str = ""            # cleaned plain text
    n_chars: int = 0
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _token_path(account: str = "") -> Path:
    """Per-account token. Default ('') = the primary inbox (token.json); a named
    account (e.g. 'research') gets its own token_<account>.json."""
    return GMAIL_DIR / (f"token_{account}.json" if account else "token.json")


def _get_service(account: str = ""):
    """Build an authenticated read-only Gmail API service for `account`. First
    use of an account opens a browser to consent (pick THAT account's email)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GMAIL_DIR.mkdir(parents=True, exist_ok=True)
    tp = _token_path(account)
    creds = None
    if tp.exists():
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CRED_PATH.exists():
                raise FileNotFoundError(_CRED_HELP)
            flow = InstalledAppFlow.from_client_secrets_file(str(CRED_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        tp.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("utf-8") + b"===")


def _walk_for_body(payload: dict) -> tuple[str, str]:
    """Depth-first walk returning (best_plain, best_html)."""
    plain, html = "", ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    if data:
        try:
            text = _b64(data).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        if mime == "text/plain" and not plain:
            plain = text
        elif mime == "text/html" and not html:
            html = text
    for part in payload.get("parts", []) or []:
        p, h = _walk_for_body(part)
        plain = plain or p
        html = html or h
    return plain, html


def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _header(headers: list, name: str) -> str:
    nl = name.lower()
    for h in headers:
        if h.get("name", "").lower() == nl:
            return h.get("value", "")
    return ""


def _split_sender(raw: str) -> tuple[str, str]:
    """'Jane Doe <jane@x.com>' -> ('Jane Doe', 'jane@x.com')."""
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        name = raw.split("<", 1)[0].strip().strip('"')
        email = raw.split("<", 1)[1].split(">", 1)[0].strip()
        return name or email, email.lower()
    return raw, raw.lower()


def _source_hint(sender_email: str, list_id: str, headers: list) -> str:
    blob = f"{sender_email} {list_id} {_header(headers, 'List-Unsubscribe')}".lower()
    if "substack" in blob:
        return "substack"
    if list_id or _header(headers, "List-Unsubscribe"):
        return "newsletter"
    return "unknown"


def _parse_message(msg: dict) -> ResearchEmail:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    sender_name, sender_email = _split_sender(_header(headers, "From"))
    plain, html = _walk_for_body(payload)
    body = plain.strip() or (_clean_html(html) if html else "")
    # Normalize the internal epoch-ms timestamp to ISO UTC.
    iso = ""
    ms = msg.get("internalDate")
    if ms:
        try:
            iso = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            iso = ""
    list_id = _header(headers, "List-Id")
    return ResearchEmail(
        id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        date=iso,
        sender=sender_name,
        sender_email=sender_email,
        subject=_header(headers, "Subject"),
        list_id=list_id,
        source_hint=_source_hint(sender_email, list_id, headers),
        snippet=msg.get("snippet", ""),
        body_text=body,
        n_chars=len(body),
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
    )


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def list_labels(account: str = "") -> list[dict]:
    svc = _get_service(account)
    resp = svc.users().labels().list(userId="me").execute()
    return resp.get("labels", [])


def _cache_path(msg_id: str, account: str = "") -> Path:
    return RAW_DIR / (f"{account}__{msg_id}.json" if account else f"{msg_id}.json")


def fetch_messages(
    *, query: str = "", label_ids: list[str] | None = None, account: str = "",
    max_results: int = 50, force: bool = False, verbose: bool = False,
) -> list[ResearchEmail]:
    """Pull messages matching a Gmail search `query` (and/or label ids) from
    `account` (default = primary inbox).

    `query` uses Gmail search syntax, e.g. 'newer_than:90d', 'from:substack.com'.
    Results are cached per (account, message id); cached are reused unless force.
    """
    svc = _get_service(account)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    listed = svc.users().messages().list(
        userId="me", q=query or None, labelIds=label_ids or None,
        maxResults=min(max_results, 500),
    ).execute()
    ids = [m["id"] for m in listed.get("messages", [])]
    if verbose:
        print(f"  [EMAIL] {len(ids)} messages match (q={query!r}, labels={label_ids})")

    out: list[ResearchEmail] = []
    for mid in ids:
        cp = _cache_path(mid, account)
        if cp.exists() and not force:
            try:
                out.append(ResearchEmail(**{
                    k: v for k, v in json.loads(cp.read_text(encoding="utf-8")).items()
                    if k in ResearchEmail.__dataclass_fields__
                }))
                continue
            except Exception:
                pass
        try:
            msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        except Exception as e:
            if verbose:
                print(f"  [EMAIL] fetch failed {mid}: {e}")
            continue
        em = _parse_message(msg)
        em.account = account or "main"
        cp.write_text(json.dumps(em.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        out.append(em)
        if verbose:
            print(f"  [EMAIL] {em.date[:10]}  {em.source_hint:9} {em.sender[:28]:28} | {em.subject[:50]}")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m ingestion.loaders.email_research_loader")
    ap.add_argument("--list-labels", action="store_true", help="Print Gmail labels and exit")
    ap.add_argument("--query", default="", help="Gmail search query (e.g. 'from:substack.com')")
    ap.add_argument("--label", default="", help="Restrict to a Gmail label name")
    ap.add_argument("--account", default="", help="Named inbox (e.g. research); default = primary")
    ap.add_argument("--days", type=int, default=90, help="Lookback window (default 90)")
    ap.add_argument("--max", type=int, default=120, help="Max messages")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    try:
        if args.list_labels:
            for lb in sorted(list_labels(args.account), key=lambda x: x.get("name", "")):
                print(f"  {lb.get('id'):20} {lb.get('name')}")
            return 0

        label_ids = None
        if args.label:
            match = [l for l in list_labels(args.account) if l.get("name", "").lower() == args.label.lower()]
            if not match:
                print(f"No label named {args.label!r}. Run --list-labels to see options.")
                return 2
            label_ids = [match[0]["id"]]

        q = args.query or f"newer_than:{args.days}d"
        emails = fetch_messages(query=q, label_ids=label_ids, account=args.account,
                                max_results=args.max, force=args.force, verbose=True)
        print(f"\n=== pulled {len(emails)} emails ===")
        from collections import Counter
        for src, n in Counter(e.source_hint for e in emails).most_common():
            print(f"  {src:10} {n}")
    except FileNotFoundError as e:
        print(e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
