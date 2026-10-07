#!/usr/bin/env python3
"""
Investment Workbench Terminal  —  local research terminal over data/ outputs.

Run:
    python dashboard.py            (--no-open skips the browser; --port N picks the port)
then open http://127.0.0.1:8765

Zero dependencies (stdlib only). READ-ONLY: never writes, never triggers runs.
It reads everything the pipeline produced under data/ and lays it out like a
research terminal so you can (a) work a name as a dense tearsheet and (b) open
any pipeline function and see the actual output it produces across your whole
universe, so you know exactly what to improve.

Views
  /                       screener across every ticker
  /co/<TICKER>            company tearsheet (quote, edge, valuation, estimates,
                          financial trajectory chart, insiders, ownership, thesis)
  /fn                     function catalog (ingestion / synthesis / analysis)
  /fn/<key>?ticker=T      function inspector: this function's real output for T,
                          plus a cross-ticker gallery + coverage stats
  /compare?t=A&t=B        side-by-side metric compare
"""

from __future__ import annotations

import collections
import glob
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(DATA, "results")
CACHE = os.path.join(DATA, "dag_cache")
TRACES = os.path.join(DATA, "dag_traces")
REPORTS = os.path.join(DATA, "reports")
EXPORTS = os.path.join(DATA, "exports")

HOST, PORT = "127.0.0.1", 8765
if "--port" in sys.argv:
    PORT = int(sys.argv[sys.argv.index("--port") + 1])

# --------------------------------------------------------------------------
# Function registry: every pipeline function you can inspect. (key, label,
# group, source file, kind, locator). kind='result' pulls from the result
# JSON field <locator>; kind='cache' pulls the DAG cache step <locator>.
# --------------------------------------------------------------------------

FUNCTIONS = [
    ("quarterly_financials", "Quarterly financials", "Data", "research/quarterly_financials_loader.py", "cache", "quarterly_financials"),
    ("financials", "Annual financials", "Data", "research/financials_fetcher.py", "cache", "financials"),
    ("consensus", "Consensus / estimates", "Data", "research/consensus_loader.py", "result", "consensus_full"),
    ("market_overlay", "Market overlay", "Data", "research/market_overlay.py", "cache", "market_overlay"),
    ("peer_comps", "Peer comps", "Data", "research/peer_comps.py", "cache", "peer_comps"),
    ("news", "News + sentiment", "Data", "research/news_loader.py", "cache", "news"),
    ("filing_form4", "Insider Form 4", "Ownership", "ingestion/loaders/edgar_form4_loader.py", "cache", "filing_form4"),
    ("filing_13d", "13D / 13G", "Ownership", "ingestion/loaders/edgar_13d_loader.py", "cache", "filing_13d"),
    ("crowding_assessment", "13F crowding", "Ownership", "research/crowding_analysis.py", "cache", "crowding_assessment"),
    ("bond_health", "Bond health", "Credit", "research/bond_health.py", "cache", "bond_health"),
    ("social_topic_analysis", "Social topics + silence", "Sentiment", "research/social_topic_analyzer.py", "cache", "social_topic_analysis"),
    ("stocktwits", "StockTwits stream", "Sentiment", "research/stocktwits_loader.py", "cache", "stocktwits"),
    ("bear_research", "Short-seller reports", "Sentiment", "research/short_research_loader.py", "cache", "bear_research"),
    ("guidance_bundle", "Guidance", "Synthesis", "research/guidance_extractor.py", "result", "guidance_bundle"),
    ("research_brief", "Research brief", "Synthesis", "research/deep_research.py", "result", "narrative_synthesis"),
    ("edge_detector", "Edge detection", "Analysis", "research/edge_detector.py", "result", "edge_assessment"),
    ("estimate_model", "EPS / estimate model", "Analysis", "research/estimate_model.py", "result", "eps_build"),
    ("valuation", "Valuation", "Analysis", "research/valuation.py", "result", "valuation"),
    ("adversarial", "Adversarial challenge", "Analysis", "research/adversarial.py", "result", "adversarial_response"),
    ("evidence_audit", "Evidence audit", "Analysis", "research/evidence_audit.py", "result", "evidence_audit_findings"),
    ("claim_verifier", "Claim verification", "Analysis", "research/claim_verifier.py", "result", "verifications"),
    ("decision", "Decision gate", "Analysis", "research/escalation.py", "result", "decision_verdict"),
]
FN_BY_KEY = {f[0]: f for f in FUNCTIONS}

FN_DESC = {
    "quarterly_financials": "Last ~11-12 quarters of real actuals (Polygon) used for the sequential-math discipline.",
    "financials": "Structured TTM/annual financials (Polygon -> Alpha Vantage -> registry).",
    "consensus": "Street EPS / revenue by period, price target, ratings, LTG, analyst count.",
    "market_overlay": "Price, short interest and market context overlaid on the name.",
    "peer_comps": "Peer multiples for the assigned sector schema (feeds the mechanical EPS check + valuation).",
    "news": "Polygon + Alpha Vantage news, de-noised, with bull/bear sentiment.",
    "filing_form4": "Insider Form 4 open-market buys/sells over 180d; skips RSU vests / tax withholdings; computes % of stake.",
    "filing_13d": "Activist 13D vs passive 13G filings.",
    "crowding_assessment": "13F institutional crowding / positioning.",
    "bond_health": "Credit / bond-spread regime from the FINRA market-credit feed.",
    "social_topic_analysis": "Clusters retail social chatter into themes and flags [SILENCE] (under-discussed) topics.",
    "stocktwits": "Raw StockTwits message stream with bull/bear labels.",
    "bear_research": "Scans for short-seller reports (Hindenburg, Spruce Point, Fuzzy Panda ...).",
    "guidance_bundle": "Extracted management guidance items.",
    "research_brief": "The one rich Claude reasoning call: drivers, edge hypothesis, contradictions, narrative synthesis.",
    "edge_detector": "Back-solves consensus, computes variant drivers, scores actionability, sets the edge verdict.",
    "estimate_model": "Mechanical schema-driven EPS build from the brief's drivers + components.",
    "valuation": "Applies a multiple to our EPS -> implied price, upside, sensitivity.",
    "adversarial": "Independent challenge pass: new contradictions, blind spots, structural critiques.",
    "evidence_audit": "Audits each driver component against its cited evidence.",
    "claim_verifier": "Extracts factual claims from the brief and web-verifies them.",
    "decision": "Final decision gate (NOT_VALUABLE_YET / etc.).",
}

# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

_RESULT_CACHE = {}
_STEPS_CACHE = {}


def _safe_load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def parse_result_name(fn):
    base = os.path.basename(fn)
    base = base[:-5] if base.endswith(".json") else base
    parts = base.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:-2]), parts[-2] + "_" + parts[-1]
    return base, ""


def list_results():
    out = collections.defaultdict(list)
    for fn in glob.glob(os.path.join(RESULTS, "*.json")):
        t, stamp = parse_result_name(fn)
        if t:
            out[t].append((stamp, fn))
    for t in out:
        out[t].sort(reverse=True)
    return out


def all_tickers():
    return sorted(list_results().keys())


def load_result(ticker, run=None):
    runs = list_results().get(ticker, [])
    if not runs:
        return None, None
    path = dict(runs).get(run) if run else runs[0][1]
    if not path:
        path = runs[0][1]
    stamp = next((s for s, p in runs if p == path), runs[0][0])
    if path not in _RESULT_CACHE:
        _RESULT_CACHE[path] = _safe_load(path) or {}
    return _RESULT_CACHE[path], stamp


def cache_steps(ticker):
    if ticker in _STEPS_CACHE:
        return _STEPS_CACHE[ticker]
    d = os.path.join(CACHE, ticker)
    steps = {}
    if os.path.isdir(d):
        for fn in glob.glob(os.path.join(d, "*.json")):
            base = os.path.basename(fn)[:-5]
            step = base.rsplit("_", 1)[0] if "_" in base else base
            mt = os.path.getmtime(fn)
            if step not in steps or mt > steps[step][1]:
                steps[step] = (fn, mt)
    _STEPS_CACHE[ticker] = steps
    return steps


def fn_output(ticker, fn):
    _, _, _, _, kind, loc = fn
    if kind == "result":
        d, _ = load_result(ticker)
        return (d or {}).get(loc)
    steps = cache_steps(ticker)
    if loc in steps:
        raw = _safe_load(steps[loc][0]) or {}
        return raw.get("output", raw)
    return None


def files_for(ticker, folder):
    if not os.path.isdir(folder):
        return []
    return sorted((os.path.basename(p) for p in glob.glob(os.path.join(folder, ticker + "_*"))), reverse=True)


def parse_quarterly(text):
    """Parse the quarterly_financials corpus_text table -> [{period,revenue,op_margin,eps}], oldest first."""
    rows = []
    if not text:
        return rows
    for line in text.splitlines():
        m = re.match(r"\s*(Q[1-4]\s+\d{4})\s+(.*)", line)
        if not m:
            continue
        period = re.sub(r"\s+", " ", m.group(1)).strip()
        rest = m.group(2)
        toks = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)(M?)", rest)
        moneyM = [t.replace(",", "") for t, suf in toks if suf == "M"]
        nonM = [t.replace(",", "") for t, suf in toks if suf != "M"]
        rev = float(moneyM[0]) if moneyM else None
        eps = float(nonM[-1]) if nonM else None
        opm = None
        if len(moneyM) >= 2:
            pos = rest.find(moneyM[1].replace(".0", "")) if "." not in moneyM[1] else rest.find(moneyM[1])
            seg = rest[rest.rfind("M", 0, rest.find("%")) :] if "%" in rest else rest
            mm = re.search(r"M\s+([\d.]+)%", rest)
            if mm:
                opm = float(mm.group(1))
        rows.append({"period": period, "revenue": rev, "op_margin": opm, "eps": eps})
    rows.reverse()
    return rows

# --------------------------------------------------------------------------
# Formatting / rendering
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s))


def num(x, pre="", suf="", d=2):
    try:
        return pre + ("%.*f" % (d, float(x))) + suf
    except Exception:
        return "—"


def fmt_int(x):
    try:
        return "{:,.0f}".format(float(x))
    except Exception:
        return "—"


def signed_pct(x, d=1):
    try:
        v = float(x)
        return ("+" if v > 0 else "") + ("%.*f%%" % (d, v))
    except Exception:
        return "—"


def render_value(v, key=""):
    if v is None or v == "" or v == [] or v == {}:
        return '<span class="dim">—</span>'
    if isinstance(v, bool):
        return '<span class="mono">%s</span>' % ("true" if v else "false")
    if isinstance(v, (int, float)):
        return '<span class="mono">%s</span>' % esc(v)
    if isinstance(v, str):
        if len(v) > 130 or "\n" in v:
            return '<pre class="prose">%s</pre>' % esc(v)
        return esc(v)
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return '<ul class="lst">%s</ul>' % "".join("<li>%s</li>" % render_value(x) for x in v)
        return "".join('<div class="subcard">%s</div>' % render_value(x) for x in v)
    if isinstance(v, dict):
        rows = "".join("<tr><td class=k>%s</td><td>%s</td></tr>" % (esc(k), render_value(val, k)) for k, val in v.items())
        return '<table class="kv">%s</table>' % rows
    return esc(v)

# --------------------------------------------------------------------------
# Per-function preview (one glance for the gallery)
# --------------------------------------------------------------------------

def preview(key, out):
    if out is None:
        return '<span class="dim">no output</span>'
    if isinstance(out, str):
        return esc(out[:160]) + ("…" if len(out) > 160 else "")
    chips = []

    def chip(label, val, cls=""):
        chips.append('<span class="chip %s">%s <b>%s</b></span>' % (cls, esc(label), esc(val)))

    if isinstance(out, dict):
        if key == "edge_detector":
            v = out.get("verdict")
            chip("verdict", v or "—", "ok" if v and "PROBABLE" in str(v) else "")
            if out.get("actionability_score") is not None:
                chip("score", num(out.get("actionability_score"), d=3))
            if out.get("priced_in") is not None:
                chip("priced-in", "yes" if out.get("priced_in") else "no")
        elif key == "valuation":
            chip("implied", num(out.get("implied_price"), pre="$"))
            up = out.get("upside_pct")
            chip("upside", signed_pct(up), "up" if isinstance(up, (int, float)) and up > 0 else "dn")
            chip("mult", num(out.get("applied_multiple"), suf="x", d=1))
        elif key == "estimate_model":
            chip("our eps", num(out.get("our_eps")))
            chip("Δ eps", num(out.get("sum_eps_impact")))
        elif key == "filing_form4":
            chip("insiders", out.get("n_unique_insiders", "—"))
            chip("filings", out.get("n_filings_total", "—"))
        elif key == "bond_health":
            chip("status", out.get("auth_status") or out.get("regime") or "—")
        elif key == "consensus":
            chip("FY eps", num(out.get("current_year", {}).get("eps") if isinstance(out.get("current_year"), dict) else out.get("eps")))
            chip("analysts", out.get("max_analysts", "—"))
        else:
            for k in list(out.keys())[:3]:
                val = out[k]
                if isinstance(val, (str, int, float, bool)):
                    chip(k, (str(val)[:24]))
        if not chips:
            chip("keys", ", ".join(list(out.keys())[:4]))
    elif isinstance(out, list):
        chip("items", len(out))
    return "".join(chips) or '<span class="dim">—</span>'

# --------------------------------------------------------------------------
# Rich panel renderers (estimates matrix, insiders, peers, bond health)
# --------------------------------------------------------------------------

def render_estimates(d, plabels=None):
    cf = d.get("consensus_full") or {}
    periods = [("current_quarter", "Curr Q"), ("next_quarter", "Next Q"),
               ("current_year", "Curr FY"), ("next_year", "Next FY")]
    data = {}
    for key, deflbl in periods:
        p = cf.get(key) or {}
        if not p:
            continue
        data[key] = {
            "label": (plabels or {}).get(key) or deflbl,
            "eps": {"mean": p.get("eps_mean"), "low": p.get("eps_low"), "high": p.get("eps_high"),
                    "ya": p.get("eps_year_ago"), "g": p.get("eps_growth_yoy"), "n": p.get("eps_num_analysts"),
                    "now": p.get("eps_current"), "d90": p.get("eps_90d_ago"),
                    "up": p.get("up_revs_30d"), "dn": p.get("down_revs_30d")},
            "rev": {"mean": p.get("revenue_mean"), "low": p.get("revenue_low"), "high": p.get("revenue_high"),
                    "ya": p.get("revenue_year_ago"), "g": p.get("revenue_growth_yoy"), "n": p.get("revenue_num_analysts")},
        }
    if not data:
        return '<span class="empty">no consensus loaded</span>'
    our_rev = d.get("post_revenue")
    data["our"] = {"eps": d.get("post_eps"),
                   "rev": (our_rev * 1e6 if isinstance(our_rev, (int, float)) else None)}
    cols = [(k, data[k]["label"]) for k, _ in periods if k in data]

    def revB(x):
        try:
            return "$%.1fB" % (float(x) / 1e9)
        except Exception:
            return "-"

    head = "<tr><th>Line item</th>" + "".join("<th class='num'>" + esc(lbl) + "</th>" for k, lbl in cols) + "</tr>"

    def cells(metric, fmt):
        out = ""
        for k, _ in cols:
            v = data[k][metric]["mean"]
            out += "<td class='ec num' onclick=\"ed('" + k + "','" + metric + "')\">" + fmt(v) + "</td>"
        return out

    def gcells(metric):
        out = ""
        for k, _ in cols:
            g = data[k][metric]["g"]
            out += "<td class='num dim'>" + (signed_pct(g * 100) if isinstance(g, (int, float)) else "-") + "</td>"
        return out

    body = ("<tr><td class=k>Revenue</td>" + cells("rev", revB) + "</tr>"
            "<tr><td class=k>Rev YoY</td>" + gcells("rev") + "</tr>"
            "<tr><td class=k>EPS</td>" + cells("eps", lambda x: num(x, pre="$")) + "</tr>"
            "<tr><td class=k>EPS YoY</td>" + gcells("eps") + "</tr>"
            "<tr><td class=k>Analysts</td>"
            + "".join("<td class='num dim'>" + str(data[k]["eps"]["n"] or "-") + "</td>" for k, _ in cols) + "</tr>")
    table = "<table class='emx'><thead>" + head + "</thead><tbody>" + body + "</tbody></table>"
    detail = ('<div id="edet" class="edet">Click any Revenue or EPS cell for the high/low range, '
              'estimate revisions, and our model vs consensus.</div>')
    js = ("<script>var ECONS=" + json.dumps(data) + ";"
          "function ed(p,m){var o=(ECONS[p]||{})[m];if(!o)return;"
          "var our=m==='eps'?ECONS.our.eps:ECONS.our.rev;"
          "var fm=m==='rev'?function(x){return x==null?'-':'$'+(x/1e9).toFixed(2)+'B'}:function(x){return x==null?'-':'$'+(+x).toFixed(2)};"
          "var pc=function(x){return x==null?'-':(x>0?'+':'')+(x*100).toFixed(1)+'%'};"
          "var h='<div class=eh>'+ECONS[p].label+' &middot; '+(m==='eps'?'EPS':'Revenue')+'</div>';"
          "h+='<div class=erow><span>consensus mean</span><b>'+fm(o.mean)+'</b></div>';"
          "h+='<div class=erow><span>low - high</span><span>'+fm(o.low)+' - '+fm(o.high)+'</span></div>';"
          "h+='<div class=erow><span>YoY growth</span><span>'+pc(o.g)+'  (vs '+fm(o.ya)+')</span></div>';"
          "h+='<div class=erow><span>analysts</span><span>'+(o.n||'-')+'</span></div>';"
          "if(m==='eps'){h+='<div class=erow><span>revision (90d)</span><span>'+fm(o.d90)+' -&gt; '+fm(o.now)+'</span></div>';"
          "h+='<div class=erow><span>up / down (30d)</span><span>'+(o.up||0)+' up / '+(o.dn||0)+' down</span></div>';}"
          "h+='<div class=erow style=\"border-top:1px solid var(--bd);margin-top:6px;padding-top:6px\"><span>our model (fwd)</span><b>'+fm(our)+'</b></div>';"
          "if(our!=null&&o.mean){var dl=(our-o.mean)/o.mean*100;h+='<div class=erow><span>vs consensus</span><b class=\"'+(dl>=0?'up':'dn')+'\">'+(dl>=0?'+':'')+dl.toFixed(1)+'%</b></div>';}"
          "document.getElementById('edet').innerHTML=h;}</script>")
    return table + detail + js


def parse_form4_dates(corpus):
    out = {}
    if not corpus:
        return out
    cur = None
    for line in corpus.splitlines():
        m = re.search(r"\[(?:SOLD|BOUGHT|BUY)\]\s+(.+?)\s+\(", line)
        if m:
            cur = m.group(1).strip().upper()
            continue
        dm = re.search(r"\((\d{4}-\d{2}-\d{2})(?:\s+to\s+(\d{4}-\d{2}-\d{2}))?", line)
        if dm and cur:
            out[cur] = dm.group(2) or dm.group(1)
            cur = None
    return out


_INSIDER_PROFILES = None


def _insider_profiles():
    global _INSIDER_PROFILES
    if _INSIDER_PROFILES is None:
        p = os.path.join(DATA, "insider_profiles.json")
        _INSIDER_PROFILES = (_safe_load(p) or {}) if os.path.exists(p) else {}
    return _INSIDER_PROFILES


def _nw_fmt(v):
    """$1.40B / $41.4M — net-worth figures span 4 orders of magnitude."""
    return f"${v/1e9:.2f}B" if v >= 1e9 else f"${v/1e6:.1f}M"


def render_call(d):
    """The call panel: stance, thesis, price, expected value, multiple, scenarios,
    catalysts and kill criteria. Shows the failure reasons when the call failed."""
    if d.get("call_error"):
        return ('<p class="dn" style="font-weight:600">Call failed validation, so no view was published.</p>'
                '<p class="muted" style="font-size:12px">%s</p>' % esc(d["call_error"]))
    res = d.get("call") or {}
    c, dv, lp = res.get("call") or {}, res.get("derived") or {}, res.get("live_price") or {}
    stance = (c.get("stance") or "").replace("_", " ").upper()
    color = {"LONG": "up", "SHORT": "dn", "AVOID": "dn"}.get(stance, "muted")
    evr = dv.get("expected_return_pct")
    head = ('<div style="display:flex;gap:18px;align-items:baseline;flex-wrap:wrap">'
            '<span class="%s" style="font-size:22px;font-weight:700">%s</span>'
            '<span class="muted">%s conviction</span>'
            '<span>price <b>$%s</b> <span class="dim">(close %s)</span></span>'
            '<span>expected value <b>$%s</b> <span class="%s">(%s)</span></span></div>') % (
        color, esc(stance), esc(dv.get("conviction") or c.get("conviction") or ""),
        num(lp.get("price")), esc(lp.get("session_date", "")), num(dv.get("expected_value")),
        "up" if (evr or 0) >= 0 else "dn", esc(f"{evr:+.1f}%" if evr is not None else "n/a"))
    parts = [head, '<p style="font-size:15px;margin:10px 0">%s</p>' % esc(c.get("thesis", ""))]
    if c.get("no_edge_trigger"):
        parts.append('<p class="muted">What would create an edge: %s</p>' % esc(c["no_edge_trigger"]))
    if c.get("why_not_short") and c.get("stance") == "avoid":
        parts.append('<p class="muted">Why not short: %s</p>' % esc(c["why_not_short"]))
    mv = c.get("multiple_view") or {}
    if mv:
        parts.append('<p><b>Multiple:</b> %sx %s looks <b>%s</b>, likely to <b>%s</b>. <span class="muted">%s</span></p>' % (
            esc(str(mv.get("current_multiple"))), esc(mv.get("basis", "")), esc(mv.get("verdict", "")),
            esc(mv.get("direction", "")), esc(mv.get("reasoning", ""))))
    rows = ""
    for n in ("bull", "base", "bear"):
        s = (dv.get("scenarios") or {}).get(n) or {}
        rows += "<tr><td>%s</td><td>$%s</td><td>%sx</td><td>$%s</td><td>%s</td><td>%s</td><td class='muted' style='font-size:12px'>%s</td></tr>" % (
            n, num(s.get("eps")), num(s.get("multiple"), d=1), num(s.get("target")),
            esc(f"{s.get('return_pct', 0):+.0f}%"), esc(f"{float(s.get('probability', 0)):.0%}"), esc(s.get("reasoning", "")))
    parts.append('<table><thead><tr><th>Case</th><th>EPS</th><th>Multiple</th><th>Target</th><th>vs price</th>'
                 '<th>Prob. (proposed)</th><th>Reasoning</th></tr></thead><tbody>%s</tbody></table>' % rows)
    cats = "".join('<li><b>%s</b> %s</li>' % (esc(str(k.get("date"))), esc(k.get("event", "")))
                   for k in sorted(c.get("catalysts") or [], key=lambda x: str(x.get("date"))))
    kills = "".join("<li>%s</li>" % esc(k) for k in c.get("kill_criteria") or [])
    parts.append('<div style="display:flex;gap:30px;flex-wrap:wrap;margin-top:8px">'
                 '<div><b>Catalysts</b><ul style="margin:4px 0 0 16px">%s</ul></div>'
                 '<div><b>Kill criteria</b><ul style="margin:4px 0 0 16px">%s</ul></div></div>' % (cats, kills))
    links = []
    for key, label in (("digest_path", "full digest"), ("pitch_path", "one-page pitch")):
        if res.get(key):
            links.append('<a class="tag" href="/report/%s">%s</a>' % (urllib.parse.quote(os.path.basename(res[key])), label))
    if links:
        parts.append('<div class="row" style="margin-top:8px">' + " ".join(links) + "</div>")
    return "".join(parts)


def render_insiders(f4, sc13=None):
    aggs = f4.get("aggregates") or []
    if not aggs:
        return '<span class="empty">no open-market insider activity in the last 180 days</span>'
    dates = parse_form4_dates(f4.get("corpus_text", ""))
    profiles = _insider_profiles()
    tot_sell = sum((a.get("sale_value") or 0) for a in aggs)
    tot_buy = sum((a.get("buy_value") or 0) for a in aggs)
    n_sell = sum(1 for a in aggs if (a.get("sale_value") or 0) > (a.get("buy_value") or 0))
    n_buy = sum(1 for a in aggs if (a.get("buy_value") or 0) > (a.get("sale_value") or 0))
    summary = ('<div class="stat" style="margin-bottom:10px">'
               f'<div class="b"><div class="l">Sellers</div><div class="v">{n_sell}</div></div>'
               f'<div class="b"><div class="l">Buyers</div><div class="v">{n_buy}</div></div>'
               f'<div class="b"><div class="l">Sold 180d</div><div class="v">${tot_sell/1e6:.1f}M</div></div>'
               f'<div class="b"><div class="l">Bought 180d</div><div class="v">${tot_buy/1e6:.1f}M</div></div></div>')
    any_web = False
    any_sec = False
    any_13d = False
    trs = ""
    for a in sorted(aggs, key=lambda x: -((x.get("sale_value") or 0) + (x.get("buy_value") or 0))):
        name = a.get("filer_name", "")
        prof = profiles.get(name.upper().strip(), {})
        buy, sell = a.get("buy_value") or 0, a.get("sale_value") or 0
        is_buy = buy > sell
        amt = buy if is_buy else sell
        remaining = a.get("approx_stake_value_remaining") or 0
        wealth = remaining + sell
        role = esc(prof.get("role") or (a.get("relationship") or "")[:24])
        nw_lo, nw_hi = prof.get("net_worth_low"), prof.get("net_worth_high")
        nw_mid = ((nw_lo + nw_hi) / 2 * 1e6) if (isinstance(nw_lo, (int, float)) and isinstance(nw_hi, (int, float))) else None
        nwp = a.get("networth") or {}
        nwp_val = nwp.get("est_disclosed_equity")
        # Best deterministic floor across sources: the SEC cross-company Form 4
        # crawl vs the insider's latest SC 13D/G stake in THIS company. The Form 4
        # crawl only sees the share class the insider trades — a founder's Class B /
        # holding-company position appears only in 13D/13G (Koerl read $41M on
        # Class A Form 4s while his 13G shows a 31.6% stake ≈ $1.4B). Both are
        # floors, so the larger is the tighter one. Matched by exact filer CIK.
        s13 = (sc13 or {}).get((a.get("filer_cik") or "").strip())
        s13_val = (s13 or {}).get("value")
        floor_val, floor_is_13d = None, False
        if isinstance(nwp_val, (int, float)) and nwp_val > 0:
            floor_val = nwp_val
        if isinstance(s13_val, (int, float)) and s13_val > (floor_val or 0):
            floor_val, floor_is_13d = s13_val, True
        if nw_mid is not None and nw_mid > wealth:
            any_web = True
            denom = nw_mid
            nw_disp = f"${nw_lo:g}-{nw_hi:g}M" if nw_lo != nw_hi else f"~${nw_lo:g}M"
            others = ("; also: " + ", ".join(prof.get("others") or [])) if prof.get("others") else ""
            tip = esc((str(prof.get("source", "")) + others).strip())
            nw_cell = (f'<span title="{tip}" style="border-bottom:1px dotted var(--dim);cursor:help">{nw_disp}</span>'
                       f' <sup style="color:var(--ac)">w</sup>')
        elif isinstance(floor_val, (int, float)) and floor_val > wealth * 1.02:
            denom = floor_val
            if floor_is_13d:
                any_13d = True
                pct13 = s13.get("pct")
                tip = esc(f"{s13.get('form') or 'SC 13D/G'} filed {s13.get('filed') or '?'}: "
                          f"{(s13.get('shares') or 0)/1e6:.1f}M shares"
                          + (f" ({pct13:g}% of class)" if isinstance(pct13, (int, float)) else "")
                          + " valued at current price — catches Class B / holding-company "
                            "stakes the Form 4 crawl never sees")
                nw_cell = (f'<span title="{tip}" style="border-bottom:1px dotted var(--dim);cursor:help">{_nw_fmt(floor_val)}</span>'
                           f' <sup style="color:#9b8cff">d</sup>')
            else:
                any_sec = True
                comps = [c for c in (nwp.get("companies") or []) if c.get("value")]
                tip = esc("; ".join(f"{(c.get('symbol') or '?')} ${(c.get('value') or 0)/1e6:.1f}M" for c in comps[:6])
                          + f"  (SEC disclosed equity across {nwp.get('n_companies', 0)} companies)")
                nw_cell = (f'<span title="{tip}" style="border-bottom:1px dotted var(--dim);cursor:help">{_nw_fmt(floor_val)}</span>'
                           f' <sup style="color:var(--gd)">s</sup>')
        else:
            denom = wealth
            nw_cell = f'{num(wealth/1e6, pre="$", suf="M", d=1)} <sup class="dim">f</sup>'
        pct = (amt / denom * 100) if denom else None
        px = a.get("buy_avg_price") if is_buy else a.get("sale_avg_price")
        if isinstance(pct, (int, float)):
            barw = min(100, max(2, pct))
            col = "var(--gd)" if is_buy else "var(--rd)"
            pctcell = (f"<div class='pctbar'><span class='pt'><i style='width:{barw:.0f}%;background:{col}'></i></span>"
                       f"<b>{pct:.1f}%</b></div>")
        else:
            pctcell = "-"
        trs += (f"<tr><td>{esc(name)}</td><td class='dim'>{role}</td>"
                f"<td class='dim'>{esc(dates.get(name.upper(),'-'))}</td>"
                f"<td class='{'up' if is_buy else 'dn'}'>{'BUY' if is_buy else 'SELL'}</td>"
                f"<td class='num'>{num(amt/1e6, pre='$', suf='M', d=2)}</td>"
                f"<td class='num'>{nw_cell}</td>"
                f"<td>{pctcell}</td>"
                f"<td class='num dim'>{num(px, pre='$')}</td></tr>")
    legend = ((('<sup style="color:var(--ac)">w</sup> third-party web estimate; ' if any_web else '')
               + ('<sup style="color:var(--gd)">s</sup> SEC-aggregated disclosed equity across companies (a floor, computed by the pipeline); ' if any_sec else '')
               + ('<sup style="color:#9b8cff">d</sup> latest SC 13D/G stake in this company valued at current price '
                  '(catches Class B / holding-company shares invisible to Form 4s); ' if any_13d else '')
               + '<sup class="dim">f</sup> filing only (stake in this company). ')) if (any_web or any_sec or any_13d) else ''
    note = ('<p class="muted" style="font-size:11px;margin:9px 0 0">' + legend +
            'Net worth is the best deterministic floor available: the pipeline\'s SEC-aggregated disclosed equity across every '
            'company the insider files Form 4s for (<sup style="color:var(--gd)">s</sup>), upgraded to their latest SC 13D/G '
            'stake when that is larger (<sup style="color:#9b8cff">d</sup>) — the Form 4 crawl only sees the share class the '
            'insider trades, so a founder\'s Class B / holding-company position shows up only in 13D/13G. Both can miss old '
            'untraded stakes and never see private wealth. A curated third-party estimate overrides where on file '
            '(<sup style="color:var(--ac)">w</sup>); otherwise it falls back to their stake in this company '
            '(<sup class="dim">f</sup>). Hover any figure for its breakdown. RSU vests / tax withholdings excluded; '
            '180-day window.</p>')
    return (summary + "<table><thead><tr><th>Insider</th><th>Role</th><th>Latest trade</th><th>Side</th>"
            "<th class='num'>Trade</th><th class='num'>Est. net worth</th><th>% of NW</th>"
            "<th class='num'>Avg px</th></tr></thead><tbody>" + trs + "</tbody></table>" + note)


