"""
Deep Research — Single Rich Claude API Call

This module makes ONE Claude API call that does qualitative reasoning:
  - Business understanding (what does this company do, how does it make money)
  - Schema reasoning (which economic structure fits)
  - Driver selection (what 2-4 drivers matter most for the forward estimate)
  - Assumption setting with basis (citing specific evidence from the filing)
  - Contradiction generation (real tensions in the data)
  - Bear revision design (how to stress-test)

IMPORTANT: Claude does NOT extract numbers from text. It receives
structured financials from Polygon/Alpha Vantage as input.
The numbers are already known. Claude's job is to reason about
what drives them forward.

The output is a ResearchBrief dataclass that the pipeline feeds
directly into the estimate engine.
"""

from __future__ import annotations

import os
import json
import httpx
from dataclasses import dataclass, field
from research.financials_fetcher import StructuredFinancials


ANTHROPIC_API_KEY = (
    os.environ.get("ANTHROPIC_API_KEY", "")
    or "***KEY-REMOVED-FROM-HISTORY***"
)
MODEL = "claude-sonnet-4-20250514"


@dataclass
class ResearchBrief:
    """Contract between upstream thinking and downstream modeling."""
    # Business understanding
    company_name: str = ""
    business_description: str = ""
    economic_structure: str = ""
    key_debate: str = ""

    # Edge hypothesis (the core of the research)
    edge_hypothesis: str = ""           # "Street underestimates SSS because..."
    edge_type: str = ""                 # EXPECTATION_GAP / VALUATION_GAP / QUALITY_GAP / DURATION_GAP
    why_market_is_wrong: str = ""       # specific reasoning about the mispricing
    consensus_assumptions: dict = field(default_factory=dict)
    # {driver_name: "what street likely assumes and why"}
    guidance_vs_our_view: dict = field(default_factory=dict)
    # {driver_name: "guidance says X, we think Y because Z"}

    # Schema
    schema_type: str = ""               # "restaurant", "franchise", "software", "general"
    schema_reasoning: str = ""

    # Drivers with real basis
    drivers: list = field(default_factory=list)
    # Each: {name, assumption_key, formula, unit, components: [{name, value, unit, confidence, basis}]}

    # Extra assumptions (below-line items, productivity, etc.)
    extra_assumptions: dict = field(default_factory=dict)

    # Contradictions from the filing
    contradictions: list = field(default_factory=list)
    # Each: {thesis, counter_evidence, severity, affected_driver}

    # Bear revisions (derived from contradictions)
    bear_revisions: list = field(default_factory=list)
    # Each: {driver, component, new_value, reason}

    # Edge claims — structured disagreements with specific published anchors.
    # Replaces the role of free-text edge_hypothesis. Each entry validated
    # post-parse against the actual anchor values in `consensus_full` /
    # guidance_bundle. Empty list = honest "no edge identified."
    edge_claims: list = field(default_factory=list)
    rejected_edge_claims: list = field(default_factory=list)

    # Narrative synthesis — a 4-6 paragraph SYNTHESIZED research note that
    # weaves together driver observations, transcript tone shifts, macro
    # context, peer divergence, accounting concerns, and analytical
    # observations into a coherent story. This is the lead deliverable —
    # the analytical color that explains "what's interesting about this
    # name and why" — and it surfaces research that would otherwise get
    # filed into Risks / Drivers / Blind Spots sub-bullets where it gets
    # buried.
    narrative_synthesis: str = ""

    # Readiness
    evidence_gaps: list = field(default_factory=list)
    confidence_notes: str = ""

    # Metadata
    source_method: str = ""             # "claude_api", "registry_fallback"
    raw_response: str = ""


def build_research_brief(
    ticker: str,
    financials: StructuredFinancials,
    earnings_text: str,
    consensus_eps: float = None,
    consensus_revenue_m: float = None,
    consensus_full: dict | None = None,
    guidance_bundle=None,           # GuidanceBundle | None
    verbose: bool = False,
) -> ResearchBrief:
    """
    Make one rich Claude API call to produce a ResearchBrief.

    Claude receives:
      1. Structured financials (exact numbers from Polygon/AV)
      2. Earnings text (for qualitative context, guidance, management commentary)
      3. STREET'S PUBLISHED VIEW — full consensus block (per-period EPS/revenue,
         LTG, price targets, revisions). Anchors against which edge_claims must
         disagree.
      4. MANAGEMENT GUIDANCE — structured guidance bundle from press releases,
         deck-guidance subagent, transcript guidance_tracker.
      5. EDGE DISCIPLINE — strict prompt instructions on what counts as edge
         vs. consensus repackaged.
      6. Available schema types with EXACT assumption keys

    Claude produces:
      1. Business understanding
      2. Schema selection with reasoning
      3. Forward drivers with component-level assumptions and basis
      4. Contradictions found in the data
      5. Bear revisions for stress testing
      6. edge_claims — structured list of quantified disagreements with
         specific anchors (validated post-parse against the actual anchor values)
    """
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        if verbose:
            print("  No ANTHROPIC_API_KEY")
        return ResearchBrief(source_method="no_api_key")

    prompt = _build_prompt(
        ticker, financials, earnings_text,
        consensus_eps, consensus_revenue_m,
        consensus_full=consensus_full,
        guidance_bundle=guidance_bundle,
    )

    if verbose:
        print(f"  Deep research: calling Claude ({MODEL})...")

    import random
    import time

    def _post_brief():
        return httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 8000,
                # Low temperature — research brief extraction needs stable
                # outputs, not creativity. At default (1.0) we were seeing
                # $3.96 stdev across 6 runs on the same ticker. 0.2 keeps
                # the brief deterministic-ish while preserving nuance.
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90.0,
        )

    try:
        # Retry on 429 (rate limit) / 529 (overloaded) with jittered backoff.
        # The brief is THE critical call — failing it silently produces
        # empty drivers downstream and garbage EPS. Match the subagent
        # retry pattern: up to 5 attempts, respect server retry-after,
        # wait ≥60s on first retry so the ITPM window clears.
        resp = None
        base_backoff = 20.0
        max_attempts = 5
        for attempt in range(max_attempts):
            resp = _post_brief()
            if resp.status_code not in (429, 529):
                break
            retry_after = resp.headers.get("retry-after") or ""
            server_wait = None
            try:
                server_wait = float(retry_after)
            except Exception:
                pass
            if server_wait and 5 <= server_wait <= 180:
                wait = server_wait + random.uniform(0, 2)
            else:
                wait = min(base_backoff * (1.6 ** attempt), 120.0) + random.uniform(0, 5)
            if verbose:
                print(f"  Deep research: HTTP {resp.status_code}, "
                      f"backoff {wait:.1f}s (attempt {attempt+1}/{max_attempts})")
            time.sleep(wait)

        if resp.status_code != 200:
            if verbose:
                print(f"  Deep research: API error {resp.status_code} (after retries)")
            return ResearchBrief(source_method="claude_api_error")

        data = resp.json()
        text = data["content"][0]["text"]

        if verbose:
            print(f"  Deep research: parsing response ({len(text)} chars)")

        brief = _parse_response(text, ticker, financials)
        brief.raw_response = text
        brief.source_method = "claude_api"

        # Validate and fix common issues (drivers, bear revisions, etc.)
        _validate_brief(brief, verbose)

        # Validate edge_claims against the actual anchor values. This catches
        # Claude making up an anchor_value or referencing an anchor_type that
        # doesn't have a corresponding published number.
        _validate_edge_claims(brief, consensus_full=consensus_full,
                              guidance_bundle=guidance_bundle, verbose=verbose)

        if verbose:
            print(f"  Deep research: {brief.schema_type} schema, "
                  f"{len(brief.drivers)} drivers, "
                  f"{len(brief.contradictions)} contradictions")

        return brief

    except Exception as e:
        if verbose:
            print(f"  Deep research: error - {e}")
        pass  # fall through to return error brief
        return ResearchBrief(source_method="claude_api_error")


