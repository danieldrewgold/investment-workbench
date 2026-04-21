"""
Analytical Escalation Framework

Product layer: Research Design (Layer 2) + Evidence Building (Layer 3)

Design principle: lightweight by default, selectively escalate when
a thesis or research question justifies heavier analytical work.

NOT a binary "light vs deep" switch. Instead:
  - default lightweight research
  - targeted analytical escalations (one or two heavy subroutines)
  - only occasionally broader deeper work

Every escalation must be:
  1. question-driven (what are we answering?)
  2. justified (why does this improve the estimate/conviction/decision?)
  3. artifact-producing (what inspectable workpaper does it create?)
  4. tied back (how does the result affect estimates/claims/decisions?)

Priority 1: Escalation framework (propose, approve, execute)
Priority 2: Upstream layers stay light, allow narrow assists
Priority 3: Question-driven, artifact-driven escalations
Priority 4: Workpaper layer — inspectable analyst outputs
Priority 5: Fetch-vs-use-vs-ask data resolution
Priority 6: Tie-back to estimate and decision layers
"""

import json
import sqlite3
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert, now_iso


# ═══════════════════════════════════════════════════════════════
# PRIORITY 1: Escalation types and framework
# ═══════════════════════════════════════════════════════════════

ESCALATION_TYPES = {
    "EXTERNAL_DATA":        "Fetch additional external data (scanner, foot traffic, web scrape)",
    "FISCAL_ALIGNMENT":     "Align external data cadence to company fiscal periods",
    "KPI_MAPPING":          "Map reported KPIs to model structure with history",
    "BRIDGE_ANALYSIS":      "Build a margin or revenue bridge across periods",
    "REGRESSION":           "Run regression of a KPI against driver variables",
    "COST_INPUT":           "Track input costs / commodity prices relevant to margins",
    "CAPITAL_ALLOCATION":   "Analyze buyback, dividend, M&A patterns in detail",
    "SEGMENT_BUILD":        "Build segment-level revenue/margin history",
    "GUIDANCE_HISTORY":     "Build multi-period guidance track record table",
    "CADENCE_TABLE":        "Build a simple historical cadence table for orientation",
    "BASELINE_FORECAST":    "Build a trend-based baseline forecast as sanity check against guidance/consensus/thesis",
}

# Escalations that are appropriate during upstream (light) phases
LIGHT_ESCALATIONS = {
    "CADENCE_TABLE",        # simple historical table for orientation
    "GUIDANCE_HISTORY",     # guidance tracking for context
    "SEGMENT_BUILD",        # basic segment history for understanding
}

WORKPAPER_TYPES = {
    "TIME_SERIES_TABLE":       "Aligned time-series with y/y, sequential changes",
    "BRIDGE_TABLE":            "Period-over-period bridge (revenue or margin)",
    "REGRESSION_SUMMARY":      "Regression input, output, R², and interpretation",
    "KPI_DRIVER_TABLE":        "KPI and driver mapping with history",
    "ASSUMPTION_CHANGE_TABLE": "Assumption revision history with reasons",
    "FISCAL_ALIGNMENT_TABLE":  "External data aligned to fiscal periods",
    "CADENCE_TABLE":           "Simple historical cadence table",
    "BASELINE_FORECAST":       "Historical trend baseline forecast vs guidance/consensus/thesis",
    "BASELINE_COMPARISON":     "Comparison of baseline forecast against targets",
    "CONTRADICTION_TABLE":     "Bear case / contradiction evidence for each key assumption",
    "POST_CHALLENGE_REVISION": "Post-challenge assumption review with keep/revise/lower decisions",
    "EXPOSURE_SUMMARY":        "Where the thesis is most exposed to being wrong",
    "GUIDANCE_TRACK_RECORD":   "Management guidance vs actual track record",
    "MODEL_SPEC":              "Estimate model specification (driver→output relationships)",
    "DRIVER_DECOMPOSITION":    "Driver breakdown into sub-components with confidence and basis",
    "DRIVER_SENSITIVITY":      "Driver component sensitivity to EPS with exposure ranking",
    "SCHEMA_SELECTION":        "Economic structure / model schema selection with evidence and fit assessment",
    "SETUP_ASSESSMENT":        "Market-structure overlay — positioning, short interest, volatility context",
}