def parse_peer_table(corpus):
    rows = []
    for line in (corpus or "").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9.\-]{0,6})\s+([+\-]?[\d.]+%)\s+([+\-]?[\d.]+%)\s+([\d.]+)", line)
        if m:
            rows.append({"peer": m.group(1), "rev": m.group(2), "eps": m.group(3),
                         "pe": m.group(4), "revs": re.sub(r"^[×xX]\s*", "", line[m.end():].strip())})
    return rows


def render_peers(pc):
    if not pc:
        return '<span class="empty">no peer comps</span>'
    rows = pc.get("rows")
    subject = (pc.get("subject") or pc.get("subject_ticker") or "").upper()
    schema = pc.get("schema") or pc.get("schema_type") or ""

    def mc(v, suf="", d=1, pct=False):
        if v is None:
            return "-"
        try:
            if pct:
                return ("+%.1f%%" % v) if v > 0 else ("%.1f%%" % v)
            return ("%.*f" % (d, float(v))) + suf
        except Exception:
            return "-"

    if rows:
        trs = ""
        for r in rows:
            tk = (r.get("ticker") or "").upper()
            mark = ' style="background:var(--surf2)"' if tk == subject else ""
            nm = "<b>" + esc(tk) + "</b>" + (" <span class='dim' style='font-size:10px'>subject</span>" if tk == subject else "")
            trs += ("<tr" + mark + "><td>" + nm + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_rev_growth_pct"), pct=True) + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_eps_growth_pct"), pct=True) + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_pe"), suf="x") + "</td>"
                    + "<td class='num'>" + mc(r.get("trailing_pe"), suf="x") + "</td>"
                    + "<td class='num'>" + mc(r.get("ev_ebitda"), suf="x") + "</td>"
                    + "<td class='num'>" + mc(r.get("ebitda_growth_pct"), pct=True) + "</td>"
                    + "<td class='num dim'>+" + str(r.get("up_revs_30d") or 0) + "/-" + str(r.get("down_revs_30d") or 0) + "</td></tr>")
        note = ('<p class="muted" style="font-size:11px;margin:8px 0 0">Forward growth and revisions from consensus; '
                'trailing P/E and EV/EBITDA from yfinance fundamentals; EBITDA growth is latest annual vs prior year '
                '(best-effort). Comp set: <code>' + esc(schema) + '</code>. A true 5-yr-average P/E is not available from this source.</p>')
        return ("<table><thead><tr><th>Peer</th><th class='num'>FY rev %</th><th class='num'>FY EPS %</th>"
                "<th class='num'>Fwd P/E</th><th class='num'>Trail P/E</th><th class='num'>EV/EBITDA</th>"
                "<th class='num'>EBITDA grw</th><th class='num'>Revs 30d</th></tr></thead><tbody>"
                + trs + "</tbody></table>" + note)

    rows2 = parse_peer_table(pc.get("corpus_text", ""))
    if not rows2:
        return '<pre class="prose">' + esc(pc.get("corpus_text", "")) + "</pre>"
    trs = ""
    for r in rows2:
        trs += ("<tr><td><b>" + esc(r["peer"]) + "</b></td><td class='num'>" + esc(r["rev"])
                + "</td><td class='num'>" + esc(r["eps"]) + "</td><td class='num'>" + esc(r["pe"])
                + "x</td><td class='dim'>" + esc(r["revs"]) + "</td></tr>")
    note = ('<p class="muted" style="font-size:11px;margin:8px 0 0">Forward consensus (yfinance), schema <code>'
            + esc(schema) + '</code>. Older cache without EV/EBITDA or trailing P/E; the enriched curated comp set '
            'populates those columns.</p>')
    return ("<table><thead><tr><th>Peer</th><th class='num'>FY rev growth</th><th class='num'>FY EPS growth</th>"
            "<th class='num'>Fwd P/E</th><th>Revisions 30d</th></tr></thead><tbody>" + trs + "</tbody></table>" + note)


def render_sotp_comps(ticker):
    """Sum-of-the-parts comp tables (data/dag_cache/<T>/peer_comps_sotp.json):
    breaks a conglomerate into segment peer sets — e.g. FEMSA = Coca-Cola FEMSA
    (KOF) bottlers + OXXO convenience retail — each vs its own peer universe.
    Built by build_femsa_comps.py. Returns None when no artifact exists."""
    path = os.path.join(DATA, "dag_cache", ticker.upper(), "peer_comps_sotp.json")
    data = _safe_load(path)
    if not data or not data.get("tables"):
        return None

    def mc(v, suf="", pct=False):
        if not isinstance(v, (int, float)) or v != v:
            return "-"
        if pct:
            return ("+%.1f%%" % v) if v > 0 else ("%.1f%%" % v)
        return ("%.1f" % v) + suf

    out = []
    for t in data["tables"]:
        subj = (t.get("subject_ticker") or "").upper()
        refonly = {r.upper() for r in (t.get("reference_only") or [])}
        med = t.get("peer_median") or {}
        trs = ""
        for r in t.get("rows", []):
            tk = (r.get("ticker") or "").upper()
            if tk == subj:
                mark, tag = ' style="background:var(--surf2)"', " <span class='dim' style='font-size:10px'>subject</span>"
            elif tk in refonly:
                mark, tag = "", " <span class='dim' style='font-size:10px'>ref</span>"
            else:
                mark, tag = "", ""
            price = ("$%.2f" % r["current_price"]) if isinstance(r.get("current_price"), (int, float)) else "-"
            trs += ("<tr" + mark + "><td><b>" + esc(tk) + "</b>" + tag + "</td>"
                    + "<td class='num'>" + price + "</td>"
                    + "<td class='num'>" + mc(r.get("ev_ebitda"), "x") + "</td>"
                    + "<td class='num'>" + mc(r.get("trailing_pe"), "x") + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_pe"), "x") + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_rev_growth_pct"), pct=True) + "</td>"
                    + "<td class='num'>" + mc(r.get("fwd_eps_growth_pct"), pct=True) + "</td>"
                    + "<td class='num dim'>" + mc(r.get("ebitda_growth_pct"), pct=True) + "</td></tr>")
        medrow = ("<tr style='border-top:2px solid var(--bd2)'><td class='dim'>peer median</td><td></td>"
                  + "<td class='num'><b>" + mc(med.get("ev_ebitda"), "x") + "</b></td>"
                  + "<td class='num'><b>" + mc(med.get("trailing_pe"), "x") + "</b></td>"
                  + "<td class='num'><b>" + mc(med.get("fwd_pe"), "x") + "</b></td>"
                  + "<td></td><td></td><td></td></tr>")
        out.append("<h4 style='margin:16px 0 4px'>" + esc(t.get("label") or "") + "</h4>"
                   + "<table><thead><tr><th>Ticker</th><th class='num'>Price</th><th class='num'>EV/EBITDA</th>"
                     "<th class='num'>Trail P/E</th><th class='num'>Fwd P/E</th><th class='num'>Fwd rev%</th>"
                     "<th class='num'>Fwd EPS%</th><th class='num'>EBITDA grw</th></tr></thead><tbody>"
                   + trs + medrow + "</tbody></table>")
    fnotes = ""
    if data.get("footnotes"):
        items = "".join("<li>" + esc(f) + "</li>" for f in data["footnotes"])
        fnotes = ('<ul class="muted" style="font-size:11px;margin:8px 0 0;padding-left:16px">' + items + "</ul>")
    note = ('<p class="muted" style="font-size:11px;margin:8px 0 0">Sum-of-the-parts: each segment valued against its own '
            'peer set. EV/EBITDA recomputed currency-consistently for foreign/ADR names; forward P/E prefers yfinance’s own '
            'forwardPE and rejects values inconsistent with trailing (drops stock-split artifacts); peer median excludes the '
            'subject and reference-only rows. Refresh via <code>python build_femsa_comps.py</code>.</p>')
    return "".join(out) + fnotes + note


def bond_regime(corpus):
    ig = hy = None
    if corpus:
        m = re.search(r"Investment Grade[^\n]*?net (BUY\w*|SELL\w*)", corpus, re.I)
        if m:
            ig = "buying" if m.group(1).upper().startswith("BUY") else "selling"
        m = re.search(r"High[- ]?Yield[^\n]*?net (BUY\w*|SELL\w*)", corpus, re.I)
        if m:
            hy = "buying" if m.group(1).upper().startswith("BUY") else "selling"
    return ig, hy


def render_bond_health(bh):
    if not bh:
        return '<span class="empty">no bond health data</span>'
    findings = bh.get("findings") or []
    cps = [(f.get("coupon_pct"), f.get("par_amount_m")) for f in findings if f.get("coupon_pct") and f.get("par_amount_m")]
    avg_coupon = (sum(c * p for c, p in cps) / sum(p for _, p in cps)) if cps else None
    mats = sorted([f.get("maturity_date") for f in findings if f.get("maturity_date")])
    debt = bh.get("total_long_term_debt_m")
    priced = bh.get("n_priced") or 0
    div = bh.get("credit_equity_divergence_flag")
    ig, hy = bond_regime(bh.get("corpus_text", ""))

    explain = ('<details style="margin-bottom:10px"><summary>what is this?</summary>'
               '<p class="muted" style="font-size:12px;margin:6px 0 0">A credit-market early-warning check. '
               "Bondholders get paid before shareholders, so the credit market often senses trouble (or comfort) before the "
               "stock does. We look at (1) the company's own bonds and whether their spreads are widening, and (2) the broad "
               "corporate-bond tape (are investors net buying or selling credit, i.e. risk-on vs risk-off). Spreads blowing out "
               "while the stock holds up is a warning; a calm credit tape is reassuring.</p></details>")

    cpvals = [f.get("coupon_pct") for f in findings if f.get("coupon_pct")]
    crange = (num(min(cpvals), suf="%", d=2) + " to " + num(max(cpvals), suf="%", d=2)) if cpvals else "n/a"
    mrange = (mats[0][:4] + " to " + mats[-1][:4]) if mats else "n/a"
    sent = []
    if debt is not None:
        sent.append("Carries about $" + fmt_int(debt) + "M of long-term debt across "
                    + str(bh.get("bond_count", len(findings))) + " senior note series (coupons " + crange
                    + ", maturing " + mrange + "), which is light, cheap, long-dated paper.")
    if priced == 0 or bh.get("auth_status") == "no_per_cusip":
        sent.append("Per-bond market pricing is not available on the free FINRA tier, so this issuer's own spread "
                    "moves cannot be tracked directly this run.")
    if ig:
        sent.append("The broad credit tape is risk-" + ("on" if ig == "buying" else "off")
                    + " right now (investment-grade investors net " + ig
                    + ((", high-yield net " + hy) if hy else "") + ").")
    sent.append("Credit is diverging from the equity, worth a closer look." if div
                else "No credit-vs-equity divergence flag: the bond market is not signaling stress the stock has missed.")
    if div:
        verdict = "Watch - credit and equity are diverging."
    elif debt is not None and (avg_coupon is None or avg_coupon < 4):
        verdict = "Benign - small, cheap, long-dated debt and a calm credit tape. Credit is not a risk to the thesis here."
    else:
        verdict = "Neutral - nothing alarming in the credit picture."
    takeaway = ('<div class="tkbox"><span class="tklbl">Takeaway</span> ' + esc(verdict)
                + '<p class="muted" style="font-size:12px;margin:6px 0 0">' + esc(" ".join(sent)) + "</p></div>")

    nextmat = mats[0] if mats else None
    tape = ("risk-" + ("on" if ig == "buying" else "off")) if ig else "n/a"
    stat = ('<div class="stat" style="margin-bottom:10px">'
            '<div class="b"><div class="l">LT debt</div><div class="v">$' + (fmt_int(debt) if debt is not None else "0") + 'M</div></div>'
            '<div class="b"><div class="l">Series</div><div class="v">' + str(bh.get("bond_count", len(findings))) + '</div></div>'
            '<div class="b"><div class="l">Avg coupon</div><div class="v">' + (num(avg_coupon, suf="%", d=2) if avg_coupon else "-") + '</div></div>'
            '<div class="b"><div class="l">Next maturity</div><div class="v" style="font-size:14px">' + esc(nextmat or "-") + '</div></div>'
            '<div class="b"><div class="l">Credit tape</div><div class="v" style="font-size:14px">' + tape + '</div></div></div>')

    trs = ""
    for f in sorted(findings, key=lambda x: x.get("maturity_date") or ""):
        px = (num(f.get("last_price")) if f.get("has_price") else '<span class="dim">no px</span>')
        trs += ("<tr><td>" + esc(f.get("series_label", "")) + "</td><td class='num'>" + num(f.get("coupon_pct"), suf="%", d=3)
                + "</td><td class='num'>$" + fmt_int(f.get("par_amount_m")) + "M</td><td class='num'>"
                + esc(f.get("maturity_date") or "-") + "</td><td>" + ("yes" if f.get("is_callable") else "no")
                + "</td><td class='num'>" + px + "</td></tr>")
    ladder = ("<table><thead><tr><th>Series</th><th class='num'>Coupon</th><th class='num'>Par</th>"
              "<th class='num'>Maturity</th><th>Callable</th><th class='num'>Price</th></tr></thead><tbody>"
              + trs + "</tbody></table>")
    return explain + takeaway + stat + ladder


def svg_price(hist):
    """Google-style price line from [[date, close], ...]."""
    pts = [(i, c) for i, (_, c) in enumerate(hist) if isinstance(c, (int, float))]
    if len(pts) < 2:
        return ""
    W, H, pl, pr, pt, pb = 720, 200, 8, 56, 12, 22
    n = len(hist)
    closes = [c for _, c in pts]
    lo, hi = min(closes), max(closes)
    if hi == lo:
        hi = lo + 1
    pw, ph = W - pl - pr, H - pt - pb

    def X(i):
        return pl + pw * i / (n - 1)

    def Y(c):
        return pt + ph - ph * (c - lo) / (hi - lo)

    poly = " ".join(f"{X(i):.1f},{Y(c):.1f}" for i, c in pts)
    last, first = closes[-1], closes[0]
    color = "var(--gd)" if last >= first else "var(--rd)"
    yl = (f'<text x="{W-pr+5}" y="{pt+9}" style="fill:var(--dim);font-size:9px">${hi:.0f}</text>'
          f'<text x="{W-pr+5}" y="{pt+ph}" style="fill:var(--dim);font-size:9px">${lo:.0f}</text>'
          f'<text x="{W-pr+5}" y="{Y(last)+3:.0f}" style="fill:{color};font-size:10px;font-weight:600">${last:.2f}</text>')
    xl = (f'<text x="{pl}" y="{H-6}" style="fill:var(--dim);font-size:9px">{esc(hist[0][0][:7])}</text>'
          f'<text x="{pl+pw}" y="{H-6}" text-anchor="end" style="fill:var(--dim);font-size:9px">{esc(hist[-1][0][:7])}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="price history" style="display:block">'
            f'<polyline points="{poly}" style="fill:none;stroke:{color};stroke-width:1.6"/>{yl}{xl}</svg>')


_PRICE_HIST = None


def price_hist(ticker):
    """Per-ticker daily+intraday closes (data/price_history.json) for the
    interactive chart. Refresh via `python refresh_price_history.py`."""
    global _PRICE_HIST
    if _PRICE_HIST is None:
        _PRICE_HIST = (_safe_load(os.path.join(DATA, "price_history.json")) or {}).get("history") or {}
    return _PRICE_HIST.get(ticker.upper()) or {}


_PRICE_CHART_JS = r"""
(function(){
 var root=document.currentScript.closest('.pchart'); if(!root||root.__pc)return; root.__pc=1;
 var data=JSON.parse(root.querySelector('.pcdata').textContent);
 var daily=data.daily||[], intraday=data.intraday||[];
 var svg=root.querySelector('.pcsvg'), tip=root.querySelector('.pctip');
 var elP=root.querySelector('.pcprice'), elC=root.querySelector('.pcchg'), elD=root.querySelector('.pcdate');
 var line=root.querySelector('.pcline'), area=root.querySelector('.pcarea');
 var cross=root.querySelector('.pccross'), dot=root.querySelector('.pcdot'), sel=root.querySelector('.pcsel');
 var hiT=root.querySelector('.pchi'), loT=root.querySelector('.pclo');
 var VW=1000,VH=240,PADL=6,PADR=56,PADT=10,PADB=20,G='#41d18f',R='#f2616b';
 var cur=[],xs=[],ys=[],color=G,drag=null,curH='1Y',hovering=false;
 function sliceFor(h){
  if(h==='1D'){ if(!intraday.length)return[]; var d=intraday[intraday.length-1][0].slice(0,10); return intraday.filter(function(p){return p[0].slice(0,10)===d;}); }
  if(h==='5D')return intraday.slice();
  if(!daily.length)return[];
  var end=new Date(daily[daily.length-1][0]).getTime(), from;
  if(h==='1M')from=end-31*864e5; else if(h==='6M')from=end-183*864e5;
  else if(h==='1Y')from=end-365*864e5; else if(h==='5Y')from=end-5*365*864e5;
  else if(h==='YTD')from=Date.UTC(new Date(daily[daily.length-1][0]).getUTCFullYear(),0,1);
  else return daily.slice();
  return daily.filter(function(p){return new Date(p[0]).getTime()>=from;});
 }
 function fp(v){return '$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});}
 function fa(v){return v>=1000?'$'+(v/1000).toFixed(1)+'k':'$'+v.toFixed(v<10?2:0);}
 function fl(s){return s.indexOf('T')>-1?s.replace('T',' '):s;}
 function X(i,N){return PADL+(VW-PADL-PADR)*i/(N>1?N-1:1);}
 var loV,hiV,Yf;
 function draw(h){
  curH=h; cur=sliceFor(h);
  root.querySelectorAll('.pcb').forEach(function(b){b.classList.toggle('on',b.dataset.h===h);});
  clearSel(); drag=null;
  if(cur.length<2){line.setAttribute('d','');area.setAttribute('d','');elP.textContent='—';elC.textContent='';return;}
  var N=cur.length; loV=Infinity; hiV=-Infinity;
  for(var i=0;i<N;i++){var c=cur[i][1]; if(c<loV)loV=c; if(c>hiV)hiV=c;}
  var rLo=loV,rHi=hiV; if(hiV===loV)hiV=loV+1; var pad=(hiV-loV)*0.08, lo=loV-pad, hi=hiV+pad;
  Yf=function(c){return PADT+(VH-PADT-PADB)*(1-(c-lo)/(hi-lo));};
  xs=[];ys=[]; var d='';
  for(var i=0;i<N;i++){var x=X(i,N),y=Yf(cur[i][1]); xs.push(x);ys.push(y); d+=(i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)+' ';}
  var a=d+'L'+X(N-1,N).toFixed(1)+' '+(VH-PADB)+' L'+X(0,N).toFixed(1)+' '+(VH-PADB)+' Z';
  color=cur[N-1][1]>=cur[0][1]?G:R;
  line.setAttribute('d',d); line.setAttribute('stroke',color);
  area.setAttribute('d',a); area.setAttribute('fill',color);
  hiT.textContent=fa(rHi); hiT.setAttribute('y',Yf(rHi)+3);
  loT.textContent=fa(rLo); loT.setAttribute('y',Yf(rLo)+3);
  setHeader(0,N-1);
 }
 function setHeader(i,j){
  var a=cur[i][1], b=cur[j][1], ab=b-a, pc=a?(b/a-1)*100:0, up=ab>=0;
  elP.textContent=fp(b);
  elC.textContent=(up?'+':'-')+'$'+Math.abs(ab).toFixed(2)+' ('+(up?'+':'')+pc.toFixed(2)+'%)';
  elC.className='pcchg '+(up?'up':'dn');
  elD.textContent=' · '+(i===0&&j===cur.length-1?curH:(fl(cur[i][0])+' → '+fl(cur[j][0])));
 }
 function idxAt(clientX){
  var r=svg.getBoundingClientRect(), vx=(clientX-r.left)/r.width*VW, t=(vx-PADL)/(VW-PADL-PADR);
  return Math.max(0,Math.min(cur.length-1,Math.round(t*(cur.length-1))));
 }
 function hover(i){
  cross.style.display='';dot.style.display='';
  cross.setAttribute('x1',xs[i]);cross.setAttribute('x2',xs[i]);cross.setAttribute('y1',PADT);cross.setAttribute('y2',VH-PADB);
  dot.setAttribute('cx',xs[i]);dot.setAttribute('cy',ys[i]);dot.setAttribute('fill',color);
  var r=svg.getBoundingClientRect(), px=xs[i]/VW*r.width;
  tip.style.display='block'; tip.innerHTML='<b>'+fp(cur[i][1])+'</b><br><span class="pctd">'+fl(cur[i][0])+'</span>';
  var tw=tip.offsetWidth; tip.style.left=Math.max(0,Math.min(r.width-tw,px-tw/2))+'px';
 }
 function drawSel(a,b){var x1=xs[Math.min(a,b)],x2=xs[Math.max(a,b)]; sel.style.display=''; sel.setAttribute('x',x1); sel.setAttribute('width',Math.max(1,x2-x1)); sel.setAttribute('y',PADT); sel.setAttribute('height',VH-PADT-PADB);}
 function clearSel(){sel.style.display='none';}
 function setHeaderLive(price){
  if(price==null||!cur.length)return; var a=cur[0][1],ab=price-a,pc=a?(price/a-1)*100:0,up=ab>=0;
  elP.textContent=fp(price); elC.textContent=(up?'+':'-')+'$'+Math.abs(ab).toFixed(2)+' ('+(up?'+':'')+pc.toFixed(2)+'%)';
  elC.className='pcchg '+(up?'up':'dn'); elD.textContent=' · '+curH;
 }
 svg.addEventListener('mousemove',function(e){ if(cur.length<2)return; hovering=true; var i=idxAt(e.clientX); hover(i);
  if(drag!==null){drawSel(drag,i); setHeader(Math.min(drag,i),Math.max(drag,i));} else {setHeader(0,i);} });
 svg.addEventListener('mouseleave',function(){ hovering=false; cross.style.display='none';dot.style.display='none';tip.style.display='none';
  if(drag===null)setHeader(0,cur.length-1); });
 svg.addEventListener('mousedown',function(e){ if(cur.length<2)return; drag=idxAt(e.clientX); e.preventDefault(); });
 window.addEventListener('mouseup',function(e){ if(drag===null)return; var i=idxAt(e.clientX);
  if(Math.abs(i-drag)<2){clearSel(); setHeader(0,cur.length-1);} drag=null; });
 root.querySelectorAll('.pcb').forEach(function(b){ b.addEventListener('click',function(){ draw(b.dataset.h); }); });
 if(!intraday.length) root.querySelectorAll('.pcb').forEach(function(b){ if(b.dataset.h==='1D'||b.dataset.h==='5D'){b.disabled=true;b.style.opacity=.35;} });
 draw('1Y');
 // Live: the global poller calls __pcLive with the current price every ~15s.
 window.__LIVE_T=data.ticker;
 window.__pcLive=function(price,chg){
  if(drag!==null||hovering)return;
  if(curH==='1D'||curH==='5D'){
   fetch('/api/intraday?t='+encodeURIComponent(data.ticker)).then(function(r){return r.json();}).then(function(j){
    if(j.intraday&&j.intraday.length&&drag===null&&!hovering){intraday=j.intraday;draw(curH);} }).catch(function(){});
  } else { setHeaderLive(price); }
 };
})();
"""


def render_price_chart(ticker, fallback_hist=None):
    """Interactive (Google-Finance-style) price chart: horizon buttons, hover
    crosshair + tooltip, drag-to-measure % change. Data from price_history.json;
    falls back to the static weekly line if the rich snapshot is missing."""
    ph = price_hist(ticker)
    daily = ph.get("daily") or []
    intraday = ph.get("intraday") or []
    if not daily and fallback_hist:
        daily = [[d, c] for d, c in fallback_hist if isinstance(c, (int, float))]
    if len(daily) < 2:
        return svg_price(fallback_hist or [])
    import json as _json
    payload = _json.dumps({"ticker": ticker.upper(), "daily": daily, "intraday": intraday}, separators=(",", ":"))
    horizons = ["1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX"]
    btns = "".join(f'<button class="pcb" data-h="{h}">{h}</button>' for h in horizons)
    return (
        '<div class="pchart">'
        '<div class="pchdr"><span class="pcprice">—</span>'
        '<span class="pcchg"></span><span class="pcdate"></span></div>'
        f'<div class="pcbtns">{btns}</div>'
        '<div class="pcwrap">'
        '<svg class="pcsvg" viewBox="0 0 1000 240" role="img" aria-label="interactive price chart">'
        '<rect class="pcsel" x="0" y="0" width="0" height="0" fill="#7896d2" opacity="0.16" style="display:none"/>'
        '<path class="pcarea" d="" stroke="none" opacity="0.10"/>'
        '<path class="pcline" d="" fill="none" stroke-width="1.6"/>'
        '<line class="pccross" stroke="var(--dim)" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>'
        '<circle class="pcdot" r="3.6" stroke="var(--bg)" stroke-width="1.5" style="display:none"/>'
        '<text class="pchi" x="948" y="14" fill="var(--dim)" font-size="10"></text>'
        '<text class="pclo" x="948" y="220" fill="var(--dim)" font-size="10"></text>'
        '</svg>'
        '<div class="pctip"></div></div>'
        f'<script class="pcdata" type="application/json">{payload.replace("<", chr(92) + "u003c")}</script>'
        f'<script>{_PRICE_CHART_JS}</script>'
        '</div>')


