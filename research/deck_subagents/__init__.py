"""
Deck vision subagents — specialized Claude Vision analyzers that extract
different dimensions from investor decks (earnings / investor_day /
conference / shareholder_letter).

Each subagent receives ALL deck pages as images in one Claude Vision call
and returns structured JSON with page-level evidence citations.

Orchestrator: research.deck_analyzer
"""

from research.deck_subagents._base import (
    DeckSubagentResult, call_vision_subagent, EVIDENCE_BLOCK_VISION,
)
from research.deck_subagents._pdf_to_images import (
    PageImage, rasterize_pdf, rasterize_pdf_cached,
)

__all__ = [
    "DeckSubagentResult",
    "call_vision_subagent",
    "EVIDENCE_BLOCK_VISION",
    "PageImage",
    "rasterize_pdf",
    "rasterize_pdf_cached",
]
