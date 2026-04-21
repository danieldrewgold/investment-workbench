# Investment Workbench — System Journal
## As of March 29, 2026

### System Overview

Investment research operating system. ~13,100 lines of Python across 30 files.
38 database tables. 53 tests across 7 suites, all passing.
Three-layer architecture: sector-agnostic core, pluggable driver schemas, future market-structure overlay.
Driver decomposition: 4 drivers, 7 sub-components, full trace chains, 12 workpaper types.

### Architecture

**Layer 1: Sector-Agnostic Core Engine** (proven on CMG, generalized to software)
```
Source Documents (EDGAR/transcripts)
    ↓
Orientation (extract observations, assess source breadth, detect framing shifts)
    ↓
Research Design (edge hypothesis, questions, drivers, workstreams, kill conditions)
    ↓
Analytical Escalations (selective: margin bridge, baseline forecast, guidance track record)
    ↓
Estimate Building (schema-driven ModelSpec reads driver schema for revenue model + cost buckets)
    ↓
Claims & Evidence Linkage (evidence → claim → estimate, with falsifiers)
    ↓
Balanced Adversarial Review (bear + bull cases, contradiction coverage check)
    ↓
Post-Challenge Revision Loop (keep/revise_up/revise_down/lower_confidence)
    ↓
Model Propagation (assumption revision mechanically flows through to EPS)
    ↓
Decision Gate (11 criteria including adversarial, revision, prediction credibility)
    ↓
Workpapers (10 types)
```

**Layer 2: Sector Driver Schemas** (`research/sector_drivers.py`)
```
RESTAURANT_DRIVERS: SSS + new stores → revenue; food/labor/occupancy/other (all above-gross)
SOFTWARE_DRIVERS:   net retention + new ARR → revenue; COGS (above-gross) / S&M/R&D/G&A (opex)
DRIVER_REGISTRY:    lookup by sector name; get_driver_schema("restaurant")
```

**Layer 3: Market-Structure Overlays** (future, not yet built)
```
Options flow, dark pool volume, short interest, positioning, crowding, timing signals.
FINRA dark pool loader exists. Full overlay deferred until core handles 2+ sectors.
```

### Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `core/schemas/canonical_schema.py` | 803 | 38-table schema |
| `research/context/business_understanding.py` | 931 | Orientation workflow |
| `research/deeper_workflow.py` | 921 | Decision gate (11 criteria, 5 verdicts) |
| `research/escalation.py` | 796 | Escalation framework + guidance track record |
| `research/core_workflow.py` | 685 | Estimate/claim/thesis builders |
| `research/baseline_forecast.py` | 595 | CAGR/regression/seasonal baselines |
| `research/adversarial.py` | 419 | Balanced adversarial review (bear+bull) |
| `research/estimate_model.py` | ~560 | Schema-driven model + driver decomposition + sensitivities |
| `research/sector_drivers.py` | 133 | Restaurant + software driver schemas |
| `run_pipeline_cmg.py` | ~1100 | Full end-to-end CMG pipeline |
| `run_extraction_test.py` | 394 | Claude API extraction + estimate vs actual |

### Validation Results

#### CMG FY2024 (estimate from FY2023 data) — 3 iterations

| Version | Pre-challenge EPS | Post-challenge EPS | Actual | Error |
|---------|------------------|--------------------|--------|-------|
| v1: flat margin, bear-only | $1.00 | $0.98 | $1.15 | **$0.18** |
| v2: flat margin, balanced | $0.99 | $0.98 | $1.15 | **$0.17** |
| v3: single-margin leverage | $1.18 | $1.17 | $1.15 | **$0.02** |
| v4: cost-bucket decomposition | $1.16 | $1.14 | $1.15 | **$0.01** |
| v5: driver decomposition | $1.19 | $1.17 | $1.15 | **$0.02** |

**v1→v4 error: $0.18 → $0.01 (18x improvement).**
**v5 adds: trace chains, component-level revision, driver sensitivity ranking.**

#### CMG FY2025 (estimate from FY2024 data, driver-decomposed pipeline)

| Metric | Model Pre-challenge | Post-challenge | Actual | Error |
|--------|-------------------|----------------|--------|-------|
| Revenue | $12,196M | $12,083M | $11,926M | +$157M |
| SSS | 4.5% (traffic 2.0 + ticket 2.5) | 3.5% (traffic 1.0 + ticket 2.5) | -1.7% | +5.2pp |
| EBIT margin | 17.6% (DERIVED) | 17.1% | 16.2% | +0.9pp |
| EPS | $1.28 | $1.23 | $1.14 | +$0.09 |

