# Investment Research Operating System — Architecture Map

## What This System Does

This is a Python-based investment research system that takes a stock ticker, fetches financial data and earnings call transcripts from public APIs, uses Claude AI to analyze the business and build a forward earnings estimate, stress-tests that estimate through an independent adversarial review, detects where the estimate differs from Wall Street consensus, and produces formatted research outputs (Excel workbooks, tear sheets, pitch documents).

The entire flow runs from one command: `python cli.py research CMG`

---

## Files and What They Do

### Entry Points

**cli.py** (106 lines)
The command-line interface. Two commands: `research <TICKER>` runs the full 13-step pipeline on any ticker. `scan <TICKER>,<TICKER>` runs multiple tickers in batch. Supports `--verbose` for step-by-step trace, `--detail <workpaper>` to expand a specific workpaper, and auto-exports to Excel.

**run_tests.py** (28 lines)
Runs all 8 test suites (62 tests total) and reports pass/fail.

**run_pipeline_cmg.py** (1293 lines)
Hardcoded end-to-end validation for Chipotle. Uses real FY2024 data to produce an FY2025 estimate and compares against actual results. Demonstrates every layer of the system working together.

**run_pipeline_wing.py** (271 lines)
Validates the franchise restaurant schema on Wingstop. Proves the franchise revenue model (royalties + ad fund + company-owned) works correctly.

**run_extraction_test.py** (394 lines)
Standalone test that sends real earnings text to Claude API and checks extraction quality. Includes honest post-mortem on where estimates went wrong.

---

### The Pipeline (how data flows)

**research/pipeline.py** (949 lines)
The main orchestrator. Runs 13 steps in sequence:

1. **Fetch structured financials** — Calls Polygon, then Alpha Vantage, then falls back to registry
2. **Fetch filing text** — Gets press release from EDGAR Exhibit 99.1, plus 8-K/10-K text
3. **Fetch earnings call transcripts** — Gets 3 years of quarterly transcripts via EarningsCall.biz API
4. **Analyze transcripts** — Separate Claude call that pre-digests transcripts into structured insights (tone shifts, guidance evolution, recurring analyst concerns)
5. **Build research brief** — One rich Claude call that receives all the data and produces an edge-seeking analysis
6. **Validate brief** — Checks schema makes sense for the financials, driver values are within bounds
7. **Multi-run convergence** — Loads prior runs, anchors new estimates toward historical median, flags outliers
8. **DB setup + orientation** — Creates in-memory SQLite, records observations from the brief
9. **Research plan** — Creates a formal research plan with questions, drivers, kill conditions
10. **Build estimate** — Converts the brief into a ModelSpec, computes forward EPS via DriverDecomposition
11. **Estimate building + claims** — Records formal estimate cases and wires claims to evidence
12. **Adversarial challenge** — Applies bear revisions from the brief, then runs a second independent Claude call that attacks the thesis. Blind spots feed back as contradictions.
13. **Market overlay + edge detection** — Fetches short interest, implied volatility, options positioning. Back-solves consensus to infer what the street assumes per driver. Computes variant decomposition and actionability score.
14. **Valuation** — PE-based implied price with sensitivity range
15. **Decision gate** — 10-criteria assessment producing WORTH_PACKAGING / WORTH_DEEPER_WORK / NOT_ACTIONABLE
16. **Save + export** — Results saved to JSON on disk, Excel workbook auto-generated

Also defines `print_concise()` (one-screen summary) and `print_detail()` (workpaper drill-down).

---

### Data Fetching Layer

**research/financials_fetcher.py** (433 lines)
Unified financial data fetcher. Tries Polygon first, then Alpha Vantage, then merges the best fields from both (Polygon has better EPS/shares data, Alpha Vantage has more granular cost breakdown). Falls back to the company registry cache if both APIs are down. Outputs a `StructuredFinancials` dataclass with normalized income statement, balance sheet, cash flow, and derived percentages.

**research/edgar_text_fetcher.py** (408 lines)
Fetches filing text from SEC EDGAR. Priority order: Exhibit 99.1 press release from an earnings 8-K (richest — has actual financial results, guidance, management commentary), then 8-K shell (Item 2.02), then 10-K MD&A, then 10-Q. Resolves ticker to CIK number via SEC's company_tickers.json. Strips HTML and extracts key sections (results of operations, guidance, comparable sales, margins).

