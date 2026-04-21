"""
Edge Detection Brain

Answers the question: "Is there an actionable edge here?"

Given our estimate and consensus, this module:
1. Back-solves consensus to infer what the street assumes for each driver
2. Identifies where our assumptions diverge (variant decomposition)
3. Scores confidence per variant driver
4. Checks if the variant is already priced into the stock
5. Identifies catalysts that would prove/disprove the thesis
6. Produces a final actionability verdict

The key insight: consensus EPS is a single number, but it IMPLIES
assumptions about every driver. By reversing our ModelSpec with
consensus EPS as the target, we can infer what the street must believe
and then compare driver-by-driver.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ImpliedConsensus:
    """What the street must assume to get their EPS number."""
    consensus_eps: float = 0
    zero_change_eps: float = 0          # EPS with no changes from prior year
    total_implied_delta: float = 0      # consensus_eps - zero_change_eps
    implied_drivers: dict = field(default_factory=dict)
    # {driver_name: {component_name: implied_value}}


@dataclass
class VariantDriver:
    """One driver where our view differs from consensus."""
    driver: str
    component: str
    our_value: float
    consensus_value: float
    delta: float                        # our - consensus
    eps_contribution: float             # how much this driver adds to total variant
    pct_of_total: float                 # % of total EPS variant from this driver
    confidence: float                   # our confidence in this component
    evidence_strength: str = "moderate" # strong/moderate/weak
    basis: str = ""                     # why we hold this view
    source: str = ""                    # "brief" / "registry" / "merged"
    brief_value: float = 0             # what Claude suggested (if different)


@dataclass
class PricedInAssessment:
    """Whether the variant is already reflected in the stock."""
    likely_priced_in: bool = False
    confidence: float = 0.5
    reasoning: str = ""
    short_signal: str = "neutral"       # supports/contradicts/neutral
    options_signal: str = "neutral"


@dataclass
class Catalyst:
    """An event that would prove or disprove the thesis."""
    event: str = ""
    timeframe: str = ""
    resolves_driver: str = ""
    impact: str = ""                    # confirms/challenges
    proximity_days: int = 90
    resolution_window: str = ""         # next_quarter / next_annual / multi_year
    actual_date: str = ""               # YYYY-MM-DD if known


@dataclass
class EdgeAssessment:
    """Final synthesis of edge analysis."""
    verdict: str = "NO_CLEAR_EDGE"
    actionability_score: float = 0
    variant_pct: float = 0              # total EPS variant as % of consensus
    variant_eps: float = 0              # our EPS - consensus EPS
    variants: list = field(default_factory=list)     # [VariantDriver]
    implied_consensus: ImpliedConsensus = field(default_factory=ImpliedConsensus)
    priced_in: PricedInAssessment = field(default_factory=PricedInAssessment)
    catalysts: list = field(default_factory=list)     # [Catalyst]
    edge_narrative: str = ""
    time_horizon: str = ""              # trade (<30d) / swing (30-90d) / position (90d+)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "actionability_score": round(self.actionability_score, 3),
            "variant_pct": round(self.variant_pct, 1),
            "variant_eps": round(self.variant_eps, 2),
            "variants": [
                {"driver": v.driver, "component": v.component,
                 "our_value": v.our_value, "consensus_value": round(v.consensus_value, 2),
                 "delta": round(v.delta, 2), "eps_contribution": round(v.eps_contribution, 4),
                 "pct_of_total": round(v.pct_of_total, 1),
                 "confidence": v.confidence, "evidence_strength": v.evidence_strength,
                 "source": v.source, "brief_value": v.brief_value}
                for v in self.variants
            ],
            "priced_in": {
                "likely_priced_in": self.priced_in.likely_priced_in,
                "reasoning": self.priced_in.reasoning,
            },
            "catalysts": [
                {"event": c.event, "timeframe": c.timeframe,
                 "resolves_driver": c.resolves_driver, "impact": c.impact}
                for c in self.catalysts
            ],
            "edge_narrative": self.edge_narrative,
            "time_horizon": self.time_horizon,
        }


def detect_edge(
    model,
    dd,
    sens_table: list,
    post_outputs: dict,
    consensus: dict,
    brief,
    registry_data: dict = None,
    setup_assessment: dict = None,
    consensus_data: dict = None,
    assumption_provenance: dict = None,
    verbose: bool = False,
) -> EdgeAssessment:
    """
    Main entry point: detect whether an actionable edge exists.

    Args:
        model: ModelSpec instance (post-challenge state)
        dd: DriverDecomposition instance
        sens_table: sensitivity table from dd.get_sensitivity_table()
        post_outputs: model.compute_outputs() after adversarial
        consensus: {"eps": float, "revenue_m": float}
        brief: ResearchBrief
        registry_data: company registry entry
        setup_assessment: from market_overlay (optional)
        consensus_data: full dict from consensus_fetcher (optional, has earnings_date)
    """
    def v(msg):
        if verbose:
            print(msg)

    consensus_eps = consensus.get("eps")
    if consensus_eps is None:
        v("  Edge: no consensus EPS available")
        return EdgeAssessment(verdict="NO_CONSENSUS", edge_narrative="No consensus EPS to compare against")

    our_eps = post_outputs.get("eps", 0)
    variant_eps = our_eps - consensus_eps
    variant_pct = (variant_eps / consensus_eps * 100) if consensus_eps else 0

    # Step 1: Back-solve consensus
    implied = back_solve_consensus(consensus_eps, model, dd, sens_table, v)

    # Step 2: Identify variants (with provenance)
    variants = identify_variants(dd, implied, sens_table, assumption_provenance, v)

    # Step 3: Score confidence
    variants = score_confidence(variants, brief, v)

    # Step 4: Priced-in check
    priced_in = assess_priced_in(variant_pct, setup_assessment, v)

    # Step 5: Identify catalysts (with real earnings dates)
    catalysts = identify_catalysts(brief, registry_data, consensus_data, v)

    # Step 6: Compute verdict
    assessment = compute_verdict(
        variants, priced_in, catalysts,
        variant_eps, variant_pct, consensus_eps, our_eps, v,
    )
    assessment.implied_consensus = implied

    return assessment


def back_solve_consensus(consensus_eps, model, dd, sens_table, v=None):
    """
    Given consensus EPS, infer what the street assumes for each driver.

    Per-driver marginal bisection: for each component independently,
    hold all other drivers at our values and binary-search for the
    value of THIS component that produces consensus EPS.

    This asks the right question: "if the street agrees with us on
    everything else, what must they assume for THIS driver?"
    """
    if v:
        v("  Edge: back-solving consensus assumptions...")

    # Save current state
    saved_assumptions = dict(model.assumptions)

    # Compute flat EPS for reporting (all drivers at neutral)
    NEUTRAL_VALUES = {"net_retention_pct": 100}
    for driver in dd.drivers.values():
        neutral = NEUTRAL_VALUES.get(driver.assumption_key, 0)
        model.assumptions[driver.assumption_key] = neutral
    flat_outputs = model.compute_outputs()
    flat_eps = flat_outputs.get("eps", 0)

    # Restore to our estimated values
    model.assumptions = saved_assumptions
    dd.inject_into_model(model)

    # Per-driver marginal back-solve via bisection
    implied_drivers = {}
    for driver in dd.drivers.values():
        comp_implied = {}
        for cname, comp in driver.components.items():
            # Check if this component has any EPS sensitivity
            eps_per_unit = 0
            for row in sens_table:
                if row["driver"] == driver.driver_name and row["component"] == cname:
                    eps_per_unit = row.get("eps_per_unit", 0)
                    break

            if abs(eps_per_unit) < 1e-8:
                # Zero sensitivity -- driver doesn't move EPS
                comp_implied[cname] = round(comp.value, 2)
                continue

            # Search bounds based on unit type
            lo, hi = _search_bounds(comp)

            # Binary search for value that produces consensus EPS
            implied_val = _bisect_for_eps(
                model, dd, driver, cname, comp,
                consensus_eps, lo, hi,
            )
            comp_implied[cname] = round(implied_val, 2)

        implied_drivers[driver.driver_name] = comp_implied

    # Restore original state cleanly
    model.assumptions = dict(saved_assumptions)
    dd.inject_into_model(model)

    if v:
        v(f"  Edge: flat EPS=${flat_eps:.2f}, consensus=${consensus_eps:.2f}")
        for dn, comps in implied_drivers.items():
            for cn, iv in comps.items():
                our_val = dd.drivers[dn].components[cn].value
                v(f"    {dn}.{cn}: street={iv:+.1f} (ours={our_val:+.1f})")

    return ImpliedConsensus(
        consensus_eps=consensus_eps,
        zero_change_eps=round(flat_eps, 2),
        total_implied_delta=round(consensus_eps - flat_eps, 2),
        implied_drivers=implied_drivers,
    )


def _search_bounds(comp):
    """Search bounds for bisection based on driver component unit type."""
    unit = comp.unit
    if unit == "bps":
        return (-500.0, 500.0)
    elif unit == "count":
        return (0.0, 2000.0)
    elif unit == "pct":
        # Check if this is a net_retention component (base ~100)
        if comp.value > 50:
            return (80.0, 140.0)
        return (-20.0, 30.0)
    return (-100.0, 100.0)


def _bisect_for_eps(model, dd, driver, cname, comp, target_eps, lo, hi, max_iters=40):
    """
    Binary search for the value of one component that makes the model
    produce target_eps.

    Other drivers are held at NEUTRAL (zero change from prior year),
    not at our estimated values. This isolates each driver's implied
    consensus value without contamination from our bearish/bullish
    assumptions on other drivers.
    """
    # Save all component values
    saved_values = {}
    for d in dd.drivers.values():
        for cn, c in d.components.items():
            saved_values[(d.driver_name, cn)] = c.value

    # Set all OTHER components to neutral (zero)
    NEUTRAL_COMPONENT = {"net_retention_pct": 100}
    for d in dd.drivers.values():
        for cn, c in d.components.items():
            if d.driver_name == driver.driver_name and cn == cname:
                continue  # leave target component alone
            # For net_retention base_retention, neutral is 100
            if d.assumption_key in NEUTRAL_COMPONENT and cn in ("base_retention",):
                c.value = NEUTRAL_COMPONENT[d.assumption_key]
            else:
                c.value = 0

    def eval_eps(test_val):
        comp.value = test_val
        dd.inject_into_model(model)
        return model.compute_outputs().get("eps", 0)

    def restore():
        for (dn, cn), val in saved_values.items():
            dd.drivers[dn].components[cn].value = val
        dd.inject_into_model(model)

    eps_lo = eval_eps(lo)
    eps_hi = eval_eps(hi)

    # If target is outside reachable range, clamp
    if target_eps <= min(eps_lo, eps_hi):
        restore()
        return lo if eps_lo <= eps_hi else hi
    if target_eps >= max(eps_lo, eps_hi):
        restore()
        return hi if eps_hi >= eps_lo else lo

    # Bisection -- works for both positive and inverted relationships
    increasing = eps_hi > eps_lo
    for _ in range(max_iters):
        mid = (lo + hi) / 2.0
        eps_mid = eval_eps(mid)

        if abs(eps_mid - target_eps) < 0.001:
            restore()
            return mid

        if (eps_mid < target_eps) == increasing:
            lo = mid
        else:
            hi = mid

    restore()
    return (lo + hi) / 2.0


def identify_variants(dd, implied, sens_table, assumption_provenance=None, v=None):
    """
    Compare our driver assumptions vs implied consensus.
    Rank by EPS contribution (where the edge comes from).
    Annotates each variant with its provenance source.
    """
    variants = []
    prov = assumption_provenance or {}

    for driver in dd.drivers.values():
        implied_comps = implied.implied_drivers.get(driver.driver_name, {})
        for cname, comp in driver.components.items():
            consensus_val = implied_comps.get(cname, 0)
            delta = comp.value - consensus_val

            # Find EPS sensitivity for this component
            eps_per_unit = 0
            for row in sens_table:
                if row["driver"] == driver.driver_name and row["component"] == cname:
                    eps_per_unit = row.get("eps_per_unit", 0)
                    break

            eps_contribution = delta * eps_per_unit

            # Look up provenance for this component
            prov_key = f"{driver.driver_name}.{cname}"
            comp_prov = prov.get(prov_key, {})
            source = comp_prov.get("source", "unknown")
            brief_val = comp_prov.get("brief_value", 0) or 0

            variants.append(VariantDriver(
                driver=driver.driver_name,
                component=cname,
                our_value=comp.value,
                consensus_value=consensus_val,
                delta=delta,
                eps_contribution=eps_contribution,
                pct_of_total=0,  # computed below
                confidence=comp.confidence,
                basis=comp.basis,
                source=source,
                brief_value=brief_val,
            ))

    # Compute % of total variant
    total_variant = sum(abs(v.eps_contribution) for v in variants)
    if total_variant > 0:
        for var in variants:
            var.pct_of_total = abs(var.eps_contribution) / total_variant * 100

    # Sort by absolute EPS contribution
    variants.sort(key=lambda x: abs(x.eps_contribution), reverse=True)

    if v:
        v(f"  Edge: {len(variants)} variant drivers identified")
        for var in variants[:3]:
            v(f"    {var.driver}.{var.component}: ours={var.our_value:+.1f} "
              f"street={var.consensus_value:+.1f} -> EPS ${var.eps_contribution:+.4f} "
              f"({var.pct_of_total:.0f}%)")

    return variants


def score_confidence(variants, brief, v=None):
    """
    Score evidence quality for each variant driver.
    Adjusts confidence based on contradictions and evidence density.
    """
    # Map contradictions to affected drivers
    contradiction_targets = {}
    for contra in (brief.contradictions or []):
        target = contra.get("affected_driver", "")
        severity = contra.get("severity", "minor")
        if target:
            contradiction_targets.setdefault(target, []).append(severity)

    for var in variants:
        # Base confidence from driver component
        base = var.confidence

        # Contradiction penalty
        contras = contradiction_targets.get(var.driver, [])
        serious = sum(1 for s in contras if s == "serious")
        moderate = sum(1 for s in contras if s == "moderate")
        penalty = serious * 0.15 + moderate * 0.05
        adjusted = max(base - penalty, 0.10)

        # Evidence strength
        if adjusted >= 0.65:
            var.evidence_strength = "strong"
        elif adjusted >= 0.45:
            var.evidence_strength = "moderate"
        else:
            var.evidence_strength = "weak"

        var.confidence = round(adjusted, 2)

    return variants


def assess_priced_in(variant_pct, setup=None, v=None):
    """
    Check if the variant is already reflected in the stock price.
    Uses market overlay data when available.
    """
    if not setup:
        # No market data -- conservative assumption
        if v:
            v("  Edge: no market data for priced-in check")
        return PricedInAssessment(
            likely_priced_in=False,
            confidence=0.3,
            reasoning="No market data available -- cannot assess",
        )

    implied_move = setup.get("implied_move_pct", 5.0)
    short_pct = setup.get("short_interest_pct", 0)
    is_bullish = variant_pct > 0

    # Variant within noise?
    within_noise = abs(variant_pct) < implied_move * 0.3

    # Short interest alignment
    short_signal = "neutral"
    if short_pct > 10 and is_bullish:
        short_signal = "supports"  # market positioned against us -- edge may exist
    elif short_pct < 3 and is_bullish:
        short_signal = "contradicts"  # market already agrees -- may be priced in

    likely_priced_in = within_noise
    if short_signal == "supports":
        likely_priced_in = False  # market disagrees, so our view isn't priced in

    reasoning_parts = []
    if within_noise:
        reasoning_parts.append(f"Variant ({variant_pct:+.1f}%) within implied move noise ({implied_move:.1f}%)")
    else:
        reasoning_parts.append(f"Variant ({variant_pct:+.1f}%) exceeds implied move ({implied_move:.1f}%)")
    if short_pct > 5:
        reasoning_parts.append(f"Short interest {short_pct:.1f}% {'supports' if short_signal == 'supports' else 'neutral'}")

    if v:
        v(f"  Edge: priced-in={likely_priced_in}, short={short_signal}")

    return PricedInAssessment(
        likely_priced_in=likely_priced_in,
        confidence=0.5 if not setup else 0.6,
        reasoning=". ".join(reasoning_parts),
        short_signal=short_signal,
    )


def identify_catalysts(brief, registry_data=None, consensus_data=None, v=None):
    """
    Identify upcoming events that would prove/disprove the thesis.
    Uses real earnings dates when available from consensus_data.
    Classifies each catalyst's resolution window.
    """
    from datetime import date, datetime

    catalysts = []

    # Compute real earnings proximity
    earnings_date_str = None
    earnings_proximity = 60  # default
    if consensus_data:
        earnings_date_str = consensus_data.get("earnings_date")
    if earnings_date_str:
        try:
            ed = datetime.strptime(earnings_date_str, "%Y-%m-%d").date()
            earnings_proximity = max((ed - date.today()).days, 1)
        except (ValueError, TypeError):
            pass

    # Next earnings is always a catalyst
    if earnings_date_str:
        timeframe_str = f"{earnings_date_str} ({earnings_proximity} days)"
    else:
        timeframe_str = f"~{earnings_proximity} days"

    catalysts.append(Catalyst(
        event="Next quarterly earnings report",
        timeframe=timeframe_str,
        resolves_driver="revenue + margins + guidance",
        impact="confirms or challenges",
        proximity_days=earnings_proximity,
        resolution_window="next_quarter",
        actual_date=earnings_date_str or "",
    ))

    # Classify each driver's resolution window
    SHORT_TERM_DRIVERS = {
        "sss_growth", "sss_growth_pct", "revenue_growth", "net_retention_pct",
        "food_cost", "labor_cost", "food_cost_delta_bps", "labor_cost_delta_bps",
        "cogs_delta_bps", "operating_margin",
    }
    MULTI_QUARTER_DRIVERS = {
        "new_restaurants", "new_stores", "new_arr_growth_pct",
        "rd_delta_bps", "sm_delta_bps",
    }
    MULTI_YEAR_DRIVERS = {
        "market_share", "competitive_position", "regulatory",
        "secular_trend", "technology_shift",
    }

    # Contradictions as catalysts with proper resolution windows
    for contra in (brief.contradictions or [])[:3]:
        driver = contra.get("affected_driver", "")
        thesis = contra.get("thesis", "")[:80]

        if driver in SHORT_TERM_DRIVERS or any(k in driver for k in ["revenue", "margin", "cost", "sss"]):
            window = "next_quarter"
            prox = earnings_proximity
            timeframe = f"Resolves at earnings ({earnings_date_str or '~60 days'})"
        elif driver in MULTI_QUARTER_DRIVERS or any(k in driver for k in ["store", "arr", "new_"]):
            window = "next_annual"
            prox = max(earnings_proximity * 2, 120)
            timeframe = "Resolves over 2-4 quarters"
        else:
            window = "multi_year"
            prox = 365
            timeframe = "Multi-year thesis -- patience required"

        catalysts.append(Catalyst(
            event=f"Resolution: {thesis}",
            timeframe=timeframe,
            resolves_driver=driver,
            impact="confirms or challenges",
            proximity_days=prox,
            resolution_window=window,
        ))

    # Evidence gaps as catalysts
    for gap in (brief.evidence_gaps or [])[:2]:
        catalysts.append(Catalyst(
            event=gap[:100],
            timeframe="Unknown timing",
            resolves_driver="multiple",
            impact="resolves uncertainty",
            proximity_days=90,
            resolution_window="next_quarter",
        ))

    if v:
        v(f"  Edge: {len(catalysts)} catalysts identified")
        for c in catalysts[:2]:
            v(f"    {c.event[:50]} [{c.resolution_window}] {c.proximity_days}d")

    return catalysts


def compute_verdict(variants, priced_in, catalysts, variant_eps, variant_pct,
                    consensus_eps, our_eps, v=None):
    """
    Synthesize everything into a final verdict.

    actionability = magnitude * confidence * (1 - priced_in) * catalyst_weight
    """
    # Magnitude: how big is the variant relative to consensus
    magnitude = min(abs(variant_pct) / 10.0, 1.0)  # 10% variant = max score

    # Confidence: weighted by EPS contribution AND confidence level
    # Higher-confidence drivers get more weight in the score
    # This means an edge driven by a high-confidence driver scores
    # higher than one driven by a low-confidence driver of equal size
    total_weighted = sum(abs(var.eps_contribution) * var.confidence for var in variants)
    total_contrib = sum(abs(var.eps_contribution) for var in variants)
    if total_weighted > 0:
        # Confidence-squared weighting: high-confidence outcomes dominate
        weighted_conf = sum(
            var.confidence * var.confidence * abs(var.eps_contribution) / total_contrib
            for var in variants
        ) if total_contrib > 0 else 0.5
    else:
        weighted_conf = 0.5

    # Priced-in penalty
    priced_in_score = 0.8 if priced_in.likely_priced_in else 0.2

    # Catalyst proximity
    if catalysts:
        min_days = min(c.proximity_days for c in catalysts)
        if min_days < 30:
            catalyst_weight = 1.0
        elif min_days < 90:
            catalyst_weight = 0.7
        else:
            catalyst_weight = 0.4
    else:
        catalyst_weight = 0.3

    actionability = magnitude * weighted_conf * (1 - priced_in_score) * catalyst_weight

    # Verdict thresholds
    if actionability >= 0.12:
        verdict = "ACTIONABLE_EDGE"
    elif actionability >= 0.06:
        verdict = "PROBABLE_EDGE"
    elif actionability >= 0.02:
        verdict = "POSSIBLE_EDGE"
    else:
        verdict = "NO_CLEAR_EDGE"

    # Build narrative
    direction = "above" if variant_eps > 0 else "below"
    top_driver = variants[0] if variants else None

    narrative_parts = [
        f"Our estimate is ${abs(variant_eps):.2f} ({abs(variant_pct):.1f}%) {direction} consensus ${consensus_eps:.2f}.",
    ]
    if top_driver:
        narrative_parts.append(
            f"Primary driver: {top_driver.driver}.{top_driver.component} "
            f"(ours {top_driver.our_value:+.1f} vs street {top_driver.consensus_value:+.1f}, "
            f"contributing ${top_driver.eps_contribution:+.3f} to EPS variant, "
            f"evidence: {top_driver.evidence_strength})."
        )
    if priced_in.likely_priced_in:
        narrative_parts.append("Variant appears within market noise -- may already be priced in.")
    else:
        narrative_parts.append("Variant appears NOT fully priced in based on available market data.")
    if catalysts:
        narrative_parts.append(f"Next catalyst: {catalysts[0].event} ({catalysts[0].timeframe}).")

    if v:
        v(f"  Edge: verdict={verdict} (score={actionability:.3f})")
        v(f"    magnitude={magnitude:.2f} conf={weighted_conf:.2f} "
          f"priced_in={priced_in_score:.2f} catalyst={catalyst_weight:.2f}")

    # Determine time horizon from catalyst proximity
    if catalysts:
        min_days = min(c.proximity_days for c in catalysts)
        if min_days <= 30:
            time_horizon = "trade"
        elif min_days <= 90:
            time_horizon = "swing"
        else:
            time_horizon = "position"
        # Also check resolution windows
        windows = [c.resolution_window for c in catalysts if c.resolution_window]
        if "multi_year" in windows and "next_quarter" not in windows:
            time_horizon = "position"
    else:
        time_horizon = "position"

    narrative_parts.append(f"Time horizon: {time_horizon}.")

    return EdgeAssessment(
        verdict=verdict,
        actionability_score=actionability,
        variant_pct=variant_pct,
        variant_eps=variant_eps,
        variants=variants,
        priced_in=priced_in,
        catalysts=catalysts,
        edge_narrative=" ".join(narrative_parts),
        time_horizon=time_horizon,
    )