def svg_hbars(items):
    """Horizontal +/- bars (HTML), green up / red down, for position changes."""
    items = [(l, v) for l, v in items if isinstance(v, (int, float))][:8]
    if not items:
        return ""
    mx = max(abs(v) for _, v in items) or 1
    out = '<div class="hbox">'
    for l, v in items:
        w = 200 * abs(v) / mx
        col = "var(--gd)" if v >= 0 else "var(--rd)"
        cls = "up" if v >= 0 else "dn"
        sign = "+" if v >= 0 else ""
        out += (f'<div class="hb"><span class="hbl">{esc(l[:24])}</span>'
                f'<span class="hbar"><i style="width:{w:.0f}px;background:{col}"></i></span>'
                f'<span class="hbv {cls}">{sign}${fmt_int(v)}M</span></div>')
    return out + "</div>"


def render_market(mo):
    if not mo:
        return '<span class="empty">no market overlay</span>'
    import datetime as _dt
    today = _dt.date.today()
    mc, pcr, iv, hv = mo.get("market_cap"), mo.get("put_call_ratio"), mo.get("near_term_iv"), mo.get("historical_vol_30d")
    spf, sr, beta, price = mo.get("short_pct_float"), mo.get("short_ratio"), mo.get("beta"), mo.get("price")
    eds = mo.get("earnings_dates") or []
    dte = None
    if eds:
        try:
            y, m, dd = eds[0].split("-")
            dte = (_dt.date(int(y), int(m), int(dd)) - today).days
        except Exception:
            pass

    def cap(x):
        if not isinstance(x, (int, float)):
            return "-"
        return f"${x/1e9:.1f}B" if x >= 1e9 else f"${x/1e6:.0f}M"

    def pctf(x, d=1):
        return f"{x*100:.{d}f}%" if isinstance(x, (int, float)) else "-"

    dte_disp = (f"{dte}d" if (dte is not None and dte > 0) else ("reported" if dte is not None else "-"))
    tkr = (mo.get("ticker") or "").upper()
    has_rich = bool((price_hist(tkr) or {}).get("daily"))
    if has_rich or mo.get("price_history"):
        chart = render_price_chart(tkr, mo.get("price_history"))
        if not has_rich:
            chart += ('<p class="muted" style="font-size:11px;margin:4px 0 10px">~1-year weekly close. '
                      'Run <code>python refresh_price_history.py ' + esc(tkr) + '</code> for the full interactive chart.</p>')
    else:
        chart = '<p class="dim" style="font-size:11px;margin-bottom:8px">Price chart needs enrichment for this ticker.</p>'
    stat = ('<div class="stat" style="margin-bottom:10px">'
            f'<div class="b"><div class="l">Price</div><div class="v">{num(price, pre="$")}</div></div>'
            f'<div class="b"><div class="l">Market cap</div><div class="v">{cap(mc)}</div></div>'
            f'<div class="b"><div class="l">Beta</div><div class="v">{num(beta)}</div></div>'
            f'<div class="b"><div class="l">Short % float</div><div class="v">{pctf(spf)}</div></div>'
            f'<div class="b"><div class="l">Days to cover</div><div class="v">{num(sr, d=1)}</div></div>'
            f'<div class="b"><div class="l">Put / call</div><div class="v">{num(pcr, d=2)}</div></div>'
            f'<div class="b"><div class="l">Near IV</div><div class="v">{pctf(iv)}</div></div>'
            f'<div class="b"><div class="l">30d real vol</div><div class="v">{pctf(hv)}</div></div>'
            f'<div class="b"><div class="l">To earnings</div><div class="v" style="font-size:15px">{dte_disp}</div></div></div>')
    notes = []
    if isinstance(pcr, (int, float)):
        tag = ("call-heavy (bullish / speculative lean)" if pcr < 0.8 else
               "balanced / neutral" if pcr <= 1.2 else "put-heavy (hedging / bearish lean)")
        notes.append(f"<b>Put/call {pcr:.2f}</b>: {tag}. Total put volume over call volume; ~1 is neutral, "
                     "below ~0.7 is call-skewed, above ~1.2 leans defensive.")
    if isinstance(iv, (int, float)) and isinstance(hv, (int, float)) and hv:
        r = iv / hv
        tag = ("rich, options price meaningfully more move than the stock has realized (event/earnings premium)" if r > 1.2 else
               "roughly fair versus realized" if r >= 0.9 else "cheap versus realized")
        notes.append(f"<b>Near-term IV {iv*100:.1f}%</b> vs 30-day realized {hv*100:.1f}% ({r:.2f}x): {tag}.")
    if isinstance(spf, (int, float)):
        lvl = ("negligible" if spf < 0.02 else "low" if spf < 0.05 else "moderate" if spf < 0.10 else "elevated / squeeze-prone")
        chg = ""
        if isinstance(mo.get("short_change_pct"), (int, float)):
            c = mo["short_change_pct"]
            chg = f" Shares short {'rose' if c > 0 else 'fell'} {abs(c):.0f}% vs the prior month."
        notes.append(f"<b>Short interest {spf*100:.1f}% of float</b> ({lvl}), {num(sr, d=1)} days to cover.{chg}")
    if dte is not None:
        notes.append("<b>Earnings</b>: " + (f"in {dte} days ({eds[0]})" if dte > 0 else
                     f"last reported around {eds[0]}; next date not yet set"))
    notes_html = ('<div class="tkbox" style="margin-top:4px">'
                  + "".join(f'<p class="muted" style="font-size:12px;margin:0 0 6px">{x}</p>' for x in notes)
                  + "</div>") if notes else ""
    return chart + stat + notes_html


def render_crowding(cr, d13):
    if not cr:
        return '<span class="empty">no 13F crowding data</span>'
    expl = ('<details style="margin-bottom:10px"><summary>what is this?</summary>'
            '<p class="muted" style="font-size:12px;margin:6px 0 0">13F crowding gauges how concentrated and one-sided '
            'institutional ownership is. We track which funds in our universe hold the name, how many are entering vs '
            'exiting, and how position sizes shift quarter on quarter. NORMAL = unremarkable ownership and flows; CROWDED '
            '= many similar funds piled onto the same side (an unwind risk if sentiment turns); thin or falling ownership '
            'can signal neglect or distribution. Score runs 0 (uncrowded) to 1 (very crowded).</p></details>')
    level, score, own = cr.get("crowding_level") or "-", cr.get("weighted_score"), cr.get("ownership_pct")
    stat = ('<div class="stat" style="margin-bottom:10px">'
            f'<div class="b"><div class="l">Crowding</div><div class="v" style="font-size:15px">{esc(level)}</div></div>'
            f'<div class="b"><div class="l">Score (0-1)</div><div class="v">{num(score, d=2)}</div></div>'
            f'<div class="b"><div class="l">Inst. ownership</div><div class="v">{num(own, suf="%", d=1)}</div></div>'
            f'<div class="b"><div class="l">Funds holding</div><div class="v">{cr.get("funds_holding","-")}/{cr.get("funds_tracked","-")}</div></div>'
            f'<div class="b"><div class="l">Entry/exit</div><div class="v" style="font-size:14px">{esc(cr.get("entry_exit_trend","-"))}</div></div>'
            f'<div class="b"><div class="l">Net in/out</div><div class="v" style="font-size:15px">+{cr.get("net_entries",0)}/-{cr.get("net_exits",0)}</div></div></div>')
    holders = cr.get("top_holders") or []
    trs = ""
    for h in holders[:10]:
        dv, dp, st = h.get("delta_value_m"), h.get("delta_pct"), h.get("entry_exit", "")
        stc = "up" if st in ("INCREASED", "NEW", "ENTERED") else "dn" if st in ("DECREASED", "EXITED", "SOLD") else "dim"
        dvs = (("+" if (dv or 0) >= 0 else "") + fmt_int(dv)) if dv is not None else "-"
        dps = (("+" if (dp or 0) >= 0 else "") + f"{dp:.0f}%") if isinstance(dp, (int, float)) else "-"
        trs += (f"<tr><td>{esc(h.get('fund_name',''))}</td><td class='dim'>{esc((h.get('fund_type','') or '').replace('_',' '))}</td>"
                f"<td class='num'>${fmt_int(h.get('value_m'))}M</td>"
                f"<td class='num {'up' if (dv or 0) >= 0 else 'dn'}'>{dvs}</td>"
                f"<td class='num'>{dps}</td><td class='{stc}'>{esc(st)}</td></tr>")
    htable = ("<table><thead><tr><th>Fund</th><th>Type</th><th class='num'>Value</th>"
              "<th class='num'>&Delta; $M</th><th class='num'>&Delta; %</th><th>Status</th></tr></thead><tbody>"
              + trs + "</tbody></table>")
    chart = svg_hbars([(h.get("fund_name", ""), h.get("delta_value_m")) for h in holders[:8]])
    chart_block = ('<p class="muted" style="font-size:11px;margin:12px 0 4px">Position change last quarter '
                   '($M, green = added, red = trimmed)</p>' + chart) if chart else ""
    f13 = (d13 or {}).get("filings") or []
    act = [f for f in f13 if f.get("activist_intent")]
    seen, rows13 = set(), ""
    for f in f13:
        k = (f.get("filer_name"), f.get("form_type"), f.get("filed_date"))
        if k in seen:
            continue
        seen.add(k)
        ia = f.get("activist_intent")
        pcl = num(f.get("pct_of_class"), suf="%", d=2) if f.get("pct_of_class") is not None else "-"
        rows13 += (f"<tr><td>{esc(f.get('filer_name',''))}</td>"
                   f"<td class='{'dn' if ia else 'dim'}'>{esc(f.get('form_type',''))}{' (activist)' if ia else ''}</td>"
                   f"<td class='num'>{pcl}</td><td class='dim'>{esc(f.get('filed_date',''))}</td></tr>")
        if len(seen) >= 8:
            break
    t13 = ""
    if rows13:
        t13 = ('<p class="muted" style="font-size:11px;margin:14px 0 4px">13D / 13G filings ('
               + (f"{len(act)} activist" if act else "all passive 13G") + ')</p>'
               "<table><thead><tr><th>Filer</th><th>Form</th><th class='num'>% class</th><th>Filed</th></tr></thead><tbody>"
               + rows13 + "</tbody></table>")
    tw = (f"{esc(level)} crowding (score {num(score, d=2)}): {cr.get('funds_holding','-')} of {cr.get('funds_tracked','-')} "
          f"tracked funds hold, {num(own, suf='%', d=1)} institutional, entries/exits {esc((cr.get('entry_exit_trend','-') or '').lower())}. "
          + ("No 13D activist on file (only passive 13G holders)." if not act else f"{len(act)} activist 13D filer(s) on file."))
    takeaway = f'<div class="tkbox"><span class="tklbl">Takeaway</span> {tw}</div>'
    return expl + takeaway + stat + htable + chart_block + t13


def render_guidance(gb):
    items = (gb or {}).get("items") or []
    if not items:
        return '<span class="empty">no guidance extracted</span>'
    metrics, periods = {}, []
    for it in items:
        ml = it.get("metric_label") or it.get("metric") or "?"
        pe = it.get("period") or "-"
        metrics.setdefault(ml, {}).setdefault(pe, []).append(it)
        if pe not in periods:
            periods.append(pe)
    periods.sort()
    head = "<tr><th>Metric</th>" + "".join(f"<th>{esc(p)}</th>" for p in periods) + "</tr>"
    body = ""
    for ml, byper in metrics.items():
        body += f"<tr><td class=k>{esc(ml)}</td>"
        for p in periods:
            its = byper.get(p)
            if not its:
                body += "<td class='dim'>-</td>"
                continue
            cell = ""
            for it in its:
                lo, hi, unit = it.get("value_low"), it.get("value_high"), it.get("value_unit") or ""
                if lo is not None and hi is not None:
                    val = f"{lo:g}-{hi:g} {unit}".strip()
                elif lo is not None:
                    val = f"{lo:g} {unit}".strip()
                else:
                    val = (it.get("raw_value") or "")[:80]
                src = it.get("source_detail") or it.get("source_type") or ""
                conf = it.get("confidence", "")
                cell += (f"<div style='margin:3px 0'><b>{esc(val)}</b>"
                         f"<div class='dim' style='font-size:10px'>{esc(src)}{' &middot; ' + esc(conf) if conf else ''}</div></div>")
            body += f"<td>{cell}</td>"
        body += "</tr>"
    table = f"<table class='emx'><thead>{head}</thead><tbody>{body}</tbody></table>"
    expl = ('<p class="muted" style="font-size:11px;margin:8px 0 0">Each cell is management guidance for that metric and '
            'period, tagged with the quarter/call it was stated on, so repeated statements show how guidance has been '
            'revised. Source: ' + esc(", ".join((gb or {}).get("sources_used") or []) or "transcript") + ".</p>")
    return table + expl


# --------------------------------------------------------------------------
# SVG financial trajectory chart (server-side, theme-aware, native hover)
# --------------------------------------------------------------------------

def _axis_money(v):
    """Compact $ axis label: $70B / $1.3B / $527M / $0."""
    a = abs(v)
    if a >= 1000:
        return f"${v/1000:.0f}B" if a >= 10000 else f"${v/1000:.1f}B"
    if a < 1:
        return "$0"
    return f"${v:.0f}M"


def _big_money(v):
    """Compact $ for market cap / enterprise value (raw dollars in):
    $1.21T / $112.3B / $4.56B / $812M. Returns '—' for missing/NaN."""
    if not isinstance(v, (int, float)) or v != v:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v/1e12:.2f}T"
    if a >= 1e10:
        return f"${v/1e9:.0f}B"
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def svg_trajectory(series, max_q=13):
    """Revenue (bars, left $ axis) + Adj EPS (line, right $ axis) by quarter,
    with gridlines, both axes labeled, a legend, and YoY revenue-growth call-
    outs. Shows the most recent `max_q` quarters so labels stay readable."""
    series = [s for s in series if s.get("revenue") is not None]
    if len(series) < 2:
        return ""
    full = series
    series = series[-max_q:]
    n = len(series)
    base_i = len(full) - n      # offset so YoY can reach back into hidden quarters
    W, H = 760, 300
    pl, pr, pt, pb = 60, 54, 38, 50
    pw, ph = W - pl - pr, H - pt - pb
    revs = [s["revenue"] for s in series]
    epss = [s["eps"] for s in series if s.get("eps") is not None]
    rmax = max(revs) * 1.16
    emin, emax = (min(epss), max(epss)) if epss else (0.0, 1.0)
    emin = min(0.0, emin)
    if emax <= emin:
        emax = emin + 1
    emax += (emax - emin) * 0.12

    def rx(i):
        return pl + pw * (i + 0.5) / n

    def ry(v):
        return pt + ph - ph * v / rmax

    def ey(v):
        return pt + ph - ph * (v - emin) / (emax - emin)

    bw = pw / n * 0.58
    # gridlines + dual axis labels
    grid, nl = [], 4
    for k in range(nl + 1):
        gy = pt + ph * k / nl
        rv = rmax * (1 - k / nl)
        ev = emax - (emax - emin) * k / nl
        grid.append(f'<line x1="{pl}" y1="{gy:.1f}" x2="{pl+pw}" y2="{gy:.1f}" stroke="var(--bd)" opacity=".55"/>')
        grid.append(f'<text x="{pl-7}" y="{gy+3:.1f}" text-anchor="end" fill="var(--ac)" font-size="9.5">{_axis_money(rv)}</text>')
        grid.append(f'<text x="{pl+pw+7}" y="{gy+3:.1f}" fill="var(--gd)" font-size="9.5">${ev:.2f}</text>')
    zero = ""
    if emin < 0 < emax:
        zy = ey(0)
        zero = f'<line x1="{pl}" y1="{zy:.1f}" x2="{pl+pw}" y2="{zy:.1f}" stroke="var(--bd2)" stroke-dasharray="3 3"/>'
    bars, dots, labs, pts = [], [], [], []
    for i, s in enumerate(series):
        x = rx(i)
        y = ry(s["revenue"])
        bars.append(f'<rect x="{x-bw/2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{pt+ph-y:.1f}" rx="2" '
                    f'fill="var(--ac)" opacity=".38"><title>{esc(s["period"])}  revenue ${fmt_int(s["revenue"])}M</title></rect>')
        gi = base_i + i
        if gi >= 4 and full[gi - 4].get("revenue") and n <= 14:
            g = (s["revenue"] / full[gi - 4]["revenue"] - 1) * 100
            labs.append(f'<text x="{x:.1f}" y="{y-4:.1f}" text-anchor="middle" '
                        f'fill="var(--{"gd" if g >= 0 else "rd"})" font-size="8.5">{g:+.0f}%</text>')
        if s.get("eps") is not None:
            yy = ey(s["eps"])
            pts.append((x, yy))
            dots.append(f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="2.8" fill="var(--gd)">'
                        f'<title>{esc(s["period"])}  EPS ${s["eps"]:.2f}</title></circle>')
        if n <= 16 or i % 2 == 0:
            # Compact "Q4'25" label — the apostrophe entity is injected OUTSIDE
            # esc() (esc would turn & into &amp; and show it literally).
            mlab = re.match(r"(Q[1-4])\s+(\d{4})", s["period"] or "")
            lab = f"{mlab.group(1)}&#8217;{mlab.group(2)[2:]}" if mlab else esc(s["period"])
            labs.append(f'<text x="{x:.1f}" y="{H-pb+20:.1f}" text-anchor="middle" '
                        f'fill="var(--mut)" font-size="9">{lab}</text>')
    line = ('<polyline points="%s" fill="none" stroke="var(--gd)" stroke-width="2"/>'
            % " ".join("%.1f,%.1f" % p for p in pts))
    growth_note = ('<text x="%d" y="23" fill="var(--dim)" font-size="10">labels = YoY rev growth</text>' % (pl + 248)) if n <= 14 else ""
    legend = (f'<rect x="{pl}" y="13" width="11" height="11" rx="2" fill="var(--ac)" opacity=".5"/>'
              f'<text x="{pl+16}" y="23" fill="var(--mut)" font-size="11">Revenue ($, left)</text>'
              f'<circle cx="{pl+138}" cy="19" r="4" fill="var(--gd)"/>'
              f'<text x="{pl+147}" y="23" fill="var(--mut)" font-size="11">Adj EPS ($, right)</text>'
              + growth_note)
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="Revenue bars and EPS line by quarter" '
            'style="display:block">%s%s%s%s%s%s%s</svg>'
            % (W, H, "".join(grid), zero, "".join(bars), line, "".join(dots), "".join(labs), legend))


def _fin_takeaway(series):
    """How revenue / EPS / op-margin have moved YoY (4 quarters) and sequentially."""
    s = [x for x in series if x.get("revenue") is not None]
    if len(s) < 2:
        return ""
    cur, prev = s[-1], s[-2]
    yago = s[-5] if len(s) >= 5 else None

    def pct(a, b):
        return None if not b else round((a / b - 1) * 100, 1)

    def span(v, suf, good_up=True):
        cls = ("up" if v >= 0 else "dn") if good_up else ("dn" if v >= 0 else "up")
        return f'<span class="{cls}">{v:+.1f}{suf}</span>'

    parts = []
    if yago and yago.get("revenue"):
        y = pct(cur["revenue"], yago["revenue"])
        if y is not None:
            parts.append(f'rev {span(y, "% YoY")}')
    q = pct(cur["revenue"], prev["revenue"])
    if q is not None:
        parts.append(f'<span class="dim">{q:+.1f}% QoQ</span>')
    if cur.get("eps") is not None and yago and yago.get("eps") is not None:
        e = round(cur["eps"] - yago["eps"], 2)
        parts.append(f'EPS <span class="{"up" if e >= 0 else "dn"}">{e:+.2f} YoY</span>')
    if cur.get("op_margin") is not None and yago and yago.get("op_margin") is not None:
        m = round(cur["op_margin"] - yago["op_margin"], 1)
        parts.append(f'op margin <span class="{"up" if m >= 0 else "dn"}">{m:+.1f}pp YoY</span>')
    return "  ·  ".join(parts)


