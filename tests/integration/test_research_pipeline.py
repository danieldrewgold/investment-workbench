"""
Integration tests for:
1. Research designer pipeline (Layer 2 operations)
2. Loader provenance chain verification
3. Plan completeness validation
4. Full research lifecycle: company -> plan -> evidence -> claim -> estimate
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance.database import init_db, new_id, create_run, upsert, hash_content
from research.planning.research_designer import (
    ResearchDesigner, build_research_plan_from_dict,
    ANALYTICAL_LENSES, EDGE_TYPES,
)
from tests.fixtures.sample_data import GOLDEN_RESEARCH_PLAN_CMG


def test_research_designer_create_plan():
    """Create a research plan with all components via the designer API."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research", {"ticker": "CMG"}) as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "Chipotle", "CMG"))
        conn.commit()

        designer = ResearchDesigner(conn)
        plan_id = designer.create_plan(
            company_id=cid,
            edge_hypothesis="SSS acceleration to 8-10% not in consensus",
            edge_type="EXPECTATION_GAP",
            why_now="Throughput rollout accelerating",
            run_id=run.run_id,
        )

        designer.add_question(plan_id, "Throughput contribution to traffic?", "high")
        designer.add_question(plan_id, "Pricing power sustainable?", "high")
        designer.add_question(plan_id, "Digital mix margin impact?", "medium")

        designer.add_driver(plan_id, "SSS growth",
                          transmission="SSS -> revenue -> leverage -> EPS beat",
                          current_consensus="4-5%",
                          independent_view="8-10%")

        designer.add_workstream(plan_id, "KPI_FORECAST",
                              justification="SSS is the key driver")
        designer.add_workstream(plan_id, "OPERATING_BUILD",
                              justification="Need margin flowthrough model")

        designer.add_kill_condition(plan_id, "Q2 SSS below 3%")

    # Verify
    summary = designer.get_plan_summary(plan_id)
    assert summary is not None
    assert len(summary["questions"]) == 3
    assert len(summary["drivers"]) == 1
    assert len(summary["workstreams"]) == 2
    assert len(summary["kill_conditions"]) == 1
    assert summary["plan"]["edge_type"] == "EXPECTATION_GAP"

    print(f"  Plan created: {len(summary['questions'])}Q, "
          f"{len(summary['drivers'])}D, {len(summary['workstreams'])}W, "
          f"{len(summary['kill_conditions'])}K")
    print(f"  Edge: {summary['plan']['edge_hypothesis'][:60]}...")
    conn.close()


def test_plan_validation_catches_incomplete():
    """Incomplete plans should be caught before downstream work proceeds."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research") as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "Test", "TST"))
        conn.commit()

        designer = ResearchDesigner(conn)

        # Minimal plan: hypothesis only, nothing else
        plan_id = designer.create_plan(
            company_id=cid,
            edge_hypothesis="Something is mispriced",
            edge_type="EXPECTATION_GAP",
            run_id=run.run_id,
        )

    issues = designer.validate_plan_completeness(plan_id)
    assert len(issues) >= 3, f"Expected multiple issues, got {len(issues)}"
    print(f"  Incomplete plan: {len(issues)} issues caught")
    for issue in issues:
        print(f"    - {issue}")

    conn.close()


def test_plan_validation_passes_complete():
    """A complete plan should pass validation."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research") as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "Chipotle", "CMG"))
        conn.commit()

        plan_id = build_research_plan_from_dict(conn, cid, GOLDEN_RESEARCH_PLAN_CMG, run.run_id)

    designer = ResearchDesigner(conn)
    issues = designer.validate_plan_completeness(plan_id)
    assert issues == [], f"Complete plan should pass: {issues}"
    print(f"  Complete plan: 0 issues (passed validation)")

    conn.close()