@dataclass
class EscalationProposal:
    """A proposed analytical escalation."""
    escalation_type: str
    question: str           # what research question this answers
    rationale: str          # why this is justified
    data_needed: str        # what data is required
    transformation: str     # what alignment/transformation is needed
    affects: str            # what estimate/claim/decision this serves
    priority: str = "medium"

    def validate(self) -> list[str]:
        issues = []
        if not self.question:
            issues.append("Escalation must have a research question")
        if not self.rationale:
            issues.append("Escalation must have a rationale")
        if not self.affects:
            issues.append("Escalation must state what estimate/claim/decision it serves")
        if self.escalation_type not in ESCALATION_TYPES:
            issues.append(f"Unknown escalation type: {self.escalation_type}")
        return issues


class EscalationManager:
    """
    Manages analytical escalations for a research plan.

    Usage:
        mgr = EscalationManager(conn, plan_id, company_id)

        # Propose an escalation
        eid = mgr.propose(EscalationProposal(
            escalation_type="BRIDGE_ANALYSIS",
            question="Is margin expansion driven by labor leverage or food cost tailwind?",
            rationale="Need to test margin hypothesis — consensus assumes flat margin",
            data_needed="FY2023-FY2025 restaurant-level P&L line items",
            transformation="Period-over-period margin bridge by cost bucket",
            affects="EBIT margin assumption in base case",
        ))

        # Check what's proposed
        proposals = mgr.get_proposals()

        # Execute and produce workpaper
        workpaper = mgr.execute(eid, run_id=...)

        # Tie back to estimates
        mgr.record_result(eid, result_summary="...", workpaper_id=...)
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str, company_id: str):
        self.conn = conn
        self.plan_id = plan_id
        self.company_id = company_id

    def propose(self, proposal: EscalationProposal, run_id: str = None) -> str:
        """
        Propose an analytical escalation. Returns escalation_id.
        Validates that the proposal has a question, rationale, and target.
        """
        issues = proposal.validate()
        if issues:
            raise EscalationError(f"Invalid proposal: {'; '.join(issues)}")

        eid = new_id()
        self.conn.execute(
            """INSERT INTO analytical_escalation
               (escalation_id, plan_id, company_id, escalation_type,
                question, rationale, data_needed, transformation,
                affects, status, priority, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, self.plan_id, self.company_id, proposal.escalation_type,
             proposal.question, proposal.rationale,
             proposal.data_needed, proposal.transformation,
             proposal.affects, "proposed", proposal.priority, run_id))
        self.conn.commit()
        return eid

    def get_proposals(self, status: str = None) -> list[dict]:
        """Get escalation proposals, optionally filtered by status."""
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        query = "SELECT * FROM analytical_escalation WHERE plan_id = ?"
        params = [self.plan_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY priority, created_at"

        rows = self.conn.execute(query, params).fetchall()
        self.conn.row_factory = old
        return [dict(r) for r in rows]

    def approve(self, escalation_id: str):
        """Approve a proposed escalation."""
        self.conn.execute(
            "UPDATE analytical_escalation SET status = 'approved' WHERE escalation_id = ?",
            (escalation_id,))
        self.conn.commit()

    def skip(self, escalation_id: str, reason: str = None):
        """Skip an escalation with an optional reason."""
        self.conn.execute(
            "UPDATE analytical_escalation SET status = 'skipped', result_summary = ? WHERE escalation_id = ?",
            (reason or "Skipped — not justified for current research scope", escalation_id))
        self.conn.commit()

    def start(self, escalation_id: str):
        """Mark escalation as in progress."""
        self.conn.execute(
            "UPDATE analytical_escalation SET status = 'in_progress' WHERE escalation_id = ?",
            (escalation_id,))
        self.conn.commit()

    def complete(self, escalation_id: str, result_summary: str,
                 workpaper_id: str = None, resolution: str = None):
        """Mark escalation as completed with results."""
        self.conn.execute(
            """UPDATE analytical_escalation
               SET status = 'completed', result_summary = ?,
                   workpaper_id = ?, resolution = ?
               WHERE escalation_id = ?""",
            (result_summary, workpaper_id, resolution, escalation_id))
        self.conn.commit()

    def assess_plan_escalation_level(self) -> dict:
        """
        Assess how much analytical escalation a plan needs.

        Returns:
          level: "none" / "narrow" / "multiple" / "broad"
          proposed_count: number of proposed escalations
          light_count: number that are light (appropriate for upstream)
          heavy_count: number that require deeper work
        """
        proposals = self.get_proposals()

        light = [p for p in proposals if p["escalation_type"] in LIGHT_ESCALATIONS]
        heavy = [p for p in proposals if p["escalation_type"] not in LIGHT_ESCALATIONS]

        total = len(proposals)
        if total == 0:
            level = "none"
        elif len(heavy) == 0:
            level = "narrow"
        elif len(heavy) <= 2:
            level = "multiple"
        else:
            level = "broad"

        return {
            "level": level,
            "proposed_count": total,
            "light_count": len(light),
            "heavy_count": len(heavy),
            "by_type": {p["escalation_type"]: p["status"] for p in proposals},
        }


# ═══════════════════════════════════════════════════════════════
# PRIORITY 4: Workpaper layer — analyst-visible artifacts
# ═══════════════════════════════════════════════════════════════

class WorkpaperBuilder:
    """
    Creates analyst-visible workpapers as outputs of escalated analysis.

    Every workpaper answers a specific question, shows what data was used,
    how it was transformed, and what caveats remain.
    """

    def __init__(self, conn: sqlite3.Connection, company_id: str):
        self.conn = conn
        self.company_id = company_id

    def create(
        self, workpaper_type: str, title: str,
        content: dict, question: str = None,
        methodology: str = None, caveats: str = None,
        source_data: list[str] = None,
        affects: str = None, escalation_id: str = None,
        run_id: str = None,
    ) -> str:
        """
        Create a workpaper. Content is the actual analytical data (JSON).

        Returns workpaper_id.
        """
        wid = new_id()
        self.conn.execute(
            """INSERT INTO workpaper
               (workpaper_id, escalation_id, company_id, workpaper_type,
                title, question, content, methodology, caveats,
                source_data, affects, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wid, escalation_id, self.company_id, workpaper_type,
             title, question, json.dumps(content),
             methodology, caveats,
             json.dumps(source_data) if source_data else None,
             affects, run_id))
        self.conn.commit()
        return wid

    def get_workpaper(self, workpaper_id: str) -> dict | None:
        """Retrieve a workpaper with parsed content."""
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute(
            "SELECT * FROM workpaper WHERE workpaper_id = ?", (workpaper_id,)
        ).fetchone()
        self.conn.row_factory = old
        if not row:
            return None
        d = dict(row)
        try:
            d["content"] = json.loads(d["content"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    def list_workpapers(self, company_id: str = None) -> list[dict]:
        """List all workpapers for a company."""
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        cid = company_id or self.company_id
        rows = self.conn.execute(
            "SELECT workpaper_id, workpaper_type, title, question, affects, created_at "
            "FROM workpaper WHERE company_id = ? ORDER BY created_at",
            (cid,)).fetchall()
        self.conn.row_factory = old
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# PRIORITY 2+3: Light upstream assists + question-driven execution
# ═══════════════════════════════════════════════════════════════

def build_cadence_table(
    conn: sqlite3.Connection, company_id: str,
    metric_name: str, escalation_id: str = None, run_id: str = None,
) -> str:
    """
    Priority 2: Light upstream assist — build a simple historical cadence
    table from existing metric series data. No external fetch needed.

    This is appropriate during Business Understanding or early Research Design.
    It produces an inspectable workpaper without requiring full escalation.

    Returns workpaper_id.
    """
    old = conn.row_factory
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT cms.value, cms.as_of_date,
               rp.fiscal_year, rp.fiscal_quarter, rp.period_type
        FROM company_metric_series cms
        JOIN metric_definition md ON cms.metric_id = md.metric_id
        LEFT JOIN reporting_period rp ON cms.period_id = rp.period_id
        WHERE cms.company_id = ? AND md.metric_name = ?
        ORDER BY COALESCE(rp.fiscal_year, 0), COALESCE(rp.fiscal_quarter, 0)
    """, (company_id, metric_name)).fetchall()

    conn.row_factory = old

    if not rows:
        # Try evidence items instead
        old2 = conn.row_factory
        conn.row_factory = sqlite3.Row
        ev_rows = conn.execute("""
            SELECT value, value_numeric, as_of_date, notes
            FROM evidence_item
            WHERE company_id = ? AND evidence_type = 'KEY_METRIC'
              AND (LOWER(value) LIKE ? OR LOWER(value) LIKE ?)
        """, (company_id, f"%{metric_name.lower()}%",
              f"%{metric_name.lower().replace('_', ' ')}%")).fetchall()
        conn.row_factory = old2

        if not ev_rows:
            return None

        table_data = []
        for e in ev_rows:
            ed = dict(e)
            period = "unknown"
            notes = ed.get("notes") or ""
            for part in notes.split("|"):
                if part.startswith("period:"):
                    period = part[7:]
            table_data.append({
                "period": period,
                "value": ed.get("value_numeric") or ed.get("value"),
                "as_of": ed.get("as_of_date"),
            })
    else:
        table_data = []
        for r in rows:
            rd = dict(r)
            period = f"FY{rd.get('fiscal_year', '?')}"
            if rd.get("fiscal_quarter"):
                period = f"Q{rd['fiscal_quarter']} {rd['fiscal_year']}"
            table_data.append({
                "period": period,
                "value": rd["value"],
                "as_of": rd.get("as_of_date"),
            })

    # Add y/y change where possible
    for i in range(1, len(table_data)):
        curr = table_data[i].get("value")
        prev = table_data[i - 1].get("value")
        if isinstance(curr, (int, float)) and isinstance(prev, (int, float)) and prev != 0:
            table_data[i]["yoy_change"] = round(curr - prev, 2)
            table_data[i]["yoy_pct"] = round((curr - prev) / abs(prev) * 100, 1)

    wb = WorkpaperBuilder(conn, company_id)
    wid = wb.create(
        workpaper_type="CADENCE_TABLE",
        title=f"{metric_name} — Historical Cadence",
        content={"metric": metric_name, "data": table_data},
        question=f"What is the historical cadence of {metric_name}?",
        methodology="Pulled from stored metric series or orientation evidence. "
                    "Y/Y changes calculated where sequential data exists.",
        caveats="Based on available data only. May not include all periods.",
        escalation_id=escalation_id,
        run_id=run_id,
    )
    return wid


def build_margin_bridge(
    conn: sqlite3.Connection, company_id: str,
    from_period: str, to_period: str,
    margin_data: dict,
    escalation_id: str = None, run_id: str = None,
) -> str:
    """
    Priority 3: Question-driven escalation artifact — margin bridge.

    margin_data should be: {
        "from_margin": float, "to_margin": float,
        "drivers": [{"name": str, "impact_bps": float, "certainty": str}, ...]
    }

    Returns workpaper_id.
    """
    total_impact = sum(d.get("impact_bps", 0) for d in margin_data.get("drivers", []))
    actual_change = (margin_data.get("to_margin", 0) - margin_data.get("from_margin", 0)) * 100
    residual = round(actual_change - total_impact, 1)

    bridge = {
        "from_period": from_period,
        "to_period": to_period,
        "from_margin": margin_data["from_margin"],
        "to_margin": margin_data["to_margin"],
        "change_bps": round(actual_change, 1),
        "drivers": margin_data["drivers"],
        "explained_bps": round(total_impact, 1),
        "residual_bps": residual,
    }

    wb = WorkpaperBuilder(conn, company_id)
    wid = wb.create(
        workpaper_type="BRIDGE_TABLE",
        title=f"Margin Bridge: {from_period} → {to_period}",
        content=bridge,
        question=f"What drove margin change from {from_period} to {to_period}?",
        methodology="Period-over-period bridge by identified cost/revenue drivers. "
                    f"Residual of {residual}bps not explained by identified drivers.",
        caveats=f"{'Residual is large — bridge may be incomplete' if abs(residual) > 20 else 'Bridge is reasonably complete'}",
        escalation_id=escalation_id,
        run_id=run_id,
    )
    return wid


# ═══════════════════════════════════════════════════════════════
# PRIORITY 5: Fetch-vs-use-vs-ask data resolution
# ═══════════════════════════════════════════════════════════════

@dataclass
class DataResolution:
    """Result of deciding how to get needed data."""
    strategy: str       # "use_stored" / "fetch" / "ask_user" / "degraded"
    reason: str
    data_available: bool
    caveats: str = ""


def resolve_data_need(
    conn: sqlite3.Connection, company_id: str,
    data_type: str, importance: str = "medium",
) -> DataResolution:
    """
    Decide whether to use stored data, fetch new data, ask the user,
    or proceed in degraded mode.

    Args:
        data_type: what kind of data is needed (metric series, filing, etc.)
        importance: "critical" / "important" / "nice_to_have"
    """
    old = conn.row_factory
    conn.row_factory = sqlite3.Row

    # Check if we already have relevant data
    has_metrics = conn.execute(
        "SELECT COUNT(*) FROM company_metric_series WHERE company_id = ?",
        (company_id,)).fetchone()[0]

    has_evidence = conn.execute(
        "SELECT COUNT(*) FROM evidence_item WHERE company_id = ?",
        (company_id,)).fetchone()[0]

    has_filings = conn.execute(
        """SELECT COUNT(*) FROM source_document
           WHERE company_id = ? AND source_type = 'FILING'""",
        (company_id,)).fetchone()[0]

    conn.row_factory = old

    # Decision logic
    if data_type in ("metric_series", "actuals", "historical_kpi"):
        if has_metrics >= 4:
            return DataResolution("use_stored", "Sufficient metric series data exists",
                                True)
        elif has_evidence >= 5:
            return DataResolution("use_stored",
                                "Metric series sparse but orientation evidence available",
                                True, "Data is from orientation observations, not structured series")
        else:
            if importance == "critical":
                return DataResolution("fetch", "Critical data missing — attempt API fetch",
                                    False, "May fail if API unavailable")
            else:
                return DataResolution("degraded",
                                    f"Data unavailable ({importance} importance) — proceeding without",
                                    False, "Analysis will be limited")

    elif data_type in ("filing", "10-K", "transcript"):
        if has_filings >= 1:
            return DataResolution("use_stored", "Filing data exists",
                                True)
        else:
            return DataResolution("ask_user",
                                "No filings available — user needs to provide or enable EDGAR fetch",
                                False, "Cannot proceed without filing data")

    elif data_type in ("external", "scanner", "foot_traffic", "commodity"):
        return DataResolution("ask_user",
                            f"External data ({data_type}) requires user-provided source or API key",
                            False, "System cannot autonomously acquire this data type")

    else:
        if has_evidence >= 3:
            return DataResolution("use_stored", "Using available evidence as best approximation",
                                True, "Data may not be exactly what was requested")
        else:
            return DataResolution("degraded", "Insufficient data — proceeding with explicit caveat",
                                False, "Results should be treated as provisional")


# ═══════════════════════════════════════════════════════════════
# PRIORITY 6: Tie-back to estimate and decision layers
# ═══════════════════════════════════════════════════════════════

@dataclass
class EscalationImpact:
    """Assessment of how an escalation affected the research."""
    escalation_id: str
    estimate_changed: bool
    confidence_changed: bool
    estimate_impact: str        # what moved and by how much
    confidence_impact: str      # how confidence was affected
    recommendation: str         # "repeat" / "skip_next_time" / "deepen_further"


def assess_escalation_impact(
    conn: sqlite3.Connection, escalation_id: str,
) -> EscalationImpact:
    """
    Evaluate whether an escalation materially affected the research.

    Checks:
    - Did any estimate revision cite this escalation's evidence?
    - Did the workpaper change an assumption?
    - Was the decision gate outcome affected?

    Returns structured impact assessment.
    """
    old = conn.row_factory
    conn.row_factory = sqlite3.Row

    esc = conn.execute(
        "SELECT * FROM analytical_escalation WHERE escalation_id = ?",
        (escalation_id,)).fetchone()

    conn.row_factory = old

    if not esc:
        return EscalationImpact(
            escalation_id, False, False,
            "Escalation not found", "", "skip_next_time")

    esc_d = dict(esc)
    workpaper_id = esc_d.get("workpaper_id")
    result = esc_d.get("result_summary", "")

    # Check if any estimate revisions reference evidence from this escalation's run
    run_id = esc_d.get("created_by_run")
    revision_count = 0
    if run_id:
        revision_count = conn.execute(
            "SELECT COUNT(*) FROM estimate_revision WHERE created_by_run = ?",
            (run_id,)).fetchone()[0]

    estimate_changed = revision_count > 0

    # Simple heuristic for confidence change
    confidence_changed = bool(result and any(
        kw in result.lower() for kw in ["confirms", "contradicts", "strengthens", "weakens"]))

    if estimate_changed:
        estimate_impact = f"{revision_count} estimate revision(s) linked to this escalation"
        recommendation = "repeat"
    elif confidence_changed:
        estimate_impact = "No estimate change"
        recommendation = "repeat"
    elif result:
        estimate_impact = "No estimate change"
        recommendation = "skip_next_time"
    else:
        estimate_impact = "Escalation incomplete — no result recorded"
        recommendation = "deepen_further"

    confidence_impact = (
        "Sharpened confidence" if confidence_changed
        else "No confidence impact" if not estimate_changed
        else "Confidence aligned with estimate change"
    )

    return EscalationImpact(
        escalation_id=escalation_id,
        estimate_changed=estimate_changed,
        confidence_changed=confidence_changed,
        estimate_impact=estimate_impact,
        confidence_impact=confidence_impact,
        recommendation=recommendation,
    )


class EscalationError(Exception):
    pass


def build_guidance_track_record(
    conn: sqlite3.Connection, company_id: str,
    metric_name: str = None,
    escalation_id: str = None, run_id: str = None,
) -> str | None:
    """
    Build a guidance track record table: for each period where we have
    both guidance and actuals, show guide vs actual and the delta.

    This answers: "Has management historically guided conservatively or
    aggressively, and should that affect how we interpret current guidance?"

    Returns workpaper_id, or None if insufficient data.
    """
    import sqlite3 as _sql

    old = conn.row_factory
    conn.row_factory = _sql.Row

    # Get all guidance points with matching actuals
    rows = conn.execute("""
        SELECT gp.guidance_id, gp.period_id, gp.guidance_type,
               gp.value_low, gp.value_high, gp.value_point,
               gp.guidance_date,
               md.metric_name, md.unit,
               rp.fiscal_year, rp.period_type,
               cms.value as actual_value
        FROM guidance_point gp
        JOIN metric_definition md ON gp.metric_id = md.metric_id
        JOIN reporting_period rp ON gp.period_id = rp.period_id
        LEFT JOIN company_metric_series cms
            ON cms.company_id = gp.company_id
            AND cms.metric_id = gp.metric_id
            AND cms.period_id = gp.period_id
        WHERE gp.company_id = ?
        ORDER BY md.metric_name, rp.fiscal_year
    """, (company_id,)).fetchall()

    conn.row_factory = old

    if not rows:
        return None

    # Filter to requested metric if specified
    if metric_name:
        rows = [r for r in rows if dict(r)["metric_name"] == metric_name]

    # Build track record
    records = []
    for r in rows:
        rd = dict(r)
        actual = rd.get("actual_value")
        guide_mid = rd.get("value_point")
        guide_low = rd.get("value_low")
        guide_high = rd.get("value_high")

        record = {
            "metric": rd["metric_name"],
            "period": f"FY{rd['fiscal_year']}",
            "guidance_low": guide_low,
            "guidance_mid": guide_mid,
            "guidance_high": guide_high,
            "actual": actual,
            "has_actual": actual is not None,
        }

        if actual is not None and guide_mid is not None:
            delta = actual - guide_mid
            record["delta_vs_mid"] = round(delta, 2)
            if guide_low is not None and guide_high is not None:
                if actual > guide_high:
                    record["position"] = "ABOVE_RANGE"
                elif actual < guide_low:
                    record["position"] = "BELOW_RANGE"
                else:
                    record["position"] = "WITHIN_RANGE"
            else:
                record["position"] = "ABOVE_MID" if delta > 0 else "BELOW_MID"
        else:
            record["delta_vs_mid"] = None
            record["position"] = "NO_ACTUAL" if actual is None else "NO_GUIDANCE"

        records.append(record)

    # Compute summary statistics (only for records with both guide and actual)
    completed = [r for r in records if r.get("delta_vs_mid") is not None]
    if completed:
        deltas = [r["delta_vs_mid"] for r in completed]
        avg_delta = sum(deltas) / len(deltas)
        above_count = sum(1 for r in completed if r["position"] in ("ABOVE_RANGE", "ABOVE_MID"))
        below_count = sum(1 for r in completed if r["position"] in ("BELOW_RANGE", "BELOW_MID"))
        within_count = sum(1 for r in completed if r["position"] == "WITHIN_RANGE")

        if avg_delta > 0.5:
            pattern = "CONSERVATIVE"
            pattern_text = f"Management has guided conservatively by an average of {avg_delta:+.1f} over {len(completed)} period(s)"
        elif avg_delta < -0.5:
            pattern = "AGGRESSIVE"
            pattern_text = f"Management has guided aggressively by an average of {avg_delta:+.1f} over {len(completed)} period(s)"
        else:
            pattern = "ACCURATE"
            pattern_text = f"Management guidance has been reasonably accurate (avg delta {avg_delta:+.1f}) over {len(completed)} period(s)"

        summary = {
            "pattern": pattern,
            "pattern_text": pattern_text,
            "avg_delta": round(avg_delta, 2),
            "periods_with_actual": len(completed),
            "above_guide": above_count,
            "below_guide": below_count,
            "within_range": within_count,
        }
    else:
        summary = {
            "pattern": "INSUFFICIENT",
            "pattern_text": "Not enough guide-vs-actual pairs to assess track record",
            "periods_with_actual": 0,
        }

    # Pending guidance (no actual yet)
    pending = [r for r in records if not r.get("has_actual")]

    wb = WorkpaperBuilder(conn, company_id)
    wid = wb.create(
        workpaper_type="GUIDANCE_TRACK_RECORD",
        title=f"Guidance Track Record{': ' + metric_name if metric_name else ''}",
        content={
            "records": records,
            "summary": summary,
            "pending_guidance": pending,
        },
        question=f"Has management historically guided conservatively or aggressively"
                 f"{' on ' + metric_name if metric_name else ''}?",
        methodology="Compares management guidance (low/mid/high) against actual reported "
                    "results for each period. Classifies as conservative, aggressive, or accurate.",
        caveats=f"Track record based on {summary.get('periods_with_actual', 0)} period(s). "
                f"{'Short sample — treat directionally.' if summary.get('periods_with_actual', 0) < 4 else ''} "
                f"CEO or CFO changes may reset the track record.",
        escalation_id=escalation_id,
        run_id=run_id,
    )
    return wid


def save_model_spec(
    conn: sqlite3.Connection, company_id: str,
    model_spec_data: dict, run_id: str = None,
) -> str:
    """
    Persist a ModelSpec as a workpaper so the dependency chain is
    traceable and auditable.
    """
    wb = WorkpaperBuilder(conn, company_id)
    return wb.create(
        workpaper_type="MODEL_SPEC",
        title="Estimate Model Specification",
        content=model_spec_data,
        question="What are the driver→output relationships in this estimate?",
        methodology="Driver-based model: assumptions flow mechanically to outputs. "
                    "SSS + new stores → revenue; revenue × margin → EBIT; EBIT → EPS.",
        caveats="Simplified model. Does not capture quarterly seasonality, mix effects, "
                "or full balance sheet dynamics. Sufficient for 2-3 key driver estimates.",
        run_id=run_id,
    )
