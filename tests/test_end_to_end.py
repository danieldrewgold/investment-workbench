"""
End-to-end test for Tasks 1-5.

Proves one honest investment workflow:
  plan -> gate -> evidence -> estimate -> claim -> decision

Uses CMG (Chipotle) as the test company.
Uses fundamental evidence, not flow evidence.

Each test section maps to a task:
  T1: Plan gate blocks unauthorized work
  T2: Real estimate workflow (actuals, guidance, consensus, independent)
  T3: Evidence -> claim -> estimate wiring
  T4: Decision gate classifies output
  T5: Insider overlay runs only when authorized
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.provenance.database import init_db, new_id, upsert, RunContext, now_iso
from research.planning.research_designer import ResearchDesigner
from research.planning.plan_gate import PlanGate, PlanGateError
from research.core_workflow import (
    EstimateBuilder, ClaimBuilder, DecisionGate, InsiderOverlayAdapter,
)


def setup_cmg(conn) -> dict:
    """Create CMG company, research plan, and basic data. Returns IDs."""
    with RunContext(conn, "test_setup") as run:
        # Company
        cid = new_id()
        conn.execute(
            "INSERT INTO company (company_id, name, ticker, cik) VALUES (?,?,?,?)",
            (cid, "Chipotle Mexican Grill", "CMG", "0001058090"))

        # Research plan with specific workstreams
        d = ResearchDesigner(conn)
        plan_id = d.create_plan(
            company_id=cid,
            edge_hypothesis="SSS acceleration to 8%+ not in consensus 4-5%",
            edge_type="EXPECTATION_GAP",
            why_now="Throughput rollout + sustained pricing",
            run_id=run.run_id,
        )

        # Core workstreams only — no overlays by default
        d.add_workstream(plan_id, "KPI_FORECAST",
                        justification="SSS is the key driver, need independent estimate")
        d.add_workstream(plan_id, "OPERATING_BUILD",
                        justification="Margin flowthrough from SSS to EPS")
        d.add_workstream(plan_id, "GUIDANCE_COMPARISON",
                        justification="Management guided 4-7%, our estimate is above range")

        d.add_driver(plan_id, "SSS_growth",
                    transmission="SSS -> revenue -> operating leverage -> EPS",
                    current_consensus="4-5%",
                    independent_view="8-10%")

        d.add_question(plan_id, "Throughput contribution to traffic growth?", "high")
        d.add_question(plan_id, "Is pricing power sustainable without traffic erosion?", "high")

        d.add_kill_condition(plan_id, "Q2 SSS below 3%")

        # Reporting periods
        periods = {}
        for fy in [2025, 2026]:
            pid = new_id()
            conn.execute(
                "INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year) VALUES (?,?,?,?)",
                (pid, cid, "FY", fy))
            periods[f"FY{fy}"] = pid

        # Source document (10-K)
        doc_id = new_id()
        conn.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator,
                source_published_at, fetched_at, company_id, run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (doc_id, "FILING", "SEC_EDGAR",
             "https://sec.gov/cmg/10k-fy2025",
             "2026-02-14", now_iso(), cid, run.run_id))

        # Metric definitions
        metrics = {}
        for name, source, unit in [
            ("revenue", "SEC_FILING", "USD_M"),
            ("ebit_margin", "SEC_FILING", "PCT"),
            ("eps", "SEC_FILING", "USD"),
            ("sss_growth", "SEC_FILING", "PCT"),
        ]:
            mid = new_id()
            upsert(conn, "metric_definition", {
                "metric_id": mid, "metric_name": name,
                "metric_source": source, "unit": unit,
            }, conflict_columns=["metric_name", "metric_source"])
            metrics[name] = mid

        conn.commit()

    return {
        "company_id": cid, "plan_id": plan_id, "run_id": run.run_id,
        "periods": periods, "doc_id": doc_id, "metrics": metrics,
    }


# ═══════════════════════════════════════════════════════════════
# TASK 1: Plan gate tests
# ═══════════════════════════════════════════════════════════════

def test_t1_plan_gate_allows_authorized_work(conn, ids):
    """In-plan work should be allowed."""
    gate = PlanGate(conn, ids["plan_id"])

    # KPI_FORECAST workstream authorizes these
    r1 = gate.check("ingest_actuals")
    assert r1.allowed, f"Should allow ingest_actuals: {r1.reason}"
    r2 = gate.check("build_estimate")
    assert r2.allowed, f"Should allow build_estimate: {r2.reason}"
    r3 = gate.check("ingest_consensus")
    assert r3.allowed, f"Should allow ingest_consensus: {r3.reason}"
    # GUIDANCE_COMPARISON authorizes this
    r4 = gate.check("ingest_guidance")
    assert r4.allowed, f"Should allow ingest_guidance: {r4.reason}"

    print("  Authorized operations: ingest_actuals, build_estimate, ingest_consensus, ingest_guidance")
    return True


def test_t1_plan_gate_blocks_unauthorized(conn, ids):
    """Out-of-plan work should be blocked."""
    gate = PlanGate(conn, ids["plan_id"])

    # No INSIDER_ACTIVITY workstream in plan
    r1 = gate.check("ingest_insider")
    assert not r1.allowed, "Should block ingest_insider"
    assert r1.requires_amendment

    # No OPTIONS_FLOW workstream
    r2 = gate.check("ingest_options_flow")
    assert not r2.allowed, "Should block ingest_options_flow"

    # No DARK_POOL workstream
    r3 = gate.check("ingest_dark_pool")
    assert not r3.allowed, "Should block ingest_dark_pool"

    print(f"  Blocked: ingest_insider, ingest_options_flow, ingest_dark_pool")
    print(f"  Reason: {r1.reason[:80]}...")
    return True


def test_t1_plan_gate_allows_after_amendment(conn, ids):
    """Amending the plan should authorize previously blocked work."""
    gate = PlanGate(conn, ids["plan_id"])

    # Blocked before amendment
    r1 = gate.check("ingest_insider")
    assert not r1.allowed

    # Amend the plan
    gate.amend_plan(
        "INSIDER_ACTIVITY",
        justification="CEO buying cluster detected during routine filing review — "
                      "warrants investigation as management confidence signal"
    )

    # Now allowed
    r2 = gate.check("ingest_insider")
    assert r2.allowed, f"Should allow after amendment: {r2.reason}"

    print(f"  After amendment: ingest_insider now allowed via INSIDER_ACTIVITY workstream")
    return True


def test_t1_plan_gate_rejects_amendment_without_justification(conn, ids):
    """Cannot amend plan without justification."""
    gate = PlanGate(conn, ids["plan_id"])
    try:
        gate.amend_plan("OPTIONS_FLOW", justification="")
        assert False, "Should reject empty justification"
    except PlanGateError:
        print("  Correctly rejected amendment without justification")
    return True


# ═══════════════════════════════════════════════════════════════
# TASK 2: Estimate workflow tests
# ═══════════════════════════════════════════════════════════════

def test_t2_actuals_guidance_consensus(conn, ids):
    """Store historical actuals, guidance, and consensus for CMG."""
    with RunContext(conn, "ingest", {"task": "t2_data"}) as run:
        cid = ids["company_id"]
        fy25 = ids["periods"]["FY2025"]
        fy26 = ids["periods"]["FY2026"]
        doc = ids["doc_id"]
        m = ids["metrics"]

        # FY2025 Actuals (from 10-K)
        for metric, period, value in [
            ("revenue", fy25, 11312),     # $11.312B
            ("ebit_margin", fy25, 16.8),  # 16.8%
            ("eps", fy25, 1.09),          # $1.09 (post-split)
            ("sss_growth", fy25, 7.4),    # 7.4%
        ]:
            upsert(conn, "company_metric_series", {
                "series_id": new_id(),
                "company_id": cid,
                "metric_id": m[metric],
                "period_id": period,
                "value": value,
                "source_document_id": doc,
                "run_id": run.run_id,
            }, conflict_columns=["company_id", "metric_id", "period_id", "source_document_id"],
            update_columns=["value", "run_id"])

            # Evidence item for each actual
            upsert(conn, "evidence_item", {
                "evidence_id": new_id(),
                "document_id": doc,
                "company_id": cid,
                "evidence_type": "REPORTED_ACTUAL",
                "evidence_key": f"fy2025_{metric}",
                "value": f"FY2025 {metric}: {value}",
                "value_numeric": value,
                "as_of_date": "2025-12-31",
                "run_id": run.run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

        # FY2026 Guidance
        guidance_doc = new_id()
        conn.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator,
                fetched_at, company_id, run_id)
               VALUES (?,?,?,?,?,?,?)""",
            (guidance_doc, "FILING", "COMPANY_GUIDANCE",
             "cmg:guidance:fy2026", now_iso(), cid, run.run_id))

        for metric, low, high, mid in [
            ("sss_growth", 4.0, 7.0, 5.5),
            ("ebit_margin", 16.5, 17.5, 17.0),
        ]:
            upsert(conn, "guidance_point", {
                "guidance_id": new_id(),
                "company_id": cid,
                "metric_id": m[metric],
                "period_id": fy26,
                "guidance_type": "initial",
                "value_low": low, "value_high": high, "value_point": mid,
                "guidance_date": "2026-02-04",
                "source_document_id": guidance_doc,
                "run_id": run.run_id,
            }, conflict_columns=["company_id", "metric_id", "period_id",
                                 "guidance_type", "source_document_id"],
            update_columns=["value_low", "value_high", "value_point", "run_id"])

        # FY2026 Consensus
        for metric, mean_val in [
            ("revenue", 12450),
            ("ebit_margin", 17.1),
            ("eps", 1.18),
        ]:
            upsert(conn, "consensus_snapshot", {
                "snapshot_id": new_id(),
                "company_id": cid,
                "metric_id": m[metric],
                "period_id": fy26,
                "as_of_date": "2026-03-25",
                "source_name": "CONSENSUS_ESTIMATE",
                "estimate_mean": mean_val,
                "run_id": run.run_id,
            }, conflict_columns=["company_id", "metric_id", "period_id",
                                 "as_of_date", "source_name"],
            update_columns=["estimate_mean", "run_id"])

        conn.commit()

    # Verify data
    actuals = conn.execute(
        "SELECT COUNT(*) FROM company_metric_series WHERE company_id=?",
        (cid,)).fetchone()[0]
    guidance = conn.execute(
        "SELECT COUNT(*) FROM guidance_point WHERE company_id=?",
        (cid,)).fetchone()[0]
    consensus = conn.execute(
        "SELECT COUNT(*) FROM consensus_snapshot WHERE company_id=?",
        (cid,)).fetchone()[0]

    assert actuals == 4, f"Expected 4 actuals, got {actuals}"
    assert guidance == 2, f"Expected 2 guidance, got {guidance}"
    assert consensus == 3, f"Expected 3 consensus, got {consensus}"

    print(f"  Data loaded: {actuals} actuals, {guidance} guidance, {consensus} consensus")
    return True


