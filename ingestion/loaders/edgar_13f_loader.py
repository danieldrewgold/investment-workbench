"""
EDGAR 13F Ingestion Loader

Fetches 13F-HR filings from SEC EDGAR, parses information table XML,
stores holdings with provenance. Builds the raw data layer for
crowding analysis.

RAW: 13F filing metadata stored as source_documents
NORMALIZED: Individual holdings in holding_13f, filing metadata in filing_13f

The 13F information table uses a standard XML schema:
  namespace: http://www.sec.gov/edgar/document/thirteenf/informationtable
  each <infoTable> element contains one holding

Data is 45 days stale by design (filing deadline is 45 days after quarter end).
"""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from core.provenance.database import new_id, upsert, hash_content, now_iso

# Reuse the same user-agent pattern as edgar_loader
SEC_HEADERS = {
    "User-Agent": "InvestmentWorkbench research@example.com",
    "Accept": "application/json",
}

# 13F XML namespace
NS_13F = "http://www.sec.gov/edgar/document/thirteenf/informationtable"

FUND_UNIVERSE_PATH = Path(__file__).parent.parent.parent / "data" / "fund_universe.json"


class Edgar13FLoader:

    def __init__(self, conn, run_id: str):
        self.conn = conn
        self.run_id = run_id
        self.client = httpx.AsyncClient(
            headers=SEC_HEADERS, timeout=30.0, follow_redirects=True,
        )
        self._semaphore = asyncio.Semaphore(8)  # SEC allows 10 req/s, stay under

    async def close(self):
        await self.client.aclose()

    # ── Fund universe management ────────────────────────────────

    def seed_fund_universe(self, fund_list: list[dict] = None) -> int:
        """
        Load fund universe from JSON into fund_universe table.
        Returns count of funds loaded.
        """
        if fund_list is None:
            if not FUND_UNIVERSE_PATH.exists():
                return 0
            fund_list = json.loads(FUND_UNIVERSE_PATH.read_text())

        count = 0
        for f in fund_list:
            cik = str(f["cik"]).zfill(10)
            upsert(self.conn, "fund_universe", {
                "fund_id": new_id(),
                "fund_name": f["name"],
                "cik": cik,
                "fund_type": f.get("type", "hedge_fund"),
                "is_active": 1,
                "added_at": now_iso(),
                "added_by_run": self.run_id,
            }, conflict_columns=["cik"],
            update_columns=["fund_name", "fund_type", "added_by_run"])
            count += 1

        self.conn.commit()
        return count

    def get_active_funds(self) -> list[dict]:
        """Return all active funds from fund_universe."""
        rows = self.conn.execute(
            "SELECT fund_id, fund_name, cik FROM fund_universe WHERE is_active = 1"
        ).fetchall()
        return [{"fund_id": r[0], "fund_name": r[1], "cik": r[2]} for r in rows]

    # ── 13F filing ingestion ────────────────────────────────────

    async def ingest_13f_filings(
        self, fund_id: str, cik: str, quarters_back: int = 4,
    ) -> list[str]:
        """
        Fetch 13F-HR filings for a single fund, parse holdings.
        Returns list of filing_ids ingested.
        """
        cik = str(cik).zfill(10)
        cik_stripped = cik.lstrip("0")

        # Step 1: Fetch filing index
        async with self._semaphore:
            try:
                resp = await self.client.get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json"
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
            except Exception:
                return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        report_dates = recent.get("reportDate", [])

        # Filter for 13F-HR filings within quarters_back
        cutoff_days = quarters_back * 95  # ~95 days per quarter
        cutoff = (datetime.now() - timedelta(days=cutoff_days)).strftime("%Y-%m-%d")
        filing_ids = []

        for i, form in enumerate(forms):
            if form not in ("13F-HR", "13F-HR/A"):
                continue
            if i >= len(dates) or i >= len(accessions):
                break
            if dates[i] < cutoff:
                continue

            accession = accessions[i]
            report_date = report_dates[i] if i < len(report_dates) else dates[i]

            # Check if already ingested
            existing = self.conn.execute(
                "SELECT filing_id FROM filing_13f WHERE fund_id = ? AND accession_number = ?",
                (fund_id, accession),
            ).fetchone()
            if existing:
                filing_ids.append(existing[0])
                continue

            # Step 2: Fetch and parse the info table
            holdings = await self._fetch_info_table(cik_stripped, accession)
            if not holdings:
                continue

            # Store source document
            locator = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession.replace('-', '')}/"
            doc_id = new_id()
            upsert(self.conn, "source_document", {
                "document_id": doc_id,
                "source_type": "FILING",
                "source_name": "SEC_EDGAR_13F",
                "source_locator": locator,
                "source_published_at": dates[i],
                "fetched_at": now_iso(),
                "content_hash": hash_content(json.dumps(holdings)),
                "content_summary": f"13F-HR: {len(holdings)} holdings",
                "run_id": self.run_id,
            }, conflict_columns=["source_type", "source_locator"],
            update_columns=["fetched_at", "run_id"])

            # Store filing record
            total_value = sum(h.get("value_thousands", 0) for h in holdings)
            filing_id = new_id()
            upsert(self.conn, "filing_13f", {
                "filing_id": filing_id,
                "fund_id": fund_id,
                "accession_number": accession,
                "report_date": report_date,
                "filed_date": dates[i],
                "total_value_m": round(total_value / 1000, 2),  # thousands → millions
                "position_count": len(holdings),
                "source_document_id": doc_id,
                "run_id": self.run_id,
                "created_at": now_iso(),
            }, conflict_columns=["fund_id", "accession_number"],
            update_columns=["total_value_m", "position_count", "run_id"])

            # Store individual holdings
            for h in holdings:
                put_call = h.get("put_call") or "NONE"
                upsert(self.conn, "holding_13f", {
                    "holding_id": new_id(),
                    "filing_id": filing_id,
                    "fund_id": fund_id,
                    "cusip": h["cusip"],
                    "issuer_name": h.get("issuer_name", ""),
                    "title_of_class": h.get("title_of_class", ""),
                    "value_thousands": h.get("value_thousands", 0),
                    "shares_or_amount": h.get("shares_or_amount", 0),
                    "sh_prn_type": h.get("sh_prn_type", "SH"),
                    "put_call": put_call,
                    "investment_discretion": h.get("investment_discretion", ""),
                    "voting_sole": h.get("voting_sole", 0),
                    "voting_shared": h.get("voting_shared", 0),
                    "voting_none": h.get("voting_none", 0),
                    "report_date": report_date,
                    "run_id": self.run_id,
                    "created_at": now_iso(),
                }, conflict_columns=["filing_id", "cusip", "put_call"],
                update_columns=[
                    "value_thousands", "shares_or_amount", "sh_prn_type",
                    "investment_discretion", "voting_sole", "voting_shared",
                    "voting_none", "run_id",
                ])

            filing_ids.append(filing_id)

        self.conn.commit()
        return filing_ids

    async def ingest_all_funds(self, quarters_back: int = 4) -> dict:
        """
        Ingest 13F filings for all active funds in fund_universe.
        Returns summary dict: {funds_processed, filings_ingested, errors}.
        """
        funds = self.get_active_funds()
        if not funds:
            # Auto-seed if empty
            self.seed_fund_universe()
            funds = self.get_active_funds()

        results = {"funds_processed": 0, "filings_ingested": 0, "errors": []}

        for fund in funds:
            try:
                fids = await self.ingest_13f_filings(
                    fund["fund_id"], fund["cik"], quarters_back,
                )
                results["funds_processed"] += 1
                results["filings_ingested"] += len(fids)
            except Exception as e:
                results["errors"].append(f"{fund['fund_name']}: {type(e).__name__}: {e}")

            # Small delay to stay well under SEC rate limit
            await asyncio.sleep(0.15)

        return results

    # ── 13F XML parsing ─────────────────────────────────────────

    async def _fetch_info_table(self, cik_stripped: str, accession: str) -> list[dict]:
        """Fetch the 13F information table XML and parse holdings."""
        acc_nodash = accession.replace("-", "")
        dir_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_nodash}/"

        async with self._semaphore:
            try:
                resp = await self.client.get(dir_url)
                if resp.status_code != 200:
                    return []
            except Exception:
                return []

        # Find the info table XML file
        xml_matches = re.findall(r'href="([^"]*(?:infotable|INFOTABLE)[^"]*\.xml)"', resp.text, re.I)
        if not xml_matches:
            # Broader match: any XML that isn't the primary document
            xml_matches = re.findall(r'href="([^"]+\.xml)"', resp.text)
            # Filter to likely info table files
            xml_matches = [m for m in xml_matches if "primary" not in m.lower()]

        if not xml_matches:
            return []

        xml_filename = xml_matches[0]
        if xml_filename.startswith("http"):
            xml_url = xml_filename
        elif xml_filename.startswith("/"):
            # SEC directory listings return absolute paths
            # ("/Archives/edgar/data/..."); prepend the host, NOT dir_url.
            xml_url = "https://www.sec.gov" + xml_filename
        else:
            xml_url = dir_url + xml_filename

        async with self._semaphore:
            try:
                resp = await self.client.get(xml_url)
                if resp.status_code != 200:
                    return []
                return self.parse_13f_xml(resp.text)
            except Exception:
                return []

    @staticmethod
    def parse_13f_xml(xml_text: str) -> list[dict]:
        """
        Parse 13F information table XML into list of holding dicts.
        Static for testability.

        Expected XML structure (namespace: NS_13F):
            <informationTable>
              <infoTable>
                <nameOfIssuer>...</nameOfIssuer>
                <titleOfClass>...</titleOfClass>
                <cusip>...</cusip>
                <value>...</value>  (in thousands)
                <shrsOrPrnAmt>
                  <sshPrnamt>...</sshPrnamt>
                  <sshPrnamtType>SH|PRN</sshPrnamtType>
                </shrsOrPrnAmt>
                <putCall>PUT|CALL</putCall>  (optional)
                <investmentDiscretion>SOLE|DEFINED|OTHER</investmentDiscretion>
                <votingAuthority>
                  <Sole>...</Sole>
                  <Shared>...</Shared>
                  <None>...</None>
                </votingAuthority>
              </infoTable>
              ...
            </informationTable>
        """
        holdings = []

        # Try with namespace first, then without (some filings omit namespace)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        # Detect namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        elif NS_13F in xml_text:
            ns = f"{{{NS_13F}}}"

        # Find all infoTable elements
        info_tables = root.findall(f".//{ns}infoTable")
        if not info_tables:
            # Try without namespace
            info_tables = root.findall(".//infoTable")

        for entry in info_tables:
            h = {}

            issuer = entry.find(f"{ns}nameOfIssuer")
            if issuer is None:
                issuer = entry.find("nameOfIssuer")
            h["issuer_name"] = issuer.text.strip() if issuer is not None and issuer.text else ""

            title = entry.find(f"{ns}titleOfClass")
            if title is None:
                title = entry.find("titleOfClass")
            h["title_of_class"] = title.text.strip() if title is not None and title.text else ""

            cusip = entry.find(f"{ns}cusip")
            if cusip is None:
                cusip = entry.find("cusip")
            h["cusip"] = cusip.text.strip() if cusip is not None and cusip.text else ""

            if not h["cusip"]:
                continue  # CUSIP is required

            # Per SEC: filings on/after Aug 2022 report value in actual
            # dollars. We only ingest recent quarters (last 4) so all our
            # data is post-Aug 2022. Always divide raw <value> by 1000
            # so the stored `value_thousands` field actually contains
            # thousands. The earlier heuristic (only divide if >=1M)
            # was wrong for small positions like ~$870K which got
            # stored unchanged and looked 1000x too large.
            value = entry.find(f"{ns}value")
            if value is None:
                value = entry.find("value")
            try:
                raw_value = int(value.text.strip()) if value is not None and value.text else 0
            except (ValueError, AttributeError):
                raw_value = 0
            h["value_thousands"] = raw_value // 1000

            # Shares or principal amount
            shramt = entry.find(f".//{ns}sshPrnamt")
            if shramt is None:
                shramt = entry.find(".//sshPrnamt")
            try:
                h["shares_or_amount"] = int(shramt.text.strip()) if shramt is not None and shramt.text else 0
            except (ValueError, AttributeError):
                h["shares_or_amount"] = 0

            shrtype = entry.find(f".//{ns}sshPrnamtType")
            if shrtype is None:
                shrtype = entry.find(".//sshPrnamtType")
            h["sh_prn_type"] = shrtype.text.strip() if shrtype is not None and shrtype.text else "SH"

            # Optional put/call
            pc = entry.find(f"{ns}putCall")
            if pc is None:
                pc = entry.find("putCall")
            h["put_call"] = pc.text.strip() if pc is not None and pc.text else None

            # Investment discretion
            disc = entry.find(f"{ns}investmentDiscretion")
            if disc is None:
                disc = entry.find("investmentDiscretion")
            h["investment_discretion"] = disc.text.strip() if disc is not None and disc.text else ""

            # Voting authority
            for vtype in ("Sole", "Shared", "None"):
                vel = entry.find(f".//{ns}{vtype}")
                if vel is None:
                    vel = entry.find(f".//{vtype}")
                try:
                    h[f"voting_{vtype.lower()}"] = int(vel.text.strip()) if vel is not None and vel.text else 0
                except (ValueError, AttributeError):
                    h[f"voting_{vtype.lower()}"] = 0

            holdings.append(h)

        return holdings

    # ── CUSIP resolution ────────────────────────────────────────

    def resolve_cusip_to_ticker(self, cusip: str) -> str | None:
        """
        Look up ticker for a CUSIP. Checks local cusip_mapping table first.
        Returns ticker or None.
        """
        row = self.conn.execute(
            "SELECT ticker FROM cusip_mapping WHERE cusip = ?", (cusip,)
        ).fetchone()
        if row:
            return row[0]
        return None

    def update_cusip_mapping(self, cusip: str, ticker: str, issuer_name: str = "",
                             company_id: str = None):
        """Store a CUSIP → ticker mapping."""
        upsert(self.conn, "cusip_mapping", {
            "cusip": cusip,
            "ticker": ticker,
            "company_id": company_id,
            "issuer_name": issuer_name,
            "security_type": "common",
            "last_seen_date": now_iso()[:10],
            "run_id": self.run_id,
        }, conflict_columns=["cusip"],
        update_columns=["ticker", "company_id", "issuer_name", "last_seen_date", "run_id"])
        self.conn.commit()

    async def resolve_cusips_via_openfigi(self, cusips: list[str]) -> dict[str, str]:
        """
        Batch resolve CUSIPs to tickers via OpenFIGI free API.
        Returns {cusip: ticker} for successful lookups.
        Free tier: 25 requests/minute, 100 CUSIPs per request.
        """
        results = {}
        # Batch in groups of 100
        for i in range(0, len(cusips), 100):
            batch = cusips[i:i + 100]
            payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]

            async with self._semaphore:
                try:
                    resp = await self.client.post(
                        "https://api.openfigi.com/v3/mapping",
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                except Exception:
                    continue

            for j, item in enumerate(data):
                if isinstance(item, dict) and "data" in item and item["data"]:
                    ticker = item["data"][0].get("ticker")
                    if ticker and j < len(batch):
                        results[batch[j]] = ticker
                        self.update_cusip_mapping(batch[j], ticker,
                                                  item["data"][0].get("name", ""))

            # Respect rate limit
            if i + 100 < len(cusips):
                await asyncio.sleep(2.5)

        return results

    def get_holdings_for_cusip(self, cusip: str, report_date: str = None) -> list[dict]:
        """
        Get all fund holdings for a given CUSIP.
        If report_date specified, filter to that quarter.
        Otherwise return the latest quarter.
        """
        if report_date:
            rows = self.conn.execute("""
                SELECT h.fund_id, f.fund_name, h.value_thousands, h.shares_or_amount,
                       h.report_date, fi.total_value_m
                FROM holding_13f h
                JOIN fund_universe f ON h.fund_id = f.fund_id
                JOIN filing_13f fi ON h.filing_id = fi.filing_id
                WHERE h.cusip = ? AND h.report_date = ?
                ORDER BY h.value_thousands DESC
            """, (cusip, report_date)).fetchall()
        else:
            # Get the latest report_date available
            latest = self.conn.execute(
                "SELECT MAX(report_date) FROM holding_13f WHERE cusip = ?", (cusip,)
            ).fetchone()
            if not latest or not latest[0]:
                return []
            report_date = latest[0]
            rows = self.conn.execute("""
                SELECT h.fund_id, f.fund_name, h.value_thousands, h.shares_or_amount,
                       h.report_date, fi.total_value_m
                FROM holding_13f h
                JOIN fund_universe f ON h.fund_id = f.fund_id
                JOIN filing_13f fi ON h.filing_id = fi.filing_id
                WHERE h.cusip = ? AND h.report_date = ?
                ORDER BY h.value_thousands DESC
            """, (cusip, report_date)).fetchall()

        return [{
            "fund_id": r[0],
            "fund_name": r[1],
            "value_thousands": r[2],
            "shares_or_amount": r[3],
            "report_date": r[4],
            "fund_total_value_m": r[5],
        } for r in rows]
