"""
Test fixtures — realistic sample payloads from each data source.

These are frozen snapshots of what the APIs actually return,
used to test ingestion, normalization, and derivation without
hitting live APIs.
"""

# ── EDGAR: Company submissions (CIK lookup) ─────────────────

EDGAR_SUBMISSIONS_CMG = {
    "cik": "0001058090",
    "entityType": "operating",
    "sic": "5812",
    "sicDescription": "Retail-eating Places",
    "name": "CHIPOTLE MEXICAN GRILL INC",
    "tickers": ["CMG"],
    "exchanges": ["NYSE"],
    "filings": {
        "recent": {
            "accessionNumber": [
                "0001058090-26-000012",
                "0001058090-26-000008",
                "0001058090-25-000045",
            ],
            "filingDate": ["2026-02-14", "2026-01-15", "2025-10-28"],
            "form": ["10-K", "8-K", "10-Q"],
            "primaryDocument": ["cmg-20251231.htm", "cmg-20260115.htm", "cmg-20250930.htm"],
        }
    }
}

# ── EDGAR: Form 4 XML (insider transaction) ──────────────────

FORM4_XML_PURCHASE = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0001058090</issuerCik>
    <issuerName>CHIPOTLE MEXICAN GRILL INC</issuerName>
    <issuerTradingSymbol>CMG</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>NICCOL SCOTT</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chairman and CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-10</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>58.25</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>125000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

FORM4_XML_SALE = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>JONES SARAH</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>VP Finance</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>61.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


# ── Polygon: Ticker details ──────────────────────────────────

POLYGON_TICKER_DETAILS_CMG = {
    "results": {
        "ticker": "CMG",
        "name": "Chipotle Mexican Grill, Inc.",
        "market": "stocks",
        "locale": "us",
        "primary_exchange": "XNYS",
        "type": "CS",
        "currency_name": "usd",
        "market_cap": 79500000000,
        "share_class_shares_outstanding": 1370000000,
        "sic_code": "5812",
        "sic_description": "Retail-Eating Places",
    }
}

# ── Polygon: Snapshot ────────────────────────────────────────

POLYGON_SNAPSHOT_CMG = {
    "ticker": {
        "ticker": "CMG",
        "lastTrade": {"p": 58.12, "s": 100, "t": 1711555200000},
        "day": {"o": 57.80, "h": 58.50, "l": 57.20, "c": 58.12, "v": 4500000},
        "prevDay": {"c": 57.95, "v": 3800000},
    }
}


# ── Yahoo Finance: Earnings calendar ─────────────────────────

YAHOO_EARNINGS_CMG = {
    "next_earnings": "2026-04-22",
    "days_until_next": 26,
    "earnings_within_10d": False,
    "last_earnings": "2026-02-04",
    "source": "yahoo_finance",
}


# ── FINRA: Dark pool volume ──────────────────────────────────

FINRA_DARK_POOL_CMG = [
    {"weekStartDate": "2026-03-17", "totalWeeklyShareQuantity": 2800000, "totalWeeklyTradeCount": 15200},
    {"weekStartDate": "2026-03-10", "totalWeeklyShareQuantity": 2100000, "totalWeeklyTradeCount": 12400},
    {"weekStartDate": "2026-03-03", "totalWeeklyShareQuantity": 2350000, "totalWeeklyTradeCount": 13100},
]


# ── FINRA: Short interest ────────────────────────────────────

FINRA_SHORT_INTEREST_CMG = {
    "short_interest": 18500000,
    "prior_short_interest": 16200000,
    "change_pct": 14.2,
    "days_to_cover": 3.8,
    "avg_daily_volume": 4870000,
    "settlement_date": "2026-03-15",
    "source": "finra",
}


# ── Consensus estimates (simulated Yahoo/FactSet) ────────────

CONSENSUS_CMG_FY2026 = {
    "ticker": "CMG",
    "period": "FY2026",
    "as_of_date": "2026-03-25",
    "estimates": {
        "revenue": {"mean": 12450, "median": 12420, "high": 12900, "low": 12100, "count": 28},
        "eps": {"mean": 1.18, "median": 1.17, "high": 1.30, "low": 1.05, "count": 28},
        "ebitda": {"mean": 2680, "median": 2670, "high": 2850, "low": 2500, "count": 22},
    },
    "source": "yahoo_finance",
}


# ── Management guidance (from earnings call) ─────────────────

GUIDANCE_CMG_FY2026 = {
    "ticker": "CMG",
    "period": "FY2026",
    "guidance_date": "2026-02-04",
    "source_filing": "8-K filed 2026-02-04",
    "metrics": {
        "sss_growth": {"low": 4.0, "mid": 5.5, "high": 7.0, "unit": "PCT"},
        "new_store_openings": {"low": 285, "mid": 300, "high": 315, "unit": "COUNT"},
        "restaurant_margin": {"low": 27.0, "mid": 27.8, "high": 28.5, "unit": "PCT"},
    },
}


# ── Golden test: complete research plan output ───────────────

GOLDEN_RESEARCH_PLAN_CMG = {
    "ticker": "CMG",
    "edge_hypothesis": "Throughput improvements + menu pricing = SSS acceleration to 8-10% not reflected in consensus 4-5%",
    "edge_type": "EXPECTATION_GAP",
    "why_opportunity_exists": "Street models assume SSS normalizes to 4-5% post-pricing. Channel checks and throughput data suggest 8%+ is sustainable for 2-3 more quarters.",
    "what_makes_valuable": "3-4% SSS beat flows through at 60%+ incremental margin, worth $0.08-0.12 EPS per quarter. On a 45x P/E that's $4-5 per share per quarter of upside.",
    "key_questions": [
        {"question": "What is the actual contribution of throughput improvements to transaction count?", "priority": "HIGH"},
        {"question": "Is pricing power sustainable at current menu levels without traffic erosion?", "priority": "HIGH"},
        {"question": "How does digital mix shift affect restaurant-level margin?", "priority": "MEDIUM"},
    ],
    "key_drivers": [
        {
            "name": "SSS growth",
            "transmission": "SSS -> revenue -> operating leverage on fixed costs -> EPS beat -> multiple re-rate on sustained beat cycle",
            "evidence_needed": "Transaction count trend, check size trend, throughput per labor hour",
        },
    ],
    "workstreams": [
        {"type": "KPI_FORECAST", "justification": "SSS is the key driver and we need an independent estimate"},
        {"type": "OPERATING_BUILD", "justification": "Need to model the margin flowthrough from SSS to EPS"},
        {"type": "GUIDANCE_COMPARISON", "justification": "Management guided 4-7% SSS; our estimate above range needs defense"},
    ],
    "kill_conditions": [
        {"condition": "Q2 2026 SSS below 3%", "metric": "SSS", "threshold": "3%"},
        {"condition": "Transaction count goes negative for 2 consecutive quarters", "metric": "transaction_count_growth", "threshold": "0%"},
    ],
    "analytical_lenses": ["price_volume_mix", "units_x_asp", "margin_bridge", "historical_analogs"],
    "run_id": "test-run-001",
}