def test_t2_independent_estimate(conn, ids):
    """Build an independent estimate case with explicit assumptions."""
    cid = ids["company_id"]
    fy26 = ids["periods"]["FY2026"]
    m = ids["metrics"]

    with RunContext(conn, "derive", {"task": "t2_estimate"}) as run:
        builder = EstimateBuilder(conn, ids["plan_id"])

        # Base case
        case_id = builder.create_case(
            cid, "base", scenario_weight=0.60,
            summary="SSS acceleration to 8% on throughput + pricing, "
                    "60% incremental margin flowthrough",
            run_id=run.run_id,
        )

        # Get driver_id for SSS_growth
        driver = conn.execute(
            "SELECT driver_id FROM key_driver WHERE plan_id=? AND driver_name='SSS_growth'",
            (ids["plan_id"],)
        ).fetchone()
        assert driver, "SSS_growth driver should exist"
        driver_id = driver[0]

        # Link driver
        builder.link_driver(case_id, driver_id,
                           driver_value=8.0,
                           driver_impact="Revenue +$450M vs consensus, EPS +$0.07")

        # Assumptions — each typed explicitly
        builder.set_assumption(case_id, "sss_growth_pct", 8.0,
                              assumption_type="INDEPENDENT",
                              basis="Throughput improvement (3.5% transaction growth) + "
                                    "menu pricing (4.5%), partially offset by mix",
                              confidence=0.75)

        builder.set_assumption(case_id, "ebit_margin_pct", 17.5,
                              assumption_type="INDEPENDENT",
                              basis="60% incremental margin on SSS above 5% threshold",
                              confidence=0.65)

        builder.set_assumption(case_id, "share_count", 1370,
                              assumption_type="CONSENSUS_HELD",
                              basis="Street estimate, no independent view on buyback pace")

        builder.set_assumption(case_id, "tax_rate", 25.5,
                              assumption_type="CONSENSUS_HELD",
                              basis="Statutory rate, no adjustments")

        builder.set_assumption(case_id, "revenue_m", 12900,
                              assumption_type="INFERRED",
                              basis="FY2025 $11,312M * (1 + 8% SSS + 5% unit growth)")

        builder.set_assumption(case_id, "eps", 1.25,
                              assumption_type="INFERRED",
                              basis="Revenue $12.9B * 17.5% EBIT margin / 1370M shares * (1 - 25.5%)")

        # Outputs vs consensus
        builder.set_output(case_id, fy26, "revenue", 12900,
                          vs_consensus=450,  # vs $12,450M consensus
                          notes="Independent: throughput-driven SSS acceleration")

        builder.set_output(case_id, fy26, "ebit_margin", 17.5,
                          vs_consensus=0.4,  # vs 17.1% consensus
                          vs_guidance_mid=0.5,  # vs 17.0% guidance mid
                          notes="60bps above consensus on incremental margin leverage")

        builder.set_output(case_id, fy26, "eps", 1.25,
                          vs_consensus=0.07,  # vs $1.18 consensus
                          notes="$0.07 above street on SSS + margin")

        conn.commit()

    # Verify
    summary = builder.get_estimate_summary(cid)
    assert len(summary) >= 1, "Should have at least 1 case"
    base = summary[0]
    assert base["case_name"] == "base"
    assert len(base["assumptions"]) >= 5
    assert len(base["outputs"]) >= 3

    # Check assumption types
    types = {a["assumption_key"]: a["assumption_text"] for a in base["assumptions"]}
    assert "INDEPENDENT" in types.get("sss_growth_pct", "")
    assert "CONSENSUS_HELD" in types.get("share_count", "")
    assert "INFERRED" in types.get("eps", "")

    print(f"  Base case: {len(base['assumptions'])} assumptions, {len(base['outputs'])} outputs")
    print(f"    Revenue: $12,900M (+$450M vs consensus)")
    print(f"    EBIT margin: 17.5% (+40bps vs consensus)")
    print(f"    EPS: $1.25 (+$0.07 vs consensus)")

    ids["case_id"] = case_id
    return True


