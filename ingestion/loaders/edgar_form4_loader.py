"""
EDGAR Form 4 Loader

Per-ticker fetch of recent Form 4 / 4-A filings (insider transactions for
directors, officers, and 10%+ owners). Parses the structured XML to extract
each non-derivative open-market sale or buy with shares, price, post-
transaction holdings — then aggregates per filer to compute:

  - total $ sold / bought in the window
  - % of stake sold = shares_sold / (shares_sold + shares_held_post_latest)
  - approx remaining stake value (shares_held_post × latest transaction
    price; not current market price, but a reasonable proxy when the
    sale was recent)

This catches what the news layer surfaces only sporadically: the actual
size of an insider's sale relative to their stake, and how much they
retain afterward. Critical for distinguishing "CEO sold $46M" (sounds
big) from "CEO sold 0.6% of his stake and retains $20B" (less alarming).

Approach:
  1. Resolve ticker -> issuer CIK (reusing the ticker_cik_cache from the
     13D loader)
  2. browse-edgar with type=4 + the issuer CIK lists all Form 4s WHERE
     this company is the issuer (filed by insiders)
  3. For each filing, fetch the primary XML doc (filenames vary;
     check the filing-index for the .xml whose form type = 4)
  4. Parse non-derivative transactions: code, date, shares, price,
     acquired/disposed, post-transaction holdings
  5. Group by reporting owner (filer); aggregate sales + buys separately;
     compute stake-relative metrics

Cache: weekly per-ticker (Form 4s drop on most days for active names but
the prior week's snapshot is already representative).

NOTE: Skips derivative transactions (option exercises, RSU conversions)
in v1 — those add complexity without changing the core "% of stake
sold" calculation. Re-add if needed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# Reuse helpers from the 13D loader (ticker -> CIK resolution)
from ingestion.loaders.edgar_13d_loader import (
    SEC_HEADERS,
    resolve_ticker_to_cik,
)


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class Form4Aggregate:
    """One filer's aggregated open-market activity over the window."""
    filer_name: str = ""
    filer_cik: str = ""
    relationship: str = ""             # "Officer: CEO, Director" | "10% Owner" | etc.
    is_officer: bool = False
    is_director: bool = False
    is_ten_pct_owner: bool = False
    officer_title: str = ""

    # Sales (transaction code 'S' or AcquiredDisposed='D' on open market)
    sale_shares: float = 0.0
    sale_value: float = 0.0
    sale_avg_price: float | None = None
    sale_n_filings: int = 0
    sale_first_date: str = ""
    sale_last_date: str = ""

    # Open-market buys (transaction code 'P')
    buy_shares: float = 0.0
    buy_value: float = 0.0
    buy_avg_price: float | None = None
    buy_n_filings: int = 0
    buy_first_date: str = ""
    buy_last_date: str = ""

    # Stake context
    shares_held_post_latest: float | None = None      # most recent
                                                       # post-transaction holdings
    latest_transaction_date: str = ""
    latest_transaction_price: float | None = None     # for stake $ approx

    # Derived: shares_held_post / (shares_held_post + sale_shares),
    # i.e. what fraction of the pre-sale stake remains after the sales
    pct_of_stake_sold: float | None = None
    # sale_value / (sale_value + shares_held_post_latest * latest_price),
    # i.e. fraction of stake-based net worth liquidated. Approximation
    # — uses Form-4 transaction price as proxy for current market price.
    pct_of_stake_value_sold: float | None = None
    approx_stake_value_remaining: float | None = None


