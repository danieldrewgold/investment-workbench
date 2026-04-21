"""
Golden tests — end-to-end evidence lineage + output contract validation.
All SQL matches the existing canonical schema column names.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance.database import init_db, new_id, create_run, upsert, hash_content
from core.contracts.output_contracts import (
    ResearchPlanOutput, ThesisOutput, EstimateSummary, CaseSummary,
    PitchPackage, validate_output,
)
from tests.fixtures.sample_data import GOLDEN_RESEARCH_PLAN_CMG


def test_research_plan_contract_valid():
    plan = ResearchPlanOutput(**GOLDEN_RESEARCH_PLAN_CMG)
    v = plan.validate()
    assert v == [], f"Should be valid: {v}"
    print("  Valid plan: 0 violations")


def test_research_plan_contract_rejects_bad():
    bad = ResearchPlanOutput(
        ticker="CMG", edge_hypothesis="Maybe something",
        edge_type="", why_opportunity_exists="", what_makes_valuable="",
        key_questions=[], key_drivers=[], kill_conditions=[],
        workstreams=[{"type": "KPI_FORECAST"}],  # missing justification
        run_id="test",
    )
    v = bad.validate()
    assert len(v) >= 5, f"Expected many violations, got {len(v)}"
    print(f"  Invalid plan: {len(v)} violations caught")


def test_thesis_rejects_weak_bear():
    t = ThesisOutput(
        ticker="CMG", direction="LONG", edge_hypothesis="SSS underpriced",
        edge_type="EXPECTATION_GAP",
        key_claims=[{"claim": "SSS up", "evidence": "Q1 8%", "confidence": "HIGH", "affects": "REV"}],
        key_risks=["Macro"], bear_case="It goes down",  # too short
        what_would_falsify="Decel", confidence="HIGH",
        estimate_summary={"eps": 1.25},
        is_novel=True, is_decision_useful=True, is_worth_sharing=True,
        overall_verdict="SHARE", run_id="test",
    )
    v = t.validate()
    assert any("bear_case" in x for x in v), "Should flag weak bear case"
    print(f"  Weak bear case caught")


def test_thesis_rejects_missing_evidence():
    t = ThesisOutput(
        ticker="CMG", direction="LONG", edge_hypothesis="SSS underpriced",
        edge_type="EXPECTATION_GAP",
        key_claims=[{"claim": "SSS accelerating", "affects": "REVENUE"}],  # no evidence
        key_risks=["Macro", "Comp"],
        bear_case="Macro downturn crushes discretionary, SSS to 1-2% for 3+ quarters",
        what_would_falsify="SSS below 3%", confidence="HIGH",
        estimate_summary={"eps": 1.25},
        is_novel=True, is_decision_useful=True, is_worth_sharing=True,
        overall_verdict="SHARE", run_id="test",
    )
    v = t.validate()
    assert any("evidence" in x for x in v), "Should flag missing evidence"
    print(f"  Missing evidence caught")


def test_estimate_bad_probabilities():
    est = EstimateSummary(
        ticker="CMG",
        cases=[
            CaseSummary(label="BASE", probability=0.5,
                       key_assumptions=[{"metric": "SSS", "value": 8, "rationale": "throughput"}],
                       outputs={"EPS": 1.25}, vs_consensus={"EPS": 0.07}),
            CaseSummary(label="BULL", probability=0.4,
                       key_assumptions=[{"metric": "SSS", "value": 11, "rationale": "pricing"}],
                       outputs={"EPS": 1.40}, vs_consensus={"EPS": 0.22}),
        ],  # sums to 0.9
        key_drivers=["SSS"], where_we_differ="Higher SSS",
        biggest_uncertainty="Consumer spend", run_id="test",
    )
    v = est.validate()
    assert any("probabilities" in x for x in v)
    print(f"  Bad probabilities caught")


def test_pitch_rejects_empty_thesis_id():
    p = PitchPackage(
        ticker="CMG", direction="LONG", one_liner="SSS underpriced",
        source_of_edge="Expectation gap", why_now="Q1 in 26d",
        key_driver_summary="SSS 8% vs street 5%",
        scenario_summary={"base": {"eps": 1.25}},
        valuation_framing="45x fwd", key_risks=["Macro", "Comp"],
        what_would_change_mind="SSS<3%", evidence_quality="strong",
        thesis_id="", run_id="test",
    )
    v = p.validate()
    assert any("thesis_id" in x for x in v)
    print(f"  Missing thesis_id caught")


def test_end_to_end_db_lineage():
    """
    Full golden test: company -> source_document -> evidence_item ->
    claim -> claim_evidence_link -> thesis -> estimate_case -> estimate_output.
    Verify the entire chain is reconstructible.
    """
    conn = init_db(Path(":memory:"))

    with create_run(conn, "research", {"ticker": "CMG"}) as run:
        # Company
        cid = new_id()
        conn.execute(
            "INSERT INTO company (company_id, name, ticker, cik, sic_code) VALUES (?,?,?,?,?)",
            (cid, "Chipotle Mexican Grill", "CMG", "0001058090", "5812"))

        # Reporting period
        pid = new_id()
        conn.execute(
            "INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year) VALUES (?,?,?,?)",
            (pid, cid, "FY", 2025))

        # Source document (10-K)
        did = new_id()
        conn.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator,
                source_published_at, fetched_at, company_id, run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (did, "FILING", "SEC_EDGAR",
             "https://sec.gov/Archives/edgar/data/1058090/000105809026000012/cmg-20251231.htm",
             "2026-02-14", "2026-03-27T10:00:00Z", cid, run.run_id))

        # Evidence item
        eid = new_id()
        conn.execute(
            """INSERT INTO evidence_item
               (evidence_id, document_id, company_id, evidence_type, evidence_key,
                value, as_of_date, run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (eid, did, cid, "FINANCIAL_DATA", "fy2025_sss_growth",
             "7.4% comp growth: 4.1% pricing + 3.3% transactions", "2025-12-31", run.run_id))

        # Research plan
        plid = new_id()
        conn.execute(
            """INSERT INTO research_plan
               (plan_id, company_id, plan_version, edge_type, edge_hypothesis,
                why_now, status, created_by_run)
               VALUES (?,?,?,?,?,?,?,?)""",
            (plid, cid, 1, "EXPECTATION_GAP",
             "SSS acceleration to 8-10% not in consensus 4-5%",
             "Throughput rollout accelerating", "active", run.run_id))

        # Thesis
        tid = new_id()
        conn.execute(
            """INSERT INTO thesis
               (thesis_id, company_id, plan_id, thesis_version, direction,
                conviction, one_liner, edge_source, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, cid, plid, 1, "LONG", "high",
             "SSS acceleration to 8%+ not priced into consensus 4-5%",
             "EXPECTATION_GAP", run.run_id))

        # Claim
        clid = new_id()
        conn.execute(
            """INSERT INTO claim
               (claim_id, company_id, thesis_id, plan_id, claim_text, claim_type,
                affects, confidence, falsifier, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (clid, cid, tid, plid,
             "SSS will accelerate to 8%+ in FY2026 driven by throughput and sustained pricing",
             "ESTIMATE", "REVENUE", 0.85,
             "SSS decelerates below 4% for two consecutive quarters", run.run_id))

        # Claim-evidence link
        conn.execute(
            """INSERT INTO claim_evidence_link (claim_id, evidence_id, role, importance, rationale)
               VALUES (?,?,?,?,?)""",
            (clid, eid, "supports", 1.0,
             "FY2025 showed 7.4% SSS with only partial throughput rollout"))

        # Estimate case
        ecid = new_id()
        conn.execute(
            """INSERT INTO estimate_case
               (case_id, company_id, plan_id, case_name, case_version,
                scenario_weight, created_by_run)
               VALUES (?,?,?,?,?,?,?)""",
            (ecid, cid, plid, "base", 1, 0.60, run.run_id))

        # Key driver
        drid = new_id()
        conn.execute(
            """INSERT INTO key_driver
               (driver_id, plan_id, driver_name, transmission)
               VALUES (?,?,?,?)""",
            (drid, plid, "SSS growth",
             "SSS -> revenue -> operating leverage -> EPS beat -> re-rate"))

        # Estimate driver link
        conn.execute(
            "INSERT INTO estimate_driver (case_id, driver_id, driver_value, driver_impact) VALUES (?,?,?,?)",
            (ecid, drid, 8.5, "+3.5% vs consensus"))

        # Estimate assumption
        conn.execute(
            """INSERT INTO estimate_assumption
               (assumption_id, case_id, assumption_key, assumption_value,
                basis, evidence_id)
               VALUES (?,?,?,?,?,?)""",
            (new_id(), ecid, "sss_growth_pct", 8.5,
             "Throughput + pricing sustaining above historical", eid))

        # Estimate output
        conn.execute(
            """INSERT INTO estimate_output
               (output_id, case_id, period_id, line_item, value, vs_consensus)
               VALUES (?,?,?,?,?,?)""",
            (new_id(), ecid, pid, "EPS", 1.25, 0.07))

        conn.commit()

    # ── VERIFY: trace from estimate output back to source document ──
    row = conn.execute("""
        SELECT
            eo.line_item,
            eo.value AS our_estimate,
            eo.vs_consensus,
            ea.basis,
            ei.value AS evidence_text,
            sd.source_name,
            sd.source_locator,
            c.claim_text,
            c.falsifier,
            t.one_liner
        FROM estimate_output eo
        JOIN estimate_case ec ON eo.case_id = ec.case_id
        JOIN estimate_assumption ea ON ea.case_id = ec.case_id
        JOIN evidence_item ei ON ea.evidence_id = ei.evidence_id
        JOIN source_document sd ON ei.document_id = sd.document_id
        JOIN thesis t ON ec.plan_id = t.plan_id AND t.company_id = ec.company_id
        JOIN claim c ON c.thesis_id = t.thesis_id
        WHERE ec.case_name = 'base'
    """).fetchone()

    assert row is not None, "Should find complete lineage"

    print(f"  Full lineage trace:")
    print(f"    Thesis: {row[9][:70]}...")
    print(f"    Claim: {row[7][:70]}...")
    print(f"    Evidence: {row[4][:70]}...")
    print(f"    Source: {row[5]} at ...{row[6][-40:]}")
    print(f"    Assumption basis: {row[3][:60]}...")
    print(f"    Output: {row[0]} = ${row[1]} (vs consensus +${row[2]})")
    print(f"    Falsifier: {row[8]}")

    assert row[0] == "EPS"
    assert row[1] == 1.25
    assert row[2] == 0.07
    assert "SEC_EDGAR" in row[5]
    assert "7.4%" in row[4]

    conn.close()


def run_all():
    tests = [
        ("Research plan contract (valid)", test_research_plan_contract_valid),
        ("Research plan contract (invalid)", test_research_plan_contract_rejects_bad),
        ("Thesis: weak bear case", test_thesis_rejects_weak_bear),
        ("Thesis: missing evidence", test_thesis_rejects_missing_evidence),
        ("Estimate: bad probabilities", test_estimate_bad_probabilities),
        ("Pitch: missing thesis_id", test_pitch_rejects_empty_thesis_id),
        ("End-to-end DB lineage", test_end_to_end_db_lineage),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            print(f"\n[GOLDEN] {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}\n{passed} passed, {failed} failed")
    print("ALL GOLDEN TESTS PASSED" if failed == 0 else "FAILURES")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
