"""Unit tests for EDGAR loader. Tests parsing without live API calls."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance.database import init_db, new_id, create_run, upsert, hash_content
from ingestion.loaders.edgar_loader import EdgarLoader
from tests.fixtures.sample_data import FORM4_XML_PURCHASE, FORM4_XML_SALE


def test_form4_purchase_parsing():
    result = EdgarLoader.parse_form4_xml(FORM4_XML_PURCHASE)
    assert result is not None
    assert result["insider_name"] == "NICCOL SCOTT"
    assert result["insider_title"] == "Chairman and CEO"
    assert result["transaction_code"] == "P"
    assert result["transaction_type"] == "PURCHASE"
    assert result["shares"] == 5000
    assert result["price"] == 58.25
    assert result["value"] == 5000 * 58.25
    assert result["transaction_date"] == "2026-03-10"
    print(f"  {result['insider_name']} ({result['insider_title']}): "
          f"{result['transaction_type']} {result['shares']:,} @ ${result['price']}")


def test_form4_sale_parsing():
    result = EdgarLoader.parse_form4_xml(FORM4_XML_SALE)
    assert result is not None
    assert result["insider_name"] == "JONES SARAH"
    assert result["insider_title"] == "VP Finance"
    assert result["transaction_code"] == "S"
    assert result["transaction_type"] == "SALE"
    assert result["shares"] == 2000
    assert result["price"] == 61.50
    print(f"  {result['insider_name']} ({result['insider_title']}): "
          f"{result['transaction_type']} {result['shares']:,} @ ${result['price']}")


def test_insider_tx_normalization_with_provenance():
    """Parsed Form 4 data normalizes into insider_transaction + evidence_item with full provenance."""
    conn = init_db(Path(":memory:"))

    with create_run(conn, "ingest", {"ticker": "CMG"}) as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker, cik) VALUES (?,?,?,?)",
                     (cid, "Chipotle", "CMG", "0001058090"))

        parsed = EdgarLoader.parse_form4_xml(FORM4_XML_PURCHASE)

        # Source document
        did = new_id()
        upsert(conn, "source_document", {
            "document_id": did,
            "source_type": "FILING",
            "source_name": "SEC_EDGAR",
            "source_locator": "https://sec.gov/Archives/edgar/data/1058090/form4.xml",
            "source_published_at": "2026-03-10",
            "fetched_at": "2026-03-27T10:00:00Z",
            "company_id": cid,
            "run_id": run.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Evidence item
        ev_key = f"{parsed['insider_name']}__{parsed['transaction_code']}__{parsed['shares']}"
        ev_value = f"{parsed['insider_name']} {parsed['transaction_type']} {parsed['shares']} shares at ${parsed['price']}"
        upsert(conn, "evidence_item", {
            "evidence_id": new_id(),
            "document_id": did,
            "company_id": cid,
            "evidence_type": "INSIDER_TX",
            "evidence_key": ev_key,
            "value": ev_value,
            "as_of_date": parsed["transaction_date"],
            "extraction_method": "FORM4_XML_PARSER",
            "run_id": run.run_id,
        }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

        # Insider transaction
        upsert(conn, "insider_transaction", {
            "transaction_id": new_id(),
            "company_id": cid,
            "insider_name": parsed["insider_name"],
            "insider_title": parsed["insider_title"],
            "transaction_date": parsed["transaction_date"],
            "transaction_code": parsed["transaction_code"],
            "transaction_type": parsed["transaction_type"],
            "shares": parsed["shares"],
            "price": parsed["price"],
            "value": parsed["value"],
            "source_document_id": did,
            "run_id": run.run_id,
        }, conflict_columns=["company_id", "insider_name", "transaction_date", "transaction_code", "shares"],
        update_columns=["insider_title", "transaction_type", "price", "value", "source_document_id", "run_id"])

        conn.commit()

    # Verify provenance chain
    tx = conn.execute("""
        SELECT it.insider_name, it.transaction_type, it.shares, it.price,
               sd.source_name, sd.source_locator,
               r.run_type, r.status
        FROM insider_transaction it
        JOIN source_document sd ON it.source_document_id = sd.document_id
        JOIN run r ON it.run_id = r.run_id
        WHERE it.company_id = ?
    """, (cid,)).fetchone()

    assert tx is not None
    assert tx[0] == "NICCOL SCOTT"
    assert tx[1] == "PURCHASE"
    assert tx[4] == "SEC_EDGAR"
    assert tx[7] == "success"

    ev = conn.execute("""
        SELECT ei.value, ei.extraction_method
        FROM evidence_item ei
        WHERE ei.document_id = ? AND ei.evidence_type = 'INSIDER_TX'
    """, (did,)).fetchone()

    assert ev is not None
    assert "PURCHASE" in ev[0]
    assert ev[1] == "FORM4_XML_PARSER"

    print(f"  Normalized: {tx[0]} {tx[1]} {tx[2]:,.0f} shares @ ${tx[3]}")
    print(f"  Source: {tx[4]} ({tx[7]})")
    print(f"  Evidence: {ev[0][:60]}...")
    print(f"  Provenance chain intact")

    conn.close()


def run_all():
    tests = [
        ("Form 4 purchase parsing", test_form4_purchase_parsing),
        ("Form 4 sale parsing", test_form4_sale_parsing),
        ("Insider tx normalization + provenance", test_insider_tx_normalization_with_provenance),
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
