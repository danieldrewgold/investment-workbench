"""
Scenario engine: bull / base / bear FY+1 EPS built bottom-up from reported lines.
All math is here; the model only proposes driver values, multiples and probabilities.

Base year (FY0E): H1 actual + H2 estimated. H2 starts from the prior year's H2 by
quarter: revenue grows by the H2 comp plus new-unit revenue; each cost ratio is the
prior-year ratio plus the H1 year-over-year change times `delta_persistence`
(1.0 = the H1 change persists); adjusted G&A grows at its H1 rate; D&A, pre-opening
and impairment ratios move with their H1 change; interest runs at the H1 rate; the
tax rate is H1's adjusted rate; shares keep shrinking at the trailing-year pace.

Forecast year (FY1), per case, for each restaurant cost line with fixed share f:
    ratio1 = ratio0 * [ f * (1+infl)/(1+comp) + (1-f) * (1+infl)/(1+check) ] * (1-efficiency)
The variable part scales with transactions, so its ratio moves with inflation versus
average check; the fixed part is per store, so its ratio moves with inflation versus
same-store sales. comp = traffic + check. Adjusted G&A grows at its driver; D&A,
pre-opening and impairment stay at base-year ratios of revenue; interest is held;
EPS = adjusted net income / average diluted shares.

The EPS bridge swaps drivers in one group at a time, from neutral (which reproduces
the base year exactly) to the case values, so the components always sum to the change.
"""

from __future__ import annotations

COST_LINES = ("food", "labor", "occupancy", "other_opex")
RATIO_LINES = COST_LINES + ("d_and_a", "preopening", "impairment_adj")
HURDLE = 0.15


def _ratio(q: dict, line: str) -> float:
    return q[line] / q["revenue"]


def _quarter_ni(q: dict) -> float:
    return q["pretax_adj"] * (1 - q["tax_rate_adj"] / 100)


def reported_quarters_in(Q: dict, fy: int) -> int:
    k = 0
    while k < 4 and (Q.get(f"Q{k + 1} {fy}") or {}).get("complete"):
        k += 1
    return k


def build_base_year(Q: dict, fy: int, bridge: dict) -> dict:
    """FY0E = reported quarters of FY0 + the rest estimated from the prior year's same quarters.
    Raises KeyError if FY0 has no reported quarter, is already complete, or prior-year data is missing."""
    k = reported_quarters_in(Q, fy)
    if not 1 <= k <= 3:
        raise KeyError(f"FY{fy} has {k} reported quarters; the base-year build needs 1 to 3")
    act = [Q[f"Q{i} {fy}"] for i in range(1, k + 1)]
    act_p = [Q[f"Q{i} {fy - 1}"] for i in range(1, k + 1)]
    rest_p = [Q[f"Q{i} {fy - 1}"] for i in range(k + 1, 5)]
    for q in act + act_p + rest_p:
        if not q.get("complete"):
            raise KeyError("incomplete reported quarter")
    persist = float(bridge.get("delta_persistence", 1.0))
    comp, unit = float(bridge["h2_comp_pct"]) / 100, float(bridge["h2_unit_pp"]) / 100
    rev = lambda qs: sum(x["revenue"] for x in qs)
    ytd_delta = {ln: sum(x[ln] for x in act) / rev(act) - sum(x[ln] for x in act_p) / rev(act_p) for ln in RATIO_LINES}
    gna_growth = sum(x["g_and_a_adj"] for x in act) / sum(x["g_and_a_adj"] for x in act_p) - 1
    interest_q = sum(x["interest_adj"] for x in act) / k
    tax_ytd = 100 - 100 * sum(_quarter_ni(x) for x in act) / sum(x["pretax_adj"] for x in act)
    last, last_p = act[-1]["diluted_shares_m"], act_p[-1]["diluted_shares_m"]
    qtr_shrink = (last / last_p) ** 0.25
    est = []
    for j, prior in enumerate(rest_p, start=1):
        r = prior["revenue"] * (1 + comp + unit)
        e = {"label": f"Q{k + j} {fy}E", "revenue": r, "g_and_a_adj": prior["g_and_a_adj"] * (1 + gna_growth),
             "interest_adj": interest_q, "tax_rate_adj": tax_ytd, "diluted_shares_m": last * qtr_shrink ** j}
        for ln in RATIO_LINES:
            e[ln] = r * (_ratio(prior, ln) + ytd_delta[ln] * persist)
        e["pretax_adj"] = (r - sum(e[ln] for ln in COST_LINES) - e["g_and_a_adj"] - e["d_and_a"]
                           - e["preopening"] - e["impairment_adj"] + e["interest_adj"])
        est.append(e)
    quarters = act + est
    base = {"fy": fy, "reported_quarters": k, "revenue": rev(quarters),
            "g_and_a_adj": sum(x["g_and_a_adj"] for x in quarters),
            "interest_adj": sum(x["interest_adj"] for x in quarters),
            "pretax_adj": sum(x["pretax_adj"] for x in quarters),
            "net_income_adj": sum(_quarter_ni(x) for x in quarters),
            "shares": sum(x["diluted_shares_m"] for x in quarters) / 4,
            "sum_of_quarters_eps": sum(_quarter_ni(x) / x["diluted_shares_m"] for x in quarters),
            "h2_estimate": est, "h1_ratio_change_pp": {kk: round(v * 100, 2) for kk, v in ytd_delta.items()},
            "h1_gna_growth_pct": round(gna_growth * 100, 1), "assumptions": dict(bridge)}
    for ln in RATIO_LINES:
        base[ln] = sum(x[ln] for x in quarters)
    base["tax_rate"] = 100 * (1 - base["net_income_adj"] / base["pretax_adj"])
    base["eps"] = base["net_income_adj"] / base["shares"]
    base["ratios_pct"] = {ln: base[ln] / base["revenue"] * 100 for ln in RATIO_LINES}
    base["rlm_pct"] = 100 - sum(base["ratios_pct"][ln] for ln in COST_LINES)
    return base


