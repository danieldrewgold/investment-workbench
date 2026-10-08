# Call redesign

Goal: the workbench should reach a clear investment call and treat management as a biased source. Built on the `call-redesign` branch and merged into master on 2026-10-07. The pre-change code is tagged `checkpoint-pre-call-redesign`.

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

## Round 2: scenarios from the cost lines

**1. Scenarios are built bottom-up, in code.**
Problem: case EPS was a number the model wrote down, so nothing tied it to the cost lines.
Change:
- `reported_lines.py` reads every earnings release's quarterly income statement and non-GAAP reconciliation, deterministically. It produces food, labor, occupancy, other operating costs, adjusted G&A, D&A, pre-opening, impairment, interest, tax and share count. Each quarter is checked by rebuilding adjusted net income: every 2025 to 2026 CMG quarter rebuilds within 0.5% of the reported figure. Non-GAAP charges booked inside a cost line (a Q1 2026 legal charge in labor) are removed from that line.
- `scenarios.py` builds FY2026E (H1 reported plus H2 from H2 2025) and FY2027 per case. Each cost line is part variable (its ratio moves with inflation versus average check) and part fixed per store (inflation versus same-store sales).
- It computes EPS, targets, expected value, the stance, conviction, where consensus falls, what consensus needs (traffic or margin), and what EPS and multiple the price implies.
- Each case's EPS bridge swaps one driver group at a time, so it always sums to the EPS change.

**2. Driver definitions live in config.** `research/call/schemas/restaurant.json` holds line labels, cost behavior, driver ranges, the base-year bridge and the macro map. A new company type needs a new config, not new code. Names without a config fall back to the earlier flow.

**3. The model proposes inputs; code makes the call.** Opus proposes drivers, multiples and probabilities. Code computes everything and sets the stance from expected value against the 15% hurdle. A second Opus call writes the narrative around the computed numbers and cannot change them. The run fails if the scenario section is missing or does not foot.

**4. "Why the market is wrong" is now "Where we differ from consensus"** (digest, dashboard, Excel). The comparison is against what consensus needs from our model, not a guessed consensus driver.

**5. Claims are judged under their own conditions.** The ledger records the conditions management attached to each forward claim. A claim is refuted or confirmed only when reported figures show those conditions held; otherwise it is "untested".
- CMG: the CFO's Q3 2025 quote is "And then we can return back to that ideal 40% flow through over time as we get back to mid single digit comps and are driving transactions again."
- Comps have run 0.5% and 2.2%, so the claim is untested, not refuted as the earlier version said.

**6. The guidance tracker gives a plain verdict.** "Shrunk bias" is gone. Guides are classed by how far ahead they were given.
- CMG: near-term guides beaten or met 11 of 11. Long-dated guides on items management controls (openings, tax) delivered 6 of 6. Long-dated demand guides missed: FY2025 comps by 5.2pp.
- Open long-dated promises are listed, including "the margin hit from underpricing inflation is temporary and will be recovered".
- Any case that assumes restaurant margin recovers above the base year is flagged as relying on a long-dated promise.

**7. Macro series are mapped to cost lines.** PPI all commodities is no longer used for costs. Each line has its own series:
- food: PPI beef and veal, processed poultry and processed foods, plus retail beef and chicken;
- labor: leisure and hospitality wages;
- occupancy: PPI for nonresidential lessors;
- other operating costs: CPI;
- menu price headroom: CPI food away from home.

CMG's own cost and pricing guides sit next to them.

**8. Cleanup.**
- Em dashes are scrubbed, and the stage refuses to publish if one survives.
- Every evidence point must state its implication, so bare restated numbers fail.
- The reconciliation moved to the appendix.
- Takeover and merger rumors are rejected, including from the appendix's analyst notes.
- Ownership is capped at two lines.

**Tests added:** `tests/test_scenarios.py` (11), registered in `run_tests.py`:
- the parser, add-backs and the rebuild check;
- neutral drivers reproducing the base year;
- every case footing (100 random cases);
- cost-line mechanics;
- the solves, the stance rule and the config's integrity (no PPI all commodities);
- the guidance verdict by horizon;
- the conditions rule;
- the narrative rules;
- the rendered output.

**CMG result (run 2026-10-07 20:27).** NO EDGE, low conviction. Probability-weighted value $33.49, +8.4% vs $30.90; the stance follows from that against the 15% hurdle.

