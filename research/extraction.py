"""
Document Extraction

Sends raw filing text to Claude API and returns structured observations
that feed directly into orientation and schema selection.

This is the bridge between raw source documents and the research pipeline.
When the API is unavailable, falls back to manual observations.
"""

import json
import os
import urllib.request
from dataclasses import dataclass, field


EXTRACTION_PROMPT = """You are extracting structured observations from a company filing for an investment research system.

Extract observations in the following JSON format. Return ONLY a JSON array, no other text:

[
  {
    "type": "<evidence_type>",
    "key": "<short key for this observation>",
    "value": "<specific observation with exact numbers>",
    "numeric": <number or null>,
    "unit": "<PCT, USD_M, COUNT, BPS, or null>",
    "period": "<period label like FY2025, Q4 2025>",
    "estimate_relevance": "<high, medium, or low>",
    "source": "<brief source description>"
  }
]

Evidence types to use:
- KEY_METRIC: Important reported metric (revenue, SSS, margin, EPS, store count)
- GROWTH_CADENCE: Growth rate or trend
- MARGIN_CADENCE: Margin level or change
- COST_STRUCTURE: Cost bucket as % of revenue
- MANAGEMENT_THEME: How management frames the business
- GUIDANCE_ITEM: Forward-looking guidance
- CAPITAL_ALLOCATION: Buyback, dividend, investment
- RECENT_CHANGE: Something that changed or inflected
- SEGMENT_MIX: Revenue or business segment composition
- BUSINESS_STRUCTURE: Franchise vs owned, subscription vs transaction, etc.

Focus on observations that would matter for:
1. Choosing the right economic model structure (franchise vs company-operated, etc.)
2. Estimating future revenue, margins, and EPS
3. Identifying key business drivers and risks

Be specific — include exact numbers, percentages, and period labels.
Do NOT extract boilerplate, legal disclaimers, or generic company descriptions.

Here is the document text:

"""


@dataclass
class ExtractionResult:
    """Result of extracting observations from a document."""
    observations: list = field(default_factory=list)
    source_text_length: int = 0
    extraction_method: str = "none"  # "claude_api", "manual", "fallback"
    confidence: float = 0.0
    total_count: int = 0
    estimate_relevant_count: int = 0
    numeric_count: int = 0
    by_type: dict = field(default_factory=dict)
    error: str = ""


def extract_from_text(
    text: str,
    source_name: str = "filing",
    api_key: str = None,
) -> ExtractionResult:
    """
    Extract structured observations from raw document text.

    Uses Claude API if key is available, otherwise returns empty result
    with a clear signal that extraction didn't happen.
    """
    result = ExtractionResult(source_text_length=len(text))

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        result.extraction_method = "no_api_key"
        result.error = "No ANTHROPIC_API_KEY available"
        return result

    # Call Claude API
    prompt = EXTRACTION_PROMPT + text

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            api_result = json.loads(resp.read())

        response_text = api_result["content"][0]["text"]

        # Strip markdown fences
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

        observations = json.loads(clean)
        result.observations = observations
        result.extraction_method = "claude_api"
        result.total_count = len(observations)

        # Score quality
        for obs in observations:
            t = obs.get("type", "UNKNOWN")
            result.by_type[t] = result.by_type.get(t, 0) + 1
            if obs.get("estimate_relevance") in ("high", "medium"):
                result.estimate_relevant_count += 1
            if obs.get("numeric") is not None:
                result.numeric_count += 1

        # Confidence based on extraction quality
        if result.total_count >= 15 and result.estimate_relevant_count >= 8:
            result.confidence = 0.85
        elif result.total_count >= 10 and result.estimate_relevant_count >= 5:
            result.confidence = 0.70
        elif result.total_count >= 5:
            result.confidence = 0.50
        else:
            result.confidence = 0.30

    except json.JSONDecodeError as e:
        result.extraction_method = "claude_api_parse_error"
        result.error = f"Failed to parse API response as JSON: {e}"
        result.confidence = 0.0
    except Exception as e:
        result.extraction_method = "claude_api_error"
        result.error = f"API call failed: {e}"
        result.confidence = 0.0

    return result


