"""
FINRA TRACE Price Loader

Fetches daily TRACE bond price aggregates per CUSIP via FINRA's Data
Gateway API. Requires `FINRA_DATA_CLIENT_ID` + `FINRA_DATA_CLIENT_SECRET`
in the environment (free registration at https://gateway.finra.org).

Without credentials, the loader returns an empty bundle with `auth_status
= "no_credentials"` — downstream code (the bond health orchestrator)
treats that as "no spread monitoring this run, skip flags."

Fallback: if `data/manual_bond_prices.csv` exists with columns
[cusip, trade_date, price, yield_pct], rows are read in instead. Useful
during FINRA-credential setup or for sneaker-net pricing.

NOTE: The FINRA Data API request format below is implemented from
documented specs. Once real credentials are available, the first end-
to-end run will surface any divergence between the docs and the live
behavior — the code is structured so adjustments are isolated to the
two helper functions `_get_oauth_token` and `_query_corp_bond_aggregates`.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# Auto-load .env so FINRA_DATA_CLIENT_ID / _SECRET are picked up without
# the user having to set persistent OS env vars.
from core.env import load_dotenv  # noqa: F401


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


_FINRA_OAUTH_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
_FINRA_DATA_BASE = "https://api.finra.org/data/group/otctransparency/name"

_TOKEN_CACHE_PATH = Path("data/finra_token_cache.json")
_TOKEN_TTL_SECONDS = 60 * 50  # tokens are 1h; refresh at 50min for slack

_MANUAL_PRICES_PATH = Path("data/manual_bond_prices.csv")


# --------------------------------------------------------------------------
# Data type
# --------------------------------------------------------------------------

@dataclass
class BondPriceObservation:
    """One day's aggregated TRACE pricing for a single CUSIP."""
    cusip: str = ""
    trade_date: str = ""        # YYYY-MM-DD
    price: float | None = None  # clean price as % of par (100 = par)
    yield_pct: float | None = None
    volume: float | None = None # $ par volume
    n_trades: int | None = None
    source: str = "finra_trace"


@dataclass
class TracePriceBundle:
    cusip: str = ""
    fetched_at: str = ""
    auth_status: str = ""           # 'ok' | 'no_credentials' | 'auth_failed' | 'manual_csv'
    n_observations: int = 0
    observations: list = field(default_factory=list)  # list[BondPriceObservation]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "cusip": self.cusip,
            "fetched_at": self.fetched_at,
            "auth_status": self.auth_status,
            "n_observations": self.n_observations,
            "observations": [asdict(o) for o in self.observations],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# OAuth token management
# --------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str] | None:
    cid = os.environ.get("FINRA_DATA_CLIENT_ID", "")
    cs = os.environ.get("FINRA_DATA_CLIENT_SECRET", "")
    if cid and cs:
        return cid, cs
    return None


