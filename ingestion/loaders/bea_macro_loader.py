"""
BEA Macro Loader — Bureau of Economic Analysis.

Adds granular PCE (personal consumption expenditures) breakdowns that
FRED/BLS can't match at the category level. For consumer-oriented
theses, BEA PCE data answers questions like:
  • Is real PCE for food services GROWING or declining? (restaurant TAM)
  • Are consumers shifting from on-premises to off-premises meals?
  • How fast is recreation services spending growing? (leisure TAM)
  • Real disposable personal income — the denominator for everything

Requires a FREE API key from https://apps.bea.gov/API/signup/
Set as env var BEA_API_KEY. If missing, this loader returns an empty
context gracefully — the pipeline continues without BEA data.

Public API:
    fetch_bea_context(verbose=False, force_refresh=False) -> BEAContext

CLI:
    python -m ingestion.loaders.bea_macro_loader [--refresh]
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


BEA_ENDPOINT = "https://apps.bea.gov/api/data"


# --------------------------------------------------------------------------
# Curated series
# --------------------------------------------------------------------------

# BEA NIPA tables — each entry is (table_name, line_number, label,
# frequency, relevance_note). Frequency is "Q" (quarterly) or "M"
# (monthly). Table line numbers are stable across BEA vintages; verified
# via https://apps.bea.gov/iTable/
#
# Curated to match the equity-research use cases the pipeline needs:
# restaurant spend, recreation spend, income & savings.
BEA_SERIES = [
    # ── Consumer spending detail (monthly PCE, Table U20405 / T20405) ──
    # Note: BEA monthly tables use "T20405" for PCE by Major Type of Product.
    ("T20405", "2", "Personal Consumption Expenditures (PCE), total",
     "M", "Total consumer spending. Nominal $B, SAAR. "
          "Top-of-funnel for every consumer-exposed ticker."),
    ("T20405", "16", "PCE Food services and accommodations",
     "M", "Restaurant + hotel consumer spend. $B, SAAR. "
          "Direct TAM indicator for restaurant names."),
    ("T20405", "19", "PCE Recreation services",
     "M", "Leisure / entertainment spend. Signals discretionary health."),
    # Goods vs services split
    ("T20405", "6", "PCE Durable goods",
     "M", "Big-ticket discretionary. Turns with rate cycle and confidence."),
    ("T20405", "10", "PCE Nondurable goods (ex food)",
     "M", "Apparel, personal care — softer discretionary."),
    # ── Income & savings (monthly T20600) ──
    ("T20600", "28", "Real disposable personal income",
     "M", "The denominator behind consumer spending capacity."),
    ("T20600", "34", "Personal saving as a percentage of DPI",
     "M", "Savings-rate buffer. Falling = consumers deferring less, signals "
          "spend resilience. Rising = consumers tightening, spend headwind."),
]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class BEASeries:
    """One BEA data point with trend signal."""
    table_name: str
    line_number: str
    label: str
    frequency: str            # "M" or "Q"
    relevance_note: str
    latest_value: float | None = None
    latest_period: str = ""   # "2026M03" or "2026Q1"
    latest_date: str = ""     # YYYY-MM-DD
    value_12m_ago: float | None = None
    change_12m_pct: float | None = None
    trend_direction: str = ""
    n_observations: int = 0
    error: str = ""

    def summary_line(self) -> str:
        if self.error or self.latest_value is None:
            return f"{self.label}: unavailable ({self.error or 'no data'})"
        # BEA units vary; format a readable default
        if self.latest_value >= 1000:
            val = f"{self.latest_value:,.0f}"
        else:
            val = f"{self.latest_value:.2f}"
        parts = [f"{self.label}: {val}"]
        parts.append(f" (as of {self.latest_date or self.latest_period})")
        if self.change_12m_pct is not None:
            arrow = "↑" if self.change_12m_pct > 1 else "↓" if self.change_12m_pct < -1 else "≈"
            parts.append(f", {arrow} {self.change_12m_pct:+.1f}% YoY")
        return "".join(parts)


@dataclass
class BEAContext:
    fetched_at: str = ""
    series: dict = field(default_factory=dict)   # {"table_line": BEASeries}
    no_key: bool = False

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "no_key": self.no_key,
            "series": {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
                       for k, v in self.series.items()},
        }

    def to_prompt_text(self) -> str:
        if self.no_key:
            return ""  # Silent skip when no key configured
        if not self.series:
            return ""
        lines = [
            "=== BEA CONTEXT (PCE / income detail — cite specific figures when used) ===",
        ]
        for key, s in self.series.items():
            if isinstance(s, BEASeries):
                line = s.summary_line()
                note = s.relevance_note
            else:
                bs = BEASeries(**s)
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
_CACHE_FILE = _CACHE_DIR / "bea_macro_daily.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age = time.time() - _CACHE_FILE.stat().st_mtime
    return age < _CACHE_TTL_SECONDS


def _load_cache() -> BEAContext | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        d = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        ctx = BEAContext(fetched_at=d.get("fetched_at", ""),
                          no_key=d.get("no_key", False))
        known = {f for f in BEASeries.__dataclass_fields__}
        for key, sdata in (d.get("series") or {}).items():
            ctx.series[key] = BEASeries(
                **{k: v for k, v in sdata.items() if k in known}
            )
        return ctx
    except Exception:
        return None


def _save_cache(ctx: BEAContext) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(
        json.dumps(ctx.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# BEA API
# --------------------------------------------------------------------------

def _bea_period_to_date(period: str) -> str:
    """
    BEA periods: '2026M03' -> '2026-03-01'; '2026Q1' -> '2026-01-01'.
    """
    if not period:
        return ""
    if "M" in period:
        try:
            y, m = period.split("M")
            return f"{int(y):04d}-{int(m):02d}-01"
        except Exception:
            return ""
    if "Q" in period:
        try:
            y, q = period.split("Q")
            month = {"1": 1, "2": 4, "3": 7, "4": 10}.get(q, 1)
            return f"{int(y):04d}-{month:02d}-01"
        except Exception:
            return ""
    return ""


def _fetch_table(table_name: str, frequency: str,
                  start_year: int, end_year: int,
                  api_key: str, verbose: bool = False) -> dict:
    """
    Fetch a full NIPA table for a date range. Returns the Data list
    (filtered rows) or empty list on error.

    We always pull the whole table then filter by line number client-side.
    That's one API call per table instead of one per line, and the BEA
    endpoint handles the filter efficiently anyway.
    """
    # Build year range
    years = ",".join(str(y) for y in range(start_year, end_year + 1))
    params = {
        "UserID": api_key,
        "method": "GetData",
        "datasetname": "NIPA",
        "TableName": table_name,
        "Frequency": frequency,
        "Year": years,
        "ResultFormat": "JSON",
    }
    try:
        r = httpx.get(BEA_ENDPOINT, params=params, timeout=45.0)
    except Exception as e:
        if verbose:
            print(f"  BEA {table_name}: {type(e).__name__}: {e}")
        return {}
    if r.status_code != 200:
        if verbose:
            print(f"  BEA {table_name}: HTTP {r.status_code}")
        return {}
    try:
        doc = r.json()
    except Exception as e:
        if verbose:
            print(f"  BEA {table_name}: JSON parse failed: {e}")
        return {}
    results = doc.get("BEAAPI", {}).get("Results", {})
    # BEA returns errors inside Results
    if results.get("Error"):
        if verbose:
            err = results["Error"]
            print(f"  BEA {table_name}: API error "
                  f"{err.get('APIErrorCode')}: "
                  f"{err.get('APIErrorDescription','?')[:100]}")
        return {}
    return results


def _row_to_series(meta: tuple, rows: list[dict]) -> BEASeries:
    """Build a BEASeries from BEA Data rows for one line."""
    table, line, label, freq, relevance = meta
    bs = BEASeries(
        table_name=table, line_number=line, label=label,
        frequency=freq, relevance_note=relevance,
    )
    if not rows:
        bs.error = "no rows returned for this line"
        return bs

    clean = []
    for r in rows:
        period = r.get("TimePeriod", "")
        raw = (r.get("DataValue") or "").replace(",", "").strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        clean.append((period, val))
    if not clean:
        bs.error = "no parseable values"
        return bs

    # Sort by period (lexicographic works for "2026M03" / "2026Q1")
    clean.sort(key=lambda x: x[0])
    bs.n_observations = len(clean)
    latest_period, latest_val = clean[-1]
    bs.latest_value = latest_val
    bs.latest_period = latest_period
    bs.latest_date = _bea_period_to_date(latest_period)

    # YoY: find the observation 12 months / 4 quarters ago
    if freq == "M":
        # Match "2026M03" -> "2025M03"
        if "M" in latest_period:
            y, m = latest_period.split("M")
            try:
                target = f"{int(y)-1:04d}M{m}"
            except ValueError:
                target = None
            for p, v in clean:
                if p == target:
                    bs.value_12m_ago = v
                    break
    else:  # Q
        if "Q" in latest_period:
            y, q = latest_period.split("Q")
            try:
                target = f"{int(y)-1:04d}Q{q}"
            except ValueError:
                target = None
            for p, v in clean:
                if p == target:
                    bs.value_12m_ago = v
                    break

    if bs.value_12m_ago and bs.value_12m_ago != 0:
        bs.change_12m_pct = (latest_val - bs.value_12m_ago) / abs(bs.value_12m_ago) * 100
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


def fetch_bea_context(verbose: bool = False,
                       force_refresh: bool = False) -> BEAContext:
    """
    Fetch the curated BEA series bundle. Requires BEA_API_KEY env var;
    returns an empty no_key=True context if absent (pipeline keeps
    running).
    """
    api_key = os.environ.get("BEA_API_KEY", "").strip()
    if not api_key:
        if verbose:
            print("  BEA: no BEA_API_KEY env var set — skipping "
                  "(register a free key at https://apps.bea.gov/API/signup/)")
        return BEAContext(
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            no_key=True,
        )

    if not force_refresh and _cache_is_fresh():
        cached = _load_cache()
        if cached and cached.series:
            if verbose:
                print(f"  BEA: cache hit ({len(cached.series)} series)")
            return cached

    ctx = BEAContext(fetched_at=datetime.now().isoformat(timespec="seconds"))

    # Group series by (table_name, frequency) to minimize API calls
    by_table: dict[tuple, list[tuple]] = {}
    for meta in BEA_SERIES:
        key = (meta[0], meta[3])
        by_table.setdefault(key, []).append(meta)

    end_year = datetime.now().year
    start_year = end_year - 2

    for (table, freq), metas in by_table.items():
        if verbose:
            print(f"  BEA: fetching {table} ({freq}, {len(metas)} line(s))...")
        results = _fetch_table(table, freq, start_year, end_year,
                                api_key, verbose=verbose)
        data_rows = results.get("Data", [])
        # Index by line number
        by_line: dict[str, list[dict]] = {}
        for r in data_rows:
            lnum = str(r.get("LineNumber", "")).strip()
            by_line.setdefault(lnum, []).append(r)
        for meta in metas:
            line = meta[1]
            rows = by_line.get(line, [])
            bs = _row_to_series(meta, rows)
            ctx.series[f"{table}-L{line}"] = bs
        time.sleep(0.5)  # polite

    try:
        _save_cache(ctx)
    except Exception as e:
        if verbose:
            print(f"  BEA: cache save failed: {e}")

    if verbose:
        n_ok = sum(1 for s in ctx.series.values() if not s.error)
        print(f"  BEA: {n_ok}/{len(ctx.series)} series loaded")

    return ctx


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch BEA macro context")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()
    ctx = fetch_bea_context(verbose=True, force_refresh=args.refresh)
    print()
    if ctx.no_key:
        print("(BEA context skipped — register a free key at "
              "https://apps.bea.gov/API/signup/ and set BEA_API_KEY)")
    else:
        print(ctx.to_prompt_text())


if __name__ == "__main__":
    _main()
