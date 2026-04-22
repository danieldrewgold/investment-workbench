"""
Lightweight execution DAG for the research pipeline.

Turns the linear `run_research()` flow into a declarative dependency graph
so:
  - Independent fetches run in parallel (financials + transcripts + press
    releases + slide decks + consensus + overlay all kick off at once)
  - Each step's output is cached by content-hash of its inputs; re-runs
    skip steps whose inputs haven't changed
  - The execution trace is observable (timings, hits, misses)

Design:
  - Pure Python, no external orchestrator deps
  - ThreadPoolExecutor for parallel steps
  - Content-hash cache on disk at data/dag_cache/{ticker}/{step}_{hash}.json
  - Trace JSON at data/dag_traces/{ticker}_{timestamp}.json

This is orchestration, not rewriting. Each Step WRAPS an existing function
(transcript_analyzer.analyze_transcripts, etc.) — no re-implementation.
"""

from research.dag.core import Step, run_dag, StepResult, DagTrace

__all__ = ["Step", "run_dag", "StepResult", "DagTrace"]
