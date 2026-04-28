"""
Research Pipeline

One command: ticker -> full research output.

Full flow (13 steps):
  1. Fetch structured financials (Polygon -> Alpha Vantage -> registry)
  2. Fetch filing text (EDGAR 8-K/10-K)
  3. Fetch consensus (yfinance)
  4. Build research brief (one rich Claude call)
  5. Validate brief (schema sanity, driver bounds)
  6. DB Setup + Orientation (business_understanding.py)
  7. Research Plan (research_designer.py)
  8. Build estimate (ModelSpec + DriverDecomposition -> EPS)
  9. Estimate Building + Claims (core_workflow.py)
  10. Adversarial challenge (ContradictionCapture + independent Claude call)
  11. Edge detection (back-solve consensus, variant drivers, actionability)
  12. Valuation + Baseline
  13. Decision gate + workpapers + save

Uses in-memory SQLite for provenance. Uses the full research infrastructure.
Also works for any ticker via API-only mode (no registry required).
"""

import json
import os
from pathlib import Path
from datetime import datetime
from core.provenance.database import init_db, new_id, upsert, RunContext, now_iso
from research.financials_fetcher import fetch_financials, StructuredFinancials
from research.deep_research import build_research_brief, ResearchBrief
from research.estimate_model import (
    ModelSpec, Driver, DriverComponent, DriverDecomposition,
)
from research.sector_drivers import DRIVER_REGISTRY
from research.company_registry import COMPANY_REGISTRY
from research.escalation import WorkpaperBuilder


# ---------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------

def grade_estimate(confidences: list) -> str:
    if not confidences:
        return "FAIL"
    avg = sum(confidences) / len(confidences)
    low = sum(1 for c in confidences if c < 0.40)
    if avg >= 0.55 and min(confidences) >= 0.35 and low == 0:
        return "PASS"
    if avg >= 0.40 and low <= 1:
        return "WARN"
    return "FAIL"


def quality_line(ext_grade, schema_grade, est_grade):
    sym = {"A": "[+]", "B": "[~]", "C": "[-]", "F": "[x]",
           "PASS": "[+]", "WARN": "[~]", "FAIL": "[x]"}
    return (f"extraction {sym.get(ext_grade,'?')}{ext_grade} | "
            f"schema {sym.get(schema_grade,'?')}{schema_grade} | "
            f"estimate {sym.get(est_grade,'?')}{est_grade}")


# ---------------------------------------------------------------
# Brief validation
# ---------------------------------------------------------------

def validate_brief(brief: ResearchBrief, financials: StructuredFinancials) -> list[str]:
    """Quick sanity checks on the Claude brief."""
    warnings = []
    if brief.schema_type == "software" and financials.gross_margin_pct and financials.gross_margin_pct < 40:
        warnings.append(f"Software schema but gross margin only {financials.gross_margin_pct:.1f}%")
    if brief.schema_type == "restaurant" and financials.gross_margin_pct and financials.gross_margin_pct > 70:
        warnings.append(f"Restaurant schema but gross margin {financials.gross_margin_pct:.1f}% (unusually high)")
    if len(brief.drivers) == 0:
        warnings.append("No drivers in brief")
    if len(brief.contradictions) == 0:
        warnings.append("No contradictions identified")
    for d in brief.drivers:
        for c in d.get("components", []):
            val = c.get("value", 0)
            unit = c.get("unit", "pct")
            if unit == "bps" and abs(val) > 500:
                warnings.append(f"{d['name']}.{c['name']}: {val} bps seems extreme")

    # --------------------------------------------------------------
    # Guidance anchoring check
    # --------------------------------------------------------------
    # Rule: when guidance_vs_our_view signals a divergence, the driver's
    # basis prose MUST cite management's guide explicitly (quote numbers or
    # direct guide language). Generic bear/bull prose is not sufficient.
    # This prevents the pipeline from producing -21% variance vs. consensus
    # on a company that just raised guide, without a concrete reason.
    gvo = getattr(brief, "guidance_vs_our_view", {}) or {}
    guide_cite_keywords = [
        "guide", "guided", "guidance", "raised guide", "lowered guide",
        "reaffirmed", "maintained guide", "management expects",
        "mgmt expects", "guided to", "guided $", "guided range",
        "sandbag", "fade",  # explicit framings of why guide may be wrong
    ]
    for d in brief.drivers:
        basis_blob = " ".join(
            (c.get("basis", "") or "")
            for c in d.get("components", [])
        ).lower()
        has_guide_cite = any(k in basis_blob for k in guide_cite_keywords)
        drv_key = d.get("assumption_key") or d.get("name", "")
        gvo_text = " ".join(
            str(v) for k, v in gvo.items()
            if k.lower() in drv_key.lower() or drv_key.lower() in k.lower()
        ).lower()
        divergence_signaled = any(k in gvo_text for k in
                                   ("above", "below", "diverg", "disagree",
                                    "conservative", "aggressive"))
        if divergence_signaled and not has_guide_cite:
            warnings.append(
                f"{d.get('name','?')} diverges from guide per guidance_vs_our_view "
                f"but the basis prose does not cite management's guide directly. "
                f"A divergence-from-guide thesis requires an explicit guide quote "
                f"('mgmt guided $X-Y on Q_ call') and a named reason for the gap, "
                f"not general skepticism."
            )

    # Restaurant/franchise: SSS comp decomposition discipline
    # Mirrors hedge-fund-equity-pitch SKILL.md "Comp Decomposition Discipline" section.
    # Rule: Never pitch easy comps without decomposing what created the hard comps.
    if brief.schema_type in ("restaurant", "franchise_restaurant"):
        sss_driver = next(
            (d for d in brief.drivers
             if "sss" in (d.get("assumption_key", "") or "").lower()
             or "sss" in (d.get("name", "") or "").lower()),
            None,
        )
        if sss_driver:
            components = sss_driver.get("components", [])
            sss_total = sum(c.get("value", 0) for c in components)
            if sss_total > 0:
                # The "easy comps" case the discipline targets
                decomposition_keywords = [
                    "new product", "core", "traffic", "ticket", "mix",
                    "repeat", "trial", "lapping", "lap ", "category",
                    "media spend", "innovation", "limited time", "lto",
                ]
                gap_keywords = ["new product", "repeat", "trial", "lap", "category"]
                decomp_hits = 0
                for c in components:
                    basis = (c.get("basis", "") or "").lower()
                    if any(k in basis for k in decomposition_keywords):
                        decomp_hits += 1
                gaps_text = " ".join(
                    (g or "").lower() for g in (brief.evidence_gaps or [])
                )
                gap_addressed = any(k in gaps_text for k in gap_keywords)
                if decomp_hits == 0 and not gap_addressed:
                    warnings.append(
                        "Restaurant/franchise SSS is positive but lacks comp decomposition "
                        "(missing: new product vs. core, repeat vs. trial occasion, "
                        "category ownership, management investment, lap risk if cycling negative). "
                        "Pitch discipline: never pitch easy comps without decomposing them."
                    )
                elif decomp_hits == 1:
                    warnings.append(
                        "Restaurant/franchise SSS decomposition is thin (only 1 component "
                        "addresses comp drivers). Verify new product vs. core split, "
                        "repeat/trial occasion, and lap risk."
                    )
    return warnings


# ---------------------------------------------------------------
# Brief -> ModelSpec
# ---------------------------------------------------------------

def brief_to_model(brief, financials, registry_data=None):
    """Convert brief + financials into ModelSpec inputs."""
    schema_key = brief.schema_type or "general"
    driver_schema = DRIVER_REGISTRY.get(schema_key, DRIVER_REGISTRY["general"])

    # Prior year: registry if available (has operational detail), else API
    reg_prior = registry_data.get("prior_year", {}) if registry_data else {}
    if reg_prior:
        prior_year = dict(reg_prior)
        if financials.revenue_m and financials.source != "none":
            prior_year["revenue_m"] = financials.revenue_m
    else:
        prior_year = _build_prior_year(schema_key, financials)

    # Constants: registry if available, else API
    reg_constants = registry_data.get("constants", {}) if registry_data else {}
    constants = {
        "tax_rate": reg_constants.get("tax_rate") or financials.tax_rate or 0.25,
        "shares_m": reg_constants.get("shares_m") or financials.diluted_shares_m or 100,
        "net_interest_m": reg_constants.get("net_interest_m", financials.net_interest_m or 0),
    }

    # Drivers: use Claude's drivers (the brief is the research engine)
    assumptions = dict(brief.extra_assumptions)
    dd = DriverDecomposition()
    all_confidences = []
    for d in brief.drivers:
        components = {}
        for c in d.get("components", []):
            comp = DriverComponent(
                name=c["name"], value=c["value"],
                unit=c.get("unit", "pct"),
                basis=c.get("basis", ""),
                confidence=c.get("confidence", 0.5),
            )
            components[c["name"]] = comp
            all_confidences.append(comp.confidence)
        dd.add_driver(Driver(
            driver_name=d["name"], assumption_key=d["assumption_key"],
            formula=d.get("formula", "sum"), unit=d.get("unit", "pct"),
            components=components,
        ))

    for driver in dd.drivers.values():
        assumptions[driver.assumption_key] = 0

    model = ModelSpec(assumptions=assumptions, prior_year=prior_year,
                      constants=constants, driver_schema=driver_schema)
    dd.inject_into_model(model)

    # Handle simple_growth mapping
    if driver_schema.get("revenue_model") == "simple_growth":
        rgp = model.assumptions.get("revenue_growth_pct", 0)
        if rgp == 0:
            sss = model.assumptions.get("sss_growth_pct", 0)
            nrp = model.assumptions.get("net_retention_pct", 0)
            new_arr = model.assumptions.get("new_arr_growth_pct", 0)
            if sss != 0:
                new_stores = model.assumptions.get("new_restaurants", 0)
                store_count = prior_year.get("store_count", 1)
                model.assumptions["revenue_growth_pct"] = sss + new_stores / max(store_count, 1) * 100 * 0.5
            elif nrp != 0:
                model.assumptions["revenue_growth_pct"] = (nrp - 100) + new_arr

    return model, dd, all_confidences, schema_key, prior_year, constants


