"""
Polygon Ingestion Loader

Fetches company details, snapshots, and price history from Polygon.io.
Stores with full provenance and normalizes into canonical objects.

RAW: API responses stored as source_documents
NORMALIZED: company updates, price metric series, market data evidence
"""

import os
import httpx
from datetime import datetime, timedelta
from core.provenance.database import new_id, upsert


class PolygonLoader:

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self.api_key = os.getenv("POLYGON_API_KEY", "")
        self.base = "https://api.polygon.io"
        self.available = bool(self.api_key)
        self.client = httpx.AsyncClient(timeout=30.0) if self.available else None

    async def close(self):
        if self.client:
            await self.client.aclose()

    def _params(self, **kwargs):
        p = {"apiKey": self.api_key}
        p.update(kwargs)
        return p

    async def _get(self, url: str, params: dict = None) -> dict:
        if not self.available:
            return {}
        resp = await self.client.get(url, params=params or self._params())
        if resp.status_code == 429:
            import asyncio
            await asyncio.sleep(12)
            resp = await self.client.get(url, params=params or self._params())
        resp.raise_for_status()
        return resp.json()

    # ── Company details ───────────────────────────────────────

    async def ingest_company_details(self, company_id: str, ticker: str) -> str | None:
        """
        Fetch ticker details from Polygon and update company record.
        Also creates a source_document for provenance.
        Returns source_document_id.
        """
        if not self.available:
            return None

        url = f"{self.base}/v3/reference/tickers/{ticker.upper()}"
        try:
            data = await self._get(url, self._params())
            results = data.get("results", {})
            if not results:
                return None
        except Exception:
            return None

        now = datetime.utcnow().isoformat() + "Z"

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "API_RESPONSE",
            "source_name": "POLYGON",
            "source_locator": url,
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Update company with latest data
        update_fields = {}
        if results.get("market_cap"):
            update_fields["market_cap"] = results["market_cap"]
        if results.get("share_class_shares_outstanding"):
            update_fields["shares_outstanding"] = results["share_class_shares_outstanding"]
        if results.get("sic_code"):
            update_fields["sic_code"] = results["sic_code"]
        if results.get("name"):
            update_fields["name"] = results["name"]

        if update_fields:
            update_fields["updated_at"] = now
            update_fields["updated_by_run"] = self.run_id
            sets = ", ".join(f"{k} = ?" for k in update_fields)
            vals = list(update_fields.values()) + [company_id]
            self.conn.execute(f"UPDATE company SET {sets} WHERE company_id = ?", vals)

        self.conn.commit()
        return doc_id

    # ── Price history ─────────────────────────────────────────

    async def ingest_price_history(
        self, company_id: str, ticker: str, days_back: int = 90,
    ) -> int:
        """
        Fetch daily OHLCV bars and store as company_metric_series.
        Returns count of bars stored.
        """
        if not self.available:
            return 0

        ticker = ticker.upper()
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = f"{self.base}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"

        try:
            data = await self._get(url, self._params(adjusted="true", sort="asc", limit=5000))
            bars = data.get("results", [])
            if not bars:
                return 0
        except Exception:
            return 0

        now = datetime.utcnow().isoformat() + "Z"

        # Source document for this price pull
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "API_RESPONSE",
            "source_name": "POLYGON",
            "source_locator": f"polygon:aggs:{ticker}:{from_date}:{to_date}",
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Ensure metric definitions exist
        for metric_name in ["close_price", "volume", "high", "low", "open"]:
            upsert(self.conn, "metric_definition", {
                "metric_id": new_id(),
                "metric_name": metric_name,
                "metric_source": "POLYGON",
                "unit": "USD" if metric_name != "volume" else "SHARES",
                "frequency": "daily",
            }, conflict_columns=["metric_name", "metric_source"])
        self.conn.commit()

        # Get metric IDs
        metric_ids = {}
        for row in self.conn.execute(
            "SELECT metric_id, metric_name FROM metric_definition WHERE metric_source='POLYGON'"
        ).fetchall():
            metric_ids[row[1]] = row[0]

        # Store bars as metric series
        count = 0
        for bar in bars:
            ts = bar.get("t", 0)
            date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else None
            if not date:
                continue

            bar_map = {
                "close_price": bar.get("c"),
                "volume": bar.get("v"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "open": bar.get("o"),
            }

            for metric_name, value in bar_map.items():
                if value is None or metric_name not in metric_ids:
                    continue
                upsert(self.conn, "company_metric_series", {
                    "series_id": new_id(),
                    "company_id": company_id,
                    "metric_id": metric_ids[metric_name],
                    "as_of_date": date,
                    "value": value,
                    "source_document_id": doc_id,
                    "run_id": self.run_id,
                }, conflict_columns=["company_id", "metric_id", "period_id", "source_document_id"],
                update_columns=["value", "as_of_date", "run_id"])
            count += 1

        self.conn.commit()
        return count

    # ── Snapshot (current price) ──────────────────────────────

    async def ingest_snapshot(self, company_id: str, ticker: str) -> dict | None:
        """Fetch current snapshot. Returns parsed data (not stored as series)."""
        if not self.available:
            return None

        ticker = ticker.upper()
        url = f"{self.base}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"

        try:
            data = await self._get(url, self._params())
            snap = data.get("ticker", {})
            if not snap:
                return None
        except Exception:
            return None

        now = datetime.utcnow().isoformat() + "Z"

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "API_RESPONSE",
            "source_name": "POLYGON",
            "source_locator": f"polygon:snapshot:{ticker}:{datetime.now().strftime('%Y-%m-%d')}",
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Evidence item for current price
        last_price = snap.get("lastTrade", {}).get("p", 0)
        day_data = snap.get("day", {})
        if last_price:
            upsert(self.conn, "evidence_item", {
                "evidence_id": new_id(),
                "document_id": doc_id,
                "company_id": company_id,
                "evidence_type": "MARKET_DATA",
                "evidence_key": f"snapshot_{datetime.now().strftime('%Y-%m-%d')}",
                "value": f"Last: ${last_price:.2f} | Day: O:{day_data.get('o',0):.2f} H:{day_data.get('h',0):.2f} L:{day_data.get('l',0):.2f} C:{day_data.get('c',0):.2f} V:{day_data.get('v',0):,.0f}",
                "as_of_date": datetime.now().strftime("%Y-%m-%d"),
                "run_id": self.run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

        self.conn.commit()
        return {
            "last_price": last_price,
            "day": day_data,
            "prev_close": snap.get("prevDay", {}).get("c", 0),
            "source_document_id": doc_id,
        }
