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

# (series_id, label, unit, transform, category, subgroup)
# Non-consumer themes carry no subgroup (""). The Consumer theme is built out as
# a BofA-Consumer-Checkpoint-style DECOMPOSITION: spending power (income/wages/
# saving) -> real spending (incl. goods-vs-services rotation) -> spending by
# category (discretionary vs necessity) -> credit & stress -> sentiment. All
# series are keyless FRED. See reference-bofa-consumer-checkpoint memory.
CHART_SERIES = [
    ("A191RL1Q225SBEA", "Real GDP (QoQ annualized)",  "%",   "level", "Growth", ""),
    ("RSAFS",      "Retail sales (YoY)",          "%",   "yoy",   "Growth", ""),
    ("INDPRO",     "Industrial production (YoY)",  "%",   "yoy",   "Growth", ""),
    ("UNRATE",     "Unemployment rate",           "%",   "level", "Labor", ""),
    ("ICSA",       "Initial jobless claims",      "k",   "level", "Labor", ""),
    ("PAYEMS",     "Nonfarm payrolls (YoY)",      "%",   "yoy",   "Labor", ""),
    ("CPIAUCSL",   "CPI inflation (YoY)",          "%",   "yoy",   "Inflation", ""),
    ("CPILFESL",   "Core CPI inflation (YoY)",     "%",   "yoy",   "Inflation", ""),
    ("PPIACO",     "PPI all commodities (YoY)",    "%",   "yoy",   "Inflation", ""),
    ("FEDFUNDS",   "Fed funds rate",              "%",   "level", "Rates & credit", ""),
    ("DGS10",      "10-year Treasury yield",       "%",   "level", "Rates & credit", ""),
    ("T10Y2Y",     "Yield curve (10Y minus 2Y)",   "%",   "level", "Rates & credit", ""),
    ("BAMLH0A0HYM2", "High-yield credit spread (OAS)", "%", "level", "Rates & credit", ""),

    # ── Consumer decomposition (BofA Consumer Checkpoint style) ──
    # Spending power — the income, wages, and buffer that fund spending
    ("DSPIC96",    "Real disposable income (YoY)", "%",   "yoy",   "Consumer", "Spending power"),
    ("CES0500000003", "Avg hourly earnings (YoY)", "%",   "yoy",   "Consumer", "Spending power"),
    ("PSAVERT",    "Personal saving rate",         "%",   "level", "Consumer", "Spending power"),
    # Real spending — headline + the goods-vs-services rotation
    ("PCEC96",     "Real consumer spending (YoY)", "%",   "yoy",   "Consumer", "Real spending"),
    ("PCEDGC96",   "Spending: durable goods (YoY)", "%",  "yoy",   "Consumer", "Real spending"),
    ("PCENDC96",   "Spending: nondurable goods (YoY)", "%", "yoy", "Consumer", "Real spending"),
    ("PCESC96",    "Spending: services (YoY)",     "%",   "yoy",   "Consumer", "Real spending"),
    # Spending by category — discretionary vs necessity reads
    ("RSFSDP",     "Restaurants & bars (YoY)",     "%",   "yoy",   "Consumer", "Spending by category"),
    ("RSMVPD",     "Autos & parts (YoY)",          "%",   "yoy",   "Consumer", "Spending by category"),
    ("RSNSR",      "Online / nonstore (YoY)",      "%",   "yoy",   "Consumer", "Spending by category"),
    ("RSGMS",      "General merchandise (YoY)",    "%",   "yoy",   "Consumer", "Spending by category"),
    ("RSGASS",     "Gas stations (YoY)",           "%",   "yoy",   "Consumer", "Spending by category"),
    # Credit & stress — leverage + delinquency
    ("REVOLSL",    "Revolving (card) credit (YoY)", "%",  "yoy",   "Consumer", "Credit & stress"),
    ("TOTALSL",    "Consumer credit outstanding",  "$M",  "level", "Consumer", "Credit & stress"),
    ("DRCCLACBS",  "Credit-card delinquency rate", "%",   "level", "Consumer", "Credit & stress"),
    # Sentiment
    ("UMCSENT",    "Consumer sentiment (UMich)",   "idx", "level", "Consumer", "Sentiment"),
]

CATEGORIES = ["Growth", "Labor", "Inflation", "Rates & credit", "Consumer"]