def _build_prior_year(schema_key, fin):
    base = {"revenue_m": fin.revenue_m or 0, "store_count": 1}
    if schema_key == "restaurant":
        cost_pct = 100 - (fin.gross_margin_pct or 75)
        base.update({"food_pct": cost_pct*0.42, "labor_pct": cost_pct*0.38,
                      "occupancy_pct": cost_pct*0.08, "other_operating_pct": cost_pct*0.12,
                      "cash_ga_m": fin.sga_m or 0, "stock_comp_m": 0,
                      "da_m": fin.da_m or 0, "preopen_m": 0, "prior_new_restaurants": 0})
    elif schema_key == "software":
        base.update({"cogs_pct": (100-fin.gross_margin_pct) if fin.gross_margin_pct else 20,
                      "sm_pct": fin.sga_pct*0.6 if fin.sga_pct else 25,
                      "rd_pct": fin.rd_pct or 15, "ga_pct": fin.sga_pct*0.15 if fin.sga_pct else 5,
                      "stock_comp_m": 0, "da_m": fin.da_m or 0})
    else:
        base.update({"cogs_pct": (100-fin.gross_margin_pct) if fin.gross_margin_pct else 70,
                      "opex_pct": fin.sga_pct or 15, "stock_comp_m": 0, "da_m": fin.da_m or 0})
    return base


# ---------------------------------------------------------------
# Structurally independent adversarial audit call
# ---------------------------------------------------------------
# MODEL SEPARATION DOCTRINE: The audit model must NOT receive the
# thesis model's self-generated contradictions, bear revisions,
# confidence scores, evidence gaps, or internal reasoning chain.
# It receives ONLY: (1) raw financials, (2) filing text, (3) the
# analyst's thesis conclusion + key assumptions. It must discover
# problems independently to avoid correlated self-confirmation.
# ---------------------------------------------------------------

# Preferred audit model — use a different model when available.
# Falls back to same model with information barrier if only one is available.
AUDIT_MODEL = os.environ.get("AUDIT_MODEL", "claude-sonnet-4-20250514")
THESIS_MODEL = "claude-sonnet-4-20250514"  # used in deep_research.py


def call_adversarial_claude(brief, filing_text, verbose=False,
                            financials_summary=None, consensus_eps=None,
                            corpus: dict | None = None):
    """
    Structurally independent adversarial audit call.

    INFORMATION BARRIER enforced:
      ✓ Receives: business description, key debate, schema, driver names + values
      ✓ Receives: LABELED evidence corpus (filing, transcripts, press releases,
        deck digest), structured financials, consensus estimates
      ✗ Does NOT receive: brief.contradictions (thesis model's self-generated bear cases)
      ✗ Does NOT receive: brief.bear_revisions (thesis model's self-corrections)
      ✗ Does NOT receive: confidence scores, evidence_gaps, reasoning chain

    The auditor must independently discover what's wrong with the thesis.

    Corpus handling:
      The `corpus` kwarg (preferred) accepts a dict of {source_name: text}:
        {"filing":    "<10-K/10-Q text>",
         "transcripts": "<combined Q&A/prepared remarks>",
         "press_releases": "<recent PR headlines/bodies>",
         "deck_digest": "<investor-deck subagent digest>"}
      Each source is rendered under its own header so the auditor knows
      which sources it's reading — crucial for existence-check claims like
      "X is not mentioned anywhere." Legacy callers that pass only
      `filing_text` still work (it's fed as the sole "filing" source).
    """
    # Build the evidence corpus. Prefer explicit labeled dict; fall back to
    # the legacy single-blob filing_text.
    if corpus is None:
        corpus = {"filing": filing_text or ""}
    else:
        # Caller may still pass filing_text; if the corpus dict already has
        # a "filing" entry, trust it. Otherwise fill from filing_text.
        if not corpus.get("filing") and filing_text:
            corpus = {**corpus, "filing": filing_text}

    total_corpus_len = sum(len(v or "") for v in corpus.values())
    if total_corpus_len < 200:
        return None
    import httpx
    from research.deep_research import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        return None

    # Build thesis summary — conclusion only, no self-critique
    drivers_summary = "\n".join(
        f"  {d['name']}: " + ", ".join(f"{c['name']}={c['value']}" for c in d.get("components", []))
        for d in brief.drivers)

    # System prompt: different persona from thesis model
    system_prompt = (
        "You are a skeptical portfolio manager reviewing a pitch from a junior analyst. "
        "Your job is to find what the analyst missed, got wrong, or is fooling themselves about. "
        "You are NOT trying to improve the thesis — you are trying to REJECT it. "
        "Focus on blind spots the analyst likely didn't consider, not on obvious risks "
        "they probably already thought about. Look for structural problems, not cosmetic ones."
    )

    # User prompt: thesis conclusion + raw data, NO self-generated contradictions
    financials_block = ""
    if financials_summary:
        financials_block = f"\nSTRUCTURED FINANCIALS:\n{financials_summary[:2000]}\n"

    consensus_block = ""
    if consensus_eps:
        consensus_block = f"\nCONSENSUS EPS: ${consensus_eps:.2f}\n"

    # Schema-specific audit rules. Mirrors the "Comp Decomposition Discipline"
    # section in the hedge-fund-equity-pitch SKILL.md. For restaurant/franchise
    # pitches involving SSS, the auditor MUST ask the 5 comp questions.
    schema_audit_block = ""
    if brief.schema_type in ("restaurant", "franchise_restaurant"):
        sss_driver = next(
            (d for d in brief.drivers
             if "sss" in (d.get("assumption_key", "") or "").lower()
             or "sss" in (d.get("name", "") or "").lower()),
            None,
        )
        if sss_driver:
            sss_total = sum(c.get("value", 0) for c in sss_driver.get("components", []))
            direction = "POSITIVE (easy comps case)" if sss_total > 0 else "NEGATIVE/FLAT"
            schema_audit_block = f"""
RESTAURANT/FRANCHISE COMP DECOMPOSITION AUDIT (mandatory for this schema):
SSS direction: {direction} (total: {sss_total:+.2f}%)

Before accepting the SSS thesis, the analyst MUST have answered these 5 questions.
If any are missing from the basis fields or evidence_gaps, flag as a serious contradiction:
  1. What % of the comp came from a NEW PRODUCT vs. CORE business?
     (If unanswered: thesis is undecomposed — the comp could vanish next quarter.)
  2. Is the new product a REPEAT occasion or a TRIAL occasion?
     (Trial = one-time tourist traffic; repeat = sustainable. Big difference for run-rate.)
  3. Does the company OWN THE CATEGORY, or is it a new entry being copied by competitors?
     (Category ownership = pricing power; new entry = competitor response coming.)
  4. Is management INVESTING in the product (media spend, operational capacity)?
     (No investment = company doesn't believe; heavy investment = sustained.)
  5. What's the QUANTIFIED COMP DRAG if the company laps a negative period next year?
     (Easy comps cycle. If the analyst hasn't quantified the lap, the out-year is hollow.)

RULE: Never let the analyst pitch easy comps without decomposing what created the hard comps.
If 2+ questions are unanswered, the SSS driver should be downgraded as a contradiction.
"""

    # --- Render the evidence corpus with per-source labels ---
    # Budgets: give transcripts + deck significant room (that's where Smart-
    # Kitchen-style operating detail lives). Filing gets the most because
    # it's the legal record. Press releases are shortest — mostly headlines.
    SOURCE_LABELS = {
        "filing":         ("FILING TEXT (10-K / 10-Q)", 4500),
        "transcripts":    ("RECENT EARNINGS TRANSCRIPTS (prepared remarks + Q&A)", 4500),
        "press_releases": ("RECENT EARNINGS PRESS RELEASES", 2500),
        "deck_digest":    ("INVESTOR DECK / INVESTOR DAY DIGEST", 2500),
        "macro_context":  ("MACRO CONTEXT — FRED (savings rate, sentiment, headline CPI, unemployment, earnings)", 2000),
        "bls_context":    ("BLS CONTEXT (category CPI incl. food-away vs food-at-home; sector hourly earnings)", 2000),
        "bea_context":    ("BEA PCE CONTEXT (consumer spend by category, real disposable income, saving rate)", 2000),
        "peer_comps":     ("PEER CONSENSUS TABLE (forward growth & revisions)", 1500),
    }
    corpus_parts = []
    sources_present = []
    for key, (label, budget) in SOURCE_LABELS.items():
        txt = (corpus.get(key) or "").strip()
        if not txt:
            continue
        sources_present.append(key)
        if len(txt) > budget:
            txt = txt[:budget] + f"\n...[truncated at {budget:,} chars]"
        corpus_parts.append(f"--- {label} ---\n{txt}")
    # Any extra sources the caller passed that we don't have a label for
    for key, txt in (corpus or {}).items():
        if key in SOURCE_LABELS or not txt:
            continue
        sources_present.append(key)
        body = txt.strip()
        if len(body) > 2000:
            body = body[:2000] + "\n...[truncated at 2,000 chars]"
        corpus_parts.append(f"--- {key.upper()} ---\n{body}")

    corpus_block = "\n\n".join(corpus_parts) if corpus_parts else "(no corpus text available)"
    sources_list = ", ".join(sources_present) if sources_present else "(none)"

    prompt = f"""Review this equity pitch. Find what the analyst missed or got wrong.

ANALYST'S THESIS:
Business: {brief.business_description}
Key debate: {brief.key_debate}
Edge hypothesis: {brief.edge_hypothesis}
Direction: {"LONG" if "long" in brief.edge_type.lower() or "under" in brief.why_market_is_wrong.lower() else "SHORT" if "short" in brief.edge_type.lower() or "over" in brief.why_market_is_wrong.lower() else "LONG"}
Schema: {brief.schema_type}

ANALYST'S KEY ASSUMPTIONS:
{drivers_summary}
{consensus_block}{financials_block}
EVIDENCE CORPUS (for independent verification — sources available: {sources_list}):
{corpus_block}
{schema_audit_block}
INSTRUCTIONS:
1. What contradictions exist ACROSS THE CORPUS (filing, transcripts, press releases, deck) that the analyst may not have considered? Cite which source supports each contradiction.
2. Which assumptions look weakest when checked against the raw data?
3. What blind spots does this thesis have — things the analyst isn't even thinking about?
4. What's the strongest short-form bear case against this pitch?
5. If a SCHEMA-SPECIFIC AUDIT block was provided above, run through each numbered question and flag missing answers as contradictions (severity = "serious" if 2+ unanswered).
6. Produce 3-4 STRUCTURAL CRITIQUES that attack the pitch at its weakest structural points. Each must tag the section(s) it belongs in so a research note renderer can place it inline. Valid section tags: "edge", "drivers", "consensus", "valuation", "catalysts", "risks". Do NOT produce filler critiques -- better 3 sharp ones than 8 generic ones.

CRITICAL — EXISTENCE CHECKS:
Before claiming an entity / initiative / number is "not mentioned" or "absent,"
you MUST confirm absence across ALL sources listed above, not just one. A
concept frequently appears in transcripts or decks but not in 10-K text
(e.g. operational initiatives, product launches). If you search ONLY the
filing for a term, you WILL produce false phantom-entity flags. State the
sources you checked in your counter_evidence.

Respond in JSON:
{{"new_contradictions": [{{"thesis": "what the analyst claims", "counter_evidence": "what the data actually shows (cite source: filing/transcripts/press/deck)", "severity": "serious|moderate|minor", "affected_driver": "driver_name"}}],
"additional_revisions": [{{"driver": "...", "component": "...", "new_value": 0.0, "reason": "..."}}],
"blind_spots": ["things the analyst isn't considering at all"],
"structural_critiques": [{{"target_sections": ["edge|drivers|consensus|valuation|catalysts|risks"], "claim_under_attack": "short paraphrase of what the analyst asserts in that section", "counter_argument": "the steel-manned opposing view", "severity": "serious|moderate|minor"}}],
"overlap_with_obvious": ["if any of your findings are things the analyst probably already knows, flag them here"],
"overall_assessment": "one sentence verdict on whether this pitch holds up"}}"""

    try:
        resp = httpx.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": AUDIT_MODEL, "max_tokens": 3500,
                  "system": system_prompt,
                  "messages": [{"role": "user", "content": prompt}],
                  "metadata": {"user_id": "audit_model"}},
            timeout=90.0)
        if resp.status_code != 200:
            if verbose:
                print(f"  Audit model: HTTP {resp.status_code}")
            return None
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
        start, end = text.find("{"), text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])

            # Separation quality check: flag if audit findings overlap with thesis contradictions
            audit_drivers = {c.get("affected_driver","") for c in data.get("new_contradictions",[])}
            thesis_drivers = {c.get("affected_driver","") for c in brief.contradictions}
            overlap = audit_drivers & thesis_drivers
            overlap_pct = len(overlap) / max(len(audit_drivers), 1) * 100

            data["_separation_metadata"] = {
                "audit_model": AUDIT_MODEL,
                "thesis_model": THESIS_MODEL,
                "same_model": AUDIT_MODEL == THESIS_MODEL,
                "information_barrier": True,
                "thesis_contradictions_shared": False,
                "overlap_pct": round(overlap_pct, 1),
                "overlap_warning": overlap_pct > 50,
            }

            if verbose:
                marker = "⚠ HIGH OVERLAP" if overlap_pct > 50 else "✓ independent"
                print(f"  Audit model ({AUDIT_MODEL}): {len(data.get('new_contradictions',[]))} contradictions, "
                      f"{len(data.get('additional_revisions',[]))} revisions, "
                      f"{len(data.get('blind_spots',[]))} blind spots "
                      f"[separation: {overlap_pct:.0f}% overlap {marker}]")
            return data
    except Exception as e:
        if verbose:
            print(f"  Audit model: error - {e}")
    return None