**research/transcript_fetcher.py** (200 lines)
Fetches 3 years of earnings call transcripts via the EarningsCall.biz API. Extracts key sections from each quarter (CEO prepared remarks, financial metrics, guidance items, analyst Q&A). Caps total transcript context at 40K chars for the Claude prompt budget. Transcripts are cached locally by the earningscall library's SQLite backend — each transcript is only fetched from the API once.

**research/transcript_analyzer.py** (250 lines)
A separate Claude call that pre-digests raw transcripts into structured insights BEFORE the main research brief. Extracts: guidance evolution (how guidance changed quarter to quarter), recurring analyst concerns (what keeps getting asked), tone trajectory (improving/deteriorating/stable), key inflection points (when the narrative shifted), and management credibility (do they beat or miss their own guidance). Output is injected into the filing text so the research brain gets organized context instead of raw 50K-char transcripts.

**research/convergence.py** (200 lines)
Loads prior saved research results for a ticker and computes stable estimates. For each driver component, calculates median value and IQR spread across all prior runs. New Claude output is blended 70/30 with the prior median (anchoring). Spread is used as an empirical confidence score — tight spread across many runs means high confidence, wide spread means the system can't make up its mind. Flags outliers when a new run produces values more than 2 standard deviations from the historical mean.

---

### The Research Brain

**research/deep_research.py** (447 lines)
The core research intelligence. Makes one rich Claude API call that receives:
- Structured financials (exact numbers from Polygon/Alpha Vantage)
- Filing text + press release + pre-digested transcript insights
- Consensus EPS and revenue (from yfinance)
- Schema definitions with exact assumption key names

Claude is explicitly told to FIND THE EDGE — where is the market wrong? The prompt asks for:
- Business understanding and economic structure
- Edge hypothesis (specific thesis about what consensus misses)
- Why the market is wrong (citing specific evidence)
- What the street likely assumes per driver
- Guidance vs our view (where we diverge from management)
- Forward drivers with multi-sentence evidence basis per component
- Contradictions found in the data
- Bear revisions for stress testing
- Evidence gaps and confidence assessment

Output is a `ResearchBrief` dataclass that feeds into the estimate engine.

**research/adversarial.py** (419 lines)
Structured adversarial review framework. `ContradictionCapture` records bear-case and bull-case evidence for each key assumption, tracks coverage (every assumption must be challenged), and produces a balanced workpaper. `PostChallengeRevisionLoop` records explicit keep/revise_down/revise_up decisions for each assumption after challenge. The pipeline also runs a second independent Claude call (different persona: "skeptical portfolio manager") that receives the thesis but NOT the original contradictions, so it must independently discover what's wrong.

---

### The Math Layer

**research/estimate_model.py** (619 lines)
The numerical engine. Three layers:
- `DriverComponent`: a sub-component of a driver (e.g., "traffic growth" with value, unit, basis, confidence)
- `Driver`: combines components into a named metric (e.g., "SSS growth = traffic + ticket")
- `DriverDecomposition`: manages the full set of drivers, injects values into ModelSpec, computes sensitivity tables, handles revisions with full trace chains
- `ModelSpec`: schema-driven revenue/cost/EPS computation. Reads a sector driver schema to determine how revenue builds from assumptions, what cost buckets exist, and how below-line costs scale. Computes: revenue, gross profit, operating income, net income, EPS.

Revenue models: SSS + new stores (restaurants), ARR + new bookings (software), franchise royalty, simple growth (general).

**research/sector_drivers.py** (217 lines)
Four pluggable sector schemas that tell the math engine how a business works:
- **Restaurant**: Revenue from existing stores × (1+SSS) + new stores. Cost buckets: food, labor, occupancy, other.
- **Franchise**: Revenue from royalties + ad fund + company-owned + supply chain. Cost buckets: cost of sales, ad expense, SGA.
- **Software**: Revenue from prior ARR × net retention + new ARR. Cost buckets: COGS, S&M, R&D, G&A.
- **General**: Revenue from prior × (1 + growth%). Cost buckets: COGS, OpEx.

---

### Edge Detection

**research/edge_detector.py** (700 lines)
Answers: "is there an actionable edge here?" Five steps:

