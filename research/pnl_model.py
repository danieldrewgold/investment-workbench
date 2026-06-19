"""
P&L Model — single source of truth for forward EPS.

The old pipeline produced EPS via two paths that drifted apart:
  1. The brief's `edge_claims` carried `eps_impact` values
  2. The mechanical `DynamicModel` ran `brief.drivers` through a sector
     schema and produced an EPS that ignored the brief's stated conviction

Result: WING brief said "+3% above consensus revenue" but mechanical model
output 50%+ below. Run-to-run EPS swings of 30%+. Reader had no way to
trust either number.

This module replaces that with a single coherent path:

    baseline_EPS  = consensus_EPS  (from yfinance current FY)
                OR guidance_midpoint  (when management gave EPS guidance)

    For each edge_claim:
        delta = our_value - anchor_value
        eps_impact = flow_through(delta, line_hit, baseline)

    our_EPS = baseline_EPS + Σ(eps_impacts)

Five flow-through formulas, five lines each. No black box, no schema
multiplication chain. Every line item is traceable from EPS back to
the specific edge_claim and the published anchor it disagrees with.

Public API:
    build_baseline(financials, consensus_full, guidance_bundle) -> BaselinePnL
    apply_edge_claim(claim, baseline) -> ClaimImpact
    compute_our_eps(baseline, edge_claims) -> EpsBuild
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


# Incremental margin model: variable-cost businesses see incremental
# drops higher than reported because fixed costs don't scale with
# marginal revenue. Multiplier reflects this; cap prevents runaway
# flow-through math.
#
# The cap is TIERED based on the company's current operating margin:
#   • Op margin < 30% → incremental capped at 50% (typical industrial,
#     mid-cycle staples)
#   • Op margin 30-60% → cap lifts to 70% (higher-margin software,
#     consumer staples giants, banks)
#   • Op margin > 60% → cap lifts to 85% (pure asset-light ad-tech /
#     software with massive operating leverage like APP at 82% adj
#     EBITDA, MSFT, GOOG advertising)
#
# Without the tiered cap, APP-like names (op margin 75%+) get their
# revenue flow-through dramatically undercounted — a $1B revenue miss
# on APP truly drops $700-800M to operating income, not $500M.
_INCREMENTAL_MARGIN_MULTIPLIER = 1.2
_INCREMENTAL_MARGIN_CAP_LOW = 0.50      # op margin < 30%
_INCREMENTAL_MARGIN_CAP_MID = 0.70      # op margin 30-60%
_INCREMENTAL_MARGIN_CAP_HIGH = 0.85     # op margin > 60%

# Floor on operating margin used in flow-through. Some companies report
# negative margins (early-stage software) but we don't want flow-through
# math to produce negative incremental margin.
_MIN_INCREMENTAL_MARGIN = 0.05


@dataclass
class BaselinePnL:
    """
    Forward-looking baseline income statement, anchored to consensus
    or guidance and back-filled with prior-year ratios. This is the
    starting point for the EPS bridge.
    """
    revenue: float = 0.0              # forward FY revenue, in dollars
    eps: float = 0.0                  # forward FY EPS (consensus mean or guidance mid)
    net_income: float = 0.0           # eps × shares
    operating_income: float = 0.0     # revenue × op margin
    operating_margin: float = 0.0     # decimal (0.18 = 18%)
    incremental_margin: float = 0.0   # decimal — variable contribution margin
    pretax_income: float = 0.0        # net_income / (1 - tax_rate)
    tax_rate: float = 0.21            # decimal
    shares: float = 0.0               # diluted shares outstanding (in millions)
    anchor_source: str = ""           # "consensus_current_fy" | "guidance_fy_eps" | etc.
    anchor_label: str = ""            # human-readable: "FY2026 consensus (yfinance)"
    # Diagnostic explaining the validity outcome — empty string OR "ok" when
    # is_valid() is True; otherwise a comma-separated list of which fields
    # failed (e.g. "revenue=0; shares=0"). Populated by build_baseline().
    # Surfaces in pipeline warnings when the bridge falls back to mechanical.
    validity_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def is_valid(self) -> bool:
        """Sanity check — baseline is usable if revenue + shares > 0."""
        return self.revenue > 0 and self.shares > 0 and self.eps != 0


@dataclass
class ClaimImpact:
    """One edge_claim's mechanical EPS impact via flow-through."""
    claim_anchor_type: str = ""
    claim_line_hit: str = ""              # "revenue" | "margin" | "opex" | "tax" | "share_count"
    claim_anchor_value: float = 0.0
    claim_our_value: float = 0.0
    delta: float = 0.0                    # our_value - anchor_value (signed)
    eps_impact: float = 0.0               # mechanical flow-through result
    claude_eps_impact: float = 0.0        # what Claude estimated (for cross-check)
    impact_mismatch: bool = False         # True when claude_eps_impact diverges >25% from mechanical
    rationale: str = ""                   # short explanation of the impact direction


