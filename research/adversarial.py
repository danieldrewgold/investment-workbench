"""
Adversarial Review Loop

Product layer: Layer 3-4 (between estimate building and decision gate)

This module addresses the system's main remaining weakness:
it can build a plausible thesis without pushing hard enough against itself.

P1: Contradiction capture — mandatory before estimate finalization
P2: Post-challenge revision loop — explicit keep/revise/lower/unresolved
P3: Decision gate strengthening — via criteria added here
P4: Adversarial workpapers — contradiction table, revision log, exposure summary
"""

import json
import sqlite3
from dataclasses import dataclass, field
from core.provenance.database import new_id, upsert, now_iso


# ═══════════════════════════════════════════════════════════════
# P1: Contradiction Capture
# ═══════════════════════════════════════════════════════════════

@dataclass
class Contradiction:
    """A piece of evidence or reasoning that cuts against the thesis."""
    assumption_key: str       # which assumption this challenges
    contradiction: str        # what the bear case is
    severity: str             # "serious" / "moderate" / "minor"
    source: str               # where this comes from
    what_would_resolve: str   # what evidence would settle the question
    evidence_id: str = None   # linked evidence item if available
    claim_id: str = None      # linked claim if available


class ContradictionCapture:
    """
    Records contradicting evidence paths for each key assumption.

    The system must attempt to find at least one serious contradiction
    per major assumption before the estimate can be finalized.

    Usage:
        cc = ContradictionCapture(conn, plan_id, company_id)

        # Record contradictions for each key assumption
        cc.record(Contradiction(
            assumption_key="sss_growth_pct",
            contradiction="Consumer spending decelerating; CMG SSS already slowing "
                         "from 8.0% to 7.9% to 6.5% over 3 years",
            severity="serious",
            source="Historical trend + macro indicators",
            what_would_resolve="Q1 2025 SSS data confirming traffic stability",
        ))

        # Check coverage
        coverage = cc.assess_coverage(["sss_growth_pct", "restaurant_margin_pct", "eps"])

        # Produce workpaper
        workpaper_id = cc.produce_contradiction_table()
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str, company_id: str):
        self.conn = conn
        self.plan_id = plan_id
        self.company_id = company_id
        self._contradictions: list[Contradiction] = []
        self._supports: list[Contradiction] = []  # bull-case evidence

    def record(self, contradiction: Contradiction, run_id: str = None) -> str:
        """Record a bear-case evidence path."""
        self._contradictions.append(contradiction)
        return self._record_evidence(contradiction, "BEAR_CASE", "contradicts", run_id)

    def record_support(self, support: Contradiction, run_id: str = None) -> str:
        """
        Record a bull-case evidence path that SUPPORTS the assumption.
        Uses the same Contradiction dataclass but with a supporting framing.

        This fixes the downward bias: the adversarial review should consider
        both what could go wrong AND what supports the current view.
        """
        self._supports.append(support)
        return self._record_evidence(support, "BULL_CASE", "supports", run_id)

    def _record_evidence(self, item: Contradiction, evidence_type: str,
                         link_role: str, run_id: str = None) -> str:
        eid = item.evidence_id
        if not eid and run_id:
            doc = self.conn.execute(
                "SELECT document_id FROM source_document WHERE company_id=? LIMIT 1",
                (self.company_id,)).fetchone()
            doc_id = doc[0] if doc else None

            if doc_id:
                prefix = "bear" if evidence_type == "BEAR_CASE" else "bull"
                ekey = f"{prefix}_{item.assumption_key}_{hash(item.contradiction) % 10000}"
                eid = new_id()
                upsert(self.conn, "evidence_item", {
                    "evidence_id": eid,
                    "document_id": doc_id,
                    "company_id": self.company_id,
                    "evidence_type": evidence_type,
                    "evidence_key": ekey,
                    "value": item.contradiction,
                    "notes": f"severity:{item.severity}|resolves:{item.what_would_resolve[:100]}",
                    "extraction_method": "ADVERSARIAL_REVIEW",
                    "run_id": run_id,
                }, conflict_columns=["document_id", "evidence_type", "evidence_key"],
                update_columns=["value", "notes", "run_id"])
                row = self.conn.execute(
                    "SELECT evidence_id FROM evidence_item WHERE document_id=? AND evidence_type=? AND evidence_key=?",
                    (doc_id, evidence_type, ekey)).fetchone()
                eid = row[0] if row else eid
                self.conn.commit()

        if eid and item.claim_id:
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO claim_evidence_link
                       (link_id, claim_id, evidence_id, role, importance, rationale)
                       VALUES (?,?,?,?,?,?)""",
                    (new_id(), item.claim_id, eid, link_role, 0.8,
                     f"{evidence_type}: {item.contradiction[:100]}"))
                self.conn.commit()
            except Exception:
                pass

        return eid or ""

    def record_no_contradiction_found(self, assumption_key: str, reason: str):
        """Explicitly record that no strong contradiction was found after checking."""
        self._contradictions.append(Contradiction(
            assumption_key=assumption_key,
            contradiction=f"[NO STRONG CONTRADICTION FOUND] {reason}",
            severity="none",
            source="adversarial_review",
            what_would_resolve="N/A — no contradiction identified",
        ))

    def assess_coverage(self, key_assumptions: list[str]) -> dict:
        """Check whether every key assumption has been adversarially tested."""
        covered = {}
        for c in self._contradictions:
            covered.setdefault(c.assumption_key, []).append(c)

        missing = [k for k in key_assumptions if k not in covered]
        serious = [k for k, cs in covered.items()
                   if any(c.severity == "serious" for c in cs)]

        return {
            "key_assumptions": key_assumptions,
            "covered": list(covered.keys()),
            "missing": missing,
            "serious_contradictions": serious,
            "total_contradictions": len(self._contradictions),
            "all_covered": len(missing) == 0,
            "assessment": (
                f"All {len(key_assumptions)} key assumptions adversarially tested. "
                f"{len(serious)} have serious contradictions."
                if not missing else
                f"Missing adversarial review for: {', '.join(missing)}"
            ),
        }

    def get_contradictions(self) -> list[Contradiction]:
        return list(self._contradictions)

    def get_supports(self) -> list[Contradiction]:
        return list(self._supports)

    def produce_contradiction_table(self, run_id: str = None) -> str:
        """Produce analyst-visible balanced adversarial review workpaper."""
        from research.escalation import WorkpaperBuilder

        bear_rows = [{"side": "BEAR", "assumption": c.assumption_key,
                      "evidence": c.contradiction, "severity": c.severity,
                      "source": c.source, "resolves": c.what_would_resolve}
                     for c in self._contradictions]
        bull_rows = [{"side": "BULL", "assumption": c.assumption_key,
                      "evidence": c.contradiction, "severity": c.severity,
                      "source": c.source, "resolves": c.what_would_resolve}
                     for c in self._supports]

        wb = WorkpaperBuilder(self.conn, self.company_id)
        return wb.create(
            workpaper_type="CONTRADICTION_TABLE",
            title="Balanced Adversarial Review — Bear & Bull Cases",
            content={"bear_cases": bear_rows, "bull_cases": bull_rows,
                     "total_bear": len(bear_rows), "total_bull": len(bull_rows)},
            question="What could prove the thesis wrong, and what supports it?",
            methodology="Balanced adversarial review: both bear and bull evidence "
                       "recorded for each key assumption before revision decisions.",
            caveats=f"Bear cases: {len(bear_rows)}, Bull cases: {len(bull_rows)}. "
                    f"{'Review is balanced' if bull_rows else 'WARNING: No bull cases — review has downward bias'}.",
            run_id=run_id,
        )


