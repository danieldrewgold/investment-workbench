"""
FRED Macro Context Loader.

Fetches a small curated set of macro indicators from the St. Louis Fed's
public CSV endpoint (no API key required) to give the research brief real
macro grounding. Before this loader existed the pipeline would make
claims like "middle-income consumer pressure persists" with zero data
behind it — this gives Claude an actual savings rate trend / delinquency
rate / sentiment reading to cite.

Public API:
    fetch_macro_context(verbose=False, force_refresh=False)
        -> MacroContext

The returned object carries per-series `latest_value`, `latest_date`,
`value_12m_ago`, `trend_direction`, and a `to_prompt_text()` method that
renders a compact paragraph suitable for brief-prompt injection.

CLI:
    python -m ingestion.loaders.fred_macro_loader [--refresh]

Design choices:
    * No API key — we use https://fred.stlouisfed.org/graph/fredgraph.csv
      which is public and stable. If this endpoint changes or rate-limits
      us we'll swap to pandas_datareader or a proper FRED API key.
    * Single flat cache file (daily) — the macro picture doesn't need a
      per-ticker refresh; one fetch per day is enough.
    * Curated series list — we only pull indicators that correspond to
      real bear-case narratives used in equity research (saving rate for
      discretionary consumer, credit-card delinquency for lower-income,
      sentiment for reopening/turnaround claims, core CPI for cost
      pressure claims).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path

import httpx


# Force UTF-8 for Windows terminals
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------
# Curated indicator list
# --------------------------------------------------------------------------

# Each entry: (series_id, label, unit, relevance_note)
# relevance_note tells Claude WHY this series matters — so when it sees
# "saving rate: 4.2%, trend: declining", it knows this supports claims
# about consumer discretionary pressure rather than just seeing a number.
MACRO_SERIES = [
    ("PSAVERT",     "Personal saving rate",
     "pct", "Lower saving rate = consumers spending more of income; rising = tightening."),
    ("UMCSENT",     "Consumer sentiment (U. of Michigan)",
     "index", "Forward-looking confidence; leads discretionary spend by ~1 quarter."),
    ("DRCCLACBS",   "Credit card delinquency rate",
     "pct", "Lower-income stress indicator; rising = discretionary spend at risk."),
    ("CPILFESL",    "Core CPI (ex food & energy)",
     "level", "Cost pressure; compute YoY change from the level."),
    ("UNRATE",      "Unemployment rate",
     "pct", "Labor market tightness; higher = weaker wage growth AND weaker consumer."),
    ("AHETPI",      "Average hourly earnings (production & non-supervisory)",
     "dollars", "Real wage proxy; compute YoY change from the level."),
    ("CPIAUCSL",    "Headline CPI",
     "level", "Top-line inflation; compute YoY from the level. Drives Fed path + real-income."),
    ("PPIACO",      "PPI (all commodities)",
     "level", "Producer/input cost pressure; leads goods-margin compression. YoY from level."),
    ("FEDFUNDS",    "Fed funds rate (effective)",
     "pct", "Policy rate / cost of capital; falling = easing tailwind for multiples + rate-sensitive demand."),
    ("TOTALSL",     "Consumer credit outstanding",
     "level", "Consumer leverage; rising fast = pulled-forward demand / late-cycle stress. YoY from level."),
    ("PMSAVE",      "Personal saving (level)",
     "level", "Dry powder for spending; falling = consumer drawing down buffers."),
]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class MacroSeries:
    """One FRED series observation with trend signal."""
    series_id: str
    label: str
    unit: str                         # "pct", "index", "level", "dollars"
    relevance_note: str
    latest_value: float | None = None
    latest_date: str = ""              # YYYY-MM-DD
    value_12m_ago: float | None = None
    change_12m: float | None = None    # absolute delta (or YoY pct if unit=level)
    change_12m_pct: float | None = None  # percent change YoY
    trend_direction: str = ""          # "up" / "down" / "flat" / "unknown"
    n_observations: int = 0
    error: str = ""

    def summary_line(self) -> str:
        """One-line rendering for prompt injection."""
        if self.error or self.latest_value is None:
            return f"{self.label}: unavailable ({self.error or 'no data'})"
        parts = [f"{self.label}: "]
        # Format latest value by unit
        if self.unit == "pct":
            parts.append(f"{self.latest_value:.2f}%")
        elif self.unit == "dollars":
            parts.append(f"${self.latest_value:.2f}")
        elif self.unit == "index":
            parts.append(f"{self.latest_value:.1f}")
        else:
            parts.append(f"{self.latest_value:.2f}")
        parts.append(f" (as of {self.latest_date})")
        # Add 12m change
        if self.change_12m_pct is not None:
            arrow = "↑" if self.change_12m_pct > 1 else "↓" if self.change_12m_pct < -1 else "≈"
            parts.append(f", {arrow} {self.change_12m_pct:+.1f}% YoY")
        elif self.change_12m is not None:
            arrow = "↑" if self.change_12m > 0.1 else "↓" if self.change_12m < -0.1 else "≈"
            parts.append(f", {arrow} {self.change_12m:+.2f} YoY")
        return "".join(parts)


@dataclass
class MacroContext:
    """Bundle of macro series for one fetch."""
    fetched_at: str = ""
    series: dict = field(default_factory=dict)   # {series_id: MacroSeries (as dict)}

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "series": {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
                       for k, v in self.series.items()},
        }

    def to_prompt_text(self) -> str:
        """
        Render a compact macro-context block suitable for injection into the
        brief prompt. Each series gets one line + its relevance note; the
        block is bracketed with explicit header/footer so Claude can cite
        specific figures without confusion.
        """
        if not self.series:
            return ""
        lines = ["=== MACRO CONTEXT (FRED, real data — cite explicitly when used) ==="]
        for sid, s in self.series.items():
            # Handle dict or dataclass shape
            if isinstance(s, MacroSeries):
                line = s.summary_line()
                note = s.relevance_note
            else:
                # Rebuild line from dict — cheap path
                ms = MacroSeries(**s)
                line = ms.summary_line()
                note = ms.relevance_note
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
_CACHE_FILE = _CACHE_DIR / "fred_macro_daily.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60   # refresh once a day


def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age = time.time() - _CACHE_FILE.stat().st_mtime
    return age < _CACHE_TTL_SECONDS


def _load_cache() -> MacroContext | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        d = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        ctx = MacroContext(fetched_at=d.get("fetched_at", ""))
        for sid, sdata in (d.get("series") or {}).items():
            try:
                ctx.series[sid] = MacroSeries(**sdata)
            except TypeError:
                # Fresh fields added; ignore unknown keys
                known = {f for f in MacroSeries.__dataclass_fields__}
                ctx.series[sid] = MacroSeries(
                    **{k: v for k, v in sdata.items() if k in known}
                )
        return ctx
    except Exception:
        return None


def _save_cache(ctx: MacroContext) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(ctx.to_dict(), default=str, indent=2),
                            encoding="utf-8")


# --------------------------------------------------------------------------
# FRED CSV fetch
# --------------------------------------------------------------------------

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _fetch_series_csv(series_id: str, lookback_days: int = 540,
                       verbose: bool = False) -> list[tuple[date, float]]:
    """
    Fetch one FRED series as a list of (date, value) observations,
    ascending by date. Returns empty list on failure.

    lookback_days defaults to 540 (~18 months) so we always have enough
    runway to compute a 12-month YoY change even with monthly series.
    """
    cosd = (datetime.now().date()
            .replace(day=1)).toordinal() - lookback_days
    from datetime import date as _date
    cosd_date = _date.fromordinal(max(cosd, 1))
    params = {"id": series_id, "cosd": cosd_date.isoformat()}
    try:
        r = httpx.get(FRED_CSV_URL, params=params,
                      timeout=30.0, follow_redirects=True)
        if r.status_code != 200:
            if verbose:
                print(f"    FRED {series_id}: HTTP {r.status_code}")
            return []
    except Exception as e:
        if verbose:
            print(f"    FRED {series_id}: {type(e).__name__}: {e}")
        return []

    obs: list[tuple[date, float]] = []
    reader = csv.reader(io.StringIO(r.text))
    header = next(reader, None)
    # Expected: ["observation_date", "<SERIES_ID>"] or ["DATE", ...]
    for row in reader:
        if len(row) < 2:
            continue
        try:
            dt = datetime.strptime(row[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        raw = row[1].strip()
        if not raw or raw == ".":
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        obs.append((dt, val))
    obs.sort(key=lambda x: x[0])
    return obs


def _compute_series(meta: tuple, verbose: bool = False) -> MacroSeries:
    """Fetch + derive one MacroSeries object."""
    sid, label, unit, relevance = meta
    ms = MacroSeries(series_id=sid, label=label, unit=unit,
                     relevance_note=relevance)
    obs = _fetch_series_csv(sid, verbose=verbose)
    if not obs:
        ms.error = "no observations returned"
        return ms

    ms.n_observations = len(obs)
    latest_dt, latest_val = obs[-1]
    ms.latest_value = latest_val
    ms.latest_date = latest_dt.isoformat()

    # Find observation closest to 12 months before latest_dt
    target_dt = date(latest_dt.year - 1, latest_dt.month, min(latest_dt.day, 28))
    best = None
    best_gap = None
    for dt, val in obs:
        gap = abs((dt - target_dt).days)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = (dt, val)
    # Require we land within 60 days of the target
    if best and best_gap is not None and best_gap <= 60:
        _, prior_val = best
        ms.value_12m_ago = prior_val
        ms.change_12m = latest_val - prior_val
        if prior_val != 0:
            ms.change_12m_pct = (latest_val - prior_val) / abs(prior_val) * 100
        # Direction
        abs_delta = abs(ms.change_12m_pct or 0)
        if abs_delta < 1:
            ms.trend_direction = "flat"
        elif (ms.change_12m_pct or 0) > 0:
            ms.trend_direction = "up"
        else:
            ms.trend_direction = "down"
    else:
        ms.trend_direction = "unknown"

    return ms


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def fetch_macro_context(verbose: bool = False,
                         force_refresh: bool = False) -> MacroContext:
    """
    Return a MacroContext with the curated FRED series. Cached daily —
    the macro picture doesn't shift meaningfully within 24 hours.
    """
    if not force_refresh and _cache_is_fresh():
        cached = _load_cache()
        if cached and cached.series:
            if verbose:
                print(f"  FRED macro: cache hit ({len(cached.series)} series, "
                      f"fetched {cached.fetched_at})")
            return cached

    ctx = MacroContext(fetched_at=datetime.now().isoformat(timespec="seconds"))
    for meta in MACRO_SERIES:
        sid = meta[0]
        if verbose:
            print(f"  FRED macro: fetching {sid}...")
        ms = _compute_series(meta, verbose=verbose)
        ctx.series[sid] = ms
        # Be polite to the endpoint
        time.sleep(0.3)

    try:
        _save_cache(ctx)
    except Exception as e:
        if verbose:
            print(f"  FRED macro: cache write failed: {e}")

    if verbose:
        n_ok = sum(1 for s in ctx.series.values() if not s.error)
        print(f"  FRED macro: {n_ok}/{len(ctx.series)} series loaded")

    return ctx


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch FRED macro context")
    p.add_argument("--refresh", action="store_true", help="Bypass daily cache")
    args = p.parse_args()

    ctx = fetch_macro_context(verbose=True, force_refresh=args.refresh)
    print()
    print(ctx.to_prompt_text())


if __name__ == "__main__":
    _main()
