"""
Tasks 2-5 Implementation

Task 2: Estimate workflow (Layer 3 - Evidence & Estimate Building)
Task 3: Evidence -> claim -> estimate wiring (Layer 3/4)
Task 4: Decision gate (Layer 4 - Decision & Edge Assessment)
Task 5: Insider overlay adapter (Layer 3 overlay)

All four tasks are tested together because they form one workflow:
plan -> evidence -> claim -> estimate -> decision.
"""

import sqlite3
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert, now_iso


# ═══════════════════════════════════════════════════════════════
# TASK 2: Estimate Builder
# Product layer: Layer 3 (Evidence & Estimate Building)
# Why: proves the system can do real estimate-building, not just schema theater
# ═══════════════════════════════════════════════════════════════

class EstimateBuilder:
    """
    Builds estimates for one company against one research plan.

    Operates on canonical tables: estimate_case, estimate_assumption,
    estimate_driver, estimate_output.

    Every assumption is typed as:
      INDEPENDENT — our own view, backed by evidence
      CONSENSUS_HELD — we use the street number (no edge here)
      INFERRED — derived from other assumptions (e.g. EPS = net income / shares)
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str):
        self.conn = conn
        self.plan_id = plan_id

    def create_case(
        self, company_id: str, case_name: str,
        scenario_weight: float = None, summary: str = None,
        run_id: str = None,
    ) -> str:
        """Create an estimate case (base/bull/bear). Returns case_id."""
        # Get next version
        row = self.conn.execute(
            "SELECT MAX(case_version) FROM estimate_case WHERE company_id=? AND case_name=?",
            (company_id, case_name)
        ).fetchone()
        next_v = (row[0] or 0) + 1

        cid = new_id()
        self.conn.execute(
            """INSERT INTO estimate_case
               (case_id, company_id, plan_id, case_name, case_version,
                scenario_weight, summary, created_by_run)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, company_id, self.plan_id, case_name, next_v,
             scenario_weight, summary, run_id),
        )
        self.conn.commit()
        return cid

    def set_assumption(
        self, case_id: str, key: str, value: float,
        assumption_type: str = "INDEPENDENT",
        basis: str = None, confidence: float = None,
        evidence_id: str = None,
    ) -> str:
        """
        Set an assumption. Types: INDEPENDENT, CONSENSUS_HELD, INFERRED.
        Upserts on (case_id, assumption_key).
        """
        text = f"[{assumption_type}] {key} = {value}"
        if basis:
            text += f" | basis: {basis}"

        aid = new_id()
        upsert(self.conn, "estimate_assumption", {
            "assumption_id": aid,
            "case_id": case_id,
            "assumption_key": key,
            "assumption_value": value,
            "assumption_text": text,
            "basis": basis,
            "confidence": confidence,
            "evidence_id": evidence_id,
        }, conflict_columns=["case_id", "assumption_key"],
        update_columns=["assumption_value", "assumption_text", "basis",
                        "confidence", "evidence_id"])
        self.conn.commit()
        return aid

    def link_driver(
        self, case_id: str, driver_id: str,
        driver_value: float = None, driver_impact: str = None,
    ):
        """Link a key driver to this estimate case."""
        upsert(self.conn, "estimate_driver", {
            "case_id": case_id,
            "driver_id": driver_id,
            "driver_value": driver_value,
            "driver_impact": driver_impact,
        }, conflict_columns=["case_id", "driver_id"],
        update_columns=["driver_value", "driver_impact"])
        self.conn.commit()

    def set_output(
        self, case_id: str, period_id: str, line_item: str,
        value: float, vs_consensus: float = None,
        vs_guidance_mid: float = None, notes: str = None,
    ) -> str:
        """Set an estimate output line item. Upserts on (case_id, period_id, line_item)."""
        oid = new_id()
        upsert(self.conn, "estimate_output", {
            "output_id": oid,
            "case_id": case_id,
            "period_id": period_id,
            "line_item": line_item,
            "value": value,
            "vs_consensus": vs_consensus,
            "vs_guidance_mid": vs_guidance_mid,
            "notes": notes,
        }, conflict_columns=["case_id", "period_id", "line_item"],
        update_columns=["value", "vs_consensus", "vs_guidance_mid", "notes"])
        self.conn.commit()
        return oid

    def get_estimate_summary(self, company_id: str) -> list[dict]:
        """Get all estimate cases and outputs for a company."""
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        cases = self.conn.execute("""
            SELECT ec.case_id, ec.case_name, ec.scenario_weight, ec.summary,
                   ec.case_version
            FROM estimate_case ec
            WHERE ec.company_id = ? AND ec.plan_id = ?
            ORDER BY ec.case_name, ec.case_version DESC
        """, (company_id, self.plan_id)).fetchall()

        result = []
        seen_cases = set()
        for c in cases:
            cd = dict(c)
            key = cd["case_name"]
            if key in seen_cases:
                continue  # only latest version
            seen_cases.add(key)

            # Get assumptions
            cd["assumptions"] = [dict(a) for a in self.conn.execute(
                "SELECT * FROM estimate_assumption WHERE case_id=?", (cd["case_id"],)
            ).fetchall()]

            # Get outputs
            cd["outputs"] = [dict(o) for o in self.conn.execute("""
                SELECT eo.*, rp.period_type, rp.fiscal_year, rp.fiscal_quarter
                FROM estimate_output eo
                JOIN reporting_period rp ON eo.period_id = rp.period_id
                WHERE eo.case_id = ?
                ORDER BY rp.fiscal_year, rp.fiscal_quarter
            """, (cd["case_id"],)).fetchall()]

            result.append(cd)

        self.conn.row_factory = old
        return result


