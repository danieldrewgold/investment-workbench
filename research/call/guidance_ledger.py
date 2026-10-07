"""
Guidance track record: every guided item and its reported outcome.

Sources of guidance (category (b) statements):
  - earnings press releases (outlook sections), extracted per release
  - earnings calls, from the transcript guidance tracker's guides_issued
Outcomes come only from reported figures in the press releases (category (a)),
never from management's commentary about how a quarter went.

Bias per metric uses the FIRST guide issued for each period, so repeated
reaffirmations don't get extra weight. Rates (comps, margins) are measured in
percentage points; levels (revenue, EPS, unit openings) in percent. The bias is
shrunk toward zero by n / (n + SHRINK) so two data points can't swing it, and
current guidance is adjusted by the shrunk bias instead of taken at face value.

Persisted at data/guidance_ledger/<TICKER>.json; extraction results are cached
by press-release content hash so reruns don't pay twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from statistics import mean

from research.call.llm import call_json, SONNET, LLMError

LEDGER_DIR = Path("data/guidance_ledger")
CACHE_DIR = LEDGER_DIR / "_extract_cache"
SHRINK = 2.0

METRICS = {
    # key: (label, kind)   kind "rate" -> error in pp; "level" -> error in %
    "comps_pct": ("comparable / same-store sales growth", "rate"),
    "revenue": ("total revenue", "level"),
    "revenue_growth_pct": ("revenue growth", "rate"),
    "gross_margin_pct": ("gross margin", "rate"),
    "unit_margin_pct": ("restaurant- or store-level margin", "rate"),
    "operating_margin_pct": ("operating margin", "rate"),
    "ebitda": ("EBITDA / adjusted EBITDA", "level"),
    "eps_gaap": ("GAAP diluted EPS", "level"),
    "eps_adjusted": ("adjusted / non-GAAP diluted EPS", "level"),
    "unit_openings": ("new unit openings", "level"),
    "capex": ("capital expenditures", "level"),
    "tax_rate_pct": ("effective tax rate", "rate"),
    "cost_inflation_pct": ("input / cost of sales inflation", "rate"),
    "price_increase_pct": ("menu price / pricing contribution", "rate"),
}

_EXTRACT_SYSTEM = """You extract structured data from a company earnings press release.
Return JSON only. Two lists:

"reported": figures the company REPORTED for completed periods (actuals). Include
the headline metrics for the quarter and, when the release covers a fiscal year,
the full-year figures too.

"guidance": forward-looking OUTLOOK / GUIDANCE the company gave for periods not yet
complete (the outlook or guidance section).

Use only these metric keys: METRIC_KEYS
If a figure doesn't fit a key, skip it. Periods are "Q1 2026" or "FY2026" (fiscal).
Give numbers as plain numbers: percentages as percent values (2.2 for 2.2%),
currency in millions (3349.0 for $3.349 billion), EPS in dollars per share,
counts as counts. For a range give low and high; for a point give low = high.
Qualitative bands: "about flat" -> -1 to 1; "low single digits" -> 1 to 3;
"low-to-mid single digits" -> 2 to 5; "mid single digits" -> 4 to 6; set
"qualitative": true for these.

