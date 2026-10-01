# Investment Workbench — Project State Summary

*Last updated: 2026-05-04. Use this when starting a fresh Claude session.*

## What this project is

Python pipeline that takes a stock ticker, fetches data from many sources, runs Claude API to produce a research brief, and outputs an institutional-style Word research note + Excel workbook + JSON.

Run via: `python cli.py research TICKER --verbose`

## Architecture in one paragraph

A DAG fetches financials (Polygon + Alpha Vantage), 10-K/10-Q filing text (EDGAR), 12 quarters of earnings transcripts, slide decks (EDGAR + IR pages with browser fallback for Cloudflare), 8 quarters of press releases, sell-side consensus (yfinance), market overlay (short interest / implied move / P/C), macro context (FRED + BLS, optionally BEA), peer comps, recent news (Polygon + Alpha Vantage), and bear-case short research (Fuzzy Panda + Culper + Hindenburg + Wolfpack via direct scraping; Culper / Iceberg / Muddy Waters via DuckDuckGo fallback). All of it is fed into a single Claude call (`build_research_brief`) that produces structured `edge_claims` + a 6-9 paragraph `narrative_synthesis`. EPS is computed via flow-through math from a consensus baseline plus structured edge_claim deltas (`research/pnl_model.py`). Adversarial Claude call (`call_adversarial_claude`) runs a structurally-independent audit. Word renderer puts it all together.

## Key files

| File | Purpose |
|---|---|
| `cli.py` | Entry point. `research`, `feedback`, etc. subcommands. |
| `research/pipeline.py` | Main orchestration. `run_research()` walks DAG outputs into the brief, EPS bridge, adversarial, decision, exports. |
| `research/deep_research.py` | The brief Claude call. `build_research_brief()`, `_build_prompt()`, `_validate_edge_claims()`. The synthesis prompt is the core — most prompt-tuning lives here. |
| `research/pnl_model.py` | EPS bridge: `BaselinePnL`, `compute_our_eps()`, flow-through formulas (revenue/margin/opex/tax/share_count/eps), period filtering, double-count dedup, tiered incremental margin caps. |
| `research/word_report.py` | Word renderer. Sections: Header → Warnings → Street Consensus → Edge (Synthesis lead) → Drivers → Consensus Gap (EPS Bridge) → Catalysts → Risks → Appendix. |
| `research/edge_detector.py` | Mechanical edge / variant back-solve. Now diagnostic-only (EPS bridge is authoritative). |
| `research/news_loader.py` | Polygon + Alpha Vantage news, material-event prioritization, daily cache. |
| `research/short_research_loader.py` | Bear-case scrapers: Fuzzy Panda / Spruce Point / Hindenburg / Wolfpack direct + DuckDuckGo fallback. Weekly cache. |
| `research/guidance_extractor.py` | Aggregates management guidance items from press releases + transcript subagent + deck subagent. |
| `research/consensus_loader.py` | yfinance consensus per period (current/next Q + FY) + revisions + LTG + price target. |
| `research/peer_registry.py` | Schema → peer ticker mapping + yfinance industry inference + ticker-level overrides for misclassifications (BAND/TWLO → cpaas not software). |
| `research/peer_comps.py` | Per-peer consensus fetcher; reuses `consensus_loader`. |
| `research/evidence_audit.py` | Post-brief audit grading driver components against the corpus. **User wants this cleaned up — see Known Issues.** |
| `research/transcript_analyzer.py` | 8-subagent transcript decomposition (guidance / Q&A / tone / QTD / metrics / business / capital / unusual). |
| `research/deck_analyzer.py` | 7-vision-subagent slide-deck decomposition. |
| `ingestion/loaders/fred_macro_loader.py`, `bls_macro_loader.py`, `bea_macro_loader.py` | Macro context loaders. |
| `ingestion/loaders/press_release_loader.py` | EDGAR 8-K Ex 99.1 parsing with table preservation. |
| `ingestion/loaders/slide_deck_loader.py` | EDGAR + IR-page deck fetch with browser fallback. |
| `ingestion/loaders/transcript_loader.py`, `transcript_batch.py` | Earnings call transcripts. |
| `research/dag/steps.py`, `dag/core.py` | DAG infrastructure for parallel fetches. |

## Brief output structure