# Key name mappings for common Claude mistakes
_KEY_ALIASES = {
    # Claude sometimes uses longer names; map to what the engine expects
    "new_store_count": "new_restaurants",
    "new_stores": "new_restaurants",
    "net_new_stores": "new_restaurants",
    "net_new_openings": "new_restaurants",
    "research_dev_delta_bps": "rd_delta_bps",
    "sales_marketing_delta_bps": "sm_delta_bps",
    "general_admin_delta_bps": "ga_delta_bps",
    "sga_delta_bps_key": "sga_delta_bps",
}

# Valid assumption keys per schema
_VALID_DRIVER_KEYS = {
    "restaurant": {"sss_growth_pct", "new_restaurants", "food_cost_delta_bps",
                    "labor_cost_delta_bps", "other_cost_delta_bps"},
    "franchise_restaurant": {"sss_growth_pct", "new_restaurants"},
    "software": {"net_retention_pct", "new_arr_growth_pct"},
    "general": {"revenue_growth_pct"},
}


# --------------------------------------------------------------------------
# Edge claim validation — drop claims that don't reference a real anchor
# --------------------------------------------------------------------------

# Tolerance for matching claimed anchor_value against the published number.
# 5% accommodates minor rounding (e.g. Claude says $4.13 vs $4.131).
_ANCHOR_VALUE_TOLERANCE_PCT = 0.05

_VALID_EDGE_CATEGORIES = {"synthesis", "interpretation", "non_public_inference", "cross_corpus"}


def _lookup_anchor_value(anchor_type: str, consensus_full: dict | None,
                          guidance_bundle) -> tuple[float | None, str]:
    """
    Look up the canonical published anchor value for a given anchor_type.
    Returns (value, source_label). value=None if the anchor isn't available.
    """
    at = (anchor_type or "").strip().lower()
    cf = consensus_full or {}

    # Consensus EPS / revenue per period
    period_map = {
        "consensus_q_eps":          (("current_quarter",), "eps_mean"),
        "consensus_q_revenue":      (("current_quarter",), "revenue_mean"),
        "consensus_next_q_eps":     (("next_quarter",), "eps_mean"),
        "consensus_next_q_revenue": (("next_quarter",), "revenue_mean"),
        "consensus_fy_eps":         (("current_year",), "eps_mean"),
        "consensus_fy_revenue":     (("current_year",), "revenue_mean"),
        "consensus_next_fy_eps":    (("next_year",), "eps_mean"),
        "consensus_next_fy_revenue":(("next_year",), "revenue_mean"),
    }
    if at in period_map:
        path, key = period_map[at]
        node = cf
        for p in path:
            node = (node or {}).get(p) or {}
        v = node.get(key)
        if v is not None:
            # Normalize revenue to dollars (yfinance returns dollars already)
            return (float(v), f"yfinance consensus {at}")
        return (None, "")

    if at == "consensus_ltg":
        v = cf.get("ltg_eps_5yr")
        if v is not None:
            return (float(v), "yfinance long-term EPS growth (5yr)")
        return (None, "")

    if at == "consensus_price_target":
        pt = cf.get("price_target") or {}
        v = pt.get("mean")
        if v is not None:
            return (float(v), "yfinance price target mean")
        return (None, "")

    # Guidance lookups
    if at.startswith("guidance_"):
        if guidance_bundle is None or not getattr(guidance_bundle, "items", None):
            return (None, "")
        # Map guidance anchor_type to (period_substring, metric)
        # e.g. guidance_q_revenue -> the most recent Q* period item with metric=revenue
        rest = at[len("guidance_"):]
        if rest.startswith("q_"):
            period_match = "q"
            metric = rest[2:]
        elif rest.startswith("fy_"):
            period_match = "fy"
            metric = rest[3:]
        else:
            period_match = ""
            metric = rest
        # canonicalize metric
        metric_canonical_map = {
            "revenue": "revenue",
            "ebitda": "adj_ebitda",
            "adj_ebitda": "adj_ebitda",
            "eps": "eps",
            "adj_eps": "adj_eps",
            "fcf": "fcf",
            "capex": "capex",
            "operating_margin": "operating_margin",
            "gross_margin": "gross_margin",
            "unit_growth": "new_units",
            "new_units": "new_units",
        }
        target = metric_canonical_map.get(metric, metric)
        for item in guidance_bundle.items:
            if item.metric != target:
                continue
            if period_match and period_match not in (item.period or "").lower():
                continue
            v = item.midpoint()
            if v is not None:
                return (float(v), f"{item.source_type}: {item.source_detail}")
        return (None, "")

    return (None, "")


