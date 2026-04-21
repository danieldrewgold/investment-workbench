"""
Output Contracts

Structured schemas for every research object the system produces.
These enforce that packaging can never outrun evidence.

Each contract defines:
  - required fields (must be present, non-empty)
  - optional fields (can be null)
  - validation rules

The point: the LLM cannot improvise the structure of important
research objects. If a field is required, the system must have
done enough upstream work to populate it.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Contract validation ──────────────────────────────────────

class ContractViolation(Exception):
    """Raised when an output doesn't meet its contract."""
    pass


def validate_contract(obj: dict, required_fields: list[str], name: str) -> list[str]:
    """
    Check that all required fields are present and non-empty.
    Returns list of violations (empty = valid).
    """
    violations = []
    for f in required_fields:
        val = obj.get(f)
        if val is None:
            violations.append(f"{name}.{f}: missing (required)")
        elif isinstance(val, str) and val.strip() == "":
            violations.append(f"{name}.{f}: empty string (required)")
        elif isinstance(val, list) and len(val) == 0:
            violations.append(f"{name}.{f}: empty list (required)")
    return violations


# ── 1. Universe Shortlist ────────────────────────────────────

UNIVERSE_SHORTLIST_REQUIRED = [
    "universe_name",
    "source_type",          # SECTOR, ANALYST, THEME, CUSTOM
    "total_names_screened",
    "shortlisted",          # list of ShortlistEntry
    "deprioritized_summary", # why others were cut
    "what_deeper_work_needed",
    "run_id",
]

@dataclass
class ShortlistEntry:
    """A single name on the shortlist."""
    ticker: str
    company_name: str
    role: str                           # PRIMARY, RUNNER_UP
    why_shortlisted: str                # specific, not generic
    likely_edge_source: str             # what kind of mispricing
    what_work_needed: str               # what research to do next
    market_cap: Optional[float] = None
    sector: Optional[str] = None

    def validate(self) -> list[str]:
        violations = []
        for f in ["ticker", "why_shortlisted", "likely_edge_source", "what_work_needed"]:
            if not getattr(self, f, None):
                violations.append(f"ShortlistEntry.{f}: missing")
        return violations


@dataclass
class UniverseShortlist:
    """Output of Layer 1: Universe Sourcing."""
    universe_name: str
    source_type: str
    total_names_screened: int
    shortlisted: list[ShortlistEntry]
    deprioritized_summary: str
    what_deeper_work_needed: str
    run_id: str
    peer_buckets: Optional[list[dict]] = None

    def validate(self) -> list[str]:
        v = validate_contract(self.__dict__, UNIVERSE_SHORTLIST_REQUIRED, "UniverseShortlist")
        if len(self.shortlisted) == 0:
            v.append("UniverseShortlist.shortlisted: must have at least 1 entry")
        if len(self.shortlisted) > 5:
            v.append("UniverseShortlist.shortlisted: >5 names defeats the purpose of triage")
        for entry in self.shortlisted:
            v.extend(entry.validate())
        return v


# ── 2. Research Plan ─────────────────────────────────────────

RESEARCH_PLAN_REQUIRED = [
    "ticker",
    "edge_hypothesis",       # one sentence
    "edge_type",
    "why_opportunity_exists",
    "what_makes_valuable",   # vs just interesting
    "key_questions",         # list, >= 2
    "key_drivers",           # list, >= 1
    "workstreams",           # list, >= 1
    "kill_conditions",       # list, >= 1
    "run_id",
]

@dataclass
class ResearchPlanOutput:
    """Output of Layer 2: Research Design."""
    ticker: str
    edge_hypothesis: str
    edge_type: str
    why_opportunity_exists: str
    what_makes_valuable: str
    key_questions: list[dict]       # [{question, priority}]
    key_drivers: list[dict]         # [{name, transmission, evidence_needed}]
    workstreams: list[dict]         # [{type, justification}]
    kill_conditions: list[dict]     # [{condition, metric, threshold}]
    run_id: str
    analytical_lenses: Optional[list[str]] = None  # which approaches fit this business

    def validate(self) -> list[str]:
        v = validate_contract(self.__dict__, RESEARCH_PLAN_REQUIRED, "ResearchPlan")
        if len(self.key_questions) < 2:
            v.append("ResearchPlan.key_questions: need at least 2")
        if len(self.key_drivers) < 1:
            v.append("ResearchPlan.key_drivers: need at least 1")
        if len(self.kill_conditions) < 1:
            v.append("ResearchPlan.kill_conditions: need at least 1")
        # Every driver must have a transmission mechanism
        for i, d in enumerate(self.key_drivers):
            if not d.get("transmission"):
                v.append(f"ResearchPlan.key_drivers[{i}]: missing transmission mechanism")
        # Every workstream must justify itself
        for i, w in enumerate(self.workstreams):
            if not w.get("justification"):
                v.append(f"ResearchPlan.workstreams[{i}]: missing justification")
        return v


# ── 3. Estimate Summary ─────────────────────────────────────

ESTIMATE_SUMMARY_REQUIRED = [
    "ticker",
    "cases",                # list of CaseSummary, >= 1
    "key_drivers",          # what drives the estimate
    "where_we_differ",      # vs consensus
    "biggest_uncertainty",
    "run_id",
]

