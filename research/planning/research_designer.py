"""
Research Design Pipeline (Layer 2)

This is the layer that decides what work is worth doing BEFORE
deep data collection begins.

For each shortlisted company, it produces:
  - edge hypothesis (what is mispriced and why)
  - key questions to answer
  - key drivers and their transmission to value
  - workstreams (analytical tasks justified by the research plan)
  - kill conditions (what would stop the work early)

The pipeline can operate in two modes:
  1. Manual: analyst provides the plan structure, system stores it
  2. Assisted: system proposes a plan using Claude, analyst reviews

Both modes produce the same canonical objects in the database.
The point is that every downstream evidence-gathering task has a
justification traceable to a research plan.
"""

import json
from core.provenance.database import new_id, upsert, RunContext


# ── Analytical lens menu ─────────────────────────────────────
# These are the possible approaches. The research plan selects
# which ones are appropriate for the specific business.

ANALYTICAL_LENSES = {
    "price_volume_mix": "Decompose revenue into price, volume, and mix components",
    "units_x_asp": "Unit economics: units sold × average selling price",
    "cohort_retention": "Cohort-based retention, churn, and LTV analysis",
    "margin_bridge": "Walk margins from period to period identifying each driver",
    "fixed_variable_cost": "Separate fixed vs variable costs to model operating leverage",
    "utilization_capacity": "Capacity utilization, throughput, and bottleneck analysis",
    "spread_analysis": "Net interest margin, credit spread, or take-rate decomposition",
    "balance_sheet": "Leverage, reserve adequacy, asset quality review",
    "segment_decomposition": "Segment-level revenue and margin build",
    "geography_decomposition": "Geographic revenue and margin build",
    "working_capital": "Cash conversion cycle, inventory turns, receivables aging",
    "capital_allocation": "Buyback timing, dilution, dividend, M&A track record",
    "sum_of_parts": "SOTP valuation with segment-specific multiples",
    "historical_analog": "Find historical parallels to the current setup",
    "regression": "Simple regression of KPI against driver variables",
    "scenario_tree": "Explicit probability-weighted scenario analysis",
}

# ── Edge type taxonomy ───────────────────────────────────────

EDGE_TYPES = {
    "EXPECTATION_GAP": "Market estimates are too low or too high on a specific metric",
    "VALUATION_GAP": "Stock is mispriced relative to fundamentals on a recognized basis",
    "QUALITY_GAP": "Market underestimates durability or quality of the business",
    "INDUSTRY_STRUCTURE": "Industry dynamics are shifting in a way the market hasn't priced",
    "DURATION_GAP": "Market is pricing a short-duration outcome when the reality is longer",
    "SETUP_GAP": "Technical, positioning, or catalyst setup creates asymmetry",
    "COMPLEXITY_GAP": "Business complexity obscures a simpler underlying value",
    "BEHAVIORAL_GAP": "Market participants are anchored, fearful, or otherwise biased",
}