def _validate_edge_claims(brief: ResearchBrief, consensus_full: dict | None,
                           guidance_bundle, verbose: bool = False):
    """
    Drop edge_claims that fail discipline checks. Keeps valid ones,
    moves rejected ones to brief.rejected_edge_claims with a reason.
    """
    raw_claims = brief.edge_claims or []
    valid: list = []
    rejected: list = []

    for c in raw_claims:
        if not isinstance(c, dict):
            rejected.append({"claim": c, "reason": "not a dict"})
            continue

        anchor_type = (c.get("anchor_type") or "").strip().lower()
        claimed_anchor_val = c.get("anchor_value")
        our_value = c.get("our_value")
        evidence = c.get("evidence") or []
        edge_category = (c.get("edge_category") or "").strip().lower()
        why_not_consensus = (c.get("why_not_consensus") or "").strip()
        falsifier = (c.get("falsifier") or "").strip()

        # 1) anchor_type must look up to a real published number
        true_anchor_val, source_label = _lookup_anchor_value(
            anchor_type, consensus_full, guidance_bundle,
        )
        if true_anchor_val is None:
            rejected.append({
                "claim": c,
                "reason": f"anchor_type={anchor_type!r} not found in published anchors "
                          "(consensus_full / guidance_bundle)",
            })
            continue

        # 2) anchor_value must be numeric and within tolerance of the true value
        try:
            claimed_val_f = float(claimed_anchor_val)
        except (TypeError, ValueError):
            rejected.append({
                "claim": c,
                "reason": "anchor_value not numeric",
            })
            continue

        if true_anchor_val != 0:
            rel_err = abs(claimed_val_f - true_anchor_val) / abs(true_anchor_val)
            if rel_err > _ANCHOR_VALUE_TOLERANCE_PCT:
                rejected.append({
                    "claim": c,
                    "reason": f"anchor_value {claimed_val_f:g} doesn't match "
                              f"published {true_anchor_val:g} (off by {rel_err*100:.1f}%) — "
                              f"likely fabricated",
                })
                continue

        # 3) our_value must be numeric
        try:
            float(our_value)
        except (TypeError, ValueError):
            rejected.append({"claim": c, "reason": "our_value not numeric"})
            continue

        # 4) evidence non-empty
        if not evidence or not isinstance(evidence, list):
            rejected.append({"claim": c, "reason": "evidence missing or empty"})
            continue

        # 5) edge_category one of canonical values
        if edge_category not in _VALID_EDGE_CATEGORIES:
            rejected.append({
                "claim": c,
                "reason": f"edge_category={edge_category!r} not in canonical set",
            })
            continue

        # 6) why_not_consensus length floor (catches single-quote-from-transcript fluff)
        if len(why_not_consensus) < 30:
            rejected.append({
                "claim": c,
                "reason": f"why_not_consensus too short ({len(why_not_consensus)} chars; need ≥30)",
            })
            continue

        # 7) falsifier length floor
        if len(falsifier) < 25:
            rejected.append({
                "claim": c,
                "reason": f"falsifier too short ({len(falsifier)} chars; need ≥25)",
            })
            continue

        # Stamp the source label so the renderer can show provenance cleanly
        if not c.get("anchor_source"):
            c["anchor_source"] = source_label

        valid.append(c)

    brief.edge_claims = valid
    brief.rejected_edge_claims = rejected

    if verbose:
        print(f"  Edge claims: {len(valid)} valid, {len(rejected)} rejected")
        for r in rejected[:5]:
            print(f"    REJECTED: {r['reason']}")


def _validate_brief(brief: ResearchBrief, verbose: bool = False):
    """
    Validate and fix common issues in Claude's response.
    Fixes key name mismatches, clamps extreme values, ensures
    extra_assumptions have required fields.
    """
    schema = brief.schema_type

    # Fix driver assumption_key aliases
    for driver in brief.drivers:
        key = driver.get("assumption_key", "")
        if key in _KEY_ALIASES:
            if verbose:
                print(f"  Fixing key: {key} -> {_KEY_ALIASES[key]}")
            driver["assumption_key"] = _KEY_ALIASES[key]

    # Fix extra_assumptions key aliases
    fixed_extra = {}
    for k, v in brief.extra_assumptions.items():
        fixed_extra[_KEY_ALIASES.get(k, k)] = v
    brief.extra_assumptions = fixed_extra

    # Move misplaced cost delta drivers into extra_assumptions
    # (Claude sometimes puts cost deltas as separate drivers instead of extra_assumptions)
    cost_delta_keys = {"cogs_delta_bps", "sm_delta_bps", "rd_delta_bps", "ga_delta_bps",
                       "opex_delta_bps", "cos_delta_bps", "sga_delta_bps",
                       "food_cost_delta_bps", "labor_cost_delta_bps", "other_cost_delta_bps"}
    valid_driver_keys = _VALID_DRIVER_KEYS.get(schema, set())

    drivers_to_keep = []
    for driver in brief.drivers:
        key = driver.get("assumption_key", "")
        if key in cost_delta_keys and key not in valid_driver_keys:
            # Move to extra_assumptions instead
            total = sum(c.get("value", 0) for c in driver.get("components", []))
            brief.extra_assumptions[key] = total
            if verbose:
                print(f"  Moved {key}={total} from driver to extra_assumptions")
        else:
            drivers_to_keep.append(driver)
    brief.drivers = drivers_to_keep

    # Clamp extreme cost deltas (anything over 200 bps is suspicious)
    for key in list(brief.extra_assumptions.keys()):
        if "delta_bps" in key:
            val = brief.extra_assumptions[key]
            if abs(val) > 200:
                clamped = max(min(val, 200), -200)
                if verbose:
                    print(f"  Clamping {key}: {val} -> {clamped}")
                brief.extra_assumptions[key] = clamped

    # Validate bear_revisions reference valid drivers
    valid_driver_names = {d["name"] for d in brief.drivers}
    valid_revisions = []
    for rev in brief.bear_revisions:
        if rev.get("driver") in valid_driver_names:
            # Check component exists
            driver = next((d for d in brief.drivers if d["name"] == rev["driver"]), None)
            if driver:
                comp_names = {c["name"] for c in driver.get("components", [])}
                if rev.get("component") in comp_names:
                    valid_revisions.append(rev)
    brief.bear_revisions = valid_revisions


