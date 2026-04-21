"""
FINRA Ingestion Loader

Fetches short interest and dark pool (ATS) volume from FINRA APIs.
Free, no API key required.

RAW: API responses stored as source_documents
NORMALIZED: short interest as evidence_items, dark pool as evidence_items
"""

import httpx
from datetime import datetime
from core.provenance.database import new_id, upsert


class FinraLoader:

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

    async def close(self):
        await self.client.aclose()

    # ── Short interest ────────────────────────────────────────

    async def ingest_short_interest(self, company_id: str, ticker: str) -> dict:
        """
        Fetch short interest from FINRA consolidated short interest.
        Returns parsed data dict.
        """
        url = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
        payload = {
            "fields": [
                "settlementDate", "issueName",
                "currentShortPositionQuantity", "previousShortPositionQuantity",
                "changePercent", "averageDailyVolumeQuantity", "daysToCoverQuantity",
            ],
            "compareFilters": [{
                "fieldName": "symbolCode",
                "fieldValue": ticker.upper(),
                "compareType": "EQUAL",
            }],
            "limit": 5,
            "sortFields": ["-settlementDate"],
        }

        try:
            resp = await self.client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                return {}
        except Exception:
            return {}

        now = datetime.utcnow().isoformat() + "Z"
        latest = data[0]

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "API_RESPONSE",
            "source_name": "FINRA",
            "source_locator": f"finra:short_interest:{ticker.upper()}:{latest.get('settlementDate','')}",
            "source_published_at": latest.get("settlementDate"),
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Evidence item
        si = latest.get("currentShortPositionQuantity", 0)
        dtc = latest.get("daysToCoverQuantity", 0)
        change = latest.get("changePercent", 0)
        settle = latest.get("settlementDate", "")

        evidence_value = (
            f"SI: {si:,.0f} shares | DTC: {dtc:.1f} | "
            f"Change: {change:+.1f}% | As of: {settle}"
        )

        upsert(self.conn, "evidence_item", {
            "evidence_id": new_id(),
            "document_id": doc_id,
            "company_id": company_id,
            "evidence_type": "SHORT_INTEREST",
            "evidence_key": f"si_{settle}",
            "value": evidence_value,
            "value_numeric": si,
            "as_of_date": settle,
            "run_id": self.run_id,
        }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

        self.conn.commit()

        result = {
            "short_interest": si,
            "prior_short_interest": latest.get("previousShortPositionQuantity", 0),
            "change_pct": change,
            "days_to_cover": dtc,
            "avg_daily_volume": latest.get("averageDailyVolumeQuantity", 0),
            "settlement_date": settle,
            "source_document_id": doc_id,
        }
        return result

    # ── Dark pool (ATS) volume ────────────────────────────────

    async def ingest_dark_pool_volume(
        self, company_id: str, ticker: str, weeks_back: int = 4,
    ) -> list[dict]:
        """
        Fetch weekly ATS volume from FINRA.
        Returns list of weekly records.
        """
        url = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
        payload = {
            "fields": [
                "weekStartDate", "totalWeeklyShareQuantity", "totalWeeklyTradeCount",
            ],
            "compareFilters": [{
                "fieldName": "issueSymbolIdentifier",
                "fieldValue": ticker.upper(),
                "compareType": "EQUAL",
            }],
            "limit": weeks_back * 10,
            "sortFields": ["-totalWeeklyShareQuantity"],
        }

        try:
            resp = await self.client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                return []
        except Exception:
            return []

        now = datetime.utcnow().isoformat() + "Z"

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "API_RESPONSE",
            "source_name": "FINRA",
            "source_locator": f"finra:dark_pool:{ticker.upper()}:{datetime.now().strftime('%Y-%m-%d')}",
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Store as evidence items (one per week)
        results = []
        for week in data[:weeks_back]:
            week_start = week.get("weekStartDate", "")
            shares = week.get("totalWeeklyShareQuantity", 0)
            trades = week.get("totalWeeklyTradeCount", 0)

            if not week_start:
                continue

            evidence_value = f"ATS volume: {shares:,.0f} shares / {trades:,.0f} trades (week of {week_start})"

            upsert(self.conn, "evidence_item", {
                "evidence_id": new_id(),
                "document_id": doc_id,
                "company_id": company_id,
                "evidence_type": "DARK_POOL_VOLUME",
                "evidence_key": f"ats_{week_start}",
                "value": evidence_value,
                "value_numeric": shares,
                "as_of_date": week_start,
                "run_id": self.run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

            results.append({
                "week_start": week_start,
                "shares": shares,
                "trades": trades,
            })

        self.conn.commit()
        return results
