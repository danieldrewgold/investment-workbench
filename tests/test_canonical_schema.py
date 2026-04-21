"""
Test suite for the canonical schema, provenance model, and core operations.

Covers:
1. Schema creation (38 tables)
2. Run tracking (success/failure)
3. Company upsert idempotency
4. Evidence lineage tracing (claim → evidence → source_document)
5. Source document dedup via content hash
6. Consensus snapshot versioning
7. FK enforcement
8. Research plan structure (plan → questions → drivers → workstreams → kills)
9. Estimate architecture (case → assumptions → drivers → outputs)
10. Rerun idempotency (same data twice = no corruption)
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.schemas.canonical_schema import CANONICAL_SCHEMA, TABLE_INDEX, TOTAL_TABLES
from core.schemas.idempotency_rules import RULES
from core.provenance.database import (
    RunContext, init_db, new_id, now_iso, hash_content, upsert,
)


def get_db():
    """Fresh in-memory database for each test."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(CANONICAL_SCHEMA)
    return conn


# ── Test 1: Schema creation ──────────────────────────────────────

def test_schema_creates_tables():
    db = get_db()
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    assert len(tables) == TOTAL_TABLES, f"Expected {TOTAL_TABLES}, got {len(tables)}: {tables}"
    for section, names in TABLE_INDEX.items():
        for name in names:
            assert name in tables, f"Missing table: {name} (section: {section})"
    print("PASS: 38 tables created")
    db.close()


# ── Test 2: Run tracking ─────────────────────────────────────────

def test_run_tracking_success():
    db = get_db()
    with RunContext(db, "ingest", {"ticker": "AAPL"}) as run:
        rid = run.run_id

    row = db.execute("SELECT status, parameters FROM run WHERE run_id = ?", (rid,)).fetchone()
    assert row[0] == "success"
    assert "AAPL" in row[1]
    print("PASS: Run tracking (success)")
    db.close()


def test_run_tracking_failure():
    db = get_db()
    try:
        with RunContext(db, "ingest", {"ticker": "FAIL"}) as run:
            rid = run.run_id
            raise ValueError("test error")
    except ValueError:
        pass

    row = db.execute("SELECT status, error_message FROM run WHERE run_id = ?", (rid,)).fetchone()
    assert row[0] == "failed"
    assert "test error" in row[1]
    print("PASS: Run tracking (failure)")
    db.close()


# ── Test 3: Company upsert idempotency ───────────────────────────

def test_company_upsert():
    db = get_db()
    with RunContext(db, "ingest") as run:
        cid = new_id()
        # First insert
        upsert(db, "company", {
            "company_id": cid,
            "name": "Apple Inc",
            "ticker": "AAPL",
            "cik": "0000320193",
            "market_cap": 3_000_000_000_000,
            "updated_by_run": run.run_id,
        }, conflict_columns=["cik"], update_columns=["name", "market_cap", "updated_by_run"])
        db.commit()

        # Second insert with updated market cap (same CIK)
        upsert(db, "company", {
            "company_id": new_id(),  # different ID, same CIK
            "name": "Apple Inc.",
            "ticker": "AAPL",
            "cik": "0000320193",
            "market_cap": 3_100_000_000_000,
            "updated_by_run": run.run_id,
        }, conflict_columns=["cik"], update_columns=["name", "market_cap", "updated_by_run"])
        db.commit()

    # Should still be one company
    count = db.execute("SELECT COUNT(*) FROM company").fetchone()[0]
    assert count == 1, f"Expected 1 company, got {count}"

    # Market cap should be updated
    mcap = db.execute("SELECT market_cap FROM company WHERE cik = '0000320193'").fetchone()[0]
    assert mcap == 3_100_000_000_000
    print("PASS: Company upsert idempotency")
    db.close()


# ── Test 4: Evidence lineage tracing ─────────────────────────────

