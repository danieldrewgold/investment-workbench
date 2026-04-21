"""
Estimate Model Engine

Three layers:
  1. Driver Decomposition — structured inputs that break into sub-components
  2. ModelSpec — schema-driven revenue/cost/EPS computation
  3. Propagation — mechanical flow-through with full trace chains

A "driver" is a named business metric that:
  - decomposes into measurable sub-components
  - maps to a specific model assumption
  - has a formula combining sub-components into the driver value
  - traces through to revenue, margin, EBIT, and EPS
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert


# ═══════════════════════════════════════════════════════════════
# Driver Decomposition Framework
# ═══════════════════════════════════════════════════════════════

@dataclass
class DriverComponent:
    """A sub-component of a driver."""
    name: str
    value: float
    unit: str = "pct"        # pct, bps, count, usd_m
    basis: str = ""          # why this value
    confidence: float = 0.5


@dataclass
class Driver:
    """
    A structured business driver that decomposes into sub-components
    and maps to a model assumption.

    Example — SSS for a restaurant:
      driver_name: "sss_growth"
      assumption_key: "sss_growth_pct"
      formula: "traffic + ticket"
      components:
        traffic_growth_pct: +2.0%
        ticket_growth_pct: +2.5%
          (which itself = menu_pricing 2.0% + mix_shift 0.5%)
      computed_value: 4.5%
    """
    driver_name: str
    assumption_key: str       # key in ModelSpec.assumptions
    formula: str              # human-readable: "traffic + ticket"
    components: dict = field(default_factory=dict)  # {name: DriverComponent}
    combine: str = "additive" # "additive" or "multiplicative"
    unit: str = "pct"

    def compute_value(self) -> float:
        """Combine sub-components into driver value."""
        values = [c.value for c in self.components.values()]
        if not values:
            return 0.0
        if self.combine == "additive":
            return sum(values)
        elif self.combine == "multiplicative":
            result = 1.0
            for v in values:
                result *= (1.0 + v / 100.0)
            return (result - 1.0) * 100.0
        return sum(values)

    @property
    def confidence(self) -> float:
        """Aggregate confidence = minimum of component confidences."""
        if not self.components:
            return 0.5
        return min(c.confidence for c in self.components.values())


class DriverDecomposition:
    """
    Manages the full set of drivers for an estimate.

    Usage:
        dd = DriverDecomposition()

        # Define SSS driver with traffic/ticket decomposition
        dd.add_driver(Driver(
            driver_name="sss_growth",
            assumption_key="sss_growth_pct",
            formula="traffic + ticket",
            components={
                "traffic": DriverComponent("traffic", 2.0, basis="Throughput improvements"),
                "ticket": DriverComponent("ticket", 2.5, basis="Menu pricing 2.0% + mix 0.5%"),
            },
        ))

        # Compute all driver values and inject into model
        dd.inject_into_model(model)

        # Change a sub-component and get full trace
        trace = dd.revise_component("sss_growth", "traffic", 1.0, model)
    """

    def __init__(self):
        self.drivers: dict[str, Driver] = {}

    def add_driver(self, driver: Driver):
        self.drivers[driver.driver_name] = driver

    def inject_into_model(self, model: ModelSpec):
        """Compute all driver values and set them as model assumptions."""
        for driver in self.drivers.values():
            value = driver.compute_value()
            model.assumptions[driver.assumption_key] = value

    def revise_component(
        self, driver_name: str, component_name: str,
        new_value: float, model: ModelSpec,
        reason: str = "",
    ) -> dict:
        """
        Change one sub-component and trace the full impact chain.

        Returns a trace dict:
          component_changed: what changed
          driver_before/after: how the driver value changed
          model_before/after: how revenue/EBIT/EPS changed
          chain: human-readable trace
        """
        driver = self.drivers.get(driver_name)
        if not driver or component_name not in driver.components:
            raise ValueError(f"Driver {driver_name}.{component_name} not found")

        # Snapshot before
        old_component = driver.components[component_name].value
        old_driver_value = driver.compute_value()
        self.inject_into_model(model)
        pre_outputs = model.compute_outputs()

        # Apply change
        driver.components[component_name].value = new_value
        new_driver_value = driver.compute_value()
        self.inject_into_model(model)
        post_outputs = model.compute_outputs()

        # Build trace chain
        deltas = {}
        for key in post_outputs:
            if key in pre_outputs:
                d = round(post_outputs[key] - pre_outputs[key], 4)
                if abs(d) > 0.0001:
                    deltas[key] = d

        chain = (
            f"{component_name}: {old_component:+.1f}% -> {new_value:+.1f}% "
            f"-> {driver_name}: {old_driver_value:+.1f}% -> {new_driver_value:+.1f}% "
            f"-> revenue: {deltas.get('revenue_m', 0):+,.1f}M "
            f"-> EBIT margin: {deltas.get('ebit_margin_pct', 0):+.1f}pp "
            f"-> EPS: ${deltas.get('eps', 0):+.2f}"
        )

        return {
            "component": component_name,
            "component_before": old_component,
            "component_after": new_value,
            "driver": driver_name,
            "driver_before": old_driver_value,
            "driver_after": new_driver_value,
            "pre_outputs": pre_outputs,
            "post_outputs": post_outputs,
            "deltas": deltas,
            "chain": chain,
            "reason": reason,
        }

    def get_driver_table(self) -> list[dict]:
        """P4: Produce driver decomposition table."""
        rows = []
        for d in self.drivers.values():
            computed = d.compute_value()
            for cname, comp in d.components.items():
                rows.append({
                    "driver": d.driver_name,
                    "component": cname,
                    "value": comp.value,
                    "unit": comp.unit,
                    "basis": comp.basis,
                    "confidence": comp.confidence,
                    "driver_total": round(computed, 2),
                    "assumption_key": d.assumption_key,
                    "formula": d.formula,
                })
        return rows

    def get_sensitivity_table(self, model: ModelSpec, perturbation: float = None) -> list[dict]:
        """
        For each component, compute EPS sensitivity.
        Perturbation is auto-sized by unit: 1pp for pct, 100bps for bps, 50 for count.
        """
        self.inject_into_model(model)
        base = model.compute_outputs()
        base_eps = base.get("eps", 0)

        rows = []
        for d in self.drivers.values():
            for cname, comp in d.components.items():
                # Smart perturbation sizing
                if perturbation is not None:
                    delta = perturbation
                elif comp.unit == "bps":
                    delta = 100.0   # 100bps is a meaningful cost change
                elif comp.unit == "count":
                    delta = 50.0    # 50 stores
                else:
                    delta = 1.0     # 1pp for pct

                # Perturb
                saved = comp.value
                comp.value = saved + delta
                self.inject_into_model(model)
                perturbed = model.compute_outputs()
                comp.value = saved
                self.inject_into_model(model)

                eps_delta = perturbed.get("eps", 0) - base_eps
                rev_delta = perturbed.get("revenue_m", 0) - base.get("revenue_m", 0)
                margin_delta = perturbed.get("ebit_margin_pct", 0) - base.get("ebit_margin_pct", 0)

                # Normalize to per-unit for display
                eps_per_unit = eps_delta / delta if delta else 0

                rows.append({
                    "driver": d.driver_name,
                    "component": cname,
                    "current_value": comp.value,
                    "unit": comp.unit,
                    "perturbation": delta,
                    "perturbation_label": f"{delta:.0f}{comp.unit}",
                    "eps_impact": round(eps_delta, 4),
                    "eps_per_unit": round(eps_per_unit, 6),
                    "revenue_impact_m": round(rev_delta, 1),
                    "margin_impact_pp": round(margin_delta, 2),
                    "confidence": comp.confidence,
                    "exposure": round(abs(eps_delta) * (1.0 - comp.confidence), 4),
                })

        rows.sort(key=lambda x: x["exposure"], reverse=True)
        return rows


# ═══════════════════════════════════════════════════════════════
# P1: Estimate Dependency Engine
# ═══════════════════════════════════════════════════════════════

@dataclass
class ModelSpec:
    """
    Schema-driven estimate model.

    The model reads a sector driver schema to determine:
      - how revenue builds from assumptions
      - what cost buckets exist and how they scale
      - what below-line costs exist

    This makes the engine sector-agnostic. The same ModelSpec class
    handles restaurants (SSS + new stores, food/labor/occupancy/other)
    and software (net retention + new ARR, COGS/S&M/R&D/G&A) by
    reading different driver schemas.
    """
    assumptions: dict = field(default_factory=dict)
    prior_year: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    driver_schema: dict = field(default_factory=dict)

    def compute_outputs(self) -> dict:
        a = self.assumptions
        py = self.prior_year
        c = self.constants
        ds = self.driver_schema

        outputs = {}
        rev_model = ds.get("revenue_model", "simple_growth")

        # ── Revenue build (dispatched by schema) ──
        revenue, extra = self._compute_revenue(rev_model, a, py)
        outputs["revenue_m"] = round(revenue, 1)
        outputs.update(extra)

        prior_rev = py.get("revenue_m", 0)
        prior_store_count = py.get("store_count", 1)
        new_stores = a.get("new_restaurants", a.get("new_units", 0))
        total_stores = prior_store_count + new_stores

        rev_ratio = revenue / max(prior_rev, 1)
        store_ratio = total_stores / max(prior_store_count, 1)

        # ── Cost buckets (from schema) ──
        cost_buckets = ds.get("cost_buckets", [])
        total_direct_costs = 0.0
        above_gross_costs = 0.0
        below_gross_opex = 0.0

        for bucket in cost_buckets:
            name = bucket["name"]
            prior_pct = py.get(bucket["prior_key"], 0)
            var_pct = bucket.get("variable_pct", 0.5)
            delta_key = bucket.get("delta_key")
            scales = bucket.get("scales_with", "revenue")

            prior_cost = prior_rev * prior_pct / 100.0
            delta_bps = a.get(delta_key, 0) if delta_key else 0

            # Variable portion scales with revenue; fixed with store count
            if scales == "stores":
                cost_m = (prior_cost * var_pct * rev_ratio +
                          prior_cost * (1 - var_pct) * store_ratio)
            else:
                cost_m = (prior_cost * var_pct * rev_ratio +
                          prior_cost * (1 - var_pct) * store_ratio +
                          revenue * delta_bps / 10000.0)

            total_direct_costs += cost_m
            outputs[f"{name}_pct"] = round(cost_m / max(revenue, 1) * 100, 1)
            outputs[f"{name}_m"] = round(cost_m, 1)

            if bucket.get("above_gross", True):
                above_gross_costs += cost_m
            else:
                below_gross_opex += cost_m

        # Gross profit = revenue minus above-gross-line costs only
        gross_profit = revenue - above_gross_costs
        gp_label = ds.get("gross_profit_label", "gross_profit")
        gm_label = ds.get("gross_margin_label", "gross_margin_pct")
        outputs[f"{gp_label}_m"] = round(gross_profit, 1)
        outputs[gm_label] = round(gross_profit / max(revenue, 1) * 100, 1)

        # ── Below-line costs (from schema) ──
        below_line = ds.get("below_line", {})
        total_below = 0.0

        for bl_name, bl_spec in below_line.items():
            prior_key = bl_spec.get("prior_key", f"{bl_name}_m")
            prior_val = py.get(prior_key, 0)

            scales = bl_spec.get("scales_with")
            growth_key = bl_spec.get("growth_key")
            default_growth = bl_spec.get("default_growth")

            if scales == "stores":
                val = prior_val * store_ratio
            elif scales == "new_stores":
                prior_new = py.get("prior_new_restaurants", py.get("prior_new_units", max(new_stores, 1)))
                per_unit = prior_val / max(prior_new, 1)
                val = new_stores * per_unit
            elif growth_key:
                growth = a.get(growth_key)
                if growth is None:
                    growth = default_growth
                if growth is not None:
                    val = prior_val * (1.0 + growth / 100.0)
                else:
                    # Scale with revenue growth if no explicit growth
                    val = prior_val * (1.0 + max((revenue - prior_rev) / max(prior_rev, 1), 0.03))
            else:
                val = prior_val

            total_below += val
            outputs[f"{bl_name}_m"] = round(val, 1)

        # Compute GA total if both cash_ga and stock_comp exist
        if "cash_ga_m" in outputs and "stock_comp_m" in outputs:
            outputs["ga_m"] = round(outputs["cash_ga_m"] + outputs["stock_comp_m"], 1)

        ebit = gross_profit - below_gross_opex - total_below
        ebit_margin = ebit / max(revenue, 1) * 100.0
        outputs["ebit_m"] = round(ebit, 1)
        outputs["ebit_margin_pct"] = round(ebit_margin, 1)

        # ── EPS ──
        interest = c.get("net_interest_m", 0)
        tax_rate = c.get("tax_rate", 0.25)
        shares = c.get("shares_m", 100)

        pretax = ebit + interest
        net_income = pretax * (1.0 - tax_rate)
        eps = net_income / max(shares, 1)
        outputs["net_income_m"] = round(net_income, 1)
        outputs["eps"] = round(eps, 2)

        return outputs

    def _compute_revenue(self, model_type: str, a: dict, py: dict) -> tuple:
        """Dispatch revenue computation based on schema's revenue_model."""
        extra = {}

        if model_type == "sss_plus_new_stores":
            prior_rev = py.get("revenue_m", 0)
            prior_stores = py.get("store_count", 1)
            sss = a.get("sss_growth_pct", 0) / 100.0
            new_stores = a.get("new_restaurants", 0)
            productivity = a.get("new_store_productivity", 0.75)

            existing = prior_rev * (1.0 + sss)
            avg_store = prior_rev / max(prior_stores, 1)
            new_rev = new_stores * avg_store * productivity * 0.5
            return existing + new_rev, extra

        elif model_type == "arr_plus_new_bookings":
            prior_rev = py.get("revenue_m", 0)
            net_retention = a.get("net_retention_pct", 100) / 100.0
            new_arr_growth = a.get("new_arr_growth_pct", 0) / 100.0

            retained = prior_rev * net_retention
            new_bookings = prior_rev * new_arr_growth
            revenue = retained + new_bookings
            extra["retained_revenue_m"] = round(retained, 1)
            extra["new_bookings_revenue_m"] = round(new_bookings, 1)
            return revenue, extra

        elif model_type == "franchise_royalty":
            # Revenue = royalty revenue + ad fund revenue + company-owned sales
            prior_rev = py.get("revenue_m", 0)
            prior_system_sales = py.get("system_wide_sales_m", prior_rev * 8)  # ~8x for typical franchise
            prior_total_stores = py.get("store_count", 1)
            sss = a.get("sss_growth_pct", 0) / 100.0
            new_stores = a.get("new_restaurants", 0)
            royalty_rate = a.get("royalty_rate_pct", 6.0) / 100.0
            ad_fund_rate = a.get("ad_fund_rate_pct", 5.0) / 100.0
            co_stores = a.get("company_owned_stores", py.get("company_owned_stores", 0))
            co_auv = py.get("company_owned_auv_m", 2.0)
            co_sss = a.get("company_sss_pct", sss * 100) / 100.0  # default: same as system SSS

            # System-wide sales growth
            total_stores = prior_total_stores + new_stores
            prior_auv = prior_system_sales / max(prior_total_stores, 1)
            new_auv = prior_auv * (1.0 + sss)
            # Existing store sales grow by SSS; new stores at partial year
            existing_system = prior_system_sales * (1.0 + sss)
            new_system = new_stores * prior_auv * 0.5
            system_wide_sales = existing_system + new_system

            royalty_rev = system_wide_sales * royalty_rate
            ad_rev = system_wide_sales * ad_fund_rate
            co_rev = co_stores * co_auv * (1.0 + co_sss)

            # Supply chain revenue (optional — for DPZ-type franchisors that also
            # manufacture/distribute food to franchisees)
            supply_chain_rev = 0
            prior_supply = py.get("supply_chain_m", 0)
            if prior_supply > 0:
                # Supply chain grows with system-wide sales + food basket pricing
                supply_growth = sss + a.get("supply_chain_pricing_pct", 1.0) / 100.0
                # Also scales with new store openings
                store_growth = new_stores / max(prior_total_stores, 1)
                supply_chain_rev = prior_supply * (1.0 + supply_growth * 0.5 + store_growth * 0.5)
                extra["supply_chain_revenue_m"] = round(supply_chain_rev, 1)

            # International royalties (optional — separate from domestic)
            intl_royalty_rev = 0
            prior_intl = py.get("intl_royalty_m", 0)
            if prior_intl > 0:
                intl_growth = a.get("intl_sss_pct", 1.0) / 100.0 + new_stores / max(prior_total_stores, 1)
                intl_royalty_rev = prior_intl * (1.0 + intl_growth)
                extra["intl_royalty_revenue_m"] = round(intl_royalty_rev, 1)

            revenue = royalty_rev + ad_rev + co_rev + supply_chain_rev + intl_royalty_rev

            extra["system_wide_sales_m"] = round(system_wide_sales, 1)
            extra["royalty_revenue_m"] = round(royalty_rev, 1)
            extra["ad_fund_revenue_m"] = round(ad_rev, 1)
            extra["company_owned_revenue_m"] = round(co_rev, 1)
            return revenue, extra

        elif model_type == "simple_growth":
            prior_rev = py.get("revenue_m", 0)
            growth = a.get("revenue_growth_pct", 0) / 100.0
            return prior_rev * (1.0 + growth), extra

        else:
            raise ValueError(f"Unknown revenue model: {model_type}")