@dataclass
class EpsBuild:
    """
    Complete EPS bridge: baseline + per-claim impacts → our EPS.
    Renderable directly as a Word doc bridge table.
    """
    baseline: BaselinePnL = field(default_factory=BaselinePnL)
    claim_impacts: list = field(default_factory=list)     # list[ClaimImpact]
    our_eps: float = 0.0                                  # baseline.eps + Σ(claim.eps_impact)
    sum_eps_impact: float = 0.0                           # Σ(claim.eps_impact)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline.to_dict(),
            "claim_impacts": [asdict(c) if hasattr(c, "__dataclass_fields__") else c
                               for c in self.claim_impacts],
            "our_eps": self.our_eps,
            "sum_eps_impact": self.sum_eps_impact,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
# Baseline construction
# --------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    """Coerce to float, returning default on None/non-numeric."""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def build_baseline(financials, consensus_full: dict | None,
                    guidance_bundle=None) -> BaselinePnL:
    """
    Build the forward-year baseline P&L. Anchors revenue and EPS to
    published consensus (or guidance midpoint when given), then back-fills
    line items using prior-year ratios from `financials`.

    Returns a BaselinePnL with `is_valid()` False if we can't anchor —
    caller must check before using.
    """
    cf = consensus_full or {}
    cy = cf.get("current_year") or {}

    # Primary anchor: consensus current FY
    consensus_revenue = _safe_float(cy.get("revenue_mean"))   # in dollars
    consensus_eps     = _safe_float(cy.get("eps_mean"))

    # Override with guidance if a midpoint exists for the matching period
    guide_revenue = None
    guide_eps = None
    guide_revenue_growth_pct = None  # parsed from a "Revenue: 0% to 1%" guide
    if guidance_bundle is not None and getattr(guidance_bundle, "items", None):
        for item in guidance_bundle.items:
            metric = (item.metric or "").lower()
            period = (item.period or "").lower()
            # Prefer FY guidance over Q guidance for the baseline
            if "fy" in period or "fiscal" in period:
                if metric == "revenue" and item.midpoint() is not None:
                    if item.value_unit == "%":
                        # Growth-percent guidance: keep as multiplier for fallback
                        guide_revenue_growth_pct = item.midpoint() / 100.0
                    elif item.value_unit == "$M":
                        guide_revenue = item.midpoint() * 1e6
                    elif item.value_unit == "$B":
                        guide_revenue = item.midpoint() * 1e9
                    else:
                        guide_revenue = item.midpoint()
                if metric in ("eps", "adj_eps") and item.midpoint() is not None:
                    guide_eps = item.midpoint()

    # Pick anchors with provenance. If the consensus revenue mean isn't
    # in yfinance (common — analysts publish EPS estimates more reliably
    # than revenue estimates), fall back to prior-year revenue × growth.
    # Without this fallback, is_valid() returns False and the whole bridge
    # collapses to the mechanical model — which on a thin-driver brief
    # produces nonsense (PRMB ran $-0.29 vs consensus $+1.31).
    if guide_revenue is not None:
        revenue = guide_revenue
        rev_source = "guidance_fy_revenue"
        rev_label = "FY guidance midpoint"
    elif consensus_revenue > 0:
        revenue = consensus_revenue
        rev_source = "consensus_current_fy"
        rev_label = "FY consensus revenue"
    else:
        prior_revenue_m = _safe_float(getattr(financials, "revenue_m", 0))
        if prior_revenue_m > 0:
            growth = guide_revenue_growth_pct if guide_revenue_growth_pct is not None else 0.0
            revenue = prior_revenue_m * 1e6 * (1.0 + growth)
            if guide_revenue_growth_pct is not None:
                rev_source = "fallback_prior_year_x_guidance_growth"
                rev_label = (
                    f"Prior-year revenue × (1 + {growth*100:.1f}% guidance growth) "
                    f"— consensus revenue mean missing"
                )
            else:
                rev_source = "fallback_prior_year_flat"
                rev_label = (
                    "Prior-year revenue, flat (consensus revenue mean and "
                    "guidance growth both missing)"
                )
        else:
            revenue = 0.0
            rev_source = "missing"
            rev_label = "no anchor (consensus, guidance, or prior-year all missing)"

    if guide_eps is not None:
        eps = guide_eps
        eps_source = "guidance_fy_eps"
        eps_label = "FY guidance EPS midpoint"
    else:
        eps = consensus_eps
        eps_source = "consensus_current_fy"
        eps_label = "FY consensus EPS"

    # Shares from prior-year financials (most reliable)
    shares = _safe_float(getattr(financials, "diluted_shares_m", 0))
    if shares > 0:
        shares = shares * 1e6   # convert millions → absolute

    # Net income = EPS × shares
    net_income = eps * shares if shares > 0 and eps != 0 else 0.0

    # Tax rate from prior-year actuals; default 21% (US corp rate)
    tax_rate = _safe_float(getattr(financials, "tax_rate", 0))
    if tax_rate <= 0 or tax_rate >= 0.6:
        tax_rate = 0.21

    # Pretax income = net_income / (1 - tax)
    pretax_income = net_income / (1 - tax_rate) if tax_rate < 0.99 else net_income

    # Operating margin from prior-year actuals (op_margin_pct stored as %)
    op_margin = _safe_float(getattr(financials, "operating_margin_pct", 0)) / 100.0
    if op_margin <= 0:
        op_margin = 0.10   # generic 10% default

    operating_income = revenue * op_margin

    # Incremental margin: variable-cost contribution. Higher than reported
    # because fixed costs don't scale with marginal revenue. Cap is
    # tiered to op margin so APP-like (75%+ op margin) businesses get
    # realistic flow-through, not the conservative 50% that fits banks
    # or industrials.
    if op_margin > 0.60:
        cap = _INCREMENTAL_MARGIN_CAP_HIGH
    elif op_margin > 0.30:
        cap = _INCREMENTAL_MARGIN_CAP_MID
    else:
        cap = _INCREMENTAL_MARGIN_CAP_LOW
    incremental_margin = max(
        op_margin * _INCREMENTAL_MARGIN_MULTIPLIER,
        _MIN_INCREMENTAL_MARGIN,
    )
    incremental_margin = min(incremental_margin, cap)

    # Diagnostic: explain WHY is_valid would fail, so the pipeline can
    # surface a useful warning instead of "baseline invalid (no consensus)".
    reasons: list[str] = []
    if revenue <= 0:
        reasons.append(
            f"revenue=0 (consensus_revenue={consensus_revenue:.0f}, "
            f"guide_revenue={guide_revenue}, prior-year financials missing)"
        )
    if shares <= 0:
        reasons.append("shares=0 (financials.diluted_shares_m missing)")
    if eps == 0:
        reasons.append(
            f"eps=0 (consensus_eps={consensus_eps}, guide_eps={guide_eps})"
        )
    validity_reason = "; ".join(reasons) if reasons else "ok"

    return BaselinePnL(
        revenue=revenue,
        eps=eps,
        net_income=net_income,
        operating_income=operating_income,
        operating_margin=op_margin,
        incremental_margin=incremental_margin,
        pretax_income=pretax_income,
        tax_rate=tax_rate,
        shares=shares,
        anchor_source=f"rev:{rev_source}, eps:{eps_source}",
        anchor_label=f"Revenue: {rev_label}; EPS: {eps_label}",
        validity_reason=validity_reason,
    )