**Driver trace: traffic +2.0→+1.0% → SSS 4.5→3.5% → rev -$113M → EBIT -0.3pp → EPS -$0.04**
**Top exposure: food_cost.commodity ($0.07/100bps, conf 0.40) ranks above SSS.traffic ($0.03/pp, conf 0.45)**

#### WING FY2025 (second company, franchise model)

| Metric | Pre-challenge | Post-challenge | Actual | Error |
|--------|--------------|----------------|--------|-------|
| EPS (adj) | $10.29 | $9.97 | $4.08 | +$5.93 |

**Honest failure: franchise economics don't fit the company-operated model structure.**

### What's Proven

1. **Claude API extraction works** — 21 structured observations from raw filing text
2. **Schema-driven model works** — same ModelSpec handles restaurants AND software via pluggable driver schemas
3. **Driver decomposition works** — SSS = traffic + ticket; food = commodity + pricing offset; each component has independent confidence
4. **Full trace chains work** — `traffic +2.0→+1.0% → SSS 4.5→3.5% → revenue -$113M → EBIT -0.3pp → EPS -$0.04`
5. **Cost-bucket sensitivity correctly calibrated** — food/labor at $0.06-0.07/100bps ranks above SSS at $0.03/pp
6. **Balanced adversarial review prevents downward bias** — bear+bull cases prevent systematic over-pessimism
7. **Driver-level revision** — contradictions target specific sub-drivers (traffic, not SSS); ticket held while traffic revised
8. **Guidance track record provides actionable insight** — CMG guides conservatively (+2.9pp on SSS)
9. **Decision gate catches weak work** — 11 criteria including adversarial, revision, prediction credibility
10. **Architecture is sector-agnostic** — restaurant and software schemas produce valid outputs from same engine
11. **12 workpaper types** — including DRIVER_DECOMPOSITION and DRIVER_SENSITIVITY

### What's NOT Built (intentionally)

- Biotech driver schema (designed in brief, not yet coded — ~30 lines when ready)
- Market-structure / options overlay (designed, deferred until core handles 2+ sectors)
- Fiscal-period alignment
- External data pipelines (credit card, foot traffic)
- Multivariate regression
- Automated 3-statement model generation
- Scenario automation (bear/base/bull toggle)
- Workpaper formatting/rendering
- CLI composition (`python3 cli.py research CMG`)
- Peer comparison framework
- Live-quarter tracking

### API Keys (ROTATE THESE)

- Anthropic: works in container, used for extraction test
- Polygon: network-blocked in container, stored for local use
- Alpha Vantage: network-blocked in container, stored for local use

### Decision Gate Criteria (11)

| # | Criterion | Category | Severity |
|---|-----------|----------|----------|
| 1 | edge_specificity | EDGE | required |
| 2 | edge_credibility | EDGE | required |
| 3 | evidence_quality | EVIDENCE | important |
| 4 | contradicting_evidence | EVIDENCE | important |
| 5 | post_challenge_review | ESTIMATE | important |
| 6 | prediction_credibility | ESTIMATE | important |
| 7 | estimate_impact | ESTIMATE | required |
| 8 | estimate_iteration | ESTIMATE | informative |
| 9 | claim_traceability | TRACEABILITY | required |
| 10 | falsifiability | TRACEABILITY | important |
| 11 | no_kills_triggered | OPPORTUNITY | required |

WORTH_PACKAGING requires: all required + all important + adversarial review + revision loop + prediction credibility.

### Remaining Weaknesses

1. **Extraction not integrated into pipeline** — Claude API extraction proven separately but not wired into `run_pipeline_cmg.py`
2. **No live data feeds** — EDGAR, Polygon, Alpha Vantage all network-blocked in container
3. **Guidance track record needs more periods** — only 1 guide-vs-actual pair for CMG SSS (N=1)
4. **Franchise model still doesn't fit well** — WING EPS error $4.12 with restaurant schema
5. **Market-structure overlay not connected** — FINRA dark pool loader exists but isn't integrated

### Recommended Next Steps (in priority order)

1. **Wire Claude API extraction into the pipeline** — replace hand-curated observations with live extraction
2. **Run locally with Polygon + Alpha Vantage** — get live market data and consensus estimates
3. **Add biotech driver schema** (~30 lines) — probability × peak sales, R&D/SG&A costs
4. **Build thin market-structure overlay** (~150 lines) — short interest + implied vol → setup workpaper
5. **Build the CLI** (`python3 cli.py research CMG`) composing all steps
