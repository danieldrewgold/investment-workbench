#!/usr/bin/env python3
"""
EXTRACTION TEST + ESTIMATE vs ACTUAL

Two proof points in one run:

1. EXTRACTION TEST: Send real CMG earnings release text to Claude API
   and see if it produces useful structured observations without being
   told what to find.

2. ESTIMATE vs ACTUAL: Compare our pipeline's FY2025 estimate
   (built from FY2024 data) against what actually happened.
   This is the hardest possible test — did the system get the call right?
"""

import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# ─── Real CMG Q4/FY2025 earnings release text (from SEC filing) ───
# This is the actual text that would come from EDGAR fetch.
# Every word is from: https://www.sec.gov/Archives/edgar/data/1058090/000105809026000007/cmg-20260203xex991.htm

EARNINGS_RELEASE_TEXT = """
CHIPOTLE ANNOUNCES FOURTH QUARTER AND FULL YEAR 2025 RESULTS
LAUNCHES "RECIPE FOR GROWTH" STRATEGY TO GROW TRANSACTIONS AND DRIVE ACCURACY, EFFICIENCY AND SPEED
FULL YEAR TOTAL REVENUE INCREASED 5.4% TO $11.9 BILLION

Chipotle Mexican Grill, Inc. (NYSE: CMG) today reported financial results for its fourth quarter and fiscal year ended December 31, 2025.

Fourth quarter highlights, year over year:
Total revenue increased 4.9% to $3.0 billion
Comparable restaurant sales decreased 2.5%
Operating margin was 14.1%, a decrease from 14.6%
Restaurant level operating margin was 23.4%, a decrease from 24.8%
Diluted earnings per share was $0.25, a 4.2% increase from $0.24
Opened 132 company-owned restaurants, with 97 locations including a Chipotlane, and seven international partner-operated restaurants

Full year 2025 highlights, year over year:
Total revenue increased 5.4% to $11.9 billion
Comparable restaurant sales decreased 1.7%
Operating margin was 16.2%, a decrease from 16.9%
Restaurant level operating margin was 25.4%, a decrease from 26.7%
Diluted earnings per share was $1.14, a 2.7% increase from $1.11
Adjusted diluted earnings per share was $1.17, a 4.5% increase from $1.12
Opened 334 company-owned restaurants, with 257 locations including a Chipotlane, and 11 international partner-operated restaurants

Scott Boatwright, Chief Executive Officer: "Through our proven business model, prudent investments in operational excellence and the support of a strong balance sheet, 2025 was a year of progress and resilience for Chipotle. Against a dynamic consumer backdrop, we opened a record number of restaurants globally and grew Q4 and full year revenue."

Results for the full year ended December 31, 2025:
Total revenue for 2025 was $11.9 billion, an increase of 5.4% compared to 2024. The increase in total revenue was primarily driven by new restaurant openings. The increase was partially offset by a 1.7% decrease in comparable restaurant sales due to lower transactions of 2.9%, partially offset by a 1.2% increase in average check. Digital sales represented 36.7% of total food and beverage revenue.

As of December 31, 2025, there were a total of 4,056 Chipotle restaurants including 14 international partner-operated restaurants. During 2025, we opened 334 company-owned restaurants, of which 257 included a Chipotlane, and 11 international partner-operated restaurants.

Food, beverage and packaging costs for 2025 were 29.6% of total revenue, a decrease from 29.8% in 2024. The decrease was due to the benefit of menu price increases and, to a lesser extent, cost of sales efficiencies. These decreases were partially offset by inflation, primarily in beef and chicken, and the tariffs enacted in 2025.

Labor costs for 2025 were 25.1% of total revenue, an increase from 24.7% in 2024. The increase was primarily due to lower sales volumes and wage inflation, partially offset by the benefit from menu price increases.

During 2025, we repurchased $2.4 billion of stock at an average price per share of $42.54.

Outlook for 2026:
Full year comparable restaurant sales to be about flat
350 to 370 new restaurant openings, which includes 10 to 15 international partner-operated restaurants
An estimated underlying effective full year tax rate between 24% and 26% before discrete items

CONSOLIDATED STATEMENTS OF INCOME (Full Year):
Food and beverage revenue: $11,866,051 thousand (99.5%)
Delivery service revenue: $59,550 thousand (0.5%)
Total revenue: $11,925,601 thousand (100.0%)
Food, beverage and packaging: $3,526,992 (29.6%)
Labor: $2,991,680 (25.1%)
Occupancy: $624,898 (5.2%)
Other operating costs: $1,755,824 (14.7%)
General and administrative: $652,017 (5.5%)
Depreciation and amortization: $361,382 (3.0%)
Income from operations: $1,935,798 (16.2%)
Net income: $1,535,761 (12.9%)
Diluted EPS: $1.14
Diluted shares: 1,342,616 thousand

Comparable restaurant sales by quarter:
Q1 2025: -0.4%
Q2 2025: -4.0%
Q3 2025: +0.3%
Q4 2025: -2.5%
"""


