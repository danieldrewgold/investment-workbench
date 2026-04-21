"""
Business Orientation Workflow

Product layer: Pre-Layer 2 (before Research Design)

This version focuses on evidence discipline:

P1: Source breadth — tracks whether conclusions rest on broad or narrow
    evidence. Classifies observations as single-source, cross-period,
    cross-source-type, or conflicting.
P2: Subtle evolution — detects framing shifts (growth→efficiency),
    issue maturation (temporary→structural), emphasis changes.
P3: Confidence penalization — confidence is lowered when source breadth
    is narrow, time coverage is thin, evidence is mostly inferred,
    or contradictions are unresolved.
P4: Honest handoff — carries source breadth, conflicts, and confidence
    penalties through to research design candidates.
"""

import json
import sqlite3
from collections import defaultdict
from core.provenance.database import new_id, upsert, now_iso


ORIENTATION_EVIDENCE_TYPES = {
    "BUSINESS_DESCRIPTION": "What the company does",
    "SEGMENT_INFO": "Segment or business line details",
    "REVENUE_MODEL": "How the company makes money",
    "KEY_METRIC": "Metric that management or the street focuses on",
    "GROWTH_CADENCE": "Historical growth pattern or trend",
    "MARGIN_CADENCE": "Historical margin pattern or trend",
    "MANAGEMENT_THEME": "How management frames the business or strategy",
    "RECURRING_DEBATE": "Bull/bear debate that recurs in the name",
    "CAPITAL_ALLOCATION": "Buyback, dividend, M&A, or investment pattern",
    "RECENT_CHANGE": "Something that recently changed or is inflecting",
    "RISK_FACTOR": "Identified risk or concern",
    "COST_STRUCTURE": "Major cost bucket or margin driver",
    "COMPETITIVE_POSITION": "Competitive dynamics or market share",
    "GUIDANCE_ITEM": "Management guidance for a specific metric",
}

ESTIMATE_RELEVANT_TYPES = {
    "KEY_METRIC", "GROWTH_CADENCE", "MARGIN_CADENCE", "COST_STRUCTURE",
    "GUIDANCE_ITEM", "RECENT_CHANGE", "CAPITAL_ALLOCATION",
}

# Source types for breadth scoring
SOURCE_TYPE_CATEGORIES = {
    "FILING": "regulatory",
    "TRANSCRIPT": "management_commentary",
    "PRESENTATION": "management_commentary",
    "WEBPAGE": "external",
    "API_RESPONSE": "market_data",
}

# Framing keywords for subtle evolution detection
GROWTH_FRAMING = {"growth", "acceleration", "expansion", "momentum", "outperformance", "upside"}
EFFICIENCY_FRAMING = {"efficiency", "throughput", "optimization", "discipline", "leverage", "productivity"}
TEMPORARY_FRAMING = {"temporary", "transitory", "one-time", "short-term", "timing"}
STRUCTURAL_FRAMING = {"structural", "persistent", "ongoing", "sustained", "secular", "permanent"}