1. **Consensus back-solve**: For each driver independently, binary-searches for the value that would produce consensus EPS (holding other drivers at neutral). This reveals what the street must assume per driver.
2. **Variant identification**: Compares our driver values to implied consensus values. Ranks by EPS contribution.
3. **Confidence scoring**: Adjusts confidence based on contradictions (serious ones penalize) and evidence density. Uses confidence-squared weighting so high-confidence drivers dominate the score.
4. **Priced-in assessment**: Uses market overlay data (short interest, implied move, put/call ratio) to check if the variant is already reflected in the stock.
5. **Catalyst identification**: Maps drivers to resolution events (next earnings, guidance updates) with actual dates from yfinance. Classifies time horizon: trade (<30 days), swing (30-90), position (90+).

Final verdict: ACTIONABLE_EDGE / PROBABLE_EDGE / POSSIBLE_EDGE / NO_CLEAR_EDGE with an actionability score.

**research/market_overlay.py** (399 lines)
Fetches market structure data from yfinance: current price, short interest, days to cover, implied volatility, put/call ratio, historical volatility, next earnings date. Produces a `SetupAssessment` with one-line setup note and caveats. Feeds into the edge detector's priced-in check. Never drives the thesis — purely a downstream overlay.

**research/valuation.py** (404 lines)
Three valuation methods:
- **PE multiple**: Our EPS × forward PE from yfinance → implied price. Sensitivity: ±3 PE turns.
- **DCF**: WACC build-up (CAPM: Ke = Rf + Beta × ERP), PV of FCFs, terminal value via perpetuity growth and exit multiple.
- **Comparable companies**: Pulls EV/EBITDA, PE, EV/Revenue, growth, margins for peer tickers.

---

### Research Infrastructure (the structured process)

**research/core_workflow.py** (685 lines)
Formal estimate and evidence wiring:
- `EstimateBuilder`: Creates estimate cases (base/bull/bear) with typed assumptions (INDEPENDENT / CONSENSUS_HELD / INFERRED), links drivers, sets outputs with vs_consensus deltas.
- `ClaimBuilder`: Wires evidence → claims → estimate assumptions with full traceability. Each claim has a falsifier.
- `DecisionGate`: 7-criteria assessment: edge specificity, evidence sufficiency, estimate change, claim traceability, falsifier presence, forward-looking, kill conditions.
- `InsiderOverlayAdapter`: Analyzes insider transaction patterns (buying/selling clusters).

**research/deeper_workflow.py** (921 lines)
Extended workflow:
- `RevisionTrackingEstimateBuilder`: Extends EstimateBuilder with automatic revision logging (prior → new value with reason).
- `StrongerDecisionGate`: 10-criteria assessment with is_novel/interesting/valuable/actionable/package_ready flags.
- `FundamentalWorkflow`: Automated margin expansion and guidance divergence workflows.

**research/escalation.py** (800 lines)
Workpaper and escalation management:
- `WorkpaperBuilder`: Creates analyst-visible workpapers stored in the DB with question, methodology, caveats, content.
- `EscalationManager`: Proposes, approves, tracks, and completes analytical escalations.
- Built-in helpers: cadence tables, margin bridges, guidance track records.

**research/baseline_forecast.py** (595 lines)
Historical baseline sanity check. Computes CAGR, linear regression, or seasonal regression from historical data. Compares our estimate and consensus against the trend. Flags if estimate looks "aggressive" or "conservative" vs history.

**research/context/business_understanding.py** (931 lines)
Orientation workflow. Records observations about the business, assesses source breadth (single-source vs cross-source), detects framing shifts (growth → efficiency language), penalizes confidence when evidence is narrow. Produces a business_context record and research candidates.

**research/planning/research_designer.py** (338 lines)
Creates research plans with edge hypothesis, key questions, drivers, workstreams, and kill conditions. Supports manual and AI-assisted plan creation.

**research/planning/plan_gate.py** (197 lines)
Operational gating. Default-deny: operations must be justified by the research plan.

---

### Data and Reference

**research/company_registry.py** (700 lines)
Hardcoded data for 8 calibrated tickers (CMG, WING, DPZ, TXRH, VRSK, NOW, AAOI, RKLB). Each entry has: company info, earnings text, prior-year financials, constants, driver assumptions with basis and confidence, bear revisions, actuals, and consensus. Used as a fallback cache when APIs are unavailable. Also used by the convergence module as a calibration reference.

