"""
Management statement ledger: what management said, classified by how much
weight it can bear, plus behavior signals.

Categories:
  (a) reported fact             a figure for a completed period
  (b) formal guidance           a quantified forward commitment
  (c) action backed by money    capex, buybacks, pricing moves, insider buying
  (d) statement against interest admitting headwinds, reinvesting savings,
                                pricing below inflation
  (e) self-serving narrative    aspirations, excuses for misses, "conservative"
                                framing, unquantified benefits

Rules enforced here and in decide.py:
  - (e) cannot support a conclusion unless independent data confirms it.
  - An excuse for a miss stays "unverified" until a LATER quarter's reported
    figure confirms or refutes it. A verification that doesn't cite a real,
    later reported figure is downgraded to unverified in code.

Signals (dodged questions, dropped metrics, tone shifts, credibility patterns)
come from the existing transcript sub-analyses; nothing here replaces them.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from research.call.llm import call_json, OPUS, LLMError

CACHE_DIR = Path("data/mgmt_ledger")
N_CALLS = 4
CATS = {"a": "reported fact", "b": "formal guidance", "c": "action backed by money",
        "d": "statement against interest", "e": "self-serving narrative"}

_SYSTEM = """You audit what a company's management said on its earnings calls. Management is a
biased source: it is paid to sell the stock and the plan. Classify the statements that bear
on the investment case (estimates, margins, demand, capital, risks). Skip pleasantries,
boilerplate and restatements.

For each statement give:
  id: "S01", "S02", ... in order
  quarter: the call it came from (e.g. "Q2 2026")
  speaker: name or role
  quote: verbatim, at most 40 words
  claim: one plain sentence saying what is being claimed
  category: one of
    a = reported fact (a figure for a completed period)
    b = formal guidance (a quantified forward commitment)
    c = action backed by money (capex, buybacks, price increases taken, insider buying)
    d = statement against interest (admits a headwind, gives away a benefit, prices below
        cost inflation, reinvests savings rather than keeping them)
    e = self-serving narrative (aspiration, excuse for a miss, "conservative" framing,
        a benefit claimed but not quantified, promises about the future without numbers)
  category_reason: one short sentence
  topic: the driver it bears on (e.g. comps, traffic, pricing, food cost, labor, margin,
         unit growth, capital allocation, food safety)
  quantified: true if it carries a number
  excuse_for_miss: true if it explains away a shortfall
  verification: for every (e) statement and every excuse, check the REPORTED FIGURES table.
    Only figures for periods AFTER the statement's quarter can confirm or refute it.
    {"status": "confirmed" | "refuted" | "unverified",
     "metric": "metric key from the table or empty", "period": "period or empty",
     "note": "what the later figure shows, or why it stays unverified"}
    For a, b, c, d statements set status "n/a".

Classify what the statement IS, not whether you agree with it. When a statement mixes
a fact with spin, split it into two statements. Use plain punctuation: no em dashes.

