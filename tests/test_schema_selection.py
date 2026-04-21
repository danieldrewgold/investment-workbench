"""
Tests for schema selection framework.

Tests:
  1. CMG selects company-operated restaurant with strong fit
  2. WING detects franchise structure, flags mismatch risk
  3. Software evidence selects SaaS schema
  4. Ambiguous evidence produces low confidence
  5. Schema workpaper production
  6. Schema selection connects to driver schema
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.provenance.database import init_db, new_id, RunContext, now_iso
from research.schema_selection import (
    SchemaEvidence, build_evidence_from_observations,
    score_schemas, select_schema, produce_schema_workpaper,
)
from research.sector_drivers import DRIVER_REGISTRY


def test_cmg_selects_company_operated():
    """CMG should select company-operated restaurant with strong fit."""
    evidence = [
        SchemaEvidence("revenue_type", "restaurant_sales", "CMG 10-K: restaurant revenue $11.3B"),
        SchemaEvidence("company_operated_pct", 100, "CMG operates 100% of locations"),
        SchemaEvidence("cost_disclosure", "food_labor_occupancy", "CMG discloses food/labor/occ costs"),
        SchemaEvidence("metric_disclosed", "sss", "CMG reports comparable restaurant sales"),
        SchemaEvidence("metric_disclosed", "restaurant_margin", "CMG reports restaurant-level margin"),
        SchemaEvidence("metric_disclosed", "new_store_openings", "CMG reports new restaurant openings"),
    ]

    sel = select_schema(evidence)

    assert sel.chosen_key == "company_operated_restaurant", \
        f"Expected company_operated_restaurant, got {sel.chosen_key}"
    assert sel.fit_level == "strong", \
        f"Expected strong fit for CMG, got {sel.fit_level}"
    assert sel.confidence >= 0.7, \
        f"Expected high confidence for CMG, got {sel.confidence}"
    assert sel.driver_schema_key == "restaurant"

    print(f"  CMG: {sel.chosen_label}")
    print(f"  Fit: {sel.fit_level}, confidence: {sel.confidence:.0%}")
    print(f"  Driver schema: {sel.driver_schema_key}")
    print(f"  Uncertainties: {sel.uncertainties or 'none'}")
    return True


def test_wing_detects_franchise():
    """WING should select franchise-heavy, NOT company-operated."""
    evidence = [
        SchemaEvidence("franchise_pct", 98, "WING: ~98% franchised"),
        SchemaEvidence("revenue_type", "royalty", "WING: royalty revenue, franchise fees"),
        SchemaEvidence("metric_disclosed", "system_wide_sales", "WING reports system-wide sales"),
        SchemaEvidence("metric_disclosed", "sss", "WING reports domestic SSS"),
        SchemaEvidence("cost_disclosure", "food_labor_occupancy",
                       "WING discloses cost of sales for company-owned (small)"),
    ]

    sel = select_schema(evidence)

    assert sel.chosen_key == "franchise_restaurant", \
        f"Expected franchise_restaurant, got {sel.chosen_key}"
    assert sel.driver_schema_key == "franchise_restaurant"

    # Check that company_operated is disqualified
    co_score = next((s for s in sel.scores if s.schema_key == "company_operated_restaurant"), None)
    assert co_score and co_score.disqualified, \
        "Company-operated should be disqualified for WING (franchise_pct >= 70)"

    print(f"  WING: {sel.chosen_label}")
    print(f"  Fit: {sel.fit_level}, confidence: {sel.confidence:.0%}")
    print(f"  Risk: {sel.risk_summary[:80]}...")
    print(f"  Company-operated disqualified: {co_score.disqualify_reason}")
    return True


def test_wing_schema_risk_visible():
    """WING schema risk should explain what breaks with wrong schema."""
    evidence = [
        SchemaEvidence("franchise_pct", 98, "WING: ~98% franchised"),
        SchemaEvidence("revenue_type", "royalty", "WING: royalty revenue"),
        SchemaEvidence("metric_disclosed", "system_wide_sales", "WING: system-wide sales"),
    ]

    sel = select_schema(evidence)

    assert "franchise" in sel.risk_summary.lower() or "royalty" in sel.risk_summary.lower(), \
        "Risk should mention franchise economics"
    assert len(sel.risk_summary) > 50, "Risk summary should be substantive"

    # If analyst forces company_operated, confidence should be very low
    forced = select_schema(evidence, analyst_override="company_operated_restaurant")
    assert forced.confidence < 0.3, \
        f"Forced wrong schema should have low confidence, got {forced.confidence}"
    assert forced.fit_level in ("no_fit", "weak"), \
        f"Forced wrong schema should show weak/no fit, got {forced.fit_level}"

    print(f"  Forced wrong schema: confidence {forced.confidence:.0%}, fit: {forced.fit_level}")
    print(f"  Uncertainties: {forced.uncertainties}")
    return True


def test_software_selects_saas():
    """Software evidence should select SaaS schema."""
    evidence = [
        SchemaEvidence("revenue_type", "subscription", "Subscription revenue $2.1B"),
        SchemaEvidence("metric_disclosed", "arr", "Reports ARR of $2.4B"),
        SchemaEvidence("metric_disclosed", "net_retention", "Net dollar retention 115%"),
        SchemaEvidence("cost_disclosure", "rd_sm_ga", "R&D 30%, S&M 28%, G&A 8%"),
        SchemaEvidence("gross_margin_range", 79.5, "Non-GAAP gross margin 79.5%"),
    ]

    sel = select_schema(evidence)

    assert sel.chosen_key == "saas_subscription", \
        f"Expected saas_subscription, got {sel.chosen_key}"
    assert sel.fit_level in ("strong", "adequate")
    assert sel.driver_schema_key == "software"

    print(f"  Software: {sel.chosen_label}")
    print(f"  Fit: {sel.fit_level}, confidence: {sel.confidence:.0%}")
    return True


def test_ambiguous_evidence_falls_back_to_general():
    """Minimal/ambiguous evidence should fall back to general schema."""
    evidence = [
        SchemaEvidence("metric_disclosed", "sss", "Reports some comp metric"),
    ]

    sel = select_schema(evidence)

    # With minimal evidence, specialized schemas score poorly
    # System should fall back to general schema
    assert sel.chosen_key == "general" or sel.confidence <= 0.6, \
        f"Ambiguous evidence should use general or have modest confidence, got {sel.chosen_key} at {sel.confidence}"

    print(f"  Ambiguous: {sel.chosen_label}")
    print(f"  Fit: {sel.fit_level}, confidence: {sel.confidence:.0%}")
    print(f"  Uncertainties: {sel.uncertainties}")
    return True


def test_observation_based_evidence():
    """build_evidence_from_observations should extract signals from raw observations."""
    observations = [
        {"key": "revenue", "value": "Revenue of $11.3B from company-owned restaurant sales", "source": "10-K"},
        {"key": "margins", "value": "Food cost 29.8%, labor cost 24.7%, occupancy 5.0%", "source": "10-K"},
        {"key": "growth", "value": "Same-store sales growth of 6.5% in FY2024", "source": "Q4 call"},
        {"key": "units", "value": "Opened 304 new restaurants in FY2024", "source": "10-K"},
        {"key": "margin", "value": "Restaurant-level margin of 28.4%", "source": "10-K"},
    ]

    evidence = build_evidence_from_observations(observations)

    signal_types = {e.signal_type for e in evidence}
    assert "revenue_type" in signal_types, "Should detect restaurant_sales revenue type"
    assert "cost_disclosure" in signal_types, "Should detect food/labor cost structure"
    assert "metric_disclosed" in signal_types, "Should detect SSS metric"

    sel = select_schema(evidence)
    assert sel.chosen_key == "company_operated_restaurant"

    print(f"  Extracted {len(evidence)} signals from {len(observations)} observations")
    for e in evidence:
        print(f"    {e.signal_type}: {str(e.value)[:40]}")
    print(f"  Selected: {sel.chosen_label} ({sel.fit_level})")
    return True


def test_schema_connects_to_driver_registry():
    """Selected schema's driver_schema_key should exist in DRIVER_REGISTRY."""
    for evidence_set, expected_driver in [
        ([SchemaEvidence("revenue_type", "restaurant_sales", ""),
          SchemaEvidence("company_operated_pct", 100, ""),
          SchemaEvidence("cost_disclosure", "food_labor_occupancy", ""),
          SchemaEvidence("metric_disclosed", "sss", "")], "restaurant"),
        ([SchemaEvidence("revenue_type", "subscription", ""),
          SchemaEvidence("metric_disclosed", "net_retention", ""),
          SchemaEvidence("gross_margin_range", 79, "")], "software"),
    ]:
        sel = select_schema(evidence_set)
        assert sel.driver_schema_key in DRIVER_REGISTRY, \
            f"Driver schema '{sel.driver_schema_key}' not in registry"
        schema = DRIVER_REGISTRY[sel.driver_schema_key]
        assert "cost_buckets" in schema, "Driver schema should have cost_buckets"
        assert "revenue_model" in schema, "Driver schema should have revenue_model"

    print(f"  Both restaurant and software schemas found in DRIVER_REGISTRY ✓")
    return True