def test_t2_estimate_revision(conn, ids):
    """Revising an assumption should update the estimate cleanly."""
    cid = ids["company_id"]
    builder = EstimateBuilder(conn, ids["plan_id"])

    # Revise SSS assumption downward
    builder.set_assumption(ids["case_id"], "sss_growth_pct", 7.0,
                          assumption_type="INDEPENDENT",
                          basis="Revised down: pricing contribution lower than initially expected",
                          confidence=0.70)

    # Check it updated (not duplicated)
    row = conn.execute(
        "SELECT assumption_value, basis FROM estimate_assumption "
        "WHERE case_id=? AND assumption_key='sss_growth_pct'",
        (ids["case_id"],)
    ).fetchone()
    assert row[0] == 7.0, f"Should be 7.0, got {row[0]}"
    assert "Revised down" in row[1]

    count = conn.execute(
        "SELECT COUNT(*) FROM estimate_assumption WHERE case_id=? AND assumption_key='sss_growth_pct'",
        (ids["case_id"],)
    ).fetchone()[0]
    assert count == 1, f"Should be 1 row (upserted), got {count}"

    # Restore for subsequent tests
    builder.set_assumption(ids["case_id"], "sss_growth_pct", 8.0,
                          assumption_type="INDEPENDENT",
                          basis="Throughput + pricing sustaining",
                          confidence=0.75)

    print("  Revision: SSS updated 8.0 -> 7.0 -> 8.0, single row throughout")
    return True