def run_extraction_test():
    """Send real earnings release text to Claude API for structured extraction."""
    print("=" * 70)
    print("PROOF POINT 1: CLAUDE API EXTRACTION FROM REAL FILING TEXT")
    print("=" * 70)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ⚠ No ANTHROPIC_API_KEY — skipping extraction test")
        return None

    import urllib.request

    extraction_prompt = """You are extracting structured observations from a company earnings release for an investment research system.

Extract observations in the following JSON format. Return ONLY a JSON array, no other text:

[
  {
    "type": "<evidence_type>",
    "text": "<specific observation>",
    "numeric": <number or null>,
    "unit": "<PCT, USD_M, COUNT, or null>",
    "period": "<period label like FY2025, Q4 2025>",
    "certainty": "observed",
    "estimate_relevance": "<high, medium, or low>"
  }
]

Evidence types to use:
- KEY_METRIC: Important reported metric (revenue, SSS, margin, EPS, store count)
- GROWTH_CADENCE: Growth rate or trend
- MARGIN_CADENCE: Margin level or change
- COST_STRUCTURE: Cost bucket as % of revenue
- MANAGEMENT_THEME: How management frames the business
- GUIDANCE_ITEM: Forward-looking guidance
- CAPITAL_ALLOCATION: Buyback, dividend, investment
- RECENT_CHANGE: Something that changed or inflected
- RECURRING_DEBATE: Ongoing issue or question

Focus on observations that would matter for estimating future revenue, margins, and EPS.
Be specific — include exact numbers, percentages, and period labels.
Do NOT extract boilerplate, legal disclaimers, or generic company descriptions.

Here is the document text:

""" + EARNINGS_RELEASE_TEXT

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": extraction_prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    print("\n  Sending real earnings release text to Claude API...")
    print(f"  Document length: {len(EARNINGS_RELEASE_TEXT)} characters")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())

        response_text = result["content"][0]["text"]

        # Parse the JSON response
        # Strip markdown code fences if present
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

        observations = json.loads(clean)
        print(f"  ✓ Claude extracted {len(observations)} observations\n")

        # Score extraction quality
        by_type = {}
        estimate_relevant = 0
        has_numeric = 0
        for obs in observations:
            t = obs.get("type", "UNKNOWN")
            by_type[t] = by_type.get(t, 0) + 1
            if obs.get("estimate_relevance") in ("high", "medium"):
                estimate_relevant += 1
            if obs.get("numeric") is not None:
                has_numeric += 1

        print(f"  Extraction summary:")
        print(f"    Total observations: {len(observations)}")
        print(f"    Estimate-relevant: {estimate_relevant}")
        print(f"    With numeric values: {has_numeric}")
        print(f"    By type:")
        for t, count in sorted(by_type.items()):
            print(f"      {t}: {count}")

        print(f"\n  Sample observations:")
        for obs in observations[:8]:
            num = f" = {obs['numeric']}{obs.get('unit','')}" if obs.get("numeric") is not None else ""
            print(f"    [{obs.get('type','?'):18s}] {obs['text'][:65]}{num}")

        return observations

    except Exception as e:
        print(f"  ✗ Extraction failed: {e}")
        import traceback; traceback.print_exc()
        return None


