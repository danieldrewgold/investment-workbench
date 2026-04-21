# Migration Plan: analyst-workbench → investment-workbench

## What the old system built (7,234 lines)

The analyst-workbench is an options flow scanner with SEC filing
cross-referencing, dark pool data, and Claude-powered synthesis.
It's organized around a scan → detect → correlate → synthesize pipeline.

The core problem: it produces polished outputs on top of weak evidence
lineage. There's no research design layer, no estimate architecture,
no claim-to-evidence tracing, and no decision gate. The system can't
explain how it reached its conclusions.

## What to preserve

### Source adapters (preserve, wrap in provenance)

These do real work and should be adapted to the new ingestion model:

| Old file | New location | What to keep | What changes |
|----------|-------------|--------------|-------------|
| `fetchers/polygon_fetcher.py` | `ingestion/loaders/polygon_loader.py` | HTTP logic, pagination, rate limit retry, chain reuse | Wrap in RunContext, create source_documents, upsert with natural keys |
| `fetchers/edgar_fetcher.py` | `ingestion/loaders/edgar_loader.py` | CIK resolution, submissions API, Form 4 XML parsing | Already migrated. Form 4 parsing is now in the new loader |
| `fetchers/finra_fetcher.py` | `ingestion/loaders/finra_loader.py` | Dark pool ATS API, short interest API | Already migrated |
| `fetchers/tradier_fetcher.py` | `ingestion/loaders/tradier_loader.py` | Free chain fallback, format normalization | Low priority — only needed if Polygon unavailable |
| `fetchers/earnings_calendar.py` | `ingestion/loaders/consensus_loader.py` | Yahoo scraping, disk cache | Already migrated |

### Detection logic (preserve as derived/signals/, not core)

These are real analytical work but they're *overlay* signals, not the spine:

| Old file | New location | Status |
|----------|-------------|--------|
| `detectors/sweep_detector.py` | `derived/signals/sweep_detector.py` | Preserve as-is. Opening/closing inference, bid/ask sidedness, expiry context are all analytically sound |
| `detectors/options_analytics.py` | `derived/signals/options_analytics.py` | Preserve. GEX with confidence qualifier, skew, implied earnings move |
| `detectors/atm_detector.py` | `derived/signals/atm_detector.py` | Preserve. Uniform lot + VWAP selling + buyback patterns |
| `detectors/signal_correlator.py` | `derived/signals/signal_correlator.py` | Preserve but reframe. Cross-domain correlation is useful but findings should feed evidence_items, not drive synthesis directly |
| `detectors/adaptive_thresholds.py` | `derived/signals/adaptive_thresholds.py` | Preserve. Market-cap-scaled detection is the right approach |
| `detectors/earnings_analyzer.py` | `derived/signals/earnings_analyzer.py` | Preserve. Implied vs realized backtest |
| `detectors/crowding_analyzer.py` | `derived/signals/crowding_analyzer.py` | Low priority. 13F data is quarterly and stale |

### Pitch framework (preserve as packaging template)

| Old file | New location | Notes |
|----------|-------------|-------|
| `skills/hedge-fund-equity-pitch.md` | `packaging/prompts/pitch_framework.md` | Good template. Should only be invoked after Layer 4 decision gate passes |

## What changes architecturally

### Before (analyst-workbench)
```
ticker → parallel fetch → detect → correlate → Claude synthesis → pitch
```
No gates, no plan, no evidence tracing. Everything runs in one pass.

### After (investment-workbench)
```
ticker → company (L1)
      → research plan (L2) — gates downstream work
      → evidence gathering (L3) — all records have provenance
      → claims linked to evidence (L3)
      → estimates built from claims (L3)
      → thesis with decision gate (L4) — valuable vs interesting?
      → packaging (L5) — only if L4 passes
```

### Key structural changes

1. **scanner signals become evidence items**: A sweep detection finding
   becomes an `evidence_item` with `evidence_type='OPTIONS_FLOW'` linked
   to a `source_document` that records when the Polygon chain was pulled.
   It can then be linked to a `claim` via `claim_evidence_link`.

2. **correlation becomes claim construction**: Instead of the correlator
   directly producing a synthesis prompt, it should produce `claim` objects
   with `claim_evidence_link` entries. The synthesis layer can then produce
   output *only if* the claims have sufficient evidence.

3. **research plan gates everything**: Before any detector runs, there
   should be a research plan that says "for this name, flow analysis is
   a relevant workstream because [justification]." If the plan doesn't
   include flow analysis, the detector shouldn't run.

4. **estimates are first-class**: The old system had no estimate objects.
   A pitch said "we think EPS will be X" but there was no `estimate_case`
   → `estimate_assumption` → `estimate_driver` → `estimate_output` chain.
   Now there is.

## What to postpone (not in Phase 1)

- Richer scan summaries and CLI formatting
- More output templates (tear sheet, email, what-changed)
- Advanced market-structure detectors (vanna, charm, 0DTE)
- Real-time alerting or scheduling
- Multi-ticker morning scan automation
- Alternative data integrations (foot traffic, app downloads)
- Claude-powered research plan generation (manual first)
- Web UI

## Migration steps

1. ✅ Canonical schema created (34 tables)
2. ✅ Provenance model implemented
3. ✅ EDGAR, Polygon, FINRA loaders wrapped in provenance
4. ✅ Research designer pipeline built
5. ✅ Output contracts defined
6. ✅ CLI entry point created
7. □ Copy detector files to derived/signals/ (no code changes needed)
8. □ Add adapter layer: detector outputs → evidence_item records
9. □ Add adapter layer: correlator findings → claim objects
10. □ Connect estimate builder to consensus/guidance data
11. □ Build decision gate (Layer 4 operations)
12. □ Connect pitch framework to thesis + decision gate
