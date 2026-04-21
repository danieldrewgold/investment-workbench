"""
Priorities 2-4 Implementation

Priority 2: Estimate revision tracking (Layer 3)
Priority 3: Stronger decision gate (Layer 4)
Priority 4: Automated evidence-to-estimate workflow (Layer 3/4)

These extend the existing core_workflow.py modules.
"""

import sqlite3
import json
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert, now_iso


# ═══════════════════════════════════════════════════════════════
# PRIORITY 2: Estimate Revision Tracking
# Product layer: Layer 3 (Evidence & Estimate Building)
# Why: the system must explain estimate evolution, not just final state
# ═══════════════════════════════════════════════════════════════

class RevisionTrackingEstimateBuilder:
    """
    Extends EstimateBuilder with automatic revision logging.

    Every time an assumption or output changes, a record is written
    to estimate_revision capturing: prior value, new value, reason,
    and linked evidence/claim.
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str):
        self.conn = conn
        self.plan_id = plan_id

    def set_assumption(
        self, case_id: str, key: str, value: float,
        assumption_type: str = "INDEPENDENT",
        basis: str = None, confidence: float = None,
        evidence_id: str = None, reason: str = None,
        linked_claim_id: str = None, run_id: str = None,
    ) -> str:
        """
        Set an assumption with automatic revision tracking.
        If the value changed, a revision record is created.
        """
        # Check if prior value exists
        prior = self.conn.execute(
            "SELECT assumption_value FROM estimate_assumption WHERE case_id=? AND assumption_key=?",
            (case_id, key)
        ).fetchone()

        prior_value = prior[0] if prior else None
        value_changed = prior_value is not None and prior_value != value

        # Upsert the assumption
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

        # Log revision if value changed
        if value_changed:
            self.conn.execute(
                """INSERT INTO estimate_revision
                   (revision_id, case_id, revision_type, field_key,
                    prior_value, new_value, change_amount, reason,
                    linked_claim_id, linked_evidence_id, created_by_run)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), case_id, "ASSUMPTION_CHANGE", key,
                 prior_value, value, value - prior_value,
                 reason or basis or "No reason provided",
                 linked_claim_id, evidence_id, run_id),
            )
        elif prior_value is None:
            # First time setting — record creation
            self.conn.execute(
                """INSERT INTO estimate_revision
                   (revision_id, case_id, revision_type, field_key,
                    prior_value, new_value, change_amount, reason,
                    linked_claim_id, linked_evidence_id, created_by_run)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), case_id, "ASSUMPTION_CREATED", key,
                 None, value, None,
                 reason or basis or "Initial assumption",
                 linked_claim_id, evidence_id, run_id),
            )

        self.conn.commit()
        return aid

    def set_output(
        self, case_id: str, period_id: str, line_item: str,
        value: float, vs_consensus: float = None,
        vs_guidance_mid: float = None, notes: str = None,
        reason: str = None, run_id: str = None,
    ) -> str:
        """Set an output with automatic revision tracking."""
        # Check prior
        prior = self.conn.execute(
            "SELECT value FROM estimate_output WHERE case_id=? AND period_id=? AND line_item=?",
            (case_id, period_id, line_item)
        ).fetchone()

        prior_value = prior[0] if prior else None
        value_changed = prior_value is not None and prior_value != value

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

        if value_changed:
            self.conn.execute(
                """INSERT INTO estimate_revision
                   (revision_id, case_id, revision_type, field_key,
                    prior_value, new_value, change_amount, reason,
                    created_by_run)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id(), case_id, "OUTPUT_CHANGE", line_item,
                 prior_value, value, value - prior_value,
                 reason or notes or "Output updated", run_id),
            )
        elif prior_value is None:
            self.conn.execute(
                """INSERT INTO estimate_revision
                   (revision_id, case_id, revision_type, field_key,
                    prior_value, new_value, change_amount, reason,
                    created_by_run)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id(), case_id, "OUTPUT_CREATED", line_item,
                 None, value, None,
                 reason or "Initial output", run_id),
            )

        self.conn.commit()
        return oid

    def get_revision_history(self, case_id: str) -> list[dict]:
        """Get all revisions for an estimate case, newest first."""
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute(
            """SELECT * FROM estimate_revision
               WHERE case_id = ?
               ORDER BY created_at DESC""",
            (case_id,)
        ).fetchall()
        self.conn.row_factory = old
        return [dict(r) for r in rows]

    def get_what_changed(self, case_id: str, since: str = None) -> dict:
        """
        Structured 'what changed' view for an estimate case.
        Returns assumptions and outputs that changed, with reasons.
        """
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        query = """SELECT * FROM estimate_revision
                   WHERE case_id = ? AND revision_type LIKE '%_CHANGE'"""
        params = [case_id]
        if since:
            query += " AND created_at > ?"
            params.append(since)
        query += " ORDER BY created_at DESC"

        rows = self.conn.execute(query, params).fetchall()
        self.conn.row_factory = old

        assumptions_changed = []
        outputs_changed = []
        for r in rows:
            rd = dict(r)
            if rd["revision_type"] == "ASSUMPTION_CHANGE":
                assumptions_changed.append(rd)
            elif rd["revision_type"] == "OUTPUT_CHANGE":
                outputs_changed.append(rd)

        return {
            "total_changes": len(rows),
            "assumptions_changed": assumptions_changed,
            "outputs_changed": outputs_changed,
            "summary": self._build_change_summary(assumptions_changed, outputs_changed),
        }

    @staticmethod
    def _build_change_summary(assumptions: list, outputs: list) -> str:
        """Build a short structured explanation of what changed."""
        parts = []
        for a in assumptions:
            parts.append(
                f"{a['field_key']}: {a['prior_value']} -> {a['new_value']} "
                f"({a['change_amount']:+.2f}) — {a.get('reason', '?')}"
            )
        for o in outputs:
            parts.append(
                f"{o['field_key']}: {o['prior_value']} -> {o['new_value']} "
                f"({o['change_amount']:+.2f}) — {o.get('reason', '?')}"
            )
        return "\n".join(parts) if parts else "No changes recorded."


# ═══════════════════════════════════════════════════════════════
# PRIORITY 3: Stronger Decision Gate
# Product layer: Layer 4 (Decision & Edge Assessment)
# Why: the gate should behave like a skeptical research reviewer,
#      not a point system
# ═══════════════════════════════════════════════════════════════

@dataclass
class ResearchCriterion:
    """One assessment criterion. Grounded in research practice, not scoring."""
    name: str
    category: str          # EDGE, EVIDENCE, ESTIMATE, TRACEABILITY, OPPORTUNITY
    assessment: str        # specific finding, not just pass/fail
    passed: bool
    severity: str = "required"  # required, important, informative
    detail: str = ""


@dataclass
class StrongerDecisionResult:
    verdict: str
    criteria: list[ResearchCriterion]
    blocking_issues: list[str]
    summary: str
    is_novel: bool = False
    is_interesting: bool = False
    is_valuable: bool = False
    is_actionable: bool = False
    is_package_ready: bool = False


class StrongerDecisionGate:
    """
    Research-quality decision gate.

    Distinguishes clearly between:
      novel — the finding is new
      interesting — worth knowing about
      valuable — changes an estimate, conviction, or risk assessment
      actionable — ready for a position or communication
      package-ready — enough substance to share externally

    Uses explicit criteria, not crude scoring.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def assess(self, thesis_id: str, plan_id: str, company_id: str) -> StrongerDecisionResult:
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        criteria = []
        blocking = []

        # ── Load state ──
        thesis = self.conn.execute(
            "SELECT * FROM thesis WHERE thesis_id=?", (thesis_id,)
        ).fetchone()
        td = dict(thesis) if thesis else {}

        claims = [dict(c) for c in self.conn.execute(
            "SELECT * FROM claim WHERE thesis_id=? AND status='active'", (thesis_id,)
        ).fetchall()]

        outputs = [dict(o) for o in self.conn.execute("""
            SELECT eo.* FROM estimate_output eo
            JOIN estimate_case ec ON eo.case_id = ec.case_id
            WHERE ec.plan_id = ?
        """, (plan_id,)).fetchall()]

        kills = [dict(k) for k in self.conn.execute(
            "SELECT * FROM kill_condition WHERE plan_id=?", (plan_id,)
        ).fetchall()]

        revisions = [dict(r) for r in self.conn.execute("""
            SELECT er.* FROM estimate_revision er
            JOIN estimate_case ec ON er.case_id = ec.case_id
            WHERE ec.plan_id = ?
        """, (plan_id,)).fetchall()]

        self.conn.row_factory = old

        # ── EDGE CRITERIA ──

        # 1. Is the edge specific?
        edge_src = td.get("edge_source", "")
        one_liner = td.get("one_liner", "")
        edge_specific = bool(edge_src and one_liner and len(one_liner) > 20)
        criteria.append(ResearchCriterion(
            "edge_specificity", "EDGE",
            f"Edge source: {edge_src or 'none'}, one-liner: {len(one_liner)} chars"
            if edge_specific else "Edge hypothesis is missing or too vague",
            edge_specific, "required",
        ))
        if not edge_specific:
            blocking.append("Edge hypothesis is not specific enough")

        # 2. Is the edge credible? (has supporting evidence)
        total_evidence_links = 0
        for c in claims:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            total_evidence_links += n

        evidence_credible = total_evidence_links >= 2
        criteria.append(ResearchCriterion(
            "edge_credibility", "EDGE",
            f"{total_evidence_links} evidence links across {len(claims)} claims",
            evidence_credible, "required",
        ))
        if not evidence_credible:
            blocking.append(f"Insufficient evidence support ({total_evidence_links} links, need >=2)")

        # ── EVIDENCE CRITERIA ──

        # 3. Evidence quality: are sources primary?
        primary_sources = self.conn.execute("""
            SELECT COUNT(DISTINCT sd.document_id)
            FROM claim_evidence_link cel
            JOIN evidence_item ei ON cel.evidence_id = ei.evidence_id
            JOIN source_document sd ON ei.document_id = sd.document_id
            JOIN claim c ON cel.claim_id = c.claim_id
            WHERE c.thesis_id = ? AND sd.source_type IN ('FILING', 'TRANSCRIPT')
        """, (thesis_id,)).fetchone()[0]

        has_primary = primary_sources >= 1
        criteria.append(ResearchCriterion(
            "evidence_quality", "EVIDENCE",
            f"{primary_sources} primary source(s) (filings/transcripts)"
            if has_primary else "No primary source evidence (filings/transcripts)",
            has_primary, "important",
        ))

        # 4. Contradicting evidence considered?
        contradictions = 0
        for c in claims:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence_link WHERE claim_id=? AND role='contradicts'",
                (c["claim_id"],)
            ).fetchone()[0]
            contradictions += n

        # Also check BEAR_CASE evidence items from adversarial review
        bear_evidence = self.conn.execute(
            "SELECT COUNT(*) FROM evidence_item WHERE company_id=? AND evidence_type='BEAR_CASE'",
            (company_id,)).fetchone()[0]

        has_adversarial = contradictions > 0 or bear_evidence > 0
        criteria.append(ResearchCriterion(
            "contradicting_evidence", "EVIDENCE",
            f"{contradictions} contradicting link(s), {bear_evidence} bear case item(s)"
            if has_adversarial else "No contradicting evidence or bear case explicitly recorded",
            has_adversarial, "important",
            detail="Estimate should not be finalized without adversarial testing",
        ))

        # 4b. Post-challenge revision loop completed?
        post_challenge_revisions = [r for r in revisions
                                   if r.get("reason", "").startswith("POST-CHALLENGE")]
        has_post_challenge = len(post_challenge_revisions) > 0
        criteria.append(ResearchCriterion(
            "post_challenge_review", "ESTIMATE",
            f"{len(post_challenge_revisions)} post-challenge revision(s) recorded"
            if has_post_challenge else "No post-challenge revision loop — estimate may be first-pass only",
            has_post_challenge, "important",
            detail="Estimate should be reviewed after contradiction and challenge artifacts are produced",
        ))

        # 4c. Prediction credibility: is the estimate reasonably aligned
        #     with at least one reference point (guidance or consensus)?
        max_vs_consensus = 0
        for o in outputs:
            vc = o.get("vs_consensus")
            if vc is not None:
                pct = abs(vc / max(abs(o.get("value", 1)), 0.01)) * 100
                max_vs_consensus = max(max_vs_consensus, pct)

        # Credible = within 15% of consensus on biggest output,
        # or has strong adversarial justification
        credible = max_vs_consensus < 15 or (has_adversarial and has_post_challenge)
        criteria.append(ResearchCriterion(
            "prediction_credibility", "ESTIMATE",
            f"Largest vs-consensus deviation: {max_vs_consensus:.0f}%"
            + (" — adversarially tested" if has_adversarial else " — NOT adversarially tested"),
            credible, "important",
            detail="Large deviations from consensus require adversarial justification",
        ))

        # ── ESTIMATE CRITERIA ──

        # 5. Did the work actually change the estimate?
        meaningful_diffs = [o for o in outputs if o.get("vs_consensus") and abs(o["vs_consensus"]) > 0]
        estimate_changed = len(meaningful_diffs) > 0
        criteria.append(ResearchCriterion(
            "estimate_impact", "ESTIMATE",
            f"{len(meaningful_diffs)} outputs differ from consensus"
            if estimate_changed else "No estimate output differs from consensus",
            estimate_changed, "required",
            detail="Research that doesn't change the estimate is color, not core",
        ))
        if not estimate_changed:
            blocking.append("Estimate does not differ from consensus — work may be color only")

        # 6. Estimate has revision history (work was iterative, not one-shot)
        has_revisions = len([r for r in revisions if r["revision_type"].endswith("_CHANGE")]) > 0
        criteria.append(ResearchCriterion(
            "estimate_iteration", "ESTIMATE",
            f"{len(revisions)} revision records — estimate was refined"
            if has_revisions else "No revisions — estimate may be a first pass",
            has_revisions, "informative",
        ))

        # ── TRACEABILITY CRITERIA ──

        # 7. Key claim is fully wired (evidence -> claim -> estimate)
        fully_wired = False
        for c in claims:
            ev_n = self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            est_n = self.conn.execute(
                "SELECT COUNT(*) FROM claim_estimate_link WHERE claim_id=?",
                (c["claim_id"],)
            ).fetchone()[0]
            if ev_n > 0 and est_n > 0:
                fully_wired = True
                break

        criteria.append(ResearchCriterion(
            "claim_traceability", "TRACEABILITY",
            "At least one claim is fully wired: evidence -> claim -> estimate"
            if fully_wired else "No claim has both evidence and estimate linkage",
            fully_wired, "required",
        ))
        if not fully_wired:
            blocking.append("No claim is fully traceable from evidence to estimate")

        # 8. Has falsification conditions
        has_falsifier = any(c.get("falsifier") for c in claims)
        criteria.append(ResearchCriterion(
            "falsifiability", "TRACEABILITY",
            "At least one claim has a defined falsifier"
            if has_falsifier else "No falsification condition — thesis is unfalsifiable",
            has_falsifier, "important",
        ))

        # ── OPPORTUNITY CRITERIA ──

        # 9. Kill conditions — none triggered
        triggered = [k for k in kills if k["status"] == "triggered"]
        no_kills = len(triggered) == 0
        criteria.append(ResearchCriterion(
            "no_kills_triggered", "OPPORTUNITY",
            "No kill conditions triggered" if no_kills
            else f"{len(triggered)} kill condition(s) triggered — idea may be dead",
            no_kills, "required",
        ))
        if not no_kills:
            blocking.append("Kill condition triggered")

        # 10. Missing evidence assessment
        # Separate critical gaps from quality improvements
        critical_missing = []
        quality_gaps = []
        if not has_primary:
            critical_missing.append("No primary source reviewed")
        if not has_falsifier:
            quality_gaps.append("No falsifier defined")
        if not has_adversarial:
            quality_gaps.append("No contradicting evidence or bear case considered")
        if not has_post_challenge:
            quality_gaps.append("No post-challenge revision loop completed")

        # ── Compute verdict ──
        required_passed = all(c.passed for c in criteria if c.severity == "required")
        important_passed = sum(1 for c in criteria if c.severity == "important" and c.passed)
        total_important = sum(1 for c in criteria if c.severity == "important")

        is_novel = estimate_changed
        is_interesting = evidence_credible and edge_specific
        is_valuable = estimate_changed and fully_wired
        is_actionable = is_valuable and required_passed
        # WORTH_PACKAGING requires: all important criteria + adversarial + revision + credibility
        is_package_ready = (is_actionable and important_passed == total_important
                           and not critical_missing and not quality_gaps
                           and has_adversarial and has_post_challenge and credible)

        if not no_kills:
            verdict = "NOT_VALUABLE_YET"
        elif not required_passed:
            if is_interesting:
                verdict = "INTERESTING_BUT_NOT_ACTIONABLE"
            elif critical_missing:
                verdict = "MISSING_CRITICAL_EVIDENCE"
            else:
                verdict = "NOT_VALUABLE_YET"
        elif critical_missing:
            verdict = "MISSING_CRITICAL_EVIDENCE"
        elif is_package_ready:
            verdict = "WORTH_PACKAGING"
        elif is_actionable:
            # Actionable but has quality gaps = worth deeper work
            verdict = "WORTH_DEEPER_WORK" if quality_gaps else "WORTH_PACKAGING"
        else:
            verdict = "INTERESTING_BUT_NOT_ACTIONABLE"

        summary_parts = [f"{'✓' if c.passed else '✗'} [{c.category}] {c.name}: {c.assessment}"
                        for c in criteria]

        return StrongerDecisionResult(
            verdict=verdict,
            criteria=criteria,
            blocking_issues=blocking,
            summary="\n".join(summary_parts),
            is_novel=is_novel,
            is_interesting=is_interesting,
            is_valuable=is_valuable,
            is_actionable=is_actionable,
            is_package_ready=is_package_ready,
        )

    def record(self, thesis_id: str, result: StrongerDecisionResult, run_id: str = None):
        """Store the assessment."""
        upsert(self.conn, "decision_assessment", {
            "assessment_id": new_id(),
            "thesis_id": thesis_id,
            "edge_is_real": 1 if result.is_valuable else 0,
            "edge_is_valuable": 1 if result.is_actionable else 0,
            "transmission_clear": 1 if any(
                c.name == "claim_traceability" and c.passed for c in result.criteria
            ) else 0,
            "recommendation": result.verdict,
            "created_by_run": run_id,
        }, conflict_columns=["thesis_id"],
        update_columns=["edge_is_real", "edge_is_valuable", "transmission_clear",
                        "recommendation", "created_by_run"])
        self.conn.commit()


