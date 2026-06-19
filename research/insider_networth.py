"""
Insider net-worth estimator (disclosed public equity).

Given a reporting owner's SEC CIK, aggregates their LATEST disclosed holding
in every company they file Form 4s for, and values each at the current market
price. The sum is "estimated net worth (disclosed public equity)" — the same
thing GuruFocus / Benzinga / MarketScreener report, but computed in-house,
deterministically, and free.

This is what makes the insider panel honest about "across stock": a director on
six boards (e.g. Susan Decker: Berkshire, Costco, Vail, Vox ...) shows her full
disclosed stake, not just the one company. It does NOT capture private / non-
public wealth, which no filing discloses — so it remains a floor/estimate.

Public API:
    estimate_networth(owner_cik, *, verbose=False) -> dict
      {est_disclosed_equity, n_companies, n_valued, companies:[...],
       as_of, basis, error}

Cached per owner CIK, daily, under data/insider_networth_cache/.
"""

from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx

from ingestion.loaders.edgar_13d_loader import SEC_HEADERS

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_CACHE_DIR = Path("data/insider_networth_cache")
_TTL_SECONDS = 24 * 60 * 60
_BASE = "https://www.sec.gov"

# in-process price memo so one run doesn't re-hit yfinance for shared issuers
_PRICE_MEMO: dict[str, float | None] = {}