def neutral_drivers(base: dict, schema: dict) -> dict:
    d = {k: 0.0 for k in schema["drivers"]}
    d["tax_rate_pct"] = base["tax_rate"]
    return d


def project(base: dict, drivers: dict, schema: dict, rlm_shift_bps: float = 0.0) -> dict:
    """FY1 for one set of drivers."""
    g = lambda k: float(drivers.get(k, 0.0)) / 100
    traffic, check, unit = g("traffic_pct"), g("check_pct"), g("unit_contribution_pp")
    comp = traffic + check
    rev = base["revenue"] * (1 + comp + unit)
    ratios = {}
    for ln, spec in schema["cost_lines"].items():
        f = float(spec["fixed_share"])
        infl = g(spec["inflation_driver"])
        r = base["ratios_pct"][ln] / 100 * (f * (1 + infl) / (1 + comp) + (1 - f) * (1 + infl) / (1 + check))
        if spec.get("efficiency_driver"):
            r *= 1 - g(spec["efficiency_driver"])
        ratios[ln] = r
    ratios["other_opex"] -= rlm_shift_bps / 10000
    out = {"revenue": rev, "comp_pct": comp * 100, "ratios_pct": {k: v * 100 for k, v in ratios.items()}}
    out["rlm_pct"] = 100 - sum(out["ratios_pct"].values())
    gna = base["g_and_a_adj"] * (1 + g("g_and_a_growth_pct"))
    below = rev * (base["d_and_a"] + base["preopening"] + base["impairment_adj"]) / base["revenue"]
    pretax = rev * (1 - sum(ratios.values())) - gna - below + base["interest_adj"]
    tax = float(drivers.get("tax_rate_pct", base["tax_rate"]))
    ni = pretax * (1 - tax / 100)
    shares = base["shares"] * (1 - g("share_reduction_pct"))
    out.update({"g_and_a_adj": gna, "pretax_adj": pretax, "net_income_adj": ni, "shares": shares,
                "eps": ni / shares, "tax_rate": tax})
    return out


def eps_bridge(base: dict, drivers: dict, schema: dict) -> list[dict]:
    """Group-by-group EPS walk from the base year to the case. Telescopes exactly."""
    cur = neutral_drivers(base, schema)
    prev = project(base, cur, schema)["eps"]
    steps = []
    for group in schema["eps_bridge_order"]:
        for k, spec in schema["drivers"].items():
            if spec["group"] == group:
                cur[k] = drivers.get(k, cur[k])
        e = project(base, cur, schema)["eps"]
        steps.append({"group": group, "eps_change": e - prev})
        prev = e
    return steps


def solve(base: dict, drivers: dict, schema: dict, target_eps: float, key: str,
          lo: float = -20.0, hi: float = 30.0) -> float | None:
    """Value of one driver (or 'rlm_shift_bps') that makes EPS hit the target."""
    def eps_at(x):
        if key == "rlm_shift_bps":
            return project(base, drivers, schema, rlm_shift_bps=x)["eps"]
        d = dict(drivers)
        d[key] = x
        return project(base, d, schema)["eps"]
    if key == "rlm_shift_bps":
        lo, hi = -1500.0, 1500.0
    f_lo, f_hi = eps_at(lo) - target_eps, eps_at(hi) - target_eps
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if (eps_at(mid) - target_eps) * f_lo > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def validate_drivers(drivers: dict, schema: dict) -> list[str]:
    errs = []
    for k, spec in schema["drivers"].items():
        if k not in drivers:
            errs.append(f"missing driver {k}")
            continue
        try:
            v = float(drivers[k])
        except (TypeError, ValueError):
            errs.append(f"{k} must be a number")
            continue
        if not spec["min"] <= v <= spec["max"]:
            errs.append(f"{k}={v} outside {spec['min']} to {spec['max']} {spec['unit']}")
    return errs