| | Previous output (17:35) | Round 2 (20:27) |
|---|---|---|
| Case EPS | written by the model | computed from food, labor, occupancy and other cost lines: bear $1.06, base $1.29, bull $1.46 (FY2026E $1.11) |
| Consensus $1.37 | not placed | 46% of the way from base to bull; needs +2.9% traffic at base margins, or a 24.37% restaurant margin |
| What the price implies | not stated | $1.14 EPS at our 27x base multiple, or 23.9x our base EPS; the price already discounts most of our 6% shortfall |
| Section title | "The drivers that matter" | "Where we differ from consensus": margin is the gap (23.50% ours vs 24.37% needed), not comps |
| Flow-through | "refuted" | untested: the CFO's 40% claim was conditional on mid-single-digit, transaction-driven comps, which have not happened |
| Guidance | bias numbers ("shrunk +0.08pp") | plain verdict: near-term guides beaten 11 of 11; long-dated demand guides missed (FY2025 comps by 5.2pp); the bull case is flagged as relying on the long-dated margin-recovery promise |
| Macro | "PPI +9.9%" (all commodities) | per cost line: beef +3.1%, poultry -12.5%, restaurant wages +3.9%, nonresidential rents +3.5% |
| Rumors, ownership, reconciliation | Starbucks rumor in the body; long ownership section; reconciliation in the body | rumors removed; ownership in two lines; reconciliation in the appendix |

The full unified diff is in `docs/call-redesign/CMG_digest_round2.diff`. The one-page pitch in `docs/call-redesign/CMG_pitch_example.md` is now the round-4 version.

Notes:
- The pitch runs about 780 words, a little over one page.
- The call stage turns Claude API errors into a clean failure. Failed calls are saved under `data/reports/failed/`, not `data/results`, so the dashboard keeps showing the last good call and its file list stays clean.

## Round 3: the stance prices come from the rule

**1. The price levels in "what would change the stance" are computed.**
Problem: the model wrote that paragraph, and its prices disagreed with the rule that sets the stance. The 20:27 pitch said "long below about $28" and "avoid above $34.85". The rule turns long at $29.12 ($33.49 / 1.15) and avoid above $33.49, where the weighted value falls below the price.
Change:
- `scenarios.stance_by_price` derives the long, avoid and short levels from the same rule as `stance_from`. A test checks the two agree at 12,000 random prices.
- Code writes the price half of the paragraph. The model writes only the data triggers, and validation rejects any share-price level in them.

**2. The narrative prompt defines the provenance tags.** It listed `MC` without saying it means "management claim, unverified". The model read it as "macro", tagged BLS wages and PPI as `MC`, and the self-serving-claim check rejected them twice, failing the run. The prompt now carries the legend, and the error says to tag third-party data `IND`.

**3. Analyst overrides work in the scenario flow.** The digest pointed to `data/overrides/<T>.json`, but only the older flow read it. Probabilities, multiples and individual drivers can now be overridden. The overridden numbers feed the math and the narrative, and the digest lists what was applied. EPS can't be overridden directly; it comes from the drivers.

**4. Re-running just the call.** `rerun_call.py <T>` re-runs the call stage from the cached research, with a fresh price and no new brief. `--narrative-only` keeps the last scenarios and price. A failed narrative now saves its scenario inputs, and `--inputs-from` reuses them, so a retry doesn't pay for that step again.

**5. The one-page pitch drops ledger refs** such as (S89), (G45) and (IND), which mean nothing without the digest.

The round-2 scenario inputs could not be reused. The bear case justified its 22x multiple partly with "takeover speculation" as a floor, which the rumor filter now rejects. Round 3 is a fresh call.

**CMG result (run 2026-10-07 21:26).** NO EDGE, low conviction. Weighted value $32.22, +4.7% vs $30.77.

| | Round 2 (20:27) | Round 3 (21:26) |
|---|---|---|
| Price | $30.90, close 2026-10-06 | $30.77, close 2026-10-07 |
| EPS bear / base / bull | $1.06 / $1.29 / $1.46 | $1.03 / $1.26 / $1.44 |
| Multiples | 22x / 27x / 31x | 22x / 25x / 32x |
| Probabilities | 30% / 50% / 20% | 25% / 55% / 20% |
| Weighted value | $33.49 (+8.4%) | $32.22 (+4.7%) |
| Consensus $1.37 needs | 24.37% restaurant margin | 24.54% restaurant margin, about the bull case's 24.58% |
| Stance prices | model-written: "below about $28", "above $34.85" | computed: long at or below $28.01, avoid above $32.22, short at or above $37.91 |