# --------------------------------------------------------------------------
# Flow-through math — five formulas, one per line_hit
# --------------------------------------------------------------------------

def flow_through(delta: float, line_hit: str, baseline: BaselinePnL) -> float:
    """
    Compute EPS impact of a delta on a specific income-statement line.

    Args:
        delta: signed difference (our_value - anchor_value). Units depend
            on line_hit:
              - revenue: dollars (e.g. -100e6 = $100M revenue miss)
              - margin: decimal pp (e.g. 0.02 = +200 bps margin)
              - opex: dollars (positive = opex up = EPS down)
              - tax: decimal pp (positive = higher tax rate)
              - share_count: shares (positive = more shares = lower EPS)
        line_hit: which P&L line the delta affects.
        baseline: BaselinePnL from build_baseline().

    Returns: EPS impact in dollars per share.
    """
    if not baseline.is_valid() or delta == 0:
        return 0.0

    lh = (line_hit or "").lower().strip()

    if lh == "revenue":
        # Revenue delta × incremental margin × after-tax / shares
        return delta * baseline.incremental_margin * (1 - baseline.tax_rate) / baseline.shares

    if lh == "margin":
        # Margin pp delta × revenue × after-tax / shares
        return baseline.revenue * delta * (1 - baseline.tax_rate) / baseline.shares

    if lh == "opex":
        # Opex up = operating income down (negative sign on delta)
        return -delta * (1 - baseline.tax_rate) / baseline.shares

    if lh == "tax":
        # Tax rate pp delta — applied to pretax income
        return -baseline.pretax_income * delta / baseline.shares

    if lh == "share_count":
        # New shares = baseline shares + delta. EPS shifts inversely.
        new_shares = baseline.shares + delta
        if new_shares <= 0:
            return 0.0
        old_eps = baseline.net_income / baseline.shares
        new_eps = baseline.net_income / new_shares
        return new_eps - old_eps

    if lh in ("eps", "direct"):
        # Direct EPS delta — used when the anchor IS EPS (consensus_fy_eps,
        # guidance_fy_eps, etc.) so the delta is already in per-share dollars
        # and no flow-through math is needed.
        return delta

    return 0.0