# ═══════════════════════════════════════════════════════════════
# P2: Post-Challenge Revision Loop
# ═══════════════════════════════════════════════════════════════

@dataclass
class RevisionDecision:
    """Decision about one assumption after adversarial challenge."""
    assumption_key: str
    prior_value: float
    decision: str        # "keep" / "revise_down" / "revise_up" / "lower_confidence" / "raise_confidence" / "unresolved"
    new_value: float = None
    new_confidence: float = None
    reason: str = ""
    linked_contradiction: str = ""
    linked_challenge: str = ""
    direction: str = ""  # "bearish" / "bullish" / "neutral" — why the revision happened


class PostChallengeRevisionLoop:
    """
    After challenge artifacts are produced, explicitly decide for each
    key assumption whether to keep, revise up, revise down, or adjust confidence.

    IMPORTANT: The review should be BALANCED, not just bearish.
    For each assumption, consider:
      - bear case evidence (what could go wrong)
      - bull case evidence (what supports or strengthens the view)
      - guidance track record (does management typically guide conservatively?)
      - baseline comparison (is the assumption stretched or reasonable?)

    The FY2024 validation showed that always revising down is a bias.
    Sometimes the thesis is right and the bear case is wrong.
    """

    def __init__(self, conn: sqlite3.Connection, plan_id: str, case_id: str):
        self.conn = conn
        self.plan_id = plan_id
        self.case_id = case_id
        self._decisions: list[RevisionDecision] = []

    def decide(self, decision: RevisionDecision):
        self._decisions.append(decision)

    def apply_revisions(self, run_id: str = None):
        """Apply all revision decisions to the estimate."""
        from research.deeper_workflow import RevisionTrackingEstimateBuilder

        rb = RevisionTrackingEstimateBuilder(self.conn, self.plan_id)

        for d in self._decisions:
            if d.decision in ("revise_down", "revise_up", "revise") and d.new_value is not None:
                direction = d.direction or ("down" if d.new_value < d.prior_value else "up")
                rb.set_assumption(
                    self.case_id, d.assumption_key, d.new_value,
                    reason=f"POST-CHALLENGE REVISION ({direction}): {d.reason}",
                    run_id=run_id,
                )
            elif d.decision == "lower_confidence" and d.new_confidence is not None:
                self.conn.execute(
                    "UPDATE estimate_assumption SET confidence=? WHERE case_id=? AND assumption_key=?",
                    (d.new_confidence, self.case_id, d.assumption_key))
            elif d.decision == "raise_confidence" and d.new_confidence is not None:
                self.conn.execute(
                    "UPDATE estimate_assumption SET confidence=? WHERE case_id=? AND assumption_key=?",
                    (d.new_confidence, self.case_id, d.assumption_key))

        self.conn.commit()

    def get_summary(self) -> dict:
        """Summarize what changed after the challenge loop."""
        kept = [d for d in self._decisions if d.decision == "keep"]
        revised_down = [d for d in self._decisions if d.decision in ("revise_down", "revise") and d.new_value and d.new_value < d.prior_value]
        revised_up = [d for d in self._decisions if d.decision in ("revise_up", "revise") and d.new_value and d.new_value > d.prior_value]
        lowered = [d for d in self._decisions if d.decision == "lower_confidence"]
        raised = [d for d in self._decisions if d.decision == "raise_confidence"]
        unresolved = [d for d in self._decisions if d.decision == "unresolved"]

        summary_parts = []
        for d in revised_down:
            summary_parts.append(f"{d.assumption_key}: REVISED DOWN {d.prior_value} → {d.new_value} ({d.reason})")
        for d in revised_up:
            summary_parts.append(f"{d.assumption_key}: REVISED UP {d.prior_value} → {d.new_value} ({d.reason})")
        for d in lowered:
            summary_parts.append(f"{d.assumption_key}: KEPT at {d.prior_value} but confidence LOWERED to {d.new_confidence}")
        for d in raised:
            summary_parts.append(f"{d.assumption_key}: KEPT at {d.prior_value} but confidence RAISED to {d.new_confidence}")
        for d in kept:
            summary_parts.append(f"{d.assumption_key}: KEPT at {d.prior_value} ({d.reason})")
        for d in unresolved:
            summary_parts.append(f"{d.assumption_key}: UNRESOLVED at {d.prior_value} ({d.reason})")

        return {
            "total_reviewed": len(self._decisions),
            "kept": len(kept),
            "revised_down": len(revised_down),
            "revised_up": len(revised_up),
            "revised": len(revised_down) + len(revised_up),
            "confidence_lowered": len(lowered),
            "confidence_raised": len(raised),
            "unresolved": len(unresolved),
            "any_changed": len(revised_down) + len(revised_up) + len(lowered) + len(raised) > 0,
            "net_direction": ("bearish" if len(revised_down) > len(revised_up)
                             else "bullish" if len(revised_up) > len(revised_down)
                             else "balanced"),
            "summary": "\n".join(summary_parts),
        }

    def produce_revision_log(self, run_id: str = None) -> str:
        """P4: Produce analyst-visible post-challenge revision workpaper."""
        from research.escalation import WorkpaperBuilder

        rows = []
        for d in self._decisions:
            rows.append({
                "assumption": d.assumption_key,
                "prior_value": d.prior_value,
                "decision": d.decision,
                "new_value": d.new_value,
                "new_confidence": d.new_confidence,
                "reason": d.reason,
                "linked_contradiction": d.linked_contradiction,
            })

        summary = self.get_summary()

        wb = WorkpaperBuilder(self.conn, self.company_id)
        return wb.create(
            workpaper_type="POST_CHALLENGE_REVISION",
            title="Post-Challenge Assumption Review",
            content={"decisions": rows, "summary": summary},
            question="Did the adversarial review change the estimate?",
            methodology="Each key assumption reviewed against contradiction table, "
                       "baseline comparison, and challenge artifacts. "
                       "Explicit keep/revise/lower/unresolved decision recorded.",
            caveats=f"Reviewed {summary['total_reviewed']} assumptions. "
                    f"Revised: {summary['revised']}. Confidence lowered: {summary['confidence_lowered']}. "
                    f"Unresolved: {summary['unresolved']}.",
            run_id=run_id,
        )

    @property
    def company_id(self):
        row = self.conn.execute(
            "SELECT company_id FROM estimate_case WHERE case_id=?",
            (self.case_id,)).fetchone()
        return row[0] if row else None


