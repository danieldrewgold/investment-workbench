"""
Reported quarterly P&L lines from earnings press releases, on the adjusted basis.

Deterministic: reads the release's "Three months ended" income statement and its
non-GAAP reconciliation tables using the label patterns in the schema config, so
the numbers are exact and free. Each release gives the current quarter and the
prior-year quarter; a quarter's own release wins when two releases overlap.

Adjusted basis (consensus is non-GAAP): G&A uses the company's adjusted G&A,
impairment is net of impairment add-backs, interest is net of investment
add-backs, and the tax rate is the adjusted rate. Each quarter is checked by
rebuilding adjusted net income and comparing it with the reported figure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent / "schemas"
_MONTH_Q = {"march": 1, "june": 2, "september": 3, "december": 4,
            "mar": 1, "jun": 2, "sep": 3, "dec": 4}


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _nums(row: list) -> list[float]:
    out = []
    for cell in row[1:]:
        c = str(cell).strip().replace("$", "").replace("%", "").strip()
        if c in ("", "—"):
            continue
        if c in ("-", "–"):
            out.append(0.0)
            continue
        neg = c.startswith("(") and c.endswith(")")
        c = c.strip("()").replace(",", "")
        try:
            v = float(c)
        except ValueError:
            continue
        out.append(-v if neg else v)
    return out


def _quarters_from_header(rows: list) -> tuple[str, str] | None:
    """('Q2 2026', 'Q2 2025') from a 'Three months ended June 30,' header and a year row."""
    head = " ".join(str(c) for r in rows[:2] for c in r).lower()
    m = re.search(r"three months ended\s+([a-z]+)", head)
    years = [c for r in rows[:3] for c in r if re.fullmatch(r"\d{4}", str(c).strip())]
    if not m or len(years) < 2 or m.group(1) not in _MONTH_Q:
        return None
    q = _MONTH_Q[m.group(1)]
    return f"Q{q} {years[0]}", f"Q{q} {years[1]}"


def _find_table(tables: list, must_label: str, header: str | None = None):
    rx = re.compile(must_label, re.I)
    for t in tables or []:
        rows = t.get("rows") or []
        text = " ".join(str(c) for r in rows[:3] for c in r)
        if header and header.lower() not in text.lower() and \
                header.lower() not in " ".join(t.get("columns") or []).lower():
            continue
        if any(rx.search(str(r[0]).strip()) for r in rows if r):
            return rows
    return None


def parse_release(rel: dict, schema: dict) -> dict:
    """{quarter: {line: $M, ...}} for the current and prior-year quarter of one release."""
    cfg = schema["income_statement"]
    tables = rel.get("tables") or []
    rows = _find_table(tables, cfg["lines"]["revenue"], cfg["table_header"])
    if not rows:
        return {}
    qs = _quarters_from_header(rows)
    if not qs:
        return {}
    cur, prior = {}, {}
    section = ""
    for r in rows:
        if not r:
            continue
        label = str(r[0]).strip()
        if re.search(cfg["eps_section"], label, re.I):
            section = "eps"
        elif re.search(cfg["shares_section"], label, re.I):
            section = "shares"
        n = _nums(r)
        if re.search(cfg["diluted_label"], label, re.I) and len(n) >= 2:
            key = "eps_gaap" if section == "eps" else "diluted_shares_m"
            scale = 1.0 if key == "eps_gaap" else 1 / 1000
            cur[key], prior[key] = n[0] * scale, n[1] * scale
            continue
        for key, pat in cfg["lines"].items():
            if key not in cur and re.search(pat, label, re.I) and len(n) >= 4:
                cur[key], prior[key] = n[0] / 1000, n[2] / 1000
                break

    adj = schema["adjustments"]
    ni_rows = _find_table(tables, adj["adjusted_net_income_label"])
    if ni_rows:
        imp_add = [0.0, 0.0]
        int_add = [0.0, 0.0]
        line_add = {k: [0.0, 0.0] for k in adj.get("cost_line_keywords", {})}
        for r in ni_rows:
            if not r:
                continue
            label = str(r[0]).strip().lower()
            n = _nums(r)
            if len(n) < 2:
                continue
            if re.search(adj["adjusted_net_income_label"], label, re.I):
                cur["net_income_adj"], prior["net_income_adj"] = n[0] / 1000, n[1] / 1000
            elif re.search(adj["adjusted_eps_label"], label, re.I):
                cur["eps_adj"], prior["eps_adj"] = n[0], n[1]
            elif re.search(adj["total_adjustments_label"], label, re.I):
                cur["total_adj"], prior["total_adj"] = n[0] / 1000, n[1] / 1000
            elif any(k in label for kws in adj.get("cost_line_keywords", {}).values() for k in kws):
                for line, kws in adj["cost_line_keywords"].items():
                    if any(k in label for k in kws):
                        line_add[line] = [line_add[line][0] + n[0] / 1000, line_add[line][1] + n[1] / 1000]
                        break
            elif any(k in label for k in adj["impairment_keywords"]):
                imp_add = [imp_add[0] + n[0] / 1000, imp_add[1] + n[1] / 1000]
            elif any(k in label for k in adj["interest_keywords"]):
                int_add = [int_add[0] + n[0] / 1000, int_add[1] + n[1] / 1000]
        cur["impairment_addback"], prior["impairment_addback"] = imp_add
        cur["interest_addback"], prior["interest_addback"] = int_add
        for line, (c_, p_) in line_add.items():
            cur[f"{line}_addback"], prior[f"{line}_addback"] = c_, p_
    ga_rows = _find_table(tables, adj["adjusted_g_and_a_label"])
    if ga_rows:
        for r in ga_rows:
            if r and re.search(adj["adjusted_g_and_a_label"], str(r[0]).strip(), re.I):
                n = _nums(r)
                if len(n) >= 2:
                    cur["g_and_a_adj"], prior["g_and_a_adj"] = n[0] / 1000, n[1] / 1000
    tax_rows = _find_table(tables, adj["adjusted_tax_rate_label"])
    if tax_rows:
        for r in tax_rows:
            if r and re.search(adj["adjusted_tax_rate_label"], str(r[0]).strip(), re.I):
                n = _nums(r)
                if len(n) >= 2:
                    cur["tax_rate_adj"], prior["tax_rate_adj"] = n[0], n[1]
    src = f"press release {rel.get('filing_date', '')}"
    cur["source"], prior["source"] = src, src
    return {qs[0]: cur, qs[1]: prior}


def finalize(q: dict) -> dict:
    """Add adjusted lines and the rebuild check to one quarter's record."""
    need = ("revenue", "food", "labor", "occupancy", "other_opex", "d_and_a", "preopening",
            "impairment", "interest", "pretax")
    if not all(k in q for k in need):
        q["complete"] = False
        return q
    q.setdefault("g_and_a_adj", q.get("g_and_a"))
    for line in ("food", "labor", "occupancy", "other_opex"):
        q[line] -= q.get(f"{line}_addback", 0.0)    # non-GAAP charges booked inside a cost line
    q["impairment_adj"] = q["impairment"] - q.get("impairment_addback", 0.0)
    q["interest_adj"] = q["interest"] + q.get("interest_addback", 0.0)
    costs = q["food"] + q["labor"] + q["occupancy"] + q["other_opex"]
    q["rlm_pct"] = (1 - costs / q["revenue"]) * 100
    q["pretax_adj"] = (q["revenue"] - costs - q["g_and_a_adj"] - q["d_and_a"] - q["preopening"]
                       - q["impairment_adj"] + q["interest_adj"])
    if "tax_rate_adj" in q:
        rebuilt = q["pretax_adj"] * (1 - q["tax_rate_adj"] / 100)
        q["rebuilt_net_income_adj"] = rebuilt
        if q.get("net_income_adj"):
            q["rebuild_error_pct"] = (rebuilt / q["net_income_adj"] - 1) * 100
    q["complete"] = True
    return q


def quarterly_lines(press_releases: list, schema: dict) -> dict:
    """All quarters available, oldest first. A quarter's own release wins on overlap."""
    by_q: dict = {}
    own: set = set()
    for rel in sorted(press_releases or [], key=lambda r: r.get("filing_date") or ""):
        parsed = parse_release(rel, schema)
        for i, (quarter, rec) in enumerate(parsed.items()):
            if i == 0:
                by_q[quarter] = rec
                own.add(quarter)
            elif quarter not in own:
                by_q[quarter] = rec
    key = lambda q: (int(q.split()[1]), int(q[1]))
    return {q: finalize(by_q[q]) for q in sorted(by_q, key=key)}
