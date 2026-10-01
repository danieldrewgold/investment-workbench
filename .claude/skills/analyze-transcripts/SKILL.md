---
name: analyze-transcripts
description: Run 8 specialized transcript subagents (guidance, Q&A, tone, QTD, metrics, business, capital allocation, unusual disclosures) against a ticker's 12 quarters of earnings calls. Caches by content hash. Use when the user asks for deep transcript analysis on a specific ticker, or for input into a research pitch.
---

# Analyze Transcripts

Runs the investment-workbench transcript analyzer pipeline — 8 specialized subagents in parallel against 12 quarters of earnings call transcripts, with content-hash caching.

## When to use

- User says "analyze transcripts for TICKER" or similar
- User wants guidance / Q&A / tone / QTD / capital allocation insights on a ticker
- Feeding rich transcript context into a research pitch or memo
- Checking management credibility on guidance (beat/miss history with mgmt's own attribution)

## Do NOT use for

- Fetching raw transcripts (that's `research/transcript_fetcher.py` — handled by the analyzer internally)
- Running the full research pipeline (use `cli.py research TICKER` instead)
- Analyzing press releases or slide decks (separate workflow, not yet built)

## What the 8 subagents produce

| Subagent | What it extracts |
|---|---|
| `guidance_tracker` | Every guide issued, beat/miss history, mgmt attribution quality, current live guides, net credibility read |
| `qanda_analyzer` | First question every call (signal), recurring themes, dodged questions with evidence, persistent pressers |
| `tone_tracker` | Per-quarter tone with verbatim evidence, CEO vs CFO divergence, hedging language patterns, language downgrades |
| `qtd_extractor` | Verbatim QTD commentary with magnitude classification (exact wording preserved) |
| `metrics_highlighted` | Which metrics mgmt leads with, which get dropped, which are newly introduced, narrative frames |
| `business_understanding` | Revenue streams, unit economics, key drivers, competitive positioning, moat claims |
| `capital_allocation` | Buybacks (pace/pricing), dividends, M&A posture, debt, capex — with stated rationales |
| `unusual_disclosures` | Topics/framings/risks new to the most recent call vs prior 4 quarters |

Every claim in every subagent output carries `evidence_quote` (verbatim ≤50 words), `source_quarter`, and `speaker`. No paraphrasing in evidence.

## How to invoke

From the project root:

```bash
python -m research.transcript_analyzer CMG --verbose
```

Flags:
- `--verbose` — show per-subagent progress
- `--force` — bypass cache, recompute from scratch
- `--summary-only` — print high-level summary, skip the full subagent JSON dump
- `--max-parallel N` — adjust subagent concurrency (default 4)

## What to tell the user after running

1. **High-level state:** tone trajectory + credibility read + current live guides count
2. **The most useful subagent for their current question** — pull out the relevant subagent's output and surface the key findings with verbatim evidence
3. **Where it failed** — if any subagents failed (network, parse, rate limit), say so. Don't hide partial failures.
4. **Cache status** — "this was a cache hit, rerun with --force to refresh" or "fresh run, cached for future calls"

## Cost + timing

- Cold run: ~8 Claude Sonnet calls + 1 Haiku call, ~$1.25 API cost, ~60-90 seconds
- Cache hit: near-zero cost, < 1 second
- Cache keyed on raw transcript content hash; invalidates automatically when new quarters are added

## Output locations

- `data/context_packs/{TICKER}_{hash}.json` — preprocessed speaker/quarter/section-tagged transcripts
- `data/transcript_digests/{TICKER}_{hash}.json` — the aggregated 8-subagent digest