_COST_LINE_NAMES = {"food": "Food", "labor": "Labor", "occupancy": "Occupancy",
                    "other_opex": "Other operating costs", "price": "Menu prices"}


def _cost_line_series() -> tuple[list, list]:
    """Cost-line series from the call layer's industry configs (research/call/schemas/*.json),
    so the macro page shows exactly what the calls use. One theme per config, e.g.
    'Restaurant costs', with a subgroup per cost line. Series already charted are skipped."""
    seen, rows, themes = {s[0] for s in CHART_SERIES}, [], []
    for f in sorted((Path(__file__).parent / "call" / "schemas").glob("*.json")):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        theme = f"{f.stem.replace('_', ' ').title()} costs"
        for line, series in (cfg.get("macro_by_cost_line") or {}).items():
            for s in series:
                if s["id"] in seen:
                    continue
                seen.add(s["id"])
                label = s["label"].split(" (")[0] + " (YoY)"
                rows.append((s["id"], label, "%", "yoy", theme, _COST_LINE_NAMES.get(line, line)))
                if theme not in themes:
                    themes.append(theme)
    return rows, themes


_cost_rows, _cost_themes = _cost_line_series()
CHART_SERIES += _cost_rows
CATEGORIES += _cost_themes

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
            cached = json.loads(_CACHE.read_text(encoding="utf-8"))
            if all(s[0] in cached for s in CHART_SERIES):   # a newly added series forces a refetch
                return cached
        except Exception:
            pass
    out: dict = {}
    for sid, label, unit, transform, cat, subgroup in CHART_SERIES:
        try:
            obs = _fetch_series_csv(sid, lookback_days=lookback_days) or []
        except Exception:
            obs = []
        obs = [(str(d), v) for d, v in obs if v is not None]
        if transform == "yoy":
            obs = _to_yoy(obs)
        out[sid] = {
            "label": label, "unit": unit, "category": cat, "subgroup": subgroup,
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


_DIGEST_VERSION = 2
_DIGEST_SYSTEM = """You are a macro strategist writing a short desk digest for an equity investor.
You get the latest US macro dashboard (levels plus 3mo / 12mo / 5y changes) and prediction-market
rate odds. Return ONLY JSON.

Read the regime from the data. Cite the figures you were given; never invent numbers. Be decisive.
Style: short plain sentences, no em dashes, no hedging filler. Every bullet carries a number.

Keys:
- regime: ONE sentence naming the regime (cycle stage; inflation, labor and rates direction).
- consumer: 2 to 3 bullets read off the decomposition: spending power (real income, wages, saving
  rate), real spending and goods vs services, discretionary vs necessity categories, credit stress.
  Name the divergence the data shows.
- inflation: 2 to 3 bullets: headline vs core CPI and the Fed. PPI all commodities includes energy
  and metals; if you cite it, call it that, and do not treat it as food or restaurant cost inflation.
- labor: 2 bullets.
- growth: 2 bullets.
- rates_credit: 2 bullets.
- costs: 2 to 4 bullets on the industry cost series (the "... costs" themes, e.g. Restaurant costs):
  food inputs (beef, poultry, processed foods), wages, occupancy, against menu prices. Say which
  costs are outrunning pricing and which are easing.
- whats_heading: 2 to 3 bullets on the next 1 to 2 quarters.
- investment_implications: 2 to 4 actionable bullets (sectors, duration, risk, what to watch).
Each bullet value is a list of strings."""


def synthesize_macro_digest(series: dict | None = None, odds: list | None = None,
                            *, force: bool = False, verbose: bool = False) -> dict:
    if not force and _DIGEST_CACHE.exists() and (time.time() - _DIGEST_CACHE.stat().st_mtime) < _TTL:
        try:
            cached = json.loads(_DIGEST_CACHE.read_text(encoding="utf-8"))
            if cached.get("_v") == _DIGEST_VERSION:   # a prompt change regenerates once
                return cached
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
        from research.call.llm import call_json, OPUS
        from research.call.text import scrub
        digest = scrub(call_json(_DIGEST_SYSTEM, user, model=OPUS, effort="medium", max_tokens=12000))
        digest["_v"] = _DIGEST_VERSION
    except Exception as e:
        digest = {"regime": f"(digest unavailable: {type(e).__name__})"}
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
