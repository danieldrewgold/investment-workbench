"""
Margin bridges computed in code, so they always foot.

The model proposes a bridge as a start margin plus components, each with a
method and its inputs. This module computes every component from its inputs,
sums them, and sets end = start + total. Stated numbers from the model are
kept only to flag disagreements; they never reach the output.

Methods (all margins and rates in percent, results in basis points):

  operating_leverage  revenue change at a given flow-through. Margin moves by
                      (flow_through - current_margin) x revenue_change, because
                      the new revenue arrives at the flow-through margin while
                      the existing base stays at the current margin.
  price_vs_cost       price increase against inflation on part of the cost base:
                      new margin = 1 - [cb(1+c) + (other costs)] / (1+p)
  stated              a component given directly in bps (e.g. a disclosed
                      one-off); must carry a basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BridgeError(ValueError):
    pass


def operating_leverage_bps(flow_through_pct: float, current_margin_pct: float,
                           revenue_change_pct: float) -> float:
    """Margin change from revenue growth at a given incremental flow-through.

    New margin = (m*R + f*dR) / (R + dR); the change is (f - m) * g / (1 + g).
    """
    f, m, g = flow_through_pct / 100, current_margin_pct / 100, revenue_change_pct / 100
    return (f - m) * g / (1 + g) * 10000


def price_vs_cost_bps(price_pct: float, cost_inflation_pct: float,
                      cost_base_pct_of_revenue: float, current_margin_pct: float) -> float:
    """Margin change when prices rise p% and costs worth cb of revenue inflate c%.

    Costs outside the inflating base are held flat in dollars.
    """
    p, c = price_pct / 100, cost_inflation_pct / 100
    cb, m = cost_base_pct_of_revenue / 100, current_margin_pct / 100
    other = (1 - m) - cb
    if other < -1e-9:
        raise BridgeError("cost base exceeds total costs (cost_base > 100% - margin)")
    new_margin = 1 - (cb * (1 + c) + other) / (1 + p)
    return (new_margin - m) * 10000


@dataclass
class BridgeComponent:
    name: str
    method: str
    inputs: dict = field(default_factory=dict)
    bps: float = 0.0
    stated_bps: float | None = None
    basis: str = ""


@dataclass
class MarginBridge:
    name: str
    metric: str
    period: str
    start_pct: float
    start_basis: str
    components: list
    total_bps: float = 0.0
    end_pct: float = 0.0
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "metric": self.metric, "period": self.period,
            "start_pct": round(self.start_pct, 2), "start_basis": self.start_basis,
            "components": [{"name": c.name, "method": c.method, "inputs": c.inputs,
                            "bps": round(c.bps, 1), "stated_bps": c.stated_bps, "basis": c.basis}
                           for c in self.components],
            "total_bps": round(self.total_bps, 1), "end_pct": round(self.end_pct, 2),
            "warnings": self.warnings,
        }


def _num(d: dict, k: str) -> float:
    try:
        return float(d[k])
    except (KeyError, TypeError, ValueError):
        raise BridgeError(f"missing or non-numeric input '{k}'")


def compute_component(comp: dict, start_pct: float) -> BridgeComponent:
    method = (comp.get("method") or "").strip()
    inputs = dict(comp.get("inputs") or {})
    stated = comp.get("stated_bps", comp.get("bps"))
    try:
        stated = float(stated) if stated is not None else None
    except (TypeError, ValueError):
        stated = None
    if method == "operating_leverage":
        inputs.setdefault("current_margin_pct", start_pct)
        bps = operating_leverage_bps(_num(inputs, "flow_through_pct"),
                                     _num(inputs, "current_margin_pct"),
                                     _num(inputs, "revenue_change_pct"))
    elif method == "price_vs_cost":
        inputs.setdefault("current_margin_pct", start_pct)
        bps = price_vs_cost_bps(_num(inputs, "price_pct"), _num(inputs, "cost_inflation_pct"),
                                _num(inputs, "cost_base_pct_of_revenue"),
                                _num(inputs, "current_margin_pct"))
    elif method == "stated":
        if stated is None:
            raise BridgeError(f"component '{comp.get('name')}' is 'stated' but has no bps")
        if not (comp.get("basis") or "").strip():
            raise BridgeError(f"stated component '{comp.get('name')}' needs a basis")
        bps = stated
    else:
        raise BridgeError(f"unknown method '{method}' on component '{comp.get('name')}'")
    return BridgeComponent(name=comp.get("name", method), method=method, inputs=inputs,
                           bps=bps, stated_bps=stated, basis=comp.get("basis", ""))


def compute_bridge(spec: dict) -> MarginBridge:
    """Compute a bridge from the model's spec. Raises BridgeError on bad input."""
    try:
        start = float(spec["start_pct"])
    except (KeyError, TypeError, ValueError):
        raise BridgeError("bridge needs a numeric start_pct")
    basis = (spec.get("start_basis") or "").strip()
    if not basis:
        raise BridgeError("bridge needs start_basis (which year and which margin definition)")
    comps = [compute_component(c, start) for c in (spec.get("components") or [])]
    if not comps:
        raise BridgeError("bridge has no components")
    total = sum(c.bps for c in comps)
    b = MarginBridge(name=spec.get("name", "margin bridge"), metric=spec.get("metric", "operating margin"),
                     period=spec.get("period", ""), start_pct=start, start_basis=basis,
                     components=comps, total_bps=total, end_pct=start + total / 100)
    for c in comps:
        if c.stated_bps is not None and c.method != "stated" and abs(c.stated_bps - c.bps) > 5:
            b.warnings.append(f"{c.name}: model stated {c.stated_bps:+.0f}bp, inputs compute {c.bps:+.0f}bp")
    stated_total = spec.get("stated_total_bps")
    if stated_total is not None:
        try:
            if abs(float(stated_total) - total) > 5:
                b.warnings.append(f"model stated total {float(stated_total):+.0f}bp, components sum to {total:+.0f}bp")
        except (TypeError, ValueError):
            pass
    return b


def check_foots(b: MarginBridge, tol_bps: float = 0.05) -> list[str]:
    """Return problems if the bridge doesn't foot. Empty list means it foots."""
    problems = []
    s = sum(c.bps for c in b.components)
    if abs(s - b.total_bps) > tol_bps:
        problems.append(f"components sum to {s:.2f}bp but total is {b.total_bps:.2f}bp")
    if abs(b.start_pct + b.total_bps / 100 - b.end_pct) > tol_bps / 100:
        problems.append(f"start {b.start_pct:.2f}% + change {b.total_bps:.1f}bp != end {b.end_pct:.2f}%")
    return problems