# ═══════════════════════════════════════════════════════════════
# TASK 3: Evidence -> claim -> estimate wiring
# ═══════════════════════════════════════════════════════════════

def test_t3_evidence_claim_estimate_chain(conn, ids):
    """Build one real evidence -> claim -> estimate chain."""
    cid = ids["company_id"]

    with RunContext(conn, "analyze", {"task": "t3_claims"}) as run:
        cb = ClaimBuilder(conn)

        # Create thesis
        thesis_id = new_id()
        conn.execute(
            """INSERT INTO thesis
               (thesis_id, company_id, plan_id, thesis_version, direction,
                conviction, one_liner, edge_source, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (thesis_id, cid, ids["plan_id"], 1, "LONG", "medium",
             "SSS acceleration to 8%+ not priced into consensus 4-5%",
             "EXPECTATION_GAP", run.run_id))
        ids["thesis_id"] = thesis_id

        # Get evidence items (created in T2)
        ev_sss = conn.execute(
            "SELECT evidence_id FROM evidence_item WHERE company_id=? AND evidence_key='fy2025_sss_growth'",
            (cid,)
        ).fetchone()
        ev_margin = conn.execute(
            "SELECT evidence_id FROM evidence_item WHERE company_id=? AND evidence_key='fy2025_ebit_margin'",
            (cid,)
        ).fetchone()
        assert ev_sss and ev_margin, "Evidence items from T2 should exist"

        # Claim: margin upside more likely than consensus implies
        margin_claim_id = cb.create_claim(
            company_id=cid,
            plan_id=ids["plan_id"],
            thesis_id=thesis_id,
            claim_text="EBIT margin will expand 40bps beyond consensus to 17.5% "
                       "due to operating leverage on SSS above 5% threshold",
            claim_type="ESTIMATE",
            affects="EBIT_MARGIN, EPS",
            confidence=0.70,
            falsifier="Input costs (food, labor) rise >200bps YoY, "
                      "OR pricing realization falls below 3%",
            run_id=run.run_id,
        )
        ids["margin_claim_id"] = margin_claim_id

        # Link supporting evidence
        cb.link_evidence(margin_claim_id, ev_sss[0],
                        role="supports", importance=1.0,
                        rationale="FY2025 SSS was 7.4%, showing the SSS acceleration is real")

        cb.link_evidence(margin_claim_id, ev_margin[0],
                        role="supports", importance=0.8,
                        rationale="FY2025 EBIT margin was 16.8%, already trending up from FY2024")

        # Link to estimate assumption
        assumption = conn.execute(
            "SELECT assumption_id FROM estimate_assumption "
            "WHERE case_id=? AND assumption_key='ebit_margin_pct'",
            (ids["case_id"],)
        ).fetchone()
        assert assumption, "EBIT margin assumption should exist"

        cb.link_to_assumption(
            margin_claim_id, assumption[0],
            impact_direction="positive",
            impact_magnitude="+40bps vs consensus (17.5% vs 17.1%)",
            rationale="SSS-driven operating leverage supports higher margin",
        )

        conn.commit()

    # Verify the chain renders correctly
    rendered = cb.render_claim(margin_claim_id)
    assert rendered is not None
    assert len(rendered["supporting_evidence"]) >= 2
    assert len(rendered["contradicting_evidence"]) == 0
    assert len(rendered["estimate_impacts"]) >= 1
    assert rendered["falsifier"] is not None
    assert rendered["confidence"] == 0.70

    print(f"  Claim: {rendered['claim_text'][:70]}...")
    print(f"  Supporting evidence: {len(rendered['supporting_evidence'])} items")
    print(f"  Estimate impact: {rendered['estimate_impacts'][0]['impact_magnitude']}")
    print(f"  Confidence: {rendered['confidence']}")
    print(f"  Falsifier: {rendered['falsifier'][:60]}...")
    return True


# ═══════════════════════════════════════════════════════════════
# TASK 4: Decision gate
# ═══════════════════════════════════════════════════════════════

def test_t4_decision_gate_with_work(conn, ids):
    """Decision gate should assess the current state of work."""
    gate = DecisionGate(conn)
    result = gate.assess(ids["thesis_id"], ids["plan_id"], ids["company_id"])

    print(f"  Verdict: {result.verdict}")
    print(f"  {result.summary}")
    for c in result.criteria:
        icon = "✓" if c.passed else "✗"
        print(f"    {icon} {c.name}: {c.reason[:70]}")

    # Should be at least WORTH_DEEPER_WORK since we have:
    # - specific edge
    # - evidence
    # - estimate that differs from consensus
    # - traceable claim
    # - falsifier
    assert result.verdict in ("WORTH_DEEPER_WORK", "WORTH_PACKAGING"), \
        f"Expected at least WORTH_DEEPER_WORK, got {result.verdict}"

    # Record it
    with RunContext(conn, "decide") as run:
        gate.record_assessment(ids["thesis_id"], result, run.run_id)

    stored = conn.execute(
        "SELECT recommendation FROM decision_assessment WHERE thesis_id=?",
        (ids["thesis_id"],)
    ).fetchone()
    assert stored[0] == result.verdict
    print(f"  Assessment recorded: {stored[0]}")
    return True


def test_t4_decision_gate_blocks_weak_idea(conn, ids):
    """Decision gate should catch weak ideas."""
    cid = ids["company_id"]

    with RunContext(conn, "test") as run:
        # Create a bare thesis with no evidence
        weak_thesis = new_id()
        conn.execute(
            """INSERT INTO thesis
               (thesis_id, company_id, plan_id, thesis_version, direction,
                one_liner, created_by_run)
               VALUES (?,?,?,?,?,?,?)""",
            (weak_thesis, cid, ids["plan_id"], 99, "LONG",
             "Something might happen", run.run_id))
        conn.commit()

    gate = DecisionGate(conn)
    result = gate.assess(weak_thesis, ids["plan_id"], cid)

    assert result.verdict in ("NOT_VALUABLE_YET", "INTERESTING_BUT_NOT_ACTIONABLE"), \
        f"Weak idea should not pass: {result.verdict}"
    assert len(result.blocking_issues) > 0, "Should have blocking issues"

    print(f"  Weak idea verdict: {result.verdict}")
    print(f"  Blocking: {result.blocking_issues}")
    return True


# ═══════════════════════════════════════════════════════════════
# TASK 5: Insider overlay
# ═══════════════════════════════════════════════════════════════

def test_t5_insider_blocked_without_workstream(conn, ids):
    """Insider analysis should be blocked if not in original plan."""
    # Note: we already amended the plan in T1 test_t1_plan_gate_allows_after_amendment
    # So we need to check with a FRESH plan that doesn't have INSIDER_ACTIVITY
    cid = ids["company_id"]

    with RunContext(conn, "test") as run:
        d = ResearchDesigner(conn)
        fresh_plan = d.create_plan(cid, "Test plan", "EXPECTATION_GAP", run_id=run.run_id)
        d.add_workstream(fresh_plan, "KPI_FORECAST", justification="test")
        d.add_question(fresh_plan, "Test?", "high")
        d.add_question(fresh_plan, "Test2?", "high")
        d.add_driver(fresh_plan, "Test", transmission="test")
        d.add_kill_condition(fresh_plan, "test condition")
        conn.commit()

    gate = PlanGate(conn, fresh_plan)
    r = gate.check("ingest_insider")
    assert not r.allowed, "Insider should be blocked on plan without INSIDER_ACTIVITY"
    print(f"  Insider blocked on fresh plan: {r.reason[:60]}...")
    return True


def test_t5_insider_overlay_creates_evidence(conn, ids):
    """Insider overlay should create evidence items, not trigger synthesis."""
    cid = ids["company_id"]

    # First, create some insider transactions in the DB
    with RunContext(conn, "ingest") as run:
        doc_id = new_id()
        conn.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator,
                fetched_at, company_id, run_id)
               VALUES (?,?,?,?,?,?,?)""",
            (doc_id, "FILING", "SEC_EDGAR", "sec.gov/cmg/form4s",
             now_iso(), cid, run.run_id))

        for name, title, code, shares, price in [
            ("NICCOL SCOTT", "CEO", "P", 5000, 58.25),
            ("BRANDT JACK", "CFO", "P", 2000, 57.80),
            ("GARNER TABASSUM", "CMO", "P", 1500, 58.10),
            ("JONES SARAH", "VP", "S", 800, 61.50),
        ]:
            upsert(conn, "insider_transaction", {
                "transaction_id": new_id(),
                "company_id": cid,
                "insider_name": name,
                "insider_title": title,
                "transaction_date": "2026-03-10",
                "transaction_code": code,
                "transaction_type": "PURCHASE" if code == "P" else "SALE",
                "shares": shares,
                "price": price,
                "value": shares * price,
                "source_document_id": doc_id,
                "run_id": run.run_id,
            }, conflict_columns=["company_id", "insider_name", "transaction_date",
                                 "transaction_code", "shares"],
            update_columns=["price", "value", "run_id"])
        conn.commit()

    # Run the overlay adapter
    with RunContext(conn, "derive") as run:
        adapter = InsiderOverlayAdapter(conn)
        result = adapter.analyze_insider_activity(cid, ids["plan_id"], run.run_id)

    assert result["pattern"] == "CLUSTER_BUYING"
    assert result["purchases"] == 3
    assert result["sales"] == 1
    assert len(result["evidence_ids"]) >= 1

    # Verify evidence items were created (not claims, not synthesis)
    ev = conn.execute(
        "SELECT COUNT(*) FROM evidence_item WHERE company_id=? AND evidence_type='INSIDER_PATTERN'",
        (cid,)
    ).fetchone()[0]
    assert ev >= 1

    # Verify NO claims were auto-created
    auto_claims = conn.execute(
        "SELECT COUNT(*) FROM claim WHERE company_id=? AND claim_type='INSIDER_AUTO'",
        (cid,)
    ).fetchone()[0]
    assert auto_claims == 0, "Overlay should not auto-create claims"

    print(f"  Pattern: {result['pattern']}")
    print(f"  Purchases: {result['purchases']} (${result['purchase_value']:,.0f})")
    print(f"  Sales: {result['sales']} (${result['sale_value']:,.0f})")
    print(f"  Evidence items created: {len(result['evidence_ids'])}")
    print(f"  Claims auto-created: 0 (correct: overlay does not trigger synthesis)")
    return True