def _cached(cik: str) -> dict | None:
    p = _CACHE_DIR / f"{cik}.json"
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) > _TTL_SECONDS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(cik: str, d: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{cik}.json").write_text(json.dumps(d, default=str), encoding="utf-8")
    except Exception:
        pass


def _get(url: str) -> str | None:
    try:
        r = httpx.get(url, headers=SEC_HEADERS, timeout=30, follow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def _price(symbol: str) -> float | None:
    if not symbol:
        return None
    sym = symbol.upper().replace(".", "-")
    if sym in _PRICE_MEMO:
        return _PRICE_MEMO[sym]
    px = None
    try:
        import yfinance as yf
        fi = yf.Ticker(sym).fast_info
        px = fi.get("last_price") or fi.get("lastPrice")
        if px is not None:
            px = float(px)
    except Exception:
        px = None
    _PRICE_MEMO[sym] = px
    return px


def _raw_xml_url(prefix_cik: str, accession: str) -> str | None:
    acc_nodash = accession.replace("-", "")
    idx = f"{_BASE}/Archives/edgar/data/{int(prefix_cik)}/{acc_nodash}/{accession}-index.htm"
    body = _get(idx)
    if not body:
        return None
    cands = re.findall(r'href="(/Archives/[^"]+\.xml)"', body)
    raw = [u for u in cands if "/xsl" not in u]
    if raw:
        return _BASE + raw[0]
    return (_BASE + cands[0]) if cands else None


def _leaf(el) -> str:
    if el is None:
        return ""
    v = el.find("value")
    if v is not None and v.text is not None:
        return v.text.strip()
    return (el.text or "").strip()


def _parse_issuer_holding(xml_text: str) -> dict | None:
    """From one Form 4 XML: issuer cik/name/symbol + the owner's most-recent
    post-transaction share count and a fallback transaction price."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    iss = root.find("issuer")
    if iss is None:
        return None
    out = {
        "issuer_cik": (iss.findtext("issuerCik") or "").strip().lstrip("0").zfill(10),
        "issuer_name": (iss.findtext("issuerName") or "").strip(),
        "symbol": (iss.findtext("issuerTradingSymbol") or "").strip(),
        "shares": None,
        "as_of": "",
        "tx_price": None,
    }
    best_date = ""
    nd = root.find("nonDerivativeTable")
    if nd is not None:
        rows = nd.findall("nonDerivativeTransaction") + nd.findall("nonDerivativeHolding")
        for tx in rows:
            post = tx.find("postTransactionAmounts")
            held = _leaf(post.find("sharesOwnedFollowingTransaction")) if post is not None else ""
            date = _leaf(tx.find("transactionDate")) or out["as_of"]
            amt = tx.find("transactionAmounts")
            price = _leaf(amt.find("transactionPricePerShare")) if amt is not None else ""
            if held:
                try:
                    h = float(held)
                except ValueError:
                    continue
                if not best_date or date >= best_date:
                    best_date = date
                    out["shares"] = h
                    out["as_of"] = date
            if price:
                try:
                    out["tx_price"] = float(price)
                except ValueError:
                    pass
    return out


def estimate_networth(owner_cik: str, *, max_companies: int = 15,
                      use_cache: bool = True, verbose: bool = False) -> dict:
    cik10 = str(owner_cik).lstrip("0").zfill(10)
    if use_cache:
        c = _cached(cik10)
        if c is not None:
            return c

    result = {
        "owner_cik": cik10,
        "est_disclosed_equity": None,
        "n_companies": 0,
        "n_valued": 0,
        "companies": [],
        "as_of": datetime.now().date().isoformat(),
        "basis": "SEC Form 4 disclosed holdings across issuers, valued at current price",
        "error": "",
    }

    subs = _get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    if not subs:
        result["error"] = "submissions fetch failed"
        return result
    try:
        rec = json.loads(subs).get("filings", {}).get("recent", {})
    except Exception:
        result["error"] = "submissions parse failed"
        return result

    forms = rec.get("form", [])
    accs = rec.get("accessionNumber", [])
    dates = rec.get("filingDate", [])
    # latest Form-4 accession per archive-prefix (a proxy for issuer that
    # bounds how many docs we fetch)
    latest_by_prefix: dict[str, tuple[str, str]] = {}
    for i, fm in enumerate(forms):
        if fm not in ("4", "4/A"):
            continue
        acc = accs[i]
        prefix = acc.split("-")[0]
        d = dates[i] if i < len(dates) else ""
        if prefix not in latest_by_prefix or d > latest_by_prefix[prefix][1]:
            latest_by_prefix[prefix] = (acc, d)

    by_issuer: dict[str, dict] = {}
    for prefix, (acc, _d) in list(latest_by_prefix.items())[: max_companies + 5]:
        url = _raw_xml_url(prefix, acc)
        if not url:
            continue
        xml = _get(url)
        if not xml:
            continue
        ih = _parse_issuer_holding(xml)
        if not ih or ih.get("shares") is None or not ih.get("issuer_cik"):
            continue
        key = ih["issuer_cik"]
        if key not in by_issuer or ih["as_of"] >= by_issuer[key]["as_of"]:
            by_issuer[key] = ih
        time.sleep(0.12)

    total = 0.0
    n_valued = 0
    companies = []
    for key, ih in by_issuer.items():
        px = _price(ih["symbol"]) or ih.get("tx_price")
        val = (ih["shares"] * px) if (px and ih["shares"]) else None
        if val is not None:
            total += val
            n_valued += 1
        companies.append({
            "issuer": ih["issuer_name"],
            "symbol": ih["symbol"],
            "shares": ih["shares"],
            "price": px,
            "value": val,
            "as_of": ih["as_of"],
        })
    companies.sort(key=lambda c: -(c["value"] or 0))
    result["companies"] = companies
    result["n_companies"] = len(companies)
    result["n_valued"] = n_valued
    result["est_disclosed_equity"] = total if n_valued else None
    if verbose:
        print(f"  net worth {cik10}: ${ (total/1e6) :.1f}M across {len(companies)} co "
              f"({n_valued} valued)")
    _save(cik10, result)
    return result


def _main():
    import argparse
    p = argparse.ArgumentParser(description="Estimate insider net worth from SEC Form 4s")
    p.add_argument("cik")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()
    d = estimate_networth(args.cik, use_cache=not args.no_cache, verbose=True)
    print(json.dumps(d, indent=2, default=str))


if __name__ == "__main__":
    _main()