class OrientationWorkflow:

    def __init__(self, conn: sqlite3.Connection, company_id: str, run_id: str = None):
        self.conn = conn
        self.company_id = company_id
        self.run_id = run_id
        self._observations = []

    # ── Observation recording ────────────────────────────────

    def observe(
        self, document_id: str, evidence_type: str,
        observation: str, numeric: float = None,
        unit: str = None, as_of: str = None,
        period_label: str = None, certainty: str = "observed",
        segment: str = None, estimate_relevance: str = None,
    ) -> str:
        if evidence_type not in ORIENTATION_EVIDENCE_TYPES:
            evidence_type = "BUSINESS_DESCRIPTION"
        if estimate_relevance is None:
            estimate_relevance = "high" if evidence_type in ESTIMATE_RELEVANT_TYPES else "low"

        key_input = f"{observation}|{period_label or ''}|{as_of or ''}"
        key = f"orient_{evidence_type.lower()}_{hash(key_input) % 100000}"
        notes_parts = []
        if period_label:
            notes_parts.append(f"period:{period_label}")
        if certainty != "observed":
            notes_parts.append(f"certainty:{certainty}")
        if segment:
            notes_parts.append(f"segment:{segment}")
        if estimate_relevance:
            notes_parts.append(f"est_relevance:{estimate_relevance}")
        notes = "|".join(notes_parts) if notes_parts else None

        eid = new_id()
        upsert(self.conn, "evidence_item", {
            "evidence_id": eid, "document_id": document_id,
            "company_id": self.company_id, "evidence_type": evidence_type,
            "evidence_key": key, "value": observation,
            "value_numeric": numeric, "unit": unit,
            "as_of_date": as_of, "notes": notes,
            "extraction_method": "ORIENTATION_PASS", "run_id": self.run_id,
        }, conflict_columns=["document_id", "evidence_type", "evidence_key"],
        update_columns=["value", "value_numeric", "unit", "as_of_date", "notes", "run_id"])

        row = self.conn.execute(
            "SELECT evidence_id FROM evidence_item WHERE document_id=? AND evidence_type=? AND evidence_key=?",
            (document_id, evidence_type, key)).fetchone()
        actual_eid = row[0] if row else eid
        self._observations.append(actual_eid)
        self.conn.commit()
        return actual_eid

    def digest_document(self, document_id: str, observations: list[dict]) -> list[str]:
        eids = []
        for obs in observations:
            eid = self.observe(
                document_id, obs.get("type", "BUSINESS_DESCRIPTION"), obs["text"],
                numeric=obs.get("numeric"), unit=obs.get("unit"), as_of=obs.get("as_of"),
                period_label=obs.get("period"), certainty=obs.get("certainty", "observed"),
                segment=obs.get("segment"), estimate_relevance=obs.get("estimate_relevance"))
            eids.append(eid)
        return eids

    # ── P1: Source breadth assessment ────────────────────────

    def assess_source_breadth(self) -> dict:
        """
        Assess how broadly supported the orientation conclusions are.

        Returns:
          source_types_seen: which categories of source material were used
          source_types_missing: which important categories are absent
          periods_covered: how many distinct periods have evidence
          observations_by_support_level: single-source / cross-period / cross-source-type
          conflicts: observations where different sources say different things
          breadth_verdict: "broad" / "adequate" / "narrow" / "single-source"
        """
        evidence = self._load_all_evidence()
        if not evidence:
            return {"breadth_verdict": "no_evidence", "source_types_seen": [],
                    "periods_covered": 0, "conflicts": []}

        # Source type diversity
        source_types = set()
        source_categories = set()
        periods = set()
        docs = set()

        for e in evidence:
            st = e.get("source_type", "")
            source_types.add(st)
            source_categories.add(SOURCE_TYPE_CATEGORIES.get(st, "other"))
            periods.add(self._get_meta(e, "period", self._period_from_date(e)))
            docs.add(e.get("document_id"))

        important_categories = {"regulatory", "management_commentary"}
        categories_missing = important_categories - source_categories

        # Classify observation support level
        # Group similar observations (same type + similar text) across docs/periods
        obs_groups = defaultdict(list)
        for e in evidence:
            # Group key: evidence_type + first 40 chars of value (rough dedup)
            group_key = f"{e['evidence_type']}::{e['value'][:40].lower()}"
            obs_groups[group_key].append(e)

        single_source = 0
        cross_period = 0
        cross_source_type = 0
        for key, group in obs_groups.items():
            group_docs = set(e.get("document_id") for e in group)
            group_periods = set(self._get_meta(e, "period", self._period_from_date(e)) for e in group)
            group_src_types = set(e.get("source_type") for e in group)

            if len(group_docs) == 1:
                single_source += 1
            if len(group_periods) >= 2:
                cross_period += 1
            if len(group_src_types) >= 2:
                cross_source_type += 1

        # Detect conflicts: same evidence_type, same period, different numeric values
        conflicts = self._detect_conflicts(evidence)

        # Breadth verdict
        n_categories = len(source_categories)
        n_periods = len(periods)
        n_docs = len(docs)

        if n_categories >= 2 and n_periods >= 3 and n_docs >= 4:
            verdict = "broad"
        elif n_categories >= 2 and n_periods >= 2:
            verdict = "adequate"
        elif n_docs >= 2:
            verdict = "narrow"
        else:
            verdict = "single-source"

        return {
            "source_types_seen": sorted(source_types),
            "source_categories": sorted(source_categories),
            "source_types_missing": sorted(categories_missing),
            "periods_covered": n_periods,
            "document_count": n_docs,
            "observation_support": {
                "single_source": single_source,
                "cross_period": cross_period,
                "cross_source_type": cross_source_type,
            },
            "conflicts": conflicts,
            "breadth_verdict": verdict,
        }

    def _detect_conflicts(self, evidence: list[dict]) -> list[dict]:
        """Find observations where different sources provide different values for the same thing."""
        conflicts = []
        # Group by (evidence_type, period) where numeric values exist
        keyed = defaultdict(list)
        for e in evidence:
            if e.get("value_numeric") is not None:
                period = self._get_meta(e, "period", self._period_from_date(e))
                k = (e["evidence_type"], period)
                keyed[k].append(e)

        for (etype, period), group in keyed.items():
            if len(group) < 2:
                continue
            values = set(e["value_numeric"] for e in group)
            if len(values) > 1:
                conflicts.append({
                    "type": etype, "period": period,
                    "values": sorted(values),
                    "observation_count": len(group),
                    "detail": f"{etype} in {period}: conflicting values {sorted(values)}",
                })
        return conflicts

    # ── P2: Subtle evolution detection ───────────────────────

    def get_chronology(self) -> dict:
        """Estimate-relevant chronology with subtle evolution detection."""
        evidence = self._load_all_evidence()
        all_themes = defaultdict(list)

        for e in evidence:
            period = self._get_meta(e, "period", self._period_from_date(e))
            all_themes[e["evidence_type"]].append((period, e))

        # Standard metric evolution
        revenue_cadence = self._build_metric_evolution(
            all_themes, {"GROWTH_CADENCE", "KEY_METRIC"},
            lambda e: any(kw in e["value"].lower() for kw in ["revenue", "growth", "sss", "comp"]))
        margin_evolution = self._build_metric_evolution(
            all_themes, {"MARGIN_CADENCE", "COST_STRUCTURE"},
            lambda e: any(kw in e["value"].lower() for kw in ["margin", "cost", "ebit", "food", "labor"]))
        guidance_evolution = self._build_period_sequence(all_themes.get("GUIDANCE_ITEM", []))
        capital_evolution = self._build_period_sequence(all_themes.get("CAPITAL_ALLOCATION", []))

        # P2: Subtle framing detection
        framing_shifts = self._detect_framing_shifts(all_themes.get("MANAGEMENT_THEME", []))
        issue_maturation = self._detect_issue_maturation(all_themes)
        emphasis_changes = self._detect_emphasis_changes(all_themes)

        # Persistent vs fading debates
        debate_data = all_themes.get("RECURRING_DEBATE", [])
        persistent_debates, fading_debates = self._classify_debates(debate_data)

        # Management emphasis shifts (exact text change detection)
        mgmt_shifts = self._detect_shifts(all_themes.get("MANAGEMENT_THEME", []))

        inflections = [e for e in evidence if e["evidence_type"] == "RECENT_CHANGE"]

        return {
            "periods_covered": sorted(set(
                self._get_meta(e, "period", self._period_from_date(e)) for e in evidence)),
            "total_observations": len(evidence),
            "estimate_relevant_count": sum(
                1 for e in evidence if e["evidence_type"] in ESTIMATE_RELEVANT_TYPES),
            "revenue_cadence": revenue_cadence,
            "margin_evolution": margin_evolution,
            "guidance_evolution": guidance_evolution,
            "capital_allocation_evolution": capital_evolution,
            "persistent_debates": persistent_debates,
            "fading_debates": fading_debates,
            "management_emphasis_shifts": mgmt_shifts,
            "recent_inflections": inflections,
            # P2: Subtle evolution
            "framing_shifts": framing_shifts,
            "issue_maturation": issue_maturation,
            "emphasis_changes": emphasis_changes,
        }

    def _detect_framing_shifts(self, mgmt_data: list) -> list[dict]:
        """
        Detect when management language shifts between framing categories.
        e.g. growth-oriented → efficiency-oriented
        """
        if len(mgmt_data) < 2:
            return []
        shifts = []
        sorted_data = sorted(mgmt_data, key=lambda x: x[0])
        for i in range(1, len(sorted_data)):
            prev_period, prev_e = sorted_data[i - 1]
            curr_period, curr_e = sorted_data[i]
            prev_text = prev_e["value"].lower()
            curr_text = curr_e["value"].lower()

            prev_growth = len(GROWTH_FRAMING & set(prev_text.split()))
            prev_efficiency = len(EFFICIENCY_FRAMING & set(prev_text.split()))
            curr_growth = len(GROWTH_FRAMING & set(curr_text.split()))
            curr_efficiency = len(EFFICIENCY_FRAMING & set(curr_text.split()))

            if prev_growth > prev_efficiency and curr_efficiency > curr_growth:
                shifts.append({
                    "from_period": prev_period, "to_period": curr_period,
                    "shift_type": "growth_to_efficiency",
                    "certainty": "inferred",
                    "estimate_relevant": True,
                    "detail": f"Management framing shifted from growth-oriented to efficiency-oriented",
                })
            elif prev_efficiency > prev_growth and curr_growth > curr_efficiency:
                shifts.append({
                    "from_period": prev_period, "to_period": curr_period,
                    "shift_type": "efficiency_to_growth",
                    "certainty": "inferred",
                    "estimate_relevant": True,
                    "detail": f"Management framing shifted from efficiency-oriented to growth-oriented",
                })
        return shifts

    def _detect_issue_maturation(self, all_themes: dict) -> list[dict]:
        """
        Detect when an issue's language shifts from temporary to structural
        or vice versa across periods.
        """
        maturation = []
        for etype in ("RECURRING_DEBATE", "MARGIN_CADENCE", "RISK_FACTOR"):
            data = all_themes.get(etype, [])
            if len(data) < 2:
                continue
            sorted_data = sorted(data, key=lambda x: x[0])
            for i in range(1, len(sorted_data)):
                prev_period, prev_e = sorted_data[i - 1]
                curr_period, curr_e = sorted_data[i]
                prev_words = set(prev_e["value"].lower().split())
                curr_words = set(curr_e["value"].lower().split())

                prev_temp = len(TEMPORARY_FRAMING & prev_words)
                curr_struct = len(STRUCTURAL_FRAMING & curr_words)

                if prev_temp > 0 and curr_struct > 0:
                    maturation.append({
                        "type": etype,
                        "from_period": prev_period, "to_period": curr_period,
                        "shift": "temporary_to_structural",
                        "certainty": "inferred",
                        "estimate_relevant": True,
                        "detail": f"{etype}: language shifted from temporary to structural framing",
                    })
        return maturation

    def _detect_emphasis_changes(self, all_themes: dict) -> list[dict]:
        """
        Detect when certain evidence types appear in later periods
        but not earlier ones, suggesting a shift in what matters.
        """
        changes = []
        # Build period -> set of evidence types
        period_types = defaultdict(set)
        for etype, data in all_themes.items():
            for period, e in data:
                period_types[period].add(etype)

        sorted_periods = sorted(period_types.keys())
        if len(sorted_periods) < 2:
            return []

        early_types = period_types[sorted_periods[0]]
        late_types = period_types[sorted_periods[-1]]

        new_in_late = late_types - early_types
        dropped_from_early = early_types - late_types

        for t in new_in_late:
            if t in ESTIMATE_RELEVANT_TYPES:
                changes.append({
                    "type": t,
                    "direction": "newly_emphasized",
                    "first_seen": sorted_periods[-1],
                    "certainty": "weak",
                    "detail": f"{t} appears in {sorted_periods[-1]} but not {sorted_periods[0]}",
                })
        for t in dropped_from_early:
            if t in ESTIMATE_RELEVANT_TYPES:
                changes.append({
                    "type": t,
                    "direction": "de_emphasized",
                    "last_seen": sorted_periods[0],
                    "certainty": "weak",
                    "detail": f"{t} appears in {sorted_periods[0]} but not {sorted_periods[-1]}",
                })
        return changes

    # ── P3: Confidence with penalization ─────────────────────

    def derive_model_architecture(self) -> dict:
        """Model architecture with confidence penalized by source breadth."""
        evidence = self._load_all_evidence()
        by_type = defaultdict(list)
        for e in evidence:
            by_type[e["evidence_type"]].append(e)

        breadth = self.assess_source_breadth()

        arch = {
            "segment_tabs": [], "kpi_structure": [], "cost_buckets": [],
            "debate_areas": [], "candidate_drivers": [],
            "missing_evidence": [], "overall_confidence": "incomplete",
            "evidence_density": {}, "confidence_penalties": [],
        }

        # Segments
        for s in by_type.get("SEGMENT_INFO", []):
            arch["segment_tabs"].append({
                "name": s["value"],
                "confidence": "high" if self._get_meta(s, "certainty", "observed") == "observed" else "provisional",
                "evidence_count": 1, "source_count": 1,
                "rationale": "Directly observed in source document",
            })
        if not by_type.get("SEGMENT_INFO"):
            arch["missing_evidence"].append("No segment information")

        # KPIs
        kpi_evidence = by_type.get("KEY_METRIC", [])
        seen_kpis = set()
        for m in kpi_evidence:
            name = m["value"]
            if name in seen_kpis:
                for existing in arch["kpi_structure"]:
                    if existing["metric"] == name:
                        existing["evidence_count"] += 1
                        existing["source_count"] = len(set(
                            e.get("document_id") for e in kpi_evidence if e["value"] == name))
                        if m.get("value_numeric") is not None:
                            existing["latest_value"] = m["value_numeric"]
                continue
            seen_kpis.add(name)
            supporting = [e for e in kpi_evidence if e["value"] == name]
            src_count = len(set(e.get("document_id") for e in supporting))
            arch["kpi_structure"].append({
                "metric": name, "latest_value": m.get("value_numeric"),
                "unit": m.get("unit"),
                "confidence": "high" if len(supporting) >= 2 and src_count >= 2 else "provisional",
                "evidence_count": len(supporting), "source_count": src_count,
                "rationale": f"{len(supporting)} obs from {src_count} sources",
            })
        if not kpi_evidence:
            arch["missing_evidence"].append("No key metrics identified")

        # Cost buckets
        for c in by_type.get("COST_STRUCTURE", []):
            arch["cost_buckets"].append({
                "name": c["value"],
                "confidence": "high" if self._get_meta(c, "certainty", "observed") == "observed" else "provisional",
                "evidence_count": 1,
                "rationale": f"Directly observed",
            })
        inferred_costs = set()
        for m in by_type.get("MARGIN_CADENCE", []):
            val = m["value"].lower()
            for kw, label in [("food", "Food/input costs"), ("cogs", "Food/input costs"),
                              ("labor", "Labor costs"), ("occupancy", "Occupancy/rent")]:
                if kw in val and label not in inferred_costs:
                    inferred_costs.add(label)
                    arch["cost_buckets"].append({
                        "name": label, "confidence": "inferred", "evidence_count": 1,
                        "rationale": f"Inferred from margin discussion mentioning '{kw}'",
                    })
        if not by_type.get("COST_STRUCTURE") and not inferred_costs:
            arch["missing_evidence"].append("No cost structure data")

        # Debates + drivers
        for d in by_type.get("RECURRING_DEBATE", []):
            arch["debate_areas"].append({
                "debate": d["value"],
                "estimate_relevant": any(kw in d["value"].lower()
                    for kw in ["margin", "growth", "sss", "cost", "pricing", "guidance"]),
            })
        for m in by_type.get("KEY_METRIC", []):
            supporting = [e for e in kpi_evidence if e["value"] == m["value"]]
            arch["candidate_drivers"].append({
                "driver": m["value"], "source": "key_metric",
                "confidence": "high" if len(supporting) >= 2 else "provisional",
            })
        for c in by_type.get("RECENT_CHANGE", []):
            arch["candidate_drivers"].append({
                "driver": c["value"], "source": "recent_change", "confidence": "exploratory",
            })

        # Evidence density
        arch["evidence_density"] = {
            "total_observations": len(evidence),
            "source_documents": len(set(e.get("document_id") for e in evidence)),
            "source_types": len(set(e.get("source_type") for e in evidence)),
            "periods_covered": breadth["periods_covered"],
            "estimate_relevant": sum(1 for e in evidence if e["evidence_type"] in ESTIMATE_RELEVANT_TYPES),
            "observed": sum(1 for e in evidence if self._get_meta(e, "certainty", "observed") == "observed"),
            "inferred": sum(1 for e in evidence if self._get_meta(e, "certainty", "observed") == "inferred"),
        }

        # P3: Confidence penalization
        penalties = []
        if breadth["breadth_verdict"] == "single-source":
            penalties.append("Single source document — conclusions may not be robust")
        elif breadth["breadth_verdict"] == "narrow":
            penalties.append("Narrow source breadth — limited document diversity")
        if breadth["periods_covered"] <= 1:
            penalties.append("Single period — no time-series perspective")
        if breadth["source_types_missing"]:
            penalties.append(f"Missing source types: {', '.join(breadth['source_types_missing'])}")
        inferred_ratio = arch["evidence_density"]["inferred"] / max(arch["evidence_density"]["total_observations"], 1)
        if inferred_ratio > 0.5:
            penalties.append(f"High inference ratio ({inferred_ratio:.0%}) — most evidence is inferred")
        if breadth["conflicts"]:
            penalties.append(f"{len(breadth['conflicts'])} unresolved conflict(s)")

        arch["confidence_penalties"] = penalties

        # Overall confidence — penalized version
        has_segments = bool(arch["segment_tabs"])
        has_kpis = len(arch["kpi_structure"]) >= 2
        has_costs = bool(arch["cost_buckets"])
        no_missing = not arch["missing_evidence"]

        # Start with existence-based confidence
        if has_segments and has_kpis and has_costs and no_missing:
            base_confidence = "high"
        elif has_kpis and (has_segments or has_costs):
            base_confidence = "provisional"
        elif has_kpis:
            base_confidence = "exploratory"
        else:
            base_confidence = "incomplete"

        # Penalize
        confidence_levels = ["incomplete", "exploratory", "provisional", "high"]
        idx = confidence_levels.index(base_confidence)
        penalty_severity = len(penalties)
        if penalty_severity >= 3:
            idx = max(0, idx - 2)
        elif penalty_severity >= 1:
            idx = max(0, idx - 1)

        arch["overall_confidence"] = confidence_levels[idx]
        return arch

    def get_extraction_quality(self) -> dict:
        """Score extraction quality including source breadth."""
        evidence = self._load_all_evidence()
        breadth = self.assess_source_breadth()

        by_certainty = defaultdict(int)
        by_type = defaultdict(int)
        by_relevance = defaultdict(int)
        source_docs = set()
        for e in evidence:
            by_certainty[self._get_meta(e, "certainty", "observed")] += 1
            by_type[e["evidence_type"]] += 1
            by_relevance[self._get_meta(e, "est_relevance", "low")] += 1
            source_docs.add(e.get("document_id"))

        total = len(evidence)
        observed_pct = by_certainty["observed"] / total if total else 0
        estimate_relevant = by_relevance.get("high", 0) + by_relevance.get("medium", 0)

        # Quality now factors in source breadth
        if (total >= 10 and observed_pct >= 0.7 and estimate_relevant >= 5
                and breadth["breadth_verdict"] in ("broad", "adequate")):
            quality = "strong"
        elif (total >= 5 and observed_pct >= 0.5 and estimate_relevant >= 2
              and breadth["breadth_verdict"] != "single-source"):
            quality = "adequate"
        elif total >= 3:
            quality = "thin"
        else:
            quality = "insufficient"

        return {
            "total_observations": total,
            "source_document_count": len(source_docs),
            "by_certainty": dict(by_certainty),
            "by_type": dict(by_type),
            "by_estimate_relevance": dict(by_relevance),
            "observed_pct": round(observed_pct, 2),
            "estimate_relevant_count": estimate_relevant,
            "source_breadth": breadth["breadth_verdict"],
            "conflicts": breadth["conflicts"],
            "quality": quality,
        }

    # ── Synthesize ───────────────────────────────────────────

    def synthesize(self, status: str = "complete") -> str:
        evidence = self._load_all_evidence()
        if not evidence:
            return self._save_empty_context(status="in_progress")

        by_type = defaultdict(list)
        source_doc_ids = set()
        for e in evidence:
            by_type[e["evidence_type"]].append(e)
            source_doc_ids.add(e.get("document_id"))

        business_desc = self._join(by_type.get("BUSINESS_DESCRIPTION", []))
        revenue_model = self._join(by_type.get("REVENUE_MODEL", []))
        segments = self._to_json(by_type.get("SEGMENT_INFO", []))
        key_metrics = self._to_json(by_type.get("KEY_METRIC", []))
        growth = self._join(by_type.get("GROWTH_CADENCE", []))
        margins = self._join(by_type.get("MARGIN_CADENCE", []))
        historical = f"{growth}\n{margins}".strip() if growth or margins else None
        debates = self._join(by_type.get("RECURRING_DEBATE", []))
        mgmt = self._join(by_type.get("MANAGEMENT_THEME", []))
        capalloc = self._join(by_type.get("CAPITAL_ALLOCATION", []))

        candidate_edges = self._derive_edges(by_type)
        model_arch = self.derive_model_architecture()
        coverage = self._assess_coverage(by_type)

        row = self.conn.execute(
            "SELECT MAX(context_version) FROM business_context WHERE company_id=?",
            (self.company_id,)).fetchone()
        next_version = (row[0] or 0) + 1

        context_id = new_id()
        self.conn.execute(
            """INSERT INTO business_context
               (context_id, company_id, context_version, status,
                business_description, revenue_model, segment_map,
                geographic_map, key_metrics, historical_cadence,
                recurring_debates, management_framing,
                capital_allocation_pattern, candidate_edges,
                candidate_model_structure, readiness_for_design,
                sources_reviewed, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (context_id, self.company_id, next_version, status,
             business_desc, revenue_model, segments,
             self._to_json(by_type.get("SEGMENT_INFO", []), key="geo"),
             key_metrics, historical, debates, mgmt, capalloc,
             json.dumps(candidate_edges) if candidate_edges else None,
             json.dumps(model_arch) if model_arch else None,
             coverage["assessment"],
             json.dumps(list(source_doc_ids)), self.run_id))
        self.conn.commit()
        return context_id

    def assess_readiness(self) -> tuple[bool, str]:
        return BusinessContextGate.is_ready(self.conn, self.company_id)

    def derive_research_candidates(self) -> dict:
        """P4: Honest handoff with source breadth, conflicts, penalties."""
        ctx = BusinessContextGate.get_latest(self.conn, self.company_id)
        if not ctx:
            return {"status": "no_context"}

        candidates = {
            "questions": [], "drivers": [], "workstreams": [],
            "kill_conditions": [], "edge_candidates": [],
            "model_architecture": None, "chronology": None,
            "extraction_quality": None, "source_breadth": None,
            "readiness": ctx.get("readiness_for_design", "unknown"),
        }

        # Questions from debates
        debates = ctx.get("recurring_debates", "")
        if debates:
            for line in debates.split("\n"):
                line = line.strip()
                if not line:
                    continue
                q = line if "?" in line else f"What is the current state of: {line}?"
                candidates["questions"].append({"question": q, "source": "recurring_debate", "priority": "high"})

        # Drivers from metrics
        metrics_raw = ctx.get("key_metrics")
        if metrics_raw:
            try:
                metrics = json.loads(metrics_raw)
                for item in (metrics if isinstance(metrics, list) else []):
                    text = item.get("value", item) if isinstance(item, dict) else str(item)
                    candidates["drivers"].append({"name": text, "source": "key_metric"})
            except (json.JSONDecodeError, TypeError):
                pass

        # Workstreams
        if ctx.get("segment_map"):
            candidates["workstreams"].append({"name": "SEGMENT_BUILD", "justification": "Multiple segments identified"})
        if ctx.get("historical_cadence"):
            candidates["workstreams"].append({"name": "KPI_FORECAST", "justification": "Historical cadence available"})
        if ctx.get("capital_allocation_pattern"):
            candidates["workstreams"].append({"name": "CAPITAL_ALLOCATION", "justification": "Pattern identified"})
        candidates["workstreams"].append({"name": "OPERATING_BUILD", "justification": "Required for any estimate"})
        candidates["workstreams"].append({"name": "GUIDANCE_COMPARISON", "justification": "Compare vs guidance"})

        # Edges
        edges_raw = ctx.get("candidate_edges")
        if edges_raw:
            try:
                candidates["edge_candidates"] = json.loads(edges_raw)
            except (json.JSONDecodeError, TypeError):
                pass

        # Kill conditions
        if debates:
            for line in debates.split("\n"):
                line = line.strip()
                if line and any(neg in line.lower() for neg in
                               ["decelerate", "compress", "risk", "fail", "miss", "slow"]):
                    candidates["kill_conditions"].append({"condition": line, "source": "recurring_debate"})

        # P1+P3: Source breadth + architecture with penalties
        candidates["source_breadth"] = self.assess_source_breadth()
        candidates["model_architecture"] = self.derive_model_architecture()
        candidates["extraction_quality"] = self.get_extraction_quality()

        # Chronology
        chrono = self.get_chronology()
        candidates["chronology"] = {
            "periods_covered": chrono["periods_covered"],
            "estimate_relevant_count": chrono["estimate_relevant_count"],
            "persistent_debates": len(chrono["persistent_debates"]),
            "margin_evolution_points": len(chrono["margin_evolution"]),
            "revenue_cadence_points": len(chrono["revenue_cadence"]),
            "inflections": len(chrono["recent_inflections"]),
            "framing_shifts": len(chrono["framing_shifts"]),
            "issue_maturation": len(chrono["issue_maturation"]),
        }

        # Chronology-derived questions
        for shift in chrono.get("management_emphasis_shifts", []):
            candidates["questions"].append({
                "question": f"What drove the shift from '{shift['from'][:30]}' to '{shift['to'][:30]}'?",
                "source": "chronology_shift", "priority": "high"})
        for shift in chrono.get("framing_shifts", []):
            candidates["questions"].append({
                "question": f"Management framing {shift['shift_type']} — what changed?",
                "source": "framing_shift", "priority": "high"})
        if chrono["margin_evolution"]:
            candidates["questions"].append({
                "question": "What drove the margin evolution across periods?",
                "source": "chronology_margin", "priority": "high"})

        return candidates

    # ── Helpers ──────────────────────────────────────────────

    def _load_all_evidence(self) -> list[dict]:
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute("""
            SELECT ei.evidence_id, ei.evidence_type, ei.evidence_key,
                   ei.value, ei.value_numeric, ei.unit, ei.as_of_date, ei.notes,
                   sd.document_id, sd.source_name, sd.source_type,
                   sd.source_published_at
            FROM evidence_item ei
            JOIN source_document sd ON ei.document_id = sd.document_id
            WHERE ei.company_id = ? AND ei.extraction_method = 'ORIENTATION_PASS'
            ORDER BY ei.as_of_date, sd.source_published_at
        """, (self.company_id,)).fetchall()
        self.conn.row_factory = old
        return [dict(r) for r in rows]

    @staticmethod
    def _get_meta(e: dict, key: str, default: str = "") -> str:
        notes = e.get("notes") or ""
        for part in notes.split("|"):
            if part.startswith(f"{key}:"):
                return part[len(key) + 1:]
        return default

    @staticmethod
    def _period_from_date(e: dict) -> str:
        for f in ("as_of_date", "source_published_at"):
            v = e.get(f, "")
            if v and len(v) >= 4:
                return v[:4]
        return "unknown"

    def _build_metric_evolution(self, themes, types, filter_fn=None):
        points = []
        for t in types:
            for period, e in themes.get(t, []):
                if filter_fn and not filter_fn(e):
                    continue
                points.append({"period": period, "observation": e["value"],
                              "numeric": e.get("value_numeric"), "unit": e.get("unit"),
                              "certainty": self._get_meta(e, "certainty", "observed"), "type": t})
        return sorted(points, key=lambda x: x["period"])

    @staticmethod
    def _build_period_sequence(data):
        return [{"period": p, "observation": e["value"], "numeric": e.get("value_numeric")}
                for p, e in sorted(data, key=lambda x: x[0])]

    @staticmethod
    def _detect_shifts(data):
        if len(data) < 2:
            return []
        shifts = []
        sd = sorted(data, key=lambda x: x[0])
        for i in range(1, len(sd)):
            if sd[i - 1][1]["value"] != sd[i][1]["value"]:
                shifts.append({"from_period": sd[i-1][0], "to_period": sd[i][0],
                              "from": sd[i-1][1]["value"], "to": sd[i][1]["value"]})
        return shifts

    def _classify_debates(self, debate_data):
        persistent, fading = [], []
        if not debate_data:
            return persistent, fading
        debate_periods = defaultdict(set)
        for p, e in debate_data:
            debate_periods[e["value"]].add(p)
        all_periods = set(p for p, _ in debate_data)
        for text, periods in debate_periods.items():
            if len(periods) >= 2:
                persistent.append({"debate": text, "periods": sorted(periods)})
            elif len(all_periods) > 1 and len(periods) == 1:
                fading.append({"debate": text, "last_seen": sorted(periods)[-1]})
        return persistent, fading

    @staticmethod
    def _join(evidence_list):
        if not evidence_list:
            return None
        return "\n".join(
            e.get("value", "") + (f" ({e['value_numeric']}{e.get('unit','')})"
                                   if e.get("value_numeric") is not None else "")
            for e in evidence_list)

    @staticmethod
    def _to_json(evidence_list, key=None):
        if not evidence_list:
            return None
        items = []
        for e in evidence_list:
            item = {"value": e.get("value", "")}
            if e.get("value_numeric") is not None:
                item["numeric"] = e["value_numeric"]
            if e.get("unit"):
                item["unit"] = e["unit"]
            if e.get("as_of_date"):
                item["as_of"] = e["as_of_date"]
            items.append(item)
        return json.dumps(items)

    def _derive_edges(self, by_type):
        edges = []
        for e in by_type.get("RECURRING_DEBATE", []):
            edges.append(e.get("value", ""))
        for e in by_type.get("RECENT_CHANGE", []):
            edges.append(f"Recent change: {e.get('value', '')}")
        return edges[:5]

    def _assess_coverage(self, by_type):
        covered = set(by_type.keys())
        important = {"BUSINESS_DESCRIPTION", "REVENUE_MODEL", "KEY_METRIC"}
        useful = {"SEGMENT_INFO", "GROWTH_CADENCE", "MARGIN_CADENCE",
                  "MANAGEMENT_THEME", "RECURRING_DEBATE"}
        important_missing = important - covered
        useful_covered = useful & covered
        if important_missing:
            assessment = f"Not yet ready — missing: {', '.join(important_missing)}."
        elif len(useful_covered) < 2:
            assessment = "Partially ready — have basics but limited depth."
        else:
            assessment = (f"Ready for research design — {len(covered)} categories, "
                         f"{len(important & covered)} critical, {len(useful_covered)} useful.")
        return {"assessment": assessment}

    def _save_empty_context(self, status):
        row = self.conn.execute(
            "SELECT MAX(context_version) FROM business_context WHERE company_id=?",
            (self.company_id,)).fetchone()
        nv = (row[0] or 0) + 1
        cid = new_id()
        self.conn.execute(
            """INSERT INTO business_context (context_id, company_id, context_version,
               status, readiness_for_design, created_by_run) VALUES (?,?,?,?,?,?)""",
            (cid, self.company_id, nv, status,
             "Not yet ready — no source documents reviewed", self.run_id))
        self.conn.commit()
        return cid


class BusinessContextGate:
    @staticmethod
    def get_latest(conn, company_id):
        old = conn.row_factory
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM business_context WHERE company_id=? ORDER BY context_version DESC LIMIT 1",
            (company_id,)).fetchone()
        conn.row_factory = old
        return dict(row) if row else None

    @staticmethod
    def is_ready(conn, company_id):
        ctx = BusinessContextGate.get_latest(conn, company_id)
        if not ctx:
            return False, "No business context built yet."
        if ctx.get("status") != "complete":
            return False, f"Status is '{ctx.get('status')}', not complete."
        readiness = ctx.get("readiness_for_design", "")
        if readiness and any(neg in readiness.lower() for neg in
                           ["not yet", "not ready", "need more", "insufficient"]):
            return False, f"Orientation says: {readiness}"
        missing = []
        if not ctx.get("business_description"):
            missing.append("business_description")
        if not ctx.get("revenue_model"):
            missing.append("revenue_model")
        if not ctx.get("candidate_edges"):
            missing.append("candidate_edges")
        if missing:
            return False, f"Missing: {', '.join(missing)}"
        return True, "Ready for research design."