def _infer_line_hit_from_anchor_type(anchor_type: str) -> str:
    """
    Map an edge_claim's anchor_type to the right flow-through line_hit.
    This is a fallback when Claude doesn't supply line_hit explicitly,
    AND it's used to OVERRIDE Claude's line_hit when it's clearly wrong
    (e.g. attacking an EPS anchor with line_hit=revenue).
    """
    at = (anchor_type or "").lower()
    if "eps" in at:
        # EPS-anchored claims: delta is already in per-share dollars.
        # Use direct pass-through, not revenue flow-through.
        return "eps"
    if "revenue" in at:
        return "revenue"
    if "ebitda" in at or "operating_income" in at or "operating_margin" in at \
       or "gross_margin" in at or "margin" in at:
        return "margin"
    if "tax" in at:
        return "tax"
    if "share" in at or "buyback" in at:
        return "share_count"
    if "capex" in at or "sga" in at or "opex" in at:
        return "opex"
    return "revenue"   # most common default


def _line_hit_compatible_with_anchor(line_hit: str, anchor_type: str) -> bool:
    """
    Sanity check: is this line_hit even plausibly correct for the anchor?
    If Claude says line_hit=revenue on a consensus_fy_eps anchor, that's
    clearly wrong (EPS isn't a revenue line). Override using inference.
    """
    inferred = _infer_line_hit_from_anchor_type(anchor_type)
    if inferred == line_hit:
        return True
    # Allow margin↔revenue substitution (margin claims can be expressed
    # either way and Claude's wording sometimes blurs the distinction).
    if {inferred, line_hit} <= {"revenue", "margin"}:
        return True
    return False


