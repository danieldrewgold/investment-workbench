"""
Tests for the bottom-up scenario engine and the call built on it: the reported-lines
parser, the base-year build, that every case foots, solves, the stance rule, the
config, the guidance verdict, the conditions rule, narrative checks, and rendering.
Synthetic data only, so the suite doesn't depend on data/.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.call import scenarios as S, reported_lines as RL, guidance_ledger as GL, mgmt_ledger as ML, pm
from research.call.report import render_digest, render_pitch
from research.call.text import EM_DASH

SCHEMA = RL.load_schema("restaurant")


def _quarter(rev, food=29.5, labor=25.0, occ=5.2, other=14.8, gna=170.0, tax=24.0, shares=1300.0):
    q = {"revenue": rev, "food": rev * food / 100, "labor": rev * labor / 100, "occupancy": rev * occ / 100,
         "other_opex": rev * other / 100, "g_and_a_adj": gna, "d_and_a": rev * 0.03, "preopening": rev * 0.004,
         "impairment_adj": rev * 0.002, "interest_adj": 10.0, "tax_rate_adj": tax, "diluted_shares_m": shares,
         "complete": True}
    costs = q["food"] + q["labor"] + q["occupancy"] + q["other_opex"]
    q["rlm_pct"] = (1 - costs / rev) * 100
    q["pretax_adj"] = rev - costs - gna - q["d_and_a"] - q["preopening"] - q["impairment_adj"] + q["interest_adj"]
    return q


def _quarters():
    Q = {}
    revs = {1: 2875, 2: 3063, 3: 3003, 4: 2984}
    for i in range(1, 5):
        Q[f"Q{i} 2025"] = _quarter(revs[i], shares=1360 - 10 * i)
    Q["Q1 2026"] = _quarter(3088, food=29.6, labor=25.7, other=15.6, gna=198, tax=25.3, shares=1302)
    Q["Q2 2026"] = _quarter(3349, food=29.7, labor=25.0, other=14.9, gna=176, shares=1279)
    return Q


def _drivers(**kw):
    d = {"traffic_pct": 1.0, "check_pct": 2.5, "unit_contribution_pp": 7.0, "food_inflation_pct": 2.5,
         "wage_inflation_pct": 3.9, "labor_efficiency_pct": 0.5, "occupancy_inflation_pct": 3.5,
         "other_inflation_pct": 3.0, "g_and_a_growth_pct": 6.0, "share_reduction_pct": 4.0, "tax_rate_pct": 24.0}
    d.update(kw)
    return d


def _cases():
    return {"bull": {"drivers": _drivers(traffic_pct=3.0, labor_efficiency_pct=1.5), "multiple": 28, "probability": 0.25},
            "base": {"drivers": _drivers(), "multiple": 24, "probability": 0.5},
            "bear": {"drivers": _drivers(traffic_pct=-1.0, food_inflation_pct=4.5), "multiple": 19, "probability": 0.25}}


BRIDGE = {"h2_comp_pct": 2.0, "h2_unit_pp": 7.0, "delta_persistence": 0.5}

# ---------------------------------------------------------------- parser


def _release():
    rows = [["", "Three months ended June 30,"], ["", "2026", "", "2025"],
            ["Total revenue", "3,348,562", "", "", "100.0", "", "", "3,063,393", "", "", "100.0", ""],
            ["Food, beverage and packaging", "993,573", "", "", "29.7", "", "", "885,989", "", "", "28.9", ""],
            ["Labor", "836,450", "", "", "25.0", "", "", "756,261", "", "", "24.7", ""],
            ["Occupancy", "174,210", "", "", "5.2", "", "", "154,250", "", "", "5.0", ""],
            ["Other operating costs", "499,764", "", "", "14.9", "", "", "428,663", "", "", "14.0", ""],
            ["General and administrative expenses", "190,471", "", "", "5.7", "", "", "172,151", "", "", "5.6", ""],
            ["Depreciation and amortization", "98,327", "", "", "2.9", "", "", "90,945", "", "", "3.0", ""],
            ["Pre-opening costs", "16,364", "", "", "0.5", "", "", "10,610", "", "", "0.3", ""],
            ["Impairment, closure costs, and asset disposals", "13,808", "", "", "0.4", "", "", "5,467", "", "", "0.2", ""],
            ["Income from operations", "525,595", "", "", "15.7", "", "", "559,057", "", "", "18.2", ""],
            ["Interest and other income, net", "7,677", "", "", "0.2", "", "", "18,355", "", "", "0.6", ""],
            ["Income before income taxes", "533,272", "", "", "15.9", "", "", "577,412", "", "", "18.8", ""],
            ["Provision for income taxes", "129,725", "", "", "3.9", "", "", "141,285", "", "", "4.6", ""],
            ["Net income", "$", "403,547", "", "", "12.1", "%", "", "$", "436,127", "", "", "14.2", "%"],
            ["Earnings per share:", "", ""], ["Diluted", "$", "0.32", "", "", "", "", "$", "0.32", "", "", ""],
            ["Weighted-average common shares outstanding:", ""], ["Diluted", "1,279,064", "", "", "", "1,350,236", "", ""]]
    ni = [["", "2026", "2025"], ["Net income", "$", "403,547", "$", "436,127"],
          ["Restaurant asset impairment (1)", "3,933", "-"], ["Legal proceedings-Labor (1)", "5,000", "-"],
          ["Investment unrealized loss (6)", "-", "6,168"],
          ["Total non-GAAP adjustments", "18,204", "16,897"],
          ["Adjusted net income", "$", "418,906", "$", "450,405"],
          ["Adjusted diluted earnings per share", "$", "0.33", "$", "0.33"]]
    ga = [["", "2026", "2025"], ["Adjusted general and administrative expenses", "$", "176,200", "$", "159,938"]]
    tx = [["", "2026", "2025"], ["Adjusted effective income tax rate", "24.0", "%", "24.2", "%"]]
    return {"filing_date": "2026-07-29", "tables": [{"rows": rows, "columns": []}, {"rows": ni, "columns": []},
                                                    {"rows": ga, "columns": []}, {"rows": tx, "columns": []}]}


def test_parser_reads_both_quarters_and_adjusts():
    parsed = RL.parse_release(_release(), SCHEMA)
    assert set(parsed) == {"Q2 2026", "Q2 2025"}
    q = RL.finalize(parsed["Q2 2026"])
    assert abs(q["revenue"] - 3348.562) < 1e-6 and abs(q["diluted_shares_m"] - 1279.064) < 1e-6
    assert abs(q["labor"] - (836.450 - 5.0)) < 1e-6           # labor charge removed from the line
    assert abs(q["impairment_adj"] - (13.808 - 3.933)) < 1e-6
    assert q["g_and_a_adj"] == 176.2 and q["tax_rate_adj"] == 24.0 and q["eps_adj"] == 0.33
    p = RL.finalize(parsed["Q2 2025"])
    assert abs(p["interest_adj"] - (18.355 + 6.168)) < 1e-6
    assert abs(p["rebuild_error_pct"]) < 0.5                  # adjusted net income rebuilds


# ---------------------------------------------------------------- engine


def test_neutral_drivers_reproduce_the_base_year():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    assert base["reported_quarters"] == 2
    e = S.project(base, S.neutral_drivers(base, SCHEMA), SCHEMA)["eps"]
    assert abs(e - base["eps"]) < 1e-12


def test_every_case_foots():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    rnd = random.Random(3)
    for _ in range(100):
        cases = {}
        for n, p in (("bull", 0.3), ("base", 0.5), ("bear", 0.2)):
            d = {k: rnd.uniform(s["min"], s["max"]) for k, s in SCHEMA["drivers"].items()}
            cases[n] = {"drivers": d, "multiple": rnd.uniform(10, 40), "probability": p}
        R = S.evaluate(base, cases, SCHEMA, 30.0, 1.37)
        assert S.foot_problems(R) == [], S.foot_problems(R)
        for c in R["cases"].values():
            assert abs(base["eps"] + sum(x["eps_change"] for x in c["eps_bridge"]) - c["eps"]) < 1e-9
            assert abs(c["rlm_pct"] - (100 - sum(c["ratios_pct"].values()))) < 1e-9


def test_cost_line_mechanics():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    neutral = S.neutral_drivers(base, SCHEMA)
    # Pure price with no inflation lifts margin on variable lines; occupancy (all fixed) moves only with comp
    up = S.project(base, {**neutral, "check_pct": 3.0}, SCHEMA)
    assert up["ratios_pct"]["food"] < base["ratios_pct"]["food"]
    assert abs(up["ratios_pct"]["occupancy"] - base["ratios_pct"]["occupancy"] / 1.03) < 1e-9
    # Inflation equal to price leaves a fully variable line unchanged
    eq = S.project(base, {**neutral, "check_pct": 3.0, "food_inflation_pct": 3.0}, SCHEMA)
    assert abs(eq["ratios_pct"]["food"] - base["ratios_pct"]["food"]) < 1e-9


def test_solves_hit_their_targets():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    d = _drivers()
    t = S.project(base, d, SCHEMA)["eps"] * 1.1
    x = S.solve(base, d, SCHEMA, t, "traffic_pct")
    assert abs(S.project(base, {**d, "traffic_pct": x}, SCHEMA)["eps"] - t) < 1e-6
    s = S.solve(base, d, SCHEMA, t, "rlm_shift_bps")
    assert abs(S.project(base, d, SCHEMA, rlm_shift_bps=s)["eps"] - t) < 1e-6


def test_stance_and_conviction_follow_the_math():
    assert S.stance_from(0.20, -0.1) == "long" and S.stance_from(-0.20, -0.3) == "short"
    assert S.stance_from(-0.05, -0.30) == "avoid" and S.stance_from(0.05, -0.30) == "no_edge"
    assert S.conviction_from(0.35) == "high" and S.conviction_from(0.16) == "medium" and S.conviction_from(0.03) == "low"
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    assert R["stance"] == S.stance_from(R["expected_return_pct"] / 100, R["cases"]["bear"]["return_pct"] / 100)
    assert "position" in R["consensus"] and R["price_implies"]["eps_at_base_multiple"] == 30.0 / 24


def _band(b: dict, price: float) -> str:
    if price <= b["long_at_or_below"]:
        return "long"
    if price >= b["short_at_or_above"]:
        return "short"
    if b["avoid_above"] is not None and price > b["avoid_above"]:
        return "avoid"
    return "no_edge"


def test_stance_by_price_matches_the_rule():
    rnd = random.Random(7)
    for _ in range(300):
        ev = rnd.uniform(5, 200)
        bear = ev * rnd.uniform(-0.2, 1.0)
        b = S.stance_by_price(ev, bear)
        for _ in range(40):
            p = ev * rnd.uniform(0.5, 1.6)
            assert _band(b, p) == S.stance_from(ev / p - 1, bear / p - 1), (ev, bear, p, b)
    # CMG round 2: weighted value $33.49, bear target $23.40
    b = S.stance_by_price(33.49, 23.40)
    assert round(b["long_at_or_below"], 2) == 29.12 and round(b["avoid_above"], 2) == 33.49
    assert round(b["short_at_or_above"], 2) == 39.40
    # A bear case close to the weighted value pushes the avoid band up, or closes it
    b = S.stance_by_price(30.0, 24.0)
    assert abs(b["avoid_above"] - 24.0 / 0.75) < 1e-9 and b["avoid_above"] < b["short_at_or_above"]
    assert S.stance_by_price(30.0, 28.0)["avoid_above"] is None


def test_price_levels_come_from_code():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    text = pm.price_levels_text(R)
    b = R["stance_by_price"]
    assert f"${int(b['long_at_or_below'] * 100) / 100:.2f}" in text and "short at or above" in text
    assert EM_DASH not in text
    ctx = {"live_price": {"price": 30.0, "session_date": "2026-10-06"}, "mgmt_ledger": {"statements": []}}
    bad = _narrative(R); bad["data_triggers"] = "below about $28 we would go long; a Q3 comp above 3% helps"
    assert any("share-price levels" in e for e in pm.validate_b(bad, R, ctx))
    ok = _narrative(R); ok["data_triggers"] = "EPS above the $1.37 consensus or a Q3 comp above 3%"   # EPS, not a price level
    assert pm.validate_b(ok, R, ctx) == []
    assert "STANCE BY PRICE" in pm.results_block(R, SCHEMA)
    # The narrative prompt defines every evidence tag (MC once got read as "macro")
    assert "MC = management claim, unverified" in pm.SYSTEM_B and "never MC" in pm.SYSTEM_B


def test_overrides_change_the_inputs_not_the_math():
    cases = {n: {**c, "drivers": dict(c["drivers"])} for n, c in _cases().items()}
    applied = pm.apply_overrides(cases, {"probabilities": {"bull": 0.4, "base": 0.4, "bear": 0.2},
                                         "multiples": {"base": 26}, "drivers": {"bear": {"traffic_pct": -2.0}},
                                         "eps": {"base": 9.99}})
    assert cases["bull"]["probability"] == 0.4 and cases["base"]["multiple"] == 26.0
    assert cases["bear"]["drivers"]["traffic_pct"] == -2.0 and "base eps" not in " ".join(applied)
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, cases, SCHEMA, 30.0, 1.37)
    assert S.foot_problems(R) == [] and abs(R["cases"]["base"]["target"] - R["cases"]["base"]["eps"] * 26) < 1e-9


def test_gap_read_says_which_lever_reaches_consensus():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    g, cs, k = R["gap_read"], R["cases"], R["consensus"]
    tr = sorted(float(cs[n]["drivers"]["traffic_pct"]) for n in cs)
    mr = sorted(cs[n]["rlm_pct"] for n in cs)
    assert g["traffic"]["range"] == (tr[0], tr[-1]) and g["margin"]["range"] == (mr[0], mr[-1])
    assert g["traffic"]["within"] == (tr[0] <= k["traffic_needed_pct"] <= tr[-1])
    assert g["margin"]["within"] == (mr[0] <= k["rlm_needed_pct"] <= mr[-1])
    assert g["route"] == S.route_from(g["traffic"]["within"], g["margin"]["within"])
    assert abs(g["base_vs_consensus_pct"] - (cs["base"]["eps"] / 1.37 - 1) * 100) < 1e-9
    assert S.route_from(True, False) == "traffic" and S.route_from(False, True) == "margin"
    assert S.route_from(True, True) == "either" and S.route_from(False, False) == "neither"
    text = pm.results_block(R, SCHEMA)
    assert "GAP READ" in text and "EPS growth vs FY2026E" in text and "EPS vs consensus" in text


def test_dollar_figures_must_come_from_inputs():
    src = "Base EPS $1.26, consensus $1.37, bull target $46.14, a $2.1M legal charge, buybacks at $42.39."
    allowed = pm.dollar_values(src)
    text = "EPS of $1.25 vs $1.37, about $46, a $2.1M charge, $42.39 average, and a $0.08 gap."
    assert pm.unsourced_dollars(text, allowed) == ["$1.25", "$0.08"]
    assert pm.unsourced_dollars("a $2.1 billion program", pm.dollar_values("$2,100M authorized")) == []


def test_case_reasoning_is_written_after_the_math():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    ctx = {"live_price": {"price": 30.0, "session_date": "2026-10-06"}, "mgmt_ledger": {"statements": []}}
    assert pm.validate_b(_narrative(R), R, ctx) == []
    missing = _narrative(R); del missing["case_reasoning"]
    assert any("case_reasoning" in e for e in pm.validate_b(missing, R, ctx))
    eps = f"${R['cases']['base']['eps']:.2f}"
    drift = _narrative(R); drift["case_reasoning"]["base"] = drift["case_reasoning"]["base"].replace(eps, "$9.99")
    errs = pm.validate_b(drift, R, ctx)
    assert any("case_reasoning.base" in e and eps in e for e in errs) and any("$9.99" in e for e in errs)
    # A figure the inputs carry is fine
    sourced = _narrative(R); sourced["strongest_counter"] = "Buybacks at $42.39 show conviction."
    assert pm.validate_b(sourced, R, ctx, sources="average price $42.39") == []
    assert "case_reasoning" in pm.SYSTEM_B and "GAP READ" in pm.SYSTEM_B


def test_narrative_sees_only_the_final_base_year():
    """The inputs step may move the base-year bridge; the narrative must not see the default build."""
    default = {"h2_comp_pct": 2.0, "h2_unit_pp": 7.0, "delta_persistence": 0.2}
    chosen = {"h2_comp_pct": 2.0, "h2_unit_pp": 7.0, "delta_persistence": 1.0, "reasoning": "x"}
    A = {"bridge_year": chosen, "cases": {n: {**c, "reasoning": "a case explained in more than enough words to pass the check here"}
                                          for n, c in _cases().items()}}
    prompts = []

    def fake(system, user, **kw):
        prompts.append((system, user))
        return A if system == pm.SYSTEM_A else {}

    ctx = {"ticker": "TEST", "today": "2026-10-07", "live_price": {"price": 30.0, "session_date": "2026-10-06"},
           "schema": SCHEMA, "quarters": _quarters(), "comps": {}, "history_quarters": [], "base_fy": 2026,
           "default_bridge": default, "consensus_full": {"current_year": {"eps_mean": 1.2}, "next_year": {"eps_mean": 1.37}},
           "macro_block": "", "valuation_block": "", "guidance_block": "", "mgmt_block": "", "brief_block": "",
           "audit": {}, "mgmt_ledger": {"statements": []}}
    real = pm.call_json
    pm.call_json = fake
    try:
        pm.make_call(ctx)
    except pm.CallError:
        pass                                   # the empty narrative fails validation; we only need its prompt
    finally:
        pm.call_json = real
    user_b = next(u for s, u in prompts if s == pm.SYSTEM_B)
    final = S.build_base_year(_quarters(), 2026, chosen)["rlm_pct"]
    stale = S.build_base_year(_quarters(), 2026, default)["rlm_pct"]
    assert abs(final - stale) > 0.05
    assert f"restaurant margin {final:.2f}%" in user_b and f"restaurant margin {stale:.2f}%" not in user_b
    assert "Margin vs FY2026E" in user_b


def test_config_is_complete():
    groups = {d["group"] for d in SCHEMA["drivers"].values()}
    assert groups <= set(SCHEMA["eps_bridge_order"])
    for spec in SCHEMA["cost_lines"].values():
        assert spec["inflation_driver"] in SCHEMA["drivers"]
        assert 0 <= spec["fixed_share"] <= 1
    assert S.validate_drivers(_drivers(), SCHEMA) == []
    assert S.validate_drivers(_drivers(traffic_pct=50), SCHEMA)
    for line, series in SCHEMA["macro_by_cost_line"].items():
        assert all(s["id"] != "PPIACO" for s in series), "PPI all commodities is not a cost-line series"


# ---------------------------------------------------------------- guidance verdict and conditions


def test_guidance_verdict_by_horizon():
    items = [{"metric": "comps_pct", "period": "Q2 2026", "low": 1, "high": 1, "actual": 2.2, "issue_date": "2026-04-29"},
             {"metric": "comps_pct", "period": "FY2025", "low": 2, "high": 5, "actual": -1.7, "issue_date": "2025-02-04"},
             {"metric": "tax_rate_pct", "period": "FY2025", "low": 25, "high": 27, "actual": 23.6, "issue_date": "2025-02-04"},
             {"metric": "unit_openings", "period": "FY2025", "low": 315, "high": 345, "actual": 345, "issue_date": "2025-02-04"}]
    st = [{"id": "S9", "horizon": "long", "claim": "Mid single digit comps in 2026.", "verification": {"status": "refuted"}}]
    v = GL.verdict({"items": items}, st)
    assert v["near-term"]["n"] == 1 and v["near-term"]["missed"] == 0
    assert v["long-dated demand"] == {"n": 1, "missed": 1}
    assert v["long-dated controllable"] == {"n": 2, "missed": 0}         # lower tax counts as delivered
    text = " ".join(v["lines"])
    assert "usually beaten" in text and "2 of 2 missed" in text and "shrunk" not in text


def test_claim_is_refuted_only_under_its_own_conditions():
    st = [{"id": "S1", "quarter": "Q3 2025", "category": "e", "conditions": "as comps get back to mid single digits",
           "verification": {"status": "refuted", "metric": "unit_margin_pct", "period": "Q2 2026", "conditions_met": False}},
          {"id": "S2", "quarter": "Q3 2025", "category": "e", "conditions": "as comps get back to mid single digits",
           "verification": {"status": "refuted", "metric": "unit_margin_pct", "period": "Q2 2026", "conditions_met": True,
                            "conditions_metric": "comps_pct", "conditions_period": "Q2 2026"}}]
    ML._enforce_verification(st, [{"metric": "unit_margin_pct", "period": "Q2 2026"},
                                  {"metric": "comps_pct", "period": "Q2 2026"}])
    assert st[0]["verification"]["status"] == "untested"
    assert st[1]["verification"]["status"] == "refuted"


# ---------------------------------------------------------------- narrative and rendering


def _narrative(R=None):
    d = {"thesis": "The stock already prices the margin miss we expect.",
         "where_we_differ": [{"key": "restaurant_margin", "why": "Wages outrun price.", "refs": ["IND: BLS"]}],
         "multiple_view": {"current_multiple": 22.6, "basis": "next-FY P/E", "verdict": "fair", "direction": "hold",
                           "reasoning": "x"},
         "price_implies_read": "x", "data_triggers": "a Q3 comp above 3% or Q3 labor below 25%",
         "catalysts": [{"date": "2026-10-28", "event": "Q3", "what_we_expect": "a", "if_wrong": "b"}],
         "kill_criteria": ["Q3 labor below 25%", "comp above 4%"],
         "evidence": [{"point": "Labor rose 70bp", "implication": "the wage gap is not closing yet",
                       "tag": "R", "refs": ["R: Q2 PR"], "supports": "driver"}],
         "management_read": {"credibility": "", "flow_through": "", "signals": []},
         "strongest_counter": "x", "ownership": "Passive holders dominate.", "reconciliation": "GAAP $1.09 vs adjusted $1.17"}
    if R is not None:
        d["case_reasoning"] = {n: f"This case lands at ${R['cases'][n]['eps']:.2f} EPS because traffic and margin "
                                  "move as its drivers say, and the multiple follows that growth." for n in ("bull", "base", "bear")}
    return d


def test_narrative_rules():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    ctx = {"live_price": {"price": 30.0, "session_date": "2026-10-06"}, "mgmt_ledger": {"statements": []}}
    assert pm.validate_b(_narrative(R), R, ctx) == []
    bad = _narrative(R); bad["strongest_counter"] = "Starbucks merger talk supports the stock"
    assert any("rumors" in e for e in pm.validate_b(bad, R, ctx))
    bad = _narrative(R); bad["ownership"] = "a\nb\nc"
    assert any("ownership" in e for e in pm.validate_b(bad, R, ctx))
    bad = _narrative(R); bad["evidence"][0]["implication"] = ""
    assert any("implication" in e for e in pm.validate_b(bad, R, ctx))


def test_rendered_outputs():
    base = S.build_base_year(_quarters(), 2026, BRIDGE)
    R = S.evaluate(base, _cases(), SCHEMA, 30.0, 1.37)
    call = {**_narrative(), "stance": R["stance"], "conviction": R["conviction"],
            "what_would_change_the_stance": pm.price_levels_text(R) + " " + _narrative()["data_triggers"],
            "where_we_differ": [{"driver": "Restaurant margin", "ours": 23.1, "consensus": 24.4, "why": "Wages " + EM_DASH + " price.",
                                 "refs": ["IND: BLS"], "consensus_basis": "margin that gets our model to consensus"}]}
    res = {"call": call, "scenario_result": R, "live_price": {"price": 30.0, "session_date": "2026-10-06", "source": "t"},
           "hurdle_pct": 15, "model": "t", "repaired": [], "scenario_inputs": {}}
    ctx = {"schema": SCHEMA, "today": "2026-10-07", "valuation_block": "", "guidance_verdict": ["Near-term guides are beaten."],
           "brief": {"narrative_synthesis": "keep this\nStarbucks takeover chatter"}, "audit": {}}
    d, p = render_digest("TEST", res, ctx), render_pitch("TEST", res, ctx)
    assert EM_DASH not in d and EM_DASH not in p
    assert "## Where we differ from consensus" in d and "Why the market is wrong" not in d
    assert "Starbucks" not in d and "keep this" in d
    assert "[R]" in d and "[R]" not in p
    assert "Consensus $1.37" in d and "The price implies" in d
    assert "On price, with the cases held fixed: long at or below $" in p
    from research.call.text import strip_tags, split_basis
    assert strip_tags("a promise ([S20], [S53]) but not delivered, as promised in [S89]. The [S89] promise "
                      "failed; guide [S87, S80] holds; keep [R] tags out.") == (
        "a promise but not delivered, as promised. The promise failed; guide holds; keep tags out.")
    assert split_basis("next-FY P/E on our base EPS") == ("next-FY P/E on our base EPS", "")
    assert split_basis("Price of $30.77 divided by our FY2027 base EPS of $1.26.") == (
        "", "Price of $30.77 divided by our FY2027 base EPS of $1.26.")
    s = strip_tags("Q4 price matches inflation (S89), poultry PPI -12.5% (IND), promise (S20, S53) and "
                   "(S21 to S79); keep (low conviction), (-26%), (AI) and (R&D).")
    assert s == ("Q4 price matches inflation, poultry PPI -12.5%, promise and; keep (low conviction), (-26%), "
                 "(AI) and (R&D).")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\nResults: {len(tests) - failed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
