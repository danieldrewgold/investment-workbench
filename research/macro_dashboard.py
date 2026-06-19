"""
Macro dashboard data — robust macro overview for investment decisions.

Pulls full observation history for a broad set of macro series (keyless FRED
CSV) grouped by theme (Growth / Labor / Inflation / Rates & credit / Consumer),
computes DATE-AWARE 3mo/12mo/5y changes (works across daily/weekly/monthly
frequencies), downsamples for charting, fetches prediction-market rate odds,
and synthesizes an AI macro digest (regime + where things are heading +
investment implications). Cached daily.

  fetch_macro_series(force=False) -> {sid: {label, unit, category, obs, latest, changes}}
  fetch_rate_odds()               -> [{source, question, prob, url}]
  synthesize_macro_digest(...)    -> {regime, consumer, inflation, labor, growth,
                                      rates_credit, whats_heading, investment_implications}
"""

from __future__ import annotations

import json
import time
from datetime import date as _date, timedelta
from pathlib import Path

from ingestion.loaders.fred_macro_loader import _fetch_series_csv

# (series_id, label, unit, transform, category)
CHART_SERIES = [
    ("A191RL1Q225SBEA", "Real GDP (QoQ annualized)",  "%",   "level", "Growth"),
    ("RSAFS",      "Retail sales (YoY)",          "%",   "yoy",   "Growth"),
    ("INDPRO",     "Industrial production (YoY)",  "%",   "yoy",   "Growth"),
    ("UNRATE",     "Unemployment rate",           "%",   "level", "Labor"),
    ("ICSA",       "Initial jobless claims",      "k",   "level", "Labor"),
    ("PAYEMS",     "Nonfarm payrolls (YoY)",      "%",   "yoy",   "Labor"),
    ("CPIAUCSL",   "CPI inflation (YoY)",          "%",   "yoy",   "Inflation"),
    ("CPILFESL",   "Core CPI inflation (YoY)",     "%",   "yoy",   "Inflation"),
    ("PPIACO",     "PPI all commodities (YoY)",    "%",   "yoy",   "Inflation"),
    ("FEDFUNDS",   "Fed funds rate",              "%",   "level", "Rates & credit"),
    ("DGS10",      "10-year Treasury yield",       "%",   "level", "Rates & credit"),
    ("T10Y2Y",     "Yield curve (10Y minus 2Y)",   "%",   "level", "Rates & credit"),
    ("BAMLH0A0HYM2", "High-yield credit spread (OAS)", "%", "level", "Rates & credit"),
    ("PSAVERT",    "Personal saving rate",         "%",   "level", "Consumer"),
    ("TOTALSL",    "Consumer credit outstanding",  "$M",  "level", "Consumer"),
    ("UMCSENT",    "Consumer sentiment (UMich)",   "idx", "level", "Consumer"),
]

CATEGORIES = ["Growth", "Labor", "Inflation", "Rates & credit", "Consumer"]

_CACHE = Path("data/macro_cache/macro_charts.json")
_DIGEST_CACHE = Path("data/macro_cache/macro_digest.json")
_TTL = 24 * 60 * 60


def _to_yoy(obs: list) -> list:
    out = []
    for i, (d, v) in enumerate(obs):
        if i >= 12 and v is not None and obs[i - 12][1]:
            out.append((d, round((v / obs[i - 12][1] - 1) * 100, 2)))
    return out


def _downsample(obs: list, target: int = 96) -> list:
    if len(obs) <= target:
        return obs
    step = len(obs) / target
    idx = sorted(set(int(i * step) for i in range(target)) | {len(obs) - 1})
    return [obs[i] for i in idx]


def _val_on_or_before(obs: list, target_iso: str):
    best = None
    for d, v in obs:
        if d <= target_iso:
            best = (d, v)
        else:
            break
    return best


def _changes(obs: list) -> dict:
    """Date-aware deltas over 3mo / 12mo / 5y (latest minus value back then)."""
    if not obs:
        return {}
    last_d, last_v = obs[-1]
    try:
        ld = _date.fromisoformat(last_d)
    except Exception:
        return {}
    out = {}
    for label, days in (("3mo", 91), ("12mo", 365), ("5y", 365 * 5)):
        nb = _val_on_or_before(obs, (ld - timedelta(days=days)).isoformat())
        if nb and nb[1] is not None:
            out[label] = round(last_v - nb[1], 2)
    return out


