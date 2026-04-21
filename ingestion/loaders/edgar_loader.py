"""
EDGAR Ingestion Loader

Fetches SEC filings and insider transactions, stores with provenance,
normalizes into canonical objects.

RAW: Filing metadata stored as source_documents
NORMALIZED: Insider transactions, evidence items
"""

import re
import httpx
from datetime import datetime, timedelta
from core.provenance.database import new_id, upsert, hash_content

SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}


class EdgarLoader:

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self.client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=30.0, follow_redirects=True)
        self._cik_cache = {}

    async def close(self):
        await self.client.aclose()

    async def resolve_cik(self, ticker: str) -> str | None:
        ticker = ticker.upper()
        if ticker in self._cik_cache:
            return self._cik_cache[ticker]
        try:
            resp = await self.client.get("https://www.sec.gov/files/company_tickers.json")
            if resp.status_code == 200:
                for entry in resp.json().values():
                    if entry.get("ticker", "").upper() == ticker:
                        cik = str(entry["cik_str"]).zfill(10)
                        self._cik_cache[ticker] = cik
                        return cik
        except Exception:
            pass
        return None

    async def ingest_filings(
        self, company_id: str, ticker: str, cik: str,
        form_types: list[str] = None, days_back: int = 365,
    ) -> list[str]:
        """Fetch filings from EDGAR, store as source_documents. Returns document_ids."""
        if not cik:
            cik = await self.resolve_cik(ticker)
            if not cik:
                return []

        cik_stripped = cik.lstrip("0")
        try:
            resp = await self.client.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        doc_ids = []

        for i, form in enumerate(forms):
            if i >= len(dates) or i >= len(accessions):
                break
            filed = dates[i]
            if filed < cutoff:
                continue
            if form_types and form.upper() not in [f.upper() for f in form_types]:
                continue

            accession = accessions[i]
            primary = primary_docs[i] if i < len(primary_docs) else ""
            locator = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession.replace('-','')}/{primary}"

            doc_id = new_id()
            upsert(self.conn, "source_document", {
                "document_id": doc_id,
                "source_type": "FILING",
                "source_name": "SEC_EDGAR",
                "source_locator": locator,
                "source_published_at": filed,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "company_id": company_id,
                "run_id": self.run_id,
            }, conflict_columns=["source_type", "source_locator"],
            update_columns=["fetched_at", "run_id"])
            doc_ids.append(doc_id)

        self.conn.commit()
        return doc_ids

    async def ingest_insider_transactions(
        self, company_id: str, ticker: str, cik: str, days_back: int = 180,
    ) -> list[str]:
        """Fetch Form 4s, parse XML, store as source_documents + insider_transactions."""
        doc_ids = await self.ingest_filings(
            company_id, ticker, cik, form_types=["4", "4/A"], days_back=days_back,
        )

        tx_ids = []
        for doc_id in doc_ids:
            row = self.conn.execute(
                "SELECT source_locator, source_published_at FROM source_document WHERE document_id=?",
                (doc_id,)
            ).fetchone()
            if not row:
                continue

            xml_data = await self._fetch_form4_xml(row[0])
            if not xml_data:
                continue

            filing_date = row[1]

            # Evidence item
            ev_key = f"{xml_data.get('insider_name','')}__{xml_data.get('transaction_code','')}__{xml_data.get('shares','')}"
            ev_value = (f"{xml_data.get('insider_name','')} {xml_data.get('transaction_type','')} "
                       f"{xml_data.get('shares','')} shares at ${xml_data.get('price','')}")
            upsert(self.conn, "evidence_item", {
                "evidence_id": new_id(),
                "document_id": doc_id,
                "company_id": company_id,
                "evidence_type": "INSIDER_TX",
                "evidence_key": ev_key,
                "value": ev_value,
                "as_of_date": xml_data.get("transaction_date", filing_date),
                "extraction_method": "FORM4_XML_PARSER",
                "run_id": self.run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

            # Insider transaction (normalized)
            tx_id = new_id()
            shares = xml_data.get("shares", 0)
            upsert(self.conn, "insider_transaction", {
                "transaction_id": tx_id,
                "company_id": company_id,
                "insider_name": xml_data.get("insider_name", "UNKNOWN"),
                "insider_title": xml_data.get("insider_title"),
                "transaction_date": xml_data.get("transaction_date", filing_date),
                "transaction_code": xml_data.get("transaction_code", "?"),
                "transaction_type": xml_data.get("transaction_type"),
                "shares": shares,
                "price": xml_data.get("price"),
                "value": xml_data.get("value"),
                "source_document_id": doc_id,
                "run_id": self.run_id,
            }, conflict_columns=[
                "company_id", "insider_name", "transaction_date", "transaction_code", "shares",
            ], update_columns=[
                "insider_title", "transaction_type", "price", "value",
                "source_document_id", "run_id",
            ])
            tx_ids.append(tx_id)

        self.conn.commit()
        return tx_ids

    async def _fetch_form4_xml(self, filing_url: str) -> dict | None:
        try:
            parts = filing_url.rsplit("/", 1)
            if len(parts) < 2:
                return None
            resp = await self.client.get(parts[0] + "/")
            if resp.status_code != 200:
                return None
            xml_matches = re.findall(r'href="([^"]+\.xml)"', resp.text)
            if not xml_matches:
                return None
            xml_url = xml_matches[0]
            if not xml_url.startswith("http"):
                xml_url = parts[0] + "/" + xml_url
            resp = await self.client.get(xml_url)
            if resp.status_code != 200:
                return None
            return self.parse_form4_xml(resp.text)
        except Exception:
            return None

    @staticmethod
    def parse_form4_xml(xml_text: str) -> dict | None:
        """Extract key fields from Form 4 XML. Static for testability."""
        result = {}
        m = re.search(r'<rptOwnerName>([^<]+)</rptOwnerName>', xml_text)
        if m:
            result["insider_name"] = m.group(1).strip()

        m = re.search(r'<officerTitle>([^<]+)</officerTitle>', xml_text)
        if m:
            result["insider_title"] = m.group(1).strip()
        else:
            m = re.search(r'<isDirector>(\d|true)</isDirector>', xml_text, re.I)
            if m and m.group(1) in ("1", "true"):
                result["insider_title"] = "Director"

        m = re.search(r'<transactionCode>([A-Z])</transactionCode>', xml_text)
        if m:
            code = m.group(1)
            result["transaction_code"] = code
            result["transaction_type"] = {
                "P": "PURCHASE", "S": "SALE", "A": "AWARD",
                "M": "EXERCISE", "F": "TAX_DISPOSITION", "G": "GIFT",
            }.get(code, code)

        m = re.search(r'<transactionDate>\s*<value>([^<]+)</value>', xml_text)
        if m:
            result["transaction_date"] = m.group(1).strip()

        m = re.search(r'<transactionShares>\s*<value>([^<]+)</value>', xml_text)
        if m:
            try: result["shares"] = int(float(m.group(1)))
            except: pass

        m = re.search(r'<transactionPricePerShare>\s*<value>([^<]+)</value>', xml_text)
        if m:
            try: result["price"] = float(m.group(1))
            except: pass

        if result.get("shares") and result.get("price"):
            result["value"] = result["shares"] * result["price"]

        return result if result else None