def test_evidence_lineage():
    """Trace: claim → evidence → source_document. The 'show your work' chain."""
    db = get_db()
    with RunContext(db, "ingest") as run:
        # Create company
        cid = new_id()
        db.execute("INSERT INTO company (company_id, name, ticker) VALUES (?, ?, ?)",
                   (cid, "Chipotle", "CMG"))

        # Create source document (a 10-K filing)
        doc_id = new_id()
        db.execute(
            """INSERT INTO source_document
               (document_id, source_type, source_name, source_locator, fetched_at,
                content_hash, company_id, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, "sec_filing", "SEC EDGAR", "https://sec.gov/cgi-bin/browse-edgar?CIK=CMG&type=10-K",
             now_iso(), hash_content("10-K content"), cid, run.run_id),
        )

        # Create evidence item extracted from the filing
        evi_id = new_id()
        db.execute(
            """INSERT INTO evidence_item
               (evidence_id, document_id, company_id, evidence_type, evidence_key,
                value, value_numeric, as_of_date, extraction_method, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (evi_id, doc_id, cid, "metric", "food_cost_pct_fy25",
             "29.5%", 29.5, "2025-12-31", "manual", run.run_id),
        )

        # Create claim supported by the evidence
        claim_id = new_id()
        db.execute(
            """INSERT INTO claim
               (claim_id, company_id, claim_text, claim_type, affects,
                confidence, falsifier, created_by_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim_id, cid,
             "CMG food costs have stabilized below 30% after the protein inflation of 2023-2024",
             "analytical", "estimate",
             0.8, "Food cost rising above 31% in Q1 2026", run.run_id),
        )

        # Link claim to evidence
        db.execute(
            """INSERT INTO claim_evidence_link
               (claim_id, evidence_id, role, importance, rationale)
               VALUES (?, ?, ?, ?, ?)""",
            (claim_id, evi_id, "supporting", 0.9,
             "FY25 10-K shows food cost at 29.5%, down from 30.8% in FY24"),
        )
        db.commit()

    # Now trace the full chain: claim → evidence → source
    chain = db.execute("""
        SELECT
            c.claim_text,
            e.evidence_key,
            e.value,
            s.source_type,
            s.source_locator,
            cel.role,
            cel.rationale
        FROM claim c
        JOIN claim_evidence_link cel ON c.claim_id = cel.claim_id
        JOIN evidence_item e ON cel.evidence_id = e.evidence_id
        JOIN source_document s ON e.document_id = s.document_id
        WHERE c.claim_id = ?
    """, (claim_id,)).fetchone()

    assert chain is not None, "Lineage query returned nothing"
    assert "food costs" in chain[0].lower()
    assert chain[1] == "food_cost_pct_fy25"
    assert chain[2] == "29.5%"
    assert chain[3] == "sec_filing"
    assert chain[5] == "supporting"
    print("PASS: Evidence lineage tracing (claim → evidence → source)")
    db.close()


# ── Test 5: Source document dedup ────────────────────────────────

def test_source_document_dedup():
    db = get_db()
    with RunContext(db, "ingest") as run:
        content = "Filing content v1"
        doc_data = {
            "document_id": new_id(),
            "source_type": "sec_filing",
            "source_name": "EDGAR",
            "source_locator": "https://sec.gov/filing/123",
            "fetched_at": now_iso(),
            "content_hash": hash_content(content),
            "run_id": run.run_id,
        }
        upsert(db, "source_document", doc_data,
               conflict_columns=["source_type", "source_locator"],
               update_columns=["content_hash", "fetched_at", "run_id"])
        db.commit()

        # Re-fetch same document with different content
        doc_data2 = dict(doc_data)
        doc_data2["document_id"] = new_id()
        doc_data2["content_hash"] = hash_content("Filing content v2")
        doc_data2["fetched_at"] = now_iso()
        upsert(db, "source_document", doc_data2,
               conflict_columns=["source_type", "source_locator"],
               update_columns=["content_hash", "fetched_at", "run_id"])
        db.commit()

    count = db.execute("SELECT COUNT(*) FROM source_document").fetchone()[0]
    assert count == 1, f"Expected 1 document, got {count}"
    h = db.execute("SELECT content_hash FROM source_document").fetchone()[0]
    assert h == hash_content("Filing content v2"), "Content hash not updated"
    print("PASS: Source document dedup")
    db.close()


# ── Test 6: Consensus snapshot versioning ────────────────────────

def test_consensus_versioning():
    db = get_db()
    with RunContext(db, "ingest") as run:
        cid = new_id()
        db.execute("INSERT INTO company (company_id, name, ticker) VALUES (?, ?, ?)",
                   (cid, "Apple", "AAPL"))
        mid = new_id()
        db.execute("INSERT INTO metric_definition (metric_id, metric_name, metric_source) VALUES (?, ?, ?)",
                   (mid, "eps", "consensus"))
        pid = new_id()
        db.execute(
            "INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year, fiscal_quarter) VALUES (?, ?, ?, ?, ?)",
            (pid, cid, "FQ", 2026, 2))

        # Snapshot as of March 15
        upsert(db, "consensus_snapshot", {
            "snapshot_id": new_id(),
            "company_id": cid, "metric_id": mid, "period_id": pid,
            "as_of_date": "2026-03-15", "source_name": "yahoo",
            "estimate_mean": 1.85, "num_analysts": 32, "run_id": run.run_id,
        }, conflict_columns=["company_id", "metric_id", "period_id", "as_of_date", "source_name"],
           update_columns=["estimate_mean", "num_analysts", "run_id"])

        # Snapshot as of March 20 (different point in time)
        upsert(db, "consensus_snapshot", {
            "snapshot_id": new_id(),
            "company_id": cid, "metric_id": mid, "period_id": pid,
            "as_of_date": "2026-03-20", "source_name": "yahoo",
            "estimate_mean": 1.88, "num_analysts": 33, "run_id": run.run_id,
        }, conflict_columns=["company_id", "metric_id", "period_id", "as_of_date", "source_name"],
           update_columns=["estimate_mean", "num_analysts", "run_id"])

        db.commit()

    # Should have 2 snapshots (different as_of_date)
    count = db.execute("SELECT COUNT(*) FROM consensus_snapshot").fetchone()[0]
    assert count == 2, f"Expected 2 snapshots, got {count}"

    # Can track consensus revision over time
    rows = db.execute(
        "SELECT as_of_date, estimate_mean FROM consensus_snapshot ORDER BY as_of_date"
    ).fetchall()
    assert rows[0][1] == 1.85 and rows[1][1] == 1.88
    print("PASS: Consensus snapshot versioning")
    db.close()


# ── Test 7: FK enforcement ───────────────────────────────────────

def test_fk_enforcement():
    db = get_db()
    errors = 0

    # Security → company FK
    try:
        db.execute("INSERT INTO security (security_id, company_id, ticker, security_type) VALUES ('s1', 'nonexistent', 'X', 'common')")
        print("FAIL: security → company FK not enforced")
    except sqlite3.IntegrityError:
        errors += 1

    # Evidence → source_document FK
    try:
        db.execute("""INSERT INTO evidence_item
            (evidence_id, document_id, evidence_type, evidence_key, run_id)
            VALUES ('e1', 'nonexistent', 'metric', 'test', 'r1')""")
        print("FAIL: evidence → document FK not enforced")
    except sqlite3.IntegrityError:
        errors += 1

    # Claim_evidence_link → claim FK
    try:
        db.execute("INSERT INTO claim_evidence_link (claim_id, evidence_id, role) VALUES ('c1', 'e1', 'supporting')")
        print("FAIL: claim_evidence_link → claim FK not enforced")
    except sqlite3.IntegrityError:
        errors += 1

    assert errors == 3, f"Expected 3 FK violations, got {errors}"
    print("PASS: FK enforcement (3/3 violations caught)")
    db.close()


# ── Test 8: Research plan structure ──────────────────────────────

def test_research_plan():
    db = get_db()
    with RunContext(db, "analyze") as run:
        cid = new_id()
        db.execute("INSERT INTO company (company_id, name, ticker) VALUES (?, ?, ?)",
                   (cid, "Chipotle", "CMG"))

        # Create research plan
        plan_id = new_id()
        db.execute(
            """INSERT INTO research_plan
               (plan_id, company_id, plan_version, status, edge_type, edge_hypothesis, why_now, created_by_run)
               VALUES (?, ?, 1, 'active', 'expectation_gap',
                       'Market underestimates margin recovery from food cost normalization',
                       'Q1 2026 should show first clean quarter with normalized avocado/chicken costs',
                       ?)""",
            (plan_id, cid, run.run_id))

        # Questions
        for q in [
            ("What is the run-rate food cost % after protein deflation?", "critical", "estimate"),
            ("Is the new throughput initiative adding labor leverage?", "high", "estimate"),
            ("Does the market already price margin expansion?", "high", "conviction"),
        ]:
            db.execute(
                "INSERT INTO research_question (question_id, plan_id, question_text, priority, affects) VALUES (?, ?, ?, ?, ?)",
                (new_id(), plan_id, q[0], q[1], q[2]))

        # Key drivers
        for d in [
            ("food_cost_pct", "margin", "critical", "Direct: 100bps food cost = ~70bps restaurant margin"),
            ("transactions_per_hour", "revenue", "high", "Volume × ticket = SSS; labor leverage on throughput"),
        ]:
            db.execute(
                "INSERT INTO key_driver (driver_id, plan_id, driver_name, driver_category, importance, transmission) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), plan_id, d[0], d[1], d[2], d[3]))

        # Workstreams
        db.execute(
            "INSERT INTO workstream (workstream_id, plan_id, workstream_name, analysis_type, justification) VALUES (?, ?, ?, ?, ?)",
            (new_id(), plan_id, "margin_bridge", "margin_bridge",
             "Decompose FY25→FY26 margin walk to isolate food cost vs labor vs other"))

        # Kill conditions
        db.execute(
            "INSERT INTO kill_condition (kill_id, plan_id, condition_text) VALUES (?, ?, ?)",
            (new_id(), plan_id, "Food cost above 31% in Q1 2026 10-Q"))

        db.commit()

    # Verify full structure
    q_count = db.execute("SELECT COUNT(*) FROM research_question WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    d_count = db.execute("SELECT COUNT(*) FROM key_driver WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    w_count = db.execute("SELECT COUNT(*) FROM workstream WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    k_count = db.execute("SELECT COUNT(*) FROM kill_condition WHERE plan_id = ?", (plan_id,)).fetchone()[0]

    assert q_count == 3 and d_count == 2 and w_count == 1 and k_count == 1
    print(f"PASS: Research plan structure (3Q, 2D, 1W, 1K)")
    db.close()


# ── Test 9: Estimate architecture ────────────────────────────────

def test_estimate_architecture():
    db = get_db()
    with RunContext(db, "derive") as run:
        cid = new_id()
        db.execute("INSERT INTO company (company_id, name, ticker) VALUES (?, ?, ?)",
                   (cid, "Chipotle", "CMG"))

        # Plan and driver
        plan_id = new_id()
        db.execute("INSERT INTO research_plan (plan_id, company_id, plan_version, created_by_run) VALUES (?, ?, 1, ?)",
                   (plan_id, cid, run.run_id))
        driver_id = new_id()
        db.execute("INSERT INTO key_driver (driver_id, plan_id, driver_name, driver_category) VALUES (?, ?, ?, ?)",
                   (driver_id, plan_id, "food_cost_pct", "margin"))

        # Reporting periods
        periods = {}
        for yr, qtr in [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]:
            pid = new_id()
            db.execute("INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year, fiscal_quarter) VALUES (?, ?, ?, ?, ?)",
                       (pid, cid, "FQ", yr, qtr))
            periods[(yr, qtr)] = pid

        # Base case estimate
        case_id = new_id()
        db.execute(
            """INSERT INTO estimate_case
               (case_id, company_id, plan_id, case_name, case_version,
                scenario_weight, summary, target_price, target_method, created_by_run)
               VALUES (?, ?, ?, 'base', 1, 0.60,
                       'Food cost normalizes to 29%, restaurant margin expands 150bps YoY',
                       72.00, 'multiple', ?)""",
            (case_id, cid, plan_id, run.run_id))

        # Assumptions
        evi_id = new_id()
        db.execute(
            """INSERT INTO source_document (document_id, source_type, source_name, source_locator, fetched_at, run_id)
               VALUES (?, 'manual', 'analyst', 'manual_entry', ?, ?)""",
            (new_id(), now_iso(), run.run_id))
        # (evidence not linked for brevity)

        for key, val, basis in [
            ("food_cost_pct_fy26", 29.0, "FY25 actuals + protein deflation trend"),
            ("sss_growth_fy26", 5.5, "Transaction growth + modest pricing"),
            ("restaurant_margin_fy26", 28.5, "Food cost + labor leverage from throughput"),
        ]:
            db.execute(
                """INSERT INTO estimate_assumption
                   (assumption_id, case_id, assumption_key, assumption_value, basis, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (new_id(), case_id, key, val, basis, 0.7))

        # Driver link
        db.execute(
            "INSERT INTO estimate_driver (case_id, driver_id, driver_value, driver_impact, sensitivity) VALUES (?, ?, ?, ?, ?)",
            (case_id, driver_id, 29.0, "100bps food cost = ~70bps restaurant margin", "+-50bps = $0.80/share"))

        # Quarterly outputs
        for qtr, eps in [(1, 14.50), (2, 17.20), (3, 18.80), (4, 15.00)]:
            pid = periods[(2026, qtr)]
            db.execute(
                "INSERT INTO estimate_output (output_id, case_id, period_id, line_item, value) VALUES (?, ?, ?, ?, ?)",
                (new_id(), case_id, pid, "eps", eps))

        db.commit()

    # Verify
    assumptions = db.execute("SELECT COUNT(*) FROM estimate_assumption WHERE case_id = ?", (case_id,)).fetchone()[0]
    outputs = db.execute("SELECT COUNT(*) FROM estimate_output WHERE case_id = ?", (case_id,)).fetchone()[0]
    drivers = db.execute("SELECT COUNT(*) FROM estimate_driver WHERE case_id = ?", (case_id,)).fetchone()[0]

    assert assumptions == 3 and outputs == 4 and drivers == 1
    total_eps = db.execute("SELECT SUM(value) FROM estimate_output WHERE case_id = ? AND line_item = 'eps'", (case_id,)).fetchone()[0]
    assert total_eps == 65.50, f"Expected FY EPS 65.50, got {total_eps}"
    print(f"PASS: Estimate architecture (3 assumptions, 4 quarterly outputs, 1 driver)")
    db.close()


# ── Test 10: Rerun idempotency ───────────────────────────────────

def test_rerun_idempotency():
    """Run the same ingestion twice. Should not duplicate data."""
    db = get_db()

    def ingest_once(db):
        with RunContext(db, "ingest") as run:
            upsert(db, "company", {
                "company_id": new_id(), "name": "Meta", "ticker": "META",
                "cik": "0001326801", "market_cap": 1_500_000_000_000,
                "updated_by_run": run.run_id,
            }, conflict_columns=["cik"], update_columns=["name", "market_cap", "updated_by_run"])

            doc_id = new_id()
            upsert(db, "source_document", {
                "document_id": doc_id,
                "source_type": "api_response", "source_name": "Polygon",
                "source_locator": "polygon/v3/snapshot/options/META",
                "fetched_at": now_iso(),
                "content_hash": hash_content("snapshot_data"),
                "run_id": run.run_id,
            }, conflict_columns=["source_type", "source_locator"],
               update_columns=["content_hash", "fetched_at", "run_id"])

            db.commit()

    ingest_once(db)
    ingest_once(db)

    companies = db.execute("SELECT COUNT(*) FROM company").fetchone()[0]
    documents = db.execute("SELECT COUNT(*) FROM source_document").fetchone()[0]
    runs = db.execute("SELECT COUNT(*) FROM run").fetchone()[0]

    assert companies == 1, f"Expected 1 company, got {companies}"
    assert documents == 1, f"Expected 1 document, got {documents}"
    assert runs == 2, f"Expected 2 runs (each ingest is a separate run), got {runs}"
    print("PASS: Rerun idempotency (2 runs, no data duplication)")
    db.close()


# ── Test 11: Idempotency rules completeness ──────────────────────

def test_rules_cover_all_tables():
    from core.schemas.canonical_schema import TABLE_INDEX
    all_tables = set()
    for names in TABLE_INDEX.values():
        all_tables.update(names)
    ruled_tables = set(RULES.keys())
    missing = all_tables - ruled_tables
    extra = ruled_tables - all_tables
    assert not missing, f"Tables without idempotency rules: {missing}"
    assert not extra, f"Rules for non-existent tables: {extra}"
    print("PASS: Idempotency rules cover all 38 tables")


# ── Run all tests ────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_schema_creates_tables,
        test_run_tracking_success,
        test_run_tracking_failure,
        test_company_upsert,
        test_evidence_lineage,
        test_source_document_dedup,
        test_consensus_versioning,
        test_fk_enforcement,
        test_research_plan,
        test_estimate_architecture,
        test_rerun_idempotency,
        test_rules_cover_all_tables,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL: {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed:
        sys.exit(1)