# --------------------------------------------------------------------------
# Page chrome
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#0d1014;--surf:#14181e;--surf2:#1b212a;--bd:#262d37;--bd2:#36404d;
--tx:#e8ebf1;--mut:#929aa6;--dim:#646c78;--ac:#5b9bff;--gd:#41d18f;--rd:#f2616b;
--am:#f3b14e;--pu:#b08bff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.mono,code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.top{position:sticky;top:0;z-index:9;display:flex;align-items:center;gap:14px;
background:var(--surf);border-bottom:1px solid var(--bd);padding:9px 16px}
.top .bd{font-weight:600;font-size:14px;white-space:nowrap}
.top .bd span{color:var(--dim);font-weight:400}
#omni{flex:1;max-width:520px;background:var(--bg);border:1px solid var(--bd2);color:var(--tx);
border-radius:8px;padding:8px 12px;font-size:13px}
#omni:focus{outline:none;border-color:var(--ac)}
.top .lnk{color:var(--mut);font-size:12px;white-space:nowrap}
.top .lnk:hover{color:var(--tx);text-decoration:none}
.kbd{border:1px solid var(--bd2);border-radius:4px;padding:0 5px;color:var(--dim);font-size:11px}
.wrap{display:flex;align-items:flex-start}
.side{width:188px;flex:0 0 188px;border-right:1px solid var(--bd);padding:12px 8px;
position:sticky;top:49px;height:calc(100vh - 49px);overflow:auto}
.side .h{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.6px;margin:12px 8px 4px}
.side a{display:block;padding:5px 9px;border-radius:6px;color:var(--tx);font-size:12.5px}
.side a:hover{background:var(--surf2);text-decoration:none}.side a.on{background:var(--surf2);color:var(--ac)}
.side .tk{font-family:ui-monospace,monospace}
.side .gh{display:flex;justify-content:space-between;align-items:center;color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin:11px 8px 3px;padding-top:7px;border-top:1px solid var(--bd)}
.side .gh .ghn{color:var(--dim);font-size:10px}
.main{flex:1;min-width:0;padding:18px 22px 70px}
h1{font-size:19px;font-weight:600;margin:0 0 2px}h2{font-size:14px;font-weight:600;margin:0}
.sub{color:var(--mut);font-size:12px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.col1{grid-column:1/-1}
.panel{background:var(--surf);border:1px solid var(--bd);border-radius:9px;overflow:hidden}
.panel>.ph{display:flex;align-items:center;gap:8px;padding:9px 13px;border-bottom:1px solid var(--bd)}
.panel>.ph .src{margin-left:auto;font-size:11px}.panel>.ph .src a{color:var(--pu)}
.panel>.pb{padding:11px 13px}
.stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:9px}
.stat .b{background:var(--surf2);border-radius:7px;padding:8px 10px}
.stat .b .l{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.4px}
.stat .b .v{font-size:18px;font-weight:600;margin-top:3px;font-family:ui-monospace,monospace}
.stat .b .s{color:var(--dim);font-size:11px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;cursor:pointer;white-space:nowrap}
th:hover{color:var(--tx)}tbody tr:hover{background:var(--surf)}
td.k{color:var(--mut);white-space:nowrap;width:1%;padding-right:16px}
td.num,.num{font-family:ui-monospace,monospace;text-align:right}
.up{color:var(--gd)}.dn{color:var(--rd)}.warnc{color:var(--am)}
.muted{color:var(--mut)}.dim{color:var(--dim)}
.pill{display:inline-block;padding:2px 9px;border-radius:5px;font-size:11.5px;font-weight:500;background:var(--surf2);border:1px solid var(--bd)}
.pill.g{color:var(--gd);border-color:#1e5b41}.pill.r{color:var(--rd);border-color:#5b2126}.pill.a{color:var(--am)}
.chip{display:inline-block;background:var(--surf2);border:1px solid var(--bd);border-radius:5px;padding:1px 7px;margin:0 5px 5px 0;font-size:11.5px;color:var(--mut)}
.chip b{color:var(--tx);font-weight:500}.chip.up b{color:var(--gd)}.chip.dn b{color:var(--rd)}.chip.ok b{color:var(--gd)}
.tag{display:inline-block;background:var(--surf2);border:1px solid var(--bd);color:var(--mut);border-radius:5px;padding:1px 7px;margin:0 4px 4px 0;font-size:11px}
pre.prose{white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;color:var(--tx);margin:4px 0;max-height:420px;overflow:auto}
.tsec{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--ac);font-weight:600;margin:16px 0 9px;padding-bottom:4px;border-bottom:1px solid var(--bd)}
.tturn{margin:0 0 12px}
.tspk{font-weight:600;color:var(--tx);font-size:12.5px}
.tttl{color:var(--dim);font-size:12px}
.ttext{color:var(--mut);margin-top:3px;white-space:pre-wrap;line-height:1.6}
.pchart{margin-bottom:12px}
.pchdr{display:flex;align-items:baseline;gap:9px;margin-bottom:7px;min-height:24px}
.pcprice{font-size:21px;font-weight:600;font-family:ui-monospace,monospace}
.pcchg{font-size:13px;font-weight:600}.pcchg.up{color:var(--gd)}.pcchg.dn{color:var(--rd)}
.pcdate{color:var(--dim);font-size:12px}
.pcbtns{display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap}
.pcb{background:var(--surf2);border:1px solid var(--bd);color:var(--mut);border-radius:6px;padding:3px 11px;font-size:11.5px;cursor:pointer}
.pcb:hover{color:var(--tx)}.pcb.on{background:var(--ac);border-color:var(--ac);color:#0d1014;font-weight:600}
.pcwrap{position:relative}
.pcsvg{width:100%;height:auto;display:block;cursor:crosshair;touch-action:none}
.pctip{position:absolute;top:4px;background:var(--surf2);border:1px solid var(--bd2);border-radius:6px;padding:4px 9px;font-size:12px;color:var(--tx);pointer-events:none;display:none;white-space:nowrap;z-index:5;font-family:ui-monospace,monospace}
.pctip .pctd{color:var(--dim);font-size:11px}
.livedot{color:var(--gd);font-size:8px;vertical-align:1px;animation:livep 2s ease-in-out infinite}
@keyframes livep{0%,100%{opacity:.3}50%{opacity:1}}
pre.j{background:var(--bg);border:1px solid var(--bd);border-radius:7px;padding:11px;overflow:auto;max-height:440px;white-space:pre-wrap;word-break:break-word;font-size:11.5px}
.subcard{background:var(--surf2);border:1px solid var(--bd);border-radius:7px;padding:8px 10px;margin:7px 0}
ul.lst{margin:5px 0;padding-left:17px}ul.lst li{margin:2px 0}
.row{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.btn{background:var(--surf);border:1px solid var(--bd2);color:var(--tx);border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer}
.btn:hover{background:var(--surf2);text-decoration:none}.btn.on{border-color:var(--ac);color:var(--ac)}
details>summary{cursor:pointer;color:var(--mut);font-size:12px;padding:4px 0;list-style:none}
details>summary::-webkit-details-marker{display:none}
.gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:10px}
.gcard{background:var(--surf);border:1px solid var(--bd);border-radius:8px;padding:10px 11px}
.gcard .t{font-weight:600;font-family:ui-monospace,monospace}
.empty{color:var(--dim);font-style:italic}
.cov{height:6px;background:var(--surf2);border-radius:4px;overflow:hidden}.cov>i{display:block;height:100%;background:var(--ac)}
.emx td.k{font-weight:500;color:var(--tx)}
.ec{cursor:pointer}.ec:hover{background:var(--surf2);color:var(--ac)}
.edet{margin-top:11px;background:var(--surf2);border:1px solid var(--bd);border-radius:8px;padding:10px 13px;font-size:12.5px;color:var(--mut);min-height:42px}
.edet .eh{font-weight:600;color:var(--tx);margin-bottom:5px}
.erow{display:flex;justify-content:space-between;gap:14px;padding:2px 0}.erow span:first-child{color:var(--mut)}
.tkbox{background:var(--surf2);border:1px solid var(--bd2);border-radius:8px;padding:11px 13px;margin-bottom:12px;font-size:13px}
.tklbl{display:inline-block;background:var(--ac);color:#06101f;font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;padding:1px 7px;border-radius:5px;margin-right:6px}
.hbox{margin:2px 0 4px}
.hb{display:flex;align-items:center;gap:8px;padding:2px 0}
.hbl{width:165px;font-size:12px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hbar{flex:1;background:var(--surf2);border-radius:3px;height:13px}
.hbar>i{display:block;height:13px;border-radius:3px}
.hbv{width:84px;text-align:right;font-family:ui-monospace,monospace;font-size:12px}
.pctbar{display:flex;align-items:center;gap:7px}
.pctbar .pt{width:84px;height:10px;background:var(--surf2);border-radius:3px;overflow:hidden;flex:0 0 auto}
.pctbar .pt>i{display:block;height:10px}
.pctbar b{font-family:ui-monospace,monospace;font-size:11.5px;font-weight:500}
.tape{overflow:hidden;white-space:nowrap;background:var(--surf);border-bottom:1px solid var(--bd)}
.tape .scroll{display:inline-block;animation:tape 90s linear infinite}
.tape:hover .scroll{animation-play-state:paused}
.tape .tk-i{display:inline-block;padding:6px 14px;font-size:12px;color:var(--tx);border-right:1px solid var(--bd)}
.tape .tk-i:hover{background:var(--surf2);text-decoration:none}
@keyframes tape{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.nf{margin-top:4px}
.nf-i{display:flex;gap:9px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--bd);font-size:13px}
.nf-i:hover{background:var(--surf)}
.nf-d{color:var(--dim);font-size:11px;font-family:ui-monospace,monospace;width:38px;flex:0 0 auto}
.nf-h{flex:1;line-height:1.45}
.nf-yr{position:sticky;top:49px;background:var(--surf);z-index:2;color:var(--ac);font-size:12px;font-weight:600;font-family:ui-monospace,monospace;letter-spacing:.5px;padding:7px 0 4px;margin-top:6px;border-bottom:1px solid var(--bd2)}
.nf-f{font-size:11px;padding:3px 9px}.nf-f.on{border-color:var(--ac);color:var(--ac)}
.ctabs{display:flex;gap:2px;border-bottom:1px solid var(--bd);margin:0 0 16px;flex-wrap:wrap}
.ct{padding:7px 14px;font-size:13px;color:var(--mut);border-bottom:2px solid transparent;margin-bottom:-1px}
.ct:hover{color:var(--tx);text-decoration:none}.ct.on{color:var(--ac);border-bottom-color:var(--ac)}
"""

JS_TMPL = """
var TICKERS=%s, FUNCS=%s;
function go(v){v=(v||'').trim();if(!v)return;var up=v.toUpperCase();
if(TICKERS.indexOf(up)>-1){location='/co/'+encodeURIComponent(up);return;}
var f=FUNCS.find(function(x){return x.toLowerCase()===v.toLowerCase()});if(f){location='/fn/'+f;return;}
var pt=TICKERS.find(function(t){return t.indexOf(up)===0});if(pt){location='/co/'+encodeURIComponent(pt);return;}
var pf=FUNCS.find(function(x){return x.toLowerCase().indexOf(v.toLowerCase())===0});if(pf){location='/fn/'+pf;return;}
if(TICKERS.length)location='/co/'+encodeURIComponent(up);}
function omkey(e){if(e.key==='Enter')go(e.target.value);}
document.addEventListener('keydown',function(e){if(e.key==='/'&&['INPUT','TEXTAREA'].indexOf(document.activeElement.tagName)<0){e.preventDefault();var o=document.getElementById('omni');if(o)o.focus();}});
function sortTable(t,i,th){var tb=t.tBodies[0],rows=[].slice.call(tb.rows),asc=th.dataset.asc!=='1';
[].forEach.call(t.tHead.rows[0].cells,function(c){c.dataset.asc=''});th.dataset.asc=asc?'1':'0';
rows.sort(function(a,b){var x=a.cells[i].dataset.v||a.cells[i].innerText,y=b.cells[i].dataset.v||b.cells[i].innerText,
nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)){x=nx;y=ny}else{x=(''+x).toLowerCase();y=(''+y).toLowerCase()}
return(x<y?-1:x>y?1:0)*(asc?1:-1)});rows.forEach(function(r){tb.appendChild(r)});}
function ffilter(v){v=v.toLowerCase();var seen={};
[].forEach.call(document.querySelectorAll('.side .tk'),function(a){var show=a.dataset.t.indexOf(v)>-1;a.style.display=show?'':'none';if(show)seen[a.dataset.sec]=1;});
[].forEach.call(document.querySelectorAll('.side .gh'),function(g){g.style.display=(!v||seen[g.dataset.sec])?'':'none';});}
function nfilter(b,s){[].forEach.call(document.querySelectorAll('.nf-f'),function(x){x.classList.remove('on')});b.classList.add('on');[].forEach.call(document.querySelectorAll('.nf-i'),function(i){i.style.display=(!s||i.dataset.s===s)?'':'none'})}
function qatoggle(m){var q=document.getElementById('q-tbl'),a=document.getElementById('a-tbl');if(q)q.style.display=m=='q'?'':'none';if(a)a.style.display=m=='a'?'':'none';var bq=document.getElementById('qa-q'),ba=document.getElementById('qa-a');if(bq)bq.classList.toggle('on',m=='q');if(ba)ba.classList.toggle('on',m=='a');var e=document.getElementById('estscroll-'+m);if(e)e.scrollLeft=e.scrollWidth;}
(function(){
 function applyLive(q){
  for(var t in q){ var d=q[t];
   document.querySelectorAll('.tk-i[data-t="'+t+'"]').forEach(function(a){
     var px=a.querySelector('.tkpx'),ch=a.querySelector('.tkch');
     if(px&&d.price!=null)px.textContent=d.price.toFixed(2);
     if(ch&&d.change_pct!=null){var up=d.change_pct>=0;ch.textContent=(up?'+':'')+d.change_pct.toFixed(2)+'%%';ch.className='tkch '+(up?'up':'dn');}
   });
  }
  document.querySelectorAll('[data-live]').forEach(function(el){var t=el.getAttribute('data-live');if(q[t]&&q[t].price!=null)el.textContent='$'+q[t].price.toFixed(2);});
  var lt=window.__LIVE_T;
  if(lt&&q[lt]&&window.__pcLive)window.__pcLive(q[lt].price,q[lt].change_pct);
 }
 function poll(){fetch('/api/quotes').then(function(r){return r.json();}).then(applyLive).catch(function(){});}
 poll(); setInterval(poll,15000);
 document.addEventListener('visibilitychange',function(){if(!document.hidden)poll();});
})();
"""


def tape_html():
    q = _safe_load(os.path.join(DATA, "quotes.json")) or {}
    quotes = q.get("quotes") or {}
    if not quotes:
        return ""
    items = ""
    for t in sorted(quotes):
        c = quotes[t].get("change_pct")
        px = quotes[t].get("price")
        ok = isinstance(c, (int, float))
        cls = "up" if (ok and c >= 0) else "dn"
        chg = (f"{'+' if c >= 0 else ''}{c:.2f}%") if ok else "-"
        items += (f'<a class="tk-i" data-t="{esc(t)}" href="/co/{urllib.parse.quote(t)}"><b>{esc(t)}</b> '
                  f'<span class="mono tkpx">{num(px, d=2)}</span> <span class="tkch {cls}">{chg}</span></a>')
    return f'<div class="tape"><div class="scroll">{items}{items}</div></div>'


def parse_news(corpus):
    items = []
    if not corpus:
        return items
    for blk in re.split(r"\n\s*[•·]\s*", corpus)[1:]:
        lines = [l.strip() for l in blk.splitlines() if l.strip()]
        if not lines:
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s+\[([^\]]+)\]\s+\[([^\]]+)\]", lines[0])
        if not m:
            continue
        rest = lines[1:]
        url = next((l for l in rest if l.startswith("http")), "")
        textlines = [l for l in rest if not l.startswith("http")]
        items.append({
            "date": m.group(1), "source": m.group(2).strip(), "sentiment": m.group(3).strip().lower(),
            "headline": textlines[0] if textlines else "",
            "desc": " ".join(textlines[1:])[:280] if len(textlines) > 1 else "",
            "url": url,
        })
    return items


def _news_items(raw):
    """News items for display — prefer the structured `display_items` (the fuller
    feed: paid feeds + Google News + HN) when present, else parse the bounded
    brief corpus_text (older caches)."""
    di = raw.get("display_items") if isinstance(raw, dict) else None
    if di:
        return [{
            "date": i.get("published", ""), "source": i.get("source", ""),
            "sentiment": (i.get("sentiment_label") or "").lower(),
            "headline": i.get("title", ""), "desc": (i.get("summary") or "")[:280],
            "url": i.get("url", ""), "via": i.get("via", ""),
        } for i in di]
    return parse_news(raw.get("corpus_text", "")) if isinstance(raw, dict) else []


# --- Press-tab classification ------------------------------------------------
# Split the feed two ways, deterministically (no LLM, so it's free per render):
#   category : 'company' (an issuer press release — the stuff on their IR page)
#              vs 'article' (third-party coverage)
#   noise    : low-signal churn the "Important" filter hides — 13F ownership
#              shuffles, law-firm solicitations, analyst-rating churn, algo/
#              listicle filler. (The upstream news "material" tag is no help
#              here — it flags exactly this churn as material.)
_PR_WIRES = ("pr newswire", "prnewswire", "business wire", "businesswire",
             "globenewswire", "globe newswire", "accesswire", "access newswire",
             "newsfile", "eqs news", "eqs group")
_PR_ACTIONS = ("reports", "announces", "declares", "provides", "posts", "to present",
               "to host", "to report", "to participate", "names", "appoints",
               "completes", "launches", "receives", "issues", "updates", "schedules",
               "releases", "unveils", "introduces", "expands", "signs", "enters",
               "awarded", "authorizes", "prices", "commences", "approves", "grants",
               "to acquire", "raises", "sets")


def _prx(p):
    return re.compile(p, re.I)


# Law-firm solicitations and listicle/technical filler always demote, even on
# an issuer-led headline. Ownership (13F) and insider patterns are checked
# AFTER the issuer test, so a real "Announces Offering of N Shares" release
# isn't mistaken for share-shuffle churn.
_PR_LEGAL = _prx(
    r"rosen law|levi & korsinsky|levi and korsinsky|pomerantz|bronstein|gross law|"
    r"glancy|kahn swick|robbins geller|robbins llp|schall law|kessler topaz|faruqi|"
    r"bragar eagel|johnson fistel|hagens berman|kirby mcinerney|portnoy law|"
    r"block & leviton|shareholder (alert|rights|investigation)|class action|"
    r"securities (fraud|class action)|investors?\s+to\s+(inquire|contact)|"
    r"encourages\s+[a-z .,&]*investors|reminds\s+[a-z .,&]*investors|"
    r"investigat(es|ion|ing)\b|important deadline|lead plaintiff|deadline reminder|"
    r"\blaw firm\b|notice to (shareholders|investors)")
_PR_FILLER = _prx(
    r"here'?s why|what you need to know|\b\d+ (stocks?|reasons?|things?|charts?)\b|"
    r"is it (a )?(buy|sell|time)|should you (buy|sell|own|invest)|moving average|"
    r"\brsi\b|technical (analysis|indicator)|stochastic|\bhow to (buy|trade)\b|"
    r"\b52[- ]week (high|low)|gap (up|down)|\bbreakout\b|crosses (above|below)")
# Insider Form-4 / RSU / 10b5-1 churn (the Ownership tab already covers this).
_PR_INSIDER = _prx(
    r"\bform 4\b|\binsider (trading|transaction|buying|selling|sells|buys|activity)\b"
    r"|\b10b5[- ]?1\b|\bvested (rsus?|shares|units|options)\b"
    r"|\b(director|ceo|cfo|coo|cto|cmo|cao|chief \w+ officer|president|officer|evp|svp|"
    r"chairman|founder|insider)\b"
    r"[^.]{0,30}\b(sells?|sold|buys?|bought|acquires?|acquired|receives?|received|"
    r"disposes?|disposed|exercises?|exercised|gifts?|gifted)\b"
    r"|\b(sells?|sold|buys?|bought)\b[^.]{0,20}\b(rsus?|vested|10b5)")
# Institutional 13F position shuffles — verb + (shares|stake|position|%) nearby,
# or an explicit share count.
_PR_OWN = _prx(
    r"\b\d[\d.,]*\s*[mkb]?\s+shares\b"
    r"|\bshares?\s+(sold|bought|purchased|acquired|owned|held)\s+by\b"
    r"|\bnew (stake|position)\b"
    r"|\b(holds?|holding|owns?|owned|boosts?|boosted|trims?|trimmed|raises?|raised|"
    r"lowers?|lowered|cuts?|reduces?|reduced|lifts?|lifted|grows?|grew|increases?|"
    r"increased|decreases?|decreased|pares?|pared|offloads?|dumps?|sells?|sold|buys?|"
    r"bought|acquires?|acquired|purchases?|purchased|takes?\s+a)\b[^.]{0,22}"
    r"\b(shares?|stake|position|holdings?)\b"
    r"|\b[\d.]+%\s+(of|stake)\b|\bshort interest\b|\b13[- ]?f\b"
    r"|\binstitutional (investors?|ownership|holdings)\b"
    r"|\bhedge fund[^.]{0,25}(buy|sell|stake|position|holding)")
# Analyst rating churn — a rating verb within range of a rating word/PT.
_PR_RATING = _prx(
    r"\b(maintains?|initiates?|assumes?|starts?|resumes?|reiterat\w*|reaffirm\w*|"
    r"raises?|raised|lowers?|lowered|cuts?|boosts?|trims?|sets?|lifts?|hikes?|"
    r"upgrad\w*|downgrad\w*|begins?)\b[^.]{0,30}"
    r"\b(overweight|underweight|equal[- ]?weight|outperform|underperform|market perform|"
    r"price target|\bpt\b|rating|\$\d)\b"
    r"|\b(buy|sell|hold|neutral|moderate buy|strong buy) rating\b|average rating of|"
    r"consensus (rating|price target)|\$[\d.]+ price target|price target of \$|"
    r"coverage (initiated|started|resumed)|\b(upgrad|downgrad)(ed|es|ing)?\s+(at|by|to)\b")


def classify_press(item, company_name=""):
    """Return (category, is_noise, tag) for one news/press item.

    category: 'company' (issuer release) | 'article' (third-party)
    is_noise: True for low-signal churn the Important filter hides
    tag:      'RELEASE' | '13F' | 'INSIDER' | 'LEGAL' | 'RATING' | 'FILLER' | ''

    Order matters: legal/filler and insider churn demote even a name-led
    headline; the issuer-release test runs next; ownership/rating churn is
    judged last so genuine offering/buyback releases survive.
    """
    src = (item.get("source") or "").strip().lower()
    head = item.get("headline") or ""
    hl = head.lower()
    text = head + " " + (item.get("desc") or "")

    if _PR_LEGAL.search(text):
        return "article", True, "LEGAL"
    if _PR_FILLER.search(text):
        return "article", True, "FILLER"
    if _PR_INSIDER.search(text):
        return "article", True, "INSIDER"

    toks = [t for t in re.split(r"[ ,.]+", (company_name or "").strip()) if t]
    name0 = toks[0].lower() if toks else ""
    if name0 in ("the", "a") and len(toks) > 1:
        name0 = toks[1].lower()
    name_led = len(name0) > 2 and hl.startswith(name0)
    is_wire = src in ("ir", "company ir", "company") or any(w in src for w in _PR_WIRES)
    if name_led and (is_wire or any(v in hl for v in _PR_ACTIONS)):
        return "company", False, "RELEASE"

    if _PR_OWN.search(text):
        return "article", True, "13F"
    if _PR_RATING.search(text):
        return "article", True, "RATING"
    return "article", False, ""


_PR_BOILER = _prx(r"^(ex[-\s]?99\.?1?|exhibit\s*99\.1|\d+|[\w.\-]+\.html?|page\s*\d+|"
                  r"unassociated document|table of contents|false|0+|-+|_+)$")
# Sub-headers / dek lines that sit ABOVE the real title and must be skipped.
_PR_SKIP = _prx(r"^(press release|news release|for immediate release|media contact|"
                r"investors?\s+contact|investor relations|forward[- ]looking|safe harbor|"
                r"source:|contact:|\(?in (thousands|millions)|unaudited|"
                r"consolidated (balance|statements|financial)|condensed consolidated|notes to|"
                r"table of contents|independent auditor|report of independent|page no|"
                r"balance sheets?|statements? of (operations|cash|income|stockholders)|"
                r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                r"(january|february|march|april|may|june|july|august|september|october|"
                r"november|december)\s+\d{1,2},?\s+\d{4})")
# Reporting/announcing verbs that mark a genuine release title.
_PR_TITLE_HINT = _prx(r"\b(reports?|announces?|provides?|declares?|posts?|results|completes?|"
                      r"appoints?|prices?|increase|dividend|launches?|receives?|expands?|named|"
                      r"raises?|reaffirms?|updates?|delivers?|achieves?|closes?|enters?|"
                      r"to (report|host|present)|grand opening|breaks ground)\b")
# Body text that means the exhibit is a financial-statement / auditor doc, not
# a press release — used to reject those so they don't render as junk.
_PR_NOT_RELEASE = _prx(r"independent auditor|report of independent|table of contents\s+page|"
                       r"notes to (the )?consolidated|consolidated balance sheets?|"
                       r"consolidated statements? of (operations|cash flows|income)")


def _pr_headline(text):
    """Pull the real headline out of an 8-K Ex 99.1 release body. The loader
    stores no title, and the first substantial line is often a 'PRESS RELEASE,
    DATED …' dek — so collect candidate lines, prefer one that reads like a
    release title (reporting verb), and join a wrapped continuation line."""
    cands = []
    for ln in (text or "").splitlines()[:120]:
        s = ln.strip()
        if len(s) < 12 or _PR_BOILER.match(s) or _PR_SKIP.match(s):
            continue
        if len(s.split()) < 3 or not re.search(r"[A-Za-z]", s):
            continue
        cands.append(s[:170])
        if len(cands) >= 25:
            break
    pick, idx = None, -1
    for i, s in enumerate(cands):
        if _PR_TITLE_HINT.search(s):
            pick, idx = s, i
            break
    if pick is None:
        return cands[0] if cands else ""
    # Join a wrapped title (e.g. 'Primo Brands Reports 2026' + 'First Quarter
    # Results') — only when the next line is a short Title-Case fragment, not a
    # dateline or body sentence.
    if len(pick.split()) < 6 and idx + 1 < len(cands):
        nxt = cands[idx + 1]
        words = nxt.split()
        titleish = words and sum(1 for w in words if w[:1].isupper()) >= len(words) - 1
        if (len(words) <= 5 and titleish and not re.search(r"\d", nxt)
                and " - " not in nxt and "—" not in nxt):
            pick = f"{pick} {nxt}".strip()
    return pick[:170]


def _pr_is_real(headline, text):
    """Reject 8-K Ex 99.1 exhibits that aren't actually press releases —
    acquisition financial statements, auditor reports, or binary/mojibake."""
    if not headline:
        return False
    junk = sum(1 for c in headline if ord(c) > 0x2000 or ord(c) < 9)
    if junk > len(headline) * 0.12:
        return False
    if _PR_NOT_RELEASE.search((text or "")[:900].lower()) and not _PR_TITLE_HINT.search(headline):
        return False
    return True


def _pr_supp_headline(text):
    """Headline for an Ex 99.2/99.3 supplement. These are slide-style HTML with
    no clean title line, so synthesize one from the 'Supplemental Information …
    FY20XX' marker or a quarter phrase."""
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(r"supplemental\s+information[^|]{0,45}?(?:quarter|fy|fiscal)[^|]{0,12}\d{4}",
                  t, re.I)
    if m:
        return m.group(0).strip()[:120]
    q = (re.search(r"(first|second|third|fourth)\s+quarter(\s+(fy\s*)?\d{4})?", t, re.I)
         or re.search(r"\bQ[1-4]\b(\s*(fy\s*)?\d{4})?", t))
    return ("Earnings supplement — " + q.group(0).strip()) if q else "Earnings supplement"


def home_news(limit=70):
    out, seen = [], set()
    for t in all_tickers():
        steps = cache_steps(t)
        if "news" not in steps:
            continue
        raw = (_safe_load(steps["news"][0]) or {}).get("output") or {}
        d, _ = load_result(t)
        schema = (d or {}).get("schema", "")
        for it in _news_items(raw):
            key = (it["headline"] or "")[:60].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            it["ticker"] = t
            it["sector"] = schema
            out.append(it)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:limit]


_TKR_SECTORS = None
_SECTOR_LABEL = {
    "Communication Services": "Communication", "Financial Services": "Financials",
    "Basic Materials": "Materials", "Consumer Cyclical": "Consumer Cyclical",
    "Consumer Defensive": "Consumer Defensive",
}

# Curated thematic sub-groups for the sidebar (finer than yfinance sector).
# Anything not listed falls back to its yfinance sector, then "Other".
_THEME = {
    # Technology
    "MSFT": "Hyperscalers", "GOOG": "Hyperscalers", "GOOGL": "Hyperscalers", "AMZN": "Hyperscalers",
    "NOW": "Software", "PLTR": "Software", "SHOP": "Software", "UBER": "Software",
    "BAND": "Software", "MSTR": "Software",
    "NVDA": "AI semis & hardware", "SMCI": "AI semis & hardware", "INTC": "AI semis & hardware",
    "MU": "Memory", "WDC": "Memory", "STX": "Memory",
    "AAOI": "Photonics / optical", "AXTI": "Photonics / optical", "GLW": "Photonics / optical",
    "AAPL": "Consumer hardware", "SONY": "Consumer hardware",
    # Communication services
    "RDDT": "Internet & social", "SPOT": "Internet & social",
    "APP": "Adtech",
    "EA": "Gaming", "TTWO": "Gaming",
    "SRAD": "Sports & betting", "GENI": "Sports & betting", "DKNG": "Sports & betting", "FLUT": "Sports & betting",
    "DIS": "Media & entertainment", "FOXA": "Media & entertainment", "LYV": "Media & entertainment",
    "PSKY": "Media & entertainment", "WBD": "Media & entertainment", "WMG": "Media & entertainment",
    # Consumer cyclical
    "CMG": "Restaurants", "DPZ": "Restaurants", "TXRH": "Restaurants", "WING": "Restaurants",
    "SBUX": "Restaurants", "EAT": "Restaurants", "KRUS": "Restaurants",
    "JBFCY": "Restaurants",
    "RIVN": "Consumer — other", "SBH": "Consumer — other", "GIII": "Consumer — other",
    "DKS": "Consumer — other",
    # Consumer defensive
    "COST": "Consumer staples", "ELF": "Consumer staples", "PRMB": "Consumer staples",
    "MNST": "Beverages", "CELH": "Beverages", "FMX": "Beverages",
    # Industrials / health / financials / materials
    "AXON": "Aerospace & defense", "RKLB": "Aerospace & defense",
    "VRSK": "Data & analytics",
    "TMDX": "Medtech", "JPM": "Financials", "ORLA": "Gold & mining",
}
_THEME_ORDER = [
    "Hyperscalers", "Software", "AI semis & hardware", "Memory", "Photonics / optical", "Consumer hardware",
    "Internet & social", "Adtech", "Gaming", "Sports & betting", "Media & entertainment",
    "Restaurants", "Consumer — other", "Consumer staples", "Beverages",
    "Aerospace & defense", "Data & analytics", "Medtech", "Financials", "Gold & mining",
]


def _ticker_sectors():
    """ticker -> sector (from valuation_snapshot.json). Refresh that with
    `python refresh_valuation.py` to pick up sectors for new names."""
    global _TKR_SECTORS
    if _TKR_SECTORS is None:
        vs = (_safe_load(os.path.join(DATA, "valuation_snapshot.json")) or {}).get("valuations") or {}
        _TKR_SECTORS = {t.upper(): (v.get("sector") or "") for t, v in vs.items()}
    return _TKR_SECTORS


def _ticker_theme(t):
    """Curated thematic group, else the yfinance sector, else 'Other'."""
    t = t.upper()
    if t in _THEME:
        return _THEME[t]
    sec = _ticker_sectors().get(t)
    return _SECTOR_LABEL.get(sec, sec) if sec else "Other"


def layout(title, body, active=""):
    tickers = all_tickers()
    side = ['<div class="h">Views</div>',
            '<a href="/" class="%s">Screener</a>' % ("on" if active == "home" else ""),
            '<a href="/fn" class="%s">Functions</a>' % ("on" if active == "fn" else ""),
            '<a href="/compare" class="%s">Compare</a>' % ("on" if active == "compare" else ""),
            '<a href="/research" class="%s">Research</a>' % ("on" if active == "research" else ""),
            '<a href="/macro" class="%s">Macro</a>' % ("on" if active == "macro" else ""),
            '<div class="h">Tickers (%d)</div>' % len(tickers),
            '<input class="btn" style="width:100%;margin-bottom:5px" placeholder="filter…" oninput="ffilter(this.value)">']
    # Group the ticker list by curated theme (finer than sector), in a fixed
    # order, then any sector-fallback groups alphabetically, "Other" last.
    from collections import defaultdict
    groups = defaultdict(list)
    for t in tickers:
        groups[_ticker_theme(t)].append(t)
    order = ([th for th in _THEME_ORDER if th in groups]
             + sorted(th for th in groups if th not in _THEME_ORDER and th != "Other")
             + (["Other"] if "Other" in groups else []))
    for th in order:
        slug = re.sub(r"[^a-z0-9]+", "-", th.lower()).strip("-")
        side.append('<div class="gh" data-sec="%s">%s <span class="ghn">%d</span></div>'
                    % (slug, esc(th), len(groups[th])))
        for t in sorted(groups[th]):
            cls = "tk on" if active == t else "tk"
            side.append('<a class="%s" data-t="%s" data-sec="%s" href="/co/%s">%s</a>'
                        % (cls, esc(t.lower()), slug, urllib.parse.quote(t), esc(t)))
    js = JS_TMPL % (json.dumps(tickers), json.dumps([f[0] for f in FUNCTIONS]))
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s · workbench</title>
<style>%s</style></head><body>
<div class="top"><div class="bd">Workbench<span> / terminal</span></div>
<input id="omni" placeholder="search ticker or function…  ( / )" onkeydown="omkey(event)" autocomplete="off">
<a class="lnk" href="/">home</a><a class="lnk" href="/fn">functions</a>
<span class="lnk"><span class="kbd">/</span> search</span></div>
%s
<div class="wrap"><div class="side">%s</div><div class="main">%s</div></div>
<script>%s</script></body></html>""" % (esc(title), CSS, tape_html(), "".join(side), body, js)


def panel(title, body, fn_key=None, ticker=None, full=False):
    src = ""
    if fn_key and fn_key in FN_BY_KEY:
        f = FN_BY_KEY[fn_key]
        href = "/fn/%s" % fn_key + (("?ticker=" + urllib.parse.quote(ticker)) if ticker else "")
        src = '<span class="src"><a href="%s">%s ↗</a></span>' % (href, esc(f[3]))
    return '<div class="panel%s"><div class="ph"><h2>%s</h2>%s</div><div class="pb">%s</div></div>' % (
        " col1" if full else "", esc(title), src, body)

# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def home_page(q=""):
    results = list_results()
    rows = []
    for t in sorted(results.keys()):
        d, stamp = load_result(t)
        d = d or {}
        ea, val = d.get("edge_assessment") or {}, d.get("valuation") or {}
        up = val.get("upside_pct")
        upc = "up" if isinstance(up, (int, float)) and up > 0 else ("dn" if isinstance(up, (int, float)) else "")
        act = ea.get("actionability_score")
        rows.append(
            '<tr><td data-v="%s"><a href="/co/%s"><b>%s</b></a> <span class="dim">%s</span></td>'
            '<td>%s</td><td class="num" data-v="%s">%s</td>'
            '<td class="num %s" data-v="%s">%s</td><td class="num" data-v="%s">%s / %s</td>'
            '<td>%s</td><td class="dim">%s</td><td class="num">%d</td></tr>' % (
                esc(t), urllib.parse.quote(t), esc(t), esc((d.get("name") or "")[:30]),
                esc(((d.get("call") or {}).get("call") or {}).get("stance", "").replace("_", " ").upper()
                    or ea.get("verdict") or "—"),
                esc(act if act is not None else -1), num(act, d=3) if act is not None else "—",
                upc, esc(up if up is not None else -999), signed_pct(up) if up is not None else "—",
                esc(d.get("post_eps") if d.get("post_eps") is not None else -1),
                num(d.get("post_eps")), num(d.get("consensus_eps")),
                esc(d.get("decision_verdict") or "—"),
                esc(stamp.replace("_", " ")), len(results[t])))
    table = """<table id="scr"><thead><tr>