# --------------------------------------------------------------------------
# Anchor block formatters (street consensus + management guidance)
# --------------------------------------------------------------------------

def _format_consensus_anchor(consensus_full: dict | None) -> str:
    """
    Render `consensus_full` (the dict from ConsensusData.to_dict()) as a
    structured block of REAL published street numbers. Edge claims must
    reference specific values from this block.
    """
    if not consensus_full:
        return ""

    # Resolve calendar-year labels for the period buckets so Claude can
    # match management guidance ("FY2026 revenue $X-Y") to the correct
    # consensus row. Without this, brief reasoning has been confusing
    # current_year and next_year — TMDX's no-edge run was caused by
    # comparing FY2026 guidance to next_year (FY2027) consensus.
    from datetime import datetime
    today = datetime.now()
    # Heuristic: if today is in the second half of the year, current_year
    # in yfinance often refers to the OUTGOING year (already in progress,
    # most analysts have moved focus to next FY). Use today's year as
    # current FY for simplicity; the label is a hint, Claude can reconcile.
    cy_label = today.year
    ny_label = today.year + 1

    lines = [
        "==================================================================",
        "STREET'S PUBLISHED VIEW (sell-side consensus from yfinance)",
        "==================================================================",
        f"(Today: {today.strftime('%Y-%m-%d')}. 'Current FY' likely refers to "
        f"FY{cy_label} for calendar-year reporters; 'Next FY' to FY{ny_label}. "
        f"Match management guidance to whichever fiscal year it explicitly names.)",
    ]

    def _fmt_period(period_data: dict | None, label: str) -> list:
        if not period_data:
            return []
        out = [f"\n{label}:"]
        eps_mean = period_data.get("eps_mean")
        eps_low = period_data.get("eps_low")
        eps_high = period_data.get("eps_high")
        n_eps = period_data.get("eps_num_analysts", 0)
        rev_mean = period_data.get("revenue_mean")
        rev_low = period_data.get("revenue_low")
        rev_high = period_data.get("revenue_high")
        eps_growth = period_data.get("eps_growth_yoy")
        rev_growth = period_data.get("revenue_growth_yoy")
        up_30d = period_data.get("up_revs_30d", 0)
        down_30d = period_data.get("down_revs_30d", 0)
        if eps_mean is not None:
            range_part = ""
            if eps_low is not None and eps_high is not None:
                range_part = f" (range ${eps_low:.2f}-${eps_high:.2f}, {n_eps} analysts)"
            growth_part = f", YoY +{eps_growth*100:.1f}%" if eps_growth is not None else ""
            out.append(f"  • EPS:           ${eps_mean:.2f}{range_part}{growth_part}")
        if rev_mean is not None:
            range_part = ""
            if rev_low is not None and rev_high is not None:
                range_part = f" (range ${rev_low/1e6:,.0f}-${rev_high/1e6:,.0f}M)"
            growth_part = f", YoY +{rev_growth*100:.1f}%" if rev_growth is not None else ""
            out.append(f"  • Revenue:       ${rev_mean/1e6:,.0f}M{range_part}{growth_part}")
        if up_30d or down_30d:
            out.append(f"  • EPS revisions: ↑{up_30d} / ↓{down_30d} over 30 days")
        return out

    lines += _fmt_period(consensus_full.get("current_quarter"), "Current Quarter")
    lines += _fmt_period(consensus_full.get("next_quarter"), "Next Quarter")
    lines += _fmt_period(consensus_full.get("current_year"), f"Current FY (≈ FY{cy_label})")
    lines += _fmt_period(consensus_full.get("next_year"), f"Next FY (≈ FY{ny_label})")

    ltg = consensus_full.get("ltg_eps_5yr")
    pt = consensus_full.get("price_target") or {}
    if ltg is not None or pt.get("mean") is not None:
        lines.append("\nLong-Term:")
        if ltg is not None:
            lines.append(f"  • 5-year EPS growth consensus:  +{ltg*100:.1f}%/yr")
        if pt.get("mean") is not None:
            range_part = ""
            if pt.get("low") is not None and pt.get("high") is not None:
                range_part = f" (range ${pt['low']:.2f}-${pt['high']:.2f})"
            lines.append(f"  • Price target mean:            ${pt['mean']:.2f}{range_part}")

    lines.append("=" * 66)
    return "\n".join(lines)


def _format_anchor_blocks(consensus_full: dict | None, guidance_bundle) -> str:
    """Combined STREET'S PUBLISHED VIEW + MANAGEMENT GUIDANCE block.
    Empty string if no anchors available; brief still proceeds but the
    EDGE DISCIPLINE will result in honest 'no edge identified.'"""
    parts = []
    consensus_text = _format_consensus_anchor(consensus_full)
    if consensus_text:
        parts.append(consensus_text)
    if guidance_bundle is not None:
        try:
            guidance_text = guidance_bundle.to_prompt_text()
            if guidance_text:
                parts.append(guidance_text)
        except Exception:
            pass
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Edge discipline — non-negotiable rules injected into the brief prompt
# --------------------------------------------------------------------------