# Fuzzy match for bear revisions
_ALIASES = {
    "same_store_sales_growth": "sss_growth", "comparable_store_sales_growth": "sss_growth",
    "traffic_growth": "traffic", "average_check_growth": "ticket", "ticket_growth": "ticket",
    "new_store_count": "new_stores", "new_restaurant_count": "new_stores",
    "net_new_openings": "guidance_midpoint", "company_new_openings": "guidance_midpoint",
    "food_cost_inflation": "food_cost", "food_cost_pressure": "food_cost",
    "labor_cost_inflation": "labor_cost", "labor_cost_pressure": "labor_cost",
    "commodity_inflation_impact": "commodity", "commodity_cost_pressure": "commodity",
    "wage_inflation_net_productivity": "wage_pressure", "net_labor_cost_impact": "wage_pressure",
    "labor_cost_change": "labor_cost", "pricing_mitigation": "pricing_offset",
    "new_restaurant_openings": "new_stores", "productivity_improvements": "throughput_offset",
    "customer_expansion_retention": "revenue_growth", "net_retention_rate": "revenue_growth",
}

def _fuzzy_match(target, candidates):
    if target in candidates: return target
    if target in _ALIASES and _ALIASES[target] in candidates: return _ALIASES[target]
    t_tokens = set(target.lower().replace("-","_").split("_"))
    best, best_score = None, 0
    for c in candidates:
        c_tokens = set(c.lower().replace("-","_").split("_"))
        overlap = len(t_tokens & c_tokens)
        score = overlap / max(len(t_tokens), len(c_tokens)) if t_tokens and c_tokens else 0
        if score > best_score and score >= 0.4: best, best_score = c, score
    return best

# Max per-revision relative move as a fraction of the component's current
# value. Adversarial may want to slash a driver 50%+ but that compounds
# bearishness when the baseline brief is already bearish vs consensus.
# Cap at 40% (so +7.0% can fall to +4.2%, not +4.0%; +3.0% can fall to
# +1.8%, not +1.5%). The adversarial still produces a meaningful stress
# test without producing cartoonish bear cases.
MAX_REVISION_FRACTION = 0.40


def apply_bear_revisions(revisions, dd, model, verbose=False):
    """Apply bear revisions with fuzzy matching + magnitude cap + sanity checks."""
    traces = []
    for rev in revisions:
        matched_d = _fuzzy_match(rev.get("driver",""), set(dd.drivers.keys()))
        if not matched_d: continue
        driver = dd.drivers[matched_d]
        matched_c = _fuzzy_match(rev.get("component",""), set(driver.components.keys()))
        if not matched_c: continue
        current = driver.components[matched_c].value
        new_val = rev["new_value"]
        if current != 0 and abs(new_val) > abs(current) * 10: continue

        # Cap the revision magnitude. If adversarial proposes a change
        # larger than MAX_REVISION_FRACTION of the current value, shrink
        # it to the cap in the same direction. Prevents compounding
        # bearishness stacking into extreme outputs.
        capped_val = new_val
        if current != 0:
            max_delta = abs(current) * MAX_REVISION_FRACTION
            proposed_delta = new_val - current
            if abs(proposed_delta) > max_delta:
                direction = 1 if proposed_delta > 0 else -1
                capped_val = round(current + direction * max_delta, 3)
                if verbose:
                    print(f"  [adversarial cap] {matched_d}.{matched_c}: "
                          f"{current:+.1f} -> {new_val:+.1f} "
                          f"CAPPED to {capped_val:+.1f} "
                          f"(max {MAX_REVISION_FRACTION*100:.0f}% relative move)")

        try:
            trace = dd.revise_component(matched_d, matched_c, capped_val, model, reason=rev.get("reason",""))
            traces.append(trace)
            if verbose: print(f"  {trace['chain']}")
        except (ValueError, KeyError): pass
    return traces


# ---------------------------------------------------------------
# DAG-based entry (parallel fetch + analysis layer)
# ---------------------------------------------------------------

def run_research_dag(
    ticker: str,
    *,
    verbose: bool = False,
    read_cache: bool = True,
    write_cache: bool = True,
    max_parallel: int = 4,
) -> tuple[dict, "DagTrace"]:
    """
    Run the fetch + analysis layer of the research pipeline as a DAG.

    Unlike `run_research()` which is linear, this:
      - Fetches financials, filings, transcripts, consensus, market overlay,
        and press releases IN PARALLEL (up to max_parallel at once)
      - Caches each step's output by content hash; re-runs skip unchanged
        steps
      - Runs the transcript digest (8 subagents) as soon as transcripts are
        ready — doesn't wait for other fetches
      - Emits a trace JSON at data/dag_traces/{ticker}_{timestamp}.json

    Returns (results_dict, trace). The results dict has entries keyed by
    step name:
      {
        "financials": {...},
        "filing_text": {"text": "...", "filing_type": "10-K"},
        "transcripts": {"text": "...", "char_count": N},
        "consensus": {"consensus": {...}, "data": {...}},
        "market_overlay": {...},
        "press_releases": [...],
        "transcript_digest": {...},  # 8 subagent outputs
      }

    Downstream (research brief, model, adversarial, edge, valuation,
    output rendering) stays in `run_research()` for now — those steps are
    tightly coupled and don't benefit from DAG overhead.
    """
    from research.dag import run_dag
    from research.dag.steps import build_research_steps

    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 10 or not all(c.isalpha() or c in '.-' for c in ticker):
        raise ValueError(f"Invalid ticker: '{ticker}'")

    registry_data = COMPANY_REGISTRY.get(ticker)

    context = {
        "ticker": ticker,
        "registry_data": registry_data,
        "verbose": verbose,
    }

    if verbose:
        print(f"\n{'='*60}\nRESEARCH DAG: {ticker}\n{'='*60}")

    results, trace = run_dag(
        build_research_steps(),
        ticker=ticker,
        context=context,
        max_parallel=max_parallel,
        read_cache=read_cache,
        write_cache=write_cache,
        verbose=verbose,
    )

    if verbose:
        print(f"\n  [DAG] total wall-clock: {trace.total_duration_seconds:.1f}s")
        n_cached = sum(1 for s in trace.steps if s.status == "cached")
        n_ok = sum(1 for s in trace.steps if s.status == "ok")
        n_fail = sum(1 for s in trace.steps if s.status == "failed")
        print(f"  [DAG] {n_ok} fresh, {n_cached} cached, {n_fail} failed")

    return results, trace