<th onclick="sortTable(scr,0,this)">Ticker</th><th onclick="sortTable(scr,1,this)">Edge</th>
<th onclick="sortTable(scr,2,this)">Action</th><th onclick="sortTable(scr,3,this)">Upside</th>
<th onclick="sortTable(scr,4,this)">Our / cons EPS</th><th onclick="sortTable(scr,5,this)">Decision</th>
<th onclick="sortTable(scr,6,this)">Latest</th><th onclick="sortTable(scr,7,this)">Runs</th></tr></thead><tbody>%s</tbody></table>""" % "".join(rows)
    nrun = sum(len(v) for v in results.values())
    stat = ('<div class="stat" style="margin-bottom:16px">'
            '<div class="b"><div class="l">Tickers</div><div class="v">%d</div></div>'
            '<div class="b"><div class="l">Runs</div><div class="v">%d</div></div>'
            '<div class="b"><div class="l">Functions</div><div class="v">%d</div></div>'
            '<div class="b"><div class="l">Reports</div><div class="v">%d</div></div>'
            '<div class="b"><div class="l">Exports</div><div class="v">%d</div></div></div>') % (
        len(results), nrun, len(FUNCTIONS),
        len(glob.glob(os.path.join(REPORTS, "*"))), len(glob.glob(os.path.join(EXPORTS, "*"))))
    feed = home_news()
    sectors = sorted({it["sector"] for it in feed if it["sector"]})
    chips = ('<button class="btn nf-f on" onclick="nfilter(this,\'\')">All</button>'
             + "".join(f'<button class="btn nf-f" onclick="nfilter(this,\'{esc(s)}\')">{esc(s)}</button>' for s in sectors))
    nrows = ""
    for it in feed:
        sc = {"bullish": "up", "bearish": "dn"}.get(it["sentiment"], "dim")
        head = f'<a href="{esc(it["url"])}">{esc(it["headline"])}</a>' if it.get("url") else esc(it["headline"])
        nrows += (f'<div class="nf-i" data-s="{esc(it["sector"])}"><span class="nf-d">{esc(it["date"][5:])}</span>'
                  f'<a class="tag" style="margin:0" href="/co/{urllib.parse.quote(it["ticker"])}">{esc(it["ticker"])}</a>'
                  f'<span class="{sc}" style="font-size:10px;text-transform:uppercase;width:34px;flex:0 0 auto">{esc(it["sentiment"][:4])}</span>'
                  f'<span class="nf-h">{head} <span class="dim" style="font-size:11px">{esc(it["source"])}</span></span></div>')
    news_block = (f'<h1>Market &amp; portfolio news</h1>'
                  f'<p class="sub">Across every name you track, newest first. Filter by sector.</p>'
                  f'<div class="row" style="margin-bottom:8px">{chips}</div><div class="nf">{nrows}</div>') if feed else '<h1>Home</h1>'
    if not results:
        table += ('<p class="muted" style="margin-top:14px">No research runs yet. Run one from the repo root, '
                  'then refresh: <code>python cli.py research COST --verbose</code></p>')
    body = news_block + '<h2 style="margin:26px 0 10px">Screener</h2>' + stat + table
    return layout("Home", body, "home")


_VAL_SNAP = None


def val_snapshot(ticker):
    """Per-ticker valuation snapshot (data/valuation_snapshot.json) — headline
    multiple + avg price target. Refresh via `python refresh_valuation.py`."""
    global _VAL_SNAP
    if _VAL_SNAP is None:
        _VAL_SNAP = (_safe_load(os.path.join(DATA, "valuation_snapshot.json")) or {}).get("valuations") or {}
    return _VAL_SNAP.get(ticker.upper()) or {}


def company_page(ticker, run=None):
    d, stamp = load_result(ticker, run)
    if d is None:
        return layout(ticker, "<h1>%s</h1><p class='sub'>No result JSON on disk.</p>" % esc(ticker), ticker)
    runs = list_results().get(ticker, [])
    ea, val = d.get("edge_assessment") or {}, d.get("valuation") or {}
    cf = d.get("consensus_full") or {}
    up = val.get("upside_pct")
    upc = "up" if isinstance(up, (int, float)) and up > 0 else ("dn" if isinstance(up, (int, float)) else "")
    dec = d.get("decision_verdict") or "—"
    verdict = ea.get("verdict") or "—"
    call_res = d.get("call") or {}
    if call_res.get("call"):
        # The call replaces the edge score and decision-gate labels (now diagnostics).
        dv = call_res.get("derived") or {}
        verdict = (call_res["call"].get("stance") or "").replace("_", " ").upper()
        dec = "EV %+.1f%% · %s conviction" % (dv.get("expected_return_pct") or 0, dv.get("conviction") or "")

    # header strip
    header = ('<h1>%s <span class="muted" style="font-size:15px;font-weight:400">%s</span></h1>'
              '<p class="sub">run %s · %d run(s) · '
              '<span class="pill %s">%s</span> '
              '<span class="pill %s">%s</span></p>') % (
        esc(ticker), esc(d.get("name") or ""), esc(stamp.replace("_", " ")), len(runs),
        "g" if verdict == "LONG" or (("PROBABLE" in str(verdict) or "EDGE" in str(verdict))
                                      and "NO_" not in str(verdict) and "NO EDGE" not in str(verdict)) else "",
        esc(verdict),
        "g" if "VALUABLE" in str(dec) and "NOT" not in str(dec) else "a" if "NOT" in str(dec) else "",
        esc(dec))

    # quote / valuation stat strip
    vs = val_snapshot(ticker)
    pt = cf.get("price_target")
    avg_tgt = vs.get("avg_price_target")
    if avg_tgt is None and isinstance(pt, dict):
        avg_tgt = pt.get("mean")
    tgt_up = vs.get("target_upside_pct")
    if tgt_up is None and isinstance(avg_tgt, (int, float)) and val.get("current_price"):
        tgt_up = round((avg_tgt / val["current_price"] - 1) * 100, 1)
    n_an = vs.get("n_analysts") or cf.get("max_analysts")
    hv, hl = vs.get("headline_multiple"), vs.get("headline_label")
    # Market cap / EV — derive mkt cap from the freshest server-side price ×
    # shares so it tracks the live tape; EV = mkt cap + net debt (net debt moves
    # only at earnings). Falls back to the yfinance snapshot value if either is
    # missing. Refresh shares/net-debt via `python refresh_valuation.py`.
    _live_px = (_LIVE.get(ticker.upper(), {}) or {}).get("price")
    _sh = vs.get("shares_out")
    if isinstance(_live_px, (int, float)) and isinstance(_sh, (int, float)) and _sh:
        mktcap = _live_px * _sh                  # live: price × shares
    else:
        mktcap = vs.get("market_cap")            # snapshot (its own price × shares — consistent)
    nd = vs.get("net_debt")
    ev_v = (mktcap + nd) if (isinstance(mktcap, (int, float)) and isinstance(nd, (int, float))) else vs.get("enterprise_value")
    qstat = (f'<div class="stat">'
             f'<div class="b"><div class="l">Price <span class="livedot" title="live">&#9679;</span></div><div class="v" data-live="{esc(ticker)}">%s</div></div>'
             '<div class="b"><div class="l">Mkt cap</div><div class="v">%s</div></div>'
             '<div class="b"><div class="l">EV</div><div class="v">%s</div></div>'
             '<div class="b"><div class="l">Implied</div><div class="v">%s</div></div>'
             '<div class="b"><div class="l">Upside</div><div class="v %s">%s</div></div>'
             '<div class="b"><div class="l">Fwd valuation</div><div class="v">%s</div><div class="s">%s</div></div>'
             '<div class="b"><div class="l">Our EPS</div><div class="v">%s</div></div>'
             '<div class="b"><div class="l">Cons EPS</div><div class="v">%s</div></div>'
             '<div class="b"><div class="l">Avg price tgt</div><div class="v">%s</div><div class="s %s">%s</div></div>'
             '<div class="b"><div class="l">Analysts</div><div class="v">%s</div><div class="s">%s</div></div></div>') % (
        num(val.get("current_price"), pre="$"),
        _big_money(mktcap), _big_money(ev_v),
        num(val.get("implied_price"), pre="$"),
        upc, signed_pct(up) if up is not None else "—",
        (num(hv, suf="x", d=1) if hv is not None else num(val.get("applied_multiple"), suf="x", d=1)),
        esc(hl or (val.get("multiple_source") or "").replace("_", " ")[:20]),
        num(d.get("post_eps")), num(d.get("consensus_eps")),
        num(avg_tgt, pre="$") if isinstance(avg_tgt, (int, float)) else "—",
        ("up" if isinstance(tgt_up, (int, float)) and tgt_up > 0 else "dn" if isinstance(tgt_up, (int, float)) else "dim"),
        (signed_pct(tgt_up) + " vs px" if isinstance(tgt_up, (int, float)) else ""),
        esc(n_an or "—"),
        esc(("rec: " + vs["recommendation"]) if vs.get("recommendation") else ""))

    steps = cache_steps(ticker)
    panels = []
    # market overlay (quote, volatility, short interest, price chart)
    if "market_overlay" in steps:
        mo = (_safe_load(steps["market_overlay"][0]) or {}).get("output") or {}
        panels.append(panel("Market overlay", render_market(mo), "market_overlay", ticker, full=True))
    # edge
    edge_body = render_value({k: ea.get(k) for k in ("verdict", "actionability_score", "priced_in",
                              "variant_pct", "variant_eps", "time_horizon", "catalysts", "edge_narrative") if k in ea})
    panels.append(panel("Edge", edge_body or '<span class="empty">no edge output</span>', "edge_detector", ticker))
    # valuation
    vmore = {}
    if vs:
        tgt_rng = (f"${vs.get('target_low')}–${vs.get('target_high')}"
                   if vs.get("target_low") and vs.get("target_high") else None)
        vmore = {k: v for k, v in {
            "Market cap": (_big_money(mktcap) if isinstance(mktcap, (int, float)) else None),
            "Enterprise value": (_big_money(ev_v) if isinstance(ev_v, (int, float)) else None),
            "Net debt": (_big_money(nd) if isinstance(nd, (int, float)) else None),
            "Headline multiple": (f"{vs.get('headline_multiple')}x  ({vs.get('headline_label')})"
                                  if vs.get("headline_multiple") is not None else None),
            "Fwd EV/EBITDA (est)": (f"{vs['fwd_ev_ebitda']}x" if vs.get("fwd_ev_ebitda") else None),
            "EV/EBITDA (TTM)": (f"{vs['ev_ebitda_ttm']}x" if vs.get("ev_ebitda_ttm") else None),
            "Fwd EV/Sales": (f"{vs['fwd_ev_sales']}x" if vs.get("fwd_ev_sales") else None),
            "Fwd P/E": (f"{vs['fwd_pe']}x" if vs.get("fwd_pe") else None),
            "Avg price target": (f"${vs['avg_price_target']}" + (f"  ({tgt_rng})" if tgt_rng else "")
                                 if vs.get("avg_price_target") else None),
            "Analyst rating": (f"{vs.get('recommendation')} · {vs.get('n_analysts')} analysts"
                               if vs.get("recommendation") else None),
            "Sector": vs.get("sector") or None,
        }.items() if v}
    val_body = render_value({k: val.get(k) for k in
                  ("implied_price", "current_price", "upside_pct", "applied_multiple", "multiple_source", "context", "narrative") if k in val})
    if vmore:
        val_body += ('<p class="muted" style="font-size:11px;margin:10px 0 4px">Market multiples '
                     '(yfinance snapshot — forward EBITDA est. from consensus revenue × TTM margin)</p>'
                     + render_value(vmore))
    panels.append(panel("Valuation", val_body or '<span class="empty">—</span>', "valuation", ticker))
    # estimates (matrix: line item x period, click a cell to drill)
    steps = cache_steps(ticker)
    qf = None
    if "quarterly_financials" in steps:
        raw = _safe_load(steps["quarterly_financials"][0]) or {}
        qf = (raw.get("output") or {}).get("corpus_text")
    panels.append(panel("Estimates vs consensus",
                        render_estimates(d, _period_labels(_parse_q_full(qf))),
                        "consensus", ticker, full=True))
    # financial trajectory chart
    series = parse_quarterly(qf)
    chart = svg_trajectory(series)
    if chart:
        take = _fin_takeaway(series)
        take_html = f'<p style="font-size:12px;margin:8px 0 2px">{take}</p>' if take else ""
        cap = '<p class="muted" style="font-size:11px;margin:4px 0 0">Bars = revenue ($M), line = EPS. Hover any quarter for values. %d quarters.</p>' % len(series)
        panels.append(panel("Financial trajectory", chart + take_html + cap, "quarterly_financials", ticker, full=True))
    # insiders — cross-ref net worth against SC 13D/G stakes (by exact filer CIK)
    if "filing_form4" in steps:
        f4 = (_safe_load(steps["filing_form4"][0]) or {}).get("output") or {}
        sc13 = {}
        if "filing_13d" in steps:
            d13o = (_safe_load(steps["filing_13d"][0]) or {}).get("output") or {}
            latest = {}
            for f13 in (d13o.get("filings") or []):
                cik13 = (f13.get("filer_cik") or "").strip()
                if cik13 and (cik13 not in latest
                              or (f13.get("filed_date") or "") > (latest[cik13].get("filed_date") or "")):
                    latest[cik13] = f13
            px13 = _live_px or val.get("current_price") or vs.get("current_price")
            for cik13, f13 in latest.items():
                sh13, pct13 = f13.get("shares_held") or 0, f13.get("pct_of_class")
                v_sh = sh13 * px13 if (px13 and sh13) else None
                v_pct = ((pct13 / 100.0) * mktcap
                         if (isinstance(pct13, (int, float)) and isinstance(mktcap, (int, float))) else None)
                cands = [v for v in (v_sh, v_pct) if isinstance(v, (int, float)) and v > 0]
                if cands:
                    # min() guards the dual-class ambiguity both ways: shares×price
                    # overstates when the shares are a junior class; pct×mktcap
                    # overstates when the pct is of a small class. The smaller of
                    # the two is the honest floor.
                    sc13[cik13] = {"value": min(cands), "shares": sh13, "pct": pct13,
                                   "filed": f13.get("filed_date"), "form": f13.get("form_type")}
        panels.append(panel("Insiders (Form 4)", render_insiders(f4, sc13), "filing_form4", ticker, full=True))
    # peer comps
    if "peer_comps" in steps:
        pc = (_safe_load(steps["peer_comps"][0]) or {}).get("output") or {}
        panels.append(panel("Peer comps", render_peers(pc), "peer_comps", ticker, full=True))
    # sum-of-the-parts comps (conglomerate segment breakout, e.g. FEMSA)
    sotp = render_sotp_comps(ticker)
    if sotp:
        panels.append(panel("Sum-of-the-parts comps", sotp, None, ticker, full=True))
    # bond health
    if "bond_health" in steps:
        bh = (_safe_load(steps["bond_health"][0]) or {}).get("output") or {}
        panels.append(panel("Bond health", render_bond_health(bh), "bond_health", ticker, full=True))
    # 13F crowding + 13D/13G (integrated)
    if "crowding_assessment" in steps:
        cr = (_safe_load(steps["crowding_assessment"][0]) or {}).get("output") or {}
        d13 = ((_safe_load(steps["filing_13d"][0]) or {}).get("output") or {}) if "filing_13d" in steps else {}
        panels.append(panel("Ownership & 13F crowding", render_crowding(cr, d13), "crowding_assessment", ticker, full=True))
    # guidance
    gb = d.get("guidance_bundle") or (((_safe_load(steps["guidance_bundle"][0]) or {}).get("output")) if "guidance_bundle" in steps else None)
    if gb and gb.get("items"):
        panels.append(panel("Guidance", render_guidance(gb), "guidance_bundle", ticker, full=True))
    # thesis
    th = []
    for k, lbl in (("key_debate", "Key debate"), ("why_market_is_wrong", "Why the market is wrong"),
                   ("narrative_synthesis", "Narrative synthesis")):
        if d.get(k):
            th.append('<h2 style="font-size:12px;color:var(--mut);margin:10px 0 3px">%s</h2>%s' % (lbl, render_value(d[k])))
    if th:
        panels.append(panel("Thesis & brief", "".join(th), "research_brief", ticker, full=True))

    # data layer index (links into function inspector)
    di = []
    for fk in ("peer_comps", "bond_health", "social_topic_analysis", "crowding_assessment", "filing_13d",
               "news", "stocktwits", "guidance_bundle", "market_overlay", "bear_research"):
        present = fk in steps or (FN_BY_KEY[fk][4] == "result" and d.get(FN_BY_KEY[fk][5]) not in (None, "", [], {}))
        st = "" if present else "dim"
        di.append('<a class="tag %s" href="/fn/%s?ticker=%s">%s</a>' % (st, fk, urllib.parse.quote(ticker), esc(FN_BY_KEY[fk][1])))
    panels.append(panel("Data & ingestion", " ".join(di) + '<p class="muted" style="font-size:11px;margin:8px 0 0">'
                        'Click any to inspect that function\'s raw output for %s.</p>' % esc(ticker), None, ticker, full=True))

    # deliverables + runs
    dl = "".join('<a class="tag" href="/report/%s">%s</a>' % (urllib.parse.quote(n), esc(n)) for n in files_for(ticker, REPORTS))
    dl += "".join('<a class="tag" href="/export/%s">%s</a>' % (urllib.parse.quote(n), esc(n)) for n in files_for(ticker, EXPORTS))
    hist = " ".join('<a class="btn %s" href="/co/%s?run=%s">%s</a>' % (
        "on" if s == stamp else "", urllib.parse.quote(ticker), s, esc(s.replace("_", " "))) for s, _ in runs[:10])

    # Workforce / restructuring flag — only when there's a real signal (silent
    # otherwise). Prepended so a material layoff is the first thing you see.
    _wfe = steps.get("workforce_signal")
    _wfraw = (_safe_load(_wfe[0]) or {}) if _wfe else {}
    wf = _wfraw.get("output", _wfraw)
    if isinstance(wf, dict) and wf.get("has_signal"):
        rows = ""
        for e in (wf.get("events") or [])[:4]:
            bits = []
            if e.get("headcount"):
                bits.append(f'{e["headcount"]:,} positions')
            if e.get("pct_of_workforce"):
                bits.append(f'{e["pct_of_workforce"]:g}% of workforce')
            if e.get("charge_usd_m"):
                c = e["charge_usd_m"]
                bits.append(f'${c/1000:.1f}B charge' if c >= 1000 else f'${c:.0f}M charge')
            meta = " · ".join(bits) if bits else "restructuring"
            url = e.get("url", "")
            snip = esc((e.get("snippet") or "")[:240])
            rows += (f'<div style="margin:5px 0"><b>{esc(e.get("filing_date",""))}</b> '
                     f'<span class="dim">· {esc(e.get("source",""))}</span> — {esc(meta)}'
                     + (f' <a class="lnk" href="{esc(url)}" target="_blank">8-K ↗</a>' if url else '')
                     + (f'<div class="dim" style="font-size:11.5px;margin-top:2px;line-height:1.4">{snip}</div>'
                        if snip else '') + '</div>')
        warn = wf.get("warn") or []
        wrows = ""
        if warn:
            wtot = sum(w.get("employees") or 0 for w in warn)
            items = "".join(
                f'<li>{esc(w.get("notice_date",""))} <span class="dim">[{esc(w.get("state",""))}]</span> '
                f'{esc(w.get("company",""))}: {esc(str(w.get("employees","?")))} '
                f'<span class="dim">— {esc(w.get("kind",""))}</span></li>' for w in warn[:6])
            more = f'<li class="dim">+{len(warn)-6} more</li>' if len(warn) > 6 else ""
            wrows = (f'<div style="margin-top:8px;font-size:11.5px"><b>WARN notices</b> '
                     f'<span class="dim">(~{wtot:,} employees · state mass-layoff filings ~60d ahead · '
                     f'employer-name matched — verify)</span>'
                     f'<ul style="margin:3px 0 0 16px;line-height:1.5">{items}{more}</ul></div>')
        inner = (f'<div style="font-size:13px;font-weight:600;margin-bottom:5px">{esc(wf.get("summary",""))}</div>'
                 + rows + wrows
                 + '<p class="dim" style="font-size:11px;margin-top:7px;line-height:1.4">Read both ways: '
                   'a near-term margin/EPS tailwind from cost takeout, vs a demand tell — companies cut hard '
                   'when they see weakness the revenue line does not yet reflect.</p>')
        panels.insert(0, panel("⚠ Workforce / restructuring", inner, None, ticker, full=True))
    if d.get("call") or d.get("call_error"):
        panels.insert(0, panel("The call", render_call(d), None, ticker, full=True))

    body = (header + company_tabs(ticker, "overview") + qstat
            + '<div class="row" style="margin-top:14px"><span class="muted">Runs:</span> ' + hist + '</div>'
            + (('<div class="row"><span class="muted">Files:</span> ' + dl + '</div>') if dl else "")
            + '<div class="grid" style="margin-top:6px">' + "".join(panels) + '</div>'
            + '<details style="margin-top:14px"><summary>Full result JSON</summary><pre class="j">%s</pre></details>'
              % esc(json.dumps(d, indent=2, default=str)[:200000]))
    return layout(ticker, body, ticker)


def functions_page():
    groups = collections.OrderedDict()
    for f in FUNCTIONS:
        groups.setdefault(f[2], []).append(f)
    tickers = all_tickers()
    sections = []
    for g, fns in groups.items():
        cards = []
        for f in fns:
            key = f[0]
            cov = sum(1 for t in tickers if fn_output(t, f) not in (None, "", [], {}))
            pct = int(100 * cov / max(1, len(tickers)))
            cards.append(
                '<a class="gcard" href="/fn/%s" style="display:block">'
                '<div class="t">%s</div><div class="muted" style="font-size:11px;margin:2px 0 6px">%s</div>'
                '<div class="mono dim" style="font-size:11px">%s</div>'
                '<div class="cov" style="margin-top:7px"><i style="width:%d%%"></i></div>'
                '<div class="dim" style="font-size:11px;margin-top:3px">%d / %d tickers</div></a>' % (
                    key, esc(f[1]), esc(FN_DESC.get(key, "")[:88]), esc(f[3]), pct, cov, len(tickers)))
        sections.append('<div class="dim" style="margin:18px 0 8px;font-size:11px;text-transform:uppercase;'
                        'letter-spacing:.6px">%s</div>'
                        '<div class="gal">%s</div>' % (esc(g), "".join(cards)))
    body = ('<h1>Functions</h1><p class="sub">Every pipeline function, grouped. The bar shows how many '
            'tickers it has produced output for. Click one to see its actual output across the universe.</p>'
            + "".join(sections))
    return layout("Functions", body, "fn")


def function_inspector(key, ticker=None):
    f = FN_BY_KEY.get(key)
    if not f:
        return layout("?", "<h1>Unknown function</h1>", "fn")
    tickers = all_tickers()
    have = [(t, fn_output(t, f)) for t in tickers]
    have = [(t, o) for t, o in have if o not in (None, "", [], {})]
    cov = len(have)

    head = ('<h1>%s</h1><p class="sub">%s</p>'
            '<div class="row"><span class="pill">source <a href="#" style="color:var(--pu)">%s</a></span>'
            '<span class="pill">%d / %d tickers</span><span class="pill">group %s</span></div>') % (
        esc(f[1]), esc(FN_DESC.get(key, "")), esc(f[3]), cov, len(tickers), esc(f[2]))

    focus = ""
    if ticker and ticker in dict(have):
        out = dict(have)[ticker]
        focus = panel("%s · %s — full output" % (esc(f[1]), esc(ticker)),
                      render_value(out) + '<details style="margin-top:8px"><summary>raw json</summary><pre class="j">%s</pre></details>'
                      % esc(json.dumps(out, indent=2, default=str)[:120000]),
                      None, ticker, full=True)

    cards = []
    for t, out in sorted(have, key=lambda x: x[0]):
        cards.append('<div class="gcard"><div class="t"><a href="/fn/%s?ticker=%s">%s</a> '
                     '<a class="dim" style="font-size:11px;font-weight:400" href="/co/%s">tearsheet ↗</a></div>'
                     '<div style="margin:7px 0 0">%s</div>'
                     '<details style="margin-top:6px"><summary>raw</summary><pre class="j">%s</pre></details></div>' % (
            key, urllib.parse.quote(t), esc(t), urllib.parse.quote(t),
            preview(key, out), esc(json.dumps(out, indent=2, default=str)[:60000])))
    gallery = ('<!--fn-gallery--><h2 style="margin:18px 0 10px;font-size:14px">Output across the universe</h2>'
               + ('<div class="gal">%s</div>' % "".join(cards) if cards else '<p class="empty">No ticker has produced this output yet.</p>')
               + '<!--/fn-gallery-->')
    return layout(f[1], head + focus + gallery, "fn")


def compare_page(tickers):
    tickers = [t for t in tickers if t in list_results()]
    if not tickers:
        opts = " ".join('<a class="tag" href="/compare?t=%s">%s</a>' % (urllib.parse.quote(t), esc(t)) for t in all_tickers())
        return layout("Compare", '<h1>Compare</h1><p class="sub">Add tickers to the URL (e.g. /compare?t=COST&amp;t=ELF) '
                      'or pick a couple:</p><div class="row">' + opts + '</div>', "compare")
    metrics = [("Decision", lambda d: d.get("decision_verdict")),
               ("Edge verdict", lambda d: (d.get("edge_assessment") or {}).get("verdict")),
               ("Actionability", lambda d: num((d.get("edge_assessment") or {}).get("actionability_score"), d=3)),
               ("Price", lambda d: num((d.get("valuation") or {}).get("current_price"), pre="$")),
               ("Implied", lambda d: num((d.get("valuation") or {}).get("implied_price"), pre="$")),
               ("Upside", lambda d: signed_pct((d.get("valuation") or {}).get("upside_pct"))),
               ("Multiple", lambda d: num((d.get("valuation") or {}).get("applied_multiple"), suf="x", d=1)),
               ("Our EPS", lambda d: num(d.get("post_eps"))),
               ("Cons EPS", lambda d: num(d.get("consensus_eps"))),
               ("Schema", lambda d: d.get("schema")),
               ("Quality", lambda d: d.get("quality_line"))]
    data = {t: (load_result(t)[0] or {}) for t in tickers}
    head = "".join('<th><a href="/co/%s">%s</a></th>' % (urllib.parse.quote(t), esc(t)) for t in tickers)
    rows = []
    for label, fn in metrics:
        cells = "".join("<td>%s</td>" % (esc(fn(data[t]) if fn(data[t]) is not None else "—")) for t in tickers)
        rows.append("<tr><td class=k>%s</td>%s</tr>" % (esc(label), cells))
    body = ('<h1>Compare</h1><p class="sub">%d names side by side.</p>'
            '<table><thead><tr><th></th>%s</tr></thead><tbody>%s</tbody></table>') % (len(tickers), head, "".join(rows))
    return layout("Compare", body, "compare")

# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

TABS = [("overview", "Overview"), ("estimates", "Estimates"), ("ownership", "Ownership"),
        ("transcripts", "Transcripts"), ("press", "Press"), ("decks", "Decks"),
        ("research", "Research"), ("search", "Search")]


def company_tabs(ticker, active):
    qt = urllib.parse.quote(ticker)
    out = '<div class="ctabs">'
    for key, label in TABS:
        href = f"/co/{qt}" if key == "overview" else f"/co/{qt}/{key}"
        out += f'<a class="{"ct on" if active == key else "ct"}" href="{href}">{esc(label)}</a>'
    return out + "</div>"


def _co_header(ticker, d, stamp):
    return (f'<h1>{esc(ticker)} <span class="muted" style="font-size:15px;font-weight:400">{esc((d or {}).get("name") or "")}</span></h1>'
            f'<p class="sub">run {esc((stamp or "").replace("_", " "))}</p>')


def render_research_records(recs):
    rows = ""
    for r in recs:
        a = r.get("author", {}) or {}
        tier = r.get("effective_tier", 3)
        stance = (r.get("stance") or "")
        sc = {"bull": "up", "bear": "dn"}.get(stance.lower(), "dim")
        pill = "g" if tier == 1 else ("a" if tier == 2 else "")
        tks = ", ".join((r.get("tickers") or [])[:5])
        rid = r.get("id", "")
        subj = esc(r.get("subject", ""))
        subj_html = f'<a href="/research/{esc(rid)}">{subj}</a>' if rid else subj
        rows += (
            '<div class="nf-i">'
            f'<span class="nf-d">{esc(str(r.get("date",""))[5:10])}</span>'
            f'<span class="pill {pill}" style="font-size:9px;padding:0 5px;flex:0 0 auto">t{tier}</span>'
            f'<span class="nf-h"><b>{subj_html}</b> '
            f'<span class="dim" style="font-size:11px">{esc(a.get("name", r.get("sender","")))}</span> '
            f'<span class="{sc}" style="font-size:10px;text-transform:uppercase">{esc(stance)}</span>'
            f'<span class="dim" style="font-size:11px"> · {esc(tks)}</span>'
            f'<div class="dim" style="font-size:12px;margin-top:2px;line-height:1.4">{esc(r.get("thesis",""))}</div>'
            '</span></div>')
    return '<div class="nf">' + rows + '</div>'


def render_edge(e):
    cv = e.get("consensus_vs_variant") or {}
    bc = e.get("variant_bear_case") or {}
    conf = (e.get("edge_confidence") or "")
    pill = "g" if "high" in conf.lower() else ("a" if "med" in conf.lower() else "")

    def _list(label, items):
        if not items:
            return ""
        lis = "".join(f"<li>{esc(str(x))}</li>" for x in items)
        return (f'<h2 style="font-size:12px;color:var(--mut);margin:11px 0 3px">{esc(label)}</h2>'
                f'<ul style="margin:0 0 0 16px;font-size:12.5px;line-height:1.5">{lis}</ul>')

    h = [f'<p><span class="pill {pill}">edge: {esc(conf)}</span> '
         f'<span class="dim">{esc(str(e.get("_version","")))} · {esc(str(e.get("_n_sources","")))} sources</span></p>']
    if e.get("variant_perception"):
        h.append(f'<p style="font-size:13.5px"><b>Variant perception:</b> {esc(e["variant_perception"])}</p>')
    if e.get("actionable_thesis"):
        h.append(f'<p style="font-size:13px"><b>Actionable:</b> {esc(e["actionable_thesis"])}</p>')
    if cv:
        h.append('<h2 style="font-size:12px;color:var(--mut);margin:11px 0 3px">Consensus vs variant</h2>')
        h.append(f'<p class="dim" style="font-size:12.5px">Consensus: {esc(cv.get("consensus_view",""))}</p>')
        h.append(f'<p style="font-size:12.5px">Variant: {esc(cv.get("variant_scenario",""))}</p>')
        if cv.get("implied_mispricing"):
            h.append(f'<p style="font-size:12.5px"><b>Mispricing:</b> {esc(cv.get("implied_mispricing",""))}</p>')
    h.append(_list("Falsifiable drivers", e.get("falsifiable_drivers")))
    h.append(_list("Kill criteria", e.get("kill_criteria")))
    if bc:
        prob = bc.get("probability_variant_correct", "")
        h.append(f'<h2 style="font-size:12px;color:var(--mut);margin:11px 0 3px">Bear case on the variant — {esc(str(prob))}% variant-correct</h2>')
        h.append(f'<p class="dim" style="font-size:12.5px">{esc(bc.get("steelman",""))}</p>')
    return "".join(x for x in h if x)


def view_research(ticker):
    d, stamp = load_result(ticker)
    try:
        from research.external_research import research_for_ticker
        recs = research_for_ticker(ticker, min_tier=3)
    except Exception:
        recs = []
    parts = ""
    edge = _safe_load(os.path.join(DATA, "email_research", "edge", ticker.upper() + ".json"))
    if isinstance(edge, dict) and edge.get("variant_perception"):
        parts += panel("Edge thesis (synthesized from inbox research)", render_edge(edge), None, ticker, full=True)
    if recs:
        parts += panel(f"External research — {len(recs)} pieces", render_research_records(recs), None, ticker, full=True)
    if not parts:
        parts = '<p class="empty">No external (inbox) research mentions this name yet.</p>'
    body = _co_header(ticker, d, stamp) + company_tabs(ticker, "research") + '<div class="grid">' + parts + '</div>'
    return layout(ticker + " research", body, ticker)


def research_page():
    try:
        from research.external_research import load_classified, universe_coverage
        recs = [r for r in load_classified() if r.get("is_research")]
        cov = universe_coverage(recs)
    except Exception:
        recs, cov = [], {}
    recs.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    cov_str = " · ".join(f'<a href="/co/{esc(k)}/research">{esc(k)}</a> {v}' for k, v in list(cov.items())[:20])
    body = ('<h1>Research knowledge base <span class="muted" style="font-size:14px;font-weight:400">'
            f'{len(recs)} digested pieces from your inbox</span></h1>'
            f'<p class="sub">coverage: {cov_str or "—"}</p>'
            '<div class="grid">'
            + panel("All research (newest first)", render_research_records(recs), None, None, full=True)
            + '</div>')
    return layout("research", body, "research")


def view_research_item(rid):
    import glob as _glob
    rec = _safe_load(os.path.join(DATA, "email_research", "classified", rid + ".json"))
    if not isinstance(rec, dict):
        return layout("research", '<p><a href="/research">← research</a></p><p class="empty">Item not found.</p>', "research")
    raw = _safe_load(os.path.join(DATA, "email_research", "raw", rid + ".json"))
    if not isinstance(raw, dict):
        for f in _glob.glob(os.path.join(DATA, "email_research", "raw", "*__" + rid + ".json")):
            raw = _safe_load(f)
            if isinstance(raw, dict):
                break
    a = rec.get("author", {}) or {}
    tier = rec.get("effective_tier", 3)
    pill = "g" if tier == 1 else ("a" if tier == 2 else "")

    def _ul(label, items):
        if not items:
            return ""
        return (f'<h2 style="font-size:12px;color:var(--mut);margin:11px 0 3px">{esc(label)}</h2>'
                '<ul style="margin:0 0 0 16px;font-size:12.5px;line-height:1.5">'
                + "".join(f"<li>{esc(str(x))}</li>" for x in items) + "</ul>")

    summ = (f'<p><span class="pill {pill}">tier {tier} · {esc(a.get("name", rec.get("sender","")))}</span> '
            f'<span class="dim">{esc(str(rec.get("date",""))[:10])} · stance {esc(rec.get("stance",""))} · '
            f'{esc(", ".join(rec.get("tickers") or []))}</span></p>')
    if rec.get("thesis"):
        summ += f'<p style="font-size:13.5px"><b>Thesis:</b> {esc(rec["thesis"])}</p>'
    summ += _ul("Key points", rec.get("key_points"))
    summ += _ul("Notable claims", rec.get("notable_claims"))
    summ += _ul("Catalysts", rec.get("catalysts"))
    if rec.get("themes"):
        summ += f'<p class="dim" style="font-size:12px">themes: {esc(", ".join(rec["themes"]))}</p>'

    body_txt = (raw or {}).get("body_text", "") or "(raw text not cached for this item)"
    raw_html = f'<pre class="prose" style="white-space:pre-wrap;font-size:12.5px">{esc(body_txt[:60000])}</pre>'
    nav = '<a href="/research">← all research</a>'
    if rec.get("primary_ticker"):
        nav += (f' · <a href="/co/{urllib.parse.quote(rec["primary_ticker"])}/research">'
                f'{esc(rec["primary_ticker"])} research</a>')
    parts = (f'<p>{nav}</p><h1 style="font-size:18px">{esc(rec.get("subject",""))}</h1>'
             '<div class="grid">'
             + panel("Summary (digested)", summ, None, None, full=True)
             + panel("Raw text (as received)", raw_html, None, None, full=True)
             + '</div>')
    return layout((rec.get("subject", "research") or "research")[:40], parts, "research")


def _sparkline(obs, w=320, h=64):
    vals = [v for _, v in obs if v is not None]
    if len(vals) < 2:
        return '<div class="dim">no data</div>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(obs)
    pts = [f"{i/(n-1)*w:.1f},{h-(v-lo)/rng*h:.1f}" for i, (d, v) in enumerate(obs)]
    up = obs[-1][1] >= obs[0][1]
    col = "var(--gd)" if up else "var(--rd)"
    lx, ly = pts[-1].split(",")
    return (f'<svg viewBox="0 0 {w} {h+4}" width="100%" height="56" preserveAspectRatio="none" '
            f'style="display:block;margin-top:4px">'
            f'<polyline fill="none" stroke="{col}" stroke-width="1.6" points="{" ".join(pts)}"/>'
            f'<circle cx="{lx}" cy="{ly}" r="2.6" fill="{col}"/></svg>')


def _fmt_macro(sid, val, unit):
    if val is None:
        return "—"
    if sid == "TOTALSL":
        return f"${val/1e6:.2f}T"      # FRED reports in $millions
    if unit == "%":
        return f"{val:.2f}%"
    if unit == "k":
        return f"{val/1000:.0f}k"      # initial claims in count → thousands
    if unit == "idx":
        return f"{val:.1f}"
    if unit == "$B":
        return f"${val:,.0f}B"
    return f"{val:.2f}"


_MACRO_MEANING = {
    "A191RL1Q225SBEA": "Real growth pace; <2% = below trend, sub-1% = stall speed.",
    "RSAFS": "Consumer spending (nominal); deflate by CPI for the real read.",
    "INDPRO": "Factory/utility/mining output; rolling over = goods-cycle weakness.",
    "UNRATE": "Higher = weaker labor market → softer wage growth and consumer demand.",
    "ICSA": "High-frequency layoffs gauge; a sustained rise leads the unemployment rate.",
    "PAYEMS": "Job growth; decelerating YoY = late-cycle labor cooling.",
    "CPIAUCSL": "Headline inflation; hotter keeps the Fed tight and squeezes real incomes.",
    "CPILFESL": "Core inflation (ex food/energy) — the Fed's underlying-trend gauge.",
    "PPIACO": "Producer input costs; leads goods margins + feeds CPI 1-2 quarters out.",
    "FEDFUNDS": "Policy rate / cost of capital; falling = easing tailwind for multiples + demand.",
    "DGS10": "Risk-free discount rate; rising pressures long-duration + growth-stock multiples.",
    "T10Y2Y": "Yield curve; negative (inverted) has preceded recessions, re-steepening near onset.",
    "BAMLH0A0HYM2": "Credit risk appetite; tight = complacent/risk-on, widening = stress/risk-off.",
    "PSAVERT": "Lower = consumers spending more of income (late-cycle); rising = retrenchment.",
    "TOTALSL": "Consumer leverage; fast rise = pulled-forward demand / credit-stress risk.",
    "UMCSENT": "Consumer mood; depressed sentiment leads discretionary pullbacks.",
    "DSPIC96": "Real after-tax income — the fuel for spending; outpacing spending = saving, lagging = drawdown.",
    "CES0500000003": "Wage growth; above inflation = rising real purchasing power, below = erosion.",
    "PCEC96": "Real (inflation-adjusted) consumer spending — the demand engine, ~68% of GDP.",
    "PCEDGC96": "Durable goods (autos, appliances) — big-ticket, rate-sensitive, first cut in a slowdown.",
    "PCENDC96": "Nondurables (food, fuel, staples) — necessity-heavy, more stable.",
    "PCESC96": "Services (housing, healthcare, travel) — the sticky majority of spend; rotation target post-COVID.",
    "RSFSDP": "Restaurants & bars — pure discretionary; an early tell when households retrench.",
    "RSMVPD": "Autos & parts — big-ticket, credit-sensitive; swings with rates + incentives.",
    "RSNSR": "Online / nonstore — secular share gainer; outgrowth vs total retail = channel shift.",
    "RSGMS": "General merchandise (big-box) — broad discretionary-goods read.",
    "RSGASS": "Gas stations — mostly PRICE not volume; a necessity that crowds out discretionary when high.",
    "REVOLSL": "Credit-card balance growth; fast rise = pulled-forward demand OR households stretching.",
    "DRCCLACBS": "Share of card balances 90+ days late — the cleanest consumer-stress signal; rising = strain.",
}


# Curated multi-series exhibits — comparison charts whose TITLE states the
# finding (single-series sparklines can't show a divergence). Each pulls obs from
# the fetched macro series by sid. (category, finding-title, note, [(label, sid)]).
_MACRO_EXHIBITS = [
    ("Consumer", "The goods cycle has stalled while services carry spending",
     "Real consumer spending, YoY by type — durable goods rolled over first; services are the last pillar.",
     [("Durable goods", "PCEDGC96"), ("Nondurables", "PCENDC96"), ("Services", "PCESC96")]),
    ("Consumer", "Spending is outrunning income — the gap is credit and savings",
     "Real consumer spending vs real disposable income, YoY.",
     [("Real spending", "PCEC96"), ("Real disposable income", "DSPIC96")]),
    ("Consumer", "Surging gas is crowding out discretionary spend",
     "Retail sales YoY — a price-driven necessity vs a pure-discretionary category.",
     [("Gas stations", "RSGASS"), ("Restaurants & bars", "RSFSDP")]),
    ("Consumer", "Real wages are negative — raises aren't keeping up with prices",
     "Average hourly earnings YoY vs headline CPI YoY; the gap is lost purchasing power.",
     [("Wages", "CES0500000003"), ("CPI", "CPIAUCSL")]),
    ("Inflation", "Inflation is re-accelerating, with PPI surging upstream",
     "Headline vs core CPI vs producer prices, YoY — pipeline pressure leads consumer prices.",
     [("Headline CPI", "CPIAUCSL"), ("Core CPI", "CPILFESL"), ("PPI", "PPIACO")]),
    ("Rates & credit", "The Fed holds as the curve stays barely positive",
     "Policy rate vs the 10-year Treasury yield (%).",
     [("Fed funds", "FEDFUNDS"), ("10-year", "DGS10")]),
]


def _fmt_chg(c):
    return f"{c:+,.0f}" if abs(c) >= 1000 else f"{c:+.2f}"


def _changes_html(changes):
    out = []
    for lbl in ("3mo", "12mo", "5y"):
        if lbl in changes:
            c = changes[lbl]
            cls = "up" if c > 0.05 else ("dn" if c < -0.05 else "dim")
            arrow = "↑" if c > 0.05 else ("↓" if c < -0.05 else "→")
            out.append(f'<span class="{cls}">{lbl} {arrow}{_fmt_chg(c)}</span>')
    return "  ·  ".join(out)


def _render_macro_digest(dg):
    if not isinstance(dg, dict) or not dg.get("regime"):
        return ""
    head = (f'<div style="font-size:14px;font-weight:600;line-height:1.55;margin-bottom:9px">'
            f'{esc(dg.get("regime",""))}</div>')
    secs = [("consumer", "Consumer"), ("inflation", "Inflation"), ("labor", "Labor"),
            ("growth", "Growth"), ("rates_credit", "Rates & credit")]
    grid = "".join(
        f'<div style="margin:7px 0"><b style="font-size:11.5px;color:var(--mut)">{lbl}</b>'
        f'<div style="font-size:12.5px;line-height:1.5">{esc(dg.get(k,""))}</div></div>'
        for k, lbl in secs if dg.get(k))
    heading = (f'<div style="margin:9px 0 0"><b style="font-size:11.5px;color:var(--mut)">'
               f'Where it\'s heading</b><div style="font-size:12.5px;line-height:1.5">'
               f'{esc(dg.get("whats_heading",""))}</div></div>' if dg.get("whats_heading") else "")
    impl = dg.get("investment_implications") or []
    if isinstance(impl, str):
        impl = [impl]
    implhtml = ('<div style="margin-top:10px"><b style="font-size:12px;color:var(--ac)">'
                'Investment implications</b><ul style="margin:4px 0 0 16px;font-size:12.5px;line-height:1.55">'
                + "".join(f"<li>{esc(str(x))}</li>" for x in impl) + "</ul></div>") if impl else ""
    return head + grid + heading + implhtml


# Consistent multi-series palette (BofA-inspired, tuned for the dark theme).
# Colors are assigned by position and reused across every exhibit so the reader
# learns them once.
_MPAL = ["#5b9bff", "#f5a524", "#41d18f", "#f2616b", "#9b8cff"]


def _nice_ticks(lo, hi, n=4):
    import math
    if hi <= lo:
        hi = lo + 1.0
    step = (hi - lo) / max(1, n)
    if step <= 0:
        return [lo]
    mag = 10 ** math.floor(math.log10(step))
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= step:
            step = mag * m
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + 1e-9:
        if v >= lo - 1e-9:
            ticks.append(round(v, 4))
        v += step
    return ticks or [lo, hi]


def _mchart(members, *, w=340, h=152):
    """Multi-series comparison chart (BofA-style): 2-4 series on shared axes,
    consistent palette, an emphasized zero baseline + light gridlines, an inline
    legend, and a multi-series hover tooltip. `members` = [(label, color,
    obs[[date,val]]), ...]. Values are rendered as % (the comparison exhibits are
    all YoY% or rate%). This is the answer to 'single-point charts are weak' — it
    shows the DIVERGENCE between series, which is usually the finding."""
    from datetime import date as _d
    ser = []
    for lbl, col, obs in members:
        clean = [(str(dd)[:10], v) for dd, v in (obs or []) if v is not None]
        if len(clean) >= 2:
            ser.append((lbl, col, clean))
    if not ser:
        return '<div class="dim">no data</div>'

    def od(s):
        return _d.fromisoformat(s).toordinal()

    dmin = min(od(o[0]) for _, _, obs in ser for o in obs)
    dmax = max(od(o[0]) for _, _, obs in ser for o in obs)
    if dmax == dmin:
        dmax = dmin + 1
    vmin = min(v for _, _, obs in ser for _, v in obs)
    vmax = max(v for _, _, obs in ser for _, v in obs)
    vmin, vmax = min(vmin, 0.0), max(vmax, 0.0)   # always show the zero baseline
    pad = (vmax - vmin) * 0.08 or 1.0
    vmin -= pad
    vmax += pad
    pl, pr, ptp, pb = 32, 8, 8, 18
    pw, ph = w - pl - pr, h - ptp - pb

    def X(o):
        return pl + (od(o) - dmin) / (dmax - dmin) * pw

    def Y(v):
        return ptp + (vmax - v) / (vmax - vmin) * ph

    grid = ""
    for t in _nice_ticks(vmin, vmax, 4):
        if t < vmin or t > vmax:
            continue
        y = Y(t)
        zero = abs(t) < 1e-9
        grid += (f'<line x1="{pl}" y1="{y:.1f}" x2="{w-pr}" y2="{y:.1f}" '
                 f'stroke="{"var(--mut)" if zero else "var(--bd)"}" stroke-width="{0.9 if zero else 0.5}"/>'
                 f'<text x="{pl-4}" y="{y+3:.1f}" text-anchor="end" font-size="9" fill="var(--dim)">{t:g}%</text>')
    base = ser[0][2]
    xs = ""
    for o, anch in ((base[0][0], "start"), (base[len(base) // 2][0], "middle"),
                    (base[-1][0], "end")):
        xs += (f'<text x="{X(o):.1f}" y="{h-5}" text-anchor="{anch}" font-size="9" '
               f'fill="var(--dim)">{o[:7]}</text>')
    polys, dots, jsdata = "", "", []
    for lbl, col, obs in ser:
        pts = [(X(d), Y(v)) for d, v in obs]
        polys += (f'<polyline fill="none" stroke="{col}" stroke-width="1.7" '
                  f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>')
        dots += (f'<circle class="mcdot" r="3" fill="{col}" stroke="var(--bg)" '
                 f'stroke-width="1" style="display:none"/>')
        jsdata.append({"label": lbl, "color": col,
                       "pts": [[round(X(d), 1), round(Y(v), 1), d[:7], f"{v:g}%"] for d, v in obs]})
    legend = " ".join(
        f'<span style="white-space:nowrap"><span style="color:{col}">●</span> '
        f'<span style="font-size:11px">{esc(lbl)}</span></span>' for lbl, col, _ in ser)
    data = json.dumps(jsdata).replace("&", "&amp;").replace("'", "&#39;")
    return (
        f'<div class="mchart" data-series=\'{data}\' data-w="{w}" data-h="{h}" '
        f'style="position:relative;margin-top:5px">'
        f'<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:2px">{legend}</div>'
        f'<svg viewBox="0 0 {w} {h}" width="100%" style="display:block;overflow:visible">'
        f'{grid}{xs}{polys}'
        f'<line class="mcx" x1="{pl}" y1="{ptp}" x2="{pl}" y2="{ptp+ph}" stroke="var(--mut)" '
        f'stroke-width="0.8" stroke-dasharray="3 3" style="display:none"/>{dots}</svg>'
        f'<div class="mctip" style="position:absolute;top:14px;display:none;background:var(--surf2);'
        f'border:1px solid var(--bd2);border-radius:5px;padding:4px 7px;font-size:11px;'
        f'pointer-events:none;white-space:nowrap;z-index:5;line-height:1.5"></div></div>')


def _ichart(obs, w=320, h=64):
    vals = [v for _, v in obs if v is not None]
    if len(vals) < 2:
        return '<div class="dim">no data</div>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(obs)
    pts = [(i / (n - 1) * w, h - (v - lo) / rng * h) for i, (d, v) in enumerate(obs)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    col = "var(--gd)" if obs[-1][1] >= obs[0][1] else "var(--rd)"
    zero_line = ""
    if lo < 0 < hi:   # show the zero baseline on series that cross it (YoY/%)
        yz = h - (0 - lo) / rng * h
        zero_line = (f'<line x1="0" y1="{yz:.1f}" x2="{w}" y2="{yz:.1f}" '
                     f'stroke="var(--bd2)" stroke-width="0.8" stroke-dasharray="3 3"/>')
    data = json.dumps([[d, v] for d, v in obs])
    return (f"<div class=\"ichart\" data-obs='{data}' data-w=\"{w}\" data-h=\"{h}\" "
            f'data-lo="{lo}" data-hi="{hi}" style="position:relative;margin-top:4px">'
            f'<svg viewBox="0 0 {w} {h+4}" width="100%" height="60" preserveAspectRatio="none" '
            f'style="display:block;overflow:visible">'
            f'{zero_line}'
            f'<polyline fill="none" stroke="{col}" stroke-width="1.6" points="{poly}"/>'
            f'<line class="cx" x1="0" y1="0" x2="0" y2="{h}" stroke="var(--mut)" stroke-width="0.8" style="display:none"/>'
            f'<circle class="dt" r="2.8" fill="{col}" style="display:none"/></svg>'
            f'<div class="tip" style="position:absolute;top:-3px;display:none;background:var(--surf2);'
            f'border:1px solid var(--bd2);border-radius:4px;padding:1px 6px;font-size:11px;'
            f'pointer-events:none;white-space:nowrap"></div></div>')


def macro_page():
    try:
        from research.macro_dashboard import (fetch_macro_series, fetch_rate_odds,
                                              synthesize_macro_digest, CATEGORIES)
        series = fetch_macro_series()
        odds = fetch_rate_odds()
        digest = synthesize_macro_digest(series, odds)
        cats = CATEGORIES
    except Exception:
        series, odds, digest, cats = {}, [], {}, []
    parts = ""
    dghtml = _render_macro_digest(digest)
    if dghtml:
        parts += ('<div class="grid">'
                  + panel("Macro digest — where we are & where it's heading", dghtml, None, None, full=True)
                  + '</div>')
    # Key exhibits — multi-series comparison charts that carry the finding (the
    # title IS the takeaway). Lead with these; the per-series decomposition grids
    # follow below as the detail.
    exhibits_html = ""
    for _cat, title, note, members in _MACRO_EXHIBITS:
        ms = [(lbl, _MPAL[i % len(_MPAL)], (series.get(sid) or {}).get("obs") or [])
              for i, (lbl, sid) in enumerate(members)]
        if not any(m[2] for m in ms):
            continue
        asof = max(((series.get(sid) or {}).get("latest") or ["", None])[0] for _, sid in members)
        inner = (_mchart(ms)
                 + f'<div class="dim" style="font-size:11px;margin-top:6px;line-height:1.4">{esc(note)}</div>'
                 + f'<div class="dim" style="font-size:10px;margin-top:3px">Source: FRED · as of {esc(asof)}</div>')
        exhibits_html += panel(title, inner, None, None)
    if exhibits_html:
        parts += ('<h2 style="margin:18px 0 7px;font-size:15px">Key exhibits</h2>'
                  '<div class="grid">' + exhibits_html + '</div>')
    by_cat = {}
    for sid, d in series.items():
        by_cat.setdefault(d.get("category", ""), []).append((sid, d))
    def _series_panel(sid, d):
        lat, unit = d.get("latest"), d.get("unit", "")
        disp = _fmt_macro(sid, lat[1] if lat else None, unit)
        take = _changes_html(d.get("changes") or {})
        meaning = _MACRO_MEANING.get(sid, "")
        inner = (f'<div style="font-size:20px;font-weight:600">{esc(disp)} '
                 f'<span class="dim" style="font-size:11px">· {lat[0] if lat else ""}</span></div>'
                 + _ichart(d.get("obs") or [])
                 + (f'<div style="font-size:11.5px;margin-top:6px">{take}</div>' if take else "")
                 + (f'<div class="dim" style="font-size:11px;margin-top:3px;line-height:1.4">{esc(meaning)}</div>' if meaning else ""))
        return panel(d.get("label", sid), inner, None, None)

    for cat in (cats or list(by_cat.keys())):
        if cat not in by_cat:
            continue
        parts += f'<h2 style="margin:20px 0 7px;font-size:15px">{esc(cat)}</h2>'
        # Group by subgroup, preserving first-seen order — this is the consumer
        # DECOMPOSITION order; non-consumer cats have a single "" subgroup (flat).
        subs: dict = {}
        for sid, d in by_cat[cat]:
            subs.setdefault(d.get("subgroup", ""), []).append((sid, d))
        for sub, items in subs.items():
            if sub:
                parts += (f'<h3 style="margin:13px 0 5px;font-size:11.5px;color:var(--mut);'
                          f'font-weight:600;text-transform:uppercase;letter-spacing:.5px">{esc(sub)}</h3>')
            parts += ('<div class="grid">'
                      + "".join(_series_panel(sid, d) for sid, d in items) + "</div>")
    if odds:
        rows = "".join(
            f'<div class="nf-i"><span class="nf-h">{esc(o["question"])}</span>'
            f'<span class="pill {"g" if (o.get("prob") or 0) >= 50 else "a"}" style="flex:0 0 auto">{o.get("prob","?")}%</span></div>'
            for o in odds)
        parts += ('<h2 style="margin:20px 0 7px;font-size:15px">Prediction markets</h2><div class="grid">'
                  + panel("Fed / macro odds (Polymarket, live)", '<div class="nf">' + rows + '</div>',
                          None, None, full=True) + "</div>")
    if not parts:
        parts = '<p class="empty">Macro data unavailable (FRED unreachable).</p>'
    script = (
        "<script>document.querySelectorAll('.ichart').forEach(function(el){"
        "var obs;try{obs=JSON.parse(el.dataset.obs)}catch(e){return}"
        "var w=+el.dataset.w,h=+el.dataset.h,lo=+el.dataset.lo,hi=+el.dataset.hi,rng=(hi-lo)||1,n=obs.length;"
        "var svg=el.querySelector('svg'),cx=el.querySelector('.cx'),dt=el.querySelector('.dt'),tip=el.querySelector('.tip');"
        "el.addEventListener('mousemove',function(e){var r=svg.getBoundingClientRect();"
        "var f=(e.clientX-r.left)/r.width;f=Math.max(0,Math.min(1,f));var i=Math.round(f*(n-1));var d=obs[i];if(!d)return;"
        "var vx=i/(n-1)*w,vy=h-(d[1]-lo)/rng*h;"
        "cx.setAttribute('x1',vx);cx.setAttribute('x2',vx);cx.style.display='';"
        "dt.setAttribute('cx',vx);dt.setAttribute('cy',vy);dt.style.display='';"
        "tip.style.display='block';tip.style.left=Math.min(f*r.width,r.width-86)+'px';"
        "tip.innerHTML='<b>'+d[1]+'</b> <span style=\"opacity:.6\">'+String(d[0]).slice(0,7)+'</span>';});"
        "el.addEventListener('mouseleave',function(){cx.style.display='none';dt.style.display='none';tip.style.display='none';});"
        "});"
        # Multi-series exhibit charts: crosshair + a dot per series + a tooltip
        # listing every series' value at the hovered date.
        "document.querySelectorAll('.mchart').forEach(function(el){"
        "var S;try{S=JSON.parse(el.dataset.series)}catch(e){return}"
        "var W=+el.dataset.w;var svg=el.querySelector('svg'),cx=el.querySelector('.mcx'),"
        "tip=el.querySelector('.mctip'),dots=el.querySelectorAll('.mcdot');"
        "svg.addEventListener('mousemove',function(e){var r=svg.getBoundingClientRect();"
        "var x=(e.clientX-r.left)/r.width*W;var rows='',dt='';"
        "S.forEach(function(s,si){var best=null,bd=1e9;"
        "s.pts.forEach(function(p){var d=Math.abs(p[0]-x);if(d<bd){bd=d;best=p;}});"
        "var dot=dots[si];if(best){if(dot){dot.setAttribute('cx',best[0]);dot.setAttribute('cy',best[1]);dot.style.display='';}"
        "rows+='<div><span style=\"color:'+s.color+'\">\\u25cf</span> '+s.label+': <b>'+best[3]+'</b></div>';dt=best[2];}});"
        "cx.setAttribute('x1',x);cx.setAttribute('x2',x);cx.style.display='';"
        "tip.innerHTML='<div style=\"color:var(--dim);margin-bottom:1px\">'+dt+'</div>'+rows;tip.style.display='block';"
        "var px=x/W*r.width;tip.style.left=Math.min(Math.max(px-42,2),r.width-132)+'px';});"
        "svg.addEventListener('mouseleave',function(){cx.style.display='none';tip.style.display='none';"
        "dots.forEach(function(d){d.style.display='none';});});"
        "});</script>")
    body = ('<h1>Macro <span class="muted" style="font-size:14px;font-weight:400">FRED + prediction markets · AI digest</span></h1>'
            '<p class="sub">hover any chart to read values · 3mo / 12mo / 5y change + what it means below each · grouped by theme</p>'
            + parts + script)
    return layout("macro", body, "macro")


def _search_hl(text, query):
    e = esc(text)
    try:
        return re.sub(re.escape(esc(query)),
                      lambda m: f'<mark style="background:#4a3c00;color:#ffd24d;padding:0 1px">{m.group(0)}</mark>',
                      e, flags=re.I)
    except Exception:
        return e


def _md_inline(s):
    e = esc(s)
    e = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e)
    e = re.sub(r"\[([^\]]+)\]", r'<span style="color:var(--ac);font-size:11px">[\1]</span>', e)
    return e


def _md(text):
    out = []
    for line in (text or "").split("\n"):
        l = line.rstrip()
        h = re.match(r"^(#{1,4})\s+(.*)", l)
        if h:
            sz = {1: 16, 2: 14, 3: 13, 4: 12.5}.get(len(h.group(1)), 13)
            out.append(f'<div style="font-size:{sz}px;font-weight:600;margin:11px 0 3px">{_md_inline(h.group(2))}</div>')
        elif re.match(r"^\s*[-*]\s+", l):
            out.append(f'<div style="margin:1px 0 1px 14px">• {_md_inline(re.sub(r"^\s*[-*]\s+", "", l))}</div>')
        elif l.strip():
            out.append(f'<div style="margin:5px 0;line-height:1.5">{_md_inline(l)}</div>')
    return "".join(out)


def view_search(ticker, query="", mode="search"):
    d, stamp = load_result(ticker)
    query = (query or "").strip()
    qt = urllib.parse.quote(ticker)
    # "Ask AI" runs a multi-second Claude RAG call on a full-page GET submit, so
    # nothing changes on screen until it returns. Show a thinking overlay on
    # click (the page stays visible until the response, so the overlay persists
    # through the wait, then the reloaded page replaces it).
    askwait_fx = (
        "<style>"
        ".askwait{position:fixed;inset:0;background:rgba(13,16,20,.74);display:flex;"
        "align-items:center;justify-content:center;z-index:9999}"
        ".askwait .bx{background:var(--surf2);border:1px solid var(--bd2);border-radius:10px;"
        "padding:20px 26px;color:var(--tx);font-size:14.5px;text-align:center;"
        "box-shadow:0 12px 44px rgba(0,0,0,.55)}"
        ".askwait .sub{color:var(--dim);font-size:11.5px;margin-top:7px}"
        ".askwait .sp{display:inline-block;width:14px;height:14px;border:2px solid var(--bd2);"
        "border-top-color:var(--ac);border-radius:50%;animation:aksp .7s linear infinite;"
        "vertical-align:-2px;margin-right:7px}"
        "@keyframes aksp{to{transform:rotate(360deg)}}"
        "</style>"
        "<script>function askWait(){"
        "var f=document.getElementById('askform');var q=f&&f.querySelector('input[name=q]');"
        "if(!q||!q.value.trim())return true;"
        "var o=document.createElement('div');o.className='askwait';"
        "o.innerHTML='<div class=\"bx\"><span class=\"sp\"></span>\\u2726 Claude is reading "
        "__TICKER__\\u2019s corpus\\u2026<div class=\"sub\">transcripts \\u00b7 filings \\u00b7 "
        "decks \\u00b7 news \\u00b7 research</div></div>';"
        "document.body.appendChild(o);return true;}</script>"
    ).replace("__TICKER__", esc(ticker))
    box = (askwait_fx
           + f'<form id="askform" method="get" action="/co/{qt}/search" style="margin:4px 0 14px">'
           f'<input name="q" value="{esc(query)}" autofocus class="btn" '
           f'style="width:60%;max-width:560px;padding:8px 11px;font-size:14px" '
           f'placeholder="Search or ask anything about {esc(ticker)} — transcripts · filings · decks · news · research">'
           f' <button name="mode" value="search" class="btn{"" if mode=="ask" else " on"}" style="padding:8px 14px">Search</button>'
           f' <button name="mode" value="ask" onclick="return askWait()" class="btn{" on" if mode=="ask" else ""}" style="padding:8px 14px">Ask AI ✦</button></form>')
    parts = ""
    if query and mode == "ask":
        try:
            from research.ticker_qa import ask_ticker
            ans = ask_ticker(ticker, query) or {}
        except Exception as e:
            ans = {"answer": f"(error: {type(e).__name__})", "snippets": []}
        conf = ans.get("confidence", "")
        pill = "g" if conf == "high" else ("a" if conf == "medium" else "")
        inner = (f'<p><span class="pill {pill}">confidence: {esc(conf or "—")}</span> '
                 f'<span class="dim">grounded in {ans.get("n_retrieved", 0)} retrieved passages</span></p>'
                 + _md(ans.get("answer", "")))
        srcs = ans.get("sources_used") or []
        if srcs:
            inner += ('<details style="margin-top:10px"><summary class="dim" style="cursor:pointer">'
                      f'sources used ({len(srcs)})</summary><div style="font-size:11.5px;margin-top:4px">'
                      + "".join(f'<div class="dim">• {esc(str(s))}</div>' for s in srcs) + "</div></details>")
        snips = ans.get("snippets") or []
        if snips:
            inner += ('<details style="margin-top:6px"><summary class="dim" style="cursor:pointer">'
                      f'retrieved passages ({len(snips)})</summary><div class="nf" style="margin-top:4px">'
                      + "".join(f'<div class="nf-i"><span class="nf-h" style="font-size:12px;line-height:1.4">{_search_hl(s, query)}</span></div>' for s in snips[:24])
                      + "</div></details>")
        parts = panel(f'Ask AI — "{esc(query)}"', inner, None, ticker, full=True)
    elif query:
        try:
            from research.corpus_search import search_ticker
            results = search_ticker(ticker, query)
        except Exception:
            results = []
        nhits = sum(r["n_hits"] for r in results)
        parts += f'<p class="sub">{nhits} hits across {len(results)} sources for "{esc(query)}"</p>'
        for r in results:
            rows = "".join(
                f'<div class="nf-i"><span class="nf-h" style="line-height:1.5">{_search_hl(h["snippet"], query)}</span></div>'
                for h in r["hits"])
            loc = " · ".join(x for x in (r.get("location"), r.get("date")) if x)
            label = f'{r["source"]}' + (f' — {loc}' if loc else "") + f'  ({r["n_hits"]})'
            parts += panel(label, '<div class="nf">' + rows + '</div>', None, ticker, full=True)
        if not results:
            parts += '<p class="empty">No matches in this name\'s corpus.</p>'
    body = (_co_header(ticker, d, stamp) + company_tabs(ticker, "search") + box
            + '<div class="grid">' + parts + '</div>')
    return layout(ticker + " search", body, ticker)


def _parse_q_full(text):
    """Token-based parse of the quarterly_financials table → oldest-first list
    of {period, revenue, rev_qoq, rev_yoy, op_inc, op_margin, eps, eps_yoy}.
    Robust to varying column counts (some names carry gross/net-margin columns,
    others don't), so we locate values by role not fixed position."""
    def _money(tok):
        m = re.match(r"\$(-?[\d,]+(?:\.\d+)?)(M)?$", tok)
        return (float(m.group(1).replace(",", "")), bool(m.group(2))) if m else None

    def _pct(tok):
        m = re.match(r"([+\-]?[\d.]+)%$", tok or "")
        return float(m.group(1)) if m else None

    rows = []
    for line in (text or "").splitlines():
        if not re.match(r"\s*Q[1-4]\s+\d{4}", line):
            continue
        toks = re.sub(r"\$\s+", "$", line).split()      # "$   70,527M" → "$70,527M"
        if len(toks) < 4:
            continue
        period = f"{toks[0]} {toks[1]}"
        rest = toks[2:]
        rev = rev_idx = op_inc = op_idx = eps = eps_idx = None
        for i, t in enumerate(rest):
            mo = _money(t)
            if not mo:
                continue
            val, is_m = mo
            if is_m:
                if rev is None:
                    rev, rev_idx = val, i
                elif op_inc is None:
                    op_inc, op_idx = val, i
            else:
                eps, eps_idx = val, i   # last non-M $ amount on the line is EPS
        after = rest[rev_idx + 1:] if rev_idx is not None else []
        rev_qoq = _pct(after[0]) if len(after) >= 1 else None   # token after rev = Q/Q
        rev_yoy = _pct(after[1]) if len(after) >= 2 else None   # next token = YoY ('—'→None)
        op_margin = None
        if op_idx is not None:
            for t in rest[op_idx + 1:]:
                p = _pct(t)
                if p is not None:
                    op_margin = p
                    break
        # EPS YoY is the token immediately after the EPS value ('—' → None).
        eps_yoy = _pct(rest[eps_idx + 1]) if (eps_idx is not None and eps_idx + 1 < len(rest)) else None
        rows.append({"period": period, "revenue": rev, "rev_qoq": rev_qoq, "rev_yoy": rev_yoy,
                     "op_inc": op_inc, "op_margin": op_margin, "eps": eps, "eps_yoy": eps_yoy})
    rows.reverse()
    return rows


def _next_q(period):
    m = re.match(r"Q([1-4])\s+(\d{4})", period or "")
    if not m:
        return None
    q, y = int(m.group(1)), int(m.group(2))
    q += 1
    if q > 4:
        q, y = 1, y + 1
    return f"Q{q} {y}"


def _period_labels(qfull):
    """Explicit fiscal labels for the consensus periods, inferred from the
    latest reported quarter: {current_quarter: 'Q1 2026', next_quarter: …,
    current_year: 'FY2026', next_year: 'FY2027'}."""
    if not qfull:
        return {}
    q1 = _next_q(qfull[-1]["period"])
    q2 = _next_q(q1) if q1 else None
    m = re.match(r"Q[1-4]\s+(\d{4})", q1 or "")
    fy1 = f"FY{m.group(1)}" if m else None
    fy2 = f"FY{int(m.group(1)) + 1}" if m else None
    return {"current_quarter": q1, "next_quarter": q2, "current_year": fy1, "next_year": fy2}


def _fmt_cell(metric, v):
    if v is None:
        return '<span class="dim">—</span>'
    if metric == "revenue":
        return f"${v:,.0f}M"
    if metric == "eps":
        return f"${v:.2f}"
    if metric == "op_margin":
        return f"{v:.1f}%"
    if metric in ("rev_growth", "eps_growth"):
        return f'<span class="{"up" if v >= 0 else "dn"}">{v:+.1f}%</span>'
    if metric == "n":
        return f'<span class="dim">{v}</span>'
    return esc(str(v))


def _est_col_from_consensus(label, e):
    g = lambda k: (e.get(k) * 100) if isinstance(e.get(k), (int, float)) else None
    rm = e.get("revenue_mean")
    return {"label": label, "est": True, "data": {
        "revenue": (rm / 1e6) if isinstance(rm, (int, float)) else None,
        "rev_growth": g("revenue_growth_yoy"), "op_margin": None,
        "eps": e.get("eps_mean"), "eps_growth": g("eps_growth_yoy"),
        "n": e.get("eps_num_analysts")}}


def _est_matrix(qrows, cf, annual=False, scroll_id="estscroll"):
    """Transposed matrix: metrics as rows, periods (up to ~24 quarters of
    actuals + forward estimates) as columns, with explicit fiscal labels.
    Rendered in a horizontal scroll box auto-positioned to the latest quarters;
    scroll left for up to ~6 years of history."""
    cols = []
    if not annual:
        for r in qrows[-24:]:        # up to 6 years; latest shown, scroll for older
            cols.append({"label": r["period"], "est": False, "data": {
                "revenue": r.get("revenue"), "rev_growth": r.get("rev_yoy"),
                "op_margin": r.get("op_margin"), "eps": r.get("eps"),
                "eps_growth": r.get("eps_yoy"), "n": None}})
        if qrows:
            q1 = _next_q(qrows[-1]["period"])
            q2 = _next_q(q1) if q1 else None
            for lab, key in ((q1, "current_quarter"), (q2, "next_quarter")):
                e = (cf or {}).get(key) or {}
                if lab and e:
                    cols.append(_est_col_from_consensus(lab, e))
    else:
        from collections import defaultdict
        byyr = defaultdict(list)
        for r in qrows:
            byyr[r["period"].split()[1]].append(r)
        prev, last_fy = None, None
        for yr in sorted(byyr):
            qs = byyr[yr]
            if len(qs) < 4:
                continue
            rev = sum(q["revenue"] for q in qs if q.get("revenue") is not None)
            eps = sum(q["eps"] for q in qs if q.get("eps") is not None)
            growth = round((rev / prev - 1) * 100, 1) if prev else None
            cols.append({"label": f"FY{yr}", "est": False, "data": {
                "revenue": rev, "rev_growth": growth, "op_margin": None,
                "eps": round(eps, 2), "eps_growth": None, "n": None}})
            prev, last_fy = rev, int(yr)
        # The estimated current FY is the FY of the next quarter to report
        # (matches the consensus labels) — NOT last-complete-actual-FY+1, which
        # breaks when the quarterly feed has gaps.
        q1 = _next_q(qrows[-1]["period"]) if qrows else None
        mq = re.match(r"Q[1-4]\s+(\d{4})", q1 or "")
        base = int(mq.group(1)) if mq else None
        for off, key in ((0, "current_year"), (1, "next_year")):
            e = (cf or {}).get(key) or {}
            if e and base:
                cols.append(_est_col_from_consensus(f"FY{base + off}", e))
    if not cols:
        return ""
    metrics = [("revenue", "Revenue ($M)"), ("rev_growth", "Rev growth YoY"),
               ("op_margin", "Op margin"), ("eps", "Adj EPS"),
               ("eps_growth", "EPS growth YoY"), ("n", "# analysts")]
    est_th = "color:var(--ac);background:var(--surf2)"
    est_td = "background:var(--surf2)"
    th = ('<th style="text-align:left;position:sticky;left:0;z-index:2;background:var(--surf2);'
          'box-shadow:1px 0 0 var(--bd)">Metric</th>'
          + "".join(f'<th class="num" style="white-space:nowrap;padding:5px 9px;'
                    f'{est_th if c["est"] else ""}">{esc(c["label"])}{" E" if c["est"] else ""}</th>'
                    for c in cols))
    body = ""
    for mk, ml in metrics:
        if mk == "n" and not any(c["est"] for c in cols):
            continue
        tds = "".join(f'<td class="num" style="padding:5px 9px;{est_td if c["est"] else ""}">'
                      f'{_fmt_cell(mk, c["data"].get(mk))}</td>' for c in cols)
        body += (f'<tr><td style="position:sticky;left:0;z-index:1;background:var(--bg);'
                 f'box-shadow:1px 0 0 var(--bd)">{esc(ml)}</td>{tds}</tr>')
    n_act = sum(1 for c in cols if not c["est"])
    hint = ('<p class="muted" style="font-size:11px;margin:0 0 5px">'
            f'{n_act} quarters of history — showing the latest, scroll &#9664; for older. '
            'Forward consensus columns marked <span style="color:var(--ac)">E</span>.</p>') if not annual else ""
    return (f'{hint}<div id="{scroll_id}" class="estscroll" style="overflow-x:auto;scrollbar-width:thin">'
            f'<table style="font-size:11.5px;white-space:nowrap;border-collapse:separate;border-spacing:0">'
            f'<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'
            f'<script>(function(){{var e=document.getElementById("{scroll_id}");'
            f'if(e)e.scrollLeft=e.scrollWidth;}})();</script>')


def view_estimates(ticker):
    d, stamp = load_result(ticker)
    d = d or {}
    steps = cache_steps(ticker)
    cf = d.get("consensus_full") or {}

    def revB(x):
        try:
            return f"${float(x)/1e9:.1f}B"
        except Exception:
            return "-"

    def grow(v):
        return signed_pct(v * 100) if isinstance(v, (int, float)) else "-"

    qf = (_safe_load(steps["quarterly_financials"][0]) or {}).get("output", {}).get("corpus_text") if "quarterly_financials" in steps else None
    series = parse_quarterly(qf)
    qfull = _parse_q_full(qf)

    panels = panel("Consensus snapshot (click a cell for range and revisions)",
                   render_estimates(d, _period_labels(qfull)), "consensus", ticker, full=True)
    q_mat = _est_matrix(qfull, cf, annual=False, scroll_id="estscroll-q") or '<p class="empty">No quarterly history parsed.</p>'
    q_tbl = f'<div id="q-tbl">{q_mat}</div>'
    a_note = ('<p class="muted" style="font-size:11px;margin-top:6px">Annual actuals aggregate the quarterly '
              'table (complete fiscal years only); forward FY estimates are consensus means (yfinance). '
              'Adj EBITDA isn\'t in the Polygon feed — it lives in earnings press releases (planned).</p>')
    a_tbl = f'<div id="a-tbl" style="display:none">{_est_matrix(qfull, cf, annual=True, scroll_id="estscroll-a")}{a_note}</div>'

    toggle = ('<div class="row" style="margin-bottom:8px"><button id="qa-q" class="btn on" onclick="qatoggle(\'q\')">Quarterly</button>'
              '<button id="qa-a" class="btn" onclick="qatoggle(\'a\')">Annual</button></div>')
    fin_tk = (f'<p style="font-size:12px;margin:8px 0 2px">{_fin_takeaway(series)}</p>'
              if series and _fin_takeaway(series) else "")
    panels += panel("Actuals and estimates", toggle + q_tbl + a_tbl
                    + (svg_trajectory(series) + fin_tk if series else ""),
                    "quarterly_financials", ticker, full=True)

    runs = list_results().get(ticker, [])
    hrows = ""
    for s, path in runs[:15]:
        rd = _safe_load(path) or {}
        ce = rd.get("consensus_eps")
        if ce is None:
            continue
        hrows += (f"<tr><td class='dim'>{esc(s.replace('_', ' '))}</td><td class='num'>{num(ce)}</td>"
                  f"<td class='num'>{num(rd.get('post_eps'))}</td></tr>")
    if hrows:
        panels += panel("Estimate revision history (across your runs)",
                        "<p class='muted' style='font-size:11px;margin-bottom:6px'>How the captured forward consensus and our "
                        "modelled EPS moved each time you ran this name.</p>"
                        "<table><thead><tr><th>Run</th><th class='num'>Consensus EPS</th><th class='num'>Our EPS</th></tr></thead><tbody>"
                        + hrows + "</tbody></table>", "consensus", ticker, full=True)

    body = _co_header(ticker, d, stamp) + company_tabs(ticker, "estimates") + '<div class="grid">' + panels + '</div>'
    return layout(ticker + " estimates", body, ticker)


_PIE_COLORS = ["#4d9fff", "#ff8a4d", "#4dd0a0", "#c77dff", "#ffd24d", "#ff6b8a",
               "#6dd5ff", "#a0d468", "#9aa5b1"]

_PIE_FX = """
<style>
.psl{transition:opacity .12s ease, transform .12s ease; transform-box:fill-box; transform-origin:center; cursor:default;}
.psl.on{opacity:1; transform:scale(1.05);}
.psl.dim{opacity:.2;}
.pleg{transition:opacity .12s, background .12s; cursor:default;}
.pleg.on{background:rgba(120,150,210,.18); font-weight:600;}
.pleg.dim{opacity:.4;}
.pseg{transition:opacity .12s, box-shadow .12s; cursor:default;}
.pseg.on{opacity:1; box-shadow:inset 0 0 0 2px rgba(255,255,255,.8);}
.pseg.dim{opacity:.3;}
</style>
<script>
(function(){
 if(window.__pieFx)return; window.__pieFx=1;
 function all(){return document.querySelectorAll('.psl,.pleg,.pseg');}
 function clear(){all().forEach(function(e){e.classList.remove('on','dim');});}
 function enter(el){
  var c=el.getAttribute('data-c'),i=el.getAttribute('data-i'),k=el.getAttribute('data-k'),t=el.getAttribute('data-trig');
  var kb=(t&&k)?k:null;
  all().forEach(function(e){
   var ec=e.getAttribute('data-c'),ei=e.getAttribute('data-i'),ek=e.getAttribute('data-k');
   var hit=(ec===c&&ei===i)||(kb&&ek===kb);
   if(hit){e.classList.add('on');e.classList.remove('dim');return;}
   var involved=(ec===c)||(kb&&ek!=null&&ek!=='');
   if(involved){e.classList.add('dim');e.classList.remove('on');}
   else{e.classList.remove('on','dim');}
  });
 }
 document.addEventListener('mouseover',function(e){var t=e.target.closest&&e.target.closest('.psl,.pleg,.pseg');if(t)enter(t);});
 document.addEventListener('mouseout',function(e){var t=e.target.closest&&e.target.closest('.psl,.pleg,.pseg');if(t)clear();});
})();
</script>
"""

# Fixed colors per ownership bucket so the by-type pie reads consistently.
_BUCKET_COLORS = {
    "PE / strategic":       "#c77dff",
    "Hedge fund":           "#ff6b8a",
    "Asset manager":        "#4d9fff",
    "Index / passive":      "#4dd0a0",
    "Other institutional":  "#ffd24d",
    "Public float / other": "#5a6675",
}


def _pie_chart(items, size=176, colors=None, chart_id="pie", keys=None, broadcast=False):
    """Donut + legend. Hovering a slice or its legend row highlights the pair
    and dims the rest of the chart (data-c/data-i). When `keys` are supplied
    (e.g. each slice's bucket) and `broadcast` is set, hovering also lights up
    every element sharing that key across the page — so hovering a type bucket
    highlights the matching holders in the other pie. See _PIE_FX for the JS."""
    import math
    pairs = [(l, float(v)) for l, v in items if v and float(v) > 0]
    # Keep colors / keys aligned to the surviving (positive) items.
    surv = [i for i, (l, v) in enumerate(items) if v and float(v) > 0]
    if colors is not None:
        colors = [colors[i] for i in surv]
    keys = [keys[i] for i in surv] if keys is not None else None
    items = pairs
    total = sum(v for _, v in items)
    if not items or total <= 0:
        return ""
    cx = cy = size / 2
    r = size / 2 - 3
    ir = r * 0.56
    angle = -90.0
    trig = ' data-trig="1"' if broadcast else ""
    paths, legend = [], []
    for i, (label, v) in enumerate(items):
        frac = v / total
        sweep = frac * 360
        a0, a1 = math.radians(angle), math.radians(angle + sweep)
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if sweep > 180 else 0
        col = colors[i] if colors else _PIE_COLORS[i % len(_PIE_COLORS)]
        k = esc(keys[i]) if keys else ""
        at = f'class="psl" data-c="{chart_id}" data-i="{i}" data-k="{k}"{trig}'
        if frac >= 0.999:  # single slice → full ring
            paths.append(f'<circle {at} cx="{cx}" cy="{cy}" r="{r}" fill="{col}"/>')
        else:
            paths.append(f'<path {at} d="M {cx:.1f} {cy:.1f} L {x0:.1f} {y0:.1f} '
                         f'A {r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" fill="{col}" '
                         f'stroke="var(--bg)" stroke-width="1"/>')
        legend.append((label, frac, col, i, k))
        angle += sweep
    paths.append(f'<circle cx="{cx}" cy="{cy}" r="{ir:.1f}" fill="var(--surf)"/>')
    svg = (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
           f'style="flex:0 0 auto">{"".join(paths)}</svg>')
    leg = "".join(
        f'<div class="pleg" data-c="{chart_id}" data-i="{idx}" data-k="{k}"{trig} '
        f'style="display:flex;gap:7px;align-items:center;padding:2px 4px;margin:0 -4px;'
        f'border-radius:4px;font-size:12px">'
        f'<span style="width:10px;height:10px;background:{c};border-radius:2px;flex:0 0 auto"></span>'
        f'<span style="flex:1">{esc(l)}</span><span class="dim">{f*100:.1f}%</span></div>'
        for l, f, c, idx, k in legend)
    return (f'<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">{svg}'
            f'<div style="flex:1;min-width:190px">{leg}</div></div>')


_HOLDER_SUFFIX_RE = re.compile(
    r"(?:[,\s]+(?:INC|INC\.|LLC|L\.L\.C\.|LP|L\.P\.|LLP|CORP|CORPORATION|CO|CO\.|"
    r"LTD|LTD\.|LIMITED|PLC|GP|N\.?V\.?|S\.?A\.?|TRUST|HOLDINGS?|& CO|MANAGEMENT))+\s*$",
    re.IGNORECASE)


def _short_name(name, limit=30):
    """Tidy a holder label for the pie legend: drop a trailing legal suffix,
    then truncate. Idempotent on already-clean names."""
    n = _HOLDER_SUFFIX_RE.sub("", name or "").strip(" ,")
    if len(n) < 4:
        n = (name or "").strip()
    return n if len(n) <= limit else n[:limit - 1] + "…"


def _decomp_bar(dc):
    """Horizontal stacked bar: closely-held vs institutional (net of shorts)
    vs retail float — the 'where's the rest of the float' decomposition."""
    if not dc:
        return ""
    segs = [
        ("Closely held", dc.get("closely_held_pct") or 0, "#9b8cff",
         f"strategic + insiders (incl. ~{num(dc.get('control_pct'), suf='%', d=0)} control block)"),
        ("Institutions (net)", dc.get("institutional_net_pct") or 0, "#4d9fff",
         "13F holders, net of short interest"),
        ("Retail / other float", dc.get("retail_pct") or 0, "#ffb454",
         "residual of the public float"),
    ]
    tot = sum(s[1] for s in segs) or 100
    bar = '<div style="display:flex;height:30px;border-radius:5px;overflow:hidden;margin:2px 0 10px">'
    for i, (lbl, pct, col, _t) in enumerate(segs):
        w = max(0.0, pct) / tot * 100
        if w <= 0:
            continue
        inside = f'{pct:.0f}%' if w >= 8 else ''
        bar += (f'<div class="pseg" data-c="decomp" data-i="{i}" data-k="" '
                f'title="{esc(lbl)}: {pct:.1f}%" style="width:{w:.2f}%;background:{col};'
                f'display:flex;align-items:center;justify-content:center;font-size:11px;'
                f'color:#1a1a1a;font-weight:600">{inside}</div>')
    bar += '</div>'
    leg = '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;margin-bottom:6px">'
    for i, (lbl, pct, col, t) in enumerate(segs):
        leg += (f'<span class="pleg" data-c="decomp" data-i="{i}" data-k="" '
                f'style="display:flex;gap:6px;align-items:center;padding:1px 4px;margin:0 -4px;border-radius:4px">'
                f'<span style="width:10px;height:10px;border-radius:2px;background:{col};flex:0 0 auto"></span>'
                f'<span><b>{esc(lbl)}</b> {pct:.1f}% '
                f'<span class="dim">· {esc(t)}</span></span></span>')
    leg += '</div>'
    short = dc.get("short_pct_of_float")
    gross = dc.get("inst_reported_pct")
    cav = ('<p class="muted" style="font-size:11px;margin:6px 0 0;line-height:1.55">'
           f'Estimate. Public float ~{num(dc.get("float_pct"), suf="%", d=0)} of shares out. '
           f'Institutions <i>report</i> ~{num(gross, suf="%", d=0)} (gross), which exceeds the float because '
           f'~{num(short, suf="%", d=0)} of the float is sold short — each shorted share is owned by two '
           'longs. Netting out short interest leaves the retail residual above. '
           'Short / float figures from yfinance; treat as rough.</p>')
    return ('<div style="margin-top:16px;border-top:1px solid var(--bd);padding-top:12px">'
            '<p class="muted" style="font-size:11px;margin:0 0 6px;font-weight:600">'
            'Float decomposition — who holds the company</p>' + leg + bar + cav + '</div>')


def render_ownership_pies(oh):
    """Two pies from the complete (reverse-13F + 13D/G) holder list:
    individual top holders, and holdings aggregated by owner type. Both in
    % of shares outstanding, so they read as 'who owns the company'."""
    holders = oh.get("holders") or []
    buckets = oh.get("buckets") or []
    if not holders:
        return None
    float_pct = oh.get("float_pct") or 0
    overlap = oh.get("overlap_flag")

    # ── By-holder pie: top 12 named + aggregated tail + float ──
    # Each holder is keyed by its bucket so a type-pie hover lights it up.
    top = holders[:12]
    rest = holders[12:]
    h_items = [(_short_name(h['name'])
                + ("" if h.get("source") == "13F" else " · 13D/G"), h["pct"]) for h in top]
    h_cols = [_BUCKET_COLORS.get(h["bucket"], "#9aa5b1") for h in top]
    h_keys = [h["bucket"] for h in top]
    if rest:
        h_items.append((f"Other institutions ({len(rest)})", sum(h["pct"] for h in rest)))
        h_cols.append("#7a8696")
        h_keys.append("_other")
    if float_pct > 0.5 and not overlap:
        h_items.append(("Public float / other", float_pct))
        h_cols.append(_BUCKET_COLORS["Public float / other"])
        h_keys.append("_float")
    holder_pie = _pie_chart(h_items, colors=h_cols, chart_id="hpie", keys=h_keys)

    # ── By-type pie: ownership buckets (already include float) ──
    # Buckets broadcast their key, so hovering one highlights the matching
    # individual holders in the by-holder pie.
    t_items = [(b["bucket"], b["pct"]) for b in buckets]
    t_cols = [_BUCKET_COLORS.get(b["bucket"], "#9aa5b1") for b in buckets]
    t_keys = [b["bucket"] for b in buckets]
    type_pie = _pie_chart(t_items, colors=t_cols, chart_id="tpie", keys=t_keys, broadcast=True)

    period = esc(oh.get("period_ending") or "")
    so = oh.get("shares_outstanding_m")
    nh = oh.get("n_holders") or len(holders)
    disclosed = oh.get("total_disclosed_pct")
    cap = (f'<p class="muted" style="font-size:11px;margin:0 0 10px">'
           f'{nh} institutional + 5%+ holders hold ~{num(disclosed, suf="%", d=0)} '
           f'of ~{num(so, suf="M", d=0)} shares outstanding · 13F as of {period}.</p>')
    note = (f'<p class="muted" style="font-size:11px;margin:8px 0 0">{esc(oh.get("as_of_note") or "")}</p>'
            if oh.get("as_of_note") else "")
    failed = oh.get("n_fetch_failed") or 0
    if failed:
        note += (f'<p class="muted" style="font-size:11px;margin:4px 0 0">'
                 f'({failed} filer info-tables unreadable, excluded.)</p>')
    if oh.get("truncated"):
        nm, nmt = oh.get("n_managers") or 0, oh.get("n_managers_total") or 0
        if nmt > nm:
            msg = (f'Top {nm} of {nmt} 13F filers shown; tail aggregated.')
        else:
            msg = (f'~{nm} filers retrieved; a few SEC pages were unavailable, '
                   f'so a small tail may be missing.')
        note += (f'<p class="muted" style="font-size:11px;margin:4px 0 0">'
                 f'({msg} Decomposition uses complete vendor institutional %.)</p>')

    two = (
        '<div style="display:flex;gap:26px;flex-wrap:wrap">'
        '<div style="flex:1;min-width:300px">'
        '<p class="muted" style="font-size:11px;margin:0 0 4px;font-weight:600">By holder</p>'
        + holder_pie + '</div>'
        '<div style="flex:1;min-width:300px">'
        '<p class="muted" style="font-size:11px;margin:0 0 4px;font-weight:600">By owner type</p>'
        + type_pie + '</div>'
        '</div>'
    )
    return _PIE_FX + cap + two + _decomp_bar(oh.get("decomposition")) + note


def view_ownership(ticker):
    d, stamp = load_result(ticker)
    steps = cache_steps(ticker)
    panels = ""
    oh = (_safe_load(steps["ownership_holders"][0]) or {}).get("output") or {} \
        if "ownership_holders" in steps else {}
    # The all-holders pies stand on their own (reverse-13F), independent of
    # whether the name is in our tracked-fund crowding universe.
    pies = render_ownership_pies(oh) if oh.get("holders") else None
    if pies:
        panels += panel("Major stakeholders (all holders, % of shares outstanding)",
                        pies, "ownership_holders", ticker, full=True)
    if "crowding_assessment" in steps:
        cr = (_safe_load(steps["crowding_assessment"][0]) or {}).get("output") or {}
        d13 = ((_safe_load(steps["filing_13d"][0]) or {}).get("output") or {}) if "filing_13d" in steps else {}
        if not pies:
            # Fallback: tracked-13F-only pie by $ value (pre-reverse-13F data).
            th = cr.get("top_holders") or []
            if th:
                ranked = sorted([(h.get("fund_name", ""), h.get("value_m") or 0) for h in th],
                                key=lambda x: -x[1])
                top = ranked[:8]
                rest = sum(v for _, v in ranked[8:])
                slices = [(l, v) for l, v in top] + ([("Other tracked funds", rest)] if rest > 0 else [])
                cap = ('<p class="muted" style="font-size:11px;margin:0 0 8px">Tracked-fund 13F only '
                       '(run the pipeline to populate the full all-holders view).</p>')
                panels += panel("Major stakeholders (13F holders, by $ value)",
                                cap + _pie_chart(slices), "crowding_assessment", ticker)
        panels += panel("13F crowding & 13D / 13G", render_crowding(cr, d13), "crowding_assessment", ticker, full=True)
    if "filing_form4" in steps:
        f4 = (_safe_load(steps["filing_form4"][0]) or {}).get("output") or {}
        panels += panel("Insider transactions (Form 4)", render_insiders(f4), "filing_form4", ticker, full=True)
    panels = panels or '<p class="empty">No ownership data on file.</p>'
    body = _co_header(ticker, d, stamp) + company_tabs(ticker, "ownership") + '<div class="grid">' + panels + '</div>'
    return layout(ticker + " ownership", body, ticker)


def _render_transcript_quarter(q):
    """One quarter's transcript as labeled speaker paragraphs, split into
    Prepared remarks vs Q&A. Falls back to a flat block for old (level-1) caches
    that have no speaker structure."""
    speakers = q.get("speakers")
    if not speakers:
        return (f'<pre class="prose" style="white-space:pre-wrap;max-height:560px;'
                f'overflow:auto;margin-top:6px">{esc(q.get("text") or "")}</pre>')

    def turns(items):
        h = ""
        for t in items:
            txt = (t.get("text") or "").strip()
            if not txt:
                continue
            ttl = t.get("title")
            label = (f'<span class="tspk">{esc(t.get("name") or "Speaker")}</span>'
                     + (f'<span class="tttl"> · {esc(ttl)}</span>' if ttl else ""))
            h += f'<div class="tturn">{label}<div class="ttext">{esc(txt)}</div></div>'
        return h

    qa = q.get("qa_start")
    if isinstance(qa, int) and 0 < qa < len(speakers):
        body = (f'<div class="tsec">Prepared remarks</div>{turns(speakers[:qa])}'
                f'<div class="tsec">Question &amp; answer</div>{turns(speakers[qa:])}')
    else:
        body = f'<div class="tsec">Transcript</div>{turns(speakers)}'
    return f'<div style="max-height:620px;overflow:auto;margin-top:6px;padding-right:6px">{body}</div>'


def view_transcripts(ticker):
    d, stamp = load_result(ticker)
    steps = cache_steps(ticker)
    parts = ""
    dig = (_safe_load(steps["transcript_digest"][0]) or {}).get("output") if "transcript_digest" in steps else None
    if dig:
        kv = {k: dig.get(k) for k in ("tone_trajectory", "management_credibility", "quarters_count") if dig.get(k) is not None}
        inner = render_value(kv)
        for k, lbl in (("recurring_concerns", "Recurring concerns"), ("key_inflection_points", "Key inflection points"),
                       ("guidance_evolution", "Guidance evolution")):
            if dig.get(k):
                inner += f"<h2 style='font-size:13px;color:var(--mut);margin:12px 0 4px'>{esc(lbl)}</h2>" + render_value(dig[k])
        parts += panel("Transcript analysis (multi-quarter digest)", inner, "transcript_digest", ticker, full=True)
    tr = (_safe_load(steps["transcripts"][0]) or {}).get("output") if "transcripts" in steps else None
    raw_q = (tr or {}).get("raw_quarters") or []
    if raw_q:
        # Full, un-truncated transcripts — one collapsible per quarter, newest first.
        inner = ('<p class="muted" style="font-size:11px;margin:0 0 8px">'
                 f'{len(raw_q)} quarters · full prepared remarks + Q&amp;A. Click a quarter to expand.</p>')
        for q in raw_q:
            title = f"Q{q.get('quarter')} {q.get('year')}"
            if q.get("date"):
                title += f" · {q['date']}"
            nsp = len(q.get("speakers") or [])
            meta = (f"{nsp} speaker turns" if nsp else f"{q.get('char_count') or len(q.get('text') or ''):,} chars")
            inner += (f'<details style="margin-bottom:4px"><summary>{esc(title)} '
                      f'<span class="dim">· {meta}</span></summary>'
                      f'{_render_transcript_quarter(q)}</details>')
        parts += panel("Earnings call transcripts (full)", inner, "transcripts", ticker, full=True)
    elif tr and tr.get("text"):
        # Legacy fallback: split the digest blob into per-quarter sections.
        text = tr["text"]
        chunks = re.split(r"(---\s*Q[1-4]\s+\d{4}\s+EARNINGS CALL\s*\([\d-]+\)\s*---)", text)
        inner, i = "", 1
        while i < len(chunks):
            marker = chunks[i].strip().strip("-").strip()
            bt = chunks[i + 1] if i + 1 < len(chunks) else ""
            inner += f'<details><summary>{esc(marker)}</summary><pre class="prose" style="white-space:pre-wrap">{esc(bt.strip())}</pre></details>'
            i += 2
        parts += panel("Earnings call transcripts (digest — re-run for full)",
                       inner or f'<pre class="prose" style="white-space:pre-wrap">{esc(text)}</pre>',
                       "transcripts", ticker, full=True)
    parts = parts or '<p class="empty">No transcripts on file.</p>'
    body = _co_header(ticker, d, stamp) + company_tabs(ticker, "transcripts") + '<div class="grid">' + parts + '</div>'
    return layout(ticker + " transcripts", body, ticker)


_IR_EARN_RE = re.compile(
    r"\b(earnings|results|first|second|third|fourth|quarter|q[1-4]|fiscal|guidance|"
    r"dividend|revenue|to announce|to report|conference call)\b", re.I)


def _near_date(d, dates, tol=2):
    """True if date string d is within `tol` days of any date in `dates`."""
    import datetime as _dt
    try:
        dd = _dt.date.fromisoformat((d or "")[:10])
    except Exception:
        return d in dates
    for x in dates:
        try:
            if abs((dd - _dt.date.fromisoformat((x or "")[:10])).days) <= tol:
                return True
        except Exception:
            if x == d:
                return True
    return False


def view_press(ticker):
    d, stamp = load_result(ticker)
    cname = (d or {}).get("name", "")
    steps = cache_steps(ticker)
    items = []
    if "news" in steps:
        raw = (_safe_load(steps["news"][0]) or {}).get("output") or {}
        items = _news_items(raw)

    # IR-site press (nicer links + product/company news). Earnings items also let
    # us prefer the IR link over the matching EDGAR 8-K exhibit below.
    ir_items, ir_earn_dates = [], set()
    irp = (_safe_load(steps["ir_press"][0]) or {}).get("output") if "ir_press" in steps else None
    for it in ((irp or {}).get("items") or []):
        title = it.get("title") or ""
        idate = str(it.get("date") or "")[:10]
        earn = bool(_IR_EARN_RE.search(title))
        if earn and idate:
            ir_earn_dates.add(idate)
        ir_items.append({"date": idate, "source": "IR site", "sentiment": "",
                         "headline": title, "desc": "", "url": it.get("url") or "",
                         "_release": True, "_ir": True,
                         "_kind": "release" if earn else "news", "_noise": not earn})

    pr = (_safe_load(steps["press_releases"][0]) or {}).get("output") if "press_releases" in steps else None
    if isinstance(pr, list):
        for it in pr:
            if not isinstance(it, dict):
                continue
            # 8-K exhibits (PressRelease dicts) carry no title field. Ex 99.1 is
            # the release; Ex 99.2/3 is the operating supplement — derive a
            # headline for each and drop financial-statement / binary exhibits.
            kind = it.get("kind") or "release"
            txt = it.get("text", "") or it.get("full_text_with_tables", "")
            head = it.get("title") or it.get("headline")
            if not head:
                head = _pr_supp_headline(txt) if kind == "supplement" else _pr_headline(txt)
            if not head:
                head = f"{ticker} earnings {kind} {it.get('quarter') or ''}".strip()
            if not _pr_is_real(head, txt):
                continue
            exnum = "99.2/3" if kind == "supplement" else "99.1"
            dstr = str(it.get("date") or it.get("report_date") or it.get("filing_date") or "")[:10]
            if kind == "release" and ir_earn_dates and _near_date(dstr, ir_earn_dates):
                continue  # the IR-site version (nicer link) is shown instead
            items.append({
                "date": dstr,
                "source": it.get("source") or f"SEC · 8-K Ex {exnum}", "sentiment": "",
                "headline": head, "desc": "",
                "url": it.get("url") or it.get("link") or it.get("source_url") or "",
                "_release": True, "_kind": kind})

    items += ir_items

    # Dedupe by headline, newest first (the feed has frequent near-dupes).
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: (str(x.get("date", "")), 1 if x.get("_release") else 0),
                     reverse=True):
        k = (it.get("headline") or "")[:70].lower().strip()
        if not k or k in seen or k.startswith(("fetched:", "===", "---", "(material")):
            continue
        seen.add(k)
        uniq.append(it)
    items = uniq

    n_company = n_noise = 0
    rows = ""
    cur_year = None
    for it in items:
        if it.get("_ir"):
            cat, noise = "company", bool(it.get("_noise"))
            tag = "RELEASE" if it.get("_kind") == "release" else "NEWS"
        elif it.get("_release"):
            cat, noise = "company", False
            tag = "SUPPL" if it.get("_kind") == "supplement" else "RELEASE"
        else:
            cat, noise, tag = classify_press(it, cname)
        n_company += cat == "company"
        n_noise += noise
        # Year divider so a 3-year feed reads as a dated timeline.
        yr = str(it.get("date", ""))[:4]
        if yr and yr != cur_year:
            cur_year = yr
            rows += f'<div class="nf-yr" data-yr="{esc(yr)}">{esc(yr)}</div>'
        sc = {"bullish": "up", "bearish": "dn"}.get(it.get("sentiment", ""), "dim")
        head = (f'<a href="{esc(it["url"])}" target="_blank">{esc(it["headline"])}</a>'
                if it.get("url") else esc(it.get("headline", "")))
        sent = (it.get("sentiment") or "")[:4]
        if cat == "company" and tag == "NEWS":
            badge = '<span class="tag" style="font-size:9.5px;padding:1px 6px;margin:0 5px 0 0">NEWS</span>'
        elif cat == "company":
            pill = "pill a" if tag == "SUPPL" else "pill g"
            badge = f'<span class="{pill}" style="font-size:9.5px;padding:1px 6px;margin-right:5px">{tag}</span>'
        elif tag:
            badge = f'<span class="tag" style="font-size:9.5px;padding:1px 6px;margin:0 5px 0 0">{esc(tag)}</span>'
        else:
            badge = ""
        rowstyle = ' style="opacity:.5"' if noise else ""
        rows += (f'<div class="nf-i" data-cat="{cat}" data-noise="{1 if noise else 0}"{rowstyle}>'
                 f'<span class="nf-d">{esc(str(it.get("date",""))[5:])}</span>'
                 f'<span class="{sc}" style="font-size:10px;text-transform:uppercase;width:34px;flex:0 0 auto">{esc(sent)}</span>'
                 f'<span class="nf-h">{badge}{head} <span class="dim" style="font-size:11px">{esc(it.get("source",""))}</span></span></div>')

    if not rows:
        inner = '<p class="empty">No press / news captured for this name.</p>'
    else:
        n_total = len(items)
        n_article = n_total - n_company
        n_important = n_total - n_noise
        # Land on "Important" only when there's something to show there;
        # otherwise default to "Show all" so the feed isn't blank (the badges
        # still mark why each item is low-signal).
        q_def = "important" if n_important > 0 else "all"
        imp_on = " on" if q_def == "important" else ""
        all_on = " on" if q_def == "all" else ""
        controls = (
            '<div style="display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-bottom:11px">'
            '<span class="dim" style="font-size:11px">source</span>'
            f'<button class="btn nf-f on" data-pf="src" data-v="all" onclick="psrc(this,\'all\')">All ({n_total})</button>'
            f'<button class="btn nf-f" data-pf="src" data-v="company" onclick="psrc(this,\'company\')">Company releases ({n_company})</button>'
            f'<button class="btn nf-f" data-pf="src" data-v="article" onclick="psrc(this,\'article\')">Articles ({n_article})</button>'
            '<span class="dim" style="font-size:11px;margin-left:6px">quality</span>'
            f'<button class="btn nf-f{imp_on}" data-pf="q" data-v="important" onclick="pq(this,\'important\')">Important ({n_important})</button>'
            f'<button class="btn nf-f{all_on}" data-pf="q" data-v="all" onclick="pq(this,\'all\')">Show all ({n_total})</button>'
            '<span class="dim" style="font-size:11px;margin-left:auto"><b id="pcount">0</b> shown</span>'
            '</div>')
        empty = ('<p class="empty" id="pempty" style="display:none">No items in this filter — '
                 'for some names the whole feed is 13F / legal / insider churn. Try <b>Show all</b>.</p>')
        script = (
            "<script>(function(){var src='all',q='%s';"
            "function ap(){var n=0,curYr=null,has=false;"
            "function flush(){if(curYr)curYr.style.display=has?'':'none';}"
            "[].forEach.call(document.querySelectorAll('#pressfeed > *'),function(el){"
            "if(el.classList.contains('nf-yr')){flush();curYr=el;has=false;return;}"
            "var a=(src=='all'||el.dataset.cat==src),b=(q=='all'||el.dataset.noise=='0'),v=a&&b;"
            "el.style.display=v?'':'none';if(v){n++;has=true;}});flush();"
            "var c=document.getElementById('pcount');if(c)c.textContent=n;"
            "var e=document.getElementById('pempty');if(e)e.style.display=n?'none':'';}"
            "window.psrc=function(btn,v){src=v;[].forEach.call(document.querySelectorAll('[data-pf=src]'),"
            "function(x){x.classList.remove('on')});btn.classList.add('on');ap();};"
            "window.pq=function(btn,v){q=v;[].forEach.call(document.querySelectorAll('[data-pf=q]'),"
            "function(x){x.classList.remove('on')});btn.classList.add('on');ap();};ap();})();</script>") % q_def
        inner = controls + '<div id="pressfeed" class="nf">' + rows + '</div>' + empty + script

    body = (_co_header(ticker, d, stamp) + company_tabs(ticker, "press")
            + '<div class="grid">' + panel("Press releases & news", inner, "news", ticker, full=True) + '</div>')
    return layout(ticker + " press", body, ticker)


