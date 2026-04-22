"""
BLS Macro Loader — Bureau of Labor Statistics public API v2.

Adds labor-cost and category-level inflation data that FRED's headline
series can't provide. For restaurant / consumer theses these are the
most directly relevant numbers an analyst would actually cite:
  • Food away from home CPI (restaurant pricing power)
  • Food at home CPI (grocery competitor)
  • Leisure & Hospitality hourly earnings (restaurant labor cost)
  • Retail Trade hourly earnings (retail labor cost)

Public API:
    fetch_bls_context(verbose=False, force_refresh=False) -> BLSContext

No API key required for the public tier (25 queries/day, 10 years max
per series). If a BLS_API_KEY env var is set, we include it in the
request which bumps the tier to 500/day and 20 years — nice to have
but not required.

CLI:
    python -m ingestion.loaders.bls_macro_loader [--refresh]
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------
# Curated series
# --------------------------------------------------------------------------

# (series_id, label, unit, relevance_note)
# Curated for equity research — categories that drive actual investment
# theses, not macro dashboards.
BLS_SERIES = [
    # ── Category-level CPI (monthly) ──
    ("CUUR0000SEFV",   "Food away from home CPI (restaurant/QSR pricing)",
     "index", "Restaurant industry's revenue-growth ceiling. When this slows, "
              "QSR SSS comes more from traffic and less from price."),
    ("CUUR0000SAF11",  "Food at home CPI (grocery)",
     "index", "Grocery competitor pricing. When food-at-home inflation outpaces "
              "food-away-from-home, consumers shift to cooking at home → "
              "restaurant traffic headwind."),
    ("CUUR0000SA0",    "CPI All Urban Consumers (headline)",
     "index", "Headline cost pressure; sets wage expectations."),
    ("CUUR0000SAR",    "Recreation services CPI",
     "index", "Leisure discretionary demand indicator."),

    # ── Sector hourly earnings (monthly) ──
    ("CES7000000003",  "Avg hourly earnings: Leisure & Hospitality",
     "dollars", "Direct restaurant labor-cost proxy. When this outpaces menu "
                "price inflation (food away CPI), restaurant margins compress."),
    ("CES4200000003",  "Avg hourly earnings: Retail Trade",
     "dollars", "Retail labor cost; same structural pressure point for "
                "consumer-discretionary retailers."),
    ("CES0500000003",  "Avg hourly earnings: Total private",
     "dollars", "Broad wage growth; inflation offset for consumer spending."),

    # ── Labor market tightness (monthly) ──
    ("CES7072200001",  "Employment: Food services and drinking places",
     "thousands", "Restaurant staffing levels; turning points here precede "
                  "unit-growth slowdowns."),
]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class BLSSeries:
    """One BLS time series with trend signal."""
    series_id: str
    label: str
    unit: str
    relevance_note: str
    latest_value: float | None = None
    latest_period: str = ""               # "2026-M03"
    latest_date: str = ""                  # YYYY-MM-DD (derived)
    value_12m_ago: float | None = None
    change_12m_pct: float | None = None
    trend_direction: str = ""              # "up" / "down" / "flat"
    n_observations: int = 0
    error: str = ""

    def summary_line(self) -> str:
        if self.error or self.latest_value is None:
            return f"{self.label}: unavailable ({self.error or 'no data'})"
        parts = [f"{self.label}: "]
        if self.unit == "dollars":
            parts.append(f"${self.latest_value:.2f}")
        elif self.unit == "index":
            parts.append(f"{self.latest_value:.2f}")
        elif self.unit == "thousands":
            parts.append(f"{self.latest_value:,.0f}k")
        else:
            parts.append(f"{self.latest_value:.2f}")
        parts.append(f" (as of {self.latest_date or self.latest_period})")
        if self.change_12m_pct is not None:
            arrow = "↑" if self.change_12m_pct > 1 else "↓" if self.change_12m_pct < -1 else "≈"
            parts.append(f", {arrow} {self.change_12m_pct:+.1f}% YoY")
        return "".join(parts)


@dataclass
class BLSContext:
    fetched_at: str = ""
    series: dict = field(default_factory=dict)   # {series_id: BLSSeries dict}

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "series": {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
                       for k, v in self.series.items()},
        }

    def to_prompt_text(self) -> str:
        if not self.series:
            return ""
        lines = [
            "=== BLS CONTEXT (labor + category inflation — cite figures when used) ===",
        ]
        for sid, s in self.series.items():
            if isinstance(s, BLSSeries):
                line = s.summary_line()
                note = s.relevance_note
            else:
                bs = BLSSeries(**s)
                line = bs.summary_line()
                note = bs.relevance_note
            lines.append(f"  • {line}")
            if note:
                lines.append(f"    (relevance: {note})")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

_CACHE_DIR = Path("data/macro_cache")
_CACHE_FILE = _CACHE_DIR / "bls_macro_daily.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age = time.time() - _CACHE_FILE.stat().st_mtime
    return age < _CACHE_TTL_SECONDS


def _load_cache() -> BLSContext | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        d = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        ctx = BLSContext(fetched_at=d.get("fetched_at", ""))
        known = {f for f in BLSSeries.__dataclass_fields__}
        for sid, sdata in (d.get("series") or {}).items():
            ctx.series[sid] = BLSSeries(
                **{k: v for k, v in sdata.items() if k in known}
            )
        return ctx
    except Exception:
        return None


def _save_cache(ctx: BLSContext) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(
        json.dumps(ctx.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# BLS API
# --------------------------------------------------------------------------

BLS_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


def _period_to_date(year: str, period: str) -> str:
    """
    Convert BLS period codes to YYYY-MM-DD at start-of-month.
    'M01' -> January, 'M13' -> annual average (treat as Jan 1).
    """
    try:
        y = int(year)
    except (ValueError, TypeError):
        return ""
    if not period or len(period) < 3 or period[0] != "M":
        return f"{y:04d}-01-01"
    try:
        m = int(period[1:])
        if m < 1 or m > 12:
            return f"{y:04d}-01-01"
        return f"{y:04d}-{m:02d}-01"
    except ValueError:
        return f"{y:04d}-01-01"


def _batch_fetch(series_ids: list[str], start_year: int, end_year: int,
                  verbose: bool = False) -> dict:
    """
    POST a batched request to the BLS v2 endpoint. Returns the `Results`
    dict or an empty dict on error. The public endpoint accepts up to 25
    series per batch for unregistered requests and 50 for registered.
    """
    payload = {
        "seriesid": list(series_ids),
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    key = os.environ.get("BLS_API_KEY", "").strip()
    if key:
        payload["registrationkey"] = key
    try:
        r = httpx.post(BLS_ENDPOINT, json=payload, timeout=30.0)
    except Exception as e:
        if verbose:
            print(f"  BLS: {type(e).__name__}: {e}")
        return {}
    if r.status_code != 200:
        if verbose:
            print(f"  BLS: HTTP {r.status_code}")
        return {}
    try:
        doc = r.json()
    except Exception as e:
        if verbose:
            print(f"  BLS: JSON parse failed: {e}")
        return {}
    if doc.get("status") != "REQUEST_SUCCEEDED":
        if verbose:
            print(f"  BLS: status={doc.get('status')}, messages={doc.get('message')}")
        return {}
    return doc.get("Results", {})


def _compute_from_data(meta: tuple, observations: list[dict]) -> BLSSeries:
    """Derive a BLSSeries from a BLS data array (most recent first)."""
    sid, label, unit, relevance = meta
    bs = BLSSeries(series_id=sid, label=label, unit=unit,
                    relevance_note=relevance)
    if not observations:
        bs.error = "no observations"
        return bs

    # BLS returns most-recent first; filter to monthly (M01-M12), skip
    # annual averages (M13). Convert values to float.
    clean = []
    for o in observations:
        period = o.get("period", "")
        if period == "M13":
            continue
        raw = o.get("value", "").replace(",", "").strip()
        if not raw or raw in ("-", "."):
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        y = o.get("year", "")
        clean.append((y, period, val))
    if not clean:
        bs.error = "no usable observations"
        return bs

    # Most recent is first
    bs.n_observations = len(clean)
    y, p, v = clean[0]
    bs.latest_value = v
    bs.latest_period = f"{y}-{p}"
    bs.latest_date = _period_to_date(y, p)

    # Find observation ~12 months ago (same period, year-1)
    target_y = str(int(y) - 1) if y.isdigit() else y
    prior = None
    for yy, pp, vv in clean:
        if yy == target_y and pp == p:
            prior = vv
            break
    if prior is None:
        # Accept nearest earlier observation by period ordering
        for yy, pp, vv in clean[1:]:
            if yy == target_y:
                prior = vv
                break
    if prior is not None and prior != 0:
        bs.value_12m_ago = prior
        bs.change_12m_pct = (v - prior) / abs(prior) * 100
        abs_delta = abs(bs.change_12m_pct)
        if abs_delta < 1:
            bs.trend_direction = "flat"
        elif bs.change_12m_pct > 0:
            bs.trend_direction = "up"
        else:
            bs.trend_direction = "down"
    else:
        bs.trend_direction = "unknown"
    return bs


def fetch_bls_context(verbose: bool = False,
                       force_refresh: bool = False) -> BLSContext:
    """
    Fetch BLS series (cached daily). Graceful failure — returns an
    empty-series BLSContext if the API is unreachable.
    """
    if not force_refresh and _cache_is_fresh():
        cached = _load_cache()
        if cached and cached.series:
            if verbose:
                print(f"  BLS: cache hit ({len(cached.series)} series, "
                      f"fetched {cached.fetched_at})")
            return cached

    ctx = BLSContext(fetched_at=datetime.now().isoformat(timespec="seconds"))
    ids = [m[0] for m in BLS_SERIES]
    meta_by_id = {m[0]: m for m in BLS_SERIES}

    # Pull ~18 months of history in one batch
    end_year = datetime.now().year
    start_year = end_year - 2

    if verbose:
        print(f"  BLS: batch-fetching {len(ids)} series "
              f"({start_year}-{end_year})...")
    results = _batch_fetch(ids, start_year, end_year, verbose=verbose)
    for s in (results.get("series") or []):
        sid = s.get("seriesID", "")
        if sid not in meta_by_id:
            continue
        data = s.get("data", [])
        ctx.series[sid] = _compute_from_data(meta_by_id[sid], data)

    # Add placeholders for any series the API didn't return
    for sid, meta in meta_by_id.items():
        if sid not in ctx.series:
            ctx.series[sid] = BLSSeries(
                series_id=sid, label=meta[1], unit=meta[2],
                relevance_note=meta[3], error="series not returned by API",
            )

    try:
        _save_cache(ctx)
    except Exception as e:
        if verbose:
            print(f"  BLS: cache save failed: {e}")

    if verbose:
        n_ok = sum(1 for s in ctx.series.values() if not s.error)
        print(f"  BLS: {n_ok}/{len(ctx.series)} series loaded")

    return ctx


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch BLS macro context")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()
    ctx = fetch_bls_context(verbose=True, force_refresh=args.refresh)
    print()
    print(ctx.to_prompt_text())


if __name__ == "__main__":
    _main()