@dataclass
class Form4Bundle:
    ticker: str = ""
    issuer_cik: str = ""
    issuer_name: str = ""
    fetched_at: str = ""
    window_days: int = 180
    n_filings_total: int = 0
    aggregates: list = field(default_factory=list)   # list[Form4Aggregate]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer_cik": self.issuer_cik,
            "issuer_name": self.issuer_name,
            "fetched_at": self.fetched_at,
            "window_days": self.window_days,
            "n_filings_total": self.n_filings_total,
            "aggregates": [asdict(a) for a in self.aggregates],
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        if not self.aggregates:
            return ""

        # Sort: sellers by $ sold desc first, then buyers by $ bought desc,
        # so the largest moves surface at the top of the block.
        sellers = [a for a in self.aggregates if a.sale_value > 0]
        buyers = [a for a in self.aggregates if a.buy_value > 0 and a.sale_value <= 0]
        sellers.sort(key=lambda a: a.sale_value, reverse=True)
        buyers.sort(key=lambda a: a.buy_value, reverse=True)

        lines = [
            f"=== INSIDER ACTIVITY (Form 4 filings on {self.ticker}, "
            f"last {self.window_days} days) ===",
            f"({self.n_filings_total} filings total. Sales = open-market "
            f"dispositions (code S); buys = open-market purchases (code P). "
            f"Skips RSU vests / tax withholdings / derivative exercises. "
            f"% of stake sold uses the LATEST post-transaction holdings on "
            f"file; stake-value % uses Form-4 transaction price as proxy "
            f"for current market.)",
            "",
        ]

        if sellers:
            lines.append("INSIDER SELLERS (open-market dispositions):")
            for a in sellers:
                role = a.relationship or "Insider"
                lines.append(f"  [SOLD] {a.filer_name} ({role})")
                detail = (
                    f"      Sold ${a.sale_value/1e6:.2f}M / "
                    f"{int(a.sale_shares):,} shares "
                    f"across {a.sale_n_filings} filing(s) "
                    f"({a.sale_first_date} to {a.sale_last_date}, "
                    f"avg ${(a.sale_avg_price or 0):.2f})"
                )
                lines.append(detail)
                if a.pct_of_stake_sold is not None:
                    pct_held = a.shares_held_post_latest or 0
                    lines.append(
                        f"      % of stake sold: {a.pct_of_stake_sold*100:.1f}% "
                        f"({int(a.sale_shares):,} of "
                        f"{int(a.sale_shares + (pct_held)):,} pre-sale)"
                    )
                if a.approx_stake_value_remaining is not None:
                    pct_value = (a.pct_of_stake_value_sold or 0) * 100
                    lines.append(
                        f"      Retains: {int(pct_held):,} shares ≈ "
                        f"${a.approx_stake_value_remaining/1e6:.1f}M "
                        f"at last sale price; "
                        f"≈ {pct_value:.1f}% of stake-value liquidated"
                    )
            lines.append("")

        if buyers:
            lines.append("INSIDER BUYERS (open-market purchases):")
            for a in buyers:
                role = a.relationship or "Insider"
                lines.append(f"  [BOUGHT] {a.filer_name} ({role})")
                lines.append(
                    f"      Bought ${a.buy_value/1e6:.2f}M / "
                    f"{int(a.buy_shares):,} shares "
                    f"across {a.buy_n_filings} filing(s) "
                    f"({a.buy_first_date} to {a.buy_last_date}, "
                    f"avg ${(a.buy_avg_price or 0):.2f})"
                )
                if a.shares_held_post_latest is not None:
                    lines.append(
                        f"      Holdings after: {int(a.shares_held_post_latest):,} shares"
                    )
            lines.append("")

        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# EDGAR browse-edgar listing
# --------------------------------------------------------------------------

# Form 4 row: type=4, type=4/A. Same row structure as 13D listing.
_FORM4_ROW_RE = re.compile(
    r'<td[^>]*>(4(?:/A)?)</td>\s*'
    r'<td[^>]*>\s*<a[^>]*href="'
    r'(/Archives/edgar/data/(\d+)/\d+/([\d-]+)-index\.htm)"',
    re.DOTALL,
)
_DATE_AFTER_RE = re.compile(r"<td[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</td>")