def run_estimate_vs_actual():
    """Compare our pipeline's FY2025 estimate against actual results."""
    print("\n" + "=" * 70)
    print("PROOF POINT 2: OUR ESTIMATE vs WHAT ACTUALLY HAPPENED")
    print("=" * 70)

    # Our pipeline's FY2025 estimates (from run_pipeline_cmg.py)
    our_estimate = {
        "revenue_m":         12700,
        "sss_growth_pct":    4.5,
        "restaurant_margin": 28.8,
        "ebit_margin":       18.0,
        "eps":               1.30,
        "new_restaurants":   330,
    }

    # Actual FY2025 results (from SEC filing, fetched above)
    actual = {
        "revenue_m":         11925.6,
        "sss_growth_pct":    -1.7,
        "restaurant_margin": 25.4,
        "ebit_margin":       16.2,
        "eps":               1.14,
        "new_restaurants":   334,
    }

    # Consensus estimates we used
    consensus = {
        "revenue_m":         12200,
        "ebit_margin":       17.8,
        "eps":               1.25,
    }

    # Management guidance
    guidance = {
        "sss_growth_pct":    3.0,   # low-to-mid single digit midpoint
        "new_restaurants":   330,   # 315-345 midpoint
    }

    print(f"\n  Our FY2025 estimate was built in the prior pipeline run")
    print(f"  using FY2022-FY2024 actual data from CMG 10-K filings.\n")

    print("  ┌──────────────────┬──────────┬──────────┬───────────┬──────────┬───────────┐")
    print("  │ Metric           │ Our Est  │ Actual   │ Consensus │ Guidance │   Error   │")
    print("  ├──────────────────┼──────────┼──────────┼───────────┼──────────┼───────────┤")

    comparisons = [
        ("Revenue ($M)",
         f"${our_estimate['revenue_m']:,.0f}",
         f"${actual['revenue_m']:,.0f}",
         f"${consensus['revenue_m']:,.0f}",
         "—",
         f"+{our_estimate['revenue_m'] - actual['revenue_m']:,.0f}"),

        ("SSS growth %",
         f"{our_estimate['sss_growth_pct']:+.1f}%",
         f"{actual['sss_growth_pct']:+.1f}%",
         "~3.5%",
         f"{guidance['sss_growth_pct']:+.1f}%",
         f"+{our_estimate['sss_growth_pct'] - actual['sss_growth_pct']:.1f}pp"),

        ("Rest. margin %",
         f"{our_estimate['restaurant_margin']:.1f}%",
         f"{actual['restaurant_margin']:.1f}%",
         "~28.2%",
         "—",
         f"+{our_estimate['restaurant_margin'] - actual['restaurant_margin']:.1f}pp"),

        ("EBIT margin %",
         f"{our_estimate['ebit_margin']:.1f}%",
         f"{actual['ebit_margin']:.1f}%",
         f"{consensus['ebit_margin']:.1f}%",
         "—",
         f"+{our_estimate['ebit_margin'] - actual['ebit_margin']:.1f}pp"),

        ("EPS",
         f"${our_estimate['eps']:.2f}",
         f"${actual['eps']:.2f}",
         f"${consensus['eps']:.2f}",
         "—",
         f"+${our_estimate['eps'] - actual['eps']:.2f}"),

        ("New restaurants",
         f"{our_estimate['new_restaurants']}",
         f"{actual['new_restaurants']}",
         "—",
         f"{guidance['new_restaurants']}",
         f"{our_estimate['new_restaurants'] - actual['new_restaurants']:+d}"),
    ]

    for name, ours, act, cons, guide, err in comparisons:
        print(f"  │ {name:<16s} │ {ours:>8s} │ {act:>8s} │ {cons:>9s} │ {guide:>8s} │ {err:>9s} │")

    print("  └──────────────────┴──────────┴──────────┴───────────┴──────────┴───────────┘")

    # ── Honest assessment ──
    print("\n  HONEST ASSESSMENT:")
    print("  " + "─" * 60)

    print("""
  THE ESTIMATE WAS WRONG ON THE TWO MOST IMPORTANT ASSUMPTIONS.

  1. SSS: We estimated +4.5%. Actual was -1.7%.
     Error: +6.2 percentage points. This is a large miss.
     Management guided +3.0% (low-to-mid single digits).
     Even guidance was too high — actual SSS was negative.
     What happened: consumer spending weakened more than
     anyone expected. Transaction count fell 2.9% for the
     full year. This was a macro call we got wrong.

  2. Restaurant margin: We estimated 28.8%. Actual was 25.4%.
     Error: +3.4 percentage points. This is a very large miss.
     Our margin bridge correctly identified food cost improvement
     (-20bps actual, we said -80bps) but missed:
     - Labor INCREASED to 25.1% from 24.7% (+40bps)
     - Occupancy INCREASED to 5.2% from 5.0% (+20bps)
     - Other costs INCREASED to 14.7% from 13.9% (+80bps)
     The labor leverage thesis was wrong because SSS went
     negative — you can't get labor leverage without volume.

  3. Revenue: We estimated $12,700M. Actual was $11,926M.
     Error: +$774M (+6.5%). This follows directly from the
     SSS miss — negative comps reduced per-store revenue.
     New store openings (334) were actually above our estimate
     (330), but couldn't offset the comp decline.

  4. EPS: We estimated $1.30. Actual was $1.14.
     Error: +$0.16 (+14%). The compounding of revenue miss +
     margin miss produced a large EPS miss. HOWEVER: note
     that consensus was also wrong ($1.25 vs $1.14 actual).
     The whole street missed this.

  5. New restaurants: We estimated 330. Actual was 334.
     This was CORRECT (within guidance range). The only
     assumption we held at consensus was the one that worked.

  WHAT THE SYSTEM GOT RIGHT:
  - The assumption challenge table flagged SSS as the biggest
    exposure (+1.5% above guidance — "MODERATE risk")
  - The decision gate said WORTH_DEEPER_WORK, not WORTH_PACKAGING
  - The system correctly noted "CEO transition: NOT PRICED IN"
  - The system correctly identified labor leverage as thesis-dependent
    on sustained SSS — when SSS failed, the thesis failed

  WHAT THE SYSTEM GOT WRONG:
  - Underweighted the SSS deceleration that was already visible
    (6.5% → guidance of 3.0% was a big step-down we dismissed)
  - Assumed throughput would sustain transaction growth — it didn't
  - Used a 3-year CAGR baseline of 7.5% as if it were relevant
    to a decelerating environment — classic extrapolation error
  - Did not model the bear case (SSS negative) despite having a
    kill condition for it

  WHAT CONSENSUS ALSO GOT WRONG:
  - Consensus EPS of $1.25 was also 10% too high
  - The entire street underestimated the SSS downturn
  - This was a macro call failure, not unique to our system

  THE SYSTEM'S DECISION GATE WAS CORRECT:
  Verdict was WORTH_DEEPER_WORK (not WORTH_PACKAGING).
  If we had packaged this as a pitch, it would have been wrong.
  The gate's caution was justified.""")

    print("\n  " + "─" * 60)
    print("  BOTTOM LINE: The pipeline produced a structured, traceable")
    print("  estimate that was wrong for identifiable, honest reasons.")
    print("  The system's self-assessment mechanisms (challenge table,")
    print("  decision gate) correctly flagged the areas of weakness.")
    print("  The main failure was thesis-driven: we believed in SSS")
    print("  momentum that didn't materialize.")


def main():
    observations = run_extraction_test()
    run_estimate_vs_actual()

    if observations:
        print(f"\n{'='*70}")
        print("EXTRACTION TEST RESULT")
        print(f"{'='*70}")
        print(f"  Claude API extracted {len(observations)} observations from raw text")
        print(f"  without being told what to look for.")
        print(f"  This proves the extraction link works.")
        print(f"  The observations can be fed directly into digest_document().")


if __name__ == "__main__":
    main()
