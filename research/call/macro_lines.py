"""
Macro series mapped to the specific cost line each one is used against.

The schema config lists, per cost line, the series that measure that line's input
costs (food: beef, poultry, processed foods; labor: restaurant wages; occupancy:
nonresidential rents; other: CPI). Broad indices like PPI all commodities are not
used for cost lines: they mix energy and metals that a restaurant doesn't buy.
"""

from __future__ import annotations

from ingestion.loaders.fred_macro_loader import _fetch_series_csv


def fetch(schema: dict) -> dict:
    """{cost_line: [{id, label, latest, date, yoy_pct}]}; failures are recorded, not raised."""
    out: dict = {}
    for line, series in (schema.get("macro_by_cost_line") or {}).items():
        rows = []
        for s in series:
            rec = {"id": s["id"], "label": s["label"]}
            try:
                obs = _fetch_series_csv(s["id"], lookback_days=430)
                obs = [(d, v) for d, v in obs if v is not None]
                if len(obs) >= 13:
                    (d1, v1), (_, v0) = obs[-1], obs[-13]
                    rec.update({"latest": v1, "date": d1, "yoy_pct": (v1 / v0 - 1) * 100})
                else:
                    rec["error"] = "not enough observations"
            except Exception as e:
                rec["error"] = f"{type(e).__name__}"
            rows.append(rec)
        out[line] = rows
    return out


def render_block(macro: dict, guides: list | None = None) -> str:
    names = {"food": "Food, beverage and packaging", "labor": "Labor", "occupancy": "Occupancy",
             "other_opex": "Other operating costs", "price": "Menu price (revenue side)"}
    L = ["=== MACRO BY COST LINE (each series is used only against the line it measures) ==="]
    for line, rows in macro.items():
        parts = [f"{r['label']} {r['yoy_pct']:+.1f}% YoY ({r['date']})" if "yoy_pct" in r
                 else f"{r['label']} unavailable" for r in rows]
        L.append(f"{names.get(line, line)}: " + "; ".join(parts))
    for g in guides or []:
        L.append(f"Company guide [{g.get('id')}]: {g.get('label')} {g.get('period')} guided {g.get('low')} to "
                 f"{g.get('high')} ({g.get('source')})")
    return "\n".join(L)