def view_decks(ticker):
    d, stamp = load_result(ticker)
    steps = cache_steps(ticker)
    sd = ((_safe_load(steps["slide_decks"][0]) or {}).get("output") if "slide_decks" in steps else None) or {}
    decks = sd.get("decks") or []
    digests = sd.get("digests") or []
    parts = ""
    if decks:
        rows = ""
        for dk in decks:
            url = dk.get("source_url") or ""
            title = dk.get("title") or dk.get("deck_type") or "deck"
            link = f'<a href="{esc(url)}" target="_blank">{esc(title)}</a>' if url else esc(title)
            badge = ' <span class="tag" style="margin:0">analyzed</span>' if dk.get("analyzed") else ""
            rows += (f'<tr><td>{link}{badge}</td><td class="dim">{esc(dk.get("deck_type",""))}</td>'
                     f'<td class="dim">{esc(str(dk.get("date") or "")[:10])}</td>'
                     f'<td class="num">{dk.get("page_count") or "-"}</td>'
                     f'<td class="dim">{esc(dk.get("source",""))}</td></tr>')
        parts += panel("Available decks", '<table><thead><tr><th>Presentation</th><th>Type</th><th>Date</th>'
                       '<th class="num">Pages</th><th>Source</th></tr></thead><tbody>' + rows + '</tbody></table>'
                       '<p class="muted" style="font-size:11px;margin:7px 0 0">Click a title to open the PDF. '
                       'Decks marked analyzed also have a breakdown below.</p>', "slide_decks", ticker, full=True)
    if digests:
        inner = "".join('<details class="subcard"><summary>'
                        + esc((dg.get("deck_type", "deck") + "  ·  " + str(dg.get("page_count", "")) + " pp"))
                        + '</summary>' + render_value(dg) + "</details>" for dg in digests)
        parts += panel("Deck analysis", inner, "slide_decks", ticker, full=True)
    if not parts:
        parts = panel("Investor presentations",
                      '<p class="empty">No investor decks found. The finder now reaches the IR site and recognizes the '
                      'link patterns, so 0 here means the company does not publish a downloadable deck (re-run to refresh).</p>',
                      "slide_decks", ticker, full=True)
    body = _co_header(ticker, d, stamp) + company_tabs(ticker, "decks") + '<div class="grid">' + parts + '</div>'
    return layout(ticker + " decks", body, ticker)


