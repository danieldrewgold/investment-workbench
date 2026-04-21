"""Unit tests for consensus/guidance loader."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance.database import init_db, new_id, create_run, upsert
from ingestion.loaders.consensus_loader import ingest_guidance_from_dict
from tests.fixtures.sample_data import GUIDANCE_CMG_FY2026


def test_guidance_ingestion():
    """Guidance from fixture creates proper source_document + guidance_point + evidence_item."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "ingest", {"ticker": "CMG"}) as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "Chipotle", "CMG"))
        conn.commit()

        gids = ingest_guidance_from_dict(conn, cid, GUIDANCE_CMG_FY2026, run.run_id)

    assert len(gids) == 3, f"Expected 3 guidance metrics, got {len(gids)}"
    print(f"  Guidance: {len(gids)} metrics ingested")

    # Verify source document
    doc = conn.execute(
        "SELECT source_name, source_locator FROM source_document WHERE company_id=? AND source_name='COMPANY_GUIDANCE'",
        (cid,)
    ).fetchone()
    assert doc is not None
    print(f"  Source: {doc[0]} at {doc[1][:50]}")

    # Verify guidance points
    guidance = conn.execute(
        "SELECT gp.guidance_type, gp.value_low, gp.value_high, gp.value_point, md.metric_name "
        "FROM guidance_point gp "
        "JOIN metric_definition md ON gp.metric_id = md.metric_id "
        "WHERE gp.company_id = ? ORDER BY md.metric_name",
        (cid,)
    ).fetchall()

    assert len(guidance) == 3
    for g in guidance:
        print(f"    {g[4]}: {g[1]}-{g[2]} (mid: {g[3]}) [{g[0]}]")

    # Verify evidence items created
    ev_count = conn.execute(
        "SELECT COUNT(*) FROM evidence_item WHERE company_id=? AND evidence_type='GUIDANCE'",
        (cid,)
    ).fetchone()[0]
    assert ev_count == 3
    print(f"  Evidence items: {ev_count}")

    # Verify reporting period created
    period = conn.execute(
        "SELECT period_type, fiscal_year FROM reporting_period WHERE company_id=?",
        (cid,)
    ).fetchone()
    assert period is not None
    print(f"  Period: {period[0]} {period[1]}")

    conn.close()


def test_guidance_idempotency():
    """Re-ingesting same guidance should not create duplicates."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "ingest") as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker) VALUES (?,?,?)",
                     (cid, "Chipotle", "CMG"))
        conn.commit()

        # Ingest twice
        ingest_guidance_from_dict(conn, cid, GUIDANCE_CMG_FY2026, run.run_id)
        ingest_guidance_from_dict(conn, cid, GUIDANCE_CMG_FY2026, run.run_id)

    gp_count = conn.execute("SELECT COUNT(*) FROM guidance_point WHERE company_id=?", (cid,)).fetchone()[0]
    doc_count = conn.execute("SELECT COUNT(*) FROM source_document WHERE company_id=? AND source_name='COMPANY_GUIDANCE'", (cid,)).fetchone()[0]

    assert gp_count == 3, f"Expected 3 guidance points, got {gp_count}"
    assert doc_count == 1, f"Expected 1 source document, got {doc_count}"
    print(f"  Idempotency: 2 ingestions -> {gp_count} guidance points, {doc_count} source doc")

    conn.close()


def run_all():
    tests = [
        ("Guidance ingestion", test_guidance_ingestion),
        ("Guidance idempotency", test_guidance_idempotency),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            print(f"\n[UNIT] {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