# ═══════════════════════════════════════════════════════════════
# P4: Exposure Summary Workpaper
# ═══════════════════════════════════════════════════════════════

def produce_exposure_summary(
    conn: sqlite3.Connection, company_id: str, plan_id: str,
    case_id: str, contradictions: list[Contradiction],
    revision_summary: dict, run_id: str = None,
) -> str:
    """
    Produce a "where is the thesis most exposed" workpaper.
    Combines contradiction severity with assumption distance from consensus/guidance.
    """
    from research.escalation import WorkpaperBuilder

    # Load assumptions
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    assumptions = conn.execute(
        "SELECT assumption_key, assumption_value, confidence FROM estimate_assumption WHERE case_id=?",
        (case_id,)).fetchall()
    conn.row_factory = old

    exposures = []
    for a in assumptions:
        ad = dict(a)
        key = ad["assumption_key"]
        value = ad["assumption_value"]
        conf = ad.get("confidence") or 0.5

        # Find contradictions for this assumption
        relevant_contradictions = [c for c in contradictions if c.assumption_key == key]
        max_severity = max(
            (c.severity for c in relevant_contradictions),
            key=lambda s: {"serious": 3, "moderate": 2, "minor": 1, "none": 0}.get(s, 0),
            default="none"
        ) if relevant_contradictions else "none"

        # Exposure = low confidence + serious contradiction
        severity_score = {"serious": 3, "moderate": 2, "minor": 1, "none": 0}.get(max_severity, 0)
        exposure_score = severity_score * (1.0 - conf)

        exposures.append({
            "assumption": key,
            "value": value,
            "confidence": conf,
            "contradiction_severity": max_severity,
            "contradiction_count": len(relevant_contradictions),
            "exposure_score": round(exposure_score, 2),
            "contradictions": [c.contradiction[:80] for c in relevant_contradictions],
        })

    # Sort by exposure (highest first)
    exposures.sort(key=lambda x: x["exposure_score"], reverse=True)

    wb = WorkpaperBuilder(conn, company_id)
    return wb.create(
        workpaper_type="EXPOSURE_SUMMARY",
        title="Thesis Exposure Summary",
        content={
            "exposures": exposures,
            "revision_summary": revision_summary,
            "most_exposed": exposures[0]["assumption"] if exposures else "none",
        },
        question="Where is the thesis most exposed to being wrong?",
        methodology="Combines contradiction severity with assumption confidence. "
                    "Higher exposure = serious contradiction + low confidence.",
        caveats="Exposure score is directional, not calibrated. "
                "Human judgment required on whether exposure is acceptable.",
        run_id=run_id,
    )