Return JSON only: {"statements": [ ... ]}"""


def _mgmt_turns(q: dict) -> str:
    """Management-only text of one call, with the question that prompted each answer."""
    sp = q.get("speakers") or []
    qa = q.get("qa_start")
    out, last_q = [], ""
    for i, s in enumerate(sp):
        title = (s.get("title") or "").lower()
        name = s.get("name") or ""
        is_analyst = "analyst" in title or (qa is not None and i >= qa and title == "")
        is_operator = "operator" in title or name.lower() == "operator"
        if is_operator:
            continue
        if is_analyst:
            last_q = (s.get("text") or "")[:300]
            continue
        prefix = f"[Q: {last_q}]\n" if last_q and qa is not None and i > qa else ""
        out.append(f"{prefix}{name} ({s.get('title') or 'management'}): {s.get('text', '')}")
        last_q = ""
    if not out and q.get("text"):
        out.append(q["text"])
    return "\n\n".join(out)


def _reported_table(reported: list, max_rows: int = 120) -> str:
    rows = sorted(reported, key=lambda r: (r.get("period", ""), r.get("metric", "")))[-max_rows:]
    return "\n".join(f"  {r['metric']} | {r['period']} | {r['value']}" for r in rows)


def _period_key(p: str) -> tuple[int, int]:
    m = re.match(r"Q([1-4]) (\d{4})", p or "")
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.match(r"FY(\d{4})", p or "")
    return (int(m.group(1)), 4) if m else (0, 0)


def _enforce_verification(statements: list, reported: list) -> None:
    have = {(r["metric"], r["period"]) for r in reported}
    for s in statements:
        v = s.get("verification") or {}
        if s.get("category") != "e" and not s.get("excuse_for_miss"):
            s["verification"] = {"status": "n/a"}
            continue
        status = v.get("status")
        if status in ("confirmed", "refuted"):
            key = (v.get("metric", ""), v.get("period", ""))
            later = _period_key(v.get("period", "")) > _period_key(s.get("quarter", ""))
            if key not in have or not later:
                v["status"] = "unverified"
                v["note"] = ("downgraded: verification must cite a reported figure for a later period; "
                             + (v.get("note") or ""))
        else:
            v["status"] = "unverified"
        s["verification"] = v


def classify_statements(ticker: str, raw_quarters: list, reported: list) -> dict:
    calls = sorted(raw_quarters or [], key=lambda q: (q.get("year") or 0, q.get("quarter") or 0))[-N_CALLS:]
    if not calls:
        return {"statements": [], "error": "no transcripts"}
    parts = [f"===== Q{q.get('quarter')} {q.get('year')} CALL ({q.get('date', '')}) =====\n{_mgmt_turns(q)}"
             for q in calls]
    user = ("REPORTED FIGURES (from press releases; use only for verification):\n"
            f"{_reported_table(reported)}\n\n" + "\n\n".join(parts))
    h = hashlib.sha1(user.encode("utf-8", "ignore")).hexdigest()[:16]
    cache = CACHE_DIR / f"{ticker.upper()}_{h}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        out = call_json(_SYSTEM, user, model=OPUS, effort="medium", max_tokens=48000, timeout=1500)
    except LLMError as e:
        return {"statements": [], "error": str(e)}
    st = out.get("statements") or []
    for s in st:
        s["category"] = (s.get("category") or "").strip().lower()[:1]
    _enforce_verification(st, reported)
    result = {"statements": st, "calls": [f"Q{q.get('quarter')} {q.get('year')}" for q in calls]}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def collect_signals(digest: dict | None) -> dict:
    """Behavior signals from the existing transcript sub-analyses."""
    sub = (digest or {}).get("subagents") or {}
    d = lambda name: (sub.get(name) or {}).get("data") or {}
    qa, mh, tt, gt, ud = d("qanda_analyzer"), d("metrics_highlighted"), d("tone_tracker"), \
        d("guidance_tracker"), d("unusual_disclosures")
    dodges = [{"id": f"D{i + 1:02d}", "quarter": x.get("quarter"), "question": (x.get("question_verbatim") or "")[:260],
               "answer": (x.get("answer_verbatim") or "")[:260],
               "why": x.get("why_its_a_dodge") or x.get("dodge_type") or x.get("evasion_type") or ""}
              for i, x in enumerate(qa.get("dodged_questions") or [])]
    dropped = [{"id": f"M{i + 1:02d}", "metric": x.get("metric"), "last_mentioned": x.get("last_mentioned_quarter"),
                "absent_since": (x.get("quarters_absent") or [None])[0],
                "read": x.get("analyst_interpretation", "")}
               for i, x in enumerate(mh.get("dropped_metrics") or [])]
    tone = [{"quarter": x.get("quarter"), "tone": x.get("overall_tone")} for x in tt.get("per_quarter_tone") or []]
    return {
        "dodged_questions": dodges,
        "dropped_metrics": dropped,
        "tone_by_quarter": tone,
        "tone_trajectory": tt.get("tone_trajectory_summary") or (digest or {}).get("tone_trajectory", ""),
        "language_downgrades": tt.get("language_downgrades") or [],
        "credibility_patterns": gt.get("credibility_patterns") or [],
        "new_risk_language": ud.get("new_risk_language") or [],
    }


def build(ticker: str, raw_quarters: list, reported: list, digest: dict | None) -> dict:
    led = classify_statements(ticker, raw_quarters, reported)
    led["signals"] = collect_signals(digest)
    st = led.get("statements") or []
    led["counts"] = {c: sum(1 for s in st if s.get("category") == c) for c in CATS}
    return led


def render_block(led: dict) -> str:
    if not led:
        return ""
    st = led.get("statements") or []
    lines = ["=== MANAGEMENT LEDGER (management is a biased source; statements are hypotheses) ===",
             "Categories: a reported fact | b formal guidance | c action backed by money | "
             "d statement against interest | e self-serving narrative (cannot support a conclusion "
             "without independent confirmation)",
             f"Calls covered: {', '.join(led.get('calls') or [])}. Counts: "
             + ", ".join(f"{c}={n}" for c, n in (led.get('counts') or {}).items())]
    for s in st:
        v = s.get("verification") or {}
        ver = f" | verification: {v.get('status')}" + (f" ({v.get('note')})" if v.get("note") else "") \
            if v.get("status") not in (None, "n/a") else ""
        lines.append(f"[{s.get('id')}] ({s.get('category')}) {s.get('quarter')} {s.get('speaker')}: "
                     f"{s.get('claim')} Quote: \"{s.get('quote')}\"{' [excuse for miss]' if s.get('excuse_for_miss') else ''}{ver}")
    sig = led.get("signals") or {}
    if sig.get("dodged_questions"):
        lines.append("Dodged analyst questions:")
        for x in sig["dodged_questions"]:
            lines.append(f"  [{x['id']}] {x['quarter']}: Q: {x['question']} | A: {x['answer']}")
    if sig.get("dropped_metrics"):
        lines.append("Metrics management stopped disclosing:")
        for x in sig["dropped_metrics"]:
            lines.append(f"  [{x['id']}] {x['metric']} (last {x['last_mentioned']}, absent since {x['absent_since']}): {x['read']}")
    if sig.get("tone_by_quarter"):
        lines.append("Tone by quarter: " + ", ".join(f"{t['quarter']} {t['tone']}" for t in sig["tone_by_quarter"]))
    if sig.get("tone_trajectory"):
        lines.append(f"Tone trajectory: {sig['tone_trajectory']}")
    if sig.get("credibility_patterns"):
        lines.append("Credibility patterns: " + json.dumps(sig["credibility_patterns"], default=str)[:1500])
    return "\n".join(lines)
