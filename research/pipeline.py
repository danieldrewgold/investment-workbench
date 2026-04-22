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
                            financials_summary=None, consensus_eps=None):
    """
    Structurally independent adversarial audit call.

    INFORMATION BARRIER enforced:
      ✓ Receives: business description, key debate, schema, driver names + values
      ✓ Receives: raw filing text, structured financials, consensus estimates
      ✗ Does NOT receive: brief.contradictions (thesis model's self-generated bear cases)
      ✗ Does NOT receive: brief.bear_revisions (thesis model's self-corrections)
      ✗ Does NOT receive: confidence scores, evidence_gaps, reasoning chain

    The auditor must independently discover what's wrong with the thesis.
    """
    if not filing_text or len(filing_text) < 200:
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
RAW FILING TEXT (for independent verification):
{filing_text[:3000]}
{schema_audit_block}
INSTRUCTIONS:
1. What contradictions exist in the filing text that the analyst may not have considered?
2. Which assumptions look weakest when checked against the raw data?
3. What blind spots does this thesis have — things the analyst isn't even thinking about?
4. What's the strongest short-form bear case against this pitch?
5. If a SCHEMA-SPECIFIC AUDIT block was provided above, run through each numbered question and flag missing answers as contradictions (severity = "serious" if 2+ unanswered).
6. Produce 3-4 STRUCTURAL CRITIQUES that attack the pitch at its weakest structural points. Each must tag the section(s) it belongs in so a research note renderer can place it inline. Valid section tags: "edge", "drivers", "consensus", "valuation", "catalysts", "risks". Do NOT produce filler critiques -- better 3 sharp ones than 8 generic ones.