# ---------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------

def run_research(ticker: str, verbose: bool = False) -> dict:
    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 10 or not all(c.isalpha() or c in '.-' for c in ticker):
        raise ValueError(f"Invalid ticker: '{ticker}'")

    registry_data = COMPANY_REGISTRY.get(ticker)

    def v(msg):
        if verbose: print(msg)

    v(f"\n{'='*60}")
    v(f"RESEARCH: {ticker}")
    v(f"{'='*60}")

    # ── Steps 1–3b: DAG (parallel fetches + transcript digest) ──
    # Previously these were 5 sequential inline blocks. The DAG runs
    # financials + filing_text + transcripts + consensus + market_overlay
    # + press_releases in parallel (up to 4 at a time), then transcript
    # digest once transcripts are ready. Content-hash cached — re-runs
    # skip steps whose inputs haven't changed.
    v(f"\n-- Fetch + Analysis DAG --")
    dag_results, dag_trace = run_research_dag(
        ticker, verbose=verbose, read_cache=True, write_cache=True,
    )

    # --- Reconstruct StructuredFinancials from DAG output ---
    from dataclasses import fields as _dc_fields
    fin_dict = dag_results.get("financials") or {}
    if fin_dict:
        _fin_fields = {f.name for f in _dc_fields(StructuredFinancials)}
        financials = StructuredFinancials(
            **{k: v for k, v in fin_dict.items() if k in _fin_fields}
        )
    else:
        # DAG failed for financials — fall back to a direct fetch so we
        # don't kill the whole pipeline on a transient API blip.
        financials = fetch_financials(ticker, registry_data=registry_data, verbose=verbose)
    if not financials.revenue_m:
        raise ValueError(f"No financial data for {ticker}")
    v(f"  Revenue: ${financials.revenue_m:,.1f}M  EPS: ${financials.diluted_eps:.2f}")
    if financials.original_currency != "USD":
        v(f"  (converted from {financials.original_currency} at FX {financials.fx_rate_applied:.6f})")

    # --- Filing text: registry + EDGAR fetch ---
    filing_text = registry_data.get("earnings_text", "") if registry_data else ""
    ft_dict = dag_results.get("filing_text") or {}
    ft_body = ft_dict.get("text", "") if isinstance(ft_dict, dict) else ""
    if ft_body:
        filing_text = (filing_text + "\n\n" + ft_body).strip() if filing_text else ft_body
        v(f"  Filing text: {ft_dict.get('filing_type','?')} ({len(ft_body):,} chars)")
    if not filing_text:
        filing_text = (f"{ticker} fiscal year results. "
                        f"Revenue ${financials.revenue_m:,.1f}M. "
                        f"EPS ${financials.diluted_eps:.2f}.")

    # --- Transcripts: append raw transcript text ---
    tr_dict = dag_results.get("transcripts") or {}
    tr_text = tr_dict.get("text", "") if isinstance(tr_dict, dict) else ""
    if tr_text:
        filing_text = filing_text + "\n\nEARNINGS CALL TRANSCRIPTS (3 YEARS):\n" + tr_text
        v(f"  Transcripts: {tr_dict.get('char_count', len(tr_text)):,} chars")

    # --- Consensus ---
    cons_wrap = dag_results.get("consensus") or {}
    consensus = cons_wrap.get("consensus") or (registry_data.get("consensus", {}) if registry_data else {})
    consensus_data = cons_wrap.get("data") or {}
    consensus_full_dict = cons_wrap.get("full")  # dict form (or None)
    cons_eps = consensus.get("eps")
    if cons_eps:
        n_analysts = consensus_data.get("analyst_count", "?")
        v(f"  Consensus EPS(FY): ${cons_eps:.2f}  ({n_analysts} analysts)")
        if consensus_full_dict:
            ny = consensus_full_dict.get("next_year") or {}
            if ny.get("eps_mean") is not None:
                g = ny.get("eps_growth_yoy")
                v(f"  Consensus EPS(+1Y): ${ny['eps_mean']:.2f}"
                  + (f" ({g*100:+.1f}% YoY)" if g is not None else ""))
            ne = consensus_full_dict.get("next_earnings") or {}
            if ne.get("date"):
                v(f"  Next earnings: {ne['date']} ({ne.get('days_out','?')}d out)")

    # --- Transcript digest (reconstruct TranscriptDigest from DAG dict) ---
    # Preserve the ORIGINAL filing text as a separate source so the
    # adversarial audit can see each corpus labeled (prevents phantom-entity
    # flags like "Smart Kitchen not in filing" when it IS in transcripts).
    raw_filing_text = filing_text or ""
    transcripts_corpus_text = ""
    deck_corpus_text = ""
    press_corpus_text = ""
    transcript_analysis = None
    td_dict = dag_results.get("transcript_digest")
    if td_dict:
        try:
            from research.transcript_analyzer import TranscriptDigest
            _td_fields = {f.name for f in _dc_fields(TranscriptDigest)}
            transcript_analysis = TranscriptDigest(
                **{k: v for k, v in td_dict.items() if k in _td_fields}
            )
            # Rebuild derived convenience fields (tone_trajectory, etc.)
            transcript_analysis._derive()
            analysis_text = transcript_analysis.to_prompt_text()
            transcripts_corpus_text = analysis_text
            filing_text = filing_text + "\n\n" + analysis_text
            ok_subs = sum(1 for s in transcript_analysis.subagents.values() if s.get("ok"))
            v(f"  Transcript digest injected: {ok_subs}/{len(transcript_analysis.subagents)} "
              f"subagents ok, {len(analysis_text):,} chars")
        except Exception as e:
            v(f"  Transcript digest reconstruction: {type(e).__name__}: {e}")

    # --- Collect press releases corpus for adversarial (labeled source) ---
    pr_dicts = dag_results.get("press_releases") or []
    if pr_dicts:
        pr_parts = []
        for pr in pr_dicts[:4]:  # most recent 4 quarters
            if isinstance(pr, dict):
                header = f"=== {pr.get('ticker','?')} {pr.get('quarter','?')} " \
                         f"({pr.get('report_date','?')}) ==="
                body = (pr.get("full_text_with_tables") or pr.get("text") or "")[:4000]
                if body:
                    pr_parts.append(f"{header}\n{body}")
        press_corpus_text = "\n\n".join(pr_parts)
        if press_corpus_text:
            v(f"  Press-release corpus collected: {len(pr_dicts)} releases, "
              f"{len(press_corpus_text):,} chars for adversarial")

    # ── Step 3c: Slide Deck Analysis (vision subagents) ──
    # Pulls recent investor decks (EDGAR + IR) and runs 8 vision subagents
    # (guidance, LRP, QTD, narrative, segments, capital, targets, new
    # initiatives). Cached by PDF content hash — cost-free on re-runs.
    # Feeds the research brief with deck-only content (LRP targets,
    # cohort charts, investor day narratives) that transcripts / press
    # releases don't surface.
    #
    # Gated on DECK_ANALYSIS_ENABLED env var (default on). Cost is
    # ~$2/deck on cold runs so we cap at 2 decks per research run:
    # the most recent earnings deck + most recent investor day.
    deck_digest_dicts: list = []   # outer-scope so guidance_extractor can read it
    if os.environ.get("DECK_ANALYSIS_ENABLED", "1") != "0":
        try:
            from ingestion.loaders.slide_deck_loader import fetch_all_slide_decks
            from research.deck_analyzer import analyze_slide_deck
            v(f"\n-- Slide Deck Analysis --")
            decks = fetch_all_slide_decks(
                ticker, quarters=2, max_ir_decks=4, verbose=verbose,
            )
            # Pick top 2 decks by type priority: most recent earnings +
            # most recent investor_day. Skip other types unless nothing
            # else is available.
            picked = []
            seen_types = set()
            priority = ["earnings", "investor_day", "conference",
                        "shareholder_letter", "other"]
            for dtype in priority:
                for d in decks:
                    if d.deck_type == dtype and d.deck_type not in seen_types:
                        picked.append(d)
                        seen_types.add(d.deck_type)
                        if len(picked) >= 2:
                            break
                if len(picked) >= 2:
                    break
            if not picked:
                v(f"  No decks available for analysis")
            # Track deck digests as dicts so the guidance_extractor downstream
            # can pull structured guides from `subagents.deck_guidance_extractor`.
            # (declared at outer scope above — accumulate here)
            for deck in picked:
                v(f"  Analyzing {deck.deck_type} deck: {deck.title[:50]} "
                  f"({deck.page_count}p, {deck.source})")
                try:
                    digest = analyze_slide_deck(
                        deck, verbose=verbose, force=False,
                    )
                except Exception as de:
                    v(f"    deck analysis failed: {de}")
                    continue
                if digest is None:
                    v(f"    deck analysis returned None")
                    continue
                deck_text = digest.to_prompt_text()
                # Also accumulate into the adversarial deck corpus
                # (multiple decks concatenated, newline-separated).
                deck_corpus_text = (deck_corpus_text + "\n\n" + deck_text).strip()
                filing_text = filing_text + "\n\n" + deck_text
                # Preserve the digest's dict shape for downstream guidance
                # extraction (without re-running deck analysis).
                try:
                    deck_digest_dicts.append(digest.to_dict())
                except Exception:
                    pass
                ok_subs = sum(1 for s in digest.subagents.values() if s.get("ok"))
                v(f"    Injected deck digest: {ok_subs}/{len(digest.subagents)} "
                  f"subagents ok, {len(deck_text):,} chars")
        except Exception as e:
            v(f"  Deck analysis skipped: {type(e).__name__}: {e}")

    # ── Step 3d: Macro context (FRED + BLS + BEA) + Peer comps ──
    # All optional — pipeline continues even if any fail. Each block gets
    # appended to filing_text so build_research_brief sees them, AND they
    # flow into the adversarial corpus separately (labeled).
    #
    # FRED: headline macro (savings rate, sentiment, CPI, unemployment,
    #       earnings). Public CSV endpoint, no key.
    # BLS:  category CPI (food away vs. at home) + sector hourly earnings
    #       (restaurant labor, retail labor). Public API, no key.
    # BEA:  PCE breakdown (food services $B, recreation $B, real DPI).
    #       Requires free BEA_API_KEY; gracefully skips if absent.
    macro_corpus_text = ""   # FRED block
    bls_corpus_text = ""     # BLS block
    bea_corpus_text = ""     # BEA block
    peer_corpus_text = ""
    try:
        from ingestion.loaders.fred_macro_loader import fetch_macro_context
        macro = fetch_macro_context(verbose=verbose)
        if macro and macro.series:
            macro_corpus_text = macro.to_prompt_text()
            filing_text = filing_text + "\n\n" + macro_corpus_text
            v(f"  FRED macro injected: {len(macro.series)} series, "
              f"{len(macro_corpus_text):,} chars")
    except Exception as e:
        v(f"  FRED macro skipped: {type(e).__name__}: {e}")

    try:
        from ingestion.loaders.bls_macro_loader import fetch_bls_context
        bls = fetch_bls_context(verbose=verbose)
        if bls and bls.series:
            bls_corpus_text = bls.to_prompt_text()
            filing_text = filing_text + "\n\n" + bls_corpus_text
            v(f"  BLS macro injected: {len(bls.series)} series, "
              f"{len(bls_corpus_text):,} chars")
    except Exception as e:
        v(f"  BLS macro skipped: {type(e).__name__}: {e}")

    try:
        from ingestion.loaders.bea_macro_loader import fetch_bea_context
        bea = fetch_bea_context(verbose=verbose)
        # Silent skip when no_key=True (user hasn't registered yet)
        if bea and bea.series and not bea.no_key:
            bea_corpus_text = bea.to_prompt_text()
            filing_text = filing_text + "\n\n" + bea_corpus_text
            v(f"  BEA macro injected: {len(bea.series)} series, "
              f"{len(bea_corpus_text):,} chars")
    except Exception as e:
        v(f"  BEA macro skipped: {type(e).__name__}: {e}")

    # Pre-declare so the post-brief safety-net block can reference it
    # even if the peer-comps try block below bails early on an error.
    _pre_brief_peer_schema = ""
    try:
        from research.peer_comps import fetch_peer_comps
        from research.peer_registry import (
            PEER_GROUPS, infer_schema_from_yfinance,
        )
        # Multi-tier schema inference so peer comps fire for ANY ticker:
        # 1) Explicit registry_data.schema (if caller set it)
        # 2) yfinance sector/industry → mapped schema (covers any public ticker)
        # 3) Subject ticker itself appears in a curated PEER_GROUPS list
        schema_guess = (registry_data or {}).get("schema") or ""
        if not schema_guess:
            schema_guess = infer_schema_from_yfinance(ticker, verbose=verbose)
        if not schema_guess:
            # Last-resort: subject ticker itself appears in a curated list
            for sk, tickers in PEER_GROUPS.items():
                if ticker.upper() in tickers:
                    schema_guess = sk
                    break
        _pre_brief_peer_schema = schema_guess
        if schema_guess:
            peers = fetch_peer_comps(ticker, schema_guess,
                                     max_peers=4, verbose=verbose)
            if peers and peers.rows:
                peer_corpus_text = peers.to_prompt_text()
                filing_text = filing_text + "\n\n" + peer_corpus_text
                v(f"  Peer comps injected: {len(peers.rows)} peers "
                  f"(schema={schema_guess}), {len(peer_corpus_text):,} chars")
            else:
                v(f"  Peer comps: schema={schema_guess} but no rows returned")
        else:
            v(f"  Peer comps skipped: no schema match for {ticker} "
              f"(yfinance didn't return a mappable industry/sector)")
    except Exception as e:
        v(f"  Peer comps skipped: {type(e).__name__}: {e}")

    # ── Step 3e: Aggregate management guidance from already-fetched sources ──
    # The guidance_extractor pulls structured guide items from the deck-
    # guidance subagent, the transcript guidance_tracker subagent, and a
    # light regex pass over press release text. This becomes the
    # MANAGEMENT GUIDANCE anchor block injected into the brief prompt.
    guidance_bundle = None
    try:
        from research.guidance_extractor import extract_guidance
        # Use the most recent deck digest (typically the shareholder letter
        # for the latest quarter) as the primary structured-guide source.
        primary_deck_dict = deck_digest_dicts[0] if deck_digest_dicts else None
        guidance_bundle = extract_guidance(
            ticker=ticker,
            transcript_digest=td_dict,
            deck_digest=primary_deck_dict,
            press_releases=pr_dicts,
        )
        v(f"  Guidance bundle: {len(guidance_bundle.items)} item(s) "
          f"from sources={guidance_bundle.sources_used}")
    except Exception as e:
        v(f"  Guidance bundle extraction skipped: {type(e).__name__}: {e}")

    # ── Step 4: Build research brief ──
    v(f"\n-- Research Brief --")
    brief = build_research_brief(ticker=ticker, financials=financials,
                                  earnings_text=filing_text,
                                  consensus_eps=cons_eps,
                                  consensus_revenue_m=consensus.get("revenue_m"),
                                  consensus_full=consensus_full_dict,
                                  guidance_bundle=guidance_bundle,
                                  verbose=verbose)

    # Post-brief peer-comps safety net: if the pre-brief attempt missed
    # (no schema match, or yfinance sector mapped to a different schema
    # than Claude's pick), re-run peer comps with brief.schema_type now
    # that we have it. The brief is already built — this result only
    # flows into the adversarial corpus and the Word report, not back
    # into the brief. Better than nothing; catches tickers where
    # yfinance industry strings didn't match our mapping.
    brief_schema = (brief.schema_type or "").strip().lower()
    if brief_schema and brief_schema != _pre_brief_peer_schema and not peer_corpus_text:
        try:
            from research.peer_comps import fetch_peer_comps
            from research.peer_registry import PEER_GROUPS
            if brief_schema in PEER_GROUPS:
                v(f"  Peer comps (post-brief retry with schema={brief_schema})...")
                peers = fetch_peer_comps(ticker, brief_schema,
                                         max_peers=4, verbose=verbose)
                if peers and peers.rows:
                    peer_corpus_text = peers.to_prompt_text()
                    v(f"  Peer comps injected post-brief: {len(peers.rows)} peers, "
                      f"{len(peer_corpus_text):,} chars")
        except Exception as e:
            v(f"  Peer comps post-brief retry skipped: {type(e).__name__}: {e}")
    is_api = brief.source_method == "claude_api"
    driver_count = sum(len(d.get("components",[])) for d in brief.drivers)
    ext_grade = "A" if is_api and driver_count >= 4 else "B" if is_api and driver_count >= 3 else "C" if driver_count >= 2 else "F"

    # Guard: if the brief call failed (no drivers), the downstream model
    # uses defaults → always produces nonsense EPS (like WING $16.52 /
    # 68% net margin on a restaurant). Abort with a clear error instead
    # of writing a bad result JSON + Word report.
    if brief.source_method == "claude_api_error" or driver_count == 0:
        raise ValueError(
            f"Research brief failed for {ticker} "
            f"(source={brief.source_method}, driver_count={driver_count}). "
            f"The brief Claude call errored or returned no drivers — retry "
            f"after rate limits clear, or check API key / network. "
            f"Not writing a partial result; any downstream EPS / valuation "
            f"would be fabricated from default values, not real analysis."
        )

    # ── Step 5: Validate brief ──
    warnings = validate_brief(brief, financials)
    if warnings: v(f"  Warnings: {'; '.join(warnings)}")

    # ── Step 5b: Multi-run convergence (anchor with prior results) ──
    convergence_adjustments = []
    outlier_flags = []
    try:
        from research.convergence import load_convergence, anchor_brief_with_priors, flag_outliers
        convergence = load_convergence(ticker)
        if convergence.num_runs >= 2:
            v(f"\n-- Convergence ({convergence.num_runs} prior runs) --")
            v(f"  Prior EPS median: ${convergence.converged_eps_median:.2f} "
              f"(spread: ${convergence.converged_eps_spread:.2f})")
            convergence_adjustments = anchor_brief_with_priors(
                brief, convergence, blend_weight=0.3, verbose=verbose)
            outlier_flags = flag_outliers(brief, convergence)
            if outlier_flags:
                for flag in outlier_flags:
                    v(f"  OUTLIER: {flag}")
                    warnings.append(f"Outlier: {flag}")
        else:
            v(f"\n-- Convergence: {convergence.num_runs} prior runs (need 2+) --")
    except Exception as e:
        v(f"  Convergence: {e}")

    # ── Step 6: DB Setup + Orientation ──
    conn = init_db(Path(":memory:"))
    with RunContext(conn, "setup", {"ticker": ticker}) as run:
        cid = new_id()
        conn.execute("INSERT INTO company (company_id, name, ticker, cik, sic_code) VALUES (?,?,?,?,?)",
                     (cid, registry_data.get("name", ticker) if registry_data else ticker,
                      ticker, registry_data.get("cik","") if registry_data else "", ""))
        did = new_id()
        conn.execute("""INSERT INTO source_document (document_id, source_type, source_name,
                        source_locator, fetched_at, company_id, run_id) VALUES (?,?,?,?,?,?,?)""",
                     (did, "FILING", "earnings", f"{ticker}/earnings", now_iso(), cid, run.run_id))
        fy = 2026
        period_id = new_id()
        conn.execute("""INSERT INTO reporting_period (period_id, company_id, period_type, fiscal_year, period_start, period_end)
                        VALUES (?,?,?,?,?,?)""", (period_id, cid, "annual", fy, f"{fy}-01-01", f"{fy}-12-31"))
        conn.commit()

    # Orientation
    orientation_evidence_ids = []
    try:
        from research.context.business_understanding import OrientationWorkflow
        with RunContext(conn, "orientation", {"ticker": ticker}) as run:
            ow = OrientationWorkflow(conn, cid, run.run_id)
            if brief.business_description:
                eid = ow.observe(did, "BUSINESS_DESCRIPTION", brief.business_description)
                orientation_evidence_ids.append(eid)
            if brief.economic_structure:
                ow.observe(did, "REVENUE_MODEL", brief.economic_structure)
            if brief.key_debate:
                ow.observe(did, "RECURRING_DEBATE", brief.key_debate)
            if financials.revenue_m:
                ow.observe(did, "KEY_METRIC", f"Revenue: ${financials.revenue_m:,.1f}M", numeric=financials.revenue_m, unit="USD_M")
            if financials.operating_margin_pct:
                ow.observe(did, "MARGIN_CADENCE", f"Operating margin: {financials.operating_margin_pct:.1f}%", numeric=financials.operating_margin_pct, unit="pct")
            ow.synthesize(status="complete")
            v(f"  Orientation: {len(orientation_evidence_ids)} evidence items")
            conn.commit()
    except Exception as e:
        v(f"  Orientation: {e}")

    # ── Step 7: Research Plan ──
    plan_id = None
    try:
        from research.planning.research_designer import ResearchDesigner
        with RunContext(conn, "planning", {"ticker": ticker}) as run:
            designer = ResearchDesigner(conn)
            plan_id = designer.create_plan(cid, brief.key_debate or f"{ticker} research",
                                            "EXPECTATION_GAP" if cons_eps else "QUALITY_GAP", run_id=run.run_id)
            for gap in (brief.evidence_gaps or [])[:3]:
                designer.add_question(plan_id, gap[:200])
            for d in brief.drivers:
                designer.add_driver(plan_id, d["name"], transmission=f"{d['name']} -> EPS")
            designer.add_workstream(plan_id, "KPI_FORECAST", justification="Forward drivers")
            designer.add_workstream(plan_id, "ADVERSARIAL_REVIEW", justification="Challenge thesis")
            for c in brief.contradictions:
                if c.get("severity") == "serious":
                    designer.add_kill_condition(plan_id, c.get("counter_evidence","")[:150])
            conn.commit()
    except Exception as e:
        v(f"  Plan: {e}")
        if not plan_id:
            plan_id = new_id()
            conn.execute("INSERT INTO research_plan (plan_id, company_id, plan_version, status, edge_type, edge_hypothesis) VALUES (?,?,1,'active','EXPECTATION_GAP',?)",
                         (plan_id, cid, f"{ticker} research"))
            conn.commit()

    # ── Step 8: Build estimate ──
    v(f"\n-- Estimate --")
    model, dd, all_confidences, schema_key, prior_year, constants = brief_to_model(brief, financials, registry_data)
    pre = model.compute_outputs()
    est_grade = grade_estimate(all_confidences)
    schema_grade = "PASS" if schema_key in DRIVER_REGISTRY else "WARN"
    v(f"  Pre-challenge: EPS ${pre['eps']:.2f}  Rev ${pre['revenue_m']:,.1f}M  [{est_grade}]")

    sens_table = dd.get_sensitivity_table(model)

    # Workpapers
    with RunContext(conn, "workpapers", {"ticker": ticker}) as run:
        wb = WorkpaperBuilder(conn, cid)
        wb.create(workpaper_type="DRIVER_DECOMPOSITION", title=f"{ticker} Driver Decomposition",
                  content={"drivers": dd.get_driver_table()}, question="How does each driver decompose?",
                  methodology="Structured components.", run_id=run.run_id)
        wb.create(workpaper_type="DRIVER_SENSITIVITY", title=f"{ticker} Sensitivity",
                  content={"sensitivities": sens_table}, question="Which component matters most to EPS?",
                  methodology="Auto-sized perturbation.", run_id=run.run_id)
        if is_api:
            wb.create(workpaper_type="RESEARCH_BRIEF", title=f"{ticker} Research Brief",
                      content={"business_description": brief.business_description, "economic_structure": brief.economic_structure,
                               "key_debate": brief.key_debate,
                               "edge_hypothesis": brief.edge_hypothesis, "edge_type": brief.edge_type,
                               "why_market_is_wrong": brief.why_market_is_wrong,
                               "consensus_assumptions": brief.consensus_assumptions,
                               "guidance_vs_our_view": brief.guidance_vs_our_view,
                               "schema_reasoning": brief.schema_reasoning,
                               "contradictions": brief.contradictions, "evidence_gaps": brief.evidence_gaps,
                               "confidence_notes": brief.confidence_notes},
                      question="Business context, edge hypothesis, and key debate?", methodology="Single rich Claude call with consensus context.", run_id=run.run_id)
        conn.commit()

    # ── Step 9: Estimate Building + Claims ──
    case_id = None
    thesis_id = new_id()
    try:
        from research.core_workflow import EstimateBuilder, ClaimBuilder
        with RunContext(conn, "estimate_build", {"ticker": ticker}) as run:
            eb = EstimateBuilder(conn, plan_id)
            case_id = eb.create_case(cid, "base", scenario_weight=1.0, summary=f"{ticker} base case", run_id=run.run_id)
            assumption_ids = {}
            for driver in dd.drivers.values():
                for comp in driver.components.values():
                    aid = eb.set_assumption(case_id, f"{driver.assumption_key}_{comp.name}",
                        comp.value, "INDEPENDENT", comp.basis[:200] if comp.basis else None, comp.confidence)
                    assumption_ids[f"{driver.driver_name}.{comp.name}"] = aid
            eb.set_output(case_id, period_id, "eps", pre["eps"],
                         vs_consensus=pre["eps"]-cons_eps if cons_eps else None)
            eb.set_output(case_id, period_id, "revenue_m", pre["revenue_m"])

            # Thesis + Claims
            direction = "long" if cons_eps and pre["eps"] > cons_eps else "short" if cons_eps else "neutral"
            conn.execute("""INSERT INTO thesis (thesis_id, company_id, plan_id, thesis_version, direction, conviction,
                            one_liner, edge_source, why_exists, key_risks, is_worth_sharing, created_by_run)
                            VALUES (?,?,?,1,?,?,?,?,?,?,0,?)""",
                         (thesis_id, cid, plan_id, direction, est_grade,
                          brief.key_debate[:200] if brief.key_debate else f"{ticker} estimate",
                          "EXPECTATION_GAP", brief.schema_reasoning[:200] if brief.schema_reasoning else "",
                          brief.confidence_notes[:200] if brief.confidence_notes else "", run.run_id))

            cb = ClaimBuilder(conn)
            for dname, driver in dd.drivers.items():
                comps_str = ", ".join(f"{c.name}={c.value}" for c in list(driver.components.values())[:3])
                falsifier = "No specific falsifier"
                for contra in brief.contradictions:
                    if contra.get("affected_driver","") == dname or contra.get("affected_driver","") == driver.assumption_key:
                        falsifier = contra.get("counter_evidence", falsifier)[:150]
                        break
                claim_id = cb.create_claim(cid, plan_id, thesis_id,
                    f"{dname}: {driver.formula} = {comps_str}"[:300], "ESTIMATE",
                    driver.assumption_key, driver.confidence, falsifier, run.run_id)
                for i, eid in enumerate(orientation_evidence_ids[:3]):
                    cb.link_evidence(claim_id, eid, "supports", rationale=f"Evidence for {dname}")
                for cname, comp in driver.components.items():
                    akey = f"{dname}.{cname}"
                    if akey in assumption_ids:
                        cb.link_to_assumption(claim_id, assumption_ids[akey], "positive",
                                               rationale=comp.basis[:100] if comp.basis else dname)
            conn.commit()
    except Exception as e:
        v(f"  Estimate/Claims: {e}")

    # ── Step 10: Adversarial ──
    v(f"\n-- Adversarial --")
    traces = []
    contradiction_coverage = {}

    # Phase A: ContradictionCapture (structured)
    try:
        from research.adversarial import ContradictionCapture, Contradiction, PostChallengeRevisionLoop, RevisionDecision
        with RunContext(conn, "adversarial", {"ticker": ticker}) as run:
            cc = ContradictionCapture(conn, plan_id, cid)
            for contra in brief.contradictions:
                cc.record(Contradiction(assumption_key=contra.get("affected_driver","general"),
                    contradiction=contra.get("counter_evidence","")[:300],
                    severity=contra.get("severity","moderate"), source="research_brief",
                    what_would_resolve=contra.get("thesis","")[:200]), run_id=run.run_id)
            for d in brief.drivers:
                for comp in d.get("components",[]):
                    if comp.get("confidence",0) >= 0.55 and comp.get("basis"):
                        cc.record_support(Contradiction(assumption_key=d.get("assumption_key",d["name"]),
                            contradiction=comp["basis"][:200], severity="moderate", source="driver_basis",
                            what_would_resolve="Continued evidence"), run_id=run.run_id)
            contradiction_coverage = cc.assess_coverage([d.get("assumption_key",d["name"]) for d in brief.drivers])
            cc.produce_contradiction_table(run_id=run.run_id)
            conn.commit()
    except Exception as e:
        v(f"  ContradictionCapture: {e}")

    # Phase B: Apply brief's bear revisions
    traces = apply_bear_revisions(brief.bear_revisions, dd, model, verbose)

    # Phase C: Structurally independent audit call (Model Separation Doctrine)
    # Information barrier: audit receives thesis conclusion + raw data only.
    # Does NOT receive brief.contradictions or brief.bear_revisions.
    financials_str = None
    if financials:
        try:
            financials_str = json.dumps({
                "revenue": financials.revenue, "eps": financials.eps,
                "margins": getattr(financials, "margins", None),
            }, default=str)[:2000]
        except Exception:
            pass
    # Pass the corpus LABELED by source so the auditor can distinguish
    # "not in the filing" from "not anywhere in our evidence" — prevents
    # false phantom-entity flags (e.g. Smart Kitchen IS in transcripts but
    # not in the 10-K). Fall back to the legacy blob if any piece is empty.
    adv_corpus = {
        "filing":         raw_filing_text,
        "transcripts":    transcripts_corpus_text,
        "press_releases": press_corpus_text,
        "deck_digest":    deck_corpus_text,
        "macro_context":  macro_corpus_text,    # FRED
        "bls_context":    bls_corpus_text,      # BLS labor + category CPI
        "bea_context":    bea_corpus_text,      # BEA PCE detail (when BEA_API_KEY set)
        "peer_comps":     peer_corpus_text,
    }

    # ── Evidence audit: grade each component's self-labeled
    # evidence_strength against the corpus. Findings downgrade mis-labeled
    # components IN PLACE (cited-without-citation → speculative, cited-but-
    # numbers-absent → inferred). "Speculative" keeps its seat at the table;
    # we don't drop hypotheses, just make their confidence level visible so
    # the Word renderer can style them differently.
    try:
        from research.evidence_audit import (
            audit_brief_evidence, apply_findings_to_brief, summarize_findings,
        )
        audit_findings = audit_brief_evidence(brief, adv_corpus)
        if audit_findings:
            n_applied = apply_findings_to_brief(brief, audit_findings)
            summary = summarize_findings(audit_findings)
            v(f"  {summary} ({n_applied} components downgraded in-place)")
            warnings.append(f"EVIDENCE AUDIT: {summary}")
            # Stash for the result dict so the Word renderer can surface
            # per-component audit notes if it wants to.
            brief._evidence_audit_findings = [
                {
                    "driver": f.driver,
                    "component": f.component,
                    "original_strength": f.original_strength,
                    "suggested_strength": f.suggested_strength,
                    "reason": f.reason,
                    "missing_numbers": f.missing_numbers,
                }
                for f in audit_findings
            ]
    except Exception as e:
        v(f"  Evidence audit: {type(e).__name__}: {e}")

    adv_response = call_adversarial_claude(
        brief, filing_text, verbose,
        financials_summary=financials_str,
        consensus_eps=consensus.get("eps") if isinstance(consensus, dict) else None,
        corpus=adv_corpus,
    )
    if adv_response:
        # Phantom-entity cross-check: when the adversarial flags a term as
        # "not mentioned in the filing", it may actually be in the public
        # record (e.g. WING Smart Kitchen is real; the 10-K just didn't
        # discuss it). Extract the flagged entity, hit DuckDuckGo, and
        # adjust severity based on whether the entity exists in the wild.
        try:
            from research.phantom_check import cross_check_adversarial_phantoms
            pc_results = cross_check_adversarial_phantoms(
                adv_response, ticker, max_checks=4, verbose=verbose,
            )
            if pc_results:
                verdicts = [r.verdict for r in pc_results]
                n_real = verdicts.count("real")
                n_fab = verdicts.count("fabricated")
                v(f"  Phantom cross-check: {len(pc_results)} entity flags "
                  f"checked ({n_real} real, {n_fab} fabricated)")
                # Stash on adv_response so renderer can surface verdicts
                adv_response["_phantom_check_results"] = [
                    {
                        "contradiction_index": r.contradiction_index,
                        "entity": r.entity,
                        "verdict": r.verdict,
                        "reason": r.reason,
                        "hits": r.hit_count,
                    }
                    for r in pc_results
                ]
        except Exception as e:
            v(f"  Phantom cross-check skipped: {type(e).__name__}: {e}")

        extra_traces = apply_bear_revisions(adv_response.get("additional_revisions",[]), dd, model, verbose)
        traces.extend(extra_traces)

        # Feed blind spots into contradictions (they inform edge confidence)
        for blind_spot in adv_response.get("blind_spots", []):
            brief.contradictions.append({
                "thesis": "Analyst blind spot",
                "counter_evidence": blind_spot,
                "severity": "moderate",
                "affected_driver": "general",
            })

    post = model.compute_outputs()
    v(f"  Post-challenge (mechanical schema-driver model): EPS ${post['eps']:.2f} "
      f"({len(traces)} revisions)")

    # ── EPS Bridge: consensus baseline + edge_claim flow-through ──
    # This is the AUTHORITATIVE EPS path. The mechanical schema-driver model
    # above is kept for sensitivity / Excel back-compat, but its output is
    # diagnostic only — the brief's edge_claims drive the EPS via clean
    # flow-through math anchored to consensus. This eliminates the
    # brief-vs-mechanical divergence that caused 30%+ run-to-run swings.
    eps_build = None
    try:
        from research.pnl_model import build_baseline, compute_our_eps
        baseline_pnl = build_baseline(financials, consensus_full_dict, guidance_bundle)
        eps_build = compute_our_eps(baseline_pnl, brief.edge_claims or [])
        if eps_build.baseline.is_valid():
            v(f"  EPS Bridge: baseline ${baseline_pnl.eps:.2f} "
              f"+ {len(eps_build.claim_impacts)} claim(s) (Σ {eps_build.sum_eps_impact:+.2f}) "
              f"= our EPS ${eps_build.our_eps:.2f}")
            for ci in eps_build.claim_impacts:
                v(f"    • {ci.claim_anchor_type} ({ci.claim_line_hit}): "
                  f"mechanical {ci.eps_impact:+.2f}"
                  + (f"  ⚠ Claude said {ci.claude_eps_impact:+.2f}"
                     if ci.impact_mismatch else ""))
            for w in eps_build.warnings:
                v(f"    [bridge] {w}")
            # Replace post_eps with the authoritative flow-through value.
            # Keep the mechanical model output stashed for Excel / sensitivity.
            mechanical_post_eps = post["eps"]
            post = {
                "eps": eps_build.our_eps,
                "revenue_m": eps_build.baseline.revenue / 1e6,
                "_mechanical_eps": mechanical_post_eps,
            }
        else:
            v(f"  EPS Bridge: baseline invalid (no consensus); falling back "
              f"to mechanical post EPS ${post['eps']:.2f}")
    except Exception as e:
        v(f"  EPS Bridge skipped: {type(e).__name__}: {e}")

    # Revision loop
    revision_summary = {}
    if case_id:
        try:
            pcrl = PostChallengeRevisionLoop(conn, plan_id, case_id)
            for rev_data in brief.bear_revisions[:5]:
                md = _fuzzy_match(rev_data.get("driver",""), set(dd.drivers.keys()))
                if md:
                    driver = dd.drivers[md]
                    mc = _fuzzy_match(rev_data.get("component",""), set(driver.components.keys()))
                    if mc:
                        pcrl.decide(RevisionDecision(assumption_key=driver.assumption_key,
                            prior_value=driver.components[mc].value, decision="revise_down",
                            new_value=rev_data["new_value"], reason=rev_data.get("reason",""), direction="bearish"))
            pcrl.apply_revisions()
            revision_summary = pcrl.get_summary()
            pcrl.produce_revision_log()
            conn.commit()
        except Exception: pass

    # ── Step 11: Market overlay + Edge detection ──
    v(f"\n-- Market Overlay --")
    setup_data = None
    try:
        from research.market_overlay import fetch_market_data, assess_setup
        md = fetch_market_data(ticker)
        sa = assess_setup(ticker, {"post_eps": post["eps"], "consensus_eps": cons_eps or 0}, md)
        setup_data = {
            "implied_move_pct": getattr(sa, "implied_move_pct", None) or 5.0,
            "short_interest_pct": getattr(sa, "short_interest_pct", None) or 0,
        }
        if sa.short_interest_pct:
            v(f"  Short interest: {sa.short_interest_pct:.1f}%")
        if sa.implied_move_pct:
            v(f"  Implied move: {sa.implied_move_pct:.1f}%")
        if sa.setup_note:
            v(f"  Setup: {sa.setup_note[:80]}")
    except Exception as e:
        v(f"  Market overlay: {e}")

    v(f"\n-- Edge Detection --")
    edge_assessment = None
    try:
        from research.edge_detector import detect_edge
        edge_assessment = detect_edge(model=model, dd=dd, sens_table=sens_table,
            post_outputs=post, consensus=consensus, brief=brief,
            setup_assessment=setup_data,
            consensus_data=consensus_data, verbose=verbose)
        v(f"  {edge_assessment.verdict} | score={edge_assessment.actionability_score:.3f}")
    except Exception as e:
        v(f"  Edge: {e}")

    # ── Step 12: Valuation ──
    v(f"\n-- Valuation --")
    valuation_dict = None
    try:
        from research.valuation import compute_valuation
        val = compute_valuation(post["eps"], ticker, cons_eps, verbose=verbose)
        if val: valuation_dict = val.to_dict()
    except Exception as e:
        v(f"  Valuation: {e}")

    # ── Step 13: Decision gate ──
    decision_verdict = "NOT_ASSESSED"
    try:
        from research.deeper_workflow import StrongerDecisionGate
        with RunContext(conn, "decision", {"ticker": ticker}) as run:
            gate = StrongerDecisionGate(conn)
            decision = gate.assess(thesis_id, plan_id, cid)
            decision_verdict = decision.verdict
            gate.record(thesis_id, decision, run.run_id)
            v(f"\n-- Decision: {decision_verdict} --")
            conn.commit()
    except Exception as e:
        v(f"  Decision gate: {e}")

    # ── Extreme-variant reasonability check ──
    # If our final EPS is radically away from consensus (>50%), the pipeline's
    # cumulative bear/bull bias may have compounded absurdly. Emit a prominent
    # warning — this flows into the Word report's warnings list and into the
    # decision narrative.
    if cons_eps and post.get("eps") is not None and cons_eps != 0:
        variant_pct = abs(post["eps"] - cons_eps) / abs(cons_eps) * 100
        if variant_pct > 50:
            n_revisions = len(bear_revisions_traces) if "bear_revisions_traces" in dir() else 0
            direction = "below" if post["eps"] < cons_eps else "above"
            warning = (
                f"EXTRAORDINARY VARIANT: our EPS ${post['eps']:.2f} is "
                f"{variant_pct:.0f}% {direction} consensus ${cons_eps:.2f}. "
                f"Claims this extreme require specific named catastrophic "
                f"(or windfall) mechanisms — review the brief + adversarial "
                f"output and confirm the thesis supports this magnitude, "
                f"or treat the number as mis-calibrated."
            )
            warnings.append(warning)
            v(f"\n  [REASONABILITY] {warning}")

    # ── Build result ──
    all_contradictions = brief.contradictions + (adv_response or {}).get("new_contradictions", [])
    ea_dict = edge_assessment.to_dict() if edge_assessment else None

    total_wp = conn.execute("SELECT COUNT(*) FROM workpaper WHERE company_id=?", (cid,)).fetchone()[0]
    wp_rows = conn.execute("SELECT workpaper_type, title, content FROM workpaper WHERE company_id=? ORDER BY created_at", (cid,)).fetchall()

    result = {
        "ticker": ticker, "name": registry_data.get("name", ticker) if registry_data else ticker,
        "schema": schema_key, "schema_grade": schema_grade,
        "extraction_grade": ext_grade, "estimate_grade": est_grade,
        "quality_line": quality_line(ext_grade, schema_grade, est_grade),
        "pre_eps": pre["eps"], "post_eps": post["eps"],
        "pre_revenue": pre["revenue_m"], "post_revenue": post["revenue_m"],
        "consensus_eps": cons_eps, "consensus_revenue": consensus.get("revenue_m"),
        # Full consensus snapshot: per-period estimates + revisions + PT + ratings
        "consensus_full": consensus_full_dict,
        "business_description": brief.business_description,
        "key_debate": brief.key_debate,
        "edge_hypothesis": brief.edge_hypothesis,
        "edge_type": brief.edge_type,
        "why_market_is_wrong": brief.why_market_is_wrong,
        # Structured edge claims — disagreements with specific published anchors
        "edge_claims": brief.edge_claims,
        "rejected_edge_claims": brief.rejected_edge_claims,
        # EPS Bridge: consensus baseline + flow-through impacts → our EPS.
        # Authoritative source of forward EPS. Replaces the mechanical
        # schema-driver model output for downstream rendering.
        "eps_build": (eps_build.to_dict() if eps_build is not None else None),
        # Guidance bundle (rendered for the Word doc's anchor reference table)
        "guidance_bundle": (guidance_bundle.to_dict() if guidance_bundle is not None else None),
        "consensus_assumptions": brief.consensus_assumptions,
        "guidance_vs_our_view": brief.guidance_vs_our_view,
        "contradictions": all_contradictions,
        "drivers": {dn: {"value": d.compute_value(),
                         "components": {c.name: {"value": c.value, "confidence": c.confidence} for c in d.components.values()}}
                    for dn, d in dd.drivers.items()},
        "sensitivities": sens_table[:5], "traces": traces,
        "edge_assessment": ea_dict, "valuation": valuation_dict,
        "decision_verdict": decision_verdict,
        "contradiction_coverage": contradiction_coverage,
        "revision_summary": revision_summary,
        "adversarial_response": adv_response,
        "brief_warnings": warnings,
        # Per-component audit findings (when the brief had any). Renderer uses
        # this to show "(auto-downgraded: reason)" notes on specific drivers.
        "evidence_audit_findings": getattr(brief, "_evidence_audit_findings", []),
        "convergence_adjustments": convergence_adjustments,
        "outlier_flags": outlier_flags,
        "transcript_analysis": {
            "tone": transcript_analysis.tone_trajectory if transcript_analysis else None,
            "credibility": transcript_analysis.management_credibility if transcript_analysis else None,
            "recurring_concerns": transcript_analysis.recurring_concerns[:5] if transcript_analysis else [],
            "inflection_points": transcript_analysis.key_inflection_points[:3] if transcript_analysis else [],
            "guidance_evolution": transcript_analysis.guidance_evolution[:5] if transcript_analysis else [],
        } if transcript_analysis else None,
        "financials_source": financials.source, "financials_fy": financials.fiscal_year,
        "workpapers": [{"type": wt, "title": tt, "content": json.loads(wc) if isinstance(wc, str) else wc}
                       for wt, tt, wc in wp_rows],
        "total_workpapers": total_wp,
        # Brief stashed under underscore key for Word rendering; stripped from JSON in _save_result
        "_brief": brief,
    }
    conn.close()
    _save_result(ticker, result)
    return result


