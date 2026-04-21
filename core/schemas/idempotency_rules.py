"""
Idempotency Rules

Defines the natural key and conflict behavior for every table in the canonical schema.
This is the reference for how reruns, backfills, and partial updates should behave.

Three conflict behaviors:
- UPSERT: insert new, update specified columns on conflict
- APPEND: always insert (version incrementing)
- IGNORE: skip if natural key already exists
"""

RULES = {
    # ── Infrastructure ──
    "run": {
        "natural_key": ["run_id"],
        "conflict": "APPEND",
        "note": "Each run is unique. Never overwritten.",
    },

    # ── Core entities ──
    "company": {
        "natural_key": ["cik"],
        "fallback_key": ["ticker"],
        "conflict": "UPSERT",
        "update_columns": ["name", "gics_sector", "gics_industry", "market_cap",
                           "shares_outstanding", "updated_at", "updated_by_run"],
        "note": "CIK is authoritative. Ticker used if CIK unknown.",
    },
    "security": {
        "natural_key": ["ticker", "security_type"],
        "conflict": "UPSERT",
        "update_columns": ["exchange", "currency"],
    },
    "reporting_period": {
        "natural_key": ["company_id", "period_type", "fiscal_year", "fiscal_quarter"],
        "conflict": "UPSERT",
        "update_columns": ["period_start", "period_end", "earnings_date",
                           "earnings_date_confirmed", "updated_by_run"],
    },

    # ── Universe/peers ──
    "universe": {
        "natural_key": ["name"],
        "conflict": "UPSERT",
        "update_columns": ["description", "universe_type"],
    },
    "universe_membership": {
        "natural_key": ["universe_id", "company_id"],
        "conflict": "IGNORE",
        "note": "Once added, membership persists. Role/rank updated separately.",
    },
    "peer_relationship": {
        "natural_key": ["company_id", "peer_company_id", "relationship_type"],
        "conflict": "UPSERT",
        "update_columns": ["strength", "rationale"],
    },
    "coverage_entry": {
        "natural_key": ["analyst_name", "firm", "company_id"],
        "conflict": "UPSERT",
        "update_columns": ["rating", "price_target", "as_of_date"],
    },

    # ── Source/evidence ──
    "source_document": {
        "natural_key": ["source_type", "source_locator"],
        "conflict": "UPSERT",
        "update_columns": ["content_hash", "content_summary", "raw_content",
                           "fetched_at", "run_id"],
        "note": "Content hash detects actual changes. Re-fetch updates fetched_at and run_id.",
    },
    "evidence_item": {
        "natural_key": ["document_id", "evidence_type", "evidence_key"],
        "conflict": "UPSERT",
        "update_columns": ["value", "value_numeric", "confidence", "as_of_date",
                           "extraction_version", "run_id"],
    },

    # ── Research design ──
    "research_plan": {
        "natural_key": ["company_id", "plan_version"],
        "conflict": "APPEND",
        "note": "New version = new row. Old versions preserved.",
    },
    "research_question": {
        "natural_key": ["plan_id", "question_text"],
        "conflict": "IGNORE",
    },
    "key_driver": {
        "natural_key": ["plan_id", "driver_name"],
        "conflict": "UPSERT",
        "update_columns": ["importance", "transmission", "current_consensus", "independent_view"],
    },
    "workstream": {
        "natural_key": ["plan_id", "workstream_name"],
        "conflict": "UPSERT",
        "update_columns": ["status", "priority", "justification", "expected_output"],
    },
    "kill_condition": {
        "natural_key": ["plan_id", "condition_text"],
        "conflict": "IGNORE",
    },

    # ── Time-series ──
    "metric_definition": {
        "natural_key": ["metric_name", "metric_source"],
        "conflict": "UPSERT",
        "update_columns": ["unit", "frequency", "description"],
    },
    "company_metric_series": {
        "natural_key": ["company_id", "metric_id", "period_id", "source_document_id"],
        "conflict": "UPSERT",
        "update_columns": ["value", "value_text", "run_id"],
    },
    "external_metric_series": {
        "natural_key": ["metric_id", "as_of_date", "source_document_id"],
        "conflict": "UPSERT",
        "update_columns": ["value", "value_text", "run_id"],
    },
    "guidance_point": {
        "natural_key": ["company_id", "metric_id", "period_id", "guidance_type", "source_document_id"],
        "conflict": "UPSERT",
        "update_columns": ["value_low", "value_high", "value_point", "run_id"],
    },
    "consensus_snapshot": {
        "natural_key": ["company_id", "metric_id", "period_id", "as_of_date", "source_name"],
        "conflict": "UPSERT",
        "update_columns": ["estimate_mean", "estimate_median", "estimate_high",
                           "estimate_low", "num_analysts", "run_id"],
    },

    # ── Estimates ──
    "estimate_case": {
        "natural_key": ["company_id", "case_name", "case_version"],
        "conflict": "APPEND",
        "note": "New version = new row. Supports tracking estimate evolution.",
    },
    "estimate_assumption": {
        "natural_key": ["case_id", "assumption_key"],
        "conflict": "UPSERT",
        "update_columns": ["assumption_value", "assumption_text", "basis",
                           "confidence", "evidence_id"],
    },
    "estimate_driver": {
        "natural_key": ["case_id", "driver_id"],
        "conflict": "UPSERT",
        "update_columns": ["driver_value", "driver_impact", "sensitivity"],
    },
    "estimate_output": {
        "natural_key": ["case_id", "period_id", "line_item"],
        "conflict": "UPSERT",
        "update_columns": ["value", "vs_consensus", "vs_guidance_mid", "notes"],
    },

    # ── Capital allocation ──
    "capital_action": {
        "natural_key": ["company_id", "action_type", "action_date", "source_document_id"],
        "conflict": "UPSERT",
        "update_columns": ["amount", "shares", "description", "run_id"],
    },
    "share_count_snapshot": {
        "natural_key": ["company_id", "as_of_date", "count_type"],
        "conflict": "UPSERT",
        "update_columns": ["share_count", "source_document_id", "run_id"],
    },
    "insider_transaction": {
        "natural_key": ["company_id", "insider_name", "transaction_date",
                        "transaction_code", "shares"],
        "conflict": "UPSERT",
        "update_columns": ["price", "value", "insider_title", "ownership_after",
                           "source_document_id", "run_id"],
    },

    # ── Claims/thesis ──
    "thesis": {
        "natural_key": ["company_id", "thesis_version"],
        "conflict": "APPEND",
    },
    "thesis_revision": {
        "natural_key": ["thesis_id", "prior_thesis_id"],
        "conflict": "IGNORE",
    },
    "claim": {
        "natural_key": ["claim_id"],
        "conflict": "APPEND",
        "note": "Claims are append-only. Superseded claims get status='superseded'.",
    },
    "claim_evidence_link": {
        "natural_key": ["claim_id", "evidence_id"],
        "conflict": "UPSERT",
        "update_columns": ["role", "importance", "rationale"],
    },
    "claim_estimate_link": {
        "natural_key": ["claim_id", "assumption_id"],
        "conflict": "UPSERT",
        "update_columns": ["impact_direction", "impact_magnitude", "rationale"],
    },

    # ── Decision/packaging ──
    "decision_assessment": {
        "natural_key": ["thesis_id"],
        "conflict": "UPSERT",
        "update_columns": ["edge_is_real", "edge_is_valuable", "why_exists_still",
                           "strongest_evidence", "weakest_link", "transmission_clear",
                           "bear_case", "falsifier", "asymmetry_credible",
                           "recommendation", "created_by_run"],
    },
    "packaged_output": {
        "natural_key": ["thesis_id", "output_type", "output_version"],
        "conflict": "APPEND",
    },

    # ── Business understanding ──
    "business_context": {
        "natural_key": ["company_id", "context_version"],
        "conflict": "APPEND",
        "note": "New version per context build. Prior versions preserved.",
    },

    # ── Estimate revisions ──
    "estimate_revision": {
        "natural_key": ["revision_id"],
        "conflict": "APPEND",
        "note": "Append-only change log. Never overwritten.",
    },

    # ── Analytical escalation ──
    "analytical_escalation": {
        "natural_key": ["escalation_id"],
        "conflict": "APPEND",
        "note": "Each escalation is a unique justified request.",
    },
    "workpaper": {
        "natural_key": ["workpaper_id"],
        "conflict": "APPEND",
        "note": "Each workpaper is a unique analyst-visible artifact.",
    },
}

assert len(RULES) == 38, f"Expected 38 rules, got {len(RULES)}"
