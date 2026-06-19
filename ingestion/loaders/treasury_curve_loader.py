"""
Treasury Yield Curve Loader

Fetches the U.S. Treasury constant-maturity yield curve from FRED's
public CSV endpoint (no API key required) and provides interpolated
yields at arbitrary maturities. Used by the bond spread monitor to
benchmark corporate bond yields against the risk-free curve.

FRED series we pull:
    DGS1MO   - 1-month       (~0.083 years)
    DGS3MO   - 3-month       (~0.25 years)
    DGS6MO   - 6-month       (~0.5 years)
    DGS1     - 1-year
    DGS2     - 2-year
    DGS3     - 3-year
    DGS5     - 5-year
    DGS7     - 7-year
    DGS10    - 10-year
    DGS20    - 20-year
    DGS30    - 30-year

Cache: daily flat file; the curve barely moves in a day and a 24h cache
is what every practitioner spreadsheet does anyway.

Public API:
    fetch_treasury_curve(*, verbose=False, force_refresh=False) -> TreasuryCurve

The returned `TreasuryCurve` exposes `interpolate(years_to_maturity)` for
linear interpolation between the constant-maturity points. Linear is the
practitioner-standard simplification for spread reporting; production
quant shops use Nelson-Siegel or cubic-spline, but for the "is this bond
widening?" signal the difference is well below 1 bp.
"""

from __future__ import annotations

import csv
import io
import json
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


# Tenor in years (decimal) keyed by FRED series id
_TREASURY_SERIES: dict[str, float] = {
    "DGS1MO": 1 / 12,
    "DGS3MO": 0.25,
    "DGS6MO": 0.5,
    "DGS1":   1.0,
    "DGS2":   2.0,
    "DGS3":   3.0,
    "DGS5":   5.0,
    "DGS7":   7.0,
    "DGS10":  10.0,
    "DGS20":  20.0,
    "DGS30":  30.0,
}

# FRED public CSV endpoint (no key)
_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_CACHE_DIR = Path("data/treasury_curve_cache")
_CACHE_TTL_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------------
# Data type
# --------------------------------------------------------------------------

@dataclass
class TreasuryCurve:
    fetched_at: str = ""
    as_of_date: str = ""                       # latest observation date
    points: list = field(default_factory=list) # [(years, yield_pct), ...] sorted by years

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "as_of_date": self.as_of_date,
            "points": list(self.points),
        }

    def interpolate(self, years_to_maturity: float) -> float | None:
        """Linear interpolation of the par-yield curve. Returns None if
        the curve is empty. Clamps at the curve endpoints."""
        if not self.points:
            return None
        pts = self.points
        if years_to_maturity <= pts[0][0]:
            return pts[0][1]
        if years_to_maturity >= pts[-1][0]:
            return pts[-1][1]
        # Find bracket
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= years_to_maturity <= x1:
                frac = (years_to_maturity - x0) / (x1 - x0)
                return y0 + frac * (y1 - y0)
        return None

    def to_prompt_text(self) -> str:
        if not self.points:
            return ""
        lines = [
            f"=== U.S. TREASURY YIELD CURVE (as of {self.as_of_date}) ===",
            "(Constant-maturity par yields from FRED. Used as the risk-free "
            "benchmark for corporate spread calculation.)",
            "",
        ]
        # Compact one-line summary
        bits = []
        for years, yld in self.points:
            label = (
                f"{int(years*12)}M" if years < 1 else
                f"{int(years)}Y"
            )
            bits.append(f"{label}: {yld:.2f}%")
        lines.append("  " + " | ".join(bits))
        lines.append("")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _cache_path() -> Path:
    return _CACHE_DIR / "treasury_curve.json"


def _load_cache() -> TreasuryCurve | None:
    p = _cache_path()
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return TreasuryCurve(
            fetched_at=d.get("fetched_at", ""),
            as_of_date=d.get("as_of_date", ""),
            points=[tuple(p) for p in d.get("points", [])],
        )
    except Exception:
        return None


def _save_cache(curve: TreasuryCurve) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text(
        json.dumps(curve.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# FRED fetch
# --------------------------------------------------------------------------

def _fetch_fred_series(series_id: str, *, timeout: float = 30.0) -> tuple[str, float] | None:
    """Pull the latest observation for a single FRED series via the public
    CSV endpoint. Returns (date, value) for the most-recent non-null row."""
    try:
        r = httpx.get(_FRED_CSV_URL, params={"id": series_id}, timeout=timeout)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    if not rows:
        return None
    # Find the most-recent row whose value is not "." or empty
    for row in reversed(rows[1:]):  # skip header
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if not v or v == ".":
            continue
        try:
            return d, float(v)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def fetch_treasury_curve(
    *,
    verbose: bool = False,
    force_refresh: bool = False,
) -> TreasuryCurve:
    if not force_refresh:
        cached = _load_cache()
        if cached and cached.points:
            if verbose:
                print(f"  Treasury curve: cache hit ({len(cached.points)} points, "
                      f"as of {cached.as_of_date})")
            return cached

    points: list[tuple[float, float]] = []
    latest_date = ""
    for series_id, years in _TREASURY_SERIES.items():
        result = _fetch_fred_series(series_id)
        if result is None:
            if verbose:
                print(f"  Treasury curve: {series_id} fetch failed")
            continue
        d, v = result
        points.append((years, v))
        if d > latest_date:
            latest_date = d

    if not points:
        if verbose:
            print(f"  Treasury curve: no points fetched")
        return TreasuryCurve(
            fetched_at=datetime.now().isoformat(timespec="seconds"),
        )

    points.sort(key=lambda p: p[0])
    curve = TreasuryCurve(
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        as_of_date=latest_date,
        points=points,
    )

    try:
        _save_cache(curve)
    except Exception:
        pass

    if verbose:
        print(f"  Treasury curve: {len(points)} points loaded, "
              f"as of {latest_date}")

    return curve


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch the U.S. Treasury yield curve from FRED")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--interpolate", type=float, default=None,
                    help="Print interpolated yield at this tenor (years)")
    args = p.parse_args()

    curve = fetch_treasury_curve(verbose=True, force_refresh=args.refresh)
    print()
    print(curve.to_prompt_text() or "(no curve data)")
    if args.interpolate is not None:
        y = curve.interpolate(args.interpolate)
        if y is not None:
            print(f"Interpolated {args.interpolate:.2f}-year yield: {y:.3f}%")


if __name__ == "__main__":
    _main()