@dataclass
class CaseSummary:
    """Summary of one estimate scenario."""
    label: str                          # BASE, BULL, BEAR
    probability: float
    key_assumptions: list[dict]         # [{metric, value, rationale}]
    outputs: dict                       # {EPS: x, REVENUE: y, ...}
    vs_consensus: dict                  # {EPS: +x, REVENUE: +y, ...}

    def validate(self) -> list[str]:
        v = []
        if not self.label:
            v.append("CaseSummary.label: missing")
        if not self.key_assumptions:
            v.append("CaseSummary.key_assumptions: empty")
        if not self.outputs:
            v.append("CaseSummary.outputs: empty")
        return v


@dataclass
class EstimateSummary:
    """Output of Layer 3: Evidence & Estimate Building."""
    ticker: str
    cases: list[CaseSummary]
    key_drivers: list[str]
    where_we_differ: str
    biggest_uncertainty: str
    run_id: str
    probability_weighted_eps: Optional[float] = None
    probability_weighted_target: Optional[float] = None

    def validate(self) -> list[str]:
        v = validate_contract(self.__dict__, ESTIMATE_SUMMARY_REQUIRED, "EstimateSummary")
        total_prob = sum(c.probability for c in self.cases)
        if abs(total_prob - 1.0) > 0.05:
            v.append(f"EstimateSummary: case probabilities sum to {total_prob:.2f}, not ~1.0")
        for c in self.cases:
            v.extend(c.validate())
        return v


# ── 4. Thesis Object ────────────────────────────────────────

THESIS_REQUIRED = [
    "ticker",
    "direction",
    "edge_hypothesis",
    "edge_type",
    "key_claims",           # list, each with evidence trace
    "key_risks",
    "bear_case",
    "what_would_falsify",
    "confidence",
    "estimate_summary",     # nested EstimateSummary or reference
    "run_id",
]

@dataclass
class ThesisOutput:
    """Output of Layer 4: Decision & Edge Assessment."""
    ticker: str
    direction: str                      # LONG, SHORT
    edge_hypothesis: str
    edge_type: str
    key_claims: list[dict]              # [{claim, evidence, confidence, affects}]
    key_risks: list[str]
    bear_case: str
    what_would_falsify: str
    confidence: str                     # HIGH, MEDIUM, LOW
    estimate_summary: dict              # or EstimateSummary reference
    is_novel: bool
    is_decision_useful: bool
    is_worth_sharing: bool
    overall_verdict: str                # SHARE, DEEPEN, MONITOR, KILL
    run_id: str
    missing_evidence: Optional[str] = None
    catalyst: Optional[str] = None
    time_horizon: Optional[str] = None

    def validate(self) -> list[str]:
        v = validate_contract(self.__dict__, THESIS_REQUIRED, "Thesis")
        if len(self.key_claims) == 0:
            v.append("Thesis.key_claims: empty")
        # Every claim must have evidence
        for i, c in enumerate(self.key_claims):
            if not c.get("evidence"):
                v.append(f"Thesis.key_claims[{i}]: missing evidence trace")
            if not c.get("affects"):
                v.append(f"Thesis.key_claims[{i}]: missing 'affects' (what does this change?)")
        if not self.bear_case or len(self.bear_case) < 20:
            v.append("Thesis.bear_case: too short to be a real bear case")
        return v


# ── 5. Pitch Package ────────────────────────────────────────

PITCH_REQUIRED = [
    "ticker",
    "direction",
    "one_liner",            # the pitch in one sentence
    "source_of_edge",
    "why_now",
    "key_driver_summary",
    "scenario_summary",     # base/bull/bear with numbers
    "valuation_framing",
    "key_risks",
    "what_would_change_mind",
    "evidence_quality",     # honest assessment
    "thesis_id",
    "run_id",
]

@dataclass
class PitchPackage:
    """Output of Layer 5: Packaging. Only produced after Layer 4 passes."""
    ticker: str
    direction: str
    one_liner: str
    source_of_edge: str
    why_now: str
    key_driver_summary: str
    scenario_summary: dict              # {base: {eps, target, prob}, bull: ..., bear: ...}
    valuation_framing: str
    key_risks: list[str]
    what_would_change_mind: str
    evidence_quality: str               # honest: "strong", "moderate", "thin"
    thesis_id: str
    run_id: str
    # Optional enrichments
    catalyst_calendar: Optional[list[dict]] = None
    comp_table: Optional[list[dict]] = None
    management_quality: Optional[str] = None

    def validate(self) -> list[str]:
        v = validate_contract(self.__dict__, PITCH_REQUIRED, "PitchPackage")
        if self.evidence_quality not in ("strong", "moderate", "thin"):
            v.append(f"PitchPackage.evidence_quality: must be strong/moderate/thin, got '{self.evidence_quality}'")
        if len(self.key_risks) < 2:
            v.append("PitchPackage.key_risks: need at least 2 risks")
        return v


# ── Validation runner ────────────────────────────────────────

def validate_output(obj) -> list[str]:
    """Validate any output contract object. Returns list of violations."""
    if hasattr(obj, "validate"):
        return obj.validate()
    return [f"Object {type(obj).__name__} has no validate() method"]