def _save_result(ticker, result):
    try:
        Path("data/results").mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save = dict(result)
        save.pop("workpapers", None)
        save.pop("_brief", None)  # not JSON-serializable; Word renderer uses it
        with open(f"data/results/{ticker}_{ts}.json", "w") as f:
            json.dump(save, f, indent=2, default=str)
    except Exception: pass


# ---------------------------------------------------------------
# Output
# ---------------------------------------------------------------

def print_concise(r):
    print()
    print(f"  [{r.get('decision_verdict','?')}]")
    print(f"{r['ticker']} | {r['schema']} | {r['quality_line']}")
    print(f"{'-'*60}")

    if r.get("business_description"):
        print(f"\n{r['business_description'][:120]}")
    if r.get("key_debate"):
        print(f"Debate: {r['key_debate'][:120]}")
    if r.get("edge_hypothesis"):
        print(f"EDGE: {r['edge_hypothesis'][:140]}")
    if r.get("why_market_is_wrong"):
        print(f"Why wrong: {r['why_market_is_wrong'][:140]}")

    cons_eps = r.get("consensus_eps")
    print(f"\nEstimate: EPS ${r['post_eps']:.2f} (post-challenge)", end="")
    if cons_eps:
        diff = r["post_eps"] - cons_eps
        print(f" vs consensus ${cons_eps:.2f} (${abs(diff):.2f} {'above' if diff > 0 else 'below'})")
    else: print()

    print(f"\nDrivers:")
    for dname, dinfo in r.get("drivers",{}).items():
        comps = ", ".join(f"{cn} {cd['value']:+.1f}" for cn, cd in dinfo["components"].items())
        print(f"  {dname}: {dinfo['value']:+.1f} ({comps})")

    ea = r.get("edge_assessment")
    if ea:
        hz = ea.get("time_horizon","")
        print(f"\nEdge: {ea['verdict']} (score={ea['actionability_score']:.3f}){f' | {hz.upper()}' if hz else ''}")
        for var in ea.get("variants",[])[:2]:
            if abs(var["eps_contribution"]) > 0.001:
                print(f"  {var['driver']}.{var['component']}: ours {var['our_value']:+.1f} vs street {var['consensus_value']:+.1f} -> EPS ${var['eps_contribution']:+.4f}")

    val = r.get("valuation")
    if val and val.get("implied_price"):
        print(f"\nValuation: ${val['implied_price']:.2f} ({val['upside_pct']:+.1f}%) at {val['applied_multiple']:.1f}x PE")

    contras = r.get("contradictions",[])
    if contras:
        print(f"\nContradictions ({len(contras)}):")
        for c in contras[:2]:
            print(f"  [{c.get('severity','')}] {c.get('counter_evidence','')[:70]}")

    cc = r.get("contradiction_coverage",{})
    if cc:
        print(f"\nAdversarial: {len(cc.get('covered',[]))}/{len(cc.get('key_assumptions',[]))} assumptions challenged")

    print(f"\nWorkpapers ({r.get('total_workpapers',0)}):")
    for wp in r.get("workpapers",[]):
        print(f"  {wp['type'].lower():<28s} {wp['title']}")
    print()