_EDGE_DISCIPLINE_BLOCK = """
==================================================================
EDGE DISCIPLINE — produce 2-3 STRUCTURED edge_claims when defensible
==================================================================
TARGET: 2-3 well-supported edge_claims that attack DIFFERENT anchors
or DIFFERENT P&L lines. A real research pitch covers MULTIPLE angles —
revenue trajectory, margin trajectory, line-item costs, capital return.
1 claim is usually thin; 2-3 is the right depth for an institutional
note. 0 is a last resort.

A good claim portfolio looks like:
  Claim 1: revenue-direction view (e.g. consensus_fy_revenue or
           guidance_q_revenue)
  Claim 2: margin / opex view (e.g. operating margin trajectory,
           specific guidance_fy_ebitda, sga_growth)
  Claim 3: long-term or structural view (e.g. consensus_ltg, peer-
           relative pricing, share count from buyback)

Each claim must be a quantified disagreement with a SPECIFIC published
anchor (from the consensus or guidance blocks above). This is the
central output of the brief — be willing to commit to a view as long
as you can ground it.

WHAT GOOD LOOKS LIKE (concrete examples):

Example A — non_public_inference (cross-disclosure triangulation):
  anchor_type: "consensus_fy_revenue"
  anchor_value: 3140000000
  anchor_source: "yfinance consensus"
  our_value: 2700000000
  rationale: "Q1 2026 guide of $600M midpoint × normal seasonal pattern
   (Q1 = 22-24% of FY for ad-driven businesses) implies $2.5-2.7B FY26,
   well below consensus $3.14B. Consensus appears to be modeling top-down
   from headline growth rather than reconciling to the Q1 guide."
  evidence: [
    {"quote": "Revenue in the range of $595 million to $605 million",
     "source_type": "deck"}
  ]
  edge_category: "non_public_inference"
  why_not_consensus: "Single-quarter analysts model FY top-down; they
   typically don't reconcile the most recent quarterly guide against
   their full-year consensus run-rate."
  falsifier: "If Q1 prints above $605M and management raises full-year
   guide on the call."
  eps_impact: -0.85

Example B — synthesis (multi-quarter language pattern):
  anchor_type: "guidance_fy_revenue"
  anchor_value: 660000000
  our_value: 615000000
  rationale: "Management's guidance language has shifted from 'we expect'
   in Q2 to 'we are aiming for' in Q3 to 'we are targeting' in Q4 — a
   3-quarter softening drift suggests internal confidence is declining
   even though the headline guide is unchanged."
  evidence: [
    {"quote": "we are now targeting", "source_type": "transcript"},
    {"quote": "we expect", "source_type": "transcript"}
  ]
  edge_category: "synthesis"
  why_not_consensus: "Single-quarter analysts hear each call in isolation;
   they don't track multi-quarter language drift as a confidence signal."
  falsifier: "If next quarter's call returns to 'we expect' or higher
   conviction language."
  eps_impact: -0.30

KEY VALIDITY CRITERIA (each claim is rejected if missing):
  ✓ anchor_type and anchor_value match a real published number above
  ✓ our_value is a different specific number
  ✓ evidence list is non-empty with at least one quote + source_type
  ✓ edge_category ∈ {synthesis, interpretation, non_public_inference, cross_corpus}
  ✓ why_not_consensus ≥ 30 chars (what stopped sell-side from same conclusion)
  ✓ falsifier ≥ 25 chars (what would disprove this in next 1-2 prints)

WHAT'S NOT EDGE (compress — these get rejected):
  ✗ "Operating leverage continues" without specific anchor
  ✗ Public capital return ("buyback EPS accretive")
  ✗ Recap of operating model ("benefits from search distribution")
  ✗ Generic moat / sustainable advantage
  ✗ Multiple-expansion arguments alone

PUBLIC-DATA TEST — for each claim, briefly answer:
  "What stopped a sell-side analyst with the same data from concluding
   this?" If the answer fits one of the four edge_categories, you have
   edge. Don't be intimidated — this is a SHORT explanation per claim,
   not a treatise.

A SINGLE claim is usually too thin. If you've found one disagreement,
look for two more from different angles:
  - You attacked revenue → can you also attack margin or opex?
  - You attacked current FY → does the same evidence imply a long-term
    (LTG) disagreement too?
  - You attacked one anchor in the consensus block → is there a
    guidance anchor (or peer anchor) that ALSO supports a delta?

EMPTY (zero claims) IS A LAST RESORT. If after honest review of every
anchor above and every section of the corpus you genuinely have no
defensible disagreement, return an empty edge_claims list. But that's
rare — for most names, careful corpus + macro + peer reading produces
2-3 specific things to disagree with. TAKE THE POSITION when the data
supports it. The discipline is to quantify and ground; it's not to
refuse to commit.
==================================================================
"""