def fetch_macro_series(*, lookback_days: int = 2000, force: bool = False) -> dict:
    if not force and _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _TTL:
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    out: dict = {}
    for sid, label, unit, transform, cat in CHART_SERIES:
        try:
            obs = _fetch_series_csv(sid, lookback_days=lookback_days) or []
        except Exception:
            obs = []
        obs = [(str(d), v) for d, v in obs if v is not None]
        if transform == "yoy":
            obs = _to_yoy(obs)
        out[sid] = {
            "label": label, "unit": unit, "category": cat,
            "obs": _downsample(obs),
            "latest": obs[-1] if obs else None,
            "changes": _changes(obs),
        }
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


_RATE_KW = ("fed ", "fomc", "rate cut", "rate hike", "interest rate", "fed funds",
            "basis point", "powell", "recession")


def fetch_rate_odds(*, force: bool = False) -> list:
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


_DIGEST_SYSTEM = """You are a macro strategist writing a concise desk digest for an \
equity investor. You are given the latest US macro dashboard (levels + 3mo/12mo/5y \
changes) and prediction-market rate odds. Return ONLY JSON.

Read the regime from the DATA — do not invent numbers; cite the figures you were \
given. Be decisive and specific. Keys:
- regime: 1-2 sentences naming the macro regime (cycle stage, inflation/labor/rates \
direction) — the headline read.
- consumer: consumer health (saving, credit, sentiment, real income) — where it sits + trend.
- inflation: trajectory (headline vs core vs PPI) and what it implies for the Fed.
- labor: labor-market read (unemployment, claims, payrolls).
- growth: growth read (GDP, retail sales, industrial production).
- rates_credit: rates / curve / credit spreads — easing or tightening, risk appetite.
- whats_heading: the forward read — where the data is pointing over the next 1-2 quarters.
- investment_implications: 2-4 crisp, actionable bullets for positioning (sectors, \
duration, risk-on/off, what to watch). This is the payoff — make it useful."""


def synthesize_macro_digest(series: dict | None = None, odds: list | None = None,
                            *, force: bool = False, verbose: bool = False) -> dict:
    if not force and _DIGEST_CACHE.exists() and (time.time() - _DIGEST_CACHE.stat().st_mtime) < _TTL:
        try:
            return json.loads(_DIGEST_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    series = series if series is not None else fetch_macro_series()
    odds = odds if odds is not None else fetch_rate_odds()
    lines = []
    for sid, d in series.items():
        lat = d.get("latest")
        if not lat:
            continue
        ch = d.get("changes") or {}
        chs = ", ".join(f"{k} {v:+}" for k, v in ch.items())
        lines.append(f"[{d['category']}] {d['label']}: {lat[1]} {d['unit']} (as of {lat[0]}); Δ {chs}")
    odd_lines = [f"{o['question']}: {o['prob']}%" for o in odds if o.get("prob") is not None]
    user = ("MACRO DASHBOARD SNAPSHOT:\n" + "\n".join(lines)
            + ("\n\nPREDICTION MARKETS (Fed/macro):\n" + "\n".join(odd_lines) if odd_lines else ""))
    try:
        from research.transcript_subagents._base import call_subagent
        res = call_subagent("macro_digest", "MACRO", _DIGEST_SYSTEM, user,
                            max_tokens=1600, verbose=verbose)
        digest = res.data if (res.ok and res.data) else {"regime": f"(digest unavailable: {res.error})"}
    except Exception as e:
        digest = {"regime": f"(digest error: {type(e).__name__})"}
    _DIGEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _DIGEST_CACHE.write_text(json.dumps(digest, ensure_ascii=False), encoding="utf-8")
    return digest


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    m = fetch_macro_series(force="--force" in sys.argv)
    for sid, d in m.items():
        lat = d["latest"]
        print(f"  [{d['category']:14}] {sid:16} {d['label']:30} "
              f"{lat[1] if lat else '—'} {d['unit']}  Δ={d['changes']}  pts={len(d['obs'])}")
    if "--digest" in sys.argv:
        print("\n=== MACRO DIGEST ===")
        dg = synthesize_macro_digest(m, force=True)
        for k, v in dg.items():
            print(f"\n{k.upper()}:\n  {v}")