def test_t5_insider_evidence_usable_as_claim_support(conn, ids):
    """Insider evidence should be linkable to claims as supporting evidence."""
    cid = ids["company_id"]
    cb = ClaimBuilder(conn)

    # Get the insider pattern evidence
    ev = conn.execute(
        "SELECT evidence_id FROM evidence_item WHERE company_id=? AND evidence_type='INSIDER_PATTERN'",
        (cid,)
    ).fetchone()
    assert ev, "Insider evidence should exist"

    # Link it to our existing margin claim as supporting evidence
    cb.link_evidence(ids["margin_claim_id"], ev[0],
                    role="supports", importance=0.5,
                    rationale="C-suite buying cluster supports management confidence in SSS acceleration")

    # Verify the rendered claim now includes insider evidence
    rendered = cb.render_claim(ids["margin_claim_id"])
    insider_ev = [e for e in rendered["supporting_evidence"]
                  if e["evidence_type"] == "INSIDER_PATTERN"]
    assert len(insider_ev) == 1

    print(f"  Insider evidence linked to margin claim as supporting")
    print(f"  Total supporting evidence on claim: {len(rendered['supporting_evidence'])}")
    return True


# ═══════════════════════════════════════════════════════════════
# FULL LINEAGE VERIFICATION
# ═══════════════════════════════════════════════════════════════