The stance held. The lower base multiple (25x vs 27x) moved the weighted value more than the price did, a reminder that the multiples and probabilities are model proposals that vary between runs.

**Tests added:** `tests/test_scenarios.py` now has 14:
- the price levels agree with the stance rule;
- the price levels come from code, share prices are rejected in the data triggers, and the tag legend is in the prompt;
- overrides change the inputs, not the math;
- ledger refs are stripped from the pitch.

**Still open:** names without a scenario config use the older flow (`decide.py`), where the model still writes its own price trigger.

## Round 4: reasoning after the math, cost series on the macro page, UI polish

**1. Case reasoning is written after the math.** The case reasoning shown on the dashboard and in the digest used to come from the inputs step, written before code computed anything. It drifted from the results: the CMG base case said comp was the main gap while the computed comparison said margin, and quoted EPS figures off by a cent or two.
- The narrative step, which sees the computed numbers, now writes each case's reasoning. The inputs-step rationale moves to appendix A7, labeled as written before the math.
- Code writes a gap read: whether traffic alone or margin alone can reach consensus inside our case range. For CMG, margin can (24.54% needed, bull case 24.58%) and traffic can't (+3.8% needed, bull case +2.0%). The narrative must follow it.
- Code supplies each case's EPS growth, EPS vs consensus and margin change vs the base year, so the model doesn't do that arithmetic.
- Every dollar figure in the narrative must appear in its inputs or the computed results. Each case's reasoning must state its computed EPS.

**2. The narrative saw the wrong base year.** Its prompt carried the base year built with the default bridge assumptions (CMG restaurant margin 23.04%) as well as the one the scenarios actually used (23.51%). One run called the 23.30% base margin "26bp above" the base year when it is 21bp below. The narrative now sees only the final base year; a test captures the real prompt to check this.

**3. Restaurant cost series on the macro page.** A "Restaurant costs" section is built from the same config the calls use: beef, poultry and processed-food PPI, retail beef and chicken, leisure and hospitality wages, nonresidential rents, and CPI food away from home and at home. A new industry config adds its own section automatically.

**4. Macro exhibit titles are computed from the data.** The titles were hard-coded findings, and several had gone stale: "Inflation is re-accelerating" over a CPI that fell 0.56pp in three months, "The goods cycle has stalled" with durables leading at +4.6%, "The Fed holds" after 47bp of cuts. Each title is now generated from the latest values.

**5. Macro digest.** It runs on Opus 5.5 (it was on Sonnet 4.6), writes short bullets with no em dashes, adds an industry-costs section, and labels PPI all commodities as including energy and metals rather than treating it as food cost inflation. A version stamp regenerates the cached digest once when the prompt changes.

**6. UI.**
- The company header and stats strip show the call's numbers when a call exists (weighted value, base target, our next-year EPS vs consensus). The earlier mechanical edge and valuation fold into a collapsed Diagnostics section.
- "What would change the stance" is a price ladder (long / no edge / avoid / short, with the close and the weighted value marked) plus the data triggers as bullets.
- Runs and files collapse to one line (digest, pitch, Word, Excel) with an expander for the rest. Failed calls save under `data/reports/failed/`.
- Home leads with the screener, which shows the call's numbers for names that have one, then a company-news feed with ownership, insider, legal and rating churn filtered out, capped at 25 items.
- Raw labels (ACTIONABLE_EDGE, implied_price) and run IDs are humanized.
- The pitch strips ledger refs in square brackets too.

`rerun_call.py --render-only` re-renders the latest digest and pitch after a renderer change, with no model call.

**CMG.** Narrative re-run on the round-3 scenarios: the numbers are unchanged (NO EDGE, low conviction, $32.22 weighted value at $30.77). The reasoning now matches them: base margin "slips 20bp to 23.30%", base EPS "grows 14.1% to $1.26", and the gap to consensus is placed on margin.

**Tests:** `tests/test_scenarios.py` now has 18, adding the gap read, sourced dollar figures, post-math case reasoning, the final-base-year prompt, and the ref and basis cleanups.