def _list_form4_filings(
    issuer_cik: str,
    *,
    window_days: int = 180,
    max_count: int = 100,
    verbose: bool = False,
) -> list[dict]:
    """Returns [{form_type, accession, filed_date, index_url}] for Form 4s
    on the issuer over the window. browse-edgar returns most-recent first;
    we trim by window_days client-side (the action=getcompany endpoint
    doesn't honor a date filter reliably for Form 4s)."""
    out: list[dict] = []
    cik = issuer_cik.lstrip("0").zfill(10)
    cutoff = (datetime.now() - timedelta(days=window_days)).date().isoformat()

    try:
        r = httpx.get(
            "https://www.sec.gov/cgi-bin/browse-edgar",
            params={
                "action": "getcompany",
                "CIK": cik,
                "type": "4",
                "dateb": "",
                "owner": "include",
                "count": max_count,
            },
            headers=SEC_HEADERS,
            timeout=30,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return out
    except Exception:
        return out

    body = r.text
    for m in _FORM4_ROW_RE.finditer(body):
        form_type = m.group(1).strip()
        idx_path = m.group(2)
        accession = m.group(4)
        rest = body[m.end():m.end() + 800]
        d = _DATE_AFTER_RE.search(rest)
        filed_date = d.group(1) if d else ""
        if filed_date and filed_date < cutoff:
            continue
        out.append({
            "form_type": form_type,
            "accession_number": accession,
            "filed_date": filed_date,
            "index_url": "https://www.sec.gov" + idx_path,
        })

    if verbose:
        print(f"  Form 4 loader: {len(out)} filings within {window_days}d for CIK {cik}")
    return out


# --------------------------------------------------------------------------
# Per-filing parse: find the primary XML doc, parse transactions
# --------------------------------------------------------------------------

def _find_primary_xml_url(index_url: str) -> str | None:
    """Form 4 filing-index pages list documents in a table. We want the
    RAW structured XML, not EDGAR's stylesheet-rendered HTML view.

    EDGAR serves two versions side-by-side:
      /Archives/.../xslF345X06/primarydocument.xml  ← HTML rendering
      /Archives/.../<accession>.xml                 ← raw XML data

    Both end in .xml; both appear as href links on the index page. We
    pick the path WITHOUT '/xsl' in it — that's the parseable form."""
    try:
        r = httpx.get(index_url, headers=SEC_HEADERS, timeout=30,
                      follow_redirects=True)
        if r.status_code != 200:
            return None
        body = r.text
    except Exception:
        return None

    base = "https://www.sec.gov"
    candidates = re.findall(r'href="(/Archives/[^"]+\.xml)"', body)
    raw = [u for u in candidates if "/xsl" not in u]
    if raw:
        return base + raw[0]
    # Fallback: any .xml link if no non-xsl version present
    return (base + candidates[0]) if candidates else None


def _xml_text(parent, tag: str) -> str:
    """Walk a Form 4 XML parent looking for <tag><value>X</value></tag>
    or just <tag>X</tag>. Returns '' if not found."""
    if parent is None:
        return ""
    el = parent.find(tag)
    if el is None:
        return ""
    val = el.find("value")
    if val is not None and val.text is not None:
        return val.text.strip()
    return (el.text or "").strip()


def _xml_text_deep(root, path: str) -> str:
    """Walk a deeper path; same value-or-text logic at the leaf."""
    el = root.find(path)
    if el is None:
        return ""
    val = el.find("value")
    if val is not None and val.text is not None:
        return val.text.strip()
    return (el.text or "").strip()


def _parse_form4_xml(xml_text: str) -> dict | None:
    """Parse one Form 4 XML doc. Returns dict with filer info + list of
    non-derivative transactions, or None on parse failure."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    out: dict = {
        "filer_name": "",
        "filer_cik": "",
        "is_director": False,
        "is_officer": False,
        "is_ten_pct_owner": False,
        "officer_title": "",
        "transactions": [],
    }

    # Reporting owner — Form 4s typically have ONE rptOwner per filing.
    # When there are several, we take the first; this is rare and the
    # most common multi-owner case is family trusts / joint filings where
    # picking any one is fine.
    owner = root.find("reportingOwner")
    if owner is not None:
        oid = owner.find("reportingOwnerId")
        if oid is not None:
            out["filer_name"] = _xml_text(oid, "rptOwnerName")
            out["filer_cik"] = _xml_text(oid, "rptOwnerCik").lstrip("0").zfill(10)
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            out["is_director"] = _xml_text(rel, "isDirector").lower() in ("1", "true")
            out["is_officer"] = _xml_text(rel, "isOfficer").lower() in ("1", "true")
            out["is_ten_pct_owner"] = _xml_text(rel, "isTenPercentOwner").lower() in ("1", "true")
            out["officer_title"] = _xml_text(rel, "officerTitle")

    # Non-derivative transactions
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for tx in nd_table.findall("nonDerivativeTransaction"):
            t = _parse_one_tx(tx)
            if t is not None:
                out["transactions"].append(t)

    return out


def _parse_one_tx(tx) -> dict | None:
    """Pull date, code, shares, price, A/D, post-tx holdings."""
    date = _xml_text_deep(tx, "transactionDate")
    coding = tx.find("transactionCoding")
    code = _xml_text(coding, "transactionCode") if coding is not None else ""

    amounts = tx.find("transactionAmounts")
    if amounts is None:
        return None
    shares = _xml_text(amounts, "transactionShares")
    price = _xml_text(amounts, "transactionPricePerShare")
    a_d = _xml_text(amounts, "transactionAcquiredDisposedCode")

    post = tx.find("postTransactionAmounts")
    held_after = (
        _xml_text(post, "sharesOwnedFollowingTransaction")
        if post is not None else ""
    )

    def _f(s: str) -> float | None:
        try:
            return float(s) if s else None
        except ValueError:
            return None

    return {
        "transaction_date": date,
        "transaction_code": code,
        "acquired_disposed": a_d,         # A or D
        "shares": _f(shares) or 0.0,
        "price": _f(price),
        "shares_held_after": _f(held_after),
    }


# --------------------------------------------------------------------------
# Aggregation per filer
# --------------------------------------------------------------------------

def _aggregate(parsed_filings: list[dict]) -> list[Form4Aggregate]:
    """Group by filer_cik, separate sales (S/D) from buys (P/A on open
    market), compute totals and weighted-avg prices."""
    by_filer: dict[str, dict] = {}

    for filing in parsed_filings:
        info = filing["info"]
        filed_date = filing["filed_date"]
        cik = info.get("filer_cik") or info.get("filer_name")
        if not cik:
            continue
        key = cik
        slot = by_filer.setdefault(key, {
            "info": info,
            "sales": [],
            "buys": [],
            "filings_seen": set(),
            "latest_post_holdings": None,
            "latest_post_holdings_date": "",
            "latest_transaction_price": None,
            "latest_transaction_date": "",
        })

        for tx in info["transactions"]:
            code = tx["transaction_code"]
            ad = tx["acquired_disposed"]
            shares = tx["shares"]
            price = tx["price"]
            held_after = tx["shares_held_after"]
            tx_date = tx["transaction_date"]

            # Track the most-recent post-transaction holdings (across any
            # transaction code — even RSU vests update the running total).
            if (held_after is not None
                    and (not slot["latest_post_holdings_date"]
                         or tx_date > slot["latest_post_holdings_date"])):
                slot["latest_post_holdings"] = held_after
                slot["latest_post_holdings_date"] = tx_date

            # Track the most-recent open-market price (for stake $ approx)
            if (price is not None and code in ("P", "S")
                    and (not slot["latest_transaction_date"]
                         or tx_date > slot["latest_transaction_date"])):
                slot["latest_transaction_price"] = price
                slot["latest_transaction_date"] = tx_date

            # Open-market sale: code S (most common), or 'D' on Code S
            if code == "S" and ad == "D" and shares > 0 and price is not None:
                slot["sales"].append({
                    "date": tx_date, "shares": shares, "price": price,
                    "filed": filed_date,
                })
                slot["filings_seen"].add(filing["accession_number"])
            # Open-market buy: code P (open-market purchase)
            elif code == "P" and ad == "A" and shares > 0 and price is not None:
                slot["buys"].append({
                    "date": tx_date, "shares": shares, "price": price,
                    "filed": filed_date,
                })
                slot["filings_seen"].add(filing["accession_number"])

    aggregates: list[Form4Aggregate] = []
    for cik, slot in by_filer.items():
        info = slot["info"]
        agg = Form4Aggregate(
            filer_name=info["filer_name"],
            filer_cik=info["filer_cik"],
            is_director=info["is_director"],
            is_officer=info["is_officer"],
            is_ten_pct_owner=info["is_ten_pct_owner"],
            officer_title=info["officer_title"],
            relationship=_format_relationship(info),
            shares_held_post_latest=slot["latest_post_holdings"],
            latest_transaction_date=slot["latest_post_holdings_date"],
            latest_transaction_price=slot["latest_transaction_price"],
        )

        sales = slot["sales"]
        if sales:
            total_shares = sum(s["shares"] for s in sales)
            total_value = sum(s["shares"] * s["price"] for s in sales)
            agg.sale_shares = total_shares
            agg.sale_value = total_value
            agg.sale_avg_price = total_value / total_shares if total_shares else None
            agg.sale_n_filings = len({s["filed"] for s in sales})
            dates = sorted({s["date"] for s in sales})
            agg.sale_first_date = dates[0]
            agg.sale_last_date = dates[-1]

            # % of stake sold
            held_post = agg.shares_held_post_latest or 0
            denom = total_shares + held_post
            if denom > 0:
                agg.pct_of_stake_sold = total_shares / denom

            # Approx stake value remaining + % of stake-value sold
            ref_price = (
                agg.latest_transaction_price
                or agg.sale_avg_price
            )
            if held_post and ref_price:
                stake_remaining = held_post * ref_price
                agg.approx_stake_value_remaining = stake_remaining
                if (total_value + stake_remaining) > 0:
                    agg.pct_of_stake_value_sold = (
                        total_value / (total_value + stake_remaining)
                    )

        buys = slot["buys"]
        if buys:
            total_shares = sum(b["shares"] for b in buys)
            total_value = sum(b["shares"] * b["price"] for b in buys)
            agg.buy_shares = total_shares
            agg.buy_value = total_value
            agg.buy_avg_price = total_value / total_shares if total_shares else None
            agg.buy_n_filings = len({b["filed"] for b in buys})
            dates = sorted({b["date"] for b in buys})
            agg.buy_first_date = dates[0]
            agg.buy_last_date = dates[-1]

        if sales or buys:
            aggregates.append(agg)

    return aggregates


def _format_relationship(info: dict) -> str:
    parts = []
    if info["is_officer"]:
        title = info["officer_title"] or "Officer"
        parts.append(title)
    if info["is_director"]:
        parts.append("Director")
    if info["is_ten_pct_owner"]:
        parts.append("10% Owner")
    return ", ".join(parts) if parts else ""


# --------------------------------------------------------------------------
# Top-level fetch
# --------------------------------------------------------------------------

def fetch_form4_filings(
    ticker: str,
    *,
    window_days: int = 180,
    max_filings: int = 100,
    verbose: bool = False,
) -> Form4Bundle:
    """Public API. Returns a Form4Bundle with per-filer aggregates."""
    bundle = Form4Bundle(
        ticker=ticker.upper(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        window_days=window_days,
    )

    resolved = resolve_ticker_to_cik(ticker, verbose=verbose)
    if not resolved:
        bundle.error = f"could not resolve {ticker} -> CIK"
        return bundle
    cik, name = resolved
    bundle.issuer_cik = cik
    bundle.issuer_name = name

    listings = _list_form4_filings(cik, window_days=window_days,
                                    max_count=max_filings, verbose=verbose)
    bundle.n_filings_total = len(listings)
    if not listings:
        return bundle

    parsed: list[dict] = []
    for f in listings:
        xml_url = _find_primary_xml_url(f["index_url"])
        if not xml_url:
            continue
        try:
            r = httpx.get(xml_url, headers=SEC_HEADERS, timeout=30,
                          follow_redirects=True)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        info = _parse_form4_xml(r.text)
        if info is None:
            continue
        parsed.append({
            "info": info,
            "filed_date": f["filed_date"],
            "accession_number": f["accession_number"],
        })

    bundle.aggregates = _aggregate(parsed)

    if verbose:
        print(f"  Form 4 loader: {len(bundle.aggregates)} unique insider(s) "
              f"with open-market activity in {window_days}d")

    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch Form 4 insider transactions")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--json", action="store_true",
                    help="print JSON instead of prompt text")
    args = p.parse_args()

    bundle = fetch_form4_filings(args.ticker, window_days=args.days, verbose=True)
    if args.json:
        print(json.dumps(bundle.to_dict(), indent=2, default=str))
    else:
        print()
        print(bundle.to_prompt_text() or "(no Form 4 activity in window)")


if __name__ == "__main__":
    _main()