def test_full_lineage_trace(conn, ids):
    """
    The final test: trace from estimate output back through
    claim -> evidence -> source document.
    This is the proof that the system can show its work.
    """
    result = conn.execute("""
        SELECT
            eo.line_item,
            eo.value,
            eo.vs_consensus,
            ea.assumption_key,
            ea.basis,
            c.claim_text,
            c.confidence,
            c.falsifier,
            ei.value AS evidence_text,
            sd.source_name,
            sd.source_locator,
            t.one_liner,
            da.recommendation
        FROM estimate_output eo
        JOIN estimate_case ec ON eo.case_id = ec.case_id
        JOIN estimate_assumption ea ON ea.case_id = ec.case_id
            AND ea.assumption_key = 'ebit_margin_pct'
        JOIN claim_estimate_link cel ON cel.assumption_id = ea.assumption_id
        JOIN claim c ON cel.claim_id = c.claim_id
        JOIN claim_evidence_link cev ON cev.claim_id = c.claim_id
        JOIN evidence_item ei ON cev.evidence_id = ei.evidence_id
            AND ei.evidence_type = 'REPORTED_ACTUAL'
        JOIN source_document sd ON ei.document_id = sd.document_id
        JOIN thesis t ON c.thesis_id = t.thesis_id
        LEFT JOIN decision_assessment da ON da.thesis_id = t.thesis_id
        WHERE eo.line_item = 'ebit_margin'
        LIMIT 1
    """).fetchone()

    assert result is not None, "Full lineage chain should be traceable"

    print(f"\n  ═══ FULL LINEAGE TRACE ═══")
    print(f"  Estimate: {result[0]} = {result[1]}% (+{result[2]}% vs consensus)")
    print(f"  Assumption: {result[3]} | {result[4][:60]}...")
    print(f"  Claim: {result[5][:70]}...")
    print(f"  Confidence: {result[6]}")
    print(f"  Falsifier: {result[7][:60]}...")
    print(f"  Evidence: {result[8][:60]}...")
    print(f"  Source: {result[9]} at {result[10]}")
    print(f"  Thesis: {result[11][:60]}...")
    print(f"  Decision: {result[12]}")
    return True


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