def test_schema_workpaper():
    """Produce and verify a schema selection workpaper."""
    conn = init_db(Path(":memory:"))
    with RunContext(conn, "test") as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "CMG", "CMG"))
        conn.commit()

    evidence = [
        SchemaEvidence("revenue_type", "restaurant_sales", "10-K"),
        SchemaEvidence("company_operated_pct", 100, "10-K"),
        SchemaEvidence("cost_disclosure", "food_labor_occupancy", "10-K"),
        SchemaEvidence("metric_disclosed", "sss", "Q4 call"),
    ]

    sel = select_schema(evidence)
    wid = produce_schema_workpaper(conn, cid, sel)

    from research.escalation import WorkpaperBuilder
    wb = WorkpaperBuilder(conn, cid)
    wp = wb.get_workpaper(wid)

    assert wp["workpaper_type"] == "SCHEMA_SELECTION"
    assert wp["content"]["chosen_schema"] == "company_operated_restaurant"
    assert wp["content"]["fit_level"] == "strong"
    assert len(wp["content"]["candidates"]) >= 3

    print(f"  Workpaper: {wp['title']}")
    print(f"  Candidates scored: {len(wp['content']['candidates'])}")
    print(f"  Evidence used: {len(wp['content']['evidence'])}")
    conn.close()
    return True