# ═══════════════════════════════════════════════════════════════
# TASK 3: Claim Builder (evidence -> claim -> estimate wiring)
# Product layer: Layer 3/4 boundary
# Why: proves "show your work" is real, not just linked tables
# ═══════════════════════════════════════════════════════════════

class ClaimBuilder:
    """
    Builds claims from evidence and wires them to estimates.

    A claim is a specific analytical assertion that can be:
    - supported or contradicted by evidence
    - linked to estimate assumptions it affects
    - rendered with full traceability
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_claim(
        self, company_id: str, plan_id: str, thesis_id: str,
        claim_text: str, claim_type: str,
        affects: str, confidence: float,
        falsifier: str, run_id: str = None,
    ) -> str:
        """Create a claim. Returns claim_id."""
        cid = new_id()
        self.conn.execute(
            """INSERT INTO claim
               (claim_id, company_id, thesis_id, plan_id, claim_text,
                claim_type, affects, confidence, falsifier, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, company_id, thesis_id, plan_id, claim_text,
             claim_type, affects, confidence, falsifier, run_id),
        )
        self.conn.commit()
        return cid

    def link_evidence(
        self, claim_id: str, evidence_id: str,
        role: str = "supports", importance: float = 1.0,
        rationale: str = None,
    ):
        """Link evidence to a claim. Role: supports | contradicts | contextual."""
        upsert(self.conn, "claim_evidence_link", {
            "claim_id": claim_id,
            "evidence_id": evidence_id,
            "role": role,
            "importance": importance,
            "rationale": rationale,
        }, conflict_columns=["claim_id", "evidence_id"],
        update_columns=["role", "importance", "rationale"])
        self.conn.commit()

    def link_to_assumption(
        self, claim_id: str, assumption_id: str,
        impact_direction: str = "positive",
        impact_magnitude: str = None,
        rationale: str = None,
    ):
        """Link a claim to an estimate assumption it affects."""
        upsert(self.conn, "claim_estimate_link", {
            "claim_id": claim_id,
            "assumption_id": assumption_id,
            "impact_direction": impact_direction,
            "impact_magnitude": impact_magnitude,
            "rationale": rationale,
        }, conflict_columns=["claim_id", "assumption_id"],
        update_columns=["impact_direction", "impact_magnitude", "rationale"])
        self.conn.commit()

    def render_claim(self, claim_id: str) -> dict | None:
        """
        Render a claim with full traceability:
        claim + supporting evidence + contradicting evidence +
        estimate impact + confidence + falsifier.
        """
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        claim = self.conn.execute(
            "SELECT * FROM claim WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if not claim:
            self.conn.row_factory = old
            return None

        cd = dict(claim)

        # Evidence links
        evidence = self.conn.execute("""
            SELECT cel.role, cel.importance, cel.rationale,
                   ei.evidence_type, ei.evidence_key, ei.value, ei.as_of_date,
                   sd.source_name, sd.source_locator
            FROM claim_evidence_link cel
            JOIN evidence_item ei ON cel.evidence_id = ei.evidence_id
            JOIN source_document sd ON ei.document_id = sd.document_id
            WHERE cel.claim_id = ?
            ORDER BY cel.importance DESC
        """, (claim_id,)).fetchall()

        cd["supporting_evidence"] = [
            dict(e) for e in evidence if e["role"] == "supports"
        ]
        cd["contradicting_evidence"] = [
            dict(e) for e in evidence if e["role"] == "contradicts"
        ]

        # Estimate impact
        impacts = self.conn.execute("""
            SELECT cel.impact_direction, cel.impact_magnitude, cel.rationale,
                   ea.assumption_key, ea.assumption_value, ea.basis
            FROM claim_estimate_link cel
            JOIN estimate_assumption ea ON cel.assumption_id = ea.assumption_id
            WHERE cel.claim_id = ?
        """, (claim_id,)).fetchall()
        cd["estimate_impacts"] = [dict(i) for i in impacts]

        self.conn.row_factory = old
        return cd


# ═══════════════════════════════════════════════════════════════
# TASK 4: Decision Gate
# Product layer: Layer 4 (Decision & Edge Assessment)
# Why: kills weak ideas before synthesis outruns substance
# ═══════════════════════════════════════════════════════════════

@dataclass
class DecisionCriterion:
    name: str
    passed: bool
    reason: str
    weight: float = 1.0


@dataclass
class DecisionResult:
    verdict: str         # NOT_VALUABLE_YET, INTERESTING_BUT_NOT_ACTIONABLE,
                         # MISSING_CRITICAL_EVIDENCE, WORTH_DEEPER_WORK, WORTH_PACKAGING
    criteria: list[DecisionCriterion]
    summary: str
    blocking_issues: list[str]

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.criteria if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.criteria)