**research/schema_selection.py** (583 lines)
Scores evidence against 4 candidate schemas (company-operated restaurant, franchise restaurant, SaaS, general). Each candidate has identifying signals and disqualifying signals. Produces a fit level (strong/adequate/weak) with confidence percentage.

**research/schema_builder.py** (195 lines)
Builds custom schemas from Claude observations when none of the 4 standard schemas fit well. Produces a custom driver schema that the ModelSpec can read.

**research/extraction.py** (320 lines)
Legacy extraction module. Sends raw filing text to Claude and returns structured observations. Superseded by deep_research.py for the main flow but kept as a fallback.

---

### Output Layer

**research/export.py** (535 lines)
Excel workbook generator. Creates a formatted 6-tab workbook: Summary (verdict, key metrics, edge hypothesis, consensus assumptions), Drivers (decomposition table), Sensitivity (EPS impact per driver), Adversarial (contradictions, blind spots, revisions), Edge (variant drivers, catalysts, narrative), Valuation (PE sensitivity, implied price).

**research/financial_model.py** (1159 lines)
Full 3-statement quarterly financial model builder. Generates a 7-tab Excel workbook: Summary, Income Statement, Balance Sheet, Cash Flow, Drivers, DCF, Comps. 12 historical quarters + 8 forecast quarters with annual summaries. Scenario toggling (bear/base/bull) via CHOOSE formula architecture. IB-style formatting (blue inputs, black formulas, green cross-references).

**research/deliverables.py** (899 lines)
Hedge-fund-format research outputs:
- **Tear sheet**: 30-second scanning format with headlines, thesis, pillars, scenarios, valuation, kill conditions.
- **Pitch document**: 550-word max prose with recommendation, thesis, catalysts, valuation, risks, rebuttals, and mandatory "Why This Could Be Fake Rigor" section.
- **Talking points**: 3-minute verbal script with opener, insight, math, catalyst, pushback Q&A, close.

Hard validation gates check source lineage, estimate traceability, and consensus reconciliation before generating.

**research/universe.py** (466 lines)
Universe screening with sector-specific composite scoring. Fetches 10+ metrics from yfinance, rank-normalizes within peer group, applies sector-weighted composite score (restaurants weight SSS and margins, SaaS weights Rule of 40 and NRR). Outputs ranked triage table.

**research/pipeline_runner.py** (457 lines)
End-to-end model builder. Fetches 12 quarters of yfinance data, auto-generates bear/base/bull scenarios, builds the 7-tab financial model Excel, saves to disk.

---

### Database Layer

**core/provenance/database.py** (160 lines)
SQLite initialization and provenance tracking. `RunContext` is a context manager that wraps every operation with a run record. `upsert()` handles idempotent inserts with natural key conflict resolution. `new_id()` generates UUIDs. Every fact links to a source document and run.

**core/schemas/canonical_schema.py** (803 lines)
Defines 34 SQLite tables across 10 sections: infrastructure (runs), core entities (company, security), universe/peers, source documents and evidence, research design (plans, questions, drivers, workstreams, kill conditions), time-series (metrics, guidance, consensus), estimates (cases, assumptions, drivers, outputs), capital allocation (insider transactions), claims and thesis, decision/packaging, business understanding, estimate revisions, analytical escalation and workpapers.

**core/schemas/idempotency_rules.py** (241 lines)
Master reference for how each table handles reruns. Defines natural keys, conflict resolution strategy (UPSERT/APPEND/IGNORE), and update columns for all 34 tables.

**core/contracts/output_contracts.py** (310 lines)
Dataclass schemas for all research outputs with validation. Covers: universe shortlist, research plan, estimate summary, thesis, pitch package. Each has a `validate()` method that returns violations.

---

## External Services