# --------------------------------------------------------------------------
# Live quotes — a daemon thread keeps current prices + today's intraday fresh
# (yfinance, batched) so the tape / price stat / price chart tick live without
# slowing page loads. Fundamentals (estimates, transcripts, ownership) stay
# cached — they only change on earnings, and live-fetching them would be slow.
# --------------------------------------------------------------------------
_LIVE = {}            # ticker -> {"price", "change_pct", "ts"}
_LIVE_INTRADAY = {}   # ticker -> [["YYYY-MM-DDThh:mm", close], ...] (today)
_LIVE_LOCK = threading.Lock()
_LIVE_STARTED = False


def _et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta
        return datetime.utcnow() - timedelta(hours=4)  # rough ET fallback


def _market_open():
    now = _et_now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (16 * 60)


def _live_tickers():
    ts = set(list_results().keys())
    q = _safe_load(os.path.join(DATA, "quotes.json")) or {}
    ts |= set((q.get("quotes") or {}).keys())
    return sorted(t for t in ts if t and str(t).isascii())


def _yf_closes(df, t, one):
    return (df["Close"] if one else df[t]["Close"]).dropna()


def _refresh_live_quotes(tickers):
    import yfinance as yf
    df = yf.download(tickers, period="2d", interval="1d", progress=False,
                     group_by="ticker", threads=True)
    out, ts, one = {}, datetime.now().isoformat(timespec="seconds"), len(tickers) == 1
    for t in tickers:
        try:
            closes = _yf_closes(df, t, one)
            if len(closes) < 1:
                continue
            price = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else price
            chg = (price / prev - 1) * 100 if prev else 0.0
            out[t] = {"price": round(price, 2), "change_pct": round(chg, 2), "ts": ts}
        except Exception:
            continue
    return out