Respond in JSON:
{{"new_contradictions": [{{"thesis": "what the analyst claims", "counter_evidence": "what the data actually shows", "severity": "serious|moderate|minor", "affected_driver": "driver_name"}}],
"additional_revisions": [{{"driver": "...", "component": "...", "new_value": 0.0, "reason": "..."}}],
"blind_spots": ["things the analyst isn't considering at all"],
"structural_critiques": [{{"target_sections": ["edge|drivers|consensus|valuation|catalysts|risks"], "claim_under_attack": "short paraphrase of what the analyst asserts in that section", "counter_argument": "the steel-manned opposing view", "severity": "serious|moderate|minor"}}],
"overlap_with_obvious": ["if any of your findings are things the analyst probably already knows, flag them here"],
"overall_assessment": "one sentence verdict on whether this pitch holds up"}}"""

    try:
        resp = httpx.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": AUDIT_MODEL, "max_tokens": 2500,
                  "system": system_prompt,
                  "messages": [{"role": "user", "content": prompt}],
                  "metadata": {"user_id": "audit_model"}},
            timeout=60.0)
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

def apply_bear_revisions(revisions, dd, model, verbose=False):
    """Apply bear revisions with fuzzy matching + sanity checks."""
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
        try:
            trace = dd.revise_component(matched_d, matched_c, new_val, model, reason=rev.get("reason",""))
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

    # ── Step 1: Fetch financials ──
    v(f"\n-- Financials --")
    financials = fetch_financials(ticker, registry_data=registry_data, verbose=verbose)
    if not financials.revenue_m:
        raise ValueError(f"No financial data for {ticker}")
    v(f"  Revenue: ${financials.revenue_m:,.1f}M  EPS: ${financials.diluted_eps:.2f}")

    # ── Step 2: Fetch filing text ──
    v(f"\n-- Filing Text --")
    filing_text = registry_data.get("earnings_text", "") if registry_data else ""
    try:
        from research.edgar_text_fetcher import fetch_best_filing_text
        text, ftype = fetch_best_filing_text(ticker)
        if text:
            filing_text = (filing_text + "\n\n" + text).strip() if filing_text else text
            v(f"  {ftype}: {len(text)} chars")
    except Exception:
        pass
    if not filing_text:
        filing_text = f"{ticker} fiscal year results. Revenue ${financials.revenue_m:,.1f}M. EPS ${financials.diluted_eps:.2f}."

    # ── Step 2b: Fetch earnings call transcripts (3 years) ──
    v(f"\n-- Transcripts --")
    try:
        from research.transcript_fetcher import fetch_transcript_history
        transcript_text = fetch_transcript_history(ticker, quarters=12, verbose=verbose)
        if transcript_text:
            filing_text = filing_text + "\n\nEARNINGS CALL TRANSCRIPTS (3 YEARS):\n" + transcript_text
            v(f"  Total transcript context: {len(transcript_text):,} chars")
        else:
            v(f"  No transcripts available")
    except Exception as e:
        v(f"  Transcript: {e}")

    # ── Step 3: Fetch consensus ──
    v(f"\n-- Consensus --")
    consensus = registry_data.get("consensus", {}) if registry_data else {}
    consensus_data = {}
    if not consensus.get("eps"):
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info or {}
            fwd = info.get("forwardEps") or info.get("epsCurrentYear")
            if fwd:
                consensus = {"eps": fwd, "revenue_m": round(info.get("totalRevenue",0)/1e6,1) if info.get("totalRevenue") else None}
                consensus_data = {"eps": fwd, "current_price": info.get("currentPrice"),
                                   "forward_pe": info.get("forwardPE"), "analyst_count": info.get("numberOfAnalystOpinions",0)}
                try:
                    cal = yf.Ticker(ticker).calendar
                    if cal and "Earnings Date" in cal and cal["Earnings Date"]:
                        consensus_data["earnings_date"] = str(cal["Earnings Date"][0])
                except: pass
                v(f"  EPS: ${fwd:.2f} ({consensus_data.get('analyst_count','?')} analysts)")
        except: pass
    cons_eps = consensus.get("eps")

    # ── Step 3b: Transcript Analysis (pre-digest for research brain) ──
    transcript_analysis = None
    try:
        from research.transcript_analyzer import analyze_transcripts
        # Only run if we have transcript text in filing_text
        if "EARNINGS CALL TRANSCRIPT" in filing_text:
            v(f"\n-- Transcript Analysis --")
            transcript_analysis = analyze_transcripts(ticker, filing_text, verbose=verbose)
            if transcript_analysis:
                # Inject structured insights into filing_text for the brief
                analysis_text = transcript_analysis.to_prompt_text()
                filing_text = filing_text + "\n\n" + analysis_text
                v(f"  Injected {len(analysis_text):,} chars of structured insights")
    except Exception as e:
        if verbose:
            v(f"  Transcript analysis: {e}")

    # ── Step 4: Build research brief ──
    v(f"\n-- Research Brief --")
    brief = build_research_brief(ticker=ticker, financials=financials,
                                  earnings_text=filing_text,
                                  consensus_eps=cons_eps,
                                  consensus_revenue_m=consensus.get("revenue_m"),
                                  verbose=verbose)
    is_api = brief.source_method == "claude_api"
    driver_count = sum(len(d.get("components",[])) for d in brief.drivers)
    ext_grade = "A" if is_api and driver_count >= 4 else "B" if is_api and driver_count >= 3 else "C" if driver_count >= 2 else "F"

    # ── Step 5: Validate brief ──
    warnings = validate_brief(brief, financials)
    if warnings: v(f"  Warnings: {'; '.join(warnings)}")

    # ── Step 5b: Multi-run convergence (anchor with prior results) ──
    convergence_adjustments = []
    outlier_flags = []
    try:
        from research.convergence import load_convergence, anchor_brief_with_priors, flag_outliers
        convergence = load_convergence(ticker)
        if convergence.num_runs >= 3:
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
            v(f"\n-- Convergence: {convergence.num_runs} prior runs (need 3+) --")
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
    adv_response = call_adversarial_claude(
        brief, filing_text, verbose,
        financials_summary=financials_str,
        consensus_eps=consensus.get("eps") if isinstance(consensus, dict) else None,
    )
    if adv_response:
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
    v(f"  Post-challenge: EPS ${post['eps']:.2f} ({len(traces)} revisions)")

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
        "business_description": brief.business_description,
        "key_debate": brief.key_debate,
        "edge_hypothesis": brief.edge_hypothesis,
        "edge_type": brief.edge_type,
        "why_market_is_wrong": brief.why_market_is_wrong,
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