def propagate_revision(
    model: ModelSpec,
    assumption_key: str,
    new_value: float,
) -> dict:
    """
    P1: Propagate a single assumption change through the model.

    Returns:
      pre: outputs before the change
      post: outputs after the change
      deltas: {output: change_amount}
    """
    # Compute pre-change outputs
    pre = model.compute_outputs()

    # Apply the change
    model.assumptions[assumption_key] = new_value

    # Compute post-change outputs
    post = model.compute_outputs()

    # Calculate deltas
    deltas = {}
    for key in post:
        if key in pre:
            deltas[key] = round(post[key] - pre[key], 4)

    return {"pre": pre, "post": post, "deltas": deltas}


# ═══════════════════════════════════════════════════════════════
# P2: Impact-Aware Exposure Scoring
# ═══════════════════════════════════════════════════════════════

def compute_eps_sensitivities(model: ModelSpec) -> dict:
    """
    For each assumption, estimate how much a 1-unit change
    affects EPS. This is the economic importance of each assumption.

    Returns: {assumption_key: eps_sensitivity}
    """
    base_outputs = model.compute_outputs()
    base_eps = base_outputs.get("eps", 0)

    sensitivities = {}

    for key, value in model.assumptions.items():
        if value == 0:
            continue

        # Perturb by a meaningful amount
        if "pct" in key.lower():
            delta = 1.0  # 1 percentage point
        elif "restaurants" in key.lower():
            delta = 50   # 50 stores
        elif value > 1000:
            delta = value * 0.01  # 1%
        else:
            delta = max(abs(value) * 0.1, 0.1)

        # Compute sensitivity
        saved = model.assumptions[key]
        model.assumptions[key] = value + delta
        perturbed = model.compute_outputs()
        model.assumptions[key] = saved  # restore

        eps_change = perturbed.get("eps", 0) - base_eps
        sensitivity = eps_change / delta if delta != 0 else 0

        sensitivities[key] = {
            "assumption_value": value,
            "perturbation": delta,
            "eps_change": round(eps_change, 4),
            "eps_sensitivity_per_unit": round(sensitivity, 4),
            "unit": "per 1pp" if "pct" in key.lower() else f"per {delta:.0f} units",
        }

    return sensitivities