def _load_cached_token() -> str | None:
    if not _TOKEN_CACHE_PATH.exists():
        return None
    try:
        d = json.loads(_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        if (time.time() - d.get("issued_at", 0)) > _TOKEN_TTL_SECONDS:
            return None
        return d.get("access_token")
    except Exception:
        return None


def _save_token(token: str) -> None:
    try:
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_PATH.write_text(
            json.dumps({"access_token": token, "issued_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_oauth_token(client_id: str, client_secret: str,
                      *, verbose: bool = False) -> str | None:
    """Client-credentials grant against FINRA's OAuth endpoint."""
    cached = _load_cached_token()
    if cached:
        return cached
    try:
        r = httpx.post(
            _FINRA_OAUTH_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            if verbose:
                print(f"  TRACE auth: HTTP {r.status_code}")
            return None
        token = r.json().get("access_token")
        if token:
            _save_token(token)
        return token
    except Exception as e:
        if verbose:
            print(f"  TRACE auth: {type(e).__name__}: {e}")
        return None


# --------------------------------------------------------------------------
# Manual CSV fallback
# --------------------------------------------------------------------------

def _load_manual_prices(cusip: str, *, lookback_days: int) -> list[BondPriceObservation]:
    """Read prices for one CUSIP from data/manual_bond_prices.csv if it
    exists. Columns: cusip, trade_date (YYYY-MM-DD), price, yield_pct."""
    if not _MANUAL_PRICES_PATH.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).date().isoformat()
    out: list[BondPriceObservation] = []
    try:
        with _MANUAL_PRICES_PATH.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("cusip") or "").upper() != cusip.upper():
                    continue
                d = row.get("trade_date", "")
                if d < cutoff:
                    continue
                try:
                    obs = BondPriceObservation(
                        cusip=cusip,
                        trade_date=d,
                        price=float(row.get("price")),
                        yield_pct=float(row["yield_pct"]) if row.get("yield_pct") else None,
                        source="manual_csv",
                    )
                    out.append(obs)
                except (TypeError, ValueError):
                    continue
    except Exception:
        return []
    return out


# --------------------------------------------------------------------------
# FINRA Data API call
# --------------------------------------------------------------------------

def _query_corp_bond_aggregates(
    cusip: str,
    *,
    token: str,
    start_date: str,
    end_date: str,
    verbose: bool = False,
) -> list[BondPriceObservation]:
    """Hit FINRA Data API for daily aggregated trades on this CUSIP.

    The exact dataset name and field names below follow FINRA's
    documented OTC-transparency schema for corporate-bond aggregates.
    Returns a list of BondPriceObservation. On any error returns empty
    list (caller treats as 'no data this fetch').
    """
    url = f"{_FINRA_DATA_BASE}/corpBondAggregates"
    params = {
        "compareFilters": json.dumps([{
            "compareType": "EQUAL",
            "fieldName": "cusip",
            "fieldValue": cusip,
        }]),
        "dateRangeFilters": json.dumps([{
            "fieldName": "tradeReportDate",
            "startDate": start_date,
            "endDate": end_date,
        }]),
        "limit": 500,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            if verbose:
                print(f"  TRACE fetch: HTTP {r.status_code} for CUSIP {cusip}")
            return []
        rows = (r.json() or {}).get("data") or r.json() or []
    except Exception as e:
        if verbose:
            print(f"  TRACE fetch: {type(e).__name__}: {e}")
        return []

    out: list[BondPriceObservation] = []
    for row in rows:
        try:
            obs = BondPriceObservation(
                cusip=cusip,
                trade_date=str(row.get("tradeReportDate", "")),
                price=_to_float(row.get("weightedAvgPrice") or row.get("avgPrice")),
                yield_pct=_to_float(row.get("weightedAvgYield") or row.get("avgYield")),
                volume=_to_float(row.get("totalVolume") or row.get("totalDollarVolume")),
                n_trades=_to_int(row.get("totalTradeCount") or row.get("numberOfTrades")),
                source="finra_trace",
            )
            if obs.price is not None and obs.trade_date:
                out.append(obs)
        except Exception:
            continue
    return out


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Top-level fetch
# --------------------------------------------------------------------------

def fetch_bond_prices(
    cusip: str,
    *,
    lookback_days: int = 180,
    verbose: bool = False,
) -> TracePriceBundle:
    """Public API. Returns a TracePriceBundle with daily price observations.

    Resolution order:
      1. FINRA Data API (if FINRA_DATA_CLIENT_ID/SECRET set in env)
      2. data/manual_bond_prices.csv (if present)
      3. Empty bundle with auth_status='no_credentials'
    """
    bundle = TracePriceBundle(
        cusip=cusip.upper().strip(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )

    creds = _get_credentials()
    if creds:
        cid, cs = creds
        token = _get_oauth_token(cid, cs, verbose=verbose)
        if not token:
            bundle.auth_status = "auth_failed"
            bundle.error = "FINRA OAuth failed despite creds being set — re-check client_id/secret"
            # Try CSV fallback
            obs = _load_manual_prices(bundle.cusip, lookback_days=lookback_days)
            if obs:
                bundle.observations = obs
                bundle.n_observations = len(obs)
                bundle.auth_status = "manual_csv"
            return bundle
        end = datetime.now().date()
        start = end - timedelta(days=lookback_days)
        observations = _query_corp_bond_aggregates(
            bundle.cusip,
            token=token,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            verbose=verbose,
        )
        if not observations:
            # Try CSV fallback before declaring nothing
            observations = _load_manual_prices(bundle.cusip,
                                                lookback_days=lookback_days)
            bundle.auth_status = "manual_csv" if observations else "ok"
        else:
            bundle.auth_status = "ok"
        bundle.observations = observations
        bundle.n_observations = len(observations)
        return bundle

    # No FINRA creds — try CSV
    obs = _load_manual_prices(bundle.cusip, lookback_days=lookback_days)
    if obs:
        bundle.observations = obs
        bundle.n_observations = len(obs)
        bundle.auth_status = "manual_csv"
        return bundle

    bundle.auth_status = "no_credentials"
    bundle.error = (
        "No FINRA_DATA_CLIENT_ID/SECRET in env, no manual_bond_prices.csv. "
        "Register at https://gateway.finra.org for free OAuth credentials, "
        "OR drop a CSV at data/manual_bond_prices.csv with columns "
        "[cusip, trade_date, price, yield_pct]."
    )
    return bundle


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch bond prices from FINRA TRACE")
    p.add_argument("cusip")
    p.add_argument("--days", type=int, default=180)
    args = p.parse_args()
    b = fetch_bond_prices(args.cusip, lookback_days=args.days, verbose=True)
    print(json.dumps(b.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    _main()
