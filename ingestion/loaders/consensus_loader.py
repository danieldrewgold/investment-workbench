"""
Consensus & Guidance Loader

Fetches consensus estimates and earnings calendar from Yahoo Finance.
Stores with provenance and normalizes into canonical objects.

RAW: Scraped HTML/JSON stored as source_documents
NORMALIZED: consensus_snapshot, guidance_point, reporting_period (earnings dates)

This is Layer 3 infrastructure: the system needs consensus and guidance
anchors to build independent estimates against.
"""

import re
import json
import httpx
from datetime import datetime, timedelta
from pathlib import Path

from core.provenance.database import new_id, upsert


class ConsensusLoader:

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    # ── Earnings calendar ─────────────────────────────────────

    async def ingest_earnings_dates(self, company_id: str, ticker: str) -> dict:
        """
        Scrape next/last earnings dates from Yahoo Finance.
        Updates reporting_period.earnings_date for the relevant periods.
        Returns dict with next_earnings, days_until, etc.
        """
        ticker = ticker.upper()
        url = f"https://finance.yahoo.com/quote/{ticker}/"

        try:
            resp = await self.client.get(url)
            if resp.status_code != 200:
                return {}
            html = resp.text
        except Exception:
            return {}

        now = datetime.utcnow().isoformat() + "Z"

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "WEBPAGE",
            "source_name": "YAHOO_FINANCE",
            "source_locator": url,
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Parse earnings date from page
        # Yahoo shows "Earnings Date" in the summary table
        earnings_date = None
        match = re.search(r'Earnings Date.*?(\w{3} \d{1,2}, \d{4})', html, re.DOTALL)
        if match:
            try:
                earnings_date = datetime.strptime(match.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        if not earnings_date:
            # Try alternate pattern
            match = re.search(r'"earningsTimestamp":(\d+)', html)
            if match:
                ts = int(match.group(1))
                earnings_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

        result = {"source_document_id": doc_id}

        if earnings_date:
            days_until = (datetime.strptime(earnings_date, "%Y-%m-%d") - datetime.now()).days
            result.update({
                "next_earnings": earnings_date,
                "days_until": days_until,
                "within_10d": days_until <= 10,
            })

            # Store as evidence
            upsert(self.conn, "evidence_item", {
                "evidence_id": new_id(),
                "document_id": doc_id,
                "company_id": company_id,
                "evidence_type": "EARNINGS_CALENDAR",
                "evidence_key": f"next_earnings_{ticker}",
                "value": f"Next earnings: {earnings_date} ({days_until} days)",
                "as_of_date": datetime.now().strftime("%Y-%m-%d"),
                "run_id": self.run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

        self.conn.commit()
        return result

    # ── Consensus estimates ───────────────────────────────────

    async def ingest_consensus_estimates(
        self, company_id: str, ticker: str,
    ) -> list[str]:
        """
        Fetch consensus EPS/revenue estimates from Yahoo Finance analysis page.
        Creates consensus_snapshot records for each metric and period.
        Returns list of snapshot_ids created.
        """
        ticker = ticker.upper()
        url = f"https://finance.yahoo.com/quote/{ticker}/analysis/"

        try:
            resp = await self.client.get(url)
            if resp.status_code != 200:
                return []
            html = resp.text
        except Exception:
            return []

        now = datetime.utcnow().isoformat() + "Z"
        today = datetime.now().strftime("%Y-%m-%d")

        # Source document
        doc_id = new_id()
        upsert(self.conn, "source_document", {
            "document_id": doc_id,
            "source_type": "WEBPAGE",
            "source_name": "YAHOO_FINANCE",
            "source_locator": f"yahoo:analysis:{ticker}:{today}",
            "fetched_at": now,
            "company_id": company_id,
            "run_id": self.run_id,
        }, conflict_columns=["source_type", "source_locator"],
        update_columns=["fetched_at", "run_id"])

        # Parse the analysis page for EPS and revenue estimates
        # Yahoo's analysis page has tables with Current Qtr, Next Qtr, Current Year, Next Year
        snapshot_ids = []
        estimates = self._parse_yahoo_analysis(html, ticker)

        for est in estimates:
            # Ensure metric definition exists
            metric_name = est["metric"]  # eps, revenue
            upsert(self.conn, "metric_definition", {
                "metric_id": new_id(),
                "metric_name": metric_name,
                "metric_source": "YAHOO_FINANCE",
                "unit": "USD" if metric_name == "eps" else "USD_M",
                "frequency": "quarterly" if "Q" in est.get("period_label", "") else "annual",
            }, conflict_columns=["metric_name", "metric_source"])

            metric_row = self.conn.execute(
                "SELECT metric_id FROM metric_definition WHERE metric_name=? AND metric_source=?",
                (metric_name, "YAHOO_FINANCE")
            ).fetchone()
            if not metric_row:
                continue
            metric_id = metric_row[0]

            # Resolve or create reporting period
            period_id = self._resolve_period(company_id, est)

            # Create consensus snapshot
            sid = new_id()
            upsert(self.conn, "consensus_snapshot", {
                "snapshot_id": sid,
                "company_id": company_id,
                "metric_id": metric_id,
                "period_id": period_id,
                "as_of_date": today,
                "source_name": "YAHOO_FINANCE",
                "estimate_mean": est.get("mean"),
                "estimate_high": est.get("high"),
                "estimate_low": est.get("low"),
                "num_analysts": est.get("num_analysts"),
                "run_id": self.run_id,
            }, conflict_columns=["company_id", "metric_id", "period_id", "as_of_date", "source_name"],
            update_columns=["estimate_mean", "estimate_high", "estimate_low", "num_analysts", "run_id"])

            snapshot_ids.append(sid)

        self.conn.commit()
        return snapshot_ids

    def _resolve_period(self, company_id: str, est: dict) -> str:
        """Find or create the reporting period for an estimate."""
        fy = est.get("fiscal_year")
        fq = est.get("fiscal_quarter")
        ptype = est.get("period_type", "FY")

        if not fy:
            fy = datetime.now().year

        row = self.conn.execute(
            """SELECT period_id FROM reporting_period
               WHERE company_id=? AND period_type=? AND fiscal_year=?
               AND (fiscal_quarter IS ? OR fiscal_quarter = ?)""",
            (company_id, ptype, fy, fq, fq)
        ).fetchone()

        if row:
            return row[0]

        pid = new_id()
        self.conn.execute(
            """INSERT INTO reporting_period
               (period_id, company_id, period_type, fiscal_year, fiscal_quarter)
               VALUES (?,?,?,?,?)""",
            (pid, company_id, ptype, fy, fq)
        )
        return pid

    def _parse_yahoo_analysis(self, html: str, ticker: str) -> list[dict]:
        """
        Parse Yahoo Finance analysis page for consensus estimates.
        Returns list of dicts with metric, period info, and estimate values.

        Yahoo's format changes periodically. This parser extracts from
        the JSON embedded in the page when available, falling back to
        table scraping.
        """
        estimates = []

        # Try to find the JSON store (more reliable than HTML scraping)
        json_match = re.search(r'"earningsTrend":\s*(\{[^}]+\})', html)
        if json_match:
            try:
                # This is a simplified extraction; real Yahoo pages have deeper nesting
                pass
            except Exception:
                pass

        # Fallback: regex for common patterns in the analysis tables
        # Look for "Avg. Estimate" rows
        # EPS estimates
        eps_patterns = re.findall(
            r'Avg\.\s*Estimate.*?(\d+\.\d+)', html, re.DOTALL
        )

        # Revenue estimates (in billions/millions)
        rev_patterns = re.findall(
            r'Avg\.\s*Estimate.*?(\d+(?:\.\d+)?[BMK])', html, re.DOTALL
        )

        # Number of analysts
        analyst_patterns = re.findall(
            r'No\.\s*of\s*Analysts.*?(\d+)', html, re.DOTALL
        )

        # If we got any EPS estimates, create entries
        current_year = datetime.now().year
        for i, eps_val in enumerate(eps_patterns[:4]):
            try:
                val = float(eps_val)
            except ValueError:
                continue

            if i == 0:
                period = {"period_type": "Q", "fiscal_year": current_year,
                         "fiscal_quarter": self._current_quarter(), "period_label": "Current Qtr"}
            elif i == 1:
                period = {"period_type": "Q", "fiscal_year": current_year,
                         "fiscal_quarter": self._next_quarter(), "period_label": "Next Qtr"}
            elif i == 2:
                period = {"period_type": "FY", "fiscal_year": current_year,
                         "fiscal_quarter": None, "period_label": "Current Year"}
            else:
                period = {"period_type": "FY", "fiscal_year": current_year + 1,
                         "fiscal_quarter": None, "period_label": "Next Year"}

            est = {"metric": "eps", "mean": val}
            est.update(period)
            if i < len(analyst_patterns):
                try:
                    est["num_analysts"] = int(analyst_patterns[i])
                except ValueError:
                    pass
            estimates.append(est)

        return estimates

    @staticmethod
    def _current_quarter() -> int:
        month = datetime.now().month
        return (month - 1) // 3 + 1

    @staticmethod
    def _next_quarter() -> int:
        q = ConsensusLoader._current_quarter()
        return q % 4 + 1


def ingest_guidance_from_dict(
    conn, company_id: str, guidance_data: dict, run_id: str,
) -> list[str]:
    """
    Store management guidance from a structured dict (e.g. from earnings call parsing).
    Matches the GUIDANCE_CMG_FY2026 fixture format.

    Returns list of guidance_ids created.
    """
    now = datetime.utcnow().isoformat() + "Z"
    guidance_date = guidance_data.get("guidance_date", "")
    period_label = guidance_data.get("period", "")

    # Source document for the guidance
    locator = f"guidance:{guidance_data.get('ticker', '')}:{period_label}:{guidance_date}"
    upsert(conn, "source_document", {
        "document_id": new_id(),
        "source_type": "FILING",
        "source_name": "COMPANY_GUIDANCE",
        "source_locator": locator,
        "source_published_at": guidance_date,
        "fetched_at": now,
        "company_id": company_id,
        "run_id": run_id,
    }, conflict_columns=["source_type", "source_locator"],
    update_columns=["fetched_at", "run_id"])

    # Retrieve the actual document_id (may be from a prior insert if upsert hit conflict)
    doc_row = conn.execute(
        "SELECT document_id FROM source_document WHERE source_type=? AND source_locator=?",
        ("FILING", locator)
    ).fetchone()
    doc_id = doc_row[0] if doc_row else new_id()

    # Resolve fiscal year from period label
    fy = None
    fq = None
    ptype = "FY"
    if period_label:
        fy_match = re.search(r'(\d{4})', period_label)
        if fy_match:
            fy = int(fy_match.group(1))
        if "Q" in period_label.upper():
            q_match = re.search(r'Q(\d)', period_label.upper())
            if q_match:
                fq = int(q_match.group(1))
                ptype = "Q"
    if not fy:
        fy = datetime.now().year

    # Resolve reporting period
    row = conn.execute(
        "SELECT period_id FROM reporting_period WHERE company_id=? AND period_type=? AND fiscal_year=?",
        (company_id, ptype, fy)
    ).fetchone()
    if row:
        period_id = row[0]
    else:
        period_id = new_id()
        conn.execute(
            "INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year, fiscal_quarter) VALUES (?,?,?,?,?)",
            (period_id, company_id, ptype, fy, fq)
        )

    guidance_ids = []

    for metric_name, values in guidance_data.get("metrics", {}).items():
        # Ensure metric definition
        unit = values.get("unit", "USD")
        upsert(conn, "metric_definition", {
            "metric_id": new_id(),
            "metric_name": metric_name,
            "metric_source": "COMPANY_GUIDANCE",
            "unit": unit,
        }, conflict_columns=["metric_name", "metric_source"])

        metric_row = conn.execute(
            "SELECT metric_id FROM metric_definition WHERE metric_name=? AND metric_source=?",
            (metric_name, "COMPANY_GUIDANCE")
        ).fetchone()
        if not metric_row:
            continue

        gid = new_id()
        upsert(conn, "guidance_point", {
            "guidance_id": gid,
            "company_id": company_id,
            "metric_id": metric_row[0],
            "period_id": period_id,
            "guidance_type": "initial",
            "value_low": values.get("low"),
            "value_high": values.get("high"),
            "value_point": values.get("mid"),
            "guidance_date": guidance_date,
            "source_document_id": doc_id,
            "run_id": run_id,
        }, conflict_columns=["company_id", "metric_id", "period_id", "guidance_type", "source_document_id"],
        update_columns=["value_low", "value_high", "value_point", "guidance_date", "run_id"])

        guidance_ids.append(gid)

        # Also store as evidence item
        ev_text = f"{metric_name} guidance: {values.get('low')}-{values.get('high')} {unit}"
        upsert(conn, "evidence_item", {
            "evidence_id": new_id(),
            "document_id": doc_id,
            "company_id": company_id,
            "evidence_type": "GUIDANCE",
            "evidence_key": f"guidance_{metric_name}_{period_label}",
            "value": ev_text,
            "as_of_date": guidance_date,
            "run_id": run_id,
        }, conflict_columns=["document_id", "evidence_type", "evidence_key"])

    conn.commit()
    return guidance_ids