def run_all():
    conn = init_db(Path(":memory:"))
    ids = setup_cmg(conn)

    tests = [
        # Task 1
        ("T1: Gate allows authorized work", test_t1_plan_gate_allows_authorized_work),
        ("T1: Gate blocks unauthorized work", test_t1_plan_gate_blocks_unauthorized),
        ("T1: Gate allows after amendment", test_t1_plan_gate_allows_after_amendment),
        ("T1: Rejects amendment without justification", test_t1_plan_gate_rejects_amendment_without_justification),
        # Task 2
        ("T2: Actuals + guidance + consensus", test_t2_actuals_guidance_consensus),
        ("T2: Independent estimate", test_t2_independent_estimate),
        ("T2: Estimate revision", test_t2_estimate_revision),
        # Task 3
        ("T3: Evidence -> claim -> estimate chain", test_t3_evidence_claim_estimate_chain),
        # Task 4
        ("T4: Decision gate (with work)", test_t4_decision_gate_with_work),
        ("T4: Decision gate (weak idea)", test_t4_decision_gate_blocks_weak_idea),
        # Task 5
        ("T5: Insider blocked without workstream", test_t5_insider_blocked_without_workstream),
        ("T5: Insider overlay creates evidence", test_t5_insider_overlay_creates_evidence),
        ("T5: Insider evidence usable as claim support", test_t5_insider_evidence_usable_as_claim_support),
        # Full lineage
        ("FULL: End-to-end lineage trace", test_full_lineage_trace),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            print(f"\n[TEST] {name}")
            fn(conn, ids)
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("ALL TESTS PASSED")
    conn.close()
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