# --------------------------------------------------------------------------
# Apply edge_claims → EPS bridge
# --------------------------------------------------------------------------

def apply_edge_claim(claim: dict, baseline: BaselinePnL) -> ClaimImpact:
    """Compute one edge_claim's mechanical EPS impact via flow-through."""
    anchor_type = claim.get("anchor_type", "")
    anchor_value = _safe_float(claim.get("anchor_value", 0))
    our_value = _safe_float(claim.get("our_value", 0))
    raw_line_hit = claim.get("line_hit", "") or ""
    # Override Claude's line_hit if it's incompatible with the anchor_type
    # (e.g. EPS anchor with line_hit=revenue → forces eps direct passthrough).
    if raw_line_hit and _line_hit_compatible_with_anchor(raw_line_hit, anchor_type):
        line_hit = raw_line_hit
    else:
        line_hit = _infer_line_hit_from_anchor_type(anchor_type)
    claude_impact = _safe_float(claim.get("eps_impact", 0))

    delta = our_value - anchor_value
    eps_impact = flow_through(delta, line_hit, baseline)

    # Cross-check: flag if Claude's stated impact is materially different
    # from the mechanical computation (>25% relative or >$0.10 absolute on
    # claims with a meaningful base).
    mismatch = False
    if abs(claude_impact) > 0.05 or abs(eps_impact) > 0.05:
        rel_err = (abs(claude_impact - eps_impact)
                   / max(abs(eps_impact), 0.05))
        if rel_err > 0.25 and abs(claude_impact - eps_impact) > 0.10:
            mismatch = True

    rationale = ""
    if line_hit == "revenue":
        rationale = (f"Revenue Δ ${delta/1e6:+,.0f}M × {baseline.incremental_margin*100:.0f}% "
                     f"incremental margin × {(1-baseline.tax_rate)*100:.0f}% after-tax")
    elif line_hit == "margin":
        rationale = (f"Margin Δ {delta*100:+.0f}bps on ${baseline.revenue/1e6:,.0f}M "
                     f"revenue × {(1-baseline.tax_rate)*100:.0f}% after-tax")
    elif line_hit == "opex":
        rationale = (f"Opex Δ ${delta/1e6:+,.0f}M × {(1-baseline.tax_rate)*100:.0f}% after-tax")
    elif line_hit == "tax":
        rationale = (f"Tax rate Δ {delta*100:+.1f}pp on ${baseline.pretax_income/1e6:,.0f}M pretax")
    elif line_hit == "share_count":
        rationale = (f"Share count Δ {delta/1e6:+,.1f}M shares")
    elif line_hit in ("eps", "direct"):
        rationale = f"Direct EPS delta: ${delta:+.2f}/share"

    return ClaimImpact(
        claim_anchor_type=anchor_type,
        claim_line_hit=line_hit,
        claim_anchor_value=anchor_value,
        claim_our_value=our_value,
        delta=delta,
        eps_impact=eps_impact,
        claude_eps_impact=claude_impact,
        impact_mismatch=mismatch,
        rationale=rationale,
    )


def _claim_targets_period(anchor_type: str, baseline_period: str) -> bool:
    """
    Does this claim's anchor target the same fiscal period as the baseline?

    Baseline is current_fy by default, so claims attacking
    `consensus_next_fy_*` or `guidance_next_fy_*` shouldn't be summed onto
    the current-FY EPS — they're a separate forecast horizon.

    Returns True when the claim's anchor period matches OR when the
    anchor isn't period-specific (e.g. consensus_ltg, consensus_price_target).
    """
    at = (anchor_type or "").lower()
    bp = (baseline_period or "").lower()
    is_next_fy = "next_fy" in at
    is_next_q = "next_q" in at and "next_quarter" not in at
    is_q = "_q_" in at or at.endswith("_q")
    is_ltg_or_pt = "ltg" in at or "price_target" in at
    if "next_fy" in bp:
        return is_next_fy or is_ltg_or_pt
    if "current_fy" in bp or "fy" in bp or not bp:
        # Baseline is current FY (default). Skip next-FY and quarterly claims
        # for the current-FY EPS calculation. LTG / PT claims have no period
        # so they pass through.
        if is_next_fy:
            return False
        if is_q:
            return False
        return True
    return True