class ResearchDesigner:
    """
    Creates and manages research plans.

    Usage:
        designer = ResearchDesigner(conn)
        plan_id = designer.create_plan(
            company_id="...",
            edge_hypothesis="SSS acceleration not in consensus",
            edge_type="EXPECTATION_GAP",
            ...
        )
        designer.add_question(plan_id, "What is throughput contribution?", "HIGH")
        designer.add_driver(plan_id, "SSS growth", "SSS -> revenue -> leverage -> EPS")
        designer.add_workstream(plan_id, "KPI_FORECAST", "SSS is the key driver")
        designer.add_kill_condition(plan_id, "Q2 SSS below 3%")
    """

    def __init__(self, conn):
        self.conn = conn

    def create_plan(
        self,
        company_id: str,
        edge_hypothesis: str,
        edge_type: str,
        why_now: str = None,
        run_id: str = None,
        universe_id: str = None,
    ) -> str:
        """
        Create a new research plan. Returns plan_id.

        Every plan gets a version number. Multiple plans for the same
        company are allowed (the version increments).
        """
        # Get next version
        row = self.conn.execute(
            "SELECT MAX(plan_version) FROM research_plan WHERE company_id = ?",
            (company_id,)
        ).fetchone()
        next_version = (row[0] or 0) + 1

        plan_id = new_id()
        self.conn.execute(
            """INSERT INTO research_plan
               (plan_id, company_id, universe_id, plan_version, status,
                edge_type, edge_hypothesis, why_now, created_by_run)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (plan_id, company_id, universe_id, next_version,
             edge_type, edge_hypothesis, why_now, run_id),
        )
        self.conn.commit()
        return plan_id

    def add_question(
        self, plan_id: str, question: str, priority: str = "high",
    ) -> str:
        qid = new_id()
        self.conn.execute(
            """INSERT INTO research_question
               (question_id, plan_id, question_text, priority)
               VALUES (?, ?, ?, ?)""",
            (qid, plan_id, question, priority),
        )
        self.conn.commit()
        return qid

    def add_driver(
        self, plan_id: str, driver_name: str,
        transmission: str = None,
        importance: str = "high",
        current_consensus: str = None,
        independent_view: str = None,
    ) -> str:
        did = new_id()
        upsert(self.conn, "key_driver", {
            "driver_id": did,
            "plan_id": plan_id,
            "driver_name": driver_name,
            "importance": importance,
            "transmission": transmission,
            "current_consensus": current_consensus,
            "independent_view": independent_view,
        }, conflict_columns=["plan_id", "driver_name"],
        update_columns=["importance", "transmission", "current_consensus", "independent_view"])
        self.conn.commit()
        return did

    def add_workstream(
        self, plan_id: str, workstream_name: str,
        justification: str = None,
        priority: str = "high",
    ) -> str:
        wid = new_id()
        upsert(self.conn, "workstream", {
            "workstream_id": wid,
            "plan_id": plan_id,
            "workstream_name": workstream_name,
            "justification": justification,
            "priority": priority,
        }, conflict_columns=["plan_id", "workstream_name"],
        update_columns=["justification", "priority"])
        self.conn.commit()
        return wid

    def add_kill_condition(self, plan_id: str, condition_text: str) -> str:
        kid = new_id()
        self.conn.execute(
            "INSERT INTO kill_condition (kill_id, plan_id, condition_text) VALUES (?, ?, ?)",
            (kid, plan_id, condition_text),
        )
        self.conn.commit()
        return kid

    def get_plan_summary(self, plan_id: str) -> dict | None:
        """Retrieve a complete research plan with all components."""
        import sqlite3

        # Temporarily set row_factory for dict access
        old_factory = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        plan = self.conn.execute(
            "SELECT * FROM research_plan WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if not plan:
            self.conn.row_factory = old_factory
            return None

        plan_dict = dict(plan)

        questions = [dict(q) for q in self.conn.execute(
            "SELECT * FROM research_question WHERE plan_id = ? ORDER BY priority",
            (plan_id,)
        ).fetchall()]

        drivers = [dict(d) for d in self.conn.execute(
            "SELECT * FROM key_driver WHERE plan_id = ?", (plan_id,)
        ).fetchall()]

        workstreams = [dict(w) for w in self.conn.execute(
            "SELECT * FROM workstream WHERE plan_id = ? ORDER BY priority",
            (plan_id,)
        ).fetchall()]

        kills = [dict(k) for k in self.conn.execute(
            "SELECT * FROM kill_condition WHERE plan_id = ?", (plan_id,)
        ).fetchall()]

        self.conn.row_factory = old_factory

        return {
            "plan": plan_dict,
            "questions": questions,
            "drivers": drivers,
            "workstreams": workstreams,
            "kill_conditions": kills,
        }

    def validate_plan_completeness(self, plan_id: str) -> list[str]:
        """
        Check that a research plan meets minimum requirements
        before allowing downstream work to proceed.

        Returns list of issues (empty = plan is ready).
        """
        summary = self.get_plan_summary(plan_id)
        if not summary:
            return ["Plan not found"]

        issues = []
        plan = summary["plan"]

        if not plan.get("edge_hypothesis"):
            issues.append("Missing edge hypothesis")
        if not plan.get("edge_type"):
            issues.append("Missing edge type")
        if len(summary["questions"]) < 2:
            issues.append(f"Only {len(summary['questions'])} questions (need >= 2)")
        if len(summary["drivers"]) < 1:
            issues.append(f"No key drivers defined")
        if len(summary["workstreams"]) < 1:
            issues.append(f"No workstreams defined")
        if len(summary["kill_conditions"]) < 1:
            issues.append(f"No kill conditions defined")

        # Check that drivers have transmission mechanisms
        for d in summary["drivers"]:
            if not d.get("transmission"):
                issues.append(f"Driver '{d.get('driver_name')}' missing transmission mechanism")

        # Check that workstreams have justifications
        for w in summary["workstreams"]:
            if not w.get("justification"):
                issues.append(f"Workstream '{w.get('workstream_name')}' missing justification")

        return issues

    def suggest_analytical_lenses(self, business_type: str = None) -> list[str]:
        """
        Return a menu of analytical approaches appropriate for the business.
        This is a menu, not a checklist — not everything applies to every name.
        """
        # Default suggestion based on business type
        if business_type:
            bt = business_type.lower()
            if any(x in bt for x in ["restaurant", "retail", "store"]):
                return ["price_volume_mix", "margin_bridge", "units_x_asp",
                        "capital_allocation", "historical_analog"]
            elif any(x in bt for x in ["saas", "software", "subscription"]):
                return ["cohort_retention", "units_x_asp", "margin_bridge",
                        "segment_decomposition", "scenario_tree"]
            elif any(x in bt for x in ["bank", "insurance", "financial"]):
                return ["spread_analysis", "balance_sheet", "working_capital",
                        "capital_allocation", "regression"]
            elif any(x in bt for x in ["industrial", "manufacturing"]):
                return ["utilization_capacity", "fixed_variable_cost", "margin_bridge",
                        "geography_decomposition", "capital_allocation"]

        # Generic default
        return ["margin_bridge", "segment_decomposition", "capital_allocation",
                "scenario_tree", "historical_analog"]


def build_research_plan_from_dict(conn, company_id: str, plan_data: dict, run_id: str = None) -> str:
    """
    Convenience: create a full research plan from a dictionary.
    Matches the structure of the GOLDEN_RESEARCH_PLAN_CMG fixture.

    Returns plan_id.
    """
    designer = ResearchDesigner(conn)

    plan_id = designer.create_plan(
        company_id=company_id,
        edge_hypothesis=plan_data["edge_hypothesis"],
        edge_type=plan_data.get("edge_type", ""),
        why_now=plan_data.get("why_opportunity_exists"),
        run_id=run_id,
    )

    for q in plan_data.get("key_questions", []):
        if isinstance(q, dict):
            designer.add_question(plan_id, q["question"], q.get("priority", "high"))
        else:
            designer.add_question(plan_id, q)

    for d in plan_data.get("key_drivers", []):
        if isinstance(d, dict):
            designer.add_driver(
                plan_id, d["name"],
                transmission=d.get("transmission"),
                current_consensus=d.get("current_consensus"),
                independent_view=d.get("independent_view"),
            )
        else:
            designer.add_driver(plan_id, d)

    for w in plan_data.get("workstreams", []):
        if isinstance(w, dict):
            designer.add_workstream(
                plan_id, w.get("type", w.get("workstream_name", "")),
                justification=w.get("justification"),
            )
        else:
            designer.add_workstream(plan_id, w)

    for k in plan_data.get("kill_conditions", []):
        if isinstance(k, dict):
            designer.add_kill_condition(plan_id, k.get("condition", k.get("condition_text", "")))
        else:
            designer.add_kill_condition(plan_id, k)

    return plan_id