def print_detail(r, workpaper_type):
    wtype = workpaper_type.upper()
    found = [wp for wp in r.get("workpapers",[]) if wp["type"] == wtype]
    if not found:
        available = ", ".join(wp["type"].lower() for wp in r.get("workpapers",[]))
        print(f"\n  No workpaper '{workpaper_type}'. Available: {available}")
        return
    wp = found[0]
    print(f"\n{'='*60}")
    print(f"WORKPAPER: {wp['title']}")
    print(f"{'='*60}")
    content = wp["content"]
    if wp["type"] == "DRIVER_DECOMPOSITION":
        for row in content.get("drivers",[]):
            print(f"  {row['driver']:<16s} {row['component']:<22s} {row['value']:>+8.1f} {row['unit']:>5s}  conf={row['confidence']:.0%}  {row.get('basis','')[:35]}")
    elif wp["type"] == "DRIVER_SENSITIVITY":
        for row in content.get("sensitivities",[]):
            print(f"  {row['driver']}.{row['component']:<18s} EPS ${row['eps_impact']:>+7.4f} per {row['perturbation_label']:>8s}  exp={row['exposure']:.4f}")
    elif wp["type"] == "RESEARCH_BRIEF":
        print(f"\n  Business: {content.get('business_description','?')[:120]}")
        print(f"  Debate: {content.get('key_debate','?')[:120]}")
        if content.get("edge_hypothesis"):
            print(f"\n  EDGE HYPOTHESIS: {content['edge_hypothesis'][:200]}")
            print(f"  Edge Type: {content.get('edge_type','?')}")
        if content.get("why_market_is_wrong"):
            print(f"  Why Market Is Wrong: {content['why_market_is_wrong'][:200]}")
        if content.get("consensus_assumptions"):
            print(f"\n  Consensus Assumptions (what street likely thinks):")
            for driver, view in content["consensus_assumptions"].items():
                print(f"    {driver}: {view[:80]}")
        if content.get("guidance_vs_our_view"):
            print(f"\n  Guidance vs Our View:")
            for driver, view in content["guidance_vs_our_view"].items():
                print(f"    {driver}: {view[:80]}")
        if content.get("contradictions"):
            print(f"\n  Contradictions:")
            for c in content["contradictions"]:
                print(f"    [{c.get('severity','')}] {c.get('thesis','')[:60]}")
                print(f"      vs: {c.get('counter_evidence','')[:60]}")
    elif wp["type"] == "CONTRADICTION_TABLE":
        for side in ["bear_cases","bull_cases"]:
            cases = content.get(side,[])
            if cases:
                print(f"\n  {'BEAR' if 'bear' in side else 'BULL'} ({len(cases)}):")
                for c in cases:
                    print(f"    [{c.get('severity','')}] {c.get('assumption','')}: {c.get('evidence','')[:60]}")
    else:
        print(json.dumps(content, indent=2, default=str)[:2000])
    print()