{"reported": [{"metric": "", "period": "", "value": 0.0, "quote": "short verbatim"}],
 "guidance": [{"metric": "", "period": "", "low": 0.0, "high": 0.0, "qualitative": false,
               "quote": "short verbatim"}]}"""

_CALL_SYSTEM = """You normalize guidance statements made on earnings calls into numbers.
Return JSON only: {"guidance": [{"ref": 0, "metric": "", "period": "", "low": 0.0,
"high": 0.0, "qualitative": false}]}
Use only these metric keys: METRIC_KEYS
Same unit rules: percentages as percent values, currency in millions, EPS in dollars,
counts as counts. Qualitative bands: "about flat" -> -1 to 1; "low single digits" ->
1 to 3; "low-to-mid single digits" -> 2 to 5; "mid single digits" -> 4 to 6 (set
qualitative true). Skip statements with no number or band. "ref" is the input index."""


def _keys_line() -> str:
    return ", ".join(f"{k} ({v[0]})" for k, v in METRICS.items())


def _norm_period(p: str) -> str:
    p = (p or "").strip().upper().replace("FISCAL ", "FY")
    m = re.match(r"^(Q[1-4])\s*(?:FY)?\s*'?(\d{2,4})$", p)
    if m:
        y = m.group(2)
        return f"{m.group(1)} {('20' + y) if len(y) == 2 else y}"
    m = re.match(r"^(?:FY|FULL YEAR)\s*'?(\d{2,4})$", p)
    if m:
        y = m.group(1)
        return f"FY{('20' + y) if len(y) == 2 else y}"
    return p


def _period_end(p: str) -> tuple[int, int]:
    """Sortable (year, quarter) with FY treated as Q4."""
    m = re.match(r"Q([1-4]) (\d{4})", p)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.match(r"FY(\d{4})", p)
    return (int(m.group(1)), 4) if m else (0, 0)


def _extract_release(rel: dict) -> dict:
    text = rel.get("full_text_with_tables") or rel.get("text") or ""
    h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]
    cache = CACHE_DIR / f"pr_{h}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    out = call_json(_EXTRACT_SYSTEM.replace("METRIC_KEYS", _keys_line()),
                    f"Press release filed {rel.get('filing_date')}:\n\n{text[:60000]}",
                    model=SONNET, effort="low", max_tokens=8000, timeout=600)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def _normalize_call_guides(guides: list) -> list:
    if not guides:
        return []
    payload = json.dumps([{"ref": i, "metric": g.get("metric"), "period": g.get("period_guided"),
                           "statement": g.get("guide_value") or g.get("guide_language"),
                           "quote": (g.get("evidence_quote") or "")[:300]}
                          for i, g in enumerate(guides)])
    h = hashlib.sha1(payload.encode()).hexdigest()[:16]
    cache = CACHE_DIR / f"call_{h}.json"
    if cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
    else:
        out = call_json(_CALL_SYSTEM.replace("METRIC_KEYS", _keys_line()), payload,
                        model=SONNET, effort="low", max_tokens=6000, timeout=600)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    items = []
    for g in out.get("guidance") or []:
        try:
            src = guides[int(g["ref"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        items.append({**g, "source_kind": "call", "source": f"{src.get('source_quarter', '?')} call",
                      "issued": src.get("source_quarter", ""), "speaker": src.get("speaker", ""),
                      "quote": (src.get("evidence_quote") or "")[:300]})
    return items


def compute_bias(items: list, actuals: dict) -> dict:
    """Per-metric bias from the first guide issued for each period.

    items: guidance dicts with metric, period, low, high, issued_order
    actuals: {(metric, period): value}
    """
    first: dict = {}
    for g in sorted(items, key=lambda x: x.get("issued_order", (0, 0))):
        key = (g["metric"], g["period"])
        if key not in first and g.get("low") is not None and g.get("high") is not None:
            first[key] = g
    per_metric: dict = {}
    for (metric, period), g in first.items():
        actual = actuals.get((metric, period))
        if actual is None:
            continue
        mid = (float(g["low"]) + float(g["high"])) / 2
        kind = METRICS.get(metric, ("", "level"))[1]
        if kind == "rate":
            err = float(actual) - mid
        else:
            if mid == 0:
                continue
            err = (float(actual) / mid - 1) * 100
        per_metric.setdefault(metric, []).append({
            "period": period, "guide_low": g["low"], "guide_high": g["high"], "guide_mid": round(mid, 3),
            "actual": actual, "error": round(err, 3), "source": g.get("source", ""),
            "above_range": float(actual) > float(g["high"]), "below_range": float(actual) < float(g["low"]),
        })
    out = {}
    for metric, rows in per_metric.items():
        n = len(rows)
        raw = mean(r["error"] for r in rows)
        shrunk = raw * n / (n + SHRINK)
        kind = METRICS.get(metric, ("", "level"))[1]
        beats = sum(1 for r in rows if r["error"] > 0)
        out[metric] = {
            "n": n, "kind": kind, "unit": "pp" if kind == "rate" else "%",
            "mean_error": round(raw, 3), "shrunk_bias": round(shrunk, 3),
            "beat_rate": round(beats / n, 2),
            "read": ("guides low (sandbags)" if shrunk > 0 else "guides high (over-promises)" if shrunk < 0 else "on target"),
            "history": sorted(rows, key=lambda r: _period_end(r["period"])),
        }
    return out


def adjust(guide_low: float, guide_high: float, bias: dict | None) -> dict:
    mid = (guide_low + guide_high) / 2
    if not bias:
        return {"raw_mid": mid, "adjusted_mid": mid, "bias_applied": 0.0, "n": 0}
    b = bias["shrunk_bias"]
    adj = mid + b if bias["kind"] == "rate" else mid * (1 + b / 100)
    return {"raw_mid": round(mid, 3), "adjusted_mid": round(adj, 3), "bias_applied": b,
            "unit": bias["unit"], "n": bias["n"]}


def build_ledger(ticker: str, press_releases: list, guides_issued: list,
                 today: date | None = None) -> dict:
    """Build and persist the ledger. Returns the ledger dict."""
    today = today or date.today()
    reported: dict = {}
    items: list = []
    errors: list = []
    for rel in sorted(press_releases or [], key=lambda r: r.get("filing_date") or ""):
        try:
            ex = _extract_release(rel)
        except LLMError as e:
            errors.append(f"{rel.get('filing_date')}: {e}")
            continue
        fd = rel.get("filing_date", "")
        for r in ex.get("reported") or []:
            m, p = r.get("metric"), _norm_period(r.get("period", ""))
            if m in METRICS and p and r.get("value") is not None:
                reported[(m, p)] = {"value": r["value"], "source": f"press release {fd}",
                                    "quote": (r.get("quote") or "")[:200]}
        for g in ex.get("guidance") or []:
            m, p = g.get("metric"), _norm_period(g.get("period", ""))
            if m in METRICS and p and g.get("low") is not None:
                items.append({"metric": m, "period": p, "low": g.get("low"), "high": g.get("high", g.get("low")),
                              "qualitative": bool(g.get("qualitative")), "source_kind": "press_release",
                              "source": f"press release {fd}", "issued": fd,
                              "issued_order": (fd, 1), "quote": (g.get("quote") or "")[:200]})
    try:
        for g in _normalize_call_guides(guides_issued or []):
            m, p = g.get("metric"), _norm_period(g.get("period", ""))
            if m in METRICS and p and g.get("low") is not None:
                q = g.get("issued", "")
                y, qn = _period_end(_norm_period(q)) if q else (0, 0)
                # Calls happen the same day as the release for that quarter's results,
                # one quarter after the quarter being reported.
                order = f"{y}-{min(12, qn * 3 + 1):02d}-15" if y else ""
                items.append({**g, "metric": m, "period": p, "high": g.get("high", g.get("low")),
                              "issued_order": (order, 2)})
    except LLMError as e:
        errors.append(f"call guidance: {e}")

    for i, g in enumerate(sorted(items, key=lambda x: x["issued_order"])):
        g["id"] = f"G{i + 1:02d}"
        act = reported.get((g["metric"], g["period"]))
        g["actual"] = act["value"] if act else None
        g["actual_source"] = act["source"] if act else None
        g["status"] = "resolved" if act else "open"

    actuals = {k: v["value"] for k, v in reported.items()}
    bias = compute_bias(items, actuals)

    # Live guidance = open items for periods that haven't ended yet. A guide for a
    # finished period with no reported counterpart (e.g. cost inflation) is not live.
    last_reported = max((_period_end(p) for (_, p) in reported), default=(0, 0))
    live = []
    latest: dict = {}
    for g in sorted(items, key=lambda x: x["issued_order"]):
        if g["status"] == "open" and _period_end(g["period"]) > last_reported:
            latest[(g["metric"], g["period"])] = g
    for (m, p), g in sorted(latest.items(), key=lambda kv: _period_end(kv[0][1])):
        a = adjust(float(g["low"]), float(g["high"]), bias.get(m))
        live.append({"id": g["id"], "metric": m, "label": METRICS[m][0], "period": p,
                     "low": g["low"], "high": g["high"], "source": g["source"],
                     "qualitative": g.get("qualitative", False), **a})

    ledger = {
        "ticker": ticker.upper(), "built": today.isoformat(),
        "items": [{k: v for k, v in g.items() if k != "issued_order"} for g in items],
        "reported": [{"metric": k[0], "period": k[1], **v} for k, v in sorted(reported.items())],
        "bias": bias, "live_guidance": live, "errors": errors,
        "method": (f"Bias = mean(actual - first guide midpoint) per metric, in pp for rates and % for "
                   f"levels, shrunk by n/(n+{SHRINK:g}). Actuals from reported press-release figures only."),
    }
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (LEDGER_DIR / f"{ticker.upper()}.json").write_text(json.dumps(ledger, indent=1, default=str), encoding="utf-8")
    return ledger


def render_block(ledger: dict) -> str:
    """Text block for the brief and the call stage."""
    if not ledger:
        return ""
    lines = ["=== GUIDANCE TRACK RECORD (management guidance vs reported results) ===",
             ledger.get("method", "")]
    bias = ledger.get("bias") or {}
    if bias:
        lines.append("Historical bias by metric:")
        for m, b in bias.items():
            lines.append(f"  {METRICS.get(m, (m,))[0]}: mean error {b['mean_error']:+.2f}{b['unit']} over "
                         f"n={b['n']} (shrunk {b['shrunk_bias']:+.2f}{b['unit']}), beat rate "
                         f"{b['beat_rate']:.0%}, read: {b['read']}")
            for h in b["history"]:
                lines.append(f"    {h['period']}: guided {h['guide_low']} to {h['guide_high']} ({h['source']}), "
                             f"actual {h['actual']}, error {h['error']:+.2f}{b['unit']}")
    else:
        lines.append("No guidance items could be matched to reported outcomes yet.")
    live = ledger.get("live_guidance") or []
    if live:
        lines.append("Current guidance, raw vs bias-adjusted:")
        for g in live:
            adj = (f"adjusted {g['adjusted_mid']} (bias {g['bias_applied']:+.2f}{g.get('unit', '')}, n={g['n']})"
                   if g.get("n") else "no track record, unadjusted")
            lines.append(f"  [{g['id']}] {g['label']} {g['period']}: guided {g['low']} to {g['high']} "
                         f"({g['source']}{', qualitative band' if g['qualitative'] else ''}), raw mid "
                         f"{g['raw_mid']}, {adj}")
    return "\n".join(lines)