class DecisionGate:
    """
    Assesses whether an idea passes the "valuable vs interesting" bar.

    Uses explicit criteria, not vague scoring. Returns structured reasons.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def assess(self, thesis_id: str, plan_id: str, company_id: str) -> DecisionResult:
        """
        Run all decision criteria against the current state of work.
        Returns a structured verdict with reasons.
        """
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        criteria = []
        blocking = []

        # 1. Is the edge specific and real?
        thesis = self.conn.execute(
            "SELECT * FROM thesis WHERE thesis_id=?", (thesis_id,)
        ).fetchone()
        thesis_d = dict(thesis) if thesis else {}

        edge_specific = bool(thesis_d.get("edge_source") and thesis_d.get("one_liner"))
        criteria.append(DecisionCriterion(
            "edge_is_specific",
            edge_specific,
            f"Edge: {thesis_d.get('edge_source', 'not defined')}" if edge_specific
            else "Edge hypothesis is missing or vague",
            weight=2.0,
        ))
        if not edge_specific:
            blocking.append("No specific edge hypothesis")

        # 2. Is the evidence quality sufficient?
        claims = self.conn.execute(
            "SELECT * FROM claim WHERE thesis_id=? AND status='active'", (thesis_id,)
        ).fetchall()

        evidence_links = 0
        for c in claims:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            evidence_links += count

        has_evidence = evidence_links >= 2  # at least 2 pieces of evidence
        criteria.append(DecisionCriterion(
            "evidence_sufficient",
            has_evidence,
            f"{evidence_links} evidence links across {len(claims)} claims"
            if has_evidence
            else f"Only {evidence_links} evidence links — need at least 2",
        ))
        if not has_evidence:
            blocking.append(f"Insufficient evidence ({evidence_links} links)")

        # 3. Did the work actually change the estimate?
        outputs = self.conn.execute("""
            SELECT eo.vs_consensus FROM estimate_output eo
            JOIN estimate_case ec ON eo.case_id = ec.case_id
            WHERE ec.plan_id = ? AND eo.vs_consensus IS NOT NULL
        """, (plan_id,)).fetchall()

        has_estimate_diff = any(abs(o["vs_consensus"]) > 0 for o in outputs)
        criteria.append(DecisionCriterion(
            "estimate_changed",
            has_estimate_diff,
            "Independent estimate differs from consensus on at least one metric"
            if has_estimate_diff
            else "No estimate output differs from consensus — work may be color only",
            weight=2.0,
        ))
        if not has_estimate_diff:
            blocking.append("Estimate does not differ from consensus")

        # 4. Is the key claim traceable?
        has_traceable_claim = False
        for c in claims:
            # Claim has evidence AND affects an assumption
            ev_count = self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            est_count = self.conn.execute(
                "SELECT COUNT(*) FROM claim_estimate_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            if ev_count > 0 and est_count > 0:
                has_traceable_claim = True
                break

        criteria.append(DecisionCriterion(
            "claim_traceable",
            has_traceable_claim,
            "At least one claim has both evidence and estimate linkage"
            if has_traceable_claim
            else "No claim is fully wired (evidence -> claim -> estimate)",
        ))

        # 5. Is there a falsifier?
        has_falsifier = any(c["falsifier"] for c in claims)
        criteria.append(DecisionCriterion(
            "has_falsifier",
            has_falsifier,
            "At least one claim has a defined falsifier"
            if has_falsifier
            else "No falsification condition defined — thesis is unfalsifiable",
        ))

        # 6. Is the opportunity still real (not just historical color)?
        has_forward_estimate = any(
            o["vs_consensus"] is not None for o in outputs
        )
        criteria.append(DecisionCriterion(
            "forward_looking",
            has_forward_estimate,
            "Has forward estimate outputs with consensus comparison"
            if has_forward_estimate
            else "No forward-looking estimate outputs",
        ))

        # 7. Kill conditions — have any been triggered?
        kills = self.conn.execute(
            "SELECT * FROM kill_condition WHERE plan_id=?", (plan_id,)
        ).fetchall()
        triggered_kills = [k for k in kills if k["status"] == "triggered"]
        no_kills_triggered = len(triggered_kills) == 0
        criteria.append(DecisionCriterion(
            "no_kills_triggered",
            no_kills_triggered,
            "No kill conditions triggered"
            if no_kills_triggered
            else f"{len(triggered_kills)} kill condition(s) triggered",
            weight=3.0,  # kill conditions are decisive
        ))
        if not no_kills_triggered:
            blocking.append("Kill condition triggered")

        self.conn.row_factory = old

        # ── Compute verdict ──
        weighted_pass = sum(c.weight for c in criteria if c.passed)
        weighted_total = sum(c.weight for c in criteria)
        score = weighted_pass / weighted_total if weighted_total > 0 else 0

        if not no_kills_triggered:
            verdict = "NOT_VALUABLE_YET"
        elif blocking:
            if has_estimate_diff and has_evidence:
                verdict = "MISSING_CRITICAL_EVIDENCE"
            elif has_evidence and not has_estimate_diff:
                verdict = "INTERESTING_BUT_NOT_ACTIONABLE"
            else:
                verdict = "NOT_VALUABLE_YET"
        elif score >= 0.85:
            verdict = "WORTH_PACKAGING"
        elif score >= 0.6:
            verdict = "WORTH_DEEPER_WORK"
        else:
            verdict = "INTERESTING_BUT_NOT_ACTIONABLE"

        summary = (
            f"{self.passed_count_str(criteria)}. "
            f"{'Blocking: ' + '; '.join(blocking) if blocking else 'No blocking issues.'}"
        )

        return DecisionResult(
            verdict=verdict,
            criteria=criteria,
            summary=summary,
            blocking_issues=blocking,
        )

    @staticmethod
    def passed_count_str(criteria):
        p = sum(1 for c in criteria if c.passed)
        return f"{p}/{len(criteria)} criteria passed"

    def record_assessment(self, thesis_id: str, result: DecisionResult,
                          run_id: str = None):
        """Store the decision assessment in the database."""
        upsert(self.conn, "decision_assessment", {
            "assessment_id": new_id(),
            "thesis_id": thesis_id,
            "edge_is_real": 1 if any(
                c.name == "edge_is_specific" and c.passed for c in result.criteria
            ) else 0,
            "edge_is_valuable": 1 if result.verdict in (
                "WORTH_DEEPER_WORK", "WORTH_PACKAGING"
            ) else 0,
            "transmission_clear": 1 if any(
                c.name == "claim_traceable" and c.passed for c in result.criteria
            ) else 0,
            "recommendation": result.verdict,
            "created_by_run": run_id,
        }, conflict_columns=["thesis_id"],
        update_columns=["edge_is_real", "edge_is_valuable", "transmission_clear",
                        "recommendation", "created_by_run"])
        self.conn.commit()


# ═══════════════════════════════════════════════════════════════
# TASK 5: Insider Overlay Adapter
# Product layer: Layer 3 overlay (not core)
# Why: proves old scanner components can be subordinated to
#      the evidence-and-decision core
# ═══════════════════════════════════════════════════════════════

class InsiderOverlayAdapter:
    """
    Adapts insider transaction data into the evidence/claim framework.

    This is an OVERLAY, not core analysis. It runs only if the research
    plan authorizes the INSIDER_ACTIVITY or CAPITAL_ALLOCATION workstream.

    It does NOT directly trigger synthesis. It creates evidence_items
    that can be linked to claims as supporting or contradicting.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def analyze_insider_activity(
        self, company_id: str, plan_id: str, run_id: str,
    ) -> dict:
        """
        Analyze existing insider_transaction records for a company.
        Creates evidence_items summarizing the pattern.

        Does not fetch data (that's the loader's job).
        Does not directly create claims (that's the analyst's job).
        Only creates evidence items that can support or contradict claims.
        """
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        # Get recent transactions (last 180 days)
        txs = self.conn.execute("""
            SELECT * FROM insider_transaction
            WHERE company_id = ?
            ORDER BY transaction_date DESC
        """, (company_id,)).fetchall()

        self.conn.row_factory = old

        if not txs:
            return {"pattern": "NO_DATA", "evidence_ids": []}

        txs = [dict(t) for t in txs]

        # Classify transactions
        purchases = [t for t in txs if t.get("transaction_code") == "P"]
        sales = [t for t in txs if t.get("transaction_code") == "S"]
        awards = [t for t in txs if t.get("transaction_code") in ("A", "M")]

        purchase_value = sum(t.get("value", 0) or 0 for t in purchases)
        sale_value = sum(t.get("value", 0) or 0 for t in sales)

        # Determine pattern
        if len(purchases) >= 3 and purchase_value > sale_value * 2:
            pattern = "CLUSTER_BUYING"
            pattern_text = (
                f"Insider buying cluster: {len(purchases)} purchases totaling "
                f"${purchase_value:,.0f} vs {len(sales)} sales totaling ${sale_value:,.0f}"
            )
        elif len(sales) >= 3 and sale_value > purchase_value * 2:
            pattern = "CLUSTER_SELLING"
            pattern_text = (
                f"Insider selling cluster: {len(sales)} sales totaling "
                f"${sale_value:,.0f} vs {len(purchases)} purchases totaling ${purchase_value:,.0f}"
            )
        elif purchases and sales:
            pattern = "MIXED"
            pattern_text = (
                f"Mixed insider activity: {len(purchases)} purchases (${purchase_value:,.0f}) "
                f"and {len(sales)} sales (${sale_value:,.0f})"
            )
        elif awards and not purchases and not sales:
            pattern = "AWARDS_ONLY"
            pattern_text = f"Only compensation-related transactions ({len(awards)} awards/exercises)"
        else:
            pattern = "MINIMAL"
            pattern_text = f"{len(txs)} insider transaction(s), no clear pattern"

        # Create evidence item (summary)
        # Need a source document — use the first transaction's source or create synthetic
        source_doc_id = None
        for t in txs:
            if t.get("source_document_id"):
                source_doc_id = t["source_document_id"]
                break

        evidence_ids = []
        if source_doc_id:
            eid = new_id()
            upsert(self.conn, "evidence_item", {
                "evidence_id": eid,
                "document_id": source_doc_id,
                "company_id": company_id,
                "evidence_type": "INSIDER_PATTERN",
                "evidence_key": f"insider_summary_{company_id}",
                "value": pattern_text,
                "as_of_date": txs[0].get("transaction_date") if txs else None,
                "extraction_method": "INSIDER_OVERLAY_ADAPTER",
                "run_id": run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"],
            update_columns=["value", "as_of_date", "run_id"])
            self.conn.commit()
            evidence_ids.append(eid)

            # Add individual notable transactions as evidence
            notable = [t for t in purchases + sales
                      if (t.get("value") or 0) > 100_000]
            for t in notable[:5]:
                neid = new_id()
                title = t.get("insider_title", "")
                name = t.get("insider_name", "")
                tx_type = t.get("transaction_type", t.get("transaction_code", ""))
                shares = t.get("shares", 0)
                price = t.get("price", 0)
                value = t.get("value", 0)

                ev_text = (
                    f"{name} ({title}): {tx_type} {shares:,.0f} shares "
                    f"at ${price:,.2f} (${value:,.0f})"
                )
                src = t.get("source_document_id", source_doc_id)
                if src:
                    upsert(self.conn, "evidence_item", {
                        "evidence_id": neid,
                        "document_id": src,
                        "company_id": company_id,
                        "evidence_type": "INSIDER_TX_DETAIL",
                        "evidence_key": f"insider_{name}_{t.get('transaction_date','')}_{tx_type}",
                        "value": ev_text,
                        "value_numeric": value,
                        "as_of_date": t.get("transaction_date"),
                        "extraction_method": "INSIDER_OVERLAY_ADAPTER",
                        "run_id": run_id,
                    }, conflict_columns=["document_id", "evidence_type", "evidence_key"],
                    update_columns=["value", "value_numeric", "as_of_date", "run_id"])
                    evidence_ids.append(neid)

            self.conn.commit()

        return {
            "pattern": pattern,
            "pattern_text": pattern_text,
            "purchases": len(purchases),
            "sales": len(sales),
            "purchase_value": purchase_value,
            "sale_value": sale_value,
            "evidence_ids": evidence_ids,
        }