def _build_prompt(ticker: str, fin: StructuredFinancials, earnings_text: str,
                  consensus_eps: float = None, consensus_revenue_m: float = None,
                  consensus_full: dict | None = None,
                  guidance_bundle=None) -> str:
    """Build the single rich prompt for Claude."""

    # Consensus context block (lightweight back-compat — gets superseded by
    # the richer STREET'S PUBLISHED VIEW block below when consensus_full is set)
    consensus_block = ""
    if consensus_eps and not consensus_full:
        consensus_block = f"""
CONSENSUS CONTEXT (this is what the street currently expects):
  Consensus forward EPS: ${consensus_eps:.2f}
  {"Consensus revenue: $" + f"{consensus_revenue_m:,.1f}M" if consensus_revenue_m else ""}
"""

    # Build the richer anchor blocks (street published view + management guidance)
    # — these are the authoritative anchors edge_claims must reference.
    anchor_block = _format_anchor_blocks(consensus_full, guidance_bundle)
    edge_discipline_block = _EDGE_DISCIPLINE_BLOCK

    return f"""You are an equity research analyst building a forward earnings estimate for {ticker}.
Your PRIMARY objective is to FIND THE EDGE -- where does the market's consensus view
have a blind spot, and what specific evidence supports a different view?

You are given EXACT structured financials (do NOT change these numbers). Your job is:
1. Understand the business and identify what drives value
2. Figure out what the street assumes and WHERE IT MIGHT BE WRONG
3. Build a forward estimate grounded in specific evidence
4. Identify the tensions and contradictions in the data
5. Design stress tests for your own assumptions

{fin.to_summary_text()}
{consensus_block}
{anchor_block}
{edge_discipline_block}
EARNINGS/FILING TEXT (for qualitative context, guidance, management commentary):
{earnings_text}

AVAILABLE ECONOMIC SCHEMAS (you MUST use the EXACT assumption_key names listed below):

1. "restaurant" -- Company-operated restaurants.
   Revenue = existing_stores x AUV x (1+SSS) + new_stores x AUV x productivity x 0.5
   REQUIRED driver assumption_keys:
     - "sss_growth_pct": same-store sales growth (decompose into traffic + ticket components)
     - "new_restaurants": new store openings (unit: count)
   REQUIRED cost delta_bps keys (positive = cost increase, negative = cost decrease):
     - "food_cost_delta_bps", "labor_cost_delta_bps", "other_cost_delta_bps"
   REQUIRED extra_assumptions:
     - "new_store_productivity": 0.70-0.85
     - "cash_ga_growth_pct", "stock_comp_growth_pct"

2. "franchise_restaurant" -- Franchise-heavy chain.
   Revenue = system_wide_sales x (royalty_rate + ad_fund_rate) + company_owned + supply_chain
   REQUIRED driver assumption_keys: "sss_growth_pct", "new_restaurants"
   REQUIRED extra_assumptions: "royalty_rate_pct", "ad_fund_rate_pct", "company_owned_stores",
     "company_sss_pct", "cos_delta_bps", "sga_delta_bps", "stock_comp_growth_pct", "da_growth_pct"

   PITCH DISCIPLINE FOR RESTAURANT/RETAIL SSS (mandatory for "restaurant" and "franchise_restaurant"):
   If sss_growth_pct is positive (the "easy comps" case), you MUST address these 5 questions
   in the basis fields of the sss_growth_pct components, OR explicitly list them in evidence_gaps:
     1. What % of the comp came from new product vs. core business?
     2. Is the new product a repeat occasion or trial occasion?
     3. Does the company own the category (or is it new entry)?
     4. Is management investing in the product (media spend, operational capacity)?
     5. What's the quantified comp drag if it laps the negative period next year?
   RULE: Never pitch easy comps without decomposing what created the hard comps.
   If 3+ questions are unanswerable from filings/transcripts, lower confidence on sss_growth_pct
   to 0.40 or below and add the unanswered questions to evidence_gaps.

3. "software" -- SaaS/subscription.
   Revenue = prior_rev x (net_retention_pct/100) + prior_rev x (new_arr_growth_pct/100)
   REQUIRED driver assumption_keys: "net_retention_pct", "new_arr_growth_pct"
   REQUIRED cost delta_bps keys: "cogs_delta_bps", "sm_delta_bps", "rd_delta_bps", "ga_delta_bps"
   REQUIRED extra_assumptions: "stock_comp_growth_pct", "da_growth_pct"

4. "general" -- Any other P&L.
   Revenue = prior_rev x (1 + revenue_growth_pct/100)
   REQUIRED driver assumption_keys: "revenue_growth_pct"
   REQUIRED cost delta_bps keys: "cogs_delta_bps", "opex_delta_bps"
   REQUIRED extra_assumptions: "stock_comp_growth_pct", "da_growth_pct"

Respond in EXACTLY this JSON format (no markdown, no explanation outside the JSON):
{{
  "business_description": "2-3 sentences: what the company does, how it makes money",
  "economic_structure": "1-2 sentences: revenue model, key economic characteristics",
  "key_debate": "1-2 sentences: what the market is arguing about",

  "edge_hypothesis": "1-2 sentence summary of the joint thesis across edge_claims below. If edge_claims is empty, state 'No edge identified — estimate is within consensus range.'",
  "edge_type": "EXPECTATION_GAP|VALUATION_GAP|QUALITY_GAP|DURATION_GAP|BEHAVIORAL_GAP",
  "why_market_is_wrong": "2-3 sentences: aggregate WHY the street is vulnerable. Cite the corpus. If no edge, state 'no clear edge.'",

  "narrative_synthesis": "THE LEAD DELIVERABLE — 6-9 paragraphs of synthesized DILIGENCING prose. SHOW YOUR WORK like a senior analyst doing real diligence — not just summarized conclusions. For every substantive claim, walk the reader through the analytical question raised, the data you pulled to answer it, and the calculation done — THEN draw the conclusion. The reader should see the diligence trail, not just the answer.\n\n  CONCRETE DILIGENCE PATTERNS — apply at least these when relevant:\n  • SEQUENTIAL TRAJECTORY: when claiming a sequential change is concerning or normal (e.g. 'Q2 EBITDA guide $25.5M is below Q1's $26M'), explicitly compare against PRIOR-YEAR Q1→Q2 sequentials (cite Q2 2025 EBITDA / Q2 2024 EBITDA from press releases or transcript references) before concluding whether it's seasonal vs. structural. Don't just observe the sequential — diligence whether it deviates from the seasonal pattern.\n  • GUIDANCE BEAT HISTORY: when discussing whether Q-guidance is conservative, walk through the actual beat magnitudes from prior quarters ('over the last 4 quarters, management has beaten Q-guide midpoint by an average of $Xm — applying that to the current $25.5M Q2 guide implies actual Q2 EBITDA of ~$Y, which would be in line with / above / below Q1's $26M').\n  • MARGIN BRIDGE: when discussing margin trajectory, decompose into mechanism (volume leverage, mix shift, opex investment, pass-through nuance). Show the math (e.g. 'gross margin shift of +50bps on $209M revenue = $1.05M of incremental gross profit, of which $X is pricing and $Y is mix per management's Q&A').\n  • PEER CONTEXT: when claiming a metric is impressive or concerning, cite the peer benchmark from the PEER CONSENSUS TABLE above ('Reddit's 38% incremental margin compares to META's 50% and SNAP's 12% — suggests room to expand as scale builds').\n  • ACCOUNTING DISCREPANCY MATH: when reconciling GAAP vs non-GAAP or normalized vs reported, do the math explicitly — show what's in the gap and how big it is in dollar / percent terms.\n  • MANAGEMENT CREDIBILITY: when calling out a tone shift / dodge / sandbag pattern, cite the SPECIFIC quote from the SPECIFIC quarter and trace the multi-quarter pattern.\n\n  WHAT TO WEAVE TOGETHER (in the SAME prose, not separate sections):\n  (1) HARD EDGE angles: quantified disagreements with consensus or guidance, with the diligence trail showing why street is wrong;\n  (2) SOFT EDGE / story-strength observations: beat-and-raise patterns shown via the actual beat-rate math, management credibility signals with specific quotes, deal-flow momentum (Salesforce-style partnerships, customer wins, ARR-per-customer trajectory), language confidence drifts across quarters, capital allocation behavior;\n  (3) Skeptic counter-points: NRR ceilings shown via peer-comp benchmark, comp difficulty walked through with the actual seasonal math, dodged Q&A moments cited verbatim;\n  (4) Accounting / capital-structure quirks reconciled with explicit math.\n\n  The CRITICAL surfacing rule still applies: research findings that would otherwise get buried in sub-bullets (tax normalization, ceiling-vs-base-case interpretation, CFO refusing to disclose, multi-quarter language drift, supply-side constraints) MUST appear in the synthesis with their full diligence trail.\n\n  RECONCILIATION DISCIPLINE: when structured financials and management commentary cite materially different versions of the same metric (GAAP vs non-GAAP gross margin, normalized vs reported EPS, organic vs reported revenue, ex-currency vs reported), you MUST cite both numbers and explain the gap quantitatively. Example: 'GAAP gross margin of 35.8% on $209M revenue = $74.8M gross profit; non-GAAP at 59.5% = $124.4M; the $49.6M gap reflects ~$50M of network pass-through costs (~24% of revenue) excluded from non-GAAP — structural to CPaaS economics, not adjustment noise.'\n\n  Use inline attribution ('In the Q3 call, CFO Hernandez said X' / 'The Q4 PR disclosed Y' / 'Deck p.12 shows Z') — not footnote markers.\n\n  Length target: 1500-2500 words across 6-9 paragraphs. Each paragraph should EARN its space by either showing the analytical work, surfacing buried research, or making a real argument with evidence — never just restating known facts. The final paragraph CAN summarize the synthesized takes, but the preceding paragraphs are the diligence work.",

  "edge_claims": [
    {{
      "anchor_type": "consensus_q_eps | consensus_q_revenue | consensus_fy_eps | consensus_fy_revenue | consensus_next_fy_eps | consensus_next_fy_revenue | consensus_ltg | consensus_price_target | guidance_q_revenue | guidance_q_ebitda | guidance_q_eps | guidance_fy_revenue | guidance_fy_ebitda | guidance_fy_eps | guidance_unit_growth | guidance_other (specify)",
      "anchor_value": 0.0,
      "anchor_source": "yfinance consensus / Q4 PR / Q4 earnings call (CFO) / etc.",
      "our_value": 0.0,
      "line_hit": "revenue | margin | opex | tax | share_count — which P&L line your delta hits, used for EPS flow-through math. Use 'revenue' for top-line variants; 'margin' for op-margin/EBITDA-margin variants in pp; 'opex' for SG&A or R&D level variants in absolute $; 'tax' for tax rate variants in pp; 'share_count' for buyback/dilution variants in shares.",
      "rationale": "1-2 sentences: why we disagree with this specific anchor",
      "evidence": [
        {{
          "quote": "verbatim from corpus (transcript / filing / deck / peer / macro)",
          "source_type": "transcript | filing | deck | press_release | macro | peer"
        }}
      ],
      "evidence_strength": "cited | inferred | speculative",
      "edge_category": "synthesis | interpretation | non_public_inference | cross_corpus",
      "why_not_consensus": "What stopped a sell-side analyst with the same public data from concluding the same thing? (≥40 chars)",
      "falsifier": "What would prove this wrong in next 1-2 prints? (≥30 chars)",
      "eps_impact": 0.0
    }}
  ],

  "consensus_assumptions": {{
    "driver_name": "What the street likely assumes for this driver and why (1-2 sentences)"
  }},
  "guidance_vs_our_view": {{
    "driver_name": "Management guides X. We think Y because Z. (1-2 sentences)"
  }},

  "schema_type": "restaurant|franchise_restaurant|software|general",
  "schema_reasoning": "Why this schema fits",

  "drivers": [
    {{
      "name": "driver_name",
      "assumption_key": "MUST be one of the REQUIRED keys listed above for your chosen schema",
      "formula": "how components combine (e.g. traffic + ticket)",
      "unit": "pct|bps|count",
      "components": [
        {{
          "name": "component_name",
          "value": 0.0,
          "unit": "pct|bps|count",
          "confidence": 0.5,
          "basis": "Multi-sentence basis citing SPECIFIC evidence from the filing, written with INLINE ATTRIBUTION suitable for a research note — phrases like 'In the Q3 2025 call, CEO Brian Niccol said X' or 'The 10-Q disclosed Y' or 'The 8-K filed 2026-01-15 showed Z'. Do NOT use footnote markers like [1]. Write prose that reads naturally when dropped into a research document. Higher confidence (0.65+) requires multiple data points. Lower confidence (0.35-0.50) when extrapolating from limited data.",
          "evidence_strength": "cited|inferred|speculative",
          "citation": "For 'cited': verbatim quote + source (e.g. 'CEO Q3 2025 call: \\'same-store sales down 3%\\'' or '10-K p.42: revenue $630.9M'). For 'inferred': a short logical chain from cited facts (e.g. 'From 15% unit growth + 3-month Smart Kitchen maturation, ~80% of 2026 benefit lags into 2H'). For 'speculative': leave empty string. Required field."
        }}
      ]
    }}
  ],

  "extra_assumptions": {{}},

  "contradictions": [
    {{
      "thesis": "What the bull case assumes",
      "counter_evidence": "SPECIFIC evidence from the filing that challenges it -- quote numbers, trends, management language",
      "severity": "serious|moderate|minor",
      "affected_driver": "which driver this challenges"
    }}
  ],

  "bear_revisions": [
    {{
      "driver": "driver_name (must match a driver name from your drivers list)",
      "component": "component_name (must match a component name from that driver)",
      "new_value": 0.0,
      "reason": "Why this bear scenario is plausible, citing evidence"
    }}
  ],

  "evidence_gaps": ["What SPECIFIC information we don't have that would change the estimate"],
  "confidence_notes": "Overall assessment: how strong is the edge? What's the biggest risk to this thesis?"
}}

CRITICAL RULES:
1. You MUST use the EXACT assumption_key names listed above. Do NOT invent new key names.
2. Drivers are FORWARD-LOOKING for the NEXT fiscal year.
3. Cost delta_bps: positive = costs INCREASE, negative = DECREASE. 100 bps = 1pp. Keep realistic (-100 to +100 typical).
4. Confidence scoring: 0.70+ requires 3+ specific data points from filing. 0.50-0.70 requires clear directional evidence. 0.30-0.50 is extrapolation or general industry view.
5. Bear revisions must EXACTLY match your driver/component names.
6. The edge_hypothesis is THE MOST IMPORTANT FIELD. If you can't articulate a specific edge, say "No clear edge identified -- estimate is close to consensus."
7. For consensus_assumptions: reason about what the street MUST be assuming to get their EPS number. Where is that assumption most fragile?
8. For guidance_vs_our_view: compare management guidance to your own view. If they diverge, explain why. If management historically guides conservatively, note that.
9. EVIDENCE-STRENGTH GRADING (per component, MANDATORY):
   • "cited"       = the value is directly supported by verbatim text in the corpus (transcript quote, filing line, press-release number, deck page). The `citation` field MUST contain the quote + source.
   • "inferred"    = the value is a LOGICAL derivation from cited facts (math, mechanical follow-on, unit economics). The `citation` field must describe the chain briefly.
   • "speculative" = plausible-mechanism reasoning without corpus support ("macro pressure will persist", "management is probably optimistic"). Still valid — hypotheses have value — but must be labeled as such and citation left as empty string.
   DO NOT label something "cited" if you're paraphrasing or generalizing. The test: could a fact-checker find the exact text in the corpus? If not, it's "inferred" or "speculative." Honest labeling is more useful than false precision — an analyst reading the note wants to know which claims have backing.

10. GAAP vs NON-GAAP RECONCILIATION (mandatory, applies to narrative_synthesis):
    Compare the gross margin number in your STRUCTURED FINANCIALS block above against any gross margin number management cites in transcripts/decks. If they differ by more than 5 percentage points (the GAAP-vs-non-GAAP gap typical for CPaaS, biotech with milestone revenue, ad-tech with TAC, etc.), you MUST cite BOTH numbers in the narrative_synthesis and explain the reconciliation. DO NOT cite only the more flattering management-cited figure.
    Same rule for: reported vs. normalized EPS (when one-time items distort), reported vs. organic revenue (when M&A obscures), reported vs. ex-currency revenue (when FX matters), reported vs. ex-acquisition operating margin.
    This is a hard rule — failure to reconcile a >5pp gross margin gap is a research-quality failure that downstream readers cannot recover from.

==================================================================
GUIDANCE ANCHORING (mandatory -- this is how analysts actually work)
==================================================================
9. ANCHOR ON GUIDANCE. For every driver whose assumption_key could map to something management guides (revenue_growth_pct, sss_growth_pct, net_retention_pct, new_restaurants, gross_margin_pct, operating_margin_pct, etc.):
   a. In the `basis` field, STATE MANAGEMENT'S MOST RECENT GUIDANCE for that driver in a specific quote ("On the Q3 2026 call, CFO guided FY26 revenue of $6.65-6.70B").
   b. STATE YOUR VALUE relative to guide ("our +11% growth is ~700bps BELOW the guide midpoint of +18%").
   c. JUSTIFY THE DEVIATION with a specific failure-mode OR confirmation-mode mechanism ("we model guide-break because X launched weak, management cited Y headwind in Q&A, analyst Z pressed on W and got a hedging answer"). Vague reasons like "conservative modeling" or "general caution" are NOT acceptable.

10. GUIDANCE DEVIATION BAR. If your driver value is >500bps away from management guidance:
   a. The thesis MUST hinge on a CONCRETE, NAMED failure mode (a specific product miss, cost program slippage, competitive response, macro signal) — NOT a general feeling.
   b. If you can't name the mechanism, MOVE CLOSER TO GUIDE and lower confidence. Do not pitch variance you can't defend.
   c. If you're BELOW guide (bearish) while management just RAISED guide, your `why_market_is_wrong` must explain why management is wrong or sandbagging-reversing. This is the rarest edge — demand extraordinary evidence.

11. GUIDANCE VS STREET. When consensus is ABOVE guide, say so explicitly in consensus_assumptions: "Street sits 300bps above guide midpoint, implicitly fading management's conservatism." Your job is to decide whether the street's implied fade is earned or not, and take a side.

12. SHORT-SIDE DISCIPLINE. A short pitch when management is RAISING guidance is statistically rare. If you're producing one, make sure the bear thesis is sharp and specific (cite the exact transcript passage, metric, or contradiction). If you only have general skepticism, the pitch is NOT a short — it's "close to consensus with a tilt."
"""