def test_plan_versioning():
    """Multiple plans for the same company should get incrementing versions."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research") as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "CMG", "CMG"))
        conn.commit()

        designer = ResearchDesigner(conn)
        p1 = designer.create_plan(cid, "V1 hypothesis", "EXPECTATION_GAP", run_id=run.run_id)
        p2 = designer.create_plan(cid, "V2 revised hypothesis", "QUALITY_GAP", run_id=run.run_id)

    v1 = conn.execute("SELECT plan_version FROM research_plan WHERE plan_id=?", (p1,)).fetchone()[0]
    v2 = conn.execute("SELECT plan_version FROM research_plan WHERE plan_id=?", (p2,)).fetchone()[0]
    assert v1 == 1
    assert v2 == 2
    print(f"  Plan versioning: v{v1} and v{v2}")

    conn.close()


def test_analytical_lens_suggestions():
    """Lens suggestions should be business-type-specific."""
    designer = ResearchDesigner(None)

    restaurant = designer.suggest_analytical_lenses("restaurant chain")
    assert "price_volume_mix" in restaurant
    assert "margin_bridge" in restaurant

    saas = designer.suggest_analytical_lenses("SaaS company")
    assert "cohort_retention" in saas

    bank = designer.suggest_analytical_lenses("regional bank")
    assert "spread_analysis" in bank

    print(f"  Restaurant: {restaurant[:3]}")
    print(f"  SaaS: {saas[:3]}")
    print(f"  Bank: {bank[:3]}")


def test_full_research_lifecycle():
    """
    End-to-end: company -> plan -> evidence collection -> claim -> estimate.
    Verify that every downstream object links back to the plan.
    """
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research", {"ticker": "CMG"}) as run:
        # 1. Company
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker, cik) VALUES (?,?,?,?)",
                     (cid, "Chipotle", "CMG", "0001058090"))

        # 2. Research plan (Layer 2)
        plan_id = build_research_plan_from_dict(conn, cid, GOLDEN_RESEARCH_PLAN_CMG, run.run_id)

        # 3. Evidence collection (Layer 3) — linked to plan via company
        doc_id = new_id()
        conn.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator,
                fetched_at, company_id, run_id)
               VALUES (?,?,?,?,?,?,?)""",
            (doc_id, "FILING", "SEC_EDGAR", "https://sec.gov/cmg/10k",
             "2026-03-27T10:00:00Z", cid, run.run_id))

        ev_id = new_id()
        conn.execute(
            """INSERT INTO evidence_item
               (evidence_id, document_id, company_id, evidence_type,
                evidence_key, value, as_of_date, run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ev_id, doc_id, cid, "FINANCIAL_DATA", "fy2025_sss",
             "7.4% comp growth", "2025-12-31", run.run_id))

        # 4. Thesis (Layer 4) — linked to plan
        tid = new_id()
        conn.execute(
            """INSERT INTO thesis
               (thesis_id, company_id, plan_id, thesis_version, direction,
                conviction, one_liner, edge_source, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, cid, plan_id, 1, "LONG", "high",
             "SSS acceleration underpriced", "EXPECTATION_GAP", run.run_id))

        # 5. Claim linked to thesis AND evidence
        cl_id = new_id()
        conn.execute(
            """INSERT INTO claim
               (claim_id, company_id, thesis_id, plan_id, claim_text,
                claim_type, affects, confidence, falsifier, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cl_id, cid, tid, plan_id,
             "SSS will be 8%+ in FY2026", "ESTIMATE", "REVENUE",
             0.85, "SSS below 4% for 2Q", run.run_id))

        conn.execute(
            "INSERT INTO claim_evidence_link (claim_id, evidence_id, role, importance, rationale) VALUES (?,?,?,?,?)",
            (cl_id, ev_id, "supports", 1.0, "FY2025 showed 7.4% with partial rollout"))

        # 6. Estimate case linked to plan
        ec_id = new_id()
        conn.execute(
            """INSERT INTO estimate_case
               (case_id, company_id, plan_id, case_name, scenario_weight, created_by_run)
               VALUES (?,?,?,?,?,?)""",
            (ec_id, cid, plan_id, "base", 0.60, run.run_id))

        # 7. Reporting period + estimate output
        pid = new_id()
        conn.execute(
            "INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year) VALUES (?,?,?,?)",
            (pid, cid, "FY", 2026))

        conn.execute(
            """INSERT INTO estimate_output
               (output_id, case_id, period_id, line_item, value, vs_consensus)
               VALUES (?,?,?,?,?,?)""",
            (new_id(), ec_id, pid, "EPS", 1.25, 0.07))

        conn.commit()

    # ── Verify: everything traces back to the research plan ──
    result = conn.execute("""
        SELECT
            rp.edge_hypothesis,
            t.one_liner,
            c.claim_text,
            ei.value AS evidence,
            eo.line_item,
            eo.value AS estimate,
            eo.vs_consensus,
            kd.driver_name,
            kd.transmission
        FROM research_plan rp
        JOIN thesis t ON t.plan_id = rp.plan_id
        JOIN claim c ON c.plan_id = rp.plan_id
        JOIN claim_evidence_link cel ON cel.claim_id = c.claim_id
        JOIN evidence_item ei ON cel.evidence_id = ei.evidence_id
        JOIN estimate_case ec ON ec.plan_id = rp.plan_id
        JOIN estimate_output eo ON eo.case_id = ec.case_id
        JOIN key_driver kd ON kd.plan_id = rp.plan_id
        WHERE rp.plan_id = ?
    """, (plan_id,)).fetchone()

    assert result is not None, "Full lifecycle chain should be traceable"
    print(f"  Full research lifecycle:")
    print(f"    Plan: {result[0][:60]}...")
    print(f"    Thesis: {result[1]}")
    print(f"    Claim: {result[2]}")
    print(f"    Evidence: {result[3]}")
    print(f"    Estimate: {result[4]} = ${result[5]} (+${result[6]} vs consensus)")
    print(f"    Driver: {result[7]} via {result[8][:50]}...")

    # Verify plan has all components
    counts = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM research_question WHERE plan_id = ?),
            (SELECT COUNT(*) FROM key_driver WHERE plan_id = ?),
            (SELECT COUNT(*) FROM workstream WHERE plan_id = ?),
            (SELECT COUNT(*) FROM kill_condition WHERE plan_id = ?)
    """, (plan_id, plan_id, plan_id, plan_id)).fetchone()

    print(f"    Plan components: {counts[0]}Q {counts[1]}D {counts[2]}W {counts[3]}K")
    assert all(c > 0 for c in counts), "All plan components should exist"

    conn.close()


def run_all():
    tests = [
        ("Create research plan", test_research_designer_create_plan),
        ("Validate incomplete plan", test_plan_validation_catches_incomplete),
        ("Validate complete plan", test_plan_validation_passes_complete),
        ("Plan versioning", test_plan_versioning),
        ("Analytical lens suggestions", test_analytical_lens_suggestions),
        ("Full research lifecycle", test_full_research_lifecycle),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            print(f"\n[INTEGRATION] {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}\n{passed} passed, {failed} failed")
    print("ALL INTEGRATION TESTS PASSED" if failed == 0 else "FAILURES")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
