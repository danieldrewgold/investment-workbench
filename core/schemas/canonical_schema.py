"""
Canonical Schema for the Investment Research Operating System

34 tables across 10 sections. Every table documents:
- PURPOSE: what it stores and why
- NATURAL KEY: what makes a row unique
- IDEMPOTENCY: how reruns/backfills behave
- LAYER: which product layer it serves (raw/normalized/derived/research/packaging)

Design principles from the product brief:
- Raw ingestion separated from normalized canonical tables
- Derived features separated from raw facts
- Strong natural keys or dedupe strategy for repeat pulls
- Upsert/idempotent loading where possible
- Explicit source metadata and provenance
- Run-level traceability
- Ability to trace a final claim back to source evidence
"""

CANONICAL_SCHEMA = """

-- ═══════════════════════════════════════════════════════════════════
-- SECTION 0: INFRASTRUCTURE
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Every operation that ingests, transforms, or derives data
--   gets a run record. This is the spine of traceability.
-- NATURAL KEY: run_id (UUID)
-- IDEMPOTENCY: Each run is unique. Idempotent reruns create new run records
--   and overwrite/upsert downstream rows tagged with the new run_id.
-- LAYER: Infrastructure
CREATE TABLE IF NOT EXISTS run (
    run_id          TEXT PRIMARY KEY,
    run_type        TEXT NOT NULL,   -- ingest, normalize, derive, analyze, package
    status          TEXT NOT NULL DEFAULT 'running',  -- running, success, failed
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    parameters      TEXT,            -- JSON: what was requested
    error_message   TEXT,
    parent_run_id   TEXT REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 1: CORE ENTITIES
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: The company is the fundamental analytical entity.
--   Separated from security because a company can have multiple securities
--   and because the analytical work is about the business, not the ticker.
-- NATURAL KEY: (cik) for SEC-registered, (ticker) as fallback
-- IDEMPOTENCY: UPSERT on natural key. name/sector/industry update on conflict.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS company (
    company_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    ticker          TEXT,
    cik             TEXT,
    sic_code        TEXT,
    gics_sector     TEXT,
    gics_industry   TEXT,
    market_cap      REAL,
    shares_outstanding REAL,
    description     TEXT,
    headquarters    TEXT,
    fiscal_year_end TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(cik),
    UNIQUE(ticker)
);

-- PURPOSE: A tradeable security. One company may have multiple.
-- NATURAL KEY: (ticker, security_type)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS security (
    security_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    ticker          TEXT NOT NULL,
    security_type   TEXT NOT NULL DEFAULT 'common',
    exchange        TEXT,
    currency        TEXT DEFAULT 'USD',
    is_primary      INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(ticker, security_type)
);

-- PURPOSE: Calendar of reporting periods. Anchors all time-series data.
-- NATURAL KEY: (company_id, period_type, fiscal_year, fiscal_quarter)
-- IDEMPOTENCY: UPSERT. Dates may update as actuals are reported.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS reporting_period (
    period_id       TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    period_type     TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    fiscal_quarter  INTEGER,
    period_start    TEXT,
    period_end      TEXT,
    earnings_date   TEXT,
    earnings_date_confirmed INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(company_id, period_type, fiscal_year, fiscal_quarter)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 2: UNIVERSE, PEERS, COVERAGE
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: A named collection of companies for analysis.
-- NATURAL KEY: (name)
-- IDEMPOTENCY: UPSERT on name.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS universe (
    universe_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    universe_type   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);

-- PURPOSE: Which companies belong to which universes.
-- NATURAL KEY: (universe_id, company_id)
-- IDEMPOTENCY: IGNORE on duplicate.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS universe_membership (
    universe_id     TEXT NOT NULL REFERENCES universe(universe_id),
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    role            TEXT DEFAULT 'member',
    rank            INTEGER,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    added_by_run    TEXT REFERENCES run(run_id),
    rationale       TEXT,
    PRIMARY KEY (universe_id, company_id)
);

-- PURPOSE: Explicit peer relationships between companies.
-- NATURAL KEY: (company_id, peer_company_id, relationship_type)
-- IDEMPOTENCY: UPSERT. Strength may update.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS peer_relationship (
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    peer_company_id TEXT NOT NULL REFERENCES company(company_id),
    relationship_type TEXT NOT NULL,
    strength        REAL,
    rationale       TEXT,
    created_by_run  TEXT REFERENCES run(run_id),
    PRIMARY KEY (company_id, peer_company_id, relationship_type)
);

-- PURPOSE: Track which analyst covers which company.
-- NATURAL KEY: (analyst_name, firm, company_id)
-- IDEMPOTENCY: UPSERT. Rating/target may update.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS coverage_entry (
    coverage_id     TEXT PRIMARY KEY,
    analyst_name    TEXT NOT NULL,
    firm            TEXT NOT NULL,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    rating          TEXT,
    price_target    REAL,
    as_of_date      TEXT,
    source_url      TEXT,
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(analyst_name, firm, company_id)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 3: SOURCE DOCUMENTS AND EVIDENCE
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Every external source that contributes evidence.
--   This is the RAW layer anchor. A filing, transcript, API response,
--   article, or datapoint download.
-- NATURAL KEY: (source_type, source_locator)
-- IDEMPOTENCY: UPSERT on natural key. Content hash detects changes.
-- LAYER: Raw
CREATE TABLE IF NOT EXISTS source_document (
    document_id     TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    source_locator  TEXT NOT NULL,
    source_published_at TEXT,
    fetched_at      TEXT NOT NULL,
    content_hash    TEXT,
    content_summary TEXT,
    raw_content     TEXT,
    company_id      TEXT REFERENCES company(company_id),
    run_id          TEXT NOT NULL REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(source_type, source_locator)
);

-- PURPOSE: A single piece of evidence extracted from a source document.
--   Every finding, datapoint, or fact that could support a claim starts here.
-- NATURAL KEY: (document_id, evidence_type, evidence_key)
-- IDEMPOTENCY: UPSERT on natural key. Value/confidence may update.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS evidence_item (
    evidence_id     TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES source_document(document_id),
    company_id      TEXT REFERENCES company(company_id),
    evidence_type   TEXT NOT NULL,
    evidence_key    TEXT NOT NULL,
    value           TEXT,
    value_numeric   REAL,
    unit            TEXT,
    as_of_date      TEXT,
    confidence      REAL DEFAULT 1.0,
    extraction_method TEXT,
    extraction_version TEXT,
    notes           TEXT,
    run_id          TEXT NOT NULL REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(document_id, evidence_type, evidence_key)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 4: RESEARCH DESIGN
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: The pre-analysis plan. Decides what work is worth doing
--   BEFORE data collection begins. This is Layer 2.
-- NATURAL KEY: (company_id, plan_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS research_plan (
    plan_id         TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    universe_id     TEXT REFERENCES universe(universe_id),
    plan_version    INTEGER NOT NULL DEFAULT 1,
    status          TEXT DEFAULT 'draft',
    edge_type       TEXT,
    edge_hypothesis TEXT,
    why_now         TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(company_id, plan_version)
);

-- PURPOSE: Specific questions the research plan needs to answer.
-- NATURAL KEY: (plan_id, question_text)
-- IDEMPOTENCY: IGNORE on duplicate question for same plan.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS research_question (
    question_id     TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES research_plan(plan_id),
    question_text   TEXT NOT NULL,
    priority        TEXT DEFAULT 'medium',
    status          TEXT DEFAULT 'open',
    answer_summary  TEXT,
    answer_confidence REAL,
    affects         TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- PURPOSE: Key financial/operational drivers where edge lives.
-- NATURAL KEY: (plan_id, driver_name)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS key_driver (
    driver_id       TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES research_plan(plan_id),
    driver_name     TEXT NOT NULL,
    driver_category TEXT,
    importance      TEXT DEFAULT 'high',
    transmission    TEXT,
    current_consensus TEXT,
    independent_view TEXT,
    UNIQUE(plan_id, driver_name)
);

-- PURPOSE: Analytical workstreams justified by the research plan.
-- NATURAL KEY: (plan_id, workstream_name)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS workstream (
    workstream_id   TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES research_plan(plan_id),
    workstream_name TEXT NOT NULL,
    analysis_type   TEXT,
    status          TEXT DEFAULT 'planned',
    priority        TEXT DEFAULT 'medium',
    justification   TEXT,
    expected_output TEXT,
    UNIQUE(plan_id, workstream_name)
);

-- PURPOSE: Conditions that would kill the idea early.
-- NATURAL KEY: (plan_id, condition_text)
-- IDEMPOTENCY: IGNORE on duplicate.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS kill_condition (
    kill_id         TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES research_plan(plan_id),
    condition_text  TEXT NOT NULL,
    status          TEXT DEFAULT 'untested',
    tested_at       TEXT,
    result_notes    TEXT
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 5: TIME-SERIES DATA
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Registry of all metrics tracked.
-- NATURAL KEY: (metric_name, metric_source)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS metric_definition (
    metric_id       TEXT PRIMARY KEY,
    metric_name     TEXT NOT NULL,
    metric_source   TEXT NOT NULL,
    unit            TEXT,
    frequency       TEXT,
    description     TEXT,
    UNIQUE(metric_name, metric_source)
);

-- PURPOSE: Time-series of company-level metrics (reported actuals, KPIs).
-- NATURAL KEY: (company_id, metric_id, period_id, source_document_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS company_metric_series (
    series_id       TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    metric_id       TEXT NOT NULL REFERENCES metric_definition(metric_id),
    period_id       TEXT REFERENCES reporting_period(period_id),
    as_of_date      TEXT,
    value           REAL,
    value_text      TEXT,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(company_id, metric_id, period_id, source_document_id)
);

-- PURPOSE: External data series not tied to a single company.
-- NATURAL KEY: (metric_id, as_of_date, source_document_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS external_metric_series (
    series_id       TEXT PRIMARY KEY,
    metric_id       TEXT NOT NULL REFERENCES metric_definition(metric_id),
    as_of_date      TEXT NOT NULL,
    value           REAL,
    value_text      TEXT,
    geography       TEXT,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(metric_id, as_of_date, source_document_id)
);

-- PURPOSE: Management guidance by period and metric.
-- NATURAL KEY: (company_id, metric_id, period_id, guidance_type, source_document_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS guidance_point (
    guidance_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    metric_id       TEXT NOT NULL REFERENCES metric_definition(metric_id),
    period_id       TEXT NOT NULL REFERENCES reporting_period(period_id),
    guidance_type   TEXT NOT NULL,
    value_low       REAL,
    value_high      REAL,
    value_point     REAL,
    guidance_date   TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(company_id, metric_id, period_id, guidance_type, source_document_id)
);

-- PURPOSE: Point-in-time consensus snapshots.
-- NATURAL KEY: (company_id, metric_id, period_id, as_of_date, source_name)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS consensus_snapshot (
    snapshot_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    metric_id       TEXT NOT NULL REFERENCES metric_definition(metric_id),
    period_id       TEXT NOT NULL REFERENCES reporting_period(period_id),
    as_of_date      TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    estimate_mean   REAL,
    estimate_median REAL,
    estimate_high   REAL,
    estimate_low    REAL,
    num_analysts    INTEGER,
    run_id          TEXT REFERENCES run(run_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(company_id, metric_id, period_id, as_of_date, source_name)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 6: ESTIMATE ARCHITECTURE
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: A scenario-specific estimate for a company.
-- NATURAL KEY: (company_id, case_name, case_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS estimate_case (
    case_id         TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    plan_id         TEXT REFERENCES research_plan(plan_id),
    case_name       TEXT NOT NULL,
    case_version    INTEGER NOT NULL DEFAULT 1,
    scenario_weight REAL,
    summary         TEXT,
    target_price    REAL,
    target_multiple TEXT,
    target_method   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(company_id, case_name, case_version)
);

-- PURPOSE: An explicit assumption within an estimate case.
-- NATURAL KEY: (case_id, assumption_key)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS estimate_assumption (
    assumption_id   TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES estimate_case(case_id),
    assumption_key  TEXT NOT NULL,
    assumption_value REAL,
    assumption_text TEXT,
    basis           TEXT,
    confidence      REAL,
    evidence_id     TEXT REFERENCES evidence_item(evidence_id),
    UNIQUE(case_id, assumption_key)
);

-- PURPOSE: Links a key driver to a specific estimate case.
-- NATURAL KEY: (case_id, driver_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS estimate_driver (
    case_id         TEXT NOT NULL REFERENCES estimate_case(case_id),
    driver_id       TEXT NOT NULL REFERENCES key_driver(driver_id),
    driver_value    REAL,
    driver_impact   TEXT,
    sensitivity     TEXT,
    PRIMARY KEY (case_id, driver_id)
);

-- PURPOSE: Output line items from an estimate case.
-- NATURAL KEY: (case_id, period_id, line_item)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS estimate_output (
    output_id       TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES estimate_case(case_id),
    period_id       TEXT NOT NULL REFERENCES reporting_period(period_id),
    line_item       TEXT NOT NULL,
    value           REAL,
    vs_consensus    REAL,
    vs_guidance_mid REAL,
    notes           TEXT,
    UNIQUE(case_id, period_id, line_item)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 7: CAPITAL ALLOCATION
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Track capital allocation actions.
-- NATURAL KEY: (company_id, action_type, action_date, source_document_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS capital_action (
    action_id       TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    action_type     TEXT NOT NULL,
    action_date     TEXT NOT NULL,
    amount          REAL,
    shares          REAL,
    description     TEXT,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    UNIQUE(company_id, action_type, action_date, source_document_id)
);

-- PURPOSE: Point-in-time share count snapshots.
-- NATURAL KEY: (company_id, as_of_date, count_type)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS share_count_snapshot (
    snapshot_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    as_of_date      TEXT NOT NULL,
    count_type      TEXT NOT NULL,
    share_count     REAL NOT NULL,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    UNIQUE(company_id, as_of_date, count_type)
);

-- PURPOSE: Insider transactions from parsed Form 4 XML.
-- NATURAL KEY: (company_id, insider_name, transaction_date, transaction_code, shares)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Normalized
CREATE TABLE IF NOT EXISTS insider_transaction (
    transaction_id  TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    insider_name    TEXT NOT NULL,
    insider_title   TEXT,
    transaction_date TEXT NOT NULL,
    transaction_code TEXT NOT NULL,
    transaction_type TEXT,
    shares          REAL NOT NULL,
    price           REAL,
    value           REAL,
    ownership_after REAL,
    source_document_id TEXT REFERENCES source_document(document_id),
    run_id          TEXT REFERENCES run(run_id),
    UNIQUE(company_id, insider_name, transaction_date, transaction_code, shares)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 8: CLAIMS AND THESIS
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: A versioned investment view on a company.
-- NATURAL KEY: (company_id, thesis_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS thesis (
    thesis_id       TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    plan_id         TEXT REFERENCES research_plan(plan_id),
    thesis_version  INTEGER NOT NULL DEFAULT 1,
    direction       TEXT,
    conviction      TEXT,
    one_liner       TEXT,
    edge_source     TEXT,
    why_exists      TEXT,
    key_evidence    TEXT,
    key_risks       TEXT,
    missing_evidence TEXT,
    what_would_change TEXT,
    is_worth_sharing INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(company_id, thesis_version)
);

-- PURPOSE: Track what changed between thesis versions.
-- NATURAL KEY: (thesis_id, prior_thesis_id)
-- IDEMPOTENCY: IGNORE on duplicate.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS thesis_revision (
    revision_id     TEXT PRIMARY KEY,
    thesis_id       TEXT NOT NULL REFERENCES thesis(thesis_id),
    prior_thesis_id TEXT REFERENCES thesis(thesis_id),
    what_changed    TEXT NOT NULL,
    change_trigger  TEXT,
    conviction_change TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- PURPOSE: A specific analytical claim. The atomic unit of "show your work."
-- NATURAL KEY: (company_id, claim_text, claim_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS claim (
    claim_id        TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES company(company_id),
    thesis_id       TEXT REFERENCES thesis(thesis_id),
    plan_id         TEXT REFERENCES research_plan(plan_id),
    claim_text      TEXT NOT NULL,
    claim_type      TEXT,
    affects         TEXT,
    confidence      REAL,
    falsifier       TEXT,
    status          TEXT DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);

-- PURPOSE: Links claims to supporting or contradicting evidence.
-- NATURAL KEY: (claim_id, evidence_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS claim_evidence_link (
    claim_id        TEXT NOT NULL REFERENCES claim(claim_id),
    evidence_id     TEXT NOT NULL REFERENCES evidence_item(evidence_id),
    role            TEXT NOT NULL,
    importance      REAL DEFAULT 1.0,
    rationale       TEXT,
    PRIMARY KEY (claim_id, evidence_id)
);

-- PURPOSE: Links claims to estimate assumptions they affect.
-- NATURAL KEY: (claim_id, assumption_id)
-- IDEMPOTENCY: UPSERT on natural key.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS claim_estimate_link (
    claim_id        TEXT NOT NULL REFERENCES claim(claim_id),
    assumption_id   TEXT NOT NULL REFERENCES estimate_assumption(assumption_id),
    impact_direction TEXT,
    impact_magnitude TEXT,
    rationale       TEXT,
    PRIMARY KEY (claim_id, assumption_id)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 9: DECISION LAYER
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Structured assessment of whether an idea passes the bar.
-- NATURAL KEY: (thesis_id)
-- IDEMPOTENCY: UPSERT on thesis_id.
-- LAYER: Research
CREATE TABLE IF NOT EXISTS decision_assessment (
    assessment_id   TEXT PRIMARY KEY,
    thesis_id       TEXT NOT NULL UNIQUE REFERENCES thesis(thesis_id),
    edge_is_real    INTEGER,
    edge_is_valuable INTEGER,
    why_exists_still TEXT,
    strongest_evidence TEXT,
    weakest_link    TEXT,
    transmission_clear INTEGER,
    bear_case       TEXT,
    falsifier       TEXT,
    asymmetry_credible INTEGER,
    recommendation  TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 10: PACKAGING
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Record of packaged outputs (pitches, memos, emails).
-- NATURAL KEY: (thesis_id, output_type, output_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Packaging
CREATE TABLE IF NOT EXISTS packaged_output (
    output_id       TEXT PRIMARY KEY,
    thesis_id       TEXT NOT NULL REFERENCES thesis(thesis_id),
    output_type     TEXT NOT NULL,
    output_version  INTEGER NOT NULL DEFAULT 1,
    content         TEXT,
    evidence_ids    TEXT,
    claim_ids       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(thesis_id, output_type, output_version)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 11: BUSINESS UNDERSTANDING (Priority 1)
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Structured output of the "get smart on the company" phase.
--   Sits BEFORE research design. You earn the right to define an edge
--   only after you understand the business.
-- NATURAL KEY: (company_id, context_version)
-- IDEMPOTENCY: APPEND with incrementing version.
-- LAYER: Research (pre-design)
CREATE TABLE IF NOT EXISTS business_context (
    context_id      TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    context_version INTEGER NOT NULL DEFAULT 1,
    status          TEXT DEFAULT 'in_progress',  -- in_progress, complete, superseded
    business_description TEXT,          -- what the company does, in analyst language
    revenue_model   TEXT,               -- how it makes money
    segment_map     TEXT,               -- JSON: segments, mix, growth profiles
    geographic_map  TEXT,               -- JSON: geo exposure
    key_metrics     TEXT,               -- JSON: what management and the street focus on
    historical_cadence TEXT,            -- what has mattered: SSS trends, margin cycles, etc.
    recurring_debates TEXT,             -- what the bull/bear arguments have been
    management_framing TEXT,            -- how management talks about the business
    capital_allocation_pattern TEXT,    -- buyback, dividend, M&A history
    candidate_edges TEXT,               -- JSON: possible areas where edge might live
    candidate_model_structure TEXT,     -- what kind of model fits this business
    readiness_for_design TEXT,          -- honest assessment: ready to define edge, or not yet
    sources_reviewed TEXT,              -- JSON: list of source_document_ids reviewed
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id),
    UNIQUE(company_id, context_version)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 12: ESTIMATE REVISION LOG (Priority 2)
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Track every change to an estimate assumption or output.
--   The system should explain not just the estimate, but its evolution.
-- NATURAL KEY: (revision_id) — each change is unique
-- IDEMPOTENCY: APPEND only.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS estimate_revision (
    revision_id     TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES estimate_case(case_id),
    revision_type   TEXT NOT NULL,
    field_key       TEXT NOT NULL,
    prior_value     REAL,
    new_value       REAL,
    change_amount   REAL,
    reason          TEXT,
    linked_claim_id TEXT REFERENCES claim(claim_id),
    linked_evidence_id TEXT REFERENCES evidence_item(evidence_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);


-- ═══════════════════════════════════════════════════════════════════
-- SECTION 13: ANALYTICAL ESCALATION
-- ═══════════════════════════════════════════════════════════════════

-- PURPOSE: Records a justified request to do heavier analytical work.
--   Default research is lightweight. Escalations are explicit, question-driven,
--   and must state what estimate/claim/decision they serve.
-- NATURAL KEY: (escalation_id)
-- IDEMPOTENCY: APPEND only.
-- LAYER: Research Design
CREATE TABLE IF NOT EXISTS analytical_escalation (
    escalation_id   TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES research_plan(plan_id),
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    escalation_type TEXT NOT NULL,       -- EXTERNAL_DATA, FISCAL_ALIGNMENT, KPI_MAPPING,
                                         -- BRIDGE_ANALYSIS, REGRESSION, COST_INPUT,
                                         -- CAPITAL_ALLOCATION, SEGMENT_BUILD
    question        TEXT NOT NULL,       -- the research question this answers
    rationale       TEXT NOT NULL,       -- why this escalation is justified
    data_needed     TEXT,                -- what data is required
    transformation  TEXT,                -- what alignment/transformation is needed
    affects         TEXT,                -- what estimate/claim/decision this serves
    status          TEXT DEFAULT 'proposed',  -- proposed, approved, in_progress, completed, skipped
    priority        TEXT DEFAULT 'medium',
    resolution      TEXT,                -- how data was obtained (fetched/stored/user/degraded)
    result_summary  TEXT,                -- what the escalation found
    workpaper_id    TEXT,                -- link to produced workpaper
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);

-- PURPOSE: Analyst-visible workpapers produced by escalated analysis.
--   These are inspectable artifacts: aligned tables, bridges, regressions,
--   KPI tables, assumption-change tables.
-- NATURAL KEY: (workpaper_id)
-- IDEMPOTENCY: APPEND only.
-- LAYER: Derived
CREATE TABLE IF NOT EXISTS workpaper (
    workpaper_id    TEXT PRIMARY KEY,
    escalation_id   TEXT REFERENCES analytical_escalation(escalation_id),
    company_id      TEXT NOT NULL REFERENCES company(company_id),
    workpaper_type  TEXT NOT NULL,       -- TIME_SERIES_TABLE, BRIDGE_TABLE, REGRESSION_SUMMARY,
                                         -- KPI_DRIVER_TABLE, ASSUMPTION_CHANGE_TABLE,
                                         -- FISCAL_ALIGNMENT_TABLE, CADENCE_TABLE
    title           TEXT NOT NULL,
    question        TEXT,                -- the question this workpaper answers
    content         TEXT NOT NULL,       -- JSON: the actual workpaper data
    methodology     TEXT,                -- how data was aligned/transformed
    caveats         TEXT,                -- approximation limits, data gaps
    source_data     TEXT,                -- JSON: list of source_document_ids or evidence_ids used
    affects         TEXT,                -- what estimate/claim this supports
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by_run  TEXT REFERENCES run(run_id)
);

"""

TABLE_INDEX = {
    "Infrastructure": ["run"],
    "Core entities": ["company", "security", "reporting_period"],
    "Universe/peers": ["universe", "universe_membership", "peer_relationship", "coverage_entry"],
    "Source/evidence": ["source_document", "evidence_item"],
    "Research design": ["research_plan", "research_question", "key_driver", "workstream", "kill_condition"],
    "Time-series": ["metric_definition", "company_metric_series", "external_metric_series", "guidance_point", "consensus_snapshot"],
    "Estimates": ["estimate_case", "estimate_assumption", "estimate_driver", "estimate_output"],
    "Capital allocation": ["capital_action", "share_count_snapshot", "insider_transaction"],
    "Claims/thesis": ["thesis", "thesis_revision", "claim", "claim_evidence_link", "claim_estimate_link"],
    "Decision/packaging": ["decision_assessment", "packaged_output"],
    "Business understanding": ["business_context"],
    "Estimate revisions": ["estimate_revision"],
    "Analytical escalation": ["analytical_escalation", "workpaper"],
}

TOTAL_TABLES = sum(len(v) for v in TABLE_INDEX.values())
assert TOTAL_TABLES == 38, f"Expected 38 tables, got {TOTAL_TABLES}"