`ResearchBrief` dataclass:
- `business_description`, `key_debate`, `economic_structure`
- `edge_hypothesis`, `edge_type`, `why_market_is_wrong` (legacy summary fields)
- `narrative_synthesis` — **the lead deliverable**, 6-9 paragraphs of analytical prose
- `edge_claims` — structured quantified disagreements with published anchors
- `rejected_edge_claims` — claims that failed validation (with reasons)
- `consensus_assumptions`, `guidance_vs_our_view`, `drivers`, `contradictions`, `bear_revisions`, `evidence_gaps`

`narrative_synthesis` requires:
1. HARD EDGE — quantified disagreements with consensus
2. SOFT EDGE — beat-and-raise patterns, language drifts, deal-flow momentum, capital allocation behavior
3. Skeptic counter-points — where the bull case is fragile
4. Reconciliation discipline (GAAP vs non-GAAP, normalized vs reported, organic vs reported)
5. Show-your-work diligence — sequential trajectory math with prior-year comparisons, margin bridges, peer-context cites, accounting math

`edge_claims` schema requires:
- `anchor_type` (consensus_fy_eps / consensus_fy_revenue / guidance_q_revenue / etc.)
- `anchor_value` (real published number)
- `our_value` (quantified alternative)
- `line_hit` (revenue / margin / opex / tax / share_count / eps)
- `rationale`, `evidence` (verbatim corpus quotes), `evidence_strength`
- `edge_category` (synthesis / interpretation / non_public_inference / cross_corpus)
- `why_not_consensus` (≥30 chars), `falsifier` (≥25 chars)
- `eps_impact` (Claude's estimate, cross-checked against mechanical flow-through)

## EPS Bridge

`our_EPS = baseline_EPS + Σ flow_through(claim.delta, claim.line_hit, baseline)`

- baseline = consensus current FY EPS (or guidance midpoint when management gives EPS)
- Flow-through formulas in `pnl_model.flow_through()`:
  - revenue: delta × incremental_margin × (1-tax) / shares
  - margin: revenue × delta(pp) × (1-tax) / shares
  - opex: -delta × (1-tax) / shares
  - tax: -pretax_income × delta(pp) / shares
  - share_count: net_income × (1/shares - 1/(shares+delta))
  - eps: delta directly (passthrough)
- Tiered incremental margin cap by op margin: <30% → 50%, 30-60% → 70%, >60% → 85%
- Period-aware filter: claims for non-baseline period skipped (current FY ≠ next FY)
- Double-count dedup: when revenue + EPS claims overlap mechanically, only the more comprehensive one is summed

## Recent commit history (most recent first)

- `4bf4a07` — News loader (Polygon + AV) with material-event prioritization + targeted prompt
- `b314aef` — Bear-research / short-seller research as labeled corpus source
- `64cbcfc` — EPS bridge double-count fix + tiered incremental margin cap
- `6983358` — Diligence-depth synthesis prompt (1500-2500 words, show-your-work patterns)
- `50533fc` — GAAP/non-GAAP reconciliation promoted to CRITICAL RULE #10
- `5da4454` — CPaaS peer override + reconciliation discipline
- `4695976` — Synthesis weaves hard + soft edge into same prose
- `47c6f84` — Narrative synthesis as lead deliverable + period-filtered EPS bridge
- `a616dbb` — Calendar-year period labels (FY26/FY27) on consensus block
- `604f9a7` — Push for 2-3 edge_claims targeting different anchors
- `632d158` — Edge pipeline producing real edge_claims (consensus_full bug fix)
- `fe3c2bb` — Edge pipeline restructure (anchor edge to published consensus + guidance)
- `af9cffd` — EPS rebuild (consensus baseline + structured-delta flow-through)

## Known issues / outstanding fixes (user-flagged 2026-05-04)

1. **Evidence audit display is "ugly and unclear value"** — need to remove from Word doc rendering. Underlying audit code in `research/evidence_audit.py` can stay but the C/I/S marker column and audit warnings shouldn't display.

2. **Q1 sequential framing keeps misfiring** — Sometimes Claude correctly identifies seasonality (e.g. APP Q1 is normally a sequential decline), other times treats it as deceleration. Prompt has SEQUENTIAL TRAJECTORY pattern but execution inconsistent. Real fix: provide structured Q-by-Q historical financials (Polygon paid tier already includes; just need to wire it in) so Claude has actual prior-year quarterly numbers as anchors instead of inferring from YoY references.

3. **Buyback interpretation is backwards** — Brief said heavy buybacks signal "internal skepticism about growth durability." Wrong. Heavy buybacks at depressed valuations = management views shares as undervalued + confidence in cash generation. Need prompt fix to enforce correct framing: heavy buyback at low valuation = bullish; heavy buyback at peak valuation = capital-allocation concern.

4. **Causal claims need evidence** — Brief makes claims like "macro pressures impact ad spend with 1-2 quarter lag" without citing evidence. Prompt should require: any cause-effect or "typically" claims must be either (a) cited from corpus, (b) labeled as speculative inference, or (c) dropped.

5. **Bear thesis engagement is shallow** — Brief mentions Fuzzy Panda / Culper claims and dismisses as "potentially overstated." User wants deeper engagement: confront each specific allegation, evaluate, etc.

6. **Synthesis quantified view sometimes doesn't fill structured edge_claim** — APP synthesis closed with "$7.2B vs Street $8.055B" but `edge_claims = []`. Narrative conviction not translating to structured layer.

## Original gap audit and status

| Gap | Status |
|---|---|
| 1. Bear research / short reports | ✅ Done (`b314aef`) |
| 2. Q-by-Q historical financials from Polygon paid tier | Open — best ROI; partly addresses #2 above |
| 3. News scraper | ✅ Done (`4bf4a07`) |
| 4. Insider Form 4 transactions (deeper than news layer) | Partially via news; could add direct EDGAR Form 4 parsing |
| 5. FMP for richer estimates (operating margin / EBITDA / capex consensus) | Open — needs $14-19/mo subscription; would unlock margin-anchored edge_claims directly |

## Test tickers we've used recently (cache should be warm)

- WING (franchise restaurant)
- RDDT (internet/social)
- TMDX (med devices)
- BAND (CPaaS — verified peer routing fix)
- APP (ad-tech, hot bear-thesis name — verified bear research + news loaders)
- LYV, CMG, TTWO (older runs)

## Reading recent results

- Word docs: `data/reports/{TICKER}_{YYYYMMDD}_{HHMM}.docx`
- JSON: `data/results/{TICKER}_{YYYYMMDD_HHMMSS}.json`
- Excel: `data/exports/{TICKER}_{YYYYMMDD}_{HHMMSS}.xlsx`

Most recent reports as of 2026-05-04:
- `data/reports/APP_20260504_1633.docx` — APP with full bear research + news, material-event prioritization
- `data/reports/BAND_20260504_1526.docx` — BAND with CPaaS routing + GAAP reconciliation + diligence depth

## API keys configured

Per `~/.claude/projects/C--/memory/api_keys.md` — all hardcoded fallbacks in source files (flagged for rotation):
- ANTHROPIC_API_KEY
- POLYGON_API_KEY
- ALPHA_VANTAGE_API_KEY
- ECALL_API_KEY (transcripts)

Optional:
- BEA_API_KEY (free, register at apps.bea.gov/API/signup) — when set, BEA macro context loads
- BLS_API_KEY (free, optional — bumps tier)
- FMP_API_KEY (paid; would unlock richer estimates) — not yet integrated

## Quick smoke test for a fresh session

```bash
# Sanity: all modules import
python -c "
import research.pipeline, research.deep_research, research.pnl_model
import research.news_loader, research.short_research_loader
import research.guidance_extractor, research.peer_registry
print('All imports OK')
"

# Cached re-run on a ticker we have data for
python cli.py research APP --verbose 2>&1 | tail -30
```

## Where the conversation was when context got long

User flagged on the latest APP run:
- Wanted evidence audit removed/cleaned up from Word doc
- Buyback framing was backwards
- "Macro lag" claims need evidence quotes
- Wanted deeper engagement with bear short-report claims
- Asked for Q-by-Q historical financials integration so seasonal math is bulletproof

Next session should focus on:
1. Fix the prompt issues (buyback / evidence / bear depth / seasonality) — discrete prompt edit
2. Remove evidence audit clutter from Word render
3. Build Q-by-Q historical financials loader from Polygon (gap audit #2)
