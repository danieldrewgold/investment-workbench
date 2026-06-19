"""
Macro dashboard data.

Pulls full observation history for the key macro series (keyless FRED CSV,
reusing fred_macro_loader._fetch_series_csv) so the dashboard can chart them:
unemployment, CPI / PPI (as YoY%), personal saving rate + level, consumer
credit outstanding, and the fed funds rate. Plus best-effort prediction-market
rate-cut odds (Kalshi / Polymarket public APIs). Cached daily.

  fetch_macro_series(force=False) -> {sid: {label, unit, obs:[(date,val)], latest, prior, chg}}
  fetch_rate_odds()               -> [{source, question, prob, url}]  (best-effort)
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from ingestion.loaders.fred_macro_loader import _fetch_series_csv

# (series_id, label, unit, transform)  transform: "level" raw | "yoy" -> YoY %
CHART_SERIES = [
    ("UNRATE",    "Unemployment rate",          "%",   "level"),
    ("CPIAUCSL",  "CPI inflation (YoY)",         "%",   "yoy"),
    ("CPILFESL",  "Core CPI inflation (YoY)",    "%",   "yoy"),
    ("PPIACO",    "PPI all commodities (YoY)",   "%",   "yoy"),
    ("PSAVERT",   "Personal saving rate",        "%",   "level"),
    ("PMSAVE",    "Personal saving ($B)",        "$B",  "level"),
    ("TOTALSL",   "Consumer credit outstanding ($B)", "$B", "level"),
    ("FEDFUNDS",  "Fed funds rate (effective)",  "%",   "level"),
]

_CACHE = Path("data/macro_cache/macro_charts.json")
_TTL = 24 * 60 * 60


def _to_yoy(obs: list) -> list:
    """Convert a monthly level series to YoY % change."""
    out = []
    by_date = {d: v for d, v in obs}
    dates = [d for d, _ in obs]
    for i, (d, v) in enumerate(obs):
        if i >= 12 and v is not None:
            prior = obs[i - 12][1]
            if prior:
                out.append((d, round((v / prior - 1) * 100, 2)))
    return out


def fetch_macro_series(*, lookback_days: int = 1150, force: bool = False) -> dict:
    if not force and _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _TTL:
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    out: dict = {}
    for sid, label, unit, transform in CHART_SERIES:
        try:
            obs = _fetch_series_csv(sid, lookback_days=lookback_days) or []
        except Exception:
            obs = []
        obs = [(str(d), v) for d, v in obs if v is not None]
        if transform == "yoy":
            obs = _to_yoy(obs)
        latest = obs[-1] if obs else None
        prior = obs[-13] if len(obs) > 13 else (obs[0] if obs else None)
        out[sid] = {
            "label": label, "unit": unit,
            "obs": obs[-60:],  # ~5y monthly for the chart
            "latest": latest, "prior": prior,
            "chg": (round(latest[1] - prior[1], 2) if latest and prior else None),
        }
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


_RATE_KW = ("fed ", "fomc", "rate cut", "rate hike", "interest rate", "fed funds",
            "basis point", "powell", "recession")


def fetch_rate_odds(*, force: bool = False) -> list:
    """Best-effort prediction-market odds on the Fed / macro. Returns [] if the
    public endpoints are unreachable (no auth, so this is fragile)."""
    import httpx
    out = []
    try:
        r = httpx.get("https://gamma-api.polymarket.com/markets",
                      params={"closed": "false", "limit": 150, "order": "volume",
                              "ascending": "false"}, timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            for m in rows:
                qn = (m.get("question") or "")
                if not any(k in qn.lower() for k in _RATE_KW):
                    continue
                p = None
                op = m.get("outcomePrices")
                if op:
                    try:
                        arr = json.loads(op) if isinstance(op, str) else op
                        p = round(float(arr[0]) * 100)
                    except Exception:
                        p = None
                out.append({"source": "Polymarket", "question": qn, "prob": p,
                            "url": "https://polymarket.com"})
                if len(out) >= 8:
                    break
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    m = fetch_macro_series(force="--force" in sys.argv)
    for sid, d in m.items():
        lat = d["latest"]
        print(f"  {sid:10} {d['label']:34} latest={lat[1] if lat else '—'} {d['unit']} "
              f"({lat[0] if lat else '?'})  chg12m={d['chg']}  pts={len(d['obs'])}")
    print("\nRate odds:")
    for o in fetch_rate_odds():
        print(f"  [{o['source']}] {o['question'][:60]} — {o['prob']}%")
