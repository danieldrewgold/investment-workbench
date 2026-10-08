"""
Tests for the call layer: margin bridges foot, prices are real and fresh,
guidance bias math, call validation rules, management-ledger verification,
valuation history guards, and no em dashes in narrative output.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.call import bridges, price, guidance_ledger, decide, mgmt_ledger, valuation_pack
from research.call.text import scrub, strip_tags, EM_DASH
from research.call.render import render_digest, render_pitch

# ---------------------------------------------------------------- bridges

def _bridge_spec(stated_total=None):
    """The old CMG FY27 operating-margin bridge, rebuilt on inputs."""
    spec = {
        "name": "FY27 operating margin", "metric": "operating margin", "period": "FY2027",
        "start_pct": 14.2, "start_basis": "FY2026 consensus-implied operating margin",
        "components": [
            {"name": "traffic leverage", "method": "operating_leverage",
             "inputs": {"flow_through_pct": 40, "revenue_change_pct": 1.0}, "stated_bps": 40},
            {"name": "G&A leverage", "method": "stated", "bps": 20, "basis": "G&A held flat on +10% revenue"},
        ],
    }
    if stated_total is not None:
        spec["stated_total_bps"] = stated_total
    return spec


def test_every_bridge_foots():
    """Components sum to the total and start + change = end, for many bridges."""
    import random
    rnd = random.Random(7)
    for _ in range(200):
        start = rnd.uniform(5, 30)
        spec = {"name": "b", "start_pct": start, "start_basis": "test", "components": []}
        if rnd.random() < 0.6:                               # a priced bridge: buckets cover all costs
            spec["components"].append({"name": "price", "method": "price", "inputs": {"price_pct": rnd.uniform(-2, 6)}})
            left = 100 - start
            for i in range(rnd.randint(1, 4)):
                share = left if i == 3 else rnd.uniform(0, left)
                left -= share
                spec["components"].append({"name": f"cost{i}", "method": "cost_inflation", "inputs": {
                    "cost_inflation_pct": rnd.uniform(-2, 8), "cost_base_pct_of_revenue": share}})
            if left > 0:
                spec["components"].append({"name": "rest", "method": "cost_inflation", "inputs": {
                    "cost_inflation_pct": 0, "cost_base_pct_of_revenue": left}})
        for i in range(rnd.randint(1, 3)):
            if rnd.random() < 0.5:
                spec["components"].append({"name": f"lev{i}", "method": "operating_leverage", "inputs": {
                    "flow_through_pct": rnd.uniform(0, 80), "revenue_change_pct": rnd.uniform(-10, 15)}})
            else:
                spec["components"].append({"name": f"s{i}", "method": "stated", "bps": rnd.uniform(-80, 80), "basis": "x"})
        b = bridges.compute_bridge(spec)
        assert bridges.check_foots(b) == [], bridges.check_foots(b)
        assert abs(sum(c.bps for c in b.components) - b.total_bps) < 1e-9
        assert abs(b.start_pct + b.total_bps / 100 - b.end_pct) < 1e-9


def test_price_and_cost_buckets_are_exact():
    """Price plus cost buckets covering all costs equal the closed-form margin change."""
    m, p = 0.239, 0.0275
    buckets = [(25.5, 3.9), (30.0, 2.75), (20.6, 3.0)]
    spec = {"name": "rlm", "start_pct": 23.9, "start_basis": "FY2026 est", "components":
            [{"name": "price", "method": "price", "inputs": {"price_pct": 2.75}}] +
            [{"name": f"b{i}", "method": "cost_inflation", "inputs": {"cost_inflation_pct": c, "cost_base_pct_of_revenue": cb}}
             for i, (cb, c) in enumerate(buckets)]}
    b = bridges.compute_bridge(spec)
    exact = (1 - sum(cb / 100 * (1 + c / 100) for cb, c in buckets) / (1 + p)) - m
    assert abs(b.total_bps - exact * 10000) < 1e-6


def test_price_counted_once_and_costs_covered():
    base = {"name": "x", "start_pct": 23.9, "start_basis": "y"}
    two_prices = dict(base, components=[{"name": "p1", "method": "price", "inputs": {"price_pct": 2}},
                                        {"name": "p2", "method": "price", "inputs": {"price_pct": 1}},
                                        {"name": "all", "method": "cost_inflation", "inputs": {"cost_inflation_pct": 3, "cost_base_pct_of_revenue": 76.1}}])
    partial = dict(base, components=[{"name": "p", "method": "price", "inputs": {"price_pct": 2.75}},
                                     {"name": "labor", "method": "cost_inflation", "inputs": {"cost_inflation_pct": 3.9, "cost_base_pct_of_revenue": 25.5}},
                                     {"name": "food", "method": "cost_inflation", "inputs": {"cost_inflation_pct": 2.75, "cost_base_pct_of_revenue": 30}}])
    old_method = dict(base, components=[{"name": "pvc", "method": "price_vs_cost", "inputs": {}}])
    for bad, why in ((two_prices, "at most one"), (partial, "cover"), (old_method, "not accepted")):
        try:
            bridges.compute_bridge(bad)
        except bridges.BridgeError as e:
            assert why in str(e), e
            continue
        raise AssertionError(f"accepted {why}")


def test_traffic_leverage_formula():
    """(flow-through - current margin) x revenue change, exact form divides by (1+g)."""
    bps = bridges.operating_leverage_bps(40, 14.2, 1.0)
    simple = (0.40 - 0.142) * 0.01 * 10000      # 25.8bp: the first-order rule
    assert abs(bps - simple / 1.01) < 1e-9
    assert 25.0 < bps < 26.0
    # Flow-through equal to the current margin leaves the margin unchanged
    assert abs(bridges.operating_leverage_bps(14.2, 14.2, 5.0)) < 1e-9


def test_cmg_bridge_bug_is_caught_and_fixed():
    """The old FY27 bridge: components -50, +40, +20 stated as +35. Code recomputes everything."""
    b = bridges.compute_bridge(_bridge_spec(stated_total=35))
    assert bridges.check_foots(b) == []
    lev = next(c for c in b.components if c.name == "traffic leverage")
    assert 25 < lev.bps < 26                     # not the +40 the narrative used
    assert any("traffic leverage" in w for w in b.warnings)
    assert any("stated total" in w for w in b.warnings)
    assert abs(b.total_bps - (lev.bps + 20)) < 1e-9
    assert abs(b.end_pct - (14.2 + b.total_bps / 100)) < 1e-9


def test_bridge_rejects_bad_specs():
    for bad in ({"start_pct": 14, "components": [{"name": "x", "method": "stated", "bps": 5, "basis": "y"}]},
                {"start_pct": 14, "start_basis": "z", "components": []},
                {"start_pct": 14, "start_basis": "z", "components": [{"name": "x", "method": "magic"}]},
                {"start_pct": 14, "start_basis": "z", "components": [{"name": "x", "method": "stated", "bps": 5}]}):
        try:
            bridges.compute_bridge(bad)
        except bridges.BridgeError:
            continue
        raise AssertionError(f"accepted bad spec {bad}")


# ---------------------------------------------------------------- price

def test_last_completed_session():
    sat = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)                 # Saturday
    assert price.last_completed_session(sat) == date(2026, 10, 2)
    before_close = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)       # Wed 11:00 ET
    assert price.last_completed_session(before_close) == date(2026, 10, 6)
    after_close = datetime(2026, 10, 7, 21, tzinfo=timezone.utc)        # Wed 17:00 ET
    assert price.last_completed_session(after_close) == date(2026, 10, 7)
    after_thanksgiving = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    assert price.last_completed_session(after_thanksgiving) == date(2026, 11, 25)


def test_stale_price_fails_loudly():
    now = datetime(2026, 10, 7, 21, tzinfo=timezone.utc)
    price.check_fresh(date(2026, 10, 7), now)
    price.check_fresh(date(2026, 10, 6), now)                           # one session of slack
    try:
        price.check_fresh(date(2026, 10, 2), now)
    except price.PriceError:
        return
    raise AssertionError("a four-day-old price was accepted")


# ---------------------------------------------------------------- guidance bias

def test_guidance_bias_and_adjustment():
    items = [
        {"metric": "comps_pct", "period": "FY2023", "low": 4, "high": 6, "issued_order": ("2023-02-01", 1), "source": "pr"},
        {"metric": "comps_pct", "period": "FY2023", "low": 6, "high": 8, "issued_order": ("2023-07-01", 1), "source": "pr"},
        {"metric": "comps_pct", "period": "FY2024", "low": 2, "high": 4, "issued_order": ("2024-02-01", 1), "source": "pr"},
        {"metric": "revenue", "period": "FY2024", "low": 1000, "high": 1000, "issued_order": ("2024-02-01", 1), "source": "pr"},
    ]
    actuals = {("comps_pct", "FY2023"): 8.0, ("comps_pct", "FY2024"): 5.0, ("revenue", "FY2024"): 1050}
    b = guidance_ledger.compute_bias(items, actuals)
    comps = b["comps_pct"]
    assert comps["n"] == 2                       # first guide per period only; the raise isn't double-counted
    assert abs(comps["mean_error"] - 2.5) < 1e-9 # FY23 8 vs mid 5 (+3), FY24 5 vs mid 3 (+2)
    assert comps["kind"] == "rate" and comps["unit"] == "pp"
    assert abs(comps["shrunk_bias"] - 2.5 * 2 / 4) < 1e-9
    assert abs(b["revenue"]["mean_error"] - 5.0) < 1e-9
    adj = guidance_ledger.adjust(1, 3, comps)
    assert abs(adj["adjusted_mid"] - (2 + 1.25)) < 1e-9
    lvl = guidance_ledger.adjust(2000, 2000, b["revenue"])
    assert abs(lvl["adjusted_mid"] - 2000 * (1 + b["revenue"]["shrunk_bias"] / 100)) < 1e-6
    assert guidance_ledger.adjust(1, 3, None)["adjusted_mid"] == 2


def test_period_normalization():
    n = guidance_ledger._norm_period
    assert n("fy26") == "FY2026" and n("Q3 2026") == "Q3 2026" and n("FY 2025") == "FY2025"


# ---------------------------------------------------------------- call validation

LP = {"price": 30.0, "session_date": "2026-10-06", "source": "test"}


def _ctx(statements=None):
    return {"ticker": "TEST", "today": "2026-10-07", "live_price": LP,
            "mgmt_ledger": {"statements": statements or []}}


def _good_call(stance="long"):
    sc = {"bull": {"eps": 2.0, "multiple": 25, "probability": 0.3, "reasoning": "r"},
          "base": {"eps": 1.6, "multiple": 25, "probability": 0.5, "reasoning": "r"},
          "bear": {"eps": 1.2, "multiple": 20, "probability": 0.2, "reasoning": "r"}}
    return {
        "stance": stance, "conviction": "medium",
        "thesis": "Margins recover faster than the Street models because pricing now exceeds cost inflation.",
        "key_drivers": [{"driver": "operating margin", "ours": 16.0, "consensus": 15.0, "unit": "%", "period": "FY2027"}],
        "price_implies": "The market prices flat margins.",
        "multiple_view": {"current_multiple": 20, "basis": "next-FY P/E", "verdict": "low", "direction": "expand",
                          "reasoning": "Growth is re-accelerating while the multiple sits at the bottom of its five-year range and revisions are rising."},
        "scenarios": sc,
        "catalysts": [{"date": "2026-10-28", "event": "Q3 print", "what_we_expect": "x", "if_wrong": "y"}],
        "kill_criteria": ["Q3 margin below 13%", "FY27 guide under 2% comps"],
        "evidence": [{"point": "Margin up 50bp in Q2", "tag": "R", "refs": ["R: Q2 PR"], "supports": "driver"}],
    }


def test_valid_call_passes_and_ev_is_computed():
    errs, d = decide.validate(_good_call(), _ctx())
    assert errs == [], errs
    ev = 0.3 * 50 + 0.5 * 40 + 0.2 * 24
    assert abs(d["expected_value"] - ev) < 1e-6
    assert abs(d["expected_return_pct"] - round((ev / 30 - 1) * 100, 1)) < 1e-9


def _errs(call, ctx=None):
    return decide.validate(call, ctx or _ctx())[0]


def test_call_rules():
    c = _good_call(); c["stance"] = "pair"
    assert any("pair" in e for e in _errs(c))
    c = _good_call(); c["scenarios"]["bull"]["probability"] = 0.6
    assert any("sum to" in e for e in _errs(c))
    c = _good_call(); c["scenarios"]["bull"]["eps"] = 1.0
    assert any("order" in e for e in _errs(c))
    c = _good_call("short")
    assert any("short needs" in e for e in _errs(c))
    c = _good_call("no_edge")
    assert any("no_edge is inconsistent" in e for e in _errs(c))
    c = _good_call(); c["catalysts"] = []
    assert any("catalyst" in e for e in _errs(c))
    c = _good_call(); c["catalysts"][0]["date"] = "2026-01-01"
    assert any("in the past" in e for e in _errs(c))
    c = _good_call(); c["kill_criteria"] = ["if things get worse", "margins disappoint"]
    assert any("measurable" in e for e in _errs(c))
    c = _good_call(); c["thesis"] = "One. Two."
    assert any("one sentence" in e for e in _errs(c))
    c = _good_call(); del c["multiple_view"]
    assert any("multiple_view" in e for e in _errs(c))
    c = _good_call(); c["key_drivers"] = []
    assert any("drivers" in e for e in _errs(c))


def test_bridge_must_land_on_the_margin_estimate():
    c = _good_call()
    c["bridges"] = [{"name": "FY27 operating margin", "metric": "operating margin", "period": "FY2027",
                     "start_pct": 15.0, "start_basis": "FY2026",
                     "components": [{"name": "x", "method": "stated", "bps": 30, "basis": "y"}]}]
    assert any("ends at 15.30%" in e for e in _errs(c))        # driver says 16.0%
    c["bridges"][0]["components"][0]["bps"] = 100
    assert not any("ends at" in e for e in _errs(c))


def test_no_edge_needs_a_trigger():
    c = _good_call("no_edge")
    for s in c["scenarios"].values():
        s["eps"], s["multiple"] = 1.5, 20                   # EV equals price
    c["no_edge_trigger"] = "something changes"
    assert any("trigger" in e for e in _errs(c))
    c["no_edge_trigger"] = "a Q3 comp above 3% or a price below $25"
    assert _errs(c) == []


def test_self_serving_claim_cannot_carry_the_call():
    st = [{"id": "S01", "category": "e"}, {"id": "S02", "category": "a"}]
    c = _good_call()
    c["evidence"] = [{"point": "HEEP lifts comps by hundreds of bp", "tag": "MC", "refs": ["S01"], "supports": "stance"}]
    assert any("self-serving" in e for e in _errs(c, _ctx(st)))
    c["evidence"][0]["refs"] = ["S01", "IND: equipped-store comp data"]
    assert not any("self-serving" in e for e in _errs(c, _ctx(st)))
    c["evidence"][0]["refs"] = ["S01", "S02"]
    assert not any("self-serving" in e for e in _errs(c, _ctx(st)))


def test_conviction_capped_when_ev_small():
    c = _good_call(); c["conviction"] = "high"
    c["scenarios"]["bull"]["eps"] = 1.8           # EV +27.7%: above the hurdle, below 2x hurdle
    errs, d = decide.validate(c, _ctx())
    assert errs == [] and d["conviction"] == "medium" and "capped" in d["conviction_note"]


def test_overrides_change_ev():
    c = _good_call()
    base = decide.compute(c, 30.0)
    over = decide.compute(c, 30.0, {"probabilities": {"bull": 0.1, "base": 0.5, "bear": 0.4}})
    assert over["expected_value"] < base["expected_value"] and over["overrides_applied"]


# ---------------------------------------------------------------- management ledger

def test_excuse_verification_needs_later_reported_figure():
    reported = [{"metric": "comps_pct", "period": "Q2 2026", "value": 2.2},
                {"metric": "comps_pct", "period": "Q1 2026", "value": 0.5}]
    st = [
        {"id": "S1", "quarter": "Q1 2026", "category": "e", "excuse_for_miss": True,
         "verification": {"status": "confirmed", "metric": "comps_pct", "period": "Q2 2026"}},
        {"id": "S2", "quarter": "Q2 2026", "category": "e", "excuse_for_miss": True,
         "verification": {"status": "confirmed", "metric": "comps_pct", "period": "Q1 2026"}},
        {"id": "S3", "quarter": "Q1 2026", "category": "e",
         "verification": {"status": "refuted", "metric": "made_up", "period": "Q2 2026"}},
        {"id": "S4", "quarter": "Q1 2026", "category": "d", "verification": {"status": "confirmed"}},
    ]
    mgmt_ledger._enforce_verification(st, reported)
    assert st[0]["verification"]["status"] == "confirmed"
    assert st[1]["verification"]["status"] == "unverified"     # earlier period can't confirm
    assert st[2]["verification"]["status"] == "unverified"     # not a real reported figure
    assert st[3]["verification"]["status"] == "n/a"


# ---------------------------------------------------------------- valuation history

def test_unadjusted_split_is_refused():
    eps = [(date(2023, 3, 31), 9.0), (date(2023, 6, 30), 10.0), (date(2023, 9, 30), 9.5),
           (date(2023, 12, 31), 10.5), (date(2024, 3, 31), 11.0), (date(2024, 6, 30), 0.25),
           (date(2024, 9, 30), 0.26), (date(2024, 12, 31), 0.27)]
    daily = [[f"2024-{m:02d}-15", 50.0] for m in range(1, 13)]
    assert "error" in valuation_pack.pe_history(daily, eps)
    adj = valuation_pack.split_adjust(eps, [(date(2024, 6, 26), 50.0)])
    assert adj[4][1] == 11.0 / 50 and adj[5][1] == 0.25


def test_peer_table_parse():
    txt = "TXRH       +11.2%      +7.6%     24.7×      ↑3 / ↓1\nSG          +0.7%     +71.4%         —            —"
    assert valuation_pack.parse_peer_pes(txt) == [("TXRH", 24.7, 7.6)]


# ---------------------------------------------------------------- narrative hygiene

def test_no_em_dashes_and_pitch_has_no_tags():
    call = _good_call()
    call["thesis"] = "Margins recover " + EM_DASH + " faster than modeled."
    call["evidence"][0]["point"] = "Margin " + EM_DASH + " up [R] and more [IND: BLS]"
    call = scrub(call)
    _, d = decide.validate(call, _ctx())
    res = {"call": call, "derived": d, "live_price": LP, "hurdle_pct": 15, "model": "test", "repaired": False}
    ctx = {"today": "2026-10-07", "valuation_block": "x " + EM_DASH + " y", "guidance_block": "",
           "mgmt_block": "", "brief": {"narrative_synthesis": "old " + EM_DASH + " essay"}, "audit": {}}
    digest, pitch = render_digest("TEST", res, ctx), render_pitch("TEST", res, ctx)
    assert EM_DASH not in digest and EM_DASH not in pitch
    assert "[R]" in digest
    assert "[R]" not in pitch and "[IND" not in pitch
    assert strip_tags("a [EST] b [IND: x]") == "a b"


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