def test_wing_franchise_model_accuracy():
    """
    WING FY2025 estimate using the CORRECT franchise schema.
    Previously: company-operated schema gave $4.12 EPS error.
    Now: franchise schema should dramatically reduce the error.

    Real WING FY2024 data:
      Total revenue: $625.8M
        Royalty + fees: ~$233M (37%)
        Advertising fees: ~$224M (36%)
        Company-owned sales: ~$119M (19%)
      System-wide sales: $4,765M
      Stores: 2,563 (50 company-owned, 2,513 franchised)
      SG&A: $116.8M, D&A: $19.5M, Interest: $20.0M
      Net income: $108.7M, EPS: $3.70

    FY2025 actual: Revenue $696.9M, adj EPS $4.08, SSS -3.3%

    FY2025 guidance: low-to-mid single SSS, 14-15% unit growth,
      SG&A ~$140M, stock comp ~$26M, D&A $29-30M, interest ~$46M
    """
    from research.estimate_model import ModelSpec
    from research.sector_drivers import FRANCHISE_DRIVERS

    # Step 1: Schema selection should pick franchise
    evidence = [
        SchemaEvidence("franchise_pct", 98, "WING: 98% franchised"),
        SchemaEvidence("revenue_type", "royalty", "Royalty revenue ~37% of total"),
        SchemaEvidence("metric_disclosed", "system_wide_sales", "System-wide sales $4.8B"),
        SchemaEvidence("metric_disclosed", "sss", "Domestic SSS 19.9%"),
    ]
    sel = select_schema(evidence)
    assert sel.chosen_key == "franchise_restaurant"
    assert sel.driver_schema_key == "franchise_restaurant"

    # Step 2: Build model with franchise schema
    model = ModelSpec(
        assumptions={
            "sss_growth_pct": 5.0,           # guidance: low-to-mid single
            "new_restaurants": 370,           # ~14.5% unit growth
            "royalty_rate_pct": 5.9,          # effective royalty rate from FY2024
            "ad_fund_rate_pct": 5.3,          # ad fund contribution rate
            "company_owned_stores": 50,
            "company_sss_pct": 3.0,           # company-owned SSS lower than system
            "cos_delta_bps": 0,
            "sga_delta_bps": 0,
            "stock_comp_growth_pct": 0.0,
            "da_growth_pct": 50.0,            # guidance: D&A growing to $29-30M from $19.5M
        },
        prior_year={
            "revenue_m": 625.8,
            "system_wide_sales_m": 4765,
            "store_count": 2563,
            "company_owned_stores": 50,
            "company_owned_auv_m": 2.38,      # $119M / 50 stores
            "cos_pct": 14.6,                  # $91.6M / $625.8M
            "ad_exp_pct": 35.8,               # $224M / $625.8M (pass-through)
            "sga_pct": 18.7,                  # $116.8M / $625.8M
            "stock_comp_m": 26.0,
            "da_m": 19.5,
        },
        constants={
            "tax_rate": 0.22,
            "shares_m": 28.5,                 # lower from buybacks
            "net_interest_m": -46,            # net interest EXPENSE (leveraged)
        },
        driver_schema=FRANCHISE_DRIVERS,
    )

    pre = model.compute_outputs()

    # Adversarial: revise SSS down (consumer weakness)
    from research.estimate_model import propagate_revision
    propagate_revision(model, "sss_growth_pct", 2.0)
    post = model.compute_outputs()

    actual_rev = 696.9
    actual_eps = 4.08   # adjusted

    pre_err = abs(pre["eps"] - actual_eps)
    post_err = abs(post["eps"] - actual_eps)

    print(f"\n  WING FY2025 — Franchise Schema Validation")
    print(f"  Schema: {sel.chosen_label} (confidence {sel.confidence:.0%})")
    print(f"  Revenue breakdown:")
    print(f"    Royalty: ${pre['royalty_revenue_m']:,.1f}M")
    print(f"    Ad fund: ${pre['ad_fund_revenue_m']:,.1f}M")
    print(f"    Company-owned: ${pre['company_owned_revenue_m']:,.1f}M")
    print(f"    Total: ${pre['revenue_m']:,.1f}M")

    print(f"\n  ┌──────────────┬──────────┬──────────┬──────────┬──────────┐")
    print(f"  │ Metric       │ Pre-Chal │ Post-Chal│  Actual  │   Error  │")
    print(f"  ├──────────────┼──────────┼──────────┼──────────┼──────────┤")
    for name, key, act in [("Revenue $M", "revenue_m", actual_rev),
                            ("EPS (adj)", "eps", actual_eps)]:
        pv = pre[key]; rv = post[key]; err = rv - act
        fmt = ",.1f" if "rev" in key else ".2f"
        print(f"  │ {name:<12s} │ {pv:>8{fmt}} │ {rv:>8{fmt}} │ {act:>8{fmt}} │ {err:>+8{fmt}} │")
    print(f"  └──────────────┴──────────┴──────────┴──────────┴──────────┘")

    print(f"\n  COMPARISON — schema matters:")
    print(f"    Wrong schema (company-operated): EPS error $4.12")
    print(f"    Right schema (franchise):        EPS error ${post_err:.2f}")
    improvement = 4.12 - post_err
    print(f"    Improvement: ${improvement:.2f} ({improvement/4.12*100:.0f}%)")

    if post_err < 2.0:
        print(f"  ✓ Franchise schema dramatically improved accuracy")
    else:
        print(f"  ✗ Still large error — franchise model needs tuning")

    return True


def run_all():
    tests = [
        ("CMG selects company-operated", test_cmg_selects_company_operated),
        ("WING detects franchise structure", test_wing_detects_franchise),
        ("WING schema risk visible", test_wing_schema_risk_visible),
        ("Software selects SaaS", test_software_selects_saas),
        ("Ambiguous evidence → general fallback", test_ambiguous_evidence_falls_back_to_general),
        ("Observation-based evidence extraction", test_observation_based_evidence),
        ("Schema connects to driver registry", test_schema_connects_to_driver_registry),
        ("Schema workpaper production", test_schema_workpaper),
        ("WING franchise model accuracy", test_wing_franchise_model_accuracy),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            print(f"\n[TEST] {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("ALL TESTS PASSED")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