def _refresh_live_intraday(tickers):
    import yfinance as yf
    df = yf.download(tickers, period="1d", interval="5m", progress=False,
                     group_by="ticker", threads=True)
    out, one = {}, len(tickers) == 1
    for t in tickers:
        try:
            closes = _yf_closes(df, t, one)
            pts = [[idx.strftime("%Y-%m-%dT%H:%M"), round(float(c), 2)] for idx, c in closes.items()]
            if pts:
                out[t] = pts
        except Exception:
            continue
    return out


def _live_loop():
    while True:
        try:
            tickers = _live_tickers()
            if tickers:
                q = _refresh_live_quotes(tickers)
                if q:
                    with _LIVE_LOCK:
                        _LIVE.update(q)
                    try:  # persist so the server-side tape render is fresh too
                        with _LIVE_LOCK:
                            snap = {t: {"price": v["price"], "change_pct": v["change_pct"]}
                                    for t, v in _LIVE.items()}
                        with open(os.path.join(DATA, "quotes.json"), "w", encoding="utf-8") as fh:
                            json.dump({"quotes": snap, "fetched_at": datetime.now().isoformat(timespec="seconds")}, fh)
                    except Exception:
                        pass
                if _market_open():
                    intr = _refresh_live_intraday(tickers)
                    if intr:
                        with _LIVE_LOCK:
                            _LIVE_INTRADAY.update(intr)
        except Exception:
            pass
        time.sleep(30 if _market_open() else 300)


def start_live():
    global _LIVE_STARTED
    if _LIVE_STARTED:
        return
    _LIVE_STARTED = True
    threading.Thread(target=_live_loop, daemon=True, name="live-quotes").start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, folder, name):
        safe = os.path.basename(urllib.parse.unquote(name))
        path = os.path.join(folder, safe)
        if not os.path.isfile(path):
            return self._send("<h1>404</h1>", code=404)
        with open(path, "rb") as fh:
            data = fh.read()
        ct = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if safe.endswith(".docx")
              else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if safe.endswith(".xlsx")
              else "text/plain; charset=utf-8" if safe.endswith(".md")
              else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        disp = "inline" if safe.endswith(".md") else "attachment"
        self.send_header("Content-Disposition", '%s; filename="%s"' % (disp, safe))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        # Live-quote API (served from the in-memory snapshot — instant, no fetch).
        if path == "/api/quotes":
            with _LIVE_LOCK:
                return self._json({t: {"price": v["price"], "change_pct": v["change_pct"]}
                                   for t, v in _LIVE.items()})
        if path == "/api/intraday":
            tk = (q.get("t") or [""])[0].upper()
            with _LIVE_LOCK:
                return self._json({"intraday": _LIVE_INTRADAY.get(tk) or []})
        try:
            _RESULT_CACHE.clear()
            _STEPS_CACHE.clear()
            if path in ("/", ""):
                return self._send(home_page((q.get("q") or [""])[0]))
            if path == "/fn":
                return self._send(functions_page())
            if path.startswith("/fn/"):
                return self._send(function_inspector(path[4:], (q.get("ticker") or [None])[0]))
            if path == "/compare":
                return self._send(compare_page(q.get("t") or []))
            if path.startswith("/research/"):
                return self._send(view_research_item(path[len("/research/"):]))
            if path == "/research":
                return self._send(research_page())
            if path == "/macro":
                return self._send(macro_page())
            if path.startswith("/co/") or path.startswith("/t/"):
                rest = path.split("/", 2)[2]
                tk, _sep, tab = rest.partition("/")
                if tab == "search":
                    return self._send(view_search(tk, (q.get("q") or [""])[0],
                                                  (q.get("mode") or ["search"])[0]))
                _views = {"estimates": view_estimates, "ownership": view_ownership,
                          "transcripts": view_transcripts, "press": view_press, "decks": view_decks,
                          "research": view_research}
                if tab in _views:
                    return self._send(_views[tab](tk))
                return self._send(company_page(tk, (q.get("run") or [None])[0]))
            if path.startswith("/report/"):
                return self._file(REPORTS, path[len("/report/"):])
            if path.startswith("/export/"):
                return self._file(EXPORTS, path[len("/export/"):])
            return self._send("<h1>404</h1><a href='/'>home</a>", code=404)
        except Exception:
            import traceback
            return self._send("<h1>500</h1><pre>%s</pre>" % esc(traceback.format_exc()), code=500)


def main():
    os.makedirs(DATA, exist_ok=True)  # fresh clone: data/ is gitignored
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    start_live()  # background thread keeps prices live during market hours
    url = "http://%s:%d" % (HOST, PORT)
    res = list_results()
    print("Investment Workbench terminal")
    print("  %d runs · %d tickers · %d functions" % (sum(len(v) for v in res.values()), len(res), len(FUNCTIONS)))
    print("  -> %s   (Ctrl+C to stop)" % url)
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


if __name__ == "__main__":
    main()