def stance_from(ev_return: float, bear_return: float) -> str:
    if ev_return >= HURDLE:
        return "long"
    if ev_return <= -HURDLE:
        return "short"
    if ev_return < 0 and bear_return <= -0.25:
        return "avoid"
    return "no_edge"


def conviction_from(ev_return: float) -> str:
    a = abs(ev_return)
    return "high" if a >= 2 * HURDLE else "medium" if a >= HURDLE else "low"


def evaluate(base: dict, cases: dict, schema: dict, price: float, cons_next_eps: float | None) -> dict:
    """Compute every case, the expected value, the stance and the consensus/price reads."""
    out: dict = {"cases": {}, "base_year": base}
    for name in ("bull", "base", "bear"):
        c = cases[name]
        p = project(base, c["drivers"], schema)
        steps = eps_bridge(base, c["drivers"], schema)
        target = p["eps"] * float(c["multiple"])
        out["cases"][name] = {
            **p, "drivers": c["drivers"], "multiple": float(c["multiple"]),
            "probability": float(c["probability"]), "reasoning": c.get("reasoning", ""),
            "target": target, "return_pct": (target / price - 1) * 100, "eps_bridge": steps,
            "margin_recovery": p["rlm_pct"] > base["rlm_pct"] + 0.10,
        }
    cs = out["cases"]
    psum = sum(cs[n]["probability"] for n in cs)
    ev = sum(cs[n]["probability"] * cs[n]["target"] for n in cs) / psum
    out.update({"probability_sum": psum, "expected_value": ev, "expected_return_pct": (ev / price - 1) * 100})
    r, bear_r = ev / price - 1, cs["bear"]["return_pct"] / 100
    out["stance"], out["conviction"] = stance_from(r, bear_r), conviction_from(r)
    base_d = cases["base"]["drivers"]
    if cons_next_eps:
        e = sorted((cs[n]["eps"], n) for n in cs)
        pos = ""
        if cons_next_eps <= e[0][0]:
            pos = f"below the {e[0][1]} case (${e[0][0]:.2f})"
        elif cons_next_eps >= e[-1][0]:
            pos = f"above the {e[-1][1]} case (${e[-1][0]:.2f})"
        else:
            for (lo_e, lo_n), (hi_e, hi_n) in zip(e, e[1:]):
                if lo_e <= cons_next_eps <= hi_e:
                    pos = (f"between {lo_n} (${lo_e:.2f}) and {hi_n} (${hi_e:.2f}), "
                           f"{(cons_next_eps - lo_e) / (hi_e - lo_e):.0%} of the way to {hi_n}")
        out["consensus"] = {
            "eps": cons_next_eps, "position": pos,
            "traffic_needed_pct": solve(base, base_d, schema, cons_next_eps, "traffic_pct"),
            "rlm_needed_pct": (lambda s: None if s is None else project(base, base_d, schema, s)["rlm_pct"])(
                solve(base, base_d, schema, cons_next_eps, "rlm_shift_bps")),
            "multiple_at_price": price / cons_next_eps,
        }
    bm = cs["base"]["multiple"]
    implied_eps = price / bm
    out["price_implies"] = {
        "price": price, "multiple_on_base_eps": price / cs["base"]["eps"],
        "eps_at_base_multiple": implied_eps, "base_multiple": bm,
        "traffic_at_base_multiple_pct": solve(base, base_d, schema, implied_eps, "traffic_pct"),
    }
    return out


def foot_problems(result: dict, tol: float = 1e-9) -> list[str]:
    """Each case's EPS bridge must sum to its EPS change, and EPS must equal NI / shares."""
    probs = []
    b = result["base_year"]["eps"]
    for n, c in result["cases"].items():
        s = sum(x["eps_change"] for x in c["eps_bridge"])
        if abs(b + s - c["eps"]) > tol:
            probs.append(f"{n}: base EPS {b:.4f} + bridge {s:+.4f} != case EPS {c['eps']:.4f}")
        if abs(c["net_income_adj"] / c["shares"] - c["eps"]) > tol:
            probs.append(f"{n}: EPS != net income / shares")
        if abs(c["target"] - c["eps"] * c["multiple"]) > 1e-6:
            probs.append(f"{n}: target != EPS x multiple")
    return probs