def _claims_mechanically_overlap(claim_a: dict, claim_b: dict) -> bool:
    """
    Two claims mechanically overlap when they target the same fiscal
    period AND attack mechanically-related anchors (revenue → operating
    margin → EBITDA → EPS chain).

    Example: a `consensus_fy_revenue` claim (revenue $X below) and a
    `consensus_fy_eps` claim (EPS $Y below because of revenue + leverage)
    are the same disagreement viewed through different metric lenses.
    Summing them double-counts.

    Different periods (current_fy vs next_fy) don't overlap.
    Different mechanisms (revenue vs tax_rate) don't overlap.
    """
    at_a = (claim_a.get("anchor_type") or "").lower()
    at_b = (claim_b.get("anchor_type") or "").lower()

    # Period gate: must be same period (current FY vs next FY don't overlap)
    def _period_key(at: str) -> str:
        if "next_q" in at: return "next_q"
        if "next_fy" in at: return "next_fy"
        if "_q_" in at or at.endswith("_q"): return "q"
        if "ltg" in at or "price_target" in at: return "long"
        return "fy"

    if _period_key(at_a) != _period_key(at_b):
        return False

    # Mechanism gate: revenue → margin → EPS chain are linked.
    # Tax rate, share count, and opex line items are independent
    # mechanisms (don't overlap with revenue/margin/EPS chain).
    chain_metrics = ("revenue", "ebitda", "ebit", "operating_income",
                     "operating_margin", "gross_margin", "net_income", "eps")
    a_in_chain = any(m in at_a for m in chain_metrics)
    b_in_chain = any(m in at_b for m in chain_metrics)
    if not (a_in_chain and b_in_chain):
        return False

    # Same period + both in revenue→EPS chain → overlap
    return True


def _select_authoritative_claim(claims: list) -> tuple:
    """
    From a group of mechanically-overlapping claims, pick the one to
    apply. Strategy: prefer the most-comprehensive metric (EPS captures
    more than revenue alone), and within ties prefer the larger absolute
    impact (more conservative).

    Returns (selected_claim, displaced_claims_list).
    """
    if not claims:
        return None, []
    if len(claims) == 1:
        return claims[0], []

    # Comprehensiveness ranking: EPS > Net income > EBIT > EBITDA > Op margin > Revenue
    rank_map = {
        "eps":              7,
        "net_income":       6,
        "operating_income": 5,
        "ebit":             5,
        "ebitda":           4,
        "operating_margin": 3,
        "gross_margin":     2,
        "revenue":          1,
    }

    def _rank(claim):
        at = (claim.get("anchor_type") or "").lower()
        for kw, score in rank_map.items():
            if kw in at:
                return score
        return 0

    # Sort: highest rank first, then larger |our_value - anchor_value| first
    def _impact_size(claim):
        try:
            return abs(float(claim.get("our_value", 0)) - float(claim.get("anchor_value", 0)))
        except (TypeError, ValueError):
            return 0

    sorted_claims = sorted(claims, key=lambda c: (-_rank(c), -_impact_size(c)))
    selected = sorted_claims[0]
    displaced = sorted_claims[1:]
    return selected, displaced