def _parse_response(text: str, ticker: str, fin: StructuredFinancials) -> ResearchBrief:
    """Parse Claude's JSON response into a ResearchBrief."""
    # Try to extract JSON from the response
    text = text.strip()
    if text.startswith("```"):
        # Strip markdown code fences
        lines = text.split("\n")
        start = 1
        end = len(lines)
        for i, line in enumerate(lines):
            if i > 0 and line.strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                return ResearchBrief(
                    source_method="claude_api_parse_error",
                    confidence_notes=f"Failed to parse JSON response: {text[:200]}",
                )
        else:
            return ResearchBrief(source_method="claude_api_parse_error")

    brief = ResearchBrief(
        company_name=f"{ticker}",
        business_description=data.get("business_description", ""),
        economic_structure=data.get("economic_structure", ""),
        key_debate=data.get("key_debate", ""),
        edge_hypothesis=data.get("edge_hypothesis", ""),
        edge_type=data.get("edge_type", ""),
        why_market_is_wrong=data.get("why_market_is_wrong", ""),
        consensus_assumptions=data.get("consensus_assumptions", {}),
        guidance_vs_our_view=data.get("guidance_vs_our_view", {}),
        schema_type=data.get("schema_type", "general"),
        schema_reasoning=data.get("schema_reasoning", ""),
        drivers=data.get("drivers", []),
        extra_assumptions=data.get("extra_assumptions", {}),
        contradictions=data.get("contradictions", []),
        bear_revisions=data.get("bear_revisions", []),
        edge_claims=data.get("edge_claims", []) or [],
        narrative_synthesis=data.get("narrative_synthesis", "") or "",
        evidence_gaps=data.get("evidence_gaps", []),
        confidence_notes=data.get("confidence_notes", ""),
    )

    return brief