def impact_weighted_exposure(
    contradictions: list,
    sensitivities: dict,
    assumptions: dict,
) -> list[dict]:
    """
    P2: Rank assumptions by true economic importance.

    impact_score = severity × (1 - confidence) × abs(eps_sensitivity)

    This answers: "what uncertain thing would hurt the most if wrong?"
    """
    from research.adversarial import Contradiction

    exposures = []

    for key, value in assumptions.items():
        # Contradiction severity
        relevant = [c for c in contradictions if c.assumption_key == key]
        if relevant:
            max_sev = max(
                (c.severity for c in relevant),
                key=lambda s: {"serious": 3, "moderate": 2, "minor": 1, "none": 0}.get(s, 0))
        else:
            max_sev = "none"
        severity_score = {"serious": 3, "moderate": 2, "minor": 1, "none": 0}.get(max_sev, 0)

        # Confidence (from assumptions dict or default)
        conf = assumptions.get(f"{key}_confidence", 0.5)

        # EPS sensitivity
        sens = sensitivities.get(key, {})
        eps_sens = abs(sens.get("eps_sensitivity_per_unit", 0))

        # Impact-weighted exposure
        process_score = severity_score * (1.0 - conf)
        impact_score = process_score * max(eps_sens, 0.01)  # floor to avoid zero

        exposures.append({
            "assumption": key,
            "value": value,
            "confidence": conf,
            "contradiction_severity": max_sev,
            "eps_sensitivity": sens.get("eps_sensitivity_per_unit", 0),
            "eps_change_per_unit": sens.get("eps_change", 0),
            "sensitivity_unit": sens.get("unit", ""),
            "process_exposure": round(process_score, 2),
            "impact_exposure": round(impact_score, 4),
        })

    exposures.sort(key=lambda x: x["impact_exposure"], reverse=True)
    return exposures
