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


# Default incremental margin uplift over reported operating margin.
# Variable-cost businesses see incremental drops higher than reported
# because fixed costs don't scale with marginal revenue. 1.2x is a
# conservative default; capped at 50% to avoid absurd flow-through.
_INCREMENTAL_MARGIN_MULTIPLIER = 1.2
_INCREMENTAL_MARGIN_CAP = 0.50

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
    if guidance_bundle is not None and getattr(guidance_bundle, "items", None):
        for item in guidance_bundle.items:
            metric = (item.metric or "").lower()
            period = (item.period or "").lower()
            # Prefer FY guidance over Q guidance for the baseline
            if "fy" in period or "fiscal" in period:
                if metric == "revenue" and item.midpoint() is not None:
                    # Convert from $M (guidance bundle stores in $M) to dollars
                    guide_revenue = item.midpoint() * 1e6 if item.value_unit == "$M" else \
                                     (item.midpoint() * 1e9 if item.value_unit == "$B" else item.midpoint())
                if metric in ("eps", "adj_eps") and item.midpoint() is not None:
                    guide_eps = item.midpoint()

    # Pick anchors with provenance
    if guide_revenue is not None:
        revenue = guide_revenue
        rev_source = "guidance_fy_revenue"
        rev_label = "FY guidance midpoint"
    else:
        revenue = consensus_revenue
        rev_source = "consensus_current_fy"
        rev_label = "FY consensus revenue"

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
    # because fixed costs don't scale with marginal revenue.
    incremental_margin = max(
        op_margin * _INCREMENTAL_MARGIN_MULTIPLIER,
        _MIN_INCREMENTAL_MARGIN,
    )
    incremental_margin = min(incremental_margin, _INCREMENTAL_MARGIN_CAP)

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


def compute_our_eps(baseline: BaselinePnL, edge_claims: list) -> EpsBuild:
    """
    Build the complete EPS bridge from baseline + edge_claims.

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

    sum_impact = 0.0
    for claim in edge_claims:
        if not isinstance(claim, dict):
            continue
        impact = apply_edge_claim(claim, baseline)
        build.claim_impacts.append(impact)
        sum_impact += impact.eps_impact
        if impact.impact_mismatch:
            build.warnings.append(
                f"Claim impact mismatch on {impact.claim_anchor_type}: "
                f"Claude said ${impact.claude_eps_impact:+.2f}, "
                f"flow-through computes ${impact.eps_impact:+.2f}"
            )

    build.sum_eps_impact = sum_impact
    build.our_eps = baseline.eps + sum_impact

    # Sanity check: if the sum is more than 50% of baseline, flag for review
    if abs(sum_impact) > 0.5 * abs(baseline.eps) and abs(baseline.eps) > 0.1:
        build.warnings.append(
            f"Aggregate edge impact ${sum_impact:+.2f} exceeds 50% of baseline "
            f"EPS ${baseline.eps:.2f} — review for compounding errors."
        )

    return build