def compute_our_eps(baseline: BaselinePnL, edge_claims: list) -> EpsBuild:
    """
    Build the complete EPS bridge from baseline + edge_claims.

    Filters edge_claims to those whose anchor period matches the baseline
    period (default: current FY). Claims attacking other periods are
    recorded but not summed into our_eps.

    Detects mechanically-overlapping claims (e.g. consensus_fy_revenue +
    consensus_fy_eps for the same period are the same disagreement
    through different lenses) and only counts the most-comprehensive
    one to prevent double-counting.

    Returns an EpsBuild that's ready to render (each impact carries
    its own rationale + cross-check status). If baseline is invalid
    (no consensus available), returns an empty bridge with a warning.
    """
    build = EpsBuild(baseline=baseline)

    if not baseline.is_valid():
        build.warnings.append(
            "Baseline P&L could not be constructed (missing consensus or "
            "shares); EPS build degraded to baseline EPS only."
        )
        build.our_eps = baseline.eps
        return build

    if not edge_claims:
        # No edge — our EPS == consensus EPS, honest output
        build.our_eps = baseline.eps
        build.warnings.append("No edge claims; our EPS matches baseline (consensus).")
        return build

    # Determine baseline period — current_fy unless anchor_source says otherwise
    baseline_period = "current_fy"
    if "next_fy" in (baseline.anchor_source or "").lower():
        baseline_period = "next_fy"

    # Pass 1: filter by period (drop claims targeting other forecast horizons)
    in_period_claims = []
    skipped_claims = []
    for claim in edge_claims:
        if not isinstance(claim, dict):
            continue
        anchor_type = claim.get("anchor_type", "")
        if not _claim_targets_period(anchor_type, baseline_period):
            skipped_claims.append(anchor_type)
            continue
        in_period_claims.append(claim)

    # Pass 2: group mechanically-overlapping claims; within each group
    # only the most-comprehensive claim contributes to the EPS sum.
    # Displaced claims are recorded as supporting impacts (rendered in
    # the bridge with a "(supporting view)" marker) but not summed.
    overlap_groups = []
    used = set()
    for i, claim in enumerate(in_period_claims):
        if i in used:
            continue
        group = [claim]
        used.add(i)
        for j in range(i + 1, len(in_period_claims)):
            if j in used:
                continue
            if _claims_mechanically_overlap(claim, in_period_claims[j]):
                group.append(in_period_claims[j])
                used.add(j)
        overlap_groups.append(group)

    sum_impact = 0.0
    for group in overlap_groups:
        selected, displaced = _select_authoritative_claim(group)
        if selected is None:
            continue
        impact = apply_edge_claim(selected, baseline)
        build.claim_impacts.append(impact)
        sum_impact += impact.eps_impact
        if impact.impact_mismatch:
            build.warnings.append(
                f"Claim impact mismatch on {impact.claim_anchor_type}: "
                f"Claude said ${impact.claude_eps_impact:+.2f}, "
                f"flow-through computes ${impact.eps_impact:+.2f}"
            )
        # Record displaced (overlapping) claims as supporting impacts —
        # rendered in the bridge but NOT summed (already represented
        # mechanically by the selected claim).
        for d_claim in displaced:
            d_impact = apply_edge_claim(d_claim, baseline)
            d_impact.rationale = (
                f"(supporting view, not summed: same disagreement as "
                f"'{selected.get('anchor_type')}' through different metric lens) "
                f"{d_impact.rationale}"
            )
            d_impact.eps_impact = 0.0   # mark as zero-contribution to make summing safe
            build.claim_impacts.append(d_impact)
            build.warnings.append(
                f"Mechanically-overlapping claim deduplicated: "
                f"'{d_claim.get('anchor_type')}' overlaps with selected "
                f"'{selected.get('anchor_type')}'; only the more comprehensive "
                f"claim is summed into our_eps."
            )

    build.sum_eps_impact = sum_impact
    build.our_eps = baseline.eps + sum_impact

    if skipped_claims:
        build.warnings.append(
            f"{len(skipped_claims)} claim(s) target a different fiscal period "
            f"than the {baseline_period} baseline and weren't summed: "
            f"{', '.join(skipped_claims[:3])}"
        )

    # Sanity check: if the sum is more than 50% of baseline, flag for review
    if abs(sum_impact) > 0.5 * abs(baseline.eps) and abs(baseline.eps) > 0.1:
        build.warnings.append(
            f"Aggregate edge impact ${sum_impact:+.2f} exceeds 50% of baseline "
            f"EPS ${baseline.eps:.2f} — review for compounding errors."
        )

    return build