| Service | What We Use It For | Authentication | Cost |
|---------|-------------------|----------------|------|
| **Anthropic Claude API** | Research brief (edge-seeking analysis), transcript analysis, independent adversarial review | API key in code | ~$0.03-0.06 per research run (2-3 calls) |
| **Polygon.io** | Structured income statement, balance sheet, cash flow (annual + quarterly, 5yr history) | API key in code | Free tier (5 calls/min) |
| **Alpha Vantage** | Fallback income statement data, company overview | API key in code | Free tier (25 calls/day) |
| **SEC EDGAR** | 8-K press releases (Exhibit 99.1), 10-K/10-Q filings, Form 4 insider transactions, CIK resolution | No auth (public, requires User-Agent header) | Free |
| **EarningsCall.biz** | Earnings call transcripts (3 years quarterly, full prepared remarks + Q&A) | API key in code | $60/month (Starter) |
| **yfinance** | Consensus EPS, forward PE, current price, earnings dates, short interest, implied volatility, put/call ratio, beta, sector/industry | No auth (scrapes Yahoo Finance) | Free |
| **FINRA** | Short interest, dark pool volume | No auth | Free |
| **Motley Fool** | Earnings transcript fallback (free but blocks programmatic access) | None | Free (when accessible) |

---

## Data Flow Diagram

```
                    EXTERNAL DATA SOURCES
                    =====================
    Polygon.io ─────┐
    Alpha Vantage ──┤
    SEC EDGAR ──────┤──→ financials_fetcher.py ──→ StructuredFinancials
    yfinance ───────┤      edgar_text_fetcher.py ──→ Press Release Text
    EarningsCall ───┘      transcript_fetcher.py ──→ 3yr Transcript Text


                    ANALYSIS LAYER
                    ==============
    Transcripts ──→ transcript_analyzer.py ──→ Structured Insights
                         (Claude call #1)           (tone, guidance evolution,
                                                     recurring concerns)
                              │
                              ▼
    All Data ─────→ deep_research.py ──────→ ResearchBrief
                    (Claude call #2)           (edge hypothesis, drivers,
                                               contradictions, bear revisions)
                              │
                              ▼
    Prior Runs ──→ convergence.py ─────────→ Anchored Brief
                   (median blend,               (stabilized driver values,
                    outlier detection)            empirical confidence)


                    ESTIMATE ENGINE
                    ===============
    Brief ────────→ estimate_model.py ─────→ Pre-Challenge EPS
                    sector_drivers.py          (ModelSpec + DriverDecomposition)
                              │
                              ▼
    Brief ────────→ adversarial.py ────────→ Post-Challenge EPS
    Filing Text ──→ pipeline.py                (bear revisions applied,
                    (Claude call #3)            blind spots → contradictions)


                    EDGE DETECTION
                    ==============
    Consensus ────→ edge_detector.py ──────→ EdgeAssessment
    Our EPS ──────→ market_overlay.py          (variant drivers, priced-in,
    Market Data ──→                             catalysts, actionability score)


                    VALUATION
                    =========
    Our EPS ──────→ valuation.py ──────────→ Implied Price + Sensitivity
    Market PE ────→                             (PE-based, DCF, comps)


                    DECISION + OUTPUT
                    =================
    Everything ───→ decision gate ─────────→ Verdict (WORTH_PACKAGING, etc.)
                    core_workflow.py
                    deeper_workflow.py
                              │
                              ▼
                    export.py ─────────────→ Excel Workbook (6 tabs)
                    financial_model.py ────→ 3-Statement Model (7 tabs)
                    deliverables.py ───────→ Tear Sheet + Pitch Doc + Talking Points
                              │
                              ▼
                    data/results/ ─────────→ JSON on disk (for convergence)
                    data/exports/ ─────────→ Excel files
```

---

## Test Coverage

8 test suites, 62 tests total:
- **test_canonical_schema.py** (12 tests): Database tables, provenance, idempotency
- **test_golden.py** (7 tests): Research plan contracts, thesis validation, lineage traces
- **test_research_pipeline.py** (6 tests): Plan creation, validation, versioning, lifecycle
- **test_end_to_end.py** (14 tests): Full pipeline from ticker to decision
- **test_priorities.py** (9 tests): Prioritization logic
- **test_schema_selection.py** (9 tests): Schema scoring, fit confidence
- **test_loaders.py** (3 tests): Polygon, Alpha Vantage, EDGAR loaders
- **test_consensus_loader.py** (2 tests): Yahoo Finance consensus scraping

Test fixtures in `tests/fixtures/sample_data.py` provide golden data for CMG.