# ═══════════════════════════════════════════════════════════════
# PRIORITY 4: Automated Evidence-to-Estimate Workflow
# Product layer: Layer 3/4 bridge
# Why: proves the system performs coherent investment work,
#      not just stores research objects
# ═══════════════════════════════════════════════════════════════

class FundamentalWorkflow:
    """
    One narrow automated path:
    raw evidence -> normalized evidence -> claim -> estimate impact -> decision

    For this implementation: a margin-expansion thesis where
    reported actuals trigger a claim that raises the margin assumption.

    The workflow runs without manual assembly of each step.
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str):
        self.conn = conn
        self.plan_id = plan_id

    def run_margin_expansion_workflow(
        self, company_id: str, thesis_id: str, case_id: str,
        actual_metric_key: str,     # e.g. "fy2025_ebit_margin"
        assumption_key: str,        # e.g. "ebit_margin_pct"
        consensus_value: float,     # what the street expects
        claim_template: str = None, # template for the claim
        run_id: str = None,
    ) -> dict:
        """
        Automated workflow:
        1. Read the evidence item for the actual metric
        2. Compare actual to consensus
        3. If actual > consensus: construct a margin-upside claim
        4. Link claim to the evidence
        5. Adjust the estimate assumption based on the claim
        6. Update estimate output
        7. Run through decision gate

        Returns workflow result dict with all IDs and the decision.
        """
        from research.core_workflow import ClaimBuilder

        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row

        # Step 1: Find the evidence
        evidence = self.conn.execute(
            """SELECT ei.*, sd.source_name, sd.source_locator
               FROM evidence_item ei
               JOIN source_document sd ON ei.document_id = sd.document_id
               WHERE ei.company_id = ? AND ei.evidence_key = ?""",
            (company_id, actual_metric_key)
        ).fetchone()
        self.conn.row_factory = old

        if not evidence:
            return {"status": "NO_EVIDENCE", "detail": f"No evidence found for {actual_metric_key}"}

        ev = dict(evidence)
        actual_value = ev.get("value_numeric")
        if actual_value is None:
            # Try parsing from value text
            import re
            match = re.search(r'[\d.]+', ev.get("value", ""))
            if match:
                actual_value = float(match.group())

        if actual_value is None:
            return {"status": "NO_NUMERIC_VALUE", "detail": f"Cannot extract numeric value from evidence"}

        # Step 2: Compare to consensus
        surprise = actual_value - consensus_value
        surprise_direction = "above" if surprise > 0 else "below"

        if abs(surprise) < 0.01:
            return {"status": "IN_LINE", "detail": f"Actual {actual_value} roughly in line with consensus {consensus_value}"}

        # Step 3: Construct claim
        claim_text = claim_template or (
            f"{'Margin upside' if surprise > 0 else 'Margin downside'}: "
            f"reported {actual_value:.1f}% vs consensus {consensus_value:.1f}% "
            f"({surprise:+.1f}% surprise). "
            f"{'Operating leverage on revenue outperformance suggests this trend continues.'}"
        )

        cb = ClaimBuilder(self.conn)
        claim_id = cb.create_claim(
            company_id=company_id,
            plan_id=self.plan_id,
            thesis_id=thesis_id,
            claim_text=claim_text,
            claim_type="ESTIMATE",
            affects="EBIT_MARGIN",
            confidence=0.65 if abs(surprise) > 0.5 else 0.50,
            falsifier=f"Next quarter margin reverts to {consensus_value:.1f}% or below",
            run_id=run_id,
        )

        # Step 4: Link evidence to claim
        cb.link_evidence(claim_id, ev["evidence_id"],
                        role="supports", importance=1.0,
                        rationale=f"Reported actual {surprise_direction} consensus by {abs(surprise):.1f}%")

        # Step 5: Adjust estimate assumption
        builder = RevisionTrackingEstimateBuilder(self.conn, self.plan_id)
        # Set new assumption = actual + half the surprise continuation
        new_estimate = actual_value + (surprise * 0.5)

        # Get the assumption_id first
        assumption = self.conn.execute(
            "SELECT assumption_id FROM estimate_assumption WHERE case_id=? AND assumption_key=?",
            (case_id, assumption_key)
        ).fetchone()

        builder.set_assumption(
            case_id, assumption_key, new_estimate,
            assumption_type="INDEPENDENT",
            basis=f"Reported {actual_value:.1f}% {surprise_direction} consensus {consensus_value:.1f}%. "
                  f"Assume 50% of surprise persists ({new_estimate:.1f}%)",
            confidence=0.65,
            evidence_id=ev["evidence_id"],
            reason=f"Actual {surprise_direction} consensus by {abs(surprise):.1f}%",
            linked_claim_id=claim_id,
            run_id=run_id,
        )

        # Link claim to assumption
        if assumption:
            cb.link_to_assumption(
                claim_id, assumption[0],
                impact_direction="positive" if surprise > 0 else "negative",
                impact_magnitude=f"{surprise:+.1f}% surprise, {surprise*0.5:+.1f}% carried forward",
                rationale=f"Actual margin {surprise_direction} consensus supports estimate revision",
            )

        # Step 6: Run decision gate
        gate = StrongerDecisionGate(self.conn)
        decision = gate.assess(thesis_id, self.plan_id, company_id)
        gate.record(thesis_id, decision, run_id)

        return {
            "status": "COMPLETED",
            "evidence_id": ev["evidence_id"],
            "actual_value": actual_value,
            "consensus_value": consensus_value,
            "surprise": surprise,
            "claim_id": claim_id,
            "new_assumption_value": new_estimate,
            "decision_verdict": decision.verdict,
            "decision_summary": decision.summary,
        }

    def run_guidance_vs_independent_workflow(
        self, company_id: str, thesis_id: str, case_id: str,
        metric_name: str,           # e.g. "sss_growth"
        assumption_key: str,        # e.g. "sss_growth_pct"
        independent_value: float,   # our independent estimate
        run_id: str = None,
    ) -> dict:
        """
        Priority 5 workflow: Compare our independent KPI estimate against
        management guidance range (low/mid/high).

        Meaningfully different from the margin-surprise path because:
        - Uses guidance_point data (range), not consensus snapshot
        - Evaluates whether independent view falls within or outside guidance
        - Claims are about management credibility and guidance reliability
        - Adjustment logic considers guidance range width as signal of uncertainty

        Steps:
        1. Load guidance for the metric from guidance_point table
        2. Compare independent estimate to guidance range
        3. Construct a claim based on the comparison
        4. Link guidance evidence to claim
        5. Set or revise the estimate assumption
        6. Run decision gate
        """
        from research.core_workflow import ClaimBuilder
        import sqlite3 as _sqlite3

        old = self.conn.row_factory
        self.conn.row_factory = _sqlite3.Row

        # Step 1: Load guidance
        guidance = self.conn.execute("""
            SELECT gp.*, md.metric_name, md.unit,
                   sd.document_id as source_doc_id, sd.source_name
            FROM guidance_point gp
            JOIN metric_definition md ON gp.metric_id = md.metric_id
            JOIN source_document sd ON gp.source_document_id = sd.document_id
            WHERE gp.company_id = ? AND md.metric_name = ?
            ORDER BY gp.guidance_date DESC LIMIT 1
        """, (company_id, metric_name)).fetchone()

        self.conn.row_factory = old

        if not guidance:
            return {"status": "NO_GUIDANCE", "detail": f"No guidance found for {metric_name}"}

        gd = dict(guidance)
        g_low = gd.get("value_low")
        g_high = gd.get("value_high")
        g_mid = gd.get("value_point") or ((g_low + g_high) / 2 if g_low and g_high else None)

        if g_mid is None:
            return {"status": "INCOMPLETE_GUIDANCE", "detail": "Guidance has no usable values"}

        # Step 2: Compare independent estimate to guidance range
        if g_low and independent_value > g_high:
            position = "ABOVE_RANGE"
            gap = independent_value - g_high
            position_text = f"above guidance range ({g_low}-{g_high})"
        elif g_high and independent_value < g_low:
            position = "BELOW_RANGE"
            gap = independent_value - g_low
            position_text = f"below guidance range ({g_low}-{g_high})"
        elif abs(independent_value - g_mid) < 0.3:
            position = "AT_MIDPOINT"
            gap = independent_value - g_mid
            position_text = f"near guidance midpoint ({g_mid})"
        else:
            position = "WITHIN_RANGE"
            gap = independent_value - g_mid
            above_below = "above" if gap > 0 else "below"
            position_text = f"{above_below} guidance midpoint ({g_mid}) but within range ({g_low}-{g_high})"

        if position == "AT_MIDPOINT":
            return {
                "status": "IN_LINE",
                "detail": f"Independent estimate {independent_value} is {position_text}. No claim warranted.",
                "position": position,
                "gap_vs_mid": gap,
            }

        # Step 3: Construct claim
        # Find or create evidence item for the guidance itself
        guidance_ev = self.conn.execute(
            """SELECT evidence_id FROM evidence_item
               WHERE company_id=? AND evidence_type='GUIDANCE'
               AND evidence_key LIKE ?""",
            (company_id, f"%{metric_name}%")
        ).fetchone()

        # If no guidance evidence exists, create one
        if not guidance_ev and gd.get("source_doc_id"):
            from core.provenance.database import new_id as _new_id
            gev_id = _new_id()
            upsert(self.conn, "evidence_item", {
                "evidence_id": gev_id,
                "document_id": gd["source_doc_id"],
                "company_id": company_id,
                "evidence_type": "GUIDANCE",
                "evidence_key": f"guidance_{metric_name}",
                "value": f"{metric_name} guidance: {g_low}-{g_high} (mid: {g_mid})",
                "as_of_date": gd.get("guidance_date"),
                "run_id": run_id,
            }, conflict_columns=["document_id", "evidence_type", "evidence_key"])
            self.conn.commit()
            guidance_ev_id = gev_id
        else:
            guidance_ev_id = guidance_ev[0] if guidance_ev else None

        # Build claim text
        if position == "ABOVE_RANGE":
            claim_text = (
                f"Independent {metric_name} estimate of {independent_value} is {position_text}. "
                f"Management may be guiding conservatively, or our estimate carries upside risk. "
                f"Gap of {gap:+.1f} above top of range suggests meaningful divergence."
            )
            confidence = 0.60  # above range = more conviction needed
            falsifier = f"{metric_name} comes in below guidance midpoint of {g_mid}"
        elif position == "BELOW_RANGE":
            claim_text = (
                f"Independent {metric_name} estimate of {independent_value} is {position_text}. "
                f"Either management is too optimistic, or we are missing a driver. "
                f"Gap of {gap:+.1f} below bottom of range is a warning signal."
            )
            confidence = 0.55
            falsifier = f"{metric_name} comes in above guidance midpoint of {g_mid}"
        else:  # WITHIN_RANGE
            above_below = "above" if gap > 0 else "below"
            claim_text = (
                f"Independent {metric_name} estimate of {independent_value} is within guidance "
                f"range but {abs(gap):.1f} {above_below} midpoint. "
                f"Modest divergence — worth monitoring but not a strong standalone signal."
            )
            confidence = 0.50
            falsifier = f"{metric_name} moves to opposite side of guidance midpoint"

        cb = ClaimBuilder(self.conn)
        claim_id = cb.create_claim(
            company_id=company_id,
            plan_id=self.plan_id,
            thesis_id=thesis_id,
            claim_text=claim_text,
            claim_type="GUIDANCE_DIVERGENCE",
            affects=metric_name.upper(),
            confidence=confidence,
            falsifier=falsifier,
            run_id=run_id,
        )

        # Step 4: Link guidance evidence to claim
        if guidance_ev_id:
            cb.link_evidence(claim_id, guidance_ev_id,
                           role="supports", importance=1.0,
                           rationale=f"Management guided {g_low}-{g_high}, "
                                     f"independent estimate {independent_value}")

        # Step 5: Set assumption with revision tracking
        builder = RevisionTrackingEstimateBuilder(self.conn, self.plan_id)
        builder.set_assumption(
            case_id, assumption_key, independent_value,
            assumption_type="INDEPENDENT",
            basis=f"Independent estimate {independent_value} vs guidance {g_low}-{g_high} (mid {g_mid}). "
                  f"Position: {position_text}.",
            confidence=confidence,
            evidence_id=guidance_ev_id,
            reason=f"Independent {metric_name} estimate {position}: {gap:+.1f} vs guidance mid",
            linked_claim_id=claim_id,
            run_id=run_id,
        )

        # Link claim to assumption
        assumption = self.conn.execute(
            "SELECT assumption_id FROM estimate_assumption WHERE case_id=? AND assumption_key=?",
            (case_id, assumption_key)
        ).fetchone()
        if assumption:
            cb.link_to_assumption(
                claim_id, assumption[0],
                impact_direction="positive" if gap > 0 else "negative",
                impact_magnitude=f"{gap:+.1f} vs guidance mid",
                rationale=f"Independent view {position_text}",
            )

        # Step 6: Decision gate
        gate = StrongerDecisionGate(self.conn)
        decision = gate.assess(thesis_id, self.plan_id, company_id)
        gate.record(thesis_id, decision, run_id)

        return {
            "status": "COMPLETED",
            "position": position,
            "independent_value": independent_value,
            "guidance_low": g_low,
            "guidance_mid": g_mid,
            "guidance_high": g_high,
            "gap_vs_mid": gap,
            "claim_id": claim_id,
            "decision_verdict": decision.verdict,
        }
