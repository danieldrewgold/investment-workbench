"""
Transcript subagents — specialized analyzers that each extract one dimension
from 12-quarter earnings call transcripts.

Each subagent is a focused Claude call with a narrow prompt, evidence-grounding
requirements, and structured JSON output. They run in parallel via the
orchestrator in research.transcript_analyzer.

The old monolithic digest was scratched. This package replaces it.
"""

from research.transcript_subagents._base import (
    SubagentResult,
    SubagentError,
    call_subagent,
    EVIDENCE_SCHEMA_BLOCK,
)
from research.transcript_subagents._context_pack import (
    ContextPack,
    build_context_pack,
)

__all__ = [
    "SubagentResult",
    "SubagentError",
    "call_subagent",
    "EVIDENCE_SCHEMA_BLOCK",
    "ContextPack",
    "build_context_pack",
]
