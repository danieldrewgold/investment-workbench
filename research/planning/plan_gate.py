"""
Plan Gate (Layer 2 enforcement)

Makes the research plan a real operational constraint.
Default behavior: deny unless justified in the plan.

Product layer: Layer 2 (Research Design)
Why it reduces rework: prevents evidence gathering without a research
design, which is the primary cause of unfocused work.
"""

import sqlite3
from dataclasses import dataclass


# ── Workstream type → allowed operation mapping ──────────────
# Each workstream type authorizes specific downstream operations.
# If a workstream isn't in the active plan, the operation is blocked.

WORKSTREAM_AUTHORIZES = {
    # Core analytical workstreams (Layer 3)
    "KPI_FORECAST":         ["ingest_actuals", "ingest_consensus", "build_estimate"],
    "OPERATING_BUILD":      ["ingest_actuals", "ingest_guidance", "build_estimate"],
    "GUIDANCE_COMPARISON":  ["ingest_guidance", "ingest_consensus"],
    "COST_BRIDGE":          ["ingest_actuals", "build_estimate"],
    "CAPITAL_ALLOCATION":   ["ingest_insider", "ingest_capital_actions"],
    "VALUATION":            ["ingest_consensus", "build_estimate"],
    "COMP_ANALYSIS":        ["ingest_actuals", "ingest_consensus"],
    "ACCOUNTING_REVIEW":    ["ingest_filings"],

    # Overlay workstreams (not default, must be justified)
    "INSIDER_ACTIVITY":     ["ingest_insider"],
    "OPTIONS_FLOW":         ["ingest_options_flow"],
    "DARK_POOL":            ["ingest_dark_pool"],
    "SHORT_INTEREST":       ["ingest_short_interest"],
    "MARKET_STRUCTURE":     ["ingest_options_flow", "ingest_dark_pool"],
}

# Operations that are always allowed regardless of plan
ALWAYS_ALLOWED = {
    "ingest_company_details",  # need basic company info to start
    "ingest_earnings_dates",   # calendar is infrastructure
}

# Overlay workstreams that require explicit justification
OVERLAY_WORKSTREAMS = {
    "INSIDER_ACTIVITY", "OPTIONS_FLOW", "DARK_POOL",
    "SHORT_INTEREST", "MARKET_STRUCTURE",
}


@dataclass
class GateResult:
    """Result of a plan gate check."""
    allowed: bool
    reason: str
    workstream_id: str = None
    requires_amendment: bool = False


class PlanGate:
    """
    Enforces the research plan as a real constraint.

    Usage:
        gate = PlanGate(conn, plan_id)
        result = gate.check("ingest_insider")
        if not result.allowed:
            print(result.reason)  # "Not authorized: no workstream covers ingest_insider"
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str):
        self.conn = conn
        self.plan_id = plan_id
        self._workstreams = None
        self._plan = None

    def _load(self):
        """Lazy-load plan and workstream data."""
        if self._workstreams is not None:
            return

        old_factory = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        self._plan = self.conn.execute(
            "SELECT * FROM research_plan WHERE plan_id = ?", (self.plan_id,)
        ).fetchone()

        ws_rows = self.conn.execute(
            "SELECT * FROM workstream WHERE plan_id = ?", (self.plan_id,)
        ).fetchall()
        self._workstreams = [dict(w) for w in ws_rows]

        self.conn.row_factory = old_factory

    @property
    def plan_status(self) -> str:
        self._load()
        return dict(self._plan).get("status", "unknown") if self._plan else "not_found"

    def get_authorized_operations(self) -> set[str]:
        """Return all operations authorized by the current plan."""
        self._load()
        ops = set(ALWAYS_ALLOWED)
        for ws in self._workstreams:
            ws_name = ws.get("workstream_name", "")
            authorized = WORKSTREAM_AUTHORIZES.get(ws_name, [])
            ops.update(authorized)
        return ops

    def check(self, operation: str) -> GateResult:
        """
        Check if an operation is authorized by the active plan.

        Returns GateResult with allowed=True/False and reason.
        """
        self._load()

        if not self._plan:
            return GateResult(
                allowed=False,
                reason=f"No plan found (plan_id={self.plan_id})",
            )

        plan_status = dict(self._plan).get("status", "")
        if plan_status not in ("active", "in_progress"):
            return GateResult(
                allowed=False,
                reason=f"Plan status is '{plan_status}', must be 'active' to authorize work",
            )

        # Always-allowed operations
        if operation in ALWAYS_ALLOWED:
            return GateResult(allowed=True, reason="Always allowed (infrastructure)")

        # Check if any workstream authorizes this operation
        for ws in self._workstreams:
            ws_name = ws.get("workstream_name", "")
            authorized = WORKSTREAM_AUTHORIZES.get(ws_name, [])
            if operation in authorized:
                return GateResult(
                    allowed=True,
                    reason=f"Authorized by workstream '{ws_name}'",
                    workstream_id=ws.get("workstream_id"),
                )

        # Not authorized
        return GateResult(
            allowed=False,
            reason=f"Not authorized: no workstream in this plan covers '{operation}'. "
                   f"Active workstreams: {[w.get('workstream_name') for w in self._workstreams]}. "
                   f"Amend the plan to add a workstream that justifies this work.",
            requires_amendment=True,
        )

    def check_or_raise(self, operation: str) -> GateResult:
        """Check and raise if not allowed."""
        result = self.check(operation)
        if not result.allowed:
            raise PlanGateError(result.reason)
        return result

    def amend_plan(self, workstream_name: str, justification: str,
                   run_id: str = None) -> str:
        """
        Add a workstream to the plan (plan amendment).
        Returns the new workstream_id.

        This is the explicit mechanism for expanding scope.
        It must be called with a justification.
        """
        from core.provenance.database import new_id, upsert

        if not justification:
            raise PlanGateError("Cannot amend plan without justification")

        wid = new_id()
        upsert(self.conn, "workstream", {
            "workstream_id": wid,
            "plan_id": self.plan_id,
            "workstream_name": workstream_name,
            "justification": justification,
            "priority": "medium",
            "status": "planned",
        }, conflict_columns=["plan_id", "workstream_name"],
        update_columns=["justification", "priority", "status"])
        self.conn.commit()

        # Reset cache
        self._workstreams = None
        return wid


class PlanGateError(Exception):
    """Raised when an operation is blocked by the plan gate."""
    pass
