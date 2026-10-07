# Call redesign (branch `call-redesign`)

Goal: the workbench should reach a clear investment call and treat management as a biased source. main is untouched; the pre-change code is tagged `checkpoint-pre-call-redesign`.

## Problems, causes, changes

**1. Output ended without a recommendation.**
Cause: the brief prompt framed the job as "build a forward earnings estimate". Its output format had no stance, target, scenarios or probabilities. It never saw the stock price. The five tests and the guidance rules pushed weak theses toward "in line", and the narrative had to end on the bear case. The valuation step could only restate the EPS gap. The labels downstream (edge score, decision gate) measured research completeness, not a view.
Change: a new call stage (`research/call/decide.py`) runs after the analysis. Opus 5.5 reads the brief, the red team, the guidance track record, the management ledger and the multiple evidence, and must return:
- a stance (long, short, avoid, or no edge with a specific trigger) and a conviction;
- a one-sentence thesis;
- our numbers vs consensus on the one or two drivers that matter;
- a verdict on the multiple (fair, high or low; compress, hold or expand);
- bull, base and bear cases with probabilities and reasoning;
- dated catalysts and measurable kill criteria.

Code computes the targets and expected value, and checks that the stance agrees with expected value against a 15% hurdle. The model gets one repair round; after that the run fails. Pair trades are not offered.

**2. The price was never real.**
Cause: no price reached the brief, so it inferred one from an insider sale.
Change: a `live_price` DAG step takes the last completed session close from Polygon, with its date. The run fails if the price is missing or more than one session old (NYSE calendar), before the brief is paid for.

**3. Management commentary was treated as evidence.**
Cause: about 82% of the brief's corpus was management-authored. The prompt counted a transcript quote as "cited", its strongest grade, and said to anchor on guidance.
Change: a `management_ledger` DAG step classifies management statements from the last 4 calls as (a) reported fact, (b) guidance, (c) action backed by money, (d) statement against interest or (e) self-serving narrative.
- An excuse stays unverified until a later reported figure confirms it, and code downgrades any "confirmed" that doesn't cite one.
- Category (e) cannot support the stance or a driver without independent data; code enforces this.
- Dodged questions, dropped metrics and tone shifts come from the existing transcript sub-analyses, all of which are kept.
- The brief prompt now says to use bias-adjusted guidance, and that management narrative is not "cited".

**4. Nothing tracked guidance against results.**
Cause: the transcript tracker reconstructed "actuals" from management's own words and stored nothing. A proper table and function existed but were never called.
Change: `research/call/guidance_ledger.py` captures guidance from press releases and from calls, takes actuals only from reported press-release figures, and computes the bias per metric (first guide per period, shrunk by n/(n+2)). Current guidance is adjusted by that bias. The ledger persists at `data/guidance_ledger/<T>.json`, and its live guidance also counts as a published anchor for estimate claims. CMG examples: tax rate over-guided 3 for 3 (adjusted 23.7% vs 25% guided); FY2025 comps missed by 5.2pp.

**5. Valuation.**
Change: no DCF and no peer fair value. `valuation_pack.py` shows where the multiple sits: CMG's split-adjusted P/E history next to the EPS growth the market paid for each year, with peers as context only. The call must judge the multiple from that, plus revisions, margins and credibility. Overrides for probabilities, multiples or EPS go in `data/overrides/<T>.json`.

**6. Margin bridges didn't foot (Step 5).**
Cause: the model did bridge arithmetic in prose. The old CMG FY27 bridge summed to +10bp but stated +35bp, from an ambiguous base, and treated a 40% flow-through as a 40bp margin gain.
Change: bridges are inputs, and code computes them (`bridges.py`):
- traffic leverage = (flow-through minus current margin) x revenue change / (1 + change);
- price = (1 - m) p / (1 + p), at most once per bridge;
- one cost-inflation component per bucket, with the buckets covering all costs;
- end = start + sum of components;
- the bridge must land on the call's own margin estimate.

A first version double-counted price; the CMG run exposed it and it is fixed.

**7. Smaller fixes.**
- No em dashes in any narrative output (scrubbed and tested).
- Failed calls are saved for review.
- Results are saved as UTF-8; before, encoding errors were silently swallowed.
- The dashboard shows a "The call" panel. When a call exists, the stance replaces the old edge and decision labels in the header and screener.
- Digest and pitch files open inline.

## What moved to the appendix (not deleted)
Business description, the EPS base reconciliation, ownership flows, macro color, M&A rumors, claim-verifier results, the mechanical model EPS, the back-solve, the edge score, the decision-gate label, and the long analyst essay. None of these change an estimate, a probability or the stance. The full digest keeps provenance tags; the one-page pitch has none.

## CMG, old vs new

| | Old (13:48 run) | New (17:35 run) |
|---|---|---|
| Ends with | "estimate cut, not a short" | NO EDGE, low conviction; long below about $27.80 or on Q3 labor and margin data; short above about $37.60 or on negative transactions |
| Price | inferred "low $30s" | $30.90, close 2026-10-06 |
| Valuation | our EPS x market multiple, -5.7% | judgment: 22.6x FY27 looks fair, likely to hold, read against 60x for +32% growth (2024) and 31x for flat EPS (2026) |
| Scenarios | none | bull $41.47 (25%), base $31.44 (50%), bear $23.60 (25%); EV $31.99, +3.5% |
| Management | guidance taken at face value | 144 statements classified; the 40% flow-through and mid-single-digit comp promises refuted by reported results; buybacks weighted as money |
| FY27 margin bridge | prose, did not foot | computed: 23.90% to 23.78% (-12bp), matches the estimate |

Runs vary a few points: an earlier run on the same inputs gave EV +7.7% at medium conviction, with the same stance.

## Tests
New suite `tests/test_call_layer.py` (21 tests), registered in `run_tests.py`:
- every bridge foots;
- traffic-leverage formula;
- price and cost buckets are exact; price counted once; costs covered;
- the bridge lands on the estimate;
- stale prices fail; session calendar;
- guidance bias math;
- call rules (pair rejected, EV vs stance, probabilities, ordering, catalysts, kill criteria, one-sentence thesis, multiple view);
- self-serving claims can't carry the call;
- excuse verification;
- the split-history guard;
- no em dashes, and the pitch has no tags.

Everything that passed before still passes. The one prior failure (database duplicate rules for 13F and bond tables) is unchanged.

## Known gaps
- The transcript digest still reaches only about the last 4 calls.
- The probabilities are model proposals, not calibrated.
- Dashboard verification covered the CMG page and the main pages only.
