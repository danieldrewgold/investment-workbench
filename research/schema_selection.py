"""
Schema Selection

Sits between Orientation and Estimate Building.
Answers: what economic structure does this company have,
and which model schema should the estimate engine use?

This is a GATE — if schema fit is weak, the system should
say so rather than confidently running the wrong model.

The module:
  1. Defines candidate schemas with identifying signals
  2. Scores orientation evidence against each candidate
  3. Selects the best-fit schema with confidence
  4. Identifies schema risk (what breaks if wrong)
  5. Produces analyst-visible workpapers
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert


# ═══════════════════════════════════════════════════════════════
# Schema Candidates — what the system knows how to model
# ═══════════════════════════════════════════════════════════════

# Each candidate has: identifying signals (evidence that supports it),
# disqualifying signals (evidence that rules it out), and the
# driver schema key it maps to.

SCHEMA_CANDIDATES = {
    "company_operated_restaurant": {
        "label": "Company-Operated Restaurant",
        "driver_schema_key": "restaurant",
        "description": "Chain where the company owns/operates most locations. "
                       "Revenue = store-level sales. Costs = food, labor, occupancy.",
        "identifying_signals": [
            ("company_operated_pct", ">=", 50, 3, "Majority company-operated stores"),
            ("revenue_type", "contains", "restaurant_sales", 3, "Revenue is primarily restaurant sales"),
            ("cost_disclosure", "contains", "food_labor_occupancy", 2, "Discloses restaurant-level costs"),
            ("metric_disclosed", "contains", "sss", 2, "Reports same-store sales"),
            ("metric_disclosed", "contains", "restaurant_margin", 2, "Reports restaurant-level margin"),
            ("metric_disclosed", "contains", "new_store_openings", 1, "Reports new store openings"),
        ],
        "disqualifying_signals": [
            ("franchise_pct", ">=", 70, "Mostly franchised — royalty model, not operated"),
            ("revenue_type", "contains", "royalty", "Revenue is primarily royalties, not sales"),
            ("revenue_type", "contains", "subscription", "Subscription revenue — not a restaurant"),
        ],
        "schema_risk": "If franchise mix is higher than assumed, company-operated "
                       "cost structure (food/labor/occupancy) won't match P&L. "
                       "EBIT will be overstated because franchise economics have "
                       "much higher margins with different cost drivers.",
    },

    "franchise_restaurant": {
        "label": "Franchise-Heavy Restaurant",
        "driver_schema_key": "franchise_restaurant",
        "description": "Chain where most locations are franchised. "
                       "Revenue = royalties + ad fund + small company-owned.",
        "identifying_signals": [
            ("franchise_pct", ">=", 70, 3, "Majority franchised stores"),
            ("revenue_type", "contains", "royalty", 3, "Royalty revenue is a major component"),
            ("revenue_type", "contains", "franchise_fee", 2, "Franchise fees disclosed"),
            ("metric_disclosed", "contains", "system_wide_sales", 2, "Reports system-wide sales"),
            ("cost_disclosure", "contains", "low_cogs_ratio", 2, "COGS/revenue ratio very low"),
        ],
        "disqualifying_signals": [
            ("company_operated_pct", ">=", 70, "Mostly company-operated — use operated schema"),
            ("revenue_type", "contains", "subscription", "Not a restaurant"),
        ],
        "schema_risk": "Franchise cost structure is fundamentally different from "
                       "company-operated. Food/labor/occupancy buckets apply only to "
                       "the small company-owned segment. SG&A and interest expense "
                       "dominate the P&L. Modeling error can be very large.",
    },

    "saas_subscription": {
        "label": "SaaS / Subscription Software",
        "driver_schema_key": "software",
        "description": "Software company with recurring subscription revenue. "
                       "Revenue = ARR driven by retention + new bookings.",
        "identifying_signals": [
            ("revenue_type", "contains", "subscription", 3, "Subscription revenue model"),
            ("metric_disclosed", "contains", "arr", 2, "Reports ARR or annual recurring revenue"),
            ("metric_disclosed", "contains", "net_retention", 3, "Reports net dollar retention"),
            ("metric_disclosed", "contains", "customer_count", 1, "Reports customer count"),
            ("cost_disclosure", "contains", "rd_sm_ga", 2, "R&D, S&M, G&A cost structure"),
            ("gross_margin_range", ">=", 60, 2, "High gross margin (60%+)"),
        ],
        "disqualifying_signals": [
            ("revenue_type", "contains", "restaurant_sales", "Not a software company"),
            ("revenue_type", "contains", "product_sales", "Hardware/product, not SaaS"),
            ("gross_margin_range", "<", 40, "Low gross margin inconsistent with SaaS"),
        ],
        "schema_risk": "If revenue is more services/consulting than subscription, "
                       "net retention won't drive the model correctly. If gross margin "
                       "is lower than expected, cost structure assumptions break.",
    },

    "general": {
        "label": "General (Revenue Growth + Cost Structure)",
        "driver_schema_key": "general",
        "description": "General-purpose model for any company with a P&L. "
                       "Revenue = prior × (1 + growth). Costs = COGS + OpEx. "
                       "Use when no specialized schema fits.",
        "identifying_signals": [
            ("revenue_type", "contains", "product_sales", 2, "Product/hardware revenue"),
            ("gross_margin_range", "<", 40, 2, "Low gross margin suggests hardware/manufacturing"),
        ],
        "disqualifying_signals": [],  # NEVER disqualified — always available as fallback
        "schema_risk": "General model has no sector-specific intelligence. "
                       "Cost leverage assumptions may not reflect the actual business dynamics. "
                       "Estimate is directionally useful but lacks structural precision.",
    },
}


# ═══════════════════════════════════════════════════════════════
# Evidence Extraction from Orientation
# ═══════════════════════════════════════════════════════════════

@dataclass
class SchemaEvidence:
    """A piece of evidence relevant to schema selection."""
    signal_type: str     # e.g. "revenue_type", "franchise_pct", "metric_disclosed"
    value: str | float   # e.g. "restaurant_sales", 95, "sss"
    source: str          # where this came from
    confidence: float = 0.8


def extract_schema_evidence(conn, company_id: str) -> list[SchemaEvidence]:
    """
    Pull schema-relevant evidence from orientation observations.

    Looks at evidence_items with types like KEY_METRIC, SEGMENT_MIX,
    REVENUE_COMPOSITION, etc. and translates them into schema signals.
    """
    evidence = []

    rows = conn.execute("""
        SELECT evidence_type, evidence_key, value, value_numeric
        FROM evidence_item
        WHERE company_id = ?
        ORDER BY as_of_date DESC
    """, (company_id,)).fetchall()

    for etype, ekey, val, vnum in rows:
        val_lower = (val or "").lower()
        ekey_lower = (ekey or "").lower()

        # Revenue composition signals
        if "revenue" in ekey_lower or "sales" in ekey_lower:
            if any(w in val_lower for w in ["restaurant", "store", "company-owned", "company owned"]):
                evidence.append(SchemaEvidence("revenue_type", "restaurant_sales", val[:100]))
            if any(w in val_lower for w in ["royalt", "franchise fee", "franchis"]):
                evidence.append(SchemaEvidence("revenue_type", "royalty", val[:100]))
            if any(w in val_lower for w in ["subscription", "saas", "recurring", "arr"]):
                evidence.append(SchemaEvidence("revenue_type", "subscription", val[:100]))

        # Franchise/operated mix
        if "franchise" in ekey_lower or "company.operated" in ekey_lower.replace(" ", "."):
            if vnum and vnum > 50:
                if "franchise" in ekey_lower:
                    evidence.append(SchemaEvidence("franchise_pct", vnum, val[:100]))
                else:
                    evidence.append(SchemaEvidence("company_operated_pct", vnum, val[:100]))

        # Cost structure signals
        if any(w in val_lower for w in ["food", "labor", "occupancy"]):
            evidence.append(SchemaEvidence("cost_disclosure", "food_labor_occupancy", val[:100]))
        if any(w in val_lower for w in ["r&d", "research", "sales and marketing", "s&m"]):
            evidence.append(SchemaEvidence("cost_disclosure", "rd_sm_ga", val[:100]))

        # Metric disclosures
        for metric in ["sss", "same.store", "comp", "comparable", "restaurant.margin",
                       "operating.margin", "new.store",
                       "new.restaurant", "arr", "net.retention", "customer.count",
                       "system.wide", "aur", "auv"]:
            if metric.replace(".", " ") in val_lower or metric.replace(".", " ") in ekey_lower:
                clean = metric.replace(".", "_")
                if clean in ["same_store", "comp", "comparable"]:
                    clean = "sss"
                if clean in ["operating_margin"]:
                    clean = "restaurant_margin"
                if clean in ["new_store", "new_restaurant"]:
                    clean = "new_store_openings"
                if clean in ["aur", "auv"]:
                    clean = "restaurant_margin"
                evidence.append(SchemaEvidence("metric_disclosed", clean, val[:80]))

        # Margin level signals
        if "margin" in ekey_lower and "gross" in ekey_lower and vnum:
            evidence.append(SchemaEvidence("gross_margin_range", vnum, val[:80]))

    # Deduplicate by (signal_type, value)
    seen = set()
    unique = []
    for e in evidence:
        key = (e.signal_type, str(e.value)[:30])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


# ═══════════════════════════════════════════════════════════════
# Schema Scoring and Selection
# ═══════════════════════════════════════════════════════════════

@dataclass
class SchemaScore:
    """Score for one candidate schema."""
    schema_key: str
    label: str
    driver_schema_key: str
    score: float = 0.0
    max_possible: float = 0.0
    matched_signals: list = field(default_factory=list)
    disqualified: bool = False
    disqualify_reason: str = ""


@dataclass
class SchemaSelection:
    """Result of schema selection."""
    chosen_key: str
    chosen_label: str
    driver_schema_key: str
    confidence: float          # 0-1
    fit_level: str             # "strong", "adequate", "weak", "no_fit"
    scores: list               # all SchemaScore objects
    evidence_used: list        # SchemaEvidence objects
    risk_summary: str
    uncertainties: list        # what could be wrong


def score_schemas(evidence: list[SchemaEvidence]) -> list[SchemaScore]:
    """Score each candidate schema against the evidence."""
    scores = []

    for schema_key, schema in SCHEMA_CANDIDATES.items():
        sc = SchemaScore(
            schema_key=schema_key,
            label=schema["label"],
            driver_schema_key=schema["driver_schema_key"],
        )

        # Check disqualifying signals
        for dtype, dop, dval, dreason in schema["disqualifying_signals"]:
            for ev in evidence:
                if ev.signal_type != dtype:
                    continue
                if dop == "contains" and isinstance(ev.value, str) and dval in ev.value:
                    sc.disqualified = True
                    sc.disqualify_reason = dreason
                    break
                if dop == ">=" and isinstance(ev.value, (int, float)) and ev.value >= dval:
                    sc.disqualified = True
                    sc.disqualify_reason = dreason
                    break
            if sc.disqualified:
                break

        # Score identifying signals
        for stype, sop, sval, weight, desc in schema["identifying_signals"]:
            sc.max_possible += weight
            for ev in evidence:
                if ev.signal_type != stype:
                    continue
                matched = False
                if sop == "contains" and isinstance(ev.value, str) and sval in ev.value:
                    matched = True
                elif sop == ">=" and isinstance(ev.value, (int, float)) and ev.value >= sval:
                    matched = True

                if matched:
                    sc.score += weight
                    sc.matched_signals.append(desc)
                    break

        scores.append(sc)

    scores.sort(key=lambda s: (-1 if s.disqualified else 0, -s.score))
    return scores


def select_schema(
    evidence: list[SchemaEvidence],
    analyst_override: str = None,
) -> SchemaSelection:
    """
    Select the best-fit schema based on evidence.

    Returns a SchemaSelection with confidence and fit assessment.
    If no schema fits well, says so honestly.
    """
    scores = score_schemas(evidence)

    # Filter out disqualified
    viable = [s for s in scores if not s.disqualified]
    # Separate specialized vs general
    specialized = [s for s in viable if s.schema_key != "general"]
    general = next((s for s in viable if s.schema_key == "general"), None)

    if analyst_override:
        chosen = next((s for s in scores if s.schema_key == analyst_override), None)
        if not chosen:
            raise ValueError(f"Unknown schema: {analyst_override}")
    elif specialized:
        best = specialized[0]
        # If best specialized schema scored poorly, fall back to general
        if best.max_possible > 0 and best.score / best.max_possible < 0.25 and general:
            chosen = general
        else:
            chosen = best
    elif general:
        # No specialized schema is viable — use general
        chosen = general
    else:
        chosen = scores[0]

    # Compute confidence
    if chosen.schema_key == "general":
        # General schema is always usable — confidence reflects that it works
        # but isn't as precise as a specialized schema
        confidence = 0.55
        fit_level = "adequate"
    elif chosen.max_possible > 0:
        raw_conf = chosen.score / chosen.max_possible
        if chosen.disqualified:
            confidence = min(raw_conf * 0.3, 0.2)
            fit_level = "no_fit"
        elif raw_conf >= 0.7:
            confidence = min(raw_conf, 0.90)
            fit_level = "strong"
        elif raw_conf >= 0.4:
            confidence = raw_conf * 0.8
            fit_level = "adequate"
        elif raw_conf > 0:
            confidence = raw_conf * 0.5
            fit_level = "weak"
        else:
            confidence = 0.1
            fit_level = "no_fit"
    else:
        raw_conf = 0.0
        confidence = 0.1
        fit_level = "no_fit"

    # Identify uncertainties
    uncertainties = []
    if chosen.disqualified:
        uncertainties.append(f"DISQUALIFIED: {chosen.disqualify_reason}")
    if confidence < 0.5:
        uncertainties.append("Low confidence — schema may not match economic structure")
    if len(viable) >= 2 and viable[0].score > 0 and viable[1].score > 0:
        gap = viable[0].score - viable[1].score
        if gap <= 2:
            uncertainties.append(
                f"Close call: {viable[0].label} ({viable[0].score:.0f}) vs "
                f"{viable[1].label} ({viable[1].score:.0f}) — review evidence")

    # Risk summary
    schema_def = SCHEMA_CANDIDATES.get(chosen.schema_key, {})
    risk = schema_def.get("schema_risk", "No specific risk identified.")

    return SchemaSelection(
        chosen_key=chosen.schema_key,
        chosen_label=chosen.label,
        driver_schema_key=chosen.driver_schema_key,
        confidence=round(confidence, 2),
        fit_level=fit_level,
        scores=scores,
        evidence_used=evidence,
        risk_summary=risk,
        uncertainties=uncertainties,
    )


# ═══════════════════════════════════════════════════════════════
# Manual Evidence Construction (for pipeline use)
# ═══════════════════════════════════════════════════════════════

def build_evidence_from_observations(observations: list[dict]) -> list[SchemaEvidence]:
    """
    Build schema evidence from a list of observation dicts.

    Each observation should have at least: {"key": ..., "value": ..., "source": ...}
    This is for cases where evidence isn't in the DB yet (e.g. pipeline scripts).
    """
    evidence = []
    for obs in observations:
        val = obs.get("value", "")
        key = obs.get("key", "")
        src = obs.get("source", "")
        val_lower = str(val).lower()
        key_lower = str(key).lower()

        # Revenue type
        if any(w in val_lower for w in ["restaurant sales", "company-owned restaurant", "store revenue"]):
            evidence.append(SchemaEvidence("revenue_type", "restaurant_sales", src))
        if any(w in val_lower for w in ["royalt", "franchise fee"]):
            evidence.append(SchemaEvidence("revenue_type", "royalty", src))
        if any(w in val_lower for w in ["subscription", "recurring", "arr "]):
            evidence.append(SchemaEvidence("revenue_type", "subscription", src))

        # Franchise/operated percentages
        if any(w in val_lower for w in ["100% of its restaurant", "100% company", "operates all",
                                          "no franchise", "does not franchise"]):
            evidence.append(SchemaEvidence("company_operated_pct", 100, src))
        # Detect franchise percentage from value text
        if any(w in val_lower for w in ["98% franchis", "97% franchis", "95% franchis",
                                          "mostly franchis", "primarily franchis",
                                          "98% of location", "97% of location",
                                          "approximately 98%", "approximately 97%"]):
            evidence.append(SchemaEvidence("franchise_pct", 98, src))
        # "X% of locations are franchised" pattern
        import re
        fmatch = re.search(r'(\d{2,3})%\s+(?:of\s+)?(?:locations?|restaurants?|units?)\s+(?:are\s+)?franchis', val_lower)
        if fmatch:
            evidence.append(SchemaEvidence("franchise_pct", float(fmatch.group(1)), src))
        # Detect "N,NNN franchise restaurants" — number before or after
        fcount = re.search(r'(\d{1,3}[,.]?\d{3})\s+(?:domestic\s+)?franchise\s+restaurant', val_lower)
        if not fcount:
            # Also match "franchise restaurants: N,NNN"
            fcount = re.search(r'franchise\s+restaurants?[:\s]+(\d{1,3}[,.]?\d{3})', val_lower)
        if fcount:
            count = int(fcount.group(1).replace(",", "").replace(".", ""))
            if count > 500:
                evidence.append(SchemaEvidence("franchise_pct", 90, src))
        # Key-based franchise detection (e.g. key="domestic_franchise_count")
        if "franchise" in key_lower and ("count" in key_lower or "number" in key_lower or "restaurant" in key_lower):
            # If the value has a large number, it's a franchise system
            nums = re.findall(r'[\d,]+', str(val))
            for n in nums:
                try:
                    v = int(n.replace(",", ""))
                    if v > 500:
                        evidence.append(SchemaEvidence("franchise_pct", 90, src))
                        break
                except ValueError:
                    pass
        # Revenue type: royalty / franchise fee from value
        if any(w in val_lower for w in ["royalty revenue", "franchise fee", "franchise fees"]):
            evidence.append(SchemaEvidence("revenue_type", "royalty", src))
        # Key-based royalty detection (e.g. key contains "royalty" or "franchise_fee")
        if any(w in key_lower for w in ["royalt", "franchise_fee", "franchise_rev"]):
            evidence.append(SchemaEvidence("revenue_type", "royalty", src))
        # If franchise_pct is very high, royalty revenue is structurally implied
        if fmatch and float(fmatch.group(1)) >= 80:
            evidence.append(SchemaEvidence("revenue_type", "royalty", src))
        # Detect SGA-heavy cost structure (franchise indicator)
        if any(w in val_lower for w in ["sg&a", "sga", "selling, general"]):
            evidence.append(SchemaEvidence("cost_disclosure", "low_cogs_ratio", src))
        if "franchise" in key_lower and "%" in str(val):
            try:
                pct = float(str(val).replace("%", "").strip().split()[0])
                evidence.append(SchemaEvidence("franchise_pct", pct, src))
            except (ValueError, IndexError):
                pass
        if "company" in key_lower and "operated" in key_lower and "%" in str(val):
            try:
                pct = float(str(val).replace("%", "").strip().split()[0])
                evidence.append(SchemaEvidence("company_operated_pct", pct, src))
            except (ValueError, IndexError):
                pass

        # Cost structure
        if any(w in val_lower for w in ["food cost", "labor cost", "occupancy"]):
            evidence.append(SchemaEvidence("cost_disclosure", "food_labor_occupancy", src))
        if any(w in val_lower for w in ["r&d", "research and development", "sales and marketing"]):
            evidence.append(SchemaEvidence("cost_disclosure", "rd_sm_ga", src))

        # Metrics
        for metric, signal in [
            ("same-store", "sss"), ("sss", "sss"), ("comp ", "sss"),
            ("comparable restaurant", "sss"), ("comparable sales", "sss"),
            ("restaurant margin", "restaurant_margin"), ("restaurant-level", "restaurant_margin"),
            ("operating margin", "restaurant_margin"),
            ("new restaurant", "new_store_openings"),
            ("new store", "new_store_openings"), ("system-wide", "system_wide_sales"),
            ("arr", "arr"), ("net retention", "net_retention"), ("net dollar retention", "net_retention"),
            ("restaurant-level margin", "restaurant_margin"), ("restaurant margin", "restaurant_margin"),
        ]:
            if metric in val_lower or metric in key_lower:
                evidence.append(SchemaEvidence("metric_disclosed", signal, src))

        # Gross margin
        if "gross margin" in key_lower or "gross margin" in val_lower:
            try:
                gm = float(str(val).replace("%", "").strip().split()[0])
                evidence.append(SchemaEvidence("gross_margin_range", gm, src))
            except (ValueError, IndexError):
                pass

        # EBITDA margin > 50% implies high-margin business (software/data)
        import re as _re
        ebitda_match = _re.search(r'ebitda\s+margin\s+(?:was\s+)?(\d+\.?\d*)', val_lower)
        if ebitda_match:
            ebitda_m = float(ebitda_match.group(1))
            if ebitda_m >= 45:
                evidence.append(SchemaEvidence("gross_margin_range", min(ebitda_m + 12, 90), src))

        # Retention signals
        if any(w in val_lower for w in ["retention rate", "high retention",
                                          "net retention", "dollar retention", "renewal rate"]):
            evidence.append(SchemaEvidence("metric_disclosed", "net_retention", src))

        # Segment reporting → suggests multi-line cost structure
        if any(w in val_lower for w in ["underwriting revenue", "claims revenue",
                                          "segment revenue"]):
            evidence.append(SchemaEvidence("cost_disclosure", "rd_sm_ga", src))

    # Deduplicate
    seen = set()
    unique = []
    for e in evidence:
        k = (e.signal_type, str(e.value)[:30])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return unique


# ═══════════════════════════════════════════════════════════════
# Workpaper Production
# ═══════════════════════════════════════════════════════════════

def produce_schema_workpaper(conn, company_id: str, selection: SchemaSelection,
                              run_id: str = None) -> str:
    """Produce a SCHEMA_SELECTION workpaper."""
    from research.escalation import WorkpaperBuilder

    wb = WorkpaperBuilder(conn, company_id)

    content = {
        "chosen_schema": selection.chosen_key,
        "chosen_label": selection.chosen_label,
        "driver_schema": selection.driver_schema_key,
        "confidence": selection.confidence,
        "fit_level": selection.fit_level,
        "risk_summary": selection.risk_summary,
        "uncertainties": selection.uncertainties,
        "candidates": [],
        "evidence": [],
    }

    for sc in selection.scores:
        content["candidates"].append({
            "schema": sc.schema_key,
            "label": sc.label,
            "score": sc.score,
            "max_possible": sc.max_possible,
            "disqualified": sc.disqualified,
            "disqualify_reason": sc.disqualify_reason,
            "matched_signals": sc.matched_signals,
        })

    for ev in selection.evidence_used:
        content["evidence"].append({
            "signal_type": ev.signal_type,
            "value": str(ev.value)[:100],
            "source": ev.source[:100],
        })

    wid = wb.create(
        workpaper_type="SCHEMA_SELECTION",
        title=f"Economic Structure: {selection.chosen_label} (fit: {selection.fit_level})",
        content=content,
        question="What economic model structure best fits this company?",
        methodology="Schema candidates scored against orientation evidence. "
                    "Disqualifying signals eliminate candidates. "
                    "Identifying signals scored by weight. "
                    f"Selection: {selection.chosen_label} "
                    f"(confidence {selection.confidence:.0%}, fit: {selection.fit_level}).",
        run_id=run_id,
    )

    return wid