def extraction_to_orientation_observations(
    extraction: ExtractionResult,
    document_source: str = "",
) -> list[dict]:
    """
    Convert extraction results into the format expected by
    OrientationWorkflow.digest_document() and
    build_evidence_from_observations() for schema selection.
    """
    orient_obs = []
    for obs in extraction.observations:
        orient_obs.append({
            "key": obs.get("key", obs.get("type", "observation")),
            "value": obs.get("value", obs.get("text", "")),
            "numeric": obs.get("numeric"),
            "unit": obs.get("unit"),
            "period": obs.get("period", ""),
            "source": obs.get("source", document_source),
            "estimate_relevance": obs.get("estimate_relevance", "medium"),
            "evidence_type": obs.get("type", "KEY_METRIC"),
        })
    return orient_obs


def extraction_to_schema_evidence(
    extraction: ExtractionResult,
) -> list:
    """
    Convert extraction results into SchemaEvidence objects for schema selection.
    Uses keyword matching as baseline.
    """
    from research.schema_selection import build_evidence_from_observations
    obs_dicts = extraction_to_orientation_observations(extraction)
    return build_evidence_from_observations(obs_dicts)


# ═══════════════════════════════════════════════════════════════
# Intelligent Schema Classification (Claude API)
# ═══════════════════════════════════════════════════════════════

SCHEMA_CLASSIFY_PROMPT = """You are classifying a company's economic structure for an investment model.

Given these observations extracted from the company's earnings release, determine:
1. Which economic model schema best fits this company
2. What specific evidence supports your choice
3. Your confidence (0-100)

Available schemas:
- "company_operated_restaurant": Company owns/operates most locations. Revenue = store-level sales. Costs = food, labor, occupancy. (e.g. Chipotle, Texas Roadhouse)
- "franchise_restaurant": Most locations franchised. Revenue = royalties + ad fund + possibly supply chain. Costs = SG&A + interest. (e.g. Wingstop, Domino's, McDonald's)
- "saas_subscription": Recurring subscription revenue, high gross margins (60%+), R&D/S&M/G&A cost structure. (e.g. ServiceNow, Verisk, Datadog)
- "general": General-purpose model for any company. Revenue = prior × (1 + growth%). Costs = COGS + OpEx. Use for hardware manufacturing, pre-profit companies, industrial, defense, or any structure where no specialized schema fits. This always works — it's just less precise than a specialized schema.

Return ONLY a JSON object, no other text:
{
  "chosen_schema": "<schema key from above>",
  "confidence": <0-100>,
  "reason": "<one sentence explaining why>",
  "signals": [
    {"signal_type": "<type>", "value": "<value>", "source": "<brief source>"}
  ]
}

Signal types to use:
- "revenue_type": "restaurant_sales", "royalty", "subscription", "product_sales"
- "franchise_pct": number 0-100
- "company_operated_pct": number 0-100
- "cost_disclosure": "food_labor_occupancy", "rd_sm_ga", "low_cogs_ratio"
- "metric_disclosed": "sss", "restaurant_margin", "new_store_openings", "system_wide_sales", "arr", "net_retention", "customer_count"
- "gross_margin_range": number (gross margin %)

Here are the observations:

"""


def classify_schema_via_api(
    extraction: ExtractionResult,
    api_key: str = None,
) -> list:
    """
    Use Claude API to intelligently classify which schema fits,
    returning SchemaEvidence objects that feed into the standard scorer.

    Falls back to keyword-based extraction_to_schema_evidence if API unavailable.
    """
    from research.schema_selection import SchemaEvidence

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not extraction.observations:
        return extraction_to_schema_evidence(extraction)

    # Build observation summary for the prompt
    obs_text = "\n".join(
        f"- [{obs.get('type','?')}] {obs.get('value', obs.get('text',''))[:120]}"
        for obs in extraction.observations
    )

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": SCHEMA_CLASSIFY_PROMPT + obs_text}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        classification = json.loads(text)

        # Convert to SchemaEvidence objects
        evidence = []
        for sig in classification.get("signals", []):
            stype = sig.get("signal_type", "")
            sval = sig.get("value", "")
            src = sig.get("source", "AI classification")

            # Convert numeric strings
            try:
                if stype in ("franchise_pct", "company_operated_pct", "gross_margin_range"):
                    sval = float(sval)
            except (ValueError, TypeError):
                pass

            evidence.append(SchemaEvidence(stype, sval, src))

        # Also add keyword-based evidence as supplementary
        keyword_evidence = extraction_to_schema_evidence(extraction)
        seen = {(e.signal_type, str(e.value)[:30]) for e in evidence}
        for e in keyword_evidence:
            key = (e.signal_type, str(e.value)[:30])
            if key not in seen:
                evidence.append(e)
                seen.add(key)

        return evidence

    except Exception:
        # Fall back to keyword matching
        return extraction_to_schema_evidence(extraction)
